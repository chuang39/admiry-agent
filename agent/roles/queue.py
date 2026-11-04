"""The roles' background queue (spec 2026-10-07-admiry-capability-layer-design §8.1, K1): one `agent_job` row per
piece of work a role does in the background. `enqueue` keeps one waiting job per (business, role, kind) -- a newer
brief replaces the waiting one's, so a plan changed sixteen times in an hour is judged once, at its newest version
(`enqueue_unless_waiting`, for a background sweep, leaves a waiting job as it is: its brief may be somebody's ask);
`claim` takes a shard's waiting jobs, those somebody asked for first and each kind oldest first, in one statement (SKIP
LOCKED: two runners never take one job; a run that must not lose the jobs it has not reached to a dead runner takes
them one at a time) and `waiting` counts them; `finish` closes one; `expire_stale` fails a job a dead runner left
running. `after_version` is what plans.create_version calls for every new version."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg
from psycopg.types.json import Jsonb

from .. import config, db

QUEUED, RUNNING, DONE, SKIPPED, FAILED = "queued", "running", "done", "skipped", "failed"
# the reason of a handler's FAILED result that the next run tries again (`retry`); any other failure is final
MODEL_UNAVAILABLE = "model unavailable"
# a brief's `trigger` for work the system started by itself (the Researcher's sweep): `claim` takes it after the work
# somebody asked for. A brief with no trigger -- the Critic's -- is not the system's own.
AUTO = "auto"
STALE_AFTER = "2 hours"
# jobs.shard_sql's fragment over the business (a vertical's job, which has none, over its own id) -- written out, not
# imported: jobs.py imports the modules that import this one
_SHARD = "mod(abs(hashtext(coalesce(business_id, id))::bigint), %(n)s) = %(i)s"


def enqueue(conn: psycopg.Connection, *, business_id: str | None, role: str, kind: str, brief: dict) -> str:
    """The waiting job for this business, role and kind, its brief replaced by `brief` -- or a new one. Its id."""
    return conn.execute(
        "INSERT INTO agent_job (id, business_id, role, kind, brief) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (business_id, role, kind) WHERE status = 'queued' "
        "DO UPDATE SET brief = EXCLUDED.brief, updated_at = now() RETURNING id",
        (db.new_id("aj"), business_id, role, kind, Jsonb(brief)),
    ).fetchone()["id"]


def enqueue_unless_waiting(
    conn: psycopg.Connection, *, business_id: str | None, role: str, kind: str, brief: dict
) -> str | None:
    """A new waiting job for this business, role and kind -- unless one waits already, which is left exactly as it is.
    The new job's id, or None. `enqueue` gives the waiting job the newest brief, which is right for a plan judged at
    its newest version and wrong for a background sweep: the waiting job's brief may be an owner's or the operator's
    ask (topics, the question), and the sweep must never take it for its own."""
    row = conn.execute(
        "INSERT INTO agent_job (id, business_id, role, kind, brief) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (business_id, role, kind) WHERE status = 'queued' DO NOTHING RETURNING id",
        (db.new_id("aj"), business_id, role, kind, Jsonb(brief)),
    ).fetchone()
    return row["id"] if row else None


def _is_auto(job: dict) -> bool:
    """Whether the job is the system's own (its brief's `trigger` is AUTO)."""
    return isinstance(job["brief"], dict) and job["brief"].get("trigger") == AUTO


def claim(
    conn: psycopg.Connection,
    i: int,
    n: int,
    *,
    roles: tuple[str, ...],
    limit: int,
    skip: Sequence[str] = (),
    asked_only: bool = False,
) -> list[dict]:
    """Up to `limit` waiting jobs of `roles` in shard i of n, marked running (one more round) in one statement: the
    jobs somebody asked for first, then the system's own (a brief whose `trigger` is AUTO), each oldest first -- so an
    owner's ask is never held up by a backlog the sweep queued before it. `asked_only` leaves the system's own out. A
    brief with no trigger (the Critic's) is not the system's own: such jobs go by age alone. `skip` names jobs not to
    take: a run that takes its jobs one at a time puts a job it tried again back to wait for the next run, and goes
    past it."""
    rows = conn.execute(
        "UPDATE agent_job SET status = 'running', rounds = rounds + 1, started_at = now(), updated_at = now() "
        "WHERE id IN (SELECT id FROM agent_job WHERE status = 'queued' AND role = ANY(%(roles)s) "
        f"AND {_SHARD} AND id <> ALL(%(skip)s::text[]) "
        "AND NOT (%(asked_only)s AND coalesce(brief->>'trigger', '') = %(auto)s) "
        "ORDER BY coalesce(brief->>'trigger', '') = %(auto)s, created_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED) "
        "RETURNING id, business_id, role, kind, brief, rounds, created_at",
        {
            "roles": list(roles),
            "limit": limit,
            "i": i,
            "n": n,
            "skip": list(skip),
            "asked_only": asked_only,
            "auto": AUTO,
        },
    ).fetchall()
    return sorted(rows, key=lambda r: (_is_auto(r), r["created_at"]))


def waiting(conn: psycopg.Connection, i: int, n: int, *, roles: tuple[str, ...], skip: Sequence[str] = ()) -> int:
    """How many jobs of `roles` in shard i of n wait (queued), the jobs in `skip` aside."""
    return conn.execute(
        "SELECT count(*) AS n FROM agent_job WHERE status = 'queued' AND role = ANY(%(roles)s) "
        f"AND {_SHARD} AND id <> ALL(%(skip)s::text[])",
        {"roles": list(roles), "i": i, "n": n, "skip": list(skip)},
    ).fetchone()["n"]


def finish(
    conn: psycopg.Connection, job_id: str, *, status: str, result: dict | None = None, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE agent_job SET status = %s, result = %s, error = %s, finished_at = now(), updated_at = now() "
        "WHERE id = %s",
        (status, Jsonb(result) if result is not None else None, error, job_id),
    )


def retry(conn: psycopg.Connection, job_id: str) -> bool:
    """A job whose work could not run this time (a busy model) waits for the next run again -- unless a newer job for
    the same business, role and kind already waits (it holds the newer brief): then this one gives way, skipped.
    Whether it waits again (final review I2)."""
    try:
        row = conn.execute(
            "UPDATE agent_job j SET status = 'queued', started_at = NULL, updated_at = now() WHERE j.id = %s "
            "AND NOT EXISTS (SELECT 1 FROM agent_job w WHERE w.status = 'queued' AND w.role = j.role "
            "AND w.kind = j.kind AND w.business_id IS NOT DISTINCT FROM j.business_id) RETURNING id",
            (job_id,),
        ).fetchone()
    except psycopg.errors.UniqueViolation:  # a newer job was queued between the check and the update
        row = None
    if row is not None:
        return True
    finish(conn, job_id, status=SKIPPED, result={"reason": "a newer job waits"})
    return False


def expire_stale(conn: psycopg.Connection, i: int, n: int) -> int:
    """Jobs left running longer than STALE_AFTER in shard i of n -> failed (the runner died); how many."""
    return conn.execute(
        "UPDATE agent_job SET status = 'failed', error = 'stale: no runner finished it', finished_at = now(), "
        f"updated_at = now() WHERE status = 'running' AND started_at < now() - interval '{STALE_AFTER}' AND {_SHARD}",
        {"i": i, "n": n},
    ).rowcount


def after_version(conn: psycopg.Connection, business_id: str, plan_id: str) -> None:
    """A new plan version: the Critic judges it in the background (spec §4: never a third call in the owner's turn)
    once the roles are on (AGENT_ROLES validate or on); inside the version's own transaction."""
    if config.load().roles_mode == "off":
        return
    enqueue(conn, business_id=business_id, role="critic", kind="plan", brief={"plan_id": plan_id})
