"""A place the owner names -- a street address, a ZIP code or a city -- put on the map with Google Places Text Search
(`places.search_text`), under the same key and the same daily budget as `start`'s lookup of the business, which asks
the same search for a few places after the reading (`listings.lookup`) and offers them to the owner to pick from.
Optional, like the lookup: with no key nothing is placed, and the callers say so in their own words.

Every call to Google is counted, and nothing else is (walk fixes 1, Task 4 round 1; final review A2): one
`places.lookup` row per call (`record_lookup`) -- start's lookup of the business (`listings.lookup`), the owner's own
search (`listings.find`) and each place put on the map alike -- which the day's Places budget (auth.places_today) and
the business's own cap (auth.PLACES_LOOKUPS_PER_DAY) read (`lookups_left`). A spent budget or cap is refused in words
(NO_LOOKUPS): it is not a place the map could not find."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import audit, auth, config, places

NOT_PLACED = "I could not place that on the map. Try a street address, or a city and ZIP code."
NO_MAP = "Admiry cannot place addresses on the map yet"  # no Places key: an operator's cause, not the owner's to know
NO_LOOKUPS = "Admiry cannot place addresses on the map right now; try again tomorrow."
# The consultation's words for it, where the location question stays open: the way on (walk fixes 1, Task 4 round 2)
NO_LOOKUPS_LATER = (
    "Admiry cannot place addresses on the map right now; say later and give the address afterwards, or try again "
    "tomorrow."
)
MAX_PLACE_CHARS = 200


class NoLookups(ToolError):
    """No lookups left today -- the day's Places budget or the business's cap is spent (NO_LOOKUPS): never a place the
    map could not find. A ToolError, so a command that lets it through is refused in its words; the consultation and
    start catch this class, and only this, to go on without the place."""


def can_place(settings: config.Settings) -> bool:
    return bool(settings.places_api_key)


def lookups_left(conn, settings: config.Settings, business_id: str | None) -> bool:
    """Whether one more call to Google fits today: the day's Places budget (`auth.places_today` against
    `settings.places_daily_budget`) and the business's own cap (`auth.PLACES_LOOKUPS_PER_DAY`). A lookup made before
    there is a business (the homepage's first turn: `business_id` None) is bounded by the day's budget alone: the rows
    of every first turn are no one business's (final review A2)."""
    if auth.places_today(conn) >= settings.places_daily_budget:
        return False
    return business_id is None or auth.places_lookups_today(conn, business_id) < auth.PLACES_LOOKUPS_PER_DAY


def record_lookup(conn, business_id: str | None, words: str, found: places.Candidate | None) -> None:
    """The row of one call to Google (`places.lookup`): the words looked up and whether they found a place, for
    `business_id` (None before there is a business). Whoever calls Google writes it right after the call, on the
    command's autocommit connection outside its transaction, so the row stays when the command is then refused."""
    audit.record_system(
        conn,
        tool="places.lookup",
        actor="places",
        business_id=business_id,
        args={"text": words},
        result={"found": found is not None},
    )


def place(conn, settings: config.Settings, text: str, *, business_id: str | None) -> places.Candidate | None:
    """The best match for `text`, or None when Google matched nothing -- or when there is nothing to look up: no key,
    blank words. Each call to Google is recorded for `business_id` (`record_lookup`); before it, the day's budget and
    the business's cap are checked (`lookups_left`), and a spent one raises NoLookups (NO_LOOKUPS). Calls Google: from
    a command's prepare only, outside its transaction, so the row stays when the command is refused.

    Both are soft ceilings: concurrent calls can pass them by a few (nothing locks the counts), bounded by the key's
    rate check. A start that places a known address no longer passes the day's budget by one: its own lookup of the
    business is a row of its own, written before this check (final review A2 closed that slack, carried item 10)."""
    words = (text or "").strip()[:MAX_PLACE_CHARS]
    if not words or not can_place(settings):
        return None
    if not lookups_left(conn, settings, business_id):
        raise NoLookups(NO_LOOKUPS)
    found = places.search_text(words, settings.places_api_key)
    record_lookup(conn, business_id, words, found)
    return found


def listing(found: places.Candidate) -> dict:
    """The knowledge's `listing` for a place the owner named: the lookup's address and point, marked `kind: address`
    so nothing reads it as a business listing (`plans.name_for` vouches for a name only by a business listing)."""
    return {**found.as_json(), "kind": "address"}
