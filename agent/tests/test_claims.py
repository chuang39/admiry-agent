"""Claim links and the claim itself (spec §4.0 "Business claim state")."""

import pytest

from agent import business, claims, jobs, keys
from agent.tests.conftest import call_tool, rpc, tool_text
from agent.tests.fixtures import knowledge_fixture


def draft_business(conn, biz="biz_1", name="Bright Smile Dental"):
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
    data = knowledge_fixture().model_dump()
    data["name"] = name
    business.add_knowledge(conn, biz, data, source="extract")
    plaintext, rec = keys.mint(conn, business_id=biz, cls="draft", scopes=("read", "manage"), label="draft")
    return plaintext, rec.id


def test_slugify_is_lowercase_ascii_hyphens_and_bounded():
    assert claims.slugify("Bright Smile Dental") == "bright-smile-dental"
    assert claims.slugify("  Café & Bar!! (Palo Alto) ") == "cafe-bar-palo-alto"
    assert claims.slugify("") == "business" and claims.slugify("!!!") == "business"
    assert len(claims.slugify("x" * 200)) == claims.SLUG_MAX and not claims.slugify("a-" * 40).endswith("-")


def test_new_link_is_a_24h_capability_stored_hashed(conn):
    draft_business(conn)
    url, expires_at = claims.new_link(conn, "biz_1")
    assert url.startswith("http://testserver/connect?claim=") and len(url.split("claim=")[1]) >= 32
    row = conn.execute("SELECT token_hash, business_id, used_at, expires_at FROM claim_link").fetchone()
    assert row["business_id"] == "biz_1" and row["used_at"] is None and row["expires_at"] == expires_at
    token = url.split("claim=")[1]
    assert token not in row["token_hash"] and len(row["token_hash"]) == 64  # sha256 hex, never the token
    assert (expires_at - conn.execute("SELECT now() AS t").fetchone()["t"]).total_seconds() == pytest.approx(
        24 * 3600, abs=5
    )


def test_take_returns_the_business_once_and_never_for_bad_or_expired_tokens(conn):
    draft_business(conn)
    url, _ = claims.new_link(conn, "biz_1")
    token = url.split("claim=")[1]
    assert claims.take(conn, "nope") is None and claims.take(conn, "") is None
    live = claims.take(conn, token)
    assert live["business_id"] == "biz_1"
    assert claims.take(conn, token)["business_id"] == "biz_1"  # taking does not consume: claiming does
    conn.execute("UPDATE claim_link SET expires_at = now() - interval '1 second'")
    assert claims.take(conn, token) is None


def test_claim_records_the_owner_reserves_a_slug_and_upgrades_the_draft_key(conn):
    plaintext, key_id = draft_business(conn)
    url, _ = claims.new_link(conn, "biz_1")
    live = claims.take(conn, url.split("claim=")[1])
    out = claims.claim(conn, "biz_1", "Owner@Example.com", token_hash=live["token_hash"])
    assert out == {
        "business_id": "biz_1",
        "claim_state": "claimed",
        "slug": "bright-smile-dental",
        "claimed_now": True,
        "key_upgraded": key_id,
    }
    row = business.row(conn, "biz_1")
    assert (
        row["owner_email"] == "owner@example.com"
        and row["claim_state"] == "claimed"
        and row["slug"] == "bright-smile-dental"
    )
    assert row["claim_expires_at"] is not None and row["verified_at"] is None
    rec = keys.lookup(conn, plaintext)
    assert rec.cls == "business" and rec.expires_at is None  # the assistant keeps working, lastingly
    assert conn.execute("SELECT used_at FROM claim_link").fetchone()["used_at"] is not None
    assert claims.take(conn, url.split("claim=")[1]) is None  # a used link is dead


def test_claim_is_idempotent_for_the_owner_and_refused_for_anyone_else(conn):
    draft_business(conn)
    claims.claim(conn, "biz_1", "owner@example.com")
    again = claims.claim(conn, "biz_1", "OWNER@example.com")
    assert again["claimed_now"] is False and again["claim_state"] == "claimed" and again["key_upgraded"] is None
    with pytest.raises(claims.ClaimRefused, match="another Google account"):
        claims.claim(conn, "biz_1", "intruder@example.com")
    conn.execute("UPDATE business SET claim_state = 'verified' WHERE id = 'biz_1'")
    assert claims.claim(conn, "biz_1", "owner@example.com")["claim_state"] == "verified"  # never downgrades


def test_claim_expire_waits_for_a_bank_transfer_still_on_its_way(conn):
    """Finding 6: a claim nobody verified in 14 days must not lapse while a bank transfer for this business is
    still 'processing' -- Stripe can confirm an ACH debit days after the claim's own window, and the claim
    lapsing would undo verify_by_payment's own wait for it."""
    draft_business(conn)
    claims.claim(conn, "biz_1", "owner@example.com")
    conn.execute("UPDATE business SET claim_expires_at = now() - interval '1 minute' WHERE id = 'biz_1'")
    conn.execute(
        "INSERT INTO payment_session (id, business_id, amount_cents, status) VALUES "
        "('cs_1', 'biz_1', 100500, 'processing')"
    )
    assert jobs.claim_expire(conn, 0, 1)["claims_released"] == 0
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"
    conn.execute("UPDATE payment_session SET status = 'failed'")
    assert jobs.claim_expire(conn, 0, 1)["claims_released"] == 1
    row = business.row(conn, "biz_1")
    assert row["claim_state"] == "draft" and row["owner_email"] == "owner@example.com"


def test_slugs_are_unique_per_business(conn):
    draft_business(conn, "biz_1")
    draft_business(conn, "biz_2")
    claims.claim(conn, "biz_1", "a@example.com")
    assert claims.claim(conn, "biz_2", "b@example.com")["slug"] == "bright-smile-dental-2"


def test_start_hands_out_a_claim_link_and_request_connect_link_renews_it(client, conn, monkeypatch):
    plaintext, _ = draft_business(conn)
    res = call_tool(client, "request_connect_link", {}, key=plaintext)
    assert not res["isError"], tool_text(res)
    body = res["structuredContent"]["data"]
    assert body["connect_url"].startswith("http://testserver/connect?claim=") and body["expires_at"]
    assert conn.execute("SELECT count(*) AS n FROM claim_link WHERE business_id = 'biz_1'").fetchone()["n"] == 1
    again = call_tool(client, "request_connect_link", {}, key=plaintext)["structuredContent"][
        "data"
    ]  # RO: every call a fresh link
    assert (
        again["connect_url"].startswith("http://testserver/connect?claim=")
        and again["connect_url"] != body["connect_url"]
    )
    assert conn.execute("SELECT count(*) AS n FROM claim_link WHERE business_id = 'biz_1'").fetchone()["n"] == 2
    audited = conn.execute(
        "SELECT args_redacted, result FROM agent_action WHERE tool = 'request_connect_link'"
    ).fetchall()
    assert len(audited) == 2 and all(r["result"]["data"]["connect_url"] == "***" for r in audited)
    assert "claim=" not in str(audited)  # the claim token never enters the audit rows
    claims.claim(conn, "biz_1", "owner@example.com")
    res = call_tool(client, "request_connect_link", {}, key=plaintext)
    body = res["structuredContent"]["data"]
    assert body["connect_url"] == "http://testserver/connect" and body["expires_at"] is None
    assert "already claimed" in body["note"]
    tools = {t["name"]: t for t in rpc(client, "tools/list", key=plaintext)["result"]["tools"]}
    assert "idempotency_key" not in tools["request_connect_link"]["inputSchema"].get("properties", {})
    assert tools["request_connect_link"]["annotations"]["readOnlyHint"] is True


def test_settings_read_the_google_client(monkeypatch):
    from agent import config

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    s = config.load()
    assert s.google_client_id is None and s.google_client_secret is None
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-x")
    s = config.load()
    assert s.google_client_id == "123.apps.googleusercontent.com" and s.google_client_secret == "GOCSPX-x"
