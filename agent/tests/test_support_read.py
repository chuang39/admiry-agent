"""The support read (spec 2026-09-30 §4.1, D131): two fields on every read of the owner's own words -- required on the
wire (the model decides each one), defaulted in Python (reads built by hand stay valid) -- and read_support's own call."""

import json

import pytest

from agent import llm, models
from agent.tests.test_llm import body, completion, generator

SHAPES = [models.AnswerRead, models.AdjustRead, models.MailRead, models.SupportRead]


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.__name__)
def test_the_support_fields_are_required_on_the_wire(shape):
    schema = shape.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"]) >= {"support", "support_summary"}
    assert schema["properties"]["support"]["enum"] == ["none", "help", "human", "dissatisfied"]
    assert "allOf" not in json.dumps(schema) and "oneOf" not in json.dumps(schema)  # the API's subset
    assert "default" not in schema["properties"]["support"]
    assert "default" not in schema["properties"]["support_summary"]


LISTING_NOT_MINE = (
    "true when the words say the Google Maps place or listing linked to the business is a different business or is "
    'not theirs; false otherwise, also when "not my business" means not their concern, when they ask to change where '
    "the ads show or only dislike the plan."
)
# ... and the bullet every reader's prompt carries (SUPPORT_RULE; Task 1 fix round 1, the controller's ruling; final
# review M1: "not my business" is also an idiom for "not my concern")
LISTING_NOT_MINE_RULE = (
    "- listing_not_mine is true only when the words say the Google Maps place or listing linked to the business is a "
    'different business or is not theirs; false when "not my business" means not their concern, when they ask to '
    "change where the ads show, when they dislike the plan, and for anything else."
)


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.__name__)
def test_every_read_of_the_owners_words_says_whether_the_listing_is_not_theirs(shape):
    """Plan 2026-10-03-agent-listing-not-mine-words, D1: one more field on the same call, decided by the model like the
    others (required on the wire, no default there), false in Python for a read built by hand."""
    schema = shape.model_json_schema()
    prop = schema["properties"]["listing_not_mine"]
    assert prop["type"] == "boolean" and "default" not in prop and "listing_not_mine" in schema["required"]
    assert prop["description"] == LISTING_NOT_MINE
    assert shape.model_fields["listing_not_mine"].default is False


def test_a_read_built_by_hand_asks_for_nothing():
    read = models.AdjustRead(
        monthly_budget_usd=None,
        level=None,
        radius_miles=None,
        offer=None,
        objective=None,
        channel=None,  # M0's per-channel amount: required like the other fields, only the support fields have defaults
        channel_monthly_usd=None,
        understood=False,
        asks_for_change=False,  # required too (final review B, Important 8)
    )
    assert read.support == "none" and read.support_summary is None and read.listing_not_mine is False


def test_read_support_sends_the_words_as_data_and_parses_the_shape():
    sent: list = []
    reply = completion('{"support": "human", "support_summary": "Wants a call about billing."}')
    read = generator(reply, sent=sent).read_support("can someone call me")
    assert read.support == "human" and read.support_summary == "Wants a call about billing."
    b = body(sent[0])
    assert b["response_format"]["json_schema"]["name"] == "SupportRead"
    assert "<owner>\ncan someone call me\n</owner>" in b["messages"][1]["content"]
    assert "never dissatisfied" in b["messages"][0]["content"]


@pytest.mark.parametrize("system", ["SYSTEM_READ", "SYSTEM_ADJUST", "SYSTEM_MAIL", "SYSTEM_SUPPORT"])
def test_every_read_of_the_owner_s_words_carries_the_support_rule(system):
    assert llm.SUPPORT_RULE in getattr(llm, system)


@pytest.mark.parametrize("system", ["SYSTEM_READ", "SYSTEM_ADJUST", "SYSTEM_MAIL", "SYSTEM_SUPPORT"])
def test_every_reader_is_told_what_not_my_business_means(system):
    """The field is explained in the prompt too, as the support fields are, and an area change or a complaint is
    named as not it (Task 1 fix round 1)."""
    assert LISTING_NOT_MINE_RULE in " ".join(llm.SUPPORT_RULE.split())
    assert LISTING_NOT_MINE_RULE in " ".join(getattr(llm, system).split())


def test_a_reply_without_the_fields_still_parses():
    reply = completion(
        '{"option_id": "calls", "amount_usd": null, "miles": null, "note": null, "understood": true, "services": []}'
    )
    read = generator(reply).read_answer({"id": "objective", "prompt": "?", "input": "single"}, [], "calls")
    assert read.support == "none"
