"""Admiry's own landing pages (A3p-2 Task 5; spec 2026-09-28-admiry-pages-design.md §7, plan H12, rulings H31 and
H35): for an owner with no assistant of their own (the homepage) and for an assistant that asks, Admiry writes the
page itself. The model writes the words (`llm.Generator.write_page` / `revise_page`, shape `models.PageCopy`) under
the kit's rules; `lint` refuses what the business's own facts do not back (the spec's P9); and the vertical's Jinja
template (`agent/templates/pages/<tree>.html` extending `_base.html`, rendered by the one environment in
`agent/templating.py`, autoescape on -- the model's words are untrusted text) places them beside what the page adds
on its own: the call button, the booking link when the business's address is a booking page, the form posting to
Admiry, the FAQ, the hours and the map link, the privacy link, the vertical's required lines and the tracking
snippet.

`page.sections` on the business's one page row holds what the page was built from -- `{"tree", "copy", "photos",
"version", "hours"}` (`version` counts the drafts; `hours` is the owner's own hours line, None for the kit's) -- so
`update_page` can change one part exactly and `revise_page` can hand the model the page as it reads now. Every one
of the three answers with a preview (`hosted.save_preview`) carrying the same sections, which a publish copies onto
the page row (`proposals.apply_publish_page`)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import config, hosted, landing, llm
from .channels.google_search import build as ads_launch  # the Google Search copy rules (moved in M0)
from .models import FaqItem, PageCopy
from .templating import env as _env

TREES = ("dentists", "home_services", "attorneys", "fallback")  # one template per consultation tree
SECTIONS = ("headline", "subheadline", "offer_line", "trust", "service", "faq", "hours")  # update_page's parts
LABELS = {  # a section as the owner reads it in a say
    "headline": "headline",
    "subheadline": "line under the headline",
    "offer_line": "offer line",
    "trust": "reasons to choose you",
    "service": "paragraphs about the service",
    "faq": "questions and answers",
    "hours": "hours",
}
# A3p-3 P22: an instruction that asks for more than the one-page template holds -- the owner's assistant can do it
BEYOND = "That's more than a quick change. Your assistant can do it with you."
REFUSED = "The page's copy did not pass Admiry's rules: "
NO_PAGE = "There is no page Admiry wrote for this business yet; ask Admiry to write one first."
MAX_DIRECTION = 1000  # a brief or an instruction, like tools_plan.MAX_INSTRUCTION

# PageCopy's limits (its field descriptions say the same to the model; checked here, never by pydantic)
HEADLINE_CHARS, SUBHEADLINE_CHARS, OFFER_CHARS, CTA_CHARS = 60, 120, 80, 30
TRUST_MAX, TRUST_CHARS = 4, 90
SERVICE_MAX, SERVICE_CHARS = 3, 400
FAQ_MAX, QUESTION_CHARS, ANSWER_CHARS = 5, 120, 400
HOURS_CHARS = 120


# ---------------------------------------------------------------- the linter (the spec's P9)

_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}
# a number as a claim names it: digits ("1,200", "4.9") or a word ("twenty"), never the tail of a longer token
_NUM = r"(?<![\w.,$])(\d[\d,]*(?:\.\d+)?|" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b"
_UNITS = (  # what a bare "2.5" measures when it is not a rating
    r"miles?|mi|km|hours?|hrs?|minutes?|mins?|days?|weeks?|months?|years?|yrs?|times|feet|ft|inches|lbs?|pounds?|kg"
)
# The four kinds of claim a page must not make unless the business's facts do (H12): each pattern's group 1 is the
# number the claim names -- compared, as a normalised digit string, with the numbers the same patterns (and, for
# prices and ratings, the facts that are about prices and reviews) find in the kit's facts.
_PRICE = (
    re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)"),
    re.compile(_NUM + r"\s*(?:dollars?|usd)\b", re.IGNORECASE),
    re.compile(r"(?<![\w.,])(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.IGNORECASE),
)
_YEARS = (
    re.compile(_NUM + r"\s*\+?\s*-?\s*(?:years?|yrs?)\b", re.IGNORECASE),
    re.compile(_NUM + r"\s+decades?\b", re.IGNORECASE),
    re.compile(r"\b(?:since|established(?:\s+in)?|est\.|founded(?:\s+in)?)\s*((?:18|19|20)\d{2})\b", re.IGNORECASE),
)
_RATING = (
    re.compile(r"(?<![\w.,$])([0-5](?:\.\d{1,2})?)\s*-?\s*(?:stars?\b|★|/\s*5\b|out of (?:5|five)\b)", re.IGNORECASE),
    re.compile(r"\b(?:rated|rating(?:\s+of)?)\s*:?\s*([0-5](?:\.\d{1,2})?)(?![\d.])", re.IGNORECASE),
    re.compile(r"\b(one|two|three|four|five)[-\s]stars?\b", re.IGNORECASE),
    re.compile(r"(?<![\w.,$])(\d[\d,]*)\s+(?:[a-z]+\s+)?reviews?\b", re.IGNORECASE),
    re.compile(r"(?<![\w.,$])([0-5]\.\d)(?![\d.%])(?!\s*(?:" + _UNITS + r")\b)", re.IGNORECASE),
)
_LICENCE = (
    re.compile(
        r"\b(?:licen[cs]ed?|lic\.?|permit|registration|reg\.)\s*(?:no\.?|number|num\.?|#)\s*:?\s*"
        r"([a-z-]*\d[a-z0-9-]*)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:licen[cs]e|permit)\s+:?\s*([a-z-]*\d[a-z0-9-]*)", re.IGNORECASE),
)
_KINDS = {"price": ("a price", _PRICE), "years": ("a number of years", _YEARS), "rating": ("a rating", _RATING)}
_ANY_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm(n: str) -> str:
    """A claim's number as one comparable string: "1,200" / "1200.00" -> "1200", "4.90" -> "4.9", "twenty" -> "20"."""
    n = n.strip().lower()
    if n in _WORD_NUMBERS:
        return str(_WORD_NUMBERS[n])
    n = n.replace(",", "")
    return n.rstrip("0").rstrip(".") if "." in n else n


def _fact_texts(kit: dict) -> list[str]:
    """Every string the kit's facts hold (lists flattened), the ads' offer too: the sources a claim may come from."""
    out: list[str] = []
    for value in (kit.get("facts") or {}).values():
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out += [v for v in value if isinstance(v, str)]
    offer = (kit.get("message") or {}).get("offer")
    if isinstance(offer, str):
        out.append(offer)
    return out


def _allowed(kit: dict) -> dict[str, set[str]]:
    """The numbers the facts back, per kind: what the kind's own patterns find in any fact ("20 years in Austin"),
    plus any number at all in the facts that are about prices (the pricing hints, the offer) or about reviews."""
    allowed: dict[str, set[str]] = {kind: set() for kind in _KINDS}
    for text in _fact_texts(kit):
        for kind, (_label, patterns) in _KINDS.items():
            allowed[kind] |= {_norm(m.group(1)) for p in patterns for m in p.finditer(text)}
    facts = kit.get("facts") or {}
    loose = {
        "price": [
            *(facts.get("pricing_hints") or []),
            facts.get("offer") or "",
            kit.get("message", {}).get("offer") or "",
        ],
        "rating": [facts.get("reviews_summary") or ""],
    }
    for kind, texts in loose.items():
        allowed[kind] |= {_norm(n) for t in texts if isinstance(t, str) for n in _ANY_NUMBER.findall(t)}
    return allowed


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


@dataclass(frozen=True)
class _Rules:
    """What `_text_notes` checks one piece of the words against, worked out once per page from the kit and the
    template's recipe."""

    banned: list[str]
    phone: str  # the business's own phone, digits only
    allowed: dict[str, set[str]]  # the numbers the facts back, per kind of claim
    facts: str  # every fact, letters and digits only, upper case: where a licence number must appear


def rules_for(kit: dict, recipe: dict) -> _Rules:
    return _Rules(
        banned=_banned(kit, recipe),
        phone=_phone_digits(kit),
        allowed=_allowed(kit),
        facts=_alnum(" ".join(_fact_texts(kit))),
    )


def _banned(kit: dict, recipe: dict) -> list[str]:
    """The template's recipe's banned words and the business's own vertical's (the kit's): usually the same list."""
    return list(dict.fromkeys([*recipe.get("banned", []), *(kit.get("rules") or {}).get("banned_words", [])]))


def _phone_digits(kit: dict) -> str:
    return re.sub(r"\D", "", (kit.get("facts") or {}).get("phone") or "")


def _own_phone(found: str, phone: str) -> bool:
    """Whether a phone-like number in the words is the business's own (last 10 digits, or the last 7 when only 7
    were written)."""
    digits = re.sub(r"\D", "", found)
    return bool(phone) and len(digits) >= 7 and phone[-min(len(digits), 10) :] == digits[-10:]


def text_notes(where: str, text: str, limit: int, rules: _Rules) -> list[str]:
    """Every rule one piece of the page's words breaks, one sentence each: its length, a banned word, a phone
    number that is not the business's own, an e-mail address, a word in capitals (ads_launch.lint_copy's rules),
    and a price, a number of years, a rating or a licence number the business's facts do not back."""
    notes: list[str] = []
    if len(text) > limit:
        notes.append(f"{where} is over {limit} characters.")
    hit = ads_launch._banned_hit(text, rules.banned)
    if hit:
        notes.append(f"{where} uses “{hit}”, a word Admiry leaves off pages for this kind of business.")
    if any(not _own_phone(m.group(0), rules.phone) for m in ads_launch._PHONE_RE.finditer(text)):
        notes.append(f"{where} includes a phone number that is not the business's own.")
    if ads_launch._EMAIL_RE.search(text):
        notes.append(f"{where} includes an e-mail address.")
    if ads_launch._shouts(text):
        notes.append(f"{where} has a word written in capitals.")
    seen: set[tuple[str, str]] = set()
    for kind, (label, patterns) in _KINDS.items():
        for p in patterns:
            for m in p.finditer(text):
                number = _norm(m.group(1))
                if number not in rules.allowed[kind] and (kind, number) not in seen:
                    seen.add((kind, number))
                    notes.append(f"{where} names {label} (“{m.group(0).strip()}”) the business's facts do not have.")
    for p in _LICENCE:
        for m in p.finditer(text):
            token = _alnum(m.group(1))
            if token not in rules.facts and ("licence", token) not in seen:
                seen.add(("licence", token))
                notes.append(
                    f"{where} names a licence number (“{m.group(0).strip()}”) the business's facts do not have."
                )
    return notes


_rules, _text_notes = rules_for, text_notes  # the names this module's own code and tests use


def _pieces(copy: PageCopy) -> list[tuple[str, str, int]]:
    """(where, text, limit) for every piece of the page's words, named the way a note says it."""
    out = [
        ("The headline", copy.headline, HEADLINE_CHARS),
        ("The line under the headline", copy.subheadline, SUBHEADLINE_CHARS),
        ("The offer line", copy.offer_line, OFFER_CHARS),
        ("The call button", copy.cta_label, CTA_CHARS),
    ]
    out += [(f"Reason {i} to choose the business", t, TRUST_CHARS) for i, t in enumerate(copy.trust_points, 1)]
    out += [(f"Service paragraph {i}", t, SERVICE_CHARS) for i, t in enumerate(copy.service_paragraphs, 1)]
    for i, item in enumerate(copy.faq, 1):
        out += [(f"Question {i}", item.q, QUESTION_CHARS), (f"The answer to question {i}", item.a, ANSWER_CHARS)]
    return out


def _message_match(copy: PageCopy, kit: dict) -> list[str]:
    """The headline must pass the check's blocking Message-match rule (landing.check): the service, and the city or
    the offer, like the ads. Asked only for what the kit knows: with neither a city nor an offer, only the service."""
    facts, message = kit.get("facts") or {}, kit.get("message") or {}
    city = (facts.get("city") or "").lower()
    offer = (message.get("offer") or "").lower()
    head = copy.headline.lower()
    placed = (city and city in head) or (offer and offer in head)
    # one of the services the owner picked is enough (landing.lead_services): never all of them joined
    if (landing.lead_services(kit) and not landing.names_a_service(kit, head)) or ((city or offer) and not placed):
        pattern = message.get("headline_pattern") or facts.get("service") or ""
        return [f"The headline must name the service and the city the way the ads do (“{pattern}”)."]
    return []


def lint(copy: PageCopy, kit: dict, recipe: dict) -> list[str]:
    """Why `copy` cannot go on the page, one sentence each ([] = fine): a price, a number of years, a rating or a
    licence number the kit's facts do not contain (digit strings compared per kind of claim); any of the recipe's
    banned words (whole words, any case); ads_launch.lint_copy's phone / e-mail / capitals rules on every piece, only
    the business's own phone allowed; the limits PageCopy describes; and a headline the check's Message-match rule
    would fail. The required lines are the template's job, never the model's, so they are not looked for here."""
    notes: list[str] = []
    if not copy.headline.strip():
        notes.append("The headline is empty.")
    if not copy.cta_label.strip():
        notes.append("The call button has no words.")
    if not copy.service_paragraphs:
        notes.append("The page needs at least one paragraph about the service.")
    if len(copy.trust_points) > TRUST_MAX:
        notes.append(f"There are more than {TRUST_MAX} reasons to choose the business.")
    if len(copy.service_paragraphs) > SERVICE_MAX:
        notes.append(f"There are more than {SERVICE_MAX} paragraphs about the service.")
    if len(copy.faq) > FAQ_MAX:
        notes.append(f"There are more than {FAQ_MAX} questions.")
    notes += _message_match(copy, kit)
    rules = _rules(kit, recipe)
    for where, text, limit in _pieces(copy):
        notes += _text_notes(where, text, limit, rules)
    return notes


# ---------------------------------------------------------------- the template (H12, H31)


def tree_for(template: str | None, kit: dict) -> str:
    """The template to use: a tree's name, or None for the business's own vertical (a vertical with no template of
    its own gets the fallback). ToolError naming the four for anything else."""
    if template is None or not template.strip():
        return kit["tree"] if kit["tree"] in TREES else "fallback"
    name = template.strip().lower()
    if name not in TREES:
        raise ToolError(
            f"There is no page template called “{template}”; use dentists, home_services, attorneys or fallback."
        )
    return name


def tel(phone: str | None) -> str | None:
    """The tap-to-call target: E.164 for a US number written with 10 digits (or 11, its leading 1 included), the
    number's digits as dialled otherwise (with the + it was written with); None without a usable number."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None
    if phone.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits if len(digits) == 11 and digits.startswith("1") else digits


_tel = tel  # the starter page's name for it; the industry sites (agent/sites) dial the same way


def _booking_url(facts: dict) -> str | None:
    """The booking link beside Call: the business's own address when it is an https page on a booking service
    Admiry knows (landing.BOOKING_HOSTS) -- the kit has no separate booking address."""
    url = facts.get("website") or ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    booking = any(host == b or host.endswith("." + b) for b in landing.BOOKING_HOSTS)
    return url if parts.scheme == "https" and booking else None


def _labelled_number(label: str, kit: dict) -> str | None:
    """The number the business's facts give right after `label` ("License #" + "TACLA00000C"), else None."""
    pattern = re.compile(re.escape(label.strip()) + r"\s*([a-z0-9-]*\d[a-z0-9-]*)", re.IGNORECASE)
    for text in _fact_texts(kit):
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def required_lines(kit: dict) -> tuple[list[str], list[dict]]:
    """(the lines the page prints verbatim, the required lines it cannot print yet). The law follows the business,
    not the layout: the lines are the business's own vertical's (the kit's rules), whichever template was chosen,
    and every one goes on the page as written wherever it may apply -- a disclaimer printed where the law does not
    ask for it is harmless, one missing where it does is not. A line that is a label for the business's own number
    ("License #") is printed with the number the facts give after it; without one it is left off, since a bare
    label would be wrong, and the caller says so."""
    lines: list[str] = []
    missing: list[dict] = []
    for line in (kit.get("rules") or {}).get("required_lines", []):
        text = line["text"]
        if text.rstrip().endswith("#"):
            number = _labelled_number(text, kit)
            if number:
                lines.append(f"{text}{number}")
            else:
                missing.append(line)
        else:
            lines.append(text)
    return lines, missing


def title_for(copy: PageCopy, kit: dict) -> str:
    """The page's <title> (and its version's title once published): the headline, and the business's name when the
    headline does not already carry it."""
    name = ((kit.get("facts") or {}).get("name") or "").strip()
    head = copy.headline.strip()
    return f"{head} | {name}" if name and name.lower() not in head.lower() else head


def render(
    tree: str,
    kit: dict,
    copy: PageCopy,
    *,
    page_id: str,
    agent_base: str,
    photos: list[dict],
    hostname: str | None,
    hours: str | None = None,
) -> str:
    """The page as one self-contained HTML file: mobile first, no navigation, one goal; the recipe's sections in the
    recipe's order; the call button (`tel:`, `data-admiry="call"`) and a call bar that stays in reach on a phone;
    the booking link when the business's address is a booking page (`data-admiry="book"`); the form with the
    vertical's own fields posting to Admiry (`<agent_base>/t/form/<page_id>`, `method="post"`, the `website2`
    honeypot); the FAQ; the hours, the address and a map link; in the footer the business's phone (nothing when
    there is none: the owner's sign-in address is the owner's to publish, never Admiry's to guess), the vertical's
    required lines verbatim, and the links to the terms of use and the privacy notice (`/terms` and `/privacy`, the
    tenant's own pages on its host -- the landing check's `terms` and `contact` rules, M1 Task 4); the kit's
    tracking snippet. `hours` is the owner's own hours line (update_page), else the kit's. `hostname` -- the page's
    address on the pages domain once it has one -- becomes the canonical link. Inline CSS only (`pages/hosted.css`,
    included by `_base.html`), system fonts, nothing loaded from elsewhere; every value escaped by Jinja's
    autoescape.

    `photos` (A3p-2 Task 6) is `sections["photos"]` verbatim -- `[{"id", "width", "height"}, ...]`, the owner's
    uploaded photos in the order they were added (agent/uploads.py is what ever writes a non-empty list here).
    Each becomes `<img src="/photos/{id}" width height>`: a root-absolute path, correct as-is once this exact
    HTML is later published verbatim to the tenant host at "/" (apply_publish_page copies a preview's bytes
    onto the live page unchanged, H7) -- the SAME bytes also have to resolve on the agent host, under a
    preview's own `/p/{token}/photos/{id}`, which a root-absolute path cannot do from there no matter what it
    reads; `hosted.preview_get` is what rewrites `/photos/` to `/p/{token}/photos/` on the way out for that
    one response, never here and never in storage (see its `_localize_photos`)."""
    if kit["page_id"] != page_id:
        raise ValueError("the kit belongs to another page")
    recipe = landing.load_recipe(tree)
    facts = kit["facts"]
    lines, _missing = required_lines(kit)
    place = facts.get("location") or facts.get("city")
    services = list(dict.fromkeys(s for s in (facts.get("service"), *(facts.get("services") or [])) if s))
    return _env.get_template(f"pages/{tree}.html").render(
        tree=tree,
        sections=[s["id"] for s in recipe["sections"]],
        titles={s["id"]: s["title"] for s in recipe["sections"]},
        cta=recipe["cta"],
        copy=copy,
        title=title_for(copy, kit),
        canonical=f"https://{hostname}/" if hostname else None,
        name=facts.get("name") or "",
        phone=facts.get("phone"),
        tel=_tel(facts.get("phone")),
        booking_url=_booking_url(facts),
        hours=hours or facts.get("hours"),
        location=facts.get("location"),
        city=facts.get("city"),
        map_url=f"https://maps.google.com/?q={quote_plus(place)}" if place else None,
        form={
            "action": f"{agent_base}/t/form/{page_id}",
            "fields": list(kit["rules"]["form_fields"]),
            "services": services,
            # A3p-3 P25: what the form asks for, the business's own vertical's (dentists: an appointment)
            "intent": kit["rules"].get("form_intent") or "consult",
        },
        required_lines=lines,
        photos=[{"src": f"/photos/{p['id']}", "width": p["width"], "height": p["height"]} for p in photos],
        snippet=kit["tracking"]["snippet"],
    )


def _agent_base(settings: config.Settings) -> str:
    """The agent host: the tracking form posts there and the snippet loads from it (never the web base)."""
    agent_base = settings.public_base_url
    return agent_base


def page_hostname(page: dict, settings: config.Settings) -> str | None:
    """The page row's address on the pages domain once a publish gave it one (H2: never changes after that)."""
    host, domain = page.get("hostname"), settings.pages_domain
    return host if host and domain and host.endswith("." + domain) else None


def finish(
    settings: config.Settings,
    kit: dict,
    tree: str,
    copy: PageCopy,
    *,
    photos: list[dict],
    hostname: str | None,
    hours: str | None = None,
) -> tuple[str, list[str]]:
    """`render`, then `hosted.sanitise` on the result as publish will run it: (html, the sanitiser's problems).
    `hostname` is `page_hostname`'s: the canonical link, and the page's own host for the sanitiser."""
    agent_base = _agent_base(settings)
    html = render(
        tree, kit, copy, page_id=kit["page_id"], agent_base=agent_base, photos=photos, hostname=hostname, hours=hours
    )
    return html, hosted.sanitise(html, page_id=kit["page_id"], own_host=hostname, agent_base=agent_base)


# ---------------------------------------------------------------- the model's copy


def model_kit(kit: dict) -> dict:
    """What the model reads of the kit: the facts, the ads' own words, the sensitive-category note and the reviews
    rule -- never the snippet, the checklist or the rules the template applies itself."""
    rules, message = kit["rules"], kit["message"]
    return {
        "facts": kit["facts"],
        "message": {k: message.get(k) for k in ("headline_pattern", "offer", "themes", "headlines", "descriptions")},
        "sensitive": rules["sensitive"],
        "sensitive_note": rules["sensitive_note"],
        "reviews": rules["reviews"],
    }


def model_recipe(recipe: dict, kit: dict) -> dict:
    """What the model reads of the template's recipe: the parts it writes (the hero, the trust points, the service,
    the FAQ) with their guides and examples, the FAQ's questions, the call to action and every banned word."""
    parts = [
        {k: s[k] for k in ("id", "title", "guide", "example") if k in s}
        for s in recipe["sections"]
        if s["id"] in ("hero", "trust", "service", "faq")
    ]
    return {"cta": recipe["cta"], "sections": parts, "faq": recipe["faq"], "banned": _banned(kit, recipe)}


def _line(text: str | None) -> str:
    return " ".join((text or "").split())


def _tidy(copy: PageCopy) -> PageCopy:
    """The model's answer with its whitespace squashed, empty items dropped and each list cut to its limit (the
    model's extra items are dropped quietly; an owner's are refused by `lint`)."""
    return PageCopy(
        headline=_line(copy.headline),
        subheadline=_line(copy.subheadline),
        offer_line=_line(copy.offer_line),
        trust_points=[_line(t) for t in copy.trust_points if t.strip()][:TRUST_MAX],
        service_paragraphs=[_line(t) for t in copy.service_paragraphs if t.strip()][:SERVICE_MAX],
        faq=[FaqItem(q=_line(i.q), a=_line(i.a)) for i in copy.faq if i.q.strip() and i.a.strip()][:FAQ_MAX],
        cta_label=_line(copy.cta_label),
    )


def _direction(text: str | None, notes: list[str] | None) -> str | None:
    """The one instruction the model gets: the owner's direction, and on the second try the linter's notes after it."""
    if not notes:
        return text
    fix = "Your last answer broke these rules; fix them and keep everything else: " + " ".join(notes)
    return f"{text}\n\n{fix}" if text else fix


def _generator(settings: config.Settings) -> llm.Generator:
    try:
        return llm.generator(settings)
    except llm.LLMError as e:
        raise ToolError(str(e)) from e


def _write(
    ask: Callable[[list[str] | None], PageCopy],
    settings: config.Settings,
    kit: dict,
    tree: str,
    *,
    photos: list[str],
    hostname: str | None,
    hours: str | None,
) -> tuple[PageCopy, str]:
    """Ask the model (`ask(None)`), lint its words and sanitise the page they make; with any note, ask once more
    with the notes (`ask(notes)`); a second refusal fails with the notes, as one ToolError sentence."""
    recipe = landing.load_recipe(tree)
    notes: list[str] | None = None
    for _attempt in range(2):
        try:
            copy = _tidy(ask(notes))
        except llm.LLMError as e:
            raise ToolError(str(e)) from e
        html, problems = finish(settings, kit, tree, copy, photos=photos, hostname=hostname, hours=hours)
        notes = lint(copy, kit, recipe) + problems
        if not notes:
            return copy, html
    raise ToolError(REFUSED + " ".join(notes))


def build(
    conn: psycopg.Connection, settings: config.Settings, biz: str, *, template: str | None, brief: str | None
) -> tuple[PageCopy, str]:
    """create_page's words and page (its `prepare`: the model is outside any transaction): the kit (NO_PLAN without a
    plan), the template (`tree_for`), the model's words for it with the owner's `brief` as the instruction -- asked
    once, and once more with the linter's notes -- rendered. New words start with no photos (H35) and the kit's
    own hours."""
    kit = landing.kit(conn, biz)
    tree = tree_for(template, kit)
    recipe = landing.load_recipe(tree)
    gen = _generator(settings)
    direction = (brief or "").strip()[:MAX_DIRECTION] or None
    page = landing.get(conn, kit["page_id"]) or {}

    def ask(notes: list[str] | None) -> PageCopy:
        return gen.write_page(model_kit(kit), model_recipe(recipe, kit), _direction(direction, notes))

    return _write(ask, settings, kit, tree, photos=[], hostname=page_hostname(page, settings), hours=None)


class Beyond(Exception):
    """revise_page's instruction asks for more than the one-page template holds (A3p-3 P22; the model's
    `beyond_template`): nothing is written, and the owner is pointed to their assistant (BEYOND)."""


def revise(
    settings: config.Settings, kit: dict, sections: dict, *, instruction: str, hostname: str | None
) -> tuple[PageCopy, str]:
    """revise_page's words and page: the model gets the page's words as they are (`sections["copy"]`) and the
    owner's instruction -- asked once, and once more with the linter's notes -- under the same rules as a new page;
    the template, the photos and the owner's hours stay. `Beyond` when the model says the instruction asks for more
    than the template holds (another page, a new kind of section, a layout or colour change, a third-party widget)."""
    tree = sections["tree"]
    recipe = landing.load_recipe(tree)
    was = PageCopy.model_validate(sections["copy"])
    gen = _generator(settings)

    def ask(notes: list[str] | None) -> PageCopy:
        out = gen.revise_page(was, _direction(instruction, notes), model_kit(kit), model_recipe(recipe, kit))
        if getattr(out, "beyond_template", False):
            raise Beyond
        return out

    return _write(
        ask,
        settings,
        kit,
        tree,
        photos=list(sections.get("photos") or []),
        hostname=hostname,
        hours=sections.get("hours"),
    )


# ---------------------------------------------------------------- update_page's one part


def _items(text: str) -> list[str]:
    """One item per line, blank lines and a leading bullet ("-", "*", "•", "·") dropped."""
    return [line for line in (_line(re.sub(r"^\s*[-*•·]\s+", "", raw)) for raw in text.splitlines()) if line]


def _faq(text: str) -> list[FaqItem]:
    """One question and its answer per line: the question up to its first "?", the answer after it."""
    out = []
    for line in _items(text):
        q, mark, a = line.partition("?")
        if not mark or not a.strip() or not q.strip():
            raise ToolError(
                "Write each question and its answer on one line, the question ending with “?”: "
                "“Is there parking? Yes, behind the building.”"
            )
        out.append(FaqItem(q=f"{q.strip()}?", a=a.strip()))
    return out


def edit(kit: dict, sections: dict, section: str, text: str) -> tuple[PageCopy, str | None]:
    """update_page's change, exactly as written: (the page's words, the owner's hours line) with one section
    replaced -- headline, subheadline or offer_line as one line; trust (one reason per line) or service (one
    paragraph per line); faq ("question? answer" per line); hours (one line; empty goes back to the kit's). The
    whole page's words are linted again; a refusal is the ToolError sentence `build`'s is."""
    if section not in SECTIONS:
        raise ToolError(
            f"section must be one of headline, subheadline, offer_line, trust, service, faq or hours, not “{section}”."
        )
    copy = PageCopy.model_validate(sections["copy"])
    hours = sections.get("hours")
    recipe = landing.load_recipe(sections["tree"])
    if section == "hours":
        hours = _line(text) or None
    else:
        value: object
        if section in ("headline", "subheadline", "offer_line"):
            value = _line(text)
        elif section in ("trust", "service"):
            value = _items(text)
        else:
            value = _faq(text)
        field = {"trust": "trust_points", "service": "service_paragraphs"}.get(section, section)
        copy = copy.model_copy(update={field: value})
    notes = lint(copy, kit, recipe)
    if section == "hours" and hours:
        notes += _text_notes("The hours", hours, HOURS_CHARS, _rules(kit, recipe))
    if notes:
        raise ToolError(REFUSED + " ".join(notes))
    return copy, hours


# ---------------------------------------------------------------- page.sections and the preview


def current(conn: psycopg.Connection, biz: str, *, lock: bool = False) -> tuple[dict, dict] | None:
    """(the business's one page row, its sections) once Admiry has written a page for it, else None. `lock` holds
    the row (FOR UPDATE) for the rest of the caller's transaction, so two changes never both start from one draft."""
    page = landing.current(conn, biz)
    if page is None:
        return None
    sql = "SELECT sections FROM page WHERE id = %s" + (" FOR UPDATE" if lock else "")
    row = conn.execute(sql, (page["id"],)).fetchone()
    sections = row["sections"] if row else None
    return (page, sections) if sections and sections.get("copy") else None


def save(
    conn: psycopg.Connection,
    page_id: str,
    *,
    tree: str,
    copy: PageCopy,
    photos: list[dict],
    hours: str | None,
    html: str,
    title: str,
    expect_version: int | None = None,
    embed_origin: str | None = None,
) -> tuple[str, dict]:
    """The new draft on the page row (`page.sections`, its `version` one past the row's own) and a preview carrying
    the same sections, in the caller's transaction; returns (the preview token, the sections). `expect_version`: the
    draft the words were made from -- a change that landed meanwhile refuses this one rather than being lost.
    `photos` (A3p-2 Task 6): `[{"id", "width", "height"}, ...]`, stored on `sections["photos"]` exactly as given --
    agent/uploads.py is the only caller that ever passes a non-empty list. `embed_origin` (Task 7, H16): None for
    every caller except create_page/revise_page's own body when it is called from the homepage (tools_hosting.py
    reads it from hosted.current_embed_origin() there; every other caller leaves it at this default)."""
    row = conn.execute("SELECT sections FROM page WHERE id = %s FOR UPDATE", (page_id,)).fetchone()
    old = (row["sections"] if row else None) or {}
    if expect_version is not None and old.get("version") != expect_version:
        raise ToolError("The page changed while Admiry was rewriting it; ask again.")
    sections = {
        "tree": tree,
        "copy": copy.model_dump(),
        "photos": list(photos),
        "version": int(old.get("version") or 0) + 1,
        "hours": hours,
    }
    conn.execute("UPDATE page SET sections = %s WHERE id = %s", (Jsonb(sections), page_id))
    token = hosted.save_preview(conn, page_id, html, title, sections=sections, embed_origin=embed_origin)
    return token, sections
