"""`research-run` (spec 2026-10-07-admiry-capability-layer-design §8.2, K1, K4): the roles' background queue, hourly.
It first takes the Reviewer's jobs (L1), whatever AGENT_ROLES says -- and whatever AGENT_LAUNCH_REVIEW says now: the
switch decides whether a yes goes to review; a review once queued is finished. Then two more kinds of work (three in
all): the Critic's look at a new plan version (queued by plans.create_version through
roles.queue.after_version), taken BATCH at a time -- plan_review.look by the job, stored with its score like the setup
card's own look, which then reads the stored row instead of the model when nothing changed since -- and the
Researcher's run (K4, roles.researcher.run_job), taken one at a time, each claimed only once research may go on
(researcher.RUN_SECONDS not spent, the day's research runs, researcher.MAX_RUNS_PER_DAY, not used up; at most
researcher.BATCH a run) and the jobs somebody asked for first: the sweep's own wait once only researcher.ASKED_RESERVE
of the day's runs are left, which are for asks. A run that dies -- prod runs the tick in a container a release can
recreate -- loses the job in hand, not every job it had claimed ahead of its turn, which would stay running until they
fail as stale. A job tried again waits for the next run (this one goes past it); `left` counts the research jobs still
waiting, the sweep's held back for the day included. Whatever the switch, a research run left building fails
(researcher.fail_stale) and old ones go (researcher.purge). With AGENT_ROLES validate or on, the run first queues the
automatic research that is due by the runs the switch can stand for (researcher.sweep: after a signed-in business's
first plan, and again 30 days later if its plan changed; a run that failed, but not for its material, is tried again
after researcher.RETRY_DAYS days, until researcher.RETRY_LIMIT runs have failed in the 30 days -- the first and two
retries; `enqueued` counts them). With
AGENT_ROLES off nothing else runs (a job left waiting runs once the switch is on).
ROLE_RUNS_PER_DAY caps a business's background model calls a day, counted apart from the owner's own day
(auth.check_llm_cap): a role's work never uses up what the owner may ask; it counts the Critic's job.critic rows, the
Reviewer's job.reviewer rows (L1) and the Researcher's job.researcher rows, whose business is in their args."""

from __future__ import annotations

import logging
import time

import psycopg

from .. import config, plan_review
from . import queue, researcher, reviewer

log = logging.getLogger(__name__)
BATCH = 50  # jobs a shard takes per run
ROLE_RUNS_PER_DAY = 24  # a business's background model calls a day (spec §8.3)
ROLE_TOOLS = ("job.critic", "job.researcher", "job.reviewer")  # the audit tools a role's model call records
BATCHED = ("critic",)  # quick work: one model call a job, BATCH at a time
ATTEMPTS = 3  # runs one job is tried at before it fails for good: a busy model at one run is tried at the next


def _handle(conn: psycopg.Connection, settings: config.Settings, job: dict, out: dict) -> None:
    """One claimed job by its handler: finished with what it returned; tried again at the next run when the model could
    not answer (up to ATTEMPTS: the handler's FAILED result says queue.MODEL_UNAVAILABLE; any other failure is final);
    failed when the handler raised."""
    try:
        status, result = HANDLERS[(job["role"], job["kind"])](conn, settings, job)
    except Exception as e:  # noqa: BLE001 - one job's failure must not stop the batch
        log.warning(
            "research-run: %s %s for business %s failed: %s",
            job["role"],
            job["kind"],
            job["business_id"],
            type(e).__name__,
        )
        queue.finish(conn, job["id"], status=queue.FAILED, error=f"{type(e).__name__}: {e}"[:500])
        out["failed"] += 1
        return
    unanswered = status == queue.FAILED and result.get("reason") == queue.MODEL_UNAVAILABLE
    if unanswered and job["rounds"] < ATTEMPTS:  # the model could not answer: the next run tries again
        out["retried" if queue.retry(conn, job["id"]) else "skipped"] += 1
        return
    queue.finish(conn, job["id"], status=status, result=result)
    out[status] += 1


def research_run(conn: psycopg.Connection, i: int, n: int) -> dict:
    settings = config.load()
    out = {
        "expired": queue.expire_stale(conn, i, n),
        "stale": researcher.fail_stale(conn, i, n),
        "purged": researcher.purge(conn, i, n),
        "enqueued": 0,
        "done": 0,
        "skipped": 0,
        "failed": 0,
        "retried": 0,
        "left": 0,
        "reviewed": 0,
        "stranded": 0,
    }
    # R16: taken whatever AGENT_LAUNCH_REVIEW says -- the switch gates only whether a yes goes to review
    # (proposals._apply); a review once queued is finished, switch on or off, so turning it off never strands one.
    _reviews(conn, settings, i, n, out)
    if settings.roles_mode == "off":
        return {**out, "mode": "off"}
    out["enqueued"] = researcher.sweep(conn, settings.roles_mode, i, n)
    for job in queue.claim(conn, i, n, roles=BATCHED, limit=BATCH):
        _handle(conn, settings, job, out)
    _research(conn, settings, i, n, out)
    return {**out, "mode": settings.roles_mode}


def _reviews(conn: psycopg.Connection, settings: config.Settings, i: int, n: int, out: dict) -> None:
    """The Reviewer's jobs (L1, plan D10; ruling R16): taken whatever AGENT_LAUNCH_REVIEW says and whatever
    AGENT_ROLES says -- the switch gates only whether a yes goes to review, never whether a review once queued is
    finished -- one at a time while reviewer.REVIEW_SECONDS last, each claimed only when its turn comes (a run that
    dies leaves one job running, not all it had taken); a job tried again waits for the next run. First, ruling
    R19c: reviews left running with no job waiting or running for them any more (that run that died) are held for a
    person, so none waits unseen -- `stranded` counts them."""
    out["stranded"] = reviewer.hold_stranded(conn, settings)
    started, handled = time.monotonic(), []
    while time.monotonic() - started < reviewer.REVIEW_SECONDS:
        claimed = queue.claim(conn, i, n, roles=(reviewer.ROLE,), limit=1, skip=handled)
        if not claimed:
            break
        handled.append(claimed[0]["id"])
        _handle(conn, settings, claimed[0], out)
        out["reviewed"] += 1


def _research(conn: psycopg.Connection, settings: config.Settings, i: int, n: int, out: dict) -> None:
    """The Researcher's jobs, one at a time while researcher.RUN_SECONDS last and the day's research runs allow, at most
    researcher.BATCH: each is claimed only after those checks, so a job this run does not reach is never claimed, and a
    run that dies leaves one job running, not all it had taken. The jobs somebody asked for go first (queue.claim); once
    the day's runs reach MAX_RUNS_PER_DAY - researcher.ASKED_RESERVE only they are claimed, the sweep's own wait for
    tomorrow, so a backlog of the sweep's can never use the runs an owner's or the operator's ask needs. A job tried
    again (the model could not answer) is queued again for the next run: the claim goes past the jobs this run handled.
    `left`: the jobs still waiting."""
    started = time.monotonic()
    handled: list[str] = []
    while len(handled) < researcher.BATCH:
        if time.monotonic() - started >= researcher.RUN_SECONDS:
            break
        spent = researcher.runs_today_all(conn)
        if spent >= researcher.MAX_RUNS_PER_DAY:
            break
        asked_only = spent >= researcher.MAX_RUNS_PER_DAY - researcher.ASKED_RESERVE
        claimed = queue.claim(conn, i, n, roles=(researcher.ROLE,), limit=1, skip=handled, asked_only=asked_only)
        if not claimed:
            break
        handled.append(claimed[0]["id"])
        _handle(conn, settings, claimed[0], out)
    out["left"] = queue.waiting(conn, i, n, roles=(researcher.ROLE,), skip=handled)


def critic_plan(conn: psycopg.Connection, settings: config.Settings, job: dict) -> tuple[str, dict]:
    """The Critic's look at the version the job names: skipped past the business's day or once a newer version
    superseded it; failed when the model could not answer; else done, with the stored review's id, score and
    verdict."""
    biz = job["business_id"]
    if runs_today(conn, biz) >= ROLE_RUNS_PER_DAY:
        return queue.SKIPPED, {"reason": "capped"}
    looked = plan_review.look(conn, settings, biz, by=plan_review.By.job("critic"), plan_id=job["brief"].get("plan_id"))
    if looked is None:
        return queue.SKIPPED, {"reason": "superseded"}
    if looked.review_id is None:
        return queue.FAILED, {"reason": queue.MODEL_UNAVAILABLE}
    return queue.DONE, {"review_id": looked.review_id, "score": looked.score, "verdict": looked.verdict}


def runs_today(conn: psycopg.Connection, business_id: str) -> int:
    """The business's background model calls since midnight (UTC), as their audit rows count them: the Critic's looks
    (job.critic, on the business's own rows), the Reviewer's last look (job.reviewer, on the business's own rows --
    L1) and the Researcher's calls (job.researcher: no business on the row, the business in its args -- K4)."""
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool = ANY(%(tools)s) AND ts >= date_trunc('day', now()) "
        "AND (business_id = %(b)s OR (business_id IS NULL AND args_redacted->>'business' = %(b)s))",
        {"tools": list(ROLE_TOOLS), "b": business_id},
    ).fetchone()["n"]


HANDLERS = {
    ("critic", "plan"): critic_plan,
    (researcher.ROLE, researcher.KIND): researcher.run_job,
    (reviewer.ROLE, reviewer.KIND): reviewer.run_job,
}
