"""The homepage's support endpoints and the plan thumbs (spec 2026-09-30 support-and-feedback §4.2, §6; spec 2026-10-12
the Help button §3.3): POST /api/help is the Help button on every page -- a report with the context Admiry adds by
itself; POST /api/support stays as it was for the MCP-era callers (the dock's "Talk to a person" is gone, H4); POST
/api/feedback records a vote on a plan version (D137, D145). All three answer 404 while AGENT_SUPPORT is off, and all
count toward the visitor key's per-minute and per-day limits like the homepage's other calls (429, as /api/turn)."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from . import (
    audit,
    auth,
    business,
    config,
    db,
    feedback,
    home_api,
    mail_grants,
    plans,
    sessions,
    support,
    support_mail,
    transcript,
)

log = logging.getLogger(__name__)
router = APIRouter()
NO_STORE = home_api.NO_STORE
MAX_TEXT = 2000
SIGN_IN_FIRST = "Sign in with Google to contact a person."
NOT_FOUND = {"ok": False, "error": "not found"}
PLAN_ID = re.compile(r"pl_[0-9a-f]{16}")  # db.new_id("pl"), as pages.PLAN_ID
# The Help button (spec 2026-10-12 §3.3): the page is a path, no query (the client sends location.pathname); the
# lookahead refuses a protocol-relative path ("//evil.com/x", review round 1 #2)
PAGE = re.compile(r"/(?!/)[A-Za-z0-9_./~-]{0,198}")
BROWSER_CHARS = 200  # the User-Agent header, cut (D17)
LAST_TURNS = 5  # the last turns of transcript.build on the report (§3.3)
# A typed address, on top of mail_grants.valid (review round 1 #3): plain ASCII, none of ,;<> or " -- mail_grants.valid
# alone lets markup through a local part (<svg/onload=x>@a.b passes its shape regex)
BAD_ADDRESS_CHARS = re.compile(r'[,;<>"]')
NEED_EMAIL = "Add your e-mail address, so a person can write back."
NOT_AN_ADDRESS = "That doesn't look like an e-mail address."


def _owned_business(conn, email: str) -> str | None:
    """The signed-in owner's most recent active business, for a visitor with no live homepage cookie."""
    row = conn.execute(
        "SELECT id FROM business WHERE owner_email = %s AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    return row["id"] if row else None


def _over_limit(conn, rec) -> JSONResponse | None:
    """The visitor key's per-key limits, checked before anything is read or written: the homepage's 429, else None."""
    if rec is None:
        return None
    try:
        auth.rate_check(conn, rec)
    except auth.RateLimited as e:
        return home_api._too_many(home_api._retry_after(str(e)))
    return None


@router.post("/api/support")
def api_support(request: Request, body: dict = Body(...)):
    """The "Talk to a person" link (§4.2): reason human, no model call. The visitor's business (the homepage cookie, as
    /api/turn reads it) or, without one, the signed-in owner's latest business; a visitor with neither is asked to
    sign in. A web session, when there is one, must carry its CSRF token."""
    settings = config.load()
    if not settings.support:
        return JSONResponse(NOT_FOUND, status_code=404, headers=NO_STORE)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return JSONResponse(home_api.BAD_REQUEST, status_code=400, headers=NO_STORE)
    text, ip = text.strip(), sessions.request_ip(request)
    planned = False
    with db.connect(settings.database_url) as conn:
        rec, biz = home_api._visitor(conn, request)
        if (limited := _over_limit(conn, rec)) is not None:
            return limited
        web = sessions.of_request(conn, request)
        if web is not None:
            sessions.check_csrf(web, body.get("csrf"))
        if rec is not None and not home_api._needs_sign_in(conn, request, biz):
            # a signed-in visitor's own address (Google verified it, D133) answers the ask, even on a draft nobody has
            # claimed yet; an anonymous draft waits for the claim
            business_id, actor, contact = biz["id"], f"key:{rec.id}", (web["email"] if web is not None else None)
            actor_class, key_id, draft = rec.cls, rec.id, rec.cls == "draft"
            # the page shows this consultation: after its plan, the answer also offers a change (§4.6), as /api/turn's
            session = home_api._current_session(conn, biz["id"])
            planned = session is not None and session["status"] == "planned"
        elif web is not None:
            business_id, actor, contact = _owned_business(conn, web["email"]), f"web:{web['email']}", web["email"]
            actor_class, key_id, draft = "business", None, False
        else:
            return JSONResponse({"ok": False, "signin": True, "error": SIGN_IN_FIRST}, headers=NO_STORE)
        try:
            out = support.open_or_append(
                conn,
                business_id=business_id,
                source="home",
                reason="human",
                words=text,
                summary=text,
                actor=actor,
                contact_email=contact,
                ip=ip,
                draft=draft,
            )
        except support.Limited as e:
            return JSONResponse({"ok": False, "error": str(e)}, headers=NO_STORE)
        audit.record(
            conn,
            tool="web.support",
            args={"chars": len(text)},
            result=support.public(out),
            ok=True,
            business_id=business_id,
            actor=actor,
            actor_class=actor_class,
            api_key_id=key_id,
            ip=ip,
        )
    support_mail.flush(out["ticket_id"])
    return JSONResponse({"ok": True, "result": support.envelope_for(out, planned=planned)}, headers=NO_STORE)


def _report_fields(body: dict) -> dict | None:
    """/api/help's fields, checked: the words (1 to MAX_TEXT characters), the page (a path, no query), an optional plan
    id of the right shape and an optional address (lower-cased; its shape is checked by the route, which knows whether
    one is needed); None when anything is off (the route answers 400)."""
    text, page, plan_id, email = body.get("text"), body.get("page"), body.get("plan_id"), body.get("email")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return None
    if not isinstance(page, str) or not PAGE.fullmatch(page):
        return None
    if plan_id is not None and not (isinstance(plan_id, str) and PLAN_ID.fullmatch(plan_id)):
        return None
    if email is not None and not isinstance(email, str):
        return None
    return {"text": text.strip(), "page": page, "plan_id": plan_id, "email": (email or "").strip().lower() or None}


def _domain(address: str) -> str:
    """The address's domain alone (review round 1 #5): a log line never carries the full address, so a log can't
    become a second place that reveals who asked for help."""
    return address.rsplit("@", 1)[-1] if "@" in address else "(no @)"


def _report(conn, fields: dict, *, business_id: str | None, signed_in: bool, browser: str, typed: str | None) -> dict:
    """What Admiry adds to a report by itself (spec 2026-10-12 §3.3, plan D3/D4): the page, whether the visitor was
    signed in, the browser, the typed address when there was one, the plan the page shows -- the named one when it is
    the business's own, else the latest on the homepage -- with its version and status, and the last LAST_TURNS turns
    of the business's conversation (transcript.build never raises: a failed build has no turns)."""
    report = {"page": fields["page"], "signed_in": signed_in, "browser": browser, "plan": None, "turns": []}
    if typed:
        report["email"] = typed
    if business_id:
        plan = plans.get(conn, business_id, fields["plan_id"]) if fields["plan_id"] else None
        if plan is None and fields["page"] == "/":
            plan = plans.latest(conn, business_id)
        if plan:
            report["plan"] = {"id": plan["id"], "version": plan["version"], "status": plan["status"]}
        report["turns"] = (transcript.build(conn, business_id).get("turns") or [])[-LAST_TURNS:]
    return report


@router.post("/api/help")
def api_help(request: Request, body: dict = Body(...)):
    """The Help button (spec 2026-10-12 §3.3): a report, with the context Admiry adds, as an SP1 ticket (reason help,
    source home) through support.open_or_append. Who is writing, as /api/support reads the page (plan D6): the page's
    business when the cookie's business is a draft nobody owns or the signed-in owner's own (contact = the session's
    address, verified, else the typed one, unverified); else the signed-in owner's latest business, or a business-less
    ticket keyed by their address; else a business-less ticket keyed by the typed address. A web session must carry its
    CSRF token. The page learns only "sent" and the address the reply goes to (D8). The per-address day limit drops a
    report silently (D5); SP1's per-IP limit answers as /api/support does."""
    settings = config.load()
    if not settings.support:
        return JSONResponse(NOT_FOUND, status_code=404, headers=NO_STORE)
    fields = _report_fields(body)
    if fields is None:
        return JSONResponse(home_api.BAD_REQUEST, status_code=400, headers=NO_STORE)
    ip = sessions.request_ip(request)
    browser = (request.headers.get("user-agent") or "")[:BROWSER_CHARS]
    with db.connect(settings.database_url) as conn:
        rec, biz = home_api._visitor(conn, request)
        if (limited := _over_limit(conn, rec)) is not None:
            return limited
        web = sessions.of_request(conn, request)
        if web is not None:
            sessions.check_csrf(web, body.get("csrf"))
        typed = fields["email"]
        if web is None:
            if typed is None:
                return JSONResponse({"ok": False, "error": NEED_EMAIL}, status_code=400, headers=NO_STORE)
            if not typed.isascii() or BAD_ADDRESS_CHARS.search(typed) or not mail_grants.valid(typed):
                return JSONResponse({"ok": False, "error": NOT_AN_ADDRESS}, status_code=400, headers=NO_STORE)
        contact, verified = (web["email"], True) if web is not None else (typed, False)
        if rec is not None and not home_api._needs_sign_in(conn, request, biz):
            business_id, actor = biz["id"], f"key:{rec.id}"
            actor_class, key_id, draft = rec.cls, rec.id, rec.cls == "draft"
        elif web is not None:
            business_id, actor = _owned_business(conn, web["email"]), f"web:{web['email']}"
            actor_class, key_id, draft = "business", None, False
        else:
            business_id, actor, actor_class, key_id, draft = None, f"visitor:{typed}", "anonymous", None, False
        if support.reports_today(conn, contact, verified=verified) >= support.REPORTS_PER_ADDRESS_DAY:
            log.warning(
                "help report dropped: %s reports today for an address at %s",
                support.REPORTS_PER_ADDRESS_DAY,
                _domain(contact),
            )
            return JSONResponse({"ok": True, "sent_to": contact}, headers=NO_STORE)
        report = _report(
            conn,
            fields,
            business_id=business_id,
            signed_in=web is not None,
            browser=browser,
            typed=None if web is not None else typed,
        )
        try:
            out = support.open_or_append(
                conn,
                business_id=business_id,
                source="home",
                reason="help",
                words=fields["text"],
                summary=fields["text"],
                actor=actor,
                contact_email=contact,
                contact_verified=verified,
                report=report,
                ip=ip,
                draft=draft,
            )
        except support.Limited as e:
            if web is None:
                # review round 1 #1: the per-IP Limited answer must say no more than a per-address drop does, else an
                # IP over its limit tells a caller whether a typed address already has an open ticket
                log.warning("help report dropped: IP limit reached for an address at %s", _domain(typed))
                return JSONResponse({"ok": True, "sent_to": typed}, headers=NO_STORE)
            return JSONResponse({"ok": False, "error": str(e)}, headers=NO_STORE)
        audit.record(
            conn,
            tool="web.help",
            args={"chars": len(fields["text"]), "page": fields["page"]},
            result=support.public(out),
            ok=True,
            business_id=business_id,
            actor=actor,
            actor_class=actor_class,
            api_key_id=key_id,
            ip=ip,
        )
    support_mail.flush(out["ticket_id"])
    # review round 1 #4: the typed path always names the address that was typed, never a verified owner's address an
    # unrelated typed contact happened to land a business-less or business-bound ticket next to
    sent_to = typed if web is None else out["contact_email"]
    return JSONResponse({"ok": True, "sent_to": sent_to}, headers=NO_STORE)


def _vote_fields(body: dict) -> dict | None:
    """/api/feedback's fields, checked; None when anything is off (the route answers 400)."""
    plan_id, surface, rating = body.get("plan_id"), body.get("surface"), body.get("rating")
    reasons, comment, contact = body.get("reasons") or [], body.get("comment"), body.get("contact_me", False)
    if not (isinstance(plan_id, str) and PLAN_ID.fullmatch(plan_id)):
        return None
    if surface not in feedback.SURFACES or rating not in feedback.RATINGS or not isinstance(contact, bool):
        return None
    if not isinstance(reasons, list) or not all(isinstance(r, str) and r in feedback.REASONS for r in reasons):
        return None
    if comment is not None and (not isinstance(comment, str) or len(comment) > feedback.MAX_COMMENT):
        return None
    return {
        "plan_id": plan_id,
        "surface": surface,
        "rating": rating,
        "reasons": list(dict.fromkeys(reasons)),
        "comment": (comment or "").replace("\x00", "").strip() or None,  # Postgres refuses a NUL in text
        "contact_me": contact,
    }


@router.post("/api/feedback")
def api_feedback(request: Request, body: dict = Body(...)):
    """👍/👎 on a plan version (§6): the signed-in owner of the plan's business, with the session's CSRF token; the vote
    with its snapshot, one row per person and version; 👎 with contact_me also opens (or appends to) the business's
    ticket (D145)."""
    settings = config.load()
    if not settings.support:
        return JSONResponse(NOT_FOUND, status_code=404, headers=NO_STORE)
    fields = _vote_fields(body)
    if fields is None:
        return JSONResponse(home_api.BAD_REQUEST, status_code=400, headers=NO_STORE)
    ip = sessions.request_ip(request)
    with db.connect(settings.database_url) as conn:
        rec, _ = home_api._visitor(conn, request)
        if (limited := _over_limit(conn, rec)) is not None:
            return limited
        web = sessions.of_request(conn, request)
        if web is None:
            return JSONResponse(
                {"ok": False, "signin": True, "error": "Sign in first."}, status_code=401, headers=NO_STORE
            )
        sessions.check_csrf(web, body.get("csrf"))
        plan = plans.by_id(conn, fields["plan_id"])
        if plan is None:
            return JSONResponse(NOT_FOUND, status_code=404, headers=NO_STORE)
        owner = ((business.row(conn, plan["business_id"]) or {}).get("owner_email") or "").lower()
        if owner != web["email"].lower():
            return JSONResponse(
                {"ok": False, "error": "This plan belongs to another Google account."},
                status_code=403,
                headers=NO_STORE,
            )
        actor, ticket_id = f"web:{web['email']}", None
        with conn.transaction():
            feedback_id = feedback.record(
                conn,
                plan=plan,
                surface=fields["surface"],
                rating=fields["rating"],
                reasons=fields["reasons"],
                comment=fields["comment"],
                actor=actor,
            )
            if fields["contact_me"] and fields["rating"] == "down":
                words = feedback.words(fields["reasons"], fields["comment"])
                ticket_id = support.open_or_append(
                    conn,
                    business_id=plan["business_id"],
                    source="feedback",
                    reason="dissatisfied",
                    words=words,
                    summary=f"Plan v{plan['version']} was not useful: {words}",
                    actor=actor,
                )["ticket_id"]
                feedback.link_ticket(conn, feedback_id, ticket_id)
            audit.record_web(
                conn,
                tool="web.feedback",
                email=web["email"],
                ip=ip,
                business_id=plan["business_id"],
                args={"plan_id": plan["id"], "rating": fields["rating"], "reasons": fields["reasons"]},
                result={"feedback_id": feedback_id, "ticket_id": ticket_id},
            )
    if ticket_id:
        support_mail.flush(ticket_id)
    return JSONResponse({"ok": True, "feedback_id": feedback_id, "ticket_id": ticket_id}, headers=NO_STORE)
