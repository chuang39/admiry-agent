"""`python -m agent.eval <command>` (spec 2026-10-07 capability layer §7; K1 `run`, K5 the rest). On the dev box,
inside the agent container (the model key stays there), with `--db-name agent_eval` (the agent's server and
credentials, a database of the eval's own); on the laptop, the `--from` forms, which need neither a model nor a database.

  run    [--cases DIR] [--db URL | --db-name NAME] [--baseline FILE] [--out FILE]
         build each case's plan fresh with the real model, judge it, print the table; fails when a dimension's mean
         falls by more than 0.3 against the baseline (agent/eval/baseline.json) over the cases both graded, and when
         there is no such case; exit 2, before any case runs, for a baseline named and missing or unusable, or no case
  freeze [--cases DIR] [--db URL | --db-name NAME] [--case ID]... [--all]
         build the plans the founder grades, once, with the real model, and print them as a bundle (JSON) on stdout:
         the cases with no frozen plan, or the --case ones, or --all
  freeze --from FILE [--cases DIR] [--flawed DIR] [--replace]
         the bundle's plans into the case files, and the flawed copies written again from them; a plan the founder
         graded is kept unless --replace (which drops its grades); a graded flawed copy whose base was frozen again is
         kept too, said STALE, and the command exits 1 (everything else is still written)
  export --json | --from FILE [--cases DIR] [--flawed DIR]
         the founder's grades: --json prints those saved on the grading page as a bundle (on the box); --from writes
         a bundle (printed on the box or downloaded from the page) into the case files; neither reads the database and
         writes the files where both are at hand. Only a plan graded on all six dimensions is written
  judge  [--cases DIR] [--flawed DIR] [--repeat N] [--effort E] [--only ID]... [--grades files|db] [--out FILE]
         the judge over every frozen plan (the cases' and the flawed copies'), N runs each (3 by default): its
         alignment with the founder's grades per dimension (acceptance 80 % within ±1) and whether it catches each
         flawed copy; exit 0 when it passes -- every flaw present and caught, no error, 80 % within ±1. --only a case
         takes its flawed copies too; such a run is not the acceptance run and never exits 0. --grades db: the
         founder's grades from the grading page's table (on the box, before the export reaches the files)
  baseline --from FILE [--to FILE]
         the last `baseline-json:` line of a saved `run` output as the new baseline (agent/eval/baseline.json); a
         run with an errored case or no grades is refused (exit 2): a baseline is an accepted run
  case-from BUSINESS --id CASE-ID
         dev only: a business on this box as a golden case with what could identify it replaced, printed as JSON for
         the operator to read and, with the founder's yes for that business, commit (audited as ops.eval_case)
  turns  [--turns FILE] [--cases DIR] [--only ID]... [--out FILE]
         the harness's golden turns (K6 slice 1, agent/eval/turns.json) against the live model: each turn's intent and
         fields judged, a table printed; exit 0 when every start_over, support and not_mine turn reads right, no turn
         reads as start_over or not_mine where it did not expect it and 90 % of all do, else 1; 2, after the table,
         when --out cannot be written (inside the container: a path such as /tmp/turns.md)

Exit 0 when the command did its work, 1 when part of it failed, 2 when it could not start."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import audit, config, db, llm
from . import align, anonymize, baseline, bundle, cases, runner, turns


def _golden(p: argparse.ArgumentParser, *, flawed: bool = False) -> None:
    p.add_argument("--cases", default=str(cases.CASES_DIR))
    if flawed:
        p.add_argument("--flawed", default=str(cases.FLAWED_DIR))


def _database(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", help="the eval database's URL (its name must contain 'eval')")
    p.add_argument("--db-name", help="a database on AGENT_DATABASE_URL's server, e.g. agent_eval")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="build each case's plan fresh with the real model, judge it, print the table")
    _golden(run)
    _database(run)
    run.add_argument("--out", help="append the table to this Markdown file, e.g. docs/ops/agent-eval-log.md")
    run.add_argument(
        "--baseline",
        help="the grades a dimension's mean must not fall below by more than 0.3 (default agent/eval/baseline.json; "
        "a file named here must exist)",
    )
    freeze = sub.add_parser("freeze", help="build the plans the founder grades (with --from: write them here)")
    _golden(freeze, flawed=True)
    _database(freeze)
    freeze.add_argument("--case", action="append", default=[], help="build this case's plan again (repeatable)")
    freeze.add_argument("--all", action="store_true", help="build every case's plan again")
    freeze.add_argument("--from", dest="source", help="a bundle `freeze` printed on the box")
    freeze.add_argument("--replace", action="store_true", help="replace a plan the founder graded, dropping its grades")
    jd = sub.add_parser("judge", help="the judge's alignment with the founder and its discrimination of the flaws")
    _golden(jd, flawed=True)
    jd.add_argument("--repeat", type=int, default=3, help="runs per plan (3 by default)")
    jd.add_argument(
        "--effort", choices=config.EFFORTS, help="the judge's effort for this run (default AGENT_LLM_EFFORT)"
    )
    jd.add_argument("--only", action="append", default=[], help="this case and its flawed copies (repeatable)")
    jd.add_argument(
        "--grades",
        choices=("files", "db"),
        default="files",
        help="db: read the founder's grades from the grading page's table",
    )
    jd.add_argument("--out", help="append the report to this Markdown file")
    base = sub.add_parser("baseline", help="a saved `run` output's grades as the new baseline")
    base.add_argument("--from", dest="source", required=True, help="the saved output of `agent.eval run`")
    base.add_argument("--to", default=str(baseline.PATH))
    ex = sub.add_parser("export", help="the founder's grades: printed on the box (--json) or written here (--from)")
    _golden(ex, flawed=True)
    which = ex.add_mutually_exclusive_group()
    which.add_argument("--json", action="store_true", help="print the grades saved on the grading page as a bundle")
    which.add_argument("--from", dest="source", help="a bundle printed on the box or downloaded from the page")
    cf = sub.add_parser("case-from", help="dev only: a business on this box as an anonymized golden case")
    cf.add_argument("business_id")
    cf.add_argument("--id", dest="case_id", required=True, help="the case's id, <vertical>-<n>, e.g. dentists-6")
    tr = sub.add_parser("turns", help="the harness's golden turns against the live model (K6 slice 1)")
    tr.add_argument("--turns", default=str(turns.PATH))
    tr.add_argument("--cases", default=str(cases.CASES_DIR))
    tr.add_argument("--only", action="append", default=[], help="this turn only (repeatable)")
    tr.add_argument("--out", help="append the table to this Markdown file, e.g. docs/ops/agent-eval-log.md")
    args = parser.parse_args(argv)
    if args.cmd == "baseline":
        return _baseline(args)
    if args.cmd == "freeze" and args.source:
        return _freeze_from(args)
    if args.cmd == "export":
        return _export(args)
    if args.cmd == "case-from":
        return _case_from(args)
    if not config.load().llm_api_key:
        print("META_MODEL_API_KEY is not set: the eval runs the real model", file=sys.stderr)
        return 2
    if args.cmd == "turns":
        return _turns(args)
    if args.cmd == "judge":
        return _judge(args)
    return _run(args) if args.cmd == "run" else _freeze(args)


def _start(args, choose=lambda golden: golden) -> list[cases.Case] | None:
    """The golden set loaded (and chosen from), the eval's database prepared and named in the environment for every
    command the cases run; None, with the reason printed, when either cannot be (nothing dropped for a typo)."""
    live = os.environ.get("AGENT_DATABASE_URL")
    try:
        url = runner.database_url(db=args.db, db_name=args.db_name, env=dict(os.environ))
        chosen = choose(cases.load_all(Path(args.cases)))
        runner.prepare(url, live)
    except cases.CaseError as e:
        print(e, file=sys.stderr)
        return None
    os.environ["AGENT_DATABASE_URL"] = url  # every command the cases run reads its database from here (config.load)
    return chosen


def _run(args) -> int:
    """The baseline is read (and checked) and the golden set loaded before the database is prepared and before any
    case runs: what cannot be compared against, or nothing to run, is exit 2 and not a paid run that then passes. Only
    the default baseline (agent/eval/baseline.json) may be absent, until the Rollout commits it; one named with
    --baseline must exist."""
    path = Path(args.baseline or baseline.PATH)
    try:
        then = baseline.load(path)
    except (OSError, ValueError) as e:  # CaseError is a ValueError, and so is a broken JSON file
        print(f"{path}: {e}", file=sys.stderr)
        return 2
    if then is None and args.baseline:
        print(f"{path}: no such baseline (`agent.eval baseline --from` a saved run makes one)", file=sys.stderr)
        return 2

    def choose(golden: list[cases.Case]) -> list[cases.Case]:
        if not golden:
            raise cases.CaseError(f"no case in {args.cases}: nothing to run")
        return golden

    golden = _start(args, choose)
    if golden is None:
        return 2
    settings = config.load()
    rows = runner.run_cases(golden, settings)
    table = runner.report(rows, settings)
    print(table)  # the paid run's table is on screen before anything is compared
    if then is None:
        compared, falls = ["no baseline yet: `agent.eval baseline --from` this output makes one"], []
    else:
        compared, falls = baseline.compare(rows, then)
    print("\n" + "\n".join(compared))
    print(baseline.line(baseline.of_rows(rows, settings, day=datetime.now(UTC).date().isoformat())))
    if args.out:
        with open(args.out, "a", encoding="utf-8") as f:
            f.write("\n" + table + "\n\n" + "\n".join(compared) + "\n")
    return 1 if falls or any("error" in r for r in rows) else 0


def _baseline(args) -> int:
    try:
        made = baseline.accepted(baseline.from_output(Path(args.source).read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:  # CaseError is a ValueError, and so is a broken JSON line
        print(e, file=sys.stderr)
        return 2
    baseline.write(Path(args.to), made)
    print(f"baseline {made.get('date')} ({len(made.get('grades') or {})} cases) written to {args.to}")
    return 0


def _freeze(args) -> int:
    def choose(golden: list[cases.Case]) -> list[cases.Case]:
        unknown = sorted(set(args.case) - {c.id for c in golden})
        if unknown:
            raise cases.CaseError(f"no case {', '.join(unknown)} in {args.cases}")
        return [c for c in golden if args.all or c.id in args.case or (not args.case and c.plan is None)]

    chosen = _start(args, choose)
    if chosen is None:
        return 2
    frozen, errors = runner.freeze_cases(chosen, config.load())
    print(json.dumps(bundle.plans_bundle(frozen, errors), ensure_ascii=False))
    for cid, err in sorted(errors.items()):
        print(f"{cid}: {err}", file=sys.stderr)
    print(f"frozen {len(frozen)} of {len(chosen)}", file=sys.stderr)
    return 1 if errors else 0


def _freeze_from(args) -> int:
    try:
        sent = bundle.read(Path(args.source))
        lines = bundle.apply_plans(sent, cases_dir=Path(args.cases), flawed_dir=Path(args.flawed), replace=args.replace)
    except cases.CaseError as e:
        print(e, file=sys.stderr)
        return 2
    print("\n".join(lines))
    left = bundle.stale(lines)
    if left:
        print(f"graded flawed copies kept stale: {len(left)} (--replace regenerates them)", file=sys.stderr)
    return 1 if left else 0


def _export(args) -> int:
    cases_dir, flawed_dir = Path(args.cases), Path(args.flawed)
    try:
        if args.source:
            sent = bundle.read(Path(args.source))
        else:
            items = cases.graded_all(cases_dir, flawed_dir)
            with db.connect() as conn:
                sent = bundle.grades_bundle(conn, items)
            if args.json:
                print(json.dumps(sent, ensure_ascii=False))
                return 0
        lines = bundle.apply_grades(sent, cases_dir=cases_dir, flawed_dir=flawed_dir)
    except cases.CaseError as e:
        print(e, file=sys.stderr)
        return 2
    print("\n".join(lines) or "no grades saved yet")
    return 0


def _case_from(args) -> int:
    if config.load().env == "prod":
        print("case-from is for the dev box: it refuses on prod", file=sys.stderr)
        return 1
    with db.connect() as conn:
        try:
            case = anonymize.validated(anonymize.case_of(conn, args.business_id, args.case_id))
        except cases.CaseError as e:
            print(e, file=sys.stderr)
            return 1
        with conn.transaction():
            audit.record(
                conn,
                tool="ops.eval_case",
                args={"business": args.business_id, "case": args.case_id},
                result={},
                ok=True,
                business_id=None,
                actor="ops:cli",
                actor_class="platform",
                api_key_id=None,
                ip=None,
            )
    print(json.dumps(case, indent=2, ensure_ascii=False))
    return 0


def _turns(args) -> int:
    """The golden turns against the live model: no database (the fixtures are K5's case files), one call a turn."""
    try:
        golden = {c.id: c for c in cases.load_all(Path(args.cases))}
        items = turns.load(Path(args.turns))
    except (OSError, cases.CaseError) as e:
        print(e, file=sys.stderr)
        return 2
    if args.only:
        items = [t for t in items if t.id in args.only]
    if not items:
        print("nothing to run: no such turn", file=sys.stderr)
        return 2
    settings = config.load()
    try:
        rows = turns.run(items, golden, llm.generator(settings))
        text, passed = turns.report(
            rows, model=settings.llm_model, effort=settings.llm_effort, day=datetime.now(UTC).date().isoformat()
        )
    except cases.CaseError as e:
        print(e, file=sys.stderr)
        return 2
    print(text)
    if args.out:
        try:
            with open(args.out, "a", encoding="utf-8") as f:
                f.write("\n" + text + "\n")
        except OSError as e:  # the table is on screen: the paid run is not lost (I4: the image holds agent/ alone)
            print(f"{args.out}: {e}", file=sys.stderr)
            return 2
    return 0 if passed else 1


def _judge(args) -> int:
    try:
        items = cases.graded_all(Path(args.cases), Path(args.flawed))
    except cases.CaseError as e:
        print(e, file=sys.stderr)
        return 2
    if args.only:
        items = [it for it in items if it.id in args.only or it.base in args.only]
    if args.grades == "db":
        with db.connect() as conn:
            items = bundle.with_db_grades(conn, items)
    if not items or args.repeat < 1:
        print("nothing to judge: no frozen plan (`freeze` first), or --repeat under 1", file=sys.stderr)
        return 2
    settings = config.load()
    judged = runner.judge_items(items, settings, repeat=args.repeat, effort=args.effort)
    text, passed = align.report(
        items,
        judged,
        model=settings.llm_model,
        effort=args.effort or settings.llm_effort,
        repeat=args.repeat,
        day=datetime.now(UTC).date().isoformat(),
        filtered=bool(args.only),
    )
    print(text)
    if args.out:
        with open(args.out, "a", encoding="utf-8") as f:
            f.write("\n" + text + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
