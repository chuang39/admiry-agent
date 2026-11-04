"""The harness's golden turns (spec 2026-10-10 K6 slice 1 §9): the owner's words at a point of a consultation, with the
intent and the fields they must read as. A fixture is one of K5's golden cases (its knowledge, its answers) answered up
to a question (D12), so the eval needs no database and the material is the harness's own (harness.context.material),
its open question the one the page posts with the words (question_id: one asked again after a go_to, Ruling 18).
`python -m agent.eval turns` runs them against the live model on dev; the gate is the three intents that must all read
right (MUST_ALL), no start over or not-mine read where a turn did not expect one (NEVER_FALSE) and PASS_RATE of every
turn. Offline, agent/tests/test_eval_turns.py checks the file against the cases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import consult, llm, models
from ..harness import context, skills
from ..harness.dispatch import CONFIDENCE, START_OVER_CONFIDENCE
from . import cases

PATH = Path(__file__).parent / "turns.json"
KEYS = ("id", "case", "answered", "question_id", "words", "recent", "expect")
INTENTS = models.TURN_INTENTS
MUST_ALL = ("start_over", "support", "not_mine")  # every turn of these intents reads right, or the gate fails
# read where the turn did not expect it, a start over discards the answers and a not-mine unlinks the listing: one such
# turn fails the gate, whatever the rate (I3)
NEVER_FALSE = ("start_over", "not_mine")
SAYS = ("explain", "ask", "smalltalk")  # the intents whose say the owner reads: the report prints it
PASS_RATE = 0.9
SKIPPED_LOCATION = {
    "skipped": True,
    "raw": "later",
    "source": "other",
}  # no case answers location (its listing centres the ads)
NUMBER_FIELDS = ("monthly_usd", "amount_usd", "miles")


@dataclass(frozen=True)
class TurnCase:
    id: str
    case: str
    answered: list[str]
    question_id: str | None  # the question card the page shows, posted with the words: one asked again (go_to), or None
    words: str
    recent: list[dict]
    expect: dict  # intent (a string, or a list of the intents accepted), then option_id, question_id, monthly_usd,
    # amount_usd, miles (within ±1), services_n (at least), note_has, say (exact), support -- only those named are judged


def load(path: Path = PATH) -> list[TurnCase]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise cases.CaseError(f"{path.name}: not JSON ({e.msg})") from None
    if not isinstance(data, list):
        raise cases.CaseError(f"{path.name}: not a JSON list")
    out = []
    for t in data:
        if not isinstance(t, dict) or list(t) != list(KEYS):
            raise cases.CaseError(f"{path.name}: a turn without exactly the keys {KEYS}")
        out.append(
            TurnCase(
                t["id"],
                t["case"],
                list(t["answered"]),
                t["question_id"],
                t["words"],
                list(t["recent"]),
                dict(t["expect"]),
            )
        )
    return out


def accepted(expect: dict) -> list[str]:
    """The intents an expectation accepts: its one intent, or its list."""
    return expect["intent"] if isinstance(expect["intent"], list) else [expect["intent"]]


def session_of(tc: TurnCase, golden: dict[str, cases.Case]) -> dict:
    """The case's consultation answered up to `answered`, as consult_session holds one (no database)."""
    case = golden.get(tc.case)
    if case is None:
        raise cases.CaseError(f"{tc.id}: no golden case {tc.case!r}")
    tree = consult.load_tree(case.vertical)
    answers = {}
    for qid in tc.answered:
        if qid == "location":
            answers[qid] = SKIPPED_LOCATION
        elif qid in case.answers:
            answers[qid] = {
                **case.answers[qid],
                "raw": case.answers[qid].get("choice") or case.answers[qid].get("other"),
                "source": "choice" if "choice" in case.answers[qid] else "other",
            }
        else:
            raise cases.CaseError(f"{tc.id}: the case {tc.case} does not answer {qid!r}")
    session = {
        "id": f"cs_{tc.id}",
        "business_id": f"biz_{tc.case}",
        "tree": case.vertical,
        "answers": answers,
        "options": consult.build_options(tree, case.knowledge),
        "known_facts": {},
        "status": "open",
    }
    if consult.next_question(session) is None:
        session["status"] = "ready"
    return session


def material_of(tc: TurnCase, golden: dict[str, cases.Case]) -> dict:
    session = session_of(tc, golden)  # raises cases.CaseError for an unknown case, before the bare subscript below
    case = golden[tc.case]
    goal = {
        "goal_type": "customers",
        "monthly_limit_usd": case.monthly_budget_usd,
        "limit_source": "owner" if case.monthly_budget_usd is not None else "system",
    }
    # the open question as turn.run reads production's: the page's question asked again, else the first unanswered one
    return context.material(
        case.knowledge, goal, session, tc.recent, open_question=context.open_question(session, tc.question_id)
    )


def judge(tc: TurnCase, turn: models.Turn) -> tuple[bool, str]:
    """Whether the Turn reads as the case expects: the intent (one of those accepted), then only the fields named."""
    want = tc.expect
    intents = accepted(want)
    if turn.intent not in intents:
        return False, f"intent {turn.intent}, wanted {'/'.join(intents)}"
    if turn.confidence < CONFIDENCE and turn.intent != "unclear":
        return False, f"confidence {turn.confidence:.2f} under {CONFIDENCE}: the harness would ask back"
    if turn.intent == "start_over" and turn.confidence < START_OVER_CONFIDENCE:  # production confirms below it (I3)
        return False, f"confidence {turn.confidence:.2f} under {START_OVER_CONFIDENCE}: the harness would ask first"
    for key in ("option_id", "question_id"):
        if key in want:
            allowed = want[key] if isinstance(want[key], list) else [want[key]]
            if getattr(turn, key) not in allowed:
                return False, f"{key} {getattr(turn, key)!r}, wanted {allowed}"
    for key in NUMBER_FIELDS:
        if key in want:
            got = getattr(turn, key)
            if want[key] is None and got is not None:
                return False, f"{key} {got}, wanted none"
            if want[key] is not None and (got is None or abs(got - want[key]) > 1):
                return False, f"{key} {got}, wanted {want[key]}"
    if "services_n" in want and len(turn.services) < want["services_n"]:
        return False, f"services {turn.services}, wanted at least {want['services_n']}"
    if "note_has" in want and want["note_has"].lower() not in (turn.note or "").lower():
        return False, f"note {turn.note!r}, wanted {want['note_has']!r} in it"
    if "say" in want and turn.say.strip() != want["say"]:
        return False, f"say {turn.say!r}, wanted exactly {want['say']!r}"
    if "support" in want and turn.support != want["support"]:
        return False, f"support {turn.support}, wanted {want['support']}"
    return True, ""


def run(items: list[TurnCase], golden: dict[str, cases.Case], gen) -> list[dict]:
    """One row per turn, in order: the Turn the model read (or the error), judged."""
    skill = skills.load("before_plan")
    rows = []
    for tc in items:
        material = material_of(tc, golden)
        try:
            turn = gen.read_turn(material, tc.words, skill=skill)
        except llm.LLMError as e:
            rows.append({"id": tc.id, "expect": tc.expect, "got": None, "ok": False, "why": str(e), "say": ""})
            continue
        ok, why = judge(tc, turn)
        got = {"intent": turn.intent, "confidence": round(turn.confidence, 2)}
        got.update(
            {k: getattr(turn, k) for k in ("option_id", "question_id", *NUMBER_FIELDS) if getattr(turn, k) is not None}
        )
        if turn.services:
            got["services"] = list(turn.services)
        rows.append({"id": tc.id, "expect": tc.expect, "got": got, "ok": ok, "why": why, "say": turn.say})
    return rows


def passes(rows: list[dict]) -> tuple[bool, list[str]]:
    """The gate (§9): every MUST_ALL turn right, no NEVER_FALSE intent read where the turn did not expect it, and
    PASS_RATE of all; the reasons it fails."""
    reasons = []
    for must in MUST_ALL:
        missed = [r["id"] for r in rows if must in accepted(r["expect"]) and not r["ok"]]
        if missed:
            reasons.append(f"{must}: {', '.join(missed)} read wrong (every one must read right)")
    for intent in NEVER_FALSE:
        false = [
            r["id"] for r in rows if (r["got"] or {}).get("intent") == intent and intent not in accepted(r["expect"])
        ]
        if false:
            reasons.append(f"{intent}: {', '.join(false)} read as it though not expected")
    right = sum(1 for r in rows if r["ok"])
    if rows and right / len(rows) < PASS_RATE:
        reasons.append(f"{right} of {len(rows)} right: under {PASS_RATE:.0%}")
    return (not reasons and bool(rows)), reasons


def report(rows: list[dict], *, model: str, effort: str | None, day: str) -> tuple[str, bool]:
    """A Markdown table and the verdict, for the screen and docs/ops/agent-eval-log.md: the model's say on the rows
    whose say the owner reads (SAYS), every `|` in a cell escaped."""
    ok, reasons = passes(rows)
    lines = [
        f"### Harness turns {day} — {model}, effort {effort or 'default'}: {sum(1 for r in rows if r['ok'])} of {len(rows)} right, {'PASS' if ok else 'FAIL'}",
        "",
        "| id | expect | got | ok | why | say |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        cells = (
            r["id"],
            json.dumps(r["expect"], ensure_ascii=False),
            json.dumps(r["got"], ensure_ascii=False),
            "yes" if r["ok"] else "no",
            r["why"],
            r["say"] if (r["got"] or {}).get("intent") in SAYS else "",
        )
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    for reason in reasons:
        lines.append(f"\n{reason}")
    return "\n".join(lines), ok
