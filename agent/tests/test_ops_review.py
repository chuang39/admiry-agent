"""L1 Task 10 (spec 2026-10-10 §7): what an admin reads of a first launch in review and the three decisions -- approve
and launch, return with a note, reject with a note -- each once, each an audit row in the admin's name."""

import pytest
from fastmcp.exceptions import ToolError

from agent import ads_refusal, ads_run, config, launch_check, launch_review, ops_review, plans
from agent.channels.google_search.client import RefusalDetail
from agent.tests.test_ads_run import BIZ, seed
from agent.tests.test_review_launch import draft_sections

ADMIN = "founder@example.com"


@pytest.fixture
def launched(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **k: calls.append(biz) or {"channels": {}, "say": ""})
    return calls


def _in_review(conn) -> str:
    seed(conn, status="draft", secs=draft_sections())
    plan_id = plans.latest(conn, BIZ)["id"]
    return launch_review.submit(conn, BIZ, plan_id, approval={"kind": "paid"})["review_id"]


def _actor(conn, tool: str) -> str | None:
    row = conn.execute("SELECT actor FROM agent_action WHERE tool = %s", (tool,)).fetchone()
    return row["actor"] if row else None


def test_the_queue_and_one_review_in_full(conn):
    review_id = _in_review(conn)
    launch_review.hold(conn, review_id, "The page check fails")
    q = ops_review.queue_view(conn)
    (row,) = q["open"]
    assert (row["id"], row["state"], row["hold_reason"], row["approval"], row["name"]) == (
        review_id,
        "held",
        "The page check fails",
        "paid",
        "Bright Smile Dental",
    )
    assert row["monthly_usd"] == 1000 and q["decided"] == []
    d = ops_review.detail(conn, review_id)
    assert d["review"]["id"] == review_id and d["plan"]["status"] == plans.IN_REVIEW and d["ads"]["headlines"]
    assert d["business"]["phone"] and d["page"]["url"] and ops_review.detail(conn, "lr_0000000000000000") is None


def test_an_admin_approves_and_launches_in_their_own_name(conn, launched):
    review_id = _in_review(conn)
    out = ops_review.approve(conn, config.load(), review_id, admin=ADMIN)
    assert out["passed"] is True and launched == [BIZ]
    assert launch_review.get(conn, review_id)["decided_by"] == f"admin:{ADMIN}"
    assert plans.latest(conn, BIZ)["status"] == plans.APPROVED
    assert _actor(conn, "admin.review_approve") == f"admin:{ADMIN}"


def test_a_return_needs_a_note_and_puts_it_first_on_the_owners_list(conn):
    review_id = _in_review(conn)
    with pytest.raises(ToolError, match=ops_review.NEEDS_NOTE):
        ops_review.send_back(conn, config.load(), review_id, admin=ADMIN, note="  ", state="returned")
    ops_review.send_back(conn, config.load(), review_id, admin=ADMIN, note="Add an end date", state="returned")
    assert launch_review.get(conn, review_id)["state"] == "returned"
    first = launch_check.check(conn, config.load(), BIZ)["items"][0]
    assert first["id"] == "review_note" and "Add an end date." in first["text"]
    assert _actor(conn, "admin.review_return") == f"admin:{ADMIN}"


def test_a_reject_keeps_the_plan_a_draft_with_its_note(conn):
    review_id = _in_review(conn)
    ops_review.send_back(conn, config.load(), review_id, admin=ADMIN, note="We cannot advertise this", state="rejected")
    assert launch_review.get(conn, review_id)["state"] == "rejected"
    assert plans.latest(conn, BIZ)["status"] == plans.DRAFT


def test_a_review_is_decided_once_whoever_comes_second(conn, launched):
    review_id = _in_review(conn)
    ops_review.approve(conn, config.load(), review_id, admin=ADMIN)
    with pytest.raises(ToolError, match="already decided"):
        ops_review.send_back(conn, config.load(), review_id, admin="ops@example.com", note="Too late", state="returned")
    assert ops_review.queue_view(conn)["decided"][0]["state"] == "passed"


# ---------------------------------------------------------------- R24: a refused approve is said, never "launched"


def test_an_admins_refused_approval_is_held_and_said_never_launched(conn, launched, monkeypatch):
    """Ruling R24: the approval's own check (proposals.apply_approve_plan) can refuse an admin's Approve -- held for a
    person, with that sentence, never reported as launched."""
    from agent import proposals
    from agent.roles import reviewer

    def refuse(conn, biz, payload):
        raise ToolError("refused")

    monkeypatch.setattr(proposals, "apply_approve_plan", refuse)
    review_id = _in_review(conn)
    with pytest.raises(ToolError, match=reviewer.APPROVAL_REFUSED.format(sentence="refused")):
        ops_review.approve(conn, config.load(), review_id, admin=ADMIN)
    held = launch_review.get(conn, review_id)
    assert held["state"] == "held" and held["hold_reason"] == reviewer.APPROVAL_REFUSED.format(sentence="refused")
    assert plans.latest(conn, BIZ)["status"] == plans.IN_REVIEW
    assert launched == []


# ---------------------------------------------------------------- R25: the decision and its audit row commit together


def test_a_failed_audit_row_rolls_back_the_decision_and_sends_no_mail(conn, monkeypatch):
    """Ruling R25: a failed audit insert must never leave a decision no one is on record for -- send_back's decide
    and its audit row commit in one transaction, and the owner is e-mailed only once that has committed."""
    from agent import audit
    from agent.roles import reviewer

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit, "record", boom)
    told = []
    monkeypatch.setattr(reviewer, "tell_back", lambda *a, **k: told.append(1))
    review_id = _in_review(conn)
    with pytest.raises(RuntimeError, match="audit down"):
        ops_review.send_back(conn, config.load(), review_id, admin=ADMIN, note="Add an end date", state="returned")
    assert launch_review.get(conn, review_id)["state"] in launch_review.OPEN
    assert plans.latest(conn, BIZ)["status"] == plans.IN_REVIEW
    assert told == []


# ---------------------------------------------------------------- minor: a bad note, a long note, approve's refusals


@pytest.mark.parametrize("bad_note", [42, ["x"]])
def test_a_non_string_note_needs_a_note(conn, bad_note):
    review_id = _in_review(conn)
    with pytest.raises(ToolError, match=ops_review.NEEDS_NOTE):
        ops_review.send_back(conn, config.load(), review_id, admin=ADMIN, note=bad_note, state="returned")


def test_a_long_note_is_cut_to_note_max(conn):
    review_id = _in_review(conn)
    ops_review.send_back(conn, config.load(), review_id, admin=ADMIN, note="x" * 600, state="returned")
    assert launch_review.get(conn, review_id)["note"] == "x" * ops_review.NOTE_MAX


def test_approve_on_an_unknown_review_raises_not_found(conn):
    with pytest.raises(ToolError, match=ops_review.NOT_FOUND):
        ops_review.approve(conn, config.load(), "lr_0000000000000000", admin=ADMIN)


def test_approve_on_an_already_decided_review_raises_decided(conn, launched):
    review_id = _in_review(conn)
    ops_review.approve(conn, config.load(), review_id, admin=ADMIN)
    with pytest.raises(ToolError, match=launch_review.DECIDED):
        ops_review.approve(conn, config.load(), review_id, admin=ADMIN)


# ---------------------------------------------------------------- the reject mail (mail.render needs no RESEND_API_KEY)


def test_the_reject_mail_renders():
    from agent import mail

    text = mail.render("mail_review_reject.txt.j2", note="We cannot advertise this", link="https://admiry.ai/plan/pl_x")
    assert "We cannot advertise this" in text
    assert "hello@admiry.ai" in text
    assert "https://admiry.ai/plan/pl_x" in text


# ---------------------------------------------------------------- the review's refusals (Task 9)


def _refused_row(conn, review_id: str, plan_id: str) -> None:
    found = RefusalDetail(
        code="policyFindingError: POLICY_FINDING",
        operation=5,
        kind="headline",
        position=1,
        field="mutate_operations[5].ad_group_ad_operation.create.ad.responsive_search_ad.headlines[1].text",
        topic="TRADEMARKS_IN_AD_TEXT",
        type="PROHIBITED",
        exemptible=False,
        certificate=False,
        text="Invisalign Deals",
    )
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=review_id,
        found=[(found, "ad_text", "dropped")],
    )


def test_the_detail_lists_the_reviews_refusals(conn):
    """spec 2026-10-11 §6 (plan D14): the admin reads Google's own code and topic, the text and what Admiry did."""
    review_id = _in_review(conn)
    assert ops_review.detail(conn, review_id)["refusals"] == []
    _refused_row(conn, review_id, launch_review.get(conn, review_id)["plan_id"])
    (row,) = ops_review.detail(conn, review_id)["refusals"]
    assert (row["code"], row["topic"], row["type"], row["kind"], row["text"], row["class"], row["action"]) == (
        "policyFindingError: POLICY_FINDING",
        "TRADEMARKS_IN_AD_TEXT",
        "PROHIBITED",
        "headline",
        "Invisalign Deals",
        "ad_text",
        "dropped",
    )
    assert isinstance(row["created_at"], str)
