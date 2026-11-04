import asyncio
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

from agent import audit, keys, oauth


def run(coro):
    return asyncio.run(coro)


def _client(client_id="cid_1", name="Claude"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name=name,
        redirect_uris=[AnyHttpUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="read manage leads",
    )


@pytest.fixture
def provider(conn):
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', 'owner@example.com')")
    p = oauth.AdmiryOAuthProvider(base_url="http://testserver")
    run(p.register_client(_client()))
    return p


def _grant_and_code(conn, provider, *, verifier="v" * 43, scopes=("read", "manage", "leads")):
    challenge = oauth.s256(verifier)
    url = run(
        provider.authorize(
            _client(),
            AuthorizationParams(
                state="st",
                scopes=list(scopes),
                code_challenge=challenge,
                redirect_uri=AnyHttpUrl("https://claude.ai/api/mcp/auth_callback"),
                redirect_uri_provided_explicitly=True,
                resource="http://testserver/mcp",
            ),
        )
    )
    assert url.startswith("http://testserver/oauth/consent?txn=txn_")
    txn = oauth.take_txn(conn, url.split("txn=")[1])
    code, redirect = oauth.mint_code(
        conn, txn=txn, business_id="biz_1", owner_email="owner@example.com", scopes=list(scopes)
    )
    assert redirect.startswith("https://claude.ai/api/mcp/auth_callback?code=") and "state=st" in redirect
    return code, verifier


def test_register_get_and_disabled_clients(conn, provider):
    assert run(provider.get_client("cid_1")).client_name == "Claude"
    assert run(provider.get_client("nope")) is None
    conn.execute("UPDATE oauth_client SET disabled_at = now() WHERE id = 'cid_1'")
    assert run(provider.get_client("cid_1")) is None


def test_code_exchange_mints_an_api_key_and_a_refresh_token(conn, provider):
    code, _ = _grant_and_code(conn, provider)
    ac = run(provider.load_authorization_code(_client(), code))
    assert ac is not None and ac.scopes == ["read", "manage", "leads"] and ac.resource == "http://testserver/mcp"
    tok = run(provider.exchange_authorization_code(_client(), ac))
    assert tok.access_token.startswith("oa_") and tok.refresh_token.startswith("or_") and tok.expires_in == 3600
    rec = keys.lookup(conn, tok.access_token)
    assert rec.business_id == "biz_1" and rec.cls == "business" and rec.source == "oauth" and rec.label == "Claude"
    assert set(rec.scopes) == {"read", "manage", "leads"} and rec.grant_id is not None
    assert conn.execute("SELECT used_at FROM oauth_code").fetchone()["used_at"] is not None
    # the code is spent
    assert run(provider.load_authorization_code(_client(), code)) is None
    with pytest.raises(TokenError):
        run(provider.exchange_authorization_code(_client(), ac))
    # the plaintexts are nowhere in the rows
    for table in ("oauth_token", "api_key", "oauth_code"):
        assert tok.access_token not in str(conn.execute(f"SELECT * FROM {table}").fetchall())
        assert tok.refresh_token not in str(conn.execute(f"SELECT * FROM {table}").fetchall())


def test_load_access_token_accepts_oa_and_ak_keys_and_refuses_dead_ones(conn, provider):
    code, _ = _grant_and_code(conn, provider)
    tok = run(provider.exchange_authorization_code(_client(), run(provider.load_authorization_code(_client(), code))))
    at = run(provider.load_access_token(tok.access_token))
    assert at.subject == "biz_1" and at.client_id == "cid_1" and at.expires_at > time.time()
    ak, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    assert run(provider.load_access_token(ak)).client_id == "key"
    keys.revoke(conn, keys.lookup(conn, tok.access_token).id, "biz_1")
    assert run(provider.load_access_token(tok.access_token)) is None
    assert run(provider.load_access_token("oa_nope")) is None


def test_refresh_rotates_and_reuse_revokes_the_family(conn, provider):
    code, _ = _grant_and_code(conn, provider)
    first = run(provider.exchange_authorization_code(_client(), run(provider.load_authorization_code(_client(), code))))
    rt = run(provider.load_refresh_token(_client(), first.refresh_token))
    second = run(provider.exchange_refresh_token(_client(), rt, ["read", "manage", "leads"]))
    assert second.access_token != first.access_token and second.refresh_token != first.refresh_token
    assert run(provider.load_access_token(first.access_token)) is None  # rotated away
    assert run(provider.load_access_token(second.access_token)) is not None
    # reuse of the spent refresh token revokes everything in the grant
    assert run(provider.load_refresh_token(_client(), first.refresh_token)) is None
    assert run(provider.load_access_token(second.access_token)) is None
    assert run(provider.load_refresh_token(_client(), second.refresh_token)) is None
    assert conn.execute("SELECT revoked_at FROM oauth_grant").fetchone()["revoked_at"] is not None


def test_refresh_scope_widening_and_other_client_are_refused(conn, provider):
    code, _ = _grant_and_code(conn, provider, scopes=("read",))
    first = run(provider.exchange_authorization_code(_client(), run(provider.load_authorization_code(_client(), code))))
    rt = run(provider.load_refresh_token(_client(), first.refresh_token))
    with pytest.raises(TokenError):
        run(provider.exchange_refresh_token(_client(), rt, ["read", "manage"]))
    run(provider.register_client(_client("cid_2", "ChatGPT")))
    assert run(provider.load_refresh_token(_client("cid_2"), first.refresh_token)) is None


def test_exchange_refresh_token_reuse_raises_cleanly_and_revokes_the_family(conn, provider):
    code, _ = _grant_and_code(conn, provider)
    first = run(provider.exchange_authorization_code(_client(), run(provider.load_authorization_code(_client(), code))))
    rt = run(provider.load_refresh_token(_client(), first.refresh_token))
    run(provider.exchange_refresh_token(_client(), rt, ["read", "manage", "leads"]))
    # rt.token is now spent; presenting the same RefreshToken object to exchange_refresh_token again (a
    # racing request that read load_refresh_token before the winner committed, or a straight replay) must
    # raise TokenError cleanly -- not crash with FrozenInstanceError -- and revoke the whole grant family.
    with pytest.raises(TokenError) as exc_info:
        run(provider.exchange_refresh_token(_client(), rt, ["read", "manage", "leads"]))
    assert exc_info.value.error == "invalid_grant"
    assert conn.execute("SELECT revoked_at FROM oauth_grant").fetchone()["revoked_at"] is not None


def test_txn_is_one_use_and_expires(conn, provider):
    url = run(
        provider.authorize(
            _client(),
            AuthorizationParams(
                state="s",
                scopes=["read"],
                code_challenge=oauth.s256("v" * 43),
                redirect_uri=AnyHttpUrl("https://claude.ai/api/mcp/auth_callback"),
                redirect_uri_provided_explicitly=True,
                resource=None,
            ),
        )
    )
    txn_id = url.split("txn=")[1]
    assert oauth.take_txn(conn, txn_id)["client_id"] == "cid_1"
    assert oauth.take_txn(conn, txn_id) is None
    conn.execute("UPDATE oauth_txn SET used_at = NULL, expires_at = now() - interval '1 minute'")
    assert oauth.take_txn(conn, txn_id) is None


def test_revoke_grant_and_grants_listing(conn, provider):
    code, _ = _grant_and_code(conn, provider)
    tok = run(provider.exchange_authorization_code(_client(), run(provider.load_authorization_code(_client(), code))))
    g = oauth.grants_for_business(conn, "biz_1")
    assert g[0]["client_name"] == "Claude" and g[0]["revoked_at"] is None
    assert oauth.revoke_grant(conn, g[0]["id"]) >= 1
    assert keys.lookup(conn, tok.access_token).revoked_at is not None
    assert oauth.grants_for_business(conn, "biz_1")[0]["revoked_at"] is not None


def test_oauth_hit_limits_twenty_a_minute(conn):
    for _ in range(20):
        assert oauth.hit(conn, "198.51.100.9", "/token")
    assert not oauth.hit(conn, "198.51.100.9", "/token")
    assert oauth.hit(conn, "198.51.100.10", "/token")


def test_secret_fields_cover_the_oauth_plaintexts():
    assert {"access_token", "refresh_token", "code"} <= audit.SECRET_FIELDS
