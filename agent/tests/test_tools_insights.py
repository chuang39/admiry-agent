"""What an owner's assistant reads of the Analyst's daily look (capability layer K2, spec 2026-10-07 §5.1 step 5;
plan R2, R5, R10; Task 14): get_insights, the insights resource and get_marketing_state's opportunities -- nothing of
it before AGENT_ROLES is on and the Critic delivered, everything of it for a platform key -- and the mailed report
beside it, never an operator's preview."""

import json
from datetime import UTC, datetime

from agent import ads_run, config, models, state, tools_insights
from agent.surface import told
from agent.tests import digest_fixtures as df
from agent.tests.conftest import call_tool, rpc, tool_text
from agent.tests.fixtures import business_with_key, insight_fixture, insight_review_fixture

PICK = models.InsightPick(candidate_id="mark_leads", text="Tell Admiry which callers booked.", confidence="medium")
ACTION = {"tool": "get_leads", "args": {"awaiting_outcome": True}, "label": "Say what the leads became"}


def _call(client, key, name, **args) -> dict:
    res = call_tool(client, name, args, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def _built(conn, monkeypatch, **kw) -> dict:
    df.seed_lead(conn, "biz_1", datetime(2026, 10, 2, 18, tzinfo=UTC))  # leads_unmarked -> the mark_leads candidate
    df.fake_llm(monkeypatch, insights=[insight_fixture(recommendations=[PICK])], **kw)
    return df.analyst_build(conn)


def _sent_digest(conn, mode: str = "on", body: dict | None = None) -> dict:
    body = body or {"sentence": "Yesterday: $23.10 spent, 2 calls, 1 form.", "rows": [], "items": []}
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status, body) "
        "VALUES (%s, 'biz_1', '2026-10-05', 'daily', 'America/Los_Angeles', %s, 'sent', %s)",
        (f"dg_{mode}", mode, json.dumps(body)),
    )
    return body


def test_the_owner_reads_the_latest_look_and_its_actions(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch)
    out = _call(client, business_with_key(conn), "get_insights")
    assert out["say"] == insight_fixture().summary
    assert set(out["data"]) == set(tools_insights.KEYS)
    [rec] = out["data"]["insight"]["recommendations"]
    assert rec["action"] == ACTION and rec["text"] == PICK.text
    assert out["next"] == {
        "kind": "actions",
        "actions": [{"id": "mark_leads", "label": ACTION["label"], "tool": "get_leads", "args": ACTION["args"]}],
    }


def test_validate_keeps_the_look_for_admirys_team(client, conn, analyst_biz, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    _built(conn, monkeypatch)
    owner = business_with_key(conn)
    out = _call(client, owner, "get_insights")
    assert out["data"]["insight"] is None and out["say"] == tools_insights.NOT_ON and out["next"] == {"kind": "none"}
    assert _call(client, owner, "marketing_state")["data"]["opportunities"] == []
    platform = business_with_key(conn, cls="platform")
    conn.execute("UPDATE api_key SET business_id = 'biz_1' WHERE class = 'platform'")
    team = _call(client, platform, "get_insights")["data"]["team"]
    assert team[0]["insight"]["summary"] == insight_fixture().summary and team[0]["mode"] == "validate"


def test_a_revised_look_is_not_read_by_the_owner(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch, insight_reviews=[insight_review_fixture(low={"priority": 2})])
    out = _call(client, business_with_key(conn), "get_insights")
    assert out["data"]["insight"] is None and out["say"] == tools_insights.FIRST


def test_opportunities_carry_the_latest_looks_suggestions(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch)
    [opp] = _call(client, business_with_key(conn), "marketing_state")["data"]["opportunities"]
    assert opp["action"] == ACTION and opp["day"] == "2026-10-05"


def test_an_old_look_is_no_opportunity(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch)
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 10, 10, 15, tzinfo=UTC))
    assert _call(client, business_with_key(conn), "marketing_state")["data"]["opportunities"] == []


def test_a_bad_day_is_refused(client, conn, analyst_biz):
    res = call_tool(client, "get_insights", {"day": "yesterday"}, key=business_with_key(conn))
    assert res["isError"] and tools_insights.BAD_DAY in tool_text(res)


def test_one_day_and_no_look_that_day(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch)
    key = business_with_key(conn)
    assert _call(client, key, "get_insights", day="2026-10-05")["data"]["insight"]["day"] == "2026-10-05"
    out = _call(client, key, "get_insights", day="2026-09-01")
    assert out["data"]["insight"] is None and out["say"] == tools_insights.NOT_THAT_DAY


def test_no_running_ads_says_so(client, conn, analyst_biz):
    conn.execute("UPDATE channel_campaign SET state = 'paused', paused_reason = NULL")
    out = _call(client, business_with_key(conn), "get_insights")
    assert out["say"] == tools_insights.NO_ADS


def test_the_insights_resource_is_the_tools_answer(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch)
    key = business_with_key(conn)
    text = rpc(client, "resources/read", {"uri": told.INSIGHTS_RESOURCE}, key=key)["result"]["contents"][0]["text"]
    assert json.loads(text) == _call(client, key, "get_insights")["data"]


def test_get_insights_returns_the_mailed_report(client, conn, analyst_biz, monkeypatch):
    _built(conn, monkeypatch)
    _sent_digest(conn, "operator", {"sentence": "the operator's preview"})
    key = business_with_key(conn)
    assert _call(client, key, "get_insights")["data"]["report"] is None  # an operator's preview is no owner's report
    conn.execute("DELETE FROM digest")
    body = _sent_digest(conn, "on")
    assert _call(client, key, "get_insights", day="2026-10-05")["data"]["report"] == body


def test_operator_digest_is_invisible_to_the_owner(client, conn, analyst_biz, digest_env, monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    _sent_digest(conn, "operator", {"sentence": "the operator's preview"})
    key = business_with_key(conn)
    out = _call(client, key, "get_insights")
    assert out["data"]["report"] is None and out["data"]["next_report_at"] is None
    assert state.build(conn, config.load(), "biz_1").report is None
    entries = _call(client, key, "get_activity_log")["data"]["entries"]
    assert not any("report" in e["sentence"].lower() for e in entries)


def test_the_state_carries_the_report_only_when_the_digest_is_on(conn, analyst_biz, digest_env, monkeypatch):
    body = _sent_digest(conn, "on")
    st = state.build(conn, config.load(), "biz_1")
    assert st.report == body and st.next_report_at.isoformat() == "2026-10-07"
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    st = state.build(conn, config.load(), "biz_1")
    assert st.report is None and st.next_report_at is None


def test_every_say_passes_owner_words():
    from agent import copy

    for s in (tools_insights.NOT_ON, tools_insights.NO_ADS, tools_insights.NOT_THAT_DAY, tools_insights.FIRST):
        assert copy.jargon(s) is None, s
