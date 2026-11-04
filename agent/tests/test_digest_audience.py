"""Who gets which edition (digest spec 2026-09-30 §3, §13; plan 2026-10-07-agent-k2 Task 2): each channel row
classified -- active, paused by the owner, ignored, or report-only -- and the business's edition from them, its
frequency and the weekday; the sync gate that holds a morning's digest until the platforms' figures are in."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import config, digest_audience
from agent.channels.google_search.adapter import PAUSED_IN_GOOGLE
from agent.tests import digest_fixtures as df

LA = ZoneInfo("America/Los_Angeles")
D = date(2026, 10, 5)  # a Monday: the Tuesday morning's report is the daily edition
SUN = date(2026, 10, 4)  # a Sunday: the Monday morning's report is the weekly edition
OWNER = {"owner_paused": True}


def at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=LA)


BEFORE = at(D - timedelta(days=3), 9)
LIVE = [{"state": "live"}]

CASES = [
    ("live on a Tuesday", D, LIVE, {}, None, "daily", "daily"),
    ("live on a Monday", SUN, LIVE, {}, None, "daily", "weekly"),
    ("live with no spend for a week", D, LIVE, {}, None, "daily", "daily"),
    ("launching", D, [{"state": "launching"}], {}, None, "daily", "daily"),
    ("limited", D, [{"state": "limited"}], {}, None, "daily", "daily"),
    (
        "paused in Google, spend 3 days ago, Monday",
        SUN,
        [{"state": "limited", "last_error": PAUSED_IN_GOOGLE}],
        {"google_search": [-3]},
        None,
        "daily",
        "weekly",
    ),
    (
        "paused in Google, spend 3 days ago, Tuesday",
        D,
        [{"state": "limited", "last_error": PAUSED_IN_GOOGLE}],
        {"google_search": [-3]},
        None,
        "daily",
        None,
    ),
    (
        "paused month_cap",
        D,
        [{"state": "paused", "reason": "month_cap", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "paused no_funds",
        D,
        [{"state": "paused", "reason": "no_funds", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "paused no_leads",
        D,
        [{"state": "paused", "reason": "no_leads", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "paused page_down",
        D,
        [{"state": "paused", "reason": "page_down", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "paused unreachable",
        D,
        [{"state": "paused", "reason": "unreachable", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "dispute under collections",
        D,
        [{"state": "paused", "reason": "dispute", "paused_at": BEFORE}],
        {},
        "collections",
        "daily",
        None,
    ),
    ("dispute alone", D, [{"state": "paused", "reason": "dispute", "paused_at": BEFORE}], {}, None, "daily", "daily"),
    (
        "owner-paused at 07:00 the next morning",
        D,
        [{"state": "paused", "resources": OWNER, "paused_at": at(D + timedelta(days=1), 7)}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "owner-paused at 15:00 on the day",
        D,
        [{"state": "paused", "resources": OWNER, "paused_at": at(D, 15)}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "owner-paused before the day, no spend for 7 days",
        D,
        [{"state": "paused", "resources": OWNER, "paused_at": at(D - timedelta(days=10), 9)}],
        {},
        None,
        "daily",
        None,
    ),
    (
        "owner-paused, spend 3 days ago, Monday",
        SUN,
        [{"state": "paused", "resources": OWNER, "paused_at": at(SUN - timedelta(days=2), 9)}],
        {"google_search": [-3]},
        None,
        "daily",
        "weekly",
    ),
    (
        "owner-paused row with cost on the day",
        D,
        [{"state": "paused", "resources": OWNER, "paused_at": BEFORE}],
        {"google_search": [0]},
        None,
        "daily",
        "daily",
    ),
    (
        "stop_requested at 10:00 on the day",
        D,
        [{"state": "paused", "reason": "stop_requested", "paused_at": at(D, 10)}],
        {},
        None,
        "daily",
        "daily",
    ),
    (
        "stop_requested before the day",
        D,
        [{"state": "paused", "reason": "stop_requested", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        None,
    ),
    (
        "dropped before the day",
        D,
        [{"state": "paused", "reason": "dropped", "paused_at": BEFORE}],
        {},
        None,
        "daily",
        None,
    ),
    (
        "only waiting and queued",
        D,
        [{"state": "waiting_account"}, {"channel": "newsbreak", "state": "queued"}],
        {},
        None,
        "daily",
        None,
    ),
    ("only failed", D, [{"state": "failed"}], {}, None, "daily", None),
    ("weekly reader on a Tuesday", D, LIVE, {}, None, "weekly", None),
    ("weekly reader on a Monday", SUN, LIVE, {}, None, "weekly", "weekly"),
    ("reports off", D, LIVE, {}, None, "off", None),
]


@pytest.mark.parametrize(
    ("name", "day", "rows", "spend", "reason", "frequency", "edition"), CASES, ids=[c[0] for c in CASES]
)
def test_edition(conn, name, day, rows, spend, reason, frequency, edition):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    for row in rows:
        df.seed_channel(conn, "biz_1", **row)
    for channel, offsets in spend.items():
        for off in offsets:
            df.seed_metric(conn, "biz_1", day + timedelta(days=off), channel, cost_usd=5.0)
    business = {"id": "biz_1", "paused_reason": reason, "digest_frequency": frequency}
    assert digest_audience.audience(conn, business, day, LA).edition == edition


def test_report_only_rows_never_make_a_business_eligible(conn):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    df.seed_channel(conn, "biz_1", state="operator")
    df.seed_metric(conn, "biz_1", D, cost_usd=12.0)
    aud = digest_audience.audience(conn, {"id": "biz_1", "paused_reason": None, "digest_frequency": "daily"}, D, LA)
    assert aud.edition is None and [c.cls for c in aud.channels] == ["report_only"]


def test_paused_on_the_platform_reads_the_adapters_words(conn):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    df.seed_channel(conn, "biz_1", state="limited", last_error=PAUSED_IN_GOOGLE)
    df.seed_channel(conn, "biz_1", "newsbreak", state="limited", last_error="anything at all")
    aud = digest_audience.audience(conn, {"id": "biz_1", "paused_reason": None, "digest_frequency": "daily"}, D, LA)
    assert {c.channel: c.cls for c in aud.channels} == {"google_search": "owner_paused", "newsbreak": "active"}
    assert digest_audience.paused_on_platform(
        {"channel": "google_search", "state": "limited", "last_error": PAUSED_IN_GOOGLE}
    )
    assert not digest_audience.paused_on_platform(
        {"channel": "google_search", "state": "live", "last_error": PAUSED_IN_GOOGLE}
    )


def test_gate(conn, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    since = at(D + timedelta(days=1), 0).astimezone(UTC)
    campaign = {"campaign": "customers/1/campaigns/2"}
    df.seed_channel(conn, "biz_1", resources=campaign, synced_at=since - timedelta(hours=1))
    assert digest_audience.gate(conn, config.load(), "biz_1", since) is False
    conn.execute("UPDATE channel_campaign SET synced_at = %s", (since + timedelta(minutes=5),))
    assert digest_audience.gate(conn, config.load(), "biz_1", since) is True
    conn.execute("UPDATE channel_campaign SET synced_at = NULL")  # never synced: no hold
    assert digest_audience.gate(conn, config.load(), "biz_1", since) is True
    conn.execute("UPDATE channel_campaign SET synced_at = %s, resources = '{}'", (since - timedelta(hours=1),))
    assert digest_audience.gate(conn, config.load(), "biz_1", since) is True  # no campaign built
    df.seed_channel(conn, "biz_1", "newsbreak", resources=campaign, synced_at=since - timedelta(hours=1))
    assert digest_audience.gate(conn, config.load(), "biz_1", since) is True  # its switch is validate
