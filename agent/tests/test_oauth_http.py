import secrets
from urllib.parse import parse_qs, urlparse

from agent import oauth
from agent.tests.conftest import call_tool, data, rpc_raw
from agent.tests.fixtures import business_with_key

CB = "https://claude.ai/api/mcp/auth_callback"


def test_missing_bearer_is_401_with_resource_metadata(client):
    r = rpc_raw(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    assert r.status_code == 401
    assert (
        r.headers["www-authenticate"]
        == 'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"'
    )
    r = rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key="ak_nope")
    assert r.status_code == 401 and "invalid_token" in r.headers["www-authenticate"]


def test_discovery_documents(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://testserver/mcp" and prm["authorization_servers"] == ["http://testserver/"]
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["authorization_endpoint"].endswith("/authorize") and asm["token_endpoint"].endswith("/token")
    assert asm["registration_endpoint"].endswith("/register")
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert set(asm["scopes_supported"]) == {"read", "manage", "leads", "account"}
    assert "authorization_code" in asm["grant_types_supported"] and "refresh_token" in asm["grant_types_supported"]
    # R11: the SDK advertises the confidential-client methods; our public clients register with "none",
    # which /register accepts all the same (pinned so an SDK upgrade that changes the list is noticed)
    assert asm["token_endpoint_auth_methods_supported"] == ["client_secret_post", "client_secret_basic"]


def register(client, name="Claude", redirect_uri=CB, *, expect=201):
    r = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert r.status_code == expect, r.text
    return r.json()["client_id"] if expect == 201 else r.json()


def test_registration_refuses_redirect_uris_that_are_not_https_or_loopback(client, conn):
    """Final review I2 (a): open DCR must not register a javascript: or plain-http callback on another host."""
    for bad in ("javascript:alert(1)", "http://evil.example/cb"):
        err = register(client, redirect_uri=bad, expect=400)
        assert err["error"] == "invalid_redirect_uri", err
    for ok in ("http://localhost:3000/cb", "http://127.0.0.1:8765/cb", CB):
        register(client, redirect_uri=ok)
    assert conn.execute("SELECT count(*) AS n FROM oauth_client").fetchone()["n"] == 3


def test_dcr_then_authorize_redirects_to_consent(client, conn):
    cid = register(client)
    assert (
        conn.execute("SELECT client_name FROM oauth_client WHERE id = %s", (cid,)).fetchone()["client_name"] == "Claude"
    )
    verifier = secrets.token_urlsafe(48)
    r = client.get(
        "/authorize",
        params={
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": CB,
            "state": "st",
            "code_challenge": oauth.s256(verifier),
            "code_challenge_method": "S256",
            "scope": "read manage leads",
            "resource": "http://testserver/mcp",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307) and r.headers["location"].startswith("http://testserver/oauth/consent?txn=txn_")
    txn = conn.execute("SELECT params FROM oauth_txn").fetchone()["params"]
    assert txn["state"] == "st" and txn["scopes"] == ["read", "manage", "leads"]


def test_plain_pkce_and_unknown_client_are_refused(client):
    cid = register(client)
    r = client.get(
        "/authorize",
        params={
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": CB,
            "state": "st",
            "code_challenge": "abc",
            "code_challenge_method": "plain",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307, 400)
    if r.status_code != 400:
        assert "error=" in r.headers["location"]
    r = client.get(
        "/authorize",
        params={
            "client_id": "nope",
            "response_type": "code",
            "redirect_uri": CB,
            "state": "st",
            "code_challenge": oauth.s256("v" * 43),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def exchange(client, cid, code, verifier, *, resource="http://testserver/mcp"):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": CB,
            "resource": resource,
        },
    )


def consented_code(client, conn, cid, verifier, scopes=("read", "manage", "leads")):
    """Skip the page (Task 3 tests it): authorize, then mint the code the way consent does."""
    r = client.get(
        "/authorize",
        params={
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": CB,
            "state": "st",
            "code_challenge": oauth.s256(verifier),
            "code_challenge_method": "S256",
            "scope": " ".join(scopes),
            "resource": "http://testserver/mcp",
        },
        follow_redirects=False,
    )
    txn = oauth.take_txn(conn, r.headers["location"].split("txn=")[1])
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', 'owner@example.com') ON CONFLICT DO NOTHING"
    )
    code, redirect = oauth.mint_code(
        conn, txn=txn, business_id="biz_1", owner_email="owner@example.com", scopes=list(scopes)
    )
    assert parse_qs(urlparse(redirect).query)["state"] == ["st"]
    return code


def test_token_exchange_then_whoami_with_the_access_token(client, conn):
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier)
    r = exchange(client, cid, code, verifier)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert (
        tok["token_type"].lower() == "bearer" and tok["expires_in"] == 3600 and tok["refresh_token"].startswith("or_")
    )
    who = data(call_tool(client, "whoami", {}, key=tok["access_token"]))
    assert who["business"]["id"] == "biz_1" and who["key"]["class"] == "business" and who["key"]["label"] == "Claude"
    # refresh
    r2 = client.post(
        "/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]}
    )
    assert r2.status_code == 200 and r2.json()["access_token"] != tok["access_token"]
    assert (
        rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=tok["access_token"]).status_code == 401
    )
    # reuse revokes the family
    r3 = client.post(
        "/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]}
    )
    # the MCP spec requires 401 (not RFC 6749's 400) for an invalid_grant response; FastMCP's TokenHandler
    # transforms it (see fastmcp/server/auth/auth.py TokenHandler.handle)
    assert r3.status_code == 401 and r3.json()["error"] == "invalid_grant"
    assert (
        rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=r2.json()["access_token"]).status_code
        == 401
    )


def test_pkce_mismatch_and_wrong_client_are_refused(client, conn):
    cid, other = register(client), register(client, "ChatGPT")
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier)
    r = exchange(client, other, code, verifier)
    assert r.status_code == 401 and r.json()["error"] == "invalid_grant"  # 401 per MCP spec, see above
    r = exchange(client, cid, code, "wrong" * 10)
    assert r.status_code == 401 and r.json()["error"] == "invalid_grant"
    r = exchange(client, cid, code, verifier)  # the rightful client, right verifier: the code is still unspent
    assert r.status_code == 200
    r = exchange(client, cid, code, verifier)  # but only once
    assert r.status_code == 401


def test_resource_mismatch_is_refused(client, conn):
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier)
    r = exchange(client, cid, code, verifier, resource="http://elsewhere/mcp")
    assert r.status_code == 400


def test_x_api_key_header_still_works_and_revoked_keys_are_401(client, conn):
    key = business_with_key(conn)
    r = rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, extra_headers={"X-API-Key": key})
    assert r.status_code == 200 and not r.json()["result"]["isError"]
    conn.execute("UPDATE api_key SET revoked_at = now()")
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=key).status_code == 401
    assert (
        conn.execute("SELECT count(*) AS n FROM agent_action WHERE ok = false").fetchone()["n"] == 0
    )  # refused before run()


def test_oauth_endpoints_are_rate_limited(client):
    for _ in range(20):
        client.post(
            "/register",
            json={"client_name": "x", "redirect_uris": [CB], "token_endpoint_auth_method": "none"},
            headers={"X-Forwarded-For": "198.51.100.9"},
        )
    r = client.post(
        "/register",
        json={"client_name": "x", "redirect_uris": [CB], "token_endpoint_auth_method": "none"},
        headers={"X-Forwarded-For": "198.51.100.9"},
    )
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
