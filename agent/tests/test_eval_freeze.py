"""K5 (P1, P9): the plans the founder grades, built once through the real commands and frozen; the bundle that carries
them from the dev box to the case files, and the flawed copies written again from them."""

import json
import shutil

from agent import benchmark, config, consult, llm
from agent.eval import __main__ as cli
from agent.eval import bundle, cases, flaws, runner
from agent.roles import contracts
from agent.tests.eval_fixtures import frozen_plan, golden_set
from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture


def test_every_case_answers_every_question_its_consultation_asks(conn, monkeypatch):
    from agent import home_api, mcp_app  # noqa: F401 - mcp_app: the commands register as the app loads them

    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: FakeGenerator(error="no model in this walk"))
    benchmark.seed(conn)
    for case in cases.load_all():
        biz, _key, sid = runner.consult_case(conn, case, home_api)
        assert consult.pending(consult.get(conn, biz, sid)) == [], case.id


def test_freeze_builds_each_case_once_and_keeps_its_plan(conn, monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    benchmark.seed(conn)
    dental = [c for c in cases.load_all() if c.id in ("dentists-1", "dentists-2")]  # the fake's ad text is theirs
    frozen, errors = runner.freeze_cases(dental, config.load())
    assert errors == {} and sorted(frozen) == ["dentists-1", "dentists-2"]
    for plan in frozen.values():
        assert list(plan) == list(runner.FROZEN_KEYS)
        assert plan["sections"]["consultation"]["business"]["category"] == "dentists"
        assert plan["model"] == config.load().llm_model and plan["built_on"]
    assert gen.review_calls == []  # freezing judges nothing
    assert json.loads(json.dumps(frozen)) == frozen  # JSON-native: the bundle carries it as it is


def test_freeze_from_writes_the_plans_and_the_six_flawed_copies(tmp_path):
    cases_dir, flawed_dir = tmp_path / "cases", tmp_path / "flawed"
    shutil.copytree(cases.CASES_DIR, cases_dir)
    bases = sorted({f.base for f in flaws.FLAWS.values()})
    frozen = {cid: frozen_plan(monthly_usd=1000 + n) for n, cid in enumerate(bases)}
    sent = bundle.plans_bundle(frozen, {"dentists-3": "LLMError: the assistant is busy"})
    lines = bundle.apply_plans(sent, cases_dir=cases_dir, flawed_dir=flawed_dir, replace=False)
    assert sum("plan frozen" in line for line in lines) == 6
    assert "dentists-3: not built (LLMError: the assistant is busy)" in lines
    assert sum(": written" in line for line in lines) == 6
    items = cases.graded_all(cases_dir, flawed_dir)
    assert len(items) == 12 and {it.flaw for it in items if it.flaw} == set(flaws.FLAWS)
    again = bundle.apply_plans(sent, cases_dir=cases_dir, flawed_dir=flawed_dir, replace=False)
    assert all("unchanged" in line or "not built" in line for line in again)


def test_a_plan_the_founder_graded_is_kept_unless_replace(tmp_path):
    cases_dir, flawed_dir = golden_set(tmp_path)
    path = cases_dir / "dentists-1.json"
    data = cases.raw(path)
    data["founder_grades"] = dict.fromkeys(contracts.PLAN.ids, 4)
    cases.write(path, data)
    sent = bundle.plans_bundle({"dentists-1": frozen_plan(monthly_usd=1500)}, {})
    assert bundle.apply_plans(sent, cases_dir=cases_dir, flawed_dir=flawed_dir, replace=False)[0].startswith(
        "dentists-1: kept"
    )
    assert cases.load(path).founder_grades is not None
    bundle.apply_plans(sent, cases_dir=cases_dir, flawed_dir=flawed_dir, replace=True)
    case = cases.load(path)
    assert case.founder_grades is None and case.plan["sections"]["ads"]["channels"][0]["monthly_usd"] == 1500


def test_freeze_from_needs_no_model_and_no_database(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    cases_dir, flawed_dir = golden_set(tmp_path)
    src = tmp_path / "bundle.json"
    src.write_text(json.dumps(bundle.plans_bundle({"dentists-2": frozen_plan(monthly_usd=900)}, {})))
    assert cli.main(["freeze", "--from", str(src), "--cases", str(cases_dir), "--flawed", str(flawed_dir)]) == 0
    assert "dentists-2: plan frozen" in capsys.readouterr().out
    src.write_text('{"plans": {}}')
    assert cli.main(["freeze", "--from", str(src), "--cases", str(cases_dir), "--flawed", str(flawed_dir)]) == 2


def _graded_flawed_set(tmp_path):
    """Every flaw's base frozen, the six flawed copies written, and the first flaw's copy graded by the founder: the
    set, the folders, the first flaw, the graded copy's path and the bundle file that froze it."""
    cases_dir, flawed_dir = tmp_path / "cases", tmp_path / "flawed"
    shutil.copytree(cases.CASES_DIR, cases_dir)
    bases = sorted({f.base for f in flaws.FLAWS.values()})
    src = tmp_path / "bundle.json"
    src.write_text(json.dumps(bundle.plans_bundle({cid: frozen_plan(monthly_usd=1000) for cid in bases}, {})))
    assert cli.main(["freeze", "--from", str(src), "--cases", str(cases_dir), "--flawed", str(flawed_dir)]) == 0
    flaw = next(iter(flaws.FLAWS.values()))
    path = flawed_dir / f"{flaw.base}.{flaw.id}.json"
    data = cases.raw(path)
    data["founder_grades"] = dict.fromkeys(contracts.PLAN.ids, 3)
    cases.write(path, data)
    return cases_dir, flawed_dir, flaw, path, src


def test_a_graded_flawed_copy_left_behind_by_a_refrozen_base_says_so_and_fails_the_freeze(tmp_path, capsys):
    cases_dir, flawed_dir, flaw, path, _src = _graded_flawed_set(tmp_path)
    capsys.readouterr()
    again = tmp_path / "again.json"
    again.write_text(json.dumps(bundle.plans_bundle({flaw.base: frozen_plan(monthly_usd=2000)}, {})))
    args = ["freeze", "--from", str(again), "--cases", str(cases_dir), "--flawed", str(flawed_dir)]
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    fid = f"{flaw.base}.{flaw.id}"
    stale = f"{fid}: kept STALE (derived from the previous plan of {flaw.base}; --replace regenerates it and drops its"
    assert stale + " grades)" in out
    assert f"{flaw.base}: plan frozen" in out  # everything else it was to write was written
    assert cases.raw(path)["founder_grades"] is not None  # the founder's grades are still there
    assert cli.main([*args, "--replace"]) == 0
    assert f"{fid}: written" in capsys.readouterr().out
    assert cases.raw(path)["founder_grades"] is None


def test_a_graded_flawed_copy_that_still_matches_its_base_is_not_stale(tmp_path, capsys):
    cases_dir, flawed_dir, flaw, path, src = _graded_flawed_set(tmp_path)
    capsys.readouterr()
    assert cli.main(["freeze", "--from", str(src), "--cases", str(cases_dir), "--flawed", str(flawed_dir)]) == 0
    out = capsys.readouterr().out
    assert "STALE" not in out and f"{flaw.base}.{flaw.id}: unchanged" in out
    assert cases.raw(path)["founder_grades"] is not None
    assert (
        bundle.stale(bundle.apply_plans(bundle.read(src), cases_dir=cases_dir, flawed_dir=flawed_dir, replace=False))
        == []
    )


def test_freeze_on_the_box_refuses_without_the_key_or_with_an_unknown_case(monkeypatch, capsys):
    touched = []
    monkeypatch.setattr(runner, "prepare", lambda *a: touched.append(a))
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    assert cli.main(["freeze"]) == 2 and touched == []
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    assert cli.main(["freeze", "--case", "dentists-99"]) == 2 and touched == []
    assert "no case dentists-99" in capsys.readouterr().err
