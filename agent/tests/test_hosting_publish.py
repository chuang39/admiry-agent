"""The hosting minimum where the owner meets it (A3p-3 spec §3.2, §3.3, §4, §7; plan 2026-10-04-agent-a3p-3-1 Q8-Q15):
the publish request's sentence and the date to start the ads by, the countdown a publish starts and a republish
keeps, coming back after a takedown, the status block, the page's source and its download, the kit's hosting
section, the revise that asks for more than the template holds, the page an assistant took over, and the homepage's
page card."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from agent import config, google_ads_link, hosting_minimum, landing, mail, models, page_builder, tools_hosting
from agent.channels import registry
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.fixtures import FakeGenerator, page_copy_fixture
from agent.tests.test_ads_run import clock, seed  # noqa: F401 (fixtures)
from agent.tests.test_home_api import IP, PAGES_DOMAIN, _checked_transport, _planned
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import (
    GOOD_HTML,
    GOOD_HTML_V2,
    HOSTNAME,
    URL,
    _checked,
    _hosted_biz,
    _mailbox,
    _publish,
)
from agent.tests.test_web import sign_in

DAY0 = date(2026, 10, 4)
ENDS = DAY0 + timedelta(days=30)  # November 3, 2026
START_BY = "Start your ads by November 3, 2026 to keep the site online."


def _on(day: date, clock_fn) -> None:
    clock_fn(datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC))


def _row(conn, biz: str) -> dict:
    return dict(conn.execute("SELECT * FROM page WHERE business_id = %s", (biz,)).fetchone())


def _ready(conn, biz: str) -> None:
    """The plan approved and the owner's own Google Ads account linked: ready to be covered (P17)."""
    conn.execute("UPDATE plan SET status = 'approved' WHERE business_id = %s", (biz,))
    google_ads_link.save(conn, biz, "1234567890", status="active")
    registry.save_account(
        conn, biz, "google_search", mode="own_account", status="active", external={"customer_id": "1234567890"}
    )


def _error(res: dict) -> str:
    assert res["isError"], res
    return tool_text(res)


# ---------------------------------------------------------------- the publish request and the countdown it starts


def test_a_first_publish_without_ads_starts_the_countdown_without_an_email(  # Review Focus 1
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    """No ads run: the publish request says the minimum and the date to start the ads by; confirming starts the
    countdown on that date with no start e-mail (the request already named it); the status shows the date."""
    _on(DAY0, clock)
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    sent = _mailbox(monkeypatch)
    prev = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))
    res = call_tool(client, "publish_page", {"idempotency_key": "pp1", "token": prev["token"]}, key=key)
    prop = data(res)
    note = f"{hosting_minimum.MINIMUM} {START_BY}"
    assert res["structuredContent"]["say"].endswith(note) and note in prop["what_changes"]
    _checked(monkeypatch, conn, URL)
    done = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c1"}, key=key)
    assert not done["isError"], tool_text(done)
    page = _row(conn, biz)
    assert (page["countdown_ends"], page["countdown_mail"]) == (ENDS, {"start": "publish"})
    stays = hosting_minimum.STAYS_UNTIL.format(host=HOSTNAME, date="November 3, 2026")
    assert data(done)["hosting"]["countdown_ends"] == ENDS.isoformat() and done["structuredContent"]["say"].endswith(
        stays
    )
    assert [r for r in sent if "Free hosting" in r.content.decode()] == []  # the check's report alone, no start mail
    status = call_tool(client, "get_page", {}, key=key)
    assert status["structuredContent"]["say"].endswith(stays)
    block = data(status)["page"]["hosting"]
    assert (block["covered"], block["why"], block["countdown_ends"], block["exempt"]) == (
        False,
        "short",
        ENDS.isoformat(),
        False,
    )


def test_a_republish_keeps_the_countdown(  # Review Focus 4
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    """A new version ten days later keeps the date; the owner's own unpublish keeps it too, and publishing again once
    it has passed needs the ads ready."""
    _on(DAY0, clock)
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    _on(DAY0 + timedelta(days=10), clock)
    prev = data(call_tool(client, "preview_page", {"html": GOOD_HTML_V2}, key=key))
    res = call_tool(client, "publish_page", {"idempotency_key": "ppx", "token": prev["token"]}, key=key)
    assert res["structuredContent"]["say"].endswith(START_BY)  # the running countdown's own date
    _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "2")
    assert _row(conn, biz)["countdown_ends"] == ENDS
    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))
    call_tool(client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "cu"}, key=key)
    page = _row(conn, biz)
    assert (page["status"], page["taken_down_reason"], page["countdown_ends"], page["purge_after"]) == (
        "unpublished",
        "owner",
        ENDS,
        None,
    )
    _on(ENDS, clock)
    refused = call_tool(client, "publish_page", {"idempotency_key": "pp3", "version": 2}, key=key)
    assert hosting_minimum.COMEBACK in _error(refused)


def test_publishing_again_after_a_takedown_needs_ads_ready(  # Review Focus 3
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    _on(DAY0, clock)
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    conn.execute(  # the job's takedown (test_hosting_job.py drives it): down for the minimum, kept 90 days
        "UPDATE page SET status = 'unpublished', taken_down_reason = 'hosting_minimum', taken_down_at = now(), "
        "purge_after = %s, countdown_ends = NULL, countdown_mail = NULL WHERE business_id = %s",
        (ENDS + timedelta(days=90), biz),
    )
    _on(ENDS + timedelta(days=5), clock)
    status = call_tool(client, "get_page", {}, key=key)
    offline = hosting_minimum.OFFLINE.format(host=HOSTNAME, date="February 1, 2027")
    assert status["structuredContent"]["say"].endswith(offline)
    assert data(status)["page"]["hosting"]["taken_down"] == "hosting_minimum"
    assert data(status)["page"]["hosting"]["keep_until"] == (ENDS + timedelta(days=90)).isoformat()
    refused = call_tool(client, "publish_page", {"idempotency_key": "pp2", "version": 1}, key=key)
    assert hosting_minimum.COMEBACK in _error(refused)
    _ready(conn, biz)
    assert hosting_minimum.ready_usd(conn, config.load(), biz) >= 200
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pp3", "version": 1}, key=key))
    done = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c3"}, key=key)
    assert not done["isError"], tool_text(done)
    page = _row(conn, biz)
    assert (page["status"], page["taken_down_reason"], page["purge_after"]) == ("published", None, None)
    # back on a plan that is ready but whose ads are not live yet: a new 30 days from today
    assert page["countdown_ends"] == ENDS + timedelta(days=35)


def test_an_exempt_business_reads_nothing_of_the_minimum(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    _on(DAY0, clock)
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    hosting_minimum.set_exempt(conn, biz, "the pilot", by="ops:cli")
    prev = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))
    res = call_tool(client, "publish_page", {"idempotency_key": "pp1", "token": prev["token"]}, key=key)
    assert "$200" not in res["structuredContent"]["say"] and "$200" not in " ".join(data(res)["what_changes"])
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "2")
    assert _row(conn, biz)["countdown_ends"] is None
    assert data(call_tool(client, "get_page", {}, key=key))["page"]["hosting"]["exempt"] is True


def test_a_covered_business_reads_the_minimum_alone(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    _on(DAY0, clock)
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _ready(conn, biz)
    plan_id = conn.execute("SELECT id FROM plan WHERE business_id = %s", (biz,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) VALUES (%s, %s, %s, 'live', %s)",
        (biz, "google_search", plan_id, Jsonb({"campaign": "customers/1/campaigns/1"})),
    )
    assert hosting_minimum.coverage(conn, biz).why == "live_budget"
    prev = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))
    res = call_tool(client, "publish_page", {"idempotency_key": "pp1", "token": prev["token"]}, key=key)
    assert res["structuredContent"]["say"].endswith(hosting_minimum.MINIMUM) and "Start your ads" not in tool_text(res)


# ---------------------------------------------------------------- the source (P19, P23) and its download


def test_the_source_is_the_live_html_then_the_kept_one(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    res = call_tool(client, "get_landing_page", {"view": "source"}, key=key)
    src = data(res)
    assert (src["kind"], src["version"], src["hostname"], src["live"]) == ("html", 1, HOSTNAME, True)
    assert "Invisalign in Palo Alto" in src["html"] and 'data-page="pg_' in src["html"]  # the snippet as it is served
    assert f"live at {URL}" in res["structuredContent"]["say"] and not JARGON.search(res["structuredContent"]["say"])
    stored = conn.execute(
        "SELECT result FROM agent_action WHERE tool = 'get_page_source' ORDER BY id DESC LIMIT 1"
    ).fetchone()["result"]
    assert stored["data"]["html"] == "***"  # a megabyte the audit row never keeps
    conn.execute("UPDATE page SET status = 'unpublished' WHERE business_id = %s", (biz,))
    assert data(call_tool(client, "get_page_source", {}, key=key))["live"] is False  # kept while down
    conn.execute("UPDATE page SET html = NULL, hostname = NULL, url = '' WHERE business_id = %s", (biz,))  # purged
    assert _error(call_tool(client, "get_page_source", {}, key=key)).startswith("Admiry hosts no page")


def test_the_source_is_refused_for_the_owners_own_page(client, conn):
    key, _plan_id = seed(conn)  # the owner's own page at their own address
    assert _error(call_tool(client, "get_page_source", {}, key=key)).startswith("Admiry hosts no page")


def test_the_download_asks_for_sign_in_and_hands_the_owner_their_own_site(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    google,
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    anonymous = client.get(f"/connect/site?b={biz}")
    assert anonymous.status_code == 200 and f"/connect/google/start?next=/connect/site%3Fb%3D{biz}" in anonymous.text
    google.email = "owner@example.com"
    sign_in(client, f"/connect/site?b={biz}")
    got = client.get(f"/connect/site?b={biz}")
    assert got.status_code == 200 and got.headers["content-disposition"] == f'attachment; filename="{HOSTNAME}.html"'
    assert got.headers["content-security-policy"].startswith("sandbox") and "Invisalign in Palo Alto" in got.text
    assert client.get("/connect/site?b=biz_someone_else").status_code == 404
    conn.execute("UPDATE page SET source = 'owner', hostname = NULL WHERE business_id = %s", (biz,))
    assert client.get(f"/connect/site?b={biz}").status_code == 404


# ---------------------------------------------------------------- the kit says it before building (P15)


def test_the_kit_carries_the_hosting_minimum(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    _key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    kit = landing.kit(conn, biz)
    assert kit["hosting"]["minimum_usd"] == 200 and kit["hosting"]["free_while"] == hosting_minimum.MINIMUM
    assert kit["hosting"]["say_first"].startswith("Before you build a page Admiry will host, tell the owner: ")
    text = mail.render("mail_kit.txt.j2", kit=kit)
    assert "HOSTING ON ADMIRY\n" + hosting_minimum.MINIMUM in text


# ---------------------------------------------------------------- more than the template holds (P22)


def _beyond_fake(monkeypatch) -> FakeGenerator:
    from agent import llm

    gen = FakeGenerator(
        copies=[page_copy_fixture(), models.PageRevision(**page_copy_fixture().model_dump(), beyond_template=True)]
    )
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    return gen


def test_a_change_beyond_the_template_changes_nothing_and_points_to_the_source(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _beyond_fake(monkeypatch)
    made = data(call_tool(client, "create_page", {}, key=key))
    before = _row(conn, biz)["sections"]
    res = call_tool(client, "revise_page", {"instruction": "add a second page about braces"}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res) == {"beyond_template": True, "preview_url": None, "token": None}
    assert res["structuredContent"]["say"] == page_builder.BEYOND
    assert res["structuredContent"]["next"]["actions"][0]["tool"] == "get_page_source"
    assert _row(conn, biz)["sections"] == before  # nothing written
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 1  # create_page's alone
    public = call_tool(
        client,
        "update_landing_page",
        {"action": "revise", "instruction": "a chat box", "idempotency_key": "r2"},
        key=key,
    )
    nxt = public["structuredContent"]["next"]["actions"][0]
    assert (nxt["tool"], nxt["args"]) == ("get_landing_page", {"view": "source"})
    assert made["token"]


# ---------------------------------------------------------------- the assistant takes the page over (P23)


def test_once_the_assistants_page_is_live_the_quick_changes_stop(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _beyond_fake(monkeypatch)
    call_tool(client, "create_page", {}, key=key)  # Admiry's page first ...
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")  # ... then the assistant's own HTML, live
    assert _row(conn, biz)["source"] == "hosted"
    revise = call_tool(client, "revise_page", {"instruction": "warmer"}, key=key)
    assert _error(revise) == tools_hosting.ASSISTANTS_PAGE
    edit = call_tool(client, "update_page", {"section": "headline", "text": "Invisalign in Palo Alto"}, key=key)
    assert _error(edit) == tools_hosting.ASSISTANTS_PAGE
    assert not call_tool(client, "create_page", {}, key=key)["isError"]  # a new page from Admiry, on request


# ---------------------------------------------------------------- the homepage's page card


@pytest.fixture
def card(client, conn, fake, google, monkeypatch, resend):  # noqa: F811 - the fixture shadows the imported name
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", PAGES_DOMAIN)
    csrf = _planned(client, conn, google)
    _checked_transport(monkeypatch, conn)
    return csrf


def test_the_card_says_the_minimum_then_invites_the_owner_to_muse(client, conn, card):
    created = client.post("/api/page/create", json={"csrf": card}, headers=IP).json()
    assert created["hosting_note"].startswith(hosting_minimum.MINIMUM + " Start your ads by ")
    assert created["muse"] is None and created["download_url"] is None
    published = client.post("/api/page/publish", json={"csrf": card, "token": created["token"]}, headers=IP).json()
    assert published["ok"] and published["muse"] == (
        "Want more pages, your own design or online booking? Continue in Muse: it can change this site with you."
    )
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert published["download_url"] == f"http://testserver/connect/site?b={biz}"
    assert re.search(r"stays online until \w+ \d+, \d{4}", published["hosting"]["say"])
    state = client.post("/api/page/state", json={"csrf": card}, headers=IP).json()["page"]
    assert (state["managed_by"], state["muse"], state["download_url"]) == (
        "admiry",
        published["muse"],
        published["download_url"],
    )


def test_the_card_answers_a_change_beyond_the_template_with_muse(client, conn, card, monkeypatch):
    client.post("/api/page/create", json={"csrf": card}, headers=IP)
    _beyond_fake(monkeypatch)
    r = client.post("/api/page/revise", json={"csrf": card, "instruction": "add a booking calendar"}, headers=IP)
    first = r.json()  # the fake's first copy is plain: a quick change
    assert first["ok"] and first["token"]
    r = client.post("/api/page/revise", json={"csrf": card, "instruction": "add a booking calendar"}, headers=IP)
    assert r.json() == {"ok": True, "beyond_template": True, "say": page_builder.BEYOND}


def test_the_card_says_the_assistant_manages_its_own_page(client, conn, card):
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    page = landing.ensure_page_for_kit(conn, biz)
    conn.execute(
        "UPDATE page SET source = 'hosted', status = 'published', hostname = %s, url = %s, html = %s WHERE id = %s",
        (HOSTNAME, URL, GOOD_HTML, page["id"]),
    )
    state = client.post("/api/page/state", json={"csrf": card}, headers=IP).json()["page"]
    assert (state["managed_by"], state["say"], state["url"]) == (
        "assistant",
        "Your assistant manages this site now.",
        URL,
    )
    assert "token" not in state and "preview_url" not in state  # no preview, no quick changes


def test_the_home_page_carries_the_minimum_and_the_muse_invitation(client):
    html = client.get("/").text
    # beside "Build your business website", as its fine print (founder's words, 2026-10-07)
    assert f'<p class="fine" data-part="page-intro">{hosting_minimum.MINIMUM}</p>' in html
    assert 'data-part="muse"' in html and 'id="t-page-managed"' in html and 'data-part="download"' in html


# ---------------------------------------------------------------- A3p-3 batch 2: the page Admiry writes measures it


def test_the_page_admiry_writes_marks_its_forms_intent_and_the_kit_says_how(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    """P25: a dentist's page asks for an appointment (its recipe's intent), whatever template it was written with;
    the kit tells an assistant the attribute, the Calendly booking and the phone as printed (C9)."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    kit = landing.kit(conn, biz)
    assert kit["rules"]["form_intent"] == "appointment"
    assert kit["tracking"]["intent"].startswith('Mark the form data-admiry-intent="appointment"')
    assert "Calendly" in kit["tracking"]["booking"] and "forwarding number" in kit["tracking"]["phone"]
    made = data(call_tool(client, "create_page", {}, key=key))
    html = conn.execute("SELECT html FROM page_preview WHERE token = %s", (made["token"],)).fetchone()["html"]
    assert 'data-admiry-intent="appointment"' in html


def test_the_snippet_carries_the_phone_once_the_account_has_the_call_action(conn):
    """P26: the page's phone, exactly as the facts give it, rides on the snippet once the account has the call-from-site
    action -- never before, and never without a phone."""
    seed(conn)  # the account's tag and every action
    conn.execute("DELETE FROM conversion_action WHERE kind = 'call_site'")  # an account set up before A3p-3
    assert "phone" not in landing.google_tag(conn, "biz_1")
    conn.execute(
        "INSERT INTO conversion_action (business_id, kind, resource_name, label) VALUES ('biz_1', 'call_site', "
        "'customers/1234567890/conversionActions/99', 'CallLbl')"
    )
    tag = landing.google_tag(conn, "biz_1")
    assert tag["phone"] == "650-555-0100" and tag["labels"]["call_site"] == "CallLbl"
    assert ' data-phone="650-555-0100"' in landing.snippet("pg_1", "https://agent.test", tag)
