"""The NewsBreak build (M1 Task 3; `agent/channels/newsbreak/build.py`): what a plan becomes as NewsBreak campaign, ad
set and ad bodies, the copy linter with NewsBreak's limits, the click URL, and the diff between two plan versions.
Pure functions -- no database and no network anywhere here, except the golden (it walks the dental consultation the
Google golden walks) and one test that hands the bodies to `FakeNewsBreak`, which enforces the fields the docs
require. Every create carries `status: "OFF"` (NewsBreak's default is ON); every ad-set update carries the whole
targeting (NewsBreak widens an omitted one to everyone)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent import config, plans
from agent.channels.google_search import build as ads_launch
from agent.channels.newsbreak import build
from agent.channels.newsbreak import client as newsbreak
from agent.tests import fake_newsbreak
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.test_ads_launch import _dental_plan_and_knowledge
from agent.tests.test_tools_consult import fake  # noqa: F401 (fixture)

GOLDEN_DIR = Path(__file__).parent / "golden"
BANNED = ["guaranteed", "guarantee", "best", "#1"]
LOCATION_IDS = ["1813859874", "1813859875"]
ASSET_URL = "https://static.particlenews.com/nova/assets/4001/6f1c.jpg"
PAGE_URL = "https://brightsmile.example/invisalign"
CLICK_URL = "https://brightsmile.example/invisalign?src=nb&newsbreak_cid=__CALLBACK_PARAM__"
BRAND = "Bright Smile Dental"
START, END = 1_790_000_000, 1_821_536_000
ALL = {"positive": ["all"]}

HEADLINES = ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners Near You"]
DESCRIPTIONS = ["Clear aligners from a family dentist. Book online.", "Straighter teeth without metal braces."]
BOOKINGS = ("consultation_booking", "phone_call_60s")
NEW_HEADLINES = ["Straighter Teeth, Sooner", "Clear Aligners Near You"]

# what a launch left on NewsBreak: the ad set and the two ads (slots "0" and "1") the plan's two pairs became
RESOURCES = {"ad_account_id": "4001", "campaign": "4002", "ad_set_id": "4003", "ads": {"0": "4010", "1": "4011"}}


def plan_with(
    *,
    monthly_usd: float = 600.0,
    cpl: str | None = "$51–109",
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    radius: float = 5.0,
    center: str = "ChIJ1",
    conversions: tuple[str, ...] = BOOKINGS,
    image: str | None = None,
) -> dict:
    """A plan version funding Google Search and NewsBreak the way the allocator writes them: NewsBreak's entry
    carries Google's words (`copy_of`), its own amount, and no cost per lead of its own."""
    google = {
        "channel": "google_search",
        "monthly_usd": 1400.0,
        "themes": ["invisalign palo alto"],
        "negatives": ["jobs"],
        "headlines": ["Not NewsBreak's headline"],
        "descriptions": ["Not NewsBreak's description."],
        "landing_service": "Invisalign",
    }
    newsbreak_entry = {
        **google,
        "channel": "newsbreak",
        "monthly_usd": monthly_usd,
        "headlines": list(HEADLINES if headlines is None else headlines),
        "descriptions": list(DESCRIPTIONS if descriptions is None else descriptions),
        "role": "test",
        "mode": "managed",
        "copy_of": "google_search",
    }
    assumptions = [{"name": "cost per lead", "value": cpl}] if cpl else []
    assumptions.append({"name": "cost per lead, NewsBreak", "value": "not published"})
    return {
        "business_id": "biz_1",
        "sections": {
            "consultation": {
                "business": {"category": "dentists", "service": "Invisalign", "services": ["Invisalign"]},
                "geo": {"radius_miles": radius, "center": center},
                "conversions": list(conversions),
            },
            "ads": {"channels": [google, newsbreak_entry]},
            "estimate": {"assumptions": assumptions},
            "creative": {"angles": ["offer"], **({"approved_image": {"newsbreak": image}} if image else {})},
        },
    }


def run_diff(old: dict, new: dict, *, location_ids: list[str] | None = None, resources: dict | None = None, **kw):
    return build.diff(
        old,
        new,
        RESOURCES if resources is None else resources,
        location_ids=LOCATION_IDS if location_ids is None else location_ids,
        asset_url=ASSET_URL,
        click_url=CLICK_URL,
        brand=BRAND,
        **kw,
    )


def ads_for(plan: dict, **kw) -> list[dict]:
    """The ads this plan's words become, built straight from the primitives (what `diff`'s creates must equal)."""
    headlines, descriptions, _notes = build.ad_copy(plan)
    return build.ad_bodies(
        ad_set_id=RESOURCES["ad_set_id"],
        brand=BRAND,
        headlines=headlines,
        descriptions=descriptions,
        cta=build.call_to_action(plan),
        asset_url=ASSET_URL,
        logo_url=None,
        click_url=CLICK_URL,
        **kw,
    )


def whole(ids: list[str] | None = None) -> dict:
    """The targeting an ad-set update must carry: every dimension, the ZIP codes and English named."""
    return build.targeting(LOCATION_IDS if ids is None else ids, "en_us")


# ---------------------------------------------------------------- the numbers


def test_the_text_limits_are_the_clients_and_the_money_constants_the_briefs():
    assert build.HEADLINE_MAX_NB == newsbreak.HEADLINE_MAX == 90
    assert build.DESCRIPTION_MAX_NB == newsbreak.DESCRIPTION_MAX == 90
    assert (build.CPC_SHARE, build.CPC_MIN_CENTS, build.CPC_MAX_CENTS) == (0.05, 40, 150)
    assert (build.DAILY_MIN_CENTS, build.MAX_ADS) == (1000, 3)
    assert (build.OBJECTIVE, build.LANGUAGE) == ("WEB_TRAFFIC", "en_us")


def test_daily_cents_is_the_month_over_30_point_4_floored_to_the_cent_never_below_ten_dollars():
    assert build.daily_cents(300) == 1000  # 986.8 cents floors to 986: under NewsBreak's $10 minimum, so 1000
    assert build.daily_cents(1) == 1000 and build.daily_cents(0) == 1000
    assert build.daily_cents(304) == 1000 and build.daily_cents(306) == 1006
    assert build.daily_cents(2200) == 7236  # 7236.8 floors, never rounds up
    assert build.daily_cents(600) == 1973


def test_daily_cents_is_googles_daily_budget_for_the_same_amount():
    """One rule for every channel: the month over 30.4 days, floored to the cent."""
    for monthly in (304, 450, 999, 1400, 2200, 5000):
        assert build.daily_cents(monthly) * 10_000 == ads_launch.daily_micros(monthly)


def test_cpc_cents_is_five_percent_of_the_cost_per_lead_between_40_and_150_cents():
    def estimate(value: str) -> dict:
        return {"assumptions": [{"name": "cost per lead", "value": value}]}

    assert build.cpc_cents(estimate("$51–109")) == 150  # midpoint 80 -> 400 cents, capped
    assert build.cpc_cents(estimate("$51-109")) == 150  # a plain hyphen reads the same
    assert build.cpc_cents(estimate("$10-30")) == 100  # midpoint 20 -> 100
    assert build.cpc_cents(estimate("$12-36")) == 120
    assert build.cpc_cents(estimate("$5-9")) == 40  # midpoint 7 -> 35, raised to the floor
    assert build.cpc_cents({"cpl_avg": 12}) == 60 and build.cpc_cents({"cpl_low": 10, "cpl_high": 14}) == 60


def test_cpc_cents_without_a_cost_per_lead_is_the_ceiling():
    """NewsBreak alone has no published cost per lead (`cost per lead, NewsBreak: not published`), and the pilot's
    plan may be exactly that: the bid is the ceiling D89 allows, and the day's budget is what limits the spend."""
    only_newsbreak = {"assumptions": [{"name": "cost per lead, NewsBreak", "value": "not published"}]}
    assert build.cpc_cents(only_newsbreak) == build.CPC_MAX_CENTS == 150
    assert build.cpc_cents({"assumptions": [{"name": "cost per lead", "value": "not published"}]}) == 150
    assert build.cpc_cents({}) == 150


def test_campaign_name_carries_the_business_id_for_an_exact_lookup():
    assert build.campaign_name("Bright Smile Dental", "biz_1") == "Admiry · Bright Smile Dental · biz_1"
    assert build.campaign_name(None, "biz_1") == "Admiry · biz_1"
    assert build.campaign_name("", "biz_1") == "Admiry · biz_1"


# ---------------------------------------------------------------- the copy linter


def test_lint_keeps_clean_lines_and_the_limits_are_newsbreaks_not_googles():
    thirty_one = "a" * 31  # over Google's 30: NewsBreak's headline takes 90
    h, d, notes = build.lint(["Invisalign in Palo Alto", thirty_one], ["Clear aligners from a family dentist."], BANNED)
    assert h == ["Invisalign in Palo Alto", thirty_one] and d == ["Clear aligners from a family dentist."]
    assert notes == []
    assert build.lint(["a" * 90], ["b" * 90], BANNED)[:2] == (["a" * 90], ["b" * 90])
    h, d, notes = build.lint(["a" * 91], ["b" * 91], BANNED)
    assert (h, d) == ([], [])
    assert notes == [
        f'Dropped a headline ("{"a" * 91}"): it is over 90 characters.',
        f'Dropped a description ("{"b" * 91}"): it is over 90 characters.',
    ]


def test_lint_counts_a_double_width_character_twice():
    """NewsBreak's spec: "a double-width character counts twice" -- 90 is a width, not a count of characters."""
    wide = "牙"
    fits_h, fits_d = "a" * 88 + wide, "b" * 88 + wide  # 88 + 2 = 90
    assert build.lint([fits_h], [fits_d], [])[:2] == ([fits_h], [fits_d])
    over_h, over_d = "a" * 89 + wide, "b" * 89 + wide  # 89 + 2 = 91, though only 90 characters
    h, d, notes = build.lint([over_h], [over_d], [])
    assert (h, d) == ([], [])
    assert notes == [
        f'Dropped a headline ("{over_h}"): it is over 90 characters (a wide character counts as two).',
        f'Dropped a description ("{over_d}"): it is over 90 characters (a wide character counts as two).',
    ]
    assert build.lint([wide * 45], [wide * 45], [])[:2] == ([wide * 45], [wide * 45])  # exactly 90
    assert build.lint([wide * 46], [wide * 46], [])[:2] == ([], [])
    # the full-width forms (Ａ, ｂ…) are double-width too, as are emoji
    assert build.lint(["ａ" * 45], ["😀" * 45], [])[:2] == (["ａ" * 45], ["😀" * 45])
    assert build.lint(["ａ" * 46], ["😀" * 46], [])[:2] == ([], [])


def test_lint_allows_the_city_placeholder_once_in_a_line_and_counts_it_at_its_written_length():
    lines = ["Invisalign in {city}", "Invisalign in {City}", "Family dentist in {city}, book today"]
    assert build.lint(lines, ["Serving {city} families."], BANNED)[:2] == (lines, ["Serving {city} families."])
    lower, upper = "a" * 83 + " {city}", "a" * 83 + " {City}"  # 83 + 1 + 6 = 90: the placeholder counts as written
    assert build.lint([lower, upper], [], [])[0] == [lower, upper]
    assert build.lint(["a" * 84 + " {city}"], [], [])[0] == []  # 91
    h, d, notes = build.lint(["{city} dentist in {City}"], ["{city} and {city}"], [])
    assert (h, d) == ([], [])
    assert notes == [
        'Dropped a headline ("{city} dentist in {City}"): it uses the city placeholder more than once.',
        'Dropped a description ("{city} and {city}"): it uses the city placeholder more than once.',
    ]


@pytest.mark.parametrize(
    "text", ["Invisalign in {state}", "Invisalign {CITY}", "Invisalign in {", "Invisalign } now", "{}"]
)
def test_lint_drops_a_placeholder_newsbreak_is_not_asked_to_fill_in(text):
    h, _d, notes = build.lint([text], [], [])
    assert h == [] and notes == [f'Dropped a headline ("{text}"): it has a placeholder we do not use.']


@pytest.mark.parametrize(
    "text, reason",
    [
        ("The #1 Choice in Town", 'uses a word we avoid ("#1")'),
        ("Book Today!", "has an exclamation mark"),
        ("Call (650) 555-0100 Now", "includes a phone number or e-mail address"),
        ("Email owner@example.com", "includes a phone number or e-mail address"),
        ("AMAZING Smiles in {city}", "is written in all capitals"),
    ],
)
def test_lint_keeps_googles_other_rules_for_headlines(text, reason):
    h, _d, notes = build.lint([text], [], BANNED)
    assert h == [] and notes == [f'Dropped a headline ("{text}"): it {reason}.']


def test_lint_keeps_googles_other_rules_for_descriptions_one_exclamation_mark_allowed():
    assert build.lint([], ["Book today! Call now."], BANNED)[1] == ["Book today! Call now."]
    _h, d, notes = build.lint([], ["Book today! Call now!", "The best dentist around"], BANNED)
    assert d == []
    assert notes == [
        'Dropped a description ("Book today! Call now!"): it has more than one exclamation mark.',
        'Dropped a description ("The best dentist around"): it uses a word we avoid ("best").',
    ]


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_lint_drops_a_blank_line(text):
    """(M1 Task 5a.) A blank line is no copy: kept, it would become an ad with nothing to read -- at launch and in a
    `diff`'s replacement ads alike, which lint the same way."""
    h, d, notes = build.lint([text, "Invisalign"], [text, "Book today."], [])
    assert (h, d) == (["Invisalign"], ["Book today."])
    assert notes == [f'Dropped a headline ("{text}"): it is blank.', f'Dropped a description ("{text}"): it is blank.']


def test_lint_keeps_the_order_of_the_lines_and_of_the_notes():
    h, d, notes = build.lint(["one", "!bad!", "two", "a" * 91, "three"], ["x", "y" * 91, "z"], [])
    assert (h, d) == (["one", "two", "three"], ["x", "z"])
    assert [n.split('"')[1][:6] for n in notes] == ["!bad!", "a" * 6, "y" * 6]  # headlines first, in the order given


def test_lint_notes_never_use_advertising_or_account_or_money_jargon():
    _h, _d, notes = build.lint(
        [
            "a" * 91,
            "牙" * 46,
            "{city} {city}",
            "{state}",
            "Best Deal Ever!",
            "Call (650) 555-0100",
            "SHOUTING HEADLINE",
        ],
        ["c" * 91, "Two! Bangs! Here!", "{x}"],
        BANNED,
    )
    assert len(notes) == 10
    for note in notes:
        assert not (JARGON.search(note) or ACCOUNT_JARGON.search(note) or MONEY_JARGON.search(note))


# ---------------------------------------------------------------- the name NewsBreak shows


def test_a_business_name_that_fits_is_shown_as_it_is():
    assert build.brand_name("Bright Smile Dental") == "Bright Smile Dental"
    assert build.brand_name("  Bright   Smile \n Dental ") == "Bright Smile Dental"  # blanks tidied
    exactly = "Abcdefghij Abcdefghij Abc"
    assert len(exactly) == 25 and build.brand_name(exactly) == exactly


@pytest.mark.parametrize(
    "name, shown",
    [
        ("Bright Smile Family Dentistry of Palo Alto", "Bright Smile Family"),  # the next word would end at 29
        ("Abcdefghij Abcdefghij Abc Defgh", "Abcdefghij Abcdefghij Abc"),  # the 25th character ends a word
        ("Palo Alto Family Dentistry, Inc.", "Palo Alto Family"),  # "Dentistry" ends at the 26th
        ("Acme Plumbing, Heating & Air Conditioning", "Acme Plumbing, Heating"),  # no dangling "&"
        ("Smith, Jones, Brown - Associates Law Group", "Smith, Jones, Brown"),  # nor "-"
    ],
)
def test_a_longer_name_is_cut_at_the_last_word_that_fits_with_no_ellipsis(name, shown):
    assert len(name) > newsbreak.BRAND_NAME_MAX
    assert build.brand_name(name) == shown
    assert len(shown) <= newsbreak.BRAND_NAME_MAX and "…" not in shown and "..." not in shown


@pytest.mark.parametrize(
    "name, shown",
    [
        ("Palo Alto Dentistry of Excellence", "Palo Alto Dentistry"),  # never "… Dentistry of"
        ("Smile Dentistry of the Bay Area", "Smile Dentistry"),  # every connective at the end goes
        ("Kids Teeth and Braces for Everyone", "Kids Teeth and Braces"),  # one inside the name stays
        ("Smiles In Motion At The Lakeshore", "Smiles In Motion"),  # whatever their case
        ("The Dental Studio at Stanford Shopping Center", "The Dental Studio"),  # a leading "The" stays
        ("Rivera Family Plumbing & Heating", "Rivera Family Plumbing"),  # "&" as a word, too
    ],
)
def test_a_cut_name_never_ends_on_a_connective(name, shown):
    """Final review (deferred minor, public ad text): the advertiser name NewsBreak shows never ends on a word that
    joins it to one cut away -- of, and, &, the, for, in, at -- dropped after the cut; a whole name stays as it is."""
    assert build.brand_name(name) == shown
    assert build.brand_name("Dentistry of Palo Alto") == "Dentistry of Palo Alto"


def test_a_name_left_with_no_word_but_connectives_is_refused():
    with pytest.raises(build.BrandNameError) as e:
        build.brand_name("The Supercalifragilisticexpialidocious Dentist")
    assert "too long" in str(e.value)


def test_a_name_whose_first_word_does_not_fit_is_refused_with_an_owner_sentence():
    with pytest.raises(build.BrandNameError) as e:
        build.brand_name("Supercalifragilisticexpialidocious Dental")
    assert isinstance(e.value, ValueError)
    assert str(e.value) == (
        "Your business name is too long for NewsBreak's 25-character advertiser name; tell me a shorter name to show"
    )
    with pytest.raises(build.BrandNameError) as e:
        build.brand_name("牙" * 13)  # 26 wide, and no space to cut at
    assert "too long" in str(e.value)
    assert build.brand_name("牙" * 12) == "牙" * 12  # 24 wide fits


@pytest.mark.parametrize("name", [None, "", "   "])
def test_no_name_at_all_is_refused_too(name):
    with pytest.raises(build.BrandNameError) as e:
        build.brand_name(name)
    assert str(e.value) == "NewsBreak needs your business name to show with your ad; tell me the name to show"


def test_the_sentences_the_build_refuses_with_are_plain_words():
    sentences = [build.NO_ADS]
    for name in ("x" * 30, None):
        with pytest.raises(build.BrandNameError) as e:
            build.brand_name(name)
        sentences.append(str(e.value))
    for sentence in sentences:
        assert "NewsBreak" in sentence
        assert not (JARGON.search(sentence) or ACCOUNT_JARGON.search(sentence) or MONEY_JARGON.search(sentence))


# ---------------------------------------------------------------- the bodies


def test_every_create_defaults_to_off_because_newsbreaks_default_is_on():
    campaign = build.campaign_body(name="c")
    ad_set = build.ad_set_body(
        campaign_id=None,
        name="s",
        daily_cents=1000,
        cpc_cents=100,
        location_ids=LOCATION_IDS,
        language="en_us",
        start=START,
        end=END,
    )
    ads = ads_for(plan_with())
    assert campaign["status"] == "OFF" and ad_set["status"] == "OFF"
    assert len(ads) == 2 and all(ad["status"] == "OFF" for ad in ads)


def test_campaign_body_is_a_website_traffic_campaign_in_the_account():
    assert build.campaign_body(name="Admiry · Bright Smile Dental · biz_1") == {
        "adAccountId": None,
        "name": "Admiry · Bright Smile Dental · biz_1",
        "objective": "WEB_TRAFFIC",
        "status": "OFF",
    }
    assert build.campaign_body(name="c", ad_account_id="4001")["adAccountId"] == "4001"


def test_ad_set_body_is_a_daily_budget_with_a_cpc_bid_spread_evenly_over_the_zip_codes():
    body = build.ad_set_body(
        campaign_id="4002",
        name="Admiry · Bright Smile Dental · biz_1",
        daily_cents=1973,
        cpc_cents=150,
        location_ids=LOCATION_IDS,
        language="en_us",
        start=START,
        end=END,
    )
    assert body == {
        "campaignId": "4002",
        "name": "Admiry · Bright Smile Dental · biz_1",
        "budgetType": "DAILY",
        "budget": 1973,
        "startTime": START,
        "endTime": END,
        "bidType": "CPC",
        "bidRate": 150,
        "deliveryRate": "EVENLY",
        "targeting": {
            "gender": ALL,
            "ageGroup": ALL,
            "interest": ALL,
            "location": {"positive": LOCATION_IDS},
            "deviceLocation": ALL,
            "language": {"positive": ["en_us"]},
            "os": ALL,
            "manufacturer": ALL,
            "carrier": ALL,
            "network": ALL,
        },
        "status": "OFF",
    }
    unattached = build.ad_set_body(
        campaign_id=None, name="s", daily_cents=1000, cpc_cents=40, location_ids=["1"], language="es_us", start=1, end=2
    )
    assert unattached["campaignId"] is None  # the launch fills in the campaign it has just made


def test_the_targeting_names_every_dimension_newsbreak_has_and_narrows_only_place_and_language():
    t = build.targeting(LOCATION_IDS, "en_us")
    assert tuple(t) == build.TARGETING_DIMENSIONS == fake_newsbreak.TARGETING_DIMENSIONS
    assert t["location"] == {"positive": LOCATION_IDS} and t["language"] == {"positive": ["en_us"]}
    assert [d for d, v in t.items() if v == ALL] == [
        d for d in build.TARGETING_DIMENSIONS if d not in ("location", "language")
    ]
    assert all(set(v) == {"positive"} for v in t.values())  # no "negative" anywhere


@pytest.mark.parametrize("ids", [[], ["all"], ["2886", "all"], [2886], ["", "2886"], ["2886 "], None])
def test_a_body_that_would_reach_everyone_is_never_built(ids):
    """An empty list is "Targeting field cannot be empty" at best and everyone at worst; "all" is everyone."""
    with pytest.raises(ValueError, match="location"):
        build.targeting(ids, "en_us")


def test_the_language_is_one_of_newsbreaks_two_values():
    assert build.targeting(["2886"], "es_us")["language"] == {"positive": ["es_us"]}
    for wrong in ("en-US", "english", "all", "", None):
        with pytest.raises(ValueError, match="language"):
            build.targeting(["2886"], wrong)


def test_ad_bodies_are_image_ads_off_on_the_businesss_page():
    ads = build.ad_bodies(
        ad_set_id="4003",
        brand=BRAND,
        headlines=["Invisalign in Palo Alto"],
        descriptions=["Clear aligners from a family dentist."],
        cta="Call now",
        asset_url=ASSET_URL,
        logo_url=None,
        click_url=CLICK_URL,
    )
    assert len(ads) == 1
    ad = ads[0]
    assert ad["name"].startswith("Admiry ad 1 ") and ad["adSetId"] == "4003" and ad["status"] == "OFF"
    assert ad["creative"] == {
        "type": "IMAGE",
        "headline": "Invisalign in Palo Alto",
        "description": "Clear aligners from a family dentist.",
        "callToAction": "Call now",
        "brandName": BRAND,
        "assetUrl": ASSET_URL,
        "clickThroughUrl": CLICK_URL,
    }
    assert set(ad) == {"adSetId", "name", "status", "creative"}


def test_ad_bodies_carry_the_logo_only_when_there_is_one():
    kw = {
        "ad_set_id": None,
        "brand": BRAND,
        "headlines": ["h"],
        "descriptions": ["d"],
        "cta": "Call now",
        "asset_url": ASSET_URL,
        "click_url": CLICK_URL,
    }
    logo = "https://static.particlenews.com/nova/assets/4001/logo.png"
    with_logo = build.ad_bodies(**kw, logo_url=logo)
    assert with_logo[0]["creative"]["logoUrl"] == logo
    assert "logoUrl" not in build.ad_bodies(**kw, logo_url=None)[0]["creative"]
    assert with_logo[0]["adSetId"] is None  # the launch fills in the ad set it has just made


def test_ad_bodies_never_exceed_three_and_pair_the_longest_lines_first():
    ads = build.ad_bodies(
        ad_set_id="4003",
        brand=BRAND,
        headlines=["a" * 10, "b" * 30, "c" * 20, "d" * 25, "e" * 5],
        descriptions=["x" * 5, "y" * 50, "z" * 40, "w" * 45, "v" * 1],
        cta="Get a quote",
        asset_url=ASSET_URL,
        logo_url=None,
        click_url=CLICK_URL,
    )
    assert len(ads) == build.MAX_ADS == 3
    assert [(ad["creative"]["headline"], ad["creative"]["description"]) for ad in ads] == [
        ("b" * 30, "y" * 50),
        ("d" * 25, "w" * 45),
        ("c" * 20, "z" * 40),
    ]


def test_ad_bodies_pair_equal_lengths_in_the_plans_order_and_stop_at_the_shorter_list():
    kw = {"ad_set_id": None, "brand": BRAND, "cta": "Call now", "asset_url": "u", "logo_url": None, "click_url": "c"}
    ads = build.ad_bodies(headlines=["aaa", "bbb", "ccc"], descriptions=["xx", "yy"], **kw)
    assert [(a["creative"]["headline"], a["creative"]["description"]) for a in ads] == [("aaa", "xx"), ("bbb", "yy")]
    assert build.ad_bodies(headlines=["h"], descriptions=[], **kw) == []
    assert build.ad_bodies(headlines=[], descriptions=["d"], **kw) == []


def test_ad_names_tell_the_ads_apart_and_are_the_same_for_the_same_creative():
    plan = plan_with()
    first, again = ads_for(plan), ads_for(plan)
    assert [a["name"] for a in first] == [a["name"] for a in again]
    assert len({a["name"] for a in first}) == 2 and first[0]["name"].startswith("Admiry ad 1 ")
    # the same slot with other words is another name, so a lookup by name never mistakes an old ad for the new one
    other = ads_for(plan_with(headlines=NEW_HEADLINES))
    assert other[0]["name"].startswith("Admiry ad 1 ") and other[0]["name"] != first[0]["name"]
    # the ad set an ad goes into is no part of its name: a launch builds the ads before the ad set exists
    headlines, descriptions, _notes = build.ad_copy(plan)
    kw = {
        "brand": BRAND,
        "headlines": headlines,
        "descriptions": descriptions,
        "cta": "Call now",
        "asset_url": ASSET_URL,
        "logo_url": None,
        "click_url": CLICK_URL,
    }
    in_set, unplaced = build.ad_bodies(ad_set_id="4003", **kw), build.ad_bodies(ad_set_id=None, **kw)
    assert [a["name"] for a in in_set] == [a["name"] for a in unplaced] == [a["name"] for a in first]


def test_the_city_placeholder_goes_out_as_the_apis_own_lowercase_spelling():
    ads = build.ad_bodies(
        ad_set_id=None,
        brand=BRAND,
        headlines=["Invisalign in {City}"],
        descriptions=["Serving {City} and {city} families."],
        cta="Call now",
        asset_url=ASSET_URL,
        logo_url=None,
        click_url=CLICK_URL,
    )
    assert ads[0]["creative"]["headline"] == "Invisalign in {city}"
    assert ads[0]["creative"]["description"] == "Serving {city} and {city} families."


def test_ad_bodies_show_the_business_name_cut_to_fit_or_refuse_without_one():
    kw = {
        "ad_set_id": None,
        "headlines": ["h"],
        "descriptions": ["d"],
        "cta": "Call now",
        "asset_url": ASSET_URL,
        "logo_url": None,
        "click_url": CLICK_URL,
    }
    cut = build.ad_bodies(brand="Bright Smile Family Dentistry of Palo Alto", **kw)
    assert cut[0]["creative"]["brandName"] == "Bright Smile Family"
    with pytest.raises(build.BrandNameError):
        build.ad_bodies(brand="x" * 40, **kw)
    with pytest.raises(build.BrandNameError):
        build.ad_bodies(brand=None, **kw)


def test_the_two_calls_to_action_fit_the_button_and_a_longer_one_is_refused():
    assert build.CTA_CALL == "Call now" and build.CTA_QUOTE == "Get a quote"
    assert max(len(build.CTA_CALL), len(build.CTA_QUOTE)) <= newsbreak.CALL_TO_ACTION_MAX == 15
    kw = {"ad_set_id": None, "brand": BRAND, "headlines": ["h"], "descriptions": ["d"], "asset_url": "u"}
    kw |= {"logo_url": None, "click_url": "c"}
    assert build.ad_bodies(cta="x" * 15, **kw)[0]["creative"]["callToAction"] == "x" * 15
    with pytest.raises(ValueError, match="call to action"):
        build.ad_bodies(cta="x" * 16, **kw)


@pytest.mark.parametrize(
    "conversions, cta",
    [
        (BOOKINGS, "Call now"),
        (("form_submit", "phone_call_60s"), "Call now"),
        (("phone_call_60s",), "Call now"),
        (("phone_call_30s",), "Call now"),  # any phone-call result, as the launch rules read it
        (("form_submit",), "Get a quote"),
        (("page_view",), "Get a quote"),
        ((), "Get a quote"),
    ],
)
def test_the_button_says_call_now_when_the_plan_counts_calls_else_get_a_quote(conversions, cta):
    assert build.call_to_action(plan_with(conversions=conversions)) == cta


# ---------------------------------------------------------------- the plan's NewsBreak entry and its words


def test_entry_is_the_newsbreak_row_of_a_plan_that_funds_several_channels():
    plan = plan_with()
    assert build.entry(plan)["channel"] == "newsbreak" and build.entry(plan)["monthly_usd"] == 600.0
    google = plan["sections"]["ads"]["channels"][0]
    only_google = {**plan, "sections": {**plan["sections"], "ads": {"channels": [google]}}}
    with pytest.raises(ValueError, match="NewsBreak"):
        build.entry(only_google)
    with pytest.raises(ValueError, match="NewsBreak"):
        build.entry(plan_with(monthly_usd=0))  # a row with no money is not funded


def test_copy_is_the_plans_words_after_the_linter_with_the_trees_banned_list():
    plan = plan_with(
        headlines=[*HEADLINES, "The best Invisalign in {city}", "a" * 91],
        descriptions=[*DESCRIPTIONS, "Call (650) 555-0100 today."],
    )
    headlines, descriptions, notes = build.ad_copy(plan)
    assert headlines == HEADLINES and descriptions == DESCRIPTIONS
    assert len(notes) == 3 and 'uses a word we avoid ("best")' in notes[0]  # the dentists' own banned list


# ---------------------------------------------------------------- the click URL


@pytest.mark.parametrize(
    "url, carried",
    [
        (PAGE_URL, "https://brightsmile.example/invisalign?src=nb&newsbreak_cid=__CALLBACK_PARAM__"),
        ("https://brightsmile.example/", "https://brightsmile.example/?src=nb&newsbreak_cid=__CALLBACK_PARAM__"),
        (
            "https://brightsmile.example/p?a=1&b=two%20words",
            "https://brightsmile.example/p?a=1&b=two%20words&src=nb&newsbreak_cid=__CALLBACK_PARAM__",
        ),
        (
            "https://brightsmile.example/p#book-now",
            "https://brightsmile.example/p?src=nb&newsbreak_cid=__CALLBACK_PARAM__#book-now",
        ),
        (
            "https://brightsmile.example/p?a=1#book-now",
            "https://brightsmile.example/p?a=1&src=nb&newsbreak_cid=__CALLBACK_PARAM__#book-now",
        ),
    ],
)
def test_page_url_adds_the_source_and_the_click_id_macro_and_keeps_the_query_and_the_fragment(url, carried):
    assert build.page_url(url) == carried
    assert build.page_url(carried) == carried  # the lifecycle compares what the ads carry with this: it is idempotent
    assert "__CALLBACK_PARAM__" in carried and "%5F" not in carried  # the macro is literal: NewsBreak fills it in


def test_page_url_adds_only_what_is_missing():
    assert build.page_url("https://x.example/p?src=nb") == "https://x.example/p?src=nb&newsbreak_cid=__CALLBACK_PARAM__"
    assert build.page_url("https://x.example/p?newsbreak_cid=__CALLBACK_PARAM__") == (
        "https://x.example/p?newsbreak_cid=__CALLBACK_PARAM__&src=nb"
    )
    assert build.CLICK_ID_PARAM == "newsbreak_cid"  # never nb_cid: that one is the Pixel's, reserved by NewsBreak


# ---------------------------------------------------------------- diff


def test_diff_with_nothing_changed_is_nothing_to_do():
    assert run_diff(plan_with(), plan_with()) == []


def test_diff_a_budget_change_is_one_ad_set_update_that_still_names_every_zip_code():
    ops = run_diff(plan_with(monthly_usd=600), plan_with(monthly_usd=900))
    assert ops == [("ad_set_update", {"budget": 2960, "targeting": whole()})]
    body = ops[0][1]
    assert set(body["targeting"]) == set(build.TARGETING_DIMENSIONS)  # never a partial targeting
    assert body["targeting"]["location"] == {"positive": LOCATION_IDS}
    assert not {"status", "campaignId", "startTime", "endTime"} & set(body)  # an update turns nothing on or off


def test_diff_a_bid_change_is_one_ad_set_update_with_the_whole_targeting():
    ops = run_diff(plan_with(cpl="$10-30"), plan_with(cpl="$12-36"))
    assert ops == [("ad_set_update", {"bidRate": 120, "targeting": whole()})]


def test_diff_a_change_that_keeps_the_bid_at_its_cap_is_no_change():
    assert run_diff(plan_with(cpl="$51-109"), plan_with(cpl="$60-120")) == []  # both 150


def test_diff_a_new_area_is_one_ad_set_update_with_the_new_zip_codes_and_nothing_else_moved():
    new_ids = [*LOCATION_IDS, "1813859876"]
    ops = run_diff(plan_with(radius=5.0), plan_with(radius=10.0), location_ids=new_ids)
    assert ops == [("ad_set_update", {"targeting": whole(new_ids)})]
    moved = run_diff(plan_with(center="ChIJ1"), plan_with(center="ChIJ2"), location_ids=new_ids)
    assert moved == [("ad_set_update", {"targeting": whole(new_ids)})]


def test_diff_budget_bid_and_area_together_are_still_one_ad_set_update():
    old = plan_with(monthly_usd=600, cpl="$10-30", radius=5.0)
    new = plan_with(monthly_usd=900, cpl="$12-36", radius=10.0)
    assert run_diff(old, new) == [("ad_set_update", {"budget": 2960, "bidRate": 120, "targeting": whole()})]


def test_diff_changed_words_are_new_ads_first_and_then_the_old_ones_off():
    new = plan_with(headlines=NEW_HEADLINES)
    ops = run_diff(plan_with(), new)
    created = ads_for(new)
    assert len(created) == 2
    assert ops == [("ad_create", created[0]), ("ad_create", created[1]), ("ad_off", "4010"), ("ad_off", "4011")]
    assert all(body["status"] == "OFF" and body["adSetId"] == "4003" for kind, body in ops if kind == "ad_create")


@pytest.mark.parametrize(
    "change",
    [
        {"descriptions": ["Straighter teeth without metal braces.", "Same-week visits, in-network."]},
        {"descriptions": [*DESCRIPTIONS, "Free parking behind the clinic."]},  # a third pair is a third ad
        {"conversions": ("form_submit",)},  # the button says "Get a quote" now
        {"image": "dentists/2.jpg"},  # the owner approved another picture
    ],
    ids=["a description", "a third pair", "the button", "the approved picture"],
)
def test_diff_a_change_to_anything_an_ad_shows_replaces_the_ads(change):
    old = plan_with(image="dentists/1.jpg")
    new = plan_with(**{"image": "dentists/1.jpg", **change})
    ops = run_diff(old, new)
    created = ads_for(new)
    assert [kind for kind, _ in ops] == ["ad_create"] * len(created) + ["ad_off", "ad_off"]
    assert [body for kind, body in ops if kind == "ad_create"] == created


def test_diff_the_picture_is_the_one_the_version_pinned_whether_its_approval_stored_it_or_not():
    """Final review I3: a version shows the picture it pinned when it was made (`plans.pinned`); one whose picture left
    the set before its approval has none stored as approved, yet shows the same picture -- the ads keep it, so the
    same words on it are no change, and another pinned picture is."""
    old = plan_with(image="dentists/1.jpg")
    kept = plan_with()
    kept["sections"]["creative"]["proposed_image"] = {"newsbreak": "dentists/1.jpg"}
    assert run_diff(old, kept) == []
    other = plan_with()
    other["sections"]["creative"]["proposed_image"] = {"newsbreak": "dentists/2.jpg"}
    assert [kind for kind, _ in run_diff(old, other)] == ["ad_create", "ad_create", "ad_off", "ad_off"]


def test_diff_words_that_never_reach_an_ad_leave_the_ads_alone():
    base = plan_with()
    # "Same-Week Appointments" is the shortest headline and finds no description to pair with: swapping it is no change
    assert run_diff(base, plan_with(headlines=[HEADLINES[0], "Open Late Fridays", HEADLINES[2]])) == []
    # a line the linter drops never runs
    assert run_diff(base, plan_with(headlines=[*HEADLINES, "Book Today!"])) == []
    assert run_diff(base, plan_with(descriptions=[*DESCRIPTIONS, "Call (650) 555-0100 for a visit."])) == []


def test_diff_puts_the_ad_set_update_first_and_the_ads_after():
    ops = run_diff(plan_with(), plan_with(monthly_usd=900, headlines=NEW_HEADLINES))
    assert [kind for kind, _ in ops] == ["ad_set_update", "ad_create", "ad_create", "ad_off", "ad_off"]
    assert ops[0] == ("ad_set_update", {"budget": 2960, "targeting": whole()})


def test_diff_without_the_money_leaves_the_ad_set_to_its_caller():
    """`with_money=False` (the adapter's update, which sends the new version's whole money and area first every time,
    S16 -- final review I1): no ad-set update, whatever moved; the ads' operations as ever."""
    old = plan_with(monthly_usd=600, cpl="$10-30", radius=5.0)
    assert run_diff(old, plan_with(monthly_usd=900, cpl="$12-36", radius=10.0), with_money=False) == []
    ops = run_diff(old, plan_with(monthly_usd=900, cpl="$10-30", headlines=NEW_HEADLINES), with_money=False)
    assert [kind for kind, _ in ops] == ["ad_create", "ad_create", "ad_off", "ad_off"]


def test_diff_carries_the_logo_into_the_replacement_ads():
    new = plan_with(descriptions=["Straighter teeth without metal braces."])
    logo = "https://static.particlenews.com/nova/assets/4001/logo.png"
    with_logo = [body for kind, body in run_diff(plan_with(), new, logo_url=logo) if kind == "ad_create"]
    assert len(with_logo) == 1 and with_logo[0]["creative"]["logoUrl"] == logo
    assert all("logoUrl" not in body["creative"] for kind, body in run_diff(plan_with(), new) if kind == "ad_create")


def test_diff_turns_off_the_ads_in_the_order_of_their_slots_and_only_those_it_knows():
    resources = {**RESOURCES, "ads": {"1": "4011", "0": "4010", "2": ""}}
    new = plan_with(headlines=NEW_HEADLINES)
    offs = [x for kind, x in run_diff(plan_with(), new, resources=resources) if kind == "ad_off"]
    assert offs == ["4010", "4011"]  # slot order, and the slot with no ad is skipped
    no_ads = {k: v for k, v in RESOURCES.items() if k != "ads"}
    assert [k for k, _ in run_diff(plan_with(), new, resources=no_ads)] == ["ad_create", "ad_create"]


def test_diff_never_turns_the_old_ads_off_without_new_ones():
    """Every line the new version brings is one the linter drops: nothing could replace the running ads."""
    broken = plan_with(headlines=["Book Today!", "Call Today!"], descriptions=["Call (650) 555-0100."])
    with pytest.raises(ValueError, match="no ad words"):
        run_diff(plan_with(), broken)


def test_diff_needs_newsbreak_on_both_versions():
    only_google = plan_with()
    only_google["sections"]["ads"]["channels"].pop()
    with pytest.raises(ValueError, match="NewsBreak"):
        run_diff(plan_with(), only_google)  # a dropped channel is the framework's pause, not a diff
    with pytest.raises(ValueError, match="NewsBreak"):
        run_diff(only_google, plan_with())


def test_diff_is_pure():
    old, new = plan_with(monthly_usd=600), plan_with(monthly_usd=900, headlines=NEW_HEADLINES)
    before = json.dumps([old, new, RESOURCES], sort_keys=True)
    assert run_diff(old, new) == run_diff(old, new)
    assert json.dumps([old, new, RESOURCES], sort_keys=True) == before


# ---------------------------------------------------------------- the bodies, handed to NewsBreak's rules


def test_newsbreak_takes_the_bodies_off_and_an_update_keeps_the_zip_codes(nbapi, tmp_path):
    """`FakeNewsBreak` enforces the fields the docs mark required and models the docs' "omitted targeting is all":
    the bodies this module builds are accepted as they are, land OFF, and the update `diff` builds does not widen."""
    settings = config.load()
    account = nbapi.add_account("Admiry · Bright Smile Dental · biz_1")
    name = build.campaign_name(BRAND, "biz_1")
    campaign = newsbreak.create_campaign(settings, build.campaign_body(name=name, ad_account_id=account))
    assert (campaign["status"], campaign["objective"]) == ("OFF", "WEB_TRAFFIC")
    old = plan_with()
    ad_set = newsbreak.create_ad_set(
        settings,
        build.ad_set_body(
            campaign_id=campaign["id"],
            name=name,
            daily_cents=build.daily_cents(600),
            cpc_cents=build.cpc_cents(old["sections"]["estimate"]),
            location_ids=LOCATION_IDS,
            language=build.LANGUAGE,
            start=START,
            end=END,
        ),
    )
    stored = nbapi.ad_sets[ad_set["id"]]
    assert stored["status"] == "OFF" and stored["budget"] == 1973 and stored["bidRate"] == 150
    assert stored["targeting"]["location"] == {"positive": LOCATION_IDS}
    image = tmp_path / "dentists_1.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0 a small jpeg")
    asset = newsbreak.upload_asset(settings, account, image)
    headlines, descriptions, _notes = build.ad_copy(old)
    ads = [
        newsbreak.create_ad(settings, body)
        for body in build.ad_bodies(
            ad_set_id=ad_set["id"],
            brand=BRAND,
            headlines=headlines,
            descriptions=descriptions,
            cta=build.call_to_action(old),
            asset_url=asset,
            logo_url=None,
            click_url=CLICK_URL,
        )
    ]
    assert len(ads) == 2 and all(nbapi.ads[a["id"]]["status"] == "OFF" for a in ads)
    # a budget change: the ad set is updated in place and keeps its ZIP codes (a bare {"budget": ...} would not)
    resources = {"ad_set_id": ad_set["id"], "ads": {str(i): a["id"] for i, a in enumerate(ads)}}
    kw = {"location_ids": LOCATION_IDS, "asset_url": asset, "click_url": CLICK_URL, "brand": BRAND}
    ((kind, body),) = build.diff(old, plan_with(monthly_usd=900), resources, **kw)
    assert kind == "ad_set_update"
    newsbreak.update_ad_set(settings, ad_set["id"], body)
    assert nbapi.ad_sets[ad_set["id"]]["budget"] == 2960
    assert nbapi.ad_sets[ad_set["id"]]["targeting"] == stored["targeting"]
    newsbreak.update_ad_set(settings, ad_set["id"], {"budget": 2961})  # what a partial body does, for contrast
    assert nbapi.ad_sets[ad_set["id"]]["targeting"]["location"] == {"positive": ["all"]}
    # changed words: the new ads are created OFF and the old ones are switched off
    for kind, item in build.diff(old, plan_with(headlines=NEW_HEADLINES), resources, **kw):
        if kind == "ad_create":
            made = newsbreak.create_ad(settings, item)
            assert nbapi.ads[made["id"]]["status"] == "OFF"
        else:
            assert kind == "ad_off"
            newsbreak.ad_status(settings, item, False)
    assert [nbapi.ads[i]["status"] for i in resources["ads"].values()] == ["OFF", "OFF"]
    assert len(nbapi.ads) == 4


# ---------------------------------------------------------------- the golden


def _dental_plan(client, conn, fake_):
    """The dental walk's plan (the Google golden's) with a NewsBreak row added the way the allocator writes one: the
    dental walk's budget funds Google Search alone, and dentists are not a `newsbreak_fit` trade, so the row is put on
    by hand -- its words are Google's (`copy_of`), its amount $600."""
    plan, knowledge = _dental_plan_and_knowledge(client, conn, fake_)
    ads = plan["sections"]["ads"]
    google = ads["channels"][0]
    row = {**google, "channel": "newsbreak", "monthly_usd": 600.0, "role": "test", "mode": "managed"}
    row["copy_of"] = "google_search"
    sections = {**plan["sections"], "ads": {**ads, "channels": [google, row]}}
    return {**plan, "sections": sections}, knowledge


def test_the_bodies_match_the_golden_for_the_dental_walk(client, conn, fake):  # noqa: F811 (fixture)
    plan, knowledge = _dental_plan(client, conn, fake)
    headlines, descriptions, notes = build.ad_copy(plan)
    assert notes == [] and len(headlines) == 3 and len(descriptions) == 2
    name = build.campaign_name(plans.name_for(knowledge), plan["business_id"])
    bodies = {
        "campaign": build.campaign_body(name=name, ad_account_id="4001"),
        "ad_set": build.ad_set_body(
            campaign_id="4002",
            name=name,
            daily_cents=build.daily_cents(build.entry(plan)["monthly_usd"]),
            cpc_cents=build.cpc_cents(plan["sections"]["estimate"]),
            location_ids=LOCATION_IDS,
            language=build.LANGUAGE,
            start=START,
            end=END,
        ),
        "ads": build.ad_bodies(
            ad_set_id="4003",
            brand=knowledge["name"],
            headlines=headlines,
            descriptions=descriptions,
            cta=build.call_to_action(plan),
            asset_url=ASSET_URL,
            logo_url=None,
            click_url=build.page_url(PAGE_URL),
        ),
    }
    assert bodies["ad_set"]["budget"] == 1973 and bodies["ad_set"]["bidRate"] == 150
    assert [ad["creative"]["callToAction"] for ad in bodies["ads"]] == ["Call now"] * 2

    path = GOLDEN_DIR / "nb_ops_dentists.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(json.dumps(bodies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    golden = json.loads(path.read_text(encoding="utf-8"))
    assert bodies == golden
