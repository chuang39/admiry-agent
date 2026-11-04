"""agent/schema.sql against a database with nothing in it yet (A3p-2 Task 3 round 4, H28): CI's postgres service
and the first boot of a new box start from an empty database, not from one an earlier version of the file built."""

from __future__ import annotations

import secrets

from agent import db


def test_schema_applies_to_a_fresh_database(conn):
    """Every statement may only use what an earlier statement in the file created. The session's test database
    cannot show a slip (the `_schema` fixture built every table before this test ran), so this applies the file
    inside a fresh PostgreSQL schema put first on the search_path: every unqualified name the file creates or
    reads resolves there, exactly as in an empty database. Applied twice, as every boot applies it again."""
    name = f"fresh_{secrets.token_hex(4)}"
    conn.execute(f"CREATE SCHEMA {name}")
    try:
        conn.execute(f"SET search_path TO {name}")
        db.apply_schema(conn)
        db.apply_schema(conn)
        tables = {
            r["table_name"]
            for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (name,))
        }
        assert {"business", "page", "page_version", "ads_campaign", "channel_campaign", "schema_migration"} <= tables
    finally:
        conn.execute("SET search_path TO DEFAULT")
        conn.execute(f"DROP SCHEMA {name} CASCADE")
