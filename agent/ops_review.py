"""What Admiry's admins read and do with first launches in review (L1, spec 2026-10-10 §7, §8): the queue, one review in
full, and the three decisions -- approve and launch (the Reviewer's own pass, roles.reviewer.approve), return to the
owner with a note, reject with a note (launch_review.decide: the plan back to a draft, the note first on its list --
committed with the decision's own audit row, R25 -- then the owner e-mailed, roles.reviewer.tell_back). Shared by the
admin page (ops_web.py) and the admin MCP (ops_mcp.py), which check who the admin is (AGENT_ADMIN_EMAILS); each
decision is an audit row in their name (admin:<email>, class platform). The Critic's grades, score and verdict are
read here, for the team, as nowhere an owner reads (B0 §2.2's never column)."""

from __future__ import annotations

import psycopg
from fastmcp.exceptions import ToolError

from . import ads_refusal, audit, business, config, landing, launch_review, plans
from .roles import reviewer
from .util import iso

NOTE_MAX = 500
NEEDS_NOTE = "Say in a sentence what the owner should change."
NOT_FOUND = "No such review."
DECIDED_DAYS = 30
ADS_KEYS = ("monthly_usd", "headlines", "descriptions", "themes", "negatives", "landing_service")


def _row(conn: psycopg.Connection, r: dict) -> dict:
    channels = (((r["json"] or {}).get("sections") or {}).get("ads") or {}).get("channels") or []
    return {
        "id": r["id"],
        "business_id": r["business_id"],
        "name": plans.name_for((business.latest_knowledge(conn, r["business_id"]) or (None, {}))[1]),
        "owner_email": r["owner_email"],
        "plan_id": r["plan_id"],
        "version": r["version"],
        "monthly_usd": sum(float(c.get("monthly_usd") or 0) for c in channels),
        "state": r["state"],
        "hold_reason": r["hold_reason"],
        "approval": (r["approval"] or {}).get("kind"),
        "since": iso(r["created_at"]),
        "decided_by": r["decided_by"],
        "decided_at": iso(r["decided_at"]),
    }


def queue_view(conn: psycopg.Connection) -> dict:
    """The queue: every review not decided yet, oldest first (`open`), and those decided in the last DECIDED_DAYS,
    newest first (`decided`)."""
    found = conn.execute(
        "SELECT r.id, r.business_id, r.plan_id, r.state, r.hold_reason, r.approval, r.created_at, r.decided_by, "
        "r.decided_at, p.version, p.json, b.owner_email FROM launch_review r JOIN plan p ON p.id = r.plan_id "
        "JOIN business b ON b.id = r.business_id "
        f"WHERE r.state = ANY(%s) OR r.decided_at > now() - interval '{DECIDED_DAYS} days' ORDER BY r.created_at",
        (list(launch_review.OPEN),),
    ).fetchall()
    rows = [_row(conn, dict(r)) for r in found]
    decided = [r for r in rows if r["state"] not in launch_review.OPEN]
    return {
        "open": [r for r in rows if r["state"] in launch_review.OPEN],
        "decided": sorted(decided, key=lambda r: r["decided_at"] or "", reverse=True),
    }


def detail(conn: psycopg.Connection, review_id: str) -> dict | None:
    """One review in full: its row and steps, the plan as agreed (its lines and its page), its Google Search ads, the
    business's facts, the Critic's latest look at that version (summary, checks, grades, score, verdict), the page's
    report, and Google's refusals in this review with what Admiry did (`refusals`). None for no such review."""
    review = launch_review.get(conn, review_id)
    if review is None:
        return None
    biz = review["business_id"]
    plan = plans.get(conn, biz, review["plan_id"])
    knowledge = (business.latest_knowledge(conn, biz) or (None, {}))[1]
    look = conn.execute(
        "SELECT review, score, verdict, created_at FROM plan_review WHERE plan_id = %s AND score IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (review["plan_id"],),
    ).fetchone()
    channels = (((plan or {}).get("sections") or {}).get("ads") or {}).get("channels") or []
    google = next((c for c in channels if c.get("channel") == "google_search"), {})
    page = landing.current(conn, biz)
    return {
        "review": {**review, **{k: iso(review[k]) for k in ("created_at", "updated_at", "decided_at")}},
        "plan": (
            {
                "id": plan["id"],
                "version": plan["version"],
                "status": plan["status"],
                "lines": plan["lines"],
                "url": plans.plan_url(plan["id"]),
            }
            if plan
            else None
        ),
        "ads": {k: google.get(k) for k in ADS_KEYS},
        "business": {
            "id": biz,
            "name": plans.name_for(knowledge),
            "phone": knowledge.get("phone"),
            "address": (knowledge.get("listing") or {}).get("address"),
            "owner_email": business.row(conn, biz)["owner_email"],
        },
        "critic": {**dict(look), "created_at": iso(look["created_at"])} if look else None,
        # spec 2026-10-11 §6 (plan D14): what Google refused in this review and what Admiry did -- the admin's alone
        "refusals": ads_refusal.for_review(conn, review_id),
        "page": {"url": page["url"] if page else None, "report": landing.judged_report(conn, page) if page else None},
    }


def _open(conn: psycopg.Connection, review_id: str) -> dict:
    review = launch_review.get(conn, review_id)
    if review is None:
        raise ToolError(NOT_FOUND)
    if review["state"] not in launch_review.OPEN:
        raise ToolError(launch_review.DECIDED)
    return review


def approve(
    conn: psycopg.Connection, settings: config.Settings, review_id: str, *, admin: str, ip: str | None = None
) -> dict:
    """Approve and launch, in the admin's name: the Reviewer's own pass (roles.reviewer.approve). R24: the approval's
    own check may refuse it now (the page broke since) -- reviewer.approve then holds the review for a person and
    returns `{"held": sentence}` rather than raise; that must be said, never reported as launched, so this re-raises
    it as a ToolError (the page answers 409 with it, the MCP returns it) instead of handing the caller a "launch" that
    never happened."""
    out = reviewer.approve(conn, settings, _open(conn, review_id), by=f"admin:{admin}", ip=ip)
    if "held" in out:
        raise ToolError(out["held"])
    return out


def send_back(
    conn: psycopg.Connection,
    settings: config.Settings,
    review_id: str,
    *,
    admin: str,
    note: object,
    state: str,
    ip: str | None = None,
) -> dict:
    """Return to the owner (`returned`) or reject (`rejected`), with the admin's note (at most NOTE_MAX characters):
    the decision (launch_review.decide: the plan back to a draft with the note first on its list) and its audit row
    (admin.review_return or admin.review_reject) commit together, in one transaction (R25) -- a failed audit insert
    must never leave a decision no one is on record for. Only once that has committed is the owner e-mailed the note
    (roles.reviewer.tell_back): an e-mail for a decision that might still roll back would be wrong."""
    if state not in ("returned", "rejected"):
        raise ValueError(f"no such decision: {state!r}")
    words = " ".join(note.split())[:NOTE_MAX] if isinstance(note, str) else ""
    if not words:
        raise ToolError(NEEDS_NOTE)
    review = _open(conn, review_id)
    with conn.transaction():
        launch_review.decide(conn, review_id, state=state, by=f"admin:{admin}", note=words)
        audit.record(
            conn,
            tool="admin.review_return" if state == "returned" else "admin.review_reject",
            args={"review_id": review_id, "plan_id": review["plan_id"]},
            result={"note": words},
            ok=True,
            business_id=review["business_id"],
            actor=f"admin:{admin}",
            actor_class="platform",
            api_key_id=None,
            ip=ip,
        )
    reviewer.tell_back(conn, settings, review, state=state, note=words)
    return {state: words}
