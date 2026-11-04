"""Postgres connection + idempotent schema application (mirrors adsmanager/db.py).

One autocommit connection per request/tool call is enough at A1 scale (spec §4.6:
1,000 businesses ≈ 8 min per hourly tick on one shard); a pool is a later change.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import migrate

DEFAULT_URL = "postgresql:///bidx_agent"


def database_url(url: str | None = None) -> str:
    return url or os.environ.get("AGENT_DATABASE_URL") or DEFAULT_URL  # "" from compose counts as unset


def connect(url: str | None = None) -> psycopg.Connection:
    """Open an autocommit connection with dict-like rows."""
    return psycopg.connect(database_url(url), autocommit=True, row_factory=dict_row)


def apply_schema(conn: psycopg.Connection) -> None:
    """Apply schema.sql (all IF NOT EXISTS) as one multi-statement query, then the registered one-off migrations
    that this database has not run yet (agent/migrate.py)."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
    migrate.run_pending(conn)


def new_id(prefix: str) -> str:
    """Opaque row id: '<prefix>_' + 16 hex chars (64 bits of entropy)."""
    return f"{prefix}_{secrets.token_hex(8)}"
