"""The `live_paused` execution mode (spec 2026-11-04-muse-verified-creatives-google-ads N4, plan Task 8): Google's
real create mutate, everything built paused, and no path ever turns a campaign on -- not the launch step, not a
resume, not the hourly ads-launch. Allowlisted businesses only, as every launch is."""

from __future__ import annotations

import functools

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, config, tick
from agent.channels import registry
from agent.channels.google_search import adapter as google
from agent.tests.conftest import TEST_DB_URL
from agent.tests.test_ads_run import BIZ, GOOGLE, REAL_CLIENT, FakeGoogle, row, seed

DEMO = "Demo mode: these ads were built paused in Google Ads and are not turned on."
CLAUSE = "built in Google Ads, paused — demo mode, nothing spends"


@pytest.fixture
def gads(monkeypatch, ads_api):
    """test_ads_run's stateful fake of Google Ads, for every client the Google Search adapter makes."""
    fake = FakeGoogle()
    monkeypatch.setattr(google, "client", functools.partial(REAL_CLIENT, transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def live_paused(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live_paused")
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "owner@example.com")  # seed()'s owner


def _queued(conn) -> str:
    """A business ready to launch with its Google Search row queued (what an approval leaves); its plan id."""
    _key, plan_id = seed(conn)
    ads_run.queue(conn, BIZ, GOOGLE, plan_id, ready=True)
    return plan_id


def _never(*_args, **_kwargs):
    raise AssertionError("live_paused turned a campaign on")


def test_config_accepts_live_paused(live_paused, monkeypatch):
    assert "live_paused" in config.ADS_EXECUTION_MODES
    assert registry.exec_mode(config.load(), GOOGLE) == "live_paused"
    monkeypatch.delenv("AGENT_EXEC_GOOGLE_SEARCH")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live_paused")  # the old variable, Google Search's alias
    assert registry.exec_mode(config.load(), GOOGLE) == "live_paused"


def test_live_paused_is_for_google_search_and_the_allowlist_only(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live_paused")
    with pytest.raises(ValueError, match="AGENT_EXEC_NEWSBREAK"):
        config.load()
    monkeypatch.delenv("AGENT_EXEC_NEWSBREAK")
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live_paused")
    monkeypatch.delenv("AGENT_LAUNCH_ALLOWLIST", raising=False)
    with pytest.raises(ValueError, match="AGENT_LAUNCH_ALLOWLIST"):
        config.load()


def test_queued_row_creates(conn, live_paused):
    _queued(conn)
    settings = config.load()
    assert ads_run._work(settings, row(conn), ads_run.approved(conn, BIZ)) == "create"
    found = conn.execute(f"SELECT c.business_id FROM channel_campaign c WHERE {ads_run.due(settings, GOOGLE)}")
    assert [r["business_id"] for r in found.fetchall()] == [BIZ]


def test_create_skips_enable(conn, gads, live_paused, monkeypatch):
    plan_id = _queued(conn)
    monkeypatch.setattr(ads_run, "_enable", _never)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    assert gads.kinds() == ["lookup", "create"]  # the real create, and nothing after it
    assert gads.bodies("create")[0]["validateOnly"] is False
    assert [c["status"] for c in gads.campaigns.values()] == ["PAUSED"]
    r = row(conn)
    assert (r["state"], r["plan_id"], r["launched_at"], r["last_error"]) == ("launching", plan_id, None, None)
    assert r["resources"]["campaign"] == gads.built["campaign"] and r["resources"]["demo_paused"] is True
    assert ads_run.state_clause(r) == CLAUSE
    assert ads_run.execution_line([r]) == ads_run.DEMO_BADGE
    assert out["state"] == "launching" and out["say"].startswith(f"Google Search: {CLAUSE}")
    note = conn.execute(
        "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()["note"]
    assert "turn on" not in note and "demo mode" in note


def test_launching_row_never_enables(conn, gads, live_paused, monkeypatch):
    _queued(conn)
    settings = config.load()
    ads_run.launch(conn, settings, BIZ, GOOGLE)  # built, paused
    monkeypatch.setattr(ads_run, "_enable", _never)
    monkeypatch.setattr(registry.adapter(GOOGLE), "enable", _never)
    sent, plan = len(gads.sent), ads_run.approved(conn, BIZ)

    assert ads_run._work(settings, row(conn), plan) is None
    assert ads_run.launch(conn, settings, BIZ, GOOGLE)["ops"] == 0
    assert ads_run.launch_now(BIZ)["channels"][GOOGLE]["state"] == "launching"
    swept = tick.run("ads-launch", url=TEST_DB_URL)  # the hourly job visits the row and has no step for it
    assert (swept["checked"], swept["ops"], swept["states"]) == (1, 0, {"launching": 1})
    assert len(gads.sent) == sent and row(conn)["state"] == "launching"

    # a refused row's owed stop is still sent: stopping spend is always allowed
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = %s",
        (Jsonb({"stop_owed": {"sentence": "Google did not take your ads", "plan_id": plan["id"]}}), BIZ),
    )
    assert ads_run._work(settings, row(conn), plan) == "stop"
    assert tick.run("ads-launch", url=TEST_DB_URL)["ops"] == 1
    assert gads.kinds()[sent:] == ["pause"]


def test_resume_refused_under_live_paused(conn, gads, live_paused, monkeypatch):
    _queued(conn)
    settings = config.load()
    ads_run.launch(conn, settings, BIZ, GOOGLE)
    ads_run.pause(conn, settings, BIZ, GOOGLE, by="owner")
    assert gads.kinds()[-1] == "pause"  # a pause is sent under live_paused too: stopping spend never waits
    assert row(conn)["state"] == "paused" and "pause_owed" not in row(conn)["resources"]
    monkeypatch.setattr(ads_run, "_enable", _never)
    monkeypatch.setattr(registry.adapter(GOOGLE), "enable", _never)
    sent = len(gads.sent)

    with pytest.raises(ToolError) as e:
        ads_run.resume(conn, settings, BIZ, GOOGLE)
    assert str(e.value) == DEMO == ads_run.DEMO_PAUSED
    with pytest.raises(ToolError) as e:
        ads_run.resume_by_owner(conn, settings, BIZ, by="owner")
    assert str(e.value) == DEMO
    assert row(conn)["state"] == "paused" and len(gads.sent) == sent


def test_off_list_owner_is_off(conn, gads, live_paused, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    _queued(conn)
    settings = config.load()
    assert registry.exec_mode(settings, GOOGLE) == "live_paused"
    assert registry.exec_mode_for(conn, settings, BIZ, GOOGLE) == "off"
    out = ads_run.launch(conn, settings, BIZ, GOOGLE)
    assert out["ops"] == 0 and row(conn)["state"] == "queued" and gads.requests == []


def test_every_wall_behind_the_routing_refuses_to_turn_ads_on(conn, gads, live_paused):
    """The enable step itself and the Google adapter's two ON calls refuse under live_paused, whoever calls them: the
    routing above them (`_work`, `_create`, `resume`) never does, and these are the walls behind it."""
    _queued(conn)
    settings = config.load()
    ads_run.launch(conn, settings, BIZ, GOOGLE)
    sent, r, plan = len(gads.sent), row(conn), ads_run.approved(conn, BIZ)
    account = ads_run._account(conn, BIZ, GOOGLE)
    adapter = registry.adapter(GOOGLE)
    with adapter.client() as http:
        ads_run._enable(conn, settings, BIZ, GOOGLE, account, plan, http, None)
        with pytest.raises(ToolError, match="Demo mode"):
            adapter.enable(conn, settings, account, plan, r["resources"], http=http)
        for validate_only in (False, True):
            with pytest.raises(ToolError, match="Demo mode"):
                adapter.set_paused(
                    conn, settings, account, r["resources"], False, http=http, validate_only=validate_only
                )
        adapter.set_paused(conn, settings, account, r["resources"], True, http=http)  # a stop is always sent
    assert gads.kinds()[sent:] == ["pause"] and row(conn)["state"] == "launching"
    assert [c["status"] for c in gads.campaigns.values()] == ["PAUSED"]
