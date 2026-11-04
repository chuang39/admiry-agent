"""Carrying the golden set's plans and grades between the dev box (where the model key and the grading page live) and
the laptop (where the case files are committed), K5 P9: a bundle is one JSON object -- `{"version": 1, "plans": {...},
"errors": {...}}` from `agent.eval freeze` -- that `freeze --from` writes into the case files, writing the flawed copies
again from the plans it froze, or `{"version": 1, "grades": {...}}` from the grading page's download or `agent.eval
export --json`, which `export --from` writes into the case and flawed files."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ..roles import contracts
from . import cases, flaws, grades
from .cases import CaseError

VERSION = 1
STALE = "kept STALE"  # what a graded flawed copy's line says when its base was frozen again (`write_flawed`)


def read(path: Path) -> dict:
    """A bundle file, or CaseError naming it when it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CaseError(f"{path.name}: not a bundle ({e})") from None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise CaseError(f"{path.name}: not an eval bundle of version {VERSION}")
    return data


def plans_bundle(frozen: dict[str, dict], errors: dict[str, str]) -> dict:
    return {"version": VERSION, "plans": frozen, "errors": errors}


def apply_plans(bundle: dict, *, cases_dir: Path, flawed_dir: Path, replace: bool) -> list[str]:
    """Each frozen plan of `bundle` into its case file, then the flawed copies written again (`write_flawed`). A case
    the founder graded keeps its plan unless `replace`, which also drops its grades and notes. One line per case and
    copy, for the operator."""
    lines: list[str] = []
    for cid, plan in sorted((bundle.get("plans") or {}).items()):
        path = cases_dir / f"{cid}.json"
        if not path.exists():
            raise CaseError(f"{cid}: no case file {path.name} in {cases_dir}")
        data = cases.raw(path)
        if cases.digest(data.get("plan")) == cases.digest(plan):
            lines.append(f"{cid}: unchanged")
            continue
        if data.get("founder_grades") is not None and not replace:
            lines.append(f"{cid}: kept (the founder graded its plan; --replace drops the grades)")
            continue
        data.update(plan=plan, founder_grades=None, founder_notes=None)
        cases.write(path, data)
        cases.load(path)
        lines.append(f"{cid}: plan frozen ({cases.digest(plan)})")
    lines += [f"{cid}: not built ({err})" for cid, err in sorted((bundle.get("errors") or {}).items())]
    return lines + write_flawed(cases_dir=cases_dir, flawed_dir=flawed_dir, replace=replace)


def stale(lines: list[str]) -> list[str]:
    """The lines of `apply_plans` that name a graded flawed copy left behind by a re-frozen base: the judge's
    discrimination (copy against base) means nothing for it until `--replace` regenerates it, so `freeze --from` fails."""
    return [line for line in lines if f": {STALE} (" in line]


def write_flawed(*, cases_dir: Path, flawed_dir: Path, replace: bool) -> list[str]:
    """Every flaw of flaws.FLAWS written from its base's frozen plan into `flawed_dir`; a copy that already is what the
    base's plan makes is unchanged; one the founder graded and the base's plan no longer makes is kept, said STALE
    (`stale`), unless `replace` writes it again and drops its grades; a base with no frozen plan yet writes none."""
    bases = {c.id: c for c in cases.load_all(cases_dir)}
    flawed_dir.mkdir(exist_ok=True)
    lines: list[str] = []
    for flaw in flaws.FLAWS.values():
        fid = f"{flaw.base}.{flaw.id}"
        base = bases.get(flaw.base)
        if base is None or base.plan is None:
            lines.append(f"{fid}: not written (its base has no frozen plan)")
            continue
        plan = flaws.flawed(flaw, base.plan, base.knowledge)
        path = flawed_dir / f"{fid}.json"
        old = cases.raw(path) if path.exists() else {}
        if cases.digest(old.get("plan")) == cases.digest(plan):
            lines.append(f"{fid}: unchanged")
            continue
        if old.get("founder_grades") is not None and not replace:
            # the copy differs from what its base's current plan makes (equal was "unchanged" above): the founder's
            # grades are on a plan the base no longer has, and the judge is compared with them against that base
            lines.append(
                f"{fid}: {STALE} (derived from the previous plan of {flaw.base}; "
                "--replace regenerates it and drops its grades)"
            )
            continue
        copy = {"id": fid, "base": flaw.base, "flaw": flaw.id, "dimension": flaw.dimension, "plan": plan}
        cases.write(path, {**copy, "founder_grades": None, "founder_notes": None})
        lines.append(f"{fid}: written ({cases.digest(plan)})")
    return lines


def grades_bundle(conn, items: list[cases.Graded]) -> dict:
    """The grades saved on the grading page, per plan: the digest of the plan they were given on and each dimension's
    grade and note -- only rows on each plan's current frozen plan (an older plan's never leave the box)."""
    out = {}
    for it in items:
        saved = grades.of(conn, it)
        if saved:
            out[it.id] = {"plan_digest": cases.digest(it.plan), "dimensions": saved}
    return {"version": VERSION, "grades": out}


def apply_grades(sent: dict, *, cases_dir: Path, flawed_dir: Path) -> list[str]:
    """The bundle's grades into the case and flawed files: a plan graded on every dimension, written onto the plan it
    was graded on (its file's digest equal to the bundle's), as `founder_grades` and `founder_notes` (the notes with
    words, or null); any other plan is named and left as it is. Every file is loaded again after."""
    ids = contracts.PLAN.ids
    given_all = sent.get("grades")
    if not isinstance(given_all, dict):
        raise CaseError("not a grades bundle (`agent.eval export --json` or the grading page's download makes one)")
    lines: list[str] = []
    for gid, entry in sorted(given_all.items()):
        given = entry if isinstance(entry, dict) else {}  # an entry that is not one names no plan: not written below
        path = cases_dir / f"{gid}.json"
        if not path.exists():
            path = flawed_dir / f"{gid}.json"
        if not path.exists():
            lines.append(f"{gid}: not written (no case or flawed file of that id)")
            continue
        data = cases.raw(path)
        held = cases.digest(data.get("plan"))
        if held != given.get("plan_digest"):
            lines.append(f"{gid}: not written (graded on plan {given.get('plan_digest')}, the file holds {held})")
            continue
        dims = {d: v for d, v in (given.get("dimensions") or {}).items() if d in ids and isinstance(v, dict)}
        full = {d: v["grade"] for d, v in dims.items() if v.get("grade") is not None}
        if set(full) != set(ids):
            lines.append(f"{gid}: not written ({len(full)} of {len(ids)} dimensions graded)")
            continue
        notes = {d: v["note"].strip() for d, v in dims.items() if isinstance(v.get("note"), str) and v["note"].strip()}
        marks = {d: full[d] for d in ids}
        try:
            cases.check_grades(marks, notes or None, data.get("plan"), "the bundle")  # before a file is touched
        except CaseError as e:
            lines.append(f"{gid}: not written ({e})")
            continue
        data.update(founder_grades=marks, founder_notes=notes or None)
        cases.write(path, data)
        lines.append(f"{gid}: graded")
    cases.graded_all(cases_dir, flawed_dir)  # every file still loads: a broken file raises CaseError here
    return lines


def with_db_grades(conn, items: list[cases.Graded]) -> list[cases.Graded]:
    """`items` with the founder's grades and notes read from the grading page's table where a plan is graded on every
    dimension (on the box, before the export reaches the files); a plan graded in part keeps what its file says."""
    ids = contracts.PLAN.ids
    out = []
    for it in items:
        saved = grades.of(conn, it)
        full = {d: v["grade"] for d, v in saved.items() if v["grade"] is not None}
        if set(full) == set(ids):
            notes = {d: v["note"] for d, v in saved.items() if v["note"]}
            out.append(dataclasses.replace(it, founder_grades={d: full[d] for d in ids}, founder_notes=notes or None))
        else:
            out.append(it)
    return out
