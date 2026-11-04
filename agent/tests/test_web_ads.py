"""Google Search ads status and the Pause/Resume buttons (the Your ads page, /ads, since YA; before it /connect) and the
monthly statement (/connect's Billing card) (spec 2026-09-28 admiry-google-search-execution-design.md §6; A3 Task 5)."""

from __future__ import annotations

import re
from html import unescape

from psycopg.types.json import Jsonb

# mcp_app must be imported (directly, not through .connect) before agent.web/.connect are: importing .connect
# first starts a real circular import the other way (agent/pages.py's home_page has the same note). Importing
# it here, before agent.tests.test_web pulls in agent.web, keeps this module import-order-independent.
from agent import ads_run, db, google_ads_link, mcp_app  # noqa: F401
from agent.channels import registry
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, without_site_header
from agent.tests.test_ads_run import CHECK_OK, GOOGLE, PAGE_URL, add_plan, gads, live, sections  # noqa: F401
from agent.tests.test_ads_sync import add_days
from agent.tests.test_settings_page import _section
from agent.tests.test_web import claimed_business, csrf_of

CID = "1234567890"
BUILT = Jsonb(
    {"campaign": "customers/1234567890/campaigns/1", "campaign_budget": "customers/1234567890/campaignBudgets/1"}
)


def _own_account(conn, *, state: str, resources=BUILT) -> None:
    google_ads_link.save(conn, "biz_1", CID, status="active")
    registry.save_account(conn, "biz_1", GOOGLE, mode="own_account", status="active", external={"customer_id": CID})
    plan_id = add_plan(conn, sections(), biz="biz_1", status="approved")  # resume's own launch() needs one to find
    conn.execute(  # precheck (resume's own launch() runs it) needs a published, checked page on file
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, 'biz_1', 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), PAGE_URL, Jsonb(CHECK_OK)),
    )
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
        "VALUES ('biz_1', %s, %s, %s, %s, now())",
        (GOOGLE, plan_id, state, resources),
    )


def _first_of_the_month(conn):
    return conn.execute("SELECT date_trunc('month', now())::date AS d").fetchone()["d"]


def test_connect_shows_the_status_sentence_and_a_pause_button_when_live(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    html = client.get("/ads").text
    ads = _section(html, GOOGLE)  # the Your ads page's Google Search card
    assert "Google Search: live" in ads  # plus the today-spend clause, since _own_account now has a real plan
    assert 'action="/connect/ads/pause"' in ads and "Pause my ads" in ads
    assert 'action="/connect/ads/resume"' not in html and "Resume my ads" not in html
    page = without_site_header(html)  # the shared header's nav is the product's own menu, not the page's words
    assert JARGON.findall(page) == [] and ACCOUNT_JARGON.findall(page) == [] and MONEY_JARGON.findall(page) == []


def test_connect_shows_a_resume_button_when_paused(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="paused")
    html = client.get("/ads").text
    assert "paused by you" in html
    assert 'action="/connect/ads/resume"' in html and "Resume my ads" in html
    assert 'action="/connect/ads/pause"' not in html and "Pause my ads" not in html


def test_connect_shows_a_pause_button_when_limited(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="limited")
    html = client.get("/ads").text
    assert "Google is holding your ads back" in html
    assert 'action="/connect/ads/pause"' in html


def test_connect_shows_the_sentence_but_no_button_while_launching(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="launching")
    html = client.get("/ads").text
    assert "your ads are built and turn on within the hour" in html
    assert 'action="/connect/ads/pause"' not in html and 'action="/connect/ads/resume"' not in html


def test_a_prepaid_business_shows_where_each_channel_stands_too(client, conn, google):
    """M0 Task 6: the ads run per channel -- NewsBreak only ever from the balance -- so a prepaid business's cards (on
    Your ads) and statement (in Settings' Billing card) show like any other's (A3's own-account-only block is gone)."""
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    plan_id = add_plan(conn, sections(), biz="biz_1", status="approved")
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
        "VALUES ('biz_1', 'newsbreak', %s, 'live', %s, now())",
        (plan_id, Jsonb({"campaign": "nb/1"})),
    )
    card = _section(client.get("/ads").text, "newsbreak")  # its card on the Your ads page
    assert "NewsBreak: live." in card and "Pause my ads" in card and 'name="channel" value="newsbreak"' in card
    assert "<h3>Statement</h3>" in _section(client.get("/connect").text, "billing")  # the statement stays in Settings


def test_connect_shows_the_statement_with_the_customer_id_and_no_fee_notice(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    add_days(conn, (_first_of_the_month(conn), 4000000, 2, 30, 1, 1.5))
    html = _section(client.get("/connect").text, "billing")
    assert "<h3>Statement</h3>" in html and "$4.00" in html
    assert google_ads_link.format_customer_id(CID) in html
    assert "Google bills this account directly; Admiry charges no management fee." in html
    assert '<td class="num">1.5</td>' in html  # fractional conversions: one decimal, like spent_usd's rule
    assert '<td class="num">1.50</td>' not in html and '<td class="num">1.500000</td>' not in html


def test_connect_shows_a_whole_conversions_count_without_a_decimal(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    add_days(conn, (_first_of_the_month(conn), 4000000, 3, 40, 5, 7))
    html = client.get("/connect").text
    assert '<td class="num">7</td>' in html and '<td class="num">7.0</td>' not in html


def test_pause_button_pauses_and_is_owner_only(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    csrf = csrf_of(client)
    r = client.post("/connect/ads/pause", data={"business_id": "biz_1", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ads?b=biz_1"  # that business's page (no channel: all)
    row = ads_run.get(conn, "biz_1", GOOGLE)
    assert row["state"] == "paused" and row["resources"]["paused_by"] == "owner"
    assert conn.execute("SELECT actor FROM agent_action WHERE tool = 'web.ads_pause'").fetchone()["actor"] == (
        "web:owner@example.com"
    )
    # a wrong csrf, and someone else's business, are both refused before anything changes
    assert client.post("/connect/ads/pause", data={"business_id": "biz_1", "csrf": "wrong"}).status_code == 403
    conn.execute(
        "INSERT INTO business (id, owner_email, claim_state, ads_mode) VALUES ('biz_2', 'other@example.com', 'claimed', 'own_account')"
    )
    assert client.post("/connect/ads/pause", data={"business_id": "biz_2", "csrf": csrf}).status_code == 404


def test_resume_button_needs_the_switch_on_and_then_resumes(client, conn, google, gads, live):  # noqa: F811 (fixture)
    claimed_business(client, conn, google)
    _own_account(conn, state="paused")
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = 'biz_1'",
        (Jsonb({"paused_by": "owner"}),),
    )
    # the stored campaign as Google already has it (paused): resume's own launch() enables it in place
    gads.campaigns["customers/1234567890/campaigns/1"] = {
        "name": "Admiry · biz_1",
        "status": "PAUSED",
        "budget": "customers/1234567890/campaignBudgets/1",
        "amount": 1_000_000,
    }
    csrf = csrf_of(client)
    r = client.post("/connect/ads/resume", data={"business_id": "biz_1", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ads?b=biz_1"  # that business's page (no channel: all)
    assert ads_run.get(conn, "biz_1", GOOGLE)["state"] == "live"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.ads_resume'").fetchone()["n"] == 1


def test_a_pause_or_resume_naming_a_platform_lands_on_its_card(client, conn, google, gads, live):  # noqa: F811 (fixture)
    """Q9 (spec 2026-10-06 Y20): a form that names a channel lands on that platform's card on the Your ads page, as the
    home's Connect link does; the forms above, which name none (all), land at the top of the page."""
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    gads.campaigns["customers/1234567890/campaigns/1"] = {
        "name": "Admiry · biz_1",
        "status": "ENABLED",
        "budget": "customers/1234567890/campaignBudgets/1",
        "amount": 1_000_000,
    }
    form = {"business_id": "biz_1", "channel": GOOGLE, "csrf": csrf_of(client)}
    for action in ("pause", "resume"):
        r = client.post(f"/connect/ads/{action}", data=form, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, f"/ads?b=biz_1#{GOOGLE}"), action


def test_resume_button_is_refused_with_a_notice_when_the_switch_is_off(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="paused")
    r = client.post("/connect/ads/resume", data={"business_id": "biz_1", "csrf": csrf_of(client)})
    assert r.status_code == 409 and "cannot change your ads on Google Search right now" in r.text
    assert 'href="/ads?b=biz_1">Back</a>' in r.text  # back to the page the button is on, not Settings
    assert ads_run.get(conn, "biz_1", GOOGLE)["state"] == "paused"


def test_a_refused_pause_links_back_to_the_ads_page(client, conn, google):
    claimed_business(client, conn, google)  # nothing built yet: nothing to pause
    r = client.post("/connect/ads/pause", data={"business_id": "biz_1", "csrf": csrf_of(client)})
    assert r.status_code == 409 and ads_run.NO_ADS in r.text and 'href="/ads?b=biz_1">Back</a>' in r.text


def test_the_statement_names_each_channel_above_its_table_and_the_no_fee_notice_once(client, conn, google):
    """A Task 3 review item: a caption per channel's table, and the channels' billing sentences once, after them."""
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    plan_id = conn.execute("SELECT plan_id FROM channel_campaign").fetchone()["plan_id"]
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES ('biz_1', 'newsbreak', %s, "
        "'waiting_account')",
        (plan_id,),
    )
    billing = _section(client.get("/connect").text, "billing")
    statement = unescape(billing.split("<h3>Statement</h3>")[1])
    assert re.findall(r"<caption>([^<]+)</caption>", statement) == ["Google Search · account 123-456-7890", "NewsBreak"]
    notice = ads_run.statement(conn, "biz_1")["no_fee_notice"]
    assert statement.count(notice) == 1 and statement.index(notice) > statement.rindex("</table>")
    assert notice.count("Admiry charges no") == 2  # Google's and NewsBreak's sentences, said once each


def test_resume_is_offered_only_where_the_owners_own_pause_is_the_only_one(client, conn, google):
    """A Task 4 review item: no Resume that would only answer "Admiry paused these ads itself"."""
    claimed_business(client, conn, google)
    _own_account(conn, state="paused", resources=Jsonb({"campaign": "c/1", "owner_paused": True}))
    conn.execute("UPDATE channel_campaign SET paused_reason = 'month_cap'")  # Admiry's pause beside the owner's
    html = client.get("/ads").text
    assert "paused by you and by Admiry" in html and 'action="/connect/ads/resume"' not in html
    conn.execute("UPDATE channel_campaign SET paused_reason = NULL")
    assert 'action="/connect/ads/resume"' in client.get("/ads").text
    conn.execute("UPDATE channel_campaign SET paused_reason = 'no_funds', resources = resources - 'owner_paused'")
    html = client.get("/ads").text
    assert "paused by Admiry" in html and 'action="/connect/ads/resume"' not in html


def test_the_channels_card_pauses_and_resumes_through_the_same_routes_in_json(client, conn, google):
    """The homepage's card posts the same forms (CSRF, the owner's session, the audit row) and reads JSON back."""
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    form = {"business_id": "biz_1", "channel": GOOGLE, "csrf": csrf_of(client)}
    r = client.post("/connect/ads/pause", data=form, headers={"Accept": "application/json"})
    assert (r.status_code, r.json(), r.headers["cache-control"]) == (200, {"ok": True}, "no-store")
    assert ads_run.get(conn, "biz_1", GOOGLE)["state"] == "paused"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.ads_pause'").fetchone()["n"] == 1
    r = client.post("/connect/ads/resume", data=form, headers={"Accept": "application/json"})
    assert r.status_code == 409 and r.json()["ok"] is False  # the switch is off: said, not a page
    assert "cannot change your ads on Google Search right now" in r.json()["error"]
    bad = client.post("/connect/ads/pause", data={**form, "csrf": "wrong"}, headers={"Accept": "application/json"})
    assert bad.status_code == 403


def test_posts_without_a_session_are_refused(client, conn, google):
    claimed_business(client, conn, google)
    _own_account(conn, state="live")
    csrf = csrf_of(client)
    client.cookies.clear()
    assert client.post("/connect/ads/pause", data={"business_id": "biz_1", "csrf": csrf}).status_code == 403
    assert client.post("/connect/ads/resume", data={"business_id": "biz_1", "csrf": csrf}).status_code == 403
    assert ads_run.get(conn, "biz_1", GOOGLE)["state"] == "live"
