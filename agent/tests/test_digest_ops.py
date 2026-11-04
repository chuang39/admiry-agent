"""The digest's operator commands (plan 2026-10-07-agent-k2 Task 16; Dg plan Task 13): `digest-preview` shows a day's
report as the job would build it and stores and sends nothing; `digest` sets or clears one business's own switch without
a trace on the owner's surfaces; `email-status` lets mail go to an address again; `digest-seed` gives a dev business a
day of numbers to report, and refuses on prod and on a business whose ads really run."""

from datetime import UTC, date, datetime

from agent import business, config, digest_audience, digest_zone, ops, tools_account
from agent.tests import digest_fixtures as df

DAY = date(2026, 10, 5)  # a Monday: the report of the 5th goes out on Tuesday, a daily edition
TABLES = ("agent_action", "digest", "email_thread", "email_message", "insight")


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = ops.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}


def _live(conn, biz: str = "biz_1") -> None:
    df.seed_business(conn, biz, zone="America/Los_Angeles")
    df.seed_plan(conn, biz, {"google_search": 600})
    df.seed_channel(conn, biz, launched_at=datetime(2026, 9, 1, tzinfo=UTC))
    df.seed_metric(conn, biz, DAY, cost_usd=23.10, clicks=41, calls=2)


def test_digest_preview_stores_and_sends_nothing(conn, digest_env, capsys):
    _live(conn)
    before, row = _counts(conn), business.row(conn, "biz_1")
    flags = conn.execute("SELECT operator_flags, time_zone FROM business WHERE id = 'biz_1'").fetchone()
    code, out, _ = _run(capsys, "digest-preview", "biz_1", "--day", DAY.isoformat())
    assert code == 0 and out.startswith("subject: ") and "$23.10" in out
    assert _counts(conn) == before and business.row(conn, "biz_1") == row
    assert conn.execute("SELECT operator_flags, time_zone FROM business WHERE id = 'biz_1'").fetchone() == flags
    assert digest_env.requests == []


def test_digest_preview_weekly_edition(conn, digest_env, capsys):
    _live(conn)
    code, out, _ = _run(capsys, "digest-preview", "biz_1", "--day", DAY.isoformat(), "--edition", "weekly", "--no-note")
    assert code == 0 and out.startswith("subject: ")


def test_digest_flag_set_and_clear(conn, digest_env, capsys):
    df.seed_business(conn, "biz_1")
    assert _run(capsys, "digest", "biz_1", "on")[0] == 0
    assert conn.execute("SELECT operator_flags FROM business").fetchone()["operator_flags"] == {"digest": "on"}
    assert _run(capsys, "digest", "biz_1", "clear")[0] == 0
    assert conn.execute("SELECT operator_flags FROM business").fetchone()["operator_flags"] == {}
    audits = conn.execute("SELECT business_id, args_redacted FROM agent_action WHERE tool = 'ops.digest'").fetchall()
    assert [(a["business_id"], a["args_redacted"]) for a in audits] == [
        (None, {"business": "biz_1", "value": "on"}),
        (None, {"business": "biz_1", "value": "clear"}),
    ]
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE business_id = 'biz_1'").fetchone()["n"] == 0
    code, _, err = _run(capsys, "digest", "biz_9", "on")
    assert code == 1 and "no business" in err


def test_email_status_ok(conn, capsys):
    df.seed_business(conn, "biz_1")
    conn.execute("UPDATE business SET notify_email_status = 'bounced'")
    assert _run(capsys, "email-status", "biz_1", "ok")[0] == 0
    assert conn.execute("SELECT notify_email_status FROM business").fetchone()["notify_email_status"] == "ok"
    [row] = conn.execute("SELECT * FROM agent_action WHERE tool = 'ops.email_status'").fetchall()
    assert row["business_id"] == "biz_1"
    assert tools_account.sentence(row, None) == "An operator let Admiry send e-mails to the address again"


def test_digest_seed_makes_a_business_eligible(conn, capsys):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    df.seed_plan(conn, "biz_1", {"google_search": 600})
    code, _, _ = _run(
        capsys,
        "digest-seed",
        "biz_1",
        "--day",
        DAY.isoformat(),
        "--spend",
        "23.10",
        "--clicks",
        "41",
        "--calls",
        "2",
        "--leads",
        "1",
    )
    assert code == 0
    zone = digest_zone.zone_of(conn, "biz_1")
    aud = digest_audience.audience(conn, {"id": "biz_1", "paused_reason": None, "digest_frequency": "daily"}, DAY, zone)
    assert aud.edition == "daily"
    [ch] = conn.execute("SELECT state, resources FROM channel_campaign").fetchall()
    assert ch["state"] == "live" and "campaign" not in ch["resources"]  # launch and sync leave it alone
    m = conn.execute("SELECT cost_micros, clicks, calls, source FROM metric_daily").fetchone()
    assert (m["cost_micros"], m["clicks"], m["calls"], m["source"]) == (23_100_000, 41, 2, "operator")
    start, end = digest_zone.bounds(DAY, zone)
    [lead] = conn.execute("SELECT kind, channel, fields, created_at FROM lead").fetchall()
    assert (lead["kind"], lead["channel"], lead["fields"]) == ("form", "google_search", {})
    assert start <= lead["created_at"] < end
    # seeding the same day again replaces its numbers and keeps the one channel row
    assert _run(capsys, "digest-seed", "biz_1", "--day", DAY.isoformat(), "--spend", "5")[0] == 0
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 1
    assert conn.execute("SELECT cost_micros FROM metric_daily").fetchone()["cost_micros"] == 5_000_000
    audits = conn.execute("SELECT business_id FROM agent_action WHERE tool = 'ops.digest_seed'").fetchall()
    assert len(audits) == 2 and all(a["business_id"] is None for a in audits)


def test_digest_seed_paused_state(conn, capsys):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    assert _run(capsys, "digest-seed", "biz_1", "--day", DAY.isoformat(), "--state", "paused:no_funds")[0] == 0
    ch = conn.execute("SELECT state, paused_reason FROM channel_campaign").fetchone()
    assert (ch["state"], ch["paused_reason"]) == ("paused", "no_funds")
    code, _, err = _run(capsys, "digest-seed", "biz_1", "--day", DAY.isoformat(), "--state", "asleep")
    assert code == 1 and "--state" in err


def test_digest_seed_refuses_on_prod(conn, capsys, monkeypatch):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    monkeypatch.setenv("AGENT_ENV", "prod")
    assert config.load().env == "prod"
    code, _, err = _run(capsys, "digest-seed", "biz_1", "--day", DAY.isoformat(), "--spend", "23.10")
    assert code == 1 and "prod" in err
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM metric_daily").fetchone()["n"] == 0


def test_digest_seed_refuses_a_real_campaign(conn, capsys):
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    df.seed_channel(conn, "biz_1", resources={"campaign": "customers/1/campaigns/2"})
    code, _, err = _run(capsys, "digest-seed", "biz_1", "--day", DAY.isoformat(), "--spend", "23.10")
    assert code == 1 and "real" in err
    df.seed_business(conn, "biz_2", zone="America/Los_Angeles")
    df.seed_metric(conn, "biz_2", date(2026, 10, 1), cost_usd=1.0, source="api")
    code, _, err = _run(capsys, "digest-seed", "biz_2", "--day", DAY.isoformat(), "--spend", "23.10")
    assert code == 1 and "real" in err
    assert conn.execute("SELECT count(*) AS n FROM metric_daily WHERE source = 'operator'").fetchone()["n"] == 0
