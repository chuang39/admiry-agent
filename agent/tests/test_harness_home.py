"""The home's free text through the harness (spec 2026-10-10 K6 slice 1 §3, §4, §5): /api/turn's `say` with
AGENT_HARNESS on, the model faked, every dispatch end to end."""

import pytest

from agent import benchmark, business, envelope, places
from agent.harness import dispatch
from agent.tests.fixtures import knowledge_fixture, turn_fixture
from agent.tests.test_home_api import IP, PICK, SITE, fake, turn  # noqa: F401 - fake is a fixture used by name
from agent.tests.test_oauth_consent import _sign_in
from agent.tests.test_tools_consult import crawled  # noqa: F401 - fixture used by name

START = {"action": "start", "text": "dentist in Palo Alto within 5 miles, $2,000 a month", "idempotency_key": "i1"}


@pytest.fixture
def harness_on(monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS", "on")


def _say(client, text, ik="s1", **extra):
    return turn(client, {"action": "say", "text": text, "idempotency_key": ik, **extra}).json()


def _walk(client, conn):
    """The site read, the place picked: the open question is the objective."""
    benchmark.seed(conn)
    turn(client, START)
    turn(client, SITE)
    r = turn(client, PICK).json()
    assert r["result"]["next"]["question"]["id"] == "objective"
    return conn.execute("SELECT id FROM business").fetchone()["id"]


def _rows(conn, biz):
    return conn.execute(
        "SELECT ok, result FROM agent_action WHERE tool = 'web.turn' AND business_id = %s ORDER BY id", (biz,)
    ).fetchall()


def _claim(client, google):
    csrf = _sign_in(client, google)
    r = client.post("/api/claim", json={"csrf": csrf}, headers=IP)
    assert r.status_code == 200 and r.json()["visitor"]["claimed"] is True


def test_a_typed_answer_goes_through_answer_with_the_turns_reading_and_no_second_call(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    biz = _walk(client, conn)
    fake.turns["the phone to ring"] = turn_fixture(intent="answer", option_id="calls")
    r = _say(client, "the phone to ring")
    assert r["ok"] and r["result"]["data"]["recorded"]["objective"]["choice"] == "calls"
    assert r["result"]["next"]["question"]["id"] == "service"
    assert len(fake.turn_calls) == 1 and fake.read_calls == []  # one call: the Turn; answer read nothing again
    material = fake.turn_calls[0]["material"]
    assert material["consultation"]["open_question"]["id"] == "objective" and material["business"]["knowledge"]["name"]
    [row] = _rows(conn, biz)
    assert row["ok"] and row["result"]["intent"] == "answer" and row["result"]["dispatch"] == "answer"
    assert row["result"]["text"] == "the phone to ring" and row["result"]["counted"] is False


def test_start_over_phrases_retire_a_draft_with_no_call_and_the_next_words_make_a_new_business(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    harness_on,
):
    biz = _walk(client, conn)
    r = turn(client, {"action": "say", "text": "i wanna restart", "idempotency_key": "s1"})
    assert r.json() == {"ok": True, "start_over": True}
    cookies = r.headers.get_list("set-cookie")
    assert any("admiry_visit=" in c and ("Max-Age=0" in c or "expires" in c.lower()) for c in cookies)
    assert any("admiry_visit_biz=" in c for c in cookies)
    assert fake.turn_calls == [] and fake.read_calls == []
    assert conn.execute("SELECT status FROM business WHERE id = %s", (biz,)).fetchone()["status"] == "expired"
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM api_key WHERE business_id = %s AND revoked_at IS NULL", (biz,)
        ).fetchone()["n"]
        == 0
    )
    [row] = _rows(conn, biz)
    assert (
        row["result"]["fast"] is True
        and row["result"]["dispatch"] == "start_over"
        and row["result"]["counted"] is False
    )
    client.cookies.clear()
    again = turn(client, {"action": "start", "text": "plumber in Austin", "idempotency_key": "i2"}).json()
    assert again["ok"] and again["result"]["data"]["business_id"] != biz
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 2


def test_a_reload_with_the_cookie_of_a_draft_started_over_says_its_answers_expired(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    """M1 (final review): a draft retired by a start over still holds its consultation, so a reload with its old cookie
    reads "expired"; "moved" is a hand-off landing's, which re-points the session to the owner's business."""
    biz = _walk(client, conn)
    visit = client.cookies.get("admiry_visit")
    assert _say(client, "start over") == {"ok": True, "start_over": True}
    got = turn(client, {"action": "state"}, Cookie=f"admiry_visit={visit}").json()
    assert got == {"ok": True, "fresh": True, "reason": "expired"}
    # Ruling 19: a superseded session is not held -- a draft that ran `start` twice, then landed, has moved
    conn.execute("UPDATE consult_session SET status = 'superseded' WHERE business_id = %s", (biz,))
    assert turn(client, {"action": "state"}, Cookie=f"admiry_visit={visit}").json()["reason"] == "moved"


def test_start_over_wins_at_a_words_only_question(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    """Review Focus 5: at the website question the phrase is a start over, never a site to read."""
    benchmark.seed(conn)
    turn(client, START)
    assert turn(client, {"action": "state"}).json()["result"]["next"]["question"]["id"] == "website"
    assert _say(client, "start over") == {"ok": True, "start_over": True}
    assert fake.turn_calls == []


def test_an_address_typed_on_the_name_question_is_rerouted_to_the_website_question_without_a_model_call(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    harness_on,
    crawled,  # noqa: F811 - fixture used by name
    monkeypatch,
):
    """Spec 2026-10-12 §3.4: the fast path reroutes an address in the words to `answer` for the website question; the
    web.turn row says dispatch answer, fast true; no Turn call is made."""
    # a draft whose site could not be read and whose open question is the name (crawled.empty, then ask_instead), as
    # test_an_address_in_the_name_answer_re_answers_the_website_question (test_tools_consult.py) builds its session
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist in Palo Alto", "idempotency_key": "i1"})
    crawled.empty = True
    turn(
        client,
        {"action": "answer", "answers": {"website": {"other": "https://brightsmile.example"}}, "idempotency_key": "w1"},
    )
    turn(client, {"action": "answer", "answers": {"website": {"choice": "ask_instead"}}, "idempotency_key": "w2"})
    assert turn(client, {"action": "state"}).json()["result"]["next"]["question"]["id"] == "business_name"
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    calls = len(fake.turn_calls)
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    r = turn(
        client, {"action": "say", "text": "https://brightsmile.example works fine for me. Why cannot you open it"}
    ).json()
    assert r["ok"] and len(fake.turn_calls) == calls and crawled.urls[-1] == "https://brightsmile.example"
    # the second fetch (not business_name's words recorded verbatim) is what tells the reroute from the pre-existing
    # words-only fast path apart -- both are fast and both dispatch to answer, but only the reroute reads the site again
    assert len(crawled.urls) == 2
    after = conn.execute("SELECT answers FROM consult_session WHERE business_id = %s", (biz,)).fetchone()["answers"]
    assert after["website"]["read"] is True and "business_name" not in after
    [row] = _rows(conn, biz)
    result = row["result"]
    assert (result["dispatch"], result["fast"], result["intent"]) == ("answer", True, "answer")


def test_a_claimed_business_starts_its_questions_over_and_keeps_its_cookies_and_its_business(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    google,
    harness_on,
):
    biz = _walk(client, conn)
    _claim(client, google)
    old = conn.execute("SELECT id FROM consult_session WHERE business_id = %s", (biz,)).fetchone()["id"]
    r = turn(client, {"action": "say", "text": "let's start over", "idempotency_key": "s1"})
    assert r.json() == {"ok": True, "start_over": True} and "set-cookie" not in r.headers
    assert conn.execute("SELECT status FROM consult_session WHERE id = %s", (old,)).fetchone()["status"] == "superseded"
    assert conn.execute("SELECT status FROM business WHERE id = %s", (biz,)).fetchone()["status"] == "active"
    assert turn(client, {"action": "state"}).json()["result"]["session"] is None
    again = turn(client, {"action": "start", "text": "dentist in Mountain View", "idempotency_key": "i2"}).json()
    assert again["ok"] and again["result"]["data"]["business_id"] == biz
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1


def test_a_paraphrased_start_over_below_the_bar_asks_back_and_a_yes_then_does_it(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    biz = _walk(client, conn)
    fake.turns["can we go back to the very beginning"] = turn_fixture(intent="start_over", confidence=0.7)
    r = _say(client, "can we go back to the very beginning")
    assert r["result"]["say"] == dispatch.CONFIRM_START_OVER and r["result"]["next"] == {"kind": "none"}  # M2
    assert conn.execute("SELECT status FROM business WHERE id = %s", (biz,)).fetchone()["status"] == "active"
    fake.turns["yes"] = turn_fixture(intent="start_over", confidence=0.95)
    assert _say(client, "yes", "s2") == {"ok": True, "start_over": True}
    recent = fake.turn_calls[-1]["material"]["recent"]
    assert recent[-1] == {"owner": "can we go back to the very beginning", "admiry": dispatch.CONFIRM_START_OVER}


def test_go_to_asks_an_earlier_question_again_and_its_answer_replaces_the_old(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    _walk(client, conn)
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    fake.turns["back to the goal question"] = turn_fixture(intent="go_to", question_id="objective")
    r = _say(client, "back to the goal question")
    q = r["result"]["next"]["question"]
    assert q["id"] == "objective" and q["index"] == 3 and r["result"]["say"].startswith("Of course.")
    a = turn(
        client, {"action": "answer", "answers": {"objective": {"choice": "calls"}}, "idempotency_key": "a2"}
    ).json()
    assert a["result"]["data"]["corrected"] == ["objective"] and a["result"]["next"]["question"]["id"] == "service"


def _answers(conn, biz):
    return conn.execute("SELECT status, answers FROM consult_session WHERE business_id = %s", (biz,)).fetchone()


def test_typed_words_under_a_question_asked_again_answer_it_and_replace_the_old(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    """C1 (final review): the page posts the id of the card it shows; after go_to that is the question asked again,
    so the words are read against it and recorded on it -- then the first unanswered question, as a correction is."""
    biz = _walk(client, conn)
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    fake.turns["back to the goal question"] = turn_fixture(intent="go_to", question_id="objective")
    assert _say(client, "back to the goal question")["result"]["next"]["question"]["id"] == "objective"
    fake.turns["the phone to ring"] = turn_fixture(intent="answer", option_id="calls")
    r = _say(client, "the phone to ring", "s2", question_id="objective")
    assert _answers(conn, biz)["answers"]["objective"]["choice"] == "calls"
    assert r["result"]["data"]["corrected"] == ["objective"] and r["result"]["next"]["question"]["id"] == "service"
    material = fake.turn_calls[-1]["material"]
    assert material["consultation"]["open_question"]["id"] == "objective" and "objective" not in material["may_go_to"]


def test_an_explanation_under_a_question_asked_again_draws_that_question_again(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    """Ruling 19: Admiry's reply under a question asked again asks that question again -- its card drawn, so the next
    words still carry its id -- never the first unanswered one."""
    _walk(client, conn)
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    fake.turns["back to the goal question"] = turn_fixture(intent="go_to", question_id="objective")
    assert _say(client, "back to the goal question")["result"]["next"]["question"]["id"] == "objective"
    fake.turns["what do you mean?"] = turn_fixture(intent="explain", say="It tells me what to plan the ads around.")
    r = _say(client, "what do you mean?", "s2", question_id="objective")
    assert r["result"]["next"]["question"]["id"] == "objective"


def test_at_ready_typed_words_under_a_question_asked_again_answer_it_and_the_plan_waits(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    google,
    harness_on,
):
    """C1 at ready: the question asked again is answered, never "say the word" with the old answer kept."""
    biz = _walk(client, conn)
    for qid, choice in (("objective", "bookings"), ("service", "svc_invisalign"), ("customer_value", "1000_plus")):
        turn(client, {"action": "answer", "answers": {qid: {"choice": choice}}, "idempotency_key": qid})
    _claim(client, google)
    fake.turns["back to the goal question"] = turn_fixture(intent="go_to", question_id="objective")
    assert _say(client, "back to the goal question")["result"]["next"]["question"]["id"] == "objective"
    fake.turns["the phone to ring"] = turn_fixture(intent="answer", option_id="calls")
    r = _say(client, "the phone to ring", "s2", question_id="objective")
    held = _answers(conn, biz)
    assert held["answers"]["objective"]["choice"] == "calls" and held["status"] == "ready"
    assert r["result"]["data"]["corrected"] == ["objective"] and r["result"]["next"] == dispatch.RECOMMEND


def test_a_question_id_off_the_menu_leaves_the_words_to_the_first_unanswered_question(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    """C1: an id the owner may not return to (the Google Maps place, settled once; an unknown one) is not the open
    question; the first unanswered one is. A question id that is not a string is a bad request."""
    biz = _walk(client, conn)  # the place picked: answered, but never on the menu
    fake.turns["the phone to ring"] = turn_fixture(intent="answer", option_id="calls")
    _say(client, "the phone to ring", question_id="listing")
    assert fake.turn_calls[-1]["material"]["consultation"]["open_question"]["id"] == "objective"
    assert _answers(conn, biz)["answers"]["objective"]["choice"] == "calls"
    fake.turns["mostly invisalign"] = turn_fixture(intent="answer", option_id="svc_invisalign")
    _say(client, "mostly invisalign", "s2", question_id="no_such")
    assert fake.turn_calls[-1]["material"]["consultation"]["open_question"]["id"] == "service"
    assert "service" in _answers(conn, biz)["answers"]
    bad = {"action": "say", "text": "x", "question_id": 5, "idempotency_key": "s3"}
    assert client.post("/api/turn", json=bad, headers=IP).status_code == 400


def test_budget_words_write_the_owners_limit_or_say_it_comes_with_the_plan(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    biz = _walk(client, conn)
    fake.turns["my budget is 500 a month"] = turn_fixture(intent="budget", monthly_usd=500)
    r = _say(client, "my budget is 500 a month")
    goal = business.latest_goal(conn, biz)
    assert (goal["monthly_limit_usd"], goal["limit_source"]) == (500, "owner")
    assert (
        r["result"]["say"].startswith("Noted: $500 a month.") and r["result"]["next"]["question"]["id"] == "objective"
    )
    fake.turns["go to the budget part"] = turn_fixture(intent="budget", monthly_usd=None)
    r = _say(client, "go to the budget part", "s2")
    assert r["result"]["say"] == dispatch.BUDGET_LATER and r["result"]["next"] == {"kind": "none"}
    assert business.latest_goal(conn, biz)["monthly_limit_usd"] == 500


def test_an_answer_after_admirys_ask_takes_the_ask_out_of_the_next_material(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    """I2 (final review): Admiry's budget ask is the words' context only until something is answered; after a tapped
    answer a later amount is no reply to it (no say is stored on a reroute's row either)."""
    _walk(client, conn)
    fake.turns["go to the budget part"] = turn_fixture(intent="budget", monthly_usd=None)
    _say(client, "go to the budget part")
    _say(client, "hmm", "s2")  # nothing answered since: the ask is live
    assert fake.turn_calls[-1]["material"]["recent"] == [
        {"owner": "go to the budget part", "admiry": dispatch.BUDGET_LATER}
    ]
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    _say(client, "about 500", "s3")
    assert fake.turn_calls[-1]["material"]["recent"] == []


def test_build_words_at_ready_hit_the_wall_for_a_draft_and_build_the_plan_once_claimed(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    google,
    harness_on,
):
    _walk(client, conn)
    for qid, choice in (("objective", "bookings"), ("service", "svc_invisalign"), ("customer_value", "1000_plus")):
        turn(client, {"action": "answer", "answers": {qid: {"choice": choice}}, "idempotency_key": qid})
    assert turn(client, {"action": "state"}).json()["result"]["session"]["status"] == "ready"
    fake.turns["ok build it"] = turn_fixture(intent="build")
    assert _say(client, "ok build it") == {"ok": False, "wall": True, "error": "Sign in to see your plan."}
    assert fake.turn_calls == []  # the wall: no model call (D10)
    _claim(client, google)
    r = _say(client, "ok build it", "s2")
    assert r["ok"] and r["result"]["data"]["cards"]["budget"]["monthly_usd"] == 2000
    assert len(fake.turn_calls) == 1


def test_build_words_before_ready_are_answered_with_the_question(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    _walk(client, conn)
    fake.turns["just build it"] = turn_fixture(intent="build")
    r = _say(client, "just build it")
    assert r["result"]["say"].startswith(dispatch.NOT_READY) and r["result"]["next"]["question"]["id"] == "objective"


@pytest.mark.parametrize("intent", ["explain", "ask", "smalltalk"])
def test_explain_ask_and_smalltalk_say_and_ask_the_question_again_recording_nothing(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    harness_on,
    intent,
):
    biz = _walk(client, conn)
    before = conn.execute("SELECT answers FROM consult_session WHERE business_id = %s", (biz,)).fetchone()["answers"]
    fake.turns["some words"] = turn_fixture(intent=intent, say="A sentence from the model.")
    r = _say(client, "some words")
    assert (
        r["result"]["say"].startswith("A sentence from the model.")
        and r["result"]["next"]["question"]["id"] == "objective"
    )
    assert (
        conn.execute("SELECT answers FROM consult_session WHERE business_id = %s", (biz,)).fetchone()["answers"]
        == before
    )
    [row] = _rows(conn, biz)
    assert row["result"]["counted"] is True and row["result"]["say"].startswith("A sentence from the model.")


def test_an_unclear_turn_asks_back(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    _walk(client, conn)
    r = _say(client, "asdf qwerty")  # the fake answers unclear at 0.3 for words it was given nothing for
    assert r["result"]["say"].startswith(dispatch.UNCLEAR_SAY) and r["result"]["next"]["question"]["id"] == "objective"


def test_skip_picks_any_on_a_choice_question_and_skips_the_services_in_words(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    biz = _walk(client, conn)
    fake.turns["no idea, you choose"] = turn_fixture(intent="skip")
    r = _say(client, "no idea, you choose")
    assert r["result"]["data"]["recorded"]["objective"]["choice"] == "any"
    assert _rows(conn, biz)[-1]["result"]["counted"] is True  # a {choice} reroute: answer's own row counts nothing
    client.cookies.clear()
    turn(client, {"action": "start", "text": "dentist in Palo Alto", "idempotency_key": "i2"})
    turn(client, {"action": "answer", "answers": {"website": {"choice": "none"}}, "idempotency_key": "w"})
    turn(client, {"action": "answer", "answers": {"business_name": {"other": "Bright Smile"}}, "idempotency_key": "n"})
    turn(
        client, {"action": "answer", "answers": {"listing": {"choice": "none"}}, "idempotency_key": "l"}
    )  # the fake's Places offers one
    assert turn(client, {"action": "state"}).json()["result"]["next"]["question"]["id"] == "services_offered"
    fake.turns["let's not go into that"] = turn_fixture(intent="skip")
    r = _say(client, "let's not go into that", "s2")
    assert r["result"]["data"]["recorded"]["services_offered"]["skipped"] is True and fake.read_calls == []


def test_rather_not_say_at_a_text_question_is_the_fast_path(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist in Palo Alto", "idempotency_key": "i1"})
    turn(client, {"action": "answer", "answers": {"website": {"choice": "none"}}, "idempotency_key": "w"})
    r = _say(client, "I'd rather not say")  # the name question, words only: today's skip, no call
    assert r["result"]["data"]["recorded"]["business_name"]["skipped"] is True and fake.turn_calls == []


def test_at_the_wall_only_the_fast_path_runs(client, conn, fake, harness_on, monkeypatch):  # noqa: F811 - fixture used by name
    monkeypatch.setenv("AGENT_ANON_TURNS", "2")
    _walk(client, conn)  # the site and the place answered: the wall is up, the objective open (D10)
    assert turn(client, {"action": "state"}).json()["visitor"]["wall"] is True
    fake.turns["what do you mean?"] = turn_fixture(intent="explain", say="x")
    assert _say(client, "what do you mean?") == {"ok": False, "wall": True, "error": "Sign in to see your plan."}
    assert fake.turn_calls == []
    assert _say(client, "start over", "s2") == {"ok": True, "start_over": True}


def test_with_the_switch_off_the_consultation_reads_words_as_before(client, conn, fake):  # noqa: F811 - fixture used by name
    _walk(client, conn)
    r = _say(client, "i wanna restart")
    assert r["ok"] and r["result"]["say"].startswith("I did not quite get that.")
    assert len(fake.read_calls) == 1 and fake.turn_calls == []
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.turn'").fetchone()["n"] == 0


def test_the_model_failing_is_the_owners_to_read_and_records_the_row(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    biz = _walk(client, conn)
    fake.error = "the assistant is busy; try again in a minute"
    r = _say(client, "some words")
    assert r == {"ok": False, "error": "the assistant is busy; try again in a minute"}
    [row] = _rows(conn, biz)
    assert row["ok"] is False and row["result"]["error"] and row["result"]["counted"] is True
    assert turn(client, {"action": "state"}).json()["result"]["next"]["question"]["id"] == "objective"


def test_empty_and_overlong_words_are_refused_before_any_call(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    _walk(client, conn)
    assert _say(client, "   ") == {"ok": False, "error": "Send a choice or a few words."}
    assert _say(client, "x" * 501, "s2") == {"ok": False, "error": "Keep it under 500 characters."}
    assert fake.turn_calls == []


def test_support_words_open_a_ticket_with_support_on_and_say_so_with_it_off(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    google,
    harness_on,
    support_on,
):
    biz = _walk(client, conn)
    _claim(client, google)  # a known contact address: the acknowledgement is sent, not left in awaiting_contact (D133)
    fake.turns["can I talk to a person"] = turn_fixture(
        intent="support", support="human", support_summary="Wants a person."
    )
    r = _say(client, "can I talk to a person")
    assert r["ok"] and r["result"]["data"]["support"]
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    assert any(m["subject"].startswith("We got your message") for m in support_on.sent)
    [row] = _rows(conn, biz)
    assert row["result"]["ticket_id"] == r["result"]["data"]["support"]["ticket_id"]


def test_support_words_with_support_off_say_so(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    _walk(client, conn)
    fake.turns["can I talk to a person"] = turn_fixture(
        intent="support", support="human", support_summary="Wants a person."
    )
    r = _say(client, "can I talk to a person")
    assert (
        r["result"]["say"].startswith(dispatch.SUPPORT_OFF_SAY) and r["result"]["next"]["question"]["id"] == "objective"
    )


def test_a_dispatch_envelope_whose_support_is_null_records_no_ticket(client, conn, fake, harness_on, monkeypatch):  # noqa: F811 - fixture used by name
    """The final review's ledger: an envelope whose data.support is null is no ticket -- read with `or {}`, never an
    error -- and the row records none."""
    biz = _walk(client, conn)
    monkeypatch.setattr(dispatch, "run", lambda *a, **kw: envelope.make({"support": None}, say="Noted."))
    r = _say(client, "some words")
    assert r["ok"] and r["result"]["say"] == "Noted."
    [row] = _rows(conn, biz)
    assert row["ok"] and "ticket_id" not in row["result"]


def test_not_mine_words_unlink_the_confirmed_place_and_ask_again(client, conn, fake, harness_on):  # noqa: F811 - fixture used by name
    biz = _walk(client, conn)  # PICK confirmed the place
    fake.turns["that listing is not my practice"] = turn_fixture(intent="not_mine", listing_not_mine=True)
    r = _say(client, "that listing is not my practice")
    assert (
        r["ok"]
        and "To link the right one" in r["result"]["say"]
        and r["result"]["next"]["question"]["id"] == "objective"
    )
    knowledge = business.latest_knowledge(conn, biz)[1]
    assert not (knowledge.get("listing") or {}).get("confirmed")


def test_a_turn_runs_under_the_keys_rate_limit(client, conn, fake, harness_on, monkeypatch):  # noqa: F811 - fixture used by name
    from agent import auth

    _walk(client, conn)
    monkeypatch.setattr(auth, "RATE_PER_MINUTE", 0)
    r = client.post("/api/turn", json={"action": "say", "text": "some words", "idempotency_key": "s1"}, headers=IP)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0 and fake.turn_calls == []
