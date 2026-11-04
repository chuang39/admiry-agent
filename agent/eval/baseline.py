"""The eval runner's failure rule (spec 2026-10-07 capability layer §7; K5 P6): a change to the Strategist's prompt, a
plan schema description, a knowledge file or a role threshold fails when one dimension's mean over the golden cases
falls by more than FALL against the baseline -- the last accepted run's grades, agent/eval/baseline.json -- over the
cases both runs graded. A change to the judge itself is gated by `agent.eval judge` instead and writes a new baseline.
`run` prints its own grades as one `baseline-json:` line, which `agent.eval baseline --from` turns into the file.

The gate never passes on nothing: a run with no case in common with the baseline fails, a baseline is only ever an
accepted run (every case graded, none errored), and one that is empty, broken or of another rubric is refused (`check`)
before any case runs."""

from __future__ import annotations

import json
from pathlib import Path

from .. import config
from ..roles import contracts
from .cases import CaseError

FALL = 0.3
PATH = Path(__file__).parent / "baseline.json"
PREFIX = "baseline-json: "
NOTHING = "no case in common"


def of_rows(rows: list[dict], settings: config.Settings, *, day: str) -> dict:
    """A run as a baseline: its day, the image's commit, the model and effort, how many cases errored, each graded
    case's grades."""
    return {
        "date": day,
        "commit": settings.build_sha,
        "model": settings.llm_model,
        "effort": settings.llm_effort,
        "errors": sum("error" in r for r in rows),
        "grades": {r["case"]: r["grades"] for r in rows if "error" not in r},
    }


def compare(rows: list[dict], then: dict) -> tuple[list[str], list[str]]:
    """(the operator's lines, the reasons the run fails) over the cases graded both in this run and in the baseline
    `then`: a dimension whose mean fell by more than FALL, or no case in common (nothing was compared). The cases on
    one side only are named, and fail nothing."""
    now = {r["case"]: r["grades"] for r in rows if "error" not in r}
    old = then.get("grades") or {}
    common = sorted(set(now) & set(old))
    notes = []
    if set(now) - set(old):
        notes.append("graded now, not in the baseline (not compared): " + ", ".join(sorted(set(now) - set(old))))
    if set(old) - set(now):
        notes.append("in the baseline, not graded now: " + ", ".join(sorted(set(old) - set(now))))
    falls = []
    if common:
        parts = []
        for d in contracts.PLAN.ids:
            before = sum(old[c][d] for c in common) / len(common)
            after = sum(now[c][d] for c in common) / len(common)
            parts.append(f"{d} {before:.2f} → {after:.2f}")
            if round(before - after, 2) > FALL:
                falls.append(f"{d} fell {before - after:.2f}")
        head = f"baseline {then.get('date')} ({then.get('commit')}, {len(common)} cases in common): " + " · ".join(
            parts
        )
    else:
        falls.append(NOTHING)
        head = f"baseline {then.get('date')} ({then.get('commit')}): this run and the baseline share no graded case"
    return [head, *notes, "FAIL: " + ", ".join(falls) if falls else f"no dimension fell by more than {FALL}"], falls


def line(b: dict) -> str:
    return PREFIX + json.dumps(b, sort_keys=True, ensure_ascii=False)


def from_output(text: str) -> dict:
    """The last `baseline-json:` line of a saved `run` output, as it parses (`accepted` says whether it may be one)."""
    found = [ln[len(PREFIX) :] for ln in text.splitlines() if ln.startswith(PREFIX)]
    if not found:
        raise CaseError(f"no {PREFIX.strip()} line: save the whole output of `agent.eval run`")
    return json.loads(found[-1])


def check(b: object) -> dict:
    """`b` as a baseline `compare` can use, or CaseError: an object whose `grades` are a non-empty case -> {dimension:
    number} over exactly the plan rubric's dimensions (one of another rubric must be made again)."""
    grades = b.get("grades") if isinstance(b, dict) else None
    if not isinstance(grades, dict) or not grades:
        raise CaseError("not a baseline: it needs `grades`, an object with at least one graded case")
    for case, given in sorted(grades.items()):
        if not isinstance(given, dict) or set(given) != set(contracts.PLAN.ids):
            raise CaseError(f"the baseline's case {case!r} is not graded on exactly {', '.join(contracts.PLAN.ids)}")
        for d, n in given.items():
            if isinstance(n, bool) or not isinstance(n, int | float):
                raise CaseError(f"the baseline's case {case!r} has {n!r} for {d}, not a number")
    return b


def accepted(b: object) -> dict:
    """`b` as the next baseline, or CaseError: a baseline is an accepted run -- usable (`check`) and with every case
    graded, none errored (a run that recorded no error count did not come from `run`)."""
    check(b)
    if b.get("errors") != 0:
        raise CaseError(
            f"that run had {b.get('errors', 'an unknown number of')} errored cases: a baseline is an accepted run, "
            "so run again until every case is graded"
        )
    return b


def load(path: Path = PATH) -> dict | None:
    """The baseline at `path` (checked), None when there is no such file; OSError or ValueError (CaseError, broken
    JSON) when it cannot be used."""
    return check(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None


def write(path: Path, b: dict) -> None:
    path.write_text(json.dumps(b, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
