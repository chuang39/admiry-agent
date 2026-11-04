"""Inbound e-mail (spec 2026-09-28 §5.1–§5.5, §6.3). Resend's `email.received` webhook lands here, is stored at
once (one row per Message-ID) and processed in the background; the content comes from the Receiving API; the
guards drop what no model should read; authentication is mail_auth's; the thread is found by the Reply-To token,
then by In-Reply-To / References; only the sender's own new words go on to mail_dispatch. Every row ends in a
named outcome; an unexpected exception leaves it `received` for email-retry (MAX_ATTEMPTS in all)."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from email.utils import getaddresses

import httpx
import psycopg
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from . import config, db, mail, mail_auth, mail_dispatch, mail_out

log = logging.getLogger(__name__)
router = APIRouter()
# Resend's Receiving API (GET /emails/receiving/:id), on the host of mail.URL: every Resend call, in or out, goes
# through mail.client(), the one client (plan P16)
RECEIVING_URL = str(httpx.URL(mail.URL).copy_with(path="/emails/receiving"))
MAX_TEXT = 1_000_000  # characters of text + html a message may carry (§5.3)
NEW_TEXT_MAX = 20_000  # what is stored of the sender's own words
MAX_ATTEMPTS = 3
RETRY_AFTER_MINUTES = 10
PLUS = re.compile(r"^([^+@]+)\+([0-9a-f]{32})@(.+)$")
ANGLE = re.compile(r"<[^>\s]+>")
# Quoted history (§5.5): the earliest marker ends the sender's own words. No DOTALL anywhere below: a header
# never crosses a blank line (a new paragraph), which is what let the old DOTALL patterns run past the sender's
# own words to a "wrote:"/"a écrit :"/"schrieb …:" several paragraphs down (finding I3). Gmail wraps "On <date>,
# <name> wrote:" onto a second physical line, so _HDR allows exactly one embedded newline -- no more -- and, to
# tell a real header from a sentence that merely starts with On/Le/Am and happens to end the line the same way,
# requires an @, a < or a digit somewhere in that span (a date or an address; plain prose rarely has either).
_HDR = r"(?=[^\n]*[@<0-9]|[^\n]*\n[^\n]*[@<0-9])[^\n]{0,300}?(?:\n[^\n]{0,300}?)?"
QUOTE_STARTS = (
    re.compile(rf"^>?\s*On {_HDR}wrote:[ \t]*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From: .+\r?\n(?:Sent|Date): .+$", re.MULTILINE),  # Date: too: Outlook for Mac has no Sent:
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(rf"^Le {_HDR}a écrit\s*:[ \t]*$", re.MULTILINE),
    re.compile(rf"^Am {_HDR}schrieb [^\n]{{0,200}}?:[ \t]*$", re.MULTILINE),
)
# The quote cues the own-address backstop needs on the same line as our address (new_text); a bare mention of the
# address is prose ("I wrote to hello@… yesterday"). The address in angle brackets (with or without mailto:) is the
# fourth cue, built per address in _quotes_us.
QUOTE_CUES = r"wrote:|a écrit\s*:|schrieb"
SIGNATURE = re.compile(r"^-- ?$", re.MULTILINE)
BLOCKQUOTE = re.compile(r"<blockquote\b.*?</blockquote>", re.IGNORECASE | re.DOTALL)
BREAKS = re.compile(r"<br\s*/?>|</p>|</div>|</li>|</tr>", re.IGNORECASE)
TAGS = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------- the webhook


@router.post("/hooks/email")
async def email_hook(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Resend's webhook. The raw body is read here (the signature covers the exact bytes); the row is stored in
    the thread pool and processed after the response (BackgroundTasks). No session, no CSRF: the signature is
    the authentication. Off (AGENT_EMAIL_INBOUND) = 404."""
    settings = config.load()
    if not settings.email_inbound:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        mail_auth.verify_svix(payload, headers, settings.resend_webhook_secret or "")
        event = json.loads(payload)
    except (mail_auth.BadSignature, ValueError):
        return JSONResponse({"error": "bad signature"}, status_code=400)
    kind = event.get("type") if isinstance(event, dict) else None
    if kind != "email.received":
        return JSONResponse({"received": True, "ignored": str(kind)})
    row_id = await run_in_threadpool(_store, settings, event)
    if row_id is not None:
        background.add_task(process, row_id)
    return JSONResponse({"received": True, "stored": row_id is not None})


def _store(settings: config.Settings, event: dict) -> str | None:
    with db.connect(settings.database_url) as conn:
        return store_event(conn, event)


def store_event(conn: psycopg.Connection, event: dict) -> str | None:
    """One `received` row per Message-ID; None (nothing stored) for a redelivery or a payload without the fields.
    A Message-ID Resend did not pass falls back to provider:<email_id> (plan P15)."""
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("email_id"), str) or not data["email_id"]:
        return None
    mid = data.get("message_id") if isinstance(data.get("message_id"), str) and data["message_id"] else None
    mid = mid or f"provider:{data['email_id']}"
    sender = mail_auth.sender({"from": data.get("from")}) or "unknown"
    to = [a for a in (data.get("to") or []) if isinstance(a, str)][:20]
    row_id = db.new_id("em")
    try:
        conn.execute(
            "INSERT INTO email_message (id, direction, message_id, provider_id, from_addr, to_addrs, subject, outcome) "
            "VALUES (%s, 'in', %s, %s, %s, %s, %s, 'received')",
            (row_id, mid[:998], data["email_id"], sender, to, str(data.get("subject") or "")[:500]),
        )
    except psycopg.errors.UniqueViolation:
        return None
    return row_id


# ---------------------------------------------------------------- processing


def set_outcome(conn: psycopg.Connection, row_id: str, outcome: str, intent: dict | None = None) -> None:
    conn.execute(
        "UPDATE email_message SET outcome = %s, intent = COALESCE(%s, intent) WHERE id = %s",
        (outcome, Jsonb(intent) if intent is not None else None, row_id),
    )


def process(row_id: str) -> None:
    """Take one `received` row to its outcome. Opens its own connection (a background task or the retry job)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        row = conn.execute(
            "UPDATE email_message SET attempts = attempts + 1 WHERE id = %s AND outcome = 'received' RETURNING *",
            (row_id,),
        ).fetchone()
        if row is None:
            return
        row = dict(row)
        try:
            _process(conn, settings, row)
        except Exception:  # one bad message must not stop the channel; the row waits for email-retry
            log.exception("e-mail %s not processed (attempt %s)", row_id, row["attempts"])
            if row["attempts"] >= MAX_ATTEMPTS:
                set_outcome(conn, row_id, "failed")
                mail_dispatch.notify_ops(
                    conn,
                    settings,
                    subject=f"[Admiry] e-mail {row_id} failed {MAX_ATTEMPTS} times",
                    paragraphs=[f"Message {row_id} from {row['from_addr']} could not be processed; see the logs."],
                )


def _process(conn: psycopg.Connection, settings: config.Settings, row: dict) -> None:
    sender = row["from_addr"]
    known = mail_dispatch.is_known(conn, sender)
    if over_ceiling(conn, sender, settings):  # every stored row regardless of outcome, before any API call (P5)
        set_outcome(conn, row["id"], ("refused:" if known else "dropped:") + "limit")
        return
    received = fetch_received(settings, row["provider_id"])
    reason = drop_reason(received, settings)
    if reason:
        set_outcome(conn, row["id"], "dropped:" + reason)
        return
    decision = mail_auth.decide(received, lambda: fetch_raw(received))
    if not decision.authenticated:
        if known:
            set_outcome(conn, row["id"], "refused:auth", {"auth": decision.reason})
            mail_dispatch.unverified_reply(conn, settings, row, received)
        else:
            set_outcome(conn, row["id"], "dropped:auth", {"auth": decision.reason})
        return
    limit = over_limit(conn, sender, settings)  # the spec's own limits, from authenticated rows only (I6)
    if limit:
        set_outcome(conn, row["id"], ("refused:" if known else "dropped:") + limit)
        return
    text = new_text(received.get("text"), received.get("html"), settings.email_address)[:NEW_TEXT_MAX]
    thread = find_thread(conn, received, settings)
    if thread is not None and not mail_dispatch.may_continue(conn, thread, sender):
        thread = None  # the token only locates; the sender decides (§4, §6.4) — checked before the breakers too
    if thread is not None:
        if thread_over_limit(conn, thread["id"], settings):
            set_outcome(conn, row["id"], "queued")
            mail_dispatch.to_operator(conn, settings, thread, row, why="limit", text=text)
            return
        if looped(conn, thread["id"], row["id"], text):
            set_outcome(conn, row["id"], "queued")
            mail_dispatch.to_operator(conn, settings, thread, row, why="loop", text=text)
            return
    conn.execute("UPDATE email_message SET new_text = %s WHERE id = %s", (text, row["id"]))
    mail_dispatch.handle(conn, settings, row, received, thread, text)


def fetch_received(settings: config.Settings, provider_id: str) -> dict:
    """GET /emails/receiving/:id — text, html, headers, authentication, message_id, the raw link (§5.1)."""
    with mail.client() as http:
        r = http.get(f"{RECEIVING_URL}/{provider_id}", headers={"Authorization": f"Bearer {settings.resend_api_key}"})
    if not r.is_success:
        raise RuntimeError(f"Receiving API answered {r.status_code} for {provider_id}")
    body = r.json()
    if not isinstance(body, dict):
        raise TypeError(f"Receiving API returned no object for {provider_id}")
    return body


def fetch_raw(received: dict) -> bytes | None:
    """The raw message from its signed download link; None when there is none or the download fails."""
    url = (received.get("raw") or {}).get("download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        with mail.client() as http:
            r = http.get(url)
    except httpx.HTTPError:
        return None
    return r.content if r.is_success else None


# ---------------------------------------------------------------- guards


def _headers(received: dict) -> dict[str, str]:
    return {str(k).lower(): (v if isinstance(v, str) else "") for k, v in (received.get("headers") or {}).items()}


def drop_reason(received: dict, settings: config.Settings) -> str | None:
    """auto | list | bounce | self | size, or None (§5.3). Every rule reads headers only; no model."""
    h = _headers(received)
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return "auto"
    if h.get("precedence", "").strip().lower() in ("bulk", "junk", "list") or "list-id" in h:
        return "list"
    sender = mail_auth.sender(received)
    local, domain = mail_out.split_address(sender)
    if local in ("mailer-daemon", "postmaster") or h.get("return-path", "x").strip() == "<>":
        return "bounce"
    ours_local, ours_domain = mail_out.split_address(settings.email_address)
    if domain == ours_domain and local.split("+", 1)[0] == ours_local:
        return "self"
    if len(received.get("text") or "") + len(received.get("html") or "") > MAX_TEXT:
        return "size"
    return None


CEILING_FACTOR = 5  # the coarse pre-fetch ceiling, as a multiple of the spec's own per-sender limits (I6/R16)


def over_ceiling(conn: psycopg.Connection, sender: str, settings: config.Settings) -> bool:
    """A coarse ceiling at CEILING_FACTOR times the spec's per-sender limits, over every stored row for `sender`
    regardless of outcome -- decided before any Receiving API call (plan P5), so a flood of forged mail (which
    the finer, authenticated-only over_limit() below cannot see until it is fetched) still never costs one."""
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE created_at > now() - interval '1 hour') AS hour, count(*) AS day "
        "FROM email_message WHERE direction = 'in' AND from_addr = %s AND created_at > now() - interval '1 day'",
        (sender,),
    ).fetchone()
    return (
        row["hour"] > CEILING_FACTOR * settings.email_per_sender_hour
        or row["day"] > CEILING_FACTOR * settings.email_per_sender_day
    )


def over_limit(conn: psycopg.Connection, sender: str, settings: config.Settings) -> str | None:
    """`limit` when this message takes the sender over the hourly or daily count (the row itself is counted), from
    rows that are authenticated or not yet decided -- a forged message (dropped:auth / refused:auth) never counts
    against the real sender's limit (finding I6): over_ceiling above is what still bounds a flood of those."""
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE created_at > now() - interval '1 hour') AS hour, count(*) AS day "
        "FROM email_message WHERE direction = 'in' AND from_addr = %s AND created_at > now() - interval '1 day' "
        "AND outcome NOT IN ('dropped:auth', 'refused:auth')",
        (sender,),
    ).fetchone()
    if row["hour"] > settings.email_per_sender_hour or row["day"] > settings.email_per_sender_day:
        return "limit"
    return None


def thread_over_limit(conn: psycopg.Connection, thread_id: str, settings: config.Settings) -> bool:
    """More inbound messages in this thread this hour than the limit (this one counted): the loop breaker's
    second rule (§6.3). Rows already bound to the thread count; this message is not bound yet, so + 1."""
    n = conn.execute(
        "SELECT count(*) AS n FROM email_message WHERE thread_id = %s AND direction = 'in' "
        "AND created_at > now() - interval '1 hour'",
        (thread_id,),
    ).fetchone()["n"]
    return n + 1 > settings.email_per_thread_hour


def _normal(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def looped(conn: psycopg.Connection, thread_id: str, row_id: str, text: str) -> bool:
    """The previous inbound message in the thread carried the same words (whitespace, case and punctuation aside)."""
    prev = conn.execute(
        "SELECT new_text FROM email_message WHERE thread_id = %s AND direction = 'in' AND id <> %s "
        "AND new_text IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (thread_id, row_id),
    ).fetchone()
    return bool(prev and _normal(prev["new_text"]) and _normal(prev["new_text"]) == _normal(text))


# ---------------------------------------------------------------- the sender's own words, the thread


def _flatten(html: str) -> str:
    text = BLOCKQUOTE.sub("", html)
    text = BREAKS.sub("\n", text)
    text = TAGS.sub("", text)
    return html_lib.unescape(text)


def _quotes_us(line: str, own_address: str) -> bool:
    """`line` quotes a message from `own_address`: the address as a whole token (never inside another address such
    as notify+hello@….evil.example) with a quote cue on the same line."""
    addr = re.escape(own_address)
    return bool(
        re.search(rf"(?<![\w.+-]){addr}(?![\w.-])", line, re.IGNORECASE)
        and re.search(rf"{QUOTE_CUES}|<(?:mailto:)?{addr}>", line, re.IGNORECASE)
    )


def new_text(text: str | None, html: str | None, own_address: str | None = None) -> str:
    """The sender's own words: the text part (or the HTML part flattened, quotes removed, when there is no text
    part), cut at the first quote marker, '>'-quoted lines and a '-- ' signature removed. Two backstops (finding
    I3), since no QUOTE_STARTS pattern matches every mail client: the first line carrying mail_out.FOOTER ends the
    sender's words unconditionally (Admiry's own sentence is never theirs), and so does the first line naming
    `own_address` (settings.email_address, passed by _process) beside a quote cue (_quotes_us) -- the address
    alone is ordinary prose and never cuts."""
    body = text if text and text.strip() else _flatten(html or "")
    body = body.replace("\r\n", "\n")
    cut = len(body)
    for pat in QUOTE_STARTS:
        m = pat.search(body)
        if m and m.start() < cut:
            cut = m.start()
    pos = 0
    for line in body.split("\n"):
        if pos >= cut:
            break
        if mail_out.FOOTER in line or (own_address and _quotes_us(line, own_address)):
            cut = pos
            break
        pos += len(line) + 1
    body = body[:cut]
    m = SIGNATURE.search(body)
    if m:
        body = body[: m.start()]
    lines = [ln.rstrip() for ln in body.split("\n") if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def find_thread(conn: psycopg.Connection, received: dict, settings: config.Settings) -> dict | None:
    """By the plus-address token on any recipient field first, then by In-Reply-To / References against the
    thread's own outbound Message-IDs — or Resend's ids, when the provider rewrote ours (plan P12)."""
    local = mail_out.split_address(settings.email_address)[0]
    fields = [*(received.get("to") or []), *(received.get("cc") or []), *(received.get("received_for") or [])]
    for _, addr in getaddresses([a for a in fields if isinstance(a, str)]):
        m = PLUS.match(addr.strip().lower())
        if m and m.group(1) == local:
            t = conn.execute("SELECT * FROM email_thread WHERE token = %s", (m.group(2),)).fetchone()
            if t:
                return dict(t)
    h = _headers(received)
    refs = ANGLE.findall(" ".join([h.get("in-reply-to", ""), h.get("references", "")]))
    if refs:
        ids = [r.strip("<>").split("@", 1)[0] for r in refs]
        t = conn.execute(
            "SELECT t.* FROM email_message m JOIN email_thread t ON t.id = m.thread_id "
            "WHERE m.direction = 'out' AND (m.message_id = ANY(%s) OR m.provider_id = ANY(%s)) "
            "ORDER BY m.created_at DESC LIMIT 1",
            (refs, ids),
        ).fetchone()
        if t:
            return dict(t)
    return None
