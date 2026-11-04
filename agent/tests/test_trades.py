"""The one trade table (capability layer K3 Task 6): the industry sites read a home-services business's trade from it
(sites/intake), and the Strategist the trade whose knowledge a plan draft reads (roles/strategist). The site's four
trades are exactly the sites' own, in their order; landscaping and remodel follow."""

import pytest

from agent import trades
from agent.sites import intake
from agent.sites.families import home_services


def test_the_six_trades_in_the_tables_order():
    assert trades.NAMES == ("roofing", "plumbing", "hvac", "electrical", "landscaping", "remodel")
    assert tuple(name for name, _ in trades.TABLE) == trades.NAMES


def test_the_sites_four_patterns_are_unchanged_and_come_first():
    assert [(name, pattern.pattern) for name, pattern in trades.TABLE[:4]] == [
        ("roofing", "roof"),
        ("plumbing", "plumb"),
        ("hvac", r"hvac|heating|cooling|air conditioning|furnace|\bac\b"),
        ("electrical", "electric"),
    ]
    assert trades.NAMES[:4] == home_services.TRADES


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Roofing contractor", "roofing"),
        ("roofer", "roofing"),
        ("residential plumber", "plumbing"),
        ("HVAC", "hvac"),
        ("Heating and cooling", "hvac"),
        ("Air Conditioning service", "hvac"),
        ("Furnace repair", "hvac"),
        ("AC repair", "hvac"),  # "ac" is the word AC ...
        ("Tarmac paving", None),  # ... not the end of "tarmac"
        ("Electrician", "electrical"),
        ("electrical contractor", "electrical"),
        ("landscape design and build", "landscaping"),
        ("Lawn care", "landscaping"),
        ("hardscaping", "landscaping"),
        ("irrigation systems", "landscaping"),
        ("garden design", "landscaping"),
        ("Kitchen remodels", "remodel"),
        ("bathroom renovation", "remodel"),
        ("renovator", "remodel"),
        ("handyman", None),
        ("general contractor", None),
        ("", None),
        # words the sites never read stay unread: "drainage" turns no landscaper into a plumber
        ("Drain cleaning", None),
        ("water heater repair", None),
        ("landscaping and drainage", "landscaping"),
    ],
)
def test_a_text_names_the_trade_its_words_name(text, want):
    assert trades.trade_in(text) == want


def test_a_text_naming_two_trades_gives_the_earlier_one_in_the_table():
    assert trades.trade_in("plumbing and roofing") == "roofing"
    assert trades.trade_in("kitchen remodels and a new roof") == "roofing"
    assert trades.trade_in("lawn care and kitchen remodels") == "landscaping"


def test_a_text_is_read_in_any_case_and_what_is_no_text_names_nothing():
    assert trades.trade_in("ROOF REPAIR") == "roofing"
    for not_text in (None, 5, ["roof"], {"roof": 1}, b"roof"):
        assert trades.trade_in(not_text) is None


@pytest.mark.parametrize(
    "text",
    [
        "roofer",
        "plumbing and roofing",
        "AC repair",
        "Tarmac paving",
        "electrician",
        "lawn care",
        "kitchen remodels",
        "",
    ],
)
def test_the_sites_read_the_same_table_but_keep_only_their_four_trades(text):
    found = trades.trade_in(text)
    site = intake.family_for({"category": "home_services", "category_detail": text})
    assert site == ("home_services", found if found in home_services.TRADES else None)
