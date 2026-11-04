"""confirm_proposal's approval hook (spec 2026-09-28 admiry-google-search-execution-design §5 step 1; plan P6; A3
Task 4): once the approval commits, the tool launches the plan's Google Search ads in the same request and its
envelope carries where they are -- `execution.ads` for the assistant, one sentence for the owner."""

from __future__ import annotations

from agent import ads_run, plans, proposals
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.surface_helpers import planned
from agent.tests.test_ads_run import BIZ, approve, gads, live, plain, row, seed  # noqa: F401 - fixtures


def test_confirming_a_plan_answers_where_its_ads_are(client, conn, gads, live):  # noqa: F811 - the fixtures imported above
    key, plan_id = seed(conn, status="draft")
    envelope = approve(client, conn, key, plan_id)
    data = envelope["data"]
    assert (data["status"], data["plan_status"], data["plan_id"]) == ("confirmed", "approved", plan_id)
    assert data["execution"] == {
        "presence": "queued_for_operator",
        "discovery": "queued_for_operator",
        "ads": "live",  # the launch's outcome, not the approval's "queued"
        "creative": "queued_for_operator",
        "conversion": "queued_for_operator",
        "optimization": "on",
        "google_search": "live",  # M0: the channel's own entry, which "ads" mirrors for one release
    }
    assert envelope["say"] == "Confirmed. Google Search: live." and plain(envelope["say"])
    assert row(conn)["state"] == "live" and gads.kinds() == ["lookup", "create", "enable"]
    stored = conn.execute("SELECT result FROM agent_action WHERE tool = 'confirm_proposal' AND ok").fetchone()
    assert stored["result"]["data"]["execution"]["ads"] == "queued"  # the approval's own record
    assert stored["result"]["data"]["execution"]["google_search"] == "queued"


def test_with_the_switch_off_the_answer_says_so_and_nothing_is_called(client, conn, gads):  # noqa: F811 - the fixtures imported above
    key, plan_id = seed(conn, status="draft", link=None, page=False)  # no precheck runs with the switch off
    envelope = approve(client, conn, key, plan_id)
    assert envelope["data"]["execution"]["ads"] == "off"
    assert envelope["say"] == f"Confirmed. {ads_run.OFF}" and plain(envelope["say"])
    assert row(conn) is None and gads.requests == []


def test_a_replayed_confirmation_answers_as_before_and_does_not_launch_again(client, conn, gads, live):  # noqa: F811 - the fixtures imported above
    key, plan_id = seed(conn, status="draft")
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    args = {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"}
    first = call_tool(client, "confirm_proposal", args, key=key)["structuredContent"]
    sent = len(gads.requests)
    again = call_tool(client, "confirm_proposal", args, key=key)["structuredContent"]
    assert first["data"]["execution"]["ads"] == "live"
    assert again["data"]["execution"]["ads"] == "queued"  # the stored answer of the approval itself
    assert len(gads.requests) == sent  # a replay changes nothing, in Google either


def test_a_second_ask_answers_with_the_request_the_owner_already_has(client, conn, walk_fake):
    """Final review B, Important 1 (A m4): the owner's page gave a code for the request that was open when the owner
    looked. Asking again with a new key answers with that same request -- one request open, never a second one beside
    it -- so the owner's code still launches the plan."""
    walk = planned(client, conn)
    ask = {"plan_id": walk["plan_id"]}
    first = call_tool(client, "approve_plan", {**ask, "idempotency_key": "ap-1"}, key=walk["key"])
    assert not first["isError"], tool_text(first)
    pid = data(first)["proposal_id"]
    code = proposals.issue_code(conn, pid, via="plan_page")  # the owner reads it on their page
    again = call_tool(client, "approve_plan", {**ask, "idempotency_key": "ap-2"}, key=walk["key"])
    assert not again["isError"], tool_text(again)
    assert (data(again)["status"], data(again)["proposal_id"]) == ("proposed", pid)
    assert [p["proposal_id"] for p in proposals.list_open(conn, BIZ) if p["kind"] == "approve_plan"] == [pid]
    launched = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"]
    )
    assert not launched["isError"], tool_text(launched)
    assert plans.latest(conn, BIZ)["status"] == "approved"


def test_a_version_a_change_replaced_is_refused_and_the_new_one_gets_its_own_request(client, conn, walk_fake):
    """An open request of a version a change replaced is never given again: asking for that version is refused as
    before, and the new version's ask makes its own request."""
    walk = planned(client, conn)
    first = call_tool(client, "approve_plan", {"plan_id": walk["plan_id"], "idempotency_key": "ap-1"}, key=walk["key"])
    assert not first["isError"], tool_text(first)
    changed = call_tool(
        client, "update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "b1"}, key=walk["key"]
    )
    assert not changed["isError"], tool_text(changed)
    old = call_tool(client, "approve_plan", {"plan_id": walk["plan_id"], "idempotency_key": "ap-2"}, key=walk["key"])
    assert old["isError"] and "this plan is superseded; create or revise a plan first" in tool_text(old)
    new = call_tool(
        client, "approve_plan", {"plan_id": data(changed)["plan_id"], "idempotency_key": "ap-3"}, key=walk["key"]
    )
    assert not new["isError"], tool_text(new)
    assert data(new)["proposal_id"] != data(first)["proposal_id"] and data(new)["summary"].startswith("Launch plan v2")
