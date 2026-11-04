"""Who gets which digest (spec 2026-09-30 §3, §7; plan 2026-10-07-agent-k2 Task 2). Each channel row is classified:
`active` (ran, or was stopped by Admiry's own hand, on the reported day), `owner_paused` (the owner's pause, or ads
someone switched off on the platform itself), `report_only` (Admiry runs it from the platform's own report: shown, never
counted) or `ignored`. The edition: any active row -> daily (weekly on a Monday morning); only rows the owner paused
that spent in the last week -> the Monday weekly; else none; then the owner's frequency. The sync gate holds a
morning's digest until every live channel's figures for the day are in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from . import ads_run, config, digest_zone
from .channels import registry

# Admiry's own pauses: the business still had its ads that day (spec §3), whatever the reason's time
SYSTEM_REASONS = ("month_cap", "no_funds", "no_leads", *ads_run.PAGE_REASONS)


@dataclass(frozen=True)
class ChannelClass:
    channel: str
    label: str
    cls: str  # active | owner_paused | ignored | report_only
    state: str
    paused_reason: str | None
    spent_on_day: bool
    row: dict


@dataclass(frozen=True)
class Audience:
    edition: str | None  # daily | weekly | None
    channels: list[ChannelClass]  # every row, classified


def paused_on_platform(row: dict) -> bool:
    """A `limited` row whose note is the adapter's own for ads someone switched off on the platform itself
    (`words.paused_elsewhere` / `words.paused_there`, as hosting_minimum reads them)."""
    channel = row.get("channel")
    if row.get("state") != "limited" or not channel or not registry.known(channel):
        return False
    words = registry.adapter(channel).words
    return row.get("last_error") in {w for w in (words.paused_elsewhere, words.paused_there) if w}


def classify(row: dict, *, start: datetime, spent_on_day: bool, business_paused_reason: str | None) -> str:
    """The row's class for the day beginning at `start` (spec §3), the first rule that holds."""
    state, reason, paused_at = row.get("state"), row.get("paused_reason"), row.get("paused_at")
    if state == "operator":
        return "report_only"
    if state in ("live", "launching"):
        return "active"
    if state == "limited" and not paused_on_platform(row):
        return "active"
    if state == "paused" and reason in SYSTEM_REASONS:
        return "active"
    if state == "paused" and reason == "dispute" and business_paused_reason != "collections":
        return "active"
    if state == "paused" and paused_at is not None and paused_at >= start:
        return "active"
    if spent_on_day:
        return "active"
    if (state == "paused" and ads_run.owner_paused(row)) or (state == "limited" and paused_on_platform(row)):
        return "owner_paused"
    return "ignored"


def audience(conn: psycopg.Connection, business: dict, day: date, zone: ZoneInfo) -> Audience:
    """The edition `business` ({id, paused_reason, digest_frequency}) gets for the local `day`, and every row's class."""
    biz = business["id"]
    start, _ = digest_zone.bounds(day, zone)
    spent = conn.execute(
        "SELECT channel, sum(cost_micros) FILTER (WHERE day = %s) AS on_day, sum(cost_micros) AS week "
        "FROM metric_daily WHERE business_id = %s AND day BETWEEN %s AND %s GROUP BY channel",
        (day, biz, day - timedelta(days=6), day),
    ).fetchall()
    on_day = {r["channel"]: r["on_day"] or 0 for r in spent}
    week = {r["channel"]: r["week"] or 0 for r in spent}
    classes = []
    for row in ads_run.rows(conn, biz):
        ch = row["channel"]
        cls = classify(
            row,
            start=start,
            spent_on_day=on_day.get(ch, 0) > 0,
            business_paused_reason=business.get("paused_reason"),
        )
        label = registry.label(ch) if registry.known(ch) else ch
        classes.append(ChannelClass(ch, label, cls, row["state"], row.get("paused_reason"), on_day.get(ch, 0) > 0, row))
    counted = [c for c in classes if c.cls != "report_only"]
    monday = (day + timedelta(days=1)).weekday() == 0
    if any(c.cls == "active" for c in counted):
        edition = "weekly" if monday else "daily"
    elif any(c.cls == "owner_paused" and week.get(c.channel, 0) > 0 for c in counted):
        edition = "weekly" if monday else None
    else:
        edition = None
    frequency = business.get("digest_frequency") or "daily"
    if frequency == "off" or (frequency == "weekly" and edition != "weekly"):
        edition = None
    return Audience(edition=edition, channels=classes)


def gate(conn: psycopg.Connection, settings: config.Settings, business_id: str, since: datetime) -> bool:
    """True once every live channel's built ads were synced at or after `since` (the end of the reported day):
    a row never synced, one with no ads built, or one whose switch is not live does not hold it (spec §7)."""
    rows = conn.execute(
        "SELECT channel, synced_at FROM channel_campaign WHERE business_id = %s "
        "AND state IN ('live', 'paused', 'limited', 'launching') AND resources ? 'campaign' AND synced_at IS NOT NULL",
        (business_id,),
    ).fetchall()
    return all(r["synced_at"] >= since for r in rows if registry.exec_mode(settings, r["channel"]) == "live")
