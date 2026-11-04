"""jobs.page_check, the hourly page-check (spec 2026-09-28 admiry-pages-design §8; plan H10; A3p-2 Task 4), and
`landing.reachable`, the fetch it runs. A page that does not open twice in a row pauses the ads that point at it
(Admiry's pause, `paused_reason = "unreachable"`, by job:page-check) and tells the owner once; the page opening again
undoes that pause -- and only that one: an owner's pause, Admiry's page-down pause, or a pause for money, is never
undone by it. M0 Task 9: only channels whose ads follow the page are touched, each on its own (a NewsBreak row never
is). Google Ads is test_ads_run.py's stateful fake; the pages are a MockTransport answering by URL; page-check's
clock is a fake one that `run_job` moves an hour on, as the hourly timer would (failures count only 45 minutes apart,
ruling H33).

A3p-5 batch 2 Task 4 (plan Q9, spec W15): an owner page's body is read with the check's own fetch
(`landing.fetch_body`) and judged by the check's tracking rule; the code going missing e-mails the owner once and the
status says so, its coming back is recorded silently, and an answer that is no page to judge (a bot challenge, a
non-2xx, no answer) changes nothing; hosted pages are never read this way. The row reads what the hourly check saw
(ruling R0-d), a check's fetch records the site's platform once (R1-c), and a pending install is watched until its code
shows up, which the check's report tells the owner (R3-a). The final review: the watch ends for good at the first pass
(F-1), a website on file without a scheme is fetched as https:// (F-3), and a run in which every owner page that had
the code came back without it records nothing (F-10)."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, business, config, crawl, db, hosted, jobs, landing, mail, site_install, tick, tools_pages
from agent.channels.google_search import adapter as google
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, TEST_DB_URL, call_tool, data
from agent.tests.fixtures import business_with_key
from agent.tests.test_ads_run import (
    BIZ,
    CHECK_OK,
    GOOGLE,
    PAGE_URL,
    REAL_CLIENT,
    FakeGoogle,
    launched,
    queued,
    row,
    seed,
)
from agent.tests.test_landing import _plan, _serve, fake  # noqa: F401 - fake is a pytest fixture used by name
from agent.tests.test_tools_hosting import DOMAIN, GOOD_HTML, HOSTNAME, URL, _checked, _launched_with_kit, _publish

PUBLIC_IP = ["93.184.216.34"]  # RFC 5737 documentation range: never a real, reachable address
NB = "newsbreak"
ADS_URL = PAGE_URL + "?src=gads"
LABEL = "Google Search"
NOTE_PAUSED = jobs.NOTE_PAUSED.format(label=LABEL)
NOTE_RESUMED = jobs.NOTE_RESUMED.format(label=LABEL)
NOTE_PAGE_GONE = jobs.NOTE_PAGE_GONE.format(label=LABEL)
# the status lines of Admiry's pauses for the page (M0's `state_clause`, then what ends the pause)
UNREACHABLE = f"{ads_run.PAUSE_WORDS['unreachable']}. {ads_run.PAUSE_ADVICE['unreachable']}"
PAGE_DOWN = f"{ads_run.PAUSE_WORDS['page_down']}. {ads_run.PAUSE_ADVICE['page_down']}"


PAGE_BODY = "<html><body>Bright Smile Dental</body></html>"  # a page without Admiry's code


class FakePages:
    """The web as page-check sees it: `answer[url]` is a status code (200 when unset), an exception to raise, or a
    whole answer (an httpx.Response) to give as it is; `body[url]` is the page a status code answers with (PAGE_BODY
    when unset); `fetched` holds every URL asked, in order."""

    def __init__(self) -> None:
        self.answer: dict[str, int | Exception | httpx.Response] = {}
        self.body: dict[str, str] = {}
        self.fetched: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.fetched.append(url)
        answer = self.answer.get(url, 200)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(answer, text=self.body.get(url, PAGE_BODY))

    def down(self, url: str = PAGE_URL, status: int = 503) -> None:
        self.answer[url] = status

    def up(self, url: str = PAGE_URL) -> None:
        self.answer.pop(url, None)


@pytest.fixture
def gads(monkeypatch, ads_api):
    """test_ads_run.py's stateful fake of Google Ads, for every client the Google Search adapter opens -- the job's
    included."""
    google_ads = FakeGoogle()  # not `fake`: that name is test_landing's fixture, which the walk's tests use
    transport = httpx.MockTransport(google_ads.handler)
    monkeypatch.setattr(google, "client", functools.partial(REAL_CLIENT, transport=transport))
    return google_ads


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")


class Clock:
    """page-check's clock (`jobs._now`), moved by hand: `run_job` moves it an hour on before each run."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, by: timedelta) -> None:
        self.now += by


@pytest.fixture
def pages(monkeypatch):
    """The pages (FakePages) and page-check's clock (`pages.clock`)."""
    web = FakePages()
    web.clock = Clock()
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    monkeypatch.setattr(jobs, "_page_transport", lambda: httpx.MockTransport(web.handler))
    monkeypatch.setattr(jobs, "_now", web.clock)
    return web


@pytest.fixture
def mailbox(monkeypatch):
    """Owner e-mail on, Resend mocked: every message sent, as its JSON body."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"em_{len(sent)}"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return sent


def run_job(shard: str = "0/1", *, after: timedelta = timedelta(hours=1)) -> dict:
    """One run, the way the hourly timer runs it (the job_run ledger row included), `after` the one before it on
    page-check's clock (the `pages` fixture's)."""
    if isinstance(jobs._now, Clock):
        jobs._now.advance(after)
    return tick.run("page-check", shard=shard, url=TEST_DB_URL)


def counters(conn, biz: str = BIZ) -> dict | None:
    r = conn.execute("SELECT check_json FROM page WHERE business_id = %s AND status = 'published'", (biz,)).fetchone()
    return (r["check_json"] or {}).get("reachable")


def audited(conn) -> list[dict]:
    rows = conn.execute("SELECT actor, result FROM agent_action WHERE tool = 'job.page_check' ORDER BY id").fetchall()
    assert all(r["actor"] == "job:page-check" for r in rows)
    return [r["result"] for r in rows]


def plain(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text))


# ---------------------------------------------------------------- Review Focus 3


def test_page_check_pauses_after_two_failures_and_resumes_only_its_own_pause(client, conn, gads, live, pages, mailbox):
    """Review Focus 3: the page stops opening -- the first hourly failure is counted and pauses nothing; the second
    pauses the ads (Admiry's pause, sent to Google Ads) and e-mails the owner, once; a third changes nothing more.
    The page opening again resumes them. An owner's own pause, the page up or down, is never touched by the job."""
    key, _v1 = launched(conn, gads)
    campaign = row(conn)["resources"]["campaign"]
    assert row(conn)["resources"]["final_url"] == ADS_URL
    sent = len(gads.sent)
    pages.down()

    first = run_job()
    assert (first["checked"], first["down"], first["paused"], first["ops"]) == (1, 1, 0, 0)
    assert counters(conn)["fails"] == 1 and counters(conn)["detail"] == "HTTP 503"
    assert row(conn)["state"] == "live" and gads.sent[sent:] == [] and mailbox == []

    second = run_job()
    assert (second["paused"], second["ops"]) == (1, 1)
    assert gads.kinds()[sent:] == ["pause"] and gads.campaigns[campaign]["status"] == "PAUSED"
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"]) == ("paused", "job:page-check", "unreachable")
    assert counters(conn)["fails"] == 2
    status = ads_run.status_sentence(r)
    assert status == f"Google Search: paused by Admiry today: {UNREACHABLE}." and plain(status)
    assert len(mailbox) == 1
    down_mail = mailbox[0]
    assert down_mail["to"] == ["owner@example.com"] and down_mail["subject"] == jobs.MAIL_PAGE_DOWN
    for part in (PAGE_URL, "What Admiry saw: HTTP 503", status, "Open the page on your phone"):
        assert part in down_mail["text"], part
    assert plain(down_mail["text"]) and "Admiry hosts this page" not in down_mail["text"]

    third = run_job()  # still down an hour later: counted, nothing more -- no second pause, no second e-mail
    assert (third["paused"], third["ops"]) == (0, 0) and counters(conn)["fails"] == 3
    assert gads.kinds()[sent:] == ["pause"] and len(mailbox) == 1

    pages.up()
    back = run_job()
    assert back["resumed"] == 1 and back["ops"] >= 1
    assert "enable" in gads.kinds()[sent:] and gads.campaigns[campaign]["status"] == "ENABLED"
    r = row(conn)
    assert r["state"] == "live" and not {"paused_by", "pause_owed"} & set(r["resources"]) and not r["paused_reason"]
    assert counters(conn)["fails"] == 0
    assert len(mailbox) == 2 and mailbox[1]["subject"] == jobs.MAIL_PAGE_UP and PAGE_URL in mailbox[1]["text"]
    assert plain(mailbox[1]["text"])
    assert [(a["note"], a["email"], a["to"], a["channel"]) for a in audited(conn)] == [
        (NOTE_PAUSED, "sent", "paused", GOOGLE),
        (NOTE_RESUMED, "sent", "live", GOOGLE),
    ]
    sentences = [e["sentence"] for e in data(call_tool(client, "get_audit_log", {}, key=key))["entries"]]
    assert f"Admiry {NOTE_PAUSED}" in sentences and f"Admiry {NOTE_RESUMED}" in sentences
    assert all(plain(s) for s in sentences)

    # the owner's own pause (their assistant's): the page up, the job leaves it alone ...
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    sent = len(gads.sent)
    assert run_job()["resumed"] == 0
    assert (row(conn)["state"], row(conn)["resources"]["paused_by"]) == ("paused", "assistant")
    # ... and the page down twice more never makes it Admiry's (nothing recorded, nothing sent, nobody e-mailed)
    pages.down()
    run_job()
    assert run_job()["paused"] == 0
    r = row(conn)
    assert (r["resources"]["paused_by"], r["paused_reason"]) == ("assistant", None)
    assert gads.sent[sent:] == [] and len(mailbox) == 2 and len(audited(conn)) == 2


def test_limited_ads_are_paused_too(conn, gads, live, pages, mailbox):
    """`limited` (Google holding the ads back, set by ads-sync) still spends: it is paused like `live`."""
    launched(conn, gads)
    conn.execute("UPDATE channel_campaign SET state = 'limited' WHERE business_id = %s AND channel = %s", (BIZ, GOOGLE))
    pages.down()
    run_job()
    assert run_job()["paused"] == 1
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "unreachable")


# ---------------------------------------------------------------- whose pause it is


def test_admirys_page_down_pause_is_never_undone_by_the_page_check(conn, gads, live, pages, mailbox):
    """Admiry's other pause -- the page the ads point at is gone (unpublished, or the owner's own page took its
    place, H29) -- is not page-check's: a fetch that answers never undoes it."""
    launched(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="admiry", reason="page_down")
    sent = len(gads.sent)

    out = run_job()

    assert (out["checked"], out["resumed"]) == (1, 0) and gads.sent[sent:] == [] and mailbox == []
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"]) == ("paused", "admiry", "page_down")
    assert ads_run.status_sentence(r) == f"Google Search: paused by Admiry today: {PAGE_DOWN}."


def test_a_resume_waits_while_the_ads_point_at_another_address(conn, gads, live, pages, mailbox):
    """page-check's own pause, the page moved meanwhile and the move not on the ads yet: the page at its new address
    opens, but the ads still carry the old one -- a resume would turn them on there. The row is left alone."""
    launched(conn, gads)
    pages.down()
    run_job()
    run_job()
    assert row(conn)["paused_reason"] == "unreachable"
    moved = "https://brightsmile.example/moved"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (moved, BIZ))
    sent = len(gads.sent)

    out = run_job()

    assert pages.fetched[-1] == moved and counters(conn)["fails"] == 0
    assert out["resumed"] == 0 and gads.sent[sent:] == [] and len(mailbox) == 1
    assert row(conn)["state"] == "paused" and row(conn)["resources"]["final_url"] == ADS_URL


def test_a_page_the_ads_do_not_point_at_never_pauses_them(conn, gads, live, pages, mailbox):
    """The ads carry another address (a move not sent yet, or refused): this page not opening is counted, and the
    ads, which send people elsewhere, are left alone."""
    launched(conn, gads)
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = %s AND channel = %s",
        (Jsonb({"final_url": "https://brightsmile.example/old?src=gads"}), BIZ, GOOGLE),
    )
    pages.down()
    run_job()
    out = run_job()
    assert (out["down"], out["paused"]) == (1, 0) and counters(conn)["fails"] == 2
    assert row(conn)["state"] == "live" and mailbox == []


def test_an_owners_pause_landing_while_admirys_pause_is_out_stands_beside_it(conn, gads, live, pages, mailbox):
    """Carried from Task 3's final review (a), re-specified by M0 Task 9 (the two pauses of M0 Task 4 fix round 1):
    the owner pauses while page-check's PAUSED status is on its way to Google Ads (S17: an owner's pause never waits
    for the step's lock). Neither pause takes the other over: Admiry's final write keeps the owner's mark and puts
    its reason beside it, so the owner is told why Admiry paused too. The page opening again lifts page-check's own
    pause and no more -- the owner's stands, the ads stay off, and nobody is told they are back on."""
    launched(conn, gads)

    def the_owner_pauses_meanwhile(body):
        with db.connect(TEST_DB_URL) as other:
            ads_run.pause(other, config.load(), BIZ, GOOGLE, by="owner")
        return FakeGoogle._apply(gads, body)

    gads._pause = the_owner_pauses_meanwhile
    pages.down()
    run_job()
    out = run_job()

    r = row(conn)
    assert (r["state"], r["resources"]["owner_paused"], r["paused_reason"]) == ("paused", True, "unreachable")
    assert (out["paused"], out["ops"]) == (1, 1) and [m["subject"] for m in mailbox] == [jobs.MAIL_PAGE_DOWN]
    assert ads_run.status_sentence(r).startswith("Google Search: paused by you and by Admiry today:")
    pages.up()
    sent = len(gads.sent)
    assert run_job()["resumed"] == 0
    r = row(conn)
    assert (r["state"], r["resources"]["owner_paused"], r["paused_reason"]) == ("paused", True, None)
    assert gads.sent[sent:] == [] and len(mailbox) == 1
    assert ads_run.status_sentence(r).startswith("Google Search: paused by you today.")  # (its own pause, owed)


def test_admirys_pause_stands_beside_the_owners_and_each_is_lifted_by_its_own_resume(conn, gads, live):
    """`ads_run.pause`'s attribution under M0's two pauses (re-specified by M0 Task 9; A3p-2 had one pause and let
    the latest pauser take it over): Admiry's pause on a row the owner paused stacks its reason beside the owner's
    mark (nothing sent: the campaign is paused already), and the owner's pause over Admiry's adds the mark beside
    Admiry's reason -- neither steals the other, so nothing of Admiry's turns the owner's ads back on and the owner's
    resume never puts ads back on a page that is down. Each pause is lifted by its own resume only."""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="owner")
    sent = len(gads.sent)
    assert ads_run.pause(conn, settings, BIZ, GOOGLE, by=jobs.PAGE_CHECK, reason="unreachable")["ops"] == 0
    r = row(conn)
    assert (r["resources"]["owner_paused"], r["paused_reason"], gads.sent[sent:]) == (True, "unreachable", [])
    assert ads_run.status_sentence(r).startswith("Google Search: paused by you and by Admiry today:")

    assert ads_run.resume(conn, settings, BIZ, GOOGLE)["state"] == "paused"  # the owner's own: Admiry's stands
    assert (row(conn)["paused_reason"], "owner_paused" in row(conn)["resources"]) == ("unreachable", False)
    with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):
        ads_run.resume(conn, settings, BIZ, GOOGLE)  # M0 Task 9 D3: the owner cannot lift a page's pause
    assert ads_run.resume(conn, settings, BIZ, GOOGLE, reason="unreachable")["state"] == "live"

    ads_run.pause(conn, settings, BIZ, GOOGLE, by=jobs.PAGE_CHECK, reason="unreachable")
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="owner")
    r = row(conn)
    assert (r["resources"]["owner_paused"], r["paused_reason"]) == (True, "unreachable")
    assert ads_run.resume(conn, settings, BIZ, GOOGLE, reason="unreachable")["state"] == "paused"  # the owner's
    assert ads_run.resume(conn, settings, BIZ, GOOGLE)["state"] == "live"
    r = row(conn)
    assert not {"paused_by", "owner_paused"} & set(r["resources"]) and r["paused_reason"] is None


def test_ads_someone_paused_in_google_ads_are_never_claimed_or_turned_back_on(conn, gads, live, pages, mailbox):
    """Ruling H33: someone paused the campaign in Google Ads itself -- ads-sync reads it and marks the row `limited`
    with PAUSED_IN_GOOGLE. The page not opening twice pauses nothing (Admiry's pause is a no-op there, as on a row
    already paused): no note of Admiry's, no e-mail. The page opening again then resumes nothing -- page-check
    never turns on a campaign it did not pause."""
    launched(conn, gads)
    campaign = row(conn)["resources"]["campaign"]
    gads.campaigns[campaign]["status"] = "PAUSED"
    gads.campaign_status = {"status": "PAUSED", "primaryStatus": "PAUSED"}
    assert ads_run.sync(conn, config.load(), BIZ, GOOGLE)["last_error"] == google.PAUSED_IN_GOOGLE
    sent = len(gads.sent)
    pages.down()

    run_job()
    out = run_job()

    assert (out["paused"], out["ops"]) == (0, 0) and gads.sent[sent:] == [] and mailbox == []
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("limited", google.PAUSED_IN_GOOGLE)
    assert "paused_by" not in r["resources"] and r["paused_reason"] is None
    pages.up()
    assert run_job()["resumed"] == 0 and gads.sent[sent:] == [] and audited(conn) == []
    assert gads.campaigns[campaign]["status"] == "PAUSED"


def test_a_note_from_google_ads_landing_while_admirys_pause_waits_keeps_the_row(conn, gads, live, monkeypatch):
    """H33, the write: Admiry's pause went without the lock (a step held it, so nothing was sent to Google) and
    ads-sync's PAUSED_IN_GOOGLE note landed between its read and its write -- the write leaves the row as it is."""
    launched(conn, gads)
    real_get, noted = ads_run.get, []
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"ads:{BIZ}",))  # a step holds the business

        def read_then_the_sync_notes(conn_, biz, channel):
            got = real_get(conn_, biz, channel)
            if not noted:
                noted.append(biz)
                other.execute(
                    "UPDATE channel_campaign SET state = 'limited', last_error = %s WHERE business_id = %s "
                    "AND channel = %s",
                    (google.PAUSED_IN_GOOGLE, biz, channel),
                )
            return got

        monkeypatch.setattr(ads_run, "get", read_then_the_sync_notes)
        out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by=jobs.PAGE_CHECK, reason="unreachable")
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"ads:{BIZ}",))

    assert out["ops"] == 0 and noted == [BIZ]
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("limited", google.PAUSED_IN_GOOGLE)
    assert not {"paused_by", "pause_owed"} & set(r["resources"]) and r["paused_reason"] is None


def test_a_pause_landing_after_the_jobs_read_is_never_resumed(conn, gads, live, pages, mailbox, monkeypatch):
    """Ruling H33: page-check read its own pause and the page opened; the owner's assistant pauses before the resume
    runs (their pause beside page-check's, M0's two pauses). The resume, on the row read under the lock, lifts
    page-check's own pause alone -- the assistant's stands: nothing is sent, nothing counted, nobody told "back
    on"."""
    launched(conn, gads)
    pages.down()
    run_job()
    run_job()
    pages.up()
    real = ads_run.resume

    def the_assistant_pauses_first(conn_, settings, biz, channel, **kw):
        with db.connect(TEST_DB_URL) as other:
            ads_run.pause(other, config.load(), biz, channel, by="assistant")
        return real(conn_, settings, biz, channel, **kw)

    monkeypatch.setattr(ads_run, "resume", the_assistant_pauses_first)
    sent = len(gads.sent)

    out = run_job()

    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"]) == ("paused", "assistant", None)
    assert (out["resumed"], out["ops"], gads.sent[sent:]) == (0, 0, [])
    assert [m["subject"] for m in mailbox] == [jobs.MAIL_PAGE_DOWN] and len(audited(conn)) == 1


@pytest.mark.parametrize("when", ["before the resume reads the row", "between its read and its write"])
def test_a_page_taken_down_before_the_resume_keeps_the_ads_off(conn, gads, live, pages, mailbox, monkeypatch, when):
    """Ruling H33: page-check's own pause, the page counted open -- and then taken down before the resume turned
    anything on. The condition "the page is still published at the address fetched" holds the resume back, on the
    row read under the lock and in the UPDATE's own WHERE: the ads never come on at a page that answers "gone"."""
    launched(conn, gads)
    pages.down()
    run_job()
    run_job()
    pages.up()

    def unpublish() -> None:
        with db.connect(TEST_DB_URL) as other:
            other.execute("UPDATE page SET status = 'unpublished' WHERE business_id = %s", (BIZ,))

    if when == "before the resume reads the row":
        real = ads_run.resume

        def unpublished_first(conn_, settings, biz, channel, **kw):
            unpublish()
            return real(conn_, settings, biz, channel, **kw)

        monkeypatch.setattr(ads_run, "resume", unpublished_first)
    else:
        real_holds = ads_run._resume_holds

        def holds_then_unpublished(*args):
            holds = real_holds(*args)
            unpublish()
            return holds

        monkeypatch.setattr(ads_run, "_resume_holds", holds_then_unpublished)
    sent = len(gads.sent)

    out = run_job()

    assert (out["resumed"], row(conn)["state"], gads.sent[sent:]) == (0, "paused", [])
    assert [m["subject"] for m in mailbox] == [jobs.MAIL_PAGE_DOWN]


def test_an_unpublish_committed_after_the_resumes_locked_read_keeps_the_ads_off(
    conn, gads, live, pages, mailbox, monkeypatch
):
    """Ruling H34 (the resume UPDATE's own guard, pinned): the unpublish's transaction -- the page taken down, and
    page-check's pause made Admiry's page-down one (`mark_page_down`) -- opens right after the resume's locked read
    found its condition holding, and commits while the resume's UPDATE waits on the row. The UPDATE re-checks its
    WHERE on the row as committed: the pause is no longer page-check's `unreachable` one, so nothing is resumed. The
    page clause alone cannot see this unpublish -- the subquery reads the UPDATE's own snapshot, taken before it
    committed; `paused_reason IS NOT DISTINCT FROM <the reason read>` does (M0 Task 9)."""
    launched(conn, gads)
    pages.down()
    run_job()
    run_job()
    pages.up()
    real_holds = ads_run._resume_holds
    other = db.connect(TEST_DB_URL)
    other.autocommit = False
    commits: list[threading.Timer] = []

    def holds_then_an_unpublish_opens(*args):
        holds = real_holds(*args)
        other.execute("UPDATE page SET status = 'unpublished' WHERE business_id = %s", (BIZ,))
        assert ads_run.mark_page_down(other, BIZ)
        commits.append(threading.Timer(0.5, other.commit))
        commits[0].start()
        return holds

    monkeypatch.setattr(ads_run, "_resume_holds", holds_then_an_unpublish_opens)
    sent = len(gads.sent)
    started = time.monotonic()
    try:
        out = run_job()
        waited = time.monotonic() - started
    finally:
        for timer in commits:
            timer.join()
        other.close()

    assert waited >= 0.4  # the resume's UPDATE waited for the unpublish to commit, then re-checked the row
    r = row(conn)
    assert (out["resumed"], out["ops"], gads.sent[sent:]) == (0, 0, [])
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"]) == ("paused", "job:page-check", "page_down")
    assert [m["subject"] for m in mailbox] == [jobs.MAIL_PAGE_DOWN]


def test_a_resume_that_ends_failed_is_never_told_as_back_on(conn, gads, live, pages, mailbox):
    """page-check's own pause; the campaign was removed in Google Ads meanwhile, so the enable finds nothing and the
    row ends `failed`. Not counted as resumed, no "back on" e-mail, no page-check audit row (ads-launch's own row says
    what happened)."""
    launched(conn, gads)
    campaign = row(conn)["resources"]["campaign"]
    pages.down()
    run_job()
    run_job()
    del gads.campaigns[campaign]
    pages.up()

    out = run_job()

    assert (out["resumed"], row(conn)["state"]) == (0, "failed") and out["ops"] >= 1
    assert [m["subject"] for m in mailbox] == [jobs.MAIL_PAGE_DOWN]
    assert [a["note"] for a in audited(conn)] == [NOTE_PAUSED]


@pytest.mark.parametrize("switch", ["live", "validate"])
def test_resume_only_if_holds_to_its_condition_on_the_row_it_reads(conn, gads, live, monkeypatch, switch):
    """`ads_run.resume(only_if=...)`: a condition that does not hold on the row read under the lock -- the ads carry
    another address (`carried`), or the page is not published at `page_url` -- leaves it as it is and sends nothing,
    not even `validate`'s check of an ENABLED; one that holds resumes as always. (Re-specified by M0 Task 9: whose
    pause it is is the resume's `reason` now, checked before the condition.)"""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="owner")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", switch)
    sent = len(gads.sent)

    elsewhere = ads_run.ResumeIf(carried="https://brightsmile.example/old?src=gads", page_url=PAGE_URL)
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE, only_if=elsewhere)

    assert (out["state"], out["ops"], gads.sent[sent:]) == ("paused", 0, [])
    assert row(conn)["resources"]["paused_by"] == "owner"
    moved = ads_run.ResumeIf(carried=ADS_URL, page_url="https://brightsmile.example/elsewhere")
    assert ads_run.resume(conn, config.load(), BIZ, GOOGLE, only_if=moved)["ops"] == 0 and gads.sent[sent:] == []
    assert row(conn)["state"] == "paused"
    with pytest.raises(ToolError, match=ads_run.NOT_PAUSED):  # not page-check's pause: never lifted as one
        ads_run.resume(conn, config.load(), BIZ, GOOGLE, reason="unreachable", only_if=moved)
    if switch == "live":
        mine = ads_run.ResumeIf(carried=ADS_URL, page_url=PAGE_URL)
        assert ads_run.resume(conn, config.load(), BIZ, GOOGLE, only_if=mine)["state"] == "live"


def test_admirys_page_down_pause_takes_over_its_own_unreachable_pause_and_nothing_else(conn, gads, live):
    """The page is gone (unpublish's pause landing right after page-check's): Admiry's page-down pause takes over its
    own unreachable one -- page-check never undoes it -- and never the other way round. M0 Task 9 D1: a pause for
    money stands above both -- a page's pause never replaces it, and a money pause replaces a page's; beside the
    owner's pause, Admiry's stands too (both pauses, M0's two-pause model)."""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause(conn, settings, BIZ, GOOGLE, by=jobs.PAGE_CHECK, reason="unreachable")
    sent = len(gads.sent)
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="admiry", reason="page_down")
    assert row(conn)["paused_reason"] == "page_down" and gads.sent[sent:] == []
    ads_run.pause(conn, settings, BIZ, GOOGLE, by=jobs.PAGE_CHECK, reason="unreachable")
    assert row(conn)["paused_reason"] == "page_down"
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="job:spend-guard", reason="month_cap")
    assert row(conn)["paused_reason"] == "month_cap"  # money above the page
    for reason in ads_run.PAGE_REASONS:
        ads_run.pause(conn, settings, BIZ, GOOGLE, by="admiry", reason=reason)
        assert row(conn)["paused_reason"] == "month_cap"  # a page's pause never replaces it

    ads_run.resume(conn, settings, BIZ, GOOGLE, reason="month_cap")
    assert row(conn)["state"] == "live"
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="owner")
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="admiry", reason="page_down")
    r = row(conn)
    assert (r["resources"]["owner_paused"], r["paused_reason"]) == (True, "page_down")


# ---------------------------------------------------------------- what the job leaves alone, the switch, the budget


def test_pages_without_built_ads_are_fetched_but_never_pause_anything(conn, gads, live, pages, mailbox):
    """No ads built (none approved, or approved and not launched yet): the page is still fetched every hour and its
    counters kept -- and nothing is paused, sent or e-mailed."""
    _key, plan_id = seed(conn)
    seed(conn, biz="biz_2", link=None, conversions=False)
    queued(conn, plan_id)  # biz_1: approved, nothing built yet
    pages.down()

    for _ in range(3):
        out = run_job()

    assert (out["checked"], out["down"], out["paused"], out["ops"]) == (2, 2, 0, 0)
    assert counters(conn)["fails"] == 3 and counters(conn, "biz_2")["fails"] == 3
    assert row(conn)["state"] == "queued" and ads_run.get(conn, "biz_2", GOOGLE) is None
    assert gads.sent == [] and mailbox == [] and audited(conn) == []


def test_under_the_off_switch_the_pause_is_owed_and_nothing_goes_to_google(
    conn, gads, live, pages, mailbox, monkeypatch
):
    """The switch `off`: the fetches and the counters happen, the pause is recorded -- owed to Google Ads (ruling
    S11) -- and nothing goes to Google; the owner is still told, in words that say the pause is owed. The page
    opening again cannot turn anything on under `off`: the pause waits for the `live` switch, and is undone then."""
    launched(conn, gads)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    sent = len(gads.sent)
    pages.down()

    run_job()
    out = run_job()

    # every channel whose ads follow the page is named, NewsBreak too since M1 Task 5a (its switch unset: off)
    assert (out["modes"], out["paused"], out["ops"]) == ({GOOGLE: "off", NB: "off"}, 1, 0) and gads.sent[sent:] == []
    assert out["ops_by_channel"] == {GOOGLE: 0, NB: 0}
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"], r["resources"]["pause_owed"]) == (
        "paused",
        "job:page-check",
        "unreachable",
        True,
    )
    owed = f"Google Search: paused by Admiry today: {UNREACHABLE}. {google.PAUSE_OWED}."
    assert ads_run.status_sentence(r) == owed
    assert len(mailbox) == 1 and owed in mailbox[0]["text"]

    pages.up()
    assert run_job()["resumed"] == 0 and gads.sent[sent:] == [] and row(conn)["state"] == "paused"
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")  # validate only has Google check a resume: it waits too
    assert run_job()["resumed"] == 0 and gads.sent[sent:] == [] and row(conn)["state"] == "paused"

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    assert run_job()["resumed"] == 1
    r = row(conn)
    assert r["state"] == "live" and not {"paused_by", "pause_owed"} & set(r["resources"]) and not r["paused_reason"]


def test_a_pause_goes_out_past_the_days_budget_and_a_resume_waits_for_it(conn, gads, live, pages, mailbox, monkeypatch):
    """Ruling S4: page-check's Google operations count in the day's budget (`ops_sent_today`); a pause goes out even
    past it (stopping spend always does, S12), a resume waits for a day with room."""
    launched(conn, gads)
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "5")
    conn.execute("INSERT INTO job_run (job, ok, details) VALUES ('ads-sync', true, %s)", (Jsonb({"ops": 5}),))
    pages.down()
    run_job()
    out = run_job()
    assert (out["paused"], out["ops"], out["ops_by_channel"]) == (1, 1, {GOOGLE: 1, NB: 0})
    assert jobs.ops_sent_today(conn) == 6

    pages.up()
    out = run_job()
    assert (out["resumed"], out["stopped"]) == (0, "ops budget") and row(conn)["state"] == "paused"

    conn.execute("DELETE FROM job_run WHERE job = 'ads-sync'")  # a day with room
    assert run_job()["resumed"] == 1 and row(conn)["state"] == "live"


def test_failures_count_only_45_minutes_apart(conn, gads, live, pages, mailbox):
    """Ruling H33: a failure counts only when the last counted one is at least 45 minutes old -- a manual tick right
    after the timer's, or a catch-up run at boot, is not "twice in a row". An answer always counts, and resets."""
    launched(conn, gads)
    pages.down()
    run_job()
    first = counters(conn)
    assert first["fails"] == 1

    for gap in (timedelta(milliseconds=3), timedelta(minutes=44)):  # 3 ms, then 44 min after the counted failure
        out = run_job(after=gap)
        assert (out["down"], out["paused"]) == (1, 0) and counters(conn) == first
    assert row(conn)["state"] == "live" and mailbox == []

    out = run_job(after=timedelta(minutes=1) - timedelta(milliseconds=3))  # exactly 45 minutes after it
    assert out["paused"] == 1 and counters(conn)["fails"] == 2 and len(mailbox) == 1

    pages.up()
    out = run_job(after=timedelta(milliseconds=3))  # an answer counts at once
    assert out["resumed"] == 1 and counters(conn)["fails"] == 0 and row(conn)["state"] == "live"


def _launched_at(conn, gads, biz: str, cid: str, url: str) -> None:
    """Another business whose ads are live at its own page `url`."""
    _key, plan_id = seed(conn, biz=biz, cid=cid)
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (url, biz))
    ads_run.queue(conn, biz, GOOGLE, plan_id, ready=True)
    assert ads_run.launch(conn, config.load(), biz, GOOGLE)["state"] == "live"


def test_every_page_down_at_once_pauses_nobody(conn, gads, live, pages, mailbox, caplog):
    """Ruling H32: a run in which every page fetched (three or more) failed is Admiry's own network down, not the
    pages: the failures are counted, nothing is paused, nobody is e-mailed, and the run says `all_down`. Once some
    pages answer again, a page still down -- its failures counted all along -- pauses its ads at once."""
    sites = {"biz_a": "https://a.example/", "biz_b": "https://b.example/", "biz_c": "https://c.example/"}
    for n, (biz, url) in enumerate(sites.items()):
        _launched_at(conn, gads, biz, f"{n + 1}" * 10, url)
    sent = len(gads.sent)
    for url in sites.values():
        pages.down(url)

    for _ in range(2):
        out = run_job()
        assert (out["checked"], out["down"], out["paused"], out["all_down"]) == (3, 3, 0, True)
    assert gads.sent[sent:] == [] and mailbox == [] and audited(conn) == []
    assert {biz: counters(conn, biz)["fails"] for biz in sites} == {"biz_a": 2, "biz_b": 2, "biz_c": 2}
    assert "all 3 pages of shard 0/1 did not open" in caplog.text
    assert all(ads_run.get(conn, biz, GOOGLE)["state"] == "live" for biz in sites)

    pages.up("https://a.example/")
    pages.up("https://b.example/")
    out = run_job()
    assert (out["down"], out["paused"], "all_down" in out) == (1, 1, False)
    assert [ads_run.get(conn, biz, GOOGLE)["state"] for biz in sites] == ["live", "live", "paused"]
    assert len(mailbox) == 1


def test_the_job_splits_its_pages_by_shard(conn, gads, live, pages):
    for biz in ("biz_a", "biz_b", "biz_c", "biz_d"):
        seed(conn, biz=biz, link=None, conversions=False)
    shards = [run_job(f"{i}/2") for i in range(2)]
    assert sum(s["checked"] for s in shards) == 4 and all(s["checked"] < 4 for s in shards)


def test_a_page_that_fails_to_check_never_stops_the_run(conn, gads, live, pages, monkeypatch, caplog):
    seed(conn, biz="biz_a", link=None, conversions=False)
    seed(conn, biz="biz_b", link=None, conversions=False)
    real = ads_run.rows

    def broken(conn_, biz):
        if biz == "biz_a":
            raise RuntimeError("boom")
        return real(conn_, biz)

    monkeypatch.setattr(ads_run, "rows", broken)  # what the job reads the business's channel rows with
    out = run_job()
    assert (out["checked"], out["errors"]) == (2, 1)
    assert "page-check: RuntimeError on business biz_a" in caplog.text


def test_expired_previews_are_pruned(conn, pages):
    seed(conn, link=None, conversions=False)
    page_id = landing.current(conn, BIZ)["id"]
    fresh = hosted.save_preview(conn, page_id, "<html>fresh</html>", None)
    stale = hosted.save_preview(conn, page_id, "<html>stale</html>", None)
    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute' WHERE token = %s", (stale,))

    assert run_job()["pruned"] == 1
    assert hosted.preview(conn, fresh) is not None
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 1


# ---------------------------------------------------------------- the counters (H18) and the hosted page


def test_the_counters_sit_beside_the_saved_report_and_survive_a_new_one(conn, gads, live, pages):
    """H18: page-check merges `reachable` into the saved 14-rule report, never over it; a new report keeps it."""
    seed(conn)
    pages.down()
    run_job()
    page = landing.current(conn, BIZ)
    assert page["check_json"]["ok"] is True and page["check_json"]["rules"] == CHECK_OK["rules"]
    counts = page["check_json"]["reachable"]
    assert (counts["fails"], counts["detail"]) == (1, "HTTP 503") and counts["since"] == counts["last"]
    datetime.fromisoformat(counts["last"])

    landing.save_check(conn, page["id"], {**CHECK_OK, "ok": False})
    page = landing.current(conn, BIZ)
    assert page["check_json"]["reachable"] == counts
    assert landing.saved_report(page) == {**CHECK_OK, "ok": False}

    run_job()  # still down: the run of failures keeps its start
    again = counters(conn)
    assert again["fails"] == 2 and again["since"] == counts["since"] and again["last"] != counts["last"]
    pages.up()
    run_job()  # up: a new run starts
    back = counters(conn)
    assert (back["fails"], back["detail"]) == (0, "HTTP 200") and back["since"] == back["last"] != counts["since"]


def test_counters_alone_are_no_check(client, conn, gads, live, pages):
    """A page the tools never checked, fetched by the job: its counters are not a 14-rule report -- a launch still
    asks for the check first, and get_page says it has not been checked (the counters shown beside it)."""
    key, plan_id = seed(conn, check=None)
    pages.down()
    run_job()
    page = landing.current(conn, BIZ)
    assert set(page["check_json"]) == {"reachable"} and landing.saved_report(page) is None
    plan = ads_run.approved(conn, BIZ)
    assert plan["id"] == plan_id
    with pytest.raises(ToolError, match="has not been checked yet"):
        ads_run.precheck(conn, config.load(), BIZ, plan)
    res = call_tool(client, "get_page", {}, key=key)
    assert data(res)["check"] is None and data(res)["page"]["reachable"]["fails"] == 1
    assert res["structuredContent"]["say"].endswith("it has not been checked yet.")


def test_a_hosted_page_that_does_not_open_pauses_its_ads_and_the_owner_has_nothing_to_fix(
    client, conn, gads, live, pages, mailbox, monkeypatch, caplog
):
    """The business's page is Admiry's own hosted one: page-check fetches its address like any other, and the
    e-mail says Admiry is on it (there is nothing for the owner to fix); the operator reads it in the log."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    assert row(conn)["resources"]["final_url"] == URL + "?src=gads"
    mailbox.clear()
    pages.down(URL, 502)

    run_job()
    out = run_job()

    assert out["paused"] == 1 and pages.fetched[-1] == URL
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "unreachable")
    assert len(mailbox) == 1 and "Admiry hosts this page, so there is nothing for you to fix" in mailbox[0]["text"]
    assert "hosted page did not open twice in a row: HTTP 502" in caplog.text


def test_taking_down_a_page_page_check_paused_makes_its_pause_the_page_down_one(
    client, conn, gads, live, pages, mailbox, monkeypatch
):
    """page-check paused the ads for their hosted page; the owner then takes that page down. The pause stays Admiry's
    but is its page-down pause from now on: the status line stops promising the ads come back once the page opens
    (page-check never fetches an unpublished page), and publishing the page again is what ends it (H24)."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    pages.down(URL, 502)
    run_job()
    run_job()
    assert row(conn)["paused_reason"] == "unreachable"

    pid = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))["proposal_id"]
    assert not call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "cu1"}, key=key)["isError"]

    r = row(conn)
    # unpublish's own pause stacks on page-check's, now its page-down one (D5): by Admiry (D8)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"]) == ("paused", "admiry", "page_down")
    assert ads_run.status_sentence(r) == f"Google Search: paused by Admiry today: {PAGE_DOWN}."
    pages.up(URL)
    assert run_job()["checked"] == 0 and row(conn)["state"] == "paused"

    prop = data(call_tool(client, "publish_page", {"idempotency_key": "rb", "version": 1}, key=key))
    _checked(monkeypatch, conn, URL)
    assert not call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c"}, key=key
    )["isError"]
    assert row(conn)["state"] == "live" and row(conn)["paused_reason"] is None


# ---------------------------------------------------------------- H43: running ads on a hosted address with no page


def _hosted_ads(client, conn, gads, monkeypatch) -> str:
    """Ads live at the business's published hosted page (test_tools_hosting's own steps); returns the key."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    assert row(conn)["resources"]["final_url"] == URL + "?src=gads"
    return key


def test_an_unpublish_whose_own_pause_never_happened_is_paused_by_the_next_run(
    client, conn, gads, live, pages, mailbox, monkeypatch
):
    """H43 (the final review's F2): unpublish_page takes the page down inside confirm's transaction and pauses the
    ads only once that has committed -- a step that can fail on its own (its database connection, the process
    dying in between), leaving nothing owed: the ads would keep sending paid clicks to a page that answers "gone",
    for good. page-check is the backstop: running ads whose address is the business's hosted one with no page live
    there get Admiry's page-down pause -- owed to Google Ads under the `off` switch (ruling S11) -- and the owner
    one e-mail; the next run finds them paused and does nothing more. It ends like unpublish's own pause: the owed
    PAUSED goes out once the switch is live, and publishing the page again turns the ads back on (H24)."""
    key = _hosted_ads(client, conn, gads, monkeypatch)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    real_pause = ads_run.pause

    def connection_lost(*args, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(ads_run, "pause", connection_lost)
    pid = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))["proposal_id"]
    res = call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "cu1"}, key=key)
    monkeypatch.setattr(ads_run, "pause", real_pause)
    assert not res["isError"] and "ads" not in data(res)  # the page is down; its pause never happened
    assert row(conn)["state"] == "live" and "pause_owed" not in row(conn)["resources"]
    mailbox.clear()  # the publish's own check report
    sent = len(gads.sent)

    first = run_job()

    assert (first["page_down"], first["ops"]) == (1, 0) and gads.sent[sent:] == []
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"], r["resources"]["pause_owed"]) == (
        "paused",
        "job:page-check",
        "page_down",
        True,
    )
    status = ads_run.status_sentence(r)
    assert status == f"Google Search: paused by Admiry today: {PAGE_DOWN}. {google.PAUSE_OWED}."
    assert len(mailbox) == 1 and mailbox[0]["subject"] == jobs.MAIL_PAGE_GONE
    assert URL in mailbox[0]["text"] and status in mailbox[0]["text"] and plain(mailbox[0]["text"])
    assert plain(jobs.MAIL_PAGE_GONE)
    assert [(a["note"], a["email"], a["to"], a["url"], a["channel"]) for a in audited(conn)] == [
        (NOTE_PAGE_GONE, "sent", "paused", URL, GOOGLE)
    ]
    sentences = [e["sentence"] for e in data(call_tool(client, "get_audit_log", {}, key=key))["entries"]]
    assert f"Admiry {NOTE_PAGE_GONE}" in sentences and plain(NOTE_PAGE_GONE)

    second = run_job()
    assert second["page_down"] == 0 and len(mailbox) == 1 and len(audited(conn)) == 1

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    jobs.ads_launch(conn, 0, 1)  # the hourly ads-launch sends the owed PAUSED status
    assert gads.kinds()[sent:] == ["pause"] and "pause_owed" not in row(conn)["resources"]
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "rb", "version": 1}, key=key))
    _checked(monkeypatch, conn, URL)
    back = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c"}, key=key)
    assert not back["isError"] and data(back)["ads"] == "live"
    assert row(conn)["state"] == "live" and "paused_by" not in row(conn)["resources"] and not row(conn)["paused_reason"]


@pytest.mark.parametrize("switch", ["live", "off"])
def test_a_move_off_the_hosted_page_is_left_to_ads_launch_only_while_the_switch_can_send_it(
    client, conn, gads, live, pages, mailbox, monkeypatch, switch
):
    """H43's one exception: the owner's own page is the live one, and the ads still carry the hosted address only
    because the move has not reached Google Ads yet (`url_stale`: Google did not answer just then). Under the
    `live` switch the hourly ads-launch sends that move, so page-check leaves the ads running rather than pause them
    with nothing left to turn them back on -- the move lands, and they never stop. Under `off` the move cannot be
    sent at all: the ads are paused (owed), as set_landing_page's own page-down pause would have done."""
    _hosted_ads(client, conn, gads, monkeypatch)
    # set_landing_page's own writes: the owner's page in the row's place (its old check cleared, as set_owner_url does
    # -- the hosted page's check is not the owner page's), and the move queued
    conn.execute(
        "UPDATE page SET source = 'owner', url = %s, check_json = NULL, checked_at = NULL WHERE business_id = %s",
        (PAGE_URL, BIZ),
    )
    assert ads_run.flag_moved_page(conn, BIZ, URL, PAGE_URL)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", switch)
    mailbox.clear()

    out = run_job()

    if switch == "off":
        assert out["page_down"] == 1 and len(mailbox) == 1
        assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "page_down")
        return
    assert out["page_down"] == 0 and row(conn)["state"] == "live" and mailbox == []
    jobs.ads_launch(conn, 0, 1)  # the move goes out
    r = row(conn)
    assert r["state"] == "live" and r["resources"]["final_url"] == ADS_URL and "url_stale" not in r["resources"]
    assert run_job()["page_down"] == 0 and row(conn)["state"] == "live" and audited(conn) == []


def test_the_backstop_never_claims_ads_someone_paused_in_google_ads(
    client, conn, gads, live, pages, mailbox, monkeypatch
):
    """Ruling H33 holds for the backstop too: ads someone paused in Google Ads itself (ads-sync's note, the row
    `limited`) at a hosted address with no page are off already -- no pause of Admiry's over theirs, no e-mail."""
    _hosted_ads(client, conn, gads, monkeypatch)
    conn.execute(
        "UPDATE channel_campaign SET state = 'limited', last_error = %s WHERE business_id = %s AND channel = %s",
        (google.PAUSED_IN_GOOGLE, BIZ, GOOGLE),
    )
    conn.execute("UPDATE page SET status = 'unpublished' WHERE business_id = %s", (BIZ,))
    mailbox.clear()

    out = run_job()

    assert out["page_down"] == 0 and mailbox == [] and audited(conn) == []
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("limited", google.PAUSED_IN_GOOGLE) and "paused_by" not in r["resources"]


def test_the_backstop_runs_even_when_every_page_fetched_failed(client, conn, gads, live, pages, mailbox, monkeypatch):
    """H32's all-down guard is about fetches -- Admiry's own network down, not the pages -- and a hosted address
    with no page behind it is no fetch's finding: the backstop pauses those ads whatever the run's fetches said."""
    _hosted_ads(client, conn, gads, monkeypatch)
    conn.execute("UPDATE page SET status = 'unpublished' WHERE business_id = %s", (BIZ,))
    sites = {"biz_a": "https://a.example/", "biz_b": "https://b.example/", "biz_c": "https://c.example/"}
    for n, (biz, url) in enumerate(sites.items()):
        _launched_at(conn, gads, biz, f"{n + 2}" * 10, url)
        pages.down(url)
    mailbox.clear()

    out = run_job()

    assert out["all_down"] is True and (out["paused"], out["page_down"]) == (0, 1)
    assert row(conn)["state"] == "paused" and all(ads_run.get(conn, biz, GOOGLE)["state"] == "live" for biz in sites)
    assert len(mailbox) == 1 and mailbox[0]["subject"] == jobs.MAIL_PAGE_GONE


def test_a_page_that_does_not_answer_in_time_is_counted_down(conn, gads, live, pages, monkeypatch):
    """Ruling H34: the job's fetch has a deadline -- a page that does not answer within it is down ("No answer within
    ... s."), whatever httpx's per-operation timeouts would still allow. (A small deadline keeps this test fast.) An
    owner page's fetch reads its body too (`landing.fetch_body`, A3p-5 batch 2 Q9), under the same deadline."""
    seed(conn, link=None, conversions=False)
    release = threading.Event()

    def stalls(request: httpx.Request) -> httpx.Response:
        release.wait(5)
        return httpx.Response(200)

    monkeypatch.setattr(jobs, "_page_transport", lambda: httpx.MockTransport(stalls))
    monkeypatch.setattr(landing, "fetch_body", functools.partial(landing.fetch_body, budget_s=0.2))
    try:
        out = run_job()
    finally:
        release.set()
    assert (out["checked"], out["down"]) == (1, 1)
    assert counters(conn)["detail"] == "No answer within 0.2 s."


def test_a_hosted_page_found_down_is_logged_even_without_ads(conn, pages, caplog):
    """The operator hears about every hosted page that does not open, not only the ones whose ads it pauses."""
    seed(conn, link=None, page=False, conversions=False)
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, status) VALUES ('pg_h', %s, 'hosted', %s, %s, "
        "'published')",
        (BIZ, URL, HOSTNAME),
    )
    pages.down(URL, 502)
    caplog.set_level(logging.INFO, logger="agent.jobs")

    out = run_job()

    assert (out["checked"], out["down"], out["paused"]) == (1, 1, 0)
    assert "page-check: business biz_1's hosted page did not open: HTTP 502" in caplog.text


# ---------------------------------------------------------------- landing.reachable


def _web(routes: dict[str, httpx.Response | Exception]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = routes.get(str(request.url), httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "routes, expected",
    [
        ({"https://a.example/": httpx.Response(200)}, (True, "HTTP 200")),
        ({"https://a.example/": httpx.Response(204)}, (True, "HTTP 204")),
        ({"https://a.example/": httpx.Response(503)}, (False, "HTTP 503")),
        ({"https://a.example/": httpx.Response(410)}, (False, "HTTP 410")),
        (
            {
                "https://a.example/": httpx.Response(301, headers={"location": "https://www.a.example/home"}),
                "https://www.a.example/home": httpx.Response(200),
            },
            (True, "HTTP 200"),
        ),
        (
            {"https://a.example/": httpx.Response(302, headers={"location": "https://elsewhere.example/"})},
            (False, "The page sends visitors to another site (elsewhere.example)."),
        ),
        (
            {"https://a.example/": httpx.ConnectTimeout("no answer")},
            (False, "Could not open the page: ConnectTimeout."),
        ),
        (
            {
                "https://a.example/": httpx.Response(302, headers={"location": "https://a.example/b"}),
                "https://a.example/b": httpx.Response(302, headers={"location": "https://a.example/c"}),
            },
            (False, "The address redirects more than once (https://a.example/ → https://a.example/c)."),
        ),
    ],
)
def test_reachable_is_a_2xx_answer_at_the_end_of_the_checks_own_fetch(routes, expected):
    assert landing.reachable("https://a.example/", transport=_web(routes), resolver=lambda h: PUBLIC_IP) == expected


def test_reachable_refuses_an_address_it_must_not_fetch_without_fetching_it():
    asked: list[str] = []
    web = httpx.MockTransport(lambda r: (asked.append(str(r.url)), httpx.Response(200))[1])
    private = landing.reachable("https://a.example/", transport=web, resolver=lambda h: ["10.0.0.5"])
    assert private == (False, "the page address must be a public address")
    assert landing.reachable("http://a.example/", transport=web, resolver=lambda h: PUBLIC_IP) == (True, "HTTP 200")
    assert asked == ["http://a.example/"]  # plain http is not "down": the question is whether the page opens


def test_reachable_reads_no_body_so_a_200_whose_body_stalls_is_up():
    """Only the status line and headers are waited for: a 200 whose body never comes is a page that opens, and no
    chunk of any body is read."""
    pulled: list[int] = []

    class Stalls(httpx.SyncByteStream):
        def __iter__(self):
            pulled.append(1)
            raise httpx.ReadTimeout("the body never came")
            yield b""

    web = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stalls()))
    assert landing.reachable("https://a.example/", transport=web, resolver=lambda h: PUBLIC_IP) == (True, "HTTP 200")
    assert pulled == []


@pytest.mark.parametrize("stall", ["the answer", "the name lookup"])
def test_reachable_gives_up_at_its_deadline(stall):
    """Ruling H34: the whole fetch -- the name lookup and the request alike -- gets `budget_s`; past it the page is
    down, and the caller is back at once (the fetch goes on, abandoned, on its own daemon thread)."""
    release = threading.Event()

    def stalls(*_args):
        release.wait(5)
        return PUBLIC_IP if stall == "the name lookup" else httpx.Response(200)

    web = httpx.MockTransport(stalls if stall == "the answer" else lambda request: httpx.Response(200))
    resolver = stalls if stall == "the name lookup" else (lambda h: PUBLIC_IP)
    started = time.monotonic()
    try:
        got = landing.reachable("https://a.example/", transport=web, resolver=resolver, budget_s=0.2)
        took = time.monotonic() - started
    finally:
        release.set()
    assert got == (False, "No answer within 0.2 s.") and took < 1.0


def test_reachable_s_deadline_is_the_fetch_budget_and_its_errors_are_the_callers():
    assert inspect.signature(landing.reachable).parameters["budget_s"].default == landing.FETCH_BUDGET_S == 10.0

    def breaks(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("not an answer")

    with pytest.raises(RuntimeError, match="not an answer"):  # the job's per-page guard counts it as an error
        landing.reachable("https://a.example/", transport=httpx.MockTransport(breaks), resolver=lambda h: PUBLIC_IP)


# ---------------------------------------------------------------- A3p-5 batch 2 Task 4: Admiry's code on the owner's page

SITE = "https://brightsmile.example"  # the seed's website: PAGE_URL is a page of it
HOST = "brightsmile.example"
GONE = f"Admiry's code is no longer on {HOST}. Calls and forms from your site are not being counted."
GONE_SUBJECT = f"Admiry's code is missing from {HOST}"
ON = f"Admiry's code is on {HOST} now. Calls and forms from your site will be counted."  # ruling R4-a
ON_SUBJECT = f"Admiry's code is on {HOST}"
ELSEWHERE = "https://offer.example/smile"  # an owner page on another host than the website


def _rule(id_: str, ok: bool | None, fix: str | None = None) -> dict:
    title = next(c["title"] for c in landing.CHECKLIST if c["id"] == id_)
    return {"id": id_, "title": title, "ok": ok, "detail": "d", "fix": fix}


TRACKING_OK = _rule("tracking", True)
TRACKING_MISSING = _rule("tracking", False, landing.TRACKING_FIX)
CHECK_CODE = {**CHECK_OK, "rules": [*CHECK_OK["rules"], TRACKING_OK]}  # a saved check that found Admiry's code
CHECK_NO_CODE = {"ok": False, "url": PAGE_URL, "rules": [*CHECK_OK["rules"], TRACKING_MISSING]}
CHALLENGE = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head><body>Checking your browser '
    "before accessing the site.</body></html>"
)


class _Stalls(httpx.SyncByteStream):
    """A body that breaks off after the status line and headers came."""

    def __iter__(self):
        raise httpx.ReadTimeout("the body never came")
        yield b""


def page_id(conn, biz: str = BIZ) -> str:
    return landing.current(conn, biz)["id"]


def with_code(pid: str) -> str:
    """A page carrying Admiry's code for page `pid`: the kit's snippet, inside <head>."""
    code = landing.snippet(pid, config.load().public_base_url, None)
    return f"<html><head>{code}</head><body>Bright Smile Dental</body></html>"


def code_seen(conn, biz: str = BIZ) -> bool | None:
    return conn.execute("SELECT code_seen FROM page WHERE business_id = %s", (biz,)).fetchone()["code_seen"]


def misses(conn, biz: str = BIZ) -> datetime | None:
    """The first of the hourly looks in a row that found no code on the page (ruling R4-b), or None."""
    return conn.execute("SELECT code_missed_at FROM page WHERE business_id = %s", (biz,)).fetchone()["code_missed_at"]


def coded(conn, gads) -> tuple[str, str]:
    """A business whose ads are live at its own page, a page of its website whose saved check found Admiry's code.
    Returns (key, the page's id)."""
    key, _v1 = launched(conn, gads)
    pid = page_id(conn)
    landing.save_check(conn, pid, CHECK_CODE)
    return key, pid


def status_say(client, key: str) -> str:
    got = call_tool(client, "get_page", {}, key=key)
    assert not got["isError"]
    return got["structuredContent"]["say"]


def test_an_owner_page_that_loses_admirys_code_is_mailed_once_and_the_status_says_so(
    client, conn, gads, live, pages, mailbox
):
    """Plan Q9 (spec W15): page-check reads an owner page's body -- its one fetch of the page, the check's own -- and
    runs the check's tracking rule on it. The code there is recorded; the code gone -- two hourly looks in a row
    (ruling R4-b) -- e-mails the owner once ("Admiry's code is missing from {host}", Q11's subject, and Q9's words with
    the link to the steps) and the status's `say` says so, while the ads keep running; an hour later, still gone,
    nothing more is sent; the code back is recorded silently (no install pending: Q9), and the status says nothing of
    it any more."""
    key, pid = coded(conn, gads)
    pages.body[PAGE_URL] = with_code(pid)

    first = run_job()
    assert pages.fetched == [PAGE_URL] and (first["checked"], first["down"]) == (1, 0)
    assert code_seen(conn) is True and mailbox == [] and first["code_gone"] == 0
    assert GONE not in status_say(client, key)

    pages.body[PAGE_URL] = PAGE_BODY  # a redesign took the code off
    once = run_job()  # one look without it is no change yet
    assert (once["code_missed"], once["code_gone"]) == (1, 0) and code_seen(conn) is True and mailbox == []
    assert GONE not in status_say(client, key)
    gone = run_job()
    assert code_seen(conn) is False and gone["code_gone"] == 1 and gone["paused"] == 0
    assert len(mailbox) == 1
    web = config.load().web_base_url
    assert mailbox[0]["to"] == ["owner@example.com"] and mailbox[0]["subject"] == GONE_SUBJECT
    assert mailbox[0]["text"].strip() == f"{GONE} Add it again: {web}/?website=install_self."
    assert plain(mailbox[0]["text"]) and plain(GONE_SUBJECT)
    say = status_say(client, key)
    assert say.endswith(f" {GONE}") and plain(say)
    assert row(conn)["state"] == "live"  # a missing code stops the counting, not the visitors: the ads keep running

    again = run_job()  # still gone an hour later: nothing more
    assert again["code_gone"] == 0 and len(mailbox) == 1 and code_seen(conn) is False

    pages.body[PAGE_URL] = with_code(pid)  # the code is back
    back = run_job()
    assert code_seen(conn) is True and back["code_back"] == 1 and len(mailbox) == 1
    assert GONE not in status_say(client, key)


def test_the_row_reads_what_the_hourly_check_saw_on_the_owners_page(conn, gads, live, pages, mailbox):
    """Ruling R0-d: the plan's "Your website" row reads `code_seen` -- what the hourly check last saw on the owner's
    page -- when it has looked since the page's last saved check, else that check's tracking rule: found while the code
    is there, needs_code once the hourly check saw it gone (never "on your site" after that), found again when it came
    back. A new saved check is newer than any look, so the row reads it until the hourly check looks again; a new
    address has had no look at all."""
    _key, pid = coded(conn, gads)
    assert site_install.row(conn, BIZ)["state"] == "found"
    pages.body[PAGE_URL] = PAGE_BODY
    run_job()
    assert site_install.row(conn, BIZ)["state"] == "found"  # one look without it is no change yet (ruling R4-b)
    run_job()
    found = site_install.row(conn, BIZ)
    assert (found["state"], found["say"]) == ("needs_code", site_install.NEEDS_CODE)
    assert site_install.status(conn, BIZ)["code"] == "missing"
    pages.body[PAGE_URL] = with_code(pid)
    run_job()
    assert site_install.row(conn, BIZ)["state"] == "found" and site_install.status(conn, BIZ)["code"] == "found"

    conn.execute("UPDATE page SET code_missed_at = now() WHERE id = %s", (pid,))
    landing.save_check(conn, pid, CHECK_NO_CODE)
    assert (code_seen(conn), misses(conn)) == (None, None) and site_install.row(conn, BIZ)["state"] == "needs_code"
    landing.save_check(conn, pid, CHECK_CODE)
    assert site_install.row(conn, BIZ)["state"] == "found"
    conn.execute("UPDATE page SET code_seen = false, code_missed_at = now() WHERE id = %s", (pid,))
    landing.set_owner_url(conn, BIZ, f"{SITE}/new")
    assert (code_seen(conn), misses(conn)) == (None, None)


def test_a_page_whose_check_found_no_code_waits_for_a_check_to_find_it(conn, gads, live, pages, mailbox):
    """A page whose saved check found no code (named before the code went on): the hourly check finding none sends
    nothing -- there was no code to lose -- and finding it records nothing either: the launch reads the saved check, so
    the row stays needs_code until a check finds the code (Check now, or the watch of a pending install, R3-a)."""
    seed(conn, check=CHECK_NO_CODE, link=None, conversions=False)
    pid = page_id(conn)
    out = run_job()
    assert (code_seen(conn), misses(conn)) == (None, None) and mailbox == [] and out["code_gone"] == 0
    pages.body[PAGE_URL] = with_code(pid)
    out = run_job()
    assert code_seen(conn) is None and mailbox == [] and out["code_back"] == 0
    assert site_install.row(conn, BIZ)["state"] == "needs_code"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda: httpx.Response(403, text=CHALLENGE), id="a 403 bot challenge"),
        pytest.param(lambda: httpx.Response(429, text=CHALLENGE), id="a 429"),
        pytest.param(lambda: httpx.Response(503, text=CHALLENGE), id="a 503"),
        pytest.param(lambda: httpx.Response(200, text='{"challenge": "js"}'), id="a 200 with no html"),
        pytest.param(lambda: httpx.ReadTimeout("no answer"), id="a timeout"),
        pytest.param(lambda: httpx.Response(200, stream=_Stalls()), id="a body that breaks off"),
    ],
)
@pytest.mark.parametrize("seen", [True, None], ids=["seen by the hourly check", "seen by the saved check"])
def test_an_answer_that_is_no_page_to_judge_leaves_code_seen_as_it_was(conn, gads, live, pages, mailbox, answer, seen):
    """Plan Q9, the Review Focus: a page behind a bot challenge (a 403, 429 or 503 status, or a body with no `<html`),
    down, or whose body did not come leaves `code_seen` as it was and sends nothing -- whether the hourly check or only
    the saved check had seen the code there: an unreachable page is the Reachable rule's, never a missing code."""
    _key, pid = coded(conn, gads)
    if seen is not None:
        conn.execute("UPDATE page SET code_seen = %s WHERE id = %s", (seen, pid))
    pages.answer[PAGE_URL] = answer()
    out = run_job()
    assert code_seen(conn) is seen and misses(conn) is None and mailbox == [] and out["code_gone"] == 0
    assert site_install.row(conn, BIZ)["state"] == "found"


def test_the_code_counts_as_gone_after_two_hourly_looks_in_a_row_without_it(conn, gads, live, pages, mailbox):
    """Ruling R4-b: as a page counts as down after two failures (H33), Admiry's code counts as gone after two hourly
    looks in a row without it. The first look is kept (`code_missed_at`) and changes nothing else; a look with the code
    ends the run; a tick minutes after the first is no second hourly look (H33's spacing); an hour with no page to judge
    says nothing either way, as it leaves `code_seen`; the second hourly look without the code makes `code_seen` false
    and e-mails the owner."""
    _key, pid = coded(conn, gads)
    pages.body[PAGE_URL] = PAGE_BODY
    out = run_job()
    assert (out["code_missed"], out["code_gone"]) == (1, 0) and misses(conn) is not None
    assert code_seen(conn) is None and mailbox == []  # the saved check's word stands: found
    pages.body[PAGE_URL] = with_code(pid)
    run_job()  # the code there: the run of looks without it is over
    assert (code_seen(conn), misses(conn)) == (True, None)
    pages.body[PAGE_URL] = PAGE_BODY
    run_job()
    assert code_seen(conn) is True and misses(conn) is not None and mailbox == []
    out = run_job(after=timedelta(minutes=10))  # a tick right after the first: no second hourly look
    assert out["code_gone"] == 0 and code_seen(conn) is True and mailbox == []
    pages.answer[PAGE_URL] = 503  # an hour with no page to judge
    out = run_job()
    assert out["code_gone"] == 0 and misses(conn) is not None and mailbox == []
    del pages.answer[PAGE_URL]
    out = run_job()
    assert out["code_gone"] == 1 and (code_seen(conn), misses(conn)) == (False, None)
    assert [m["subject"] for m in mailbox] == [GONE_SUBJECT]


def test_a_page_whose_code_comes_and_goes_is_told_at_most_once_a_week(conn, gads, live, pages, mailbox):
    """Ruling R4-b: at most one "code missing" e-mail a page in 7 days. Found, missing, found, missing again within the
    week: one e-mail, though `code_seen` turns false each time (the status says so); a week on, missing again is told
    again."""
    _key, pid = coded(conn, gads)

    def found() -> None:
        pages.body[PAGE_URL] = with_code(pid)
        run_job()
        assert code_seen(conn) is True

    def missing_twice(*, after: timedelta = timedelta(hours=1)) -> None:
        pages.body[PAGE_URL] = PAGE_BODY
        run_job(after=after)
        run_job()
        assert code_seen(conn) is False

    found()
    missing_twice()
    assert [m["subject"] for m in mailbox] == [GONE_SUBJECT]
    found()
    missing_twice()
    assert len(mailbox) == 1  # within the week: not told again
    found()
    missing_twice(after=timedelta(days=7))
    assert [m["subject"] for m in mailbox] == [GONE_SUBJECT, GONE_SUBJECT]


def test_a_run_in_which_every_owner_page_lost_the_code_at_once_records_nothing(conn, pages, mailbox, caplog):
    """Ruling F-10 (the final review; H32's guard, for the code): a run in which no owner page judged carries Admiry's
    code, three or more of them pages that had it, is Admiry's own side -- an egress proxy's 200 block page, a 2xx
    interstitial -- not every owner's site at once: no look is kept, no `code_seen` changes and nobody is e-mailed, run
    after run, and the run says so at WARNING. With the code on the other pages again, the one page without it is told
    the usual way: two hourly looks, one e-mail."""
    sites = {"biz_a": "https://a.example/", "biz_b": "https://b.example/", "biz_c": "https://c.example/"}
    pids = {}
    for biz, url in sites.items():
        seed(conn, biz=biz, link=None, conversions=False, check=CHECK_CODE)
        conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (url, biz))
        pids[biz] = page_id(conn, biz)

    for _ in range(2):  # every page answers, none with the code (PAGE_BODY): a block page, as the box sees it
        out = run_job()
        assert (out["checked"], out["down"], out["code_missed"], out["code_gone"]) == (3, 0, 0, 0)
        assert out["code_all_missing"] is True
    assert [(code_seen(conn, biz), misses(conn, biz)) for biz in sites] == [(None, None)] * 3 and mailbox == []
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("all 3 owner pages of shard 0/1 that had Admiry's code came back without it" in m for m in warned)

    pages.body[sites["biz_a"]] = with_code(pids["biz_a"])
    pages.body[sites["biz_b"]] = with_code(pids["biz_b"])
    out = run_job()  # one page without the code among others with it: the usual way
    assert "code_all_missing" not in out and out["code_missed"] == 1 and misses(conn, "biz_c") is not None
    out = run_job()
    assert out["code_gone"] == 1 and code_seen(conn, "biz_c") is False
    assert (code_seen(conn, "biz_a"), code_seen(conn, "biz_b")) == (True, True)
    assert [m["subject"] for m in mailbox] == ["Admiry's code is missing from c.example"]


def test_only_pages_that_had_the_code_count_toward_the_guard(conn, pages, mailbox):
    """Ruling F-10: a page judged without the code where none was ever seen says nothing either way. Two pages that had
    the code and one that never did, all three without it: fewer than three that had it, so the run goes the usual way
    and both looks are kept. A third page that had it, all four without the code: Admiry's own side, nothing kept."""
    for biz, check in (("biz_a", CHECK_CODE), ("biz_b", CHECK_CODE), ("biz_c", CHECK_NO_CODE)):
        seed(conn, biz=biz, link=None, conversions=False, check=check)
        conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (f"https://{biz[-1]}.example/", biz))
    out = run_job()
    assert "code_all_missing" not in out and out["code_missed"] == 2 and misses(conn, "biz_c") is None
    seed(conn, biz="biz_d", link=None, conversions=False, check=CHECK_CODE)
    conn.execute("UPDATE page SET url = 'https://d.example/' WHERE business_id = 'biz_d'")
    out = run_job()
    assert (out["code_missed"], out["code_gone"], misses(conn, "biz_d"), code_seen(conn, "biz_a")) == (0, 0, None, None)
    assert out["code_all_missing"] is True and mailbox == []


def test_reading_an_owner_pages_body_never_changes_whether_it_opens(conn, gads, live, pages, mailbox):
    """The body read is the check's own fetch with its body (Q9), but up or down stays the status line's, as the
    status-only fetch had it (`landing.reachable`): a 200 whose body breaks off is a page that opens -- counted up,
    its code not judged."""
    coded(conn, gads)
    pages.answer[PAGE_URL] = httpx.Response(200, stream=_Stalls())
    out = run_job()
    assert (out["checked"], out["down"]) == (1, 0) and counters(conn)["detail"] == "HTTP 200"
    assert code_seen(conn) is None and mailbox == []


def test_hosted_pages_are_never_read_for_the_code(conn, pages, mailbox):
    """Plan Q9: only an owner page's body is read. A page Admiry hosts is fetched as before -- its status line alone,
    no byte of its body -- and nothing about the code is recorded or sent."""
    seed(conn, link=None, page=False, conversions=False)
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, status, check_json) VALUES ('pg_h', %s, 'hosted', "
        "%s, %s, 'published', %s)",
        (BIZ, URL, HOSTNAME, Jsonb(CHECK_CODE)),
    )
    pulled: list[int] = []

    class Counted(httpx.SyncByteStream):
        def __iter__(self):
            pulled.append(1)
            yield PAGE_BODY.encode()

    pages.answer[URL] = httpx.Response(200, stream=Counted())
    out = run_job()
    assert (out["checked"], out["down"]) == (1, 0) and pulled == []
    assert code_seen(conn) is None and mailbox == [] and out["code_gone"] == 0


def test_the_tracking_rule_is_the_checks_own():
    """landing.tracking_found: the check's tracking rule on its own -- Admiry's script with this page's token."""
    assert landing.tracking_found(with_code("pg_1"), "pg_1")
    assert not landing.tracking_found(with_code("pg_2"), "pg_1")
    assert not landing.tracking_found('<html><div data-page="pg_1"></div></html>', "pg_1")  # the token, no script


def test_fetch_body_is_reachables_answer_and_the_page_when_there_is_one_to_judge():
    """landing.fetch_body (plan Q9): the check's own fetch with its body read, at the check's limits -- (up, detail) as
    `reachable` has them, and the page as text only when a page came back to judge: a 2xx whose body came whole, with
    `<html` in it (any case); never a bot challenge, an answer that is no page, or a body that broke off."""
    page = '<HTML><body data-page="pg_1">Bright Smile</body></HTML>'

    def body(answer: httpx.Response | Exception):
        web = _web({"https://a.example/": answer})
        return landing.fetch_body("https://a.example/", transport=web, resolver=lambda h: PUBLIC_IP)

    assert body(httpx.Response(200, text=page)) == (True, "HTTP 200", page)
    assert body(httpx.Response(200, text='{"ok": true}')) == (True, "HTTP 200", None)
    assert body(httpx.Response(403, text=CHALLENGE)) == (False, "HTTP 403", None)
    assert body(httpx.Response(200, stream=_Stalls())) == (True, "HTTP 200", None)
    assert body(httpx.ConnectTimeout("no")) == (False, "Could not open the page: ConnectTimeout.", None)
    assert landing.fetch_body("https://a.example/", resolver=lambda h: ["10.0.0.5"]) == (
        False,
        "the page address must be a public address",
        None,
    )
    redirected = _web(
        {
            "https://a.example/": httpx.Response(301, headers={"location": "https://www.a.example/home"}),
            "https://www.a.example/home": httpx.Response(200, text=page),
        }
    )
    assert landing.fetch_body("https://a.example/", transport=redirected, resolver=lambda h: PUBLIC_IP) == (
        True,
        "HTTP 200",
        page,
    )


def test_fetch_body_never_judges_a_body_cut_off_at_the_deadline():
    """A body still coming at the check's deadline (FETCH_BUDGET_S, checked between chunks) is cut there: what came
    may not be the whole page, so there is no page to judge -- the page opened, though."""

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b"<html><head>"
            yield b"<script></script></head></html>"

    ticks = iter([0.0] + [100.0] * 20)  # the fetch starts at 0; every later look at the clock is past its deadline
    web = httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow()))
    got = landing.fetch_body(
        "https://a.example/", transport=web, resolver=lambda h: PUBLIC_IP, clock=lambda: next(ticks)
    )
    assert got == (True, "HTTP 200", None)


# ---------------------------------------------------------------- ruling R3-a: a pending install is watched


def watching(conn, way: str = "send", *, days_ago: int = 0, biz: str = BIZ) -> None:
    """An install_request the owner made `days_ago` days ago, as site_install's two ways write them (Task 3)."""
    conn.execute(
        "INSERT INTO install_request (id, business_id, way, created_at) VALUES (%s, %s, %s, "
        "now() - make_interval(days => %s))",
        (db.new_id("ir"), biz, way, days_ago),
    )


def check_as(monkeypatch, *, found: bool = True, ok: bool | None = None, platform: str | None = None) -> list[str]:
    """The check the commands run, answered without the network: its tracking rule found the code or did not; the
    report passes when it did, unless `ok` says otherwise (the phone rule fails then); its fetch read `platform`.
    Returns the addresses checked."""
    checked: list[str] = []
    passes = found if ok is None else ok

    def check(url, page, kit, **kw):
        checked.append(url)
        rules = [_rule("reachable", True), _rule("terms", True), _rule("contact", True)]
        rules.append(TRACKING_OK if found else TRACKING_MISSING)
        if not passes and found:
            rules.append(_rule("phone", False, "Put the business phone at the top as a tap-to-call link."))
        fetched = {"status": 200, "bytes": 1, "seconds": 0.1, "final_url": url, "platform": platform}
        return {"ok": passes, "url": url, "rules": rules, "fetched": fetched}

    monkeypatch.setattr(landing, "check", check)
    return checked


def walked(client, conn, fake) -> tuple[str, str]:  # noqa: F811 - the walk's own fixture, passed on
    """The dental walk's business (test_landing's `_plan`: a consulted plan, the website brightsmile.example read),
    whose owner signed in with Google. Returns (key, business id)."""
    key, biz, _plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    return key, biz


def test_a_pending_install_is_watched_until_its_code_shows_up_and_the_checks_report_tells_the_owner(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling R3-a: the owner sent the steps to their web person ("We'll tell you when the code shows up"). Every hour
    the page the ads use is read -- once: page-check's own read of it -- and the tracking rule run on it; an hour
    without the code saves nothing and sends nothing. The first hour with it records it the way Check now does: the
    page on file is checked again (check_landing_page), its report saved and -- whether it passes changed -- e-mailed:
    that e-mail is the "we'll tell you". The row is found, so the watch is over."""
    key, biz = walked(client, conn, fake)
    pid = landing.set_owner_url(conn, biz, PAGE_URL)["id"]
    landing.save_check(conn, pid, CHECK_NO_CODE)
    watching(conn, "send", biz=biz)
    checked = check_as(monkeypatch)

    out = run_job()
    assert (out["watched"], out["watch_found"]) == (1, 0) and checked == [] and mailbox == []
    assert landing.saved_report(landing.current(conn, biz)) == CHECK_NO_CODE  # nothing new saved
    assert pages.fetched == [PAGE_URL]  # one fetch: page-check's own read of the page

    pages.body[PAGE_URL] = with_code(pid)
    out = run_job()
    assert (out["watched"], out["watch_found"]) == (1, 1) and checked == [PAGE_URL]
    assert pages.fetched == [PAGE_URL, PAGE_URL]  # still one read an hour (the check here answers without one)
    assert len(mailbox) == 1 and mailbox[0]["subject"] == "Your landing page: 4 of 4 checks pass"
    assert site_install.row(conn, biz)["state"] == "found"
    rows = conn.execute("SELECT actor, result FROM agent_action WHERE tool = 'check_landing_page'").fetchall()
    # the report went out (its result changed), so no second e-mail of the watch's own (ruling R4-a)
    assert [(r["actor"], r["result"]["url"], r["result"]["email"], r["result"]["code_on"]) for r in rows] == [
        ("job:page-check", PAGE_URL, "sent", None)
    ]
    sentences = [e["sentence"] for e in data(call_tool(client, "get_audit_log", {}, key=key))["entries"]]
    assert "Admiry checked the landing page" in sentences

    out = run_job()  # the row is found: the watch is over
    assert out["watched"] == 0 and len(mailbox) == 1 and checked == [PAGE_URL]


def test_a_pending_install_with_no_page_yet_names_the_website_once_its_code_shows_up(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling R3-a, the website: Admiry's team was asked (`help`) and the ads have no page yet -- only the kit's
    placeholder, whose id the code carries. The watch reads the owner's website every hour (a fetch of its own:
    page-check reads published pages); the first hour with the code names it the ads' page the way Check now does
    (set_landing_page: its check runs, and its one report is e-mailed), and the watch is over."""
    _key, biz = walked(client, conn, fake)
    pid = landing.ensure_page_for_kit(conn, biz)["id"]
    watching(conn, "help", biz=biz)
    checked = check_as(monkeypatch)

    out = run_job()
    assert out["watched"] == 1 and pages.fetched == [SITE] and checked == [] and mailbox == []
    assert landing.current(conn, biz)["status"] == "preview"

    pages.body[SITE] = with_code(pid)
    out = run_job()
    assert out["watch_found"] == 1 and checked == [SITE]
    page = landing.current(conn, biz)
    assert (page["id"], page["url"], page["status"], page["source"]) == (pid, SITE, "published", "owner")
    assert len(mailbox) == 1 and mailbox[0]["subject"] == "Your landing page: 4 of 4 checks pass"
    assert site_install.row(conn, biz)["state"] == "found"
    assert jobs._watched(conn, 0, 1) == []  # ruling F-1: set_landing_page's steps found the code, the ask fulfilled
    rows = conn.execute("SELECT actor, result FROM agent_action WHERE tool = 'set_landing_page'").fetchall()
    assert [(r["actor"], r["result"]["url"]) for r in rows] == [("job:page-check", SITE)]


def test_the_watch_lasts_14_days(client, conn, fake, pages, mailbox, monkeypatch):  # noqa: F811 - the walk's fixture
    """Ruling R3-a: a way asked for more than 14 days ago is watched no more -- nothing is fetched for it."""
    _key, biz = walked(client, conn, fake)
    pid = landing.ensure_page_for_kit(conn, biz)["id"]
    watching(conn, "send", days_ago=15, biz=biz)
    pages.body[SITE] = with_code(pid)
    out = run_job()
    assert out["watched"] == 0 and pages.fetched == [] and mailbox == []
    watching(conn, "send", days_ago=13, biz=biz)
    check_as(monkeypatch)
    assert run_job()["watch_found"] == 1


def test_a_pending_install_whose_code_shows_up_while_another_rule_fails_is_told_by_the_watch(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling R4-a, case (a): the code shows up on a pending install's page while another rule still fails, so the
    check the watch records has an unchanged result (it failed, it fails) and its report stays unsent: the owner gets
    the watch's own e-mail, once -- "Admiry's code is on {host}" -- and the row is found."""
    _key, biz = walked(client, conn, fake)
    pid = landing.set_owner_url(conn, biz, PAGE_URL)["id"]
    landing.save_check(conn, pid, CHECK_NO_CODE)
    watching(conn, "send", biz=biz)
    check_as(monkeypatch, found=True, ok=False)  # the code there, the phone still not
    pages.body[PAGE_URL] = with_code(pid)

    out = run_job()
    assert out["watch_found"] == 1
    assert [(m["subject"], m["text"].strip()) for m in mailbox] == [(ON_SUBJECT, ON)]
    assert plain(ON) and plain(ON_SUBJECT) and site_install.row(conn, biz)["state"] == "found"
    rows = conn.execute("SELECT result FROM agent_action WHERE tool = 'check_landing_page'").fetchall()
    assert [(r["result"]["email"], r["result"]["code_on"]) for r in rows] == [("unchanged", "sent")]
    run_job()
    assert len(mailbox) == 1


def test_a_pending_install_whose_code_comes_back_to_a_checked_page_is_told_by_the_watch(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling R4-a, case (b): the code went from a page whose check had passed (two looks: the owner told), the owner
    then sent the steps to their web person, and the code comes back. page-check leaves it to the watch rather than
    record it silently; the watch checks the page again, whose result is unchanged (it passed, it passes), so its
    report stays unsent -- and the owner gets the watch's own e-mail, once. Outside a watch it would stay silent (Q9)."""
    _key, biz = walked(client, conn, fake)
    pid = landing.set_owner_url(conn, biz, PAGE_URL)["id"]
    landing.save_check(conn, pid, CHECK_CODE)
    pages.body[PAGE_URL] = PAGE_BODY
    run_job()
    run_job()
    assert code_seen(conn, biz) is False and [m["subject"] for m in mailbox] == [GONE_SUBJECT]
    watching(conn, "send", biz=biz)
    check_as(monkeypatch)
    pages.body[PAGE_URL] = with_code(pid)

    out = run_job()
    assert (out["code_back"], out["watch_found"]) == (0, 1)
    assert [m["subject"] for m in mailbox] == [GONE_SUBJECT, ON_SUBJECT]
    assert site_install.row(conn, biz)["state"] == "found"
    run_job()
    assert len(mailbox) == 2 and code_seen(conn, biz) is True


def test_the_watch_records_nothing_when_the_owner_names_another_page_during_its_fetch(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling R4-d: the watch reads what Check now would check again after its fetch. The owner named another page for
    the ads meanwhile (set_landing_page): their page stands -- never replaced by the website -- and nothing is checked,
    recorded or sent."""
    _key, biz = walked(client, conn, fake)
    pid = landing.ensure_page_for_kit(conn, biz)["id"]
    watching(conn, "help", biz=biz)
    checked = check_as(monkeypatch)
    pages.body[SITE] = with_code(pid)
    real = landing.fetch_body

    def fetched_then_the_owner_names_another_page(url, **kw):
        got = real(url, **kw)
        landing.set_owner_url(conn, biz, ELSEWHERE)
        return got

    monkeypatch.setattr(landing, "fetch_body", fetched_then_the_owner_names_another_page)
    out = run_job()
    assert (out["watched"], out["watch_found"]) == (1, 0) and checked == [] and mailbox == []
    assert landing.current(conn, biz)["url"] == ELSEWHERE
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE actor = 'job:page-check'").fetchone()["n"] == 0


def test_the_watch_records_nothing_when_the_owners_check_now_records_the_pass_first(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling R4-d: the owner pressed Check now while the watch fetched the website -- their check named it the ads'
    page and its report went out. The watch, reading what Check now would check again, finds it changed and records
    nothing: no second report, no e-mail of its own."""
    _key, biz = walked(client, conn, fake)
    pid = landing.ensure_page_for_kit(conn, biz)["id"]
    watching(conn, "send", biz=biz)
    checked = check_as(monkeypatch)
    pages.body[SITE] = with_code(pid)
    real = landing.fetch_body

    def fetched_then_check_now(url, **kw):
        got = real(url, **kw)
        tools_pages.name_page(conn, biz, SITE)  # Check now with no page on file: set_landing_page's own steps
        return got

    monkeypatch.setattr(landing, "fetch_body", fetched_then_check_now)
    out = run_job()
    assert out["watch_found"] == 0 and checked == [SITE]  # one check: the owner's
    assert [m["subject"] for m in mailbox] == ["Your landing page: 4 of 4 checks pass"]  # its report, once
    assert site_install.row(conn, biz)["state"] == "found"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE actor = 'job:page-check'").fetchone()["n"] == 0


def test_the_watch_tells_the_code_is_on_once_a_request_and_ends_at_its_first_pass(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling F-1 (the final review): once the watch has recorded a pass for an install request it ends for good -- the
    request is fulfilled (`install_request.found_at`). The code gone, back (the watch's own e-mail, "Admiry's code is
    on {host}", R4-a), gone again and back within the week: one such e-mail, and the page is watched no more -- the
    second return is Q9's, recorded silently."""
    _key, biz = walked(client, conn, fake)
    pid = landing.set_owner_url(conn, biz, PAGE_URL)["id"]
    landing.save_check(conn, pid, CHECK_CODE)
    check_as(monkeypatch)

    def gone() -> dict:
        pages.body[PAGE_URL] = PAGE_BODY
        run_job()
        out = run_job()  # the second hourly look without it: gone
        assert code_seen(conn, biz) is False
        return out

    def back() -> dict:
        pages.body[PAGE_URL] = with_code(pid)
        return run_job()

    gone()
    watching(conn, "send", biz=biz)
    assert back()["watch_found"] == 1
    assert [m["subject"] for m in mailbox] == [GONE_SUBJECT, ON_SUBJECT]
    assert gone()["watched"] == 0  # the request is fulfilled: the page that lost the code again is not watched
    again = back()
    assert (again["watched"], again["watch_found"], again["code_back"]) == (0, 0, 1)
    assert [m["subject"] for m in mailbox] == [GONE_SUBJECT, ON_SUBJECT]  # within the week: neither e-mail again


def test_a_request_the_owners_own_check_now_fulfilled_is_watched_no_more(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling F-1: the owner's own Check now finding the code while a request is open fulfils it too (check_landing_page
    on the page on file, as the homepage's Check now runs it): the code then gone and back is the code-gone e-mail's
    and Q9's silent return, never the watch's."""
    key, biz = walked(client, conn, fake)
    pid = landing.set_owner_url(conn, biz, PAGE_URL)["id"]
    landing.save_check(conn, pid, CHECK_NO_CODE)
    watching(conn, "send", biz=biz)
    check_as(monkeypatch)
    pages.body[PAGE_URL] = with_code(pid)  # the web person added the code, and the owner pressed Check now
    got = call_tool(client, "check_landing_page", {}, key=key)
    assert not got["isError"], got
    run_job()
    pages.body[PAGE_URL] = PAGE_BODY
    run_job()
    gone = run_job()
    assert gone["watched"] == 0 and code_seen(conn, biz) is False
    pages.body[PAGE_URL] = with_code(pid)
    back = run_job()
    assert (back["watched"], back["watch_found"], back["code_back"]) == (0, 0, 1)
    assert [m["subject"] for m in mailbox] == ["Your landing page: 4 of 4 checks pass", GONE_SUBJECT]


def test_the_watch_reads_a_website_on_file_without_a_scheme_as_https(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    pages,
    mailbox,
    monkeypatch,
):
    """Ruling F-3 (the final review): the website on file is text a model or an assistant wrote, and may come without
    a scheme ("brightsmile.example"); the watch reads it as https:// before it fetches it, every hour, and names that
    address the ads' page once the code shows up."""
    _key, biz = walked(client, conn, fake)
    latest = business.latest_knowledge(conn, biz)[1]
    business.add_knowledge(conn, biz, {**latest, "website": HOST}, source="owner")
    pid = landing.ensure_page_for_kit(conn, biz)["id"]
    watching(conn, "help", biz=biz)
    checked = check_as(monkeypatch)

    out = run_job()
    assert out["watched"] == 1 and pages.fetched == [SITE]
    pages.body[SITE] = with_code(pid)
    out = run_job()
    assert out["watch_found"] == 1 and checked == [SITE] and landing.current(conn, biz)["url"] == SITE


def test_page_check_keeps_only_whether_the_code_was_there(conn, gads, live, pages, mailbox, monkeypatch):
    """Ruling R4-e: page-check keeps, per owner page, only what the watch needs -- the address and whether the code
    with its token was there (None: no page to judge) -- never the page it read."""
    _key, pid = coded(conn, gads)
    pages.body[PAGE_URL] = with_code(pid)
    kept: list[dict] = []
    monkeypatch.setattr(jobs, "_watch_installs", lambda conn_, watched, looks, out: kept.append(dict(looks)))
    run_job()
    pages.answer[PAGE_URL] = 503
    run_job()
    assert kept == [{pid: (PAGE_URL, True)}, {pid: (PAGE_URL, None)}]


# ---------------------------------------------------------------- ruling R1-c: a check's fetch records the platform


def test_a_checks_fetch_reads_the_platform_the_page_is_built_on(client, conn, fake):  # noqa: F811 - walk fixture
    """Ruling R1-c (W9: the platform is read from the HTML Admiry already fetched -- the reading's crawl, the check's
    fetch): the check's report says what its fetch read (`fetched.platform`, site_platform.detect on the page and its
    headers); nothing when no page came back."""
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, f"{SITE}/x")
    wix = (Path(__file__).parent / "fixtures" / "sites" / "wix.html").read_text(encoding="utf-8")
    web = _serve({page["url"]: (200, wix)})
    served = landing.check(page["url"], page, kit, conn=conn, transport=web, resolver=lambda h: PUBLIC_IP)
    assert served["fetched"]["platform"] == "wix"
    missing = landing.check(page["url"], page, kit, conn=conn, transport=_serve({}), resolver=lambda h: PUBLIC_IP)
    assert missing["fetched"]["platform"] is None  # a 404: no page came back


def test_a_saved_check_of_the_owners_own_site_records_its_platform_once(
    client,
    conn,
    fake,  # noqa: F811 - test_landing's walk fixture, by name
    monkeypatch,
):
    """Ruling R1-c (Q13: a site never read has no platform until a check fetches it): a check saved for a page on the
    owner's own site -- set_landing_page's, check_landing_page's of the page on file -- records the platform its fetch
    read, once: only while none is stored (ruling R4-c: the crawl's `other` stays), never `other` itself, never for a
    page elsewhere, never from a dry run. The row's steps are then that platform's -- for an address Admiry could not
    read too."""
    key, biz, _plan_id = _plan(client, conn, fake)
    monkeypatch.setattr(crawl, "resolve", lambda h: PUBLIC_IP)
    latest = business.latest_knowledge(conn, biz)[1]
    unread = {k: v for k, v in latest.items() if k not in ("website", "website_platform")}
    business.add_knowledge(conn, biz, {**unread, "website_unread": SITE}, source="owner")

    def platform() -> str | None:
        return business.latest_knowledge(conn, biz)[1].get("website_platform")

    def call(tool: str, args: dict) -> None:
        got = call_tool(client, tool, args, key=key)
        assert not got["isError"], got

    check_as(monkeypatch, found=False, platform="other")  # a page whose platform could not be told: nothing
    call("set_landing_page", {"url": f"{SITE}/x", "idempotency_key": "s1"})
    assert platform() is None
    check_as(monkeypatch, found=False, platform="shopify")  # a dry run saves nothing
    call("check_landing_page", {"url": f"{SITE}/y"})
    assert platform() is None
    check_as(monkeypatch, found=False, platform="wix")
    call("check_landing_page", {})
    assert platform() == "wix"
    version, _k = business.latest_knowledge(conn, biz)
    source = conn.execute("SELECT source FROM knowledge WHERE business_id = %s AND version = %s", (biz, version))
    assert source.fetchone()["source"] == "check"
    found = site_install.row(conn, biz)
    assert (found["platform"], found["steps"]) == ("wix", site_install.STEPS["wix"]["steps"])
    check_as(monkeypatch, found=False, platform="squarespace")  # once: a platform on file stays
    call("set_landing_page", {"url": f"{SITE}/z", "idempotency_key": "s2"})
    assert platform() == "wix"
    # a page elsewhere is not the owner's site: its builder is not the site's
    business.add_knowledge(conn, biz, {**unread, "website_unread": SITE, "website_platform": "other"}, source="owner")
    check_as(monkeypatch, found=False, platform="shopify")
    call("set_landing_page", {"url": ELSEWHERE, "idempotency_key": "s3"})
    assert platform() == "other"
    # the crawl's `other` (no signature, or two) is never replaced by a reading of another page (ruling R4-c)
    call("set_landing_page", {"url": f"{SITE}/x", "idempotency_key": "s4"})
    assert platform() == "other"


def test_recording_the_platform_never_writes_over_a_version_it_did_not_read(conn, monkeypatch):
    """Ruling R1-c's write is the knowledge read plus the platform: an owner's change landing between the read and the
    write wins -- nothing is recorded, and the owner's change stands (the next saved check records the platform)."""
    business_with_key(conn)
    business.add_knowledge(conn, BIZ, {"name": "Bright Smile Dental", "website_unread": SITE}, source="owner")
    real = business.latest_knowledge

    def read_then_the_owner_renames(conn_, biz):
        got = real(conn_, biz)
        with db.connect(TEST_DB_URL) as other:
            business.add_knowledge(other, biz, {**got[1], "name": "Brighter Smile Dental"}, source="owner")
        return got

    monkeypatch.setattr(business, "latest_knowledge", read_then_the_owner_renames)
    report = {"ok": False, "url": f"{SITE}/x", "rules": [], "fetched": {"platform": "wix"}}
    assert site_install.record_platform(conn, BIZ, report) is None
    monkeypatch.setattr(business, "latest_knowledge", real)
    latest = business.latest_knowledge(conn, BIZ)[1]
    assert latest["name"] == "Brighter Smile Dental" and "website_platform" not in latest
    assert site_install.record_platform(conn, BIZ, report) == "wix"
    assert business.latest_knowledge(conn, BIZ)[1]["name"] == "Brighter Smile Dental"
