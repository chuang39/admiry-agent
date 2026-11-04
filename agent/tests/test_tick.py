import logging
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from agent import ads_run, config, jobs, state, tick, tools_account
from agent.channels.google_search import adapter as google
from agent.tests.conftest import TEST_DB_URL
from agent.tests.test_ads_run import (  # noqa: F401 - clock and gads are fixtures
    BIZ,
    GOOGLE,
    _row_for,
    clock,
    gads,
    plain,
    queued,
    row,
)
from agent.tests.test_ads_run_channels import NB, add_plan_by_hand, nb_row, seed_plan, two_channels

# spend-guard's details when nothing is paused or resumed (M0 Task 7 adds the month cap's counts and operations)
IDLE = {
    "paused": 0,
    "funds_paused": 0,
    "funds_resumed": 0,
    "month_capped": 0,
    "month_resumed": 0,
    "errors": 0,
    "ops": 0,
    "ops_by_channel": {},
}


@pytest.fixture
def google_live(monkeypatch):
    """Google Search's own switch at `live`: test_ads_run's `live`, under a name no test here uses for a variable."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")


REPO = Path(__file__).resolve().parents[2]


def _business(conn, biz_id, **cols):
    keys_ = ", ".join(["id", *cols])
    marks = ", ".join(["%s"] * (1 + len(cols)))
    conn.execute(f"INSERT INTO business ({keys_}) VALUES ({marks})", (biz_id, *cols.values()))


def test_parse_shard():
    assert tick.parse_shard("0/1") == (0, 1)
    assert tick.parse_shard("2/4") == (2, 4)
    for bad in ("4/4", "-1/2", "a/b", "1", "0/0"):
        with pytest.raises(ValueError):
            tick.parse_shard(bad)


def test_run_records_a_job_run_row_and_returns_details(conn):
    out = tick.run("proposal-expire", url=TEST_DB_URL)
    assert out == {"expired": 0}
    row = conn.execute("SELECT job, shard, ok, details, finished_at FROM job_run").fetchone()
    assert (row["job"], row["shard"], row["ok"], row["details"]) == ("proposal-expire", "0/1", True, {"expired": 0})
    assert row["finished_at"] is not None


def test_job_failed_details_reach_the_ledger(conn, monkeypatch):
    """A job that ran but must page (jobs.JobFailed, the digest's failing sends): its details reach job_run with ok
    false, and the error re-raises so the command exits 1 (plan 2026-10-07-agent-k2 Task 11, Dg P13)."""

    def paging(conn, i, n):
        raise jobs.JobFailed("digest sends failing", {"sent": 0, "failed": 2})

    monkeypatch.setitem(jobs.JOBS, "paging", paging)
    monkeypatch.setitem(tick.JOBS, "paging", paging)
    with pytest.raises(jobs.JobFailed):
        tick.run("paging", url=TEST_DB_URL)
    row = conn.execute("SELECT ok, details, finished_at FROM job_run").fetchone()
    assert row["ok"] is False and row["finished_at"] is not None
    assert row["details"] == {"sent": 0, "failed": 2, "error": "digest sends failing"}


def test_unknown_job_is_an_error_before_any_row(conn):
    with pytest.raises(KeyError):
        tick.run("no-such-job", url=TEST_DB_URL)
    assert conn.execute("SELECT count(*) AS c FROM job_run").fetchone()["c"] == 0


def test_proposal_expire_only_touches_overdue_proposed_rows(conn):
    _business(conn, "biz_1")
    conn.execute(
        "INSERT INTO proposal (id, business_id, class, kind, payload, summary, status, expires_at) VALUES "
        "('p_old', 'biz_1', 'spend', 'approve_plan', '{}', 's', 'proposed', now() - interval '1 hour'), "
        "('p_fresh', 'biz_1', 'spend', 'approve_plan', '{}', 's', 'proposed', now() + interval '1 hour'), "
        "('p_done', 'biz_1', 'spend', 'approve_plan', '{}', 's', 'confirmed', now() - interval '1 hour')"
    )
    assert tick.run("proposal-expire", url=TEST_DB_URL) == {"expired": 1}
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM proposal")}
    assert status == {"p_old": "expired", "p_fresh": "proposed", "p_done": "confirmed"}
    assert conn.execute("SELECT resolved_at FROM proposal WHERE id = 'p_old'").fetchone()["resolved_at"] is not None


def test_claim_expire_retires_dead_drafts_and_frees_stale_claims(conn):
    # a draft whose only draft key expired more than 24 h ago -> expired
    _business(conn, "biz_dead", created_at="2020-01-01")
    conn.execute(
        "INSERT INTO api_key (id, key_hash, business_id, label, class, scopes, expires_at) VALUES "
        "('key_dead', 'h1', 'biz_dead', 'draft', 'draft', '{read,manage}', now() - interval '2 hours')"
    )
    # a draft with a live draft key -> untouched
    _business(conn, "biz_live", created_at="2020-01-01")
    conn.execute(
        "INSERT INTO api_key (id, key_hash, business_id, label, class, scopes, expires_at) VALUES "
        "('key_live', 'h2', 'biz_live', 'draft', 'draft', '{read,manage}', now() + interval '2 hours')"
    )
    # a draft younger than 24 h with no key yet -> untouched
    _business(conn, "biz_new")
    # a claimed-but-unverified business past its 14-day deadline -> back to draft, slug freed
    _business(conn, "biz_stale", claim_state="claimed", slug="stale-biz", claim_expires_at="2020-01-15")
    # a verified business with a claim_expires_at in the past -> untouched (verified never expires)
    _business(conn, "biz_ok", claim_state="verified", slug="ok-biz", claim_expires_at="2020-01-15")
    assert tick.run("claim-expire", url=TEST_DB_URL) == {"drafts_expired": 1, "claims_released": 1}
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, claim_state, slug FROM business")}
    assert rows["biz_dead"]["status"] == "expired"
    assert rows["biz_live"]["status"] == "active" and rows["biz_new"]["status"] == "active"
    assert (rows["biz_stale"]["claim_state"], rows["biz_stale"]["slug"]) == ("draft", None)
    assert (rows["biz_ok"]["claim_state"], rows["biz_ok"]["slug"]) == ("verified", "ok-biz")


def test_a_released_claim_keeps_the_business_and_its_owner(conn):
    from agent import claims
    from agent.tests.test_claims import draft_business

    _plaintext, _ = draft_business(conn)
    claims.claim(conn, "biz_1", "owner@example.com")  # the draft key becomes a business key
    conn.execute(
        "UPDATE business SET created_at = now() - interval '30 days', claim_expires_at = now() - interval '1 day'"
    )
    assert tick.run("claim-expire", url=TEST_DB_URL) == {"drafts_expired": 0, "claims_released": 1}
    row = conn.execute("SELECT status, claim_state, slug, owner_email FROM business WHERE id = 'biz_1'").fetchone()
    assert dict(row) == {"status": "active", "claim_state": "draft", "slug": None, "owner_email": "owner@example.com"}
    assert tick.run("claim-expire", url=TEST_DB_URL) == {"drafts_expired": 0, "claims_released": 0}
    assert conn.execute("SELECT status FROM business WHERE id = 'biz_1'").fetchone()["status"] == "active"
    with pytest.raises(claims.ClaimRefused):  # another account is still refused
        claims.claim(conn, "biz_1", "other@example.com")


def test_shards_partition_by_hashtext(conn):
    _business(conn, "biz_1")
    for i in range(6):
        conn.execute(
            "INSERT INTO proposal (id, business_id, class, kind, payload, summary, status, expires_at) VALUES "
            "(%s, %s, 'spend', 'approve_plan', '{}', 's', 'proposed', now() - interval '1 hour')",
            (f"p_{i}", "biz_1"),
        )
    # all six rows belong to biz_1, so exactly one of the two shards owns them all
    a = tick.run("proposal-expire", shard="0/2", url=TEST_DB_URL)["expired"]
    b = tick.run("proposal-expire", shard="1/2", url=TEST_DB_URL)["expired"]
    assert sorted([a, b]) == [0, 6]
    owner = conn.execute("SELECT mod(abs(hashtext('biz_1')::bigint), 2) AS s").fetchone()["s"]
    assert (a if owner == 0 else b) == 6


def test_spend_guard_flags_funded_prepaid_businesses_at_or_below_zero_once(conn):
    from datetime import date

    from agent import wallet
    from agent.tests.test_wallet import fund

    for biz in ("biz_zero", "biz_minus", "biz_plus", "biz_never", "biz_gone", "biz_own", "biz_credit", "biz_spent"):
        _business(conn, biz, claim_state="verified", ads_mode="own_account" if biz == "biz_own" else "managed")
    for i, biz in enumerate(("biz_zero", "biz_minus", "biz_plus", "biz_gone", "biz_own")):
        fund(conn, biz, cents=10000, session_id=f"cs_{i}")
    wallet.adjust(conn, "biz_credit", 5000, note="goodwill credit")  # funded by credit, never a card
    for biz, cost in (
        ("biz_zero", 10000),
        ("biz_minus", 12000),
        ("biz_gone", 12000),
        ("biz_own", 12000),
        ("biz_credit", 6000),
        ("biz_spent", 1000),
    ):  # biz_spent: spend, never funded
        wallet.spend(conn, biz, channel="google_search", day=date(2026, 9, 24), cost_cents=cost)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_gone'")
    assert tick.run("spend-guard", url=TEST_DB_URL) == {**IDLE, "paused": 4}
    flags = {r["id"]: r["paused_reason"] for r in conn.execute("SELECT id, paused_reason FROM business")}
    assert flags == {
        "biz_zero": "no_funds",
        "biz_minus": "no_funds",
        "biz_plus": None,
        "biz_never": None,
        "biz_gone": None,
        "biz_own": None,
        "biz_credit": "no_funds",
        "biz_spent": "no_funds",
    }
    rows = conn.execute(
        "SELECT business_id, actor, actor_class FROM agent_action WHERE tool = 'job.spend_guard' ORDER BY business_id"
    ).fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("biz_credit", "job:spend-guard", "job"),
        ("biz_minus", "job:spend-guard", "job"),
        ("biz_spent", "job:spend-guard", "job"),
        ("biz_zero", "job:spend-guard", "job"),
    ]
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE  # once
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'job.spend_guard'").fetchone()["n"] == 4


def _session(conn, biz_id, *, expires_at, opening_text="dentist"):
    from agent import consult

    _business(conn, biz_id)
    return consult.create(
        conn,
        biz_id,
        tree="fallback",
        locale="en-US",
        opening_text=opening_text,
        known_facts={},
        options={},
        answers={},
        expires_at=expires_at,
    )


def test_consult_expire_retires_overdue_open_and_ready_sessions_and_clears_the_handoff(conn):
    from agent import consult

    open_dead = _session(conn, "biz_open_dead", expires_at="2020-01-01")
    ready_dead = _session(conn, "biz_ready_dead", expires_at="2020-01-01")
    conn.execute("UPDATE consult_session SET status = 'ready' WHERE id = %s", (ready_dead["id"],))
    token = consult.mint_handoff(conn, ready_dead["id"])
    assert token  # a live token on the session that is about to expire
    live = _session(conn, "biz_live", expires_at="2099-01-01")
    planned_dead = _session(conn, "biz_planned_dead", expires_at="2020-01-01")
    conn.execute("UPDATE consult_session SET status = 'planned' WHERE id = %s", (planned_dead["id"],))
    assert tick.run("consult-expire", url=TEST_DB_URL) == {"expired": 2}
    rows = {
        r["id"]: r for r in conn.execute("SELECT id, status, handoff_hash, handoff_expires_at FROM consult_session")
    }
    assert rows[ready_dead["id"]]["status"] == "expired"
    assert rows[ready_dead["id"]]["handoff_hash"] is None and rows[ready_dead["id"]]["handoff_expires_at"] is None
    assert rows[open_dead["id"]]["status"] == "expired"
    assert rows[planned_dead["id"]]["status"] == "planned"  # untouched: not open/ready
    assert rows[live["id"]]["status"] == "open"  # untouched: not overdue
    assert tick.run("consult-expire", url=TEST_DB_URL) == {"expired": 0}


def _oauth_client(conn, client_id="cl_1"):
    conn.execute(
        "INSERT INTO oauth_client (id, client_name, redirect_uris, info) VALUES (%s, 'Test Client', '[]', '{}')",
        (client_id,),
    )


def _oauth_grant(conn, biz_id="biz_1", grant_id="gr_1", client_id="cl_1"):
    _business(conn, biz_id)
    _oauth_client(conn, client_id)
    conn.execute(
        "INSERT INTO oauth_grant (id, client_id, business_id, owner_email, scopes) VALUES (%s, %s, %s, 'o@example.com', '{read}')",
        (grant_id, client_id, biz_id),
    )


def test_oauth_prune_deletes_old_txns_codes_and_stale_rate_limit_hits(conn):
    _oauth_grant(conn)
    conn.execute(
        "INSERT INTO oauth_txn (id, client_id, params, expires_at, used_at) VALUES "
        "('tx_used_old', 'cl_1', '{}', now() + interval '1 hour', now() - interval '2 days'), "
        "('tx_expired_old', 'cl_1', '{}', now() - interval '2 days', NULL), "
        "('tx_used_recent', 'cl_1', '{}', now() + interval '1 hour', now() - interval '1 hour'), "
        "('tx_live', 'cl_1', '{}', now() + interval '1 hour', NULL)"
    )
    conn.execute(
        "INSERT INTO oauth_code (code_hash, grant_id, client_id, scopes, code_challenge, expires_at, used_at) VALUES "
        "('co_used_old', 'gr_1', 'cl_1', '{read}', 'chal', now() + interval '10 minutes', now() - interval '2 days'), "
        "('co_expired_old', 'gr_1', 'cl_1', '{read}', 'chal', now() - interval '2 days', NULL), "
        "('co_live', 'gr_1', 'cl_1', '{read}', 'chal', now() + interval '10 minutes', NULL)"
    )
    conn.execute(
        "INSERT INTO oauth_hit (ip, ts) VALUES "
        "('1.1.1.1', now() - interval '2 hours'), ('1.1.1.1', now() - interval '90 minutes'), ('1.1.1.1', now() - interval '10 minutes')"
    )
    assert tick.run("oauth-prune", url=TEST_DB_URL) == {"txns": 2, "codes": 2, "hits": 2}
    assert {r["id"] for r in conn.execute("SELECT id FROM oauth_txn")} == {"tx_used_recent", "tx_live"}
    assert {r["code_hash"] for r in conn.execute("SELECT code_hash FROM oauth_code")} == {"co_live"}
    assert conn.execute("SELECT count(*) AS n FROM oauth_hit").fetchone()["n"] == 1
    assert tick.run("oauth-prune", url=TEST_DB_URL) == {"txns": 0, "codes": 0, "hits": 0}


def test_a_top_up_after_the_guard_lifts_the_flag_for_good(conn):
    from datetime import date

    from agent import wallet
    from agent.tests.test_wallet import fund

    _business(conn, "biz_1", claim_state="verified", ads_mode="managed")
    fund(conn, "biz_1", cents=10000, session_id="cs_1")
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=10000)
    assert tick.run("spend-guard", url=TEST_DB_URL) == {**IDLE, "paused": 1}
    fund(conn, "biz_1", cents=20000, session_id="cs_2")
    assert conn.execute("SELECT paused_reason FROM business WHERE id = 'biz_1'").fetchone()["paused_reason"] is None
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE


# ---------------------------------------------------------------- the month cap (spec 2026-09-29 §6.4; M0 Task 7)


def _spent(conn, channel: str, day: date, usd: float, *, biz: str = BIZ) -> None:
    """A day of the channel's spend as ads-sync writes it."""
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'api')",
        (biz, channel, day, round(usd * 1_000_000)),
    )


def _google_live_beside_newsbreak(conn) -> str:
    """BIZ with a plan of $1,000 a month on Google Search -- launched against the fake Google Ads, the owner's own
    account -- and $300 on NewsBreak, whose row is written as running (no NewsBreak row runs in M0). Returns the
    plan id."""
    _key, plan_id = seed_plan(conn, two_channels(google_usd=1000.0, newsbreak_usd=300.0))
    queued(conn, plan_id)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
        "VALUES (%s, %s, %s, 'live', %s, now())",
        (BIZ, NB, plan_id, Jsonb({"campaign": "nb/1"})),
    )
    return plan_id


def _approve(conn, secs: dict) -> str:
    """A new version approved by the owner (the one before superseded, as proposals.apply_approve_plan leaves them);
    no step has run yet, so the rows still name the version before."""
    conn.execute("UPDATE plan SET status = 'superseded' WHERE business_id = %s AND status = 'approved'", (BIZ,))
    return add_plan_by_hand(conn, secs)


def _card_cap(conn, channel: str) -> float | None:
    """The channels card's monthly amount for the channel (`month_cap_usd`)."""
    rows = state.channel_rows(conn, config.load(), BIZ)
    return next(r.month_cap_usd for r in rows if r.channel == channel)


def _month_caps(conn) -> list[dict]:
    return conn.execute(
        "SELECT actor, actor_class, tool, ok, args_redacted, result FROM agent_action "
        "WHERE tool = 'job.spend_guard' ORDER BY id"
    ).fetchall()


def _paused_at(conn, channel: str, at: datetime) -> None:
    """A pause stamped at the scenario's time: Postgres writes `paused_at` from its own clock, while the tests move
    the app's (`clock`)."""
    conn.execute(
        "UPDATE channel_campaign SET paused_at = %s WHERE business_id = %s AND channel = %s AND state = 'paused'",
        (at, BIZ, channel),
    )


def test_spend_guard_pauses_a_channel_at_its_month_cap_once_and_resumes_it_on_the_first(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """§6.4: month-to-date spend at the approved amount pauses that channel alone with `month_cap` -- Google Search
    here, in the owner's own account -- tells Google Ads, marks when the cap was set (`resources.month_cap_at`, fix
    round 2) and says so in the activity log; a later run that month adds nothing (no second pause, audit row or
    Google operation, the mark unmoved); the first of the month turns it back on and the mark is gone. The Google
    operations the pause and the resume sent are in the run's details and in what Google's budget counts (fix round 1,
    Minor 4)."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 8, 31), 5000.0)  # last month's spend never counts
    _spent(conn, GOOGLE, date(2026, 9, 5), 600.0)
    _spent(conn, GOOGLE, date(2026, 9, 19), 399.99)
    _spent(conn, NB, date(2026, 9, 19), 299.99)
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE  # $999.99 of $1,000
    _spent(conn, GOOGLE, date(2026, 9, 20), 0.01)
    sent = len(gads.sent)
    assert tick.run("spend-guard", url=TEST_DB_URL) == {
        **IDLE,
        "month_capped": 1,
        "ops": 1,
        "ops_by_channel": {GOOGLE: 1},
    }
    assert jobs.ops_sent_today(conn) == 1  # the pause counts against Google's budget, never gated by it
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["paused_by"]) == ("paused", "month_cap", "job:spend-guard")
    assert r["resources"]["month_cap_at"] == "2026-09-20T20:00:00+00:00"
    assert gads.kinds()[sent:] == ["pause"] and "pause_owed" not in r["resources"]  # Google Ads was told
    assert nb_row(conn)["state"] == "live"  # NewsBreak is under its own $300
    (audited,) = _month_caps(conn)
    assert (audited["actor"], audited["result"]) == (
        "job:spend-guard",
        {"paused_reason": "month_cap", "channel": GOOGLE},
    )
    said = tools_account.sentence(audited, None)
    assert said == "Admiry paused Google Search: this month's amount is spent. It starts again on the 1st."
    assert plain(said)

    sent = len(gads.sent)
    clock(datetime(2026, 9, 25, 9, 0, tzinfo=UTC))
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE  # a later run that month: nothing more
    assert len(gads.sent) == sent and len(_month_caps(conn)) == 1
    assert row(conn)["resources"]["month_cap_at"] == "2026-09-20T20:00:00+00:00"  # never moved by a later run
    clock(datetime(2026, 9, 30, 23, 30, tzinfo=UTC))
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE and row(conn)["state"] == "paused"  # still September
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert out == {**IDLE, "month_resumed": 1, "ops": out["ops"], "ops_by_channel": {GOOGLE: out["ops"]}}
    assert out["ops"] > 0 and jobs.ops_sent_today(conn) == 1 + out["ops"]  # the resume's lookup and enable count too
    r = row(conn)
    assert (r["state"], r["paused_reason"]) == ("live", None) and "enable" in gads.kinds()[sent:]
    assert "month_cap_at" not in r["resources"]
    assert nb_row(conn)["state"] == "live" and len(_month_caps(conn)) == 1


def test_the_first_of_the_month_is_each_accounts_own(conn, gads, google_live, clock, monkeypatch, nbapi):  # noqa: F811 - gads and clock are fixtures imported under these names
    """Ruling S14's calendar: both channels spent their September; at 03:00 UTC on October 1st NewsBreak's month (its
    zone unknown: UTC) has begun, so its month-to-date is under its amount again, and Google Search's (Palo Alto:
    still September 30th) has not -- only NewsBreak's `month_cap` is lifted, and Google Search's at the first run of
    its own October. (Both switches `live`: a cap is lifted under that switch only.)"""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    _google_live_beside_newsbreak(conn)
    ads_run.remember(conn, BIZ, GOOGLE, {"time_zone": "America/Los_Angeles"})
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    _spent(conn, NB, date(2026, 9, 19), 300.0)
    ads_run.pause_channels(conn, config.load(), BIZ, channels=[GOOGLE], by="job:spend-guard", reason="month_cap")
    conn.execute(  # NewsBreak's paused before anything was built (M0's stub builds nothing)
        "UPDATE channel_campaign SET state = 'paused', paused_reason = 'month_cap', resources = %s "
        "WHERE business_id = %s AND channel = %s",
        (Jsonb({"paused_by": "job:spend-guard"}), BIZ, NB),
    )
    for channel in (GOOGLE, NB):
        _paused_at(conn, channel, datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    clock(datetime(2026, 10, 1, 3, 0, tzinfo=UTC))
    assert tick.run("spend-guard", url=TEST_DB_URL) == {**IDLE, "month_resumed": 1, "ops_by_channel": {NB: 0}}
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "month_cap")
    nb = nb_row(conn)
    assert (nb["state"], nb["paused_reason"]) == ("waiting_account", None)  # back to where an approval puts it
    clock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))  # 01:00 on October 1st in Palo Alto
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_resumed"] == 1
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("live", None)


def test_a_month_cap_from_last_month_lifts_on_a_later_day_and_one_from_this_month_stays(
    conn,
    gads,  # noqa: F811 - the fixture of the same name
    google_live,
    clock,  # noqa: F811 - the fixture of the same name
    monkeypatch,
    nbapi,
):
    """Fix round 1, Minor 2: a `month_cap` set in an earlier month of the account's calendar is lifted by the first run
    of the new month, whichever day that is -- here the 3rd, every run of the 1st and the 2nd lost -- and one set this
    month stays while its month is at its amount (final review I3: the lift reads the month-to-date against the
    amount, not when the cap was set -- these caps are written by hand, with no mark). Both switches `live`."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    _google_live_beside_newsbreak(conn)
    ads_run.pause_channels(conn, config.load(), BIZ, by="job:spend-guard", reason="month_cap")  # both channels
    _paused_at(conn, GOOGLE, datetime(2026, 9, 25, 12, 0, tzinfo=UTC))
    _paused_at(conn, NB, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    _spent(conn, GOOGLE, date(2026, 9, 24), 1000.0)  # September's: October is under its amount
    _spent(conn, NB, date(2026, 10, 2), 300.0)  # October's own: at its amount
    clock(datetime(2026, 10, 3, 12, 0, tzinfo=UTC))
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_resumed"] == 1
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("live", None)
    nb = nb_row(conn)
    assert (nb["state"], nb["paused_reason"]) == ("paused", "month_cap")
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE  # level-triggered: the next run finds nothing to do


def test_a_cap_is_lifted_only_under_the_live_switch(conn, gads, google_live, clock, monkeypatch):  # noqa: F811 (fixture)
    """Fix round 3: under `off` or `validate` a lift could only fail or be checked, so none is tried -- no Google
    operation, no error, the cap stands -- instead of one every hour; the first run once the switch is `live` again
    lifts it."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    for switch in ("off", "validate"):
        monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", switch)
        sent = len(gads.sent)
        assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE, switch
        assert len(gads.sent) == sent and row(conn)["paused_reason"] == "month_cap", switch
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert (out["month_resumed"], out["errors"]) == (1, 0) and out["ops"] > 0
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("live", None)


def test_each_channel_is_lifted_under_its_own_switch(conn, gads, google_live, clock, nbapi):  # noqa: F811 - gads and clock are fixtures imported under these names
    """Both channels capped in September; on October 1st both months are under their amounts again, but NewsBreak's
    switch is `off` (unset) while Google Search's is `live`: Google Search is lifted and NewsBreak keeps its cap -- one
    channel's switch never decides another's."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    _spent(conn, NB, date(2026, 9, 19), 300.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 2
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert (out["month_resumed"], out["errors"]) == (1, 0) and set(out["ops_by_channel"]) == {GOOGLE}
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("live", None)
    assert (nb_row(conn)["state"], nb_row(conn)["paused_reason"]) == ("paused", "month_cap")


def test_a_month_already_at_its_amount_on_its_first_day_stays_paused(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """A channel capped on the first day of its month (a plan lowered under what the day already spent) stays paused
    through that day -- never turned on and paused again every hour: its cap is this month's."""
    clock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 10, 1), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    clock(datetime(2026, 10, 1, 13, 0, tzinfo=UTC))
    sent = len(gads.sent)
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "month_cap") and len(gads.sent) == sent


def test_the_first_of_the_month_lifts_the_month_cap_alone_and_the_owners_pause_stands(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """The first of the month lifts Admiry's `month_cap` alone: the owner's own pause beside it stands and the ads
    stay off. Only running ads are capped: ads the owner paused are not, whatever they spent."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    ads_run.pause_by_owner(conn, config.load(), BIZ, GOOGLE, by="owner")
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    sent = len(gads.sent)
    assert tick.run("spend-guard", url=TEST_DB_URL) == {**IDLE, "month_resumed": 1, "ops_by_channel": {GOOGLE: 0}}
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["owner_paused"]) == ("paused", None, True)
    assert "month_cap_at" not in r["resources"] and "enable" not in gads.kinds()[sent:]
    _spent(conn, GOOGLE, date(2026, 10, 1), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE  # paused by the owner: nothing for the guard to do
    assert row(conn)["paused_reason"] is None and len(_month_caps(conn)) == 1


def test_the_owners_pause_in_the_new_month_never_keeps_last_months_cap(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """Fix round 2: the owner pausing a September-capped channel at 00:03 on October 1st, before the lift runs, stamps
    `paused_at` with October, yet the cap is lifted at 00:10 all the same (final review I3: the lift reads the month
    against its amount, never a timestamp): the ads stay off for the owner's pause alone, with no reason of Admiry's
    and no mark left."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    clock(datetime(2026, 10, 1, 0, 3, tzinfo=UTC))
    ads_run.pause_by_owner(conn, config.load(), BIZ, GOOGLE, by="owner")
    _paused_at(conn, GOOGLE, datetime(2026, 10, 1, 0, 3, tzinfo=UTC))  # the owner's pause moved it into October
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    sent = len(gads.sent)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_resumed"] == 1
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["owner_paused"]) == ("paused", None, True)
    assert "month_cap_at" not in r["resources"] and "enable" not in gads.kinds()[sent:]
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE


def test_a_cap_set_this_month_lifts_on_the_first_run_after_the_owner_approves_a_bigger_amount(
    conn,
    gads,  # noqa: F811 - the fixture of the same name
    google_live,
    clock,  # noqa: F811 - the fixture of the same name
):
    """Final review I3: the cap is lifted when the month is under the approved amount again, whatever made it so --
    here the owner approving $1,500 on Google Search after $1,000 of it was spent -- not only on the first of the next
    month. The channels card reads the same approved amount (not the version the row still names)."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    _approve(conn, two_channels(google_usd=1500.0, newsbreak_usd=300.0))
    assert _card_cap(conn, GOOGLE) == 1500.0 and row(conn)["plan_id"] != ads_run.approved(conn, BIZ)["id"]
    clock(datetime(2026, 9, 20, 21, 0, tzinfo=UTC))
    sent = len(gads.sent)
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert (out["month_resumed"], out["errors"]) == (1, 0)
    r = row(conn)
    assert (r["state"], r["paused_reason"]) == ("live", None) and "enable" in gads.kinds()[sent:]
    assert "month_cap_at" not in r["resources"]  # the mark goes with the cap
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE  # nothing more to do while the month is under it


def test_a_raise_that_leaves_the_month_at_its_amount_does_nothing(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """$1,200 spent against $1,000, then $1,100 approved: the month is still at its amount, so the cap stands and no
    operation, audit row or new mark is sent or written -- the guard is level-triggered both ways."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1200.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    mark = row(conn)["resources"]["month_cap_at"]
    _approve(conn, two_channels(google_usd=1100.0, newsbreak_usd=300.0))
    clock(datetime(2026, 9, 21, 9, 0, tzinfo=UTC))
    sent = len(gads.sent)
    assert tick.run("spend-guard", url=TEST_DB_URL) == IDLE
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["month_cap_at"]) == ("paused", "month_cap", mark)
    assert len(gads.sent) == sent and len(_month_caps(conn)) == 1


def test_a_cap_on_a_channel_the_approved_plan_no_longer_funds_stays(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """A version that drops the capped channel puts no amount on it: the next month does not turn it back on."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_capped"] == 1
    secs = two_channels()
    secs["ads"]["channels"] = [c for c in secs["ads"]["channels"] if c["channel"] != GOOGLE]
    _approve(conn, secs)
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    sent = len(gads.sent)
    assert tick.run("spend-guard", url=TEST_DB_URL)["month_resumed"] == 0
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "month_cap") and len(gads.sent) == sent


def test_the_month_cap_never_claims_or_turns_on_ads_someone_paused_in_google_ads(conn, gads, google_live, clock):  # noqa: F811 (fixture)
    """M0 Task 9 D6 (A3p-2's ruling H33, for every pause of Admiry's): someone paused the ads in Google Ads itself --
    ads-sync's note, the row `limited`. A month at its amount caps nothing there: no pause of Admiry's, no mark, no
    audit row, no operation. And a month cap that stands on a row carrying that note is never lifted by the new
    month: no resume of Admiry's turns on ads someone paused by hand."""
    clock(datetime(2026, 9, 20, 20, 0, tzinfo=UTC))
    _google_live_beside_newsbreak(conn)
    conn.execute(
        "UPDATE channel_campaign SET state = 'limited', last_error = %s WHERE business_id = %s AND channel = %s",
        (google.PAUSED_IN_GOOGLE, BIZ, GOOGLE),
    )
    _spent(conn, GOOGLE, date(2026, 9, 19), 1000.0)
    sent = len(gads.sent)
    tried = {**IDLE, "ops_by_channel": {GOOGLE: 0}}  # asked, and nothing done: no operation
    assert tick.run("spend-guard", url=TEST_DB_URL) == tried
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["last_error"]) == ("limited", None, google.PAUSED_IN_GOOGLE)
    assert "month_cap_at" not in r["resources"] and _month_caps(conn) == [] and gads.sent[sent:] == []

    conn.execute(  # a cap that stands on a row carrying the note (written by hand: no path of Admiry's makes one)
        "UPDATE channel_campaign SET state = 'paused', paused_reason = 'month_cap', paused_at = now() "
        "WHERE business_id = %s AND channel = %s",
        (BIZ, GOOGLE),
    )
    clock(datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    assert tick.run("spend-guard", url=TEST_DB_URL) == tried
    r = row(conn)
    assert (r["state"], r["paused_reason"]) == ("paused", "month_cap") and gads.sent[sent:] == []


def test_one_business_failing_never_stops_the_month_cap_of_the_others(conn, monkeypatch, caplog):
    """An unexpected failure stays with its business -- logged by its type, never its words, and counted in `errors`
    -- and the next business is capped as usual."""
    built = {"campaign": "customers/1/campaigns/1", "campaign_budget": "customers/1/campaignBudgets/1"}
    for biz in ("biz_a", "biz_b"):
        _row_for(conn, biz, "live", resources=built)  # $1,000 a month each
        _spent(conn, GOOGLE, ads_run.account_today(None), 1000.0, biz=biz)
    capped: list[str] = []

    def pause_channels(conn_, settings, biz, *, channels, by, reason):
        if biz == "biz_a":
            raise RuntimeError("boom with ya29.SECRET")
        capped.append(biz)
        return {"channels": {ch: {"state": "paused"} for ch in channels}, "say": ""}

    monkeypatch.setattr(ads_run, "pause_channels", pause_channels)
    with caplog.at_level(logging.WARNING):
        out = tick.run("spend-guard", url=TEST_DB_URL)
    assert (out["month_capped"], out["errors"], capped) == (1, 1, ["biz_b"])
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "biz_a" in logged and "SECRET" not in logged


def test_the_job_runner_sends_the_jobs_info_lines_to_stderr():
    """Ruling H34: `python -m agent.tick` (the systemd unit and the Airflow tasks alike) configures logging before a
    job runs -- the root logger at INFO with one stderr handler -- so a job's INFO line (page-check's down hosted page)
    reaches journald and Airflow's task log instead of being dropped at the default WARNING. Run in a fresh
    interpreter: the test process's own root logger is pytest's."""
    code = (
        "import logging; from agent import tick; tick.setup_logging(); root = logging.getLogger(); "
        "print(logging.getLevelName(root.level), [type(h).__name__ for h in root.handlers]); "
        "logging.getLogger('agent.jobs').info(\"page-check: business biz_1's hosted page did not open: HTTP 502\")"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=REPO, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "INFO ['StreamHandler']"
    line = r"^\S+ \S+ INFO agent\.jobs: page-check: business biz_1's hosted page did not open: HTTP 502$"
    assert re.search(line, out.stderr, re.MULTILINE), out.stderr


def test_the_jobs_logging_keeps_the_addresses_the_http_client_reads_out_of_the_log():
    """K4 R7 (plan D5: a business nearby's website lives in memory for one run): httpx logs every request it makes as
    an INFO line with the whole URL, and research-run reads the websites of businesses nearby -- into journald and
    Airflow's task log, which keep INFO. The job's logging speaks of httpx and httpcore at WARNING and above only. In a
    fresh interpreter with the web answered from memory: one page is read before the job's logging is set up (the
    control: the line is there) and one after it."""
    code = (
        "import logging, socket, httpx; from agent import crawl, tick\n"
        "from agent.roles import research_material as rm\n"
        "public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]\n"
        "socket.getaddrinfo = lambda host, port, *a, **k: public\n"
        "real = httpx.Client\n"
        "html = {'content-type': 'text/html'}\n"
        "page = lambda request: httpx.Response(200, headers=html, text='<html><body>Hello there.</body></html>')\n"
        "crawl.httpx.Client = lambda **kw: real(**{**kw, 'transport': httpx.MockTransport(page)})\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "print(rm.fetch_page('https://before-rival.example/') is not None)\n"
        "tick.setup_logging()\n"
        "print(rm.fetch_page('https://after-rival.example/') is not None)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=REPO, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "True"]
    assert "before-rival.example" in out.stderr  # the control: httpx does log the address at INFO
    assert "after-rival.example" not in out.stderr


def test_main_sets_logging_up_before_the_job_runs(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(tick, "setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(tick, "run", lambda job, shard: (calls.append(f"run {job} {shard}"), {"expired": 0})[1])
    assert tick.main(["proposal-expire"]) == 0
    assert calls == ["logging", "run proposal-expire 0/1"]
    assert '"expired": 0' in capsys.readouterr().out
