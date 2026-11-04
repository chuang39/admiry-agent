"""The judge measured against the founder (spec 2026-10-07 capability layer §7; K5 P4, P5): per dimension, how often
the judge's grade is within one of the founder's over the (plan, dimension) pairs -- every run of the judge counted, so
the share is what one production call gives -- with 80 % the acceptance; and whether the judge tells each flawed copy
from its clean base (its dimension at CHECK_CAP or under and below the clean plan's). Pure arithmetic over grades: no
model here."""

from __future__ import annotations

import statistics

from ..roles import base, contracts
from .cases import Graded
from .flaws import FLAWS

WITHIN = 1  # §7: "agreement within ±1"
ACCEPT = 0.80  # §7: on at least 80 % of the (case, dimension) pairs


def pairs(items: list[Graded], judged: dict[str, list[dict[str, int]] | str]) -> list[tuple[str, str, int, int]]:
    """(plan id, dimension, the judge's grade, the founder's) for every run of every plan the founder graded."""
    out = []
    for it in items:
        runs = judged.get(it.id)
        if not it.founder_grades or not isinstance(runs, list):
            continue
        for run in runs:
            out += [(it.id, d, run[d], it.founder_grades[d]) for d in contracts.PLAN.ids]
    return out


def share(ps: list[tuple[str, str, int, int]]) -> float | None:
    """The share of pairs within WITHIN of each other; None for none."""
    return sum(1 for _i, _d, j, f in ps if abs(j - f) <= WITHIN) / len(ps) if ps else None


def by_dimension(ps: list[tuple[str, str, int, int]]) -> dict[str, dict]:
    """Per dimension with pairs: how many, the share within one, the share exactly equal, each side's mean."""
    stats: dict[str, dict] = {}
    for d in contracts.PLAN.ids:
        mine = [p for p in ps if p[1] == d]
        if mine:
            stats[d] = {
                "pairs": len(mine),
                "within": share(mine),
                "exact": sum(1 for p in mine if p[2] == p[3]) / len(mine),
                "judge": sum(p[2] for p in mine) / len(mine),
                "founder": sum(p[3] for p in mine) / len(mine),
            }
    return stats


def median_of(runs: list[dict[str, int]], dimension: str) -> int:
    """The judge's median grade over its runs (the lower of the two middle ones for an even count)."""
    return statistics.median_low([r[dimension] for r in runs])


def discrimination(items: list[Graded], judged: dict[str, list[dict[str, int]] | str]) -> list[dict]:
    """One row per flawed copy: its dimension's median grade on the clean base and on the copy, and whether the judge
    caught the flaw -- the copy at CHECK_CAP or under and at least one below the base (K5 P4)."""
    rows = []
    for it in items:
        if it.flaw is None:
            continue
        flawed, clean = judged.get(it.id), judged.get(it.base)
        if not isinstance(flawed, list) or not isinstance(clean, list):
            rows.append({"id": it.id, "dimension": it.dimension, "clean": None, "flawed": None, "caught": False})
            continue
        f, c = median_of(flawed, it.dimension), median_of(clean, it.dimension)
        rows.append(
            {
                "id": it.id,
                "dimension": it.dimension,
                "clean": c,
                "flawed": f,
                "caught": f <= base.CHECK_CAP and f <= c - 1,
            }
        )
    return rows


def consistency(judged: dict[str, list[dict[str, int]] | str]) -> float | None:
    """The share of (plan, dimension) whose grades over the runs lie within one of each other; None with one run."""
    spans = [
        max(r[d] for r in runs) - min(r[d] for r in runs)
        for runs in judged.values()
        if isinstance(runs, list) and len(runs) > 1
        for d in contracts.PLAN.ids
    ]
    return sum(1 for s in spans if s <= WITHIN) / len(spans) if spans else None


def expected_flaws() -> tuple[str, ...]:
    """The ids of the flawed copies a complete run has: one per flaw in flaws.FLAWS, `<base>.<flaw id>`."""
    return tuple(f"{f.base}.{f.id}" for f in FLAWS.values())


def _cell(n: int | None) -> str:
    return "—" if n is None else str(n)


def report(
    items: list[Graded],
    judged: dict[str, list[dict[str, int]] | str],
    *,
    model: str,
    effort: str | None,
    repeat: int,
    day: str,
    expected: tuple[str, ...] | None = None,
    filtered: bool = False,
) -> tuple[str, bool]:
    """The dated Markdown for docs/ops/agent-eval-log.md -- each plan's judge median and founder grade per dimension,
    the alignment per dimension and overall, the dimensions that miss 80 %, the flaws caught and any flawed copy of
    `expected` (default: every one in flaws.FLAWS, as `<base>.<flaw id>`) the run does not have, how steady the runs
    were, the errors -- and whether the judge passes (K5 P5): all of the expected flaws present and caught, no error,
    the founder's grades matched on every plan -- a set he graded in part is `incomplete` and does not pass, a set he
    has not graded leaves the flaws alone to decide. A `filtered` run (--only chose the plans) is not the acceptance
    run and never passes."""
    ids = contracts.PLAN.ids
    lines = [
        f"#### Judge — {day} — {model} (effort {effort or 'default'}) — {len(items)} plans × {repeat} runs",
        "",
        "| Plan | " + " | ".join(ids) + " |",
        "|" + "---|" * (len(ids) + 1),
    ]
    for it in items:
        runs = judged.get(it.id)
        if not isinstance(runs, list):
            lines.append(f"| {it.id} | {runs or 'not judged'} |" + " |" * (len(ids) - 1))
            continue
        cells = [str(median_of(runs, d)) + (f"/{it.founder_grades[d]}" if it.founder_grades else "") for d in ids]
        lines.append(f"| {it.id} | " + " | ".join(cells) + " |")
    lines += ["", "Cells: the judge's median over the runs / the founder's grade. The shares below count every run."]
    ps = pairs(items, judged)
    overall = share(ps)
    graded = sum(1 for it in items if it.founder_grades)
    # the acceptance is "over all the plans" (P5): a set graded in part measures a part and never passes; none graded
    # leaves the discrimination alone to decide
    everyone_graded = graded == len(items)
    lines += ["", f"founder grades: {graded} of {len(items)} plans"]
    if ps:
        stats = by_dimension(ps)
        lines += [
            "",
            "| Dimension | Pairs | Within ±1 | Exact | Judge mean | Founder mean |",
            "|---|---|---|---|---|---|",
        ]
        for d, s in stats.items():
            lines.append(
                f"| {d} | {s['pairs']} | {s['within']:.0%} | {s['exact']:.0%} | {s['judge']:.2f} | {s['founder']:.2f} |"
            )
        low = [p for p in ps if p[3] <= base.CHECK_CAP]
        low_part = (
            f" · the founder's grades of 3 or under matched within ±1: {share(low):.0%} (n={len(low)})" if low else ""
        )
        misses = [f"{d} {s['within']:.0%}" for d, s in stats.items() if s["within"] < ACCEPT]
        verdict = "pass" if overall >= ACCEPT else "FAIL"
        if not everyone_graded:
            verdict = f"incomplete — {graded} of {len(items)} plans graded"
        lines += [
            "",
            f"overall within ±1: {overall:.0%} (acceptance {ACCEPT:.0%}: {verdict})" + low_part,
            "dimensions that miss: " + (", ".join(misses) if misses else "none"),
        ]
    elif graded:
        lines += [
            "",
            f"acceptance {ACCEPT:.0%}: incomplete — no graded plan was judged ({graded} of {len(items)} graded)",
        ]
    else:
        lines += ["", "no founder grades yet: the alignment waits for the grading session"]
    disc = discrimination(items, judged)
    caught = sum(1 for r in disc if r["caught"])
    present = {r["id"] for r in disc}
    missing = [e for e in (expected_flaws() if expected is None else expected) if e not in present]
    if disc:
        lines += ["", "| Flaw | Dimension | Clean | Flawed | Caught |", "|---|---|---|---|---|"]
        for r in disc:
            clean, flawed, word = _cell(r["clean"]), _cell(r["flawed"]), "yes" if r["caught"] else "NO"
            lines.append(f"| {r['id']} | {r['dimension']} | {clean} | {flawed} | {word} |")
        lines.append(f"discrimination: {caught} of {len(disc) + len(missing)} flaws caught")
        if missing:
            lines.append(f"missing flaws: {', '.join(missing)}")
    else:
        lines.append("no flawed plans: `agent.eval freeze --from` writes them")
    steady = consistency(judged)
    if steady is not None:
        lines.append(f"runs within one grade of each other: {steady:.0%}")
    errors = [i for i, v in judged.items() if not isinstance(v, list)]
    lines.append(f"{len(errors)} errors")
    if filtered:
        lines.append("filtered by --only: not the acceptance run (it needs every plan, all six flaws)")
    passed = (
        bool(disc)
        and not missing
        and not filtered
        and caught == len(disc)
        and not errors
        and (graded == 0 or (everyone_graded and overall is not None and overall >= ACCEPT))
    )
    return "\n".join(lines), passed
