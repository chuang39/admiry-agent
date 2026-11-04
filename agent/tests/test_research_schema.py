"""The research_run table (capability layer K4, spec 2026-10-07 §8.1): one row per research job, its trigger, mode and
status checked by the database; a run outlives its job and goes with its business."""

import psycopg
import pytest

ROW = (
    "INSERT INTO research_run (id, job_id, business_id, vertical, trigger, mode, status) "
    "VALUES (%s, %s, 'biz_1', 'dentists', %s, %s, %s)"
)


def _job(conn) -> None:
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    conn.execute(
        "INSERT INTO agent_job (id, business_id, role, kind) VALUES ('aj_1', 'biz_1', 'researcher', 'business')"
    )


def test_one_run_per_job(conn):
    _job(conn)
    conn.execute(ROW, ("rr_1", "aj_1", "auto", "validate", "building"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(ROW, ("rr_2", "aj_1", "auto", "validate", "building"))


@pytest.mark.parametrize(
    ("trigger", "mode", "status"),
    [("cron", "validate", "building"), ("auto", "off", "building"), ("auto", "on", "skipped")],
)
def test_the_database_refuses_an_unknown_trigger_mode_or_status(conn, trigger, mode, status):
    _job(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ROW, ("rr_1", "aj_1", trigger, mode, status))


def test_a_run_outlives_its_job_and_goes_with_its_business(conn):
    _job(conn)
    conn.execute(ROW, ("rr_1", "aj_1", "owner", "on", "done"))
    conn.execute("DELETE FROM agent_job")
    assert conn.execute("SELECT job_id FROM research_run").fetchone()["job_id"] is None
    conn.execute("DELETE FROM business")
    assert conn.execute("SELECT count(*) AS n FROM research_run").fetchone()["n"] == 0
