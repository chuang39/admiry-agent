"""What an owner reads from a role passes these checks first (spec 2026-10-07-admiry-capability-layer-design §4:
numbers are computed, never generated; the digest spec 2026-09-30 §6.3's guards, first written for the digest's
comment): it is there and grounded, short, carries no link, address, phone number or markup, states no number the
material does not hold, and uses no advertising jargon. Pure: it reads only its arguments."""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from .. import copy

EMPTY, UNGROUNDED, LONG, LINK, NUMBER, JARGON = "empty", "ungrounded", "long", "link", "number", "jargon"
WORD_NUMBERS = {
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
}
ONE_EXEMPT = ("one of", "the one", "no one", "each one", "one thing", "any one")  # "one" is no number there
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\w)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
# a number read whole (12,000 is 12000, never 12) and an ordinal as its number (the 5th is 5); what is glued to a
# letter or left half-read (1.2x, 5k, 12,5) matches nothing here and leaves a digit behind, which fails the check
_PLAIN = re.compile(
    r"(?<![\w$.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:st|nd|rd|th)?(?![\w%]|[.,]\d)", re.IGNORECASE
)
_DIGIT = re.compile(r"\d")
_HELD = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_LINK = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}|[<>]")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_WORDS = re.compile(r"[a-z]+")


def _amounts(text: str) -> set[Decimal]:
    """Every amount after a $, its thousands separators dropped."""
    out = set()
    for m in _MONEY.finditer(text):
        whole = m.group(1).replace(",", "")
        out.add(Decimal(f"{whole}.{m.group(2)}" if m.group(2) else whole))
    return out


def _read(text: str) -> tuple[set[Decimal], set[Decimal], str]:
    """(the money amounts, the other numbers, what is left once they are read) of a text."""
    money = _amounts(text)
    rest = _MONEY.sub(" ", text)
    other = {Decimal(m.group(1)) for m in _PERCENT.finditer(rest)}
    rest = _PERCENT.sub(" ", rest)
    other |= {Decimal(m.group(1).replace(",", "")) for m in _PLAIN.finditer(rest)}
    rest = _PLAIN.sub(" ", rest)
    words = rest.lower()
    for phrase in ONE_EXEMPT:
        words = words.replace(phrase, " ")
    other |= {Decimal(WORD_NUMBERS[w]) for w in _WORDS.findall(words) if w in WORD_NUMBERS}
    return money, other, rest


def tokens(text: str) -> tuple[set[Decimal], set[Decimal]]:
    """(the money amounts, the other numbers) a text states: amounts after a $ first, then percentages, then plain
    numbers (read whole, an ordinal as its number), then number words ("one" not inside ONE_EXEMPT, where it is no
    number)."""
    money, other, _ = _read(text)
    return money, other


def known(material_text: str) -> set[Decimal]:
    """Every number the material holds as it writes it: its $ amounts whole ($1,234.50 is 1234.50), then the rest."""
    return _amounts(material_text) | {Decimal(n) for n in _HELD.findall(_MONEY.sub(" ", material_text))}


def sentences(text: str) -> int:
    """How many sentences: a full stop, ! or ? followed by a space or the end closes one (23.10 closes none)."""
    return len([s for s in _SENTENCE_END.split(text) if s.strip()])


def _whole(x: Decimal) -> Decimal:
    return x.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def check(
    text: str,
    grounded: bool,
    material_text: str,
    *,
    extra: Iterable[Decimal] = (),
    max_sentences: int = 3,
    max_chars: int = 450,
) -> str | None:
    """None when an owner may read `text`; else why not, the first of empty, ungrounded, long, link, number, jargon. A
    money amount passes when the material holds it to the cent or rounded half up to whole dollars ($1,235 for
    $1,234.50); any other number when the material or `extra` (counts and days the caller knows) holds it. A phone
    number is reported as a link, never as a number."""
    if not text or not text.strip():
        return EMPTY
    if not grounded:
        return UNGROUNDED
    if sentences(text) > max_sentences or len(text) > max_chars:
        return LONG
    if _LINK.search(text):
        return LINK
    held = known(material_text)
    rounded = {_whole(x) for x in held}
    money, other, rest = _read(text)
    allowed = held | set(extra)
    if any(m not in held and m not in rounded for m in money) or any(o not in allowed for o in other):
        return NUMBER
    if _DIGIT.search(rest):  # a number it could not read whole (1.2x, 5k, 12,5) is one it cannot vouch for
        return NUMBER
    if copy.jargon(text):
        return JARGON
    return None
