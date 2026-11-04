"""The harness's fast path (spec 2026-10-10 K6 slice 1 §4.1): words that start over, read in code; what a skip picks."""

import pytest

from agent import consult
from agent.harness import fast


@pytest.mark.parametrize(
    "words",
    [
        "i wanna restart",
        "Start over",
        "can we start over?",
        "Let's start again.",
        "I'd like to start from scratch",
        "restart please",
        "begin again",
        "start from the beginning",
        "Can I start afresh",
        "let me start over, thanks",
    ],
)
def test_words_that_ask_to_start_over_read_as_such(words):
    assert fast.says_start_over(words)


@pytest.mark.parametrize(
    "words",
    ["restart my membership", "we help people restart their fitness", "training", "", "start over the weekend", "no"],
)
def test_an_answer_with_those_words_inside_it_is_no_start_over(words):
    assert not fast.says_start_over(words)


def test_a_skip_picks_the_any_or_estimate_option_of_a_choice_question_and_none_otherwise():
    q = {x["id"]: x for x in consult.tree_questions(consult.load_tree("home_services"))}
    assert fast.skip_option(q["objective"], q["objective"]["options"]) == "any"
    assert fast.skip_option(q["customer_value"], q["customer_value"]["options"]) == "estimate"
    assert fast.skip_option(q["radius"], q["radius"]["options"]) is None
    assert fast.skip_option(q["service"], q["service"]["options"]) is None
    assert fast.skip_option(q["location"], []) is None and fast.skip_option(q["business_name"], []) is None
    assert fast.skip_option(q["listing"], [{"id": "any", "label": "x"}]) is None  # the place is never skipped by words
