"""L1 Task 9 (spec 2026-10-10 §6.2): the Reviewer -- research-run takes a first launch's review job whatever
AGENT_LAUNCH_REVIEW says now and whatever AGENT_ROLES says (ruling R16: the switch gates only whether a yes goes to
review; a review once queued is finished): the Critic's look, the page, Google's validateOnly, the configured ads
against the agreed plan, then the approval and the launch -- or a hold for a person, a return to the owner, a try at
the next run."""

import psycopg
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_refusal, ads_run, blockers, config, launch_check, launch_review, llm, models, plans
from agent.channels.base import PlatformError, Resources
from agent.channels.google_search import adapter as google
from agent.channels.google_search import build as google_build
from agent.channels.google_search.client import REFUSED_WITH_REASON, RefusalDetail
from agent.roles import queue, reviewer, runner
from agent.tests.conftest import JARGON
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_ads_run import (  # noqa: F401 -- `gads` is a fixture used by name
    BIZ,
    DESCRIPTIONS,
    HEADLINES,
    add_plan,
    gads,
    seed,
)
from agent.tests.test_review_launch import REVIEW, draft_sections

REVISE = REVIEW.model_copy(
    update={"grades": [g.model_copy(update={"grade": 2}) for g in REVIEW.grades]}
)  # every dimension at 2: the Critic's verdict is not deliver


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")


def _model(monkeypatch, **kw) -> FakeGenerator:
    gen = FakeGenerator(reviews=[REVIEW], **kw)
    monkeypatch.setattr(llm, "generator", lambda settings=None, **k: gen)
    return gen


@pytest.fixture
def page_ok(monkeypatch):
    monkeypatch.setattr(reviewer, "_page_now", lambda conn, biz: (None, False))


def _in_review(conn) -> dict:
    seed(conn, status="draft", secs=draft_sections())
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    return launch_review.get(conn, out["review_id"])


def _in_review_as(conn, knowledge: dict) -> dict:
    """As `_in_review`, with the business's own knowledge -- so `plans.name_for` vouches for a name, or not."""
    seed(conn, status="draft", secs=draft_sections(), knowledge=knowledge)
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    return launch_review.get(conn, out["review_id"])


def _run(conn) -> dict:
    return runner.research_run(conn, 0, 1)


def test_a_review_that_passes_approves_and_launches_the_plan(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    gen = _model(monkeypatch)
    review = _in_review(conn)
    out = _run(conn)
    assert out["reviewed"] == 1 and out["mode"] == "off"  # AGENT_ROLES stays off
    done = launch_review.get(conn, review["id"])
    assert done["state"] == "passed" and done["decided_by"] == "reviewer"
    # spec 2026-10-11 §5 (plan D1): the accepted validateOnly is a "validated" step too, ok=True, before "configured"
    assert [s["name"] for s in done["steps"]] == ["creative", "validated", "configured", "checked"]
    assert done["steps"][2]["detail"]["sent"] is True
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.APPROVED
    assert ads_run.get(conn, BIZ, "google_search")["validated_at"] is not None  # launch_now under validate
    assert len(gen.configured_calls) == 1
    row = conn.execute("SELECT approval_kind, approval_ref FROM agent_action WHERE tool = 'job.review_pass'").fetchone()
    assert (row["approval_kind"], row["approval_ref"]) == ("review", review["id"])


def test_the_critics_doubt_holds_it_for_a_person(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    gen = FakeGenerator(reviews=[REVISE])
    monkeypatch.setattr(llm, "generator", lambda settings=None, **k: gen)
    review = _in_review(conn)
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held" and held["hold_reason"].startswith("The Critic's verdict on this version is")
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.IN_REVIEW
    assert launch_review.owner_words(conn, BIZ) == launch_review.HELD_SAY


def test_googles_refusal_holds_it(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)

    def refused(*a, **k):
        raise PlatformError("Google Ads did not take the ads", retryable=False)

    monkeypatch.setattr(google.GoogleSearchAdapter, "launch", refused)
    review = _in_review(conn)
    _run(conn)
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.GOOGLE_REFUSED.format(
        error="Google Ads did not take the ads"
    )
    # ruling G7, task review fix round 1: a non-retryable refusal with no details never answered a mutate (B1) --
    # held, same as above, but never counted toward the version's three answered calls
    assert launch_review.validations(conn, review["plan_id"]) == 0


def test_googles_passing_trouble_is_tried_at_the_next_run_and_held_after_the_last(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)

    def busy(*a, **k):
        raise PlatformError("Google did not answer; Admiry retries within the hour", retryable=True)

    monkeypatch.setattr(google.GoogleSearchAdapter, "launch", busy)
    review = _in_review(conn)
    for _ in range(runner.ATTEMPTS - 1):
        assert _run(conn)["retried"] == 1
        assert launch_review.get(conn, review["id"])["state"] in launch_review.RUNNING
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "held"


def test_configured_ads_the_model_doubts_are_held(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(
        monkeypatch,
        configured_reviews=[
            models.ConfiguredReview(matches=False, issues=["Names a city the business does not serve"], confidence=0.8)
        ],
    )
    review = _in_review(conn)
    _run(conn)
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.MODEL_SAYS.format(
        issues="Names a city the business does not serve"
    )


def test_too_little_copy_holds_it_with_the_dropped_lines_on_the_creative_step(conn, on, page_ok, monkeypatch):
    """Minor (D1): a NO_COPY hold must still let the admin see what the linter dropped and why -- not just that too
    little survived."""
    _model(monkeypatch)
    secs = draft_sections(
        headlines=["Call Now!", "Only Today!", "Best Deal!"],
        descriptions=["Great service!! Call now!!", "Amazing!! Don't wait!!"],
    )
    seed(conn, status="draft", secs=secs)
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    review = launch_review.get(conn, out["review_id"])
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["hold_reason"] == reviewer.NO_COPY
    creative = next(s for s in held["steps"] if s["name"] == "creative")
    assert len(creative["detail"]["dropped"]) == 5
    assert all("Dropped a" in line for line in creative["detail"]["dropped"])


def test_the_owners_own_page_failing_goes_back_to_the_owner(conn, on, gads, monkeypatch):  # noqa: F811
    _model(monkeypatch)
    fix = ads_run.PAGE_FIX.format(fix="Paste the tracking snippet.")
    monkeypatch.setattr(reviewer, "_page_now", lambda conn, biz: (fix, True))
    owner_calls = []
    monkeypatch.setattr(reviewer.mail, "to_owner", lambda *a, **k: owner_calls.append(k) or "sent")
    review = _in_review(conn)
    _run(conn)
    back = launch_review.get(conn, review["id"])
    assert (back["state"], back["note"]) == ("returned", fix)
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.DRAFT
    # R25 fix round: back_to_owner's mail is now tell_back's own call -- the Reviewer's own return still e-mails
    assert len(owner_calls) == 1 and owner_calls[0]["note"] == fix


def test_off_for_the_business_nothing_reaches_google_and_the_plan_is_approved(conn, gads, page_ok, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")  # every channel switch off
    _model(monkeypatch)
    review = _in_review(conn)
    _run(conn)
    done = launch_review.get(conn, review["id"])
    assert done["state"] == "passed" and done["steps"][1]["detail"]["sent"] is False
    assert plans.get(conn, BIZ, review["plan_id"])["execution"]["google_search"] == "off"


def test_a_decided_review_is_skipped(conn, on, page_ok, monkeypatch):
    _model(monkeypatch)
    review = _in_review(conn)
    launch_review.decide(conn, review["id"], state="rejected", by="admin:founder@example.com", note="No.")
    assert _run(conn)["skipped"] == 1


def test_a_plan_that_left_review_another_way_is_withdrawn_and_the_job_skipped(conn, on, page_ok, monkeypatch):
    """Ruling R19d: a plan can leave in_review by a path other than launch_review.decide (set directly here, as a
    bug or an unanticipated writer might) -- run_job still withdraws the review itself before skipping the job, so
    it is never left dangling in a running state with nothing left to finish it (hold_stranded's own concern)."""
    _model(monkeypatch)
    review = _in_review(conn)
    conn.execute("UPDATE plan SET status = %s WHERE id = %s", (plans.APPROVED, review["plan_id"]))
    out = _run(conn)
    assert out["skipped"] == 1
    assert launch_review.get(conn, review["id"])["state"] == "withdrawn"


def test_a_spent_day_holds_it(conn, on, page_ok, monkeypatch):
    _model(monkeypatch)
    monkeypatch.setattr(runner, "runs_today", lambda conn, biz: runner.ROLE_RUNS_PER_DAY)
    review = _in_review(conn)
    _run(conn)
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.CAPPED


def test_the_model_reads_the_agreed_plan_and_the_configured_ads(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    gen = _model(monkeypatch)
    _in_review(conn)
    _run(conn)
    m = gen.configured_calls[0]["material"]
    assert set(m) == {"business", "agreed", "configured"}
    assert m["configured"]["headlines"] and m["configured"]["sent_to_google"] is True
    assert m["agreed"]["page"] == m["configured"]["page"]


def test_the_checked_steps_detail_records_the_comparison_for_the_admin(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    """Ruling R21 (D1, spec §7): the agreed and configured figures the model was asked to compare, on the row --
    never shown to the owner, for the admin to read alongside a hold -- and `agreed.daily_usd` is the build's own
    figure, matching `configured.daily_usd`."""
    _model(monkeypatch)
    review = _in_review(conn)
    _run(conn)
    checked = next(s for s in launch_review.get(conn, review["id"])["steps"] if s["name"] == "checked")
    assert checked["detail"]["agreed"]["daily_usd"] == checked["detail"]["configured"]["daily_usd"]
    assert isinstance(checked["detail"]["configured"]["dropped"], list)
    assert {"matches", "issues", "confidence"} <= set(checked["detail"])


def test_the_agreed_daily_budget_is_floored_to_the_cent_as_the_build_sends_it(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    """Fix round 2, still R21: the build floors to the cent (math.floor), never rounds -- a $500 monthly gives
    16.44, not round(500 / 30.4, 2) == 16.45, so the model and the admin never see a false mismatch."""
    _model(monkeypatch)
    seed(conn, status="draft", secs=draft_sections(monthly_usd=500.0))
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    review = launch_review.get(conn, out["review_id"])
    _run(conn)
    checked = next(s for s in launch_review.get(conn, review["id"])["steps"] if s["name"] == "checked")
    assert checked["detail"]["agreed"]["daily_usd"] == checked["detail"]["configured"]["daily_usd"] == 16.44


def test_run_now_takes_the_waiting_job(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)
    review = _in_review(conn)
    out = reviewer.run_now(conn, config.load(), review["id"])
    assert out["review"] == "passed"
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == queue.DONE


# ---------------------------------------------------------------- ruling R22: the pass commits before its launch


def test_approve_refuses_to_run_inside_an_open_transaction(conn, on, monkeypatch):
    review = _in_review(conn)
    with pytest.raises(RuntimeError, match="outside a transaction"), conn.transaction():
        reviewer.approve(conn, config.load(), review, by="reviewer")


def test_run_now_refuses_to_run_inside_an_open_transaction(conn, on, monkeypatch):
    """Minor (trap): Task 12's MCP body runs in its own transaction; run_now must refuse before it even claims the
    job, or a passing review would be caught by run_job's own `except Exception` and turned into a false
    UNEXPECTED hold instead of a RuntimeError the caller sees."""
    review = _in_review(conn)
    with pytest.raises(RuntimeError, match="outside a transaction"), conn.transaction():
        reviewer.run_now(conn, config.load(), review["id"])
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "queued"  # never claimed


# ---------------------------------------------------------------- ruling R16: the switch gates entry only


def test_switch_off_after_submit_still_finishes_the_review(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    """A review already queued is the Reviewer's to finish whatever AGENT_LAUNCH_REVIEW says now: the switch decides
    only whether a yes goes to review (proposals._apply), never whether a review once queued is taken."""
    _model(monkeypatch)
    review = _in_review(conn)
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "off")
    out = _run(conn)
    assert out["reviewed"] == 1
    assert launch_review.get(conn, review["id"])["state"] == "passed"


# ---------------------------------------------------------------- ruling R17: what a person sent back is passed by a person


def test_a_version_an_admin_returned_before_is_held_again_without_a_second_pass(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)
    review = _in_review(conn)
    plan_id = review["plan_id"]
    launch_review.decide(conn, review["id"], state="returned", by="admin:founder@example.com", note="Name your license")
    again = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    _run(conn)
    held = launch_review.get(conn, again["review_id"])
    assert held["state"] == "held"
    assert held["hold_reason"] == reviewer.RETURNED_BEFORE.format(note="Name your license")


def test_a_version_the_reviewer_itself_returned_is_reviewed_again_in_full(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)
    review = _in_review(conn)
    plan_id = review["plan_id"]
    fix = ads_run.PAGE_FIX.format(fix="Paste the tracking snippet.")
    launch_review.decide(conn, review["id"], state="returned", by="reviewer", note=fix)
    again = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    _run(conn)
    assert launch_review.get(conn, again["review_id"])["state"] == "passed"


# ---------------------------------------------------------------- ruling R19: nothing a person paid for waits unseen


def test_a_plan_with_no_google_search_entry_is_held_as_no_search(conn, on, page_ok, monkeypatch):
    _model(monkeypatch)
    secs = draft_sections()
    secs["ads"]["channels"][0]["channel"] = "newsbreak"  # the only funded channel is not Google Search
    seed(conn, status="draft", secs=secs)
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    review = launch_review.get(conn, out["review_id"])
    _run(conn)
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.NO_SEARCH


def test_an_unexpected_error_holds_it_and_the_job_is_done(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reviewer, "configured_material", boom)
    review = _in_review(conn)
    out = _run(conn)
    assert out["done"] == 1
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.UNEXPECTED.format(
        error="RuntimeError: boom"
    )


def test_a_review_whose_job_died_is_held_as_stranded(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)
    review = _in_review(conn)
    # a run that died: queue.expire_stale (or a retry that could not be queued) failed the job, nothing waits for it
    conn.execute("UPDATE agent_job SET status = 'failed' WHERE role = %s AND kind = %s", (reviewer.ROLE, reviewer.KIND))
    out = _run(conn)
    assert out["stranded"] == 1
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.STRANDED


def test_an_admin_deciding_mid_run_ends_with_the_review_returned_and_nothing_else(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    _model(monkeypatch)
    review = _in_review(conn)
    sent_ops, sent_owner = [], []
    monkeypatch.setattr(reviewer.mail_out, "send_plain", lambda *a, **k: sent_ops.append(k) or True)
    monkeypatch.setattr(reviewer.mail, "to_owner", lambda *a, **k: sent_owner.append(k) or "sent")

    def intervene(*a, **k):
        launch_review.decide(conn, review["id"], state="returned", by="admin:founder@example.com", note="Stop")
        return Resources(ids={})  # as the gads fake's validateOnly answer would be

    monkeypatch.setattr(google.GoogleSearchAdapter, "launch", intervene)
    out = _run(conn)
    assert out["skipped"] == 1 and out["done"] == 0  # approve's own decide(state="passed") hits DECIDED (R19a)
    after = launch_review.get(conn, review["id"])
    assert (after["state"], after["note"]) == ("returned", "Stop")
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.DRAFT  # never approved
    assert not sent_ops and not sent_owner  # no hold e-mail: nothing was held


# ---------------------------------------------------------------- minor: _tell_held sends each notice on its own


def test_a_hold_e_mails_the_operator_once_and_the_owner_once(conn, on, page_ok, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_EMAIL", "ops@example.com")
    review = _in_review(conn)
    ops_calls, owner_calls = [], []
    monkeypatch.setattr(reviewer.mail_out, "send_plain", lambda settings, **kw: ops_calls.append(kw) or True)
    monkeypatch.setattr(reviewer.mail, "to_owner", lambda *a, **k: owner_calls.append(k) or "sent")
    out = reviewer.hold(conn, config.load(), review, "Some reason.")
    assert out == {"held": "Some reason."}
    assert len(ops_calls) == 1 and len(owner_calls) == 1
    assert ops_calls[0]["paragraphs"][1] == "Why: Some reason."
    # a second hold of an already-held review e-mails nobody (launch_review.hold's own OPEN check no-ops)
    reviewer.hold(conn, config.load(), review, "Another reason.")
    assert len(ops_calls) == 1 and len(owner_calls) == 1


def test_a_failed_operator_notice_still_sends_the_owners_and_the_hold_stands(conn, on, page_ok, monkeypatch):
    review = _in_review(conn)
    owner_calls = []

    def boom(*a, **k):
        raise RuntimeError("Resend down")

    monkeypatch.setattr(reviewer.mail_out, "send_plain", boom)
    monkeypatch.setattr(reviewer.mail, "to_owner", lambda *a, **k: owner_calls.append(1) or "sent")
    out = reviewer.hold(conn, config.load(), review, "Some reason.")
    assert out == {"held": "Some reason."}
    assert launch_review.get(conn, review["id"])["state"] == "held"
    assert len(owner_calls) == 1


def test_a_name_read_that_raises_still_sends_the_owner_and_hold_never_raises(conn, on, page_ok, monkeypatch):
    """Residual fix item 3: _tell_held used to read the business's name (plans.name_for / business.latest_knowledge)
    outside either try, so a database error there -- after the hold had already committed -- escaped hold() despite
    its docstring's "never raises". The name read now sits inside the operator notice's own try, so it is skipped
    like any other failure there (no operator e-mail), but the owner's must still go."""
    review = _in_review(conn)
    ops_calls, owner_calls = [], []
    monkeypatch.setattr(reviewer.mail_out, "send_plain", lambda settings, **kw: ops_calls.append(kw) or True)
    monkeypatch.setattr(reviewer.mail, "to_owner", lambda *a, **k: owner_calls.append(k) or "sent")

    def boom(conn, biz):
        raise RuntimeError("database is down")

    monkeypatch.setattr(reviewer.business, "latest_knowledge", boom)
    out = reviewer.hold(conn, config.load(), review, "Some reason.")
    assert out == {"held": "Some reason."}
    assert launch_review.get(conn, review["id"])["state"] == "held"
    assert len(owner_calls) == 1 and not ops_calls


def test_no_name_names_the_business_by_its_id_once_not_doubled(conn, on, page_ok, monkeypatch):
    """Residual fix item 3: with no name `plans.name_for` vouches for, the old fallback (`name_for(...) or biz`) read
    as "biz_x (biz_x)" in both the subject and the body; the id now appears once in each."""
    review = _in_review_as(conn, {"phone": "650-555-0100", "category": "dentists"})  # no name, website or listing
    ops_calls = []
    monkeypatch.setattr(reviewer.mail_out, "send_plain", lambda settings, **kw: ops_calls.append(kw) or True)
    monkeypatch.setattr(reviewer.mail, "to_owner", lambda *a, **k: "sent")
    reviewer.hold(conn, config.load(), review, "Some reason.")
    assert len(ops_calls) == 1
    subject, body = ops_calls[0]["subject"], ops_calls[0]["paragraphs"][0]
    assert subject == f"[Admiry] A launch waits for review: {BIZ}" and subject.count(BIZ) == 1
    assert (
        body == f"Admiry's Reviewer held the first launch of {BIZ}, plan {review['plan_id']}." and body.count(BIZ) == 1
    )


# ---------------------------------------------------------------- minor: APPROVAL_REFUSED and model-unavailable


def test_the_approvals_own_check_refusing_at_the_pass_holds_it(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    from agent import proposals

    _model(monkeypatch)

    def refuse(conn, biz, payload):
        raise ToolError("refused")

    monkeypatch.setattr(proposals, "apply_approve_plan", refuse)
    review = _in_review(conn)
    out = _run(conn)
    assert out["done"] == 1
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held"
    assert held["hold_reason"] == reviewer.APPROVAL_REFUSED.format(sentence="refused")
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.IN_REVIEW
    assert ads_run.get(conn, BIZ, "google_search") is None  # nothing launched


def test_model_unavailable_is_retried_then_held_with_model_down(conn, on, gads, page_ok, monkeypatch):  # noqa: F811
    gen = FakeGenerator(reviews=[REVIEW], error="down")
    monkeypatch.setattr(llm, "generator", lambda settings=None, **k: gen)
    review = _in_review(conn)
    for _ in range(runner.ATTEMPTS - 1):
        assert _run(conn)["retried"] == 1
    _run(conn)
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.MODEL_DOWN.format(n=runner.ATTEMPTS)


# ---------------------------------------------------------------- spec 2026-10-11 §5: Google's refusals, acted on by class

REFUSED_CODE = "policyFindingError: POLICY_FINDING"
FOUR = [*HEADLINES, "Family Dentist in Palo Alto"]  # one more than plans.MIN_HEADLINES: one can go


def _detail(
    kind: str = "headline",
    text: str | None = "Same-Week Appointments",
    *,
    topic: str | None = "TRADEMARKS_IN_AD_TEXT",
    code: str = REFUSED_CODE,
    certificate: bool = False,
) -> RefusalDetail:
    return RefusalDetail(
        code=code,
        operation=5,
        kind=kind,
        position=1 if kind in ("headline", "description") else None,
        field=f"mutate_operations[5].{kind}",
        topic=topic,
        type="PROHIBITED" if topic else None,
        exemptible=False,
        certificate=certificate,
        text=text,
    )


def _refusal(*details: RefusalDetail) -> PlatformError:
    return PlatformError(REFUSED_WITH_REASON.format(REFUSED_CODE), retryable=False, details=tuple(details))


def _google(monkeypatch, *answers) -> list:
    """GoogleSearchAdapter.launch answering each validateOnly call in turn -- a PlatformError to raise, or None
    (Google accepted) -- the last answer repeating; the list holds the Build of every call."""
    calls: list = []

    def launch(self, conn, settings, account, build, existing, *, http=None, validate_only):
        calls.append(build)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if answer is not None:
            raise answer
        return Resources(ids={})

    monkeypatch.setattr(google.GoogleSearchAdapter, "launch", launch)
    return calls


def _mails(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr(
        reviewer.mail,
        "to_owner",
        lambda conn, biz, template, subject, **ctx: sent.append((template, subject, ctx)) or "sent",
    )
    monkeypatch.setattr(reviewer.mail_out, "send_plain", lambda *a, **k: True)
    return sent


def _in_review_with(conn, **kw) -> dict:
    seed(conn, status="draft", secs=draft_sections(**kw))
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    return launch_review.get(conn, out["review_id"])


def _rows(conn) -> list[tuple]:
    # conn's row_factory is dict_row (agent/db.py): tuple(a dict) gives its keys, not its values -- read explicitly.
    return [
        (r["kind"], r["text"], r["class"], r["action"])
        for r in conn.execute("SELECT kind, text, class, action FROM ads_refusal ORDER BY created_at, id")
    ]


def test_refused_ad_text_is_dropped_sent_again_and_the_review_passes(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), None)
    review = _in_review_with(conn, headlines=FOUR)
    _run(conn)
    done = launch_review.get(conn, review["id"])
    assert done["state"] == "passed" and plans.get(conn, BIZ, review["plan_id"])["status"] == plans.APPROVED
    # 2 of the Reviewer's own, +1 from approve()'s own ads_run.launch_now (AGENT_EXEC_GOOGLE_SEARCH=validate: its
    # first launch step is itself a validateOnly call, through the same adapter.launch this monkeypatch catches)
    assert len(calls) == 3 and calls[1].payload["dropped"].headlines == frozenset({"Same-Week Appointments"})
    assert _rows(conn) == [("headline", "Same-Week Appointments", "ad_text", "dropped")]
    # ruling G5, task review fix round 1: the "changed" step (and its mail) moves to the pass, after "checked" --
    # never between the two "validated" steps, and never while the review is still only held or returned
    assert [s["name"] for s in done["steps"]] == [
        "creative",
        "validated",
        "validated",
        "configured",
        "checked",
        "changed",
    ]
    validated = [s for s in done["steps"] if s["name"] == "validated"]
    assert [s["ok"] for s in validated] == [False, True] and launch_review.validations(conn, review["plan_id"]) == 2
    assert validated[0]["detail"]["refusals"][0]["class"] == "ad_text" and validated[1]["detail"] == {"validations": 2}
    (mail,) = sent
    assert mail[0] == "mail_review_changed.txt.j2" and mail[1] == reviewer.CHANGED_SUBJECT
    assert (
        mail[2]["changes"][0]["what"] == "the headline “Same-Week Appointments”" and "/?setup=biz_1" in mail[2]["link"]
    )
    assert next(s for s in done["steps"] if s["name"] == "changed")["detail"]["mail"] == "sent"
    checked = next(s for s in done["steps"] if s["name"] == "checked")
    assert "Same-Week Appointments" not in checked["detail"]["configured"]["headlines"]
    assert checked["detail"]["configured"]["removed"] == ["the headline “Same-Week Appointments”"]
    assert plans.get(conn, BIZ, review["plan_id"])["sections"]["ads"]["channels"][0]["headlines"] == FOUR


def test_a_keyword_google_refuses_is_dropped_too_and_the_ad_text_stays(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    refused = _detail("keyword", "invisalign cost", topic=None, code="policyViolationError: POLICY_ERROR")
    calls = _google(monkeypatch, _refusal(refused), None)
    review = _in_review_with(conn)
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "passed"
    assert calls[1].payload["dropped"] == google_build.Dropped(keywords=frozenset({"invisalign cost"}))
    assert _rows(conn) == [("keyword", "invisalign cost", "keyword", "dropped")]


def test_a_multi_channel_draft_listing_another_channel_first_still_drops_the_refused_phrase(
    conn,
    on,
    gads,  # noqa: F811 - gads is a fixture used by name
    page_ok,
    monkeypatch,
):
    """Ruling N1 (final review): `_classify` must match a refused search phrase against the view the build
    actually sends -- Google Search's entry first (`google._search_view`), never `channels[0]` of the raw plan.
    A draft that lists another channel first (plans.py keeps the draft's order) still drops the phrase, never
    holds it as a person's (`other`, GOOGLE_REFUSED) for want of a theme on the channel that happens to be first."""
    _model(monkeypatch)
    _mails(monkeypatch)
    refused = _detail("keyword", "invisalign cost", topic=None, code="policyViolationError: POLICY_ERROR")
    calls = _google(monkeypatch, _refusal(refused), None)
    secs = draft_sections()
    search_ch = secs["ads"]["channels"][0]
    other_ch = {**search_ch, "channel": "newsbreak", "monthly_usd": 200.0, "themes": []}  # no themes of its own
    secs["ads"]["channels"] = [other_ch, search_ch]  # another channel listed before Google Search
    seed(conn, status="draft", secs=secs)
    plan_id = plans.latest(conn, BIZ)["id"]
    out = launch_review.submit(conn, BIZ, plan_id, approval={"kind": "web"})
    review = launch_review.get(conn, out["review_id"])
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "passed"
    assert calls[1].payload["dropped"] == google_build.Dropped(keywords=frozenset({"invisalign cost"}))
    assert _rows(conn) == [("keyword", "invisalign cost", "keyword", "dropped")]


def test_a_drop_that_leaves_too_little_holds_it_for_the_back_office(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Review Focus 3: three headlines agreed, one refused -- never returned to the owner, never passed thin."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), None)
    review = _in_review_with(conn)  # HEADLINES: exactly plans.MIN_HEADLINES
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held" and held["hold_reason"] == reviewer.TOO_LITTLE_AFTER_DROP.format(h=2, d=2)
    assert len(calls) == 1 and _rows(conn) == [("headline", "Same-Week Appointments", "ad_text", "dropped")]
    # ruling G5, task review fix round 1: the version never passes, so the change mail never goes -- only the hold's
    assert [m[0] for m in sent] == ["mail_review_hold.txt.j2"]


def test_a_second_refusal_after_the_drop_holds_it(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Review Focus 2 (plan D2): one drop a run; the next refusal, whatever it is, is a person's; no third call."""
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), _refusal(_detail(text="Clear Aligners Near You")))
    review = _in_review_with(conn, headlines=FOUR)
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held" and held["hold_reason"].startswith("Google refused the configuration again")
    assert len(calls) == 2 and launch_review.validations(conn, review["plan_id"]) == 2
    assert _rows(conn) == [
        ("headline", "Same-Week Appointments", "ad_text", "dropped"),
        ("headline", "Clear Aligners Near You", "ad_text", "held"),
    ]


def test_the_fourth_call_is_never_made(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """spec §5 (plan D1): two answered calls on the version already; the third is refused and dropped; no fourth."""
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), None)
    review = _in_review_with(conn, headlines=FOUR)
    for _ in range(2):
        launch_review.step(conn, review["id"], name=launch_review.VALIDATED, ok=False, detail={})
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held" and held["hold_reason"] == reviewer.BUDGET_SPENT.format(n=3)
    assert len(calls) == 1 and launch_review.validations(conn, review["plan_id"]) == 3


def test_a_version_whose_validations_are_spent_is_held_before_any_call(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, None)
    review = _in_review_with(conn)
    for _ in range(3):
        launch_review.step(conn, review["id"], name=launch_review.VALIDATED, ok=False, detail={})
    _run(conn)
    assert launch_review.get(conn, review["id"])["hold_reason"] == reviewer.BUDGET_SPENT.format(n=3) and calls == []


def test_google_not_answering_after_a_drop_is_tried_next_run_with_the_drop_kept(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), PlatformError(google.RETRYING, retryable=True), None)
    review = _in_review_with(conn, headlines=FOUR)
    assert _run(conn)["retried"] == 1
    assert launch_review.validations(conn, review["plan_id"]) == 1  # the call Google did not answer is not counted
    _run(conn)
    # 3 of the Reviewer's own (refusal, busy, accept), +1 from approve()'s own ads_run.launch_now (see the sibling
    # test above: its first launch step under AGENT_EXEC_GOOGLE_SEARCH=validate is itself a validateOnly call)
    assert launch_review.get(conn, review["id"])["state"] == "passed" and len(calls) == 4
    assert calls[2].payload["dropped"].headlines == frozenset({"Same-Week Appointments"})


def test_the_owners_own_page_refused_goes_back_with_the_page_check(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    url = "https://brightsmile.example/invisalign?src=gads"
    calls = _google(monkeypatch, _refusal(_detail("url", url, topic="DESTINATION_NOT_WORKING")))
    review = _in_review_with(conn)
    _run(conn)
    note = reviewer.PAGE_REFUSED_WHY_NOTE.format(why=reviewer.PAGE_REFUSED_WHY["DESTINATION_NOT_WORKING"])
    back = launch_review.get(conn, review["id"])
    assert (back["state"], back["note"], back["resolution"]) == ("returned", note, "check_page")
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.DRAFT and len(calls) == 1
    assert _rows(conn) == [("url", url, "destination", "returned")]
    assert [m[0] for m in sent] == ["mail_review_return.txt.j2"] and sent[0][2]["note"] == note
    item = launch_check.check(conn, config.load(), BIZ)["items"][0]
    assert (item["id"], item["kind"], item["actions"]) == ("review_note", "fix", [blockers.CHECK_NOW])


def test_the_page_refusal_note_says_why_by_topic():
    """Follow-up 2026-10-12: the destination detail's own topic becomes a short owner-words reason inside the usual
    sentence shape (first match wins); an unknown topic, or none, keeps today's sentence unchanged."""
    for topic, why in reviewer.PAGE_REFUSED_WHY.items():
        note, resolution = reviewer._owner_note([], [_detail("url", "https://x.example", topic=topic)])
        assert note == f"Google could not use your page for these ads: {why}. Fix the page, then check it again."
        assert resolution == "check_page"
    for topic in ("DESTINATION_SOMETHING_ELSE", None):
        note, resolution = reviewer._owner_note([], [_detail("url", "https://x.example", topic=topic)])
        assert (note, resolution) == (reviewer.PAGE_REFUSED_NOTE, "check_page")


def test_an_earlier_unknown_destination_topic_is_skipped_for_a_later_known_one():
    """Review round 1, M5: _owner_note reads the first destination detail *whose topic it knows*, not simply the
    first one in the list -- an unknown topic earlier in the refusal's own details never shadows a known one that
    comes after it."""
    destinations = [
        _detail("url", "https://x.example", topic="DESTINATION_SOMETHING_ELSE"),
        _detail("url", "https://x.example", topic="DESTINATION_MISMATCH"),
    ]
    note, resolution = reviewer._owner_note([], destinations)
    why = reviewer.PAGE_REFUSED_WHY["DESTINATION_MISMATCH"]
    assert note == f"Google could not use your page for these ads: {why}. Fix the page, then check it again."
    assert resolution == "check_page"


def test_admirys_own_page_refused_is_held_for_the_back_office(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    _google(monkeypatch, _refusal(_detail("url", "https://x.example", topic="DESTINATION_MISMATCH")))
    review = _in_review_with(conn)
    # Admiry's own page (not the owner's)
    conn.execute("UPDATE page SET sections = %s", (Jsonb({"copy": {"headline": "Invisalign in Palo Alto"}}),))
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held" and held["hold_reason"] == reviewer.DESTINATION_HELD.format(
        topic="DESTINATION_MISMATCH"
    )
    assert _rows(conn) == [("url", "https://x.example", "destination", "held")]


def test_the_phone_google_refuses_goes_back_as_the_phone_ask(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    _google(monkeypatch, _refusal(_detail("phone", "6505550100", topic=None, code="assetError: INVALID_PHONE_NUMBER")))
    review = _in_review_with(conn)
    _run(conn)
    back = launch_review.get(conn, review["id"])
    assert (back["state"], back["note"], back["resolution"]) == ("returned", reviewer.PHONE_REFUSED_NOTE, "phone")
    item = launch_check.check(conn, config.load(), BIZ)["items"][0]
    assert (item["id"], item["kind"], item["question"]["input"]) == ("phone", "ask", "tel")
    assert _rows(conn) == [("phone", "6505550100", "business", "returned")]


def test_another_business_fact_goes_back_as_a_plain_note(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    _google(monkeypatch, _refusal(_detail("ad", None, topic="HEALTHCARE", certificate=True)))
    review = _in_review_with(conn)
    _run(conn)
    back = launch_review.get(conn, review["id"])
    assert (back["state"], back["note"], back["resolution"]) == ("returned", reviewer.BUSINESS_NOTE, None)
    item = launch_check.check(conn, config.load(), BIZ)["items"][0]
    assert (item["id"], item["kind"], item["actions"], item["blocking"]) == ("review_note", "note", [], False)


def test_a_refusal_admiry_cannot_place_is_held_and_recorded(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    _google(monkeypatch, _refusal(_detail("budget", None, topic=None, code="campaignBudgetError: INVALID_AMOUNT")))
    review = _in_review_with(conn)
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held"
    assert held["hold_reason"] == reviewer.GOOGLE_REFUSED.format(error=REFUSED_WITH_REASON.format(REFUSED_CODE))
    assert _rows(conn) == [("budget", None, "other", "held")]


def test_a_line_google_quotes_that_the_plan_does_not_hold_is_a_persons(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail("ad", "Free Whitening")))
    review = _in_review_with(conn)
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "held" and len(calls) == 1
    assert _rows(conn) == [("ad", "Free Whitening", "other", "held")]


def test_a_line_google_quotes_inside_the_ad_drops_every_line_that_holds_it(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail("ad", "Aligners")), None)
    # "Aligners" holds in a headline ("Clear Aligners Near You") AND in DESCRIPTIONS[0] ("Clear aligners from a
    # family dentist...") -- every line that holds the quoted text drops, across both kinds, not just the first
    # one found; a third description keeps enough once the matching one goes too (task review fix round 1, item 9)
    review = _in_review_with(
        conn, headlines=FOUR, descriptions=[*DESCRIPTIONS, "Flexible scheduling; book online today."]
    )
    _run(conn)
    # 2 of the Reviewer's own (refusal, accept), +1 from approve()'s own ads_run.launch_now (see the sibling tests
    # above: its first launch step under AGENT_EXEC_GOOGLE_SEARCH=validate is itself a validateOnly call)
    assert launch_review.get(conn, review["id"])["state"] == "passed" and len(calls) == 3
    assert _rows(conn) == [
        ("headline", "Clear Aligners Near You", "ad_text", "dropped"),
        ("description", "Clear aligners from a family dentist. Book online.", "ad_text", "dropped"),
    ]


def test_a_mixed_refusal_drops_the_ad_text_and_returns_the_rest(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    phone = _detail("phone", "6505550100", topic=None, code="assetError: INVALID_PHONE_NUMBER")
    calls = _google(monkeypatch, _refusal(_detail(), phone))
    review = _in_review_with(conn, headlines=FOUR)
    _run(conn)
    back = launch_review.get(conn, review["id"])
    assert (back["state"], back["resolution"]) == ("returned", "phone") and len(calls) == 1
    assert _rows(conn) == [
        ("headline", "Same-Week Appointments", "ad_text", "dropped"),
        ("phone", "6505550100", "business", "returned"),
    ]
    # ruling G5, task review fix round 1: the version never passes this round -- only the return's own mail goes,
    # never the change mail (it waits for the pass)
    assert [m[0] for m in sent] == ["mail_review_return.txt.j2"]
    # resubmitted once the owner answered: reviewed again in full (R17: the Reviewer's own return), the drop still
    # out of the build, the count carried, the owner told of the change exactly once -- at this, its first pass
    again = launch_review.submit(conn, BIZ, review["plan_id"], approval={"kind": "web"})
    calls = _google(monkeypatch, None)
    _run(conn)
    assert launch_review.get(conn, again["review_id"])["state"] == "passed"
    assert calls[0].payload["dropped"].headlines == frozenset({"Same-Week Appointments"})
    assert launch_review.validations(conn, review["plan_id"]) == 2
    assert [m[0] for m in sent] == ["mail_review_return.txt.j2", "mail_review_changed.txt.j2"]


def test_the_configured_material_leaves_the_dropped_lines_out(conn, on, page_ok, monkeypatch):
    """Review Focus 3: the model and `differs` read what is sent -- after the drop -- so a thin ad is "the ad text"."""
    _model(monkeypatch)
    review = _in_review_with(conn)
    plan = plans.get(conn, BIZ, review["plan_id"])
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan["id"],
        channel="google_search",
        review_id=review["id"],
        found=[(_detail(), "ad_text", "dropped")],
    )
    material = reviewer.configured_material(conn, config.load(), BIZ, plan, None)
    assert material["configured"]["headlines"] == ["Invisalign in Palo Alto", "Clear Aligners Near You"]
    assert material["configured"]["removed"] == ["the headline “Same-Week Appointments”"]
    assert reviewer.differs(material) == "the ad text"


def test_the_change_mail_names_the_line_and_the_why_in_owner_words():
    from agent import mail

    one = [
        {
            "what": "the headline “Same-Week Appointments”",
            "why": "a name Google treats as a trademark",
            "when": "2026-10-11T10:00:00+00:00",
        }
    ]
    text = mail.render("mail_review_changed.txt.j2", changes=one, link="http://testserver/?setup=biz_1")
    assert (
        "refused one part of your ads" in text
        and "The headline “Same-Week Appointments”: a name Google treats as a trademark." in text
    )
    assert "Admiry removed it so your ads can run" in text and "nothing you need to do" in text
    assert "http://testserver/?setup=biz_1" in text and "hold your ads instead" in text and "hello@admiry.ai" in text
    assert not JARGON.search(text) and "POLICY" not in text
    two = mail.render(
        "mail_review_changed.txt.j2",
        changes=[*one, {"what": "the search phrase “x”", "why": "Google's rules for search phrases", "when": "y"}],
        link="L",
    )
    assert "refused 2 parts of your ads" in two and "Admiry removed them" in two


def test_the_owner_notes_are_owner_words():
    """The notes and the subject: no jargon, no Google code or raw policy name (an underscore is a code) -- including
    the page-refused note for each of the destination topics the table knows (follow-up 2026-10-12)."""
    notes = [reviewer.PHONE_REFUSED_NOTE, reviewer.BUSINESS_NOTE, reviewer.PAGE_REFUSED_NOTE, reviewer.CHANGED_SUBJECT]
    notes += [reviewer.PAGE_REFUSED_WHY_NOTE.format(why=why) for why in reviewer.PAGE_REFUSED_WHY.values()]
    for text in notes:
        assert not JARGON.search(text) and "POLICY" not in text and "_" not in text, text


# ---------------------------------------------------------------- task review fix round 1


def test_an_admins_decision_mid_call_wins_and_the_call_still_counts(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling G1, from the reviewer's own probe: an admin's decision while Google is still answering the
    Reviewer's own validateOnly call wins at once -- the call itself still counts toward VALIDATIONS_PER_VERSION
    (so a resubmission never makes a fourth or fifth call), but nothing else happens: no drop, no change mail, no
    second call, the run skipped."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    calls: list = []
    review = _in_review_with(conn, headlines=FOUR)

    def launch(self, c, settings, account, build, existing, *, http=None, validate_only):
        calls.append(validate_only)
        if len(calls) == 1:  # the admin decides while Google is still answering the first call
            launch_review.decide(conn, review["id"], state="returned", by="admin:x@y.z", note="Change the offer.")
            raise _refusal(_detail())
        return Resources(ids={})

    monkeypatch.setattr(google.GoogleSearchAdapter, "launch", launch)
    out = _run(conn)
    assert out["skipped"] == 1 and out["done"] == 0
    row = launch_review.get(conn, review["id"])
    assert (row["state"], row["note"]) == ("returned", "Change the offer.")  # the admin's own decision, untouched
    assert launch_review.validations(conn, review["plan_id"]) == 1  # the answered call still counts
    assert len(calls) == 1 and _rows(conn) == [] and sent == []
    assert [s["name"] for s in row["steps"]] == ["creative", "validated"]


def test_a_too_thin_drop_holds_before_any_return_even_with_a_business_fact_too(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling G4 (amends D6): a drop that leaves too little ad text holds for the back office BEFORE any return to
    the owner -- even when the same call also named a business fact only the owner could answer: a too-thin draft
    must never reach the owner's checklist as a return (plan D3)."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    phone = _detail("phone", "6505550100", topic=None, code="assetError: INVALID_PHONE_NUMBER")
    calls = _google(monkeypatch, _refusal(_detail(), phone))
    review = _in_review_with(conn)  # HEADLINES: exactly plans.MIN_HEADLINES -- one drop is already too few
    _run(conn)
    held = launch_review.get(conn, review["id"])
    assert held["state"] == "held" and held["hold_reason"] == reviewer.TOO_LITTLE_AFTER_DROP.format(h=2, d=2)
    assert plans.get(conn, BIZ, review["plan_id"])["status"] == plans.IN_REVIEW  # never back to a draft
    assert len(calls) == 1
    assert _rows(conn) == [
        ("headline", "Same-Week Appointments", "ad_text", "dropped"),
        ("phone", "6505550100", "business", "held"),
    ]
    assert [m[0] for m in sent] == ["mail_review_hold.txt.j2"]  # no change mail (never passes), no return mail


def test_a_quoted_line_the_build_never_sent_is_not_a_drop(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Minor (ruling G6): a line the copy linter already dropped was never in what this call actually sent --
    Google could never really refuse it, but a refusal that happens to quote it anyway is `other`, held for a
    person, never a wasted drop that was never really there to remove."""
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail("headline", "The Best Invisalign Deal")))
    review = _in_review_with(conn, headlines=[*HEADLINES, "The Best Invisalign Deal"])  # "best" is a banned word
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "held" and len(calls) == 1
    assert _rows(conn) == [("headline", "The Best Invisalign Deal", "other", "held")]


def test_a_refused_phrase_the_build_never_sent_is_not_a_drop(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Minor (ruling G6): a search phrase this plan never builds -- no theme, example search or intent phrase makes
    it -- is `other`, held for a person, never a wasted drop."""
    _model(monkeypatch)
    _mails(monkeypatch)
    refused = _detail("keyword", "pizza delivery service", topic=None, code="policyViolationError: POLICY_ERROR")
    _google(monkeypatch, _refusal(refused))
    review = _in_review_with(conn)
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "held"
    assert _rows(conn) == [("keyword", "pizza delivery service", "other", "held")]


def test_the_change_mail_never_goes_twice_for_the_same_version(conn, on, page_ok, monkeypatch):
    """Ruling G5 (amends D10): the change e-mail goes once a plan version, at the pass -- calling the Reviewer's
    own notifier again for the same version never mails twice."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    review = _in_review_with(conn)
    plan = plans.get(conn, BIZ, review["plan_id"])
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan["id"],
        channel="google_search",
        review_id=review["id"],
        found=[(_detail(), "ad_text", "dropped")],
    )
    reviewer._tell_changed(conn, config.load(), review)
    reviewer._tell_changed(conn, config.load(), review)
    assert [m[0] for m in sent] == ["mail_review_changed.txt.j2"]


def test_a_failed_change_mail_at_the_pass_still_records_the_drop_and_passes(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling G5: _tell_changed never raises -- a mail that cannot go out at the pass is only logged, the changed
    step still records mail: "failed" with the drop it would have named, and the pass itself (and the drop already
    recorded) stand."""
    _model(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), None)
    monkeypatch.setattr(reviewer.mail_out, "send_plain", lambda *a, **k: True)

    def boom(*a, **k):
        raise RuntimeError("Resend down")

    monkeypatch.setattr(reviewer.mail, "to_owner", boom)
    review = _in_review_with(conn, headlines=FOUR)
    _run(conn)
    done = launch_review.get(conn, review["id"])
    assert done["state"] == "passed" and len(calls) == 3
    changed = next(s for s in done["steps"] if s["name"] == "changed")
    assert changed["detail"]["mail"] == "failed" and changed["detail"]["changes"]


# ---------------------------------------------------------------- final review fix wave (ruling L1)


def test_a_change_step_that_cannot_be_written_never_undoes_the_pass_or_its_launch(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling L1 (final review, Important 1): `_tell_changed` runs after the launch and never raises -- a database
    error writing its `changed` step, after the pass committed, leaves the review passed, the launch made and the
    job's result the pass, never a hold."""
    _model(monkeypatch)
    _mails(monkeypatch)
    calls = _google(monkeypatch, _refusal(_detail()), None)
    launched: list[str] = []
    real_launch = ads_run.launch_now
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **k: launched.append(biz) or real_launch(biz, **k))
    real_step = launch_review.append_step

    def append_step(conn, review_id, *, name, ok, detail):
        if name == reviewer.CHANGED_STEP:
            raise psycopg.OperationalError("the database went away")
        return real_step(conn, review_id, name=name, ok=ok, detail=detail)

    monkeypatch.setattr(launch_review, "append_step", append_step)
    review = _in_review_with(conn, headlines=FOUR)
    _run(conn)
    done = launch_review.get(conn, review["id"])
    assert done["state"] == "passed" and plans.get(conn, BIZ, review["plan_id"])["status"] == plans.APPROVED
    # 2 of the Reviewer's own, +1 from the launch itself (validate: its first step is a validateOnly call)
    assert launched == [BIZ] and len(calls) == 3
    job = conn.execute("SELECT status, result FROM agent_job WHERE role = %s", (reviewer.ROLE,)).fetchone()
    assert job["status"] == queue.DONE and job["result"]["passed"] is True and "held" not in job["result"]
    assert reviewer.CHANGED_STEP not in [s["name"] for s in done["steps"]]


def test_changes_unread_at_the_pass_write_no_step_and_the_pass_stands(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling L1: when the changes cannot be read at the pass, no `changed` step is written -- an empty one would read
    as told (`launch_review.has_step`), and the owner would never be -- and the pass and its launch stand."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    _google(monkeypatch, _refusal(_detail()), None)
    real = ads_refusal.changes

    def changes(conn, biz, plan_id):
        if plans.get(conn, biz, plan_id)["status"] == plans.APPROVED:  # at the pass: the read fails
            raise psycopg.OperationalError("the database went away")
        return real(conn, biz, plan_id)

    monkeypatch.setattr(ads_refusal, "changes", changes)
    review = _in_review_with(conn, headlines=FOUR)
    _run(conn)
    done = launch_review.get(conn, review["id"])
    assert done["state"] == "passed" and plans.get(conn, BIZ, review["plan_id"])["status"] == plans.APPROVED
    assert reviewer.CHANGED_STEP not in [s["name"] for s in done["steps"]]
    assert "mail_review_changed.txt.j2" not in [m[0] for m in sent]
    # the version is not taken as told: once the changes read again, a later pass of it tells the owner
    monkeypatch.setattr(ads_refusal, "changes", real)
    reviewer._tell_changed(conn, config.load(), done)
    assert [m[0] for m in sent].count("mail_review_changed.txt.j2") == 1
    assert launch_review.has_step(conn, review["plan_id"], reviewer.CHANGED_STEP)


# ---------------------------------------------------------------- final review fix wave (ruling L2)


def test_an_earlier_versions_drop_stays_out_of_the_next_ones_review(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling L2: a line Google refused for v1 (returned to its owner) stays out of v2's validateOnly build too, and
    the configured ads say so -- v2 passes with the headline left out, and the owner is told of it at v2's pass."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    calls = _google(monkeypatch, None)
    _key, v1 = seed(conn, status="superseded", secs=draft_sections(headlines=FOUR))
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=v1,
        channel="google_search",
        review_id=None,
        found=[(_detail(), "ad_text", "dropped")],
    )
    v2 = add_plan(conn, draft_sections(headlines=FOUR, monthly_usd=900.0), status="draft")
    out = launch_review.submit(conn, BIZ, v2, approval={"kind": "web"})
    _run(conn)
    done = launch_review.get(conn, out["review_id"])
    assert done["state"] == "passed"
    assert calls[0].payload["dropped"].headlines == frozenset({"Same-Week Appointments"})
    checked = next(s for s in done["steps"] if s["name"] == "checked")
    assert "Same-Week Appointments" not in checked["detail"]["configured"]["headlines"]
    assert checked["detail"]["configured"]["removed"] == ["the headline “Same-Week Appointments”"]
    (mail,) = [m for m in sent if m[0] == "mail_review_changed.txt.j2"]
    assert [c["what"] for c in mail[2]["changes"]] == ["the headline “Same-Week Appointments”"]


def test_a_version_an_earlier_drop_leaves_too_thin_is_held_before_any_call(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - gads is a fixture used by name
    """Ruling L2's too-thin check, the Reviewer's own build: v2 has exactly three headlines, v1's refused one among
    them -- two once it is left out -- so it is held for the back office as too little after Google's refusals
    (TOO_LITTLE_AFTER_DROP, never "the copy linter"), before any call to Google, and never returned to the owner."""
    _model(monkeypatch)
    sent = _mails(monkeypatch)
    calls = _google(monkeypatch, None)
    _key, v1 = seed(conn, status="superseded", secs=draft_sections(headlines=FOUR))
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=v1,
        channel="google_search",
        review_id=None,
        found=[(_detail(), "ad_text", "dropped")],
    )
    v2 = add_plan(conn, draft_sections(), status="draft")  # HEADLINES: three, "Same-Week Appointments" among them
    out = launch_review.submit(conn, BIZ, v2, approval={"kind": "web"})
    _run(conn)
    held = launch_review.get(conn, out["review_id"])
    assert held["state"] == "held" and held["hold_reason"] == reviewer.TOO_LITTLE_AFTER_DROP.format(h=2, d=2)
    assert calls == [] and plans.get(conn, BIZ, v2)["status"] == plans.IN_REVIEW
    assert [m[0] for m in sent] == ["mail_review_hold.txt.j2"]
