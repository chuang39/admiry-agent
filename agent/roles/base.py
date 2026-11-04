"""What every role shares (spec 2026-10-07-admiry-capability-layer-design §4, §4.1): a contract, and the Critic's
arithmetic -- grades clamped to 1..5 and capped by the checks the judge says a dimension fails (K5), a score on 0..1 and
a verdict, computed here and never by the model (numbers are computed, never generated). Imports nothing from the
service, so the prompt module can read a rubric's words."""

from __future__ import annotations

from dataclasses import dataclass

DELIVER, REVISE, FETCH_MORE = "deliver", "revise", "fetch_more"
LOW, HIGH = 1, 5
FLOOR = 2  # a grade at or under it is a real problem, whatever the mean (`verdict`)
SERIOUS_CAP, CHECK_CAP = FLOOR, 3  # K5: a failed serious check is a real problem; any other: change something first
ANCHORS = (
    "5 = a marketer would launch it as it is",
    "4 = small fixes only",
    "3 = workable, but change something before launch",
    "2 = a real problem",
    "1 = wrong or unusable",
)
CHECKS_RULE = (
    "For each dimension, `failed` lists the ids of its checks the output fails (empty when none): a failed serious "
    f"check means a real problem, {SERIOUS_CAP} or under; any other failed check means change something first, "
    f"{CHECK_CAP} or under."
)


@dataclass(frozen=True)
class Check:
    """One yes/no question a dimension is judged against (K5): its id (what the judge's `failed` names), its words, and
    whether failing it is a real problem (`serious`)."""

    id: str
    text: str
    serious: bool = False


@dataclass(frozen=True)
class Dimension:
    id: str
    label: str
    definition: str
    checks: tuple[Check, ...] = ()  # K5: none for a rubric that grades on the definition and the anchors alone


@dataclass(frozen=True)
class Rubric:
    name: str
    dimensions: tuple[Dimension, ...]
    threshold: float  # the score at or above which the verdict may be deliver (§4.1: constants, tuned on dev)
    anchors: tuple[str, ...] = ANCHORS  # what each grade means for this rubric's output (rubric_text)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.dimensions)

    def dimension(self, dim_id: str) -> Dimension:
        return next(d for d in self.dimensions if d.id == dim_id)


@dataclass(frozen=True)
class Contract:
    """A role's contract (§4): the schema the model fills, the rubric the Critic judges it by (None for the Critic
    itself), the model calls one round may make and the rounds of the confidence loop (§4.1)."""

    role: str
    output: type
    rubric: Rubric | None
    max_calls: int
    max_rounds: int


def _named(failed: object) -> set[str]:
    """The check ids a judge's `failed` names: the strings of a list; anything else names none."""
    return {f for f in failed if isinstance(f, str)} if isinstance(failed, list) else set()


def cap(dimension: Dimension, failed: object) -> int:
    """The highest grade a dimension may have given the checks the judge says it fails (K5): SERIOUS_CAP for a serious
    one, CHECK_CAP for any other, HIGH for none; an id the dimension has no check for counts as no failure."""
    hit = [c for c in dimension.checks if c.id in _named(failed)]
    if any(c.serious for c in hit):
        return SERIOUS_CAP
    return CHECK_CAP if hit else HIGH


def grades_of(raw: list[dict], rubric: Rubric) -> dict[str, int]:
    """Each dimension's grade, clamped to LOW..HIGH and capped by the checks it fails (`cap`): a dimension named twice
    keeps its first grade, a grade that is no whole number is LOW, a dimension the judge never gave is LOW (it did not
    pass it), one the rubric does not have is dropped."""
    out: dict[str, int] = {}
    for g in raw:
        dim = g.get("dimension")
        if dim not in rubric.ids or dim in out:
            continue
        try:
            n = int(g.get("grade"))
        except (TypeError, ValueError):
            n = LOW
        out[dim] = min(HIGH, max(LOW, n), cap(rubric.dimension(dim), g.get("failed")))
    return {d: out.get(d, LOW) for d in rubric.ids}


def failed_of(raw: list[dict], rubric: Rubric) -> dict[str, list[str]]:
    """The checks each dimension fails as the judge named them in its first entry for it, in the checks' order; a
    dimension that fails none is left out (K5: the team reads them on the issues)."""
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for g in raw:
        dim = g.get("dimension")
        if dim not in rubric.ids or dim in seen:
            continue
        seen.add(dim)
        hit = [c.id for c in rubric.dimension(dim).checks if c.id in _named(g.get("failed"))]
        if hit:
            out[dim] = hit
    return out


def score(grades: dict[str, int], rubric: Rubric) -> float:
    """0..1: the mean over the rubric's dimensions of (grade - LOW) / (HIGH - LOW), to two places."""
    return round(sum((grades[d] - LOW) / (HIGH - LOW) for d in rubric.ids) / len(rubric.ids), 2)


def verdict(grades: dict[str, int], rubric: Rubric, missing: list[str]) -> str:
    """deliver when the score reaches the rubric's threshold and no dimension is at FLOOR or under; else fetch_more
    when the judge named material it did not have, else revise (§4.1)."""
    if score(grades, rubric) >= rubric.threshold and min(grades.values()) > FLOOR:
        return DELIVER
    return FETCH_MORE if missing else REVISE


def rubric_text(rubric: Rubric) -> str:
    """The rubric as the judge reads it (llm.SYSTEM_REVIEW, llm.SYSTEM_INSIGHT_REVIEW): one line per dimension -- and,
    for a dimension with checks (K5), a line of them, each with its id and `(serious)` where failing it is a real
    problem -- then the rubric's anchors, and, for a rubric with checks, how a failed check bounds the grade. A rubric
    without checks reads exactly as it did before K5."""
    lines: list[str] = []
    for d in rubric.dimensions:
        lines.append(f"- {d.id} ({d.label}): {d.definition}")
        if d.checks:
            listed = "; ".join(f"{c.id}{' (serious)' if c.serious else ''}: {c.text}" for c in d.checks)
            lines.append(f"  Checks -- {listed}.")
    lines.append("Grades: " + "; ".join(rubric.anchors) + ".")
    if any(d.checks for d in rubric.dimensions):
        lines.append(CHECKS_RULE)
    return "\n".join(lines)
