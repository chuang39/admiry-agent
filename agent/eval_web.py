"""The founder's grading page (K5, ruling R-K5): every frozen plan of the golden set -- the cases' and the flawed
copies', mixed in one fixed order -- shown as the judge reads it (agent/eval/view.py), the plan rubric's six dimensions
as 1-5 choices with a note each, saved as they are given (agent/eval/grades.py). Dev only, and only for an e-mail in
AGENT_EVAL_GRADERS signed in with Google (sessions.py): on prod, with the list empty, or for anyone signed in and not
listed, every route here is a 404; an anonymous visitor on dev gets the sign-in page (K5 P7). A refused request stores
nothing and leaves no audit row. The plans come from the image's own golden set; the grades leave by the export
(agent/eval/bundle.py): this page's download, /ops/eval/export.json, or `agent.eval export --json` on the box."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import audit, config, db, sessions, web
from .eval import bundle, cases, grades, view
from .roles import contracts

router = APIRouter()
# the golden set the page reads: the image's own; the tests point these at one of theirs
CASES_DIR, FLAWED_DIR = cases.CASES_DIR, cases.FLAWED_DIR
TOOL = "web.eval_grade"
GRADES = ("1", "2", "3", "4", "5")


def _items() -> list[cases.Graded]:
    """Every frozen plan, in a fixed order that mixes the flawed copies in among the cases (a hash of the id), so the
    order says nothing about which is which."""
    return sorted(cases.graded_all(CASES_DIR, FLAWED_DIR), key=lambda it: hashlib.sha256(it.id.encode()).hexdigest())


def _page(template: str, **ctx) -> HTMLResponse:
    resp = web.page(template, **ctx)
    resp.headers["Content-Security-Policy"] = web.CONNECT_CSP
    return resp


def _at(items: list[cases.Graded], key: str) -> int:
    """The position of the plan whose digest is `key`; 404 for none."""
    for n, it in enumerate(items):
        if cases.digest(it.plan) == key:
            return n
    raise HTTPException(status_code=404)


def _session(conn, request: Request, settings: config.Settings) -> dict | None:
    """The signed-in session of a listed grader, or None for an anonymous visitor; a 404 on prod, with no one listed
    and for a signed-in e-mail that is not listed (compared lower-cased, as AGENT_EVAL_GRADERS is read)."""
    if settings.env == "prod" or not settings.eval_graders:
        raise HTTPException(status_code=404)
    session = sessions.of_request(conn, request)
    if session is not None and session["email"].strip().lower() not in settings.eval_graders:
        raise HTTPException(status_code=404)
    return session


def _signin(next_path: str, settings: config.Settings) -> HTMLResponse:
    return web.page(
        "signin.html",
        next=sessions.safe_next(next_path),
        configured=bool(settings.google_client_id),
        help=False,  # the admin sign-in screen (spec 2026-10-12 §3.1 excludes /ops/*; Task 3 review)
    )


@router.get("/ops/eval", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _session(conn, request, settings)
        if who is None:
            return _signin("/ops/eval", settings)
        rows = [
            {
                "n": n,
                "key": cases.digest(it.plan),
                "name": it.knowledge.get("name"),
                "trade": it.knowledge.get("category_detail"),
                "graded": grades.count(grades.of(conn, it)),
            }
            for n, it in enumerate(_items(), 1)
        ]
    dims = len(contracts.PLAN.ids)
    done = sum(1 for r in rows if r["graded"] == dims)
    return _page("eval_index.html", rows=rows, done=done, total=len(rows), dims=dims)


@router.get("/ops/eval/plan/{key}", response_class=HTMLResponse)
def plan_page(request: Request, key: str) -> HTMLResponse:
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _session(conn, request, settings)
        if who is None:
            return _signin(f"/ops/eval/plan/{key}", settings)
        items = _items()
        n = _at(items, key)
        saved = grades.of(conn, items[n])
    keys = [cases.digest(it.plan) for it in items]
    return _page(
        "eval_plan.html",
        key=key,
        n=n + 1,
        total=len(items),
        prev=keys[n - 1] if n else None,
        next=keys[n + 1] if n + 1 < len(items) else None,
        v=view.of(items[n]),
        saved=saved,
        dimensions=contracts.PLAN.dimensions,
        anchors=contracts.PLAN.anchors,
        csrf=who["csrf"],
    )


@router.get("/ops/eval/export.json")
def export(request: Request) -> Response:
    """The grades saved so far, as the bundle `agent.eval export --from` writes into the case files (a listed grader
    only; an anonymous visitor gets a 404 here, not the sign-in page: this is a file). Nothing is written, so nothing is
    audited."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        if _session(conn, request, settings) is None:
            raise HTTPException(status_code=404)
        body = bundle.grades_bundle(conn, _items())
    headers = {**web.NO_STORE, "Content-Disposition": 'attachment; filename="admiry-eval-grades.json"'}
    return JSONResponse(body, headers=headers)


def _grader(request: Request) -> dict:
    """The listed grader's session for a POST, before its body is read: a 404 for anyone else (anonymous too: there is
    no page to sign in to) and for every request on prod."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        who = _session(conn, request, settings)
    if who is None:
        raise HTTPException(status_code=404)
    return who


@router.post("/ops/eval/plan/{key}")
async def save_plan(request: Request, key: str) -> Response:
    who = await run_in_threadpool(_grader, request)
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    wants_json = "application/json" in request.headers.get("accept", "")
    return await run_in_threadpool(_save, request, who, key, fields, wants_json)


def _given(fields: dict[str, str]) -> tuple[dict[str, int], dict[str, str]]:
    """The whole form checked before anything is written: each `grade_<dimension>` blank or absent (nothing chosen) or a
    whole number from 1 to 5, each `note_<dimension>` at most NOTE_MAX characters (a browser sends a textarea's line
    breaks as two characters; they count once, as the box counts them); a field that names no dimension is ignored.
    A 400 for the first that is not."""
    given: dict[str, int] = {}
    notes: dict[str, str] = {}
    for d in contracts.PLAN.ids:
        raw = fields.get(f"grade_{d}", "").strip()
        if raw and raw not in GRADES:
            raise HTTPException(status_code=400, detail=f"{d}: a grade is a whole number from 1 to 5")
        if raw:
            given[d] = int(raw)
        if f"note_{d}" in fields:
            note = fields[f"note_{d}"].replace("\r\n", "\n")
            if len(note) > cases.NOTE_MAX:
                raise HTTPException(status_code=400, detail=f"{d}: a note is at most {cases.NOTE_MAX} characters")
            notes[d] = note
    return given, notes


def _save(request: Request, who: dict, key: str, fields: dict[str, str], wants_json: bool) -> Response:
    """One save of the plan's form: 403 without the form's csrf, 404 for a plan that is not in the set, 400 for a form
    with a grade that is not 1-5 or a note too long (nothing of it saved); then the grades and notes given, audited (no
    business on the row)."""
    sessions.check_csrf(who, fields.get("csrf"))
    items = _items()
    n = _at(items, key)
    given, notes = _given(fields)
    settings = config.load()
    with db.connect(settings.database_url) as conn, conn.transaction():
        graded = grades.save(conn, items[n], grades=given, notes=notes, grader=who["email"])
        audit.record_web(
            conn,
            tool=TOOL,
            email=who["email"],
            ip=sessions.request_ip(request),
            business_id=None,
            args={"plan": key},
            result={"graded": graded},
        )
    if wants_json:
        return JSONResponse({"graded": graded, "dimensions": len(contracts.PLAN.ids)}, headers=web.NO_STORE)
    after = f"/ops/eval/plan/{cases.digest(items[n + 1].plan)}" if n + 1 < len(items) else "/ops/eval"
    return RedirectResponse(after, status_code=303, headers=web.NO_STORE)
