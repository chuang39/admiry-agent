"""Two real Meta Model API calls (extract, then plan) — run only with AGENT_LLM_LIVE=1 and META_MODEL_API_KEY set;
cost unmeasured, check the usage page."""

import os

import pytest

from agent import llm

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_LLM_LIVE") != "1", reason="set AGENT_LLM_LIVE=1 to call the API")


def test_extract_and_plan_live():
    gen = llm.generator()
    k = gen.extract_knowledge(
        "Family dentist in Palo Alto, Invisalign and cleanings; I want 20 new patients a month, $2,000/mo",
        pages=[],
        listing=None,
        locale="en-US",
    )
    assert k.category == "dentists" and k.services and k.goal_hint.monthly_budget_usd == 2000
    d = gen.draft_plan(
        k.model_dump(),
        {"goal_type": "customers", "target": 20, "monthly_limit_usd": 2000, "focus_services": [], "autopilot": "safe"},
    )
    assert d.ads and sum(a.monthly_usd for a in d.ads) <= 2000.01
    assert all(len(h) <= 30 for a in d.ads for h in a.headlines)
