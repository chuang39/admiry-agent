"""The industry site's intake (A3p-6 batch 2 Task 1; plan 2026-10-06-agent-a3p-6-2-facts-photos-publish Q1-Q4 and its
Review Focus "a name the model derived from words" and "a bad patch writes nothing"): the family and the trade a
business's knowledge says, the site's facts filled from what Admiry already has -- the profile, the confirmed Google
listing, the consultation -- with the four required ones still missing named in the order they are asked, what a
patch may carry and what it writes where (the profile's own facts on the profile, the extras on the owner's draft),
and the schema's two new columns."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import business, consult, db, geocode, hosting_minimum, landing, places, plans, profile
from agent.sites import intake, spec, words
from agent.sites.families import SLUG_RE
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_DB_URL

BIZ = "biz_1"
HOURS = "Mon–Fri 7am–6pm; Sat 8am–12pm"


# ---------------------------------------------------------------- what a business has on file


def _listing(**more) -> dict:
    """A Google Maps listing the owner confirmed (listings.link): the one `profile.business_listing` reads."""
    return {
        "place_id": "ChIJ_oak",
        "name": "Oakline Roofing",
        "address": "100 Trade St, Charlotte, NC 28202, USA",
        "lat": 35.2271,
        "lng": -80.8431,
        "source": "Google Maps",
        "confirmed": True,
        **more,
    }


def _roofer(**more) -> dict:
    """A roofer Admiry knows well: its name read from its site, a confirmed listing, a storefront, hours, three
    services, a licence in what makes it different."""
    return {
        "name": "Oakline Roofing",
        "name_source": "site",
        "category": "home_services",
        "category_detail": "roofing contractor",
        "service_area": ["Charlotte", "Matthews"],
        "services": [
            {"name": "Roof repair", "intent_phrases": [], "price_hint": "From $350"},
            {"name": "Roof replacement", "intent_phrases": [], "price_hint": None},
            {"name": "Gutter cleaning", "intent_phrases": [], "price_hint": None},
        ],
        "hours": HOURS,
        "phone": "(704) 555-0142",
        "pricing_hints": [],
        "usp": ["License #NC00000"],
        "geo_model": "storefront",
        "listing": _listing(),
        "locale": "en-US",
        **more,
    }


def _no_geo(**more) -> dict:
    """`_roofer()` with no `geo_model` on file -- nothing says whether people visit the business -- and `more` over it."""
    return {k: v for k, v in _roofer().items() if k != "geo_model"} | more


def _thin(**more) -> dict:
    """What a first reading of plain words leaves: a name the model derived (`name_source: "words"`), one service,
    nothing else."""
    return {
        "name": "Charlotte Roofer",
        "name_source": "words",
        "category": "home_services",
        "category_detail": "roofer",
        "service_area": [],
        "services": [{"name": "Roof repair", "intent_phrases": [], "price_hint": None}],
        "hours": None,
        "phone": None,
        "pricing_hints": [],
        "usp": [],
        "locale": "en-US",
        **more,
    }


def _business(conn, knowledge: dict | None, biz: str = BIZ) -> str:
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'claimed')", (biz,))
    if knowledge is not None:
        business.add_knowledge(conn, biz, knowledge, source="owner")
    return biz


def _page(conn, biz: str = BIZ) -> dict:
    return landing.ensure_page_for_kit(conn, biz)


def _consulted(conn, biz: str = BIZ, **known) -> dict:
    """An open consultation in which the owner picked Roof replacement to lead with (and told the website question
    `known`'s social page, say)."""
    return consult.create(
        conn,
        biz,
        tree="home_services",
        locale="en-US",
        opening_text="a roofer in Charlotte",
        known_facts=known,
        options={"service": [{"id": "svc_roof_replacement", "label": "Roof replacement"}]},
        answers={"service": {"choice": "svc_roof_replacement", "source": "choice"}},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


def _knowledge(conn, biz: str = BIZ) -> dict:
    return business.latest_knowledge(conn, biz)[1]


def _versions(conn, biz: str = BIZ) -> int:
    return conn.execute("SELECT count(*) AS n FROM knowledge WHERE business_id = %s", (biz,)).fetchone()["n"]


def _stored_draft(conn, page_id: str):
    return conn.execute("SELECT site_draft FROM page WHERE id = %s", (page_id,)).fetchone()["site_draft"]


# ---------------------------------------------------------------- Q1: the family and the trade


@pytest.mark.parametrize(
    ("knowledge", "expected"),
    [
        ({"category": "home_services", "category_detail": "residential plumber"}, ("home_services", "plumbing")),
        ({"category": "home_services", "category_detail": "Roofing contractor"}, ("home_services", "roofing")),
        ({"category": "home_services", "category_detail": "roofer"}, ("home_services", "roofing")),
        (
            {"category": "home_services", "category_detail": "handyman", "services": [{"name": "Furnace repair"}]},
            ("home_services", "hvac"),
        ),
        ({"category": "home_services", "category_detail": "AC repair"}, ("home_services", "hvac")),
        ({"category": "home_services", "category_detail": "Heating and cooling"}, ("home_services", "hvac")),
        ({"category": "home_services", "category_detail": "Air Conditioning service"}, ("home_services", "hvac")),
        ({"category": "home_services", "category_detail": "HVAC"}, ("home_services", "hvac")),
        ({"category": "home_services", "category_detail": "Electrician"}, ("home_services", "electrical")),
        ({"category": "home_services", "category_detail": "electrical contractor"}, ("home_services", "electrical")),
        # no keyword: the home-services structure with no trade (no problem picker)
        ({"category": "home_services", "category_detail": "handyman"}, ("home_services", None)),
        (
            {"category": "home_services", "category_detail": "handyman", "services": [{"name": "Odd jobs"}]},
            ("home_services", None),
        ),
        ({"category": "home_services"}, ("home_services", None)),
        # trades the shared table (agent/trades.py) knows and the site families do not: today's generic site, no trade
        ({"category": "home_services", "category_detail": "landscaping company"}, ("home_services", None)),
        ({"category": "home_services", "category_detail": "kitchen remodeler"}, ("home_services", None)),
        # ... and what the detail names does not stop the services being read, as before the table had them
        (
            {
                "category": "home_services",
                "category_detail": "kitchen remodeler",
                "services": [{"name": "Roof repair"}],
            },
            ("home_services", "roofing"),
        ),
        # "ac " is the word AC: no other word that holds those letters is an air conditioner
        ({"category": "home_services", "category_detail": "general contractor"}, ("home_services", None)),
        ({"category": "home_services", "category_detail": "Tarmac paving"}, ("home_services", None)),
        # the trade field and the detail come first, the services after; the first match in the plan's order wins
        (
            {"category": "home_services", "category_detail": "handyman", "trade": "plumbing"},
            ("home_services", "plumbing"),
        ),
        (
            {
                "category": "home_services",
                "category_detail": "residential plumber",
                "services": [{"name": "Roof repair"}],
            },
            ("home_services", "plumbing"),
        ),
        ({"category": "home_services", "category_detail": "plumbing and roofing"}, ("home_services", "roofing")),
        (
            {
                "category": "home_services",
                "category_detail": "handyman",
                "services": [{"name": "Electric panels"}, {"name": "Roof repair"}],
            },
            ("home_services", "roofing"),
        ),
        ({"category": "dentists", "category_detail": "family dentist"}, ("dental", "dentist")),
        ({"category": "dentists"}, ("dental", "dentist")),
        ({"category": "attorneys", "category_detail": "immigration lawyer"}, None),
        ({"category": "fallback", "category_detail": "florist"}, None),
        ({"category_detail": "residential plumber"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_family_and_the_trade_are_read_from_the_knowledge(knowledge, expected):
    assert intake.family_for(knowledge) == expected


# ---------------------------------------------------------------- Q2, Q3: the words, the keys, the slug


def test_the_four_required_facts_and_their_questions_are_the_plans_words():
    assert intake.REQUIRED == ("name", "phone", "place", "services")
    assert intake.QUESTIONS == {
        "name": "What is the business called?",
        "phone": "What phone number should the site show?",
        "place": "Where is the business? Its address, or the towns it serves.",
        "services": "What do you offer? Name the main services, one per line.",
    }
    assert list(intake.QUESTIONS) == list(intake.REQUIRED)
    for sentence in intake.QUESTIONS.values():
        assert not (JARGON.search(sentence) or ACCOUNT_JARGON.search(sentence) or MONEY_JARGON.search(sentence))


def test_the_facts_a_patch_may_carry_are_the_plans_and_every_one_is_a_field_of_the_document():
    assert intake.SITE_FACTS == (
        "name",
        "phone",
        "place",  # the answer to the place question: an address, or the towns (fix round 1)
        "address",
        "service_area",
        "services",
        "hours",
        "about",
        "email",
        "social",
        "licence",
        "years",
        "insured",
        "warranty",
        "free_estimate",
        "emergency",
        "financing",
        "languages",
        "reviews",
        "dentist",
        "new_patients",
        "insurance_plans",
        "in_office_plan",
        "emergency_line",
    )
    # `place` is the answer to a question, read as an address or as towns: it is no field of the document itself
    assert set(intake.SITE_FACTS) - {"place"} <= set(spec.Facts.model_fields)
    assert set(intake.QUESTIONS) <= set(intake.SITE_FACTS)  # every question's answer key is a key a patch may carry


def test_no_question_asks_for_claim_help_and_no_patch_carries_it():
    """The K3 review of the knowledge files (2026-10-15): a roofer's page names storm-damage repair and a written
    estimate, never claim handling (agent/knowledge/home_services/roofing.md; Fla. Stat. 489.147, Tex. Ins. Code
    4102.163, and a website is an advertisement, 28 TAC 21.102(1)). So the site card's extras no longer ask "Do you
    help with insurance claims?", the document has no such fact, and a patch that carries one is refused as any
    unknown key is."""
    labels = [e["label"] for extras in intake.EXTRAS.values() for e in extras]
    assert not any("claim" in label.lower() for label in labels), labels
    assert "insurance_claims" not in intake.SITE_FACTS and "insurance_claims" not in spec.Facts.model_fields
    with pytest.raises(ToolError, match="not a fact a site takes"):
        intake.check({"insurance_claims": True})


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Roof Repair", "roof-repair"),
        ("  Roof repair & replacement!  ", "roof-repair-replacement"),
        ("24/7 Emergency Plumbing", "24-7-emergency-plumbing"),
        ("Café repair", "cafe-repair"),
        ("x" * 60, "x" * 40),
        ("Heating, ventilation and air conditioning installation", "heating-ventilation-and-air-conditioning"),
        ("$$$", "service"),
        ("", "service"),
    ],
)
def test_a_service_slug_is_lower_case_hyphenated_trimmed_and_at_most_40_characters(name, slug):
    assert intake.slug(name) == slug
    assert SLUG_RE.match(intake.slug(name)) and len(intake.slug(name)) <= 40


# ---------------------------------------------------------------- Q3: the facts Admiry has


def test_the_facts_are_filled_from_what_admiry_has(conn):
    """The profile's name (a site reading vouches for it), phone, the confirmed listing's address and map link, the
    storefront, the hours as lines, the services with the one the owner picked leading, the licence in what makes the
    business different -- and nothing is missing: the document passes the one gate."""
    biz = _business(conn, _roofer())
    _consulted(conn, biz)
    facts, missing = intake.facts(conn, biz, _page(conn, biz))
    assert missing == []
    assert (facts.name, facts.phone) == ("Oakline Roofing", "(704) 555-0142")
    assert (facts.address, facts.city, facts.state, facts.storefront) == (
        "100 Trade St, Charlotte, NC 28202, USA",
        "Charlotte",
        "NC",
        True,
    )
    assert facts.service_area == ["Charlotte", "Matthews"]
    assert facts.hours == ["Mon–Fri 7am–6pm", "Sat 8am–12pm"]
    assert [(s.slug, s.name, s.lead, s.price_line) for s in facts.services] == [
        ("roof-repair", "Roof repair", False, "From $350"),
        ("roof-replacement", "Roof replacement", True, None),
        ("gutter-cleaning", "Gutter cleaning", False, None),
    ]
    assert facts.licence == "NC00000"
    assert facts.gbp_url == places.maps_url("Oakline Roofing", "ChIJ_oak") and facts.gbp_url.startswith(
        "https://www.google.com/"
    )
    assert facts.place_id == "ChIJ_oak"
    assert (facts.about, facts.email, facts.years, facts.warranty, facts.reviews) == (None, None, None, None, [])
    spec.validate(spec.SiteSpec(family="home_services", style="solid", trade="roofing", facts=facts))
    assert intake.facts(conn, biz, None) == (facts, [])  # a business with no page yet reads the same


def test_a_dental_practice_is_filled_the_same_way(conn):
    knowledge = {
        **_roofer(),
        "name": "Bright Smile Dental",
        "category": "dentists",
        "category_detail": "family dentist",
        "services": [
            {"name": "Invisalign", "intent_phrases": [], "price_hint": None},
            {"name": "Cleanings", "intent_phrases": [], "price_hint": "$120"},
        ],
        "usp": ["Same-week appointments"],
        "listing": _listing(name="Bright Smile Dental", place_id="ChIJ_smile"),
    }
    biz = _business(conn, knowledge)
    facts, missing = intake.facts(conn, biz, _page(conn, biz))
    assert missing == []
    assert [(s.slug, s.lead) for s in facts.services] == [("invisalign", True), ("cleanings", False)]  # none picked
    assert facts.licence is None
    spec.validate(spec.SiteSpec(family="dental", style="calm", trade="dentist", facts=facts))


def test_a_name_the_model_derived_from_words_is_the_one_missing_fact_and_the_owners_answer_vouches_for_it(conn):
    """Review Focus: a phone, an address and services, but a name no site reading, no confirmed listing and no owner
    vouches for: the name alone is missing, and once the owner gives it nothing else is asked."""
    knowledge = _thin(
        phone="(704) 555-0142",
        service_area=["Charlotte"],
        listing={"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203, USA", "lat": 35.2, "lng": -80.8},
    )
    biz = _business(conn, knowledge)
    page = _page(conn, biz)
    assert plans.name_for(_knowledge(conn, biz)) is None
    facts, missing = intake.facts(conn, biz, page)
    assert missing == ["name"] and facts.name == ""
    intake.merge(conn, biz, page, {"name": "Oakline Roofing"})
    after = _knowledge(conn, biz)
    assert (after["name"], after["name_source"]) == ("Oakline Roofing", "owner")
    assert plans.name_for(after) == "Oakline Roofing"
    facts, missing = intake.facts(conn, biz, page)
    assert missing == [] and facts.name == "Oakline Roofing"


@pytest.mark.parametrize(
    ("knowledge", "missing"),
    [
        # services and a phone, but no listing and no service area: only the place is missing
        (
            {
                "name": "Oakline Roofing",
                "name_source": "owner",
                "category": "home_services",
                "phone": "(704) 555-0142",
                "services": [{"name": "Roof repair"}],
                "service_area": [],
            },
            ["place"],
        ),
        # nothing at all but the category: the four, in the order they are asked
        ({"category": "home_services"}, ["name", "phone", "place", "services"]),
        # a phone too short to be one is as good as none
        (
            {
                "name": "Oakline Roofing",
                "name_source": "owner",
                "phone": "555-0142",
                "services": [{"name": "Roof repair"}],
                "service_area": ["Charlotte"],
            },
            ["phone"],
        ),
        # services the profile cannot read are none
        (
            {
                "name": "Oakline Roofing",
                "name_source": "owner",
                "phone": "+1 (704) 555-0142",
                "services": [{"name": "  "}, {"price_hint": "$5"}],
                "service_area": ["Charlotte"],
            },
            ["services"],
        ),
        (None, ["name", "phone", "place", "services"]),
    ],
)
def test_the_required_facts_still_missing_are_named_in_the_order_they_are_asked(conn, knowledge, missing):
    biz = _business(conn, knowledge)
    assert intake.facts(conn, biz, _page(conn, biz))[1] == missing


@pytest.mark.parametrize(
    ("more", "met"),
    [
        ({}, False),  # no address and no town
        ({"service_area": ["Charlotte"]}, True),  # one town serves
        ({"listing": {"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203, USA"}}, True),  # the address's town
        # no town to read and no storefront, but a street number: the business is there (Task 4 fix round 1)
        ({"listing": {"kind": "address", "address": "9 Elm Ct, Charlotte NC"}}, True),
        ({"listing": {"kind": "address", "address": "Downtown"}}, False),  # no street number: no place
        ({"listing": {"kind": "address", "address": "9 Elm Ct, Charlotte NC"}, "geo_model": "storefront"}, True),
        # a lookup's guess nobody confirmed names no place
        ({"listing": {"place_id": "ChIJ_x", "name": "Guess", "address": "1 A St, Charlotte, NC 28202, USA"}}, False),
        # a radius or a ZIP code says how far, not where: the site would name no place (Task 4, the controller's ruling)
        ({"service_area": ["within 25 miles"]}, False),
        ({"service_area": ["28202", "nationwide"]}, False),
        ({"service_area": ["within 25 miles", "Matthews"]}, True),
    ],
)
def test_the_place_is_met_by_a_storefront_address_or_one_town(conn, more, met):
    base = {"name": "Oakline Roofing", "name_source": "owner", "phone": "(704) 555-0142", "services": ["Roof repair"]}
    biz = _business(conn, {**base, **more})
    assert (intake.facts(conn, biz, _page(conn, biz))[1] == []) is met


def test_a_listing_nobody_confirmed_is_never_the_address_or_the_map_link(conn):
    guess = {"place_id": "ChIJ_x", "name": "Guess", "address": "1 A St, Charlotte, NC 28202, USA", "lat": 1, "lng": 2}
    biz = _business(conn, _roofer(listing=guess, geo_model="storefront"))
    facts, _ = intake.facts(conn, biz, _page(conn, biz))
    assert (facts.address, facts.city, facts.state, facts.gbp_url, facts.place_id) == (
        None,
        "Charlotte",  # no address to read a town from: the first of the towns the business serves
        None,
        None,
        None,
    )
    # an address the owner placed is an address, and no Google profile
    biz2 = _business(
        conn, _roofer(listing={"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203, USA"}), "biz_2"
    )
    facts, _ = intake.facts(conn, biz2, _page(conn, biz2))
    assert (facts.address, facts.city, facts.gbp_url, facts.place_id) == (
        "9 Elm Ct, Charlotte, NC 28203, USA",
        "Charlotte",
        None,
        None,
    )


_DENTAL = {"category": "dentists", "category_detail": "family dentist"}


@pytest.mark.parametrize(
    ("knowledge", "storefront"),
    [
        # nothing said: a dental practice is a place people visit once an address is on file, a home-services firm is not
        (_no_geo(**_DENTAL), True),
        (_no_geo(**_DENTAL, listing={"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203, USA"}), True),
        (_no_geo(**_DENTAL, listing=None), False),  # no address on file
        (_no_geo(), False),
        (_no_geo(listing=None), False),
        (_no_geo(category="attorneys"), False),  # no family of sites at all
        # said: what the owner said stands, whatever the family
        (_no_geo(**_DENTAL, geo_model="service_area"), False),
        (_no_geo(**_DENTAL, geo_model="storefront"), True),
        (_no_geo(geo_model="storefront"), True),
        (_no_geo(geo_model="service_area"), False),
    ],
)
def test_a_dental_practice_is_a_storefront_unless_the_owner_said_otherwise(conn, knowledge, storefront):
    biz = _business(conn, knowledge)
    assert intake.facts(conn, biz, _page(conn, biz))[0].storefront is storefront


@pytest.mark.parametrize(
    ("more", "city", "state"),
    [
        # towns only, no address: the first town stands for the city (the headlines name a town: spec T5)
        ({"listing": None, "service_area": ["Charlotte", "Matthews"]}, "Charlotte", None),
        # an address with no town to read: the first of the towns, and no state the address does not give
        (
            {
                "listing": {"kind": "address", "address": "9 Elm Ct, Charlotte NC"},
                "service_area": ["Matthews", "Mint Hill"],
            },
            "Matthews",
            None,
        ),
        # an address that names its town: its own, with its state, before the areas
        (
            {
                "listing": {"kind": "address", "address": "9 Elm Ct, Matthews, NC 28105, USA"},
                "service_area": ["Charlotte"],
            },
            "Matthews",
            "NC",
        ),
        ({"listing": None, "service_area": []}, "", None),  # nothing to name
    ],
)
def test_a_business_with_no_town_in_its_address_is_named_by_the_first_town_it_serves(conn, more, city, state):
    biz = _business(conn, _no_geo(**more))
    facts, _ = intake.facts(conn, biz, _page(conn, biz))
    assert (facts.city, facts.state) == (city, state)


@pytest.mark.parametrize(
    ("address", "city"),
    [
        ("9 Elm Ct, Charlotte NC", "Charlotte"),  # the state written after the town is no part of it
        ("9 Elm Ct, Charlotte NC 28203", "Charlotte"),
        ("9 Elm Ct, Charlotte", "Charlotte"),
        ("12-14 Oak Ln, Mint Hill nc", "Mint Hill nc"),  # a state in small letters is not read as one
        ("9 Elm Ct Charlotte NC", ""),  # nothing after the street to read: the place is met, no town is named
        # Task 5's follow-up: a ZIP code, the country and a unit are never the city, and the town is the last part's
        # when that part ends in a state (and ZIP code) -- whatever stands before it
        ("9 Elm Ct, 28203", ""),
        ("9 Elm Ct, 28203-1234", ""),
        ("9 Elm Ct, USA", ""),
        ("9 Elm Ct, United States", ""),
        ("9 Elm Ct, Houston TX, 77002, USA", "Houston"),
        ("9 Elm Ct, Charlotte NC 28203, United States", "Charlotte"),
        ("123 Main St, Suite 4, Houston TX 77002", "Houston"),
        ("123 Main St, Suite 4, Houston", "Houston"),  # a unit before the town is passed over
        ("123 Main St, #12, Charlotte NC", "Charlotte"),
        ("123 Main St, Suite 200-A, Houston TX 77002", "Houston"),
        ("123 Main St, Ste. 4, Houston TX 77002", "Houston"),
        ("9 Elm Ct, Dilworth, Charlotte NC 28203", "Charlotte"),  # a part before the town (a neighbourhood) is no town
        ("9 Elm Ct, Dilworth, Charlotte NC 28203, USA", "Charlotte"),
        ("123 Main St, Suite 4", ""),  # nothing but a unit after the street: the place is met, no town is named
        ("123 Main St, Apt 5B, USA", ""),
        # Task 6's follow-up: a country or a ZIP code glued to a part is taken off it, and with no part ending in a state
        # the last part left is the town (the first or the second-to-last it was read as before was a neighbourhood)
        ("9 Elm Ct, Charlotte NC 28203 USA", "Charlotte"),
        ("9 Elm Ct, Charlotte NC 28203 United States", "Charlotte"),
        ("9 Elm Ct, Charlotte USA", "Charlotte"),
        ("9 Elm Ct, Miami 33127", "Miami"),
        ("9 Elm Ct, Wynwood, Miami, 33127", "Miami"),
        ("9 Elm Ct, Wynwood, Miami", "Miami"),
        ("9 Elm Ct, Wynwood, Miami, USA", "Miami"),
        (
            "9 Elm Ct, Charlotte NC, Mecklenburg County",
            "Charlotte",
        ),  # a part that ends in a state wins over a later one
        # "fl" after a town is Florida -- followed by a ZIP code or ending the part -- not a floor, and no town
        ("9 Elm Ct, Miami, fl 33127", "Miami"),
        ("9 Elm Ct, Wynwood, Miami, fl 33127", "Miami"),
        ("9 Elm Ct, Wynwood, Miami, Fl.", "Miami"),
        ("9 Elm Ct, Miami, fl", "Miami"),
        ("9 Elm Ct, Miami, nc", "Miami"),  # any state written alone, in small letters as well
        ("9 Elm Ct, fl 33127", ""),
        # fix round 1: a part cleaned down to a bare ZIP code or a country is dropped and the one before it judged
        ("9 Elm Ct, Miami, 33127 USA", "Miami"),
        ("9 Elm Ct, Miami, USA 33127", "Miami"),
        ("9 Elm Ct, Miami, 33127-1234 United States", "Miami"),
        ("9 Elm Ct, 33127 USA", ""),
        # ... and a part that is a spelled-out state (the fifty names and the District of Columbia, in any case), with or
        # without its ZIP code or country, is a state part: no town, and the town is the part before it
        ("9 Elm Ct, Miami, Florida 33127", "Miami"),
        ("9 Elm Ct, Miami, florida", "Miami"),
        ("9 Elm Ct, Wynwood, Miami, FLORIDA 33127 USA", "Miami"),
        ("9 Elm Ct, Charlotte, North Carolina 28203, United States", "Charlotte"),
        ("9 Elm Ct, Washington, District of Columbia 20001", "Washington"),
        ("9 Elm Ct, Dilworth, Charlotte, north   carolina", "Charlotte"),  # in any case, and with any spaces between
        # a state spelled out that stands alone, with no part before it, may be the town ("123 Broadway, New York"): it is
        # dropped only when a part stands before it to be the town
        ("9 Elm Ct, Florida", "Florida"),
        ("123 Broadway, New York", "New York"),
        ("123 Broadway, New York, New York", "New York"),
        ("123 Broadway, New York, NEW YORK 10006 USA", "New York"),
        ("9 Elm Ct, Suite 4, Washington", "Washington"),
        # a floor is still a unit: neither a town nor what hides the town after it
        ("123 Main St, Fl 3, Houston", "Houston"),
        ("123 Main St, Fl. 12, Houston TX 77002", "Houston"),
        ("123 Main St, Floor 3", ""),
        ("123 Main St, Suite 10000, Houston", "Houston"),  # a unit's own five-digit number is no ZIP code
    ],
)
def test_an_address_with_a_street_number_and_no_town_to_read_names_the_town_after_its_street(conn, address, city):
    """Task 4 fix round 1 (the controller's ruling, item 9): a service-area business whose address Admiry cannot place
    -- no town `places.town` reads, none it serves -- is still somewhere: an address with a street number meets the
    place, so the question is not asked again for the same answer, and the city is what follows the street, read as
    `landing._city` reads an address with a state written after the town dropped. Task 5's follow-up: a ZIP code on its
    own, the country and a unit are never the city, and the last part left names it when that part ends in a state (and
    ZIP code). Task 6's follow-up: a country or a ZIP code glued to a part is taken off it, a state on its own ("fl 33127",
    "Fl.") is no town and no floor, and with no part ending in a state the last part left is the town."""
    biz = _business(conn, _no_geo(listing={"kind": "address", "address": address}, service_area=["within 25 miles"]))
    facts, missing = intake.facts(conn, biz, _page(conn, biz))
    assert (facts.city, missing, facts.storefront, facts.service_area) == (city, [], False, [])


def test_a_towns_only_business_has_a_town_for_its_home_headline(conn):
    biz = _business(conn, _no_geo(listing=None, service_area=["Charlotte", "Matthews"]))
    facts, missing = intake.facts(conn, biz, _page(conn, biz))
    assert missing == []
    site = spec.SiteSpec(family="home_services", style="solid", facts=facts)
    assert words.model_message(site)["city"] == "Charlotte"
    assert words.model_message(site)["headline_pattern"] == "Roof repair in Charlotte"


@pytest.mark.parametrize(
    ("areas", "city", "towns", "missing"),
    [
        (["within 25 miles of Charlotte", "Matthews"], "Matthews", ["Matthews"], []),
        (["28202"], "", [], ["place"]),
        (["Charlotte", "50 miles around"], "Charlotte", ["Charlotte"], []),
        (["nationwide"], "", [], ["place"]),
    ],
)
def test_a_radius_or_a_zip_code_is_no_town_to_name_the_city_by_or_to_print(conn, areas, city, towns, missing):
    """`consult.NO_TOWN` says what an entry of the service area is that is no town: the site prints and names the city
    by the towns only, and a business whose only entries are a radius or a ZIP code is asked where it is -- or its site
    would name no place (Task 4, the controller's ruling: the place is met by a town, not by any entry)."""
    biz = _business(conn, _no_geo(listing=None, service_area=areas))
    facts, asked = intake.facts(conn, biz, _page(conn, biz))
    assert (facts.city, facts.service_area, asked) == (city, towns, missing)


def test_a_radius_line_does_not_name_the_home_headlines_town(conn):
    biz = _business(conn, _no_geo(listing=None, service_area=["within 25 miles of Charlotte", "Matthews"]))
    facts, _ = intake.facts(conn, biz, _page(conn, biz))
    site = spec.SiteSpec(family="home_services", style="solid", facts=facts)
    assert words.model_message(site)["headline_pattern"] == "Roof repair in Matthews"


def test_the_hours_are_lines_split_on_newlines_and_semicolons(conn):
    biz = _business(conn, _roofer(hours="Mon-Fri 8-5\nSat 9-1; Sun closed;; "))
    assert intake.facts(conn, biz, _page(conn, biz))[0].hours == ["Mon-Fri 8-5", "Sat 9-1", "Sun closed"]
    biz3 = _business(conn, _roofer(hours="Mon-Fri 8-5, Sat 9-1; Sun closed"), "biz_3")  # a comma is part of a line
    assert intake.facts(conn, biz3, _page(conn, biz3))[0].hours == ["Mon-Fri 8-5, Sat 9-1", "Sun closed"]
    biz2 = _business(conn, _roofer(hours=None), "biz_2")
    assert intake.facts(conn, biz2, _page(conn, biz2))[0].hours == []


def test_a_service_slug_used_twice_gets_a_number_and_every_slug_is_its_own(conn):
    services = [{"name": "Roof repair"}, {"name": "Roof repair!"}, {"name": "Roof-repair"}, {"name": "Roof repair 2"}]
    biz = _business(conn, _roofer(services=services))
    slugs = [s.slug for s in intake.facts(conn, biz, _page(conn, biz))[0].services]
    assert slugs[:3] == ["roof-repair", "roof-repair-2", "roof-repair-3"]
    assert len(set(slugs)) == 4 and all(SLUG_RE.match(s) for s in slugs)  # the fourth's own slug was taken too


def test_with_no_lead_service_the_first_leads(conn):
    biz = _business(conn, _roofer())
    assert [s.lead for s in intake.facts(conn, biz, _page(conn, biz))[0].services] == [True, False, False]


def test_the_plans_focus_services_lead_and_no_more_than_six_do(conn):
    names = [f"Service {n}" for n in range(1, 9)]
    biz = _business(conn, _roofer(services=[{"name": n} for n in names]))
    business.set_goal(conn, biz, focus_services=names[3:5])  # the plan leads with the fourth and the fifth
    leads = [s.lead for s in intake.facts(conn, biz, _page(conn, biz))[0].services]
    assert leads == [False, False, False, True, True, False, False, False]
    business.set_goal(conn, biz, focus_services=names)  # and now with all eight
    leads = [s.lead for s in intake.facts(conn, biz, _page(conn, biz))[0].services]
    assert leads == [True] * 6 + [False] * 2


@pytest.mark.parametrize(
    ("url", "kept", "key"),
    [
        ("https://www.facebook.com/oaklineroofing", "https://www.facebook.com/oaklineroofing", "facebook"),
        ("https://instagram.com/oakline", "https://instagram.com/oakline", "instagram"),
        ("https://www.tiktok.com/@oakline", "https://www.tiktok.com/@oakline", "tiktok"),
        ("https://m.yelp.com/biz/oakline-charlotte", "https://m.yelp.com/biz/oakline-charlotte", "yelp"),
        ("https://linktr.ee/oakline", "https://linktr.ee/oakline", "other"),
        ("https://nextdoor.com/pages/oakline", "https://nextdoor.com/pages/oakline", "other"),
        ("http://facebook.com/oakline", "https://facebook.com/oakline", "facebook"),  # always https
    ],
)
def test_a_social_page_the_owner_gave_the_website_question_is_kept_by_its_host(conn, url, kept, key):
    biz = _business(conn, _roofer())
    _consulted(conn, biz, social=url)
    assert intake.facts(conn, biz, _page(conn, biz))[0].social == {key: kept}


def test_no_social_page_is_none(conn):
    biz = _business(conn, _roofer())
    _consulted(conn, biz)
    assert intake.facts(conn, biz, _page(conn, biz))[0].social == {}
    biz2 = _business(conn, _roofer(), "biz_2")
    assert intake.facts(conn, biz2, _page(conn, biz2))[0].social == {}  # no consultation at all


@pytest.mark.parametrize(
    ("more", "licence"),
    [
        ({"usp": ["Family owned", "license #nc-0099"]}, "nc-0099"),  # case-blind
        ({"usp": [], "pricing_hints": ["Licence is shown", "License #TACLA00000C on every quote"]}, "TACLA00000C"),
        ({"usp": ["License #A1"], "pricing_hints": ["License #B2"]}, "A1"),  # the first, usp before the hints
        ({"usp": ["Licensed and insured"]}, None),  # no number, no licence
        ({"usp": ["License # pending"]}, None),
    ],
)
def test_the_licence_is_the_first_number_after_license_hash_in_what_makes_the_business_different(conn, more, licence):
    biz = _business(conn, _roofer(**more))
    assert intake.facts(conn, biz, _page(conn, biz))[0].licence == licence


# ---------------------------------------------------------------- Q2: the draft


def test_a_draft_is_an_empty_one_for_a_page_with_none_and_saved_whole(conn):
    assert intake.draft(None) == {
        "family": None,
        "trade": None,
        "style": None,
        "accent": None,
        "wordmark_ok": False,
        "facts": {},
        "extras_asked": False,
        "words": None,
        "words_for": None,  # Task 4: what the model read when it wrote the words
        "words_pending": None,  # Task 4 fix round 1: the words of a build that has not passed yet
        "mailed_ready": False,  # Task 5: the first build's e-mail went to the owner, once
        "asked": 0,  # Task 6 fix round 1: the questions asked so far in this intake (the look and each fact)
        "asked_last": None,  # ... and the one asked last: the same question asked again keeps its number
    }
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    assert intake.draft(page) == intake.draft(None) and page["site_draft"] is None
    saved = {**intake.draft(page), "family": "home_services", "style": "solid", "facts": {"warranty": "10 years"}}
    intake.save_draft(conn, page["id"], saved)
    assert _stored_draft(conn, page["id"]) == saved
    assert intake.draft(landing.get(conn, page["id"])) == saved
    # a stored draft that lacks a key reads with that key's empty value; reading never changes the page row's own
    old = {"site_draft": {"family": "dental", "facts": {"years": 3}}}
    assert intake.draft(old) == {**intake.draft(None), "family": "dental", "facts": {"years": 3}}
    intake.draft(old)["facts"]["years"] = 99
    assert old["site_draft"]["facts"]["years"] == 3


def test_a_page_row_carries_the_draft_and_the_spec_id(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    assert page["site_draft"] is None and page["site_spec_id"] is None
    intake.save_draft(conn, page["id"], {**intake.draft(None), "family": "home_services"})
    conn.execute("UPDATE page SET site_spec_id = 'ss_1' WHERE id = %s", (page["id"],))
    for row in (landing.current(conn, biz), landing.get(conn, page["id"])):
        assert row["site_draft"]["family"] == "home_services" and row["site_spec_id"] == "ss_1"


def test_the_draft_lies_over_what_admiry_has_key_by_key_and_a_list_replaces(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    draft = {
        **intake.draft(None),
        "facts": {
            "phone": "(980) 555-0111",
            "hours": ["By appointment"],
            "service_area": ["Mint Hill"],
            "warranty": "10 years on workmanship",
            "years": 12,
            "insured": True,
            "licence": "NC11111",
            "reviews": [{"text": "Fast and fair.", "first_name": "Dana", "source": "Google"}],
            "dentist": {"name": "Dr. Lee", "credentials": "DDS"},
            "social": {"yelp": "https://yelp.com/biz/oakline"},
        },
    }
    intake.save_draft(conn, page["id"], draft)
    facts, missing = intake.facts(conn, biz, landing.get(conn, page["id"]))
    assert missing == []
    assert facts.phone == "(980) 555-0111" and facts.hours == ["By appointment"] and facts.service_area == ["Mint Hill"]
    assert (facts.warranty, facts.years, facts.insured, facts.licence) == (
        "10 years on workmanship",
        12,
        True,
        "NC11111",
    )
    assert [r.first_name for r in facts.reviews] == ["Dana"] and facts.dentist.name == "Dr. Lee"
    assert facts.social == {"yelp": "https://yelp.com/biz/oakline"}
    assert facts.name == "Oakline Roofing" and facts.city == "Charlotte"  # the rest is the profile's still


def test_a_change_to_the_profile_reaches_the_site_the_drafts_own_facts_stay(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.merge(conn, biz, page, {"warranty": "10 years on workmanship"})
    assert intake.facts(conn, biz, page)[0].phone == "(704) 555-0142"
    business.add_knowledge(conn, biz, {**_knowledge(conn, biz), "phone": "(980) 555-0111"}, source="owner")
    facts, _ = intake.facts(conn, biz, page)
    assert (facts.phone, facts.warranty) == ("(980) 555-0111", "10 years on workmanship")


def test_social_pages_add_up_and_the_owners_own_lie_over_the_one_the_website_question_kept(conn):
    biz = _business(conn, _roofer())
    _consulted(conn, biz, social="https://facebook.com/oakline")
    page = _page(conn, biz)
    draft = intake.merge(conn, biz, page, {"social": {"yelp": "https://yelp.com/biz/oakline"}})
    assert draft["facts"]["social"] == {"yelp": "https://yelp.com/biz/oakline"}
    draft = intake.merge(conn, biz, page, {"social": {"instagram": "https://instagram.com/oakline"}})
    assert draft["facts"]["social"] == {
        "yelp": "https://yelp.com/biz/oakline",
        "instagram": "https://instagram.com/oakline",
    }
    assert intake.facts(conn, biz, page)[0].social == {
        "facebook": "https://facebook.com/oakline",
        "yelp": "https://yelp.com/biz/oakline",
        "instagram": "https://instagram.com/oakline",
    }
    intake.merge(conn, biz, page, {"social": {"facebook": "https://facebook.com/oakline.new"}})
    assert intake.facts(conn, biz, page)[0].social["facebook"] == "https://facebook.com/oakline.new"


# ---------------------------------------------------------------- Q3: what a patch may carry


@pytest.mark.parametrize(
    ("patch", "key"),
    [
        ({"wheelbarrow": "yes"}, "wheelbarrow"),  # a key no site takes
        ({"place": 28202}, "place"),  # the question's answer is a fact a patch carries (fix round 1): in words
        ({"phone": "555-0142"}, "phone"),  # under 10 digits
        ({"phone": "call us"}, "phone"),
        ({"phone": 7045550142}, "phone"),
        ({"services": [f"Service {n}" for n in range(40)]}, "services"),  # more than 30
        ({"services": [{"lead": True}]}, "services"),
        ({"services": [{"name": "Roof repair", "lead": "yes"}]}, "services"),
        ({"services": [{"name": "Roof repair", "colour": "red"}]}, "services"),
        ({"services": {"name": "Roof repair"}}, "services"),
        ({"services": [5]}, "services"),
        ({"reviews": [{"text": "x" * 700, "first_name": "Dana", "source": "Google"}]}, "reviews"),  # over 600
        ({"reviews": [{"text": "Fast.", "source": "Google"}]}, "reviews"),
        ({"reviews": [{"text": "Fast.", "first_name": "Dana", "source": "Google"}] * 7}, "reviews"),  # at most 6
        ({"reviews": "great"}, "reviews"),
        ({"warranty": "x" * 401}, "warranty"),  # a text over 400 characters
        ({"about": "x" * 401}, "about"),
        ({"about": 5}, "about"),
        ({"name": ["Oakline"]}, "name"),
        ({"hours": 5}, "hours"),
        ({"hours": [5]}, "hours"),
        ({"years": "many"}, "years"),
        ({"years": 0}, "years"),
        ({"years": 151}, "years"),
        ({"years": 12.5}, "years"),
        ({"years": True}, "years"),
        ({"years": 10**400}, "years"),  # too big to be a float: still the key's own sentence
        ({"years": float("inf")}, "years"),
        ({"years": float("nan")}, "years"),
        ({"dentist": {"name": "Dr. Lee", "years": 10**400}}, "dentist"),
        ({"insured": "yes"}, "insured"),
        ({"insurance_claims": 1}, "insurance_claims"),  # gone with the claim-help question (K3 review, 2026-10-15)
        ({"email": "not an e-mail"}, "email"),
        ({"social": {"myspace": "https://myspace.com/oakline"}}, "social"),
        ({"social": {"facebook": "ftp://facebook.com/oakline"}}, "social"),
        ({"social": {"facebook": "http://facebook.com/oakline"}}, "social"),
        ({"social": ["https://facebook.com/oakline"]}, "social"),
        ({"dentist": {"credentials": "DDS"}}, "dentist"),
        ({"dentist": {"name": "Dr. Lee", "hobby": "golf"}}, "dentist"),
        ({"dentist": "Dr. Lee"}, "dentist"),
        ({"languages": [5]}, "languages"),
        ({"insurance_plans": {"delta": True}}, "insurance_plans"),
        ({"service_area": [5]}, "service_area"),
        ({"leads": ["Roof repair"]}, "leads"),  # the draft's own: a patch marks a service `lead` instead
    ],
)
def test_a_bad_patch_is_refused_in_one_sentence_naming_the_key(patch, key):
    with pytest.raises(ToolError) as refused:
        intake.check(patch)
    sentence = str(refused.value)
    assert key in sentence and sentence.endswith(".") and "\n" not in sentence and len(sentence) < 320
    assert not (JARGON.search(sentence) or ACCOUNT_JARGON.search(sentence) or MONEY_JARGON.search(sentence))


TOO_MUCH = "That is more than a site's facts take; send fewer at a time."


def _hours_patch_of(size: int) -> dict:
    """A patch of hours whose JSON is `size` characters long (every line within its own limit)."""
    patch = {"hours": ["x" * 300] * 25 + [""]}
    patch["hours"][-1] = "y" * (size - len(json.dumps(patch, ensure_ascii=False)))
    assert len(json.dumps(patch, ensure_ascii=False)) == size
    return patch


def test_a_patch_is_bounded_as_a_whole_before_anything_else_is_looked_at():
    for patch in (
        {"hours": ["x" * 400] * 30},  # every line within its limit, about 12 KB all told
        _hours_patch_of(8001),
        {"wheelbarrow": "x" * 9000},  # the size is judged before the key is
    ):
        with pytest.raises(ToolError) as refused:
            intake.check(patch)
        assert str(refused.value) == TOO_MUCH
    assert len(intake.check(_hours_patch_of(8000))["hours"]) == 26  # the limit itself is allowed
    assert not (JARGON.search(TOO_MUCH) or ACCOUNT_JARGON.search(TOO_MUCH) or MONEY_JARGON.search(TOO_MUCH))
    with pytest.raises(ToolError, match="send fewer at a time"):  # a number past what JSON can write is too much too
        intake.check({"years": 10**5000})


def test_a_patch_that_is_no_object_is_refused():
    for patch in ("phone", ["phone"], 5):
        with pytest.raises(ToolError, match="facts must be an object"):
            intake.check(patch)


def test_a_key_that_is_no_text_is_refused_in_the_keys_own_sentence():
    """A caller in this process can send a key JSON cannot write (Task 4 follow-up): the size check leaves it to the
    key's own sentence -- the unknown fact, or the shape of the fact that holds it -- never an error of its own."""
    with pytest.raises(ToolError, match="is not a fact a site takes") as refused:
        intake.check({("a", 1): "x"})
    assert "('a', 1)" in str(refused.value)
    with pytest.raises(ToolError, match="^dentist must be an object"):
        intake.check({"dentist": {("name",): "Dr. Lee"}})


def test_a_good_patch_is_cleaned_and_the_patch_itself_is_never_changed():
    patch = {
        "name": "  Oakline Roofing ",
        "phone": " (704) 555-0142",
        "address": " 100 Trade St, Charlotte, NC 28202 ",
        "service_area": ["Charlotte", " Matthews ", ""],
        "services": [
            "Roof repair",
            {"name": "Roof replacement", "lead": True, "price_line": " From $9,000 "},
            "  Gutters ",
            {"name": "Skylights"},
        ],
        "hours": "Mon–Fri 7am–6pm; Sat 8am–12pm\nSun closed",
        "years": "12",
        "insured": True,
        "social": {"facebook": "https://facebook.com/oakline", "other": "https://linktr.ee/oakline"},
        "languages": ["English", " Spanish"],
        "reviews": [
            {"text": "Fast and fair.", "first_name": "Dana", "source": "Google", "when": "May 2026"},
            {"text": "x" * 600, "first_name": "Sam", "source": "Facebook"},
        ],
        "dentist": {"name": "Dr. Lee", "credentials": "DDS", "years": "9", "note": "Treats children"},
        "email": "hello@oakline.example",
        "warranty": "x" * 400,
    }
    before = repr(patch)
    assert intake.check(patch) == {
        "name": "Oakline Roofing",
        "phone": "(704) 555-0142",
        "address": "100 Trade St, Charlotte, NC 28202",
        "service_area": ["Charlotte", "Matthews"],
        "services": [
            {"name": "Roof repair", "lead": False},
            {"name": "Roof replacement", "lead": True, "price_line": "From $9,000"},
            {"name": "Gutters", "lead": False},
            {"name": "Skylights", "lead": False},
        ],
        "hours": ["Mon–Fri 7am–6pm", "Sat 8am–12pm", "Sun closed"],
        "years": 12,
        "insured": True,
        "social": {"facebook": "https://facebook.com/oakline", "other": "https://linktr.ee/oakline"},
        "languages": ["English", "Spanish"],
        "reviews": [
            {"text": "Fast and fair.", "first_name": "Dana", "source": "Google", "when": "May 2026"},
            {"text": "x" * 600, "first_name": "Sam", "source": "Facebook"},
        ],
        "dentist": {"name": "Dr. Lee", "credentials": "DDS", "years": 9, "note": "Treats children"},
        "email": "hello@oakline.example",
        "warranty": "x" * 400,
    }
    assert repr(patch) == before
    cleaned = intake.check(patch)
    assert intake.check(cleaned) == cleaned  # `merge` checks again what a command already checked


def test_the_answer_to_the_place_question_is_read_as_an_address_or_as_towns_and_a_fact_beside_it_lies_over_it():
    """Fix round 1: `place` is a key of a patch -- the answer to the place question -- read as `read_place` reads it; the
    cleaned patch holds the address or the towns, never `place`; a fact the patch names on its own lies over what the
    words said, and what is no text is refused in one sentence."""
    assert intake.check({"place": " 9 Elm Ct, Charlotte, NC 28203 "}) == {"address": "9 Elm Ct, Charlotte, NC 28203"}
    assert intake.check({"place": "Charlotte, Matthews and Mint Hill"}) == {
        "service_area": ["Charlotte", "Matthews", "Mint Hill"]
    }
    assert intake.check({"place": "Charlotte", "service_area": ["Matthews"]}) == {"service_area": ["Matthews"]}
    assert intake.check({"place": "28202"}) == {} and intake.check({"place": None}) == {}
    assert intake.check({"place": "", "phone": "(704) 555-0142"}) == {"phone": "(704) 555-0142"}
    cleaned = intake.check({"place": "Charlotte and Matthews"})
    assert "place" not in cleaned and intake.check(cleaned) == cleaned
    for said in (28202, ["Charlotte"], {"town": "Charlotte"}, True):
        with pytest.raises(ToolError, match="^place must be text: the address, or the towns the business serves.$"):
            intake.check({"place": said})
    assert intake.PLACE_NOT_TEXT == "place must be text: the address, or the towns the business serves."


def test_the_spelled_out_states_are_the_fifty_and_the_district_of_columbia():
    assert len(intake._STATE_NAMES) == 51 and "district of columbia" in intake._STATE_NAMES
    assert {"florida", "new york", "north carolina", "west virginia", "washington"} <= intake._STATE_NAMES
    assert "miami" not in intake._STATE_NAMES and "puerto rico" not in intake._STATE_NAMES


def test_a_nul_character_is_taken_out_of_every_text_before_anything_else_is_looked_at():
    """Fix round 1: JSONB cannot hold a NUL character, and JSON can say one ("\\u0000"): a fact that carried one made an
    internal error. Every text -- a fact, an item of a list of texts, a part of an object, the answer to the place
    question -- comes out of `check` without it, and one that was nothing else says nothing."""
    nul = "\x00"
    assert intake.check({"about": f"a{nul}b", "warranty": f"{nul}10 years{nul}", "licence": nul}) == {
        "about": "ab",
        "warranty": "10 years",
    }
    assert intake.check({"languages": [f"Span{nul}ish", nul, "English"], "insurance_plans": f"Del{nul}ta\nCigna"}) == {
        "languages": ["Spanish", "English"],
        "insurance_plans": ["Delta", "Cigna"],
    }
    assert intake.check({"hours": [f"Mon{nul} 8-5", nul], "service_area": f"Char{nul}lotte; {nul}"}) == {
        "hours": ["Mon 8-5"],
        "service_area": ["Charlotte"],
    }
    assert intake.check({"services": [{"name": f"Roof{nul} repair", "price_line": f"$9{nul}00"}, f"Gut{nul}ters"]}) == {
        "services": [
            {"name": "Roof repair", "lead": False, "price_line": "$900"},
            {"name": "Gutters", "lead": False},
        ]
    }
    review = {
        "text": f"Fast{nul} and fair.",
        "first_name": f"Da{nul}na",
        "source": f"Goo{nul}gle",
        "when": f"May{nul} 2026",
    }
    assert intake.check({"reviews": [review], "dentist": {"name": f"Dr.{nul} Lee", "note": f"Kind{nul}"}}) == {
        "reviews": [{"text": "Fast and fair.", "first_name": "Dana", "source": "Google", "when": "May 2026"}],
        "dentist": {"name": "Dr. Lee", "note": "Kind"},
    }
    assert intake.check({"place": f"9 Elm{nul} Ct, Charlotte"}) == {"address": "9 Elm Ct, Charlotte"}
    assert intake.check({"social": {"facebook": f"https://facebook.com/oak{nul}line"}}) == {
        "social": {"facebook": "https://facebook.com/oakline"}
    }
    assert intake.check({"name": nul, "phone": f"{nul}(704) 555-0142"}) == {"phone": "(704) 555-0142"}
    # a patch that holds one is never changed, and what it holds is still judged: a phone short of ten digits is refused
    patch = {"about": f"a{nul}b"}
    intake.check(patch)
    assert patch == {"about": f"a{nul}b"}
    with pytest.raises(ToolError, match="^phone must be a number"):
        intake.check({"phone": f"555{nul}-0142"})


def test_what_is_said_as_one_text_is_read_as_the_list_it_stands_for():
    assert intake.check({"services": "Roof repair\nGutters\n- Skylights\n\n"})["services"] == [
        {"name": "Roof repair", "lead": False},
        {"name": "Gutters", "lead": False},
        {"name": "Skylights", "lead": False},
    ]
    assert [s["name"] for s in intake.check({"services": "Roof repair, Gutters and skylights"})["services"]] == [
        "Roof repair",
        "Gutters and skylights",
    ]
    assert intake.check({"service_area": "Charlotte, Matthews and Mint Hill"}) == {
        "service_area": ["Charlotte", "Matthews", "Mint Hill"]
    }
    assert intake.check({"languages": "English, Spanish", "insurance_plans": "Delta\nCigna"}) == {
        "languages": ["English", "Spanish"],
        "insurance_plans": ["Delta", "Cigna"],
    }
    assert intake.check({"years": 12.0, "hours": ["Mon 8-5", " ", "Tue 8-5"]}) == {
        "years": 12,
        "hours": ["Mon 8-5", "Tue 8-5"],
    }


def test_a_state_or_a_zip_code_after_a_town_is_part_of_it_in_the_towns_a_patch_says_too():
    assert intake.check({"service_area": "Charlotte, NC 28202"}) == {"service_area": ["Charlotte"]}
    assert intake.check({"service_area": "28202"}) == {}  # a ZIP code alone says nothing
    assert intake.check({"service_area": ["Charlotte", "NC 28202"]}) == {"service_area": ["Charlotte", "NC 28202"]}


def test_nothing_said_is_nothing_given_and_a_false_is_an_answer():
    assert intake.check({}) == {} and intake.check(None) == {}
    assert intake.check({"name": "", "phone": "  ", "services": [], "social": {}, "about": None, "years": None}) == {}
    assert intake.check({"insured": False}) == {"insured": False}


# ---------------------------------------------------------------- Q4: a place said in words


@pytest.mark.parametrize(
    ("text", "read"),
    [
        ("123 Main St, Charlotte NC", {"address": "123 Main St, Charlotte NC"}),
        (
            "  1600 Pennsylvania Ave NW, Washington, DC 20500 ",
            {"address": "1600 Pennsylvania Ave NW, Washington, DC 20500"},
        ),
        ("12B Oak Ln", {"address": "12B Oak Ln"}),
        ("12-14 Main St, Charlotte NC", {"address": "12-14 Main St, Charlotte NC"}),
        ("Charlotte, Matthews and Mint Hill", {"service_area": ["Charlotte", "Matthews", "Mint Hill"]}),
        ("Charlotte, Matthews, and Mint Hill", {"service_area": ["Charlotte", "Matthews", "Mint Hill"]}),
        ("Charlotte and Matthews", {"service_area": ["Charlotte", "Matthews"]}),
        ("Charlotte\nMatthews", {"service_area": ["Charlotte", "Matthews"]}),
        ("Charlotte", {"service_area": ["Charlotte"]}),
        # a state, or a ZIP code, after a town is part of it; a ZIP code alone is no town
        ("Charlotte, NC", {"service_area": ["Charlotte"]}),
        ("Charlotte, NC 28202", {"service_area": ["Charlotte"]}),
        ("charlotte, nc 28202-1234", {"service_area": ["charlotte"]}),
        ("Charlotte, NC, 28202", {"service_area": ["Charlotte"]}),
        ("Charlotte, 28202", {"service_area": ["Charlotte"]}),
        ("Charlotte, Matthews, NC", {"service_area": ["Charlotte", "Matthews"]}),
        ("28202", {}),
        ("28202, 28203", {}),
        ("Anderson, Sandy Springs", {"service_area": ["Anderson", "Sandy Springs"]}),
        ("5th Avenue", {"service_area": ["5th Avenue"]}),  # no street number
        ("", {}),
        ("   ", {}),
        (None, {}),
    ],
)
def test_a_place_in_words_is_an_address_when_it_has_a_street_number_else_towns(text, read):
    assert intake.read_place(text) == read


# ---------------------------------------------------------------- Q3: what a patch writes where


def test_merge_puts_the_profiles_facts_on_the_profile_in_one_version_and_the_extras_on_the_draft(conn, monkeypatch):
    """The name (the owner's own, so vouched for), phone, address, towns, hours and services go to the business's
    knowledge as one version written by the owner -- the service not on file added by id, the one on file as it was --
    and what is left, the extras, is all the draft's own facts hold."""
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    before = _versions(conn, biz)
    draft = intake.merge(
        conn,
        biz,
        page,
        {
            "name": "Oakline Roofing",
            "phone": "(704) 555-0142",
            "address": "100 Trade St, Charlotte, NC 28202",
            "service_area": ["Charlotte", "Matthews"],
            "hours": ["Mon–Fri 7am–6pm", "Sat 8am–12pm"],
            "services": ["Roof repair", "Gutter cleaning"],
            "warranty": "10 years on workmanship",
            "years": 12,
        },
    )
    assert _versions(conn, biz) == before + 1
    row = conn.execute(
        "SELECT source FROM knowledge WHERE business_id = %s ORDER BY version DESC LIMIT 1", (biz,)
    ).fetchone()
    assert row["source"] == "owner"
    knowledge = _knowledge(conn, biz)
    assert (knowledge["name"], knowledge["name_source"]) == ("Oakline Roofing", "owner")
    assert plans.name_for(knowledge) == "Oakline Roofing"
    assert knowledge["phone"] == "(704) 555-0142" and knowledge["hours"] == HOURS
    assert knowledge["service_area"] == ["Charlotte", "Matthews"]
    assert knowledge["listing"] == {"kind": "address", "address": "100 Trade St, Charlotte, NC 28202"}
    assert [(s["id"], s["name"]) for s in profile.services(knowledge)] == [
        ("svc_roof_repair", "Roof repair"),
        ("svc_gutter_cleaning", "Gutter cleaning"),
    ]
    assert draft["facts"] == {"warranty": "10 years on workmanship", "years": 12}
    assert (draft["family"], draft["trade"]) == ("home_services", "roofing")
    assert _stored_draft(conn, page["id"]) == draft == page["site_draft"]  # saved, and the caller's row is current
    assert intake.facts(conn, biz, page)[1] == []  # the four are in


def test_merge_with_an_empty_patch_writes_nothing(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    before = _versions(conn, biz)
    assert intake.merge(conn, biz, page, {}) == intake.draft(None)
    assert intake.merge(conn, biz, page, {"name": "", "services": [], "years": None}) == intake.draft(None)
    assert _versions(conn, biz) == before
    assert _stored_draft(conn, page["id"]) is None and page["site_draft"] is None


def test_merge_of_what_is_on_file_adds_no_knowledge_version(conn):
    """The same phone, the same name a site reading vouched for, a service already on file: nothing new, so no version
    -- only the draft is saved, and it holds no fact."""
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    before = _versions(conn, biz)
    draft = intake.merge(
        conn, biz, page, {"phone": "(704) 555-0142", "name": "Oakline Roofing", "services": ["Roof repair"]}
    )
    assert draft["facts"] == {} and _versions(conn, biz) == before
    assert _knowledge(conn, biz)["name_source"] == "site"  # an unchanged name keeps where it came from


def test_merge_with_only_extras_writes_no_knowledge_version(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    before = _versions(conn, biz)
    draft = intake.merge(conn, biz, page, {"warranty": "10 years", "email": "hello@oakline.example"})
    assert _versions(conn, biz) == before
    assert draft["facts"] == {"warranty": "10 years", "email": "hello@oakline.example"}
    later = intake.merge(conn, biz, page, {"years": 12, "warranty": "12 years"})
    assert later["facts"] == {"warranty": "12 years", "email": "hello@oakline.example", "years": 12}  # key by key


def test_merge_leaves_the_rest_of_the_draft_as_it_was(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.save_draft(
        conn,
        page["id"],
        {**intake.draft(None), "style": "bold", "accent": "#C2410C", "wordmark_ok": True, "words": {"a": 1}},
    )
    page = landing.get(conn, page["id"])
    draft = intake.merge(conn, biz, page, {"about": "Family owned since 2009."})
    assert (draft["style"], draft["accent"], draft["wordmark_ok"], draft["words"]) == (
        "bold",
        "#C2410C",
        True,
        {"a": 1},
    )
    assert draft["facts"] == {"about": "Family owned since 2009."}


def test_merge_reads_the_draft_again_so_a_row_read_before_another_save_loses_nothing(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)  # read before the look was picked
    intake.save_draft(conn, page["id"], {**intake.draft(None), "style": "clean", "words": {"a": 1}})
    assert page["site_draft"] is None
    draft = intake.merge(conn, biz, page, {"about": "Family owned since 2009."})
    assert (draft["style"], draft["words"], draft["facts"]) == (
        "clean",
        {"a": 1},
        {"about": "Family owned since 2009."},
    )
    assert _stored_draft(conn, page["id"]) == draft


def test_a_draft_is_written_under_the_page_rows_lock_and_a_write_that_waits_in_vain_leaves_nothing(conn):
    """`merge` takes the page row (FOR UPDATE) before it writes anything, so it waits for a writer in progress and,
    when it cannot get the row, has written neither the knowledge nor the draft; `save_draft` and `update_draft` wait
    for the row the same way."""
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    before = (_versions(conn, biz), _knowledge(conn, biz))
    other = db.connect(TEST_DB_URL)
    try:
        with other.transaction():
            other.execute("SELECT id FROM page WHERE id = %s FOR UPDATE", (page["id"],))
            conn.execute("SET lock_timeout = '150ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                intake.merge(conn, biz, page, {"phone": "(980) 555-0111", "warranty": "10 years"})
            with pytest.raises(psycopg.errors.LockNotAvailable):
                intake.save_draft(conn, page["id"], {**intake.draft(None), "style": "solid"})
            with pytest.raises(psycopg.errors.LockNotAvailable):
                intake.update_draft(conn, page["id"], {"style": "solid"})
    finally:
        other.close()
    assert (_versions(conn, biz), _knowledge(conn, biz)) == before
    assert _stored_draft(conn, page["id"]) is None and page["site_draft"] is None


def _another_writer(page_id: str, changes: dict, held: threading.Event, seconds: float, failures: list) -> None:
    """Another caller (its own connection): takes the page row, sets `changes` on the draft and holds the row `seconds`
    before it commits."""
    try:
        with db.connect(TEST_DB_URL) as other, other.transaction():
            row = other.execute("SELECT site_draft FROM page WHERE id = %s FOR UPDATE", (page_id,)).fetchone()
            other.execute(
                "UPDATE page SET site_draft = %s WHERE id = %s", (Jsonb({**intake.draft(row), **changes}), page_id)
            )
            held.set()
            time.sleep(seconds)
    except Exception as e:  # noqa: BLE001 - kept for the test to say, not lost with the thread
        failures.append(e)
        held.set()


@pytest.mark.parametrize(
    ("write", "kept"),
    [
        (lambda conn, biz, page: intake.merge(conn, biz, page, {"warranty": "10 years"}), "extras_asked"),
        (lambda conn, biz, page: intake.update_draft(conn, page["id"], {"style": "bold"}), "extras_asked"),
    ],
    ids=["merge", "update_draft"],
)
def test_a_draft_write_waits_for_a_writer_in_progress_and_keeps_what_it_wrote(conn, write, kept):
    """Another caller sets `extras_asked` and holds the page row: the write that starts meanwhile waits for it, then
    reads the draft with that key in it -- it never writes a copy read before the other's commit over it."""
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    held, failures = threading.Event(), []
    writer = threading.Thread(target=_another_writer, args=(page["id"], {kept: True}, held, 0.5, failures))
    writer.start()
    assert held.wait(10)
    began = time.monotonic()
    written = write(conn, biz, page)  # blocks on the row until the other writer's transaction ends
    waited = time.monotonic() - began
    writer.join()
    assert failures == [] and waited > 0.1
    assert written[kept] is True and _stored_draft(conn, page["id"])[kept] is True
    assert _stored_draft(conn, page["id"]) == written


def test_update_draft_sets_its_own_keys_and_never_overwrites_another_writers(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.save_draft(conn, page["id"], {**intake.draft(None), "family": "home_services", "facts": {"about": "Hi."}})
    stale = landing.get(conn, page["id"])  # read before another writer set its key
    intake.update_draft(conn, page["id"], {"extras_asked": True})
    out = intake.update_draft(conn, page["id"], {"style": "bold", "accent": "#C2410C"})
    stored = _stored_draft(conn, page["id"])
    assert out == stored
    assert (stored["family"], stored["style"], stored["accent"], stored["extras_asked"]) == (
        "home_services",
        "bold",
        "#C2410C",
        True,
    )
    assert stored["facts"] == {"about": "Hi."} and stale["site_draft"]["extras_asked"] is False
    # a page row that is gone: nothing to write, and the draft the caller meant is still answered
    assert intake.update_draft(conn, "pg_nope", {"style": "calm"})["style"] == "calm"


def test_update_draft_leaves_the_facts_to_merge(conn):
    """The facts are `merge`'s (the profile's own go to the profile): `update_draft` refuses them and writes nothing
    (Task 4 follow-up)."""
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.save_draft(conn, page["id"], {**intake.draft(None), "facts": {"about": "Hi."}})
    with pytest.raises(ValueError, match="merge"):
        intake.update_draft(conn, page["id"], {"style": "bold", "facts": {"about": "Bye."}})
    stored = _stored_draft(conn, page["id"])
    assert stored["facts"] == {"about": "Hi."} and stored["style"] is None


def test_a_patch_with_no_address_reads_the_knowledge_once_under_the_lock(conn, monkeypatch):
    """No address, nothing to put on the map: the step before the lock reads nothing (Task 4 follow-up)."""
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    reads: list[str] = []
    real = business.latest_knowledge
    monkeypatch.setattr(business, "latest_knowledge", lambda c, b: reads.append(b) or real(c, b))
    intake.merge(conn, biz, page, {"phone": "(704) 555-0199", "warranty": "10 years"})
    assert reads == [biz] and _knowledge(conn, biz)["phone"] == "(704) 555-0199"


def test_a_bad_patch_writes_nothing(conn):
    """Review Focus: a good key beside a bad one -- the knowledge, the draft and the page are as they were."""
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    before = (_versions(conn, biz), _knowledge(conn, biz))
    for patch in (
        {"name": "Oakline Roofing", "phone": "555-0142"},
        {"name": "Oakline Roofing", "warranty": "10 years", "wheelbarrow": "yes"},
        {"services": [f"Service {n}" for n in range(40)], "about": "Family owned."},
        {"years": 500, "phone": "(704) 555-0142"},
    ):
        with pytest.raises(ToolError):
            intake.merge(conn, biz, page, patch)
    assert (_versions(conn, biz), _knowledge(conn, biz)) == before
    assert _stored_draft(conn, page["id"]) is None and page["site_draft"] is None


def test_merge_adds_only_the_services_not_on_file_by_the_names_the_profile_tells_apart(conn):
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    intake.merge(conn, biz, page, {"services": ["ROOF  repair", "Gutters", "gutters!", {"name": "Skylights"}]})
    assert [(s["id"], s["name"]) for s in profile.services(_knowledge(conn, biz))] == [
        ("svc_roof_repair", "Roof repair"),
        ("svc_gutters", "Gutters"),
        ("svc_skylights", "Skylights"),
    ]


def test_a_services_price_line_is_the_profiles_price_hint_and_the_names_it_marks_lead_are_remembered(conn):
    """One truth: a price the owner gives for a service is the profile's `price_hint`. The profile has no place for
    which services lead (the plan's picks and focus are the plan's), so the names a patch marks `lead` are the draft's:
    they lie over the profile's flags."""
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    intake.merge(
        conn,
        biz,
        page,
        {
            "services": [
                {"name": "Roof repair", "price_line": "From $350"},
                {"name": "Gutters", "price_line": "From $120", "lead": True},
                {"name": "Skylights"},
            ]
        },
    )
    prices = {s["name"]: s.get("price_hint") for s in _knowledge(conn, biz)["services"]}
    assert prices == {"Roof repair": "From $350", "Gutters": "From $120", "Skylights": None}
    facts, _ = intake.facts(conn, biz, page)
    assert [(s.name, s.price_line, s.lead) for s in facts.services] == [
        ("Roof repair", "From $350", False),  # the first would lead, the plan having picked none: the owner's say wins
        ("Gutters", "From $120", True),
        ("Skylights", None, False),
    ]
    assert intake.draft(page)["facts"]["leads"] == ["Gutters"]


def test_the_names_a_patch_marks_lead_lie_over_the_plans_lead_flags(conn):
    biz = _business(conn, _roofer())
    _consulted(conn, biz)  # the plan leads with Roof replacement
    page = _page(conn, biz)
    assert [s.lead for s in intake.facts(conn, biz, page)[0].services] == [False, True, False]
    draft = intake.merge(
        conn, biz, page, {"services": [{"name": "Gutter cleaning", "lead": True}, {"name": "Skylights"}]}
    )
    assert draft["facts"]["leads"] == ["Gutter cleaning"]
    facts, _ = intake.facts(conn, biz, page)
    assert [(s.name, s.lead) for s in facts.services] == [
        ("Roof repair", False),
        ("Roof replacement", False),  # the plan's pick no longer leads: the list is the owner's
        ("Gutter cleaning", True),
        ("Skylights", False),
    ]


def test_a_lead_is_remembered_by_the_profiles_own_spelling_of_the_name_until_another_patch_marks_others(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    draft = intake.merge(conn, biz, page, {"services": [{"name": "gutter CLEANING!", "lead": True}]})
    assert draft["facts"]["leads"] == ["Gutter cleaning"]  # the name on file, not a second service
    assert len(_knowledge(conn, biz)["services"]) == 3
    # a patch that marks no lead leaves the list as it was
    draft = intake.merge(conn, biz, page, {"services": ["Skylights", {"name": "Chimneys", "lead": False}]})
    assert draft["facts"]["leads"] == ["Gutter cleaning"]
    # one that marks others replaces it (a list replaces)
    draft = intake.merge(conn, biz, page, {"services": [{"name": "Skylights", "lead": True}, "Roof repair"]})
    assert draft["facts"]["leads"] == ["Skylights"]
    facts, _ = intake.facts(conn, biz, page)
    assert [s.name for s in facts.services if s.lead] == ["Skylights"]


def test_remembered_leads_that_name_no_service_any_more_leave_the_profiles_flags_standing(conn):
    biz = _business(conn, _roofer())
    _consulted(conn, biz)
    page = _page(conn, biz)
    intake.save_draft(conn, page["id"], {**intake.draft(None), "facts": {"leads": ["A service taken off since"]}})
    facts, _ = intake.facts(conn, biz, landing.get(conn, page["id"]))
    assert [s.lead for s in facts.services] == [False, True, False]  # the plan's pick
    intake.save_draft(conn, page["id"], {**intake.draft(None), "facts": {"leads": []}})  # an empty list says nothing
    assert [s.lead for s in intake.facts(conn, biz, landing.get(conn, page["id"]))[0].services] == [False, True, False]


def test_no_more_than_six_services_lead_whatever_the_owner_marks(conn):
    names = [f"Service {n}" for n in range(1, 9)]
    biz = _business(conn, _roofer(services=[{"name": n} for n in names]))
    page = _page(conn, biz)
    draft = intake.merge(conn, biz, page, {"services": [{"name": n, "lead": True} for n in reversed(names)]})
    assert draft["facts"]["leads"] == list(reversed(names))[:6]  # the first six marked
    facts, _ = intake.facts(conn, biz, page)
    assert [s.name for s in facts.services if s.lead] == names[2:]  # six, in the order the services stand


def test_a_patch_past_the_limit_of_services_writes_nothing(conn):
    """Thirty on file and three new: the profile refuses, and neither the knowledge nor the draft has changed."""
    thirty = [{"name": f"Service {n}"} for n in range(1, 31)]
    biz = _business(conn, _thin(services=thirty))
    page = _page(conn, biz)
    before = (_versions(conn, biz), _knowledge(conn, biz))
    with pytest.raises(ToolError, match="at most 30 services"):
        intake.merge(conn, biz, page, {"services": ["Gutters", "Skylights", "Chimneys"], "warranty": "10 years"})
    assert (_versions(conn, biz), _knowledge(conn, biz)) == before and _stored_draft(conn, page["id"]) is None


def test_no_lookup_is_spent_on_a_patch_the_profile_refuses(conn, places_fake):
    """The rest of the patch is checked as the profile will apply it before the address is put on the map."""
    thirty = [{"name": f"Service {n}"} for n in range(1, 31)]
    biz = _business(conn, _thin(services=thirty))
    with pytest.raises(ToolError, match="at most 30 services"):
        intake.merge(conn, biz, _page(conn, biz), {"address": "123 Main St", "services": ["Gutters"]})
    assert places_fake.calls == []


def test_merge_refuses_a_patch_for_a_business_with_nothing_on_file_and_changes_nothing(conn):
    biz = _business(conn, None)
    page = _page(conn, biz)
    with pytest.raises(ToolError, match="Admiry knows nothing"):
        intake.merge(conn, biz, page, {"name": "Oakline Roofing"})
    assert _versions(conn, biz) == 0 and _stored_draft(conn, page["id"]) is None


def test_an_address_is_put_on_the_map_when_the_key_is_set(conn, places_fake):
    """The way the consultation's location answer adopts a place: the lookup's listing, kind `address`, and its town
    among the areas."""
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    intake.merge(conn, biz, page, {"address": "123 Main St"})
    assert places_fake.calls == ["123 Main St"]
    found = places.Candidate("ChIJ2", "123 Main St", "123 Main St, Houston, TX 77002", 29.76, -95.37)
    knowledge = _knowledge(conn, biz)
    assert knowledge["listing"] == geocode.listing(found)
    assert knowledge["service_area"] == ["Houston"]
    facts, missing = intake.facts(conn, biz, page)
    assert (facts.city, facts.state, facts.address) == ("Houston", "TX", "123 Main St, Houston, TX 77002")
    assert "place" not in missing
    lookups = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'places.lookup'").fetchone()["n"]
    assert lookups == 1  # counted against the day's Places budget like every other lookup


def test_an_address_the_map_cannot_place_or_may_not_today_is_kept_as_the_owner_typed_it(conn, monkeypatch, places_none):
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    intake.merge(conn, biz, page, {"address": "123 Main St, Charlotte NC"})  # Places found nothing
    assert _knowledge(conn, biz)["listing"] == {"kind": "address", "address": "123 Main St, Charlotte NC"}
    monkeypatch.setattr(geocode, "lookups_left", lambda *args, **kwargs: False)  # the day's lookups are spent
    intake.merge(conn, biz, page, {"address": "9 Elm Ct, Charlotte, NC 28203"})
    assert _knowledge(conn, biz)["listing"] == {"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203"}
    assert _knowledge(conn, biz)["service_area"] == ["Charlotte"]  # the town the address says


def test_a_change_that_lands_during_the_address_lookup_is_not_lost(conn, monkeypatch):
    """The lookup is the slow step: another caller's change that lands while it runs is in the version this call
    writes, because the knowledge is read after the lookup, right before it is written back."""
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    biz = _business(conn, _thin())
    page = _page(conn, biz)
    real = intake._placed

    def lookup(conn_, biz_, address):
        business.add_knowledge(conn_, biz_, {**_knowledge(conn_, biz_), "phone": "(980) 555-0111"}, source="owner")
        return real(conn_, biz_, address)

    monkeypatch.setattr(intake, "_placed", lookup)
    intake.merge(conn, biz, page, {"name": "Oakline Roofing", "address": "123 Main St, Charlotte, NC 28202"})
    after = _knowledge(conn, biz)
    assert (after["phone"], after["name"]) == ("(980) 555-0111", "Oakline Roofing")  # both the other's and this one's
    assert after["listing"] == {"kind": "address", "address": "123 Main St, Charlotte, NC 28202"}
    assert _versions(conn, biz) == 3


def test_an_address_never_unlinks_a_confirmed_google_listing(conn, places_fake):
    """The business's own listing, as the owner linked it, stays whatever address a patch gives: the typed address is
    the draft's -- the site prints it -- and neither the knowledge, the map nor the day's lookups are touched."""
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    before = (_versions(conn, biz), _knowledge(conn, biz))
    draft = intake.merge(conn, biz, page, {"address": "200 Moved Rd, Matthews, NC 28105", "warranty": "10 years"})
    assert (_versions(conn, biz), _knowledge(conn, biz)) == before
    assert _knowledge(conn, biz)["listing"] == _listing()
    assert places_fake.calls == []
    assert draft["facts"] == {"address": "200 Moved Rd, Matthews, NC 28105", "warranty": "10 years"}
    facts, _ = intake.facts(conn, biz, page)
    assert (facts.address, facts.city, facts.state) == ("200 Moved Rd, Matthews, NC 28105", "Matthews", "NC")
    assert (facts.gbp_url, facts.place_id) == (places.maps_url("Oakline Roofing", "ChIJ_oak"), "ChIJ_oak")


def test_the_listings_own_address_said_again_drops_the_one_the_draft_kept(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.merge(conn, biz, page, {"address": "200 Moved Rd, Matthews, NC 28105"})
    assert intake.facts(conn, biz, page)[0].address == "200 Moved Rd, Matthews, NC 28105"
    draft = intake.merge(conn, biz, page, {"address": "100 Trade St, Charlotte, NC 28202, USA"})
    assert "address" not in draft["facts"] and _knowledge(conn, biz)["listing"] == _listing()
    assert intake.facts(conn, biz, page)[0].address == "100 Trade St, Charlotte, NC 28202, USA"


def test_an_address_the_profile_takes_replaces_the_one_the_draft_kept(conn, monkeypatch):
    """The draft's address lies over the profile's: when the listing is unlinked and the owner gives another address,
    the profile has the latest, so the draft's older one goes."""
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.merge(conn, biz, page, {"address": "200 Moved Rd, Matthews, NC 28105"})  # kept by the draft
    assert intake.draft(page)["facts"]["address"] == "200 Moved Rd, Matthews, NC 28105"
    unlinked = {k: v for k, v in _knowledge(conn, biz).items() if k != "listing"}
    business.add_knowledge(conn, biz, unlinked, source="owner")
    draft = intake.merge(conn, biz, page, {"address": "9 Elm Ct, Charlotte, NC 28203"})
    assert "address" not in draft["facts"]
    assert _knowledge(conn, biz)["listing"] == {"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203"}
    assert intake.facts(conn, biz, page)[0].address == "9 Elm Ct, Charlotte, NC 28203"


def test_an_address_still_replaces_the_place_the_owner_gave_when_no_listing_is_confirmed(conn, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    biz = _business(conn, _roofer(listing={"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203, USA"}))
    draft = intake.merge(conn, biz, _page(conn, biz), {"address": "200 Moved Rd, Matthews, NC 28105"})
    assert _knowledge(conn, biz)["listing"] == {"kind": "address", "address": "200 Moved Rd, Matthews, NC 28105"}
    assert "address" not in draft["facts"]


def test_an_address_already_on_file_is_not_put_on_the_map_again(conn, places_fake):
    """Said again, the address changes nothing: no lookup is spent and no version is written."""
    biz = _business(conn, _roofer())
    before = _versions(conn, biz)
    draft = intake.merge(conn, biz, _page(conn, biz), {"address": "100 trade st, Charlotte, NC 28202, USA"})
    assert places_fake.calls == [] and _versions(conn, biz) == before and draft["facts"] == {}


# ---------------------------------------------------------------- places.state


@pytest.mark.parametrize(
    ("address", "state"),
    [
        ("123 Main St, Houston, TX 77002, USA", "TX"),
        ("Houston, TX 77002, USA", "TX"),
        ("Houston, TX, USA", "TX"),
        ("500 University Ave, Palo Alto, CA 94301", "CA"),
        ("Suite 200, 123 Main St, Houston, TX 77002-1234, USA", "TX"),
        ("100 Trade St, Charlotte, NC 28202, USA", "NC"),
        ("Paris, France", None),
        ("Palo Alto", None),
        ("123 Main St, Charlotte NC", None),
        ("TX 77002, USA", None),
        ("", None),
        (None, None),
    ],
)
def test_the_state_of_an_address_is_the_two_letters_of_its_state_part(address, state):
    assert places.state(address) == state


# ---------------------------------------------------------------- the schema, the purge


def test_the_schema_applies_again_and_has_the_drafts_column_and_the_photos_kind(conn):
    db.apply_schema(conn)  # every statement is IF NOT EXISTS: a second application changes nothing
    found = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE (table_name, column_name) IN (('page', 'site_draft'), ('page_photo', 'kind'))"
    ).fetchall()
    assert {(r["table_name"], r["column_name"]): r["data_type"] for r in found} == {
        ("page", "site_draft"): "jsonb",
        ("page_photo", "kind"): "text",
    }


def test_the_purge_clears_the_draft_with_the_rest_of_the_owners_facts(conn):
    biz = _business(conn, _roofer())
    page = _page(conn, biz)
    intake.save_draft(conn, page["id"], {**intake.draft(None), "facts": {"warranty": "10 years"}})
    conn.execute(
        "UPDATE page SET status = 'unpublished', taken_down_reason = 'hosting_minimum', purge_after = %s WHERE id = %s",
        (date(2020, 1, 1), page["id"]),
    )
    assert _stored_draft(conn, page["id"]) is not None
    assert hosting_minimum.purge(conn, biz, page["id"], {"purged": 0}) is True
    assert _stored_draft(conn, page["id"]) is None
