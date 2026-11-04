"""plan_words: the lines of a words edit cleaned, each platform's linter and minimums, and the words swapped into a
version's sections (spec 2026-10-06-admiry-your-ads-page-design.md §4, Y8)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from agent import plan_words, plans
from agent.channels.newsbreak import adapter as newsbreak_adapter

GOOGLE = {
    "channel": "google_search",
    "monthly_usd": 1700.0,
    "themes": ["invisalign"],
    "negatives": [],
    "headlines": ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners Near You"],
    "descriptions": ["Clear aligners from a family dentist. Book online.", "Straighter teeth without metal braces."],
    "landing_service": "Invisalign",
    "role": "primary",
}
NEWSBREAK = {**GOOGLE, "channel": "newsbreak", "monthly_usd": 300.0, "role": "secondary", "copy_of": "google_search"}


def _sections(*entries: dict) -> dict:
    return {"ads": {"channels": [dict(e) for e in entries]}, "consultation": {"business": {"category": "dentists"}}}


def test_clean_lines_trims_drops_blanks_and_duplicates_and_keeps_the_order():
    text = "  Invisalign in Palo Alto \r\n\r\nSame-Week\tAppointments\r\nInvisalign in Palo Alto\n   \nClear Aligners"
    assert plan_words.clean_lines(text) == ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners"]
    assert plan_words.clean_lines("") == [] and plan_words.clean_lines(None) == []


def test_clean_lines_caps_the_field_before_anything_else():
    assert plan_words.clean_lines("a\n" * 5000) == ["a"]
    long = "x" * (plan_words.FIELD_MAX + 50)
    assert plan_words.clean_lines(long) == ["x" * plan_words.FIELD_MAX]


def test_google_lint_keeps_the_passing_lines_and_names_each_dropped_one():
    kept_h, kept_d, notes = plan_words.lint(
        "google_search",
        "dentists",
        ["Call now!!", "Invisalign in Palo Alto", "A headline that is far too long for Google Search"],
        ["Clear aligners from a family dentist.", "Call 650-555-0100 today"],
    )
    assert kept_h == ["Invisalign in Palo Alto"] and kept_d == ["Clear aligners from a family dentist."]
    assert notes == [
        'Dropped a headline ("Call now!!"): it has an exclamation mark.',
        'Dropped a headline ("A headline that is far too long for Google Search"): it is over 30 characters.',
        'Dropped a description ("Call 650-555-0100 today"): it includes a phone number or e-mail address.',
    ]


def test_newsbreak_lint_uses_its_own_lengths_and_placeholder_rule():
    kept_h, kept_d, notes = plan_words.lint(
        "newsbreak",
        "dentists",
        ["A headline that is far too long for Google Search", "{city} dentists {City}"],
        ["Straighter teeth without metal braces."],
    )
    assert kept_h == ["A headline that is far too long for Google Search"]  # 49 characters: fine for NewsBreak's 90
    assert kept_d == ["Straighter teeth without metal braces."]
    assert notes == ['Dropped a headline ("{city} dentists {City}"): it uses the city placeholder more than once.']


def test_check_counts_refuses_too_few_with_the_notes_first_and_too_many():
    with pytest.raises(ToolError) as e:
        plan_words.check_counts("google_search", ["One", "Two"], ["A", "B"], ['Dropped a headline ("x"): it y.'])
    assert str(e.value) == 'Dropped a headline ("x"): it y. Keep at least 3 headlines and 2 descriptions that pass'
    with pytest.raises(ToolError) as e:
        plan_words.check_counts("newsbreak", [], ["A"], [])
    assert str(e.value) == newsbreak_adapter.TOO_LITTLE_COPY
    with pytest.raises(ToolError) as e:
        plan_words.check_counts("google_search", [f"H{i}" for i in range(16)], ["A", "B"], [])
    assert str(e.value) == "At most 15 headlines and 4 descriptions"
    plan_words.check_counts("google_search", ["One", "Two", "Three"], ["A", "B"], [])  # the minimum passes
    plan_words.check_counts("newsbreak", ["One"], ["A"], [])


def test_limits_carry_each_platforms_numbers_and_notes():
    g, nb = plan_words.limits("google_search"), plan_words.limits("newsbreak")
    assert (g.h_min, g.d_min, g.h_max, g.d_max) == (plans.MIN_HEADLINES, plans.MIN_DESCRIPTIONS, 15, 4)
    assert g.headline_note == "At most 30 characters each; keep at least 3, at most 15"
    assert g.description_note == "At most 90 characters each; keep at least 2, at most 4"
    assert (nb.h_min, nb.d_min, nb.h_max, nb.d_max) == (1, 1, None, None)
    assert nb.headline_note.startswith(
        "At most 90 characters each, a wide character counts as two; NewsBreak shows up to 3"
    )
    assert nb.too_few == newsbreak_adapter.TOO_LITTLE_COPY


def test_copiers_names_the_funded_entries_that_run_the_channels_words():
    assert plan_words.copiers(_sections(GOOGLE, NEWSBREAK), "google_search") == ["newsbreak"]
    assert plan_words.copiers(_sections(GOOGLE, NEWSBREAK), "newsbreak") == []
    assert plan_words.copiers(_sections(GOOGLE), "google_search") == []


def test_swapping_the_primarys_words_reaches_every_copy_and_keeps_their_copy_of():
    new_h, new_d = ["One Headline", "Two Headline", "Three Headline"], ["First description.", "Second description."]
    out = plan_words.swapped(_sections(GOOGLE, NEWSBREAK), "google_search", new_h, new_d)
    google, nb = out["ads"]["channels"]
    assert google["headlines"] == new_h and google["descriptions"] == new_d and "copy_of" not in google
    assert nb["headlines"] == new_h and nb["descriptions"] == new_d and nb["copy_of"] == "google_search"
    assert nb["monthly_usd"] == 300.0 and google["themes"] == ["invisalign"]  # nothing else moves
    original = _sections(GOOGLE, NEWSBREAK)
    plan_words.swapped(original, "google_search", new_h, new_d)
    assert original["ads"]["channels"][0]["headlines"] == GOOGLE["headlines"]  # a copy, never in place


def test_giving_a_copying_platform_its_own_words_drops_its_copy_of_and_leaves_the_primary():
    out = plan_words.swapped(_sections(GOOGLE, NEWSBREAK), "newsbreak", ["Own NewsBreak line"], ["Own description."])
    google, nb = out["ads"]["channels"]
    assert nb["headlines"] == ["Own NewsBreak line"] and "copy_of" not in nb
    assert google["headlines"] == GOOGLE["headlines"] and google["descriptions"] == GOOGLE["descriptions"]
