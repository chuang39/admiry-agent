"""The consent page of the OAuth flow (spec 2026-09-26 §6): Google sign-in (the existing web session), the
assistant's name, the scopes in owner words, which business the assistant may act on, Allow / Deny."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.routing import APIRoute
from mcp.server.auth.provider import construct_redirect_uri

from . import audit, claims, config, db, home_api, oauth, sessions, support_mail, web
from .templating import env as _env

NO_STORE = {"Cache-Control": "no-store"}
# The consent page must never be framed (clickjacking the Allow button): every /oauth/consent response has these.
NO_FRAME = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"}
EXPIRED = "This sign-in link has expired. Go back to your assistant and connect again."
FIXED_SCOPES = frozenset({"read", "manage"})  # shown ticked and disabled — granted whenever they were requested


class _NoFrameRoute(APIRoute):
    """Adds NO_FRAME to every response of this router: the page, a notice, a redirect, and the error an
    HTTPException becomes (re-raised with the headers, since exception handlers build that response)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def no_frame(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as e:
                raise HTTPException(e.status_code, e.detail, headers={**(e.headers or {}), **NO_FRAME}) from None
            response.headers.update(NO_FRAME)
            return response

        return no_frame


router = APIRouter(route_class=_NoFrameRoute)


def _sign_in_redirect(txn: str) -> RedirectResponse:
    return RedirectResponse(
        f"/connect/google/start?next={quote(f'/oauth/consent?txn={txn}', safe='/')}", status_code=303
    )


def _requested(txn: dict) -> list[str]:
    """The scopes the client asked for at /authorize, in SCOPES order (DEFAULT_SCOPES when it named none)."""
    asked = set(txn["params"].get("scopes") or oauth.DEFAULT_SCOPES) & set(oauth.SCOPES)
    return [s for s in oauth.SCOPES if s in asked] or list(oauth.DEFAULT_SCOPES)


def _draft_key_business(conn, key_hash: str | None) -> str | None:
    """The business a live draft key's hash belongs to, or None (no cookie, unknown hash, revoked,
    expired, or a key that is no longer class 'draft' — e.g. claims.claim already upgraded it). The
    visitor's second cookie (home_api.BIZ_COOKIE) holds this hash, never the business id or the key
    itself: a cookie only useful while the underlying key is still a live draft proves more than a bare
    id would (final review C1/cheap item 1)."""
    if not key_hash:
        return None
    row = conn.execute(
        "SELECT business_id FROM api_key WHERE key_hash = %s AND class = 'draft' AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now())",
        (key_hash,),
    ).fetchone()
    return row["business_id"] if row else None


def _website_draft(conn, request: Request) -> str | None:
    """The visitor's own draft business, from the second visitor cookie (home_api.BIZ_COOKIE, Path=/):
    this page cannot read `admiry_visit` itself (Path=/api, spec §3 D41 R2). None unless that business
    is still an unclaimed draft right now — the POST re-checks the same two conditions before claiming
    it, since a cookie this old may name a business that was since claimed by someone else."""
    biz_id = _draft_key_business(conn, request.cookies.get(home_api.BIZ_COOKIE))
    if biz_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM business WHERE id = %s AND claim_state = 'draft' AND status = 'active'", (biz_id,)
    ).fetchone()
    return row["id"] if row else None


def _render(session: dict, txn: dict, conn, request: Request) -> HTMLResponse:
    html = _env.get_template("oauth_consent.html").render(
        assistant=oauth.client_name(conn, txn["client_id"]),
        return_host=oauth.redirect_host(str(txn["params"].get("redirect_uri") or "")),
        txn=txn["id"],
        csrf=session["csrf"],
        email=session["email"],
        scopes=[
            {"id": s, "words": oauth.SCOPE_WORDS[s], "fixed": s in FIXED_SCOPES, "checked": s != "account"}
            for s in _requested(txn)
        ],
        businesses=web._businesses(conn, session["email"]),
        website_draft_id=_website_draft(conn, request),
    )
    return HTMLResponse(html, headers=NO_STORE)


@router.get("/oauth/consent", response_class=HTMLResponse)
def consent_page(request: Request, txn: str = ""):
    with db.connect(config.load().database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return _sign_in_redirect(txn)
        row = oauth.peek_txn(conn, txn)
        if row is None:
            return web.notice(EXPIRED, status=410, back="/connect")
        return _render(session, row, conn, request)


@router.post("/oauth/consent")
def consent_decide(
    request: Request,
    txn: str = Form(""),
    csrf: str | None = Form(None),
    business_id: str = Form("new", alias="business"),
    scopes: list[str] = Form([]),
    decision: str = Form("deny"),
):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return _sign_in_redirect(txn)
        sessions.check_csrf(session, csrf)  # 403 on mismatch
        try:
            with conn.transaction():
                row = oauth.take_txn(conn, txn)
                if row is None:
                    return web.notice(EXPIRED, status=410, back="/connect")
                p = row["params"]
                if decision != "allow":
                    return RedirectResponse(
                        construct_redirect_uri(str(p["redirect_uri"]), error="access_denied", state=p.get("state")),
                        status_code=303,
                    )
                email = session["email"]
                if business_id == "new":  # the signer's blank business if there is one (final review I3)
                    biz_id, _created = web.blank_or_new_business(conn, email)
                elif business_id.startswith("website:"):
                    # Task 3 (spec §3 D41 R2): the plan the visitor started on the website. Possession of
                    # the cookie's key hash alone cannot claim someone else's business, so both are
                    # re-checked here (the GET's own check may be stale by the time the owner clicks
                    # Allow): the cookie's key must still be a live draft key of exactly this business,
                    # and the business (locked FOR UPDATE, so nothing else can claim it out from under
                    # this check) must still be an unclaimed, active draft — a draft is by definition
                    # unowned, so claims.claim below binds it to the signer with no other proof needed.
                    draft_id = business_id.removeprefix("website:")
                    draft_row = conn.execute(
                        "SELECT claim_state, status FROM business WHERE id = %s FOR UPDATE", (draft_id,)
                    ).fetchone()
                    if (
                        _draft_key_business(conn, request.cookies.get(home_api.BIZ_COOKIE)) != draft_id
                        or draft_row is None
                        or draft_row["claim_state"] != "draft"
                        or draft_row["status"] != "active"
                    ):
                        raise HTTPException(status_code=403, detail="this plan can no longer be claimed")
                    try:
                        claims.claim(conn, draft_id, email)
                    except claims.ClaimRefused as e:
                        raise HTTPException(status_code=403, detail=str(e)) from e
                    biz_id = draft_id
                else:
                    owned = conn.execute(
                        "SELECT id FROM business WHERE id = %s AND owner_email = %s AND status = 'active'",
                        (business_id, email),
                    ).fetchone()
                    if owned is None:
                        raise HTTPException(status_code=403, detail="not your business")
                    biz_id = business_id
                # The fixed scopes are granted only when the client asked for them (final review I1): a client
                # that asked for `read` alone never gets `manage`, which the page never showed.
                granted = [s for s in _requested(row) if s in FIXED_SCOPES or s in scopes]
                _code, redirect = oauth.mint_code(conn, txn=row, business_id=biz_id, owner_email=email, scopes=granted)
                audit.record_web(
                    conn,
                    tool="web.oauth_grant",
                    email=email,
                    ip=sessions.request_ip(request),
                    business_id=biz_id,
                    args={"assistant": oauth.client_name(conn, row["client_id"]), "scopes": granted},
                    result={"granted": True},
                )
        except web.NewBusinessBusy:  # rolled back: the transaction is still there to allow again
            return web.notice(web.BUSY_TEXT, status=409, back=f"/oauth/consent?txn={quote(txn)}")
        support_mail.flush(business_id=biz_id)  # the acknowledgement a waiting ticket was holding back
        return RedirectResponse(redirect, status_code=303)
