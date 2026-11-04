"""ads_launch.py: a plan version to Google Ads v25 operations, the copy linter, the diff between two
versions, and resources_from (A3 Task 3). Pure functions -- no MockTransport needed anywhere here."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent import benchmark, business, local_rules, plans
from agent.channels.google_search import build as ads_launch
from agent.tests.conftest import JARGON, call_tool, tool_text
from agent.tests.fixtures import plan_draft_fixture
from agent.tests.test_ads_run import BIZ, KNOWLEDGE, sections
from agent.tests.test_ads_run import DESCRIPTIONS as RUN_DESCRIPTIONS
from agent.tests.test_ads_run import HEADLINES as RUN_HEADLINES
from agent.tests.test_tools_consult import _answer, _start, fake  # noqa: F401 (fixture)

GOLDEN_DIR = Path(__file__).parent / "golden"
CUSTOMER_ID = "1112223333"
ACCOUNT = ads_launch.Account(
    customer_id=CUSTOMER_ID,
    tag="AW-123456789",
    conversion_actions={"call_ad": f"customers/{CUSTOMER_ID}/conversionActions/1"},
)
TARGET = ads_launch.Target(kind="proximity", lat=37.4419, lng=-122.143, radius_miles=5.0, geo_targets=())
PAGE_URL = "https://brightsmile.example/invisalign"


# ---------------------------------------------------------------- money and naming


def test_daily_micros_floors_to_the_cent():
    assert ads_launch.daily_micros(2200) == 72_360_000


def test_cpc_ceiling_micros_from_a_cost_per_lead_range():
    estimate = {"assumptions": [{"name": "cost per lead", "value": "$51-109"}]}
    assert ads_launch.cpc_ceiling_micros(estimate) == 12_000_000


def test_cpc_ceiling_micros_reads_the_en_dash_google_ads_estimate_columns_actually_writes():
    # plans.estimate_columns formats this with an en dash, "$51–109" -- not a plain hyphen
    estimate = {"assumptions": [{"name": "cost per lead", "value": "$51–109"}]}
    assert ads_launch.cpc_ceiling_micros(estimate) == 12_000_000


def test_cpc_ceiling_micros_clamps_between_2_and_20_dollars():
    assert ads_launch.cpc_ceiling_micros({"assumptions": [{"name": "cost per lead", "value": "$5-9"}]}) == 2_000_000
    assert (
        ads_launch.cpc_ceiling_micros({"assumptions": [{"name": "cost per lead", "value": "$400-600"}]}) == 20_000_000
    )


def test_cpc_ceiling_micros_falls_back_to_a_bare_cpl_avg_or_a_low_high_pair():
    assert ads_launch.cpc_ceiling_micros({"cpl_avg": 80}) == 12_000_000
    assert ads_launch.cpc_ceiling_micros({"cpl_low": 51, "cpl_high": 109}) == 12_000_000


def test_campaign_name_carries_the_business_id_for_an_exact_lookup():
    assert ads_launch.campaign_name("Bright Smile Dental", "biz_1") == "Admiry · Bright Smile Dental · biz_1"
    assert ads_launch.campaign_name(None, "biz_1") == "Admiry · biz_1"  # no vouched name: still unique


def test_enable_turns_the_paused_campaign_on():
    assert ads_launch.enable("customers/1/campaigns/8") == [
        {
            "campaignOperation": {
                "update": {"resourceName": "customers/1/campaigns/8", "status": "ENABLED"},
                "updateMask": "status",
            }
        }
    ]


# ---------------------------------------------------------------- the copy linter (spec §7), one rule per case

BANNED = ["guaranteed", "guarantee", "best", "#1"]


def test_lint_copy_keeps_clean_lines_untouched():
    h, d, notes = ads_launch.lint_copy(["Invisalign in Palo Alto"], ["Clear aligners from a family dentist."], BANNED)
    assert h == ["Invisalign in Palo Alto"] and d == ["Clear aligners from a family dentist."] and notes == []


def test_lint_copy_drops_a_headline_over_30_characters():
    long_h = "A" * 31
    h, _d, notes = ads_launch.lint_copy([long_h], [], BANNED)
    assert h == [] and notes == [f'Dropped a headline ("{long_h}"): it is over 30 characters.']


def test_lint_copy_drops_a_description_over_90_characters():
    long_d = "B" * 91
    _h, d, notes = ads_launch.lint_copy([], [long_d], BANNED)
    assert d == [] and "is over 90 characters" in notes[0]


def test_lint_copy_drops_a_banned_word_as_a_whole_word_only():
    h, _d, notes = ads_launch.lint_copy(["The #1 Choice in Town"], [], BANNED)
    assert h == [] and 'uses a word we avoid ("#1")' in notes[0]
    # (?<!\w)...(?!\w), not \b...\b (\b cannot sit before "#"; landing.py's own check hit this first)
    h2, _d2, notes2 = ads_launch.lint_copy(["Bestowed With Care Today"], [], BANNED)
    assert h2 == ["Bestowed With Care Today"] and notes2 == []  # "bestowed" is not "best"


def test_lint_copy_drops_any_exclamation_mark_in_a_headline():
    h, _d, notes = ads_launch.lint_copy(["Book Today!"], [], BANNED)
    assert h == [] and "has an exclamation mark" in notes[0]


def test_lint_copy_allows_one_exclamation_mark_in_a_description_but_not_two():
    _h, d, notes = ads_launch.lint_copy([], ["Book today! Call now."], BANNED)
    assert d == ["Book today! Call now."] and notes == []
    _h2, d2, notes2 = ads_launch.lint_copy([], ["Book today! Call now!"], BANNED)
    assert d2 == [] and "more than one exclamation mark" in notes2[0]


def test_lint_copy_drops_a_phone_number_or_an_email_address():
    h, _d, notes = ads_launch.lint_copy(["Call (650) 555-0100 Now"], [], BANNED)
    assert h == [] and "phone number or e-mail address" in notes[0]
    h2, _d2, notes2 = ads_launch.lint_copy(["Email owner@example.com"], [], BANNED)
    assert h2 == [] and "phone number or e-mail address" in notes2[0]


def test_lint_copy_drops_a_bare_local_number_too():
    """Review round 1: a 3-4 digit local number with no area code ("555-1234") matched neither of
    _PHONE_RE's two original alternatives (the xxx-xxx-xxxx shape needs 10 digits; the bare-digits shape
    needs 7+ *consecutive* digits, and the hyphen breaks that) -- it slipped through uncaught."""
    h, _d, notes = ads_launch.lint_copy(["Call 555-1234 Today"], [], BANNED)
    assert h == [] and "phone number or e-mail address" in notes[0]


def test_lint_copy_drops_shouted_words_of_5_or_more_letters_but_allows_shorter_ones():
    """P11: an ALL-CAPS word of 5+ letters is shouting; up to 4 passes as a plausible acronym, with no
    allowlist to maintain (Review round 1 S7 -- SEO/HVAC/DDS/LLC all clear this threshold on their own)."""
    h, _d, notes = ads_launch.lint_copy(["SEO Experts Here"], [], [])  # empty banned: isolates the caps rule
    assert h == ["SEO Experts Here"] and notes == []
    h2, _d2, notes2 = ads_launch.lint_copy(["AMAZING Smiles"], [], [])
    assert h2 == [] and "is written in all capitals" in notes2[0]
    h3, _d3, notes3 = ads_launch.lint_copy(["Ask About Our HVAC FAQ"], [], [])
    assert h3 == ["Ask About Our HVAC FAQ"] and notes3 == []


def test_lint_copy_notes_never_use_advertising_jargon():
    """D52: the preview's `dropped` notes are read by owners and their assistants."""
    _h, _d, notes = ads_launch.lint_copy(
        ["A" * 31, "Best Deal Ever!", "Call (650) 555-0100", "SHOUTING HEADLINE HERE"],
        ["C" * 91, "Two! Bangs! Here!"],
        BANNED,
    )
    assert len(notes) == 6 and all(not JARGON.search(n) for n in notes)


def test_lint_copy_defaults_are_googles_numbers_and_a_channel_may_pass_its_own():
    """M1 Task 3: NewsBreak lints with its own lengths through the same function. Left out or None, the limits are
    Google's (30 and 90); a number given replaces only its own limit and the note says it."""
    h30, h31, h91 = "a" * 30, "a" * 31, "a" * 91  # lower case: the shouting rule is not what these probe
    d90, d91 = "b" * 90, "b" * 91
    assert ads_launch.lint_copy([h30], [d90], [])[:2] == ([h30], [d90])
    assert ads_launch.lint_copy([h31], [d91], [])[:2] == ([], [])
    assert ads_launch.lint_copy([h31], [d91], [], max_headline=None, max_description=None)[:2] == ([], [])
    h, d, notes = ads_launch.lint_copy([h31], [d91], [], max_headline=90)
    assert h == [h31] and d == [] and notes == [f'Dropped a description ("{d91}"): it is over 90 characters.']
    h, d, notes = ads_launch.lint_copy([h31], [d91], [], max_description=100)
    assert h == [] and d == [d91] and notes == [f'Dropped a headline ("{h31}"): it is over 30 characters.']
    assert ads_launch.lint_copy([h91], [], [], max_headline=90)[2] == [
        f'Dropped a headline ("{h91}"): it is over 90 characters.'
    ]
    assert ads_launch.lint_copy(["a"], [], [], max_headline=0)[0] == []  # 0 is a limit like any other, not "left out"


# ---------------------------------------------------------------- operations(): the golden dental walk


LAUNCHABLE_HEADLINES = ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners Near You"]
LAUNCHABLE_DESCRIPTIONS = [
    "Clear aligners from a family dentist. Book online.",
    "Straighter teeth without metal braces.",
]


def _launchable_draft():
    """The dental walk's model draft with enough ad copy to launch (A3 final review: at least 3 headlines and 2
    descriptions left after the linter, or precheck -- and Google, for a responsive search ad -- refuse it)."""
    draft = plan_draft_fixture()
    google = draft.ads[0].model_copy(
        update={"headlines": LAUNCHABLE_HEADLINES, "descriptions": LAUNCHABLE_DESCRIPTIONS}
    )
    return draft.model_copy(update={"ads": [google, *draft.ads[1:]]})


def _dental_plan_and_knowledge(client, conn, fake):  # noqa: F811 (fixture)
    """The dental walk (test_tools_consult.test_the_dental_walk_through_ends_in_a_plan_with_cards), returning
    the plan and the knowledge dict ads_launch.operations needs -- the plan-building helper the brief points
    at, read back exactly as Task 4 will read them (plans.get, business.latest_knowledge). The model's draft
    carries launchable ad copy (`_launchable_draft`), so the golden is a mutate Google could take."""
    fake.draft = _launchable_draft()
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, {"listing": {"choice": "gm_1"}}, "g0")  # the business on Google Maps: centres the ads
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "g1")
    _answer(client, key, sid, {"service": {"choice": "svc_invisalign"}}, "g2")
    _answer(client, key, sid, {"customer_value": {"other": "around three thousand"}}, "g3")
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "gr"}, key=key)
    assert not res["isError"], tool_text(res)
    biz = b["data"]["business_id"]
    plan = plans.get(conn, biz, res["structuredContent"]["data"]["plan_id"])
    knowledge = business.latest_knowledge(conn, biz)[1]
    return plan, knowledge


def _temp_creates(ops: list[dict]) -> list[str]:
    """Every explicit temporary resourceName any `create` operation carries, in call order."""
    names = []
    for op in ops:
        body = (op[next(iter(op))] or {}).get("create")
        if isinstance(body, dict) and isinstance(body.get("resourceName"), str):
            names.append(body["resourceName"])
    return names


def _assert_caps_and_temp_id_hygiene(ops: list[dict]) -> None:
    ad_groups = [
        op["adGroupOperation"]["create"]
        for op in ops
        if "adGroupOperation" in op and "create" in op["adGroupOperation"]
    ]
    assert len(ad_groups) <= ads_launch.MAX_AD_GROUPS
    for ag in ad_groups:
        kws = [
            op["adGroupCriterionOperation"]["create"]
            for op in ops
            if "adGroupCriterionOperation" in op
            and op["adGroupCriterionOperation"]["create"].get("adGroup") == ag["resourceName"]
        ]
        assert len(kws) <= ads_launch.MAX_KEYWORDS_PER_AD_GROUP
    names = _temp_creates(ops)
    ids = [int(n.rsplit("/", 1)[1]) for n in names]
    assert len(ids) == len(set(ids)) and all(i < 0 for i in ids)  # unique and negative, spec D50/task brief


def test_operations_match_the_golden_for_the_dental_walk(client, conn, fake):  # noqa: F811 (fixture)
    plan, knowledge = _dental_plan_and_knowledge(client, conn, fake)
    ops = ads_launch.operations(
        plan, knowledge, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    _assert_caps_and_temp_id_hygiene(ops)
    rsa = next(op["adGroupAdOperation"]["create"] for op in ops if "adGroupAdOperation" in op)["ad"]
    assert len(rsa["responsiveSearchAd"]["headlines"]) >= 3 and len(rsa["responsiveSearchAd"]["descriptions"]) >= 2

    path = GOLDEN_DIR / "ads_ops_dentists.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(json.dumps(ops, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    golden = json.loads(path.read_text(encoding="utf-8"))
    assert ops == golden


def test_operations_pins_the_first_headline_when_it_names_the_service(client, conn, fake):  # noqa: F811 (fixture)
    plan, knowledge = _dental_plan_and_knowledge(client, conn, fake)
    ops = ads_launch.operations(
        plan, knowledge, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    ad = next(op["adGroupAdOperation"]["create"] for op in ops if "adGroupAdOperation" in op)
    headlines = ad["ad"]["responsiveSearchAd"]["headlines"]
    assert headlines[0] == {"text": "Invisalign in Palo Alto", "pinnedField": "HEADLINE_1"}
    assert all("pinnedField" not in h for h in headlines[1:])


def test_operations_is_pure_and_deterministic_across_calls(client, conn, fake):  # noqa: F811 (fixture)
    """Spec §3: ads_launch.py is pure -- no database, no HTTP. Calling it twice on the same inputs must be
    perfectly deterministic (Task 4's idempotent retries depend on this)."""
    plan, knowledge = _dental_plan_and_knowledge(client, conn, fake)
    a = ads_launch.operations(
        plan, knowledge, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    b = ads_launch.operations(
        plan, knowledge, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert a == b


# ---------------------------------------------------------------- operations(): the ≤5/≤20 caps (synthetic)


def _synthetic_plan_and_knowledge() -> tuple[dict, dict]:
    """A plan with more themes and candidate keywords than the caps allow -- the dental walk's own plan has
    only one of each, so the caps need a richer fixture to mean anything."""
    themes = [f"repair theme {i}" for i in range(8)]
    plan = {
        "business_id": "biz_9",
        "sections": {
            "consultation": {"business": {"category": "fallback", "service": "Repairs", "services": ["Repairs"]}},
            "ads": {
                "channels": [
                    {
                        "channel": "google_search",
                        "monthly_usd": 1000.0,
                        "themes": themes,
                        "negatives": ["diy"],
                        "headlines": ["Fast Repairs Near You", "Same-Day Service Available", "Call For A Free Quote"],
                        "descriptions": ["We fix it fast and fair. Call today.", "Licensed, insured, always on time."],
                        "landing_service": "Repairs",
                    }
                ]
            },
            "rationale": {"example_searches": [f"search phrase {i}" for i in range(30)]},
            "estimate": {"assumptions": [{"name": "cost per lead", "value": "$51-109"}]},
        },
    }
    knowledge = {
        "phone": "650-555-0100",
        "name": "Fixit Co",
        "website": "https://fixit.example",
        "services": [{"name": "Repairs", "intent_phrases": [f"intent phrase {i}" for i in range(30)]}],
    }
    return plan, knowledge


def test_operations_caps_ad_groups_at_5_and_keywords_at_20():
    plan, knowledge = _synthetic_plan_and_knowledge()
    ops = ads_launch.operations(
        plan,
        knowledge,
        page_url="https://fixit.example/repairs",
        target=TARGET,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
    )
    _assert_caps_and_temp_id_hygiene(ops)
    ad_groups = [
        op["adGroupOperation"]["create"]
        for op in ops
        if "adGroupOperation" in op and "create" in op["adGroupOperation"]
    ]
    assert len(ad_groups) == ads_launch.MAX_AD_GROUPS  # 8 themes offered, 5 kept
    first = ad_groups[0]
    kws = [
        op["adGroupCriterionOperation"]["create"]["keyword"]["text"]
        for op in ops
        if "adGroupCriterionOperation" in op
        and op["adGroupCriterionOperation"]["create"]["adGroup"] == first["resourceName"]
    ]
    assert len(kws) == ads_launch.MAX_KEYWORDS_PER_AD_GROUP  # far more than 20 candidates offered, 20 kept
    assert kws[0] == "repair theme 0"  # the theme itself always makes the cut


def test_operations_call_asset_uses_the_resource_level_conversion_action_when_present():
    plan, knowledge = _synthetic_plan_and_knowledge()
    ops = ads_launch.operations(
        plan,
        knowledge,
        page_url="https://fixit.example/repairs",
        target=TARGET,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
    )
    asset = next(op["assetOperation"]["create"] for op in ops if "assetOperation" in op)
    assert asset["callAsset"] == {
        "countryCode": "US",
        "phoneNumber": "6505550100",
        "callConversionReportingState": "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
        "callConversionAction": ACCOUNT.conversion_actions["call_ad"],
    }
    campaign_asset = next(op["campaignAssetOperation"]["create"] for op in ops if "campaignAssetOperation" in op)
    assert campaign_asset["fieldType"] == "CALL" and campaign_asset["asset"] == asset["resourceName"]


def test_operations_call_asset_falls_back_to_account_level_reporting():
    plan, knowledge = _synthetic_plan_and_knowledge()
    no_action_account = ads_launch.Account(customer_id=CUSTOMER_ID, tag=None, conversion_actions={})
    ops = ads_launch.operations(
        plan,
        knowledge,
        page_url="https://fixit.example/repairs",
        target=TARGET,
        account=no_action_account,
        customer_id=CUSTOMER_ID,
    )
    asset = next(op["assetOperation"]["create"] for op in ops if "assetOperation" in op)
    assert asset["callAsset"]["callConversionReportingState"] == "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION"
    assert "callConversionAction" not in asset["callAsset"]


def test_operations_uses_geo_target_constants_when_the_target_is_service_area_locations():
    plan, knowledge = _synthetic_plan_and_knowledge()
    locations = ads_launch.Target(
        kind="locations", lat=None, lng=None, radius_miles=None, geo_targets=("geoTargetConstants/1023191",)
    )
    ops = ads_launch.operations(
        plan,
        knowledge,
        page_url="https://fixit.example/repairs",
        target=locations,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
    )
    criteria = [op["campaignCriterionOperation"]["create"] for op in ops if "campaignCriterionOperation" in op]
    assert {
        "campaign": criteria[0]["campaign"],
        "location": {"geoTargetConstant": "geoTargetConstants/1023191"},
    } in criteria
    assert not any("proximity" in c for c in criteria)


def test_operations_negatives_combine_the_plans_own_and_the_recipes_without_duplicates():
    plan, knowledge = _synthetic_plan_and_knowledge()
    ops = ads_launch.operations(
        plan,
        knowledge,
        page_url="https://fixit.example/repairs",
        target=TARGET,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
    )
    negatives = [
        c["keyword"]["text"]
        for op in ops
        if "campaignCriterionOperation" in op
        for c in [op["campaignCriterionOperation"]["create"]]
        if c.get("negative") is True
    ]
    # the plan's own "diy" and the fallback recipe's list, "diy" kept once (plan's own copy, first seen)
    assert negatives == ["diy", "jobs", "salary", "free"]
    assert all(
        op["campaignCriterionOperation"]["create"]["keyword"]["matchType"] == "PHRASE"
        for op in ops
        if "campaignCriterionOperation" in op and op["campaignCriterionOperation"]["create"].get("negative")
    )


# ---------------------------------------------------------------- keywords and negatives Google takes


def test_a_phrase_loses_the_characters_google_refuses_and_is_lowercased():
    """A3 final review: Google refuses a keyword with `! @ % , *` (and the match-type marks) in it, or over 80
    characters or 10 words -- and one bad phrase must never fail the whole launch."""
    assert ads_launch._phrase("invisalign cost, palo alto!") == "invisalign cost palo alto"
    assert ads_launch._phrase('"Clear aligners" [near me] (Palo Alto)?') == "clear aligners near me palo alto"
    assert ads_launch._phrase("teeth @ 50% off*; ~ever^ {now} <here> | back\\slash") == (
        "teeth 50 off ever now here back slash"
    )
    assert ads_launch._phrase("one two three four five six seven eight nine ten eleven") is None  # 11 words
    assert ads_launch._phrase("one two three four five six seven eight nine ten") is not None
    assert ads_launch._phrase("x" * 81) is None and ads_launch._phrase("x" * 80) == "x" * 80
    assert ads_launch._phrase("!!! ,,,") is None and ads_launch._phrase(None) is None
    assert ads_launch._phrase("dentist's open-late + walk-ins & more.") == "dentist's open-late + walk-ins & more."


def test_operations_send_only_phrases_google_takes_deduplicated_after_cleaning():
    plan, knowledge = _synthetic_plan_and_knowledge()
    ch = plan["sections"]["ads"]["channels"][0]
    ch["themes"] = ["Repairs, Near Me!"]
    ch["negatives"] = ["DIY!", "free*", "diy"]
    plan["sections"]["rationale"]["example_searches"] = [
        "repairs near me",  # the theme again, once cleaned
        "one two three four five six seven eight nine ten eleven",
        "fix it today?",
    ]
    knowledge = {**knowledge, "services": []}
    ops = ads_launch.operations(
        plan, knowledge, page_url="https://fixit.example/r", target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    keywords = [
        op["adGroupCriterionOperation"]["create"]["keyword"]["text"] for op in ops if "adGroupCriterionOperation" in op
    ]
    assert keywords == ["repairs near me", "fix it today"]
    negatives = [
        c["keyword"]["text"]
        for op in ops
        if "campaignCriterionOperation" in op
        for c in [op["campaignCriterionOperation"]["create"]]
        if c.get("negative")
    ]
    assert negatives == ["diy", "free", "jobs", "salary"]  # the recipe's "free" and "diy" once each
    group = next(op["adGroupOperation"]["create"] for op in ops if "adGroupOperation" in op)
    assert group["name"] == "Repairs, Near Me!"  # an ad group's name is the theme as the plan wrote it


# ---------------------------------------------------------------- resources_from


def test_resources_from_maps_each_kind_and_ad_groups_ads_keywords_by_theme():
    ops = [
        {"campaignBudgetOperation": {"create": {"resourceName": "customers/1/campaignBudgets/-1", "name": "x"}}},
        {"campaignOperation": {"create": {"resourceName": "customers/1/campaigns/-2", "name": "x"}}},
        {
            "campaignCriterionOperation": {
                "create": {"campaign": "customers/1/campaigns/-2", "proximity": {"radius": 5.0}}
            }
        },
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": "customers/1/campaigns/-2",
                    "language": {"languageConstant": "languageConstants/1000"},
                }
            }
        },
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": "customers/1/campaigns/-2",
                    "negative": True,
                    "keyword": {"text": "jobs", "matchType": "PHRASE"},
                }
            }
        },
        {
            "adGroupOperation": {
                "create": {
                    "resourceName": "customers/1/adGroups/-3",
                    "name": "invisalign palo alto",
                    "campaign": "customers/1/campaigns/-2",
                    "status": "ENABLED",
                    "type": "SEARCH_STANDARD",
                }
            }
        },
        {
            "adGroupCriterionOperation": {
                "create": {
                    "adGroup": "customers/1/adGroups/-3",
                    "status": "ENABLED",
                    "keyword": {"text": "invisalign palo alto", "matchType": "PHRASE"},
                }
            }
        },
        {"adGroupAdOperation": {"create": {"adGroup": "customers/1/adGroups/-3", "ad": {"finalUrls": ["https://x"]}}}},
        {"assetOperation": {"create": {"resourceName": "customers/1/assets/-4", "callAsset": {"countryCode": "US"}}}},
        {
            "campaignAssetOperation": {
                "create": {
                    "campaign": "customers/1/campaigns/-2",
                    "asset": "customers/1/assets/-4",
                    "fieldType": "CALL",
                }
            }
        },
    ]
    responses = [
        {"campaignBudgetResult": {"resourceName": "customers/1/campaignBudgets/9"}},
        {"campaignResult": {"resourceName": "customers/1/campaigns/8"}},
        {"campaignCriterionResult": {"resourceName": "customers/1/campaignCriteria/7"}},  # proximity
        {"campaignCriterionResult": {"resourceName": "customers/1/campaignCriteria/2"}},  # language: not kept
        {"campaignCriterionResult": {"resourceName": "customers/1/campaignCriteria/1"}},  # negative "jobs"
        {"adGroupResult": {"resourceName": "customers/1/adGroups/6"}},
        {"adGroupCriterionResult": {"resourceName": "customers/1/adGroupCriteria/5"}},
        {"adGroupAdResult": {"resourceName": "customers/1/adGroupAds/6~4"}},
        {"assetResult": {"resourceName": "customers/1/assets/3"}},
        {"campaignAssetResult": {"resourceName": "customers/1/campaignAssets/8~3"}},
    ]
    assert ads_launch.resources_from(responses, ops) == {
        "campaign_budget": "customers/1/campaignBudgets/9",
        "campaign": "customers/1/campaigns/8",
        "geo_criteria": ["customers/1/campaignCriteria/7"],
        "negatives": {"jobs": "customers/1/campaignCriteria/1"},
        "ad_groups": {"invisalign palo alto": "customers/1/adGroups/6"},
        "keywords": {"invisalign palo alto": {"invisalign palo alto": "customers/1/adGroupCriteria/5"}},
        "ads": {"invisalign palo alto": "customers/1/adGroupAds/6~4"},
        "final_url": "https://x",  # H9: read off the same op's ad.finalUrls, not a separate parameter
        "call_asset": "customers/1/assets/3",
        "campaign_asset": "customers/1/campaignAssets/8~3",
    }


def test_resources_from_ignores_operations_with_no_matching_response():
    empty = {"ad_groups": {}, "ads": {}, "keywords": {}, "negatives": {}, "geo_criteria": []}
    assert ads_launch.resources_from([], []) == empty
    ops = [{"campaignBudgetOperation": {"create": {"resourceName": "customers/1/campaignBudgets/-1"}}}]
    assert ads_launch.resources_from([{}], ops) == empty


# ---------------------------------------------------------------- diff(): P10


def _diff_plan(
    *,
    monthly_usd: float,
    themes: list[str],
    headlines: list[str],
    descriptions: list[str],
    negatives: list[str] | None = None,
    radius_miles: float = 5.0,
    center: str = "ChIJ1",
    location: str = "Austin, TX",
) -> dict:
    return {
        "sections": {
            "consultation": {
                "business": {
                    "category": "dentists",
                    "service": "Invisalign",
                    "services": ["Invisalign"],
                    "location": location,
                },
                "geo": {"radius_miles": radius_miles, "center": center},
            },
            "ads": {
                "channels": [
                    {
                        "channel": "google_search",
                        "monthly_usd": monthly_usd,
                        "themes": themes,
                        "negatives": negatives if negatives is not None else ["jobs"],
                        "headlines": headlines,
                        "descriptions": descriptions,
                        "landing_service": "Invisalign",
                    }
                ]
            },
            "rationale": {"example_searches": ["invisalign palo alto"]},
            "estimate": {"assumptions": [{"name": "cost per lead", "value": "$51-109"}]},
        }
    }


# A baseline `resources` as a prior `operations()` + `resources_from()` call would have left it: the
# "invisalign palo alto" ad group already has its one keyword and "jobs" is already a negative, so a diff
# test that changes nothing else sees no spurious keyword/negative ops from that baseline alone.
RESOURCES = {
    "campaign_budget": "customers/1/campaignBudgets/9",
    "campaign": "customers/1/campaigns/8",
    "ad_groups": {"invisalign palo alto": "customers/1/adGroups/6", "cleanings palo alto": "customers/1/adGroups/7"},
    "ads": {"invisalign palo alto": "customers/1/adGroupAds/6~4", "cleanings palo alto": "customers/1/adGroupAds/7~5"},
    "keywords": {
        "invisalign palo alto": {"invisalign palo alto": "customers/1/adGroupCriteria/1"},
        "cleanings palo alto": {"cleanings palo alto": "customers/1/adGroupCriteria/2"},
    },
    "negatives": {"jobs": "customers/1/campaignCriteria/1"},
    "geo_criteria": ["customers/1/campaignCriteria/9"],
    "call_asset": "customers/1/assets/3",
    "final_url": PAGE_URL,  # H9: every `_diff` call below sends the same PAGE_URL, so this reads as unchanged
}
HEADLINES = ["Invisalign in Palo Alto", "Same-Week Appointments"]
DESCRIPTIONS = ["Clear aligners from a family dentist. Book online."]


def _diff(old: dict, new: dict) -> list[dict]:
    return ads_launch.diff(
        old, new, RESOURCES, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )


def test_diff_a_budget_change_updates_the_budget_and_the_ceiling_together():
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = _diff_plan(monthly_usd=2400, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    assert _diff(old, new) == [
        {
            "campaignBudgetOperation": {
                "update": {"resourceName": RESOURCES["campaign_budget"], "amountMicros": ads_launch.daily_micros(2400)},
                "updateMask": "amountMicros",
            }
        },
        {
            "campaignOperation": {
                "update": {"resourceName": RESOURCES["campaign"], "targetSpend": {"cpcBidCeilingMicros": 12_000_000}},
                "updateMask": "targetSpend.cpcBidCeilingMicros",
            }
        },
    ]


def test_money_is_the_budget_and_the_ceiling_and_diff_leaves_it_out_on_request():
    """S16: `ads_run` sends the money on its own mutate first, then asks `diff` for the rest without it."""
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = _diff_plan(monthly_usd=2400, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    assert ads_launch.money(new, RESOURCES) == _diff(old, new)  # the pair, whatever moved
    assert ads_launch.money(old, RESOURCES)[0]["campaignBudgetOperation"]["update"]["amountMicros"] == (
        ads_launch.daily_micros(2000)
    )
    no_money = ads_launch.diff(
        old,
        new,
        RESOURCES,
        page_url=PAGE_URL,
        target=TARGET,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
        with_money=False,
    )
    assert no_money == []


def test_diff_a_theme_that_comes_back_turns_its_paused_group_back_on():
    """S16 (I3): "cleanings palo alto" left an earlier version (its group paused, still in `resources`) and is
    approved again -- Google refuses a second group of that name, so the paused one is turned back on, its
    keywords topped up like a kept theme's, and its ad replaced with the approved text (the paused group's ad
    carries whichever version's text last ran it)."""
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto", "cleanings palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
    )
    group = RESOURCES["ad_groups"]["cleanings palo alto"]
    ops = _diff(old, new)
    assert not any("create" in op.get("adGroupOperation", {}) for op in ops)
    assert ops[0] == {
        "adGroupOperation": {"update": {"resourceName": group, "status": "ENABLED"}, "updateMask": "status"}
    }
    assert ops[1]["adGroupCriterionOperation"]["create"]["adGroup"] == group
    assert ops[1]["adGroupCriterionOperation"]["create"]["keyword"]["text"] == "invisalign palo alto"
    assert ops[2]["adGroupAdOperation"]["create"]["adGroup"] == group
    assert ops[3] == {
        "adGroupAdOperation": {
            "update": {"resourceName": RESOURCES["ads"]["cleanings palo alto"], "status": "PAUSED"},
            "updateMask": "status",
        }
    }
    assert len(ops) == 4


def test_diff_no_change_is_an_empty_mutate():
    same = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    assert _diff(same, same) == []


def test_diff_a_removed_theme_pauses_its_ad_group_only():
    old = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto", "cleanings palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
    )
    new = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    assert _diff(old, new) == [
        {
            "adGroupOperation": {
                "update": {"resourceName": RESOURCES["ad_groups"]["cleanings palo alto"], "status": "PAUSED"},
                "updateMask": "status",
            }
        }
    ]


def test_diff_a_new_theme_creates_an_ad_group_its_keywords_and_an_ad():
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto", "implants palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
    )
    ops = _diff(old, new)
    kinds = [next(iter(o)) for o in ops]
    assert kinds == ["adGroupOperation", "adGroupCriterionOperation", "adGroupCriterionOperation", "adGroupAdOperation"]
    ag = ops[0]["adGroupOperation"]["create"]
    assert ag["name"] == "implants palo alto" and ag["campaign"] == RESOURCES["campaign"] and ag["status"] == "ENABLED"
    keywords = {
        ops[1]["adGroupCriterionOperation"]["create"]["keyword"]["text"],
        ops[2]["adGroupCriterionOperation"]["create"]["keyword"]["text"],
    }
    assert keywords == {"implants palo alto", "invisalign palo alto"}  # the new theme + rationale.example_searches
    ad = ops[3]["adGroupAdOperation"]["create"]
    assert ad["adGroup"] == ag["resourceName"] and ad["ad"]["finalUrls"] == [PAGE_URL]


def test_diff_changed_copy_creates_a_new_ad_and_pauses_the_old_one():
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new_headlines = ["Invisalign in Palo Alto", "Free Consultation Today"]
    new = _diff_plan(
        monthly_usd=2000, themes=["invisalign palo alto"], headlines=new_headlines, descriptions=DESCRIPTIONS
    )
    ops = _diff(old, new)
    assert ops == [
        {
            "adGroupAdOperation": {
                "create": {
                    "adGroup": RESOURCES["ad_groups"]["invisalign palo alto"],
                    "status": "ENABLED",
                    "ad": {
                        "finalUrls": [PAGE_URL],
                        "responsiveSearchAd": {
                            "headlines": [
                                {"text": "Invisalign in Palo Alto", "pinnedField": "HEADLINE_1"},
                                {"text": "Free Consultation Today"},
                            ],
                            "descriptions": [{"text": d} for d in DESCRIPTIONS],
                        },
                    },
                }
            }
        },
        {
            "adGroupAdOperation": {
                "update": {"resourceName": RESOURCES["ads"]["invisalign palo alto"], "status": "PAUSED"},
                "updateMask": "status",
            }
        },
    ]


def test_diff_treats_the_final_url_as_part_of_the_ad():
    """A3p-2 H9: a page moving off its old address (a hosted page published, or a plan otherwise unchanged)
    refreshes the ad exactly like changed copy would -- a new ad carrying the new finalUrls, the old one
    paused -- even though neither headlines nor descriptions moved."""
    same = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new_url = "https://bright-smile-dental.business.test/"

    ops = ads_launch.diff(
        same, same, RESOURCES, page_url=new_url, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )

    assert ops == [
        {
            "adGroupAdOperation": {
                "create": {
                    "adGroup": RESOURCES["ad_groups"]["invisalign palo alto"],
                    "status": "ENABLED",
                    "ad": {
                        "finalUrls": [new_url],
                        "responsiveSearchAd": {
                            "headlines": [
                                {"text": "Invisalign in Palo Alto", "pinnedField": "HEADLINE_1"},
                                {"text": "Same-Week Appointments"},
                            ],
                            "descriptions": [{"text": d} for d in DESCRIPTIONS],
                        },
                    },
                }
            }
        },
        {
            "adGroupAdOperation": {
                "update": {"resourceName": RESOURCES["ads"]["invisalign palo alto"], "status": "PAUSED"},
                "updateMask": "status",
            }
        },
    ]
    # the same URL as what `resources["final_url"]` already has: no ad churn from this alone
    assert _diff(same, same) == []
    # an unknown final_url (a campaign recovered by name, never diffed since) reads as changed too (conservative)
    unknown = {k: v for k, v in RESOURCES.items() if k != "final_url"}
    recovered = ads_launch.diff(
        same, same, unknown, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert recovered[0]["adGroupAdOperation"]["create"]["ad"]["finalUrls"] == [PAGE_URL]


def test_diff_replaces_the_geo_criteria_when_the_area_moves():
    """P10 "the proximity/location criteria (replace)": every old geo criterion removed, the new target's
    recreated -- since consultation.geo (the radius here) moved between versions."""
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        radius_miles=10.0,
    )
    assert _diff(old, new) == [
        {"campaignCriterionOperation": {"remove": "customers/1/campaignCriteria/9"}},
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": RESOURCES["campaign"],
                    "proximity": {
                        "geoPoint": {"latitudeInMicroDegrees": 37441900, "longitudeInMicroDegrees": -122143000},
                        "radius": 5.0,  # TARGET's own radius (already re-resolved by the caller for the new plan)
                        "radiusUnits": "MILES",
                    },
                }
            }
        },
    ]


def test_diff_leaves_the_geo_criteria_alone_when_the_area_is_unchanged():
    same = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    assert _diff(same, same) == []


LOCATIONS_TARGET = ads_launch.Target(
    kind="locations", lat=None, lng=None, radius_miles=None, geo_targets=("geoTargetConstants/1023191",)
)


def test_diff_replaces_the_location_criteria_when_the_service_area_grows():
    """Review round 1, Important: a business with no listing (a `locations` target, built from the
    service-area names, not a proximity circle) whose service area grew -- Austin -> Austin + Round Rock --
    was missed entirely before this fix: `_geo_key` compared only `consultation.geo`'s radius/center, which
    never changes for this kind of business, so `target.geo_targets` growing from 1 to 2 never produced any
    `campaignCriterionOperation` and the campaign kept serving only the old area forever. `_geo_key` now folds
    in `consultation.business.location` (the free-text service-area address a `locations` target is resolved
    from) too."""
    old = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        location="Austin, TX",
    )
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        location="Austin, TX; Round Rock, TX",
    )
    grown = ads_launch.Target(
        kind="locations",
        lat=None,
        lng=None,
        radius_miles=None,
        geo_targets=("geoTargetConstants/1023191", "geoTargetConstants/1023192"),
    )
    ops = ads_launch.diff(
        old, new, RESOURCES, page_url=PAGE_URL, target=grown, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert ops == [
        {"campaignCriterionOperation": {"remove": "customers/1/campaignCriteria/9"}},
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": RESOURCES["campaign"],
                    "location": {"geoTargetConstant": "geoTargetConstants/1023191"},
                }
            }
        },
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": RESOURCES["campaign"],
                    "location": {"geoTargetConstant": "geoTargetConstants/1023192"},
                }
            }
        },
    ]


def test_diff_leaves_the_location_criteria_alone_when_the_service_area_is_unchanged():
    same = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        location="Austin, TX",
    )
    ops = ads_launch.diff(
        same, same, RESOURCES, page_url=PAGE_URL, target=LOCATIONS_TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert ops == []


def test_diff_adds_and_removes_negative_keywords_by_text():
    # "coupon"/"discount code" are the plan's own words, not in the dentists recipe's fixed list (jobs/
    # school/salary/free/diy), so changing them actually changes the union `operations` would negate.
    old = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        negatives=["coupon"],
    )
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        negatives=["discount code"],
    )
    resources = {**RESOURCES, "negatives": {**RESOURCES["negatives"], "coupon": "customers/1/campaignCriteria/11"}}
    ops = ads_launch.diff(
        old, new, resources, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert ops == [
        {"campaignCriterionOperation": {"remove": "customers/1/campaignCriteria/11"}},
        {
            "campaignCriterionOperation": {
                "create": {
                    "campaign": RESOURCES["campaign"],
                    "negative": True,
                    "keyword": {"text": "discount code", "matchType": "PHRASE"},
                }
            }
        },
    ]


def test_diff_negatives_are_compared_case_insensitively():
    """Review round 1: the within-call dedup (_negatives -> _dedupe_keep_order) already folds "DIY" and
    "diy" together as one negative; the old-vs-new comparison across versions must too, or a mere casing
    change between approved versions would spuriously remove-then-recreate the same negative."""
    old = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        negatives=["DIY"],
    )
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
        negatives=["diy"],
    )
    assert _diff(old, new) == []


def test_diff_adds_a_new_keyword_to_a_kept_theme_but_never_removes_one():
    """P10 "keywords (add/remove by text)", narrowed to add-only for a kept theme (see diff's docstring): a
    newly-named rationale.example_searches phrase is added; a keyword resources_from recorded from the
    original launch (e.g. one operations() built from knowledge.services' intent_phrases) is never removed
    just because this call's own, knowledge-less candidate list does not happen to name it."""
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = dict(old)
    new["sections"] = {
        **old["sections"],
        "rationale": {"example_searches": ["invisalign palo alto", "invisalign cost"]},
    }
    ops = _diff(old, new)
    assert ops == [
        {
            "adGroupCriterionOperation": {
                "create": {
                    "adGroup": RESOURCES["ad_groups"]["invisalign palo alto"],
                    "status": "ENABLED",
                    "keyword": {"text": "invisalign cost", "matchType": "PHRASE"},
                }
            }
        }
    ]


def test_diff_stops_adding_keywords_to_a_kept_theme_once_it_has_20():
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = dict(old)
    new["sections"] = {
        **old["sections"],
        "rationale": {"example_searches": [f"new phrase {i}" for i in range(30)]},
    }
    full_resources = {
        **RESOURCES,
        "keywords": {"invisalign palo alto": {f"existing {i}": f"customers/1/adGroupCriteria/{i}" for i in range(20)}},
    }
    ops = ads_launch.diff(
        old, new, full_resources, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert ops == []  # already at the 20-keyword cap: nothing more is added


# ---------------------------------------------------------------- spec 2026-10-11 §5 "How a drop is kept" (plan D3)


def _plan(**kw) -> dict:
    return {"id": "pl_1", "business_id": BIZ, "sections": sections(**kw)}


def _ad(ops: list[dict]) -> dict:
    return next(
        op["adGroupAdOperation"]["create"]["ad"]["responsiveSearchAd"]
        for op in ops
        if "create" in op.get("adGroupAdOperation", {})
    )


def _keywords_sent(ops: list[dict]) -> list[str]:
    return [
        op["adGroupCriterionOperation"]["create"]["keyword"]["text"] for op in ops if "adGroupCriterionOperation" in op
    ]


def test_kept_lines_are_the_linters_survivors_minus_what_admiry_dropped():
    dropped = ads_launch.Dropped(
        headlines=frozenset({"Same-Week Appointments"}), descriptions=frozenset({RUN_DESCRIPTIONS[1]})
    )
    h, d, notes = ads_launch.kept_lines([*RUN_HEADLINES, "Call Now!"], RUN_DESCRIPTIONS, BANNED, dropped)
    assert h == ["Invisalign in Palo Alto", "Clear Aligners Near You"] and d == [RUN_DESCRIPTIONS[0]]
    assert len(notes) == 1 and "Call Now!" in notes[0]  # the linter's own note; a drop leaves none (it is the record's)
    assert ads_launch.kept_lines(RUN_HEADLINES, RUN_DESCRIPTIONS, BANNED, ads_launch.NOTHING_DROPPED) == (
        RUN_HEADLINES,
        RUN_DESCRIPTIONS,
        [],
    )


def test_operations_leaves_the_dropped_lines_out_and_never_edits_the_plan():
    plan = _plan(headlines=[*RUN_HEADLINES, "Family Dentist in Palo Alto"])
    before = json.dumps(plan, sort_keys=True)
    dropped = ads_launch.Dropped(
        headlines=frozenset({"Same-Week Appointments"}),
        descriptions=frozenset({RUN_DESCRIPTIONS[1]}),
        keywords=frozenset({"invisalign cost"}),
    )
    ops = ads_launch.operations(
        plan, KNOWLEDGE, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID, dropped=dropped
    )
    ad = _ad(ops)
    assert [h["text"] for h in ad["headlines"]] == [
        "Invisalign in Palo Alto",
        "Clear Aligners Near You",
        "Family Dentist in Palo Alto",
    ]
    assert [d["text"] for d in ad["descriptions"]] == [RUN_DESCRIPTIONS[0]]
    assert "invisalign cost" not in _keywords_sent(ops) and "invisalign palo alto" in _keywords_sent(ops)
    assert json.dumps(plan, sort_keys=True) == before
    plain = ads_launch.operations(
        plan, KNOWLEDGE, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID
    )
    assert plain == ads_launch.operations(
        plan,
        KNOWLEDGE,
        page_url=PAGE_URL,
        target=TARGET,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
        dropped=ads_launch.NOTHING_DROPPED,
    )
    assert "Same-Week Appointments" in [h["text"] for h in _ad(plain)["headlines"]]


def test_diff_leaves_the_dropped_lines_out_of_the_fresh_ad_and_the_new_keywords():
    old = _plan()
    new = _plan(headlines=[*RUN_HEADLINES, "Family Dentist in Palo Alto"])
    new["sections"]["rationale"]["example_searches"].append("invisalign price")
    resources = {
        "campaign": f"customers/{CUSTOMER_ID}/campaigns/8",
        "campaign_budget": f"customers/{CUSTOMER_ID}/campaignBudgets/9",
        "ad_groups": {"invisalign palo alto": f"customers/{CUSTOMER_ID}/adGroups/3"},
        "ads": {"invisalign palo alto": f"customers/{CUSTOMER_ID}/adGroupAds/3~1"},
        "keywords": {"invisalign palo alto": {"invisalign palo alto": f"customers/{CUSTOMER_ID}/adGroupCriteria/3~1"}},
        "negatives": {},
        "geo_criteria": [],
        "final_url": PAGE_URL,
    }
    dropped = ads_launch.Dropped(
        headlines=frozenset({"Same-Week Appointments"}), keywords=frozenset({"invisalign price"})
    )
    ops = ads_launch.diff(
        old, new, resources, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID, dropped=dropped
    )
    assert [h["text"] for h in _ad(ops)["headlines"]] == [
        "Invisalign in Palo Alto",
        "Clear Aligners Near You",
        "Family Dentist in Palo Alto",
    ]
    assert _keywords_sent(ops) == ["invisalign cost"]  # the new phrase Google refused is not added either


def test_diff_leaves_out_a_new_phrase_a_block_rule_matches_too():
    """Ruling X3: `diff`'s own inline exclusion on a kept theme's added search phrases
    (`kw not in existing and not dropped.holds("keyword", kw)`) asks `Dropped.holds`, not the exact dropped texts
    alone -- a block rule's phrase is never added either, even though it was never refused by its exact text."""
    old = _plan()
    new = _plan(headlines=[*RUN_HEADLINES, "Family Dentist in Palo Alto"])
    new["sections"]["rationale"]["example_searches"].append("invisalign price")
    resources = {
        "campaign": f"customers/{CUSTOMER_ID}/campaigns/8",
        "campaign_budget": f"customers/{CUSTOMER_ID}/campaignBudgets/9",
        "ad_groups": {"invisalign palo alto": f"customers/{CUSTOMER_ID}/adGroups/3"},
        "ads": {"invisalign palo alto": f"customers/{CUSTOMER_ID}/adGroupAds/3~1"},
        "keywords": {"invisalign palo alto": {"invisalign palo alto": f"customers/{CUSTOMER_ID}/adGroupCriteria/3~1"}},
        "negatives": {},
        "geo_criteria": [],
        "final_url": PAGE_URL,
    }
    price_rule = local_rules.compile_rule(
        {"kind": "word", "applies_to": "keyword", "value": "price", "level": "block", "why": "a word we avoid"}
    )
    dropped = ads_launch.Dropped(rules=(price_rule,))
    ops = ads_launch.diff(
        old, new, resources, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CUSTOMER_ID, dropped=dropped
    )
    assert _keywords_sent(ops) == ["invisalign cost"]  # "invisalign price" matches the block rule, never created
    assert not any(
        op.get("adGroupCriterionOperation", {}).get("create", {}).get("keyword", {}).get("text") == "invisalign price"
        for op in ops
    )


def test_diff_leaves_out_an_added_themes_phrase_a_block_rule_matches_too():
    """Ruling X3, the other site: `diff`'s added-theme keyword list (`p not in dropped.keywords`) also must ask
    `Dropped.holds` -- a new ad group's theme name is a search phrase too, and a block rule matching it (not just the
    exact dropped texts) keeps it out of the mutate, even though it was never refused before."""
    old = _diff_plan(monthly_usd=2000, themes=["invisalign palo alto"], headlines=HEADLINES, descriptions=DESCRIPTIONS)
    new = _diff_plan(
        monthly_usd=2000,
        themes=["invisalign palo alto", "implants palo alto"],
        headlines=HEADLINES,
        descriptions=DESCRIPTIONS,
    )
    implants_rule = local_rules.compile_rule(
        {"kind": "word", "applies_to": "keyword", "value": "implants", "level": "block", "why": "a word we avoid"}
    )
    ops = ads_launch.diff(
        old,
        new,
        RESOURCES,
        page_url=PAGE_URL,
        target=TARGET,
        account=ACCOUNT,
        customer_id=CUSTOMER_ID,
        dropped=ads_launch.Dropped(rules=(implants_rule,)),
    )
    assert "implants palo alto" not in _keywords_sent(ops)  # the new theme's own name matches the block rule
    assert "invisalign palo alto" in _keywords_sent(ops)  # the example search alone is still added for it
