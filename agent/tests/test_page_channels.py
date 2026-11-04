"""The page the ads send people to, over the channel framework (M0 Task 9: A3p-2's page follow ported onto M0, the
decisions D1-D6 of the task's brief): a pause for money stands above a pause for the page, and nothing turns ads on
at a page that is down (D1); publishing a page, or naming one, lifts only a pause for the page (D2); the owner's
resume never lifts Admiry's pause (D3, test_page_check and test_tools_pages); only a channel whose ads follow the page
is ever touched (D4: a channel's that does not; NewsBreak's follows it since M1 Task 5a); unpublishing stacks its
pause beside the owner's (D5,
test_tools_hosting); no pause of Admiry's claims ads someone paused by hand on the platform (D6, here and in
test_page_check and test_tick)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, config, crawl, jobs, tools_pages
from agent.channels import registry
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import FakeAdapter
from agent.tests.test_ads_run import BIZ, GOOGLE, PAGE_URL, add_plan, live, row  # noqa: F401 - live is a fixture
from agent.tests.test_page_check import (  # noqa: F401 - gads, pages and mailbox are fixtures
    PUBLIC_IP,
    _hosted_ads,
    audited,
    gads,
    mailbox,
    pages,
    run_job,
)
from agent.tests.test_tools_hosting import (
    DOMAIN,
    GOOD_HTML,
    GOOD_HTML_V2,
    URL,
    _checked,
    _kit_ready_sections,
    _launched_with_kit,
    _publish,
)
from agent.tests.test_tools_pages import _report

NB = "newsbreak"
OTHER = "reddit"  # D4's channel whose ads do not follow the page: a FakeAdapter under one of the registry's names
PAGE_DOWN = f"{ads_run.PAUSE_WORDS['page_down']}. {ads_run.PAUSE_ADVICE['page_down']}"


def _unpublish(client, key: str) -> dict:
    pid = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))["proposal_id"]
    res = call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "cu1"}, key=key)
    assert not res["isError"], tool_text(res)
    return data(res)


def _republish(client, conn, monkeypatch, key: str) -> dict:
    """publish_page(version=1) -- the hosted page back at its own address -- confirmed."""
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "rb", "version": 1}, key=key))
    _checked(monkeypatch, conn, URL)
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c"}, key=key)
    assert not res["isError"], tool_text(res)
    return data(res)


# ---------------------------------------------------------------- D1: money above the page; never on at a dead page


def test_the_new_months_lift_never_turns_ads_on_at_a_page_that_is_down(client, conn, gads, live, monkeypatch):  # noqa: F811
    """D1: a month cap stands; the hosted page the ads carry is taken down meanwhile. Unpublish's page-down pause
    never replaces the month cap (money above the page: one reason is kept, the stronger). The new month's lift --
    spend-guard's `resume_channels(reason="month_cap")` -- then finds the page down before it turns anything on: the
    ads are paused for the page instead, and no enable goes to Google. Publishing the page again lifts that pause."""
    key = _hosted_ads(client, conn, gads, monkeypatch)
    settings = config.load()
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="job:spend-guard", reason="month_cap")
    out = _unpublish(client, key)
    assert out["ads"] == "paused" and row(conn)["paused_reason"] == "month_cap"  # never replaced by the page's
    sent = len(gads.sent)

    lifted = ads_run.resume_channels(conn, settings, BIZ, reason="month_cap", by="job:spend-guard", channels=[GOOGLE])

    assert lifted["channels"][GOOGLE]["state"] == "paused"
    assert "enable" not in gads.kinds()[sent:]
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["paused_by"]) == ("paused", "page_down", "admiry")
    assert ads_run.status_sentence(r) == f"Google Search: paused by Admiry today: {PAGE_DOWN}."
    note = "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    assert conn.execute(note).fetchone()["note"] == "paused your ads on Google Search: the page they point at is down"

    assert _republish(client, conn, monkeypatch, key)["ads"] == "live"
    assert row(conn)["state"] == "live" and row(conn)["paused_reason"] is None


def test_a_resume_never_turns_ads_on_at_a_page_taken_down_without_admirys_pause(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """D1's guard on the owner's resume: the owner paused, then the hosted page went down without Admiry's own pause
    (its after-commit pause never happened -- page-check's backstop skips paused ads). The owner's resume lifts
    their pause, and the enable step finds the page the ads carry down: Admiry's page-down pause, no enable."""
    _hosted_ads(client, conn, gads, monkeypatch)
    settings = config.load()
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="owner")
    conn.execute("UPDATE page SET status = 'unpublished' WHERE business_id = %s", (BIZ,))
    sent = len(gads.sent)

    out = ads_run.resume(conn, settings, BIZ, GOOGLE)

    assert out["state"] == "paused" and "enable" not in gads.kinds()[sent:]
    r = row(conn)
    assert (r["paused_reason"], "owner_paused" in r["resources"]) == ("page_down", False)


@pytest.mark.parametrize("pending", ["move", "version"])
def test_a_pending_change_never_carries_running_ads_onto_a_page_taken_down(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    mailbox,  # noqa: F811
    monkeypatch,
    pending,
):
    """D1 for moves (fix round 1, R1): the ads run at the owner's own page; a hosted page is published while the
    switch keeps the move from Google (`url_stale` queued: the ads still carry the owner's page), then taken down --
    unpublish pauses only ads that carry the hosted address (D5), so these run on. The hourly step then has the move
    to send -- or a newer approved version, whose update would carry the page's address too: neither goes out to a
    page that answers "gone". Nothing is sent, the ads are paused for the page (by Admiry), the owner gets the
    backstop's own e-mail once (fix round 2), the move or the version stays pending, and the owner's resume says the
    way out (D3). Publishing the page again lifts the pause, and the pending change goes out with the resume."""
    key, v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    r = row(conn)
    assert (r["state"], r["resources"]["url_stale"], r["resources"]["final_url"]) == (
        "live",
        True,
        PAGE_URL + "?src=gads",
    )
    assert "ads_by_channel" not in _unpublish(client, key)  # not on the hosted address: nothing for unpublish to pause
    assert row(conn)["state"] == "live"
    newer = None
    if pending == "version":
        conn.execute(
            "UPDATE channel_campaign SET resources = resources - 'url_stale' WHERE business_id = %s AND channel = %s",
            (BIZ, GOOGLE),
        )
        conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
        newer = add_plan(conn, _kit_ready_sections(monthly_usd=1500.0))
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent = len(gads.sent)
    mailbox.clear()  # the publish's own check reports

    ads_run.launch(conn, settings, BIZ, GOOGLE)  # the hourly step

    assert gads.kinds()[sent:] == ["pause"]  # no address, no money: only the pause
    assert [m["subject"] for m in mailbox] == [ads_run.MAIL_PAGE_HELD]  # once, and it says the move was held
    text = mailbox[0]["text"]
    assert f"Admiry paused your ads on Google Search instead of moving them to {URL}: there is no page" in text
    assert "they send people to" not in text and "NewsBreak" not in text
    assert ads_run.status_sentence(row(conn)) in text
    ads_run.launch(conn, settings, BIZ, GOOGLE)  # the next hour: paused already, nothing more to tell
    assert len(mailbox) == 1
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["paused_by"]) == ("paused", "page_down", "admiry")
    assert (r["resources"]["final_url"], r["plan_id"]) == (PAGE_URL + "?src=gads", v1)  # still pending
    assert r["resources"].get("url_stale") is (True if pending == "move" else None)
    advice = ads_run._say(ads_run.PAUSED_BY_ADMIRY, ads_run.PAUSE_ADVICE["page_down"])  # R3: the way out
    for resume in (
        lambda: ads_run.resume(conn, settings, BIZ, GOOGLE),
        lambda: ads_run.resume_by_owner(conn, settings, BIZ, by="owner"),
    ):
        with pytest.raises(ToolError) as refused:
            resume()
        assert str(refused.value) == advice

    assert _republish(client, conn, monkeypatch, key)["ads"] == "live"
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["final_url"]) == ("live", None, URL + "?src=gads")
    assert "url_stale" not in r["resources"] and r["plan_id"] == (newer or v1)


# ---------------------------------------------------------------- D2: a page lifts only a pause for the page


@pytest.mark.parametrize("reason", ["month_cap", "no_funds"])
def test_publishing_or_naming_a_page_never_lifts_a_pause_for_money(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
    reason,
):
    """D2: publish_page and set_landing_page lift Admiry's pause only when it is one for the page (page_down,
    unreachable) -- a month cap or a missing balance stays, whatever page goes live, and nothing is turned on."""
    key = _hosted_ads(client, conn, gads, monkeypatch)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="job:spend-guard", reason=reason)
    sent = len(gads.sent)

    out = _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "2")

    assert (out["ads"], out["ads_by_channel"]) == ("paused", {GOOGLE: "paused"})
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", reason)

    monkeypatch.setattr(crawl, "resolve", lambda h: PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    assert data(res)["execution"]["state"] == "paused"
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["url_stale"]) == ("paused", reason, True)
    assert "enable" not in gads.kinds()[sent:]
    with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):
        ads_run.resume(conn, config.load(), BIZ, GOOGLE)


# ---------------------------------------------------------------- D4: a channel that does not follow the page


@pytest.fixture
def unfollowing(monkeypatch):
    """A channel whose ads do not follow the page (`Capabilities.follows_page` False): NewsBreak's did not in M0 and
    do since M1 Task 5a, so a fake stands in for it here, under one of the registry's names."""
    monkeypatch.setitem(registry.CHANNELS, OTHER, FakeAdapter(OTHER))


def _other_running(conn) -> None:
    """A row of the channel that does not follow the page, written as running and carrying the hosted address the way
    a page-following channel's ads would -- which nothing here may read."""
    plan_id = row(conn)["plan_id"]
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
        "VALUES (%s, %s, %s, 'live', %s, now())",
        (BIZ, OTHER, plan_id, Jsonb({"campaign": "other/1", "final_url": URL + "?src=other"})),
    )


def _other(conn) -> dict:
    r = ads_run.get(conn, BIZ, OTHER)
    return {k: r[k] for k in ("state", "paused_reason", "resources", "last_error", "paused_at", "updated_at")}


def test_a_row_whose_ads_do_not_follow_the_page_is_never_touched_by_it(
    client,
    conn,
    gads,  # noqa: F811 - the fixture imported from test_page_check under this name
    live,  # noqa: F811 - the fixture imported from test_ads_run under this name
    pages,  # noqa: F811 - the fixture imported from test_page_check under this name
    mailbox,  # noqa: F811 - the fixture imported from test_page_check under this name
    monkeypatch,
    unfollowing,
):
    """D4: a channel whose ads do not follow the page (`Capabilities.follows_page` False) is never reached by it:
    unpublishing pauses Google Search alone, the backstop and page-check pause and resume Google Search alone, a
    publish and set_landing_page flag and move Google Search alone, and the owner's e-mails name Google Search alone.
    The other channel's row is exactly as it was throughout."""
    key = _hosted_ads(client, conn, gads, monkeypatch)
    _other_running(conn)
    before = _other(conn)
    assert not ads_run.follows_page(OTHER) and ads_run.follows_page(GOOGLE)

    out = _unpublish(client, key)
    assert out["ads_by_channel"] == {GOOGLE: "paused"} and row(conn)["paused_reason"] == "page_down"
    mailbox.clear()
    assert run_job()["page_down"] == 0  # the backstop: NewsBreak's row carries no page it can read
    back = _republish(client, conn, monkeypatch, key)
    assert back["ads_by_channel"] == {GOOGLE: "live"} and row(conn)["state"] == "live"

    pages.down(URL, 502)
    run_job()
    assert run_job()["paused"] == 1  # Google Search's, alone
    assert row(conn)["paused_reason"] == "unreachable"
    assert [a["channel"] for a in audited(conn)] == [GOOGLE]
    assert "Google Search" in mailbox[-1]["text"] and registry.label(OTHER) not in mailbox[-1]["text"]
    pages.up(URL)
    assert run_job()["resumed"] == 1 and row(conn)["state"] == "live"

    monkeypatch.setattr(crawl, "resolve", lambda h: PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)
    assert set(data(res)["execution"]["channels"]) == {GOOGLE}
    assert row(conn)["resources"]["final_url"] == PAGE_URL + "?src=gads"

    assert _other(conn) == before
    assert "url_stale" not in ads_run.due(config.load(), OTHER)
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, "https://elsewhere.example/") is True  # Google's, flagged
    assert "url_stale" not in ads_run.get(conn, BIZ, OTHER)["resources"]
    assert ads_run.on_host(conn, BIZ, "bright-smile-dental.business.test") == []  # Google moved off; the other unread


def test_the_month_cap_and_the_page_pauses_ignore_a_channel_that_does_not_follow_the_page(conn, unfollowing):
    """D4, the lifecycle's own helpers: a channel whose ads do not follow the page has no page pause to lift, no
    address to retry, and is never paused for a page that is down (`pause_page_down` is False and writes nothing)."""
    conn.execute("INSERT INTO business (id) VALUES (%s)", (BIZ,))
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) VALUES (%s, %s, 'pl_1', "
        "'live', %s)",
        (BIZ, OTHER, Jsonb({"campaign": "other/1", "refused_url": "https://x.example/"})),
    )
    assert ads_run.page_rows(conn, BIZ) == []
    assert ads_run.retry_refused_address(conn, BIZ, "https://x.example/") == []
    assert ads_run.pause_page_down(conn, config.load(), BIZ, OTHER) is False
    assert ads_run.get(conn, BIZ, OTHER)["state"] == "live"
    # page-check's channels: the ones that follow -- NewsBreak among them since M1 Task 5a, never the other
    assert jobs.page_check(conn, 0, 1)["modes"] == {GOOGLE: "off", NB: "off"}
