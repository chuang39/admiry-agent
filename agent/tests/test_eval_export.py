"""K5 (R-K5, P9): the founder's grades leave the dev box as a bundle -- the page's download or `agent.eval export --json`
-- and `agent.eval export --from` writes them into the case files: only a plan graded on every dimension, only onto the
plan it was graded on."""

import dataclasses
import json

from agent import eval_web
from agent.eval import __main__ as cli
from agent.eval import bundle, cases, grades
from agent.roles import contracts
from agent.tests.eval_fixtures import ADMIN, frozen_plan, golden_set
from agent.tests.test_web import sign_in

DIMS = contracts.PLAN.ids


def _grade_all(conn, item, n=4, notes=None) -> None:
    grades.save(conn, item, grades=dict.fromkeys(DIMS, n), notes=notes or {}, grader=ADMIN)


def test_export_writes_complete_grades_and_notes_into_the_files(tmp_path, conn):
    cases_dir, flawed_dir = golden_set(tmp_path)
    items = cases.graded_all(cases_dir, flawed_dir)
    _grade_all(conn, items[0], 4, {"copy": "too generic", "fit": "  "})
    sent = json.loads(json.dumps(bundle.grades_bundle(conn, items)))
    assert set(sent["grades"]) == {items[0].id}
    assert bundle.apply_grades(sent, cases_dir=cases_dir, flawed_dir=flawed_dir) == [f"{items[0].id}: graded"]
    case = cases.load(cases_dir / f"{items[0].id}.json")
    assert case.founder_grades == dict.fromkeys(DIMS, 4) and case.founder_notes == {"copy": "too generic"}


def test_a_case_graded_in_part_is_not_exported(tmp_path, conn):
    cases_dir, flawed_dir = golden_set(tmp_path)
    items = cases.graded_all(cases_dir, flawed_dir)
    grades.save(conn, items[0], grades=dict.fromkeys(DIMS[:4], 4), notes={}, grader=ADMIN)
    lines = bundle.apply_grades(bundle.grades_bundle(conn, items), cases_dir=cases_dir, flawed_dir=flawed_dir)
    assert lines == [f"{items[0].id}: not written (4 of 6 dimensions graded)"]
    assert cases.load(cases_dir / f"{items[0].id}.json").founder_grades is None


def test_a_grade_for_a_plan_frozen_again_is_never_exported_onto_the_new_one(tmp_path, conn):
    cases_dir, flawed_dir = golden_set(tmp_path)
    items = cases.graded_all(cases_dir, flawed_dir)
    _grade_all(conn, items[0])
    sent = bundle.grades_bundle(conn, items)
    path = cases_dir / f"{items[0].id}.json"
    data = cases.raw(path)
    data["plan"] = frozen_plan(monthly_usd=1500)  # frozen again here before the export landed
    cases.write(path, data)
    (line,) = bundle.apply_grades(sent, cases_dir=cases_dir, flawed_dir=flawed_dir)
    assert line.startswith(f"{items[0].id}: not written (graded on plan ") and cases.load(path).founder_grades is None
    moved = [dataclasses.replace(items[0], plan=frozen_plan(monthly_usd=1500))]
    assert bundle.grades_bundle(conn, moved)["grades"] == {}  # and on the box, an older plan's rows never leave


def test_export_on_the_box_prints_the_bundle_and_from_writes_it_here(tmp_path, conn, capsys):
    cases_dir, flawed_dir = golden_set(tmp_path)
    items = cases.graded_all(cases_dir, flawed_dir)
    _grade_all(conn, items[1], 5)
    where = ["--cases", str(cases_dir), "--flawed", str(flawed_dir)]
    assert cli.main(["export", "--json", *where]) == 0
    src = tmp_path / "grades.json"
    src.write_text(capsys.readouterr().out)
    assert cli.main(["export", "--from", str(src), *where]) == 0
    assert f"{items[1].id}: graded" in capsys.readouterr().out
    assert cases.load(cases_dir / f"{items[1].id}.json").founder_grades == dict.fromkeys(DIMS, 5)


def test_a_bundle_that_is_not_the_grades_or_holds_a_grade_the_files_refuse_changes_nothing(tmp_path, conn, capsys):
    cases_dir, flawed_dir = golden_set(tmp_path)
    items = cases.graded_all(cases_dir, flawed_dir)
    where = ["--cases", str(cases_dir), "--flawed", str(flawed_dir)]
    plans = tmp_path / "plans.json"
    plans.write_text(json.dumps(bundle.plans_bundle({}, {})))  # a freeze bundle is not a grades bundle
    assert cli.main(["export", "--from", str(plans), *where]) == 2
    assert "not a grades bundle" in capsys.readouterr().err
    _grade_all(conn, items[0])
    sent = bundle.grades_bundle(conn, items)
    sent["grades"][items[0].id]["dimensions"][DIMS[0]]["grade"] = 9  # edited by hand: not a grade from 1 to 5
    path = cases_dir / f"{items[0].id}.json"
    before = path.read_text(encoding="utf-8")
    (line,) = bundle.apply_grades(sent, cases_dir=cases_dir, flawed_dir=flawed_dir)
    assert line.startswith(f"{items[0].id}: not written (") and "whole numbers from 1 to 5" in line
    assert path.read_text(encoding="utf-8") == before


def test_judge_reads_the_founders_grades_from_the_database_on_the_box(tmp_path, conn):
    cases_dir, flawed_dir = golden_set(tmp_path)
    items = cases.graded_all(cases_dir, flawed_dir)
    _grade_all(conn, items[0], 2, {"copy": "x"})
    grades.save(conn, items[1], grades={"fit": 4}, notes={}, grader=ADMIN)  # in part: the file's word stands
    got = bundle.with_db_grades(conn, items)
    assert got[0].founder_grades == dict.fromkeys(DIMS, 2) and got[0].founder_notes == {"copy": "x"}
    assert got[1].founder_grades is None


def test_the_pages_download_is_the_box_commands_bundle(client, conn, google, tmp_path, monkeypatch):
    cases_dir, flawed_dir = golden_set(tmp_path)
    monkeypatch.setattr(eval_web, "CASES_DIR", cases_dir)
    monkeypatch.setattr(eval_web, "FLAWED_DIR", flawed_dir)
    items = cases.graded_all(cases_dir, flawed_dir)
    _grade_all(conn, items[0])
    monkeypatch.setenv("AGENT_EVAL_GRADERS", ADMIN)
    assert client.get("/ops/eval/export.json").status_code == 404  # anonymous: no sign-in page for a file
    google.email = ADMIN
    sign_in(client, "/ops/eval")
    r = client.get("/ops/eval/export.json")
    assert r.status_code == 200 and 'filename="admiry-eval-grades.json"' in r.headers["content-disposition"]
    assert r.json() == bundle.grades_bundle(conn, items)
    assert 'href="/ops/eval/export.json"' in client.get("/ops/eval").text


def test_the_download_is_a_404_wherever_the_grading_page_is(client, conn, google, tmp_path, monkeypatch):
    cases_dir, flawed_dir = golden_set(tmp_path)
    monkeypatch.setattr(eval_web, "CASES_DIR", cases_dir)
    monkeypatch.setattr(eval_web, "FLAWED_DIR", flawed_dir)
    _grade_all(conn, cases.graded_all(cases_dir, flawed_dir)[0])
    google.email = ADMIN
    sign_in(client, "/connect")  # signed in as the founder, with no list yet: no page
    assert client.get("/ops/eval/export.json").status_code == 404
    monkeypatch.setenv("AGENT_EVAL_GRADERS", "someone.else@example.com")  # signed in and not listed
    assert client.get("/ops/eval/export.json").status_code == 404
    monkeypatch.setenv("AGENT_EVAL_GRADERS", ADMIN)
    assert client.get("/ops/eval/export.json").status_code == 200
    monkeypatch.delenv("AGENT_EVAL_GRADERS")  # prod never has a list (it refuses to boot with one): a 404, as the page
    monkeypatch.setenv("AGENT_ENV", "prod")
    assert client.get("/ops/eval/export.json").status_code == 404
    audited = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool LIKE 'web.eval%%'").fetchone()["n"]
    assert audited == 0  # a download writes no audit row
