"""What code does with a Turn's one dispatch (spec 2026-10-10 K6 slice 1 §4.4, §5): each intent on a real session,
the model faked away."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import business, consult, copy, envelope, home_api, keys, listings, models, support, tools_consult
from agent.harness import dispatch
from agent.tests.fixtures import knowledge_fixture, turn_fixture

KNOWLEDGE = knowledge_fixture().model_dump()
STOCK = {
    "website": {"none": True, "raw": "none", "source": "choice"},
    "business_name": {"name": "Bright Smile", "raw": "Bright Smile", "source": "other"},
    "services_offered": {"skipped": True, "raw": "skip", "source": "other"},
    "objective": {"choice": "bookings", "raw": "bookings", "source": "choice"},
    "service": {"choice": ["svc_invisalign"], "raw": ["svc_invisalign"], "source": "choice"},
    "customer_value": {"choice": "1000_plus", "raw": "1000_plus", "source": "choice"},
    "location": {"skipped": True, "raw": "later", "source": "other"},
    "radius": {"choice": "r5", "raw": "r5", "source": "choice"},
}


@pytest.fixture
def draft(conn):
    """A draft business with a dentists consultation answered up to the objective question (open: objective)."""
    return _business(conn, "draft", ("website", "business_name", "services_offered"))


def _business(conn, claim_state, answered):
    biz_id = business.create(conn)
    conn.execute("UPDATE business SET claim_state = %s WHERE id = %s", (claim_state, biz_id))
    business.add_knowledge(conn, biz_id, KNOWLEDGE, source="extract")
    plaintext, rec = keys.mint(
        conn,
        business_id=biz_id,
        cls="draft" if claim_state == "draft" else "business",
        scopes=("read", "manage"),
        label="draft" if claim_state == "draft" else "website",
    )
    tree = consult.load_tree("dentists")
    session = consult.create(
        conn,
        biz_id,
        tree="dentists",
        locale="en-US",
        opening_text="dentist in Palo Alto",
        known_facts={},
        options=consult.build_options(tree, KNOWLEDGE),
        answers={q: STOCK[q] for q in answered},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    principal = SimpleNamespace(key=rec, ip="198.51.100.9", business_id=biz_id, actor=f"key:{rec.id}")
    return SimpleNamespace(
        id=biz_id, row=business.row(conn, biz_id), session=session, principal=principal, key=plaintext
    )


def _run(conn, b, turn, **kw):
    args = {"support_on": False, "linked": False, "idempotency_key": "ik_1", **kw}
    return dispatch.run(conn, b.principal, b.row, b.session, "the words", turn, **args)


def test_an_answer_reroutes_into_answer_with_the_reading_for_the_open_question(conn, draft):
    out = _run(conn, draft, turn_fixture(intent="answer", option_id="calls"))
    assert isinstance(out, dispatch.Reroute) and out.action == "answer"
    assert out.body == {"answers": {"objective": {"other": "the words"}}}
    assert out.reading["objective"].option_id == "calls" and out.reading["objective"].understood is True
    assert dispatch.name_of(out) == "answer"


def test_an_answer_or_a_skip_to_a_question_asked_again_reroutes_to_it_open_or_ready(conn, draft):
    """C1 (final review): `open_q` -- turn.run's, the question the page shows asked again -- is the one the reroute
    answers, never the first unanswered one; at ready too, where no question is open otherwise."""
    asked = consult.question_for(draft.session, "business_name")["question"]
    out = _run(conn, draft, turn_fixture(intent="answer", note="Bright Smiles"), open_q=asked)
    assert out.action == "answer" and out.body == {"answers": {"business_name": {"other": "the words"}}}
    assert list(out.reading) == ["business_name"]
    ready = _business(conn, "claimed", tuple(STOCK))
    objective = consult.question_for(ready.session, "objective")["question"]
    out = _run(conn, ready, turn_fixture(intent="answer", option_id="calls"), open_q=objective)
    assert (
        out.body == {"answers": {"objective": {"other": "the words"}}} and out.reading["objective"].option_id == "calls"
    )
    skip = _run(conn, ready, turn_fixture(intent="skip"), open_q=objective)
    assert skip == dispatch.Reroute("answer", {"answers": {"objective": {"choice": "any"}}}, {})


def test_a_reply_under_a_question_asked_again_asks_that_question_again_open_or_ready(conn, monkeypatch):
    """Ruling 19 (C1 threaded through `again` and `ticket`): what Admiry says under a question asked again re-asks
    that question, never the first unanswered one -- at ready never READY_SAY -- so its card, and its id on the next
    words, stay; a budget ask still draws no card."""
    explain = turn_fixture(intent="explain", say="It tells me what to plan the ads around.")
    opened = _business(conn, "draft", ("website", "business_name", "services_offered", "objective"))  # open: service
    ready = _business(conn, "claimed", tuple(STOCK))
    for b in (opened, ready):
        asked = consult.question_for(b.session, "objective")
        out = _run(conn, b, explain, open_q=asked["question"])
        assert out["next"] == asked and out["say"].endswith(consult.question_sentence(asked)), b.session["status"]
        assert dispatch.READY_SAY not in out["say"]
        noted = _run(conn, b, turn_fixture(intent="budget", monthly_usd=500), open_q=asked["question"])
        assert noted["say"].startswith("Noted: $500 a month.") and noted["next"] == asked
        later = _run(conn, b, turn_fixture(intent="budget", monthly_usd=None), open_q=asked["question"])
        assert later == envelope.make({"session_id": b.session["id"]}, say=dispatch.BUDGET_LATER)
    contacted = {"ticket_id": "tk_1", "status": "open", "appended": False, "contact_email": "owner@example.com"}
    monkeypatch.setattr(support, "open_or_append", lambda conn, **kw: contacted)  # an acknowledgement with no link
    asked = consult.question_for(opened.session, "objective")
    out = _run(conn, opened, turn_fixture(intent="support", support="human"), support_on=True, open_q=asked["question"])
    assert out["data"]["support"]["ticket_id"] == "tk_1" and out["next"] == asked


def test_go_to_asks_the_question_again_and_off_the_menu_asks_the_open_question_again(conn, draft):
    out = _run(conn, draft, turn_fixture(intent="go_to", question_id="business_name", say="Sure."))
    assert out["next"]["kind"] == "question" and out["next"]["question"]["id"] == "business_name"
    assert out["next"]["question"]["index"] == 2 and out["say"].startswith("Of course.")
    assert out["say"].endswith(consult.question_sentence(out["next"]))
    for off in ("objective", "listing", "radius", "no_such", None):
        again = _run(conn, draft, turn_fixture(intent="go_to", question_id=off))
        assert again["next"]["question"]["id"] == "objective", off
        assert again["say"].startswith(dispatch.UNCLEAR_SAY)


def test_start_over_retires_a_draft_and_supersedes_a_claimed_businesss_consultation(conn, draft):
    out = _run(conn, draft, turn_fixture(intent="start_over", confidence=0.95))
    assert out == dispatch.StartOver(fresh=True)
    assert conn.execute("SELECT status FROM business WHERE id = %s", (draft.id,)).fetchone()["status"] == "expired"
    assert keys.lookup(conn, draft.key).revoked_at is not None
    assert (
        conn.execute("SELECT status FROM consult_session WHERE id = %s", (draft.session["id"],)).fetchone()["status"]
        == "open"
    )
    claimed = _business(conn, "claimed", ("website", "business_name", "services_offered"))
    out = _run(conn, claimed, turn_fixture(intent="start_over", confidence=0.95))
    assert out == dispatch.StartOver(fresh=False)
    assert (
        conn.execute("SELECT status FROM consult_session WHERE id = %s", (claimed.session["id"],)).fetchone()["status"]
        == "superseded"
    )
    assert conn.execute("SELECT status FROM business WHERE id = %s", (claimed.id,)).fetchone()["status"] == "active"
    assert keys.lookup(conn, claimed.key).revoked_at is None


def test_start_over_supersedes_a_released_claim_and_never_retires_a_business_with_an_owner(conn, draft):
    """I1 (final review): claim-expire puts an unverified claim back to draft but keeps its owner e-mail and the
    upgraded key; that business is the owner's -- its questions start over, it is never expired, its keys stay live."""
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (draft.id,))
    released = SimpleNamespace(**{**vars(draft), "row": business.row(conn, draft.id)})
    assert released.row["claim_state"] == "draft"
    assert _run(conn, released, turn_fixture(intent="start_over", confidence=0.95)) == dispatch.StartOver(fresh=False)
    assert conn.execute("SELECT status FROM business WHERE id = %s", (draft.id,)).fetchone()["status"] == "active"
    assert keys.lookup(conn, draft.key).revoked_at is None
    assert (
        conn.execute("SELECT status FROM consult_session WHERE id = %s", (draft.session["id"],)).fetchone()["status"]
        == "superseded"
    )


def test_a_start_over_read_below_the_bar_asks_back_and_changes_nothing(conn, draft):
    """M2 (final review): the confirm is said alone -- no question card, no Build button -- the yes or no is the next
    turn."""
    out = _run(conn, draft, turn_fixture(intent="start_over", confidence=0.7))
    assert out == envelope.make({"session_id": draft.session["id"]}, say=dispatch.CONFIRM_START_OVER)
    assert conn.execute("SELECT status FROM business WHERE id = %s", (draft.id,)).fetchone()["status"] == "active"


def test_a_budget_with_an_amount_writes_the_owners_limit_and_keeps_the_rest_of_the_goal(conn, draft):
    business.set_goal(
        conn, draft.id, target=12, monthly_limit_usd=900, focus_services=["Invisalign"], limit_source="system"
    )
    out = _run(conn, draft, turn_fixture(intent="budget", monthly_usd=500))
    goal = business.latest_goal(conn, draft.id)
    assert (goal["monthly_limit_usd"], goal["limit_source"], goal["target"], goal["focus_services"]) == (
        500,
        "owner",
        12,
        ["Invisalign"],
    )
    assert out["say"].startswith("Noted: $500 a month.") and out["next"]["question"]["id"] == "objective"
    out = _run(conn, draft, turn_fixture(intent="budget", monthly_usd=20 * 30))
    assert business.latest_goal(conn, draft.id)["monthly_limit_usd"] == 600 and "$600" in out["say"]


def test_a_budget_without_an_amount_says_it_comes_with_the_plan_and_draws_no_card(conn, draft):
    before = conn.execute("SELECT count(*) AS n FROM goal WHERE business_id = %s", (draft.id,)).fetchone()["n"]
    out = _run(conn, draft, turn_fixture(intent="budget", monthly_usd=None, say="whatever the model wrote"))
    assert out["say"] == dispatch.BUDGET_LATER and out["next"] == {"kind": "none"}
    assert conn.execute("SELECT count(*) AS n FROM goal WHERE business_id = %s", (draft.id,)).fetchone()["n"] == before


def test_a_budget_out_of_range_is_refused_and_nothing_is_written(conn, draft):
    for amount in (0, -5, business.BUDGET_MAX + 1, 5_000_000):
        out = _run(conn, draft, turn_fixture(intent="budget", monthly_usd=amount))
        assert out["say"].startswith(dispatch.BUDGET_RANGE) and out["next"]["question"]["id"] == "objective", amount
    assert business.latest_goal(conn, draft.id) is None


def test_build_runs_recommend_at_ready_and_asks_the_question_before(conn, draft):
    assert _run(conn, draft, turn_fixture(intent="build"))["say"].startswith(dispatch.NOT_READY)
    ready = _business(conn, "claimed", tuple(STOCK))
    assert ready.session["status"] == "ready"
    out = _run(conn, ready, turn_fixture(intent="build"))
    assert out == dispatch.Reroute("recommend", {}, {}) and dispatch.name_of(out) == "recommend"


def test_at_ready_an_answer_or_a_skip_says_what_to_do_with_the_build_action(conn):
    ready = _business(conn, "claimed", tuple(STOCK))
    out = _run(conn, ready, turn_fixture(intent="answer", option_id="calls"))
    assert out["say"] == dispatch.READY_SAY and out["next"] == tools_consult.RECOMMEND_ACTION
    assert _run(conn, ready, turn_fixture(intent="skip"))["next"] == tools_consult.RECOMMEND_ACTION


def test_a_skip_picks_any_on_a_choice_question_hands_a_skip_for_words_and_asks_again_otherwise(conn, draft):
    out = _run(conn, draft, turn_fixture(intent="skip"))
    assert out == dispatch.Reroute("answer", {"answers": {"objective": {"choice": "any"}}}, {})
    named = _business(conn, "draft", ("website",))  # open: business_name, words only
    out = _run(conn, named, turn_fixture(intent="skip"))
    assert out.action == "answer" and out.body == {"answers": {"business_name": {"other": "the words"}}}
    assert out.reading == {"business_name": consult.SKIP}
    placed = _business(
        conn, "draft", ("website", "business_name", "services_offered", "objective", "service", "customer_value")
    )
    assert _run(conn, placed, turn_fixture(intent="skip")).reading == {"location": consult.SKIP}
    wide = _business(conn, "draft", tuple(q for q in STOCK if q != "radius"))
    out = _run(conn, wide, turn_fixture(intent="skip"))
    assert out["say"].startswith(dispatch.SKIP_SAY) and out["next"]["question"]["id"] == "radius"


@pytest.mark.parametrize("intent", ["explain", "ask", "smalltalk"])
def test_explain_ask_and_smalltalk_say_the_turns_words_and_ask_the_question_again(conn, draft, intent):
    out = _run(conn, draft, turn_fixture(intent=intent, say="A short sentence from the model."))
    nxt = consult.next_question(draft.session)
    assert out["say"] == f"A short sentence from the model. {consult.question_sentence(nxt)}" and out["next"] == nxt
    assert (
        conn.execute("SELECT answers FROM consult_session WHERE id = %s", (draft.session["id"],)).fetchone()["answers"]
        == draft.session["answers"]
    )
    empty = _run(conn, draft, turn_fixture(intent=intent, say="  "))
    assert empty["say"].startswith(dispatch.FALLBACK_SAY[intent])


def test_the_models_say_reaches_the_owner_only_plain_without_a_number_and_short(conn, draft):
    """I5 (final review): the model's say is the owner's to read only with no advertising jargon (copy.jargon), no
    digit and at most 300 characters; else the intent's fixed sentence (unclear: UNCLEAR_SAY)."""
    clean = "It tells me what to plan the ads around."
    assert dispatch.safe_say("explain", clean) == clean and dispatch.safe_say("ask", "x" * 300) == "x" * 300
    assert dispatch.safe_say("ask", "That depends on the campaign.") == dispatch.FALLBACK_SAY["ask"]
    assert dispatch.safe_say("smalltalk", "Most dentists spend $500 a month.") == dispatch.FALLBACK_SAY["smalltalk"]
    assert dispatch.safe_say("explain", "x" * 301) == dispatch.FALLBACK_SAY["explain"]
    assert dispatch.safe_say("unclear", "Do you mean 2 things?") == dispatch.UNCLEAR_SAY
    assert dispatch.safe_say("unclear", "  ") == dispatch.UNCLEAR_SAY
    out = _run(conn, draft, turn_fixture(intent="ask", say="Dentists spend about $1,000 a month."))
    assert out["say"] == f"{dispatch.FALLBACK_SAY['ask']} {consult.question_sentence(out['next'])}"


def test_an_unclear_turn_or_a_low_confidence_one_asks_back_with_the_fixed_sentence(conn, draft):
    for turn in (
        turn_fixture(intent="unclear", say="Do you mean A or B?"),
        turn_fixture(intent="answer", option_id="calls", confidence=0.4),
    ):
        out = _run(conn, draft, turn)
        assert isinstance(out, dict) and out["next"]["question"]["id"] == "objective"
    assert _run(conn, draft, turn_fixture(intent="unclear", say="Do you mean A or B?"))["say"].startswith(
        "Do you mean A or B?"
    )
    assert _run(conn, draft, turn_fixture(intent="answer", option_id="calls", confidence=0.4))["say"].startswith(
        dispatch.UNCLEAR_SAY
    )


def test_support_with_it_on_opens_the_ticket_and_asks_again_and_with_it_off_says_so(conn, draft, support_on):
    turn = turn_fixture(intent="support", support="human", support_summary="Wants a person.")
    out = _run(conn, draft, turn, support_on=True)
    ticket = conn.execute("SELECT reason, summary FROM support_ticket WHERE business_id = %s", (draft.id,)).fetchone()
    assert ticket["reason"] == "human" and ticket["summary"] == "Wants a person."
    assert out["data"]["support"] and (out["next"]["kind"] == "link" or out["next"]["question"]["id"] == "objective")
    off = _run(conn, draft, turn, support_on=False)
    assert off["say"].startswith(dispatch.SUPPORT_OFF_SAY) and off["next"]["question"]["id"] == "objective"
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1


def test_not_mine_reroutes_into_the_unlink_when_a_place_is_linked_and_says_so_otherwise(conn, draft):
    out = _run(conn, draft, turn_fixture(intent="not_mine", listing_not_mine=True), linked=True)
    assert out == dispatch.Reroute("update_business", {"idempotency_key": "ik_1", "then": listings.LINK_AGAIN}, {})
    off = _run(conn, draft, turn_fixture(intent="not_mine", listing_not_mine=True), linked=False)
    assert off["say"].startswith(dispatch.NOT_LINKED_SAY)


def test_the_fixed_sentences_read_plainly_and_the_commands_the_intents_run_are_safe():
    from agent import operations

    for text in (
        dispatch.READY_SAY,
        dispatch.BUDGET_LATER,
        dispatch.BUDGET_NOTED.format(amount=500),
        dispatch.BUDGET_RANGE,
        dispatch.CONFIRM_START_OVER,
        dispatch.NOT_READY,
        dispatch.SKIP_SAY,
        dispatch.SUPPORT_OFF_SAY,
        dispatch.NOT_LINKED_SAY,
        dispatch.UNCLEAR_SAY,
        *dispatch.FALLBACK_SAY.values(),
    ):
        assert copy.jargon(text) is None, text
    assert dispatch.READY_SAY == home_api.READY_SAY and dispatch.RECOMMEND == tools_consult.RECOMMEND_ACTION
    assert set(dispatch.COMMAND_OF.values()) <= operations.SAFE
    assert set(dispatch.COMMAND_OF) | operations.HARNESS_SAFE == set(models.TURN_INTENTS)
