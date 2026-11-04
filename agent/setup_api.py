"""POST /api/setup — the home's ads setup cards (spec 2026-09-29 setup-in-conversation §3). The signed-in owner's web
session; CSRF on every action but `state`; the business the page names (`/?setup=<id>`), or the visitor's own from
the home's cookie (Launch under a plan); then setup.py. Answers carry an owner sentence, never a page."""

from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from . import config, db, sessions, setup
from .web import NO_STORE

router = APIRouter()
ACTIONS = ("state", "choose", "accept", "link", "check", "pay", "approve")
SIGN_IN = {"ok": False, "signin": True, "error": "Sign in first."}
GONE = {"ok": False, "gone": True, "error": "There is nothing to set up here."}


def _business_id(conn, request: Request, session: dict, wanted: object) -> str | None:
    """The business to set up, when the signed-in owner owns it; None otherwise (never says whether it exists)."""
    if wanted is None:
        from .home_api import _visitor  # home_api imports the tool registry: not at import time

        _rec, biz = _visitor(conn, request)
        wanted = biz["id"] if biz else None
    if not isinstance(wanted, str):
        return None
    row = conn.execute(
        "SELECT id FROM business WHERE id = %s AND owner_email = %s AND status = 'active'",
        (wanted, session["email"]),
    ).fetchone()
    return row["id"] if row else None


@router.post("/api/setup")
def api_setup(request: Request, body: dict = Body(...)):
    action = body.get("action")
    if action not in ACTIONS:
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400, headers=NO_STORE)
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return JSONResponse(SIGN_IN, status_code=401, headers=NO_STORE)
        if action != "state":
            csrf = body.get("csrf")
            sessions.check_csrf(session, csrf if isinstance(csrf, str) else None)  # a non-string is simply wrong
        biz = _business_id(conn, request, session, body.get("business_id"))
        if biz is None:
            return JSONResponse(GONE, status_code=404, headers=NO_STORE)
        if action == "state":
            return JSONResponse({"ok": True, "state": setup.state(conn, settings, biz)}, headers=NO_STORE)
    owner = setup.Owner(email=session["email"], ip=sessions.request_ip(request))
    try:
        if action == "choose":
            out = {"state": setup.choose(settings, owner, biz, body.get("mode"))}
        elif action == "accept":
            out = {"state": setup.accept(settings, owner, biz, body.get("documents"))}
        elif action == "link":
            out = {"state": setup.link(settings, owner, biz, body.get("customer_id"))}
        elif action == "check":
            out = {"state": setup.check(settings, owner, biz)}
        elif action == "approve":
            out = setup.approve(settings, owner, biz, body.get("plan_id"))
        else:
            out = {
                "url": setup.pay(
                    settings, owner, biz, body.get("amount_usd"), body.get("rail") or "card", body.get("plan_id")
                )
            }
    except setup.Refused as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status, headers=NO_STORE)
    return JSONResponse({"ok": True, **out}, headers=NO_STORE)
