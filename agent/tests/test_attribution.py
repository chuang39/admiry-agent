"""agent/attribution.py (A3p-3 P28, plan C5): the source a lead and an event keep -- the click's channel, then the
tagged link's utm_source, then the kind of site that referred the visitor, then direct -- and its owner words."""

from __future__ import annotations

import pytest

from agent import attribution


@pytest.mark.parametrize(
    ("ref", "kind"),
    [
        ("www.google.com", "search"),
        ("google.co.uk", "search"),
        ("search.yahoo.com", "search"),
        ("duckduckgo.com", "search"),
        ("mail.google.com", "other"),  # a message, not search results
        ("l.facebook.com", "social"),
        ("t.co", "social"),
        ("youtu.be", "social"),
        ("nextdoor.com", "social"),
        ("brightsmile.example", "other"),
        ("partner.example:8443", "other"),  # a port is no part of the host
        ("", None),
        (None, None),
        ("not a host", None),
        ("localhost", None),  # no dot: no site
    ],
)
def test_the_referrers_kind(ref, kind):
    assert attribution.referrer_kind(ref) == kind


@pytest.mark.parametrize(
    ("click_kind", "tags", "ref", "source"),
    [
        ("gclid", {"utm_source": "newsletter"}, "www.google.com", "google_search"),  # the click first
        ("nb_cid", {}, None, "newsbreak"),
        (None, {"utm_source": "Spring Newsletter!"}, "www.google.com", "utm:spring-newsletter"),  # then the tag
        (None, {"utm_medium": "email"}, "www.google.com", "search"),  # no utm_source: the referrer
        (None, {}, "l.facebook.com", "social"),
        (None, {}, None, "direct"),
        ("weird", {}, "partner.example", "other"),  # a click id kind nobody maps names no channel
    ],
)
def test_the_source_is_the_click_then_the_tag_then_the_referrer(click_kind, tags, ref, source):
    assert attribution.came_from(click_kind, tags, ref) == source


def test_the_tags_are_read_from_a_beacon_or_a_form_and_cut():
    posted = {"utm_source": "fb", "_utm_campaign": "spring", "utm_term": "x" * 300, "utm_medium": "  "}
    assert attribution.utm(lambda key: posted.get(key, "")) == {
        "utm_source": "fb",
        "utm_campaign": "spring",
        "utm_term": "x" * attribution.UTM_MAX,
    }


def test_the_sources_read_in_the_owners_words():
    assert attribution.label("google_search") == "Google Search"
    assert attribution.label("utm:newsletter") == "A link tagged “newsletter”"
    assert attribution.label("direct") == "Came straight to the page"
    assert attribution.label(None) == attribution.WORDS["unknown"]
    from agent.tests.conftest import JARGON

    assert not any(JARGON.search(w) for w in attribution.WORDS.values())
