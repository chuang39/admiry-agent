import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import (
    ads_run,
    benchmark,
    business,
    config,
    consult,
    crawl,
    google_ads_link,
    home_api,
    landing,
    listings,
    llm,
    models,
    places,
    profile,
    proposals,
    site_install,
    tools_account,
    tools_hosting,
    tools_start,
)
from agent.tests.conftest import JARGON
from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture
from agent.tests.test_oauth_consent import _sign_in

IP = {"X-Forwarded-For": "198.51.100.9"}
# the ways to Admiry's code the walks' plans offer (A3p-5 batch 2 Q6): support is off in these tests, so without
# Admiry's team (ruling R3-f)
WAYS = [way for way in site_install.WAYS if way["id"] != "install_help"]
FRESH_EXPECTED = home_api.FRESH


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(
        places,
        "search",
        # the business as Google Maps lists it, close by its website and phone: the first question offers it, and the
        # walk's pick centres the ads (plan 2026-10-03-agent-listing-confirm)
        lambda q, k, **kw: [
            places.Candidate(
                "ChIJ1",
                "Bright Smile Dental",
                "500 University Ave, Palo Alto, CA",
                37.4419,
                -122.143,
                website="https://brightsmile.example",
                phone="(650) 555-0100",
            )
        ],
    )
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    return gen


# The visitor's answer to the first question, the website, when the opening words name none (plan
# 2026-10-05-agent-a3p-5-1, Q15): the fixture site, which the fakes crawl and read as the dental fixture -- the
# consultation starts again from it, in place, and what follows is the walk as it was
SITE = {"action": "answer", "answers": {"website": {"other": "https://brightsmile.example"}}, "idempotency_key": "site"}
# The visitor's answer to the question after it: the business is the place offered first (plan
# 2026-10-03-agent-listing-confirm)
PICK = {"action": "answer", "answers": {"listing": {"choice": "gm_1"}}, "idempotency_key": "pick"}


def turn(client, body, **headers):
    r = client.post("/api/turn", json=body, headers={**IP, **headers})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r


def test_start_sets_the_visitor_cookie_and_returns_the_first_question(client, conn, fake):
    benchmark.seed(conn)
    r = turn(
        client, {"action": "start", "text": "dentist in Palo Alto, more Invisalign patients", "idempotency_key": "i1"}
    )
    body = r.json()
    assert (
        body["ok"]
        and body["result"]["next"]["kind"] == "question"
        and body["visitor"]
        == {
            "signed_in": False,
            "claimed": False,
            "session_id": body["result"]["data"]["session_id"],
            "wall": False,
            "business_id": body["result"]["data"]["business_id"],
        }
    )
    assert "draft_key" not in json.dumps(body) or body["result"]["data"]["draft_key"] is None
    cookie = r.headers["set-cookie"]
    assert (
        cookie.startswith("admiry_visit=")
        and "HttpOnly" in cookie
        and "samesite=lax" in cookie.lower()
        and "Path=/api" in cookie
    )
    assert "Secure" not in cookie  # http://testserver; a test with AGENT_PUBLIC_BASE_URL=https://… asserts Secure
    assert not JARGON.search(body["result"]["say"])
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE class = 'draft'").fetchone()["n"] == 1


def test_the_homepage_asks_which_place_is_the_business_and_takes_the_pick(client, conn, fake):
    benchmark.seed(conn)
    first = turn(
        client, {"action": "start", "text": "dentist in Palo Alto, more Invisalign patients", "idempotency_key": "i1"}
    ).json()
    assert first["result"]["next"]["question"]["id"] == "website"  # the words name no website: it is asked first
    sited = turn(client, SITE).json()
    q = sited["result"]["next"]["question"]
    assert q["id"] == "listing" and [o["id"] for o in q["options"]] == ["gm_1", "none"]
    picked = turn(client, PICK).json()
    assert picked["result"]["say"].startswith("Linked to Bright Smile Dental on Google Maps.")


def test_answer_recommend_and_adjust_go_through_the_tools_and_the_wall_sits_before_the_plan(client, conn, fake, google):
    benchmark.seed(conn)
    turn(
        client,
        {"action": "start", "text": "dentist in Palo Alto within 5 miles, $2,000 a month", "idempotency_key": "i1"},
    )
    turn(client, SITE)
    turn(client, PICK)
    a = turn(
        client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"}
    ).json()
    assert a["ok"] and a["result"]["next"]["question"]["id"] == "service" and a["visitor"]["wall"] is False
    turn(client, {"action": "answer", "answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "a2"})
    b = turn(
        client, {"action": "answer", "answers": {"customer_value": {"choice": "1000_plus"}}, "idempotency_key": "a3"}
    ).json()
    assert b["result"]["data"]["ready"] is True and b["visitor"]["wall"] is True
    w = turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()
    assert w == {"ok": False, "wall": True, "error": "Sign in to see your plan."}
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 0
    # a claimed business with its owner signed in (simulate the wall's sign-in by claiming directly) passes
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)
    r = turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()
    assert r["ok"] and r["result"]["data"]["cards"]["budget"]["monthly_usd"] == 2000 and r["visitor"]["claimed"] is True
    j = turn(client, {"action": "adjust", "level": "conservative", "idempotency_key": "j1"}).json()
    assert j["ok"] and j["result"]["data"]["cards"]["budget"]["monthly_usd"] == 900
    s = turn(client, {"action": "state"}).json()
    assert (
        s["ok"]
        and s["result"]["session"]["status"] == "planned"
        and s["result"]["cards"]["budget"]["monthly_usd"] == 900
    )


def test_actions_without_a_cookie_and_bad_input(client, conn, fake):
    r = turn(client, {"action": "answer", "answers": {}, "idempotency_key": "x"}).json()
    assert r == {"ok": False, "error": "Start by telling me what you want.", "fresh": True}
    assert client.post("/api/turn", json={"action": "fly"}, headers=IP).status_code == 400
    assert (
        client.post("/api/turn", data="not json", headers={**IP, "Content-Type": "application/json"}).status_code == 400
    )


def test_tool_errors_and_rate_limits_are_readable(client, conn, fake):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    r = turn(client, {"action": "answer", "answers": {"colour": {"choice": "blue"}}, "idempotency_key": "a1"}).json()
    assert r["ok"] is False and "objective" in r["error"]
    for i in range(3):
        client.cookies.clear()  # a fresh visitor each time: exercise the anonymous per-IP path, not the keyed one
        client.post(
            "/api/turn",
            json={"action": "start", "text": "dentist", "idempotency_key": f"s{i}"},
            headers={"X-Forwarded-For": "198.51.100.77"},
        )
    client.cookies.clear()
    r = client.post(
        "/api/turn",
        json={"action": "start", "text": "dentist", "idempotency_key": "s9"},
        headers={"X-Forwarded-For": "198.51.100.77"},
    )
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_a_second_start_from_the_same_visitor_reuses_the_business_and_key(client, conn, fake):
    benchmark.seed(conn)
    first = turn(client, {"action": "start", "text": "dentist in Palo Alto", "idempotency_key": "i1"}).json()
    second = turn(client, {"action": "start", "text": "dentist in Mountain View", "idempotency_key": "i2"}).json()
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE class = 'draft'").fetchone()["n"] == 1
    first_sid = first["result"]["data"]["session_id"]
    second_sid = second["result"]["data"]["session_id"]
    assert first_sid != second_sid
    assert conn.execute("SELECT status FROM consult_session WHERE id = %s", (first_sid,)).fetchone()["status"] == (
        "superseded"
    )
    assert second["result"]["data"]["draft_key"] is None


def test_state_with_a_dead_key_resets_the_visitor(client, conn, fake):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    conn.execute("UPDATE api_key SET expires_at = now() - interval '1 minute'")
    r = turn(client, {"action": "state"})
    assert r.json() == {"ok": True, "fresh": True, "reason": "expired"}  # the page says the answers expired
    assert "admiry_visit=;" in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    client.cookies.clear()
    assert turn(client, {"action": "state"}).json() == {"ok": True, "fresh": True}  # a first visit: nothing to explain


def test_anon_turns_wall_blocks_the_next_answer(client, conn, fake, monkeypatch):
    benchmark.seed(conn)
    monkeypatch.setenv("AGENT_ANON_TURNS", "1")
    turn(client, {"action": "start", "text": "dentist in Palo Alto within 5 miles", "idempotency_key": "i1"})
    a = turn(client, SITE).json()  # the first question: the website
    assert a["ok"] and a["visitor"]["wall"] is True
    b = turn(
        client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a2"}
    ).json()
    assert b == {"ok": False, "wall": True, "error": "Sign in to see your plan."}
    sid = a["result"]["data"]["session_id"]
    assert (
        "objective"
        not in conn.execute("SELECT answers FROM consult_session WHERE id = %s", (sid,)).fetchone()["answers"]
    )
    # the same at the wall in the visitor's own words: refused before any model call, nothing recorded
    before = conn.execute("SELECT answers FROM consult_session WHERE id = %s", (sid,)).fetchone()["answers"]
    c = turn(client, {"action": "say", "text": "bookings, mostly", "idempotency_key": "s1"}).json()
    assert c == {"ok": False, "wall": True, "error": "Sign in to see your plan."}
    answers = conn.execute("SELECT answers FROM consult_session WHERE id = %s", (sid,)).fetchone()["answers"]
    assert answers == before and "objective" not in answers  # no objective, no free-text retry counter
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'answer'").fetchone()["n"] == 1
    assert turn(client, {"action": "state"}).json()["result"]["next"]["question"]["id"] == "listing"


def test_with_headers_override_feeds_request_headers():
    from agent import auth

    with auth.with_headers({"Authorization": "Bearer ak_x", "X-Forwarded-For": "1.2.3.4"}):
        h = auth.request_headers()
        assert h["authorization"] == "Bearer ak_x" and auth.client_ip(h) == "1.2.3.4"
    assert auth.request_headers() == {}


def test_claim_after_sign_in_upgrades_the_visitor_key_and_lifts_the_wall(client, conn, fake, google):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    assert turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()["wall"] is True
    csrf = _sign_in(client, google)
    assert client.get("/api/csrf").json()["csrf"] == csrf
    r = client.post("/api/claim", json={"csrf": csrf}, headers=IP)
    assert (
        r.status_code == 200
        and r.json()["ok"]
        and r.json()["visitor"]["claimed"] is True
        and r.json()["visitor"]["wall"] is False
    )
    assert "Max-Age=2592000" in r.headers["set-cookie"]
    biz = conn.execute("SELECT owner_email, claim_state FROM business").fetchone()
    assert (biz["owner_email"], biz["claim_state"]) == ("owner@example.com", "claimed")
    assert conn.execute("SELECT class, expires_at FROM api_key").fetchone()["class"] == "business"
    rec = turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()
    assert rec["ok"] and rec["result"]["data"]["cards"]["budget"]["monthly_usd"] == 2000


def test_claim_refuses_a_draft_owned_by_another_account(client, conn, fake, google):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    conn.execute("UPDATE business SET owner_email = 'other@example.com', claim_state = 'claimed'")
    csrf = _sign_in(client, google)
    r = client.post("/api/claim", json={"csrf": csrf}, headers=IP)
    assert r.status_code == 409 and "another" in r.json()["error"].lower()
    biz = conn.execute("SELECT owner_email, claim_state FROM business").fetchone()
    assert (biz["owner_email"], biz["claim_state"]) == ("other@example.com", "claimed")  # nothing changed
    assert conn.execute("SELECT class FROM api_key").fetchone()["class"] == "draft"  # the key was not upgraded


def test_claim_needs_a_session_a_cookie_and_the_csrf_token(client, conn, fake, google):
    assert client.post("/api/claim", json={"csrf": "x"}, headers=IP).status_code == 401
    csrf = _sign_in(client, google)
    assert (
        client.post("/api/claim", json={"csrf": csrf}, headers=IP).json() == FRESH_EXPECTED
    )  # define from home_api.FRESH
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    assert client.post("/api/claim", json={"csrf": "bad"}, headers=IP).status_code == 403


def test_safe_next_accepts_the_after_signin_path():
    from agent import sessions

    assert sessions.safe_next("/?after=signin") == "/?after=signin"


def test_free_text_answers_the_open_question_or_adjusts_the_plan(client, conn, fake, monkeypatch, google, pictures):
    from agent import models

    benchmark.seed(conn)
    fake.reads["the phone to ring"] = models.AnswerRead(
        option_id="calls", amount_usd=None, miles=None, note=None, understood=True, services=[]
    )
    none = {"monthly_budget_usd": None, "level": None, "radius_miles": None, "offer": None, "objective": None}
    none |= {"channel": None, "channel_monthly_usd": None, "asks_for_change": True}  # the homepage ignores the last
    fake.adjusts = {
        "make it fifteen hundred": models.AdjustRead(**{**none, "monthly_budget_usd": 1500}, understood=True),
        "put $300 on newsbreak": models.AdjustRead(
            **{**none, "channel": "newsbreak", "channel_monthly_usd": 300}, understood=True
        ),
    }
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    turn(client, SITE)
    turn(client, PICK)  # the business on Google Maps first: the words then answer the question after it
    a = turn(client, {"action": "say", "text": "the phone to ring", "idempotency_key": "s1"}).json()
    assert a["ok"] and a["result"]["data"]["recorded"]["objective"]["choice"] == "calls"
    for qid, choice in (("service", "svc_invisalign"), ("customer_value", "1000_plus")):
        turn(client, {"action": "answer", "answers": {qid: {"choice": choice}}, "idempotency_key": qid})
    # a prepaid business with NewsBreak's switch on (final review I2) and a picture for its trade (M1 Task 7 fix
    # round 1, `pictures`): the one kind whose plan can have NewsBreak
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com', ads_mode = 'managed'")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    j = turn(client, {"action": "say", "text": "make it fifteen hundred", "idempotency_key": "s2"}).json()
    assert j["ok"] and j["result"]["data"]["cards"]["budget"]["monthly_usd"] == 1500
    # one channel's amount, in the owner's words: adjust's channel pin, the rest split around it (M0 Task 5)
    k = turn(client, {"action": "say", "text": "put $300 on newsbreak", "idempotency_key": "s3"}).json()
    rows = k["result"]["data"]["cards"]["plan"]["channels"]
    assert k["ok"] and [(r["platform"], r["monthly_usd"]) for r in rows] == [
        ("google_search", 1200),
        ("newsbreak", 300),
    ]


def test_free_text_after_the_plan_that_is_not_an_adjustment(client, conn, fake, google):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    r = turn(client, {"action": "say", "text": "what colour is the sky", "idempotency_key": "s3"}).json()
    assert (
        r["ok"] and r["result"]["say"].startswith("I can change the budget") and r["result"]["next"] == {"kind": "none"}
    )
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1


def _answer_all(client):
    """Every question the homepage walk asks: the website (SITE), then the business on Google Maps (it centres the
    ads), then the trade's."""
    turn(client, SITE)
    for qid, choice in (
        ("listing", "gm_1"),
        ("objective", "bookings"),
        ("service", "svc_invisalign"),
        ("customer_value", "1000_plus"),
    ):
        turn(client, {"action": "answer", "answers": {qid: {"choice": choice}}, "idempotency_key": qid})


# ---------------------------------------------------------------- "not my business" in the owner's own words
# (plan 2026-10-03-agent-listing-not-mine-words, Task 2)

NOT_MINE = "that place on the map is not my business"
UNLINKED = listings.UNLINKED.format(name="Bright Smile Dental")
# the walk's pick centred the ads, and the centre goes with the listing: before the plan the consultation asks where
# they centre again; after it, none will, and a new version centres on the areas the business serves (Task 2 round 1)
ASKED_AGAIN_SAY = UNLINKED + listings.NO_CENTRE + listings.LINK_AGAIN
# ... when another question comes before it, the sentence says it will be asked (final review M2)
ASKED_LATER_SAY = UNLINKED + listings.NO_CENTRE_ASKED + listings.LINK_AGAIN
AFTER_PLAN_SAY = UNLINKED + listings.NO_CENTRE_LATER + listings.LINK_AGAIN


def _adjust_read(**fields) -> models.AdjustRead:
    """A read of the owner's words after the plan that names no change but what it is given."""
    blank = {"monthly_budget_usd": None, "level": None, "radius_miles": None, "offer": None, "objective": None}
    blank |= {"channel": None, "channel_monthly_usd": None, "understood": False, "asks_for_change": False}
    return models.AdjustRead(**{**blank, **fields})


def _listing_on_file(conn) -> dict | None:
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    return profile.business_listing(business.latest_knowledge(conn, biz)[1])


def _actions(conn, *tools) -> list[tuple[str, bool]]:
    rows = conn.execute("SELECT tool, ok FROM agent_action WHERE tool = ANY(%s) ORDER BY ts, id", (list(tools),))
    return [(r["tool"], r["ok"]) for r in rows]


def _signed_in_ready(client, conn, google) -> None:
    """The walk answered, the place offered picked (linked, the ads centred on it), the business claimed and its owner
    signed in: a ready consultation, no plan yet."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)


def test_after_the_plan_words_that_say_the_place_is_not_theirs_unlink_it(client, conn, fake, google):
    """D4/D5: after the plan the homepage runs the command every unlink runs -- update_business with google_listing
    null, its row the one the activity log reads -- and no adjustment; the reply is the unlink's sentences and how to
    link the right place. The same words again find nothing linked: not an adjustment, as before -- this walk builds
    no page, so the checklist still holds NO_PAGE (review Finding 2 / ruling R11): the in-place reply, not "continue
    in your assistant"."""
    _signed_in_ready(client, conn, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    assert _listing_on_file(conn)["place_id"] == "ChIJ1"  # the walk picked it
    fake.adjusts[NOT_MINE] = _adjust_read(listing_not_mine=True)
    r = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == AFTER_PLAN_SAY and r["result"]["next"] == {"kind": "none"}
    assert r["result"]["data"] == {"listing": "unlink", "google_listing": None}
    assert _listing_on_file(conn) is None
    assert _actions(conn, "web.say", "update_business", "adjust") == [("web.say", True), ("update_business", True)]
    row = conn.execute(
        "SELECT actor, actor_class, tool, result, ok, args_redacted FROM agent_action WHERE tool = 'update_business'"
    ).fetchone()
    assert json.loads(row["args_redacted"]["patch"]) == {"google_listing": None}  # the audit keeps it as text
    assert tools_account.sentence(row, None).endswith("unlinked the business's Google Maps listing")
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1  # no new version
    again = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s2"}).json()
    assert again["ok"] and again["result"]["say"] == home_api.NOT_AN_ADJUSTMENT_HERE
    assert _actions(conn, "update_business") == [("update_business", True)]


def test_after_the_plan_a_change_named_with_those_words_is_not_made_and_the_reply_says_so(client, conn, fake, google):
    """As update_marketing_plan's `message` (Task 1 fix round 1): the unlink takes the place of the change the same
    words named, and the reply asks for it again."""
    _signed_in_ready(client, conn, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    words = "that place is not my business, and make it fifteen hundred"
    fake.adjusts[words] = _adjust_read(listing_not_mine=True, monthly_budget_usd=1500, understood=True)
    r = turn(client, {"action": "say", "text": words, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == AFTER_PLAN_SAY + listings.NOTHING_ELSE
    assert _actions(conn, "adjust") == [] and conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1


def test_after_the_plan_a_listing_unlinked_meanwhile_reads_as_a_reply(client, conn, fake, google, monkeypatch):
    """The race (Task 2 round 1): Settings' "Not my business" lands after the turn saw the listing and before the
    command runs. The command finds nothing linked and changes nothing; the turn answers as change_plan does -- that
    nothing is linked and how to link the right place -- a reply, not an error."""
    _signed_in_ready(client, conn, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    real = home_api.listing_linked

    def seen_then_unlinked(c, biz_id):
        linked = real(c, biz_id)
        listings.unlink_listing(conn, biz_id)  # another path's unlink, between the check and the command
        return linked

    monkeypatch.setattr(home_api, "listing_linked", seen_then_unlinked)
    fake.adjusts[NOT_MINE] = _adjust_read(listing_not_mine=True)
    version = business.latest_knowledge(conn, conn.execute("SELECT id FROM business").fetchone()["id"])[0]
    r = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == listings.NOTHING_LINKED + listings.LINK_AGAIN, r
    assert r["result"]["next"] == {"kind": "none"}
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert business.latest_knowledge(conn, biz)[0] == version + 1  # the other path's unlink, none of the command's
    assert _actions(conn, "update_business", "adjust") == [("update_business", False)]  # refused: nothing to unlink


def test_after_the_plan_a_complaint_never_unlinks_the_listing(client, conn, fake, google):
    """Review focus 1: a read that sets only `support: dissatisfied` is no listing (Admiry's support off, the
    default). This walk builds no page, so the checklist still holds NO_PAGE (review Finding 2 / ruling R11): the
    in-place reply, not "continue in your assistant"."""
    _signed_in_ready(client, conn, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    fake.adjusts["this plan is not what I wanted"] = _adjust_read(support="dissatisfied", support_summary="unhappy")
    r = turn(client, {"action": "say", "text": "this plan is not what I wanted", "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == home_api.NOT_AN_ADJUSTMENT_HERE
    assert _listing_on_file(conn)["place_id"] == "ChIJ1" and _actions(conn, "update_business") == []


def test_an_open_question_met_with_those_words_unlinks_and_is_asked_again(client, conn, fake):
    """The homepage hands free text on an open consultation to `answer`, which unlinks (Task 1): pinned end to end."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    turn(client, SITE)
    turn(client, PICK)
    fake.reads[NOT_MINE] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=False, listing_not_mine=True, services=[]
    )
    r = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"}).json()
    nxt = r["result"]["next"]
    assert r["ok"] and r["result"]["data"]["listing"] == "unlink" and nxt["question"]["id"] == "objective"
    assert r["result"]["say"] == f"{ASKED_LATER_SAY} {consult.question_sentence(nxt)}"
    assert _listing_on_file(conn) is None and r["result"]["data"]["recorded"] == {}


def test_a_ready_consultation_reads_the_words_when_a_listing_is_linked(client, conn, fake, google):
    """D4: on a ready consultation of a business with a listing its owner confirmed, the words are read -- with
    Admiry's support off too: one read_support call under the cap, its web.say row -- and the flag unlinks it; the
    centre was the listing's own, so the consultation asks where again, as the answer path asks its question again
    (Task 2 round 1): after the sentences, and as the reply's next. Words that say nothing of it: READY_SAY."""
    _signed_in_ready(client, conn, google)
    s = turn(client, {"action": "say", "text": "sounds good", "idempotency_key": "s0"}).json()
    assert s["ok"] and s["result"]["say"] == home_api.READY_SAY and fake.support_calls == ["sounds good"]
    fake.supports[NOT_MINE] = models.SupportRead(support="none", support_summary=None, listing_not_mine=True)
    r = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"}).json()
    nxt = r["result"]["next"]
    assert r["ok"] and nxt["kind"] == "question" and nxt["question"]["id"] == "location"
    assert r["result"]["say"] == f"{ASKED_AGAIN_SAY} {consult.question_sentence(nxt)}"
    assert fake.support_calls == ["sounds good", NOT_MINE] and _listing_on_file(conn) is None
    assert _actions(conn, "web.say", "update_business") == [
        ("web.say", True),
        ("web.say", True),
        ("update_business", True),
    ]
    session = consult.get(conn, conn.execute("SELECT id FROM business").fetchone()["id"], r["visitor"]["session_id"])
    assert session["status"] == "open" and consult.next_question(session) == nxt


def test_a_ready_consultation_that_stays_ready_keeps_build_my_plan(client, conn, fake, google):
    """Task 2 round 1: the centre was a place the owner put on the map, so it stays and the consultation stays ready --
    the reply keeps the ready stage's next step, Build my plan, not none."""
    from agent import tools_consult

    _signed_in_ready(client, conn, google)
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    knowledge = business.latest_knowledge(conn, biz)[1]
    moved = {**knowledge, "listing": {**knowledge["listing"], "placed": True}}  # as listings.link keeps a placed centre
    business.add_knowledge(conn, biz, moved, source="owner")
    fake.supports[NOT_MINE] = models.SupportRead(support="none", support_summary=None, listing_not_mine=True)
    r = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == UNLINKED + listings.LINK_AGAIN  # the centre stays: nothing to say of it
    assert r["result"]["next"] == tools_consult.RECOMMEND_ACTION
    assert consult.open_for(conn, biz)["status"] == "ready" and _listing_on_file(conn) is None


def test_a_ready_consultation_without_a_linked_listing_is_not_read(client, conn, fake, google):
    """D4: nothing linked (unlinked already, as Settings' "Not my business" does) -- no model call, READY_SAY."""
    _signed_in_ready(client, conn, google)
    listings.unlink_listing(conn, conn.execute("SELECT id FROM business").fetchone()["id"])
    fake.supports[NOT_MINE] = models.SupportRead(support="none", support_summary=None, listing_not_mine=True)
    r = turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"}).json()
    assert r["ok"] and r["result"]["say"] == home_api.READY_SAY
    assert fake.support_calls == [] and _actions(conn, "web.say", "update_business") == []


SIGNIN = {"ok": False, "error": "Sign in to continue.", "signin": True}


def test_sign_out_ends_the_claimed_visitor(client, conn, fake, google):
    """I1 (final review): after the wall's claim the visitor key is a lasting business key; signing out
    deletes both visitor cookies, and a copy of the old cookie without the owner's web session opens nothing."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    visit = client.cookies.get("admiry_visit")
    out = client.post("/connect/signout", data={"csrf": csrf}, follow_redirects=False)
    assert out.status_code == 303
    deleted = out.headers.get_list("set-cookie")
    for name, path in (("admiry_visit", "/api"), ("admiry_visit_biz", "/"), ("admiry_session", "/")):
        assert any(c.startswith(f"{name}=") and "Max-Age=0" in c and f"Path={path};" in c + ";" for c in deleted), name
    kept = {"Cookie": f"admiry_visit={visit}"}  # the old cookie, as if a copy survived the sign-out
    assert turn(client, {"action": "state"}, **kept).json() == {"ok": True, "signin": True}
    for action in ("recommend", "start", "adjust"):
        assert turn(client, {"action": action, "text": "x", "idempotency_key": action}, **kept).json() == SIGNIN
    assert client.post("/api/handoff", json={}, headers={**IP, **kept}).json() == SIGNIN
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 0


def test_a_claimed_visitor_signed_in_as_someone_else_must_sign_in(client, conn, fake, google):
    """I1: the claimed business's cookie works only beside its owner's web session — another signed-in
    e-mail gets the sign-in card and no tool runs."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google, email="someone@example.com")
    assert turn(client, {"action": "recommend", "idempotency_key": "r1"}).json() == SIGNIN
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'recommend'").fetchone()["n"] == 0


def test_recommend_on_a_draft_is_always_the_wall(client, conn, fake):
    """I2 (final review): the wall is not decided on one read and re-checked on another — a draft's
    `recommend` is the wall whatever its session's status, so a racing final answer cannot slip a plan out."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    s = turn(client, {"action": "state"}).json()
    assert s["result"]["session"]["status"] == "open"
    assert turn(client, {"action": "recommend", "idempotency_key": "r1"}).json() == home_api.WALL
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'recommend'").fetchone()["n"] == 0


def test_turn_refuses_wrongly_typed_fields(client, conn, fake):
    """m1 (final review): /api/turn checks the types of the fields it passes on before any tool runs."""
    bad = {"ok": False, "error": "bad request"}
    for body in (
        {"action": "start", "text": 5},
        {"action": "adjust", "monthly_budget_usd": "1500"},
        {"action": "adjust", "radius_miles": True},
        {"action": "answer", "answers": ["objective"]},
        {"action": "say", "idempotency_key": 7},
        {"action": "adjust", "level": ["recommended"]},
        {"action": "adjust", "channel": 5, "channel_monthly_usd": 300},
        {"action": "adjust", "channel": "newsbreak", "channel_monthly_usd": "300"},
    ):
        r = client.post("/api/turn", json=body, headers=IP)
        assert r.status_code == 400 and r.json() == bad, body
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 0


def test_a_rate_limited_adjust_reader_answers_429(client, conn, fake, google, monkeypatch):
    """m4 (final review): the free-text adjust path answers 429 with Retry-After like the other paths."""
    from agent import auth

    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    assert turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()["ok"]

    def limited(conn, scope):
        raise auth.RateLimited("rate limited; retry_after=7")

    monkeypatch.setattr(home_api.auth, "require", limited)
    r = client.post("/api/turn", json={"action": "say", "text": "make it cheaper", "idempotency_key": "s1"}, headers=IP)
    assert r.status_code == 429 and r.headers["retry-after"] == "7" and r.json()["ok"] is False


def _api_state(client, **headers):
    r = client.get("/api/state", headers={**IP, **headers})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()


def test_the_state_is_served_only_to_its_signed_in_owner(client, conn, fake, google):
    """M0 Task 6 (spec 2026-09-29 D102): GET /api/state keeps the `state` action's rules -- no cookie or a dead one
    starts over, a draft nobody owns is behind the wall, a claimed business needs its owner's web session -- and
    gives the owner the whole MarketingState."""
    from agent import state

    assert _api_state(client) == {"ok": True, "fresh": True}
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    assert _api_state(client) == home_api.WALL  # a draft: its answers are the visitor's, the state the owner's
    _answer_all(client)
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    got = _api_state(client)
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert got["ok"] and got["state"] == state.build(conn, config.load(), biz).model_dump(mode="json")
    assert got["state"]["version"] == 1 and got["state"]["business"]["id"] == biz and got["state"]["plan"]
    assert got["state"]["channels"] == [] and got["visitor"]["claimed"] is True
    visit = client.cookies.get("admiry_visit")
    client.post("/connect/signout", data={"csrf": csrf}, follow_redirects=False)
    assert _api_state(client, Cookie=f"admiry_visit={visit}") == {"ok": True, "signin": True}


def test_the_state_action_carries_the_business_and_the_channels_card(client, conn, fake, google):
    """The homepage's `state` action: the cards are MarketingState's (the plan's parts unchanged, the channels card
    beside them), and the business id the card's Pause/Resume and Settings links need."""
    from agent import state

    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    s = turn(client, {"action": "state"}).json()["result"]
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert s["business_id"] == biz and s["cards"] == state.build(conn, config.load(), biz).cards()
    assert s["cards"]["channels"] == {
        "rows": [],
        "footer": {"month_usd": 0.0, "month_cap_usd": None, "next_report_at": None},
    }
    assert set(s["cards"]) == {"plan", "budget", "economics", "rationale", "ads_preview", "channels", "money", "report"}


# ---------------------------------------------------------------- the launch card on a reload (walk fixes 1, Task 6)


def _claimed_plan(client, conn, google, *, prepaid: bool = False) -> dict:
    """This file's dental walk, claimed by its signed-in owner (`/api/claim`: the visitor key becomes a business key, as
    on the page) and built; `prepaid`: the business prepays before the build. Returns the build's answer (`result`)."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    if prepaid:
        conn.execute("UPDATE business SET ads_mode = 'managed'")
    built = turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()
    assert built["ok"], built
    return built["result"]


def _reloaded(client) -> tuple[dict, str | None]:
    """What a reload's `state` answer carries for the launch card: its `next` and its `launch_note`."""
    s = turn(client, {"action": "state"}).json()["result"]
    return s["next"], s["launch_note"]


def test_a_reload_offers_the_own_account_and_a_change_as_the_build_did(client, conn, fake, google):
    """Walk fixes 1, Task 6 round 1: the `state` answer for a planned plan carries the build's own `next` and
    `launch_note`, so a reload draws the same launch card: never the page's own old list, which offered prepaying."""
    built = _claimed_plan(client, conn, google)  # its own Google Ads account, Google Search alone
    nxt, note = _reloaded(client)
    # the ways to put Admiry's code on the owner's site first, while it has none (A3p-5 batch 2 Q6; with support off,
    # as here, without Admiry's team: ruling R3-f)
    assert nxt["actions"][: len(WAYS)] == WAYS
    # Admiry running the ads leads, then the own account (founder's ruling, 2026-10-07)
    assert [a["id"] for a in nxt["actions"][len(WAYS) :]] == ["prepay", "link_own_account", "adjust"] and note is None
    assert (nxt, note) == (built["next"], built["data"]["launch_note"])


def test_a_reload_of_a_prepaid_plan_with_newsbreak_offers_the_balance_as_the_build_did(
    client, conn, fake, google, newsbreak_on, pictures
):
    _claimed_plan(client, conn, google, prepaid=True)
    pin = {"action": "adjust", "channel": "newsbreak", "channel_monthly_usd": 300, "idempotency_key": "p1"}
    pinned = turn(client, pin).json()
    assert pinned["ok"], pinned
    rows = pinned["result"]["data"]["cards"]["plan"]["channels"]
    assert [r["platform"] for r in rows] == ["google_search", "newsbreak"]
    nxt, note = _reloaded(client)
    assert [a["tool"] for a in nxt["actions"][len(WAYS) :]] == ["request_payment_link", "adjust"] and note is None
    assert (nxt, note) == (pinned["result"]["next"], pinned["result"]["data"]["launch_note"])


def test_a_reload_of_a_prepaid_google_only_plan_offers_the_balance_as_the_build_did(client, conn, fake, google):
    """A3c-1: Admiry runs a prepaid business's Google Search in a pool account of its own (`plans.google_open`), so a
    plan that funds Google Search alone offers the balance, on the build and on a reload alike."""
    built = _claimed_plan(client, conn, google, prepaid=True)  # a dentist: NewsBreak is not its trade
    nxt, note = _reloaded(client)
    assert [a["tool"] for a in nxt["actions"][len(WAYS) :]] == ["request_payment_link", "adjust"] and note is None
    assert (nxt, note) == (built["next"], built["data"]["launch_note"])


def test_a_reload_of_a_prepaid_google_only_plan_says_why_nothing_can_launch_while_google_is_closed(
    client, conn, fake, google, monkeypatch
):
    from agent import envelope, plans, tools_consult

    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1: Admiry-run Google accounts not open
    built = _claimed_plan(client, conn, google, prepaid=True)
    nxt, note = _reloaded(client)
    assert nxt == envelope.actions(*WAYS, tools_consult.ADJUST_ACTION) == built["next"]
    assert note == tools_consult.PREPAID_WAITS_SAY == built["data"]["launch_note"]


# ---------------------------------------------------------------- Admiry Pages, the homepage's own card (Task 7)

PAGES_DOMAIN = "business.test"
CHECK_PUBLIC_IP = ["93.184.216.34"]  # RFC 5737 documentation range: never a real, reachable address


def _planned(client, conn, google) -> str:
    """A signed-in, claimed business with a built plan -- the page card's own prerequisite; returns the
    owner's CSRF token for /api/page/*."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    csrf = _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    return csrf


def _checked_transport(monkeypatch, conn) -> None:
    """The post-publish check's own network call (test_tools_hosting.py's own `_checked`, inlined for one
    call): any hostname resolves as public, and the request is answered from whatever `page.html` the
    just-committed publish actually wrote, not a value predicted in advance."""
    monkeypatch.setattr(crawl, "resolve", lambda h: CHECK_PUBLIC_IP)

    def handler(request: httpx.Request) -> httpx.Response:
        row = conn.execute("SELECT html FROM page WHERE url = %s", (str(request.url),)).fetchone()
        return httpx.Response(200, text=row["html"]) if row and row["html"] else httpx.Response(404, text="gone")

    monkeypatch.setattr(tools_hosting, "_check_transport", lambda: httpx.MockTransport(handler))


def test_page_create_mints_a_preview_framed_on_the_web_base(client, conn, fake, google, monkeypatch):
    # sign in BEFORE splitting the web base: the session cookie _sign_in reads back is minted for
    # http://testserver (conftest.py's AGENT_PUBLIC_BASE_URL default) and would not be Secure enough to
    # survive a subsequent http round trip once web_base_url claims https (test_home_page.py's `split`
    # fixture makes the same call with a plain http host for exactly this reason).
    csrf = _planned(client, conn, google)
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://home.test")
    r = client.post("/api/page/create", json={"csrf": csrf}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] and body["token"] and body["preview_url"] and body["sections"] and body["upload_url"]
    row = conn.execute("SELECT embed_origin FROM page_preview WHERE token = %s", (body["token"],)).fetchone()
    assert row["embed_origin"] == "https://home.test"
    preview = client.get(f"/p/{body['token']}")
    assert "frame-ancestors https://home.test" in preview.headers["content-security-policy"]


def test_page_endpoints_need_sign_in_when_anonymous(client, conn, fake):
    for path, extra in (
        ("create", {}),
        ("revise", {"instruction": "warmer"}),
        ("publish", {"token": "pv_x"}),
        ("state", {}),
        ("website", {}),
        ("check", {}),
        ("install-send", {"email": "web@agency.example"}),
        ("install-help", {}),
    ):
        r = client.post(f"/api/page/{path}", json={"csrf": "x", **extra}, headers=IP)
        assert r.json() == home_api.SIGNIN, path


def test_page_endpoints_wall_a_draft_business(client, conn, fake, google):
    """Defensive (H14): the real flow can never reach a planned session while still a draft -- `recommend` is
    always the wall for one (I2) -- but the gate checks `claim_state` directly regardless, so a hand-built
    edge (signed in, never claimed) still gets the wall rather than a tool error."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles", "idempotency_key": "i1"})
    _answer_all(client)
    csrf = _sign_in(client, google)
    for path, extra in (
        ("create", {}),
        ("revise", {"instruction": "warmer"}),
        ("publish", {"token": "pv_x"}),
        ("state", {}),
        ("website", {}),
        ("check", {}),
        ("install-send", {"email": "web@agency.example"}),
        ("install-help", {}),
    ):
        r = client.post(f"/api/page/{path}", json={"csrf": csrf, **extra}, headers=IP)
        assert r.json() == home_api.WALL, path


def test_page_revise_answers_a_new_token(client, conn, fake, google):
    csrf = _planned(client, conn, google)
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    r = client.post("/api/page/revise", json={"csrf": csrf, "instruction": "make it warmer"}, headers=IP)
    assert r.status_code == 200
    revised = r.json()
    assert revised["ok"] and revised["token"] != created["token"] and revised["preview_url"] != created["preview_url"]


def test_page_revise_refuses_a_blank_instruction(client, conn, fake, google):
    csrf = _planned(client, conn, google)
    client.post("/api/page/create", json={"csrf": csrf}, headers=IP)
    r = client.post("/api/page/revise", json={"csrf": csrf, "instruction": "   "}, headers=IP)
    assert r.status_code == 400 and r.json() == home_api.BAD_REQUEST


def test_page_publish_confirms_checks_mails_and_reports_the_ads_sentence(
    client, conn, fake, google, monkeypatch, resend
):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", PAGES_DOMAIN)
    csrf = _planned(client, conn, google)
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    plan_id = conn.execute("SELECT id FROM plan LIMIT 1").fetchone()["id"]
    # A minimal live ads row -- enough for apply_publish_page's "built, but execution is off" branch, without
    # driving the whole Google Ads launch machinery through this web-only test (already covered by
    # test_tools_hosting.py's own ads-launch scenarios).
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) VALUES (%s, %s, %s, %s, %s)",
        (biz, "google_search", plan_id, "live", Jsonb({"campaign": "customers/1/campaigns/1", "customer_id": "1"})),
    )
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    _checked_transport(monkeypatch, conn)
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": created["token"]}, headers=IP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["page"]["status"] == "published" and body["page"]["url"]
    assert body["ads"] == "off" and body["ads_by_channel"] == {"google_search": "off"}  # M0 Task 9 D12
    assert ads_run.OFF_OLD_ADDRESS.format(label="Google Search") in body["say"]  # the ads sentence
    row = conn.execute("SELECT status, check_json FROM page WHERE id = %s", (body["page"]["id"],)).fetchone()
    assert row["status"] == "published" and row["check_json"] is not None  # the after-commit check saved
    assert resend.sent  # the e-mail was attempted
    audited = conn.execute(
        "SELECT actor, actor_class, approval_kind, tool, result, ok FROM agent_action WHERE tool = 'web.publish_page'"
    ).fetchone()
    assert audited["actor"] == "web:owner@example.com" and audited["approval_kind"] == "web"
    # the owner's activity line (get_audit_log) names what happened, not the internal tool name
    proposal_id = conn.execute("SELECT id FROM proposal WHERE kind = 'publish_page'").fetchone()["id"]
    line = tools_account.sentence(audited, None)
    assert line == f"The owner (web) published the page (proposal {proposal_id})" and not JARGON.search(line)


def test_page_publish_needs_a_token(client, conn, fake, google):
    csrf = _planned(client, conn, google)
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": ""}, headers=IP)
    assert r.status_code == 400 and r.json() == home_api.BAD_REQUEST


def test_page_publish_refuses_when_hosting_is_off(client, conn, fake, google):
    csrf = _planned(client, conn, google)
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": created["token"]}, headers=IP)
    assert r.status_code == 409 and "not set up" in r.json()["error"]


def test_page_publish_is_idempotent_per_token(client, conn, fake, google, monkeypatch, resend):
    """H40: a second Publish with the same token must not publish twice (version 1->2, a second proposal, a
    second e-mail, the after-commit hooks re-run) -- it answers with the live URL and does nothing further."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", PAGES_DOMAIN)
    csrf = _planned(client, conn, google)
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    _checked_transport(monkeypatch, conn)

    r1 = client.post("/api/page/publish", json={"csrf": csrf, "token": created["token"]}, headers=IP)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    # H40 minor 3: the web reply says "web", not proposals.confirm's own generic nonspend "assistant"
    assert body1["ok"] and body1["page"]["version"] == 1 and body1["approval"] == {"kind": "web"}

    r2 = client.post("/api/page/publish", json={"csrf": csrf, "token": created["token"]}, headers=IP)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["ok"] and "already published" in body2["say"] and "approval" not in body2

    page_id = body1["page"]["id"]
    assert conn.execute("SELECT count(*) AS n FROM page_version WHERE page_id = %s", (page_id,)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM proposal WHERE kind = 'publish_page'").fetchone()["n"] == 1
    assert len(resend.sent) == 1  # no second report e-mailed
    # "no proposal, no hooks" (H40) also means no new audit trail for a no-op confirm
    audited = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.publish_page'").fetchone()["n"]
    assert audited == 1


def test_page_publish_of_an_expired_token_reads_like_the_homepage_not_a_tool(client, conn, fake, google, monkeypatch):
    """H40 minor 2: the tool says "make a new preview" for an expired token; the web path answers in the homepage's own
    words, "build the page again"."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", PAGES_DOMAIN)
    csrf = _planned(client, conn, google)
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    conn.execute(
        "UPDATE page_preview SET expires_at = now() - interval '1 minute' WHERE token = %s", (created["token"],)
    )
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": created["token"]}, headers=IP)
    assert r.status_code == 409
    assert r.json()["error"] == "That preview has expired; build the page again."


def test_page_state_reflects_no_page_then_a_preview_then_published(client, conn, fake, google, monkeypatch, resend):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", PAGES_DOMAIN)
    csrf = _planned(client, conn, google)  # sign in before splitting the web base -- see the comment above
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://home.test")
    assert client.post("/api/page/state", json={"csrf": csrf}, headers=IP).json() == {"ok": True, "page": None}
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    s = client.post("/api/page/state", json={"csrf": csrf}, headers=IP).json()
    assert s["ok"] and s["page"]["status"] == "preview" and s["page"]["token"] == created["token"]
    assert s["page"]["preview_url"] == created["preview_url"] and s["page"]["upload_url"] == created["upload_url"]
    assert s["page"]["url"] is None and s["page"]["check"] is None
    _checked_transport(monkeypatch, conn)
    client.post("/api/page/publish", json={"csrf": csrf, "token": created["token"]}, headers=IP)
    s2 = client.post("/api/page/state", json={"csrf": csrf}, headers=IP).json()
    assert s2["page"]["status"] == "published" and s2["page"]["url"] and s2["page"]["check"]["rules"]
    # the same preview stays framable on the web base after publish -- nothing re-minted, since embed_origin
    # already matches (_home_preview reuses it)
    assert s2["page"]["token"] == created["token"] and s2["page"]["preview_url"] == created["preview_url"]


def test_page_state_remints_a_preview_the_upload_link_left_unframeable(client, conn, fake, google, monkeypatch):
    """A photo added from the phone link (uploads.py's /u/{token}, out of this card's reach) leaves the page's
    newest preview with no embed_origin (H16/H37 do not thread one through that route) -- /api/page/state must
    still hand the card something it can frame, so it mints a fresh one rather than handing back that one."""
    csrf = _planned(client, conn, google)
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://home.test")
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    page_before = conn.execute("SELECT id, sections FROM page").fetchone()
    conn.execute("UPDATE page_preview SET embed_origin = NULL WHERE token = %s", (created["token"],))
    s = client.post("/api/page/state", json={"csrf": csrf}, headers=IP).json()
    assert s["page"]["token"] != created["token"]  # a fresh preview, not the un-embeddable one
    assert s["page"]["sections"] == created["sections"]  # the same content, just re-minted
    row = conn.execute("SELECT embed_origin FROM page_preview WHERE token = %s", (s["page"]["token"],)).fetchone()
    assert row["embed_origin"] == "https://home.test"
    preview = client.get(f"/p/{s['page']['token']}")
    assert "frame-ancestors https://home.test" in preview.headers["content-security-policy"]
    # page.sections itself (the draft's version counter) is untouched -- this is a fresh window, not a new draft
    page_after = conn.execute("SELECT sections FROM page WHERE id = %s", (page_before["id"],)).fetchone()
    assert page_after["sections"] == page_before["sections"]


# ---------------------------------------------------------------- the "Your website" card (A3p-5 batch 2 Task 2)

ELSEWHERE = "https://offer.example/smile"  # an owner page on another host than the website (ruling R2-b)


def _check_as(monkeypatch, *, ok: bool, tracking: bool) -> list[str]:
    """The check set_landing_page runs, answered without the network (test_tools_pages mocks it the same way): its
    tracking rule found Admiry's code or did not, and the report passes or fails. Returns the addresses checked."""
    checked: list[str] = []
    monkeypatch.setattr(crawl, "resolve", lambda h: CHECK_PUBLIC_IP)

    def check(url, page, kit, **kw):
        checked.append(url)
        rules = [
            {
                "id": "tracking",
                "title": "t",
                "ok": tracking,
                "detail": "d",
                "fix": None if tracking else landing.TRACKING_FIX,
            }
        ]
        if tracking and not ok:
            rules.append({"id": "phone", "title": "t", "ok": False, "detail": "d", "fix": "Put the phone at the top."})
        return {"ok": ok, "url": url, "rules": rules, "fetched": {"status": 200, "bytes": 1, "seconds": 0.1}}

    monkeypatch.setattr(landing, "check", check)
    return checked


def _biz(conn) -> str:
    return conn.execute("SELECT id FROM business").fetchone()["id"]


def test_check_now_names_the_website_and_answers_found_it(client, conn, fake, google, monkeypatch):
    """Q6: with no page for the ads yet, Check now names the owner's website as the ads' page (set_landing_page: the
    existing command, so its check runs and its report is saved) and answers Q11's words when the check passes."""
    csrf = _planned(client, conn, google)
    checked = _check_as(monkeypatch, ok=True, tracking=True)
    r = client.post("/api/page/check", json={"csrf": csrf}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] and body["say"] == "Found it. Your ads can run." == site_install.FOUND_IT
    assert checked == ["https://brightsmile.example"]
    page = landing.current(conn, _biz(conn))
    assert page["url"] == "https://brightsmile.example" and page["status"] == "published"
    assert landing.saved_report(page)["ok"] is True
    found = {"state": "found", "say": site_install.FOUND, "url": "https://brightsmile.example", "platform_name": None}
    assert body["website"] == found  # the page the ads use, for the card's line (spec 2026-10-12 §3.5)


def test_check_now_answers_the_checks_own_sentence_and_the_launch_stays_refused(
    client, conn, fake, google, monkeypatch
):
    """Q11: a check that does not pass answers with its own sentence (set_landing_page's, the fixes in it), and the
    launch gate is unchanged (W14): with the account ready, approving the plan is refused for the page's fix."""
    csrf = _planned(client, conn, google)
    biz = _biz(conn)
    google_ads_link.save(conn, biz, "1234567890", status="active")
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")
    _check_as(monkeypatch, ok=False, tracking=False)
    body = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert body["ok"] and body["say"] != site_install.FOUND_IT
    assert body["say"].startswith("Your ads will send people to brightsmile.example.")
    assert landing.TRACKING_FIX in body["say"] and body["website"]["state"] == "needs_code"
    plan_id = conn.execute("SELECT id FROM plan ORDER BY version DESC LIMIT 1").fetchone()["id"]
    with pytest.raises(ToolError) as e:
        proposals.precheck_approve_plan(conn, biz, {"plan_id": plan_id})
    assert str(e.value) == ads_run.PAGE_FIX.format(fix=landing.TRACKING_FIX)


def test_check_now_mails_a_report_only_when_the_result_changed(client, conn, fake, google, monkeypatch, resend):
    """Ruling R2-d: naming the website the first time is set_landing_page, which mails its report; once the ads have an
    owner page, Check now checks it again as check_landing_page does -- its report mailed only when whether it passes
    changed."""
    csrf = _planned(client, conn, google)

    def reports() -> int:
        return sum(1 for m in resend.sent if m["subject"].startswith("Your landing page:"))

    _check_as(monkeypatch, ok=False, tracking=False)
    first = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert first["ok"] and reports() == 1  # the website named the ads' page: its first report
    again = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert again["ok"] and reports() == 1  # the same page, the same result: no second e-mail
    assert again["say"].endswith("(Same result as the last check, so no new e-mail.)")
    assert landing.current(conn, _biz(conn))["url"] == "https://brightsmile.example"
    _check_as(monkeypatch, ok=True, tracking=True)
    passed = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert passed["say"] == site_install.FOUND_IT and reports() == 2  # a changed result is mailed


def test_check_now_checks_the_owner_page_the_ads_use_and_never_moves_it(client, conn, fake, google, monkeypatch):
    """Ruling R2-b: ads on an owner page on another host -- Check now checks that page again (set_landing_page with the
    same address), so running ads never move to the website; a page that has the code is `found`, one that does not
    asks for it with `other`'s steps."""
    csrf = _planned(client, conn, google)
    biz = _biz(conn)
    landing.set_owner_url(conn, biz, ELSEWHERE)
    checked = _check_as(monkeypatch, ok=False, tracking=False)
    body = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert checked == [ELSEWHERE] and landing.current(conn, biz)["url"] == ELSEWHERE
    assert body["website"]["state"] == "needs_code" and body["website"]["platform"] == "other"
    assert body["website"]["steps"] == site_install.STEPS["other"]["steps"]
    checked = _check_as(monkeypatch, ok=True, tracking=True)
    body = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert checked == [ELSEWHERE] and landing.current(conn, biz)["url"] == ELSEWHERE
    found = {
        "state": "found",
        "say": site_install.FOUND,
        "url": ELSEWHERE,
        "platform_name": None,
    }  # never read: no name
    assert body["say"] == site_install.FOUND_IT and body["website"] == found


def test_check_now_checks_nothing_while_the_ads_use_an_admiry_page(client, conn, fake, google, monkeypatch):
    """The ads on a page Admiry hosts (its row has no Check now): nothing is checked and nothing moves; the answer is the
    row's own sentence."""
    csrf = _planned(client, conn, google)
    biz = _biz(conn)
    page = landing.ensure_page_for_kit(conn, biz)
    conn.execute(
        "UPDATE page SET source = 'admiry', status = 'published', hostname = 'smile.business.test', "
        "url = 'https://smile.business.test/' WHERE id = %s",
        (page["id"],),
    )
    checked = _check_as(monkeypatch, ok=True, tracking=True)
    body = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert body["ok"] and checked == [] and landing.current(conn, biz)["url"] == "https://smile.business.test/"
    assert body["say"] == site_install.ADMIRY_PAGE and body["website"]["state"] == "admiry_page"


def test_check_now_reads_a_website_without_a_scheme_as_https(client, conn, fake, google, monkeypatch):
    """Ruling F-3 (the final review): the website on file is text a model or an assistant wrote, so it may come without
    a scheme; Check now reads it as https://, as consult reads an address in the owner's words -- the check runs on
    https://brightsmile.example and names it the ads' page, never "url must be an http(s) address"."""
    csrf = _planned(client, conn, google)
    biz = _biz(conn)
    latest = business.latest_knowledge(conn, biz)[1]
    business.add_knowledge(conn, biz, {**latest, "website": "brightsmile.example"}, source="owner")
    checked = _check_as(monkeypatch, ok=True, tracking=True)
    body = client.post("/api/page/check", json={"csrf": csrf}, headers=IP).json()
    assert body["ok"] and body["say"] == site_install.FOUND_IT, body
    assert checked == ["https://brightsmile.example"]
    assert landing.current(conn, biz)["url"] == "https://brightsmile.example"


def test_the_website_card_gets_the_rows_steps_and_the_pages_code(client, conn, fake, google):
    """Q6's steps view: "Here is how to add it on {name}.", the steps of the row's platform and Admiry's code for the
    business's one page -- the kit's own code (the same token)."""
    csrf = _planned(client, conn, google)
    r = client.post("/api/page/website", json={"csrf": csrf}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    biz = _biz(conn)
    assert body["ok"] and body["website"] == site_install.row(conn, biz) and body["website"]["state"] == "needs_code"
    install = body["install"]
    other = site_install.STEPS["other"]
    assert install["how"] == "Here is how to add it on your site." and install["steps"] == other["steps"]
    assert install["platform"] == "other" and install["requirement"] is None and install["help_url"] is None
    assert install["snippet"] == landing.kit(conn, biz)["tracking"]["snippet"]
    assert 'data-page="' + landing.current(conn, biz)["id"] + '"' in install["snippet"]
    assert "password" not in json.dumps(body).lower()


def test_the_state_and_a_build_carry_the_row_and_the_ways_alike(client, conn, fake, google):
    """The homepage's reload (`state`) draws the card and offers what the build offered: the plan's setup carries the row,
    and `next` the four ways before the launch actions (ruling R2-c: the ways live in launch_next)."""
    built = _claimed_plan(client, conn, google)
    state = turn(client, {"action": "state"}).json()["result"]
    assert state["cards"]["plan"]["setup"]["website"] == built["data"]["cards"]["plan"]["setup"]["website"]
    assert state["cards"]["plan"]["setup"]["website"]["state"] == "needs_code"
    assert state["next"] == built["next"] and state["next"]["actions"][: len(WAYS)] == WAYS
    # ruling R3-f: support off, as here -- the card's ways and `next` leave Admiry's team out
    shown = [w["id"] for w in state["cards"]["plan"]["setup"]["website"]["ways"]]
    assert shown == ["install_self", "install_send", "admiry_page"]
    assert "install_help" not in [a["id"] for a in state["next"]["actions"]]


# ---------------------------------------------------------------- the web person and Admiry's team (A3p-5 batch 2 Task 3)


def _install_mail(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["subject"].startswith("How to add Admiry's code")]


def test_the_card_sends_the_steps_to_the_web_person(client, conn, fake, google, resend):
    """Q7 on the homepage (ruling R0-f): "Send it to my web person" posts the address the owner typed to
    send_install_steps, run in process under the visitor's key; the e-mail goes from Admiry with reply-to the owner and
    the answer is Q7's sentence. A refusal comes back as its own sentence and sends nothing; a body without an address
    is a bad request."""
    csrf = _planned(client, conn, google)
    r = client.post("/api/page/install-send", json={"csrf": csrf, "email": "web@agency.example"}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"ok": True, "say": "Sent to web@agency.example. We'll tell you when the code shows up."}
    [sent] = _install_mail(resend.sent)
    assert sent["to"] == ["web@agency.example"] and sent["reply_to"] == "owner@example.com"
    own = client.post("/api/page/install-send", json={"csrf": csrf, "email": "Owner@example.com"}, headers=IP)
    assert own.status_code == 200 and own.json() == {"ok": False, "error": site_install.OWN_ADDRESS}
    for bad in ({"csrf": csrf}, {"csrf": csrf, "email": "  "}, {"csrf": csrf, "email": 7}):
        assert client.post("/api/page/install-send", json=bad, headers=IP).status_code == 400, bad
    assert len(_install_mail(resend.sent)) == 1
    rows = conn.execute(
        "SELECT ok, args_redacted, result FROM agent_action WHERE tool = 'send_install_steps' ORDER BY id"
    ).fetchall()
    assert [r["ok"] for r in rows] == [True, False]  # each call an audit row, the refusal too
    assert "agency" not in json.dumps([dict(r) for r in rows]).lower()  # ruling R3-e: never the address


def test_the_card_asks_admirys_team(client, conn, fake, google, support_on):
    """Q8 on the homepage (ruling R0-f): "Have Admiry's team do it" -- its button's tap, ruling R3-c -- opens the
    `website` ticket; the card's field left empty, its contact is the signed-in owner's address (Google verified it) and
    the answer is Q8's sentence; SP1's acknowledgement goes to that address. A contact typed in the field must pass the
    e-mail channel's rule; one that is not text is a bad request."""
    csrf = _planned(client, conn, google)
    for bad in ({"csrf": csrf, "contact": 7}, {"csrf": csrf, "contact": "x" * 400}):
        assert client.post("/api/page/install-help", json=bad, headers=IP).status_code == 400, bad
    nope = client.post("/api/page/install-help", json={"csrf": csrf, "contact": "not an address"}, headers=IP)
    assert nope.json() == {"ok": False, "error": site_install.NOT_AN_ADDRESS}
    r = client.post("/api/page/install-help", json={"csrf": csrf, "contact": "  "}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"ok": True, "say": "Our team will contact you at owner@example.com soon."}
    t = conn.execute("SELECT reason, status, contact_email, source FROM support_ticket").fetchone()
    assert t == {"reason": "website", "status": "open", "contact_email": "owner@example.com", "source": "home"}
    acks = [m for m in support_on.sent if m["subject"].startswith("We got your message")]
    assert [m["to"] for m in acks] == [["owner@example.com"]]


def test_a_contact_typed_on_the_card_is_the_tickets(client, conn, fake, google, support_on):
    """Ruling R3-c, the field ready beside the button: an address the owner types is the new ticket's contact (Q8:
    the contact given, else the owner's sign-in address), and the acknowledgement goes there."""
    csrf = _planned(client, conn, google)
    r = client.post("/api/page/install-help", json={"csrf": csrf, "contact": "Office@Smile.example"}, headers=IP)
    assert r.json() == {"ok": True, "say": "Our team will contact you at office@smile.example soon."}
    t = conn.execute("SELECT contact_email FROM support_ticket").fetchone()
    assert t == {"contact_email": "office@smile.example"}
    acks = [m for m in support_on.sent if m["subject"].startswith("We got your message")]
    assert [m["to"] for m in acks] == [["office@smile.example"]]


def test_another_person_signed_in_here_acts_on_no_business(client, conn, fake, google, support_on):
    """W11/W12: only the business's own owner -- someone else signed in on this browser is asked to sign in, and
    nothing is sent or asked."""
    _planned(client, conn, google)
    csrf = _sign_in(client, google, email="someone@else.example")
    for path, extra in (("install-send", {"email": "web@agency.example"}), ("install-help", {})):
        r = client.post(f"/api/page/{path}", json={"csrf": csrf, **extra}, headers=IP)
        assert r.json() == home_api.SIGNIN, path
    assert support_on.sent == []
    assert conn.execute("SELECT count(*) AS n FROM install_request").fetchone()["n"] == 0


# ---------------------------------------------------------------- a signed-in owner without the visitor cookie
# (founder's dev walk, 2026-10-07: a re-sign-in had no business cookie, so the first words ran an anonymous `start`
# and made an "unknown business"; ruling: ask first, and list the owner's businesses to pick from)


def _planned_and_signed_out(client, conn, google) -> tuple[str, str]:
    """A business whose plan the owner built on the homepage and claimed, then signed out of: the visitor cookies are
    gone with the sign-out. Returns (business_id, the csrf of a fresh sign-in)."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    assert turn(client, {"action": "recommend", "idempotency_key": "r1"}).json()["ok"]
    assert client.post("/connect/signout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert "admiry_visit" not in client.cookies
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    return biz, _sign_in(client, google)


def test_a_signed_in_owner_without_the_visitor_cookie_is_offered_their_businesses(client, conn, fake, google):
    biz, _csrf = _planned_and_signed_out(client, conn, google)
    r = turn(client, {"action": "state"})
    assert r.json() == {
        "ok": True,
        "fresh": True,
        "choose": [{"id": biz, "name": "Bright Smile Dental", "state": "plan"}],
    }
    assert not any(  # nothing attached until the owner picks (the stale cookies' deletion is all it sets)
        c.startswith("admiry_visit=") and "Max-Age=0" not in c for c in r.headers.get_list("set-cookie")
    )
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE label = 'web'").fetchone()["n"] == 0


def test_the_businesses_offered_lead_with_the_newest_consultation_and_name_the_blank_one(client, conn, fake, google):
    biz, _csrf = _planned_and_signed_out(client, conn, google)
    blank = business.create(conn, locale="en-US")
    conn.execute(
        "UPDATE business SET owner_email = 'owner@example.com', claim_state = 'claimed' WHERE id = %s", (blank,)
    )
    other = business.create(conn, locale="en-US")  # someone else's: never offered
    conn.execute(
        "UPDATE business SET owner_email = 'other@example.com', claim_state = 'claimed' WHERE id = %s", (other,)
    )
    r = turn(client, {"action": "state"}).json()
    assert [(c["id"], c["state"]) for c in r["choose"]] == [(biz, "plan"), (blank, "none")]
    assert r["choose"][1]["name"] == "A business without a name yet"


def test_a_signed_in_visitor_with_no_business_starts_afresh(client, conn, fake, google):
    _sign_in(client, google)
    assert turn(client, {"action": "state"}).json() == {"ok": True, "fresh": True}


def test_picking_a_business_sets_the_visitor_cookie_and_resumes_its_plan(client, conn, fake, google):
    biz, csrf = _planned_and_signed_out(client, conn, google)
    r = turn(client, {"action": "pick", "business_id": biz, "csrf": csrf, "idempotency_key": "p1"})
    assert r.json() == {
        "ok": True,
        "visitor": {
            "signed_in": True,
            "claimed": True,
            "session_id": r.json()["visitor"]["session_id"],
            "wall": False,
            "business_id": biz,
        },
    }
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith("admiry_visit=") and "Max-Age=2592000" in c and "Path=/api" in c for c in cookies)
    assert any(c.startswith("admiry_visit_biz=") and "Max-Age=2592000" in c and "Path=/;" in c + ";" for c in cookies)
    key = conn.execute("SELECT class, scopes, expires_at, business_id FROM api_key WHERE label = 'web'").fetchone()
    assert (key["class"], sorted(key["scopes"]), key["business_id"]) == ("business", ["manage", "read"], biz)
    assert timedelta(days=29, hours=23) < key["expires_at"] - datetime.now(timezone.utc) <= timedelta(days=30)
    st = turn(client, {"action": "state"}).json()
    assert st["result"]["session"]["status"] == "planned" and st["result"]["cards"]["budget"]["monthly_usd"] == 2000
    assert _actions(conn, "web.pick") == [("web.pick", True)]


def test_pick_refuses_another_owners_business_and_needs_the_session_and_csrf(client, conn, fake, google):
    biz, csrf = _planned_and_signed_out(client, conn, google)
    other = business.create(conn, locale="en-US")
    conn.execute(
        "UPDATE business SET owner_email = 'other@example.com', claim_state = 'claimed' WHERE id = %s", (other,)
    )
    r = turn(client, {"action": "pick", "business_id": other, "csrf": csrf, "idempotency_key": "p1"}).json()
    assert r == {"ok": False, "error": home_api.NOT_YOURS}
    assert (
        client.post("/api/turn", json={"action": "pick", "business_id": biz, "csrf": "bad"}, headers=IP).status_code
        == 403
    )
    assert (
        client.post("/api/turn", json={"action": "pick", "business_id": 7, "csrf": csrf}, headers=IP).status_code == 400
    )
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE label = 'web'").fetchone()["n"] == 0
    client.cookies.clear()  # no session at all
    assert (
        turn(client, {"action": "pick", "business_id": biz, "csrf": csrf, "idempotency_key": "p2"}).json()
        == FRESH_EXPECTED
    )


def test_pick_is_limited_per_owner_per_hour(client, conn, fake, google, monkeypatch):
    monkeypatch.setattr(home_api, "PICK_LIMIT_PER_HOUR", 2)
    biz, csrf = _planned_and_signed_out(client, conn, google)
    for i in range(2):
        assert turn(client, {"action": "pick", "business_id": biz, "csrf": csrf, "idempotency_key": f"p{i}"}).json()[
            "ok"
        ]
    r = client.post("/api/turn", json={"action": "pick", "business_id": biz, "csrf": csrf}, headers=IP)
    assert r.status_code == 429 and r.headers["retry-after"]
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE label = 'web'").fetchone()["n"] == 2


def test_anonymous_words_about_an_existing_plan_ask_for_sign_in_instead_of_a_new_business(client, conn, fake):
    benchmark.seed(conn)
    for text in ("i wanna continue my proposal", "show me my plan", "where are my campaigns?", "let me sign in"):
        r = turn(client, {"action": "start", "text": text, "idempotency_key": text}).json()
        assert r == {"ok": False, "signin": True, "error": home_api.SIGN_IN_FOR_PLAN}, text
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 0
    assert "admiry_visit" not in client.cookies
    # words that describe a business still start one ("plan" alone is not "my plan")
    r = turn(client, {"action": "start", "text": "I plan to open a dentist office in Austin", "idempotency_key": "s"})
    assert r.json()["ok"] and conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1


def test_a_signed_in_owner_typing_about_their_plan_is_offered_their_businesses(client, conn, fake, google):
    biz, _csrf = _planned_and_signed_out(client, conn, google)
    r = turn(client, {"action": "start", "text": "i wanna continue my proposal", "idempotency_key": "s1"}).json()
    assert r == {"ok": True, "fresh": True, "choose": [{"id": biz, "name": "Bright Smile Dental", "state": "plan"}]}
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1
