"""Who wrote this (spec 2026-09-28 §6.1) and did Resend send this webhook (§5.1).

Resend's DMARC verdict decides when it is definite (pass / fail). Otherwise — usually a sender domain with no
DMARC record — the raw message's DKIM signatures are verified here (dkimpy) and one of them must be aligned with
the From domain. SPF alone never authenticates: forwarding breaks it. The webhook is authenticated by its Svix
signature over the raw body, checked in-house the way stripe_client.verify_signature checks Stripe's: HMAC-SHA256,
constant-time compare, 300 s tolerance, no SDK."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import dkim

TOLERANCE_S = 300


class BadSignature(Exception):
    """A webhook whose Svix headers are missing, malformed, stale or wrong: nothing may be stored."""


@dataclass(frozen=True)
class Decision:
    authenticated: bool
    reason: str  # dmarc_pass | dmarc_fail | dkim_aligned | dkim_unaligned | dkim_none | dkim_invalid | no_from | raw_unavailable


def sender(received: dict) -> str:
    """The From address, lower-cased, without its display name ("" when absent)."""
    return email.utils.parseaddr(received.get("from") or "")[1].strip().lower()


def domain_of(address: str) -> str:
    return address.rpartition("@")[2].lower()


def aligned(sig_domain: str, from_domain: str) -> bool:
    """Relaxed alignment without a public-suffix list: equal, or one a subdomain of the other."""
    a, b = sig_domain.lower().strip("."), from_domain.lower().strip(".")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def dkim_status(raw: bytes, from_domain: str, *, dnsfunc: Callable | None = None) -> str:
    """dkim_aligned when a signature verifies and aligns; dkim_unaligned when one verifies but none aligns;
    dkim_none without any DKIM-Signature header; dkim_invalid when none verifies, or the raw message itself is
    malformed (dkim.DKIM() raises dkim.MessageFormatError on a bad header line: that must not cost three
    retries and an ops notice, so it counts as dkim_invalid like any other verification failure -- finding M5)."""
    try:
        d = dkim.DKIM(raw)
        count = sum(1 for name, _ in d.headers if name.lower() == b"dkim-signature")
        if count == 0:
            return "dkim_none"
        verified: list[str] = []
        for i in range(count):
            try:
                ok = d.verify(idx=i, dnsfunc=dnsfunc) if dnsfunc is not None else d.verify(idx=i)
            except dkim.DKIMException:
                ok = False
            if ok:
                domain = d.domain
                verified.append(domain.decode() if isinstance(domain, bytes) else str(domain))
    except dkim.DKIMException:
        return "dkim_invalid"
    if not verified:
        return "dkim_invalid"
    return "dkim_aligned" if any(aligned(v, from_domain) for v in verified) else "dkim_unaligned"


def decide(received: dict, fetch_raw: Callable[[], bytes | None], *, dnsfunc: Callable | None = None) -> Decision:
    """§6.1: the verdict when definite, else the raw message's DKIM. `fetch_raw` is called only when needed."""
    addr = sender(received)
    if not addr or "@" not in addr:
        return Decision(False, "no_from")
    verdict = str((received.get("authentication") or {}).get("dmarc") or "").lower()
    if verdict == "pass":
        return Decision(True, "dmarc_pass")
    if verdict == "fail":
        return Decision(False, "dmarc_fail")
    raw = fetch_raw()
    if raw is None:
        return Decision(False, "raw_unavailable")
    status = dkim_status(raw, domain_of(addr), dnsfunc=dnsfunc)
    return Decision(status == "dkim_aligned", status)


def verify_svix(
    payload: bytes, headers: Mapping[str, str], secret: str, *, tolerance: int = TOLERANCE_S, now: float | None = None
) -> None:
    """Svix's scheme: the signed content is "<svix-id>.<svix-timestamp>.<body>", the key is the base64-decoded
    secret after "whsec_", and `svix-signature` holds space-separated "v1,<base64>" entries; any one matching
    passes. Raises BadSignature otherwise."""
    msg_id, ts, sigs = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if not secret or not msg_id or not ts or not sigs:
        raise BadSignature("missing signature headers or secret")
    if not (ts.isascii() and ts.isdigit()) or len(ts) > 12:
        raise BadSignature("malformed timestamp")
    if abs((time.time() if now is None else now) - int(ts)) > tolerance:
        raise BadSignature("timestamp outside the tolerance")
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except ValueError as e:
        raise BadSignature("malformed secret") from e
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + payload, hashlib.sha256).digest()).decode()
    for entry in sigs.split():
        version, _, value = entry.partition(",")
        if version == "v1" and value.isascii() and hmac.compare_digest(expected, value):
            return
    raise BadSignature("signature mismatch")
