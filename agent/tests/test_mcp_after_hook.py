"""run()'s `after` hook (spec 2026-09-30 D142): called once the transaction has committed, with no connection held;
never for a replay or a failure; an exception in it is logged, not raised. Exercised without HTTP, by standing in for
the request headers (auth.require reads them through request_headers), like test_run.py."""

import logging

import pytest
from fastmcp.exceptions import ToolError

from agent import envelope, keys, mcp_app


def _as_key(monkeypatch, plaintext: str) -> None:
    monkeypatch.setattr(
        mcp_app.auth,
        "request_headers",
        lambda: {"authorization": f"Bearer {plaintext}", "x-forwarded-for": "203.0.113.5"},
    )


def _minted(conn) -> str:
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="muse")
    return plaintext


def _done(c, p) -> dict:
    return envelope.make({"x": 1}, say="Done.")


def test_after_runs_once_with_the_result_after_the_commit_and_holds_no_connection(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    opened = []
    real_connect = mcp_app.db.connect

    def tracked(url=None):  # the connections run() itself opens
        opened.append(real_connect(url))
        return opened[-1]

    monkeypatch.setattr(mcp_app.db, "connect", tracked)
    seen = []

    def after(result):
        # `conn` is another connection: it sees the body's audit row only once that transaction has committed
        n = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'whoami' AND ok").fetchone()["n"]
        seen.append((result, n, [c.closed for c in opened]))

    result = mcp_app.run("whoami", "read", {}, _done, after=after)
    assert len(seen) == 1
    got, committed, closed = seen[0]
    assert got is result and got["data"] == {"x": 1}
    assert committed == 1  # the body and its audit row were committed before the hook ran
    assert closed == [True]  # run()'s own connection was already closed: mail I/O holds neither it nor its lock


def test_after_is_not_called_for_a_replay(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    bodies, hooks = [], []

    def body(c, p):
        bodies.append(1)
        return _done(c, p)

    first = mcp_app.run("whoami", "read", {}, body, idempotency_key="k1", after=hooks.append)
    again = mcp_app.run("whoami", "read", {}, body, idempotency_key="k1", after=hooks.append)
    assert again["data"] == first["data"] and len(bodies) == 1  # the second call was answered from the audit row
    assert len(hooks) == 1  # nothing new was committed, so nothing for the hook to follow up


def test_after_is_not_called_for_a_failure(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    hooks = []

    def refused(c, p):
        raise ToolError("nope")

    def crashed(c, p):
        raise RuntimeError("boom")

    with pytest.raises(ToolError):
        mcp_app.run("whoami", "read", {}, refused, after=hooks.append)
    with pytest.raises(RuntimeError):
        mcp_app.run("whoami", "read", {}, crashed, after=hooks.append)
    assert hooks == []  # both rolled back: no committed change to follow up


def test_a_failing_after_is_logged_and_the_call_still_returns_its_result(conn, monkeypatch, caplog):
    _as_key(monkeypatch, _minted(conn))

    def after(result):
        raise RuntimeError("mail is down")

    with caplog.at_level(logging.WARNING, logger="agent.mcp_app"):
        result = mcp_app.run("whoami", "read", {}, _done, after=after)
    assert result["data"] == {"x": 1}  # returned, not raised: the change is committed
    assert conn.execute("SELECT ok FROM agent_action WHERE tool = 'whoami'").fetchone()["ok"] is True
    logged = [r for r in caplog.records if "after-commit step of whoami failed" in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info is not None  # with its traceback
