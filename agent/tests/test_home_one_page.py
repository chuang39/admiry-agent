"""B0 follow-up R1: every home request names the business its page shows (`X-Admiry-Business`, from the answers'
`visitor.business_id`); a write from a page that shows another business than the visitor cookie's -- a tab left open
while another tab switched the business -- is refused with a reload sentence before anything is written. A reload
(`state`) answers, and names the business the page should now show."""

from agent import business, home_api
from agent.tests.test_blockers_home import (  # noqa: F401 -- `switched_on` is a fixture
    _biz,
    _csrf,
    _knows,
    _plan_id,
    switched_on,
)
from agent.tests.test_home_api import IP, _claimed_plan, fake, turn  # noqa: F401 -- `fake` is a fixture used by name

REFUSED = {"ok": False, "error": home_api.OTHER_PAGE, "reload": True}


def _on(page: str) -> dict:
    return {**IP, home_api.PAGE_HEADER: page}


def _phone_answer(client, conn) -> dict:
    biz = _biz(conn)
    _knows(conn, biz, phone=None)
    return {
        "csrf": _csrf(client),
        "action": "answer",
        "id": "phone",
        "plan_id": _plan_id(conn, biz),
        "text": "(650) 555-0100",
    }


def test_every_answer_names_the_visitors_business(client, conn, fake, google):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    assert turn(client, {"action": "state"}).json()["visitor"]["business_id"] == _biz(conn)


def test_a_write_from_a_page_showing_another_business_is_refused_and_writes_nothing(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    switched_on,  # noqa: F811 -- the fixture
):
    _claimed_plan(client, conn, google)
    body = _phone_answer(client, conn)
    version = business.latest_knowledge(conn, _biz(conn))[0]
    assert client.post("/api/blocker", json=body, headers=_on("biz_other")).json() == REFUSED
    assert business.latest_knowledge(conn, _biz(conn))[0] == version
    assert client.post("/api/blocker", json=body, headers=_on(_biz(conn))).json()["ok"]  # its own business: answered


def test_a_turn_from_another_businesss_page_is_refused_but_its_reload_answers(client, conn, fake, google):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    adjust = {"action": "adjust", "level": "conservative", "idempotency_key": "r1"}
    assert client.post("/api/turn", json=adjust, headers=_on("biz_other")).json() == REFUSED
    state = client.post("/api/turn", json={"action": "state"}, headers=_on("biz_other")).json()
    assert state["ok"] and state["visitor"]["business_id"] == _biz(conn)


def test_a_page_route_from_another_businesss_page_is_refused(client, conn, fake, google):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    assert client.post("/api/page/create", json={"csrf": _csrf(client)}, headers=_on("biz_other")).json() == REFUSED


def test_a_page_that_names_no_business_is_not_checked(client, conn, fake, google, switched_on):  # noqa: F811 -- the fixture
    _claimed_plan(client, conn, google)
    assert client.post("/api/blocker", json=_phone_answer(client, conn), headers=IP).json()["ok"]  # an older page


def test_a_start_from_another_businesss_page_is_refused_and_supersedes_nothing(client, conn, fake, google):  # noqa: F811 -- the fixture
    """F4: a keyed `start` -- a live cookie -- from a page showing another business would supersede the cookie
    business's consultation; it is refused before anything is written."""
    _claimed_plan(client, conn, google)
    sessions = conn.execute("SELECT count(*) AS n FROM consult_session").fetchone()["n"]
    body = {"action": "start", "text": "a bakery in Palo Alto", "idempotency_key": "s1"}
    assert client.post("/api/turn", json=body, headers=_on("biz_other")).json() == REFUSED
    assert conn.execute("SELECT count(*) AS n FROM consult_session").fetchone()["n"] == sessions
