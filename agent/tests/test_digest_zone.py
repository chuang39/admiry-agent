"""The business's time zone and its reported day (digest spec 2026-09-30 §7, D109; plan 2026-10-07-agent-k2 Task 2):
Google's zone first, then the listing's state, then Los Angeles; only the jobs write it; local days across daylight
saving; the business's daily lock the digest and the Analyst share."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import db, digest_zone
from agent.tests import digest_fixtures as df
from agent.tests.conftest import TEST_DB_URL

LA = ZoneInfo("America/Los_Angeles")
AUSTIN = "100 Congress Ave, Austin, TX 78701"


@pytest.mark.parametrize(
    ("address", "state"),
    [
        ("500 University Ave, Palo Alto, CA", "CA"),
        ("500 University Ave, Palo Alto, CA 94301, USA", "CA"),
        (AUSTIN, "TX"),
        ("1 Main St, Springfield, IL 62701-1234, United States", "IL"),
        ("Somewhere without a state", None),
        ("10 Rue X, Paris, FR", None),
        (None, None),
    ],
)
def test_state_of(address, state):
    assert digest_zone.state_of(address) == state


def test_resolve_prefers_google_then_state_then_default(conn):
    df.seed_business(conn, "biz_1")
    assert digest_zone.resolve(conn, "biz_1") == ("America/Los_Angeles", "default")
    df.seed_business(conn, "biz_2", address=AUSTIN)
    assert digest_zone.resolve(conn, "biz_2") == ("America/Chicago", "state")
    df.seed_channel(conn, "biz_2", resources={"time_zone": "America/New_York"})
    assert digest_zone.resolve(conn, "biz_2") == ("America/New_York", "google")
    conn.execute('UPDATE channel_campaign SET resources = \'{"time_zone": "Mars/Base"}\'')
    assert digest_zone.resolve(conn, "biz_2") == ("America/Chicago", "state")


def test_ensure_writes_only_on_change(conn):
    df.seed_business(conn, "biz_1", address=AUSTIN)
    assert digest_zone.ensure(conn, "biz_1") == ZoneInfo("America/Chicago")
    first = conn.execute("SELECT time_zone, xmin::text AS x FROM business").fetchone()
    assert first["time_zone"] == "America/Chicago"
    assert digest_zone.ensure(conn, "biz_1") == ZoneInfo("America/Chicago")
    assert conn.execute("SELECT xmin::text AS x FROM business").fetchone()["x"] == first["x"]  # no second write


def test_zone_of_reads_without_writing(conn):
    df.seed_business(conn, "biz_1", address=AUSTIN)
    assert digest_zone.zone_of(conn, "biz_1") == ZoneInfo("America/Chicago")
    assert conn.execute("SELECT time_zone FROM business").fetchone()["time_zone"] is None
    conn.execute("UPDATE business SET time_zone = 'America/Denver'")
    assert digest_zone.zone_of(conn, "biz_1") == ZoneInfo("America/Denver")


@pytest.mark.parametrize(
    ("now", "day", "hours"),
    [
        (datetime(2026, 11, 1, 16, 2, tzinfo=UTC), date(2026, 10, 31), 24),
        (datetime(2026, 11, 2, 16, 2, tzinfo=UTC), date(2026, 11, 1), 25),  # the clocks went back
        (datetime(2026, 3, 9, 15, 2, tzinfo=UTC), date(2026, 3, 8), 23),  # the clocks went forward
    ],
)
def test_reported_day_and_bounds_across_dst(now, day, hours):
    assert digest_zone.reported_day(now, LA) == day
    start, end = digest_zone.bounds(day, LA)
    assert start.tzinfo == UTC and end - start == timedelta(hours=hours)
    assert start.astimezone(LA).hour == 0 and end.astimezone(LA).date() == day + timedelta(days=1)


def test_local_hour():
    assert digest_zone.local_hour(datetime(2026, 10, 6, 15, 2, tzinfo=UTC), LA) == pytest.approx(8 + 2 / 60)
    assert digest_zone.local_hour(datetime(2026, 10, 6, 7, 0, tzinfo=UTC), LA) == 0


def test_daily_lock_is_held_by_one_connection_at_a_time(conn):
    with (
        digest_zone.daily_lock(conn, "biz_1") as first,
        db.connect(TEST_DB_URL) as other,
        digest_zone.daily_lock(other, "biz_1") as second,
    ):
        assert first and not second
    with db.connect(TEST_DB_URL) as other, digest_zone.daily_lock(other, "biz_1") as again:
        assert again
