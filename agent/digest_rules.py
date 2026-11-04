"""The "needs you" items of a digest (spec 2026-09-30 §5; plan 2026-10-07-agent-k2 Task 4): only what the owner can
act on, each a fixed owner sentence -- a platform's own words and codes never reach the body (codes ride along in
`Item.codes` for the operator's preview only) -- with its link when it has one, every window measured from the end of
the reported day (never from the clock). The Analyst reads the same items as its flags (roles/analyst.py).

The Google Ads codes below are CampaignPrimaryStatusReason names checked against API v25's enum (the client's version,
agent/channels/google_search/client.py) on 2026-10-07."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import ads_run, benchmark, digest_links, digest_numbers, digest_zone, plan_review
from .channels import registry
from .digest_audience import SYSTEM_REASONS, Audience

SEVERITY = {"high": 0, "medium": 1, "info": 2}
LEADS_UNMARKED_MIN_H = 48
LEADS_UNMARKED_MAX_D = 14
PROPOSAL_WINDOW_H = 48
NO_LEADS_DAYS = 14
NO_LEADS_FLOOR_USD = 75
NO_LEADS_NO_BENCHMARK_SHARE = 0.45
TRACKING_MIN_CLICKS = 30
UNDERDELIVERY_SHARE = 0.30
UNDERDELIVERY_DAYS = 3
LIMIT_NEAR_SHARE = 0.90
LIMIT_NEAR_BEFORE_DAY = 25
FUNDS_LOW_DAYS = 3
SAFE_ACTION_TOOLS = ("job.spend_guard",)  # M6 appends its tools
# Channels whose adapter writes `channel_issue` rows (P16; plan delta D8): their issues come from those rows, never
# from `resources.issues`
ISSUE_WRITERS: frozenset[str] = frozenset({"newsbreak"})
ADS_LINK = "See your ads"  # the label of every link to the Your ads page (plan delta D9)
OPEN_ADMIRY = "Open Admiry"
# Google's CampaignPrimaryStatusReason -> "high" | "medium" | None (no item). Any code not listed is "medium". None:
# the normal state of new or budget-bound ads, nothing the owner can act on (spec §5 and Dg plan P15)
CAMPAIGN_CODES: dict[str, str | None] = {
    "HAS_ADS_DISAPPROVED": "high",
    "CALL_EXTENSION_DISAPPROVED": "high",
    "LEAD_FORM_EXTENSION_DISAPPROVED": "high",
    "BIDDING_STRATEGY_LEARNING": None,
    "BIDDING_STRATEGY_LIMITED": None,
    "BIDDING_STRATEGY_CONSTRAINED": None,
    "BUDGET_CONSTRAINED": None,
    "SEARCH_VOLUME_LIMITED": None,
    "MOST_ADS_UNDER_REVIEW": None,
    "CALL_EXTENSION_UNDER_REVIEW": None,
    "LEAD_FORM_EXTENSION_UNDER_REVIEW": None,
    "CAMPAIGN_PENDING": None,
}
# channel_issue.kind -> the owner's sentence ("{label}" filled in); a kind not listed reads GENERIC_ISSUE
ISSUE_SENTENCES: dict[str, str] = {
    "disapproval": "{label} stopped one of your ads. Open Admiry to see the reason.",
    "billing": "{label} can't charge the payment method on your account, so your ads may stop. Check your billing in "
    "{label}.",
    "verification": "{label} is asking you to verify your business before the ads can run.",
    "account": "{label} has suspended the account your ads run in. Open Admiry to see what to do.",
    "suspended": "{label} has suspended the account your ads run in. Open Admiry to see what to do.",
}
GENERIC_ISSUE = "{label} has a problem with your ads. Open Admiry to see what to do."
ADS_STOPPED = "{label} stopped one or more of your ads. Open Admiry to see the reason."
CODE_HIGH = "{label} stopped some of your ads. Open Admiry to see the reason."
CODE_MEDIUM = "{label} is holding back some of your ads; Admiry is looking into it."
PAUSE_SENTENCES = {
    "month_cap": "{label} is paused until {next_month} 1: {month}'s {amount} is used. Raise the limit to keep it running.",
    "no_funds": "{label} is paused: there is no money left to run it. Add funds to start it again.",
    "no_leads": "{label} is paused: it spent {spend} over 14 days without a lead.",
    "dispute": "{label} is paused while a card payment is disputed.",
    "page_down": "{label} is paused: the page your ads send people to is not answering. Admiry checks it every hour "
    "and starts the ads again when it answers.",
    "unreachable": "{label} is paused: the page your ads send people to is not answering. Admiry checks it every hour "
    "and starts the ads again when it answers.",
}
PAUSE_SEVERITY = {"month_cap": "info"}  # every other pause of Admiry's is high


@dataclass(frozen=True)
class Item:
    rule: str
    severity: str
    text: str
    link: tuple[str, str] | None
    ref: str
    codes: tuple[str, ...] = ()  # raw platform codes: the operator's preview only, never the body


@dataclass(frozen=True)
class Ctx:
    conn: Any
    settings: Any
    business_id: str
    day: date
    zone: ZoneInfo
    start: datetime
    end: datetime
    aud: Audience
    numbers: digest_numbers.Numbers
    category: str
    weekly: bool


def _ads(ctx: Ctx) -> tuple[str, str]:
    return ADS_LINK, digest_links.ads_url(ctx.settings, ctx.business_id)


def _report(ctx: Ctx) -> tuple[str, str]:
    return OPEN_ADMIRY, digest_links.report_url(ctx.settings, ctx.business_id, ctx.day)


def _shown(ctx: Ctx):
    return [c for c in ctx.aud.channels if c.cls != "ignored"]


def _local_days(ctx: Ctx, first: date) -> tuple[datetime, datetime]:
    """[the start of local `first`, the end of the reported day)."""
    return digest_zone.bounds(first, ctx.zone)[0], ctx.end


def channel_issue(ctx: Ctx) -> list[Item]:
    out = []
    for c in _shown(ctx):
        if c.channel in ISSUE_WRITERS:
            rows = ctx.conn.execute(
                "SELECT id, kind FROM channel_issue WHERE business_id = %s AND channel = %s AND resolved_at IS NULL "
                "ORDER BY id",
                (ctx.business_id, c.channel),
            ).fetchall()
            for r in rows:
                text = ISSUE_SENTENCES.get(r["kind"], GENERIC_ISSUE).format(label=c.label)
                out.append(Item("channel_issue", "high", text, _ads(ctx), f"issue:{r['id']}"))
            continue
        issues = (c.row.get("resources") or {}).get("issues") or {}
        if issues.get("ads"):
            out.append(Item("channel_issue", "high", ADS_STOPPED.format(label=c.label), _ads(ctx), f"ads:{c.channel}"))
        for code in issues.get("campaign") or []:
            severity = CAMPAIGN_CODES.get(code, "medium")
            if severity is None:
                continue
            text = (CODE_HIGH if severity == "high" else CODE_MEDIUM).format(label=c.label)
            out.append(Item("channel_issue", severity, text, _ads(ctx), f"code:{c.channel}:{code}", (code,)))
    return out


def _fourteen_day_spend_cents(ctx: Ctx, channel: str) -> int:
    row = ctx.conn.execute(
        "SELECT coalesce(sum(cost_micros), 0) AS c FROM metric_daily WHERE business_id = %s AND channel = %s "
        "AND day BETWEEN %s AND %s",
        (ctx.business_id, channel, ctx.day - timedelta(days=NO_LEADS_DAYS - 1), ctx.day),
    ).fetchone()
    return int(row["c"]) // 10_000


def paused_by_system(ctx: Ctx) -> list[Item]:
    out = []
    n = ctx.numbers
    for c in _shown(ctx):
        reason = c.paused_reason
        if c.state != "paused" or reason not in (*SYSTEM_REASONS, "dispute"):
            continue
        amount = n.channel_amount_cents.get(c.channel)
        text = PAUSE_SENTENCES[reason].format(
            label=c.label,
            month=n.month_name,
            next_month=n.next_month_name,
            amount=digest_numbers.usd(amount) if amount else "",
            spend=digest_numbers.usd(_fourteen_day_spend_cents(ctx, c.channel)),
        )
        link = _report(ctx) if reason in ("month_cap", *ads_run.PAGE_REASONS) else _ads(ctx)
        out.append(Item("paused_by_system", PAUSE_SEVERITY.get(reason, "high"), text, link, f"pause:{c.channel}"))
    return out


def waiting_account(ctx: Ctx) -> list[Item]:
    if not any(c.cls == "active" for c in ctx.aud.channels):
        return []
    planned = set(ads_run.funded_channels(ads_run.approved(ctx.conn, ctx.business_id)))
    waiting = [r for r in ads_run.rows(ctx.conn, ctx.business_id) if r["state"] == "waiting_account"]
    waiting = [r for r in waiting if r["channel"] in planned]
    if not waiting:
        return []
    out = []
    for r in waiting:
        label = registry.label(r["channel"]) if registry.known(r["channel"]) else r["channel"]
        # where the row stands in Admiry's own words (the lifecycle's clause): the channels card quotes an open
        # issue's platform text, which never reaches the report or the Analyst's material (final review I4)
        said = ads_run.state_clause(r).strip().rstrip(".")
        text = f"{label}: {said[:1].upper()}{said[1:]}." if said else f"{label} is waiting for a step from you."
        out.append(Item("waiting_account", "high", text, _ads(ctx), f"waiting:{r['channel']}"))
    return out


def funds_low(ctx: Ctx) -> list[Item]:
    plan = ads_run.approved(ctx.conn, ctx.business_id)
    if plan is None:
        return []
    _, money = plan_review.money(ctx.conn, ctx.business_id, plan)
    if money is None or money["runway_days"] is None or money["runway_days"] > FUNDS_LOW_DAYS:
        return []
    managed = set(registry.managed_channels(ctx.conn, ctx.business_id))
    running = any(
        r["channel"] in managed and r["state"] in ("queued", "launching", "live")
        for r in ads_run.rows(ctx.conn, ctx.business_id)
    )
    if not running:
        return []
    days = money["runway_days"]
    if days == 0:
        text = "Your Admiry balance covers less than a day of ads. Add funds to keep them running."
    else:
        text = (
            f"Your Admiry balance covers about {days} more day{'' if days == 1 else 's'} of ads. Add funds to keep "
            "them running."
        )
    return [Item("funds_low", "high", text, _report(ctx), "funds_low")]


def proposal_pending(ctx: Ctx) -> list[Item]:
    rows = ctx.conn.execute(
        "SELECT id, class, summary, payload, expires_at FROM proposal WHERE business_id = %s AND status = 'proposed' "
        "AND expires_at > %s AND expires_at <= %s ORDER BY expires_at",
        (ctx.business_id, ctx.end, ctx.end + timedelta(hours=PROPOSAL_WINDOW_H)),
    ).fetchall()
    out = []
    for r in rows:
        weekday = r["expires_at"].astimezone(ctx.zone).strftime("%A")
        summary = (r["summary"] or "").strip().rstrip(".")
        url = (r["payload"] or {}).get("approval_url")
        link = ("Review and approve", url) if r["class"] == "spend" and url else _report(ctx)
        text = f"Waiting for your yes: {summary}. It expires {weekday}."
        out.append(Item("proposal_pending", "medium", text, link, f"proposal:{r['id']}"))
    return out


def leads_unmarked(ctx: Ctx) -> list[Item]:
    n = ctx.conn.execute(
        "SELECT count(*) AS n FROM lead WHERE business_id = %s AND status = 'new' AND created_at >= %s "
        "AND created_at < %s",
        (
            ctx.business_id,
            ctx.end - timedelta(days=LEADS_UNMARKED_MAX_D),
            ctx.end - timedelta(hours=LEADS_UNMARKED_MIN_H),
        ),
    ).fetchone()["n"]
    if not n:
        return []
    who = "1 person who contacted you is" if n == 1 else f"{n} people who contacted you are"
    text = (
        f"{who} still marked new. Tell Admiry which ones booked (in Claude, ChatGPT or Muse) — that is how it learns "
        "what a booked job costs."
    )
    return [Item("leads_unmarked", "medium", text, None, "leads_unmarked")]


def _channel_leads(ctx: Ctx, channel: str, first: date) -> int:
    start, end = _local_days(ctx, first)
    return ctx.conn.execute(
        "SELECT count(*) AS n FROM lead WHERE business_id = %s AND channel = %s AND created_at >= %s "
        "AND created_at < %s AND status <> 'spam'",
        (ctx.business_id, channel, start, end),
    ).fetchone()["n"]


def _days(ctx: Ctx, channel: str, n: int) -> list[dict]:
    return ctx.conn.execute(
        "SELECT day, cost_micros, clicks, impressions, calls, conversions FROM metric_daily WHERE business_id = %s "
        "AND channel = %s AND day BETWEEN %s AND %s ORDER BY day",
        (ctx.business_id, channel, ctx.day - timedelta(days=n - 1), ctx.day),
    ).fetchall()


def no_leads_spend(ctx: Ctx) -> list[Item]:
    out = []
    budgets = {r.channel: r.daily_budget_cents for r in ctx.numbers.rows}
    for c in _shown(ctx):
        launched = c.row.get("launched_at")
        if c.cls != "active" or c.state != "live" or launched is None:
            continue
        if launched > ctx.end - timedelta(days=NO_LEADS_DAYS):
            continue
        spent = _fourteen_day_spend_cents(ctx, c.channel)
        bench = benchmark.lookup(ctx.conn, ctx.category, c.channel)
        if bench is not None:
            enough = spent >= max(NO_LEADS_FLOOR_USD, bench["cpl_avg"]) * 100
        elif budgets.get(c.channel):
            enough = spent >= NO_LEADS_NO_BENCHMARK_SHARE * NO_LEADS_DAYS * budgets[c.channel]
        else:
            continue
        if not enough:
            continue
        first = ctx.day - timedelta(days=NO_LEADS_DAYS - 1)
        calls = sum(int(d["calls"]) for d in _days(ctx, c.channel, NO_LEADS_DAYS))
        if calls or _channel_leads(ctx, c.channel, first):
            continue
        text = (
            f"{c.label} spent {digest_numbers.usd(spent)} in 14 days without a call or a form. Admiry will suggest a "
            "change if this goes on."
        )
        out.append(Item("no_leads_spend", "medium", text, _report(ctx), f"no_leads:{c.channel}"))
    return out


def tracking_silent(ctx: Ctx) -> list[Item]:
    out = []
    for c in _shown(ctx):
        if c.state != "live":
            continue
        days = _days(ctx, c.channel, 7)
        if len(days) < 7 or any(int(d["impressions"]) <= 0 for d in days):
            continue
        clicks = sum(int(d["clicks"]) for d in days)
        if clicks < TRACKING_MIN_CLICKS:
            continue
        if any(int(d["calls"]) or float(d["conversions"]) for d in days):
            continue
        if _channel_leads(ctx, c.channel, ctx.day - timedelta(days=6)):
            continue
        text = (
            f"{clicks} people clicked your ads this week, but no calls or forms were recorded. Check that your page's "
            "Admiry snippet is in place."
        )
        link = ("Open Settings", digest_links.settings_url(ctx.settings, ctx.business_id, "advanced"))
        out.append(Item("tracking_silent", "medium", text, link, f"tracking:{c.channel}"))
    return out


def underdelivery(ctx: Ctx) -> list[Item]:
    out = []
    budgets = {r.channel: r.daily_budget_cents for r in ctx.numbers.rows}
    for c in _shown(ctx):
        budget = budgets.get(c.channel)
        if c.state != "live" or not budget:
            continue
        days = _days(ctx, c.channel, UNDERDELIVERY_DAYS)
        if len(days) < UNDERDELIVERY_DAYS:
            continue
        if all(int(d["cost_micros"]) // 10_000 < UNDERDELIVERY_SHARE * budget for d in days):
            text = (
                f"{c.label} spent less than a third of its daily budget three days running; few people nearby are "
                "searching for your services right now."
            )
            out.append(Item("underdelivery", "info", text, None, f"underdelivery:{c.channel}"))
    return out


def limit_near(ctx: Ctx) -> list[Item]:
    n = ctx.numbers
    if ctx.day.day >= LIMIT_NEAR_BEFORE_DAY:
        return []
    out = []
    for row in n.rows:
        amount = n.channel_amount_cents.get(row.channel)
        spent = n.month_spend_cents.get(row.channel, 0)
        cls = next((c for c in ctx.aud.channels if c.channel == row.channel), None)
        if not amount or spent < LIMIT_NEAR_SHARE * amount or (cls and cls.state == "paused"):
            continue
        usd = digest_numbers.usd
        text = (
            f"{row.label}: {usd(spent)} of its {usd(amount)} for {n.month_name}; it pauses at {usd(amount)} and "
            f"restarts {n.next_month_name} 1."
        )
        out.append(Item("limit_near", "info", text, _report(ctx), f"limit:{row.channel}"))
    return out


def safe_actions(ctx: Ctx) -> list[dict]:
    """The safe actions Admiry's jobs took for the business on the reported day (the week on a Monday)."""
    since = ctx.start - timedelta(days=6) if ctx.weekly else ctx.start
    return ctx.conn.execute(
        "SELECT * FROM agent_action WHERE business_id = %s AND actor_class = 'job' AND tool = ANY(%s) "
        "AND ts >= %s AND ts < %s ORDER BY ts, id",
        (ctx.business_id, list(SAFE_ACTION_TOOLS), since, ctx.end),
    ).fetchall()


def _channels_named(row: dict) -> set[str]:
    named = set()
    for part in (row.get("args_redacted") or {}, row.get("result") if isinstance(row.get("result"), dict) else {}):
        ch = part.get("channel")
        if isinstance(ch, str):
            named.add(ch)
    return named


def safe_action_taken(ctx: Ctx, paused: set[str] | None = None) -> list[Item]:
    from . import tools_account  # tools_account registers tools on mcp_app: imported when used, never at load

    paused = paused if paused is not None else {i.ref.split(":", 1)[1] for i in paused_by_system(ctx)}
    out = []
    for row in safe_actions(ctx):
        named = _channels_named(row)
        if (named and named & paused) or (not named and paused):
            continue
        out.append(Item("safe_action_taken", "info", tools_account.sentence(row, None), None, f"action:{row['id']}"))
    return out


RULES: tuple[Callable[[Ctx], list[Item]], ...] = (
    channel_issue,
    paused_by_system,
    waiting_account,
    funds_low,
    proposal_pending,
    leads_unmarked,
    no_leads_spend,
    tracking_silent,
    underdelivery,
    limit_near,
    safe_action_taken,
)


def items(ctx: Ctx) -> list[Item]:
    """Every rule's items, deduplicated by (rule, ref), sorted by severity, then the rules' order."""
    seen: set[tuple[str, str]] = set()
    ranked = []
    for order, rule in enumerate(RULES):
        for item in rule(ctx):
            if (item.rule, item.ref) in seen:
                continue
            seen.add((item.rule, item.ref))
            ranked.append((SEVERITY[item.severity], order, item))
    return [item for _, _, item in sorted(ranked, key=lambda t: (t[0], t[1]))]
