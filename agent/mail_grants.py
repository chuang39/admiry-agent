"""Authorized addresses (spec 2026-09-28 §4, D57: email_grant) and the /m/{token} links (§3.2, §6.4: email_link).
Shared by mail_dispatch (mints links, reads grants) and mail_web (the web step, /connect), which must not import
each other. A grant never carries the account scope; a link is a hashed, 7-day, one-use capability that still
needs Google sign-in to do anything."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

import psycopg

from . import db

SCOPES = ("read", "manage", "leads")  # never account (D57)
DEFAULT_SCOPES = ("read", "manage", "leads")
SCOPE_WORDS = {
    "read": "see the plan and results",
    "manage": "make changes within the monthly limit",
    "leads": "see who the leads are",
}
LINK_TTL = timedelta(days=7)
ADDRESS = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
COLS = "id, business_id, address, scopes, via, created_by, created_at, last_used_at, revoked_at"


def valid(address: str) -> bool:
    return bool(ADDRESS.fullmatch(address.strip().lower())) and len(address.strip()) <= 254


def clean_scopes(chosen) -> tuple[str, ...]:
    """read always; manage and leads when named; anything else (account included) dropped."""
    picked = {s for s in (chosen or ()) if s in SCOPES} | {"read"}
    return tuple(s for s in SCOPES if s in picked)


def live_for(conn: psycopg.Connection, business_id: str, address: str) -> dict | None:
    r = conn.execute(
        f"SELECT {COLS} FROM email_grant WHERE business_id = %s AND address = %s AND revoked_at IS NULL",
        (business_id, address.strip().lower()),
    ).fetchone()
    return dict(r) if r else None


def add(conn: psycopg.Connection, business_id: str, address: str, *, scopes, via: str, created_by: str) -> dict:
    """One live grant per (business, address): a repeat updates the scopes in place."""
    address = address.strip().lower()
    if not valid(address):
        raise ValueError("that is not an e-mail address")
    scopes = list(clean_scopes(scopes))
    live = live_for(conn, business_id, address)
    if live:
        conn.execute("UPDATE email_grant SET scopes = %s WHERE id = %s", (scopes, live["id"]))
        return {**live, "scopes": scopes}
    gid = db.new_id("eg")
    r = conn.execute(
        "INSERT INTO email_grant (id, business_id, address, scopes, via, created_by) VALUES (%s, %s, %s, %s, %s, %s) "
        f"RETURNING {COLS}",
        (gid, business_id, address, scopes, via, created_by.strip().lower()),
    ).fetchone()
    return dict(r)


def for_business(conn: psycopg.Connection, business_id: str) -> list[dict]:
    """The live grants, newest first."""
    rows = conn.execute(
        f"SELECT {COLS} FROM email_grant WHERE business_id = %s AND revoked_at IS NULL ORDER BY created_at DESC",
        (business_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke(conn: psycopg.Connection, grant_id: str, business_id: str) -> bool:
    cur = conn.execute(
        "UPDATE email_grant SET revoked_at = now() WHERE id = %s AND business_id = %s AND revoked_at IS NULL",
        (grant_id, business_id),
    )
    return cur.rowcount == 1


def touch(conn: psycopg.Connection, grant_id: str) -> None:
    conn.execute("UPDATE email_grant SET last_used_at = now() WHERE id = %s", (grant_id,))


# ---------------------------------------------------------------- /m/{token}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_link(conn: psycopg.Connection, thread_id: str, business_id: str) -> str:
    """A fresh 128-bit link for the thread's business; earlier links stay valid until used or expired."""
    token = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO email_link (token_hash, thread_id, business_id, expires_at) VALUES (%s, %s, %s, now() + %s)",
        (_hash(token), thread_id, business_id, LINK_TTL),
    )
    return token


LINK_COLS = "l.token_hash, l.thread_id, l.business_id, l.expires_at, l.used_at, t.counterpart, t.subject, t.session_id"


def peek_link(conn: psycopg.Connection, token: str) -> dict | None:
    """The live, unused link for a token, with its thread's counterpart; None otherwise. Opening does not consume."""
    if not token or len(token) > 128:
        return None
    r = conn.execute(
        f"SELECT {LINK_COLS} FROM email_link l JOIN email_thread t ON t.id = l.thread_id "
        "WHERE l.token_hash = %s AND l.used_at IS NULL AND l.expires_at > now()",
        (_hash(token),),
    ).fetchone()
    return dict(r) if r else None


def used_link(conn: psycopg.Connection, token: str) -> dict | None:
    """A link already used (any age), so the owner can be sent on to the plan; None otherwise."""
    if not token or len(token) > 128:
        return None
    r = conn.execute(
        f"SELECT {LINK_COLS} FROM email_link l JOIN email_thread t ON t.id = l.thread_id "
        "WHERE l.token_hash = %s AND l.used_at IS NOT NULL",
        (_hash(token),),
    ).fetchone()
    return dict(r) if r else None


def use_link(conn: psycopg.Connection, token_hash: str) -> bool:
    """Mark the link used; True the first time (this call won it), False when another call already had —
    the UPDATE's row lock serializes concurrent callers, so exactly one ever sees True (mail_web.link_continue's
    guard against a double-click or two tabs racing the same link)."""
    cur = conn.execute("UPDATE email_link SET used_at = now() WHERE token_hash = %s AND used_at IS NULL", (token_hash,))
    return cur.rowcount == 1
