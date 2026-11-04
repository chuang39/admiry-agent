"""The "Your ads" page (spec 2026-10-06-admiry-your-ads-page-design.md): one card per platform from the read model,
the edits reviewed and approved, Settings' channel rows moved here (Q12)."""

from __future__ import annotations

import re
import threading
import time
from html import unescape

import pytest
from markupsafe import escape
from psycopg.types.json import Jsonb

from agent import (  # noqa: F401 - mcp_app: import order
    ads_mode,
    ads_run,
    benchmark,
    db,
    google_ads_link,
    mcp_app,
    wallet,
    your_ads,
    your_ads_mock,
)
from agent.channels import registry
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, without_site_header
from agent.tests.test_ads_run import CHECK_OK, PAGE_URL
from agent.tests.test_pages import _dropped
from agent.tests.test_settings_page import _section
from agent.tests.test_tools_consult import _home_plan, fake, recommended_with  # noqa: F401 - the model fake, a fixture
from agent.tests.test_wallet import fund
from agent.tests.test_web import claimed_business, csrf_of, sign_in

CID = "1234567890"
GOOGLE = "google_search"
BUILT = Jsonb(
    {"campaign": "customers/1234567890/campaigns/1", "campaign_budget": "customers/1234567890/campaignBudgets/1"}
)


def launched(client, conn, fake, *, state: str = "live", monthly_budget_usd: int = 2000) -> dict:  # noqa: F811 - the fixture
    """A verified business of owner@example.com with a consulted plan approved and its Google Search ads built on that
    version (a channel_campaign row in `state`), the owner signed in. Returns the plan ({plan_id, version, ...})."""
    r = recommended_with(client, conn, fake, monthly_budget_usd=monthly_budget_usd)["data"]
    conn.execute("UPDATE business SET owner_email = 'owner@example.com', claim_state = 'verified' WHERE id = 'biz_1'")
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (r["plan_id"],))
    google_ads_link.save(conn, "biz_1", CID, status="active")
    registry.save_account(conn, "biz_1", GOOGLE, mode="own_account", status="active", external={"customer_id": CID})
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
        "VALUES ('biz_1', %s, %s, %s, %s, now())",
        (GOOGLE, r["plan_id"], state, BUILT),
    )
    sign_in(client, "/ads")
    return r


def launched_two(client, conn) -> dict:
    """`launched` with two platforms: a prepaid roofer's consulted plan at $2,000 -- Google Search $1,700 and NewsBreak
    $300, the allocator's share, NewsBreak's words a copy of Google Search's -- approved, both platforms' ads built on
    that version (live rows), the balance funded and a checked page on file (what the approval's precheck asks of a
    prepaid business), the business verified and its owner signed in. The test needs `fake`, `google`, `newsbreak_on`
    and `pictures`. Returns the plan ({plan_id, version, ...})."""
    benchmark.seed(conn)
    _key, r = _home_plan(client, conn, service="svc_roofing", limit=2000, prepaid=True)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com', claim_state = 'verified' WHERE id = 'biz_1'")
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (r["plan_id"],))
    for channel, built in ((GOOGLE, BUILT), ("newsbreak", Jsonb({"campaign": "nb/1"}))):
        conn.execute(
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
            "VALUES ('biz_1', %s, %s, 'live', %s, now())",
            (channel, r["plan_id"], built),
        )
    fund(conn, cents=250_000)
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, 'biz_1', 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), PAGE_URL, Jsonb(CHECK_OK)),
    )
    sign_in(client, "/ads")
    return r


def _clean(html: str) -> None:
    assert JARGON.findall(html) == [] and ACCOUNT_JARGON.findall(html) == [] and MONEY_JARGON.findall(html) == []


def test_signed_out_gets_the_sign_in_page_and_a_business_less_owner_the_way_in(client, google):
    r = client.get("/ads")
    assert r.status_code == 200 and 'href="/connect/google/start?next=/ads"' in r.text
    sign_in(client, "/ads")
    html = client.get("/ads").text
    assert "No business yet" in html and 'href="/">the home page</a>' in html


def test_a_live_platform_shows_its_status_numbers_ads_and_forms(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    res = client.get("/ads")
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert res.headers["content-security-policy"].startswith("default-src 'self'")
    html = res.text
    assert "<title>Admiry — Your ads</title>" in html and '<meta name="robots" content="noindex">' in html
    assert "Nothing spent yet this month." in html
    assert "Within 5 miles of the business" in html and 'name="radius" inputmode="decimal" required value="5"' in html
    card = _section(html, GOOGLE)
    assert "Google Search: live" in card and "Connected ✓ · account 123-456-7890" in card
    assert "$0 of $2,000 this month" in card and 'action="/connect/ads/pause"' in card
    assert f"Version {r['version']} · running" in card and "Shows for:" in card and "Sends visitors to:" in card
    assert "$2,000 a month · about $65.78 a day" in card
    assert f'name="base" value="{r["plan_id"]}"' in card and 'name="kind" value="words"' in card
    assert "<style" not in html and re.findall(r"<script(?![^>]*\bsrc=)", html) == []
    assert (
        html.index('href="/static/base.css"')
        < html.index('href="/static/settings.css"')
        < html.index('href="/static/your_ads.css"')
    )
    css = client.get("/static/your_ads.css")
    assert css.status_code == 200 and len(css.text) < 8000 and "https://" not in css.text
    _clean(html)


def test_the_builder_and_the_mock_share_one_context_shape(client, conn, fake, google):  # noqa: F811 - the fixture
    launched(client, conn, fake)
    from agent import config, db

    settings = config.load()
    with db.connect(settings.database_url) as conn2:
        session = conn2.execute("SELECT * FROM web_session WHERE revoked_at IS NULL").fetchone()
        ctx = your_ads._context(conn2, settings, dict(session), "biz_1")
    assert set(ctx) == set(your_ads_mock.fixture())
    assert set(ctx["platforms"][0]) == set(your_ads_mock.fixture()["platforms"][0])
    assert set(ctx["platforms"][0]["ads"]) == set(your_ads_mock.fixture()["platforms"][0]["ads"])


def test_a_pause_of_the_owners_offers_resume_and_admirys_none(client, conn, fake, google):  # noqa: F811 - the fixture
    launched(client, conn, fake, state="paused")
    conn.execute("UPDATE channel_campaign SET resources = resources || %s", (Jsonb({"owner_paused": True}),))
    card = _section(client.get("/ads").text, GOOGLE)
    assert 'action="/connect/ads/resume"' in card and "Resume my ads" in card
    conn.execute("UPDATE channel_campaign SET paused_reason = 'month_cap'")
    card = _section(client.get("/ads").text, GOOGLE)
    assert "this month's budget is spent" in unescape(card) and 'action="/connect/ads/' not in card


def test_what_google_holds_back_and_an_ad_it_did_not_approve_are_on_the_card_in_its_words(client, conn, fake, google):  # noqa: F811 - the fixture
    """Final review I2 (spec §4, Risk 4): every issue on the row reaches the card -- a reason Google gave for holding
    the ads back as Google wrote it, and an ad it did not approve with that ad's headline and Google's own policy
    topic -- so the owner who edits the words again sees why."""
    launched(client, conn, fake)
    read = {
        "campaign": ["Limited by budget"],
        "ads": [
            {
                "resource_name": "customers/1234567890/adGroupAds/1~2",
                "topics": [{"topic": "Misleading content", "type": "PROHIBITED"}],
            }
        ],
    }
    conn.execute("UPDATE channel_campaign SET resources = resources || %s", (Jsonb({"issues": read}),))
    warnings = re.findall(r'<p class="warn">([^<]+)</p>', _section(client.get("/ads").text, GOOGLE))
    assert [unescape(w) for w in warnings] == [
        "Limited by budget",
        'Google Search needs a change to one of your ads ("Invisalign in Palo Alto"): Misleading content.',
    ]


def test_a_platform_trailing_the_approved_version_says_so_and_a_refusal_keeps_the_old_one(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    newer = conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) SELECT 'pl_newer0000000001', business_id, "
        "version + 1, json, 'approved' FROM plan WHERE id = %s RETURNING version",
        (r["plan_id"],),
    ).fetchone()["version"]
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (r["plan_id"],))
    card = _section(client.get("/ads").text, GOOGLE)
    assert (
        f"Version {newer} is approved; Google Search still runs version {r['version']} — Admiry sends it within the hour."
        in unescape(card)
    )
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s", (Jsonb({"refused_plan_id": "pl_newer0000000001"}),)
    )
    card = _section(client.get("/ads").text, GOOGLE)
    assert f"Version {newer} was not accepted; version {r['version']} keeps running." in card


def test_a_newer_draft_is_named_above_the_rows(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) SELECT 'pl_draft0000000001', business_id, "
        "version + 1, json, 'draft' FROM plan WHERE id = %s",
        (r["plan_id"],),
    )
    html = client.get("/ads").text
    assert "A newer version of your plan is waiting for your approval." in html
    assert f'href="/plan/pl_draft0000000001">See version {r["version"] + 1}</a>' in html


def test_a_funded_platform_without_a_row_still_shows_its_ads_and_forms(client, conn, fake, google):  # noqa: F811 - the fixture
    """Review Focus 3: approved while the switch was off, nothing queued -- the plan's words and forms are there."""
    launched(client, conn, fake)
    conn.execute("DELETE FROM channel_campaign")
    html = client.get("/ads").text
    card = _section(html, GOOGLE)
    assert ads_run.OFF_CHANNEL.format(label="Google Search") in card
    assert 'class="numbers"' not in card and 'action="/connect/ads/' not in card
    assert 'name="kind" value="words"' in card and 'name="kind" value="budget"' in card


def test_before_an_approved_plan_the_page_is_a_status_page(client, conn, fake, google):  # noqa: F811 - the fixture
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    sign_in(client, "/ads")
    html = client.get("/ads").text
    assert "Your ads are not launched yet." in html and f'href="/plan/{r["plan_id"]}"' in html
    assert 'name="kind"' not in html


def test_the_in_review_notice_replaces_approve_it_there(client, conn, fake, google):  # noqa: F811 - the fixture
    """L1 review M4/M8: while a first launch is in Admiry's review, the page says so instead of "see it and approve
    it there" -- the plan page offers no approval then (plan.html's own in_review branch)."""
    from agent import launch_review

    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    launch_review.submit(conn, "biz_1", r["plan_id"], approval={"kind": "web"})
    sign_in(client, "/ads")
    html = client.get("/ads").text
    assert launch_review.IN_REVIEW_SAY in html and "see it and approve it there" not in html
    assert f'href="/plan/{r["plan_id"]}"' not in html


def test_two_businesses_get_the_switcher_and_a_strangers_id_falls_back_to_the_owners(client, conn, fake, google):  # noqa: F811 - the fixture
    launched(client, conn, fake)
    conn.execute(  # both older than biz_1, which stays the newest (the default shown)
        "INSERT INTO business (id, claim_state, owner_email, slug, created_at) VALUES "
        "('biz_2', 'claimed', 'owner@example.com', 'two', now() - interval '1 day'), "
        "('biz_x', 'claimed', 'other@example.com', 'x', now() - interval '2 days')"
    )
    html = client.get("/ads?b=biz_2").text
    assert '<details class="switcher">' in html and 'href="/ads?b=biz_1"' in html and 'aria-current="page">' in html
    assert "Your ads are not launched yet." in html  # biz_2 has no plan
    html = client.get("/ads?b=biz_x").text
    assert 'id="google_search"' in html  # the newest of the owner's own, never the stranger's


def test_the_approved_banner_takes_only_the_approved_versions_number(client, conn, fake, google):  # noqa: F811 - the fixture
    """Review Focus 5: a hand-edited link never 422s."""
    r = launched(client, conn, fake)
    assert f"Version {r['version']} approved." in client.get(f"/ads?approved={r['version']}").text
    # "²" is a digit to str.isdigit() that int() refuses, and a run of digits past int()'s 4,300-digit limit raises too
    for junk in ("abc", "99", "", "1.5", "²", "9" * 5000):
        res = client.get(f"/ads?approved={junk}")
        assert res.status_code == 200 and "approved.</p>" not in res.text


def test_the_later_lines_and_the_disclosure_close_the_page(client, conn, fake, google):  # noqa: F811 - the fixture
    launched(client, conn, fake)
    html = client.get("/ads").text
    assert (
        '<section id="later" class="card">' in html and "Worth a test after 30 days of booking data. Not now." in html
    )
    assert "Admiry charges no management fee." in html and your_ads.FOOT in html


def test_a_locked_account_row_says_why_its_way_cannot_change_linked_or_prepaid(client, conn, fake, google):  # noqa: F811 - the fixture
    """Spec §2 item 4: under the account line, the sentence that says why the way a platform runs cannot change, as
    Settings said it before the rows moved here: a live link (which also withholds Change), or money paid in for a
    prepaid business (which keeps Add funds)."""
    launched(client, conn, fake)  # the Google link is live
    card = _section(client.get("/ads").text, GOOGLE)
    assert ads_mode.LINK_IN_PLACE.format(display="123-456-7890") in card and "To change that, ask Admiry." in card
    assert "/?setup=" not in card  # no Change button: the lock withholds the account row's one action
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    google_ads_link.mirror(conn, "biz_1")  # Google Search's account row follows the column, as ads_mode.change does
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=50_000, ref="pi_1", created_by="stripe")  # money in
    card = _section(client.get("/ads").text, GOOGLE)
    assert "Run by Admiry" in card and "Add funds" in card  # Admiry runs it now: the action is Add funds
    assert ads_mode.MONEY_IN_PLACE in card and "is linked to Admiry" not in card  # money's sentence, not the link's


@pytest.mark.parametrize(
    ("state", "running"),
    [("live", True), ("limited", True), ("paused", False), ("waiting_account", False), (None, False)],
)
def test_running_is_said_only_of_a_platform_that_is_spending(client, conn, fake, google, state, running):  # noqa: F811 - the fixture
    """The version line says "· running" only while the platform spends (live or limited): not under "not launched yet"
    (no row: `state` None), nor beside a pause or a wait. The version itself is always said."""
    r = launched(client, conn, fake, state=state or "live")
    if state is None:
        conn.execute("DELETE FROM channel_campaign")
    card = _section(client.get("/ads").text, GOOGLE)
    assert f"Version {r['version']}" in card
    assert ("· running" in card) is running


# ---------------------------------------------------------------- Save (the review) and Approve (spec §3)

# words the consultation's draft does not already carry (an edit to the same lines is "Nothing changed.")
WORDS_H = "Invisalign Dentist Palo Alto\nSame-Week Visits\nClear Aligners For Adults"
WORDS_D = "Clear aligners from a family dentist. Book online today.\nStraighter teeth, no metal braces."


def _form(client, r: dict, **fields) -> dict:
    return {"business_id": "biz_1", "csrf": csrf_of(client), "base": r["plan_id"], **fields}


def _launches(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **kw: calls.append(biz) or {"channels": {}, "say": ""})
    return calls


def _plans(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"]


def test_save_shows_the_review_and_stores_nothing(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    res = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount="$1,500"))
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    html = res.text
    assert "<h2>Google Search: $2,000 → $1,500 a month</h2>" in unescape(html)
    assert '<td>Google Search</td><td class="num">$2,000</td><td class="num">$1,500</td><td>about $49.34</td>' in html
    assert '<td>Total a month</td><td class="num">$2,000</td><td class="num">$1,500</td>' in html
    assert ads_run.OFF_CHANNEL.format(label="Google Search") in html  # the switch is off in the suite
    assert (
        'action="/ads/approve"' in html
        and 'name="amount" value="$1,500"' in html
        and f'name="base" value="{r["plan_id"]}"' in html
    )
    assert "Where your ads show" not in html  # a budget change moves no radius
    assert _plans(conn) == 1
    _clean(html)


def test_an_area_review_shows_the_radius_moving_and_stores_nothing(client, conn, fake, google):  # noqa: F811 - the fixture
    """The area's review (the counterpart of the approval below): the headline and the "Where your ads show" line both
    say 5 → 7.5, and the hidden fields carry the new radius to Approve."""
    r = launched(client, conn, fake)
    res = client.post("/ads/review", data=_form(client, r, kind="area", radius="7.5"))
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    html = res.text
    assert "<h2>Within 5 → 7.5 miles of the business</h2>" in html
    assert "<p>Within 5 → 7.5 miles of the business.</p>" in html
    assert '<td>Total a month</td><td class="num">$2,000</td><td class="num">unchanged</td>' in html  # no amount moves
    assert 'name="kind" value="area"' in html and 'name="radius" value="7.5"' in html
    assert _plans(conn) == 1
    _clean(html)


def test_a_words_review_keeps_the_passing_lines_and_names_the_dropped_one_crlf_included(client, conn, fake, google):  # noqa: F811 - the fixture
    """Review Focus 1: lines pasted with CRLF, tabs and trailing spaces are the same lines."""
    r = launched(client, conn, fake)
    typed = "Call now!!\r\n" + WORDS_H.replace("\n", "  \r\n").replace("Same-Week ", "Same-Week\t")
    html = client.post(
        "/ads/review", data=_form(client, r, kind="words", channel=GOOGLE, headlines=typed, descriptions=WORDS_D)
    ).text
    assert "<h2>New words for Google Search</h2>" in html and "Google Search: the lines that will run" in html
    assert "Invisalign Dentist Palo Alto<br>Same-Week Visits<br>Clear Aligners For Adults<br>" in html
    assert 'Dropped a headline ("Call now!!"): it has an exclamation mark.' in unescape(html)
    assert '<td class="num">unchanged</td>' in html and "reviews new words before they show" not in html  # switch off
    assert _plans(conn) == 1


def test_a_refused_save_comes_back_as_the_page_with_the_form_open(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    res = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount="2000"))
    assert res.status_code == 409 and '<p class="banner banner-alert" role="alert">Nothing changed.</p>' in res.text
    card = _section(res.text, GOOGLE)
    assert '<details class="edit" open>' in card and 'name="amount" inputmode="numeric" required value="2000"' in card
    for amount, rule in (
        ("abc", your_ads.AMOUNT_RULE),
        ("0", your_ads.AMOUNT_RULE),
        ("1,000,000", your_ads.AMOUNT_RULE),
    ):
        res = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount=amount))
        assert res.status_code == 409 and rule in res.text, amount
    res = client.post("/ads/review", data=_form(client, r, kind="area", radius="0.5"))
    assert res.status_code == 409 and your_ads.RADIUS_RULE in res.text
    res = client.post(
        "/ads/review", data=_form(client, r, kind="words", channel=GOOGLE, headlines="One\nTwo", descriptions=WORDS_D)
    )
    assert res.status_code == 409 and "Keep at least 3 headlines and 2 descriptions that pass" in res.text
    res = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount="2500"))
    over = (  # the refusal ends its sentence before the pointer to the assistant
        '<p class="banner banner-alert" role="alert">$2,500 a month is above your $2,000 monthly limit; raise the '
        "limit first. You can raise it in your assistant.</p>"
    )
    assert res.status_code == 409 and over in res.text
    assert _plans(conn) == 1


def test_approve_writes_the_version_approves_it_with_the_owners_click_and_launches(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    google,
    monkeypatch,
):
    r = launched(client, conn, fake)
    calls = _launches(monkeypatch)
    res = client.post("/ads/approve", data=_form(client, r, kind="area", radius="7.5"), follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == f"/ads?b=biz_1&approved={r['version'] + 1}#area"
    new = ads_run.approved(conn, "biz_1")
    assert new["version"] == r["version"] + 1 and new["diff_from"] == r["plan_id"]
    assert new["sections"]["consultation"]["geo"]["radius_miles"] == 7.5
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (r["plan_id"],)).fetchone()["status"] == "superseded"
    prop = conn.execute("SELECT status, created_by FROM proposal").fetchone()
    assert (prop["status"], prop["created_by"]) == ("confirmed", "web")
    row = conn.execute(
        "SELECT actor, approval_kind, approval_ref, result FROM agent_action WHERE tool = 'web.ads_edit'"
    ).fetchone()
    assert row["actor"] == "web:owner@example.com" and row["approval_kind"] == "web" and row["approval_ref"]
    assert row["result"]["plan_id"] == new["id"] and row["result"]["version"] == new["version"]
    assert calls == ["biz_1"]
    html = client.get(res.headers["location"].split("#")[0]).text
    assert f"Version {new['version']} approved." in html and "Within 7.5 miles of the business" in html


def test_the_launch_runs_after_the_commit_and_reads_the_committed_version(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    """Spec §9: `launch_now` is called once, with the business, after the commit -- a second connection, which sees
    only committed rows, finds the new version approved when it runs."""
    from agent import config, db

    r = launched(client, conn, fake)
    seen: list[int] = []

    def spy(biz, **kw):
        with db.connect(config.load().database_url) as other:
            seen.append(ads_run.approved(other, biz)["version"])
        return {"channels": {}, "say": ""}

    monkeypatch.setattr(ads_run, "launch_now", spy)
    res = client.post("/ads/approve", data=_form(client, r, kind="area", radius="7.5"), follow_redirects=False)
    assert res.status_code == 303 and seen == [r["version"] + 1]


def test_approve_of_the_words_stores_the_kept_lines(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    _launches(monkeypatch)
    res = client.post(
        "/ads/approve",
        data=_form(client, r, kind="words", channel=GOOGLE, headlines="Call now!!\n" + WORDS_H, descriptions=WORDS_D),
        follow_redirects=False,
    )
    assert res.status_code == 303 and res.headers["location"].endswith("#google_search")
    entry = ads_run.entry(ads_run.approved(conn, "biz_1"), GOOGLE)
    assert entry["headlines"] == WORDS_H.split("\n") and entry["descriptions"] == WORDS_D.split("\n")


def test_the_review_pages_own_form_approves_what_it_showed(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    """Final review I3 (c): Approve posts exactly the hidden fields of the review page's own form (your_ads_review.html
    and the route agree on them), and the multi-line words come through their hidden inputs whole."""
    r = launched(client, conn, fake)
    calls = _launches(monkeypatch)
    review = client.post(
        "/ads/review", data=_form(client, r, kind="words", channel=GOOGLE, headlines=WORDS_H, descriptions=WORDS_D)
    )
    assert review.status_code == 200
    form = re.search(r'<form [^>]*action="/ads/approve"[^>]*>(.*?)</form>', review.text, re.DOTALL).group(1)
    fields = {k: unescape(v) for k, v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)}
    assert set(fields) == {"csrf", "business_id", "base", "kind", "channel", "headlines", "descriptions"}
    assert fields["headlines"] == WORDS_H and fields["descriptions"] == WORDS_D  # the line breaks survive
    res = client.post("/ads/approve", data=fields, follow_redirects=False)
    assert res.status_code == 303 and calls == ["biz_1"]
    entry = ads_run.entry(ads_run.approved(conn, "biz_1"), GOOGLE)
    assert entry["headlines"] == WORDS_H.split("\n") and entry["descriptions"] == WORDS_D.split("\n")


# a plan with two platforms, NewsBreak's words a copy of Google Search's (final review I3 (a); spec §4, Y8, §9)
ROOF_H = "Roof Repair Near You\nFree Roofing Estimates\nLocal Roofing Crew"
ROOF_D = "Repairs and full roof replacements. Ask for a quote.\nLeaks and storm damage fixed by a local crew."
CITY_TWICE = "Roofers in {city} who answer the phone, for homes all over {city}."  # Google keeps it; NewsBreak does not


def test_a_review_of_the_primarys_words_shows_the_lines_the_copying_platform_will_run(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    google,
    newsbreak_on,
    pictures,
):
    """Google Search's new words reach NewsBreak, which copies them: the review shows NewsBreak's lines as NewsBreak's
    own linter keeps them, with its note for the line only it drops, and stores nothing."""
    r = launched_two(client, conn)
    words = {"headlines": ROOF_H, "descriptions": f"{ROOF_D}\n{CITY_TWICE}"}
    res = client.post("/ads/review", data=_form(client, r, kind="words", channel=GOOGLE, **words))
    assert res.status_code == 200
    html = unescape(res.text)
    google_lines = html.split("<h3>Google Search: the lines that will run</h3>")[1].split("<h3>")[0]
    nb_lines = html.split("<h3>NewsBreak: the lines that will run</h3>")[1].split("<h3>")[0]
    assert CITY_TWICE in google_lines and '<p class="warn">' not in google_lines
    kept, _, notes = nb_lines.partition('<p class="warn">')
    assert "Roof Repair Near You<br>" in kept and "Leaks and storm damage fixed by a local crew.<br>" in kept
    assert CITY_TWICE not in kept
    assert f'Dropped a description ("{CITY_TWICE}"): it uses the city placeholder more than once.' in notes
    assert "NewsBreak reviews the new words first" in html and _plans(conn) == 1


def test_approving_the_primarys_words_carries_them_to_the_platform_that_copies_them(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    google,
    newsbreak_on,
    pictures,
    monkeypatch,
):
    r = launched_two(client, conn)
    _launches(monkeypatch)
    words = {"headlines": ROOF_H, "descriptions": ROOF_D}
    res = client.post(
        "/ads/approve", data=_form(client, r, kind="words", channel=GOOGLE, **words), follow_redirects=False
    )
    assert res.status_code == 303
    new = ads_run.approved(conn, "biz_1")
    assert new["version"] == r["version"] + 1
    google_ads, nb = ads_run.entry(new, GOOGLE), ads_run.entry(new, "newsbreak")
    assert (google_ads["headlines"], google_ads["descriptions"]) == (ROOF_H.split("\n"), ROOF_D.split("\n"))
    assert (nb["headlines"], nb["descriptions"], nb["copy_of"]) == (ROOF_H.split("\n"), ROOF_D.split("\n"), GOOGLE)


def test_approving_a_copying_platforms_own_words_ends_its_copy_and_leaves_the_primarys(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    google,
    newsbreak_on,
    pictures,
    monkeypatch,
):
    """NewsBreak given words of its own -- one headline and one description, NewsBreak's own minimum -- stops copying
    Google Search's: its `copy_of` goes, and Google Search's words stay as they were."""
    r = launched_two(client, conn)
    _launches(monkeypatch)
    google_before = ads_run.entry(ads_run.approved(conn, "biz_1"), GOOGLE)
    words = {"headlines": "Roof Repair in {city}", "descriptions": "Local roofers fix leaks and storm damage."}
    res = client.post(
        "/ads/approve", data=_form(client, r, kind="words", channel="newsbreak", **words), follow_redirects=False
    )
    assert res.status_code == 303 and res.headers["location"].endswith("#newsbreak")
    new = ads_run.approved(conn, "biz_1")
    nb, google_ads = ads_run.entry(new, "newsbreak"), ads_run.entry(new, GOOGLE)
    assert (nb["headlines"], nb["descriptions"]) == (
        ["Roof Repair in {city}"],
        ["Local roofers fix leaks and storm damage."],
    )
    assert "copy_of" not in nb
    assert (google_ads["headlines"], google_ads["descriptions"]) == (
        google_before["headlines"],
        google_before["descriptions"],
    )


def test_two_tabs_the_second_approval_is_refused_and_writes_nothing(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    """Review Focus 4."""
    r = launched(client, conn, fake)
    calls = _launches(monkeypatch)
    first = client.post(
        "/ads/approve", data=_form(client, r, kind="budget", channel=GOOGLE, amount="1500"), follow_redirects=False
    )
    assert first.status_code == 303 and _plans(conn) == 2
    second = client.post("/ads/approve", data=_form(client, r, kind="budget", channel=GOOGLE, amount="1200"))
    assert second.status_code == 409 and "Your plan changed since you looked" in second.text and _plans(conn) == 2
    assert calls == ["biz_1"]  # the first approval launched; the refused second launched nothing
    stale = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount="1200"))
    assert stale.status_code == 409 and "Your plan changed since you looked" in stale.text


def test_an_approval_overlapping_another_on_the_same_base_is_refused_and_launches_nothing(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    google,
    monkeypatch,
):
    """Review Focus 4 for tabs that overlap instead of following each other: a second connection plays tab A
    mid-flight (the base superseded, not yet committed); tab B's approval waits on the base's row lock and, once A
    commits, finds the base gone -- the 409, no draft written, nothing launched (spec §3.3)."""
    from agent import config, db

    r = launched(client, conn, fake)
    calls = _launches(monkeypatch)
    form = _form(client, r, kind="budget", channel=GOOGLE, amount="1200")
    got: list = []
    tab_b = threading.Thread(
        target=lambda: got.append(client.post("/ads/approve", data=form, follow_redirects=False)), daemon=True
    )
    with db.connect(config.load().database_url) as other, other.transaction():
        other.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (r["plan_id"],))
        tab_b.start()
        time.sleep(0.5)
        assert tab_b.is_alive()  # waiting on the row lock, not answered
    tab_b.join(timeout=30)
    assert not tab_b.is_alive() and len(got) == 1, "tab B never answered"
    res = got[0]
    assert res.status_code == 409 and "Your plan changed since you looked" in res.text
    assert _plans(conn) == 1 and calls == []
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.ads_edit'").fetchone()["n"] == 0


def test_an_approval_replaces_the_newer_draft_the_page_names(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    """The page says "A change you approve here replaces it" (NEWER): the open draft is superseded, the new version
    numbered after it."""
    r = launched(client, conn, fake)
    _launches(monkeypatch)
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) SELECT 'pl_draft0000000001', business_id, "
        "version + 1, json, 'draft' FROM plan WHERE id = %s",
        (r["plan_id"],),
    )
    res = client.post("/ads/approve", data=_form(client, r, kind="area", radius="7.5"), follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == f"/ads?b=biz_1&approved={r['version'] + 2}#area"
    assert conn.execute("SELECT status FROM plan WHERE id = 'pl_draft0000000001'").fetchone()["status"] == "superseded"
    assert ads_run.approved(conn, "biz_1")["version"] == r["version"] + 2


def test_thirty_approvals_an_hour_is_the_limit(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    from agent import audit

    r = launched(client, conn, fake)
    calls = _launches(monkeypatch)
    for i in range(your_ads.EDITS_PER_HOUR):
        audit.record_web(
            conn, tool="web.ads_edit", email="owner@example.com", ip=None, business_id="biz_1", args={"i": i}, result={}
        )
    res = client.post("/ads/approve", data=_form(client, r, kind="budget", channel=GOOGLE, amount="1500"))
    assert res.status_code == 409 and your_ads.TOO_MANY in res.text and _plans(conn) == 1 and calls == []


def test_posts_need_the_session_the_csrf_the_owner_and_a_verified_business(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    form = _form(client, r, kind="budget", channel=GOOGLE, amount="1500")
    assert client.post("/ads/review", data={**form, "csrf": "wrong"}).status_code == 403
    conn.execute(
        "INSERT INTO business (id, owner_email, claim_state, slug) VALUES ('biz_x', 'other@example.com', 'verified', 'x')"
    )
    assert client.post("/ads/review", data={**form, "business_id": "biz_x"}).status_code == 404
    conn.execute("UPDATE business SET claim_state = 'claimed' WHERE id = 'biz_1'")
    res = client.post("/ads/approve", data=form)
    assert res.status_code == 409 and res.json()["detail"] == your_ads.NOT_VERIFIED
    client.cookies.clear()
    assert client.post("/ads/approve", data=form).status_code == 403
    assert _plans(conn) == 1


def test_before_an_approved_plan_a_post_is_refused(client, conn, fake, google):  # noqa: F811 - the fixture
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    conn.execute("UPDATE business SET owner_email = 'owner@example.com', claim_state = 'verified' WHERE id = 'biz_1'")
    sign_in(client, "/ads")
    res = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount="1500"))
    assert res.status_code == 409 and "Your ads are not launched yet." in res.text


def test_a_radius_the_base_never_had_reads_any_distance(client, conn, fake, google):  # noqa: F811 - the fixture
    """The net under plan_edit's pin, which fails open for a base without a usable radius: the radius the rebuild brings
    in shows on every kind of review, and "any distance" is what the base had."""
    r = launched(client, conn, fake)
    conn.execute(
        "UPDATE plan SET json = json #- '{sections,consultation,geo,radius_miles}' WHERE id = %s", (r["plan_id"],)
    )
    html = client.post("/ads/review", data=_form(client, r, kind="area", radius="7.5")).text
    assert "<h2>Within 7.5 miles of the business</h2>" in html
    assert "<p>Within any distance → 7.5 miles of the business.</p>" in html
    html = client.post("/ads/review", data=_form(client, r, kind="budget", channel=GOOGLE, amount="1500")).text
    assert "<h3>Where your ads show</h3><p>Within any distance → 5 miles of the business.</p>" in html


def test_a_hand_edited_post_is_a_sentence_never_a_500(client, conn, fake, google, monkeypatch):  # noqa: F811 - the fixture
    """Review Focus 5's rule, for the POSTs: a run of digits past int()'s 4,300-digit limit is an amount that is not
    allowed, and a NUL (which jsonb cannot hold) is dropped from the words, as the audit row's text drops it."""
    r = launched(client, conn, fake)
    _launches(monkeypatch)
    for amount in ("9" * 5000, "1," + "000," * 2000 + "000"):
        for route in ("/ads/review", "/ads/approve"):
            res = client.post(route, data=_form(client, r, kind="budget", channel=GOOGLE, amount=amount))
            assert res.status_code == 409 and your_ads.AMOUNT_RULE in res.text, (route, amount[:8])
    typed = {"headlines": WORDS_H.replace("Visits", "Vis\x00its"), "descriptions": WORDS_D.replace("Book", "B\x00ook")}
    res = client.post(
        "/ads/approve", data=_form(client, r, kind="words", channel=GOOGLE, **typed), follow_redirects=False
    )
    assert res.status_code == 303
    entry = ads_run.entry(ads_run.approved(conn, "biz_1"), GOOGLE)
    assert entry["headlines"] == WORDS_H.split("\n") and entry["descriptions"] == WORDS_D.split("\n")


def test_the_review_builder_and_the_mock_share_one_context_shape(client, conn, fake, google):  # noqa: F811 - the fixture
    r = launched(client, conn, fake)
    from agent import config, db, plan_edit

    settings = config.load()
    fields = {
        "kind": "words",
        "channel": GOOGLE,
        "amount": None,
        "radius": None,
        "headlines": WORDS_H,
        "descriptions": WORDS_D,
    }
    with db.connect(settings.database_url) as conn2:
        session = dict(conn2.execute("SELECT * FROM web_session WHERE revoked_at IS NULL").fetchone())
        change = your_ads._change(**fields)
        pv = plan_edit.preview(conn2, "biz_1", r["plan_id"], change)
        ctx = your_ads._review_context(settings, session, {"id": "biz_1"}, r["plan_id"], change, pv, fields)
    mock = your_ads_mock.review_fixture()
    assert (
        set(ctx) == set(mock) and set(ctx["fields"]) == set(mock["fields"]) and set(ctx["total"]) == set(mock["total"])
    )
    assert set(ctx["rows"][0]) == set(mock["rows"][0]) and set(ctx["words"][0]) == set(mock["words"][0])


# ---------------------------------------------------------------- the account rows, moved from Settings (spec Y10)


def _account_rows(html: str) -> dict[str, dict]:
    """Each platform card's account line by channel id: the status words and the one action (its words and address)."""
    rows = {}
    for channel, card in re.findall(r'<section id="([a-z_]+)" class="card platform">(.*?)</section>', html, re.DOTALL):
        action = re.search(r'<a class="btn" href="([^"]+)">([^<]+)<span class="sr">', card)
        meta = re.search(r'<div class="row account">.*?<p class="row-meta">([^<]+)</p>', card, re.DOTALL)
        rows[channel] = {
            "status": unescape(meta.group(1)) if meta else None,
            "action": (action.group(2), action.group(1)) if action else None,
            "warn": re.findall(r'<p class="warn">([^<]+)</p>', card),
        }
    return rows


def _with_plan(conn) -> None:
    """A claimed business with an approved plan funding Google Search, as test_ads_run's `sections` builds one."""
    from agent.tests.test_ads_run import add_plan, sections

    add_plan(conn, sections(), biz="biz_1", status="approved")


@pytest.mark.parametrize(
    ("link", "status", "action"),
    [
        (None, "Not connected", ("Link", "/?setup=biz_1")),
        ("pending", "Invitation sent · account 123-456-7890", ("Check now", "/?setup=biz_1")),
        ("refused", "Not connected · account 123-456-7890", ("Link", "/?setup=biz_1")),
        ("active", "Connected ✓ · account 123-456-7890", None),  # Change is refused while the link is live
    ],
)
def test_each_platform_card_has_its_account_line_with_its_one_action(client, conn, google, link, status, action):
    claimed_business(client, conn, google)
    _with_plan(conn)
    if link:
        google_ads_link.save(conn, "biz_1", "1234567890", status=link)
    rows = _account_rows(client.get("/ads").text)
    assert rows["google_search"] == {"status": status, "action": action, "warn": []}


def test_a_prepaid_business_reads_run_by_admiry_and_a_platform_notice_in_its_own_words(client, conn, google):
    claimed_business(client, conn, google)
    _with_plan(conn)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    fund(conn, cents=25000)
    words = "Your account is under review."
    registry.save_account(
        conn, "biz_1", "newsbreak", mode="managed", status="needs_attention", external={}, last_error=words
    )
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) SELECT 'biz_1', 'newsbreak', id, "
        "'waiting_account' FROM plan WHERE business_id = 'biz_1'"
    )
    rows = _account_rows(client.get("/ads").text)
    assert rows["google_search"]["status"] == "Run by Admiry" and rows["google_search"]["action"][0] == "Add funds"
    assert rows["newsbreak"]["status"] == f'Needs attention: "{words}"' and rows["newsbreak"]["warn"] == []
    assert rows["newsbreak"]["action"] == ("Add funds", "/?setup=biz_1")  # a prepaid business adds to its balance


def test_a_note_the_last_check_left_on_the_link_stays_on_its_card(client, conn, google):
    claimed_business(client, conn, google)
    _with_plan(conn)
    google_ads_link.save(conn, "biz_1", "1234567890", status="pending")
    google_ads_link.record_error(conn, "biz_1", "1234567890", google_ads_link.UNREACHABLE)
    rows = _account_rows(client.get("/ads").text)
    assert rows["google_search"]["warn"] == [
        google_ads_link.UNREACHABLE[0].upper() + google_ads_link.UNREACHABLE[1:] + "."
    ]


def test_settings_keeps_only_billing_and_links_here(client, conn, fake, google):  # noqa: F811 - the fixture
    """Spec Y10, read for a business whose ads are live: the old "Your ads now" block (one row per channel_campaign
    row, its Pause form) only rendered with such a row, so a business with no ads could not tell it was gone."""
    launched(client, conn, fake)  # a live Google Search row on an approved plan: Pause is what /ads offers it
    assert 'action="/connect/ads/pause"' in client.get("/ads").text
    html = client.get("/connect").text
    billing = _section(html, "billing")
    assert "<h2>Billing</h2>" in billing and 'href="/ads?b=biz_1">Your ads</a>' in billing
    assert "Where your ads run" not in html and "Your ads now" not in html and 'id="ads"' not in html
    assert 'action="/connect/ads/' not in html and 'id="ch-google_search"' not in html


def test_changes_admiry_made_show_on_the_approved_versions_page(client, conn, fake, google):  # noqa: F811 - fake is a fixture used by name
    r = launched(client, conn, fake)
    assert "Changes Admiry made" not in client.get("/ads").text
    _dropped(conn, r["plan_id"])
    html = client.get("/ads").text
    assert "Changes Admiry made" in html and escape("Removed the headline “Same-Week Appointments”") in html
    assert JARGON.findall(without_site_header(html)) == []
