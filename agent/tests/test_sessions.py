"""Cookie sessions, sign-in attempts and the sweep (spec §4.5 pages, §5.5 rate limits)."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent import jobs, sessions


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/plan/pl_0123456789abcdef", "/plan/pl_0123456789abcdef"),
        ("/connect?claim=abc", "/connect?claim=abc"),
        (None, "/connect"),
        ("", "/connect"),
        ("connect", "/connect"),
        ("//evil.example/x", "/connect"),
        ("https://evil.example/", "/connect"),
        ("/x\\y", "/connect"),
        ("/x\r\nSet-Cookie: a=b", "/connect"),
        ("/" + "a" * 600, "/connect"),
    ],
)
def test_next_targets_are_same_site_paths_only(path, expected):
    assert sessions.safe_next(path) == expected


def test_begin_login_records_an_attempt_and_returns_state_and_challenge(conn):
    state, challenge, nonce = sessions.begin_login(conn, ip="198.51.100.9", next_path="/plan/pl_0123456789abcdef")
    row = conn.execute("SELECT * FROM login_attempt").fetchone()
    assert row["state"] == state and len(state) >= 32 and row["ip"] == "198.51.100.9"
    assert row["next_path"] == "/plan/pl_0123456789abcdef" and row["used_at"] is None
    assert len(row["code_verifier"]) >= 43 and challenge and challenge != row["code_verifier"]
    assert len(nonce) >= 32 and row["login_hash"] == sessions._hash(nonce) and nonce not in row.values()


def test_sign_in_starts_are_limited_per_address_per_minute(conn):
    for _ in range(sessions.SIGN_IN_PER_MINUTE):
        sessions.begin_login(conn, ip="198.51.100.9", next_path=None)
    with pytest.raises(sessions.TooManySignIns):
        sessions.begin_login(conn, ip="198.51.100.9", next_path=None)
    sessions.begin_login(conn, ip="198.51.100.10", next_path=None)  # another address is unaffected
    conn.execute("UPDATE login_attempt SET created_at = now() - interval '2 minutes'")
    sessions.begin_login(conn, ip="198.51.100.9", next_path=None)  # the window slid


def test_finish_login_is_single_use_and_expires(conn):
    state, _, nonce = sessions.begin_login(conn, ip="local", next_path="/connect?claim=t")
    assert sessions.finish_login(conn, "nope", nonce) is None and sessions.finish_login(conn, "", nonce) is None
    assert sessions.finish_login(conn, state, None) is None and sessions.finish_login(conn, state, "other") is None
    attempt = sessions.finish_login(conn, state, nonce)  # a refused browser did not consume it
    assert attempt["next_path"] == "/connect?claim=t" and attempt["ip"] == "local" and attempt["code_verifier"]
    assert (
        conn.execute("SELECT next_path FROM login_attempt WHERE state = %s", (state,)).fetchone()["next_path"]
        == "/connect"
    )
    assert sessions.finish_login(conn, state, nonce) is None  # used
    state2, _, nonce2 = sessions.begin_login(conn, ip="local", next_path=None)
    conn.execute("UPDATE login_attempt SET created_at = now() - interval '11 minutes' WHERE state = %s", (state2,))
    assert sessions.finish_login(conn, state2, nonce2) is None  # older than LOGIN_TTL


def test_sessions_are_created_read_and_revoked_by_a_hashed_token(conn):
    token, csrf = sessions.create(conn, email="Owner@Example.com", sub="1057", ip="local")
    assert len(token) >= 32 and len(csrf) >= 24
    row = conn.execute("SELECT token_hash, email, google_sub, csrf FROM web_session").fetchone()
    assert token not in row["token_hash"] and len(row["token_hash"]) == 64 and row["email"] == "owner@example.com"
    s = sessions.get(conn, token)
    assert s == {"token_hash": row["token_hash"], "email": "owner@example.com", "google_sub": "1057", "csrf": csrf}
    assert (
        sessions.get(conn, "nope") is None
        and sessions.get(conn, None) is None
        and sessions.get(conn, "x" * 200) is None
    )
    sessions.revoke(conn, token)
    assert sessions.get(conn, token) is None
    token2, _ = sessions.create(conn, email="a@example.com", sub="2", ip="local")
    conn.execute(
        "UPDATE web_session SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
        (sessions._hash(token2),),
    )
    assert sessions.get(conn, token2) is None


def test_cookie_args_are_httponly_lax_and_secure_only_on_https():
    http = sessions.cookie_args(SimpleNamespace(web_base_url="http://localhost:8097"))
    assert http == {
        "httponly": True,
        "samesite": "lax",
        "secure": False,
        "path": "/",
        "max_age": sessions.SESSION_DAYS * 86400,
    }
    assert sessions.cookie_args(SimpleNamespace(web_base_url="https://admiry.ai"))["secure"] is True


def test_check_csrf_refuses_missing_or_foreign_tokens():
    session = {"csrf": "abc"}
    sessions.check_csrf(session, "abc")
    # a JSON body can carry any value: never a 500 from compare_digest (anything but two ASCII strings raises there)
    for bad in (None, "", "abd", "abcd", 123, ["abc"], b"abc", "ébc", "\ud800bc"):
        with pytest.raises(HTTPException) as e:
            sessions.check_csrf(session, bad)
        assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        sessions.check_csrf(None, "abc")


def test_session_expire_sweeps_old_leftovers_only(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    live, _ = sessions.create(conn, email="a@example.com", sub="1", ip="local")
    old, _ = sessions.create(conn, email="b@example.com", sub="2", ip="local")
    conn.execute(
        "UPDATE web_session SET expires_at = now() - interval '2 days' WHERE token_hash = %s", (sessions._hash(old),)
    )
    sessions.begin_login(conn, ip="local", next_path=None)
    conn.execute(
        "INSERT INTO login_attempt (state, code_verifier, login_hash, ip, created_at) VALUES ('stale', 'v', 'h', 'local', now() - interval '2 hours')"
    )
    conn.execute(
        "INSERT INTO claim_link (token_hash, business_id, expires_at) VALUES ('fresh', 'biz_1', now() + interval '1 hour'), "
        "('dead', 'biz_1', now() - interval '2 days')"
    )
    assert jobs.session_expire(conn, 0, 1) == {"sessions": 1, "logins": 1, "links": 1}
    assert sessions.get(conn, live) is not None
    assert conn.execute("SELECT count(*) AS n FROM login_attempt").fetchone()["n"] == 1
    assert conn.execute("SELECT token_hash FROM claim_link").fetchone()["token_hash"] == "fresh"
    assert "session-expire" in jobs.JOBS
