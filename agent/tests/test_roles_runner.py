"""K1 (spec 2026-10-07-admiry-capability-layer-design §8.2): research-run, the roles' background queue, with the
Critic's look at each new plan version -- stored like the setup card's own look, which then reads it."""

import pytest

from agent import audit, config, jobs, llm, plan_review, plans
from agent.roles import queue, runner
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_review_launch import OWNER, REVIEW, draft_sections, funded_prepaid


@pytest.fixture(autouse=True)
def no_sweep(monkeypatch):
    """The Critic's queue alone: research-run's sweep (K4) is test_research_triggers'."""
    from agent.roles import researcher

    monkeypatch.setattr(researcher, "sweep", lambda conn, mode, i, n: 0)


@pytest.fixture
def fake(monkeypatch):
    """The model faked for the second look: REVIEW, every dimension graded 4."""
    gen = FakeGenerator(reviews=[REVIEW])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    return gen


@pytest.fixture
def roles_on(monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")


def version(conn, **kw) -> dict:
    return plans.create_version(
        conn, "biz_1", goal_id=None, sections=draft_sections(**kw), estimate_basis={}, rationale="", notes=[]
    )


def test_research_run_is_a_scheduled_job():
    assert jobs.JOBS["research-run"] is runner.research_run


def test_with_the_roles_off_nothing_runs_and_the_waiting_job_waits(conn, fake, monkeypatch):
    funded_prepaid(conn)
    monkeypatch.setenv("AGENT_ROLES", "validate")
    version(conn)
    monkeypatch.setenv("AGENT_ROLES", "off")
    assert runner.research_run(conn, 0, 1) == {
        "expired": 0,
        "stale": 0,
        "purged": 0,
        "enqueued": 0,
        "done": 0,
        "skipped": 0,
        "failed": 0,
        "retried": 0,
        "left": 0,
        "reviewed": 0,
        "stranded": 0,
        "mode": "off",
    }
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "queued" and fake.review_calls == []


def test_the_background_critic_judges_the_newest_version_once_and_the_card_reads_it(conn, fake, roles_on):
    funded_prepaid(conn)
    version(conn)
    newest = version(conn, monthly_usd=1500.0)
    assert runner.research_run(conn, 0, 1) == {
        "expired": 0,
        "stale": 0,
        "purged": 0,
        "enqueued": 0,
        "done": 1,
        "skipped": 0,
        "failed": 0,
        "retried": 0,
        "left": 0,
        "reviewed": 0,
        "stranded": 0,
        "mode": "validate",
    }
    row = conn.execute("SELECT plan_id, score, verdict, actor FROM plan_review").fetchone()
    assert dict(row) == {"plan_id": newest["id"], "score": 0.75, "verdict": "deliver", "actor": "job:critic"}
    job = conn.execute("SELECT status, result FROM agent_job").fetchone()
    assert job["status"] == "done" and (job["result"]["score"], job["result"]["verdict"]) == (0.75, "deliver")
    # the setup card's own look at the same state reads the stored row: no second model call
    owner = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert owner["summary"] and len(fake.review_calls) == 1


def test_a_version_superseded_before_its_turn_is_skipped(conn, fake, roles_on):
    funded_prepaid(conn)
    first = version(conn)
    (taken,) = queue.claim(conn, 0, 1, roles=("critic",), limit=1)  # a runner takes it...
    version(conn, monthly_usd=1500.0)  # ...while the owner changes the plan again
    got = runner.critic_plan(conn, config.load(), {**taken, "brief": {"plan_id": first["id"]}})
    assert got == (queue.SKIPPED, {"reason": "superseded"}) and fake.review_calls == []


def test_a_business_past_its_role_runs_for_the_day_is_skipped(conn, fake, roles_on):
    funded_prepaid(conn)
    for _ in range(runner.ROLE_RUNS_PER_DAY):
        audit.record_job(conn, job="critic", tool="job.critic", business_id="biz_1", args={}, result={})
    version(conn)
    assert runner.research_run(conn, 0, 1)["skipped"] == 1
    assert conn.execute("SELECT result FROM agent_job").fetchone()["result"] == {"reason": "capped"}
    assert fake.review_calls == []


def test_one_failing_job_does_not_stop_the_batch(conn, fake, roles_on, monkeypatch):
    funded_prepaid(conn)
    conn.execute("INSERT INTO business (id) VALUES ('biz_2')")
    version(conn)
    queue.enqueue(conn, business_id="biz_2", role="critic", kind="plan", brief={"plan_id": "pl_gone"})
    real = runner.HANDLERS[("critic", "plan")]

    def flaky(conn, settings, job):
        if job["business_id"] == "biz_2":
            raise RuntimeError("boom")
        return real(conn, settings, job)

    monkeypatch.setitem(runner.HANDLERS, ("critic", "plan"), flaky)
    out = runner.research_run(conn, 0, 1)
    assert (out["done"], out["failed"]) == (1, 1)
    assert conn.execute("SELECT error FROM agent_job WHERE business_id = 'biz_2'").fetchone()["error"] == (
        "RuntimeError: boom"
    )


def test_a_look_the_model_cannot_give_is_tried_again_then_fails(conn, fake, roles_on):
    """Final review I2: a busy model at one run is tried at the next; after ATTEMPTS tries the job fails."""
    fake.error = "the assistant is busy; try again in a minute"
    funded_prepaid(conn)
    version(conn)
    for _ in range(runner.ATTEMPTS - 1):
        assert runner.research_run(conn, 0, 1)["retried"] == 1
        assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "queued"
    assert runner.research_run(conn, 0, 1)["failed"] == 1
    row = conn.execute("SELECT status, rounds, result FROM agent_job").fetchone()
    assert (row["status"], row["rounds"], row["result"]) == ("failed", runner.ATTEMPTS, {"reason": "model unavailable"})
    fake.error = None  # the model is back: the next version is judged
    version(conn, monthly_usd=1500.0)
    assert runner.research_run(conn, 0, 1)["done"] == 1
