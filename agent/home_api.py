"""The homepage's client of the tools (spec 2026-09-26 §3, D32, D39): one JSON action per call, run in
process against the same registered tools under a request-header override. The visitor is a draft key in an
HttpOnly cookie; the wall (D39) is a page state decided here, never a tool refusal."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response
from fastmcp.exceptions import ToolError

from . import (
    ads_refusal,
    ads_run,
    audit,
    auth,
    blockers,
    bundles,
    business,
    claims,
    config,
    connect,
    consult,
    db,
    envelope,
    feedback,
    hosted,
    hosting_minimum,
    keys,
    landing,
    launch_check,
    launch_review,
    listings,
    llm,
    page_builder,
    plans,
    profile,
    proposals,
    sessions,
    setup,
    site_install,
    state,
    support,
    support_mail,
    tools_hosting,
    tools_sites,
    tools_start,
    transcript,
    uploads,
)
from .harness import turn as harness_turn
from .mcp_app import REGISTRY
from .models import PageCopy
from .sites import build as site_build
from .sites import intake
from .util import iso

router = APIRouter()
VISIT_COOKIE = "admiry_visit"
BIZ_COOKIE = "admiry_visit_biz"  # Path=/ (Task 3): sha256(draft key), so /oauth/consent can find the draft
DRAFT_MAX_AGE = int(timedelta(hours=24).total_seconds())
CLAIMED_MAX_AGE = int(timedelta(days=30).total_seconds())
HANDOFF_LIMIT_PER_HOUR = 5
NO_STORE = {"Cache-Control": "no-store"}
WALL = {"ok": False, "wall": True, "error": "Sign in to see your plan."}
FRESH = {"ok": False, "error": "Start by telling me what you want.", "fresh": True}
# I1 (final review): a claimed business's cookie is a lasting business key; it opens nothing without that
# owner's web session, so a sign-out (or another account on the same browser) cannot leave the plan open.
SIGNIN = {"ok": False, "error": "Sign in to continue.", "signin": True}
BAD_REQUEST = {"ok": False, "error": "bad request"}
STR_FIELDS = (
    "text",
    "website_url",
    "idempotency_key",
    "level",
    "offer",
    "objective",
    "channel",
    "business_id",
    "question_id",  # the question card the page shows, with typed words (K6 slice 1 C1)
)
NUM_FIELDS = ("monthly_budget_usd", "radius_miles", "channel_monthly_usd")
ACTIONS = ("start", "answer", "recommend", "adjust", "say", "state", "pick")
ADJUST_PARAMS = ("monthly_budget_usd", "level", "radius_miles", "offer", "objective", "channel", "channel_monthly_usd")
# Task 4: free text typed after the plan. Longer than this never reaches the model — NOT_AN_ADJUSTMENT_SAY
# answers directly, so a long ramble that is plainly not "change the budget" costs nothing.
SAY_ADJUST_MAX_CHARS = 400
SUPPORT_MAX_CHARS = 2000  # what read_support reads of a long message (the composer's maxlength)
# What the home can change from words after the plan (B0 P7: said alone, with the open question again below it, while
# something stops the launch -- never "continue in your assistant" then)
NOT_AN_ADJUSTMENT_HERE = "I can change the budget, the area, the offer or the goal here."
NOT_AN_ADJUSTMENT_SAY = NOT_AN_ADJUSTMENT_HERE + " For anything else, continue in your assistant."
READY_SAY = "Say the word and I will build the plan."
# A signed-in owner whose browser carries no visitor cookie (a sign-out deleted it, another browser, a business an
# assistant built): `state` offers their businesses to pick from (`choose`) instead of a bare fresh start, and `pick`
# attaches one -- a `web` business key in the two visitor cookies, as the wall's claim leaves them (founder's dev walk,
# 2026-10-07: the first words ran an anonymous `start` and made an "unknown business"; ruling: ask first, list them).
PICK_LIMIT_PER_HOUR = 10  # `pick` mints a key each time; per owner e-mail
WEB_KEY_HOURS = 30 * 24  # a picked key dies with its cookie (CLAIMED_MAX_AGE)
NOT_YOURS = "That business is not yours."
# follow-up R1: a page names the business it shows on every request (static/home.js `post`); a write from a page that
# shows another business than the visitor cookie's -- a tab left open while another tab switched it (a setup link's
# pick, the picker) -- is refused before anything is written, and the page offers a reload
PAGE_HEADER = "X-Admiry-Business"
OTHER_PAGE = "This page is showing another business. Reload it to continue."
BLANK_NAME = "A business without a name yet"
# Anonymous words that name something the visitor already has here: no business is made from them; the answer is to
# sign in (or, signed in, the businesses to pick from). "plan" alone is not "my plan": "I plan to open…" starts one.
SIGN_IN_FOR_PLAN = "Sign in and I'll show you your plan."
SIGN_IN_FOR_PLAN_RESPONSE = {"ok": False, "signin": True, "error": SIGN_IN_FOR_PLAN}
EXISTING_WORDS = re.compile(
    r"\bmy\s+(?:existing\s+|old\s+|previous\s+|current\s+)?(?:proposals?|plans?|campaigns?|ads|account)\b"
    r"|\b(?:sign|log)\s*in\b",
    re.IGNORECASE,
)


class Limited(Exception):
    """The tool raised auth.RateLimited; the route answers 429 with this many seconds to wait."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


def cookie_args(settings, *, max_age: int) -> dict:
    """set_cookie keyword arguments for the visitor cookie: HttpOnly, SameSite=Lax, Secure on https,
    scoped to /api (the homepage's own endpoints only)."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.web_base_url.startswith("https://"),
        "path": "/api",
        "max_age": max_age,
    }


def biz_cookie_args(settings, *, max_age: int) -> dict:
    """set_cookie keyword arguments for the second visitor cookie (BIZ_COOKIE, Task 3): the same lifetime
    as the main visitor cookie, but Path=/ so /oauth/consent can read it too (the main cookie is Path=/api,
    unreachable there). Holds sha256(the draft key's plaintext) — never the business id or the key itself
    — so the consent page can look the business up only while that exact key is still a live draft
    (keys.hash_key; see oauth_web._draft_key_business), the same possession proof the main cookie is.
    Not a secret on its own (a hash cannot be turned back into the key), but still HttpOnly (never read by
    page JS) and SameSite=Lax like the main cookie."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.web_base_url.startswith("https://"),
        "path": "/",
        "max_age": max_age,
    }


def _tool(name: str):
    return next(m.tool for m in REGISTRY if m.name == name)


def call(name: str, args: dict, key: str, *, ip: str) -> dict:
    """The tool's envelope, or {"error": sentence}; auth.RateLimited becomes Limited (never a key,
    code, token or claim link: the caller pops those out of a tool's raw envelope before this)."""
    return _call_fn(_tool(name).fn, args, key, ip=ip)


def _call_fn(fn, args: dict, key: str, *, ip: str) -> dict:
    """`call` for a command's function by itself -- a registered tool's, or one the registry does not publish
    (tools_sites.fix_headline) -- under the visitor's key."""
    with auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": ip}):
        try:
            return fn(**args)
        except auth.RateLimited as e:
            raise Limited(_retry_after(str(e))) from e
        except (ToolError, auth.Denied) as e:
            return {"error": str(e)}


def _retry_after(text: str) -> int:
    marker = "retry_after="
    return int(text.split(marker, 1)[1].split()[0]) if marker in text else 60


def _visitor(conn, request: Request) -> tuple[keys.KeyRecord | None, dict | None]:
    """(the visitor's live key record, its business row) from the cookie; (None, None) when absent or dead."""
    plaintext = request.cookies.get(VISIT_COOKIE)
    rec = keys.lookup(conn, plaintext) if plaintext else None
    if rec is None or rec.revoked_at is not None or rec.is_expired() or rec.business_id is None:
        return None, None
    biz = business.row(conn, rec.business_id)
    if biz is None or biz["status"] != "active":
        return None, None
    return rec, biz


def _needs_sign_in(conn, request: Request, biz: dict) -> bool:
    """True when the visitor's business has an owner (claimed or verified — or a released claim, which keeps
    its owner e-mail and its upgraded key) and no web session of that same e-mail is present. A draft
    nobody owns yet keeps working on the cookie alone, as before."""
    owner = (biz["owner_email"] or "").strip().lower()
    if biz["claim_state"] == "draft" and not owner:
        return False
    web = sessions.of_request(conn, request)
    return web is None or (web["email"] or "").strip().lower() != owner


def _fresh_reason(conn, request: Request) -> str | None:
    """Why a visitor with a cookie starts over (I3): "moved" when its key was revoked by a hand-off landing
    (consult.move_session re-points the session to the owner's business, then retires the draft and revokes its keys),
    else "expired" -- a draft the harness's start over retired too, which still holds its consultation (K6 final review
    M1). None without a cookie at all: a first visit has nothing to explain."""
    plaintext = request.cookies.get(VISIT_COOKIE)
    if not plaintext:
        return None
    rec = keys.lookup(conn, plaintext)
    if rec is not None and rec.revoked_at is not None and rec.business_id is not None:
        biz = business.row(conn, rec.business_id)
        if biz is not None and biz["status"] == "expired" and not _holds_session(conn, biz["id"]):
            return "moved"
    return "expired"


def _holds_session(conn, business_id: str) -> bool:
    """A consultation not superseded: a draft a start over retired keeps its open or ready one (Ruling 19)."""
    row = conn.execute(
        "SELECT 1 FROM consult_session WHERE business_id = %s AND status <> 'superseded' LIMIT 1", (business_id,)
    ).fetchone()
    return row is not None


def _well_typed(body: dict) -> bool:
    """m1 (final review): the fields /api/turn passes on have the types the tools take (None = absent)."""
    if any(body.get(k) is not None and not isinstance(body[k], str) for k in STR_FIELDS):
        return False
    if body.get("answers") is not None and not isinstance(body["answers"], dict):
        return False
    return not any(
        body.get(k) is not None and (isinstance(body[k], bool) or not isinstance(body[k], int | float))
        for k in NUM_FIELDS
    )


def _wall(conn, biz: dict, session: dict | None, settings) -> bool:
    """True when the business is unclaimed and either the consultation is ready to plan, or
    AGENT_ANON_TURNS is set and the owner has already answered that many questions."""
    if biz["claim_state"] != "draft":
        return False
    if session is None:
        return False
    if session["status"] in ("ready", "planned"):
        return session["status"] == "ready"
    if settings.anon_turns > 0:
        answered = sum(
            1 for k, v in session["answers"].items() if not k.startswith("_") and v.get("source") != "known_fact"
        )
        return answered >= settings.anon_turns
    return False


def _visitor_block(conn, request: Request, biz: dict, session: dict | None, settings) -> dict:
    web = sessions.of_request(conn, request)
    return {
        "business_id": biz["id"],
        "signed_in": web is not None,
        "claimed": biz["claim_state"] != "draft",
        "session_id": session["id"] if session else None,
        "wall": _wall(conn, biz, session, settings),
    }


def _other_page(request: Request, biz: dict | None) -> JSONResponse | None:
    """The answer for a request from a page showing another business than `biz` (the visitor cookie's), or None -- a
    request that names none (an older page, another script) is not checked (follow-up R1)."""
    shown = request.headers.get(PAGE_HEADER)
    if not shown or biz is None or shown == biz["id"]:
        return None
    return JSONResponse({"ok": False, "error": OTHER_PAGE, "reload": True}, headers=NO_STORE)


def _current_session(conn, business_id: str) -> dict | None:
    """The consultation a reload should resume: open/ready first, else the latest planned one."""
    s = consult.open_for(conn, business_id)
    if s:
        return s
    r = conn.execute(
        "SELECT id FROM consult_session WHERE business_id = %s AND status = 'planned' ORDER BY created_at DESC LIMIT 1",
        (business_id,),
    ).fetchone()
    return consult.get(conn, business_id, r["id"]) if r else None


def _owned(conn, email: str) -> list[dict]:
    """The owner's active businesses as the picker lists them: id, a name (plans.name_for, else the listing's address,
    else the stored name, else BLANK_NAME) and `state` -- "plan" (a plan was built), "questions" (a consultation still
    open or ready), "none" (no consultation yet). The ones with a consultation first, newest consultation first (the
    one a reload would resume: consult.handoff_for's order), the blank ones after them."""
    rows = conn.execute(
        "SELECT b.id, s.status FROM business b LEFT JOIN LATERAL ("
        "  SELECT status, created_at FROM consult_session WHERE business_id = b.id"
        "  AND status IN ('open', 'ready', 'planned') ORDER BY (status = 'planned'), created_at DESC LIMIT 1"
        ") s ON true WHERE b.owner_email = %s AND b.status = 'active' "
        "ORDER BY s.created_at DESC NULLS LAST, b.created_at DESC",
        (email,),
    ).fetchall()
    out = []
    for r in rows:
        knowledge = (business.latest_knowledge(conn, r["id"]) or (None, {}))[1]
        name = (
            plans.name_for(knowledge)
            or plans.address_for(knowledge)
            or profile.clean_name((knowledge or {}).get("name"))
            or BLANK_NAME
        )
        state_ = "plan" if r["status"] == "planned" else "questions" if r["status"] else "none"
        out.append({"id": r["id"], "name": name, "state": state_})
    return out


def _choose(conn, request: Request) -> list[dict] | None:
    """The businesses a signed-in visitor without a live visitor cookie may pick from; None without a web session or
    when the e-mail owns none (then the visit is simply fresh)."""
    web = sessions.of_request(conn, request)
    return (_owned(conn, web["email"]) or None) if web is not None else None


def _picks_last_hour(conn, email: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.pick' AND actor = %s AND ts > now() - interval '1 hour'",
        (f"web:{email}",),
    ).fetchone()["n"]


def _pick(conn, request: Request, body: dict, settings) -> JSONResponse:
    """`pick`: the signed-in owner chose one of their businesses; a `web` business key for it goes into the two
    visitor cookies (the lasting ones the wall's claim leaves, CLAIMED_MAX_AGE), so the next `state` resumes it. Needs
    the web session and its csrf; a business not theirs (or not active) is refused without saying whether it exists."""
    web = sessions.of_request(conn, request)
    if web is None:
        return JSONResponse(FRESH, headers=NO_STORE)
    sessions.check_csrf(web, body.get("csrf"))
    row = conn.execute(
        "SELECT id FROM business WHERE id = %s AND owner_email = %s AND status = 'active'",
        (body.get("business_id"), web["email"]),
    ).fetchone()
    if row is None:
        return JSONResponse({"ok": False, "error": NOT_YOURS}, headers=NO_STORE)
    if _picks_last_hour(conn, web["email"]) >= PICK_LIMIT_PER_HOUR:
        return _too_many(600)
    with conn.transaction():
        plaintext, rec = keys.mint(
            conn, business_id=row["id"], cls="business", scopes=("read", "manage"), label="web", ttl_hours=WEB_KEY_HOURS
        )
        audit.record_web(
            conn,
            tool="web.pick",
            email=web["email"],
            ip=sessions.request_ip(request),
            business_id=row["id"],
            args={"business_id": row["id"]},
            result={"key_id": rec.id},
        )
    biz = business.row(conn, row["id"])
    session = _current_session(conn, biz["id"])
    resp = JSONResponse(
        {"ok": True, "visitor": _visitor_block(conn, request, biz, session, settings)}, headers=NO_STORE
    )
    resp.set_cookie(VISIT_COOKIE, plaintext, **cookie_args(settings, max_age=CLAIMED_MAX_AGE))
    resp.set_cookie(BIZ_COOKIE, keys.hash_key(plaintext), **biz_cookie_args(settings, max_age=CLAIMED_MAX_AGE))
    return resp


def _args(action: str, body: dict, session: dict | None) -> dict:
    ik = body.get("idempotency_key") or db.new_id("ik")
    if action == "start":
        return {
            "text": body.get("text") or "",
            "website_url": body.get("website_url"),
            "locale": "en-US",
            "known_facts": None,
        }
    if action == "answer":
        return {
            "session_id": session["id"] if session else "",
            "answers": body.get("answers") or {},
            "idempotency_key": ik,
        }
    if action == "recommend":
        return {"session_id": session["id"] if session else "", "idempotency_key": ik}
    if action == "adjust":
        return {
            "plan_id": session["plan_id"] if session else "",
            "idempotency_key": ik,
            **{k: body.get(k) for k in ADJUST_PARAMS if body.get(k) is not None},
        }
    # reached through `_unlink` and the harness's not-mine reroute: the listing the owner confirmed, unlinked
    if action == "update_business":
        return {"patch": {"google_listing": None}, "idempotency_key": ik}
    raise AssertionError(action)


def listing_linked(conn, business_id: str) -> bool:
    """Whether the business has a Google Maps listing its owner confirmed (profile.business_listing): only then do
    words that call it not theirs unlink it (plan 2026-10-03-agent-listing-not-mine-words, D2)."""
    return profile.business_listing((business.latest_knowledge(conn, business_id) or (None, {}))[1]) is not None


def _unlink(idempotency_key: str | None, *, named: bool) -> tuple[str, dict]:
    """The reroute for words that say the Google Maps place linked is not the owner's business (plan
    2026-10-03-agent-listing-not-mine-words, D4, D5): the `update_business` command that unlinks it, as Settings' "Not
    my business" and an assistant's `google_listing: null` do -- its row is the one the activity log reads -- under the
    turn's own idempotency key, and, in `then`, what the reply adds to the command's say: how to link the right place,
    and that a change the same words named was not made. A body of its own, never the request's: nothing a page sends
    reaches the command or its say."""
    then = listings.LINK_AGAIN + (listings.NOTHING_ELSE if named else "")
    return "update_business", {"idempotency_key": idempotency_key, "then": then}


def _unlinked(env: dict, then: str, session: dict | None) -> dict:
    """`_unlink`'s reroute answered as the owner reads it: the command's say -- or, when another path unlinked the
    listing meanwhile, the sentence that nothing is linked (the caller's envelope: the command refused and changed
    nothing, as change_plan answers the same race) -- then `then`. A consultation not yet planned keeps its next step
    (Task 2 round 1): the question it asks again, after the sentences as `answer` asks it, else Build my plan."""
    say, nxt = env["say"] + then, env["next"]
    if session is not None and session["status"] in ("open", "ready"):
        from . import tools_consult  # local, as the `state` action's: mcp_app orders the tool modules' imports

        question = consult.next_question(session)
        say += f" {consult.question_sentence(question)}" if question else ""
        nxt = question or tools_consult.RECOMMEND_ACTION
    return {**env, "say": say, "next": nxt}


def _funded(conn, settings, business_id: str) -> bool:
    """L1 (spec 2026-10-10 §5): whether the money is in (or the owner's own account is live) while the latest plan is a
    draft -- the setup card done, the plan waiting for the owner's yes -- so the home's launch card offers the yes
    (tools_consult.confirm_action) instead of a way to run the ads."""
    st = setup.state(conn, settings, business_id)
    return st["step"] == "done" and st["plan_status"] == plans.DRAFT


def _funded_next(conn, settings, business_id: str, env: dict) -> dict:
    """A plan answer's `next` as the home draws it for a funded business (L1): the build's and a change's answers come
    from the shared commands, whose actions offer prepaying or linking the owner's own account; once the account step
    is done and the plan is a draft, those ways become the owner's yes, as a reload's `state` offers it
    (launch_next(funded=True)). `first` (L1 review M2, D6) tells `confirm_action` whether this would be the
    business's first launch (`ads_run.approved` is None): a change to an already-running plan approves and launches
    at once whatever AGENT_LAUNCH_REVIEW says, so its yes stays "Confirm and launch"."""
    from . import tools_consult  # local, as the `state` action's: mcp_app orders the tool modules' imports

    nxt = env.get("next") or {}
    ways = ("prepay", "link_own_account")
    if nxt.get("kind") != "actions" or not any(a["id"] in ways for a in nxt["actions"]):
        return env
    if not _funded(conn, settings, business_id):
        return env
    confirm = tools_consult.confirm_action(settings, first=ads_run.approved(conn, business_id) is None)
    actions: list[dict] = []
    for a in nxt["actions"]:  # the ways to run the ads become the one yes, where the first of them stood
        if a["id"] not in ways:
            actions.append(a)
        elif not any(x["id"] == "confirm" for x in actions):  # dedupe by id (L1 review): never the whole dict
            actions.append(confirm)
    return {**env, "next": envelope.actions(*actions)}


def _planned_cards(conn, biz: dict) -> tuple[dict | None, dict | None]:
    """The latest plan and its cards (plans.cards) for a planned session, or (None, None) when there is
    no plan yet from a consultation (mirrors the `state` action's own lookup)."""
    latest = plans.latest(conn, biz["id"])
    if latest is None or "consultation" not in latest["sections"]:
        return None, None
    knowledge = (business.latest_knowledge(conn, biz["id"]) or (None, {}))[1]
    latest["business_name"] = plans.name_for(knowledge) or plans.address_for(knowledge)
    return latest, plans.cards(latest, final_host=landing.final_host(conn, biz["id"]))


def _record_say(conn, principal: auth.Principal, text: str, result, *, ok: bool) -> None:
    """One `web.say` row for a model read outside a tool (Task 4): it lets auth.check_llm_cap count the call toward the
    business's daily limit (its own explicit clause), and it keeps the owner's words whole in `result.text` (spec
    2026-09-30 D147: the audit cuts arguments to 80 characters; transcript.py reads the words from here)."""
    audit.record(
        conn,
        tool="web.say",
        args={"text": text},
        result={**result, "text": text[: transcript.MAX_TEXT]},
        ok=ok,
        business_id=principal.business_id,
        actor=principal.actor,
        actor_class=principal.key.cls,
        api_key_id=principal.key.id,
        ip=principal.ip,
    )


def _support_env(conn, principal: auth.Principal, text: str, read, *, planned: bool, ip: str) -> dict:
    """A homepage turn that asks for a person, for help, or is unhappy (spec 2026-09-30 §4.2): the business's ticket
    opened or appended, answered with the acknowledgement; {"error": sentence} at the anonymous limit. It records the
    turn's web.say row (the read, whatever became of the ticket) with the id of the ticket that took the words, when
    one did: transcript leaves those words to the ticket's own turn, and keeps a turn whose ask opened none."""
    out = None
    try:
        out = support.open_or_append(
            conn,
            business_id=principal.business_id,
            source="home",
            reason=read.support,
            words=text,
            summary=read.support_summary or text,
            actor=principal.actor,
            ip=ip,
            draft=principal.key.cls == "draft",
        )
    except support.Limited as e:
        return {"error": str(e)}
    finally:
        took = {"ticket_id": out["ticket_id"]} if out else {}
        _record_say(conn, principal, text, {**read.model_dump(), **took}, ok=True)
    return support.envelope_for(out, planned=planned)


def _say_support(
    conn, text: str, key: str, ip: str, *, linked: bool = False, idempotency_key: str | None = None
) -> dict | tuple[str, dict] | None:
    """A turn on a ready consultation, or with none (no model call today, spec 2026-09-30 D131): read_support first,
    under the cap and recorded as a web.say row. With AGENT_SUPPORT on, the acknowledgement's envelope (or {"error": …})
    when the words ask for a person, for help, or are unhappy. With `linked` -- a ready consultation of a business with
    a listing its owner confirmed, read with support off too (plan 2026-10-03-agent-listing-not-mine-words, D4) -- the
    reroute that unlinks it (`_unlink`, under the turn's `idempotency_key`) when the words say the place is not theirs.
    None otherwise, or when the read cannot run (today's reply stands)."""
    if not text:
        return None
    settings = config.load()
    with auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": ip}):
        try:
            principal = auth.require(conn, "manage")
        except auth.RateLimited as e:
            raise Limited(_retry_after(str(e))) from e
        except auth.Denied:
            return None
        try:
            auth.check_llm_cap(conn, principal)
            read = llm.generator().read_support(text[:SUPPORT_MAX_CHARS])
        except (ToolError, llm.LLMError) as e:
            _record_say(conn, principal, text, {"error": str(e)}, ok=False)
            return None
    if settings.support and read.support != "none":
        return _support_env(conn, principal, text, read, planned=False, ip=ip)  # records the row, with its ticket
    _record_say(conn, principal, text, read.model_dump(), ok=True)
    if linked and read.listing_not_mine:
        return _unlink(idempotency_key, named=False)
    return None


def _say_adjust(conn, body: dict, biz: dict, key: str, ip: str) -> dict | tuple[str, dict]:
    """The `say` action on a planned session: read the owner's free text against the plan's cards and,
    when it names at least one of adjust's fields, reroute into the real `adjust` tool with them —
    returns ("adjust", body) for the caller to fall through the normal tool-call machinery. Otherwise
    returns a finished envelope (NOT_AN_ADJUSTMENT_SAY) or {"error": sentence} (auth or the model refused);
    a rate limit raises Limited, which the route answers 429 like its other paths.
    check_llm_cap is checked here, under the same headers `auth.require` would read from a real request
    (auth.with_headers, like the homepage's other tool calls), since this model call has no tool around it.
    With AGENT_SUPPORT on (spec 2026-09-30 §4.2) the same read says whether the words ask for a person, for help,
    or are unhappy: that takes the turn — a ticket, no adjustment — and a message longer than SAY_ADJUST_MAX_CHARS,
    which never reaches read_adjust, goes to read_support instead.
    Words the read says call the Google Maps place linked not theirs, for a business with a listing its owner confirmed
    (plan 2026-10-03-agent-listing-not-mine-words, D4): rerouted into `update_business`, which unlinks it (`_unlink`),
    in place of any adjustment the same words named; nothing linked, they are read as ever."""
    settings = config.load()
    text = (body.get("text") or "").strip()
    long = len(text) > SAY_ADJUST_MAX_CHARS
    if long and not settings.support:
        return envelope.make({}, say=NOT_AN_ADJUSTMENT_SAY)
    latest, cards = _planned_cards(conn, biz)
    if latest is None:
        return {"error": "no plan yet; build the plan first"}
    with auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": ip}):
        try:
            principal = auth.require(conn, "manage")
        except auth.RateLimited as e:
            raise Limited(_retry_after(str(e))) from e
        except auth.Denied as e:
            return {"error": str(e)}
        try:
            auth.check_llm_cap(conn, principal)
        except ToolError as e:
            _record_say(conn, principal, text, {"error": str(e)}, ok=False)
            return {"error": str(e)}
        try:
            gen = llm.generator()
            read = gen.read_support(text[:SUPPORT_MAX_CHARS]) if long else gen.read_adjust(text, cards)
        except llm.LLMError as e:
            _record_say(conn, principal, text, {"error": str(e)}, ok=False)
            return {"error": str(e)}
    if settings.support and read.support != "none":
        return _support_env(conn, principal, text, read, planned=True, ip=ip)  # records the row, with its ticket
    _record_say(conn, principal, text, read.model_dump(), ok=True)
    if read.listing_not_mine and listing_linked(conn, biz["id"]):  # the server decides (D2), as change_plan does
        named = not long and any(getattr(read, k) is not None for k in ADJUST_PARAMS)
        return _unlink(body.get("idempotency_key"), named=named)
    if long:
        return envelope.make({}, say=NOT_AN_ADJUSTMENT_SAY)
    fields = {k: v for k in ADJUST_PARAMS if (v := getattr(read, k)) is not None}
    if read.understood and fields:
        return "adjust", {**body, **fields}
    return envelope.make({}, say=NOT_AN_ADJUSTMENT_SAY)


def _state_result(conn, request: Request, settings, rec, biz: dict, session: dict | None) -> dict:
    """The `state` action's result -- a reload, and the state a fix answers with (/api/blocker): the consultation, the
    plan's cards, the "Live since …" line, the signed-in visitor's thumbs, the open question or the launch card's
    actions as the build answered them, and the latest plan's status (the plan card checks a draft's list itself:
    GET /api/launch_check) and, while it is in Admiry's review (L1), the owner's words for it
    (`launch_review.owner_words`)."""
    # M0 Task 6 (D102): the cards are MarketingState's -- the plan's, and the channels card once the ads run -- for a
    # planned session, as before; the "Live since …" line once a plan was approved (A3 Task 5)
    st = state.build(conn, settings, biz["id"]) if session and session["status"] == "planned" else None
    cards = st.cards() if st is not None and st.plan is not None else None
    # SP1 (spec 2026-09-30): the signed-in visitor's thumbs on this plan version, beside the cards (not a MarketingState
    # field); None with support off, without a web session, or before any vote
    vote = None
    if cards is not None and settings.support:
        web = sessions.of_request(conn, request)
        if web is not None:
            vote = feedback.vote_of(conn, st.plan_id, f"web:{web['email']}")
    # the latest plan's own words while it is in Admiry's review (L1): computed once, read both to gate the launch
    # card below and for the reply's own key
    in_review = launch_review.owner_words(conn, biz["id"]) if cards is not None else None
    # the open question, exactly as `answer` would present it, so a reload resumes it; for a plan, its actions and
    # launch_note as its build answered them (walk fixes 1), so a reload draws the same launch card -- none of
    # either while the plan is in review (R15 d): there is nothing to launch, and nothing else to call meanwhile
    nxt, launch_note = None, None
    if session and session["status"] == "open":
        nxt = consult.next_question(session)
    elif cards is not None and not in_review:
        from . import tools_consult  # local, as tools_start's own: mcp_app orders the tool modules' imports

        funded = rec.cls != "draft" and _funded(conn, settings, biz["id"])
        # the read is spent only where launch_next would read it (L1 review, the same reasoning as _apply's M6)
        first = ads_run.approved(conn, biz["id"]) is None if funded else True
        nxt, launch_note = tools_consult.launch_next(
            cards["plan"]["channels"],
            draft=rec.cls == "draft",
            website=cards["plan"]["setup"].get("website"),
            funded=funded,
            first=first,
        )
    return {
        "session": session,
        "cards": cards,
        "plan_id": st.plan_id if st is not None else None,
        "execution": st.execution if st is not None else None,
        "feedback": vote,
        "business_id": biz["id"],  # the channels card's Pause/Resume and Settings links
        "next": nxt,
        "launch_note": launch_note,
        "plan_status": (plans.latest(conn, biz["id"]) or {}).get("status") if cards is not None else None,
        "in_review": in_review,
        # spec 2026-10-11 §5.1: what Admiry removed from the latest version's ads after Google refused it -- the card's
        # "Changes Admiry made" line, in review and once live
        "changes": (
            [ads_refusal.line(c) for c in ads_refusal.changes(conn, biz["id"], st.plan_id)]
            if st is not None and st.plan_id
            else []
        ),
    }


@router.post("/api/turn")
def turn(request: Request, body: dict = Body(...)):
    settings = config.load()
    action = body.get("action")
    reading: dict = {}  # a typed answer's reading from the harness (K6 slice 1, D2); {} wraps `call` as a no-op
    if action not in ACTIONS:
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400, headers=NO_STORE)
    if not _well_typed(body):
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    ip = sessions.request_ip(request)
    with db.connect(settings.database_url) as conn:
        rec, biz = _visitor(conn, request)
        if action == "pick":  # a signed-in owner attaching one of their businesses: no visitor cookie needed
            return _pick(conn, request, body, settings)
        # "start" is anonymous only with no live cookie at all; any live key (draft or business)
        # runs the keyed `start` tool on that visitor's own business (it supersedes the open session
        # and writes a new knowledge version — draft_key stays None in its data either way).
        if action == "start" and rec is None:
            if EXISTING_WORDS.search(body.get("text") or ""):  # "continue my proposal": nothing to start from
                choose = _choose(conn, request)
                fresh = {"ok": True, "fresh": True, "choose": choose} if choose else SIGN_IN_FOR_PLAN_RESPONSE
                return JSONResponse(fresh, headers=NO_STORE)
            try:
                env = tools_start.begin_anonymous(body.get("text") or "", body.get("website_url"), "en-US", None, ip=ip)
            except auth.RateLimited as e:
                return JSONResponse(
                    {"ok": False, "error": str(e)},
                    status_code=429,
                    headers={**NO_STORE, "Retry-After": str(_retry_after(str(e)))},
                )
            except (ToolError, auth.Denied) as e:
                return JSONResponse({"ok": False, "error": str(e)}, headers=NO_STORE)
            draft_key = env["data"].pop("draft_key")
            env["data"].pop("connect_url", None)
            biz = business.row(conn, env["data"]["business_id"])
            session = consult.get(conn, biz["id"], env["data"]["session_id"])
            resp = JSONResponse(
                {"ok": True, "result": env, "visitor": _visitor_block(conn, request, biz, session, settings)},
                headers=NO_STORE,
            )
            resp.set_cookie(VISIT_COOKIE, draft_key, **cookie_args(settings, max_age=DRAFT_MAX_AGE))
            resp.set_cookie(BIZ_COOKIE, keys.hash_key(draft_key), **biz_cookie_args(settings, max_age=DRAFT_MAX_AGE))
            return resp
        if rec is None:
            fresh = {"ok": True, "fresh": True}
            if action == "state" and (choose := _choose(conn, request)):
                fresh["choose"] = choose  # a signed-in owner: their businesses to pick from, before any fresh start
            elif action == "state" and (reason := _fresh_reason(conn, request)):
                fresh["reason"] = reason  # the page says why it starts over (I3)
            resp = JSONResponse(fresh if action == "state" else FRESH, headers=NO_STORE)
            resp.delete_cookie(VISIT_COOKIE, path="/api")
            resp.delete_cookie(BIZ_COOKIE, path="/")
            return resp
        if _needs_sign_in(conn, request, biz):
            return JSONResponse({"ok": True, "signin": True} if action == "state" else SIGNIN, headers=NO_STORE)
        if action != "state" and (other := _other_page(request, biz)) is not None:
            return other  # a reload (`state`) is how such a page catches up
        draft = biz["claim_state"] == "draft"
        session = _current_session(conn, biz["id"])
        wall = _wall(conn, biz, session, settings)
        if action == "state":
            return JSONResponse(
                {
                    "ok": True,
                    "result": _state_result(conn, request, settings, rec, biz, session),
                    "visitor": _visitor_block(conn, request, biz, session, settings),
                },
                headers=NO_STORE,
            )
        if action == "say":
            # A free-text turn: an open question behaves exactly as `answer`; a planned session goes
            # through the structured adjust reader (_say_adjust); a ready one echoes recommend's own
            # wall/flow (nothing to read yet — no model call). Rerouting sets `action`/`body` to the
            # real tool call the caller falls through to below; anything else returns here directly.
            # With AGENT_SUPPORT on, a ready session (or none) first reads the words for a request for a person; a
            # ready one of a business with a listing its owner confirmed reads them with support off too, for whether
            # they call the place not theirs (plan 2026-10-03-agent-listing-not-mine-words, D4): that unlinks it.
            status = session["status"] if session else None
            if settings.harness and status in ("open", "ready"):
                # K6 slice 1 (spec 2026-10-10 §3): the harness reads the words -- the fast path, else one Turn call --
                # and says what to do; the wall, the cookies and the visitor block stay here (D3)
                try:
                    outcome = harness_turn.run(
                        conn,
                        settings,
                        rec,
                        biz,
                        session,
                        body.get("text") or "",
                        key=request.cookies[VISIT_COOKIE],
                        ip=ip,
                        wall=wall,
                        support_on=settings.support,
                        linked=listing_linked(conn, biz["id"]),
                        idempotency_key=body.get("idempotency_key"),
                        question_id=body.get("question_id"),
                    )
                except auth.RateLimited as e:
                    return _too_many(_retry_after(str(e)))
                if isinstance(outcome, harness_turn.StartOver):
                    resp = JSONResponse({"ok": True, "start_over": True}, headers=NO_STORE)
                    if outcome.fresh:  # §5: a draft retired, its cookies go; a claimed business keeps its
                        resp.delete_cookie(VISIT_COOKIE, path="/api")
                        resp.delete_cookie(BIZ_COOKIE, path="/")
                    return resp
                if isinstance(outcome, harness_turn.Reroute):
                    action, body, reading = outcome.action, {**body, **outcome.body}, outcome.reading
                elif outcome.get("wall"):
                    return JSONResponse(WALL, headers=NO_STORE)
                elif "error" in outcome:
                    return JSONResponse({"ok": False, "error": outcome["error"]}, headers=NO_STORE)
                else:
                    support_mail.after_tool(outcome)  # a ticket opened: its mail goes out now
                    return JSONResponse(
                        {
                            "ok": True,
                            "result": outcome,
                            "visitor": _visitor_block(conn, request, biz, session, settings),
                        },
                        headers=NO_STORE,
                    )
            else:
                linked = status == "ready" and listing_linked(conn, biz["id"])
                asked = None
                if (settings.support and status in (None, "ready")) or linked:  # read first (spec 2026-09-30 D131)
                    try:
                        asked = _say_support(
                            conn,
                            (body.get("text") or "").strip(),
                            request.cookies[VISIT_COOKIE],
                            ip,
                            linked=linked,
                            idempotency_key=body.get("idempotency_key"),
                        )
                    except Limited as e:
                        return _too_many(e.retry_after)
                    if isinstance(asked, dict) and "error" in asked:
                        return JSONResponse({"ok": False, "error": asked["error"]}, headers=NO_STORE)
                    if isinstance(asked, dict):
                        support_mail.after_tool(asked)  # the ticket is committed: its mail goes out now
                        return JSONResponse(
                            {
                                "ok": True,
                                "result": asked,
                                "visitor": _visitor_block(conn, request, biz, session, settings),
                            },
                            headers=NO_STORE,
                        )
                if (
                    asked is not None
                ):  # the words said the place linked is not theirs: unlinked, before the wall (_unlink)
                    action, body = asked
                elif session is None:
                    env = envelope.make({}, say=NOT_AN_ADJUSTMENT_SAY)
                elif session["status"] == "open":
                    qid = consult.pending(session)[0]["id"]
                    action, body = "answer", {**body, "answers": {qid: {"other": (body.get("text") or "").strip()}}}
                elif session["status"] == "ready":
                    if draft:  # I2: the business's own state, never a second read of the session's
                        return JSONResponse(WALL, headers=NO_STORE)
                    env = envelope.make({}, say=READY_SAY)
                else:  # planned
                    # B0 §3.3: an open question of what stops the launch reads the words first, in code; R11
                    # (review): the same list says whether "I can change …" answers in place (anything still on
                    # it) or sends the owner on to their assistant (nothing is)
                    checklist = (None if draft else launch_check.check(conn, settings, biz["id"])) or {}
                    found = next((i for i in checklist.get("items", []) if i["kind"] == "ask"), None)
                    value = blockers.read_typed(found, (body.get("text") or "").strip()) if found else None
                    if value is not None:
                        try:
                            key, ik = request.cookies[VISIT_COOKIE], body.get("idempotency_key")
                            said = _saved(settings, biz["id"], found, value, key, ip, ik)
                        except Limited as e:
                            return _too_many(e.retry_after)
                        except ToolError as e:
                            return JSONResponse({"ok": False, "error": str(e)}, headers=NO_STORE)
                        return JSONResponse(
                            {
                                "ok": True,
                                "result": _resolved(conn, request, settings, said),
                                "visitor": _visitor_block(conn, request, biz, session, settings),
                            },
                            headers=NO_STORE,
                        )
                    try:
                        outcome = _say_adjust(conn, body, biz, request.cookies[VISIT_COOKIE], ip)
                    except Limited as e:
                        return _too_many(e.retry_after)
                    if isinstance(outcome, tuple):
                        action, body = outcome
                    elif "error" in outcome:
                        return JSONResponse({"ok": False, "error": outcome["error"]}, headers=NO_STORE)
                    elif checklist.get("items") and outcome.get("say") == NOT_AN_ADJUSTMENT_SAY:
                        env = {**outcome, "say": NOT_AN_ADJUSTMENT_HERE}  # B0 P7: the list still has something to fix
                    else:
                        env = outcome
                if action == "say":  # not rerouted into a real tool: answer directly with the envelope above
                    support_mail.after_tool(env)  # a ticket opened after the plan: its mail goes out now
                    return JSONResponse(
                        {"ok": True, "result": env, "visitor": _visitor_block(conn, request, biz, session, settings)},
                        headers=NO_STORE,
                    )
        # I2 (final review): a draft's recommend is always the wall — deciding it from the session's status
        # here and letting the tool re-read it on another connection let a racing final answer through.
        if (draft and action == "recommend") or (wall and action == "answer" and settings.anon_turns > 0):
            return JSONResponse(WALL, headers=NO_STORE)
        args = _args(action, body, session)
    try:
        with consult.with_reading(reading):  # the harness's reading of a typed answer (K6 slice 1, D2); {} otherwise
            env = call(action, args, request.cookies[VISIT_COOKIE], ip=ip)
    except Limited as e:
        return _too_many(e.retry_after)
    if action == "update_business" and env.get("error") == listings.NOTHING_LINKED:
        env = envelope.make({}, say=listings.NOTHING_LINKED)  # unlinked meanwhile: a reply, as change_plan's
    if "error" in env:
        return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        biz = business.row(conn, biz["id"])
        session = _current_session(conn, biz["id"])
        if action == "update_business":  # the owner's words unlinked the listing
            env = _unlinked(env, body["then"], session)
        if action in ("recommend", "adjust"):  # L1: a funded business's `next` offers the owner's yes, not a way to pay
            env = _funded_next(conn, settings, biz["id"], env)
        return JSONResponse(
            {"ok": True, "result": env, "visitor": _visitor_block(conn, request, biz, session, settings)},
            headers=NO_STORE,
        )


def _too_many(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": "Too many requests; try again in a moment."},
        status_code=429,
        headers={**NO_STORE, "Retry-After": str(retry_after)},
    )


@router.get("/api/state")
def api_state(request: Request):
    """The business's MarketingState (spec 2026-09-29 D102) for any later page, under the homepage's own rules: the
    visitor's cookie (none or a dead one: start over, as the `state` action says), its owner's web session (a claimed
    business's cookie opens nothing without it), and the owner's only -- a draft nobody owns yet is behind the wall.
    `version` in the state says which shape it is."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        rec, biz = _visitor(conn, request)
        if rec is None:
            fresh = {"ok": True, "fresh": True}
            if reason := _fresh_reason(conn, request):
                fresh["reason"] = reason
            resp = JSONResponse(fresh, headers=NO_STORE)
            resp.delete_cookie(VISIT_COOKIE, path="/api")
            resp.delete_cookie(BIZ_COOKIE, path="/")
            return resp
        if _needs_sign_in(conn, request, biz):
            return JSONResponse({"ok": True, "signin": True}, headers=NO_STORE)
        if biz["claim_state"] == "draft":
            return JSONResponse(WALL, headers=NO_STORE)
        session = _current_session(conn, biz["id"])
        return JSONResponse(
            {
                "ok": True,
                "state": state.build(conn, settings, biz["id"]).model_dump(mode="json"),
                "visitor": _visitor_block(conn, request, biz, session, settings),
            },
            headers=NO_STORE,
        )


@router.get("/api/csrf")
def api_csrf(request: Request):
    """The page's CSRF token once signed in (for /api/claim and /api/handoff); null without a web session."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        web_session = sessions.of_request(conn, request)
    return JSONResponse({"csrf": web_session["csrf"] if web_session else None}, headers=NO_STORE)


@router.post("/api/claim")
def api_claim(request: Request, body: dict = Body(...)):
    """The wall's sign-in continuation: a signed-in visitor with a live draft claims it, upgrading the draft
    key to a business key in place (no new key) and lifting the wall. Refused with OTHER_PAGE (`_other_page`)
    for a page showing another business than the visitor cookie's (follow-up R1)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        web_session = sessions.of_request(conn, request)
        if web_session is None:
            return JSONResponse({"ok": False, "error": "Sign in first."}, status_code=401, headers=NO_STORE)
        rec, biz = _visitor(conn, request)
        if rec is None:
            return JSONResponse(FRESH, headers=NO_STORE)
        if (other := _other_page(request, biz)) is not None:
            return other
        sessions.check_csrf(web_session, body.get("csrf"))
        try:
            with conn.transaction():
                out = claims.claim(conn, biz["id"], web_session["email"])
                audit.record_web(
                    conn,
                    tool="web.claim",
                    email=web_session["email"],
                    ip=sessions.request_ip(request),
                    business_id=biz["id"],
                    args={"business_id": biz["id"]},
                    result=out,
                )
        except claims.ClaimRefused as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409, headers=NO_STORE)
        support_mail.flush(business_id=biz["id"])  # the acknowledgement a waiting ticket was holding back
        biz = business.row(conn, biz["id"])
        session = _current_session(conn, biz["id"])
        resp = JSONResponse(
            {"ok": True, "visitor": _visitor_block(conn, request, biz, session, settings)}, headers=NO_STORE
        )
        resp.set_cookie(VISIT_COOKIE, request.cookies[VISIT_COOKIE], **cookie_args(settings, max_age=CLAIMED_MAX_AGE))
        resp.set_cookie(
            BIZ_COOKIE,
            keys.hash_key(request.cookies[VISIT_COOKIE]),
            **biz_cookie_args(settings, max_age=CLAIMED_MAX_AGE),
        )
        return resp


def _handoff_mints_last_hour(conn, business_id: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE business_id = %s AND tool = 'web.handoff' "
        "AND ts > now() - interval '1 hour'",
        (business_id,),
    ).fetchone()["n"]


@router.post("/api/handoff")
def api_handoff(request: Request, body: dict = Body(...)):
    """The assistant sheet's Copy/Share button (spec §3 D41): mints a 30-minute hand-off token for the
    visitor's own consultation and the three paste prompts. Needs a live visitor cookie (any anonymous
    or signed-in one); a web session, if there is one, must carry the right CSRF token. Rate-limited to
    HANDOFF_LIMIT_PER_HOUR mints per business per hour. Refused with OTHER_PAGE (`_other_page`) for a page
    showing another business than the visitor cookie's (follow-up R1)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        rec, biz = _visitor(conn, request)
        if rec is None:
            return JSONResponse(FRESH, headers=NO_STORE)
        if _needs_sign_in(conn, request, biz):
            return JSONResponse(SIGNIN, headers=NO_STORE)
        if (other := _other_page(request, biz)) is not None:
            return other
        web_session = sessions.of_request(conn, request)
        if web_session is not None:
            sessions.check_csrf(web_session, body.get("csrf"))
        session = consult.handoff_for(conn, biz["id"])  # open/ready first, else the plan already built
        if session is None:
            return JSONResponse(
                {"ok": False, "error": "Nothing to hand off yet — start a plan first."}, headers=NO_STORE
            )
        if _handoff_mints_last_hour(conn, biz["id"]) >= HANDOFF_LIMIT_PER_HOUR:
            return JSONResponse(
                {"ok": False, "error": "Too many requests; try again in a moment."},
                status_code=429,
                headers=NO_STORE,
            )
        ip = sessions.request_ip(request)
        with conn.transaction():
            token = consult.mint_handoff(conn, session["id"])
            args, result = {"session_id": session["id"]}, {"minted": True}
            if web_session is not None:
                audit.record_web(
                    conn,
                    tool="web.handoff",
                    email=web_session["email"],
                    ip=ip,
                    business_id=biz["id"],
                    args=args,
                    result=result,
                )
            else:  # the visitor's own draft key minted it (m8), attributed like web.say
                audit.record(
                    conn,
                    tool="web.handoff",
                    args=args,
                    result=result,
                    ok=True,
                    business_id=biz["id"],
                    actor=f"key:{rec.id}",
                    actor_class=rec.cls,
                    api_key_id=rec.id,
                    ip=ip,
                )
        base = settings.public_base_url
        return JSONResponse(
            {
                "ok": True,
                "token": token,
                "expires_in": int(consult.HANDOFF_TTL.total_seconds()),
                "prompts": connect.prompts(base, token),
                "mcp_url": f"{base}/mcp",
            },
            headers=NO_STORE,
        )


# ---------------------------------------------------------------- Admiry Pages, the homepage's own card (Task 7, H14)


# A3p-3 P22/P23: the Muse invitation once the page Admiry wrote is live, and the card once the assistant's own is
MUSE_INVITE = "Want more pages, your own design or online booking? Continue in Muse: it can change this site with you."
MANAGED = "Your assistant manages this site now."


def _page_extras(conn, settings, biz_id: str, page: dict | None) -> dict:
    """What the page card says beside the preview (A3p-3, plan Q9-Q11, Q13): the hosting status block (the
    countdown's or the takedown's sentence in its `say`), the note above Publish (the minimum, and the date to start
    the ads by while the site is not covered), the Muse invitation once the page is live, and where the owner
    downloads the site Admiry hosts (behind sign-in)."""
    live = page is not None and page["status"] == "published" and page.get("source") in hosting_minimum.HOSTED
    return {
        "hosting": hosting_minimum.status(conn, biz_id, page),
        "hosting_note": hosting_minimum.publish_note(conn, biz_id, page),
        "muse": MUSE_INVITE if live else None,
        "download_url": hosting_minimum.download_url(settings, biz_id) if hosted.source(conn, biz_id) else None,
    }


def _page_gate(conn, request: Request, settings) -> tuple[dict | None, dict | None, JSONResponse | None]:
    """(business, web session) for a signed-in owner ready to act on their page, or (None, None, the response
    to send instead): SIGNIN for no live visitor cookie at all, or one whose owner is not who is signed in here
    (as every other owner-only action on this page requires — mirrors `_needs_sign_in`'s own use above); OTHER_PAGE
    (`_other_page`) for a page showing another business than the visitor cookie's (follow-up R1); WALL
    for a business that has not been claimed yet — the same gate `recommend` and the plan page's own Approve
    show a draft. A plan already exists by the time the homepage's own button can be clicked (a draft business
    can never reach a planned session: `recommend` is always the wall for one, see `turn` above), so the WALL
    branch here is defensive rather than a path real traffic takes."""
    rec, biz = _visitor(conn, request)
    if rec is None or _needs_sign_in(conn, request, biz):
        return None, None, JSONResponse(SIGNIN, headers=NO_STORE)
    if (other := _other_page(request, biz)) is not None:
        return None, None, other
    if biz["claim_state"] == "draft":
        return None, None, JSONResponse(WALL, headers=NO_STORE)
    return biz, sessions.of_request(conn, request), None


def _web_origin(settings) -> str:
    """H16: the origin the homepage's own card frames a page's preview at (`hosted.with_embed_origin`'s
    argument, and what a fresh preview `/api/page/state` mints carries too)."""
    return settings.web_base_url


@router.post("/api/page/create")
def api_page_create(request: Request, body: dict = Body(...)):
    """ "Build a page for these ads" (H14): create_page, called exactly the way `_turn`'s actions call every
    other tool (`call`, under `auth.with_headers`) — wrapped in `hosted.with_embed_origin` so the preview it
    mints can be framed on this same card (H16) instead of refusing every origin like an assistant's own. A
    business that gets Admiry's own site (A3p-6 batch 2 Q17: `tools_sites.takes_over`) is answered its site card
    instead, the state `/api/page/state` gives it -- the look is picked there first -- and nothing is written."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        if tools_sites.takes_over(conn, biz["id"]):  # the button pressed before the state came: the site card
            return JSONResponse({"ok": True, **_site_state(conn, settings, biz["id"])}, headers=NO_STORE)
        key = request.cookies[VISIT_COOKIE]
    ip = sessions.request_ip(request)
    try:
        with hosted.with_embed_origin(_web_origin(settings)):
            env = call("create_page", {"template": None, "brief": None}, key, ip=ip)
    except Limited as e:
        return _too_many(e.retry_after)
    if "error" in env:
        return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        extras = _page_extras(conn, settings, biz["id"], landing.current(conn, biz["id"]))
    return JSONResponse({"ok": True, **env["data"], **extras}, headers=NO_STORE)


@router.post("/api/page/revise")
def api_page_revise(request: Request, body: dict = Body(...)):
    """ "Change…" (H14): one sentence of direction, through revise_page the same way create above calls
    create_page — its fresh preview minted for this card's own origin again."""
    instruction = body.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        key = request.cookies[VISIT_COOKIE]
    ip = sessions.request_ip(request)
    try:
        with hosted.with_embed_origin(_web_origin(settings)):
            env = call("revise_page", {"instruction": instruction}, key, ip=ip)
    except Limited as e:
        return _too_many(e.retry_after)
    if "error" in env:
        return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
    if env["data"].get("beyond_template"):  # A3p-3 P22: nothing changed; the card offers Muse with the sentence
        return JSONResponse({"ok": True, "beyond_template": True, "say": env["say"]}, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        extras = _page_extras(conn, settings, biz["id"], landing.current(conn, biz["id"]))
    return JSONResponse({"ok": True, **env["data"], **extras}, headers=NO_STORE)


def _web_tool_error(e: ToolError, *, site: bool = False) -> str:
    """An owner-readable ToolError sentence (H40 minor 2): for an expired or unknown preview the tool says "make a new
    preview", the homepage says "build the page again" -- for Admiry's own site, Q12's EXPIRED (A3p-6 batch 2 Task 7 fix
    round 1); an assistant relaying `str(e)` still reads the tool's."""
    if str(e) == proposals.PREVIEW_UNKNOWN:
        return tools_sites.EXPIRED if site else "That preview has expired; build the page again."
    return str(e)


@router.post("/api/page/publish")
def api_page_publish(request: Request, body: dict = Body(...)):
    """ "Publish this page" (H14, H8): the very proposal `publish_page` itself would build
    (`tools_hosting._propose_publish`, shared rather than duplicated), confirmed right here as the web owner —
    like the plan page's own Approve (`proposals.confirm(web_owner=True)`) — and then, since `proposals.confirm`
    runs no after-commit work of its own (the Task 3 review noted this for exactly this path), the same hooks
    `confirm_proposal` would dispatch: `tools_hosting.AFTER_COMMIT["publish_page"]` — the 14-rule check, the
    report e-mail, and the ads move or resume. H40: a token already live (or already the subject of an
    open, unconfirmed proposal) never creates a second one — `_propose_publish` answers with the live URL
    (or reuses the open proposal) and this handler runs nothing further for the former (no confirm, no
    after-commit hooks, no audit row: nothing changed)."""
    token = body.get("token")
    if not isinstance(token, str) or not token:
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        try:
            with conn.transaction():
                prop, say = tools_hosting._propose_publish(conn, biz["id"], token=token, version=None)
                if prop is None:
                    page = landing.current(conn, biz["id"])
                    return JSONResponse(
                        {
                            "ok": True,
                            "page": proposals._hosted_page_view(page),
                            "check": landing.saved_report(page),
                            "say": say,
                            **_page_extras(conn, settings, biz["id"], page),
                        },
                        headers=NO_STORE,
                    )
                out = proposals.confirm(conn, biz["id"], prop["proposal_id"], code=None, web_owner=True)
                audit.record_web(
                    conn,
                    tool="web.publish_page",
                    email=session["email"],
                    ip=sessions.request_ip(request),
                    business_id=biz["id"],
                    args={"token": token, "proposal_id": prop["proposal_id"]},
                    result=out,
                    approval_kind="web",
                    approval_ref=prop["proposal_id"],
                )
        except ToolError as e:
            said = _web_tool_error(e, site=tools_hosting._is_site(conn, biz["id"]))
            return JSONResponse({"ok": False, "error": said}, status_code=409, headers=NO_STORE)
        result = tools_hosting.AFTER_COMMIT[out["kind"]](envelope.wrap("confirm_proposal", out), biz["id"])
        # H40 minor 3: proposals.confirm(web_owner=True) only skips the spend-class code for a nonspend kind
        # like this one -- its own `approval` always reads {"kind": "assistant"} regardless of who confirmed
        # it. The audit row above already says "web" correctly; the reply must say the same thing.
        data = {**result["data"], "approval": {"kind": "web"}}
        with db.connect(settings.database_url) as conn:
            extras = _page_extras(conn, settings, biz["id"], landing.current(conn, biz["id"]))
        return JSONResponse({"ok": True, **data, **extras, "say": result["say"]}, headers=NO_STORE)


def _home_preview(conn, settings, biz_id: str, page: dict, sections: dict, origin: str) -> tuple[str, str]:
    """(preview_url, token) for the page's current sections, always framable at `origin` (H16) for a returning
    visitor: the page's newest preview when it already carries this origin, else one minted right here —
    `page_builder.finish` + `hosted.save_preview`, the same render `create_page`/`revise_page`'s own `save`
    leaves behind, but without `save`'s version bump (nothing changed; this is a fresh window on the same
    content, not a new draft). Needed because a photo added from the phone link (`uploads.py`'s `/u/{token}`,
    out of this card's reach) mints its own preview with no embed_origin — H16/H37 do not thread one through
    that route, so its newest preview alone would fail to frame here until this re-mints it."""
    token = uploads._latest_preview_token(conn, page["id"])
    prev = hosted.preview(conn, token) if token else None
    if prev is not None and prev["embed_origin"] == origin:
        return f"{settings.public_base_url}/p/{token}", token
    kit = landing.kit(conn, biz_id)
    copy = PageCopy.model_validate(sections["copy"])
    html, _problems = page_builder.finish(
        settings,
        kit,
        sections["tree"],
        copy,
        photos=list(sections.get("photos") or []),
        hostname=page_builder.page_hostname(page, settings),
        hours=sections.get("hours"),
    )
    token = hosted.save_preview(
        conn, page["id"], html, page_builder.title_for(copy, kit), sections=sections, embed_origin=origin
    )
    return f"{settings.public_base_url}/p/{token}", token


SHOT_PATH = "/api/page/shot"
SIZES = ("phone", "tablet", "desktop")


@router.get(SHOT_PATH)
def api_page_shot(request: Request, size: str = "phone", preview: str | None = None):
    """A3p-3 batch 4 (R6): the render check's screenshot of the site an assistant made, as it is live (`size` phone, tablet
    or desktop) -- for the signed-in owner of the visitor's own business only; 404 for anyone else and for a site with
    none. Private: a browser may keep it five minutes, a proxy never (the card's address names the version). With
    `preview=1` (A3p-6 batch 2 Q18), the screenshot of the owner's live site preview instead -- the industry site Admiry
    built, at the size asked -- under the same rules: the site card's three sizes."""
    settings = config.load()
    if size not in SIZES:
        return Response(status_code=404, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        biz, _session, err = _page_gate(conn, request, settings)
        if err is not None:
            shot = None
        elif preview == "1":
            shot = _preview_shot(conn, biz["id"], size)
        else:
            live = landing.current(conn, biz["id"])
            shot = (
                bundles.shot(conn, live["id"], live["version"], size) if tools_hosting._assistants_page(live) else None
            )
    if shot is None:
        return Response(status_code=404, headers=NO_STORE)
    return Response(
        shot,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/page/state")
def api_page_state(request: Request, body: dict = Body(...)):
    """The current preview/published state for a returning visitor (H14): null once Admiry has not written a
    page for this business yet (the card shows just the button), else its sections, a preview the card can
    always frame, the upload link while it is valid, and — once published — the address and the last check's
    pass count for the status line. A home-services business or a dental practice that gets Admiry's own site
    (A3p-6 batch 2 Q17: AGENT_SITES on, no starter page) is answered its site card's state (`_site_state`) instead,
    after the assistant's own live page and before the starter page."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        live = landing.current(conn, biz["id"])
        if tools_hosting._assistants_page(live):  # A3p-3 P23: the assistant's own page is live; no quick changes here
            managed = {
                "managed_by": "assistant",
                "say": MANAGED,
                "status": live["status"],
                "url": live["url"],
                "hostname": live.get("hostname"),
                "check": landing.saved_report(live),
                # A3p-3 batch 4: a site bundle's screenshot from the render check, when it has one
                "shot_url": f"{SHOT_PATH}?size=phone&v={live['version']}"
                if bundles.shot(conn, live["id"], live["version"], "phone")
                else None,
                **_page_extras(conn, settings, biz["id"], live),
            }
            return JSONResponse({"ok": True, "page": managed}, headers=NO_STORE)
        if tools_sites.takes_over(conn, biz["id"]):  # A3p-6 batch 2 Q17: Admiry's own site, never a null page
            return JSONResponse({"ok": True, "page": _site_state(conn, settings, biz["id"])}, headers=NO_STORE)
        found = page_builder.current(conn, biz["id"])
        if found is None:
            return JSONResponse({"ok": True, "page": None}, headers=NO_STORE)
        page, sections = found
        preview_url, token = _home_preview(conn, settings, biz["id"], page, sections, _web_origin(settings))
        published = page["status"] == "published"
        data = {
            "status": page["status"],
            "token": token,
            "preview_url": preview_url,
            "sections": sections,
            "upload_url": uploads.latest_upload_url(conn, page["id"], settings.web_base_url),
            "url": page.get("url") if published else None,
            "hostname": page.get("hostname"),
            "check": landing.saved_report(page) if published else None,
            "managed_by": "admiry",
            **_page_extras(conn, settings, biz["id"], page),
        }
        return JSONResponse({"ok": True, "page": data}, headers=NO_STORE)


# ---------------------------------------------------------------- the industry site card (A3p-6 batch 2 Task 7, Q17)

# The site card's build button: before a site is built, and once its preview has ended (the draft keeps its words)
BUILD_SITE = "Build my site"
BUILD_AGAIN = "Build it again"
EXTRAS_SKIPPED = "Noted. You can add more facts any time."  # the industry's facts skipped (fix round 1)
SITE_ARGS = {"style": str, "accent": str, "facts": dict, "wordmark": bool}  # what the card may send build_site


def _preview_shots(conn, token: str) -> dict[str, str]:
    """The site card's screenshots (Q18): for each size the live site preview `token` has one of, its address on this
    host -- `v` the token's start, so a new preview is a new address and never an old picture kept five minutes."""
    row = conn.execute(
        "SELECT shot_phone IS NOT NULL AS phone, shot_tablet IS NOT NULL AS tablet, "
        "shot_desktop IS NOT NULL AS desktop FROM page_preview WHERE token = %s",
        (token,),
    ).fetchone()
    return {size: f"{SHOT_PATH}?size={size}&preview=1&v={token[:8]}" for size in SIZES if row and row[size]}


def _preview_shot(conn, biz_id: str, size: str) -> bytes | None:
    """The screenshot at `size` of the business's live site preview (Q18: `tools_sites._live_preview`, its newest bundle
    preview whose sections say `kind: site`, never the gallery of looks), or None."""
    page = landing.current(conn, biz_id)
    found = tools_sites._live_preview(conn, page["id"]) if page else None
    if found is None:
        return None
    column = {"phone": "shot_phone", "tablet": "shot_tablet", "desktop": "shot_desktop"}[size]
    row = conn.execute(f"SELECT {column} AS shot FROM page_preview WHERE token = %s", (found["token"],)).fetchone()
    return bytes(row["shot"]) if row and row["shot"] else None


def _site_state(conn, settings, biz_id: str) -> dict:
    """The site card's state (Q17), drawn by static/sites.js: where the site stands (`tools_sites.site_view`), the
    screenshots of its live preview (`shots`, by size; none without the render check), the question of each fact still
    missing (`prompts`), the industry's own facts the card asks (`extras`), the build button's words (`build_label`:
    "Build it again" once the preview has ended, or while it must be built again: `rebuild_needed`, `_rebuild_needed`)
    and where to download the site once Admiry hosts it."""
    view = tools_sites.site_view(conn, settings, biz_id)
    preview = view["preview"]
    ended = view["status"] == "preview" and preview is None
    rebuild = _rebuild_needed(conn, view)
    return {
        "kind": "site",
        **view,
        "shots": _preview_shots(conn, preview["token"]) if preview else {},
        "prompts": {fact: intake.QUESTIONS[fact] for fact in view["missing"]},
        "extras": list(intake.EXTRAS[view["family"]]),
        "build_label": BUILD_AGAIN if ended or rebuild else BUILD_SITE,
        "download_url": hosting_minimum.download_url(settings, biz_id) if hosted.source(conn, biz_id) else None,
        "newer_preview": view["status"] == "published" and preview is not None and _newer(conn, preview["token"]),
        "rebuild_needed": rebuild,
        # the owner's own website as the ads' page (the card's "Already have a website?", 2026-10-08): the card says
        # where the ads send people, and offers Admiry's site instead
        "own_site_url": _own_site_url(conn, biz_id),
        # the hours as the site's words wrote them (2026-10-08), said on the card for the owner to check
        "hours_shown": list((intake.draft(landing.current(conn, biz_id))["words"] or {}).get("hours") or []),
    }


def _own_site_url(conn, biz_id: str) -> str | None:
    """The address of the owner's own page the ads use -- the page on file is the owner's (`source` owner), published at
    an address -- else None."""
    page = landing.current(conn, biz_id)
    if page and page.get("source") == "owner" and page.get("status") == "published" and page.get("url"):
        return page["url"]
    return None


def _rebuild_needed(conn, view: dict) -> bool:
    """Whether the live site preview must be built once more before it is published (final review): the logo and the key
    photo are in and the preview does not carry the photos now on file -- `build.publish_gate` answers `REBUILD_FIRST`,
    so the card offers "Build it again". The gate is asked only once the key photos are in (before, its own sentence
    holds Publish) and only of a live preview."""
    preview = view["preview"]
    if preview is None or not view["key_photos"]["ok"]:
        return False
    return site_build.publish_gate(conn, hosted.preview(conn, preview["token"])) == site_build.REBUILD_FIRST


def _newer(conn, token: str) -> bool:
    """Whether a live site's preview `token` is newer than what is live (fix round 1, item 5): drawn from another document
    than the one the page was published from -- a photo or a look changed it since -- not the preview just published,
    which stays live for 24 hours (a publish lays the preview's sections, `site_spec_id` with them, on the page row)."""
    row = conn.execute(
        "SELECT v.sections->>'site_spec_id' AS shown, p.sections->>'site_spec_id' AS live "
        "FROM page_preview v JOIN page p ON p.id = v.page_id WHERE v.token = %s",
        (token,),
    ).fetchone()
    return row is not None and row["shown"] != row["live"]


def _site_args(body: dict) -> dict | None:
    """What the card sends build_site (`SITE_ARGS`), each absent or of its kind, and a text any page can show -- else
    None: a bad request. The command's own sentences judge the rest (a look the family has not, a fact refused)."""
    out = {}
    for name, kind in SITE_ARGS.items():
        value = body.get(name)
        if value is not None and not isinstance(value, kind):
            return None
        if isinstance(value, str) and not value.isprintable():  # a lone surrogate, a control character
            return None
        out[name] = value
    return out


@router.post("/api/site/build")
def api_site_build(request: Request, body: dict = Body(...)):
    """The site card's build (Q17): a look tapped, the facts typed on the card, the wordmark, the industry's own facts,
    or "Build it again" -- build_site, called as /api/page/create calls create_page (`call`, the preview minted for
    this origin) and from the card (`tools_sites.on_the_card`: the card asks the industry's facts itself). Answers the
    card's state again (`_site_state`) with the command's sentence, or {ok: false, error} with its refusal: the lock's
    sentence while another build runs, a fact refused, a build that failed. Industry facts given here mark them asked
    (Q4)."""
    args = _site_args(body)
    if args is None:
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        key = request.cookies[VISIT_COOKIE]
    try:
        with hosted.with_embed_origin(_web_origin(settings)), tools_sites.on_the_card():
            env = call("build_site", args, key, ip=sessions.request_ip(request))
    except Limited as e:
        return _too_many(e.retry_after)
    if "error" in env:
        return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        built_on = landing.current(conn, biz["id"])  # the call wrote the page row before it asked or built
        if built_on and any(fact not in intake.REQUIRED for fact in args["facts"] or {}):  # the industry's, answered
            intake.update_draft(conn, built_on["id"], {"extras_asked": True})
        page = _site_state(conn, settings, biz["id"])
    return JSONResponse({"ok": True, "page": page, "say": env["say"]}, headers=NO_STORE)


def _no_site(conn, settings, biz_id: str) -> str | None:
    """Why the business has no site card, in the command's own words -- the sites are off here, the starter page Admiry
    wrote is its page, or it is of no family of sites -- or None when it has one (`tools_sites.takes_over`)."""
    if not settings.sites:
        return tools_sites.SITES_OFF
    if tools_sites.takes_over(conn, biz_id):
        return None
    return tools_sites.STARTER_PAGE if page_builder.current(conn, biz_id) is not None else tools_sites.NOT_A_FAMILY


@router.post("/api/site/extras")
def api_site_extras(request: Request, body: dict = Body(...)):
    """ "Skip" on the site card's industry facts (Q4, Q17): not asked again -- `extras_asked` on the draft, nothing
    built -- and the card's state again, with a sentence the card reads out (fix round 1)."""
    if body.get("skipped") is not True:
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        if (why := _no_site(conn, settings, biz["id"])) is not None:
            return JSONResponse({"ok": False, "error": why}, headers=NO_STORE)
        intake.update_draft(conn, landing.ensure_page_for_kit(conn, biz["id"])["id"], {"extras_asked": True})
        page = _site_state(conn, settings, biz["id"])
        return JSONResponse({"ok": True, "page": page, "say": EXTRAS_SKIPPED}, headers=NO_STORE)


# ---------------------------------------------------------------- the "Your website" card (A3p-5 batch 2 Task 2)

# Check now with no website of the business's own to check: the card shows only for a business with one (defensive)
NO_WEBSITE = "There is no website on file to check."


@router.post("/api/page/website")
def api_page_website(request: Request, body: dict = Body(...)):
    """The "Your website" card's steps view (plan Q6, "I'll add it"): the plan's row (site_install.row) and, while it
    needs Admiry's code, the steps for the page the ads use and the code to paste (site_install.steps_view) -- for the
    signed-in owner of a claimed business, as the page card's own endpoints are (`_page_gate`). `check` (spec
    2026-10-12 §3.5): the page on file's report as the card lists it, judged against the plan of now
    (landing.judged_report), with the time it was checked; null before any check."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        row = site_install.row(conn, biz["id"])
        steps = site_install.steps_view(conn, biz["id"], row)
        page = landing.current(conn, biz["id"])
        check = landing.judged_report(conn, page)
    if check is not None and not check.get("checked_at"):  # the saved report has no time: the page row keeps it
        check = {**check, "checked_at": iso(page.get("checked_at"))}
    return JSONResponse({"ok": True, "website": row, "install": steps, "check": check}, headers=NO_STORE)


@router.post("/api/page/check")
def api_page_check(request: Request, body: dict = Body(...)):
    """Check now (plan Q6): the check, now, on the page the ads use -- an existing command, called in process as the other
    actions call tools (`call`, under `auth.with_headers`), so its check runs and its report is saved and read by the
    launch, whose gate does not change (W14). The owner page the ads use is checked again as check_landing_page checks
    the page on file: running ads never move, and its report is e-mailed only when whether it passes changed (rulings
    R2-b, R2-d); with none, set_landing_page names the owner's website the ads' page and e-mails its first report
    (site_install.check_now). The answer: "Found it. Your ads can run." when the check passes, else the command's own
    sentence, its fixes in it (Q11), with the row as it now stands. While the ads use a page Admiry hosts nothing is
    checked or moved: the answer is the row's own sentence."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        target = site_install.check_now(conn, biz["id"])
        row = site_install.row(conn, biz["id"]) if target is None else None
        key = request.cookies[VISIT_COOKIE]
    if target is None:
        if row is None:
            return JSONResponse({"ok": False, "error": NO_WEBSITE}, headers=NO_STORE)
        return JSONResponse({"ok": True, "say": row["say"], "website": row, "check": None}, headers=NO_STORE)
    command, url = target
    # check_landing_page with no address checks the page on file (an address would be a dry run, saved nowhere)
    args = {"url": None} if command == "check_landing_page" else {"url": url, "idempotency_key": db.new_id("ik")}
    try:
        env = call(command, args, key, ip=sessions.request_ip(request))
    except Limited as e:
        return _too_many(e.retry_after)
    if "error" in env:
        return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
    report = env["data"]["check"]
    with db.connect(settings.database_url) as conn:
        row = site_install.row(conn, biz["id"])
    say = site_install.FOUND_IT if report and report["ok"] else env["say"]
    return JSONResponse({"ok": True, "say": say, "website": row, "check": report}, headers=NO_STORE)


MAX_ADDRESS = 320  # what /api/page/install-send takes as an address before the command's own rule reads it
OWN_SITE_NEEDED = "Type your website's address, like yourbusiness.com."


@router.post("/api/page/own-site")
def api_page_own_site(request: Request, body: dict = Body(...)):
    """ "Already have a website?" on the site card (the founder's dev walk, 2026-10-08): the address the owner typed (no
    scheme read as https) named the ads' page by set_landing_page -- its refusals, its check, its saved report and its
    e-mail are the command's -- and then kept as the business's website (update_business), so the "Your website" card
    and its ways to add Admiry's code follow. A refused address writes nothing. The answer is Check now's: "Found it."
    when the check passes, else the command's own sentence, with the row as it now stands."""
    url = body.get("url")
    url = url.strip() if isinstance(url, str) else ""
    if not url or len(url) > MAX_ADDRESS or not url.isprintable():
        return JSONResponse({"ok": False, "error": OWN_SITE_NEEDED}, headers=NO_STORE)
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        key = request.cookies[VISIT_COOKIE]
    ip = sessions.request_ip(request)
    try:
        env = call("set_landing_page", {"url": url, "idempotency_key": db.new_id("ik")}, key, ip=ip)
        if "error" in env:
            return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
        kept = call("update_business", {"patch": {"website": url}, "idempotency_key": db.new_id("ik")}, key, ip=ip)
    except Limited as e:
        return _too_many(e.retry_after)
    if "error" in kept:
        return JSONResponse({"ok": False, "error": kept["error"]}, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        row = site_install.row(conn, biz["id"])
    report = env["data"]["check"]
    say = site_install.FOUND_IT if report and report["ok"] else env["say"]
    return JSONResponse({"ok": True, "say": say, "website": row, "check": report}, headers=NO_STORE)


@router.post("/api/page/install-send")
def api_page_install_send(request: Request, body: dict = Body(...)):
    """ "Send it to my web person" (plan Q7, A3p-5 batch 2 Task 3): the address the owner typed, through
    send_install_steps -- its e-mail, its refusals and its limit are the command's (site_install.send_steps)."""
    email = body.get("email")
    if not isinstance(email, str) or not email.strip() or len(email) > MAX_ADDRESS:
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    return _install_way(request, body, "send_install_steps", {"email": email})


@router.post("/api/page/install-help")
def api_page_install_help(request: Request, body: dict = Body(...)):
    """ "Have Admiry's team do it" (plan Q8, Task 3): request_install_help, posted only when the owner taps the way's
    button, never by a page load or a link (ruling R3-c), with the contact the owner typed in the card's field, which may
    be left empty -- the gate below lets only the signed-in owner through, whose address Google verified, so the
    ticket then takes that one. A contact that is not text is a bad request; the command's rule reads the rest."""
    contact = body.get("contact")
    if contact is not None and (not isinstance(contact, str) or len(contact) > MAX_ADDRESS):
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    return _install_way(request, body, "request_install_help", {"contact": (contact or "").strip() or None})


def _install_way(request: Request, body: dict, command: str, args: dict) -> JSONResponse:
    """One of the card's two ways (ruling R0-f): for the signed-in owner of a claimed business (`_page_gate`, with the
    session's CSRF token), the command in process under the visitor's key (`call`, as Check now runs its own), a fresh
    idempotency key each press. {ok, say}, or {ok: false, error} with the command's own sentence."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        _biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        key = request.cookies[VISIT_COOKIE]
    try:
        env = call(command, {**args, "idempotency_key": db.new_id("ik")}, key, ip=sessions.request_ip(request))
    except Limited as e:
        return _too_many(e.retry_after)
    if "error" in env:
        return JSONResponse({"ok": False, "error": env["error"]}, headers=NO_STORE)
    return JSONResponse({"ok": True, "say": env["say"]}, headers=NO_STORE)


# ---------------------------------------------------------------- what stops the launch (B0, spec 2026-10-08 §3)

BLOCKER_ACTIONS = ("answer", "write_copy", "check_page", "fresh_plan", "fix_headline")


def _ran(env: dict) -> dict:
    """A command's envelope, or ToolError with its refusal (`call`'s {"error": sentence})."""
    if "error" in env:
        raise ToolError(env["error"])
    return env


def _saved(settings, biz_id: str, found: dict, value, key: str, ip: str, idempotency_key: object) -> str:
    """The answer to the open question of what stops the launch, saved by its command under the visitor's key (spec
    §3.3, plan P2): the phone and the address by update_business -- the address placed on the map as
    update_business_profile's is --, the miles by adjust on the version the question stands on now. Admiry's sentence
    for it -- for the address, the place the map found, as the business's location now stores it (final review I4;
    the words typed only when none was stored); ToolError with the command's refusal (a place the map cannot find, the
    day's lookups spent)."""
    ik = idempotency_key if isinstance(idempotency_key, str) and idempotency_key else db.new_id("ik")
    if found["id"] == "phone":
        _ran(call("update_business", {"patch": {"phone": value}, "idempotency_key": ik}, key, ip=ip))
        return blockers.SAVED_PHONE.format(phone=value)
    if found["id"] == "location":
        patch = {"location": {"address": value}}
        _ran(call("update_business", {"patch": patch, "idempotency_key": ik}, key, ip=ip))
        with db.connect(settings.database_url) as conn:
            knowledge = (business.latest_knowledge(conn, biz_id) or (None, {}))[1]
        return blockers.SAVED_PLACE.format(place=blockers.placed(knowledge) or value)
    _ran(call("adjust", {"plan_id": found["plan_id"], "radius_miles": value, "idempotency_key": ik}, key, ip=ip))
    miles = f"{value:g} mile" + ("" if value == 1 else "s")
    return blockers.SAVED_MILES.format(miles=miles)


def _resolved(conn, request: Request, settings, say: str, **more) -> dict:
    """A fix's answer as the home reads it (static/home.js `resolved`): what was done, and the state the next look
    reads -- the plan card looks at its checklist again itself (GET /api/launch_check) -- for this request's visitor."""
    rec, biz = _visitor(conn, request)
    session = _current_session(conn, biz["id"])
    return {"resolved": True, "say": say, "state": _state_result(conn, request, settings, rec, biz, session), **more}


@router.post("/api/blocker")
def api_blocker(request: Request, body: dict = Body(...)):
    """What Admiry runs for the owner from the message that says what stops the launch (B0, spec 2026-10-08 §3; plan
    P5): the question's answer (the phone, the address, the miles), the ad text, a check of the page on file, a fresh
    plan -- each an existing command (or the ad text's own work, blockers.write_copy), none dangerous (blockers.RUNS) --
    for the signed-in owner of a claimed business (`_page_gate`, its CSRF token). What stands now is the latest
    draft's checklist (launch_check), and `id` names the item a fix is for; the answer is what was done (`say`) and
    the state the next look reads (`state`); a refusal is {ok: false, error} -- except a stale one (`blockers.STALE`:
    the plan moved, there is no question open any more, or the headline no longer stands in the way), which answers
    `ok: true` with the state as it stands now, so the page can redraw instead of reading a dead end."""
    action = body.get("action")
    if action not in BLOCKER_ACTIONS:
        return JSONResponse(BAD_REQUEST, status_code=400, headers=NO_STORE)
    settings = config.load()
    ip = sessions.request_ip(request)
    with db.connect(settings.database_url) as conn:
        biz, session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        sessions.check_csrf(session, body.get("csrf"))
        key = request.cookies[VISIT_COOKIE]
        listed = (launch_check.check(conn, settings, biz["id"]) or {}).get("items") or []
        found = next((i for i in listed if i["id"] == body.get("id")), None)
        if found is None and action == "fix_headline":  # a page that named no item: the headline's own (B0's body)
            found = next((i for i in listed if any(a["id"] == "fix_headline" for a in i["actions"])), None)
    try:
        say, more = _fix(action, settings, biz["id"], found, body, key, ip, session["email"])
    except Limited as e:
        return _too_many(e.retry_after)
    except ToolError as e:
        if str(e) in blockers.STALE:  # the page can still redraw: what stands now, not a dead end
            with db.connect(settings.database_url) as conn:
                return JSONResponse(
                    {"ok": True, "result": _resolved(conn, request, settings, str(e))}, headers=NO_STORE
                )
        return JSONResponse({"ok": False, "error": str(e)}, headers=NO_STORE)
    with db.connect(settings.database_url) as conn:
        return JSONResponse({"ok": True, "result": _resolved(conn, request, settings, say, **more)}, headers=NO_STORE)


@router.get("/api/launch_check")
def api_launch_check(request: Request):
    """The plan card's checklist (L1, spec 2026-10-10 §4.2): every reason the latest draft cannot launch yet, each with
    its suggestion and its fix, for the signed-in owner of a claimed business (`_page_gate`: a draft key gets the
    wall, another tab's business the reload); `check` is null once the latest plan is no draft (in review,
    approved). Database only: the card's spinner is this request's own time."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        biz, _session, err = _page_gate(conn, request, settings)
        if err is not None:
            return err
        return JSONResponse({"ok": True, "check": launch_check.check(conn, settings, biz["id"])}, headers=NO_STORE)


def _fix(action: str, settings, biz_id: str, found: dict | None, body: dict, key: str, ip: str, email: str):
    """One of /api/blocker's actions: (Admiry's sentence for what was done, what the answer adds). ToolError with a
    refusal; Limited at a rate limit. The ones on a plan act on the version the message named (`blockers.draft_named`):
    a plan that changed meanwhile is refused, and nothing is written. An `answer` names the question it answers (`id`)
    and the plan it was asked on (`plan_id`): sent for another question or another plan than the one open -- another
    tab's message, another business's page -- it is no answer at all (spec §3.3; P10; final review C1). The composer's
    words need neither: they are read against the question open now (`turn`'s `say`)."""
    if action == "answer":
        asked = found is not None and found["kind"] == "ask"
        if not asked or body.get("id") != found["id"] or body.get("plan_id") != found["plan_id"]:
            raise ToolError(blockers.NOTHING_ASKED)
        value = blockers.read_answer(found["id"], body.get("text"))
        if value is None:
            raise ToolError(blockers.SHAPES[found["id"]])
        return _saved(settings, biz_id, found, value, key, ip, body.get("idempotency_key")), {}
    if action == "check_page":  # the page on file, whoever hosts it (/api/page/check checks no Admiry page)
        env = _ran(call("check_landing_page", {"url": None}, key, ip=ip))
        report = env["data"].get("check") or {}
        return (blockers.CHECKED_OK if report.get("ok") else env["say"]), {}
    if action == "fix_headline":
        return _fix_headline(settings, biz_id, found, body, key, ip)
    if action == "write_copy":
        blockers.write_copy(settings, biz_id, body.get("plan_id"), email=email, ip=ip)
        return blockers.WROTE_COPY, {}
    with db.connect(settings.database_url) as conn:
        plan = blockers.draft_named(conn, biz_id, body.get("plan_id"))
    key_in = body.get("idempotency_key")
    ik = key_in if isinstance(key_in, str) and key_in else db.new_id("ik")
    args = {"plan_id": plan["id"], "instruction": blockers.FRESH_INSTRUCTION, "idempotency_key": ik}
    _ran(call("revise_plan", args, key, ip=ip))
    return blockers.FRESH_DRAWN, {}


def _fix_headline(settings, biz_id: str, found: dict | None, body: dict, key: str, ip: str) -> tuple[str, dict]:
    """ "Fix the headline" (B0 P6; spec §8.2: the headline alone): blockers.run_headline_fix under the visitor's key
    -- its site built again with every other word kept (tools_sites.fix_headline), its starter page's headline
    changed (update_page) -- as a preview; publishing it is the owner's next tap (spec §8.1: dangerous, always
    confirmed: `publish` in the answer). Refused when the headline of Admiry's page is no longer what stops the
    launch. The same body the public update_landing_page's "fix_headline" action runs (follow-up 2026-10-12;
    agent/surface/supporting.py) -- only how `site` and `starter` are called (this visitor's key, not the
    assistant's own) differs."""

    def run_site(h: str) -> dict:
        with tools_sites.on_the_card():
            return _ran(_call_fn(tools_sites.fix_headline, {"headline": h}, key, ip=ip))

    with hosted.with_embed_origin(_web_origin(settings)):
        headline, env = blockers.run_headline_fix(
            settings,
            biz_id,
            found,
            body.get("plan_id"),
            site=run_site,
            starter=lambda h: _ran(call("update_page", {"section": "headline", "text": h}, key, ip=ip)),
        )
    token = env["data"].get("token")
    if not token:  # the site asked first for a fact it lacks (build_site's question): said as it is
        raise ToolError(env["say"])
    return blockers.HEADLINE_READY.format(headline=headline), {"publish": {"token": token}}
