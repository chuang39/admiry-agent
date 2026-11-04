"""Leads from a page's form (spec 2026-09-28-admiry-pages-design.md §6; A3p-2 may add `kind = 'call'` from
the tracking beacon). `record`/`notify` are `track.form`'s: store the lead, then tell the owner by e-mail.
`list_for`/`update` are `list_leads`/`update_lead`'s: both hand back the same owner-facing shape, the person's
own words only under keys `audit.SECRET_FIELDS` scrubs from the audit row (I6): the contact info under one
`contact` key, scrubbed whole, and `preferred_time` / `message`."""

from __future__ import annotations

from datetime import date, datetime, time, timezone, tzinfo

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import business, db, mail, plans
from .channels import registry
from .util import iso

STATUSES = ("new", "contacted", "booked", "won", "lost", "spam")
AWAITING = ("new", "contacted")  # no outcome yet: what get_leads(awaiting_outcome) and the state's count read
BECAME_A_JOB = ("booked", "won")  # a lead in one of these has no reason for being lost (open item B10)
# what a lead can be: a form sent, a call, what the owner reports (a walk-in), and (A3p-3 P27) a booking completed on
# the page -- the owner reports only the first three (REPORTED_KINDS)
KINDS = ("form", "call", "other", "booking")
REPORTED_KINDS = ("form", "call", "other")
INTENTS = ("consult", "appointment", "quote")  # a form's intent (A3p-3 P25): consult unless the form says otherwise
SOURCES = ("page", "platform", "owner")
REASONS = ("out_of_area", "wrong_service", "no_answer", "price", "duplicate", "not_a_customer", "other")
CONTACT_KEYS = ("name", "phone", "email")  # what a contact holds: each text, under the one secret `contact` key (I6)
VALUE_MAX = 1_000_000  # USD: what one won lead may be worth
NOTE_MAX, SERVICE_MAX, CONTACT_MAX = 500, 120, 120
NOON = time(12)  # a lead the owner reports by its date alone is that day at noon in the account's calendar
LEAD_COLS = (
    "id, business_id, page_id, kind, fields, click_id, click_id_kind, channel, status, note, notified_at, uploaded_at, "
    "created_at, value_usd, reason, source, intent, came_from, utm, referrer"
)
BAD_STATUS = f"status must be one of {', '.join(STATUSES[:-1])} or {STATUSES[-1]}"
BAD_KIND = "kind must be one of form, call or other"  # what the owner reports (add_lead)
BAD_LIST_KIND = "kind must be one of form, call, other or booking"
BAD_REASON = f"reason must be one of {', '.join(REASONS)}"
BAD_VALUE = f"value_usd must be between 0 and {VALUE_MAX:,}"
BAD_WHEN = "happened_at must be an ISO date or time, e.g. 2026-09-28"
FUTURE = "happened_at cannot be in the future"
BAD_CONTACT = "contact must be an object with name, phone or email"
CONTACT_TAKES = "contact takes name, phone and email"


def digits(s: str) -> int:
    return sum(1 for c in s if c.isdigit())


def business_name(conn: psycopg.Connection, biz: str) -> str:
    """The name the thank-you and the lead e-mail lead with: the knowledge's vouched name (plans.name_for
    -- a website or a Google listing backs it), else the generic "the business"."""
    latest = business.latest_knowledge(conn, biz)
    return (plans.name_for(latest[1]) if latest else None) or "the business"


def _shape(row: dict) -> dict:
    fields = row.get("fields") or {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "when": iso(row["created_at"]),
        "contact": {key: fields.get(key) or None for key in CONTACT_KEYS},
        "service": fields.get("service") or None,
        "preferred_time": fields.get("preferred_time") or None,
        "message": fields.get("message") or None,
        # what an industry site's form asks besides (the founder's dev walk, 2026-10-08: the lead e-mail had them,
        # the list did not): the visitor's ZIP code -- where they are: never in an audit row -- and a dental site's
        # new or existing patient
        "zip": fields.get("zip") or None,
        "patient": fields.get("patient") or None,
        "click": bool(row.get("click_id")),
        # spec 2026-09-29 §7.1: the channel the click came from (none: organic) and the click id itself -- the
        # owner's own ad click, not the person's words (the audit keeps it)
        "channel": row.get("channel"),
        "click_id": row.get("click_id"),
        "page_id": row.get("page_id"),
        # spec 2026-10-01 §5: what it became and where the row came from
        "value_usd": float(row["value_usd"]) if row.get("value_usd") is not None else None,
        "reason": row.get("reason"),
        "source": row.get("source") or "page",
        # A3p-3 P25, P28: what the form asked for, and what brought the visitor (the click's channel, a tagged link,
        # the kind of site that referred them, or direct); a lead from before Admiry kept it reads its channel
        "intent": row.get("intent"),
        "came_from": row.get("came_from") or row.get("channel"),
    }


def record(
    conn: psycopg.Connection,
    page: dict,
    fields: dict,
    click: tuple[str | None, str | None],
    *,
    kind: str = "form",
    intent: str | None = None,
    came_from: str | None = None,
    utm: dict | None = None,
    referrer: str | None = None,
) -> dict:
    """Store one lead for page['business_id'] -- a form sent (kind 'form', with its `intent`), or (A3p-3 P27) a booking
    completed on the page (kind 'booking') -- on the channel its click id came from (spec 2026-09-29 §8: NULL for no
    click id, or one nobody maps), with where the visitor came from (A3p-3 P28: `came_from`, the link's `utm` tags, the
    `referrer` host). Returns the raw row (notify needs business_id/fields/created_at, not the owner-facing shape)."""
    click_id, click_id_kind = click
    row = conn.execute(
        "INSERT INTO lead (id, business_id, page_id, kind, fields, click_id, click_id_kind, channel, intent, came_from, "
        f"utm, referrer) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {LEAD_COLS}",
        (
            db.new_id("ld"),
            page["business_id"],
            page["id"],
            kind,
            Jsonb(fields),
            click_id,
            click_id_kind,
            registry.channel_for_click(click_id_kind),
            intent,
            came_from,
            Jsonb(utm) if utm else None,
            referrer,
        ),
    ).fetchone()
    return dict(row)


def notify(conn: psycopg.Connection, lead: dict) -> str | None:
    """E-mail the owner about a new lead, only when notification e-mail is on for this business
    (owner_email set, notify_email_status 'ok' -- business.row() has no such column, so a direct SELECT
    reads both). Sets lead.notified_at on a successful send. A refusal raises mail.MailError (already
    logged once, in mail.py); the lead itself is already stored by the time this runs."""
    row = conn.execute(
        "SELECT owner_email, notify_email_status FROM business WHERE id = %s", (lead["business_id"],)
    ).fetchone()
    if not row or not row["owner_email"] or row["notify_email_status"] != "ok":
        return None
    fields = lead["fields"]
    who = (fields.get("name") or fields.get("phone") or "a visitor")[:60]  # I10: mail.send cleans the rest
    text = mail.render(
        "mail_lead.txt.j2",
        name=business_name(conn, lead["business_id"]),
        fields=fields,
        when=lead["created_at"].astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC"),
    )
    sent = mail.send(row["owner_email"], f"New lead from your page: {who}", text)
    if sent is not None:
        conn.execute("UPDATE lead SET notified_at = now() WHERE id = %s", (lead["id"],))
    return sent


def _parse_since(since: str | None) -> datetime | None:
    """`since` as a timezone-aware datetime (a naive ISO value is read as UTC), or None. A caller-facing
    ToolError, not a raw psycopg error, on anything that isn't ISO."""
    if since is None:
        return None
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        raise ToolError("since must be an ISO date or time, e.g. 2026-09-01") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _check_value(value_usd) -> float | None:
    if value_usd is None:
        return None
    if isinstance(value_usd, bool) or not isinstance(value_usd, (int, float)) or not 0 <= value_usd <= VALUE_MAX:
        raise ToolError(BAD_VALUE)
    return float(value_usd)


def list_for(
    conn: psycopg.Connection,
    biz: str,
    since: str | None,
    limit: int = 50,
    *,
    status: str | None = None,
    channel: str | None = None,
    kind: str | None = None,
    awaiting_outcome: bool = False,
    lead_id: str | None = None,
) -> list[dict]:
    """This business's leads, newest first; `since` (ISO date/time, optional) keeps only leads from then on,
    `status` only those in that status, `channel` only those a click on that channel brought (spec 2026-09-29 §7.1)
    -- a channel the owner can name (D76), whether or not Admiry runs ads there yet -- `kind` only forms, calls or
    the rest, `awaiting_outcome` only those nobody has said the outcome of (new, contacted), `lead_id` that one."""
    when = _parse_since(since)
    if status is not None and status not in STATUSES:
        raise ToolError(BAD_STATUS)
    if channel is not None and channel not in registry.LABELS:
        raise ToolError("unknown channel")
    if kind is not None and kind not in KINDS:
        raise ToolError(BAD_LIST_KIND)
    n = max(1, min(int(limit), 50))
    rows = conn.execute(
        f"SELECT {LEAD_COLS} FROM lead WHERE business_id = %s "
        "AND (%s::timestamptz IS NULL OR created_at >= %s) AND (%s::text IS NULL OR status = %s) "
        "AND (%s::text IS NULL OR channel = %s) AND (%s::text IS NULL OR kind = %s) "
        "AND (NOT %s OR status = ANY(%s)) AND (%s::text IS NULL OR id = %s) ORDER BY created_at DESC LIMIT %s",
        (
            biz,
            when,
            when,
            status,
            status,
            channel,
            channel,
            kind,
            kind,
            bool(awaiting_outcome),
            list(AWAITING),
            lead_id,
            lead_id,
            n,
        ),
    ).fetchall()
    return [_shape(dict(r)) for r in rows]


def counts(conn: psycopg.Connection, biz: str) -> dict[str, int]:
    """How many of the business's leads are in each status (every status present, zero included)."""
    found = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM lead WHERE business_id = %s GROUP BY status", (biz,)
        ).fetchall()
    }
    return {s: int(found.get(s, 0)) for s in STATUSES}


def awaiting(conn: psycopg.Connection, biz: str) -> int:
    """How many leads still wait for an outcome: a count, safe for a caller without the `leads` scope."""
    return conn.execute(
        "SELECT count(*) AS n FROM lead WHERE business_id = %s AND status = ANY(%s)", (biz, list(AWAITING))
    ).fetchone()["n"]


def update(
    conn: psycopg.Connection,
    biz: str,
    lead_id: str,
    status: str,
    note: str | None,
    *,
    value_usd: float | None = None,
    reason: str | None = None,
) -> dict:
    """Set a lead's status (and, when given, its note, what it was worth, why it was lost) once the owner says what
    happened with it. A value given earlier stays until another replaces it. So does a reason, except on a lead that
    became a job (booked or won: BECAME_A_JOB), which has no reason for being lost: its reason is cleared, and one sent
    with it is not kept (open item B10)."""
    if status not in STATUSES:
        raise ToolError(BAD_STATUS)
    if reason is not None and reason not in REASONS:
        raise ToolError(BAD_REASON)
    value = _check_value(value_usd)
    row = conn.execute(
        "UPDATE lead SET status = %s, note = COALESCE(%s, note), value_usd = COALESCE(%s, value_usd), "
        "reason = CASE WHEN %s THEN NULL ELSE COALESCE(%s, reason) END "
        f"WHERE id = %s AND business_id = %s RETURNING {LEAD_COLS}",
        (status, note, value, status in BECAME_A_JOB, reason, lead_id, biz),
    ).fetchone()
    if row is None:
        raise ToolError("no lead with that id for this business")
    return _shape(dict(row))


def _happened(happened_at: str | None, calendar: tuple[date, tzinfo]) -> datetime:
    """When a lead the owner reports happened, read in the account's own calendar (`calendar`: its today and its zone,
    the ones the funnel counts by; final review B, Important 5): a date alone is that day there, at noon; a time with
    no zone is a time there; one with a zone is kept. Now when omitted. Refused: what is not an ISO date or time, a
    date after today there, a time after now."""
    today, zone = calendar
    now = datetime.now(timezone.utc)
    if happened_at is None:
        return now
    try:
        day = date.fromisoformat(happened_at)  # a date alone; a time is refused here and read below
    except (TypeError, ValueError):
        day = None
    if day is not None:
        if day > today:
            raise ToolError(FUTURE)
        return datetime.combine(day, NOON, tzinfo=zone)
    try:
        when = datetime.fromisoformat(happened_at)
    except (TypeError, ValueError):
        raise ToolError(BAD_WHEN) from None
    when = when if when.tzinfo else when.replace(tzinfo=zone)
    if when > now:
        raise ToolError(FUTURE)
    return when


def _contact(contact) -> dict:
    """The person a reported lead names, taken whole or refused (open item A4): CONTACT_KEYS only, each text; a key
    it does not hold, or a value that is not text (a phone sent as a number), is refused, never dropped. A key sent
    null or blank is one the owner did not give. The refusals name no person (the audit row keeps the sentence)."""
    if contact is None:
        return {}
    if not isinstance(contact, dict):
        raise ToolError(BAD_CONTACT)
    if set(contact) - set(CONTACT_KEYS):
        raise ToolError(CONTACT_TAKES)
    fields: dict = {}
    for key in CONTACT_KEYS:
        text = contact.get(key)
        if text is not None and not isinstance(text, str):
            raise ToolError(f"contact.{key} must be text")
        if text and text.strip():
            fields[key] = text.strip()[:CONTACT_MAX]
    return fields


def add_owner(
    conn: psycopg.Connection,
    biz: str,
    *,
    kind: str,
    status: str,
    calendar: tuple[date, tzinfo],
    happened_at: str | None = None,
    service: str | None = None,
    value_usd: float | None = None,
    note: str | None = None,
    contact: dict | None = None,
) -> dict:
    """A lead the owner reports and Admiry has no row for (a call, a walk-in): `source = 'owner'`, no channel and no
    click id, so it counts in the business's own numbers and in no channel's (spec 2026-10-01 D163). `happened_at`
    (ISO date or time, never in the future) is when it is counted, read in `calendar` -- the account's today and zone
    (funnel.calendar, which the command reads: this module does not import the funnel): a date alone is that day
    there, at noon. Now when omitted. `contact` holds name, phone and email, each text, or is refused."""
    if kind not in REPORTED_KINDS:  # a booking completed on the page is the page's to report, never the owner's
        raise ToolError(BAD_KIND)
    if status not in STATUSES:
        raise ToolError(BAD_STATUS)
    value = _check_value(value_usd)
    when = _happened(happened_at, calendar)
    fields = _contact(contact)
    if isinstance(service, str) and service.strip():
        fields["service"] = service.strip()[:SERVICE_MAX]
    row = conn.execute(
        "INSERT INTO lead (id, business_id, kind, fields, status, note, value_usd, source, created_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, 'owner', %s) RETURNING {LEAD_COLS}",
        (db.new_id("ld"), biz, kind, Jsonb(fields), status, (note or "").strip()[:NOTE_MAX] or None, value, when),
    ).fetchone()
    return _shape(dict(row))
