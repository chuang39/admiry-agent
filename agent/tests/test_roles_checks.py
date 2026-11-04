"""K5 (P2b, P2c): the plan rubric's checks -- yes/no questions the judge answers per dimension before its grade -- and
the caps code applies to a dimension that fails one; the insight rubric unchanged."""

import json

from agent import config, llm, models
from agent.roles import base, contracts, critic
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_review_launch import REVIEW

PLAN = contracts.PLAN


def test_every_plan_dimension_has_checks_and_the_five_a_flaw_breaks_have_a_serious_one():
    for d in PLAN.dimensions:
        ids = [c.id for c in d.checks]
        assert 2 <= len(ids) <= 4 and len(set(ids)) == len(ids), d.id
        assert all(";" not in c.text and c.text == c.text.strip() for c in d.checks), d.id
    assert {d.id for d in PLAN.dimensions if any(c.serious for c in d.checks)} == {
        "fit",
        "budget",
        "copy",
        "keywords",
        "estimate",
    }


def test_a_failed_check_caps_the_grade_whatever_the_judge_said():
    raw = [
        {"dimension": "copy", "grade": 5, "failed": ["rules"]},  # serious: a real problem
        {"dimension": "keywords", "grade": 5, "failed": ["negatives"]},  # not serious: change something first
        {"dimension": "fit", "grade": 4, "failed": ["colour", 7, None]},  # no check of fit's: no cap
        {"dimension": "budget", "grade": 1, "failed": ["limit"]},  # already under the cap
        {"dimension": "estimate", "grade": 5, "failed": "basis"},  # not a list: names nothing
        {"dimension": "clarity", "grade": 5, "failed": []},
        {"dimension": "copy", "grade": 5, "failed": []},  # named twice: the first stands
    ]
    assert base.grades_of(raw, PLAN) == {"fit": 4, "budget": 1, "copy": 2, "keywords": 3, "estimate": 5, "clarity": 5}
    assert base.failed_of(raw, PLAN) == {"copy": ["rules"], "keywords": ["negatives"], "budget": ["limit"]}
    assert base.cap(PLAN.dimension("copy"), ["specific", "claims"]) == base.SERIOUS_CAP == 2
    assert base.cap(PLAN.dimension("copy"), ["specific"]) == base.CHECK_CAP == 3


def test_the_insight_rubric_reads_as_it_did_before_checks():
    insight = contracts.INSIGHT
    before = "\n".join(
        [
            *(f"- {d.id} ({d.label}): {d.definition}" for d in insight.dimensions),
            "Grades: " + "; ".join(insight.anchors) + ".",
        ]
    )
    assert base.rubric_text(insight) == before and all(not d.checks for d in insight.dimensions)


def test_the_plan_rubric_reads_each_dimension_then_its_checks_then_the_anchors_and_the_cap():
    lines = base.rubric_text(PLAN).splitlines()
    assert lines[0] == f"- fit (Services and area): {PLAN.dimensions[0].definition}"
    assert lines[1].startswith("  Checks -- service (serious): every search phrase and every headline")
    assert lines[-2].startswith("Grades: 5 = ") and lines[-1] == base.CHECKS_RULE
    assert len(lines) == 2 * len(PLAN.ids) + 2


def test_the_judge_is_told_to_answer_the_checks_first_and_to_read_the_ads():
    prompt = " ".join(llm.SYSTEM_REVIEW.split())
    for words in (
        "(`failed`: the ids of the checks it fails)",
        "5 means you would change nothing in that dimension",
        "not by the few lines the cards preview",
        "the phrases left out and all of its ad text with what the launch's linter would drop (ads)",
    ):
        assert words in prompt, words
    assert " ".join(base.rubric_text(PLAN).split()) in prompt


def test_a_line_the_launch_drops_is_never_failed_on_by_the_owners_look_or_the_copy_checks():
    """K5 final review: the material lists the lines plan_words.lint drops (`lint`, under `ads`); they never run, so
    neither the owner-facing compliance check nor a copy check may fail the plan on one."""
    prompt = " ".join(llm.SYSTEM_REVIEW.split())
    assert "Lines the linter drops (lint, under ads) never run: never fail compliance or a copy check on one" in prompt
    assert "mention one only to say the launch drops it" in prompt
    copy = PLAN.dimension("copy")
    for check_id in ("claims", "rules"):
        text = next(c.text for c in copy.checks if c.id == check_id)
        assert "among the lines the launch keeps" in text, check_id
    assert "among the lines the launch keeps" in prompt  # the rubric is in the prompt word for word


def test_a_plan_grade_reads_its_failed_checks_leniently_and_the_issue_names_them():
    from agent.tests.test_llm import completion, generator

    owner_part = REVIEW.model_dump(exclude={"grades", "missing"})
    answer = {
        **owner_part,
        "grades": [
            {"dimension": "copy", "failed": ["Rules", 3, {"x": 1}], "grade": 5, "note": "promises a result"},
            {"dimension": "fit", "failed": "service", "grade": 4, "note": "right service"},
        ],
        "missing": [],
    }
    review = generator(completion(json.dumps(answer))).review_plan({})
    assert [(g.dimension, g.failed) for g in review.grades] == [("copy", ["rules"]), ("fit", [])]
    got = critic.judged(review)
    assert got.grades["copy"] == 2 and got.grades["fit"] == 4
    assert {"dimension": "copy", "text": "promises a result", "failed": ["rules"]} in got.issues
    assert models.PlanGrade.model_json_schema()["required"] == ["dimension", "failed", "grade", "note"]


def test_judge_plan_asks_for_an_effort_only_when_given_one(monkeypatch):
    seen = []

    def gen(settings=None, *, effort=None):
        seen.append(effort)
        return FakeGenerator()

    monkeypatch.setattr(llm, "generator", gen)
    critic.judge_plan(config.load(), {})
    critic.judge_plan(config.load(), {}, effort="medium")
    assert seen == [None, "medium"]
