"""The admin pages (L1, spec 2026-10-10 §7; the Help button, spec 2026-10-12 §4): /ops/review, the queue of first
launches in review, and /ops/review/{id}, one review in full with its three decisions; /ops/tasks, the support
tickets by tab, and /ops/tasks/{id}, one ticket with a reply by e-mail and a close -- for an e-mail in
AGENT_ADMIN_EMAILS signed in with Google (sessions.py), on every box (prod's queues are prod's). Everyone else, signed
in or not, gets a 404, and so does everyone while the list is empty; an anonymous visitor gets the sign-in page, as
/ops/eval. The pages read and act through ops_review and ops_tasks, which the admin MCP shares; a refused action is
said on the page (409) and changes nothing; /ops/rules, the refusal rules (spec 2026-10-12 §2.5: proposed, active,
add one, the daily summaries)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastmcp.exceptions import ToolError

from . import config, db, ops_review, ops_rules, ops_tasks, sessions, support_mail, web
from .web import NO_STORE

router = APIRouter()
REVIEW_ID = re.compile(r"lr_[0-9a-f]{16}")
TICKET_ID = re.compile(r"tk_[0-9a-f]{16}")  # db.new_id("tk")
ACTIONS = {"approve": None, "return": "returned", "reject": "rejected"}
TASK_ACTIONS = ("reply", "reply_close", "close")
RULE_ID = re.compile(r"rr_[0-9a-f]{16}")  # db.new_id("rr")
RULE_ACTIONS = (*ops_rules.ACTIONS, "add")


def _admin(conn, request: Request, settings: config.Settings) -> dict | None:
    """The signed-in session of a listed admin, or None for an anonymous visitor; a 404 with no one listed, and for a
    signed-in e-mail that is not listed (compared lower-cased, as AGENT_ADMIN_EMAILS is read)."""
    if not settings.admin_emails:
        raise HTTPException(status_code=404)
    session = sessions.of_request(conn, request)
    if session is not None and session["email"].strip().lower() not in settings.admin_emails:
        raise HTTPException(status_code=404)
    return session


def _page(template: str, *, status: int = 200, **ctx) -> HTMLResponse:
    resp = web.page(template, status=status, **ctx)
    resp.headers["Content-Security-Policy"] = web.CONNECT_CSP
    return resp


def _signin(next_path: str, settings: config.Settings) -> HTMLResponse:
    return web.page(
        "signin.html",
        next=sessions.safe_next(next_path),
        configured=bool(settings.google_client_id),
        help=False,  # the admin sign-in screen (spec 2026-10-12 §3.1 excludes /ops/*; Task 3 review)
    )


@router.get("/ops/review", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None:
            return _signin("/ops/review", settings)
        return _page("ops_review_index.html", queue=ops_review.queue_view(conn))


@router.get("/ops/review/{review_id}", response_class=HTMLResponse)
def show(request: Request, review_id: str, done: str | None = None) -> HTMLResponse:
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None:
            return _signin(f"/ops/review/{review_id}", settings)
        found = ops_review.detail(conn, review_id) if REVIEW_ID.fullmatch(review_id) else None
        if found is None:
            raise HTTPException(status_code=404)
        return _page("ops_review.html", d=found, csrf=who["csrf"], done=done if done in ACTIONS else None, notice=None)


@router.post("/ops/review/{review_id}")
def decide(
    request: Request,
    review_id: str,
    action: str = Form(""),
    note: str | None = Form(None),
    csrf: str | None = Form(None),
):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None or not REVIEW_ID.fullmatch(review_id):
            raise HTTPException(status_code=404)
        sessions.check_csrf(who, csrf)
        if action not in ACTIONS:
            raise HTTPException(status_code=400)
        admin, ip = who["email"].strip().lower(), sessions.request_ip(request)
        try:
            if action == "approve":
                ops_review.approve(conn, settings, review_id, admin=admin, ip=ip)
            else:
                ops_review.send_back(conn, settings, review_id, admin=admin, note=note, state=ACTIONS[action], ip=ip)
        except ToolError as e:
            found = ops_review.detail(conn, review_id)
            if found is None:
                raise HTTPException(status_code=404) from None
            return _page("ops_review.html", status=409, d=found, csrf=who["csrf"], done=None, notice=str(e))
    return RedirectResponse(f"/ops/review/{review_id}?done={action}", status_code=303, headers=NO_STORE)


# ---------------------------------------------------------------- /ops/tasks (spec 2026-10-12 §4)


@router.get("/ops/tasks", response_class=HTMLResponse)
def tasks(request: Request, tab: str = "open") -> HTMLResponse:
    """The task list: one tab (open | waiting | closed, D11), newest activity first."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None:
            return _signin("/ops/tasks", settings)
        if tab not in ops_tasks.TABS:
            raise HTTPException(status_code=404)
        return _page("ops_tasks_index.html", q=ops_tasks.queue_view(conn, tab))


@router.get("/ops/tasks/{ticket_id}", response_class=HTMLResponse)
def task(request: Request, ticket_id: str, done: str | None = None) -> HTMLResponse:
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None:
            return _signin(f"/ops/tasks/{ticket_id}", settings)
        found = ops_tasks.detail(conn, ticket_id) if TICKET_ID.fullmatch(ticket_id) else None
        if found is None:
            raise HTTPException(status_code=404)
        shown = done if done in TASK_ACTIONS else None
        return _page("ops_task.html", d=found, csrf=who["csrf"], done=shown, notice=None, draft=None)


@router.post("/ops/tasks/{ticket_id}")
def task_act(
    request: Request,
    ticket_id: str,
    action: str = Form(""),
    text: str | None = Form(None),
    resolution: str | None = Form(None),
    csrf: str | None = Form(None),
):
    """Reply by e-mail, send and close, or close as (D13): the change and its audit row commit together in ops_tasks;
    the outbox is flushed here, after the commit, so the reply goes now rather than at the hourly job."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None or not TICKET_ID.fullmatch(ticket_id):
            raise HTTPException(status_code=404)
        sessions.check_csrf(who, csrf)
        if action not in TASK_ACTIONS:
            raise HTTPException(status_code=400)
        admin, ip = who["email"].strip().lower(), sessions.request_ip(request)
        try:
            if action == "close":
                ops_tasks.close(conn, ticket_id, admin=admin, resolution=resolution, ip=ip)
            else:
                ops_tasks.reply(conn, ticket_id, admin=admin, text=text, close=action == "reply_close", ip=ip)
        except ToolError as e:
            found = ops_tasks.detail(conn, ticket_id)
            if found is None:
                raise HTTPException(status_code=404) from None
            draft = text if action != "close" else None
            return _page("ops_task.html", status=409, d=found, csrf=who["csrf"], done=None, notice=str(e), draft=draft)
    if action != "close":
        support_mail.flush(ticket_id)  # after the commit: the reply, and the close it asked for once the mail is sent
    return RedirectResponse(f"/ops/tasks/{ticket_id}?done={action}", status_code=303, headers=NO_STORE)


# ---------------------------------------------------------------- /ops/rules (spec 2026-10-12 §2.5)


@router.get("/ops/rules", response_class=HTMLResponse)
def rules(request: Request, done: str | None = None) -> HTMLResponse:
    """The refusal rules: proposed (with evidence), active, add one, the last summaries (plan D16)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None:
            return _signin("/ops/rules", settings)
        shown = done if done in RULE_ACTIONS else None
        return _page("ops_rules.html", v=ops_rules.page_view(conn), csrf=who["csrf"], done=shown, notice=None)


@router.post("/ops/rules")
def rules_act(
    request: Request,
    action: str = Form(""),
    rule_id: str | None = Form(None),
    level: str | None = Form(None),
    why: str | None = Form(None),
    kind: str | None = Form(None),
    value: str | None = Form(None),
    applies_to: str | None = Form(None),
    topic: str | None = Form(None),
    csrf: str | None = Form(None),
):
    """approve / reject / retire / level on one rule, or add one (D16): the change and its audit row commit together
    in ops_rules; a refusal is said on the page (409) and changes nothing."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _admin(conn, request, settings)
        if who is None:
            raise HTTPException(status_code=404)
        sessions.check_csrf(who, csrf)
        if action not in RULE_ACTIONS:
            raise HTTPException(status_code=400)
        admin, ip = who["email"].strip().lower(), sessions.request_ip(request)
        try:
            if action == "add":
                ops_rules.add(
                    conn,
                    admin=admin,
                    kind=kind or "",
                    value=value,
                    applies_to=applies_to or "",
                    level=level or "",
                    why=why,
                    topic=topic,
                    ip=ip,
                )
            else:
                if not rule_id or not RULE_ID.fullmatch(rule_id):
                    raise HTTPException(status_code=404)
                ops_rules.decide(conn, rule_id, admin=admin, action=action, level=level, why=why, ip=ip)
        except ToolError as e:
            return _page(
                "ops_rules.html", status=409, v=ops_rules.page_view(conn), csrf=who["csrf"], done=None, notice=str(e)
            )
    return RedirectResponse(f"/ops/rules?done={action}", status_code=303, headers=NO_STORE)
