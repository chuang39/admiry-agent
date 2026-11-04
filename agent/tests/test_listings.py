"""The closeness rule, the offer, linking and unlinking (plan 2026-10-03-agent-listing-confirm, Task 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent import auth, business, config, consult, geocode, listings, places
from agent.tests.fixtures import READ_SITE
from agent.tests.listings_fakes import found

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CHOSEN = {
    "id": "gm_1",
    "place_id": "ChIJ1",
    "name": "Bright Smile Dental",
    "address": "500 University Ave, Palo Alto, CA 94301, USA",
    "lat": 37.4419,
    "lng": -122.143,
}


@pytest.mark.parametrize(
    "a, b, towns, match",
    [
        ("Bright Smile Dental", "Bright Smile Dental Care", [], True),
        ("Bright Smile Dental, LLC", "The Bright Smile Dental", [], True),
        ("Smith Dental", "Smith Dental", [], True),
        ("Smith Dental", "Smith Dental Care", [], False),
        ("Dental Practice", "Palo Alto Dental Practice", [], False),
        ("Immigration Lawyer", "Immigration Law Office", [], False),
        ("Apex", "Apex", [], False),
        (None, "Apex Plumbing", [], False),
        ("Palo Alto Dental Practice", "Palo Alto Family Dentistry", ["Palo Alto"], False),
        ("Palo Alto Dental", "Palo Alto Dental", ["Palo Alto"], True),
    ],
)
def test_names_match_only_on_what_tells_one_business_from_another(a, b, towns, match):
    assert listings.names_match(a, b, towns=towns) is match


def test_same_site_and_same_phone():
    assert listings.same_site("https://www.brightsmile.example/", "brightsmile.example")
    assert not listings.same_site("https://brightsmile.example", "https://brightsmile.example.net")
    assert not listings.same_site(None, None)
    assert listings.same_phone("(650) 555-0100", "+1 650-555-0100")
    assert not listings.same_phone("555-0100", "555-0100")  # fewer than 10 digits never match
    assert listings.same_site("www.example.com/a//b", "example.com")  # a schemeless site with "//" in its path


@pytest.mark.parametrize("text", ["[none]", "[not available]", "see [site]", "http://[x", "", "   ", 7])
def test_a_website_the_model_wrote_never_breaks_the_rule(text):
    """The knowledge's website is free text (a reading's, an assistant's): whatever it says, the comparison answers."""
    assert listings.same_site(text, text) is False
    assert listings.same_site(text, "brightsmile.example") is False
    assert listings.same_site("brightsmile.example", text) is False


@pytest.mark.parametrize(
    "a, b, same",
    [
        # the same page, whatever its case, scheme, `www.` and trailing slash
        ("https://www.facebook.com/BrightSmile/", "facebook.com/brightsmile", True),
        ("http://FACEBOOK.com/brightsmile", "https://www.facebook.com/BrightSmile/", True),
        # another page of the host, below the same first segment too
        ("https://www.facebook.com/BrightSmile", "https://www.facebook.com/otherbiz", False),
        ("https://www.facebook.com/BrightSmile", "https://www.facebook.com/BrightSmile/about", False),
        ("https://www.yelp.com/biz/bright-smile-palo-alto", "yelp.com/biz/other-dental", False),
        ("https://www.yelp.com/biz/bright-smile-palo-alto", "yelp.com/biz/bright-smile-palo-alto/", True),
        ("https://sites.google.com/view/a", "sites.google.com/view/b", False),
        # the query names the page of the host that has one
        ("https://www.facebook.com/profile.php?id=1", "facebook.com/profile.php?id=2", False),
        ("https://www.facebook.com/profile.php?id=1", "facebook.com/profile.php?id=1", True),
        ("https://www.facebook.com/profile.php?id=1", "facebook.com/profile.php", False),
        # a shared host with an empty path is no page: it matches nothing
        ("https://www.facebook.com", "https://www.facebook.com", False),
        ("https://www.facebook.com/", "facebook.com", False),
        ("https://www.facebook.com/?ref=a", "facebook.com/?ref=a", False),
        ("https://www.facebook.com/BrightSmile", "https://www.instagram.com/BrightSmile", False),
        ("https://linktr.ee/brightsmile", "linktr.ee/brightsmile", True),
        ("https://linktr.ee/brightsmile", "linktr.ee/other", False),
        ("https://brightsmile.example/a", "https://brightsmile.example/b", True),  # a business's own host: any page
        ("https://brightsmile.example", "brightsmile.example/contact", True),
    ],
)
def test_a_shared_host_is_one_site_only_with_the_same_whole_page(a, b, same):
    assert listings.same_site(a, b) is same
    assert listings.same_site(b, a) is same


def test_every_shared_host_needs_its_page():
    for host in listings.SHARED_HOSTS:
        assert not listings.same_site(f"https://{host}", f"https://{host}/")
        assert listings.same_site(f"https://{host}/Page/", f"{host}/page")
        assert not listings.same_site(f"https://{host}/Page", f"{host}/other")


def test_a_place_is_in_the_towns_the_owner_stated_or_anywhere_when_none():
    towns = listings.towns_of({"service_area": ["Palo Alto"]}, "12 Elm St, Houston, TX 77002")
    assert towns == ["Palo Alto", "Houston", "77002"]
    assert listings.in_towns("500 University Ave, Palo Alto, CA 94301, USA", towns)
    assert not listings.in_towns("1 Main St, Austin, TX 78701, USA", towns)
    assert listings.in_towns("1 Main St, Austin, TX 78701, USA", [])


def test_only_a_zip_code_after_a_state_names_a_town_not_a_street_number():
    towns = listings.towns_of({}, "12345 Main St, Houston, TX 77002")
    assert towns == ["Houston", "77002"]
    assert not listings.in_towns("12345 Elm St, Austin, TX 78701, USA", towns)
    assert listings.towns_of({}, "Houston, TX 77002-1234, USA") == ["Houston", "77002"]


def test_close_by_site_or_phone_wherever_it_is_and_by_name_only_in_the_owners_towns():
    rule = {"name": "Bright Smile Dental", "website": None, "phone": None, "towns": ["Palo Alto"]}
    assert listings.close(found(), **rule)
    assert not listings.close(found(address="1 Main St, Austin, TX 78701, USA"), **rule)
    far = found(name="BSD Inc", address="1 Main St, Austin, TX", website="https://brightsmile.example")
    assert listings.close(far, **{**rule, "website": "brightsmile.example"})
    assert listings.close(found(name="BSD Inc", phone="650.555.0100"), **{**rule, "phone": "(650) 555-0100"})
    assert not listings.close(found(name="Palo Alto Dental Care"), **rule)


def test_numbered_keeps_three_places_in_googles_order_without_repeats():
    out = listings.numbered([found(place_id=f"ChIJ{n}") for n in (1, 2, 2, 3, 4)])
    assert [(c["id"], c["place_id"]) for c in out] == [("gm_1", "ChIJ1"), ("gm_2", "ChIJ2"), ("gm_3", "ChIJ3")]
    assert set(out[0]) == {"id", "place_id", "name", "address", "lat", "lng"}
    assert listings.label(out[0]) == "Bright Smile Dental, 500 University Ave, Palo Alto, CA 94301"


def test_words_cover_a_place_whose_distinctive_words_the_owner_typed():
    assert listings.words_cover("Apex Plumbing", "Apex Plumbing, Houston TX")
    assert not listings.words_cover("Houston Drain Pros", "Apex Plumbing, Houston TX")
    assert not listings.words_cover("Plumbing Services", "plumbing services houston")  # no distinctive word


def test_words_cover_reads_no_place_by_the_owners_town_alone():
    """Final review A m1: as names_match does, the words of the business's own towns tell no place from another --
    typing "Palo Alto Dental" in Palo Alto names neither "Palo Alto Family Dentistry" nor "Smith Dental Palo Alto";
    a place whose name is the owner's words exactly still is."""
    towns = ["Palo Alto"]
    assert not listings.words_cover("Palo Alto Family Dentistry", "Palo Alto Dental", towns=towns)
    assert not listings.words_cover("Palo Alto Dental", "Smith Dental Palo Alto", towns=towns)
    assert listings.words_cover("Smith Dental Palo Alto", "Smith Dental, Palo Alto", towns=towns)
    assert listings.words_cover("Palo Alto Dental", "Palo Alto Dental", towns=towns)
    assert listings.words_cover("Palo Alto Family Dentistry", "Palo Alto Dental")  # no towns: as before


def test_words_cover_a_business_named_for_its_town_when_the_owner_types_its_name_and_address():
    """Final review F6 residual: the Settings form asks for "its name and address", so a business named for its own
    town is named when its whole name stands in the owner's words as a phrase (consecutive whole words, legal words
    aside) -- a name of two words or more, with a word that says which business it is (the town's included). Typing
    just the town, a name scattered through the words, or a one-word name names nothing."""
    palo = ["Palo Alto"]
    assert listings.words_cover("Palo Alto Dental", "Palo Alto Dental, Palo Alto", towns=palo)
    assert listings.words_cover(
        "Palo Alto Dental, LLC", "palo alto dental, 500 University Ave, Palo Alto CA", towns=palo
    )
    assert listings.words_cover("Houston Plumbing", "Houston Plumbing, 12 Main St, Houston TX", towns=["Houston"])
    assert not listings.words_cover("Palo Alto Family Dentistry", "Palo Alto dentist, Palo Alto", towns=palo)
    assert not listings.words_cover("Palo Alto Family Dentistry", "Palo Alto Dental, Palo Alto", towns=palo)
    assert not listings.words_cover("Palo Alto Dental", "Smith Dental, Palo Alto", towns=palo)
    assert not listings.words_cover("Palo Alto Dental", "Palo Dental Alto, Palo Alto", towns=palo)  # not a phrase
    assert not listings.words_cover("Houston", "Houston Plumbing, 12 Main St, Houston TX", towns=["Houston"])
    assert not listings.words_cover("Plumbing Services", "Plumbing Services, 12 Main St, Houston TX")  # says nothing


def test_link_confirms_the_place_and_takes_its_name_and_point():
    k = listings.link({"name": "Dentist", "service_area": ["Palo Alto"]}, CHOSEN, now=NOW)
    assert k["listing"] == {
        "place_id": "ChIJ1",
        "name": "Bright Smile Dental",
        "address": "500 University Ave, Palo Alto, CA 94301, USA",
        "lat": 37.4419,
        "lng": -122.143,
        "source": "Google Maps",
        "confirmed": True,
        "confirmed_at": NOW.isoformat(),
    }
    assert k["name"] == "Bright Smile Dental"  # "Dentist" names no business
    assert listings.link({"name": "Bright Smile Dental LLC"}, CHOSEN, now=NOW)["name"] == "Bright Smile Dental LLC"


def test_link_keeps_the_centre_the_owner_placed_and_forgets_the_place_as_rejected():
    placed = {"kind": "address", "address": "12 Elm St, Houston, TX 77002", "lat": 29.76, "lng": -95.37}
    k = listings.link({"listing": placed, "listing_rejected": ["ChIJ1", "ChIJ9"]}, CHOSEN, now=NOW)
    assert (k["listing"]["address"], k["listing"]["lat"], k["listing"]["placed"]) == (placed["address"], 29.76, True)
    assert (k["listing"]["place_id"], k["listing"]["confirmed"]) == ("ChIJ1", True)
    assert k["listing_rejected"] == ["ChIJ9"]


def test_an_address_the_owner_placed_is_never_remembered_as_not_this_business():
    """An address listing carries the place id of the street's (or a shop's) place on the map (geocode.listing): neither
    linking over it nor unlinking a listing moved to it spends one of the remembered places on it."""
    placed = {"kind": "address", "address": "12 Elm St, Houston, TX", "lat": 29.76, "lng": -95.37, "place_id": "ChIJ7"}
    assert listings.link({"listing": placed}, CHOSEN, now=NOW)["listing_rejected"] == []
    assert listings.unlink({"listing": placed})["listing_rejected"] == []
    assert listings.unlink({"listing": placed})["listing"]["kind"] == "address"  # the centre stays
    assert listings.unlink({"listing": "not a listing"})["listing_rejected"] == []


def test_linking_another_place_remembers_the_old_one_as_not_this_business():
    old = {**CHOSEN, "place_id": "ChIJ0", "source": "Google Maps", "confirmed": True}
    old.pop("id")
    k = listings.link({"listing": old}, CHOSEN, now=NOW)
    assert k["listing"]["place_id"] == "ChIJ1" and k["listing_rejected"] == ["ChIJ0"]


def test_unlink_removes_the_listing_and_remembers_it_and_keeps_a_placed_centre():
    linked = listings.link({}, CHOSEN, now=NOW)
    out = listings.unlink(linked)
    assert "listing" not in out and out["listing_rejected"] == ["ChIJ1"]
    placed = listings.link(
        {"listing": {"kind": "address", "address": "12 Elm St, Houston, TX", "lat": 29.76, "lng": -95.37}},
        CHOSEN,
        now=NOW,
    )
    kept = listings.unlink(placed)
    assert kept["listing"] == {
        "address": "12 Elm St, Houston, TX",
        "lat": 29.76,
        "lng": -95.37,
        "source": "Google Maps",
        "kind": "address",
    }


def test_the_offer_is_kept_a_day_replaced_moved_and_dropped(conn):
    for biz in ("biz_1", "biz_2"):
        conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
    listings.save_offer(conn, "biz_1", [CHOSEN])
    assert listings.offer(conn, "biz_1") == [CHOSEN]
    conn.execute("UPDATE listing_offer SET created_at = now() - interval '25 hours'")
    assert listings.offer(conn, "biz_1") == []
    listings.save_offer(conn, "biz_1", [CHOSEN])
    listings.move_offer(conn, "biz_1", "biz_2")
    assert listings.offer(conn, "biz_1") == [] and listings.offer(conn, "biz_2") == [CHOSEN]
    listings.save_offer(conn, "biz_2", [])
    assert listings.offer(conn, "biz_2") == []


def test_moving_an_offer_to_the_business_it_is_already_on_keeps_it(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    listings.save_offer(conn, "biz_1", [CHOSEN])
    listings.move_offer(conn, "biz_1", "biz_1")
    assert listings.offer(conn, "biz_1") == [CHOSEN]


def _versions(conn, biz):
    """The business's knowledge versions, oldest first, as (version, source)."""
    rows = conn.execute("SELECT version, source FROM knowledge WHERE business_id = %s ORDER BY version", (biz,))
    return [(r["version"], r["source"]) for r in rows.fetchall()]


def test_confirm_links_only_an_offered_id_of_this_business(conn):
    for biz in ("biz_1", "biz_2"):
        conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
        business.add_knowledge(conn, biz, {"name": "Dentist"}, source="extract")
    listings.save_offer(conn, "biz_1", [CHOSEN])
    assert listings.confirm(conn, "biz_2", "gm_1") is None  # another business's offer
    assert listings.confirm(conn, "biz_1", "gm_7") is None  # made up
    linked = listings.confirm(conn, "biz_1", "gm_1")
    assert linked["place_id"] == "ChIJ1" and linked["confirmed"] is True
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"]["place_id"] == "ChIJ1"
    assert listings.offer(conn, "biz_1") == []  # the offer is closed
    assert _versions(conn, "biz_1") == [(1, "extract"), (2, "owner")]  # the owner's word, one version
    assert _versions(conn, "biz_2") == [(1, "extract")]  # the other business's knowledge is untouched


def test_confirm_with_a_label_links_only_the_place_that_label_named(conn):
    """Final review A1: a consultation's options were frozen when it began, and a later find renumbers the offer from
    gm_1: the pick links only when the place offered under that id now is the one the owner saw (its label), and
    otherwise links nothing and leaves the newer offer open."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    business.add_knowledge(conn, "biz_1", {"name": "Dentist"}, source="extract")
    other = {**CHOSEN, "place_id": "ChIJ9", "name": "Other Dental", "address": "1 Elsewhere Rd, Fresno, CA"}
    listings.save_offer(conn, "biz_1", [other])
    assert listings.confirm(conn, "biz_1", "gm_1", label=listings.label(CHOSEN)) is None
    assert listings.offer(conn, "biz_1") == [other] and _versions(conn, "biz_1") == [(1, "extract")]
    linked = listings.confirm(conn, "biz_1", "gm_1", label="Other Dental, 1 Elsewhere Rd, Fresno, CA")
    assert linked["place_id"] == "ChIJ9"


def test_reject_offer_remembers_every_offered_place(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    business.add_knowledge(conn, "biz_1", {"name": "Dentist"}, source="extract")
    listings.save_offer(conn, "biz_1", [CHOSEN, {**CHOSEN, "id": "gm_2", "place_id": "ChIJ2"}])
    assert listings.reject_offer(conn, "biz_1") == 2
    assert business.latest_knowledge(conn, "biz_1")[1]["listing_rejected"] == ["ChIJ1", "ChIJ2"]
    assert _versions(conn, "biz_1") == [(1, "extract"), (2, "owner")]
    assert listings.reject_offer(conn, "biz_1") == 0  # nothing open: nothing written
    assert _versions(conn, "biz_1") == [(1, "extract"), (2, "owner")]


def test_unlink_listing_answers_none_when_nothing_is_linked(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    business.add_knowledge(conn, "biz_1", {"name": "Dentist"}, source="extract")
    assert listings.unlink_listing(conn, "biz_1") is None
    assert _versions(conn, "biz_1") == [(1, "extract")]  # nothing linked: nothing written
    business.add_knowledge(conn, "biz_1", listings.link({"name": "Dentist"}, CHOSEN, now=NOW), source="owner")
    assert listings.unlink_listing(conn, "biz_1")["place_id"] == "ChIJ1"
    assert "listing" not in business.latest_knowledge(conn, "biz_1")[1]
    assert _versions(conn, "biz_1") == [(1, "extract"), (2, "owner"), (3, "owner")]


def _open_consultation(conn, biz: str, centre: dict) -> dict:
    """An open dental consultation whose question of where the ads centre is pre-answered with `centre`, as a listing's
    point pre-answers it (consult.preanswers), and its website read at start (READ_SITE)."""
    return consult.create(
        conn,
        biz,
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=consult.build_options(consult.load_tree("dentists"), {}),
        answers={**READ_SITE, "location": {**centre, "source": "known_fact"}},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_unlink_and_say_says_nothing_is_linked_and_writes_nothing(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    business.add_knowledge(conn, "biz_1", {"name": "Dentist"}, source="extract")
    assert listings.unlink_and_say(conn, "biz_1") == (None, listings.NOTHING_LINKED)
    assert _versions(conn, "biz_1") == [(1, "extract")]


def test_unlink_and_say_drops_the_listings_own_centre_and_says_the_running_ads_wait(conn):
    """The owner's "not my business", however it was said (plan 2026-10-03-agent-listing-not-mine-words): the listing
    removed and remembered, the consultation's centre that was the listing's own dropped, and the sentences in order."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    business.add_knowledge(conn, "biz_1", listings.link({"name": "Dentist"}, CHOSEN, now=NOW), source="owner")
    s = _open_consultation(conn, "biz_1", {"address": CHOSEN["address"], "lat": CHOSEN["lat"], "lng": CHOSEN["lng"]})
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES ('pl_1', 'biz_1', 1, '{}', 'approved')"
    )
    unlinked, say = listings.unlink_and_say(conn, "biz_1")
    assert unlinked["place_id"] == "ChIJ1"
    assert say == listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE + listings.RUNNING
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert "listing" not in k and k["listing_rejected"] == ["ChIJ1"]
    assert "location" not in consult.get(conn, "biz_1", s["id"])["answers"]  # asked again
    assert listings.unlink_and_say(conn, "biz_1") == (None, listings.NOTHING_LINKED)  # the same word twice


def test_unlink_and_say_for_a_caller_that_asks_next_asks_for_an_address_only_when_the_centre_is_next(conn):
    """Final review M2: NO_CENTRE reads as a request, so a caller that goes on to ask the consultation's next question
    (`answer`) gets it only when that question is where the ads centre; with another first, the sentence says the
    question will come (NO_CENTRE_ASKED). The default caller (update_business_profile) keeps NO_CENTRE: its assistant
    can take an address as it is."""
    centre = {"address": CHOSEN["address"], "lat": CHOSEN["lat"], "lng": CHOSEN["lng"]}
    for biz in ("biz_1", "biz_2", "biz_3"):
        conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
        business.add_knowledge(conn, biz, listings.link({"name": "Dentist"}, CHOSEN, now=NOW), source="owner")
    _open_consultation(conn, "biz_1", centre)  # the objective, the services and the value wait before the centre
    _, say = listings.unlink_and_say(conn, "biz_1", asks_next=True)
    assert say == listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE_ASKED
    assert listings.NO_CENTRE_ASKED == " Admiry will ask where the ads should centre."
    _open_consultation(conn, "biz_2", centre)  # ... and the same, for a caller that asks nothing next
    _, say = listings.unlink_and_say(conn, "biz_2")
    assert say == listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE
    answered = {q: {"choice": "x", "source": "choice"} for q in ("objective", "service", "customer_value")}
    consult.create(
        conn,
        "biz_3",
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=consult.build_options(consult.load_tree("dentists"), {}),
        answers={
            **READ_SITE,
            **answered,
            "location": {**centre, "source": "known_fact"},
            "radius": {"source": "known_fact"},
        },
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _, say = listings.unlink_and_say(conn, "biz_3", asks_next=True)  # the centre is the only question left: asked next
    assert say == listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE


def test_unlink_and_say_with_no_consultation_to_ask_says_where_a_new_version_centres(conn):
    """Task 2 round 1 (the controller's ruling): the listing's own centre goes, and no consultation will ask where the
    ads centre next (none open, or a plan already built) -- so no address is asked for that nothing would take. With
    areas the business serves on file, a new version centres on them; with none, it needs the right place linked."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    served = {"name": "Dentist", "service_area": ["Palo Alto", " "]}
    business.add_knowledge(conn, "biz_1", listings.link(served, CHOSEN, now=NOW), source="owner")
    _unlinked, say = listings.unlink_and_say(conn, "biz_1")
    assert say == listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE_LATER
    assert listings.NO_CENTRE_LATER == (
        " Until the right place is linked, a new version of the plan centres on the areas the business serves."
    )
    conn.execute("INSERT INTO business (id) VALUES ('biz_2')")
    unserved = {"name": "Dentist", "service_area": [" "]}  # a blank entry is no area (profile._listed)
    business.add_knowledge(conn, "biz_2", listings.link(unserved, CHOSEN, now=NOW), source="owner")
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES ('pl_2', 'biz_2', 1, '{}', 'approved')"
    )
    _unlinked, say = listings.unlink_and_say(conn, "biz_2")
    assert say == listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_PLACE + listings.RUNNING
    assert listings.NO_PLACE == " A new version of the plan needs a place to centre on: link the right one first."


def test_unlink_and_say_keeps_a_centre_the_owner_placed(conn):
    """A placed centre has the shape of the listing's own: consult.forget_centre is not called for it."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    placed = {"kind": "address", "address": "12 Elm St, Houston, TX", "lat": 29.76, "lng": -95.37}
    business.add_knowledge(conn, "biz_1", listings.link({"listing": placed}, CHOSEN, now=NOW), source="owner")
    s = _open_consultation(conn, "biz_1", {"address": "12 Elm St, Houston, TX", "lat": 29.76, "lng": -95.37})
    unlinked, say = listings.unlink_and_say(conn, "biz_1")
    assert unlinked["placed"] is True
    assert say == listings.UNLINKED.format(name="Bright Smile Dental")  # no NO_CENTRE, no plan approved: no RUNNING
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"]["kind"] == "address"
    assert consult.get(conn, "biz_1", s["id"])["answers"]["location"]["address"] == "12 Elm St, Houston, TX"


def test_link_again_says_how_to_link_the_right_place():
    assert listings.LINK_AGAIN == (
        " To link the right one, use Find it in Settings, or give its name and address to your assistant."
    )


def test_has_approved_plan_is_true_only_for_an_approved_plan(conn):
    for biz in ("biz_1", "biz_2"):
        conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
    assert not listings.has_approved_plan(conn, "biz_1")  # no plan
    conn.execute("INSERT INTO plan (id, business_id, version, json, status) VALUES ('pl_0', 'biz_1', 1, '{}', 'draft')")
    assert not listings.has_approved_plan(conn, "biz_1")  # a draft
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES ('pl_1', 'biz_2', 1, '{}', 'approved')"
    )
    assert not listings.has_approved_plan(conn, "biz_1")  # another business's approved plan
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES ('pl_2', 'biz_1', 2, '{}', 'approved')"
    )
    assert listings.has_approved_plan(conn, "biz_1")


def test_lookup_needs_a_name_and_offers_only_close_places(conn, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    queries = []

    def search(query, key, **kw):
        queries.append((query, kw.get("max_results"), kw.get("with_identity")))
        return [found(), found(name="Palo Alto Dental Care", place_id="ChIJ2"), found(place_id="ChIJ3")]

    monkeypatch.setattr(places, "search", search)
    settings = config.load()
    assert listings.lookup(conn, settings, {"service_area": ["Palo Alto"]}, {}, business_id=None, rejected=[]) == []
    assert queries == []  # no name, no call to Google
    out = listings.lookup(
        conn,
        settings,
        {"name": "Bright Smile Dental", "service_area": ["Palo Alto"]},
        {},
        business_id=None,
        rejected=["ChIJ3"],
    )
    assert [c["place_id"] for c in out] == ["ChIJ1"]
    assert queries == [("Bright Smile Dental Palo Alto", listings.SEARCHED, True)]  # asks for the website and phone
    assert auth.places_today(conn) == 1  # one call to Google, one row


def test_lookup_goes_on_when_the_stored_website_is_not_a_website(conn, monkeypatch):
    """A website the model wrote ("[none]") is no reason to fail after Google was called: the name still offers it."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [found(website="https://brightsmile.example")])
    for website in ("[none]", "http://[x", "not available"):
        knowledge = {"name": "Bright Smile Dental", "service_area": ["Palo Alto"], "website": website}
        out = listings.lookup(conn, config.load(), knowledge, {}, business_id=None, rejected=[])
        assert [c["place_id"] for c in out] == ["ChIJ1"]


def test_lookup_uses_the_known_name_and_address_and_skips_a_spent_day(conn, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "1")
    queries = []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: queries.append(q) or [])
    facts = {"business_name": "Apex Plumbing", "address": "12 Elm St, Houston, TX 77002"}
    listings.lookup(conn, config.load(), {"name": "Plumber"}, facts, business_id=None, rejected=[])
    assert queries == ["Apex Plumbing 12 Elm St, Houston, TX 77002"]
    assert listings.lookup(conn, config.load(), {"name": "Plumber"}, facts, business_id=None, rejected=[]) == []
    assert len(queries) == 1  # the day's one call is spent: no second call, no error


def test_find_counts_the_call_and_offers_what_the_owner_typed(conn, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    asked = []

    def search(query, key, **kw):
        asked.append((query, kw.get("max_results"), kw.get("with_identity")))
        return [found(name="Apex Plumbing"), found(name="Houston Drain Pros", place_id="ChIJ2")]

    monkeypatch.setattr(places, "search", search)
    out = listings.find(conn, config.load(), "Apex Plumbing, Houston", business_id="biz_1", knowledge={})
    assert [c["name"] for c in out] == ["Apex Plumbing"]
    assert asked == [("Apex Plumbing, Houston", listings.SEARCHED, True)]  # asks for the website and phone
    assert auth.places_lookups_today(conn, "biz_1") == 1
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "1")
    with pytest.raises(geocode.NoLookups):
        listings.find(conn, config.load(), "Apex Plumbing", business_id="biz_1", knowledge={})


def test_find_keeps_the_businesss_own_website_or_phone(conn, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    monkeypatch.setattr(
        places,
        "search",
        lambda q, k, **kw: [
            found(name="Apex Plumbing Houston", place_id="ChIJ2"),
            found(name="APX Inc", place_id="ChIJ3", website="https://www.apexplumbing.example/contact"),
            found(name="Rooter Co", place_id="ChIJ4", phone="+1 713-555-0100"),
            found(name="Houston Drain Pros", place_id="ChIJ5"),
        ],
    )
    knowledge = {"website": "apexplumbing.example", "phone": "(713) 555-0100"}
    out = listings.find(conn, config.load(), "Apex Plumbing Houston", business_id="biz_1", knowledge=knowledge)
    # ChIJ3 and ChIJ4 are the business's by website and phone though the words name neither
    assert [c["place_id"] for c in out] == ["ChIJ2", "ChIJ3", "ChIJ4"]
    assert [c["id"] for c in out] == ["gm_1", "gm_2", "gm_3"]


def test_the_owners_own_search_finds_a_place_they_once_rejected_and_linking_it_forgets_that(conn, monkeypatch):
    """Final review A2/B I2: "None of these" keeps a place out of start's lookup, the search Admiry makes; the owner's
    own search (find) is a newer word than that, so it offers the place again -- and linking it un-rejects it."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    knowledge = {"name": "Apex Plumbing", "listing_rejected": ["ChIJ1", "ChIJ8"]}
    business.add_knowledge(conn, "biz_1", knowledge, source="owner")
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [found(name="Apex Plumbing", place_id="ChIJ1")])
    out = listings.find(conn, config.load(), "Apex Plumbing, Houston", business_id="biz_1", knowledge=knowledge)
    assert [c["place_id"] for c in out] == ["ChIJ1"]
    listings.save_offer(conn, "biz_1", out)
    assert listings.confirm(conn, "biz_1", "gm_1")["place_id"] == "ChIJ1"
    assert business.latest_knowledge(conn, "biz_1")[1]["listing_rejected"] == ["ChIJ8"]
    # start's lookup still leaves out what the owner said is not theirs
    rule = {"name": "Apex Plumbing"}
    assert listings.lookup(conn, config.load(), rule, {}, business_id="biz_1", rejected=["ChIJ1"]) == []


def test_find_offers_no_place_by_the_owners_town_alone(conn, monkeypatch):
    """Final review A m1: typing "Palo Alto Dental" for a business in Palo Alto does not offer every dentist named for
    the town."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    monkeypatch.setattr(
        places,
        "search",
        lambda q, k, **kw: [
            found(name="Palo Alto Family Dentistry", place_id="ChIJ2"),
            found(name="Palo Alto Dental", place_id="ChIJ3"),
        ],
    )
    knowledge = {"name": "Palo Alto Dental", "service_area": ["Palo Alto"]}
    out = listings.find(conn, config.load(), "Palo Alto Dental", business_id="biz_1", knowledge=knowledge)
    assert [c["place_id"] for c in out] == ["ChIJ3"]
    out = listings.find(conn, config.load(), "Palo Alto dentist", business_id="biz_1", knowledge=knowledge)
    assert out == []


def test_find_offers_a_business_named_for_its_town_when_the_owner_types_its_name_and_address(conn, monkeypatch):
    """Final review F6 residual: "Its name and address" (the Settings form, after-launch.md) must find "Palo Alto
    Dental" in Palo Alto and "Houston Plumbing" in Houston -- and still not the other places named for the town."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    monkeypatch.setattr(
        places,
        "search",
        lambda q, k, **kw: [
            found(name="Palo Alto Family Dentistry", place_id="ChIJ2"),
            found(name="Palo Alto Dental", place_id="ChIJ3"),
            found(name="Houston Plumbing", place_id="ChIJ4"),
        ],
    )
    palo = {"name": "Palo Alto Dental", "service_area": ["Palo Alto"]}
    out = listings.find(conn, config.load(), "Palo Alto Dental, Palo Alto", business_id="biz_1", knowledge=palo)
    assert [c["place_id"] for c in out] == ["ChIJ3"]
    houston = {"name": "Houston Plumbing", "service_area": ["Houston"]}
    out = listings.find(
        conn, config.load(), "Houston Plumbing, 12 Main St, Houston TX", business_id="biz_1", knowledge=houston
    )
    assert [c["place_id"] for c in out] == ["ChIJ4"]


def test_find_and_lookup_without_a_key_or_words_call_nothing(conn, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setattr(places, "search", lambda *a, **kw: pytest.fail("no call to Google without a key"))
    assert listings.find(conn, config.load(), "Apex Plumbing", business_id="biz_1", knowledge={}) == []
    assert listings.lookup(conn, config.load(), {"name": "Apex Plumbing"}, {}, business_id=None, rejected=[]) == []
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    assert listings.find(conn, config.load(), "   ", business_id="biz_1", knowledge={}) == []


def test_found_say_lists_the_places_for_the_owner():
    two = [CHOSEN, {**CHOSEN, "id": "gm_2", "name": "Bright Smile Care", "address": "1 Main St, Austin, TX"}]
    assert listings.found_say(two) == (
        "On Google Maps I found 1. Bright Smile Dental, 500 University Ave, Palo Alto, CA 94301; "
        "2. Bright Smile Care, 1 Main St, Austin, TX. Which one is the business? Or none of these."
    )
