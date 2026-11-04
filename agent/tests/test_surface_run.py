"""Running the ads through the public surface (spec 2026-10-01 §4.2; plan G1 Task 11): where they stand, stop, start
again, change what they may spend."""

from agent import keys, plans
from agent.surface import run as surface_run
from agent.surface.where import Where
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import ANSWERS, planned, settle_page, started
from agent.tests.test_ads_run import gads, launched, live, row  # noqa: F401 - gads and live are fixtures

REUSED = "idempotency_key already used for a different request; use a new key"

# What the budget is told when there is no plan to change yet, as the plan's own changes are
NO_PLAN = "there is no plan yet: begin a plan first"
QUESTIONS_FIRST = "there is no plan yet: the consultation's questions come first"
SEE_THE_PLAN_FIRST = "the questions are answered: show the plan first, then say what to change"
# The budget's own refusals, in its own arguments' names (adjust's would name monthly_budget_usd)
NOTHING_TO_CHANGE = "say what to change: monthly_usd, level, or channel with channel_monthly_usd"
OUT_OF_RANGE = "monthly_usd must be above 0 and at most 100,000"


def _count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]


def _last(conn) -> tuple:
    r = conn.execute("SELECT tool, surface_tool, args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    return r["tool"], r["surface_tool"], r["args_redacted"]


def test_campaigns_read_the_channels_card(client, conn, walk_fake):
    walk = planned(client, conn)
    res = call_tool(client, "get_campaigns", {}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert set(data(res)) == {"channels", "footer"} and res["structuredContent"]["say"] == "You have no ads yet."
    assert _last(conn)[:2] == ("get_channel_status", "get_campaigns")


def test_pause_and_resume_take_a_channel_and_default_to_all(client, conn, walk_fake):
    """Nothing runs for this business yet, so both are refused in the lifecycle's own words: what the test pins is
    that each public tool reached its command with the channel it was given (a refusal is audited too)."""
    walk = planned(client, conn)
    paused = call_tool(client, "pause_campaign", {"idempotency_key": "p1"}, key=walk["key"])
    assert paused["isError"] and "You have no ads yet; there is nothing to pause" in tool_text(paused)
    tool, public, args = _last(conn)
    assert (tool, public, args["channel"]) == ("pause", "pause_campaign", "all")
    resumed = call_tool(
        client, "resume_campaign", {"idempotency_key": "p2", "channel": "google_search"}, key=walk["key"]
    )
    assert resumed["isError"] and "Your ads are not paused" in tool_text(resumed)
    tool, public, args = _last(conn)
    assert (tool, public, args["channel"]) == ("resume", "resume_campaign", "google_search")


def test_pausing_running_ads_stops_them(client, conn, walk_fake):
    walk = planned(client, conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES ('biz_1', 'google_search', %s, 'live', '{\"campaign\": \"customers/1/campaigns/2\"}')",
        (walk["plan_id"],),
    )
    res = call_tool(client, "pause_campaign", {"idempotency_key": "p3"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert "google_search" in data(res)["channels"]
    assert conn.execute("SELECT state FROM channel_campaign").fetchone()["state"] == "paused"


def _refusal(conn) -> tuple:
    """The last row as a refusal of a reused key records it: command, public tool, ok, result, key."""
    r = conn.execute(
        "SELECT tool, surface_tool, ok, result, idempotency_key FROM agent_action ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return r["tool"], r["surface_tool"], r["ok"], r["result"], r["idempotency_key"]


def test_a_pause_sent_twice_pauses_once(client, conn, gads, live, strict_surface):  # noqa: F811 - fixtures
    """While the owner's pause stands, the same pause sent again is answered with its first answer: one pause."""
    key, _plan = launched(conn, gads)
    first = call_tool(client, "pause_campaign", {"idempotency_key": "p-1"}, key=key)
    again = call_tool(client, "pause_campaign", {"idempotency_key": "p-1"}, key=key)
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert row(conn)["state"] == "paused" and gads.kinds().count("pause") == 1


def test_a_pause_sent_again_after_a_resume_is_refused_and_a_new_key_pauses(client, conn, gads, live, strict_surface):  # noqa: F811 - fixtures
    """Final review A P1: paused with key K, resumed with another key, then K again within the window. The answer
    "paused" is not true any more: the key is refused as reused (recorded under the pause), nothing is paused, and the
    assistant's new key pauses the ads for real."""
    key, _plan = launched(conn, gads)
    assert not call_tool(client, "pause_campaign", {"idempotency_key": "p-1"}, key=key)["isError"]
    resumed = call_tool(client, "resume_campaign", {"idempotency_key": "r-1"}, key=key)
    assert not resumed["isError"] and row(conn)["state"] == "live", tool_text(resumed)
    pauses, before = gads.kinds().count("pause"), _count(conn)
    again = call_tool(client, "pause_campaign", {"idempotency_key": "p-1"}, key=key)
    assert again["isError"] and tool_text(again) == REUSED, tool_text(again)
    assert _count(conn) == before + 1 and _refusal(conn) == ("pause", "pause_campaign", False, {"error": REUSED}, None)
    assert row(conn)["state"] == "live" and gads.kinds().count("pause") == pauses  # nothing was paused
    fresh = call_tool(client, "pause_campaign", {"idempotency_key": "p-2"}, key=key)
    assert not fresh["isError"] and row(conn)["state"] == "paused", tool_text(fresh)


def test_a_resume_sent_again_is_answered_while_the_ads_run_and_refused_after_a_pause(
    client,
    conn,
    gads,  # noqa: F811 - fixture
    live,  # noqa: F811 - fixture
    strict_surface,
):
    """The mirror: the same resume sent again while nothing it names is under the owner's pause gets its first answer
    (one turn back on); once the owner paused again, the key is refused and nothing is turned on, and a new key
    resumes."""
    key, _plan = launched(conn, gads)
    assert not call_tool(client, "pause_campaign", {"idempotency_key": "p-1"}, key=key)["isError"]
    first = call_tool(client, "resume_campaign", {"idempotency_key": "r-1"}, key=key)
    enables = gads.kinds().count("enable")
    again = call_tool(client, "resume_campaign", {"idempotency_key": "r-1"}, key=key)
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert row(conn)["state"] == "live" and gads.kinds().count("enable") == enables
    assert not call_tool(client, "pause_campaign", {"idempotency_key": "p-2"}, key=key)["isError"]
    before = _count(conn)
    late = call_tool(client, "resume_campaign", {"idempotency_key": "r-1"}, key=key)
    assert late["isError"] and tool_text(late) == REUSED, tool_text(late)
    assert _count(conn) == before + 1
    assert _refusal(conn) == ("resume", "resume_campaign", False, {"error": REUSED}, None)
    assert row(conn)["state"] == "paused" and gads.kinds().count("enable") == enables  # nothing was turned on
    fresh = call_tool(client, "resume_campaign", {"idempotency_key": "r-2"}, key=key)
    assert not fresh["isError"] and row(conn)["state"] == "live", tool_text(fresh)


def test_a_pause_or_a_resume_is_held_against_the_rows_it_names():
    """A call that names one place is held against that place's row; "all" against every row the business has. A
    pause stands while the owner's pause is on every one of them (one at least: with no row nothing is paused); a
    resume stands while none of them is under the owner's pause."""
    paused, resumed = surface_run.still_paused, surface_run.still_resumed
    one_paused = Where(business_id="biz_1", channels=("google_search", "newsbreak"), owner_paused=("google_search",))
    answer = {"data": {}, "say": "Paused.", "next": {"kind": "none"}}
    places = ("google_search", "newsbreak", "all")
    assert [paused(one_paused, named)(answer) for named in places] == [True, False, False]
    assert [resumed(one_paused, named)(answer) for named in places] == [False, True, False]
    both = Where(
        business_id="biz_1", channels=("google_search", "newsbreak"), owner_paused=("google_search", "newsbreak")
    )
    assert paused(both, "all")(answer) and not resumed(both, "all")(answer)
    nothing = Where(business_id="biz_1")
    assert not paused(nothing, "all")(answer) and not paused(nothing, "google_search")(answer)


def test_the_budget_changes_the_latest_plan(client, conn, walk_fake):
    walk = planned(client, conn)
    settle_page(conn)  # the page comes first (the founder's ruling, 2026-10-13): then the ways and a change
    res = call_tool(client, "update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "b1"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert data(res)["cards"]["budget"]["monthly_usd"] == 1500 and data(res)["version"] == 2
    assert [a["tool"] for a in res["structuredContent"]["next"]["actions"]][-1] == "update_marketing_plan"
    assert _last(conn)[:2] == ("adjust", "update_marketing_budget")
    none = call_tool(client, "update_marketing_budget", {"idempotency_key": "b2"}, key=walk["key"])
    assert none["isError"] and "say what to change" in tool_text(none)


def test_the_budget_refuses_in_its_own_arguments_names(client, conn, walk_fake, strict_surface):
    """Final review C, minor 1: nothing to change, or a monthly amount out of range, is refused by the tool itself in
    the names of its own arguments (monthly_usd), never adjust's (monthly_budget_usd): no command runs and no row is
    written. The top of the range reaches adjust, which answers for it."""
    walk = planned(client, conn)
    before = _count(conn)
    for i, (args, sentence) in enumerate(
        (
            ({}, NOTHING_TO_CHANGE),
            ({"monthly_usd": 0}, OUT_OF_RANGE),
            ({"monthly_usd": -5}, OUT_OF_RANGE),
            ({"monthly_usd": 100_000.5}, OUT_OF_RANGE),
        )
    ):
        res = call_tool(client, "update_marketing_budget", {**args, "idempotency_key": f"r{i}"}, key=walk["key"])
        assert res["isError"] and tool_text(res) == sentence, (args, tool_text(res))
    assert _count(conn) == before
    call_tool(client, "update_marketing_budget", {"monthly_usd": 100_000, "idempotency_key": "r9"}, key=walk["key"])
    assert _count(conn) == before + 1 and _last(conn)[:2] == ("adjust", "update_marketing_budget")


def test_the_budget_of_a_business_with_no_plan_is_refused_in_the_tools_own_words(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(client, "update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "b3"}, key=key)
    assert res["isError"] and "there is no plan yet: begin a plan first" in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0  # no command ran


def test_the_budget_before_the_plan_is_told_what_comes_first(client, conn, walk_fake):
    """Final review B, minor: with no plan to change, the budget is told what comes first, as the plan's own changes
    are -- a plan begun when nothing is, the consultation's questions while it asks them, the plan once they are
    answered -- never "begin a plan first" while a consultation is under way (following it would start that over). No
    command runs."""
    budget = {"monthly_usd": 1500}
    nothing = business_with_key(conn, biz_id="biz_2", claim_state="claimed")
    walk = started(client, conn)
    before = _count(conn)
    for i, (key, sentence) in enumerate(((nothing, NO_PLAN), (walk["key"], QUESTIONS_FIRST))):
        res = call_tool(client, "update_marketing_budget", {**budget, "idempotency_key": f"q{i}"}, key=key)
        assert res["isError"] and sentence in tool_text(res), tool_text(res)
    assert _count(conn) == before
    for i, (qid, value) in enumerate(ANSWERS, 1):
        turn = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"a{i}"}, key=walk["key"]
        )
        assert not turn["isError"], tool_text(turn)
    before = _count(conn)
    answered = call_tool(client, "update_marketing_budget", {**budget, "idempotency_key": "q9"}, key=walk["key"])
    assert answered["isError"] and SEE_THE_PLAN_FIRST in tool_text(answered), tool_text(answered)
    assert _count(conn) == before


def test_the_budget_from_a_key_the_tool_cannot_resolve_gets_the_commands_refusal(client, conn):
    """The other side of the check above: a key without the manage scope is no caller the tool knows, so adjust
    refuses it in its own words and records the attempt under both names."""
    business_with_key(conn, claim_state="claimed")
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    res = call_tool(client, "update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "b4"}, key=reader)
    assert res["isError"] and "lacks the 'manage' scope" in tool_text(res)
    assert _last(conn)[:2] == ("adjust", "update_marketing_budget")


def test_a_channels_row_names_the_plan_version_it_runs_and_that_version_shows_its_ads(client, conn, walk_fake):
    """ "What ads am I running?": the row says which version of the plan runs there, and reading that version shows
    its ad text and its searches -- also once a newer version exists that has not been launched."""
    walk = planned(client, conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES ('biz_1', 'google_search', %s, 'live', '{\"campaign\": \"customers/1/campaigns/2\"}')",
        (walk["plan_id"],),
    )
    newer = call_tool(
        client, "update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "v2"}, key=walk["key"]
    )
    assert not newer["isError"] and data(newer)["version"] == 2
    row = data(call_tool(client, "get_campaigns", {}, key=walk["key"]))["channels"][0]
    assert (row["channel"], row["plan_id"], row["plan_version"]) == ("google_search", walk["plan_id"], 1)
    running = call_tool(client, "get_marketing_plan", {"plan_id": row["plan_id"]}, key=walk["key"])
    assert not running["isError"], tool_text(running)
    shown = data(running)
    assert shown["version"] == 1 and shown["cards"]["ads_preview"]["headlines"]
    assert (
        shown["ads"] == plans.get(conn, "biz_1", walk["plan_id"])["sections"]["ads"]
    )  # the text as the running version holds it
    there = next(c for c in shown["ads"]["channels"] if c["channel"] == "google_search")
    assert there["headlines"] and there["themes"]  # the ad text and the searches, as the running version holds them
    now = data(call_tool(client, "get_marketing_state", {}, key=walk["key"]))
    assert now["plan"]["version"] == 2 and now["channels"][0]["plan_version"] == 1
