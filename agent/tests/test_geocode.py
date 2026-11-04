"""agent/geocode.py (walk fixes 1, Task 4; round 1): a place the owner names put on the map with start's own lookup,
under its key, counted in its daily budget and capped per business."""

import pytest
from fastmcp.exceptions import ToolError

from agent import auth, config, geocode, places, tools_account

FOUND = places.Candidate("ChIJ2", "123 Main St", "123 Main St, Houston, TX 77002, USA", 29.76, -95.37)
BIZ = "biz_1"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    return config.load()


def _rows(conn, n: int, tool: str, business_id: str | None = None) -> None:
    """`n` rows of `tool` today: a `places.lookup` is a call to Google as auth counts it (start's lookup of a business,
    for none on the homepage's first turn, or an address put on the map); any other tool is none."""
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, actor_class, tool, args_hash, ok) "
        "SELECT %s, 'x', 'system', %s, 'h', true FROM generate_series(1, %s)",
        (business_id, tool, n),
    )


def _lookups(conn, business_id: str = BIZ) -> list[dict]:
    return conn.execute(
        "SELECT actor, actor_class, args_redacted, result, ok FROM agent_action "
        "WHERE tool = 'places.lookup' AND business_id = %s ORDER BY id",
        (business_id,),
    ).fetchall()


def test_without_a_key_nothing_is_placed_and_nothing_is_looked_up(conn, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setattr(places, "search_text", lambda query, key: pytest.fail("no lookup without a key"))
    settings = config.load()
    assert geocode.can_place(settings) is False
    assert geocode.place(conn, settings, "123 Main St, Houston", business_id=BIZ) is None
    assert _lookups(conn) == []


def test_a_server_with_a_key_can_place(keyed):
    assert geocode.can_place(keyed) is True


def test_a_place_is_looked_up_with_starts_key(conn, keyed, monkeypatch):
    calls = []
    monkeypatch.setattr(places, "search_text", lambda query, key: calls.append((query, key)) or FOUND)
    assert geocode.place(conn, keyed, "  123 Main St, Houston  ", business_id=BIZ) == FOUND
    assert calls == [("123 Main St, Houston", "places-test-key")]


def test_every_lookup_is_recorded_for_the_business(conn, keyed, monkeypatch):
    """Round 1: each call to Google is a row the budget and the cap count, whatever it found."""
    found = iter([FOUND, None])
    monkeypatch.setattr(places, "search_text", lambda query, key: next(found))
    assert geocode.place(conn, keyed, "123 Main St, Houston", business_id=BIZ) == FOUND
    assert geocode.place(conn, keyed, "near the mall", business_id=BIZ) is None  # no match: a lookup all the same
    assert [(r["actor"], r["actor_class"], r["args_redacted"], r["result"], r["ok"]) for r in _lookups(conn)] == [
        ("places", "system", {"text": "123 Main St, Houston"}, {"found": True}, True),
        ("places", "system", {"text": "near the mall"}, {"found": False}, True),
    ]


def test_the_days_two_hundredth_places_call_is_the_last(conn, keyed, monkeypatch):
    """AGENT_PLACES_DAILY_BUDGET (200 by default) bounds every Places call of the day, for every business: each start's
    lookup of the business and each address put on the map, a `places.lookup` row each. With 199 made the next places;
    the one after is refused in words, with no call and no row -- a spent budget is not a place the map could not
    find."""
    assert keyed.places_daily_budget == 200
    _rows(conn, 120, "places.lookup")  # the homepage's first turns: start's lookups for no business yet
    _rows(conn, 79, "places.lookup", "biz_other")
    _rows(conn, 5, "start", BIZ)  # a start's row is no call (final review A2): its lookup is a row of its own
    _rows(conn, 5, "answer", BIZ)  # not a Places call
    assert auth.places_today(conn) == 199
    monkeypatch.setattr(places, "search_text", lambda query, key: FOUND)
    assert geocode.place(conn, keyed, "123 Main St, Houston", business_id=BIZ) == FOUND
    monkeypatch.setattr(places, "search_text", lambda query, key: pytest.fail("no lookup past the budget"))
    with pytest.raises(ToolError) as spent:
        geocode.place(conn, keyed, "123 Main St, Houston", business_id=BIZ)
    assert str(spent.value) == geocode.NO_LOOKUPS and isinstance(spent.value, geocode.NoLookups)
    assert len(_lookups(conn)) == 1 and auth.places_today(conn) == 200


def test_the_two_sentences_for_no_lookups_left():
    """Round 2: the profile's command refuses with NO_LOOKUPS; the consultation, where the question stays open, says the
    way on (NO_LOOKUPS_LATER)."""
    assert geocode.NO_LOOKUPS == "Admiry cannot place addresses on the map right now; try again tomorrow."
    assert geocode.NO_LOOKUPS_LATER == (
        "Admiry cannot place addresses on the map right now; say later and give the address afterwards, or try again "
        "tomorrow."
    )


def test_a_server_with_no_map_says_so_in_the_owners_words():
    """Final review B M3: an assistant relays NO_MAP to the owner (update_business_profile's refusal, the consultation's
    answer on a server with no key): no operator's word such as "server" in it; the runbooks name the cause."""
    assert geocode.NO_MAP == "Admiry cannot place addresses on the map yet"


def test_a_business_puts_at_most_twenty_addresses_on_the_map_a_day(conn, keyed, monkeypatch):
    assert auth.PLACES_LOOKUPS_PER_DAY == 20
    _rows(conn, 19, "places.lookup", BIZ)
    monkeypatch.setattr(places, "search_text", lambda query, key: FOUND)
    assert geocode.place(conn, keyed, "123 Main St, Houston", business_id=BIZ) == FOUND  # the twentieth
    with pytest.raises(ToolError) as capped:
        geocode.place(conn, keyed, "123 Main St, Houston", business_id=BIZ)
    assert str(capped.value) == geocode.NO_LOOKUPS
    assert geocode.place(conn, keyed, "123 Main St, Houston", business_id="biz_other") == FOUND  # its own cap


def test_a_lookup_for_no_business_is_bounded_by_the_days_budget_alone(conn, keyed, monkeypatch):
    """Final review A2: the homepage's first turn looks a business up before there is one, a `places.lookup` row for no
    business. The business's cap skips such a lookup -- the rows of every first turn are no one business's -- while the
    day's budget still bounds it."""
    _rows(conn, 25, "places.lookup")  # more first turns today than one business's cap
    assert geocode.lookups_left(conn, keyed, None) and geocode.lookups_left(conn, keyed, BIZ)
    monkeypatch.setattr(places, "search_text", lambda query, key: FOUND)
    assert geocode.place(conn, keyed, "123 Main St, Houston", business_id=None) == FOUND
    _rows(conn, 20, "places.lookup", BIZ)
    assert not geocode.lookups_left(conn, keyed, BIZ) and geocode.lookups_left(conn, keyed, None)
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "46")  # 25 + 1 + 20 made: the day's budget is spent for all
    assert not geocode.lookups_left(conn, config.load(), None)


def test_no_match_is_no_place(conn, keyed, monkeypatch):
    monkeypatch.setattr(places, "search_text", lambda query, key: None)
    assert geocode.place(conn, keyed, "near the mall", business_id=BIZ) is None


def test_blank_words_are_not_looked_up(conn, keyed, monkeypatch):
    monkeypatch.setattr(places, "search_text", lambda query, key: pytest.fail("no lookup for blank words"))
    assert geocode.place(conn, keyed, "   ", business_id=BIZ) is None
    assert _lookups(conn) == []


def test_words_over_two_hundred_characters_are_cut_before_the_lookup(conn, keyed, monkeypatch):
    calls = []
    monkeypatch.setattr(places, "search_text", lambda query, key: calls.append(query) or FOUND)
    geocode.place(conn, keyed, "A" * 150 + " " + "B" * 150, business_id=BIZ)
    assert calls == [("A" * 150 + " " + "B" * 150)[: geocode.MAX_PLACE_CHARS]] and geocode.MAX_PLACE_CHARS == 200


def test_the_listing_of_a_placed_address_is_marked_as_an_address():
    assert geocode.listing(FOUND) == {
        "place_id": "ChIJ2",
        "name": "123 Main St",
        "address": "123 Main St, Houston, TX 77002, USA",
        "lat": 29.76,
        "lng": -95.37,
        "source": "Google Maps",
        "kind": "address",
    }


def test_a_lookup_reads_in_the_activity_log_as_admiry_looking_up_a_place(conn, keyed, monkeypatch):
    """The owner's activity log names no internal tool: a lookup is Admiry looking a place up on the map."""
    monkeypatch.setattr(places, "search_text", lambda query, key: FOUND)
    geocode.place(conn, keyed, "123 Main St, Houston", business_id=BIZ)
    row = conn.execute("SELECT * FROM agent_action WHERE tool = 'places.lookup'").fetchone()
    assert tools_account.sentence(row, None) == "Admiry looked up a place on the map"
