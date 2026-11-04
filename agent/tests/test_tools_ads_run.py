"""The owner-facing tools over the ads lifecycle that stay in agent/tools_ads_run.py (A3 Task 5; Review Focus 4): get_report,
pause, resume. get_channel_status and get_status_issues moved to tools_channels.py with their tests (M0 Task 6)."""

from __future__ import annotations

from datetime import timedelta

from agent import ads_run, auth, google_ads_link
from agent.channels.google_search import adapter as google
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_ads_run import BIZ, CID, gads, launched, live, row  # noqa: F401
from agent.tests.test_ads_sync import add_days


def plain(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


# ---------------------------------------------------------------- get_report


def test_get_report_7d_carries_days_and_totals(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    day = ads_run.account_today(row(conn))
    add_days(conn, (day, 2000000, 1, 10, 0, 0), (day - timedelta(days=1), 3000000, 2, 20, 1, 1))
    res = call_tool(client, "get_report", {"period": "7d"}, key=key)
    assert not res["isError"], tool_text(res)
    d = data(res)
    assert d["period"] == "7d" and len(d["days"]) == 7  # zero-filled (Fix round 1): always exactly `limit` rows
    assert d["totals"] == {"spent_usd": 5.0, "clicks": 3, "impressions": 30, "calls": 1, "conversions": 1.0}
    assert d["planning"] is None  # seed()'s plan carries no estimate.columns (a bare fixture, not a consult)
    assert plain(res["structuredContent"]["say"])


def test_get_report_rejects_an_unknown_period(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    res = call_tool(client, "get_report", {"period": "9d"}, key=key)
    assert res["isError"] and "period" in tool_text(res)


def test_get_report_month_is_the_statement(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    add_days(conn, (ads_run.account_today(row(conn)).replace(day=1), 4000000, 2, 30, 1, 1))
    res = call_tool(client, "get_report", {"period": "month"}, key=key)
    d = data(res)
    assert d["period"] == "month" and d["statement"] == ads_run.statement(conn, BIZ)  # one entry per channel row
    assert d["statement"]["channels"][0]["account_public"] == google_ads_link.format_customer_id(CID)
    assert d["statement"]["no_fee_notice"] == google.NO_FEE
    assert res["structuredContent"]["say"] == (
        f"$4 spent so far this month on Google Search. {google.NO_FEE}."
    )  # A3's words, from the Google Search entry
    assert "no management fee" in res["structuredContent"]["say"]
    assert plain(res["structuredContent"]["say"])


def test_get_report_month_names_every_channels_spend(client, conn, gads, live):  # noqa: F811 (fixture)
    """Fix round 1 (a): the month's sentence names each channel's spend, A3's words for the first one kept."""
    key, plan_id = launched(conn, gads)
    first = ads_run.account_today(row(conn)).replace(day=1)
    add_days(conn, (first, 842_000_000, 2, 30, 1, 1))
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, 'newsbreak', %s, 'live')",
        (BIZ, plan_id),
    )
    add_days(conn, (first, 120_000_000, 0, 0, 0, 0), channel="newsbreak")
    res = call_tool(client, "get_report", {"period": "month"}, key=key)
    say = res["structuredContent"]["say"]
    assert say.startswith("$842 spent so far this month on Google Search, $120 on NewsBreak. ")
    assert [c["label"] for c in data(res)["statement"]["channels"]] == ["Google Search", "NewsBreak"]
    assert plain(say)


def test_get_report_without_any_ads_reports_zeros_and_no_statement(client, conn):
    key = business_with_key(conn)
    res = call_tool(client, "get_report", {"period": "7d"}, key=key)
    d = data(res)
    assert len(d["days"]) == 7 and all(  # zero-filled (Fix round 1): the period, not the absence of it
        day == {"day": day["day"], "spent_usd": 0.0, "clicks": 0, "impressions": 0, "calls": 0, "conversions": 0.0}
        for day in d["days"]
    )
    assert d["totals"] == {"spent_usd": 0.0, "clicks": 0, "impressions": 0, "calls": 0, "conversions": 0.0}
    res = call_tool(client, "get_report", {"period": "month"}, key=key)
    assert data(res)["statement"] is None
    assert res["structuredContent"]["say"] == "No Google Ads account is linked yet."


def test_get_report_month_for_a_linked_business_not_launched_yet_says_so(client, conn):
    """A3 final review: a linked account with no ads row is not "no account linked"."""
    key = business_with_key(conn)
    google_ads_link.save(conn, "biz_1", CID, status="pending")
    res = call_tool(client, "get_report", {"period": "month"}, key=key)
    assert res["structuredContent"]["say"] == "No Google Ads account is linked yet."  # an invitation is not a link
    google_ads_link.save(conn, "biz_1", CID, status="active")
    res = call_tool(client, "get_report", {"period": "month"}, key=key)
    assert data(res)["statement"] is None
    assert res["structuredContent"]["say"] == "Your ads have not launched yet." and plain(tool_text(res))


# ---------------------------------------------------------------- pause / resume (Review Focus 4)


def test_pause_needs_manage_scope(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    read_only = business_with_key(conn, scopes=("read",), biz_id=BIZ)
    res = call_tool(client, "pause", {"idempotency_key": "p1"}, key=read_only)
    assert res["isError"] and "scope" in tool_text(res)
    assert row(conn)["state"] == "live"  # refused before it ever touched the row
    res = call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert row(conn)["state"] == "paused"


def test_pause_is_never_refused_by_the_hourly_change_limit(client, conn, gads, live, monkeypatch):  # noqa: F811 (fixture)
    """A3 final review: stopping spend is always allowed -- like revoke_key, pause is exempt from the per-business
    change limit another key may have used up."""
    key, _plan_id = launched(conn, gads)
    monkeypatch.setattr(auth, "MUTATIONS_PER_HOUR", 2)
    for i in range(2):
        res = call_tool(
            client, "update_business", {"patch": {"ai_crawlers": bool(i)}, "idempotency_key": f"u{i}"}, key=key
        )
        assert not res["isError"], tool_text(res)
    capped = call_tool(client, "update_business", {"patch": {"ai_crawlers": False}, "idempotency_key": "u9"}, key=key)
    assert capped["isError"] and "changes per hour per business" in tool_text(capped)
    res = call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert row(conn)["state"] == "paused" and gads.kinds().count("pause") == 1


def test_pause_is_idempotent_by_key_and_needs_no_approval(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    first = call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    again = call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    assert again["structuredContent"] == first["structuredContent"]  # replayed, not re-run
    assert gads.kinds().count("pause") == 1


def test_resume_needs_manage_scope_and_restarts_the_ads(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    read_only = business_with_key(conn, scopes=("read",), biz_id=BIZ)
    res = call_tool(client, "resume", {"idempotency_key": "r1"}, key=read_only)
    assert res["isError"] and "scope" in tool_text(res)
    assert row(conn)["state"] == "paused"
    res = call_tool(client, "resume", {"idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert row(conn)["state"] == "live"
    assert plain(res["structuredContent"]["say"])


def test_resume_without_a_pause_is_refused(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    res = call_tool(client, "resume", {"idempotency_key": "r1"}, key=key)
    assert res["isError"] and "not paused" in tool_text(res).lower()
