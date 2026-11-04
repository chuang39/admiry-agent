"""The golden set (spec 2026-10-07 capability layer §7): one JSON file per case in agent/eval/cases/ -- the business as
Admiry knows it (`knowledge`, with a confirmed Google listing and its point, so no lookup runs), the budget the owner
named (or null: the benchmark's), the owner's opening words (where a "within N miles" answers the radius), the answers
to the consultation's questions by id; then (K5) the plan the Strategist built for it, frozen once (`plan`, null until
`agent.eval freeze`), and the founder's grades on the plan rubric's dimensions with his notes (`founder_grades`,
`founder_notes`, null until the grading session's export). agent/eval/flawed/ holds copies of frozen plans, each broken
in one dimension (agent/eval/flaws.py, K5 Task 3). `load` checks a case against the options a session of that business
would offer; every file is written in one canonical form (`write`), so a freeze or an export changes only its lines."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .. import consult, models
from ..roles import contracts
from . import flaws

CASES_DIR = Path(__file__).parent / "cases"
FLAWED_DIR = Path(__file__).parent / "flawed"
KEYS = (
    "id",
    "vertical",
    "opening",
    "monthly_budget_usd",
    "knowledge",
    "answers",
    "plan",
    "founder_grades",
    "founder_notes",
)
FLAWED_KEYS = ("id", "base", "flaw", "dimension", "plan", "founder_grades", "founder_notes")
NOTE_MAX = 1000  # characters of one note (the grading page's textarea holds as many)


class CaseError(ValueError):
    """A case that cannot run as written; the message starts with the file's name (or the case's id) and says why."""


@dataclass(frozen=True)
class Case:
    id: str
    vertical: str
    opening: str
    knowledge: dict
    monthly_budget_usd: float | None
    answers: dict[str, dict]
    founder_grades: dict[str, int] | None
    plan: dict | None = None
    founder_notes: dict[str, str] | None = None


@dataclass(frozen=True)
class Graded:
    """One plan the founder grades and the judge's alignment is measured on (K5): a case's frozen plan, or a flawed copy
    of one (`base` the case, `flaw` its id in flaws.FLAWS, `dimension` the one it breaks)."""

    id: str
    vertical: str
    knowledge: dict
    plan: dict
    founder_grades: dict[str, int] | None
    founder_notes: dict[str, str] | None
    base: str | None = None
    flaw: str | None = None
    dimension: str | None = None


def digest(plan: object) -> str:
    """The frozen plan's name (16 hex characters of the SHA-256 of its JSON with sorted keys): a grade is saved and
    exported against it, so a plan frozen again starts ungraded (P1)."""
    text = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def raw(path: Path) -> dict:
    """The file as JSON, unchecked."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CaseError(f"{path.name}: not JSON ({e.msg})") from None
    if not isinstance(data, dict):
        raise CaseError(f"{path.name}: not a JSON object")
    return data


def write(path: Path, data: dict) -> None:
    """A case or flawed file in the one canonical form: its keys in KEYS (FLAWED_KEYS for a flawed copy) order, any
    other key after them, two-space indents, UTF-8, a closing newline."""
    order = FLAWED_KEYS if "flaw" in data else KEYS
    ordered = {k: data.get(k) for k in order} | {k: v for k, v in data.items() if k not in order}
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load(path: Path) -> Case:
    data = raw(path)
    case = Case(
        id=data.get("id") or "",
        vertical=data.get("vertical") or "",
        opening=data.get("opening") or "",
        knowledge=data.get("knowledge") or {},
        monthly_budget_usd=data.get("monthly_budget_usd"),
        answers=data.get("answers") or {},
        founder_grades=data.get("founder_grades"),
        plan=data.get("plan"),
        founder_notes=data.get("founder_notes"),
    )
    _check(case, path.name)
    return case


def load_all(directory: Path = CASES_DIR) -> list[Case]:
    return [load(p) for p in sorted(directory.glob("*.json"))]


def graded_cases(every: list[Case]) -> list[Graded]:
    """The cases that have a frozen plan, as plans to grade and judge."""
    return [
        Graded(c.id, c.vertical, c.knowledge, c.plan, c.founder_grades, c.founder_notes)
        for c in every
        if c.plan is not None
    ]


def load_flawed(path: Path, bases: dict[str, Case]) -> Graded:
    """A flawed copy (agent/eval/flawed/<base>.<flaw>.json): one of flaws.FLAWS on its own base, which must be a case
    of the set (its knowledge is the base's), with its frozen plan and, once graded, the founder's grades and notes."""
    data, name = raw(path), path.name
    fid, base, flaw_id = data.get("id") or "", data.get("base") or "", data.get("flaw") or ""
    if fid != name.removesuffix(".json"):
        raise CaseError(f"{name}: the id {fid!r} is not the file's name")
    flaw = flaws.FLAWS.get(flaw_id)
    if flaw is None or flaw.base != base or fid != f"{base}.{flaw_id}" or data.get("dimension") != flaw.dimension:
        raise CaseError(f"{name}: not one of the flaws in agent/eval/flaws.py (its base, its flaw and its dimension)")
    case = bases.get(base)
    if case is None:
        raise CaseError(f"{name}: its base case {base!r} is not in the golden set")
    check_plan(data.get("plan"), name)
    check_grades(data.get("founder_grades"), data.get("founder_notes"), data["plan"], name)
    return Graded(
        fid,
        case.vertical,
        case.knowledge,
        data["plan"],
        data.get("founder_grades"),
        data.get("founder_notes"),
        base=base,
        flaw=flaw_id,
        dimension=flaw.dimension,
    )


def graded_all(cases_dir: Path = CASES_DIR, flawed_dir: Path = FLAWED_DIR) -> list[Graded]:
    """Every plan to grade and judge: the cases with a frozen plan, then the flawed copies in name order."""
    every = load_all(cases_dir)
    bases = {c.id: c for c in every}
    found = sorted(flawed_dir.glob("*.json")) if flawed_dir.exists() else []
    return graded_cases(every) + [load_flawed(p, bases) for p in found]


def _check(case: Case, name: str) -> None:
    if case.id != name.removesuffix(".json"):
        raise CaseError(f"{name}: the id {case.id!r} is not the file's name")
    if consult.tree_for(case.vertical) != case.vertical:
        raise CaseError(f"{name}: {case.vertical!r} has no consultation of its own")
    if case.knowledge.get("category") != case.vertical:
        raise CaseError(f"{name}: the knowledge's category is not the case's vertical")
    try:
        models.Knowledge.model_validate({k: v for k, v in case.knowledge.items() if k != "listing"})
    except ValueError as e:
        raise CaseError(f"{name}: the knowledge is not Business Knowledge ({e})") from None
    listing = case.knowledge.get("listing") or {}
    if not (listing.get("confirmed") is True and listing.get("lat") is not None and listing.get("lng") is not None):
        raise CaseError(f"{name}: the knowledge needs a confirmed listing with its point (no lookup runs)")
    tree = consult.load_tree(case.vertical)
    asked = {q["id"] for q in consult.tree_questions(tree)}
    offered = consult.build_options(tree, case.knowledge)  # what a session of this business offers
    for qid, value in case.answers.items():
        if qid not in asked:
            raise CaseError(f"{name}: {qid!r} is not a question of the {case.vertical} consultation")
        ids = [o["id"] for o in offered.get(qid) or []]
        if "choice" in value and value["choice"] not in ids:
            raise CaseError(f"{name}: {qid} offers {ids}, not {value['choice']!r}")
    if case.plan is not None:
        check_plan(case.plan, name)
    check_grades(case.founder_grades, case.founder_notes, case.plan, name)


def check_plan(plan: object, name: str) -> None:
    """A frozen plan is a consultation plan's stored shape: a `sections` object holding `consultation`."""
    if not (isinstance(plan, dict) and isinstance(plan.get("sections"), dict) and "consultation" in plan["sections"]):
        raise CaseError(f"{name}: the plan is not a frozen consultation plan (no sections.consultation)")


def check_grades(grades: object, notes: object, plan: object, name: str) -> None:
    """The founder's grades: every dimension, a whole number from 1 to 5 each, only beside the plan they grade; his
    notes: words per dimension, only beside the grades."""
    ids = contracts.PLAN.ids
    if grades is not None:
        if not isinstance(grades, dict) or set(grades) != set(ids):
            raise CaseError(f"{name}: founder_grades must grade every dimension: {', '.join(ids)}")
        if any(isinstance(g, bool) or not isinstance(g, int) or not 1 <= g <= 5 for g in grades.values()):
            raise CaseError(f"{name}: founder_grades are whole numbers from 1 to 5")
        if plan is None:
            raise CaseError(f"{name}: founder_grades without the plan they grade")
    if notes is not None:
        if grades is None:
            raise CaseError(f"{name}: founder_notes without founder_grades")
        if (
            not isinstance(notes, dict)
            or not set(notes) <= set(ids)
            or any(not isinstance(n, str) or len(n) > NOTE_MAX for n in notes.values())
        ):
            raise CaseError(f"{name}: founder_notes are words (at most {NOTE_MAX} characters) per dimension")
