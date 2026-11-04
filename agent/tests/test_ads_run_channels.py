"""The channel-agnostic lifecycle over adapters (plan 2026-09-29-agent-m0-channel-framework Task 4, Review Focus 2
and 4; spec 2026-09-29-admiry-channels-design §5.3, §6.4, D77): plans that fund two channels -- Google Search through
its adapter against the fake Google Ads of test_ads_run.py, NewsBreak through its adapter against FakeNewsBreak (M1
Task 5a: its rows here mostly wait, for funds or an image) -- the precheck per funded channel, the pause and resume
per channel and per reason that M1m's money paths call, the numbers they read (`daily_budgets`, `all_paused`), the
execution switch per channel, the Google Search account mirror and the account a campaign was built in."""

from __future__ import annotations

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_mode, ads_run, agreements, config, crawl, db, google_ads_link, ops, plans, proposals, tick, wallet
from agent.channels import base, registry
from agent.channels.google_search import adapter as google
from agent.channels.google_search import build as gbuild
from agent.channels.newsbreak import adapter as nb_adapter
from agent.tests.conftest import TEST_DB_URL, call_tool, tool_text
from agent.tests.fixtures import FakeAdapter, business_with_key
from agent.tests.test_ads_run import (  # noqa: F401 - gads, live and step_clock are fixtures
    BIZ,
    CHECK_OK,
    CID,
    GOOGLE,
    KNOWLEDGE,
    approve,
    gads,
    google_error,
    launched,
    live,
    plain,
    queued,
    row,
    sections,
    seed,
    step_clock,
)
from agent.tests.test_landing import GOOD
from agent.tests.test_page_builder import PUBLIC_IP

NB = "newsbreak"
NO_FUNDS = "Add funds to start NewsBreak"
OTHER_CID = "9876543210"


def two_channels(*, google_usd: float = 1000.0, newsbreak_usd: float = 300.0, **kw) -> dict:
    """A consultation plan's sections funding Google Search and NewsBreak, built by hand at any amounts (the
    allocator's own split is test_allocate.py's and test_plans_channels.py's)."""
    secs = sections(monthly_usd=google_usd, **kw)
    secs["ads"]["channels"].append({"channel": NB, "monthly_usd": newsbreak_usd})
    return secs


def newsbreak_only(monthly_usd: float = 300.0, **kw) -> dict:
    secs = sections(**kw)
    secs["ads"]["channels"] = [{"channel": NB, "monthly_usd": monthly_usd}]
    return secs


def add_plan_by_hand(conn, secs: dict, *, biz: str = BIZ, status: str = "approved") -> str:
    """A plan funding several channels, stored as `test_ads_run.add_plan` stores one, with its owner lines."""
    lines = plans.owner_lines(secs)
    version = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 AS v FROM plan WHERE business_id = %s", (biz,)
    ).fetchone()["v"]
    plan_id = db.new_id("pl")
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES (%s, %s, %s, %s, %s)",
        (plan_id, biz, version, Jsonb({"sections": secs, "lines": lines, "rationale": "", "notes": []}), status),
    )
    return plan_id


def seed_plan(conn, secs: dict, *, status: str = "approved", **kw) -> tuple[str, str]:
    """`seed`'s business (its own Google Search plan superseded) with `secs` as its plan. Returns (key, plan_id)."""
    key, _first = seed(conn, status="superseded", **kw)
    return key, add_plan_by_hand(conn, secs, biz=kw.get("biz", BIZ), status=status)


def funds(conn, cents: int = 50_000, biz: str = BIZ) -> None:
    wallet.add_entry(conn, biz, kind="topup", amount_cents=cents, ref=f"pi_{biz}_{cents}", created_by="stripe")


def nb_row(conn, biz: str = BIZ) -> dict | None:
    return ads_run.get(conn, biz, NB)


def refused(conn, plan: dict) -> str:
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, plan)
    return str(e.value)


@pytest.fixture
def nb_live(monkeypatch, nbapi):
    """NewsBreak's switch `live` -- and NewsBreak faked (M1 Task 5a): the adapter's client is NewsBreak's own now, made
    for every step, pause and approval that reaches the channel. Returns the fake."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    return nbapi


class ReportOnly(FakeAdapter):
    """A channel Admiry only reads (spec D83: Reddit's kind): nothing can be created on it."""

    def capabilities(self):
        return super().capabilities().model_copy(update={"create": False})


class NeedsAViewport(FakeAdapter):
    """A channel whose platform asks more of the page than Google does (M1's NewsBreak: terms, contact), here with
    a rule the check already has; its account is always ready."""

    def capabilities(self):
        return super().capabilities().model_copy(update={"page_rules": ("mobile",)})

    def check_account(self, conn, settings, account):
        return base.AccountStatus(ready=True, status="active", reason=None, account_public=None)


class NeedsARuleTheCheckLacks(NeedsAViewport):
    """A channel naming a page rule the landing check has no rule for: it blocks nothing (logged)."""

    def capabilities(self):
        return super().capabilities().model_copy(update={"page_rules": ("no_such_rule",)})


# ---------------------------------------------------------------- Review Focus 2: one approval, two channels


def test_two_channel_plan_launches_google_and_waits_for_newsbreak(client, conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    key, plan_id = seed_plan(conn, two_channels(), status="draft")
    envelope = approve(client, conn, key, plan_id)
    assert conn.execute("SELECT count(*) AS n FROM approval_code WHERE used_at IS NOT NULL").fetchone()["n"] == 1
    stored = conn.execute("SELECT json->'execution' AS e FROM plan WHERE id = %s", (plan_id,)).fetchone()["e"]
    assert (stored[GOOGLE], stored[NB], stored["ads"]) == ("queued", "waiting_account", "queued")
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("live", plan_id)
    assert gads.kinds() == ["lookup", "create", "enable"]  # Google Search launched in the approval's own request
    nb = nb_row(conn)
    assert (nb["state"], nb["plan_id"], nb["last_error"], nb["resources"]) == (
        "waiting_account",
        plan_id,
        NO_FUNDS,
        {},
    )
    assert ads_run.status_sentence(nb) == "NewsBreak: waiting for funds."
    execution = envelope["data"]["execution"]
    assert (execution[GOOGLE], execution[NB], execution["ads"]) == ("live", "waiting_account", "live")
    assert envelope["say"] == "Confirmed. Google Search: live. NewsBreak: waiting for funds." and plain(envelope["say"])
    line = ads_run.execution_line(ads_run.rows(conn, BIZ))
    assert line == "Google Search: Live since today; NewsBreak: Waiting for funds" and plain(line)
    assert registry.managed_channels(conn, BIZ) == [NB]  # its account row, opened the first time it was funded


def test_a_waiting_channel_moves_on_the_step_its_account_is_ready(client, conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    """Spec §5.3 step 1, §9: a step of a `waiting_account` row re-checks the account (ads-launch takes one every hour
    for every due row; its loop over every channel is M0 Task 7's). NewsBreak's account is ready once there is a
    balance; with no picture approved on the plan it then waits for one (D89) in NewsBreak's words (M1 Task 7 fix round
    1, item 3), and nothing reaches NewsBreak."""
    key, plan_id = seed_plan(conn, two_channels(), status="draft")
    approve(client, conn, key, plan_id)
    settings = config.load()
    due = f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(settings, NB)}"
    assert conn.execute(due).fetchall() != []  # the hourly job's to pick up
    assert ads_run.launch(conn, settings, BIZ, NB)["state"] == "waiting_account"
    assert nb_row(conn)["last_error"] == NO_FUNDS
    funds(conn)
    out = ads_run.launch(conn, settings, BIZ, NB)
    nb = nb_row(conn)
    assert (out["state"], nb["state"], nb["last_error"]) == ("waiting_creative", "waiting_creative", None)
    assert ads_run.status_sentence(nb) == f"NewsBreak: {nb_adapter.IMAGE_WAIT}."
    assert conn.execute(due).fetchall() == []  # nothing to do until a version with its image is approved


def test_a_google_account_that_is_not_ready_waits_while_newsbreak_goes_ahead(conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    """D77: a channel whose account is not ready waits in `waiting_account` and never blocks the others; the reason
    is its own (here the invitation Google has not seen accepted)."""
    _key, plan_id = seed_plan(conn, two_channels(), status="draft", link="pending")
    funds(conn)
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    with conn.transaction():
        out = proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    assert (out["execution"][GOOGLE], out["execution"][NB]) == ("waiting_account", "waiting_creative")
    assert (row(conn)["state"], row(conn)["last_error"]) == ("waiting_account", google.LINK_PENDING)
    assert ads_run.status_sentence(row(conn)) == f"Google Search: waiting for your account. {google.LINK_PENDING}."


def test_a_plan_whose_only_account_is_not_ready_is_refused_before_the_code_is_used(conn, nb_live):
    """Nothing could launch: the precheck refuses with the account's own sentence rather than approving a plan that
    waits for everything (a doomed approval never burns a code)."""
    _key, plan_id = seed_plan(conn, newsbreak_only(), status="draft")
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    assert str(e.value) == NO_FUNDS
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert nb_row(conn) is None


# ---------------------------------------------------------------- the precheck per funded channel


def test_precheck_refuses_a_channel_admiry_cannot_launch(conn, monkeypatch):
    seed(conn)
    monkeypatch.setattr(registry, "CHANNELS", dict(registry.CHANNELS))
    registry.register(ReportOnly("reddit"))
    for channel, label in (("reddit", "Reddit"), ("meta", "Facebook & Instagram")):  # report-only; no adapter at all
        secs = sections()
        secs["ads"]["channels"].append({"channel": channel, "monthly_usd": 600.0})
        assert refused(conn, {"id": "pl_x", "sections": secs}) == ads_run.REPORT_ONLY.format(label=label)
    assert plain(ads_run.REPORT_ONLY.format(label="Reddit"))


def test_precheck_refuses_an_amount_under_the_channels_floor(conn):
    seed(conn)
    sentence = refused(conn, {"id": "pl_x", "sections": two_channels(newsbreak_usd=200.0)})
    assert sentence == ads_run.UNDER_FLOOR.format(label="NewsBreak", floor="$300") and plain(sentence)


def test_precheck_refuses_the_plans_total_over_the_limit(conn):
    seed(conn)  # a $2,000 limit: $1,800 on Google Search is under it, $2,100 across both channels is not
    assert refused(conn, {"id": "pl_x", "sections": two_channels(google_usd=1800.0)}) == ads_run.OVER_LIMIT


def test_precheck_refuses_a_managed_channel_with_no_balance(conn):
    seed(conn)
    assert refused(conn, {"id": "pl_x", "sections": newsbreak_only()}) == NO_FUNDS
    funds(conn)
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_x", "sections": newsbreak_only()})


def test_precheck_refuses_a_zip_channel_without_a_point_and_a_radius(conn):
    seed(conn, knowledge={**KNOWLEDGE, "listing": None})
    funds(conn)
    assert refused(conn, {"id": "pl_x", "sections": newsbreak_only()}) == ads_run.NO_LOCATION
    conn.execute("UPDATE knowledge SET json = %s", (Jsonb(KNOWLEDGE),))
    assert refused(conn, {"id": "pl_x", "sections": newsbreak_only(radius_miles=None)}) == ads_run.NO_RADIUS


def test_a_channels_page_rules_block_its_launch_until_its_page_passes_them(conn, monkeypatch):
    """Capabilities.page_rules are blocking for that channel's launch: on the stored check, a rule it names must
    have passed outright (a warning is not enough). A report the check never ran that rule on is stale for the channel
    and the page is checked again before it is judged (M1 Task 5b, R18 -- M0 let such a rule block nothing, for the
    time before M1 added terms and contact); a rule the check has no rule for at all still blocks nothing."""
    fix = "Add a viewport line."
    secs = sections()
    secs["consultation"].update({"conversions": ["phone_call_60s"], "duration_days": 30})  # as consultations store it
    seed(conn, secs=secs, check={**CHECK_OK, "rules": [*CHECK_OK["rules"], {"id": "mobile", "ok": None, "fix": fix}]})
    monkeypatch.setattr(registry, "CHANNELS", dict(registry.CHANNELS))
    registry.register(NeedsAViewport("reddit"))
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_x", "sections": secs})  # Google Search alone: a warning
    secs["ads"]["channels"] = [{"channel": "reddit", "monthly_usd": 600.0}]
    assert refused(conn, {"id": "pl_x", "sections": secs}) == ads_run.PAGE_FIX.format(fix=fix)
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(CHECK_OK),))  # the check never ran "mobile"
    page_id = conn.execute("SELECT id FROM page WHERE business_id = %s", (BIZ,)).fetchone()["id"]
    fetched: list = []

    def serve(request):
        fetched.append(request)
        body = GOOD.replace("{pid}", page_id)  # a page made for a phone: its viewport line passes "mobile"
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(ads_run, "_check_transport", lambda: httpx.MockTransport(serve))
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_x", "sections": secs})
    saved = conn.execute("SELECT check_json FROM page WHERE id = %s", (page_id,)).fetchone()["check_json"]
    assert len(fetched) == 1 and {r["id"]: r["ok"] for r in saved["rules"]}["mobile"] is True
    registry.register(NeedsARuleTheCheckLacks("meta"))
    secs["ads"]["channels"] = [{"channel": "meta", "monthly_usd": 600.0}]
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_x", "sections": secs})  # blocks nothing, fetches nothing
    assert len(fetched) == 1


def test_precheck_waits_for_the_image_rather_than_refusing(conn):
    """D89: an image channel with no image approved on the plan is `waiting_creative` at approval, not an error."""
    seed(conn)
    funds(conn)
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_x", "sections": newsbreak_only()})


def test_precheck_checks_only_the_channels_it_is_asked_about(conn):
    seed(conn)  # NewsBreak has no balance; the step of Google Search's own row never asks about it
    plan = {"id": "pl_x", "sections": two_channels(newsbreak_usd=200.0)}
    ads_run.precheck(conn, config.load(), BIZ, plan, channels=[GOOGLE])
    assert refused(conn, plan) == ads_run.UNDER_FLOOR.format(label="NewsBreak", floor="$300")


# ---------------------------------------------------------------- Review Focus 4: pause and resume per channel


def test_pause_channels_is_per_channel_and_per_reason(client, conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    key, plan_id = seed_plan(conn, two_channels(), status="draft")
    approve(client, conn, key, plan_id)
    settings = config.load()

    out = ads_run.pause_channels(conn, settings, BIZ, channels=[NB], by="assistant", reason="owner")
    assert list(out["channels"]) == [NB] and out["say"] == "NewsBreak: paused by you today."
    nb = nb_row(conn)
    assert (nb["state"], nb["paused_reason"], nb["resources"]["owner_paused"]) == ("paused", None, True)
    assert row(conn)["state"] == "live" and "pause" not in gads.kinds()  # Google Search untouched

    out = ads_run.pause_channels(conn, settings, BIZ, by="job:spend-guard", reason="month_cap")  # every row
    assert set(out["channels"]) == {GOOGLE, NB}
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("paused", "month_cap")
    assert "owner_paused" not in row(conn)["resources"] and gads.kinds().count("pause") == 1  # Google Ads was told
    assert ads_run.status_sentence(row(conn)) == "Google Search: paused by Admiry today: this month's budget is spent."
    nb = nb_row(conn)  # both marks stand on NewsBreak: the owner's and Admiry's
    assert (nb["paused_reason"], nb["resources"]["owner_paused"]) == ("month_cap", True)
    assert ads_run.status_sentence(nb) == "NewsBreak: paused by you and by Admiry today: this month's budget is spent."

    assert ads_run.resume_channels(conn, settings, BIZ, reason="no_funds", by="job:spend-guard")["channels"] == {}
    out = ads_run.resume_channels(conn, settings, BIZ, reason="month_cap", by="job:spend-guard")
    assert {ch: o["state"] for ch, o in out["channels"].items()} == {GOOGLE: "live", NB: "paused"}
    assert out["channels"][NB]["say"] == "NewsBreak: paused by you today."  # why it stays paused
    assert (row(conn)["state"], row(conn)["paused_reason"]) == ("live", None)
    nb = nb_row(conn)  # an owner's pause is never lifted by a job
    assert (nb["state"], nb["paused_reason"], nb["resources"]["owner_paused"]) == ("paused", None, True)

    out = ads_run.resume_channels(conn, settings, BIZ, reason="owner", by="assistant")
    assert list(out["channels"]) == [NB]
    nb = nb_row(conn)
    assert (nb["state"], nb["paused_reason"], nb["last_error"]) == ("waiting_account", None, NO_FUNDS)
    assert "owner_paused" not in nb["resources"]


def test_an_owner_pause_then_a_dispute_then_the_owners_resume_stays_paused_for_the_dispute(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    """Ruling (fix round 1): the owner's pause is a mark of its own; Admiry's reason is `paused_reason`. The owner's
    resume clears only their mark, and the ads stay off while the dispute stands."""
    launched(conn, gads)
    settings, before = config.load(), len(gads.kinds())
    ads_run.pause_by_owner(conn, settings, BIZ, "all", by="assistant")
    ads_run.pause_channels(conn, settings, BIZ, by="stripe", reason="dispute")
    r = row(conn)
    assert (r["paused_reason"], r["resources"]["owner_paused"]) == ("dispute", True)
    assert ads_run.status_sentence(r) == "Google Search: paused by you and by Admiry today: a payment dispute is open."
    out = ads_run.resume_by_owner(conn, settings, BIZ, "all", by="assistant")
    assert out["channels"][GOOGLE]["state"] == "paused"
    assert out["say"] == "Google Search: paused by Admiry today: a payment dispute is open."
    r = row(conn)
    assert (r["state"], r["paused_reason"], "owner_paused" in r["resources"]) == ("paused", "dispute", False)
    assert "enable" not in gads.kinds()[before:] and ads_run.all_paused(conn, BIZ, [GOOGLE])
    out = ads_run.resume_channels(conn, settings, BIZ, reason="dispute", by="stripe")  # the dispute closed
    assert out["channels"][GOOGLE]["state"] == "live" and row(conn)["paused_reason"] is None


def test_a_dispute_then_an_owner_pause_then_the_disputes_end_stays_paused_for_the_owner(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    launched(conn, gads)
    settings, before = config.load(), len(gads.kinds())
    ads_run.pause_channels(conn, settings, BIZ, by="stripe", reason="dispute")
    ads_run.pause_by_owner(conn, settings, BIZ, "all", by="owner")  # the owner's own pause adds its mark
    r = row(conn)
    assert (r["paused_reason"], r["resources"]["owner_paused"]) == ("dispute", True)
    out = ads_run.resume_channels(conn, settings, BIZ, reason="dispute", by="stripe")
    assert (out["channels"][GOOGLE]["state"], out["say"]) == ("paused", "Google Search: paused by you today.")
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["owner_paused"]) == ("paused", None, True)
    assert "enable" not in gads.kinds()[before:]
    assert ads_run.resume_by_owner(conn, settings, BIZ, GOOGLE, by="owner")["channels"][GOOGLE]["state"] == "live"


def test_a_stronger_reason_of_admirys_replaces_a_weaker_one_and_never_the_reverse(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    """The ruling's order: dispute > stop_requested > no_funds > month_cap > no_leads > dropped."""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause_channels(conn, settings, BIZ, by="job:optimize", reason="no_leads")
    for reason, stands in (("month_cap", "month_cap"), ("dropped", "month_cap"), ("dispute", "dispute")):
        ads_run.pause_channels(conn, settings, BIZ, by="job:x", reason=reason)
        assert row(conn)["paused_reason"] == stands, reason
    for weaker in ("stop_requested", "no_funds", "month_cap", "no_leads", "dropped"):
        ads_run.pause_channels(conn, settings, BIZ, by="job:x", reason=weaker)
        assert row(conn)["paused_reason"] == "dispute", weaker
    assert ads_run.resume_channels(conn, settings, BIZ, reason="month_cap", by="job:x")["channels"] == {}
    assert row(conn)["state"] == "paused"


def test_an_admiry_pause_landing_while_the_owners_resume_runs_wins(conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """S17 with the marks: a dispute pause that lands between the owner's resume reading the row and writing it moves
    `paused_at`, so the resume's write (conditioned on the `paused_at` it read) never happens -- the ads stay off."""
    launched(conn, gads)
    ads_run.pause_by_owner(conn, config.load(), BIZ, "all", by="owner")
    before = len(gads.kinds())
    original, raced = ads_run.get, []

    def get_then_dispute(c, biz, channel):
        found = original(c, biz, channel)
        if not raced:  # the resume's own read: the dispute's pause lands right after it, without the lock
            raced.append(found["paused_at"])
            with db.connect(TEST_DB_URL) as other:
                ads_run.pause_channels(other, config.load(), BIZ, by="stripe", reason="dispute")
        return found

    monkeypatch.setattr(ads_run, "get", get_then_dispute)
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    r = original(conn, BIZ, GOOGLE)
    assert out["state"] == "paused" and (r["state"], r["paused_reason"]) == ("paused", "dispute")
    assert r["paused_at"] > raced[0] and "enable" not in gads.kinds()[before:]


def test_a_pause_landing_while_a_resume_lifts_one_of_two_pauses_wins(conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """S17 on the partial lift: the owner resumes while a dispute stands, and pauses again from another tab between
    the resume's read and its write. The resume's lift is conditioned on the `paused_at` it read, so the owner's
    latest word -- the pause -- stands, beside the dispute."""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause_by_owner(conn, settings, BIZ, "all", by="owner")
    ads_run.pause_channels(conn, settings, BIZ, by="stripe", reason="dispute")
    original, raced = ads_run.get, []

    def get_then_owner_pause(c, biz, channel):
        found = original(c, biz, channel)
        if not raced:  # the resume's own read: the owner's pause from the other tab lands right after it
            raced.append(True)
            with db.connect(TEST_DB_URL) as other:
                ads_run.pause_by_owner(other, config.load(), BIZ, "all", by="owner")
        return found

    monkeypatch.setattr(ads_run, "get", get_then_owner_pause)
    out = ads_run.resume(conn, settings, BIZ, GOOGLE)
    r = original(conn, BIZ, GOOGLE)
    assert raced and out["state"] == "paused"
    assert (r["state"], r["paused_reason"], r["resources"].get("owner_paused")) == ("paused", "dispute", True)


def test_resume_lifts_only_a_pause_that_stands(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    """`resume` itself refuses a pause that is not there: the owner's resume of Admiry's pause alone
    (PAUSED_BY_ADMIRY), and Admiry's resume for a reason not standing (NOT_PAUSED); the row stays as it was."""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause_channels(conn, settings, BIZ, by="job:spend-guard", reason="month_cap")
    before = row(conn)
    with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):
        ads_run.resume(conn, settings, BIZ, GOOGLE)
    with pytest.raises(ToolError, match=ads_run.NOT_PAUSED):
        ads_run.resume(conn, settings, BIZ, GOOGLE, reason="dispute")
    assert row(conn) == before


def test_an_admiry_pause_that_read_a_paused_row_a_resume_then_turned_on_still_pauses_it(conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """S17 the other way round: the dispute's pause reads the row while it is still paused, and a resume turns it back
    on before the pause writes. The pause must not only add its reason to a running row: it pauses it for real."""
    launched(conn, gads)
    ads_run.pause_by_owner(conn, config.load(), BIZ, "all", by="owner")
    before = len(gads.kinds())
    original, raced = ads_run.get, []

    def get_then_resumed(c, biz, channel):
        found = original(c, biz, channel)
        if not raced and found["state"] == "paused":  # the pause's own read: the owner's resume finishes right after
            raced.append(True)
            with db.connect(TEST_DB_URL) as other:
                other.execute(
                    "UPDATE channel_campaign SET state = 'live', paused_at = NULL, paused_reason = NULL, "
                    "resources = resources - 'paused_by' - 'owner_paused' WHERE business_id = %s",
                    (BIZ,),
                )
        return found

    monkeypatch.setattr(ads_run, "get", get_then_resumed)
    out = ads_run.pause_channels(conn, config.load(), BIZ, by="stripe", reason="dispute")
    r = original(conn, BIZ, GOOGLE)
    assert raced and out["channels"][GOOGLE]["state"] == "paused"
    assert (r["state"], r["paused_reason"], "owner_paused" in r["resources"]) == ("paused", "dispute", False)
    assert "pause" in gads.kinds()[before:]  # Google Ads was told


def test_an_owner_pause_racing_an_admiry_pause_on_running_ads_keeps_admirys_reason(conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """Two pauses that both read the ads running: the one that writes last must not wipe the other's mark -- here the
    owner's pause (holding the step lock) writes after a dispute's pause landed without it."""
    launched(conn, gads)
    original, raced = ads_run.get, []

    def get_then_dispute(c, biz, channel):
        found = original(c, biz, channel)
        if not raced and found["state"] == "live":  # the owner's pause's own read: the dispute's lands right after
            raced.append(True)
            with db.connect(TEST_DB_URL) as other:
                ads_run.pause_channels(other, config.load(), BIZ, by="stripe", reason="dispute")
        return found

    monkeypatch.setattr(ads_run, "get", get_then_dispute)
    ads_run.pause_by_owner(conn, config.load(), BIZ, "all", by="owner")
    r = original(conn, BIZ, GOOGLE)
    assert raced and (r["state"], r["paused_reason"], r["resources"]["owner_paused"]) == ("paused", "dispute", True)
    out = ads_run.resume_by_owner(conn, config.load(), BIZ, "all", by="owner")  # the dispute still stands
    assert out["channels"][GOOGLE]["state"] == "paused" and original(conn, BIZ, GOOGLE)["paused_reason"] == "dispute"


def test_an_approval_lifting_dropped_during_the_owners_resume_leaves_the_owners_pause(conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """Final review (the Task 4 re-review's first race): the owner resumes a row paused by them and `dropped`; an
    approval funding the channel again commits between the resume's read and its write, lifting `dropped`. The
    resume's partial lift is conditioned on the other pause as it read it, so it does not run: the row is left as the
    approval left it -- paused by the owner alone, never paused with neither mark nor reason -- and the owner's next
    resume turns it on."""
    _key, v1 = launched(conn, gads)
    settings = config.load()
    ads_run.pause_by_owner(conn, settings, BIZ, GOOGLE, by="owner")  # the owner's mark
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="admiry", reason="dropped")  # a version left Google out
    original, raced = ads_run.get, []

    def get_then_queue(c, biz, channel):
        found = original(c, biz, channel)
        if not raced:  # the owner's resume read the row; an approval funding Google again commits right after
            raced.append(True)
            with db.connect(TEST_DB_URL) as other:
                ads_run.queue(other, BIZ, GOOGLE, v1, ready=True)
        return found

    monkeypatch.setattr(ads_run, "get", get_then_queue)
    out = ads_run.resume(conn, settings, BIZ, GOOGLE)
    monkeypatch.setattr(ads_run, "get", original)
    r = row(conn)
    assert raced and out["state"] == "paused"
    assert (r["state"], r["paused_reason"], r["resources"].get("owner_paused")) == ("paused", None, True)
    assert ads_run.status_sentence(r) == "Google Search: paused by you today."
    assert ads_run.resume_by_owner(conn, settings, BIZ, GOOGLE, by="owner")["channels"][GOOGLE]["state"] == "live"


def test_an_admiry_pause_never_puts_back_the_mark_the_owner_just_lifted(conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """Final review (the Task 4 re-review's second race): a month cap's pause reads a row paused by the owner and for no
    leads; the owner's resume (holding the step lock) lifts the owner's mark -- no leads stands -- before the pause
    writes. The pause adds the owner's mark only to an A3 pause (a paused row with neither mark nor reason), so the
    lifted mark stays lifted: the row is paused for Admiry's month cap alone."""
    launched(conn, gads)
    settings = config.load()
    ads_run.pause_by_owner(conn, settings, BIZ, GOOGLE, by="owner")
    ads_run.pause_channels(conn, settings, BIZ, by="job:optimize", reason="no_leads")
    original, raced = ads_run.get, []

    def get_then_owner_lift(c, biz, channel):
        found = original(c, biz, channel)
        if not raced and found["state"] == "paused":  # the month cap's pause read the row (the mark on it)
            raced.append(True)
            with db.connect(TEST_DB_URL) as other:  # the owner's partial lift, as `resume` writes it, lands now
                cur = other.execute(
                    "UPDATE channel_campaign SET resources = resources - 'owner_paused', updated_at = now() "
                    "WHERE business_id = %s AND channel = %s AND paused_at IS NOT DISTINCT FROM %s",
                    (BIZ, GOOGLE, found["paused_at"]),
                )
                assert cur.rowcount == 1
        return found

    monkeypatch.setattr(ads_run, "get", get_then_owner_lift)
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="job:spend-guard", reason="month_cap")
    monkeypatch.setattr(ads_run, "get", original)
    r = row(conn)
    assert raced and (r["state"], r["paused_reason"]) == ("paused", "month_cap")
    assert "owner_paused" not in r["resources"]
    assert ads_run.status_sentence(r) == "Google Search: paused by Admiry today: this month's budget is spent."


def test_pause_channels_names_only_rows_there_are_and_refuses_no_one(conn):
    seed(conn)
    assert ads_run.pause_channels(conn, config.load(), BIZ, by="job:spend-guard", reason="no_funds") == {
        "channels": {},
        "say": "",
    }


def test_an_owner_pause_from_before_m0_is_the_owners(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    """A3 had only the owner's pause: a paused row with neither mark resumes with the owner's."""
    launched(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    conn.execute("UPDATE channel_campaign SET paused_reason = NULL, resources = resources - 'owner_paused'")
    assert ads_run.status_sentence(row(conn)) == "Google Search: paused by you today."
    out = ads_run.resume_channels(conn, config.load(), BIZ, reason="owner", by="owner")
    assert out["channels"][GOOGLE]["state"] == "live"


def test_an_admiry_pause_on_an_owner_pause_from_before_m0_keeps_the_owners(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    """A reason of Admiry's landing on an A3 pause (neither mark) must not hide the owner's: the mark is written."""
    launched(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    conn.execute("UPDATE channel_campaign SET paused_reason = NULL, resources = resources - 'owner_paused'")
    ads_run.pause_channels(conn, config.load(), BIZ, by="job:spend-guard", reason="month_cap")
    r = row(conn)
    assert (r["paused_reason"], r["resources"]["owner_paused"]) == ("month_cap", True)
    assert (
        ads_run.status_sentence(r) == "Google Search: paused by you and by Admiry today: this month's budget is spent."
    )
    out = ads_run.resume_channels(conn, config.load(), BIZ, reason="month_cap", by="job:spend-guard")
    assert out["channels"][GOOGLE]["state"] == "paused" and out["say"] == "Google Search: paused by you today."


# ---------------------------------------------------------------- the numbers M1m reads


def test_daily_budgets_come_from_each_rows_own_plan_version(conn, nb_live):
    _key, plan_id = seed_plan(conn, two_channels(google_usd=1000.0, newsbreak_usd=300.0))
    queued(conn, plan_id)
    ads_run.queue(conn, BIZ, NB, plan_id, ready=True)
    assert ads_run.daily_budgets(conn, BIZ) == {GOOGLE: 3289, NB: 986}  # floor(monthly / 30.4 * 100) cents
    conn.execute("UPDATE channel_campaign SET state = 'live' WHERE channel = %s", (GOOGLE,))
    conn.execute("UPDATE channel_campaign SET state = 'paused' WHERE channel = %s", (NB,))
    assert ads_run.daily_budgets(conn, BIZ) == {GOOGLE: 3289}
    for state in ("waiting_account", "waiting_creative", "failed", "limited"):
        conn.execute("UPDATE channel_campaign SET state = %s WHERE channel = %s", (state, NB))
        assert NB not in ads_run.daily_budgets(conn, BIZ), state
    assert ads_run.daily_budgets(conn, "biz_nobody") == {}


def test_all_paused_is_false_while_a_pause_is_owed(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    launched(conn, gads)
    settings = config.load()
    assert ads_run.all_paused(conn, BIZ, [GOOGLE]) is False
    gads.fail["pause"] = google_error(503)
    ads_run.pause_channels(conn, settings, BIZ, by="job:refunds", reason="stop_requested")
    assert row(conn)["resources"]["pause_owed"] is True
    assert ads_run.all_paused(conn, BIZ, [GOOGLE]) is False
    del gads.fail["pause"]
    tick.run("ads-launch", url=TEST_DB_URL)  # the sweeper tells Google Ads (ruling S11)
    assert ads_run.all_paused(conn, BIZ, [GOOGLE]) is True
    assert ads_run.all_paused(conn, BIZ, [GOOGLE, NB]) is True  # no row: nothing runs there
    assert ads_run.all_paused(conn, BIZ, []) is True


# ---------------------------------------------------------------- launch_now over the funded channels


def test_launch_now_with_two_channels_and_a_tiny_budget_leaves_both_retryable(conn, gads, live, nb_live, step_clock):  # noqa: F811 - the fixture shadows the imported name
    _key, plan_id = seed_plan(conn, two_channels())
    queued(conn, plan_id)
    ads_run.queue(conn, BIZ, NB, plan_id, ready=False, reason=NO_FUNDS)
    gads.delay, gads.sleep = 0.05, step_clock.sleep  # the token exchange alone outlasts the budget (a stepped clock)
    out = ads_run.launch_now(BIZ, budget_s=0.01)
    assert (out["channels"][GOOGLE]["state"], out["channels"][NB]["state"]) == ("queued", "waiting_account")
    assert gads.sent == [] and row(conn)["last_error"] == google.RETRYING
    settings = config.load()
    for channel in (GOOGLE, NB):  # both are the hourly ads-launch's to pick up
        due = conn.execute(f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(settings, channel)}").fetchall()
        assert len(due) == 1, channel


def test_launch_now_names_a_funded_channel_whose_switch_is_off(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    _key, plan_id = seed_plan(conn, two_channels())
    queued(conn, plan_id)
    out = ads_run.launch_now(BIZ)
    assert out["channels"][GOOGLE]["state"] == "live"
    assert out["channels"][NB] == {"state": "off", "say": ads_run.OFF_CHANNEL.format(label="NewsBreak")}


# ---------------------------------------------------------------- the switch per channel (ruling 1)


def test_an_explicit_off_for_google_search_beats_the_old_switch(client, conn, gads, live, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "off")
    key, plan_id = seed(conn, status="draft", link=None, page=False)  # nothing is prechecked with the switch off
    envelope = approve(client, conn, key, plan_id)
    assert (envelope["data"]["execution"][GOOGLE], envelope["data"]["execution"]["ads"]) == ("off", "off")
    assert row(conn) is None and gads.requests == []
    assert tick.run("ads-launch", url=TEST_DB_URL)["modes"] == {}


def test_the_newsbreak_switch_off_leaves_newsbreak_out_while_google_search_launches(client, conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    key, plan_id = seed_plan(conn, two_channels(), status="draft")
    envelope = approve(client, conn, key, plan_id)
    assert (envelope["data"]["execution"][GOOGLE], envelope["data"]["execution"][NB]) == ("live", "off")
    assert nb_row(conn) is None  # nothing is queued, nor asked of its adapter


def test_a_launch_under_google_searchs_own_switch_sets_up_its_conversions_live(conn, gads, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """Ruling 1, the review's scenario (a): AGENT_EXEC_GOOGLE_SEARCH=live with the old switch unset -- the conversion
    setup runs live too, so the ads never go out without Admiry's conversion actions and the call's own action."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    _key, plan_id = seed(conn, conversions=False)
    queued(conn, plan_id)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    assert gads.kinds() == ["lookup", "customer", "actions", "conversions", "actions", "create", "enable"]
    ops = gads.bodies("create")[0]["mutateOperations"]
    asset = next(op["assetOperation"]["create"] for op in ops if "assetOperation" in op)
    assert asset["callAsset"]["callConversionReportingState"] == "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"


def test_a_validate_step_never_creates_conversion_actions_for_real(conn, gads, monkeypatch):  # noqa: F811 - the fixture shadows the imported name
    """Ruling 1, the review's scenario (b): AGENT_EXEC_GOOGLE_SEARCH=validate beats AGENT_ADS_EXECUTION=live -- the
    conversion setup a validate step reaches is validate-only too, and only the account's tag is kept."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    _key, plan_id = seed(conn, conversions=False)
    queued(conn, plan_id)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "queued"
    assert gads.kinds() == ["customer", "actions", "validate_conversions", "validate_create"]
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM conversion_action WHERE business_id = %s", (BIZ,))}
    assert kinds == {"tag"}


def test_due_under_validate_is_the_rows_of_its_own_channel(conn, monkeypatch):
    """Ruling 8 (the Task 3 review gap): `validate`'s branch is filtered by channel like `live`'s."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    _key, plan_id = seed_plan(conn, two_channels())
    queued(conn, plan_id)
    ads_run.queue(conn, BIZ, NB, plan_id, ready=True)
    seed(conn, biz="biz_2", cid=OTHER_CID)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, validated_at) "
        "SELECT 'biz_2', %s, id, 'queued', now() FROM plan WHERE business_id = 'biz_2'",  # checked already
        (GOOGLE,),
    )
    settings = config.load()

    def due(channel: str) -> list[tuple[str, str]]:
        rows = conn.execute(
            f"SELECT business_id, channel FROM channel_campaign c WHERE {ads_run.due(settings, channel)}"
        )
        return [(r["business_id"], r["channel"]) for r in rows.fetchall()]

    assert due(GOOGLE) == [(BIZ, GOOGLE)] and due(NB) == [(BIZ, NB)]


# ---------------------------------------------------------------- the Google Search account mirror (ruling 2)


def test_a_business_linked_after_boot_approves_into_queued(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn, status="draft", link=None)
    assert registry.account(conn, BIZ, GOOGLE) is None  # no row since the migration: linked only now
    google_ads_link.save(conn, BIZ, CID, status="active")
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    with conn.transaction():
        out = proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    assert out["execution"][GOOGLE] == "queued" and row(conn)["state"] == "queued"


def test_every_link_write_keeps_the_google_search_account_row(conn, capsys):
    business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com'")

    def mirrored() -> tuple:
        account = registry.account(conn, BIZ, GOOGLE)
        return (account.mode, account.status, account.external)

    google_ads_link.save(conn, BIZ, CID, status="pending")
    assert mirrored() == ("own_account", "pending", {"customer_id": CID})
    for google_status, status in (("refused", "refused"), ("cancelled", "inactive"), ("active", "active")):
        with conn.transaction():
            google_ads_link.apply(conn, BIZ, CID, google_ads_link.Remote(status=google_status, resource_name=None))
        assert mirrored() == ("own_account", status, {"customer_id": CID}), google_status
    conn.execute("DELETE FROM ads_link")
    with conn.transaction():
        assert ads_mode.change(conn, BIZ, "managed") is True
    assert mirrored() == ("managed", "none", {})
    with conn.transaction():
        ads_mode.change(conn, BIZ, "own_account")
    agreements.accept(
        conn, business_id=BIZ, email="owner@example.com", ip="203.0.113.9"
    )  # the operator's link needs it
    assert ops.main(["ads-link", BIZ, "123-456-7890", "--status", "active"]) == 0
    assert mirrored() == ("own_account", "active", {"customer_id": CID})


# ---------------------------------------------------------------- the account a campaign was built in (ruling 3)


def test_later_steps_address_the_account_that_built_the_ads(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    launched(conn, gads)
    assert row(conn)["resources"]["customer_id"] == CID
    conn.execute("UPDATE ads_link SET customer_id = %s", (OTHER_CID,))  # the owner re-linked another account
    registry.save_account(conn, BIZ, GOOGLE, mode="own_account", status="active", external={"customer_id": OTHER_CID})
    sent = len(gads.requests)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    paths = {q.url.path for q in gads.requests[sent:] if q.url.path.startswith("/v25/customers/")}
    assert paths == {f"/v25/customers/{CID}/googleAds:mutate"}  # never moved by itself
    account = registry.account(conn, BIZ, GOOGLE)
    sentence = ads_run.status_sentence(row(conn), account=account)
    assert sentence == f"Google Search: paused by you today. {ads_run.MOVED}." and plain(sentence)
    assert ads_run.MOVED not in ads_run.status_sentence(row(conn))


# ---------------------------------------------------------------- a channel dropped from the plan (spec §5.3 step 4)


def test_a_channel_a_new_version_drops_is_paused_and_a_later_one_brings_it_back(client, conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    """Spec §5.3 step 4: a channel dropped from the approved version is paused (`dropped`), in the approval's own
    request, never deleted -- its row keeps the resources, and a later version that funds it again turns it back
    on with that version's budget."""
    key, _v1 = launched(conn, gads)
    funds(conn)
    v2 = add_plan_by_hand(conn, newsbreak_only(), status="draft")
    envelope = approve(client, conn, key, v2, idem="c2")
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["paused_by"]) == ("paused", "dropped", "admiry")
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"
    dropped = "Google Search: paused by Admiry today: the plan you approved leaves it out."
    assert ads_run.status_sentence(r) == dropped and dropped in envelope["say"]
    v3 = add_plan_by_hand(conn, two_channels(google_usd=600.0), status="draft")
    approve(client, conn, key, v3, idem="c3")
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["plan_id"]) == ("live", None, v3)
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (campaign["status"], campaign["amount"]) == ("ENABLED", gbuild.daily_micros(600.0))


def test_a_later_version_lifts_only_the_dropped_pause_and_the_owners_stands(client, conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    """The ruling: a version that funds the channel again lifts Admiry's `dropped` -- and nothing else: an owner's
    pause beside it stands, and the ads stay off until the owner resumes."""
    key, _v1 = launched(conn, gads)
    funds(conn)
    approve(client, conn, key, add_plan_by_hand(conn, newsbreak_only(), status="draft"), idem="c2")
    ads_run.pause_by_owner(conn, config.load(), BIZ, GOOGLE, by="owner")
    assert (row(conn)["paused_reason"], row(conn)["resources"]["owner_paused"]) == ("dropped", True)
    before = len(gads.kinds())
    approve(client, conn, key, add_plan_by_hand(conn, two_channels(google_usd=600.0), status="draft"), idem="c3")
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["owner_paused"]) == ("paused", None, True)
    assert "enable" not in gads.kinds()[before:] and gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"
    assert ads_run.status_sentence(r) == "Google Search: paused by you today."


def test_the_step_and_due_agree_on_an_image_row_a_new_version_drops(conn, nb_live):
    """(Fix round 1, minor i.) A `waiting_creative` row waits for a new approval while its channel is funded; once the
    approved version leaves the channel out, the step pauses it (`dropped`) -- and ads-launch's `due` selects it."""
    _key, v1 = seed_plan(conn, two_channels())
    funds(conn)
    ads_run.queue(conn, BIZ, NB, v1, ready=True, image=False)
    settings = config.load()
    due = f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(settings, NB)}"
    assert nb_row(conn)["state"] == "waiting_creative" and conn.execute(due).fetchall() == []
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
    add_plan_by_hand(conn, sections(), status="approved")  # Google Search alone
    assert len(conn.execute(due).fetchall()) == 1
    out = ads_run.launch(conn, settings, BIZ, NB)
    assert (out["state"], nb_row(conn)["paused_reason"]) == ("paused", "dropped")
    assert conn.execute(due).fetchall() == []


def test_a_version_approved_mid_step_that_drops_the_channel_keeps_it_off(conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    """The review's Important 2: the owner approves a version without Google Search while its create runs; the enable
    reads the version approved last and never turns the ads on -- the row stays `launching`, and the next step pauses
    it (`dropped`). No hour of spend on a channel the owner removed."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    v2 = add_plan_by_hand(conn, newsbreak_only(), status="draft")
    create = gads._create

    def create_while_the_owner_drops_google(body):
        answer = create(body)
        with db.connect(TEST_DB_URL) as other:  # the owner's approval of v2, in its own request
            other.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
            other.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (v2,))
        return answer

    gads._create = create_while_the_owner_drops_google
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.kinds() == ["lookup", "create"] and out["state"] == "launching"  # never turned on
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)  # the next step
    assert (out["state"], row(conn)["paused_reason"]) == ("paused", "dropped")
    assert gads.kinds()[2:] == ["pause"] and gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"


# ---------------------------------------------------------------- the tools and /connect take a channel


def test_the_pause_tool_takes_a_channel_or_all(client, conn, gads, live, nb_live):  # noqa: F811 - the fixture shadows the imported name
    key, plan_id = seed_plan(conn, two_channels(), status="draft")
    approve(client, conn, key, plan_id)
    res = call_tool(client, "pause", {"channel": "newsbreak", "idempotency_key": "p1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"]["state"] == "paused" and list(
        res["structuredContent"]["data"]["channels"]
    ) == [NB]
    assert row(conn)["state"] == "live" and nb_row(conn)["state"] == "paused"
    res = call_tool(client, "pause", {"idempotency_key": "p2"}, key=key)  # "all" by default
    assert not res["isError"] and row(conn)["state"] == "paused"
    res = call_tool(client, "pause", {"channel": "myspace", "idempotency_key": "p3"}, key=key)
    assert res["isError"] and tool_text(res) == "unknown channel"
    res = call_tool(client, "resume", {"channel": "google_search", "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert row(conn)["state"] == "live" and nb_row(conn)["state"] == "paused"
    res = call_tool(client, "resume", {"channel": "google_search", "idempotency_key": "r2"}, key=key)
    assert res["isError"] and tool_text(res) == ads_run.NOT_PAUSED


def test_resume_never_lifts_a_pause_that_is_not_the_owners(client, conn, gads, live):  # noqa: F811 - the fixture shadows the imported name
    key, _v1 = launched(conn, gads)
    ads_run.pause_channels(conn, config.load(), BIZ, by="job:spend-guard", reason="no_funds")
    for arguments in ({"idempotency_key": "r1"}, {"channel": "google_search", "idempotency_key": "r2"}):
        res = call_tool(client, "resume", arguments, key=key)
        assert res["isError"] and tool_text(res) == ads_run.PAUSED_BY_ADMIRY and plain(tool_text(res))
    assert row(conn)["state"] == "paused"


def test_the_pause_button_takes_a_channel(client, conn, gads, live, nb_live, google):  # noqa: F811 - the fixture shadows the imported name
    from agent.tests.test_web import csrf_of, sign_in

    key, plan_id = seed_plan(conn, two_channels(), status="draft")
    approve(client, conn, key, plan_id)
    sign_in(client, "/connect")
    csrf = csrf_of(client)
    data = {"business_id": BIZ, "csrf": csrf, "channel": "newsbreak"}
    assert client.post("/connect/ads/pause", data=data, follow_redirects=False).status_code == 303
    assert (row(conn)["state"], nb_row(conn)["state"]) == ("live", "paused")
    data = {"business_id": BIZ, "csrf": csrf, "channel": "myspace"}
    r = client.post("/connect/ads/pause", data=data)
    assert r.status_code == 409 and "unknown channel" in r.text


def test_pausing_with_nothing_to_pause_says_so(client, conn):
    key, _plan_id = seed(conn)
    res = call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    assert res["isError"] and tool_text(res) == ads_run.NO_ADS
    res = call_tool(client, "pause", {"channel": "newsbreak", "idempotency_key": "p2"}, key=key)
    assert res["isError"] and tool_text(res) == ads_run.NOT_BUILT.format(label="NewsBreak")


def test_a_newsbreak_row_without_words_newsbreak_can_take_fails_with_its_own_sentence(conn, nb_live, pictures):
    """M1 Task 5a (the M0 stub's refusal is gone): a NewsBreak row queued with no ad words its build can use fails
    with NewsBreak's own sentence -- the request itself would be refused again, so it is not retried -- and nothing
    reaches NewsBreak. Its picture is one Admiry's set has (since M1 Task 7 fix round 1 a picture that left the set
    holds the row before any build)."""
    secs = newsbreak_only()  # an entry with an amount and no ad lines
    secs["creative"] = {**secs["creative"], "approved_image": {NB: "home_services/1.jpg"}}
    _key, plan_id = seed_plan(conn, secs)
    funds(conn)
    ads_run.queue(conn, BIZ, NB, plan_id, ready=True)
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"], out["ops"]) == ("failed", nb_adapter.TOO_LITTLE_COPY, 0)
    assert nb_live.requests == [] and plain(out["say"])


def test_the_new_owner_sentences_read_plainly():
    templates = (
        ads_run.REPORT_ONLY,
        ads_run.UNDER_FLOOR,
        ads_run.NO_FUNDS,
        ads_run.MODE_SOON,
        ads_run.NOT_READY,
        ads_run.NOT_BUILT,
        ads_run.SWITCHED_OFF,
        ads_run.PAGE_UNCHECKED,
        ads_run.OFF_CHANNEL,
        ads_run.OFF_OLD_ADDRESS,  # M0 Task 9: the page's sentences, per channel
        ads_run.STOPPING_ADS,  # the M1 follow-up's fix round 2 (G3): a refused row whose stop is owed
        base.MANAGED_NO_FEE,  # M1 follow-up 2 (W2): the statement's billing sentence for every managed channel
    )
    for template in templates:
        assert plain(template.format(label="NewsBreak", floor="$300")), template
    assert plain(base.MANAGED_NO_FEE.format(label="Google Search"))
    assert plain(ads_run.ADDRESS_RETRY.format(labels=ads_run.labels([GOOGLE, NB])))
    for sentence in (ads_run.NO_ADS, ads_run.PAUSED_BY_ADMIRY, ads_run.MOVED, ads_run.LAUNCH_LATER, ads_run.OFF):
        assert plain(sentence), sentence
    assert plain(ads_run.SWITCHING_OFF) and plain(nb_adapter.CONNECT)  # W5's badge, W3's step
    for reason, words in ads_run.PAUSE_WORDS.items():  # "paused by Admiry: <words>" on every card and status line
        assert plain(words), reason
    for reason, advice in ads_run.PAUSE_ADVICE.items():  # and what ends a page's pause (M0 Task 9 D3)
        assert plain(advice), reason
    # spec §7.3: the jargon check knows every channel's words too
    assert not plain("the ad set is paused") and not plain("add the Pixel to your page")
    for words in (ads_run._PLATFORM, google.WORDS, registry.adapter(NB).words):
        # a channel whose ads do not follow the page (NewsBreak until M1) has no page sentences: None
        assert all(plain(s) for s in vars(words).values() if s is not None), words
    assert google.WORDS.refused_address and google.WORDS.paused_elsewhere and google.WORDS.retrying
