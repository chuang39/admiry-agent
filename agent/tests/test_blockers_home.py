"""B0 §3.2 (spec 2026-10-08), now the checklist (L1 Task 4, GET /api/launch_check): what stops the latest draft's
launch, for the homepage's dental walk claimed by its signed-in owner (test_home_api) with the Google switch on as
the dev box has it (validate). The home's `state` and plan answers carry no `blocker` any more."""

import pytest
from psycopg.types.json import Jsonb

from agent import ads_run, blockers, business, google_ads_link, landing, plans
from agent.tests.test_home_api import IP, _claimed_plan, fake, turn  # noqa: F401 -- `fake` is a fixture used by name


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")


def _biz(conn) -> str:
    return conn.execute("SELECT id FROM business").fetchone()["id"]


def _knows(conn, biz: str, **changes) -> None:
    _version, now = business.latest_knowledge(conn, biz)
    business.add_knowledge(conn, biz, {**now, **changes}, source="owner")


def _items(client) -> list[dict]:
    """The plan card's checklist now (GET /api/launch_check), where a home answer's `blocker` moved to (L1 Task 4)."""
    r = client.get("/api/launch_check", headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()["check"]["items"]


def test_a_reload_carries_no_blocker_and_the_list_has_what_stops_it(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    """L1 Task 4: `state` no longer carries B0's `blocker`; the checklist (GET /api/launch_check) lists everything
    that stops the launch at once -- unlike B0's single blocker, which the account alone would have masked here
    (the old single-refusal check never reached NO_PAGE once NOT_LINKED came first; the checklist reads every
    place's own rules regardless, leaving the account and the funds alone -- the step after the list)."""
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    assert "blocker" not in turn(client, {"action": "state"}).json()["result"]
    assert any(i["resolution"] == "page" for i in _items(client))  # no page built yet
    _knows(conn, biz, phone=None)
    found = next(i for i in _items(client) if i["id"] == "phone")
    assert found["text"] == ads_run.NO_PHONE
    assert found["plan_id"] == plans.latest(conn, biz)["id"] and found["question"]["input"] == "tel"
    assert any(i["resolution"] == "page" for i in _items(client))  # both stand on the list at once


def test_a_change_to_the_plan_lists_the_blocker_it_meets_and_a_choice_clears_it(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
):
    """OVER_LIMIT: the owner's limit lowered under the plan's amount; the list offers the budgets that pass, and
    adjust with one of them -- the home's own change path -- clears it from the list."""
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    business.set_goal(conn, biz, monthly_limit_usd=1000)
    found = next(i for i in _items(client) if i["id"] == "limit")
    assert found["text"] == ads_run.OVER_LIMIT and found["kind"] == "decide" and found["actions"]
    assert found["actions"][0]["args"] == {"monthly_budget_usd": 1000} and found["actions"][0]["primary"]
    changed = turn(client, {"action": "adjust", **found["actions"][0]["args"], "idempotency_key": "b1"}).json()
    assert changed["ok"], changed
    assert changed["result"]["data"]["cards"]["budget"]["monthly_usd"] <= 1000
    assert not any(i["id"] == "limit" for i in _items(client))


def _csrf(client) -> str:
    return client.get("/api/csrf").json()["csrf"]


def _fix(client, csrf: str, **body) -> dict:
    r = client.post("/api/blocker", json={"csrf": csrf, **body}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()


def _plan_id(conn, biz: str) -> str:
    """The latest version's id: the plan an answer names, as the page sends it from its message (`plan_id`)."""
    return plans.latest(conn, biz)["id"]


def _plan_sections(conn, biz: str, change) -> dict:
    """`change(sections)` made in place on the latest version, as a plan from before a rule was kept stored it."""
    plan = plans.latest(conn, biz)
    secs = plan["sections"]
    change(secs)
    conn.execute("UPDATE plan SET json = jsonb_set(json, '{sections}', %s) WHERE id = %s", (Jsonb(secs), plan["id"]))
    return plans.get(conn, biz, plan["id"])


def test_the_phone_typed_into_the_question_is_saved_and_the_next_look_runs(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    out = _fix(client, _csrf(client), action="answer", id="phone", plan_id=_plan_id(conn, biz), text="(650) 555-0100")
    assert out["ok"], out
    assert out["result"]["resolved"] and out["result"]["say"] == blockers.SAVED_PHONE.format(phone="(650) 555-0100")
    assert "blocker" not in out["result"]["state"] and out["result"]["state"]["cards"]
    assert not any(i["id"] == "phone" for i in _items(client))
    assert business.latest_knowledge(conn, biz)[1]["phone"] == "(650) 555-0100"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'update_business'").fetchone()["n"] == 1


@pytest.mark.parametrize("words", ["call me", "555-0100", "", "   "])
def test_an_answer_not_in_the_questions_shape_is_refused_and_nothing_is_written(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
    words,
):
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    version = business.latest_knowledge(conn, biz)[0]
    out = _fix(client, _csrf(client), action="answer", id="phone", plan_id=_plan_id(conn, biz), text=words)
    assert out == {"ok": False, "error": blockers.PHONE_SHAPE}
    assert business.latest_knowledge(conn, biz)[0] == version


def test_an_answer_naming_another_plan_is_stale_and_writes_nothing(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    """Final review C1: an answer names the plan its question was asked on (`plan_id`, beside the question's `id`).
    One for another plan -- another tab's, another business's message -- answers where things stand (NOTHING_ASKED,
    with the state) and writes nothing, though the question open now is the same one."""
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    found = next(i for i in _items(client) if i["id"] == "phone")
    assert found["plan_id"] == _plan_id(conn, biz)
    version = business.latest_knowledge(conn, biz)[0]
    out = _fix(client, _csrf(client), action="answer", id="phone", plan_id="pl_other", text="(650) 555-0100")
    assert out["ok"] and out["result"]["say"] == blockers.NOTHING_ASKED, out
    assert next(i for i in _items(client) if i["id"] == "phone") == found  # still open, drawn again where it stands
    assert business.latest_knowledge(conn, biz)[0] == version
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'update_business'").fetchone()["n"] == 0


def test_an_answer_naming_a_different_question_than_the_one_open_is_refused_and_spends_nothing(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
    monkeypatch,
):
    """P10: an answer names the question it answers (`id`); sent for a different one than the open ask -- here, an
    address for "location" while "phone" is the one open -- it is no answer at all (NOTHING_ASKED, the stale state,
    not the phone's shape). Nothing is written, and the address never reaches the map: no call to Google
    (`places.search`, the consultation's own lookup; `places.search_text`, the one `update_business`'s address patch
    would use) is ever made."""
    from agent import places

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    assert any(i["id"] == "phone" for i in _items(client))
    version = business.latest_knowledge(conn, biz)[0]
    calls: list[str] = []
    monkeypatch.setattr(places, "search", lambda *a, **kw: calls.append("search") or [])
    monkeypatch.setattr(places, "search_text", lambda *a, **kw: calls.append("search_text") or None)
    text = "500 University Ave, Palo Alto, CA"
    out = _fix(client, _csrf(client), action="answer", id="location", plan_id=_plan_id(conn, biz), text=text)
    assert out["ok"] and out["result"]["say"] == blockers.NOTHING_ASKED and out["result"]["state"]
    assert business.latest_knowledge(conn, biz)[0] == version
    assert calls == []


def test_the_address_is_placed_on_the_map_and_centres_the_ads(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, listing=None, service_area=[])
    assert any(i["id"] == "location" for i in _items(client))
    place = "500 University Ave, Palo Alto, CA"
    out = _fix(client, _csrf(client), action="answer", id="location", plan_id=_plan_id(conn, biz), text=place)
    assert out["ok"] and out["result"]["say"] == blockers.SAVED_PLACE.format(place=place), out
    assert "blocker" not in out["result"]["state"]
    assert not any(i["id"] == "location" for i in _items(client))
    assert ads_run.has_point(business.latest_knowledge(conn, biz)[1])


def test_the_miles_change_the_draft(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _plan_sections(conn, biz, lambda s: s["consultation"]["geo"].update(radius_miles=None))
    assert any(i["id"] == "radius" for i in _items(client))
    out = _fix(client, _csrf(client), action="answer", id="radius", plan_id=_plan_id(conn, biz), text="10 miles")
    assert out["ok"] and out["result"]["say"] == blockers.SAVED_MILES.format(miles="10 miles")
    assert plans.latest(conn, biz)["sections"]["consultation"]["geo"]["radius_miles"] == 10
    assert "blocker" not in out["result"]["state"]
    assert not any(i["id"] == "radius" for i in _items(client))


def test_admiry_writes_the_ad_text_from_the_conversation_and_changes_nothing_else(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
):
    """TOO_LITTLE_COPY: "Write the ad text" -- the setup card's former button, now the conversation's (B0 P3): the
    model drafts the words, adjust's word swap writes the new version, the budget and the business stand; one model call
    counted toward the owner's day (`web.write_copy`)."""
    from agent.channels.google_search import adapter as google_search
    from agent.tests.test_review_launch import _without_copy

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    before = _without_copy(conn, biz)
    found = next(i for i in _items(client) if i["resolution"] == "write_copy")
    assert found["actions"] == [blockers.WRITE_COPY]
    out = _fix(client, _csrf(client), action="write_copy", plan_id=found["plan_id"])
    assert out["ok"] and out["result"]["say"] == blockers.WROTE_COPY, out
    after = plans.latest(conn, biz)
    assert after["id"] != before["id"] and after["status"] == plans.DRAFT
    ch = google_search._search_channel(after)
    assert len(ch["headlines"]) >= plans.MIN_HEADLINES and len(ch["descriptions"]) >= plans.MIN_DESCRIPTIONS
    assert plans.total_usd(after["sections"]) == plans.total_usd(before["sections"])
    assert after["sections"]["consultation"]["business"] == before["sections"]["consultation"]["business"]
    assert fake.plan_calls[-1]["instruction"] == blockers.COPY_INSTRUCTION
    assert out["result"]["state"]["plan_id"] == after["id"] and "blocker" not in out["result"]["state"]
    assert not any(i["resolution"] == "write_copy" for i in _items(client))
    row = conn.execute("SELECT actor, args_redacted, result FROM agent_action WHERE tool = 'web.write_copy'").fetchone()
    assert row["actor"] == "web:owner@example.com" and row["args_redacted"]["plan_id"] == before["id"]
    assert row["result"]["plan_id"] == after["id"]


def test_writing_the_ad_text_counts_toward_the_owners_day(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    from agent import auth
    from agent.tests.test_review_launch import _without_copy

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    plan = _without_copy(conn, biz)
    calls = len(fake.plan_calls)
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, actor_class, tool, args_redacted, args_hash, ok) "
        "SELECT %s, 'web:owner@example.com', 'business', 'web.write_copy', '{}', 'h', true "
        "FROM generate_series(1, %s)",
        (biz, auth.LLM_CALLS_PER_DAY),
    )
    out = _fix(client, _csrf(client), action="write_copy", plan_id=plan["id"])
    assert not out["ok"] and out["error"].startswith("today's limit") and len(fake.plan_calls) == calls


def test_a_fix_for_a_plan_that_changed_is_refused_and_writes_nothing(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    """Review Focus 2: a tap on a message about a version the owner has changed since (another tab, the slider) acts
    on nothing: PLAN_MOVED, no version written, no model asked -- but the page can still redraw (blockers.STALE: the
    answer is `ok` with the state as it stands now, not a dead end)."""
    from agent.tests.test_review_launch import _without_copy

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    plan = _without_copy(conn, biz)
    versions = conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"]
    calls = len(fake.plan_calls)
    for action in ("write_copy", "fresh_plan"):
        out = _fix(client, _csrf(client), action=action, plan_id="pl_other")
        assert out["ok"] is True and out["result"]["say"] == blockers.PLAN_MOVED
        assert out["result"]["state"]["plan_id"] == plan["id"]
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == versions
    assert len(fake.plan_calls) == calls


def test_the_ad_text_in_place_is_not_written_again(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    plan = plans.latest(conn, _biz(conn))
    calls = len(fake.plan_calls)
    out = _fix(client, _csrf(client), action="write_copy", plan_id=plan["id"])
    assert out == {"ok": False, "error": blockers.COPY_IN_PLACE} and len(fake.plan_calls) == calls


def test_a_fresh_plan_is_drawn_from_the_consultation(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    before = _plan_sections(conn, biz, lambda s: s.update(estimate={"basis": "none"}))
    found = next(i for i in _items(client) if i["resolution"] == "fresh_plan")
    assert found["actions"] == [blockers.FRESH_PLAN]
    out = _fix(client, _csrf(client), action="fresh_plan", plan_id=found["plan_id"])
    assert out["ok"] and out["result"]["say"] == blockers.FRESH_DRAWN, out
    after = plans.latest(conn, biz)
    assert after["id"] != before["id"] and fake.plan_calls[-1]["instruction"] == blockers.FRESH_INSTRUCTION
    assert out["result"]["state"]["plan_id"] == after["id"] and "blocker" not in out["result"]["state"]
    assert not any(i["resolution"] == "fresh_plan" for i in _items(client))


def test_check_my_page_now_checks_the_page_on_file(client, conn, fake, google, switched_on, monkeypatch):  # noqa: F811 -- the fixture
    """PAGE_UNCHECKED: the ads have the owner's page with no report yet (the account linked, so the launch's
    check reaches the page): Check my page now runs check_landing_page on the page on file and the next look passes it."""
    from agent.tests.test_home_api import ELSEWHERE, _check_as

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    google_ads_link.save(conn, biz, "1234567890", status="active")
    assert any(i["resolution"] == "page" for i in _items(client))  # NO_PAGE first
    landing.set_owner_url(conn, biz, ELSEWHERE)
    found = next(i for i in _items(client) if i["resolution"] == "check_page")
    assert found["actions"] == [blockers.CHECK_NOW]
    checked = _check_as(monkeypatch, ok=True, tracking=True)
    out = _fix(client, _csrf(client), action="check_page")
    assert out["ok"] and out["result"]["say"] == blockers.CHECKED_OK and checked == [ELSEWHERE], out
    assert "blocker" not in out["result"]["state"]
    assert not any(i["resolution"] == "check_page" for i in _items(client))


def test_the_fixes_need_the_signed_in_owner_and_the_csrf_token(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    from agent import home_api

    _claimed_plan(client, conn, google)
    assert client.post("/api/blocker", json={"action": "check_page"}, headers=IP).status_code == 403
    assert client.post("/api/blocker", json={"action": "nope", "csrf": _csrf(client)}, headers=IP).status_code == 400
    client.cookies.clear()
    r = client.post("/api/blocker", json={"action": "check_page", "csrf": "x"}, headers=IP)
    assert r.json() == home_api.SIGNIN


def test_an_answer_with_no_question_open_is_refused(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    plan_id = _plan_id(conn, _biz(conn))
    out = _fix(client, _csrf(client), action="answer", id="phone", plan_id=plan_id, text="(650) 555-0100")
    assert out["ok"] is True and out["result"]["say"] == blockers.NOTHING_ASKED and out["result"]["state"]


def _say(client, text: str, key: str = "s1") -> dict:
    out = turn(client, {"action": "say", "text": text, "idempotency_key": key}).json()
    assert out["ok"], out
    return out["result"]


@pytest.mark.parametrize("words", ["(650) 555-0100", "my number is 650 555 0100", "+1 650-555-0100"])
def test_a_phone_typed_in_the_conversation_answers_the_question_before_the_adjust_read(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
    words,
):
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    reads = len(fake.adjust_calls)
    result = _say(client, words)
    assert result["resolved"] and "blocker" not in result["state"]
    assert not any(i["id"] == "phone" for i in _items(client))
    saved = business.latest_knowledge(conn, biz)[1]["phone"]
    assert saved in words and result["say"] == blockers.SAVED_PHONE.format(phone=saved)
    assert len(fake.adjust_calls) == reads  # read in code: the adjust read never saw the words


def test_an_address_and_miles_typed_in_the_conversation_answer_their_questions(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, listing=None, service_area=[])
    result = _say(client, "500 University Ave, Palo Alto, CA", "s1")
    assert result["resolved"] and ads_run.has_point(business.latest_knowledge(conn, biz)[1])
    _plan_sections(conn, biz, lambda s: s["consultation"]["geo"].update(radius_miles=None))
    result = _say(client, "within 15 miles", "s2")
    assert result["resolved"] and result["say"] == blockers.SAVED_MILES.format(miles="15 miles")
    assert plans.latest(conn, biz)["sections"]["consultation"]["geo"]["radius_miles"] == 15


def test_a_typed_address_goes_to_the_map_without_its_lead_in_and_the_place_found_is_said(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
    monkeypatch,
):
    """Final review I4: "we're at 12 Main St" reaches update_business -- and so the map -- as "12 Main St", and Admiry
    says back the address the map placed (the location as stored, without the country), so an owner whose words
    Places put somewhere else reads where the ads now centre, not their own words."""
    import json

    from agent import places

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, listing=None, service_area=[])
    asked: list[str] = []
    found = places.Candidate("ChIJ12", "12 Main St", "12 Main St, Palo Alto, CA 94301, USA", 37.4443, -122.1598)
    monkeypatch.setattr(places, "search_text", lambda query, key: asked.append(query) or found)
    result = _say(client, "we're at 12 Main St")
    assert result["resolved"] and asked == ["12 Main St"], result
    row = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'update_business'").fetchone()
    assert json.loads(row["args_redacted"]["patch"]) == {"location": {"address": "12 Main St"}}
    assert result["say"] == blockers.SAVED_PLACE.format(place="12 Main St, Palo Alto, CA 94301")


def test_words_that_are_not_the_answer_go_to_the_adjust_read_and_the_question_stays(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,
):
    """Review Focus 3: with the place's question open, a change asked in words that only look like an address part, a
    number meant as money, a sentence with digits -- the adjust read takes them (today's path); with nothing read, the
    home says what it can change here, never "continue in your assistant" (B0 P7), and the question again."""
    from agent import home_api

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, listing=None, service_area=[])
    words_list = [
        "make it 10",
        "change the offer to free whitening",
        "what colour is the sky",
        "change the area to Palo Alto, CA",  # fix round 1: ends like an address, but asks for something else
    ]
    for i, words in enumerate(words_list):
        reads = len(fake.adjust_calls)
        result = _say(client, words, f"w{i}")
        assert len(fake.adjust_calls) == reads + 1, words
        assert "resolved" not in result and "blocker" not in result
        assert result["say"] == home_api.NOT_AN_ADJUSTMENT_HERE and "continue in your assistant" not in result["say"]
        assert any(item["id"] == "location" for item in _items(client))


def test_a_change_named_in_words_still_changes_the_plan_with_a_question_open(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    from agent import models

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    none = {"monthly_budget_usd": None, "level": None, "radius_miles": None, "offer": None, "objective": None}
    none |= {"channel": None, "channel_monthly_usd": None, "asks_for_change": True}
    fake.adjusts = {
        "make it fifteen hundred": models.AdjustRead(**{**none, "monthly_budget_usd": 1500}, understood=True)
    }
    result = _say(client, "make it fifteen hundred")
    assert result["data"]["cards"]["budget"]["monthly_usd"] == 1500
    assert "blocker" not in result
    assert any(i["id"] == "phone" for i in _items(client))  # the question stays open after the change


def test_the_new_words_are_the_owners():
    """Every sentence B0 says to the owner passes the jargon check -- the fixes', the headline's (final review m15),
    P7's "I can change …" and setup.js's own sentence for a payment that has not shown up (follow-up final review
    I2), each read from its script, as it ships. home.js's own "Ready: Confirm and launch." is gone with B0's
    single blocker (spec 2026-10-10 §4.3's "Words removed"; Task 5 replaces it) -- no longer read here."""
    import re
    from pathlib import Path

    from agent import copy, home_api

    setup_js = (Path(__file__).parents[1] / "static" / "setup.js").read_text()
    not_yet = re.search(r'^  const NOT_YET = "([^"]+)";$', setup_js, re.MULTILINE)
    assert not_yet, "setup.js has no `const NOT_YET = …;` line"
    for text in (
        blockers.PHONE_SHAPE,
        blockers.PLACE_SHAPE,
        blockers.MILES_SHAPE,
        blockers.SAVED_PHONE,
        blockers.SAVED_PLACE,
        blockers.SAVED_MILES,
        blockers.CHECKED_OK,
        blockers.WROTE_COPY,
        blockers.FRESH_DRAWN,
        blockers.PLAN_MOVED,
        blockers.NOTHING_ASKED,
        blockers.COPY_IN_PLACE,
        blockers.HEADLINE_READY,
        blockers.NO_HEADLINE_FIX,
        home_api.NOT_AN_ADJUSTMENT_HERE,
        not_yet.group(1),
    ):
        assert copy.jargon(text) is None, text
    assert '"Pick a look for your website."' in (Path(__file__).parents[1] / "static" / "home.js").read_text()
    assert copy.jargon("Pick a look for your website.") is None
