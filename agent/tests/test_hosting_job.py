"""The daily `hosting-minimum` job (A3p-3 spec §3.1, §3.3, P14-P16, P20; plan 2026-10-04-agent-a3p-3-1 Q4-Q7): a hosted
site that is not covered gets its 30 days with a start e-mail, the 7-day and 1-day e-mails, and comes down at the end
with the ads that point at it paused first; a covered site's countdown clears; 90 days after a takedown the files go and
the address is free; the leads and the events stay. The clock is `ads_run.now` (the `clock` fixture)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent import ads_run, config, hosted, hosting_minimum, jobs, mail, tick
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_DB_URL
from agent.tests.test_ads_run import (  # noqa: F401 (fixtures)
    BIZ,
    GOOGLE,
    clock,
    gads,
    live,
    queued,
    sections,
    seed,
)

HOST = "bright-smile-dental.business.test"
URL = f"https://{HOST}/"
DAY0 = date(2026, 10, 4)
HTML = "<html><body><h1>Invisalign in Palo Alto</h1><p>Call us.</p></body></html>"


@pytest.fixture
def mailbox(monkeypatch) -> list[dict]:
    """Owner e-mail through Resend, mocked: each sent message's JSON body, in order."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"em_{len(sent)}"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return sent


def _on(day: date, clock_fn) -> None:
    clock_fn(datetime(day.year, day.month, day.day, 14, 47, tzinfo=UTC))


def _hosted(conn, *, monthly_usd: float = 150.0) -> str:
    """seed()'s business with its page hosted by Admiry at HOST (published), before anything launches; returns the
    page id. $150 a month is under the minimum: the site is not covered by its budget."""
    seed(conn, secs=sections(monthly_usd=monthly_usd))
    page_id = conn.execute("SELECT id FROM page WHERE business_id = %s", (BIZ,)).fetchone()["id"]
    conn.execute(
        "UPDATE page SET source = 'admiry', hostname = %s, url = %s, html = %s, status = 'published' WHERE id = %s",
        (HOST, URL, HTML, page_id),
    )
    conn.execute(
        "INSERT INTO page_version (page_id, version, html, title, source) VALUES (%s, 1, %s, 't', 'admiry')",
        (page_id, HTML),
    )
    return page_id


def _launch(conn) -> dict:
    plan_id = conn.execute("SELECT id FROM plan WHERE business_id = %s AND status = 'approved'", (BIZ,)).fetchone()
    queued(conn, plan_id["id"])
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live", out
    row = ads_run.get(conn, BIZ, GOOGLE)
    assert row["resources"]["final_url"].startswith(URL), row["resources"]  # the ads carry the hosted address
    return row


def _run() -> dict:
    return tick.run("hosting-minimum", url=TEST_DB_URL)


def _page(conn, page_id: str) -> dict:
    return dict(conn.execute("SELECT * FROM page WHERE id = %s", (page_id,)).fetchone())


def _notes(conn) -> list[str]:
    rows = conn.execute(
        "SELECT result FROM agent_action WHERE tool = 'job.hosting_minimum' AND business_id = %s ORDER BY id", (BIZ,)
    ).fetchall()
    return [r["result"]["note"] for r in rows]


def _plain(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


def test_the_countdown_reminds_then_takes_the_site_down_ads_first(  # Review Focus 2
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
    monkeypatch,
    client,
):
    """A site live when the rule ships (P20) with ads at $150 a month: the first run starts its 30 days (the start
    e-mail, an end date no earlier than that day + 30); 7 days and 1 day before, one e-mail each; at the end the ads
    that carry its address are paused before the site goes down, the hostname answers 410, no certificate is vouched
    for, the files are kept 90 days, and one e-mail says so. Every run in between, and every run again the same day,
    changes nothing."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    _on(DAY0, clock)
    page_id = _hosted(conn)
    _launch(conn)
    out = _run()
    assert (out["checked"], out["started"], out["covered"]) == (1, 1, 0)
    ends = DAY0 + timedelta(days=30)
    assert _page(conn, page_id)["countdown_ends"] == ends
    assert len(mailbox) == 1 and mailbox[0]["subject"] == f"Free hosting for {HOST} ends on November 3, 2026"
    start = mailbox[0]["text"]
    assert start.startswith(
        hosting_minimum.LAPSED.format(
            host=HOST, date="November 3, 2026", link=f"http://testserver/connect/site?b={BIZ}"
        )
    )
    assert _plain(start)
    assert _run()["started"] == 0 and len(mailbox) == 1  # once

    for day, sent in ((DAY0 + timedelta(days=22), 1), (DAY0 + timedelta(days=23), 2), (DAY0 + timedelta(days=28), 2)):
        _on(day, clock)
        _run()
        assert len(mailbox) == sent, day
    assert mailbox[1]["subject"] == f"Your site {HOST} stays online until November 3, 2026"
    assert mailbox[1]["text"].startswith(hosting_minimum.STAYS_UNTIL.format(host=HOST, date="November 3, 2026"))
    _on(DAY0 + timedelta(days=29), clock)
    assert _run()["reminded"] == 1 and _run()["reminded"] == 0
    assert len(mailbox) == 3 and mailbox[2]["subject"] == f"One day left for your site {HOST}"

    seen: list[str] = []
    real_pause = ads_run.pause

    def pause(conn_, settings, biz, channel, **kw):
        seen.append(conn_.execute("SELECT status FROM page WHERE id = %s", (page_id,)).fetchone()["status"])
        return real_pause(conn_, settings, biz, channel, **kw)

    monkeypatch.setattr(ads_run, "pause", pause)
    _on(ends, clock)
    out = _run()
    assert out["taken_down"] == 1 and seen == ["published"]  # the ads paused while the site was still up
    page = _page(conn, page_id)
    assert (page["status"], page["taken_down_reason"], page["purge_after"]) == (
        "unpublished",
        "hosting_minimum",
        ends + timedelta(days=90),
    )
    assert (page["countdown_ends"], page["countdown_mail"]) == (None, None)
    row = ads_run.get(conn, BIZ, GOOGLE)
    assert (row["state"], row["paused_reason"], row["resources"]["paused_by"]) == (
        "paused",
        "page_down",
        "job:hosting-minimum",
    )
    assert "pause" in gads.kinds()
    assert out["ops_by_channel"].get(GOOGLE, 0) == out["ops"] >= 1
    assert len(mailbox) == 4 and mailbox[3]["subject"] == f"Your site {HOST} is offline"
    down = mailbox[3]["text"]
    assert down.startswith(
        f"Your site {HOST} is offline. We keep its pages and photos until February 1, 2027. To bring it back, get "
        "your ads ready at $200 a month and publish it again. Download it: "
        f"http://testserver/connect/site?b={BIZ}."
    )
    assert "Google Search" in down and _plain(down)
    assert hosted.serve(conn, config.load(), HOST, "/", "GET").status_code == 410
    assert client.get("/pages/ask", params={"domain": HOST}).status_code == 404
    assert _run()["taken_down"] == 0 and len(mailbox) == 4
    assert _notes(conn) == [
        hosting_minimum.NOTE_START.format(host=HOST, date="November 3, 2026"),
        hosting_minimum.NOTE_REMINDER.format(host=HOST, date="November 3, 2026"),
        hosting_minimum.NOTE_REMINDER.format(host=HOST, date="November 3, 2026"),
        hosting_minimum.NOTE_DOWN.format(host=HOST, date="February 1, 2027"),
    ]
    assert all(_plain(n) for n in _notes(conn))


def test_coverage_clears_the_countdown_and_says_so_only_after_an_email(  # Review Focus 5
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
):
    """A countdown a publish started (no e-mail went) clears silently once the ads spend $200; one whose start e-mail
    went clears with "stays online"."""
    _on(DAY0, clock)
    page_id = _hosted(conn)
    conn.execute(
        "UPDATE page SET countdown_ends = %s, countdown_mail = %s WHERE id = %s",
        (DAY0 + timedelta(days=12), Jsonb({"start": "publish"}), page_id),
    )
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, %s, %s, 210000000, 0, 0, 0, 0, 0, 0, 'api')",
        (BIZ, GOOGLE, DAY0 - timedelta(days=2)),
    )
    out = _run()
    assert (out["covered"], out["cleared"], len(mailbox)) == (1, 1, 0)
    assert _page(conn, page_id)["countdown_ends"] is None
    conn.execute(
        "UPDATE page SET countdown_ends = %s, countdown_mail = %s WHERE id = %s",
        (DAY0 + timedelta(days=12), Jsonb({"start": "2026-09-16"}), page_id),
    )
    out = _run()
    assert out["cleared"] == 1 and len(mailbox) == 1
    assert mailbox[0]["subject"] == f"Your site {HOST} stays online"
    assert mailbox[0]["text"].strip() == f"Your ads are back above $200 a month. {HOST} stays online."
    assert _notes(conn) == [hosting_minimum.NOTE_KEPT.format(host=HOST)] * 2


def test_a_missed_day_sends_one_reminder_never_two(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
):
    """The job did not run on the 7-day mark: the day it runs with one day left, the owner gets the 1-day e-mail alone."""
    _on(DAY0, clock)
    page_id = _hosted(conn)
    conn.execute(
        "UPDATE page SET countdown_ends = %s, countdown_mail = %s WHERE id = %s",
        (DAY0 + timedelta(days=1), Jsonb({"start": "2026-09-05"}), page_id),
    )
    assert _run()["reminded"] == 1 and _run()["reminded"] == 0
    assert [m["subject"] for m in mailbox] == [f"One day left for your site {HOST}"]
    assert set(_page(conn, page_id)["countdown_mail"]) == {"start", "d1", "d7"}


def test_an_exempt_site_is_left_alone(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
):
    _on(DAY0, clock)
    page_id = _hosted(conn)
    hosting_minimum.set_exempt(conn, BIZ, "the pilot", by="ops:cli")
    out = _run()
    assert (out["exempt"], out["started"], len(mailbox)) == (1, 0, 0)
    assert _page(conn, page_id)["countdown_ends"] is None
    _on(DAY0 + timedelta(days=60), clock)
    assert _run()["taken_down"] == 0 and _page(conn, page_id)["status"] == "published"


def test_the_purge_frees_the_address_and_keeps_the_leads_and_events(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    """90 days after a takedown for the minimum: the versions, previews, photos and upload links go, and the address is
    free for another business; the page row, its leads and its events stay. An owner's own takedown is never purged."""
    _on(DAY0, clock)
    page_id = _hosted(conn)
    conn.execute(
        "UPDATE page SET status = 'unpublished', taken_down_reason = 'hosting_minimum', taken_down_at = now(), "
        "purge_after = %s WHERE id = %s",
        (DAY0 + timedelta(days=1), page_id),
    )
    conn.execute(
        "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height) VALUES ('ph_1', %s, '\\x00', "
        "'image/jpeg', 1, 1)",
        (page_id,),
    )
    hosted.save_preview(conn, page_id, HTML, "t")
    conn.execute(
        "INSERT INTO site_spec (id, business_id, page_id, version, family, style, data) "
        "VALUES ('ss_1', %s, %s, 1, 'home_services', 'solid', '{\"facts\": {\"phone\": \"704-555-0142\"}}')",
        (BIZ, page_id),
    )
    conn.execute(
        "UPDATE page SET site_spec_id = 'ss_1', site_draft = %s WHERE id = %s",
        (Jsonb({"family": "home_services", "facts": {"warranty": "10 years"}}), page_id),
    )  # the owner's own answers, kept between builds (A3p-6 batch 2)
    conn.execute(
        "INSERT INTO upload_token (token, page_id, expires_at) VALUES ('up_1', %s, now() + interval '1 day')",
        (page_id,),
    )
    conn.execute(
        "INSERT INTO lead (id, business_id, page_id, kind, fields) VALUES ('ld_1', %s, %s, 'form', '{}')",
        (BIZ, page_id),
    )
    conn.execute("INSERT INTO page_event (page_id, kind) VALUES (%s, 'visit')", (page_id,))
    assert _run()["purged"] == 0  # not yet: the files are kept until purge_after
    _on(DAY0 + timedelta(days=1), clock)
    assert _run()["purged"] == 1 and _run()["purged"] == 0
    page = _page(conn, page_id)
    assert (page["hostname"], page["url"], page["html"], page["sections"], page["purge_after"]) == (
        None,
        "",
        None,
        None,
        None,
    )
    assert page["taken_down_reason"] == "hosting_minimum"  # coming back still needs the ads ready (P17)
    for table in ("page_version", "page_preview", "page_photo", "upload_token", "site_spec"):
        assert conn.execute(f"SELECT count(*) AS n FROM {table} WHERE page_id = %s", (page_id,)).fetchone()["n"] == 0
    assert (page["site_spec_id"], page["site_draft"]) == (None, None)  # the owner's facts go with the site's documents
    assert conn.execute("SELECT count(*) AS n FROM lead WHERE page_id = %s", (page_id,)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM page_event WHERE page_id = %s", (page_id,)).fetchone()["n"] == 1
    assert hosted.hostname_for(conn, "biz_2", "Bright Smile Dental", "business.test") == HOST  # the address is free
    assert _notes(conn) == [hosting_minimum.NOTE_PURGED.format(host=HOST)]


def test_an_owners_own_takedown_is_never_purged(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    _on(DAY0, clock)
    page_id = _hosted(conn)
    conn.execute(
        "UPDATE page SET status = 'unpublished', taken_down_reason = 'owner', taken_down_at = now() WHERE id = %s",
        (page_id,),
    )
    _on(DAY0 + timedelta(days=400), clock)
    assert _run()["purged"] == 0
    assert _page(conn, page_id)["hostname"] == HOST


def test_the_job_is_scheduled_and_counted_with_the_platform_jobs():
    assert jobs.JOBS["hosting-minimum"] is jobs.hosting_minimum
    assert "hosting-minimum" in jobs.OPS_JOBS


def test_one_business_failing_never_stops_the_run(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
    monkeypatch,
):
    _on(DAY0, clock)
    _hosted(conn)
    monkeypatch.setattr(hosting_minimum, "judge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = jobs.hosting_minimum(conn, 0, 1)
    assert out["errors"] == 1 and out["started"] == 0


def test_a_business_that_is_not_active_is_not_judged(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
):
    _on(DAY0, clock)
    _hosted(conn)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = %s", (BIZ,))
    assert _run()["checked"] == 0 and mailbox == []


def test_a_site_the_owners_own_page_replaced_loses_only_admirys_files(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    """Taken down for the minimum, then the owner named their own page for the ads: 90 days on, Admiry's files and
    the hosted address go, while the owner's page -- its address, its status and its check -- stays as it is."""
    _on(DAY0, clock)
    page_id = _hosted(conn)
    conn.execute(
        "UPDATE page SET source = 'owner', url = 'https://brightsmile.example/', status = 'published', "
        "taken_down_reason = 'hosting_minimum', purge_after = %s, check_json = '{\"ok\": true}' WHERE id = %s",
        (DAY0, page_id),
    )
    assert _run()["purged"] == 1
    page = _page(conn, page_id)
    assert (page["url"], page["status"], page["check_json"], page["hostname"], page["html"]) == (
        "https://brightsmile.example/",
        "published",
        {"ok": True},
        None,
        None,
    )
    assert conn.execute("SELECT count(*) AS n FROM page_version WHERE page_id = %s", (page_id,)).fetchone()["n"] == 0


def test_a_takedown_that_loses_the_race_with_a_publish_lifts_its_own_pause(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    mailbox,
    monkeypatch,
):
    """The owner republishes (a new countdown: their ads became ready) between the takedown's pause and its write: the
    site stays up, and the pause the takedown made for it is lifted -- no paid click is held back from a live page."""
    _on(DAY0, clock)
    page_id = _hosted(conn)
    _launch(conn)
    ends = DAY0 + timedelta(days=30)
    conn.execute(
        "UPDATE page SET countdown_ends = %s, countdown_mail = %s WHERE id = %s",
        (ends, Jsonb({"start": "2026-10-04", "d7": "2026-10-27", "d1": "2026-11-02"}), page_id),
    )
    real_pause = ads_run.pause

    def pause_then_republish(conn_, settings, biz, channel, **kw):
        out = real_pause(conn_, settings, biz, channel, **kw)
        conn_.execute(  # the publish's own write: a new countdown, the page live
            'UPDATE page SET countdown_ends = %s, countdown_mail = \'{"start": "publish"}\' WHERE id = %s',
            (ends + timedelta(days=30), page_id),
        )
        return out

    monkeypatch.setattr(ads_run, "pause", pause_then_republish)
    _on(ends, clock)
    out = _run()
    assert out["taken_down"] == 0 and _page(conn, page_id)["status"] == "published"
    row = ads_run.get(conn, BIZ, GOOGLE)
    assert row["paused_reason"] is None and row["state"] in ("live", "launching")
    assert [m["subject"] for m in mailbox] == []  # no takedown e-mail


def test_the_digest_line_is_the_countdowns_sentence(
    conn,
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    _on(DAY0, clock)
    page_id = _hosted(conn)
    assert hosting_minimum.digest_line(conn, BIZ) is None
    conn.execute("UPDATE page SET countdown_ends = %s WHERE id = %s", (DAY0 + timedelta(days=30), page_id))
    assert hosting_minimum.digest_line(conn, BIZ) == hosting_minimum.STAYS_UNTIL.format(
        host=HOST, date="November 3, 2026"
    )
