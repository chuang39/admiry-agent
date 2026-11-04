"""The daily refusal summary (spec 2026-10-12-admiry-google-refusal-knowledge-design §2.4; Part B phase 1): the job
`refusal-summary` (daily, 03:37 UTC: the dev DAG bidx_agent_daily, prod bidx-agent-refusal-summary.timer) reads
yesterday's `ads_refusal` rows and the live disapprovals ads-sync keeps (channel_campaign.resources.issues.ads, plan
D12) and writes one `refusal_summary` row: counts by topic, class and action; the ad-text and search-phrase lines
refused; the active rules' hits (rows the Reviewer stamped, local_rules.stamp) and the misses (lines no rule matched);
and up to MAX_PROPOSALS proposed rules from one model call (llm.propose_rules) under the Analyst's daily budget
(roles/analyst.used_today, plan D13) -- each re-verified in code before a row is written (`_accept`, plan D14): the
value, the evidence the rule actually matches, the thresholds (spec: never one business's single refusal), a trademark
warn only, a key never proposed twice. A `warn` rule is active at once, a `block` one waits for a person on /ops/rules.
With no model available the counts are still written. A rerun for the same day rewrites the row (plan D11)."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from . import ads_refusal, config, db, llm, local_rules
from .roles import analyst
from .util import iso

log = logging.getLogger(__name__)
JOB = "refusal-summary"
MIN_BUSINESSES, MIN_REFUSALS = 2, 3  # spec §2.4: a rule needs lines of ≥2 businesses, or ≥3 lines
MAX_PROPOSALS = 5
MAX_LINES = 200  # what the model reads at most
WHY_MAX, NOTE_MAX = 160, 200
LINE_CLASSES = ("ad_text", "keyword")  # the rows with a line a rule could catch
# what the one model call did (refusal_summary.model)
PROPOSED, NOTHING, BUDGET, NO_MODEL, FAILED = "proposed", "nothing_to_propose", "budget", "no_model", "failed"
SUMMARY_COLS = "id, day, counts, lines, rules, proposals, model, calls, created_at"


def _now() -> datetime:
    return datetime.now(UTC)


def job(conn: psycopg.Connection, i: int, n: int) -> dict:
    """`refusal-summary` (jobs.JOBS): yesterday's summary, on shard 0 alone -- one row a day, whichever replica."""
    if i != 0:
        return {"skipped": "shard"}
    return run(conn, config.load())


def run(
    conn: psycopg.Connection, settings: config.Settings, *, day: date | None = None, now: datetime | None = None
) -> dict:
    """One day's summary, written (upsert on `day`): `day` defaults to yesterday UTC, `now` to the clock (the
    budget's midnight). Returns what the job_run ledger keeps."""
    now = now or _now()
    day = day or (now.astimezone(UTC).date() - timedelta(days=1))
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows = conn.execute(
        "SELECT id, business_id, kind, text, topic, class, action, rule_id FROM ads_refusal "
        "WHERE created_at >= %s AND created_at < %s ORDER BY created_at, id",
        (start, end),
    ).fetchall()
    lines = [
        {k: r[k] for k in ("id", "business_id", "kind", "text", "topic", "class", "action", "rule_id")}
        for r in rows
        if r["text"] and r["class"] in LINE_CLASSES
    ]
    counts = {
        "refusals": len(rows),
        "businesses": len({r["business_id"] for r in rows}),
        "by_topic": dict(Counter(r["topic"] or "none" for r in rows)),
        "by_class": dict(Counter(r["class"] for r in rows)),
        "by_action": dict(Counter(r["action"] for r in rows)),
        "live": _live(conn, start),
    }
    hits = Counter(line["rule_id"] for line in lines if line["rule_id"])
    misses = [line for line in lines if not line["rule_id"]]
    rules = {"hits": dict(hits), "misses": len(misses)}
    model, calls, proposed = _propose(conn, settings, day, misses, now=now)
    conn.execute(
        "INSERT INTO refusal_summary (id, day, counts, lines, rules, proposals, model, calls) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (day) DO UPDATE SET counts = EXCLUDED.counts, "
        "lines = EXCLUDED.lines, rules = EXCLUDED.rules, proposals = refusal_summary.proposals || EXCLUDED.proposals, "
        "model = EXCLUDED.model, calls = refusal_summary.calls + EXCLUDED.calls, created_at = now()",
        (db.new_id("rs"), day, Jsonb(counts), Jsonb(lines), Jsonb(rules), Jsonb(proposed), model, calls),
    )
    return {
        "day": day.isoformat(),
        "refusals": len(rows),
        "lines": len(lines),
        "hits": sum(hits.values()),
        "misses": len(misses),
        "live": counts["live"]["ads"],
        "model": model,
        "proposed": proposed,
    }


def _live(conn: psycopg.Connection, start: datetime) -> dict:
    """The disapproved ads ads-sync still keeps on Google Search rows synced since the day began (plan D12): ads,
    businesses, and Google's topics -- counts alone, no text."""
    rows = conn.execute(
        "SELECT resources->'issues'->'ads' AS ads FROM channel_campaign WHERE channel = %s AND synced_at >= %s "
        "AND (resources->'issues') ? 'ads'",
        (ads_refusal.GOOGLE_SEARCH, start),
    ).fetchall()
    by_topic: Counter = Counter()
    ads = 0
    for r in rows:
        for ad in r["ads"] or []:
            ads += 1
            for t in ad.get("topics") or []:
                if isinstance(t, dict) and t.get("topic"):
                    by_topic[t["topic"]] += 1
    return {"ads": ads, "businesses": len(rows), "by_topic": dict(by_topic)}


def _propose(
    conn: psycopg.Connection, settings: config.Settings, day: date, misses: list[dict], *, now: datetime
) -> tuple[str, int, list[str]]:
    """The one model call and what came of it: (model outcome, calls made, the rule ids written)."""
    if not misses:
        return NOTHING, 0, []
    if analyst.used_today(conn, now) + 1 > settings.analyst_max_per_day:
        log.info("refusal-summary: the Analyst's day is spent; no proposals for %s", day)
        return BUDGET, 0, []
    try:
        gen = llm.generator(settings, effort=settings.analyst_llm_effort)
    except llm.LLMError as e:
        log.warning("refusal-summary: no model for %s: %s", day, e)
        return NO_MODEL, 0, []
    try:
        answer = gen.propose_rules(_material(conn, day, misses))
    except llm.LLMError as e:
        log.warning("refusal-summary: the model did not answer for %s: %s", day, e)
        return FAILED, 1, []
    by_id = {line["id"]: line for line in misses}
    written: list[str] = []
    for p in answer.proposals[:MAX_PROPOSALS]:
        try:
            row, verdict = _accept(conn, p, by_id)
            if row is None:
                log.info("refusal-summary: a proposal dropped (%s): %s %r", verdict, p.kind, p.value)
                continue
            written.append(_insert(conn, row, day))
        except Exception:  # one bad proposal must not lose the day's row
            log.exception("refusal-summary: a proposal raised; dropped for %s", day)
    return PROPOSED, 1, written


def _material(conn: psycopg.Connection, day: date, misses: list[dict]) -> dict:
    """What the model reads (plan D15): the missed lines with the business as b1, b2, … (no name, no id), the shapes,
    the rules already active (whatever the switch says: the model must not propose them again), the thresholds."""
    refs: dict[str, str] = {}
    lines = []
    for line in misses[:MAX_LINES]:
        ref = refs.setdefault(line["business_id"], f"b{len(refs) + 1}")
        lines.append(
            {
                "id": line["id"],
                "business": ref,
                "kind": line["kind"],
                "text": line["text"],
                "topic": line["topic"],
                "class": line["class"],
            }
        )
    active = conn.execute(
        "SELECT kind, applies_to, value, level FROM refusal_rule WHERE status = %s ORDER BY created_at, id",
        (local_rules.ACTIVE,),
    ).fetchall()
    return {
        "day": day.isoformat(),
        "lines": lines,
        "shapes": [{"id": s.id, "looks_like": s.tell} for s in local_rules.SHAPES.values()],
        "active_rules": [dict(r) for r in active],
        "needs": {"businesses": MIN_BUSINESSES, "refusals": MIN_REFUSALS},
    }


def _accept(conn: psycopg.Connection, p, by_id: dict[str, dict]) -> tuple[dict | None, str]:
    """One proposal checked again in code (plan D14): (the row to write, "ok"), or (None, why not)."""
    try:
        value = local_rules.validate(p.kind, p.value)
    except ValueError as e:
        return None, str(e)
    if p.applies_to not in local_rules.APPLIES or p.level not in local_rules.LEVELS:
        return None, "shape"
    rule = local_rules.compile_rule(
        {"kind": p.kind, "applies_to": p.applies_to, "value": value, "level": p.level, "why": p.why, "topic": p.topic}
    )
    evidence = [
        by_id[i]
        for i in dict.fromkeys(p.line_ids)
        if i in by_id and local_rules.matches(rule, by_id[i]["kind"], by_id[i]["text"])
    ]
    count, businesses = len(evidence), len({e["business_id"] for e in evidence})
    if businesses < MIN_BUSINESSES and count < MIN_REFUSALS:
        return None, "thin"
    exists = conn.execute(
        "SELECT 1 FROM refusal_rule WHERE kind = %s AND applies_to = %s AND value = %s", (p.kind, p.applies_to, value)
    ).fetchone()
    if exists:
        return None, "exists"
    # the model's topic is trusted only when the evidence itself carries it (AA1 #2); otherwise the topic the
    # evidence shares (every row the same), else none -- never an invented topic (e.g. a stray "TRADEMARK...").
    topics = {(e["topic"].strip().upper() if e["topic"] else None) for e in evidence}
    want = p.topic.strip().upper() if p.topic else None
    topic = want if want in topics else (next(iter(topics)) if len(topics) == 1 else None)
    level = local_rules.WARN if "TRADEMARK" in (topic or "") else p.level
    # the owner reads only Admiry's own sentence for the class (AA1 #1): the model's free text could carry another
    # business's line, a Google code, or a URL. The model's sentence is kept for the admin alone, under evidence.
    why = ads_refusal.why(evidence[0]["class"], topic)
    model_why = " ".join((p.why or "").split())[:WHY_MAX]
    evidence_row = {
        "refusal_ids": [e["id"] for e in evidence],
        "count": count,
        "businesses": businesses,
        "note": " ".join((p.reason or "").split())[:NOTE_MAX],
    }
    if model_why:
        evidence_row["model_why"] = model_why
    return {
        "kind": p.kind,
        "applies_to": p.applies_to,
        "value": value,
        "topic": topic,
        "why": why,
        "level": level,
        "evidence": evidence_row,
    }, "ok"


def _insert(conn: psycopg.Connection, row: dict, day: date) -> str:
    """A `warn` rule active at once, a `block` one proposed (spec §2.2); proposed by this summary."""
    rule_id = db.new_id("rr")
    status = local_rules.ACTIVE if row["level"] == local_rules.WARN else local_rules.PROPOSED
    conn.execute(
        "INSERT INTO refusal_rule (id, kind, applies_to, value, topic, why, level, status, evidence, proposed_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            rule_id,
            row["kind"],
            row["applies_to"],
            row["value"],
            row["topic"],
            row["why"],
            row["level"],
            status,
            Jsonb(row["evidence"]),
            f"summary:{day.isoformat()}",
        ),
    )
    return rule_id


def latest(conn: psycopg.Connection, days: int = 14) -> list[dict]:
    """The last `days` summaries, newest first, for /ops/rules and the MCP: dates as ISO text."""
    rows = conn.execute(f"SELECT {SUMMARY_COLS} FROM refusal_summary ORDER BY day DESC LIMIT %s", (days,)).fetchall()
    return [{**dict(r), "day": r["day"].isoformat(), "created_at": iso(r["created_at"])} for r in rows]
