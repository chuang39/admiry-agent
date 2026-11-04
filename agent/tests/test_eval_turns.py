"""The harness's golden turns (spec 2026-10-10 K6 slice 1 §9): the file's shape against K5's cases, each fixture a
real session, the judge, the run with the model faked, the gate, the command line."""

import json

import pytest

from agent import consult, llm
from agent.eval import cases, turns
from agent.harness import context, dispatch
from agent.knowledge_index import tokens
from agent.operations import NEVER_SAY
from agent.tests.fixtures import FakeGenerator, turn_fixture

GOLDEN = {c.id: c for c in cases.load_all()}
ITEMS = turns.load()


def test_the_file_is_canonical_and_every_turn_has_the_keys():
    data = json.loads(turns.PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) >= 40
    assert turns.PATH.read_text(encoding="utf-8") == json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    ids = [t["id"] for t in data]
    assert len(ids) == len(set(ids))
    for t in data:
        assert list(t) == list(turns.KEYS), t["id"]
        assert t["case"] in GOLDEN and t["words"].strip() and isinstance(t["recent"], list), t["id"]
        intents = t["expect"]["intent"] if isinstance(t["expect"]["intent"], list) else [t["expect"]["intent"]]
        assert set(intents) <= set(turns.INTENTS), t["id"]


@pytest.mark.parametrize("tc", ITEMS, ids=[t.id for t in ITEMS])
def test_each_fixture_is_a_session_the_consultation_would_reach_and_its_expectation_is_reachable(tc):
    session = turns.session_of(tc, GOLDEN)
    asked = [q["id"] for q in consult.questions(session)]
    assert tc.answered == asked[: len(tc.answered)], "answered is the questions in their order"
    nxt = consult.next_question(session)
    material = turns.material_of(tc, GOLDEN)
    open_q = material["consultation"].get("open_question")  # the page's question asked again, else nxt's (Ruling 18)
    if tc.question_id is not None:
        assert tc.question_id in consult.may_go_to(session) and open_q["id"] == tc.question_id
    want = tc.expect
    if "option_id" in want:
        allowed = want["option_id"] if isinstance(want["option_id"], list) else [want["option_id"]]
        assert open_q is not None and set(allowed) <= {o["id"] for o in open_q["options"]}
    if "question_id" in want:
        assert want["question_id"] in consult.may_go_to(session)
    if "build" in (want["intent"] if isinstance(want["intent"], list) else [want["intent"]]):
        assert nxt is None or "smalltalk" in want["intent"]
    assert tokens(json.dumps(material, ensure_ascii=False, default=str)) <= context.MATERIAL_TOKENS
    assert material["recent"] == tc.recent


def test_a_turn_typed_under_a_question_asked_again_reads_it_as_the_page_posts_it():
    """Ruling 18: a golden turn carries the question id the page posts with the words, and its material is built as
    turn.run builds production's (context.open_question): after a go_to, the question asked again is the open one."""
    tc = next(t for t in ITEMS if t.id == "goto-then-answer")
    material = turns.material_of(tc, GOLDEN)
    assert tc.question_id == "objective" and material["consultation"]["open_question"]["id"] == "objective"
    assert "objective" not in material["may_go_to"] and material["recent"][-1]["admiry"].startswith("Of course. ")
    assert all(t.question_id is None for t in ITEMS if t.id != "goto-then-answer")


def test_the_golden_set_covers_every_intent_and_the_three_that_must_all_pass():
    covered = {
        i for t in ITEMS for i in (t.expect["intent"] if isinstance(t.expect["intent"], list) else [t.expect["intent"]])
    }
    assert covered == set(turns.INTENTS)
    for must in turns.MUST_ALL:
        assert sum(1 for t in ITEMS if t.expect["intent"] == must) >= 2, must


def test_the_judge_reads_intent_fields_and_confidence():
    tc = next(t for t in ITEMS if t.id == "answer-calls")
    assert turns.judge(tc, turn_fixture(intent="answer", option_id="calls")) == (True, "")
    ok, why = turns.judge(tc, turn_fixture(intent="answer", option_id="bookings"))
    assert not ok and "option_id" in why
    ok, why = turns.judge(tc, turn_fixture(intent="answer", option_id="calls", confidence=0.5))
    assert not ok and "confidence" in why
    budget = next(t for t in ITEMS if t.id == "budget-daily")
    assert turns.judge(budget, turn_fixture(intent="budget", monthly_usd=600.4))[0]
    assert not turns.judge(budget, turn_fixture(intent="budget", monthly_usd=20))[0]
    never = next(t for t in ITEMS if t.id == "ask-never")
    assert not turns.judge(never, turn_fixture(intent="ask", say="Sure, they spend $500."))[0]
    assert turns.judge(never, turn_fixture(intent="ask", say=NEVER_SAY))[0]
    either = next(t for t in ITEMS if isinstance(t.expect["intent"], list))
    assert all(turns.judge(either, turn_fixture(intent=i, say="x"))[0] for i in either.expect["intent"])
    # I3 (b): production asks before a start over read below START_OVER_CONFIDENCE, so the judge wants it above
    start = next(t for t in ITEMS if t.id == "start-paraphrase")
    ok, why = turns.judge(start, turn_fixture(intent="start_over", confidence=0.7))
    assert not ok and why.startswith(f"confidence 0.70 under {dispatch.START_OVER_CONFIDENCE}")
    assert turns.judge(start, turn_fixture(intent="start_over", confidence=dispatch.START_OVER_CONFIDENCE))[0]


def test_the_gate_fails_a_start_over_or_a_not_mine_read_where_neither_was_expected():
    """I3 (a): a false start over discards the answers and a false not-mine unlinks the listing, so either fails the
    gate whatever the rate; a must-all intent in a list-shaped expectation is judged as one."""
    rows = [
        {
            "id": t.id,
            "expect": t.expect,
            "got": {"intent": turns.accepted(t.expect)[0]},
            "ok": True,
            "why": "",
            "say": "",
        }
        for t in ITEMS
    ]
    assert turns.passes(rows) == (True, [])
    for intent, tid in (("start_over", "start-false-continue"), ("not_mine", "answer-calls")):
        misread = [{**r, "got": {"intent": intent}, "ok": False} if r["id"] == tid else r for r in rows]
        assert turns.passes(misread) == (False, [f"{intent}: {tid} read as it though not expected"])
    listed = {
        "id": "listed",
        "expect": {"intent": ["start_over", "smalltalk"]},
        "got": {"intent": "unclear"},
        "ok": False,
    }
    assert turns.passes([*rows, listed]) == (False, ["start_over: listed read wrong (every one must read right)"])
    errored = {"id": "errored", "expect": {"intent": "answer"}, "got": None, "ok": False, "why": "busy", "say": ""}
    assert turns.passes([*rows, errored]) == (True, [])  # an error is a miss, never a false start over


def test_the_report_prints_the_say_the_owner_reads_and_escapes_bars():
    """I3: the table shows the model's say for the intents whose say the owner reads (explain, ask, smalltalk), and a
    `|` in any cell is escaped so the Markdown row holds."""
    rows = [
        {
            "id": "e",
            "expect": {"intent": "explain"},
            "got": {"intent": "explain"},
            "ok": True,
            "why": "",
            "say": "a | b",
        },
        {
            "id": "x",
            "expect": {"intent": "answer"},
            "got": {"intent": "answer"},
            "ok": True,
            "why": "",
            "say": "hidden",
        },
        {
            "id": "w",
            "expect": {"intent": "ask"},
            "got": {"intent": "unclear"},
            "ok": False,
            "why": "say 'c | d'",
            "say": "",
        },
    ]
    text, _ = turns.report(rows, model="m", effort="low", day="2026-10-10")
    assert "| id | expect | got | ok | why | say |" in text and "| e |" in text and "| a \\| b |" in text
    assert "hidden" not in text and "say 'c \\| d'" in text


def test_a_run_with_the_model_faked_grades_each_turn_and_the_gate_reads_the_rows():
    gen = FakeGenerator(
        turns={
            t.words: turn_fixture(
                intent="start_over" if t.expect["intent"] == "start_over" else "unclear", confidence=0.95
            )
            for t in ITEMS
        }
    )
    rows = turns.run(ITEMS, GOLDEN, gen)
    assert len(rows) == len(ITEMS) and len(gen.turn_calls) == len(ITEMS)
    assert all(r["ok"] for r in rows if r["expect"]["intent"] == "start_over")
    text, passed = turns.report(rows, model="m", effort="low", day="2026-10-10")
    assert not passed and "start_over" in text and "| answer-calls |" in text
    perfect = [{**r, "ok": True, "why": ""} for r in rows]
    assert turns.report(perfect, model="m", effort="low", day="2026-10-10")[1] is True
    one_must_fail = [{**r, "ok": r["expect"]["intent"] != "support", "why": ""} for r in rows]
    assert turns.report(one_must_fail, model="m", effort="low", day="2026-10-10")[1] is False


def test_a_malformed_turns_file_and_an_unknown_case_are_refused_as_case_errors(tmp_path):
    bad = tmp_path / "turns.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(cases.CaseError, match="not JSON"):
        turns.load(bad)
    tc = turns.TurnCase("x", "no-such-case", [], None, "hi", [], {"intent": "ask"})
    with pytest.raises(cases.CaseError, match="no golden case"):
        turns.run([tc], GOLDEN, FakeGenerator())


def test_the_cli_runs_the_turns_and_exits_with_the_gate(monkeypatch, capsys, tmp_path):
    from agent.eval import __main__ as cli

    monkeypatch.setenv("META_MODEL_API_KEY", "k")
    gen = FakeGenerator(
        turns={
            t.words: turn_fixture(
                intent=t.expect["intent"] if isinstance(t.expect["intent"], str) else t.expect["intent"][0],
                **_fields(t),
            )
            for t in ITEMS
        }
    )
    monkeypatch.setattr(llm, "generator", lambda settings=None, **kw: gen)
    out = tmp_path / "log.md"
    assert cli.main(["turns", "--out", str(out)]) == 0
    assert "| id |" in capsys.readouterr().out and out.read_text().count("| answer-calls |") == 1
    assert cli.main(["turns", "--only", "answer-calls"]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["turns", "--turns", str(bad)]) == 2
    no_cases = tmp_path / "no-cases"
    no_cases.mkdir()
    capsys.readouterr()
    assert cli.main(["turns", "--cases", str(no_cases)]) == 2 and "no golden case" in capsys.readouterr().err
    monkeypatch.delenv("META_MODEL_API_KEY")
    assert cli.main(["turns"]) == 2


def test_a_table_the_out_path_cannot_take_is_printed_first_and_the_run_exits_2(monkeypatch, capsys, tmp_path):
    """I4 (final review): the live run's table is on screen before --out is written, so a path that cannot be written
    (inside the container the image holds agent/ alone: no docs/) loses nothing -- its reason, and exit 2."""
    from agent.eval import __main__ as cli

    monkeypatch.setenv("META_MODEL_API_KEY", "k")
    monkeypatch.setattr(llm, "generator", lambda settings=None, **kw: FakeGenerator())
    missing = tmp_path / "no-such-dir" / "agent-eval-log.md"
    assert cli.main(["turns", "--only", "answer-calls", "--out", str(missing)]) == 2
    out, err = capsys.readouterr()
    assert "| answer-calls |" in out and err.startswith(f"{missing}: ") and not missing.parent.exists()


def _fields(t):
    want = t.expect
    out = {}
    if "option_id" in want:
        out["option_id"] = want["option_id"] if isinstance(want["option_id"], str) else want["option_id"][0]
    for key in ("question_id", "monthly_usd", "amount_usd", "miles"):
        if key in want:
            out[key] = want[key]
    if "services_n" in want:
        out["services"] = ["Service"] * want["services_n"]
    if "note_has" in want:
        out["note"] = want["note_has"]
    if "say" in want:
        out["say"] = want["say"]
    if "support" in want:
        out["support"] = want["support"]
    return out
