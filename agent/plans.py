"""The Plan (spec §4.1): plan.json built from a model draft, the owner-facing lines in the
founder's order and wording, the estimate from the benchmark table (never from the model), and
the plan table. Budgets from the model are clamped to the goal's monthly limit here."""

from __future__ import annotations

import math
from itertools import zip_longest
from typing import TYPE_CHECKING
from urllib.parse import quote

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import allocate, benchmark, config, db, launch_review, places, profile
from .channels import registry
from .models import PlanDraft
from .roles import queue as role_queue
from .util import iso

if TYPE_CHECKING:
    from .channels.newsbreak.creative import Image

CHANNEL_NAMES = registry.LABELS  # the D76 owner names; this name is kept one release for its importers
HEADLINE_MAX, DESCRIPTION_MAX = 30, 90
MIN_HEADLINES, MIN_DESCRIPTIONS = 3, 2  # a responsive search ad's minimums: the draft and the adapter's precheck
# Both ways the ads can run, side by side (the founder's ruling, 2026-10-13: the owner chooses, so neither is named as
# the given one): the prepaid balance at cost, or Google billing the owner's own account; no management fee
DISCLOSURE = (
    "Admiry runs the ads from your prepaid balance at cost, or Google bills your own Google Ads account; Admiry charges "
    "no management fee."
)
AUTOPILOT_LINE = {
    "off": "automatic optimization: off",
    "safe": "automatic optimization: safe changes only",
    "all": "automatic optimization: on",
}
SECTION_ORDER = ("presence", "discovery", "ads", "creative", "conversion", "optimization", "estimate", "disclosure")
PLAN_COLS = "id, business_id, goal_id, version, json, status, estimate_basis, diff_from, created_at"
LEVELS = {"conservative": 0.45, "recommended": 1.0, "aggressive": 1.67}
DAYS = 30  # a month of budget is DAYS × the daily amount, and the card's "a day" is the month ÷ DAYS (floored)
MIN_MONTHLY_USD = 10 * DAYS  # the "minimum" level: $10 a day, below any level the benchmark would pick
FOOTNOTE = "Planning estimates from published averages for US {noun}, not a forecast. Actual results vary."
MIN_ESTIMATE_SPEND = 100  # benchmark.estimate refuses a $0 channel budget; below this, refuse with an
# owner-readable message instead of crashing (Task 4 review round 1)


def usd(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def plan_url(plan_id: str) -> str:
    """Where the owner reads (and, with A1b, approves) a plan."""
    return f"{config.load().web_base_url}/plan/{plan_id}"


def _merged_channels(draft: PlanDraft) -> list[dict]:
    """The funded channels, one entry each: a channel the draft names twice is merged (budgets
    summed, lists concatenated, the first landing_service kept)."""
    merged: dict[str, dict] = {}
    for ch in draft.ads:
        if ch.monthly_usd <= 0:
            continue
        m = merged.get(ch.channel)
        if m is None:
            merged[ch.channel] = {
                "channel": ch.channel,
                "monthly_usd": ch.monthly_usd,
                "themes": list(ch.themes),
                "negatives": list(ch.negatives),
                "headlines": list(ch.headlines),
                "descriptions": list(ch.descriptions),
                "landing_service": ch.landing_service,
            }
            continue
        m["monthly_usd"] += ch.monthly_usd
        for k in ("themes", "negatives", "headlines", "descriptions"):
            m[k] += getattr(ch, k)
    return list(merged.values())


def normalize(draft: PlanDraft, monthly_limit: float | None, *, autopilot: str | None = None) -> tuple[dict, list[str]]:
    """The draft as JSON-native sections with the code-enforced limits applied; returns (sections, notes).
    `autopilot` (the goal's setting) overrides the draft's when given."""
    notes: list[str] = []
    limit = float(monthly_limit or 0)
    ads = []
    for ch in _merged_channels(draft):
        headlines = [h for h in ch["headlines"] if len(h) <= HEADLINE_MAX]
        descriptions = [d for d in ch["descriptions"] if len(d) <= DESCRIPTION_MAX]
        dropped = (len(ch["headlines"]) - len(headlines)) + (len(ch["descriptions"]) - len(descriptions))
        if dropped:
            notes.append(f"{registry.label(ch['channel'])}: {dropped} ad line(s) over the length limit were dropped")
        ads.append(
            {**ch, "monthly_usd": round(ch["monthly_usd"], 2), "headlines": headlines, "descriptions": descriptions}
        )
    total = sum(a["monthly_usd"] for a in ads)
    if ads and limit <= 0:
        notes.append("no monthly limit set: advertising budgets were removed")
        ads = []
    elif total > limit:
        scale = limit / total
        for a in ads:
            a["monthly_usd"] = float(int(a["monthly_usd"] * scale))
        notes.append(f"budgets scaled to the {usd(limit)} monthly limit")
        for a in ads:
            if a["monthly_usd"] <= 0:  # truncation can leave a channel with nothing: it is not funded
                notes.append(f"{registry.label(a['channel'])}: below $1 after scaling, removed")
        ads = [a for a in ads if a["monthly_usd"] > 0]
    sections = {
        "presence": {"pages": [p.model_dump() for p in draft.pages], "gbp_notes": draft.gbp_notes},
        "discovery": {"fixes": draft.discovery_fixes, "monthly_usd": 0},
        "ads": {"channels": ads, "monthly_limit_usd": limit},
        "creative": {"angles": draft.creative_angles},
        "conversion": {"tracking": draft.tracking, "review_request_after_days": draft.review_request_after_days},
        "optimization": {"autopilot": autopilot or draft.autopilot},
        "disclosure": {"text": DISCLOSURE},
    }
    return sections, notes


def half_up(x: float) -> int:
    """Round half up (Python's round() is banker's: round(22.5) == 22, which the pinned numbers must not depend on)."""
    return math.floor(float(x) + 0.5)


def round100(x: float) -> int:
    return 100 * half_up(float(x) / 100)


def round_revenue(x: float) -> int:
    """A revenue figure for the owner: the nearest $50 under $1,000, so a low customer value is not shown as $500
    steps, and the nearest $500 from there."""
    step = 50 if x < 1000 else 500
    return step * half_up(float(x) / step)


def levels(recommended_usd: int, limit: float | None) -> dict[str, int]:
    """Monthly amounts, cheapest first: the fixed $10-a-day minimum, then the benchmark levels (conservative never
    below the minimum), each clipped to the owner's limit."""
    out = {"minimum": MIN_MONTHLY_USD}
    out.update({k: max(MIN_MONTHLY_USD, round100(recommended_usd * f)) for k, f in LEVELS.items()})
    if limit and limit > 0:
        out = {k: min(v, int(limit)) for k, v in out.items()}
    return out


def funded(sections: dict) -> list[dict]:
    """The plan's channel entries that carry money, in the plan's order."""
    return [c for c in (sections.get("ads") or {}).get("channels") or [] if c.get("monthly_usd")]


def primary(sections: dict) -> dict:
    """The primary row (`role == 'primary'`, Google Search since spec 2026-09-29 §6.2); on an older plan without
    roles, Google Search's entry; else the first funded one; {} when nothing is funded."""
    rows = funded(sections)
    return (
        next((c for c in rows if c.get("role") == "primary"), None)
        or next((c for c in rows if c["channel"] == allocate.PRIMARY), None)
        or (rows[0] if rows else {})
    )


def copy_of(entry: dict) -> str | None:
    """The channel whose ad text `entry` carries -- the draft wrote none for it, so it has another channel's until its
    own build fits them (M1) -- or None when the text is its own. Never the primary's entry: Google Search's build runs
    the text under its name and the chat card previews it, so a mark a version stored on it (before the final wave's
    re-review) is ignored."""
    return None if entry.get("channel") == allocate.PRIMARY else entry.get("copy_of") or None


def total_usd(sections: dict) -> float:
    """The plan's amount: the sum of its funded rows (the split always sums to it, to the dollar)."""
    return float(sum(float(c["monthly_usd"]) for c in funded(sections)))


def pins(sections: dict) -> dict[str, int]:
    """The amounts the owner set per channel (adjust's `channel_monthly_usd`), which every later version keeps."""
    return {ch: int(v) for ch, v in ((sections.get("ads") or {}).get("named") or {}).items()}


def default_mode(channel: str) -> str:
    """How a channel runs for a business without an account row: its own account when the adapter offers that
    (spec 2026-09-29 §2's default order), else Admiry runs it from the balance."""
    modes = registry.adapter(channel).capabilities().modes if registry.known(channel) else {"own_account"}
    return "own_account" if "own_account" in modes else "managed"


OWN_ACCOUNT_TEXT = {"google_search": "your own Google Ads account"}
MANAGED_TEXT = "Admiry runs it from your balance"


def mode_text(channel: str, mode: str | None) -> str:
    if (mode or default_mode(channel)) == "managed":
        return MANAGED_TEXT
    return OWN_ACCOUNT_TEXT.get(channel, f"your own {registry.label(channel)} account")


# How a place runs in Admiry's answerers' material (`ways`, walk fixes 1): the other way for a place the owner's own
# account runs (A3c-1: Google Search's exists once `google_open`), and a prepaid business's Google Search while Admiry
# does not run it from a balance
OTHER_WAY_SOON = "; Admiry running it from a prepaid balance is coming"
OTHER_WAY = "; Admiry can also run it from a prepaid balance"
GOOGLE_WAITS = "waits for Admiry-run accounts, which are coming"


def google_open() -> bool:
    """Whether Admiry runs Google Search from a balance yet: its adapter declares the mode (A3c-1, a pool account of
    Admiry's: `google_managed`)."""
    return "managed" in registry.adapter(allocate.PRIMARY).capabilities().modes


def ways(rows: list[dict]) -> list[str]:
    """How each place a plan funds runs, and the other way it could, one line per card row (`cards()["plan"]
    ["channels"]`): the material Admiry's answerers read (`ask`, and the e-mail channel's). A place Admiry runs from
    the balance says so; the owner's own account adds the prepaid way -- open for Google Search once `google_open`,
    otherwise coming; a prepaid business's Google Search, while Admiry does not run it from a balance, waits."""
    out = []
    for r in rows:
        managed = r["mode_text"] == MANAGED_TEXT
        google = r["platform"] == allocate.PRIMARY
        if managed and google and not google_open():
            out.append(f"{r['label']}: {GOOGLE_WAITS}")
        else:
            other = "" if managed else (OTHER_WAY if google and google_open() else OTHER_WAY_SOON)
            out.append(f"{r['label']}: {r['mode_text']}" + other)
    return out


def add_estimate(conn: psycopg.Connection, sections: dict, category: str) -> dict:
    """Fill sections['estimate'] from the benchmark table and return the estimate_basis ({} when
    nothing is funded — spec §4.1: never an estimate without a basis)."""
    budgets = {a["channel"]: a["monthly_usd"] for a in sections["ads"]["channels"]}
    if not any(v > 0 for v in budgets.values()):
        sections["estimate"] = {"low": None, "high": None, "basis": "none", "confidence": None}
        return {}
    try:
        est = benchmark.estimate(conn, category, budgets)
    except (ValueError, LookupError) as e:
        sections["estimate"] = {"low": None, "high": None, "basis": "none", "confidence": None, "note": str(e)}
        return {}
    sections["estimate"] = est.as_json()
    return est.as_json()


class BelowMinimum(ValueError):
    """A monthly budget too small to estimate; its message is the owner's sentence (the tools refuse with it)."""


def _estimate(conn: psycopg.Connection, category: str, split: dict[str, int]) -> benchmark.Estimate:
    """The lead estimate over the split's channels that have a published benchmark row; a split none of whose channels
    has one (NewsBreak alone, spec 2026-09-29 P8) estimates no leads -- its money is spent, and learned from."""
    if any(benchmark.lookup(conn, category, ch) for ch in split):
        return benchmark.estimate(conn, category, split)
    return benchmark.Estimate(low=0, high=0, basis="none", confidence="low", rows=[])


def _rates(est: benchmark.Estimate) -> tuple[float, float]:
    """The lead-to-booking rate at each end of the lead range: every channel's own rate (spec 2026-09-29 §6.1),
    weighted by the leads that channel brings; a single rate (one channel, or rates alike) exactly."""
    rates = {r["channel"]: r["lead_to_booking"] for r in est.rows}
    if not rates:
        return 0.0, 0.0
    if len(set(rates.values())) == 1:
        rate = next(iter(rates.values()))
        return rate, rate
    ends = []
    for end in (0, 1):
        leads = sum(est.per_channel[ch][end] for ch in rates)
        ends.append(sum(est.per_channel[ch][end] * rate for ch, rate in rates.items()) / leads if leads else 0.0)
    return ends[0], ends[1]


def _column(est: benchmark.Estimate, spend: int, b2c: float, value: float) -> dict:
    leads = [est.low, est.high]
    bookings = [half_up(x * rate) for x, rate in zip(leads, _rates(est), strict=True)]
    customers = [half_up(x * b2c) for x in bookings]
    revenue = [round_revenue(x * value) for x in customers]
    return {"spend": spend, "leads": leads, "bookings": bookings, "customers": customers, "revenue": revenue}


def estimate_columns(
    conn: psycopg.Connection,
    category: str,
    *,
    split: dict[str, float],
    recommended_split: dict[str, float],
    customer_value_usd: float,
    value_source: str,
    footnote_noun: str,
) -> dict:
    """Two economics columns (planning at the plan's split, recommended at the recommended level's), each
    with a basis for every factor — spec §7 — summed over the funded channels (spec 2026-09-29 §6.2): a channel
    without a published row adds its spend and no leads, and its assumption says so in the spec's words; every other
    assumption names its `(category, channel)` row -- Google Search's under the plain names (the Google build reads its
    cost per lead there), every other channel's with its owner name. A split none of whose channels has a row is
    `basis: "none"` with that sentence as its `reason`. `estimate_basis`-shaped `low`/`high`/`basis`/`per_channel`/
    `benchmark_rows`/`confidence` keys are kept at the top level so owner_lines and /plan can read them unchanged.

    Raises BelowMinimum (a ValueError, the owner's sentence) below MIN_ESTIMATE_SPEND rather than degrade to a "not
    estimated" payload: the owner card needs both columns or a clear refusal, never a silent gap."""
    planning_split = {ch: int(v) for ch, v in split.items() if v and v > 0}
    if sum(planning_split.values()) < MIN_ESTIMATE_SPEND:
        raise BelowMinimum(f"a monthly budget below {usd(MIN_ESTIMATE_SPEND)} cannot be estimated")
    recommended_split = {ch: int(v) for ch, v in recommended_split.items() if v and v > 0}
    if sum(recommended_split.values()) < MIN_ESTIMATE_SPEND:  # a recommendation below the minimum: estimated at it
        recommended_split = {allocate.PRIMARY: MIN_ESTIMATE_SPEND}
    l2b, b2c, funnel_basis = benchmark.funnel(conn, category)
    planning = _estimate(conn, category, planning_split)
    recommended = _estimate(conn, category, recommended_split)
    rows = {r["channel"]: r for r in planning.rows}
    costs, bookings = [], []
    for channel in planning_split:  # the plan's order
        search = channel == allocate.PRIMARY
        named = "" if search else f", {registry.label(channel)}"
        row = rows.get(channel)
        if row is None:  # no published cost per lead: spent, not estimated (P8)
            costs.append({"name": f"cost per lead{named}", "value": "not published", "basis": allocate.NO_BENCHMARK})
            continue
        cpl_lo, cpl_hi = row["cpl_avg"] * benchmark.LOW_FACTOR, row["cpl_avg"] * benchmark.HIGH_FACTOR
        costs.append(
            {
                "name": f"cost per lead{named}",
                "value": f"${cpl_lo:,.0f}–{cpl_hi:,.0f}",
                "basis": f"benchmark {row['id']} US, confidence {row['confidence']}",
            }
        )
        bookings.append(
            {
                "name": f"lead to booking{named}",
                "value": f"{(l2b if search else row['lead_to_booking']):.0%}",
                "basis": funnel_basis if search else f"benchmark {row['id']}, confidence {row['confidence']}",
            }
        )
    assumptions = [
        *costs,
        *bookings,
        {"name": "booking to customer", "value": f"{b2c:.0%}", "basis": funnel_basis},
        {
            "name": "customer value",
            "value": usd(customer_value_usd),
            "basis": {"owner": "your answer", "estimate": "our estimate for your business"}.get(
                value_source, f"benchmark {category}, confidence low"
            ),
        },
    ]
    return {
        "columns": {
            "recommended": _column(recommended, sum(recommended_split.values()), b2c, customer_value_usd),
            "planning": _column(planning, sum(planning_split.values()), b2c, customer_value_usd),
        },
        "assumptions": assumptions,
        "footnote": FOOTNOTE.format(noun=footnote_noun),
        "basis": "benchmark" if planning.rows else "none",
        **({} if planning.rows else {"reason": allocate.NO_BENCHMARK}),
        "confidence": planning.confidence,
        "benchmark_rows": [r["id"] for r in planning.rows],
        "low": planning.low,
        "high": planning.high,
        "per_channel": {c: list(v) for c, v in planning.per_channel.items()},
    }


OBJECTIVE_WORDS = {
    "bookings": "bookings",
    "calls": "phone calls",
    "enquiries": "enquiries",
    "awareness": "awareness",
    "any": "customers",
}
CONVERSIONS = {
    "bookings": ["consultation_booking", "phone_call_60s"],
    "calls": ["phone_call_60s"],
    "enquiries": ["form_submit", "phone_call_60s"],
    "awareness": ["page_view"],
    "any": ["consultation_booking", "phone_call_60s"],
}
AUDIENCE = [
    {"signal": "local_search_intent", "role": "primary"},
    {"signal": "website_visitors", "role": "retargeting"},
    {"signal": "in_market", "role": "expansion"},
    {"signal": "broad_local", "role": "test"},
]
LATER = [{"platform": "tiktok", "when": "after 30 days of booking data"}]
# Not channels (spec 2026-09-29-admiry-channels-design §2): later lines with one sentence each, no adapter, no budget.
LATER_LABELS = {"tiktok": "TikTok", "yelp": "Yelp"}
YELP = {"platform": "yelp", "label": "Yelp", "why": "Not now: Yelp opens its ads only to large partners."}
# The later lines of channels (`later_rows`): report-only (D83), not built yet, and ChatGPT before M8 (D103).
LATER_REPORT_ONLY = "Run by Admiry from the platform's own report when you ask for it."
LATER_COMING = "Not in this plan yet; coming to Admiry."
LATER_CHATGPT = "Later: Admiry is trying it on its own ads first."
LATER_PREPAID = "Not in this plan; Admiry runs it only for businesses that prepay."
LATER_PICTURE = "Not in this plan yet; once Admiry has a picture for your trade."
# Why a channel the plan would have funded is held back (plans_channels.held): its switch is off, its ads show a
# picture and Admiry's set has none for the trade yet (M1 Task 7 fix round 1, item 3), or it runs only from a balance
# Admiry holds and the business does not prepay.
HELD = {"coming": LATER_COMING, "picture": LATER_PICTURE, "prepaid": LATER_PREPAID}


def held_note(channel: str, why: str) -> str:
    """A held channel in a plan's notes or a refusal, e.g. "NewsBreak: not in this plan; Admiry runs it only for
    businesses that prepay."."""
    sentence = HELD.get(why, LATER_COMING)
    return f"{registry.label(channel)}: {sentence[:1].lower()}{sentence[1:]}"


def consultation_section(facts: dict, knowledge: dict, *, level: str, listing: dict | None) -> dict:
    """The consultation's answers as a plan section (spec §7 `consultation`): what the owner told the
    server, frozen against the plan version it produced (cards() and the model prompt both read it)."""
    return {
        "business": {
            "category": knowledge.get("category", "fallback"),
            "service": facts.get("service"),
            "services": facts.get("services") or [],
            "location": (listing or {}).get("address") or ", ".join(knowledge.get("service_area") or []) or None,
            "address_source": "places" if listing else ("knowledge" if knowledge.get("service_area") else None),
        },
        "objective": facts["objective"],
        "offer": facts.get("offer"),
        "customer_value_usd": facts["customer_value_usd"],
        "customer_value_source": facts["customer_value_source"],
        "geo": {"radius_miles": facts.get("radius_miles"), "center": (listing or {}).get("place_id")},
        "audience": AUDIENCE,
        "conversions": CONVERSIONS[facts["objective"]],
        "duration_days": 30,
        "level": level,
        "later": LATER,
    }


def rationale_section(knowledge: dict, facts: dict) -> dict:
    """Why Google Search now and TikTok later, plus example searches for the owner (spec §7 `rationale`)."""
    chosen = facts.get("services") or ([facts["service"]] if facts.get("service") else [])
    service = chosen[0] if chosen else "your service"
    own = {s.get("name"): list(s.get("intent_phrases") or []) for s in knowledge.get("services", [])}
    phrases: list[str] = []
    for row in zip_longest(*(own.get(name, []) for name in chosen)):  # several services take turns
        phrases += [p for p in row if p and p not in phrases]
    area = (knowledge.get("service_area") or [""])[0]
    examples = phrases[:4] or [
        f"{service.lower()} near me",
        f"{service.lower()} {area}".strip(),
        f"{service.lower()} cost",
    ]
    return {
        "why_primary": (
            "Your customer shows up with strong intent: they type what they want, close to the moment they decide. "
            "Google Search meets them there."
        ),
        "why_not_later": (
            "TikTok is good at making people nearby aware of you, but the goal today is "
            f"{OBJECTIVE_WORDS[facts['objective']]}. Once Search has 30 days of results we will know what one costs "
            "you, and can test TikTok against that number."
        ),
        "example_searches": examples,
    }


def name_for(knowledge: dict | None) -> str | None:
    """The business name the goal sentence may lead with: only one a website, a Google listing or the owner vouches
    for. A name the model derived from the owner's own words ("H1B Visa Lawyer") reads as a label, not a place, so
    the sentence says "your business" instead (founder's dev walk, 2026-09-28). The name only when it is text with
    something in it, stripped (`profile.clean_name`): one stored as an object or a number before update_business
    checked its shape never reaches a card or a sentence (open item B3). A listing vouches only when the owner
    confirmed it as the business's (listings.link): an address the owner placed names a place, and a lookup's guess
    names nothing.

    Where the name came from (`name_source`, A3p-5 batch 2 Task 0, ruling R0-k) decides the rest: a name a reading of
    a site gave ("site") or the owner's own (the answer to the name question, or a name update_business patched:
    "owner") is vouched for; one a reading of plain words gave ("words") is not, even beside the `website` an earlier
    reading read and profile.carry kept (ruling F3). A knowledge stored before the field existed (no `name_source`)
    keeps the old rule: its `website` vouches."""
    k = knowledge or {}
    if "name_source" in k:
        vouched = k["name_source"] in ("site", "owner")
    else:
        vouched = bool(k.get("website"))
    return profile.clean_name(k.get("name")) if vouched or profile.business_listing(k) else None


def address_for(knowledge: dict | None) -> str | None:
    """The place a sentence may centre on when no name is vouched for: the address of a listing the owner placed
    (kind `address`), as a sentence says it (places.display: without the country), else None."""
    listing = (knowledge or {}).get("listing") if isinstance((knowledge or {}).get("listing"), dict) else {}
    return places.display(listing.get("address")) if listing.get("kind") == "address" else None


def channel_rows(sections: dict) -> list[dict]:
    """The plan card's rows (spec 2026-09-29-admiry-channels-design §7.2), one per funded channel in the plan's order;
    an entry stored before the split (no role, reason or mode) is described in the allocator's words."""
    c = sections["consultation"]
    rows, total, named = funded(sections), int(total_usd(sections)), pins(sections)
    out = []
    for r in rows:
        ch, amount = r["channel"], int(r["monthly_usd"])
        role = r.get("role") or allocate.ROLE.get(ch, "test")
        service = c["business"].get("service")
        basis = r.get("basis") or ("named" if ch in named else "benchmark")
        why = r.get("why") or allocate.why(ch, role, basis, amount, total=total, service=service, pin=named.get(ch))
        out.append(
            {
                "platform": ch,
                "label": registry.label(ch),
                "role": role,
                "monthly_usd": amount,
                "per_day": amount // DAYS,
                "why": why,
                "counts_as_result": list(c.get("conversions") or []),
                "mode_text": mode_text(ch, r.get("mode")),
            }
        )
    return out


def later_rows(sections: dict) -> list[dict]:
    """The plan card's later lines, one sentence each (spec 2026-09-29 §2, §7.2, D83, D103), channels in card order:
    every channel the plan would have funded but the business cannot have yet (`ads.held`: switched off, no picture
    for the trade, or run only for businesses that prepay), every registered channel that cannot create and is not
    funded (Admiry reads its own report), every channel the draft proposed that has no adapter yet, ChatGPT until it
    is funded; then Yelp and TikTok, always. A channel that is neither registered nor proposed has no line."""
    paid = {c["channel"] for c in funded(sections)}
    drafted = set((sections.get("ads") or {}).get("later") or [])
    kept_back = (sections.get("ads") or {}).get("held") or {}
    lines = []
    for ch in registry.ORDER:
        if ch in paid:
            continue
        if ch in kept_back:
            why = HELD.get(kept_back[ch], LATER_COMING)
        elif registry.known(ch) and not registry.adapter(ch).capabilities().create:
            why = LATER_REPORT_ONLY
        elif ch in drafted and not registry.known(ch):
            why = LATER_COMING
        elif ch == "chatgpt_ads":
            why = LATER_CHATGPT
        else:
            continue
        lines.append({"platform": ch, "label": registry.label(ch), "why": why})
    tiktok = [
        {
            "platform": x["platform"],
            "label": LATER_LABELS.get(x["platform"], x["platform"]),
            "why": f"Worth a test {x['when']}. Not now.",
        }
        for x in sections["consultation"].get("later") or LATER
    ]
    return [*lines, YELP, *tiktok]


def level_rows(sections: dict) -> dict[str, dict]:
    """Each budget level's amount and the split recomputed at it (spec §7.2: the level control shows it at once); a
    plan stored before the split ran on one channel at every level."""
    splits = (sections.get("ads") or {}).get("splits")
    lead = primary(sections).get("channel", allocate.PRIMARY)
    return {
        level: {"monthly_usd": amount, "split": splits.get(level, {}) if splits is not None else {lead: amount}}
        for level, amount in (sections.get("levels") or {}).items()
    }


# The picture a channel shows with the ad (D89, plan 2026-09-29-agent-m1-newsbreak P7, M1 Task 7): the plan card's line
# under it, and its line while the business's trade has no picture in Admiry's set yet; the "Ready to launch" card shows
# no picture, so its step says which picture it means
IMAGE_SHOWN = "{label} shows this picture with your ad; approving the plan approves it"
# the version's picture is no longer in Admiry's set (M1 Task 7 fix round 1, item 4): said, never swapped for another
# -- ads already running keep the picture they carry, and a new picture comes with a new version (final review I3)
IMAGE_GONE = (
    "The picture for your {label} ad is no longer in Admiry's set: {label} ads already running keep their current "
    "picture while a new version of your plan waits for a new one"
)
# a version that funds a channel showing a picture with none pinned -- made before the pin, never approved
IMAGE_NONE = "This version of your plan has no picture for your {label} ad, so {label} waits for a new version"
IMAGE_STEP = "{label} shows the picture on your plan with your ad; approving the plan approves it"


def image_channels(sections: dict) -> list[str]:
    """The funded channels whose ads show a picture (`Capabilities.needs_image`, D89: NewsBreak in M1), in plan order."""
    return [
        c["channel"]
        for c in funded(sections)
        if registry.known(c["channel"]) and registry.adapter(c["channel"]).capabilities().needs_image
    ]


def _first_image(images: object) -> str | None:
    """The file of a {channel: file} map (one picture per version, whatever channels show it); None for none."""
    return next((f for f in images.values() if isinstance(f, str) and f), None) if isinstance(images, dict) else None


def pinned(sections: dict) -> str | None:
    """The file of the picture this version shows with its ads (M1 Task 7 fix round 1, item 4): pinned when the version
    was made (`sections.creative.proposed_image`, `pin_picture`) -- or, on a version made before the pin, the one its
    approval stored (`approved_image`); None for a version with no picture."""
    creative_ = sections.get("creative") or {}
    return _first_image(creative_.get("proposed_image")) or _first_image(creative_.get("approved_image"))


def in_set(file: str | None) -> bool:
    """Admiry's licensed set (`creative.manifest`, read once until it changes) still has the picture `file` names."""
    from .channels.newsbreak import creative  # deferred: the channel package imports this module at load time

    return creative.find(file) is not None


def choose_picture(category: str | None, service: str | None, *, previous: dict | None = None) -> Image | None:
    """The picture a new version pins (`plans_channels.fund`): the one the version it comes from showed (`previous`,
    its sections: `pinned`) while the set still has it for the trade -- the running ads keep their picture, and
    NewsBreak reviews nothing new -- else `creative.pick` by the trade and the service; None when the trade has none."""
    from .channels.newsbreak import creative  # deferred: the channel package imports this module at load time

    return creative.pick(category or "fallback", service, kept=pinned(previous or {}))


def pin_picture(sections: dict, shown: Image | None) -> None:
    """Pins `shown` on the version for every funded channel that shows a picture (`sections.creative.proposed_image`:
    what its card shows and approving it approves), or drops the pin when it funds none. The creative section is
    replaced, never changed in place: it may be the previous version's own dict."""
    channels = image_channels(sections)
    rest = {k: v for k, v in (sections.get("creative") or {}).items() if k != "proposed_image"}
    if channels and shown is not None:
        rest["proposed_image"] = dict.fromkeys(channels, shown.file)
    sections["creative"] = rest


def picture(sections: dict) -> Image | None:
    """The licensed picture the plan's image channels show (P7): the version's own (`pinned`) while the set still has
    it -- what its card shows and what approving it stores (`approved_creative`) -- never another one in its place;
    None when it has none, or its picture left the set. Reads the set (`creative.manifest`, read once until it
    changes)."""
    from .channels.newsbreak import creative  # deferred: the channel package imports this module at load time

    return creative.find(pinned(sections))


def _missing_line(sections: dict, label: str) -> str:
    """The line for a version whose channel shows no picture: its pinned one left the set, or it pinned none."""
    return (IMAGE_GONE if pinned(sections) else IMAGE_NONE).format(label=label)


def _picture_preview(sections: dict) -> dict:
    """ads_preview's picture (P7) for a plan with a channel that shows one: `image` {"url", "alt"} -- the address the
    web host serves it at, and the manifest's words for an owner who cannot see it -- or None when the version's picture
    left the set (or it has none), and `image_note`, the line under it; nothing for a plan whose channels show no
    picture. One sentence names the first such channel (NewsBreak, the only one in M1)."""
    channels = image_channels(sections)
    if not channels:
        return {}
    label = registry.label(channels[0])
    shown = picture(sections)
    if shown is None:
        return {"image": None, "image_note": _missing_line(sections, label)}
    url = f"{config.load().web_base_url}/static/creative/{quote(shown.file)}"
    return {"image": {"url": url, "alt": shown.alt}, "image_note": IMAGE_SHOWN.format(label=label)}


def image_step(sections: dict) -> str | None:
    """The "Ready to launch" card's line about the picture for a plan with a channel that shows one (spec 2026-09-29
    §4 item 2: "the image approved with the plan"); None for a plan whose channels show no picture."""
    channels = image_channels(sections)
    if not channels:
        return None
    label = registry.label(channels[0])
    return IMAGE_STEP.format(label=label) if picture(sections) is not None else _missing_line(sections, label)


def approved_creative(sections: dict) -> dict | None:
    """The plan's `creative` section as approving it stores it (P7, `proposals.apply_approve_plan`): `approved_image`
    {channel: the manifest file} for every funded channel that shows a picture -- exactly the version's own, the one
    its card shows (`picture`; M1 Task 7 fix round 1, item 4) -- or none when that picture left the set (or it has
    none): another is never approved in its place, and the channel waits in `waiting_creative` (`ads_run.image_ready`);
    None for a plan whose channels show no picture (nothing to store)."""
    channels = image_channels(sections)
    if not channels:
        return None
    shown = picture(sections)
    rest = {k: v for k, v in (sections.get("creative") or {}).items() if k != "approved_image"}
    return {**rest, "approved_image": dict.fromkeys(channels, shown.file)} if shown else rest


def cards(plan: dict, *, final_host: str | None = None, website: dict | None = None) -> dict:
    """The shape recommend/adjust return and the plan page renders: plan.goal (business_name, when the
    caller set plan["business_name"], leads the sentence) with one row per funded channel and the later lines,
    budget with its levels and the split at each, the two-column economics, the rationale, and ads_preview (spec
    §7): the primary's ad text exactly as it will run, so the owner sees it before approving -- and, for a plan with a
    channel that shows a picture with the ad (NewsBreak), that picture and its line (`_picture_preview`, P7).
    `final_host` is the landing page's host when the caller has one to give (`landing.final_host(conn, biz)`), and
    `website` the plan's "Your website" row (`site_install.row(conn, biz)`, A3p-5 batch 2 Q5), which goes in
    `setup["website"]` -- None for a business with no website of its own, or a caller with none to give. This function
    stays pure, so it never looks either up itself."""
    # deferred: landing and the Google build module both import this module at load time (breaks the cycle)
    from . import landing
    from .channels.google_search import build as ads_launch

    s = plan["sections"]
    c, est, ch = s["consultation"], s["estimate"], primary(s)
    name = plan.get("business_name") or "your business"
    radius = (
        f"within {c['geo']['radius_miles']:g} miles of {name}" if c["geo"].get("radius_miles") else f"around {name}"
    )
    goal = f"More {c['business']['service'] or ''} {OBJECTIVE_WORDS[c['objective']]} {radius}".replace("  ", " ")
    monthly = int(total_usd(s))
    tree = c["business"].get("category") or "fallback"
    kept_h, kept_d, notes = ads_launch.lint_copy(
        list(ch.get("headlines") or []), list(ch.get("descriptions") or []), landing.load_recipe(tree)["banned"]
    )
    return {
        "plan": {
            "goal": goal,
            "channels": channel_rows(s),
            "later": later_rows(s),
            "setup": {
                "area": radius.replace(f" of {name}", " of the business"),
                "goal": OBJECTIVE_WORDS[c["objective"]],
                "offer": c.get("offer"),
                "counts_as_result": c["conversions"],
                "runs": f"{c['duration_days']} days, checked every day",
                "landing_page": ch.get("landing_service"),
                "website": website,
            },
        },
        "budget": {
            "monthly_usd": monthly,
            "per_day": monthly // DAYS,
            "level": c["level"],
            "levels": level_rows(s),
        },
        "economics": {k: est[k] for k in ("columns", "assumptions", "footnote") if k in est},
        "rationale": s.get("rationale", {}),
        "ads_preview": {
            "headlines": kept_h[:3],
            "descriptions": kept_d[:2],
            "final_host": final_host,
            "dropped": notes,
            **_picture_preview(s),
        },
    }


def owner_lines(sections: dict) -> dict[str, str]:
    """The founder's lines (spec §4.1 table), keyed by section."""
    ads = sections["ads"]["channels"]
    est = sections["estimate"]
    if est.get("basis") == "none":
        # `reason`: estimate_columns' owner words for channels with no published cost per lead (add_estimate's
        # `note` is internal and never shown)
        estimate = f"Estimated monthly leads: not estimated yet ({est.get('reason') or 'no advertising budget'})"
    elif est["high"] < 5:  # the range is rounded to 5s: "0–0" would read as a promise of nothing
        estimate = "Estimated monthly leads: fewer than 5 (typical range for similar businesses, not a guarantee)"
    else:
        estimate = (
            f"Estimated monthly leads: {est['low']}–{est['high']} "
            "(typical range for similar businesses, not a guarantee)"
        )
    return {
        "presence": "Landing page: yours, or one Admiry builds for you",  # owner words (founder, 2026-10-13)
        "discovery": "Local SEO/GEO: $0",
        "ads": " · ".join(f"{registry.label(a['channel'])}: {usd(a['monthly_usd'])}/mo" for a in ads)
        or "Ads: none until a budget is set",
        "creative": "Ads and images: made by Admiry, you approve",
        "conversion": "Call, form and appointment tracking: connected",
        # no e-mail promise in a stored line (Dg plan P8): the report's promises live on the read-time surfaces
        "optimization": AUTOPILOT_LINE[sections["optimization"]["autopilot"]][:1].upper()
        + AUTOPILOT_LINE[sections["optimization"]["autopilot"]][1:],
        "estimate": estimate,
        "disclosure": sections["disclosure"]["text"],
    }


def _json(r) -> dict:
    return {
        "id": r["id"],
        "business_id": r["business_id"],
        "goal_id": r["goal_id"],
        "version": r["version"],
        "status": r["status"],
        "lines": r["json"]["lines"],
        "sections": r["json"]["sections"],
        "rationale": r["json"].get("rationale"),
        "notes": r["json"].get("notes", []),
        "execution": r["json"].get("execution"),
        "estimate_basis": r["estimate_basis"],
        "diff_from": r["diff_from"],
        "created_at": iso(r["created_at"]),
    }


# A plan version's status: a new version is a draft; the owner's yes to a business's first launch while
# AGENT_LAUNCH_REVIEW is on makes it in_review until Admiry passes it (agent/launch_review.py); an approved one runs;
# and each is superseded when a newer one takes its place (a new draft the draft or the version in review before it,
# an approval the version approved before it). Its writers -- create_version here, launch_review and the approval,
# proposals.apply_approve_plan -- write these; the public contract lists them.
DRAFT, IN_REVIEW, APPROVED, SUPERSEDED = "draft", "in_review", "approved", "superseded"
STATUSES = (DRAFT, IN_REVIEW, APPROVED, SUPERSEDED)


def create_version(
    conn: psycopg.Connection,
    biz_id: str,
    *,
    goal_id: str | None,
    sections: dict,
    estimate_basis: dict,
    rationale: str,
    notes: list[str],
    diff_from: str | None = None,
) -> dict:
    """Append a draft version; the previous draft, or the version in review, becomes superseded (its
    review withdrawn); approved plans stay."""
    conn.execute(
        "UPDATE plan SET status = %s WHERE business_id = %s AND status = ANY(%s)",
        (SUPERSEDED, biz_id, [DRAFT, IN_REVIEW]),
    )
    launch_review.withdraw_open(conn, biz_id)  # the version in review gave way to this one (L1, plan D7)
    plan_id = db.new_id("pl")
    version = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 AS v FROM plan WHERE business_id = %s", (biz_id,)
    ).fetchone()["v"]
    payload = {"sections": sections, "lines": owner_lines(sections), "rationale": rationale, "notes": notes}
    try:
        conn.execute(
            "INSERT INTO plan (id, business_id, goal_id, version, json, estimate_basis, diff_from, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (plan_id, biz_id, goal_id, version, Jsonb(payload), Jsonb(estimate_basis), diff_from, DRAFT),
        )
    except psycopg.errors.UniqueViolation as e:  # two concurrent versions computed the same number
        if e.diag.constraint_name != "plan_business_id_version_key":
            raise
        raise ToolError("another plan change for this business landed at the same moment; repeat the call") from e
    role_queue.after_version(conn, biz_id, plan_id)  # the Critic judges it in the background (capability layer K1)
    return get(conn, biz_id, plan_id)


def get(conn: psycopg.Connection, biz_id: str, plan_id: str) -> dict | None:
    r = conn.execute(f"SELECT {PLAN_COLS} FROM plan WHERE id = %s AND business_id = %s", (plan_id, biz_id)).fetchone()
    return _json(r) if r else None


def latest(conn: psycopg.Connection, biz_id: str) -> dict | None:
    r = conn.execute(
        f"SELECT {PLAN_COLS} FROM plan WHERE business_id = %s ORDER BY version DESC LIMIT 1", (biz_id,)
    ).fetchone()
    return _json(r) if r else None


def by_id(conn: psycopg.Connection, plan_id: str) -> dict | None:
    """For the /plan/{id} page: the id is the capability (opaque 64-bit token)."""
    r = conn.execute(f"SELECT {PLAN_COLS} FROM plan WHERE id = %s", (plan_id,)).fetchone()
    return _json(r) if r else None


def summary(plan: dict) -> dict:
    """The compact shape tools return: the lines in the founder's order plus ids (details on /plan/{id})."""
    return {
        "plan_id": plan["id"],
        "version": plan["version"],
        "status": plan["status"],
        "lines": [plan["lines"][k] for k in SECTION_ORDER],
        "rationale": plan["rationale"],
        "notes": plan["notes"],
        "execution": plan["execution"],
    }
