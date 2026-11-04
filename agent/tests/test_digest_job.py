"""The digest job (digest spec 2026-09-30 §3, §7, §8; plan 2026-10-07-agent-k2 Task 11, Dg Task 8): one row per business
and reported day, claimed when due, built with the Analyst's look, sent under its own id, re-checked before every send,
stale at 20:00, paging when sends fail; operator mode leaves nothing an owner can read."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agent import ads_run, config, db, digest, digest_zone, jobs, tick
from agent.tests import digest_fixtures as df
from agent.tests.conftest import TEST_DB_URL
from agent.tests.fixtures import insight_fixture, insight_review_fixture

NOW = df.ANALYST_NOW  # 08:02 on Tuesday 2026-10-06 in Los Angeles
DAY = df.ANALYST_DAY


def at(hours: float) -> datetime:
    """Tuesday in Los Angeles at `hours` local (PDT, UTC-7)."""
    return datetime(2026, 10, 6, 7, tzinfo=UTC) + timedelta(hours=hours)


@pytest.fixture
def dg(conn, digest_env, analyst_biz):
    """The Analyst's business with the digest on: Resend mocked (`dg.requests`), roles on, 08:02 Tuesday."""
    return digest_env


def run(conn, monkeypatch=None, when: datetime | None = None) -> dict:
    if when is not None:
        monkeypatch.setattr(ads_run, "now", lambda: when)
    return digest.run(conn, config.load(), 0, 1)


def rows(conn) -> list[dict]:
    return conn.execute("SELECT * FROM digest ORDER BY day").fetchall()


def sent_json(dg) -> list[dict]:
    return [json.loads(r.content) for r in dg.requests]


def test_tuesday_digest_end_to_end(conn, dg):
    out = run(conn)
    [d] = rows(conn)
    assert (d["edition"], d["status"], d["mode"], d["numbers_final"], d["attempts"]) == ("daily", "sent", "on", True, 1)
    assert d["rendered"]["subject"] == "Yesterday: $23.10 spent, 2 calls, 1 form"
    [req] = dg.requests
    assert req.headers["Idempotency-Key"] == d["id"]
    sent = json.loads(req.content)
    assert {"List-Unsubscribe", "List-Unsubscribe-Post"} <= set(sent["headers"])
    out_row = conn.execute("SELECT id, outcome FROM email_message WHERE direction = 'out'").fetchone()
    assert out_row == {"id": d["id"], "outcome": "sent"}
    assert d["note"] == insight_fixture().summary and d["body"]["note"] == d["note"]
    assert (out["sent"], out["takes"], out["insights"]) == (1, 1, {"done": 1})


def test_monday_is_the_weekly_edition(conn, dg, monkeypatch):
    run(conn, monkeypatch, NOW - timedelta(days=1))
    [d] = rows(conn)
    assert d["edition"] == "weekly" and d["body"]["week"] is not None


def test_not_due_before_eight_or_after_eight_pm(conn, dg, monkeypatch):
    run(conn, monkeypatch, at(7 + 2 / 60))
    run(conn, monkeypatch, at(20 + 2 / 60))
    assert rows(conn) == [] and dg.requests == []


def test_gated_until_synced_then_sent_once_synced(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    conn.execute("UPDATE channel_campaign SET synced_at = %s", (at(-1),))  # before local midnight
    out = run(conn)
    assert out["gated"] == 1 and rows(conn) == []
    conn.execute("UPDATE channel_campaign SET synced_at = %s", (at(9),))
    run(conn, monkeypatch, at(9 + 2 / 60))
    [d] = rows(conn)
    assert (d["status"], d["numbers_final"]) == ("sent", True)


def test_sent_at_eleven_not_final(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    conn.execute("UPDATE channel_campaign SET synced_at = %s", (at(-1),))
    run(conn, monkeypatch, at(11 + 2 / 60))
    [d] = rows(conn)
    assert (d["status"], d["numbers_final"]) == ("sent", False)
    assert d["body"]["sentence"].startswith("Yesterday's numbers aren't all in yet")


def test_never_synced_rows_do_not_hold_the_gate(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    conn.execute("UPDATE channel_campaign SET synced_at = NULL")
    run(conn)
    [d] = rows(conn)
    assert (d["status"], d["numbers_final"]) == ("sent", True)


def test_crash_in_building_is_rebuilt(conn, dg):
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode) "
        "VALUES ('dg_crash', 'biz_1', %s, 'daily', 'America/Los_Angeles', 'on')",
        (DAY,),
    )
    run(conn)
    [d] = rows(conn)
    assert (d["id"], d["status"]) == ("dg_crash", "sent")


def test_kill_after_accept_resends_the_same_bytes(conn, dg, monkeypatch):
    run(conn)
    conn.execute("UPDATE digest SET status = 'ready'")  # killed after Resend accepted, before the row said so
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    run(conn, monkeypatch, at(9 + 2 / 60))
    a, b = dg.requests
    assert json.loads(a.content) == json.loads(b.content)
    assert a.headers["Idempotency-Key"] == b.headers["Idempotency-Key"]
    assert conn.execute("SELECT count(*) AS n FROM email_message WHERE direction = 'out'").fetchone()["n"] == 1


def test_retry_keeps_one_row_and_one_key(conn, dg, monkeypatch):
    dg.fail = httpx.Response(500, json={"message": "boom"})
    with pytest.raises(jobs.JobFailed):
        run(conn)
    dg.fail = None
    run(conn, monkeypatch, at(9 + 2 / 60))
    [d] = rows(conn)
    assert (d["status"], d["attempts"]) == ("sent", 2)
    a, b = dg.requests
    assert a.headers["Idempotency-Key"] == b.headers["Idempotency-Key"] == d["id"]
    assert json.loads(a.content) == json.loads(b.content)
    out_rows = conn.execute("SELECT outcome FROM email_message WHERE direction = 'out'").fetchall()
    assert out_rows == [{"outcome": "sent"}]


def test_three_failures_then_stale_at_eight_pm(conn, dg, monkeypatch):
    dg.fail = httpx.Response(500, json={"message": "boom"})
    for hour in (8, 9, 10):
        with pytest.raises(jobs.JobFailed):
            run(conn, monkeypatch, at(hour + 2 / 60))
    run(conn, monkeypatch, at(11 + 2 / 60))  # a fourth run sends nothing
    assert len(dg.requests) == 3 and rows(conn)[0]["attempts"] == 3
    out = run(conn, monkeypatch, at(20 + 2 / 60))
    assert out["stale"] == 1 and rows(conn)[0]["status"] == "stale"
    assert conn.execute("SELECT count(*) AS n FROM email_message WHERE direction = 'out'").fetchone()["n"] == 1


def test_no_catch_up_of_older_days(conn, dg):
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status, rendered) "
        "VALUES ('dg_old', 'biz_1', %s, 'daily', 'America/Los_Angeles', 'on', 'ready', %s)",
        (DAY - timedelta(days=3), json.dumps({"subject": "x", "text": "x", "html": "x", "headers": {}})),
    )
    run(conn)
    old = conn.execute("SELECT status FROM digest WHERE id = 'dg_old'").fetchone()
    assert old["status"] == "stale"
    assert all(r.headers["Idempotency-Key"] != "dg_old" for r in dg.requests)


def _failed_once(conn, dg) -> None:
    dg.fail = httpx.Response(500, json={"message": "boom"})
    with pytest.raises(jobs.JobFailed):
        run(conn)
    dg.fail = None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE business SET digest_frequency = 'off'",
        "UPDATE business SET notify_email_status = 'bounced'",
        """UPDATE business SET operator_flags = '{"digest": "off"}'""",
    ],
    ids=["frequency_off", "bounce", "business_flag_off"],
)
def test_recheck_skips_a_failed_row(conn, dg, monkeypatch, change):
    _failed_once(conn, dg)
    conn.execute(change)
    run(conn, monkeypatch, at(9 + 2 / 60))
    assert rows(conn)[0]["status"] == "skipped" and len(dg.requests) == 1


def test_two_runs_on_one_business(conn, dg):
    with db.connect(TEST_DB_URL) as other, digest_zone.daily_lock(other, "biz_1") as held:
        assert held
        run(conn)
    assert rows(conn) == [] and dg.requests == []


def test_operator_mode_writes_nothing_owner_visible(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    before = conn.execute("SELECT count(*) AS n FROM agent_action WHERE business_id = 'biz_1'").fetchone()["n"]
    run(conn)
    [d] = rows(conn)
    assert (d["mode"], d["status"], d["thread_id"]) == ("operator", "sent", None)
    after = conn.execute("SELECT count(*) AS n FROM agent_action WHERE business_id = 'biz_1'").fetchone()["n"]
    assert after == before
    assert conn.execute("SELECT count(*) AS n FROM email_thread").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM email_message").fetchone()["n"] == 0
    [sent] = sent_json(dg)
    assert sent["to"] == ["ops@example.com"] and sent["subject"].startswith("[preview] ")


def test_operator_preview_with_the_channel_off(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    run(conn)
    [sent] = sent_json(dg)
    assert "Operator:" in sent["text"] and "insight: done · verdict deliver" in sent["text"]


def test_switch_change_applies_from_the_next_day(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    run(conn)
    monkeypatch.setenv("AGENT_DIGEST", "on")
    run(conn, monkeypatch, at(9 + 2 / 60))  # the same day: its row was sent as an operator preview
    assert [r["mode"] for r in rows(conn)] == ["operator"]
    run(conn, monkeypatch, NOW + timedelta(days=1))
    assert [r["mode"] for r in rows(conn)] == ["operator", "on"]


def test_off_still_purges_and_sends_nothing(conn, dg, monkeypatch):
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status) "
        "VALUES ('dg_ancient', 'biz_1', '2025-08-01', 'daily', 'America/Los_Angeles', 'on', 'sent')"
    )
    monkeypatch.setenv("AGENT_DIGEST", "off")
    out = run(conn)
    assert out == {"mode": "off", "purged": 1, "stale": 0} and dg.requests == [] and rows(conn) == []


def test_frequency_weekly_skips_tuesday(conn, dg):
    conn.execute("UPDATE business SET digest_frequency = 'weekly'")
    run(conn)
    assert rows(conn) == []


def test_several_businesses_name_themselves_in_the_subject(conn, dg):
    df.seed_business(conn, "biz_2", owner="OWNER@example.com")
    run(conn)
    [d] = rows(conn)
    assert d["rendered"]["subject"].endswith(f"· {digest._name(conn, 'biz_1')}")


def test_preview_writes_nothing(conn, dg):
    counts = "SELECT (SELECT count(*) FROM agent_action) AS a, (SELECT count(*) FROM digest) AS d, " + (
        "(SELECT count(*) FROM email_thread) AS t, (SELECT time_zone FROM business WHERE id = 'biz_1') AS z"
    )
    before = conn.execute(counts).fetchone()
    out = digest.preview(conn, config.load(), "biz_1", DAY, edition=None, note=True)
    assert conn.execute(counts).fetchone() == before and dg.requests == []
    assert out["subject"] == "Yesterday: $23.10 spent, 2 calls, 1 form" and "Operator:" in out["text"]


def test_job_fails_when_every_send_fails(conn, dg):
    dg.fail = httpx.Response(500, json={"message": "boom"})
    with pytest.raises(jobs.JobFailed):
        tick.run("digest", url=TEST_DB_URL)
    row = conn.execute("SELECT ok, details FROM job_run").fetchone()
    assert row["ok"] is False and row["details"]["failed"] == 1


@pytest.mark.parametrize("mode", ["on", "operator"])
def test_quota_refusal_pages(conn, dg, monkeypatch, mode):
    monkeypatch.setenv("AGENT_DIGEST", mode)
    dg.fail = httpx.Response(429, json={"message": "daily quota"})
    with pytest.raises(jobs.JobFailed) as e:
        run(conn)
    assert e.value.details["quota_refusals"] == 1


def test_job_is_registered():
    assert jobs.JOBS["digest"] is jobs.digest_job


# ---------------------------------------------------------------- the Analyst's look in the mail (K2)


def test_digest_carries_the_take_when_the_roles_are_on(conn, dg, monkeypatch):
    df.fake_llm(monkeypatch, insights=[insight_fixture(summary="Two calls yesterday.")])
    run(conn)
    [d] = rows(conn)
    assert d["note"] == "Two calls yesterday." and "Admiry's take: Two calls yesterday." in d["rendered"]["text"]
    assert conn.execute("SELECT count(*) AS n FROM insight").fetchone()["n"] == 1


def test_validate_insight_only_in_the_operator_preview(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    run(conn)
    [owner] = rows(conn)
    assert owner["note"] is None and owner["note_dropped"] == "validate" and owner["body"]["recommendations"] == []
    conn.execute("DELETE FROM digest")
    conn.execute("DELETE FROM email_message")
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    run(conn, monkeypatch, at(9 + 2 / 60))
    preview = sent_json(dg)[-1]
    assert insight_fixture().summary in preview["text"] and "verdict deliver" in preview["text"]


def test_digest_reads_the_insight_the_analyst_built(conn, dg, monkeypatch):
    from agent.roles import analyst

    gen = df.fake_llm(monkeypatch)
    assert analyst.analyst_run(conn, 0, 1)["done"] == 1  # :02
    run(conn, monkeypatch, NOW + timedelta(minutes=10))  # :12
    assert len(gen.insight_calls) == 1 and rows(conn)[0]["note"] == insight_fixture().summary


def test_digest_goes_without_the_take_when_the_model_fails(conn, dg, monkeypatch):
    df.fake_llm(monkeypatch, insight_error="the assistant is busy; try again in a minute")
    run(conn)
    [d] = rows(conn)
    assert (d["status"], d["note"], d["note_dropped"]) == ("sent", None, "llm")


def test_an_error_in_the_analysts_figures_never_holds_up_the_report(conn, dg, monkeypatch):
    """Final review I3: an error that is not the model's fails the day's look; the report goes on time without it."""
    from agent.roles import analyst

    def broken(*a, **k):
        raise KeyError("cost_per_lead_usd")

    monkeypatch.setattr(analyst, "metrics", broken)
    df.fake_llm(monkeypatch)
    out = run(conn)
    [d] = rows(conn)
    assert (d["status"], d["note"], d["note_dropped"]) == ("sent", None, "error") and out["errors"] == 0
    assert conn.execute("SELECT status, dropped FROM insight").fetchone() == {"status": "failed", "dropped": "error"}


def test_a_look_that_cannot_be_read_at_all_never_holds_up_the_report(conn, dg, monkeypatch):
    """Final review I3: whatever the Analyst raises, the report is built and sent without a take."""
    from agent.roles import analyst

    def broken(*a, **k):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(analyst, "ensure", broken)
    df.fake_llm(monkeypatch)
    out = run(conn)
    [d] = rows(conn)
    assert (d["status"], d["note"]) == ("sent", None) and out["errors"] == 0


def test_one_pending_row_that_fails_never_stops_the_run(conn, dg, monkeypatch):
    """Final review (re-graded Important): an error on one ready or failed row is counted and the run goes on to the
    due businesses, as an error on one business is."""
    _failed_once(conn, dg)
    real, raised = digest._send, []

    def flaky(*a, **k):
        if not raised:
            raised.append(1)
            raise RuntimeError("thread missing")
        return real(*a, **k)

    monkeypatch.setattr(digest, "_send", flaky)
    out = run(conn)
    assert out["errors"] == 1 and out["considered"] == 1


def test_roles_off_digest_has_no_take(conn, dg, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "off")
    run(conn)
    [d] = rows(conn)
    assert (d["status"], d["note"], d["note_dropped"]) == ("sent", None, "roles_off")
    assert conn.execute("SELECT count(*) AS n FROM insight").fetchone()["n"] == 0


def test_a_revised_look_reaches_no_owner_mail(conn, dg, monkeypatch):
    df.fake_llm(monkeypatch, insight_reviews=[insight_review_fixture(low={"priority": 2})])
    run(conn)
    [d] = rows(conn)
    assert (d["note"], d["note_dropped"]) == (None, "revise") and "Admiry's take" not in d["rendered"]["text"]
