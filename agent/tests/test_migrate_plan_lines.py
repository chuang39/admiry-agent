"""The plan's optimization line promises no e-mail (Dg plan P8, founder 2026-09-30; plan 2026-10-07-agent-k2 Task 14):
`plan_lines_without_weekly_summary` rewrites the stored line in every plan and in an open approve-plan proposal's
`what_changes`, and leaves every other line and every closed proposal alone."""

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from agent import migrate

NAME = "plan_lines_without_weekly_summary"
OLD = "Weekly summary by e-mail · automatic optimization: safe changes only"
NEW = "Automatic optimization: safe changes only"
OTHER = "Local SEO/GEO: $0"


def _plan(conn, pid: str, line: str) -> None:
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json) VALUES (%s, 'biz_1', %s, %s)",
        (pid, int(pid[-1]), Jsonb({"sections": {}, "lines": {"optimization": line, "discovery": OTHER}})),
    )


def _proposal(conn, pid: str, status: str) -> None:
    conn.execute(
        "INSERT INTO proposal (id, business_id, class, kind, payload, summary, status, expires_at) "
        "VALUES (%s, 'biz_1', 'spend', 'approve_plan', %s, 'x', %s, %s)",
        (pid, Jsonb({"what_changes": [OTHER, OLD]}), status, datetime.now(UTC) + timedelta(days=1)),
    )


def test_the_stored_line_is_rewritten_once_and_nothing_else(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    _plan(conn, "pl_1", OLD)
    _plan(conn, "pl_2", "Weekly summary by e-mail · automatic optimization: off")
    _proposal(conn, "pr_open", "proposed")
    _proposal(conn, "pr_done", "confirmed")
    migrate.MIGRATIONS[NAME](conn)
    lines = {r["id"]: r["json"]["lines"] for r in conn.execute("SELECT id, json FROM plan ORDER BY id")}
    assert lines["pl_1"] == {"optimization": NEW, "discovery": OTHER}
    assert lines["pl_2"]["optimization"] == "Automatic optimization: off"
    payloads = {r["id"]: r["payload"] for r in conn.execute("SELECT id, payload FROM proposal")}
    assert payloads["pr_open"]["what_changes"] == [OTHER, NEW]
    assert payloads["pr_done"]["what_changes"] == [OTHER, OLD]
    migrate.MIGRATIONS[NAME](conn)  # a second run changes nothing
    assert conn.execute("SELECT json FROM plan WHERE id = 'pl_1'").fetchone()["json"]["lines"]["optimization"] == NEW
