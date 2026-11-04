"""Google's speed test of an industry site's preview (A3p-6 batch 1 Q8; spec T21): PageSpeed Insights v5 on a phone
(`strategy=MOBILE`), its four category scores as 0-100 and the home page's largest paint and layout shift.

`AGENT_PAGESPEED_API_KEY` set turns it on; unset, nothing calls Google. The key travels in the `X-Goog-Api-Key`
header (Google's own alternative to the `key` parameter), never in the URL, so no request log can print it."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from . import config

log = logging.getLogger(__name__)

URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
TIMEOUT_S = 120  # a run takes Google 10-60 s
CATEGORIES = ("PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO")
# lighthouseResult.categories' keys -> the names the preview keeps
_SCORES = {
    "performance": "performance",
    "accessibility": "accessibility",
    "best-practices": "best_practices",
    "seo": "seo",
}
REFUSE_UNDER = 50  # a preview of Admiry's own template under this is held back (TemplateFault)
WARN_UNDER = 80  # under this the operator is told (a WARNING), the preview stands


class Unavailable(Exception):
    """Google did not answer, answered with a status other than 200, or with no scores."""


def on(settings: config.Settings) -> bool:
    return bool(settings.pagespeed_api_key)


def transport() -> httpx.BaseTransport | None:
    """Google is called through this (real network: None); a test monkeypatches it to a MockTransport."""
    return None


def _score(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Unavailable("no score")
    return round(value * 100)


def score(settings: config.Settings, url: str) -> dict:
    """The speed test of `url` on a phone: {performance, accessibility, best_practices, seo (0-100), lcp_ms, cls,
    fetched_at}. Unavailable for no answer, a status other than 200, or a body without the scores."""
    params = [("url", url), ("strategy", "MOBILE"), *(("category", c) for c in CATEGORIES)]
    headers = {"X-Goog-Api-Key": settings.pagespeed_api_key or ""}
    try:
        with httpx.Client(transport=transport(), timeout=TIMEOUT_S) as client:
            r = client.get(URL, params=params, headers=headers)
    except httpx.HTTPError as e:
        log.warning("pagespeed: Google could not be reached: %s", type(e).__name__)
        raise Unavailable("unreachable") from None
    if r.status_code != 200:
        log.warning("pagespeed: Google answered %s", r.status_code)
        raise Unavailable(f"status {r.status_code}")
    try:
        body = r.json()
        result = body["lighthouseResult"]
        categories = result["categories"]
        out: dict = {name: _score(categories[key]["score"]) for key, name in _SCORES.items()}
        audits = result.get("audits") or {}
        lcp = (audits.get("largest-contentful-paint") or {}).get("numericValue")
        cls = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
    except (ValueError, KeyError, TypeError) as e:
        log.warning("pagespeed: Google's answer has no scores: %s", type(e).__name__)
        raise Unavailable("no scores") from None
    except Unavailable:
        log.warning("pagespeed: Google's answer has no scores")
        raise
    out["lcp_ms"] = round(lcp) if isinstance(lcp, int | float) else None
    out["cls"] = round(cls, 3) if isinstance(cls, int | float) else None
    fetched = result.get("fetchTime")
    out["fetched_at"] = fetched if isinstance(fetched, str) else datetime.now(UTC).isoformat()
    return out
