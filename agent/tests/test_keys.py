from datetime import datetime, timedelta, timezone

import pytest

from agent import keys


def _business(conn, biz_id="biz_1"):
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz_id,))


def test_mint_returns_plaintext_once_and_stores_only_the_hash(conn):
    _business(conn)
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="muse")
    assert plaintext.startswith("ak_") and len(plaintext) == 3 + 40
    assert rec.cls == "business" and rec.scopes == ("read", "manage") and rec.business_id == "biz_1"
    row = conn.execute("SELECT key_hash FROM api_key WHERE id = %s", (rec.id,)).fetchone()
    assert row["key_hash"] == keys.hash_key(plaintext) and plaintext not in row["key_hash"]


def test_lookup_round_trips_and_rejects_garbage(conn):
    _business(conn)
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    assert keys.lookup(conn, plaintext).id == rec.id
    assert keys.lookup(conn, "ak_" + "0" * 40) is None
    assert keys.lookup(conn, "bx_notours") is None


def test_draft_keys_expire_after_24h_with_read_manage(conn):
    _business(conn)
    _, rec = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage", "leads"), label="draft")
    assert rec.scopes == ("read", "manage")  # drafts are read+manage only, whatever was asked
    delta = rec.expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=23, minutes=59) < delta <= timedelta(hours=24)
    assert not rec.is_expired()
    assert rec.is_expired(now=datetime.now(timezone.utc) + timedelta(hours=25))


def test_platform_keys_pass_every_scope_and_bind_no_business(conn):
    _, rec = keys.mint(conn, business_id=None, cls="platform", scopes=("read",), label="ops")
    assert rec.allows("account") and rec.business_id is None
    with pytest.raises(ValueError):
        keys.mint(conn, business_id="biz_1", cls="platform", scopes=("read",), label="ops")
    with pytest.raises(ValueError):
        keys.mint(conn, business_id=None, cls="business", scopes=("read",), label="muse")
    with pytest.raises(ValueError):
        keys.mint(conn, business_id=None, cls="platform", scopes=("admin",), label="ops")


def test_revoke_only_own_business_keys(conn):
    _business(conn, "biz_1")
    _business(conn, "biz_2")
    _, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    assert keys.revoke(conn, rec.id, "biz_2") is False
    assert keys.revoke(conn, rec.id, "biz_1") is True
    assert keys.revoke(conn, rec.id, "biz_1") is False  # already revoked
    assert keys.lookup(conn, "ak_" + "0" * 40) is None
    listed = keys.list_for_business(conn, "biz_1")
    assert listed[0]["id"] == rec.id and listed[0]["revoked_at"] is not None
    assert "key_hash" not in listed[0] and isinstance(listed[0]["created_at"], str)


def test_draft_ttl_cannot_be_overridden_and_bad_ttls_are_rejected(conn):
    _business(conn)
    _, rec = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read",), label="draft", ttl_hours=0)
    assert rec.expires_at is not None and not rec.is_expired()
    delta = rec.expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=23, minutes=59) < delta <= timedelta(hours=24)
    with pytest.raises(ValueError, match="ttl_hours must be positive"):
        keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse", ttl_hours=0)
    _, forever = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    assert forever.expires_at is None


def test_upgrade_draft_promotes_the_newest_live_draft_key_only(conn):
    _business(conn)
    plaintext1, rec1 = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    plaintext2, rec2 = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    assert keys.upgrade_draft(conn, "biz_1") == rec2.id
    upgraded = keys.lookup(conn, plaintext2)
    assert upgraded.cls == "business" and upgraded.expires_at is None
    assert upgraded.label == "website"  # named for what it is, not "draft" (Settings' Keys list hides it)
    untouched = keys.lookup(conn, plaintext1)
    assert untouched.cls == "draft" and untouched.expires_at is not None
    keys.revoke(conn, rec1.id, "biz_1")
    assert keys.upgrade_draft(conn, "biz_1") is None  # no more live draft keys to upgrade
    assert keys.upgrade_draft(conn, "biz_nope") is None
