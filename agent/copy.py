"""Owner words (spec 2026-09-24 D52, 2026-09-25 §3, 2026-09-29 §7.3; the daily digest spec 2026-09-30 §4): the words an
owner never reads in anything Admiry writes them -- advertising jargon, the account structure's words and the money
plumbing's -- as whole words in any case, plurals too ("instructions" or "structure" never match). Every owner-facing
text the digest and the roles write passes all three (`jargon`); the tests import the same regexes."""

from __future__ import annotations

import re

# D52's ad-tech words, and every channel's since spec 2026-09-29 §7.3 ("ad set", "pixel")
JARGON = re.compile(
    r"\b(?:cpc|ctr|roas|cpm|pacing|match type|ad group|ad set|campaign|keyword|pixel)s?\b", re.IGNORECASE
)
# the account structure (spec 2026-09-25 §3)
ACCOUNT_JARGON = re.compile(r"\b(?:mcc|manager account hierarchy|customer client links?)\b", re.IGNORECASE)
# the money plumbing (channels spec §3): an owner reads "your balance", never these
MONEY_JARGON = re.compile(r"\b(?:wallet|ledger|settlement)s?\b", re.IGNORECASE)


def jargon(text: str) -> str | None:
    """The first banned word `text` holds, as written there, by any of the three; None when it holds none."""
    for pattern in (JARGON, ACCOUNT_JARGON, MONEY_JARGON):
        found = pattern.search(text or "")
        if found:
            return found.group(0)
    return None
