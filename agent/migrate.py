"""One-off data moves that schema.sql's idempotent DDL cannot express (plan 2026-09-29-agent-m0-channel-framework
P11). A migration is a function `(conn) -> None` registered under a name; `db.apply_schema` calls `run_pending` after
the DDL, and each migration runs once, in its own transaction, recorded in `schema_migration`. Write them as
idempotent SQL with WHERE guards (ON CONFLICT DO NOTHING, WHERE NOT EXISTS): a transaction that fails rolls back and
the migration is tried again at the next boot, and a restored older database runs them again safely."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

Migration = Callable[[psycopg.Connection], None]
MIGRATIONS: dict[str, Migration] = {}  # name -> function, in the order they were registered (= the order they run)
# the advisory lock that lets one process at a time decide what is pending
LOCK_KEY = int.from_bytes(b"agentmig", "big")
# Admiry's pauses for the page (`ads_run.PAGE_REASONS`, kept here: db imports this module before ads_run can load)
PAGE_REASONS = ("page_down", "unreachable")
# A3p-2's own sentence for its page-down pause, which its `pause_page_down` also wrote into `last_error` (its
# unreachable pause never wrote one there); a page pause moved over says why in M0's words instead (PAUSE_ADVICE)
A3P2_PAGE_DOWN = "The page your ads point at is down; they stay off until they can move to one that is up"


def register(name: str) -> Callable[[Migration], Migration]:
    """Decorator: `@migrate.register("name")` on a function `(conn) -> None`. A name is registered once, and never
    renamed once a database has recorded it."""

    def decorate(fn: Migration) -> Migration:
        if name in MIGRATIONS:
            raise ValueError(f"migration {name!r} is registered twice")
        MIGRATIONS[name] = fn
        return fn

    return decorate


def run_pending(conn: psycopg.Connection) -> list[str]:
    """Run every registered migration this database has not recorded, oldest registration first; the names run by
    this call. A migration that raises rolls back alone (its rows and its record) and the error propagates, so a
    boot with a broken migration fails fast. Processes booting together (the app and an `agent.keys` command) take
    turns: the one that waits finds the migration recorded and skips it. Needs schema_migration, which schema.sql
    creates."""
    applied: list[str] = []
    for name, fn in list(MIGRATIONS.items()):
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            if conn.execute("SELECT 1 FROM schema_migration WHERE name = %s", (name,)).fetchone():
                continue
            fn(conn)
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (name,))
        applied.append(name)
    return applied


# ---------------------------------------------------------------- the migrations, in the order they run


@register("channel_tables_from_ads")
def channel_tables_from_ads(conn: psycopg.Connection) -> None:
    """M0 Task 3 (plan P4, P6): A3's single-channel rows into the channel tables, once. One `channel_account` row
    per business for Google Search, its mode from `business.ads_mode` and its status mirrored from `ads_link`
    (active and an operator's manual -> active, cancelled -> inactive, no link -> none; the Customer ID in
    `external`); every `ads_campaign` row as the business's `channel_campaign(google_search)` row -- A3's
    `validated` is `queued` with `validated_at` kept, an A3 `queued` row drops the `validated_at` an earlier
    version left on it (A3 checked every `queued` row again; under P4 a mark would read as "checked"), and built
    ads keep the Customer ID they were built in as `resources.customer_id` (M0 Task 4: every later step addresses
    that account); every `ads_day` row into `metric_daily` (`phone_calls` is `calls`; `leads` and `booked` come from
    the lead store, 0 until ads-sync counts them). ON CONFLICT DO NOTHING throughout: a row the new code wrote since
    is never written over. The old tables stay, read by nothing.

    Who paused a paused row (M0 Task 9 D7; A3p-2's `ads_run.pause` writes `resources.paused_by` -- "owner",
    "assistant" or "admiry" -- and, for Admiry's own pause only, `resources.pause_reason`, "page_down" or
    "unreachable"): Admiry's pause for the page (`paused_by = 'admiry'` with a reason in `ads_run.PAGE_REASONS`)
    becomes that `paused_reason` -- no owner's mark, so page-check, publish_page and set_landing_page still lift it
    and the card says "paused by Admiry"; every other pause -- the owner's or their assistant's, A3's without a
    `paused_by`, and Admiry's with no reason or one this code does not know -- gets the owner's mark
    `resources.owner_paused` (`paused_reason` is only ever Admiry's, M0 Task 4 fix round 1), the conservative side:
    it stays paused until someone acts. `pause_reason` itself is not carried (the column is its home now), nor, on a
    page pause moved over, A3p-2's own page-down sentence in `last_error` (the status line says the reason and its
    way out in M0's words); A3p-2's page keys -- `final_url`, `url_stale`, `refused_url` -- are, as they are."""
    conn.execute(
        "INSERT INTO channel_account (business_id, channel, mode, status, external) "
        "SELECT b.id, 'google_search', b.ads_mode, "
        "CASE l.status WHEN 'active' THEN 'active' WHEN 'manual' THEN 'active' WHEN 'pending' THEN 'pending' "
        "WHEN 'refused' THEN 'refused' WHEN 'cancelled' THEN 'inactive' WHEN 'inactive' THEN 'inactive' "
        "ELSE 'none' END, "
        "CASE WHEN l.customer_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('customer_id', l.customer_id) END "
        "FROM business b LEFT JOIN ads_link l ON l.business_id = b.id "
        "ON CONFLICT DO NOTHING"
    )
    page_pause = (  # A3p-2's own pause of Admiry's for the page (D7); anything else paused is the owner's
        "COALESCE(state = 'paused' AND resources->>'paused_by' = 'admiry' "
        "AND resources->>'pause_reason' = ANY(%(page)s), false)"
    )
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, paused_reason, resources, last_error, "
        "validated_at, launched_at, paused_at, synced_at, updated_at) "
        "SELECT business_id, 'google_search', plan_id, CASE state WHEN 'validated' THEN 'queued' ELSE state END, "
        f"CASE WHEN {page_pause} THEN resources->>'pause_reason' END, "
        "(CASE WHEN resources ? 'campaign' THEN resources || jsonb_build_object('customer_id', customer_id) "
        "ELSE resources END - 'pause_reason') "
        f"|| CASE WHEN state = 'paused' AND NOT {page_pause} THEN '{{\"owner_paused\": true}}'::jsonb "
        "ELSE '{}'::jsonb END, "
        f"CASE WHEN {page_pause} AND last_error = %(page_down_said)s THEN NULL ELSE last_error END, "
        "CASE WHEN state = 'queued' THEN NULL ELSE validated_at END, launched_at, paused_at, synced_at, updated_at "
        "FROM ads_campaign "
        "ON CONFLICT DO NOTHING",
        {"page": list(PAGE_REASONS), "page_down_said": A3P2_PAGE_DOWN},
    )
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source, synced_at) "
        "SELECT business_id, 'google_search', day, cost_micros, impressions, clicks, phone_calls, 0, conversions, 0, "
        "'api', synced_at FROM ads_day "
        "ON CONFLICT DO NOTHING"
    )


@register("lead_kind_other")
def lead_kind_other(conn: psycopg.Connection) -> None:
    """G1 (spec 2026-10-01 §5, D163): a lead the owner reports can be a walk-in, `kind = 'other'`. The check on
    `lead.kind` was written into CREATE TABLE, which a database that already has the table never reads again; this
    replaces it once. Run again (a restored older database), it replaces the same check with the same check."""
    conn.execute("ALTER TABLE lead DROP CONSTRAINT IF EXISTS lead_kind_check")
    conn.execute("ALTER TABLE lead ADD CONSTRAINT lead_kind_check CHECK (kind IN ('form', 'call', 'other'))")


@register("listing_unconfirmed_dropped")
def listing_unconfirmed_dropped(conn: psycopg.Connection) -> None:
    """Plan 2026-10-03-agent-listing-confirm, D7: before this release `start` stored Google's best match as the
    business's listing without asking the owner. Every business whose latest knowledge holds such a listing (no
    `kind`, no `confirmed`) gets one new version (source "migration") without it -- or, when the owner had put the ads'
    centre on it (`placed`), with that address and point kept as an address the owner placed. The next consultation
    offers places again, and the owner picks. Run again, it finds nothing to change."""
    rows = conn.execute(
        "SELECT DISTINCT ON (business_id) business_id, version, json FROM knowledge ORDER BY business_id, version DESC"
    ).fetchall()
    for r in rows:
        knowledge = r["json"]
        listing = knowledge.get("listing") if isinstance(knowledge, dict) else None
        if not isinstance(listing, dict) or listing.get("kind") == "address" or listing.get("confirmed") is True:
            continue
        out = {key: value for key, value in knowledge.items() if key != "listing"}
        if listing.get("placed") is True:
            out["listing"] = {
                "address": listing.get("address"),
                "lat": listing.get("lat"),
                "lng": listing.get("lng"),
                "source": "Google Maps",
                "kind": "address",
            }
        conn.execute(
            "INSERT INTO knowledge (business_id, version, json, source) VALUES (%s, %s, %s, 'migration')",
            (r["business_id"], r["version"] + 1, Jsonb(out)),
        )


@register("lead_kind_booking")
def lead_kind_booking(conn: psycopg.Connection) -> None:
    """A3p-3 batch 2 (spec 2026-10-03 P27): a booking completed on the page is a lead of its own kind, 'booking'. Like
    lead_kind_other, it replaces the check CREATE TABLE wrote, once (after lead_kind_other on a new database)."""
    conn.execute("ALTER TABLE lead DROP CONSTRAINT IF EXISTS lead_kind_check")
    conn.execute("ALTER TABLE lead ADD CONSTRAINT lead_kind_check CHECK (kind IN ('form', 'call', 'other', 'booking'))")


@register("page_event_booked_online")
def page_event_booked_online(conn: psycopg.Connection) -> None:
    """A3p-3 batch 2 (spec 2026-10-03 P27): the page's beacon reports a booking completed on the page,
    'booked_online'."""
    conn.execute("ALTER TABLE page_event DROP CONSTRAINT IF EXISTS page_event_kind_check")
    conn.execute(
        "ALTER TABLE page_event ADD CONSTRAINT page_event_kind_check "
        "CHECK (kind IN ('visit', 'call', 'mail', 'book', 'form', 'booked_online'))"
    )


@register("payment_session_rails")
def _payment_session_rails(conn: psycopg.Connection) -> None:
    """D92 (plan 2026-10-04-agent-m1m-1 P2): every session from before the payment cost was a card payment credited
    in full -- v0's terms, which those payments keep."""
    conn.execute(
        "UPDATE payment_session SET rail = 'card', credited_cents = amount_cents, processing_cents = 0 "
        "WHERE rail IS NULL"
    )


_WEEKLY = "Weekly summary by e-mail · %"
# the line without its e-mail clause, first letter capitalised: "Automatic optimization: safe changes only"
_WITHOUT = "upper(left(split_part({x}, ' · ', 2), 1)) || substr(split_part({x}, ' · ', 2), 2)"


@register("plan_lines_without_weekly_summary")
def plan_lines_without_weekly_summary(conn: psycopg.Connection) -> None:
    """The daily digest (Dg plan P8, founder 2026-09-30; plan 2026-10-07-agent-k2 Task 14): a stored plan line promises
    no e-mail. The optimization line of every plan, and of an open approve-plan proposal's `what_changes`, loses its
    "Weekly summary by e-mail · " clause; every other line and every closed proposal stays as it is."""
    line = "json->'lines'->>'optimization'"
    conn.execute(
        f"UPDATE plan SET json = jsonb_set(json, '{{lines,optimization}}', to_jsonb({_WITHOUT.format(x=line)})) "
        f"WHERE {line} LIKE %s",
        (_WEEKLY,),
    )
    conn.execute(
        "UPDATE proposal SET payload = jsonb_set(payload, '{what_changes}', (SELECT jsonb_agg(CASE WHEN e LIKE %s "
        f"THEN to_jsonb({_WITHOUT.format(x='e')}) ELSE to_jsonb(e) END ORDER BY n) "
        "FROM jsonb_array_elements_text(payload->'what_changes') WITH ORDINALITY AS t(e, n))) "
        "WHERE kind = 'approve_plan' AND status = 'proposed' AND jsonb_typeof(payload->'what_changes') = 'array' "
        "AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(payload->'what_changes') AS w(e) WHERE e LIKE %s)",
        (_WEEKLY, _WEEKLY),
    )
