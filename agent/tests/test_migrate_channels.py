"""The one-off move of A3's single-channel rows into the channel tables (agent/migrate.py `channel_tables_from_ads`;
plan 2026-09-29-agent-m0-channel-framework P4, P6, P11): one `channel_account(google_search)` row per business from
`business.ads_mode` and `ads_link`, `ads_campaign` into `channel_campaign` (A3's `validated` becomes `queued` with its
`validated_at` kept, an owner's pause gets its reason), `ads_day` into `metric_daily`. The session's schema already ran
it once, on whatever the database held then; these tests call it directly on the rows they write."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, config, migrate
from agent.tests.test_ads_run import gads, live  # noqa: F401 - fixtures

NAME = "channel_tables_from_ads"
AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)  # every timestamp the old rows carry, a day apart
BUILT = {"campaign": "customers/1234567890/campaigns/8", "campaign_budget": "customers/1234567890/campaignBudgets/9"}


def move(conn) -> None:
    migrate.MIGRATIONS[NAME](conn)


def _business(conn, biz: str, *, mode: str = "own_account", link: str | None = None, cid: str | None = None) -> None:
    conn.execute("INSERT INTO business (id, ads_mode) VALUES (%s, %s)", (biz, mode))
    if link:
        conn.execute("INSERT INTO ads_link (business_id, customer_id, status) VALUES (%s, %s, %s)", (biz, cid, link))


def _campaign(conn, biz: str, state: str, *, resources: dict | None = None, last_error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO ads_campaign (business_id, customer_id, plan_id, state, resources, last_error, validated_at, "
        "launched_at, paused_at, synced_at, updated_at) VALUES (%s, '1234567890', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            biz,
            f"pl_{biz}",
            state,
            Jsonb(resources or {}),
            last_error,
            AT,
            AT + timedelta(days=1) if state != "validated" else None,
            AT + timedelta(days=2) if state == "paused" else None,
            AT + timedelta(days=3),
            AT + timedelta(days=4),
        ),
    )


def _accounts(conn) -> dict:
    rows = conn.execute("SELECT business_id, channel, mode, status, external, secret_enc FROM channel_account")
    return {r["business_id"]: r for r in rows.fetchall()}


def _campaigns(conn) -> dict:
    rows = conn.execute(
        "SELECT business_id, channel, plan_id, state, paused_reason, resources, last_error, validated_at, launched_at, "
        "paused_at, synced_at, updated_at FROM channel_campaign"
    )
    return {r["business_id"]: r for r in rows.fetchall()}


def test_the_move_is_registered_and_ran_when_the_schema_was_applied(conn):
    assert NAME in migrate.MIGRATIONS
    assert conn.execute("SELECT 1 FROM schema_migration WHERE name = %s", (NAME,)).fetchone() is not None


def test_every_business_gets_its_google_search_account_row_mirrored_from_the_link(conn):
    links = {  # ads_link.status -> channel_account.status (plan P6; the Task 2 ruling's vocabulary)
        "biz_active": ("active", "active"),
        "biz_manual": ("manual", "active"),
        "biz_pending": ("pending", "pending"),
        "biz_refused": ("refused", "refused"),
        "biz_cancelled": ("cancelled", "inactive"),
        "biz_inactive": ("inactive", "inactive"),
    }
    for n, (biz, (link, _status)) in enumerate(links.items()):
        _business(conn, biz, link=link, cid=f"12345678{n:02d}")
    _business(conn, "biz_none")
    _business(conn, "biz_managed", mode="managed")
    move(conn)
    accounts = _accounts(conn)
    assert set(accounts) == {*links, "biz_none", "biz_managed"}
    for n, (biz, (_link, status)) in enumerate(links.items()):
        a = accounts[biz]
        assert (a["channel"], a["mode"], a["status"], a["external"]) == (
            "google_search",
            "own_account",
            status,
            {"customer_id": f"12345678{n:02d}"},
        ), biz
    assert (accounts["biz_none"]["status"], accounts["biz_none"]["external"]) == ("none", {})
    managed = accounts["biz_managed"]
    assert (managed["mode"], managed["status"], managed["external"]) == ("managed", "none", {})
    assert all(a["secret_enc"] is None for a in accounts.values())  # a link carries no token


def test_a_validated_row_moves_as_queued_with_its_check_and_a_paused_one_with_its_reason(conn):
    """Plan P4: A3's `validated` is `queued` with `validated_at` set (launchable under `live`, ruling S9); an owner's
    pause (A3 had no other kind) is `paused` with the owner's mark (`resources.owner_paused`; M0 Task 4 fix round 1:
    `paused_reason` is only ever Admiry's). Everything else is copied as it was."""
    for biz in ("biz_v", "biz_p", "biz_l", "biz_f"):
        _business(conn, biz)
    _campaign(conn, "biz_v", "validated")
    paused = {**BUILT, "paused_by": "assistant", "pause_owed": True, "time_zone": "America/Los_Angeles"}
    _campaign(conn, "biz_p", "paused", resources=paused, last_error="Google Ads was not told yet")
    _campaign(conn, "biz_l", "live", resources=BUILT)
    _campaign(conn, "biz_f", "failed", last_error="Google Ads refused this request")
    move(conn)
    rows = _campaigns(conn)
    assert {biz: (r["channel"], r["state"], r["paused_reason"]) for biz, r in rows.items()} == {
        "biz_v": ("google_search", "queued", None),
        "biz_p": ("google_search", "paused", None),
        "biz_l": ("google_search", "live", None),
        "biz_f": ("google_search", "failed", None),
    }
    v = rows["biz_v"]
    assert (v["plan_id"], v["resources"], v["last_error"], v["validated_at"]) == ("pl_biz_v", {}, None, AT)
    assert v["launched_at"] is None
    p = rows["biz_p"]
    # the keys exactly as A3's, and -- built ads (M0 Task 4) -- the Customer ID they were built in, from its column
    built_in = {"customer_id": "1234567890"}
    owners = {"owner_paused": True}
    assert (p["resources"], p["last_error"]) == ({**paused, **built_in, **owners}, "Google Ads was not told yet")
    assert (p["validated_at"], p["launched_at"], p["paused_at"]) == (AT, AT + timedelta(days=1), AT + timedelta(days=2))
    assert (p["synced_at"], p["updated_at"]) == (AT + timedelta(days=3), AT + timedelta(days=4))
    assert rows["biz_l"]["resources"] == {**BUILT, **built_in}
    assert rows["biz_f"]["last_error"] == "Google Ads refused this request"


def test_an_a3_queued_row_is_never_taken_for_one_google_checked(conn):
    """A3 kept `validated_at` when a new version was queued over a checked one (or a checked row was paused and
    resumed), and checked every `queued` row again; under P4 a `queued` row with validated_at set means "checked", so
    the move drops that stale mark -- `validate` still checks the version, and the owner still reads "being set up"."""
    _business(conn, "biz_q")
    _campaign(conn, "biz_q", "queued")  # validated_at = AT, from the version before
    move(conn)
    moved = _campaigns(conn)["biz_q"]
    assert (moved["state"], moved["validated_at"]) == ("queued", None)


def test_ads_day_rows_move_with_their_channel_and_their_calls(conn):
    _business(conn, "biz_1", link="active", cid="1234567890")
    synced = AT + timedelta(hours=5)
    conn.execute(
        "INSERT INTO ads_day (business_id, day, cost_micros, clicks, impressions, phone_calls, conversions, synced_at) "
        "VALUES ('biz_1', '2026-09-25', 5000000, 3, 40, 2, 1.5, %s), ('biz_1', '2026-09-26', 0, 0, 0, 0, 0, %s)",
        (synced, synced),
    )
    move(conn)
    rows = conn.execute(
        "SELECT business_id, channel, day, cost_micros, impressions, clicks, calls, leads, conversions, booked, source, "
        "synced_at FROM metric_daily ORDER BY day"
    ).fetchall()
    assert [(r["channel"], r["day"]) for r in rows] == [
        ("google_search", date(2026, 9, 25)),
        ("google_search", date(2026, 9, 26)),
    ]
    first = rows[0]
    assert (first["cost_micros"], first["impressions"], first["clicks"], first["calls"]) == (5_000_000, 40, 3, 2)
    assert (float(first["conversions"]), first["leads"], first["booked"], first["source"]) == (1.5, 0, 0, "api")
    assert first["synced_at"] == synced


def test_the_move_twice_duplicates_nothing_and_never_writes_over_a_newer_row(conn):
    """P11: a half-run, or a restored older database, runs it again safely -- ON CONFLICT DO NOTHING, so a row the
    new code wrote since stays as it is."""
    _business(conn, "biz_1", link="active", cid="1234567890")
    _business(conn, "biz_2", mode="managed")
    _campaign(conn, "biz_1", "validated")
    conn.execute(
        "INSERT INTO ads_day (business_id, day, cost_micros, clicks, impressions, phone_calls, conversions) "
        "VALUES ('biz_1', '2026-09-25', 5000000, 3, 40, 2, 1)"
    )
    move(conn)
    conn.execute("UPDATE channel_campaign SET state = 'live', validated_at = NULL")
    conn.execute("UPDATE metric_daily SET cost_micros = 7000000")
    conn.execute("UPDATE channel_account SET status = 'pending' WHERE business_id = 'biz_1'")
    move(conn)
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM channel_account) AS accounts, (SELECT count(*) FROM channel_campaign) AS rows, "
        "(SELECT count(*) FROM metric_daily) AS days"
    ).fetchone()
    assert counts == {"accounts": 2, "rows": 1, "days": 1}
    assert _campaigns(conn)["biz_1"]["state"] == "live" and _campaigns(conn)["biz_1"]["validated_at"] is None
    assert conn.execute("SELECT cost_micros FROM metric_daily").fetchone()["cost_micros"] == 7_000_000
    assert _accounts(conn)["biz_1"]["status"] == "pending"
    assert conn.execute("SELECT count(*) AS n FROM ads_campaign").fetchone()["n"] == 1  # the old tables stay


def test_who_paused_a_row_moves_with_it_a_pause_for_the_page_as_admirys(conn):
    """M0 Task 9 D7: A3p-2's `ads_run.pause` wrote who paused (`resources.paused_by`: owner, assistant or admiry)
    and, for Admiry's own pause only, why (`resources.pause_reason`: page_down or unreachable). Admiry's pause for
    the page becomes its `paused_reason` -- no owner's mark, so page-check, publish_page and set_landing_page still
    lift it -- and `pause_reason` itself is not carried; every other pause gets the owner's mark (as before), Admiry's
    with no reason or one this code does not know included (the conservative side: it stays paused until someone
    acts). A3p-2's page keys (`final_url`, `url_stale`, `refused_url`) carry over as they are. A page-down pause moved
    over drops A3p-2's own sentence for it from `last_error` (fix round 1, R2: M0's status line says the reason and its
    way out); any other row keeps its `last_error`, the same sentence on a pause that is not Admiry's included."""
    page = {
        "final_url": "https://bright.business.test/?src=gads",
        "url_stale": True,
        "refused_url": "https://x.example/",
    }
    cases = {
        "biz_owner": {"paused_by": "owner"},
        "biz_assistant": {"paused_by": "assistant"},
        "biz_a3": {},  # A3's own, before anyone wrote paused_by
        "biz_down": {"paused_by": "admiry", "pause_reason": "page_down"},
        "biz_unreachable": {"paused_by": "admiry", "pause_reason": "unreachable", "pause_owed": True},
        "biz_admiry": {"paused_by": "admiry"},  # no reason
        "biz_unknown": {"paused_by": "admiry", "pause_reason": "something_new"},
        "biz_stray": {"paused_by": "owner", "pause_reason": "page_down"},  # not Admiry's: the owner's
    }
    errors = {
        "biz_down": migrate.A3P2_PAGE_DOWN,  # what A3p-2's pause_page_down wrote
        "biz_unreachable": "Google Ads was not told yet; an Admiry operator will pause your ads there",
        "biz_stray": migrate.A3P2_PAGE_DOWN,
    }
    for biz, marks in cases.items():
        _business(conn, biz)
        _campaign(conn, biz, "paused", resources={**BUILT, **page, **marks}, last_error=errors.get(biz))
    _business(conn, "biz_live")
    _campaign(conn, "biz_live", "live", resources={**BUILT, **page})
    move(conn)
    rows = _campaigns(conn)
    got = {biz: (r["paused_reason"], r["resources"].get("owner_paused", False)) for biz, r in rows.items()}
    assert got == {
        "biz_owner": (None, True),
        "biz_assistant": (None, True),
        "biz_a3": (None, True),
        "biz_down": ("page_down", False),
        "biz_unreachable": ("unreachable", False),
        "biz_admiry": (None, True),
        "biz_unknown": (None, True),
        "biz_stray": (None, True),
        "biz_live": (None, False),
    }
    built_in = {"customer_id": "1234567890"}
    for biz, r in rows.items():
        assert "pause_reason" not in r["resources"], biz
        assert {k: r["resources"][k] for k in page} == page, biz  # the page keys carry over
    down = rows["biz_unreachable"]["resources"]
    assert down == {**BUILT, **page, **built_in, "paused_by": "admiry", "pause_owed": True}
    assert {biz: r["last_error"] for biz, r in rows.items() if r["last_error"]} == {
        "biz_unreachable": errors["biz_unreachable"],  # the owed pause's own sentence stays
        "biz_stray": migrate.A3P2_PAGE_DOWN,  # not a page pause of Admiry's: as it was
    }
    assert rows["biz_live"]["resources"] == {**BUILT, **page, **built_in}


def test_a_page_pause_moved_over_is_lifted_the_way_the_page_lifts_it(conn, gads, live):  # noqa: F811 - fixtures
    """D7's point, end to end on the moved row: page-check's own pause (A3p-2's `unreachable`) reads "paused by
    Admiry" on the moved row, the owner's resume is refused (`PAUSED_BY_ADMIRY`, M0 Task 9 D3), and page-check's own
    resume -- `resume(reason="unreachable")`, under the `live` switch -- lifts it: nothing of Admiry's is left on the
    row, which goes on towards live (`launching`: this bare business has no approved plan for the enable to read)."""
    _business(conn, "biz_u")
    _campaign(conn, "biz_u", "paused", resources={**BUILT, "paused_by": "admiry", "pause_reason": "unreachable"})
    move(conn)
    row = ads_run.get(conn, "biz_u", "google_search")
    assert ads_run.page_pause(row) == "unreachable" and not ads_run.owner_paused(row)
    assert ads_run.state_clause(row).startswith("paused by Admiry")
    with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):
        ads_run.resume_by_owner(conn, config.load(), "biz_u", by="owner")

    out = ads_run.resume(conn, config.load(), "biz_u", "google_search", reason="unreachable")

    row = ads_run.get(conn, "biz_u", "google_search")
    assert (out["state"], row["state"], row["paused_reason"], row["paused_at"]) == (
        "launching",
        "launching",
        None,
        None,
    )
    assert not {"paused_by", "pause_owed", "owner_paused"} & set(row["resources"])
