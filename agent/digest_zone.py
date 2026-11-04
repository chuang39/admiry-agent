"""The business's time zone and its local days (digest spec 2026-09-30 §7, D109; plan 2026-10-07-agent-k2 Task 2, R4,
R12). The zone chain: Google's own (`resources.time_zone` on the business's google_search row, which the managed pool's
rows carry too) -> the two-letter state at the end of the listing's address -> Los Angeles. Only the jobs write
`business.time_zone` (`ensure`); readers compute it without writing (`zone_of`). The digest and the Analyst share the
due hours and the business's daily lock, so the two jobs never build one day twice."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from . import ads_run, business

DEFAULT_ZONE = "America/Los_Angeles"
GOOGLE = "google_search"  # the channel whose account zone comes first
# The digest's (and the Analyst's) local hours: due from 08:00; from 11:00 even without the platforms' final figures;
# stale from 20:00 (the day's mail is not sent that late)
DUE_FROM_H, CUTOFF_H, STALE_AT_H = 8.0, 11.0, 20.0
US_STATE_ZONES: dict[str, str] = {
    "AL": "America/Chicago",
    "AK": "America/Anchorage",
    "AZ": "America/Phoenix",
    "AR": "America/Chicago",
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DE": "America/New_York",
    "DC": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "ID": "America/Boise",
    "IL": "America/Chicago",
    "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "KY": "America/New_York",
    "LA": "America/Chicago",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MA": "America/New_York",
    "MI": "America/Detroit",
    "MN": "America/Chicago",
    "MS": "America/Chicago",
    "MO": "America/Chicago",
    "MT": "America/Denver",
    "NE": "America/Chicago",
    "NV": "America/Los_Angeles",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NY": "America/New_York",
    "NC": "America/New_York",
    "ND": "America/Chicago",
    "OH": "America/New_York",
    "OK": "America/Chicago",
    "OR": "America/Los_Angeles",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "UT": "America/Denver",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WA": "America/Los_Angeles",
    "WV": "America/New_York",
    "WI": "America/Chicago",
    "WY": "America/Denver",
}  # the zone most of each state lives in
_STATE = re.compile(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*(?:USA|United States))?\s*$")


def state_of(address: str | None) -> str | None:
    """The US state an address ends with ("…, Palo Alto, CA", "…, CA 94301, USA"); None for any other ending."""
    found = _STATE.search(address or "")
    return found.group(1) if found and found.group(1) in US_STATE_ZONES else None


def _valid(name: object) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def resolve(conn: psycopg.Connection, business_id: str) -> tuple[str, str]:
    """(the IANA name, where it came from: google | state | default), read only."""
    row = ads_run.get(conn, business_id, GOOGLE)
    google = _valid(((row or {}).get("resources") or {}).get("time_zone"))
    if google:
        return google, "google"
    latest = business.latest_knowledge(conn, business_id)
    listing = ((latest[1] if latest else None) or {}).get("listing")
    state = state_of(listing.get("address") if isinstance(listing, dict) else None)
    if state:
        return US_STATE_ZONES[state], "state"
    return DEFAULT_ZONE, "default"


def ensure(conn: psycopg.Connection, business_id: str) -> ZoneInfo:
    """The zone, written to `business.time_zone` when it differs (the jobs' writer; readers use `zone_of`)."""
    name, _ = resolve(conn, business_id)
    conn.execute(
        "UPDATE business SET time_zone = %s WHERE id = %s AND time_zone IS DISTINCT FROM %s", (name, business_id, name)
    )
    return ZoneInfo(name)


def zone_of(conn: psycopg.Connection, business_id: str) -> ZoneInfo:
    """The stored zone when there is one, else the chain's -- never written here."""
    row = conn.execute("SELECT time_zone FROM business WHERE id = %s", (business_id,)).fetchone()
    stored = _valid((row or {}).get("time_zone"))
    return ZoneInfo(stored or resolve(conn, business_id)[0])


def reported_day(now: datetime, zone: ZoneInfo) -> date:
    """The day a morning's report covers: the local date of `now`, minus one day."""
    return now.astimezone(zone).date() - timedelta(days=1)


def bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of the local day as aware UTC datetimes (23 or 25 hours on the days the clocks change)."""
    start = datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    return start, end


def local_hour(now: datetime, zone: ZoneInfo) -> float:
    """The local wall-clock hour of `now` as a number (08:02 -> 8.033…)."""
    t = now.astimezone(zone)
    return t.hour + t.minute / 60 + t.second / 3600


@contextlib.contextmanager
def daily_lock(conn: psycopg.Connection, business_id: str) -> Iterator[bool]:
    """The business's daily lock: held while the Analyst or the digest works on one business, so the two jobs never
    build one day twice; yields False when another run holds it. Session level, released on the way out."""
    name = f"daily:{business_id}"
    held = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (name,)).fetchone()["ok"]
    try:
        yield held
    finally:
        if held:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
