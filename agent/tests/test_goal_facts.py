"""The goal's most-per-customer and the two facts an assistant states up front (spec 2026-10-01 §4.3, §5, D162; plan
G1 Task 3)."""

from datetime import datetime, timedelta, timezone

import pytest

from agent import benchmark, business, consult, crawl, db, llm, models, places, tools_start
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setattr(tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="x")])
    # start's lookup of the business (plan 2026-10-03-agent-listing-confirm: places.search, after the reading)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [places.Candidate("ChIJ1", "Bright Smile", "A")])
    return gen


def test_a_goal_keeps_its_most_per_customer_until_another_is_given(conn):
    business_with_key(conn)
    first = business.set_goal(conn, "biz_1", target=30, monthly_limit_usd=2000, max_cost_per_customer_usd=80)
    assert first["max_cost_per_customer_usd"] == 80.0
    kept = business.set_goal(conn, "biz_1", target=25, monthly_limit_usd=2000)  # written for another reason
    assert kept["max_cost_per_customer_usd"] == 80.0
    cleared = business.set_goal(conn, "biz_1", target=25, monthly_limit_usd=2000, max_cost_per_customer_usd=None)
    assert cleared["max_cost_per_customer_usd"] is None
    assert business.latest_goal(conn, "biz_1")["max_cost_per_customer_usd"] is None


def test_two_goals_written_in_one_transaction_come_back_in_the_order_they_were_written(conn, monkeypatch):
    """A goal is dated when it is written, not from the start of its transaction: two written in one transaction do not
    share a date (and fall to the order of their random ids), so the latest goal is the last one written."""
    business_with_key(conn)
    ids = iter(["goal_9", "goal_1"])  # the first written has the greater id, the one a shared date would pick
    monkeypatch.setattr(db, "new_id", lambda prefix: next(ids))
    with conn.transaction():
        first = business.set_goal(conn, "biz_1", target=10)
        second = business.set_goal(conn, "biz_1", target=20)
    assert datetime.fromisoformat(first["created_at"]) < datetime.fromisoformat(second["created_at"])
    assert business.latest_goal(conn, "biz_1")["id"] == second["id"]


def test_a_goal_written_for_another_reason_keeps_the_goal_type_and_autopilot(conn):
    business_with_key(conn)
    business.set_goal(conn, "biz_1", goal_type="calls", target=30, autopilot="off")
    kept = business.set_goal(conn, "biz_1", target=25)  # written for another reason: it names neither
    assert (kept["goal_type"], kept["autopilot"]) == ("calls", "off")
    named = business.set_goal(conn, "biz_1", goal_type="bookings", autopilot="safe")  # naming them sets them
    assert (named["goal_type"], named["autopilot"]) == ("bookings", "safe")
    assert business.latest_goal(conn, "biz_1")["id"] == named["id"]


def test_a_business_with_no_goal_gets_the_default_goal_type_and_autopilot(conn):
    business_with_key(conn)
    first = business.set_goal(conn, "biz_1", target=25)
    assert (first["goal_type"], first["autopilot"]) == ("customers", "safe")


def test_known_facts_take_the_two_numbers_and_drop_what_is_not_one():
    cleaned = consult.clean_known_facts(
        {"target_customers_per_month": "30", "max_cost_per_customer_usd": 80, "radius_miles": True, "offer": "  "}
    )
    assert cleaned == {"target_customers_per_month": 30.0, "max_cost_per_customer_usd": 80.0}


def test_known_facts_take_a_goal_type_and_drop_any_other_value():
    """What the goal counts (G1 Task 10, fix round 1): customers, calls or bookings; anything else is dropped like any
    fact that cannot be used, never refused."""
    assert consult.clean_known_facts({"goal_type": "calls"}) == {"goal_type": "calls"}
    for odd in ("fame", "Calls", ["calls"], 3, None):
        assert consult.clean_known_facts({"goal_type": odd}) == {}, odd


def test_start_writes_a_stated_goal_type_onto_the_goal_and_drops_one_that_is_not_a_type(client, conn, fake):
    key = business_with_key(conn, claim_state="claimed")
    odd = call_tool(client, "start", {"text": "dentist", "known_facts": {"goal_type": "fame"}}, key=key)
    assert not odd["isError"], tool_text(odd)  # dropped like any fact that cannot be used, not an error
    assert "goal_type" not in consult.open_for(conn, "biz_1")["known_facts"]
    assert business.latest_goal(conn, "biz_1")["goal_type"] == "customers"  # the words' goal, of the default type
    calls = call_tool(client, "start", {"text": "dentist", "known_facts": {"goal_type": "calls"}}, key=key)
    assert not calls["isError"], tool_text(calls)
    assert business.latest_goal(conn, "biz_1")["goal_type"] == "calls"


def test_a_stated_goal_type_alone_is_reason_enough_to_write_the_goal(client, conn, fake):
    nothing = models.GoalHint(target_customers_per_month=None, monthly_budget_usd=None, focus_services=[])
    fake.knowledge = knowledge_fixture().model_copy(update={"goal_hint": nothing})  # the words state no goal
    key = business_with_key(conn, claim_state="claimed")
    unstated = call_tool(client, "start", {"text": "dentist"}, key=key)
    assert not unstated["isError"] and business.latest_goal(conn, "biz_1") is None  # nothing stated, nothing written
    res = call_tool(client, "start", {"text": "dentist", "known_facts": {"goal_type": "bookings"}}, key=key)
    assert not res["isError"], tool_text(res)
    goal = business.latest_goal(conn, "biz_1")
    assert goal is not None
    assert (goal["goal_type"], goal["target"], goal["monthly_limit_usd"]) == ("bookings", None, None)


def test_start_writes_the_stated_target_and_ceiling_onto_the_goal(client, conn, fake):
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(
        client,
        "start",
        {
            "text": "dentist in Palo Alto, more Invisalign patients",
            "known_facts": {
                "target_customers_per_month": 30,
                "max_cost_per_customer_usd": 80,
                "monthly_budget_usd": 2000,
            },
        },
        key=key,
    )
    assert not res["isError"], tool_text(res)
    goal = business.latest_goal(conn, "biz_1")
    assert (goal["target"], goal["monthly_limit_usd"], goal["max_cost_per_customer_usd"]) == (30, 2000.0, 80.0)


def test_a_stated_target_wins_over_the_one_read_from_the_words(client, conn, fake):
    benchmark.seed(conn)  # knowledge_fixture's goal_hint says 20 customers a month
    key = business_with_key(conn, claim_state="claimed")
    call_tool(client, "start", {"text": "dentist", "known_facts": {"target_customers_per_month": 45}}, key=key)
    assert business.latest_goal(conn, "biz_1")["target"] == 45


def test_a_stated_number_that_is_not_finite_is_no_fact_not_a_crash(client, conn, fake):
    benchmark.seed(conn)  # knowledge_fixture's goal_hint says 20 customers a month
    key = business_with_key(conn, claim_state="claimed")
    facts = {"target_customers_per_month": "inf", "max_cost_per_customer_usd": "1e999"}
    res = call_tool(client, "start", {"text": "dentist", "known_facts": facts}, key=key)
    assert not res["isError"], tool_text(res)
    goal = business.latest_goal(conn, "biz_1")
    assert (goal["target"], goal["max_cost_per_customer_usd"]) == (20, None)


def test_set_goal_takes_the_ceiling_and_refuses_one_out_of_range(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    ok = call_tool(
        client,
        "set_goal",
        {"monthly_budget_usd": 2000, "max_cost_per_customer_usd": 80, "idempotency_key": "g-1"},
        key=key,
    )
    assert not ok["isError"] and data(ok)["goal"]["max_cost_per_customer_usd"] == 80.0
    bad = call_tool(
        client,
        "set_goal",
        {"monthly_budget_usd": 2000, "max_cost_per_customer_usd": -1, "idempotency_key": "g-2"},
        key=key,
    )
    assert bad["isError"] and "max_cost_per_customer_usd must be between 1 and 100,000" in tool_text(bad)


@pytest.mark.parametrize(("draft_states", "lands"), [(80, 80.0), (None, 50.0)], ids=["stated", "not_stated"])
def test_a_hand_off_keeps_the_destinations_most_per_customer_unless_the_draft_states_one(conn, draft_states, lands):
    business_with_key(conn, biz_id="biz_draft")
    business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    business.set_goal(conn, "biz_draft", target=30, monthly_limit_usd=2000, max_cost_per_customer_usd=draft_states)
    business.set_goal(conn, "biz_owner", target=10, monthly_limit_usd=500, max_cost_per_customer_usd=50)  # its own
    tree = consult.load_tree("fallback")
    session = consult.create(
        conn,
        "biz_draft",
        tree="fallback",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, {}),
        answers={},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    moved = consult.move_session(conn, session, to_business="biz_owner")
    assert moved["business_id"] == "biz_owner"
    assert business.latest_goal(conn, "biz_owner")["max_cost_per_customer_usd"] == lands
