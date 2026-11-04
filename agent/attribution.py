"""Where a contact came from (spec 2026-10-03-admiry-pages-a3p-3-design P28, §5.3; plan
2026-10-04-agent-a3p-3-2-measured-contacts C5): the ad click's platform first, else the tagged link's `utm_source`,
else the kind of site that referred the visitor (a search engine, a social network, another site), else `direct`. The
page script keeps what brought the visitor for 90 days and sends it with every beacon and form; this module reads it
off the posted fields and names the source the lead and the event keep (`came_from`). Pure: no database."""

from __future__ import annotations

import re
from collections.abc import Callable

from .channels import registry

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
UTM_MAX = 100  # one tag's value, kept
SOURCE_MAX = 40  # a utm_source as a source's name
HOST_MAX = 200
# the referrer's kind: a search engine's results (any of its country domains; its mail is not search), a social network
_SEARCH = re.compile(
    r"(?:^|\.)(?:google|bing|duckduckgo|yahoo|ecosia|baidu|yandex|brave|startpage)\.[a-z]{2,3}(?:\.[a-z]{2})?$"
)
_NOT_SEARCH = ("mail.", "docs.", "drive.", "calendar.", "maps.")
_SOCIAL = (
    "facebook.com",
    "fb.com",
    "instagram.com",
    "t.co",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "lnkd.in",
    "reddit.com",
    "nextdoor.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "threads.net",
    "threads.com",
    "snapchat.com",
)
_HOST = re.compile(r"^[a-z0-9.-]+$")
_TAG_WORD = re.compile(r"[^a-z0-9._-]+")
# the sources' owner words (get_campaign_performance's by_source; a channel's is its own name)
WORDS = {
    "search": "Search results (not an ad)",
    "social": "Social media (not an ad)",
    "other": "Another website",
    "direct": "Came straight to the page",
    "reported": "You reported",
    "unknown": "Before Admiry kept the source",
}


def host(value: str | None) -> str | None:
    """A referring host as the script sends it: lowercased, no port, letters, digits, dots and hyphens only, at most
    HOST_MAX characters; None for anything else."""
    h = (value or "").strip().lower().rstrip(".")
    h = re.sub(r":\d+$", "", h)
    return h if h and len(h) <= HOST_MAX and _HOST.match(h) and "." in h else None


def referrer_kind(ref: str | None) -> str | None:
    """`search`, `social` or `other` for a referring host; None for none."""
    h = host(ref)
    if h is None:
        return None
    if _SEARCH.search(h) and not h.startswith(_NOT_SEARCH):
        return "search"
    if any(h == d or h.endswith("." + d) for d in _SOCIAL):
        return "social"
    return "other"


def utm(read: Callable[[str], str]) -> dict[str, str]:
    """The UTM tags posted beside a beacon or a form (`read(name)`: the posted value or ""), each cut to UTM_MAX
    characters; only those present."""
    out = {}
    for key in UTM_KEYS:
        value = (read(key) or read("_" + key)).strip()
        if value:
            out[key] = value[:UTM_MAX]
    return out


def came_from(click_kind: str | None, tags: dict[str, str], ref: str | None) -> str:
    """The source a lead or an event keeps (P28): the click id's channel, else `utm:<utm_source>` (lowercased, the
    characters a tag name takes), else the referrer's kind, else `direct`. A click id kind nobody maps still names no
    channel, and falls through to the rest."""
    channel = registry.channel_for_click(click_kind)
    if channel:
        return channel
    tag = _TAG_WORD.sub("-", (tags.get("utm_source") or "").strip().lower()).strip("-")[:SOURCE_MAX]
    if tag:
        return f"utm:{tag}"
    return referrer_kind(ref) or "direct"


def label(source: str | None) -> str:
    """A source in the owner's words: a channel's own name, a tagged link's tag, or WORDS."""
    if not source:
        return WORDS["unknown"]
    if source.startswith("utm:"):
        return f"A link tagged “{source[4:]}”"
    if source in WORDS:
        return WORDS[source]
    return registry.label(source) if source in registry.LABELS else source
