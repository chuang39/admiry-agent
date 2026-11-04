"""The page-side endpoints (spec §5.2): the script, the event beacon, the form handler. Cross-origin by design
(P7): form-encoded bodies, `Access-Control-Allow-Origin: *`, no cookies. Silent on abuse (P8). psycopg and
mail.send are blocking, so the handlers only read the body themselves and hand the rest to a thread
(run_in_threadpool) instead of stalling the worker's event loop. Bodies are read under limits (I8): 413 past
MAX_BODY by content-length, before anything is parsed; 400 for what Starlette's parser refuses (a file part,
more than 20 fields, a part over 4 KB); every value a string cut at MAX_FIELD characters."""

from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException

from . import attribution, bundles, config, db, leads, mail
from .channels import registry

router = APIRouter()
SCRIPT = Path(__file__).parent / "static" / "admiry.js"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Cache-Control": "no-store",
}
# R14 (C3): the beacon counts visits and taps only. A form is recorded by the form handler, next to its stored
# lead: /t/event is open to anyone, so a `kind=form` beacon must never count toward a business's form limit.
# A3p-3 P27: and a booking completed on the page (Calendly's own message), which also records a `booking` lead --
# reported by the page, under the event limit and the form limit both
KINDS = ("visit", "call", "mail", "book", "booked_online")
BOOKING_FIELDS = {
    "reported_by": "page",
    "provider": "calendly",
}  # a booking lead's whole record: the provider has the rest
EVENTS_PER_HOUR = 600
FORMS_PER_HOUR = 30
# I9: what the form handler stores (every recipe's form fields come from this set), and the names two recipes
# used to carry, still taken from a page built before the rename. `patient` (A3p-6 Q12): a dental site's appointment
# form asks "New patient" or "Existing patient", never a health question.
FIELDS = ("name", "phone", "email", "patient", "service", "preferred_time", "zip", "message")
ALIASES = {"timing": "preferred_time", "matter_type": "service"}
MAX_BODY = 64 * 1024
MAX_FIELD = 2000
# request.form()'s own (Starlette 1.7); 32 fields since A3p-3 (a form also carries the link's five UTM tags, the
# referring host and its intent beside the click id and the visitor's fields)
FORM_LIMITS = {"max_files": 0, "max_fields": 32, "max_part_size": 4096}
NO_CONTACT = "Please add a phone number or an e-mail so they can reach you."
NOT_CONNECTED = "This page is not connected to Admiry."
UNREADABLE = "The form could not be sent as it is; please shorten the message and send it again."
# The answer to a scripts-off form post (I4): the same sentence as the JSON's `say`, no scripts (the CSP makes
# sure of it whatever the business name holds).
PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
    "<title>{title}</title></head><body><p>{say}</p></body></html>"
)
PAGE_HEADERS = {**CORS, "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}


@router.get("/t/admiry.js")
def script():
    return FileResponse(
        SCRIPT,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


@router.options("/t/event")
@router.options("/t/form/{page_id}")
def preflight(page_id: str | None = None):
    return Response(status_code=204, headers=CORS)


async def _read_form(request: Request) -> tuple[FormData | None, int]:
    """The posted fields under I8's limits: (form, 200); or (None, 413) past MAX_BODY by content-length, before
    anything is parsed; or (None, 400) for what the parser refuses -- a file part, more than 20 fields, a part
    over 4 KB, a broken body -- where a file part used to end in a 500."""
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return None, 413
    except ValueError:
        return None, 400
    try:
        return await request.form(**FORM_LIMITS), 200
    except HTTPException:  # Starlette's own 400 for its parser's MultiPartException
        return None, 400


def _text(form: FormData, key: str) -> str:
    """One posted value as text, cut at MAX_FIELD characters: "" when missing or not a string."""
    v = form.get(key)
    return v.strip()[:MAX_FIELD] if isinstance(v, str) else ""


def _click(form: FormData) -> tuple[str | None, str | None]:
    """The click id the visit carried and its kind: the first of registry.CLICK_ID_KINDS (Google's three, then the
    other platforms') found as `k`, or `_k` from the snippet's hidden input, of at most 200 characters."""
    for k in registry.CLICK_ID_KINDS:
        v = _text(form, k) or _text(form, "_" + k)
        if v and len(v) <= 200:
            return v, k
    return None, None


def _visit(form: FormData) -> tuple[tuple[str | None, str | None], dict, str | None, str]:
    """What brought the visitor, as the script sends it with a beacon or a form (A3p-3 P28): the click id and its kind,
    the link's UTM tags, the referring host (`ref`, or `_ref` from a form) and the source those name (`came_from`)."""
    click = _click(form)
    tags = attribution.utm(lambda key: _text(form, key))
    ref = attribution.host(_text(form, "ref") or _text(form, "_ref"))
    return click, tags, ref, attribution.came_from(click[1], tags, ref)


# The columns the beacon and the form read of the page (`_live_page`): a visitor's every beacon reads the row, so not
# `landing.PAGE_COLS`, whose wide columns -- an industry site's draft is KBs of JSON (A3p-6 batch 2) -- it would carry
# for nothing
LIVE_PAGE_COLS = "id, business_id, status"


def _live_page(conn, page_id: str) -> dict | None:
    """The page a snippet names, when it may take events and forms: `published`, or `preview` -- R11: the kit's
    row before set_landing_page names an address, so a snippet pasted from the kit works from the start --
    never an `unpublished` one. Its `LIVE_PAGE_COLS` only."""
    page = None
    if page_id and page_id.startswith("pg_") and len(page_id) <= 40:
        page = conn.execute(f"SELECT {LIVE_PAGE_COLS} FROM page WHERE id = %s", (page_id,)).fetchone()
    return page if page and page["status"] in ("preview", "published") else None


def _events_over(conn, page_id: str) -> bool:
    n = conn.execute(
        "SELECT count(*) AS n FROM page_event WHERE page_id = %s AND ts > now() - interval '1 hour'", (page_id,)
    ).fetchone()["n"]
    return n >= EVENTS_PER_HOUR


def _forms_over(conn, page: dict) -> bool:
    """R14 (C3): the form limit counts the page's stored leads of the last hour, never page_event rows."""
    n = conn.execute(
        "SELECT count(*) AS n FROM lead WHERE business_id = %s AND page_id = %s "
        "AND created_at > now() - interval '1 hour'",
        (page["business_id"], page["id"]),
    ).fetchone()["n"]
    return n >= FORMS_PER_HOUR


def _record_event(page_id: str, kind: str, visit: tuple) -> None:
    """The blocking half of `event`: a fresh connection per call (run_in_threadpool gives it its own thread),
    same as before. The event keeps its source (A3p-3 P28). A booking completed on the page (`booked_online`, P27)
    also records a `booking` lead with no contact -- the booking provider has the person's details -- while the
    page's form limit allows; it is the page's own report (`fields.reported_by`), and no lead e-mail goes (the
    provider tells the owner)."""
    click, tags, ref, came = visit
    with db.connect(config.load().database_url) as conn:
        page = _live_page(conn, page_id)
        if page and not _events_over(conn, page["id"]):
            conn.execute(
                "INSERT INTO page_event (page_id, kind, click_id, click_id_kind, came_from) VALUES (%s, %s, %s, %s, %s)",
                (page["id"], kind, *click, came),
            )
            if kind == "booked_online" and not _forms_over(conn, page):
                leads.record(
                    conn, page, dict(BOOKING_FIELDS), click, kind="booking", came_from=came, utm=tags, referrer=ref
                )


def _from_preview(form: FormData) -> bool:
    """A beacon from a site bundle's preview host (A3p-3 B8): the owner's look at a preview, or the render check's, is
    no visit -- answered alike and stored nowhere. Its forms still make leads, like a single page's preview (H45)."""
    try:
        return bundles.is_preview_host(urlsplit(_text(form, "url")).hostname)
    except ValueError:
        return False


@router.post("/t/event")
async def event(request: Request):
    form, status = await _read_form(request)
    if form is None:
        return Response(status_code=status, headers=CORS)
    page_id, kind = _text(form, "page"), _text(form, "kind")
    # anything else, `form` included (R14), is answered alike and stored nowhere
    if kind in KINDS and not _from_preview(form):
        await run_in_threadpool(_record_event, page_id, kind, _visit(form))
    return Response(status_code=204, headers=CORS)


def _thanks(conn, page: dict, *, lead: bool, intent: str = "consult") -> dict:
    """The thank-you, worded the same whether or not a lead was stored (P8); `lead` tells the script whether to
    report a Google conversion -- never for the honeypot or an over-the-limit form -- and `conversion` which one: one
    primary conversion per form (A3p-3 P25), an appointment request's own, else the form's."""
    name = leads.business_name(conn, page["business_id"])
    out = {"ok": True, "lead": lead, "say": f"Thanks — {name} will call you back shortly."}
    if lead:
        out["conversion"] = "appointment" if intent == "appointment" else "form"
    return out


def _take_form(page_id: str, fields: dict, honeypot: bool, intent: str, visit: tuple) -> tuple[int, dict]:
    """The blocking half of `form`: (status_code, body). `leads.business_name` (a knowledge lookup) is only
    called where the thank-you is actually returned, not on the 404/400 paths."""
    click, tags, ref, came = visit
    with db.connect(config.load().database_url) as conn:
        page = _live_page(conn, page_id)
        if page is None:
            return 404, {"ok": False, "say": NOT_CONNECTED}
        if honeypot:
            return 200, _thanks(conn, page, lead=False)
        if not (leads.digits(fields["phone"]) >= 10 or "@" in fields["email"]):
            return 400, {"ok": False, "say": NO_CONTACT}
        if _forms_over(conn, page):
            return 200, _thanks(conn, page, lead=False)
        # an answer left empty or never sent is stored as none, never as some default (A3p-6 batch 1 follow-ups)
        given = {k: v for k, v in fields.items() if v}
        lead = leads.record(conn, page, given, click, intent=intent, came_from=came, utm=tags, referrer=ref)
        conn.execute(
            "INSERT INTO page_event (page_id, kind, click_id, click_id_kind, came_from) VALUES (%s, 'form', %s, %s, %s)",
            (page["id"], *click, came),
        )
        try:
            leads.notify(conn, lead)
        except mail.MailError:
            pass  # mail.py already logged the refusal/transport error; the lead survives regardless
        return 200, _thanks(conn, page, lead=True, intent=intent)


def _prefers_html(accept: str) -> bool:
    """Whether the Accept header ranks text/html above application/json: a browser's own navigation (a native
    form post, scripts off) sends "text/html,…"; fetch() sends "*/*", and the script "application/json"."""
    q: dict[str, float] = {}
    for item in accept.lower().split(","):
        media, *params = (p.strip() for p in item.split(";"))
        try:
            q[media] = float(next((p[2:] for p in params if p.startswith("q=")), "1"))
        except ValueError:
            q[media] = 0.0
    return q.get("text/html", 0.0) > q.get("application/json", 0.0)


def _answer(request: Request, status: int, body: dict) -> Response:
    """JSON for the script's fetch; for a navigation (I4: a form built from the kit, posted with scripts off) a
    small HTML page carrying the same sentence, not raw JSON."""
    if _prefers_html(request.headers.get("accept", "")):
        title = "Thank you" if body["ok"] else "Not sent"
        page = PAGE.format(title=title, say=html.escape(body["say"]))
        return HTMLResponse(page, status_code=status, headers=PAGE_HEADERS)
    return JSONResponse(body, status_code=status, headers=CORS)


@router.post("/t/form/{page_id}")
async def form(request: Request, page_id: str):
    data, status = await _read_form(request)
    if data is None:
        return _answer(request, status, {"ok": False, "say": UNREADABLE})
    fields = {k: _text(data, k) for k in FIELDS}
    for alias, canonical in ALIASES.items():
        fields[canonical] = fields[canonical] or _text(data, alias)
    honeypot = bool(_text(data, "website2"))
    asked = _text(data, "_intent").lower()  # A3p-3 P25: the form's data-admiry-intent, as the script sends it
    intent = asked if asked in leads.INTENTS else "consult"
    status, body = await run_in_threadpool(_take_form, page_id, fields, honeypot, intent, _visit(data))
    return _answer(request, status, body)
