"""A business's profile in the shape assistants read (spec 2026-10-01 §5; plan G1 Task 4): services with ids, one
location, offers.

The knowledge keeps its single-location fields (`listing`, `service_area`, `hours`, `phone`): `view` assembles
`locations[0]` from them and the consultation's radius, and `apply` writes a change back to them. A second location
is a later design; the array is there so it changes the data and not the interface.

Pure: no database, no model. It imports nothing from `agent` but `places`, a leaf, for the town of an address
(business.py imports this module, and consult.py imports business)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastmcp.exceptions import ToolError

from . import places

SCHEMA = 2
LOCATION_ID = "loc_1"
GEO_MODELS = ("storefront", "service_area")
SECTIONS = ("business", "locations", "services", "offers", "facts")
CHANGE_KEYS = ("service_changes", "offer_changes", "location", "notes_add")
# What a new extraction never erases unless it brings its own: what only the owner sets, and the Google listing (the
# address and the map point the ads centre on), which only a reading that looks the business up brings, and the places
# the owner said are not theirs (listing_rejected: a lookup never offers them again)
CARRIED = ("offers", "geo_model", "phone_hours", "notes", "trade", "listing", "listing_rejected")
# The business's website, read (`website`) or given and not read (`website_unread`): kept, the pair, when a new reading
# names no site of either kind (ruling F3) -- a reading of plain words never erases a site Admiry read
SITE_FIELDS = ("website", "website_unread")
# The platform the read site is built on (A3p-5 batch 2, Q1): it goes with the site it was read from (`carry`)
PLATFORM = "website_platform"
LOCATION_TEXT = ("hours", "phone", "phone_hours")
# What a change to the one location may set besides naming it by `id` (`_location` refuses anything else)
LOCATION_KEYS = ("geo_model", "areas", *LOCATION_TEXT)
MAX_SERVICES, MAX_OFFERS, MAX_NOTES, NOTE_MAX = 30, 10, 50, 500


def norm(text: str) -> str:
    """A name for comparing: lower case, letters and digits, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def slug(prefix: str, text: str) -> str:
    """`svc_water_heaters`: the rule consult._slug names the consultation's service options by (a test holds the
    two together)."""
    return f"{prefix}_" + re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40]


def clean_name(stored) -> str | None:
    """The business's name as a read gives it (the profile's `business.name`, marketing_state's, and the one
    plans.name_for vouches for): the stored value stripped when it is text with something in it, else None.
    update_business's patch bounded a value's size, not its shape, before it checked the name's, so a name stored
    before that may be a number or an object."""
    return stored.strip() if isinstance(stored, str) and stored.strip() else None


def own_site(knowledge: dict | None):
    """The owner's own website (A3p-5 batch 2, Q13): the site Admiry read (`website`), else the address the owner gave
    that could not be read (`website_unread`, batch 1's ruling R2-e); None for neither. Read wherever the owner's own
    site is meant -- the profile's `business.website`, the kit's facts and booking link, which places a lookup finds
    close -- while `steps()` and plans.name_for keep reading `website` alone: they mean a site Admiry read."""
    k = knowledge or {}
    return k.get("website") or k.get("website_unread")


def _host(url) -> str | None:
    """A website's host, lower case and `www.` aside, or None for what reads as no address: whether a new reading
    names the site a platform was read from (`carry`). The knowledge's website is text a model wrote, so text that is
    no address is none, never an error -- listings.same_site's rule for a site of its own, kept here because this
    module imports no `agent` module but places."""
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    try:
        host = urlsplit(text if "://" in text else f"//{text}").hostname
    except ValueError:
        return None
    return host.removeprefix("www.") if host else None


def service_id(service: dict) -> str:
    """A service's id: the one stored on it (it survives a rename), else the one its name gives."""
    return service.get("id") or slug("svc", service.get("name") or "")


def _readable(services) -> list[dict]:
    """The stored services the profile can read: objects with a name in text, and words, read as a service of that
    name. update_business's whole-list `services` patch bounds its size, not its shape, so a version may hold
    anything there; what has no name to read (a blank one included) is left out of the profile, of a by-id change
    and of what a new reading or a hand-off carries, never an error."""
    if not isinstance(services, list):
        return []
    out = []
    for s in services:
        if isinstance(s, str) and s.strip():
            out.append({"name": s.strip()})
        elif isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip():
            out.append(s)
    return out


def services(knowledge: dict | None) -> list[dict]:
    """The services on file as `{"id", "name"}`, the ones the profile can read: what the funnel groups leads by."""
    return [{"id": service_id(s), "name": s["name"]} for s in _readable((knowledge or {}).get("services"))]


def _listed(value) -> list[str]:
    """A stored list field as its entries in text, stripped, for the same reason: one text is one entry (an area, a
    reason, a phrase), not its letters; a blank, a number or an object is none."""
    entries = value if isinstance(value, list) else [value]
    return [v.strip() for v in entries if isinstance(v, str) and v.strip()]


def _lead_ids(knowledge: dict, goal: dict | None, session: dict | None) -> set[str]:
    """The services the plan leads with: the consultation's picks, by id or by the name of the option picked (a
    hand-off's session froze its options from the draft's knowledge, whose ids need not be the ones on file), and the
    goal's focus services by name."""
    session = session or {}
    choice = ((session.get("answers") or {}).get("service") or {}).get("choice")
    ids = set(choice if isinstance(choice, list) else [choice] if choice else [])
    shown = (session.get("options") or {}).get("service") or []
    names = {norm(o["label"]) for o in shown if o.get("id") in ids and isinstance(o.get("label"), str)}
    names |= {norm(name) for name in (goal or {}).get("focus_services") or []}
    return ids | {service_id(s) for s in _readable(knowledge.get("services")) if norm(s["name"]) in names}


def view(
    row: dict,
    version: int | None,
    knowledge: dict | None,
    *,
    goal: dict | None = None,
    session: dict | None = None,
    radius_miles: float | None = None,
    plan_offer: str | None = None,
) -> dict:
    """The profile: `row` is the business row, `session` the latest consultation as consult stores it (its service
    picks and their frozen options mark the services the plan leads with), `radius_miles` and `plan_offer` what that
    consultation holds (the caller reads them with consult.facts). An offer the plan carries and the profile does not
    is shown as coming from the plan, without an id: no change takes it by id until it is added by its text."""
    k = knowledge or {}
    listing = k.get("listing") if isinstance(k.get("listing"), dict) else {}
    lead = _lead_ids(k, goal, session)
    offers = [dict(o) for o in k.get("offers") or []]
    if plan_offer and all(norm(o.get("text") or "") != norm(plan_offer) for o in offers):
        offers.append(
            {
                "id": None,
                "text": plan_offer,
                "service_id": None,
                "valid_until": None,
                "terms": None,
                "from": "plan",
            }
        )
    has_point = listing.get("lat") is not None and listing.get("lng") is not None
    return {
        "business": {
            "id": row["id"],
            "name": clean_name(k.get("name")),
            "trade": k.get("trade"),
            "category": k.get("category"),
            "category_detail": k.get("category_detail"),
            "website": own_site(k),
            "google_listing": google_listing(k),
            "locale": row["locale"],
            "claim_state": row["claim_state"],
        },
        "locations": [
            {
                "id": LOCATION_ID,
                "geo_model": k.get("geo_model"),
                "address": listing.get("address"),
                "center": {"lat": listing["lat"], "lng": listing["lng"]} if has_point else None,
                "radius_miles": radius_miles,
                "areas": _listed(k.get("service_area")),
                "hours": k.get("hours"),
                "phone": k.get("phone"),
                "phone_hours": k.get("phone_hours"),
            }
        ],
        "services": [
            {
                "id": service_id(s),
                "name": s["name"],
                "catalog_id": s.get("catalog_id"),
                "price_hint": s.get("price_hint"),
                "lead": service_id(s) in lead,
                "intent_phrases": _listed(s.get("intent_phrases")),
            }
            for s in _readable(k.get("services"))
        ],
        "offers": offers,
        "facts": {
            "usp": _listed(k.get("usp")),
            "pricing_hints": _listed(k.get("pricing_hints")),
            "compliance_flags": _listed(k.get("compliance_flags")),
            "reviews_summary": k.get("reviews_summary"),
            "typical_customer_value_usd": k.get("typical_customer_value_usd"),
            "notes": list(k.get("notes") or []),
        },
        "knowledge_version": version,
    }


def _objects(value, key: str) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ToolError(f"{key} must be a list of objects")
    return value


def _words(value, refusal: str) -> str:
    """`value` stripped, when it is text with something in it; ToolError(refusal) otherwise."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError(refusal)
    return value.strip()


def _texts(change: dict, keys: tuple[str, ...]) -> dict:
    """The fields of `keys` a change sets, each text or null: the profile, the pages and the ads read them back as
    text. ToolError naming one that is neither."""
    fields = {key: change[key] for key in keys if key in change}
    for key, value in fields.items():
        if value is not None and not isinstance(value, str):
            raise ToolError(f"{key} must be text")
    return fields


def _unique(base: str, taken: set) -> str:
    """`base` when no entry holds it, else the first free `base_2`, `base_3`, ...: two different names can share a
    slug (a renamed entry keeps its id; the slug is cut at 40 characters)."""
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _service_fields(change: dict) -> dict:
    """What a change sets on a service, checked: its name, a price hint, the phrases people search for it with."""
    fields = _texts(change, ("price_hint",))
    if "name" in change:
        fields["name"] = _words(
            change["name"], "a service needs a name; to take one off, send its id with remove: true"
        )
    if "intent_phrases" in change:
        phrases = change["intent_phrases"]
        if phrases is not None and not (isinstance(phrases, list) and all(isinstance(p, str) for p in phrases)):
            raise ToolError("intent_phrases must be a list of phrases")
        fields["intent_phrases"] = list(phrases or [])
    return fields


def _services(current: list[dict], changes) -> list[dict]:
    out = [dict(s) for s in current]
    for change in _objects(changes, "service_changes"):
        sid = change.get("id")
        if sid is not None:
            at = next((i for i, s in enumerate(out) if service_id(s) == sid), None)
            if at is None:
                raise ToolError(f"no service with id {sid}")
            if change.get("remove"):
                out.pop(at)
                continue
            out[at] = {**out[at], **_service_fields(change), "id": sid}
            continue
        name = _words(change.get("name"), "a new service needs a name")
        same = next((s for s in out if norm(s["name"]) == norm(name)), None)  # a service is the same by its name
        if same is not None:
            raise ToolError(f"{name} is already on file as {service_id(same)}; change it by that id")
        fields = _service_fields(change)
        out.append(
            {
                "id": _unique(slug("svc", name), {service_id(s) for s in out}),
                "name": name,
                "intent_phrases": fields.get("intent_phrases", []),
                "price_hint": fields.get("price_hint"),
            }
        )
    if len(out) > MAX_SERVICES:
        raise ToolError(f"at most {MAX_SERVICES} services")
    return out


def _offer_fields(change: dict) -> dict:
    """What a change sets on an offer, checked: its text, the service it is for, until when, its terms."""
    fields = _texts(change, ("service_id", "valid_until", "terms"))
    if "text" in change:
        fields["text"] = _words(
            change["text"], "an offer needs its text; to take one off, send its id with remove: true"
        )
    return fields


def _offers(current: list[dict], changes) -> list[dict]:
    out = [dict(o) for o in current]
    for change in _objects(changes, "offer_changes"):
        oid = change.get("id")
        if oid is not None:
            at = next((i for i, o in enumerate(out) if o.get("id") == oid), None)
            if at is None:
                raise ToolError(f"no offer with id {oid}")
            if change.get("remove"):
                out.pop(at)
                continue
            out[at] = {**out[at], **_offer_fields(change)}
            continue
        text = _words(change.get("text"), "a new offer needs its text")
        same = next((o for o in out if norm(o.get("text") or "") == norm(text)), None)  # the same by its text
        if same is not None:
            raise ToolError(f"{text} is already on file as {same.get('id')}; change it by that id")
        fields = _offer_fields(change)
        out.append(
            {
                "id": _unique(slug("off", text), {o.get("id") for o in out}),
                "text": text,
                "service_id": fields.get("service_id"),
                "valid_until": fields.get("valid_until"),
                "terms": fields.get("terms"),
            }
        )
    if len(out) > MAX_OFFERS:
        raise ToolError(f"at most {MAX_OFFERS} offers")
    return out


def is_address(listing) -> bool:
    """Whether a stored listing is an address the owner placed (geocode.listing marks it `kind: address`) rather than
    the business's own Google listing: it names a place, never the business."""
    return isinstance(listing, dict) and listing.get("kind") == "address"


def placed(listing) -> bool:
    """Whether a stored listing is where the owner put the ads' centre on the map (walk fixes 1, final review A1): an
    address they placed (`is_address`), or the business's own Google listing moved to one (`_placed` marks it
    `placed`). A new reading keeps such a place (tools_start's `start`, consult.move_session): the owner's statement
    beats a lookup's best guess."""
    return is_address(listing) or (isinstance(listing, dict) and listing.get("placed") is True)


def business_listing(knowledge: dict | None) -> dict | None:
    """The business's own Google Maps listing, as the owner confirmed it (listings.link): a listing that is no address
    they placed and carries `confirmed`. None otherwise: no listing, an address, or a lookup's guess stored before
    owners confirmed listings (the migration listing_unconfirmed_dropped removes those)."""
    listing = (knowledge or {}).get("listing")
    if isinstance(listing, dict) and not is_address(listing) and listing.get("confirmed") is True:
        return listing
    return None


def google_listing(knowledge: dict | None) -> dict | None:
    """The business's own Google Maps listing as the owner reads it (D5, plan 2026-10-03-agent-listing-confirm): its
    name, its address and a link that opens it on Google Maps; None when the owner confirmed none."""
    listing = business_listing(knowledge)
    if listing is None:
        return None
    return {
        "name": listing.get("name"),
        "address": listing.get("address"),
        "maps_url": places.maps_url(listing.get("name"), listing.get("place_id")),
    }


def model_listing(listing):
    """A listing as a model may read it, the one rule every prompt follows (walk fixes 1, Task 4 round 2; plan
    2026-10-03-agent-listing-confirm): the business's own Google listing, as the owner confirmed it
    (`business_listing`), whole; an address the owner placed as `{"kind": "address", "address": …}` only -- its name
    is a street's, or a shop's at that address, never the business's, and its place id and rating are the map's.
    Anything else -- a lookup's guess nobody confirmed, or no listing at all -- None: the model never reads a place a
    lookup found before the owner picked it."""
    if is_address(listing):
        return {"kind": "address", "address": listing.get("address")}
    return business_listing({"listing": listing})


def for_model(knowledge: dict | None) -> dict:
    """The knowledge as a model reads it, whatever the prompt (the plan's draft, the answers to the owner's questions):
    a copy, its listing as `model_listing` shows it -- and no `listing` key at all when that shows none -- and without
    `listing_rejected`, place ids no prompt needs (final review A m4). The knowledge given is not changed."""
    seen = dict(knowledge or {})
    seen.pop("listing_rejected", None)
    if "listing" in seen:
        shown = model_listing(seen.pop("listing"))
        if shown is not None:
            seen["listing"] = shown
    return seen


def _placed(place: dict, knowledge: dict) -> dict:
    """The fields a place put on the map changes (walk fixes 1, Task 4 round 1): the listing -- the business's own
    Google listing on file (no `kind`) moved to the place, its address and point, with its name, place id and the rest
    kept, so the name stays vouched for (plans.name_for), and marked `placed` so a later reading knows the owner put it
    there (`placed`, final review A1); else the place's listing as it came, marked an address (geocode.listing) -- and
    the place's town (places.town) added to the areas when they do not hold it already."""
    on_file = knowledge.get("listing")
    if isinstance(on_file, dict) and on_file and not is_address(on_file):
        listing = {**on_file, **{key: place.get(key) for key in ("address", "lat", "lng")}, "placed": True}
    else:
        listing = place
    out: dict = {"listing": listing}
    town = places.town(place.get("address"))
    areas = _listed(knowledge.get("service_area"))  # an area stored as text is one area, not its letters
    if town and all(norm(area) != norm(town) for area in areas):
        out["service_area"] = [*areas, town]
    return out


def _location(change, knowledge: dict) -> dict:
    """A change to the one location, as the knowledge's own fields. An `address` reaches here with the `listing` the
    command found for it on the map (update_business; the consultation's answer adopts a place the same way), and the
    location takes the place (`_placed`). Areas the change names still replace the knowledge's, as given: the place's
    town is not added to them."""
    if not isinstance(change, dict):
        raise ToolError("location must be an object")
    if "id" in change and change["id"] != LOCATION_ID:
        raise ToolError(f"this business has one location, {LOCATION_ID}")
    if "radius_miles" in change:
        raise ToolError("the radius is part of the plan: change it there, not on the profile")
    unknown = set(change) - {"id", *LOCATION_KEYS, "address", "listing"}
    if unknown:
        raise ToolError(f"unknown location fields: {sorted(unknown)}")
    if "address" in change and "listing" not in change:
        raise ToolError("address is placed by the command, not here")
    out: dict = {}
    # the place the command's prepare put on the map for `address` (tools_business.update_business)
    if "listing" in change:
        out.update(_placed(change["listing"], knowledge))
    if "geo_model" in change:
        if change["geo_model"] not in GEO_MODELS:
            raise ToolError("geo_model must be storefront or service_area")
        out["geo_model"] = change["geo_model"]
    if "areas" in change:
        areas = change["areas"]
        if not isinstance(areas, list) or not all(isinstance(a, str) and a.strip() for a in areas):
            raise ToolError("areas must be a list of place names")
        out["service_area"] = [a.strip() for a in areas]
    out.update(_texts(change, LOCATION_TEXT))
    return out


def apply(knowledge: dict, changes: dict, *, now: datetime | None = None) -> dict:
    """The knowledge with by-id changes applied (update_business): `service_changes` and `offer_changes` (an item
    with an id changes that one, or removes it with `remove: true`; one without adds), `location` (the one
    location's fields) and `notes_add` (sentences of the owner's, kept and given to every later plan)."""
    out = {**knowledge, "schema": SCHEMA}
    if "service_changes" in changes:
        out["services"] = _services(_readable(knowledge.get("services")), changes["service_changes"])
    if "offer_changes" in changes:
        out["offers"] = _offers(knowledge.get("offers") or [], changes["offer_changes"])
    if "location" in changes:
        out.update(_location(changes["location"], knowledge))
    if "notes_add" in changes:
        added = changes["notes_add"]
        if not isinstance(added, list) or not all(isinstance(n, str) and n.strip() for n in added):
            raise ToolError("notes_add must be a list of sentences")
        at = (now or datetime.now(timezone.utc)).isoformat()
        notes = list(knowledge.get("notes") or []) + [
            {"text": n.strip()[:NOTE_MAX], "source": "owner", "at": at} for n in added
        ]
        out["notes"] = notes[-MAX_NOTES:]
    return out


def carry(previous: dict | None, new: dict) -> dict:
    """A new knowledge (a new extraction, or a hand-off's copy of a draft's) with what it cannot know kept from the
    version before: a service's stored id (matched by name), the offers, the geo model, the hours the phone is
    answered, the notes, the trade, and the listing -- the business's own Google listing the owner confirmed, or the
    address they placed, with the map point the ads centre on. No reading brings one (what a lookup finds waits for the
    owner's pick, listings; a hand-off's copy brings only what the visitor confirmed or placed), so a new reading,
    `start`'s too, keeps the one on file (a place the owner put on the map is adopted again by the caller, `placed`:
    final review A1) -- and the places the owner said are not theirs
    (`listing_rejected`). Each of these only when `new` does not hold it already. The website too (SITE_FIELDS, ruling
    F3): the version before's `website` and `website_unread` when `new` names no site of either kind, so a business
    whose site was read and that starts over with plain words is offered it as one tap again (consult._unread_site);
    a reading that names one, read or not, replaces both. The platform the read site is built on (PLATFORM, A3p-5
    batch 2 Q1) goes with the site: a new reading that brings none keeps the version before's when it names no site,
    or names that same site as its `website` without reading a page of it; another site, read or not, never inherits
    it (Q13: a site never read has no platform).
    `new` itself when the version before held none of these; else its services as the profile reads them."""
    if not previous:
        return new
    ids = {norm(s["name"]): s["id"] for s in _readable(previous.get("services")) if s.get("id")}
    kept = {key: previous[key] for key in CARRIED if key in previous and key not in new}
    if not any(new.get(key) for key in SITE_FIELDS):
        kept |= {key: previous[key] for key in (*SITE_FIELDS, PLATFORM) if previous.get(key)}
    elif PLATFORM not in new and previous.get(PLATFORM):
        site = _host(new.get("website"))
        if site and site == _host(previous.get("website")):
            kept[PLATFORM] = previous[PLATFORM]
    if not ids and not kept:
        return new
    # read as the profile reads: a hand-off brings the draft's stored version, which may hold any shape
    services = [
        {**s, "id": ids[norm(s["name"])]} if norm(s["name"]) in ids else s for s in _readable(new.get("services"))
    ]
    return {**new, **kept, "services": services, "schema": SCHEMA}
