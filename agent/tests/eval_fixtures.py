"""K5's test helpers: a plan as `agent.eval freeze` stores it (built by hand from the dental draft the S0 tests use, with
the estimate, the levels and the rationale `recommend` adds), and a small golden set in a temporary folder."""

from __future__ import annotations

from pathlib import Path

from agent import plans
from agent.eval import cases
from agent.tests.fixtures import knowledge_fixture
from agent.tests.test_review_launch import draft_sections

KNOWLEDGE = knowledge_fixture().model_dump()
ADMIN = "founder@example.com"


def _column() -> dict:
    """One economics column at $1,000 a month (a new dict each time: a flaw replaces its lists)."""
    return {"spend": 1000, "leads": [10, 20], "bookings": [5, 10], "customers": [3, 6], "revenue": [7500, 15000]}


def frozen_plan(**kw) -> dict:
    """A frozen consultation plan for Bright Smile Dental's Invisalign ads: `kw` changes the draft (monthly_usd, themes,
    headlines, descriptions, negatives, radius_miles)."""
    s = draft_sections(**kw)
    s["estimate"] = {
        "basis": "benchmark",
        "confidence": "medium",
        "benchmark_rows": ["dentists_google_search"],
        "low": 10,
        "high": 20,
        "per_channel": {"google_search": [10.2, 19.6]},
        "columns": {"planning": _column(), "recommended": _column()},
        "assumptions": [
            {
                "name": "cost per lead",
                "value": "$51–109",
                "basis": "benchmark dentists_google_search US, confidence medium",
            },
            {"name": "lead to booking", "value": "50%", "basis": "benchmark dentists, confidence medium"},
            {"name": "booking to customer", "value": "55%", "basis": "benchmark dentists, confidence medium"},
            {"name": "customer value", "value": "$2,500", "basis": "your answer"},
        ],
        "footnote": plans.FOOTNOTE.format(noun="dental practices"),
    }
    s["levels"] = {"minimum": 300, "conservative": 500, "recommended": 1000, "aggressive": 1700}
    s["rationale"] = {
        "why_primary": "Your customer shows up with strong intent: they type what they want, close to the moment they "
        "decide. Google Search meets them there.",
        "why_not_later": "TikTok is good at making people nearby aware of you, but the goal today is bookings.",
        "example_searches": ["invisalign palo alto", "clear aligners palo alto"],
    }
    return {
        "sections": s,
        "lines": plans.owner_lines(s),
        "rationale": s["rationale"]["why_primary"],
        "notes": [],
        "estimate_basis": {"basis": "benchmark", "confidence": "medium", "benchmark_rows": ["dentists_google_search"]},
        "model": "fake",
        "effort": "low",
        "commit": "test",
        "built_on": "2026-10-09",
    }


def golden_set(
    tmp_path: Path, ids: tuple[str, ...] = ("dentists-1", "dentists-2"), budgets: tuple[float, ...] = (1000.0, 1200.0)
) -> tuple[Path, Path]:
    """A golden set of its own: the named cases copied from the real one, each with a frozen plan of a different
    budget (so each has its own digest), and an empty flawed folder."""
    cases_dir, flawed_dir = tmp_path / "cases", tmp_path / "flawed"
    cases_dir.mkdir()
    flawed_dir.mkdir()
    for cid, usd in zip(ids, budgets, strict=True):
        data = cases.raw(cases.CASES_DIR / f"{cid}.json")
        data["plan"] = frozen_plan(monthly_usd=usd)
        cases.write(cases_dir / f"{cid}.json", data)
    return cases_dir, flawed_dir
