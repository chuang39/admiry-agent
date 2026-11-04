"""The homepage's support paths (spec 2026-09-30 §4.2, §4.6): the support read on every free-text turn, a ticket in place
of an answer or an adjustment, the anonymous ask that waits for the sign-in, a ready consultation read first, the
/api/support endpoint (the dock's "Talk to a person" until the Help button replaced it, spec 2026-10-12 H4; the
endpoint stays for the MCP-era callers), and nothing of it while support is off."""

import re

import pytest

from agent import auth, benchmark, home_api, models, support, support_api, transcript
from agent.tests.test_home_api import (  # noqa: F401 - fake is a fixture used by name
    IP,
    PICK,
    SITE,
    _answer_all,
    fake,
    turn,
)
from agent.tests.test_mobile import phone_block, rule
from agent.tests.test_oauth_consent import _sign_in
from agent.tests.test_support import biz, open_
from agent.tests.test_transcript import planned

PERSON = "can I talk to a real person"
OPENING = "dentist within 5 miles, $2,000 a month"


def human(summary="Wants a person."):
    return models.SupportRead(support="human", support_summary=summary)


def to_plan(client, conn, google):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})


def test_an_anonymous_visitor_asks_for_a_person_mid_consultation(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    fake.reads[PERSON] = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=False,
        support="human",
        support_summary="x",
        services=[],
    )
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    # the website, then the business on Google Maps: words answering those questions are read without the model, so no
    # request for a person is looked for in them (plan 2026-10-03-agent-listing-confirm, Task 3 round 1; plan
    # 2026-10-05-agent-a3p-5-1, Q6); the next question's are
    turn(client, SITE)
    turn(client, PICK)
    r = turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json()
    env = r["result"]
    assert r["ok"] and env["data"]["support"]["status"] == "awaiting_contact" and env["next"]["kind"] == "question"
    assert env["say"].startswith(support.SAY_NEW_ANON)
    t = conn.execute("SELECT source, reason FROM support_ticket").fetchone()
    assert t == {"source": "home", "reason": "human"} and support_on.sent == []  # no address yet: nothing is sent
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_after_the_plan_a_request_for_a_person_takes_the_turn(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    fake.adjusts["this plan is useless"] = models.AdjustRead(
        monthly_budget_usd=None,
        level=None,
        radius_miles=None,
        offer=None,
        objective=None,
        channel=None,
        channel_monthly_usd=None,
        understood=False,
        asks_for_change=False,  # a complaint, not a change (the homepage reads only the support read here)
        support="dissatisfied",
        support_summary="Finds the plan useless.",
    )
    to_plan(client, conn, google)
    r = turn(client, {"action": "say", "text": "this plan is useless", "idempotency_key": "s2"}).json()
    assert r["ok"] and r["result"]["say"] == (
        support.SAY_NEW.format(email="owner@example.com") + " " + support.SAY_PLAN_TAIL
    )
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1  # no adjustment ran
    t = conn.execute("SELECT reason, summary FROM support_ticket").fetchone()
    assert t == {"reason": "dissatisfied", "summary": "Finds the plan useless."}
    assert len(support_on.sent) == 2


def test_a_long_message_after_the_plan_goes_to_read_support(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    to_plan(client, conn, google)
    long_words = " ".join(["I have been trying for an hour and nothing works on this page."] * 8)
    assert len(long_words) > home_api.SAY_ADJUST_MAX_CHARS
    fake.supports[long_words] = human("Stuck on the page.")
    r = turn(client, {"action": "say", "text": long_words, "idempotency_key": "s3"}).json()
    assert r["result"]["data"]["support"]["status"] == "open"
    assert fake.support_calls == [long_words] and fake.adjust_calls == []


def test_a_ready_consultation_reads_the_words_first(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    fake.supports[PERSON] = human()
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    r = turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["data"]["support"]["status"] == "awaiting_contact"
    assert r["result"]["next"] == {"kind": "link", "link": {"url": support.SIGN_IN_URL, "label": "Sign in with Google"}}
    assert turn(client, {"action": "say", "text": "sounds good", "idempotency_key": "s2"}).json() == home_api.WALL


def test_with_support_off_nothing_is_read_for_support(client, conn, fake, resend):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    fake.supports[PERSON] = human()
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    assert turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json() == home_api.WALL
    # the walk linked the place offered: the words are read for whether they call it not theirs, and for that alone
    # (plan 2026-10-03-agent-listing-not-mine-words, D4) -- an ask for a person opens nothing while support is off
    assert fake.support_calls == [PERSON]
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def test_api_support_still_answers_as_the_dock_s_link_did(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    none = client.post("/api/support", json={"text": "hello"}, headers=IP).json()
    assert none == {"ok": False, "signin": True, "error": support_api.SIGN_IN_FIRST}
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    r = client.post("/api/support", json={"text": "I need help with my account"}, headers=IP).json()
    assert r["ok"] and r["result"]["data"]["support"]["status"] == "awaiting_contact" and fake.support_calls == []
    t = conn.execute("SELECT reason, summary FROM support_ticket").fetchone()
    assert t == {"reason": "human", "summary": "I need help with my account"}
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.support'").fetchone()["n"] == 1
    _sign_in(client, google)  # a web session now: its CSRF token is required
    assert client.post("/api/support", json={"text": "again"}, headers=IP).status_code == 403


def test_the_link_refuses_bad_input_and_is_absent_while_support_is_off(client, conn, resend, monkeypatch):
    assert client.post("/api/support", json={"text": "x"}, headers=IP).status_code == 404
    monkeypatch.setenv("AGENT_SUPPORT", "on")
    for text in ("", "   ", "x" * (support_api.MAX_TEXT + 1), 5):
        assert client.post("/api/support", json={"text": text}, headers=IP).status_code == 400, text


def test_the_dock_has_no_talk_link_and_the_thumbs_script_loads_only_when_support_is_on(client, resend, monkeypatch):
    """Spec 2026-10-12 H4: "Talk to a person" is gone from the dock whatever the switch says (the Help button is the
    one entry, test_help_button); support.js and support.css still load for the plan thumbs while support is on."""
    html = client.get("/").text
    assert 'id="talk"' not in html and "Talk to a person" not in html and "/static/support.js" not in html
    monkeypatch.setenv("AGENT_SUPPORT", "on")
    html = client.get("/").text
    assert 'id="talk"' not in html and "Talk to a person" not in html
    assert "/static/support.js" in html and "/static/support.css" in html
    assert html.index("/static/support.js") < html.index("/static/home.js")  # AdmirySupport exists before home.js runs
    js = client.get("/static/support.js")
    assert js.status_code == 200 and "innerHTML" not in js.text and "eval(" not in js.text


def test_the_owner_of_the_visitors_business_is_acknowledged_by_e_mail(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    csrf = _sign_in(client, google)
    r = client.post("/api/support", json={"text": "I need a hand", "csrf": csrf}, headers=IP).json()
    assert r["ok"] and r["result"]["say"] == support.SAY_NEW.format(email="owner@example.com")
    assert r["result"]["next"] == {"kind": "none"}
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert conn.execute("SELECT business_id FROM support_ticket").fetchone() == {"business_id": biz}
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_a_signed_in_visitor_with_no_business_gets_one_ticket_for_their_address(client, conn, support_on, google):
    csrf = _sign_in(client, google)
    first = client.post("/api/support", json={"text": "first ask", "csrf": csrf}, headers=IP).json()
    assert first["ok"] and first["result"]["say"] == support.SAY_NEW.format(email="owner@example.com")
    t = conn.execute("SELECT business_id, contact_email, status FROM support_ticket").fetchone()
    assert t == {"business_id": None, "contact_email": "owner@example.com", "status": "open"}
    second = client.post("/api/support", json={"text": "second ask", "csrf": csrf}, headers=IP).json()
    assert second["ok"] and second["result"]["say"] == support.SAY_APPENDED
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    assert len(support_on.sent) == 2  # one ticket, one mail pair: the acknowledgement and the ops notice


def test_the_link_never_files_a_request_under_another_owners_business(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'someone@example.com'")
    csrf = _sign_in(client, google)  # owner@example.com, on a browser that holds someone else's business cookie
    assert client.post("/api/support", json={"text": "help me", "csrf": csrf}, headers=IP).json()["ok"]
    t = conn.execute("SELECT business_id, contact_email FROM support_ticket").fetchone()
    assert t == {"business_id": None, "contact_email": "owner@example.com"}


def test_a_read_that_cannot_run_leaves_the_reply_there_was_before(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    fake.error = "the assistant is busy; try again in a minute"
    assert turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json() == home_api.WALL
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0
    assert conn.execute("SELECT ok FROM agent_action WHERE tool = 'web.say'").fetchone() == {"ok": False}


# ---------------------------------------------------------------- the controller's rulings on the first review


def test_support_css_keeps_only_the_thumbs_phone_rule(client):
    """With the dock's link gone the dock is home.css's own height again: nothing of this sheet's about scroll padding
    or #talk; the one phone rule left hides the dock while the thumbs' comment is typed in (test_mobile's one-input
    rule)."""
    css = client.get("/static/support.css").text
    assert "#talk" not in css and ".talk" not in css and "scroll-padding-bottom" not in css
    assert "display: none" in rule(phone_block(css), "body:has(.rate-why textarea:focus) .dock")


# what support.css may style: the thumbs (.rate*) and the homepage's own dock, reached only through them
OWN = re.compile(r"\.rate|body:has\(\.rate-why ")


def test_support_css_reaches_no_page_but_its_own(client):
    """/plan/{id} loads this sheet for the thumbs (Task 9): a rule on `:root` or `main` would move that page too."""
    css = re.sub(r"/\*.*?\*/", "", client.get("/static/support.css").text, flags=re.DOTALL)
    selectors = [
        s.strip() for m in re.finditer(r"([^{}]+)\{[^{}]*\}", css) for s in re.split(r",(?![^()]*\))", m.group(1))
    ]
    assert [s for s in selectors if not OWN.match(s)] == []


def test_a_nul_byte_in_the_words_never_reaches_the_database(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    r = client.post("/api/support", json={"text": "help\x00 me"}, headers=IP)  # Postgres text and jsonb refuse a NUL
    assert r.status_code == 200 and r.json()["ok"]
    assert conn.execute("SELECT summary FROM support_ticket").fetchone() == {"summary": "help me"}
    assert conn.execute("SELECT body FROM ticket_event WHERE kind = 'owner_words'").fetchone() == {"body": "help me"}


def test_a_nul_byte_in_a_spoken_ask_never_reaches_the_database(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    words = "can I talk to a real\x00 person"
    fake.supports[words] = human()
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    r = turn(client, {"action": "say", "text": words, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["data"]["support"]["status"] == "awaiting_contact"
    assert conn.execute("SELECT body FROM ticket_event WHERE kind = 'owner_words'").fetchone() == {"body": PERSON}


def test_support_js_keeps_the_thumbs_and_nothing_of_the_link(client):
    js = client.get("/static/support.js").text
    assert "function rate(" in js and "return { rate };" in js
    assert "function talk(" not in js and "function say(" not in js and "/api/support" not in js


def test_a_signed_in_visitor_on_an_unclaimed_draft_is_answered_by_e_mail(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    csrf = _sign_in(client, google)  # signed in first, so the draft that `start` makes is unclaimed
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    assert conn.execute("SELECT claim_state FROM business").fetchone() == {"claim_state": "draft"}
    r = client.post("/api/support", json={"text": "I need a hand", "csrf": csrf}, headers=IP).json()
    assert r["ok"] and r["result"]["say"] == support.SAY_NEW.format(email="owner@example.com")
    assert r["result"]["next"] == {"kind": "none"}  # no sign-in link for someone who is signed in
    t = conn.execute("SELECT status, contact_email FROM support_ticket").fetchone()
    assert t == {"status": "open", "contact_email": "owner@example.com"}
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


# ---------------------------------------------------------------- the controller's rulings on the second review


def test_a_signed_in_visitor_s_next_ask_opens_the_ticket_that_was_waiting(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """The reviewer's probe: an anonymous ask waits for an address; the visitor signs in through the header (not the
    wall, so the draft stays unclaimed) and asks again. The address opens the waiting ticket."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    first = client.post("/api/support", json={"text": "I need help with my account"}, headers=IP).json()
    ticket_id = first["result"]["data"]["support"]["ticket_id"]
    assert first["result"]["data"]["support"]["status"] == "awaiting_contact" and support_on.sent == []
    csrf = _sign_in(client, google)
    assert conn.execute("SELECT claim_state FROM business").fetchone() == {"claim_state": "draft"}
    second = client.post("/api/support", json={"text": "I signed in, please reply", "csrf": csrf}, headers=IP).json()
    assert second["ok"] and second["result"]["data"]["support"] == {
        "ticket_id": ticket_id,
        "status": "open",
        "appended": True,
    }
    # the spec's sentence for an append (§4.6): it carries no address, and no longer asks a signed-in visitor to sign in
    assert second["result"]["say"] == support.SAY_APPENDED and second["result"]["next"] == {"kind": "none"}
    t = conn.execute("SELECT status, contact_email FROM support_ticket").fetchone()  # still the one ticket
    assert t == {"status": "open", "contact_email": "owner@example.com"}
    assert conn.execute("SELECT body FROM ticket_event WHERE kind = 'ack'").fetchone() == {
        "body": "I signed in, please reply"  # the words that came with the address
    }
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


# ---------------------------------------------------------------- the finish check (Task 12): every flush is pinned


def test_a_signed_in_owner_on_a_ready_consultation_is_answered_by_e_mail(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """The homepage turn's own flush after a ready consultation's support read (`turn`, after `_say_support`): the owner's
    address is known, so the acknowledgement and the ops notice are in the outbox and go out once the turn has
    committed. The anonymous ask above sends nothing (its ticket waits for the claim), so no other test here needed that
    flush; without it the mail would wait for the hourly support-mail job."""
    benchmark.seed(conn)
    fake.supports[PERSON] = human()
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)
    r = turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == support.SAY_NEW.format(email="owner@example.com")
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


# ---------------------------------------------------------------- the final review


def at_the_minute_limit(conn) -> None:
    """The visitor's key -- the only key there is -- has made auth.RATE_PER_MINUTE calls in the last minute."""
    (key,) = conn.execute("SELECT id FROM api_key").fetchall()
    conn.execute(
        "INSERT INTO agent_action (actor, api_key_id, tool, args_hash) "
        "SELECT %s, %s, 'state', 'x' FROM generate_series(1, %s)",
        (f"key:{key['id']}", key["id"], auth.RATE_PER_MINUTE),
    )


def test_the_link_counts_toward_the_visitor_key_s_limits(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    """The link is one of the homepage's calls: a visitor key at its per-minute limit gets the homepage's 429, with the
    seconds to wait, and nothing is written -- no ticket, no audit row."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    at_the_minute_limit(conn)
    rows = conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]
    r = client.post("/api/support", json={"text": "I need help with my account"}, headers=IP)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json() == {"ok": False, "error": "Too many requests; try again in a moment."}
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == rows


def test_the_link_after_the_plan_also_offers_a_change(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Spec §4.6: at the planned stage the acknowledgement also says a change to the plan is one message away -- the
    link's as well as a typed ask's (test_after_the_plan_a_request_for_a_person_takes_the_turn)."""
    _, csrf = planned(client, conn, google)
    r = client.post("/api/support", json={"text": "I need a hand", "csrf": csrf}, headers=IP).json()
    said = support.SAY_NEW.format(email="owner@example.com") + " " + support.SAY_PLAN_TAIL
    assert r["ok"] and r["result"]["say"] == said


def test_an_ask_that_opened_no_ticket_keeps_its_words_in_the_snapshot(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    """The read asked for a person, but the visitor's address had opened its tickets for the day (support.Limited): no
    ticket took the words, so the snapshot keeps them as the owner's turn."""
    for n in range(support.ANON_PER_IP_DAY):  # the address's other drafts opened the day's tickets
        biz(conn, f"draft_{n}", claim_state="draft", owner=None)
        open_(conn, business_id=f"draft_{n}", draft=True, ip=IP["X-Forwarded-For"])
    benchmark.seed(conn)
    fake.supports[PERSON] = human()
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    assert turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json() == {
        "ok": False,
        "error": support.LIMITED,
    }
    visitor = conn.execute("SELECT business_id FROM consult_session").fetchone()["business_id"]
    owner = [t["text"] for t in transcript.build(conn, visitor)["turns"] if t["who"] == "owner"]
    assert owner[-1] == PERSON


def test_an_ask_a_ticket_took_is_the_ticket_s_turn_alone(client, conn, fake, support_on):  # noqa: F811 - fixture used by name
    """The turn's web.say row names the ticket that took the words, so the snapshot shows them once: the ticket's."""
    benchmark.seed(conn)
    fake.supports[PERSON] = human()
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    assert turn(client, {"action": "say", "text": PERSON, "idempotency_key": "s1"}).json()["ok"]
    ticket = conn.execute("SELECT id FROM support_ticket").fetchone()["id"]
    row = conn.execute("SELECT result FROM agent_action WHERE tool = 'web.say'").fetchone()["result"]
    assert (row["support"], row["ticket_id"], row["text"]) == ("human", ticket, PERSON)
    visitor = conn.execute("SELECT business_id FROM consult_session").fetchone()["business_id"]
    owner = [t["text"] for t in transcript.build(conn, visitor)["turns"] if t["who"] == "owner"]
    assert owner.count(PERSON) == 1 and owner[-1] == PERSON


@pytest.mark.parametrize("csrf", [123, ["t"], "é"])
def test_a_csrf_that_is_not_a_token_is_refused_not_a_crash(client, conn, support_on, google, csrf):
    """A JSON body can carry any value as its csrf: anything but the session's token is a 403, never a 500."""
    _sign_in(client, google)
    r = client.post("/api/support", json={"text": "help me", "csrf": csrf}, headers=IP)
    assert r.status_code == 403 and conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0
