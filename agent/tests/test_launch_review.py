"""L1 Task 6 (spec 2026-10-10 §6.1, §9): the switches, a plan in review, and its review row -- opened by the owner's
yes, withdrawn by a new version, decided once."""

import pytest
from fastmcp.exceptions import ToolError

from agent import config, launch_review, plans
from agent.tests.test_ads_run import BIZ, seed
from agent.tests.test_review_launch import draft_sections


def test_the_switch_and_the_admin_list_are_read_as_the_other_switches(monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "ON")
    assert config.load().launch_review is True
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "")
    assert config.load().launch_review is False
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "yes")
    with pytest.raises(ValueError, match="AGENT_LAUNCH_REVIEW"):
        config.load()
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "off")
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", " Founder@Example.com , ops@example.com,")
    assert config.load().admin_emails == frozenset({"founder@example.com", "ops@example.com"})
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", "founder")
    with pytest.raises(ValueError, match="AGENT_ADMIN_EMAILS"):
        config.load()


def _submitted(conn) -> tuple[str, str]:
    seed(conn, status="draft", secs=draft_sections())
    plan_id = plans.latest(conn, BIZ)["id"]
    return plan_id, launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web", "ref": "pr_1"})["review_id"]


def test_the_owners_yes_puts_the_draft_in_review_opens_its_row_and_queues_the_reviewer(conn):
    plan_id, review_id = _submitted(conn)
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW
    row = launch_review.get(conn, review_id)
    assert (row["state"], row["approval"], row["steps"], row["plan_id"]) == (
        "queued",
        {"kind": "web", "ref": "pr_1"},
        [],
        plan_id,
    )
    job = conn.execute("SELECT role, kind, brief, status FROM agent_job").fetchone()
    assert dict(job) == {
        "role": "reviewer",
        "kind": "launch",
        "brief": {"review_id": review_id, "plan_id": plan_id},
        "status": "queued",
    }
    with pytest.raises(ToolError, match="changed while confirming"):
        launch_review.submit(conn, BIZ, plan_id, approval={})


def test_a_new_version_supersedes_a_plan_in_review_and_withdraws_its_review(conn):
    plan_id, review_id = _submitted(conn)
    plans.create_version(conn, BIZ, goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[])
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.SUPERSEDED
    row = launch_review.get(conn, review_id)
    assert (row["state"], row["decided_by"]) == ("withdrawn", "owner")
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "skipped"


def test_a_review_is_decided_once_and_a_return_puts_the_plan_back_with_its_note(conn):
    plan_id, review_id = _submitted(conn)
    launch_review.decide(conn, review_id, state="returned", by="admin:founder@example.com", note="Name your license.")
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.DRAFT
    assert launch_review.note_for(conn, BIZ, plan_id) == {
        "state": "returned",
        "note": "Name your license.",
        "resolution": None,
    }
    with pytest.raises(ToolError, match="already decided"):
        launch_review.decide(conn, review_id, state="passed", by="reviewer")


def test_a_return_or_a_reject_always_carries_its_note(conn):
    """R18: a blank note is a programming error, never the admin's own words -- Task 10's admin command refuses one
    first, and Task 9's back_to_owner always passes one -- so decide() refuses it before any write; the review
    stays open and the plan stays in_review."""
    plan_id, review_id = _submitted(conn)
    for state in ("rejected", "returned"):
        for blank in (None, "  "):
            with pytest.raises(ValueError, match="a return or a reject carries its note"):
                launch_review.decide(conn, review_id, state=state, by="admin:founder@example.com", note=blank)
    row = launch_review.get(conn, review_id)
    assert row["state"] == "queued" and row["decided_by"] is None
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW


def test_the_owner_reads_in_review_then_held(conn):
    _plan_id, review_id = _submitted(conn)
    assert launch_review.owner_words(conn, BIZ) == launch_review.IN_REVIEW_SAY
    launch_review.step(conn, review_id, name="creative", ok=True, detail={}, state="creative")
    assert launch_review.get(conn, review_id)["steps"][0]["name"] == "creative"
    assert launch_review.hold(conn, review_id, "The page check fails") is True
    assert launch_review.owner_words(conn, BIZ) == launch_review.HELD_SAY
    assert launch_review.get(conn, review_id)["hold_reason"] == "The page check fails"


def test_a_hold_on_a_review_decided_meanwhile_does_nothing(conn):
    """Ruling R20: hold's read and write are one transaction, and the write's own WHERE repeats the OPEN check the
    read made -- a review a person decided before the hold is attempted is never overwritten."""
    plan_id, review_id = _submitted(conn)
    launch_review.decide(conn, review_id, state="rejected", by="admin:founder@example.com", note="No.")
    assert launch_review.hold(conn, review_id, "Some reason") is False
    row = launch_review.get(conn, review_id)
    assert (row["state"], row["decided_by"], row["hold_reason"]) == ("rejected", "admin:founder@example.com", None)
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.DRAFT  # the reject's own effect, untouched


def test_a_return_carries_its_resolution_and_an_admins_none(conn):
    """spec 2026-10-11 §5 (plan D4): the Reviewer's return names the resolution the owner's list fixes it with; an
    admin's return names none."""
    plan_id, review_id = _submitted(conn)
    launch_review.decide(
        conn, review_id, state="returned", by="reviewer", note="Give Admiry the number.", resolution="phone"
    )
    assert launch_review.note_for(conn, BIZ, plan_id) == {
        "state": "returned",
        "note": "Give Admiry the number.",
        "resolution": "phone",
    }
    assert launch_review.get(conn, review_id)["resolution"] == "phone"
    again = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})["review_id"]
    launch_review.decide(conn, again, state="returned", by="admin:founder@example.com", note="Name your licence.")
    assert launch_review.note_for(conn, BIZ, plan_id) == {
        "state": "returned",
        "note": "Name your licence.",
        "resolution": None,
    }


def test_validations_count_the_answered_calls_of_every_review_of_the_version(conn):
    """spec 2026-10-11 §5 (plan D1): the `validated` steps, ok or not, over every review row of the plan version --
    one review's rounds and a resubmitted version's second review alike; another step is not one, and another
    version's are its own."""
    plan_id, review_id = _submitted(conn)
    assert launch_review.validations(conn, plan_id) == 0
    launch_review.step(conn, review_id, name=launch_review.VALIDATED, ok=False, detail={"google": "refused"})
    launch_review.step(conn, review_id, name="creative", ok=True, detail={}, state="creative")
    launch_review.step(conn, review_id, name=launch_review.VALIDATED, ok=True, detail={})
    assert launch_review.validations(conn, plan_id) == 2
    launch_review.decide(
        conn, review_id, state="returned", by="reviewer", note="Fix the page.", resolution="check_page"
    )
    again = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})["review_id"]
    launch_review.step(conn, again, name=launch_review.VALIDATED, ok=True, detail={})
    assert launch_review.validations(conn, plan_id) == 3
    other = plans.create_version(
        conn, BIZ, goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[]
    )
    assert launch_review.validations(conn, other["id"]) == 0


def test_decide_refuses_a_resolution_outside_the_list_and_writes_nothing(conn):
    """Ruling F1 (task review): `resolution` outside launch_review.RESOLUTIONS is a programming error, the same as a
    blank note on a return -- decide() refuses it before any write, so `blockers.controls` is never fed a value
    `_controls`'s own `fixed[resolution]` would raise KeyError on."""
    plan_id, review_id = _submitted(conn)
    with pytest.raises(ValueError, match="resolution"):
        launch_review.decide(conn, review_id, state="returned", by="reviewer", note="Fix it.", resolution="nope")
    row = launch_review.get(conn, review_id)
    assert (row["state"], row["resolution"]) == ("queued", None)
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW
