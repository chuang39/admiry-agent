"""The three edits of the "Your ads" page (spec 2026-10-06-admiry-your-ads-page-design.md §3): a platform's amount a
month, the area's radius, a platform's words -- each a preview that stores nothing and a commit that writes the draft
version, both on `adjust`'s own prepare and commit (Y12), always from the approved version (Y6): its sections, and the
answers it was built from (`_base_answers`), not the consultation's as they stand. The route proposes and confirms the
draft in the same transaction and runs `ads_run.launch_now` after it."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from fastmcp.exceptions import ToolError

from . import ads_run, config, plan_words, plans, tools_consult
from .channels import registry

DAY_DIVISOR = 30.4  # the platforms' daily amount (ads_run and the Google and NewsBreak builds divide by it)
RADIUS_MAX = 100  # miles: the farthest `adjust_prepare` takes ("radius_miles must be between 1 and 100")
NOTHING_CHANGED = "Nothing changed."
NOT_LAUNCHED = "Your ads are not launched yet."
PLAN_CHANGED = "Your plan changed since you looked; look again."
WHICH = "Say which platform."
# what each platform does with an approved change (spec Y13), by (channel, kind)
NEXT = {
    ("google_search", "budget"): "Google Search gets the new amount now.",
    ("google_search", "area"): "Google Search redraws the area now.",
    ("google_search", "words"): (
        "Google Search reviews new words before they show, usually within a day; the current ad pauses while the "
        "new one is reviewed."
    ),
    ("newsbreak", "budget"): "NewsBreak gets the new amount now.",
    ("newsbreak", "area"): "NewsBreak takes the new area now.",
    ("newsbreak", "words"): (
        "NewsBreak reviews the new words first; your current ads keep running until the new ones pass."
    ),
}
# what an amount change does to a platform the owner did not edit (final review I1): it holds within the plan's band,
# but a new total in another band can give it another share of it, or none -- said for each one it moves or leaves out
MOVED = "{label} gets {amount} a month now, the most this total gives it."
LEFT_OUT = "{label}: not in this version; Admiry switches its ads off."


@dataclass(frozen=True)
class Change:
    """One edit from one form: `budget` (channel, amount a month), `area` (radius in miles), `words` (channel, the
    cleaned lines)."""

    kind: str
    channel: str | None = None
    amount: int | None = None
    radius: float | None = None
    headlines: tuple[str, ...] = ()
    descriptions: tuple[str, ...] = ()


@dataclass
class Preview:
    """What the review step shows (spec §3.2); `ctx` is adjust's context, which `commit` writes."""

    rows: list[dict]  # {channel, label, old_usd, new_usd, day_usd, changed}, every funded platform in card order
    total_old: float
    total_new: float
    radius_old: float | None
    radius_new: float | None
    words: dict[str, dict]  # channel -> {label, headlines, descriptions, notes}: the edited platform, then its copiers
    next_steps: list[dict]  # {channel, label, sentence}
    ctx: tuple = field(repr=False)


def day_usd(monthly: float) -> float:
    """ "About $X a day": the platform's own figure, floored to the cent (spec Y7)."""
    return math.floor(float(monthly) / DAY_DIVISOR * 100) / 100


def base_plan(conn, biz: str, base_id: str) -> dict:
    """The approved version, which must be the one the page was rendered from (Y6)."""
    approved = ads_run.approved(conn, biz)
    if approved is None:
        raise ToolError(NOT_LAUNCHED)
    if approved["id"] != base_id:
        raise ToolError(PLAN_CHANGED)
    return approved


def _entry(plan: dict, channel: str | None) -> dict:
    if not channel or not registry.known(channel):
        raise ToolError(WHICH)
    found = ads_run.entry(plan, channel)
    if found is None:
        raise ToolError(f"{registry.label(channel)} is not in your plan")
    return found


def _radius(sections: dict) -> float | None:
    value = ((sections.get("consultation") or {}).get("geo") or {}).get("radius_miles")
    return float(value) if value else None


def _amounts(sections: dict) -> dict[str, float]:
    return {c["channel"]: float(c["monthly_usd"]) for c in plans.funded(sections)}


def _base_answers(sections: dict) -> dict:
    """The answers the approved version was built from -- its radius, objective and offer -- as `adjust_prepare`'s own
    fields. adjust rebuilds from the consultation session's answers as they stand, and a draft nobody approved (an
    assistant's `adjust`) leaves its edits there: left alone they would ride unseen on a change made here, which is
    built from the approved version (spec Y6) and approved as the review shows it (§3.2). Passed, these put the
    version's own back, and `adjust_commit` records them, so an approval leaves the session in step with it. What
    adjust would refuse stays with the session, which holds the same value: a radius the owner's own words gave
    outside its range, an objective it does not know. A version without an offer pins an empty one (None would leave
    the session's alone); `consult.facts` then falls back to the known fact, as the version's did."""
    c = sections.get("consultation") or {}
    out: dict = {"offer": c["offer"] if isinstance(c.get("offer"), str) else ""}
    radius = _radius(sections)
    if radius is not None and 0 < radius <= RADIUS_MAX:
        out["radius_miles"] = radius
    if c.get("objective") in plans.OBJECTIVE_WORDS:
        out["objective"] = c["objective"]
    return out


def preview(conn, biz: str, base_id: str, change: Change) -> Preview:
    """The change computed on the approved version and nothing stored: adjust's prepare with, by kind, the platform's
    amount and the total moved by the same difference (Y7), the radius, or the lines each platform's linter kept
    (Y8; refused under a platform's minimum, with the notes), each with the version's own radius, objective and offer
    (`_base_answers`) under it. ToolError carries every refusal's sentence."""
    prev = base_plan(conn, biz, base_id)
    sections = prev["sections"]
    answers = _base_answers(sections)
    words: dict[str, dict] = {}
    if change.kind == "budget":
        entry = _entry(prev, change.channel)
        if change.amount is None:
            raise ToolError(WHICH)
        old = float(entry["monthly_usd"])
        if float(change.amount) == old:
            raise ToolError(NOTHING_CHANGED)
        total = plans.total_usd(sections) - old + float(change.amount)
        ctx = tools_consult.adjust_prepare(
            conn,
            biz,
            base_id,
            **answers,
            channel=change.channel,
            channel_monthly_usd=change.amount,
            monthly_budget_usd=total,
        )
    elif change.kind == "area":
        if change.radius is None:
            raise ToolError("Say how far, in miles.")
        if _radius(sections) == float(change.radius):
            raise ToolError(NOTHING_CHANGED)
        ctx = tools_consult.adjust_prepare(conn, biz, base_id, **{**answers, "radius_miles": float(change.radius)})
    elif change.kind == "words":
        entry = _entry(prev, change.channel)
        category = ((sections.get("consultation") or {}).get("business") or {}).get("category")
        kept_h, kept_d, notes = plan_words.lint(
            change.channel, category, list(change.headlines), list(change.descriptions)
        )
        plan_words.check_counts(change.channel, kept_h, kept_d, notes)
        if kept_h == list(entry.get("headlines") or []) and kept_d == list(entry.get("descriptions") or []):
            raise ToolError(NOTHING_CHANGED)
        words[change.channel] = {
            "label": registry.label(change.channel),
            "headlines": kept_h,
            "descriptions": kept_d,
            "notes": notes,
        }
        for other in plan_words.copiers(sections, change.channel):  # a copy, linted by its own rules (spec §4)
            h, d, n = plan_words.lint(other, category, kept_h, kept_d)
            plan_words.check_counts(other, h, d, n)
            words[other] = {"label": registry.label(other), "headlines": h, "descriptions": d, "notes": n}
        ctx = tools_consult.adjust_prepare(
            conn, biz, base_id, **answers, channel=change.channel, headlines=kept_h, descriptions=kept_d
        )
    else:
        raise ToolError("Say what to change.")
    new_sections = ctx[4][0]
    before, after = _amounts(sections), _amounts(new_sections)
    order = [c for c in registry.ORDER if c in before or c in after]
    order += [c for c in [*before, *after] if c not in order]
    rows = [
        {
            "channel": ch,
            "label": registry.label(ch),
            "old_usd": before.get(ch),
            "new_usd": after.get(ch),
            "day_usd": day_usd(after[ch]) if ch in after else None,
            "changed": before.get(ch) != after.get(ch),
        }
        for ch in order
    ]
    if change.kind == "budget":  # the edited platform, then each other one the new total moved or left out (I1)
        reached = [change.channel, *(ch for ch in order if ch != change.channel and before.get(ch) != after.get(ch))]
    elif change.kind == "area":
        reached = [ch for ch in order if ch in after]
    else:
        reached = list(words)
    settings = config.load()
    steps = []
    for ch in reached:
        if not registry.known(ch):
            continue
        label = registry.label(ch)
        if registry.exec_mode(settings, ch) == "off":
            sentence = ads_run.OFF_CHANNEL.format(label=label)
        elif change.kind == "budget" and ch != change.channel:
            sentence = (
                MOVED.format(label=label, amount=plans.usd(after[ch])) if ch in after else LEFT_OUT.format(label=label)
            )
        else:
            sentence = NEXT.get((ch, change.kind), "")
        if sentence:
            steps.append({"channel": ch, "label": label, "sentence": sentence})
    return Preview(
        rows=rows,
        total_old=plans.total_usd(sections),
        total_new=plans.total_usd(new_sections),
        radius_old=_radius(sections),
        radius_new=_radius(new_sections),
        words=words,
        next_steps=steps,
        ctx=ctx,
    )


def commit(conn, biz: str, base_id: str, change: Change) -> dict:
    """The preview computed again from the same inputs and written: the new draft version, for the caller to propose
    and confirm in the same transaction (spec §3.3)."""
    return tools_consult.adjust_commit(conn, biz, preview(conn, biz, base_id, change).ctx)
