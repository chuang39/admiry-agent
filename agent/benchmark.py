"""Benchmark cost-per-lead rows (spec Appendix C) and the lead estimate (spec §4.1).

Every plan's "Estimated monthly leads" comes from here, never from the LLM:
  per channel   low  = budget ÷ (1.5 × cpl_avg)      high = budget ÷ (0.7 × cpl_avg)
  overall       sums over channels, each end rounded to the nearest 5
An unknown category falls back to the cross-industry rows. The result always carries its
basis, confidence and the benchmark row ids, so the plan can print where the number came
from. Seed rows are published US averages (confidence "low"); Keyword Planner data
replaces them per metro once Google Ads Basic access exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import psycopg

LOCALIQ_SEARCH = "https://localiq.com/blog/search-advertising-benchmarks/"
LOCALIQ_META = "https://localiq.com/blog/facebook-advertising-benchmarks/"
WORDSTREAM_META = "https://www.wordstream.com/blog/facebook-ads-benchmarks-2025"
MEDIACAPTAIN_LSA = "https://www.themediacaptain.com/google-local-service-ad-statistics/"

CATEGORIES = (
    "home_services",
    "attorneys",
    "finance_insurance",
    "real_estate",
    "dentists",
    "physicians",
    "health_fitness",
    "automotive_repair",
    "animals_pets",
    "personal_services",
    "fallback",
)


LOW_FACTOR, HIGH_FACTOR = 0.7, 1.5
# The funnel of a category's Google Search row: (lead to booking, booking to customer). funnel() reads it; every other
# channel's row falls back to its category's rate (lookup).
FUNNEL: dict[str, tuple[float, float]] = {
    "dentists": (0.50, 0.55),
    "physicians": (0.50, 0.55),
    "home_services": (0.60, 0.60),
    "attorneys": (0.40, 0.50),
    "personal_services": (0.50, 0.50),
    "fallback": (0.50, 0.50),
}
# A channel's own lead-to-booking rate where the category's Search row is not the answer (spec 2026-09-29 §6.1,
# Appendix B): Meta 8-18 % in Watson & Co's agency data, seeded at 0.13 for every category.
BOOKING_RATE = {"meta": 0.13}
BING_SOURCE = "no separate benchmark: seeded equal to the category's google_search row"
NEXTDOOR_CASE = (
    "https://blog.nextdoor.com/how-nextdoor-helped-visiting-angels-cut-cost-per-lead-by-64-and-build-a-better-campaign-"
    "in-the-process"
)


def _row(
    category: str,
    channel: str,
    cpl_avg: float,
    source_url: str,
    as_of: int,
    *,
    low: float | None = None,
    high: float | None = None,
    lead_to_booking: float | None = None,
) -> dict:
    """A seed row; `low`/`high` default to cpl_avg x LOW_FACTOR / HIGH_FACTOR; a category's Google Search row carries
    its FUNNEL rates, any other row BOOKING_RATE's (or the one given), so seed() writes each row whole in one statement."""
    funnel_rates = FUNNEL.get(category) if channel == "google_search" else None
    default_rate = funnel_rates[0] if funnel_rates else BOOKING_RATE.get(channel)
    return {
        "id": f"{category}:{channel}",
        "category": category,
        "channel": channel,
        "cpl_avg": cpl_avg,
        "cpl_low": round(cpl_avg * LOW_FACTOR, 2) if low is None else low,
        "cpl_high": round(cpl_avg * HIGH_FACTOR, 2) if high is None else high,
        "lead_to_booking": default_rate if lead_to_booking is None else lead_to_booking,
        "booking_to_customer": funnel_rates[1] if funnel_rates else None,
        "source_url": source_url,
        "as_of": as_of,
    }


SEED: list[dict] = [
    _row("home_services", "google_search", 90.92, LOCALIQ_SEARCH, 2026),
    _row("home_services", "meta", 42.95, LOCALIQ_META, 2026),
    _row("home_services", "lsa", 69.00, MEDIACAPTAIN_LSA, 2025),  # plumbing; per-trade rows below
    _row("home_services_plumbing", "lsa", 69.00, MEDIACAPTAIN_LSA, 2025),
    _row("home_services_hvac", "lsa", 80.00, MEDIACAPTAIN_LSA, 2025),
    _row("home_services_roofing", "lsa", 162.00, MEDIACAPTAIN_LSA, 2025),
    _row("home_services_painter", "lsa", 40.00, MEDIACAPTAIN_LSA, 2025),
    _row("home_services_landscaper", "lsa", 39.00, MEDIACAPTAIN_LSA, 2025),
    _row("home_services_locksmith_handyman", "lsa", 34.00, MEDIACAPTAIN_LSA, 2025),
    _row("attorneys", "google_search", 131.63, LOCALIQ_SEARCH, 2026),
    _row("attorneys", "meta", 72.40, WORDSTREAM_META, 2025),
    _row("attorneys_estate", "lsa", 45.00, MEDIACAPTAIN_LSA, 2025),
    _row("attorneys_personal_injury", "lsa", 249.00, MEDIACAPTAIN_LSA, 2025),
    _row("finance_insurance", "google_search", 74.44, LOCALIQ_SEARCH, 2026),
    _row("finance_insurance", "meta", 58.70, WORDSTREAM_META, 2025),
    _row("real_estate", "google_search", 102.51, LOCALIQ_SEARCH, 2026),
    _row("real_estate", "meta", 13.74, LOCALIQ_META, 2026),
    _row("dentists", "google_search", 72.97, LOCALIQ_SEARCH, 2026),
    _row("dentists", "meta", 61.56, LOCALIQ_META, 2026),
    _row("physicians", "google_search", 40.04, LOCALIQ_SEARCH, 2026),
    _row("physicians", "meta", 32.14, LOCALIQ_META, 2026),
    _row("health_fitness", "google_search", 67.36, LOCALIQ_SEARCH, 2026),
    _row("health_fitness", "meta", 27.11, LOCALIQ_META, 2026),
    _row("automotive_repair", "google_search", 29.96, LOCALIQ_SEARCH, 2026),
    _row("animals_pets", "google_search", 31.50, LOCALIQ_SEARCH, 2026),
    _row("personal_services", "google_search", 54.60, LOCALIQ_SEARCH, 2026),
    _row("personal_services", "meta", 38.09, LOCALIQ_META, 2026),
    _row("fallback", "google_search", 66.69, LOCALIQ_SEARCH, 2026),
    _row("fallback", "meta", 27.66, WORDSTREAM_META, 2025),
    _row("fallback", "lsa", 60.00, MEDIACAPTAIN_LSA, 2025),
]
# Sub-category rows (home_services_plumbing, attorneys_estate, …) refine a category for one
# channel; CATEGORIES lists the keys the Business Agent maps a business to.


def _as_search(category: str) -> dict:
    """Bing's row for a category (Appendix B): no separate benchmark, so the category's Google Search row -- the same
    costs per lead and booking rate, and a source that says so."""
    search = next(r for r in SEED if r["id"] == f"{category}:google_search")
    return _row(
        category,
        "microsoft_search",
        search["cpl_avg"],
        BING_SOURCE,
        search["as_of"],
        low=search["cpl_low"],
        high=search["cpl_high"],
        lead_to_booking=search["lead_to_booking"],
    )


# The channels of spec 2026-09-29-admiry-channels-design (Appendix B): Bing for the four categories it names,
# Nextdoor's own case study for home services. NewsBreak, Reddit and ChatGPT have no published cost per lead for local
# services: no row (the columns are NOT NULL; plan P8), so the allocator never proposes them by the ratio.
SEED += [_as_search(c) for c in ("home_services", "dentists", "attorneys", "fallback")]
SEED.append(_row("home_services", "nextdoor", 50.00, NEXTDOOR_CASE, 2026, low=26.00, high=73.00, lead_to_booking=0.25))
RECOMMEND_LEADS = 30  # the recommended budget buys about this many leads a month at the average cost per lead
MIN_BUDGET = 500


def seed(conn: psycopg.Connection) -> int:
    """Upsert the seed rows, each whole in one statement -- rates included, so on an autocommit connection no reader
    ever sees a Search row without its funnel (M0 Task 5 fix round 1); returns how many rows the table holds for them."""
    for r in SEED:
        conn.execute(
            "INSERT INTO benchmark (id, category, geo, channel, cpl_low, cpl_high, cpl_avg, source_url, as_of, confidence, "
            "lead_to_booking, booking_to_customer) VALUES (%(id)s, %(category)s, 'US', %(channel)s, %(cpl_low)s, "
            "%(cpl_high)s, %(cpl_avg)s, %(source_url)s, %(as_of)s, 'low', %(lead_to_booking)s, %(booking_to_customer)s) "
            "ON CONFLICT (id) DO UPDATE SET cpl_low = EXCLUDED.cpl_low, cpl_high = EXCLUDED.cpl_high, "
            "cpl_avg = EXCLUDED.cpl_avg, source_url = EXCLUDED.source_url, as_of = EXCLUDED.as_of, "
            "lead_to_booking = EXCLUDED.lead_to_booking, booking_to_customer = EXCLUDED.booking_to_customer",
            r,
        )
    return len(SEED)


@dataclass
class Estimate:
    low: int
    high: int
    basis: str  # "benchmark" (part 2 adds "own_history")
    confidence: str
    rows: list[dict]  # the benchmark rows used (id, category, channel, cpl_avg, source_url)
    per_channel: dict[str, tuple[float, float]] = field(default_factory=dict)

    def owner_line(self) -> str:
        return (
            f"Estimated monthly leads: {self.low}–{self.high} (typical range for similar businesses, not a guarantee)"
        )

    def as_json(self) -> dict:
        return {
            "low": self.low,
            "high": self.high,
            "basis": self.basis,
            "confidence": self.confidence,
            "benchmark_rows": [r["id"] for r in self.rows],
            "per_channel": {c: list(v) for c, v in self.per_channel.items()},
        }


def _round5(x: float) -> int:
    return int(5 * round(x / 5))


def lookup(conn: psycopg.Connection, category: str, channel: str) -> dict | None:
    """The channel's row for the category, else the fallback category's; None when neither has one. Its
    `lead_to_booking` is the row's own rate, else the category's Google Search rate (`funnel`, spec 2026-09-29 §6.1)."""
    for cat in (category, "fallback"):
        row = conn.execute(
            "SELECT id, category, channel, cpl_avg, source_url, confidence, lead_to_booking FROM benchmark "
            "WHERE category = %s AND channel = %s AND geo = 'US'",
            (cat, channel),
        ).fetchone()
        if row:
            rate = row["lead_to_booking"]
            return {
                **row,
                "cpl_avg": float(row["cpl_avg"]),
                "lead_to_booking": float(rate) if rate is not None else funnel(conn, category)[0],
            }
    return None


class NotSeeded(LookupError):
    """The rows an estimate or a recommended budget needs are not in the table -- a box before `seed()` ran. The plan
    tools turn this, and only this, into their "try again" sentence (final review I5): any other error is a bug."""


def estimate(conn: psycopg.Connection, category: str, budgets: dict[str, float]) -> Estimate:
    """Monthly lead range for the given per-channel monthly budgets (USD)."""
    if not any(v and v > 0 for v in budgets.values()):
        raise ValueError("at least one channel needs a positive monthly budget")
    if conn.execute("SELECT count(*) AS c FROM benchmark").fetchone()["c"] == 0:
        raise NotSeeded("benchmark table is empty; run benchmark.seed()")
    rows: list[dict] = []
    per_channel: dict[str, tuple[float, float]] = {}
    low = high = 0.0
    for channel, budget in budgets.items():
        if not budget or budget <= 0:
            continue
        row = lookup(conn, category, channel)
        if row is None:
            continue  # channel we have no benchmark for (e.g. tiktok)
        lo, hi = budget / (HIGH_FACTOR * row["cpl_avg"]), budget / (LOW_FACTOR * row["cpl_avg"])
        per_channel[channel] = (round(lo, 2), round(hi, 2))
        low, high = low + lo, high + hi
        rows.append(row)
    if not rows:
        raise ValueError("no benchmark row matches any funded channel")
    confidence = "low" if any(r["confidence"] == "low" for r in rows) else rows[0]["confidence"]
    return Estimate(
        low=_round5(low),
        high=_round5(high),
        basis="benchmark",
        confidence=confidence,
        rows=rows,
        per_channel=per_channel,
    )


def funnel(conn: psycopg.Connection, category: str) -> tuple[float, float, str]:
    for cat in (category, "fallback"):
        row = conn.execute(
            "SELECT lead_to_booking, booking_to_customer, confidence FROM benchmark "
            "WHERE category = %s AND channel = 'google_search' AND lead_to_booking IS NOT NULL",
            (cat,),
        ).fetchone()
        if row:
            return (
                float(row["lead_to_booking"]),
                float(row["booking_to_customer"]),
                f"benchmark {cat}, confidence {row['confidence']}",
            )
    raise NotSeeded("benchmark table has no funnel rows; run benchmark.seed()")


def recommend_budget(conn: psycopg.Connection, category: str, stated_limit: float | None) -> int:
    if stated_limit and stated_limit > 0:
        return int(stated_limit)
    row = lookup(conn, category, "google_search")
    if row is None:
        raise NotSeeded("no google_search benchmark row; run benchmark.seed()")
    return max(MIN_BUDGET, 100 * math.floor(RECOMMEND_LEADS * row["cpl_avg"] / 100 + 0.5))
