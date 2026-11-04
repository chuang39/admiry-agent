"""The Analyst's run (capability layer K2, spec 2026-10-07 §4.1, §5.1; plan R4-R7, R12, R13): one insight per business
and local day, built when the day is due, guarded, graded by the Critic, written again at most once; nothing applied;
owners read it only with AGENT_ROLES on and the Critic's deliver."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from agent import ads_run, config, db, digest_audience, digest_zone, models
from agent.roles import analyst, base
from agent.tests import digest_fixtures as df
from agent.tests.conftest import TEST_DB_URL
from agent.tests.fixtures import insight_fixture, insight_review_fixture

DAY = df.ANALYST_DAY
BUSY = "the assistant is busy; try again in a minute"


def _jobs(conn) -> list[dict]:
    return conn.execute(
        "SELECT business_id, args_redacted AS args FROM agent_action WHERE tool = 'job.analyst'"
    ).fetchall()


def test_one_insight_with_its_review(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    assert (row["status"], row["verdict"], row["score"], row["rounds"], row["calls"]) == ("done", "deliver", 1.0, 1, 2)
    assert (row["mode"], row["dropped"], row["plan_id"]) == ("on", None, row["metrics"]["plan_id"])
    assert row["insight"]["summary"] == insight_fixture().summary and row["insight"]["summary_dropped"] is None
    assert gen.insight_calls[0]["feedback"] is None
    assert gen.insight_calls[0]["material"]["day"] == "Monday, October 5"
    [job] = _jobs(conn)
    assert job["business_id"] is None and job["args"]["business"] == "biz_1" and job["args"]["status"] == "done"


def test_the_stored_row_is_read_again_without_a_call(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(monkeypatch)
    first = df.analyst_build(conn)
    assert df.analyst_build(conn)["id"] == first["id"] and len(gen.insight_calls) == 1


def test_roles_off_builds_nothing(conn, analyst_biz, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "off")
    gen = df.fake_llm(monkeypatch)
    assert df.analyst_build(conn) is None and gen.insight_calls == []
    assert conn.execute("SELECT count(*) AS n FROM insight").fetchone()["n"] == 0


def test_validate_builds_for_the_team(conn, analyst_biz, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    assert (row["mode"], row["status"]) == ("validate", "done")
    assert analyst.owner_view(config.load(), row) is None


def test_a_revision_runs_one_more_round_with_what_was_wrong(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(
        monkeypatch, insight_reviews=[insight_review_fixture(low={"priority": 2}), insight_review_fixture()]
    )
    row = df.analyst_build(conn)
    assert (row["rounds"], row["calls"], row["verdict"], row["dropped"]) == (2, 4, "deliver", None)
    assert "priority: priority note" in gen.insight_calls[1]["feedback"]


def test_two_rounds_at_most(conn, analyst_biz, monkeypatch):
    df.fake_llm(monkeypatch, insight_reviews=[insight_review_fixture(low={"priority": 2})])
    row = df.analyst_build(conn)
    assert (row["rounds"], row["calls"], row["verdict"], row["dropped"]) == (2, 4, base.REVISE, base.REVISE)
    assert analyst.owner_view(config.load(), row) is None


def test_a_candidate_the_material_does_not_hold_is_dropped_and_fed_back(conn, analyst_biz, monkeypatch):
    made_up = insight_fixture(
        recommendations=[models.InsightPick(candidate_id="teleport", text="Move the shop.", confidence="high")]
    )
    gen = df.fake_llm(
        monkeypatch,
        insights=[made_up],
        insight_reviews=[insight_review_fixture(low={"fit": 2}), insight_review_fixture()],
    )
    row = df.analyst_build(conn)
    assert row["insight"]["recommendations"] == []
    assert "recommendation 'teleport': it is not one of the candidates" in gen.insight_calls[1]["feedback"]


def test_an_action_comes_from_its_candidate_never_the_model(conn, analyst_biz, monkeypatch):
    df.seed_lead(conn, "biz_1", datetime(2026, 10, 2, 18, tzinfo=UTC))  # still new after 48 hours: leads_unmarked
    pick = models.InsightPick(candidate_id="mark_leads", text="Say which callers booked.", confidence="medium")
    df.fake_llm(monkeypatch, insights=[insight_fixture(recommendations=[pick])])
    [rec] = df.analyst_build(conn)["insight"]["recommendations"]
    assert (rec["tool"], rec["args"], rec["label"]) == (
        "get_leads",
        {"awaiting_outcome": True},
        "Say what the leads became",
    )
    assert (rec["id"], rec["text"], rec["confidence"], rec["delta_usd"], rec["dropped"]) == (
        "mark_leads",
        "Say which callers booked.",
        "medium",
        None,
        None,
    )


def test_a_summary_the_guard_drops_is_written_again(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(monkeypatch, insights=[insight_fixture(summary="$9,999 spent yesterday."), insight_fixture()])
    row = df.analyst_build(conn)
    assert row["rounds"] == 2 and row["insight"]["summary_dropped"] is None
    assert "summary: it stated a number the material does not hold" in gen.insight_calls[1]["feedback"]


def test_the_guard_drops_only_the_sentence_that_fails(conn, analyst_biz, monkeypatch):
    df.fake_llm(monkeypatch, insights=[insight_fixture(summary="$9,999 spent yesterday.")])
    row = df.analyst_build(conn)
    view = analyst.owner_view(config.load(), row)
    assert row["insight"]["summary_dropped"] == "number"
    assert view["summary"] is None and view["findings"] == ["Google Search brought 2 calls."]


def test_a_busy_model_leaves_a_failed_row_for_the_day(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(monkeypatch, insight_error=BUSY)
    row = df.analyst_build(conn)
    assert (row["status"], row["dropped"], row["calls"]) == ("failed", "llm", 1)
    assert df.analyst_build(conn)["id"] == row["id"] and len(gen.insight_calls) == 1


def test_without_the_critic_the_words_stay_with_the_team(conn, analyst_biz, monkeypatch):
    df.fake_llm(monkeypatch, insight_review_error=BUSY)
    row = df.analyst_build(conn)
    assert (row["status"], row["verdict"], row["dropped"]) == ("done", None, "review")
    assert analyst.owner_view(config.load(), row) is None
    assert analyst.team_view(row)["insight"]["summary"] == insight_fixture().summary


def test_the_day_budget_skips_without_a_call(conn, analyst_biz, monkeypatch):
    monkeypatch.setenv("AGENT_ANALYST_MAX_PER_DAY", "1")
    gen = df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    assert (row["status"], row["dropped"], gen.insight_calls) == ("skipped", "budget", [])


def test_a_crashed_building_row_is_built_again(conn, analyst_biz, monkeypatch):
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status) VALUES ('in_x', 'biz_1', %s, 'on', 'building')",
        (DAY,),
    )
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    assert (row["id"], row["status"]) == ("in_x", "done")


def test_owners_read_it_only_with_the_roles_on_and_the_critics_deliver(conn, analyst_biz, monkeypatch):
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    monkeypatch.setenv("AGENT_ROLES", "validate")
    assert analyst.owner_view(config.load(), row) is None
    assert analyst.owner_view(config.load(), row, preview=True)["summary"] == insight_fixture().summary
    monkeypatch.setenv("AGENT_ROLES", "on")
    view = analyst.owner_view(config.load(), row)
    assert (view["day"], view["summary"], view["recommendations"]) == ("2026-10-05", insight_fixture().summary, [])


def test_a_look_built_in_validate_stays_with_the_team_after_the_flip(conn, analyst_biz, monkeypatch):
    """Final review I1 (R5): turning AGENT_ROLES on shows owners the looks built from then on, never the ones built for
    the team while it was validate -- in the mail, get_insights, opportunities or a reply."""
    monkeypatch.setenv("AGENT_ROLES", "validate")
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    settings = config.load()
    assert (row["mode"], row["verdict"]) == ("validate", "deliver")
    assert analyst.owner_view(settings, row) is None
    assert analyst.owner_view(settings, row, preview=True) is not None  # the operator's preview still reads it
    assert analyst.opportunities(conn, settings, "biz_1") == []


def test_a_summary_the_guard_dropped_is_no_material_the_next_day(conn, analyst_biz, monkeypatch):
    """Final review I2 (R14): yesterday's summary the number guard dropped never becomes today's material, where its
    number would pass today's guard."""
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status, insight) "
        "VALUES ('in_p', 'biz_1', '2026-10-04', 'on', 'done', %s)",
        (Jsonb({"summary": "$9,999 spent yesterday.", "summary_dropped": "number", "recommendations": []}),),
    )
    df.fake_llm(monkeypatch, insights=[insight_fixture(summary="You have spent $9,999 on your ads.")])
    row = df.analyst_build(conn)
    assert row["metrics"]["previous"]["summary"] is None
    assert row["insight"]["summary_dropped"] == "number"
    view = analyst.owner_view(config.load(), row)
    assert view is None or view["summary"] is None


def test_an_error_building_the_look_fails_it_for_the_day(conn, analyst_biz, monkeypatch):
    """Final review I3: an error that is not the model's leaves a failed row (dropped = error), never a building one
    every later hour builds again."""
    calls = []

    def broken(*a, **k):
        calls.append(1)
        raise KeyError("cost_per_lead_usd")

    monkeypatch.setattr(analyst, "metrics", broken)
    df.fake_llm(monkeypatch)
    row = df.analyst_build(conn)
    assert (row["status"], row["dropped"]) == ("failed", "error")
    assert df.analyst_build(conn)["id"] == row["id"] and len(calls) == 1


def _run_at(conn, monkeypatch, when: datetime) -> dict:
    monkeypatch.setattr(ads_run, "now", lambda: when)
    return analyst.analyst_run(conn, 0, 1)


def test_the_job_builds_once_the_day_is_due(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(monkeypatch)
    assert _run_at(conn, monkeypatch, datetime(2026, 10, 6, 14, 2, tzinfo=UTC))["not_due"] == 1  # 07:02 there
    assert _run_at(conn, monkeypatch, df.ANALYST_NOW)["done"] == 1
    assert _run_at(conn, monkeypatch, datetime(2026, 10, 6, 16, 2, tzinfo=UTC))["had"] == 1
    assert _run_at(conn, monkeypatch, datetime(2026, 10, 7, 3, 2, tzinfo=UTC))["not_due"] == 1  # 20:02 there
    assert len(gen.insight_calls) == 1


def test_gated_until_the_figures_are_in_or_eleven(conn, analyst_biz, monkeypatch):
    monkeypatch.setattr(digest_audience, "gate", lambda *a, **k: False)
    df.fake_llm(monkeypatch)
    assert _run_at(conn, monkeypatch, df.ANALYST_NOW)["gated"] == 1
    assert _run_at(conn, monkeypatch, datetime(2026, 10, 6, 18, 2, tzinfo=UTC))["done"] == 1  # 11:02 there


def test_a_business_whose_ads_did_not_run_gets_none(conn, analyst_biz, monkeypatch):
    conn.execute("UPDATE channel_campaign SET state = 'waiting_account'")
    conn.execute("DELETE FROM metric_daily")
    df.fake_llm(monkeypatch)
    assert _run_at(conn, monkeypatch, df.ANALYST_NOW)["quiet"] == 1


def test_a_business_another_run_holds_is_left_alone(conn, analyst_biz, monkeypatch):
    df.fake_llm(monkeypatch)
    with db.connect(TEST_DB_URL) as other, digest_zone.daily_lock(other, "biz_1") as held:
        assert held
        assert _run_at(conn, monkeypatch, df.ANALYST_NOW)["locked"] == 1
    assert conn.execute("SELECT count(*) AS n FROM insight").fetchone()["n"] == 0


def test_thirteen_month_old_insights_go_even_with_the_roles_off(conn, analyst_biz, monkeypatch):
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status) VALUES ('in_old', 'biz_1', '2025-09-01', 'on', 'done')"
    )
    monkeypatch.setenv("AGENT_ROLES", "off")
    out = _run_at(conn, monkeypatch, df.ANALYST_NOW)
    assert (out["purged"], out["mode"], out["considered"]) == (1, "off", 0)


def test_one_failing_business_never_stops_the_run(conn, analyst_biz, monkeypatch):
    df.seed_business(conn, "biz_2", zone="America/Los_Angeles")
    df.seed_plan(conn, "biz_2", {"google_search": 600})
    df.seed_channel(
        conn, "biz_2", synced_at=df.ANALYST_NOW, resources={"campaign": "c", "time_zone": "America/Los_Angeles"}
    )
    real = analyst.inputs

    def broken(conn, settings, business_id, *a, **k):
        if business_id == "biz_1":
            raise RuntimeError("boom")
        return real(conn, settings, business_id, *a, **k)

    monkeypatch.setattr(analyst, "inputs", broken)
    df.fake_llm(monkeypatch)
    out = _run_at(conn, monkeypatch, df.ANALYST_NOW)
    assert out["errors"] == 1 and out["considered"] == 2


def test_rebuild_builds_the_day_again_now(conn, analyst_biz, monkeypatch):
    gen = df.fake_llm(monkeypatch)
    first = df.analyst_build(conn)
    again = analyst.rebuild(conn, config.load(), "biz_1", DAY)
    assert again["id"] != first["id"] and len(gen.insight_calls) == 2


def test_the_job_is_registered():
    from agent import jobs

    assert jobs.JOBS["analyst-run"] is analyst.analyst_run
