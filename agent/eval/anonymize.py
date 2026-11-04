"""`python -m agent.eval case-from <business> --id <case-id>` (K5 R-K5b, P10): a business on the dev box as a golden
case -- its knowledge, its latest consultation's opening words and answers, the budget its owner set -- with what could
identify it replaced: its name wherever it appears (and each word of it that is not a trade's or a town's), phones,
e-mail and web addresses, the street, the map point past two decimals, the place id; its reviews dropped. Printed for
the executor to read field by field, and committed only with the founder's yes for that business. Refused on prod.

The knowledge is built field by field from `HANDLED`, never copied whole: a field the business's knowledge carries that
this module does not name (the owner's `notes`, `offers`, `phone_hours`, ...) is left out, so a new field can only
be missing from a case, never leak into one; a test holds `HANDLED` to `models.Knowledge`'s fields, so a new field
is named here, and scrubbed or dropped, when it is added."""

from __future__ import annotations

import hashlib
import re
import tempfile
import unicodedata
from pathlib import Path

import psycopg

from .. import business, consult
from .cases import CaseError, load, write

FAKE_NAMES = {
    "dentists": ("Maplewood Dental", "Riverbend Family Dentistry", "Oak Hollow Dental Care", "Clearwater Smiles"),
    "home_services": (
        "Northgate Home Services",
        "Ridgeline Plumbing & Air",
        "Copperfield Roofing",
        "Elm Street Electric",
    ),
    "attorneys": ("Hawthorne Law Group", "Birchwood Legal", "Stonebridge Law Office", "Willow Creek Attorneys"),
}
TRADE_WORDS = frozenset(
    {
        *("family", "dental", "dentistry", "dentist", "dentists", "smile", "smiles", "care", "clinic", "center"),
        *("centre", "office", "group", "firm", "legal", "attorney", "attorneys", "lawyer", "lawyers", "injury"),
        *("estate", "immigration", "plumbing", "plumber", "roofing", "roofer", "electric", "electrical"),
        *("electrician", "heating", "cooling", "services", "service", "home", "landscape", "landscaping"),
        *("remodeling", "remodel", "kitchen", "bath", "company", "studio", "associates", "partners"),
    }
)
# Every field of models.Knowledge, each one named in `case_of` below as copied, scrubbed, replaced or dropped
HANDLED = frozenset(
    {
        *("name", "category", "category_detail", "service_area", "stated_address", "services", "hours", "phone"),
        *("website", "pricing_hints", "typical_customer_value_usd", "usp", "reviews_summary", "compliance_flags"),
        *("locale", "goal_hint"),
    }
)
STREET = "100 Main St"
COMPLIANCE_FLAGS = (
    "healthcare",
    "legal",
    "financial",
    "housing",
    "alcohol",
    "cannabis",
)  # models.Knowledge's vocabulary
_BAND = re.compile(r"^(?:lt_(\d+)|(\d+)_(\d+)|(\d+)_plus)$")

# ---- what is looked for. Over-replacement is the rule: the printed case is committed, so a pattern that takes a word
# ---- too many costs a tidier-looking field, and one that takes a word too few costs a person's name.
# What starts at a word's start only (the (?<![...]) at the head of each): a match that may start inside a long run of
# word characters is tried from every character of it, and a run of 50,000 takes minutes.
_EMAIL = r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?:/\S*)?"  # and a path: "<digits>@facebook.com/name" is a link
_AT_DOT = r"[\[(]\s*(?:at|dot)\s*[\])]"  # [at] (at) [dot] (dot)
_OBFUSCATED = rf"(?<![\w.+-])[\w.+-]+(?:\s*{_AT_DOT}\s*[\w.+-]+)+"  # hello [at] name [dot] com
# A domain starts after no word character, hyphen or "@", and after a dot only when that dot follows no word's last
# character ("see:.name.com", "online...name.com"): a start after "a." is inside a domain, and is refused so a long
# "a.a.a..." is tried once, not from each of its letters.
_URL = r"(?:https?://|www\.)\S+|(?<![\w@-])(?<![\w-]\.)(?:[\w-]+\.)+[a-z]{2,}\b(?:/\S*)?"  # scheme, www, name.tld/path
_HANDLE = r"(?<![\w@])@[\w.]{2,}(?:/\S*)?"  # @name, and a profile link written as one (@facebook.com/name)
# The removal that runs again over what the scan left: an address glued to what the scan matched just before it
# ("(312) 555-7788.name@mail.com", "1234 N Clark St.name@mail.com", "<the name>-name@mail.com") is not at the start of a
# word in the text it was found in, because that match took its left neighbour; in the text the scan leaves, the fake
# phone, street or name stands there instead and is a word the address starts at. Any "@" still there goes with the word
# before it and the word after it and a path (a domain the name's replacement cut, an "@name" after a digit, a profile
# link after one); any [at] or [dot] goes with the word before it, whether or not a word follows (the scan may have
# taken "gmail [dot] com" on its own and left "name [at]"): no address is left whole enough to read.
_LEFT_OVER = rf"(?<![\w.+-])(?:[\w.+-]+\s*)?{_AT_DOT}(?:\s*[\w.+@-]+)?(?:\s*{_AT_DOT}(?:\s*[\w.+@-]+)?)*"
_STRIP = re.compile(rf"(?<![\w.+-])[\w.+-]*@[\w.-]*(?:/\S*)?|{_LEFT_OVER}|{_URL}", re.IGNORECASE)
# What a phone's digits are separated by: spaces, dots, slashes, underscores, a middle dot, any dash
_DOT_AND_DASHES = (
    "\N{MIDDLE DOT}\N{HYPHEN}\N{NON-BREAKING HYPHEN}\N{FIGURE DASH}\N{EN DASH}\N{EM DASH}\N{HORIZONTAL BAR}"
    "\N{MINUS SIGN}"
)
_SEP = rf"[\s./_{_DOT_AND_DASHES}-]"
_TEN_DIGITS = (
    rf"(?<!\d)(?:\+?1{_SEP}*)?\(?\d{{3}}\)?{_SEP}*\d{{3}}{_SEP}*\d{{4}}(?!\d)"  # +1 (312) 555-7788, 13125557788
)
_PHONE = re.compile(
    rf"(?<!\d)(?:\+?1{_SEP}*)?\(?(\d{{3}})\)?{_SEP}*\d{{3}}{_SEP}*\d{{4}}(?!\d)"
)  # group 1: the area code
_SEVEN_DIGITS = rf"(?<!\d)\d{{3}}{_SEP}{{1,3}}\d{{4}}(?!\d)"  # 555-7788
_LETTER = r"[^\W\d_]"  # any letter of any alphabet
# Where a name word may start: not after a letter, or at the hump of a camel-case glue ("DrKowalski", "SmithAndJones").
# A name word may be glued to what follows it ("Kowalskis", "kowalskidental"); a run of glued name words is one match.
_LEFT = rf"(?:(?<!{_LETTER})|(?-i:(?<=[a-z])(?=[A-Z])))"
_MARKS = f"[{chr(0x300)}-{chr(0x36F)}]*"  # a combining accent that was not folded into its letter
_EAST_ASIAN = "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x3040, 0x30FF), (0x3400, 0x9FFF), (0xAC00, 0xD7AF)))
_LETTERS = re.compile(rf"{_LETTER}{{4,}}|[{_EAST_ASIAN}]{{2,}}")  # 4+ letters, or 2+ of kana, CJK or hangul
_TOKENS = re.compile(r"[^\W_]+")
_STREET_PART = re.compile(r"^\d+(?:-\d+)?[A-Za-z]?\s+\S")  # "1234 N Clark St", "12-14 Oak Ln"; not a ZIP code
_HOUSE = re.compile(r"\d+(?:-\d+)?[A-Za-z]?")
_COUNTRY = re.compile(r"(?:usa|u\.s\.a?\.?|us|united states(?: of america)?)", re.IGNORECASE)
_STATE_ZIP = re.compile(r"(?:[A-Za-z]{2}(?:\s+\d{5}(?:-\d{4})?)?|\d{5}(?:-\d{4})?)")  # "IL 60610", "IL", "60610"
_UNIT_PART = re.compile(r"(?:#|(?:suite|ste|apt|apartment|unit|bldg|building|floor|room|rm)\b)", re.IGNORECASE)
_UNIT_TAIL = r"(?:,?\s+(?:suite|ste|apt|apartment|unit|bldg|building|floor|room|rm)\.?\s*#?[\w-]+|,?\s*#[\w-]+)?"
_SUFFIXES = (
    *("street", "avenue", "boulevard", "parkway", "highway", "terrace", "circle", "square", "place", "court", "drive"),
    *("trail", "road", "lane", "way", "ave", "blvd", "pkwy", "hwy", "ter", "cir", "trl", "av", "st", "rd", "dr"),
    *("ln", "ct", "pl", "sq"),
)
_DIRECTIONS = ("north", "south", "east", "west", "ne", "nw", "se", "sw", "n", "s", "e", "w")
_SUFFIX = "(?:" + "|".join(_SUFFIXES) + ")"
_DIRECTION = "(?:" + "|".join(_DIRECTIONS) + ")"
_SPECIAL = {"đ": "d", "ð": "d", "ø": "o", "ł": "l", "ħ": "h", "ı": "i"}  # letters with no accent to fold off


def _fold(text: str) -> str:
    """Lower case with the accents off: how a word reads when it is typed without them."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return "".join(_SPECIAL.get(c, c) for c in plain.casefold())


def _norm(text: str) -> str:
    """Composed accents and straight apostrophes, so one spelling of a word is one spelling."""
    return unicodedata.normalize("NFC", text).replace("’", "'").replace("‘", "'").replace("ʼ", "'")


def _accented() -> dict[str, str]:
    """For each ASCII letter, the letters that fold to it (n: ñ ń ň ..., e: é ê ễ ...)."""
    found: dict[str, set[str]] = {}
    for code in (*range(0xC0, 0x250), *range(0x1E00, 0x1F00)):
        char = chr(code)
        base, lower = _fold(char), char.lower()
        if char.isalpha() and len(base) == 1 and base.isascii() and len(lower) == 1:
            found.setdefault(base, set()).add(lower)
    return {base: "".join(sorted(chars)) for base, chars in found.items()}


_ACCENTED = _accented()


def _word_pattern(word: str) -> str:
    """A regex for `word` in any case, with or without its accents ("Nuñez" is "Nunez" is "NUÑEZ")."""
    out = []
    for char in unicodedata.normalize("NFKD", _norm(word)):
        if unicodedata.combining(char):
            continue
        base = _fold(char)
        letter = len(base) == 1 and base.isascii() and base.isalpha()
        out.append((f"[{base}{_ACCENTED.get(base, '')}]" if letter else re.escape(char)) + _MARKS)
    return "".join(out)


def _name_pattern(name: str) -> str | None:
    """A regex for a whole name in any spacing and punctuation between its words; one word of three letters or fewer
    is a name only as a whole word (a short name is no part of other words), any other is a name where it starts."""
    tokens = _TOKENS.findall(_norm(name))
    if not tokens:
        return None
    short = len(tokens) == 1 and len(tokens[0]) <= 3
    return _LEFT + r"[\W_]{0,4}".join(_word_pattern(t) for t in tokens) + (rf"(?!{_LETTER})" if short else "")


def _street_shape(part: str) -> tuple[str | None, list[str]]:
    """What a dropped address part is written as in free text: its house number, if it has one, and the regexes for
    its name with its street suffix ("Clark Street", "N. Clark St", "One Kowalski Plaza", "PO Box 12")."""
    house, runs = None, []
    for piece in re.split(r"\s*&\s*|\s+and\s+", _norm(part), flags=re.IGNORECASE):
        tokens = piece.replace(",", " ").split()
        if tokens and _HOUSE.fullmatch(tokens[0]):
            house, tokens = house or tokens[0], tokens[1:]
        while tokens and tokens[0].strip(".").lower() in _DIRECTIONS:
            tokens = tokens[1:]
        suffixed = False
        while tokens and tokens[-1].strip(".").lower() in (*_SUFFIXES, *_DIRECTIONS):
            suffixed = suffixed or tokens[-1].strip(".").lower() in _SUFFIXES
            tokens = tokens[:-1]
        words = [w for t in tokens for w in _TOKENS.findall(t)]
        if sum(len(w) for w in words) < 3:
            continue
        name = r"[\s'.-]+".join(_word_pattern(w) for w in words)
        tail = rf"\s+{_SUFFIX}\b\.?" if suffixed else rf"(?:\s+{_SUFFIX}\b\.?)?"
        runs.append(rf"{_LEFT}(?:{_DIRECTION}\b\.?\s+)?{name}{tail}{_UNIT_TAIL}")
    return house, runs


def _house_runs(houses: set[str]) -> list[str]:
    """Regexes for a run that starts with a house number as a whole number: up to a street suffix within five words
    ("1234 N. Clark Street", "1234 North Clark St"), else, for a number of three digits or more, up to the next comma,
    sentence end or the end ("1234 N. Clark")."""
    runs = []
    for long in (False, True):
        group = sorted((h for h in houses if (len(re.sub(r"\D", "", h)) >= 3) == long), key=len, reverse=True)
        if not group:
            continue
        numbers = "|".join(re.escape(h) for h in group)
        start = rf"(?<![\w$.,/#-])(?:{numbers})(?!\w)(?=\s+[A-Za-z])"
        runs.append(rf"{start}(?:\s+[^\s,]+){{0,5}}?\s+{_SUFFIX}\b\.?{_UNIT_TAIL}")
        if long:
            runs.append(rf"{start}(?:\s+(?:[A-Za-z]{{1,4}}\.|[^\s,]*[^\s,.]))*{_UNIT_TAIL}")
    return runs


class _Scrub:
    """One business's replacements, applied to any text in one pass (what replaces a word is never looked at again):
    e-mail and web addresses, handles and phones; the street in the forms it is written in; the name, and each word of
    it that is neither a trade's nor one of its towns', in any case, accent and apostrophe, glued to what follows."""

    def __init__(self, names: list, towns: list, streets: list, fake: str, phone: str) -> None:
        names = sorted({_norm(n).strip() for n in names if isinstance(n, str) and n.strip()}, key=len, reverse=True)
        kept = {_fold(w) for t in towns if isinstance(t, str) for w in _LETTERS.findall(_norm(t))} | TRADE_WORDS
        own = {w for n in names for w in _LETTERS.findall(n) if _fold(w) not in kept}
        houses, runs = set(), []
        for street in streets:
            if isinstance(street, str):
                house, found = _street_shape(street)
                houses |= {house} if house else set()
                runs += found
        groups = [
            ("email", _EMAIL),
            ("hidden", _OBFUSCATED),
            ("url", _URL),
            ("handle", _HANDLE),
            ("phone", _TEN_DIGITS),
            ("local", _SEVEN_DIGITS),
            ("house", "|".join(_house_runs(houses))),
            ("street", "|".join(runs)),
            ("name", "|".join(p for p in map(_name_pattern, names) if p)),
            (
                "word",
                _LEFT + "(?:" + "|".join(_word_pattern(w) for w in sorted(own, key=len, reverse=True)) + ")+"
                if own
                else "",
            ),
        ]
        self._rx = re.compile("|".join(f"(?P<{k}>{p})" for k, p in groups if p), re.IGNORECASE)
        self.fake, self.phone, self.first = fake, phone, fake.split()[0]
        self._by_kind = {
            "phone": phone,
            "local": phone.split("-", 1)[1],
            "house": STREET,
            "street": STREET,
            "name": fake,
            "word": self.first,
        }

    def __call__(self, text):
        if not isinstance(text, str):
            return text
        out = self._rx.sub(lambda m: self._by_kind.get(m.lastgroup or "", ""), _norm(text))
        out = _STRIP.sub("", out)  # what is left glued to a replacement goes with it (the fake text is not identifying)
        out = re.sub(r"\(\s*\)", "", out)
        out = re.sub(
            r"(?<!\s)\s+([,;])", r"\1", out
        )  # (from the start of a run of spaces: from each, a run is quadratic)
        out = re.sub(r"([,;])(?:\s*[,;])+", r"\1", out)  # what was between two commas is gone
        return re.sub(r"\s{2,}", " ", out).strip(" ,;")


def _split_address(address) -> tuple[bool, list[str], list[str]]:
    """An address as (whether it had a numbered street, the parts to drop, the parts to keep). The numbered street and
    any building before it go; so do units, a country and what lies between the street and the town. What is kept is
    the town and the state with its ZIP code (the last two parts when the last is a state or a ZIP code, else the last
    one): without a numbered street ("One Kowalski Plaza", "PO Box 12", "N Clark St & Division St") nothing earlier."""
    if not isinstance(address, str):
        return False, [], []
    parts = [p.strip() for p in _norm(address).split(",") if p.strip()]
    while parts and _COUNTRY.fullmatch(parts[-1]):
        parts.pop()
    at = next((i for i, p in enumerate(parts) if _STREET_PART.match(p)), None)
    tail = [p for p in (parts if at is None else parts[at + 1 :]) if not _UNIT_PART.match(p)]
    area = tail[-2:] if tail and _STATE_ZIP.fullmatch(tail[-1]) else tail[-1:]
    if at is None and len(tail) == 1 and not _STATE_ZIP.fullmatch(tail[0]):
        area = []  # one part and no street number: a building, a box or a street, never known to be a town
    dropped = [*([] if at is None else parts[: at + 1]), *tail[: len(tail) - len(area)]]
    return at is not None, dropped, area


def _band(options: list[dict], amount: float) -> str | None:
    """The value option whose band holds `amount` (`lt_300`, `300_1000`, `3000_plus`, ...); None when none does."""
    for o in options:
        m = _BAND.match(o["id"])
        if not m:
            continue
        below, low, high, plus = m.groups()
        if (
            (below and amount < float(below))
            or (low and float(low) <= amount < float(high))
            or (plus and amount >= float(plus))
        ):
            return o["id"]
    return None


def _answer(stored: dict, before: list[dict], after: list[dict], scrub: _Scrub) -> dict | None:
    """A stored answer as a case gives it (`{"choice"}` or `{"other"}`), against the options a session of the scrubbed
    business offers; None when it cannot be given."""
    choice = stored.get("choice")
    if isinstance(choice, list):
        choice = choice[0] if choice else None
    if isinstance(choice, str):
        ids_before, ids_after = [o["id"] for o in before], [o["id"] for o in after]
        if choice in ids_after:
            return {"choice": choice}
        if choice in ids_before and len(ids_before) == len(ids_after):  # a service renamed by the scrub
            return {"choice": ids_after[ids_before.index(choice)]}
        return None
    if isinstance(stored.get("miles"), int | float):
        hit = next((o["id"] for o in after if o.get("miles") == stored["miles"]), None)
        return {"choice": hit} if hit else {"other": f"{stored['miles']:g} miles"}
    if isinstance(stored.get("amount_usd"), int | float):
        hit = _band(after, float(stored["amount_usd"]))
        return {"choice": hit} if hit else {"other": f"${stored['amount_usd']:,.0f}"}
    note = scrub(stored.get("note")) if isinstance(stored.get("note"), str) else None
    return {"other": note} if note else None


def _trade_of(vertical: str, original: dict) -> str | None:
    """The trade a home-services business's words name (`hvac`, `plumbing`, ...: sites.intake's reading, from its
    `trade` and its category's detail and its services), which picks the services the consultation leads with
    (consult_trees/home_services.json `by_trade`); one of that vocabulary's own words, never the business's text. None
    for any other trade, or when its words name none."""
    from ..sites import intake  # it imports consult: read when asked, as consult reads it

    found = intake.family_for(original) if vertical == "home_services" else None
    return found[1] if found else None


def _items(value) -> list:
    """`value` when it is a list, else nothing: stored knowledge is as a model or an owner's patch wrote it."""
    return value if isinstance(value, list) else []


def _list(scrub: _Scrub, values) -> list[str]:
    """A list of words scrubbed, what scrubs to nothing left out."""
    return [s for s in (scrub(x) for x in _items(values) if isinstance(x, str)) if s]


def _flags(values) -> list[str]:
    """The compliance flags that are words of the vocabulary models.Knowledge names, as written there: the scrub would
    turn "healthcare" into "Maplewoodcare" for a business named "... Health ...", and a flag outside it is free text."""
    seen = (v.strip().lower() for v in _items(values) if isinstance(v, str))
    return [v for v in seen if v in COMPLIANCE_FLAGS]


def _same(text: str) -> str:
    """A part of an address or a service-area entry as it compares: accents, case, spacing and apostrophes aside."""
    return " ".join(_fold(_norm(text)).split())


def _number(value) -> int | float | None:
    """A figure copied only when it is a number: a figure's field holding words is dropped, never copied unscrubbed."""
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def case_of(conn: psycopg.Connection, business_id: str, case_id: str) -> dict:
    """The business as a case of the golden set, scrubbed; CaseError when it cannot be one (no knowledge, a trade with
    no golden set, an id not of its trade, no confirmed listing, no consultation)."""
    latest = business.latest_knowledge(conn, business_id)
    if latest is None:
        raise CaseError(f"{business_id}: no knowledge to make a case of")
    original = latest[1]
    vertical = original.get("category") or "fallback"
    if vertical not in FAKE_NAMES or consult.tree_for(vertical) != vertical:
        raise CaseError(f"{business_id}: its trade {vertical!r} has no golden set (dentists, home_services, attorneys)")
    if not re.fullmatch(rf"{vertical}-\d+", case_id):
        raise CaseError(f"{business_id}: a {vertical} case's id is {vertical}-<n>, not {case_id!r}")
    listing = original.get("listing") if isinstance(original.get("listing"), dict) else {}
    if not (listing.get("confirmed") is True and listing.get("lat") is not None and listing.get("lng") is not None):
        raise CaseError(f"{business_id}: a case needs the business's confirmed Google listing with its point")
    session = conn.execute(
        "SELECT opening_text, answers FROM consult_session WHERE business_id = %s AND tree = %s "
        "ORDER BY (status = 'planned') DESC, created_at DESC LIMIT 1",
        (business_id, vertical),
    ).fetchone()
    if session is None:
        raise CaseError(f"{business_id}: no consultation to take the answers from")
    seed = int(hashlib.sha256(business_id.encode()).hexdigest(), 16)
    fake = FAKE_NAMES[vertical][seed % len(FAKE_NAMES[vertical])]
    area_code = _PHONE.search(original.get("phone") if isinstance(original.get("phone"), str) else "")
    phone = f"{area_code.group(1) if area_code else '555'}-555-01{seed % 100:02d}"
    numbered, dropped, kept = _split_address(listing.get("address"))
    _, stated_dropped, stated_kept = _split_address(original.get("stated_address"))
    # a service-area entry that is a part dropped from the address (a neighbourhood between the street and the town) is
    # dropped from the list, never rewritten into a street: the plan reads the list
    gone = {_same(p) for p in (*dropped, *stated_dropped)}
    areas = [a for a in _items(original.get("service_area")) if not (isinstance(a, str) and _same(a) in gone)]
    # a town protects a name word that is also its own; a street, a building or a first part of an address never does
    towns = [*areas, *kept, *stated_kept]
    scrub = _Scrub([original.get("name"), listing.get("name")], towns, [*dropped, *stated_dropped], fake, phone)
    hint = original.get("goal_hint") if isinstance(original.get("goal_hint"), dict) else {}
    known = {
        "name": fake,
        "category": vertical,
        "category_detail": scrub(original.get("category_detail")) or "",
        "service_area": _list(scrub, areas),
        # stated_address: the owner's own street, dropped (the confirmed listing's town, state and ZIP stay)
        "services": [
            {
                "name": scrub(s.get("name")),
                "intent_phrases": _list(scrub, s.get("intent_phrases")),
                "price_hint": scrub(s.get("price_hint")) or None,
            }
            for s in _items(original.get("services"))
            if isinstance(s, dict)
        ],
        "hours": scrub(original.get("hours")) or None,
        "phone": phone,
        "website": None,
        "pricing_hints": _list(scrub, original.get("pricing_hints")),
        "typical_customer_value_usd": _number(original.get("typical_customer_value_usd")),
        "usp": _list(scrub, original.get("usp")),
        "reviews_summary": None,  # the reviewers' names
        "compliance_flags": _flags(original.get("compliance_flags")),  # a closed vocabulary, never scrubbed
        "locale": scrub(original.get("locale")) or "en-US",
        "goal_hint": {
            "target_customers_per_month": _number(hint.get("target_customers_per_month")),
            "monthly_budget_usd": _number(hint.get("monthly_budget_usd")),
            "focus_services": _list(scrub, hint.get("focus_services")),
        },
    }
    if trade := _trade_of(vertical, original):
        known["trade"] = trade
    try:
        point = {"lat": round(float(listing["lat"]), 2), "lng": round(float(listing["lng"]), 2)}
    except (TypeError, ValueError):
        raise CaseError(f"{business_id}: its listing's point is not a pair of numbers") from None
    known["listing"] = {
        "place_id": "eval_" + case_id.replace("-", "_"),
        "name": fake,
        # the kept parts are scrubbed, the street put in front after: what the case adds is never scrubbed again
        "address": ", ".join([*([STREET] if numbered else []), *(p for p in map(scrub, kept) if p)]) or None,
        **point,
        "confirmed": True,
    }
    tree = consult.load_tree(vertical)
    before, after = consult.build_options(tree, original), consult.build_options(tree, known)
    answers = {
        consult.WEBSITE: {"choice": "none"},
        "business_name": {"other": fake},
        "services_offered": {"other": "skip"},
    }
    for qid, stored in (session["answers"] or {}).items():
        if qid in answers or qid in ("listing", "location") or not isinstance(stored, dict):
            continue
        given = _answer(stored, before.get(qid) or [], after.get(qid) or [], scrub)
        if given is not None:
            answers[qid] = given
    goal = business.latest_goal(conn, business_id)
    budget = goal["monthly_limit_usd"] if goal and goal.get("limit_source") == "owner" else None
    return {
        "id": case_id,
        "vertical": vertical,
        "opening": scrub(session["opening_text"]),
        "monthly_budget_usd": budget,
        "knowledge": known,
        "answers": answers,
        "plan": None,
        "founder_grades": None,
        "founder_notes": None,
    }


def validated(case: dict) -> dict:
    """The case as `cases.load` reads it from its own file; CaseError with the reason when it is not one (an answer the
    session would not offer, knowledge the schema refuses)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{case['id']}.json"
        write(path, case)
        load(path)
    return case
