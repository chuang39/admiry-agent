"""ads_run.sync and the ads-sync job (spec 2026-09-28 admiry-google-search-execution-design.md §6; A3 Task 5),
against the same stateful FakeGoogle test_ads_run.py's launch tests use, extended with the three sync queries
(FakeGoogle.day_metrics/campaign_status/ad_policy; test_ads_run.SEARCHES) and the account's time zone. The clock is
pinned (`clock`, ruling S14): the fake answers a metrics search only for the three days it names."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent import ads_launch, ads_mode, ads_run, config, db, google_ads, google_ads_link, tick
from agent.channels.google_search import adapter as google_search
from agent.channels.newsbreak import adapter as nb_adapter
from agent.tests.conftest import ADS_SECRETS, TEST_DB_URL, call_tool
from agent.tests.test_ads_run import (
    BIZ,
    CID,
    GOOGLE,
    REAL_CLIENT,
    _row_for,
    add_plan,
    approve,
    clock,  # noqa: F401 -- pytest fixture used by name
    gads,  # noqa: F401 -- pytest fixture used by name
    google_error,
    launched,
    live,  # noqa: F401 -- pytest fixture used by name
    plain,
    queued,
    row,
    sections,
    seed,
)

# 13:00 in Palo Alto on 2026-09-27 (the fake account's zone is America/Los_Angeles): the sync's three days are
# 2026-09-25 .. 2026-09-27, DAY1 .. DAY3
NOON_27TH = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _pinned(clock):  # noqa: F811 (fixture)
    clock(NOON_27TH)


DAY1 = {
    "segments": {"date": "2026-09-25"},
    "metrics": {"costMicros": "5000000", "clicks": "3", "impressions": "40", "conversions": 1, "phoneCalls": "1"},
}
DAY2 = {
    "segments": {"date": "2026-09-26"},
    "metrics": {"costMicros": "6500000", "clicks": "4", "impressions": "50", "conversions": 0.5, "phoneCalls": "0"},
}
DAY3 = {
    "segments": {"date": "2026-09-27"},
    "metrics": {"costMicros": "7250000", "clicks": "5", "impressions": "60", "conversions": 2, "phoneCalls": "2"},
}


def day_rows(conn) -> list[dict]:
    return conn.execute(
        "SELECT day, cost_micros, clicks, impressions, calls, conversions FROM metric_daily "
        "WHERE business_id = %s AND channel = %s ORDER BY day",
        (BIZ, GOOGLE),
    ).fetchall()


def add_days(conn, *days: tuple, channel: str = GOOGLE) -> None:
    """metric_daily rows of BIZ, each (day, cost_micros, clicks, impressions, calls, conversions), as ads-sync writes
    them (leads and booked are the lead store's; none here)."""
    for day, cost_micros, clicks, impressions, calls, conversions in days:
        conn.execute(
            "INSERT INTO metric_daily (business_id, channel, day, cost_micros, clicks, impressions, calls, leads, "
            "conversions, booked, source) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, 0, 'api')",
            (BIZ, channel, day, cost_micros, clicks, impressions, calls, conversions),
        )


# ---------------------------------------------------------------- metric_daily (the metrics query)


def test_sync_writes_three_metric_daily_rows_and_stays_live(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.day_metrics = [DAY1, DAY2, DAY3]
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live" and out["ops"] == 4 and out["last_error"] is None  # the time zone, read once
    assert gads.metric_days == [("2026-09-25", "2026-09-27")]
    days = day_rows(conn)
    assert [str(d["day"]) for d in days] == ["2026-09-25", "2026-09-26", "2026-09-27"]
    assert days[0]["cost_micros"] == 5_000_000 and days[0]["clicks"] == 3 and days[0]["impressions"] == 40
    assert days[0]["calls"] == 1 and float(days[0]["conversions"]) == 1.0
    assert days[1]["cost_micros"] == 6_500_000 and float(days[1]["conversions"]) == 0.5
    assert gads.kinds() == ["time_zone", "metrics", "policy", "status"]
    assert row(conn)["synced_at"] is not None and row(conn)["resources"]["time_zone"] == "America/Los_Angeles"
    gads.sent.clear()
    assert ads_run.sync(conn, config.load(), BIZ, GOOGLE)["ops"] == 3  # kept: never asked again
    assert gads.kinds() == ["metrics", "policy", "status"]


def test_sync_names_the_accounts_own_three_days_and_keys_them_by_its_calendar(conn, gads, live, clock):  # noqa: F811 (fixture)
    """Ruling S14 (A3 final review C2): GAQL has no `LAST_3_DAYS`; the three days are named, ending on today in the
    account's own time zone. At 23:30 in Palo Alto on the 29th it is already the 30th in UTC: Google's own days are
    the 27th to the 29th, the day written is the 29th, and today's spend is the 29th's."""
    launched(conn, gads)
    gads.sent.clear()
    clock(datetime(2026, 9, 30, 6, 30, tzinfo=UTC))  # 23:30 PDT on 2026-09-29
    gads.day_metrics = [
        {**DAY1, "segments": {"date": d}, "metrics": {**DAY1["metrics"], "costMicros": str(n * 1_000_000)}}
        for n, d in ((1, "2026-09-27"), (2, "2026-09-28"), (3, "2026-09-29"))
    ]
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    query = gads.bodies("metrics")[0]["query"]
    assert "segments.date BETWEEN '2026-09-27' AND '2026-09-29'" in query and "DURING" not in query
    assert gads.metric_days == [("2026-09-27", "2026-09-29")]
    assert [str(d["day"]) for d in day_rows(conn)] == ["2026-09-27", "2026-09-28", "2026-09-29"]  # never the 30th
    assert ads_run.account_today(row(conn)).isoformat() == "2026-09-29"
    assert ads_run.today_spend(conn, BIZ, GOOGLE, row(conn))["spent_usd"] == 3.0  # the 29th's, not an empty 30th
    assert [d["day"] for d in ads_run.day_rows(conn, BIZ, GOOGLE, 3, row(conn))] == [
        "2026-09-29",
        "2026-09-28",
        "2026-09-27",
    ]


def test_sync_reads_the_time_zone_the_launch_already_kept(conn, gads, live):  # noqa: F811 (fixture)
    """Ruling S14: the conversion setup's own customer read (a launch whose actions were missing) keeps the zone,
    so the first sync asks Google nothing more; an unknown zone reads as UTC."""
    _key, plan_id = seed(conn, conversions=False)
    queued(conn, plan_id)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    assert row(conn)["resources"]["time_zone"] == "America/Los_Angeles"
    gads.sent.clear()
    assert ads_run.sync(conn, config.load(), BIZ, GOOGLE)["ops"] == 3 and "time_zone" not in gads.kinds()
    assert ads_run.account_today(row(conn)).isoformat() == "2026-09-27"
    unknown = {"resources": {"time_zone": "Mars/Olympus_Mons"}}
    assert ads_run.account_today(unknown) == ads_run.account_today(None) == NOON_27TH.date()


def test_sync_upserts_the_same_day_rather_than_duplicating_it(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.day_metrics = [DAY3]
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    gads.day_metrics = [{**DAY3, "metrics": {**DAY3["metrics"], "costMicros": "9000000", "clicks": "9"}}]
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    days = day_rows(conn)
    assert len(days) == 1 and days[0]["cost_micros"] == 9_000_000 and days[0]["clicks"] == 9


# ---------------------------------------------------------------- state mapping (the status query)


def test_sync_maps_other_primary_status_to_limited_with_reasons_in_issues(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.campaign_status = {"status": "PAUSED", "primaryStatus": "LIMITED", "primaryStatusReasons": ["MISCONFIGURED"]}
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "limited" and out["last_error"] is None
    assert row(conn)["resources"]["issues"]["campaign"] == ["MISCONFIGURED"]


def test_sync_maps_primary_paused_to_limited_when_admiry_did_not_pause_it(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.campaign_status = {"status": "PAUSED", "primaryStatus": "PAUSED"}
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "limited" and out["last_error"] == google_search.PAUSED_IN_GOOGLE
    gads.campaign_status = {"status": "ENABLED", "primaryStatus": "ELIGIBLE"}  # resumed in Google Ads
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live" and out["last_error"] is None  # ruling S15: sync clears its own note


PAUSED_THERE = "Someone paused your ads in Google Ads; resume them there"
# what main's sync wrote: "or tell Admiry to", while Admiry's resume answers "Your ads are not paused" for such a row
PAUSED_THERE_BEFORE = "Someone paused your ads in Google Ads; resume them there, or tell Admiry to"


def test_a_new_mismatch_writes_the_sentence_that_sends_the_owner_to_google(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.campaign_status = {"status": "PAUSED", "primaryStatus": "PAUSED"}
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], out["last_error"]) == ("limited", PAUSED_THERE)
    assert google_search.PAUSED_IN_GOOGLE == PAUSED_THERE


@pytest.mark.parametrize(
    ("google_says", "after"),
    [
        ({"status": "ENABLED", "primaryStatus": "ELIGIBLE"}, ("live", None)),  # resumed in Google Ads: cleared
        ({"status": "PAUSED", "primaryStatus": "PAUSED"}, ("limited", PAUSED_THERE)),  # still paused: the new words
    ],
)
def test_a_row_carrying_the_old_sentence_is_still_syncs_own_to_clear(conn, gads, live, google_says, after):  # noqa: F811 (fixture)
    """A row the old sync left with its old sentence is sync's own note still (ruling S15): the next sync clears it, or
    writes the new sentence over it -- never keeps it as another step's error."""
    launched(conn, gads)
    gads.sent.clear()
    conn.execute(
        "UPDATE channel_campaign SET state = 'limited', last_error = %s WHERE business_id = %s",
        (PAUSED_THERE_BEFORE, BIZ),
    )
    gads.campaign_status = google_says
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], out["last_error"]) == after


def test_sync_recovers_a_limited_row_and_clears_its_stale_issues(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.campaign_status = {"status": "PAUSED", "primaryStatus": "LIMITED", "primaryStatusReasons": ["MISCONFIGURED"]}
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert row(conn)["state"] == "limited"
    gads.campaign_status = {"status": "ENABLED", "primaryStatus": "ELIGIBLE"}
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live" and out["last_error"] is None
    assert "issues" not in row(conn)["resources"]


def test_a_refused_change_is_still_said_after_three_syncs(client, conn, gads, live):  # noqa: F811 (fixture)
    """Ruling S15 (A3 final review I2): sync sets and clears only its own notes -- a refused update's sentence stays,
    and the owner keeps reading that the change was not taken, and what to do, while the refused mark stands."""
    key, v1 = launched(conn, gads)
    gads.fail["money"] = google_error(400, {"campaignBudgetError": "NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT"})
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    approve(client, conn, key, v2)
    refusal = google_ads.REFUSED_WITH_REASON.format("campaignBudgetError: NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT")
    assert (row(conn)["last_error"], row(conn)["resources"]["refused_plan_id"]) == (refusal, v2)
    for _ in range(3):
        assert ads_run.sync(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    r = row(conn)
    assert (r["plan_id"], r["last_error"], r["resources"]["refused_plan_id"]) == (v1, refusal, v2)
    sentence = ads_run.status_sentence(r)
    assert sentence == f"Google Search: live. {google_search.REFUSED_CHANGE}. {refusal}." and plain(sentence)
    (google_row,) = call_tool(client, "get_channel_status", {}, key=key)["structuredContent"]["data"]["channels"]
    assert google_search.REFUSED_CHANGE in google_row["sentence"]  # M0 Task 6: one row per channel


def test_a_pause_that_lands_while_sync_reads_google_is_never_written_over(conn, gads, live):  # noqa: F811 (fixture)
    """Ruling S17: `pause` never waits for sync's lock; sync's own write never undoes it."""
    launched(conn, gads)
    metrics = gads._metrics

    def pause_meanwhile(body):
        with db.connect(TEST_DB_URL) as other:
            ads_run.pause(other, config.load(), BIZ, GOOGLE, by="owner")  # the lock is sync's: recorded without it
        return metrics(body)

    gads._metrics = pause_meanwhile
    ads_run.sync(
        conn, config.load(), BIZ, GOOGLE
    )  # Google still says ENABLED + ELIGIBLE: sync alone would write `live`
    r = row(conn)
    assert (r["state"], r["last_error"], r["resources"]["pause_owed"]) == ("paused", google_search.PAUSE_OWED, True)
    assert r["synced_at"] is not None  # the rest of the sync still landed


def test_sync_never_moves_a_paused_row_that_still_owes_google_a_pause(conn, gads, live):  # noqa: F811 (fixture)
    """Ruling S11: an owed pause is the sweeper's alone to finish -- sync must never touch state or last_error,
    and skips the status query too (nothing here is sync's to resolve)."""
    launched(conn, gads)
    gads.sent.clear()
    conn.execute(
        "UPDATE channel_campaign SET state = 'paused', last_error = %s, resources = resources || %s "
        "WHERE business_id = %s",
        (google_search.PAUSE_OWED, Jsonb({"paused_by": "assistant", "pause_owed": True}), BIZ),
    )
    gads.campaign_status = {"status": "ENABLED", "primaryStatus": "ELIGIBLE"}  # Google still thinks it is on
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "paused" and out["last_error"] == google_search.PAUSE_OWED and out["ops"] == 3
    assert row(conn)["resources"]["pause_owed"] is True
    assert gads.kinds() == ["time_zone", "metrics", "policy"]  # no status query


def test_sync_keeps_a_paused_row_paused_when_google_agrees(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")  # the `live` switch: Google is told directly
    assert row(conn)["state"] == "paused" and not row(conn)["resources"].get("pause_owed")
    gads.campaign_status = {"status": "PAUSED", "primaryStatus": "PAUSED"}
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "paused" and out["last_error"] is None and out["ops"] == 4


def test_sync_flags_enabled_in_google_without_ever_going_live_again(conn, gads, live):  # noqa: F811 (fixture)
    """Ruling S12: someone enabled the ads back in Google Ads; Admiry's own pause still stands."""
    launched(conn, gads)
    gads.sent.clear()
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    gads.campaign_status = {"status": "ENABLED", "primaryStatus": "ELIGIBLE"}
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "paused" and out["last_error"] == google_search.ENABLED_IN_GOOGLE


def test_sync_skips_the_status_query_for_a_still_launching_row(conn, gads, live):  # noqa: F811 (fixture)
    """A `launching` row's real Google status is normally PAUSED by design (P7) until its own `_enable` step;
    sync never reclassifies it as `limited` for that -- it just leaves it be, and skips asking."""
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    built = {"campaign": f"customers/{CID}/campaigns/1", "campaign_budget": f"customers/{CID}/campaignBudgets/1"}
    conn.execute(
        "UPDATE channel_campaign SET state = 'launching', resources = %s WHERE business_id = %s", (Jsonb(built), BIZ)
    )
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "launching" and out["ops"] == 3
    assert gads.kinds() == ["time_zone", "metrics", "policy"]


def test_sync_sends_nothing_when_the_switch_is_off(conn):
    """Global Constraint: with the switch `off`, no Google call happens anywhere except `agent.ops ads-probe` --
    `sync` gates on it itself (Fix round 1), the same as `launch`/`pause`/`resume`, rather than relying on
    `jobs.ads_sync`'s own outer check alone."""
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    built = {"campaign": f"customers/{CID}/campaigns/1", "campaign_budget": f"customers/{CID}/campaignBudgets/1"}
    conn.execute(
        "UPDATE channel_campaign SET state = 'live', resources = %s WHERE business_id = %s", (Jsonb(built), BIZ)
    )
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={}))
    with REAL_CLIENT(transport=transport) as http:
        out = ads_run.sync(conn, config.load(), BIZ, GOOGLE, http=http)
    assert out == {
        "state": "live",
        "say": ads_run.status_sentence(row(conn)),
        "ops": 0,
        "plan_id": plan_id,
        "last_error": None,
    }
    assert requests == []


# ---------------------------------------------------------------- ad disapprovals (the policy query)


def test_sync_stores_and_then_clears_disapproved_ad_issues(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.ad_policy = [
        {
            "adGroupAd": {
                "resourceName": f"customers/{CID}/adGroupAds/1~1",
                "policySummary": {
                    "approvalStatus": "DISAPPROVED",
                    "reviewStatus": "REVIEWED",
                    "policyTopicEntries": [{"topic": "Alcohol", "type": "PROHIBITED"}],
                },
            }
        },
        {  # an approved ad never becomes an issue
            "adGroupAd": {
                "resourceName": f"customers/{CID}/adGroupAds/2~1",
                "policySummary": {"approvalStatus": "APPROVED", "reviewStatus": "REVIEWED", "policyTopicEntries": []},
            }
        },
    ]
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live"
    assert row(conn)["resources"]["issues"]["ads"] == [
        {"resource_name": f"customers/{CID}/adGroupAds/1~1", "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}]}
    ]
    gads.ad_policy = []  # Google approved it since
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert "issues" not in row(conn)["resources"]


# ---------------------------------------------------------------- nothing to sync, busy, and failures


def test_sync_does_nothing_for_a_row_with_no_stored_campaign(conn, gads):  # noqa: F811 (fixture)
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "queued" and out["ops"] == 0
    assert gads.kinds() == []


def test_sync_does_nothing_without_a_row_at_all(conn, gads):  # noqa: F811 (fixture)
    out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] is None and out["ops"] == 0


def test_a_sync_while_a_launch_step_runs_is_told_it_is_busy(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"ads:{BIZ}",))
        out = ads_run.sync(conn, config.load(), BIZ, GOOGLE)
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"ads:{BIZ}",))
    assert out.get("busy") is True and gads.kinds() == []


def test_sync_propagates_a_google_failure_for_the_job_to_isolate(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.fail["metrics"] = google_error(403)
    with pytest.raises(google_ads.AdsError):
        ads_run.sync(conn, config.load(), BIZ, GOOGLE)


# ---------------------------------------------------------------- the ads-sync job


def test_ads_sync_job_syncs_every_eligible_state_but_never_queued_or_failed(conn, gads, live):  # noqa: F811 (fixture)
    built = {"campaign": f"customers/{CID}/campaigns/1", "campaign_budget": f"customers/{CID}/campaignBudgets/1"}
    for biz, state in (("biz_a", "live"), ("biz_b", "paused"), ("biz_c", "limited"), ("biz_d", "launching")):
        _row_for(conn, biz, state, resources=built)
    _row_for(conn, "biz_e", "queued")  # no campaign yet: never synced
    _row_for(conn, "biz_f", "failed", resources=built)  # a refused request is never retried
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert out["checked"] == 4 and out["errors"] == 0
    assert ads_run.get(conn, "biz_e", GOOGLE)["state"] == "queued"
    assert ads_run.get(conn, "biz_f", GOOGLE)["state"] == "failed"


def test_ads_sync_sends_nothing_when_the_switch_is_off(conn, gads):  # noqa: F811 (fixture)
    _row_for(conn, "biz_a", "live", resources={"campaign": "x", "campaign_budget": "y"})
    assert tick.run("ads-sync", url=TEST_DB_URL) == {
        "modes": {},
        "checked": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": {},
        "states": {},
    }
    assert gads.requests == []


def test_ads_sync_stops_for_the_day_at_the_ops_budget(conn, monkeypatch, live):  # noqa: F811 (fixture)
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "5")
    built = {"campaign": f"customers/{CID}/campaigns/1", "campaign_budget": f"customers/{CID}/campaignBudgets/1"}
    for biz in ("biz_a", "biz_b", "biz_c"):
        _row_for(conn, biz, "live", resources=built)
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-launch', now(), true, '{\"ops\": 3}')"
    )
    calls: list[str] = []

    def fake_sync(conn_, settings, biz, channel, *, http=None):
        calls.append(biz)
        return {"state": "live", "ops": 2}

    monkeypatch.setattr(ads_run, "sync", fake_sync)
    out = tick.run("ads-sync", url=TEST_DB_URL)  # 3 already sent; biz_a's 2 reaches 5 >= 5: stop before biz_b
    assert calls == ["biz_a"] and out["ops"] == 2 and out["checked"] == 1 and out["stopped"] == "ops budget"


def test_ads_sync_job_isolates_a_failing_business_and_leaks_no_secret(conn, gads, live, caplog):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    gads.fail["metrics"] = google_error(403)  # its default message carries the secret markers below
    with caplog.at_level(logging.INFO):
        out = tick.run("ads-sync", url=TEST_DB_URL)
    assert out["errors"] == 1 and out["checked"] == 0
    assert row(conn)["state"] == "live"  # untouched: the failure never reached the row
    stored = " ".join(
        [
            str(row(conn)),
            str(conn.execute("SELECT details FROM job_run").fetchall()),
            " ".join(rec.getMessage() for rec in caplog.records),
        ]
    )
    assert not any(v in stored for v in ADS_SECRETS) and "ya29.SECRET" not in stored and "raw words" not in stored


# ---------------------------------------------------------------- the reporting helpers (get_report, get_channel_status,
# /connect's statement)


def test_scaled_planning_scales_pro_rata_to_the_period():
    plan = {
        "sections": {
            "estimate": {
                "columns": {
                    "planning": {
                        "spend": 1000,
                        "leads": [10, 20],
                        "bookings": [5, 10],
                        "customers": [2, 4],
                        "revenue": [1000, 2000],
                    }
                }
            }
        }
    }
    scaled = ads_run.scaled_planning(plan, 7)
    assert scaled["spend"] == round(1000 * 7 / 30, 2)
    assert scaled["leads"] == [round(10 * 7 / 30, 1), round(20 * 7 / 30, 1)]
    assert scaled["revenue"] == [round(1000 * 7 / 30), round(2000 * 7 / 30)]
    assert ads_run.scaled_planning({"sections": {"estimate": {}}}, 7) is None


def test_statement_sums_this_and_last_month_and_zero_fills_a_quiet_one(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    today = ads_run.account_today(row(conn))
    add_days(conn, (today.replace(day=1), 4000000, 2, 30, 1, 1))
    st = ads_run.statement(conn, BIZ)
    assert [(c["channel"], c["label"]) for c in st["channels"]] == [(GOOGLE, "Google Search")]  # one per channel row
    google = st["channels"][0]
    assert google["account_public"] == google_ads_link.format_customer_id(CID)
    assert st["no_fee_notice"] == google_search.NO_FEE
    assert google["months"][0]["month"] == today.strftime("%B %Y") and google["months"][0]["spent_usd"] == 4.0
    assert google["months"][0]["clicks"] == 2 and google["months"][0]["calls"] == 1
    assert google["months"][1]["spent_usd"] == 0.0 and google["months"][1]["clicks"] == 0  # last month: zero-filled
    assert ads_run.statement(conn, "biz_none") is None  # no channel row: nothing to show an account for


def test_the_statement_has_an_entry_for_every_channel_with_a_row_and_counts_only_its_own_days(conn, gads, live):  # noqa: F811 (fixture)
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, 'newsbreak', %s, 'queued')",
        (BIZ, plan_id),
    )
    today = ads_run.account_today(row(conn))
    add_days(conn, (today, 4000000, 2, 30, 1, 1))
    add_days(conn, (today, 9000000, 7, 90, 0, 3), channel="newsbreak")
    st = ads_run.statement(conn, BIZ)
    assert [(c["channel"], c["label"], c["months"][0]["spent_usd"]) for c in st["channels"]] == [
        (GOOGLE, "Google Search", 4.0),
        ("newsbreak", "NewsBreak", 9.0),
    ]
    assert st["channels"][1]["account_public"] is None  # no Google Ads link stands for NewsBreak's account
    assert ads_run.week_totals(conn, BIZ, GOOGLE, row(conn)) == {"clicks": 2, "calls": 1, "leads": 1}
    assert ads_run.day_rows(conn, BIZ, "newsbreak", 1, row(conn))[0]["spent_usd"] == 9.0


@pytest.mark.parametrize(
    ("link", "google_says"),
    [
        ("active", "Google bills this account directly; Admiry charges no management fee"),
        (None, "Google Search's cost is taken from your balance at cost; Admiry charges no fee"),
    ],
    ids=["its own account", "Admiry-run"],
)
def test_the_statements_billing_sentence_follows_the_way_each_channel_runs(conn, link, google_says):
    """M1 follow-up 2, W2 (found by the G1 session): the monthly statement's billing sentence is the account's way's --
    the owner's own account: the channel's own words (Google bills it directly); Admiry-run (`managed`, paid from the
    balance): its cost is taken from the balance at cost -- so a prepaid business never reads that Google bills it.
    NewsBreak's, Admiry-run always, reads as it did."""
    _key, plan_id = seed(conn, link=link)
    if link is None:
        ads_mode.change(conn, BIZ, "managed")  # prepaid: Google Search's account is Admiry's to open (A3c)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, %s), "
        "(%s, 'newsbreak', %s, 'waiting_account')",
        (BIZ, GOOGLE, plan_id, "live" if link else "waiting_account", BIZ, plan_id),
    )
    newsbreak_says = "NewsBreak's cost is taken from your balance at cost; Admiry charges no fee"
    assert ads_run.statement(conn, BIZ)["no_fee_notice"] == f"{google_says}; {newsbreak_says}"


def test_today_spend_and_week_totals(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    day = ads_run.account_today(row(conn))
    add_days(  # the last: eight days back
        conn,
        (day, 7800000, 5, 80, 2, 2),
        (day - timedelta(days=1), 6000000, 4, 60, 1, 1),
        (day - timedelta(days=7), 1, 100, 1, 100, 100),
    )
    today = ads_run.today_spend(conn, BIZ, GOOGLE, row(conn))
    expected_budget = google_ads.from_micros(ads_launch.daily_micros(1000.0))
    assert today == {"spent_usd": 7.8, "budget_usd": expected_budget}
    week = ads_run.week_totals(conn, BIZ, GOOGLE, row(conn))
    assert week == {"clicks": 9, "calls": 3, "leads": 3}
    assert ads_run.today_spend(conn, BIZ, GOOGLE, None) is None


def test_day_rows_and_totals_are_newest_first_and_limit_respected(conn, gads, live):  # noqa: F811 (fixture)
    launched(conn, gads)
    gads.sent.clear()
    day = ads_run.account_today(row(conn))
    add_days(conn, (day, 2000000, 1, 10, 0, 0), (day - timedelta(days=1), 3000000, 2, 20, 1, 1))
    days = ads_run.day_rows(conn, BIZ, GOOGLE, 7, row(conn))
    assert len(days) == 7 and days[0]["day"] == day.isoformat()  # zero-filled: exactly `limit` rows (Fix round 1)
    assert days[0]["spent_usd"] == 2.0 and days[1]["spent_usd"] == 3.0  # newest first
    assert days[2] == {
        "day": days[2]["day"],
        "spent_usd": 0.0,
        "clicks": 0,
        "impressions": 0,
        "calls": 0,
        "conversions": 0.0,
    }
    assert ads_run.totals(days) == {"spent_usd": 5.0, "clicks": 3, "impressions": 30, "calls": 1, "conversions": 1.0}
    assert ads_run.day_rows(conn, BIZ, GOOGLE, 1, row(conn)) == [days[0]]


def lead_counts(conn) -> dict[str, tuple[int, int]]:
    """BIZ's Google Search days: day -> (leads, booked)."""
    found = conn.execute(
        "SELECT day, leads, booked FROM metric_daily WHERE business_id = %s AND channel = %s", (BIZ, GOOGLE)
    ).fetchall()
    return {str(r["day"]): (r["leads"], r["booked"]) for r in found}


def add_lead(conn, lead_id: str, channel: str | None, at: datetime, status: str = "new", *, biz: str = BIZ) -> None:
    """A lead the page recorded at `at`, attributed to `channel` (None: organic), as the owner has marked it."""
    kind = {GOOGLE: "gclid", "newsbreak": "nb_cid"}.get(channel or "")
    conn.execute(
        "INSERT INTO lead (id, business_id, kind, fields, click_id, click_id_kind, channel, status, created_at) "
        "VALUES (%s, %s, 'form', '{}', %s, %s, %s, %s, %s)",
        (lead_id, biz, f"c_{lead_id}" if kind else None, kind, channel, status, at),
    )


def test_sync_counts_each_days_leads_and_bookings_from_the_lead_store_in_the_accounts_calendar(conn, gads, live):  # noqa: F811 (fixture)
    """Plan P5 (M0 Task 7): a day's `leads` are the business's `lead` rows the channel brought (`lead.channel`) created
    that day in the account's own calendar -- the zone this very sync read from Google -- whatever their status, spam
    included; `booked` are those booked or won. Every sync counts its days again, so a lead marked booked since, or a
    new one, is in the next sync's numbers."""
    launched(conn, gads)
    gads.sent.clear()
    gads.day_metrics = [DAY1, DAY2, DAY3]
    conn.execute("INSERT INTO business (id) VALUES ('biz_2')")
    pacific = ZoneInfo("America/Los_Angeles")
    for lead_id, channel, at, status, biz in (
        ("ld_1", GOOGLE, datetime(2026, 9, 27, 10, 0, tzinfo=pacific), "new", BIZ),
        ("ld_2", GOOGLE, datetime(2026, 9, 27, 0, 30, tzinfo=pacific), "booked", BIZ),
        ("ld_3", GOOGLE, datetime(2026, 9, 26, 23, 30, tzinfo=pacific), "won", BIZ),  # already the 27th in UTC
        ("ld_4", GOOGLE, datetime(2026, 9, 25, 9, 0, tzinfo=pacific), "spam", BIZ),
        ("ld_5", GOOGLE, datetime(2026, 9, 24, 23, 0, tzinfo=pacific), "won", BIZ),  # the account's 24th; UTC's 25th
        ("ld_6", "newsbreak", datetime(2026, 9, 27, 9, 0, tzinfo=pacific), "booked", BIZ),  # another channel's
        ("ld_7", None, datetime(2026, 9, 27, 9, 0, tzinfo=pacific), "booked", BIZ),  # organic
        ("ld_8", GOOGLE, datetime(2026, 9, 27, 9, 0, tzinfo=pacific), "booked", "biz_2"),  # another business's
        ("ld_10", GOOGLE, datetime(2026, 9, 27, 0, 0, tzinfo=pacific), "new", BIZ),  # midnight: the new day's
    ):
        add_lead(conn, lead_id, channel, at, status, biz=biz)

    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert row(conn)["resources"]["time_zone"] == "America/Los_Angeles"  # read by this sync, first
    assert lead_counts(conn) == {"2026-09-25": (1, 0), "2026-09-26": (1, 1), "2026-09-27": (3, 1)}
    conn.execute("UPDATE lead SET status = 'booked' WHERE id = 'ld_1'")
    add_lead(conn, "ld_9", GOOGLE, datetime(2026, 9, 27, 12, 30, tzinfo=pacific))
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert lead_counts(conn) == {"2026-09-25": (1, 0), "2026-09-26": (1, 1), "2026-09-27": (4, 2)}
    assert ads_run.account_zone(conn, BIZ, GOOGLE) == pacific
    assert ads_run.account_zone(conn, BIZ, "newsbreak") == UTC  # no row: UTC


def test_sync_counts_leads_on_the_accounts_own_day_east_of_utc_too(conn, gads, live):  # noqa: F811 (fixture)
    """Fix round 1, Minor 6: an account ahead of UTC (Berlin, UTC+2 in September) starts its days before UTC's -- a
    lead at 01:30 on the 27th there is UTC's 26th, one at midnight Berlin opens the 25th while UTC is still on the
    24th -- and each is counted on the account's own day."""
    gads.time_zone = "Europe/Berlin"  # the account's zone the first sync reads (22:00 on the 27th there)
    launched(conn, gads)
    gads.day_metrics = [DAY1, DAY2, DAY3]
    berlin = ZoneInfo("Europe/Berlin")
    for lead_id, at, status in (
        ("ld_1", datetime(2026, 9, 27, 1, 30, tzinfo=berlin), "booked"),  # 23:30 on the 26th in UTC
        ("ld_2", datetime(2026, 9, 25, 0, 0, tzinfo=berlin), "won"),  # 22:00 on the 24th in UTC
        ("ld_3", datetime(2026, 9, 25, 23, 59, tzinfo=berlin), "new"),
        ("ld_4", datetime(2026, 9, 26, 0, 0, tzinfo=berlin), "new"),  # midnight: the 26th's, not the 25th's
        ("ld_5", datetime(2026, 9, 24, 23, 59, tzinfo=berlin), "won"),  # before the window
    ):
        add_lead(conn, lead_id, GOOGLE, at, status)
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    assert ads_run.account_zone(conn, BIZ, GOOGLE) == berlin
    assert lead_counts(conn) == {"2026-09-25": (2, 1), "2026-09-26": (1, 0), "2026-09-27": (1, 1)}


def test_sync_writes_its_days_under_the_channel_with_calls_and_no_leads_of_its_own(conn, gads, live):  # noqa: F811 (fixture)
    """metric_daily carries the channel; Google's phone calls are `calls`; `leads` and `booked` are the lead store's
    to count (plan P5), never the platform's, and the source says the numbers came from the API."""
    launched(conn, gads)
    gads.day_metrics = [DAY1]
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    r = conn.execute("SELECT channel, calls, leads, booked, source FROM metric_daily WHERE business_id = %s", (BIZ,))
    assert r.fetchall() == [{"channel": GOOGLE, "calls": 1, "leads": 0, "booked": 0, "source": "api"}]


def test_execution_line_says_where_each_state_really_is(conn, gads):  # noqa: F811 (fixture)
    """A3 final review: `failed`, `validated` and `queued` never read "Launching"; no row at all is a plan approved
    while the switch was off, which a new approval launches (ruling S18)."""
    assert ads_run.execution_line([]) == ads_run.NOT_LAUNCHED_OFF
    assert "approve a new version once execution is on" in ads_run.NOT_LAUNCHED_OFF
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    lines = {}
    for state, error in (
        ("queued", None),
        ("queued", google_search.RETRYING),
        ("validated", None),
        ("validated", ads_run.NO_PAGE),
        ("launching", google_search.RETRYING),
        ("failed", google_search.GONE),
        ("paused", None),
    ):
        conn.execute(  # A3's `validated` is `queued` with validated_at set (plan P4)
            "UPDATE channel_campaign SET state = %s, last_error = %s, validated_at = CASE WHEN %s THEN now() END "
            "WHERE business_id = %s",
            ("queued" if state == "validated" else state, error, state == "validated", BIZ),
        )
        lines[state, error] = ads_run.execution_line(ads_run.rows(conn, BIZ))
    assert lines == {
        ("queued", None): "Turns on within the hour",
        ("queued", google_search.RETRYING): f"Waiting to launch: {google_search.RETRYING}",
        ("validated", None): "Checked with Google; turns on once execution is live",
        ("validated", ads_run.NO_PAGE): f"Waiting to launch: {ads_run.NO_PAGE}",
        ("launching", google_search.RETRYING): "Launching",
        ("failed", google_search.GONE): f"Not running: {google_search.GONE}",
        ("paused", None): "Paused",
    }
    assert all(plain(line) for line in lines.values())
    conn.execute("UPDATE channel_campaign SET state = 'live', launched_at = now() WHERE business_id = %s", (BIZ,))
    assert ads_run.execution_line(ads_run.rows(conn, BIZ)) == "Live since today"


def test_execution_line_is_one_clause_per_channel_row(conn, gads):  # noqa: F811 (fixture)
    """One row reads exactly as A3's line; several name their channel, one clause each, in card order."""
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    conn.execute("UPDATE channel_campaign SET state = 'live', launched_at = now() WHERE business_id = %s", (BIZ,))
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, last_error) "
        "VALUES (%s, 'newsbreak', %s, 'waiting_account', %s)",
        (BIZ, plan_id, nb_adapter.ACCOUNT_WAIT),
    )
    line = ads_run.execution_line(ads_run.rows(conn, BIZ))
    # M1 Task 7: an account NewsBreak gets ready for Admiry is NewsBreak's work, in its own words (never "your account",
    # never funds: fix round 1, item 2) -- the row's wait as the launch writes it (final review Minor 7: the clause is
    # that wait's alone)
    assert line == (
        "Google Search: Live since today; NewsBreak: NewsBreak is getting your account ready; your ads start once it is"
    )
    assert plain(line)
