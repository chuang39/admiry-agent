"""The conversation and the plan through the public surface (spec 2026-10-01 §4.2-§4.4; plan G1 Task 10): one tool
begins, one carries every turn, one shows or builds the plan, one launches it with the owner's code, one answers."""

import json
import re

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import (
    ads_mode,
    audit,
    auth,
    benchmark,
    business,
    consult,
    envelope,
    google_ads_link,
    keys,
    launch_check,
    models,
    plans,
    proposals,
    site_install,
    tools_consult,
    tools_state,
)
from agent.surface import plan as surface_plan
from agent.surface import registry
from agent.surface.where import where
from agent.tests.conftest import call_tool, data, rpc_raw, tool_text
from agent.tests.fixtures import business_with_key, plan_draft_fixture
from agent.tests.surface_helpers import WALK_TEXT, planned, settle_page, started
from agent.tests.test_tools_hosting import GOOD_HTML

# The walk's open questions, answered: a consultation with nothing left to ask (the business on Google Maps first:
# picking it centres the ads, plan 2026-10-03-agent-listing-confirm)
REST = {
    "listing": {"choice": "gm_1"},
    "objective": {"choice": "bookings"},
    "service": {"choice": "svc_invisalign"},
    "customer_value": {"choice": "estimate"},
}
# The same questions once the business's listing is on file (a new consultation after the walk): no place is asked about
ON_FILE_REST = {qid: value for qid, value in REST.items() if qid != "listing"}
# The answers as REST leaves them, written straight into a session: the pick also centred the ads on the place picked
ANSWERED = {
    **REST,
    "location": {
        "address": "500 University Ave, Palo Alto, CA",
        "lat": 37.4419,
        "lng": -122.143,
        "source": "known_fact",
    },
}
# What a tool says itself to a caller it knows when the thing it needs is missing: no command runs, no row is written
NO_LAUNCH_REQUEST = (
    "nothing is waiting for an approval code on this plan: ask to launch it first, then send the code the owner reads "
    "on their approval page"
)
ALREADY_APPROVED = "this plan is already approved: nothing is left to confirm, and the code was not used"
SETUP_KEY = "a setup key cannot approve or confirm spending"  # confirm_proposal's own refusal of a setup key
ALREADY_ANSWERED = "the questions are already answered: send what should change as message or changes"
NO_CONSULTATION = "there is no consultation yet: begin a plan first"
NO_PLAN = "there is no plan yet: begin a plan first"
QUESTIONS_FIRST = "there is no plan yet: the consultation's questions come first"
SEE_THE_PLAN_FIRST = "the questions are answered: show the plan first, then say what to change"
SEE_THE_PLAN_THEN_LAUNCH = "the questions are answered: show the plan first, then launch it"
CANNOT_BUILD = "the questions are answered and the plan is not built yet; building it needs the manage permission"
MESSAGE_REQUIRED = (
    "message is required: what the owner said about the business, in their own words (or pass handoff alone)"
)
# What the launch says itself to a code that is no code (final review A m6), and confirm_proposal's own refusal of a
# code for a version the plan has moved past
CODE_AS_READ = 'approval_code must be the code as the owner read it, for example "48-311"'
REVISED = "this plan was revised; approve the newest version instead"
# The plan's fields inside `changes`, each with a value of the wrong type and the sentence that refuses it
WRONG_IN_CHANGES = [
    *[
        ({field: value}, f"{field} must be a number")
        for field in ("monthly_budget_usd", "radius_miles", "channel_monthly_usd")
        for value in ("abc", "1,500", [1500], {"a": 1}, True)
    ],
    *[
        ({field: value}, f"{field} must be text")
        for field in ("level", "offer", "objective", "channel")
        for value in (["x"], {"a": 1}, 5)
    ],
    ({"offer": {"text": "Free consultation"}}, "offer must be text"),
]


def _tools(conn) -> list[tuple]:
    return [
        (r["tool"], r["surface_tool"]) for r in conn.execute("SELECT tool, surface_tool FROM agent_action ORDER BY id")
    ]


def _count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]


def test_structured_arguments_become_the_facts_the_consultation_reads():
    facts = surface_plan.facts_from(
        goal={"type": "customers", "target": 30, "max_cost_per_customer_usd": 80},
        service={"name": "Invisalign"},
        location={"address": "500 University Ave, Palo Alto", "radius_miles": 8},
        budget={"monthly_usd": 2000},
        preferred_outcome="forms",
        known_facts={"business_name": "Bright Smile", "customer_value_usd": 3500},
    )
    assert facts == {
        "business_name": "Bright Smile",
        "customer_value_usd": 3500,
        "goal_type": "customers",
        "target_customers_per_month": 30,
        "max_cost_per_customer_usd": 80,
        "service": "Invisalign",
        "address": "500 University Ave, Palo Alto",
        "radius_miles": 8,
        "monthly_budget_usd": 2000,
        "objective": "enquiries",
    }
    assert surface_plan.facts_from(goal={"type": "calls"}) == {"goal_type": "calls", "objective": "calls"}
    assert surface_plan.facts_from(service={"service_id": "svc_invisalign"}) == {"service": "svc_invisalign"}
    with pytest.raises(ToolError, match="goal must be an object"):
        surface_plan.facts_from(goal="30 customers")
    with pytest.raises(ToolError, match="goal.type must be customers, calls or bookings"):
        surface_plan.facts_from(goal={"type": "fame"})
    with pytest.raises(ToolError, match="preferred_outcome must be calls, forms or bookings"):
        surface_plan.facts_from(preferred_outcome="fame")


def test_a_structured_goal_service_and_location_reach_the_consultation_and_the_goal(client, conn, walk_fake):
    """Each structured argument is one of start's known facts: it answers its question or lands on the goal, the goal's
    type included (a goal of calls is counted in calls, not in new customers)."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="verified")
    res = call_tool(
        client,
        "create_marketing_plan",
        {
            "message": WALK_TEXT,
            "goal": {"type": "calls", "target": 30, "max_cost_per_customer_usd": 80},
            "service": {"name": "Invisalign"},
            "location": {"address": "500 University Ave, Palo Alto", "radius_miles": 8},
        },
        key=key,
    )
    assert not res["isError"], tool_text(res)
    assert consult.open_for(conn, "biz_1")["known_facts"] == {
        "goal_type": "calls",
        "target_customers_per_month": 30,
        "max_cost_per_customer_usd": 80,
        "service": "Invisalign",
        "address": "500 University Ave, Palo Alto",
        "radius_miles": 8,
        "objective": "calls",
    }
    goal = business.latest_goal(conn, "biz_1")
    assert (goal["goal_type"], goal["target"], goal["max_cost_per_customer_usd"]) == ("calls", 30, 80.0)


def test_a_new_owner_walks_from_words_to_a_plan_through_three_tools(client, conn, walk_fake):
    benchmark.seed(conn)
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="verified")
    begun = call_tool(
        client,
        "create_marketing_plan",
        {"message": WALK_TEXT, "website_url": "https://brightsmile.example", "budget": {"monthly_usd": 2000}},
        key=key,
    )
    assert not begun["isError"], tool_text(begun)
    env = begun["structuredContent"]
    assert env["next"]["question"]["id"] == "listing"  # the business on Google Maps, first
    assert consult.open_for(conn, "biz_1")["known_facts"]["monthly_budget_usd"] == 2000.0
    zero = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"listing": {"choice": "gm_1"}}, "idempotency_key": "t0"},
        key=key,
    )
    assert not zero["isError"] and zero["structuredContent"]["next"]["question"]["id"] == "objective"
    one = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "t1"},
        key=key,
    )
    assert not one["isError"] and one["structuredContent"]["next"]["question"]["id"] == "service"
    two = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "t2"},
        key=key,
    )
    assert not two["isError"] and two["structuredContent"]["next"]["question"]["id"] == "customer_value"
    # the owner types instead of picking: the words go to the question that is waiting, exactly as typed
    three = call_tool(
        client, "update_marketing_plan", {"message": "around three thousand", "idempotency_key": "t3"}, key=key
    )
    assert data(three)["recorded"]["customer_value"]["amount_usd"] == 3000.0 and data(three)["ready"] is True
    assert [a["tool"] for a in three["structuredContent"]["next"]["actions"]] == ["get_marketing_plan"]
    built = call_tool(client, "get_marketing_plan", {}, key=key)
    assert not built["isError"], tool_text(built)
    assert data(built)["cards"]["plan"]["goal"] and data(built)["status"] == "draft"
    assert {a["tool"] for a in built["structuredContent"]["next"]["actions"]} <= {
        "get_landing_page",  # the four ways to put Admiry's code on the owner's site (A3p-5 batch 2 Q6)
        "update_landing_page",
        "connect_account",
        "get_billing",
        "update_marketing_plan",
    }
    again = call_tool(client, "get_marketing_plan", {}, key=key)
    assert data(again)["plan_id"] == data(built)["plan_id"] and "cards" in data(again)
    assert _tools(conn) == [
        ("places.lookup", None),  # start's lookup of the business, a row of its own as every call to Google (A2)
        ("start", "create_marketing_plan"),
        ("answer", "update_marketing_plan"),
        ("answer", "update_marketing_plan"),
        ("answer", "update_marketing_plan"),
        ("answer", "update_marketing_plan"),
        ("recommend", "get_marketing_plan"),
        ("get_plan", "get_marketing_plan"),
    ]


def test_a_google_only_plan_offers_the_owners_account_and_a_change_and_never_the_balance(
    client, conn, walk_fake, strict_surface
):
    """Walk fixes 1, Task 6: the build of a plan that funds Google Search alone, for a business with its own Google Ads
    account, names linking that account and a change; never the balance (get_billing), which led to "no Admiry
    balance" while Admiry-run Google accounts are not open. Since the founder's ruling of 2026-10-13 the owner chooses
    the way: connect_account's own question's two choices, side by side, neither marked first (Admiry running the ads,
    mode managed; the owner's own account, its Customer ID) -- still never get_billing. (The page is settled: it comes
    first, test_a_plan_answer_asks_for_the_page_before_how_the_ads_run.)"""
    walk = started(client, conn)
    for i, (qid, value) in enumerate(REST.items(), 1):
        res = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"g{i}"}, key=walk["key"]
        )
        assert not res["isError"], tool_text(res)
    settle_page(conn)
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not built["isError"], tool_text(built)
    assert _tools(conn)[-1] == ("recommend", "get_marketing_plan")
    assert [c["platform"] for c in data(built)["cards"]["plan"]["channels"]] == ["google_search"]
    # the four ways to put Admiry's code on the owner's site first, while it has none (A3p-5 batch 2 Q6): public
    # already, they reach the assistant as they are (support off, as here: without Admiry's team, ruling R3-f)
    ways = site_install.ways_for(None)
    assert built["structuredContent"]["next"]["actions"][: len(ways)] == ways
    launch = built["structuredContent"]["next"]["actions"][len(ways) :]
    assert launch[:2] == ads_mode.way_actions() and not any("primary" in a for a in launch[:2])
    assert [(a["tool"], a.get("args")) for a in launch] == [
        ("connect_account", {"mode": "managed"}),
        ("connect_account", {"account_id": "<Customer ID>"}),
        ("update_marketing_plan", None),
    ]


def test_a_read_of_the_plan_asks_how_the_ads_run_while_that_is_the_next_step(client, conn, walk_fake):
    """The founder's ruling (2026-10-13): a read of the plan, while the owner has not chosen how the ads run and that is
    the next step, carries the same question as connect_account -- in `say`, after the plan's own words, and as `next`,
    the two ways side by side, neither marked first. Once a way is chosen, or for an earlier version, a read's `next` is
    empty as before, and its words are the plan's own. (The page is settled: it comes first.)"""
    walk = planned(client, conn)
    settle_page(conn)
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not read["isError"], tool_text(read)
    env = read["structuredContent"]
    assert _tools(conn)[-1] == ("get_plan", "get_marketing_plan")
    assert env["say"] == f"{envelope.SAY['get_plan'](data(read))} {ads_mode.WAY_QUESTION}"
    assert env["next"] == envelope.actions(*ads_mode.way_actions())
    assert not any("primary" in a for a in env["next"]["actions"])
    one = call_tool(client, "get_marketing_plan", {"plan_id": walk["plan_id"]}, key=walk["key"])
    assert one["structuredContent"]["next"] == env["next"]  # the latest version, named: the same step
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    ads_mode.change(conn, "biz_1", "managed")  # the owner chose: nothing more to ask
    chosen = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert chosen["structuredContent"]["next"] == envelope.NONE
    assert chosen["structuredContent"]["say"] == envelope.SAY["get_plan"](data(chosen))


def test_a_read_of_the_plan_asks_no_way_while_another_step_comes_first(client, conn, walk_fake):
    """Owner-chooses review, fix round 1 (item 2): a read asks how the ads run only where get_marketing_state's own
    `next` is that question. A new consultation under way leads with its own question, so a read of the plan it left
    behind is the plan's own words, its `next` empty -- though that plan still waits for the owner's choice of way."""
    walk = planned(client, conn)
    begun = call_tool(client, "create_marketing_plan", {"message": WALK_TEXT}, key=walk["key"])
    assert not begun["isError"], tool_text(begun)
    state = call_tool(client, "get_marketing_state", {}, key=walk["key"])["structuredContent"]
    assert state["next"]["kind"] == "question"  # the consultation's question leads, not the account step
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not read["isError"], tool_text(read)
    assert data(read)["plan_id"] == walk["plan_id"] and data(read)["status"] == plans.DRAFT
    assert read["structuredContent"]["next"] == envelope.NONE
    assert read["structuredContent"]["say"] == envelope.SAY["get_plan"](data(read))


@pytest.mark.parametrize("link", ["active", "pending"])
def test_a_business_that_already_chose_its_way_is_not_asked_again_by_a_plan_answer(client, conn, walk_fake, link):
    """Owner-chooses review, fix round 1 (item 1): a business whose own Google Ads account is linked -- live, or its
    invitation on its way -- has chosen how its ads run (ads_mode.chosen). The build's answer and a change's answer (and
    that change sent again) offer it one connect_account, the own account's, with no arguments -- it answers where the
    link stands -- never the two ways nor a placeholder Customer ID; get_marketing_state leads where it led: past the
    account once the link is live, the account while its invitation waits. (The page is settled: it comes first.)"""
    walk = started(client, conn)
    for i, (qid, value) in enumerate(REST.items(), 1):
        res = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"k{i}"}, key=walk["key"]
        )
        assert not res["isError"], tool_text(res)
    settle_page(conn)
    google_ads_link.save(conn, "biz_1", "1234567890", status=link)
    assert ads_mode.chosen(conn, "biz_1")
    own = {**tools_consult.OWN_ACCOUNT_ACTION, "tool": "connect_account"}  # no `args`, no `primary`
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    change = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "k9"}
    changed = call_tool(client, "update_marketing_plan", change, key=walk["key"])
    again = call_tool(client, "update_marketing_plan", change, key=walk["key"])  # the same turn: its first answer
    assert data(again) == data(changed) and data(changed)["version"] == 2
    for res in (built, changed, again):
        assert not res["isError"], tool_text(res)
        env = res["structuredContent"]
        assert [a for a in env["next"]["actions"] if a["tool"] == "connect_account"] == [own]
        assert ads_mode.CUSTOMER_ID not in json.dumps(env) and ads_mode.WAY_QUESTION not in env["say"]
    state = call_tool(client, "get_marketing_state", {}, key=walk["key"])["structuredContent"]
    account = state["data"]["setup"]["steps"][3]
    assert "choices" not in account and account["label"] != tools_state.WAY_STEP
    led = [a["id"] for a in state["next"]["actions"]] if state["next"]["kind"] == "actions" else []
    assert account["done"] is (link == "active")
    if link == "pending":
        assert led == ["connect"]
    else:
        assert "connect" not in led and not {c["id"] for c in ads_mode.WAY_CHOICES} & set(led)


PAGE_CHOICES = [
    {"id": "admiry_builds", "label": "Admiry builds it for me", "args": {"action": "write"}},
    {"id": "assistant_designs", "label": "My assistant designs it, Admiry hosts it", "args": {"action": "upload_site"}},
    {
        "id": "own_website",
        "label": "I already have a website",
        "args": {"action": "use_url", "url": "<the owner's page address>"},
    },
]


def test_a_plan_answer_asks_for_the_page_before_how_the_ads_run(client, conn, walk_fake, monkeypatch):
    """The founder's ruling (2026-10-13, from a real Muse walk: the connect-and-prepay link came before the page): while
    the latest draft has no page settled, the build's answer, a change's answer (and that change sent again) and a read
    of the plan end with the page question, and their `next` is its three ways alone -- update_landing_page, side by
    side, none marked first -- never the ways the ads run, a step toward the money, nor a placeholder Customer ID. Once
    the page is settled, a read asks how the ads run, as before."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the assistant-designed way is offered
    walk = started(client, conn)
    for i, (qid, value) in enumerate(REST.items(), 1):
        res = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"pf{i}"}, key=walk["key"]
        )
        assert not res["isError"], tool_text(res)
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    change = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "pf9"}
    changed = call_tool(client, "update_marketing_plan", change, key=walk["key"])
    again = call_tool(client, "update_marketing_plan", change, key=walk["key"])  # the same turn: its first answer
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    page_next = envelope.actions(*launch_check.page_actions(PAGE_CHOICES))
    for res in (built, changed, again, read):
        assert not res["isError"], tool_text(res)
        env = res["structuredContent"]
        assert env["say"].endswith(f" {launch_check.PAGE_QUESTION}") and env["say"].count("Which page") == 1
        assert env["next"] == page_next and not any("primary" in a for a in env["next"]["actions"])
        assert ads_mode.WAY_QUESTION not in env["say"] and ads_mode.CUSTOMER_ID not in json.dumps(env["next"])
    assert data(again) == data(changed) and data(changed)["version"] == 2
    settle_page(conn)
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert read["structuredContent"]["next"] == envelope.actions(*ads_mode.way_actions())
    assert read["structuredContent"]["say"] == f"{envelope.SAY['get_plan'](data(read))} {ads_mode.WAY_QUESTION}"


def test_a_plan_answer_with_a_page_in_hand_leads_to_publishing_it(client, conn, walk_fake, monkeypatch):
    """Final review, item 1: once the owner's assistant has put a page on a private preview (preview_html), the page is
    in hand: a change's answer and a read of the plan end with the sentence that leads to publishing it, and their
    `next` is that one step -- update_landing_page's "publish" with the preview's token -- never the three ways again,
    nor the ways the ads run."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)
    args = {"action": "preview_html", "html": GOOD_HTML, "idempotency_key": "in-hand"}
    made = call_tool(client, "update_landing_page", args, key=walk["key"])
    assert not made["isError"], tool_text(made)
    publish = {"action": "publish", "token": data(made)["token"]}
    lead = envelope.actions(
        {"id": "publish", "label": "Publish the page", "tool": "update_landing_page", "args": publish}
    )
    changed = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "ih"},
        key=walk["key"],
    )
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    for res in (changed, read):
        assert not res["isError"], tool_text(res)
        env = res["structuredContent"]
        assert env["say"].endswith(f" {launch_check.PAGE_READY}") and launch_check.PAGE_QUESTION not in env["say"]
        assert env["next"] == lead and ads_mode.WAY_QUESTION not in env["say"]


def test_a_plan_answer_reads_the_page_and_the_way_in_one_look(client, conn, walk_fake, monkeypatch):
    """Page-first review, item 3: what a plan answer that offers the ways asks of the caller's business -- whether the
    page comes first, and whether the business has already chosen how its ads run -- is one read: one connection, one
    look at the key (where.owner_choice), never one per question. The page is settled and no way is chosen here, so
    the answer needs both and offers the two ways."""
    from agent.surface import where as surface_where

    walk = planned(client, conn)
    settle_page(conn)
    looked: list[str] = []
    real = surface_where._caller_business
    monkeypatch.setattr(surface_where, "_caller_business", lambda c: looked.append("key") or real(c))
    change = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "one-look"}
    res = call_tool(client, "update_marketing_plan", change, key=walk["key"])
    assert not res["isError"], tool_text(res)
    offered = res["structuredContent"]["next"]["actions"]
    assert all(way in offered for way in ads_mode.way_actions())  # after the ways to Admiry's code on the website
    assert looked == ["key"]


def test_one_turn_takes_exactly_one_of_answers_message_and_changes(client, conn, walk_fake):
    walk = started(client, conn)
    before = _count(conn)
    for args in ({}, {"answers": {"objective": {"choice": "bookings"}}, "message": "bookings"}):
        res = call_tool(client, "update_marketing_plan", {**args, "idempotency_key": "x"}, key=walk["key"])
        assert res["isError"] and "send exactly one of answers, message or changes" in tool_text(res)
    assert _count(conn) == before  # refused by the tool: no command ran


def test_a_plan_begun_with_no_words_is_told_what_the_tool_needs(client, conn, walk_fake, strict_surface):
    """Final review C, minor 1: no words of the owner's (no message, no description, or only blanks) and no hand-off:
    the tool says what it needs in its own arguments' names -- start would say "text is required", a parameter the
    tool does not have. No command runs and no row is written. A hand-off alone still reaches start, which answers for
    its code."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="verified")
    for args in (
        {},
        {"message": "   "},
        {"message": "", "description": "  "},
        {"website_url": "https://brightsmile.example", "known_facts": {"business_name": "Bright Smile Dental"}},
    ):
        res = call_tool(client, "create_marketing_plan", args, key=key)
        assert res["isError"] and tool_text(res) == MESSAGE_REQUIRED, (args, tool_text(res))
    assert _count(conn) == 0
    handoff = call_tool(client, "create_marketing_plan", {"handoff": "hs_unknown"}, key=key)
    assert handoff["isError"] and "already used or expired" in tool_text(handoff), tool_text(handoff)
    assert _tools(conn) == [("start", "create_marketing_plan")]


def test_changes_go_to_the_plan_or_to_the_goal_never_both(client, conn, walk_fake):
    walk = planned(client, conn)
    budget = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "c1"},
        key=walk["key"],
    )
    assert not budget["isError"] and data(budget)["cards"]["budget"]["monthly_usd"] == 1500
    before = business.latest_goal(conn, "biz_1")
    goal = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"target": 30, "max_cost_per_customer_usd": 80}, "idempotency_key": "c2"},
        key=walk["key"],
    )
    assert not goal["isError"], tool_text(goal)
    latest = business.latest_goal(conn, "biz_1")
    assert (latest["target"], latest["max_cost_per_customer_usd"]) == (30, 80.0)
    # a change to part of the goal keeps the rest: the monthly limit and whose limit it is
    assert (latest["monthly_limit_usd"], latest["limit_source"]) == (
        before["monthly_limit_usd"],
        before["limit_source"],
    )
    both = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1200, "target": 10}, "idempotency_key": "c3"},
        key=walk["key"],
    )
    assert both["isError"] and "in separate calls" in tool_text(both)
    odd = call_tool(
        client, "update_marketing_plan", {"changes": {"colour": "red"}, "idempotency_key": "c4"}, key=walk["key"]
    )
    assert odd["isError"] and "unknown changes: ['colour']" in tool_text(odd)
    # the two refusals above ran no command: the last rows are the two changes, each under the public tool
    assert _tools(conn)[-2:] == [("adjust", "update_marketing_plan"), ("change_goal", "update_marketing_plan")]


def test_a_value_of_the_wrong_type_inside_changes_is_refused_in_its_own_sentence(
    client, conn, walk_fake, strict_surface
):
    """Final review B, Important 6 (A m3): FastMCP checks a public tool's own arguments, not the values inside
    `changes`, and the tool calls its command directly. Each value of the wrong type reaches adjust, which refuses it
    in a sentence that names the field: no plan version is written, and the failure row carries the sentence -- never
    "internal error". An offer sent as an object is refused too, never written onto the plan card as its repr."""
    walk = planned(client, conn)
    versions = conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"]
    for i, (changes, sentence) in enumerate(WRONG_IN_CHANGES):
        res = call_tool(
            client, "update_marketing_plan", {"changes": changes, "idempotency_key": f"t{i}"}, key=walk["key"]
        )
        assert res["isError"] and tool_text(res) == sentence, (changes, tool_text(res))
        row = conn.execute(
            "SELECT tool, surface_tool, ok, result FROM agent_action ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (row["tool"], row["surface_tool"], row["ok"], row["result"]) == (
            "adjust",
            "update_marketing_plan",
            False,
            {"error": sentence},
        ), changes
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == versions
    errors = conn.execute("SELECT count(*) AS n FROM agent_action WHERE result->>'error' = 'internal error'")
    assert errors.fetchone()["n"] == 0


def test_words_after_the_plan_go_to_change_plan(client, conn, walk_fake):
    walk = planned(client, conn)
    blank = dict.fromkeys(("level", "radius_miles", "offer", "objective", "channel", "channel_monthly_usd"), None)
    walk_fake.adjusts["make it $1,500 a month"] = models.AdjustRead(
        monthly_budget_usd=1500, understood=True, asks_for_change=True, **blank
    )
    res = call_tool(
        client, "update_marketing_plan", {"message": "make it $1,500 a month", "idempotency_key": "m1"}, key=walk["key"]
    )
    assert not res["isError"] and data(res)["cards"]["budget"]["monthly_usd"] == 1500
    assert _tools(conn)[-1] == ("change_plan", "update_marketing_plan")


def test_words_that_name_a_plan_change_it_while_a_new_consultation_asks(client, conn, walk_fake):
    """Final review B, minor: a business with a plan begins a new consultation. Words sent with the plan's id are a
    change to that plan, and the consultation's answers stay as they were; words without it answer the question that
    waits, as before. Once the questions are answered, words with a plan's id still change it, and words without one
    are told to see the plan first."""
    walk = planned(client, conn)
    begun = call_tool(client, "create_marketing_plan", {"message": WALK_TEXT}, key=walk["key"])
    assert not begun["isError"], tool_text(begun)
    # the website, first: the words name none (plan 2026-10-05-agent-a3p-5-1, Q15) -- the fixture site, read
    site = {"answers": {"website": {"other": "https://brightsmile.example"}}, "idempotency_key": "w0"}
    assert not call_tool(client, "update_marketing_plan", site, key=walk["key"])["isError"]
    asking = consult.open_for(conn, "biz_1")
    assert asking["status"] == "open" and asking["id"] == data(begun)["session_id"]
    blank = dict.fromkeys(("level", "radius_miles", "offer", "objective", "channel", "channel_monthly_usd"), None)
    for usd in (1500, 1200):
        walk_fake.adjusts[f"make it ${usd:,} a month"] = models.AdjustRead(
            monthly_budget_usd=usd, understood=True, asks_for_change=True, **blank
        )
    named = call_tool(
        client,
        "update_marketing_plan",
        {"message": "make it $1,500 a month", "plan_id": walk["plan_id"], "idempotency_key": "w1"},
        key=walk["key"],
    )
    assert not named["isError"], tool_text(named)
    assert _tools(conn)[-1] == ("change_plan", "update_marketing_plan")
    assert data(named)["cards"]["budget"]["monthly_usd"] == 1500 and data(named)["version"] == 2
    assert consult.open_for(conn, "biz_1")["answers"] == asking["answers"]  # the consultation's answers: untouched
    unnamed = call_tool(
        client, "update_marketing_plan", {"message": "bookings, mostly", "idempotency_key": "w2"}, key=walk["key"]
    )
    assert not unnamed["isError"], tool_text(unnamed)
    assert _tools(conn)[-1] == ("answer", "update_marketing_plan")  # the question that waits takes it, as before
    for i, (qid, value) in enumerate(ON_FILE_REST.items(), 1):
        turn = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"w-a{i}"}, key=walk["key"]
        )
        assert not turn["isError"], tool_text(turn)
    assert consult.open_for(conn, "biz_1")["status"] == "ready"
    again = call_tool(
        client,
        "update_marketing_plan",
        {"message": "make it $1,200 a month", "plan_id": data(named)["plan_id"], "idempotency_key": "w3"},
        key=walk["key"],
    )
    assert not again["isError"], tool_text(again)
    assert _tools(conn)[-1] == ("change_plan", "update_marketing_plan") and data(again)["version"] == 3
    before = _count(conn)
    unnamed = call_tool(
        client, "update_marketing_plan", {"message": "make it $1,200 a month", "idempotency_key": "w4"}, key=walk["key"]
    )
    assert unnamed["isError"] and SEE_THE_PLAN_FIRST in tool_text(unnamed)
    assert _count(conn) == before


def _automatic_changes(res: dict) -> str:
    """The plan line that says whether Admiry may change things by itself, as the answer carries it."""
    return data(res)["lines"][plans.SECTION_ORDER.index("optimization")]


def test_a_new_versions_line_about_automatic_changes_follows_the_goal(client, conn, walk_fake):
    """Final review B, Important 10 (open item B5): once the owner turned automatic changes off, a budget change (built
    from the previous version) and a rewrite of the owner's words (built from the model's draft) both write a version
    that says so; back to safe, both say the safe line -- also when the model's draft says otherwise."""
    walk = planned(client, conn)
    key = walk["key"]

    def call(tool: str, args: dict, ik: str) -> dict:
        res = call_tool(client, tool, {**args, "idempotency_key": ik}, key=key)
        assert not res["isError"], tool_text(res)
        return res

    call("update_marketing_plan", {"changes": {"autopilot": "off"}}, "g1")
    budget = call("update_marketing_budget", {"level": "conservative"}, "b1")
    words = call("update_marketing_plan", {"message": "lead with whitening"}, "m1")
    for res in (budget, words):
        assert plans.AUTOPILOT_LINE["off"] in _automatic_changes(res).lower(), _automatic_changes(res)
    assert plans.latest(conn, "biz_1")["sections"]["optimization"]["autopilot"] == "off"
    call("update_marketing_plan", {"changes": {"autopilot": "safe"}}, "g2")
    walk_fake.draft = plan_draft_fixture().model_copy(update={"autopilot": "off"})  # the model's draft says off
    budget = call("update_marketing_budget", {"level": "recommended"}, "b2")
    words = call("update_marketing_plan", {"message": "lead with the free consultation"}, "m2")
    for res in (budget, words):
        assert plans.AUTOPILOT_LINE["safe"] in _automatic_changes(res).lower(), _automatic_changes(res)
    assert plans.latest(conn, "biz_1")["sections"]["optimization"]["autopilot"] == "safe"


def test_words_on_a_ready_consultation_are_told_to_see_the_plan_first(client, conn, walk_fake):
    walk = started(client, conn)
    for i, (qid, value) in enumerate(REST.items(), 1):
        call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"r{i}"}, key=walk["key"]
        )
    before = _count(conn)
    res = call_tool(
        client, "update_marketing_plan", {"message": "cheaper please", "idempotency_key": "r9"}, key=walk["key"]
    )
    assert res["isError"] and "show the plan first" in tool_text(res)
    assert _count(conn) == before  # refused by the tool: no command ran


def test_a_consultation_left_open_with_nothing_to_ask_is_built_too(client, conn, walk_fake):
    """Its questions changed under it: still `open`, nothing pending. The state says the plan is ready to be built
    (Task 6), so the tool it points at builds it."""
    walk = started(client, conn)
    conn.execute(
        "UPDATE consult_session SET answers = answers || %s WHERE id = %s", (Jsonb(ANSWERED), walk["session_id"])
    )
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not built["isError"], tool_text(built)
    assert data(built)["status"] == "draft" and _tools(conn)[-1] == ("recommend", "get_marketing_plan")


def test_reading_one_version_never_builds_even_while_a_consultation_is_ready(client, conn, walk_fake):
    """`plan_id` reads that version and nothing else: a consultation begun again and ready to build stays unbuilt."""
    walk = planned(client, conn)
    again = call_tool(client, "create_marketing_plan", {"message": WALK_TEXT}, key=walk["key"])
    assert not again["isError"], tool_text(again)
    conn.execute(
        "UPDATE consult_session SET answers = answers || %s, status = 'ready' WHERE id = %s",
        (Jsonb(ON_FILE_REST), data(again)["session_id"]),
    )
    res = call_tool(client, "get_marketing_plan", {"plan_id": walk["plan_id"]}, key=walk["key"])
    assert not res["isError"] and data(res)["plan_id"] == walk["plan_id"]
    assert _tools(conn)[-1] == ("get_plan", "get_marketing_plan")
    assert [tool for tool, _ in _tools(conn)].count("recommend") == 1  # the walk's own build, and no other
    assert consult.open_for(conn, "biz_1")["status"] == "ready"


def test_a_key_that_cannot_build_is_told_so_and_nothing_runs(client, conn, walk_fake):
    """A reader's call would reach `recommend` only to be refused there, and that refusal counts against the owner's
    daily model limit: the tool refuses first, in its own words, and nothing is recorded. A key that may manage
    builds."""
    walk = started(client, conn)
    conn.execute(
        "UPDATE consult_session SET answers = answers || %s WHERE id = %s", (Jsonb(ANSWERED), walk["session_id"])
    )
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    with auth.with_headers({"Authorization": f"Bearer {reader}"}):
        assert where().scopes == ("read",)
    before = _count(conn)
    refused = call_tool(client, "get_marketing_plan", {}, key=reader)
    assert refused["isError"] and CANNOT_BUILD in tool_text(refused)
    assert _count(conn) == before and "recommend" not in [tool for tool, _ in _tools(conn)]
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not built["isError"], tool_text(built)
    assert data(built)["status"] == "draft" and _tools(conn)[-1] == ("recommend", "get_marketing_plan")


def test_launching_asks_for_the_code_and_then_takes_it(client, conn, walk_fake):
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "l1"}, key=walk["key"])
    assert not asked["isError"], tool_text(asked)
    assert data(asked)["status"] == "proposed" and data(asked)["needs_code"] is True
    assert data(asked)["approval_url"].endswith(f"/plan/{walk['plan_id']}")
    wrong = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "l2", "approval_code": "00-000"}, key=walk["key"]
    )
    assert wrong["isError"] and "not valid for this proposal" in tool_text(wrong)
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    done = call_tool(client, "launch_marketing_plan", {"idempotency_key": "l3", "approval_code": code}, key=walk["key"])
    assert not done["isError"], tool_text(done)  # every channel's switch is off in tests: approved, nothing launched
    assert plans.latest(conn, "biz_1")["status"] == "approved"
    assert _tools(conn)[-3:] == [
        ("approve_plan", "launch_marketing_plan"),
        ("confirm_proposal", "launch_marketing_plan"),
        ("confirm_proposal", "launch_marketing_plan"),
    ]
    row = conn.execute("SELECT args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert code not in str(row["args_redacted"])  # the code never sits in the audit row


def test_a_launch_refused_for_a_link_not_made_names_neither_way_while_none_is_chosen(client, conn, monkeypatch):
    """Addendum 3 (founder 2026-10-13): launch_marketing_plan's refusal for a Google Ads link not made, while the owner
    has chosen no way (no link, no agreement, Admiry not running the ads), is the neutral line -- never "since your ads
    run in your own account", which picks a way for them; the code is not used. Once a way is chosen, today's
    sentence."""
    from agent.channels.google_search import adapter as google
    from agent.tests.test_ads_run import seed

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    key, _plan_id = seed(conn, status="draft", link=None)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "nl1"}, key=key)
    assert not asked["isError"], tool_text(asked)
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    refused = call_tool(client, "launch_marketing_plan", {"idempotency_key": "nl2", "approval_code": code}, key=key)
    assert refused["isError"] and tool_text(refused) == ads_mode.WAY_LINE
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    from agent import agreements

    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")  # the owner's own account
    refused = call_tool(client, "launch_marketing_plan", {"idempotency_key": "nl3", "approval_code": code}, key=key)
    assert refused["isError"] and tool_text(refused) == google.NOT_LINKED
    assert conn.execute("SELECT used_at FROM approval_code").fetchone()["used_at"] is None


def test_a_second_ask_to_launch_keeps_the_code_the_owner_already_has(client, conn, walk_fake, strict_surface):
    """Final review B, Important 1: the assistant asks, the owner reads the code on their approval page, the assistant
    asks again with a new key. The answer is the same request -- still the only one open -- and the owner's code
    launches the plan: no wrong-code strike, no second request in its way."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    assert not asked["isError"], tool_text(asked)
    pid = data(asked)["proposal_id"]
    code = proposals.issue_code(conn, pid, via="plan_page")
    again = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-2"}, key=walk["key"])
    assert not again["isError"] and data(again)["proposal_id"] == pid, tool_text(again)
    assert [p["proposal_id"] for p in proposals.list_open(conn, "biz_1")] == [pid]
    done = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"])
    assert not done["isError"], tool_text(done)
    assert plans.latest(conn, "biz_1")["status"] == "approved"
    strikes = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'confirm_proposal' AND NOT ok")
    assert strikes.fetchone()["n"] == 0


def test_a_code_after_the_owner_approved_on_the_page_is_told_the_plan_is_approved(
    client, conn, walk_fake, strict_surface
):
    """Final review open item A1: the owner approved on their page, and the assistant still sends a code. Nothing is
    left to confirm: the tool says the plan is already approved -- not "ask to launch it first", which is false of an
    approved plan -- and no command runs."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    with conn.transaction():  # the owner's own click on the page
        proposals.confirm(conn, "biz_1", data(asked)["proposal_id"], code=None, web_owner=True)
    before = _count(conn)
    res = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "code-1", "approval_code": "12-345"}, key=walk["key"]
    )
    assert res["isError"] and ALREADY_APPROVED in tool_text(res), tool_text(res)
    assert _count(conn) == before  # no row of confirm_proposal, nor of anything else
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'confirm_proposal'").fetchone()["n"] == 0


def test_a_launch_request_left_open_on_an_approved_plan_is_not_waiting_for_a_code(
    client, conn, walk_fake, strict_surface
):
    """Two launch requests for one plan (as there could be before a second ask reused the open one): the owner
    approved one on their page and the other is still open. That one waits for nothing -- its plan is approved -- so a
    code sent now gets the tool's own answer that the plan is already approved: no command runs and no row is
    written."""
    walk = planned(client, conn)
    with conn.transaction():
        approved = proposals.propose_plan(conn, "biz_1", walk["plan_id"])
        left = proposals.propose_plan(conn, "biz_1", walk["plan_id"])
        proposals.confirm(conn, "biz_1", approved["proposal_id"], code=None, web_owner=True)
    assert [p["proposal_id"] for p in proposals.list_open(conn, "biz_1")] == [left["proposal_id"]]
    before = _count(conn)
    res = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "late", "approval_code": "12-345"}, key=walk["key"]
    )
    assert res["isError"] and tool_text(res) == surface_plan.ALREADY_APPROVED, tool_text(res)
    assert _count(conn) == before
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'confirm_proposal'").fetchone()["n"] == 0


def test_asking_again_on_an_in_review_plan_is_told_so_not_create_or_revise(client, conn, walk_fake, monkeypatch):
    """L1 review I1a: once a first launch is in Admiry's review, asking to launch it again must not land on
    propose_plan's generic "create or revise a plan first" -- the plan needs neither; it is simply waiting."""
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    done = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"])
    assert not done["isError"], tool_text(done)
    assert plans.latest(conn, "biz_1")["status"] == "in_review"
    before = _count(conn)
    again = call_tool(client, "launch_marketing_plan", {"idempotency_key": "again"}, key=walk["key"])
    assert again["isError"] and tool_text(again) == proposals.IN_REVIEW_REFUSAL, tool_text(again)
    assert _count(conn) == before + 1 and _tools(conn)[-1] == ("approve_plan", "launch_marketing_plan")


def test_a_fresh_code_on_an_in_review_plan_is_told_so_too(client, conn, walk_fake, monkeypatch):
    """L1 review I1b: nothing waits for a code once the plan is in review (its own proposal confirmed already); a
    fresh code sent anyway is told the plan is in review, not "nothing is waiting for an approval code"."""
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    done = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"])
    assert not done["isError"], tool_text(done)
    before = _count(conn)
    res = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "fresh", "approval_code": "12-345"}, key=walk["key"]
    )
    assert res["isError"] and tool_text(res) == proposals.IN_REVIEW_REFUSAL, tool_text(res)
    assert _count(conn) == before  # the tool's own refusal: no command ran


def _unused_codes(conn, proposal_id: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM approval_code WHERE proposal_id = %s AND used_at IS NULL", (proposal_id,)
    ).fetchone()["n"]


def test_a_code_sent_for_a_version_the_plan_moved_past_is_refused_by_the_launch_and_kept(
    client, conn, walk_fake, strict_surface
):
    """Carried from part 2a: the owner's code for version 1 arrives after the budget changed (version 2), sent with
    version 1's plan_id. Its request still waits, so the code reaches confirm_proposal, whose own refusal says the way
    on -- before any code is used: the owner's code stays good, and nothing is launched."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    pid = data(asked)["proposal_id"]
    code = proposals.issue_code(conn, pid, via="test")
    changed = call_tool(
        client, "update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "b1"}, key=walk["key"]
    )
    assert not changed["isError"] and data(changed)["version"] == 2, tool_text(changed)
    sent = {"idempotency_key": "go", "approval_code": code, "plan_id": walk["plan_id"]}
    res = call_tool(client, "launch_marketing_plan", sent, key=walk["key"])
    assert res["isError"] and tool_text(res) == REVISED, tool_text(res)
    assert _tools(conn)[-1] == ("confirm_proposal", "launch_marketing_plan")
    assert _unused_codes(conn, pid) == 1
    approved = conn.execute("SELECT count(*) AS n FROM plan WHERE status = 'approved'").fetchone()["n"]
    assert approved == 0 and conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0


def test_five_wrong_codes_lock_the_launch_and_the_owners_code_waits_out_the_hour(
    client, conn, walk_fake, strict_surface
):
    """Final review A m9 (a probe kept): through the public tool, five wrong codes lock the launch request; the
    owner's right code is refused while it is locked and is not used up, and launches the plan once the hour is
    over."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    pid = data(asked)["proposal_id"]
    code = proposals.issue_code(conn, pid, via="test")
    wrong = next(guess for guess in ("11-111", "22-222") if guess != code)
    for i in range(proposals.CODE_ATTEMPTS_PER_HOUR):
        res = call_tool(
            client, "launch_marketing_plan", {"idempotency_key": f"w{i}", "approval_code": wrong}, key=walk["key"]
        )
        assert res["isError"] and proposals.WRONG_CODE in tool_text(res), tool_text(res)
    locked = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "right-1", "approval_code": code}, key=walk["key"]
    )
    assert locked["isError"] and proposals.LOCKED in tool_text(locked), tool_text(locked)
    assert _unused_codes(conn, pid) == 1 and plans.latest(conn, "biz_1")["status"] == "draft"
    conn.execute("UPDATE agent_action SET ts = ts - interval '61 minutes' WHERE tool = 'confirm_proposal'")
    done = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "right-2", "approval_code": code}, key=walk["key"]
    )
    assert not done["isError"], tool_text(done)
    assert plans.latest(conn, "biz_1")["status"] == "approved" and _unused_codes(conn, pid) == 0


def test_leads_another_key_records_never_hold_back_the_owners_launch(client, conn, walk_fake, strict_surface):
    """Final review A I1, the review's reproduction: a key that may only record leads records thirty in an hour. None
    of them is a change the hourly limit counts, so the owner's code still launches the plan."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    leads_only, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("leads",), label="claude")
    for i in range(auth.MUTATIONS_PER_HOUR):
        lead = {"kind": "call", "status": "booked", "idempotency_key": f"lead-{i}"}
        res = call_tool(client, "add_lead", lead, key=leads_only)
        assert not res["isError"], tool_text(res)
    launched = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"]
    )
    assert not launched["isError"], tool_text(launched)
    assert plans.latest(conn, "biz_1")["status"] == "approved"


def test_a_readers_refused_model_calls_never_use_up_the_owners_day(
    client, conn, walk_fake, strict_surface, monkeypatch
):
    """Final review B (security, minor), the review's reproduction: a key that may only read sends a dozen changes in
    words. Each is refused for its scope -- recorded, but not one of the business's model calls -- so the owner's own
    key still builds the plan (the day's cap set to 10 here, below what the refusals would have counted)."""
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 10)
    walk = started(client, conn)
    for i, (qid, value) in enumerate(REST.items(), 1):
        turn = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"a{i}"}, key=walk["key"]
        )
        assert not turn["isError"], tool_text(turn)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    for i in range(12):
        words = {"message": "lead with whitening", "idempotency_key": f"r{i}"}
        res = call_tool(client, "update_marketing_plan", words, key=reader)
        assert res["isError"] and "lacks the 'manage' scope" in tool_text(res), tool_text(res)
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not built["isError"] and data(built)["status"] == "draft", tool_text(built)


# Every form a five-digit code could take in a row (as typed, as digits, as a number that lost its leading zero),
# standing alone: never inside an id's hex
STORED_CODE = re.compile(r"\b(?:04-831|04831|4831)\b")


def test_a_code_sent_as_a_number_launches_the_plan_and_is_never_stored(
    client, conn, walk_fake, strict_surface, monkeypatch
):
    """Final review A m6: a number is the code -- 4831 is "04-831" with its leading zero lost on the way. It launches
    the plan, and no row holds it in any form: not the arguments, not the result, not the call's identity."""
    monkeypatch.setattr(proposals, "new_code", lambda: "04-831")
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    assert proposals.issue_code(conn, data(asked)["proposal_id"], via="test") == "04-831"
    done = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": 4831}, key=walk["key"])
    assert not done["isError"], tool_text(done)
    assert plans.latest(conn, "biz_1")["status"] == "approved"
    for row in conn.execute("SELECT tool, args_redacted, result, idempotency_key, surface_tool FROM agent_action"):
        assert not STORED_CODE.search(json.dumps(row, default=str)), row
    identity = conn.execute("SELECT surface_call_hash FROM agent_action WHERE idempotency_key = 'go'").fetchone()
    blanked = {"idempotency_key": "go", "approval_code": "***"}
    assert identity["surface_call_hash"] == audit.call_hash("launch_marketing_plan", blanked)


def test_a_boolean_is_no_approval_code(client, conn, walk_fake, strict_surface):
    """true or false is not a code the owner read: the launch says so itself, before anything else, and no command
    runs."""
    walk = planned(client, conn)
    assert not call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])["isError"]
    before = _count(conn)
    for value in (True, False):
        sent = {"idempotency_key": f"code-{value}", "approval_code": value}
        res = call_tool(client, "launch_marketing_plan", sent, key=walk["key"])
        assert res["isError"] and tool_text(res) == CODE_AS_READ, (value, tool_text(res))
    assert _count(conn) == before and plans.latest(conn, "biz_1")["status"] == "draft"


def test_a_setup_key_with_a_code_gets_the_commands_own_refusal(client, conn, walk_fake, strict_surface):
    """Final review B, minor: the tool does not answer a setup key's code itself -- with nothing waiting, nor once the
    plan is approved -- confirm_proposal refuses it in its own words (sign in and finish setup)."""
    walk = planned(client, conn)
    setup, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    sent = {"idempotency_key": "s1", "approval_code": "12-345"}
    never_asked = call_tool(client, "launch_marketing_plan", sent, key=setup)
    assert never_asked["isError"] and SETUP_KEY in tool_text(never_asked), tool_text(never_asked)
    assert _tools(conn)[-1] == ("confirm_proposal", "launch_marketing_plan")
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    with conn.transaction():
        proposals.confirm(conn, "biz_1", data(asked)["proposal_id"], code=None, web_owner=True)
    approved = call_tool(client, "launch_marketing_plan", {**sent, "idempotency_key": "s2"}, key=setup)
    assert approved["isError"] and SETUP_KEY in tool_text(approved), tool_text(approved)
    assert _tools(conn)[-1] == ("confirm_proposal", "launch_marketing_plan")


def test_the_first_answer_of_a_launch_offers_the_approval_page(client, conn, walk_fake, strict_surface):
    """Final review C, minor 3: the ask's `next` is a link to the owner's approval page, its sentence as before; the
    same ask sent again (approve_plan's own replay) carries the same link."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "l1"}, key=walk["key"])
    assert not asked["isError"], tool_text(asked)
    env = asked["structuredContent"]
    page = {"kind": "link", "link": {"url": data(asked)["approval_url"], "label": "Open the approval page"}}
    assert env["next"] == page and env["say"] == envelope.SAY["approve_plan"](data(asked))
    again = call_tool(client, "launch_marketing_plan", {"idempotency_key": "l1"}, key=walk["key"])
    assert not again["isError"] and again["structuredContent"]["next"] == page, tool_text(again)
    assert again["structuredContent"]["say"] == env["say"]


def test_the_first_answer_for_an_unverified_business_offers_where_setup_finishes(
    client, conn, walk_fake, strict_surface
):
    walk = planned(client, conn, claim_state="claimed")
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "v1"}, key=walk["key"])
    assert not asked["isError"] and data(asked)["status"] == "needs_verification", tool_text(asked)
    env = asked["structuredContent"]
    finish = {"kind": "link", "link": {"url": data(asked)["verify_url"], "label": "Finish setting up"}}
    assert env["next"] == finish and env["say"] == envelope.SAY["approve_plan"](data(asked))
    again = call_tool(client, "launch_marketing_plan", {"idempotency_key": "v1"}, key=walk["key"])
    assert not again["isError"] and again["structuredContent"]["next"] == finish, tool_text(again)


def test_a_known_caller_with_nothing_begun_is_told_so_and_nothing_runs(client, conn):
    """No consultation, no plan, nothing waiting for a code: the tool says what is missing itself; no command runs and
    no row is written."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="verified")
    cases = (
        ("update_marketing_plan", {"answers": {"objective": {"choice": "bookings"}}}, NO_CONSULTATION),
        ("update_marketing_plan", {"message": "make it $1,500 a month"}, NO_PLAN),
        ("update_marketing_plan", {"changes": {"monthly_budget_usd": 1500}}, NO_PLAN),
        ("launch_marketing_plan", {"approval_code": "12-345"}, NO_LAUNCH_REQUEST),
    )
    for i, (tool, args, sentence) in enumerate(cases):
        res = call_tool(client, tool, {**args, "idempotency_key": f"n{i}"}, key=key)
        assert res["isError"] and sentence in tool_text(res), (tool, args, tool_text(res))
    assert _count(conn) == 0


def test_a_change_or_a_launch_before_the_plan_exists_is_refused_by_the_tool(client, conn, walk_fake):
    """No plan yet and none named (Task 18: two states Task 10's rule missed reached a command with an empty id, which
    answered "no plan with that id"). The plan's changes and a launch are told what comes first: the questions while
    the consultation still asks them, the plan once they are answered -- then what to change, or the launch. No
    command runs and no row is written."""
    walk = started(client, conn)
    budget = {"changes": {"monthly_budget_usd": 1500}}
    calls = (("update_marketing_plan", budget), ("launch_marketing_plan", {}))
    before = _count(conn)
    for i, (tool, args) in enumerate(calls):
        res = call_tool(client, tool, {**args, "idempotency_key": f"e{i}"}, key=walk["key"])
        assert res["isError"] and QUESTIONS_FIRST in tool_text(res), (tool, tool_text(res))
    assert _count(conn) == before
    for i, (qid, value) in enumerate(REST.items(), 1):
        turn = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"e-a{i}"}, key=walk["key"]
        )
        assert not turn["isError"], tool_text(turn)
    before = _count(conn)
    for i, ((tool, args), sentence) in enumerate(zip(calls, (SEE_THE_PLAN_FIRST, SEE_THE_PLAN_THEN_LAUNCH))):
        res = call_tool(client, tool, {**args, "idempotency_key": f"e-r{i}"}, key=walk["key"])
        assert res["isError"] and sentence in tool_text(res), (tool, tool_text(res))
    assert _count(conn) == before
    # a plan the call names goes to adjust, which answers for it in its own words
    named = call_tool(
        client, "update_marketing_plan", {**budget, "plan_id": "plan_named", "idempotency_key": "e4"}, key=walk["key"]
    )
    assert _count(conn) == before + 1 and _tools(conn)[-1] == ("adjust", "update_marketing_plan"), tool_text(named)


def test_a_launch_with_nothing_begun_is_told_so_and_a_setup_key_gets_the_commands_answer(client, conn):
    """With nothing begun, a launch from a caller the tool knows is told there is no plan, and no command runs. A
    setup key is not refused by the tool: approve_plan's own answer is the one it needs (sign in first)."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="verified")
    res = call_tool(client, "launch_marketing_plan", {"idempotency_key": "n1"}, key=key)
    assert res["isError"] and NO_PLAN in tool_text(res)
    assert _count(conn) == 0
    setup = business_with_key(conn, biz_id="biz_2", cls="draft")
    answered = call_tool(client, "launch_marketing_plan", {"idempotency_key": "n2"}, key=setup)
    assert answered["isError"] and "a setup key cannot approve or confirm spending" in tool_text(answered)
    assert _tools(conn) == [("approve_plan", "launch_marketing_plan")]


def test_what_a_call_names_itself_still_reaches_its_command(client, conn):
    """The other side of the tool's own refusals (Task 18): a known caller with nothing begun, whose call names what
    it is for. The goal's changes need no plan and run change_goal; answers for a named consultation reach answer
    although where() finds none of the caller's own; a launch of a named plan asks approve_plan for that plan. Each
    command answers in its own words: what is pinned is the one audit row each call leaves."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="verified")
    cases = (
        ("update_marketing_plan", {"changes": {"target": 30}}, "change_goal"),
        (
            "update_marketing_plan",
            {"answers": {"objective": {"choice": "bookings"}}, "session_id": "cs_named"},
            "answer",
        ),
        ("launch_marketing_plan", {"plan_id": "plan_named"}, "approve_plan"),
    )
    for i, (tool, args, command) in enumerate(cases):
        res = call_tool(client, tool, {**args, "idempotency_key": f"o{i}"}, key=key)
        assert _count(conn) == i + 1 and _tools(conn)[-1] == (command, tool), (args, tool_text(res))
    asked = conn.execute("SELECT args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert asked["args_redacted"]["plan_id"] == "plan_named"  # the plan the call named, not an empty id


def test_after_the_plan_answers_and_a_code_nobody_asked_for_are_refused_by_the_tool(client, conn, walk_fake):
    walk = planned(client, conn)
    before = _count(conn)
    answers = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"objective": {"choice": "calls"}}, "idempotency_key": "p1"},
        key=walk["key"],
    )
    assert answers["isError"] and ALREADY_ANSWERED in tool_text(answers)
    never_asked = call_tool(
        client, "launch_marketing_plan", {"approval_code": "12-345", "idempotency_key": "p2"}, key=walk["key"]
    )
    assert never_asked["isError"] and NO_LAUNCH_REQUEST in tool_text(never_asked)
    assert _count(conn) == before
    # launching was asked for this version, then the plan changed: nothing waits for a code on the new version
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "p3"}, key=walk["key"])
    changed = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "p4"},
        key=walk["key"],
    )
    assert not asked["isError"] and not changed["isError"]
    before = _count(conn)
    stale = call_tool(
        client, "launch_marketing_plan", {"approval_code": "12-345", "idempotency_key": "p5"}, key=walk["key"]
    )
    assert stale["isError"] and NO_LAUNCH_REQUEST in tool_text(stale)
    assert _count(conn) == before


def test_an_unknown_caller_still_gets_the_commands_or_the_servers_refusal(client, conn):
    """The tool's own sentences are for a caller it knows. A key it cannot resolve under the tool's scope is refused by
    the command, in the command's words; a key the server does not know is refused before any tool runs."""
    business_with_key(conn, claim_state="verified")  # nothing begun: no consultation, no plan
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    cases = (
        ("update_marketing_plan", {"answers": {"objective": {"choice": "bookings"}}}),
        ("update_marketing_plan", {"message": "make it $1,500 a month"}),
        ("update_marketing_plan", {"changes": {"monthly_budget_usd": 1500}}),
        ("launch_marketing_plan", {"approval_code": "12-345"}),
    )
    for i, (tool, args) in enumerate(cases):
        res = call_tool(client, tool, {**args, "idempotency_key": f"u{i}"}, key=reader)
        assert res["isError"] and "lacks the 'manage' scope" in tool_text(res), (tool, args, tool_text(res))
    unknown = rpc_raw(
        client,
        "tools/call",
        {"name": "update_marketing_plan", "arguments": {"message": "hello", "idempotency_key": "u9"}},
        key="ak_unknown",
    )
    assert unknown.status_code == 401


def test_a_question_goes_to_admiry(client, conn, walk_fake):
    walk = planned(client, conn)
    walk_fake.answers_to["why this budget?"] = models.MailAnswer(
        text="It follows what a new patient is worth.", grounded=True
    )
    res = call_tool(client, "ask_admiry", {"question": "why this budget?"}, key=walk["key"])
    assert not res["isError"] and res["structuredContent"]["say"] == "It follows what a new patient is worth."
    assert _tools(conn)[-1] == ("ask", "ask_admiry")


def test_a_tool_finds_its_way_under_the_scope_it_needs(client, conn, walk_fake):
    """`where()` resolves the caller under the scope of the tool that asks (Task 9's review: under `read` alone, a
    key that may manage and not read was nowhere, and its turn went to a consultation that "does not exist"). A key
    that lacks the tool's own scope is still nowhere, and the command refuses it in its own words."""
    started(client, conn)
    manager, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("manage",), label="claude")
    turn = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "s1"},
        key=manager,
    )
    assert not turn["isError"], tool_text(turn)  # the open consultation was found without a session_id
    with auth.with_headers({"Authorization": f"Bearer {manager}"}):
        assert where().business_id is None and where(scope="manage").business_id == "biz_1"
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    with auth.with_headers({"Authorization": f"Bearer {reader}"}):
        assert where(scope="manage").business_id is None and where().business_id == "biz_1"
    refused = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "s2"},
        key=reader,
    )
    assert refused["isError"] and "lacks the 'manage' scope" in tool_text(refused)


def test_create_marketing_plan_is_not_marked_idempotent():
    """A repeat of create_marketing_plan starts over, so it does not tell an assistant a repeat is harmless; a tool
    registered without saying otherwise does."""
    by_name = {meta.name: meta.tool for meta in registry.SURFACE}
    assert by_name["create_marketing_plan"].annotations.idempotentHint is False
    assert by_name["get_marketing_plan"].annotations.idempotentHint is True
