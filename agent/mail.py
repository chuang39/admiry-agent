"""Owner e-mail through Resend, and Admiry's one Resend client (plan 2026-09-29 P16: the e-mail channel's mail_out
sends through send() too). Sender: Settings.mail_from, `Admiry <hello@agent.admiry.ai>` unless AGENT_MAIL_FROM
says otherwise (e-mail channel spec 2026-09-28 D59: all Admiry mail comes from hello@, which takes replies; main
spec D12's no-reply@ is retired); the DNS records live in deploy/terraform/envs/linode-prod/dns-agent.tf.
Unconfigured (no RESEND_API_KEY) = nothing is sent and callers say so; a refusal is MailError with Resend's message
for the log, never for the owner."""

from __future__ import annotations

import json
import logging
import re

import httpx

from . import config
from .templating import env as _env

log = logging.getLogger(__name__)
URL = "https://api.resend.com/emails"
TIMEOUT_S = 10.0
MAX_SUBJECT = 200
# C0 and C1 control characters and the Unicode line/paragraph separators: none may reach a header (I10).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(config.load().resend_api_key)


def client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S)


def render(template: str, **ctx) -> str:
    return _env.get_template(template).render(**ctx)


def subject_line(subject: str) -> str:
    """One header line (I10: a visitor's name reaches the lead e-mail's subject): every control character
    becomes a space, runs of spaces collapse to one, at most MAX_SUBJECT characters."""
    return re.sub(r" {2,}", " ", _CONTROL.sub(" ", subject)).strip()[:MAX_SUBJECT]


def send(
    to: str,
    subject: str,
    text: str,
    *,
    http: httpx.Client | None = None,
    html: str | None = None,
    headers: dict | None = None,
    reply_to: str | None = None,
    idempotency_key: str | None = None,
    from_addr: str | None = None,
    attachments: list[dict] | None = None,
) -> str | None:
    """Resend's id; None when unconfigured. The keyword-only fields are the e-mail channel's (mail_out, P16), each
    sent only when given: `html`, `headers` (Message-ID, In-Reply-To, References) and `reply_to` in the body,
    `idempotency_key` as the Idempotency-Key request header, `from_addr` in place of settings.mail_from;
    `attachments` (A3p-3 batch 4: a site's screenshots), Resend's own `[{filename, content}]`, content in base64."""
    settings = config.load()
    if not settings.resend_api_key:
        return None
    body = {"from": from_addr or settings.mail_from, "to": [to], "subject": subject_line(subject), "text": text}
    if html:
        body["html"] = html
    if headers:
        body["headers"] = headers
    if reply_to:
        body["reply_to"] = reply_to
    if attachments:
        body["attachments"] = attachments
    own = http is None
    h = http or client()
    request_headers = {"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"}
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    try:
        # json.dumps (not httpx's json= kwarg, which uses compact separators): Resend does not care about
        # whitespace, but this keeps the wire body human-diffable in a log or a test.
        r = h.post(URL, content=json.dumps(body), headers=request_headers)
    except httpx.HTTPError as e:
        log.warning("mail: %s", e)
        raise MailError(f"mail transport: {type(e).__name__}") from None
    finally:
        if own:
            h.close()
    if not r.is_success:
        detail = r.text
        if r.headers.get("content-type", "").startswith("application/json"):
            try:
                refusal = r.json()
            except ValueError:
                refusal = None  # claimed JSON but did not parse: fall back to the raw text already in `detail`
            if isinstance(refusal, dict):
                detail = refusal.get("message", detail)
        log.warning("resend refused (%s): %s", r.status_code, str(detail)[:200])
        raise MailError(f"mail refused ({r.status_code})")
    try:
        answer = r.json()
    except ValueError:  # json.JSONDecodeError is one
        answer = None
    if not isinstance(answer, dict):  # a 2xx that is not Resend's JSON object: say so as a MailError, never raw
        log.warning("resend answered %s without a JSON object", r.status_code)
        raise MailError(f"mail answer unreadable ({r.status_code})")
    return str(answer.get("id") or "")


def to_owner(conn, biz: str, template: str, subject: str, **ctx) -> str:
    """One e-mail to the business's owner about their ads (page-check's, and the lifecycle's own pause for a page that
    is down -- spec 2026-09-28-admiry-pages-design §8: the owner is told why): "sent", or why not -- "unconfigured",
    "no_owner" (no address on file, or it bounced or unsubscribed), "failed" (a refusal this module already logged).
    Never raises for the mail itself."""
    if not configured():
        return "unconfigured"
    row = conn.execute("SELECT owner_email, notify_email_status FROM business WHERE id = %s", (biz,)).fetchone()
    if not row or not row["owner_email"] or row["notify_email_status"] != "ok":
        return "no_owner"
    try:
        sent = send(row["owner_email"], subject, render(template, **ctx))
    except MailError:
        return "failed"
    return "unconfigured" if sent is None else "sent"
