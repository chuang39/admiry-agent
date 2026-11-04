"""Browser sessions for the owner pages (spec §4.5): a random token in an HttpOnly cookie, its sha256 in
web_session with the verified e-mail; sign-in attempts (OAuth state + PKCE verifier, single use, LOGIN_TTL)
whose rows also count the per-address limit (spec §5.5: OAuth 20/min/IP) and whose login cookie binds the
callback to the browser that started it; the CSRF token every POST
carries. All in Postgres, so N replicas share nothing else. Sessions last SESSION_DAYS; sign-out revokes;
jobs.session_expire sweeps. Tokens never appear in logs, rows or errors — one accepted exception: an
attempt's next_path may carry a claim link (/connect?claim=<token>) in plaintext for the ≤LOGIN_TTL sign-in
window; finish_login resets it to DEFAULT_NEXT and an unfinished row is deleted after an hour. The access
log prints claim=*** (agent.__main__.redact_claim)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import psycopg
from fastapi import HTTPException, Request

from . import auth, google_auth

COOKIE = "admiry_session"
LOGIN_COOKIE = "admiry_login"  # binds a sign-in to the browser that started it (login CSRF)
LOGIN_PATH = "/connect/google"
SESSION_DAYS = 30
LOGIN_TTL = timedelta(minutes=10)
SIGN_IN_PER_MINUTE = 20
DEFAULT_NEXT = "/connect"
NEXT_MAX = 512


class TooManySignIns(Exception):
    """The address started too many sign-ins this minute (never recorded)."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_next(path: str | None) -> str:
    """A same-site path or DEFAULT_NEXT: one leading '/', no scheme or host, no backslash or line break."""
    if (
        not path
        or len(path) > NEXT_MAX
        or not path.startswith("/")
        or path.startswith("//")
        or any(c in path for c in "\\\r\n")
    ):
        return DEFAULT_NEXT
    return path


def begin_login(conn: psycopg.Connection, *, ip: str, next_path: str | None) -> tuple[str, str, str]:
    """Record a sign-in start under the per-address limit; returns (state, code_challenge, login nonce).
    The nonce goes in the LOGIN_COOKIE of the browser that started; the row keeps only its hash."""
    n = conn.execute(
        "SELECT count(*) AS n FROM login_attempt WHERE ip = %s AND created_at > now() - interval '1 minute'", (ip,)
    ).fetchone()["n"]
    if n >= SIGN_IN_PER_MINUTE:
        raise TooManySignIns(f"{SIGN_IN_PER_MINUTE} sign-ins per minute per address")
    verifier, challenge = google_auth.pkce()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO login_attempt (state, code_verifier, login_hash, next_path, ip) VALUES (%s, %s, %s, %s, %s)",
        (state, verifier, _hash(nonce), safe_next(next_path), ip),
    )
    return state, challenge, nonce


def finish_login(conn: psycopg.Connection, state: str, login_nonce: str | None) -> dict | None:
    """Consume the attempt for `state` when it is unused, younger than LOGIN_TTL and presented by the browser
    that started it (its login nonce); a state from another browser is refused and not consumed. Consuming
    also resets the row's next_path to DEFAULT_NEXT, so a claim token carried in it lives in the table only for
    the sign-in window (accepted; jobs.session_expire deletes the row after an hour)."""
    if not state or len(state) > 128 or not login_nonce or len(login_nonce) > 128:
        return None
    row = conn.execute(
        "UPDATE login_attempt a SET used_at = now(), next_path = %s FROM "
        "(SELECT state, next_path FROM login_attempt WHERE state = %s AND login_hash = %s AND used_at IS NULL "
        " AND created_at > now() - %s FOR UPDATE) old WHERE a.state = old.state "
        "RETURNING a.code_verifier, old.next_path, a.ip",
        (DEFAULT_NEXT, state, _hash(login_nonce), LOGIN_TTL),
    ).fetchone()
    return dict(row) if row else None


def create(conn: psycopg.Connection, *, email: str, sub: str, ip: str | None) -> tuple[str, str]:
    """A new session for a Google-verified e-mail: returns (cookie token, csrf)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO web_session (token_hash, email, google_sub, csrf, ip, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, now() + %s)",
        (_hash(token), email.strip().lower(), sub, csrf, ip, timedelta(days=SESSION_DAYS)),
    )
    return token, csrf


def get(conn: psycopg.Connection, token: str | None) -> dict | None:
    """The live session for a cookie token, or None."""
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT token_hash, email, google_sub, csrf FROM web_session WHERE token_hash = %s "
        "AND revoked_at IS NULL AND expires_at > now()",
        (_hash(token),),
    ).fetchone()
    return dict(row) if row else None


def revoke(conn: psycopg.Connection, token: str | None) -> None:
    if token:
        conn.execute(
            "UPDATE web_session SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL", (_hash(token),)
        )


def cookie_args(settings) -> dict:
    """set_cookie keyword arguments: HttpOnly, SameSite=Lax, Secure on https, path /, SESSION_DAYS."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.web_base_url.startswith("https://"),
        "path": "/",
        "max_age": SESSION_DAYS * 86400,
    }


def login_cookie_args(settings) -> dict:
    """The sign-in binding cookie: HttpOnly, SameSite=Lax (sent on Google's top-level redirect back), Secure on
    https, only for /connect/google, LOGIN_TTL."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.web_base_url.startswith("https://"),
        "path": LOGIN_PATH,
        "max_age": int(LOGIN_TTL.total_seconds()),
    }


def of_request(conn: psycopg.Connection, request: Request) -> dict | None:
    return get(conn, request.cookies.get(COOKIE))


def request_ip(request: Request) -> str:
    return auth.client_ip({k.lower(): v for k, v in request.headers.items()})


def check_csrf(session: dict | None, csrf: str | None) -> None:
    """Every state-changing POST carries the session's csrf; anything else is refused (403). A JSON body can carry any
    value there, and compare_digest raises on anything but two ASCII strings: a value that is not a string, or holds a
    character outside ASCII (the token never does), is refused before the comparison, so no value is a 500."""
    if (
        not session
        or not isinstance(csrf, str)
        or not csrf
        or not csrf.isascii()
        or not secrets.compare_digest(csrf, session["csrf"])
    ):
        raise HTTPException(status_code=403, detail="the form expired; reload the page and try again")
