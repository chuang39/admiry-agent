"""Support tickets (spec 2026-09-30 support-and-feedback §3–§5, D130–D146): at most one ticket per business that is not
closed (D132), opened from the homepage (its Help button, spec 2026-10-12, and its reads), an outside assistant
(contact_support), the e-mail channel (to_operator) or a 👎 with contact-me; a later ask is appended to it. Everything
here runs in the caller's transaction, each function in a savepoint of its own, and writes only the database: the
acknowledgement, a person's reply and the ops notice are outbox rows (ticket_event.mail_state = 'pending') that
support_mail.flush sends once the caller has committed (D142)."""

from __future__ import annotations

import logging
import secrets

import psycopg
from psycopg.types.json import Jsonb

from . import db, envelope, transcript

log = logging.getLogger(__name__)

SOURCES = ("home", "mcp", "email", "feedback")
# "website" (A3p-5 batch 2, plan Q8, spec W12): Admiry's team putting Admiry's code on the owner's site, which
# A3p-6's building help uses too; the table checks no reason (schema.sql), so it needs no DDL
REASONS = ("help", "human", "dissatisfied", "other", "website")
RESOLUTIONS = ("resolved", "not_support", "duplicate", "no_reply", "no_contact")
MAX_WORDS = 4000  # an event's body
MAX_SUMMARY = 200
QUOTE = 500  # the owner's words quoted in the acknowledgement and the ops notice
ANON_PER_IP_DAY = 5  # §4.5: tickets a draft visitor's address may open a day
# spec 2026-10-12 §3.5: Help reports a day per e-mail address (reports_today); /api/help drops the next one, logged
REPORTS_PER_ADDRESS_DAY = 20
# §4.5: what one ticket takes an hour; more is dropped and logged. Counted per path: an ask that appends counts the
# ticket's owner words; an inbound message on a handed thread (on_inbound) counts owner words and replies together.
APPENDS_PER_HOUR = 20
MAIL_ATTEMPTS = 5  # tries per outbox row; after that the row stays `failed` (support_mail sends, reply waits on it)
SIGN_IN_URL = "/connect/google/start?next=%2F%3Fafter%3Dsignin"  # home.html's wall button: sign in, then claim
THREAD_SUBJECT = "Your request to Admiry ({id})"

# What the owner reads (spec §4.6); every sentence passes conftest.JARGON.
SIGN_IN_HINT = "Sign in with Google so they can reply to you by e-mail."
SAY_NEW = "I've passed this to the Admiry team. They'll reply by e-mail to {email}."
SAY_NEW_ANON = "I've passed this to the Admiry team. " + SIGN_IN_HINT
SAY_APPENDED = "Your earlier request is still with the Admiry team; I've added this to it."
SAY_PLAN_TAIL = "If you also want a change to the plan, tell me what to change."
LIMITED = "You have asked for a person several times today; the Admiry team has your earlier requests."
NOT_AVAILABLE = "Contacting Admiry support from here isn't available yet."


class Limited(Exception):
    """ANON_PER_IP_DAY reached: nothing was written."""


class Refused(Exception):
    """A change the ticket's state does not allow; the message says why."""


def source_of(key) -> str:
    """Where a ticket opened under this key started (§4.2): the e-mail channel's per-message keys, the homepage's own
    key (a draft, or the business key labelled `website` a claim upgraded it to), else an outside assistant."""
    if key.source == "email":
        return "email"
    if key.cls == "draft" or key.label == "website":
        return "home"
    return "mcp"


def open_or_append(
    conn: psycopg.Connection,
    *,
    business_id: str | None,
    source: str,
    reason: str,
    words: str,
    summary: str,
    actor: str,
    email_thread_id: str | None = None,
    contact_email: str | None = None,
    contact_verified: bool = True,
    context: str | None = None,
    report: dict | None = None,
    ip: str | None = None,
    draft: bool = False,
) -> dict:
    """Open the business's ticket (or the thread's, for an e-mail with no business, or the contact's, for a visitor
    with neither), or append `words` to the one that is not closed (D132). {ticket_id, status, appended,
    contact_email}. The contact is `contact_email` (an address the e-mail channel accepts, the one a signed-in visitor
    proved, or -- `contact_verified` False -- one a visitor typed on the Help button, spec 2026-10-12 §3.3) or the
    business's owner (Google verified it at the claim) -- never for an e-mail ticket, whose contact is its thread's
    reply address (the answer tool's read opens one before the channel binds the thread, and whoever asked may be an
    address the owner allowed: bind_thread gives it). Without one the ticket waits in awaiting_contact (D133), and a
    later ask that brings an address opens it (_take_contact). A new ticket with a verified contact queues the
    acknowledgement (not for e-mail: HANDED is one) and the ops notice; one with a typed, unverified contact queues the
    notice alone (§3.5: Admiry mails an address nobody verified nothing on its own), and a typed address finds only a
    business-less ticket that is itself unverified (_current), never an owner's. `report` is the Help button's context
    (the page, the plan, the browser, the last turns), kept on the owner_words event's data["help"]. Limited when a
    draft visitor's address, or that of a visitor with no business, opened ANON_PER_IP_DAY tickets today. The business
    row is locked (key share) before the ticket, the order claims.claim takes them in, so an ask racing a claim waits
    for it rather than deadlocking with it. Two racing requests for a business or a thread end with one ticket: the
    partial unique index refuses the second insert, which appends instead (a visitor with neither has no index, so two
    truly concurrent first asks can make two). A NUL in the words, the summary or the context is dropped before
    anything is written (_clean): the database refuses one."""
    if source not in SOURCES or reason not in REASONS:
        raise ValueError(f"unknown source {source!r} or reason {reason!r}")
    words = _clean(words).strip()[:MAX_WORDS]
    summary = (_clean(summary).strip() or words)[:MAX_SUMMARY]
    context = _clean(context) or None
    # a visitor with no business counts toward the address's daily limit like a draft does
    counted = draft or (business_id is None and bool(ip))
    with conn.transaction():
        owner = _owner_email(conn, business_id)  # the business first, then the ticket: the claim's order
        ticket = _current(conn, business_id, email_thread_id, contact_email, verified=contact_verified)
        if ticket is None:
            if counted and ip and _anon_today(conn, ip) >= ANON_PER_IP_DAY:
                raise Limited(LIMITED)
            try:
                with conn.transaction():
                    return _open(
                        conn,
                        business_id=business_id,
                        source=source,
                        reason=reason,
                        words=words,
                        summary=summary,
                        actor=actor,
                        email_thread_id=email_thread_id,
                        contact=contact_email or (None if source == "email" else owner),
                        verified=contact_verified if contact_email else True,  # the owner's: Google's at the claim
                        context=context,
                        report=report,
                        ip=ip,
                        draft=counted,
                    )
            except psycopg.errors.UniqueViolation:
                ticket = _current(conn, business_id, email_thread_id, contact_email, verified=contact_verified)
                if ticket is None:  # a racing request opened it first
                    raise
        return _append(
            conn,
            ticket,
            words,
            actor=actor,
            via=source,
            context=context,
            report=report,
            thread_id=email_thread_id,
            contact=contact_email,
            verified=contact_verified,
        )


def on_claim(conn: psycopg.Connection, business_id: str, email: str) -> str | None:
    """claims.claim's hook (D133; widened by spec 2026-10-12 §3.3): the business's ticket waiting for a contact takes
    the signer's address -- one Google just verified -- and opens, with its acknowledgement (not for an e-mail ticket)
    and the ops notice in the outbox; a ticket keyed by an address the visitor typed before signing in takes the
    verified one instead and gets its acknowledgement then (_verify). Its id, or None when neither was there (the
    unique index allows one ticket that is not closed per business)."""
    email = email.strip().lower()
    with conn.transaction():
        t = conn.execute(
            "SELECT * FROM support_ticket WHERE business_id = %s AND status <> 'closed' "
            "AND (status = 'awaiting_contact' OR NOT contact_verified) FOR UPDATE",
            (business_id,),
        ).fetchone()
        if t is None:
            return None
        if t["status"] == "awaiting_contact":
            conn.execute(
                "UPDATE support_ticket SET contact_email = %s, contact_verified = true WHERE id = %s", (email, t["id"])
            )
            _set(conn, t["id"], "open", actor=f"web:{email}")
            _opened(conn, t["id"], ack=t["source"] != "email")
        else:
            _verify(conn, t["id"], email, actor=f"web:{email}")
    return t["id"]


def bind_thread(conn: psycopg.Connection, ticket_id: str, thread_id: str, contact_email: str | None) -> None:
    """to_operator's hook when an e-mail answer's read already opened the ticket (§4.4), and an append's when its words
    come on a thread: the thread is the ticket's from now on. A ticket with none takes it, and with it the thread's
    address as its contact: who asked on it, so the ops notice names them (only an e-mail ticket the answer tool opened
    on this same message can have an address and no thread -- every other ticket with one got a thread with its
    acknowledgement). One that already holds another (its own acknowledgement's, say) keeps that one and its contact,
    and records this thread once in a `thread` event, so open_for_thread finds the ticket by either and closing hands
    both back (_release_threads). A ticket still waiting for a contact takes the thread's address and opens -- no
    acknowledgement: the channel's HANDED reply is one."""
    with conn.transaction():
        t = _row(conn, ticket_id, lock=True)
        if t["email_thread_id"] is None:
            # support_ticket_thread_open cannot refuse this: a thread another open ticket holds as its own is a thread
            # with no business (a business's thread goes to the business's one open ticket), and such a thread cannot
            # gain a business while its ticket is open -- it waits for a person, and the channel queues what comes on it
            conn.execute(
                "UPDATE support_ticket SET email_thread_id = %s, contact_email = COALESCE(%s, contact_email), "
                "updated_at = now() WHERE id = %s",
                (thread_id, contact_email, ticket_id),
            )
        elif not _holds(conn, ticket_id, thread_id):
            _event(conn, ticket_id, "thread", actor="admiry", data={"thread_id": thread_id})
        if t["status"] == "awaiting_contact" and contact_email:
            conn.execute("UPDATE support_ticket SET contact_email = %s WHERE id = %s", (contact_email, ticket_id))
            _set(conn, ticket_id, "open", actor="admiry")
            _opened(conn, ticket_id, ack=False)


def on_inbound(conn: psycopg.Connection, thread_id: str, row: dict, text: str) -> str | None:
    """A message on a thread that waits for a person (mail_dispatch.handle's operator branch, and to_operator's for the
    messages mail_in's loop and limit breakers catch, §4.4): a reply_in event on the ticket that holds the thread; an
    answered ticket goes back to open, with the ops notice. The ticket's id, or None when no open ticket holds the
    thread (a hand-off from before tickets). A ticket that already took APPENDS_PER_HOUR owner words and replies in the
    last hour takes no more: the message is logged and dropped -- nothing recorded, no status changed -- and the id is
    still returned, since the ticket does hold the thread."""
    with conn.transaction():
        t = open_for_thread(conn, thread_id, lock=True)
        if t is None:
            return None
        recent = conn.execute(
            "SELECT count(*) AS n FROM ticket_event WHERE ticket_id = %s AND kind IN ('owner_words', 'reply_in') "
            "AND created_at > now() - interval '1 hour'",
            (t["id"],),
        ).fetchone()["n"]
        if recent >= APPENDS_PER_HOUR:
            log.warning("ticket %s: reply dropped (%s owner messages in the last hour)", t["id"], recent)
            return t["id"]
        who = f"email:{row['from_addr']}"
        _event(
            conn,
            t["id"],
            "reply_in",
            actor=who,
            via="email",
            body=(text or "")[:MAX_WORDS],
            data={"thread_id": thread_id, "message_id": row["id"]},
        )
        if t["status"] == "answered":
            _set(conn, t["id"], "open", actor=who)
            _event(conn, t["id"], "notice", actor="admiry", mail_state="pending")
    return t["id"]


def set_status(
    conn: psycopg.Connection,
    ticket_id: str,
    status: str,
    *,
    actor: str,
    resolution: str | None = None,
    expect: str | None = None,
) -> dict:
    """open or closed (§3). Closing needs a resolution (D143) and hands every thread the ticket held back to the e-mail
    channel; reopening a closed ticket makes its thread wait for a person again. With `expect`, only a ticket that is
    still in that status under its lock changes: anything else is returned as it is (the support-mail job closes a
    ticket as no_contact only while it still waits for an address -- a claim may have opened it since the job looked).
    Refused: an unknown status or resolution, reopening a ticket with no contact, or while the business has another
    one open."""
    if status not in ("open", "closed") or (status == "closed") != (resolution is not None):
        raise Refused("status must be open, or closed with a resolution: " + ", ".join(RESOLUTIONS))
    if resolution is not None and resolution not in RESOLUTIONS:
        raise Refused("resolution must be one of " + ", ".join(RESOLUTIONS))
    with conn.transaction():
        t = _row(conn, ticket_id, lock=True)
        if t is None:
            raise Refused("no such ticket")
        if t["status"] == status or (expect is not None and t["status"] != expect):
            return t
        if status == "closed":
            _set(conn, ticket_id, "closed", actor=actor, resolution=resolution)
            _release_threads(conn, ticket_id)
        else:
            if not t["contact_email"]:
                raise Refused("this ticket has no contact yet")
            try:
                with conn.transaction():
                    _set(conn, ticket_id, "open", actor=actor)
            except psycopg.errors.UniqueViolation:
                raise Refused("another request for this business is open; add to that one instead") from None
            if t["status"] == "closed" and t["email_thread_id"]:
                conn.execute("UPDATE email_thread SET status = 'operator' WHERE id = %s", (t["email_thread_id"],))
        return _row(conn, ticket_id)


def reply(
    conn: psycopg.Connection,
    ticket_id: str,
    text: str,
    *,
    actor: str,
    close: bool = False,
    thread_id: str | None = None,
) -> dict:
    """A person's reply (§5.3): a reply_out event in the outbox, first_reply_at, status answered. It goes on
    `thread_id` (email-reply's thread; it must be one the ticket holds, and the event keeps it), else on the ticket's
    own thread, to that thread's reply address when it is sent (support_mail._send). With `close` the ticket closes as
    resolved once the mail is sent (support_mail.flush), unless it has moved on by then -- a failed send leaves it with
    the operator, as email-reply always did. Refused for an empty text, a thread that is not the ticket's, a ticket with
    no contact yet, a closed one, or while an earlier reply is still waiting in the outbox (pending, or failed with
    tries left): the hourly support-mail job retries that one, and a second reply now would reach the owner twice -- a
    run of email-reply after a failed send is such a reply."""
    text = (text or "").strip()
    if not text:
        raise Refused("the reply is empty")
    with conn.transaction():
        t = _row(conn, ticket_id, lock=True)
        if t is None:
            raise Refused("no such ticket")
        if thread_id is not None and not _holds(conn, ticket_id, thread_id):
            raise Refused("that thread is not this ticket's")
        if t["status"] == "closed":
            raise Refused("this ticket is closed")
        if t["status"] == "awaiting_contact" or not t["contact_email"]:
            raise Refused("this ticket has no contact yet")
        waiting = conn.execute(
            "SELECT 1 FROM ticket_event WHERE ticket_id = %s AND kind = 'reply_out' "
            "AND mail_state IN ('pending', 'failed') AND mail_attempts < %s LIMIT 1",
            (ticket_id, MAIL_ATTEMPTS),
        ).fetchone()
        if waiting:
            raise Refused("an earlier reply is still waiting to be sent; the support-mail job retries it")
        if thread_id is None and t["email_thread_id"] is None:
            _new_thread(conn, t)
        event_id = _event(
            conn,
            ticket_id,
            "reply_out",
            actor=actor,
            via="ops",
            body=text[:MAX_WORDS],
            data={"close": close, **({"thread_id": thread_id} if thread_id else {})},
            mail_state="pending",
        )
        conn.execute(
            "UPDATE support_ticket SET first_reply_at = COALESCE(first_reply_at, now()) WHERE id = %s", (ticket_id,)
        )
        if t["status"] != "answered":
            _set(conn, ticket_id, "answered", actor=actor)
    return {"event_id": event_id, "ticket_id": ticket_id}


def open_for_thread(conn: psycopg.Connection, thread_id: str, *, lock: bool = False) -> dict | None:
    """The ticket that is not closed and holds `thread_id`: its own thread, or one an append brought to it."""
    row = conn.execute(
        "SELECT * FROM support_ticket t WHERE t.status <> 'closed' AND " + _HOLDS + " "
        "ORDER BY t.created_at LIMIT 1" + (" FOR UPDATE" if lock else ""),
        {"th": thread_id},
    ).fetchone()
    return dict(row) if row else None


def reports_today(conn: psycopg.Connection, address: str, *, verified: bool) -> int:
    """Help reports (spec 2026-10-12 §3.5, review fix 2) of the last day keyed by `address`, counted apart by
    verification class: `verified=True` counts only reports on tickets this address now owns, verified (a signed-in
    or claimed caller's own daily count); `verified=False` counts only reports on tickets still unverified (a typed
    caller's own). Kept apart so a stranger typing an owner's own address 20 times cannot silently mute the real
    owner's reports -- Task 2's check for a verified caller runs against a count a typed one can never touch.
    What REPORTS_PER_ADDRESS_DAY bounds, checked by /api/help before anything is written."""
    address = address.strip().lower()
    return conn.execute(
        "SELECT count(*) AS n FROM ticket_event e JOIN support_ticket t ON t.id = e.ticket_id "
        "WHERE e.kind = 'owner_words' AND e.data ? 'help' AND e.created_at > now() - interval '1 day' "
        "AND t.contact_verified = %(v)s AND (t.contact_email = %(a)s OR e.data->'help'->>'email' = %(a)s)",
        {"a": address, "v": verified},
    ).fetchone()["n"]


def public(out: dict) -> dict:
    """What a tool result or the page gets: never the contact address."""
    return {k: out[k] for k in ("ticket_id", "status", "appended")}


def say_for(out: dict, *, planned: bool = False) -> str:
    """The owner's sentence for open_or_append's result (spec §4.6)."""
    if out["appended"]:
        text = SAY_APPENDED + (f" {SIGN_IN_HINT}" if out["status"] == "awaiting_contact" else "")
    elif out["status"] == "awaiting_contact":
        text = SAY_NEW_ANON
    else:
        text = SAY_NEW.format(email=out["contact_email"])
    return f"{text} {SAY_PLAN_TAIL}" if planned else text


def envelope_for(out: dict, *, planned: bool = False) -> dict:
    """The homepage's envelope for a ticket: the sentence, and the sign-in link while no address is known."""
    nxt = envelope.link(SIGN_IN_URL, "Sign in with Google") if out["status"] == "awaiting_contact" else envelope.NONE
    return envelope.make({"support": public(out)}, say=say_for(out, planned=planned), next=nxt)


# ---------------------------------------------------------------- inside


def _clean(text: str | None) -> str:
    """Words as the database can hold them: Postgres text and jsonb refuse a NUL, which a caller can put in any string
    (audit._storable does the same for the audit rows). A NUL is dropped, not refused: the rest is still an ask."""
    return (text or "").replace("\x00", "")


# Ticket `t` holds thread %(th)s: its own, or one an event carries (a bound `thread`, owner words or a reply that came
# on it, the thread a reply went on) -- what open_for_thread finds a ticket by and closing hands back (_release_threads)
_HOLDS = (
    "(t.email_thread_id = %(th)s OR EXISTS ("
    "SELECT 1 FROM ticket_event e WHERE e.ticket_id = t.id AND e.data->>'thread_id' = %(th)s))"
)


def _holds(conn, ticket_id: str, thread_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM support_ticket t WHERE t.id = %(id)s AND " + _HOLDS, {"id": ticket_id, "th": thread_id}
    ).fetchone()
    return row is not None


def _row(conn, ticket_id: str, *, lock: bool = False) -> dict | None:
    row = conn.execute(
        "SELECT * FROM support_ticket WHERE id = %s" + (" FOR UPDATE" if lock else ""), (ticket_id,)
    ).fetchone()
    return dict(row) if row else None


def _current(conn, business_id: str | None, thread_id: str | None, contact: str | None, *, verified: bool = True):
    """The ticket that is not closed for this business; for this thread when there is no business; and, for a visitor
    with neither, the business-less ticket of the same contact -- any ticket of the address when the caller proved it
    (a web session), only one keyed by an address nobody verified when the caller typed it (spec 2026-10-12 §3.5: a
    typed address never joins a verified ticket, so nobody puts words on an owner's own request). Locked."""
    row = conn.execute(
        "SELECT * FROM support_ticket WHERE status <> 'closed' AND ((%(b)s::text IS NOT NULL AND business_id = %(b)s) "
        "OR (%(b)s::text IS NULL AND %(t)s::text IS NOT NULL AND email_thread_id = %(t)s) "
        "OR (%(b)s::text IS NULL AND %(t)s::text IS NULL AND %(c)s::text IS NOT NULL "
        "AND business_id IS NULL AND contact_email = %(c)s AND (%(v)s::boolean OR NOT contact_verified))) "
        "ORDER BY contact_verified DESC, created_at LIMIT 1 FOR UPDATE",  # review fix 4: a signed-in ask's own
        # verified ticket never loses to an older unverified one of the same address
        {"b": business_id, "t": thread_id, "c": contact, "v": verified},
    ).fetchone()
    return dict(row) if row else None


def _owner_email(conn, business_id: str | None) -> str | None:
    """The business's owner, whose address Google verified at the claim (D133); None for a draft. The key-share lock
    conflicts with the claim's FOR UPDATE (not with plain updates of the row), so a claim racing this read waits, or is
    waited for, and the ticket never strands in awaiting_contact. open_or_append reads it before it locks the ticket:
    claims.claim holds the business before on_claim locks the ticket, so the two never wait on each other in a circle
    (a new acknowledgement thread's foreign key reads the business too)."""
    if not business_id:
        return None
    row = conn.execute("SELECT owner_email FROM business WHERE id = %s FOR KEY SHARE", (business_id,)).fetchone()
    return (row["owner_email"] or None) if row else None


def _anon_today(conn, ip: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM ticket_event WHERE kind = 'created' AND data->>'ip' = %s "
        "AND data->>'draft' = 'true' AND created_at > now() - interval '1 day'",
        (ip,),
    ).fetchone()["n"]


def _event(
    conn,
    ticket_id: str,
    kind: str,
    *,
    actor: str,
    via: str | None = None,
    body: str | None = None,
    data: dict | None = None,
    mail_state: str | None = None,
) -> str:
    """One ticket_event (append-only). clock_timestamp(), not now(): the events of one transaction keep their order."""
    event_id = db.new_id("te")
    conn.execute(
        "INSERT INTO ticket_event (id, ticket_id, kind, actor, via, body, data, mail_state, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())",
        (event_id, ticket_id, kind, actor, via, body, Jsonb(data or {}), mail_state),
    )
    conn.execute("UPDATE support_ticket SET updated_at = now() WHERE id = %s", (ticket_id,))
    return event_id


def _set(conn, ticket_id: str, status: str, *, actor: str, resolution: str | None = None) -> None:
    conn.execute(
        "UPDATE support_ticket SET status = %s, resolution = %s, "
        "closed_at = CASE WHEN %s::text = 'closed' THEN now() END, updated_at = now() WHERE id = %s",
        (status, resolution, status, ticket_id),
    )
    data = {"status": status, **({"resolution": resolution} if resolution else {})}
    _event(conn, ticket_id, "status", actor=actor, data=data)


def _open(
    conn,
    *,
    business_id,
    source,
    reason,
    words,
    summary,
    actor,
    email_thread_id,
    contact,
    verified,
    context,
    report,
    ip,
    draft,
) -> dict:
    """The new ticket, its first events, then its snapshot (built after the owner's words are written, so the words
    that opened it are in it), then -- with a contact -- the ops notice, and the acknowledgement only for a verified
    one."""
    ticket_id = db.new_id("tk")
    status = "open" if contact else "awaiting_contact"
    conn.execute(
        "INSERT INTO support_ticket (id, business_id, source, reason, status, contact_email, contact_verified, summary, "
        "email_thread_id, snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)",
        (ticket_id, business_id, source, reason, status, contact, verified, summary, email_thread_id),
    )
    _event(conn, ticket_id, "created", actor=actor, via=source, data={"ip": ip, "draft": True} if draft else {})
    _words(conn, ticket_id, words, actor=actor, via=source, context=context, report=report, thread_id=email_thread_id)
    snapshot = transcript.build(conn, business_id, thread_id=email_thread_id)
    conn.execute("UPDATE support_ticket SET snapshot = %s WHERE id = %s", (Jsonb(snapshot), ticket_id))
    if status == "open":
        _opened(conn, ticket_id, ack=source != "email" and verified)
    return _result(conn, ticket_id, appended=False)


def _words(conn, ticket_id: str, words: str, *, actor, via, context, thread_id, report=None) -> None:
    data = {}
    if context:
        data["context"] = context[:MAX_WORDS]
    if report:
        data["help"] = report  # the Help button's context (spec 2026-10-12 §3.3); transcript reads only `context`
    if thread_id:
        data["thread_id"] = thread_id  # closing the ticket hands this thread back too (_release_threads)
    _event(conn, ticket_id, "owner_words", actor=actor, via=via, body=words, data=data)


def _append(conn, t: dict, words: str, *, actor, via, context, report, thread_id, contact, verified) -> dict:
    if thread_id:  # first: the ticket keeps the thread even when the words are over the limit and dropped
        bind_thread(conn, t["id"], thread_id, contact)
    recent = conn.execute(
        "SELECT count(*) AS n FROM ticket_event WHERE ticket_id = %s AND kind = 'owner_words' "
        "AND created_at > now() - interval '1 hour'",
        (t["id"],),
    ).fetchone()["n"]
    if recent >= APPENDS_PER_HOUR:
        log.warning("ticket %s: owner words dropped (%s in the last hour)", t["id"], recent)
        _take_contact(
            conn, t["id"], contact, actor=actor, verified=verified
        )  # kept like the thread, whatever became of the words
        return _result(conn, t["id"], appended=True)
    _words(conn, t["id"], words, actor=actor, via=via, context=context, report=report, thread_id=thread_id)
    _take_contact(
        conn, t["id"], contact, actor=actor, verified=verified
    )  # after the words: the acknowledgement quotes the latest
    if _row(conn, t["id"])["status"] == "answered":
        _set(conn, t["id"], "open", actor=actor)
        _event(conn, t["id"], "notice", actor="admiry", mail_state="pending")
    return _result(conn, t["id"], appended=True)


def _take_contact(conn, ticket_id: str, contact: str | None, *, actor: str, verified: bool = True) -> None:
    """An ask that brings an address. A ticket still waiting for one takes it and opens with the ops notice -- and the
    acknowledgement only when the address is verified (a draft visitor who signed in since, D133; never for an e-mail
    ticket), what on_claim does at the claim and bind_thread does for an e-mail thread. An address Google verified (a
    web session) reaching a ticket keyed by one nobody did makes it the ticket's and sends the acknowledgement then
    (_verify, spec 2026-10-12 §3.3). A typed address reaching a ticket keyed by another typed one replaces it (the
    same browser's draft: the latest is the one meant, and a reply already made for the old address moves with it,
    review fix 1); one reaching a verified ticket changes nothing. Nothing without an address: the row is read
    again, since bind_thread may have opened it a moment ago."""
    t = _row(conn, ticket_id)
    if not contact:
        return
    if t["status"] == "awaiting_contact":
        conn.execute(
            "UPDATE support_ticket SET contact_email = %s, contact_verified = %s WHERE id = %s",
            (contact, verified, ticket_id),
        )
        _set(conn, ticket_id, "open", actor=actor)
        _opened(conn, ticket_id, ack=t["source"] != "email" and verified)
    elif not t["contact_verified"] and verified:
        _verify(conn, ticket_id, contact, actor=actor)
    elif not t["contact_verified"] and contact != t["contact_email"]:
        conn.execute(
            "UPDATE support_ticket SET contact_email = %s, updated_at = now() WHERE id = %s", (contact, ticket_id)
        )
        if t["email_thread_id"]:  # review fix 1: a reply already made for the old typed address follows it
            conn.execute(
                "UPDATE email_thread SET counterpart = %s WHERE id = %s AND counterpart = %s",
                (contact, t["email_thread_id"], t["contact_email"]),
            )
        _event(
            conn,
            ticket_id,
            "contact",
            actor=actor,
            data={"email": contact, "was": t["contact_email"], "verified": False},
        )


def _verify(conn, ticket_id: str, email: str, *, actor: str) -> None:
    """A ticket keyed by an address nobody verified meets one Google did -- the claim (on_claim) or a signed-in ask
    (_take_contact): the address is the ticket's, verified; the thread a person's earlier reply made for the old
    address now talks to the new one; a `contact` event records both; and SP1's acknowledgement goes now (spec
    2026-10-12 §3.3) -- once, and no second ops notice: a person has the ticket already. Not at all (review fix 3)
    once a person has already replied (`first_reply_at` set): nothing to acknowledge, the reply is the answer."""
    t = _row(conn, ticket_id)
    conn.execute(
        "UPDATE support_ticket SET contact_email = %s, contact_verified = true, updated_at = now() WHERE id = %s",
        (email, ticket_id),
    )
    if t["email_thread_id"]:
        conn.execute(
            "UPDATE email_thread SET counterpart = %s WHERE id = %s AND counterpart = %s",
            (email, t["email_thread_id"], t["contact_email"]),
        )
    _event(conn, ticket_id, "contact", actor=actor, data={"email": email, "was": t["contact_email"], "verified": True})
    if t["source"] != "email":
        t = _row(conn, ticket_id)
        if t["email_thread_id"] is None:
            _new_thread(conn, t)
        if t["first_reply_at"] is None:  # review fix 3: a person already replying needs no acknowledgement queued
            _event(
                conn,
                ticket_id,
                "ack",
                actor="admiry",
                body=_latest_words(conn, ticket_id)[:QUOTE],
                mail_state="pending",
            )


def _opened(conn, ticket_id: str, *, ack: bool) -> None:
    """A ticket just entered open with a contact: the acknowledgement (quoting the owner's latest words) on the
    ticket's thread, made now if it has none, and the ops notice -- both outbox rows."""
    t = _row(conn, ticket_id)
    if ack:
        if t["email_thread_id"] is None:
            _new_thread(conn, t)
        _event(
            conn, ticket_id, "ack", actor="admiry", body=_latest_words(conn, ticket_id)[:QUOTE], mail_state="pending"
        )
    _event(conn, ticket_id, "notice", actor="admiry", mail_state="pending")


def _new_thread(conn, t: dict) -> str:
    """The e-mail thread a ticket that did not start by e-mail talks on (§5.1): its counterpart is the contact, and it
    waits for a person (status operator), so the e-mail channel never answers on it by itself."""
    thread_id = db.new_id("et")
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, subject, status) "
        "VALUES (%s, %s, %s, %s, %s, 'operator')",
        (thread_id, t["business_id"], t["contact_email"], secrets.token_hex(16), THREAD_SUBJECT.format(id=t["id"])),
    )
    conn.execute("UPDATE support_ticket SET email_thread_id = %s WHERE id = %s", (thread_id, t["id"]))
    return thread_id


def _latest_words(conn, ticket_id: str) -> str:
    row = conn.execute(
        "SELECT body FROM ticket_event WHERE ticket_id = %s AND kind IN ('owner_words', 'reply_in') "
        "ORDER BY created_at DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return (row["body"] or "") if row else ""


def _release_threads(conn, ticket_id: str) -> None:
    """Closing hands every thread the ticket held -- its own, and any an append brought -- back to the e-mail channel
    (§5.3): status open, so the channel's agent answers what comes next."""
    conn.execute(
        "UPDATE email_thread SET status = 'open', unclear = 0 WHERE status = 'operator' AND id IN ("
        "SELECT email_thread_id FROM support_ticket WHERE id = %(t)s AND email_thread_id IS NOT NULL UNION "
        "SELECT data->>'thread_id' FROM ticket_event WHERE ticket_id = %(t)s AND data ? 'thread_id')",
        {"t": ticket_id},
    )


def _result(conn, ticket_id: str, *, appended: bool) -> dict:
    t = _row(conn, ticket_id)
    return {"ticket_id": ticket_id, "status": t["status"], "appended": appended, "contact_email": t["contact_email"]}
