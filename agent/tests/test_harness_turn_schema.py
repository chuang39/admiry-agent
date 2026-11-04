"""The harness's one model call (spec 2026-10-10 K6 slice 1 §4.3): the Turn's shape on the wire, what read_turn sends,
the stage skill's size and the fake that stands in for the call."""

import json

import pydantic
import pytest

from agent import copy, llm, models
from agent.harness import skills
from agent.knowledge_index import tokens
from agent.operations import NEVER_SAY
from agent.tests.fixtures import FakeGenerator, turn_fixture
from agent.tests.test_llm import body, completion, generator


def test_every_turn_field_is_required_on_the_wire_and_the_intents_are_the_twelve():
    schema = models.Turn.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert tuple(schema["properties"]["intent"]["enum"]) == models.TURN_INTENTS
    assert models.TURN_INTENTS == (
        "answer",
        "go_to",
        "start_over",
        "budget",
        "build",
        "explain",
        "ask",
        "skip",
        "smalltalk",
        "support",
        "not_mine",
        "unclear",
    )
    assert "allOf" not in json.dumps(schema) and "oneOf" not in json.dumps(schema)
    without_services = {k: v for k, v in turn_fixture().model_dump().items() if k != "services"}
    with pytest.raises(pydantic.ValidationError, match="services"):
        models.Turn.model_validate_json(json.dumps(without_services))


def test_read_turn_sends_the_material_as_data_the_skill_after_the_rules_and_parses_the_shape():
    sent: list = []
    reply = completion(json.dumps(turn_fixture(intent="go_to", question_id="objective", say="Of course.").model_dump()))
    read = generator(reply, sent=sent).read_turn(
        {"consultation": {"status": "open"}}, "back to the goal question", skill="# Intake\n"
    )
    assert read.intent == "go_to" and read.question_id == "objective" and read.say == "Of course."
    system, user = (m["content"] for m in body(sent[0])["messages"])
    assert system.startswith(llm.SYSTEM_TURN) and system.endswith("# Intake\n") and NEVER_SAY in system
    assert user.index("<material>") < user.index("<owner>") and "back to the goal question" in user
    assert body(sent[0])["response_format"]["json_schema"]["name"] == "Turn"


def test_the_prompt_holds_the_rules_the_spec_names():
    for words in (
        "never invent an id or a number",
        "may_go_to",
        "a daily amount × 30",
        "only when the consultation is ready",
        "below 0.6",
        "no advertising jargon",
        "no promise about results",
    ):
        assert words in llm.SYSTEM_TURN, words


def test_a_turns_reading_is_an_answer_read_understood_only_when_it_answers():
    t = turn_fixture(intent="answer", option_id="calls", support="human", support_summary="Wants a person.")
    r = t.reading()
    assert isinstance(r, models.AnswerRead)
    assert (r.option_id, r.understood, r.support, r.support_summary) == ("calls", True, "human", "Wants a person.")
    assert turn_fixture(intent="skip").reading().understood is False


def test_the_stage_skill_is_short_and_names_every_question_and_the_rules():
    text = skills.load("before_plan")
    assert tokens(text) <= 1000
    for qid in (
        "website",
        "business_name",
        "listing",
        "services_offered",
        "objective",
        "service",
        "customer_value",
        "location",
        "radius",
    ):
        assert qid in text, qid
    for words in ("may_go_to", "ready", "budget", "start over", "another business"):
        assert words in text, words
    assert copy.jargon(text) is None and "convert" not in text  # no ad jargon in the skill (final review M7)
    assert skills.STAGES == ("before_plan",)
    with pytest.raises(KeyError):
        skills.load("after_launch")


def test_the_fake_answers_the_turn_a_test_gave_for_the_words_else_unclear():
    gen = FakeGenerator(turns={"restart": turn_fixture(intent="start_over", confidence=0.9)})
    assert gen.read_turn({}, "restart", skill="s").intent == "start_over"
    other = gen.read_turn({"a": 1}, "mumble", skill="s")
    assert other.intent == "unclear" and other.confidence < 0.6
    assert gen.turn_calls == [
        {"material": {}, "text": "restart", "skill": "s"},
        {"material": {"a": 1}, "text": "mumble", "skill": "s"},
    ]
    with pytest.raises(llm.LLMError):
        FakeGenerator(error="busy").read_turn({}, "x", skill="s")
