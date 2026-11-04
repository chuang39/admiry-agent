"""The stage skills the harness reads into its one call (spec 2026-10-10 K6 slice 1 §6): one Markdown file per stage,
at most 1,000 tokens, text an executor edits; a change is gated by the turns golden set (agent/eval/turns.json)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

HERE = Path(__file__).parent
STAGES = ("before_plan",)


@cache
def load(stage: str = "before_plan") -> str:
    """The stage's skill text; KeyError for a stage this slice has no skill for."""
    if stage not in STAGES:
        raise KeyError(stage)
    return (HERE / f"{stage}.md").read_text(encoding="utf-8")
