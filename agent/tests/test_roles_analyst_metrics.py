"""What code computes for the Analyst (capability layer K2, spec 2026-10-07 §5.1 step 2; plan R1, R14): the day's
figures, the 7- and 30-day windows, the benchmark and the pace, the flags, the candidate actions -- each a public tool
with its arguments -- the previous look, and the material the model reads (aggregates only, no tool named)."""

import json
from datetime import date

import pytest
from psycopg.types.json import Jsonb

from agent import benchmark, copy, plans
from agent.digest_audience import Audience, ChannelClass
from agent.digest_rules import Item
from agent.roles import analyst
from agent.surface import gate, load
from agent.tests import digest_fixtures as df

CASES = [
    ("leads_unmarked", "leads_unmarked", None, ["mark_leads"]),
    ("funds_low", "funds_low", None, ["add_funds"]),
    ("limit_near", "limit:google_search", None, ["raise_amount:google_search"]),
    ("no_leads_spend", "no_leads:google_search", None, ["change_ads:google_search", "pause:google_search"]),
    ("tracking_silent", "tracking:google_search", None, ["check_page"]),
    ("channel_issue", "ads:google_search", None, ["see_issue:google_search"]),
    ("channel_issue", "issue:7", None, ["see_issue"]),
    ("waiting_account", "waiting:newsbreak", None, ["finish_setup:newsbreak"]),
    ("proposal_pending", "proposal:pr_1", None, ["review_change"]),
    ("underdelivery", "underdelivery:google_search", None, []),
    ("safe_action_taken", "action:5", None, []),
    ("paused_by_system", "pause:google_search", "no_funds", ["add_funds"]),
    ("paused_by_system", "pause:google_search", "month_cap", ["raise_amount:google_search"]),
    ("paused_by_system", "pause:google_search", "no_leads", ["change_ads:google_search"]),
    ("paused_by_system", "pause:google_search", "page_down", ["check_page"]),
    ("paused_by_system", "pause:google_search", "dispute", []),
]


def _item(rule: str, ref: str) -> Item:
    return Item(rule=rule, severity="medium", text="x", link=None, ref=ref)


def _aud(reason: str | None = None) -> Audience:
    row = ChannelClass(
        channel="google_search",
        label="Google Search",
        cls="active",
        state="paused",
        paused_reason=reason,
        spent_on_day=False,
        row={},
    )
    return Audience(edition="daily", channels=[row])


def _place(pct, confidence) -> dict:
    return {
        "channel": "google_search",
        "label": "Google Search",
        "cost_per_lead_vs_benchmark_pct": pct,
        "last_30_days": {"confidence": confidence},
    }


@pytest.mark.parametrize(("rule", "ref", "reason", "ids"), CASES)
def test_candidates_follow_the_flags(rule, ref, reason, ids):
    assert [c.id for c in analyst.candidates([_item(rule, ref)], [], _aud(reason), plan_id="pl_1")] == ids


@pytest.mark.parametrize(
    ("pct", "confidence", "ids"),
    [(160, "solid", ["change_ads:google_search"]), (160, "early", []), (140, "solid", []), (None, "solid", [])],
)
def test_a_costly_place_on_a_solid_sample_gets_a_change(pct, confidence, ids):
    assert [c.id for c in analyst.candidates([], [_place(pct, confidence)], _aud(), plan_id="pl_1")] == ids


def test_a_candidate_is_offered_once():
    items = [_item("no_leads_spend", "no_leads:google_search"), _item("paused_by_system", "pause:google_search")]
    ids = [c.id for c in analyst.candidates(items, [_place(160, "solid")], _aud("no_leads"), plan_id="pl_1")]
    assert ids == ["change_ads:google_search", "pause:google_search"]


def test_a_change_names_the_approved_plan():
    """Final review I6: update_marketing_plan routes a message with no plan_id to a consultation's waiting question, so
    a change always names the plan it is for -- and with no approved plan there is none to offer."""
    [change] = analyst.candidates([], [_place(160, "solid")], _aud(), plan_id="pl_9")
    assert change.tool == "update_marketing_plan" and change.args["plan_id"] == "pl_9"
    assert analyst.candidates([], [_place(160, "solid")], _aud(), plan_id=None) == []


def test_the_actions_carry_their_arguments():
    found = {c.id: c for c in analyst.candidates([_item(r, ref) for r, ref, _, _ in CASES], [], _aud(), plan_id="pl_1")}
    assert (found["mark_leads"].tool, found["mark_leads"].args) == ("get_leads", {"awaiting_outcome": True})
    assert (found["add_funds"].tool, found["add_funds"].args) == ("get_billing", {"view": "topup_link"})
    assert found["raise_amount:google_search"].args == {"channel": "google_search"}
    assert found["pause:google_search"].args == {"channel": "google_search"}
    assert found["check_page"].args == {"view": "check"}
    assert found["finish_setup:newsbreak"].args == {"channel": "newsbreak"}
    assert set(found["change_ads:google_search"].args) == {"message", "plan_id"}


def test_every_candidate_tool_is_a_public_tool():
    load()
    assert set(analyst.KINDS.values()) <= gate.PUBLIC


def test_candidate_words_pass_owner_words():
    every = analyst.candidates([_item(r, ref) for r, ref, _, _ in CASES], [_place(160, "solid")], _aud())
    for c in every:
        assert copy.jargon(c.label) is None, c.label
        assert copy.jargon(c.args.get("message", "")) is None, c.args


def test_the_figures_of_the_day_the_windows_and_the_benchmark(conn, analyst_biz):
    m = df.analyst_metrics(conn)
    g = m["channels"][0]
    assert g["label"] == "Google Search"
    assert g["yesterday"] == {
        "spend": "$23.10",
        "clicks": 41,
        "calls": 2,
        "forms": 1,
        "leads": 1,
        "daily_budget_used_pct": 117,
    }
    bench = benchmark.lookup(conn, "fallback", "google_search")
    assert g["benchmark_cost_per_lead"] == plans.usd(bench["cpl_avg"])
    assert g["month"] == {
        "spent": "$23.10",
        "amount": "$600",
        "pace_pct": analyst._pace_pct(2310, 60000, df.ANALYST_DAY),
    }
    assert g["last_30_days"]["spend"] == "$23.10" and g["last_30_days"]["leads"] == 1
    assert m["day"] == "2026-10-05" and m["weekly"] is False and m["plan_id"] and m["previous"] is None


def test_the_previous_look_is_material(conn, analyst_biz):
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status, insight) VALUES ('in_p', 'biz_1', '2026-10-04', 'on', "
        "'done', %s)",
        (Jsonb({"summary": "A slow Sunday.", "recommendations": [{"label": "Say what the leads became"}]}),),
    )
    m = df.analyst_metrics(conn)
    assert m["previous"] == {
        "day": "Sunday, October 4",
        "summary": "A slow Sunday.",
        "suggested": ["Say what the leads became"],
    }


def test_the_pace_is_spend_against_an_even_month():
    assert analyst._pace_pct(30000, 60000, date(2026, 9, 15)) == 100
    assert analyst._pace_pct(15000, 60000, date(2026, 9, 15)) == 50
    assert analyst._pace_pct(100, None, date(2026, 9, 15)) is None


def test_the_material_names_no_tool_and_holds_no_contact(conn, analyst_biz):
    mat = analyst.material(df.analyst_metrics(conn), business_name="Bright Smile Dental")
    text = json.dumps(mat)
    assert mat["day"] == "Monday, October 5" and mat["edition"] == "daily"
    assert not any(tool in text for tool in analyst.KINDS.values())
    assert "@" not in text and "2026-" not in text and "google_search" not in text
    assert all(set(c) == {"id", "label"} for c in mat["candidates"])
