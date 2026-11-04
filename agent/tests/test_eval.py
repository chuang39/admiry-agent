"""The eval scaffold (spec 2026-10-07-admiry-capability-layer-design §7, K1): the golden set's cases, the runner that
builds each case's plan through the real commands and judges it, and the guard on the database it drops."""

import json
import re

import psycopg
import pytest

from agent import benchmark, config, llm
from agent.eval import cases, runner
from agent.roles import contracts
from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture, plan_review_fixture


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"id": "other"}, "is not the file's name"),
        ({"answers": {"objective": {"choice": "fame"}}}, "objective offers"),
        ({"answers": {"colour": {"choice": "red"}}}, "'colour' is not a question"),
        ({"knowledge_listing": None}, "confirmed listing"),
        ({"founder_grades": {"fit": 4}}, "must grade every dimension"),
    ],
)
def test_a_case_that_cannot_run_as_written_names_its_file_and_the_problem(tmp_path, change, words):
    good = json.loads((cases.CASES_DIR / "dentists-1.json").read_text())
    if "knowledge_listing" in change:
        good["knowledge"]["listing"] = None
    else:
        good.update(change)
    path = tmp_path / "dentists-1.json"
    path.write_text(json.dumps(good))
    with pytest.raises(cases.CaseError) as e:
        cases.load(path)
    assert str(e.value).startswith("dentists-1.json: ") and words in str(e.value)


def test_the_runner_builds_each_case_through_the_commands_and_judges_it(conn, monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reviews=[plan_review_fixture()])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    benchmark.seed(conn)
    dental = [c for c in cases.load_all() if c.id in ("dentists-1", "dentists-2")]  # the fake's ad text is theirs
    rows = runner.run_cases(dental, config.load())
    assert [r["case"] for r in rows] == ["dentists-1", "dentists-2"] and not [r for r in rows if "error" in r], rows
    for r in rows:
        assert r["grades"] == dict.fromkeys(contracts.PLAN.ids, 4) and (r["score"], r["verdict"]) == (0.75, "deliver")
    assert len(gen.review_calls) == 2
    assert all(c["material"]["blocked"] is None and c["material"]["money"] is None for c in gen.review_calls)
    assert all(c["material"]["ads"] for c in gen.review_calls)  # K5: the judge reads every phrase and line
    report = runner.report(rows, config.load())
    assert "| Case | Plan | fit | budget | copy | keywords | estimate | clarity | Score | Verdict |" in report
    row = r"^\| dentists-1 \| \$[\d,]+/mo \| 4 \| 4 \| 4 \| 4 \| 4 \| 4 \| 0\.75 \| deliver \|$"
    assert re.search(row, report, re.MULTILINE)
    assert "mean score 0.75" in report


def test_a_plan_the_judge_left_a_dimension_ungraded_is_an_error_row_not_a_low_grade(conn, monkeypatch):
    five = plan_review_fixture()
    five = five.model_copy(update={"grades": [g for g in five.grades if g.dimension != "clarity"]})
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reviews=[five])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    benchmark.seed(conn)
    dental = [c for c in cases.load_all() if c.id == "dentists-1"]
    rows = runner.run_cases(dental, config.load())
    assert rows == [{"case": "dentists-1", "error": "NotGraded: clarity"}]  # a filled 1 would enter a baseline
    assert "| dentists-1 | error: NotGraded: clarity |" in runner.report(rows, config.load())


def test_the_runner_refuses_a_database_that_is_not_an_eval_one():
    for url, live in (
        ("postgresql:///bidx_agent", None),
        ("postgresql:///agent", None),
        ("postgresql:///agent_eval", "postgresql:///agent_eval"),
    ):
        with pytest.raises(cases.CaseError, match="refusing"):
            runner.prepare(url, live)


def test_db_name_keeps_the_server_and_the_credentials():
    live = {"AGENT_DATABASE_URL": "postgresql://bidx:pw@postgres:5432/agent"}
    info = psycopg.conninfo.conninfo_to_dict(runner.database_url(db=None, db_name="agent_eval", env=live))
    assert (info["dbname"], info["host"], info["user"], info["password"]) == ("agent_eval", "postgres", "bidx", "pw")
    assert runner.database_url(db=None, db_name=None, env={}) == "postgresql:///bidx_agent_eval"
    assert runner.database_url(db="postgresql:///x_eval", db_name=None, env=live) == "postgresql:///x_eval"


def test_the_cli_refuses_without_the_models_key_before_touching_a_database(monkeypatch, capsys):
    from agent.eval import __main__ as cli

    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    touched = []
    monkeypatch.setattr(runner, "prepare", lambda *a: touched.append(a))
    assert cli.main(["run"]) == 2 and touched == []
    assert "META_MODEL_API_KEY" in capsys.readouterr().err
