"""run()'s prepare step, the approval columns and the anonymous path, exercised without HTTP by
standing in for the request headers (auth.require reads them through request_headers)."""

import pytest
from fastmcp.exceptions import ToolError
from psycopg.pq import TransactionStatus

from agent import audit, auth, envelope, keys, mcp_app, tools_account

# run()'s refusal of an idempotency key the index could not hold (final review A P2)
TOO_LONG = "idempotency_key is too long (over 200 characters); send a shorter unique key"


def _business(conn, biz_id="biz_1"):
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz_id,))


def _as_key(monkeypatch, plaintext: str, ip: str = "203.0.113.5"):
    monkeypatch.setattr(
        mcp_app.auth, "request_headers", lambda: {"authorization": f"Bearer {plaintext}", "x-forwarded-for": ip}
    )


def _as_ip(monkeypatch, ip: str):
    monkeypatch.setattr(mcp_app.auth, "request_headers", lambda: {"x-forwarded-for": ip})


def _minted(conn):
    _business(conn)
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="muse")
    return plaintext


def test_prepare_runs_outside_the_transaction_and_feeds_the_body(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    seen = {}

    def prepare(c, p):
        seen["prepare_tx"] = c.info.transaction_status
        return {"token": 42}

    def body(c, p, ctx):
        seen["ctx"], seen["body_tx"] = ctx, c.info.transaction_status
        return {"ok": True}

    assert mcp_app.run("whoami", "read", {}, body, prepare=prepare)["data"] == {"ok": True}
    assert seen["ctx"] == {"token": 42}
    assert seen["prepare_tx"] == TransactionStatus.IDLE and seen["body_tx"] == TransactionStatus.INTRANS


def test_a_failing_prepare_is_audited_and_the_body_never_runs(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))

    def prepare(c, p):
        raise ToolError("the assistant is unavailable")

    def body(c, p, ctx):
        raise AssertionError("body must not run")

    with pytest.raises(ToolError, match="unavailable"):
        mcp_app.run("whoami", "read", {}, body, prepare=prepare)
    row = conn.execute("SELECT ok, result FROM agent_action WHERE tool = 'whoami'").fetchone()
    assert row["ok"] is False and row["result"] == {"error": "the assistant is unavailable"}


def test_result_approval_lands_in_the_audit_columns(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    result = mcp_app.run(
        "whoami", "read", {}, lambda c, p: {"confirmed": "pr_1", "approval": {"kind": "code", "ref": "pr_1"}}
    )
    assert result["data"]["confirmed"] == "pr_1"
    row = conn.execute("SELECT approval_kind, approval_ref FROM agent_action WHERE tool = 'whoami'").fetchone()
    assert (row["approval_kind"], row["approval_ref"]) == ("code", "pr_1")


def test_run_anonymous_is_limited_per_address_and_audited_as_ip(conn, monkeypatch):
    _as_ip(monkeypatch, "198.51.100.7")
    calls = []

    def prepare(c, ip):
        calls.append(ip)
        return len(calls)

    def body(c, ip, n):
        c.execute("INSERT INTO business (id) VALUES (%s)", (f"biz_{n}",))
        # `start` returns its own envelope (envelope.EXPLICIT): a plain dict has no SAY[tool] entry to wrap it
        return envelope.make({"business_id": f"biz_{n}", "draft_key": "ak_" + "f" * 40}, say="Started.")

    for i in range(1, auth.START_PER_HOUR + 1):
        assert (
            mcp_app.run_anonymous("start", {"text": "x"}, prepare=prepare, body=body)["data"]["business_id"]
            == f"biz_{i}"
        )
    with pytest.raises(auth.RateLimited, match="retry_after="):
        mcp_app.run_anonymous("start", {"text": "x"}, prepare=prepare, body=body)
    assert len(calls) == auth.START_PER_HOUR  # the refused call never reached prepare
    rows = conn.execute(
        "SELECT actor, actor_class, business_id, ok, result FROM agent_action WHERE tool = 'start' ORDER BY id"
    ).fetchall()
    assert [(r["actor"], r["actor_class"], r["business_id"], r["ok"]) for r in rows] == [
        ("ip:198.51.100.7", "anonymous", f"biz_{i}", True) for i in (1, 2, 3)
    ]
    assert all(r["result"]["data"]["draft_key"] == "***" for r in rows)  # the key is never stored


def test_an_unexpected_exception_in_run_leaves_an_internal_error_row(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))

    def body(c, p):
        c.execute("INSERT INTO business (id) VALUES ('biz_partial')")  # rolled back with the failure
        raise RuntimeError("boom: a detail the caller must not see")

    with pytest.raises(RuntimeError, match="boom"):
        mcp_app.run("whoami", "read", {}, body)
    rows = conn.execute("SELECT ok, result, business_id, api_key_id FROM agent_action WHERE tool = 'whoami'").fetchall()
    assert [(r["ok"], r["result"], r["business_id"]) for r in rows] == [(False, {"error": "internal error"}, "biz_1")]
    assert rows[0]["api_key_id"] is not None  # attributed to the key, like a ToolError
    assert conn.execute("SELECT count(*) AS c FROM business WHERE id = 'biz_partial'").fetchone()["c"] == 0


def test_an_unexpected_exception_in_run_anonymous_leaves_a_row_that_counts(conn, monkeypatch):
    _as_ip(monkeypatch, "198.51.100.11")

    def body(c, ip, ctx):
        raise RuntimeError("boom")

    for _ in range(auth.START_PER_HOUR):
        with pytest.raises(RuntimeError, match="boom"):
            mcp_app.run_anonymous("start", {"text": "x"}, prepare=lambda c, ip: None, body=body)
    with pytest.raises(auth.RateLimited):
        mcp_app.run_anonymous("start", {"text": "x"}, prepare=lambda c, ip: None, body=body)
    rows = conn.execute("SELECT ok, result, business_id, actor FROM agent_action WHERE tool = 'start'").fetchall()
    assert [(r["ok"], r["result"], r["business_id"], r["actor"]) for r in rows] == [
        (False, {"error": "internal error"}, None, "ip:198.51.100.11")
    ] * auth.START_PER_HOUR


def test_a_nul_in_the_args_still_leaves_the_failure_row(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))

    def body(c, p):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        mcp_app.run("whoami", "read", {"text": "\x00dentist", "tags": ["a\x00", {"k\x00": "v\x00"}]}, body)
    row = conn.execute("SELECT ok, result, args_redacted FROM agent_action WHERE tool = 'whoami'").fetchone()
    assert (row["ok"], row["result"], row["args_redacted"]["text"]) == (False, {"error": "internal error"}, "dentist")
    assert "\\u0000" not in str(row["args_redacted"]) and '"k": "v"' in row["args_redacted"]["tags"]


def test_a_nul_idempotency_key_is_stored_stripped_and_replays(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    args = {"idempotency_key": "k\x00"}
    first = mcp_app.run("whoami", "read", args, lambda c, p: {"n": 1}, idempotency_key="k\x00")
    again = mcp_app.run("whoami", "read", args, lambda c, p: {"n": 2}, idempotency_key="k\x00")
    assert first == again and first["data"] == {"n": 1}
    keys_stored = [r["idempotency_key"] for r in conn.execute("SELECT idempotency_key FROM agent_action ORDER BY id")]
    assert keys_stored == ["k", None]  # the original and its replay


def test_an_unwritable_failure_row_falls_back_to_a_minimal_one(conn, monkeypatch):
    _as_key(monkeypatch, _minted(conn))
    real = audit.record

    def picky(c, **kw):
        if kw["args"]:
            raise ValueError("cannot store these args")
        return real(c, **kw)

    monkeypatch.setattr(mcp_app.audit, "record", picky)

    def body(c, p):
        raise ToolError("refused")

    with pytest.raises(ToolError, match="refused"):
        mcp_app.run("whoami", "read", {"text": "x"}, body)
    rows = conn.execute("SELECT ok, result, args_redacted, api_key_id FROM agent_action").fetchall()
    assert [(r["ok"], r["result"], r["args_redacted"]) for r in rows] == [(False, {"error": "internal error"}, {})]
    assert rows[0]["api_key_id"] is not None  # same attribution


def test_when_no_row_can_be_written_the_original_error_propagates(conn, monkeypatch):
    _as_ip(monkeypatch, "198.51.100.12")

    def broken(c, **kw):
        raise ValueError("audit table unavailable")

    monkeypatch.setattr(mcp_app.audit, "record", broken)

    def body(c, ip, ctx):
        raise ToolError("could not read that website")

    with pytest.raises(ToolError, match="could not read that website"):
        mcp_app.run_anonymous("start", {"text": "x"}, prepare=lambda c, ip: None, body=body)


def test_run_anonymous_audits_failures_and_counts_them_toward_the_limit(conn, monkeypatch):
    _as_ip(monkeypatch, "198.51.100.8")

    def body(c, ip, ctx):
        raise ToolError("could not read that website")

    for _ in range(auth.START_PER_HOUR):
        with pytest.raises(ToolError, match="website"):
            mcp_app.run_anonymous("start", {"text": "x"}, prepare=lambda c, ip: None, body=body)
    with pytest.raises(auth.RateLimited):
        mcp_app.run_anonymous("start", {"text": "x"}, prepare=lambda c, ip: None, body=body)
    n = conn.execute("SELECT count(*) AS c FROM agent_action WHERE tool = 'start' AND NOT ok").fetchone()["c"]
    assert n == auth.START_PER_HOUR


def _done(c, p, ctx=None) -> dict:
    return envelope.make({"ok": True}, say="Done.")


def _changes(conn, tool: str, n: int) -> None:
    """`n` successful calls of `tool` this hour for biz_1, each with its own idempotency key, as run() records them."""
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, tool, args_hash, idempotency_key) "
        "SELECT 'biz_1', 'key:another', %s, 'h', %s || g FROM generate_series(1, %s) g",
        (tool, f"{tool}-", n),
    )


def test_the_hourly_change_limit_counts_only_the_changes_it_limits(conn, monkeypatch):
    """Final review A I1: the limit applies to the manage-scope commands other than a pause, and it counts only theirs.
    Leads recorded or marked, a support request, a revoked key, pauses -- 30 of each -- use up none of it, so the
    owner's change still goes through. Thirty changes it counts refuse the next one, in its own words; a pause still
    runs."""
    _as_key(monkeypatch, _minted(conn))
    for tool in ("add_lead", "update_lead", "contact_support", "revoke_key", "pause"):
        _changes(conn, tool, auth.MUTATIONS_PER_HOUR)
    assert mcp_app.run("adjust", "manage", {}, _done, idempotency_key="change-1")["data"] == {"ok": True}
    _changes(conn, "change_goal", auth.MUTATIONS_PER_HOUR - 1)  # thirty with the change above
    with pytest.raises(auth.RateLimited, match=r"^rate limit: 30 changes per hour per business; retry_after=\d+$"):
        mcp_app.run("adjust", "manage", {}, _done, idempotency_key="change-2")
    assert mcp_app.run("pause", "manage", {}, _done, idempotency_key="pause-now")["data"] == {"ok": True}


def test_a_keys_own_denial_never_spends_the_businesss_model_calls(conn, monkeypatch):
    """Final review B (security, minor): a key refused a model command -- it lacks the scope, or it was revoked -- is
    recorded as denied and is not one of the business's model calls, so no such key can use up the owner's day. The
    owner holds a setup key here (its cap is 10): twelve denials of each kind, and the owner's own call still drafts.
    The activity log says what it said before."""
    _business(conn)
    owner = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")[0]
    reader, read_only = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    revoked, gone = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="chatgpt")
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE id = %s", (gone.id,))
    lacks = "this key lacks the 'manage' scope; keys are issued with scopes on /connect"
    for key, refusal in ((reader, lacks), (revoked, "this key was revoked; get a new one at /connect")):
        _as_key(monkeypatch, key)
        for _ in range(12):
            with pytest.raises(auth.Denied, match=refusal):
                mcp_app.run("revise_plan", "manage", {}, _done, prepare=auth.check_llm_cap)
    rows = conn.execute(
        "SELECT actor, actor_class, tool, ok, result, args_redacted, api_key_id FROM agent_action ORDER BY id"
    ).fetchall()
    assert [r["result"] for r in rows] == [{"error": lacks, "denied": True}] * 12 + [
        {"error": "this key was revoked; get a new one at /connect", "denied": True}
    ] * 12
    # the marker changes nothing the owner reads: the sentence of a refused call, as before
    assert tools_account.sentence(rows[0], "claude") == f"Claude tried to revise the plan, but: {lacks}"
    assert rows[0]["api_key_id"] == read_only.id
    _as_key(monkeypatch, owner)
    assert mcp_app.run("revise_plan", "manage", {}, _done, prepare=auth.check_llm_cap)["data"] == {"ok": True}


def test_a_model_commands_own_failure_still_counts_against_the_day(conn, monkeypatch):
    """The other side: a model command that ran and failed in its own words (a ToolError, not a denial) spent its
    call, as before -- ten of them use up a setup key's day."""
    _business(conn)
    _as_key(monkeypatch, keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")[0])

    def unread(c, p):
        auth.check_llm_cap(c, p)
        raise ToolError("the model could not read that")

    for _ in range(auth.LLM_CALLS_PER_DAY_DRAFT):
        with pytest.raises(ToolError, match="could not read that"):
            mcp_app.run("revise_plan", "manage", {}, _done, prepare=unread)
    with pytest.raises(ToolError, match="today's limit of 10 drafts for this business is reached"):
        mcp_app.run("revise_plan", "manage", {}, _done, prepare=unread)


def test_an_idempotency_key_over_200_characters_is_refused_before_anything_runs(conn, monkeypatch):
    """Final review A P2: a key too long for the idempotency index let a command that acts in `prepare` act, then fail
    to store its success and record "internal error". Over 200 characters it is refused beside the empty key, before
    the replay lookup and before `prepare`, and the refusal is recorded in its own words; 200 characters pass."""
    _as_key(monkeypatch, _minted(conn))
    ran = []

    def prepare(c, p):
        ran.append("prepare")

    def body(c, p, ctx):
        ran.append("body")
        return _done(c, p)

    long = "k" * 201
    with pytest.raises(ToolError) as refused:
        mcp_app.run("pause", "manage", {"idempotency_key": long}, body, idempotency_key=long, prepare=prepare)
    assert str(refused.value) == TOO_LONG and ran == []
    row = conn.execute("SELECT tool, ok, result, idempotency_key FROM agent_action").fetchone()
    assert (row["tool"], row["ok"], row["result"], row["idempotency_key"]) == (
        "pause",
        False,
        {"error": TOO_LONG},
        None,
    )
    fits = "k" * 200
    done = mcp_app.run("pause", "manage", {"idempotency_key": fits}, body, idempotency_key=fits, prepare=prepare)
    assert done["data"] == {"ok": True} and ran == ["prepare", "body"]
