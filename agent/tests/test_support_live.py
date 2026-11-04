"""The support read against the real model (spec 2026-09-30 §12): twenty labelled sentences, English and Chinese,
through all four reads; each reader must place at least 90 % of them. The printed rate is the baseline for later prompt
changes. Runs only with AGENT_LLM_LIVE=1 and META_MODEL_API_KEY set (80 calls; check the usage page)."""

import json
import os
from pathlib import Path

import pytest

from agent import consult, llm

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_LLM_LIVE") != "1", reason="set AGENT_LLM_LIVE=1 to call the API")
GOLDEN = json.loads((Path(__file__).parent / "golden" / "support_reads.json").read_text())
CARDS = {
    "plan": {"goal": "More Invisalign patients within 5 miles of Bright Smile Dental"},
    "budget": {"monthly_usd": 2000, "per_day": 66, "level": "recommended"},
}


def _objective() -> tuple[dict, list[dict]]:
    q = next(q for q in consult.load_tree("dentists")["questions"] if q["id"] == "objective")
    return {"id": q["id"], "prompt": q["prompt"], "input": q["input"]}, q["options"]


READERS = {
    "read_support": lambda gen, text: gen.read_support(text),
    "read_adjust": lambda gen, text: gen.read_adjust(text, CARDS),
    "read_mail": lambda gen, text: gen.read_mail(text, CARDS),
    "read_answer": lambda gen, text: gen.read_answer(*_objective(), text),
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_the_support_read_places_the_golden_sentences(reader):
    gen = llm.generator()
    misses = []
    for case in GOLDEN:
        got = READERS[reader](gen, case["text"]).support
        if got != case["support"]:
            misses.append((case["text"], case["support"], got))
    rate = 1 - len(misses) / len(GOLDEN)
    print(f"{reader}: {rate:.0%} ({len(GOLDEN) - len(misses)}/{len(GOLDEN)}); misses: {misses}")
    assert rate >= 0.9, misses
