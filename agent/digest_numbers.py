"""Every number a digest shows, each from one source (digest spec 2026-09-30 §4.3; plan 2026-10-07-agent-k2 Task 3):
the platforms' spend, clicks and calls from `metric_daily`; forms and leads from the `lead` table counted in the
business's own local day; booked from what the leads are now; the month from the running plan's amounts; the Monday
edition's week against the week before. The Analyst reads the same numbers (roles/analyst.py), so the mail and the
look never disagree. Aggregates only: no lead's fields ever leave this module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from . import ads_run, business, digest_zone, plans
from .channels.google_search.build import daily_micros

SOLID_LEADS, SOLID_BOOKED = 30, 10  # channels spec §6.5: the sample that reads as "solid"
BOOKED_FOR_COST, OWN_DAYS = 5, 14  # cost per booked once 5 bookings in 28 days and 14 days of the channel's own data
BOOKED = ("booked", "won")


@dataclass(frozen=True)
class ChannelDay:
    channel: str
    label: str
    cls: str
    state_text: str | None
    spend_cents: int
    clicks: int
    impressions: int
    calls: int
    conversions: float
    forms: int
    leads: int
    daily_budget_cents: int | None
    source: str  # "api" | "operator"


@dataclass(frozen=True)
class WeekRow:
    channel: str
    label: str
    source: str
    spend_cents: int
    leads: int
    calls: int
    booked: int
    prior_spend_cents: int
    prior_leads: int
    prior_calls: int
    prior_booked: int
    cost_per_lead_usd: float | None
    cost_per_booked_usd: float | None
    sample_word: str | None


@dataclass(frozen=True)
class Week:
    rows: list[WeekRow]
    leads_by_status: dict[str, int]
    best_channel: str | None
    month_spend_cents: int
    month_leads: int
    month_booked: int


@dataclass(frozen=True)
class Numbers:
    day: date
    zone: str
    rows: list[ChannelDay]
    organic_leads: int
    leads_by_status: dict[str, int]  # the reported window (the day; the week on Monday), spam left out
    month_name: str
    next_month_name: str
    month_spend_cents: dict[str, int]
    channel_amount_cents: dict[str, int]
    limit_usd: float | None
    limit_source: str | None
    week: Week | None


# Fixed owner sentences keyed on (class, state, paused_reason): never the row's last_error or a platform's own text
STATE_TEXTS = {
    ("active", "paused", "month_cap"): "Paused until {next_month} 1: {month}'s {amount} is used",
    ("active", "paused", "no_funds"): "Paused: there is no money left to run it",
    ("active", "paused", "no_leads"): "Paused after 14 days without a lead",
    ("active", "paused", "dispute"): "Paused while a card payment is disputed",
    ("active", "paused", "page_down"): "Paused: the page your ads send people to is not answering",
    ("active", "paused", "unreachable"): "Paused: the page your ads send people to is not answering",
    ("active", "limited", None): "Running, but held back",
    ("active", "launching", None): "Starting",
    ("owner_paused", "paused", "owner"): "Paused by you",
    ("owner_paused", "paused", None): "Paused by you",
    ("owner_paused", "limited", None): "Paused in Google Ads",
    ("report_only", "operator", None): "Run by Admiry from the platform's own report",
}


def usd(cents: int) -> str:
    """Cents as the owner reads them: "$23" or "$23.10"."""
    return plans.usd(cents / 100)


def state_text(cls: str, row: dict, *, month: str, next_month: str, amount: str | None) -> str | None:
    """The row's fixed owner sentence, None when it is simply running. A pause of Admiry's the table does not name
    reads "Paused: <ads_run.PAUSE_WORDS' words>"; the limited row's reason is never read (Google's own words stay out)."""
    state, reason = row.get("state"), row.get("paused_reason")
    key = (cls, state, reason if state == "paused" else None)
    text = STATE_TEXTS.get(key)
    if text is None and state == "paused" and reason in ads_run.PAUSE_WORDS:
        return f"Paused: {ads_run.PAUSE_WORDS[reason]}"
    if text is None and state == "paused":
        return "Paused"
    if text is None:
        return None
    return text.format(month=month, next_month=next_month, amount=amount or "")


def daily_budget_cents(monthly_usd: float) -> int:
    """The daily amount the platform receives (monthly ÷ 30.4, floored to the cent), in cents."""
    return daily_micros(monthly_usd) // 10_000


def channel_amounts(plan: dict | None) -> dict[str, float]:
    """The plan's monthly amount per channel it funds (`sections.ads.channels[].monthly_usd`)."""
    return {ch: ads_run.month_amount(plan, ch) for ch in ads_run.funded_channels(plan)}


def sample_word(leads: int, booked: int) -> str:
    """How much data a cost rests on (channels spec §6.5): "solid: 46 leads" or "early: 11 leads"."""
    word = "solid" if leads >= SOLID_LEADS or booked >= SOLID_BOOKED else "early"
    return f"{word}: {leads} lead{'' if leads == 1 else 's'}"


def _spend_calls(conn, biz: str, first: date, last: date) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT channel, sum(cost_micros) AS cost, sum(calls) AS calls FROM metric_daily "
        "WHERE business_id = %s AND day BETWEEN %s AND %s GROUP BY channel",
        (biz, first, last),
    ).fetchall()
    return {r["channel"]: {"cents": int(r["cost"] or 0) // 10_000, "calls": int(r["calls"] or 0)} for r in rows}


def _leads(conn, biz: str, start: datetime, end: datetime) -> dict[str | None, dict]:
    """Per channel (None: no ad brought them) the leads created in [start, end): all, forms, booked now. Spam and the
    leads the owner reported are left out (neither is a contact the ads brought that day)."""
    rows = conn.execute(
        "SELECT channel, count(*) AS leads, count(*) FILTER (WHERE kind = 'form') AS forms, "
        "count(*) FILTER (WHERE status = ANY(%s)) AS booked FROM lead WHERE business_id = %s "
        "AND created_at >= %s AND created_at < %s AND status <> 'spam' AND source <> 'owner' GROUP BY channel",
        (list(BOOKED), biz, start, end),
    ).fetchall()
    return {r["channel"]: {"leads": r["leads"], "forms": r["forms"], "booked": r["booked"]} for r in rows}


def _by_status(conn, biz: str, start: datetime, end: datetime) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM lead WHERE business_id = %s AND created_at >= %s AND created_at < %s "
        "AND status <> 'spam' GROUP BY status",
        (biz, start, end),
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def _per(cents: int, n: int) -> float | None:
    return round(cents / 100 / n, 2) if n else None


def _week(conn, biz: str, aud, day: date, zone: ZoneInfo, month_first: date) -> Week:
    """D-6..D against D-13..D-7 per channel shown, the 28 days' sample, the best channel, the month's totals."""
    _, end = digest_zone.bounds(day, zone)
    week_start = digest_zone.bounds(day - timedelta(days=6), zone)[0]
    prior_start = digest_zone.bounds(day - timedelta(days=13), zone)[0]
    sample_start = digest_zone.bounds(day - timedelta(days=27), zone)[0]
    money = _spend_calls(conn, biz, day - timedelta(days=6), day)
    prior_money = _spend_calls(conn, biz, day - timedelta(days=13), day - timedelta(days=7))
    leads = _leads(conn, biz, week_start, end)
    prior_leads = _leads(conn, biz, prior_start, week_start)
    sample = _leads(conn, biz, sample_start, end)
    rows = []
    for c in aud.channels:
        if c.cls == "ignored":
            continue
        m, pm = money.get(c.channel, {}), prior_money.get(c.channel, {})
        lw, lp, ls = leads.get(c.channel, {}), prior_leads.get(c.channel, {}), sample.get(c.channel, {})
        spend = m.get("cents", 0)
        launched = c.row.get("launched_at")
        own_days = launched is not None and launched <= end - timedelta(days=OWN_DAYS)
        booked_ok = ls.get("booked", 0) >= BOOKED_FOR_COST and own_days
        rows.append(
            WeekRow(
                channel=c.channel,
                label=c.label,
                source="operator" if c.cls == "report_only" else "api",
                spend_cents=spend,
                leads=lw.get("leads", 0),
                calls=m.get("calls", 0),
                booked=lw.get("booked", 0),
                prior_spend_cents=pm.get("cents", 0),
                prior_leads=lp.get("leads", 0),
                prior_calls=pm.get("calls", 0),
                prior_booked=lp.get("booked", 0),
                cost_per_lead_usd=_per(spend, lw.get("leads", 0)),
                cost_per_booked_usd=_per(spend, lw.get("booked", 0)) if booked_ok else None,
                sample_word=sample_word(ls.get("leads", 0), ls.get("booked", 0)),
            )
        )
    solid = [r for r in rows if r.sample_word and r.sample_word.startswith("solid")]
    ranked = sorted(
        (r for r in solid if (r.cost_per_booked_usd or r.cost_per_lead_usd) is not None),
        key=lambda r: r.cost_per_booked_usd if r.cost_per_booked_usd is not None else r.cost_per_lead_usd,
    )
    month = _leads(conn, biz, digest_zone.bounds(month_first, zone)[0], end)
    month_money = _spend_calls(conn, biz, month_first, day)
    return Week(
        rows=rows,
        leads_by_status=_by_status(conn, biz, week_start, end),
        best_channel=ranked[0].channel if ranked else None,
        month_spend_cents=sum(v["cents"] for v in month_money.values()),
        month_leads=sum(v["leads"] for v in month.values()),
        month_booked=sum(v["booked"] for v in month.values()),
    )


def gather(conn: psycopg.Connection, business_id: str, aud, day: date, zone: ZoneInfo, *, weekly: bool) -> Numbers:
    """The day's numbers for `aud`'s shown channels (active, paused by the owner, report-only with figures that day),
    the month and, on a Monday, the week."""
    start, end = digest_zone.bounds(day, zone)
    plan = ads_run.approved(conn, business_id)
    amounts = channel_amounts(plan)
    amount_cents = {ch: round(v * 100) for ch, v in amounts.items()}
    metrics = {
        r["channel"]: r
        for r in conn.execute(
            "SELECT channel, cost_micros, clicks, impressions, calls, conversions, source FROM metric_daily "
            "WHERE business_id = %s AND day = %s",
            (business_id, day),
        ).fetchall()
    }
    leads = _leads(conn, business_id, start, end)
    month_first = day.replace(day=1)
    next_first = (month_first + timedelta(days=32)).replace(day=1)
    month_name, next_month = day.strftime("%B"), next_first.strftime("%B")
    month_spend = {ch: v["cents"] for ch, v in _spend_calls(conn, business_id, month_first, day).items()}
    rows = []
    for c in aud.channels:
        if c.cls == "ignored" or (c.cls == "report_only" and c.channel not in metrics):
            continue
        m = metrics.get(c.channel) or {}
        lw = leads.get(c.channel, {})
        amount = amount_cents.get(c.channel)
        rows.append(
            ChannelDay(
                channel=c.channel,
                label=c.label,
                cls=c.cls,
                state_text=state_text(
                    c.cls, c.row, month=month_name, next_month=next_month, amount=usd(amount) if amount else None
                ),
                spend_cents=int(m.get("cost_micros") or 0) // 10_000,
                clicks=int(m.get("clicks") or 0),
                impressions=int(m.get("impressions") or 0),
                calls=int(m.get("calls") or 0),
                conversions=float(m.get("conversions") or 0),
                forms=lw.get("forms", 0),
                leads=lw.get("leads", 0),
                daily_budget_cents=daily_budget_cents(amounts[c.channel]) if c.channel in amounts else None,
                source=m.get("source") or "api",
            )
        )
    week = _week(conn, business_id, aud, day, zone, month_first) if weekly else None
    goal = business.latest_goal(conn, business_id) or {}
    return Numbers(
        day=day,
        zone=zone.key,
        rows=rows,
        organic_leads=leads.get(None, {}).get("leads", 0),
        leads_by_status=week.leads_by_status if week else _by_status(conn, business_id, start, end),
        month_name=month_name,
        next_month_name=next_month,
        month_spend_cents=month_spend,
        channel_amount_cents=amount_cents,
        limit_usd=round(sum(amounts.values()), 2) if amounts else None,
        limit_source=goal.get("limit_source"),
        week=week,
    )
