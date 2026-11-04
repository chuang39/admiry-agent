"""The funnel in the owner's words (spec 2026-10-01 §4.3, D163, D164; plan G1 Task 5): spend, visits, leads, booked,
won, and the revenue the owner reported -- for the business, by channel and by service.

Reads only. Days are the first channel row's account calendar (ruling S14: `metric_daily.day` is a day in the
account's own time zone), UTC while nothing runs. A lead is counted on the day it was created in that zone.

What counts as a lead here: a row a channel's click brought, or one the owner reported. A page lead with no ad click
is organic and stays out of every cost figure; spam is not a lead. A channel's own figures use only its own leads:
the allocator reads those, never a lead the owner reported (D163).

Nothing a customer typed is returned: a result under the `read` scope carries counts and money, never a person. A
lead's own words about the service it wanted only choose the group it is counted in -- a service on file, "Other" or
"Not stated" -- and are not shown."""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import NamedTuple

import psycopg
from fastmcp.exceptions import ToolError

from . import ads_run, attribution, plans, profile
from .channels import registry

PERIODS = ("7d", "30d", "month", "last_month")
BAD_PERIOD = f"period must be one of {', '.join(PERIODS)}"
DAYS = {"7d": 7, "30d": 30}
WORDS = {"7d": "the last 7 days", "30d": "the last 30 days", "month": "this month", "last_month": "last month"}
NOTHING = {  # say: a period in which nothing ran
    "7d": "Nothing has run in the last 7 days yet.",
    "30d": "Nothing has run in the last 30 days yet.",
    "month": "Nothing has run this month yet.",
    "last_month": "Nothing ran last month.",
}
BOOKED = ("booked", "won")
NOT_STATED = "Not stated"  # by_service: the leads that named no service
REPORT_KEYS = (  # the report's top-level keys: part of the public contract (surface/contract.py)
    "period",
    "from",
    "to",
    "funnel",
    "by_channel",
    "by_service",
    "by_source",  # A3p-3 P28: what brought each lead
    "reported_by_owner",
    "organic",
    "plan_estimate",
    "statement",
    "days",
)
OTHER = "Other"  # by_service: the leads whose words match no service on file (the words themselves are never shown)
SOLID_LEADS, SOLID_BOOKED = 30, 10  # channels spec §6.5: the sample that reads as "solid"


def window(period: str, today: date) -> tuple[date, date]:
    """The first and last day of a period ending today (or, for last_month, the calendar month before today's).
    PERIODS is the one list of them: the public contract lists it."""
    if period not in PERIODS:
        raise ToolError(BAD_PERIOD)
    if period in DAYS:
        return today - timedelta(days=DAYS[period] - 1), today
    if period == "month":
        return today.replace(day=1), today
    if period == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    raise ToolError(BAD_PERIOD)


def calendar(conn: psycopg.Connection, biz: str) -> tuple[date, tzinfo]:
    """Today and the time zone of the business's account calendar: its first channel row's (ruling S14: a platform
    keys its days by the account's own zone, and so does `metric_daily.day`); UTC while nothing runs."""
    rows = ads_run.rows(conn, biz)
    primary = rows[0] if rows else None
    return ads_run.account_today(primary), ads_run.zone(((primary or {}).get("resources") or {}).get("time_zone"))


def confidence(leads: int, booked: int) -> str:
    if leads == 0:
        return "no leads yet"
    word = "solid" if leads >= SOLID_LEADS or booked >= SOLID_BOOKED else "early"
    return f"{word}: {leads} lead{'s' if leads != 1 else ''}"


def _per(spend: float, n: int) -> float | None:
    """A cost per lead (or per booked one): none where nothing was spent, since a lead the owner reported with no ad
    money behind it did not cost $0, and none where there is nothing to divide by."""
    return round(spend / n, 2) if spend and n else None


def _outcomes(rows: list[dict]) -> dict:
    won = [r for r in rows if r["status"] == "won"]
    valued = [r for r in won if r["value_usd"] is not None]
    return {
        "leads": len(rows),
        "booked": sum(1 for r in rows if r["status"] in BOOKED),
        "won": len(won),
        "revenue_reported_usd": round(float(sum(r["value_usd"] for r in valued)), 2),
        "revenue_reported_on": len(valued),
    }


def _read(conn: psycopg.Connection, biz: str, first: date, last: date, zone) -> tuple[dict, list[dict]]:
    """The window's spend, visits and counted calls by channel, and its leads (spam left out)."""
    metrics = {
        r["channel"]: r
        for r in conn.execute(
            "SELECT channel, coalesce(sum(cost_micros), 0) AS cost_micros, coalesce(sum(clicks), 0) AS clicks, "
            "coalesce(sum(calls), 0) AS calls, max(source) AS source FROM metric_daily "
            "WHERE business_id = %s AND day BETWEEN %s AND %s GROUP BY channel",
            (biz, first, last),
        ).fetchall()
    }
    start = datetime.combine(first, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    found = conn.execute(
        "SELECT channel, source, kind, status, value_usd, came_from, fields->>'service' AS service FROM lead "
        "WHERE business_id = %s AND created_at >= %s AND created_at < %s AND status <> 'spam'",
        (biz, start, end),
    ).fetchall()
    return metrics, [dict(r) for r in found]


def _usd(micros) -> float:
    return round(int(micros) / 1_000_000, 2)


def _funnel(metrics: dict, counted: list[dict]) -> dict:
    """The period's headline figures: what the channels spent and sent, and what the counted leads became."""
    spend = round(sum(_usd(m["cost_micros"]) for m in metrics.values()), 2)
    totals = _outcomes(counted)
    return {
        "spend_usd": spend,
        "visits": sum(int(m["clicks"]) for m in metrics.values()),
        "leads": totals["leads"],
        "leads_by_kind": {k: sum(1 for r in counted if r["kind"] == k) for k in ("form", "call", "other", "booking")},
        "calls_counted": sum(int(m["calls"]) for m in metrics.values()),
        "booked": totals["booked"],
        "won": totals["won"],
        "revenue_reported_usd": totals["revenue_reported_usd"],
        "revenue_reported_on": totals["revenue_reported_on"],
        "cost_per_lead_usd": _per(spend, totals["leads"]),
        "cost_per_booked_usd": _per(spend, totals["booked"]),
        "confidence": confidence(totals["leads"], totals["booked"]),
    }


class _Period(NamedTuple):
    """A period as `build` and `month` both read it: its first and last day, the channels' spend and clicks, the
    leads by where they came from (spam left out) and the funnel's headline figures."""

    first: date
    last: date
    metrics: dict
    attributed: list[dict]  # a channel's click brought them
    reported: list[dict]  # the owner reported them
    organic: list[dict]  # a page lead with no ad click
    funnel: dict


def _read_period(conn: psycopg.Connection, biz: str, period: str) -> _Period:
    """The one place a period is read: its window in the account's own calendar (ToolError for a period there is
    none of), the metrics and leads in it, and the funnel's figures. `build` adds the breakdowns to it; `month` is
    this and nothing more."""
    today, zone = calendar(conn, biz)
    first, last = window(period, today)
    metrics, found = _read(conn, biz, first, last, zone)
    reported = [r for r in found if r["source"] == "owner"]
    attributed = [r for r in found if r["channel"] is not None and r["source"] != "owner"]
    organic = [r for r in found if r["channel"] is None and r["source"] != "owner"]
    return _Period(first, last, metrics, attributed, reported, organic, _funnel(metrics, attributed + reported))


def _by_channel(metrics: dict, attributed: list[dict]) -> list[dict]:
    """Each channel with spend or a lead of its own, in card order. A channel's figures use only the leads its click
    brought, never one the owner reported (D163)."""
    out = []
    for channel in [c for c in registry.ORDER if c in metrics or any(r["channel"] == c for r in attributed)]:
        m = metrics.get(channel) or {"cost_micros": 0, "clicks": 0, "calls": 0, "source": None}
        own = _outcomes([r for r in attributed if r["channel"] == channel])
        cost = _usd(m["cost_micros"])
        out.append(
            {
                "channel": channel,
                "label": registry.label(channel),
                "spend_usd": cost,
                "visits": int(m["clicks"]),
                "calls_counted": int(m["calls"]),
                "leads": own["leads"],
                "booked": own["booked"],
                "won": own["won"],
                "revenue_reported_usd": own["revenue_reported_usd"],
                "cost_per_lead_usd": _per(cost, own["leads"]),
                "cost_per_booked_usd": _per(cost, own["booked"]),
                "confidence": confidence(own["leads"], own["booked"]),
                "source": m["source"],
            }
        )
    return out


def _source(row: dict) -> str:
    """What brought a lead (A3p-3 P28): the source the page recorded (`came_from`), else the channel its click came
    from, else `reported` for one the owner reported, else `unknown` (a page lead from before Admiry kept sources)."""
    return row.get("came_from") or row.get("channel") or ("reported" if row.get("source") == "owner" else "unknown")


def _by_source(found: list[dict]) -> list[dict]:
    """Every lead of the period by what brought it (P28), organic ones included: the ad click's channel, a tagged
    link, a search engine's or a social network's own results, another site, or a visit that came straight to the
    page; with what they became. Most leads first."""
    groups: dict[str, list[dict]] = {}
    for r in found:
        groups.setdefault(_source(r), []).append(r)
    out = []
    for source, rows in groups.items():
        o = _outcomes(rows)
        out.append(
            {
                "source": source,
                "label": attribution.label(source),
                "leads": o["leads"],
                "booked": o["booked"],
                "won": o["won"],
            }
        )
    out.sort(key=lambda s: (-s["leads"], -s["won"], s["label"]))
    return out


def _group(words: str | None, ids: dict[str, dict], names: dict[str, dict]) -> tuple[str | None, str]:
    """The (service id, name) a lead is counted under, from the words it used for the service it wanted: the service
    on file whose id they are (the owner's assistant names a service by its id), or whose name they say; "Other" for
    words that name none, which are not kept (a visitor typed them); "Not stated" for no words."""
    text = (words or "").strip()
    if not text:
        return None, NOT_STATED
    asked = profile.norm(text)
    match = ids.get(text) or (names.get(asked) if asked else None)  # words with no a-z or 0-9 name nothing
    return (match["id"], match["name"]) if match else (None, OTHER)


def _by_service(counted: list[dict], services: list[dict] | tuple) -> list[dict]:
    """Leads and what they became, by service. Only a service on file (`services`: the profile's `id` and `name`),
    "Other" and "Not stated" name a row; a lead's own words choose its row and are never returned. A lead takes the
    service whose id its words are exactly, else the one whose name they say. A service whose name has no a-z or 0-9
    cannot be matched by name, so it never takes a lead that named none."""
    ids = {s["id"]: s for s in services if s.get("id")}
    names = {n: s for s in services if (n := profile.norm(s.get("name") or ""))}
    groups: dict[tuple[str | None, str], list[dict]] = {}
    for r in counted:
        groups.setdefault(_group(r["service"], ids, names), []).append(r)
    out = []
    for (service_id, name), rows in groups.items():
        o = _outcomes(rows)
        out.append(
            {
                "service_id": service_id,
                "name": name,
                "leads": o["leads"],
                "booked": o["booked"],
                "won": o["won"],
                "revenue_reported_usd": o["revenue_reported_usd"],
            }
        )
    out.sort(key=lambda s: (-s["leads"], -s["won"], s["name"]))
    return out


def build(conn: psycopg.Connection, biz: str, period: str, *, services: list[dict] | tuple = ()) -> dict:
    """The report for `period`. `services` is the profile's services (`id`, `name`), the only names `by_service`
    shows: a lead's own words are matched to one by id or by name; a lead that named none is counted under "Not
    stated", and one whose words match nothing on file under "Other", never under the words themselves."""
    p = _read_period(conn, biz, period)
    mine = _outcomes(p.reported)
    approved = ads_run.approved(conn, biz)
    return {
        "period": period,
        "from": p.first.isoformat(),
        "to": p.last.isoformat(),
        "funnel": p.funnel,
        "by_channel": _by_channel(p.metrics, p.attributed),
        "by_service": _by_service(p.attributed + p.reported, services),
        "by_source": _by_source(p.attributed + p.reported + p.organic),
        "reported_by_owner": {k: mine[k] for k in ("leads", "booked", "won")},
        "organic": {"leads": len(p.organic)},
        "plan_estimate": ads_run.scaled_planning(approved, (p.last - p.first).days + 1) if approved else None,
        "statement": ads_run.statement(conn, biz, p.first) if period in ("month", "last_month") else None,
        "days": [],
    }


def month(conn: psycopg.Connection, biz: str) -> dict:
    """This month so far, for the state: the funnel's counts and money, nothing a person could be found by. It reads
    the month's window and totals it, and runs no statement, plan estimate or breakdown (the state asks on every
    read)."""
    f = _read_period(conn, biz, "month").funnel
    return {k: f[k] for k in ("spend_usd", "visits", "leads", "calls_counted", "booked", "won", "revenue_reported_usd")}


def goal_progress(conn: psycopg.Connection, biz: str, goal: dict | None) -> dict | None:
    """Where the goal stands this calendar month: won leads for customers, booked ones for bookings, the platforms'
    counted calls for calls. None without a target. Every lead counts, whatever brought it."""
    if not goal or goal.get("target") is None:
        return None
    today, zone = calendar(conn, biz)
    first = today.replace(day=1)
    out = {"month": f"{today:%Y-%m}"}
    if goal.get("goal_type") == "calls":
        n = conn.execute(
            "SELECT coalesce(sum(calls), 0) AS n FROM metric_daily WHERE business_id = %s AND day BETWEEN %s AND %s",
            (biz, first, today),
        ).fetchone()["n"]
        return {"so_far": int(n), **out, "counted": "calls the platforms counted"}
    wanted = list(BOOKED) if goal.get("goal_type") == "bookings" else ["won"]
    n = conn.execute(
        "SELECT count(*) AS n FROM lead WHERE business_id = %s AND created_at >= %s AND status = ANY(%s)",
        (biz, datetime.combine(first, datetime.min.time(), tzinfo=zone), wanted),
    ).fetchone()["n"]
    return {"so_far": int(n), **out, "counted": "booked leads" if len(wanted) == 2 else "won leads"}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def say(report: dict) -> str:
    """The report as the owner's sentences: money, visits, leads and what they became. No ad vocabulary."""
    f, words = report["funnel"], WORDS[report["period"]]
    if not f["spend_usd"] and not f["leads"] and not f["visits"]:
        return NOTHING[report["period"]]
    lead_in = f"Over {words}" if report["period"] in DAYS else words[:1].upper() + words[1:]
    out = [
        f"{lead_in}: {plans.usd(f['spend_usd'])} spent, {_n(f['visits'], 'visit', 'visits')} from your ads, "
        f"{_n(f['leads'], 'lead', 'leads')}"
        + (f", {f['booked']} of them booked and {f['won']} won." if f["leads"] else ".")
    ]
    if f["cost_per_lead_usd"] is not None:
        booked = (
            f" and {plans.usd(f['cost_per_booked_usd'])} a booked one" if f["cost_per_booked_usd"] is not None else ""
        )
        out.append(f"That is about {plans.usd(f['cost_per_lead_usd'])} a lead{booked}.")
    if f["revenue_reported_on"]:
        out.append(
            f"You reported {plans.usd(f['revenue_reported_usd'])} from {f['revenue_reported_on']} of the won ones."
        )
    mine = report["reported_by_owner"]["leads"]
    if mine:
        out.append(f"{mine} of these leads {'is one' if mine == 1 else 'are ones'} you reported.")
    return " ".join(out)
