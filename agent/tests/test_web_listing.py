"""Settings: the business's Google Maps listing (plan 2026-10-03-agent-listing-confirm, Task 6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent import business, consult, geocode, listings, mcp_app, places  # noqa: F401 (mcp_app first: see test_web_ads)
from agent.tests import listings_fakes
from agent.tests.conftest import JARGON, call_tool, data, without_site_header
from agent.tests.fixtures import business_with_key
from agent.tests.test_settings_page import _section
from agent.tests.test_web import claimed_business, csrf_of

LINKED = {**listings_fakes.offered(), "source": "Google Maps", "confirmed": True}
LINKED.pop("id")


def _last_action(conn) -> dict:
    return conn.execute("SELECT tool, actor, result FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()


def _post(client, route: str, **form):
    return client.post(
        f"/connect/listing/{route}",
        data={"business_id": "biz_1", "csrf": csrf_of(client), **form},
        follow_redirects=False,
    )


def test_settings_shows_the_listing_and_unlinks_it(client, conn, google):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental", "listing": LINKED}, source="owner")
    card = _section(client.get("/connect").text, "overview")
    assert "On Google Maps:" in card and "Bright Smile Dental" in card and "query_place_id=ChIJ1" in card
    assert 'action="/connect/listing/unlink"' in card and "Not my business" in card
    r = _post(client, "unlink")
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#overview"
    assert r.headers["cache-control"] == "no-store"
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert "listing" not in k and k["listing_rejected"] == ["ChIJ1"]
    row = _last_action(conn)
    assert row["tool"] == "web.listing_unlink" and row["actor"].startswith("web:")
    assert row["result"] == {"unlinked": "ChIJ1"}
    # nothing is linked now, so the card offers a search instead
    card = _section(client.get("/connect").text, "overview")
    assert "On Google Maps:" not in card and 'action="/connect/listing/find"' in card


def test_settings_unlink_with_nothing_linked_is_refused_in_words(client, conn, google):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    r = _post(client, "unlink")
    assert r.status_code == 409 and listings.NOTHING_LINKED in r.text


def test_settings_unlink_keeps_a_centre_the_owner_placed_and_drops_one_read_from_the_listing(client, conn, google):
    claimed_business(client, conn, google)

    def session(biz_listing: dict) -> dict:
        business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental", "listing": biz_listing}, source="owner")
        conn.execute("DELETE FROM consult_session")
        return consult.create(
            conn,
            "biz_1",
            tree="dentists",
            locale="en-US",
            opening_text="x",
            known_facts={},
            options=consult.build_options(consult.load_tree("dentists"), {}),
            answers={"location": {"address": "500 University Ave", "lat": 37.4, "lng": -122.1, "source": "known_fact"}},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    own = session(LINKED)  # the centre was the listing's own: it goes, and the consultation asks again
    assert _post(client, "unlink").status_code == 303
    assert "location" not in consult.get(conn, "biz_1", own["id"])["answers"]
    placed = session({**LINKED, "placed": True})  # the owner put the centre there: it stays
    assert _post(client, "unlink").status_code == 303
    assert consult.get(conn, "biz_1", placed["id"])["answers"]["location"]["address"] == "500 University Ave"
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"]["kind"] == "address"


def test_settings_finds_the_business_and_links_the_place_picked(client, conn, google, monkeypatch):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setattr(
        places,
        "search",
        lambda q, k, **kw: [listings_fakes.found(name="Apex Plumbing", address="9 Oak St, Houston, TX")],
    )
    assert 'action="/connect/listing/find"' in _section(client.get("/connect").text, "overview")
    r = _post(client, "find", words="Apex Plumbing Houston")
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#overview"
    row = _last_action(conn)
    assert row["tool"] == "web.listing_find" and row["result"] == {"offered": ["ChIJ1"]}
    lookups = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'places.lookup'").fetchone()["n"]
    assert lookups == 1  # one counted call to Google
    card = _section(client.get("/connect").text, "overview")
    assert "Is one of these your business on Google Maps?" in card and "Apex Plumbing, 9 Oak St, Houston, TX" in card
    assert "None of these" in card and 'action="/connect/listing/find"' not in card
    assert business.latest_knowledge(conn, "biz_1")[1].get("listing") is None  # nothing linked until the pick
    r = _post(client, "choose", choice="gm_1")
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#overview"
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"]["name"] == "Apex Plumbing"
    row = _last_action(conn)
    assert row["tool"] == "web.listing_choose" and row["result"] == {"linked": "ChIJ1"}
    card = _section(client.get("/connect").text, "overview")
    assert "On Google Maps:" in card and "Apex Plumbing" in card and "Is one of these" not in card


def test_settings_find_with_nothing_close_says_so_and_leaves_no_offer(client, conn, google, monkeypatch):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    listings.save_offer(conn, "biz_1", [listings_fakes.offered()])
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setattr(
        places, "search", lambda q, k, **kw: [listings_fakes.found(name="Quick Lube", place_id="ChIJ7")]
    )
    r = _post(client, "find", words="Apex Plumbing Houston")
    assert r.status_code == 200 and listings.NOTHING_CLOSE in r.text and 'href="/connect?b=biz_1#overview"' in r.text
    assert listings.offer(conn, "biz_1") == []
    assert _last_action(conn)["result"] == {"offered": []}


def test_settings_find_with_blank_words_does_nothing(client, conn, google, monkeypatch):
    """Carried item 11: a search of nothing (spaces) is no search -- back to the card, the places waiting for the
    owner's pick still there, no call to Google and no row in the log."""
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    listings.save_offer(conn, "biz_1", [listings_fakes.offered()])
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setattr(places, "search", lambda *a, **kw: pytest.fail("no call to Google for blank words"))
    before = conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]
    r = _post(client, "find", words="   ")
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#overview"
    assert listings.offer(conn, "biz_1") == [listings_fakes.offered()]
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == before


def test_the_card_shows_the_listings_address_as_a_sentence_says_it(client, conn, google):
    """Final review B M7: the stored address keeps Google's ", USA"; the card leaves it out (places.display), as every
    other place the owner reads a place does."""
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental", "listing": LINKED}, source="owner")
    line = _section(client.get("/connect").text, "overview").split('<div class="listing-line">', 1)[1]
    line = line.split("</div>", 1)[0]
    assert "Bright Smile Dental</a>, 500 University Ave, Palo Alto, CA 94301</p>" in line and "USA" not in line


def test_settings_find_without_a_places_key_or_lookups_is_refused_in_words(client, conn, google, monkeypatch):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    r = _post(client, "find", words="Apex Plumbing Houston")
    assert r.status_code == 409 and geocode.NO_MAP + "." in r.text
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "1")
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    assert _post(client, "find", words="Apex Plumbing Houston").status_code == 200  # the day's one call
    r = _post(client, "find", words="Apex Plumbing Houston")
    assert r.status_code == 429 and geocode.NO_LOOKUPS in r.text


def test_settings_none_of_these_and_a_closed_offer(client, conn, google):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    listings.save_offer(conn, "biz_1", [listings_fakes.offered()])
    r = _post(client, "choose", choice="none")
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#overview"
    assert business.latest_knowledge(conn, "biz_1")[1]["listing_rejected"] == ["ChIJ1"]
    assert _last_action(conn)["tool"] == "web.listing_choose" and _last_action(conn)["result"]["none"] is True
    r = _post(client, "choose", choice="gm_1")
    assert r.status_code == 409 and listings.OFFER_GONE in r.text
    assert business.latest_knowledge(conn, "biz_1")[1].get("listing") is None


def test_settings_none_of_these_with_nothing_open_is_no_error(client, conn, google):
    """The offer lapsed (a day) or was closed in another tab: the owner's "None of these" has nothing to reject, and
    the card simply shows the search again."""
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    r = _post(client, "choose", choice="none")
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#overview"
    assert "listing_rejected" not in business.latest_knowledge(conn, "biz_1")[1]
    row = _last_action(conn)
    assert row["tool"] == "web.listing_choose" and row["result"]["none"] is True


def test_settings_takes_only_an_id_this_business_was_offered(client, conn, google):
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Apex"}, source="owner")
    conn.execute("INSERT INTO business (id) VALUES ('biz_2')")
    listings.save_offer(conn, "biz_2", [listings_fakes.offered()])
    r = _post(client, "choose", choice="gm_1")
    assert r.status_code == 409 and listings.OFFER_GONE in r.text
    assert listings.offer(conn, "biz_2") != []  # another business's offer is untouched


def test_settings_refuses_another_owners_business_and_a_missing_csrf(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_9', 'claimed', 'other@example.com')")
    listings.save_offer(conn, "biz_9", [listings_fakes.offered()])
    business.add_knowledge(conn, "biz_9", {"name": "Other", "listing": LINKED}, source="owner")
    for route, form in (("unlink", {}), ("find", {"words": "Other"}), ("choose", {"choice": "gm_1"})):
        other = client.post(f"/connect/listing/{route}", data={"business_id": "biz_9", "csrf": csrf_of(client), **form})
        assert other.status_code == 404, route  # owned_business never says whether the id exists
        no_csrf = client.post(f"/connect/listing/{route}", data={"business_id": "biz_1", **form})
        assert no_csrf.status_code == 403, route  # sessions.check_csrf
        bad_csrf = client.post(f"/connect/listing/{route}", data={"business_id": "biz_1", "csrf": "nope", **form})
        assert bad_csrf.status_code == 403, route
    assert "listing" in business.latest_knowledge(conn, "biz_9")[1]  # nothing of the other owner's changed
    assert len(listings.offer(conn, "biz_9")) == 1


def test_settings_refuses_a_browser_that_is_not_signed_in(client, conn):
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', 'owner@example.com')")
    for route in ("unlink", "find", "choose"):
        r = client.post(f"/connect/listing/{route}", data={"business_id": "biz_1", "csrf": "x"})
        assert r.status_code == 403, route


def test_the_card_words_are_plain_and_the_name_is_escaped(client, conn, google):
    claimed_business(client, conn, google)
    business.add_knowledge(
        conn, "biz_1", {"name": "Bright Smile", "listing": {**LINKED, "name": '<i>"Smile"</i>'}}, source="owner"
    )
    html = client.get("/connect").text
    card = _section(html, "overview")
    assert "<i>" not in card and "&lt;i&gt;" in card
    assert JARGON.findall(without_site_header(html)) == []


def test_a_confirmed_listing_without_a_place_id_shows_its_name_and_no_link(client, conn, google):
    claimed_business(client, conn, google)
    business.add_knowledge(
        conn, "biz_1", {"name": "Apex", "listing": {"name": "Apex Plumbing", "confirmed": True}}, source="owner"
    )
    r = client.get("/connect")
    card = _section(r.text, "overview")
    assert r.status_code == 200 and "On Google Maps: Apex Plumbing</p>" in card and 'href="None"' not in card


def test_the_activity_log_says_what_the_owner_did_to_the_listing(client, conn, google, monkeypatch):
    """The owner reads the log (get_audit_log, get_activity_log): an unlink, a find, a place linked and "None of these"
    on Settings each read as what they were, never as "used web.listing_unlink"."""
    claimed_business(client, conn, google)
    business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental", "listing": LINKED}, source="owner")
    key = business_with_key(conn, claim_state="claimed")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    box = {"found": [listings_fakes.found(place_id="ChIJ2", name="Apex Plumbing", address="9 Oak St, Houston, TX")]}
    monkeypatch.setattr(places, "search", lambda q, k, **kw: list(box["found"]))
    assert _post(client, "unlink").status_code == 303
    assert _post(client, "find", words="Apex Plumbing Houston").status_code == 303
    assert _post(client, "choose", choice="gm_1").status_code == 303
    box["found"] = [listings_fakes.found(place_id="ChIJ3", name="Apex Plumbing", address="40 Pine St, Houston, TX")]
    assert _post(client, "find", words="Apex Plumbing Houston").status_code == 303
    assert _post(client, "choose", choice="none").status_code == 303
    entries = data(call_tool(client, "get_audit_log", {}, key=key))["entries"]
    said = [e["sentence"] for e in entries if "Google Maps" in e["sentence"]]  # newest first
    assert said == [
        "The owner (web) said none of the places found on Google Maps is the business",
        "The owner (web) looked the business up on Google Maps",
        "The owner (web) linked the business's Google Maps listing",
        "The owner (web) looked the business up on Google Maps",
        "The owner (web) unlinked the business's Google Maps listing",
    ]
    assert not [e for e in entries if "used web." in e["sentence"]]
