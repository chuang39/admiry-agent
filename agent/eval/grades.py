"""The founder's grades of the golden set's frozen plans, as the grading page saves them (K5 P9; table eval_grade): one
row per plan and dimension -- the grade (null until chosen: a note may come first) and the note -- bound to the frozen
plan it was given on by its digest (cases.digest), so a plan frozen again starts ungraded."""

from __future__ import annotations

import psycopg

from ..roles import contracts
from .cases import NOTE_MAX, Graded, digest

_UPSERT = (
    "INSERT INTO eval_grade (case_id, dimension, grade, note, plan_digest, grader) "
    "VALUES (%(case)s, %(dim)s, %(grade)s, %(note)s, %(plan)s, %(grader)s) "
    "ON CONFLICT (case_id, dimension) DO UPDATE SET "
    "grade = COALESCE(EXCLUDED.grade, CASE WHEN eval_grade.plan_digest = EXCLUDED.plan_digest "
    "THEN eval_grade.grade END), "
    "note = CASE WHEN %(keep_note)s AND eval_grade.plan_digest = EXCLUDED.plan_digest THEN eval_grade.note "
    "ELSE EXCLUDED.note END, "
    "plan_digest = EXCLUDED.plan_digest, grader = EXCLUDED.grader, updated_at = now()"
)


def save(conn: psycopg.Connection, item: Graded, *, grades: dict[str, int], notes: dict[str, str], grader: str) -> int:
    """Each dimension the form gave, saved on `item`'s current plan: a grade replaces the one saved; a dimension given a
    note and no grade keeps the grade saved on this same plan, one given a grade and no note keeps its note (nothing of
    an older plan carries over). Returns how many dimensions now have a grade on this plan."""
    plan = digest(item.plan)
    for dim in contracts.PLAN.ids:
        if dim not in grades and dim not in notes:
            continue
        conn.execute(
            _UPSERT,
            {
                "case": item.id,
                "dim": dim,
                "grade": grades.get(dim),
                "note": (notes.get(dim) or "").strip()[:NOTE_MAX],
                "plan": plan,
                "grader": grader,
                "keep_note": dim not in notes,
            },
        )
    return count(of(conn, item))


def of(conn: psycopg.Connection, item: Graded) -> dict[str, dict]:
    """{dimension: {"grade", "note"}} saved on `item`'s current plan; a row given on an older plan of it is left out."""
    rows = conn.execute(
        "SELECT dimension, grade, note FROM eval_grade WHERE case_id = %s AND plan_digest = %s",
        (item.id, digest(item.plan)),
    ).fetchall()
    return {r["dimension"]: {"grade": r["grade"], "note": r["note"]} for r in rows}


def count(saved: dict[str, dict]) -> int:
    """How many of `saved`'s dimensions have a grade."""
    return sum(1 for v in saved.values() if v["grade"] is not None)
