"""The links a digest carries (digest spec 2026-09-30 §4, §9; plan 2026-10-07-agent-k2 Tasks 4 and 9), every one from
`settings.web_base_url`: Settings' sections, the "Your ads" page (YA-1: the ads left Settings for /ads), the
homepage's view of one day's report, and the unsubscribe links.

An unsubscribe token (Dg plan P9) is `v<fp>.<mac>`: `fp` the first 6 hex characters of sha256(secret), `mac` the
unpadded base64url of HMAC-SHA256(secret, "digest-unsubscribe|<business_id>"). Verification takes the key whose
fingerprint matches -- AGENT_DIGEST_LINK_SECRET, then AGENT_DIGEST_LINK_SECRET_PREVIOUS -- and compares in constant
time; every other token fails the same way."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import date
from urllib.parse import quote

FREQUENCIES = ("daily", "weekly", "off")


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:6]


def _mac(secret: str, business_id: str) -> str:
    digest = hmac.new(secret.encode(), f"digest-unsubscribe|{business_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def mint(settings, business_id: str) -> str:
    """The business's unsubscribe token under the current link secret (ValueError when none is set)."""
    secret = settings.digest_link_secret
    if not secret:
        raise ValueError("AGENT_DIGEST_LINK_SECRET is not set")
    return f"v{fingerprint(secret)}.{_mac(secret, business_id)}"


def verify(settings, business_id: str, token: str) -> bool:
    """True when `token` is the business's under the current or the previous link secret."""
    if not token or not token.startswith("v") or "." not in token:
        return False
    fp, mac = token[1:].split(".", 1)
    for secret in (settings.digest_link_secret, settings.digest_link_secret_previous):
        if secret and hmac.compare_digest(fingerprint(secret).encode(), fp.encode()):
            return hmac.compare_digest(_mac(secret, business_id).encode(), mac.encode())
    return False


def unsubscribe_url(settings, business_id: str, f: str | None = None) -> str:
    """The business's unsubscribe link: without `f` the one-click link (reports off), with `f` weekly or off."""
    url = f"{settings.web_base_url}/unsubscribe/{mint(settings, business_id)}?b={quote(business_id)}"
    return f"{url}&f={f}" if f else url


def settings_url(settings, business_id: str, section: str) -> str:
    """Settings opened on one of its sections (connect_page's `section`, carried through sign-in)."""
    return f"{settings.web_base_url}/connect?b={quote(business_id)}&section={quote(section)}"


def ads_url(settings, business_id: str) -> str:
    """The "Your ads" page of the business (agent/your_ads.py)."""
    return f"{settings.web_base_url}/ads?b={quote(business_id)}"


def report_url(settings, business_id: str, day: date) -> str:
    """The homepage opening that day's report (home.js's `?report=` branch)."""
    return f"{settings.web_base_url}/?report={quote(business_id)}&day={day.isoformat()}"
