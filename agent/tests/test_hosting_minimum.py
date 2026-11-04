"""agent/hosting_minimum.py, the rule (A3p-3 spec P12-P18; plan 2026-10-04-agent-a3p-3-1 Q1-Q3, Q8, Q16): what covers
a hosted site -- the ads live on a monthly budget of $200 or more across channels, or $200 spent in the last 30 days,
both ways of running the ads alike -- the business's calendar, when it is ready to come back, the operator's exemption
and its commands."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from psycopg.types.json import Jsonb

from agent import ads_run, config, google_ads_link, hosting_minimum, ops, wallet
from agent.channels import registry
from agent.tests.test_ads_run import (  # noqa: F401 (fixtures)
    BIZ,
    GOOGLE,
    add_plan,
    clock,
    sections,
    seed,
)

NEWSBREAK = "newsbreak"
HOST = "bright-smile-dental.business.test"
TODAY = date(2026, 10, 4)


def _at(day: date, clock_fn) -> None:
    clock_fn(datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC))


def _row(conn, channel: str, plan_id: str, *, state: str = "live", last_error: str | None = None, zone=None) -> None:
    """A channel row as a launch leaves it: built (`resources.campaign`), in `state`."""
    resources = {"campaign": f"campaigns/{channel}"} | ({"time_zone": zone} if zone else {})
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, last_error) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (BIZ, channel, plan_id, state, Jsonb(resources), last_error),
    )


def _spent(conn, channel: str, day: date, usd: float) -> None:
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'api')",
        (BIZ, channel, day, int(usd * 1_000_000)),
    )


def _two_channels(google_usd: float, newsbreak_usd: float) -> dict:
    secs = sections(monthly_usd=google_usd)
    nb = {"channel": NEWSBREAK, "monthly_usd": newsbreak_usd, "headlines": [], "descriptions": []}
    secs["ads"]["channels"].append(nb)
    return secs


# ---------------------------------------------------------------- (a) the ads live on $200 a month


def test_live_ads_on_200_a_month_cover_the_site(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    _key, plan_id = seed(conn, secs=sections(monthly_usd=200.0))
    _row(conn, GOOGLE, plan_id)
    cov = hosting_minimum.coverage(conn, BIZ)
    assert (cov.covered, cov.why, cov.live_usd, cov.spent_usd) == (True, "live_budget", 200.0, 0.0)


def test_live_ads_under_200_do_not_cover_it(conn, clock):  # noqa: F811 (fixture)
    """The own-account floor is $100 a month (cold-start spec): such a business is not covered by its budget alone
    (spec §10, the second risk)."""
    _at(TODAY, clock)
    _key, plan_id = seed(conn, secs=sections(monthly_usd=150.0))
    _row(conn, GOOGLE, plan_id)
    cov = hosting_minimum.coverage(conn, BIZ)
    assert (cov.covered, cov.why, cov.live_usd) == (False, "short", 150.0)


def test_the_budget_counts_across_channels_but_only_where_the_ads_run(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    _key, plan_id = seed(conn, secs=_two_channels(150.0, 100.0))
    _row(conn, GOOGLE, plan_id)
    _row(conn, NEWSBREAK, plan_id, state="waiting_account")
    assert hosting_minimum.coverage(conn, BIZ).live_usd == 150.0  # NewsBreak waits: not live
    conn.execute("UPDATE channel_campaign SET state = 'live' WHERE channel = %s", (NEWSBREAK,))
    cov = hosting_minimum.coverage(conn, BIZ)
    assert (cov.covered, cov.why, cov.live_usd) == (True, "live_budget", 250.0)


def test_ads_switched_off_on_the_platform_are_not_live(conn, clock):  # noqa: F811 (fixture)
    """A `limited` row serves with a problem (it counts); one the hourly read found switched off on the platform by
    hand (the adapter's `paused_elsewhere` note) does not."""
    _at(TODAY, clock)
    _key, plan_id = seed(conn, secs=sections(monthly_usd=300.0))
    _row(conn, GOOGLE, plan_id, state="limited", last_error="Google is holding back some of your ads")
    assert hosting_minimum.coverage(conn, BIZ).why == "live_budget"
    elsewhere = registry.adapter(GOOGLE).words.paused_elsewhere
    conn.execute("UPDATE channel_campaign SET last_error = %s", (elsewhere,))
    assert hosting_minimum.coverage(conn, BIZ).why == "short"
    conn.execute("UPDATE channel_campaign SET state = 'paused', last_error = NULL")
    assert hosting_minimum.coverage(conn, BIZ).why == "short"


def test_the_budget_is_the_approved_plans(conn, clock):  # noqa: F811 (fixture)
    """A draft at $1,000 a month covers nothing: the ads run on the approved plan's amounts."""
    _at(TODAY, clock)
    _key, plan_id = seed(conn, secs=sections(monthly_usd=120.0))
    _row(conn, GOOGLE, plan_id)
    add_plan(conn, sections(monthly_usd=1000.0), status="draft")
    assert hosting_minimum.coverage(conn, BIZ).live_usd == 120.0


# ---------------------------------------------------------------- (b) $200 spent in the last 30 days


def test_200_spent_in_30_days_covers_the_site_in_both_modes(conn, clock):  # noqa: F811 (fixture)
    """Spend is metric_daily over every channel: the owner's own Google Ads account and NewsBreak run from the balance
    alike (P13). Paused ads keep the site covered while the 30 days' spend reaches $200."""
    _at(TODAY, clock)
    seed(conn)
    _spent(conn, GOOGLE, TODAY - timedelta(days=3), 120.0)
    _spent(conn, NEWSBREAK, TODAY - timedelta(days=29), 80.0)
    cov = hosting_minimum.coverage(conn, BIZ)
    assert (cov.covered, cov.why, cov.spent_usd) == (True, "spend", 200.0)


def test_spend_older_than_30_days_does_not_count(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    seed(conn)
    _spent(conn, GOOGLE, TODAY - timedelta(days=30), 500.0)  # the window is the 30 days ending today
    _spent(conn, GOOGLE, TODAY - timedelta(days=1), 199.99)
    cov = hosting_minimum.coverage(conn, BIZ)
    assert (cov.covered, cov.spent_usd) == (False, 199.99)


def test_the_calendar_is_the_ads_accounts(conn, clock):  # noqa: F811 (fixture)
    """Dates are the business's own time zone, as in the ads' reports (spec §3.1): 03:00 UTC on Oct 5 is still Oct 4
    in Los Angeles; without a known zone, UTC."""
    clock(datetime(2026, 10, 5, 3, 0, tzinfo=UTC))
    _key, plan_id = seed(conn)
    assert hosting_minimum.today(conn, BIZ) == date(2026, 10, 5)
    _row(conn, GOOGLE, plan_id, zone="America/Los_Angeles")
    assert hosting_minimum.today(conn, BIZ) == date(2026, 10, 4)


# ---------------------------------------------------------------- ready to come back (P17)


def test_ready_needs_an_approved_200_plan_with_its_link(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    settings = config.load()
    seed(conn, secs=sections(monthly_usd=200.0))  # the owner's own account, linked
    assert hosting_minimum.ready(conn, settings, BIZ)
    google_ads_link.save(conn, BIZ, "1234567890", status="pending")
    registry.save_account(conn, BIZ, GOOGLE, mode="own_account", status="pending", external={"customer_id": "1"})
    assert not hosting_minimum.ready(conn, settings, BIZ)


def test_ready_is_never_a_draft_or_a_plan_under_200(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    settings = config.load()
    seed(conn, secs=sections(monthly_usd=150.0))
    assert hosting_minimum.ready_usd(conn, settings, BIZ) == 150.0 and not hosting_minimum.ready(conn, settings, BIZ)
    conn.execute("UPDATE plan SET status = 'draft'")
    assert hosting_minimum.ready_usd(conn, settings, BIZ) == 0.0


def test_ready_counts_a_managed_channel_only_with_funds(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    settings = config.load()
    seed(conn, secs=_two_channels(100.0, 100.0))  # Google Search on the owner's linked account
    registry.save_account(conn, BIZ, NEWSBREAK, mode="managed", status="active", external={"ad_account_id": "4001"})
    assert hosting_minimum.ready_usd(conn, settings, BIZ) == 100.0  # NewsBreak runs from the balance: none yet
    assert not hosting_minimum.ready(conn, settings, BIZ)
    wallet.add_entry(conn, BIZ, kind="topup", amount_cents=50_000, ref="pi_1", created_by="stripe")
    assert ads_run.account_status(conn, settings, BIZ, NEWSBREAK).ready
    assert hosting_minimum.ready_usd(conn, settings, BIZ) == 200.0 and hosting_minimum.ready(conn, settings, BIZ)


# ---------------------------------------------------------------- the operator's exemption (P18)


def _hosted_page(conn, *, ends: date | None = None) -> str:
    page_id = conn.execute("SELECT id FROM page WHERE business_id = %s", (BIZ,)).fetchone()["id"]
    conn.execute(
        "UPDATE page SET source = 'admiry', hostname = %s, url = %s, status = 'published', countdown_ends = %s, "
        "countdown_mail = %s WHERE id = %s",
        (HOST, f"https://{HOST}/", ends, Jsonb({"start": "2026-10-01"}) if ends else None, page_id),
    )
    return page_id


def test_an_exemption_covers_the_site_and_clears_its_countdown(conn, clock):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    seed(conn)
    page_id = _hosted_page(conn, ends=TODAY + timedelta(days=20))
    assert hosting_minimum.set_exempt(conn, BIZ, "the pilot", by="ops:cli")
    cov = hosting_minimum.coverage(conn, BIZ)
    assert (cov.covered, cov.why) == (True, "exempt")
    h = hosting_minimum.hosting_row(conn, page_id)
    assert (h["countdown_ends"], h["countdown_mail"]) == (None, None)
    assert hosting_minimum.exemption(conn, BIZ)["reason"] == "the pilot"
    assert hosting_minimum.clear_exempt(conn, BIZ) and hosting_minimum.exemption(conn, BIZ) is None
    assert not hosting_minimum.set_exempt(conn, "biz_nope", "x", by="ops:cli")


def test_the_ops_commands_exempt_audit_and_show(conn, clock, capsys):  # noqa: F811 (fixture)
    _at(TODAY, clock)
    seed(conn)
    _hosted_page(conn, ends=TODAY + timedelta(days=9))
    assert ops.main(["hosting-exempt", BIZ, "--reason", "a partner"]) == 0
    assert hosting_minimum.exemption(conn, BIZ)["by"] == "ops:cli"
    assert ops.main(["hosting", BIZ]) == 0
    shown = capsys.readouterr().out
    assert "covered yes (exempt)" in shown and "exempt a partner" in shown and f"{HOST}" in shown
    assert "countdown ends -" in shown  # the exemption cleared it
    assert ops.main(["hosting-exempt", BIZ, "--clear"]) == 0
    assert hosting_minimum.exemption(conn, BIZ) is None
    rows = conn.execute(
        "SELECT result FROM agent_action WHERE tool = 'ops.hosting_exempt' AND business_id = %s ORDER BY id", (BIZ,)
    ).fetchall()
    assert [r["result"]["note"] for r in rows] == [
        hosting_minimum.NOTE_EXEMPT.format(reason="a partner"),
        hosting_minimum.NOTE_UNEXEMPT,
    ]
    assert ops.main(["hosting-exempt", BIZ, "--reason", "  "]) == 1
    assert ops.main(["hosting-exempt", "biz_nope", "--clear"]) == 1


def test_the_owner_sentences_are_the_specs_and_plain():
    from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON

    texts = [
        hosting_minimum.MINIMUM,
        hosting_minimum.START_BY,
        hosting_minimum.LAPSED,
        hosting_minimum.STAYS_UNTIL,
        hosting_minimum.KEPT,
        hosting_minimum.OFFLINE,
        hosting_minimum.COMEBACK,
        hosting_minimum.NOTE_START,
        hosting_minimum.NOTE_REMINDER,
        hosting_minimum.NOTE_KEPT,
        hosting_minimum.NOTE_DOWN,
        hosting_minimum.NOTE_PURGED,
        hosting_minimum.NOTE_EXEMPT,
        hosting_minimum.NOTE_UNEXEMPT,
    ]
    for text in texts:
        assert not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text)), text
    assert hosting_minimum.MINIMUM == (
        "Hosting is free while your ads with Admiry spend at least $200 a month. Without that, the site comes down 30 "
        "days later. We remind you first."
    )
    assert hosting_minimum.day_text(date(2026, 11, 3)) == "November 3, 2026"
