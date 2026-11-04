"""The owner's controls over the daily digest (digest spec 2026-09-30 §9; plan 2026-10-07-agent-k2 Task 12, Dg Task
9). The mail's links: GET /unsubscribe/{token} never changes anything (a mail scanner opens links); the page's button
and the RFC 8058 one-click POST only ever lower the frequency, for the business the token names. Settings' Reports
card (POST /connect/digest) and its resume after a bounce (POST /connect/digest/resume). GET /api/report: one sent
report, and the day's look when its owner may read it, for the homepage's `?report=` turn. POST /hooks/resend-events
(Task 13, Dg P12): Resend's bounce and complaint events about our own mail."""

from __future__ import annotations

import json
import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import audit, config, db, digest, digest_links, mail_auth, mail_out, sessions, setup_api, web
from .roles import analyst

log = logging.getLogger(__name__)
router = APIRouter()

FREQUENCY_WORDS = {"daily": "daily reports", "weekly": "weekly reports (Mondays)", "off": "no reports"}
LINK_DEAD = "This link no longer works; change your reports in Settings."
DONE = "Done: {business} now gets {words}."
RANK = {"daily": 2, "weekly": 1, "off": 0}
BUTTONS = {"weekly": "Send weekly reports only", "off": "Stop the reports"}
NO_STORE = {"Cache-Control": "no-store"}
NO_FRAME = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"}


def lower(current: str, requested: str) -> str:
    """The lower of two frequencies: a link never raises what the owner chose."""
    return requested if RANK.get(requested, 0) < RANK.get(current, 0) else current


def _dead() -> HTMLResponse:
    out = web.notice(LINK_DEAD, status=404, back="/connect", back_text="Settings")
    out.headers.update(NO_FRAME)
    return out


def _valid(settings, b: str, token: str) -> bool:
    if not settings.digest_link_secret:
        log.warning("digest: an unsubscribe link was opened while AGENT_DIGEST_LINK_SECRET is unset")
        return False
    return digest_links.verify(settings, b, token)


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str, b: str = "", f: str = "off") -> HTMLResponse:
    """The link's page: names the business and the change, with one button. Changes nothing (HEAD answers alike)."""
    settings = config.load()
    if f not in BUTTONS or not _valid(settings, b, token):
        return _dead()
    with db.connect(settings.database_url) as conn:
        name = digest._name(conn, b) if conn.execute("SELECT 1 FROM business WHERE id = %s", (b,)).fetchone() else None
    if name is None:
        return _dead()
    out = web.page(
        "digest_unsubscribe.html",
        business=name[:1].upper() + name[1:],
        words=FREQUENCY_WORDS[f],
        f=f,
        button=BUTTONS[f],
        action=f"/unsubscribe/{token}?b={b}",
    )
    out.headers.update(NO_FRAME)
    return out


def _apply(settings, b: str, requested: str) -> tuple[str, str] | None:
    """(the business's name, its frequency now), the lower one written and audited; None for no such business."""
    with db.connect(settings.database_url) as conn, conn.transaction():
        row = conn.execute("SELECT digest_frequency FROM business WHERE id = %s FOR UPDATE", (b,)).fetchone()
        if row is None:
            return None
        new = lower(row["digest_frequency"], requested)
        conn.execute("UPDATE business SET digest_frequency = %s WHERE id = %s", (new, b))
        audit.record_system(
            conn,
            tool="system.digest_unsubscribe",
            actor="unsubscribe link",
            business_id=b,
            args={"f": requested},
            result={"note": FREQUENCY_WORDS[new]},
        )
        return digest._name(conn, b), new


@router.post("/unsubscribe/{token}")
async def unsubscribe(request: Request, token: str, b: str = "") -> Response:
    """The page's button (a form with `f`) or a mail client's one-click POST (`List-Unsubscribe=One-Click`, RFC 8058:
    reports off). No CSRF: the token is the capability, and the change only ever lowers."""
    settings = config.load()
    form = await request.form()
    one_click = form.get("List-Unsubscribe") == "One-Click"
    f = form.get("f")
    requested = "off" if one_click or f not in BUTTONS else str(f)
    if not _valid(settings, b, token):
        return _dead()
    done = await run_in_threadpool(_apply, settings, b, requested)
    if done is None:
        return _dead()
    if one_click:
        return PlainTextResponse("ok", status_code=200, headers=NO_STORE)
    name, new = done
    out = web.notice(DONE.format(business=name[:1].upper() + name[1:], words=FREQUENCY_WORDS[new]), back="/connect")
    out.headers.update(NO_FRAME)
    return out


def _owner(conn, request: Request, csrf: str | None, business_id: str) -> tuple[dict, dict]:
    session = sessions.of_request(conn, request)
    sessions.check_csrf(session, csrf)
    return session, web.owned_business(conn, session, business_id)


@router.post("/connect/digest")
def set_reports(
    request: Request,
    business_id: str = Form(...),
    frequency: str = Form(...),
    csrf: str | None = Form(None),
):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session, biz = _owner(conn, request, csrf, business_id)
        if frequency not in digest_links.FREQUENCIES:
            return web.notice("Pick daily, weekly or off.", status=400)
        with conn.transaction():
            conn.execute("UPDATE business SET digest_frequency = %s WHERE id = %s", (frequency, biz["id"]))
            audit.record_web(
                conn,
                tool="web.digest_frequency",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"frequency": frequency},
                result={"note": FREQUENCY_WORDS[frequency]},
            )
    return RedirectResponse(f"/connect?b={biz['id']}#reports", status_code=303, headers=NO_STORE)


@router.post("/connect/digest/resume")
def resume_email(request: Request, business_id: str = Form(...), csrf: str | None = Form(None)):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session, biz = _owner(conn, request, csrf, business_id)
        with conn.transaction():
            conn.execute("UPDATE business SET notify_email_status = 'ok' WHERE id = %s", (biz["id"],))
            audit.record_web(
                conn,
                tool="web.email_status",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={},
                result={},
            )
    return RedirectResponse(f"/connect?b={biz['id']}#reports", status_code=303, headers=NO_STORE)


@router.get("/api/report")
def api_report(request: Request, b: str = "", day: str | None = None) -> JSONResponse:
    """One sent report (an owner's mail, never an operator's preview) and the day's look when its owner may read it:
    401 signed out, 404 for a business that is not the signer's or a day with no report."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return JSONResponse({"ok": False, "signin": True}, status_code=401, headers=NO_STORE)
        biz = setup_api._business_id(conn, request, session, b or None)
        try:
            wanted = date.fromisoformat(day) if day else None
        except ValueError:
            wanted = None
        row = digest.latest_sent(conn, biz, day=wanted) if biz else None
        if row is None:
            return JSONResponse({"ok": False, "gone": True}, status_code=404, headers=NO_STORE)
        look = conn.execute("SELECT * FROM insight WHERE business_id = %s AND day = %s", (biz, row["day"])).fetchone()
        view = analyst.owner_view(settings, look)
    return JSONResponse(
        {"ok": True, "report": row["body"], "day": row["day"].isoformat(), "insight": view}, headers=NO_STORE
    )


# ---------------------------------------------------------------- Resend's events (Task 13)


@router.post("/hooks/resend-events")
async def resend_events(request: Request) -> JSONResponse:
    """Resend's bounce and complaint events (Svix-signed; RESEND_EVENTS_WEBHOOK_SECRET unset = 404). The raw body is
    verified here; the work runs in the thread pool."""
    settings = config.load()
    if not settings.resend_events_webhook_secret:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        mail_auth.verify_svix(payload, headers, settings.resend_events_webhook_secret)
        event = json.loads(payload)
    except (mail_auth.BadSignature, ValueError):
        return JSONResponse({"error": "bad signature"}, status_code=400)
    if not isinstance(event, dict):
        return JSONResponse({"error": "bad event"}, status_code=400)
    out = await run_in_threadpool(_apply_event, settings, event, headers.get("svix-id") or "")
    return JSONResponse({"received": True, **out})


def _apply_event(settings, event: dict, event_id: str) -> dict:
    with db.connect(settings.database_url) as conn:
        return apply_event(conn, settings, event, event_id)


def _operator(settings, event_id: str, subject: str, line: str) -> None:
    domain = mail_out.split_address(settings.email_address)[1]
    text, html = mail_out.compose([line])
    why = mail_out.send_operator(
        settings,
        subject=subject,
        text=text,
        html=html,
        message_id=f"<evt-{event_id}@{domain}>",
        idempotency_key=f"evt-{event_id}",
    )
    if why:
        log.warning("resend event %s: the operator notice was not sent: %s", event_id, why)


def apply_event(conn, settings, event: dict, event_id: str) -> dict:
    """{"action": bounced | complaint | notice | ignored}. Only mail from this environment's own address counts (the
    other box's and the website's share the Resend team); a permanent bounce (never Resend's own suppression) marks
    every business of the address bounced -- lead notices and digests stop -- and tells the operator; a complaint
    about a digest turns that address's reports off; a complaint about any other mail is the operator's. An ignored
    event's log line names its type and id, never an address."""
    kind = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    ours = mail_out.split_address(settings.email_address)[1]
    if mail_auth.domain_of(mail_auth.sender({"from": data.get("from")})) != ours:
        log.info("resend event %s (%s) ignored: not this environment's mail", event_id, kind)
        return {"action": "ignored"}
    recipients = [a.strip().lower() for a in data.get("to") or [] if isinstance(a, str) and a.strip()]
    bounce = data.get("bounce") if isinstance(data.get("bounce"), dict) else {}
    if kind == "email.bounced" and bounce.get("type") == "Permanent" and bounce.get("subType") != "Suppressed":
        for address in recipients:
            with conn.transaction():
                hit = conn.execute(
                    "UPDATE business SET notify_email_status = 'bounced' "
                    "WHERE lower(owner_email) = %s AND notify_email_status = 'ok' RETURNING id",
                    (address,),
                ).fetchall()
                for row in hit:
                    audit.record_system(
                        conn,
                        tool="system.email_bounced",
                        actor="resend",
                        business_id=row["id"],
                        args={"type": bounce.get("type"), "subtype": bounce.get("subType"), "event": event_id},
                        result={},
                    )
            if hit:
                _operator(
                    settings,
                    event_id,
                    f"[Admiry] e-mails to {address} bounced",
                    f"Resend reported a permanent bounce for {address}: lead alerts and reports to "
                    f"{len(hit)} business(es) are paused until the owner resumes them in Settings.",
                )
        return {"action": "bounced"}
    if kind == "email.complained":
        sent = conn.execute(
            "SELECT intent FROM email_message WHERE provider_id = %s", (data.get("email_id"),)
        ).fetchone()
        if sent and (sent["intent"] or {}).get("kind") == "digest":
            with conn.transaction():
                hit = conn.execute(
                    "UPDATE business SET digest_frequency = 'off' WHERE lower(owner_email) = ANY(%s) RETURNING id",
                    (recipients,),
                ).fetchall()
                for row in hit:
                    audit.record_system(
                        conn,
                        tool="system.digest_complaint",
                        actor="resend",
                        business_id=row["id"],
                        args={"event": event_id},
                        result={},
                    )
            return {"action": "complaint"}
        _operator(
            settings,
            event_id,
            "[Admiry] an e-mail was marked as spam",
            f"Resend reported a spam complaint about one of Admiry's e-mails (event {event_id}).",
        )
        return {"action": "notice"}
    log.info("resend event %s (%s) ignored", event_id, kind)
    return {"action": "ignored"}
