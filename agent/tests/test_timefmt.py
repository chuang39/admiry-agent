"""Owner-facing times on Settings (spec 2026-09-29-admiry-settings-redesign §5.3)."""

from datetime import UTC, datetime, timedelta, timezone

from agent.timefmt import when

NOW = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


def test_never():
    assert when(None, NOW) == {"rel": "never", "abs": "", "iso": ""}


def test_relative_boundaries():
    assert when(NOW - timedelta(seconds=20), NOW)["rel"] == "just now"
    assert when(NOW - timedelta(minutes=1), NOW)["rel"] == "1 minute ago"
    assert when(NOW - timedelta(minutes=59), NOW)["rel"] == "59 minutes ago"
    assert when(NOW - timedelta(hours=1), NOW)["rel"] == "1 hour ago"
    assert when(NOW - timedelta(hours=23), NOW)["rel"] == "23 hours ago"
    assert when(NOW - timedelta(hours=30), NOW)["rel"] == "yesterday"
    assert when(datetime(2026, 9, 20, 9, 5, tzinfo=UTC), NOW)["rel"] == "Sep 20"
    assert when(datetime(2025, 9, 20, 9, 5, tzinfo=UTC), NOW)["rel"] == "Sep 20, 2025"


def test_absolute_and_iso():
    d = when(datetime(2026, 9, 28, 20, 40, 47, 707832, tzinfo=UTC), NOW)
    assert d["abs"] == "Sep 28, 2026, 20:40 UTC" and d["iso"] == "2026-09-28T20:40:47Z"


def test_future_times_read_as_just_now():  # clock skew between Postgres and the app
    assert when(NOW + timedelta(seconds=5), NOW)["rel"] == "just now"


def test_other_offsets_are_shown_in_utc():  # Postgres hands back the session's zone; the page always says UTC
    d = when(datetime(2026, 9, 28, 13, 40, tzinfo=timezone(timedelta(hours=-7))), NOW)
    assert d["abs"] == "Sep 28, 2026, 20:40 UTC" and d["iso"] == "2026-09-28T20:40:00Z"
