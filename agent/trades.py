"""The home-services trades Admiry tells apart, by the words a business uses -- one table for the two readers of a
trade: the industry sites (sites/intake: the trade a site's problem picker is for) and the Strategist (roles/strategist:
the trade whose curated knowledge a plan draft reads, capability layer K3). A leaf: it imports only the standard
library.

The table is ordered: the first trade whose pattern the text matches wins. The site's four come first, with the
patterns the sites have always used ("ac" is the word AC, not the end of "tarmac"); landscaping and remodel follow,
trades the knowledge has files for and the site families do not (sites/intake keeps only its own four)."""

from __future__ import annotations

import re

TABLE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("roofing", re.compile(r"roof")),
    ("plumbing", re.compile(r"plumb")),
    ("hvac", re.compile(r"hvac|heating|cooling|air conditioning|furnace|\bac\b")),
    ("electrical", re.compile(r"electric")),
    ("landscaping", re.compile(r"landscap|lawn|hardscap|irrigation|garden")),
    ("remodel", re.compile(r"remodel|renovat|kitchen|bathroom")),
)
NAMES: tuple[str, ...] = tuple(name for name, _ in TABLE)


def trade_in(text: str) -> str | None:
    """The first trade, in the table's order, whose words `text` holds (read in any case); None for no trade, and for
    anything that is not text."""
    if not isinstance(text, str):
        return None
    low = text.lower()
    return next((name for name, pattern in TABLE if pattern.search(low)), None)
