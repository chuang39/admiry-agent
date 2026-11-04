"""agent/profile.py and the business_profile command (spec 2026-10-01 §5, D161; plan G1 Task 4): services with ids,
one location assembled from the knowledge and the consultation's radius, offers; by-id changes; what a new
extraction keeps."""

from datetime import datetime, timedelta, timezone

import pytest
from fastmcp.exceptions import ToolError

from agent import business, consult, plans, profile
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key, knowledge_fixture
from agent.tests.surface_helpers import WALK_TEXT, planned

ROW = {"id": "biz_1", "locale": "en-US", "claim_state": "claimed"}
OFFER = {
    "id": "off_free_consultation",
    "text": "Free consultation",
    "service_id": None,
    "valid_until": None,
    "terms": None,
}


def _knowledge(**more) -> dict:
    return {
        **knowledge_fixture().model_dump(),
        "listing": {
            "place_id": "ChIJ1",
            "name": "Bright Smile Dental",
            "address": "500 University Ave",
            "lat": 37.4,
            "lng": -122.1,
        },
        **more,
    }


def test_a_service_id_is_the_consultations_own_and_a_stored_one_wins():
    assert profile.service_id({"name": "Water Heater Replacement"}) == consult._slug("Water Heater Replacement")
    assert profile.service_id({"name": "Tankless heaters", "id": "svc_water_heater_replacement"}) == (
        "svc_water_heater_replacement"
    )


def test_the_view_has_services_one_location_offers_and_facts():
    session = {"answers": {"service": {"choice": "svc_invisalign"}}}
    v = profile.view(
        ROW, 3, _knowledge(geo_model="storefront"), session=session, radius_miles=5.0, plan_offer="Free consultation"
    )
    assert list(v) == ["business", "locations", "services", "offers", "facts", "knowledge_version"]
    assert v["business"] == {
        "id": "biz_1",
        "name": "Bright Smile Dental",
        "trade": None,
        "category": "dentists",
        "category_detail": "family dentist",
        "website": "https://brightsmile.example",
        "google_listing": None,  # the listing on file is a lookup's guess: the owner confirmed none
        "locale": "en-US",
        "claim_state": "claimed",
    }
    assert v["locations"] == [
        {
            "id": "loc_1",
            "geo_model": "storefront",
            "address": "500 University Ave",
            "center": {"lat": 37.4, "lng": -122.1},
            "radius_miles": 5.0,
            "areas": ["Palo Alto"],
            "hours": "Mon-Fri 8-5",
            "phone": "650-555-0100",
            "phone_hours": None,
        }
    ]
    assert [(s["id"], s["name"], s["lead"], s["price_hint"]) for s in v["services"]] == [
        ("svc_invisalign", "Invisalign", True, None),
        ("svc_cleanings", "Cleanings", False, "$120"),
    ]
    assert v["offers"] == [
        {
            "id": None,  # not on file: no change takes it by id until it is added by its text
            "text": "Free consultation",
            "service_id": None,
            "valid_until": None,
            "terms": None,
            "from": "plan",
        }
    ]
    assert v["facts"]["usp"] == ["Same-week appointments"] and v["facts"]["compliance_flags"] == ["healthcare"]
    assert v["knowledge_version"] == 3
    added = profile.apply(_knowledge(), {"offer_changes": [{"text": "Free consultation"}]})
    offers = profile.view(ROW, 4, added, plan_offer="Free consultation")["offers"]
    assert [(o["id"], o.get("from")) for o in offers] == [("off_free_consultation", None)]


def test_the_goals_focus_services_lead_too():
    v = profile.view(ROW, 1, _knowledge(), goal={"focus_services": ["cleanings"]})
    assert [s["lead"] for s in v["services"]] == [False, True]


def test_changes_add_update_and_remove_services_by_id():
    k = profile.apply(
        _knowledge(),
        {
            "service_changes": [
                {"name": "Teeth whitening", "price_hint": "$399"},
                {"id": "svc_cleanings", "name": "Cleanings and exams"},
                {"id": "svc_invisalign", "remove": True},
            ]
        },
    )
    assert k["schema"] == 2
    assert [(s["id"], s["name"], s.get("price_hint")) for s in k["services"]] == [
        ("svc_cleanings", "Cleanings and exams", "$120"),  # the id survives the rename
        ("svc_teeth_whitening", "Teeth whitening", "$399"),
    ]


@pytest.mark.parametrize(
    ("changes", "text"),
    [
        ({"service_changes": [{"id": "svc_nope", "name": "x"}]}, "no service with id svc_nope"),
        ({"service_changes": [{"price_hint": "$5"}]}, "a new service needs a name"),
        ({"service_changes": [{"name": "Invisalign"}]}, "already on file as svc_invisalign"),
        ({"service_changes": "Invisalign"}, "service_changes must be a list of objects"),
        ({"location": {"geo_model": "drive_through"}}, "geo_model must be storefront or service_area"),
        ({"location": {"radius_miles": 10}}, "the radius is part of the plan"),
        ({"location": {"areas": "Palo Alto"}}, "areas must be a list of place names"),
        ({"location": {"id": "loc_2", "hours": "Sat"}}, "this business has one location, loc_1"),
        ({"location": {"address": "123 Main St"}}, "address is placed by the command, not here"),
        ({"location": {"center": {"lat": 1, "lng": 2}}}, r"unknown location fields: \['center'\]"),
        ({"offer_changes": [{"id": "off_nope", "text": "x"}]}, "no offer with id off_nope"),
        ({"offer_changes": [{"service_id": "svc_invisalign"}]}, "a new offer needs its text"),
        ({"notes_add": "one long note"}, "notes_add must be a list of sentences"),
    ],
)
def test_a_bad_change_is_refused(changes, text):
    with pytest.raises(ToolError, match=text):
        profile.apply(_knowledge(), changes)


def test_the_location_and_the_offers_and_the_notes_change():
    k = profile.apply(
        _knowledge(),
        {
            "location": {
                "geo_model": "service_area",
                "areas": ["Palo Alto", "Menlo Park"],
                "phone_hours": "Mon-Fri 8-6",
            },
            "offer_changes": [{"text": "Free consultation", "service_id": "svc_invisalign"}],
            "notes_add": ["We are closed on Sundays."],
        },
    )
    assert (k["geo_model"], k["service_area"], k["phone_hours"]) == (
        "service_area",
        ["Palo Alto", "Menlo Park"],
        "Mon-Fri 8-6",
    )
    assert k["offers"] == [
        {
            "id": "off_free_consultation",
            "text": "Free consultation",
            "service_id": "svc_invisalign",
            "valid_until": None,
            "terms": None,
        }
    ]
    assert [n["text"] for n in k["notes"]] == ["We are closed on Sundays."] and k["notes"][0]["source"] == "owner"
    gone = profile.apply(k, {"offer_changes": [{"id": "off_free_consultation", "remove": True}]})
    assert gone["offers"] == []


# The place the command found for "123 Main St, Houston" (geocode.listing): an address, as Google Maps formats it
PLACED = {
    "place_id": "ChIJ2",
    "name": "123 Main St",
    "address": "123 Main St, Houston, TX 77002, USA",
    "lat": 29.76,
    "lng": -95.37,
    "source": "Google Maps",
    "kind": "address",
}
HOUSTON = {"location": {"address": "123 Main St, Houston", "listing": PLACED}}


def test_a_place_moves_the_business_listing_and_keeps_its_name():
    """Walk fixes 1, Task 4 (round 1): an address put on the map (update_business's prepare, the consultation's answer)
    moves the business's own Google listing to the place -- its address and point -- and keeps the rest of it, its
    name and place id: the name stays vouched for (plans.name_for). The location keeps the place, not the words."""
    confirmed = {**_knowledge()["listing"], "confirmed": True}  # the owner's own listing: they confirmed it
    k = profile.apply(_knowledge(listing=confirmed), HOUSTON)
    moved = {**confirmed, "address": PLACED["address"], "lat": 29.76, "lng": -95.37, "placed": True}
    assert k["listing"] == moved and "kind" not in k["listing"] and "address" not in k
    assert plans.name_for({**k, "website": None}) == "Bright Smile Dental"  # the listing alone vouches for it
    where = profile.view(ROW, 2, k)["locations"][0]
    assert (where["address"], where["center"]) == (PLACED["address"], {"lat": 29.76, "lng": -95.37})


def test_with_no_business_listing_the_place_is_kept_as_an_address():
    """None on file, or an address placed before: the place's own listing, marked an address, which vouches for no
    name."""
    earlier = {**PLACED, "address": "1 Old Rd, Austin, TX 78701, USA", "lat": 30.27, "lng": -97.74}
    for on_file in (None, earlier):
        k = profile.apply(_knowledge(listing=on_file), HOUSTON)
        assert k["listing"] == PLACED, on_file
        assert plans.name_for({**k, "website": None}) is None  # a sentence names the address, without its country
        assert plans.address_for(k) == "123 Main St, Houston, TX 77002"


def test_a_place_the_owner_put_on_the_map_is_known_as_one():
    """Final review A1: what a new reading keeps where the ads centre (tools_start, consult.move_session) -- an address
    the owner placed, or the business's own listing moved to one (`_placed` marks it) -- and never the business's
    listing as Google Maps gave it, nor anything that is no listing."""
    moved = profile.apply(_knowledge(), HOUSTON)["listing"]
    assert profile.placed(PLACED) and profile.placed(moved)
    assert not profile.placed(_knowledge()["listing"])
    assert not any(profile.placed(other) for other in (None, {}, ["123 Main St"], {"placed": "yes"}))


def test_a_model_reads_a_placed_address_as_its_address_only():
    """Round 2: the one rule every prompt goes through (the reading's listing, the plan draft's and the answerer's
    knowledge): an address the owner placed shows only that it is an address, and where -- never its name, a street's
    or a shop's at that address, its place id or its rating; the business's own listing, as the owner confirmed it,
    shows whole. A copy: the knowledge given is not changed."""
    placed = {**PLACED, "name": "Joe's Coffee", "rating": 4.7}
    k = _knowledge(listing=placed)
    seen = profile.for_model(k)
    assert seen["listing"] == profile.model_listing(placed) == {"kind": "address", "address": PLACED["address"]}
    assert k["listing"] == placed and {**seen, "listing": placed} == k
    own = _knowledge(listing={**_knowledge()["listing"], "confirmed": True})
    assert profile.for_model(own) == own  # the business's own listing, whole
    assert profile.for_model({}) == {} and profile.for_model(None) == {}
    assert profile.model_listing(None) is None


def test_a_model_never_reads_a_listing_the_owner_did_not_confirm():
    """Plan 2026-10-03-agent-listing-confirm: a place a lookup found before the owner picked it is read by no prompt --
    not the reading's (tools_business.reading_listing), not the plan draft's or the answerer's (for_model): the copy a
    model reads has no listing at all. Anything that is no listing shows to no one either."""
    guess = _knowledge()["listing"]
    k = _knowledge(listing=guess)
    seen = profile.for_model(k)
    assert "listing" not in seen and {**seen, "listing": guess} == k
    assert k["listing"] == guess  # the knowledge given is not changed
    for other in (guess, {**guess, "confirmed": False}, {**guess, "confirmed": "yes"}, ["500 Univ"], "500 Univ", None):
        assert profile.model_listing(other) is None, other
        assert "listing" not in profile.for_model({"name": "X", "listing": other}), other
    assert profile.for_model({"name": "X", "listing": guess}) == {"name": "X"}


def test_a_model_never_reads_the_places_the_owner_said_are_not_theirs():
    """Final review A m4: `listing_rejected` is place ids, which no prompt needs: the copy a model reads drops it, and
    the knowledge given keeps it."""
    k = _knowledge(listing_rejected=["ChIJ1", "ChIJ2"])
    assert "listing_rejected" not in profile.for_model(k)
    assert k["listing_rejected"] == ["ChIJ1", "ChIJ2"]


def test_a_place_adds_its_town_to_the_areas_once():
    assert profile.apply(_knowledge(), HOUSTON)["service_area"] == ["Palo Alto", "Houston"]
    there = profile.apply(_knowledge(service_area=["Palo Alto", "houston"]), HOUSTON)
    assert there["service_area"] == ["Palo Alto", "houston"]  # already there, whatever its case
    text = profile.apply(_knowledge(service_area="Palo Alto, CA"), HOUSTON)
    assert text["service_area"] == ["Palo Alto, CA", "Houston"]  # an area stored as text is one area, not its letters
    paris = {"location": {"address": "Paris", "listing": {**PLACED, "address": "Paris, France"}}}
    assert profile.apply(_knowledge(), paris)["service_area"] == ["Palo Alto"]  # no town to read: nothing added
    named = {"location": {**HOUSTON["location"], "areas": ["Bellaire"]}}
    assert profile.apply(_knowledge(), named)["service_area"] == ["Bellaire"]  # areas given with it still replace


def test_a_new_service_whose_id_is_taken_gets_a_suffix_and_only_its_name_is_a_duplicate():
    renamed = profile.apply(_knowledge(), {"service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}]})
    k = profile.apply(renamed, {"service_changes": [{"name": "Cleanings"}]})  # the old name: a service of its own now
    assert [(profile.service_id(s), s["name"]) for s in k["services"]] == [
        ("svc_invisalign", "Invisalign"),
        ("svc_cleanings", "Cleanings and exams"),
        ("svc_cleanings_2", "Cleanings"),
    ]
    first, second = (
        "Full mouth reconstruction with dental implants",
        "Full mouth reconstruction with dental implant bridges",
    )
    assert profile.slug("svc", first) == profile.slug("svc", second)  # the slug is cut at 40 characters
    k = profile.apply(k, {"service_changes": [{"name": first}, {"name": second}]})
    assert [profile.service_id(s) for s in k["services"][3:]] == [
        profile.slug("svc", first),
        f"{profile.slug('svc', first)}_2",
    ]
    with pytest.raises(ToolError, match="CLEANINGS is already on file as svc_cleanings_2; change it by that id"):
        profile.apply(k, {"service_changes": [{"name": "CLEANINGS"}]})


def test_a_new_offer_whose_id_is_taken_gets_a_suffix_and_only_its_text_is_a_duplicate():
    renamed = {"offer_changes": [{"id": "off_free_consultation", "text": "Free consultation and X-rays"}]}
    k = profile.apply(
        profile.apply(_knowledge(offers=[OFFER]), renamed), {"offer_changes": [{"text": "Free consultation"}]}
    )
    assert [(o["id"], o["text"]) for o in k["offers"]] == [
        ("off_free_consultation", "Free consultation and X-rays"),
        ("off_free_consultation_2", "Free consultation"),
    ]
    with pytest.raises(ToolError, match="free consultation is already on file as off_free_consultation_2"):
        profile.apply(k, {"offer_changes": [{"text": "free consultation"}]})


def test_a_new_extraction_keeps_what_it_cannot_know():
    before = profile.apply(
        _knowledge(),
        {
            "service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}],
            "offer_changes": [{"text": "Free consultation"}],
            "location": {"geo_model": "storefront", "phone_hours": "Mon-Fri 8-6"},
            "notes_add": ["Closed on Sundays."],
        },
    )
    fresh = knowledge_fixture().model_dump()
    fresh["services"][1]["name"] = "Cleanings and exams"  # the site now says so too
    after = profile.carry(before, fresh)
    assert after["services"][1]["id"] == "svc_cleanings"  # the stored id, not svc_cleanings_and_exams
    assert (after["geo_model"], after["phone_hours"]) == ("storefront", "Mon-Fri 8-6")
    assert [n["text"] for n in after["notes"]] == ["Closed on Sundays."]
    assert [o["id"] for o in after["offers"]] == ["off_free_consultation"]
    assert profile.carry(None, fresh) is fresh and profile.carry(knowledge_fixture().model_dump(), fresh) is fresh


def test_a_new_extraction_keeps_the_listing_unless_it_brings_its_own():
    """Final review B, Important 9 (open item C2): a reading that looked nothing up brings no listing, and the one on
    file -- the address and the map point the ads centre on -- stays; a reading that looked the business up brings its
    own, which replaces it."""
    before = _knowledge()
    fresh = knowledge_fixture().model_dump()
    assert "listing" not in fresh
    assert profile.carry(before, fresh)["listing"] == before["listing"]
    found = {
        "place_id": "ChIJ2",
        "name": "Bright Smile Dental",
        "address": "1 Hamilton Ave",
        "lat": 37.5,
        "lng": -122.2,
    }
    assert profile.carry(before, {**fresh, "listing": found})["listing"] == found
    assert profile.view(ROW, 2, profile.carry(before, fresh))["locations"][0]["address"] == "500 University Ave"


def test_the_profile_shows_the_confirmed_listing_with_a_google_maps_link():
    row = {"id": "biz_1", "locale": "en-US", "claim_state": "claimed"}
    confirmed = {
        "place_id": "ChIJ1",
        "name": "Bright Smile Dental",
        "address": "500 University Ave, Palo Alto, CA 94301, USA",
        "lat": 37.44,
        "lng": -122.14,
        "source": "Google Maps",
        "confirmed": True,
    }
    shown = profile.view(row, 3, {"listing": confirmed})["business"]["google_listing"]
    assert shown == {
        "name": "Bright Smile Dental",
        "address": "500 University Ave, Palo Alto, CA 94301, USA",
        "maps_url": "https://www.google.com/maps/search/?api=1&query=Bright%20Smile%20Dental&query_place_id=ChIJ1",
    }
    guess = {key: value for key, value in confirmed.items() if key != "confirmed"}
    assert profile.view(row, 3, {"listing": guess})["business"]["google_listing"] is None
    placed = {"kind": "address", "address": "12 Elm St", "lat": 1.0, "lng": 2.0}
    assert profile.view(row, 3, {"listing": placed})["business"]["google_listing"] is None


def test_a_new_reading_keeps_the_places_the_owner_said_are_not_theirs():
    kept = profile.carry({"listing_rejected": ["ChIJ1"]}, {"name": "Bright Smile Dental"})
    assert kept["listing_rejected"] == ["ChIJ1"]


def test_a_new_reading_that_names_no_site_keeps_the_one_on_file():
    """Ruling F3: a reading that brings no site -- neither a `website` it read nor a `website_unread` it could not --
    keeps the version before's, so a business whose site was read is offered it again when it starts over with plain
    words; a reading that brings a site of either kind replaces both."""
    site, unread = "https://brightsmile.example", "https://bright.example"
    fresh = {**knowledge_fixture().model_dump(), "website": None}
    assert profile.carry({"website": site}, fresh)["website"] == site
    kept = profile.carry({"website": site, "website_unread": unread}, fresh)
    assert (kept["website"], kept["website_unread"]) == (site, unread)
    assert profile.carry({"website_unread": unread}, fresh)["website_unread"] == unread
    newer = profile.carry({"website": site, "website_unread": unread}, {**fresh, "website": "https://new.example"})
    assert newer["website"] == "https://new.example" and "website_unread" not in newer
    unreadable = profile.carry({"website": site}, {**fresh, "website_unread": unread})
    assert unreadable["website_unread"] == unread and unreadable["website"] is None


def test_the_command_answers_with_the_profile_and_include_limits_it(client, conn, walk_fake):
    walk = planned(client, conn)
    res = call_tool(client, "business_profile", {}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    d = data(res)
    assert d["business"]["name"] == "Bright Smile Dental" and d["locations"][0]["radius_miles"] == 5.0
    assert [s["id"] for s in d["services"] if s["lead"]] == ["svc_invisalign"]
    say = res["structuredContent"]["say"]
    assert say == "Here is what I know about Bright Smile Dental: 2 services on file." and not JARGON.search(say)
    only = data(call_tool(client, "business_profile", {"include": ["services"]}, key=walk["key"]))
    assert list(only) == ["services", "knowledge_version"]
    bad = call_tool(client, "business_profile", {"include": ["secrets"]}, key=walk["key"])
    assert bad["isError"] and "include may name business, locations, services, offers, facts" in tool_text(bad)


def test_update_business_takes_by_id_changes_and_the_consultation_offers_the_stored_id(client, conn, walk_fake):
    walk = planned(client, conn)
    res = call_tool(
        client,
        "update_business",
        {
            "patch": {"service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}]},
            "idempotency_key": "ub-1",
        },
        key=walk["key"],
    )
    assert not res["isError"], tool_text(res)
    stored = business.latest_knowledge(conn, "biz_1")[1]
    assert stored["services"][1] == {**stored["services"][1], "id": "svc_cleanings", "name": "Cleanings and exams"}
    options = consult.build_options(consult.load_tree("dentists"), stored)["service"]
    assert {"id": "svc_cleanings", "label": "Cleanings and exams"} in options


def test_update_business_stores_the_location_the_offers_and_the_notes(client, conn, walk_fake):
    walk = planned(client, conn)
    patch = {
        "location": {"id": "loc_1", "geo_model": "storefront", "phone_hours": "Mon-Fri 8-6"},
        "offer_changes": [{"text": "Free consultation", "service_id": "svc_invisalign"}],
        "notes_add": ["Closed on Sundays."],
    }
    res = call_tool(client, "update_business", {"patch": patch, "idempotency_key": "ub-2"}, key=walk["key"])
    assert not res["isError"] and data(res)["updated"] == ["location", "notes_add", "offer_changes"], tool_text(res)
    stored = business.latest_knowledge(conn, "biz_1")[1]
    assert (stored["geo_model"], stored["phone_hours"]) == ("storefront", "Mon-Fri 8-6")
    assert stored["offers"] == [{**OFFER, "service_id": "svc_invisalign"}]
    assert [(n["text"], n["source"]) for n in stored["notes"]] == [("Closed on Sundays.", "owner")]


def _renamed(client, conn, walk_fake) -> dict:
    """The walk's business with Cleanings renamed by id to "Cleanings and exams", and a site that now says so too."""
    walk = planned(client, conn)
    rename = {"service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}]}
    res = call_tool(client, "update_business", {"patch": rename, "idempotency_key": "rename"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    walk_fake.knowledge.services[1].name = "Cleanings and exams"
    return walk


def test_a_new_consultation_offers_the_id_on_file_and_its_pick_leads(client, conn, walk_fake):
    walk = _renamed(client, conn, walk_fake)
    res = call_tool(client, "start", {"text": WALK_TEXT, "website_url": "https://brightsmile.example"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    sid = data(res)["session_id"]
    objective = {"session_id": sid, "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "again-1"}
    first = call_tool(client, "answer", objective, key=walk["key"])
    assert not first["isError"], tool_text(first)
    question = first["structuredContent"]["next"]["question"]
    offered = next(o["id"] for o in question["options"] if o["label"] == "Cleanings and exams")
    assert (question["id"], offered) == ("service", "svc_cleanings")  # the id on file, not svc_cleanings_and_exams
    pick = {"session_id": sid, "answers": {"service": {"choice": offered}}, "idempotency_key": "again-2"}
    assert not call_tool(client, "answer", pick, key=walk["key"])["isError"]
    services = data(call_tool(client, "business_profile", {}, key=walk["key"]))["services"]
    assert [(s["id"], s["lead"]) for s in services] == [("svc_invisalign", False), ("svc_cleanings", True)]
    change = {"patch": {"service_changes": [{"id": offered, "price_hint": "$150"}]}, "idempotency_key": "again-3"}
    changed = call_tool(client, "update_business", change, key=walk["key"])
    assert not changed["isError"], tool_text(changed)


def test_describe_business_answers_with_the_version_on_file(client, conn, walk_fake):
    walk = _renamed(client, conn, walk_fake)
    res = call_tool(client, "describe_business", {"text": "family dentist", "idempotency_key": "d-1"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    knowledge = data(res)["knowledge"]
    assert knowledge["services"][1] == {
        **knowledge["services"][1],
        "id": "svc_cleanings",
        "name": "Cleanings and exams",
    }
    assert knowledge == business.latest_knowledge(conn, "biz_1")[1]


def test_describe_business_keeps_no_address_the_words_gave_on_the_profile(client, conn, walk_fake):
    """The reading's stated_address is start's, to put on the map (2026-10-04): a description keeps it off the profile."""
    walk = _renamed(client, conn, walk_fake)
    walk_fake.knowledge.stated_address = "500 University Ave, Palo Alto, CA"
    args = {"text": "family dentist at 500 University Ave", "idempotency_key": "d-2"}
    res = call_tool(client, "describe_business", args, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert "stated_address" not in business.latest_knowledge(conn, "biz_1")[1]


def test_a_hand_off_keeps_the_profile_and_the_moved_pick_leads_by_its_name(conn):
    """consult.move_session copies a draft's knowledge onto a business that may already have a profile: what the owner
    set by hand stays, as on a new reading. The moved session froze its options from the draft, whose ids are not the
    ones on file: its pick leads by the name of the option picked."""
    business_with_key(conn, biz_id="biz_draft")
    business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    owner = profile.apply(
        _knowledge(),
        {
            "service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}],
            "offer_changes": [{"text": "Free consultation"}],
            "location": {"geo_model": "storefront", "phone_hours": "Mon-Fri 8-6"},
            "notes_add": ["Closed on Sundays."],
        },
    )
    business.add_knowledge(conn, "biz_owner", owner, source="owner")
    draft = knowledge_fixture().model_dump()
    draft["services"][1]["name"] = "Cleanings and exams"  # the visitor's words name it too
    business.add_knowledge(conn, "biz_draft", draft, source="extract")
    tree = consult.load_tree("dentists")
    picked = {"choice": ["svc_cleanings_and_exams"], "raw": "svc_cleanings_and_exams", "source": "choice"}
    session = consult.create(
        conn,
        "biz_draft",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, draft),
        answers={"service": picked},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    moved = consult.move_session(conn, session, to_business="biz_owner")
    version, landed = business.latest_knowledge(conn, "biz_owner")
    kept = (landed.get("offers"), landed.get("notes"), landed.get("geo_model"), landed.get("phone_hours"))
    assert kept == (owner["offers"], owner["notes"], "storefront", "Mon-Fri 8-6")
    assert landed["services"][1] == {**landed["services"][1], "id": "svc_cleanings", "name": "Cleanings and exams"}
    services = profile.view(business.row(conn, "biz_owner"), version, landed, session=moved)["services"]
    assert [(s["id"], s["lead"]) for s in services] == [("svc_invisalign", False), ("svc_cleanings", True)]


def test_a_business_with_nothing_on_file_says_so(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(client, "business_profile", {}, key=key)
    assert res["isError"] and "Admiry knows nothing about this business yet" in tool_text(res)


# update_business's whole-list `services` patch bounds its size, not its shape (test_settings_page says so of the
# knowledge): the profile reads such a version without failing, and a by-id change never stores a shape it cannot read.


def test_services_stored_as_words_read_as_services_and_the_nameless_are_left_out():
    odd = _knowledge(services=[" Invisalign ", {"name": 5, "id": "svc_five"}, {"name": "Cleanings", "id": "svc_kept"}])
    v = profile.view(ROW, 1, odd, goal={"focus_services": ["cleanings"]})
    assert [(s["id"], s["name"], s["lead"]) for s in v["services"]] == [
        ("svc_invisalign", "Invisalign", False),
        ("svc_kept", "Cleanings", True),
    ]
    changed = profile.apply(odd, {"service_changes": [{"id": "svc_kept", "price_hint": "$99"}]})
    assert changed["services"] == [{"name": "Invisalign"}, {"name": "Cleanings", "id": "svc_kept", "price_hint": "$99"}]
    fresh = knowledge_fixture().model_dump()
    assert profile.carry(odd, fresh)["services"][1]["id"] == "svc_kept"
    unreadable = profile.carry({**odd, "services": 7}, fresh)  # no id from services it cannot read; the listing stays
    assert unreadable["services"] == fresh["services"] and unreadable["listing"] == odd["listing"]


def test_a_service_whose_name_is_only_spaces_is_left_out():
    v = profile.view(ROW, 1, _knowledge(services=[{"name": "  "}, {"name": "Cleanings"}]))
    assert [(s["id"], s["name"]) for s in v["services"]] == [("svc_cleanings", "Cleanings")]


@pytest.mark.parametrize(
    ("shape", "readable"),
    [
        (["Invisalign", "Cleanings"], [{"name": "Invisalign"}, {"name": "Cleanings"}]),
        ([{"name": 5}], []),
        ([None], []),
        (7, []),
    ],
    ids=["words", "a name that is not text", "nothing", "a number"],
)
def test_carry_reads_a_draft_in_any_shape_never_an_error(shape, readable):
    """A hand-off carries onto the destination from the draft's stored version, which a draft key's whole-list patch
    may have given any shape."""
    destination = profile.apply(
        _knowledge(),
        {
            "service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}],
            "offer_changes": [{"text": "Free consultation"}],
        },
    )
    after = profile.carry(destination, {**knowledge_fixture().model_dump(), "services": shape})
    assert after["offers"] == destination["offers"] and after["services"] == readable


def test_a_hand_off_of_services_patched_as_words_keeps_the_id_on_file(conn):
    business_with_key(conn, biz_id="biz_draft")
    business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    rename = {"service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}]}
    business.add_knowledge(conn, "biz_owner", profile.apply(_knowledge(), rename), source="owner")
    words = {**knowledge_fixture().model_dump(), "services": ["Invisalign", "Cleanings and exams"]}
    business.add_knowledge(conn, "biz_draft", words, source="owner")  # a draft key's whole-list patch
    session = consult.create(
        conn,
        "biz_draft",
        tree="fallback",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(consult.load_tree("fallback"), {}),
        answers={},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert consult.move_session(conn, session, to_business="biz_owner")["business_id"] == "biz_owner"
    assert business.latest_knowledge(conn, "biz_owner")[1]["services"] == [
        {"name": "Invisalign"},
        {"name": "Cleanings and exams", "id": "svc_cleanings"},
    ]


def test_other_fields_stored_in_other_shapes_read_as_one_entry_or_none():
    odd = _knowledge(service_area="Palo Alto, CA", usp=5, pricing_hints={"cleanings": "$120"}, listing=["500 Univ"])
    v = profile.view(ROW, 1, odd)
    where = v["locations"][0]
    assert (where["areas"], where["address"], where["center"]) == (["Palo Alto, CA"], None, None)
    assert (v["facts"]["usp"], v["facts"]["pricing_hints"]) == ([], [])
    mixed = profile.view(ROW, 1, _knowledge(service_area=[{"city": "Austin"}, "  ", " Queens "]))
    assert mixed["locations"][0]["areas"] == ["Queens"]  # only the entries in text, stripped


def test_a_by_id_change_keeps_services_patched_as_words_as_objects(client, conn, walk_fake):
    key = business_with_key(conn, claim_state="claimed")
    assert not call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)["isError"]
    words = {"patch": {"services": ["Invisalign", "Cleanings"]}, "idempotency_key": "u1"}
    assert not call_tool(client, "update_business", words, key=key)["isError"]
    added = {"patch": {"service_changes": [{"name": "Teeth whitening"}]}, "idempotency_key": "u2"}
    res = call_tool(client, "update_business", added, key=key)
    assert not res["isError"], tool_text(res)
    assert business.latest_knowledge(conn, "biz_1")[1]["services"] == [
        {"name": "Invisalign"},
        {"name": "Cleanings"},
        {"id": "svc_teeth_whitening", "name": "Teeth whitening", "intent_phrases": [], "price_hint": None},
    ]


def test_a_new_description_lands_over_services_an_assistant_patched_as_words(client, conn, walk_fake):
    """Services patched as words, and a name stored as an object before update_business checked the name's shape."""
    key = business_with_key(conn, claim_state="claimed")
    first = call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    assert not first["isError"], tool_text(first)
    words = {"patch": {"services": ["Invisalign", "Cleanings"]}, "idempotency_key": "u1"}
    assert not call_tool(client, "update_business", words, key=key)["isError"]
    patched = business.latest_knowledge(conn, "biz_1")[1]
    business.add_knowledge(conn, "biz_1", {**patched, "name": {"en": "Bright Smile"}}, source="owner")
    seen = call_tool(client, "business_profile", {}, key=key)
    assert not seen["isError"], tool_text(seen)
    assert [s["id"] for s in data(seen)["services"]] == ["svc_invisalign", "svc_cleanings"]  # the words, read as names
    assert seen["structuredContent"]["say"] == "Here is what I know about your business: 2 services on file."
    again = call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d2"}, key=key)
    assert not again["isError"], tool_text(again)
    assert [s["id"] for s in data(call_tool(client, "business_profile", {}, key=key))["services"]] == [
        "svc_invisalign",
        "svc_cleanings",
    ]


@pytest.mark.parametrize(
    ("changes", "text"),
    [
        ({"service_changes": [{"id": "svc_cleanings", "name": 5}]}, "a service needs a name"),
        ({"service_changes": [{"id": "svc_cleanings", "name": "  "}]}, "a service needs a name"),
        (
            {"service_changes": [{"id": "svc_cleanings", "intent_phrases": 5}]},
            "intent_phrases must be a list of phrases",
        ),
        ({"service_changes": [{"name": "Whitening", "intent_phrases": "whitening"}]}, "intent_phrases must be a list"),
        ({"service_changes": [{"name": "Whitening", "price_hint": 399}]}, "price_hint must be text"),
        ({"offer_changes": [{"id": "off_free_consultation", "text": 5}]}, "an offer needs its text"),
        ({"offer_changes": [{"text": "20% off", "terms": {"min": 100}}]}, "terms must be text"),
        ({"offer_changes": [{"id": "off_free_consultation", "service_id": 7}]}, "service_id must be text"),
    ],
)
def test_a_change_in_a_shape_the_profile_cannot_read_is_refused(changes, text):
    with pytest.raises(ToolError, match=text):
        profile.apply(_knowledge(offers=[OFFER]), changes)


def test_an_offer_on_file_is_changed_by_its_id_never_added_twice():
    """An id names one offer, as it names one service: a second one would leave the first unreachable by id."""
    with pytest.raises(ToolError, match="Free consultation is already on file as off_free_consultation"):
        profile.apply(_knowledge(offers=[OFFER]), {"offer_changes": [{"text": "Free consultation"}]})


def test_a_profile_holds_at_most_thirty_services_and_ten_offers(client, conn, walk_fake, strict_surface):
    """Open item A7: the caps, through the public tool. The add that would pass a cap is refused with its sentence and
    nothing is written; the adds that reach it exactly are taken."""
    walk = planned(client, conn)

    def patched(patch: dict, key: str) -> dict:
        sent = {"patch": patch, "idempotency_key": key}
        return call_tool(client, "update_business_profile", sent, key=walk["key"])

    def on_file() -> dict:
        return data(call_tool(client, "get_business_profile", {}, key=walk["key"]))

    room = profile.MAX_SERVICES - len(on_file()["services"])
    services = [{"name": f"Service number {i}"} for i in range(room + 1)]
    version = on_file()["knowledge_version"]
    over = patched({"services": services}, "cap-s1")
    assert over["isError"] and tool_text(over) == f"at most {profile.MAX_SERVICES} services", tool_text(over)
    assert on_file()["knowledge_version"] == version  # nothing written
    full = patched({"services": services[:room]}, "cap-s2")
    assert not full["isError"], tool_text(full)
    assert len(on_file()["services"]) == profile.MAX_SERVICES

    def offers_on_file() -> int:
        return len([o for o in on_file()["offers"] if o["id"]])  # an offer the plan carries has no id: not on file

    room = profile.MAX_OFFERS - offers_on_file()
    offers = [{"text": f"Offer number {i}"} for i in range(room + 1)]
    over = patched({"offers": offers}, "cap-o1")
    assert over["isError"] and tool_text(over) == f"at most {profile.MAX_OFFERS} offers", tool_text(over)
    full = patched({"offers": offers[:room]}, "cap-o2")
    assert not full["isError"], tool_text(full)
    assert offers_on_file() == profile.MAX_OFFERS
