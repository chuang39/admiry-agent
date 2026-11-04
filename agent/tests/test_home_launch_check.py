"""L1 Task 4 (spec 2026-10-10 §4.2): the home's list -- GET /api/launch_check for the signed-in owner of a claimed
business -- and the home answers without B0's single blocker: typed words answer the list's first question, a fix
names its item, and a funded draft's yes is the plan card's Confirm."""

from pathlib import Path

import pytest

from agent import blockers, business, plans, tools_consult
from agent.tests.test_blockers_home import _biz, _csrf, _fix, _knows, _plan_id
from agent.tests.test_home_api import IP, _answer_all, _claimed_plan, fake, turn  # noqa: F401 -- `fake` is a fixture
from agent.tests.test_pages import _dropped


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")


def _list(client) -> dict:
    r = client.get("/api/launch_check", headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()


def _ids(client) -> list[str]:
    return [i["id"] for i in _list(client)["check"]["items"]]


def test_the_list_of_a_claimed_draft_and_a_state_without_a_blocker(client, conn, fake, google):  # noqa: F811
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    out = _list(client)
    assert out["ok"] and out["check"]["plan_id"] == plans.latest(conn, biz)["id"]
    phone = next(i for i in out["check"]["items"] if i["id"] == "phone")
    assert phone["kind"] == "ask" and phone["blocking"] and out["check"]["ready"] is False
    state = turn(client, {"action": "state"}).json()["result"]
    assert "blocker" not in state and state["plan_status"] == "draft"


def test_a_draft_key_gets_no_list(client, conn, fake):  # noqa: F811
    from agent import benchmark

    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "i1"})
    _answer_all(client)
    assert _list(client)["ok"] is False


def test_typed_words_answer_the_lists_first_question(client, conn, fake, google, switched_on):  # noqa: F811
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    said = turn(client, {"action": "say", "text": "(650) 555-0100", "idempotency_key": "s1"}).json()
    assert said["ok"] and said["result"]["say"] == blockers.SAVED_PHONE.format(phone="(650) 555-0100")
    assert "phone" not in _ids(client)


def test_a_fix_names_its_item_and_a_stale_one_answers_with_the_state(client, conn, fake, google):  # noqa: F811
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    plan_id, csrf = _plan_id(conn, biz), _csrf(client)
    out = _fix(client, csrf, action="answer", id="phone", plan_id=plan_id, text="(650) 555-0100")
    assert out["ok"] and "blocker" not in out["result"]["state"]
    assert business.latest_knowledge(conn, biz)[1]["phone"] == "(650) 555-0100"
    stale = _fix(client, csrf, action="answer", id="phone", plan_id=plan_id, text="(650) 555-0199")
    assert stale["ok"] and stale["result"]["say"] == blockers.NOTHING_ASKED  # no phone question any more


def test_a_funded_drafts_yes_is_confirm(client, conn, fake, google):  # noqa: F811
    from agent import agreements, legal
    from agent.tests.test_wallet import fund

    result = _claimed_plan(client, conn, google, prepaid=True)
    assert "confirm" not in [a["id"] for a in result["next"]["actions"]]  # no money yet: the ways to run the ads
    biz = turn(client, {"action": "state"}).json()["result"]["business_id"]  # the walk's own business
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    fund(conn, biz=biz, cents=126000)
    ids = [a["id"] for a in turn(client, {"action": "state"}).json()["result"]["next"]["actions"]]
    assert "confirm" in ids and "prepay" not in ids  # the website's ways (when its row offers them) still lead
    changed = turn(client, {"action": "adjust", "monthly_budget_usd": 1500, "idempotency_key": "a2"}).json()
    assert "confirm" in [a["id"] for a in changed["result"]["next"]["actions"]]
    assert tools_consult.CONFIRM_ACTION["label"] == "Confirm and launch"


def test_a_secondary_places_ask_item_is_answered_without_a_500(conn):
    """review Finding 1 / ruling R10: `home_api._fix`'s "answer" branch (what `/api/blocker` delegates to once past
    its session gate -- the finding's own fallback, a NewsBreak-only claimed walk through the home being
    impractical here) used to KeyError on `blockers.SHAPES[id]` for an ask item credited to a secondary place: its
    id carried a `@place` suffix neither `blockers.SHAPES` nor `blockers.read_answer` ever keyed on -- an uncaught
    exception, a 500 at /api/blocker. Built the way test_launch_check's own regression test is (test_ads_run's
    seeded business, a NewsBreak-only plan, no phone on file): the checklist's phone item now carries the bare id,
    and answering it runs clean, saving the phone."""
    from agent import config, home_api, launch_check
    from agent.tests.test_ads_run import BIZ, _knowledge, seed
    from agent.tests.test_review_launch import draft_sections

    secs = draft_sections()
    secs["ads"]["channels"] = [
        {"channel": "newsbreak", "monthly_usd": 300.0, "headlines": [], "descriptions": [], "themes": []}
    ]
    key, _pid = seed(conn, status="draft", secs=secs)
    _knowledge(conn, phone=None)
    settings = config.load()
    found = next(i for i in launch_check.check(conn, settings, BIZ)["items"] if i["resolution"] == "phone")
    assert found["id"] == "phone" and found["channel"] == "newsbreak"  # not "phone@newsbreak": answerable (R10)
    body = {"id": found["id"], "plan_id": found["plan_id"], "text": "(650) 555-0100"}
    say, more = home_api._fix("answer", settings, BIZ, found, body, key, "198.51.100.9", "owner@example.com")
    assert say == blockers.SAVED_PHONE.format(phone="(650) 555-0100") and more == {}
    assert business.latest_knowledge(conn, BIZ)[1]["phone"] == "(650) 555-0100"


def test_words_that_are_no_change_get_the_in_place_reply_while_a_non_ask_item_stands(
    client,
    conn,
    fake,  # noqa: F811
    google,
    switched_on,
):
    """review Finding 2 / ruling R11: TOO_LITTLE_COPY blocks (a "fix" item; no question is open) -- words the
    adjust read rejects used to send the owner to "continue in your assistant" (B0 P7 never meant that while
    anything on the list stands); now they get the in-place reply, since the list still holds the write_copy item."""
    from agent import home_api
    from agent.tests.test_review_launch import _without_copy

    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    _without_copy(conn, biz)
    assert any(i["id"] == "write_copy" for i in _list(client)["check"]["items"])
    result = turn(client, {"action": "say", "text": "what colour is the sky", "idempotency_key": "s1"}).json()["result"]
    assert "resolved" not in result
    assert result["say"] == home_api.NOT_AN_ADJUSTMENT_HERE and "continue in your assistant" not in result["say"]


def test_the_state_carries_what_admiry_removed_and_the_card_has_its_line(client, conn, fake, google):  # noqa: F811 - fake is a fixture used by name
    """spec 2026-10-11 §5.1: the home's state read lists the lines, and the plan card has the part and the script that
    draws them (in review and once live)."""
    _claimed_plan(client, conn, google)
    biz = _biz(conn)
    assert turn(client, {"action": "state"}).json()["result"]["changes"] == []
    _dropped(conn, plans.latest(conn, biz)["id"], biz)
    state = turn(client, {"action": "state"}).json()["result"]
    assert state["changes"] == ["Removed the headline “Same-Week Appointments”: a name Google treats as a trademark."]
    assert 'data-part="changes"' in client.get("/").text
    js = (Path(__file__).parents[1] / "static" / "home.js").read_text()
    assert "function changesLine" in js and "changesLine(n);" in js and "Changes Admiry made" in js
