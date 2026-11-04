"""When the Researcher runs (founder ruling R-K4; plan decisions D1-D3; the final review's M1 and M3): after a
signed-in business's first plan, then not within 30 days of research that stands for it -- a run done that the switch
can stand for (with the roles on, only one built on) or one that failed for its material -- and again only when the plan
changed after them; a run that failed otherwise holds the sweep RETRY_DAYS, and RETRY_LIMIT of them hold it for the 30
days; a business never researched before one due again; on the owner's ask once a day, never while one waits or runs;
on the operator's at once; nothing with the roles off."""

from datetime import UTC, datetime, timedelta

import pytest

from agent import audit, config
from agent.roles import queue, research_material, researcher, runner
from agent.tests import research_fixtures as rf


@pytest.fixture
def world(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    return rf.fake_llm(monkeypatch)


def _claim(conn) -> dict:
    """The one waiting research job, taken as research-run takes it: running now."""
    [job] = queue.claim(conn, 0, 1, roles=(researcher.ROLE,), limit=1)
    return job


def _spend_the_day(conn, biz: str = rf.BIZ) -> None:
    """The business's background model calls of the day used up: a research run needs room for its four."""
    for _ in range(runner.ROLE_RUNS_PER_DAY):
        audit.record_job(
            conn, job="research-run", tool="job.researcher", business_id=None, args={"business": biz}, result={}
        )


def _job(conn, job_id: str) -> dict:
    return conn.execute("SELECT * FROM agent_job WHERE id = %s", (job_id,)).fetchone()


def _due(conn, *args, **kw) -> list[str]:
    """The businesses due with the roles on validate, as the world fixture has them."""
    return researcher.due(conn, "validate", *args, **kw)


def _aged(conn, run_id: str, days: float) -> None:
    """The run made and finished `days` ago."""
    conn.execute(
        "UPDATE research_run SET created_at = now() - make_interval(secs => %s), "
        "finished_at = now() - make_interval(secs => %s) WHERE id = %s",
        (days * 86400, days * 86400, run_id),
    )


def test_a_signed_in_business_with_its_first_plan_gets_research_once(conn, world):
    rf.seed(conn)
    out = runner.research_run(conn, 0, 1)
    assert (out["enqueued"], out["done"]) == (1, 1)
    row = rf.run_row(conn)
    assert row["trigger"] == "auto" and row["brief"]["topics"] == list(research_material.TOPICS)
    assert runner.research_run(conn, 0, 1)["enqueued"] == 0 and len(world.research_calls) == 1


def test_the_sweep_queues_one_waiting_job_for_each_business_due_and_never_a_second(conn, world):
    rf.seed(conn)
    rf.seed(conn, "biz_2")
    assert researcher.sweep(conn, "validate", 0, 1) == 2
    rows = conn.execute("SELECT business_id, role, kind, status, brief FROM agent_job ORDER BY business_id").fetchall()
    assert [(r["business_id"], r["role"], r["kind"], r["status"]) for r in rows] == [
        (rf.BIZ, "researcher", "business", "queued"),
        ("biz_2", "researcher", "business", "queued"),
    ]
    assert rows[0]["brief"] == {
        "trigger": "auto",
        "topics": list(research_material.TOPICS),
        "note": None,
        "by": "job:research-run",
    }
    assert researcher.sweep(conn, "validate", 0, 1) == 0  # a job waiting is not queued twice


def test_a_draft_business_gets_none(conn, world):
    rf.seed(conn, claim_state="draft")
    assert _due(conn) == [] and runner.research_run(conn, 0, 1)["enqueued"] == 0


def test_a_business_that_is_not_active_or_that_nothing_is_known_of_gets_none(conn, world):
    for biz in (rf.BIZ, "biz_2", "biz_3"):
        rf.seed(conn, biz)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_2'")
    conn.execute("DELETE FROM knowledge WHERE business_id = 'biz_3'")
    assert _due(conn) == [rf.BIZ]


def test_no_plan_within_thirty_days_no_research(conn, world):
    rf.seed(conn, plan_at=datetime.now(UTC) - timedelta(days=31))
    assert _due(conn) == []


def test_research_comes_again_only_after_thirty_days_and_a_changed_plan(conn, world):
    rf.seed(conn, plan_at=datetime.now(UTC) - timedelta(days=40))
    rf.delivered(conn, days_ago=31)
    assert _due(conn) == []  # its plan has not changed within the 30 days
    rf.seed(conn)  # a new plan version today (the business and its knowledge are kept)
    assert _due(conn) == [rf.BIZ]
    rf.delivered(conn, days_ago=29)
    assert _due(conn) == []


def test_research_waiting_or_running_is_not_queued_twice(conn, world):
    rf.seed(conn)
    rf.job(conn, trigger="owner")
    assert _due(conn) == []
    _claim(conn)  # the job runs now
    assert _due(conn) == []


def test_a_business_whose_research_was_skipped_today_is_not_queued_again_until_tomorrow(conn, world):
    """K4 Task 4 R1 (pre-flight F26): a skipped job leaves no research run, so without this the next hourly sweep
    would queue the same business again, and again."""
    rf.seed(conn)
    _spend_the_day(conn)  # no room for a run: the job is skipped, and no run is written
    first = runner.research_run(conn, 0, 1)
    assert (first["enqueued"], first["skipped"], first["done"]) == (1, 1, 0) and rf.run_row(conn) is None
    assert _due(conn) == [] and runner.research_run(conn, 0, 1)["enqueued"] == 0
    conn.execute("UPDATE agent_job SET finished_at = now() - interval '2 days', updated_at = now() - interval '2 days'")
    assert _due(conn) == [rf.BIZ]  # tomorrow the day's calls are there again


def test_the_retry_constants_are_named_beside_the_thirty_days():
    assert (researcher.AUTO_EVERY_DAYS, researcher.RETRY_DAYS, researcher.RETRY_LIMIT) == (30, 3, 3)


@pytest.mark.parametrize("reason", ["llm", "stale", "error"])
def test_a_run_that_failed_but_not_for_its_material_holds_the_sweep_a_few_days_never_an_owners_ask(conn, world, reason):
    """The final review's M1 (reversing the record of K4 Task 4 R1): a transient failure -- the model three times, a
    dead runner, an error -- no longer cancels the first plan's research for 30 days: the sweep waits RETRY_DAYS. An
    owner's ask is held by a run that finished within the day (REQUEST_GAP), never by one that failed."""
    rf.seed(conn)
    run = rf.failed(conn, reason=reason)
    assert _due(conn) == []
    _aged(conn, run, researcher.RETRY_DAYS - 0.5)
    assert _due(conn) == []
    _aged(conn, run, researcher.RETRY_DAYS + 0.5)
    assert _due(conn) == [rf.BIZ]
    assert researcher.request(conn, config.load(), rf.BIZ, trigger="owner").queued


def test_three_failed_runs_hold_the_sweep_until_the_oldest_is_thirty_days_old(conn, world):
    rf.seed(conn)
    runs = [rf.failed(conn) for _ in range(researcher.RETRY_LIMIT)]
    for run, days in zip(runs, (20, 12, 5), strict=True):
        _aged(conn, run, days)
    assert _due(conn) == []  # RETRY_LIMIT failures in the 30 days, the last past RETRY_DAYS
    _aged(conn, runs[0], researcher.AUTO_EVERY_DAYS + 1)
    assert _due(conn) == [rf.BIZ]


def test_a_run_that_failed_for_its_material_holds_the_sweep_for_thirty_days(conn, world):
    """What the business stored could not be read (`material`): research-run does not try the job again, and the sweep
    waits the 30 days too -- another run would fail the same way, after another Places lookup."""
    rf.seed(conn)
    run = rf.failed(conn, reason=researcher.NO_MATERIAL)
    _aged(conn, run, researcher.AUTO_EVERY_DAYS - 1)
    assert _due(conn) == []
    _aged(conn, run, researcher.AUTO_EVERY_DAYS + 1)
    assert _due(conn) == [rf.BIZ]


def test_research_done_with_the_roles_on_validate_stands_for_validate_and_not_for_on(conn, world):
    """The final review's M1: with the roles on, only research built while they were on stands for the 30 days -- the
    validate week's runs stay the team's, so the businesses it researched are due again; on validate, any run done
    does, delivered or not."""
    rf.seed(conn)
    rf.delivered(conn, mode="validate", days_ago=5)
    assert researcher.due(conn, "validate") == [] and researcher.due(conn, "on") == [rf.BIZ]
    rf.delivered(conn, mode="on", dropped="revise", days_ago=3)  # done, short of the Critic's deliver
    assert researcher.due(conn, "validate") == [] and researcher.due(conn, "on") == []


def test_after_the_switch_to_on_the_validate_weeks_businesses_get_research_an_owner_can_read(conn, world, monkeypatch):
    rf.seed(conn)
    assert runner.research_run(conn, 0, 1)["enqueued"] == 1 and rf.run_row(conn)["mode"] == "validate"
    assert runner.research_run(conn, 0, 1)["enqueued"] == 0
    monkeypatch.setenv("AGENT_ROLES", "on")
    out = runner.research_run(conn, 0, 1)
    assert (out["enqueued"], out["done"]) == (1, 1) and rf.run_row(conn)["mode"] == "on"
    assert researcher.owner_view(config.load(), rf.run_row(conn)) is not None
    assert runner.research_run(conn, 0, 1)["enqueued"] == 0


def test_a_run_still_building_holds_the_sweep(conn, world):
    rf.seed(conn)
    run = rf.failed(conn)
    conn.execute(
        "UPDATE research_run SET status = 'building', dropped = NULL, finished_at = NULL WHERE id = %s", (run,)
    )
    _aged(conn, run, researcher.RETRY_DAYS + 1)
    assert _due(conn) == []


def test_a_business_never_researched_comes_before_one_due_again(conn, world):
    """The final review's M3: a new business's first research does not wait behind the month's refreshes, which come
    oldest first."""
    for biz in ("biz_a", "biz_b", "biz_c"):
        rf.seed(conn, biz)
    for biz, days in (("biz_a", 30), ("biz_b", 20), ("biz_c", 10)):  # biz_a is the oldest business
        conn.execute("UPDATE business SET created_at = now() - make_interval(days => %s) WHERE id = %s", (days, biz))
    _aged(conn, rf.delivered(conn, "biz_a"), researcher.AUTO_EVERY_DAYS + 1)  # researched a month ago
    _aged(conn, rf.failed(conn, "biz_b"), researcher.RETRY_DAYS + 1)  # tried, and failed, days ago
    assert _due(conn) == ["biz_c", "biz_a", "biz_b"]


def test_each_shard_takes_its_own_businesses_the_oldest_first_and_no_more_than_the_limit(conn, world):
    ids = [rf.BIZ, "biz_2", "biz_3", "biz_4"]
    for biz in ids:
        rf.seed(conn, biz)
    assert _due(conn) == ids  # the oldest business first
    assert _due(conn, limit=2) == ids[:2]
    left, right = _due(conn, 0, 2), _due(conn, 1, 2)
    assert set(left).isdisjoint(right) and sorted(left + right) == ids


def test_one_sweep_queues_no_more_than_its_limit_and_the_next_takes_the_rest(conn, world):
    ids = [f"biz_{k:02d}" for k in range(researcher.SWEEP_LIMIT + 1)]
    for biz in ids:
        rf.seed(conn, biz)
    assert researcher.sweep(conn, "validate", 0, 1) == researcher.SWEEP_LIMIT
    waiting = conn.execute("SELECT business_id FROM agent_job ORDER BY business_id").fetchall()
    assert [r["business_id"] for r in waiting] == ids[:-1]  # the oldest first: the newest business waits an hour
    assert researcher.sweep(conn, "validate", 0, 1) == 1
    assert researcher.sweep(conn, "validate", 0, 1) == 0


def test_with_the_roles_off_the_sweep_queues_nothing(conn, world, monkeypatch):
    rf.seed(conn)
    monkeypatch.setenv("AGENT_ROLES", "off")
    assert runner.research_run(conn, 0, 1)["enqueued"] == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_the_sweep_that_loses_a_race_to_an_owners_ask_leaves_the_owners_brief(conn, world, monkeypatch):
    """K4 Task 4 fix round 1 (review I2b): the sweep reads who is due, the owner's ask lands, then the sweep's insert
    finds a job waiting -- the owner's, whose brief (topics, the question) the sweep must leave alone."""
    rf.seed(conn)
    s = config.load()
    real_due = researcher.due
    ask = {"topics": ["seasonality"], "note": "When are we busiest?", "by": "key:k1"}

    def due_then_the_owner_asks(c, mode, i=0, n=1, **kw):
        ids = real_due(c, mode, i, n, **kw)  # the sweep has read who is due ...
        researcher.request(c, s, rf.BIZ, trigger="owner", **ask)  # ... the owner's ask lands ...
        return ids

    monkeypatch.setattr(researcher, "due", due_then_the_owner_asks)
    assert researcher.sweep(conn, "validate", 0, 1) == 0  # ... and the sweep's own insert finds it waiting
    [job] = conn.execute("SELECT brief FROM agent_job").fetchall()
    assert job["brief"] == {"trigger": "owner", **ask}


def test_research_run_takes_a_job_somebody_asked_for_before_the_sweeps_backlog(conn, world):
    """K4 Task 4 fix round 1 (review I1): the sweep queues up to 20 a shard and a run serves ten, oldest first -- an
    owner's ask queued after them must not wait for the backlog."""
    ids = [f"biz_{k:02d}" for k in range(researcher.BATCH + 1)]
    for biz in ids:
        rf.seed(conn, biz)
    for biz in ids[:-1]:
        rf.job(conn, biz, trigger="auto")  # the sweep's ten, older than the ask
    asked = researcher.request(conn, config.load(), ids[-1], trigger="owner", note="When?")
    assert asked.queued
    out = runner.research_run(conn, 0, 1)
    assert (out["done"], out["left"]) == (researcher.BATCH, 1)
    assert _job(conn, asked.job_id)["status"] == "done"
    waiting = conn.execute("SELECT business_id FROM agent_job WHERE status = 'queued'").fetchall()
    assert [r["business_id"] for r in waiting] == [ids[-2]]  # the newest of the sweep's waits for the next run


@pytest.mark.parametrize(
    "spent, jobs, ran",
    [
        (39, [("a", "auto")], {"a"}),  # one below the sweep's stop: its job runs
        (39, [("a", "auto"), ("b", "auto")], {"a"}),  # ... and that is the day's 40th: the next waits
        (40, [("a", "auto")], set()),  # the last ten runs of the day are for asks
        (40, [("a", "auto"), ("b", "owner")], {"b"}),
        (49, [("a", "owner"), ("b", "operator")], {"a"}),  # an ask may take the last of the day ...
        (50, [("a", "owner")], set()),  # ... and then nothing runs
    ],
)
def test_the_last_ten_runs_of_the_day_are_for_asks(conn, world, spent, jobs, ran):
    """K4 Task 4 fix round 1 (review I1): research-run stops taking the sweep's own jobs ASKED_RESERVE runs short of
    MAX_RUNS_PER_DAY; a job somebody asked for goes on to the ceiling."""
    assert researcher.MAX_RUNS_PER_DAY - researcher.ASKED_RESERVE == 40
    rf.seed(conn, "biz_h")  # the runs made today are this business's
    for _ in range(spent):
        rf.delivered(conn, "biz_h")
    made = {}
    for suffix, trigger in jobs:
        rf.seed(conn, f"biz_{suffix}")
        made[suffix] = rf.job(conn, f"biz_{suffix}", trigger=trigger)
    out = runner.research_run(conn, 0, 1)
    assert {s for s, jid in made.items() if _job(conn, jid)["status"] == "done"} == ran
    assert (out["done"], out["left"]) == (len(ran), len(jobs) - len(ran))


def test_an_owners_ask_waits_for_the_one_in_hand_and_a_day_after_the_last(conn, world):
    rf.seed(conn)
    s = config.load()
    first = researcher.request(conn, s, rf.BIZ, trigger="owner", topics=["seasonality"], note="When?", by="key:k1")
    assert (first.queued, first.status) == (True, "queued")
    brief = conn.execute("SELECT brief FROM agent_job WHERE id = %s", (first.job_id,)).fetchone()["brief"]
    assert brief == {"trigger": "owner", "topics": ["seasonality"], "note": "When?", "by": "key:k1"}
    again = researcher.request(conn, s, rf.BIZ, trigger="owner")
    assert (again.queued, again.status, again.job_id) == (False, "queued", first.job_id)
    runner.research_run(conn, 0, 1)
    later = researcher.request(conn, s, rf.BIZ, trigger="owner")
    assert (later.queued, later.status) == (False, "recent")
    conn.execute("UPDATE research_run SET finished_at = now() - interval '25 hours'")
    assert researcher.request(conn, s, rf.BIZ, trigger="owner").queued


def test_an_owners_ask_while_research_runs_queues_nothing_and_says_so(conn, world):
    rf.seed(conn)
    running = rf.job(conn, trigger="auto")
    _claim(conn)
    asked = researcher.request(conn, config.load(), rf.BIZ, trigger="owner")
    assert (asked.queued, asked.status, asked.job_id) == (False, "running", running)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 1


def test_the_operators_ask_needs_no_wait(conn, world):
    rf.seed(conn)
    rf.delivered(conn)
    assert researcher.request(conn, config.load(), rf.BIZ, trigger="operator").queued


@pytest.mark.parametrize("trigger", ["owner", "operator"])
def test_an_ask_that_names_no_topic_asks_for_every_one(conn, world, trigger):
    rf.seed(conn)
    asked = researcher.request(conn, config.load(), rf.BIZ, trigger=trigger)
    assert _job(conn, asked.job_id)["brief"] == {
        "trigger": trigger,
        "topics": list(research_material.TOPICS),
        "note": None,
        "by": None,
    }


def test_the_operators_ask_replaces_a_waiting_jobs_brief_and_waits_behind_a_running_one(conn, world):
    rf.seed(conn)
    s = config.load()
    waiting = rf.job(conn, trigger="owner", topics=["seasonality"], note="When?", by="key:k1")
    asked = researcher.request(conn, s, rf.BIZ, trigger="operator", topics=["competitors"], by="ops:cli")
    assert (asked.queued, asked.status, asked.job_id) == (True, "queued", waiting)
    assert _job(conn, waiting)["brief"] == {
        "trigger": "operator",
        "topics": ["competitors"],
        "note": None,
        "by": "ops:cli",
    }
    _claim(conn)  # it runs now: the operator's next ask queues a new job behind it
    behind = researcher.request(conn, s, rf.BIZ, trigger="operator")
    assert behind.queued and behind.job_id != waiting
    rows = conn.execute("SELECT id, status FROM agent_job ORDER BY created_at").fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [(waiting, "running"), (behind.job_id, "queued")]


def test_an_owners_ask_takes_over_the_sweeps_waiting_job_and_keeps_its_place(conn, world):
    """K4 Task 4 fix round 1 (review I2a): a job the sweep queued is waiting, but it is not the owner's question being
    researched: the ask puts its topics and note in that job, which keeps its place in line -- and the owner hears
    that research is queued, as it now is. A second ask finds the owner's own job and changes nothing."""
    rf.seed(conn)
    s = config.load()
    assert researcher.sweep(conn, "validate", 0, 1) == 1
    swept = conn.execute("SELECT id, created_at FROM agent_job").fetchone()
    asked = researcher.request(
        conn, s, rf.BIZ, trigger="owner", topics=["seasonality"], note="When are we busiest?", by="key:k1"
    )
    assert (asked.queued, asked.status, asked.job_id) == (True, "queued", swept["id"])
    [job] = conn.execute("SELECT id, brief, created_at FROM agent_job").fetchall()
    assert job["brief"] == {
        "trigger": "owner",
        "topics": ["seasonality"],
        "note": "When are we busiest?",
        "by": "key:k1",
    }
    assert job["created_at"] == swept["created_at"]
    again = researcher.request(conn, s, rf.BIZ, trigger="owner", note="another")
    assert (again.queued, again.status, again.job_id) == (False, "queued", swept["id"])
    assert _job(conn, swept["id"])["brief"]["note"] == "When are we busiest?"


def test_a_recent_run_still_holds_an_owners_ask_while_the_sweeps_job_waits(conn, world):
    rf.seed(conn)
    waiting = rf.job(conn, trigger="auto")
    rf.delivered(conn)
    asked = researcher.request(conn, config.load(), rf.BIZ, trigger="owner", note="When?")
    assert (asked.queued, asked.status, asked.job_id) == (False, "recent", None)
    assert _job(conn, waiting)["brief"]["trigger"] == "auto"


@pytest.mark.parametrize("trigger", ["owners", "", "OWNER", "auto"])
def test_an_ask_of_a_kind_nobody_makes_is_refused(conn, world, trigger):
    """K4 Task 4 fix round 1: only an owner's or the operator's ask is a request -- the sweep's `auto` has its own
    insert -- and a word that is neither must not pass for the operator's, which skips every wait."""
    rf.seed(conn)
    with pytest.raises(ValueError, match="trigger"):
        researcher.request(conn, config.load(), rf.BIZ, trigger=trigger)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_a_draft_business_cannot_ask_but_the_operator_can_for_it(conn, world):
    """K4 Task 4 fix round 1 (plan D2, held where the queue is, not only in Task 5's tool)."""
    rf.seed(conn, claim_state="draft")
    s = config.load()
    asked = researcher.request(conn, s, rf.BIZ, trigger="owner")
    assert (asked.queued, asked.status, asked.job_id) == (False, "draft", None)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0
    assert researcher.request(conn, s, rf.BIZ, trigger="operator").queued


def test_why_not_ask_names_what_about_the_business_stops_an_owner(conn, world):
    """K4 Task 5 fix round 1: the one place that says whether an owner may ask for a business -- the queue's `request`
    and get_research read it -- and why not: not signed in (`draft`), or Admiry has not read it (`unread`: no knowledge
    row, so research-run would skip the job)."""
    assert researcher.why_not_ask(conn, rf.BIZ) == "unread"  # a business that is not there has been read by nobody
    rf.seed(conn)
    assert researcher.why_not_ask(conn, rf.BIZ) is None
    conn.execute("UPDATE business SET claim_state = 'draft' WHERE id = %s", (rf.BIZ,))
    assert researcher.why_not_ask(conn, rf.BIZ) == "draft"
    conn.execute("DELETE FROM knowledge WHERE business_id = %s", (rf.BIZ,))
    assert researcher.why_not_ask(conn, rf.BIZ) == "draft"  # signing in comes first
    conn.execute("UPDATE business SET claim_state = 'claimed' WHERE id = %s", (rf.BIZ,))
    assert researcher.why_not_ask(conn, rf.BIZ) == "unread"


def test_an_owners_ask_for_a_business_admiry_has_not_read_queues_nothing_but_the_operators_does(conn, world):
    """K4 Task 5 fix round 1: the ask was taken, "usually ready within the hour" said, and research-run then skipped the
    job (`no knowledge`). The operator's ask is unchanged: the skipped job is how the operator reads why."""
    rf.seed(conn)
    conn.execute("DELETE FROM knowledge WHERE business_id = %s", (rf.BIZ,))
    s = config.load()
    asked = researcher.request(conn, s, rf.BIZ, trigger="owner")
    assert (asked.queued, asked.status, asked.job_id) == (False, "unread", None)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0
    waiting = rf.job(conn, trigger="auto")  # the business's static state comes before any job or run
    assert researcher.request(conn, s, rf.BIZ, trigger="owner").status == "unread"
    assert _job(conn, waiting)["brief"]["trigger"] == "auto"
    assert researcher.request(conn, s, rf.BIZ, trigger="operator").queued


def test_the_job_in_hand_and_the_run_that_finished_within_a_day_are_read_in_one_place(conn, world):
    """K4 Task 4 fix round 1: the two reads an owner's ask is judged by, shared with Task 5's get_research."""
    rf.seed(conn)
    assert researcher.waiting_job(conn, rf.BIZ) is None and researcher.recently_done(conn, rf.BIZ) is False
    jid = rf.job(conn, trigger="auto")
    assert researcher.waiting_job(conn, rf.BIZ) == {"id": jid, "status": "queued", "trigger": "auto"}
    _claim(conn)
    assert researcher.waiting_job(conn, rf.BIZ) == {"id": jid, "status": "running", "trigger": "auto"}
    behind = rf.job(conn, trigger="operator")  # queued behind the running one: the newest is the job in hand
    assert researcher.waiting_job(conn, rf.BIZ) == {"id": behind, "status": "queued", "trigger": "operator"}
    queue.finish(conn, jid, status=queue.DONE, result={})
    assert researcher.waiting_job(conn, rf.BIZ) == {"id": behind, "status": "queued", "trigger": "operator"}
    queue.finish(conn, behind, status=queue.DONE, result={})
    assert researcher.waiting_job(conn, rf.BIZ) is None
    other = rf.job(conn, trigger="owner")
    assert researcher.waiting_job(conn, rf.BIZ) == {"id": other, "status": "queued", "trigger": "owner"}
    run = rf.delivered(conn, days_ago=2)
    assert researcher.recently_done(conn, rf.BIZ) is False  # a run two days ago
    conn.execute("UPDATE research_run SET finished_at = now() - interval '23 hours' WHERE id = %s", (run,))
    assert researcher.recently_done(conn, rf.BIZ) is True
    conn.execute("UPDATE research_run SET status = 'failed' WHERE id = %s", (run,))
    assert researcher.recently_done(conn, rf.BIZ) is False  # a run that failed does not hold an ask


@pytest.mark.parametrize("trigger", ["owner", "operator"])
def test_with_the_roles_off_an_ask_queues_nothing(conn, world, monkeypatch, trigger):
    rf.seed(conn)
    monkeypatch.setenv("AGENT_ROLES", "off")
    asked = researcher.request(conn, config.load(), rf.BIZ, trigger=trigger)
    assert (asked.queued, asked.status, asked.job_id) == (False, "off", None)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_run_now_runs_the_waiting_job_at_once_and_finishes_it_as_research_run_does(conn, world):
    rf.seed(conn)
    jid = rf.job(conn, topics=["competitors"])
    row = researcher.run_now(conn, config.load(), jid)
    assert (row["job_id"], row["status"], row["verdict"], row["trigger"]) == (jid, "done", "deliver", "operator")
    job = _job(conn, jid)
    assert (job["status"], job["rounds"]) == ("done", 1)
    assert job["result"] == {"run": row["id"], "verdict": "deliver", "score": 1.0, "confidence": "high"}
    assert len(world.research_calls) == 1


def test_run_now_holds_the_job_while_it_runs_so_research_run_cannot_take_it_too(conn, world, monkeypatch):
    rf.seed(conn)
    jid = rf.job(conn)
    seen = {}

    def run_job(conn, settings, job):
        seen["status"] = _job(conn, job["id"])["status"]
        seen["taken"] = queue.claim(conn, 0, 1, roles=(researcher.ROLE,), limit=5)  # research-run, at that moment
        return queue.DONE, {}

    monkeypatch.setattr(researcher, "run_job", run_job)
    researcher.run_now(conn, config.load(), jid)
    assert seen == {"status": "running", "taken": []}


@pytest.mark.parametrize("status", ["running", "done", "skipped", "failed"])
def test_run_now_takes_a_waiting_job_and_no_other(conn, world, status):
    """K4 Task 4 R2 (pre-flight F29): a job that is not waiting is left exactly as it is."""
    rf.seed(conn)
    jid = rf.job(conn)
    conn.execute("UPDATE agent_job SET status = %s WHERE id = %s", (status, jid))
    before = _job(conn, jid)
    assert researcher.run_now(conn, config.load(), jid) is None
    assert _job(conn, jid) == before and rf.run_row(conn) is None and world.research_calls == []


def test_run_now_on_a_job_that_is_not_there_does_nothing(conn, world):
    rf.seed(conn)
    assert researcher.run_now(conn, config.load(), "aj_nothing") is None
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0 and world.research_calls == []


def test_run_now_keeps_the_days_caps_the_job_ends_skipped_and_no_run_is_written(conn, world):
    rf.seed(conn)
    _spend_the_day(conn)
    jid = rf.job(conn)
    assert researcher.run_now(conn, config.load(), jid) is None
    job = _job(conn, jid)
    assert (job["status"], job["result"]) == ("skipped", {"reason": "capped"})
    assert rf.run_row(conn) is None and world.research_calls == []


def test_run_now_keeps_the_days_ceiling_and_the_job_waits_for_research_run(conn, world):
    """K4 Task 4 fix round 1 (review I3): research-run stops at MAX_RUNS_PER_DAY research runs a day and so does an
    operator's run at once; the job it was asked to run stays as it was."""
    rf.seed(conn)
    for _ in range(researcher.MAX_RUNS_PER_DAY):
        rf.delivered(conn)
    jid = rf.job(conn)
    before = _job(conn, jid)
    assert researcher.run_now(conn, config.load(), jid) is None
    assert _job(conn, jid) == before and world.research_calls == []
    conn.execute("DELETE FROM research_run WHERE id = (SELECT id FROM research_run LIMIT 1)")  # one under the ceiling
    assert researcher.run_now(conn, config.load(), jid)["job_id"] == jid


def test_run_now_will_not_start_a_second_run_for_a_business_whose_research_is_running(conn, world):
    """K4 Task 4 fix round 1 (review I3): the operator's ask while research runs queues a new job behind it; running it
    at once would be two runs for one business (two Places calls, two reads of the same sites)."""
    rf.seed(conn)
    running = rf.job(conn, trigger="auto")
    _claim(conn)
    behind = rf.job(conn)  # the operator's ask, queued behind the running one
    before = _job(conn, behind)
    assert researcher.run_now(conn, config.load(), behind) is None
    assert _job(conn, behind) == before and _job(conn, running)["status"] == "running" and world.research_calls == []
    rf.seed(conn, "biz_2")  # another business's research running holds nothing of this one's
    other = rf.job(conn, "biz_2")
    assert researcher.run_now(conn, config.load(), other)["business_id"] == "biz_2"
    queue.finish(conn, running, status=queue.DONE, result={})  # the running job ends: now it may
    assert researcher.run_now(conn, config.load(), behind)["job_id"] == behind


def test_run_now_fails_the_job_and_raises_when_the_run_itself_breaks(conn, world, monkeypatch):
    rf.seed(conn)
    jid = rf.job(conn)

    def broken(conn, settings, job):
        raise RuntimeError("boom")

    monkeypatch.setattr(researcher, "run_job", broken)
    with pytest.raises(RuntimeError, match="boom"):
        researcher.run_now(conn, config.load(), jid)
    job = _job(conn, jid)
    assert (job["status"], job["error"]) == ("failed", "RuntimeError: boom")
