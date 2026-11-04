"""The business's own Google Maps listing (plan 2026-10-03-agent-listing-confirm): which places a lookup found are
close enough to ask the owner about (a fixed rule: the same website, the same phone, or the same name in the owner's
towns -- anywhere, when the owner named none), the places offered and waiting for the owner's pick (`listing_offer`),
and what linking or unlinking one does to the knowledge. Nothing a lookup finds is the business's listing until the
owner picks it: the knowledge's `listing` is a place the owner confirmed (`confirmed: true`) or an address they placed
(`kind: address`, geocode.listing). The model never reads an offered place."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from . import booking_hosts, business, config, consult, geocode, places, profile

MAX_OFFERED = 3
SEARCHED = 5  # places asked of Google per lookup
OFFER_TTL = timedelta(hours=24)
MAX_REJECTED = 20
QUERY_CHARS = 200
# hosts that are no one business's: a small business's website is often its page on one of them (same_site) -- a
# social or listing site, or a booking service (booking_hosts, the one list the check and the page's booking link read
# too: A3p-5 batch 2 ruling R1-e), where an owner's address is their booking page among other businesses'
SHARED_HOSTS = frozenset(
    {
        "facebook.com",
        "m.facebook.com",
        "instagram.com",
        "yelp.com",
        "linktr.ee",
        "google.com",
        "sites.google.com",
        "g.page",
        "maps.app.goo.gl",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "youtube.com",
        "nextdoor.com",
        "linkedin.com",
        *booking_hosts.BOOKING_HOSTS,
    }
)


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


# words a business's legal name carries that say nothing of which business it is
LEGAL_WORDS = _wordset("the and of llc inc co corp corporation ltd pllc pc pa lp llp company")
# words that say what a business is, never which one: a name made only of these matches no place by name
GENERIC_WORDS = _wordset(
    """dental dentist dentists dentistry orthodontic orthodontics orthodontist family practice clinic clinics office
    offices care center centre centers group services service law lawyer lawyers attorney attorneys firm legal
    immigration injury accident plumbing plumber plumbers roofing roofer roofers hvac heating cooling air electric
    electrical electrician home homes repair repairs cleaning cleaners landscaping construction contractor contractors
    studio salon spa medical health associates partners solutions pro pros"""
)

LINKED = "Linked to {name} on Google Maps."
CENTRED = " The ads centre on {address}."
UNLINKED = "Unlinked {name}: Admiry will not use it, or offer it again."
NO_CENTRE = " Give the business's address, or its city and ZIP code, and the ads will centre there."
# ... when the consultation will ask where the ads centre, but another question comes first (final review M2): NO_CENTRE
# would read as a request, and the next question is a different one
NO_CENTRE_ASKED = " Admiry will ask where the ads should centre."
# ... when no consultation will ask where the ads centre (none open, or the plan is built: plan
# 2026-10-03-agent-listing-not-mine-words, Task 2 round 1, the controller's ruling): what a new version centres on
NO_CENTRE_LATER = (
    " Until the right place is linked, a new version of the plan centres on the areas the business serves."
)
NO_PLACE = " A new version of the plan needs a place to centre on: link the right one first."
RUNNING = " The ads running now keep their area until a new version of the plan is launched."
# ... and, after the owner's own words unlinked it (plan 2026-10-03-agent-listing-not-mine-words, D3), how to link the
# right place
LINK_AGAIN = " To link the right one, use Find it in Settings, or give its name and address to your assistant."
# ... when the same words also named a change to the plan, which the unlink took the place of (change_plan: Task 1 fix
# round 1, the controller's ruling)
NOTHING_ELSE = " Nothing else was changed: ask for the other change again."
NONE_NOTED = "Noted: none of these is the business, and Admiry will not offer them again."
# ... when the places were no longer offered (a day passed): none remembered, so nothing is promised (Task 3 round 1)
NONE_GONE = "Noted: none of these is the business."
# The consultation's words that picked no place, twice: nothing linked and nothing remembered -- the owner did not say
# none of these is the business (final review A2/B I2)
UNCLEAR_NONE = (
    "I could not tell which place is the business, so none is linked. You can link it later in Settings, or tell your "
    "assistant."
)
NOTHING_CLOSE = (
    "I found nothing on Google Maps close to that. Check the name and the address, or leave the business unlinked."
)
NOTHING_LINKED = "No Google Maps listing is linked to this business."
OFFER_GONE = "That choice is no longer open: look the business up on Google Maps again."


def _words(name: str | None) -> list[str]:
    return [w for w in profile.norm(name or "").split() if w not in LEGAL_WORDS]


def _distinctive(name: str | None, ignore: frozenset[str] | set[str] = frozenset()) -> set[str]:
    return {w for w in _words(name) if w not in GENERIC_WORDS and w not in ignore}


def names_match(a: str | None, b: str | None, *, towns: list[str] | tuple[str, ...] = ()) -> bool:
    """Two names of one business: the same words, two or more of them (legal words aside: "Bright Smile Dental, LLC"
    is "Bright Smile Dental"), or two or more distinctive words in common -- words that are not GENERIC_WORDS, which
    say what a business is and not which one, nor words of `towns`, the places the owner named (two businesses named
    for one town are not one): "Bright Smile Dental" and "Bright Smile Dental Care" share "bright smile"; "Dental
    Practice" shares nothing with any name; "Smith Dental" matches only "Smith Dental"; in Palo Alto, "Palo Alto
    Dental Practice" and "Palo Alto Family Dentistry" share nothing, while "Palo Alto Dental" is "Palo Alto Dental"."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa == wb and len(wa) >= 2:
        return True
    ignore = _town_words(towns)
    return len(_distinctive(a, ignore) & _distinctive(b, ignore)) >= 2


def _town_words(towns: list[str] | tuple[str, ...]) -> set[str]:
    return {w for town in towns for w in profile.norm(town).split()}


def _site(url: str | None) -> str | None:
    """A website as `same_site` compares it: its host, `www.` aside -- and, on a SHARED_HOSTS host, its whole page too
    (the path, lower case, without a trailing "/", and the query string). None for what is no website: the knowledge's
    website is text a model wrote, so text that cannot be read as a URL ("[none]", "http://[x") is none, never an
    error."""
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    try:
        parts = urlsplit(text if "://" in text else f"//{text}")
        host, path, query = parts.hostname, parts.path, parts.query
    except ValueError:
        return None
    if not host:
        return None
    host = host.removeprefix("www.")
    if host not in SHARED_HOSTS:
        return host
    page = path.lower().rstrip("/")
    if not page:
        return None
    return f"{host}{page}?{query}" if query else f"{host}{page}"


def same_site(a: str | None, b: str | None) -> bool:
    """The same website: the same host, `www.` aside. A shared host (SHARED_HOSTS: a business's Facebook, Instagram or
    Yelp page, or its Calendly booking page, is a page on a site that is no one business's) is one site only with the
    same whole page -- path and query -- so two businesses' pages there are not one site and a bare "facebook.com" is
    none."""
    sa, sb = _site(a), _site(b)
    return bool(sa) and sa == sb


def _digits(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone) if isinstance(phone, str) else ""
    return digits[-10:] if len(digits) >= 10 else None


def same_phone(a: str | None, b: str | None) -> bool:
    """The same phone: the same last ten digits (fewer than ten never match)."""
    da, db = _digits(a), _digits(b)
    return bool(da) and da == db


def towns_of(knowledge: dict, address: str | None = None) -> list[str]:
    """Where the owner said the business is: its service areas, then the town and ZIP codes of an address an
    assistant passed (places.town reads Google's US format; a ZIP code is the five digits after a state, never a street
    number)."""
    towns = list(profile._listed(knowledge.get("service_area")))
    if address:
        town = places.town(address)
        towns += [town] if town else []
        towns += re.findall(r"\b[A-Z]{2}\s+(\d{5})\b", address)
    return towns


def in_towns(address: str | None, towns: list[str]) -> bool:
    """Whether a place's address names one of `towns` (a town, or a ZIP code); True when the owner named none."""
    if not towns:
        return True
    text = f" {profile.norm(address or '')} "
    return any(f" {profile.norm(t)} " in text for t in towns if profile.norm(t))


def close(
    found: places.Candidate, *, name: str | None, website: str | None, phone: str | None, towns: list[str]
) -> bool:
    """Whether a place a lookup found is close enough to ask the owner about (D2): the business's website or phone,
    anywhere; or its name, in one of the towns the owner named."""
    if same_site(found.website, website) or same_phone(found.phone, phone):
        return True
    return names_match(found.name, name, towns=towns) and in_towns(found.address, towns)


def words_cover(name: str | None, words: str | None, *, towns: list[str] | tuple[str, ...] = ()) -> bool:
    """Whether the owner's own words name a place: they are its name (legal words aside, two words or more), or every
    distinctive word of its name is in them, and it has one -- the words of `towns`, the business's own, aside, as
    names_match reads them: typing "Palo Alto Dental" in Palo Alto names no "Palo Alto Family Dentistry" (final review
    A m1). So is a place whose whole name, two words or more and with a word that says which business it is (a
    town's too), stands in them as a phrase -- consecutive whole words: "Palo Alto Dental, Palo Alto" names the
    "Palo Alto Dental" in Palo Alto, as the Settings form's "its name and address" asks for, and "Palo Alto dentist"
    names no "Palo Alto Family Dentistry" (final review F6 residual)."""
    mine, theirs = _words(words), _words(name)
    if mine == theirs and len(theirs) >= 2:
        return True
    if len(theirs) >= 2 and _distinctive(name) and _has_phrase(mine, theirs):
        return True
    distinctive = _distinctive(name, _town_words(towns))
    return bool(distinctive) and distinctive <= set(mine)


def _has_phrase(words: list[str], phrase: list[str]) -> bool:
    """Whether `phrase` stands in `words` as consecutive whole words."""
    return any(words[i : i + len(phrase)] == phrase for i in range(len(words) - len(phrase) + 1))


def numbered(found: list[places.Candidate]) -> list[dict]:
    """The places to offer, in Google's order, each once, at most MAX_OFFERED, with the ids the owner picks by."""
    out: list[dict] = []
    for f in found:
        if not f.place_id or any(c["place_id"] == f.place_id for c in out):
            continue
        out.append(
            {
                "id": f"gm_{len(out) + 1}",
                "place_id": f.place_id,
                "name": f.name,
                "address": f.address,
                "lat": f.lat,
                "lng": f.lng,
            }
        )
        if len(out) == MAX_OFFERED:
            break
    return out


def label(candidate: dict) -> str:
    """An offered place as the owner reads it (places.label; the consultation's options read the same)."""
    return places.label(candidate["name"], candidate["address"])


def _shown_as(candidate: dict, text: str | None) -> bool:
    """Whether `candidate` is the place the owner saw as `text` (its `label`); True when no label was given."""
    return text is None or label(candidate) == text


def found_say(candidates: list[dict]) -> str:
    listed = "; ".join(f"{n}. {label(c)}" for n, c in enumerate(candidates, start=1))
    return f"On Google Maps I found {listed}. Which one is the business? Or none of these."


def _remember(rejected, place_ids) -> list[str]:
    out = [p for p in (rejected or []) if isinstance(p, str)]
    for place_id in place_ids:
        if place_id and place_id not in out:
            out.append(place_id)
    return out[-MAX_REJECTED:]


def link(knowledge: dict, chosen: dict, *, now: datetime | None = None) -> dict:
    """The knowledge with `chosen` (an offered place) as the business's own listing, confirmed by the owner (D4): its
    place id, name, address and point -- the address and point kept from where the owner put the ads' centre, when
    they did (profile.placed). The business's name becomes the place's when the two do not match (names_match). A
    listing linked before is remembered as not this business; the chosen place no longer is."""
    on_file = knowledge.get("listing") if isinstance(knowledge.get("listing"), dict) else None
    listing = {
        "place_id": chosen["place_id"],
        "name": chosen["name"],
        "address": chosen["address"],
        "lat": chosen.get("lat"),
        "lng": chosen.get("lng"),
        "source": "Google Maps",
        "confirmed": True,
        "confirmed_at": (now or datetime.now(UTC)).isoformat(),
    }
    if profile.placed(on_file):
        listing.update({key: on_file.get(key) for key in ("address", "lat", "lng")}, placed=True)
    out = {**knowledge, "listing": listing}
    if not names_match(profile.clean_name(knowledge.get("name")), chosen["name"]):
        out["name"] = chosen["name"]
    old = on_file.get("place_id") if on_file and not profile.is_address(on_file) else None
    rejected = _remember(knowledge.get("listing_rejected"), [old] if old != chosen["place_id"] else [])
    out["listing_rejected"] = [p for p in rejected if p != chosen["place_id"]]
    return out


def unlink(knowledge: dict) -> dict:
    """The knowledge without the business's own listing (D5), its place remembered as not this business. Where the
    owner put the ads' centre stays, as an address they placed; a centre that was the listing's own goes with it."""
    on_file = knowledge.get("listing")
    out = {key: value for key, value in knowledge.items() if key != "listing"}
    if profile.placed(on_file):
        out["listing"] = {
            "address": on_file.get("address"),
            "lat": on_file.get("lat"),
            "lng": on_file.get("lng"),
            "source": "Google Maps",
            "kind": "address",
        }
    old = on_file.get("place_id") if isinstance(on_file, dict) and not profile.is_address(on_file) else None
    out["listing_rejected"] = _remember(knowledge.get("listing_rejected"), [old])
    return out


# ---------------------------------------------------------------- the offer (listing_offer)


def save_offer(conn: psycopg.Connection, biz: str, candidates: list[dict]) -> None:
    """The places waiting for the owner's pick, replacing any before; none closes the offer."""
    if not candidates:
        drop_offer(conn, biz)
        return
    conn.execute(
        "INSERT INTO listing_offer (business_id, candidates) VALUES (%s, %s) ON CONFLICT (business_id) "
        "DO UPDATE SET candidates = EXCLUDED.candidates, created_at = now()",
        (biz, Jsonb(candidates)),
    )


def offer(conn: psycopg.Connection, biz: str) -> list[dict]:
    """The places waiting for the owner's pick, or [] (none, or offered more than OFFER_TTL ago)."""
    r = conn.execute(
        "SELECT candidates FROM listing_offer WHERE business_id = %s AND created_at > now() - %s",
        (biz, OFFER_TTL),
    ).fetchone()
    return list(r["candidates"]) if r else []


def drop_offer(conn: psycopg.Connection, biz: str) -> None:
    conn.execute("DELETE FROM listing_offer WHERE business_id = %s", (biz,))


def move_offer(conn: psycopg.Connection, from_biz: str, to_biz: str) -> None:
    """A hand-off landing (tools_start): the draft's offer goes with its session, replacing the destination's. Moved
    to the business it is on already, it stays."""
    if from_biz == to_biz:
        return
    if conn.execute("SELECT 1 FROM listing_offer WHERE business_id = %s", (from_biz,)).fetchone() is None:
        return
    drop_offer(conn, to_biz)
    conn.execute("UPDATE listing_offer SET business_id = %s WHERE business_id = %s", (to_biz, from_biz))


# ---------------------------------------------------------------- looking up (calls Google: prepare only)


def lookup(
    conn: psycopg.Connection,
    settings: config.Settings,
    knowledge: dict,
    facts: dict,
    *,
    business_id: str | None,
    rejected: list[str],
) -> list[dict]:
    """`start`'s lookup (D1): with the business's name -- the one an assistant passed, else the reading's -- and where
    it is (the known-fact address, else the first service area), the places close to it (D2), minus those the owner
    said are not theirs. The business's website is the owner's own, read or not (profile.own_site: A3p-5 batch 2, Q13).
    No name, no key or no lookups left today: [] without a call. One call is one row."""
    name = facts.get("business_name") or profile.clean_name(knowledge.get("name"))
    if not name or not settings.places_api_key or not geocode.lookups_left(conn, settings, business_id):
        return []
    where = facts.get("address") or next(iter(profile._listed(knowledge.get("service_area"))), "")
    words = f"{name} {where}".strip()[:QUERY_CHARS]
    found = places.search(words, settings.places_api_key, max_results=SEARCHED, with_identity=True)
    geocode.record_lookup(conn, business_id, words, found[0] if found else None)
    towns = towns_of(knowledge, facts.get("address"))
    rule = {"name": name, "website": profile.own_site(knowledge), "phone": knowledge.get("phone"), "towns": towns}
    return numbered([f for f in found if f.place_id not in set(rejected or []) and close(f, **rule)])


def find(
    conn: psycopg.Connection, settings: config.Settings, words: str, *, business_id: str, knowledge: dict
) -> list[dict]:
    """The owner's own search (D5): the places whose name the owner's words cover (the business's own towns aside:
    `words_cover`), or with the business's website (the owner's own, read or not: profile.own_site, A3p-5 batch 2 Q13)
    or phone. A place the owner once said is not theirs is offered too:
    their search is a newer word than that, and linking it forgets it (`link`; final review A2/B I2) -- only start's
    lookup, which the owner did not ask for, leaves those out. A spent day raises geocode.NoLookups; one call is one
    row."""
    text = (words or "").strip()[:QUERY_CHARS]
    if not text or not geocode.can_place(settings):
        return []
    if not geocode.lookups_left(conn, settings, business_id):
        raise geocode.NoLookups(geocode.NO_LOOKUPS)
    found = places.search(text, settings.places_api_key, max_results=SEARCHED, with_identity=True)
    geocode.record_lookup(conn, business_id, text, found[0] if found else None)
    towns = towns_of(knowledge)
    keep = [
        f
        for f in found
        if same_site(f.website, profile.own_site(knowledge))
        or same_phone(f.phone, knowledge.get("phone"))
        or words_cover(f.name, text, towns=towns)
    ]
    return numbered(keep)


# ---------------------------------------------------------------- the owner's word (in the caller's transaction)


def _knowledge(conn: psycopg.Connection, biz: str) -> dict:
    latest = business.latest_knowledge(conn, biz)
    return latest[1] if latest else {}


def confirm(conn: psycopg.Connection, biz: str, choice_id: str, *, label: str | None = None) -> dict | None:
    """The owner's pick: the place offered under `choice_id` linked (`link`, a new version, source owner) and the
    offer closed. The listing linked, or None when this business's open offer holds no such id -- or, with `label` (the
    place as the owner saw it: a consultation's options are frozen when it begins, while every find replaces the offer
    and numbers it from gm_1 again), when the place under that id now is another (final review A1): nothing linked, the
    offer left as it is."""
    chosen = next((c for c in offer(conn, biz) if c["id"] == choice_id), None)
    if chosen is None or not _shown_as(chosen, label):
        return None
    linked = link(_knowledge(conn, biz), chosen)
    business.add_knowledge(conn, biz, linked, source="owner")
    drop_offer(conn, biz)
    return linked["listing"]


def reject_offer(conn: psycopg.Connection, biz: str) -> int:
    """ "None of these": every place offered remembered as not this business, the offer closed. How many were
    offered (0: nothing was open, nothing written)."""
    shown = offer(conn, biz)
    drop_offer(conn, biz)
    if not shown:
        return 0
    knowledge = _knowledge(conn, biz)
    rejected = _remember(knowledge.get("listing_rejected"), [c["place_id"] for c in shown])
    business.add_knowledge(conn, biz, {**knowledge, "listing_rejected": rejected}, source="owner")
    return len(shown)


def unlink_listing(conn: psycopg.Connection, biz: str) -> dict | None:
    """ "Not my business": the linked listing removed (`unlink`, a new version, source owner). The listing that was
    linked, or None when none is."""
    knowledge = _knowledge(conn, biz)
    listing = profile.business_listing(knowledge)
    if listing is None:
        return None
    business.add_knowledge(conn, biz, unlink(knowledge), source="owner")
    return listing


def unlink_and_say(conn: psycopg.Connection, biz: str, *, asks_next: bool = False) -> tuple[dict | None, str]:
    """The owner's "not my business", however it was said (Settings' button, google_listing null, the owner's own
    words): the linked listing removed and remembered, a centre that was its own dropped from an open consultation
    (consult.forget_centre: only then -- a placed centre has the same shape), and the sentences that say so. (None,
    NOTHING_LINKED) when nothing is linked.
    What the centre sentence offers is a way every caller's channel can take (Task 2 round 1, the controller's
    ruling): an address only when a consultation will ask where the ads centre next (NO_CENTRE); otherwise what a new
    version of the plan centres on -- the areas the business serves (NO_CENTRE_LATER), or nothing until the right
    place is linked (NO_PLACE). A caller that goes on to ask the consultation's next question (`asks_next`: `answer`)
    asks for the address only when that question is where the ads centre; with another first, the sentence says the
    question will come (NO_CENTRE_ASKED), for an address asked for and then a different question reads as a request
    the next words cannot answer (final review M2)."""
    unlinked = unlink_listing(conn, biz)
    if unlinked is None:
        return None, NOTHING_LINKED
    say = UNLINKED.format(name=unlinked["name"])
    if not unlinked.get("placed"):
        if consult.forget_centre(conn, biz):
            say += NO_CENTRE_ASKED if asks_next and not _centre_is_next(conn, biz) else NO_CENTRE
        elif profile._listed(_knowledge(conn, biz).get("service_area")):
            say += NO_CENTRE_LATER
        else:
            say += NO_PLACE
    if has_approved_plan(conn, biz):
        say += RUNNING
    return unlinked, say


def _centre_is_next(conn: psycopg.Connection, biz: str) -> bool:
    """Whether the open consultation's next question is where the ads centre."""
    nxt = consult.next_question(consult.open_for(conn, biz))
    return nxt is not None and nxt["question"]["id"] == "location"


def has_approved_plan(conn: psycopg.Connection, biz: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM plan WHERE business_id = %s AND status = 'approved' LIMIT 1", (biz,)).fetchone()
        is not None
    )
