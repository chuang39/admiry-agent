"""What Admiry's admins read and do with support tickets (spec 2026-10-12 §4): the task list by tab, one ticket in full
with every event and the Help button's context, a reply by e-mail (support.reply; the caller flushes the outbox once
the transaction has committed) and a close with a resolution (support.set_status). Shared by the admin page
(ops_web.py) and the admin MCP (ops_mcp.py), which check who the admin is (AGENT_ADMIN_EMAILS); each action is an
audit row in their name (admin:<email>, class platform), committed with the change. Ticket text is a customer's (SP1
D148): the page shows it as text, and nothing here reads it as instructions."""

from __future__ import annotations

import psycopg
from fastmcp.exceptions import ToolError

from . import audit, business, plans, support, timefmt
from .util import iso

TABS = ("open", "waiting", "closed")
TAB_STATUSES = {"open": ("open", "awaiting_contact"), "waiting": ("answered",), "closed": ("closed",)}
CLOSED_DAYS = 30  # the Closed tab's reach, as /ops/review's decided list (ops_review.DECIDED_DAYS)
RESOLUTIONS = (  # support.RESOLUTIONS (D143), in the page's words
    ("resolved", "Resolved"),
    ("not_support", "Not a support question"),
    ("duplicate", "Duplicate"),
    ("no_reply", "No reply from the owner"),
    ("no_contact", "No way to reach them"),
)
ORIGINS = {"mcp": "Their assistant", "email": "E-mail", "feedback": "A 👎 on a plan"}
CONVERSATION = "The conversation"  # SP1's reads on the homepage, and the dock's link before the Help button
WEBSITE_CARD = "The website card"  # A3p-5 batch 2: "Ask Admiry's team" on the plan's website card (reason website)
NOT_FOUND = "No such ticket."
NEEDS_TEXT = "Write the reply first."
SNAPSHOT_TURNS = 10  # the opening snapshot's last turns on the detail
BAD_MAIL_CHARS = ',;<>"'  # review (Task 5): characters mail_out/Resend headers choke on in a To: address
ADDRESS_REFUSED = "This address has characters a mail can't go to."
ALREADY_CLOSED = "This ticket is already closed."


def origin(source: str, reason: str, words: dict | None) -> str:
    """Where a ticket came from, in the list's words (plan D12): the Help button and the page it was pressed on (the
    latest report's data.help), the owner's assistant, e-mail, a 👎, the website card, else the homepage conversation."""
    help_ = (words or {}).get("help")
    if help_:
        page = help_.get("page") or "/"
        return f"Help button · {page}" + ("" if help_.get("signed_in") else " · not signed in")
    if reason == "website":
        return WEBSITE_CARD
    return ORIGINS.get(source, CONVERSATION)


def _tab_clause(tab: str) -> tuple[str, list]:
    if tab == "closed":
        return f"t.status = 'closed' AND t.closed_at > now() - interval '{CLOSED_DAYS} days'", []
    return "t.status = ANY(%s)", [list(TAB_STATUSES[tab])]


def _name(conn: psycopg.Connection, business_id: str | None) -> str | None:
    if not business_id:
        return None
    return plans.name_for((business.latest_knowledge(conn, business_id) or (None, {}))[1])


def counts(conn: psycopg.Connection) -> dict:
    """How many tickets each tab holds."""
    out = {}
    for tab in TABS:
        clause, args = _tab_clause(tab)
        out[tab] = conn.execute(f"SELECT count(*) AS n FROM support_ticket t WHERE {clause}", args).fetchone()["n"]
    return out


def queue_view(conn: psycopg.Connection, tab: str = "open") -> dict:
    """The list for one tab (D11) -- open: open or waiting for an address; waiting: answered, waiting on the owner;
    closed: the last CLOSED_DAYS days -- newest activity first (updated_at, which every event touches): each row's
    origin, business (or the visitor's address, verified or not), first line and age; and every tab's count."""
    if tab not in TABS:
        raise ToolError(f"tab must be one of {', '.join(TABS)}")
    clause, args = _tab_clause(tab)
    found = conn.execute(
        "SELECT t.*, w.data AS words_data FROM support_ticket t LEFT JOIN LATERAL ("
        "SELECT data FROM ticket_event e WHERE e.ticket_id = t.id AND e.kind = 'owner_words' "
        "ORDER BY e.created_at DESC LIMIT 1) w ON true "
        f"WHERE {clause} ORDER BY t.updated_at DESC, t.created_at DESC",
        args,
    ).fetchall()
    rows = [
        {
            "id": r["id"],
            "status": r["status"],
            "resolution": r["resolution"],
            "origin": origin(r["source"], r["reason"], r["words_data"]),
            "source": r["source"],
            "reason": r["reason"],
            "business_id": r["business_id"],
            "name": _name(conn, r["business_id"]),
            "contact": r["contact_email"],
            "verified": r["contact_verified"],
            "first_line": r["summary"],
            "age": timefmt.when(r["updated_at"])["rel"],
            "updated_at": iso(r["updated_at"]),
        }
        for r in found
    ]
    return {"tab": tab, "rows": rows, "counts": counts(conn)}


def detail(conn: psycopg.Connection, ticket_id: str) -> dict | None:
    """One ticket in full: the row, the business now (name, claim state, the owner, its latest plan's version and status
    and that plan's latest review when it has one, D19), the contact (verified or not), every event in order with its
    data (a report's context under `help`), and the opening snapshot's last SNAPSHOT_TURNS turns (none, with the error's
    name, when SP1's snapshot failed). None for no such ticket."""
    t = conn.execute("SELECT * FROM support_ticket WHERE id = %s", (ticket_id,)).fetchone()
    if t is None:
        return None
    biz_id = t["business_id"]
    biz = business.row(conn, biz_id) if biz_id else None
    plan = plans.latest(conn, biz_id) if biz_id else None
    review = None
    if plan:
        review = conn.execute(
            "SELECT id, state FROM launch_review WHERE plan_id = %s ORDER BY created_at DESC LIMIT 1", (plan["id"],)
        ).fetchone()
    events = [
        {
            "id": e["id"],
            "kind": e["kind"],
            "actor": e["actor"],
            "via": e["via"],
            "body": e["body"],
            "data": e["data"] or {},
            "mail_state": e["mail_state"],
            "at": iso(e["created_at"]),
            "age": timefmt.when(e["created_at"])["rel"],
        }
        for e in conn.execute("SELECT * FROM ticket_event WHERE ticket_id = %s ORDER BY created_at", (ticket_id,))
    ]
    latest_words = next((e["data"] for e in reversed(events) if e["kind"] == "owner_words"), None)
    snapshot = t["snapshot"] if isinstance(t["snapshot"], dict) else {}
    return {
        "ticket": {
            **{k: t[k] for k in ("id", "business_id", "source", "reason", "status", "resolution", "summary")},
            "email_thread_id": t["email_thread_id"],
            "origin": origin(t["source"], t["reason"], latest_words),
            "created_at": iso(t["created_at"]),
            "updated_at": iso(t["updated_at"]),
            "closed_at": iso(t["closed_at"]),
            "first_reply_at": iso(t["first_reply_at"]),
            "age": timefmt.when(t["created_at"])["rel"],
        },
        "contact": {"email": t["contact_email"], "verified": t["contact_verified"]},
        "business": (
            {
                "id": biz_id,
                "name": _name(conn, biz_id),
                "claim_state": biz["claim_state"] if biz else None,
                "owner_email": biz["owner_email"] if biz else None,
                "plan": {"id": plan["id"], "version": plan["version"], "status": plan["status"]} if plan else None,
                "review": {"id": review["id"], "state": review["state"]} if review else None,
            }
            if biz_id
            else None
        ),
        "events": events,
        "snapshot_turns": (snapshot.get("turns") or [])[-SNAPSHOT_TURNS:],
        "snapshot_error": snapshot.get("error"),
        "resolutions": RESOLUTIONS,
    }


def _ticket(conn: psycopg.Connection, ticket_id: str) -> dict:
    t = conn.execute("SELECT * FROM support_ticket WHERE id = %s", (ticket_id,)).fetchone()
    if t is None:
        raise ToolError(NOT_FOUND)
    return t


def reply(
    conn: psycopg.Connection, ticket_id: str, *, admin: str, text: object, close: bool = False, ip: str | None = None
) -> dict:
    """A reply by e-mail in the admin's name (support.reply: a reply_out row in the outbox, the ticket answered; with
    `close`, closed as resolved once the mail is sent, SP1 §5.3) and its audit row, admin.ticket_reply, in one
    transaction (D13). The caller flushes the outbox after the commit (support_mail.flush): the mail must never go for
    a row that rolled back. A refusal of support.reply (no contact yet, closed, an earlier reply still waiting) is a
    ToolError with its sentence, nothing written -- as is a contact_email a mail header can't carry (review, Task 5)."""
    words = text.strip() if isinstance(text, str) else ""
    if not words:
        raise ToolError(NEEDS_TEXT)
    t = _ticket(conn, ticket_id)
    if t["contact_email"] and any(ch in t["contact_email"] for ch in BAD_MAIL_CHARS):
        raise ToolError(ADDRESS_REFUSED)
    business_id = t["business_id"]
    try:
        with conn.transaction():
            out = support.reply(conn, ticket_id, words, actor=f"admin:{admin}", close=close)
            audit.record(
                conn,
                tool="admin.ticket_reply",
                args={"ticket_id": ticket_id, "close": close, "chars": len(words)},
                result={"event_id": out["event_id"]},
                ok=True,
                business_id=business_id,
                actor=f"admin:{admin}",
                actor_class="platform",
                api_key_id=None,
                ip=ip,
            )
    except support.Refused as e:
        raise ToolError(str(e)) from e
    return {"ticket_id": ticket_id, "event_id": out["event_id"], "close": close}


def close(conn: psycopg.Connection, ticket_id: str, *, admin: str, resolution: object, ip: str | None = None) -> dict:
    """Close with a resolution (support.set_status, D143's words) and its audit row, admin.ticket_close, in one
    transaction (D13). A refusal (an unknown resolution, or a ticket the row read under lock shows already closed --
    review, Task 5: support.set_status is otherwise silent on a repeat close) is a ToolError with its sentence,
    nothing written."""
    business_id = _ticket(conn, ticket_id)["business_id"]
    try:
        with conn.transaction():
            locked = conn.execute("SELECT status FROM support_ticket WHERE id = %s FOR UPDATE", (ticket_id,)).fetchone()
            if locked["status"] == "closed":
                raise ToolError(ALREADY_CLOSED)
            row = support.set_status(
                conn,
                ticket_id,
                "closed",
                actor=f"admin:{admin}",
                resolution=resolution if isinstance(resolution, str) else None,
            )
            audit.record(
                conn,
                tool="admin.ticket_close",
                args={"ticket_id": ticket_id, "resolution": resolution},
                result={"status": row["status"]},
                ok=True,
                business_id=business_id,
                actor=f"admin:{admin}",
                actor_class="platform",
                api_key_id=None,
                ip=ip,
            )
    except support.Refused as e:
        raise ToolError(str(e)) from e
    return {"ticket_id": ticket_id, "status": row["status"], "resolution": row["resolution"]}
