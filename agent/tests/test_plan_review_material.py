"""K5 (P2a, P13): what the judge reads of a plan -- the cards, and every funded place's search phrases, left-out phrases
and lines of ad text with what the launch's linter would drop (`ads`) -- from the plan and the knowledge alone, so the
eval's frozen plans and the grading page read exactly what the S0 look reads."""

from agent import config, plan_review
from agent.tests.eval_fixtures import KNOWLEDGE, frozen_plan
from agent.tests.test_ads_run import DESCRIPTIONS, HEADLINES
from agent.tests.test_ads_run_channels import add_plan_by_hand
from agent.tests.test_review_launch import (  # noqa: F401 - `fake` is a pytest fixture used by name
    OWNER,
    draft_sections,
    fake,
    funded_prepaid,
)


def test_the_judge_reads_every_search_phrase_left_out_phrase_and_line_of_ad_text():
    plan = frozen_plan(
        themes=("invisalign palo alto", "clear aligners near me"),
        negatives=("jobs", "free"),
        headlines=[*HEADLINES, "Best Invisalign Dentist"],
    )
    m = plan_review.material_of(plan, KNOWLEDGE, final_host=None, money=None, blocked=None)
    assert list(m) == ["cards", "rationale", "ads", "knowledge", "money", "blocked"]
    (ads,) = m["ads"]
    assert (ads["channel"], ads["monthly_usd"], ads["landing_service"]) == ("Google Search", 1000, "Invisalign")
    assert ads["themes"] == ["invisalign palo alto", "clear aligners near me"] and ads["negatives"] == ["jobs", "free"]
    assert ads["headlines"] == [*HEADLINES, "Best Invisalign Dentist"] and ads["descriptions"] == DESCRIPTIONS
    assert len(ads["lint"]) == 1 and "Best Invisalign Dentist" in ads["lint"][0]
    assert m["cards"]["ads_preview"]["headlines"] == HEADLINES  # the owner's preview: the first three the linter kept
    assert m["rationale"]["example_searches"] == plan["sections"]["rationale"]["example_searches"]


def test_each_place_is_linted_with_its_own_launch_linter():
    wide = "Clear aligners from a family dentist who sees you the same week in Palo Alto, CA"
    assert len(wide) == 80  # NewsBreak's headlines run to 90, Google's to 30
    plan = frozen_plan(headlines=[*HEADLINES, wide])
    plan["sections"]["ads"]["channels"].append(
        {
            "channel": "newsbreak",
            "monthly_usd": 300,
            "headlines": [wide],
            "descriptions": DESCRIPTIONS,
            "landing_service": "Invisalign",
        }
    )
    m = plan_review.material_of(plan, KNOWLEDGE, final_host=None, money=None, blocked=None)
    google, newsbreak = m["ads"]
    assert (google["channel"], newsbreak["channel"]) == ("Google Search", "NewsBreak")
    assert len(google["lint"]) == 1 and wide in google["lint"][0]  # Google's linter: over 30 characters
    assert newsbreak["lint"] == [] and newsbreak["headlines"] == [wide]  # NewsBreak's own: 80 fits in 90


def test_a_plan_without_a_consultation_gives_the_judge_no_cards_and_no_ads():
    plan = frozen_plan()
    del plan["sections"]["consultation"]
    m = plan_review.material_of(plan, KNOWLEDGE, final_host=None, money=None, blocked=None)
    assert (m["cards"], m["rationale"], m["ads"]) == (None, None, [])


def test_the_second_look_hands_the_judge_the_ads(conn, fake):  # noqa: F811 - fixture used by name
    funded_prepaid(conn)
    add_plan_by_hand(conn, draft_sections(negatives=("jobs", "diy")), biz="biz_1", status="draft")
    plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    (call,) = fake.review_calls
    assert call["material"]["ads"][0]["negatives"] == ["jobs", "diy"]
    assert call["material"]["money"]["runway_days"] == 38  # the owner's look still reads the money
