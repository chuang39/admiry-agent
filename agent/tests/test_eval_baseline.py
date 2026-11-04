"""K5 (spec §7, P6): `agent.eval run` fails when a dimension's mean falls by more than 0.3 against the baseline, over the
cases both runs graded; a saved run becomes the next baseline. The gate never passes on nothing: no case in common, an
empty or partial baseline, a baseline file that is missing, broken or of another rubric, an empty golden set."""

import json
import os

import pytest

from agent import config
from agent.eval import __main__ as cli
from agent.eval import baseline
from agent.roles import contracts

IDS = contracts.PLAN.ids


def row(case: str, n: int, **some) -> dict:
    return {"case": case, "grades": {**dict.fromkeys(IDS, n), **some}}


THEN = {"date": "2026-10-12", "commit": "abc", "grades": {"a": dict.fromkeys(IDS, 4), "b": dict.fromkeys(IDS, 4)}}


def test_a_fall_of_more_than_three_tenths_on_one_dimension_fails():
    lines, falls = baseline.compare([row("a", 4, copy=3), row("b", 4, copy=4)], THEN)  # copy 4.00 -> 3.50
    assert falls == ["copy fell 0.50"] and lines[-1] == "FAIL: copy fell 0.50"
    assert "2 cases in common" in lines[0] and "copy 4.00 → 3.50" in lines[0]
    _lines, falls = baseline.compare([row("a", 4, copy=3), row("b", 4, copy=4), row("c", 1)], {**THEN})
    assert falls == ["copy fell 0.50"]  # c is not in the baseline: not compared
    lines, falls = baseline.compare([row("a", 4, keywords=4), row("b", 4)], THEN)
    assert falls == [] and lines[-1] == "no dimension fell by more than 0.3"


def test_a_fall_of_exactly_three_tenths_passes_and_errors_are_left_out():
    then = {"date": "d", "commit": "c", "grades": {c: dict.fromkeys(IDS, 4) for c in "abcdefghij"}}
    rows = [row(c, 4, budget=3 if c in "abc" else 4) for c in "abcdefghij"] + [{"case": "k", "error": "x"}]
    assert baseline.compare(rows, then)[1] == []  # budget 4.00 -> 3.70


def test_the_cases_on_one_side_only_are_named_and_no_case_in_common_fails():
    lines, falls = baseline.compare([row("a", 4), row("c", 1), {"case": "d", "error": "busy"}], THEN)
    assert falls == [] and lines[-1] == "no dimension fell by more than 0.3"  # informational, not failures
    assert "graded now, not in the baseline (not compared): c" in lines
    assert "in the baseline, not graded now: b" in lines  # d errored: it is in neither list
    lines, falls = baseline.compare([row("x", 4), {"case": "a", "error": "busy"}], THEN)  # the only match errored
    assert falls == ["no case in common"] and lines[-1] == "FAIL: no case in common"
    assert "graded now, not in the baseline (not compared): x" in lines
    assert "in the baseline, not graded now: a, b" in lines
    assert baseline.compare([], THEN)[1] == ["no case in common"]
    assert baseline.compare([row("a", 4)], {**THEN, "grades": {}})[1] == ["no case in common"]


def test_a_run_s_output_becomes_the_next_baseline(tmp_path):
    erred = baseline.of_rows([row("a", 5), {"case": "b", "error": "busy"}], config.load(), day="2026-10-13")
    assert erred["grades"] == {"a": dict.fromkeys(IDS, 5)} and erred["date"] == "2026-10-13" and erred["errors"] == 1
    made = baseline.of_rows([row("a", 5), row("b", 3)], config.load(), day="2026-10-13")
    assert made["errors"] == 0 and made["grades"] == {"a": dict.fromkeys(IDS, 5), "b": dict.fromkeys(IDS, 3)}
    saved = tmp_path / "run.txt"
    saved.write_text("#### Eval — table\n\n" + baseline.line(made) + "\n", encoding="utf-8")
    assert baseline.from_output(saved.read_text(encoding="utf-8")) == made
    to = tmp_path / "baseline.json"
    assert cli.main(["baseline", "--from", str(saved), "--to", str(to)]) == 0
    assert baseline.load(to) == made and json.loads(to.read_text())["grades"]["a"]["fit"] == 5
    assert baseline.load(tmp_path / "none.json") is None
    saved.write_text("no baseline line here\n")
    assert cli.main(["baseline", "--from", str(saved), "--to", str(to)]) == 2


@pytest.mark.parametrize(
    "made",
    [
        baseline.of_rows([row("a", 5), {"case": "b", "error": "busy"}], config.load(), day="d"),  # an errored case
        baseline.of_rows([{"case": "a", "error": "busy"}], config.load(), day="d"),  # nothing graded
        {"date": "d", "errors": 0, "grades": {}},  # no grades at all
        {"date": "d", "grades": {"a": dict.fromkeys(IDS, 5)}},  # no error count: not from `run`
        {"date": "d", "errors": 0, "grades": {"a": {"fit": 5}}},  # a case graded on one dimension only
        None,  # a `baseline-json: null` line
        ["a"],
    ],
)
def test_a_baseline_is_an_accepted_run_so_an_errored_or_empty_or_partial_one_is_refused(tmp_path, capsys, made):
    saved, to = tmp_path / "run.txt", tmp_path / "baseline.json"
    saved.write_text(baseline.PREFIX + json.dumps(made) + "\n", encoding="utf-8")
    assert cli.main(["baseline", "--from", str(saved), "--to", str(to)]) == 2
    assert not to.exists() and capsys.readouterr().err


def run_stubs(monkeypatch):
    """`run` with the model, the database and the cases stubbed out; returns the list `run_cases` appends to."""
    monkeypatch.setenv("META_MODEL_API_KEY", "k")
    monkeypatch.setattr(cli, "_start", lambda args, choose=None: ["a case"])
    scored = {"monthly_usd": 100, "score": 0.5, "verdict": "deliver"}
    called = []

    def run_cases(golden, settings):
        called.append(golden)
        return [{**row("a", 4, copy=3), **scored}, {**row("b", 4), **scored}]

    monkeypatch.setattr(cli.runner, "run_cases", run_cases)
    return called


def test_run_exits_1_on_a_fall_prints_the_baseline_line_and_has_none_to_fail_only_by_default(
    tmp_path, monkeypatch, capsys
):
    called = run_stubs(monkeypatch)
    monkeypatch.setattr(baseline, "PATH", tmp_path / "baseline.json")  # the default, not there yet
    assert cli.main(["run"]) == 0  # the Rollout has not committed one: nothing to fall from
    out = capsys.readouterr().out
    assert "no baseline yet" in out and baseline.from_output(out)["grades"]["a"]["copy"] == 3
    assert out.index("| Case |") < out.index("no baseline yet")  # the table, then the comparison
    path = tmp_path / "other.json"
    baseline.write(path, THEN)
    assert cli.main(["run", "--baseline", str(path)]) == 1
    assert "FAIL: copy fell 0.50" in capsys.readouterr().out
    baseline.write(path, {**THEN, "grades": {"a": dict.fromkeys(IDS, 3), "b": dict.fromkeys(IDS, 3)}})
    assert cli.main(["run", "--baseline", str(path)]) == 0
    assert len(called) == 3


def test_run_over_no_case_in_common_with_the_baseline_exits_1(tmp_path, monkeypatch, capsys):
    run_stubs(monkeypatch)
    path = tmp_path / "baseline.json"
    baseline.write(path, {**THEN, "grades": {"x": dict.fromkeys(IDS, 4)}})  # other case ids than the run's
    assert cli.main(["run", "--baseline", str(path)]) == 1
    assert "FAIL: no case in common" in capsys.readouterr().out


def test_run_with_a_baseline_it_cannot_use_exits_2_before_any_model_call(tmp_path, monkeypatch, capsys):
    called = run_stubs(monkeypatch)
    path = tmp_path / "baseline.json"
    assert cli.main(["run", "--baseline", str(path)]) == 2  # named, so it must be there
    assert str(path) in capsys.readouterr().err
    other_rubric = {a: {**dict.fromkeys(IDS, 4), "tone": 4} for a in "ab"}
    for broken in (
        "{not json",
        "null",
        json.dumps({"date": "d", "grades": []}),
        json.dumps({"date": "d", "grades": {}}),
        json.dumps({"date": "d", "grades": {"a": {**dict.fromkeys(IDS, 4), "fit": "high"}}}),
        json.dumps({"date": "d", "grades": other_rubric}),
        json.dumps({"date": "d", "grades": {"a": {"fit": 4}}}),
    ):
        path.write_text(broken, encoding="utf-8")
        assert cli.main(["run", "--baseline", str(path)]) == 2, broken
        assert capsys.readouterr().err.strip(), broken
    assert called == []  # not one case ran


def test_run_over_an_empty_golden_set_exits_2_before_the_database_is_prepared(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("META_MODEL_API_KEY", "k")
    monkeypatch.setenv("AGENT_DATABASE_URL", os.environ["AGENT_DATABASE_URL"])  # `_start` sets it: restored after
    prepared, ran = [], []
    monkeypatch.setattr(cli.runner, "prepare", lambda *a: prepared.append(a))
    monkeypatch.setattr(cli.runner, "run_cases", lambda *a: ran.append(a))
    monkeypatch.setattr(baseline, "PATH", tmp_path / "baseline.json")
    assert cli.main(["run", "--cases", str(tmp_path / "typo")]) == 2
    assert prepared == [] and ran == [] and "typo" in capsys.readouterr().err
