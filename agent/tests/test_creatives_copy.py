"""The claim ledger (spec 2026-11-04 §4 steps 4-5b): every line kept cites a fact of the business and passed the
checker; what failed twice after repair, ran long or cited nothing is dropped with its reason. A fake MetaClient
answers by schema: no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.creatives import copy, meta

ROOFER = Path(__file__).resolve().parents[1] / "data" / "sites" / "samples" / "roofer.json"
FACTS = [
    {"id": "f1", "text": "Name: Oakline Roofing"},
    {"id": "f2", "text": "Service: Roof repair"},
    {"id": "f3", "text": "Free estimate: Free roof inspections"},
    {"id": "f4", "text": "Service area: Charlotte"},
]
LICENCE_RULE = "home_services/compliance: a licence claim needs the licence number"


def claim(text: str, *ids: str) -> copy.Claim:
    return copy.Claim(text=text, fact_ids=list(ids))


def written(**extra) -> copy.AdText:
    base = {
        "headlines": [
            claim("Oakline Roofing", "f1"),
            claim("Roof Repair in Charlotte", "f2", "f4"),
            claim("Free Roof Inspections", "f3"),
        ],
        "descriptions": [
            claim("Leaks and missing shingles fixed by Oakline Roofing.", "f1", "f2"),
            claim("Ask for a free roof inspection in Charlotte.", "f3", "f4"),
        ],
        "callouts": [claim("Free Inspections", "f3")],
    }
    for kind, claims in extra.items():
        base[kind] = base[kind] + claims
    return copy.AdText(**base)


class FakeMeta:
    """Answers `json` by schema: the writer's AdText, the checker's Check (a licence line is never grounded: there is
    no licence fact), the repairer's Repairs (it hands the same line back, so it fails again)."""

    def __init__(self, text: copy.AdText, *, wonder_down: bool = False, bad_shape_first: bool = False):
        self.text = text
        self.wonder_down = wonder_down
        self.bad_shape_first = bad_shape_first
        self.calls: list[tuple[str, str]] = []

    def json(self, model, system, parts, schema):
        self.calls.append((model, schema.__name__))
        if schema is copy.AdText:
            if self.bad_shape_first and len(self.calls) == 1:
                raise meta.MetaError("muse-spark-1.3 answered in the wrong shape: headlines missing")
            return self.text
        body = json.loads(parts[0]["text"])
        if schema is copy.Check:
            if model == meta.WONDER and self.wonder_down:
                raise meta.MetaError("muse-wonder-1.0 is unavailable (503)")
            verdicts = []
            for c in body["claims"]:
                if "licensed" in c["text"].lower():
                    verdicts.append(
                        copy.Verdict(text=c["text"], grounded=False, rule=LICENCE_RULE, why="No licence fact.")
                    )
                else:
                    verdicts.append(copy.Verdict(text=c["text"], grounded=True, rule=None, why="Cites its fact."))
            return copy.Check(verdicts=verdicts)
        if schema is copy.Repairs:
            return copy.Repairs(
                fixes=[copy.Fix(original=c["text"], text=c["text"], fact_ids=c["fact_ids"]) for c in body["claims"]]
            )
        raise AssertionError(schema)

    def count(self, schema_name: str) -> int:
        return sum(1 for _, name in self.calls if name == schema_name)


def kept_texts(ledger: copy.Ledger, kind: str) -> list[str]:
    return [c.text for c in getattr(ledger.kept, kind)]


def test_ungrounded_claim_is_removed_after_two_repairs():
    fake = FakeMeta(written(headlines=[claim("Licensed & insured", "f1")]))
    ledger = copy.write_and_check(fake, FACTS, "rules", "roofing")
    assert "Licensed & insured" not in kept_texts(ledger, "headlines")
    gone = [r for r in ledger.removed if r.text == "Licensed & insured"]
    assert len(gone) == 1 and gone[0].kind == "headlines"
    assert LICENCE_RULE in gone[0].why
    assert fake.count("Repairs") == copy.MAX_REPAIRS == 2
    assert fake.count("Check") == 3  # the first check and one after each repair


def test_grounded_claim_kept():
    fake = FakeMeta(written())
    ledger = copy.write_and_check(fake, FACTS, "rules", "roofing")
    assert kept_texts(ledger, "headlines") == ["Oakline Roofing", "Roof Repair in Charlotte", "Free Roof Inspections"]
    assert ledger.kept.headlines[1].fact_ids == ["f2", "f4"]
    assert len(ledger.kept.descriptions) == 2 and kept_texts(ledger, "callouts") == ["Free Inspections"]
    assert ledger.removed == [] and ledger.checker == meta.WONDER
    assert fake.count("Repairs") == 0


def test_overlong_headline_dropped_not_truncated():
    long = "Charlotte's Friendliest Roof Repair Crew"  # 40 characters
    fake = FakeMeta(written(headlines=[claim(long, "f2")]))
    ledger = copy.write_and_check(fake, FACTS, "rules", "roofing")
    assert all(len(t) <= 30 for t in kept_texts(ledger, "headlines"))
    assert not any(long.startswith(t) for t in kept_texts(ledger, "headlines"))
    gone = [r for r in ledger.removed if r.text == long]
    assert len(gone) == 1 and "30" in gone[0].why


def test_wonder_failure_falls_back_to_spark_checker():
    fake = FakeMeta(written(), wonder_down=True)
    ledger = copy.write_and_check(fake, FACTS, "rules", "roofing")
    assert ledger.checker == "muse-spark-1.3"
    assert len(ledger.kept.headlines) == 3
    assert (meta.SPARK, "Check") in fake.calls


def test_claim_citing_no_fact_is_not_kept():
    fake = FakeMeta(written(callouts=[claim("Best Prices in Town")]))
    ledger = copy.write_and_check(fake, FACTS, "rules", "roofing")
    assert "Best Prices in Town" not in kept_texts(ledger, "callouts")
    assert any(r.text == "Best Prices in Town" for r in ledger.removed)


def test_writer_wrong_shape_is_retried_once_with_the_error():
    fake = FakeMeta(written(), bad_shape_first=True)
    steps: list[dict] = []
    ledger = copy.write_and_check(fake, FACTS, "rules", "roofing", steps=steps)
    assert fake.count("AdText") == 2 and len(ledger.kept.headlines) == 3
    assert [s["ok"] for s in steps if s["step"].startswith("write_text")] == [False, True]
    assert all(set(s) == {"step", "model", "ok", "ms"} for s in steps)


def test_steps_name_the_models():
    fake = FakeMeta(written(), wonder_down=True)
    steps: list[dict] = []
    copy.write_and_check(fake, FACTS, "rules", "roofing", steps=steps)
    assert ("check_text", meta.WONDER, False) in [(s["step"], s["model"], s["ok"]) for s in steps]
    assert ("check_text", meta.SPARK, True) in [(s["step"], s["model"], s["ok"]) for s in steps]


def test_facts_of_roofer_sample():
    sample = json.loads(ROOFER.read_text())
    facts = copy.facts_of(sample)
    assert [f["id"] for f in facts] == [f"f{i}" for i in range(1, len(facts) + 1)]
    texts = " | ".join(f["text"] for f in facts)
    for expected in ("Oakline Roofing", "roofing", "Roof replacement", "Charlotte", "(704) 555-0142", "15"):
        assert expected in texts
    assert "NC License #00000" in texts and "10-year workmanship warranty" in texts
    assert "[A real review" not in texts  # the sample's placeholders are no facts


def test_facts_of_stored_knowledge_shape():
    knowledge = {
        "name": "Summit Roofing",
        "category_detail": "roofer",
        "services": [{"name": "Roofing", "intent_phrases": ["roofing near me"], "price_hint": "from $300"}],
        "service_area": ["Palo Alto"],
        "phone": "650-555-0100",
        "offers": [{"id": "o1", "text": "10% off gutters", "terms": None}],
        "usp": ["Same-week appointments"],
        "notes": [],
        "website": None,
    }
    texts = [f["text"] for f in copy.facts_of(knowledge)]
    assert any("Summit Roofing" in t for t in texts)
    assert any("from $300" in t for t in texts)
    assert any("10% off gutters" in t for t in texts)
    assert any("Same-week appointments" in t for t in texts)
    assert not any("None" in t for t in texts)


@pytest.mark.parametrize("kind", list(copy.LIMITS))
def test_limits_shape(kind):
    chars, low, high = copy.LIMITS[kind]
    assert chars > 0 and 0 <= low <= high
