"""Running the ads (spec 2026-10-01 §4.2): where they stand, stop, start again, change what they may spend."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.exceptions import ToolError

from ..mcp_app import CHANNELS_VIEW, PLAN_VIEW
from .plan import SEE_THE_PLAN_FIRST, _before_the_plan, still_the_latest
from .registry import dispatch, public_tool, replayed
from .where import Where, where

# What the budget refuses itself, in its own arguments' names (adjust's refusals name monthly_budget_usd): no command
# runs and no row is written (final review C, minor 1)
NOTHING_TO_CHANGE = "say what to change: monthly_usd, level, or channel with channel_monthly_usd"
OUT_OF_RANGE = "monthly_usd must be above 0 and at most 100,000"


def _named(w: Where, channel: str) -> tuple[str, ...]:
    """The channel rows a pause or a resume names: every one the business has for "all", else the one of `channel`
    (none when it has no row there)."""
    return w.channels if channel == "all" else tuple(c for c in w.channels if c == channel)


def still_paused(w: Where, channel: str) -> Callable[[dict], bool]:
    """Whether a pause's first answer, sent again, still stands (final review A P1): the owner's pause is in force on
    every channel row the call names, and it names one at least. Once the ads were resumed there, or a place the call
    names runs since, the answer "paused" is no longer true: the key is refused as reused (`replayed`) and nothing is
    dispatched, so the assistant sends a new key and the pause really runs."""
    named = _named(w, channel)

    def stands(_answer: dict) -> bool:  # what stands is decided by the rows as they are now, whatever it said
        return bool(named) and all(c in w.owner_paused for c in named)

    return stands


def still_resumed(w: Where, channel: str) -> Callable[[dict], bool]:
    """Whether a resume's first answer, sent again, still stands: no channel row the call names is under the owner's
    pause. Once the owner paused there again, the key is refused as reused and nothing is turned on."""
    named = _named(w, channel)

    def stands(_answer: dict) -> bool:
        return not any(c in w.owner_paused for c in named)

    return stands


@public_tool(cls="RO", scope="read", commands=("get_channel_status",), app=CHANNELS_VIEW)
def get_campaigns() -> dict:
    """Where the business's ads stand right now, one row for each place they run: its state in a sentence, today's
    spend against today's budget, the month so far against that place's monthly amount, this week's leads and
    calls, what the platform says is wrong (its own words: relay them exactly, quotes included), and what the owner
    can do there (pause, resume, connect). After a launch this is also where each place's progress shows: one that
    waits says what it waits for. Each row also names the plan version its ads come from (plan_id, plan_version):
    the one that place runs or, for a place that has not started, the one approved for it. When the owner asks what
    ads are running, read that version with get_marketing_plan: its ad text and, for Google Search, examples of the
    searches it shows for (NewsBreak shows its ads to people in the plan's area, not on searches). Call it when the
    owner asks how things are going, and before pause_campaign or resume_campaign."""
    return dispatch("get_channel_status")


@public_tool(cls="W", scope="manage", commands=("pause",))
def pause_campaign(idempotency_key: str, channel: str = "all") -> dict:
    """Stop spending: on one place the ads run (channel: google_search, newsbreak, …) or on every one ("all", the
    default). The pause is recorded at once and sent to the platform right away, or within the hour when it could
    not be sent at once. Always allowed: stopping spend never needs approval. When the ads run in more than one
    place and the owner did not say which, ask: "one of them, or all?". Use a new idempotency_key per pause."""
    w = where(scope="manage")
    # the same pause sent again: its first answer while the owner's pause still stands on every place it names; after
    # a resume its key is refused as a reused one, here, and nothing is dispatched
    earlier = replayed(idempotency_key, scope="manage", still_stands=still_paused(w, channel))
    if earlier is not None:
        return earlier
    return dispatch("pause", idempotency_key=idempotency_key, channel=channel)


@public_tool(cls="W", scope="manage", approval="assistant", commands=("resume",))
def resume_campaign(idempotency_key: str, channel: str = "all") -> dict:
    """Start spending again after the owner paused: on one place the ads run or on every one ("all", the default).
    A pause Admiry made itself (the month's amount spent, the page down) is Admiry's to lift, not this tool's, and
    so are ads a platform switched off itself. IMPORTANT ACTION: it spends money again, so confirm with the owner
    before calling. Use a new idempotency_key per resume."""
    w = where(scope="manage")
    # the same resume sent again: its first answer while no place it names is under the owner's pause; after another
    # pause its key is refused as a reused one, here, and nothing is turned on
    earlier = replayed(idempotency_key, scope="manage", still_stands=still_resumed(w, channel))
    if earlier is not None:
        return earlier
    return dispatch("resume", idempotency_key=idempotency_key, channel=channel)


@public_tool(cls="W", scope="manage", commands=("adjust",), app=PLAN_VIEW)
def update_marketing_budget(
    idempotency_key: str,
    monthly_usd: float | None = None,
    level: str | None = None,
    channel: str | None = None,
    channel_monthly_usd: float | None = None,
) -> dict:
    """Change what the advertising may spend: the whole plan's monthly amount (monthly_usd, up to $100,000; under
    $100 a month is refused: it cannot be estimated), one of its levels (level: minimum, conservative, recommended,
    aggressive), or one place's amount (channel: google_search or newsbreak, with channel_monthly_usd, at least that
    place's minimum; it stays set through later changes and the rest is split around it). It makes a new version of
    the plan with the cards recomputed: a new amount is split again, so a place can join the plan or drop out of it,
    and launching a version that leaves a place out pauses its ads there. The running ads follow the new version once
    the owner approves it (launch_marketing_plan, which this answer's `next` does not offer). An amount above the
    owner's monthly limit is refused: the limit is raised first, with update_marketing_plan. Use a new
    idempotency_key per change. USD a month."""
    if monthly_usd is None and level is None and channel is None and channel_monthly_usd is None:
        raise ToolError(NOTHING_TO_CHANGE)
    if monthly_usd is not None and not 0 < monthly_usd <= 100_000:
        raise ToolError(OUT_OF_RANGE)
    w = where(scope="manage")
    # the same change sent again: its first answer, while the version it made is still the latest; after that its key
    # is refused as a reused one, here, and nothing is routed
    earlier = replayed(idempotency_key, scope="manage", still_stands=still_the_latest(w))
    if earlier is not None:
        return earlier
    # a caller the tool knows, with no plan to change (Task 10), is told what comes first, as the plan's own changes
    # are: a plan begun, the consultation's questions while it asks them, the plan once they are answered
    if w.business_id is not None and w.plan is None:
        raise ToolError(_before_the_plan(w, answered=SEE_THE_PLAN_FIRST))
    return dispatch(
        "adjust",
        plan_id=(w.plan or {}).get("id") or "",
        idempotency_key=idempotency_key,
        monthly_budget_usd=monthly_usd,
        level=level,
        channel=channel,
        channel_monthly_usd=channel_monthly_usd,
    )
