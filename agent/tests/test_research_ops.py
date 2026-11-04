"""The operator's research commands (capability layer K4): research queues it (or, on dev, runs it at once),
research-show prints a run as the team sees it, research-due lists what the next sweep would queue. Audited with no
business on the row."""

import pytest
from psycopg.types.json import Jsonb

from agent import audit, ops, places
from agent.roles import queue, researcher, runner
from agent.tests import research_fixtures as rf


@pytest.fixture
def world(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    return rf.fake_llm(monkeypatch)


def _jobs(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"]


def test_research_queues_for_research_run_with_the_operators_brief(conn, world):
    argv = ["research", rf.BIZ, "--topic", "competitors", "--topic", "seasonality", "--note", "Evening hours?"]
    assert ops.main(argv) == 0
    job = conn.execute("SELECT status, brief FROM agent_job WHERE role = 'researcher'").fetchone()
    assert job["status"] == "queued" and job["brief"]["trigger"] == "operator"
    assert job["brief"]["topics"] == ["competitors", "seasonality"] and job["brief"]["note"] == "Evening hours?"
    assert job["brief"]["by"] == ops.ACTOR
    [row] = conn.execute("SELECT business_id, args_redacted FROM agent_action WHERE tool = 'ops.research'").fetchall()
    assert row["business_id"] is None and row["args_redacted"]["business"] == rf.BIZ


def test_research_now_runs_it_and_research_show_prints_every_round(conn, world, capsys):
    assert ops.main(["research", rf.BIZ, "--now"]) == 0
    out = capsys.readouterr().out
    assert "status=done" in out and "verdict=deliver" in out and "round 1: verdict=deliver" in out
    assert "finding (medium; benchmark:dentists:google_search):" in out
    assert ops.main(["research-show", rf.BIZ]) == 0
    assert "source place:1 [place]" in capsys.readouterr().out
    shown = conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.research_show' AND business_id IS NULL"
    ).fetchone()
    assert shown["n"] == 1


def test_research_now_refuses_on_prod(conn, world, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "prod")
    assert ops.main(["research", rf.BIZ, "--now"]) == 1
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_research_show_without_a_run_says_so(conn, world, capsys):
    assert ops.main(["research-show", rf.BIZ]) == 1
    assert "no research" in capsys.readouterr().err


def test_research_due_lists_what_the_next_sweep_would_queue(conn, world, capsys):
    assert ops.main(["research-due"]) == 0
    assert capsys.readouterr().out.split() == ["1", rf.BIZ]


def test_research_for_a_business_that_is_not_there_queues_nothing(conn, world, capsys):
    assert ops.main(["research", "biz_nope"]) == 1
    assert "no business biz_nope" in capsys.readouterr().err and _jobs(conn) == 0


def test_research_with_the_roles_off_says_so_and_queues_nothing(conn, world, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ROLES", "off")
    assert ops.main(["research", rf.BIZ]) == 1
    assert "AGENT_ROLES is off" in capsys.readouterr().err and _jobs(conn) == 0


def test_research_now_says_so_when_the_businesss_day_is_spent(conn, world, capsys):
    for _ in range(runner.ROLE_RUNS_PER_DAY):  # a run needs room for its four model calls
        audit.record_job(
            conn, job="research-run", tool="job.researcher", business_id=None, args={"business": rf.BIZ}, result={}
        )
    assert ops.main(["research", rf.BIZ, "--now"]) == 1
    err = capsys.readouterr().err
    assert "skipped" in err and "capped" in err and "did not run" not in err and world.research_calls == []
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "skipped"


def test_research_now_prints_the_reason_a_job_was_skipped_not_a_guess(conn, world, capsys):
    """K4 Task 4 fix round 1 (review item 4): a business Admiry knows nothing of is skipped for that, not for its day."""
    conn.execute("INSERT INTO business (id, claim_state) VALUES ('biz_nk', 'claimed')")
    assert ops.main(["research", "biz_nk", "--now"]) == 1
    err = capsys.readouterr().err
    assert "skipped" in err and "no knowledge" in err and "capped" not in err and "day" not in err
    assert conn.execute("SELECT result FROM agent_job").fetchone()["result"] == {"reason": "no knowledge"}


def test_research_now_says_so_when_research_run_took_the_job_first(conn, world, monkeypatch, capsys):
    real = researcher.run_now

    def taken_first(c, settings, job_id):
        queue.claim(c, 0, 1, roles=(researcher.ROLE,), limit=1)  # the hourly run's claim lands between the two
        return real(c, settings, job_id)

    monkeypatch.setattr(researcher, "run_now", taken_first)
    assert ops.main(["research", rf.BIZ, "--now"]) == 1
    assert "research-run took the job (running)" in capsys.readouterr().err and world.research_calls == []


def test_research_now_says_the_days_ceiling_and_leaves_the_job_for_research_run(conn, world, capsys):
    """K4 Task 4 fix round 1 (review I3)."""
    for _ in range(researcher.MAX_RUNS_PER_DAY):
        rf.delivered(conn)
    assert ops.main(["research", rf.BIZ, "--now"]) == 1
    err = capsys.readouterr().err
    assert f"{researcher.MAX_RUNS_PER_DAY} research runs" in err and "research-run" in err
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "queued" and world.research_calls == []


def test_research_now_says_research_is_running_and_leaves_the_job_for_research_run(conn, world, capsys):
    """K4 Task 4 fix round 1 (review I3): the ask is queued behind the running job, and not run at once beside it."""
    rf.job(conn, trigger="auto")
    queue.claim(conn, 0, 1, roles=(researcher.ROLE,), limit=1)  # research-run has it in hand
    assert ops.main(["research", rf.BIZ, "--now"]) == 1
    err = capsys.readouterr().err
    assert "running" in err and "research-run" in err and world.research_calls == []
    statuses = conn.execute("SELECT status FROM agent_job ORDER BY created_at").fetchall()
    assert [r["status"] for r in statuses] == ["running", "queued"]


def test_research_now_prints_a_run_the_model_could_not_write(conn, world, capsys):
    world.research_error = "the assistant is busy; try again in a minute"
    assert ops.main(["research", rf.BIZ, "--now"]) == 0
    out = capsys.readouterr().out
    assert "status=failed" in out and "dropped=llm" in out and "read: places=" in out
    assert conn.execute("SELECT status FROM agent_job").fetchone()["status"] == "failed"  # the operator asks again


def test_research_show_prints_what_was_dropped_and_why(conn, world, capsys):
    rid = rf.delivered(conn)
    assert ops.main(["research-show", rf.BIZ, "--run", rid]) == 0
    out = capsys.readouterr().out
    assert f"research {rid} " in out and "status=done" in out and "confidence=high" in out
    assert "finding (medium; page:1): Rival Dental 2 offers free whitening.  [dropped: named]" in out
    assert "aim for: invisalign palo alto\n" in out and "aim for: rival dental 2  [dropped: named]" in out
    assert "keep out: dental jobs" in out and "offer angle: Same-week appointments for cleanings." in out
    assert "unknown: How many people nearby search for Invisalign each month." in out
    assert (
        f"source page:1 [page] The website of a business nearby, 2.0 miles away {places.maps_url(None, 'ChIJ2')}" in out
    )


def test_research_show_prints_what_each_round_missed_and_the_critics_issues(conn, world, capsys):
    rid = rf.delivered(conn)
    review = {
        "rounds": [
            {"round": 1, "verdict": "revise", "score": 0.6, "missing": ["a price to compare"]},
            {"round": 2, "verdict": "deliver", "score": 0.9, "missing": []},
        ],
        "final": {"issues": [{"dimension": "relevant", "text": "Too general for a dentist."}]},
    }
    conn.execute("UPDATE research_run SET review = %s WHERE id = %s", (Jsonb(review), rid))
    assert ops.main(["research-show", rf.BIZ]) == 0
    out = capsys.readouterr().out
    assert "round 1: verdict=revise score=0.6 missing=a price to compare" in out
    assert "round 2: verdict=deliver score=0.9 missing=-" in out
    assert "issue: relevant: Too general for a dentist." in out


def test_research_show_names_the_run_asked_for_and_only_the_businesss_own(conn, world, capsys):
    old, new = rf.delivered(conn, days_ago=3), rf.delivered(conn)
    rf.seed(conn, "biz_2")
    other = rf.delivered(conn, "biz_2")
    assert ops.main(["research-show", rf.BIZ, "--run", old]) == 0
    assert f"research {old} " in capsys.readouterr().out
    assert ops.main(["research-show", rf.BIZ]) == 0
    assert f"research {new} " in capsys.readouterr().out  # the latest by default
    assert ops.main(["research-show", rf.BIZ, "--run", other]) == 1
    assert f"no research {other}" in capsys.readouterr().err  # another business's run is not this one's
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.research_show'").fetchone()["n"] == 2


def test_research_due_lists_every_business_due_not_one_sweeps_share(conn, world, capsys):
    ids = [rf.BIZ, *(f"biz_{k:02d}" for k in range(researcher.SWEEP_LIMIT))]
    for biz in ids[1:]:
        rf.seed(conn, biz)
    assert ops.main(["research-due"]) == 0
    assert capsys.readouterr().out.split() == [str(len(ids)), *ids]


def test_research_due_says_none_when_nothing_is_due(conn, world, capsys):
    rf.delivered(conn)
    assert ops.main(["research-due"]) == 0
    assert capsys.readouterr().out.split() == ["0"]


def test_research_due_reads_the_runs_the_switch_can_stand_for(conn, world, monkeypatch, capsys):
    """The final review's M1: with AGENT_ROLES on only research built on stands for the 30 days; on validate (and off,
    when the sweep queues nothing) any run done does."""
    rf.delivered(conn, mode="validate")
    for mode, out in (("validate", ["0"]), ("off", ["0"]), ("on", ["1", rf.BIZ])):
        monkeypatch.setenv("AGENT_ROLES", mode)
        assert ops.main(["research-due"]) == 0
        assert capsys.readouterr().out.split() == out, mode
