"""Role contracts and the Critic's plan rubric (spec 2026-10-07-admiry-capability-layer-design §4, §7, K1): the six
dimensions the founder grades, the grades clamped, the score and the verdict computed in code."""

import pytest

from agent import models
from agent.roles import base, contracts

PLAN = contracts.PLAN


def test_the_plan_rubric_is_the_six_dimensions_the_founder_grades():
    assert PLAN.ids == ("fit", "budget", "copy", "keywords", "estimate", "clarity")
    assert PLAN.threshold == 0.7 and all(d.definition.endswith(".") for d in PLAN.dimensions)
    assert contracts.RUBRICS == {
        "plan": PLAN,
        "insight": contracts.INSIGHT,
        "research": contracts.RESEARCH,
    }  # K2 the Analyst's, K4 the Researcher's


def test_grades_are_clamped_first_kept_and_a_missing_dimension_counts_as_one():
    raw = [
        {"dimension": "fit", "grade": 7},
        {"dimension": "fit", "grade": 2},  # named twice: the first stands
        {"dimension": "budget", "grade": 0},
        {"dimension": "copy", "grade": "4"},
        {"dimension": "keywords", "grade": None},
        {"dimension": "colour", "grade": 5},  # not the rubric's
    ]
    assert base.grades_of(raw, PLAN) == {"fit": 5, "budget": 1, "copy": 4, "keywords": 1, "estimate": 1, "clarity": 1}


def test_the_score_is_the_mean_of_the_grades_on_zero_to_one():
    assert base.score(dict.fromkeys(PLAN.ids, 5), PLAN) == 1.0
    assert base.score(dict.fromkeys(PLAN.ids, 1), PLAN) == 0.0
    assert base.score({**dict.fromkeys(PLAN.ids, 4), "clarity": 2}, PLAN) == 0.67  # (5 × 0.75 + 0.25) / 6


@pytest.mark.parametrize(
    ("grades", "missing", "want"),
    [
        (dict.fromkeys(PLAN.ids, 4), [], base.DELIVER),  # 0.75 reaches 0.7, nothing at the floor
        ({**dict.fromkeys(PLAN.ids, 5), "copy": 2}, [], base.REVISE),  # 0.88, but one real problem
        (dict.fromkeys(PLAN.ids, 3), [], base.REVISE),  # 0.5
        (dict.fromkeys(PLAN.ids, 3), ["the trade's licence rules"], base.FETCH_MORE),
    ],
)
def test_the_verdict(grades, missing, want):
    assert base.verdict(grades, PLAN, missing) == want


def test_the_rubric_reads_each_dimension_its_checks_then_the_anchors():
    lines = base.rubric_text(PLAN).splitlines()
    assert lines[0] == f"- fit (Services and area): {PLAN.dimensions[0].definition}"
    assert len(lines) == 2 * len(PLAN.ids) + 2 and lines[-2].startswith("Grades: 5 = ")


def test_every_contract_names_a_rubric_that_exists_and_a_schema_the_model_fills():
    assert set(contracts.CONTRACTS) == {"strategist", "analyst", "researcher", "critic"}
    for c in contracts.CONTRACTS.values():
        assert c.rubric is None or contracts.RUBRICS[c.rubric.name] is c.rubric
        assert c.max_calls >= 1 and c.max_rounds >= 1 and issubclass(c.output, models.BaseModel)
