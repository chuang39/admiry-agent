"""Local rules drawn from what Google refused (spec 2026-10-12-admiry-google-refusal-knowledge-design §2.2, §2.3;
Part B phase 1): `refusal_rule` rows -- a whole word or phrase (`word`, case-insensitive) or one of SHAPES
(`pattern`: a fixed set of bounded regular expressions chosen by id; never a regular expression of the model's or a
form's own) -- on headlines, descriptions, search phrases or any. One pure `check(lines, rules)` is what every reader
runs: the Google build (build.Dropped.holds: a `block` rule's line is left out before any build, as a line Google
refused by its exact text is -- plan D3), the checklist (launch_check.refused_items: a `warn` finding is a
non-blocking item, a `block` finding a blocking one), the Reviewer (`stamp`: the active rule a refused line matched,
the summary's hits) and the plan writer's prompt (ads_refusal.avoid_words, through `tells`). `load` reads the active
rules while AGENT_REFUSAL_RULES is on and nothing while it is off -- off, every reader sees no rule (spec §4). Owner
words only in `why`; Google's topic stays on admin surfaces."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

import psycopg

from . import config
from .util import iso

KINDS = ("word", "pattern")
WORD, PATTERN = KINDS
APPLIES = ("headline", "description", "keyword", "any")
ANY = "any"
LEVELS = ("warn", "block")
WARN, BLOCK = LEVELS
STATUSES = ("proposed", "active", "rejected", "retired")
PROPOSED, ACTIVE, REJECTED, RETIRED = STATUSES
WORD_MIN, WORD_MAX = 2, 40
AVOID_MAX = 40  # the plan writer's words to avoid, at most (ads_refusal.avoid_words)
_WORD_OK = re.compile(r"^[a-z0-9][a-z0-9' -]*[a-z0-9]$")
# function words a rule may never be: a `word` rule of "the" would drop every line
STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "this",
        "to",
        "we",
        "with",
        "you",
        "your",
    ]
)
COLS = (
    "id, kind, applies_to, value, topic, why, level, status, evidence, proposed_by, decided_by, decided_at, created_at"
)
BAD_KIND = "A rule is a word or a pattern."
BAD_APPLIES = "A rule applies to a headline, a description, a search phrase, or any of them."
BAD_LEVEL = "A rule warns or blocks."
BAD_WORD = f"A word rule is {WORD_MIN} to {WORD_MAX} letters, digits, spaces, apostrophes or hyphens, with at least one letter."
STOP_WORD = "That word is too common to be a rule."
BAD_SHAPE = "A pattern rule is one of the shapes: {shapes}."


@dataclass(frozen=True)
class Shape:
    """One bounded pattern a `pattern` rule may stand for (plan D1): its id (the rule's `value`), what it looks like
    in words (the admin page, the model's material, the plan writer's words to avoid) and the regular expression --
    in code, with its test, never from input."""

    id: str
    tell: str
    regex: re.Pattern


SHAPES: dict[str, Shape] = {
    s.id: s
    for s in (
        Shape("repeated_symbols", "a symbol repeated or piled up (!!, $$, ?!)", re.compile(r"[!?$%*#@&]{2,}")),
        Shape(
            "symbol_in_word",
            "a symbol in place of a letter ($ave, fr*e)",
            re.compile(r"(?<!\S)[$@*][A-Za-z]{2,}\b|[A-Za-z][$@#*!]+[A-Za-z]"),
        ),
        Shape("spaced_letters", "letters spaced out (S A L E)", re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")),
        Shape(
            "number_one",
            "a number-one claim (#1, No. 1)",
            re.compile(r"(?<![\w$])#\s?1(?!\d)|\bno\.\s?1(?!\d)", re.IGNORECASE),
        ),
        Shape("ellipsis", "a run of dots or an ellipsis (...)", re.compile(r"\.{3,}|…")),
        Shape(
            "web_address",
            "a web address in the line (www., .com)",
            re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|co|io|us)\b", re.IGNORECASE),
        ),
        Shape("all_caps_run", "two or more words in all capitals", re.compile(r"\b[A-Z]{3,}(?: [A-Z]{3,})+\b")),
    )
}


@dataclass(frozen=True)
class Rule:
    """An active rule as the readers hold it: the row's fields and its compiled pattern."""

    id: str
    kind: str
    applies_to: str
    value: str
    level: str
    why: str
    topic: str | None
    regex: re.Pattern


@dataclass(frozen=True)
class Finding:
    """One line an active rule matched: the rule (its id, level, why, topic, value) and the line (its kind, text)."""

    rule_id: str
    level: str
    why: str
    topic: str | None
    value: str
    line_kind: str
    text: str


def validate(kind: str, value: object) -> str:
    """The stored `value` for a rule of `kind`, or ValueError with a sentence (plan D2): a word rule lower-cased with
    its spaces collapsed, WORD_MIN..WORD_MAX of letters, digits, spaces, apostrophes and hyphens, at least one letter,
    no STOP word; a pattern rule one of SHAPES' ids. The one check for the model's proposal, the admin's form and the
    MCP."""
    if kind not in KINDS:
        raise ValueError(BAD_KIND)
    text = " ".join(str(value or "").split()).lower()
    if kind == PATTERN:
        if text not in SHAPES:
            raise ValueError(BAD_SHAPE.format(shapes=", ".join(SHAPES)))
        return text
    if not WORD_MIN <= len(text) <= WORD_MAX or not _WORD_OK.fullmatch(text) or not re.search(r"[a-z]", text):
        raise ValueError(BAD_WORD)
    if text in STOP:
        raise ValueError(STOP_WORD)
    return text


def compile_rule(row: dict) -> Rule:
    """A Rule from a `refusal_rule` row, or a dict shaped like one (a proposal under validation: no id yet). A word
    is matched whole, case-insensitively, with `(?<!\\w)…(?!\\w)` as the linter's `_banned_hit` does -- `\\b` cannot
    sit before a symbol, so "#1" would never be caught by it."""
    if row["kind"] == PATTERN:
        regex = SHAPES[row["value"]].regex
    else:
        regex = re.compile(r"(?<!\w)" + re.escape(row["value"]) + r"(?!\w)", re.IGNORECASE)
    return Rule(
        id=row.get("id") or "",
        kind=row["kind"],
        applies_to=row["applies_to"],
        value=row["value"],
        level=row["level"],
        why=row["why"],
        topic=row.get("topic"),
        regex=regex,
    )


def load(conn: psycopg.Connection, settings: config.Settings, *, levels: tuple[str, ...] = LEVELS) -> list[Rule]:
    """The active rules of `levels`, oldest first -- none while AGENT_REFUSAL_RULES is off (spec §4)."""
    if not settings.refusal_rules:
        return []
    rows = conn.execute(
        f"SELECT {COLS} FROM refusal_rule WHERE status = %s AND level = ANY(%s) ORDER BY created_at, id",
        (ACTIVE, list(levels)),
    ).fetchall()
    return [compile_rule(r) for r in rows]


def block_rules(conn: psycopg.Connection, settings: config.Settings) -> list[Rule]:
    """The active `block` rules: what every Google build leaves out (build.Dropped.rules)."""
    return load(conn, settings, levels=(BLOCK,))


def matches(rule: Rule, line_kind: str, text: str) -> bool:
    """Whether `rule` applies to a line of `line_kind` (headline, description, keyword) and matches its text."""
    return rule.applies_to in (ANY, line_kind) and rule.regex.search(text) is not None


def check(lines: list[tuple[str, str]], rules: list[Rule]) -> list[Finding]:
    """The one check (spec §2.3; plan D4): for each (kind, text) line, at most one finding -- the first `block` rule
    that matches it, else the first `warn` -- in the lines' order. [] with no rules."""
    out: list[Finding] = []
    for line_kind, text in lines:
        hits = [r for r in rules if matches(r, line_kind, text)]
        if not hits:
            continue
        rule = next((r for r in hits if r.level == BLOCK), hits[0])
        out.append(Finding(rule.id, rule.level, rule.why, rule.topic, rule.value, line_kind, text))
    return out


def stamp(conn: psycopg.Connection, refusal_ids: list[str], rules: list[Rule]) -> int:
    """§2.3, the Reviewer (plan D5): each `ads_refusal` row of `refusal_ids` whose line an active rule matches gets
    that rule's id on `rule_id` -- the summary's hits: a rule that was right, and a line that still reached Google
    under it. The count stamped; 0 with no rules or no ids."""
    if not rules or not refusal_ids:
        return 0
    rows = conn.execute(
        "SELECT id, kind, text FROM ads_refusal WHERE id = ANY(%s) AND text IS NOT NULL ORDER BY created_at, id",
        (refusal_ids,),
    ).fetchall()
    n = 0
    for r in rows:
        found = check([(r["kind"], r["text"])], rules)
        if found:
            conn.execute("UPDATE ads_refusal SET rule_id = %s WHERE id = %s", (found[0].rule_id, r["id"]))
            n += 1
    return n


def with_rules(dropped, rules: list[Rule]):
    """`dropped` (build.Dropped) with the `block` rules of `rules` on it: what every Google build leaves out (plan
    D3). A dataclass replace, so this module never imports the build."""
    return dataclasses.replace(dropped, rules=tuple(r for r in rules if r.level == BLOCK))


def tells(rules: list[Rule]) -> list[str]:
    """The rules as words to avoid (the plan writer's prompt, spec §2.3): a word rule's word; a pattern rule's tell."""
    return [SHAPES[r.value].tell if r.kind == PATTERN else r.value for r in rules]


def view(row: dict) -> dict:
    """A `refusal_rule` row for the admin page and the MCP: dates as ISO text, a pattern's tell beside its id."""
    out = {**dict(row), "created_at": iso(row["created_at"]), "decided_at": iso(row["decided_at"])}
    out["tell"] = SHAPES[row["value"]].tell if row["kind"] == PATTERN and row["value"] in SHAPES else None
    return out
