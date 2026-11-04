"""K5 (P7, P9): the founder's grades as the grading page saves them -- per plan and dimension, bound to the frozen plan
they were given on -- and the switch that names who may give them."""

import dataclasses

import pytest

from agent import config
from agent.eval import cases, grades
from agent.roles import contracts
from agent.tests.eval_fixtures import ADMIN, KNOWLEDGE, frozen_plan

DIMS = contracts.PLAN.ids


def _item() -> cases.Graded:
    return cases.Graded("dentists-1", "dentists", KNOWLEDGE, frozen_plan(), None, None)


def test_eval_graders_are_read_lower_cased_and_refused_on_prod(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_GRADERS", " Founder@Example.com , ops@example.com,")
    assert config.load().eval_graders == frozenset({"founder@example.com", "ops@example.com"})
    monkeypatch.setenv("AGENT_EVAL_GRADERS", "founder")
    with pytest.raises(ValueError, match="e-mail addresses separated by commas"):
        config.load()
    monkeypatch.setenv("AGENT_EVAL_GRADERS", ADMIN)
    monkeypatch.setenv("AGENT_ENV", "prod")
    with pytest.raises(ValueError, match="never runs on prod"):
        config.load()
    monkeypatch.delenv("AGENT_EVAL_GRADERS")
    assert config.load().eval_graders == frozenset()


@pytest.mark.parametrize(
    "raw", ["founder", "a@", "@example.com", "a@@example.com", "a@b@example.com", "a@example.com b@example.com", ","]
)
def test_an_entry_that_is_not_one_address_refuses_to_boot_and_a_blank_list_is_no_list(monkeypatch, raw):
    """The list is the grading page's only gate: a pasted pair of addresses or a half-typed one must not boot."""
    monkeypatch.setenv("AGENT_EVAL_GRADERS", raw)
    if raw == ",":
        assert config.load().eval_graders == frozenset()
        return
    with pytest.raises(ValueError, match="e-mail addresses separated by commas"):
        config.load()


def test_a_grade_is_saved_per_dimension_and_a_note_and_a_grade_keep_each_other(conn):
    it = _item()
    assert grades.save(conn, it, grades={"fit": 4, "copy": 2}, notes={"copy": " promises a cure "}, grader=ADMIN) == 2
    assert grades.save(conn, it, grades={}, notes={"fit": "right service"}, grader=ADMIN) == 2
    saved = grades.of(conn, it)
    assert saved["fit"] == {"grade": 4, "note": "right service"}
    assert saved["copy"] == {"grade": 2, "note": "promises a cure"}
    assert grades.save(conn, it, grades={"copy": 3}, notes={}, grader=ADMIN) == 2
    assert grades.of(conn, it)["copy"] == {"grade": 3, "note": "promises a cure"}  # a grade alone keeps its note
    assert grades.save(conn, it, grades={}, notes={"budget": "n"}, grader=ADMIN) == 2  # a note alone grades nothing
    assert grades.count(grades.of(conn, it)) == 2 and len(grades.of(conn, it)) == 3
    long = "x" * (cases.NOTE_MAX + 50)
    grades.save(conn, it, grades={}, notes={"clarity": long}, grader=ADMIN)
    assert len(grades.of(conn, it)["clarity"]["note"]) == cases.NOTE_MAX


def test_a_plan_frozen_again_starts_ungraded(conn):
    it = _item()
    grades.save(conn, it, grades=dict.fromkeys(DIMS, 4), notes={"fit": "old"}, grader=ADMIN)
    again = dataclasses.replace(it, plan=frozen_plan(monthly_usd=1500))
    assert grades.of(conn, again) == {}
    assert grades.save(conn, again, grades={}, notes={"fit": "new"}, grader=ADMIN) == 0  # the old grade stays behind
    assert grades.of(conn, again)["fit"] == {"grade": None, "note": "new"}
    assert grades.save(conn, again, grades={"copy": 5}, notes={}, grader=ADMIN) == 1
    assert grades.of(conn, again)["copy"]["note"] == ""  # a note given on the old plan does not carry over
