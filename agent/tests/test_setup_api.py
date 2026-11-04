"""POST /api/setup (spec 2026-09-29 setup-in-conversation §3): the signed-in owner's setup actions from the home."""

from agent import (  # noqa: F401 - mcp_app first: connect<->surface.told cycle
    agreements,
    google_ads_link,
    legal,
    mcp_app,
)
from agent.tests.test_google_ads_link import RESOURCE
from agent.tests.test_web import claimed_business, csrf_of


def setup_call(client, action, **body):
    return client.post("/api/setup", json={"action": action, **body})


def test_without_a_web_session_it_asks_for_sign_in(client, conn):
    r = setup_call(client, "state", business_id="biz_1")
    assert r.status_code == 401 and r.json()["signin"] is True and r.headers["cache-control"] == "no-store"


def test_state_needs_no_csrf_but_every_change_does(client, conn, google):
    claimed_business(client, conn, google)
    r = setup_call(client, "state", business_id="biz_1")
    assert r.status_code == 200 and r.json()["state"]["step"] == "choose"
    assert setup_call(client, "choose", business_id="biz_1", mode="managed").status_code == 403
    assert setup_call(client, "choose", business_id="biz_1", mode="managed", csrf="wrong").status_code == 403
    for odd in (123, ["x"], {"a": 1}, None):  # not a string: refused, never a 500
        assert setup_call(client, "choose", business_id="biz_1", mode="managed", csrf=odd).status_code == 403


def test_another_owners_business_is_gone(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("INSERT INTO business (id, owner_email, claim_state) VALUES ('biz_2', 'other@example.com', 'claimed')")
    for biz in ("biz_2", "biz_nope", 7):
        r = setup_call(client, "state", business_id=biz)
        assert r.status_code == 404 and r.json()["gone"] is True


def test_the_prepaid_path_end_to_end(client, conn, google, stripe):
    claimed_business(client, conn, google)
    csrf = csrf_of(client)
    r = setup_call(client, "choose", business_id="biz_1", mode="managed", csrf=csrf).json()
    assert r["ok"] and r["state"]["step"] == "accept" and len(r["state"]["documents"]) == 2
    docs = {d["id"]: d["version"] for d in r["state"]["documents"]}
    r = setup_call(client, "accept", business_id="biz_1", documents=docs, csrf=csrf).json()
    assert r["ok"] and r["state"]["step"] == "pay" and agreements.terms_accepted(conn, "biz_1")
    r = setup_call(client, "pay", business_id="biz_1", amount_usd="250", csrf=csrf).json()
    assert r == {"ok": True, "url": "https://checkout.stripe.com/c/pay/cs_test_1"}


def test_the_pay_route_takes_a_rail(client, conn, google, stripe, monkeypatch):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    claimed_business(client, conn, google)
    csrf = csrf_of(client)
    r = setup_call(client, "choose", business_id="biz_1", mode="managed", csrf=csrf).json()
    docs = {d["id"]: d["version"] for d in r["state"]["documents"]}
    setup_call(client, "accept", business_id="biz_1", documents=docs, csrf=csrf)
    r = setup_call(client, "pay", business_id="biz_1", amount_usd="500", rail="card", csrf=csrf).json()
    assert r == {"ok": True, "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
    assert conn.execute("SELECT rail FROM payment_session WHERE id = 'cs_test_1'").fetchone()["rail"] == "card"
    # finding 9: the route must carry "ach" through to setup.pay just as it does "card" -- not only the direct
    # setup.pay() calls test_managed_mode.py already covers
    r = setup_call(client, "pay", business_id="biz_1", amount_usd="500", rail="ach", csrf=csrf).json()
    assert r["ok"] is True
    rail = conn.execute(
        "SELECT rail FROM payment_session WHERE id = (SELECT id FROM payment_session ORDER BY created_at DESC LIMIT 1)"
    ).fetchone()["rail"]
    assert rail == "ach"


def test_the_own_account_path_and_a_refusal(client, conn, google, monkeypatch):
    claimed_business(client, conn, google)
    csrf = csrf_of(client)
    setup_call(client, "choose", business_id="biz_1", mode="own_account", csrf=csrf)
    r = setup_call(client, "accept", business_id="biz_1", documents={}, csrf=csrf)
    assert r.status_code == 400 and r.json() == {"ok": False, "error": "Tick every box to accept."}
    setup_call(client, "accept", business_id="biz_1", documents={legal.AGREEMENT: "v1"}, csrf=csrf)
    monkeypatch.setattr(google_ads_link, "send", lambda settings, cid, held=False, http=None: RESOURCE)
    r = setup_call(client, "link", business_id="biz_1", customer_id="123-456-7890", csrf=csrf).json()
    assert r["state"]["step"] == "link" and r["state"]["link"]["status"] == "pending"


def test_without_a_business_id_it_uses_the_visitors_own_business(client, conn, google, monkeypatch):
    from agent import home_api

    claimed_business(client, conn, google)
    monkeypatch.setattr(home_api, "_visitor", lambda conn, request: (object(), {"id": "biz_1"}))
    r = setup_call(client, "state")
    assert r.status_code == 200 and r.json()["state"]["business_id"] == "biz_1"
    monkeypatch.setattr(home_api, "_visitor", lambda conn, request: (None, None))
    assert setup_call(client, "state").status_code == 404


def test_unknown_actions_are_400(client, conn, google):
    claimed_business(client, conn, google)
    assert setup_call(client, "fly", business_id="biz_1").status_code == 400
