from fastmcp.exceptions import ToolError

from agent import audit, auth, db, envelope, keys, mcp_app
from agent.tests.conftest import TEST_DB_URL, call_tool, rpc, rpc_raw, tool_text
from agent.tests.fixtures import business_with_key


def _business(conn, biz_id="biz_1", claim_state="draft"):
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, %s)", (biz_id, claim_state))


def test_tools_list_exposes_the_account_tools_with_llm_descriptions(client, conn):
    key = business_with_key(conn)  # every /mcp request needs a bearer now (A1b-4b Task 2), tools/list included
    tools = {t["name"]: t for t in rpc(client, "tools/list", key=key)["result"]["tools"]}
    assert {"whoami", "list_keys", "revoke_key"} <= set(tools)
    assert tools["whoami"]["description"].startswith("Who is calling")
    assert tools["revoke_key"]["inputSchema"]["required"] == ["key_id", "idempotency_key"]
    assert tools["whoami"]["annotations"]["readOnlyHint"] is True
    assert tools["revoke_key"]["annotations"]["readOnlyHint"] is False


def _destructive_hint(tool) -> bool:
    """A FunctionTool's `.annotations` is a ToolAnnotations pydantic model in this fastmcp
    version, but read it defensively in case a future version exposes it as a dict."""
    ann = tool.annotations
    return ann.destructiveHint if hasattr(ann, "destructiveHint") else ann["destructiveHint"]


def test_revoke_key_is_marked_destructive_and_whoami_is_not():
    by_name = {m.name: m.tool for m in mcp_app.REGISTRY}
    assert _destructive_hint(by_name["revoke_key"]) is True
    assert _destructive_hint(by_name["whoami"]) is False


def test_whoami_without_a_key_is_a_tool_error(client):
    # FastMCP's auth middleware now refuses a keyless /mcp request outright (A1b-4b Task 2): 401, never
    # a JSON-RPC result. See test_oauth_http.test_missing_bearer_is_401_with_resource_metadata for the body.
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}).status_code == 401


def test_a_keyless_call_is_refused_before_any_database_connection(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("run() opened a database connection for a keyless call")

    monkeypatch.setattr(mcp_app.db, "connect", unreachable)
    # The SDK's auth middleware refuses this before it ever reaches FastMCP's tool dispatch (let alone
    # run()), so db.connect being unreachable is never exercised here -- it would raise instead of a
    # clean 401 if it were.
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}).status_code == 401


def test_whoami_with_a_business_key_and_the_audit_row(client, conn):
    _business(conn)
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="muse")
    res = call_tool(client, "whoami", key=plaintext)
    assert not res["isError"], tool_text(res)
    sc = res["structuredContent"]["data"]
    assert sc["business"] == {
        "id": "biz_1",
        "slug": None,
        "claim_state": "draft",
        "status": "active",
        "locale": "en-US",
    }
    assert sc["key"] == {
        "id": rec.id,
        "class": "business",
        "scopes": ["read", "manage"],
        "label": "muse",
        "expires_at": None,
    }
    assert sc["connect_url"] == "http://testserver/connect"
    row = conn.execute(
        "SELECT business_id, actor, actor_class, api_key_id, tool, ok, ip, args_hash FROM agent_action"
    ).fetchone()
    assert row["business_id"] == "biz_1" and row["actor"] == f"key:{rec.id}" and row["actor_class"] == "business"
    assert (row["api_key_id"], row["tool"], row["ok"], row["ip"]) == (rec.id, "whoami", True, "local")
    assert len(row["args_hash"]) == 64


def test_x_api_key_header_and_forwarded_ip(client, conn):
    _business(conn)
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    res = call_tool(
        client, "whoami", extra_headers={"X-API-Key": plaintext, "X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    )
    assert not res["isError"]
    assert conn.execute("SELECT ip FROM agent_action").fetchone()["ip"] == "203.0.113.9"


def test_scope_is_enforced_and_denials_of_known_keys_are_audited(client, conn):
    _business(conn)
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    res = call_tool(client, "list_keys", key=plaintext, extra_headers={"X-Forwarded-For": "203.0.113.7"})
    assert res["isError"] and "'account' scope" in tool_text(res)
    # the body never ran, but the refusal is attributed to the key that was refused
    rows = conn.execute(
        "SELECT business_id, actor, actor_class, api_key_id, tool, ok, result, ip, idempotency_key FROM agent_action"
    ).fetchall()
    assert rows == [
        {
            "business_id": "biz_1",
            "actor": f"key:{rec.id}",
            "actor_class": "business",
            "api_key_id": rec.id,
            "tool": "list_keys",
            "ok": False,
            # marked as the key's own denial, which the daily model cap does not count (final review B)
            "result": {"error": tool_text(res), "denied": True},
            "ip": "203.0.113.7",
            "idempotency_key": None,
        }
    ]


def test_keyless_unknown_key_and_rate_limited_calls_write_nothing(client, conn, monkeypatch):
    _business(conn)
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    # missing and unknown keys are now refused by the SDK's auth middleware, before run() ever sees them
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}).status_code == 401
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key="ak_" + "0" * 40).status_code == 401
    assert conn.execute("SELECT count(*) AS c FROM agent_action").fetchone()["c"] == 0  # nothing to attribute
    monkeypatch.setattr(auth, "RATE_PER_MINUTE", 2)
    for _ in range(2):
        assert not call_tool(client, "whoami", key=plaintext)["isError"]
    limited = call_tool(client, "whoami", key=plaintext)
    assert limited["isError"] and "rate limit: 2 calls per minute" in tool_text(limited)
    assert conn.execute("SELECT count(*) AS c FROM agent_action").fetchone()["c"] == 2  # no row for the rejection


def test_a_revoked_key_is_refused_by_the_sdk_and_never_reaches_run(client, conn):
    # Before A1b-4b Task 2, a revoked key's denials were audited (ok=false) and rate-limited by run()'s own
    # auth.require. Now the SDK's load_access_token refuses a revoked key outright (P3): every call is a
    # 401 before run() -- and before any connection it would open -- ever sees it, so nothing is recorded.
    _business(conn)
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE id = %s", (rec.id,))
    for _ in range(3):
        assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=plaintext).status_code == 401
    assert conn.execute("SELECT count(*) AS c FROM agent_action WHERE api_key_id = %s", (rec.id,)).fetchone()["c"] == 0


def test_expired_business_keys_stop_working(client, conn):
    # load_access_token checks the business is active too (Task 1), so this is now a 401 from the SDK,
    # before run() (and its audit row) ever sees the call -- like a revoked key.
    _business(conn)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_1'")
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=plaintext).status_code == 401
    assert conn.execute("SELECT count(*) AS c FROM agent_action WHERE api_key_id = %s", (rec.id,)).fetchone()["c"] == 0


def test_platform_key_passes_every_scope(client, conn):
    plaintext, _ = keys.mint(conn, business_id=None, cls="platform", scopes=("read",), label="ops")
    res = call_tool(client, "whoami", key=plaintext)
    assert not res["isError"] and res["structuredContent"]["data"]["business"] is None
    res = call_tool(client, "list_keys", key=plaintext)
    assert res["isError"] and "not bound to a business" in tool_text(res)


def test_list_keys_never_returns_hashes(client, conn):
    _business(conn)
    admin, arec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    res = call_tool(client, "list_keys", key=admin)
    listed = res["structuredContent"]["data"]["keys"]
    assert [k["id"] for k in listed] == [arec.id] and set(listed[0]) == {
        "id",
        "label",
        "class",
        "scopes",
        "created_at",
        "expires_at",
        "last_used_at",
        "revoked_at",
        "source",
    }


def test_revoke_key_is_idempotent_and_stops_the_key(client, conn):
    _business(conn)
    admin, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    victim, vrec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    first = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": "idem-1"}, key=admin)
    second = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": "idem-1"}, key=admin)
    assert first["structuredContent"]["data"] == second["structuredContent"]["data"] == {"revoked": vrec.id}
    # one row did the revoking; the repeat is recorded as its replay (test_a_replay_is_recorded_...)
    assert (
        conn.execute(
            "SELECT count(*) AS c FROM agent_action WHERE tool = 'revoke_key' AND idempotency_key = 'idem-1'"
        ).fetchone()["c"]
        == 1
    )
    # the revoked key is refused by the SDK now, not by run(): 401, not a 200 isError result
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=victim).status_code == 401
    third = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": "idem-2"}, key=admin)
    assert third["isError"] and "no active key" in tool_text(third)
    failed = conn.execute(
        "SELECT ok, result, idempotency_key FROM agent_action WHERE tool = 'revoke_key' AND NOT ok"
    ).fetchone()
    assert failed["result"]["error"].startswith("no active key") and failed["idempotency_key"] is None


def test_a_replay_is_recorded_as_a_replay_of_the_original_row(client, conn):
    _business(conn)
    admin, arec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, vrec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    args = {"key_id": vrec.id, "idempotency_key": "idem-r"}
    call_tool(client, "revoke_key", args, key=admin)
    again = call_tool(client, "revoke_key", args, key=admin)
    assert not again["isError"] and again["structuredContent"]["data"] == {"revoked": vrec.id}
    original, replay = conn.execute(
        "SELECT id, ok, result, idempotency_key, api_key_id FROM agent_action WHERE tool = 'revoke_key' ORDER BY id"
    ).fetchall()
    assert (original["result"]["data"], original["idempotency_key"]) == ({"revoked": vrec.id}, "idem-r")
    # the repeat counts toward the key's rate limit and points at the row whose result it returned
    assert (replay["ok"], replay["result"], replay["idempotency_key"], replay["api_key_id"]) == (
        True,
        {"replay_of": original["id"]},
        None,
        arec.id,
    )


def test_an_idempotency_key_reused_for_a_different_request_is_refused(client, conn):
    _business(conn)
    admin, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, a = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    _, b = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    first = call_tool(client, "revoke_key", {"key_id": a.id, "idempotency_key": "k"}, key=admin)
    assert first["structuredContent"]["data"] == {"revoked": a.id}
    second = call_tool(client, "revoke_key", {"key_id": b.id, "idempotency_key": "k"}, key=admin)
    message = "idempotency_key already used for a different request; use a new key"
    assert second["isError"] and message in tool_text(second)
    assert conn.execute("SELECT revoked_at FROM api_key WHERE id = %s", (b.id,)).fetchone()["revoked_at"] is None
    refused = conn.execute("SELECT result FROM agent_action WHERE tool = 'revoke_key' AND NOT ok").fetchall()
    assert refused == [{"result": {"error": message}}]


def test_a_tool_error_rolls_back_what_the_body_already_wrote(client, conn, monkeypatch):
    _business(conn)
    admin, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, vrec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    real_revoke = keys.revoke

    def revoke_then_refuse(c, key_id, business_id):
        real_revoke(c, key_id, business_id)  # the body writes on the call's own connection ...
        raise ToolError("refused after writing")  # ... then a later step of it refuses

    monkeypatch.setattr(mcp_app.keys, "revoke", revoke_then_refuse)
    res = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": "idem-rb"}, key=admin)
    assert res["isError"] and "refused after writing" in tool_text(res)
    assert conn.execute("SELECT revoked_at FROM api_key WHERE id = %s", (vrec.id,)).fetchone()["revoked_at"] is None
    rows = conn.execute("SELECT ok, result, idempotency_key FROM agent_action WHERE tool = 'revoke_key'").fetchall()
    assert rows == [{"ok": False, "result": {"error": "refused after writing"}, "idempotency_key": None}]


def test_revoke_key_cannot_touch_another_business(client, conn):
    _business(conn, "biz_1")
    _business(conn, "biz_2")
    admin, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, other = keys.mint(conn, business_id="biz_2", cls="business", scopes=("read",), label="muse")
    res = call_tool(client, "revoke_key", {"key_id": other.id, "idempotency_key": "idem-9"}, key=admin)
    assert res["isError"] and "no active key" in tool_text(res)
    assert conn.execute("SELECT revoked_at FROM api_key WHERE id = %s", (other.id,)).fetchone()["revoked_at"] is None


def test_health_still_served_next_to_the_mcp_mount(client):
    assert client.get("/health").json()["ok"] is True


def test_unexpected_errors_are_masked_but_toolerrors_pass_through_verbatim(client, conn, monkeypatch):
    _business(conn)
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("account",), label="web")

    def boom(conn, business_id):
        raise RuntimeError("host=secret-db user=secret")

    monkeypatch.setattr(mcp_app.keys, "list_for_business", boom)
    res = call_tool(client, "list_keys", key=plaintext)
    assert res["isError"]
    assert "secret" not in tool_text(res)
    # a keyless call is refused by the SDK before it ever reaches a tool (Task 2): 401, not a ToolError
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}).status_code == 401


def test_loser_of_a_race_gets_the_winners_result_not_an_error(client, conn, monkeypatch):
    _business(conn)
    admin, arec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, vrec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    real_revoke = keys.revoke

    def racing_revoke(c, key_id, business_id):
        # A concurrent caller revokes first and records its success -- on its own connection:
        # writes on `c` would roll back with this call's transaction ...
        with db.connect(TEST_DB_URL) as other:
            real_revoke(other, key_id, business_id)
            audit.record(
                other,
                tool="revoke_key",
                args={"key_id": key_id, "idempotency_key": "race-2"},
                result=envelope.wrap("revoke_key", {"revoked": key_id}),
                ok=True,
                business_id=business_id,
                actor="key:competitor",
                actor_class="business",
                api_key_id=None,
                ip="local",
                idempotency_key="race-2",
            )
        return False  # ... so this caller's own attempt finds nothing left to revoke

    monkeypatch.setattr(mcp_app.keys, "revoke", racing_revoke)
    res = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": "race-2"}, key=admin)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"] == {"revoked": vrec.id}
    # two rows: the winner's success, then the loser's replay of it -- no ok=false row for the loser
    winner, loser = conn.execute(
        "SELECT id, actor, ok, result FROM agent_action WHERE tool = 'revoke_key' ORDER BY id"
    ).fetchall()
    assert (winner["actor"], winner["ok"]) == ("key:competitor", True)
    assert (loser["actor"], loser["ok"], loser["result"]) == (f"key:{arec.id}", True, {"replay_of": winner["id"]})


def test_two_winners_collide_on_the_index_and_share_the_first_result(client, conn, monkeypatch):
    _business(conn)
    admin, arec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, vrec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    real_record = audit.record
    state = {"raced": False}

    def racing_record(c, **kw):
        if kw.get("ok") and kw.get("idempotency_key") == "race-3" and not state["raced"]:
            state["raced"] = True  # the competitor's success commits first, on its own connection
            with db.connect(TEST_DB_URL) as other:
                real_record(
                    other,
                    **{
                        **kw,
                        "actor": "key:competitor",
                        "result": envelope.wrap("revoke_key", {"revoked": "competitor-won"}),
                    },
                )
        return real_record(c, **kw)

    monkeypatch.setattr(mcp_app.audit, "record", racing_record)
    res = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": "race-3"}, key=admin)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"] == {"revoked": "competitor-won"}
    # two rows: the winner's success, then the loser's replay of it (the loser's own success row never landed)
    winner, loser = conn.execute(
        "SELECT id, actor, result FROM agent_action WHERE tool = 'revoke_key' ORDER BY id"
    ).fetchall()
    assert (winner["actor"], winner["result"]["data"]) == ("key:competitor", {"revoked": "competitor-won"})
    assert (loser["actor"], loser["result"]) == (f"key:{arec.id}", {"replay_of": winner["id"]})
    # the loser's revoke rolled back with its refused audit insert (in a real race the winner's revoke stands)
    assert conn.execute("SELECT revoked_at FROM api_key WHERE id = %s", (vrec.id,)).fetchone()["revoked_at"] is None


def test_empty_idempotency_key_is_rejected_and_audited(client, conn):
    _business(conn)
    admin, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "account"), label="web")
    _, vrec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    res = call_tool(client, "revoke_key", {"key_id": vrec.id, "idempotency_key": ""}, key=admin)
    assert res["isError"] and "must not be empty" in tool_text(res)
    assert conn.execute("SELECT revoked_at FROM api_key WHERE id = %s", (vrec.id,)).fetchone()["revoked_at"] is None
    rows = conn.execute("SELECT ok, idempotency_key FROM agent_action WHERE tool = 'revoke_key'").fetchall()
    assert rows == [{"ok": False, "idempotency_key": None}]


def test_web_actions_read_as_the_owner_in_the_audit_log(client, conn):
    from agent import audit

    key = business_with_key(conn, scopes=("read",))
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    for tool, result in (
        ("web.claim", {"business_id": "biz_1", "claimed_now": True}),
        ("web.key_create", {"key_id": "key_x", "label": "muse"}),
        ("web.key_revoke", {"revoked": True}),
        ("web.code_issue", {"proposal_id": "pr_1"}),
        ("web.approve_plan", {"status": "confirmed", "proposal_id": "pr_1"}),
    ):
        audit.record_web(
            conn, tool=tool, email="owner@example.com", ip="local", business_id="biz_1", args={}, result=result
        )
    res = call_tool(client, "get_audit_log", {}, key=key)
    texts = [e["sentence"] for e in res["structuredContent"]["data"]["entries"]]
    assert texts[:5] == [
        "The owner (web) approved and launched a plan (proposal pr_1)",
        "The owner (web) showed the approval code for proposal pr_1",
        "The owner (web) revoked a key",
        "The owner (web) created a key for muse",
        "The owner (web) claimed the business",
    ]


def test_link_actions_read_plainly_in_the_audit_log(client, conn):
    from agent.tests.conftest import ACCOUNT_JARGON, JARGON

    key = business_with_key(conn, scopes=("read",))
    for tool, result in (
        ("web.agree", {"version": "v0.1"}),
        ("web.ads_link_invite", {"customer_id": "123-456-7890", "status": "pending", "mode": "api"}),
        ("web.ads_link_check", {"customer_id": "123-456-7890", "status": "active"}),
        ("web.ads_link_active", {"customer_id": "123-456-7890", "verified_now": True}),
    ):
        audit.record_web(
            conn, tool=tool, email="owner@example.com", ip="local", business_id="biz_1", args={}, result=result
        )
    audit.record_job(
        conn,
        job="ads-link-sync",
        tool="job.ads_link_active",
        business_id="biz_1",
        args={},
        result={"customer_id": "123-456-7890", "verified_now": False},
    )
    audit.record(
        conn,
        tool="ops.ads_link",
        args={},
        result={"customer_id": "123-456-7890", "status": "active", "verified_now": False},
        ok=True,
        business_id="biz_1",
        actor="ops:cli",
        actor_class="platform",
        api_key_id=None,
        ip=None,
    )
    audit.record(
        conn,
        tool="connect_google_ads",
        args={},
        result={"status": "needs_agreement", "connect_url": "x"},
        ok=True,
        business_id="biz_1",
        actor="key:k",
        actor_class="business",
        api_key_id=None,
        ip=None,
    )
    texts = [
        e["sentence"] for e in call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]["entries"]
    ]
    assert texts[:7] == [
        "An assistant asked to link a Google Ads account, but the owner has not accepted the management agreement yet",
        "An operator set the Google Ads link for account 123-456-7890 to active",
        "Admiry confirmed Google Ads account 123-456-7890 is linked",
        "The owner (web) confirmed Google Ads account 123-456-7890 is linked; the business is now verified",
        "The owner (web) checked the Google Ads link: active",
        "The owner (web) asked to link Google Ads account 123-456-7890",
        "The owner (web) accepted the Google Ads Management Agreement (v0.1)",
    ]
    assert all(not JARGON.search(t) and not ACCOUNT_JARGON.search(t) and "manager account" not in t for t in texts)


def test_an_unlink_in_the_owners_own_words_reads_as_an_unlink(client, conn):
    """Plan 2026-10-03-agent-listing-not-mine-words, D5: an `answer` or a `change_plan` whose words unlinked the
    business's Google Maps listing reads as Settings' and update_business's unlink -- for change_plan, before its
    "not a change" reading of the same row; one with no listing key reads as before."""
    key = business_with_key(conn, scopes=("read",))
    rows = (
        ("answer", {"session_id": "cs_1", "recorded": {}, "listing": "unlink", "ready": False}),
        ("change_plan", {"changed": False, "listing": "unlink"}),
        ("change_plan", {"changed": False}),
    )
    for tool, result in rows:
        audit.record(
            conn,
            tool=tool,
            args={},
            result=envelope.make(result, say="x"),
            ok=True,
            business_id="biz_1",
            actor="key:k",
            actor_class="business",
            api_key_id=None,
            ip=None,
        )
    texts = [
        e["sentence"] for e in call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]["entries"]
    ]
    assert texts[:3] == [
        "An assistant sent the owner's words; they were not a change to the plan, and it did not change",
        "An assistant unlinked the business's Google Maps listing",
        "An assistant unlinked the business's Google Maps listing",
    ]


def test_every_tool_and_link_action_has_an_owner_sentence():
    from agent import tools_account

    actions = {
        "web.agree",
        "web.ads_link_invite",
        "web.ads_link_check",
        "web.ads_link_active",
        "job.ads_link_active",
        "ops.ads_link",
        "web.ads_mode",
        "web.agree_prepayment",
        "web.pay_start",
        "web.pay_stopped",
        "web.topup",
        "ops.refund",
        "ops.adjust",
        "job.spend_guard",
        "job.google_account",  # A3c-1: the allocator's pick, and an operator's bind and release
        "ops.google_account_bind",
        "ops.google_account_release",
        "job.ads_launch",
        "job.wallet_settle",
        "job.page_check",
        "web.publish_page",  # the homepage's Publish button (A3p-2 Task 7)
        "job.hosting_minimum",  # A3p-3: the countdown, the takedown and the purge
        "ops.hosting_exempt",
        "web.review_plan",  # S0: the setup card's second look
        "web.fact",  # S0: the phone the launch asks for, saved on the card
        "job.critic",  # capability layer K1: the Critic's look at a new plan version
        "job.analyst",  # capability layer K2: the Analyst's daily look
        "web.digest_frequency",  # the daily digest's Reports card (K2 Task 12)
        "web.email_status",
        "system.digest_unsubscribe",
        "system.email_bounced",  # Resend's events (K2 Task 13)
        "system.digest_complaint",
        "ops.email_status",  # the operator's commands (K2 Task 16)
        "ops.digest",
        "ops.digest_seed",
        "ops.insight",
        "ops.insight_rebuild",
        "web.eval_grade",  # K5: the founder's grading page (no business on the row)
        "ops.eval_case",  # K5: a dev business made into a golden case (no business on the row)
        "job.researcher",  # capability layer K4: the Researcher's model calls
        "ops.research",  # K4: the operator's commands (no business on the row)
        "ops.research_show",
    }
    assert ({m.name for m in mcp_app.REGISTRY} | actions) - set(tools_account.VERBS) == set()


def test_the_second_look_reads_plainly_in_the_activity_log():
    """Final review I3: the owner's activity log never shows an internal tool name for the second look (the setup
    card's, the background Critic's) or the phone saved on the card -- and never a grade or a score."""
    from agent import tools_account

    row = {
        "tool": "job.critic",
        "actor": "job:critic",
        "actor_class": "job",
        "ok": True,
        "result": {"model": "ok", "grounded": True, "review_id": "rv_1"},
        "args_redacted": {"plan_id": "pl_1"},
    }
    assert tools_account.sentence(row, None) == "Admiry took a second look at the plan"
    card = {**row, "tool": "web.review_plan", "actor": "web:owner@example.com", "actor_class": "business"}
    assert tools_account.sentence(card, None) == "The owner (web) asked Admiry for a second look at the plan"
    phone = {**card, "tool": "web.fact", "result": {"version": 2}, "args_redacted": {"id": "phone"}}
    assert tools_account.sentence(phone, None) == "The owner (web) gave the business phone for the ads"


def test_money_actions_read_plainly_in_the_audit_log(client, conn):
    from agent.tests.conftest import JARGON, MONEY_JARGON

    key = business_with_key(conn, scopes=("read",))
    audit.record_web(
        conn,
        tool="web.pay_stopped",
        email="owner@example.com",
        ip="local",
        business_id="biz_1",
        args={},
        result={"session_id": "cs_0", "amount_usd": 200.0},
    )
    audit.record_web(
        conn,
        tool="web.pay_start",
        email="owner@example.com",
        ip="local",
        business_id="biz_1",
        args={},
        result={"session_id": "cs_1", "amount_usd": 500.0},
    )
    audit.record_system(
        conn,
        tool="web.topup",
        actor="stripe",
        business_id="biz_1",
        args={},
        result={"amount_usd": 500.0, "verified_now": True},
    )
    for tool, result in (
        ("ops.adjust", {"entry_id": 3, "amount_usd": -25.5, "note": "late Google credit"}),
        ("ops.refund", {"refund_id": "re_1", "amount_usd": 200.0, "status": "succeeded"}),
    ):
        audit.record(
            conn,
            tool=tool,
            args={},
            result=result,
            ok=True,
            business_id="biz_1",
            actor="ops:cli",
            actor_class="platform",
            api_key_id=None,
            ip=None,
        )
    audit.record_job(
        conn,
        job="spend-guard",
        tool="job.spend_guard",
        business_id="biz_1",
        args={},
        result={"paused_reason": "no_funds"},
    )
    res = call_tool(client, "get_audit_log", {}, key=key)
    texts = [e["sentence"] for e in res["structuredContent"]["data"]["entries"]]
    assert texts[:5] == [
        "Admiry paused your ads: balance is $0. Add funds to resume.",
        "An operator refunded $200.00 to the original payment",
        "An operator corrected the balance by -$25.50 (late Google credit)",
        "Stripe confirmed a payment: $500.00 added to the balance; the business is now verified",
        "The owner (web) started a payment of $500.00",
    ]
    assert texts[5] == (
        "The owner (web) started a payment of $200.00, but the business no longer uses prepaid ads: nothing was charged"
    )
    assert all(not JARGON.search(t) and not MONEY_JARGON.search(t) for t in texts)
