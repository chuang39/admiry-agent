"""agent/migrate.py: the one-off data moves schema.sql's idempotent DDL cannot express. Each runs once, in its own
transaction, and is recorded in schema_migration (plan 2026-09-29-agent-m0-channel-framework P11)."""

import threading

import psycopg
import pytest

from agent import business, db, migrate
from agent.tests.conftest import TEST_DB_URL

# The check on lead.kind as CREATE TABLE wrote it before G1 (ra_security/migration_probe.sql), and as it is now
TWO_KINDS = "CHECK (kind IN ('form', 'call'))"
THREE_KINDS = "CHECK (kind IN ('form', 'call', 'other'))"
FOUR_KINDS = "CHECK (kind IN ('form', 'call', 'other', 'booking'))"  # A3p-3 batch 2: the schema every test expects
EVENT_KINDS = "CHECK (kind IN ('visit', 'call', 'mail', 'book', 'form', 'booked_online'))"


@pytest.fixture
def migrations(monkeypatch, conn):
    """migrate.MIGRATIONS as this test's own empty registry (the real migrations stay out of it), and no trace of the
    test's migrations in schema_migration afterwards (that table outlives a test, like the DB itself)."""
    monkeypatch.setattr(migrate, "MIGRATIONS", {})
    yield migrate.MIGRATIONS
    conn.execute("DELETE FROM schema_migration WHERE name = ANY(%s)", (list(migrate.MIGRATIONS),))


def _businesses(conn, biz_id: str) -> int:
    return conn.execute("SELECT count(*) AS n FROM business WHERE id = %s", (biz_id,)).fetchone()["n"]


def _recorded(conn, name: str) -> int:
    return conn.execute("SELECT count(*) AS n FROM schema_migration WHERE name = %s", (name,)).fetchone()["n"]


def test_a_registered_migration_runs_once_and_is_recorded(conn, migrations):
    calls = []

    @migrate.register("test_first")
    def first(c):
        calls.append(1)
        c.execute("INSERT INTO business (id) VALUES ('biz_m1')")

    assert migrate.run_pending(conn) == ["test_first"]
    assert migrate.run_pending(conn) == []
    assert calls == [1]
    assert _businesses(conn, "biz_m1") == 1
    assert _recorded(conn, "test_first") == 1


def test_migrations_run_in_the_order_they_were_registered(conn, migrations):
    for name in ("test_b", "test_a", "test_c"):
        migrate.register(name)(lambda c: None)
    assert migrate.run_pending(conn) == ["test_b", "test_a", "test_c"]


def test_a_failed_migration_leaves_nothing_behind_and_is_retried(conn, migrations):
    state = {"broken": True}

    @migrate.register("test_flaky")
    def flaky(c):
        c.execute("INSERT INTO business (id) VALUES ('biz_m2')")
        if state["broken"]:
            raise RuntimeError("halfway")

    with pytest.raises(RuntimeError, match="halfway"):
        migrate.run_pending(conn)
    assert _businesses(conn, "biz_m2") == 0
    assert _recorded(conn, "test_flaky") == 0
    state["broken"] = False
    assert migrate.run_pending(conn) == ["test_flaky"]
    assert _businesses(conn, "biz_m2") == 1


def test_apply_schema_runs_the_pending_migrations(conn, migrations):
    migrate.register("test_at_boot")(lambda c: c.execute("INSERT INTO business (id) VALUES ('biz_m3')"))
    db.apply_schema(conn)
    db.apply_schema(conn)  # the second boot finds it recorded
    assert _businesses(conn, "biz_m3") == 1
    assert _recorded(conn, "test_at_boot") == 1


def test_a_name_is_registered_once(migrations):
    migrate.register("test_twice")(lambda c: None)
    with pytest.raises(ValueError, match="test_twice"):
        migrate.register("test_twice")(lambda c: None)


def _kind_checks(conn) -> list[tuple]:
    """The check constraints on lead.kind, by name and definition."""
    return [
        (r["conname"], r["def"])
        for r in conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conrelid = 'lead'::regclass AND contype = 'c' AND conname LIKE %s ORDER BY conname",
            ("lead_kind%",),
        )
    ]


def _insert_lead(conn, lead_id: str, kind: str) -> None:
    conn.execute("INSERT INTO lead (id, business_id, kind) VALUES (%s, 'biz_m4', %s)", (lead_id, kind))


def test_lead_kind_other_moves_a_database_off_the_two_kinds_check(conn):
    """Open item A3: a database whose lead table was created before G1 has the two-kinds check, which schema.sql's
    CREATE TABLE never reads again. The migration lead_kind_other, pending there, makes room for 'other' (a walk-in
    the owner reports); run again (a restored older database) it changes nothing. The schema is put back as every
    other test expects it, whatever happens here."""
    try:
        conn.execute("ALTER TABLE lead DROP CONSTRAINT lead_kind_check")
        conn.execute(f"ALTER TABLE lead ADD CONSTRAINT lead_kind_check {TWO_KINDS}")
        conn.execute("DELETE FROM schema_migration WHERE name = 'lead_kind_other'")
        conn.execute("INSERT INTO business (id) VALUES ('biz_m4')")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_lead(conn, "ld_before", "other")
        assert migrate.run_pending(conn) == ["lead_kind_other"]
        _insert_lead(conn, "ld_after", "other")
        migrated = _kind_checks(conn)
        assert migrated == [
            ("lead_kind_check", "CHECK ((kind = ANY (ARRAY['form'::text, 'call'::text, 'other'::text])))")
        ]
        with conn.transaction():
            migrate.lead_kind_other(conn)  # again
        assert _kind_checks(conn) == migrated and _recorded(conn, "lead_kind_other") == 1
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM lead WHERE business_id = 'biz_m4' ORDER BY id")]
        assert kinds == ["other"]
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_lead(conn, "ld_walkin", "walkin")
    finally:
        conn.execute("DELETE FROM lead WHERE business_id = 'biz_m4'")
        conn.execute("ALTER TABLE lead DROP CONSTRAINT IF EXISTS lead_kind_check")
        conn.execute(f"ALTER TABLE lead ADD CONSTRAINT lead_kind_check {FOUR_KINDS}")
        conn.execute("INSERT INTO schema_migration (name) VALUES ('lead_kind_other') ON CONFLICT DO NOTHING")


def test_a_booking_completed_on_the_page_is_a_lead_and_an_event_of_its_own_kind(conn):
    """A3p-3 batch 2 (P27): a database whose tables hold the earlier kind checks gets 'booking' on the lead and
    'booked_online' on the page event from the two migrations, run once each, in order after lead_kind_other. The
    schema is put back as every other test expects it."""
    try:
        conn.execute("ALTER TABLE lead DROP CONSTRAINT lead_kind_check")
        conn.execute(f"ALTER TABLE lead ADD CONSTRAINT lead_kind_check {THREE_KINDS}")
        conn.execute("ALTER TABLE page_event DROP CONSTRAINT page_event_kind_check")
        conn.execute(
            "ALTER TABLE page_event ADD CONSTRAINT page_event_kind_check "
            "CHECK (kind IN ('visit', 'call', 'mail', 'book', 'form'))"
        )
        conn.execute("DELETE FROM schema_migration WHERE name IN ('lead_kind_booking', 'page_event_booked_online')")
        conn.execute("INSERT INTO business (id) VALUES ('biz_m5')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO lead (id, business_id, kind) VALUES ('ld_b0', 'biz_m5', 'booking')")
        assert migrate.run_pending(conn) == ["lead_kind_booking", "page_event_booked_online"]
        conn.execute("INSERT INTO lead (id, business_id, kind) VALUES ('ld_b1', 'biz_m5', 'booking')")
        assert _kind_checks(conn) == [
            (
                "lead_kind_check",
                "CHECK ((kind = ANY (ARRAY['form'::text, 'call'::text, 'other'::text, 'booking'::text])))",
            )
        ]
        assert migrate.run_pending(conn) == []  # once
    finally:
        conn.execute("DELETE FROM lead WHERE business_id = 'biz_m5'")
        conn.execute("ALTER TABLE lead DROP CONSTRAINT IF EXISTS lead_kind_check")
        conn.execute(f"ALTER TABLE lead ADD CONSTRAINT lead_kind_check {FOUR_KINDS}")
        conn.execute("ALTER TABLE page_event DROP CONSTRAINT IF EXISTS page_event_kind_check")
        conn.execute(f"ALTER TABLE page_event ADD CONSTRAINT page_event_kind_check {EVENT_KINDS}")
        for name in ("lead_kind_booking", "page_event_booked_online"):
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))


def test_two_runners_at_once_apply_a_migration_once(conn, migrations):
    """The app and an `agent.keys` command can boot together: neither may run a migration the other is running, and
    the second must not die on the record the first wrote."""
    calls, errors = [], []
    together = threading.Barrier(2)

    @migrate.register("test_slow")
    def slow(c):
        calls.append(1)
        c.execute("SELECT pg_sleep(0.3)")

    def boot():
        with db.connect(TEST_DB_URL) as c:
            together.wait()
            try:
                migrate.run_pending(c)
            except Exception as e:  # noqa: BLE001 - the test reports whatever a runner died of
                errors.append(e)

    runners = [threading.Thread(target=boot, daemon=True) for _ in range(2)]
    for r in runners:
        r.start()
    for r in runners:
        r.join(10)
    assert not any(r.is_alive() for r in runners)
    assert errors == []
    assert calls == [1]


def test_listings_nobody_confirmed_are_dropped_and_a_placed_centre_kept_as_an_address(conn):
    guess = {"place_id": "ChIJ1", "name": "Twin Smiles", "address": "1 A St", "lat": 1.0, "lng": 2.0}
    moved = {**guess, "address": "12 Elm St, Houston, TX", "placed": True}
    confirmed = {**guess, "confirmed": True}
    rest = {"name": "X", "services": [{"id": "svc_cleanings", "name": "Cleanings"}], "listing_rejected": ["ChIJ9"]}
    for biz, listing in (("biz_1", guess), ("biz_2", moved), ("biz_3", confirmed), ("biz_4", None)):
        conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
        business.add_knowledge(conn, biz, {**rest, **({"listing": listing} if listing else {})}, source="extract")
    # only the latest version counts: a guess in an older version under a newer one without it is left alone
    conn.execute("INSERT INTO business (id) VALUES ('biz_5')")
    business.add_knowledge(conn, "biz_5", {**rest, "listing": guess}, source="extract")
    business.add_knowledge(conn, "biz_5", rest, source="owner")
    migrate.listing_unconfirmed_dropped(conn)
    ids = ("biz_1", "biz_2", "biz_3", "biz_4", "biz_5")
    latest = {biz: business.latest_knowledge(conn, biz) for biz in ids}
    assert latest["biz_1"][0] == 2 and "listing" not in latest["biz_1"][1]
    assert latest["biz_1"][1] == rest  # the rest of the knowledge is the new version's, unchanged
    assert latest["biz_2"][0] == 2
    assert latest["biz_2"][1] == {
        **rest,
        "listing": {
            "address": "12 Elm St, Houston, TX",
            "lat": 1.0,
            "lng": 2.0,
            "source": "Google Maps",
            "kind": "address",
        },
    }
    assert latest["biz_3"][0] == 1 and latest["biz_4"][0] == 1 and latest["biz_5"][0] == 2  # nothing to change
    assert latest["biz_3"][1] == {**rest, "listing": confirmed}
    for biz in ("biz_1", "biz_2"):
        source = conn.execute("SELECT source FROM knowledge WHERE business_id = %s AND version = 2", (biz,)).fetchone()
        assert source["source"] == "migration"
    migrate.listing_unconfirmed_dropped(conn)  # run again (a restored database): nothing more
    assert {biz: business.latest_knowledge(conn, biz) for biz in ids} == latest
