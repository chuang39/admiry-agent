"""Google Places Text Search (New) for `start`'s lookup of the business, whose close places the consultation's first
question offers (spec §4.0; plan 2026-10-03-agent-listing-confirm), and for an address put on the map.
Only the name, address, place id and point are kept, with "Google Maps" attribution in the owner-facing question. A
lookup of the business (listings) also asks for each place's website and phone (`IDENTITY_MASK`), to tell whether it is
the business (listings.close), and never stores them; an address put on the map (`search_text`) asks for neither.
Nothing else is cached or republished. Optional: no key, no lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"
# The basic mask and the website and phone, which only a lookup that tells a place from the business reads (Google bills
# the contact fields at a higher tier than the name, address and point)
IDENTITY_MASK = FIELD_MASK + ",places.websiteUri,places.nationalPhoneNumber"
TIMEOUT_S = 5.0
_STATE = re.compile(r"^[A-Z]{2}( \d{5}(-\d{4})?)?$")  # a US address's state part: "TX", or "TX 77002", "TX 77002-1234"
_COUNTRY = re.compile(r",\s*(USA|United States)\s*$")  # Google Maps' last part of a US address


def display(address: str | None) -> str | None:
    """An address as a sentence says it (walk fixes 1, final review B M2): Google Maps' formatted address without its
    last part when that is the country (", USA", ", United States") -- the owner knows it, and one turn can name the
    place twice ("Centred on …", then the radius question). The stored address keeps it; anything else as it is."""
    return _COUNTRY.sub("", address) if isinstance(address, str) else address


def town(address: str | None) -> str | None:
    """The town of a US address as Google Maps formats it ("123 Main St, Houston, TX 77002, USA" → "Houston"): the
    part before the last part that is a state, alone or with its ZIP code. None when no part is one, or nothing comes
    before it -- an address this cannot read (one outside the US, a bare city) names no town (walk fixes 1, Task 4
    round 1)."""
    if not isinstance(address, str):
        return None
    parts = [part.strip() for part in address.split(",")]
    for at in range(len(parts) - 1, 0, -1):
        if _STATE.match(parts[at]):
            return parts[at - 1] or None
    return None


def state(address: str | None) -> str | None:
    """The state of a US address as Google Maps formats it ("123 Main St, Houston, TX 77002, USA" → "TX"): the two
    letters of the same part `town` reads the town before -- the last part that is a state, alone or with its ZIP code,
    with a part before it. None for an address this cannot read, as `town` is (A3p-6 batch 2: the industry site's
    state)."""
    if not isinstance(address, str):
        return None
    parts = [part.strip() for part in address.split(",")]
    for at in range(len(parts) - 1, 0, -1):
        if _STATE.match(parts[at]):
            return parts[at][:2]
    return None


@dataclass(frozen=True)
class Candidate:
    place_id: str
    name: str
    address: str
    lat: float | None = None
    lng: float | None = None
    website: str | None = None  # read only with_identity, to tell whether a place is the business; never stored
    phone: str | None = None

    def as_json(self) -> dict:
        return {
            "place_id": self.place_id,
            "name": self.name,
            "address": self.address,
            "lat": self.lat,
            "lng": self.lng,
            "source": "Google Maps",
        }


def maps_url(name: str | None, place_id: str | None) -> str | None:
    """A link that opens the place on Google Maps (Google's Maps URLs: a search for its name pinned to its place id);
    None without a place id."""
    if not place_id:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={quote(name or 'place')}&query_place_id={quote(place_id)}"


def label(name: str, address: str) -> str:
    """A place as an option and a sentence name it: its name and its address, the country left out (`display`)."""
    return f"{name}, {display(address)}"


def _candidate(p: dict) -> Candidate:
    location = p.get("location") if isinstance(p.get("location"), dict) else {}
    return Candidate(
        place_id=p.get("id", ""),
        name=(p.get("displayName") or {}).get("text", ""),
        address=p.get("formattedAddress", ""),
        lat=location.get("latitude"),
        lng=location.get("longitude"),
        website=p.get("websiteUri"),
        phone=p.get("nationalPhoneNumber"),
    )


def search(
    query: str,
    api_key: str,
    *,
    max_results: int = 1,
    with_identity: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> list[Candidate]:
    """Google's places for a free-text query, best first, at most `max_results`; [] on no match, an error or a
    timeout. `with_identity` also asks for each place's website and phone (`IDENTITY_MASK`): only a lookup that tells a
    place from the business needs them. Each call is one call to Google: its caller records it
    (geocode.record_lookup)."""
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_S) as client:
            r = client.post(
                PLACES_URL,
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": IDENTITY_MASK if with_identity else FIELD_MASK,
                },
                json={"textQuery": query, "maxResultCount": max_results},
            )
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    try:
        found = r.json().get("places") or []
    except ValueError:
        return []
    return [_candidate(p) for p in found if isinstance(p, dict)]


def search_text(query: str, api_key: str, *, transport: httpx.BaseTransport | None = None) -> Candidate | None:
    """The best match for a free-text business description, or None (geocode.place: an address put on the map)."""
    found = search(query, api_key, max_results=1, transport=transport)
    return found[0] if found else None
