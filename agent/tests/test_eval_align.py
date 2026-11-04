"""K5 (P4, P5): the judge measured against the founder -- per dimension, how often its grade is within one of his over
the (plan, dimension) pairs, every run counted -- and whether it tells each flawed copy from its clean base."""

import dataclasses

from agent import config, llm, models
from agent.eval import align, cases, flaws, runner
from agent.roles import contracts
from agent.tests.eval_fixtures import KNOWLEDGE, frozen_plan
from agent.tests.test_review_launch import REVIEW

IDS = contracts.PLAN.ids
ONE = ("a.copy",)  # the one flawed copy of the placeholder plans below (a complete run has the six in flaws.FLAWS)


def grades(n: int, **some) -> dict[str, int]:
    return {**dict.fromkeys(IDS, n), **some}


def item(iid: str, founder: dict | None, *, base: str | None = None, dimension: str | None = None) -> cases.Graded:
    return cases.Graded(
        iid,
        "dentists",
        KNOWLEDGE,
        frozen_plan(),
        founder,
        None,
        base=base,
        flaw="x" if base else None,
        dimension=dimension,
    )


def test_alignment_counts_every_run_within_one_per_dimension_and_overall():
    items = [item("a", grades(4, estimate=5, clarity=5)), item("b", grades(2)), item("c", None)]
    judged = {"a": [grades(5), grades(4)], "b": [grades(4)], "c": [grades(3)]}
    ps = align.pairs(items, judged)
    assert len(ps) == 3 * len(IDS)  # two runs of a, one of b, c ungraded
    assert align.share(ps) == 2 / 3
    stats = align.by_dimension(ps)
    assert stats["fit"] == {"pairs": 3, "within": 2 / 3, "exact": 1 / 3, "judge": 13 / 3, "founder": 10 / 3}


def test_a_flaw_is_caught_when_its_dimension_falls_to_three_or_under_and_below_the_clean_plan():
    items = [
        item("a", None),
        item("a.copy", None, base="a", dimension="copy"),
        item("a.fit", None, base="a", dimension="fit"),
        item("a.clarity", None, base="a", dimension="clarity"),
    ]
    judged = {
        "a": [grades(5, clarity=3), grades(4, clarity=3), grades(5, clarity=3)],
        "a.copy": [grades(5, copy=2), grades(5, copy=2), grades(5, copy=4)],  # median 2: caught
        "a.fit": [grades(4), grades(4), grades(4)],  # 4: not caught
        "a.clarity": [grades(5, clarity=3)] * 3,  # 3, but not below the clean 3: not caught
    }
    rows = {r["id"]: r for r in align.discrimination(items, judged)}
    assert (rows["a.copy"]["clean"], rows["a.copy"]["flawed"], rows["a.copy"]["caught"]) == (5, 2, True)
    assert rows["a.fit"]["caught"] is False and rows["a.clarity"]["caught"] is False


def test_the_report_passes_at_eighty_percent_with_every_flaw_caught_and_names_what_misses():
    items = [item("a", grades(4)), item("a.copy", grades(4, copy=2), base="a", dimension="copy")]
    judged = {"a": [grades(4, keywords=2)], "a.copy": [grades(4, copy=2, keywords=2)]}
    text, passed = align.report(items, judged, model="m", effort="low", repeat=1, day="2026-10-12", expected=ONE)
    assert passed  # 10 of 12 pairs within one (83 %), the one flaw caught
    assert "overall within ±1: 83% (acceptance 80%: pass)" in text
    assert "dimensions that miss: keywords 0%" in text and "discrimination: 1 of 1 flaws caught" in text
    judged["a"] = [grades(5, keywords=1, budget=1)]
    text, passed = align.report(items, judged, model="m", effort="low", repeat=1, day="2026-10-12", expected=ONE)
    assert not passed and "acceptance 80%: FAIL" in text


def test_the_report_without_a_founder_grade_of_three_or_under_leaves_that_share_out():
    items = [item("a", grades(4)), item("a.copy", grades(5), base="a", dimension="copy")]
    judged = {"a": [grades(4)], "a.copy": [grades(5, copy=2)]}
    text, passed = align.report(items, judged, model="m", effort="low", repeat=1, day="2026-10-12", expected=ONE)
    assert passed and "overall within ±1: 92% (acceptance 80%: pass)" in text  # 11 of 12: the copy's 2 against his 5
    assert "3 or under" not in text


def test_without_founder_grades_the_report_rests_on_the_flaws_and_an_error_fails_it():
    items = [item("a", None), item("a.copy", None, base="a", dimension="copy")]
    judged = {"a": [grades(5)], "a.copy": [grades(5, copy=2)]}
    text, passed = align.report(items, judged, model="m", effort=None, repeat=1, day="2026-10-12", expected=ONE)
    assert passed and "no founder grades yet" in text
    judged["a.copy"] = "LLMError: the assistant is busy; try again in a minute"
    text, passed = align.report(items, judged, model="m", effort=None, repeat=1, day="2026-10-12", expected=ONE)
    assert not passed and "1 errors" in text
    assert not align.report(
        [item("a", None)], {"a": [grades(5)]}, model="m", effort=None, repeat=1, day="d", expected=ONE
    )[1]


def the_six() -> tuple[list[cases.Graded], dict]:
    """Every flawed copy of the real set on its real base, each judged clean on the base and caught on the copy."""
    items, judged = [], {}
    for f in flaws.FLAWS.values():
        copy = f"{f.base}.{f.id}"
        items += [item(f.base, None), item(copy, None, base=f.base, dimension=f.dimension)]
        judged |= {f.base: [grades(5)], copy: [grades(5, **{f.dimension: 2})]}
    return items, judged


def graded_first(items: list[cases.Graded], judged: dict, how_many: int) -> list[cases.Graded]:
    """`items` with a founder's grades (equal to the judge's medians) on the first `how_many` plans only."""
    return [
        dataclasses.replace(
            it, founder_grades={d: align.median_of(judged[it.id], d) for d in IDS} if i < how_many else None
        )
        for i, it in enumerate(items)
    ]


def test_the_report_counts_the_plans_the_founder_graded_and_is_incomplete_unless_he_graded_them_all():
    items, judged = the_six()
    day = {"model": "m", "effort": "low", "repeat": 1, "day": "2026-10-12"}
    one = graded_first(items, judged, 1)
    text, passed = align.report(one, judged, **day)
    # the six flaws caught and the one graded plan agreeing, yet 1 of 12 is no "over all the plans" (K5 P5)
    assert not passed and "discrimination: 6 of 6 flaws caught" in text and "overall within ±1: 100%" in text
    assert "founder grades: 1 of 12 plans" in text
    assert "(acceptance 80%: incomplete — 1 of 12 plans graded)" in text and "acceptance 80%: pass" not in text
    text, passed = align.report(graded_first(items, judged, 11), judged, **day)
    assert not passed and "founder grades: 11 of 12 plans" in text and "incomplete — 11 of 12 plans graded" in text
    text, passed = align.report(graded_first(items, judged, 12), judged, **day)
    assert passed and "founder grades: 12 of 12 plans" in text and "(acceptance 80%: pass)" in text
    assert "incomplete" not in text
    text, passed = align.report(graded_first(items, judged, 0), judged, **day)
    assert passed and "founder grades: 0 of 12 plans" in text and "no founder grades yet" in text
    assert "incomplete" not in text and "acceptance 80%" not in text


def test_the_judge_command_exits_nonzero_for_a_run_graded_in_part(tmp_path, monkeypatch, capsys):
    from agent.eval import __main__ as cli

    items, judged = the_six()
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    monkeypatch.setattr(runner, "judge_items", lambda its, s, **kw: judged)
    argv = ["judge", "--cases", str(tmp_path), "--flawed", str(tmp_path), "--repeat", "1"]
    for graded, exit_code in ((1, 1), (11, 1), (12, 0), (0, 0)):
        monkeypatch.setattr(cases, "graded_all", lambda *a, _g=graded, **k: graded_first(items, judged, _g))
        assert cli.main(argv) == exit_code, graded
    assert "incomplete" in capsys.readouterr().out


def test_the_report_passes_only_with_all_six_flaws_and_names_the_ones_missing():
    items, judged = the_six()
    text, passed = align.report(items, judged, model="m", effort="low", repeat=1, day="2026-10-12")
    assert passed and "discrimination: 6 of 6 flaws caught" in text and "missing flaws" not in text
    gone = items.pop().id  # a deleted flawed file: its five siblings are all caught, and that is no pass
    del judged[gone]
    text, passed = align.report(items, judged, model="m", effort="low", repeat=1, day="2026-10-12")
    assert not passed and "discrimination: 5 of 6 flaws caught" in text and f"missing flaws: {gone}" in text


def test_a_run_over_a_single_case_is_not_the_acceptance_run_whatever_it_catches():
    items, judged = the_six()
    text, passed = align.report(items, judged, model="m", effort="low", repeat=1, day="2026-10-12", filtered=True)
    assert not passed and "not the acceptance run" in text and "discrimination: 6 of 6 flaws caught" in text
    only = [it for it in items if "dentists-1" in (it.id, it.base)]
    text, passed = align.report(only, judged, model="m", effort="low", repeat=1, day="2026-10-12", filtered=True)
    assert not passed and "not the acceptance run" in text and "missing flaws: " in text


def test_judge_items_records_a_plan_the_judge_left_ungraded_as_an_error(monkeypatch):
    class Judge:
        def review_plan(self, material):
            usd = material["ads"][0]["monthly_usd"]
            given = {800: [], 700: IDS[:5]}.get(usd, IDS)  # nothing, five of the six, all six
            marks = [models.PlanGrade(dimension=d, grade=1, note="x") for d in given]
            return REVIEW.model_copy(update={"grades": marks, "missing": []})

    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: Judge())
    by_name = {"none": 800, "five": 700, "all": 600}
    items = [
        cases.Graded(n, "dentists", KNOWLEDGE, frozen_plan(monthly_usd=usd), None, None) for n, usd in by_name.items()
    ]
    judged = runner.judge_items(items, config.load(), repeat=2, effort=None)
    assert judged["none"] == "NotGraded: " + ", ".join(IDS) and judged["five"] == f"NotGraded: {IDS[5]}"
    assert judged["all"] == [dict.fromkeys(IDS, 1)] * 2
    # the flawed copy the judge left ungraded is no caught flaw (a missing grade reads as 1), and the run cannot pass
    pair = [
        cases.Graded("a", "dentists", KNOWLEDGE, frozen_plan(monthly_usd=600), None, None),
        cases.Graded(
            "a.copy",
            "dentists",
            KNOWLEDGE,
            frozen_plan(monthly_usd=800),
            None,
            None,
            base="a",
            flaw="x",
            dimension="copy",
        ),
    ]
    judged = runner.judge_items(pair, config.load(), repeat=1, effort=None)
    text, passed = align.report(pair, judged, model="m", effort=None, repeat=1, day="d", expected=ONE)
    assert not passed and "| a.copy | copy | — | — | NO |" in text and "1 errors" in text


def test_judge_items_reads_the_material_the_page_shows_and_survives_a_busy_model(monkeypatch):
    seen = []

    class Judge:
        def review_plan(self, material):
            seen.append(material)
            if material["ads"][0]["monthly_usd"] == 900:
                raise llm.LLMError("the assistant is busy; try again in a minute")
            marks = [models.PlanGrade(dimension=d, grade=4, note="ok") for d in IDS]
            return REVIEW.model_copy(update={"grades": marks, "missing": []})

    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: Judge())
    busy = cases.Graded("b", "dentists", KNOWLEDGE, frozen_plan(monthly_usd=900), None, None)
    judged = runner.judge_items([item("a", None), busy], config.load(), repeat=2, effort=None)
    assert judged["a"] == [grades(4), grades(4)] and judged["b"].startswith("LLMError: the assistant is busy")
    assert all(m["money"] is None and m["blocked"] is None and m["ads"] for m in seen)


def test_the_judge_command_prints_the_report_and_exits_by_it(tmp_path, monkeypatch, capsys):
    from agent.eval import __main__ as cli
    from agent.tests.eval_fixtures import golden_set

    cases_dir, flawed_dir = golden_set(tmp_path)
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    monkeypatch.setattr(runner, "judge_items", lambda items, s, **kw: {it.id: [grades(4)] for it in items})
    argv = ["judge", "--cases", str(cases_dir), "--flawed", str(flawed_dir), "--repeat", "1"]
    assert cli.main(argv) == 1  # no flawed plan: discrimination unproven
    assert "no flawed plans" in capsys.readouterr().out
    monkeypatch.delenv("META_MODEL_API_KEY")
    assert cli.main(argv) == 2


def test_the_judge_command_tells_the_report_when_only_chose_the_plans(tmp_path, monkeypatch, capsys):
    from agent.eval import __main__ as cli
    from agent.tests.eval_fixtures import golden_set

    cases_dir, flawed_dir = golden_set(tmp_path)
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    monkeypatch.setattr(runner, "judge_items", lambda items, s, **kw: {it.id: [grades(4)] for it in items})
    real, seen = align.report, []
    monkeypatch.setattr(
        align,
        "report",
        lambda items, judged, **kw: seen.append((len(items), kw["filtered"])) or real(items, judged, **kw),
    )
    argv = ["judge", "--cases", str(cases_dir), "--flawed", str(flawed_dir), "--repeat", "1"]
    assert cli.main(argv) == 1 and cli.main([*argv, "--only", "dentists-1"]) == 1
    assert seen == [(2, False), (1, True)]
    assert "not the acceptance run" in capsys.readouterr().out
