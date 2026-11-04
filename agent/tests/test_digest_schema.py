"""The digest's and the Analyst's tables (digest spec 2026-09-30 §11; capability layer K2 §8.1; plan
2026-10-07-agent-k2 Task 1): applied twice without harm, one digest and one insight per business and day, the
frequency and the thread kind defaulted and checked."""

import psycopg
import pytest

from agent import db


def test_schema_applies_twice(conn):
    db.apply_schema(conn)
    db.apply_schema(conn)
    found = conn.execute(
        "SELECT to_regclass('digest') IS NOT NULL AS d, to_regclass('insight') IS NOT NULL AS i"
    ).fetchone()
    assert found == {"d": True, "i": True}


def _digest(conn, id_: str, day: str = "2026-10-05") -> None:
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode) "
        "VALUES (%s, 'biz_1', %s, 'daily', 'America/Los_Angeles', 'on')",
        (id_, day),
    )


def test_one_digest_per_business_and_day(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    _digest(conn, "dg_1")
    assert conn.execute("SELECT status, attempts FROM digest").fetchone() == {"status": "building", "attempts": 0}
    with pytest.raises(psycopg.errors.UniqueViolation):
        _digest(conn, "dg_2")


def test_digest_frequency_defaults_to_daily_and_checks(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    assert conn.execute("SELECT digest_frequency, time_zone FROM business").fetchone() == {
        "digest_frequency": "daily",
        "time_zone": None,
    }
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE business SET digest_frequency = 'hourly'")


def test_email_thread_kind_defaults_to_conversation(conn):
    conn.execute("INSERT INTO email_thread (id, counterpart, token) VALUES ('et_1', 'a@example.com', 'tok')")
    assert conn.execute("SELECT kind FROM email_thread").fetchone()["kind"] == "conversation"


def test_one_insight_per_business_and_day(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status) VALUES ('in_1', 'biz_1', '2026-10-05', 'on', 'done')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO insight (id, business_id, day, mode, status) "
            "VALUES ('in_2', 'biz_1', '2026-10-05', 'on', 'done')"
        )


def test_insight_mode_and_status_are_checked(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    for mode, status in (("off", "done"), ("on", "sent")):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO insight (id, business_id, day, mode, status) "
                "VALUES ('in_x', 'biz_1', '2026-10-05', %s, %s)",
                (mode, status),
            )
