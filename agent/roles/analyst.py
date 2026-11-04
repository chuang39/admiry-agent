"""The Analyst (spec 2026-10-07-admiry-capability-layer-design §4, §5; capability layer K2). Once a local day per
business with ads, code computes the day's figures and flags -- the digest's own modules (digest_numbers, digest_rules),
so the mail and the look never disagree -- the last 7 and 30 days per place (funnel: what get_campaign_performance
gives), the benchmark, the month's pace and the balance, and the candidate actions, each an existing public tool with
its arguments. One model call writes the Insight, which only picks among the candidates and words why: the model never
names a tool, an argument or a number of its own (plan K2 R1). The guards check its words (roles.guard), the Critic
grades it (contracts.INSIGHT) and one more round runs when it asks (§4.1).

Nothing is applied (founder 2026-10-07: no automatic actions). A recommendation is data: the owner, or their
assistant after the owner's yes, runs its action, which asks for the owner's approval itself where money is involved.
Stored once per business and local day in `insight`; an owner reads one only with AGENT_ROLES on and the Critic's
deliver (`owner_view`); with validate it is Admiry's team's alone (`team_view`)."""

from __future__ import annotations

import calendar
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from .. import (
    ads_run,
    audit,
    benchmark,
    business,
    config,
    db,
    digest_audience,
    digest_numbers,
    digest_rules,
    digest_zone,
    funnel,
    llm,
    models,
    plan_review,
    plans,
)
from ..channels import registry
from . import base, contracts, critic, guard

log = logging.getLogger(__name__)

# A candidate's kind -> the public tool that does it. Code picks candidates from the day's flags and figures; the model
# only chooses among them and words why (spec §4). Every tool is a public tool (test_every_candidate_tool_is_public).
KINDS: dict[str, str] = {
    "mark_leads": "get_leads",
    "add_funds": "get_billing",
    "raise_amount": "update_marketing_budget",
    "change_ads": "update_marketing_plan",
    "pause": "pause_campaign",
    "check_page": "get_landing_page",
    "see_issue": "get_campaigns",
    "finish_setup": "connect_account",
    "review_change": "get_marketing_state",
}
CHANGE_ADS_PCT = 150  # a place's 30-day cost per lead, as a % of the benchmark, from which changing its ads is offered
PREVIOUS_DAYS = 7  # the previous look the material carries is at most this old (R14)


@dataclass(frozen=True)
class Candidate:
    """One action an insight may recommend: its id (`kind` or `kind:channel`), the public tool and its arguments --
    the ones that pick this business's thing; the assistant adds its own idempotency key and asks the owner for
    anything else the tool needs (an amount) -- and the owner's label."""

    id: str
    kind: str
    tool: str
    args: dict
    label: str
    channel: str | None = None


def category_of(conn: psycopg.Connection, business_id: str) -> str:
    """The business's trade as the benchmark reads it: the latest knowledge's category, else fallback."""
    latest = business.latest_knowledge(conn, business_id)
    return ((latest[1] if latest else None) or {}).get("category") or "fallback"


def inputs(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, day: date, zone, aud
) -> tuple[digest_numbers.Numbers, list[digest_rules.Item], digest_rules.Ctx]:
    """The day's figures and flags exactly as the digest builds them (one source for the mail and the look); a
    Monday's edition carries the week."""
    weekly = aud.edition == "weekly"
    numbers = digest_numbers.gather(conn, business_id, aud, day, zone, weekly=weekly)
    start, end = digest_zone.bounds(day, zone)
    ctx = digest_rules.Ctx(
        conn=conn,
        settings=settings,
        business_id=business_id,
        day=day,
        zone=zone,
        start=start,
        end=end,
        aud=aud,
        numbers=numbers,
        category=category_of(conn, business_id),
        weekly=weekly,
    )
    return numbers, digest_rules.items(ctx), ctx


def _channel_of(ref: str) -> str | None:
    """The channel a flag's ref names (`limit:google_search`, `code:google_search:X`), when it names a known one."""
    parts = ref.split(":")
    return parts[1] if len(parts) > 1 and registry.known(parts[1]) else None


def candidates(
    items: list[digest_rules.Item],
    channels: list[dict],
    aud: digest_audience.Audience,
    *,
    plan_id: str | None = None,
) -> list[Candidate]:
    """The actions the day's flags and figures call for, each once: in the flags' order (their severity first), then
    the places whose 30-day cost per lead runs CHANGE_ADS_PCT % of the benchmark or more on a solid sample. A flag the
    owner can do nothing about gives none: a place spending little because few people search, a safe action Admiry
    took, a disputed payment (a person handles it). A change to the ads names the approved plan (`plan_id`):
    update_marketing_plan reads a message with no plan as the answer to a consultation's waiting question, so with no
    approved plan there is no change to offer."""
    labels = {c["channel"]: c["label"] for c in channels}
    reasons = {c.channel: c.paused_reason for c in aud.channels}
    found: dict[str, Candidate] = {}

    def add(kind: str, label: str, *, channel: str | None = None, args: dict | None = None) -> None:
        c = Candidate(
            id=f"{kind}:{channel}" if channel else kind,
            kind=kind,
            tool=KINDS[kind],
            args=dict(args or {}),
            label=label,
            channel=channel,
        )
        found.setdefault(c.id, c)

    def change_ads(channel: str) -> None:
        if plan_id is None:
            return
        name = labels.get(channel) or registry.label(channel)
        message = f"Change the {name} ads: they are not bringing enough calls and forms for what they spend."
        args = {"message": message, "plan_id": plan_id}
        add("change_ads", f"Ask Admiry to change the {name} ads", channel=channel, args=args)

    for item in items:
        ch = _channel_of(item.ref)
        name = (labels.get(ch) or registry.label(ch)) if ch else None
        reason = reasons.get(ch) if item.rule == "paused_by_system" else None
        if item.rule == "leads_unmarked":
            add("mark_leads", "Say what the leads became", args={"awaiting_outcome": True})
        elif item.rule == "funds_low" or reason == "no_funds":
            add("add_funds", "Add funds", args={"view": "topup_link"})
        elif ch and (item.rule == "limit_near" or reason == "month_cap"):
            add("raise_amount", f"Raise the monthly amount for {name}", channel=ch, args={"channel": ch})
        elif ch and (item.rule == "no_leads_spend" or reason == "no_leads"):
            change_ads(ch)
            if item.rule == "no_leads_spend":
                add("pause", f"Pause {name}", channel=ch, args={"channel": ch})
        elif item.rule == "tracking_silent" or reason in ads_run.PAGE_REASONS:
            add("check_page", "Check the page your ads send people to", args={"view": "check"})
        elif item.rule == "channel_issue":
            add("see_issue", f"See what {name} said" if name else "See what the platform said", channel=ch)
        elif ch and item.rule == "waiting_account":
            add("finish_setup", f"Finish connecting {name}", channel=ch, args={"channel": ch})
        elif item.rule == "proposal_pending":
            add("review_change", "Look at the change waiting for your yes")
    for c in channels:
        pct, last30 = c.get("cost_per_lead_vs_benchmark_pct"), c.get("last_30_days")
        if pct is not None and pct >= CHANGE_ADS_PCT and last30 and last30.get("confidence") == "solid":
            change_ads(c["channel"])
    return list(found.values())


def _window(row: dict | None) -> dict | None:
    """A place's figures over a funnel window, money as the owner reads it."""
    if row is None:
        return None
    cpl = row["cost_per_lead_usd"]
    return {
        "spend": plans.usd(row["spend_usd"]),
        "leads": row["leads"],
        "booked": row["booked"],
        "cost_per_lead": plans.usd(cpl) if cpl is not None else None,
        "confidence": row["confidence"],
    }


def _pace_pct(spent_cents: int, amount_cents: int | None, day: date) -> int | None:
    """The month's spend to the end of `day` as a % of what an even month allows by then; None without an amount."""
    if not amount_cents:
        return None
    allowed = amount_cents * day.day / calendar.monthrange(day.year, day.month)[1]
    return round(100 * spent_cents / allowed)


def _week(week) -> dict | None:
    """The Monday edition's week against the week before, per place, money as the owner reads it."""
    if week is None:
        return None
    usd = digest_numbers.usd
    return {
        "rows": [
            {
                "label": r.label,
                "spend": usd(r.spend_cents),
                "leads": r.leads,
                "calls": r.calls,
                "booked": r.booked,
                "spend_week_before": usd(r.prior_spend_cents),
                "leads_week_before": r.prior_leads,
                "calls_week_before": r.prior_calls,
                "booked_week_before": r.prior_booked,
                "cost_per_lead": plans.usd(r.cost_per_lead_usd) if r.cost_per_lead_usd is not None else None,
                "sample": r.sample_word,
            }
            for r in week.rows
        ],
        "best_channel": registry.label(week.best_channel) if week.best_channel else None,
        "month_spend": usd(week.month_spend_cents),
        "month_leads": week.month_leads,
        "month_booked": week.month_booked,
    }


def _previous(conn: psycopg.Connection, business_id: str, day: date) -> dict | None:
    """The latest earlier look of the last PREVIOUS_DAYS days that was built (R14): its day in words, its summary and
    its suggestions' labels; None when there is none. A summary the guards dropped is left out (None): its numbers
    would otherwise be material today, and pass today's guard."""
    row = conn.execute(
        "SELECT day, insight FROM insight WHERE business_id = %s AND status = 'done' AND day < %s AND day >= %s "
        "ORDER BY day DESC LIMIT 1",
        (business_id, day, day - timedelta(days=PREVIOUS_DAYS)),
    ).fetchone()
    if row is None or not row["insight"]:
        return None
    d = row["day"]
    return {
        "day": f"{d:%A}, {d:%B} {d.day}",
        "summary": None if row["insight"].get("summary_dropped") else row["insight"].get("summary"),
        "suggested": [r["label"] for r in row["insight"].get("recommendations", []) if r.get("label")],
    }


def metrics(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, day: date, *, aud, numbers, items
) -> dict:
    """Everything code computes for the day's look (spec §5.1 step 2), aggregates only: per place the day, the last 7
    and 30 days (funnel, to the account's today: R9), the benchmark's cost per lead and the 30-day cost per lead as a %
    of it, the month's spend against its amount and the pace; the balance when Admiry runs the ads from it; the flags
    (the digest's items, their owner sentences); the previous look (R14); the candidates. Stored whole on the insight
    row."""
    category = category_of(conn, business_id)
    d7 = {r["channel"]: r for r in funnel.build(conn, business_id, "7d")["by_channel"]}
    d30 = {r["channel"]: r for r in funnel.build(conn, business_id, "30d")["by_channel"]}
    plan = ads_run.approved(conn, business_id)
    money = plan_review.money(conn, business_id, plan)[1] if plan else None
    usd = digest_numbers.usd
    channels = []
    for row in numbers.rows:
        bench = benchmark.lookup(conn, category, row.channel)
        last30 = d30.get(row.channel)
        cpl30 = last30["cost_per_lead_usd"] if last30 else None
        amount = numbers.channel_amount_cents.get(row.channel)
        spent = numbers.month_spend_cents.get(row.channel, 0)
        channels.append(
            {
                "channel": row.channel,
                "label": row.label,
                "state": row.state_text,
                "yesterday": {
                    "spend": usd(row.spend_cents),
                    "clicks": row.clicks,
                    "calls": row.calls,
                    "forms": row.forms,
                    "leads": row.leads,
                    "daily_budget_used_pct": (
                        round(100 * row.spend_cents / row.daily_budget_cents) if row.daily_budget_cents else None
                    ),
                },
                "last_7_days": _window(d7.get(row.channel)),
                "last_30_days": _window(last30),
                "benchmark_cost_per_lead": plans.usd(bench["cpl_avg"]) if bench else None,
                "cost_per_lead_vs_benchmark_pct": (
                    round(100 * cpl30 / bench["cpl_avg"]) if cpl30 and bench and bench["cpl_avg"] else None
                ),
                "month": {
                    "spent": usd(spent),
                    "amount": usd(amount) if amount else None,
                    "pace_pct": _pace_pct(spent, amount, numbers.day),
                },
            }
        )
    return {
        "day": numbers.day.isoformat(),
        "weekly": numbers.week is not None,
        "plan_id": plan["id"] if plan else None,
        "category": category,
        "channels": channels,
        "organic_leads": numbers.organic_leads,
        "leads_by_status": numbers.leads_by_status,
        "month": numbers.month_name,
        "money": (
            {
                "balance": plans.usd(money["balance_usd"]),
                "daily_budget": plans.usd(money["daily_usd"]),
                "days_covered": money["runway_days"],
            }
            if money and money["daily_usd"]
            else None
        ),
        "week": _week(numbers.week),
        "previous": _previous(conn, business_id, day),
        "flags": [{"rule": i.rule, "severity": i.severity, "ref": i.ref, "text": i.text} for i in items],
        "candidates": [asdict(c) for c in candidates(items, channels, aud, plan_id=plan["id"] if plan else None)],
    }


def material(m: dict, *, business_name: str) -> dict:
    """What the model reads (aggregates only): the figures and the flags' owner sentences, the day in words, each
    place by its label and each candidate as its id and label -- never a tool's name, its arguments, a channel's id or a
    lead's contact."""
    day = date.fromisoformat(m["day"])
    return {
        "business": business_name,
        "trade": m["category"],
        "day": f"{day:%A}, {day:%B} {day.day}",
        "edition": "weekly" if m["weekly"] else "daily",
        "channels": [{k: v for k, v in c.items() if k != "channel"} for c in m["channels"]],
        "organic_leads": m["organic_leads"],
        "leads_by_status": m["leads_by_status"],
        "month": m["month"],
        "money": m["money"],
        "week": m["week"],
        "previous": m["previous"],
        "flags": [{"severity": f["severity"], "text": f["text"]} for f in m["flags"]],
        "candidates": [{"id": c["id"], "label": c["label"]} for c in m["candidates"]],
    }


# ---------------------------------------------------------------- the run: one look per business and local day

BUILDING, DONE, FAILED, SKIPPED = "building", "done", "failed", "skipped"
MAX_FINDINGS, MAX_PICKS = 4, 3
RUN_SECONDS = 2400  # a shard stops starting new businesses after this long (the hour's next run picks them up)
RETENTION_DAYS = 396  # 13 months, as the digest's
OPPORTUNITY_DAYS = 3  # get_marketing_state's opportunities read a look this many days old at most
GUARD_WORDS = {
    guard.EMPTY: "it was empty",
    guard.UNGROUNDED: "it said the material gave nothing true to say",
    guard.LONG: "it was too long",
    guard.LINK: "it carried a link, an address, a phone number or markup",
    guard.NUMBER: "it stated a number the material does not hold",
    guard.JARGON: "it used advertising jargon",
}


def _get(conn: psycopg.Connection, business_id: str, day: date) -> dict | None:
    return conn.execute("SELECT * FROM insight WHERE business_id = %s AND day = %s", (business_id, day)).fetchone()


def used_today(conn: psycopg.Connection, now: datetime) -> int:
    """The Analyst's and its Critic's model calls since midnight UTC, all businesses, and the daily refusal
    summary's (refusal_summary.calls; Part B phase 1, plan D13) -- AGENT_ANALYST_MAX_PER_DAY bounds them together."""
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        "SELECT (SELECT coalesce(sum(calls), 0) FROM insight WHERE created_at >= %(m)s) "
        "+ (SELECT coalesce(sum(calls), 0) FROM refusal_summary WHERE created_at >= %(m)s) AS n",
        {"m": midnight},
    ).fetchone()["n"]


def _guarded(written: models.Insight, text: str, extra: set[Decimal], by_id: dict[str, dict]) -> tuple[dict, list[str]]:
    """The insight as stored -- the summary's guard reason (None: it may be shown), each finding and recommendation
    with its own, a recommendation's action copied from its candidate (one naming no candidate left out), at most
    MAX_FINDINGS and MAX_PICKS -- and the feedback a second round reads."""
    feedback: list[str] = []

    def checked(words: str, where: str, *, sentences: int, chars: int) -> str | None:
        why = guard.check(words, written.grounded, text, extra=extra, max_sentences=sentences, max_chars=chars)
        if why:
            feedback.append(f"{where}: {GUARD_WORDS[why]}")
        return why

    summary_dropped = checked(written.summary, "summary", sentences=3, chars=450)
    findings = [
        {"text": f.text, "dropped": checked(f.text, f"finding {n}", sentences=2, chars=200)}
        for n, f in enumerate(written.findings[:MAX_FINDINGS], 1)
    ]
    recs: list[dict] = []
    for r in written.recommendations:
        c = by_id.get(r.candidate_id.strip())
        if c is None:
            feedback.append(f"recommendation {r.candidate_id!r}: it is not one of the candidates")
            continue
        if len(recs) == MAX_PICKS or any(x["id"] == c["id"] for x in recs):
            continue
        recs.append(
            {
                "id": c["id"],
                "kind": c["kind"],
                "tool": c["tool"],
                "args": c["args"],
                "label": c["label"],
                "text": r.text,
                "confidence": r.confidence,
                "delta_usd": None,
                "dropped": checked(r.text, f"recommendation {c['id']}", sentences=2, chars=240),
            }
        )
    stored = {
        "summary": written.summary,
        "summary_dropped": summary_dropped,
        "grounded": written.grounded,
        "findings": findings,
        "recommendations": recs,
    }
    return stored, feedback


def _finish(
    conn: psycopg.Connection,
    row: dict,
    m: dict,
    *,
    status: str,
    dropped: str | None,
    stored: dict | None = None,
    judged: critic.Judged | None = None,
    calls: int = 0,
    rounds: int = 0,
) -> dict:
    """The row as built, and its one audit row (job.analyst, no business: R5 -- a validate-mode look leaves no trace
    on any owner surface, and an on-mode one is read through owner_view, never the activity log)."""
    review = (
        None
        if judged is None
        else {
            "grades": judged.grades,
            "score": judged.score,
            "verdict": judged.verdict,
            "issues": judged.issues,
            "missing": judged.review.missing,
        }
    )
    done = conn.execute(
        "UPDATE insight SET status = %s, plan_id = %s, metrics = %s, insight = %s, review = %s, score = %s, "
        "verdict = %s, dropped = %s, rounds = %s, calls = %s, finished_at = %s WHERE id = %s RETURNING *",
        (
            status,
            m.get("plan_id"),
            Jsonb(m),
            Jsonb(stored) if stored is not None else None,
            Jsonb(review) if review is not None else None,
            judged.score if judged else None,
            judged.verdict if judged else None,
            dropped,
            rounds,
            calls,
            ads_run.now(),
            row["id"],
        ),
    ).fetchone()
    audit.record_job(
        conn,
        job="analyst-run",
        tool="job.analyst",
        business_id=None,
        args={
            "business": row["business_id"],
            "day": row["day"].isoformat(),
            "status": status,
            "calls": calls,
            "verdict": done["verdict"],
        },
        result={},
    )
    return done


def _build(conn, settings, biz: dict, row: dict, *, now: datetime, aud, numbers, items) -> dict:
    """The day's look, written and graded: at most contracts.ANALYST.max_rounds rounds -- a second when the Critic asks
    for one (revise, fetch_more: its issues and what it missed as feedback, no new material in K2) or when the guard
    dropped the summary (R6)."""
    m = metrics(conn, settings, biz["id"], row["day"], aud=aud, numbers=numbers, items=items)
    if used_today(conn, now) + 2 > settings.analyst_max_per_day:
        return _finish(conn, row, m, status=SKIPPED, dropped="budget")
    latest = business.latest_knowledge(conn, biz["id"])
    mat = material(m, business_name=plans.name_for(latest[1] if latest else None) or "the business")
    text = json.dumps(mat, ensure_ascii=False, default=str)
    day = row["day"]
    counts = (len(m["flags"]), len(m["channels"]), len(m["candidates"]), day.day, (day + timedelta(days=1)).day)
    extra = {Decimal(x) for x in counts}
    by_id = {c["id"]: c for c in m["candidates"]}
    try:
        gen = llm.generator(settings, effort=settings.analyst_llm_effort)
    except llm.LLMError as e:
        log.warning("analyst: no model for %s on %s: %s", biz["id"], day, e)
        return _finish(conn, row, m, status=FAILED, dropped="llm")
    stored: dict | None = None
    judged: critic.Judged | None = None
    feedback: list[str] | None = None
    calls = rounds = 0
    for _ in range(contracts.ANALYST.max_rounds):
        calls += 1
        try:
            written = gen.write_insight(mat, feedback)
        except llm.LLMError as e:
            log.warning("analyst: no insight for %s on %s: %s", biz["id"], day, e)
            break
        rounds += 1
        stored, feedback = _guarded(written, text, extra, by_id)
        calls += 1
        try:
            judged = critic.judge_insight(settings, mat, written.model_dump())
        except llm.LLMError as e:
            log.warning("analyst: no review for %s on %s: %s", biz["id"], day, e)
            judged = None
            break
        if judged.verdict == base.DELIVER and not stored["summary_dropped"]:
            break
        feedback = [
            *feedback,
            *(f"{i['dimension']}: {i['text']}" for i in judged.issues),
            *(f"missing: {w}" for w in judged.review.missing),
        ]
    if stored is None:
        return _finish(conn, row, m, status=FAILED, dropped="llm", calls=calls, rounds=rounds)
    if judged is None:
        dropped = "review"
    else:
        dropped = None if judged.verdict == base.DELIVER else judged.verdict
    return _finish(conn, row, m, status=DONE, dropped=dropped, stored=stored, judged=judged, calls=calls, rounds=rounds)


def ensure(
    conn: psycopg.Connection, settings: config.Settings, biz: dict, day: date, *, now: datetime, aud, numbers, items
) -> dict | None:
    """The business's insight for `day`: the stored one when it is built (any status but building); else, with
    AGENT_ROLES validate or on, built now. The caller holds the business's daily lock (digest_zone.daily_lock), so a
    `building` row it finds is a crashed run's and is built again (R12). None with the roles off. A model that could
    not answer is a `failed` row for the day, never an exception: the digest goes without the take -- and so is any
    other error building it (`dropped = error`): never a `building` row each later hour builds again."""
    if settings.roles_mode == "off":
        return None
    row = _get(conn, biz["id"], day)
    if row is not None and row["status"] != BUILDING:
        return row
    if row is None:
        row = conn.execute(
            "INSERT INTO insight (id, business_id, day, mode, status, created_at) "
            "VALUES (%s, %s, %s, %s, 'building', %s) ON CONFLICT (business_id, day) DO NOTHING RETURNING *",
            (db.new_id("in"), biz["id"], day, settings.roles_mode, now),
        ).fetchone()
        if row is None:  # claimed by a connection that does not hold the lock: leave it to that one
            return _get(conn, biz["id"], day)
    try:
        return _build(conn, settings, biz, row, now=now, aud=aud, numbers=numbers, items=items)
    except Exception:  # a look that cannot be built is the day's failed look, never a held-up report
        log.exception("analyst: the look for %s on %s failed", biz["id"], day)
        return _finish(conn, row, {}, status=FAILED, dropped="error")


def owner_view(settings: config.Settings, row: dict | None, *, preview: bool = False) -> dict | None:
    """What an owner may read of an insight: with AGENT_ROLES on, a built look the Critic delivered -- its summary and
    each sentence the guards passed, each recommendation with its action; None otherwise (validate keeps every look
    for the team). `preview` (the operator's digest preview only) skips the roles and the verdict, never the guards."""
    if row is None or row["status"] != DONE or not row["insight"]:
        return None
    if not preview and (settings.roles_mode != "on" or row["mode"] != "on" or row["dropped"] is not None):
        return None  # a look built while the roles were validate stays the team's after the switch (R5)
    ins = row["insight"]
    view = {
        "day": row["day"].isoformat(),
        "summary": None if ins.get("summary_dropped") else ins.get("summary"),
        "findings": [f["text"] for f in ins.get("findings", []) if not f.get("dropped")],
        "recommendations": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "text": r["text"],
                "confidence": r["confidence"],
                "action": {"tool": r["tool"], "args": r["args"], "label": r["label"]},
            }
            for r in ins.get("recommendations", [])
            if not r.get("dropped")
        ],
    }
    return view if view["summary"] or view["findings"] or view["recommendations"] else None


def team_view(row: dict) -> dict:
    """Everything stored, for a platform key and the operator's commands: the figures, the flags, the candidates, the
    words with their guard reasons, the review, why an owner sees none of it."""
    return {k: v.isoformat() if isinstance(v, date | datetime) else v for k, v in row.items()}


def opportunities(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> list[dict]:
    """get_marketing_state's `opportunities` ("the open bundle once M6 does", gateway spec §5; plan R2): the
    recommendations of the latest built look, when an owner may read it and it is at most OPPORTUNITY_DAYS old, each
    with its day; [] otherwise."""
    since = ads_run.now().date() - timedelta(days=OPPORTUNITY_DAYS)
    row = conn.execute(
        "SELECT * FROM insight WHERE business_id = %s AND status = 'done' AND day >= %s ORDER BY day DESC LIMIT 1",
        (business_id, since),
    ).fetchone()
    view = owner_view(settings, row)
    return [{**r, "day": view["day"]} for r in view["recommendations"]] if view else []


def rebuild(conn: psycopg.Connection, settings: config.Settings, business_id: str, day: date) -> dict | None:
    """agent.ops insight-rebuild (dev only; the command refuses on prod): `day`'s look built again now, whatever the
    hour. None when the business's ads did not run that day or the roles are off."""
    biz = conn.execute("SELECT id, paused_reason FROM business WHERE id = %s", (business_id,)).fetchone()
    zone = digest_zone.zone_of(conn, business_id)
    aud = digest_audience.audience(conn, {**biz, "digest_frequency": "daily"}, day, zone)
    if aud.edition is None:
        return None
    numbers, items, _ = inputs(conn, settings, business_id, day, zone, aud)
    with digest_zone.daily_lock(conn, business_id) as held:
        if not held:
            raise RuntimeError("another run holds this business; try again in a minute")
        conn.execute("DELETE FROM insight WHERE business_id = %s AND day = %s", (business_id, day))
        return ensure(conn, settings, biz, day, now=ads_run.now(), aud=aud, numbers=numbers, items=items)


def _one(conn: psycopg.Connection, settings: config.Settings, biz: dict, now: datetime) -> str:
    """One business under its lock: its local yesterday's look when due (the digest's rule, R4), else why not."""
    zone = digest_zone.ensure(conn, biz["id"])
    day = digest_zone.reported_day(now, zone)
    hour = digest_zone.local_hour(now, zone)
    if not digest_zone.DUE_FROM_H <= hour < digest_zone.STALE_AT_H:
        return "not_due"
    had = _get(conn, biz["id"], day)
    if had is not None and had["status"] != BUILDING:
        return "had"
    aud = digest_audience.audience(conn, {**biz, "digest_frequency": "daily"}, day, zone)
    if aud.edition is None:
        return "quiet"
    since = digest_zone.bounds(day + timedelta(days=1), zone)[0]
    if hour < digest_zone.CUTOFF_H and not digest_audience.gate(conn, settings, biz["id"], since):
        return "gated"
    numbers, items, _ = inputs(conn, settings, biz["id"], day, zone, aud)
    row = ensure(conn, settings, biz, day, now=now, aud=aud, numbers=numbers, items=items)
    return row["status"] if row is not None else "locked"


def analyst_run(conn: psycopg.Connection, i: int, n: int) -> dict:
    """`analyst-run` (hourly, :02; plan R4): thirteen-month-old looks go first, whatever the switch; then, with
    AGENT_ROLES validate or on, each business with an approved plan and ads gets its look at its local yesterday once
    that day is due, under the business's daily lock (a business another run holds is left to it)."""
    from ..jobs import shard_sql  # jobs imports this module: a module-level import would be a cycle

    settings = config.load()
    now = ads_run.now()
    keys = ("purged", "considered", "not_due", "had", "quiet", "gated", "locked", BUILDING, DONE, FAILED, SKIPPED)
    out: dict = {**dict.fromkeys(keys, 0), "errors": 0, "mode": settings.roles_mode}
    cut = (now - timedelta(days=RETENTION_DAYS)).date()
    out["purged"] = conn.execute(
        f"DELETE FROM insight WHERE day < %(cut)s AND {shard_sql('business_id')}", {"cut": cut, "i": i, "n": n}
    ).rowcount
    if settings.roles_mode == "off":
        return out
    rows = conn.execute(
        "SELECT b.id, b.paused_reason FROM business b WHERE b.status = 'active' "
        "AND EXISTS (SELECT 1 FROM plan p WHERE p.business_id = b.id AND p.status = 'approved') "
        "AND EXISTS (SELECT 1 FROM channel_campaign c WHERE c.business_id = b.id) "
        f"AND {shard_sql('b.id')} ORDER BY b.id",
        {"i": i, "n": n},
    ).fetchall()
    started = time.monotonic()
    for biz in rows:
        if time.monotonic() - started > RUN_SECONDS:
            break
        out["considered"] += 1
        with digest_zone.daily_lock(conn, biz["id"]) as held:
            if not held:
                out["locked"] += 1
                continue
            try:
                out[_one(conn, settings, biz, now)] += 1
            except Exception as e:  # noqa: BLE001 - one business never stops the run
                log.warning("analyst-run: business %s failed: %s", biz["id"], type(e).__name__)
                out["errors"] += 1
    return out
