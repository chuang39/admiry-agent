"""K5 (P3): six deliberately flawed copies of the golden set's frozen plans, each broken in one rubric dimension -- the
proof that the judge tells a flawed plan from its clean base -- and their files in agent/eval/flawed/."""

import json

import pytest

from agent import plans
from agent.eval import cases, flaws
from agent.roles import contracts
from agent.tests.eval_fixtures import KNOWLEDGE, frozen_plan, golden_set

COPY_CLAIMS = {
    "id": "dentists-2.copy-claims",
    "base": "dentists-2",
    "flaw": "copy-claims",
    "dimension": "copy",
    "founder_grades": None,
    "founder_notes": None,
}


def _broken(fid: str) -> dict:
    return flaws.flawed(flaws.FLAWS[fid], frozen_plan(), KNOWLEDGE)


def test_six_flaws_one_per_dimension_on_bases_across_the_verticals():
    every = list(flaws.FLAWS.values())
    assert sorted(f.dimension for f in every) == sorted(contracts.PLAN.ids)
    assert {f.base for f in every} <= {c.id for c in cases.load_all()} and len({f.base for f in every}) == 6
    assert {f.base.split("-")[0] for f in every} == {"dentists", "home_services", "attorneys"}
    assert all(f.what and f.id == key for key, f in flaws.FLAWS.items())


def test_a_flaw_breaks_a_copy_never_the_base():
    plan = frozen_plan()
    before = json.dumps(plan, sort_keys=True)
    for flaw in flaws.FLAWS.values():
        assert flaws.flawed(flaw, plan, KNOWLEDGE) != plan, flaw.id
        assert json.dumps(plan, sort_keys=True) == before, flaw.id


def test_what_each_flaw_changes():
    assert _broken("fit-area")["sections"]["consultation"]["geo"]["radius_miles"] == 120
    over = _broken("budget-over-limit")["sections"]
    assert plans.total_usd(over) == 3000.0
    assert over["estimate"]["columns"]["planning"] == {
        "spend": 3000,
        "leads": [30, 60],
        "bookings": [15, 30],
        "customers": [9, 18],
        "revenue": [22500, 45000],
    }
    assert (over["estimate"]["low"], over["estimate"]["high"]) == (30, 60)
    assert over["estimate"]["columns"]["recommended"]["spend"] == 1000  # the recommended level stays as it was
    claims = plans.primary(_broken("copy-claims")["sections"])
    assert claims["headlines"] == flaws.BAD_HEADLINES and claims["descriptions"] == flaws.BAD_DESCRIPTIONS
    waste = plans.primary(_broken("keywords-waste")["sections"])
    assert waste["themes"] == flaws.WASTE_THEMES and waste["negatives"] == []
    promise = _broken("estimate-promise")["sections"]["estimate"]
    assert promise["columns"]["planning"]["leads"] == [20, 20] and promise["assumptions"] == []
    assert promise["footnote"].startswith("Guaranteed")
    jargon = _broken("clarity-jargon")
    assert jargon["sections"]["rationale"]["why_primary"] == flaws.JARGON_WHY == jargon["rationale"]


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"id": "dentists-2.copy-other"}, "is not the file's name"),
        ({"flaw": "copy-other"}, "not one of the flaws"),
        ({"base": "dentists-1"}, "not one of the flaws"),
        ({"dimension": "fit"}, "not one of the flaws"),
        ({"plan": None}, "not a frozen consultation plan"),
        ({"founder_grades": {"copy": 2}}, "must grade every dimension"),
    ],
)
def test_a_flawed_file_names_its_base_its_flaw_and_its_dimension(tmp_path, change, words):
    cases_dir, flawed_dir = golden_set(tmp_path)
    cases.write(flawed_dir / "dentists-2.copy-claims.json", {**COPY_CLAIMS, "plan": frozen_plan(), **change})
    with pytest.raises(cases.CaseError) as e:
        cases.graded_all(cases_dir, flawed_dir)
    assert str(e.value).startswith("dentists-2.copy-claims.json: ") and words in str(e.value)


def test_a_flawed_copy_needs_its_base_in_the_set_and_reads_its_knowledge(tmp_path):
    cases_dir, flawed_dir = golden_set(tmp_path, ids=("dentists-2",), budgets=(1000.0,))
    orphan = flawed_dir / "dentists-1.budget-over-limit.json"
    base = {"base": "dentists-1", "flaw": "budget-over-limit", "dimension": "budget"}
    cases.write(orphan, {**COPY_CLAIMS, **base, "id": "dentists-1.budget-over-limit", "plan": frozen_plan()})
    with pytest.raises(cases.CaseError, match="its base case 'dentists-1' is not in the golden set"):
        cases.graded_all(cases_dir, flawed_dir)
    orphan.unlink()
    cases.write(flawed_dir / "dentists-2.copy-claims.json", {**COPY_CLAIMS, "plan": frozen_plan(monthly_usd=900)})
    items = cases.graded_all(cases_dir, flawed_dir)
    assert [(it.id, it.base, it.flaw, it.dimension) for it in items] == [
        ("dentists-2", None, None, None),
        ("dentists-2.copy-claims", "dentists-2", "copy-claims", "copy"),
    ]
    assert items[1].knowledge == items[0].knowledge and items[1].vertical == "dentists"
