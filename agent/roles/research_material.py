"""What the Researcher reads (spec 2026-10-07-admiry-capability-layer-design §4 Researcher row; plan K4 Task 2):
assembled by code, all data between tags. The business as a model reads it (profile.for_model), its trade, its area
and what its plan runs today; the topics asked and the owner's own question; the published cost-per-lead averages for
the trade (benchmark rows); Admiry's reviewed guides (agent/knowledge, K3) -- with AGENT_KNOWLEDGE on only, the files a
plan draft for this business would read (its consultation tree, its channels, `strategist.topics` of its trade) within
a token budget; the businesses nearby on Google Maps -- one Text Search call at most per attempt of the run's job
(research-run tries a job the model could not answer three times), within the day's Places budget and never its last
quarter -- shown by id and distance, never by name; and the first page of up to ten of their websites, by the crawl's
own guards and caps, untrusted (`pages`, a tag of their own). Every source has an id the model cites (`sources`); a
source's own words are what a finding's numbers are checked against, beside the counts the material gives (`counts`)
and how many businesses nearby the finding's own sources are from (`businesses`).

`more` is the confidence loop's fetch_more (§4.1): every business the one call found, more of their pages up to
MAX_PAGES in the run, and the same guides within twice the budget. Names, addresses, websites and phones from Places
live in memory for one run (Google's terms keep place ids, not the rest): the digest stored with the run keeps ids,
kinds, labels and links. What an owner may read of a source is words about it, never the business's own: a business
nearby, and its page, are labelled from their place ("The website of a business nearby, 2.0 miles away") and link to
that place on Google Maps -- never by the page's title or host (a small business's title is its name), never to its
website -- and a trade is named in words, never by the id the benchmark and the consultation trees use. Research never
reads a page Admiry hosts (`admiry_hosts`) and never mentions Admiry (`admiry_in`, researcher's guards), so it cannot say
whether a business nearby uses Admiry (founder ruling 2026-10-09, the B0 R3 boundary).

A business nearby is described, never named (plan decision D5; the final review's I1 and the re-review's N1, M-1 to
M-4). `Gathered.names` holds every form of the name of each business the one call found and of the title of each of
their pages read: the whole, and each part of it split at " - ", an en or em dash, "|", "·", "•", ":", ",", "#" (a store
number), " of " or " at "; each of those also without its trailing trade words, legal or professional SUFFIXES and
numbers (a branch's), one at a time ("Smith & Jones Dental": "smith and jones"; "Aspen Dental 4512": "aspen dental",
"aspen") -- never by taking off any other word; a part that starts with an honorific (HONORIFICS; "Doctor" is "Dr")
also without it; and the person a part names -- after an honorific anywhere in it, the words up to a possessive or a
word of the trade ("Meet Dr. Raj Patel"), or before a doctor's suffix at its end, back to a word of the trade ("Jane
Doe, DDS") -- whole and as the honorific ("dr" for a suffix) with the last of them ("jane doe", "dr doe"). A name and a
text are read alike (`words`): accents and case aside, & as "and", a possessive's 's left out, initials ("D.D.S.") as one
word, anything else as a space; a form is found as whole words. A form made only of words any business of the trade
here may have in its name (`Vocabulary`: the business's trade words, its own words for its services and its searches --
its services' names and searches, its plan's services and the searches it runs and keeps out -- its town and area
words, SERVICE_WORDS, STOPWORDS, HONORIFICS, SUFFIXES, numerals) is a description, not a brand: it names a business only
written as a proper noun inside a sentence -- every word of it a sentence would not capitalise anyway (any but a town's,
a stopword, an honorific, a suffix, a numeral) written with a capital, and one of them not the first word of a sentence
(nor after "(", ":" or a quote, where a capital says nothing) -- and never inside a search. A form of one word (stopwords
aside) names a business only written with a capital -- wherever it is in a sentence, first included (N1), a script
without capitals as it is written -- and never inside a search; one that is generic, or shorter than MIN_NAME, is none.
A form made only of town and area words, stopwords and numerals is none: a town is not a business. Any other form, and
a person's name, names a business wherever it is written: a finding, a search to aim for or to keep out, an offer angle,
an unknown -- but a form of a page's title only when it is its business's brand (the coordinator's ruling on concern
(a), as M-4 tightened it, `_branded`): a word of it of MIN_NAME letters or more that no business here has in its name is
a word of the business's Places name or is in the first label of its website's host ("Downtown Dental" ~
downtowndental.com; never a short word, "New", nor a label named for the town, paloalto.brightnow.com); any other form
of a title -- a tagline ("New Patients") or a brand the business took since ("Zenith Smiles" on Old Mill Dental's page)
-- is read as a description. A form found inside the business's own name names no rival. What is stored is masked by
the same forms (`mask_names`): what the guards dropped, and the notes about it (their feedback, which never writes a
name, and the Critic's), however a name that is no description is written there, and a description wherever it has
its capitals (`strict`); what they passed, as it is. A name the forms do not know -- a short brand a page uses, a person
a page names, a new brand or a one-word name in a search -- is the Critic's to send back: the research rubric's
serious `unnamed` check (contracts.RESEARCH)."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg

from .. import (
    audit,
    auth,
    business,
    config,
    consult,
    crawl,
    geo,
    geocode,
    knowledge_index,
    listings,
    places,
    plans,
    profile,
)
from ..channels import registry
from . import strategist

log = logging.getLogger(__name__)

# the research brief's topics (request_research's `topics`) -- not K3's knowledge topics, which strategist.topics picks
TOPICS = ("competitors", "search_phrases", "local_demand", "seasonality", "offer_angles")
TOPIC_WORDS = {
    "competitors": "what other businesses in the trade nearby offer and say about themselves",
    "search_phrases": "what customers here type when they need the service, and what to keep the ads away from",
    "local_demand": "when and why people here need the service",
    "seasonality": "the busy and the quiet times of the year for the trade",
    "offer_angles": "offers and reasons to choose this business that fit the trade's rules",
}
COMPETITORS_FETCHED = 10  # the one Places call asks for this many
COMPETITORS_SHOWN = 5  # round one shows this many; fetch_more shows them all
MAX_PAGES = 10  # pages a run tries at most (spec §4), one per site
ROUND1_PAGES = 5
PAGE_CHARS = 2500  # a page's words the model reads (crawl.PER_PAGE_CHARS caps the crawl itself)
KNOWLEDGE_TOKENS = 1500  # retrieval's budget in round one (spec §6.1: ≤ 1.5k per call)
MORE_KNOWLEDGE_TOKENS = 3000
PLACES_SHARE = 0.75  # research never takes the last quarter of the day's Places lookups: those stay for owners
# Founder ruling 2026-10-09 (the B0 R3 boundary): research never says whether a business nearby uses Admiry -- it never
# reads a page on a host of Admiry's own (admiry_hosts: every name under ADMIRY_DOMAIN and the hosts the agent serves)
# and never mentions Admiry (admiry_in: the word, any case)
ADMIRY_DOMAIN = "admiry.ai"
ADMIRY = "admiry"
MIN_NAME = 4  # a one-word name, or a website's host, shorter than this is not looked for ("CA", "Jo")
# The words a name of a business nearby is read by (the module docstring): legal and professional suffixes, the
# honorifics a person's name starts with, and the words any business of a trade may have in its name beside its own
# trade's, services' and town's -- a short list on purpose: each word in it makes a name of only such words a
# description (written as a proper noun, it still names its business)
SUFFIXES = frozenset({"llc", "inc", "pc", "pllc", "dds", "dmd", "md", "co", "ltd", "corp", "esq"})
DOCTOR_SUFFIXES = frozenset({"dds", "dmd", "md"})  # a name ending in one is a doctor's: "Jane Doe, DDS" is Dr. Doe
HONORIFICS = frozenset({"dr", "mr", "mrs", "ms"})  # "Doctor" is read as "dr" (`words`)
STOPWORDS = frozenset(
    {"a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is", "it", "its", "my", "of", "on", "or", "our"}
    | {"the", "to", "us", "we", "with", "you", "your"}
)
# the brief's fifteen (final review I1), and five words any local service's name or search has: "dental care", "plumbing
# services", "dental clinic", "dental office", "24 hour plumber"
SERVICE_WORDS = frozenset(
    {"emergency", "repair", "family", "general", "cosmetic", "pediatric", "residential", "commercial", "local", "same"}
    | {"day", "near", "me", "best", "affordable", "service", "care", "clinic", "office", "hour"}
)
BUSINESS_LABEL = "What the owner and the business's website told Admiry"
NEARBY_LABEL = "A business nearby on Google Maps"
NEARBY_PAGE_LABEL = "The website of a business nearby"  # never the page's title or its host: a title is a name
# what stands for a business nearby's name in the words that are kept (mask_names)
NEARBY_MASK = "[a business nearby]"
# A trade as an owner reads it: the benchmark's category ids (benchmark.CATEGORIES, and the `<category>_<trade>` rows it
# keeps for a trade of its own) are codes. A category this table lacks reads as its id's words (trade_words).
CATEGORY_WORDS = {
    "home_services": "home services",
    "attorneys": "attorneys",
    "finance_insurance": "finance and insurance",
    "real_estate": "real estate",
    "dentists": "dentists",
    "physicians": "physicians",
    "health_fitness": "health and fitness",
    "automotive_repair": "auto repair",
    "animals_pets": "animals and pets",
    "personal_services": "personal services",
    "fallback": "local businesses",
    "home_services_plumbing": "plumbing",
    "home_services_hvac": "heating and cooling",
    "home_services_roofing": "roofing",
    "home_services_painter": "painting",
    "home_services_landscaper": "landscaping",
    "home_services_locksmith_handyman": "locksmith and handyman work",
    "attorneys_estate": "estate law",
    "attorneys_personal_injury": "personal injury law",
}
# A knowledge file's topic as an owner reads it; a topic not here reads as its slug's words
GUIDE_TOPIC_WORDS = {
    "google-search": "Google Search",
    "newsbreak": "NewsBreak",
    "hvac": "heating and cooling",
    "remodel": "remodeling",
}
# where a name splits into parts (the module docstring): a spaced hyphen, an en or em dash, |, ·, •, :, a comma, a "#"
# (a store number: "Aspen Dental #4512"), " of ", " at "
_PARTS = re.compile(r"\s+-\s+|\s*[–—]\s*|[|·•:,#]|\s+(?:of|at)\s+", re.IGNORECASE)
# a word of a text (`words`): initials ("D.D.S.") as one; a word with a possessive 's after it; & alone
_TOKEN = re.compile(
    r"(?P<initials>(?:[^\W\d_]\.)+[^\W\d_]\.?(?![^\W_]))"
    r"|(?P<word>[^\W_]+)(?P<possessive>['’]s(?![^\W_]))?"
    r"|(?P<amp>&)"
)
_STARTS = re.compile(r"[.!?:\"“(]")  # one of these before a word: the word starts a sentence (or a quote)
_ENDINGS = ("istry", "ist", "ic", "er", "ing", "al")  # a trade's word's endings (_stem): dentist, dental, plumber


@dataclass(frozen=True)
class Competitor:
    """One business nearby as the one Places call found it -- held for the run; only its place id and distance are
    ever stored, or printed: its name and its website are left out of its repr, so a log line cannot carry them."""

    place_id: str
    name: str = field(repr=False)
    website: str | None = field(repr=False)
    miles: float | None


@dataclass
class Gathered:
    """One run's material, grown in place by `more`. What other businesses' pages say (`pages`, `sources`) is left out
    of its repr, as the names and websites are (Competitor)."""

    business_id: str
    vertical: str
    category: str
    topics: list[str]
    channels: list[str]
    base: dict  # business, trade, area, plan, topics, owner_asked: fixed for the run
    benchmark: list[dict]
    competitors: list[Competitor]
    places: str  # called | none_found | no_key | no_area | budget
    trade: str | None = None  # strategist.trade: the home-services trade whose file the guides include
    knowledge_on: bool = False  # AGENT_KNOWLEDGE (K3 P9): off, no guide is read
    sources: dict[str, dict] = field(default_factory=dict, repr=False)  # id -> {"kind", "label", "url", "text"}
    shown: int = 0
    pages: list[dict] = field(default_factory=list, repr=False)  # [{"id", "of", "title", "text"}]: what <pages> holds
    tried: set[int] = field(default_factory=set)  # the competitors (1-based) whose page this run tried
    knowledge: dict | None = None  # {"text", "ids"} retrieved within knowledge_tokens
    knowledge_tokens: int = KNOWLEDGE_TOKENS
    fetched_more: bool = False
    admirys: tuple[str, ...] = (ADMIRY_DOMAIN,)  # Admiry's own hosts (admiry_hosts): no page on one is read

    @property
    def material(self) -> dict:
        """What the model reads in <material>: never a business nearby's name, address or website."""
        page_of = {p["of"]: p["id"] for p in self.pages}
        return {
            **self.base,
            "benchmark": self.benchmark,
            "knowledge": self.knowledge,
            "competitors": [
                {"id": f"place:{k}", "miles": c.miles, "page": page_of.get(f"place:{k}")}
                for k, c in enumerate(self.competitors[: self.shown], 1)
            ],
            "sources": [{"id": i, "kind": s["kind"], "label": s["label"]} for i, s in self.sources.items()],
        }

    @property
    def vocabulary(self) -> Vocabulary:
        """The words any business of this business's trade here may have in its name (the module docstring): its trade
        -- its benchmark category's words and its sub-trades' (CATEGORY_WORDS), its own words for what it does, the
        trade its guides are read for -- its own words for its services and its searches (its services' names and their
        searches, the services, the searches and the searches kept out its plan runs: a rival named for one of them is a
        description, the re-review's M-3), and its town, its service areas, its state and the places of its location
        (all but the street of an address: Places is searched in the location when no town can be read from it). What is
        stored can be any shape: only text is read."""
        area = _dict(self.base.get("area"))
        told = _dict(self.base.get("business"))
        plan = _dict(self.base.get("plan"))
        services = [s for s in _list(told.get("services")) if isinstance(s, dict)]
        family = (
            [trade_words(self.category)]
            + [said for key, said in CATEGORY_WORDS.items() if key.startswith(f"{self.category}_")]
            if isinstance(self.category, str)
            else []
        )
        return Vocabulary.of(
            trade=[*family, self.base.get("trade"), self.trade, GUIDE_TOPIC_WORDS.get(self.trade or "")],
            services=[
                *(s.get("name") for s in services),
                *(phrase for s in services for phrase in _list(s.get("intent_phrases"))),
                *(phrase for key in ("services", "searches", "kept_out") for phrase in _list(plan.get(key))),
            ],
            area=[
                area.get("town"),
                *_list(area.get("service_area")),
                places.state(area.get("location")),
                *_places_of(area.get("location")),
            ],
        )

    @property
    def names(self) -> Names:
        """What names a business nearby (the module docstring): the forms of the name of every business the one call
        found -- shown or not -- and of the title of every page read, each title read beside its business's Places
        name and website (a part of it is a brand only when it shares a word with them), and the business's own name,
        which names no rival. In memory only, as the names are."""
        place = {f"place:{k}": c for k, c in enumerate(self.competitors, 1)}
        titles = [
            (p.get("title"), c.name if c else None, host(c.website) if c else None)
            for p in self.pages
            for c in (place.get(p.get("of")),)
        ]
        return names_of(
            [c.name for c in self.competitors],
            self.vocabulary,
            titles=titles,
            own=_dict(self.base.get("business")).get("name"),
        )

    @property
    def hosts(self) -> tuple[str, ...]:
        """Every business nearby's website host, without `www.`, each once. A host names a business as its name does:
        a sentence or a search that holds one is dropped (host_in) and what a run keeps has it masked (mask_hosts).
        Not a host too short to look for, nor one that is no one business's (listings.SHARED_HOSTS: a Facebook page, a
        booking page), which names none."""
        found = (host(c.website) for c in self.competitors)
        return tuple(dict.fromkeys(h for h in found if h and len(h) >= MIN_NAME and h not in listings.SHARED_HOSTS))

    def counts(self) -> set[Decimal]:
        """The counts code knows, which a sentence may state though no source writes them: how many businesses nearby
        are shown and were found, how many pages were read, the plan's radius."""
        radius = (self.base.get("area") or {}).get("radius_miles")
        known = {self.shown, len(self.competitors), len(self.pages)}
        return {Decimal(str(x)) for x in known | ({radius} if radius is not None else set())}

    def businesses(self, ids: Iterable[str]) -> int:
        """How many businesses nearby the sources `ids` are from -- the count a finding citing them may state, as in "two
        of the five nearby practices" (the final review's I2): a place and its own page are one business, a page the
        material cannot tie to its place is one of its own, and any other source (the business, a benchmark row, a
        guide) is none."""
        page_of = {p.get("id"): p.get("of") for p in self.pages}
        found = set()
        for i in ids:
            kind = (self.sources.get(i) or {}).get("kind")
            if kind == "place":
                found.add(i)
            elif kind == "page":
                found.add(page_of.get(i) or i)
        return len(found)


def core(name: str) -> str:
    """A business's name as `_is_the_business` compares it with the business's own: before any comma, without full
    stops and trailing suffixes. What is no text (a stored value can be anything) has no name."""
    if not isinstance(name, str):
        return ""
    kept = name.split(",")[0].replace(".", "").split()
    while kept and kept[-1].lower() in SUFFIXES:
        kept.pop()
    return " ".join(kept)


def _dict(value: object) -> dict:
    """`value` when it is an object, else an empty one: what is stored can be any shape."""
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    """`value` when it is a list, else an empty one."""
    return value if isinstance(value, list) else []


def _places_of(location: object) -> list[str]:
    """The parts of a location that name places: all but the first of an address of several (Google Maps writes the
    street first), the whole of one of a single part (a bare city). None of what is no text."""
    parts = [p for p in location.split(",") if p.strip()] if isinstance(location, str) else []
    return parts[1:] if len(parts) > 1 else parts


@dataclass(frozen=True)
class _Word:
    """One word of a text as `words` reads it, with where it is in the text and how it is written there."""

    word: str  # read: casefolded, accents aside; & is "and", "doctor" is "dr"
    start: int  # where it is in the text as given (a possessive's 's is not part of it)
    end: int
    upper: bool  # written with a capital, or in a script that has none ("微笑牙科": a name is written so there)
    first: bool  # the first word of the text or of a sentence (or a quote) in it
    possessive: bool  # an 's after it


def _plain(text: str) -> tuple[str, list[int]]:
    """`text` with its accents taken off (NFKD, the combining marks left out) and, for each character of that, the
    index of the character of `text` it comes from."""
    out: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch):
            if not unicodedata.combining(c):
                out.append(c)
                where.append(i)
    return "".join(out), where


def _read(text: object) -> list[_Word]:
    """The words of `text` (the module docstring): none of what is no text."""
    if not isinstance(text, str):
        return []
    plain, where = _plain(text)
    out: list[_Word] = []
    after = 0
    for m in _TOKEN.finditer(plain):
        if m.group("amp"):
            surface, word, stop = "&", "and", m.end()
        elif m.group("initials"):
            surface, stop = m.group("initials"), m.end()
            word = surface.replace(".", "").casefold()
        else:
            surface, stop = m.group("word"), m.end("word")
            word = surface.casefold()
        out.append(
            _Word(
                word="dr" if word == "doctor" else word,
                start=where[m.start()],
                end=where[stop - 1] + 1,
                upper=not surface[:1].islower(),
                first=not out or _STARTS.search(plain, after, m.start()) is not None,
                possessive=m.group("possessive") is not None,
            )
        )
        after = m.end()
    return out


def words(text: object) -> tuple[str, ...]:
    """`text`'s words as a name and a sentence are both read: casefolded, accents aside, & as "and", "doctor" as "dr",
    a possessive's 's left out, initials ("D.D.S.") as one word and anything else as a space; () for what is no text."""
    return tuple(w.word for w in _read(text))


def _stem(word: str) -> str:
    """A word as the generic words are kept: without a plural s, then without one of a trade's endings -- dentists,
    dentistry and dental are one word; so are plumber, plumbers and plumbing -- never under three letters."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


_SERVICE_STEMS = frozenset(_stem(w) for w in SERVICE_WORDS)


@dataclass(frozen=True)
class Vocabulary:
    """The words any business of one trade in one place may have in its name, each kept by its stem (`_stem`): the
    trade's (`trade`, which also come off the end of a name), its services' (`services`) and the place's (`area`) --
    beside STOPWORDS, SERVICE_WORDS, HONORIFICS, SUFFIXES and numerals, which every vocabulary has."""

    trade: frozenset[str] = frozenset()
    services: frozenset[str] = frozenset()
    area: frozenset[str] = frozenset()

    @classmethod
    def of(cls, *, trade: Iterable = (), services: Iterable = (), area: Iterable = ()) -> Vocabulary:
        """The vocabulary of these texts' words (stopwords aside): what is no text is none."""

        def stems(texts: Iterable) -> frozenset[str]:
            return frozenset(_stem(w) for text in texts for w in words(text) if w not in STOPWORDS)

        return cls(stems(trade), stems(services), stems(area))

    def is_trade(self, word: str) -> bool:
        return _stem(word) in self.trade

    def is_area(self, word: str) -> bool:
        return _stem(word) in self.area

    def is_generic(self, word: str) -> bool:
        """Whether any business of the trade here may have `word` in its name."""
        if word.isdigit() or word in STOPWORDS or word in HONORIFICS or word in SUFFIXES:
            return True
        stem = _stem(word)
        return stem in self.trade or stem in self.services or stem in self.area or stem in _SERVICE_STEMS

    def is_plain(self, word: str) -> bool:
        """Whether a sentence writes `word` the same in a name and out of one: a numeral, a stopword, an honorific, a
        suffix, a town's word. A proper noun's other words are what its capitals are read on."""
        return word.isdigit() or word in STOPWORDS or word in HONORIFICS or word in SUFFIXES or self.is_area(word)


@dataclass(frozen=True)
class Form:
    """One way a sentence can write the name of a business nearby: its words, as `words` reads them; whether it names
    the business only written as a proper noun inside a sentence, never in a search (`proper`: one word, or a
    description); which of its words such a proper noun writes with a capital (`capitals`); and whether it is a
    description (`generic`: words any business here may have, or a part of a title that is not its business's brand),
    whose capital on a sentence's first word says nothing."""

    words: tuple[str, ...]
    proper: bool = False
    capitals: tuple[bool, ...] = ()
    generic: bool = False


@dataclass(frozen=True)
class Names:
    """What names a business nearby (`names_of`): the forms of their names, and the business's own name (`own`, its
    words whole and without its trade's last words), inside which a form names no rival."""

    forms: tuple[Form, ...] = ()
    own: tuple[tuple[str, ...], ...] = ()


def _form(found: tuple[str, ...], vocabulary: Vocabulary) -> Form | None:
    """The form these words are, or None when they name no business: none at all, a town's words, stopwords and
    numerals only, or one word (stopwords aside) that any business here may have or that is shorter than MIN_NAME. A
    form of one word names a business only written with a capital (wherever it is in the sentence); one of words any
    business here may have only written as a proper noun (a description)."""
    if not found or all(w.isdigit() or w in STOPWORDS or vocabulary.is_area(w) for w in found):
        return None
    capitals = tuple(not vocabulary.is_plain(w) for w in found)
    kept = [w for w in found if w not in STOPWORDS]
    if len(kept) == 1:
        if vocabulary.is_generic(kept[0]) or len(kept[0]) < MIN_NAME:
            return None
        return Form(found, True, capitals)
    generic = all(vocabulary.is_generic(w) for w in found)
    return Form(found, generic, capitals, generic)


def _trims(found: tuple[str, ...], vocabulary: Vocabulary) -> list[tuple[str, ...]]:
    """The words without their last one, then the one before, while each is a trade's word, one of SUFFIXES or a number
    (a branch's or a store's) -- never another word: each shorter one in turn ("Rival Dental 2, DDS" read as one: "rival
    dental 2", "rival dental", "rival")."""
    out: list[tuple[str, ...]] = []
    kept = list(found)
    while kept and (kept[-1] in SUFFIXES or kept[-1].isdigit() or vocabulary.is_trade(kept[-1])):
        kept.pop()
        out.append(tuple(kept))
    return out


def _person(after: list[_Word], vocabulary: Vocabulary) -> tuple[str, ...]:
    """The name of the person an honorific names: the words after it up to the first possessive (that word included)
    or the first word any business here may have in its name (that word left out)."""
    out: list[str] = []
    for w in after:
        if vocabulary.is_generic(w.word):
            break
        out.append(w.word)
        if w.possessive:
            break
    return tuple(out)


def _doctor(found: tuple[str, ...], vocabulary: Vocabulary) -> tuple[str, ...]:
    """The doctor a name ending with a doctor's suffix names (DOCTOR_SUFFIXES, other suffixes and degrees after it
    aside: "Robert Chen DDS, MS"): the words before the suffix, back to the first one any business here may have in its
    name. () for any other name."""
    rest = list(found)
    while rest and rest[-1] not in DOCTOR_SUFFIXES and (rest[-1] in SUFFIXES or rest[-1] in HONORIFICS):
        rest.pop()
    if not rest or rest[-1] not in DOCTOR_SUFFIXES:
        return ()
    out: list[str] = []
    for w in reversed(rest[:-1]):
        if vocabulary.is_generic(w):
            break
        out.append(w)
    return tuple(reversed(out))


def _branded(found: tuple[str, ...], brand: tuple[frozenset[str], str], vocabulary: Vocabulary) -> bool:
    """Whether a form read from a page's title is its business's brand (the coordinator's ruling on concern (a), as the
    re-review's M-4 tightened it): a word of it of MIN_NAME letters or more that no business here would have in its name
    (not the town's, the trade's, a service's, a stopword) is a word of the business's Places name (`brand`'s stems) or
    is in the first label of its website's host (`brand`'s label, letters and digits only: "Downtown Dental" ~
    downtowndental.com). A short word ("New") or a label named for the town (paloalto.brightnow.com) makes no brand."""
    stems, label = brand
    kept = [w for w in found if len(w) >= MIN_NAME and not vocabulary.is_generic(w)]
    return any(_stem(w) in stems or w in label for w in kept)


def names_of(
    rivals: Iterable, vocabulary: Vocabulary | None = None, *, titles: Iterable = (), own: object = None
) -> Names:
    """Every form of each of `rivals` -- the names of businesses nearby -- and of each title in `titles` (each a page's
    title, its business's Places name and its website's host as `host` reads it), by the rule of the module docstring,
    with `vocabulary`'s words for those any business here may have in its name, and the business's `own` name. A form
    of a title that is no description is the business's name only when it is its brand (`_branded`); otherwise it is
    read as a description is. A person's name is one wherever it is read from. A form read both ways is a name the
    stronger way. What is no text names no one."""
    vocabulary = vocabulary or Vocabulary()
    forms: dict[tuple[str, ...], Form] = {}

    def put(form: Form | None, brand: tuple[frozenset[str], str] | None = None) -> None:
        if form is None:
            return
        if brand is not None and not form.generic and not _branded(form.words, brand, vocabulary):
            form = Form(form.words, True, form.capitals, True)
        held = forms.get(form.words)
        if held is None or (held.proper, held.generic) > (form.proper, form.generic):
            forms[form.words] = form

    def persons(honorific: str, person: tuple[str, ...]) -> None:
        put(Form(person) if len(person) > 1 else _form(person, vocabulary))
        if person:
            put(Form((honorific, person[-1])))

    def add(text: object, brand: tuple[frozenset[str], str] | None = None) -> None:
        if not isinstance(text, str):
            return
        parts = [_read(part) for part in _PARTS.split(text)]
        for read in [_read(text), *(parts if len(parts) > 1 else [])]:
            found = tuple(w.word for w in read)
            if not found or all(w in HONORIFICS or w in SUFFIXES or w.isdigit() for w in found):
                continue
            starts = [found, found[1:]] if found[0] in HONORIFICS else [found]  # a part without its honorific too
            for start in starts:
                for each in (start, *_trims(start, vocabulary)):
                    put(_form(each, vocabulary), brand)
            for i, w in enumerate(found):  # the person an honorific names, wherever it is in the part
                if w in HONORIFICS:
                    persons(w, _person(read[i + 1 :], vocabulary))
            persons("dr", _doctor(found, vocabulary))  # "Jane Doe, DDS" is Dr. Doe

    for rival in rivals:
        add(rival)
    for title, name, site in titles:
        stems = frozenset(_stem(w) for w in words(name) if not vocabulary.is_generic(w))
        label = re.sub(r"[^a-z0-9]", "", site.split(".")[0]) if isinstance(site, str) else ""
        add(title, (stems, label))
    whole = words(own)
    return Names(tuple(forms.values()), tuple(dict.fromkeys(w for w in (whole, *_trims(whole, vocabulary)) if w)))


def _at(found: tuple[str, ...], form: tuple[str, ...]) -> Iterator[int]:
    """Where `form` starts in `found`, as whole words."""
    n = len(form)
    return (i for i in range(len(found) - n + 1) if found[i : i + n] == form)


def _written(span: list[_Word], form: Form, strict: bool) -> bool:
    """Whether `span`, where a form that names only as a proper noun is found, names its business: each of its words a
    proper noun writes with a capital has one -- and, for a description, one of them is not the first word of a
    sentence (after ". ! ? : ( or a quote, where a capital says nothing). `strict` (what a run keeps of what was dropped
    or noted, the re-review's N1): a form that is no description, in any case; a description with its capitals
    wherever it is."""
    if strict and not form.generic:
        return True
    looked = [w for w, capital in zip(span, form.capitals, strict=True) if capital]
    if not looked or not all(w.upper for w in looked):
        return False
    return strict or not form.generic or any(not w.first for w in looked)


def _found(
    text: object, names: Names, *, search: bool, strict: bool = False
) -> tuple[list[_Word], list[tuple[int, int]]]:
    """`text`'s words and the spans of them (word indexes) that name a business nearby: a form wherever it is -- one
    that names only as a proper noun, only so written (`_written`) and never in a search -- unless inside the business's
    own name."""
    read = _read(text)
    found = tuple(w.word for w in read)
    own = [(i, i + len(name)) for name in names.own for i in _at(found, name)]
    spans: list[tuple[int, int]] = []
    for form in names.forms:
        if form.proper and search:
            continue
        for i in _at(found, form.words):
            j = i + len(form.words)
            if form.proper and not _written(read[i:j], form, strict):
                continue
            if not any(a <= i and j <= b for a, b in own):
                spans.append((i, j))
    return read, spans


def names_in(text: str, names: Names, *, search: bool = False) -> str | None:
    """The first name of a business nearby `text` holds (the module docstring), as `text` writes it -- the longest
    where two start together -- or None when it holds none. `search`: `text` is a search, where only a form that names
    a business wherever it is written does."""
    read, spans = _found(text, names, search=search)
    if not spans:
        return None
    i, j = min(spans, key=lambda s: (s[0], s[0] - s[1]))
    return text[read[i].start : read[j - 1].end]


def mask_names(text: str, names: Names, *, strict: bool = False) -> str:
    """`text` with every name of a business nearby it holds (`names_in`, as a sentence) replaced by NEARBY_MASK -- the
    longest first, so a name goes whole -- and nothing else changed. `strict` for what a run keeps of what it dropped
    and of the notes about it (its feedback, the Critic's): a name that is no description is masked however it is
    written there, a description wherever it has its capitals (`_written`) -- so the team can read why a sentence was
    dropped without the name outliving the run (plan K4 D5). A website's host is another matter (mask_hosts)."""
    read, spans = _found(text, names, search=False, strict=strict)
    chosen: list[tuple[int, int]] = []
    for i, j in sorted(spans, key=lambda s: (s[0] - s[1], s[0])):
        if all(j <= a or b <= i for a, b in chosen):
            chosen.append((i, j))
    out, at = [], 0
    for i, j in sorted(chosen):
        out += [text[at : read[i].start], NEARBY_MASK]
        at = read[j - 1].end
    return "".join([*out, text[at:]])


@functools.lru_cache(maxsize=512)
def _host_re(name: str) -> re.Pattern[str]:
    """A host as it is written as one, and only so: its labels joined by dots with spaces allowed around each dot
    (`rival1 . example`), an optional `www.` before it, in any case. Not preceded by a letter, a digit, & or a hyphen
    (that is one more label, or part of one), nor followed by one, or by `.` and one (a further label:
    `rival1.example.com` is another host); a full stop after it, a port or a path are no part of it. The same words with
    no dot are no host: `gentle dental care` is not gentle.dental."""
    dot = r"\s*\.\s*"
    body = dot.join(re.escape(label) for label in name.split("."))
    return re.compile(rf"(?<![a-z0-9&-])(?:www{dot})?{body}(?![a-z0-9&-])(?!\.[a-z0-9])", re.IGNORECASE)


def host_in(text: str, hosts: Iterable[str]) -> str | None:
    """The first of `hosts` that `text` holds written as a host (`_host_re`); None when it holds none. A website names
    a business as its name does (Gathered.hosts), so a sentence or a search that holds one is dropped like one that
    names a business."""
    for name in hosts:
        if _host_re(name).search(text or ""):
            return name
    return None


def mask_hosts(text: str, hosts: Iterable[str]) -> str:
    """`text` with each of `hosts` it holds written as a host replaced by NEARBY_MASK, its `www.` with it, wherever and
    as often as `host_in` finds it -- the longest host first, so a subdomain goes whole, not as a mask behind its
    `shop.`. What a run keeps of what names a business nearby by its website (plan K4 D5): a text with only the words
    of a host in it is left as it is."""
    for name in sorted(hosts, key=len, reverse=True):
        text = _host_re(name).sub(NEARBY_MASK, text)
    return text


def admiry_hosts(settings: config.Settings) -> tuple[str, ...]:
    """Admiry's own hosts, each with every name under it: ADMIRY_DOMAIN, the agent's (AGENT_PUBLIC_BASE_URL's), the
    browser pages' (AGENT_WEB_BASE_URL's) -- where the sites Admiry builds are served -- and the hosted pages' domain
    (AGENT_PAGES_DOMAIN). Research reads no page on one (`_read_pages`; founder ruling 2026-10-09)."""
    found = (ADMIRY_DOMAIN, host(settings.public_base_url), host(settings.web_base_url), settings.pages_domain)
    return tuple(dict.fromkeys(h for h in found if h))


def is_admirys(site: str | None, admirys: Iterable[str]) -> bool:
    """Whether the host `site` is one of Admiry's own (`admirys`) or a name under one."""
    return bool(site) and any(site == h or site.endswith(f".{h}") for h in admirys)


def admiry_in(text: str) -> str | None:
    """Admiry as `text` writes it -- the word, whole, in any case -- or None when `text` does not mention it. Research
    never does (founder ruling 2026-10-09): it would say whether a business nearby uses Admiry."""
    found = next((w for w in _read(text) if w.word == ADMIRY), None)
    return text[found.start : found.end] if found is not None else None


def host(url: str | None) -> str | None:
    """A URL's host without `www.`, in lower case; None when there is none, and for what is no text."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        name = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return name.lower().removeprefix("www.") if name else None


def trade_words(category: str) -> str:
    """A benchmark category or a consultation tree's id as an owner reads it: `home_services_plumbing` is "plumbing",
    `dentists` "dentists" (CATEGORY_WORDS); an id the table lacks reads as its words (`a_new_trade`, "a new trade")."""
    return CATEGORY_WORDS.get(category) or category.replace("_", " ")


def fetch_page(url: str) -> crawl.Page | None:
    """A website's first page by the crawl's guards and caps (public hosts only, MAX_BYTES, BUDGET_S), or None when it
    cannot be read: refused (a private address, a name that does not resolve) or broken. A business sets its own
    website, so whatever it sends can raise past the crawl's guards -- a punycode label that is no label raises
    idna.IDNAError once DNS says the host is public -- and that is one page fewer, never a failed run: the day's Places
    lookup is spent by then. Logged by the error's type alone, never the address. Tests patch it
    (research_fixtures.fake_pages)."""
    try:
        got = crawl.fetch_site(url, max_pages=1)
    except crawl.CrawlError:
        return None
    except Exception as err:  # noqa: BLE001 - one site must not fail the run (its address can raise past the crawl)
        log.warning("research: a business's website could not be read (%s)", type(err).__name__)
        return None
    return got[0] if got else None


def places_left(conn: psycopg.Connection, settings: config.Settings) -> bool:
    """Whether research may make one more call to Google today: under PLACES_SHARE of AGENT_PLACES_DAILY_BUDGET
    (auth.places_today counts every caller's calls, research's own included)."""
    return auth.places_today(conn) < int(settings.places_daily_budget * PLACES_SHARE)


def _area(knowledge: dict, sections: dict) -> dict:
    listing = knowledge.get("listing") or {}
    consultation = sections.get("consultation") or {}
    location = (consultation.get("business") or {}).get("location") or listing.get("address")
    areas = list(knowledge.get("service_area") or [])
    return {
        "service_area": areas,
        "location": places.display(location) if location else None,
        "town": places.town(location) or (areas[0] if areas else None),
        "radius_miles": (consultation.get("geo") or {}).get("radius_miles"),
    }


def _plan_view(sections: dict) -> dict | None:
    """What the plan runs today, as research reads it (plan decision D12); None before there is a plan."""
    if not sections:
        return None
    consultation = sections.get("consultation") or {}
    told = consultation.get("business") or {}
    rows = plans.funded(sections)
    return {
        "services": told.get("services") or ([told["service"]] if told.get("service") else []),
        "objective": consultation.get("objective"),
        "offer": consultation.get("offer"),
        "searches": sorted({t for r in rows for t in r.get("themes") or []}),
        "kept_out": sorted({t for r in rows for t in r.get("negatives") or []}),
    }


def _benchmark(conn: psycopg.Connection, category: str) -> list[tuple[dict, str | None]]:
    """The trade's benchmark rows (its own and its sub-trades', `home_services_plumbing`), each as the material shows
    it -- its trade in words, as the labels an owner reads name it -- with its published source's address, None when
    the row's source is a note, not a link (Bing's row is seeded equal to Google's and says so:
    benchmark.BING_SOURCE)."""
    rows = conn.execute(
        "SELECT id, category, channel, cpl_avg, cpl_low, cpl_high, source_url, as_of FROM benchmark "
        "WHERE geo = 'US' AND (category = %s OR category LIKE %s) ORDER BY id",
        (category, category.replace("_", "\\_") + "\\_%"),
    ).fetchall()
    return [
        (
            {
                "id": f"benchmark:{r['id']}",
                "trade": trade_words(r["category"]),
                "channel": registry.label(r["channel"]),
                "cost_per_lead": plans.usd(float(r["cpl_avg"])),
                "low": plans.usd(float(r["cpl_low"])),
                "high": plans.usd(float(r["cpl_high"])),
                "as_of": r["as_of"],
            },
            r["source_url"] if r["source_url"].startswith(("http://", "https://")) else None,
        )
        for r in rows
    ]


def _is_the_business(c: places.Candidate, knowledge: dict, listing: dict) -> bool:
    """Whether a place Google found is the business itself: its listing's place id; its website, the one Admiry read or
    the address the owner gave (profile.own_site, listings.same_site: a page on a shared host is the business only as
    the same page, so its Facebook page does not make every rival on facebook.com the business); its phone (the last
    ten digits); or its name as a sentence writes it (core). Not listings.close, whose name rule asks the owner about a
    place that only shares words with the business: "Bright Smile Orthodontics" is a business nearby. What is stored
    here can be any value; these read none of it as an error."""
    own_name = core(knowledge.get("name")).lower()
    return bool(
        (c.place_id and c.place_id == listing.get("place_id"))
        or listings.same_site(c.website, profile.own_site(knowledge))
        or listings.same_phone(c.phone, knowledge.get("phone"))
        or (own_name and core(c.name).lower() == own_name)
    )


def _nearby(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, knowledge: dict, area: dict
) -> tuple[list[Competitor], str]:
    """The one Places call (plan decision D6): the trade in the town, up to COMPETITORS_FETCHED places with their
    websites and phones, the business itself left out (_is_the_business). Recorded as a places.lookup row with no
    business -- the day's budget counts it, no owner's activity log shows it. A place's phone is read to tell the
    business and goes no further (Competitor has none)."""
    if not geocode.can_place(settings):
        return [], "no_key"
    what = knowledge.get("category_detail") or next((s["name"] for s in profile.services(knowledge)), None)
    where = area.get("town") or area.get("location")
    if not what or not where:
        return [], "no_area"
    if not places_left(conn, settings):
        return [], "budget"
    query = f"{what} in {where}"
    found = places.search(query, settings.places_api_key, max_results=COMPETITORS_FETCHED, with_identity=True)
    audit.record_system(
        conn,
        tool="places.lookup",
        actor="places",
        business_id=None,
        args={"text": query, "research": business_id},
        result={"found": len(found)},
    )
    listing = knowledge.get("listing") or {}
    lat, lng = listing.get("lat"), listing.get("lng")
    out = []
    for c in found:
        if _is_the_business(c, knowledge, listing):
            continue
        miles = round(geo.miles(lat, lng, c.lat, c.lng), 1) if None not in (lat, lng, c.lat, c.lng) else None
        out.append(Competitor(c.place_id, c.name, c.website, miles))
    return out, "called" if out else "none_found"


def _away(c: Competitor) -> str:
    """How far a business nearby is, as a source's label ends: ", 2.0 miles away", ", 1 mile away"; nothing without a
    point to measure from."""
    if c.miles is None:
        return ""
    return ", 1 mile away" if c.miles == 1 else f", {c.miles} miles away"


def _show(g: Gathered, upto: int) -> None:
    g.shown = min(upto, len(g.competitors))
    for k, c in enumerate(g.competitors[: g.shown], 1):
        g.sources.setdefault(
            f"place:{k}",
            {
                "kind": "place",
                "label": NEARBY_LABEL + _away(c),
                "url": places.maps_url(None, c.place_id),
                "text": json.dumps({"miles": c.miles}),
            },
        )


def _read_pages(g: Gathered, upto: int) -> None:
    """The first page of each shown business with a website, in order, until `upto` pages were tried in the run: one
    page per site. A business is listed but not read when it has no website, when another's site (the same host) was
    read already -- two offices of one practice are one claim -- when its "website" is a page on a host that is no
    one business's (listings.SHARED_HOSTS: a Facebook page, a booking page), or when it is on a host of Admiry's own
    (`admirys`: research never reads a page Admiry hosts, so it cannot say whether a business nearby uses Admiry --
    founder ruling 2026-10-09); none costs an attempt. A page's source is described from its place and links to it on
    Google Maps: the page's title is a name and its address the business's own, so neither outlives the model's reading
    of it (`pages`)."""
    read = {host(g.competitors[k - 1].website) for k in g.tried}
    for k, c in enumerate(g.competitors[: g.shown], 1):
        if len(g.tried) >= min(upto, MAX_PAGES):
            return
        site = host(c.website)
        if site is None or site in listings.SHARED_HOSTS or site in read or is_admirys(site, g.admirys):
            continue
        g.tried.add(k)
        read.add(site)
        page = fetch_page(c.website)
        if page is None or not page.text:
            continue
        pid = f"page:{len(g.pages) + 1}"
        text = page.text[:PAGE_CHARS]
        g.pages.append({"id": pid, "of": f"place:{k}", "title": page.title[:120], "text": text})
        g.sources[pid] = {
            "kind": "page",
            "label": NEARBY_PAGE_LABEL + _away(c),
            "url": places.maps_url(None, c.place_id),
            "text": text,
        }


def _guide_label(entry: knowledge_index.Entry) -> str:
    """A guide as an owner reads its name: its trade and its topic in words, not its id ("home_services/hvac")."""
    topic = GUIDE_TOPIC_WORDS.get(entry.topic) or entry.topic.replace("-", " ")
    return f"Admiry's guide to {trade_words(entry.vertical)}: {topic}"


def _retrieve(g: Gathered, budget: int) -> None:
    """Admiry's reviewed guides within `budget` tokens (K3's retrieve): the files a plan draft for this business would
    read -- its tree, its channels, strategist.topics of its trade (compliance, the trade, Google Search, NewsBreak
    where it runs, offers). None while AGENT_KNOWLEDGE is off (K3 P9), for a tree with no reviewed file, or while a file
    is malformed (K3 logs it and reads none). A source's words are its block (Key points and Do not say, the file's own
    source ids out), exactly what the model reads of it. retrieve reads each file inside K3's own guard, but a block is
    read again here, and a file can be gone by then: that guide is left out whole -- its words, its id, its source --
    and logged, as K3 leaves out a file it cannot read; the run goes on."""
    if not g.knowledge_on:
        return
    got = knowledge_index.retrieve(
        g.vertical,
        channels=g.channels,
        topics=strategist.topics(g.trade, tuple(g.channels)),
        budget_tokens=budget,
    )
    g.knowledge_tokens = budget
    read: dict[str, tuple[knowledge_index.Entry, str]] = {}
    for i in got.ids:
        entry = knowledge_index.get(i)
        if entry is None:
            continue
        try:
            read[i] = (entry, knowledge_index.block(entry))
        except knowledge_index.KnowledgeError as err:
            log.error("knowledge: %s; this guide is left out of the research", err)
    if not read:
        return
    g.knowledge = {
        "text": knowledge_index.SEPARATOR.join(block for _, block in read.values()),
        "ids": [f"knowledge:{i}" for i in read],
    }
    for i, (entry, block) in read.items():
        g.sources[f"knowledge:{i}"] = {
            "kind": "knowledge",
            "label": _guide_label(entry),
            "url": f"admiry://knowledge/{i}",
            "text": block,
        }


def gather(conn: psycopg.Connection, settings: config.Settings, business_id: str, brief: dict) -> Gathered:
    """Round one's material for the business and the brief (`topics`, the owner's `note`). It calls Google (once, in
    budget) and fetches pages: from outside a transaction, holding no lock. It reads no model, so the caller makes sure
    one can be called first -- a run that cannot call a model must not spend the day's Places lookups."""
    known = business.latest_knowledge(conn, business_id)
    knowledge = known[1] if known else {}
    category = knowledge.get("category") or "fallback"
    plan = plans.latest(conn, business_id)
    sections = plan["sections"] if plan else {}
    topics = [t for t in dict.fromkeys(brief.get("topics") or ()) if t in TOPICS] or list(TOPICS)
    area = _area(knowledge, sections)
    competitors, state = _nearby(conn, settings, business_id, knowledge, area)
    seen = profile.for_model(knowledge)
    told = (sections.get("consultation") or {}).get("business") or {}
    g = Gathered(
        business_id=business_id,
        vertical=consult.tree_for(category),
        category=category,
        topics=topics,
        channels=[c["channel"] for c in plans.funded(sections)] or ["google_search"],
        base={
            "business": seen,
            "trade": knowledge.get("category_detail") or trade_words(category),
            "area": area,
            "plan": _plan_view(sections),
            "topics": [{"id": t, "about": TOPIC_WORDS[t]} for t in topics],
            "owner_asked": brief.get("note"),
        },
        benchmark=[],
        competitors=competitors,
        places=state,
        trade=strategist.trade(knowledge, {"service": told.get("service"), "services": told.get("services") or []}),
        knowledge_on=settings.knowledge,
        admirys=admiry_hosts(settings),
    )
    g.sources["business"] = {
        "kind": "business",
        "label": BUSINESS_LABEL,
        "url": None,
        "text": json.dumps(seen, ensure_ascii=False, default=str),
    }
    for row, url in _benchmark(conn, category):
        g.benchmark.append(row)
        g.sources[row["id"]] = {
            "kind": "benchmark",
            "label": f"Published cost-per-lead averages for {row['trade']} on {row['channel']} ({row['as_of']})",
            "url": url,
            "text": json.dumps(row, ensure_ascii=False),
        }
    _show(g, COMPETITORS_SHOWN)
    _read_pages(g, ROUND1_PAGES)
    _retrieve(g, KNOWLEDGE_TOKENS)
    return g


def more(g: Gathered) -> Gathered:
    """fetch_more (spec §4.1): every business the one call found (no second call), their pages up to MAX_PAGES in the
    run, the same guides within MORE_KNOWLEDGE_TOKENS. The same object, grown, marked fetched_more."""
    _show(g, len(g.competitors))
    _read_pages(g, MAX_PAGES)
    _retrieve(g, MORE_KNOWLEDGE_TOKENS)
    g.fetched_more = True
    return g


def digest(g: Gathered) -> dict:
    """The materials digest stored with the run (spec §4: every run stores its materials digest): the sha256 of what
    the model read, the counts, and each source's id, kind, label and link -- never a source's words, never a name."""
    seen = json.dumps({"material": g.material, "pages": g.pages}, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "sha256": hashlib.sha256(seen.encode()).hexdigest(),
        "vertical": g.vertical,
        "places": g.places,
        "fetched_more": g.fetched_more,
        "counts": {
            "shown": g.shown,
            "found": len(g.competitors),
            "pages": len(g.pages),
            "tried": len(g.tried),
            "benchmark": len(g.benchmark),
            "knowledge": len((g.knowledge or {}).get("ids", [])),
        },
        "sources": [
            {"id": i, "kind": s["kind"], "label": s["label"], "url": s.get("url")} for i, s in g.sources.items()
        ],
    }
