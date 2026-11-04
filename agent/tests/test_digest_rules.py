"""The "needs you" items (digest spec 2026-09-30 §5; plan 2026-10-07-agent-k2 Task 4): owner-actionable only, fixed
owner sentences (never a platform's own words), every window measured from the end of the reported day, links to the
Your ads page, sorted high first and deduplicated."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg.types.json import Jsonb

from agent import (
    ads_run,
    audit,
    benchmark,
    config,
    copy,
    digest_audience,
    digest_numbers,
    digest_rules,
    digest_zone,
    wallet,
)
from agent.tests import digest_fixtures as df

LA = ZoneInfo("America/Los_Angeles")
D = date(2026, 10, 5)
START, END = digest_zone.bounds(D, LA)
LAUNCHED = datetime(2026, 8, 1, tzinfo=UTC)


def ctx(conn, day: date = D, *, weekly: bool = False, category: str = "dentists") -> digest_rules.Ctx:
    settings = config.load()
    business = {"id": "biz_1", "paused_reason": None, "digest_frequency": "daily"}
    aud = digest_audience.audience(conn, business, day, LA)
    numbers = digest_numbers.gather(conn, "biz_1", aud, day, LA, weekly=weekly)
    start, end = digest_zone.bounds(day, LA)
    return digest_rules.Ctx(
        conn=conn,
        settings=settings,
        business_id="biz_1",
        day=day,
        zone=LA,
        start=start,
        end=end,
        aud=aud,
        numbers=numbers,
        category=category,
        weekly=weekly,
    )


def found(conn, **kw) -> dict[str, digest_rules.Item]:
    return {i.ref: i for i in digest_rules.items(ctx(conn, **kw))}


@pytest.fixture
def live(conn):
    """A Los Angeles business running a $600 Google Search plan since August."""
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    df.seed_plan(conn, "biz_1", {"google_search": 600})
    df.seed_channel(conn, "biz_1", launched_at=LAUNCHED, resources={"campaign": "customers/1/campaigns/2"})
    return "biz_1"


def test_a_quiet_live_day_needs_nothing(conn, live):
    df.seed_metric(conn, "biz_1", D, cost_usd=15, clicks=10, impressions=200, calls=1)
    assert digest_rules.items(ctx(conn)) == []


@pytest.mark.parametrize(("hours", "fires"), [(47, False), (49, True)])
def test_leads_unmarked_after_48_hours(conn, live, hours, fires):
    df.seed_lead(conn, "biz_1", END - timedelta(hours=hours))
    assert ("leads_unmarked" in found(conn)) is fires


def test_leads_unmarked_counts_14_days_and_names_the_number(conn, live):
    df.seed_lead(conn, "biz_1", END - timedelta(days=15))  # too old
    df.seed_lead(conn, "biz_1", END - timedelta(days=3))
    df.seed_lead(conn, "biz_1", END - timedelta(days=4), channel=None)  # the page's own lead counts too
    item = found(conn)["leads_unmarked"]
    assert item.severity == "medium" and item.text.startswith("2 people who contacted you are still marked new.")
    assert item.link is None


def _proposal(conn, pid: str, expires: datetime, *, cls: str = "spend") -> None:
    conn.execute(
        "INSERT INTO proposal (id, business_id, class, kind, payload, summary, expires_at) "
        "VALUES (%s, 'biz_1', %s, 'approve_plan', %s, 'Raise Google Search to $800 a month.', %s)",
        (pid, cls, Jsonb({"approval_url": "http://testserver/plan/pl_9"}), expires),
    )


@pytest.mark.parametrize(("hours", "fires"), [(47, True), (49, False)])
def test_proposal_pending_in_its_last_48_hours(conn, live, hours, fires):
    _proposal(conn, "pr_1", END + timedelta(hours=hours))
    item = found(conn).get("proposal:pr_1")
    assert (item is not None) is fires
    if item:
        expires = (END + timedelta(hours=hours)).astimezone(LA).strftime("%A")
        assert item.text == f"Waiting for your yes: Raise Google Search to $800 a month. It expires {expires}."
        assert item.link == ("Review and approve", "http://testserver/plan/pl_9")


def test_a_nonspend_proposal_links_to_admiry(conn, live):
    _proposal(conn, "pr_2", END + timedelta(hours=10), cls="nonspend")
    assert found(conn)["proposal:pr_2"].link[0] == "Open Admiry"


@pytest.mark.parametrize(("clicks", "fires"), [(29, False), (30, True)])
def test_tracking_silent_at_30_clicks(conn, live, clicks, fires):
    for i in range(7):
        n = clicks // 7 + (1 if i < clicks % 7 else 0)
        df.seed_metric(conn, "biz_1", D - timedelta(days=i), cost_usd=10, clicks=n, impressions=100)
    item = found(conn).get("tracking:google_search")
    assert (item is not None) is fires
    if item:
        assert item.link == ("Open Settings", f"{config.load().web_base_url}/connect?b=biz_1&section=advanced")


@pytest.mark.parametrize(("usd", "fires"), [(6.12, False), (5.72, True)])  # 31 % and 29 % of $19.73
def test_underdelivery_three_days_under_a_third(conn, live, usd, fires):
    for i in range(3):
        df.seed_metric(conn, "biz_1", D - timedelta(days=i), cost_usd=usd, clicks=3, impressions=50)
    item = found(conn).get("underdelivery:google_search")
    assert (item is not None) is fires
    if item:
        assert item.severity == "info" and item.link is None


@pytest.mark.parametrize(("usd", "day", "fires"), [(540, D, True), (534, D, False), (540, date(2026, 10, 25), False)])
def test_limit_near_at_90_percent_before_the_25th(conn, live, usd, day, fires):
    df.seed_metric(conn, "biz_1", day, cost_usd=usd)
    item = found(conn, day=day).get("limit:google_search")
    assert (item is not None) is fires
    if item:
        assert item.text == "Google Search: $540 of its $600 for October; it pauses at $600 and restarts November 1."


def test_limit_near_on_the_24th(conn, live):
    df.seed_metric(conn, "biz_1", date(2026, 10, 24), cost_usd=540)
    assert "limit:google_search" in found(conn, day=date(2026, 10, 24))


def test_no_leads_spend_against_the_benchmark(conn, live):
    benchmark.seed(conn)
    floor = max(digest_rules.NO_LEADS_FLOOR_USD, benchmark.lookup(conn, "dentists", "google_search")["cpl_avg"])
    df.seed_metric(conn, "biz_1", D, cost_usd=floor - 1)
    assert "no_leads:google_search" not in found(conn)
    df.seed_metric(conn, "biz_1", D, cost_usd=floor + 1)
    item = found(conn)["no_leads:google_search"]
    assert item.severity == "medium" and item.text.startswith("Google Search spent $")


def test_no_leads_spend_without_a_benchmark(conn, live):
    # no benchmark rows: 45 % of 14 days at $19.73 a day is $124.30
    df.seed_metric(conn, "biz_1", D, cost_usd=124)
    assert "no_leads:google_search" not in found(conn)
    df.seed_metric(conn, "biz_1", D, cost_usd=125)
    assert "no_leads:google_search" in found(conn)
    df.seed_lead(conn, "biz_1", END - timedelta(days=2))  # one lead in the 14 days: nothing to say
    assert "no_leads:google_search" not in found(conn)


def test_no_leads_spend_needs_14_days_live(conn, live):
    conn.execute("UPDATE channel_campaign SET launched_at = %s", (END - timedelta(days=10),))
    df.seed_metric(conn, "biz_1", D, cost_usd=500)
    assert "no_leads:google_search" not in found(conn)


def _issues(conn, issues: dict, channel: str = "google_search") -> None:
    conn.execute(
        "UPDATE channel_campaign SET state = 'limited', resources = resources || %s WHERE channel = %s",
        (Jsonb({"issues": issues}), channel),
    )


def test_learning_under_review_and_budget_limited_codes_make_no_item(conn, live):
    _issues(
        conn,
        {"campaign": ["BIDDING_STRATEGY_LEARNING", "BUDGET_CONSTRAINED", "MOST_ADS_UNDER_REVIEW", "CAMPAIGN_PENDING"]},
    )
    assert not [i for i in digest_rules.items(ctx(conn)) if i.rule == "channel_issue"]


def test_unknown_code_is_medium_and_generic_and_keeps_the_code(conn, live):
    _issues(conn, {"campaign": ["MISSING_LOCATION_TARGETING", "HAS_ADS_DISAPPROVED"]})
    got = found(conn)
    unknown = got["code:google_search:MISSING_LOCATION_TARGETING"]
    assert unknown.severity == "medium" and unknown.codes == ("MISSING_LOCATION_TARGETING",)
    assert unknown.text == "Google Search is holding back some of your ads; Admiry is looking into it."
    assert got["code:google_search:HAS_ADS_DISAPPROVED"].severity == "high"


def test_issue_text_is_never_googles(conn, live):
    _issues(conn, {"ads": [{"resource_name": "x", "topics": [{"topic": "HEALTH_IN_PERSONALIZED_ADS"}]}]})
    item = found(conn)["ads:google_search"]
    assert item.text == "Google Search stopped one or more of your ads. Open Admiry to see the reason."
    assert "HEALTH" not in item.text and item.link == (
        digest_rules.ADS_LINK,
        f"{config.load().web_base_url}/ads?b=biz_1",
    )


def test_newsbreak_issues_come_from_its_rows(conn, live):
    df.seed_channel(conn, "biz_1", "newsbreak", launched_at=LAUNCHED)
    _issues(conn, {"ads": [{"resource_name": "ignored"}]}, channel="newsbreak")
    conn.execute(
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text) "
        "VALUES ('biz_1', 'newsbreak', 'billing', 'NewsBreak says: card declined')"
    )
    issues = [i for i in digest_rules.items(ctx(conn)) if i.rule == "channel_issue"]
    billing = digest_rules.ISSUE_SENTENCES["billing"].format(label="NewsBreak")
    assert billing.startswith("NewsBreak can't charge the payment method") and billing.endswith("in NewsBreak.")
    assert [i.text for i in issues] == [billing]
    assert issues[0].ref.startswith("issue:") and "declined" not in issues[0].text


def test_ads_links_go_to_the_your_ads_page(conn, live):
    conn.execute("UPDATE channel_campaign SET state = 'paused', paused_reason = 'dispute', paused_at = %s", (START,))
    item = found(conn)["pause:google_search"]
    assert item.link == (digest_rules.ADS_LINK, f"{config.load().web_base_url}/ads?b=biz_1")


@pytest.mark.parametrize(
    ("reason", "severity", "start"),
    [
        ("no_funds", "high", "Google Search is paused: there is no money left to run it."),
        ("no_leads", "high", "Google Search is paused: it spent $"),
        ("dispute", "high", "Google Search is paused while a card payment is disputed."),
        ("page_down", "high", "Google Search is paused: the page your ads send people to is not answering."),
        ("unreachable", "high", "Google Search is paused: the page your ads send people to is not answering."),
        ("month_cap", "info", "Google Search is paused until November 1: October's $600 is used."),
    ],
)
def test_paused_by_system(conn, live, reason, severity, start):
    conn.execute("UPDATE channel_campaign SET state = 'paused', paused_reason = %s, paused_at = %s", (reason, START))
    item = found(conn)["pause:google_search"]
    assert item.severity == severity and item.text.startswith(start)


def test_month_cap_item_dropped_on_the_first_after_resume(conn, live):
    # paused for the month on the 30th, started again by the spend guard on the 1st: the morning has nothing to say
    day = date(2026, 9, 30)
    conn.execute("UPDATE channel_campaign SET paused_at = %s", (digest_zone.bounds(day, LA)[0] + timedelta(hours=9),))
    df.seed_metric(conn, "biz_1", day, cost_usd=30)
    assert "pause:google_search" not in found(conn, day=day)


def test_waiting_account_while_another_place_runs(conn, live):
    df.seed_plan(conn, "biz_1", {"google_search": 600, "newsbreak": 300})
    conn.execute("UPDATE plan SET status = 'superseded' WHERE version = 1")
    df.seed_channel(conn, "biz_1", "newsbreak", state="waiting_account")
    item = found(conn)["waiting:newsbreak"]
    assert item.severity == "high" and item.link[0] == digest_rules.ADS_LINK and copy.jargon(item.text) is None


def test_waiting_account_never_carries_the_platforms_words(conn, live):
    """Final review I4: an open issue on the waiting row puts the platform's own text on the channels card; the report's
    item (and the Analyst's material) says where the row stands in Admiry's words instead."""
    df.seed_plan(conn, "biz_1", {"google_search": 600, "newsbreak": 300})
    conn.execute("UPDATE plan SET status = 'superseded' WHERE version = 1")
    df.seed_channel(conn, "biz_1", "newsbreak", state="waiting_account")
    conn.execute(
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text) "
        "VALUES ('biz_1', 'newsbreak', 'account', 'Your campaign account was rejected: visit nb.example/x')"
    )
    item = found(conn)["waiting:newsbreak"]
    assert "nb.example" not in item.text and "rejected" not in item.text and copy.jargon(item.text) is None
    assert item.text.startswith("NewsBreak: ")


def _managed(conn, balance_cents: int) -> None:
    conn.execute(
        "INSERT INTO channel_account (business_id, channel, mode, status) VALUES ('biz_1', 'google_search', 'managed', 'active')"
    )
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=balance_cents, ref="pi_1", created_by="stripe")


@pytest.mark.parametrize(("balance", "fires"), [(6000, True), (8000, False)])  # $20 a day: 3 days, 4 days
def test_funds_low_reads_the_balance_and_the_daily_budget(conn, live, balance, fires):
    _managed(conn, balance)
    item = found(conn).get("funds_low")
    assert (item is not None) is fires
    if item:
        assert item.text == "Your Admiry balance covers about 3 more days of ads. Add funds to keep them running."


def test_funds_low_never_for_a_business_google_bills(conn, live):
    assert "funds_low" not in found(conn)


def _spend_guard(conn, channel: str = "google_search", when: datetime | None = None) -> None:
    audit.record_job(
        conn,
        job="spend-guard",
        tool="job.spend_guard",
        business_id="biz_1",
        args={"channel": channel},
        result={"paused": [channel]},
    )
    conn.execute("UPDATE agent_action SET ts = %s WHERE id = (SELECT max(id) FROM agent_action)", (when or START,))


def test_safe_action_not_repeated_after_a_pause_item(conn, live):
    _spend_guard(conn, when=START + timedelta(hours=3))
    assert [i.rule for i in digest_rules.items(ctx(conn))] == ["safe_action_taken"]
    conn.execute("UPDATE channel_campaign SET state = 'paused', paused_reason = 'no_funds', paused_at = %s", (START,))
    assert [i.rule for i in digest_rules.items(ctx(conn))] == ["paused_by_system"]


def test_two_actions_keep_two_items(conn, live):
    _spend_guard(conn, when=START + timedelta(hours=1))
    _spend_guard(conn, when=START + timedelta(hours=2))
    assert len([i for i in digest_rules.items(ctx(conn)) if i.rule == "safe_action_taken"]) == 2


def test_two_channels_keep_two_items(conn, live):
    df.seed_plan(conn, "biz_1", {"google_search": 600, "newsbreak": 600})
    conn.execute("UPDATE plan SET status = 'superseded' WHERE version = 1")
    df.seed_channel(conn, "biz_1", "newsbreak", launched_at=LAUNCHED)
    for ch in ("google_search", "newsbreak"):
        for i in range(3):
            df.seed_metric(conn, "biz_1", D - timedelta(days=i), ch, cost_usd=1, impressions=10)
        df.seed_metric(conn, "biz_1", date(2026, 10, 1), ch, cost_usd=541, impressions=10)  # the month near its limit
    refs = set(found(conn))
    assert {"underdelivery:google_search", "underdelivery:newsbreak", "limit:google_search", "limit:newsbreak"} <= refs


def test_items_sorted_and_deduped(conn, live):
    df.seed_lead(conn, "biz_1", END - timedelta(days=3))  # medium
    for i in range(3):
        df.seed_metric(conn, "biz_1", D - timedelta(days=i), cost_usd=1, impressions=10)  # info: underdelivery
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s",
        (Jsonb({"issues": {"campaign": ["HAS_ADS_DISAPPROVED", "HAS_ADS_DISAPPROVED"]}}),),
    )
    got = digest_rules.items(ctx(conn))
    assert [i.severity for i in got] == sorted((i.severity for i in got), key=digest_rules.SEVERITY.get)
    assert len({(i.rule, i.ref) for i in got}) == len(got)
    assert got[0].severity == "high"


def test_windows_are_measured_from_the_end_of_the_day(conn, live, monkeypatch):
    df.seed_lead(conn, "biz_1", END - timedelta(hours=49))
    _proposal(conn, "pr_1", END + timedelta(hours=40))
    monkeypatch.setattr(ads_run, "now", lambda: END + timedelta(hours=1, minutes=2))
    at_8 = digest_rules.items(ctx(conn))
    monkeypatch.setattr(ads_run, "now", lambda: END + timedelta(hours=3, minutes=2))
    assert digest_rules.items(ctx(conn)) == at_8


def test_every_rule_text_passes_owner_words(conn, live):
    df.seed_lead(conn, "biz_1", END - timedelta(days=3))
    _proposal(conn, "pr_1", END + timedelta(hours=10))
    _spend_guard(conn, when=START + timedelta(hours=1))
    _issues(conn, {"campaign": ["HAS_ADS_DISAPPROVED", "MISSING_LOCATION_TARGETING"], "ads": [{"resource_name": "x"}]})
    df.seed_metric(conn, "biz_1", D, cost_usd=541, impressions=10)
    texts = [i.text for i in digest_rules.items(ctx(conn))]
    texts += [t.format(label="Google Search") for t in digest_rules.ISSUE_SENTENCES.values()]
    assert len(texts) > 6
    for text in texts:
        assert copy.jargon(text) is None, text
