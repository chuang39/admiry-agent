"""The eval's runner (spec 2026-10-07 capability layer §7): each case's plan built through the real commands --
the session `start` builds after its extraction (the case gives the knowledge: no extraction, no map lookup, no site
read), the owner's answers through `answer`, the plan through `recommend` -- against a database of its own. `run` judges
the plan it just built by the Critic's plan rubric, on the plan alone (no money, no launch refusal) (K1); `freeze` keeps
it as the plan the founder grades (K5 P1); `judge` judges the frozen ones (K5 Task 7). The database is dropped and
created again each run, so `prepare` refuses any whose name lacks "eval" or that is the agent's own."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg import sql

from .. import benchmark, business, config, consult, db, keys, llm, plan_review, plans
from ..roles import contracts, critic
from .cases import Case, CaseError

DEFAULT_URL = "postgresql:///bidx_agent_eval"
EMAIL = "eval@admiry.test"
IP = "203.0.113.10"  # TEST-NET-3: never a real visitor
MAX_QUESTIONS = 12
FROZEN_KEYS = ("sections", "lines", "rationale", "notes", "estimate_basis", "model", "effort", "commit", "built_on")


def database_url(*, db: str | None, db_name: str | None, env: dict) -> str:
    """--db as given; --db-name on the server and with the credentials of AGENT_DATABASE_URL (on the dev box, inside
    the agent container); else AGENT_EVAL_DB_URL, else DEFAULT_URL."""
    if db:
        return db
    if db_name:
        live = env.get("AGENT_DATABASE_URL")
        if not live:
            raise CaseError("--db-name takes the server and the credentials from AGENT_DATABASE_URL, which is unset")
        return psycopg.conninfo.make_conninfo(live, dbname=db_name)
    return env.get("AGENT_EVAL_DB_URL") or DEFAULT_URL


def prepare(url: str, live_url: str | None) -> None:
    """The eval's database dropped and created again, the schema applied and the benchmark seeded."""
    name = psycopg.conninfo.conninfo_to_dict(url).get("dbname") or ""
    live = psycopg.conninfo.conninfo_to_dict(live_url).get("dbname") if live_url else None
    if "eval" not in name or name == live:
        raise CaseError(
            f"refusing the database {name!r}: the eval drops and creates its database again, so its name must "
            "contain 'eval' and must not be the agent's own"
        )
    admin = psycopg.conninfo.make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with db.connect(url) as c:
        db.apply_schema(c)
        benchmark.seed(c)


def run_cases(cases: list[Case], settings: config.Settings) -> list[dict]:
    """One row per case, in order; a case that fails is a row with its error, and the rest still run."""
    from .. import home_api, mcp_app  # noqa: F401 - mcp_app: the commands register as the app loads them

    rows = []
    with db.connect(settings.database_url) as conn:
        for case in cases:
            try:
                rows.append(_run(conn, settings, case, home_api))
            except Exception as e:  # noqa: BLE001 - one case's failure must not stop the run
                rows.append({"case": case.id, "error": f"{type(e).__name__}: {e}"})
    return rows


def consult_case(conn, case: Case, home_api) -> tuple[str, str, str]:
    """The case's business and its consultation, answered as the case says: the business and its key, the case's
    knowledge and budget, the session `start` builds after its extraction, each question it asks answered from the
    case. Returns (business id, key, session id); CaseError when the consultation asks what the case does not answer."""
    from .. import tools_start  # the session start builds after its extraction (tools import mcp_app at load)

    biz = "eval_" + case.id.replace("-", "_")
    conn.execute("INSERT INTO business (id, owner_email, claim_state) VALUES (%s, %s, 'verified')", (biz, EMAIL))
    key, _rec = keys.mint(conn, business_id=biz, cls="business", scopes=("read", "manage"), label="eval")
    business.add_knowledge(conn, biz, case.knowledge, source="extract")
    if case.monthly_budget_usd:
        business.set_goal(conn, biz, monthly_limit_usd=case.monthly_budget_usd)
    payload, _say, _next = tools_start._session_payload(conn, biz, case.opening, "en-US", case.knowledge, {}, [], [])
    sid = payload["session_id"]
    for _ in range(MAX_QUESTIONS):
        left = consult.pending(consult.get(conn, biz, sid))
        if not left:
            return biz, key, sid
        qid = left[0]["id"]
        if qid not in case.answers:
            raise CaseError(f"{case.id}: the consultation asks {qid!r} and the case does not answer it")
        args = {"session_id": sid, "answers": {qid: case.answers[qid]}, "idempotency_key": f"eval-{case.id}-{qid}"}
        said = home_api.call("answer", args, key, ip=IP)
        if "error" in said:
            raise CaseError(f"{case.id}: answering {qid}: {said['error']}")
    raise CaseError(f"{case.id}: still asking after {MAX_QUESTIONS} answers")


def build_case(conn, case: Case, home_api) -> dict:
    """The case's plan, built through `recommend` after its consultation (`consult_case`): the stored plan."""
    biz, key, sid = consult_case(conn, case, home_api)
    built = home_api.call("recommend", {"session_id": sid, "idempotency_key": f"eval-{case.id}-build"}, key, ip=IP)
    if "error" in built:
        raise CaseError(f"{case.id}: building the plan: {built['error']}")
    return plans.get(conn, biz, built["data"]["plan_id"])


def ungraded(judged) -> list[str]:
    """The plan dimensions the judge's answer left without a grade: base.grades_of fills such a dimension with 1, which
    would read as a low grade (in a baseline) and as a caught flaw (in `judge`), so a caller treats a plan with any as an
    error ("NotGraded: <those dimensions>"), not as graded."""
    given = {g.dimension for g in judged.review.grades if isinstance(g.grade, int)}
    return [d for d in contracts.PLAN.ids if d not in given]


def _run(conn, settings: config.Settings, case: Case, home_api) -> dict:
    plan = build_case(conn, case, home_api)
    biz = "eval_" + case.id.replace("-", "_")
    judged = critic.judge_plan(settings, plan_review.material(conn, biz, plan, money=None, blocked=None))
    left = ungraded(judged)
    if left:
        return {"case": case.id, "error": f"NotGraded: {', '.join(left)}"}
    return {
        "case": case.id,
        "vertical": case.vertical,
        "monthly_usd": int(plans.total_usd(plan["sections"])),
        "grades": judged.grades,
        "score": judged.score,
        "verdict": judged.verdict,
        "issues": judged.issues,
    }


def frozen_of(plan: dict, settings: config.Settings, *, day: str) -> dict:
    """A built plan as the golden set keeps it (K5 P1): what the judge and the grading page read (its sections), its
    owner lines, rationale, notes and estimate basis, and what built it (the model, the effort, the image's commit, the
    day)."""
    return {
        "sections": plan["sections"],
        "lines": plan["lines"],
        "rationale": plan["rationale"],
        "notes": plan["notes"],
        "estimate_basis": plan["estimate_basis"],
        "model": settings.llm_model,
        "effort": settings.llm_effort,
        "commit": settings.build_sha,
        "built_on": day,
    }


def freeze_cases(every: list[Case], settings: config.Settings) -> tuple[dict[str, dict], dict[str, str]]:
    """Each case's plan built once with the real model and frozen (`frozen_of`), nothing judged: (the frozen plans by
    case id, the errors by case id); one case's failure never stops the rest."""
    from .. import home_api, mcp_app  # noqa: F401 - mcp_app: the commands register as the app loads them

    day = datetime.now(UTC).date().isoformat()
    frozen: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with db.connect(settings.database_url) as conn:
        for case in every:
            try:
                frozen[case.id] = frozen_of(build_case(conn, case, home_api), settings, day=day)
            except Exception as e:  # noqa: BLE001 - one case's failure must not stop the run
                errors[case.id] = f"{type(e).__name__}: {e}"
    return frozen, errors


def judge_items(
    items: list, settings: config.Settings, *, repeat: int, effort: str | None
) -> dict[str, list[dict[str, int]] | str]:
    """Each frozen plan judged `repeat` times by the plan rubric over exactly what the grading page shows
    (plan_review.material_of: no database, no money, no launch refusal); a plan whose judge could not answer is its
    error ("LLMError: ..."), and so is one whose answer left a dimension ungraded ("NotGraded: <those dimensions>":
    base.grades_of fills a dimension the judge never gave with 1, which would read as a caught flaw and as an aligned
    low grade); the rest still run."""
    out: dict[str, list[dict[str, int]] | str] = {}
    for it in items:
        material = plan_review.material_of(it.plan, it.knowledge, final_host=None, money=None, blocked=None)
        runs: list[dict[str, int]] = []
        try:
            for _ in range(repeat):
                judged = critic.judge_plan(settings, material, effort=effort)
                left = ungraded(judged)
                if left:
                    out[it.id] = f"NotGraded: {', '.join(left)}"
                    break
                runs.append(judged.grades)
            else:
                out[it.id] = runs
        except llm.LLMError as e:
            out[it.id] = f"LLMError: {e}"
    return out


def report(rows: list[dict], settings: config.Settings) -> str:
    """The dated Markdown table: one row per case, the judge's grades per dimension, the score and the verdict; then
    the mean score. The founder's grades are not compared here: they grade the frozen plan, not this one (K5 P12;
    `agent.eval judge` measures the alignment)."""
    ids = contracts.PLAN.ids
    lines = [
        f"#### Eval — {datetime.now(UTC).date().isoformat()} — {settings.llm_model} — {len(rows)} cases",
        "",
        "| Case | Plan | " + " | ".join(ids) + " | Score | Verdict |",
        "|" + "---|" * (len(ids) + 4),
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['case']} | error: {r['error']} |" + " |" * (len(ids) + 2))
            continue
        grades = " | ".join(str(r["grades"][d]) for d in ids)
        lines.append(f"| {r['case']} | ${r['monthly_usd']:,}/mo | {grades} | {r['score']:.2f} | {r['verdict']} |")
    ok = [r for r in rows if "error" not in r]
    mean = f"{sum(r['score'] for r in ok) / len(ok):.2f}" if ok else "—"
    lines += ["", f"mean score {mean} · {len(rows) - len(ok)} errors"]
    return "\n".join(lines)
