"""K1 (spec 2026-10-07-admiry-capability-layer-design §8.1): the roles' background queue -- one waiting job per
business, role and kind holding the newest brief, taken by shard oldest first, failed when a dead runner left it."""

from agent import plans
from agent.roles import queue
from agent.tests.test_review_launch import draft_sections


def businesses(conn) -> None:
    conn.execute("INSERT INTO business (id) VALUES ('biz_1'), ('biz_2')")


def critic(conn, biz: str, brief: dict | None = None) -> str:
    return queue.enqueue(conn, business_id=biz, role="critic", kind="plan", brief=brief or {})


def version(conn) -> dict:
    return plans.create_version(
        conn, "biz_1", goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[]
    )


def test_one_waiting_job_per_business_role_and_kind_holds_the_newest_brief(conn):
    businesses(conn)
    first = critic(conn, "biz_1", {"plan_id": "pl_1"})
    again = critic(conn, "biz_1", {"plan_id": "pl_2"})
    other = critic(conn, "biz_2", {"plan_id": "pl_9"})
    assert again == first and other != first
    rows = conn.execute("SELECT business_id, brief, status FROM agent_job ORDER BY business_id").fetchall()
    assert [(r["business_id"], r["brief"], r["status"]) for r in rows] == [
        ("biz_1", {"plan_id": "pl_2"}, "queued"),
        ("biz_2", {"plan_id": "pl_9"}, "queued"),
    ]


def test_a_running_job_keeps_its_brief_and_a_new_one_waits_behind_it(conn):
    businesses(conn)
    first = critic(conn, "biz_1", {"plan_id": "pl_1"})
    (taken,) = queue.claim(conn, 0, 1, roles=("critic",), limit=10)
    assert (taken["id"], taken["rounds"], taken["brief"]) == (first, 1, {"plan_id": "pl_1"})
    assert critic(conn, "biz_1", {"plan_id": "pl_2"}) != first


def test_claim_takes_waiting_jobs_oldest_first_once_and_splits_them_by_shard(conn):
    businesses(conn)
    a = critic(conn, "biz_1")
    b = critic(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (b,))
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("critic",), limit=1)] == [b]
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("critic",), limit=5)] == [a]
    assert queue.claim(conn, 0, 1, roles=("critic",), limit=5) == []
    conn.execute("UPDATE agent_job SET status = 'queued'")
    assert queue.claim(conn, 0, 1, roles=("analyst",), limit=5) == []
    halves = [{j["id"] for j in queue.claim(conn, i, 2, roles=("critic",), limit=5)} for i in (0, 1)]
    assert halves[0] | halves[1] == {a, b} and not halves[0] & halves[1]


def test_a_job_a_dead_runner_left_running_fails_after_two_hours(conn):
    businesses(conn)
    old = critic(conn, "biz_1")
    new = critic(conn, "biz_2")
    queue.claim(conn, 0, 1, roles=("critic",), limit=5)
    conn.execute("UPDATE agent_job SET started_at = now() - interval '3 hours' WHERE id = %s", (old,))
    assert queue.expire_stale(conn, 0, 1) == 1
    rows = {r["id"]: (r["status"], r["error"]) for r in conn.execute("SELECT id, status, error FROM agent_job")}
    assert rows[old][0] == "failed" and "stale" in rows[old][1] and rows[new] == ("running", None)


def test_finish_records_the_outcome(conn):
    businesses(conn)
    job = critic(conn, "biz_1")
    queue.finish(conn, job, status=queue.DONE, result={"score": 0.75})
    row = conn.execute("SELECT status, result, finished_at FROM agent_job WHERE id = %s", (job,)).fetchone()
    assert row["status"] == "done" and row["result"] == {"score": 0.75} and row["finished_at"] is not None


def test_a_new_plan_version_queues_the_critic_only_with_the_roles_on(conn, monkeypatch):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    version(conn)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0  # AGENT_ROLES unset: off
    monkeypatch.setenv("AGENT_ROLES", "validate")
    version(conn)
    newest = version(conn)
    rows = conn.execute("SELECT role, kind, brief, status FROM agent_job").fetchall()
    assert [dict(r) for r in rows] == [
        {"role": "critic", "kind": "plan", "brief": {"plan_id": newest["id"]}, "status": "queued"}
    ]


def test_a_job_tried_again_waits_again_unless_a_newer_one_already_waits(conn):
    """Final review I2: a job whose model was busy waits for the next run -- unless the owner changed the plan
    meanwhile and a newer job for it waits already: then this one gives way, skipped."""
    businesses(conn)
    first = critic(conn, "biz_1", {"plan_id": "pl_1"})
    queue.claim(conn, 0, 1, roles=("critic",), limit=1)
    assert queue.retry(conn, first) is True
    row = conn.execute("SELECT status, started_at FROM agent_job WHERE id = %s", (first,)).fetchone()
    assert (row["status"], row["started_at"]) == ("queued", None)
    (again,) = queue.claim(conn, 0, 1, roles=("critic",), limit=1)
    assert (again["id"], again["rounds"]) == (first, 2)
    newer = critic(conn, "biz_1", {"plan_id": "pl_2"})
    assert queue.retry(conn, first) is False
    rows = {r["id"]: (r["status"], r["result"]) for r in conn.execute("SELECT id, status, result FROM agent_job")}
    assert rows[first] == ("skipped", {"reason": "a newer job waits"}) and rows[newer][0] == "queued"


def test_claim_leaves_out_the_jobs_it_is_told_to_skip(conn):
    """A run that takes its jobs one at a time puts a job it tried again back to wait: that one is for the next run, so
    it names the jobs it already handled and the next claim goes past them."""
    businesses(conn)
    a, b = critic(conn, "biz_1"), critic(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (a,))
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("critic",), limit=5, skip=[a])] == [b]
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("critic",), limit=5, skip=[])] == [a]


def researcher(conn, biz: str, trigger: str | None) -> str:
    """A waiting research job whose brief says who asked for it (`trigger`), or nothing of the kind."""
    brief = {} if trigger is None else {"trigger": trigger}
    return queue.enqueue(conn, business_id=biz, role="researcher", kind="business", brief=brief)


def test_claim_takes_the_jobs_somebody_asked_for_before_the_systems_own_each_oldest_first(conn):
    """K4 Task 4 fix round 1: the sweep's jobs (brief trigger `auto`) wait behind an owner's or the operator's, however
    old they are, so a backlog of the sweep's cannot hold up an ask. The Critic's briefs carry no trigger: theirs all
    count as asked for, and the order of their jobs is age alone (the tests above)."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1'), ('biz_2'), ('biz_3'), ('biz_4'), ('biz_5')")
    old = researcher(conn, "biz_1", "auto")
    new = researcher(conn, "biz_2", "auto")
    owner = researcher(conn, "biz_3", "owner")
    operator = researcher(conn, "biz_4", "operator")
    plain = researcher(conn, "biz_5", None)
    conn.execute("UPDATE agent_job SET created_at = now() - interval '2 minutes' WHERE id = %s", (old,))
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (new,))
    asked = [owner, operator, plain]  # created in this order, after the sweep's two
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("researcher",), limit=5)] == [*asked, old, new]
    conn.execute("UPDATE agent_job SET status = 'queued'")
    taken = [queue.claim(conn, 0, 1, roles=("researcher",), limit=1)[0]["id"] for _ in range(5)]
    assert taken == [*asked, old, new]


def test_claim_can_leave_the_systems_own_jobs_out(conn):
    """K4 Task 4 fix round 1: once the day's runs are nearly spent only the jobs somebody asked for may take the last
    ones (runner._research): `asked_only` takes those and leaves the sweep's waiting."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1'), ('biz_2')")
    auto = researcher(conn, "biz_1", "auto")
    assert queue.claim(conn, 0, 1, roles=("researcher",), limit=5, asked_only=True) == []
    owner = researcher(conn, "biz_2", "owner")
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("researcher",), limit=5, asked_only=True)] == [owner]
    assert queue.claim(conn, 0, 1, roles=("researcher",), limit=5, asked_only=True) == []
    assert queue.waiting(conn, 0, 1, roles=("researcher",)) == 1  # the sweep's job still waits
    assert [j["id"] for j in queue.claim(conn, 0, 1, roles=("researcher",), limit=5)] == [auto]


def test_enqueue_unless_waiting_leaves_a_waiting_job_exactly_as_it_is(conn):
    """K4 Task 4 fix round 1: the sweep's insert must never replace the brief of a job somebody asked for (`enqueue`
    gives the newest brief to the waiting job: right for the Critic's, wrong for a background sweep)."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    ask = {"trigger": "owner", "note": "When?"}
    owner = queue.enqueue(conn, business_id="biz_1", role="researcher", kind="business", brief=ask)
    before = conn.execute("SELECT * FROM agent_job").fetchall()
    taken = queue.enqueue_unless_waiting(
        conn, business_id="biz_1", role="researcher", kind="business", brief={"trigger": "auto"}
    )
    assert taken is None and conn.execute("SELECT * FROM agent_job").fetchall() == before
    queue.claim(conn, 0, 1, roles=("researcher",), limit=1)  # it runs now: nothing waits
    made = queue.enqueue_unless_waiting(
        conn, business_id="biz_1", role="researcher", kind="business", brief={"trigger": "auto"}
    )
    rows = conn.execute("SELECT id, status, brief FROM agent_job ORDER BY created_at").fetchall()
    assert made is not None and made != owner
    assert [(r["id"], r["status"], r["brief"]) for r in rows] == [
        (owner, "running", ask),
        (made, "queued", {"trigger": "auto"}),
    ]


def test_waiting_counts_the_queued_jobs_of_the_roles_in_a_shard_but_the_skipped(conn):
    businesses(conn)
    a, b = critic(conn, "biz_1"), critic(conn, "biz_2")
    queue.enqueue(conn, business_id="biz_1", role="researcher", kind="business", brief={})
    assert queue.waiting(conn, 0, 1, roles=("critic",)) == 2
    assert queue.waiting(conn, 0, 1, roles=("critic", "researcher")) == 3
    assert queue.waiting(conn, 0, 1, roles=("critic",), skip=[a]) == 1
    assert sum(queue.waiting(conn, i, 2, roles=("critic",)) for i in (0, 1)) == 2  # the shards split them
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (a,))
    queue.claim(conn, 0, 1, roles=("critic",), limit=1)  # a is running now: it no longer waits
    assert queue.waiting(conn, 0, 1, roles=("critic",)) == 1 and b != a
