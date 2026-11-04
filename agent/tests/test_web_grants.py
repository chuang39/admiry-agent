import secrets

from agent import auth, business, db, keys
from agent.tests.conftest import TEST_DB_URL, call_tool, data, rpc_raw, tool_text
from agent.tests.test_oauth_consent import _sign_in, _txn
from agent.tests.test_oauth_http import consented_code, exchange, register


def _grant(client, conn, scopes=("read", "manage", "leads")):
    """A grant for biz_1 (owner@example.com) the way Task 2's tests make one; returns (access_token, grant_id)."""
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier, scopes=scopes)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    tok = exchange(client, cid, code, verifier).json()
    return tok["access_token"], conn.execute("SELECT id FROM oauth_grant").fetchone()["id"]


def test_connect_lists_and_revokes_grants(client, conn, google):
    access_token, grant_id = _grant(client, conn)
    assert not call_tool(client, "whoami", {}, key=access_token)["isError"]
    csrf = _sign_in(client, google)
    page = client.get("/connect").text
    assert "Connected assistants" in page and "Claude" in page and "Read your plans and reports" in page
    r = client.post(f"/connect/grants/{grant_id}/revoke", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=access_token).status_code == 401
    other_csrf = _sign_in(client, google, email="other@example.com")
    r2 = client.post(f"/connect/grants/{grant_id}/revoke", data={"csrf": other_csrf}, follow_redirects=False)
    assert r2.status_code in (403, 404)


def test_revoke_key_tool_revokes_the_grant(client, conn, google):
    access_token, grant_id = _grant(client, conn, scopes=("read", "manage", "leads", "account"))
    key_id = conn.execute("SELECT id FROM api_key WHERE source = 'oauth'").fetchone()["id"]
    res = call_tool(client, "revoke_key", {"key_id": key_id, "idempotency_key": "r1"}, key=access_token)
    assert not res["isError"], tool_text(res)
    row = conn.execute("SELECT revoked_at FROM oauth_grant WHERE id = %s", (grant_id,)).fetchone()
    assert row["revoked_at"] is not None
    assert conn.execute("SELECT count(*) AS n FROM oauth_token WHERE revoked_at IS NULL").fetchone()["n"] == 0


def test_connect_keys_table_excludes_oauth_access_tokens(client, conn, google):
    _access_token, grant_id = _grant(client, conn)
    key_id = conn.execute("SELECT id FROM api_key WHERE source = 'oauth'").fetchone()["id"]
    _sign_in(client, google)
    page = client.get("/connect").text
    assert f"/connect/grants/{grant_id}/revoke" in page  # the grant's own row, in Connected assistants
    assert f"/connect/keys/{key_id}/revoke" not in page  # not also listed (and revocable) as a plain key


def test_revoking_an_oauth_key_through_the_keys_route_ends_the_grant(client, conn, google):
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    tok = exchange(client, cid, code, verifier).json()
    grant_id = conn.execute("SELECT id FROM oauth_grant").fetchone()["id"]
    key_id = conn.execute("SELECT id FROM api_key WHERE source = 'oauth'").fetchone()["id"]
    csrf = _sign_in(client, google)
    r = client.post(f"/connect/keys/{key_id}/revoke", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert (
        conn.execute("SELECT revoked_at FROM oauth_grant WHERE id = %s", (grant_id,)).fetchone()["revoked_at"]
        is not None
    )
    r2 = client.post(
        "/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]}
    )
    assert r2.status_code == 401


def test_connect_can_start_a_new_business_for_the_key_path(client, conn, google):
    csrf = _sign_in(client, google)
    r = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/connect")
    biz = conn.execute("SELECT owner_email, claim_state, slug FROM business").fetchone()
    assert (biz["owner_email"], biz["claim_state"]) == ("owner@example.com", "claimed")
    assert biz["slug"] is not None


def test_a_concurrent_new_business_post_is_refused_not_queued(client, conn, google):
    csrf = _sign_in(client, google)
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('newbiz:owner@example.com'))")  # a POST in flight
        r = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
        assert r.status_code == 409 and "try again in a moment" in r.text
        assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 0
        other.execute("SELECT pg_advisory_unlock(hashtext('newbiz:owner@example.com'))")
    r2 = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    assert r2.status_code == 303
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1


def test_a_second_new_business_is_refused_while_the_first_has_no_knowledge(client, conn, google):
    csrf = _sign_in(client, google)
    first = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    assert first.status_code == 303
    biz_id = conn.execute("SELECT id FROM business").fetchone()["id"]
    again = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    assert again.status_code == 409 and "Finish setting up" in again.text
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1
    business.add_knowledge(conn, biz_id, {"name": "Bright Smile"}, source="extract")
    second = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    assert second.status_code == 303
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 2


def test_new_business_route_refuses_after_a_consent_made_blank_business(client, conn, google):
    """Final review I3: the consent page's "A new business" and /connect/businesses/new share one blank
    business per signer; the route (the owner clicked "new") refuses rather than silently reusing it."""
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent",
        data={"txn": txn, "csrf": csrf, "business": "new", "scopes": ["read", "manage"], "decision": "allow"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    again = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    assert again.status_code == 409 and "Finish setting up" in again.text
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1


def _refresh(client, cid, refresh_token):
    r = client.post("/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": refresh_token})
    assert r.status_code == 200, r.text
    return r.json()


def test_rate_limits_count_every_access_token_of_the_grant(client, conn, monkeypatch):
    """Final review I4: a refresh mints a new api_key row; the per-key limits are counted per grant, so
    refreshing does not reset them."""
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier)
    tok = exchange(client, cid, code, verifier).json()
    old_key = conn.execute("SELECT id FROM api_key WHERE source = 'oauth'").fetchone()["id"]
    monkeypatch.setattr(auth, "RATE_PER_MINUTE", 3)
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, api_key_id, tool, args_hash) "
        "SELECT 'biz_1', %s, %s, 'whoami', 'h' FROM generate_series(1, 3) g",
        (f"key:{old_key}", old_key),
    )
    fresh = _refresh(client, cid, tok["refresh_token"])
    limited = call_tool(client, "whoami", {}, key=fresh["access_token"])
    assert limited["isError"] and "rate limit: 3 calls per minute" in tool_text(limited)
    # an ak_ key on the same business is still counted on its own
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    assert not call_tool(client, "whoami", {}, key=plaintext)["isError"]


def test_list_keys_hides_spent_oauth_access_tokens(client, conn):
    """Final review I5: every refresh leaves a revoked oa_ row behind; list_keys shows only the live one."""
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier, scopes=("read", "manage", "leads", "account"))
    tok = exchange(client, cid, code, verifier).json()
    fresh = _refresh(client, cid, tok["refresh_token"])
    conn.execute(
        "INSERT INTO api_key (id, key_hash, business_id, label, class, scopes, expires_at, source) VALUES "
        "('key_old_oa', 'h-old', 'biz_1', 'Claude', 'business', '{read}', now() - interval '1 hour', 'oauth')"
    )
    listed = data(call_tool(client, "list_keys", {}, key=fresh["access_token"]))["keys"]
    assert [k["source"] for k in listed] == ["oauth"] and listed[0]["revoked_at"] is None
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE source = 'oauth'").fetchone()["n"] == 3
