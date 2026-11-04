"""The Analyst's two model calls (capability layer K2, spec 2026-10-07 §4): write_insight and review_insight on the
wire, the Insight and InsightReview schemas, the insight rubric with its own anchors, the generator's effort."""

import json
import typing

import pytest

from agent import config, llm, models
from agent.roles import base, contracts, critic
from agent.tests.fixtures import FakeGenerator, insight_fixture, insight_review_fixture
from agent.tests.test_llm import body, completion, generator


def test_the_insight_schemas_fit_the_api_subset():
    for m in (models.Insight, models.InsightReview):
        s = m.model_json_schema()
        assert s["type"] == "object" and "enum" not in s and "anyOf" not in s
        assert "allOf" not in json.dumps(s) and "oneOf" not in json.dumps(s)
        assert set(s["required"]) == set(s["properties"])  # the model decides every field (models._all_required)


def test_insight_dimensions_are_the_rubrics():
    assert typing.get_args(models.InsightDimension) == contracts.INSIGHT.ids
    assert contracts.CONTRACTS["analyst"].rubric is contracts.INSIGHT
    assert contracts.RUBRICS["insight"] is contracts.INSIGHT


def test_each_rubric_reads_its_own_anchors():
    assert "5 = a marketer would launch it as it is" in base.rubric_text(contracts.PLAN)
    insight = base.rubric_text(contracts.INSIGHT)
    assert "5 = a marketer would send it to the owner as it is" in insight and "launch" not in insight


def test_write_insight_posts_the_material_and_the_feedback():
    sent: list = []
    gen = generator(completion(insight_fixture().model_dump_json()), sent=sent, effort="medium")
    out = gen.write_insight({"day": "Monday, October 5"}, ["summary: it was too long"])
    assert out == insight_fixture()
    b = body(sent[0])
    assert b["response_format"]["json_schema"]["name"] == "Insight"
    assert b["messages"][0] == {"role": "developer", "content": llm.SYSTEM_INSIGHT}
    assert b["reasoning_effort"] == "medium"
    user = b["messages"][1]["content"]
    assert user.startswith("<material>") and "<feedback>" in user and "too long" in user


def test_write_insight_without_feedback_sends_no_feedback_tag():
    sent: list = []
    generator(completion(insight_fixture().model_dump_json()), sent=sent).write_insight({"day": "x"})
    assert "<feedback>" not in body(sent[0])["messages"][1]["content"]


def test_review_insight_posts_the_rubric_and_the_insight():
    sent: list = []
    generator(completion(insight_review_fixture().model_dump_json()), sent=sent).review_insight(
        {"day": "x"}, {"summary": "Quiet day."}
    )
    b = body(sent[0])
    assert b["response_format"]["json_schema"]["name"] == "InsightReview"
    system = b["messages"][0]["content"]
    assert system == llm.SYSTEM_INSIGHT_REVIEW and all(f"- {d} (" in system for d in contracts.INSIGHT.ids)
    assert "<insight>" in b["messages"][1]["content"]


def test_the_generator_takes_an_effort(monkeypatch):
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    assert llm.generator(config.load(), effort="medium").effort == "medium"
    assert llm.generator(config.load()).effort == config.load().llm_effort


def _fake(monkeypatch, **kw) -> FakeGenerator:
    gen = FakeGenerator(**kw)
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    return gen


def test_judge_insight_scores_and_delivers(monkeypatch):
    gen = _fake(monkeypatch)
    j = critic.judge_insight(config.load(), {"day": "x"}, {"summary": "Quiet day."})
    assert (j.score, j.verdict, j.issues) == (1.0, base.DELIVER, [])
    assert gen.insight_review_calls == [{"material": {"day": "x"}, "insight": {"summary": "Quiet day."}}]


def test_a_low_grade_asks_for_a_revision(monkeypatch):
    _fake(monkeypatch, insight_reviews=[insight_review_fixture(low={"priority": 2})])
    j = critic.judge_insight(config.load(), {}, {})
    assert j.verdict == base.REVISE and j.issues == [{"dimension": "priority", "text": "priority note"}]


def test_missing_material_asks_for_more(monkeypatch):
    _fake(monkeypatch, insight_reviews=[insight_review_fixture(low={"fit": 2}, missing=["what the leads became"])])
    assert critic.judge_insight(config.load(), {}, {}).verdict == base.FETCH_MORE


def test_insight_grades_are_read_leniently():
    r = models.InsightReview.model_validate(
        {
            "grades": [
                {"dimension": " Grounded ", "grade": 5, "note": "ok"},
                {"dimension": "speed", "grade": 5, "note": "x"},
                {"dimension": "fit", "grade": "five", "note": "x"},
            ],
            "missing": ["a", 3],
        }
    )
    assert [g.dimension for g in r.grades] == ["grounded"] and r.missing == ["a"]


@pytest.mark.parametrize("error", ["insight_error", "insight_review_error"])
def test_the_fake_fails_each_call_on_its_own(monkeypatch, error):
    gen = _fake(monkeypatch, **{error: "the assistant is busy; try again in a minute"})
    calls = {"insight_error": lambda: gen.write_insight({}), "insight_review_error": lambda: gen.review_insight({}, {})}
    with pytest.raises(llm.LLMError):
        calls[error]()
