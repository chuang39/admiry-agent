"""`python -m agent.ops insight` and `insight-rebuild` (plan 2026-10-07-agent-k2 Task 16): the operator reads a day's
look as the team sees it -- every grade, flag, candidate and dropped sentence -- and on dev builds it again. Both leave
one audit row with no business on it, so nothing of them reaches an owner's activity log."""

from agent import ops
from agent.tests import digest_fixtures as df
from agent.tests.fixtures import insight_fixture


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = ops.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _owner_rows(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM agent_action WHERE business_id IS NOT NULL").fetchone()["n"]


def test_insight_prints_the_team_view(conn, analyst_biz, monkeypatch, capsys):
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    owner_rows = _owner_rows(conn)
    code, out, _ = _run(capsys, "insight", "biz_1")
    assert code == 0
    assert f"insight {row['id']} 2026-10-05" in out and "verdict=deliver" in out and "status=done" in out
    assert f"summary: {insight_fixture().summary}" in out
    for c in row["metrics"]["candidates"]:
        assert c["id"] in out
    assert _owner_rows(conn) == owner_rows
    [audit] = conn.execute("SELECT business_id, args_redacted FROM agent_action WHERE tool = 'ops.insight'").fetchall()
    assert audit["business_id"] is None and audit["args_redacted"]["business"] == "biz_1"


def test_insight_names_the_day_asked_and_says_when_there_is_none(conn, analyst_biz, monkeypatch, capsys):
    df.fake_llm(monkeypatch)
    df.analyst_build(conn)
    assert _run(capsys, "insight", "biz_1", "--day", "2026-10-05")[0] == 0
    code, _, err = _run(capsys, "insight", "biz_1", "--day", "2026-10-01")
    assert code == 1 and "no look" in err


def test_insight_rebuild_refuses_on_prod(conn, analyst_biz, monkeypatch, capsys):
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    monkeypatch.setenv("AGENT_ENV", "prod")
    code, _, err = _run(capsys, "insight-rebuild", "biz_1", "--day", "2026-10-05")
    assert code != 0 and "prod" in err
    again = conn.execute("SELECT id, created_at FROM insight WHERE business_id = 'biz_1'").fetchone()
    assert (again["id"], again["created_at"]) == (row["id"], row["created_at"])
    assert (
        conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.insight_rebuild'").fetchone()["n"] == 0
    )


def test_insight_rebuild_builds_again(conn, analyst_biz, monkeypatch, capsys):
    gen = df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    code, out, _ = _run(capsys, "insight-rebuild", "biz_1", "--day", "2026-10-05")
    assert code == 0 and len(gen.insight_calls) == 2
    new = conn.execute("SELECT id FROM insight WHERE business_id = 'biz_1'").fetchone()
    assert new["id"] != row["id"] and f"insight {new['id']} 2026-10-05" in out
    [audit] = conn.execute(
        "SELECT business_id, args_redacted FROM agent_action WHERE tool = 'ops.insight_rebuild'"
    ).fetchall()
    assert audit["business_id"] is None and audit["args_redacted"] == {"business": "biz_1", "day": "2026-10-05"}
