"""Admiry writes (spec 2026-09-28 §5.8, D59–D61): one sender address (settings.mail_from), a Reply-To that carries
the thread's plus-address token, the same thread (In-Reply-To / References), no Auto-Submitted header (every Admiry
mail invites a reply; the loop guard is ours), and FOOTER on every message. Resend through mail.send, Admiry's one
Resend client (plan P16): POST /emails with an Idempotency-Key. A failure is the out row's outcome and a log line,
never an exception the caller's own state rolls back on; MailError (mail.MailError) is raised only by send() itself.
Tests replace mail.client() with an httpx.MockTransport client (conftest `resend`)."""

from __future__ import annotations

import html as html_lib
import logging
import re

import psycopg
from psycopg.types.json import Jsonb

from . import config, db, mail

log = logging.getLogger(__name__)
MailError = mail.MailError  # the one client's error (P16): Resend refused the message or could not be reached
FOOTER = "Admiry never asks for passwords, codes or card numbers by e-mail."
ONLY_YOU = "Admiry replied only to you."
ATTACHMENTS = "I cannot open attachments yet; please put what matters in the text."
DEFAULT_SUBJECT = "Your marketing plan"


def split_address(address: str) -> tuple[str, str]:
    """("hello", "agent.admiry.ai") for hello@agent.admiry.ai."""
    local, _, domain = address.partition("@")
    return local, domain


def reply_address(address: str, token: str) -> str:
    """The thread's Reply-To: hello+<token>@domain (sub-addressing, D59)."""
    local, domain = split_address(address)
    return f"{local}+{token}@{domain}"


def reply_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    if not s:
        return DEFAULT_SUBJECT
    return s if s.lower().startswith("re:") else f"Re: {s}"


def compose(paragraphs: list[str], links: list[tuple[str, str]] = ()) -> tuple[str, str]:
    """(text, html) with the same words: the paragraphs, one line per link ("label: url"), then FOOTER."""
    lines = [p.strip() for p in paragraphs if p and p.strip()]
    text = "\n\n".join([*lines, *[f"{label}: {url}" for label, url in links], FOOTER])
    parts = [f"<p>{html_lib.escape(p)}</p>" for p in lines]
    parts += [
        f'<p><a href="{html_lib.escape(url, quote=True)}">{html_lib.escape(label)}</a></p>' for label, url in links
    ]
    parts.append(f'<p style="color:#5b6172;font-size:0.9em">{html_lib.escape(FOOTER)}</p>')
    return text, "".join(parts)


def send(payload: dict, *, idempotency_key: str) -> str:
    """`payload` (one recipient) through mail.send, the one Resend client (P16); returns Resend's id. Not
    configured, no answer, a 5xx, a 4xx or an answer without an id -> MailError (the Idempotency-Key makes a retry
    safe for 24 h on Resend's side)."""
    provider_id = mail.send(
        payload["to"][0],
        payload["subject"],
        payload["text"],
        html=payload.get("html"),
        headers=payload.get("headers"),
        reply_to=payload.get("reply_to"),
        idempotency_key=idempotency_key,
        from_addr=payload.get("from"),
    )
    if not provider_id:  # None: no RESEND_API_KEY at send time; "": a 2xx without an id
        raise MailError("RESEND_API_KEY unset" if provider_id is None else "Resend answered without an id")
    return provider_id


def _payload(settings: config.Settings, *, to: str, subject: str, text: str, html: str, headers: dict) -> dict:
    return {
        "from": settings.mail_from,  # D59: Admiry <hello@…> unless AGENT_MAIL_FROM overrides it
        "to": [to],
        "subject": subject,
        "text": text,
        "html": html,
        "headers": headers,
    }


def _mask_link_token(text: str, settings: config.Settings) -> str:
    """The stored copy only: `<web_base_url>/m/<token>` becomes `.../m/…` (finding I7 -- the /m/{token}
    capability must not sit in email_message.new_text, in plaintext, for 90 days). The sent mail (`text` itself,
    used for the payload and the returned dict) is never touched by this."""
    for prefix in (
        f"{settings.web_base_url}/m/",  # must match mail_dispatch._wall's link
        f"{settings.web_base_url}/unsubscribe/",  # the daily digest's links (digest_links.unsubscribe_url)
    ):
        text = re.sub(re.escape(prefix) + r"\S+", prefix + "…", text)
    return text


def channel_gate(settings: config.Settings) -> str | None:
    """Why the e-mail channel's own mail may not go out now (finding I1), or None."""
    return None if settings.email_inbound else "the channel is off"


def support_gate(settings: config.Settings) -> str | None:
    """Why ticket mail may not go out now (spec 2026-09-30 D141): AGENT_SUPPORT decides, whatever the channel says."""
    return None if settings.support else "support is off"


def digest_gate(settings: config.Settings) -> str | None:
    """Why a digest may not go out now (digest spec 2026-09-30 §8): AGENT_DIGEST decides, whatever the channel says."""
    return None if settings.digest != "off" else "the digest is off"


def deliver_core(
    conn: psycopg.Connection,
    settings: config.Settings,
    *,
    thread: dict,
    to: str,
    subject: str,
    text: str,
    html: str,
    gate: str | None,
    reply_to: str | None,
    row_id: str | None = None,
    idempotency_key: str | None = None,
    extra_headers: dict | None = None,
    sent_by: str = "admiry",
    intent: dict | None = None,
) -> dict:
    """One message in `thread` to `to`, recorded as an out row (outcome sent | failed): the core every Admiry mail on a
    thread goes through (spec 2026-09-30 §5.2 -- `deliver`, `deliver_ticket`, the daily digest's `deliver_digest`).
    `gate` is why this mail may not go out now (None = it may); `reply_to` None sends no Reply-To. In-Reply-To is the
    thread's last inbound Message-ID and References every Message-ID the thread has seen (plan P12); the Message-ID is
    ours. A caller-supplied `row_id` makes a retry update that same row and repeat its Message-ID; the Resend
    Idempotency-Key is `idempotency_key`, else the row id. A row already `sent` stays sent with its provider id: a
    losing duplicate try (two flushes of one outbox row, the later one refused) never marks a mail that went as failed;
    the result is still that try's own. Never raises: a failure is the row and a log line, and the result's `error` is
    Resend's refusal (str of the MailError) when it refused or could not be reached -- None when the mail was sent or
    gated (the daily digest pages on the first quota refusal)."""
    row_id = row_id or db.new_id("em")
    seen = conn.execute(
        "SELECT message_id, direction FROM email_message WHERE thread_id = %s AND id <> %s ORDER BY created_at",
        (thread["id"], row_id),
    ).fetchall()
    last_in = next((r["message_id"] for r in reversed(seen) if r["direction"] == "in"), None)
    domain = split_address(settings.email_address)[1]
    mid = f"<{row_id}@{domain}>"
    headers = {"Message-ID": mid}
    if last_in:
        headers["In-Reply-To"] = last_in
        headers["References"] = " ".join(r["message_id"] for r in seen)
    headers.update(extra_headers or {})
    payload = _payload(settings, to=to, subject=subject, text=text, html=html, headers=headers)
    if reply_to:
        payload["reply_to"] = reply_to
    provider_id, outcome, error = None, "sent", None
    if gate is not None:
        outcome = "failed"
        log.warning("e-mail not sent: %s (thread %s)", gate, thread["id"])
    elif not settings.resend_api_key:
        outcome = "failed"
        log.warning("e-mail not sent: RESEND_API_KEY unset (thread %s)", thread["id"])
    else:
        try:
            provider_id = send(payload, idempotency_key=idempotency_key or row_id)
        except MailError as e:
            outcome, error = "failed", str(e)
            log.warning("e-mail not sent (thread %s): %s", thread["id"], e)
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, provider_id, from_addr, to_addrs, subject, "
        "new_text, intent, outcome, sent_by) VALUES (%s, %s, 'out', %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "provider_id = CASE WHEN email_message.outcome = 'sent' THEN email_message.provider_id "
        "ELSE EXCLUDED.provider_id END, "
        "outcome = CASE WHEN email_message.outcome = 'sent' THEN email_message.outcome ELSE EXCLUDED.outcome END, "
        "subject = EXCLUDED.subject, new_text = EXCLUDED.new_text",
        (
            row_id,
            thread["id"],
            mid,
            provider_id,
            settings.email_address,
            [to],
            subject,
            _mask_link_token(text, settings),
            Jsonb(intent) if intent is not None else None,
            outcome,
            sent_by,
        ),
    )
    conn.execute("UPDATE email_thread SET last_out_at = now() WHERE id = %s", (thread["id"],))
    return {
        "id": row_id,
        "message_id": mid,
        "provider_id": provider_id,
        "outcome": outcome,
        "subject": subject,
        "text": text,
        "error": error,
    }


def deliver(
    conn: psycopg.Connection,
    settings: config.Settings,
    *,
    thread: dict,
    to: str,
    paragraphs: list[str],
    links: list[tuple[str, str]] = (),
    subject: str | None = None,
    sent_by: str = "admiry",
    others_on_copy: bool = False,
    has_attachments: bool = False,
    intent: dict | None = None,
) -> dict:
    """One message in `thread` to `to` (only ever the counterpart, the owner or the operator), recorded as an out
    row (outcome sent | failed), through deliver_core with the thread's plus-address as Reply-To. Never raises: a
    failure is the row and a log line. The channel off (settings.email_inbound) refuses just like a missing key --
    finding I1."""
    extra = [*([ONLY_YOU] if others_on_copy else []), *([ATTACHMENTS] if has_attachments else [])]
    text, html = compose([*paragraphs, *extra], links)
    return deliver_core(
        conn,
        settings,
        thread=thread,
        to=to,
        subject=subject or reply_subject(thread.get("subject")),
        text=text,
        html=html,
        gate=channel_gate(settings),
        reply_to=reply_address(settings.email_address, thread["token"]),
        sent_by=sent_by,
        intent=intent,
    )


def deliver_ticket(
    conn: psycopg.Connection,
    settings: config.Settings,
    *,
    thread: dict,
    to: str,
    subject: str,
    paragraphs: list[str],
    row_id: str,
    sent_by: str,
) -> dict:
    """Ticket mail (spec 2026-09-30 §5.2, D141): deliver_core under the support gate, whatever the e-mail channel says.
    Reply-To is the thread's plus-address while the channel is on (the owner's reply lands on the ticket), else
    AGENT_SUPPORT_REPLY_TO (a person's mailbox; config refuses AGENT_SUPPORT=on without it then). `row_id` comes from
    the outbox event, so a retry is the same row, the same Message-ID and the same Resend Idempotency-Key."""
    reply_to = (
        reply_address(settings.email_address, thread["token"]) if settings.email_inbound else settings.support_reply_to
    )
    text, html = compose(paragraphs)
    return deliver_core(
        conn,
        settings,
        thread=thread,
        to=to,
        subject=subject,
        text=text,
        html=html,
        gate=support_gate(settings),
        reply_to=reply_to,
        row_id=row_id,
        sent_by=sent_by,
        intent={"kind": "ticket"},
    )


def deliver_digest(
    conn: psycopg.Connection,
    settings: config.Settings,
    *,
    digest_id: str,
    thread: dict,
    to: str,
    rendered: dict,
) -> dict:
    """An owner's digest (digest spec 2026-09-30 §8; plan 2026-10-07-agent-k2 Task 10, Dg P4): deliver_core under the
    digest's own gate, the stored `rendered` mail as it is -- subject, text, html, the unsubscribe headers and the
    Reply-To it was built with -- so every attempt sends the same bytes. The out row's id is the digest's id: one row,
    one Message-ID and one Idempotency-Key across retries."""
    return deliver_core(
        conn,
        settings,
        thread=thread,
        to=to,
        subject=rendered["subject"],
        text=rendered["text"],
        html=rendered["html"],
        gate=digest_gate(settings),
        reply_to=rendered.get("reply_to"),
        row_id=digest_id,
        extra_headers=rendered.get("headers") or {},
        intent={"kind": "digest", "digest_id": digest_id},
    )


def send_operator(
    settings: config.Settings, *, subject: str, text: str, html: str, message_id: str, idempotency_key: str
) -> str | None:
    """A message to the operator (AGENT_OPS_EMAIL) outside any thread and any row -- the digest's operator preview,
    Resend's bounce notices -- whatever AGENT_EMAIL_INBOUND says: None when sent, else why not ("RESEND_API_KEY
    unset", "no AGENT_OPS_EMAIL", or Resend's refusal, which the digest pages on)."""
    if not settings.resend_api_key:
        return "RESEND_API_KEY unset"
    if not settings.ops_email:
        return "no AGENT_OPS_EMAIL"
    payload = _payload(
        settings, to=settings.ops_email, subject=subject, text=text, html=html, headers={"Message-ID": message_id}
    )
    try:
        send(payload, idempotency_key=idempotency_key)
    except MailError as e:
        log.warning("operator mail not sent: %s", e)
        return str(e)
    return None


_CHANNEL = object()  # send_plain's default gate: the e-mail channel's own rule (finding I1)


def send_plain(
    settings: config.Settings,
    *,
    to: str,
    subject: str,
    paragraphs: list[str],
    links: list[tuple[str, str]] = (),
    idempotency_key: str,
    gate: str | None | object = _CHANNEL,
    message_id: str | None = None,
) -> bool:
    """A message outside any thread (the operator notices): no Reply-To token, no row; True when sent. `gate` is why it
    may not go out (None = it may); by default the e-mail channel's own rule (finding I1). The ticket notice passes
    support_gate (spec 2026-09-30 D141). The Message-ID is `<message_id@domain>` when the caller names one -- the
    ticket notice does, so a retry is the same request under its Idempotency-Key -- else a new one."""
    reason = channel_gate(settings) if gate is _CHANNEL else gate
    if reason is not None:
        log.warning("notice to %s not sent: %s", to, reason)
        return False
    if not settings.resend_api_key:
        return False
    text, html = compose(list(paragraphs), links)
    domain = split_address(settings.email_address)[1]
    mid = f"<{message_id or db.new_id('em')}@{domain}>"
    payload = _payload(settings, to=to, subject=subject, text=text, html=html, headers={"Message-ID": mid})
    try:
        send(payload, idempotency_key=idempotency_key)
    except MailError as e:
        log.warning("notice to %s not sent: %s", to, e)
        return False
    return True
