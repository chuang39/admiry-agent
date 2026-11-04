"""Every number a digest shows, one source each (digest spec 2026-09-30 §4.3; plan 2026-10-07-agent-k2 Task 3): the
local day's forms and leads from the lead table, the platform's calls, the month by name, the daily budget the
platform receives, the Monday week against the week before, the sample word and the best channel, fixed owner
sentences for a row's state."""

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import copy, digest_audience, digest_numbers
from agent.tests import digest_fixtures as df

CHI = ZoneInfo("America/Chicago")
D = date(2026, 10, 5)
LAUNCHED = datetime(2026, 8, 1, tzinfo=UTC)


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=CHI)


def numbers(conn, day: date = D, *, weekly: bool = False):
    business = {"id": "biz_1", "paused_reason": None, "digest_frequency": "daily"}
    aud = digest_audience.audience(conn, business, day, CHI)
    return digest_numbers.gather(conn, "biz_1", aud, day, CHI, weekly=weekly)


@pytest.fixture
def live(conn):
    """A Chicago business running a $600 Google Search plan since August."""
    df.seed_business(conn, "biz_1", zone="America/Chicago")
    df.seed_plan(conn, "biz_1", {"google_search": 600})
    df.seed_channel(conn, "biz_1", launched_at=LAUNCHED)
    return "biz_1"


def test_forms_and_leads_follow_the_local_day(conn, live):
    df.seed_lead(conn, "biz_1", local(D, 23, 30))  # counts: still the 5th in Chicago
    df.seed_lead(conn, "biz_1", local(D + timedelta(days=1), 0, 10))  # the 6th
    df.seed_lead(conn, "biz_1", local(D, 12), kind="call")
    [row] = numbers(conn).rows
    assert (row.forms, row.leads) == (1, 2)


def test_calls_come_from_the_platform_and_page_calls_are_leads(conn, live):
    df.seed_metric(conn, "biz_1", D, cost_usd=23.10, clicks=41, calls=3)
    df.seed_lead(conn, "biz_1", local(D, 9), kind="call")
    [row] = numbers(conn).rows
    assert (row.spend_cents, row.clicks, row.calls, row.leads, row.forms) == (2310, 41, 3, 1, 0)


def test_organic_leads_get_their_own_count(conn, live):
    df.seed_lead(conn, "biz_1", local(D, 9), channel=None)
    n = numbers(conn)
    assert n.organic_leads == 1 and n.rows[0].leads == 0


def test_spam_and_owner_reported_leads_are_not_the_days_leads(conn, live):
    df.seed_lead(conn, "biz_1", local(D, 9), status="spam")
    lead = df.seed_lead(conn, "biz_1", local(D, 10), channel=None)
    conn.execute("UPDATE lead SET source = 'owner' WHERE id = %s", (lead,))
    n = numbers(conn)
    assert (n.rows[0].leads, n.organic_leads) == (0, 0)


def test_missing_metric_row_reads_zero(conn, live):
    [row] = numbers(conn).rows
    assert (row.spend_cents, row.clicks, row.impressions, row.calls, row.source) == (0, 0, 0, 0, "api")


def test_month_name_and_spend_at_a_month_boundary(conn, live):
    day = date(2026, 9, 30)
    df.seed_metric(conn, "biz_1", date(2026, 8, 31), cost_usd=100)
    df.seed_metric(conn, "biz_1", date(2026, 9, 1), cost_usd=10)
    df.seed_metric(conn, "biz_1", day, cost_usd=5)
    n = numbers(conn, day)
    assert (n.month_name, n.next_month_name) == ("September", "October")
    assert n.month_spend_cents == {"google_search": 1500}
    assert n.channel_amount_cents == {"google_search": 60000} and n.limit_usd == 600


def test_daily_budget_floor(conn, live):
    assert digest_numbers.daily_budget_cents(600) == 1973
    assert numbers(conn).rows[0].daily_budget_cents == 1973


def test_week_against_prior_week(conn, live):
    df.seed_metric(conn, "biz_1", D - timedelta(days=1), cost_usd=10, calls=1)
    df.seed_metric(conn, "biz_1", D - timedelta(days=8), cost_usd=20, calls=2)
    df.seed_lead(conn, "biz_1", local(D - timedelta(days=2), 9), status="booked")
    df.seed_lead(conn, "biz_1", local(D - timedelta(days=9), 9))
    [row] = numbers(conn, weekly=True).week.rows
    assert (row.spend_cents, row.calls, row.leads, row.booked) == (1000, 1, 1, 1)
    assert (row.prior_spend_cents, row.prior_calls, row.prior_leads, row.prior_booked) == (2000, 2, 1, 0)
    assert row.cost_per_lead_usd == 10.0


def _booked(conn, n: int) -> None:
    for i in range(n):
        df.seed_lead(conn, "biz_1", local(D - timedelta(days=i % 6), 9), status="booked")


def test_cost_per_booked_needs_five_bookings_and_fourteen_days(conn, live):
    df.seed_metric(conn, "biz_1", D, cost_usd=100)
    _booked(conn, 5)
    assert numbers(conn, weekly=True).week.rows[0].cost_per_booked_usd == 20.0
    conn.execute("UPDATE channel_campaign SET launched_at = %s", (local(D - timedelta(days=10), 9),))
    assert numbers(conn, weekly=True).week.rows[0].cost_per_booked_usd is None
    conn.execute("UPDATE channel_campaign SET launched_at = %s", (LAUNCHED,))
    conn.execute("UPDATE lead SET status = 'new' WHERE id = (SELECT min(id) FROM lead)")
    assert numbers(conn, weekly=True).week.rows[0].cost_per_booked_usd is None


def test_sample_words():
    assert digest_numbers.sample_word(29, 0) == "early: 29 leads"
    assert digest_numbers.sample_word(30, 0) == "solid: 30 leads"
    assert digest_numbers.sample_word(11, 10) == "solid: 11 leads"
    assert digest_numbers.sample_word(1, 0) == "early: 1 lead"


def test_best_channel_needs_a_solid_sample(conn, live):
    df.seed_channel(conn, "biz_1", "newsbreak", launched_at=LAUNCHED)
    for ch in ("google_search", "newsbreak"):
        df.seed_metric(conn, "biz_1", D, ch, cost_usd=300)
    for i in range(30):
        df.seed_lead(conn, "biz_1", local(D - timedelta(days=i % 6), 9))
    for i in range(5):
        df.seed_lead(conn, "biz_1", local(D - timedelta(days=i), 9), channel="newsbreak")
    assert numbers(conn, weekly=True).week.best_channel == "google_search"
    conn.execute("DELETE FROM lead WHERE id = (SELECT min(id) FROM lead WHERE channel = 'google_search')")
    assert numbers(conn, weekly=True).week.best_channel is None


def test_booked_reads_the_current_status(conn, live):
    lead = df.seed_lead(conn, "biz_1", local(D - timedelta(days=1), 9), status="won")
    assert numbers(conn, weekly=True).week.rows[0].booked == 1
    conn.execute("UPDATE lead SET status = 'lost' WHERE id = %s", (lead,))
    week = numbers(conn, weekly=True).week
    assert week.rows[0].booked == 0 and week.leads_by_status == {"lost": 1}


def test_state_texts_are_fixed_owner_sentences(conn, live):
    conn.execute(
        "UPDATE channel_campaign SET state = 'paused', paused_reason = 'month_cap', paused_at = %s, last_error = %s",
        (local(D, 9), "Google said: CAMPAIGN_BUDGET_EXHAUSTED"),
    )
    [row] = numbers(conn).rows
    assert row.state_text == "Paused until November 1: October's $600 is used"
    for (cls, state, reason), text in digest_numbers.STATE_TEXTS.items():
        assert copy.jargon(text) is None, text
    for reason in ("stop_requested", "dropped"):
        shown = digest_numbers.state_text(
            "active",
            {"state": "paused", "paused_reason": reason, "last_error": "X"},
            month="",
            next_month="",
            amount=None,
        )
        assert shown.startswith("Paused: ") and "X" not in shown and copy.jargon(shown) is None


def test_a_page_pause_has_its_own_owner_sentence():
    for reason in ("page_down", "unreachable"):
        text = digest_numbers.state_text(
            "active", {"state": "paused", "paused_reason": reason}, month="October", next_month="November", amount=None
        )
        assert text == "Paused: the page your ads send people to is not answering"


def test_a_running_row_has_no_state_text(conn, live):
    assert numbers(conn).rows[0].state_text is None


def test_numbers_hold_no_lead_fields(conn, live):
    df.seed_lead(conn, "biz_1", local(D, 9), fields={"name": "Ann Example", "phone": "650-555-0100"})
    shown = json.dumps(asdict(numbers(conn, weekly=True)), default=str)
    assert "Ann" not in shown and "650-555" not in shown
