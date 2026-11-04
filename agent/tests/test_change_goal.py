"""change_goal (spec 2026-10-01 D162, rev. 2; plan G1 Task 17): a change names only what changes; the rest of the
goal stays, a limit Admiry set for the plan included."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import business, db, plans, proposals, tools_business
from agent.channels import registry
from agent.tests.conftest import JARGON, TEST_DB_URL, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import ANSWERS, WALK_TEXT, planned, started
from agent.tests.test_ads_run import add_plan, sections

LIMIT_RANGE = "monthly_limit_usd must be above 0 and at most 100,000; null removes the limit"


def _change(client, key, changes, ik="cg-1"):
    return call_tool(client, "change_goal", {"changes": changes, "idempotency_key": ik}, key=key)


def _below_the_plan(plan: float, limit: float) -> str:
    """The refusal of a monthly limit below what the plan spends, both amounts as the owner reads them."""
    return (
        f"the plan spends {plans.usd(plan)} a month, more than a {plans.usd(limit)} limit: lower the plan's budget "
        "first (and launch that version if ads are running), then set the limit"
    )


def test_a_change_names_only_what_changes_and_the_rest_stays(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(
        conn,
        "biz_1",
        target=20,
        monthly_limit_usd=2000,
        focus_services=["Invisalign"],
        channel_caps={"newsbreak": 200.0},
    )
    res = _change(client, key, {"max_cost_per_customer_usd": 80})
    assert not res["isError"], tool_text(res)
    goal = data(res)["goal"]
    assert (goal["target"], goal["monthly_limit_usd"], goal["limit_source"]) == (20, 2000.0, "owner")
    assert goal["max_cost_per_customer_usd"] == 80.0 and goal["focus_services"] == ["Invisalign"]
    assert goal["channel_caps"] == {"newsbreak": 200.0}
    say = res["structuredContent"]["say"]
    assert say == (
        "The goal now reads: 20 new customers a month, at most $80 for each new customer, no more than $2,000 a month, "
        "no more than $200 a month on NewsBreak."
    )
    assert not JARGON.search(say)
    later = _change(client, key, {"target": 30}, ik="cg-2")  # the ceiling is not named: it stays
    assert (data(later)["goal"]["target"], data(later)["goal"]["max_cost_per_customer_usd"]) == (30, 80.0)


def test_a_limit_admiry_set_for_the_plan_survives_a_change_to_the_goal(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", monthly_limit_usd=3600, limit_source="system")
    res = _change(client, key, {"max_cost_per_customer_usd": 80})  # no target, no budget of the owner's: still a change
    assert not res["isError"], tool_text(res)
    goal = business.latest_goal(conn, "biz_1")
    assert (goal["monthly_limit_usd"], goal["limit_source"]) == (3600.0, "system")
    assert goal["max_cost_per_customer_usd"] == 80.0
    assert res["structuredContent"]["say"] == "The goal now reads: at most $80 for each new customer."


def test_the_owner_states_a_limit_and_it_is_theirs_from_then_on(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20, monthly_limit_usd=3600, limit_source="system")
    goal = data(_change(client, key, {"monthly_limit_usd": 2500}))["goal"]
    assert (goal["monthly_limit_usd"], goal["limit_source"], goal["target"]) == (2500.0, "owner", 20)


def test_null_removes_a_target_or_a_ceiling_and_an_empty_object_clears_the_caps(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(
        conn,
        "biz_1",
        target=20,
        monthly_limit_usd=2000,
        channel_caps={"newsbreak": 200.0},
        max_cost_per_customer_usd=80,
    )
    res = _change(client, key, {"target": None, "max_cost_per_customer_usd": None, "channel_caps": {}})
    goal = data(res)["goal"]
    assert (goal["target"], goal["max_cost_per_customer_usd"], goal["channel_caps"]) == (None, None, {})
    assert goal["monthly_limit_usd"] == 2000.0
    assert res["structuredContent"]["say"] == "The goal now reads: no more than $2,000 a month."


def test_a_business_with_no_goal_yet_gets_one(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    res = _change(client, key, {"goal_type": "calls", "target": 40})
    goal = data(res)["goal"]
    assert (goal["goal_type"], goal["target"], goal["autopilot"], goal["monthly_limit_usd"]) == (
        "calls",
        40,
        "safe",
        None,
    )
    assert res["structuredContent"]["say"] == "The goal now reads: 40 calls a month."


def test_a_goal_with_nothing_of_the_owners_left_says_so_and_keeps_admirys_limit(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20, monthly_limit_usd=3600, limit_source="system")
    res = _change(client, key, {"target": None})
    assert not res["isError"], tool_text(res)
    say = res["structuredContent"]["say"]
    assert say == "The goal has no target and no limit of yours now."
    assert not JARGON.search(say)
    assert business.latest_goal(conn, "biz_1")["monthly_limit_usd"] == 3600.0  # the limit Admiry set still stands


@pytest.mark.parametrize(
    ("changes", "text"),
    [
        ({}, "changes must name at least one of"),
        ({"colour": "red"}, "unknown goal fields: ['colour']"),
        ({"goal_type": "fame"}, "goal_type must be customers, calls or bookings"),
        ({"goal_type": ["customers"]}, "goal_type must be customers, calls or bookings"),
        ({"target": 0}, "target must be a whole number between 1 and 10,000"),
        ({"target": 12.5}, "target must be a whole number between 1 and 10,000"),
        ({"target": True}, "target must be a whole number between 1 and 10,000"),
        ({"target": False}, "target must be a whole number between 1 and 10,000"),
        ({"monthly_limit_usd": 200000}, LIMIT_RANGE),
        ({"monthly_limit_usd": True}, LIMIT_RANGE),
        ({"monthly_limit_usd": 0}, LIMIT_RANGE),  # build and recommend read 0 as no limit: never "no more than $0"
        ({"monthly_limit_usd": -5}, LIMIT_RANGE),
        ({"max_cost_per_customer_usd": 0.5}, "max_cost_per_customer_usd must be between 1 and 100,000"),
        ({"max_cost_per_customer_usd": True}, "max_cost_per_customer_usd must be between 1 and 100,000"),
        ({"focus_services": "Invisalign"}, "focus_services: a list of at most"),
        ({"autopilot": "all"}, "autopilot must be off or safe"),
        ({"channel_caps": {"fax": 5}}, "unknown channel"),
        ({"channel_caps": [200]}, "channel_caps must be an object"),
    ],
)
def test_a_bad_change_is_refused_and_no_goal_is_written(client, conn, changes, text):
    key = business_with_key(conn, claim_state="claimed")
    res = _change(client, key, changes)
    assert res["isError"] and text in tool_text(res), tool_text(res)
    assert business.latest_goal(conn, "biz_1") is None


def test_a_read_only_key_cannot_change_the_goal(client, conn):
    key = business_with_key(conn, scopes=("read",), claim_state="claimed")
    res = _change(client, key, {"target": 30})
    assert res["isError"] and "lacks the 'manage' scope" in tool_text(res)


def test_only_what_a_change_names_is_checked_a_held_autopilot_passes_through(client, conn):
    """A goal may hold what a later feature writes (autopilot `all`): a change that does not name it keeps it, and
    naming it is still refused."""
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20, autopilot="all")
    res = _change(client, key, {"target": 30})
    assert not res["isError"], tool_text(res)
    assert (data(res)["goal"]["target"], data(res)["goal"]["autopilot"]) == (30, "all")
    refused = _change(client, key, {"autopilot": "all"}, ik="cg-2")
    assert refused["isError"] and "autopilot must be off or safe" in tool_text(refused)
    assert business.latest_goal(conn, "biz_1")["id"] == data(res)["goal"]["id"]  # the refusal wrote no goal


def test_a_held_value_passes_through_even_where_naming_it_would_be_refused(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    focus = [f"Service {n}" for n in range(business.FOCUS_MAX_ITEMS + 2)]
    business.set_goal(conn, "biz_1", goal_type="leads", target=20_000, focus_services=focus)
    res = _change(client, key, {"max_cost_per_customer_usd": 80})
    assert not res["isError"], tool_text(res)
    goal = data(res)["goal"]
    assert (goal["goal_type"], goal["target"], goal["focus_services"]) == ("leads", 20_000, focus)
    assert res["structuredContent"]["say"] == (
        "The goal now reads: 20,000 leads a month, at most $80 for each new customer."
    )


def test_a_whole_number_written_as_a_float_is_a_whole_number(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    res = _change(client, key, {"target": 20.0})
    assert not res["isError"], tool_text(res)
    assert data(res)["goal"]["target"] == 20 and isinstance(data(res)["goal"]["target"], int)
    assert res["structuredContent"]["say"] == "The goal now reads: 20 new customers a month."
    assert conn.execute("SELECT target FROM goal WHERE business_id = 'biz_1'").fetchone()["target"] == 20


def _waits_for_the_lock(conn, call) -> bool:
    """True once `call` waits on an advisory lock of this database; False if it finished without waiting."""
    deadline = time.monotonic() + 10
    while not call.done() and time.monotonic() < deadline:
        waiting = conn.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ).fetchone()["n"]
        if waiting:
            return True
        time.sleep(0.02)
    return False


def test_two_patches_at_once_do_not_lose_one(client, conn):
    """One change at a time per business: a patch in flight holds the goal's lock; the next waits for it and then reads
    the goal the first wrote, so its change lands on top of the first's instead of replacing it."""
    key = business_with_key(conn, claim_state="claimed")
    with ThreadPoolExecutor(max_workers=1) as pool, db.connect(TEST_DB_URL) as first, first.transaction():
        first.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goal:biz_1",))  # the first patch, in flight
        second = pool.submit(_change, client, key, {"max_cost_per_customer_usd": 80})
        assert _waits_for_the_lock(conn, second), "the second patch did not wait for the first"
        business.set_goal(first, "biz_1", target=20, monthly_limit_usd=2000)  # what the first patch writes
    res = second.result(timeout=10)  # the first transaction has committed and released the lock
    assert not res["isError"], tool_text(res)
    goal = business.latest_goal(conn, "biz_1")
    assert (goal["target"], goal["monthly_limit_usd"], goal["max_cost_per_customer_usd"]) == (20, 2000.0, 80.0)
    assert goal["id"] == data(res)["goal"]["id"]


def test_the_patch_that_wrote_last_is_the_goal_even_if_its_transaction_began_first(client, conn, monkeypatch):
    """The newest goal row counts, and a goal is dated when it is written, not from the start of its transaction: a
    patch that began first but took the lock second wrote the goal with both changes in it, and it must sort last."""
    key = business_with_key(conn, claim_state="claimed")
    began, release = threading.Event(), threading.Event()
    real = tools_business.need_business

    def hold_the_first_call(p):  # the call's transaction has begun; the first call waits here, before it takes the lock
        if not began.is_set():
            began.set()
            release.wait(10)
        return real(p)

    monkeypatch.setattr(tools_business, "need_business", hold_the_first_call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(_change, client, key, {"max_cost_per_customer_usd": 80}, "cg-slow")
        try:
            assert began.wait(10)
            fast = pool.submit(_change, client, key, {"target": 20}, "cg-fast")  # begins later, takes the lock first
            assert not fast.result(timeout=10)["isError"]
        finally:
            release.set()
        assert not slow.result(timeout=10)["isError"]
    goal = business.latest_goal(conn, "biz_1")
    assert (goal["target"], goal["max_cost_per_customer_usd"]) == (20, 80.0)


def test_a_change_to_the_caps_alone_is_read_back_as_the_cap_not_as_an_empty_goal(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", monthly_limit_usd=3600, limit_source="system")  # a limit Admiry set: not read back
    res = _change(client, key, {"channel_caps": {"newsbreak": 200}})
    assert not res["isError"], tool_text(res)
    say = res["structuredContent"]["say"]
    assert say == "The goal now reads: no more than $200 a month on NewsBreak."
    assert not JARGON.search(say)


def test_turning_the_automatic_changes_off_is_confirmed_and_the_default_says_nothing(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20)
    off = _change(client, key, {"autopilot": "off"})["structuredContent"]["say"]
    assert off == "The goal now reads: 20 new customers a month, no automatic changes."
    safe = _change(client, key, {"autopilot": "safe"}, ik="cg-2")["structuredContent"]["say"]
    assert safe == "The goal now reads: 20 new customers a month."
    bare = _change(client, key, {"target": None, "autopilot": "off"}, ik="cg-3")["structuredContent"]["say"]
    assert bare == "The goal now reads: no automatic changes."
    assert not any(JARGON.search(say) for say in (off, safe, bare))


def test_the_sentence_reads_back_every_part_in_order_and_leaves_out_a_place_it_does_not_know(client, conn):
    """The target (with its thousands), what a customer may cost, the owner's monthly limit, each cap in the order the
    plan's cards show the places, then the automatic changes. A cap for a place this code does not name is not said,
    and is not an error."""
    key = business_with_key(conn, claim_state="claimed")
    caps = {"meta": 50.0, "newsbreak": 200.0, "fax": 5.0, "google_search": 1250.5}
    business.set_goal(
        conn,
        "biz_1",
        target=1_500,
        monthly_limit_usd=2000,
        channel_caps=caps,
        autopilot="off",
        max_cost_per_customer_usd=80,
    )
    res = _change(client, key, {"target": 2_500})
    assert not res["isError"], tool_text(res)
    say = res["structuredContent"]["say"]
    assert say == (
        "The goal now reads: 2,500 new customers a month, at most $80 for each new customer, "
        "no more than $2,000 a month, no more than $1,250.50 a month on Google Search, "
        "no more than $200 a month on NewsBreak, no more than $50 a month on Facebook & Instagram, "
        "no automatic changes."
    )
    assert not JARGON.search(say)
    assert data(res)["goal"]["channel_caps"] == caps  # held, so kept as it is, the unknown one included
    # no name a cap can be read back under is jargon
    assert not [c for c in registry.ORDER if JARGON.search(registry.label(c))]


def test_null_removes_the_monthly_limit_even_one_admiry_set(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20, monthly_limit_usd=3600, limit_source="system")
    goal = data(_change(client, key, {"monthly_limit_usd": None}))["goal"]
    assert (goal["monthly_limit_usd"], goal["limit_source"], goal["target"]) == (None, "owner", 20)


def test_a_limit_below_the_draft_plan_is_refused_and_the_goal_stays(client, conn):
    """Final review B, Important 7: a monthly limit below what the plan spends would be read back as a limit the plan
    breaks, and every later change would be refused against it."""
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20, monthly_limit_usd=3000)
    add_plan(conn, sections(monthly_usd=2000.0), status="draft")
    held = business.latest_goal(conn, "biz_1")
    res = _change(client, key, {"monthly_limit_usd": 1500})
    assert res["isError"] and _below_the_plan(2000, 1500) in tool_text(res), tool_text(res)
    assert business.latest_goal(conn, "biz_1")["id"] == held["id"]  # no goal was written


def test_a_limit_below_the_approved_plan_is_refused_while_a_cheaper_version_waits(client, conn):
    """The ads spend what the approved version says until a cheaper one is launched: the larger of the two counts."""
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", monthly_limit_usd=3000)
    add_plan(conn, sections(monthly_usd=2000.0), status="approved")
    add_plan(conn, sections(monthly_usd=1000.0), status="draft")
    res = _change(client, key, {"monthly_limit_usd": 1500})
    assert res["isError"] and _below_the_plan(2000, 1500) in tool_text(res), tool_text(res)
    assert business.latest_goal(conn, "biz_1")["monthly_limit_usd"] == 3000.0


def test_a_limit_equal_to_what_the_plan_spends_is_set_and_null_still_removes_it(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", monthly_limit_usd=3000)
    add_plan(conn, sections(monthly_usd=2000.0), status="approved")
    add_plan(conn, sections(monthly_usd=1000.0), status="draft")
    equal = _change(client, key, {"monthly_limit_usd": 2000})
    assert not equal["isError"], tool_text(equal)
    assert (data(equal)["goal"]["monthly_limit_usd"], data(equal)["goal"]["limit_source"]) == (2000.0, "owner")
    removed = _change(client, key, {"monthly_limit_usd": None}, ik="cg-2")
    assert not removed["isError"], tool_text(removed)
    assert data(removed)["goal"]["monthly_limit_usd"] is None


def test_a_business_with_no_plan_takes_any_limit_in_range(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    for i, limit in enumerate((1, 100_000)):
        res = _change(client, key, {"monthly_limit_usd": limit}, ik=f"cg-{i}")
        assert not res["isError"], tool_text(res)
        assert data(res)["goal"]["monthly_limit_usd"] == float(limit)


def test_a_limit_below_the_launched_plan_is_refused_through_the_public_tool(client, conn, walk_fake, strict_surface):
    """B's reproduction: the plan launched with the owner's code, then a monthly limit below what it spends sent as a
    change of the goal. Refused in the same words, and the goal keeps the limit it had."""
    walk = planned(client, conn)
    prop = proposals.propose_plan(conn, "biz_1", walk["plan_id"])
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    go = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"])
    assert not go["isError"] and plans.latest(conn, "biz_1")["status"] == "approved", tool_text(go)
    spends = plans.total_usd(plans.latest(conn, "biz_1")["sections"])
    assert spends > 900
    held = business.latest_goal(conn, "biz_1")
    res = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_limit_usd": 900}, "idempotency_key": "g1"},
        key=walk["key"],
    )
    assert res["isError"] and _below_the_plan(spends, 900) in tool_text(res), tool_text(res)
    assert business.latest_goal(conn, "biz_1")["id"] == held["id"]


def test_null_for_the_caps_leaves_them_as_they_are(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(conn, "biz_1", target=20, channel_caps={"newsbreak": 200.0})
    res = _change(client, key, {"channel_caps": None})
    assert not res["isError"], tool_text(res)
    assert data(res)["goal"]["channel_caps"] == {"newsbreak": 200.0}


def test_a_named_focus_or_goal_type_changes_and_the_rest_stays(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    business.set_goal(
        conn,
        "biz_1",
        target=20,
        monthly_limit_usd=2000,
        focus_services=["Invisalign"],
        channel_caps={"newsbreak": 200.0},
        max_cost_per_customer_usd=80,
        autopilot="off",
    )
    rest = ("target", "monthly_limit_usd", "max_cost_per_customer_usd", "channel_caps", "autopilot")
    focus = data(_change(client, key, {"focus_services": ["Whitening", "Implants"]}))["goal"]
    assert (focus["focus_services"], focus["goal_type"]) == (["Whitening", "Implants"], "customers")
    assert [focus[k] for k in rest] == [20, 2000.0, 80.0, {"newsbreak": 200.0}, "off"]
    kind = data(_change(client, key, {"goal_type": "bookings"}, ik="cg-2"))["goal"]
    assert (kind["goal_type"], kind["focus_services"]) == ("bookings", ["Whitening", "Implants"])
    assert [kind[k] for k in rest] == [20, 2000.0, 80.0, {"newsbreak": 200.0}, "off"]


def test_recommends_ceiling_keeps_the_owners_goal_type_and_autopilot(client, conn, walk_fake):
    """The owner sets both before the plan is built and names no monthly limit; recommend writes its own ceiling
    (a goal written for another reason) and must not turn calls back into customers or off back into safe."""
    walk_fake.knowledge.goal_hint.monthly_budget_usd = None  # the owner names no monthly limit of their own
    walk = started(client, conn)
    set_both = _change(client, walk["key"], {"goal_type": "calls", "autopilot": "off"})
    assert not set_both["isError"], tool_text(set_both)
    for i, (qid, value) in enumerate(ANSWERS, 1):
        answered = call_tool(
            client,
            "answer",
            {"session_id": walk["session_id"], "answers": {qid: value}, "idempotency_key": f"w-a{i}"},
            key=walk["key"],
        )
        assert not answered["isError"], tool_text(answered)
    rec = call_tool(client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "w-r"}, key=walk["key"])
    assert not rec["isError"], tool_text(rec)
    goal = business.latest_goal(conn, "biz_1")
    assert goal["limit_source"] == "system" and goal["id"] != data(set_both)["goal"]["id"]  # recommend wrote this one
    assert (goal["goal_type"], goal["autopilot"]) == ("calls", "off")


def test_a_start_that_states_a_goal_keeps_the_owners_goal_type_and_autopilot(client, conn, walk_fake):
    walk = started(client, conn)
    set_both = _change(client, walk["key"], {"goal_type": "calls", "autopilot": "off"})
    assert not set_both["isError"], tool_text(set_both)
    again = call_tool(client, "start", {"text": WALK_TEXT}, key=walk["key"])
    assert not again["isError"], tool_text(again)
    goal = business.latest_goal(conn, "biz_1")
    assert goal["id"] != data(set_both)["goal"]["id"]  # start wrote a goal of its own from the words
    assert (goal["goal_type"], goal["autopilot"]) == ("calls", "off")
