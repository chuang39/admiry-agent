"""What a message means and what Admiry does about it (spec 2026-09-28 §3, §4, §5.6–§5.9, D53, D57, D58).

The state decides before any model call (plan P1): a new thread from an unknown address starts a consultation, an
open consultation takes the words as its answer, a ready draft gets the sign-in link, a ready claimed business gets
its plan, and only a planned session goes to the reader (llm.read_mail), whose intent this module turns into at
most one tool call. Tools run through home_api.call under a per-message key of the sender's authority (§4), minted
before the call and revoked once the reply is out (plan P3/P4). No tool of the spend class and no account-scope tool
is ever named in this module — test_mail_dispatch greps for them."""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import (
    audit,
    auth,
    business,
    config,
    consult,
    db,
    digest_links,
    home_api,
    keys,
    listings,
    llm,
    mail_grants,
    mail_out,
    plans,
    support,
    support_mail,
    tools_start,
)

log = logging.getLogger(__name__)
CONFIDENCE = 0.6  # below it the reader's intent is `unclear`
UNCLEAR_MAX = 2  # consecutive unclear messages before the operator
UNDO = re.compile(r"^\W*(undo|revert|put it back)\W*$", re.IGNORECASE)
INTRO_SHORT, INTRO_QUESTION = 25, 60  # plan P8
STARTS_PER_DAY = 3  # new anonymous consultations per sender per day (spec §6.3)
DRAFT_TTL = "7 days"  # spec §3.1 step 5; plan P3
SCOPES_OWNER = mail_grants.DEFAULT_SCOPES
ADJUST_FIELDS = ("monthly_budget_usd", "level", "radius_miles", "offer", "objective")

# Fixed owner sentences (every one passes conftest.JARGON: test_fixed_sentences_use_owner_words).
ONLY_YOU = mail_out.ONLY_YOU  # re-exported: mail_out.deliver appends this itself when others_on_copy is True
ATTACHMENTS = mail_out.ATTACHMENTS  # re-exported: mail_out.deliver appends this itself when has_attachments is True
INTRO = (
    "Admiry is an AI marketing agent for local and service businesses: tell me what your business does, where it "
    "is and what you want more of, and I will ask a few questions and write a plan. The consultation is free; "
    "nothing is published or spent without the owner's approval."
)
SIGN_IN = "Your plan is ready. Sign in once with the business's Google account to see it; then we continue here."
SIGNED_IN = "Signed in as {email}. Here is the plan in short. Reply with anything you want changed."
UNCLEAR = "I did not quite get that. Tell me in a few words what you would like."
HANDED = "I have passed this on to a person at Admiry; you will hear back by e-mail."
READ_ONLY = "This address can see the plan but not change it; the owner can change that in Settings."
EXPIRED = "That conversation expired. Tell me about the business again and I will start over."
NO_PLAN = "There is no plan yet. Tell me about the business and what you want more of, and I will start one."
CHANGE_PLAN = "To change your plan, open Admiry."  # a reply to a report, while no finished plan can take the change
UNDO_HINT = "Reply undo to put it back."
UNDO_DONE = "Put back: {what} a month."
UNDO_NOTHING = "Nothing to put back in this conversation."
WHICH = "You have more than one business with Admiry: {names}. Reply with the name of the one you mean."
CHOSEN = "Got it: {name}. What would you like?"
UNVERIFIED = "We could not verify this message came from you. You can do this on the website instead:"
UNVERIFIED_SUBJECT = "Admiry could not verify your message"  # never the possibly-forged message's own Subject (M3)
BUSY = "Admiry is busy right now; try again in a few minutes."
FULL = "Admiry's free setup is busy today; try again tomorrow."


@dataclass(frozen=True)
class Authority:
    kind: str  # unknown | owner | grant
    business_ids: tuple[str, ...]
    scopes: tuple[str, ...]
    grant_id: str | None

    @property
    def may_manage(self) -> bool:
        return "manage" in self.scopes


# ---------------------------------------------------------------- who


def is_known(conn: psycopg.Connection, sender: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM business WHERE owner_email = %s AND status = 'active' "
        "UNION ALL SELECT 1 FROM email_grant WHERE address = %s AND revoked_at IS NULL LIMIT 1",
        (sender, sender),
    ).fetchone()
    return r is not None


def authority(conn: psycopg.Connection, sender: str, business_id: str | None) -> Authority:
    """What `sender` may do (§4): on `business_id` when the thread names one, else across their businesses."""
    if business_id is not None:
        row = conn.execute(
            "SELECT owner_email FROM business WHERE id = %s AND status = 'active'", (business_id,)
        ).fetchone()
        if row and (row["owner_email"] or "").lower() == sender:
            return Authority("owner", (business_id,), SCOPES_OWNER, None)
        g = mail_grants.live_for(conn, business_id, sender)
        if g:
            return Authority("grant", (business_id,), tuple(g["scopes"]), g["id"])
        return Authority("unknown", (), (), None)
    owned = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM business WHERE owner_email = %s AND status = 'active' ORDER BY created_at DESC", (sender,)
        )
    ]
    if owned:
        return Authority("owner", tuple(owned), SCOPES_OWNER, None)
    granted = conn.execute(
        "SELECT g.id, g.business_id, g.scopes FROM email_grant g JOIN business b ON b.id = g.business_id "
        "WHERE g.address = %s AND g.revoked_at IS NULL AND b.status = 'active' ORDER BY g.created_at DESC",
        (sender,),
    ).fetchall()
    if granted:
        one = granted[0] if len(granted) == 1 else None
        return Authority(
            "grant",
            tuple(g["business_id"] for g in granted),
            tuple(one["scopes"]) if one else (),
            one["id"] if one else None,
        )
    return Authority("unknown", (), (), None)


def extend_draft(conn: psycopg.Connection, business_id: str, session_id: str) -> None:
    """A consultation by e-mail lives DRAFT_TTL: the draft key that keeps the business alive (jobs.claim_expire) and
    the session both get it (plan P3). The key's plaintext is never kept by the channel."""
    conn.execute(
        f"UPDATE api_key SET expires_at = now() + interval '{DRAFT_TTL}' "
        "WHERE business_id = %s AND class = 'draft' AND revoked_at IS NULL",
        (business_id,),
    )
    conn.execute(f"UPDATE consult_session SET expires_at = now() + interval '{DRAFT_TTL}' WHERE id = %s", (session_id,))


@contextlib.contextmanager
def message_key(
    conn: psycopg.Connection, business_id: str, *, draft: bool, scopes, grant_id: str | None, row_id: str | None
):
    """A key for one message (plan P3/P4): minted with the sender's authority, recorded on the message row, revoked
    on exit; its plaintext lives only in this frame."""
    plaintext, rec = keys.mint(
        conn,
        business_id=business_id,
        cls="draft" if draft else "business",
        scopes=tuple(scopes),  # never SCOPES_OWNER as a fallback: "unknown" authority is exactly empty scopes (D57)
        label="email",
        source="email",
        grant_id=grant_id,
    )
    if row_id:
        conn.execute("UPDATE email_message SET key_id = %s WHERE id = %s", (rec.id, row_id))
    if grant_id:
        mail_grants.touch(conn, grant_id)
    try:
        yield plaintext
    finally:
        keys.revoke(conn, rec.id, business_id)


def run_tool(name: str, args: dict, key: str) -> dict:
    """The tool's envelope or {"error": sentence}; a rate limit is a readable sentence too."""
    try:
        return home_api.call(name, args, key, ip="mail")
    except home_api.Limited:
        return {"error": BUSY}


# ---------------------------------------------------------------- the message


def handle(
    conn: psycopg.Connection, settings: config.Settings, row: dict, received: dict, thread: dict | None, text: str
) -> None:
    sender = row["from_addr"]
    others = _others_on_copy(received, settings)
    attachments = bool(received.get("attachments"))
    if thread is not None and not may_continue(conn, thread, sender):
        thread = None  # the token only locates; the sender decides (§4, §6.4)
    if thread is None:
        thread = _new_thread(conn, sender, received)
    _bind(conn, row, thread)
    if thread["status"] == "operator":
        if settings.support and (ticket_id := support.on_inbound(conn, thread["id"], row, text)):
            support_mail.flush(ticket_id)  # the owner's reply is on the ticket (spec 2026-09-30 §4.4)
        _finish(conn, row, "queued", {"intent": "queued"})
        return
    if not text:
        _reply(conn, settings, thread, sender, [UNCLEAR], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "empty"})
        return
    who = authority(conn, sender, thread["business_id"])
    if thread["business_id"] is None:
        if who.kind == "unknown":
            _start_anonymous(conn, settings, row, thread, sender, text, others, attachments)
        else:
            _choose_business(conn, settings, row, thread, sender, text, who, others, attachments)
        return
    biz = business.row(conn, thread["business_id"])
    if biz is None or biz["status"] != "active":
        conn.execute("UPDATE email_thread SET business_id = NULL, session_id = NULL WHERE id = %s", (thread["id"],))
        _reply(conn, settings, thread, sender, [EXPIRED], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "expired"})
        return
    if thread.get("kind") == "digest":  # a reply to a report: never an answer to a consultation, never the start of one
        _digest_reply(conn, settings, row, thread, biz, who, sender, text, others, attachments)
        return
    draft = biz["claim_state"] == "draft"
    session = _session(conn, thread, biz)
    status = session["status"] if session else None
    if status == "open":
        _answer(conn, settings, row, thread, biz, session, who, sender, text, others, attachments)
    elif status == "ready":
        if settings.support and _ready_support(conn, settings, row, thread, biz, who, text):
            return
        if draft:
            _wall(conn, settings, row, thread, biz, sender, others, attachments)
        else:
            _recommend(conn, settings, row, thread, biz, session, who, sender, others, attachments)
    elif status == "planned":
        _planned(conn, settings, row, thread, biz, who, sender, text, others, attachments)
    elif draft:
        conn.execute("UPDATE email_thread SET business_id = NULL, session_id = NULL WHERE id = %s", (thread["id"],))
        _reply(conn, settings, thread, sender, [EXPIRED], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "expired"})
    else:
        _start_keyed(conn, settings, row, thread, biz, who, sender, text, others, attachments)


def may_continue(conn, thread: dict, sender: str) -> bool:
    """Whether `sender` may act on `thread` at all (§4, §6.4): the plus-token or a References match only
    *locates* a thread; this decides whether the sender may use it. Checked in mail_in._process right after
    find_thread, before the loop/limit breakers (a stranger holding the token must never freeze or queue the
    owner's thread), and again here for any caller that bypasses _process."""
    biz = business.row(conn, thread["business_id"]) if thread["business_id"] else None
    if biz is None or not biz["owner_email"]:
        return sender == thread["counterpart"]
    return authority(conn, sender, biz["id"]).kind != "unknown"


def reply_address(conn: psycopg.Connection, thread: dict) -> str:
    """Where a reply that answers the thread itself, not one particular inbound message, should go (HANDED, the
    operator's own reply, M1): the `from_addr` of the most recent inbound message in `thread` whose sender
    `may_continue` still accepts, else the thread's original counterpart. A grant revoked, or a claim released,
    after the thread took that sender's words must not strand the reply with someone who no longer has authority
    over it -- `thread["counterpart"]` alone never updates once the thread is created."""
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT from_addr FROM email_message WHERE thread_id = %s AND direction = 'in' ORDER BY created_at DESC",
        (thread["id"],),
    ):
        addr = row["from_addr"]
        if addr in seen:
            continue
        seen.add(addr)
        if may_continue(conn, thread, addr):
            return addr
    return thread["counterpart"]


def _new_thread(conn, sender: str, received: dict, *, subject: str | None = None) -> dict:
    """`subject` overrides the header's own Subject (unverified_reply's fixed UNVERIFIED_SUBJECT, M3); by
    default the thread takes the message's own Subject, same as always."""
    who = authority(conn, sender, None)
    biz_id = who.business_ids[0] if len(who.business_ids) == 1 else None
    subj = subject if subject is not None else (str(received.get("subject") or "").strip()[:200] or None)
    r = conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, subject) VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (db.new_id("et"), biz_id, sender, secrets.token_hex(16), subj),
    ).fetchone()
    return dict(r)


def open_thread(
    conn, business_id: str, counterpart: str, subject: str, *, kind: str = "digest", session_id: str | None = None
) -> dict:
    """A thread Admiry starts itself (the daily digest's, plan 2026-10-07-agent-k2 Task 10): bound to the business,
    talking to `counterpart` (lower-cased), with its own reply token; `kind` tells the reply handler what it is."""
    r = conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, subject, kind, session_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (db.new_id("et"), business_id, counterpart.strip().lower(), secrets.token_hex(16), subject, kind, session_id),
    ).fetchone()
    return dict(r)


def _bind(conn, row: dict, thread: dict) -> None:
    conn.execute("UPDATE email_message SET thread_id = %s WHERE id = %s", (thread["id"], row["id"]))
    conn.execute("UPDATE email_thread SET last_in_at = now() WHERE id = %s", (thread["id"],))
    row["thread_id"] = thread["id"]


def _finish(conn, row: dict, outcome: str, intent: dict) -> None:
    from . import mail_in  # mail_in imports this module

    mail_in.set_outcome(conn, row["id"], outcome, intent)


def _reply(
    conn,
    settings,
    thread: dict,
    to: str,
    paragraphs: list[str],
    links=(),
    *,
    others: bool = False,
    attachments: bool = False,
    subject: str | None = None,
    intent=None,
) -> dict:
    return mail_out.deliver(
        conn,
        settings,
        thread=thread,
        to=to,
        paragraphs=paragraphs,
        links=list(links),
        others_on_copy=others,
        has_attachments=attachments,
        subject=subject,
        intent=intent,
    )


def _others_on_copy(received: dict, settings) -> bool:
    ours_local, ours_domain = mail_out.split_address(settings.email_address)
    fields = [a for a in [*(received.get("to") or []), *(received.get("cc") or [])] if isinstance(a, str)]
    for _, addr in getaddresses(fields):
        local, domain = mail_out.split_address(addr.strip().lower())
        if not (domain == ours_domain and local.split("+", 1)[0] == ours_local):
            return True
    return False


def _ik(row: dict, suffix: str = "") -> str:
    return f"email:{row['message_id']}" + (f":{suffix}" if suffix else "")


def _expired(session: dict) -> bool:
    return datetime.fromisoformat(session["expires_at"]) < datetime.now(timezone.utc)


def _session(conn, thread: dict, biz: dict) -> dict | None:
    """The thread's own session while it is live, else the business's current one (open/ready first, then the
    latest planned — a plan never expires with its session)."""
    if thread.get("session_id"):
        s = consult.get(conn, biz["id"], thread["session_id"])
        if s and s["status"] == "planned" or (s and s["status"] in ("open", "ready") and not _expired(s)):
            return s
    s = home_api._current_session(conn, biz["id"])
    if s is None or (s["status"] in ("open", "ready") and _expired(s)):
        return None
    conn.execute("UPDATE email_thread SET session_id = %s WHERE id = %s", (s["id"], thread["id"]))
    thread["session_id"] = s["id"]
    return s


def _name(conn, biz_id: str) -> str:
    latest = business.latest_knowledge(conn, biz_id)
    row = business.row(conn, biz_id) or {}
    return plans.name_for(latest[1] if latest else None) or row.get("slug") or biz_id


def _clear_unclear(conn, thread: dict) -> None:
    conn.execute("UPDATE email_thread SET unclear = 0 WHERE id = %s", (thread["id"],))


# ---------------------------------------------------------------- the states


def _starts_today(conn, sender: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM email_message WHERE direction = 'in' AND from_addr = %s "
        "AND intent->>'intent' = 'start' AND created_at >= date_trunc('day', now())",
        (sender,),
    ).fetchone()["n"]


def _start_anonymous(conn, settings, row, thread, sender, text, others, attachments) -> None:
    if len(text) < INTRO_SHORT or (text.endswith("?") and len(text) < INTRO_QUESTION):
        _reply(conn, settings, thread, sender, [INTRO], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "intro"})
        return
    if _starts_today(conn, sender) >= STARTS_PER_DAY:
        _reply(conn, settings, thread, sender, [FULL], others=others, attachments=attachments)
        _finish(conn, row, "refused:limit", {"intent": "start_limit"})
        return
    try:
        env = tools_start.begin_anonymous(text, None, "en-US", None, ip=sender)
    except auth.RateLimited:
        _reply(conn, settings, thread, sender, [FULL], others=others, attachments=attachments)
        _finish(conn, row, "refused:limit", {"intent": "start"})
        return
    except (ToolError, auth.Denied) as e:
        _reply(conn, settings, thread, sender, [str(e)], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "start", "error": True})
        return
    data = env["data"]
    extend_draft(conn, data["business_id"], data["session_id"])
    conn.execute(
        "UPDATE email_thread SET business_id = %s, session_id = %s WHERE id = %s",
        (data["business_id"], data["session_id"], thread["id"]),
    )
    thread.update(business_id=data["business_id"], session_id=data["session_id"])
    if env["next"]["kind"] == "question":
        _reply(conn, settings, thread, sender, [env["say"]], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "start"})
    else:  # everything pre-answered from the words: ready at once — still a `start`, so STARTS_PER_DAY counts it
        _wall(
            conn,
            settings,
            row,
            thread,
            business.row(conn, data["business_id"]),
            sender,
            others,
            attachments,
            lead=env["say"],
            intent="start",
        )


def _choose_business(conn, settings, row, thread, sender, text, who, others, attachments) -> None:
    options = [(bid, _name(conn, bid)) for bid in who.business_ids]
    lowered = text.lower()
    match = next((bid for bid, name in options if len(name) >= 3 and name.lower() in lowered), None)
    if match is None:
        _reply(
            conn,
            settings,
            thread,
            sender,
            [WHICH.format(names=", ".join(n for _, n in options))],
            others=others,
            attachments=attachments,
        )
        _finish(conn, row, "answered", {"intent": "which"})
        return
    conn.execute("UPDATE email_thread SET business_id = %s WHERE id = %s", (match, thread["id"]))
    _reply(
        conn,
        settings,
        thread,
        sender,
        [CHOSEN.format(name=dict(options)[match])],
        others=others,
        attachments=attachments,
    )
    _finish(conn, row, "answered", {"intent": "chose", "business_id": match})


def _wall(
    conn, settings, row, thread, biz, sender, others, attachments, *, lead: str | None = None, intent: str = "answer"
) -> None:
    token = mail_grants.mint_link(conn, thread["id"], biz["id"])
    url = f"{settings.web_base_url}/m/{token}"  # a browser page with sign-in: the web host (spec 2026-09-29 §3)
    paragraphs = [lead, SIGN_IN] if lead else [SIGN_IN]
    _reply(
        conn,
        settings,
        thread,
        sender,
        paragraphs,
        [("Sign in to see your plan", url)],
        others=others,
        attachments=attachments,
    )
    _finish(conn, row, "answered", {"intent": intent, "ready": True})


def _fit_answer(text: str) -> str:
    """`text` cut to at most consult.MAX_OTHER characters: at the last blank line (a paragraph boundary) at or
    before the limit when there is one, else a hard cut. An e-mail reply -- a short answer plus a client's
    auto-appended disclaimer paragraph, say -- can run well past 500 characters; consult.parse refuses more
    outright (finding I4), so mail_dispatch must fit it first rather than dead-end the operator."""
    if len(text) <= consult.MAX_OTHER:
        return text
    cut = text.rfind("\n\n", 0, consult.MAX_OTHER)
    return text[:cut] if cut > 0 else text[: consult.MAX_OTHER]


def _answer(conn, settings, row, thread, biz, session, who, sender, text, others, attachments) -> None:
    draft = biz["claim_state"] == "draft"
    qid = consult.pending(session)[0]["id"]
    with message_key(conn, biz["id"], draft=draft, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key:
        env = run_tool(
            "answer",
            {"session_id": session["id"], "answers": {qid: {"other": _fit_answer(text)}}, "idempotency_key": _ik(row)},
            key,
        )
        if "error" in env:
            _reply(conn, settings, thread, sender, [env["error"]], others=others, attachments=attachments)
            _finish(conn, row, "answered", {"intent": "answer", "error": True})
            return
        asked = (env.get("data") or {}).get("support")
        if asked:  # the words asked for a person (spec 2026-09-30 §4.4): the answer's read opened the ticket
            to_operator(conn, settings, thread, row, why="support", text=text, ticket_id=asked["ticket_id"])
            _finish(conn, row, "queued", {"intent": "support"})
            return
        if env["data"].get("ready"):
            if draft:
                _wall(conn, settings, row, thread, biz, sender, others, attachments)
                return
            env2 = run_tool("recommend", {"session_id": session["id"], "idempotency_key": _ik(row, "recommend")}, key)
            _plan_reply(conn, settings, row, thread, sender, env2, others, attachments, intent="recommend")
            return
    _reply(conn, settings, thread, sender, [env["say"]], others=others, attachments=attachments)
    _finish(conn, row, "answered", {"intent": "answer"})


def _ready_support(conn, settings, row, thread, biz, who, text: str) -> bool:
    """A message on a ready consultation (no model call today, spec 2026-09-30 D131): read_support first, under the cap
    and recorded like read_mail. True when the words asked for a person and the thread went to the operator; False
    otherwise, or when the read cannot run (a sender without the read scope, the cap, the model) -- today's path."""
    draft = biz["claim_state"] == "draft"
    with (
        message_key(conn, biz["id"], draft=draft, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key,
        auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": "mail"}),
    ):
        try:
            principal = auth.require(conn, "read")
            auth.check_llm_cap(conn, principal)
            read = llm.generator().read_support(text)
        except (ToolError, llm.LLMError):
            return False
        _record(conn, principal, "mail.read", text, read.model_dump())
    if read.support == "none":
        return False
    to_operator(
        conn, settings, thread, row, why="support", text=text, reason=read.support, summary=read.support_summary
    )
    _finish(conn, row, "queued", {"intent": "support"})
    return True


def _recommend(conn, settings, row, thread, biz, session, who, sender, others, attachments) -> None:
    # A claim can commit between authority() (handle, before biz is (re-)read) and here; draft=False is hard-coded
    # below, so an unknown sender must never reach message_key with it (never a scoped key for a stranger).
    if who.kind == "unknown":
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "recommend", "denied": True})
        return
    with message_key(conn, biz["id"], draft=False, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key:
        env = run_tool("recommend", {"session_id": session["id"], "idempotency_key": _ik(row)}, key)
    _plan_reply(conn, settings, row, thread, sender, env, others, attachments, intent="recommend")


def _plan_reply(
    conn, settings, row, thread, sender, env: dict, others, attachments, *, intent: str, extra: list[str] = ()
) -> None:
    if "error" in env:
        _reply(conn, settings, thread, sender, [env["error"]], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": intent, "error": True})
        return
    url = env["data"].get("plan_url")
    _reply(
        conn,
        settings,
        thread,
        sender,
        [env["say"], *extra],
        [("Your plan", url)] if url else (),
        others=others,
        attachments=attachments,
    )
    _clear_unclear(conn, thread)
    _finish(conn, row, "answered", {"intent": intent})


def _start_keyed(conn, settings, row, thread, biz, who, sender, text, others, attachments) -> None:
    """A known sender's business with no consultation and no plan: their words open one (plan P14)."""
    if not who.may_manage:
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "start", "denied": True})
        return
    with message_key(conn, biz["id"], draft=False, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key:
        env = run_tool(
            "start", {"text": text, "website_url": None, "locale": "en-US", "known_facts": None, "handoff": None}, key
        )
    if "error" in env:
        _reply(conn, settings, thread, sender, [env["error"]], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "start", "error": True})
        return
    conn.execute("UPDATE email_thread SET session_id = %s WHERE id = %s", (env["data"]["session_id"], thread["id"]))
    _reply(conn, settings, thread, sender, [env["say"]], others=others, attachments=attachments)
    _finish(conn, row, "answered", {"intent": "start"})


def _record(conn, principal: auth.Principal, tool: str, text: str, result) -> None:
    """One mail.read / mail.answer row: the model ran outside a tool, and auth.check_llm_cap counts these (P10)."""
    audit.record(
        conn,
        tool=tool,
        args={"text": text},
        result=result,
        ok=True,
        business_id=principal.business_id,
        actor=principal.actor,
        actor_class=principal.key.cls,
        api_key_id=principal.key.id,
        ip=principal.ip,
    )


def _planned(conn, settings, row, thread, biz, who, sender, text, others, attachments) -> None:
    if UNDO.match(text):
        _undo(conn, settings, row, thread, biz, who, sender, others, attachments)
        return
    latest, cards = home_api._planned_cards(conn, biz)
    if latest is None:
        _reply(conn, settings, thread, sender, [NO_PLAN], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "no_plan"})
        return
    # Same race as _recommend: draft=False is hard-coded below, so an unknown sender must never reach message_key.
    if who.kind == "unknown":
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "planned", "denied": True})
        return
    with (
        message_key(conn, biz["id"], draft=False, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key,
        auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": "mail"}),
    ):
        try:
            principal = auth.require(conn, "read")
            auth.check_llm_cap(conn, principal)
            read = llm.generator().read_mail(text, cards)
        except (ToolError, llm.LLMError) as e:
            _reply(conn, settings, thread, sender, [str(e)], others=others, attachments=attachments)
            _finish(conn, row, "answered", {"intent": "read", "error": True})
            return
        _record(conn, principal, "mail.read", text, read.model_dump())
        if settings.support and read.support != "none":
            to_operator(
                conn, settings, thread, row, why="support", text=text, reason=read.support, summary=read.support_summary
            )
            _finish(conn, row, "queued", {"intent": "support"})
            return
        # words that call the Google Maps place linked not theirs, for a business with a listing its owner confirmed
        # (plan 2026-10-03-agent-listing-not-mine-words, D4): unlinked in place of any intent; nothing linked, as ever
        if read.listing_not_mine and home_api.listing_linked(conn, biz["id"]):
            _not_mine(conn, settings, row, thread, who, sender, read, key, others, attachments)
            return
        intent = read.intent if read.confidence >= CONFIDENCE else "unclear"
        if intent == "adjust":
            if _adjust(conn, settings, row, thread, who, sender, read, latest, key, others, attachments):
                return
            intent = "unclear"
        if intent == "plan":
            env = run_tool("get_plan", {}, key)
            _plan_reply(conn, settings, row, thread, sender, env, others, attachments, intent="plan")
            return
        if intent == "connect":
            _connect(conn, settings, row, thread, biz, sender, cards, others, attachments)
            return
        if intent == "question":
            _question(
                conn,
                settings,
                row,
                thread,
                biz,
                principal,
                sender,
                read.question or text,
                latest,
                cards,
                others,
                attachments,
            )
            return
        if intent == "unclear":
            _unclear(conn, settings, row, thread, sender, text, others, attachments)
            return
    to_operator(conn, settings, thread, row, why="other", text=text)
    _finish(conn, row, "queued", {"intent": "other"})


def _digest_reply(conn, settings, row, thread, biz, who, sender, text, others, attachments) -> None:
    """A reply to a daily report (plan 2026-10-07-agent-k2 Task 15; Dg plan Task 12): read with the report's numbers
    and the day's look as far as an owner may read it, never as an answer to a consultation and never the start of one.
    P17: the report is read while its row is `ready` or `failed` too (Resend may have taken it before the row said so;
    the owner's reply proves it arrived). A change needs a plan the owner finished; what Admiry cannot place goes to a
    person, and every such reply carries the way back to the report."""
    from .roles import analyst  # local: the Analyst reads the digest's modules, which reach this one

    dg = conn.execute(
        "SELECT day, body FROM digest WHERE thread_id = %s AND mode = 'on' AND status IN ('ready', 'failed', 'sent')",
        (thread["id"],),
    ).fetchone()
    report = dg["body"] if dg else None
    day_look = (
        conn.execute("SELECT * FROM insight WHERE business_id = %s AND day = %s", (biz["id"], dg["day"])).fetchone()
        if dg
        else None
    )
    look = analyst.owner_view(settings, day_look)
    url = digest_links.report_url(settings, biz["id"], dg["day"]) if dg else f"{settings.web_base_url}/"
    back = [("Open Admiry", url)]
    session = home_api._current_session(conn, biz["id"])
    planned = session is not None and session["status"] == "planned"
    if planned and UNDO.match(text):
        _undo(conn, settings, row, thread, biz, who, sender, others, attachments)
        return
    # Same race as _planned: draft=False is hard-coded below, so an unknown sender must never reach message_key.
    if who.kind == "unknown":
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "report", "denied": True})
        return
    latest, cards = home_api._planned_cards(conn, biz)
    with (
        message_key(conn, biz["id"], draft=False, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key,
        auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": "mail"}),
    ):
        try:
            principal = auth.require(conn, "read")
            auth.check_llm_cap(conn, principal)
            read = llm.generator().read_mail(text, {"report": report, "insight": look, "plan": cards})
        except (ToolError, llm.LLMError) as e:
            _reply(conn, settings, thread, sender, [str(e)], others=others, attachments=attachments)
            _finish(conn, row, "answered", {"intent": "read", "error": True})
            return
        _record(conn, principal, "mail.read", text, read.model_dump())
        if settings.support and read.support != "none":
            to_operator(
                conn,
                settings,
                thread,
                row,
                why="support",
                text=text,
                reason=read.support,
                summary=read.support_summary,
                links=back,
            )
            _finish(conn, row, "queued", {"intent": "support"})
            return
        if read.listing_not_mine and home_api.listing_linked(conn, biz["id"]):
            _not_mine(conn, settings, row, thread, who, sender, read, key, others, attachments)
            return
        intent = read.intent if read.confidence >= CONFIDENCE else "unclear"
        if intent == "adjust":
            if not planned or latest is None:  # a consultation under way, or none finished: the change waits for it
                _reply(conn, settings, thread, sender, [CHANGE_PLAN], back, others=others, attachments=attachments)
                _clear_unclear(conn, thread)
                _finish(conn, row, "answered", {"intent": "adjust", "planned": False})
                return
            if _adjust(conn, settings, row, thread, who, sender, read, latest, key, others, attachments):
                return
            intent = "unclear"
        if intent == "plan":
            env = run_tool("get_plan", {}, key)
            _plan_reply(conn, settings, row, thread, sender, env, others, attachments, intent="plan")
            return
        if intent == "connect" and cards is not None:
            _connect(conn, settings, row, thread, biz, sender, cards, others, attachments)
            return
        if intent in ("question", "connect"):  # with no plan yet, how to connect is a question the material answers
            extra = {"report": report, "insight": look}
            _question(
                conn,
                settings,
                row,
                thread,
                biz,
                principal,
                sender,
                read.question or text,
                latest,
                cards,
                others,
                attachments,
                extra=extra,
            )
            return
        if intent == "unclear":
            _unclear(conn, settings, row, thread, sender, text, others, attachments, links=back)
            return
    to_operator(conn, settings, thread, row, why="other", text=text, links=back)
    _finish(conn, row, "queued", {"intent": "other"})


def _connect(conn, settings, row, thread, biz, sender, cards, others, attachments) -> None:
    from . import tools_consult  # local, as home_api's: mcp_app orders the tool modules' imports

    rows = cards["plan"]["channels"]
    way = tools_consult.google_way(rows)
    _reply(
        conn,
        settings,
        thread,
        sender,
        # the plan answer's own sentence on how Google Search runs for this business (walk fixes 1); a plan
        # without it says how its places run from the balance, never Google's (final review A5)
        [way] if way is not None else tools_consult.balance_ways(rows),
        # the setup step of THIS thread's business, like the ads/wallet tools' connect_url: /connect?setup=
        # lost `setup` over sign-in and 303'd a signed-in owner to their newest business, not this one -- only
        # where there is an own account to link (final review A6: never under the sentence that it waits)
        [("Connect Google Ads", f"{settings.web_base_url}/?setup={biz['id']}")]
        if tools_consult.links_own_account(rows)
        else [],
        others=others,
        attachments=attachments,
    )
    _clear_unclear(conn, thread)
    _finish(conn, row, "answered", {"intent": "connect"})


def _adjust(conn, settings, row, thread, who, sender, read, latest, key: str, others, attachments) -> bool:
    """The change the words name, made on the latest plan under the message's own key (READ_ONLY for an address that
    may only read); True when it replied. False when the words name no field: the caller reads them as unclear."""
    fields = {k: v for k in ADJUST_FIELDS if (v := getattr(read, k)) is not None}
    if not fields:
        return False
    if not who.may_manage:
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "adjust", "denied": True})
        return True
    env = run_tool("adjust", {"plan_id": latest["id"], "idempotency_key": _ik(row), **fields}, key)
    if "error" not in env:
        conn.execute(
            "UPDATE email_thread SET last_change = %s WHERE id = %s",
            (
                Jsonb({"tool": "adjust", "plan_before": latest["id"], "plan_after": env["data"]["plan_id"]}),
                thread["id"],
            ),
        )
    _plan_reply(conn, settings, row, thread, sender, env, others, attachments, intent="adjust", extra=[UNDO_HINT])
    return True


def _not_mine(conn, settings, row, thread, who, sender, read, key: str, others, attachments) -> None:
    """The listing unlinked by the command every unlink runs (update_business with google_listing null, D5) under the
    message's own key, when the sender may manage the business; READ_ONLY otherwise. The reply is the command's say and
    how to link the right place, and that a change the same words named was not made (as change_plan says it). Unlinked
    meanwhile by another path, the command refuses and changes nothing: the reply says nothing is linked, as
    change_plan's does (Task 2 round 1)."""
    if not who.may_manage:
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "listing_not_mine", "denied": True})
        return
    env = run_tool("update_business", {"patch": {"google_listing": None}, "idempotency_key": _ik(row)}, key)
    lost = env.get("error") == listings.NOTHING_LINKED
    if lost:  # unlinked meanwhile: a reply, as change_plan's
        env = {"say": listings.NOTHING_LINKED}
    if "error" in env:
        _reply(conn, settings, thread, sender, [env["error"]], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "listing_not_mine", "error": True})
        return
    if not lost:  # an unlink is a change the owner may write "undo" after, and means that, not an earlier budget change
        conn.execute("UPDATE email_thread SET last_change = NULL WHERE id = %s", (thread["id"],))
    named = any(getattr(read, k) is not None for k in ADJUST_FIELDS)
    said = env["say"] + listings.LINK_AGAIN + (listings.NOTHING_ELSE if named else "")
    _reply(conn, settings, thread, sender, [said], others=others, attachments=attachments)
    _clear_unclear(conn, thread)
    _finish(conn, row, "answered", {"intent": "listing_not_mine"})


def _question(
    conn,
    settings,
    row,
    thread,
    biz,
    principal,
    sender,
    question,
    latest,
    cards,
    others,
    attachments,
    *,
    extra: dict | None = None,
) -> None:
    """`extra`: what a reply to a report adds (the report's numbers, the day's look); there, `latest` and `cards` are
    None when the business has no plan from a consultation yet."""
    material = {
        "cards": cards,
        "rationale": latest["sections"].get("rationale") if latest else None,
        "knowledge": (business.latest_knowledge(conn, biz["id"]) or (None, {}))[1],
        "ways": plans.ways(cards["plan"]["channels"]) if cards else [],  # as `ask` reads them (walk fixes 1)
        **(extra or {}),
    }
    from .roles import strategist  # local, as the Analyst's import here: the roles reach modules that reach this one

    told = strategist.for_answer(settings, material["rationale"])  # capability layer K3: the files the plan cites
    if told:
        material["trade_knowledge"] = told
    try:
        auth.check_llm_cap(conn, principal)
        ans = llm.generator().answer_question(question, material)
    except (ToolError, llm.LLMError) as e:
        _reply(conn, settings, thread, sender, [str(e)], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "question", "error": True})
        return
    _record(conn, principal, "mail.answer", question, ans.model_dump())
    if ans.grounded and ans.text.strip():
        _reply(conn, settings, thread, sender, [ans.text.strip()], others=others, attachments=attachments)
        _clear_unclear(conn, thread)
        _finish(conn, row, "answered", {"intent": "question"})
        return
    to_operator(conn, settings, thread, row, why="question", text=question)
    _finish(conn, row, "queued", {"intent": "question"})


def _unclear(conn, settings, row, thread, sender, text, others, attachments, *, links=()) -> None:
    """`links` ride on the reply and on the hand-off (a reply to a report: the way back to it)."""
    n = thread["unclear"] + 1
    conn.execute("UPDATE email_thread SET unclear = %s WHERE id = %s", (n, thread["id"]))
    if n >= UNCLEAR_MAX:
        to_operator(conn, settings, thread, row, why="unclear", text=text, links=links)
        _finish(conn, row, "queued", {"intent": "unclear"})
        return
    _reply(conn, settings, thread, sender, [UNCLEAR], links, others=others, attachments=attachments)
    _finish(conn, row, "answered", {"intent": "unclear"})


def _undo(conn, settings, row, thread, biz, who, sender, others, attachments) -> None:
    change = thread.get("last_change") or {}
    latest = plans.latest(conn, biz["id"])
    before = plans.get(conn, biz["id"], change["plan_before"]) if change.get("tool") == "adjust" else None
    if before is None or latest is None or latest["id"] != change.get("plan_after"):
        _reply(conn, settings, thread, sender, [UNDO_NOTHING], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "undo", "nothing": True})
        return
    if not who.may_manage:
        _reply(conn, settings, thread, sender, [READ_ONLY], others=others, attachments=attachments)
        _finish(conn, row, "answered", {"intent": "undo", "denied": True})
        return
    c, budget = before["sections"]["consultation"], plans.total_usd(before["sections"])  # every channel's amount
    args = {
        "plan_id": latest["id"],
        "idempotency_key": _ik(row),
        "monthly_budget_usd": budget,
        "objective": c["objective"],
    }
    if c["geo"].get("radius_miles"):
        args["radius_miles"] = float(c["geo"]["radius_miles"])
    if c.get("offer"):
        args["offer"] = c["offer"]
    with message_key(conn, biz["id"], draft=False, scopes=who.scopes, grant_id=who.grant_id, row_id=row["id"]) as key:
        env = run_tool("adjust", args, key)
    if "error" not in env:
        conn.execute("UPDATE email_thread SET last_change = NULL WHERE id = %s", (thread["id"],))
        env = {**env, "say": f"{UNDO_DONE.format(what=plans.usd(budget))} {env['say']}"}
    _plan_reply(conn, settings, row, thread, sender, env, others, attachments, intent="undo")


# ---------------------------------------------------------------- the operator, the unverified sender


def to_operator(
    conn,
    settings,
    thread: dict,
    row: dict,
    *,
    why: str,
    text: str | None = None,
    reason: str | None = None,
    summary: str | None = None,
    ticket_id: str | None = None,
    links=(),
) -> None:
    """The thread stops here: status operator (every later message is queued without a reply), one HANDED to the
    counterpart, one notice to the operator (§5.9). With AGENT_SUPPORT on (spec 2026-09-30 §4.4) the hand-off is a
    support ticket -- opened here, or the one `ticket_id` names (an answer's read opened it) bound to this thread -- made
    first, so a database error leaves the thread as it was and the retry redoes the whole hand-off; then the thread
    goes to the operator, HANDED goes out (it stays the acknowledgement) and the ticket's own notice, which replaces
    notify_ops, is sent. A message on a thread that already waits for a person is the owner's reply on its ticket
    (mail_in's loop and limit breakers come here for those). Off: exactly as before. `links` ride on HANDED (a reply
    to a report: the way back to it)."""
    _bind(conn, row, thread)
    conn.execute("UPDATE email_message SET intent = %s WHERE id = %s", (Jsonb({"intent": why}), row["id"]))
    words = text if text is not None else row.get("new_text") or ""
    if thread["status"] == "operator":  # already waiting for a person: the row is queued, nothing more is sent
        if settings.support and (ticket_id := support.on_inbound(conn, thread["id"], row, words)):
            support_mail.flush(ticket_id)  # a message the breakers caught is still the owner's reply (spec §4.4)
        return
    if settings.support:
        contact = reply_address(conn, thread)
        if ticket_id is not None:
            support.bind_thread(conn, ticket_id, thread["id"], contact)
        else:
            ticket_id = support.open_or_append(
                conn,
                business_id=thread.get("business_id"),
                source="email",
                reason=reason or "other",
                words=words or "(no words)",
                summary=summary or f"An e-mail was handed to a person ({why})",
                actor=f"email:{row['from_addr']}",
                email_thread_id=thread["id"],
                contact_email=contact,
            )["ticket_id"]
        conn.execute("UPDATE email_thread SET status = 'operator' WHERE id = %s", (thread["id"],))
        thread["status"] = "operator"
        _reply(conn, settings, thread, contact, [HANDED], links)
        support_mail.flush(ticket_id)
        return
    conn.execute("UPDATE email_thread SET status = 'operator' WHERE id = %s", (thread["id"],))
    thread["status"] = "operator"
    _reply(conn, settings, thread, reply_address(conn, thread), [HANDED], links)
    notify_ops(
        conn,
        settings,
        subject=f"[Admiry] e-mail thread {thread['id']} needs a reply ({why})",
        paragraphs=[
            f"From {row['from_addr']}; business {thread.get('business_id') or '-'}; thread {thread['id']}.",
            f"Last message: {words[:500]}" if words else "Last message: (empty)",
            "On the box: python -m agent.ops email-queue, then email-reply <thread id> --text-file <file>.",
        ],
    )


def unverified_reply(conn, settings, row: dict, received: dict) -> None:
    """A known sender whose message failed authentication (§4's last row): one link a day, nothing executes.
    A fixed subject throughout (M3): the message that failed authentication may be forged, so its own Subject
    header must never end up in a DKIM-signed Admiry mail or on the thread it starts."""
    sender = row["from_addr"]
    n = conn.execute(
        "SELECT count(*) AS n FROM email_message WHERE direction = 'out' AND %s = ANY(to_addrs) "
        "AND intent->>'kind' = 'unverified' AND created_at > now() - interval '1 day'",
        (sender,),
    ).fetchone()["n"]
    if n:
        return
    thread = _new_thread(conn, sender, received, subject=UNVERIFIED_SUBJECT)
    _bind(conn, row, thread)
    _reply(
        conn,
        settings,
        thread,
        sender,
        [UNVERIFIED],
        [("Admiry", f"{settings.web_base_url}/")],
        subject=UNVERIFIED_SUBJECT,
        intent={"kind": "unverified"},
    )


def notify_ops(conn, settings, *, subject: str, paragraphs: list[str]) -> bool:
    if not settings.ops_email:
        return False
    return mail_out.send_plain(
        settings, to=settings.ops_email, subject=subject, paragraphs=paragraphs, idempotency_key=db.new_id("ops")
    )
