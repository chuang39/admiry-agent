"""`python -m agent.tick <job> [--shard i/N]` — run one scheduled job once.

Every run writes a job_run row (started, finished, ok, details) — the ledger that makes
the dev-box Airflow DAG and the prod systemd timers comparable (spec §4.6). Exit code
0 on success, 1 on failure; the traceback goes to stderr and the message into details.
The jobs' own log lines (INFO and up) go to stderr too, which journald (the systemd unit)
and Airflow's task log both keep (ruling H34).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback

from psycopg.types.json import Jsonb

from . import crawl, db
from .jobs import JOBS, JobFailed


def parse_shard(text: str) -> tuple[int, int]:
    try:
        i_s, n_s = text.split("/")
        i, n = int(i_s), int(n_s)
    except ValueError as e:
        raise ValueError(f"shard must look like i/N, got {text!r}") from e
    if n < 1 or not 0 <= i < n:
        raise ValueError(f"shard index out of range: {text!r}")
    return i, n


def run(job: str, shard: str = "0/1", url: str | None = None) -> dict:
    fn = JOBS[job]  # KeyError for an unknown job, before any row is written
    i, n = parse_shard(shard)
    with db.connect(url) as conn:
        run_id = conn.execute("INSERT INTO job_run (job, shard) VALUES (%s, %s) RETURNING id", (job, shard)).fetchone()[
            "id"
        ]
        try:
            details = fn(conn, i, n)
        except JobFailed as e:  # the job ran, and what it did must page: its details reach the ledger
            conn.execute(
                "UPDATE job_run SET finished_at = now(), ok = false, details = %s WHERE id = %s",
                (Jsonb({**e.details, "error": str(e)}), run_id),
            )
            raise
        except Exception as e:
            conn.execute(
                "UPDATE job_run SET finished_at = now(), ok = false, details = %s WHERE id = %s",
                (Jsonb({"error": f"{type(e).__name__}: {e}"}), run_id),
            )
            raise
        conn.execute(
            "UPDATE job_run SET finished_at = now(), ok = true, details = %s WHERE id = %s", (Jsonb(details), run_id)
        )
        return details


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    """The job's log lines (INFO and up) on stderr -- for journald and Airflow's task log. Without it the process has
    no handler and an effective WARNING level, so every job's INFO line (page-check's down hosted pages, ...) is
    dropped (ruling H34). A logger that already has its own handlers (fastmcp's) keeps them. The HTTP client's own
    lines, which hold the whole URL of each request, are left out (crawl.quiet_http_logs: research-run reads the
    websites of businesses nearby, plan K4 D5)."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    crawl.quiet_http_logs()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="agent.tick", description="run one scheduled agent job")
    parser.add_argument("job", choices=sorted(JOBS))
    parser.add_argument("--shard", default="0/1", help="i/N: this process handles businesses with hashtext %% N == i")
    args = parser.parse_args(argv)
    try:
        details = run(args.job, args.shard)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    print(json.dumps({"job": args.job, "shard": args.shard, **details}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
