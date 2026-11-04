"""The words of an industry site: the model writes them from the owner's facts, and the linter refuses any number, licence,
rating, phone, e-mail, capitals or banned word the facts do not back (A3p-6 batch 1 Q11; spec P9, T8). The linter is
`page_builder.text_notes` run on every string of `SiteWords` against a kit-shaped dict built from the facts, so the
rules are the landing pages' own."""

from __future__ import annotations

import hashlib
import json
import re

import psycopg
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from .. import config, llm, page_builder
from ..models import SiteWords
from . import families
from .spec import SiteSpec

REFUSED = "The site's words did not pass Admiry's rules: "
DEFAULT_CHARS = 400  # a string whose description names no limit
_LIMIT = re.compile(r"at most (\d+) characters")
WHY_MAX, STEPS, HOME_FAQ, STORY, SERVICE_FAQ = 4, (3, 4), (4, 6), (2, 3), 3
# what each section is for, with the shape of an example: the model's brief for a family (never facts)
_BRIEF = {
    "home_services": {
        "home": "hero: the lead service and the city as the headline, one plain line under it; services; why choose "
        "the business (the owner's own points); how it works in 3 or 4 steps; where the business works; "
        "questions; a closing call.",
        "example_headline": "Roof repair in Charlotte",
        "example_why_point": "A family-run company that answers its own phone.",
    },
    "dental": {
        "home": "hero: the lead treatment and the city as the headline, one calm line under it; treatments; why "
        "choose the practice; what a first visit is like in 3 or 4 steps; where the practice is; questions; a "
        "closing invitation to request an appointment.",
        "example_headline": "Invisalign in San Jose",
        "example_why_point": "A quiet office and time for your questions.",
    },
}


def _flat(value: object) -> list[str]:
    """Every string of a fact (a model, a list, a dict), in order."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, BaseModel):
        return _flat(value.model_dump())
    if isinstance(value, dict):
        return [t for v in value.values() for t in _flat(v)]
    if isinstance(value, list):
        return [t for v in value for t in _flat(v)]
    return []


def _kit(spec: SiteSpec, family: families.Family) -> dict:
    """The kit-shaped dict `page_builder.text_notes` checks against: the business's phone, every string of its facts
    (a number of years counted as a claim of years), the facts that are about prices, and about reviews."""
    f = spec.facts
    texts = _flat(f.model_dump(exclude={"years", "dentist"}))
    if f.years is not None:
        texts.append(f"{f.years} years in business")
    if f.dentist:
        texts += _flat(f.dentist.model_dump(exclude={"years"}))
        if f.dentist.years is not None:
            texts.append(f"{f.dentist.years} years in practice")
    pricing = [s.price_line for s in f.services if s.price_line] + [t for t in (f.financing, f.free_estimate) if t]
    return {
        "facts": {
            "phone": f.phone,
            "texts": texts,
            "pricing_hints": pricing,
            "reviews_summary": " ".join([f.rating or "", *(r.text for r in f.reviews)]),
        },
        "message": {"offer": f.free_estimate},
        "rules": {"banned_words": list(family.banned)},
    }


def lead_names(spec: SiteSpec) -> list[str]:
    return [s.name for s in spec.facts.services if s.lead]


def model_message(spec: SiteSpec) -> dict:
    """The lead service and the city the home headline must name (the ads' words, as the plan's kit gives them)."""
    leads = lead_names(spec)
    city = spec.facts.city
    return {
        "lead_services": leads,
        "city": city,
        "headline_pattern": f"{leads[0]} in {city}" if leads and city else (leads[0] if leads else ""),
    }


def model_facts(spec: SiteSpec) -> dict:
    """What the model reads of the owner's facts: never the phone, the address, the e-mail, the links or the place id
    (the templates place those; the form's address is not for the model) and never the snippet."""
    return spec.facts.model_dump(
        exclude={"phone", "email", "address", "social", "gbp_url", "place_id"}, exclude_none=True
    )


def model_family(family: families.Family, spec: SiteSpec) -> dict:
    """What the model reads of the family: its structure and each section's purpose, an example and the banned words."""
    brief = _BRIEF[family.id]
    return {
        "id": family.id,
        "trade": spec.trade,
        "structure": brief["home"],
        "pages": "home, about, services, contact and one page for each lead service",
        "limits": {"why_points": WHY_MAX, "steps": "3 or 4", "home_faq": "4 to 6", "story": "2 or 3", "service_faq": 3},
        "example": {"headline": brief["example_headline"], "why_point": brief["example_why_point"]},
        "banned": list(family.banned),
    }


def fingerprint(spec: SiteSpec) -> str:
    """What the model reads for `spec` with no direction -- the owner's facts as `model_facts` gives them, the family's
    brief and the message the home headline carries -- as one SHA-256: words written for a document stand for another
    with the same fingerprint. The look, the colour, the photos and the facts the model never reads (the phone, the
    e-mail, the address, the links) are not in it: the templates place those."""
    read = {"facts": model_facts(spec), "family": model_family(families.get(spec.family), spec)}
    read["message"] = model_message(spec)
    return hashlib.sha256(json.dumps(read, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _label(path: tuple[object, ...], words: SiteWords) -> str:
    """The piece as a note says it: "The home page's headline", "The roof-repair page's answer 2"."""
    head, *rest = path
    if head == "service_pages":
        slug = words.service_pages[rest[0]].slug  # type: ignore[index]
        owner, rest = f"The {slug} page's", rest[1:]
    elif head in ("home", "about", "services", "contact"):
        owner = f"The {head} page's"
    else:
        return f"The {str(head).replace('_', ' ')}"
    bits: list[str] = []
    for part in rest:
        bits.append(str(part + 1) if isinstance(part, int) else str(part).replace("_", " "))
    return f"{owner} {' '.join(bits)}"


def _limit(model: BaseModel, name: str) -> int:
    description = type(model).model_fields[name].description or ""
    found = _LIMIT.search(description)
    return int(found.group(1)) if found else DEFAULT_CHARS


# the limits a model's own field descriptions do not carry (a list's items, the question and the answer of a Faq)
_ITEM_LIMITS = {"question": 120, "q": 120, "a": 400, "answer": 400}


def _pieces(words: SiteWords) -> list[tuple[tuple[object, ...], str, int]]:
    """(path, text, limit) of every string of `words`: the walk is by field, so a field added later is linted too."""
    out: list[tuple[tuple[object, ...], str, int]] = []

    def walk(model: BaseModel, path: tuple[object, ...]) -> None:
        for name, value in model:
            if name == "slug" or (name == "hours" and path == ()):  # the hours are judged on their own (_tidy_hours)
                continue
            limit = _limit(model, name)
            here = (*path, name)
            if isinstance(value, str):
                out.append((here, value, _ITEM_LIMITS.get(name, limit) if name in _ITEM_LIMITS else limit))
            elif isinstance(value, BaseModel):
                walk(value, here)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        out.append(((*here, i), item, limit))
                    elif isinstance(item, BaseModel):
                        walk(item, (*here, i))

    walk(words, ())
    return out


def lint(words: SiteWords, spec: SiteSpec, family: families.Family) -> list[str]:
    """Why `words` cannot go on the site, one sentence each ([] = fine): the landing pages' rules on every piece (a price,
    years, a rating, a licence number or a phone the facts do not back, an e-mail, capitals, the family's banned words,
    the length), the home headline naming a lead service and the city, the counts and the service pages."""
    notes: list[str] = []
    kit = _kit(spec, family)
    rules = page_builder.rules_for(kit, {})
    for path, text, limit in _pieces(words):
        notes += page_builder.text_notes(_label(path, words), text, limit, rules)

    home = words.home
    required = [
        ("The home page's headline", home.headline),
        ("The home page's lead", home.lead),
        ("The about page's headline", words.about.headline),
        ("The about page's lead", words.about.lead),
        ("The services page's headline", words.services.headline),
        ("The contact page's headline", words.contact.headline),
        ("The call button's words", words.cta_label),
        ("The second button's words", words.book_label),
    ]
    for page in words.service_pages:
        required += [(f"The {page.slug} page's lead", page.lead), (f"The {page.slug} page's what", page.what)]
    notes += [f"{label} is empty." for label, text in required if not text.strip()]
    if home.headline.strip():
        leads, city = [n.lower() for n in lead_names(spec)], spec.facts.city.lower()
        head = home.headline.lower()
        if (leads and not any(n in head for n in leads)) or (city and city not in head):
            pattern = model_message(spec)["headline_pattern"]
            notes.append(f"The home headline must name the lead service and the city the way the ads do (“{pattern}”).")
    if len(home.why_points) > WHY_MAX:
        notes.append(f"There are more than {WHY_MAX} reasons to choose the business.")
    if not STEPS[0] <= len(home.steps) <= STEPS[1]:
        notes.append("How it works needs 3 or 4 steps.")
    if not HOME_FAQ[0] <= len(home.faq) <= HOME_FAQ[1]:
        notes.append("The home page needs 4 to 6 questions.")
    if not STORY[0] <= len(words.about.story) <= STORY[1]:
        notes.append("The about page's story needs 2 or 3 paragraphs.")
    slugs = {s.slug for s in spec.facts.services if s.lead}
    seen: set[str] = set()
    for page in words.service_pages:
        if page.slug not in slugs:
            notes.append(f"There is a page for “{page.slug}”, which is not one of the business's lead services.")
        elif page.slug in seen:
            notes.append(f"The {page.slug} page is written twice.")
        seen.add(page.slug)
        if len(page.faq) > SERVICE_FAQ:
            notes.append(f"The {page.slug} page has more than {SERVICE_FAQ} questions.")
    for slug in sorted(slugs - seen):
        notes.append(f"The {slug} service has no page words.")
    return notes


def _squash(model: BaseModel) -> BaseModel:
    """The model's answer with the whitespace of every string squashed to single spaces."""
    data = model.model_dump()

    def fix(v: object) -> object:
        if isinstance(v, str):
            return " ".join(v.split())
        if isinstance(v, list):
            return [fix(i) for i in v]
        if isinstance(v, dict):
            return {k: fix(i) for k, i in v.items()}
        return v

    return type(model).model_validate(fix(data))


def write(
    conn: psycopg.Connection, settings: config.Settings, spec: SiteSpec, *, direction: str | None = None
) -> SiteWords:
    """The model's words for `spec` (the facts and the family's brief, never the snippet or the form address), linted;
    with any note, asked once more with the notes; a second refusal fails with the notes as one ToolError."""
    del conn  # the facts are in the spec; kept so a later batch can read the business's page rows here
    family = families.get(spec.family)
    gen = page_builder._generator(settings)
    facts, brief, message = model_facts(spec), model_family(family, spec), model_message(spec)
    base = (direction or "").strip()[: page_builder.MAX_DIRECTION] or None
    notes: list[str] | None = None
    for _attempt in range(2):
        ask = base
        if notes:
            ask = ((base + "\n\n") if base else "") + "Fix these before answering: " + " ".join(notes)
        try:
            words = _squash(gen.write_site(facts, brief, message, ask))
        except llm.LLMError as e:
            raise ToolError(str(e)) from e
        notes = lint(words, spec, family)
        if not notes:
            return _tidy_hours(words, spec)
    raise ToolError(REFUSED + " ".join(notes))


HOURS_LINES, HOURS_CHARS = 7, 40
_NOT_HOURS = re.compile(r"\d{3}[\s.-]?\d{4}|@|https?://|www\.", re.IGNORECASE)  # a phone, an e-mail, a link


def _tidy_hours(words: SiteWords, spec: SiteSpec) -> SiteWords:
    """The words with their tidy hours kept only when they fit: the owner gave hours, at most HOURS_LINES lines of at
    most HOURS_CHARS characters, none carrying a phone, an e-mail or a link. Anything else and the site shows the
    owner's own lines (`render.bundle`): a line that does not fit never refuses the site's words, as the linter's notes
    do -- a time written another way ("17" as "5 pm") is no claim the facts must back."""
    lines = [h for h in words.hours if h.strip()]
    fits = bool(spec.facts.hours) and len(lines) <= HOURS_LINES
    fits = fits and all(len(h) <= HOURS_CHARS and not _NOT_HOURS.search(h) for h in lines)
    return words if fits and lines == words.hours else words.model_copy(update={"hours": lines if fits else []})
