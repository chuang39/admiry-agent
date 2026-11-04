"""L1 Task 1 (spec 2026-10-10 §4.1): the launch's own rules as lists -- every refusal at once, in the order the launch
asks them -- for the checklist; the launch itself (ads_run.precheck, _launch_rules, the Google adapter's precheck)
still refuses with the first, word for word as before."""

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, business, config, landing, plans
from agent.channels import registry
from agent.channels.google_search import adapter as google
from agent.tests.test_ads_run import (
    BIZ,
    CHECK_OK,
    PAGE_URL,
    PHONE_FIX,
    _check_failed,
    _knowledge,
    _no_page,
    _unchecked,
    sections,
    seed,
)

GOOGLE = "google_search"


def _first(rule) -> str | None:
    try:
        rule()
    except ToolError as e:
        return str(e)
    return None


def test_the_plans_own_rules_list_every_refusal_and_precheck_refuses_with_the_first(conn):
    seed(conn, status="draft", secs=sections(monthly_usd=90.0))  # under Google's $100 floor for an own account
    business.set_goal(conn, BIZ, monthly_limit_usd=50)  # and over the owner's limit
    plan = plans.latest(conn, BIZ)
    under = ads_run.UNDER_FLOOR.format(label="Google Search", floor=plans.usd(100))
    assert ads_run.plan_refusals(conn, BIZ, plan) == [under, ads_run.OVER_LIMIT]
    assert _first(lambda: ads_run.precheck(conn, config.load(), BIZ, plan)) == under


def test_googles_own_rules_list_every_refusal_and_its_precheck_refuses_with_the_first(conn):
    seed(conn, status="draft", secs=sections(headlines=["Invisalign in Palo Alto"]))  # too little ad text
    _knowledge(conn, phone=None, listing=None, service_area=[])  # no phone, nowhere to centre on
    plan, knowledge = plans.latest(conn, BIZ), business.latest_knowledge(conn, BIZ)[1]
    adapter = registry.adapter(GOOGLE)
    assert adapter.refusals(conn, config.load(), BIZ, plan, knowledge) == [
        google.TOO_LITTLE_COPY,
        ads_run.NO_PHONE,
        ads_run.NO_LOCATION,
    ]
    assert _first(lambda: adapter.precheck(conn, config.load(), BIZ, plan, knowledge)) == google.TOO_LITTLE_COPY


def test_a_plan_google_cannot_run_is_that_refusal_alone(conn):
    secs = sections(headlines=["Invisalign in Palo Alto"])
    secs["ads"]["channels"][0]["monthly_usd"] = 0  # no budget: nothing for Google to run (R1: an empty `estimate`
    # breaks `seed`'s own `plans.owner_lines` with a bare KeyError, not the ValueError `_search_channel` catches)
    seed(conn, status="draft", secs=secs)
    _knowledge(conn, phone=None)
    plan, knowledge = plans.latest(conn, BIZ), business.latest_knowledge(conn, BIZ)[1]
    assert registry.adapter(GOOGLE).refusals(conn, config.load(), BIZ, plan, knowledge) == [google.NOT_SEARCH_PLAN]


def test_every_failed_page_rule_is_its_own_refusal_and_the_page_rule_refuses_with_the_first(conn):
    seed(conn, status="draft", secs=sections())
    _check_failed(conn)  # phone and tracking fail; mobile only warns
    assert ads_run.page_refusals(conn, BIZ, GOOGLE) == [
        (ads_run.PAGE_FIX.format(fix=PHONE_FIX), "phone"),
        (ads_run.PAGE_FIX.format(fix="Paste the tracking snippet."), "tracking"),
    ]
    assert _first(lambda: ads_run._page_ready(conn, BIZ, GOOGLE)) == ads_run.PAGE_FIX.format(fix=PHONE_FIX)


@pytest.mark.parametrize(
    ("change", "sentence"),
    [(_no_page, ads_run.NO_PAGE), (_unchecked, ads_run.PAGE_UNCHECKED.format(label="Google Search"))],
)
def test_no_page_or_no_report_is_one_refusal(conn, change, sentence):
    seed(conn, status="draft", secs=sections())
    change(conn)
    assert ads_run.page_refusals(conn, BIZ, GOOGLE) == [(sentence, None)]


def test_the_page_rules_read_the_place_and_the_page_once(conn, monkeypatch):
    """Page-first review, item 3: page_refusals reads the place's row and the business's page once each -- whether a
    page is there (ads_run.has_page's own rule) and what its report says come from the same two reads."""
    seed(conn, status="draft", secs=sections())
    _check_failed(conn)
    reads: list[str] = []
    row, page = ads_run.get, landing.current
    monkeypatch.setattr(ads_run, "get", lambda *a, **k: reads.append("row") or row(*a, **k))
    monkeypatch.setattr(landing, "current", lambda *a, **k: reads.append("page") or page(*a, **k))
    assert ads_run.page_refusals(conn, BIZ, GOOGLE)[0] == (ads_run.PAGE_FIX.format(fix=PHONE_FIX), "phone")
    assert sorted(reads) == ["page", "row"]
    reads.clear()
    _no_page(conn)
    assert ads_run.page_refusals(conn, BIZ, GOOGLE) == [(ads_run.NO_PAGE, None)] and sorted(reads) == ["page", "row"]


def test_a_report_older_than_a_places_rules_is_unchecked_and_nothing_is_fetched(conn, monkeypatch):
    seed(conn, status="draft", secs=sections())
    stale = {"ok": True, "url": PAGE_URL, "rules": [{"id": "reachable", "ok": True, "fix": None}]}
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(stale),))
    monkeypatch.setattr(landing, "check", lambda *a, **k: pytest.fail("the list never fetches the page"))
    assert ads_run.page_refusals(conn, BIZ, "newsbreak") == [(ads_run.PAGE_UNCHECKED.format(label="NewsBreak"), None)]


def test_a_rule_a_place_needs_that_only_warned_is_its_own_refusal(conn):
    seed(conn, status="draft", secs=sections())
    rules = [r for r in CHECK_OK["rules"] if r["id"] != "terms"] + [
        {"id": "terms", "ok": None, "fix": landing.TERMS_FIX}
    ]
    conn.execute("UPDATE page SET check_json = %s", (Jsonb({**CHECK_OK, "rules": rules}),))
    assert ads_run.page_refusals(conn, BIZ, "newsbreak") == [(ads_run.PAGE_FIX.format(fix=landing.TERMS_FIX), "terms")]
    assert ads_run.page_refusals(conn, BIZ, GOOGLE) == []  # Google Search needs no terms rule


def test_a_blocking_failure_outranks_a_rule_that_only_warned(conn):
    """R7 (fix round 1, Important review finding): page_refusals lists in the report's own priority order -- every
    BLOCKING rule the report fails outright first, as page_fix/_page_ready ask it -- never the checklist's fixed
    position order, which would rank terms (position 9, only a warning here) ahead of compliance (position 12, the
    rule that actually fails)."""
    seed(conn, status="draft", secs=sections())
    compliance_fix = "Remove the banned claim before your ads can launch."
    report = {
        "ok": False,
        "url": PAGE_URL,
        "rules": [
            {"id": "reachable", "ok": True, "fix": None},
            {"id": "phone", "ok": True, "fix": None},
            {"id": "tracking", "ok": True, "fix": None},
            {"id": "privacy", "ok": True, "fix": None},
            {"id": "terms", "ok": None, "fix": landing.TERMS_FIX},
            {"id": "contact", "ok": True, "fix": None},
            {"id": "compliance", "ok": False, "fix": compliance_fix},
        ],
    }
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(report),))
    found = ads_run.page_refusals(conn, BIZ, "newsbreak")
    assert found == [
        (ads_run.PAGE_FIX.format(fix=compliance_fix), "compliance"),
        (ads_run.PAGE_FIX.format(fix=landing.TERMS_FIX), "terms"),
    ]
    assert _first(lambda: ads_run._page_ready(conn, BIZ, "newsbreak", fetch=False)) == found[0][0]


def test_the_place_and_the_phone_list_every_refusal(conn):
    seed(conn, status="draft", secs=sections(radius_miles=None))
    plan = plans.latest(conn, BIZ)
    plan["sections"]["consultation"]["conversions"] = ["phone_call_60s"]  # the plan counts calls
    point = {"phone": None, "listing": {"lat": 37.4419, "lng": -122.143}}
    assert ads_run.place_and_phone(plan, "newsbreak", point) == [ads_run.NO_RADIUS, ads_run.NO_PHONE]
    assert ads_run.place_and_phone(plan, "newsbreak", {"phone": None}) == [ads_run.NO_LOCATION, ads_run.NO_PHONE]
    assert ads_run.place_and_phone(plan, GOOGLE, {"phone": None}) == [ads_run.NO_PHONE]  # radius: the adapter's rule
