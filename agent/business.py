"""Business rows, Business Knowledge versions and goals (spec §4.4 business, knowledge, goal)."""

from __future__ import annotations

from collections.abc import Iterable

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import db, profile
from .util import iso

KEEP = object()  # set_goal: "the latest goal's value" for a field the caller does not mention


def create(conn: psycopg.Connection, *, locale: str = "en-US") -> str:
    biz_id = db.new_id("biz")
    conn.execute("INSERT INTO business (id, locale) VALUES (%s, %s)", (biz_id, locale))
    return biz_id


def row(conn: psycopg.Connection, biz_id: str) -> dict | None:
    r = conn.execute(
        "SELECT id, slug, owner_email, locale, status, claim_state, verified_via, verified_at, "
        "claim_expires_at, ai_crawlers, operator_flags, ads_mode, paused_reason, created_at "
        "FROM business WHERE id = %s",
        (biz_id,),
    ).fetchone()
    return dict(r) if r else None


def latest_knowledge(conn: psycopg.Connection, biz_id: str) -> tuple[int, dict] | None:
    r = conn.execute(
        "SELECT version, json FROM knowledge WHERE business_id = %s ORDER BY version DESC LIMIT 1", (biz_id,)
    ).fetchone()
    return (r["version"], r["json"]) if r else None


def add_knowledge(conn: psycopg.Connection, biz_id: str, data: dict, *, source: str) -> int:
    """Append a version (extract | owner | gbp). Two concurrent appends compute the same next
    version and collide on the primary key; the loser gets an owner-readable, retryable error
    (run() rolls its transaction back and audits the failure)."""
    # spec 2026-10-01 §5: a service's stored id, the offers, the notes survive a new reading, and a hand-off's copy of a
    # draft's knowledge (consult.move_session) onto a business that already has them
    if source in ("extract", "handoff"):
        before = latest_knowledge(conn, biz_id)
        data = profile.carry(before[1] if before else None, data)
    try:
        r = conn.execute(
            "INSERT INTO knowledge (business_id, version, json, source) VALUES "
            "(%s, COALESCE((SELECT max(version) FROM knowledge WHERE business_id = %s), 0) + 1, %s, %s) "
            "RETURNING version",
            (biz_id, biz_id, Jsonb(data), source),
        ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name != "knowledge_pkey":
            raise
        raise ToolError("another change to this business landed at the same moment; repeat the call") from e
    return r["version"]


def readopt_place(conn: psycopg.Connection, biz_id: str, place) -> dict:
    """The knowledge on file with `place` -- where the owner put the ads' centre on the map, as the version before a new
    reading or a hand-off's copy held it -- adopted again as an address is adopted (profile.apply with the place as the
    command's listing): a business listing the copy brought is moved to it, its name kept, and with none the place
    stays as it was (walk fixes 1, final review A1). A new version (source owner) only when that changes anything, and
    nothing when `place` is no place the owner put on the map (profile.placed). Returns the knowledge on file after."""
    data = latest_knowledge(conn, biz_id)[1]
    if not profile.placed(place):
        return data
    kept = profile.apply(data, {"location": {"address": place.get("address"), "listing": place}})
    if kept != data:
        add_knowledge(conn, biz_id, kept, source="owner")
    return kept


def retire_draft(conn: psycopg.Connection, biz_id: str) -> None:
    """A draft business done for: status 'expired', every live key of its own revoked -- a hand-off landing
    (consult.move_session) and the home's start over (harness/dispatch.py, K6 slice 1 §5) both end a draft this way.
    Never a claimed business: the callers check."""
    conn.execute("UPDATE business SET status = 'expired', updated_at = now() WHERE id = %s", (biz_id,))
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE business_id = %s AND revoked_at IS NULL", (biz_id,))


def _goal_json(r) -> dict:
    return {
        "id": r["id"],
        "goal_type": r["goal_type"],
        "target": r["target"],
        "monthly_limit_usd": float(r["monthly_limit_usd"]) if r["monthly_limit_usd"] is not None else None,
        "focus_services": list(r["focus_services"]),
        "autopilot": r["autopilot"],
        "limit_source": r["limit_source"],
        "channel_caps": {ch: float(v) for ch, v in (r["channel_caps"] or {}).items()},
        "max_cost_per_customer_usd": (
            float(r["max_cost_per_customer_usd"]) if r["max_cost_per_customer_usd"] is not None else None
        ),
        "created_at": iso(r["created_at"]),
    }


GOAL_COLS = (
    "id, goal_type, target, monthly_limit_usd, focus_services, autopilot, limit_source, channel_caps, created_at, "
    "max_cost_per_customer_usd"
)
TARGET_MAX = 10_000  # customers per month
BUDGET_MAX = 100_000  # USD per month
COST_MAX = 100_000  # USD: the most a new customer may cost
GOAL_TYPES = ("customers", "calls", "bookings")  # what a goal counts each month (goal_type)
LIMIT_SOURCES = ("owner", "system")  # whose monthly limit a goal holds (limit_source): set_goal refuses any other
FOCUS_MAX_ITEMS, FOCUS_MAX_CHARS = 10, 80  # services a goal focuses on


def target_ok(target: int | None) -> bool:
    return target is None or 0 < target <= TARGET_MAX


def budget_ok(monthly_limit_usd: float | None) -> bool:
    return monthly_limit_usd is None or 0 <= monthly_limit_usd <= BUDGET_MAX


def cost_ok(value) -> bool:
    return value is None or (not isinstance(value, bool) and isinstance(value, (int, float)) and 1 <= value <= COST_MAX)


def check_goal(
    target: int | None, monthly_limit_usd: float | None, focus_services: Iterable[str] = ()
) -> tuple[int | None, float | None, list[str], list[str]]:
    """A goal read by the model from the owner's words (`start`): out-of-range values are dropped
    with a note each instead of refusing the whole setup, and the focus services are clipped to
    FOCUS_MAX_ITEMS of at most FOCUS_MAX_CHARS characters. Returns (target, limit, focus, notes).
    set_goal refuses the same out-of-bounds values when the owner gives them explicitly."""
    notes: list[str] = []
    if not target_ok(target):
        notes.append(f"target {target} was out of range and was not recorded")
        target = None
    if not budget_ok(monthly_limit_usd):
        notes.append(f"monthly budget ${monthly_limit_usd:,.0f} was out of range and was not recorded")
        monthly_limit_usd = None
    focus = [s[:FOCUS_MAX_CHARS] for s in list(focus_services)[:FOCUS_MAX_ITEMS]]
    return target, monthly_limit_usd, focus, notes


def set_goal(
    conn: psycopg.Connection,
    biz_id: str,
    *,
    goal_type=KEEP,
    target: int | None = None,
    monthly_limit_usd: float | None = None,
    focus_services: Iterable[str] = (),
    autopilot=KEEP,
    limit_source: str = "owner",
    channel_caps: dict[str, float] | None = None,
    max_cost_per_customer_usd=KEEP,
) -> dict:
    """`limit_source`: "owner" when the owner named the monthly limit; "system" when recommend set it as
    the ceiling for a plan the owner gave no budget for (tools_consult.build reads it back differently).
    `channel_caps` (spec 2026-09-29-admiry-channels-design §7.1): the owner's monthly ceiling per channel, which the
    allocator water-fills against; None keeps the latest goal's (a goal written for another reason, recommend's
    ceiling included, never drops them), {} clears them. `max_cost_per_customer_usd` (spec 2026-10-01 D162): the
    most a new customer may cost; KEEP (the default) keeps the latest goal's, None clears it. `goal_type` and
    `autopilot` work like the ceiling: KEEP (the default) keeps the latest goal's, else "customers" and "safe", so a
    goal written for another reason (start's stated goal, recommend's ceiling) never resets what the owner set. A goal
    is dated when it is written, so the latest goal is the last one written."""
    if limit_source not in LIMIT_SOURCES:
        raise ValueError(f"limit_source must be {' or '.join(LIMIT_SOURCES)}, not {limit_source!r}")
    previous = (
        latest_goal(conn, biz_id)
        if channel_caps is None or goal_type is KEEP or autopilot is KEEP or max_cost_per_customer_usd is KEEP
        else None
    )
    if goal_type is KEEP:
        goal_type = (previous or {}).get("goal_type") or "customers"
    if autopilot is KEEP:
        autopilot = (previous or {}).get("autopilot") or "safe"
    if channel_caps is None:
        channel_caps = (previous or {}).get("channel_caps") or {}
    if max_cost_per_customer_usd is KEEP:
        max_cost_per_customer_usd = (previous or {}).get("max_cost_per_customer_usd")
    goal_id = db.new_id("goal")
    # dated when it is written, not by the column default now(), which is the start of the transaction: a goal is read
    # back as the latest one, and two writers that overlap would otherwise sort by who began first
    r = conn.execute(
        f"INSERT INTO goal (id, business_id, goal_type, target, monthly_limit_usd, focus_services, autopilot, "
        f"limit_source, channel_caps, max_cost_per_customer_usd, created_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp()) RETURNING {GOAL_COLS}",
        (
            goal_id,
            biz_id,
            goal_type,
            target,
            monthly_limit_usd,
            list(focus_services),
            autopilot,
            limit_source,
            Jsonb(channel_caps),
            max_cost_per_customer_usd,
        ),
    ).fetchone()
    return _goal_json(r)


def latest_goal(conn: psycopg.Connection, biz_id: str) -> dict | None:
    r = conn.execute(
        f"SELECT {GOAL_COLS} FROM goal WHERE business_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (biz_id,)
    ).fetchone()
    return _goal_json(r) if r else None


def goal_by_id(conn: psycopg.Connection, biz_id: str, goal_id: str) -> dict | None:
    r = conn.execute(f"SELECT {GOAL_COLS} FROM goal WHERE business_id = %s AND id = %s", (biz_id, goal_id)).fetchone()
    return _goal_json(r) if r else None
