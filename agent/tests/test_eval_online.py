"""K5 (spec §7, P11): the online numbers from the tables that exist, for the operator -- aggregates only."""

from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from agent import benchmark, business, ops
from agent.eval import online
from agent.tests import research_fixtures as rf

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def biz(conn, bid: str, days_ago: float) -> None:
    conn.execute("INSERT INTO business (id, created_at) VALUES (%s, %s)", (bid, NOW - timedelta(days=days_ago)))


def plan(conn, bid: str, version: int, days_ago: float, status: str = "draft") -> None:
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status, created_at) VALUES (%s, %s, %s, '{}'::jsonb, %s, %s)",
        (f"pl_{bid}_{version}", bid, version, status, NOW - timedelta(days=days_ago)),
    )


def approve(conn, bid: str, days_ago: float) -> None:
    at = NOW - timedelta(days=days_ago)
    conn.execute(
        "INSERT INTO proposal (id, business_id, class, kind, payload, summary, status, expires_at, resolved_at, "
        "created_at) VALUES (%s, %s, 'spend', 'approve_plan', '{}'::jsonb, 'approve', 'confirmed', %s, %s, %s)",
        (f"pr_{bid}", bid, at + timedelta(days=1), at, at),
    )


def look(conn, bid: str, days_ago: float, *, verdict="deliver", mode="on", recs=()) -> None:
    at = NOW - timedelta(days=days_ago)
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status, insight, verdict, created_at, finished_at) "
        "VALUES (%s, %s, %s, %s, 'done', %s, %s, %s, %s)",
        (f"in_{bid}_{days_ago}", bid, at.date(), mode, Jsonb({"recommendations": list(recs)}), verdict, at, at),
    )


def test_approval_time_to_launch_and_changes_before_approval(conn):
    biz(conn, "b1", 5)
    plan(conn, "b1", 1, 5)
    plan(conn, "b1", 2, 4, "approved")
    approve(conn, "b1", 3)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, launched_at) "
        "VALUES ('b1', 'google_search', 'pl_b1_2', 'live', %s)",
        (NOW - timedelta(days=2),),
    )
    biz(conn, "b2", 2)
    plan(conn, "b2", 1, 2)
    biz(conn, "b3", 60)
    plan(conn, "b3", 1, 60)  # before the window
    m = online.metrics(conn, days=30, now=NOW)
    assert m["approval"] == {"planned": 2, "approved": 1}
    assert m["to_launch_hours"] == [72.0] and m["changes_before_approval"] == [1]


def test_thumbs_and_the_critics_verdicts(conn):
    biz(conn, "b1", 5)
    plan(conn, "b1", 1, 5)
    for n, (rating, reasons) in enumerate((("down", ["budget", "copy"]), ("up", []))):
        conn.execute(
            "INSERT INTO plan_feedback (id, plan_id, business_id, surface, rating, reasons, actor, snapshot, "
            "created_at) VALUES (%s, 'pl_b1_1', 'b1', 'home', %s, %s, %s, '{}'::jsonb, %s)",
            (f"fb_{n}", rating, reasons, f"web:o{n}@example.com", NOW - timedelta(days=1)),
        )
    for n, verdict in enumerate(("deliver", "revise", "fetch_more")):
        conn.execute(
            "INSERT INTO plan_review (id, plan_id, business_id, review, model, verdict, created_at) "
            "VALUES (%s, 'pl_b1_1', 'b1', '{}'::jsonb, 'm', %s, %s)",
            (f"rv_{n}", verdict, NOW - timedelta(days=1)),
        )
    look(conn, "b1", 1, verdict="revise", mode="validate")
    m = online.metrics(conn, days=30, now=NOW)
    assert m["thumbs"] == {"votes": 2, "down": 1, "reasons": {"budget": 1, "copy": 1}}
    assert m["critic"] == {
        "plan": {"deliver": 1, "revise": 1, "fetch_more": 1},
        "insight": {"revise": 1},
        "research": {},
    }


def test_a_suggestion_counts_as_followed_when_its_tool_ran_within_three_days(conn):
    biz(conn, "b1", 10)
    recs = [
        {"tool": "get_billing", "dropped": None},
        {"tool": "pause_campaign", "dropped": None},
        {"tool": "get_leads", "dropped": "too long"},  # never shown
    ]
    look(conn, "b1", 5, recs=recs)
    look(conn, "b1", 4, recs=recs, mode="validate")  # the team's alone: not counted
    for tool, days_ago in (("get_billing", 4), ("pause_campaign", 1)):  # the pause came four days after: too late
        conn.execute(
            "INSERT INTO agent_action (business_id, actor, tool, args_hash, ok, surface_tool, ts) "
            "VALUES ('b1', 'key:k1', %s, 'h', true, %s, %s)",
            (tool, tool, NOW - timedelta(days=days_ago)),
        )
    assert online.metrics(conn, days=30, now=NOW)["suggestions"] == {"shown": 2, "followed": 1}


def test_a_look_whose_three_days_are_not_over_is_not_counted_yet(conn):
    """The follow rate counts only looks whose window has closed: a young look would pull the rate down."""
    biz(conn, "b1", 10)
    recs = [{"tool": "get_billing", "dropped": None}]
    look(conn, "b1", 5, recs=recs)  # its window closed two days ago
    look(conn, "b1", 1, recs=recs)  # two days of its window are left
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, tool, args_hash, ok, surface_tool, ts) "
        "VALUES ('b1', 'key:k1', 'get_billing', 'h', true, 'get_billing', %s)",
        (NOW - timedelta(hours=12),),  # the owner already acted on the young look's advice
    )
    assert online.metrics(conn, days=30, now=NOW)["suggestions"] == {"shown": 1, "followed": 0}
    # finished exactly three days ago, its window has just closed: counted, and the action 12 hours ago was inside it
    look(conn, "b1", 3, recs=recs)
    assert online.metrics(conn, days=30, now=NOW)["suggestions"] == {"shown": 2, "followed": 1}


def channel_row(channel: str, spend: float, leads: int) -> dict:
    """One row of funnel.build's by_channel, as far as the online numbers read it."""
    return {
        "channel": channel,
        "spend_usd": spend,
        "leads": leads,
        "cost_per_lead_usd": round(spend / leads, 2) if leads else None,
    }


def report(*rows: dict) -> dict:
    """funnel.build's answer for a business whose channels are `rows`: the blended funnel and the rows."""
    spend, leads = sum(r["spend_usd"] for r in rows), sum(r["leads"] for r in rows)
    blended = {"spend_usd": spend, "leads": leads, "cost_per_lead_usd": round(spend / leads, 2) if leads else None}
    return {"funnel": blended, "by_channel": list(rows)}


def test_cost_per_lead_against_the_trades_benchmark(conn, monkeypatch):
    benchmark.seed(conn)
    for bid in ("b1", "b2", "b3"):
        biz(conn, bid, 40)
        plan(conn, bid, 1, 40, "approved")
        business.add_knowledge(conn, bid, {"category": "dentists"}, source="owner")
    reports = {
        "b1": report(channel_row("google_search", 300.0, 2)),
        "b2": report(channel_row("google_search", 200.0, 0)),  # spent, no lead
        "b3": report(),  # nothing ran
    }
    monkeypatch.setattr(online.funnel, "build", lambda conn, biz, period: reports[biz])
    cpl = benchmark.lookup(conn, "dentists", "google_search")["cpl_avg"]
    m = online.metrics(conn, days=30, now=NOW)
    assert m["cpl"]["7d"] == {"businesses": 1, "spend_no_leads": 1, "median_ratio": round(150.0 / cpl, 2)}


def test_the_ratio_is_google_searchs_own_cost_per_lead_not_the_blend(conn, monkeypatch):
    benchmark.seed(conn)
    for bid in ("b1", "b2", "b3", "b4"):
        biz(conn, bid, 40)
        plan(conn, bid, 1, 40, "approved")
        business.add_knowledge(conn, bid, {"category": "dentists"}, source="owner")
    reports = {
        # google_search $150 a lead, NewsBreak $10 a lead: the blend ($33.33) is neither
        "b1": report(channel_row("google_search", 300.0, 2), channel_row("newsbreak", 100.0, 10)),
        "b2": report(channel_row("newsbreak", 500.0, 0)),  # no Google Search spend: not in the ratio, not "silent"
        "b3": report(channel_row("newsbreak", 40.0, 4)),  # no Google Search spend: not in the ratio
        "b4": report(channel_row("google_search", 0, 0), channel_row("newsbreak", 60.0, 3)),  # a row with no spend
    }
    monkeypatch.setattr(online.funnel, "build", lambda conn, biz, period: reports[biz])
    cpl = benchmark.lookup(conn, "dentists", "google_search")["cpl_avg"]
    m = online.metrics(conn, days=30, now=NOW)
    assert m["cpl"]["30d"] == {"businesses": 1, "spend_no_leads": 0, "median_ratio": round(150.0 / cpl, 2)}


def test_research_confidence_counts_the_runs_of_the_window(conn):
    """K4's research_run (spec §7: the research confidence distribution and the Critic's revise rate): the runs created
    in the window by confidence -- a delivered high one, a low one the Critic sent back, a run that never finished (no
    confidence, no verdict) -- and nothing older; the Critic's verdicts over the same runs."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    rf.delivered(conn)  # high, deliver
    rf.delivered(conn, dropped="revise")  # low, revise
    rf.delivered(conn, days_ago=40)  # outside the window
    stuck = rf.delivered(conn)
    conn.execute("UPDATE research_run SET status = 'failed', confidence = NULL, verdict = NULL WHERE id = %s", (stuck,))
    now = datetime.now(UTC)  # rf.delivered writes with the database's clock
    m = online.metrics(conn, days=30, now=now)
    assert m["research"] == {"high": 1, "low": 1, "None": 1}
    assert m["critic"]["research"] == {"deliver": 1, "revise": 1}
    out = online.text(m)
    assert "research confidence: None 1, high 1, low 1" in out
    assert "Critic, research runs: 2 judged, revise or fetch_more 1 (50%)" in out


def test_a_database_without_research_run_says_so_and_does_not_fail(conn):
    """A database the agent has not started on since K4 has no research_run: the research confidence has no numbers
    and the line says why, the Critic's research count is empty -- the operator's other numbers are still there. The
    table is dropped inside a transaction that is rolled back, so the shared test database keeps it."""
    with conn.transaction():
        conn.execute("DROP TABLE research_run")
        m = online.metrics(conn, days=30, now=NOW)
        out = online.text(m)
        raise psycopg.Rollback
    assert m["research"] is None and m["critic"]["research"] == {}
    assert "research confidence: no research_run table on this database" in out
    assert "Critic, research runs: 0 judged" in out and "plan approval: 0 of 0" in out


def test_the_operator_reads_them_as_lines(conn, monkeypatch, capsys):
    monkeypatch.setattr(ops.ads_run, "now", lambda: NOW)
    assert ops.main(["eval-online", "--days", "30"]) == 0
    out = capsys.readouterr().out
    for words in (
        "plan approval: 0 of 0",
        "first turn to first launch",
        "thumbs down: 0 of 0",
        "Google Search CPL against the vertical benchmark, 7d",
        "Critic, plan looks: 0 judged",
        "Critic, research runs: 0 judged",
        "research confidence: none in the window",
        "from looks at least 3 days old",
        "followed within 3 days",
    ):
        assert words in out, words


def test_the_lines_name_no_business_and_leave_no_audit_row(conn, monkeypatch, capsys):
    """Operator-only aggregates: nothing a person could be found by, and nothing audited (as `email-stats`)."""
    monkeypatch.setattr(ops.ads_run, "now", lambda: NOW)
    biz(conn, "biz_Zq7Secret", 5)
    plan(conn, "biz_Zq7Secret", 1, 5, "approved")
    approve(conn, "biz_Zq7Secret", 3)
    conn.execute(
        "INSERT INTO plan_feedback (id, plan_id, business_id, surface, rating, reasons, comment, actor, snapshot, "
        "created_at) VALUES ('fb_1', 'pl_biz_Zq7Secret_1', 'biz_Zq7Secret', 'home', 'down', '{budget}', "
        "'my crown moulding shop in Tulsa', 'web:owner.zq7@example.com', '{}'::jsonb, %s)",
        (NOW - timedelta(days=1),),
    )
    look(conn, "biz_Zq7Secret", 4, recs=[{"tool": "get_billing", "dropped": None, "text": "Tulsa crown moulding"}])
    before = conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]
    assert ops.main(["eval-online"]) == 0
    out = capsys.readouterr().out
    assert "plan approval: 1 of 1" in out and "thumbs down: 1 of 1" in out and "budget 1" in out
    for private in ("Zq7", "zq7", "example.com", "Tulsa", "crown", "pl_", "in_"):
        assert private not in out, private
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == before


def test_a_business_whose_report_fails_is_left_out_not_fatal(conn, monkeypatch):
    for bid in ("b1", "b2"):
        biz(conn, bid, 40)
        plan(conn, bid, 1, 40, "approved")

    def build(conn, biz, period):
        if biz == "b1":
            raise RuntimeError("no report")
        return report(channel_row("google_search", 50.0, 0))

    monkeypatch.setattr(online.funnel, "build", build)
    m = online.metrics(conn, days=30, now=NOW)
    assert m["cpl"]["30d"] == {"businesses": 0, "spend_no_leads": 1, "median_ratio": None}
