"""connect_google_ads -- since M0 Task 6 the alias of connect_channel("google_search", account_id=…), answering in its
shape -- and whoami's link fields over MCP (spec 2026-09-25 rev. 2 §3; spec 2026-09-29 §4). get_connection_status
reads every channel now: test_tools_channels.py."""

from agent import agreements, google_ads_link, keys, mcp_app, tools_channels
from agent.channels.google_search import adapter as google
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, call_tool, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_google_ads_link import CID, MANAGED, RESOURCE, failure, google_ads, pending_link

SETUP_URL = "http://testserver/?setup=biz_1"
GOOGLE_STEPS = [{"text": google.CONNECT_OWN_ACCOUNT, "url": SETUP_URL}]


def owner_key(conn, *, agree=True):
    """A claimed biz_1 owned by owner@example.com (who accepted the agreement by default) and a business key for it."""
    key = business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    if agree:
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    return key


def test_connect_google_ads_asks_for_the_agreement_first(client, conn):
    key = owner_key(conn, agree=False)
    res = call_tool(client, "connect_google_ads", {"customer_id": "123-456-7890", "idempotency_key": "l1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"] == {
        "status": "needs_agreement",
        "channel": "google_search",
        "label": "Google Search",
        "steps": GOOGLE_STEPS,
        "connect_url": SETUP_URL,
        "note": tools_channels.NEEDS_AGREEMENT_NOTE,
    }
    assert google_ads_link.get(conn, "biz_1") is None


def test_connect_google_ads_records_the_invitation_and_a_repeat_replays(client, conn):
    key = owner_key(conn)
    args = {"customer_id": "123 456 7890", "idempotency_key": "l1"}
    res = call_tool(client, "connect_google_ads", args, key=key)
    assert not res["isError"], tool_text(res)
    sc = res["structuredContent"]["data"]
    assert sc == {
        "status": "pending",
        "channel": "google_search",
        "label": "Google Search",
        "steps": GOOGLE_STEPS,
        "customer_id": "123-456-7890",
        "instructions": google_ads_link.instructions("manual"),
    }
    assert res["structuredContent"]["say"] == f"Invitation sent to 123-456-7890. {sc['instructions']}"
    assert call_tool(client, "connect_google_ads", args, key=key)["structuredContent"]["data"] == sc
    assert conn.execute("SELECT count(*) AS n FROM ads_link").fetchone()["n"] == 1
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM agent_action WHERE tool = 'connect_google_ads' AND idempotency_key IS NOT NULL"
        ).fetchone()["n"]
        == 1
    )
    assert sc["instructions"].count("manager account") == 1
    assert not JARGON.search(tool_text(res)) and not ACCOUNT_JARGON.search(tool_text(res))


def test_connect_google_ads_refuses_setup_keys_bad_numbers_and_taken_accounts(client, conn):
    pending_link(conn, "biz_other", email="other@example.com")
    key = owner_key(conn)
    draft, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    cases = ((draft, CID, "setup key"), (key, "12345", "10 digits"), (key, CID, "already linked to another business"))
    for i, (k, cid, text) in enumerate(cases):
        res = call_tool(client, "connect_google_ads", {"customer_id": cid, "idempotency_key": f"x{i}"}, key=k)
        assert res["isError"] and text in tool_text(res), tool_text(res)
    assert google_ads_link.get(conn, "biz_1") is None
    assert (
        conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'connect_google_ads' AND NOT ok").fetchone()[
            "n"
        ]
        == 3
    )


def test_connect_google_ads_in_api_mode_sends_through_the_adapter(client, conn, ads_api, monkeypatch):
    key = owner_key(conn)
    sent = []

    def fake_send(settings, customer_id, *, held=False, http=None):
        sent.append((settings.google_ads_link_mode, customer_id))
        return RESOURCE

    monkeypatch.setattr(google_ads_link, "send", fake_send)
    res = call_tool(client, "connect_google_ads", {"customer_id": "123-456-7890", "idempotency_key": "l1"}, key=key)
    assert res["structuredContent"]["data"]["instructions"] == google_ads_link.instructions("api")
    assert sent == [("api", CID)] and google_ads_link.get(conn, "biz_1").resource_name == RESOURCE

    def busy(settings, customer_id, *, held=False, http=None):
        raise google_ads_link.LinkError(google_ads_link.BUSY)

    monkeypatch.setattr(google_ads_link, "send", busy)
    res = call_tool(client, "connect_google_ads", {"customer_id": "0987654321", "idempotency_key": "l2"}, key=key)
    assert res["isError"] and google_ads_link.BUSY in tool_text(res)
    link = google_ads_link.get(conn, "biz_1")
    assert (link.customer_id, link.last_error) == (CID, None)  # the earlier invitation stands, its row untouched


def test_connect_google_ads_refuses_an_account_already_managed_elsewhere(client, conn, ads_api, monkeypatch):
    key = owner_key(conn)
    monkeypatch.setattr(google_ads_link, "client", lambda: google_ads([], mutate=failure(400, MANAGED)))
    res = call_tool(client, "connect_google_ads", {"customer_id": "123-456-7890", "idempotency_key": "l1"}, key=key)
    assert res["isError"] and google_ads_link.ALREADY_LINKED in tool_text(res), tool_text(res)
    assert google_ads_link.get(conn, "biz_1") is None
    assert not JARGON.search(google_ads_link.ALREADY_LINKED) and not ACCOUNT_JARGON.search(
        google_ads_link.ALREADY_LINKED
    )


def test_get_connection_status_reports_the_google_link_and_verification(client, conn):
    """M0 Task 6: the Google Search entry of every channel's list (the whole shape: test_tools_channels.py)."""
    key = owner_key(conn)
    res = call_tool(client, "get_connection_status", {}, key=key)["structuredContent"]["data"]
    assert (res["agreement_accepted"], res["verified"], res["connect_url"]) == (True, False, SETUP_URL)
    assert (res["channels"][0]["channel"], res["channels"][0]["status"]) == ("google_search", "none")
    google_ads_link.save(conn, "biz_1", CID, status="manual")
    conn.execute("UPDATE business SET claim_state = 'verified' WHERE id = 'biz_1'")
    res = call_tool(client, "get_connection_status", {}, key=key)["structuredContent"]["data"]
    g = res["channels"][0]
    assert res["verified"] and (g["account_public"], g["status"], g["mode"], g["next_step"]) == (
        "123-456-7890",
        "active",
        "own_account",
        None,
    )
    assert g["checked_at"]


def test_whoami_carries_the_ads_mode_and_the_link_status(client, conn):
    key = owner_key(conn)
    who = call_tool(client, "whoami", {}, key=key)["structuredContent"]["data"]
    assert (who["ads_mode"], who["google_ads_status"]) == ("own_account", None)
    google_ads_link.save(conn, "biz_1", CID, status="pending")
    who = call_tool(client, "whoami", {}, key=key)["structuredContent"]["data"]
    assert (who["ads_mode"], who["google_ads_status"]) == ("own_account", "pending")
    assert set(who) == {
        "business",
        "key",
        "connect_url",
        "ads_mode",
        "google_ads_status",
        "channels_connected",
        "balance_usd",
        "open_session_id",
    }


def test_the_link_tools_are_registered_documented_and_plain(client):
    by_name = {m.name: m for m in mcp_app.REGISTRY}
    c, s = by_name["connect_google_ads"], by_name["get_connection_status"]
    assert (c.cls, c.scope, c.approval) == ("W", "manage", "assistant") and (s.cls, s.scope, s.approval) == (
        "RO",
        "read",
        "none",
    )
    assert c.tool.parameters["required"] == ["customer_id", "idempotency_key"]
    docs = client.get("/connect/docs").text + client.get("/connect/llms.txt").text
    assert "needs_agreement" in docs and "<code>connect_account</code>" in docs
    assert JARGON.findall(docs) == [] and ACCOUNT_JARGON.findall(docs) == []
