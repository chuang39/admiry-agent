"""The industry site's intake (A3p-6 batch 2 Q1-Q4; spec T3, T15, T19): which family of site a business is -- and which
trade of it --, the site's facts filled from what Admiry already has (the profile, the Google listing the owner
confirmed, the consultation) with the required ones still missing named, what a patch of facts may carry, and the
owner's own answers kept in `page.site_draft` between builds.

One truth: a fact the profile holds -- the name, the phone, the address, the towns, the hours, the services -- is
written to the profile by `merge` (the plan reads it, `get_business_profile` shows it) and read again by `facts` at
every build, so a change to the profile reaches the site; only the extras the profile has no place for (a warranty,
the years in business, a review, the dentist, which services lead) are the draft's -- and an address typed over a
Google listing the owner confirmed, which is never unlinked -- and the draft's own facts lie over what the profile
says. Nothing here calls a model."""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from urllib.parse import urlsplit

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from .. import business, config, consult, geocode, landing, page_builder, places, plans, profile, trades
from . import spec as spec_mod
from .families import home_services
from .spec import Facts, Service

# The four facts a site cannot be built without, in the order they are asked (T15). `place` is met by a storefront's
# address, by one town -- the address's own, or a town the business serves (a radius or a ZIP code is none) -- or by an
# address with a street number (Task 4 fix round 1); the others by a name, a phone and a service.
REQUIRED = ("name", "phone", "place", "services")
QUESTIONS = {
    "name": "What is the business called?",
    "phone": "What phone number should the site show?",
    "place": "Where is the business? Its address, or the towns it serves.",
    "services": "What do you offer? Name the main services, one per line.",
}
# The keys a patch of facts may carry: the names an assistant reads (the contract's `values.site_facts`). `place` is the
# answer to the place question -- an address, or the towns the business serves -- which `check` reads as `read_place`
# does; it is no field of the document itself
SITE_FACTS = (
    "name",
    "phone",
    "place",
    "address",
    "service_area",
    "services",
    "hours",
    "about",
    "email",
    "social",
    "licence",
    "years",
    "insured",
    "warranty",
    "free_estimate",
    "emergency",
    "financing",
    "languages",
    "reviews",
    "dentist",
    "new_patients",
    "insurance_plans",
    "in_office_plan",
    "emergency_line",
)
# The industry's own facts the homepage's site card asks once the site is built (Q4's extras, the founder's ruling 2 of
# 2026-10-06), in Q4's order, every one skippable: `{id, label, input}`, `input` text, textarea, yesno (true or false),
# list (lines or commas), review (what a customer said, their first name and where they left it: a list of one) or
# group (one fact of a few parts: the dentist, the social pages). A review's and a group's parts are its `fields`, those
# `needed` asked for once any part is given; a `type` is the field's own (`email`), and a `link` is an address read as
# https. The labels are the owner's words: the founder approves them (Task 7's report lists them).
_REVIEW = (
    {"id": "text", "label": "What they said", "needed": True},
    {"id": "first_name", "label": "Their first name", "needed": True},
    {"id": "source", "label": "Where they left it, such as Google", "needed": True},
)
_SOCIAL_PAGES = (
    {"id": "facebook", "label": "Facebook page", "type": "link"},
    {"id": "instagram", "label": "Instagram page", "type": "link"},
)
_PAGES = {"id": "social", "label": "Your Facebook or Instagram page", "input": "group", "fields": _SOCIAL_PAGES}
EXTRAS: dict[str, tuple[dict, ...]] = {
    "home_services": (
        {"id": "hours", "label": "Your opening hours", "input": "textarea"},
        {"id": "about", "label": "A few words about your business", "input": "textarea"},
        {"id": "licence", "label": "Your licence number", "input": "text"},
        {"id": "years", "label": "Years in business", "input": "text"},
        {"id": "insured", "label": "Are you insured?", "input": "yesno"},
        {"id": "warranty", "label": "Your warranty", "input": "text"},
        {"id": "free_estimate", "label": "Your free estimate, if you give one", "input": "text"},
        {"id": "emergency", "label": "Emergency service, if you offer it", "input": "text"},
        {"id": "financing", "label": "Financing, if you offer it", "input": "text"},
        {"id": "reviews", "label": "A review from a customer", "input": "review", "fields": _REVIEW},
        _PAGES,
        {"id": "email", "label": "An e-mail address for customers", "input": "text", "type": "email"},
    ),
    "dental": (
        {"id": "hours", "label": "Your opening hours", "input": "textarea"},
        {"id": "about", "label": "A few words about your practice", "input": "textarea"},
        {
            "id": "dentist",
            "label": "The dentist",
            "input": "group",
            "fields": (
                {"id": "name", "label": "Name", "needed": True},
                {"id": "credentials", "label": "Credentials, such as DDS"},
            ),
        },
        {"id": "new_patients", "label": "What new patients should know", "input": "text"},
        {"id": "insurance_plans", "label": "The insurance plans you take", "input": "list"},
        {"id": "in_office_plan", "label": "Your own savings plan, if you have one", "input": "text"},
        {"id": "emergency_line", "label": "Emergency care, if you offer it", "input": "text"},
        {"id": "reviews", "label": "A review from a patient", "input": "review", "fields": _REVIEW},
        _PAGES,
        {"id": "email", "label": "An e-mail address for patients", "input": "text", "type": "email"},
    ),
}
# What the profile holds, so what `merge` writes there; every other fact of a patch is the draft's
PROFILE_FACTS = ("name", "phone", "address", "service_area", "hours", "services")
SOCIAL_KEYS = ("facebook", "instagram", "tiktok", "yelp", "other")
MAX_TEXT, MAX_REVIEW, MAX_REVIEWS, MAX_LIST = 400, 600, 6, 30
MAX_PATCH = 8000  # characters of JSON in one patch of facts, whatever each field's own limit allows
PLACE_NOT_TEXT = "place must be text: the address, or the towns the business serves."
TOO_MUCH = "That is more than a site's facts take; send fewer at a time."
MAX_SLUG = 40
YEARS = (1, 150)

_SOCIAL_SITES = {"facebook.com": "facebook", "instagram.com": "instagram", "tiktok.com": "tiktok", "yelp.com": "yelp"}
_STREET_NUMBER = re.compile(r"^\s*\d+(?:-\d+)?[A-Za-z]?\s+\S")  # "123 Main St", "12-14 Oak Ln"; not "5th Avenue"
_TOWNS = re.compile(r"\s*(?:[,;\n&]|\band\b)\s*", re.IGNORECASE)
# What follows the town in "Charlotte, NC 28202": a state, alone or with its ZIP code; and a ZIP code, alone
_STATE_TAIL = re.compile(r"[A-Za-z]{2}(?:\s+\d{5}(?:-\d{4})?)?")
_ZIP = re.compile(r"\d{5}(?:-\d{4})?")
# A state written after the town in one part, alone or with its ZIP code ("Charlotte NC", "Charlotte NC 28203"):
# capitals, so a town's own last word ("Mint Hill") is never taken for one
_TRAILING_STATE = re.compile(r"\s+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$")
# What a part of an address after its street is when it names no town: the country, a ZIP code on its own, a unit
# ("Suite 4", "Apt 5B", "#12", "Fl 3"). "fl" is a floor only before something that is no ZIP code: "fl 33127" and a bare
# "Fl." are Florida (`_BARE_STATE`)
_NOT_A_TOWN = re.compile(
    r"USA|United States|\d{5}(?:-\d{4})?|#\s*[\w-]+"
    r"|(?:suite|ste|apt|apartment|unit|bldg|building|floor|room|rm)\.?\s+[\w-]+"
    r"|fl\.?\s+(?!\d{5}(?:-\d{4})?$)[\w-]+",
    re.IGNORECASE,
)
# What stands at the end of a part after the town in it: a country, a ZIP code ("Charlotte NC 28203 USA", "Miami 33127")
_COUNTRY_TAIL = re.compile(r"\s+(?:USA|United States)$", re.IGNORECASE)
_ZIP_TAIL = re.compile(r"\s+\d{5}(?:-\d{4})?$")
# A part that is a state alone -- in any case, with or without its period ("NC", "fl", "Fl.") -- once its ZIP code and
# country are off: a state is no town
_BARE_STATE = re.compile(r"[A-Za-z]{2}\.?")
# ... and one spelled out, in any case (the fifty names and the District of Columbia): a state part, not a town, when a part
# stands before it ("Miami, Florida 33127"); alone it may be the town ("123 Broadway, New York"), so it stays
_STATE_NAMES = frozenset(
    name.lower()
    for name in (
        "Alabama",
        "Alaska",
        "Arizona",
        "Arkansas",
        "California",
        "Colorado",
        "Connecticut",
        "Delaware",
        "Florida",
        "Georgia",
        "Hawaii",
        "Idaho",
        "Illinois",
        "Indiana",
        "Iowa",
        "Kansas",
        "Kentucky",
        "Louisiana",
        "Maine",
        "Maryland",
        "Massachusetts",
        "Michigan",
        "Minnesota",
        "Mississippi",
        "Missouri",
        "Montana",
        "Nebraska",
        "Nevada",
        "New Hampshire",
        "New Jersey",
        "New Mexico",
        "New York",
        "North Carolina",
        "North Dakota",
        "Ohio",
        "Oklahoma",
        "Oregon",
        "Pennsylvania",
        "Rhode Island",
        "South Carolina",
        "South Dakota",
        "Tennessee",
        "Texas",
        "Utah",
        "Vermont",
        "Virginia",
        "Washington",
        "West Virginia",
        "Wisconsin",
        "Wyoming",
        "District of Columbia",
    )
)
_BULLET = re.compile(r"^[-*•·–—]\s+")
_SURROGATE = re.compile("[\ud800-\udfff]")  # half of a character: no text can hold one alone (`_without_nul`)
_LICENCE_LABEL = "License #"
_EMPTY_DRAFT = {
    "family": None,
    "trade": None,
    "style": None,
    "accent": None,
    "wordmark_ok": False,
    "facts": {},
    "extras_asked": False,
    "words": None,
    "words_for": None,  # what the model read when it wrote `words` (`sites.words.fingerprint`, Task 4)
    # the words the model wrote for a build that has not passed yet, {"words", "for"}: a retry pays nothing (Task 4 fix
    # round 1); `words` stays the words of the last build that passed, so a site with none was never built
    "words_pending": None,
    # the e-mail of the site's first build went to the owner (Task 5, Q13): set once it was sent, so a build whose mail
    # could not go (no address, no mail on this server, a refusal) is tried again by the next, and none is sent twice
    "mailed_ready": False,
    # the questions this intake has asked so far -- the look, then each fact -- and the one asked last (Task 6 fix round 1):
    # a question's place among them, "2 of 5"; the same question asked again keeps its number; 0 and None once a site builds
    "asked": 0,
    "asked_last": None,
}


# ---------------------------------------------------------------- Q1: the family and the trade


def _trade_of(knowledge: dict) -> str | None:
    """The trade the knowledge's own words name: from the category's detail and the `trade` field first, then from
    every service's name; the first trade in the plan's order (the shared table, agent/trades.py) that either names.
    Only a trade a site family has counts: landscaping and remodel are in the table for the Strategist, and a text that
    names only those names no trade here, so the next text is read, as before the table had them."""
    own = " | ".join(v for v in (knowledge.get("category_detail"), knowledge.get("trade")) if isinstance(v, str))
    named = " | ".join(s["name"] for s in profile.services(knowledge))
    for words in (own, named):
        found = trades.trade_in(words)
        if found in home_services.TRADES:
            return found
    return None


def family_for(knowledge: dict | None) -> tuple[str, str | None] | None:
    """The family of site a business gets and its trade (Q1): home services (with the trade its words name -- one of
    the family's own four -- or none: the structure without a problem picker), or dental; None for any other
    category -- that business keeps the one-page starter page."""
    k = knowledge or {}
    if k.get("category") == "home_services":
        return "home_services", _trade_of(k)
    if k.get("category") == "dentists":
        return "dental", "dentist"
    return None


# ---------------------------------------------------------------- the small readings the rest is made of


def slug(name: str) -> str:
    """A service's slug (Q3): lower case, every run of characters that are not letters or digits one hyphen, accents
    dropped, trimmed, at most 40 characters, matching `families.SLUG_RE` ("service" for a name with none of them)."""
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:MAX_SLUG].strip("-") or "service"


def _slugs(names: list[str]) -> list[str]:
    """`slug` of each name, a slug already taken getting `-2`, `-3`, ... after it."""
    taken: set[str] = set()
    out = []
    for name in names:
        base = slug(name)
        found, n = base, 1
        while found in taken:
            n += 1
            found = f"{base}-{n}"
        taken.add(found)
        out.append(found)
    return out


def _lines(value) -> list[str]:
    """Text as the lines it says -- the knowledge's hours, a list of them -- split on newlines and semicolons, each
    stripped, the empty ones dropped."""
    return [line.strip() for line in re.split(r"[\n;]", value) if line.strip()] if isinstance(value, str) else []


def _towns(text: str) -> list[str]:
    """Towns said in words: split on commas, semicolons, new lines, "&" and "and", each once (case aside). A state --
    alone or with its ZIP code ("NC", "NC 28202") -- after a town is part of it: "Charlotte, NC 28202" is Charlotte;
    and a ZIP code is no town."""
    out: list[str] = []
    for part in (p.strip() for p in _TOWNS.split(text)):
        if not part or _ZIP.fullmatch(part) or (out and _STATE_TAIL.fullmatch(part)):
            continue
        if all(profile.norm(part) != profile.norm(town) for town in out):
            out.append(part)
    return out


def _names(text: str) -> list[str]:
    """Names typed as lines, or -- on one line -- separated by commas or semicolons; a bullet before one is dropped."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    parts = lines if len(lines) > 1 else [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    return [name for part in parts if (name := _BULLET.sub("", part))]


def read_place(text) -> dict:
    """What the owner said to "where is the business?" as a patch of facts (Q4): an address when it starts with a street
    number, else the towns it names (`service_area`); nothing when nothing was said."""
    if not isinstance(text, str) or not text.strip():
        return {}
    said = text.strip()
    if _STREET_NUMBER.match(said):
        return {"address": said}
    towns = _towns(said)
    return {"service_area": towns} if towns else {}


def _phone_ok(phone) -> bool:
    """Whether `phone` is a number the site may dial: ten digits at least, as `spec.validate` holds it."""
    target = page_builder.tel(phone) if isinstance(phone, str) else None
    return bool(target) and len(re.sub(r"\D", "", target)) >= spec_mod.MIN_PHONE_DIGITS


def _social_of(known_facts: dict | None) -> dict[str, str]:
    """The one social page the owner gave the website question (the consultation's `social` known fact), keyed by its
    host -- facebook, instagram, tiktok, yelp, else other -- and always as an https address."""
    url = (known_facts or {}).get("social")
    if not isinstance(url, str) or len(url) > MAX_TEXT:
        return {}
    url = re.sub(r"^http://", "https://", url.strip())
    try:
        host = (urlsplit(url).hostname or "") if url.startswith("https://") else ""
    except ValueError:
        return {}
    if not host:
        return {}
    for domain, key in _SOCIAL_SITES.items():
        if host == domain or host.endswith(f".{domain}"):
            return {key: url}
    return {"other": url}


def _services(entries: list[tuple[str, bool, str | None]]) -> list[Service]:
    """`(name, leads, price line)` as the document's services (Q3): each with a slug of its own, no more than six
    leading, and one leading when none does -- the first."""
    out: list[Service] = []
    for (name, wants_lead, price), at in zip(entries, _slugs([e[0] for e in entries]), strict=True):
        leads = sum(s.lead for s in out)
        out.append(
            Service(slug=at, name=name, lead=wants_lead and leads < spec_mod.MAX_LEAD_SERVICES, price_line=price)
        )
    if out and not any(s.lead for s in out):
        out[0].lead = True
    return out


def _led(entries: list[tuple[str, bool, str | None]], names) -> list[tuple[str, bool, str | None]]:
    """The services' `(name, leads, price line)` with the names the owner marked `lead` laid over the profile's flags
    (A3p-6 batch 2 Task 1, fix round 0): a name on the list leads and the others do not -- unless the list names none
    of the services (it is empty, or what it names was taken off since), when the profile's flags stand. Names are told
    apart as the profile tells them (`profile.norm`)."""
    wanted = {profile.norm(n) for n in names if isinstance(n, str)} - {""} if isinstance(names, list) else set()
    if not any(profile.norm(name) in wanted for name, _, _ in entries):
        return entries
    return [(name, profile.norm(name) in wanted, price) for name, _, price in entries]


def _storefront(knowledge: dict, address: str | None) -> bool:
    """Whether people visit the business (fix round 0): what the owner said (`geo_model`) when they said it, else the
    family's own default -- a dental practice is a place people visit once an address is on file, a home-services
    business is not (nor one of no family of sites)."""
    said = knowledge.get("geo_model")
    if said in profile.GEO_MODELS:
        return said == "storefront"
    found = family_for(knowledge)
    return bool(found and found[0] == "dental" and address)


def _spelled_state(part: str) -> bool:
    """Whether a part is a state spelled out ("Florida", "north   carolina", "District of Columbia"), in any case."""
    return " ".join(part.split()).lower() in _STATE_NAMES


def _town_after_street(address: str | None) -> str:
    """The town an address with a street number names when `places.town` cannot read it (Task 4 fix round 1, the
    controller's ruling; Task 5's and Task 6's follow-ups): what follows the street, without the parts that are no town
    -- the country, a ZIP code on its own, a unit ("Suite 4"), a state on its own, whatever its case ("fl 33127", "Fl.")
    -- with a country or a ZIP code glued to the end of a part, in either order, taken off it ("Charlotte NC 28203 USA",
    -- "Miami 33127", "33127 USA", "USA 33127": a part that is nothing else is dropped and the one before it judged), and
    -- a state spelled out after the town dropped too ("Miami, Florida 33127"; one that stands alone is the town, as in "123
    -- Broadway, New York"). The town is the last part left that ends in a state, alone or with its ZIP code, with the state
    -- dropped ("9 Elm Ct, Houston TX, 77002, USA" is Houston); with none that does, the last part left ("9 Elm Ct, Wynwood,
    -- Miami, 33127" is Miami). "" for an address with no street number, or nothing but those parts after its street."""
    if not address or not _STREET_NUMBER.match(address):
        return ""
    parts = []
    for part in (p.strip() for p in address.split(",")[1:]):
        if not part or _NOT_A_TOWN.fullmatch(part):  # a unit's own number is no ZIP code: judged before it is taken off
            continue
        while (cleaned := _ZIP_TAIL.sub("", _COUNTRY_TAIL.sub("", part))) != part:
            part = cleaned
        if part and not _NOT_A_TOWN.fullmatch(part) and not _BARE_STATE.fullmatch(part):
            parts.append(part)
    while len(parts) > 1 and _spelled_state(parts[-1]):  # a state spelled out is the town's, never the town
        parts.pop()
    if not parts:
        return ""
    ending = [part for part in parts if _TRAILING_STATE.search(part)]
    return _TRAILING_STATE.sub("", (ending or parts)[-1]).strip()


def _stripped(value) -> str | None:
    """A text the profile holds (a price hint, an address) as it is read: stripped, None for what is no text or blank."""
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------- Q2: the draft


def draft(page: dict | None) -> dict:
    """The site's working copy between builds, from a page row (`page.site_draft`): every key it has, an empty one for a
    page with none, and a copy -- nothing read here changes the row's own."""
    stored = (page or {}).get("site_draft")
    own = copy.deepcopy({**_EMPTY_DRAFT, **(stored if isinstance(stored, dict) else {})})
    if not isinstance(own["facts"], dict):
        own["facts"] = {}
    return own


def _write_draft(conn: psycopg.Connection, page_id: str, draft: dict) -> None:
    conn.execute("UPDATE page SET site_draft = %s WHERE id = %s", (Jsonb(draft), page_id))


def save_draft(conn: psycopg.Connection, page_id: str, draft: dict) -> None:
    """The whole draft written, under the page row's lock (`FOR UPDATE`, as `page_builder.current(lock=True)` holds a
    page): it waits for a read-modify-write in progress (`merge`, `update_draft`). It writes the document it is given,
    so a caller that read the draft before another writer set a key writes that key away: set single keys with
    `update_draft`."""
    with conn.transaction():
        conn.execute("SELECT id FROM page WHERE id = %s FOR UPDATE", (page_id,))
        _write_draft(conn, page_id, draft)


def update_draft(conn: psycopg.Connection, page_id: str, changes: dict) -> dict:
    """The stored draft with `changes` laid over its top-level keys (the look, the accent, the wordmark, the words,
    `extras_asked`), read and written under the page row's lock: a caller that sets its own keys never overwrites
    another writer's, which `save_draft` of a copy read earlier would. Returns the draft as written. The facts are
    `merge`'s (the profile's own go to the profile): ValueError for changes that carry them, and nothing written."""
    if "facts" in changes:
        raise ValueError("a draft's facts are written by merge, never by update_draft")
    with conn.transaction():
        stored = conn.execute("SELECT site_draft FROM page WHERE id = %s FOR UPDATE", (page_id,)).fetchone()
        current = {**draft(stored), **changes}
        _write_draft(conn, page_id, current)
    return current


# ---------------------------------------------------------------- Q3: the facts Admiry has


def _tools_state():
    """`tools_state`, read when asked: it reaches the page tools, which will reach this module -- no import cycle forms."""
    from .. import tools_state

    return tools_state


def facts(conn: psycopg.Connection, biz: str, page: dict | None) -> tuple[Facts, list[str]]:
    """The site's document of facts for a business, and the required facts still missing (`REQUIRED`'s order), read
    from what Admiry has now (Q3): the name a site reading, the owner or a confirmed listing vouches for (`plans.name_for`;
    one the model derived from words is asked), the phone when it has ten digits, the address of the confirmed listing or
    the one the owner placed, the towns, the hours as lines, the services (the ones the plan leads with lead), the
    social page the website question kept, the Google profile and the licence number. The draft's own facts lie over all
    of it, key by key (a list replaces), the services the owner marked `lead` over the plan's flags. Read from the
    address as it then stands: the town (else the first town the business serves: a radius or a ZIP code in the service
    area is no town, `consult.NO_TOWN`, and is not printed) and the state; and whether people visit the business --
    the owner's `geo_model`, else a dental practice with an address is a storefront, a home-services business is not."""
    latest = business.latest_knowledge(conn, biz)
    version, knowledge = latest if latest else (None, {})
    session, held = _tools_state()._consultation_facts(conn, biz)
    view = profile.view(
        business.row(conn, biz),
        version,
        knowledge,
        goal=business.latest_goal(conn, biz),
        session=session,
        radius_miles=held.get("radius_miles"),
        plan_offer=held.get("offer"),
    )
    where = profile.model_listing(knowledge.get("listing")) or {}  # the confirmed listing's address, or the owner's own
    own_listing = profile.business_listing(knowledge) or {}
    wording = view["facts"]
    table: dict = {
        "name": plans.name_for(knowledge) or "",
        "phone": knowledge["phone"].strip() if _phone_ok(knowledge.get("phone")) else "",
        "address": _stripped(where.get("address")),
        "service_area": list(view["locations"][0]["areas"]),
        "hours": _lines(knowledge.get("hours")),
        "social": _social_of((session or {}).get("known_facts")),
        "gbp_url": places.maps_url(own_listing.get("name"), own_listing.get("place_id")),
        "place_id": own_listing.get("place_id") or None,
        "licence": page_builder._labelled_number(
            _LICENCE_LABEL, {"facts": {"usp": wording["usp"], "pricing_hints": wording["pricing_hints"]}}
        ),
    }
    entries = [(s["name"], s["lead"], _stripped(s["price_hint"])) for s in view["services"]]
    own = draft(page)["facts"]
    for key, value in own.items():
        if key == "services":
            entries = [(s["name"], s.get("lead", False), s.get("price_line")) for s in value]
        elif key == "social":
            table["social"] = {**table["social"], **value}
        elif key != "leads":  # the owner's lead names are laid over the services' flags below
            table[key] = value
    # read last, from the table as the draft left it (the address it kept over a confirmed listing's included)
    table["services"] = _services(_led(entries, own.get("leads")))
    areas = table["service_area"]  # whatever the profile or the draft says: the places where the business works
    table["service_area"] = [a for a in areas if not consult.NO_TOWN.search(a)]  # the site prints the towns only
    table["city"] = (
        places.town(table["address"])
        or next(iter(table["service_area"]), "")
        or _town_after_street(table["address"])  # an address no town of which reads (Task 4 fix round 1)
    )
    table["state"] = places.state(table["address"])
    table["storefront"] = _storefront(knowledge, table["address"])
    doc = Facts(**table)
    met = {
        "name": bool(doc.name.strip()),
        "phone": _phone_ok(doc.phone),
        # a town the site can name -- the address's own, or one the business serves -- a storefront's address, or an
        # address with a street number (the business is there, though no town of it reads: Task 4 fix round 1); a
        # radius or a ZIP code alone says how far, not where (Task 4: the controller's ruling)
        "place": bool((doc.storefront and doc.address) or doc.city or _STREET_NUMBER.match(doc.address or "")),
        "services": bool(doc.services),
    }
    return doc, [key for key in REQUIRED if not met[key]]


# ---------------------------------------------------------------- Q3: what a patch may carry


def _without_nul(value):
    """`value` with the NUL characters taken out of every text in it, however deep -- a list, an object's values and its
    text keys -- as a copy: JSON can say one ("\\u0000") and JSONB cannot hold one, so a fact that carried one was an
    internal error (Task 6 fix round 1). A lone surrogate ("\\ud800", half of a character JSON can name and no text can
    hold) is taken out the same way (Task 7: the homepage's own JSON reads one). A text that was nothing else is empty,
    and says nothing. RecursionError for a looped object, or one nested deeper than Python goes: `check` says it is too
    much."""
    if isinstance(value, str):
        return _SURROGATE.sub("", value.replace("\x00", ""))
    if isinstance(value, list):
        return [_without_nul(item) for item in value]
    if isinstance(value, dict):
        return {(_without_nul(k) if isinstance(k, str) else k): _without_nul(v) for k, v in value.items()}
    return value


def _text(key: str, value, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{key} must be text.")
    text = value.replace("\x00", "").strip()  # before anything else: a NUL character is no text
    if len(text) > limit:
        raise ToolError(f"{key} must be at most {limit} characters.")
    return text


def _phone(key: str, value) -> str:
    text = _text(key, value)
    if not _phone_ok(text):
        raise ToolError(f"{key} must be a number with its area code, at least {spec_mod.MIN_PHONE_DIGITS} digits.")
    return text


def _email(key: str, value) -> str:
    text = _text(key, value)
    if not landing._EMAIL.fullmatch(text):
        raise ToolError(f"{key} must look like name@example.com.")
    return text


def _whole(key: str, value, low: int, high: int) -> int:
    """A whole number from `low` to `high`: a number, or text that is one ("12"). A true or false is no number."""
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        number = int(value.strip()) if re.fullmatch(r"\d{1,4}", value.strip()) else None
    else:
        number = None
    if number is None or not low <= number <= high:
        raise ToolError(f"{key} must be a whole number from {low} to {high}.")
    return number


def _flag(key: str, value) -> bool:
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be true or false.")
    return value


def _words(key: str, value) -> list[str]:
    """A list of words (languages, plans): a list of texts, or one text of lines or commas; the blanks dropped."""
    if isinstance(value, str):
        value = _names(value)
    elif not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{key} must be a list of words.")
    out = [_text(key, v) for v in value if v.strip()]
    if len(out) > MAX_LIST:
        raise ToolError(f"{key} can hold at most {MAX_LIST} entries.")
    return out


def _area(key: str, value) -> list[str]:
    if isinstance(value, str):
        value = _towns(value)
    return _words(key, value)


def _hours(key: str, value) -> list[str]:
    """Hours as lines: a list of lines or one text, split as the knowledge's are (newlines and semicolons)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{key} must be a list of lines or one text.")
    lines = [_text(key, line) for entry in value for line in _lines(entry)]
    if len(lines) > MAX_LIST:
        raise ToolError(f"{key} can hold at most {MAX_LIST} lines.")
    return lines


def _service_names(key: str, value) -> list[dict]:
    """Services as `{name, lead, price_line?}`: names (a list, or one text of lines or commas) or objects with a name,
    at most 30."""
    shape = f"{key} must be a list of names, or of objects with a name, a lead and a price_line."
    if isinstance(value, str):
        value = _names(value)
    if not isinstance(value, list):
        raise ToolError(shape)
    out = []
    for item in value:
        if isinstance(item, str):
            if not item.strip():
                continue
            item = {"name": item}
        if not isinstance(item, dict) or set(item) - {"name", "lead", "price_line"}:
            raise ToolError(shape)
        if not isinstance(item.get("name"), str) or not _BULLET.sub("", item["name"].strip()):
            raise ToolError(shape)
        lead = item.get("lead")
        if lead is not None and not isinstance(lead, bool):
            raise ToolError(shape)
        entry = {"name": _text(key, _BULLET.sub("", item["name"].strip())), "lead": bool(lead)}
        price = item.get("price_line")
        if price is not None and not isinstance(price, str):
            raise ToolError(shape)
        if price and price.strip():
            entry["price_line"] = _text(key, price)
        out.append(entry)
    if len(out) > profile.MAX_SERVICES:
        raise ToolError(f"{key} can name at most {profile.MAX_SERVICES} services.")
    return out


def _social(key: str, value) -> dict:
    shape = f"{key} must be an object with facebook, instagram, tiktok, yelp or other, each an https address."
    if not isinstance(value, dict) or set(value) - set(SOCIAL_KEYS):
        raise ToolError(shape)
    out = {}
    for name, url in value.items():
        if url is None or (isinstance(url, str) and not url.strip()):
            continue
        text = _text(key, url)
        try:
            host = urlsplit(text).hostname if text.startswith("https://") else None
        except ValueError:
            host = None
        if not host or re.search(r"\s", text):
            raise ToolError(shape)
        out[name] = text
    return out


def _reviews(key: str, value) -> list[dict]:
    shape = f"{key} must be a list of at most {MAX_REVIEWS} reviews, each an object with text, first_name and source."
    if not isinstance(value, list) or len(value) > MAX_REVIEWS:
        raise ToolError(shape)
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"text", "first_name", "source", "when"}:
            raise ToolError(shape)
        entry = {}
        for part in ("text", "first_name", "source"):
            if not isinstance(item.get(part), str) or not item[part].strip():
                raise ToolError(shape)
            entry[part] = item[part].strip()
        if len(entry["text"]) > MAX_REVIEW:
            raise ToolError(f"{key}: a review's text must be at most {MAX_REVIEW} characters.")
        for part in ("first_name", "source"):
            entry[part] = _text(key, entry[part])
        if isinstance(item.get("when"), str) and item["when"].strip():
            entry["when"] = _text(key, item["when"])
        elif item.get("when") is not None and not isinstance(item["when"], str):
            raise ToolError(shape)
        out.append(entry)
    return out


def _dentist(key: str, value) -> dict:
    shape = f"{key} must be an object with a name, and optionally credentials, school, years and note."
    if not isinstance(value, dict) or set(value) - {"name", "credentials", "school", "years", "note"}:
        raise ToolError(shape)
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ToolError(shape)
    out = {"name": _text(key, value["name"])}
    for part in ("credentials", "school", "note"):
        if not _nothing(value.get(part)):
            out[part] = _text(key, value[part])
    if not _nothing(value.get("years")):
        out["years"] = _whole(key, value["years"], *YEARS)
    return out


_CLEANERS = {
    "name": _text,
    "phone": _phone,
    "address": _text,
    "service_area": _area,
    "services": _service_names,
    "hours": _hours,
    "about": _text,
    "email": _email,
    "social": _social,
    "licence": _text,
    "years": lambda key, value: _whole(key, value, *YEARS),
    "insured": _flag,
    "warranty": _text,
    "free_estimate": _text,
    "emergency": _text,
    "financing": _text,
    "languages": _words,
    "reviews": _reviews,
    "dentist": _dentist,
    "new_patients": _text,
    "insurance_plans": _words,
    "in_office_plan": _text,
    "emergency_line": _text,
}


def _nothing(value) -> bool:
    """What says nothing: no value, a blank text, an empty list or object. A false and a number say something."""
    if isinstance(value, str):
        return not value.strip()
    return value is None or (isinstance(value, (list, dict)) and not value)


def _too_much(patch: dict) -> bool:
    """Whether a patch is longer than `MAX_PATCH` characters of JSON (what JSON cannot write -- a number of thousands of
    digits, a loop, a nesting too deep -- is more than a site's facts take too). A key that is no text (a caller in
    this process can send one) is not judged here: its own sentence refuses it -- the unknown fact, or the shape of
    the fact that holds it."""
    try:
        return len(json.dumps(patch, ensure_ascii=False, default=str)) > MAX_PATCH
    except TypeError:
        return False
    except (ValueError, RecursionError):
        return True


def check(patch: dict | None) -> dict:
    """A patch of facts, cleaned, or one ToolError sentence naming the key that is wrong (Q3): a key outside
    `SITE_FACTS`, a shape that is not the fact's, a phone under ten digits, more than 30 services, a text over 400
    characters (a review's, over 600), and a patch whose JSON is over 8,000 characters, judged before its keys are -- a
    looped object or one nested deeper than Python goes is too much too. What says nothing -- no value, a blank, an
    empty list -- is not given. NUL characters and lone surrogates are taken out of every text first (JSONB cannot hold
    either). `place`, the answer to the place question, is read as `read_place` reads it: the cleaned
    patch holds the address or the towns, never `place`. Nothing is written here: a patch that is refused leaves no
    trace."""
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        raise ToolError("facts must be an object of the facts to set.")
    try:
        patch = _without_nul(patch)  # before anything else is looked at
    except RecursionError:  # a looped object, or one nested deeper than any fact is (Task 7: JSON can send one)
        raise ToolError(TOO_MUCH) from None
    if _too_much(patch):
        raise ToolError(TOO_MUCH)
    if "place" in patch:  # the answer to the place question, read as an address or the towns it names; a fact the patch
        said = patch["place"]  # names on its own lies over what the words said
        if said is not None and not isinstance(said, str):
            raise ToolError(PLACE_NOT_TEXT)
        patch = {**read_place(said), **{key: value for key, value in patch.items() if key != "place"}}
    out = {}
    for key, value in patch.items():
        if key not in SITE_FACTS:
            raise ToolError(f"“{str(key)[:40]}” is not a fact a site takes; the facts are {', '.join(SITE_FACTS)}.")
        if _nothing(value):
            continue
        cleaned = _CLEANERS[key](key, value)
        if not _nothing(cleaned):
            out[key] = cleaned
    return out


# ---------------------------------------------------------------- Q3: what a patch writes where


def _same(after: dict, before: dict) -> bool:
    """Whether two versions of the knowledge say the same: the schema's number aside (`profile.apply` stamps it)."""
    return {k: v for k, v in after.items() if k != "schema"} == {k: v for k, v in before.items() if k != "schema"}


def _placed(conn: psycopg.Connection, biz: str, address: str) -> dict:
    """The listing an address the owner gave becomes -- the way the consultation's location answer adopts one: the
    place the map finds for it (`geocode.listing`) when this server has a map key and today's lookups allow, else the
    owner's words as they typed them; either way an address, `kind: address`, never a business's own listing."""
    settings = config.load()
    if geocode.can_place(settings):
        try:
            found = geocode.place(conn, settings, address, business_id=biz)
        except geocode.NoLookups:  # no lookups left today: the site needs no map point, the words stand
            found = None
        if found is not None:
            return geocode.listing(found)
    return {"kind": "address", "address": address}


def _on_file(knowledge: dict, address: str) -> bool:
    """Whether the place on file -- the confirmed listing or the address the owner placed, as `facts` reads it -- is at
    `address` already (the profile tells addresses apart as `profile.norm` does): said again, it is not put on the map
    again."""
    on_file = (profile.model_listing((knowledge or {}).get("listing")) or {}).get("address")
    return isinstance(on_file, str) and profile.norm(on_file) == profile.norm(address)


def _service_changes(knowledge: dict, wanted: list[dict]) -> list[dict]:
    """The profile's changes for the services a patch names: each name not on file added (the profile tells names apart
    as `profile.norm` does), and the price a patch gives -- the profile's `price_hint` -- for one on file or new. The
    profile has no place for which services lead: the names a patch marks `lead` are the draft's (`merge`)."""
    on_file = {profile.norm(s["name"]): s for s in profile._readable(knowledge.get("services"))}
    changes: list[dict] = []
    seen: set[str] = set()
    for item in wanted:
        said = profile.norm(item["name"])
        if not said or said in seen:
            continue
        seen.add(said)
        price = item.get("price_line")
        if said not in on_file:
            changes.append({"name": item["name"], **({"price_hint": price} if price else {})})
        elif price and price != on_file[said].get("price_hint"):
            changes.append({"id": profile.service_id(on_file[said]), "price_hint": price})
    return changes


def _route(knowledge: dict | None, address: str) -> str:
    """Where an address of a patch goes: `on_file` -- the place on file is at it already, nothing to write --, `draft` --
    the knowledge holds a Google listing the owner confirmed, which an address never unlinks: the site prints the typed
    one -- or `profile`, which puts it on the map as the consultation does (`_placed`)."""
    if _on_file(knowledge, address):
        return "on_file"
    return "draft" if profile.business_listing(knowledge) is not None else "profile"


def _changes(knowledge: dict, said: dict, listing: dict | None) -> tuple[dict, dict]:
    """The knowledge with the name of `said` written into it -- as the owner's own (`name_source`, so `plans.name_for`
    vouches for it) -- and the profile's changes for the rest of the profile's facts it says: the phone, hours and towns,
    the address (as `listing`, the place found for it, else the words as typed) and the services."""
    merged = dict(knowledge)
    if "name" in said and not (
        profile.clean_name(knowledge.get("name")) == said["name"] and knowledge.get("name_source") in ("site", "owner")
    ):
        merged["name"], merged["name_source"] = said["name"], "owner"
    where: dict = {}
    if "phone" in said:
        where["phone"] = said["phone"]
    if "hours" in said:
        where["hours"] = "; ".join(said["hours"])
    if "service_area" in said:
        where["areas"] = said["service_area"]
    if "address" in said:
        where |= {"address": said["address"], "listing": listing or {"kind": "address", "address": said["address"]}}
    changes: dict = {"location": where} if where else {}
    if services := _service_changes(knowledge, said.get("services", [])):
        changes["service_changes"] = services
    return merged, changes


def _on_the_profile(knowledge: dict, said: dict, listing: dict | None) -> dict | None:
    """The knowledge with the profile's facts of a patch written into it (`_changes`, applied by `profile.apply`), or
    None when it says all of it already."""
    merged, changes = _changes(knowledge, said, listing)
    after = profile.apply(merged, changes) if changes else merged
    return None if _same(after, knowledge) else after


def _look_up(conn: psycopg.Connection, biz: str, said: dict) -> dict | None:
    """The place the map finds for the address of a patch that goes to the profile -- None when it has no address or the
    address goes elsewhere (`_route`). Looked up first, before anything is read to be written back: the lookup is the
    slow step, and a change another caller makes while it runs must be in the version `merge` then writes. The rest of
    the patch is checked first, as the profile will apply it, so no lookup is spent on a patch the profile refuses."""
    address = said.get("address")
    if not address:  # nothing to put on the map: nothing to read before the lock
        return None
    latest = business.latest_knowledge(conn, biz)
    if latest is None or _route(latest[1], address) != "profile":
        return None
    rest = {k: v for k, v in said.items() if k in PROFILE_FACTS and k != "address"}
    merged, changes = _changes(latest[1], rest, None)
    if changes:
        profile.apply(merged, changes)
    return _placed(conn, biz, address)


def merge(conn: psycopg.Connection, biz: str, page: dict, patch: dict | None) -> dict:
    """A patch of facts put where it belongs, and the draft that holds the rest, saved and returned (Q3). The patch is
    checked first (`check`): a refusal writes nothing. The name, phone, address, towns, hours and services go to the
    business's knowledge as one version of the owner's own (`profile.apply`; an address is put on the map as the
    consultation's location answer does) -- none when it holds them all already -- and every other fact is the draft's,
    key by key (a list replaces; the social pages add up). Two things go to the draft instead of the knowledge
    (fix round 0): an address, when the knowledge holds a Google listing the owner confirmed -- an address never
    unlinks it; the site prints the typed one, and the listing's own said again takes it off -- and the names a patch
    marks `lead` (`facts["leads"]`, six at most, by the profile's own spelling), which the profile has no place for;
    a patch that marks none leaves them as they were. The draft also learns the family and the trade the knowledge
    says. A patch that says nothing writes nothing.

    Order (fix round 1): the map lookup an address may need comes first, outside any lock; then, in one transaction,
    the page row is taken (`FOR UPDATE`: one writer of the draft at a time, the draft read under it, so another writer's
    keys are kept), the knowledge is read last -- right before it is written back, so a change another caller made while
    the lookup ran is in what is written -- and knowledge and draft are written together or not at all. `page` (the
    row the caller holds) is left carrying the new draft."""
    said = check(patch)
    if not said:
        return draft(page)
    listing = _look_up(conn, biz, said)
    with conn.transaction():
        stored = conn.execute("SELECT site_draft FROM page WHERE id = %s FOR UPDATE", (page["id"],)).fetchone()
        current = draft(stored or page)
        latest = business.latest_knowledge(conn, biz)
        knowledge = latest[1] if latest else None
        on_the_profile = {k: v for k, v in said.items() if k in PROFILE_FACTS}
        the_draft = {k: v for k, v in said.items() if k not in PROFILE_FACTS}
        if address := on_the_profile.get("address"):
            route = _route(knowledge, address)
            if route == "draft":  # the listing the owner linked is never unlinked
                the_draft["address"] = on_the_profile.pop("address")
            else:  # on file: said again, what is on file stands; the profile's: the latest is its -- the draft's goes
                current["facts"].pop("address", None)
                if route == "on_file":
                    del on_the_profile["address"]
        if on_the_profile:
            if knowledge is None:
                raise ToolError(_tools_state().NOTHING_ON_FILE)
            if (after := _on_the_profile(knowledge, on_the_profile, listing)) is not None:
                business.add_knowledge(conn, biz, after, source="owner")
                knowledge = after
        for key, value in the_draft.items():
            if key == "social":
                current["facts"]["social"] = {**current["facts"].get("social", {}), **value}
            else:
                current["facts"][key] = value
        if leads := [s["name"] for s in said.get("services", []) if s["lead"]]:
            spelt = {profile.norm(s["name"]): s["name"] for s in profile._readable((knowledge or {}).get("services"))}
            named = dict.fromkeys(spelt.get(profile.norm(name), name) for name in leads)
            current["facts"]["leads"] = list(named)[: spec_mod.MAX_LEAD_SERVICES]
        if found := family_for(knowledge):
            current["family"], current["trade"] = found
        _write_draft(conn, page["id"], current)
    page["site_draft"] = copy.deepcopy(current)
    return current
