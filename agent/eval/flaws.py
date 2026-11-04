"""Deliberately flawed plans (spec 2026-10-07 capability layer §7; K5 P3, P4): six copies of the golden set's frozen
plans, each broken in one rubric dimension on a base of its own across the three verticals, so the judge can be shown
to tell a flawed plan from its clean base before the founder's grading -- and the founder grades them too, so the
alignment covers low grades. Code makes them from the base's frozen plan, the same way every time (`flawed`);
`agent.eval freeze --from` writes them into agent/eval/flawed/ (bundle.write_flawed)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from .. import plans

SCALE = 3  # budget-over-limit: the plan spends this many times what it did
WIDE_MILES = 120  # fit-area: far past how far a plumber drives
BAD_HEADLINES = [
    "Pain-Free Emergency Dentist",
    "Guaranteed Same-Day Relief",
    "#1 Dentist in Maple Grove",
    "50% Off Your First Visit",
    "Open 24/7 for Emergencies",
    "Perfect Smile, Guaranteed",
]
BAD_DESCRIPTIONS = [
    "Painless treatment, guaranteed. Half price for new patients this month.",
    "Rated the best dentist in Minnesota. Any tooth fixed in one visit.",
]
WASTE_THEMES = [
    "free will template",
    "estate planning paralegal jobs",
    "how to write a will yourself",
    "lawyer salary columbus",
]
JARGON_WHY = (
    "We'll maximize ROAS with broad-match CPC bidding, RLSA audiences and PMax-style expansion to lift CTR and cut CPA."
)
JARGON_LATER = (
    "TikTok's CPMs make top-of-funnel prospecting inefficient until conversion-rate data accrues for lookalike seeding."
)


@dataclass(frozen=True)
class Flaw:
    """One way to break a frozen plan: its id, the case whose plan it breaks, the rubric dimension it breaks, one line
    on what it does (the eval log's), and the function that breaks a copy in place (given the base's knowledge)."""

    id: str
    base: str
    dimension: str
    what: str
    apply: Callable[[dict, dict], None]


def _round5(x: float) -> int:
    return int(5 * round(x / 5))


def _wide_area(plan: dict, knowledge: dict) -> None:
    plan["sections"]["consultation"]["geo"]["radius_miles"] = WIDE_MILES


def _over_limit(plan: dict, knowledge: dict) -> None:
    """SCALE times the money, and the planning column and the lead range scaled with it, as the benchmark's linear
    estimate would (so only the budget is wrong, never the arithmetic)."""
    s = plan["sections"]
    for c in plans.funded(s):
        c["monthly_usd"] = float(c["monthly_usd"]) * SCALE
    est = s["estimate"]
    col = (est.get("columns") or {}).get("planning")
    if col:
        col["spend"] = int(col["spend"]) * SCALE
        col["leads"] = [_round5(x * SCALE) for x in col["leads"]]
        col["bookings"] = [plans.half_up(x * SCALE) for x in col["bookings"]]
        col["customers"] = [plans.half_up(x * SCALE) for x in col["customers"]]
        col["revenue"] = [plans.round_revenue(x * SCALE) for x in col["revenue"]]
    for key in ("low", "high"):
        if isinstance(est.get(key), int | float):
            est[key] = _round5(est[key] * SCALE)
    est["per_channel"] = {ch: [round(x * SCALE, 2) for x in v] for ch, v in (est.get("per_channel") or {}).items()}


def _bad_claims(plan: dict, knowledge: dict) -> None:
    lead = plans.primary(plan["sections"])
    lead["headlines"], lead["descriptions"] = list(BAD_HEADLINES), list(BAD_DESCRIPTIONS)


def _wasted_searches(plan: dict, knowledge: dict) -> None:
    lead = plans.primary(plan["sections"])
    lead["themes"], lead["negatives"] = list(WASTE_THEMES), []


def _promised(plan: dict, knowledge: dict) -> None:
    est = plan["sections"]["estimate"]
    n = max(int(est.get("high") or 0), 5)
    for col in (est.get("columns") or {}).values():
        col["leads"] = [n, n]
    est["low"] = est["high"] = n
    est["assumptions"] = []
    est["footnote"] = f"Guaranteed: {n} new customers every month."


def _jargon(plan: dict, knowledge: dict) -> None:
    why = plan["sections"]["rationale"]
    why["why_primary"], why["why_not_later"] = JARGON_WHY, JARGON_LATER
    plan["rationale"] = JARGON_WHY


FLAWS: dict[str, Flaw] = {
    f.id: f
    for f in (
        Flaw("fit-area", "home_services-2", "fit", "the ads reach 120 miles around a 24/7 plumber", _wide_area),
        Flaw("budget-over-limit", "dentists-1", "budget", "three times the monthly budget the owner gave", _over_limit),
        Flaw(
            "copy-claims",
            "dentists-2",
            "copy",
            "outcome promises, a discount, a rating and hours the knowledge does not state",
            _bad_claims,
        ),
        Flaw(
            "keywords-waste",
            "attorneys-1",
            "keywords",
            "job, salary, free and do-it-yourself searches, nothing left out",
            _wasted_searches,
        ),
        Flaw("estimate-promise", "home_services-1", "estimate", "one number, no basis, a guarantee", _promised),
        Flaw("clarity-jargon", "home_services-3", "clarity", "the plan's reasons in advertising jargon", _jargon),
    )
}


def flawed(flaw: Flaw, plan: dict, knowledge: dict) -> dict:
    """A copy of `plan` broken by `flaw`; `plan` itself is never changed."""
    out = json.loads(json.dumps(plan))
    flaw.apply(out, knowledge)
    return out
