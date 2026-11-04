"""The web step of the e-mail channel (spec 2026-09-28 §3.2, §6.4, D55, D56) and the owner's addresses on
/connect (§4). GET /m/{token} asks for Google sign-in, then shows one question — allow the address that wrote? —
and one button; POST claims the draft for the signer (any Google account, D56), builds the plan, records the
grant, spends the link, writes back into the thread and sends the owner to /plan. /connect/addresses adds and
revokes grants (CSRF, the owner only). Every /m page refuses framing."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import (
    audit,
    business,
    claims,
    config,
    consult,
    db,
    mail_dispatch,
    mail_grants,
    mail_out,
    plans,
    sessions,
    support_mail,
    web,
)
from .templating import env as _env

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}
NO_FRAME = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"}
LINK_DEAD = "This link has expired or was already used. Reply to Admiry's e-mail and it will send a new one."
PLAN_FAILED = "Could not build the plan right now; nothing changed. Open the link again in a minute."


def _sign_in(token: str) -> RedirectResponse:
    return RedirectResponse(f"/connect/google/start?next={quote(f'/m/{token}', safe='/')}", status_code=303)


def _name(conn, biz_id: str) -> str:
    latest = business.latest_knowledge(conn, biz_id)
    return plans.name_for(latest[1] if latest else None) or "your business"


@router.get("/m/{token}", response_class=HTMLResponse)
def link_page(request: Request, token: str):
    settings = config.load()
    if not settings.email_inbound:  # like the webhook: the capability does not exist when the channel is off (I1)
        raise HTTPException(status_code=404, detail="not found")
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return _sign_in(token)
        link = mail_grants.peek_link(conn, token)
        if link is None:
            used = mail_grants.used_link(conn, token)
            if used:
                biz = business.row(conn, used["business_id"])
                latest = plans.latest(conn, used["business_id"])
                if biz and biz["owner_email"] == session["email"] and latest:
                    return RedirectResponse(f"/plan/{latest['id']}", status_code=303, headers=NO_STORE)
            return web.notice(LINK_DEAD, status=404)
        biz = business.row(conn, link["business_id"])
        if biz is None or biz["status"] != "active":
            return web.notice(LINK_DEAD, status=404)
        html = _env.get_template("mail_link.html").render(
            csrf=session["csrf"],
            token=token,
            name=_name(conn, biz["id"]),
            email=session["email"],
            address=link["counterpart"],
            ask=link["counterpart"] != session["email"],
            scopes=[{"id": s, "words": mail_grants.SCOPE_WORDS[s], "fixed": s == "read"} for s in mail_grants.SCOPES],
        )
    return HTMLResponse(html, headers={**NO_STORE, **NO_FRAME})


@router.post("/m/{token}")
def link_continue(
    request: Request,
    token: str,
    csrf: str | None = Form(None),
    allow: str | None = Form(None),
    scopes: list[str] = Form([]),
):
    settings = config.load()
    if not settings.email_inbound:  # like the webhook: the capability does not exist when the channel is off (I1)
        raise HTTPException(status_code=404, detail="not found")
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return _sign_in(token)
        sessions.check_csrf(session, csrf)
        link = mail_grants.peek_link(conn, token)
        if link is None:
            return web.notice(LINK_DEAD, status=410)
        biz = business.row(conn, link["business_id"])
        if biz is None or biz["status"] != "active":
            return web.notice(LINK_DEAD, status=410)
        biz_id, email, ip = link["business_id"], session["email"], sessions.request_ip(request)
        try:
            with conn.transaction():
                claims.claim(conn, biz_id, email)  # D56: any Google account may own the business
        except claims.ClaimRefused as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        support_mail.flush(business_id=biz_id)  # the acknowledgement a waiting ticket was holding back
        thread = dict(conn.execute("SELECT * FROM email_thread WHERE id = %s", (link["thread_id"],)).fetchone())
        s = consult.get(conn, biz_id, link["session_id"]) if link.get("session_id") else None
        # the plan: recommend on a ready session (the model runs here, outside any transaction), else the plan
        # that already exists (a repeat of the step)
        with mail_dispatch.message_key(
            conn, biz_id, draft=False, scopes=mail_grants.DEFAULT_SCOPES, grant_id=None, row_id=None
        ) as key:
            if s and s["status"] == "ready":
                env = mail_dispatch.run_tool(
                    "recommend", {"session_id": s["id"], "idempotency_key": f"link:{link['token_hash']}"}, key
                )
            else:
                env = mail_dispatch.run_tool("get_plan", {}, key)
        if "error" in env:
            return web.notice(PLAN_FAILED, status=502, back=f"/m/{quote(token)}", back_text="Try again")
        latest = plans.latest(conn, biz_id)
        with conn.transaction():
            # use_link first: its UPDATE's row lock is what serializes a double-click or two tabs racing this
            # token: the loser sees False here and skips the grant, the audit row and the write-back mail below.
            won = mail_grants.use_link(conn, link["token_hash"])
            if won:
                granted = None
                if link["counterpart"] != email and allow == "yes":
                    granted = mail_grants.add(
                        conn, biz_id, link["counterpart"], scopes=scopes, via="web_step", created_by=email
                    )["scopes"]
                audit.record_web(
                    conn,
                    tool="web.mail_link",
                    email=email,
                    ip=ip,
                    business_id=biz_id,
                    args={"address": link["counterpart"], "scopes": granted},
                    result={"plan_id": latest["id"]},
                )
        # the write-back only reaches an address the owner actually authorized: the signer's own address needs no
        # grant, but a *different* counterpart (an assistant's inbox, say) must have just been granted -- a
        # declined "allow" (granted stays None) gets nothing at all, not even the plan link (finding I2).
        if won and (link["counterpart"] == email or granted is not None):
            mail_out.deliver(
                conn,
                settings,
                thread=thread,
                to=link["counterpart"],
                paragraphs=[mail_dispatch.SIGNED_IN.format(email=email), env["say"]],
                links=[("Your plan", plans.plan_url(latest["id"]))],
            )
    return RedirectResponse(f"/plan/{latest['id']}", status_code=303, headers=NO_STORE)


@router.post("/connect/addresses")
def add_address(
    request: Request,
    business_id: str = Form(...),
    address: str = Form(""),
    manage: str | None = Form(None),
    leads: str | None = Form(None),
    csrf: str | None = Form(None),
):
    settings = config.load()
    if not settings.email_inbound:  # like the webhook: the capability does not exist when the channel is off (I1)
        raise HTTPException(status_code=404, detail="not found")
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        biz = web.owned_business(conn, session, business_id)
        if not mail_grants.valid(address):
            return web.notice("That is not an e-mail address.", status=400)
        chosen = ["read", *(["manage"] if manage else []), *(["leads"] if leads else [])]
        with conn.transaction():
            g = mail_grants.add(conn, biz["id"], address, scopes=chosen, via="connect", created_by=session["email"])
            audit.record_web(
                conn,
                tool="web.mail_grant",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"address": g["address"], "scopes": g["scopes"]},
                result={"grant_id": g["id"]},
            )
    return RedirectResponse(f"/connect?b={biz['id']}#email", status_code=303, headers=NO_STORE)


@router.post("/connect/addresses/{grant_id}/revoke")
def revoke_address(request: Request, grant_id: str, csrf: str | None = Form(None)):
    settings = config.load()
    if not settings.email_inbound:  # like the webhook: the capability does not exist when the channel is off (I1)
        raise HTTPException(status_code=404, detail="not found")
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        row = conn.execute("SELECT business_id FROM email_grant WHERE id = %s", (grant_id,)).fetchone()
        biz = web.owned_business(conn, session, row["business_id"] if row else "")
        with conn.transaction():
            done = mail_grants.revoke(conn, grant_id, biz["id"])
            audit.record_web(
                conn,
                tool="web.mail_grant_revoke",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"grant_id": grant_id},
                result={"revoked": done},
            )
    return RedirectResponse(f"/connect?b={biz['id']}#email", status_code=303, headers=NO_STORE)
