"""L1 Task 7 (spec 2026-10-10 §5, §6.1, §6.3; plan D5, D6): with AGENT_LAUNCH_REVIEW on, the owner's yes to a business's
first launch -- the plan card's, the plan page's, an approval code -- puts the plan in review and launches nothing; a
business with an approved plan approves a newer version as before; the owner and the assistant read where it stands."""

import re

import pytest

from agent import ads_run, agreements, config, envelope, launch_check, launch_review, plans, proposals, setup
from agent.tests.conftest import call_tool, tool_text
from agent.tests.test_ads_run import BIZ, add_plan, seed
from agent.tests.test_review_launch import OWNER, draft_sections


@pytest.fixture
def review_on(monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")


@pytest.fixture
def launched(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **k: calls.append(biz) or {"channels": {}, "say": "Live."})
    return calls


def _ready_with_key(conn) -> tuple[str, str]:
    key, _plan_id = seed(conn, status="draft", secs=draft_sections())
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    return key, plans.latest(conn, BIZ)["id"]


def _ready(conn) -> str:
    return _ready_with_key(conn)[1]


def test_the_plan_cards_yes_sends_a_first_launch_to_review(conn, review_on, launched):
    plan_id = _ready(conn)
    out = setup.approve(config.load(), setup.Owner(**OWNER), BIZ, plan_id)
    assert out["approved"]["plan_status"] == "in_review" and out["launch"] is None and launched == []
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW
    assert out["state"]["in_review"] == launch_review.IN_REVIEW_SAY
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0
    row = conn.execute("SELECT approval_kind FROM agent_action WHERE tool = 'web.approve_plan'").fetchone()
    assert row["approval_kind"] == "web"


def test_a_plan_in_review_is_not_approved_twice(conn, review_on, launched):
    """L1 review I1a: after the first yes the proposal is confirmed, so a second approve_on_web proposes again --
    and now propose_plan's own IN_REVIEW check (not its generic "create or revise") refuses it first, the same
    sentence precheck_approve_plan, asked directly of the plan, refuses it with. Both refusals stay in the code."""
    plan_id = _ready(conn)
    setup.approve(config.load(), setup.Owner(**OWNER), BIZ, plan_id)
    with pytest.raises(proposals.ToolError, match=proposals.IN_REVIEW_REFUSAL):
        proposals.approve_on_web(conn, BIZ, plan_id, email="owner@example.com", ip=None)
    with pytest.raises(proposals.ToolError, match=proposals.IN_REVIEW_REFUSAL):
        proposals.precheck_approve_plan(conn, BIZ, {"plan_id": plan_id})


def test_with_an_approved_plan_a_newer_version_is_approved_at_the_yes(conn, review_on, launched):
    seed(conn, status="approved", secs=draft_sections())
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    plan_id = add_plan(conn, draft_sections(monthly_usd=1200.0), status="draft")
    out = setup.approve(config.load(), setup.Owner(**OWNER), BIZ, plan_id)
    assert out["approved"]["plan_status"] == "approved" and launched == [BIZ]


def test_with_the_review_off_the_yes_approves_and_launches(conn, monkeypatch, launched):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    plan_id = _ready(conn)
    out = setup.approve(config.load(), setup.Owner(**OWNER), BIZ, plan_id)
    assert out["approved"]["plan_status"] == "approved" and launched == [BIZ]


def test_an_approval_code_sends_it_to_review_too(client, conn, review_on, launched):
    key, plan_id = seed(conn, status="draft", secs=draft_sections())
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    assert launch_review.IN_REVIEW_SAY in tool_text(res) and launched == []
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW


def test_a_returned_version_lists_the_admins_note_first_and_does_not_block(conn, review_on, launched):
    """R15 c: the note stays the list's first item, but a RETURNED version -- unlike a rejected one, below -- does
    not block: what it asks for may be outside the plan itself (the owner's own page, say), and agreeing again
    sends it back to review (Task 9's Reviewer holds a version a person returned before)."""
    plan_id = _ready(conn)
    out = setup.approve(config.load(), setup.Owner(**OWNER), BIZ, plan_id)
    launch_review.decide(
        conn, out["approved"]["review_id"], state="returned", by="admin:founder@example.com", note="Add an end date"
    )
    found = launch_check.check(conn, config.load(), BIZ)
    first = found["items"][0]
    assert (first["id"], first["kind"], first["blocking"]) == ("review_note", "note", False)
    assert first["text"] == launch_check.note_text("Add an end date") and found["ready"] is True


def test_a_rejected_version_refuses_until_it_changes(client, conn, review_on, launched):
    """R15 b: a REJECTED version must never go back to review unchanged on any yes path -- the plan page, a code, a
    payment and the assistant all run through proposals.precheck_approve_plan, which is where this refusal lives --
    launch_check lists the note first and blocking (unlike a returned one, above), get_marketing_state's `say`
    carries it too, and a new version clears it."""
    key, plan_id = _ready_with_key(conn)
    out = setup.approve(config.load(), setup.Owner(**OWNER), BIZ, plan_id)
    launch_review.decide(
        conn, out["approved"]["review_id"], state="rejected", by="admin:founder@example.com", note="Not for this trade"
    )
    note = launch_check.note_text("Not for this trade")
    with pytest.raises(proposals.ToolError, match=re.escape(note)):
        proposals.approve_on_web(conn, BIZ, plan_id, email="owner@example.com", ip=None)
    found = launch_check.check(conn, config.load(), BIZ)
    first = found["items"][0]
    assert (first["id"], first["kind"], first["blocking"]) == ("review_note", "note", True)
    assert first["text"] == note and found["ready"] is False
    state = call_tool(client, "get_marketing_state", {}, key=key)
    assert "It cannot launch yet:" in tool_text(state) and "Not for this trade" in tool_text(state)
    plans.create_version(conn, BIZ, goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[])
    cleared = launch_check.check(conn, config.load(), BIZ)
    assert not any(i["id"] == "review_note" for i in cleared["items"])
    proposals.precheck_approve_plan(conn, BIZ, {"plan_id": plans.latest(conn, BIZ)["id"]})  # lets the new one through


def test_the_assistant_reads_in_review(client, conn, review_on, launched):
    key, plan_id = seed(conn, status="draft", secs=draft_sections())
    launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    res = call_tool(client, "get_marketing_state", {}, key=key)
    assert launch_review.IN_REVIEW_SAY in tool_text(res)
    assert res["structuredContent"]["next"] == envelope.NONE  # nothing to call meanwhile (L1 review item 11)


def test_the_contract_lists_in_review():
    assert plans.STATUSES == ("draft", "in_review", "approved", "superseded")
