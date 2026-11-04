"""Ticket mail (spec 2026-09-30 support-and-feedback §5, D141, D142): the outbox rows support.py writes -- the
acknowledgement, a person's reply, the ops notice -- sent once the caller has committed, and retried by the hourly
support-mail job (jobs.support_mail) alone. Gated by AGENT_SUPPORT and the Resend key, never by AGENT_EMAIL_INBOUND:
finding I1 stays true for the e-mail channel's own mail (mail_out.channel_gate)."""

from __future__ import annotations

import logging

import psycopg

from . import business, config, db, mail_out, support

log = logging.getLogger(__name__)
MAIL_ATTEMPTS = support.MAIL_ATTEMPTS  # tries per outbox row; after that the row stays `failed`
ACK = "Thanks — a person at Admiry will read this and reply to this address."
ACK_QUOTE = "What you told us:"
ACK_SUBJECT = "We got your message ({id})"
NOTICE_SUBJECT = "[Admiry] ticket {id} ({reason}, {source})"


def after_tool(result: dict) -> None:
    """mcp_app.run's `after` hook and the homepage's own paths: flush the ticket a result opened or appended to."""
    ticket = ((result or {}).get("data") or {}).get("support") or {}
    if ticket.get("ticket_id"):
        flush(ticket["ticket_id"])


def flush(ticket_id: str | None = None, *, business_id: str | None = None, retry: bool = False) -> dict:
    """Send the pending outbox rows of one ticket, or of every ticket of a business (the claim sites), on a connection
    of its own -- after the caller's commit, so the rows are there. `retry` also sends the rows that failed and have
    tries left: the hourly support-mail job alone passes it. A request's flush never retries a failed row, so during a
    Resend outage the owner's every message does not spend another of a row's MAIL_ATTEMPTS tries: they stay the
    job's, about an hour apart (spec §5.2). {sent, failed}; never raises: the ticket is committed, and the job sends
    whatever is left. While AGENT_SUPPORT is off it returns at once and touches no row (the claim sites call it whatever
    the switch says), so no row loses a try."""
    settings = config.load()
    counts = {"sent": 0, "failed": 0}
    if not settings.support:
        return counts
    states = ["pending", "failed"] if retry else ["pending"]
    try:
        with db.connect(settings.database_url) as conn:
            due = conn.execute(
                "SELECT e.id FROM ticket_event e JOIN support_ticket t ON t.id = e.ticket_id "
                "WHERE e.mail_state = ANY(%s) AND e.mail_attempts < %s "
                "AND (t.id = %s OR t.business_id = %s) ORDER BY e.created_at",
                (states, MAIL_ATTEMPTS, ticket_id, business_id),
            ).fetchall()
            for row in due:
                ok = _send(conn, settings, row["id"], states)
                if ok is not None:
                    counts["sent" if ok else "failed"] += 1
    except Exception:  # the ticket is committed; the support-mail job sends what is left
        log.warning("ticket mail not flushed (ticket %s, business %s)", ticket_id, business_id, exc_info=True)
    return counts


def _send(conn: psycopg.Connection, settings: config.Settings, event_id: str, states: list[str]) -> bool | None:
    """One outbox row, if it is still in one of `states` (flush's: pending, and failed only for the job's retry) with
    tries left: counted as a try first (a crash mid-send is still a try), sent, then marked (never over a `sent` mark
    another flush made first); a sent reply that asked to close closes the ticket unless it has moved on since
    (_close_if_still_answered), the mark and the close in one transaction. None when the row is no longer due (another
    flush took it, or failed it). A reply's address is resolved on every try (mail_dispatch.reply_address), never kept
    from the hand-off. The Resend Idempotency-Key is the event's id (the notice, which also names its Message-ID after
    it) or the row id derived from it (thread mail), so every try of a row is the same request and two flushes of one
    row are one mail."""
    ev = conn.execute(
        "UPDATE ticket_event SET mail_attempts = mail_attempts + 1 WHERE id = %s "
        "AND mail_state = ANY(%s) AND mail_attempts < %s RETURNING *",
        (event_id, states, MAIL_ATTEMPTS),
    ).fetchone()
    if ev is None:
        return None
    t = conn.execute("SELECT * FROM support_ticket WHERE id = %s", (ev["ticket_id"],)).fetchone()
    message_id = None
    if ev["kind"] == "notice":
        ok = mail_out.send_plain(
            settings,
            to=settings.ops_email or "",
            subject=NOTICE_SUBJECT.format(id=t["id"], reason=t["reason"], source=t["source"]),
            paragraphs=_notice(conn, settings, t),
            idempotency_key=ev["id"],
            message_id=ev["id"],  # the same on every try: a retry is byte for byte the same request
            gate=mail_out.support_gate(settings) if settings.ops_email else "AGENT_OPS_EMAIL unset",
        )
    else:
        from . import mail_dispatch  # mail_dispatch imports this module

        ack = ev["kind"] == "ack"
        # The acknowledgement goes on the ticket's own thread, to its contact. A reply goes on the thread it was made
        # for (email-reply's), else the ticket's own, to that thread's reply address as it is now: the channel's rule
        # (M1), so an address the owner allowed that asked on the thread is answered, and one revoked since is not.
        thread_id = t["email_thread_id"] if ack else ((ev["data"] or {}).get("thread_id") or t["email_thread_id"])
        thread = conn.execute("SELECT * FROM email_thread WHERE id = %s", (thread_id,)).fetchone()
        if thread is None:
            to = None
        else:
            to = t["contact_email"] if ack else mail_dispatch.reply_address(conn, dict(thread))
        if not to:
            ok = False
        else:
            out = mail_out.deliver_ticket(
                conn,
                settings,
                thread=dict(thread),
                to=to,
                subject=ACK_SUBJECT.format(id=t["id"]) if ack else mail_out.reply_subject(thread["subject"]),
                paragraphs=[ACK, ACK_QUOTE, ev["body"] or ""] if ack else (ev["body"] or "").split("\n\n"),
                row_id="em_" + ev["id"].removeprefix("te_"),
                sent_by="admiry" if ack else "ops",
            )
            ok, message_id = out["outcome"] == "sent", out["id"]
    with conn.transaction():
        conn.execute(
            "UPDATE ticket_event SET mail_state = %s, email_message_id = COALESCE(%s, email_message_id) "
            "WHERE id = %s AND mail_state <> 'sent'",
            ("sent" if ok else "failed", message_id, ev["id"]),
        )
        if ok and ev["kind"] == "reply_out" and (ev["data"] or {}).get("close"):
            _close_if_still_answered(conn, ev)
    return ok


def _close_if_still_answered(conn: psycopg.Connection, ev: dict) -> None:
    """A sent reply that asked to close closes its ticket as resolved -- but a failed send can leave the reply waiting
    while the owner writes again (the ticket is `open`) or a person reopens it, and closing then would drop the
    follow-up from the queue and hand the thread back to the channel. So the ticket is locked first (a racing append
    waits for us, or we see it) and closes only if it is still `answered` and no owner_words / reply_in event is newer
    than this reply. (support.reply refuses a second reply while this one waits, so `answered` is this one's.)"""
    row = conn.execute("SELECT status FROM support_ticket WHERE id = %s FOR UPDATE", (ev["ticket_id"],)).fetchone()
    newer = conn.execute(
        "SELECT 1 FROM ticket_event WHERE ticket_id = %s AND kind IN ('owner_words', 'reply_in') "
        "AND created_at > %s LIMIT 1",
        (ev["ticket_id"], ev["created_at"]),
    ).fetchone()
    if row is not None and row["status"] == "answered" and newer is None:
        support.set_status(conn, ev["ticket_id"], "closed", actor=ev["actor"], resolution="resolved")


def _notice(conn: psycopg.Connection, settings: config.Settings, t: dict) -> list[str]:
    """The ops notice (SP1 §5.4; spec 2026-10-12 §5): who, what they need, their latest words (at most support.QUOTE
    characters, like the channel's notices), and where to open the ticket -- /ops/tasks, the back office's task list."""
    name = None
    if t["business_id"]:
        name = ((business.latest_knowledge(conn, t["business_id"]) or (None, {}))[1]).get("name")
    words = conn.execute(
        "SELECT body FROM ticket_event WHERE ticket_id = %s AND kind IN ('owner_words', 'reply_in') "
        "ORDER BY created_at DESC LIMIT 1",
        (t["id"],),
    ).fetchone()
    latest = ((words["body"] if words else "") or "")[: support.QUOTE]
    contact = t["contact_email"] or "none yet"
    if t["contact_email"] and not t["contact_verified"]:
        contact += " (not verified)"
    return [
        f"Business: {name or '-'} ({t['business_id'] or 'no business'}); contact: {contact}.",
        f"Summary: {t['summary']}",
        f"Latest words: {latest or '(none)'}",
        f"Open it at {settings.web_base_url}/ops/tasks/{t['id']}",
    ]
