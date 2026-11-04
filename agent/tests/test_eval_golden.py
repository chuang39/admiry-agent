"""K5 (spec 2026-10-07 capability layer §7): the golden set's files -- the frozen plan the founder grades, his grades
and notes, one canonical form for every file, the digest that binds a grade to its plan."""

import json
from collections import Counter

import pytest

from agent.eval import cases
from agent.roles import contracts
from agent.tests.eval_fixtures import frozen_plan

PLAN = contracts.PLAN
PLAN_OK = {"sections": {"consultation": {}}}


def test_every_case_file_is_in_the_canonical_form():
    for path in sorted(cases.CASES_DIR.glob("*.json")):
        data = cases.raw(path)
        assert list(data) == list(cases.KEYS), path.name
        assert path.read_text(encoding="utf-8") == json.dumps(data, indent=2, ensure_ascii=False) + "\n", path.name


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"plan": {"sections": {}}}, "not a frozen consultation plan"),
        ({"plan": "a plan"}, "not a frozen consultation plan"),
        ({"founder_grades": dict.fromkeys(PLAN.ids, 4)}, "without the plan they grade"),
        ({"plan": PLAN_OK, "founder_grades": {**dict.fromkeys(PLAN.ids, 4), "fit": 6}}, "whole numbers from 1 to 5"),
        ({"plan": PLAN_OK, "founder_grades": {**dict.fromkeys(PLAN.ids, 4), "fit": True}}, "whole numbers from 1 to 5"),
        ({"plan": PLAN_OK, "founder_notes": {"fit": "ok"}}, "founder_notes without founder_grades"),
        (
            {"plan": PLAN_OK, "founder_grades": dict.fromkeys(PLAN.ids, 4), "founder_notes": {"colour": "x"}},
            "per dimension",
        ),
    ],
)
def test_a_frozen_plan_and_the_founders_grades_are_checked(tmp_path, change, words):
    data = cases.raw(cases.CASES_DIR / "dentists-1.json")
    data.update(change)
    path = tmp_path / "dentists-1.json"
    cases.write(path, data)
    with pytest.raises(cases.CaseError) as e:
        cases.load(path)
    assert str(e.value).startswith("dentists-1.json: ") and words in str(e.value)


def test_a_graded_case_loads_with_its_plan_its_grades_and_its_notes(tmp_path):
    data = cases.raw(cases.CASES_DIR / "dentists-1.json")
    data.update(plan=frozen_plan(), founder_grades=dict.fromkeys(PLAN.ids, 4), founder_notes={"copy": "too generic"})
    cases.write(tmp_path / "dentists-1.json", data)
    case = cases.load(tmp_path / "dentists-1.json")
    assert case.plan["sections"]["consultation"]["business"]["service"] == "Invisalign"
    assert case.founder_grades == dict.fromkeys(PLAN.ids, 4) and case.founder_notes == {"copy": "too generic"}
    (item,) = cases.graded_cases([case, cases.load(cases.CASES_DIR / "dentists-2.json")])  # no plan: not graded
    assert (item.id, item.vertical, item.base, item.flaw) == ("dentists-1", "dentists", None, None)


def test_the_digest_names_a_plan_whatever_its_key_order():
    plan = frozen_plan()
    shuffled = json.loads(json.dumps(plan, sort_keys=True))
    assert cases.digest(plan) == cases.digest(shuffled) and len(cases.digest(plan)) == 16
    assert cases.digest(frozen_plan(monthly_usd=1200)) != cases.digest(plan)


def test_twenty_cases_cover_the_three_verticals_and_the_six_home_service_trades():
    every = cases.load_all()
    assert len(every) == 20
    assert Counter(c.vertical for c in every) == {"dentists": 5, "home_services": 8, "attorneys": 7}
    trades = [c.knowledge["category_detail"].lower() for c in every if c.vertical == "home_services"]
    for word in ("plumb", "hvac", "roof", "electric", "landscap", "remodel"):
        assert any(word in t for t in trades), word
    assert all(c.plan is None and c.founder_grades is None and c.founder_notes is None for c in every)
    assert all("healthcare" in c.knowledge["compliance_flags"] for c in every if c.vertical == "dentists")
    assert all("legal" in c.knowledge["compliance_flags"] for c in every if c.vertical == "attorneys")
    assert {c.monthly_budget_usd is None for c in every} == {True, False}  # some owners named no budget
    assert len({c.knowledge["listing"]["place_id"] for c in every}) == 20
    assert all(c.knowledge["phone"].split("-")[1] == "555" for c in every)  # fictional numbers only
    assert len({c.answers["objective"]["choice"] for c in every}) >= 3
