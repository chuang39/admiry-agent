"""agent/leads.py: a lead's channel is the one its click id came from (spec 2026-09-29 §8); no click id, no channel."""

import pytest

from agent import landing, leads
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key


@pytest.mark.parametrize(
    ("click", "channel"),
    [
        (("Cj0abc", "gclid"), "google_search"),
        (("abc", "nb_cid"), "newsbreak"),
        (("fb1", "fbclid"), "meta"),
        ((None, None), None),  # organic
        (("x", "unheard_of"), None),  # a kind nothing maps
    ],
)
def test_a_lead_carries_the_channel_its_click_id_came_from(conn, click, channel):
    business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="claimed")
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    leads.record(conn, page, {"name": "Ana", "phone": "6505550199"}, click)
    row = conn.execute("SELECT click_id, click_id_kind, channel FROM lead").fetchone()
    assert (row["click_id"], row["click_id_kind"], row["channel"]) == (*click, channel)


def _three_leads(conn) -> str:
    """A Google Search lead (booked), a NewsBreak one and an organic one. Returns a key that sees leads."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="claimed")
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    for name, click in (("Ana", ("g1", "gclid")), ("Bo", ("nb1", "nb_cid")), ("Cy", (None, None))):
        leads.record(conn, page, {"name": name, "phone": "6505550199"}, click)
    conn.execute("UPDATE lead SET status = 'booked' WHERE channel = 'google_search'")
    return key


def test_every_lead_names_its_channel_and_click_id_and_the_list_filters_by_either(conn):
    """Spec 2026-09-29 §7.1: `channel` and `click_id` on every lead (the id is the owner's own to see);
    list_leads(channel=…) and (status=…) keep only those."""
    _three_leads(conn)
    every = leads.list_for(conn, "biz_1", None)
    assert {(x["channel"], x["click_id"], x["click"]) for x in every} == {
        ("google_search", "g1", True),
        ("newsbreak", "nb1", True),
        (None, None, False),
    }
    assert [x["click_id"] for x in leads.list_for(conn, "biz_1", None, channel="newsbreak")] == ["nb1"]
    assert [x["channel"] for x in leads.list_for(conn, "biz_1", None, status="booked")] == ["google_search"]
    assert leads.list_for(conn, "biz_1", None, status="booked", channel="newsbreak") == []


def test_the_leads_tool_filters_by_channel_and_status_and_refuses_what_it_does_not_know(client, conn):
    key = _three_leads(conn)
    res = call_tool(client, "list_leads", {"channel": "newsbreak"}, key=key)
    assert not res["isError"], tool_text(res)
    assert [x["channel"] for x in data(res)["leads"]] == ["newsbreak"]
    assert res["structuredContent"]["say"] == "1 lead(s) from NewsBreak, newest first."
    booked = data(call_tool(client, "list_leads", {"status": "booked"}, key=key))["leads"]
    assert [x["click_id"] for x in booked] == ["g1"]
    for args, text in (({"channel": "tiktok"}, "unknown channel"), ({"status": "maybe"}, "status must be one of")):
        bad = call_tool(client, "list_leads", args, key=key)
        assert bad["isError"] and text in tool_text(bad), args


def test_the_list_carries_what_the_form_asked_and_the_audit_never_the_zip(client, conn):
    """The founder's dev walk, 2026-10-08: the lead e-mail said the visitor's ZIP code and new-or-existing patient,
    the list did not. Both are on each lead now; the ZIP, where the visitor is, stays out of the audit row as the
    contact does."""
    key = business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="claimed")
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    leads.record(
        conn, page, {"name": "Ana", "phone": "6505550199", "zip": "94306", "patient": "New patient"}, (None, None)
    )
    leads.record(conn, page, {"name": "Bo", "phone": "6505550198"}, (None, None))
    res = call_tool(client, "list_leads", {}, key=key)
    assert not res["isError"], tool_text(res)
    by_name = {x["contact"]["name"]: x for x in data(res)["leads"]}
    assert (by_name["Ana"]["zip"], by_name["Ana"]["patient"]) == ("94306", "New patient")
    assert (by_name["Bo"]["zip"], by_name["Bo"]["patient"]) == (None, None)
    row = conn.execute("SELECT result::text AS r FROM agent_action WHERE tool = 'list_leads'").fetchone()
    assert "94306" not in row["r"] and "New patient" in row["r"]
