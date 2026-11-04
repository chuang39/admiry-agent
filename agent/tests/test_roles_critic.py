"""K1 (spec 2026-10-07-admiry-capability-layer-design §4, §4.2): every second look is a Critic run -- the plan
rubric's grades from the look's own model call, the score and the verdict computed in code, stored with the look; the
owner never reads them."""

import json

import pytest
from psycopg.types.json import Jsonb

from agent import config, llm, models, plan_review, plans
from agent.roles import base, contracts, critic
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_ads_run_channels import add_plan_by_hand
from agent.tests.test_review_launch import OWNER, REVIEW, _funded_on_the_card, draft_sections, funded_prepaid

BY = plan_review.By.owner(**OWNER)


@pytest.fixture
def fake(monkeypatch):
    """The model faked for the second look: REVIEW, every dimension graded 4."""
    gen = FakeGenerator(reviews=[REVIEW])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    return gen


def graded(**grades) -> models.PlanReview:
    """REVIEW with these grades (every other dimension 4), each with a note naming its dimension, nothing missing."""
    want = {**dict.fromkeys(contracts.PLAN.ids, 4), **grades}
    marks = [models.PlanGrade(dimension=d, grade=g, note=f"{d} note") for d, g in want.items()]
    return REVIEW.model_copy(update={"grades": marks, "missing": []})


def version(conn, **kw) -> dict:
    return plans.create_version(
        conn, "biz_1", goal_id=None, sections=draft_sections(**kw), estimate_basis={}, rationale="", notes=[]
    )


def test_the_judge_reads_the_plan_rubric_word_for_word():
    prompt = " ".join(llm.SYSTEM_REVIEW.split())
    assert " ".join(base.rubric_text(contracts.PLAN).split()) in prompt
    assert "For Admiry's team only, never shown to the owner" in prompt
    assert models.PlanDimension.__args__ == contracts.PLAN.ids


def test_the_critic_turns_the_grades_into_a_score_a_verdict_and_the_issues():
    got = critic.judged(graded())
    assert (got.score, got.verdict, got.issues) == (0.75, base.DELIVER, [])
    got = critic.judged(graded(copy=2, keywords=3))
    assert got.verdict == base.REVISE
    assert got.issues == [
        {"dimension": "copy", "text": "copy note"},
        {"dimension": "keywords", "text": "keywords note"},
    ]


def test_a_dimension_the_model_left_out_is_an_issue_graded_one():
    partial = REVIEW.model_copy(
        update={
            "grades": [models.PlanGrade(dimension="fit", grade=5, note="right service")],
            "missing": ["the trade's licence rules"],
        }
    )
    got = critic.judged(partial)
    assert got.grades["fit"] == 5 and got.grades["clarity"] == 1
    assert {"dimension": "clarity", "text": "not graded"} in got.issues and got.verdict == base.FETCH_MORE


def test_a_judge_answer_with_mangled_or_missing_grades_still_gives_the_owner_the_look():
    """Final review I1: the team-only fields never take the owner's summary down -- an answer without `grades` or
    `missing` reads as ungraded, and a grade the judge mangled (a word for a number, a fraction, another case, a
    dimension the rubric does not have) is dropped and counted as not graded."""
    from agent.tests.test_llm import completion, generator

    owner_part = REVIEW.model_dump(exclude={"grades", "missing"})
    review = generator(completion(json.dumps(owner_part))).review_plan({})
    assert (review.summary, review.grades, review.missing) == (REVIEW.summary, [], [])
    mangled = {
        **owner_part,
        "grades": [
            {"dimension": "Fit", "grade": 5, "note": "right service"},
            {"dimension": "copy", "grade": "four", "note": "?"},
            {"dimension": "budget", "grade": 4.5, "note": "?"},
            {"dimension": "colour", "grade": 5, "note": "?"},
            "not a grade",
        ],
        "missing": None,
    }
    review = generator(completion(json.dumps(mangled))).review_plan({})
    assert [(g.dimension, g.grade) for g in review.grades] == [("fit", 5)] and review.missing == []
    got = critic.judged(review)
    assert (
        got.grades["fit"] == 5 and got.grades["copy"] == 1 and {"dimension": "copy", "text": "not graded"} in got.issues
    )
    assert models.PlanReview.model_json_schema()["required"][-2:] == ["grades", "missing"]  # still asked for


def test_a_look_whose_judge_gave_no_grades_still_shows_the_owner_and_is_stored_ungraded(conn, monkeypatch):
    bare = models.PlanReview.model_validate(REVIEW.model_dump(exclude={"grades", "missing"}))
    gen = FakeGenerator(reviews=[bare])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    funded_prepaid(conn)
    add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    looked = plan_review.look(conn, config.load(), "biz_1", by=BY)
    assert (looked.owner["summary"], looked.owner["model"]) == (REVIEW.summary, "ok")
    assert (looked.score, looked.verdict) == (0.0, "revise")


def test_every_second_look_is_a_critic_run_with_a_stored_score(conn, fake):
    funded_prepaid(conn)
    plan_id = add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    looked = plan_review.look(conn, config.load(), "biz_1", by=BY)
    row = conn.execute("SELECT id, score, verdict, actor FROM plan_review WHERE plan_id = %s", (plan_id,)).fetchone()
    assert (row["score"], row["verdict"], row["actor"]) == (0.75, "deliver", "web:owner@example.com")
    assert (looked.review_id, looked.score, looked.verdict) == (row["id"], 0.75, "deliver")
    audit = conn.execute("SELECT result FROM agent_action WHERE tool = 'web.review_plan'").fetchone()["result"]
    assert audit == {"model": "ok", "grounded": True, "review_id": row["id"]}  # no score in what get_audit_log reads


def test_the_owner_never_reads_a_grade_a_score_or_a_verdict(client, conn, google, fake):
    from agent.tests.test_setup_api import setup_call

    csrf, _plan_id = _funded_on_the_card(client, conn, google)
    said = json.dumps(setup_call(client, "review", business_id="biz_1", csrf=csrf).json())
    for internal in ('"grades"', '"score"', '"verdict"', '"missing"', '"note"'):
        assert internal not in said, internal
    owner = plan_review.review(conn, config.load(), "biz_1", by=BY)
    assert not {"grades", "score", "verdict", "missing"} & set(owner)


def test_a_look_stored_before_the_critic_is_judged_again(conn, fake):
    funded_prepaid(conn)
    plan_id = add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    conn.execute(
        "INSERT INTO plan_review (id, plan_id, business_id, balance_cents, blocked, review, model) "
        "VALUES ('rv_old', %s, 'biz_1', 126000, NULL, %s, 'm')",
        (plan_id, Jsonb(REVIEW.model_dump())),
    )
    looked = plan_review.look(conn, config.load(), "biz_1", by=BY)
    assert len(fake.review_calls) == 1 and looked.review_id != "rv_old" and looked.score == 0.75


def test_the_background_look_judges_the_version_it_names_unless_a_newer_one_superseded_it(conn, fake):
    funded_prepaid(conn)
    first = version(conn)
    job = plan_review.By.job("critic")
    assert plan_review.look(conn, config.load(), "biz_1", by=job, plan_id=first["id"]).score == 0.75
    row = conn.execute("SELECT actor, actor_class FROM agent_action WHERE tool = 'job.critic'").fetchone()
    assert (row["actor"], row["actor_class"]) == ("job:critic", "job")
    version(conn, monthly_usd=1500.0)
    assert plan_review.look(conn, config.load(), "biz_1", by=job, plan_id=first["id"]) is None
