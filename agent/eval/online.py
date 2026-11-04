"""Admiry's online numbers (spec 2026-10-07 capability layer §7; K5 P11): what the tables that exist say about how plans
and looks land, for the operator (`python -m agent.ops eval-online`). Aggregates only -- no business is named. Reads
only. Approximations: a suggestion counts as followed when its tool ran for the business within FOLLOW_DAYS of the
look (no decline is recorded anywhere), and only looks that old are counted at all; the cost per lead is Google
Search's own against its benchmark (the other channels have none to match here); the research numbers are K4's
research_run: the runs created in the window by confidence (a run that did not finish has none, so it counts as
"None") and the Critic's verdicts on them, counted as those on the plans and the daily looks are. A database with no
research_run table has no research numbers: the line says so."""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta

import psycopg
from psycopg import sql

from .. import benchmark, funnel
from ..roles import analyst
from ..roles import base as role_base

log = logging.getLogger(__name__)

GOOGLE_SEARCH = "google_search"
FOLLOW_DAYS = 3
SECOND_TRIES = (role_base.REVISE, role_base.FETCH_MORE)


def metrics(conn: psycopg.Connection, *, days: int, now: datetime) -> dict:
    since = now - timedelta(days=days)
    researched = _has_research_run(conn)
    return {
        "days": days,
        "since": since.date().isoformat(),
        "until": now.date().isoformat(),
        "approval": _approval(conn, since),
        "to_launch_hours": _to_launch(conn, since),
        "changes_before_approval": _changes(conn, since),
        "thumbs": _thumbs(conn, since),
        "cpl": {period: _cpl(conn, period) for period in ("7d", "30d")},
        "critic": {
            "plan": _verdicts(conn, "plan_review", since),
            "insight": _verdicts(conn, "insight", since),
            "research": _verdicts(conn, "research_run", since) if researched else {},
        },
        "research": _research(conn, since) if researched else None,
        "suggestions": _suggestions(conn, since, now),
    }


def _approval(conn, since: datetime) -> dict:
    row = conn.execute(
        "WITH first AS (SELECT business_id, min(created_at) AS at FROM plan GROUP BY business_id) "
        "SELECT count(*) AS planned, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM proposal p WHERE "
        "p.business_id = first.business_id AND p.kind = 'approve_plan' AND p.status = 'confirmed')) AS approved "
        "FROM first WHERE at >= %s",
        (since,),
    ).fetchone()
    return {"planned": row["planned"], "approved": row["approved"]}


def _to_launch(conn, since: datetime) -> list[float]:
    rows = conn.execute(
        "SELECT EXTRACT(EPOCH FROM (l.at - b.created_at)) / 3600.0 AS hours FROM business b JOIN "
        "(SELECT business_id, min(launched_at) AS at FROM channel_campaign WHERE launched_at IS NOT NULL "
        "GROUP BY business_id) l ON l.business_id = b.id WHERE l.at >= %s",
        (since,),
    ).fetchall()
    return sorted(round(float(r["hours"]), 1) for r in rows)


def _changes(conn, since: datetime) -> list[int]:
    rows = conn.execute(
        "SELECT (SELECT count(*) FROM plan v WHERE v.business_id = a.business_id AND v.created_at <= a.at) - 1 AS n "
        "FROM (SELECT business_id, min(resolved_at) AS at FROM proposal WHERE kind = 'approve_plan' "
        "AND status = 'confirmed' GROUP BY business_id) a WHERE a.at >= %s",
        (since,),
    ).fetchall()
    return sorted(max(0, int(r["n"])) for r in rows)


def _thumbs(conn, since: datetime) -> dict:
    rows = conn.execute("SELECT rating, reasons FROM plan_feedback WHERE created_at >= %s", (since,)).fetchall()
    down = [r for r in rows if r["rating"] == "down"]
    return {
        "votes": len(rows),
        "down": len(down),
        "reasons": dict(Counter(x for r in down for x in r["reasons"] or [])),
    }


def _google_search(conn, biz: str, period: str) -> dict:
    """The business's Google Search row of the period's report (`by_channel`: that channel's spend and the leads its
    own clicks brought); empty when nothing ran there, or (so it is left out) when its report cannot be built. The
    log line names no business: these numbers are aggregates."""
    try:
        rows = funnel.build(conn, biz, period)["by_channel"]
    except Exception:  # one business's report must not stop the operator's numbers
        log.warning(
            "eval-online: a business's %s report failed; it is left out of the cost per lead", period, exc_info=True
        )
        return {}
    return next((r for r in rows if r["channel"] == GOOGLE_SEARCH), {})


def _cpl(conn, period: str) -> dict:
    """Each business with an approved plan that spent on Google Search in the period: that channel's cost per lead
    against its trade's Google Search benchmark (the blend of all its channels is no fair match for it); the median
    ratio, how many it rests on, and how many spent there with no lead."""
    ratios: list[float] = []
    silent = 0
    for r in conn.execute("SELECT DISTINCT business_id FROM plan WHERE status = 'approved'").fetchall():
        biz = r["business_id"]
        f = _google_search(conn, biz, period)
        if not f.get("spend_usd"):
            continue
        if not f.get("leads"):
            silent += 1
            continue
        row = benchmark.lookup(conn, analyst.category_of(conn, biz), GOOGLE_SEARCH)
        if row and row["cpl_avg"] and f.get("cost_per_lead_usd") is not None:
            ratios.append(f["cost_per_lead_usd"] / row["cpl_avg"])
    median = round(statistics.median(ratios), 2) if ratios else None
    return {"businesses": len(ratios), "spend_no_leads": silent, "median_ratio": median}


def _verdicts(conn, table: str, since: datetime) -> dict:
    query = sql.SQL(
        "SELECT verdict, count(*) AS n FROM {} WHERE created_at >= %s AND verdict IS NOT NULL GROUP BY verdict"
    ).format(sql.Identifier(table))
    return {r["verdict"]: r["n"] for r in conn.execute(query, (since,)).fetchall()}


def _has_research_run(conn) -> bool:
    """Whether this database has K4's table (the agent creates it when it starts): one it never started on has not."""
    return conn.execute("SELECT to_regclass('research_run') IS NOT NULL AS present").fetchone()["present"]


def _research(conn, since: datetime) -> dict:
    """The research runs created in the window by confidence (high, medium, low); a run that did not finish has none,
    and is counted under "None"."""
    rows = conn.execute(
        "SELECT confidence, count(*) AS n FROM research_run WHERE created_at >= %s GROUP BY confidence", (since,)
    ).fetchall()
    return {str(r["confidence"]): r["n"] for r in rows}


def _suggestions(conn, since: datetime, now: datetime) -> dict:
    """The recommendations an owner could read, from looks whose follow window has closed (a look younger than
    FOLLOW_DAYS may yet be acted on, and would pull the rate down), and how many of them were followed."""
    row = conn.execute(
        "SELECT count(*) AS shown, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM agent_action a WHERE "
        "a.business_id = i.business_id AND a.surface_tool = r->>'tool' AND a.ok AND a.ts > i.finished_at "
        "AND a.ts <= i.finished_at + make_interval(days => %s))) AS followed "
        "FROM insight i CROSS JOIN LATERAL jsonb_array_elements(coalesce(i.insight->'recommendations', '[]'::jsonb)) r "
        "WHERE i.mode = 'on' AND i.status = 'done' AND i.verdict = 'deliver' AND i.dropped IS NULL "
        "AND r->>'dropped' IS NULL AND i.finished_at >= %s AND i.finished_at <= %s",
        (FOLLOW_DAYS, since, now - timedelta(days=FOLLOW_DAYS)),
    ).fetchone()
    return {"shown": row["shown"], "followed": row["followed"]}


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


def text(m: dict) -> str:
    """The numbers as the operator's lines."""
    a, hours, changes, th = m["approval"], m["to_launch_hours"], m["changes_before_approval"], m["thumbs"]
    median_hours = f"{statistics.median(hours):.1f} h" if hours else "—"
    longest = f"{max(hours):.1f} h" if hours else "—"
    median_changes = f"{statistics.median(changes):g}" if changes else "—"
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(th["reasons"].items())) or "—"
    lines = [
        f"Admiry online numbers, the last {m['days']} days ({m['since']} to {m['until']}); aggregates only",
        f"plan approval: {a['approved']} of {a['planned']} businesses with a first plan ({_pct(a['approved'], a['planned'])})",
        f"first turn to first launch: median {median_hours}, longest {longest} (n={len(hours)})",
        f"plan changes before approval: median {median_changes} (n={len(changes)})",
        f"thumbs down: {th['down']} of {th['votes']} ({_pct(th['down'], th['votes'])}); reasons: {reasons}",
    ]
    for period, c in m["cpl"].items():
        ratio = "—" if c["median_ratio"] is None else f"{c['median_ratio']:.2f}x"
        lines.append(
            f"Google Search CPL against the vertical benchmark, {period}: median {ratio} "
            f"(n={c['businesses']}; {c['spend_no_leads']} spent there with no lead)"
        )
    for name, label in (("plan", "plan looks"), ("insight", "daily looks"), ("research", "research runs")):
        v = m["critic"][name]
        total, again = sum(v.values()), sum(v.get(x, 0) for x in SECOND_TRIES)
        lines.append(f"Critic, {label}: {total} judged, revise or fetch_more {again} ({_pct(again, total)})")
    r = m["research"]
    lines.append(
        "research confidence: no research_run table on this database"
        if r is None
        else "research confidence: " + (", ".join(f"{k} {v}" for k, v in sorted(r.items())) or "none in the window")
    )
    s = m["suggestions"]
    lines.append(
        f"Analyst suggestions owners could read, from looks at least {FOLLOW_DAYS} days old: {s['shown']}; "
        f"followed within {FOLLOW_DAYS} days: {s['followed']} ({_pct(s['followed'], s['shown'])}); "
        "one not followed is not recorded as declined"
    )
    return "\n".join(lines)
