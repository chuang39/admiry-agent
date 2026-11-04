"""The material one Turn reads (spec 2026-10-10 K6 slice 1 §4.2): the key's own business alone -- its knowledge as a
model reads it and its goal, the consultation (every question with its answer so far, the open one with its options
and what it is for, the status), the questions the owner may return to, the last free-text turns -- within
MATERIAL_TOKENS. Every block goes between data tags in llm.read_turn. Nothing of another business is ever read into
it: everything comes from the one business id `load` is given."""

from __future__ import annotations

import json

from .. import business, consult, profile
from ..knowledge_index import tokens

MATERIAL_TOKENS = 3000  # the budget (§4.2); knowledge_index.tokens is the unit
RECENT_TURNS = 5
RECENT_CHARS = 300  # of each recent turn's words, the owner's and Admiry's
TOOLS = ("web.say", "web.turn")  # the home's free-text rows (home_api._record_say, turn._record)
# what the knowledge keeps when the material is over budget (trim): the facts a turn needs
CORE_KNOWLEDGE = ("name", "category", "category_detail", "services", "service_area", "listing", "website", "locale")
READY = "ready: every question is answered; the plan can be built"


def open_question(session: dict, question_id: str | None) -> dict | None:
    """The question the owner's words answer (C1, spec §4.4): the one the page shows asked again when `question_id`
    (what the page posts with the words) is on the menu (consult.may_go_to), else the first unanswered one; None at
    ready with nothing asked again. turn.run and the golden turns (eval/turns.material_of) both read it here."""
    if question_id in consult.may_go_to(session):
        return consult.question_for(session, question_id)["question"]
    nxt = consult.next_question(session)
    return nxt["question"] if nxt else None


def load(conn, business_id: str, session: dict, open_question: dict | None = None) -> dict:
    """The material from the database: the business's latest knowledge and goal, the session, its recent turns."""
    latest = business.latest_knowledge(conn, business_id)
    return material(
        (latest or (None, {}))[1],
        business.latest_goal(conn, business_id),
        session,
        recent(conn, business_id),
        open_question=open_question,
    )


def recent(conn, business_id: str, limit: int = RECENT_TURNS) -> list[dict]:
    """The last `limit` free-text turns of the home (web.say and web.turn rows), oldest first: the owner's words
    (result.text, whole since D147; else the clipped argument) and what Admiry said back (result.say) when it did.
    Only the turns after the business's latest `answer` row (I2): an ask of Admiry's is live only while nothing was
    answered after it, so later words are never read as a reply to it."""
    rows = conn.execute(
        "SELECT args_redacted, result FROM agent_action WHERE business_id = %s AND tool = ANY(%s) "
        "AND ts > COALESCE((SELECT max(ts) FROM agent_action WHERE business_id = %s AND tool = 'answer'), '-infinity') "
        "ORDER BY ts DESC, id DESC LIMIT %s",
        (business_id, list(TOOLS), business_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        result = r["result"] if isinstance(r["result"], dict) else {}
        words = str(result.get("text") or (r["args_redacted"] or {}).get("text") or "")
        turn = {"owner": words[:RECENT_CHARS]}
        if result.get("say"):
            turn["admiry"] = str(result["say"])[:RECENT_CHARS]
        out.append(turn)
    return out


def material(
    knowledge: dict, goal: dict | None, session: dict, recent_turns: list[dict], open_question: dict | None = None
) -> dict:
    """The blocks, as data, trimmed to the budget. `open_question` is the question the words answer (`open_question`
    above: one asked again is open, and off the menu); None, the first unanswered one."""
    asks = consult.questions(session)
    if open_question is None:
        nxt = consult.next_question(session)
        open_question = nxt["question"] if nxt else None
    consultation = {
        "status": READY if open_question is None else "open",
        "questions": [
            {"id": q["id"], "prompt": consult.prompt_for(q, session), "answer": _answer_words(session, q)} for q in asks
        ],
    }
    if open_question is not None:
        why = next((q.get("why") for q in asks if q["id"] == open_question["id"]), None)
        consultation["open_question"] = {
            "id": open_question["id"],
            "prompt": open_question["prompt"],
            "input": open_question["input"],
            "options": [{"id": o["id"], "label": o["label"]} for o in open_question["options"]],
            **({"why": why} if why else {}),
        }
    goal_block = (
        {
            "goal_type": goal.get("goal_type"),
            "monthly_limit_usd": goal.get("monthly_limit_usd"),
            "limit_source": goal.get("limit_source"),
        }
        if goal
        else None
    )
    return trim(
        {
            "business": {"knowledge": profile.for_model(knowledge), "goal": goal_block},
            "consultation": consultation,
            "may_go_to": [
                qid for qid in consult.may_go_to(session) if open_question is None or qid != open_question["id"]
            ],
            "recent": list(recent_turns),
        }
    )


def trim(material: dict) -> dict:
    """The material within MATERIAL_TOKENS: the oldest recent turns go first, then the knowledge keeps its core alone."""
    out = {**material, "recent": list(material.get("recent") or [])}
    while tokens(_text(out)) > MATERIAL_TOKENS and out["recent"]:
        out["recent"] = out["recent"][1:]
    if tokens(_text(out)) > MATERIAL_TOKENS:
        know = out["business"]["knowledge"]
        out["business"] = {**out["business"], "knowledge": {k: know[k] for k in CORE_KNOWLEDGE if k in know}}
    return out


def _text(material: dict) -> str:
    return json.dumps(material, ensure_ascii=False, default=str)


def _answer_words(session: dict, q: dict) -> str:
    """The answer as the owner gave it: the labels picked, the words typed, "skipped", or "open"."""
    a = session["answers"].get(q["id"])
    if a is None:
        return "open"
    if a.get("skipped"):
        return "skipped"
    if a.get("none"):
        return "No website"
    if a.get("url"):
        return str(a["url"])
    options = session.get("options", {}).get(q["id"], q.get("options", []))
    if "choice" in a:
        ids = a["choice"] if isinstance(a["choice"], list) else [a["choice"]]
        labels = [o["label"] for o in options if o["id"] in ids]
        if labels:
            return ", ".join(labels)
    for key in ("services", "name", "address", "note", "other", "raw"):
        if a.get(key):
            return ", ".join(a[key]) if isinstance(a[key], list) else str(a[key])
    return "answered"
