"""The material the harness's one call reads (spec 2026-10-10 K6 slice 1 §4.2): the business, the consultation with
its answers and the open question, the questions the owner may return to, the last turns -- within its budget, and
never anything of another business."""

import json
from datetime import datetime, timedelta, timezone

from agent import audit, business, consult
from agent.harness import context
from agent.knowledge_index import tokens
from agent.tests.fixtures import knowledge_fixture
from agent.tests.test_consult_parse import _q, _session

KNOWLEDGE = knowledge_fixture().model_dump()
GOAL = {"goal_type": "customers", "monthly_limit_usd": 2000, "limit_source": "owner", "target": None}


def _answered(*ids):
    s = _session()
    stock = {
        "website": {"none": True, "raw": "none", "source": "choice"},
        "business_name": {"name": "Bright Smile", "raw": "Bright Smile", "source": "other"},
        "services_offered": {
            "services": ["Cleanings", "Whitening"],
            "raw": "cleanings and whitening",
            "source": "other",
        },
        "objective": {"choice": "bookings", "raw": "bookings", "source": "choice"},
        "service": {"choice": ["svc_invisalign"], "raw": ["svc_invisalign"], "source": "choice"},
        "customer_value": {"skipped": True, "raw": "no idea", "source": "other"},
        "location": {
            "note": "Palo Alto",
            "address": "Palo Alto, CA",
            "lat": 37.4,
            "lng": -122.1,
            "raw": "palo alto",
            "source": "other",
        },
        "radius": {"miles": 5.0, "raw": "within 5 miles", "source": "known_fact"},
    }
    s["answers"] = {qid: stock[qid] for qid in ids}
    s["status"] = "open"
    return s


def test_the_material_holds_the_questions_with_their_answers_the_open_one_with_its_options_and_why():
    s = _answered("website", "business_name", "services_offered", "objective")
    m = context.material(KNOWLEDGE, GOAL, s, [{"owner": "hi", "admiry": "Hello."}])
    qs = {q["id"]: q["answer"] for q in m["consultation"]["questions"]}
    label = next(o["label"] for o in s["options"]["objective"] if o["id"] == "bookings")
    assert qs["website"] == "No website" and qs["business_name"] == "Bright Smile"
    assert qs["services_offered"] == "Cleanings, Whitening" and qs["objective"] == label and qs["service"] == "open"
    open_q = m["consultation"]["open_question"]
    assert open_q["id"] == "service" and [o["id"] for o in open_q["options"]] == [
        o["id"] for o in s["options"]["service"]
    ]
    assert ("why" in open_q) == bool(_q("service").get("why"))
    assert m["consultation"]["status"] == "open"
    assert m["may_go_to"] == ["website", "business_name", "services_offered", "objective"]
    assert m["business"]["goal"] == {"goal_type": "customers", "monthly_limit_usd": 2000, "limit_source": "owner"}
    assert m["recent"] == [{"owner": "hi", "admiry": "Hello."}]
    assert (
        "listing_rejected" not in m["business"]["knowledge"] and m["business"]["knowledge"]["name"] == KNOWLEDGE["name"]
    )
    assert all(q["prompt"] for q in m["consultation"]["questions"])


def test_a_ready_consultation_says_so_and_has_no_open_question():
    s = _answered(
        "website", "business_name", "services_offered", "objective", "service", "customer_value", "location", "radius"
    )
    s["status"] = "ready"
    m = context.material(KNOWLEDGE, None, s, [])
    assert m["consultation"]["status"].startswith("ready") and "open_question" not in m["consultation"]
    assert m["business"]["goal"] is None and m["may_go_to"] == [q["id"] for q in consult.questions(s)]


def test_a_question_asked_again_is_the_open_question_and_leaves_the_menu():
    """C1 (final review): open_question -- turn.run's and the golden turns' -- is the question the page shows asked
    again when its id is on the menu, else the first unanswered one; the material reads it as the open question."""
    s = _answered("website", "business_name", "services_offered", "objective")
    for off in (None, "service", "no_such"):  # not answered, unknown, none: the first unanswered question
        assert context.open_question(s, off)["id"] == "service", off
    asked = context.open_question(s, "objective")
    m = context.material(KNOWLEDGE, GOAL, s, [], open_question=asked)
    assert m["consultation"]["open_question"]["id"] == "objective" and m["consultation"]["status"] == "open"
    assert [o["id"] for o in m["consultation"]["open_question"]["options"]] == [o["id"] for o in asked["options"]]
    assert m["may_go_to"] == ["website", "business_name", "services_offered"]
    ready = _answered(
        "website", "business_name", "services_offered", "objective", "service", "customer_value", "location", "radius"
    )
    ready["status"] = "ready"
    assert context.open_question(ready, None) is None
    m = context.material(KNOWLEDGE, None, ready, [], open_question=context.open_question(ready, "objective"))
    assert m["consultation"]["status"] == "open" and m["consultation"]["open_question"]["id"] == "objective"
    assert "objective" not in m["may_go_to"]


def test_answers_read_as_the_owner_gave_them():
    s = _answered(
        "website", "business_name", "services_offered", "objective", "service", "customer_value", "location", "radius"
    )
    qs = {q["id"]: q["answer"] for q in context.material(KNOWLEDGE, None, s, [])["consultation"]["questions"]}
    assert qs["customer_value"] == "skipped" and qs["location"] == "Palo Alto, CA" and qs["radius"] == "within 5 miles"
    assert qs["service"] == next(o["label"] for o in s["options"]["service"] if o["id"] == "svc_invisalign")


def _row(conn, biz, tool, words, say=None):
    audit.record(
        conn,
        tool=tool,
        args={"text": words},
        result={"text": words, "say": say},
        ok=True,
        business_id=biz,
        actor="key:k",
        actor_class="draft",
        api_key_id=None,
        ip=None,
    )


def test_recent_holds_only_the_turns_after_the_latest_answer(conn):
    """I2 (final review): an ask of Admiry's is live only while nothing was answered after it -- recent holds the
    free-text turns newer than the business's latest `answer` row, every one while there is none."""
    biz = business.create(conn)
    for i in range(3):
        _row(conn, biz, "web.turn", f"words {i}", f"ask {i}")
    assert [t["owner"] for t in context.recent(conn, biz)] == ["words 0", "words 1", "words 2"]
    _row(conn, biz, "answer", "")
    _row(conn, biz, "web.say", "after the answer")
    assert context.recent(conn, biz) == [{"owner": "after the answer"}]


def test_recent_turns_come_from_the_homes_rows_oldest_first_and_clipped(conn):
    biz = business.create(conn)
    audit.record(  # an answer before the turns: never a turn itself, and it hides none of them (I2)
        conn,
        tool="answer",
        args={},
        result={"say": "not a free-text turn"},
        ok=True,
        business_id=biz,
        actor="key:k",
        actor_class="draft",
        api_key_id=None,
        ip=None,
    )
    for i in range(7):
        audit.record(
            conn,
            tool="web.turn" if i % 2 else "web.say",
            args={"text": f"words {i}"},
            result={"text": f"words {i} " + "x" * 400, "say": f"said {i}" if i % 2 else None},
            ok=True,
            business_id=biz,
            actor="key:k",
            actor_class="draft",
            api_key_id=None,
            ip=None,
        )
    got = context.recent(conn, biz)
    assert [t["owner"][:7] for t in got] == ["words 2", "words 3", "words 4", "words 5", "words 6"]
    assert all(len(t["owner"]) <= context.RECENT_CHARS for t in got)
    assert got[1]["admiry"] == "said 3" and "admiry" not in got[0]
    assert context.recent(conn, business.create(conn)) == []


def test_the_material_fits_the_budget_and_is_trimmed_from_the_oldest_turn_then_the_knowledge():
    s = _answered("website", "business_name", "services_offered")
    long_turns = [
        {"owner": f"{i} " + "w" * context.RECENT_CHARS, "admiry": "s" * context.RECENT_CHARS} for i in range(5)
    ]
    fat = {**KNOWLEDGE, "reviews_summary": "r" * 9000, "usp": "u" * 3000}
    m = context.material(fat, GOAL, s, long_turns)
    assert tokens(json.dumps(m, ensure_ascii=False, default=str)) <= context.MATERIAL_TOKENS
    assert m["recent"] == [] or m["recent"][0]["owner"].startswith(str(5 - len(m["recent"])))  # the oldest went first
    assert set(m["business"]["knowledge"]) <= set(context.CORE_KNOWLEDGE)  # then the knowledge kept its core
    small = context.material(KNOWLEDGE, GOAL, s, long_turns[:1])
    assert small["recent"] == long_turns[:1] and small["business"]["knowledge"]["usp"] == KNOWLEDGE["usp"]


def test_nothing_of_another_business_is_read(conn):
    alpha, beta = business.create(conn), business.create(conn)
    business.add_knowledge(conn, alpha, {**KNOWLEDGE, "name": "Alpha Dental"}, source="extract")
    business.add_knowledge(conn, beta, {**KNOWLEDGE, "name": "Beta Dental"}, source="extract")
    business.set_goal(conn, beta, monthly_limit_usd=777, limit_source="owner")
    audit.record(
        conn,
        tool="web.say",
        args={"text": "beta's words"},
        result={"text": "beta's words"},
        ok=True,
        business_id=beta,
        actor="key:k",
        actor_class="draft",
        api_key_id=None,
        ip=None,
    )
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        alpha,
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, KNOWLEDGE),
        answers={},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    text = json.dumps(context.load(conn, alpha, s), ensure_ascii=False, default=str)
    assert "Alpha Dental" in text and "Beta" not in text and "777" not in text and "beta's words" not in text
