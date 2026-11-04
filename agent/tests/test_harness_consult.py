"""The consultation's seams the harness uses (spec 2026-10-10 K6 slice 1 §4.4, D2, D5): a reading handed in place of
read_answer, a skip handed, a question asked again, the questions the owner may return to; and the `answer` command
making no call with a handed reading."""

from datetime import datetime, timedelta, timezone

import pytest

from agent import business, consult, home_api, keys, llm, models
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture
from agent.tests.test_consult_parse import _common_q, _q, _session

IP = "198.51.100.9"


def _read(**kw) -> models.AnswerRead:
    base = {"option_id": None, "amount_usd": None, "miles": None, "note": None, "understood": True, "services": []}
    return models.AnswerRead(**{**base, **kw})


def _answered(*ids):
    """A dentists session (no site read) with these questions answered the way the owner would have."""
    s = _session()
    stock = {
        "website": {"none": True, "raw": "none", "source": "choice"},
        "business_name": {"name": "Bright Smile", "raw": "Bright Smile", "source": "other"},
        "services_offered": {"skipped": True, "raw": "skip", "source": "other"},
        "objective": {"choice": "bookings", "raw": "bookings", "source": "choice"},
        "service": {"choice": ["svc_invisalign"], "raw": ["svc_invisalign"], "source": "choice"},
    }
    s["answers"] = {qid: stock[qid] for qid in ids}
    return s


def test_a_handed_reading_is_used_in_place_of_the_model_and_only_for_its_question():
    gen = FakeGenerator()
    with consult.with_reading({"objective": _read(option_id="calls")}):
        stored, why = consult.parse(_session(), _q("objective"), {"other": "the phone to ring"}, gen)
        assert stored == {"raw": "the phone to ring", "source": "other", "choice": "calls"} and why is None
        assert gen.read_calls == []
        consult.parse(_session(), _q("customer_value"), {"other": "about a grand"}, gen)
        assert gen.read_calls[-1]["question"]["id"] == "customer_value"  # not handed: the model, as before
        assert consult.reading_for("objective").option_id == "calls"
    assert consult.reading_for("objective") is None  # gone with the block


def test_a_handed_reading_serves_the_services_and_the_address_questions_too():
    gen = FakeGenerator()
    with consult.with_reading({"services_offered": _read(services=["Cleanings"]), "location": _read(note="Palo Alto")}):
        s = _session()
        stored, _ = consult.parse(s, _common_q("services_offered"), {"other": "we clean teeth"}, gen)
        assert stored == {"services": ["Cleanings"], "raw": "we clean teeth", "source": "other"}
        place, _ = consult.parse(s, _q("location"), {"other": "around palo alto"}, gen)
        assert place == {"note": "Palo Alto", "raw": "around palo alto", "source": "other"}
    assert gen.read_calls == []


def test_a_skip_handed_in_skips_a_text_or_address_question_as_the_owners_own_skip_words_would():
    gen = FakeGenerator()
    with consult.with_reading(
        {"services_offered": consult.SKIP, "location": consult.SKIP, "business_name": consult.SKIP}
    ):
        s = _session()
        assert consult.parse(s, _common_q("services_offered"), {"other": "let us not go into that"}, gen) == (
            {"skipped": True, "raw": "let us not go into that", "source": "other"},
            None,
        )
        assert consult.parse(s, _q("location"), {"other": "later maybe"}, gen) == (
            {"skipped": True, "raw": "later maybe", "source": "other"},
            None,
        )
        # the name question stores the words as the name today; a handed skip stores a skip, never the words (Review
        # Focus 5)
        assert consult.parse(s, _common_q("business_name"), {"other": "keep the name private"}, gen) == (
            {"skipped": True, "raw": "keep the name private", "source": "other"},
            None,
        )
    assert gen.read_calls == []


def test_question_for_asks_a_question_again_with_its_own_place_in_the_count():
    s = _answered("website", "business_name", "services_offered", "objective")
    nxt = consult.next_question(s)
    assert nxt["question"]["id"] == "service" and nxt["question"]["index"] == 5
    again = consult.question_for(s, "objective")
    assert again["kind"] == "question" and again["question"]["id"] == "objective"
    assert again["question"]["index"] == 4 and again["question"]["count"] == nxt["question"]["count"]
    assert again["question"]["options"] == s["options"]["objective"]
    assert consult.question_for(s, "website")["question"]["index"] == 1
    assert consult.question_for(s, "no_such") is None


def test_a_known_fact_counts_for_no_place_in_question_fors_index():
    s = _answered("website", "business_name", "services_offered", "objective")
    s["answers"]["radius"] = {"miles": 5.0, "raw": "within 5 miles", "source": "known_fact"}
    asked = [q["id"] for q in consult.questions(s)]
    assert asked.index("radius") > asked.index("location")
    assert consult.question_for(s, "location")["question"]["count"] == len(asked) - 1
    radius = consult.question_for(s, "radius")["question"]  # the fact itself asked again counts itself: "8 / 8"
    assert radius["index"] == len(asked) and radius["count"] == len(asked)


def test_may_go_to_lists_the_answered_questions_but_the_google_maps_place():
    s = _answered("website", "business_name", "objective")
    assert consult.may_go_to(s) == ["website", "business_name", "objective"]
    s["options"]["listing"] = [
        {"id": "gm_1", "label": "Bright Smile Dental, 500 University Ave"},
        dict(consult.LISTING_NONE),
    ]
    s["answers"]["listing"] = {"choice": "gm_1", "raw": "gm_1", "source": "choice"}
    assert "listing" in [q["id"] for q in consult.questions(s)] and "listing" not in consult.may_go_to(s)
    assert consult.may_go_to(_session()) == []


def test_answer_with_a_handed_reading_records_it_and_builds_no_generator(conn, monkeypatch):
    """D2: the home's harness read the words in its one Turn call; `answer` makes no second call and checks no cap --
    llm.generator is never reached (a fake that would fail stands in for it)."""
    monkeypatch.setattr(llm, "generator", lambda settings=None: FakeGenerator(error="must not be called"))
    key = business_with_key(conn, biz_id="biz_h4", claim_state="claimed")
    business.add_knowledge(conn, "biz_h4", knowledge_fixture().model_dump(), source="extract")
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_h4",
        tree="dentists",
        locale="en-US",
        opening_text="dentist in Palo Alto",
        known_facts={},
        options=consult.build_options(tree, knowledge_fixture().model_dump()),
        answers=_answered("website", "business_name", "services_offered")["answers"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    with consult.with_reading({"objective": _read(option_id="calls")}):
        env = home_api.call(
            "answer",
            {"session_id": s["id"], "answers": {"objective": {"other": "the phone to ring"}}, "idempotency_key": "h4"},
            key,
            ip=IP,
        )
    assert "error" not in env, env
    assert env["data"]["recorded"]["objective"]["choice"] == "calls" and env["next"]["question"]["id"] == "service"
    row = conn.execute("SELECT answers FROM consult_session WHERE id = %s", (s["id"],)).fetchone()["answers"]
    assert row["objective"] == {"raw": "the phone to ring", "source": "other", "choice": "calls"}


def test_the_sentinel_and_the_context_are_module_level_and_empty_outside_a_block():
    assert consult.reading_for("objective") is None and consult.SKIP is consult.SKIP
    with pytest.raises(TypeError):
        consult.SKIP()  # an object, never a reading


def test_retire_draft_expires_the_business_and_revokes_its_live_keys(conn):
    """D4: what consult.move_session does to a landed draft, in one place, for the harness's start over too (§5)."""
    biz = business.create(conn)
    plaintext, _ = keys.mint(conn, business_id=biz, cls="draft", scopes=("read", "manage"), label="draft")
    other = business.create(conn)
    keys.mint(conn, business_id=other, cls="draft", scopes=("read", "manage"), label="draft")
    business.retire_draft(conn, biz)
    assert conn.execute("SELECT status FROM business WHERE id = %s", (biz,)).fetchone()["status"] == "expired"
    assert keys.lookup(conn, plaintext).revoked_at is not None
    assert conn.execute("SELECT status FROM business WHERE id = %s", (other,)).fetchone()["status"] == "active"
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM api_key WHERE business_id = %s AND revoked_at IS NULL", (other,)
        ).fetchone()["n"]
        == 1
    )
