"""API keys (spec 2026-09-24 §4.0 "Keys: class × scopes").

    ak_<40 hex>   the plaintext, returned exactly once by mint(); only its SHA-256 is stored
    class         draft | business | platform   -- whose key it is
    scopes        read | manage | leads | account -- what it may do

Draft keys (minted by `start`, part 2) are read+manage on a draft business and always
live 24 hours -- ttl_hours is ignored for drafts, never caller-overridable; business
keys are minted on /connect (A1b); platform keys are minted by an operator with
`python -m agent.keys mint --class platform --label ops` and pass every scope check.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from . import db
from .util import iso

CLASSES = ("draft", "business", "platform")
SCOPES = ("read", "manage", "leads", "account")
PREFIX = "ak_"
LOOKUP_PREFIXES = (PREFIX, "oa_")  # ak_ keys and oa_ OAuth access tokens (A1b-4b) share the api_key table
DRAFT_TTL_HOURS = 24
DRAFT_SCOPES = ("read", "manage")


@dataclass(frozen=True)
class KeyRecord:
    id: str
    business_id: str | None
    label: str
    cls: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    source: str
    grant_id: str | None

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.expires_at is not None and self.expires_at <= now

    def allows(self, scope: str) -> bool:
        return self.cls == "platform" or scope in self.scopes


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _record(r) -> KeyRecord:
    return KeyRecord(
        id=r["id"],
        business_id=r["business_id"],
        label=r["label"],
        cls=r["class"],
        scopes=tuple(r["scopes"]),
        expires_at=r["expires_at"],
        revoked_at=r["revoked_at"],
        last_used_at=r["last_used_at"],
        source=r["source"],
        grant_id=r["grant_id"],
    )


def mint(
    conn: psycopg.Connection,
    *,
    business_id: str | None,
    cls: str,
    scopes: Iterable[str],
    label: str,
    ttl_hours: float | None = None,
    source: str = "key",
    grant_id: str | None = None,
    prefix: str = "ak",
    ttl: timedelta | None = None,
) -> tuple[str, KeyRecord]:
    """Create a key; returns (plaintext, record). The plaintext is never stored.

    `ttl` (a timedelta) is for OAuth access tokens (A1b-4b); `ttl_hours` is the older, caller-facing form
    (draft keys always get DRAFT_TTL_HOURS regardless of either). Passing both is not meaningful; `ttl_hours`
    wins when both are given (it is checked first)."""
    if cls not in CLASSES:
        raise ValueError(f"unknown key class {cls!r}")
    scopes = tuple(scopes)
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ValueError(f"unknown scopes {unknown}")
    if cls == "platform" and business_id is not None:
        raise ValueError("platform keys are not bound to a business")
    if cls != "platform" and business_id is None:
        raise ValueError(f"{cls} keys need a business_id")
    if cls == "draft":
        ttl_hours = DRAFT_TTL_HOURS
        scopes = DRAFT_SCOPES
    if ttl_hours is not None and ttl_hours <= 0:
        raise ValueError("ttl_hours must be positive (omit it for a key that does not expire)")
    plaintext = f"{prefix}_" + secrets.token_hex(20 if prefix == "ak" else 24)
    if ttl_hours is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    elif ttl is not None:
        expires_at = datetime.now(timezone.utc) + ttl
    else:
        expires_at = None
    row = conn.execute(
        "INSERT INTO api_key (id, key_hash, business_id, label, class, scopes, expires_at, source, grant_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (db.new_id("key"), hash_key(plaintext), business_id, label, cls, list(scopes), expires_at, source, grant_id),
    ).fetchone()
    return plaintext, _record(row)


def lookup(conn: psycopg.Connection, plaintext: str) -> KeyRecord | None:
    """The record for a plaintext key — expired and revoked ones included; callers decide."""
    if not plaintext or not plaintext.startswith(LOOKUP_PREFIXES):
        return None
    row = conn.execute("SELECT * FROM api_key WHERE key_hash = %s", (hash_key(plaintext),)).fetchone()
    return _record(row) if row else None


def get(conn: psycopg.Connection, key_id: str) -> KeyRecord | None:
    """A key by id (any state) — revoke_key reads back grant_id after revoking one, to revoke its OAuth
    grant too."""
    row = conn.execute("SELECT * FROM api_key WHERE id = %s", (key_id,)).fetchone()
    return _record(row) if row else None


def touch(conn: psycopg.Connection, key_id: str) -> None:
    conn.execute("UPDATE api_key SET last_used_at = now() WHERE id = %s", (key_id,))


def revoke(conn: psycopg.Connection, key_id: str, business_id: str | None) -> bool:
    """Revoke an active key of the given business (business_id=None: any business, operators only)."""
    if business_id is None:
        cur = conn.execute("UPDATE api_key SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,))
    else:
        cur = conn.execute(
            "UPDATE api_key SET revoked_at = now() WHERE id = %s AND business_id = %s AND revoked_at IS NULL",
            (key_id, business_id),
        )
    return cur.rowcount == 1


def upgrade_draft(conn: psycopg.Connection, business_id: str) -> str | None:
    """Make the business's newest live draft key a lasting business key (class business, no expiry, labelled
    `website` since only the homepage mints drafts) in place; returns its id, or None when there is no live draft
    key. ops.verify and claims.claim share this."""
    row = conn.execute(
        "UPDATE api_key SET class = 'business', expires_at = NULL, label = 'website' WHERE id = ("
        "  SELECT id FROM api_key WHERE business_id = %s AND class = 'draft' AND revoked_at IS NULL"
        "  AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC, id DESC LIMIT 1) "
        "RETURNING id",
        (business_id,),
    ).fetchone()
    return row["id"] if row else None


def list_for_business(conn: psycopg.Connection, business_id: str, *, include_oauth: bool = True) -> list[dict]:
    """Key metadata for the owner (never the hash), timestamps as ISO strings.

    `include_oauth=False` drops `oa_` access tokens (source 'oauth'): the Keys list (Settings › Advanced) passes
    this, since its own "Connected assistants" block already covers those (by grant, with its own revoke that
    also ends the grant and its refresh token — Keys' plain `keys.revoke` does not). The `list_keys` tool
    keeps the default: an assistant with the `account` scope sees every live credential on the business,
    OAuth-issued ones included, with no separate "grants" view of its own to duplicate against — but only
    the live ones: every refresh revokes the previous access token and every one expires within the hour,
    so the spent `oa_` rows are hidden (final review I5; pruning them is a later tick job). Revoked or
    expired `ak_` keys are still listed."""
    query = "SELECT id, label, class, scopes, created_at, expires_at, last_used_at, revoked_at, source FROM api_key WHERE business_id = %s"
    if include_oauth:
        query += " AND (source <> 'oauth' OR (revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())))"
    else:
        query += " AND source <> 'oauth'"
    rows = conn.execute(query + " ORDER BY created_at", (business_id,)).fetchall()
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "class": r["class"],
            "scopes": list(r["scopes"]),
            "created_at": iso(r["created_at"]),
            "expires_at": iso(r["expires_at"]),
            "last_used_at": iso(r["last_used_at"]),
            "revoked_at": iso(r["revoked_at"]),
            "source": r["source"],
        }
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    """Operator CLI: mint a key and print it once.

    python -m agent.keys mint --class platform --label ops
    python -m agent.keys mint --class business --business biz_x --scopes read,manage,leads --label muse
    """
    parser = argparse.ArgumentParser(
        prog="agent.keys", description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="mint a key and print it once")
    m.add_argument("--class", dest="cls", choices=CLASSES, required=True)
    m.add_argument("--business", default=None, help="business id (business/draft keys)")
    m.add_argument("--scopes", default="read", help="comma-separated: read,manage,leads,account")
    m.add_argument("--label", required=True)
    m.add_argument("--ttl-hours", type=float, default=None)
    args = parser.parse_args(argv)
    with db.connect() as conn:
        db.apply_schema(conn)
        plaintext, rec = mint(
            conn,
            business_id=args.business,
            cls=args.cls,
            scopes=[s for s in args.scopes.split(",") if s],
            label=args.label,
            ttl_hours=args.ttl_hours,
        )
    print(
        f"id={rec.id} class={rec.cls} scopes={','.join(rec.scopes)} expires_at={iso(rec.expires_at)}", file=sys.stderr
    )
    print(plaintext)  # stdout carries only the key, so `$(python -m agent.keys mint ...)` works
    return 0


if __name__ == "__main__":
    sys.exit(main())
