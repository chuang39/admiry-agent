"""The claim ledger (spec 2026-11-04 §4 steps 1, 4, 5a, 5b): the shapes the studio asks the models for, the business's
facts as numbered sentences, and `write_and_check` -- Spark writes the ad text, each line citing facts; Wonder checks
every line against the facts and the trade's rules (Spark when Wonder fails, labelled); a rejected line is repaired by
Spark and checked again, at most MAX_REPAIRS rounds, then dropped. Lines over Google's length are dropped, never
truncated. What is kept always cites a fact of the business and passed the checker; what is dropped keeps its reason
for the owner."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from . import prompts
from .meta import SPARK, WONDER, MetaClient, MetaError

# kind -> (most characters, fewest lines Google takes, most lines written)
LIMITS = {"headlines": (30, 3, 15), "descriptions": (90, 2, 4), "callouts": (25, 0, 4)}
MAX_REPAIRS = 2
NO_FACT = "It does not cite any fact of the business."
UNCHECKED = "It could not be checked, so it is left out."
NO_VERDICT = "The checker gave no answer for it, so it is left out."
NOT_FIXED = "It could not be rewritten to say only what the business's facts state."


class PhotoRead(BaseModel):
    photo_id: str
    subject: str = Field(description="A short noun phrase for the work shown, e.g. 'new shingle roof'.")
    suitable: bool
    reason: str | None = None
    privacy: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)


class AdText(BaseModel):
    headlines: list[Claim] = Field(default_factory=list)
    descriptions: list[Claim] = Field(default_factory=list)
    callouts: list[Claim] = Field(default_factory=list)


class Verdict(BaseModel):
    text: str
    grounded: bool
    rule: str | None = None
    why: str = ""


class Check(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


class Fix(BaseModel):
    original: str
    text: str
    fact_ids: list[str] = Field(default_factory=list)


class Repairs(BaseModel):
    fixes: list[Fix] = Field(default_factory=list)


class Removed(BaseModel):
    text: str
    kind: str
    why: str


class Ledger(BaseModel):
    kept: AdText
    removed: list[Removed] = Field(default_factory=list)
    checker: str


# ---------------------------------------------------------------- the business's facts


def _plain(value: Any) -> str | None:
    """`value` as a fact's words, or None for nothing, a placeholder ("[First name]") or a non-text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if text and not text.startswith("[") else None


def _items(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def facts_of(knowledge: dict) -> list[dict]:
    """The business's facts, numbered f1, f2, ...: `{"id", "text"}` with the text in the owner's own words, labelled
    ("Licence: NC License #00000"). Reads the stored knowledge's keys (name, trade, category_detail, services, service
    area, phone, hours, offers, prices, reasons customers pick it, notes) and the industry site's (licence, years,
    warranty, insured, free estimate, about, city); a site sample's `facts` block is read as the knowledge itself.
    Missing, empty and placeholder values are no facts."""
    k = dict(knowledge or {})
    if isinstance(k.get("facts"), dict):
        k = {**{key: v for key, v in k.items() if key != "facts"}, **k["facts"]}
    texts: list[str] = []

    def add(label: str, value: Any) -> None:
        words = _plain(value)
        if words:
            texts.append(f"{label}: {words}")

    add("Name", k.get("name"))
    add("Trade", k.get("trade"))
    if k.get("category_detail") != k.get("trade"):
        add("Trade", k.get("category_detail"))
    for service in _items(k.get("services")):
        if isinstance(service, dict):
            name = _plain(service.get("name"))
            if not name:
                continue
            detail = [w for w in (_plain(service.get("summary")), _plain(service.get("price_hint"))) if w]
            texts.append(f"Service: {name}" + (f" ({'; '.join(detail)})" if detail else ""))
        else:
            add("Service", service)
    area = [w for w in (_plain(a) for a in _items(k.get("service_area"))) if w]
    if area:
        texts.append("Service area: " + ", ".join(area))
    place = [w for w in (_plain(k.get("city")), _plain(k.get("state"))) if w]
    if place:
        texts.append("Based in: " + ", ".join(place))
    add("Phone", k.get("phone"))
    hours = [w for w in (_plain(h) for h in _items(k.get("hours"))) if w]
    if hours:
        texts.append("Hours: " + "; ".join(hours))
    add("Years in business", k.get("years"))
    add("Licence", k.get("licence") or k.get("license"))
    if k.get("insured") is True:
        texts.append("Insured: yes")
    else:
        add("Insurance", k.get("insured"))
    add("Warranty", k.get("warranty"))
    add("Free estimate", k.get("free_estimate"))
    for offer in _items(k.get("offers")):
        if isinstance(offer, dict):
            words = _plain(offer.get("text"))
            terms = _plain(offer.get("terms"))
            if words:
                texts.append(f"Offer: {words}" + (f" ({terms})" if terms else ""))
        else:
            add("Offer", offer)
    for hint in _items(k.get("pricing_hints")):
        add("Pricing", hint)
    for reason in _items(k.get("usp")):
        add("Why customers pick us", reason)
    add("Reviews", k.get("reviews_summary"))
    add("About", k.get("about"))
    for note in _items(k.get("notes")):
        add("Owner's note", note.get("text") if isinstance(note, dict) else note)
    return [{"id": f"f{i}", "text": text} for i, text in enumerate(texts, start=1)]


# ---------------------------------------------------------------- write, check, repair


@dataclass
class _Line:
    kind: str
    origin: str  # the text first written: what the owner sees when it is dropped
    claim: Claim
    why: str = ""


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _step(steps: list[dict] | None, name: str, model: str, ok: bool, started: float) -> None:
    if steps is not None:
        steps.append({"step": name, "model": model, "ok": ok, "ms": int((time.monotonic() - started) * 1000)})


def _limits() -> dict:
    return {kind: {"max_characters": chars, "count": high} for kind, (chars, _low, high) in LIMITS.items()}


def _write(meta: MetaClient, facts: list[dict], trade: str, steps: list[dict] | None) -> AdText:
    """Spark's ad text; a failed answer is retried once, told what was wrong (spec §4 step 4). A second failure is the
    MetaError the studio reports."""
    body = {"trade": trade, "facts": facts, "limits": _limits()}
    parts: list[dict] = [{"type": "text", "text": json.dumps(body)}]
    for attempt in range(2):
        started = time.monotonic()
        try:
            text = meta.json(SPARK, prompts.WRITE, parts, AdText)
        except MetaError as e:
            _step(steps, "write_text" if attempt == 0 else "write_text retry", SPARK, False, started)
            if attempt == 1:
                raise
            parts = [*parts, {"type": "text", "text": f"Your last answer was refused: {e}. Answer in the schema."}]
            continue
        _step(steps, "write_text" if attempt == 0 else "write_text retry", SPARK, True, started)
        return text
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def _shape(lines: list[_Line], taken: set[str], removed: list[Removed]) -> list[_Line]:
    """Google's shape, enforced by code: an empty line, one over its kind's length, or one already said is dropped."""
    out = []
    for line in lines:
        text = line.claim.text.strip()
        chars = LIMITS[line.kind][0]
        if not text:
            continue
        if len(text) > chars:
            removed.append(Removed(text=line.origin, kind=line.kind, why=f"Longer than {chars} characters."))
            continue
        if _norm(text) in taken:
            removed.append(Removed(text=line.origin, kind=line.kind, why="Says the same as another line."))
            continue
        taken.add(_norm(text))
        out.append(_Line(line.kind, line.origin, Claim(text=text, fact_ids=line.claim.fact_ids), line.why))
    return out


class _Checker:
    """Wonder, and Spark from the first time Wonder fails (spec §4 step 5a: the fallback is labelled)."""

    def __init__(self, meta: MetaClient, facts: list[dict], rules: str, steps: list[dict] | None) -> None:
        self.meta, self.facts, self.rules, self.steps = meta, facts, rules, steps
        self.model = WONDER

    def verdicts(self, lines: list[_Line]) -> dict[str, Verdict] | None:
        """Verdicts by the claim's normalised text; None when no checker answered."""
        body = {
            "facts": self.facts,
            "claims": [{"text": ln.claim.text, "kind": ln.kind, "fact_ids": ln.claim.fact_ids} for ln in lines],
        }
        parts = [{"type": "text", "text": json.dumps(body)}, {"type": "text", "text": "Rules:\n" + self.rules}]
        for model in (WONDER, SPARK) if self.model == WONDER else (SPARK,):
            started = time.monotonic()
            try:
                check = self.meta.json(model, prompts.CHECK_TEXT, parts, Check)
            except MetaError:
                _step(self.steps, "check_text", model, False, started)
                self.model = SPARK
                continue
            _step(self.steps, "check_text", model, True, started)
            self.model = model
            return {_norm(v.text): v for v in check.verdicts}
        return None


def _why(verdict: Verdict | None) -> str:
    if verdict is None:
        return NO_VERDICT
    why = verdict.why.strip() or "It is not backed by the business's facts."
    return f"{why} (rule: {verdict.rule})" if verdict.rule else why


def _repair(
    meta: MetaClient,
    lines: list[_Line],
    facts: list[dict],
    trade: str,
    removed: list[Removed],
    steps: list[dict] | None,
    round_no: int,
) -> list[_Line]:
    """Spark's rewrites of the rejected `lines`, matched back by their text; a line with no fix, or every line when the
    call fails, is dropped with the reason it was rejected (spec §4 step 5b: "drop the claim")."""
    body = {
        "trade": trade,
        "facts": facts,
        "limits": _limits(),
        "claims": [
            {"text": ln.claim.text, "kind": ln.kind, "fact_ids": ln.claim.fact_ids, "why": ln.why} for ln in lines
        ],
    }
    started = time.monotonic()
    try:
        repairs = meta.json(SPARK, prompts.REPAIR, [{"type": "text", "text": json.dumps(body)}], Repairs)
    except MetaError:
        _step(steps, f"repair_text {round_no}", SPARK, False, started)
        removed.extend(Removed(text=ln.origin, kind=ln.kind, why=ln.why) for ln in lines)
        return []
    _step(steps, f"repair_text {round_no}", SPARK, True, started)
    fixes = {}
    for fix in repairs.fixes:
        fixes.setdefault(_norm(fix.original), fix)
    out = []
    for ln in lines:
        fix = fixes.get(_norm(ln.claim.text))
        if fix is None:
            removed.append(Removed(text=ln.origin, kind=ln.kind, why=ln.why or NOT_FIXED))
            continue
        out.append(_Line(ln.kind, ln.origin, Claim(text=fix.text, fact_ids=fix.fact_ids), ln.why))
    return out


def write_and_check(
    meta: MetaClient, facts: list[dict], rules: str, trade: str, *, steps: list[dict] | None = None
) -> Ledger:
    """The ad text, written and checked (spec §4 steps 4-5b). `steps`, when given, gets one `{"step", "model", "ok",
    "ms"}` per model call. Raises MetaError only when Spark could not write the text at all (twice)."""
    known = {f["id"] for f in facts}
    written = _write(meta, facts, trade, steps)
    removed: list[Removed] = []
    taken: set[str] = set()
    kept: list[_Line] = []
    checker = _Checker(meta, facts, rules, steps)
    checked_by: set[str] = set()
    lines = [_Line(kind, c.text, c) for kind in LIMITS for c in getattr(written, kind)]
    for round_no in range(MAX_REPAIRS + 1):
        lines = _shape(lines, taken, removed)
        rejected, to_check = [], []
        for ln in lines:
            ln.claim = Claim(text=ln.claim.text, fact_ids=[i for i in ln.claim.fact_ids if i in known])
            if ln.claim.fact_ids:
                to_check.append(ln)
            else:
                ln.why = NO_FACT
                rejected.append(ln)
        if to_check:
            verdicts = checker.verdicts(to_check)
            if verdicts is None:  # neither model answered: nothing unchecked is kept
                removed.extend(Removed(text=ln.origin, kind=ln.kind, why=UNCHECKED) for ln in to_check + rejected)
                break
            checked_by.add(checker.model)
            for ln in to_check:
                verdict = verdicts.get(_norm(ln.claim.text))
                if verdict is not None and verdict.grounded and not verdict.rule:
                    kept.append(ln)
                else:
                    ln.why = _why(verdict)
                    rejected.append(ln)
        for ln in rejected:  # a rewrite may say the same as the rejected line again: that is no duplicate
            taken.discard(_norm(ln.claim.text))
        if not rejected:
            break
        if round_no == MAX_REPAIRS:
            removed.extend(Removed(text=ln.origin, kind=ln.kind, why=ln.why) for ln in rejected)
            break
        lines = _repair(meta, rejected, facts, trade, removed, steps, round_no + 1)
        if not lines:
            break
    out: dict[str, list[Claim]] = {kind: [] for kind in LIMITS}
    for ln in kept:
        high = LIMITS[ln.kind][2]
        if len(out[ln.kind]) < high:
            out[ln.kind].append(ln.claim)
        else:
            removed.append(Removed(text=ln.origin, kind=ln.kind, why=f"More than {high} {ln.kind}."))
    label = SPARK if SPARK in checked_by else WONDER if checked_by else "none"
    return Ledger(kept=AdText(**out), removed=removed, checker=label)
