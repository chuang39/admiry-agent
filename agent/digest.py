"""The daily digest job (spec 2026-09-30-admiry-daily-digest-design §3, §7, §8, §9; plan 2026-10-07-agent-k2 Task 11,
Dg Task 8): every eligible business's owner gets one e-mail a local morning -- the Monday one is the weekly edition --
built from the stored numbers and the "needs you" items, with Admiry's take and suggestions from the Analyst's look
(roles/analyst.py `ensure`: the stored look, or built here under the same lock). One `digest` row per business and
reported day goes building -> ready -> sent (or failed, at most MAX_ATTEMPTS sends, re-checked before each; skipped;
stale at 20:00 local). Every send is the stored `rendered` mail under the digest's id, so a retry sends the same
bytes with the same Idempotency-Key. `operator` mode mails each preview to AGENT_OPS_EMAIL only and leaves no trace an
owner can read. The one clock is `ads_run.now()` (plan R8)."""

from __future__ import annotations

import logging
import time as clock
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb

from . import (
    ads_run,
    business,
    config,
    db,
    digest_audience,
    digest_body,
    digest_mail,
    digest_rules,
    digest_zone,
    mail_dispatch,
    mail_out,
    plans,
    profile,
)
from .roles import analyst

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETENTION_DAYS = 396  # 13 months (D115)
REPORT_DAYS = 8  # MarketingState's report is the latest one sent at most this many days ago
PENDING_DAYS = 2  # the pending pass looks at rows this many days back
BUSINESS_COLS = (
    "id, owner_email, status, paused_reason, operator_flags, digest_frequency, notify_email_status, time_zone"
)


def effective_mode(settings: config.Settings, business_row: dict) -> str:
    """off | operator | on (§9, D113): the global AGENT_DIGEST off wins; else the business's `operator_flags.digest`
    when set; else the global value."""
    if settings.digest == "off":
        return "off"
    flag = (business_row.get("operator_flags") or {}).get("digest")
    return flag if flag in config.DIGEST_MODES else settings.digest


def promised(settings: config.Settings, business_row: dict) -> bool:
    """Whether the owner is told a report is coming: the effective mode on, reports not off, the address not bounced."""
    return (
        effective_mode(settings, business_row) == "on"
        and business_row.get("digest_frequency") != "off"
        and business_row.get("notify_email_status") == "ok"
    )


def recheck(settings: config.Settings, business_row: dict, row: dict) -> str | None:
    """The pre-send re-check (§7): None when the row may still be sent, else why it is skipped."""
    if settings.digest == "off":
        return "off"
    if effective_mode(settings, business_row) != row["mode"]:
        return "mode"
    frequency = business_row.get("digest_frequency")
    if frequency == "off" or (frequency == "weekly" and row["edition"] != "weekly"):
        return "frequency"
    if business_row.get("notify_email_status") != "ok":
        return "bounced"
    if business_row.get("status") != "active":
        return "inactive"
    return None


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or digest_zone.DEFAULT_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(digest_zone.DEFAULT_ZONE)


def _name(conn: psycopg.Connection, business_id: str) -> str:
    latest = business.latest_knowledge(conn, business_id)
    return profile.clean_name(plans.name_for(latest[1] if latest else None)) or "your business"


def _insight_line(row: dict | None) -> dict | None:
    """What the operator's preview says of the Analyst's look (Admiry's team only)."""
    if row is None:
        return None
    ins = row.get("insight") or {}
    review = row.get("review") or {}
    return {
        "status": row["status"],
        "verdict": row.get("verdict"),
        "score": row.get("score"),
        "dropped": row.get("dropped"),
        "summary_dropped": ins.get("summary_dropped"),
        "issues": review.get("issues") or [],
    }


def _note_dropped(settings: config.Settings, insight: dict | None, take: str | None) -> str | None:
    """Why the mail carries no take (stored on the row for the team): the roles off, no look, a look that failed or
    was skipped, a look kept for the team (validate, or the Critic's verdict), or a summary the guards dropped."""
    if take:
        return None
    if insight is None:
        return "roles_off" if settings.roles_mode == "off" else "none"
    if insight["status"] != analyst.DONE:
        return insight.get("dropped") or insight["status"]
    if settings.roles_mode != "on" or insight.get("mode") != "on":  # built for the team while validate (R5)
        return "validate"
    return insight.get("dropped") or (insight.get("insight") or {}).get("summary_dropped") or "empty"


def _due(now: datetime, zone: ZoneInfo) -> tuple[date, float]:
    return digest_zone.reported_day(now, zone), digest_zone.local_hour(now, zone)


def _sweep(conn: psycopg.Connection, now: datetime, shard: str, params: dict) -> int:
    """Rows still building, ready or failed once 20:00 local of the day after theirs has passed -> stale."""
    rows = conn.execute(
        f"SELECT id, day, time_zone FROM digest WHERE status IN ('building', 'ready', 'failed') AND {shard}", params
    ).fetchall()
    stale = 0
    for r in rows:
        deadline = datetime.combine(
            r["day"] + timedelta(days=1), time(int(digest_zone.STALE_AT_H)), _zone(r["time_zone"])
        )
        if deadline <= now:
            conn.execute("UPDATE digest SET status = 'stale' WHERE id = %s", (r["id"],))
            stale += 1
    return stale


def _send(conn: psycopg.Connection, settings: config.Settings, biz: dict, row: dict, now: datetime, out: dict) -> None:
    """The send step, for a row that is `ready` or `failed`: re-checked first; an owner's mail through the thread, a
    preview to the operator; sent, or failed with one more attempt (a quota refusal counted: it pages)."""
    why = recheck(settings, biz, row)
    if why is not None:
        conn.execute("UPDATE digest SET status = 'skipped' WHERE id = %s", (row["id"],))
        out["skipped"] += 1
        return
    rendered = row["rendered"]
    if row["mode"] == "on":
        thread = conn.execute("SELECT * FROM email_thread WHERE id = %s", (row["thread_id"],)).fetchone()
        result = mail_out.deliver_digest(
            conn, settings, digest_id=row["id"], thread=dict(thread), to=biz["owner_email"], rendered=rendered
        )
        error = result["error"] if result["outcome"] != "sent" else None
        sent = result["outcome"] == "sent"
        if not sent and error is None:
            error = "not sent"
    else:
        domain = mail_out.split_address(settings.email_address)[1]
        name = (row.get("body") or {}).get("business") or "your business"
        error = mail_out.send_operator(
            settings,
            subject=f"[preview] {name} — {rendered['subject']}",
            text=rendered["text"],
            html=rendered["html"],
            message_id=f"<{row['id']}@{domain}>",
            idempotency_key=row["id"],
        )
        sent = error is None
    out["attempted"] += 1
    if sent:
        conn.execute(
            "UPDATE digest SET status = 'sent', sent_at = %s, attempts = attempts + 1 WHERE id = %s", (now, row["id"])
        )
        out["sent"] += 1
        return
    conn.execute("UPDATE digest SET status = 'failed', attempts = attempts + 1 WHERE id = %s", (row["id"],))
    out["failed"] += 1
    if error and "(429)" in error:
        out["quota_refusals"] += 1


def _build(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: dict,
    row: dict,
    *,
    mode: str,
    day: date,
    zone: ZoneInfo,
    aud,
    final: bool,
    now: datetime,
    out: dict,
) -> dict:
    """building -> ready: the numbers and items, the Analyst's look (stored, or built now under this lock), the body,
    the owner's thread (owner mode), the rendered mail; returns the ready row."""
    numbers, items, ctx = analyst.inputs(conn, settings, biz["id"], day, zone, aud)
    acts = digest_rules.safe_actions(ctx)
    from . import tools_account  # registers tools on mcp_app: imported when used

    try:
        insight = analyst.ensure(conn, settings, biz, day, now=now, aud=aud, numbers=numbers, items=items)
    except Exception:  # the report goes on time without a look it could not read
        log.exception("digest: the look for %s on %s could not be read", biz["id"], day)
        insight = {"status": analyst.FAILED, "dropped": "error", "insight": None}
    if insight is not None:
        out["insights"][insight["status"]] = out["insights"].get(insight["status"], 0) + 1
    view = analyst.owner_view(settings, insight, preview=(mode == "operator"))
    take = view["summary"] if view else None
    name = _name(conn, biz["id"])
    several = conn.execute(
        "SELECT count(*) > 1 AS s FROM business WHERE lower(owner_email) = lower(%s) AND status = 'active'",
        (biz["owner_email"],),
    ).fetchone()["s"]
    body = digest_body.build(
        numbers,
        items,
        edition=row["edition"],
        numbers_final=final,
        business_name=name,
        several=several,
        built_at=now,
        safe_actions=[tools_account.sentence(a, None) for a in acts],
    )
    body = digest_body.with_insight(body, take=take, recommendations=view["recommendations"] if view else [])
    thread_id, reply_to = None, None
    if mode == "on":
        thread = mail_dispatch.open_thread(conn, biz["id"], biz["owner_email"], body["subject"])
        thread_id = thread["id"]
        if settings.email_inbound:
            reply_to = mail_out.reply_address(settings.email_address, thread["token"])
        else:
            reply_to = settings.support_reply_to
    extra = None
    if mode == "operator":
        codes = {i.ref.split(":")[1]: list(i.codes) for i in items if i.codes}
        extra = {"insight": _insight_line(insight), "codes": codes}
    rendered = digest_mail.render(
        settings,
        body,
        business_id=biz["id"],
        frequency=biz.get("digest_frequency") or "daily",
        operator=(mode == "operator"),
        reply_to=reply_to,
        operator_extra=extra,
    )
    if take:
        out["takes"] += 1
    out["built"] += 1
    return conn.execute(
        "UPDATE digest SET body = %s, rendered = %s, note = %s, note_dropped = %s, numbers_final = %s, thread_id = %s, "
        "status = 'ready' WHERE id = %s RETURNING *",
        (
            Jsonb(body),
            Jsonb(rendered),
            take,
            _note_dropped(settings, insight, take),
            final,
            thread_id,
            row["id"],
        ),
    ).fetchone()


def _one(conn: psycopg.Connection, settings: config.Settings, biz: dict, now: datetime, out: dict) -> None:
    """One candidate business under its daily lock: claim its due day, build it, send it."""
    mode = effective_mode(settings, biz)
    if mode == "off":
        return
    zone = digest_zone.ensure(conn, biz["id"])
    day, hour = _due(now, zone)
    if not digest_zone.DUE_FROM_H <= hour < digest_zone.STALE_AT_H:
        return
    row = conn.execute("SELECT * FROM digest WHERE business_id = %s AND day = %s", (biz["id"], day)).fetchone()
    if row is not None and row["status"] != "building":
        return  # the pending pass owns ready and failed rows; sent, skipped and stale are done
    aud = digest_audience.audience(conn, biz, day, zone)
    if aud.edition is None:
        if row is not None:
            conn.execute("UPDATE digest SET status = 'skipped' WHERE id = %s", (row["id"],))
        return
    out["due"] += 1
    since = digest_zone.bounds(day + timedelta(days=1), zone)[0]
    final = digest_audience.gate(conn, settings, biz["id"], since)
    if not final and hour < digest_zone.CUTOFF_H:
        out["gated"] += 1
        return
    if row is None:
        row = conn.execute(
            "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, numbers_final, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'building', %s) ON CONFLICT (business_id, day) DO NOTHING RETURNING *",
            (db.new_id("dg"), biz["id"], day, aud.edition, zone.key, mode, final, now),
        ).fetchone()
        if row is None:
            return
    ready = _build(conn, settings, biz, row, mode=mode, day=day, zone=zone, aud=aud, final=final, now=now, out=out)
    _send(conn, settings, biz, ready, now, out)


def run(conn: psycopg.Connection, settings: config.Settings, i: int, n: int) -> dict:
    """The job body (§7): purge, sweep, then -- unless AGENT_DIGEST is off -- the pending rows and the due businesses.
    Raises jobs.JobFailed (with these details) when every send failed, when most of five or more did, or on a quota
    refusal: the ledger pages."""
    from .jobs import JobFailed, shard_sql  # jobs imports this module: a module-level import would be a cycle

    now = ads_run.now()
    keys = ("considered", "due", "gated", "built", "sent", "failed", "skipped", "stale", "quota_refusals", "takes")
    out: dict = {"mode": settings.digest, **dict.fromkeys(keys, 0), "attempted": 0, "errors": 0, "insights": {}}
    params = {"i": i, "n": n}
    cut = (now - timedelta(days=RETENTION_DAYS)).date()
    out["purged"] = conn.execute(
        f"DELETE FROM digest WHERE day < %(cut)s AND {shard_sql('business_id')}", {**params, "cut": cut}
    ).rowcount
    out["stale"] = _sweep(conn, now, shard_sql("business_id"), params)
    if settings.digest == "off":
        return {"mode": "off", "purged": out["purged"], "stale": out["stale"]}
    pending = conn.execute(
        f"SELECT d.* FROM digest d WHERE d.status IN ('ready', 'failed') AND d.day >= %(since)s "
        f"AND {shard_sql('d.business_id')} ORDER BY d.day, d.id",
        {**params, "since": (now - timedelta(days=PENDING_DAYS)).date()},
    ).fetchall()
    for row in pending:
        biz = conn.execute(f"SELECT {BUSINESS_COLS} FROM business WHERE id = %s", (row["business_id"],)).fetchone()
        if biz is None or (row["status"] == "failed" and row["attempts"] >= MAX_ATTEMPTS):
            continue
        with digest_zone.daily_lock(conn, row["business_id"]) as held:
            if not held:
                continue
            try:
                _send(conn, settings, biz, row, now, out)
            except Exception as e:  # noqa: BLE001 - one row never stops the run
                log.warning("digest: row %s failed: %s", row["id"], type(e).__name__)
                out["errors"] += 1
    candidates = conn.execute(
        f"SELECT {BUSINESS_COLS} FROM business b WHERE status = 'active' AND owner_email IS NOT NULL "
        "AND notify_email_status = 'ok' AND digest_frequency <> 'off' "
        "AND EXISTS (SELECT 1 FROM channel_campaign c WHERE c.business_id = b.id) "
        f"AND {shard_sql('b.id')} ORDER BY b.id",
        params,
    ).fetchall()
    started = clock.monotonic()
    for biz in candidates:
        if clock.monotonic() - started > settings.digest_run_seconds:
            break
        out["considered"] += 1
        with digest_zone.daily_lock(conn, biz["id"]) as held:
            if not held:
                continue
            try:
                _one(conn, settings, biz, now, out)
            except Exception as e:  # noqa: BLE001 - one business never stops the run
                log.warning("digest: business %s failed: %s", biz["id"], type(e).__name__)
                out["errors"] += 1
    attempted, failed = out["attempted"], out["failed"]
    if (attempted >= 1 and failed == attempted) or (attempted >= 5 and failed * 2 > attempted) or out["quota_refusals"]:
        raise JobFailed("digest sends failing", out)
    return out


def _sent_sql(include_operator: bool) -> str:
    return "status = 'sent'" + ("" if include_operator else " AND mode = 'on'")


def latest_sent(
    conn: psycopg.Connection, business_id: str, *, day: date | None = None, include_operator: bool = False
) -> dict | None:
    """The latest sent digest -- of `day` when given, else of any day; an owner's (mode on) unless include_operator."""
    return conn.execute(
        f"SELECT * FROM digest WHERE business_id = %s AND {_sent_sql(include_operator)} "
        "AND (%s::date IS NULL OR day = %s) ORDER BY day DESC LIMIT 1",
        (business_id, day, day),
    ).fetchone()


def recent_sent(
    conn: psycopg.Connection, business_id: str, *, limit: int = 14, include_operator: bool = False
) -> list[dict]:
    return conn.execute(
        f"SELECT * FROM digest WHERE business_id = %s AND {_sent_sql(include_operator)} ORDER BY day DESC LIMIT %s",
        (business_id, limit),
    ).fetchall()


def next_report_at(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> date | None:
    """The local date of the owner's next report while one is promised: tomorrow for a daily reader, the next Monday
    for a weekly one; None otherwise."""
    biz = conn.execute(f"SELECT {BUSINESS_COLS} FROM business WHERE id = %s", (business_id,)).fetchone()
    if biz is None or not promised(settings, biz):
        return None
    today = ads_run.now().astimezone(digest_zone.zone_of(conn, business_id)).date()
    if biz["digest_frequency"] == "weekly":
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return today + timedelta(days=1)


def report_for_state(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> dict | None:
    """MarketingState's `report`: the latest owner's digest body sent within REPORT_DAYS, while the digest is on for
    the business; None otherwise."""
    biz = conn.execute(f"SELECT {BUSINESS_COLS} FROM business WHERE id = %s", (business_id,)).fetchone()
    if biz is None or effective_mode(settings, biz) != "on":
        return None
    row = latest_sent(conn, business_id)
    if row is None or row["day"] < ads_run.now().date() - timedelta(days=REPORT_DAYS):
        return None
    return row["body"]


def preview(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, day: date, *, edition: str | None, note: bool
) -> dict:
    """agent.ops digest-preview: the operator's rendering of `day` as the job would build it, read only -- no row, no
    thread, no zone written, no model called (the take is the stored look's, when `note` and one exists)."""
    biz = conn.execute(f"SELECT {BUSINESS_COLS} FROM business WHERE id = %s", (business_id,)).fetchone()
    zone = digest_zone.zone_of(conn, business_id)
    aud = digest_audience.audience(conn, {**biz, "digest_frequency": "daily"}, day, zone)
    aud = digest_audience.Audience(edition=edition or aud.edition or "daily", channels=aud.channels)
    numbers, items, _ = analyst.inputs(conn, settings, business_id, day, zone, aud)
    insight = (
        conn.execute("SELECT * FROM insight WHERE business_id = %s AND day = %s", (business_id, day)).fetchone()
        if note
        else None
    )
    view = analyst.owner_view(settings, insight, preview=True)
    body = digest_body.build(
        numbers,
        items,
        edition=aud.edition,
        numbers_final=True,
        business_name=_name(conn, business_id),
        several=False,
        built_at=ads_run.now(),
        safe_actions=[],
    )
    body = digest_body.with_insight(
        body, take=view["summary"] if view else None, recommendations=view["recommendations"] if view else []
    )
    codes = {i.ref.split(":")[1]: list(i.codes) for i in items if i.codes}
    rendered = digest_mail.render(
        settings,
        body,
        business_id=business_id,
        frequency=biz["digest_frequency"],
        operator=True,
        reply_to=None,
        operator_extra={"insight": _insight_line(insight), "codes": codes},
    )
    return {"body": body, **rendered}
