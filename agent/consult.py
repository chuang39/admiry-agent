"""Consultation sessions (spec 2026-09-26 §4, §8): the question tree a business gets, the frozen
options, what the assistant already told us (known_facts) and the answers so far. The server decides
the questions; the model is never asked what to ask."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from . import business, db, envelope, places, profile, site_install
from .util import iso

TREES_DIR = Path(__file__).parent / "consult_trees"
# The most of the owner's words anything reads: the reading (tools_business, which takes it from here) and the address
# scan (`site_in_words`, ruling R2-f)
MAX_TEXT = 4000
MAX_OTHER = 500
MAX_ANSWERS = 10
KNOWN_FACTS = (
    "business_name",
    "address",
    "objective",
    "service",
    "customer_value_usd",
    "radius_miles",
    "monthly_budget_usd",
    "offer",
    # spec 2026-10-01 §4.3: what the owner wants and the most a new customer may cost, written onto the goal
    "target_customers_per_month",
    "max_cost_per_customer_usd",
    # plan G1 Task 10: what the goal counts (one of business.GOAL_TYPES), written onto the goal with the two above
    "goal_type",
)
MAX_SERVICE_OPTIONS = 6
MAX_LEAD_SERVICES = (
    3  # picks the "lead with" question takes: six "lead" services is no lead (founder's walk, 2026-10-03)
)
TOO_MANY_PICKS = "Pick up to three services for the ads to lead with, or answer in your own words."
# The consultation's first question, when a lookup found places close to the business (plan
# 2026-10-03-agent-listing-confirm): the option that links none of them
LISTING_NONE = {"id": "none", "label": "None of these"}
_SPELLED = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}  # MAX_LEAD_SERVICES as question_sentence says it
COLS = "id, business_id, tree, locale, opening_text, known_facts, answers, options, step, status, plan_id, created_at, expires_at"
HANDOFF_PREFIX = "hs_"
HANDOFF_TTL = timedelta(minutes=30)  # spec §3 D41: the pasted prompt says "valid 30 minutes"
_MILES = re.compile(r"\b(\d{1,3})\s*(?:-?\s*)?(?:miles?|mi)\b", re.IGNORECASE)

# Q6 (plan 2026-10-05-agent-a3p-5-1-website-before-the-quote): a web address, in the owner's own words (W2, read at
# `start`) or as the answer to the website question (`_parse_website`).
_SCHEMES = ("http", "https")
_HOST_CHARS = re.compile(r"[a-z0-9.-]+", re.IGNORECASE)
_IP_HOST = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_TLD_MIN, _TLD_MAX = 2, 12


def _is_tld(label: str) -> bool:
    """Whether `label` -- the host's last dot-separated segment -- is a plausible top-level domain: 2 to 12 letters
    ("com", "example", "photography"), or an internationalized one ("xn--p1ai"). `label` only ever reaches here out
    of a host `_HOST_CHARS` already passed, so it holds nothing but ASCII letters, digits and hyphens -- `.isalpha()`
    needs no extra ASCII check of its own. Refuses a plain number or decimal ("3.5", "19.99") and a short
    abbreviation ("e.g", "i.e") reading as a website (task review round 2, Important): the old address-in-the-words
    regex enforced a letters-only tail after the last dot; normalize_site itself did not, until now."""
    if label.lower().startswith("xn--"):
        return len(label) > 4
    return _TLD_MIN <= len(label) <= _TLD_MAX and label.isalpha()


def normalize_site(text: str) -> str | None:
    """`text` as a website address (`scheme://host[/path]`, the host lower-cased, the path kept as given): `https://`
    is added when no scheme is given, `http`/`https` kept when one is given; None for anything else -- another
    scheme (`ftp://`), an e-mail address or any other host with an `@` in it, an IP address, `localhost`, a host with
    a character besides a letter, digit, dot or hyphen (a space included), no dot in the host at all, an empty label
    ("bright..com": never a real host, ruling T2-1), or a last label that is not a plausible TLD (`_is_tld`)."""
    text = (text or "").strip()
    if not text:
        return None
    scheme, sep, rest = text.partition("://")
    if sep:
        if scheme.lower() not in _SCHEMES or not rest:
            return None
        candidate = f"{scheme.lower()}://{rest}"
    else:
        candidate = f"https://{text}"
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError:  # e.g. "https://[": an unterminated IPv6 literal
        return None
    if not host or "@" in parts.netloc or not _HOST_CHARS.fullmatch(host):
        return None
    if "." not in host or host == "localhost" or _IP_HOST.fullmatch(host):
        return None
    labels = host.split(".")
    if not all(labels) or not _is_tld(labels[-1]):
        return None
    path = parts.path.lstrip("/")
    return f"{parts.scheme}://{host}" + (f"/{path}" if path else "")


# A host of a social or listing site (Q6): not a website of the business's own -- the clarification SOCIAL names it
# instead of reading it (`social_say`) -- their `www.`/`m.` forms included.
_SOCIAL_SITES = (
    "facebook.com",
    "instagram.com",
    "yelp.com",
    "linktr.ee",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "nextdoor.com",
    "google.com",
    "g.page",
    "maps.app.goo.gl",
)
SOCIAL_HOSTS: frozenset[str] = frozenset(host for site in _SOCIAL_SITES for host in (site, f"www.{site}", f"m.{site}"))


def is_social(url: str) -> bool:
    """Whether `url`'s host is a social or listing site (SOCIAL_HOSTS), never a website of the business's own."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return (host or "") in SOCIAL_HOSTS


# Q13 (spec §2, verbatim): the website question and its option, what the reply says once a site was read or could not
# be read -- an address `start` found in the owner's own words (W2) or the answer to the website question -- the two
# questions without a site, and the plan's clarifications of a website answer that is no site of the business's own.
WEBSITE_PROMPT = "Does the business have a website? Paste its address and I'll read it, or tap No website."
# ... and in its place when a site the owner named could not be read (spec 2026-10-12 §3.2, R1): the question asked
# again, after SITE_UNREAD_SAY, with "Try again" and "Ask me instead" its options (RETRY_OPTIONS), never "No website"
UNOPENED_PROMPT = "Paste the address again, or let me ask you instead."
NO_WEBSITE = "No website"
# R1's words for a site that could not be read, the first time and, with a retry spent again, the second (spec
# 2026-10-12 §3.2)
SITE_UNREAD_SAY = "I couldn't read {host} just now. It happens with some sites; it is nothing you did."
SITE_UNREAD_AGAIN = "Still couldn't read {host}. I'll ask instead."
RETRY = "retry"
ASK_INSTEAD = "ask_instead"
RETRY_OPTIONS = ({"id": RETRY, "label": "Try again"}, {"id": ASK_INSTEAD, "label": "Ask me instead"})
MAX_SITE_TRIES = 2  # fetches of one address before it is recorded unread (plan P1: start's own try counts as one)
NAME_AGAIN = "That doesn't look like a business name. What is the business called?"
MIN_SENTENCE_WORDS = 10
NAME_PROMPT = "What is the business called?"
SERVICES_PROMPT = "What do you offer? A few words are enough, like {examples}."
# ... and its words for a tree with no example of its own (M7: the fallback tree's trades are too different for one --
# a florist, a tutor, a salon): the "like ..." clause dropped
SERVICES_CLAUSE = ", like {examples}"
NOT_A_SITE = "That doesn't look like a web address. Paste it like example.com, or tap No website."
SOCIAL = "That's a page on {host}. Does the business have a website of its own? Paste it, or tap No website."
# Q1: the questions every tree asks before its own, in one file beside the trades'
COMMON_TREE = TREES_DIR / "_common.json"
WEBSITE = "website"
# The website question's one-tap answer when the reading saw a site nobody read (Q5): the option carries its address
SITE_KNOWN = "site_known"
# The questions asked only without a site read (Q3): the business's name and what it offers
WITHOUT_SITE = ("business_name", "services_offered")
MAX_NAME = 120  # Q4: the longest known business name that answers its question


def social_say(url: str) -> str:
    """SOCIAL for a page on a social or listing site (Q6), naming the site as the owner knows it: its host without the
    `www.` or `m.` in front ("yelp.com")."""
    host = urlsplit(url).hostname or ""
    return SOCIAL.format(host=host.removeprefix("www.").removeprefix("m."))


@cache
def load_tree(name: str) -> dict:
    path = TREES_DIR / f"{name}.json"
    if not path.exists():
        path = TREES_DIR / "fallback.json"
    return json.loads(path.read_text())


@cache
def _common_questions() -> tuple[dict, ...]:
    return tuple(json.loads(COMMON_TREE.read_text())["questions"])


def tree_questions(tree: dict) -> list[dict]:
    """A tree's questions in the order a consultation asks them (Q1, ruling F1): the website and the business's name
    (common to every tree: `_common.json`), the trade's listing question -- which Google Maps place is the business,
    asked right after the name its lookup finds the places by -- then what the business offers (common too), then the
    trade's own. Every reader that walks a tree's questions reads them here; a lookup by a trade question's id
    (customer_value, service, the radius question at the end) reads tree["questions"], which load_tree leaves as the
    trade's file has it (the listing question first)."""
    common = _common_questions()
    after_name = next(i for i, q in enumerate(common) if q["id"] == "business_name") + 1
    listing = [q for q in tree["questions"] if q.get("options_from") == "listing"]
    own = [q for q in tree["questions"] if q.get("options_from") != "listing"]
    return [*common[:after_name], *listing, *common[after_name:], *own]


def website_answer(session: dict) -> dict | None:
    """The session's answer to the website question (Q3): a site read or not ({"url", "read"}), "No website"
    ({"none": true}); None while it is unanswered."""
    answer = (session.get("answers") or {}).get(WEBSITE)
    return answer if isinstance(answer, dict) else None


def site_read(session: dict) -> bool:
    """Whether the website question was answered by a site that was read: the one `start` read (its pre-answer, Q4)
    or the one the owner gave as the answer (Q7)."""
    return (website_answer(session) or {}).get("read") is True


def no_website_said(answer: dict | None) -> bool:
    """Whether a website answer is the owner's own "No website" -- the tap, or words that say so (`says_no_website`) --
    and not one the retry rule ended as no website (a second miss's `note`, a second social page's `social`): only the
    owner's own takes an address that could not be read off the profile (A3p-5 batch 2 Task 0, rulings R0-i, R0-i2)."""
    answer = answer or {}
    return answer.get("none") is True and "note" not in answer and "social" not in answer


def tree_for(category: str) -> str:
    return category if (TREES_DIR / f"{category}.json").exists() else "fallback"


def _slug(label: str) -> str:
    return "svc_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]


def _same_label(label: str) -> str:
    """A label for duplicate checks, as the profile tells two services apart (`profile.norm`: case, punctuation and
    spacing aside -- "Teeth-whitening" is "Teeth Whitening", ruling F8) with a trailing "s" ignored ("Cleanings" is
    "Cleaning")."""
    return profile.norm(label).removesuffix("s")


# Value bands for a business whose customers are worth less than its trade's tree assumes (a florist is not a
# dentist): (upper bound of the typical value, bands). A typical value at or above the last bound keeps the tree's.
VALUE_LADDERS = (
    (
        100,
        [
            {"id": "lt_20", "label": "Under $20", "usd": 15},
            {"id": "20_50", "label": "$20 to 50", "usd": 35},
            {"id": "50_100", "label": "$50 to 100", "usd": 75},
            {"id": "100_500", "label": "$100 to 500", "usd": 250},
            {"id": "500_plus", "label": "$500 and up", "usd": 750},
        ],
    ),
    (
        1000,
        [
            {"id": "lt_300", "label": "Under $300", "usd": 200},
            {"id": "300_1000", "label": "$300 to 1,000", "usd": 600},
            {"id": "1000_3000", "label": "$1,000 to 3,000", "usd": 1800},
            {"id": "3000_plus", "label": "$3,000 and up", "usd": 4500},
        ],
    ),
)


def _value_options(options: list[dict], typical) -> list[dict]:
    """The customer_value options for a business whose knowledge says one customer is typically worth
    `typical` USD: the ladder that fits it, and "estimate" carrying that number. Unknown (None, not a
    positive number) keeps the tree's own bands."""
    typical = _clean_number(typical)
    if typical is None:
        return options
    estimate = next(({**o, "usd": typical} for o in options if o["id"] == "estimate"), None)
    bands = next((b for bound, b in VALUE_LADDERS if typical < bound), None)
    if bands is None:
        return [o for o in options if o["id"] != "estimate"] + ([estimate] if estimate else [])
    return [dict(b) for b in bands] + ([estimate] if estimate else [])


# The location question's one option: the town the owner's opening words named (the founder's dev walk, 2026-10-04)
STATED_PLACE = "stated_place"
# What a service area says that is no town to centre on: a street address or ZIP code (digits: start puts those on the
# map), a distance, or nowhere in particular. Public: the industry site's facts print and name the city by the towns
# only (agent/sites/intake.py, A3p-6 batch 2)
NO_TOWN = re.compile(
    r"\d|\b(?:miles?|km|kilomet(?:er|re)s?|radius|nationwide|statewide|worldwide|online|anywhere|everywhere|remote)\b",
    re.IGNORECASE,
)


def stated_town(knowledge: dict) -> str | None:
    """The one town the owner's words named for the business (Knowledge.service_area), for the location question to
    start from: a single entry that is a town (NO_TOWN); none once the business is on the map (its listing's point
    centres the ads and pre-answers the question)."""
    knowledge = knowledge or {}
    listing = knowledge.get("listing") if isinstance(knowledge.get("listing"), dict) else {}
    if listing.get("lat") is not None and listing.get("lng") is not None:
        return None
    area = [a.strip() for a in knowledge.get("service_area") or [] if isinstance(a, str) and a.strip()]
    if len(area) != 1 or NO_TOWN.search(area[0]):
        return None
    return area[0][:MAX_OTHER]


def _unread_site(knowledge: dict) -> str | None:
    """The website on the knowledge, as normalize_site writes it, when it is one of the business's own (no social or
    listing page): a site the reading saw that nobody read is offered as the website question's one tap (Q5) -- the
    words named it in a way the address pattern missed (W2). A site that was read pre-answers the question, so what
    it offers is never asked."""
    site = (knowledge or {}).get("website")
    url = normalize_site(site) if isinstance(site, str) else None
    return url if url and not is_social(url) else None


def _trade_defaults(question: dict, knowledge: dict | None) -> list[dict] | None:
    """The services a home-services trade leads with, in place of the family's defaults (the question's `by_trade`, by
    the trade the business's words name: sites.intake's reading) -- the founder's dev walk, 2026-10-08: an AC contractor
    was offered "Remodels" and "Roofing". None for a tree with none, or a business of no trade the tree names."""
    from .sites import intake as site_intake  # it imports this module: read when asked

    by_trade = question.get("by_trade") or {}
    found = site_intake.family_for(knowledge) if by_trade else None
    picked = by_trade.get(found[1]) if found and found[1] else None
    return [dict(o) for o in picked] if picked else None


def build_options(
    tree: dict, knowledge: dict, *, candidates=(), known_facts: dict | None = None
) -> dict[str, list[dict]]:
    """The options each question shows, frozen for the session: a site the reading saw that nobody read, then "No
    website" (`_unread_site`) -- RETRY_OPTIONS alone ("Try again", "Ask me instead"), never "No website", when `start`
    tried a site it could not open (the session's `known_facts`, `site_unopened`: spec 2026-10-12 §3.2, R1), whose
    question asks for the address again; the places `start`'s lookup found close to the business (`candidates`,
    listings.numbered's) and "None of these", or none at all when it found none; the business's own services first; a
    default that names one of them (singular or plural) is left out; value bands sized to the business."""
    out: dict[str, list[dict]] = {}
    unopened = site_unopened(known_facts) is not None
    for q in tree_questions(tree):
        options = [dict(o) for o in q["options"]]
        if q["input"] == "website" and unopened:
            options = [dict(o) for o in RETRY_OPTIONS]  # the address again, or Admiry asks instead (spec §3.2)
        elif q["input"] == "website" and (site := _unread_site(knowledge)):
            options = [{"id": SITE_KNOWN, "label": f"Yes: {urlsplit(site).hostname}", "url": site}, *options]
        if q.get("options_from") == "services":
            options = _trade_defaults(q, knowledge) or options
            own = [
                {"id": s.get("id") or _slug(s["name"]), "label": s["name"]}
                for s in (knowledge or {}).get("services", [])
                if s.get("name")
            ]
            seen = {_same_label(o["label"]) for o in own}
            options = (own + [o for o in options if _same_label(o["label"]) not in seen])[:MAX_SERVICE_OPTIONS]
        if q["id"] == "customer_value":
            options = _value_options(options, (knowledge or {}).get("typical_customer_value_usd"))
        if q["input"] == "address" and (town := stated_town(knowledge)):
            options = [{"id": STATED_PLACE, "label": f"All of {town}", "place": town}]
        if q.get("options_from") == "listing":  # the places a lookup found close to the business, then "None of these"
            options = (
                [
                    {"id": c["id"], "label": places.label(c["name"], c["address"]), "sublabel": "Google Maps"}
                    for c in candidates
                ]
                + [dict(LISTING_NONE)]
                if candidates
                else []
            )
        out[q["id"]] = options
    return out


def _known(value, q: dict, options: list[dict]) -> dict | None:
    """A known fact as a stored answer for question q, or None when it answers nothing. A text question (the business's
    name, Q4) takes words of at most MAX_NAME characters."""
    if q["input"] == "text":
        name = value.strip() if isinstance(value, str) else ""
        return {"name": name, "source": "known_fact"} if 0 < len(name) <= MAX_NAME else None
    if q["input"] == "radius":
        try:
            miles = float(value)
        except (TypeError, ValueError):
            return None
        return {"miles": miles, "raw": value, "source": "known_fact"} if 0 < miles <= 100 else None
    if q["id"] == "customer_value":
        try:
            usd = float(value)
        except (TypeError, ValueError):
            return None
        return {"amount_usd": usd, "raw": value, "source": "known_fact"} if usd > 0 else None
    if isinstance(value, str):
        by_id = {o["id"]: o for o in options}
        by_label = {o["label"].lower(): o for o in options}
        hit = by_id.get(value) or by_label.get(value.strip().lower())
        if hit:
            return {"choice": hit["id"], "raw": value, "source": "known_fact"}
        if q["id"] == "service" and value.strip():
            return {"note": value.strip()[:MAX_OTHER], "raw": value, "source": "known_fact"}
    return None


NUMERIC_FACTS = (
    "customer_value_usd",
    "radius_miles",
    "monthly_budget_usd",
    "target_customers_per_month",
    "max_cost_per_customer_usd",
)
TEXT_FACTS = tuple(k for k in KNOWN_FACTS if k not in (*NUMERIC_FACTS, "goal_type"))


def _clean_number(value) -> float | None:
    """A positive float from a plain number or a numeric string ("$2,000", "2000.0"); anything else
    (a bool, a dict, a list, unparsable text, zero, negative or infinite -- "inf" and "1e999" parse, and a JSONB
    column cannot hold them) is dropped, never raised: a bad known_fact is treated as no fact, not a crash."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str):
        try:
            n = float(value.strip().replace("$", "").replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return n if 0 < n < math.inf else None


def clean_known_facts(raw: dict | None) -> dict:
    """known_facts as `start`'s caller may send them, bounded before they reach set_goal or a plan
    section: only the keys of KNOWN_FACTS -- the numbers (NUMERIC_FACTS: money, distance, the target) coerced to a
    positive float (or dropped), the text fields (TEXT_FACTS) kept only when they are str, stripped and at most
    MAX_OTHER characters (empty after stripping is dropped too), and goal_type only when it is one of
    business.GOAL_TYPES."""
    raw = raw or {}
    out: dict = {}
    for key in NUMERIC_FACTS:
        if key in raw:
            n = _clean_number(raw[key])
            if n is not None:
                out[key] = n
    for key in TEXT_FACTS:
        if key in raw and isinstance(raw[key], str) and raw[key].strip():
            out[key] = raw[key].strip()[:MAX_OTHER]
    if raw.get("goal_type") in business.GOAL_TYPES:  # a tuple: a list or an object compares unequal, never raises
        out["goal_type"] = raw["goal_type"]
    return out


def host_of(url: str) -> str:
    """A site's host as the owner knows it: without the `www.` in front ("acme.example")."""
    return (urlsplit(url).hostname or url).removeprefix("www.")


def same_host(a: str | None, b: str | None) -> bool:
    """Whether two addresses name one site: the same host, `www.` aside."""
    return bool(a and b) and host_of(a).lower() == host_of(b).lower()


def _platform_name(platform: str | None) -> str | None:
    """`platform` (`website_platform`) as the owner reads it: site_install's own display name ("WordPress", "Wix",
    ...) when site_install names it -- `steps_for`'s own entry; None for `other` (its "your site" names no builder),
    for anything site_install does not know, and for no platform at all, so the facts card draws no "Built on" row
    (Task 7 fix round)."""
    named = site_install.steps_for(platform)
    return named["name"] if platform and platform != "other" and named["platform"] == platform else None


def reading_view(data: dict) -> dict:
    """What a site's reading gave, for the owner to check (spec 2026-10-12 §3.3, R6): the host, the name when the site
    vouches for it (`name_source` site), up to six services, the areas served, the phone, the platform and the pages
    read. Missing parts are None; `services` may be empty."""
    areas = [a.strip() for a in data.get("service_area") or [] if isinstance(a, str) and a.strip()]
    return {
        "host": host_of(data.get("website") or ""),
        "name": data.get("name") if data.get("name_source") == "site" else None,
        "services": [s["name"] for s in data.get("services") or [] if isinstance(s, dict) and s.get("name")][:6],
        "area": ", ".join(areas[:3]) or None,
        "phone": data.get("phone") or None,
        "platform": _platform_name(data.get("website_platform")),
        "pages": data.get("pages_read") or None,
    }


def reading_say(view: dict) -> str:
    """The one sentence of what was read: the parts present, lower-cased services, then the invitation to correct."""
    parts = [
        view.get("name"),
        ", ".join(s.lower() for s in view.get("services") or []) or None,
        view.get("area"),
        view.get("phone"),
    ]
    said = "; ".join(p for p in parts if p)
    return f"I read {view['host']}: {said}. Tell me if anything is off." if said else f"I read {view['host']}."


def site_tried(known_facts: dict | None) -> dict | None:
    """The site `start` or an answer tried, as the known facts carry it (ruling P1: {"url", "read"}, with `tries`, the
    fetches spent on it -- one when the key is missing: `start`'s own) in the shape of the website question's
    pre-answer, which it is when it was read; None when none was tried."""
    site = (known_facts or {}).get(WEBSITE)
    if not isinstance(site, dict) or not isinstance(site.get("url"), str) or not isinstance(site.get("read"), bool):
        return None
    return {"url": site["url"], "read": site["read"], "tries": int(site.get("tries") or 1), "source": "known_fact"}


def site_unopened(known_facts: dict | None) -> str | None:
    """The address `start` tried and could not open -- an address in the owner's first words, or an assistant's
    website_url, whose crawl read no page: its known fact with `read` false (ruling P1) -- or None. It answers nothing:
    the website question asks for it again, in UNOPENED_PROMPT's words, with "Try again" and "Ask me instead" its
    options (RETRY_OPTIONS), never "No website" (spec 2026-10-12 §3.2, R1), so a typo in it is corrected by answering
    (A3p-5 batch 2 Task 0, ruling R0-i)."""
    site = site_tried(known_facts)
    return site["url"] if site is not None and not site["read"] else None


def site_tries_left(session: dict, url: str) -> int:
    """How many more fetches `url` may have (MAX_SITE_TRIES less the tries spent on its host, when it is the site
    tried and unread); MAX_SITE_TRIES for another address."""
    tried = site_tried(session.get("known_facts"))
    if tried is None or tried["read"] or not same_host(tried["url"], url):
        return MAX_SITE_TRIES
    return max(0, MAX_SITE_TRIES - tried["tries"])


def site_reanswer(session: dict, text: str) -> str | None:
    """An address in the words of any answer that answers the website question instead (spec 2026-10-12 §3.4, R2):
    the first address `site_in_words` finds, a social page passed over, while no site was read and the owner did not
    say "No website" (`no_website_said` -- the retry rule's own note or social ending is no such word: final review
    Important 1), and the address still has a fetch left (`site_tries_left`); None otherwise."""
    if site_read(session) or no_website_said(website_answer(session)):
        return None
    site = site_in_words(text, social=False)
    if site is None or not site_tries_left(session, site):
        return None
    return site


def looks_like_a_sentence(text: str) -> bool:
    """Whether words given as the business's name read as a sentence instead (R2): a question mark at the end, or
    MIN_SENTENCE_WORDS words or more."""
    words = (text or "").strip()
    return words.endswith("?") or len(words.split()) >= MIN_SENTENCE_WORDS


def _confirmed_name(knowledge: dict) -> str | None:
    """The name of the business's own Google Maps listing, as the owner confirmed it (profile.business_listing); None
    without one."""
    return (profile.business_listing(knowledge) or {}).get("name")


def preanswers(
    tree: dict, known_facts: dict, knowledge: dict, opening_text: str, *, can_place: bool = True
) -> dict[str, dict]:
    options = build_options(tree, knowledge)
    facts = {k: v for k, v in (known_facts or {}).items() if k in KNOWN_FACTS}
    out: dict[str, dict] = {}
    listing = knowledge.get("listing") if isinstance((knowledge or {}).get("listing"), dict) else {}
    pointed = listing.get("lat") is not None and listing.get("lng") is not None
    for q in tree_questions(tree):
        key = q.get("known_fact")
        if q["input"] == "website":  # the site start read (Q4); one it could not open is asked for (ruling R0-i)
            if (site := site_tried(known_facts)) and site["read"]:
                out[q["id"]] = {k: v for k, v in site.items() if k != "tries"}  # a site read has no tries to count
            continue
        if q.get("options_from") == "listing":
            # with no map lookup no place is ever offered: skipped, as where the ads centre is, so the question is never
            # counted ahead of places that cannot come (ruling F1)
            if not can_place:
                out[q["id"]] = {"skipped": True, "source": "known_fact"}
            continue
        if q["input"] == "address":
            # asked only where there is no point to centre on, and only where the server can place an answer: a
            # listing's point pre-answers it -- the business's own listing the owner confirmed, or the address an
            # assistant passed, which start always puts on the map (Task 4 rounds 1-2) -- never the bare known fact
            # nor a place a lookup found (the owner's pick of one centres the ads: tools_consult.answer); with no map
            # lookup, skipped
            if pointed:
                out[q["id"]] = {
                    "address": listing.get("address"),
                    "lat": listing["lat"],
                    "lng": listing["lng"],
                    "source": "known_fact",
                }
            elif not can_place:
                out[q["id"]] = {"skipped": True, "source": "known_fact"}
            continue
        if key in facts and (ans := _known(facts[key], q, options[q["id"]])):
            out[q["id"]] = ans
        elif q["id"] == "business_name" and (ans := _known(_confirmed_name(knowledge), q, options[q["id"]])):
            out[q["id"]] = ans  # the listing the owner confirmed names the business: never asked, nor looked up (F2)
        elif q["input"] == "radius" and (m := _MILES.search(opening_text or "")):
            out[q["id"]] = {"miles": float(m.group(1)), "raw": m.group(0), "source": "known_fact"}
    return out


def corrections(tree: dict, text: str) -> list[str]:
    said = []
    for c in tree.get("corrections", []):
        if any(re.search(rf"\b{re.escape(p)}\b", text or "", re.IGNORECASE) for p in c["patterns"]):
            said.append(c["say"])
    return said


def _json(r) -> dict:
    d = dict(r)
    d["created_at"], d["expires_at"] = iso(d["created_at"]), iso(d["expires_at"])
    return d


def create(
    conn: psycopg.Connection,
    business_id: str,
    *,
    tree: str,
    locale: str,
    opening_text: str,
    known_facts: dict,
    options: dict,
    answers: dict,
    expires_at: datetime,
) -> dict:
    sid = db.new_id("cs")
    conn.execute(
        "INSERT INTO consult_session (id, business_id, tree, locale, opening_text, known_facts, answers, options, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (sid, business_id, tree, locale, opening_text, Jsonb(known_facts), Jsonb(answers), Jsonb(options), expires_at),
    )
    s = get(conn, business_id, sid)
    return _settle(conn, s)


def get(conn: psycopg.Connection, business_id: str, session_id: str) -> dict | None:
    r = conn.execute(
        f"SELECT {COLS} FROM consult_session WHERE id = %s AND business_id = %s", (session_id, business_id)
    ).fetchone()
    return _json(r) if r else None


def open_for(conn: psycopg.Connection, business_id: str) -> dict | None:
    """The consultation an assistant that lost its session_id should resume: still open (has questions
    left), or ready (nothing left to ask, but recommend has not run yet). The partial unique index
    (consult_session_one_open) only covers 'open', so this orders by created_at to pick the latest."""
    r = conn.execute(
        f"SELECT {COLS} FROM consult_session WHERE business_id = %s AND status IN ('open', 'ready') "
        "ORDER BY created_at DESC LIMIT 1",
        (business_id,),
    ).fetchone()
    return _json(r) if r else None


HANDOFF_STATUSES = ("open", "ready", "planned")  # a finished plan hands off too (founder's phone walk, 2026-09-28)


def handoff_for(conn: psycopg.Connection, business_id: str) -> dict | None:
    """The consultation the assistant sheet hands to Muse: the open/ready one when there is one, else the
    latest planned one (the owner built the plan on the website and continues from it, not from question 1)."""
    r = conn.execute(
        f"SELECT {COLS} FROM consult_session WHERE business_id = %s AND status IN ('open', 'ready', 'planned') "
        "ORDER BY (status = 'planned'), created_at DESC LIMIT 1",
        (business_id,),
    ).fetchone()
    return _json(r) if r else None


def supersede_open(conn: psycopg.Connection, business_id: str) -> None:
    """Retire this business's own current session (open, still being answered, or ready to plan) before a
    new one takes its place — a fresh `start` on the business, or a hand-off session landing on it
    (move_session) while it already has one of its own."""
    conn.execute(
        "UPDATE consult_session SET status = 'superseded' WHERE business_id = %s AND status IN ('open', 'ready')",
        (business_id,),
    )


def mint_handoff(conn: psycopg.Connection, session_id: str) -> str:
    """A fresh hand-off token for this session (spec §3 D41): `hs_` + 16 random hex bytes, valid 30
    minutes. Only its sha256 is stored; a new mint replaces whatever token was live before it."""
    token = HANDOFF_PREFIX + secrets.token_hex(16)
    conn.execute(
        "UPDATE consult_session SET handoff_hash = %s, handoff_expires_at = now() + %s WHERE id = %s",
        (hashlib.sha256(token.encode()).hexdigest(), HANDOFF_TTL, session_id),
    )
    return token


def _live_handoff_hash(token: str) -> str | None:
    """The sha256 to look up by, or None for a token that cannot possibly redeem (wrong prefix/length) —
    shared by peek_handoff and take_handoff so the two can never drift on what counts as well-formed."""
    if not token or not token.startswith(HANDOFF_PREFIX) or len(token) > 128:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def peek_handoff(conn: psycopg.Connection, token: str) -> bool:
    """Whether `token` currently redeems — read-only, so a caller about to do real work (an LLM
    extraction) on the strength of a hand-off can refuse first and never spend it on a token that
    would fail anyway. take_handoff still does the one real, consuming check; a token can in principle
    be spent by someone else between this call and that one, which take_handoff still catches."""
    digest = _live_handoff_hash(token)
    if digest is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM consult_session WHERE handoff_hash = %s AND handoff_expires_at > now() "
        "AND status IN ('open', 'ready', 'planned')",
        (digest,),
    ).fetchone()
    return row is not None


def take_handoff(conn: psycopg.Connection, token: str) -> dict | None:
    """The session a hand-off token names, consuming it (one use: the hash is cleared in the same
    statement, so a concurrent second call can never also match it) — None for an unknown, already-used
    or expired token, or one whose session is superseded or expired (open, ready and planned all redeem)."""
    digest = _live_handoff_hash(token)
    if digest is None:
        return None
    row = conn.execute(
        "UPDATE consult_session SET handoff_hash = NULL, handoff_expires_at = NULL "
        "WHERE handoff_hash = %s AND handoff_expires_at > now() AND status IN ('open', 'ready', 'planned') "
        "RETURNING id, business_id",
        (digest,),
    ).fetchone()
    return get(conn, row["business_id"], row["id"]) if row else None


# The fields of the draft's goal a hand-off carries onto the destination (move_session); the most-per-customer has a
# rule of its own
HANDED_OFF = ("goal_type", "autopilot", "target", "monthly_limit_usd", "limit_source", "focus_services")


def move_session(conn: psycopg.Connection, session: dict, *, to_business: str) -> dict:
    """A hand-off landing: re-point `session` onto `to_business` — the draft business it came from is
    done for. Runs in the caller's transaction (the body of `start`).

    Before the move: `to_business`'s own open/ready session, if it has one, is superseded (it would
    otherwise collide with the moved session on consult_session_one_open, or just sit there stale); the
    draft's latest knowledge and goal are copied onto `to_business` as its own new versions, so nothing
    the visitor told the website is lost. After the move: the draft business is retired (status
    'expired') and every live key of its own is revoked — a business already migrated off is not a
    draft to claim or a key to use again.

    The goal (final review open item A2): a destination with no goal takes the draft's whole. One with a goal takes
    from the draft only what the draft states and keeps what its owner set for the rest (a draft's default is no
    choice): the draft's goal type unless it is the default "customers", its automatic changes unless "safe", its
    target and its focus services unless it has none, its monthly limit and whose it is only when the visitor stated
    one (source "owner", never a ceiling Admiry set), its most-per-customer unless it has none.

    The listing (plan 2026-10-03-agent-listing-confirm): the draft's knowledge holds a listing only when the visitor
    confirmed one on the website or placed an address, so only that is copied; the places the draft's first question
    offers are not here -- `start` moves that offer with the session (listings.move_offer).

    Where the ads centre (walk fixes 1, final review A1): a place the destination's owner put on the map
    (profile.placed) survives the copy unless the visitor put one there on the website -- the draft's listing, a
    confirmed place, is moved to it (business.readopt_place), and the moved session's pre-answer is read from it."""
    from_business = session["business_id"]
    latest = business.latest_knowledge(conn, from_business)
    if latest is not None:
        place = (business.latest_knowledge(conn, to_business) or (None, {}))[1].get("listing")
        business.add_knowledge(conn, to_business, latest[1], source="handoff")
        if profile.placed(place) and not profile.placed(latest[1].get("listing")):
            kept = business.readopt_place(conn, to_business, place)
            _centred(conn, session, kept.get("listing") or {})
    goal = business.latest_goal(conn, from_business)
    if goal is not None:
        held = business.latest_goal(conn, to_business)
        if held is None:  # nothing set on the destination: the draft's goal, whole
            kept = {k: goal[k] for k in HANDED_OFF}
        else:
            stated = goal["monthly_limit_usd"] is not None and goal["limit_source"] == "owner"
            limit = goal if stated else held  # the limit and whose it is go together
            kept = {
                "goal_type": goal["goal_type"] if goal["goal_type"] != "customers" else business.KEEP,
                "autopilot": goal["autopilot"] if goal["autopilot"] != "safe" else business.KEEP,
                "target": goal["target"] if goal["target"] is not None else held["target"],
                "monthly_limit_usd": limit["monthly_limit_usd"],
                "limit_source": limit["limit_source"],
                "focus_services": goal["focus_services"] or held["focus_services"],
            }
        business.set_goal(
            conn,
            to_business,
            **kept,
            # a draft that states no most-per-customer keeps the destination's; one that states one replaces it
            max_cost_per_customer_usd=(
                goal["max_cost_per_customer_usd"] if goal["max_cost_per_customer_usd"] is not None else business.KEEP
            ),
        )
    supersede_open(conn, to_business)  # before re-pointing: never supersedes the session we are about to move
    conn.execute("UPDATE consult_session SET business_id = %s WHERE id = %s", (to_business, session["id"]))
    business.retire_draft(conn, from_business)
    return get(conn, to_business, session["id"])


def _centred(conn: psycopg.Connection, session: dict, listing: dict) -> None:
    """A session's pre-answer of where the ads centre -- a known fact read from a listing's point -- read again from
    `listing`, the place a hand-off's landing kept (final review A1), as `preanswers` reads one: the radius question
    then names it. An answer the owner gave, a skip, or no answer yet stays as it is."""
    answer = (session.get("answers") or {}).get("location") or {}
    if answer.get("source") != "known_fact" or answer.get("lat") is None:
        return
    if listing.get("lat") is None or listing.get("lng") is None:
        return
    centre = {"address": listing.get("address"), "lat": listing["lat"], "lng": listing["lng"], "source": "known_fact"}
    answers = {**session["answers"], "location": centre}
    conn.execute("UPDATE consult_session SET answers = %s WHERE id = %s", (Jsonb(answers), session["id"]))


def forget_centre(conn: psycopg.Connection, business_id: str) -> bool:
    """A centre read from a listing that is gone (an unlink, plan 2026-10-03-agent-listing-confirm): the open or ready
    consultation's pre-answer of where the ads centre is dropped, so the question is asked again, and a session that
    was ready is open again. An answer the owner gave stays. Whether anything was dropped. Call it only when the centre
    was the listing's own: a place the owner put on the map has the same shape."""
    s = open_for(conn, business_id)
    answer = ((s or {}).get("answers") or {}).get("location") or {}
    if s is None or answer.get("source") != "known_fact" or answer.get("lat") is None:
        return False
    answers = {qid: a for qid, a in s["answers"].items() if qid != "location"}
    conn.execute("UPDATE consult_session SET answers = %s, status = 'open' WHERE id = %s", (Jsonb(answers), s["id"]))
    return True


RETRY_KEY = "_retry"  # answers[RETRY_KEY][qid] = how many times a free text was not understood


class SupportAsk(Exception):
    """The owner's words ask for a person, for help this conversation cannot give, or say they are unhappy (spec
    2026-09-30 §4.1): the caller opens a support ticket instead of recording an answer."""

    def __init__(self, kind: str, summary: str | None, text: str) -> None:
        super().__init__(kind)
        self.kind, self.summary, self.text = kind, summary, text


class ListingNotMine(Exception):
    """The owner's words say the Google Maps place linked to the business is not theirs (plan
    2026-10-03-agent-listing-not-mine-words, D1): the caller unlinks it (listings.unlink_and_say) instead of recording
    an answer. Raised only for a business with a listing the owner confirmed (`parse`'s `linked`: the server decides,
    D2); `text` is the words."""

    def __init__(self, text: str) -> None:
        super().__init__("the listing is not the business")
        self.text = text


# The home's harness (K6 slice 1, spec 2026-10-10 §4.4, D2) read the owner's words in its one Turn call: `answer` runs
# with that reading handed in for the question, in place of read_answer's, and makes no second call. SKIP says the
# words skip the question (a text or address question stores the owner's own skip shape). The auth.with_headers
# pattern: a context variable, set around home_api's `call`, read by _read at every read_answer site.
_READINGS: ContextVar[dict | None] = ContextVar("consult_readings", default=None)
SKIP = object()


@contextlib.contextmanager
def with_reading(readings: dict):
    token = _READINGS.set(dict(readings))
    try:
        yield
    finally:
        _READINGS.reset(token)


def reading_for(qid: str):
    """The reading handed in for this question (an AnswerRead, or SKIP), else None: the model reads the words."""
    held = _READINGS.get()
    return held.get(qid) if held else None


def _read(gen, session: dict, q: dict, options: list[dict], text: str):
    """read_answer, or the reading the harness handed in for this question (with_reading): no model call then."""
    held = reading_for(q["id"])
    if held is not None and held is not SKIP:
        return held
    return gen.read_answer({"id": q["id"], "prompt": prompt_for(q, session), "input": q["input"]}, options, text)


def parse(
    session: dict, q: dict, value: dict, gen, *, support: bool = False, linked: bool = False
) -> tuple[dict | None, str | None]:
    """(stored answer, clarification). A choice validates against the frozen options; free text goes to
    the model once, and on the second miss is kept as a note. With `support`, words that ask for a person
    (the read's `support` is not "none") raise SupportAsk instead: nothing is stored. With `linked` -- the business
    has a listing the owner confirmed -- words the read says call it not theirs raise ListingNotMine next, before
    anything else the read says is used; without, those words are read as any answer. The listing question's words
    never go to the model (`_parse_listing`), nor the website question's (`_parse_website`), nor the business's name
    (`_parse_text`); what it offers goes to the model as any free text does, inside that same function."""
    options = session["options"].get(q["id"], q["options"])
    ids = {o["id"] for o in options}
    if q.get("options_from") == "listing":
        return _parse_listing(session, q, value)
    if q["input"] == "website":
        return _parse_website(session, q, value)
    if reading_for(q["id"]) is SKIP:  # the harness read the words as a skip (D5): the owner's own skip shape
        return {"skipped": True, "raw": str(value.get("other") or "").strip(), "source": "other"}, None
    if q["input"] == "address":
        return _parse_place(session, q, value, gen, support=support, linked=linked)
    if q["input"] == "text":
        return _parse_text(session, q, value, gen, support=support, linked=linked)
    if "choice" in value:
        chosen = value["choice"]
        if q["input"] == "multi":
            # a repeated id counts once: [a, a, b, c] is three picks
            picks = list(dict.fromkeys(c for c in (chosen if isinstance(chosen, list) else [chosen]) if c in ids))
            if not picks:
                return None, "Pick one of the options, or answer in your own words."
            if len(picks) > MAX_LEAD_SERVICES:
                return None, TOO_MANY_PICKS
            return {"choice": picks, "raw": chosen, "source": "choice"}, None
        if chosen not in ids:
            return None, f"Pick one of the options ({', '.join(sorted(ids))}), or answer in your own words."
        stored = {"choice": chosen, "raw": chosen, "source": "choice"}
        opt = next(o for o in options if o["id"] == chosen)
        if opt.get("usd"):
            stored["amount_usd"] = float(opt["usd"])
        if opt.get("miles"):
            stored["miles"] = float(opt["miles"])
        return stored, None
    text = str(value.get("other") or "").strip()
    if not text:
        return None, "Send a choice or a few words."
    if len(text) > MAX_OTHER:
        return None, f"Keep it under {MAX_OTHER} characters."
    read = _read(gen, session, q, options, text)
    if support and read.support != "none":
        raise SupportAsk(read.support, read.support_summary, text)
    if linked and read.listing_not_mine:
        raise ListingNotMine(text)
    if read.understood:
        stored = {"raw": text, "source": "other"}
        if read.option_id in ids:
            stored["choice"] = read.option_id
        if read.amount_usd and read.amount_usd > 0:
            stored["amount_usd"] = float(read.amount_usd)
        if read.miles and 0 < read.miles <= 100:
            stored["miles"] = float(read.miles)
        if read.note and "choice" not in stored:
            stored["note"] = read.note.strip()[:MAX_OTHER]
        if len(stored) > 2:
            return stored, None
    tries = (session["answers"].get(RETRY_KEY) or {}).get(q["id"], 0)
    if tries >= 1:
        return {"note": text, "raw": text, "source": "other"}, None
    return None, "I did not quite get that. In a few words, what should the answer be?"


LISTING_BY_CHOICE = "Pick one of these places, or say none of these."
# A second answer to the listing question in one consultation (fix round 1, the controller's ruling): nothing recorded
LISTING_SETTLED = (
    "Which place on Google Maps is the business is already answered. To change it later, use Settings, or tell your "
    "assistant."
)
# How the owner's words say "None of these" (as profile.norm writes them): "no" only as the whole answer -- it begins
# too many answers that go on to pick a place -- and the rest as the whole answer or anywhere in it
_NONE_ANYWHERE = ("none", "neither", "not mine", "none of these", "not listed", "not on google")
# A yes, as the whole answer (as profile.norm writes it, "please" or "thanks" after it aside): the question is a yes/no
# one when one place is offered, so a yes picks that place; with several, it says not which (final review A2/B I1)
_YES = frozenset(
    {"yes", "yeah", "yep", "yup", "correct", "right", "sure", "it is", "yes it is", "that one", "this one"}
    | {"that s it", "that s us", "that s me", "that s mine", "yes that s us"}
)
_COURTESY = frozenset({"please", "thanks"})
# The words that say yes alone: one before another yes is a yes too ("yes, that's it", "yep, that one")
_YES_WORDS = frozenset({"yes", "yeah", "yep", "yup"})
# A place by its number in the list: these, or a digit; the words around them that say nothing more -- a yes too
_ORDINALS = {"first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}
_AROUND_A_NUMBER = frozenset(
    {"the", "one", "option", "number", "place", "it", "s", "its", "is", "that", "this", "i", "m"}
    | {"pick", "choose", "take", "go", "with", "please", "thanks"}
    | {"yes", "yeah", "yep", "yup", "correct", "right", "sure"}
)
# Words that say no: beside a place, the place is not picked (unclear, asked again)
_NEGATIONS = frozenset({"no", "not", "nope", "never", "wrong", "isn", "isnt", "neither", "none"})


def _a_yes(words: str) -> bool:
    """Whether normalized words are a yes alone (_YES), "please" or "thanks" after it aside -- a word of _YES_WORDS
    before another yes too ("yes, that's it")."""
    said = words.split()
    while said and said[-1] in _COURTESY:
        said.pop()
    if len(said) > 1 and said[0] in _YES_WORDS and " ".join(said[1:]) in _YES:
        return True
    return " ".join(said) in _YES


def _listing_words(text: str, offered: list[dict]) -> str | None:
    """The option the owner's words pick among `offered` (the listing question's frozen options, "None of these"
    aside): LISTING_NONE's id for a way of saying none of these; the one place offered for a yes (`_a_yes`); an offered
    place by its number in the list ("2", "the second one", "yes, the first") or by its name in the words (the
    option's label up to the address: places.label; a name in the words only as part of another offered name gives way
    to it); None when they pick nothing clearly -- no place, two places, a place and a "no", a place and none of these,
    or a yes when several places are offered."""
    words = profile.norm(text)
    if _a_yes(words):
        return offered[0]["id"] if len(offered) == 1 else None
    padded = f" {words} "
    picked: set[str] = set()
    rest = [w for w in words.split() if w not in _AROUND_A_NUMBER]
    if len(rest) == 1:
        n = _ORDINALS.get(rest[0]) or (int(rest[0]) if rest[0].isdigit() else 0)
        if 1 <= n <= len(offered):
            picked.add(offered[n - 1]["id"])
    names = {o["id"]: f" {profile.norm(o['label'].split(', ', 1)[0])} " for o in offered}
    named = {i: name for i, name in names.items() if name.strip() and name in padded}
    picked |= {i for i, name in named.items() if not any(name != other and name in other for other in named.values())}
    if words == "no" or any(f" {phrase} " in padded for phrase in _NONE_ANYWHERE):
        return None if picked else LISTING_NONE["id"]
    if len(picked) == 1 and not _NEGATIONS & set(words.split()):
        return picked.pop()
    return None


def _parse_listing(session: dict, q: dict, value: dict) -> tuple[dict | None, str | None]:
    """Which place is the business (plan 2026-10-03-agent-listing-confirm, D4): a pick among the frozen options, or
    words read as one -- here, never by the model, which never reads a place a lookup found before the owner picked it
    (`_listing_words`; support asks are not looked for in them); words that pick nothing clearly twice answer the
    question with no place picked (`skipped`) -- never a place the owner did not pick, and never "None of these",
    which they did not say either (final review A2/B I2)."""
    options = session["options"].get(q["id"]) or []
    ids = {o["id"] for o in options}
    if "choice" in value:
        if value["choice"] not in ids:
            return None, LISTING_BY_CHOICE
        return {"choice": value["choice"], "raw": value["choice"], "source": "choice"}, None
    text = str(value.get("other") or "").strip()
    if not text:
        return None, LISTING_BY_CHOICE
    if len(text) > MAX_OTHER:
        return None, f"Keep it under {MAX_OTHER} characters."
    pick = _listing_words(text, [o for o in options if o["id"] != LISTING_NONE["id"]])
    if pick is not None:
        return {"choice": pick, "raw": text, "source": "other"}, None
    if (session["answers"].get(RETRY_KEY) or {}).get(q["id"]):
        return {"skipped": True, "raw": text, "source": "other"}, None
    return None, LISTING_BY_CHOICE


PLACE_BY_CHOICE = "Answer in your own words: an address, or a city and ZIP code."


def _parse_place(
    session: dict, q: dict, value: dict, gen, *, support: bool, linked: bool
) -> tuple[dict | None, str | None]:
    """The location question: words, or the one option a town the opening words named offers (STATED_PLACE), whose
    town is the note as words naming it would be. The reader names the place in `note` (an address, a ZIP code or
    a city); words that name none -- the reader did not take them as an answer: the owner would rather say later --
    skip the question at once, kept as they were said. Words the reader took as an answer without naming a place are
    no skip (final review A3): the question is asked again (PLACE_BY_CHOICE), and once it was, the owner's own words
    go to the map, as `parse` keeps words on a second miss. The map lookup happens in the command's prepare
    (tools_consult.answer), not here."""
    if "choice" in value:
        town = next((o for o in session["options"].get(q["id"], []) if o.get("id") == value["choice"]), None)
        if town is None or not town.get("place"):
            return None, PLACE_BY_CHOICE
        return {"note": town["place"], "raw": town["label"], "source": "choice"}, None
    text = str(value.get("other") or "").strip()
    if not text:
        return None, "Send a few words: an address, or a city and ZIP code."
    if len(text) > MAX_OTHER:
        return None, f"Keep it under {MAX_OTHER} characters."
    read = _read(gen, session, q, [], text)
    if support and read.support != "none":
        raise SupportAsk(read.support, read.support_summary, text)
    if linked and read.listing_not_mine:
        raise ListingNotMine(text)
    if not read.understood:
        return {"skipped": True, "raw": text, "source": "other"}, None
    note = (read.note or "").strip()
    if not note and not (session["answers"].get(RETRY_KEY) or {}).get(q["id"]):
        return None, PLACE_BY_CHOICE
    return {"note": (note or text)[:MAX_OTHER], "raw": text, "source": "other"}, None


# Ruling F4: what ends the first paragraph of an answer to the website question -- a blank line, or a line an e-mail's
# signature or a phone's sign-off starts with ("--", an em dash, "Sent from"; the mail channel takes off only a
# signature after "-- ") -- and the ways its first line says the business has none: a first word that says no, or a
# phrase anywhere in that line
_SIGNATURE_STARTS = ("--", "\u2014", "sent from")
_NO_WORDS = frozenset({"no", "nope", "nah", "none", "n/a", "na"})
_NO_PHRASES = ("don't have", "do not have", "dont have", "not yet", "no thanks")
_WORD_EDGES = "\"'()[],.;:!?*"  # around a first word: "No," and "Nope!" are no-words, "na.example.com" is none
# Ruling R0-m (A3p-5 batch 2 Task 0, item b): how a first paragraph that is only a greeting starts ("Hi,", "Hello Admiry
# team,", "Good morning!", "Dear Admiry,"), and the most words it has; the reading skips it once (`first_paragraph`)
_GREETINGS = frozenset({"hi", "hello", "hey", "dear", "greetings", "good"})
MAX_GREETING = 4
# What is taken off a pasted address first (ruling T2-1): the quotes around it, and the punctuation after it when it
# ends a sentence
_SITE_QUOTES = "\"'“”‘’"
_SITE_TRAILING = ".,;:!?)]" + _SITE_QUOTES
# What is taken off each word before it is read as a possible address (Q10, `site_in_words`): the brackets and quotes a
# sentence might wrap one in, and a sentence-ending period -- never a dot in the middle of the word, so
# "brightsmile.com" survives untouched and only the wrapping of "(brightsmile.com)." is lost.
_WORD_PUNCTUATION = "()[]<>'\",;:!?."
# The top-level domains an address among other words may end in without a scheme or `www.` (ruling R2-f): by its
# shape alone a run-on sentence reads as one ("one.Thanks", "St.Louis")
WORD_TLDS = frozenset(
    {"com", "net", "org", "us", "co", "biz", "info", "io", "me", "app", "dev", "site", "online", "store", "shop"}
    | {"dental", "law", "health", "care", "clinic", "services", "pro", "page", "tech", "solutions"}
)


def _says_it_is_a_site(word: str, site: str) -> bool:
    """Whether a word among others that normalize_site took as `site` says it is an address (ruling R2-f): a scheme,
    `www.` in front, or a top-level domain of WORD_TLDS."""
    tld = (urlsplit(site).hostname or "").rsplit(".", 1)[-1]
    return word.lower().startswith(("http://", "https://", "www.")) or tld in WORD_TLDS


def site_in_words(text: str, *, social: bool = True) -> str | None:
    """The first web address among the words of `text`, as normalize_site writes it -- one that says it is an address
    (`_says_it_is_a_site`, ruling R2-f) -- with `social` False a social or listing site's page (is_social) passed over
    for the next one. None when the words name none. `text` is cut to MAX_TEXT before anything else touches it, and
    each whitespace-separated word is tested on its own -- one with an "@" anywhere in it (an e-mail address) skipped
    outright: never one regex over the whole text (Task 1 review round 1 Critical: a single pattern with a repeated
    group and an overlapping mandatory tail was quadratic on crafted input, and the opening words reach the homepage's
    unauthenticated caller with no bound of their own). The one scan of the opening words (tools_start.url_in_words,
    W2) and of the website question's answer (ruling R2-d)."""
    for word in (text or "")[:MAX_TEXT].split():
        if "@" in word:
            continue
        word = word.strip(_WORD_PUNCTUATION)
        site = normalize_site(word)
        if site and _says_it_is_a_site(word, site) and (social or not is_social(site)):
            return site
    return None


def site_words(text: str) -> str | None:
    """The address an answer to the website question is when it is one, as normalize_site writes it -- the quotes
    around it and the punctuation after it taken off first (ruling T2-1) -- or None. One word only: words on two lines
    are two words (a URL parser drops a line break, which would join them into one host)."""
    word = text.strip().lstrip(_SITE_QUOTES).rstrip(_SITE_TRAILING)
    return normalize_site(word) if len(word.split()) == 1 else None


def site_answer(text: str) -> str | None:
    """The address an answer to the website question gives (ruling R2-d): the whole answer when it is one (`site_words`),
    else the first address in its words (`site_in_words`, a social page's included), else None."""
    return site_words(text) or site_in_words(text)


def _paragraph(lines: list[str]) -> tuple[str, list[str]]:
    """The paragraph `lines` start with, blank lines before it aside -- its lines up to the next blank one, or to one
    that starts with "--", an em dash or "Sent from" (any case): an e-mail's signature, never read -- stripped, and the
    lines after it: none once a signature line ended it."""
    at = 0
    while at < len(lines) and not lines[at].strip():
        at += 1
    taken = []
    for i in range(at, len(lines)):
        start = lines[i].strip()
        if start.lower().startswith(_SIGNATURE_STARTS):
            return "\n".join(taken).strip(), []
        if not start:
            return "\n".join(taken).strip(), lines[i + 1 :]
        taken.append(lines[i])
    return "\n".join(taken).strip(), []


def _greeting(paragraph: str) -> bool:
    """Whether a paragraph is only a greeting (ruling R0-m): at most MAX_GREETING words, the first of them (lower case,
    the punctuation around it aside) one of _GREETINGS -- and none of what the answer is read for: no address
    (`site_answer`) and no no (`says_no_website`), so "Hi, it's brightsmile.com" and "Hey, don't have one" are answers
    (ruling R0-m3)."""
    words = paragraph.split()
    if not words or len(words) > MAX_GREETING or words[0].lower().strip(_WORD_EDGES) not in _GREETINGS:
        return False
    return site_answer(paragraph) is None and not says_no_website(paragraph)


def first_paragraph(text: str) -> str:
    """The words of an answer that are read (ruling F4): its first paragraph -- its lines up to the first blank one, or
    to the first that starts with "--", an em dash or "Sent from" (any case): an e-mail's signature, never read --
    stripped. Ruling R0-m (A3p-5 batch 2 Task 0, item b): a first paragraph that is only a greeting (`_greeting`) is
    skipped once and the next one read instead -- empty when nothing but a signature, or nothing at all, follows it --
    and an answer that starts with a signature line, whose first paragraph is empty, is read whole, stripped: an
    e-mail opens with "Hi," or a signature as often as with the answer."""
    first, rest = _paragraph((text or "").strip().splitlines())
    if not first:
        return (text or "").strip()
    return _paragraph(rest)[0] if _greeting(first) else first


def says_no_website(words: str) -> bool:
    """Whether an answer to the website question says the business has none (ruling F4), read from its first line: a
    first word that says no (_NO_WORDS, whole: the punctuation around it aside, never a dot inside it -- an address that
    begins like one, "na.example.com", is an address), or a phrase of _NO_PHRASES anywhere in the line (any case, a
    curly apostrophe read as a straight one). Checked before any address in the words is read."""
    lines = (words or "").strip().splitlines()
    line = lines[0].replace("\u2019", "'").lower() if lines else ""
    first = next(iter(line.split()), "").strip(_WORD_EDGES)
    return first in _NO_WORDS or any(phrase in line for phrase in _NO_PHRASES)


def _no_line_site(words: str) -> str | None:
    """The address of the business's own (`site_in_words`, a social or listing page passed over) that the first line of
    an answer's words names: what a line that says no may still give (ruling R0-m) -- "No, it's newsite.com"."""
    lines = words.splitlines()
    return site_in_words(lines[0], social=False) if lines else None


def _answers_after_a_no(session: dict, site: str, known: dict | None) -> bool:
    """Whether a site of the business's own that a first line saying no names (`_no_line_site`) is the answer: with the
    site the reading saw offered (`known`, SITE_KNOWN), one on another host -- a no that names the offered one ("No,
    oldsite.com is gone") stays a no (ruling R0-m); else, in the question that asks again for an address `start` could
    not open (`site_unopened`), any -- "No, it's brightsmile.com" corrects it, and "No, brightsmlie.com is right" names
    it again, which the command never fetches twice (A3p-5 batch 2 Task 0 fix round 1, ruling R0-i3)."""
    if known is not None:
        from . import listings  # local: listings imports this module

        return not listings.same_site(site, known["url"])
    return site_unopened(session.get("known_facts")) is not None


def may_read_a_site(text: str) -> bool:
    """Whether words answering the website question may have a site read, told without the session (the command flags
    its audit row before it opens one: tools_consult._free_text): never when they say there is none
    (`says_no_website`) and name no address of the business's own on that line; always for an address of the business's
    own (`site_answer`, a social page aside), and for a yes or the site_known option's "Yes: <host>" label, which pick
    the site the reading saw when it is offered -- a yes with none offered is counted all the same: the count of model
    calls never falls short of the calls made. A no whose line names an address of the business's own is counted too
    (ruling R0-m1): it is read when a site on another host is offered ("No, it's newsite.com") and in the question that
    asks again for an address `start` could not open ("No, it's brightsmile.com": ruling R0-i3, `_answers_after_a_no`),
    and this flag -- part of the call's replay hash -- cannot see what is offered or asked, so "No, oldsite.com is gone"
    is over-counted by design. "Try again" typed (`retry_typed`) is counted as the tap is: it fetches the address tried
    once more (Task 3 review, ruling I1a), over-counted when no site stands unopened."""
    words = first_paragraph(text)
    if not words:
        return False
    if retry_typed(words) == RETRY:
        return True
    if says_no_website(words):
        return _no_line_site(words) is not None
    if _a_yes(profile.norm(words)) or words.lower().startswith("yes:"):
        return True
    site = site_answer(words)
    return site is not None and not is_social(site)


# What an owner types for Try again or Ask me instead (final review Important 2), as profile.norm writes it: the
# option's own label, or one of these; and the courtesy words around it, at either end
_RETRY_WORDS = {
    RETRY: frozenset({"retry", "try it again", "again", "try once more"}),
    ASK_INSTEAD: frozenset({"ask instead", "ask me", "just ask me"}),
}
_RETRY_COURTESY = _COURTESY | {"ok", "okay"}


def retry_typed(words: str) -> str | None:
    """The id of the option among RETRY_OPTIONS that `words` say, as profile.norm writes them, with "please", "thanks"
    or "ok" at either end aside (`_RETRY_COURTESY`, as `skips` sets its own aside): the option's label ("Try again",
    "ask me instead.") or another way of saying it (`_RETRY_WORDS`: "please retry", "ok, just ask me"); None for
    anything more -- what an owner types for the button (Task 3 review, ruling I1a; final review Important 2)."""
    said = profile.norm(words).split()
    while said and said[0] in _RETRY_COURTESY:
        said.pop(0)
    while said and said[-1] in _RETRY_COURTESY:
        said.pop()
    typed = " ".join(said)
    return next(
        (o["id"] for o in RETRY_OPTIONS if typed == profile.norm(o["label"]) or typed in _RETRY_WORDS[o["id"]]), None
    )


def _tried_again(tried: str, choice: str, raw: str, source: str) -> dict:
    """The website answer Try again (RETRY: the address tried, fetched once more) or Ask me instead (ASK_INSTEAD: kept
    unread, no fetch) gives, tapped or typed (spec 2026-10-12 §3.2, R1)."""
    if choice == RETRY:
        return {"url": tried, "retry": True, "raw": raw, "source": source}
    return {"url": tried, "read": False, "asked": True, "raw": raw, "source": source}


def _parse_website(session: dict, q: dict, value: dict) -> tuple[dict | None, str | None]:
    """The website question (Q6), never read by the model: "No website" (`none`), "Try again" or "Ask me instead"
    (RETRY, ASK_INSTEAD) for a site tried and not read -- the tried address, or NOT_A_SITE with nothing to retry (spec
    2026-10-12 §3.2, R1) -- the site the reading saw (SITE_KNOWN, its address on the option), or words -- their first
    paragraph alone, an e-mail's signature under it never read and a greeting before it skipped (`first_paragraph`,
    rulings F4 and R0-m) -- read in this order: while a site stands unopened, Try again or Ask me instead in words
    (`retry_typed`: the label, or another way of saying it), which is that option as the tap is (Task 3 review, ruling
    I1a; final review Important 2); a way of saying there is none (`says_no_website`, before any address) -- save a site
    of the business's own named on that line, which is the answer as a typed address is, when `_answers_after_a_no` says
    so: on another host than the site the reading saw, when that is offered ("No, it's newsite.com": ruling R0-m), or
    any in the question that asks again for an address `start` could not open (ruling R0-i3); with the site the reading
    saw offered, a yes or that option's own label, which picks it as the tap does; an address (`site_answer`: the whole
    answer, else the first address in its words, ruling R2-d; the command reads it in its prepare,
    tools_consult.answer); a page on a social or listing site, kept as the answer's `social` (the command says SOCIAL,
    counts a retry and keeps the address in the session's known facts, for A3p-6: nothing is recorded); or else
    NOT_A_SITE. Nothing to read (a greeting and nothing after it) is asked once more, as an empty answer is. A second
    miss -- words that are no address, nothing to read, or a social page again -- answers the question as no website:
    the words kept as a note (all of them when nothing was read), or the page as `social`. `raw` keeps the words as
    sent."""
    options = session["options"].get(q["id"], q["options"])
    if "choice" in value:
        chosen = next((o for o in options if o["id"] == value["choice"]), None)
        if chosen is None:
            ids = ", ".join(sorted(o["id"] for o in options))
            return None, f"Pick one of the options ({ids}), or answer in your own words."
        if chosen["id"] in (RETRY, ASK_INSTEAD):
            tried = site_unopened(session.get("known_facts"))
            if tried is None:  # nothing to retry (a stale card, an assistant's first choice): the address is asked
                return None, NOT_A_SITE
            return _tried_again(tried, chosen["id"], chosen["id"], "choice"), None
        if chosen["id"] == SITE_KNOWN:
            return {"choice": SITE_KNOWN, "url": chosen["url"], "raw": SITE_KNOWN, "source": "choice"}, None
        return {"choice": "none", "none": True, "raw": "none", "source": "choice"}, None  # "No website", the other one
    text = str(value.get("other") or "").strip()
    if not text:
        return None, "Send a choice or a few words."
    if len(text) > MAX_OTHER:
        return None, f"Keep it under {MAX_OTHER} characters."
    words = first_paragraph(text)
    again = bool((session["answers"].get(RETRY_KEY) or {}).get(q["id"]))
    if not words:  # a greeting and nothing after it (ruling R0-m): an empty answer, then on a second miss no website
        if again:
            return {"note": text, "none": True, "raw": text, "source": "other"}, None
        return None, "Send a choice or a few words."
    tried = site_unopened(session.get("known_facts"))
    if tried is not None and (typed := retry_typed(words)) is not None:  # the button's words: that option (I1a)
        return _tried_again(tried, typed, text, "other"), None
    known = next((o for o in options if o["id"] == SITE_KNOWN), None)
    if says_no_website(words):
        other = _no_line_site(words)
        if other is not None and _answers_after_a_no(session, other, known):  # rulings R0-m and R0-i3
            return {"url": other, "raw": text, "source": "other"}, None
        return {"none": True, "raw": text, "source": "other"}, None
    said = profile.norm(words)
    if known is not None and (_a_yes(said) or said == profile.norm(known["label"])):
        return {"choice": SITE_KNOWN, "url": known["url"], "raw": text, "source": "other"}, None
    site = site_answer(words)
    if site is not None and is_social(site):
        return {"social": site, **({"none": True} if again else {}), "raw": text, "source": "other"}, None
    if site is not None:
        return {"url": site, "raw": text, "source": "other"}, None
    if again:
        return {"note": words, "none": True, "raw": text, "source": "other"}, None
    return None, NOT_A_SITE


# Ways of saying "I would rather not say" to the business's name or what it offers (W4, ruling F5), as profile.norm
# writes them, "thanks" or "please" after them aside: the whole answer only -- "Skip's Plumbing" is a name
_SKIP_WORDS = frozenset(
    {"skip", "pass", "none", "n a", "na", "no name", "we don t have a name yet", "we dont have a name yet"}
)
_RATHER_NOT = re.compile(r"(?:(?:i would|i d) )?(?:rather|prefer) not (?:to )?(?:say|share|tell)")


def skips(text) -> bool:
    """Whether the whole of `text` is one way of saying the owner would rather not say (ruling F5): one of _SKIP_WORDS,
    or "(I would|I'd) (rather|prefer) not (to) (say|share|tell)", after profile.norm, with "thanks" or "please" after
    it aside -- never a word inside a longer answer. Anything but text is no skip."""
    said = profile.norm(text if isinstance(text, str) else "").split()
    while said and said[-1] in _COURTESY:
        said.pop()
    words = " ".join(said)
    return words in _SKIP_WORDS or _RATHER_NOT.fullmatch(words) is not None


def _parse_text(
    session: dict, q: dict, value: dict, gen, *, support: bool = False, linked: bool = False
) -> tuple[dict | None, str | None]:
    """The two questions asked without a site (Q3, WITHOUT_SITE), free text only -- neither offers a choice. Either is
    skipped by an answer that is one way of saying the owner would rather not say (`skips`, ruling F5), before any
    lookup or model call. The business's name (Q11) is never read by the model: the words themselves, stripped and
    clipped to MAX_NAME characters, are the name -- `raw` keeps the words as sent, a skip's too (ruling R3-a), so a
    later correction's "Updated: …" names what now stands (`tools_consult._answer_label`) instead of rendering blank.
    Words that read as a sentence instead of a name (`looks_like_a_sentence`, R2 -- "Why cannot you open it?" never
    becomes the business's name), or that carry a web address (`site_in_words`, a social page passed over: a name
    carries none -- final review Important 1, where the reroute to the website question did not take it), are asked
    once more (NAME_AGAIN); on a second miss the words are kept as the name (R0-l), the retry rule every free-text
    question follows. What it offers (Q12) goes to the model's reader once, as any free text does: its answer's
    `services`, the distinct services the words name; an empty read asks once more, then keeps the words as a note, the
    retry rule every free-text question follows."""
    text = str(value.get("other") or "").strip()
    if not text:
        return None, "Send a choice or a few words."
    if len(text) > MAX_OTHER:
        return None, f"Keep it under {MAX_OTHER} characters."
    if skips(text):
        return {"skipped": True, "raw": text, "source": "other"}, None
    if q["id"] == "business_name":
        tries = (session["answers"].get(RETRY_KEY) or {}).get(q["id"], 0)
        no_name = looks_like_a_sentence(text) or site_in_words(text, social=False) is not None
        if no_name and tries < 1:  # asked once more (R2); the second time the words are the name
            return None, NAME_AGAIN
        return {"name": text[:MAX_NAME], "raw": text, "source": "other"}, None
    read = _read(gen, session, q, [], text)
    if support and read.support != "none":
        raise SupportAsk(read.support, read.support_summary, text)
    if linked and read.listing_not_mine:
        raise ListingNotMine(text)
    services = [s.strip()[:MAX_OTHER] for s in read.services if s and s.strip()]
    if services:
        return {"services": services, "raw": text, "source": "other"}, None
    tries = (session["answers"].get(RETRY_KEY) or {}).get(q["id"], 0)
    if tries >= 1:
        return {"note": text, "raw": text, "source": "other"}, None
    return None, "I did not quite get that. In a few words, what should the answer be?"


def questions(session: dict) -> list[dict]:
    """The questions this session asks, in order (Q3, ruling F1: `tree_questions`): the website question, answered or
    not; the business's name and what it offers while the website is unanswered -- counted from the first turn, so a
    site read later only takes them off and the count never grows -- and after an answer with no site read ("No
    website", or a site that could not be read), never once a site was read; which Google Maps place is the business,
    between the two, only when the session froze places for it; then the trade's own. A session from before a question
    existed (its options frozen without it), or one whose lookup offered nothing, never shows that question -- the
    listing question still counts ahead of its places while a lookup by the name may offer them (`_listing_may_come`:
    next_question's count, never a question asked)."""
    tree = load_tree(session["tree"])
    options = session.get("options") or {}
    read = site_read(session)
    return [
        q
        for q in tree_questions(tree)
        if (not q.get("common") or WEBSITE in options)
        and not (read and q["id"] in WITHOUT_SITE)
        and (q.get("options_from") != "listing" or options.get("listing"))
    ]


def pending(session: dict) -> list[dict]:
    return [q for q in questions(session) if q["id"] not in session["answers"]]


def _listing_may_come(session: dict) -> bool:
    """Whether the listing question counts before it can be asked (ruling F1): a session that asks the business's name
    (the website question's sessions, no site read) while the name is unanswered and the listing question unanswered
    with no place offered yet -- the name's Google Maps lookup may still offer places, and the count the first turn
    gives must not grow when it does. It is never asked: `pending` leaves it out (so next_question's question,
    `_settle` and `remaining` do), and it drops out once the name is answered (the lookup found nothing, the name was
    skipped, or no lookup ran) or a site is read, or becomes the question itself when the lookup offers places. A name
    known at start or from the listing the owner confirmed (`preanswers`), and a server with no map lookup (the
    question skipped there), leave nothing to look up: nothing is counted."""
    options = session.get("options") or {}
    answers = session.get("answers") or {}
    return (
        WEBSITE in options
        and "listing" in options
        and not options["listing"]
        and not site_read(session)
        and "business_name" not in answers
        and "listing" not in answers
    )


def questions_left(session: dict) -> int:
    """How many questions the owner is still to be asked, as the first turn says it ("Before I plan anything, six
    quick questions."; more than six are "a few": tools_start.opening_say, ruling R0-h): the pending ones and the
    listing question while it may still come (`_listing_may_come`) -- the count the first question's card shows."""
    return len(pending(session)) + (1 if _listing_may_come(session) else 0)


# Words that name the visit itself: a service called by one ("New-patient dental appointment") is no kind of patient
VISIT_WORDS = frozenset({"appointment", "consultation", "visit", "booking"})


def _names_the_visit(service: str, noun: str | None) -> bool:
    """Whether a service's name already says what the prompt goes on to say -- the visit (VISIT_WORDS) or the
    prompt's own noun after {service} ("patient", "client") -- so filling it in reads twice."""
    words = {w.removesuffix("s") for w in re.findall(r"[a-z]+", service.lower())}
    return bool(words & (VISIT_WORDS | {noun}))


def prompt_for(q: dict, session: dict) -> str:
    """q's prompt as the owner sees it: {service} filled from the answers so far, but only when they name one
    real service; several, a placeholder ("A special offer"), a service named for the visit or the person the prompt
    names ("New-patient dental appointment") or none drop it ("one new customer"). {place} is the address the ads
    centre on once there is one, as a sentence says it (places.display), else the tree's own noun for the business
    (`place_default`). The location question asks from the town the opening words named, when its option offers one
    (`prompt_stated`). {examples} is the trade's example of what such a business offers (the tree's
    `defaults.services_example`, Q2); a tree with none (the fallback tree, M7) drops the clause (SERVICES_CLAUSE). The
    website question for a site that could not be read (`site_unopened`, the session's known facts) asks again
    instead, in UNOPENED_PROMPT's words -- no {host} of its own: SITE_UNREAD_SAY, said once before the question is
    asked again, already names the host (spec 2026-10-12 §3.2, R1; A3p-5 batch 2 Task 0, ruling R0-i)."""
    if q["input"] == "website" and site_unopened(session.get("known_facts")):
        return UNOPENED_PROMPT
    town = next((o["place"] for o in session["options"].get(q["id"], []) if o.get("id") == STATED_PLACE), None)
    if town and q.get("prompt_stated"):
        return q["prompt_stated"].replace("{stated}", town)
    if "{examples}" in q["prompt"]:
        example = load_tree(session.get("tree") or "fallback")["defaults"].get("services_example")
        return q["prompt"].replace("{examples}", example) if example else q["prompt"].replace(SERVICES_CLAUSE, "")
    svc = session["answers"].get("service") or {}
    picks = _picks(svc)
    opts = {o["id"]: o for o in session["options"].get("service", [])}
    prompt = q["prompt"]
    after = re.search(r"\{service\} ([a-z]+)", prompt)
    noun = after.group(1) if after else None
    one = opts.get(picks[0]) if len(picks) == 1 else None
    if one and not one.get("generic") and not _names_the_visit(one["label"], noun):
        prompt = prompt.replace("{service}", one["label"])
    elif not picks and svc.get("note") and not _names_the_visit(svc["note"], noun):
        prompt = prompt.replace("{service}", svc["note"])
    else:
        prompt = prompt.replace("{service} ", "")
    centre = (session["answers"].get("location") or {}).get("address")
    place = places.display(centre) or q.get("place_default") or "your business"
    return prompt.replace("{place}", place)


def _real_answers(session: dict) -> dict[str, dict]:
    """session["answers"] without RETRY_KEY, whose value is a retry-count dict, not an answer."""
    return {qid: a for qid, a in session["answers"].items() if qid != RETRY_KEY}


def next_question(session: dict) -> dict | None:
    """index/count count only what the owner is actually asked: a known-fact pre-answer (radius read
    from the opening text, or a fact the caller passed in known_facts) fills a slot silently and is
    never one of the "N questions" the owner sees (spec 2026-09-26 §4.2, the "1 / 3" mockup). Only the questions the
    session asks count: a known name whose question a site read took off (Q3) is none of them -- and the listing
    question while a lookup by the name may still offer places (`_listing_may_come`, ruling F1), so the count never
    grows when it does. A question's own placeholder goes to the envelope (Q2: the homepage shows it in the text
    box)."""
    asks = questions(session)
    todo = [q for q in asks if q["id"] not in session["answers"]]
    if not todo:
        return None
    q = todo[0]
    answers = _real_answers(session)
    given = [answers[x["id"]] for x in asks if x["id"] in answers]
    known = sum(1 for a in given if a.get("source") == "known_fact")
    return envelope.question(
        id=q["id"],
        index=len(given) - known + 1,
        count=len(asks) - known + (1 if _listing_may_come(session) else 0),
        prompt=prompt_for(q, session),
        input=q["input"],
        options=session["options"].get(q["id"], q["options"]),
        **({"placeholder": q["placeholder"]} if q.get("placeholder") else {}),
    )


def question_for(session: dict, qid: str) -> dict | None:
    """The question `qid` asked again (the harness's go_to, spec 2026-10-10 K6 §4.4): its envelope as next_question
    writes one, its index its own place among the questions the owner is asked -- a known-fact pre-answer counts for
    none, as next_question counts -- and the same count. None for a question this session does not ask."""
    asks = questions(session)
    q = next((x for x in asks if x["id"] == qid), None)
    if q is None:
        return None
    answers = _real_answers(session)
    # a known fact asked again counts itself, so its index and the count agree ("8 / 8")
    known = {x["id"] for x in asks if answers.get(x["id"], {}).get("source") == "known_fact"} - {qid}
    before = [x for x in asks[: asks.index(q)] if x["id"] not in known]
    return envelope.question(
        id=q["id"],
        index=len(before) + 1,
        count=len(asks) - len(known) + (1 if _listing_may_come(session) else 0),
        prompt=prompt_for(q, session),
        input=q["input"],
        options=session["options"].get(q["id"], q["options"]),
        **({"placeholder": q["placeholder"]} if q.get("placeholder") else {}),
    )


def may_go_to(session: dict) -> list[str]:
    """The questions the owner may return to (K6 §4.2): the asked ones answered already, less the Google Maps place
    (settled once per consultation, LISTING_SETTLED)."""
    return [q["id"] for q in questions(session) if q["id"] in session["answers"] and q.get("options_from") != "listing"]


def option_labels(options: list[dict]) -> str:
    """The options' labels as a sentence lists them: ", " between them, or "; " when a label holds ", " itself (a
    place's name and address: final review B M1), so the reader sees where one ends. home.js's withoutQuestion and the
    transcript read them the same way."""
    sep = "; " if any(", " in o["label"] for o in options) else ", "
    return sep.join(o["label"] for o in options)


def question_sentence(nxt: dict) -> str:
    """A question envelope (next_question's) as one owner sentence, so `say` stands on its own when an
    assistant shows only the text: the prompt, then the option labels (`option_labels`), then the way to answer
    freely. A question with no options (where the ads centre) is an open one; a multi question says how many it takes
    (MAX_LEAD_SERVICES, as `parse` counts them)."""
    q = nxt["question"]
    if not q["options"]:
        return f"{q['prompt']} Answer in your own words."
    up_to = _SPELLED.get(MAX_LEAD_SERVICES, str(MAX_LEAD_SERVICES))
    pick = f"pick up to {up_to}, or answer" if q.get("input") == "multi" else "or answer"
    return f"{q['prompt']} Options: {option_labels(q['options'])} — {pick} in your own words."


def _settle(conn: psycopg.Connection, session: dict) -> dict:
    """status ready when nothing is pending (an open session that was pre-answered in full is ready at once)."""
    if session["status"] == "open" and not pending(session):
        conn.execute("UPDATE consult_session SET status = 'ready' WHERE id = %s", (session["id"],))
        session["status"] = "ready"
    return session


def record(conn: psycopg.Connection, session: dict, parsed: dict[str, dict]) -> dict:
    answers = {**session["answers"], **parsed}
    conn.execute(
        "UPDATE consult_session SET answers = %s, step = step + 1 WHERE id = %s", (Jsonb(answers), session["id"])
    )
    fresh = get(conn, session["business_id"], session["id"])
    return _settle(conn, fresh)


def _still_offered(answer: dict, options: list[dict]) -> bool:
    """Whether every option an answer picked is among `options`; an answer in the owner's own words picked none."""
    ids = {o["id"] for o in options}
    return all(pick in ids for pick in _picks(answer))


def restart(conn: psycopg.Connection, session: dict, *, tree: str, options: dict, answers: dict) -> dict:
    """The same session row started again from a website read mid-consultation (Q7, W3), in the caller's transaction:
    its id -- the one an assistant holds -- unchanged. `tree` and `options` (the reading's) replace the old ones;
    `answers` (the new pre-answers and the website answer) are what it starts from, and every answer the owner gave
    before is kept when the new tree has its question, `answers` holds no answer of the owner's to it, and every option
    it picked is still offered -- over a pre-answer, as the owner's own answer always is -- save an answer to which
    place is the business while the new reading offers places: it answered the places offered then, so the owner
    confirms the new set (ruling R2-i). An earlier pre-answer gives way to the new ones, and the retries are forgotten.
    Step 0, status open, then settled (ready when nothing is left to ask). Returns the session as stored."""
    questions_of = tree_questions(load_tree(tree))
    asks = {q["id"] for q in questions_of}
    asked_again = {q["id"] for q in questions_of if q.get("options_from") == "listing" and options.get(q["id"])}
    given = {qid for qid, a in answers.items() if a.get("source") != "known_fact"}
    kept = {
        qid: a
        for qid, a in _real_answers(session).items()
        if qid in asks
        and qid not in given
        and qid not in asked_again
        and a.get("source") != "known_fact"
        and _still_offered(a, options.get(qid, []))
    }
    conn.execute(
        "UPDATE consult_session SET tree = %s, options = %s, answers = %s, step = 0, status = 'open' WHERE id = %s",
        (tree, Jsonb(options), Jsonb({**answers, **kept}), session["id"]),
    )
    return _settle(conn, get(conn, session["business_id"], session["id"]))


def forget_answer(conn: psycopg.Connection, session: dict, qid: str) -> dict:
    """The session with question `qid`'s answer taken back, in the caller's transaction, so the question is pending
    again and a ready session open again: an address that corrects the website answer and whose first try fails
    reopens the website question, asked again with Try again and Ask me instead (spec 2026-10-12 §3.2, R1; Task 3
    review, ruling I2). Returns the session as stored, settled."""
    conn.execute(
        "UPDATE consult_session SET answers = answers - %s, "
        "status = CASE WHEN status = 'ready' THEN 'open' ELSE status END WHERE id = %s",
        (qid, session["id"]),
    )
    return _settle(conn, get(conn, session["business_id"], session["id"]))


def set_options(conn: psycopg.Connection, session: dict, qid: str, options: list[dict]) -> dict:
    """The session with question `qid`'s frozen options replaced by `options`, the others as they were, in the caller's
    transaction (Task 3: the places a name finds offered by the listing question, the services the owner named leading
    the service question's). Returns the session as stored, settled."""
    frozen = {**(session.get("options") or {}), qid: [dict(o) for o in options]}
    conn.execute("UPDATE consult_session SET options = %s WHERE id = %s", (Jsonb(frozen), session["id"]))
    return _settle(conn, get(conn, session["business_id"], session["id"]))


def add_known_facts(conn: psycopg.Connection, session: dict, facts: dict) -> dict:
    """The session with `facts` merged into its known facts, in the caller's transaction: a page on a social or listing
    site the website question was answered with, kept as `social` for A3p-6 (Q6). Returns the session as stored."""
    known = {**(session.get("known_facts") or {}), **facts}
    conn.execute("UPDATE consult_session SET known_facts = %s WHERE id = %s", (Jsonb(known), session["id"]))
    return get(conn, session["business_id"], session["id"])


def mark_planned(conn: psycopg.Connection, session_id: str, plan_id: str) -> None:
    conn.execute("UPDATE consult_session SET status = 'planned', plan_id = %s WHERE id = %s", (plan_id, session_id))


def value_for_band(tree: dict, band: str) -> tuple[float, str]:
    q = next(q for q in tree["questions"] if q["id"] == "customer_value")
    for o in q["options"]:
        if o["id"] == band and o.get("usd"):
            return float(o["usd"]), "owner"
    return float(tree["defaults"]["customer_value_usd"]), "benchmark"


def _session_value(session: dict, tree: dict, band: str) -> tuple[float, str]:
    """A value band as the session froze it: a band is the owner's answer; "estimate" is the business's own
    typical value when its knowledge had one, else the tree's benchmark."""
    opt = next((o for o in session["options"].get("customer_value", []) if o["id"] == band), None)
    if opt and opt.get("usd"):
        return float(opt["usd"]), ("estimate" if band == "estimate" else "owner")
    return value_for_band(tree, band)


def _picks(stored: dict) -> list[str]:
    """The option ids a stored answer chose: a multi answer's list, a single answer's one id, or none."""
    choice = stored.get("choice")
    return list(choice) if isinstance(choice, list) else [choice] if choice else []


FIT_FLAGS = ("meta_fit", "newsbreak_fit")  # spec 2026-09-29-admiry-channels-design §6.2


def service_flags(session: dict, tree: dict) -> set[str]:
    """The fit flags of the services the owner picked (spec 2026-09-29-admiry-channels-design §6.2), which the
    allocator reads: a tree option carries them in its file; a business's own service -- or an option a session froze
    before the trees had flags -- reads them from the tree option with the same id or label (singular or plural,
    `_same_label`). Any picked service that fits a channel fits the plan; words typed instead of a pick carry none."""
    q = next((q for q in tree["questions"] if q["id"] == "service"), None)
    listed = q["options"] if q else []
    shown = {o["id"]: o for o in (session.get("options") or {}).get("service", [])}
    flags: set[str] = set()
    for pick in _picks((session.get("answers") or {}).get("service") or {}):
        opt = shown.get(pick) or {"id": pick, "label": ""}
        label = _same_label(opt["label"]) if opt["label"] else None
        same = [o for o in listed if o["id"] == opt["id"] or _same_label(o["label"]) == label]
        for o in (opt, *same):
            flags |= {k for k in FIT_FLAGS if o.get(k) is True}
    return flags


def _phrase(labels: list[str]) -> str | None:
    """ "A", "A and B", "A, B and C"."""
    if len(labels) < 2:
        return labels[0] if labels else None
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def facts(session: dict, tree: dict) -> dict:
    """What the plan is built from, with the tree's defaults where the owner gave nothing usable."""
    a = session["answers"]
    objective = (a.get("objective") or {}).get("choice") or "any"
    svc = a.get("service") or {}
    picks = _picks(svc)
    shown = session["options"].get("service", [])
    services = [o["label"] for o in shown if o["id"] in picks] + [p for p in picks if p not in {o["id"] for o in shown}]
    if not services and svc.get("note"):
        services = [svc["note"]]
    cv = a.get("customer_value") or {}
    if cv.get("amount_usd") and cv.get("choice") != "estimate":
        value, source = float(cv["amount_usd"]), "owner"
    else:
        value, source = _session_value(session, tree, cv.get("choice") or "estimate")
    rad = a.get("radius") or {}
    miles = float(rad["miles"]) if rad.get("miles") else None
    if miles is None and rad.get("choice"):
        miles = next(
            (
                float(o["miles"])
                for o in tree["questions"][-1]["options"]
                if o["id"] == rad["choice"] and o.get("miles")
            ),
            None,
        )
    offer = (a.get("offer") or {}).get("note") or (session.get("known_facts") or {}).get("offer")
    return {
        "objective": objective,
        "service": _phrase(services),
        "services": services,
        "customer_value_usd": value,
        "customer_value_source": source,
        "radius_miles": miles,
        "offer": offer,
    }
