"""The scheduled jobs over every channel (plan 2026-09-29-agent-m0-channel-framework Task 7; spec
2026-09-29-admiry-channels-design §3.3, §9): ads-launch and ads-sync step the rows of every channel whose switch is
not `off`, each channel on its own adapter's client, and count each channel's operations (`details.ops_by_channel`)
-- Google Ads' daily operations budget counts and stops Google Search alone; wallet-settle posts each closed day's
spend of the channels Admiry runs from the business's balance, once; every job has its prod timer."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent import ads_run, config, db, jobs, landing, tick, tools_account, wallet
from agent.channels import registry
from agent.channels.newsbreak import build as nb_build
from agent.channels.newsbreak import client as newsbreak
from agent.channels.newsbreak import creative
from agent.tests.conftest import TEST_DB_URL
from agent.tests.fixtures import FakeAdapter, creative_set
from agent.tests.test_ads_run import (  # noqa: F401 - clock, gads and live are fixtures
    BIZ,
    DESCRIPTIONS,
    GOOGLE,
    HEADLINES,
    PAGE_URL,
    _row_for,
    clock,
    gads,
    launched,
    live,
    plain,
    queued,
    row,
)
from agent.tests.test_ads_run_channels import (  # noqa: F401 - nb_live is a fixture
    NB,
    NO_FUNDS,
    OTHER_CID,
    add_plan_by_hand,
    funds,
    nb_live,
    nb_row,
    newsbreak_only,
    seed_plan,
    two_channels,
)
from agent.tests.test_page_check import pages, run_job  # noqa: F401 - pages is a fixture

REPO = Path(__file__).resolve().parents[2]
SYSTEMD = REPO / "deploy/linode-prod/systemd"
BING = "microsoft_search"


class RunFromBalance(FakeAdapter):
    """A second channel Admiry runs from the balance, beside NewsBreak (as Bing Search might one day): Google Search's
    own adapter runs only in the owner's account before A3c, so wallet-settle never takes its days (final review I4)."""

    def capabilities(self):
        return super().capabilities().model_copy(update={"modes": {"managed"}})


@pytest.fixture
def bing_from_balance(monkeypatch):
    monkeypatch.setitem(registry.CHANNELS, BING, RunFromBalance(BING))


def _business(conn, biz: str, **modes: str) -> None:
    """A business and its account on each channel named, in the mode given (`newsbreak="managed"`)."""
    conn.execute("INSERT INTO business (id) VALUES (%s) ON CONFLICT DO NOTHING", (biz,))
    for channel, mode in modes.items():
        registry.save_account(conn, biz, channel, mode=mode, status="active", external={})


def _campaign(
    conn, biz: str, channel: str, state: str, *, plan_id: str = "pl_x", resources: dict | None = None
) -> None:
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) VALUES (%s, %s, %s, %s, %s)",
        (biz, channel, plan_id, state, Jsonb(resources or {})),
    )


def _day(conn, biz: str, channel: str, day: date, cost_micros: int, *, synced_at: datetime | None = None) -> None:
    """A day of the channel's numbers as ads-sync writes them, last read at `synced_at` -- by default at 18:00 UTC the
    next day, past the day's close and `jobs.SETTLE_LAG` in every US zone."""
    read = synced_at or datetime.combine(day + timedelta(days=1), time(18), tzinfo=UTC)
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source, synced_at) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'api', %s)",
        (biz, channel, day, cost_micros, read),
    )


def _spent(conn) -> dict[tuple[str, str], int]:
    """Every spend entry: (business, ref) -> cents."""
    found = conn.execute("SELECT business_id, ref, amount_cents FROM wallet_entry WHERE kind = 'spend'").fetchall()
    return {(r["business_id"], r["ref"]): r["amount_cents"] for r in found}


# ---------------------------------------------------------------- ads-launch and ads-sync over every channel


def test_ads_launch_launches_both_channels_each_on_its_own_client_and_meter(
    conn,
    gads,  # noqa: F811 - the fixture of the same name
    live,  # noqa: F811 - the fixture of the same name
    nb_live,  # noqa: F811 - the fixture of the same name
    monkeypatch,
    tmp_path,
):
    """Both switches on: the job steps both channels, each on its own adapter's client, and counts each one's
    operations. Google Search launches; a NewsBreak row waiting for funds stays `waiting_account` with its reason, and
    one ready to go is built and turned on at NewsBreak (M1 Task 5a; the M0 stub left it `queued`) -- neither is an
    error of the job."""
    monkeypatch.setattr(creative, "ROOT", creative_set(tmp_path / "creative"))
    _key, plan_id = seed_plan(conn, two_channels())
    queued(conn, plan_id)
    ads_run.queue(conn, BIZ, NB, plan_id, ready=False, reason=NO_FUNDS)
    secs = newsbreak_only()
    secs["ads"]["channels"][0].update(headlines=list(HEADLINES), descriptions=list(DESCRIPTIONS))
    secs["creative"] = {**secs["creative"], "approved_image": {NB: "dentists/1.jpg"}}
    _key, other_plan = seed_plan(conn, secs, biz="biz_2", cid=OTHER_CID)
    funds(conn, biz="biz_2")
    ads_run.queue(conn, "biz_2", NB, other_plan, ready=True)
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert out["modes"] == {GOOGLE: "live", NB: "live"}
    assert (out["checked"], out["errors"]) == (3, 0)
    assert out["states"] == {"live": 2, "waiting_account": 1}
    assert out["ops_by_channel"] == {GOOGLE: out["ops"] - len(nb_live.requests), NB: len(nb_live.requests)}
    assert (
        len(nb_live.requests) == 14
    )  # the account, its event, the image, the three objects, two ads, the cap, four ONs
    assert gads.kinds() == ["lookup", "create", "enable"] and row(conn)["state"] == "live"
    waiting = nb_row(conn)
    assert (waiting["state"], waiting["last_error"], waiting["resources"]) == ("waiting_account", NO_FUNDS, {})
    assert nb_row(conn, "biz_2")["state"] == "live" and len(nb_live.campaigns) == 1


def test_ads_sync_reads_newsbreak_beside_google_search_each_on_its_own_client_and_meter(
    conn,
    gads,  # noqa: F811 - the fixture of the same name
    live,  # noqa: F811 - the fixture of the same name
    nb_live,  # noqa: F811 - the fixture of the same name
    monkeypatch,
    tmp_path,
):
    """M1 Task 5b (NewsBreak's reads; M0 left them `Unsupported`): a NewsBreak row the adapter built is read like
    Google Search's -- its HOURLY report into `metric_daily`, its ads, campaign and ad set -- on NewsBreak's own
    client, and its four requests are counted under NewsBreak, never against Google's budget."""
    monkeypatch.setattr(creative, "ROOT", creative_set(tmp_path / "creative"))
    launched(conn, gads)
    secs = newsbreak_only()
    secs["ads"]["channels"][0].update(headlines=list(HEADLINES), descriptions=list(DESCRIPTIONS))
    secs["creative"] = {**secs["creative"], "approved_image": {NB: "dentists/1.jpg"}}
    _key, other_plan = seed_plan(conn, secs, biz="biz_2", cid=OTHER_CID)
    funds(conn, biz="biz_2")
    ads_run.queue(conn, "biz_2", NB, other_plan, ready=True)
    assert ads_run.launch(conn, config.load(), "biz_2", NB)["state"] == "live"
    first = next(iter(nb_live.ads))
    nb_live.add_report_row(first, ads_run.account_today(nb_row(conn, "biz_2")).isoformat(), cost=1234, click=3)
    gads.sent.clear()
    nb_live.sent.clear()
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert (out["checked"], out["errors"], out["states"]) == (2, 0, {"live": 2})
    assert out["ops_by_channel"] == {GOOGLE: 4, NB: 4} and out["ops"] == 8  # Google's first sync reads the zone too
    assert nb_live.kinds() == ["reports/getIntegratedReport", "ad/getList", "campaign/getList", "ad-set/getList"]
    day = conn.execute(
        "SELECT cost_micros, clicks FROM metric_daily WHERE business_id = 'biz_2' AND channel = %s", (NB,)
    ).fetchone()
    assert (day["cost_micros"], day["clicks"]) == (12_340_000, 3)


class ReadsNothingYet(FakeAdapter):
    """A channel whose adapter cannot read its platform yet (`pull_metrics` raises `Unsupported`, as M0's NewsBreak stub
    did): it has a client of its own, which sends nothing."""

    def client(self, *, budget_s=None):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))


def test_ads_sync_counts_a_row_its_adapter_cannot_read_yet_without_an_error(
    conn,
    gads,  # noqa: F811 - the fixture of the same name
    live,  # noqa: F811 - the fixture of the same name
    monkeypatch,
):
    """A row with ads on file whose adapter cannot read yet (`Unsupported`) is counted as ads-launch counts a step an
    adapter cannot take (checked, under the state it stays in), with no error, no write and no operation; the Google
    Search row beside it syncs."""
    monkeypatch.setitem(registry.CHANNELS, "reddit", ReadsNothingYet("reddit"))
    monkeypatch.setenv("AGENT_EXEC_REDDIT", "live")
    _key, v1 = launched(conn, gads)
    _campaign(conn, BIZ, "reddit", "live", plan_id=v1, resources={"campaign": "r/1"})
    before = ads_run.get(conn, BIZ, "reddit")
    gads.sent.clear()
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert (out["checked"], out["errors"], out["states"]) == (2, 0, {"live": 2})
    assert out["ops_by_channel"] == {GOOGLE: 4, "reddit": 0} and out["ops"] == 4
    assert ads_run.get(conn, BIZ, "reddit") == before
    assert conn.execute("SELECT count(*) AS n FROM metric_daily WHERE channel = 'reddit'").fetchone()["n"] == 0


def test_googles_ops_budget_counts_and_stops_google_search_alone(conn, monkeypatch, live, nb_live):  # noqa: F811 (fixture)
    """Ruling S4 per channel: `ADS_OPS_DAILY_BUDGET` is Google Ads' quota. It counts Google Search's operations of the
    day -- the ads jobs' and spend-guard's pauses and resumes (`ops_by_channel.google_search`; a run from before the
    loop counts whole: it was Google's alone) -- and never another channel's, and at the budget it stops Google
    Search's rows only: NewsBreak's are still stepped."""
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "100")
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES "
        "('ads-launch', now(), true, '{\"ops\": 57}'), "
        '(\'ads-sync\', now(), true, \'{"ops": 45, "ops_by_channel": {"google_search": 40, "newsbreak": 5}}\'), '
        '(\'ads-launch\', now(), true, \'{"ops": 500, "ops_by_channel": {"newsbreak": 500}}\'), '
        '(\'spend-guard\', now(), true, \'{"paused": 0, "ops": 3, "ops_by_channel": {"google_search": 3}}\'), '
        "('spend-guard', now(), true, '{\"paused\": 2}'), "
        "('ads-launch', now() - interval '2 days', true, '{\"ops_by_channel\": {\"google_search\": 900}}')"
    )
    assert jobs.ops_sent_today(conn) == 100  # 57 + 40 + 3: NewsBreak's 505 never count against Google's budget
    built = {"campaign": "customers/1/campaigns/1", "campaign_budget": "customers/1/campaignBudgets/1"}
    _row_for(conn, "biz_a", "queued")
    _row_for(conn, "biz_b", "live", resources=built)
    _campaign(conn, "biz_b", NB, "waiting_account")
    calls: list[tuple[str, str]] = []

    def step(conn_, settings, biz, channel, *, http=None):
        calls.append((biz, channel))
        return {"state": "waiting_account", "ops": 2}

    monkeypatch.setattr(ads_run, "launch", step)
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert calls == [("biz_b", NB)] and out["stopped"] == "ops budget"
    assert (out["ops"], out["ops_by_channel"]) == (2, {GOOGLE: 0, NB: 2})

    conn.execute("UPDATE channel_campaign SET state = 'live', resources = %s WHERE channel = %s", (Jsonb(built), NB))
    calls.clear()
    monkeypatch.setattr(ads_run, "sync", step)
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert calls == [("biz_b", NB)] and out["stopped"] == "ops budget"
    assert out["ops_by_channel"] == {GOOGLE: 0, NB: 2}


# ---------------------------------------------------------------- NewsBreak's day budget and its throttles (T6-4)

UTC_MIDNIGHT = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
LA = "America/Los_Angeles"


def _newsbreak_requests(conn, job: str, n: int, *, when: str = "now()") -> None:
    """A run of `job` that sent NewsBreak `n` requests, started `when` (an SQL expression)."""
    conn.execute(
        f"INSERT INTO job_run (job, started_at, ok, details) VALUES (%s, {when}, true, %s)",
        (job, Jsonb({"ops_by_channel": {NB: n}})),
    )


def _recorded(calls: list, state: str = "live"):
    """A lifecycle step that only records which row it was asked to take."""

    def step(conn_, settings, biz, channel, *, http=None):
        calls.append((biz, channel))
        return {"state": state, "ops": 1}

    return step


def test_newsbreaks_day_budget_counts_its_requests_of_every_job_that_calls_it_since_midnight_utc(conn):
    """T6-4: NewsBreak's quota is per developer app and day, starting again at 00:00 UTC (Rate Limits): its share
    (`ops_by_channel.newsbreak`) of every job that calls it -- ads-launch, ads-sync, spend-guard, page-check and
    conversion-upload -- counts from midnight UTC, whatever the database session's zone; a run from before the jobs
    counted per channel was Google's alone, and a job that never calls NewsBreak never counts."""
    conn.execute("SET TIME ZONE 'Pacific/Kiritimati'")  # a session whose day never starts at midnight UTC
    after = f"{UTC_MIDNIGHT} + interval '1 minute'"
    for job, n in (("ads-launch", 100), ("ads-sync", 200), ("spend-guard", 3), ("page-check", 4)):
        _newsbreak_requests(conn, job, n, when=after)
    _newsbreak_requests(conn, "conversion-upload", 5, when=after)
    conn.execute(
        f"INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-launch', {after}, true, %s)",
        (Jsonb({"ops": 50}),),
    )
    _newsbreak_requests(conn, "ads-link-sync", 1000, when=after)
    _newsbreak_requests(conn, "ads-sync", 9000, when=f"{UTC_MIDNIGHT} - interval '1 minute'")  # yesterday in UTC
    assert jobs.ops_sent_today(conn, NB) == 312


def test_at_newsbreaks_day_budget_only_its_stops_go_and_google_search_runs_as_usual(conn, monkeypatch, live, nb_live):  # noqa: F811 - the fixtures of the same name
    """T6-4: once NewsBreak's requests of the UTC day reach `NB_QPD_BUDGET`, its rows are left for the rest of the day
    but for what stops spend -- ads-launch takes only an owed pause and a version that leaves NewsBreak out (which
    pauses it), and ads-sync gives each NewsBreak row the stops-only pass instead of the whole read (fix round 1, item
    1) -- and each run says so for NewsBreak (`stopped_by_channel`); Google Search's rows are stepped and read as
    usual, its own `stopped` unset."""
    _newsbreak_requests(conn, "ads-sync", jobs.NB_QPD_BUDGET)
    built = {"campaign": "customers/1/campaigns/1", "campaign_budget": "customers/1/campaignBudgets/1"}
    _row_for(conn, "biz_g", "queued")
    _row_for(conn, "biz_d", "live", resources=built)  # its approved version funds Google Search alone ...
    _campaign(conn, "biz_d", NB, "live", resources={"campaign": "nb/2"})  # ... so its NewsBreak ads are to be paused
    _business(conn, "biz_p", newsbreak="managed")
    _campaign(conn, "biz_p", NB, "paused", resources={"campaign": "nb/1", "pause_owed": True})
    _business(conn, "biz_q", newsbreak="managed")
    _campaign(conn, "biz_q", NB, "queued")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ads_run, "launch", _recorded(calls))
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert calls == [("biz_g", GOOGLE), ("biz_p", NB), ("biz_d", NB)]
    assert out["stopped_by_channel"] == {NB: "ops budget"} and "stopped" not in out
    calls.clear()
    stops: list[tuple[str, str]] = []
    monkeypatch.setattr(ads_run, "sync", _recorded(calls))
    monkeypatch.setattr(ads_run, "sync_stops", _recorded(stops))
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert calls == [("biz_d", GOOGLE)] and stops == [("biz_d", NB), ("biz_p", NB)]
    assert out["stopped_by_channel"] == {NB: "ops budget"} and "stopped" not in out


def test_two_throttled_answers_in_a_row_hold_newsbreak_for_the_rest_of_the_utc_day(conn, nb_live):  # noqa: F811 - the fixtures of the same name
    """T6-4: ads-sync's reads answered with two NewsBreak throttles in a row (code 4034) stop NewsBreak's rows there --
    the third business gets only the stops-only pass, which owes nothing here and so asks nothing -- and the run says
    `throttled`. For the rest of the UTC day every job leaves NewsBreak's work but its stops: ads-launch sends an owed
    pause and starts no launch, conversion-upload sends no lead back. The next day NewsBreak is read again."""
    for biz in ("biz_a", "biz_b", "biz_c"):
        account_id = nb_live.add_account(f"Admiry · Bright Smile · {biz}")
        _business(conn, biz, newsbreak="managed")
        _campaign(conn, biz, NB, "live", resources={"campaign": "nb/1", "ad_account_id": account_id, "time_zone": LA})
    nb_live.throttle = 2
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert nb_live.kinds() == ["reports/getIntegratedReport"] * 2
    assert (out["errors"], out["stopped_by_channel"]) == (2, {NB: "throttled"})
    assert out["ops_by_channel"][NB] == 2  # reads NewsBreak failed still count in its day's budget

    _business(conn, "biz_p", newsbreak="managed")  # the same UTC day: a pause owed to NewsBreak ...
    paused_account = nb_live.add_account("Admiry · Bright Smile · biz_p")
    campaign = nb_live.add_campaign(paused_account, "Admiry · Bright Smile · biz_p")
    plan_id = add_plan_by_hand(conn, newsbreak_only(), biz="biz_p")
    _campaign(
        conn,
        "biz_p",
        NB,
        "paused",
        plan_id=plan_id,
        resources={"campaign": campaign, "ad_account_id": paused_account, "pause_owed": True, "time_zone": LA},
    )
    _business(conn, "biz_q", newsbreak="managed")  # ... a launch ...
    _campaign(conn, "biz_q", NB, "queued")
    registry.save_account(  # ... and a lead from a NewsBreak click to send back
        conn, "biz_a", NB, mode="managed", status="active", external={"ad_account_id": "1000001", "event_id": "77"}
    )
    conn.execute(
        "INSERT INTO lead (id, business_id, kind, click_id, click_id_kind, channel) "
        "VALUES ('ld_1', 'biz_a', 'form', 'nvss_1', 'nb_cid', 'newsbreak')"
    )
    nb_live.sent.clear()
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert nb_live.kinds() == ["campaign/updateStatus"] and nb_live.campaigns[campaign]["status"] == "OFF"
    assert out["stopped_by_channel"] == {NB: "throttled"}
    assert "pause_owed" not in nb_row(conn, "biz_p")["resources"] and nb_row(conn, "biz_q")["state"] == "queued"
    nb_live.sent.clear()
    out = tick.run("conversion-upload", url=TEST_DB_URL)
    assert nb_live.sent == [] and (out["sent"], out["stopped_by_channel"]) == (0, {NB: "throttled"})

    conn.execute("UPDATE job_run SET started_at = started_at - interval '1 day'")  # the next UTC day
    out = tick.run("ads-sync", url=TEST_DB_URL)
    assert "stopped_by_channel" not in out and nb_live.kinds().count("reports/getIntegratedReport") == 4


def test_ads_launch_stops_newsbreak_after_two_throttled_answers_in_a_row(conn, monkeypatch, nb_live):  # noqa: F811 - the fixture of the same name
    """T6-4 in ads-launch: two of NewsBreak's answers in a row throttled on the job's client stop NewsBreak's steps for
    the rest of the run -- the third business is not stepped -- and the run says `throttled`, which holds the rest of
    the UTC day."""
    for biz in ("biz_a", "biz_b", "biz_c"):
        _business(conn, biz, newsbreak="managed")
        _campaign(conn, biz, NB, "queued")
    account_id = nb_live.add_account("Admiry · Bright Smile · biz_a")
    nb_live.throttle = 2
    stepped: list[str] = []

    def launch(conn_, settings, biz, channel, *, http=None):  # a step whose first NewsBreak request is answered
        stepped.append(biz)
        try:
            newsbreak.campaigns(settings, account_id, http=http)
        except newsbreak.NewsBreakError:  # the lifecycle keeps a throttled step's row queued, RETRYING
            return {"state": "queued", "ops": 1}
        return {"state": "live", "ops": 1}

    monkeypatch.setattr(ads_run, "launch", launch)
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert stepped == ["biz_a", "biz_b"] and out["stopped_by_channel"] == {NB: "throttled"}
    stepped.clear()
    assert tick.run("ads-launch", url=TEST_DB_URL)["stopped_by_channel"] == {NB: "throttled"} and stepped == []


def test_page_check_pauses_newsbreak_through_its_throttles_and_records_them_for_the_day(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    pages,  # noqa: F811 - the fixture of the same name
):
    """T6-4 in page-check: its pauses of NewsBreak are stops and go out whatever the day says; two of them answered
    with throttles in a row hold NewsBreak's other work for the rest of the UTC day, like any job's -- and the next
    hourly ads-launch still sends the pauses NewsBreak did not take, while it starts nothing else there."""
    for n, biz in enumerate(("biz_a", "biz_b")):
        _business(conn, biz, newsbreak="managed")
        url = f"https://bright{n}.example/invisalign"
        page = landing.set_owner_url(conn, biz, url)
        account_id = nb_live.add_account(f"Admiry · Bright Smile · {biz}")
        campaign = nb_live.add_campaign(account_id, f"Admiry · Bright Smile · {biz}")
        plan_id = add_plan_by_hand(conn, newsbreak_only(), biz=biz)
        resources = {"campaign": campaign, "ad_account_id": account_id, "click_url": nb_build.page_url(page["url"])}
        _campaign(conn, biz, NB, "live", plan_id=plan_id, resources=resources)
        pages.down(url)
    run_job()  # the first counted failure of each page
    nb_live.throttle = 2
    out = run_job()  # the second: both pauses go out and NewsBreak throttles both
    assert nb_live.kinds() == ["campaign/updateStatus", "campaign/updateStatus"]
    assert out["stopped_by_channel"] == {NB: "throttled"}
    assert {nb_row(conn, biz)["resources"].get("pause_owed") for biz in ("biz_a", "biz_b")} == {True}
    nb_live.sent.clear()
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert nb_live.kinds() == ["campaign/updateStatus", "campaign/updateStatus"]  # the owed pauses: stops go
    assert out["stopped_by_channel"] == {NB: "throttled"}
    assert {c["status"] for c in nb_live.campaigns.values()} == {"OFF"}


def test_spend_guard_leaves_newsbreaks_lift_for_the_next_utc_day_but_never_google_searchs(
    conn,
    monkeypatch,
    live,  # noqa: F811 - the fixture of the same name
    nb_live,  # noqa: F811 - the fixture of the same name
):
    """T6-4 in spend-guard: lifting a month cap is no stop, so NewsBreak's waits while NewsBreak is held for the day
    (here its budget is spent) and goes the next day; Google Search's lift never waits on Google's budget (A3 S4:
    spend-guard's pauses and lifts are counted, never stopped)."""
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "10")
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-sync', now(), true, %s)",
        (Jsonb({"ops_by_channel": {GOOGLE: 10, NB: jobs.NB_QPD_BUDGET}}),),
    )
    _row_for(conn, "biz_g", "paused")
    _business(conn, "biz_n", newsbreak="managed")
    _campaign(conn, "biz_n", NB, "paused", plan_id=add_plan_by_hand(conn, newsbreak_only(), biz="biz_n"))
    conn.execute("UPDATE channel_campaign SET paused_reason = 'month_cap'")
    lifted: list[tuple[str, list[str]]] = []

    def resume_channels(conn_, settings, business_id, *, reason, by, channels=None):
        lifted.append((business_id, channels))
        return {"channels": {}, "say": ""}

    monkeypatch.setattr(ads_run, "resume_channels", resume_channels)
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert lifted == [("biz_g", [GOOGLE])] and out["stopped_by_channel"] == {NB: "ops budget"}
    conn.execute("UPDATE job_run SET started_at = started_at - interval '1 day'")
    lifted.clear()
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert sorted(lifted) == [("biz_g", [GOOGLE]), ("biz_n", [NB])] and "stopped_by_channel" not in out


def test_spend_guard_leaves_newsbreaks_funds_lift_for_the_next_utc_day(
    conn,
    monkeypatch,
    nb_live,  # noqa: F811 - the fixture of the same name
):
    """A3c-1 review: the funds stop's lift (A3c-1, `_funds_stop`) is no stop either -- NewsBreak's `no_funds` pause
    waits while NewsBreak is held for the day (T6-4) and is lifted the next day, as the month cap's."""
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-sync', now(), true, %s)",
        (Jsonb({"ops_by_channel": {NB: jobs.NB_QPD_BUDGET}}),),
    )
    _business(conn, "biz_n", newsbreak="managed")
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_n'")
    _campaign(conn, "biz_n", NB, "paused", plan_id=add_plan_by_hand(conn, newsbreak_only(), biz="biz_n"))
    conn.execute("UPDATE channel_campaign SET paused_reason = 'no_funds'")
    lifted: list[tuple[str, str, list[str]]] = []

    def resume_channels(conn_, settings, business_id, *, reason, by, channels=None):
        lifted.append((business_id, reason, channels))
        return {"channels": {}, "say": ""}

    monkeypatch.setattr(ads_run, "resume_channels", resume_channels)
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert lifted == [] and out["stopped_by_channel"] == {NB: "ops budget"}
    conn.execute("UPDATE job_run SET started_at = started_at - interval '1 day'")
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert lifted == [("biz_n", "no_funds", [NB])] and "stopped_by_channel" not in out


def test_page_check_leaves_newsbreaks_resume_for_the_next_utc_day(conn, monkeypatch, nb_live, pages):  # noqa: F811 - the fixtures of the same name
    """T6-4 in page-check: undoing its own pause once the page opens again is no stop, so NewsBreak's waits while
    NewsBreak is held for the day (here two throttles in a row earlier today) and goes the next day."""
    _business(conn, "biz_n", newsbreak="managed")
    page = landing.set_owner_url(conn, "biz_n", PAGE_URL)
    resources = {"campaign": "nb/1", "click_url": nb_build.page_url(page["url"]), "paused_by": jobs.PAGE_CHECK}
    _campaign(conn, "biz_n", NB, "paused", resources=resources)
    conn.execute("UPDATE channel_campaign SET paused_reason = 'unreachable'")
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-sync', now(), true, %s)",
        (Jsonb({"ops_by_channel": {NB: 2}, "stopped_by_channel": {NB: "throttled"}}),),
    )
    resumed: list[tuple[str, str]] = []

    def resume(conn_, settings, biz, channel, *, http=None, reason, only_if=None):
        resumed.append((biz, channel))
        return {"state": "paused", "ops": 0}

    monkeypatch.setattr(ads_run, "resume", resume)
    out = tick.run("page-check", url=TEST_DB_URL)
    assert resumed == [] and out["stopped_by_channel"] == {NB: "throttled"}
    conn.execute("UPDATE job_run SET started_at = started_at - interval '1 day'")
    out = tick.run("page-check", url=TEST_DB_URL)
    assert resumed == [("biz_n", NB)] and "stopped_by_channel" not in out


# ---------------------------------------------------------------- wallet-settle


def test_wallet_settle_posts_each_closed_day_of_every_managed_channel_once(conn, clock, monkeypatch, bing_from_balance):  # noqa: F811 (fixture)
    """Ruling 3: one `spend` entry per managed channel and closed day, the cost rounded up to the cent -- never today in
    the channel's own account calendar (the platform still adds to it), never a day twice (its ref: a day the platform
    revised later is not posted again), never an own-account channel (the owner pays that platform), nothing for a
    zero-cost day. Without the overspend absorber (M1m adds it) the job does the rest as it is."""
    monkeypatch.delattr(wallet, "absorb_overspend", raising=False)
    clock(datetime(2026, 9, 28, 3, 30, tzinfo=UTC))  # 20:30 on the 27th in Palo Alto; already the 28th in UTC
    _business(conn, "biz_m", microsoft_search="managed", newsbreak="managed")
    _campaign(conn, "biz_m", BING, "live", resources={"campaign": "c/1", "time_zone": "America/Los_Angeles"})
    _campaign(conn, "biz_m", NB, "live", resources={"campaign": "nb/1"})  # its zone not known yet: UTC
    for day, cost in ((24, 0), (25, 5_000_000), (26, 1_234_567)):
        _day(conn, "biz_m", BING, date(2026, 9, day), cost)
    _day(conn, "biz_m", BING, date(2026, 9, 27), 7_250_000, synced_at=datetime(2026, 9, 28, 3, 9, tzinfo=UTC))
    _day(conn, "biz_m", NB, date(2026, 9, 26), 9_000_000)
    _day(conn, "biz_m", NB, date(2026, 9, 27), 3_000_000, synced_at=datetime(2026, 9, 28, 3, 9, tzinfo=UTC))
    _business(conn, "biz_o", google_search="own_account")
    _day(conn, "biz_o", GOOGLE, date(2026, 9, 26), 4_000_000)
    _business(conn, "biz_x", google_search="own_account", newsbreak="managed")
    _day(conn, "biz_x", GOOGLE, date(2026, 9, 26), 2_000_000)
    _day(conn, "biz_x", NB, date(2026, 9, 26), 1_000_000)

    settled = {"settled": 5, "waiting": 0, "businesses": 2, "errors": 0}
    assert tick.run("wallet-settle", url=TEST_DB_URL) == settled
    assert _spent(conn) == {
        ("biz_m", "microsoft_search:2026-09-25"): -500,
        ("biz_m", "microsoft_search:2026-09-26"): -124,  # $1.234567, up to the cent
        ("biz_m", "newsbreak:2026-09-26"): -900,
        ("biz_m", "newsbreak:2026-09-27"): -300,  # the 27th is over in NewsBreak's calendar (UTC here)
        ("biz_x", "newsbreak:2026-09-26"): -100,
    }
    audited = conn.execute(
        "SELECT business_id, actor, actor_class, tool, ok, args_redacted, result FROM agent_action "
        "WHERE tool = 'job.wallet_settle' ORDER BY id"
    ).fetchall()
    assert [(r["business_id"], r["args_redacted"], r["result"]) for r in audited] == [  # by day, then card order
        ("biz_m", {"channel": BING, "day": "2026-09-25"}, {"cost_cents": 500}),
        ("biz_m", {"channel": NB, "day": "2026-09-26"}, {"cost_cents": 900}),
        ("biz_m", {"channel": BING, "day": "2026-09-26"}, {"cost_cents": 124}),
        ("biz_m", {"channel": NB, "day": "2026-09-27"}, {"cost_cents": 300}),
        ("biz_x", {"channel": NB, "day": "2026-09-26"}, {"cost_cents": 100}),
    ]
    assert {(r["actor"], r["actor_class"]) for r in audited} == {("job:wallet-settle", "job")}
    said = [tools_account.sentence(r, None) for r in audited]
    assert said[2] == "Admiry charged your balance $1.24 for Bing Search ads on Sep 26"
    assert said[3] == "Admiry charged your balance $3.00 for NewsBreak ads on Sep 27"
    assert all(plain(s) for s in said)

    conn.execute(  # the platform revises a day already posted: a correction is an adjustment, never a second entry
        "UPDATE metric_daily SET cost_micros = 6000000, synced_at = %s WHERE business_id = 'biz_m' AND channel = %s "
        "AND day = '2026-09-25'",
        (datetime(2026, 9, 28, 2, 7, tzinfo=UTC), BING),
    )
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {**settled, "settled": 0}
    assert len(_spent(conn)) == 5


def test_wallet_settle_takes_google_search_only_from_the_first_account_admiry_ran_it_in(conn, clock):  # noqa: F811 (fixture)
    """Final review I4, A3c-1: a prepaid business's Google Search runs in a pool account Admiry pays Google for since
    A3c-1, so its closed days are the balance's -- but only from the day Admiry first ran it in one
    (`google_managed.first_bound`, a day of the account's calendar, UTC while its zone is unknown): a day before that
    was the owner's own account's, with Google, and never comes from the balance; with no account of Admiry's yet,
    none does. NewsBreak, run from the balance from its first day, is settled as before."""
    clock(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    _business(conn, "biz_m", google_search="managed", newsbreak="managed")
    _day(conn, "biz_m", GOOGLE, date(2026, 9, 26), 5_000_000)  # no account of Admiry's: never the balance's
    _day(conn, "biz_m", NB, date(2026, 9, 26), 1_000_000)
    _business(conn, "biz_g", google_search="managed")
    _day(conn, "biz_g", GOOGLE, date(2026, 9, 25), 1_000_000)  # before its first account: the owner's own
    _day(conn, "biz_g", GOOGLE, date(2026, 9, 26), 2_000_000)
    conn.execute(
        "INSERT INTO google_ads_pool (customer_id, scope, added_by) VALUES ('5555555555', 'root', 'ops:cli');"
        "INSERT INTO google_ads_binding (business_id, customer_id, family, bound_by, bound_at) "
        "VALUES ('biz_g', '5555555555', 'dentists', 'job:allocator', '2026-09-26 08:00+00')"
    )
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 2, "waiting": 0, "businesses": 2, "errors": 0}
    assert _spent(conn) == {("biz_m", "newsbreak:2026-09-26"): -100, ("biz_g", "google_search:2026-09-26"): -200}
    audited = conn.execute(
        "SELECT business_id, args_redacted FROM agent_action WHERE tool = 'job.wallet_settle' ORDER BY business_id"
    )
    assert [(r["business_id"], r["args_redacted"]["channel"]) for r in audited] == [("biz_g", GOOGLE), ("biz_m", NB)]


def test_wallet_settle_posts_a_day_only_once_a_read_after_its_close_has_its_whole_cost(conn, clock):  # noqa: F811 (fixture)
    """Fix rounds 1 and 3: a day is posted once, so it waits for a read by ads-sync at least `jobs.SETTLE_LAG` (three
    hours) after it closed in the account's own zone -- the platform keeps adding to a day for a while after it ends.
    Palo Alto's 27th, read at 01:09 there (after its close, inside the margin), waits (`waiting`) and is posted with
    its whole cost once the 10:09 UTC sync read it; Berlin's 27th closed at 22:00 UTC and was read at 01:09 UTC, past
    the margin, so it is posted at once; each account's today is never posted."""
    clock(datetime(2026, 9, 28, 8, 20, tzinfo=UTC))  # 01:20 on the 28th in Palo Alto, 10:20 in Berlin
    for biz, zone in (("biz_la", "America/Los_Angeles"), ("biz_de", "Europe/Berlin")):
        _business(conn, biz, newsbreak="managed")
        _campaign(conn, biz, NB, "live", resources={"campaign": "nb/1", "time_zone": zone})
    _day(conn, "biz_la", NB, date(2026, 9, 27), 5_000_000, synced_at=datetime(2026, 9, 28, 8, 9, tzinfo=UTC))
    _day(conn, "biz_la", NB, date(2026, 9, 28), 400_000, synced_at=datetime(2026, 9, 28, 8, 9, tzinfo=UTC))
    _day(conn, "biz_de", NB, date(2026, 9, 27), 2_000_000, synced_at=datetime(2026, 9, 28, 1, 9, tzinfo=UTC))
    _day(conn, "biz_de", NB, date(2026, 9, 28), 300_000, synced_at=datetime(2026, 9, 28, 8, 9, tzinfo=UTC))

    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 1, "waiting": 1, "businesses": 2, "errors": 0}
    assert _spent(conn) == {("biz_de", "newsbreak:2026-09-27"): -200}
    conn.execute(  # the 10:09 UTC sync reads Palo Alto's 27th again, past the margin, with the rest of its cost
        "UPDATE metric_daily SET cost_micros = 7250000, synced_at = %s WHERE business_id = 'biz_la' AND day = %s",
        (datetime(2026, 9, 28, 10, 9, tzinfo=UTC), date(2026, 9, 27)),
    )
    clock(datetime(2026, 9, 28, 10, 27, tzinfo=UTC))
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 1, "waiting": 0, "businesses": 2, "errors": 0}
    assert _spent(conn) == {
        ("biz_de", "newsbreak:2026-09-27"): -200,
        ("biz_la", "newsbreak:2026-09-27"): -725,  # the whole day, not the $5.00 read inside the margin
    }
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 0, "waiting": 0, "businesses": 2, "errors": 0}


def test_a_day_posts_from_a_read_exactly_three_hours_after_its_close(conn, clock):  # noqa: F811 (fixture)
    """`jobs.SETTLE_LAG`'s edge: NewsBreak's 26th (its zone unknown: UTC) closed at midnight; a read one minute short
    of three hours after that waits, a read at three hours exactly posts."""
    clock(datetime(2026, 9, 28, 14, 27, tzinfo=UTC))
    close = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    for biz, read in (("biz_early", close + timedelta(hours=2, minutes=59)), ("biz_edge", close + timedelta(hours=3))):
        _business(conn, biz, newsbreak="managed")
        _day(conn, biz, NB, date(2026, 9, 26), 2_000_000, synced_at=read)
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 1, "waiting": 1, "businesses": 2, "errors": 0}
    assert _spent(conn) == {("biz_edge", "newsbreak:2026-09-26"): -200}


def test_a_pacific_day_across_the_november_change_waits_for_three_hours_after_its_own_close(conn, clock):  # noqa: F811 (fixture)
    """November 1st 2026 is Palo Alto's 25-hour day (daylight time ends at 02:00): it closes at 00:00 PST on the 2nd,
    08:00 UTC -- not 07:00, where a daylight-time midnight would put it -- so a read at 10:30 UTC is still inside the
    three hours and waits, and the 11:07 read posts it."""
    clock(datetime(2026, 11, 2, 14, 27, tzinfo=UTC))
    _business(conn, "biz_la", newsbreak="managed")
    _campaign(conn, "biz_la", NB, "live", resources={"campaign": "nb/1", "time_zone": "America/Los_Angeles"})
    _day(conn, "biz_la", NB, date(2026, 11, 1), 2_500_000, synced_at=datetime(2026, 11, 2, 10, 30, tzinfo=UTC))
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 0, "waiting": 1, "businesses": 1, "errors": 0}
    conn.execute(
        "UPDATE metric_daily SET synced_at = %s WHERE business_id = 'biz_la'",
        (datetime(2026, 11, 2, 11, 7, tzinfo=UTC),),
    )
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 1, "waiting": 0, "businesses": 1, "errors": 0}
    assert _spent(conn) == {("biz_la", "newsbreak:2026-11-01"): -250}


def test_one_business_failing_never_stops_the_others_settling(conn, clock, monkeypatch, caplog):  # noqa: F811 (fixture)
    """Fix round 1, Minor 1: a business whose settling fails -- here M1m's absorber raising -- keeps nothing of that day
    (its transaction is undone), is logged by the error's type only and counted in `errors`; the next business is
    settled as usual."""
    clock(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    for biz in ("biz_a", "biz_b"):
        _business(conn, biz, newsbreak="managed")
        _day(conn, biz, NB, date(2026, 9, 26), 1_000_000)
    absorbed: list[str] = []

    def absorb(conn_, business_id, *, day):
        if business_id == "biz_a":
            raise RuntimeError("boom with ya29.SECRET")
        absorbed.append(business_id)

    monkeypatch.setattr(wallet, "absorb_overspend", absorb, raising=False)
    with caplog.at_level(logging.WARNING):
        out = tick.run("wallet-settle", url=TEST_DB_URL)
    assert out == {"settled": 1, "waiting": 0, "businesses": 2, "errors": 1}
    assert _spent(conn) == {("biz_b", "newsbreak:2026-09-26"): -100} and absorbed == ["biz_b"]
    audited = conn.execute("SELECT business_id FROM agent_action WHERE tool = 'job.wallet_settle'").fetchall()
    assert [r["business_id"] for r in audited] == ["biz_b"]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "biz_a" in logged and "SECRET" not in logged and "boom" not in logged


def test_wallet_settle_posts_nothing_for_a_day_a_run_beside_it_posted_first(conn, clock, monkeypatch):  # noqa: F811 (fixture)
    """Fix round 1, Minor 6: a run beside this one (another shard's retry, an operator's hand) posts the same day
    between this run's read and its write: the ref refuses the second entry (`wallet.spend` is None), and this run
    writes no audit row and counts nothing."""
    clock(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    _business(conn, "biz_m", newsbreak="managed")
    _day(conn, "biz_m", NB, date(2026, 9, 26), 1_000_000)
    spend = wallet.spend

    def posted_beside_first(conn_, business_id, *, channel, day, cost_cents):
        with db.connect(TEST_DB_URL) as beside:
            spend(beside, business_id, channel=channel, day=day, cost_cents=cost_cents)
        return spend(conn_, business_id, channel=channel, day=day, cost_cents=cost_cents)

    monkeypatch.setattr(wallet, "spend", posted_beside_first)
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 0, "waiting": 0, "businesses": 1, "errors": 0}
    assert _spent(conn) == {("biz_m", "newsbreak:2026-09-26"): -100}  # the one entry, the other run's
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'job.wallet_settle'").fetchone()["n"] == 0


def test_wallet_settle_absorbs_overspend_once_per_settled_day_after_that_days_entries(
    conn,
    clock,  # noqa: F811 - the fixture of the same name
    monkeypatch,
    bing_from_balance,
):
    """M1m's `wallet.absorb_overspend(conn, business_id, *, day)` -- once it exists -- runs once for each day the job
    posted, after every entry of that day is in (two channels' entries of one day: one call), and never for a day
    nothing was posted for."""
    clock(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    _business(conn, "biz_m", microsoft_search="managed", newsbreak="managed")
    _day(conn, "biz_m", BING, date(2026, 9, 25), 1_000_000)
    _day(conn, "biz_m", BING, date(2026, 9, 26), 2_000_000)
    _day(conn, "biz_m", NB, date(2026, 9, 26), 3_000_000)
    _day(conn, "biz_m", NB, date(2026, 9, 27), 0)  # nothing to post: no call for the 27th either
    calls: list[tuple[str, date, list[str]]] = []

    def absorb(conn_, business_id, *, day):
        posted = conn_.execute(
            "SELECT ref FROM wallet_entry WHERE business_id = %s ORDER BY id", (business_id,)
        ).fetchall()
        calls.append((business_id, day, [r["ref"] for r in posted]))

    monkeypatch.setattr(wallet, "absorb_overspend", absorb, raising=False)
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 3, "waiting": 0, "businesses": 1, "errors": 0}
    assert calls == [
        ("biz_m", date(2026, 9, 25), ["microsoft_search:2026-09-25"]),
        (
            "biz_m",
            date(2026, 9, 26),
            ["microsoft_search:2026-09-25", "newsbreak:2026-09-26", "microsoft_search:2026-09-26"],
        ),
    ]
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 0, "waiting": 0, "businesses": 1, "errors": 0}
    assert len(calls) == 2  # nothing posted, nothing absorbed


# ---------------------------------------------------------------- the prod timers


def test_every_job_has_a_prod_timer_that_bootstrap_installs():
    """Each job runs on prod from its own timer through the tick template, installed (not enabled) by bootstrap.sh;
    wallet-settle's runs daily at 14:27 UTC: with `jobs.SETTLE_LAG` (3 h) after the close, the day of every US zone --
    Hawaii's (over at 10:00 UTC) and American Samoa's (11:00 UTC) included -- has closed and been read by the 14:07
    sync by then."""
    boot = (REPO / "deploy/linode-prod/bootstrap.sh").read_text()
    installed = boot[boot.index("for u in bidx-compose.service") :]
    installed = installed[: installed.index("; do")]
    for job in jobs.JOBS:
        timer = (SYSTEMD / f"bidx-agent-{job}.timer").read_text()
        assert f"Unit=bidx-agent-tick@{job}.service" in timer and "Persistent=true" in timer, job
        assert f"bidx-agent-{job}.timer" in installed.split(), job
    settle = (SYSTEMD / "bidx-agent-wallet-settle.timer").read_text()
    assert "OnCalendar=*-*-* 14:27:00 UTC" in settle and "daily" in settle
