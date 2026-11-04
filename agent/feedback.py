"""Plan feedback (spec 2026-09-30 support-and-feedback §6, D136, D137, D145): one vote per person per plan version, with
a snapshot of the conversation and that plan at the moment of the vote; a second vote updates it."""

from __future__ import annotations

import hashlib

import psycopg
from psycopg.types.json import Jsonb

from . import db, transcript

REASONS = {  # id -> the label the owner ticks (static/support.js mirrors it; test_feedback pins the two)
    "budget": "Budget doesn't fit",
    "area": "Wrong area or customers",
    "copy": "Ads don't sound like us",
    "business": "Didn't understand my business",
    "other": "Something else",
}
SURFACES = ("home", "plan_page")
RATINGS = ("up", "down")
MAX_COMMENT = 1000


def sampled(plan_id: str, rate: float) -> bool:
    """Whether /plan/{id} shows the thumbs for this plan (D136): a hash of the id, so a plan is in or out on every
    visit, at AGENT_FEEDBACK_PLAN_PAGE_RATE of plans."""
    return int(hashlib.sha256(plan_id.encode()).hexdigest()[:8], 16) / 2**32 < rate


def vote_of(conn: psycopg.Connection, plan_id: str, actor: str) -> dict | None:
    r = conn.execute(
        "SELECT rating, reasons, comment FROM plan_feedback WHERE plan_id = %s AND actor = %s", (plan_id, actor)
    ).fetchone()
    return {"rating": r["rating"], "reasons": list(r["reasons"]), "comment": r["comment"]} if r else None


def record(
    conn: psycopg.Connection,
    *,
    plan: dict,
    surface: str,
    rating: str,
    reasons: list[str],
    comment: str | None,
    actor: str,
) -> str:
    """The vote with its snapshot (D137): an upsert per person and plan version, so a change of mind is one row with a
    fresh snapshot. A 👍 carries no reasons and no comment."""
    if rating == "up":
        reasons, comment = [], None
    snapshot = transcript.build(conn, plan["business_id"], plan_id=plan["id"])
    row = conn.execute(
        "INSERT INTO plan_feedback (id, plan_id, business_id, surface, rating, reasons, comment, actor, snapshot) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (plan_id, actor) DO UPDATE SET "
        "surface = EXCLUDED.surface, rating = EXCLUDED.rating, reasons = EXCLUDED.reasons, comment = EXCLUDED.comment, "
        "snapshot = EXCLUDED.snapshot, updated_at = now() RETURNING id",
        (
            db.new_id("fb"),
            plan["id"],
            plan["business_id"],
            surface,
            rating,
            reasons,
            comment,
            actor,
            Jsonb(snapshot),
        ),
    ).fetchone()
    return row["id"]


def link_ticket(conn: psycopg.Connection, feedback_id: str, ticket_id: str) -> None:
    conn.execute("UPDATE plan_feedback SET ticket_id = %s WHERE id = %s", (ticket_id, feedback_id))


def words(reasons: list[str], comment: str | None) -> str:
    """What a 👎 with contact-me puts on the ticket: the comment, else the reasons' labels."""
    return comment or ", ".join(REASONS[r] for r in reasons) or "The plan was not useful."
