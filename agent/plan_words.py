"""The words of a plan's ads, edited by hand on the "Your ads" page (spec 2026-10-06-admiry-your-ads-page-design.md
§4, Y8): the lines cleaned, each platform's own linter and minimums, and the swap into a version's sections -- the
primary's new words reaching every entry that copies them (`copy_of`), a copying platform given its own words losing
its `copy_of` (plans_channels._borrowed reads `plans.copy_of(own)`)."""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp.exceptions import ToolError

from . import landing, plans
from .channels.google_search import build as google_build
from .channels.newsbreak import adapter as newsbreak_adapter
from .channels.newsbreak import build as newsbreak_build

FIELD_MAX = 2_000  # characters per text area: a guard before any rule, not a rule the owner meets
GOOGLE_MAX_HEADLINES, GOOGLE_MAX_DESCRIPTIONS = 15, 4  # a responsive search ad's room


@dataclass(frozen=True)
class Limits:
    """A platform's counts and the notes under its two fields."""

    h_min: int
    d_min: int
    h_max: int | None
    d_max: int | None
    headline_note: str
    description_note: str
    too_few: str  # the refusal when fewer lines than the minimum pass


def limits(channel: str) -> Limits:
    if channel == "newsbreak":
        return Limits(
            1,
            1,
            None,
            None,
            f"At most {newsbreak_build.HEADLINE_MAX_NB} characters each, a wide character counts as two; NewsBreak "
            f"shows up to {newsbreak_build.MAX_ADS} of these, the longest first",
            f"At most {newsbreak_build.DESCRIPTION_MAX_NB} characters each; NewsBreak pairs each with a headline, "
            "the longest first",
            newsbreak_adapter.TOO_LITTLE_COPY,
        )
    return Limits(
        plans.MIN_HEADLINES,
        plans.MIN_DESCRIPTIONS,
        GOOGLE_MAX_HEADLINES,
        GOOGLE_MAX_DESCRIPTIONS,
        f"At most {plans.HEADLINE_MAX} characters each; keep at least {plans.MIN_HEADLINES}, "
        f"at most {GOOGLE_MAX_HEADLINES}",
        f"At most {plans.DESCRIPTION_MAX} characters each; keep at least {plans.MIN_DESCRIPTIONS}, "
        f"at most {GOOGLE_MAX_DESCRIPTIONS}",
        f"Keep at least {plans.MIN_HEADLINES} headlines and {plans.MIN_DESCRIPTIONS} descriptions that pass",
    )


def clean_lines(text: str | None) -> list[str]:
    """One line per entry (spec §3.1): the field capped at FIELD_MAX first, then each line's spaces collapsed, blank
    lines dropped, duplicates dropped keeping the first, the order kept."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (text or "")[:FIELD_MAX].splitlines():
        line = " ".join(raw.split())
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def lint(
    channel: str, category: str | None, headlines: list[str], descriptions: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """The platform's own linter over the lines, with the trade's banned words: Google Search's `lint_copy` (30 / 90)
    for every platform but NewsBreak, whose `lint` has its own lengths and placeholder rule. Returns the lines kept,
    in order, and one note per dropped line."""
    banned = landing.load_recipe(category or "fallback")["banned"]
    if channel == "newsbreak":
        return newsbreak_build.lint(headlines, descriptions, banned)
    return google_build.lint_copy(headlines, descriptions, banned)


def check_counts(channel: str, headlines: list[str], descriptions: list[str], notes: list[str]) -> None:
    """ToolError when fewer lines than the platform's minimum passed (the notes first, so the owner reads why each
    line went) or more than its room."""
    lim = limits(channel)
    if len(headlines) < lim.h_min or len(descriptions) < lim.d_min:
        raise ToolError(" ".join([*notes, lim.too_few]))
    if (lim.h_max is not None and len(headlines) > lim.h_max) or (
        lim.d_max is not None and len(descriptions) > lim.d_max
    ):
        raise ToolError(f"At most {lim.h_max} headlines and {lim.d_max} descriptions")


def copiers(sections: dict, channel: str) -> list[str]:
    """The funded channels whose words are `channel`'s (`copy_of`), in the plan's order."""
    return [c["channel"] for c in plans.funded(sections) if plans.copy_of(c) == channel]


def swapped(sections: dict, channel: str, headlines: list[str], descriptions: list[str]) -> dict:
    """A copy of `sections` with `channel`'s headlines and descriptions replaced -- and every entry that copies
    `channel`'s words carrying the same new lines (they are copies: the rebuild keeps them in step). An entry given its
    own words stops copying: its `copy_of` is dropped."""
    reached = {channel, *copiers(sections, channel)}
    ads = sections["ads"]
    entries = []
    for e in ads["channels"]:
        if e.get("channel") not in reached:
            entries.append(e)
            continue
        own = {k: v for k, v in e.items() if not (e.get("channel") == channel and k == "copy_of")}
        entries.append({**own, "headlines": list(headlines), "descriptions": list(descriptions)})
    return {**sections, "ads": {**ads, "channels": entries}}
