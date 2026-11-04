"""Places a fake lookup returns (plan 2026-10-03-agent-listing-confirm)."""

from __future__ import annotations

from agent import places


def found(name="Bright Smile Dental", address="500 University Ave, Palo Alto, CA 94301, USA", place_id="ChIJ1", **kw):
    return places.Candidate(
        place_id=place_id,
        name=name,
        address=address,
        lat=kw.get("lat", 37.4419),
        lng=kw.get("lng", -122.143),
        website=kw.get("website"),
        phone=kw.get("phone"),
    )


def offered() -> dict:
    """`found()` as an offer holds it (listings.numbered): the first place, gm_1."""
    return {"id": "gm_1", **{k: v for k, v in found().as_json().items() if k != "source"}}
