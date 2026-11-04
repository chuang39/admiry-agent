import json

import httpx
import pytest

from agent import places


def test_search_text_sends_key_and_field_mask_and_returns_the_first_candidate():
    seen = {}

    def handler(request):
        seen["headers"], seen["body"] = request.headers, request.read()
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "ChIJ1",
                        "displayName": {"text": "Bright Smile Dental"},
                        "formattedAddress": "500 University Ave, Palo Alto, CA",
                    }
                ]
            },
        )

    c = places.search_text("dentist palo alto bright smile", "key-1", transport=httpx.MockTransport(handler))
    assert c == places.Candidate("ChIJ1", "Bright Smile Dental", "500 University Ave, Palo Alto, CA")
    assert seen["headers"]["x-goog-api-key"] == "key-1" and seen["headers"]["x-goog-fieldmask"] == places.FIELD_MASK
    assert b"maxResultCount" in seen["body"] and b"dentist palo alto bright smile" in seen["body"]
    # as the consultation's listing question shows it (plan 2026-10-03-agent-listing-confirm: no "Is this X?" of its own)
    assert places.label(c.name, c.address) == "Bright Smile Dental, 500 University Ave, Palo Alto, CA"
    assert c.as_json()["source"] == "Google Maps"


def test_search_text_parses_the_location_into_lat_lng():
    """A3 Task 3: FIELD_MASK asks for `places.location`, and a response that carries one turns into
    `Candidate.lat`/`lng` (D-row "Location", 2026-09-28 admiry-google-search-execution-design.md §4)."""
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "ChIJ1",
                        "displayName": {"text": "Bright Smile Dental"},
                        "formattedAddress": "500 University Ave, Palo Alto, CA",
                        "location": {"latitude": 37.4419, "longitude": -122.143},
                    }
                ]
            },
        )

    c = places.search_text("dentist palo alto", "key-1", transport=httpx.MockTransport(handler))
    assert (
        "places.location" in seen["headers"]["x-goog-fieldmask"]
        and places.FIELD_MASK == seen["headers"]["x-goog-fieldmask"]
    )
    assert c.lat == 37.4419 and c.lng == -122.143
    assert c.as_json()["lat"] == 37.4419 and c.as_json()["lng"] == -122.143


def test_search_text_defaults_lat_lng_to_none_without_a_location():
    """A response with no `location` key (an older fixture, or Google omitting it) is not a KeyError:
    lat/lng stay None, and the Candidate still equals one built with no lat/lng given at all."""
    t = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "ChIJ1",
                        "displayName": {"text": "Bright Smile Dental"},
                        "formattedAddress": "500 University Ave",
                    }
                ]
            },
        )
    )
    c = places.search_text("x", "k", transport=t)
    assert c.lat is None and c.lng is None
    assert c == places.Candidate("ChIJ1", "Bright Smile Dental", "500 University Ave")


@pytest.mark.parametrize(
    ("address", "town"),
    [
        ("123 Main St, Houston, TX 77002, USA", "Houston"),
        ("Houston, TX 77002, USA", "Houston"),
        ("Houston, TX, USA", "Houston"),
        ("500 University Ave, Palo Alto, CA 94301", "Palo Alto"),
        ("Paris, France", None),
        ("Palo Alto", None),
        ("Suite 200, 123 Main St, Houston, TX 77002-1234, USA", "Houston"),  # a ZIP+4
        ("TX 77002, USA", None),  # nothing before the state
        ("", None),
        (None, None),
    ],
)
def test_the_town_of_an_address_is_the_part_before_its_state(address, town):
    """Walk fixes 1, Task 4 round 1: Google Maps formats a US address "…, Houston, TX 77002, USA"; the town is the part
    before the last part that is a state (alone or with its ZIP code). An address this cannot read names no town."""
    assert places.town(address) == town


@pytest.mark.parametrize(
    ("address", "said"),
    [
        ("123 Main St, Houston, TX 77002, USA", "123 Main St, Houston, TX 77002"),
        ("123 Main St, Houston, TX 77002, United States", "123 Main St, Houston, TX 77002"),
        ("500 University Ave, Palo Alto, CA 94301", "500 University Ave, Palo Alto, CA 94301"),
        ("USA Mattress, 1 Main St, Austin, TX 78701", "USA Mattress, 1 Main St, Austin, TX 78701"),  # only a last part
        ("10 Downing St, London SW1A 2AA, UK", "10 Downing St, London SW1A 2AA, UK"),
        ("", ""),
        (None, None),
    ],
)
def test_an_address_is_said_without_its_country(address, said):
    """Walk fixes 1, final review B M2: Google Maps ends a US address with ", USA", and one turn can name the place
    twice ("Centred on …", then the radius question): a sentence says it without the country. The stored address keeps
    it."""
    assert places.display(address) == said


@pytest.mark.parametrize("status, body", [(200, {}), (200, {"places": []}), (403, {"error": "denied"})])
def test_search_text_returns_none_on_no_match_or_error(status, body):
    t = httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    assert places.search_text("x", "k", transport=t) is None


def test_search_text_returns_none_on_a_non_json_body():
    t = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    assert places.search_text("x", "k", transport=t) is None


def test_search_returns_every_place_with_its_website_and_phone():
    body = {
        "places": [
            {
                "id": "ChIJ1",
                "displayName": {"text": "Bright Smile Dental"},
                "formattedAddress": "500 University Ave, Palo Alto, CA 94301, USA",
                "location": {"latitude": 37.4419, "longitude": -122.143},
                "websiteUri": "https://www.brightsmile.example/",
                "nationalPhoneNumber": "(650) 555-0100",
            },
            {"id": "ChIJ2", "displayName": {"text": "Bright Smile Care"}, "formattedAddress": "1 Main St, Austin, TX"},
        ]
    }
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        seen["mask"] = request.headers["X-Goog-FieldMask"]
        return httpx.Response(200, json=body)

    out = places.search(
        "Bright Smile Dental Palo Alto", "k", max_results=5, with_identity=True, transport=httpx.MockTransport(handler)
    )
    assert [c.place_id for c in out] == ["ChIJ1", "ChIJ2"]
    assert (out[0].website, out[0].phone) == ("https://www.brightsmile.example/", "(650) 555-0100")
    assert (out[1].website, out[1].phone, out[1].lat) == (None, None, None)
    assert seen["json"] == {"textQuery": "Bright Smile Dental Palo Alto", "maxResultCount": 5}
    assert seen["mask"] == places.IDENTITY_MASK
    assert "places.websiteUri" in seen["mask"] and "places.nationalPhoneNumber" in seen["mask"]


def test_only_a_search_that_reads_the_website_and_phone_asks_google_for_them():
    """An address put on the map (`search_text`) and a plain `search` need neither: their request carries the basic
    mask, which Google bills at a lower tier than the contact fields (listing confirm, Task 1 round 1)."""
    masks = []

    def handler(request):
        masks.append(request.headers["X-Goog-FieldMask"])
        return httpx.Response(200, json={"places": [{"id": "ChIJ1", "displayName": {"text": "A"}}]})

    transport = httpx.MockTransport(handler)
    places.search_text("a", "k", transport=transport)
    places.search("a", "k", transport=transport)
    places.search("a", "k", with_identity=True, transport=transport)
    assert masks == [places.FIELD_MASK, places.FIELD_MASK, places.IDENTITY_MASK]
    assert "websiteUri" not in places.FIELD_MASK and "nationalPhoneNumber" not in places.FIELD_MASK
    assert places.IDENTITY_MASK.startswith(places.FIELD_MASK)


def test_search_text_is_the_first_of_one():
    def handler(request):
        assert json.loads(request.content)["maxResultCount"] == 1
        return httpx.Response(200, json={"places": [{"id": "ChIJ1", "displayName": {"text": "A"}}]})

    assert places.search_text("a", "k", transport=httpx.MockTransport(handler)).place_id == "ChIJ1"


def test_maps_url_opens_the_place_on_google_maps():
    assert places.maps_url("Bright Smile Dental", "ChIJ1") == (
        "https://www.google.com/maps/search/?api=1&query=Bright%20Smile%20Dental&query_place_id=ChIJ1"
    )
    assert places.maps_url("A", None) is None
