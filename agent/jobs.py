"""Scheduled jobs (spec 2026-09-24 §4.6). Each job is `fn(conn, shard_i, shard_n) -> dict`:
set-based, idempotent, sharded in SQL by mod(abs(hashtext(<id column>)::bigint), N) so N
replicas split the work with no coordination (see shard_sql()) -- the bigint cast avoids
"integer out of range" from abs() on hashtext's rare INT_MIN result, which has no int4
magnitude. The runner (tick.py) owns the job_run ledger.

Cadence (dev: Airflow DAG agent_ticks.py; prod: systemd bidx-agent-<job>.timer):
  proposal-expire  hourly   ads-link-sync  hourly   spend-guard  hourly   consult-expire  hourly
  ads-launch  hourly   ads-sync  hourly   page-check  hourly   email-retry  hourly
  support-mail  hourly   conversion-upload  hourly
  research-run  hourly (capability layer K1, K4: the roles' background queue: the Critic's looks and the Researcher's
                 runs, agent/roles/runner.py)
  analyst-run  hourly at :02 (capability layer K2: each business's daily look once its local day is due,
                 agent/roles/analyst.py)
  digest  hourly at :12 (the owners' morning reports, agent/digest.py; after analyst-run, whose look it carries)
  claim-expire  daily   session-expire  daily   oauth-prune  daily   email-purge  daily
  wallet-settle  daily at 14:27 UTC: a day posts from a read SETTLE_LAG (3 h) after its close in the account's
                 zone, and by 14:27 every US zone's day -- Hawaii's (closed 10:00 UTC) and American Samoa's (11:00 UTC)
                 included -- has closed and been read by the hourly sync (:07 on dev, :09:30 on prod), so each
                 posts on the run after it ends
  hosting-minimum  daily at 14:47 UTC (A3p-3): after wallet-settle, when yesterday's spend of every US zone is in
                 metric_daily; the hosted sites' countdown, reminders, takedown and 90-day purge (agent/hosting_minimum.py)
  refusal-summary  daily at 03:37 UTC (Part B phase 1, spec 2026-10-12 §2.4: yesterday's Google refusals summarised
                 into refusal_summary and rules proposed; agent/refusal_summary.py)
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import httpx
import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import (
    ads_run,
    audit,
    bundles,
    config,
    crawl,
    google_ads_link,
    hosted,
    landing,
    mail,
    refusal_summary,
    render_check,
    site_install,
    site_upload,
    wallet,
)
from . import hosting_minimum as minimum
from .channels import registry
from .channels.base import Adapter, ConversionEvent, Unsupported
from .channels.google_search import client as google_ads  # A3's names for the modules moved in M0
from .channels.google_search import conversions as ads_conversions
from .channels.newsbreak import client as newsbreak
from .roles import analyst as roles_analyst
from .roles import runner as roles_runner
from .util import iso

log = logging.getLogger(__name__)

DRAFT_TTL = "24 hours"
# ADS_OPS_DAILY_BUDGET is Google Ads' own daily operations quota (A3 ruling S4): it counts this channel's operations
# and stops this channel's rows alone; no other channel's rows ever stop for it
OPS_BUDGET_CHANNEL = "google_search"
# NewsBreak's own day budget (M1 Task 6, ruling T6-4): 80 % of the 864,000 requests a day per developer app of its Rate
# Limits (client.py), counted from midnight UTC, when that limit starts again -- it holds NewsBreak's rows alone, and
# so do two throttled answers in a row (`THROTTLED_IN_A_ROW`) for the rest of that UTC day; a stop always goes
NEWSBREAK = "newsbreak"
NB_QPD_BUDGET = 691_200
THROTTLED_IN_A_ROW = 2
UTC_TODAY = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"  # this UTC day's start, in SQL
# conversion-upload sends a lead back only this long after it came: a NewsBreak click id is good for 7 days (its
# postback FAQ: later postbacks fail, "typically 400")
CONVERSION_WINDOW = "7 days"
VALIDATE_LEADS = 20  # under `validate`, the lead ids a conversion-upload run records at most (it counts them all)
# wallet-settle posts a day once, from a read at least this long after the day closed in the account's zone: a
# platform keeps reporting a day for hours after it ends (Google: most metrics within about three hours)
SETTLE_LAG = timedelta(hours=3)


class JobFailed(Exception):
    """A job ran but its outcome must page (the daily digest when its sends fail): `details` still reaches job_run,
    with ok = false (tick.run), and the run exits 1 (Dg plan P13)."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def digest_job(conn: psycopg.Connection, i: int, n: int) -> dict:
    """`digest` (hourly, :12; plan 2026-10-07-agent-k2 R4): the owners' morning reports (agent/digest.py)."""
    from . import digest  # digest imports jobs' shard_sql and JobFailed: imported when run

    return digest.run(conn, config.load(), i, n)


def shard_sql(col: str) -> str:
    """The SQL fragment (params `%(i)s`/`%(n)s`) assigning rows of `col` to shard i of n."""
    return f"mod(abs(hashtext({col})::bigint), %(n)s) = %(i)s"


def proposal_expire(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Proposals past expires_at that were never confirmed or declined -> expired."""
    cur = conn.execute(
        f"UPDATE proposal SET status = 'expired', resolved_at = now() "
        f"WHERE status = 'proposed' AND expires_at < now() AND {shard_sql('business_id')}",
        {"i": i, "n": n},
    )
    return {"expired": cur.rowcount}


def claim_expire(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Never-claimed drafts die with their 24-hour draft key; unverified claims release their slug after
    14 days (spec §4.0 "Business claim state") but stay active with their owner e-mail, so the same owner can
    claim again from a new link and another account is still refused. A claim with a bank transfer still
    'processing' never lapses (finding 6): Stripe can confirm an ACH debit days after the 14-day window, and
    wallet.verify_by_payment must still get to run when it does."""
    shard = shard_sql("id")
    drafts = conn.execute(
        f"UPDATE business SET status = 'expired', updated_at = now() "
        f"WHERE status = 'active' AND claim_state = 'draft' AND owner_email IS NULL "
        f"AND created_at < now() - interval '{DRAFT_TTL}' "
        f"AND NOT EXISTS (SELECT 1 FROM api_key k WHERE k.business_id = business.id AND k.class = 'draft' "
        f"                AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now())) "
        f"AND {shard}",
        {"i": i, "n": n},
    )
    claims = conn.execute(
        f"UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL, updated_at = now() "
        f"WHERE claim_state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at < now() "
        f"AND NOT EXISTS (SELECT 1 FROM payment_session WHERE business_id = business.id AND status = 'processing') "
        f"AND {shard}",
        {"i": i, "n": n},
    )
    return {"drafts_expired": drafts.rowcount, "claims_released": claims.rowcount}


def session_expire(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Sign-in leftovers (A1b): sessions a day past expiry or revocation, sign-in attempts older than an
    hour, claim links a day past expiry. Live rows are never touched."""
    args = {"i": i, "n": n}
    sessions = conn.execute(
        f"DELETE FROM web_session WHERE (expires_at < now() - interval '1 day' OR revoked_at < now() - interval '1 day') "
        f"AND {shard_sql('token_hash')}",
        args,
    )
    logins = conn.execute(
        f"DELETE FROM login_attempt WHERE created_at < now() - interval '1 hour' AND {shard_sql('state')}", args
    )
    links = conn.execute(
        f"DELETE FROM claim_link WHERE expires_at < now() - interval '1 day' AND {shard_sql('token_hash')}", args
    )
    return {"sessions": sessions.rowcount, "logins": logins.rowcount, "links": links.rowcount}


def ads_link_sync(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Pending Google Ads invitations of this shard (spec 2026-09-25 §5 "Status sync"): read each one's status
    from Google (api mode; the connection is idle, no transaction, while Google answers) and record it in its own
    transaction; ACTIVE verifies the business, audited as job:ads-link-sync. A Google failure is kept on its row
    and the run carries on. In manual mode nothing is read (an operator runs agent.ops ads-link). A newly active
    link also gets its conversion actions and call reporting (A3 Task 2, `ads_conversions.ensure`) — outside that
    row's transaction; a failure there is logged and does not affect the link, which stays active (the next
    `ensure` call is idempotent by name lookup, so it is safe to retry)."""
    settings = config.load()
    out = {"mode": settings.google_ads_link_mode, "checked": 0, "activated": 0, "errors": 0}
    if settings.google_ads_link_mode != "api":
        return out
    rows = conn.execute(
        f"SELECT business_id, customer_id FROM ads_link WHERE status = 'pending' "
        f"AND {shard_sql('business_id')} ORDER BY business_id",
        {"i": i, "n": n},
    ).fetchall()
    if not rows:
        return out
    with google_ads_link.client() as http:
        for r in rows:
            try:
                try:
                    remote = google_ads_link.read(settings, r["customer_id"], http=http)
                except google_ads_link.LinkError as e:
                    google_ads_link.record_error(conn, r["business_id"], r["customer_id"], str(e))
                    out["errors"] += 1
                    continue
                with conn.transaction():
                    done = google_ads_link.apply(conn, r["business_id"], r["customer_id"], remote)
                    if done.became_active:
                        audit.record_job(
                            conn,
                            job="ads-link-sync",
                            tool="job.ads_link_active",
                            business_id=r["business_id"],
                            args={"customer_id": r["customer_id"]},
                            result={
                                "customer_id": google_ads_link.format_customer_id(r["customer_id"]),
                                "verified_now": done.verified_now,
                            },
                        )
                        out["activated"] += 1
                if done.became_active:  # outside the transaction; same db conn (autocommit), same http session
                    try:
                        ads_conversions.ensure(conn, settings, r["business_id"], r["customer_id"], http=http)
                    except google_ads.AdsError as e:
                        log.warning("ads-link-sync: conversion setup for business %s: %s", r["business_id"], e)
                out["checked"] += 1
            except Exception as e:  # noqa: BLE001 - one row never stops the batch; the message may carry Google's words
                log.warning("ads-link-sync: %s on business %s", type(e).__name__, r["business_id"])
                out["errors"] += 1
    return out


def spend_guard(conn: psycopg.Connection, i: int, n: int) -> dict:
    """The no-funds flag (spec 2026-09-25 rev. 2 D30; rev. 1 §4 "Spend guard", the flag half): an active prepaid
    (managed) business that has been funded or spent at least once (a card payment, an operator's credit, or ad
    spend) and whose balance is at or below zero gets paused_reason = 'no_funds' and one owner sentence, once — a
    flagged business is skipped until a top-up or an operator's credit that brings the balance above zero lifts the
    flag (wallet.resume_if_funded). A business with no money in and no spend is left alone (nothing of its runs),
    and an own_account business pays Google directly (it has no balance). Pausing the ads themselves is the Google
    adapter's call (A3). Then the month cap of every channel (`_month_cap`, M0 Task 7). Returns {"paused",
    "month_capped", "month_resumed", "errors", "ops", "ops_by_channel"}."""
    with conn.transaction():
        rows = conn.execute(
            f"UPDATE business b SET paused_reason = 'no_funds', updated_at = now() "
            f"WHERE b.status = 'active' AND b.ads_mode = 'managed' AND b.paused_reason IS NULL "
            f"AND EXISTS (SELECT 1 FROM wallet_entry w WHERE w.business_id = b.id "
            f"AND (w.kind IN ('topup', 'spend') OR (w.kind = 'adjustment' AND w.amount_cents > 0))) "
            f"AND (SELECT sum(w.amount_cents) FROM wallet_entry w WHERE w.business_id = b.id) <= 0 "
            f"AND {shard_sql('b.id')} RETURNING b.id",
            {"i": i, "n": n},
        ).fetchall()
        for r in rows:
            audit.record_job(
                conn,
                job="spend-guard",
                tool="job.spend_guard",
                business_id=r["id"],
                args={},
                result={"paused_reason": "no_funds"},
            )
    settings = config.load()
    funds = _funds_stop(conn, settings, i, n)
    month = _month_cap(conn, settings, i, n)
    for channel, ops in funds.pop("ops_by_channel").items():
        month["ops_by_channel"][channel] = month["ops_by_channel"].get(channel, 0) + ops
    month["ops"] += funds.pop("ops")
    month["errors"] += funds.pop("errors")
    for channel, why in funds.pop("stopped_by_channel", {}).items():  # a NewsBreak lift held for the day (T6-4)
        _stop(month, channel, why)
    return {"paused": len(rows) + funds.pop("flagged"), **funds, **month}


def _funds_stop(conn: psycopg.Connection, settings: config.Settings, i: int, n: int) -> dict:
    """The no-funds stop on the channels Admiry pays for (A3c-1; the channels spec's guard over the balance, before
    M1m's `available` and its reserve): for every active prepaid business of this shard with a managed channel
    Admiry runs from the balance (`settleable`) that is running, or paused for `no_funds`:
      - the flag also when the balance less the spend not posted to it yet (`_unposted_cents`: today's, and closed
        days waiting for their late read) is at or below zero -- the platforms report spend within hours while
        wallet-settle posts a day the day after, and Admiry pays them meanwhile (Google: Admiry's own card) -- one
        audit row, as the flag above;
      - flagged (`paused_reason` 'no_funds'): each such channel that runs is paused with `no_funds`
        (`ads_run.pause_channels`: the platform is told, or the pause is owed and the hourly step sends it), one audit
        row per channel paused;
      - the flag lifted when the balance is above zero and above the spend not posted yet (as a top-up's
        `wallet.resume_if_funded` lifts it; here also when that top-up landed between this run's read and its flag);
      - not flagged (a top-up or an operator's credit lifted the flag): each `no_funds` pause is lifted
        (`ads_run.resume_channels`: that pause alone -- the owner's own beside it stands), under the channel's `live`
        switch for ads built (under `off` or `validate` their lift could only fail or be checked), and never on
        NewsBreak while it is held for the UTC day (`_held`, T6-4: the run says so in `stopped_by_channel`) -- the next
        run re-flags first if the money is still not there, so nothing turns on that the balance cannot pay for.
    Level-triggered both ways, as the month cap. One business's failure is logged by its type and counted in
    `errors`. Returns {"flagged", "funds_paused", "funds_resumed", "errors", "ops", "ops_by_channel"}."""
    out = {"flagged": 0, "funds_paused": 0, "funds_resumed": 0, "errors": 0, "ops": 0, "ops_by_channel": {}}
    channels = settleable()
    found = conn.execute(
        "SELECT DISTINCT c.business_id FROM channel_campaign c JOIN channel_account a ON a.business_id = c.business_id "
        "AND a.channel = c.channel AND a.mode = 'managed' JOIN business b ON b.id = c.business_id "
        "AND b.status = 'active' AND b.ads_mode = 'managed' WHERE c.channel = ANY(%(chs)s) "
        "AND (c.state IN ('queued', 'launching', 'live', 'limited') "
        "OR (c.state = 'paused' AND c.paused_reason = 'no_funds')) "
        f"AND {shard_sql('c.business_id')} ORDER BY c.business_id",
        {"i": i, "n": n, "chs": channels},
    ).fetchall()
    for r in found:
        try:
            _funds_stop_business(conn, settings, r["business_id"], channels, out)
        except Exception as e:  # noqa: BLE001 - one business never stops the guard; its message may hold platform words
            log.warning("spend-guard: %s on business %s (funds)", type(e).__name__, r["business_id"])
            out["errors"] += 1
    return out


def _funds_stop_business(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channels: list[str], out: dict
) -> None:
    """`_funds_stop` for one business."""
    managed = [c for c in registry.managed_channels(conn, biz) if c in channels]
    with wallet.lock(conn, biz), conn.transaction():
        unposted = _unposted_cents(conn, biz, managed)
        balance = wallet.balance_cents(conn, biz)
        flagged = conn.execute(
            "UPDATE business SET paused_reason = 'no_funds', updated_at = now() "
            "WHERE id = %s AND paused_reason IS NULL AND %s > 0 AND %s <= 0 RETURNING id",
            (biz, unposted, balance - unposted),
        ).fetchone()
        if flagged:
            audit.record_job(
                conn,
                job="spend-guard",
                tool="job.spend_guard",
                business_id=biz,
                args={},
                result={"paused_reason": "no_funds", "unposted_cents": unposted, "funds_stop": "business"},
            )
            out["flagged"] += 1
        # the other way, as `wallet.resume_if_funded` lifts it: a top-up the webhook committed between this run's read
        # of the balance and its flag (the webhook takes no wallet lock) would otherwise leave a funded business paused
        conn.execute(
            "UPDATE business SET paused_reason = NULL, updated_at = now() "
            "WHERE id = %s AND paused_reason = 'no_funds' AND %s > 0 AND %s > 0",
            (biz, balance, balance - unposted),
        )
    row = conn.execute("SELECT paused_reason FROM business WHERE id = %s", (biz,)).fetchone()
    rows = [r for r in ads_run.rows(conn, biz) if r["channel"] in managed]
    if row["paused_reason"] == "no_funds":
        running = [r["channel"] for r in rows if r["state"] in ("queued", "launching", "live", "limited")]
        if not running:
            return
        done = ads_run.pause_channels(conn, settings, biz, channels=running, by="job:spend-guard", reason="no_funds")
        _guard_ops(out, done)
        for channel in running:
            if (done["channels"].get(channel) or {}).get("state") == "paused":
                audit.record_job(
                    conn,
                    job="spend-guard",
                    tool="job.spend_guard",
                    business_id=biz,
                    args={"channel": channel},
                    result={"paused_reason": "no_funds", "channel": channel, "funds_stop": "channel"},
                )
                out["funds_paused"] += 1
        return
    under = [
        r["channel"]
        for r in rows
        if r["state"] == "paused"
        and r["paused_reason"] == "no_funds"
        and (registry.exec_mode(settings, r["channel"]) == "live" or not ads_run.has_ads(conn, biz, r["channel"]))
    ]
    for channel in [ch for ch in under if _held(conn, settings, ch)]:  # T6-4: a lift is no stop -- the next UTC day
        _stop(out, channel, _why(conn, channel))
        under.remove(channel)
    if under:
        done = ads_run.resume_channels(conn, settings, biz, reason="no_funds", by="job:spend-guard", channels=under)
        _guard_ops(out, done)
        out["funds_resumed"] += sum(
            1 for ch in under if (ads_run.get(conn, biz, ch) or {}).get("paused_reason") != "no_funds"
        )


def _month_spent(conn: psycopg.Connection, biz: str, plan: dict | None, row: dict) -> bool | None:
    """Spec 2026-09-29-admiry-channels-design §6.4: whether the channel's spend this month in its account's own
    calendar (`ads_run.month_spend`, the channels card's month-to-date) is at or over the approved plan's monthly
    amount for it (`ads_run.month_amount`, the card's cap too); None -- neither -- when the plan puts no amount on it."""
    amount = ads_run.month_amount(plan, row["channel"])
    return None if amount is None else ads_run.month_spend(conn, biz, row["channel"], row) >= amount


def _guard_ops(out: dict, done: dict) -> None:
    """The platform operations the guard's own pauses and resumes sent (the outcomes of `pause_channels` and
    `resume_channels`), into its details: `ops_sent_today` counts them against each channel's budget beside the ads
    jobs' -- counted, never gated: stopping spend is always allowed, and a lift is sent only under the channel's `live`
    switch, once per cap (or again the next hour, when one could not be sent) -- but NewsBreak's lift, no stop, waits
    for the next UTC day while NewsBreak is held for this one (M1 Task 6, T6-4: `_held`)."""
    for channel, outcome in done["channels"].items():
        ops = outcome.get("ops") or 0
        out["ops"] += ops
        out["ops_by_channel"][channel] = out["ops_by_channel"].get(channel, 0) + ops


def _month_cap_business(conn: psycopg.Connection, settings: config.Settings, biz: str, out: dict) -> None:
    """The month cap for one business, per channel against the approved plan's amount for it: each running channel
    whose month is at its amount is paused (`month_cap`), marked with when (`resources.month_cap_at`, informational:
    set as the cap is applied and never again -- only running rows are capped -- and forgotten with the cap) and
    audited once -- never one someone paused by hand on the platform (H33, M0 Task 9 D6: `ads_run.pause` leaves it
    as it is); each `month_cap` pause whose month is under its amount again -- the account's month rolled over, or
    the owner approved a bigger amount -- is lifted, each channel under its own switch: only where that switch is
    `live` (a channel at `validate` or `off` beside a `live` one keeps its cap), never on a channel the approved
    plan no longer funds, and never on NewsBreak while it is held for the UTC day (its day budget spent, or two
    throttles in a row: `_held`, T6-4) -- the run says so (`stopped_by_channel`) and the next day lifts it."""
    plan = ads_run.approved(conn, biz)
    rows = [r for r in ads_run.rows(conn, biz) if registry.known(r["channel"])]
    for row in rows:
        channel = row["channel"]
        if row["state"] not in ("live", "limited") or not _month_spent(conn, biz, plan, row):
            continue
        done = ads_run.pause_channels(conn, settings, biz, channels=[channel], by="job:spend-guard", reason="month_cap")
        _guard_ops(out, done)
        # a row someone paused by hand on the platform is never claimed (H33, M0 Task 9 D6): `pause` leaves it as it
        # is -- not paused -- and it gets no mark and no audit row
        if (done["channels"].get(channel) or {}).get("state") == "paused":
            ads_run.remember(conn, biz, channel, {"month_cap_at": ads_run.now().isoformat()})
            audit.record_job(
                conn,
                job="spend-guard",
                tool="job.spend_guard",
                business_id=biz,
                args={"channel": channel},
                result={"paused_reason": "month_cap", "channel": channel},
            )
            out["month_capped"] += 1
    under = [  # under `off` or `validate` a lift could only fail or be checked: none is tried until `live`
        r["channel"]
        for r in rows
        if r["state"] == "paused"
        and r["paused_reason"] == "month_cap"
        and registry.exec_mode(settings, r["channel"]) == "live"
        and _month_spent(conn, biz, plan, r) is False  # a channel the plan no longer funds (None) stays capped
    ]
    for channel in [ch for ch in under if _held(conn, settings, ch)]:  # T6-4: a lift is no stop -- the next UTC day
        _stop(out, channel, _why(conn, channel))
        under.remove(channel)
    if under:
        done = ads_run.resume_channels(conn, settings, biz, reason="month_cap", by="job:spend-guard", channels=under)
        _guard_ops(out, done)
        lifted = [ch for ch in under if (ads_run.get(conn, biz, ch) or {}).get("paused_reason") != "month_cap"]
        out["month_resumed"] += len(lifted)


def _month_cap(conn: psycopg.Connection, settings: config.Settings, i: int, n: int) -> dict:
    """Spec 2026-09-29-admiry-channels-design §6.4 (M0 Task 7; final review I3), per channel, own-account channels
    included, against the approved plan's monthly amount for it (`ads_run.month_amount`, the channels card's cap
    too): a running channel whose month-to-date spend in its account's own calendar reached that amount
    (`_month_spent`) is paused with `month_cap` (`ads_run.pause_channels`: the platform is told, or the pause is owed
    to it) and one audit row (`job.spend_guard`); a `month_cap` pause whose month-to-date is under the amount again
    -- the account's month rolled over, or the owner approved more -- is lifted (`ads_run.resume_channels`: that pause
    alone -- the owner's own pause beside it stands) by the first run that sees it, and only while the channel's
    switch is `live` (under `off` or `validate` a lift could only fail or be checked); a raise that leaves the month
    at its amount, or a version that no longer funds the channel, lifts nothing. Level-triggered both ways: a row
    already paused gets no second pause, audit row, platform operation or new mark, and a lift is sent once. The
    operations the pauses and resumes sent are counted (`ops`, `ops_by_channel`: `_guard_ops`). One business's
    failure is logged by its type and counted in `errors`; the others carry on. Returns {"month_capped",
    "month_resumed", "errors", "ops", "ops_by_channel"}."""
    out = {"month_capped": 0, "month_resumed": 0, "errors": 0, "ops": 0, "ops_by_channel": {}}
    found = conn.execute(
        "SELECT DISTINCT c.business_id FROM channel_campaign c "
        "WHERE (c.state IN ('live', 'limited') OR (c.state = 'paused' AND c.paused_reason = 'month_cap')) "
        "AND EXISTS (SELECT 1 FROM plan p WHERE p.business_id = c.business_id AND p.status = 'approved') "
        f"AND {shard_sql('c.business_id')} ORDER BY c.business_id",
        {"i": i, "n": n},
    ).fetchall()
    for r in found:
        try:
            _month_cap_business(conn, settings, r["business_id"], out)
        except Exception as e:  # noqa: BLE001 - one business never stops the guard; its message may hold platform words
            log.warning("spend-guard: %s on business %s", type(e).__name__, r["business_id"])
            out["errors"] += 1
    return out


# the jobs that send platform operations (ruling S4): the ads jobs, spend-guard's pauses and resumes, page-check's
# (A3p-2 Task 4), the conversion upload's postbacks (M1 Task 6) and the hosting minimum's takedown pauses (A3p-3)
OPS_JOBS = ("ads-launch", "ads-sync", "spend-guard", "page-check", "conversion-upload", "hosting-minimum")


def ops_sent_today(conn: psycopg.Connection, channel: str = OPS_BUDGET_CHANNEL) -> int:
    """What `channel`'s rows sent today in the jobs that call a platform (`OPS_JOBS`), from their own ledger rows: each
    run's share for the channel (`details.ops_by_channel`, M0 Task 7) -- one channel's never counts against another's
    budget. Google Search's operations (A3 ruling S4) since the database's midnight -- and, for a run from before the
    jobs stepped every channel, its whole `details.ops` (Google's alone then; none in spend-guard's); NewsBreak's
    requests (M1 Task 6, T6-4) since midnight UTC, when its daily limit starts again (Rate Limits)."""
    if channel == OPS_BUDGET_CHANNEL:
        return conn.execute(
            "SELECT coalesce(sum(CASE WHEN details ? 'ops_by_channel' "
            "THEN (details->'ops_by_channel'->>%(channel)s::text)::int ELSE (details->>'ops')::int END), 0) AS ops "
            "FROM job_run WHERE job = ANY(%(jobs)s) AND started_at >= date_trunc('day', now())",
            {"channel": channel, "jobs": list(OPS_JOBS)},
        ).fetchone()["ops"]
    return conn.execute(
        "SELECT coalesce(sum((details->'ops_by_channel'->>%(channel)s::text)::int), 0) AS ops FROM job_run "
        f"WHERE job = ANY(%(jobs)s) AND started_at >= {UTC_TODAY}",
        {"channel": channel, "jobs": list(OPS_JOBS)},
    ).fetchone()["ops"]


def _throttled_today(conn: psycopg.Connection, channel: str) -> bool:
    """Whether a job's run this UTC day stopped `channel` after two throttled answers in a row (its `stopped_by_channel`):
    the throttle hold, read back by every job -- the channel's work but its stops waits for the next UTC day (`_stop`,
    where the holds are said)."""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM job_run WHERE job = ANY(%(jobs)s) "
        f"AND started_at >= {UTC_TODAY} AND details->'stopped_by_channel'->>%(channel)s::text = 'throttled') AS held",
        {"channel": channel, "jobs": list(OPS_JOBS)},
    ).fetchone()["held"]


def _budget_left(conn: psycopg.Connection, settings: config.Settings, channel: str) -> int | None:
    """What `channel`'s rows may still send today: Google Search's operations under `ADS_OPS_DAILY_BUDGET` (A3 S4),
    NewsBreak's requests under `NB_QPD_BUDGET` -- and none at all for the rest of the UTC day once NewsBreak answered
    two throttles in a row (M1 Task 6, T6-4). None for any other channel: never counted against a budget, never stopped
    by one."""
    if channel == OPS_BUDGET_CHANNEL:
        return settings.ads_ops_daily_budget - ops_sent_today(conn)
    if channel == NEWSBREAK:
        return 0 if _throttled_today(conn, channel) else NB_QPD_BUDGET - ops_sent_today(conn, channel)
    return None


def _why(conn: psycopg.Connection, channel: str) -> str:
    """Why a channel's day budget holds it: two throttled answers in a row earlier this UTC day, else the budget."""
    return "throttled" if channel != OPS_BUDGET_CHANNEL and _throttled_today(conn, channel) else "ops budget"


def _stop(out: dict, channel: str, why: str) -> None:
    """The day holds, said once here (M1 Task 6, T6-4 as the controller revised it in fix round 1, item 2). A run that
    finds a channel at its day budget -- Google Search's `ADS_OPS_DAILY_BUDGET`, NewsBreak's `NB_QPD_BUDGET` -- or
    NewsBreak answering two throttles in a row leaves the channel's work for the rest of the day (NewsBreak's: the UTC
    day) and says so in its details: Google Search's in `stopped`, A3's key as before; any other channel's in
    `stopped_by_channel` -- NewsBreak's "ops budget" or "throttled", which every job reads back (`_throttled_today`)
    and nothing in the run overwrites. Under EITHER hold the stops still go out: an owed pause, a version that leaves
    the channel out, an ad on its way out turned OFF, a pause NewsBreak ignored owed again -- stopping spend is always
    allowed, a 4034 may be NewsBreak's per-minute limit the client cannot tell from its daily one, and a stop that
    fails costs one request and stays owed. Only the work that is no stop waits: launches, updates, resumes and lifts,
    the whole read, the conversion upload."""
    if channel == OPS_BUDGET_CHANNEL:
        out["stopped"] = why
        return
    stopped = out.setdefault("stopped_by_channel", {})
    if stopped.get(channel) != "throttled":
        stopped[channel] = why


def _over(out: dict, channel: str, left: int | None) -> bool:
    """Whether `channel`'s non-stop work waits from here on in this run: its day budget reached (what its rows of this
    run sent, against `left`), or NewsBreak answered two throttles in a row during the run."""
    if (out.get("stopped_by_channel") or {}).get(channel) == "throttled":
        return True
    return left is not None and (out.get("ops_by_channel") or {}).get(channel, 0) >= left


def _throttled(channel: str, http: httpx.Client) -> bool:
    """Whether NewsBreak's last answers on the job's client were two throttles in a row (`client.throttled_in_a_row`,
    `NewsBreakError.throttled`): T6-4 stops its rows for the rest of the UTC day."""
    return channel == NEWSBREAK and newsbreak.throttled_in_a_row(http) >= THROTTLED_IN_A_ROW


def _held(conn: psycopg.Connection, settings: config.Settings, channel: str) -> bool:
    """Whether spend-guard's lift of `channel`'s month cap waits for the next UTC day: NewsBreak's while it is held for
    the day -- its requests at its budget, or two throttles in a row (T6-4: only stops go then). Google Search's never:
    A3 S4 counts spend-guard's pauses and lifts against Google's budget and never stops them."""
    if channel == OPS_BUDGET_CHANNEL:
        return False
    left = _budget_left(conn, settings, channel)
    return left is not None and left <= 0


def _switched_on(settings: config.Settings) -> list[tuple[str, str]]:
    """(channel, its switch) for each channel with an adapter whose switch (`registry.exec_mode`) is not `off`."""
    modes = {channel: registry.exec_mode(settings, channel) for channel in registry.ORDER if registry.known(channel)}
    return [(channel, mode) for channel, mode in modes.items() if mode != "off"]


def _ads_details(settings: config.Settings) -> dict:
    """A looping ads job's details, before its first row: the switch of each channel it steps (`modes`) and, per
    channel, the operations it sent (`ops_by_channel`: what each platform's quota counts); `checked`, `errors`,
    `ops` and `states` (the rows by the state each ended in) are over every channel."""
    on = _switched_on(settings)
    return {
        "modes": dict(on),
        "checked": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": {channel: 0 for channel, _mode in on},
        "states": {},
    }


def _tally(out: dict, channel: str, step: dict) -> None:
    """One row's step, counted into its job's details."""
    out["checked"] += 1
    out["ops"] += step["ops"]
    out["ops_by_channel"][channel] += step["ops"]
    out["states"][step["state"]] = out["states"].get(step["state"], 0) + 1


def ads_launch(conn: psycopg.Connection, i: int, n: int) -> dict:
    """The retry half of every channel's launch (spec 2026-09-28 admiry-google-search-execution-design §3, §5; A3
    Task 4; M0 Task 7): the approval's own request launches first (`ads_run.launch_now`); this job takes one lifecycle
    step for each row of its shard that still needs one (`ads_run.due`: a launch not finished, a row waiting for its
    account, a newer approved version not yet on the ads, a pause the platform has not been told yet) -- channel by
    channel in card order, every channel with an adapter whose switch is not `off`, each on one client of its own
    adapter, with the connection idle while the platform answers. `ads_run.launch` keeps each row's outcome and audit
    rows, so one business's failure is on its own row and the run carries on; a step an adapter cannot take
    (`Unsupported`) leaves its row as it is. Every switch `off` sends nothing. Ruling S4, Google Search's alone: the
    operations its rows of this job, ads-sync, spend-guard and page-check sent today
    (`ops_sent_today`) count against `ADS_OPS_DAILY_BUDGET`; at the budget the job stops Google Search's rows for the day and says so in `stopped` --
    except for owed pauses (ruling S11), taken first and sent even past the budget: stopping spend is always allowed,
    and one costs an operation or two, within Explorer's own 2,880. M1 Task 6 (T6-4), NewsBreak's the same way under
    its own `NB_QPD_BUDGET`, and for the rest of the UTC day after two throttled answers in a row, said in
    `stopped_by_channel`; under either hold the steps that stop spend -- an owed pause, a version that leaves a channel
    out, which pauses it, and a refused row's owed stop (`ads_run.stops_spend`; M1 follow-up fix round 1), for every
    channel -- still go (`_stop`, where the holds are said). Details: `_ads_details`."""
    settings = config.load()
    out = _ads_details(settings)
    for channel in out["modes"]:
        _launch_channel(conn, settings, channel, i, n, out)
    return out


def _stopping(conn: psycopg.Connection, settings: config.Settings, channel: str, row: dict) -> bool:
    """Whether a due row's step stops spend: an owed pause (`pausing`), a version that leaves the channel out, or a
    refused row's owed stop (`ads_run.stops_spend`)."""
    return row["pausing"] or ads_run.stops_spend(conn, settings, row["business_id"], channel)


def _launch_channel(
    conn: psycopg.Connection, settings: config.Settings, channel: str, i: int, n: int, out: dict
) -> None:
    """ads-launch's step for each due row of `channel` in this shard, owed pauses first; once the channel's day budget
    holds it (or NewsBreak throttles twice in a row), only the steps that stop spend."""
    left = _budget_left(conn, settings, channel)
    rows = conn.execute(
        f"SELECT c.business_id, c.state = 'paused' AS pausing FROM channel_campaign c "
        f"WHERE ({ads_run.due(settings, channel)}) "
        f"AND {shard_sql('c.business_id')} ORDER BY c.state = 'paused' DESC, c.business_id",
        {"i": i, "n": n},
    ).fetchall()
    if left is not None and left <= 0:
        _stop(out, channel, _why(conn, channel))
        rows = [r for r in rows if _stopping(conn, settings, channel, r)]
    if not rows:
        return
    with registry.adapter(channel).client() as http:
        for r in rows:
            if _over(out, channel, left) and not _stopping(conn, settings, channel, r):
                _stop(out, channel, "ops budget")
                continue
            try:
                step = ads_run.launch(conn, settings, r["business_id"], channel, http=http)
            except Exception as e:  # noqa: BLE001 - one row never stops the batch; its message may carry platform words
                log.warning("ads-launch: %s on business %s (%s)", type(e).__name__, r["business_id"], channel)
                out["errors"] += 1
            else:
                _tally(out, channel, step)
            if _throttled(channel, http):
                _stop(out, channel, "throttled")


def ads_sync(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Spend, clicks, serving status and ad policy into `metric_daily` and each row (spec 2026-09-28
    admiry-google-search-execution-design §6; A3 Task 5; M0 Task 7 over every channel): every row of this shard with
    ads on file that is `live`, `paused`, `limited` or `launching`, channel by channel in card order -- every channel
    with an adapter whose switch is not `off`, each on one client of its own adapter. `ads_run.sync` reads the platform
    (Google Search: three GAQL searches; a fourth, once per row, for the account's time zone; NewsBreak: its report,
    its ads, campaign and ad set, M1 Task 5b) and keeps each row's own outcome, so one business's failure is on its own
    row and the run carries on; a row its adapter cannot read (`Unsupported`) counts as ads-launch counts one: checked,
    in the state it stays in -- no error, nothing written, no operation. Every switch `off` reads nothing.
    Ruling S4: Google Search's rows share `ADS_OPS_DAILY_BUDGET` with ads-launch's, spend-guard's and page-check's
    (`ops_sent_today`); already at the budget when this run starts, they are left this round rather than spend even one
    more read -- unlike ads-launch's owed pauses, nothing Google's sync does is ever urgent enough to go over -- and
    another channel's rows are read as usual. NewsBreak's read does stop spend (M1 Task 6 fix round 1, item 1): it turns
    off ads on their way out and spots ads NewsBreak serves while the row shows them paused. So once NewsBreak is held
    for the UTC day (`_stop`: its `NB_QPD_BUDGET`, or two throttled answers in a row; `stopped_by_channel`), only its
    whole read waits -- each of its rows still gets the stops-only pass (`ads_run.sync_stops`, counted in
    `stops_only`). A read the platform failed still counts the requests it sent. Details: `_ads_details` (`states`:
    the state `sync` left each row in)."""
    settings = config.load()
    out = _ads_details(settings)
    for channel in out["modes"]:
        _sync_channel(conn, settings, channel, i, n, out)
    return out


def _sync_channel(conn: psycopg.Connection, settings: config.Settings, channel: str, i: int, n: int, out: dict) -> None:
    """ads-sync's read of each row of `channel` in this shard with ads on file -- once the channel is held for the day
    (its budget, or NewsBreak's two throttles in a row), only the stops-only pass of a channel whose adapter has one
    (`_sync_stops`); Google Search's rows are then left for the day (A3 S4)."""
    left = _budget_left(conn, settings, channel)
    adapter = registry.adapter(channel)
    stops = getattr(adapter, "pull_stops", None) is not None  # NewsBreak's: the read's own stops (fix round 1)
    if left is not None and left <= 0:
        _stop(out, channel, _why(conn, channel))
        if not stops:
            return
    rows = conn.execute(
        "SELECT business_id, state FROM channel_campaign c WHERE c.channel = %(channel)s "
        "AND c.state IN ('live', 'paused', 'limited', 'launching') "
        f"AND c.resources ? 'campaign' AND {shard_sql('c.business_id')} ORDER BY c.business_id",
        {"channel": channel, "i": i, "n": n},
    ).fetchall()
    if not rows:
        return
    with adapter.client() as http:
        for r in rows:
            if _over(out, channel, left):
                _stop(out, channel, "ops budget")
                if not stops:
                    break
                _sync_stops(conn, settings, adapter, r["business_id"], http, out)
                continue
            sent = adapter.ops_sent(http)
            try:
                step = ads_run.sync(conn, settings, r["business_id"], channel, http=http)
            except Unsupported:  # counted as ads-launch counts a step an adapter cannot take: as it is, nothing sent
                log.info(
                    "ads-sync: %s is not read yet; business %s left as it is", registry.label(channel), r["business_id"]
                )
                step = {"state": r["state"], "ops": 0}
            except Exception as e:  # noqa: BLE001 - one row never stops the batch; its message may carry platform words
                log.warning("ads-sync: %s on business %s (%s)", type(e).__name__, r["business_id"], channel)
                out["errors"] += 1
                failed = adapter.ops_sent(http) - sent  # what the platform saw of a read it failed counts all the same
                out["ops"] += failed
                out["ops_by_channel"][channel] += failed
                step = None
            if step is not None:
                _tally(out, channel, step)
            if _throttled(channel, http):
                _stop(out, channel, "throttled")


def _sync_stops(
    conn: psycopg.Connection, settings: config.Settings, adapter: Adapter, biz: str, http: httpx.Client, out: dict
) -> None:
    """One row's stops-only pass on a day its channel is held (`ads_run.sync_stops`, fix round 1, item 1): its requests
    counted like any other (`ops`, `ops_by_channel`; one NewsBreak did not answer included), the rows given it in
    `stops_only`; a failure is the row's error alone, retried the next hour."""
    channel = adapter.channel
    out["stops_only"] = out.get("stops_only", 0) + 1
    try:
        _metered(out, adapter, http, lambda: ads_run.sync_stops(conn, settings, biz, channel, http=http))
    except Exception as e:  # noqa: BLE001 - one row never stops the batch; its message may carry platform words
        log.warning("ads-sync: %s on business %s (%s), its stops", type(e).__name__, biz, channel)
        out["errors"] += 1
    if _throttled(channel, http):
        _stop(out, channel, "throttled")


def settleable() -> list[str]:
    """The channels Admiry can run from a balance: their adapter lists `managed` among its modes (final review I4).
    Google Search's since A3c-1 -- a prepaid business's ads run in an account Admiry pays Google for; its days count
    from the first account Admiry bound for it (`_since`: before that, ads in the owner's own account were the
    owner's with Google)."""
    return [c for c in registry.ORDER if registry.known(c) and "managed" in registry.adapter(c).capabilities().modes]


def wallet_settle(conn: psycopg.Connection, i: int, n: int) -> dict:
    """The ad spend of the channels Admiry runs from a business's balance, taken from that balance (spec
    2026-09-29-admiry-channels-design §3.3, §9; M0 Task 7): for every business of this shard with a managed channel
    (`registry.managed_channels`) that Admiry can run from a balance (`settleable`), each `metric_daily` day of such a
    channel that is over in the channel's own account
    calendar (`day < ads_run.account_today`), was read by ads-sync at least SETTLE_LAG after it closed (`synced_at`
    at or after the next day's midnight in the account's zone plus the lag: an earlier read may be short of the day's
    cost, and a day is posted once) and has no `spend` entry yet becomes one (`wallet.spend`, ref "{channel}:{day}"):
    the cost rounded up to the cent, one audit row each (`job.wallet_settle`), day after day under the business's
    balance lock (`wallet.lock`), a day's entries in one transaction -- and, once a day's are in,
    `wallet.absorb_overspend(conn, business_id, day=day)` when it exists (M1m adds it). A closed day not read that
    late yet waits for a later run (`waiting`). Never today (the platform still adds to it), never a day twice (its
    ref: a platform's later revision of a posted day is an operator's adjustment), never an own-account channel (the
    owner pays that platform), nothing for a day that cost nothing. One business's failure is on its own -- its day
    undone, logged by the error's type, counted in `errors` -- and the others are settled. Returns {"settled": the
    entries written, "waiting", "businesses": the businesses visited, "errors"}."""
    channels = settleable()
    found = conn.execute(
        "SELECT DISTINCT business_id FROM channel_account WHERE mode = 'managed' AND channel = ANY(%(chs)s) "
        f"AND {shard_sql('business_id')} ORDER BY business_id",
        {"i": i, "n": n, "chs": channels},
    ).fetchall()
    out = {"settled": 0, "waiting": 0, "businesses": len(found), "errors": 0}
    for r in found:
        try:
            _settle(conn, r["business_id"], channels, out)
        except Exception as e:  # noqa: BLE001 - one business never stops the others; its message is not ours to log
            log.warning("wallet-settle: %s on business %s", type(e).__name__, r["business_id"])
            out["errors"] += 1
    return out


# a business's days on a channel that cost something and have no spend entry yet, from `since` on
_UNPOSTED = (
    "metric_daily m WHERE m.business_id = %(b)s AND m.channel = %(ch)s AND m.day >= %(since)s AND m.cost_micros > 0 "
    "AND NOT EXISTS (SELECT 1 FROM wallet_entry w WHERE w.business_id = m.business_id AND w.kind = 'spend' "
    "AND w.ref = m.channel || ':' || to_char(m.day, 'YYYY-MM-DD'))"
)


def _since(conn: psycopg.Connection, biz: str, channel: str, zone) -> date | None:
    """The first day of the channel's account calendar that is the balance's: its adapter's `settles_from` (Google
    Search's: when Admiry first bound an account of its own for the business, A3c-1) as a day in the account's zone --
    None when the hook says nothing of Admiry's ran there yet; a channel without the hook (NewsBreak: managed only,
    every day of it is Admiry's) from its first day."""
    hook = getattr(registry.adapter(channel), "settles_from", None)
    if hook is None:
        return date.min
    start = hook(conn, biz)
    return start.astimezone(zone).date() if start is not None else None


def _unposted_cents(conn: psycopg.Connection, biz: str, channels: list[str]) -> int:
    """What the business's managed channels among `channels` (`settleable`) cost on days not posted to its balance
    yet -- today, and closed days still waiting for their late read, included: the spend the balance does not show
    yet, rounded up to the cent per day as `_settle` will post it."""
    total = 0
    for channel in [c for c in registry.managed_channels(conn, biz) if c in channels]:
        since = _since(conn, biz, channel, ads_run.account_zone(conn, biz, channel))
        if since is None:
            continue
        r = conn.execute(
            f"SELECT coalesce(sum((m.cost_micros + 9999) / 10000), 0)::bigint AS cents FROM {_UNPOSTED}",
            {"b": biz, "ch": channel, "since": since},
        ).fetchone()
        total += r["cents"]
    return total


def _closed_days(conn: psycopg.Connection, biz: str, channels: list[str]) -> tuple[list[dict], int]:
    """The days of the business's managed channels among `channels` (`settleable`) ready to post -- over in the
    channel's own account calendar, read at least SETTLE_LAG after they closed (`synced_at` at or after the next day's
    midnight in the account's zone plus the lag, in elapsed time), costing something and with no spend entry yet -- as
    {"day", "channel", "cost_micros"}, by day, then in card order; and how many closed days wait for such a read."""
    managed = [c for c in registry.managed_channels(conn, biz) if c in channels]
    ready: list[dict] = []
    waiting = 0
    for channel in managed:
        zone = ads_run.account_zone(conn, biz, channel)
        since = _since(conn, biz, channel, zone)
        if since is None:
            continue  # nothing of Admiry's ran there yet
        today = ads_run.account_today(ads_run.get(conn, biz, channel))
        unposted = conn.execute(
            f"SELECT m.day, m.channel, m.cost_micros, m.synced_at FROM {_UNPOSTED} AND m.day < %(today)s",
            {"b": biz, "ch": channel, "since": since, "today": today},
        ).fetchall()
        for d in unposted:
            closed = datetime.combine(d["day"] + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(UTC)
            if d["synced_at"] >= closed + SETTLE_LAG:
                ready.append(d)
            else:
                waiting += 1
    return sorted(ready, key=lambda d: (d["day"], managed.index(d["channel"]))), waiting


def _settle(conn: psycopg.Connection, biz: str, channels: list[str], out: dict) -> None:
    """wallet-settle for one business: the closed days of its `channels` read after their close, posted day by day;
    each day's entries count in `out` once its transaction is in."""
    with wallet.lock(conn, biz):
        ready, waiting = _closed_days(conn, biz, channels)
        out["waiting"] += waiting
        for day, entries in itertools.groupby(ready, key=lambda d: d["day"]):
            with conn.transaction():
                posted = 0
                for e in entries:
                    cents = -(-e["cost_micros"] // 10_000)  # ceil(cost_micros / 10_000), in whole numbers
                    if wallet.spend(conn, biz, channel=e["channel"], day=day, cost_cents=cents) is None:
                        continue  # a run beside this one posted it first
                    audit.record_job(
                        conn,
                        job="wallet-settle",
                        tool="job.wallet_settle",
                        business_id=biz,
                        args={"channel": e["channel"], "day": day.isoformat()},
                        result={"cost_cents": cents},
                    )
                    posted += 1
                absorb = getattr(wallet, "absorb_overspend", None)  # M1m adds it
                if posted and absorb is not None:
                    absorb(conn, biz, day=day)
            out["settled"] += posted


def conversion_upload(conn: psycopg.Connection, i: int, n: int) -> dict:
    """The leads an ad click brought, told back to the channel the click came from, hourly (plan
    2026-09-29-agent-m1-newsbreak P9, Task 6; rulings T6-2 to T6-4): for every channel whose adapter sends
    conversions (`Capabilities.conversions`: NewsBreak's, by click id) and whose switch is not `off`, the leads of this
    shard on that channel (`lead.channel`) with one of its click ids (`registry.CLICK_ID_KINDS`; NewsBreak's `nb_cid`,
    the value of its ads' `__CALLBACK_PARAM__` the page's script keeps, T6-1), not sent yet (`uploaded_at`), created
    within CONVERSION_WINDOW and not marked spam by the owner -- never every lead of a business: an owner-reported lead
    has no channel or click id (T6-2). Business by business, on one client of the adapter: the account the leads go to
    -- through the adapter's `conversion_account` hook when it has one (NewsBreak's looks up and keeps the lead event
    an operator makes by hand, T6-3); none yet, the leads wait (`waiting`) -- then each lead by itself
    (`push_conversions`, one `lead` event with its click id: no name or number of the person, D84), marked sent the
    moment the channel takes it; one the channel refuses (a click past its 7 days) stays unmarked and goes again the
    next hour (`rejected`), and the channel failing -- or the database, after a postback went (fix round 1, item 5) --
    stops that business's leads for this run (`errors`, logged by the error's type alone) while the next business goes
    on and every request the run made stays counted. One audit row per business with leads sent
    (`job.conversion_upload`). Under `validate` nothing is called -- the postback is a write, and the event's lookup
    is only for one -- and the leads that would go are recorded: all counted (`validated`, and per business in
    `would_send`), at most the first VALIDATE_LEADS ids (`would_send_leads`), since the same leads come back each hour;
    under `off`, nothing. NewsBreak's day holds hold this run as they hold the ads jobs (`_stop`; its requests here
    count in the budget): at `NB_QPD_BUDGET`, or after two throttled answers in a row, the leads wait for the next UTC
    day (`stopped_by_channel`). Returns {"modes", "sent", "rejected", "waiting", "errors", "ops", "ops_by_channel"},
    plus "validated", "would_send" and "would_send_leads" under `validate`."""
    settings = config.load()
    on = [(c, mode) for c, mode in _switched_on(settings) if registry.adapter(c).capabilities().conversions]
    out: dict = {
        "modes": dict(on),
        "sent": 0,
        "rejected": 0,
        "waiting": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": {channel: 0 for channel, _mode in on},
    }
    for channel, mode in on:
        _upload_channel(conn, settings, channel, mode, i, n, out)
    return out


def _upload_channel(
    conn: psycopg.Connection, settings: config.Settings, channel: str, mode: str, i: int, n: int, out: dict
) -> None:
    """conversion-upload's leads of `channel` in this shard (see conversion_upload), business by business."""
    kinds = [kind for kind, owner in registry.CLICK_ID_KINDS.items() if owner == channel]
    leads = conn.execute(
        "SELECT l.id, l.business_id, l.click_id, l.created_at FROM lead l WHERE l.channel = %(channel)s "
        "AND l.click_id_kind = ANY(%(kinds)s) AND l.click_id IS NOT NULL AND l.click_id <> '' "
        "AND l.uploaded_at IS NULL AND l.status <> 'spam' "
        f"AND l.created_at > now() - interval '{CONVERSION_WINDOW}' AND {shard_sql('l.business_id')} "
        "ORDER BY l.business_id, l.created_at, l.id",
        {"channel": channel, "kinds": kinds, "i": i, "n": n},
    ).fetchall()
    if not leads:
        return
    if mode == "validate":  # the postback is a write: recorded, never sent -- and the same leads come back each hour
        out["validated"] = out.get("validated", 0) + len(leads)
        counts = out.setdefault("would_send", {})
        for r in leads:
            counts[r["business_id"]] = counts.get(r["business_id"], 0) + 1
        ids = out.setdefault("would_send_leads", [])
        ids.extend(r["id"] for r in leads[: max(0, VALIDATE_LEADS - len(ids))])
        return
    left = _budget_left(conn, settings, channel)
    if left is not None and left <= 0:
        _stop(out, channel, _why(conn, channel))
        return
    adapter = registry.adapter(channel)
    with adapter.client() as http:
        for biz, theirs in itertools.groupby(leads, key=lambda r: r["business_id"]):
            if _over(out, channel, left):
                _stop(out, channel, "ops budget")
                break
            try:
                _upload_business(conn, settings, adapter, http, biz, list(theirs), out, left)
            except Exception as e:  # noqa: BLE001 - one business never stops the run; its words may be the database's
                log.warning("conversion-upload: %s on business %s (%s)", type(e).__name__, biz, channel)
                out["errors"] += 1


def _metered(out: dict, adapter: Adapter, http: httpx.Client, call: Callable):
    """`call()`, what it sent on `http` counted into the run's details (`ops`, `ops_by_channel`) however it ends."""
    before = adapter.ops_sent(http)
    try:
        return call()
    finally:
        sent = adapter.ops_sent(http) - before
        out["ops"] += sent
        out["ops_by_channel"][adapter.channel] += sent


def _upload_business(
    conn: psycopg.Connection,
    settings: config.Settings,
    adapter: Adapter,
    http: httpx.Client,
    biz: str,
    leads: list[dict],
    out: dict,
    left: int | None,
) -> None:
    """conversion-upload for one business's leads on `adapter`'s channel: the account they go to, then each lead."""
    channel = adapter.channel
    account = registry.account(conn, biz, channel)
    ready = getattr(adapter, "conversion_account", None)  # what a channel needs first (NewsBreak: its lead event)
    try:
        if account is not None and ready is not None:
            account = _metered(out, adapter, http, lambda: ready(conn, settings, account, http=http))
    except Exception as e:  # noqa: BLE001 - one business never stops the run; its message may carry platform words
        log.warning("conversion-upload: %s on business %s (%s)", type(e).__name__, biz, channel)
        out["errors"] += 1
        return
    finally:
        if _throttled(channel, http):
            _stop(out, channel, "throttled")
    if account is None:
        log.info("conversion-upload: business %s has no %s account to take its leads yet", biz, registry.label(channel))
        out["waiting"] += len(leads)
        return
    sent: list[str] = []
    for lead in leads:
        if _over(out, channel, left):
            _stop(out, channel, "ops budget")
            break
        event = ConversionEvent(
            lead_id=lead["id"], kind="lead", at=lead["created_at"], click_id=lead["click_id"], value_usd=None
        )
        try:
            done = _metered(
                out, adapter, http, lambda one=event: adapter.push_conversions(settings, account, [one], http=http)
            )
        except Exception as e:  # noqa: BLE001 - its other leads wait for the next run; the message may carry platform words
            log.warning("conversion-upload: %s on business %s (%s)", type(e).__name__, biz, channel)
            out["errors"] += 1
            break
        finally:
            if _throttled(channel, http):
                _stop(out, channel, "throttled")
        if done.accepted:
            conn.execute("UPDATE lead SET uploaded_at = now() WHERE id = %s AND uploaded_at IS NULL", (lead["id"],))
            sent.append(lead["id"])
        else:
            out["rejected"] += 1
    out["sent"] += len(sent)
    if sent:
        some = "1 lead" if len(sent) == 1 else f"{len(sent)} leads"
        audit.record_job(
            conn,
            job="conversion-upload",
            tool="job.conversion_upload",
            business_id=biz,
            args={"channel": channel},
            result={
                "sent": len(sent),
                "leads": sent,
                "note": f"told {registry.label(channel)} about {some} from its ads, without names or numbers",
            },
        )


PAGE_DOWN_AFTER = 2  # consecutive counted failures of a page that pause the ads pointing at it (spec §8)
FAIL_SPACING = timedelta(minutes=45)  # a failure counts only this long after the last counted one (ruling H33)
ALL_DOWN_MIN = 3  # H32: a run whose every fetch failed, over at least this many pages, is Admiry's own outage
PAGE_CHECK = "job:page-check"  # who page-check's pauses are by (`resources.paused_by`, M0 Task 9 D8)
SERVING = ("live", "limited", "launching")  # a resume that ends here brought the ads back (or on their way)
MAIL_PAGE_DOWN = "Your page is not opening, so your ads are paused"
MAIL_PAGE_UP = "Your page opens again, so your ads are back on"
MAIL_PAGE_GONE = ads_run.MAIL_PAGE_GONE  # H43 (the lifecycle sends it too: a move onto a page that is down)
# the audit notes, one row per channel (M0 Task 9 D10: each names its channel)
NOTE_PAUSED = "paused your ads on {label}: your page did not open twice in a row"
NOTE_RESUMED = "resumed your ads on {label}: your page opens again"
NOTE_PAGE_GONE = "paused your ads on {label}: the page they point at is down"  # H43; ads-launch's H29 note too
# A3p-5 batch 2, ruling R3-a: a way to Admiry's code the owner asked for (an install_request: the steps sent to their web
# person, or Admiry's team asked) is watched this long, until the code shows up
WATCH_DAYS = 14
CODE_MAIL_EVERY = timedelta(days=7)  # ruling R4-b: at most one "code missing" e-mail a page in this long


def _now() -> datetime:
    """page-check's clock (the spacing of counted failures, `FAIL_SPACING`); tests set it."""
    return datetime.now(UTC)


def _page_transport() -> httpx.BaseTransport | None:
    """The transport page-check fetches pages through: the real network (None). A test sets a MockTransport here
    (tools_hosting._check_transport's shape) -- and the job passes `crawl.resolve` read at call time, so a test's
    `monkeypatch.setattr(crawl, "resolve", ...)` reaches the fetch too."""
    return None


def _count_fetch(conn: psycopg.Connection, page: dict, up: bool, detail: str) -> dict | None:
    """The page's `check_json["reachable"]` after one fetch: {"fails", "last", "since", "detail"} -- `fails` the
    counted failures in a row (0 once the page opens), `last` when the last counted answer came, `since` when the
    current run of answers began (the first counted failure of a run of them, or the first answer after one),
    `detail` what that answer was. Ruling H33: a failure counts only when the last counted one is at least
    FAIL_SPACING old -- a second tick right after the timer's (a manual one, a `Persistent=true` catch-up) changes
    nothing, so "twice in a row" is always two answers most of an hour apart; an answer always counts, and resets.
    Ruling H18: merged into the saved 14-rule report (`||`), never replacing it -- `landing.save_check` keeps
    these in turn. One statement reads and writes the counter, and only while the page is still published at the
    address fetched: a page that moved or went down meanwhile starts its own count (None comes back)."""
    now = _now()
    r = conn.execute(
        "UPDATE page SET check_json = COALESCE(check_json, '{}'::jsonb) || jsonb_build_object('reachable', "
        "CASE WHEN NOT %(up)s AND COALESCE((check_json->'reachable'->>'fails')::int, 0) > 0 "
        "AND (check_json->'reachable'->>'last')::timestamptz > %(spaced)s THEN check_json->'reachable' "
        "ELSE jsonb_build_object("
        "'fails', CASE WHEN %(up)s THEN 0 ELSE COALESCE((check_json->'reachable'->>'fails')::int, 0) + 1 END, "
        "'last', %(now)s::text, "
        "'since', CASE WHEN (COALESCE((check_json->'reachable'->>'fails')::int, 0) > 0) = (NOT %(up)s) "
        "AND check_json->'reachable' ? 'since' THEN check_json->'reachable'->'since' ELSE to_jsonb(%(now)s::text) END, "
        "'detail', %(detail)s::text) END) "
        "WHERE id = %(id)s AND url = %(url)s AND status = 'published' RETURNING check_json->'reachable' AS reachable",
        {
            "up": up,
            "now": iso(now),
            "spaced": now - FAIL_SPACING,
            "detail": detail[:200],
            "id": page["id"],
            "url": page["url"],
        },
    ).fetchone()
    return r["reachable"] if r else None


def _own_pause(row: dict | None) -> bool:
    """The ads are paused by page-check itself: Admiry's pause for an unreachable page (`paused_reason`) -- never an
    owner's (their mark is their own), and never Admiry's other pauses (`page_down`: the page is gone, which a fetch
    cannot undo; a pause for money above all)."""
    return bool(row) and row["state"] == "paused" and row["paused_reason"] == "unreachable"


def _when_text(value: str | None) -> str:
    """An ISO time from the counters, the way an owner e-mail says it (leads.notify's own format)."""
    try:
        return datetime.fromisoformat(value).astimezone(UTC).strftime("%b %d, %Y %H:%M UTC") if value else "now"
    except ValueError:
        return value or "now"


def _status(rows: list[dict]) -> str:
    """The owner sentences of the channels an e-mail is about, one after the other."""
    return " ".join(ads_run.status_sentence(r) for r in rows)


def _audit(conn: psycopg.Connection, biz: str, result: dict, *, channel: str, row: dict, ops: int, note: str) -> None:
    """page-check's audit row for one channel (`job.page_check`: "{who} {note}"), its own outcome beside the page's."""
    audit.record_job(
        conn,
        job="page-check",
        tool="job.page_check",
        business_id=biz,
        args={"channel": channel},
        result={
            **result,
            "channel": channel,
            "to": row["state"],
            "ops": ops,
            "note": note.format(label=registry.label(channel)),
        },
    )


class _Clients:
    """One client per channel's adapter, opened the first time page-check sends that channel anything, and the
    operations each sent counted into the run's details (`ops`, `ops_by_channel`: each channel's count against its own
    budget, `ops_sent_today`) -- with NewsBreak's two throttled answers in a row, which leave its resumes for the next
    UTC day (T6-4)."""

    def __init__(self, stack: ExitStack, out: dict) -> None:
        self._stack, self._open, self._out = stack, {}, out

    def __call__(self, channel: str) -> httpx.Client:
        if channel not in self._open:
            self._open[channel] = self._stack.enter_context(registry.adapter(channel).client())
        return self._open[channel]

    def count(self, channel: str, ops: int) -> None:
        self._out["ops"] += ops
        self._out["ops_by_channel"][channel] = self._out["ops_by_channel"].get(channel, 0) + ops
        if channel in self._open and _throttled(channel, self._open[channel]):
            _stop(self._out, channel, "throttled")


def _carrying(conn: psycopg.Connection, biz: str, url: str) -> list[dict]:
    """The business's rows whose built ads carry the page at `url` -- channels that follow the page, their ads'
    address (`carried_url`) this page's (`landing_url`). Ads that send people elsewhere (a move not sent yet, or
    refused) are not this page's: its answer is not theirs. Every other channel is never touched (M0 Task 9 D4)."""
    out = []
    for row in ads_run.rows(conn, biz):
        channel = row["channel"]
        if not ads_run.follows_page(channel) or "campaign" not in (row["resources"] or {}):
            continue
        adapter = registry.adapter(channel)
        if adapter.carried_url(row["resources"]) == adapter.landing_url(url):
            out.append(row)
    return out


def _act(
    conn: psycopg.Connection,
    settings: config.Settings,
    fetched: tuple[dict, bool, str, dict],
    clients: _Clients,
    out: dict,
    left: dict[str, int | None],
) -> None:
    """What one counted fetch does to the ads that carry this page, channel by channel (see page_check): paused after
    the second counted failure in a row, or page-check's own pause undone once it opens; the owner told once per
    business and transition, naming the channels, and one audit row per channel."""
    page, up, detail, counts = fetched
    biz = page["business_id"]
    carrying = _carrying(conn, biz, page["url"])
    if not carrying:
        return  # no ads built that carry it: the counters are all there is to keep
    result = {"page_id": page["id"], "url": page["url"], "fails": counts["fails"] if not up else 0}
    if not up and counts["fails"] >= PAGE_DOWN_AFTER:
        paused: list[tuple[dict, int]] = []
        for row in carrying:
            if row["state"] not in ("live", "limited"):
                continue
            channel = row["channel"]
            # stopping spend: never held back by the day's operation budget (rulings S4, S12); a no-op on ads someone
            # paused by hand on the platform (H33, D6); beside a pause already there it stacks, never takes over
            done = ads_run.pause(
                conn, settings, biz, channel, http=clients(channel), by=PAGE_CHECK, reason="unreachable"
            )
            clients.count(channel, done["ops"])
            after = ads_run.get(conn, biz, channel)
            if _own_pause(after):
                paused.append((after, done["ops"]))
        if not paused:
            return  # someone's pause got there first, or nothing was running: nothing of page-check's to tell
        out["paused"] += len(paused)
        hosted_page = page["source"] in ("hosted", "admiry")
        if hosted_page:
            log.warning("page-check: business %s's hosted page did not open twice in a row: %s", biz, detail)
        email = mail.to_owner(
            conn,
            biz,
            "mail_page_down.txt.j2",
            MAIL_PAGE_DOWN,
            url=page["url"],
            detail=detail,
            since=_when_text(counts.get("since")),
            status=_status([r for r, _ops in paused]),
            hosted=hosted_page,
            labels=ads_run.labels([r["channel"] for r, _ops in paused]),
        )
        for after, ops in paused:
            _audit(
                conn, biz, {**result, "email": email}, channel=after["channel"], row=after, ops=ops, note=NOTE_PAUSED
            )
    elif up:
        resumed: list[tuple[dict, int]] = []
        for row in carrying:
            channel = row["channel"]
            if not _own_pause(row) or registry.exec_mode(settings, channel) != "live":
                continue  # `off` cannot, and `validate` would only have the platform check a resume: they wait for `live`
            if _over(out, channel, left[channel]):  # a resume is no stop: the next run, or (NewsBreak) the next UTC day
                _stop(out, channel, _why(conn, channel))
                continue
            adapter = registry.adapter(channel)
            # H33: the resume holds to what this run saw -- page-check's own pause, the ads at this page, the page still
            # published there -- under the business's lock and in its own UPDATE, never on this earlier read
            only_if = ads_run.ResumeIf(carried=adapter.landing_url(page["url"]), page_url=page["url"])
            try:
                done = ads_run.resume(
                    conn, settings, biz, channel, http=clients(channel), reason="unreachable", only_if=only_if
                )
            except ToolError as e:  # a step holds the business right now, or a pause landed: the next hour looks again
                log.info("page-check: resume for business %s on %s waits: %s", biz, channel, e)
                continue
            clients.count(channel, done["ops"])
            after = ads_run.get(conn, biz, channel)
            if after["state"] in SERVING:  # else still paused (someone's pause, the page moved), or the enable failed
                resumed.append((after, done["ops"]))
        if not resumed:
            return
        out["resumed"] += len(resumed)
        email = mail.to_owner(
            conn,
            biz,
            "mail_page_up.txt.j2",
            MAIL_PAGE_UP,
            url=page["url"],
            status=_status([r for r, _ops in resumed]),
            labels=ads_run.labels([r["channel"] for r, _ops in resumed]),
        )
        for after, ops in resumed:
            _audit(
                conn, biz, {**result, "email": email}, channel=after["channel"], row=after, ops=ops, note=NOTE_RESUMED
            )


def _pause_page_gone(conn: psycopg.Connection, settings: config.Settings, i: int, n: int, out: dict) -> None:
    """H43 (final review), page-check's backstop: running ads (`live`, `limited`) of a channel that follows the page
    that carry the business's own hosted address while no hosted page is live there -- unpublish_page took it down, or
    the owner's own page took its place -- get Admiry's page-down pause (`ads_run.pause_page_down`, by page-check:
    sent under the `live` switch, owed under `off`/`validate`, never on ads someone paused by hand on the platform),
    and the owner one e-mail per business naming the channels, with an audit row per channel; a later run finds them
    paused and does nothing more. The pause those two paths make themselves happens after their own transaction
    commits -- a step that can fail on its own (a database hiccup, the process dying in between), leaving nothing
    owed -- so without this nothing would ever stop paid clicks landing on a page that answers "gone" or "not
    found". The one exception: under a channel's `live` switch a move of its ads to the page's new address still on
    its way (`url_stale`) is ads-launch's to send within the hour -- pausing them instead would leave nothing to turn
    them back on once the owner's own page is live. Stopping spend: never held back by the day's operation budget
    (rulings S4, S12). One business's failure never stops the run."""
    following = [c for c in registry.ORDER if ads_run.follows_page(c)]
    live = [c for c in following if registry.exec_mode(settings, c) == "live"]
    found = conn.execute(
        "SELECT DISTINCT c.business_id, c.channel FROM channel_campaign c "
        "JOIN page p ON p.business_id = c.business_id "
        "WHERE c.channel = ANY(%(following)s) AND c.state IN ('live', 'limited') AND c.resources ? 'campaign' "
        "AND p.hostname IS NOT NULL AND NOT (p.source IN ('hosted', 'admiry') AND p.status = 'published') "
        "AND NOT (c.channel = ANY(%(live)s) AND c.resources @> '{\"url_stale\": true}') "
        f"AND {shard_sql('c.business_id')} ORDER BY c.business_id",
        {"i": i, "n": n, "following": following, "live": live},
    ).fetchall()
    if not found:
        return
    by_business: dict[str, list[str]] = {}
    for r in found:
        by_business.setdefault(r["business_id"], []).append(r["channel"])
    with ExitStack() as stack:
        clients = _Clients(stack, out)
        for biz, channels in by_business.items():
            try:
                _gone(conn, settings, biz, sorted(channels, key=registry.ORDER.index), clients, out)
            except Exception as e:  # noqa: BLE001 - one business never stops the run; its words may be the platform's
                log.warning("page-check: %s on business %s", type(e).__name__, biz)
                out["errors"] += 1


def _gone(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channels: list[str], clients: _Clients, out: dict
) -> None:
    """`_pause_page_gone` for one business: each of its `channels` still running paused for the page, then the
    owner's one e-mail and an audit row per channel paused."""
    paused: list[tuple[dict, int]] = []
    for channel in channels:
        if (ads_run.get(conn, biz, channel) or {}).get("state") not in ("live", "limited"):
            continue
        http = clients(channel)
        adapter = registry.adapter(channel)
        sent = adapter.ops_sent(http)  # the PAUSED status, when the switch sends it (ruling S4)
        done = ads_run.pause_page_down(conn, settings, biz, channel, http=http, by=PAGE_CHECK)
        ops = adapter.ops_sent(http) - sent
        clients.count(channel, ops)
        if done:  # else the ads carry another address after all, or someone's pause got there first
            paused.append((ads_run.get(conn, biz, channel), ops))
    if not paused:
        return
    out["page_down"] += len(paused)
    page = landing.current(conn, biz) or {}
    url = f"https://{page.get('hostname')}/"
    log.warning("page-check: business %s's ads pointed at %s with no page there; paused", biz, url)
    email = mail.to_owner(
        conn,
        biz,
        "mail_page_gone.txt.j2",
        MAIL_PAGE_GONE,
        url=url,
        status=_status([r for r, _ops in paused]),
        labels=ads_run.labels([r["channel"] for r, _ops in paused]),
    )
    result = {"page_id": page.get("id"), "url": url, "email": email}
    for after, ops in paused:
        _audit(conn, biz, result, channel=after["channel"], row=after, ops=ops, note=NOTE_PAGE_GONE)


def _code_seen(conn: psycopg.Connection, page: dict, seen: bool | None, out: dict, *, watched: bool) -> None:
    """A3p-5 batch 2, plan Q9 (spec W15): what one hourly look at an owner page says of Admiry's code on it -- `seen`,
    the check's tracking rule on the page read (`landing.tracking_found`) -- kept in `page.code_seen`. None, no page to
    judge (a bot challenge: a 403, 429 or 503, or a body with no `<html`; any other answer but a 2xx; no answer; a body
    that did not come whole: `landing.fetch_body`), changes nothing: an unreachable page is the Reachable rule's, never a
    missing code. Otherwise, against what Admiry last saw there (`site_install.code_found`: `code_seen`, or before the
    hourly check's first look since the page's last saved check, that check's tracking rule; ruling R0-d):
      - the code missing where it had been seen counts as gone after two hourly looks in a row without it (ruling R4-b,
        as a page that does not open counts as down after two failures, H33): the first is kept (`code_missed_at`;
        `code_missed`), a look within FAIL_SPACING of it is no second one, and the second makes `code_seen` false
        (`code_gone`) and e-mails the owner (mail_code_gone.txt.j2, Q9's words) -- at most once a page in
        CODE_MAIL_EVERY (`code_mailed_at`). The status's `say` says so while it stays gone (site_install.code_gone).
        The ads keep running -- a missing code stops the counting, not the visitors -- and the launch gate, which reads
        the page's saved report (W14), is not changed by it;
      - the code there ends a run of looks without it; there again after it went makes `code_seen` true, silently
        (`code_back`) -- unless the business has a pending install (`watched`): then the watch records it and tells the
        owner (`_watch_installs`, rulings R3-a, R4-a); there and seen by the saved check too: recorded, silently;
      - the code there while the saved check found none: nothing -- a check must find it first (Check now, or the watch
        of a pending install). So a `code_seen` stands only beside a saved check that found the code, and the row
        (site_install.row) never says the code is on the site while the launch would refuse for it;
      - no code where none was ever seen: nothing.
    Each write holds to the page as read (`_write_code`), so a change is told once, and a page that moved or was checked
    meanwhile is left to the next hour. page-check calls it once every page is counted (`_code_looks`), never in a run
    in which every owner page that had the code came back without it (ruling F-10)."""
    if seen is None:
        return
    was, missed, now = page["code_seen"], page["code_missed_at"], _now()
    if seen:
        if (was is False and watched) or (was is None and not site_install.checked_found(page)):
            return
        if was is True and missed is None:
            return
        if _write_code(conn, page, True, None) and was is False:
            out["code_back"] += 1
        return
    if not site_install.code_found(page):
        return
    if missed is None:
        if _write_code(conn, page, was, now):
            out["code_missed"] += 1
        return
    if missed > now - FAIL_SPACING or not _write_code(conn, page, False, None):
        return
    out["code_gone"] += 1
    mailed = page["code_mailed_at"]
    if mailed is not None and mailed > now - CODE_MAIL_EVERY:
        log.info("page-check: Admiry's code is gone from business %s's page; told within a week", page["business_id"])
        return
    host = urlparse(page["url"]).hostname or page["url"]
    email = mail.to_owner(
        conn,
        page["business_id"],
        "mail_code_gone.txt.j2",
        site_install.CODE_GONE_SUBJECT.format(host=host),
        gone=site_install.CODE_GONE.format(host=host),
        link=site_install.steps_link(),
    )
    if email == "sent":
        conn.execute("UPDATE page SET code_mailed_at = %s WHERE id = %s", (now, page["id"]))
    log.info("page-check: Admiry's code is gone from business %s's page; the e-mail: %s", page["business_id"], email)


def _write_code(conn: psycopg.Connection, page: dict, seen: bool | None, missed: datetime | None) -> bool:
    """`_code_seen`'s one write -- `code_seen` and the first look without the code (`code_missed_at`) -- made only while
    the page is still published at the address read, its `code_seen`, its first miss and its check as they were read (a
    check saved meanwhile clears both, landing.save_check). Whether it was made."""
    return (
        conn.execute(
            "UPDATE page SET code_seen = %(seen)s, code_missed_at = %(missed)s WHERE id = %(id)s AND url = %(url)s "
            "AND status = 'published' AND source = 'owner' AND code_seen IS NOT DISTINCT FROM %(was)s "
            "AND code_missed_at IS NOT DISTINCT FROM %(was_missed)s AND checked_at IS NOT DISTINCT FROM %(checked)s "
            "RETURNING id",
            {
                "seen": seen,
                "missed": missed,
                "id": page["id"],
                "url": page["url"],
                "was": page["code_seen"],
                "was_missed": page["code_missed_at"],
                "checked": page["checked_at"],
            },
        ).fetchone()
        is not None
    )


def _code_looks(
    conn: psycopg.Connection, looked: list[tuple[dict, bool | None]], pending: set[str], i: int, n: int, out: dict
) -> None:
    """Plan Q9 for the owner pages this run counted (`looked`: each page and whether its look found the code, None for
    no page to judge): `_code_seen` for each, the business's pending install deciding who records a return (`pending`,
    ruling R4-a) -- unless the run says nothing of the code (ruling F-10, the final review; H32's guard, for the code):
    no owner page judged carries Admiry's code, and at least ALL_DOWN_MIN of them are pages on which it had been seen
    (site_install.code_found). Every owner's code gone at one look is Admiry's own side -- an egress proxy's 200 block
    page, a 2xx interstitial in front of the box -- not every site at once: nothing is written (no look without the
    code is kept, no `code_seen` changes), nobody is e-mailed, `code_all_missing` says so and the run is logged at
    WARNING. A page judged without the code where none was ever seen neither counts toward the guard nor breaks it;
    any page judged with the code breaks it, and a run with fewer than ALL_DOWN_MIN pages that had the code goes the
    usual way. One page's failure never stops the run (`errors`)."""
    judged = [(page, seen) for page, seen in looked if seen is not None]
    lost = [page for page, seen in judged if not seen and _had_code(page)]
    if len(lost) >= ALL_DOWN_MIN and not any(seen for _page, seen in judged):
        out["code_all_missing"] = True
        log.warning(
            "page-check: all %d owner pages of shard %d/%d that had Admiry's code came back without it, and none with "
            "it; Admiry's own network is the likely cause, so nothing was recorded and nobody e-mailed",
            len(lost),
            i,
            n,
        )
        return
    for page, seen in looked:
        try:
            _code_seen(conn, page, seen, out, watched=page["business_id"] in pending)
        except Exception as e:  # noqa: BLE001 - one page never stops the run, nor its count
            log.warning("page-check: %s reading business %s's page for the code", type(e).__name__, page["business_id"])
            out["errors"] += 1


def _had_code(page: dict) -> bool:
    """Whether Admiry had seen its code on `page` before this look (site_install.code_found), for `_code_looks`'s guard:
    False for a page whose saved report cannot be read, which the guard then leaves out rather than stop the run."""
    try:
        return site_install.code_found(page)
    except Exception:  # noqa: BLE001 - one page never stops the run: it goes the usual way, where its failure counts
        return False


def _watched(conn: psycopg.Connection, i: int, n: int) -> list[str]:
    """Ruling R3-a: the businesses of this shard with a pending install -- a way to Admiry's code asked for (an
    install_request, `send` or `help`) in the last WATCH_DAYS days and not yet fulfilled: no saved check has found the
    code since (`found_at`, site_install.fulfil; ruling F-1, the final review: the watch ends for good at the first
    pass, its own or the owner's Check now) -- whether or not their row still needs the code."""
    rows = conn.execute(
        "SELECT DISTINCT r.business_id FROM install_request r JOIN business b ON b.id = r.business_id "
        "WHERE r.created_at > now() - make_interval(days => %(days)s) AND r.found_at IS NULL AND b.status = 'active' "
        f"AND {shard_sql('r.business_id')} ORDER BY r.business_id",
        {"i": i, "n": n, "days": WATCH_DAYS},
    ).fetchall()
    return [r["business_id"] for r in rows]


def _watch_installs(
    conn: psycopg.Connection, watched: list[str], looks: dict[str, tuple[str, bool | None]], out: dict
) -> None:
    """A3p-5 batch 2, ruling R3-a: the promise "Admiry checks the site every hour and tells {owner} when the code shows
    up" (the web person's e-mail) and "We'll tell you when the code shows up" (Q7, spec §3.1), kept. A business with a
    pending install (`watched`, `_watched`) whose plan row still needs the code (site_install.row) is watched each hour
    (`watched`): the page Check now would check (site_install.check_now: the owner page the ads use, else the owner's
    website) is looked at with the check's own fetch -- once: an owner page page-check read this hour is not fetched
    again (`looks`: page id -> its address and whether the code with its token was there, None for no page to judge;
    the result alone, never the page, ruling R4-e) -- and the check's tracking rule run on it for the business's page
    token. An hour without the code saves nothing and sends nothing. Ruling R4-d: after the fetch, what Check now would
    check is read again, and the row: an owner's change meanwhile -- another address named, or the pass recorded by
    their own Check now -- wins, and nothing is recorded. The first hour with the code records it the way Check now
    does, through the command's own steps (`watch_found`): check_landing_page's for an owner page on file
    (tools_pages.check_on_file: the check saved, its report e-mailed when whether it passes changed),
    set_landing_page's for the website with no page on file (tools_pages.name_page: it becomes the ads' page, its one
    report e-mailed) -- that report is the "tells you"; when its result was unchanged, so that the report stayed unsent
    (the code there while another rule still fails, or back after it went from a page whose check had passed), the
    owner gets the watch's own e-mail instead, once (ruling R4-a: mail_code_on.txt.j2). An audit row by Admiry in the
    command's name. The watch ends there for good: the check it recorded found the code, which fulfils the asks
    (site_install.fulfil, ruling F-1), so a code that comes and goes after it is the code-gone e-mail's and never
    brings the watch's e-mail back -- or after WATCH_DAYS. One business's failure never stops the run (`errors`)."""
    for biz in watched:
        try:
            _watch(conn, biz, looks, out)
        except Exception as e:  # noqa: BLE001 - one business never stops the run; its words may be a platform's
            log.warning("page-check: %s watching business %s's install", type(e).__name__, biz)
            out["errors"] += 1


def _watch(conn: psycopg.Connection, biz: str, looks: dict[str, tuple[str, bool | None]], out: dict) -> None:
    """`_watch_installs` for one business."""
    if (site_install.row(conn, biz) or {}).get("state") != "needs_code":
        return  # the code found, an Admiry page for the ads, or no website any more: nothing to watch
    target = site_install.check_now(conn, biz)
    page = landing.current(conn, biz)
    if target is None or page is None:
        return  # no page row at all: no code anywhere carries its token
    command, url = target
    out["watched"] += 1
    looked = looks.get(page["id"])
    if looked is not None and looked[0] == url:
        seen = looked[1]
    else:
        _up, _detail, html = landing.fetch_body(url, transport=_page_transport(), resolver=crawl.resolve)
        seen = landing.tracking_found(html, page["id"]) if html is not None else None
    if not seen:
        return
    if site_install.check_now(conn, biz) != target or (site_install.row(conn, biz) or {}).get("state") != "needs_code":
        return  # ruling R4-d: changed meanwhile -- the owner's own step stands
    # the commands' own steps, read at call time: a tool module, whose import loads mcp_app and its other tools
    from . import tools_pages

    if command == "check_landing_page":
        _page, report, _to, reason, _retry = tools_pages.check_on_file(conn, biz)
    else:
        _noop, _host, _page, report, _to, reason, _execution = tools_pages.name_page(conn, biz, url)
    if report is None:
        return
    told = None
    if reason == "unchanged" and site_install.tracking_passed(report):
        host = urlparse(url).hostname or url
        told = mail.to_owner(
            conn,
            biz,
            "mail_code_on.txt.j2",
            site_install.CODE_ON_SUBJECT.format(host=host),
            on=site_install.CODE_ON.format(host=host),
        )
    audit.record_job(
        conn,
        job="page-check",
        tool=command,
        business_id=biz,
        args={"url": url},
        result={"url": url, "ok": bool(report["ok"]), "email": reason, "code_on": told},
    )
    out["watch_found"] += 1


def page_check(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Is every published page up? (spec 2026-09-28 admiry-pages-design §8; plan H10, A3p-2 Task 4; M0 Task 9 over
    the channels). Every `published` page of an active business -- the owner's own or a hosted one -- of this shard
    is fetched once (`landing.reachable`, or for an owner page `landing.fetch_body`, which reads its body too: the
    check's own fetch, public addresses only, one redirect within the site, a 2xx status line = up, no answer within
    10 s = down) and counted in `check_json["reachable"]` (ruling H18; a failure counts only FAIL_SPACING after the
    last counted one, H33). Then, unless every page failed (H32), only the ads of the channels that follow the page
    (`ads_run.follows_page`) and carry this page's address (their adapter's `carried_url` is its `landing_url`) are
    touched, each channel on its own, and only through `ads_run` (pause/resume are switch-gated):
      - the second counted failure in a row pauses them when they are `live` or `limited` -- `pause(by=PAGE_CHECK,
        reason="unreachable")`, never on ads someone paused by hand on the platform (H33), and beside a pause already
        there it stacks, never takes it over -- and e-mails the owner once per business, naming the channels (the
        pause is the transition: a page still down an hour later finds the ads paused and does nothing more);
      - a page that opens again resumes them only when page-check's own pause is what holds them -- checked again
        under the lock and in the resume's own UPDATE (`ads_run.ResumeIf`, H33): an owner's pause, Admiry's
        page-down pause, or a pause for money is never undone here -- and only under that channel's `live` switch;
        the owner is told only of the channels whose ads end on their way back on (`SERVING`).
    H32: a run in which every fetched page failed, over at least ALL_DOWN_MIN pages, is Admiry's own network down,
    not the pages: the failures are counted, nothing is paused, nobody is e-mailed, and `all_down` says so. A page
    whose business has no ads built is still fetched and counted, and never pauses anything; a hosted page found
    down is logged for the operator. Under a channel's switch `off` or `validate`, its pause is recorded, owed to the
    platform (ruling S11, `ads_run.pause`): nothing goes out. Ruling S4: the operations sent are counted in `ops` and
    per channel (`ops_by_channel`; each channel's count in its day's budget, `ops_sent_today`); a pause goes out even
    past the budget (stopping spend always does), a resume waits for the next run (`stopped`) -- NewsBreak's for the
    next UTC day, at its own budget or after two throttled answers in a row (M1 Task 6, T6-4; `stopped_by_channel`).
    Expired previews are pruned here too (`pruned`), and a site bundle's leftovers (A3p-3 B13): expired zip links and
    upload links a week past their end (`links_pruned`), then the files nothing names any more (`blobs_freed`). Each
    pause and resume is an audit row per channel
    (`job.page_check`). One page's failure never stops the run (`errors`). `modes`: the switch of every channel that
    follows the page.

    H43, first and whatever the fetches then find (no fetch decides it, so H32's guard does not hold it back): the
    backstop for ads still running at the business's hosted address with no page live there (`_pause_page_gone`;
    `page_down` counts them).

    A3p-5 batch 2 (plan Q9, spec W15): an owner page is fetched with its body (`landing.fetch_body`: the same fetch with
    the body read at the check's limits -- up or down is still the status line's alone), and the check's tracking rule
    run on it (`_code_seen`): the code gone where it had been seen, two hourly looks in a row (ruling R4-b;
    `code_missed`, `code_gone`), e-mails the owner once a week at most; back after it went is recorded silently
    (`code_back`) -- for a business with a pending install, the watch's to record and tell (R4-a). Ruling F-10 (the
    final review), as H32 for the pauses: Q9 runs once every page is counted (`_code_looks`), and a run in which no
    owner page judged carries the code, at least ALL_DOWN_MIN of them pages that had it, records nothing of the code
    and e-mails nobody (`code_all_missing`): every owner's code gone at once is Admiry's own side (an egress proxy's
    block page, an interstitial). A page Admiry hosts is fetched as before (`landing.reachable`), its body never read.
    Ruling R3-a, last and unless every page failed: the pending installs are watched until their code shows up
    (`_watch_installs`; `watched`, `watch_found`), each to its first pass (ruling F-1)."""
    settings = config.load()
    following = [c for c in registry.ORDER if ads_run.follows_page(c)]
    out: dict = {
        "modes": {c: registry.exec_mode(settings, c) for c in following},
        "checked": 0,
        "down": 0,
        "paused": 0,
        "page_down": 0,
        "resumed": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": dict.fromkeys(following, 0),
        "pruned": hosted.prune_previews(conn, i=i, n=n),
        "links_pruned": site_upload.prune(conn, i=i, n=n),
        "blobs_freed": bundles.gc(conn, i=i, n=n),  # after the previews: their files cascade with them
        "rendered": 0,  # A3p-3 batch 4: the live site bundles the render check opened again (once a day each)
        "render_errors": 0,
        "render_mailed": 0,
        "code_missed": 0,  # A3p-5 batch 2 (Q9, ruling R4-b): owner pages looked at once without their code
        "code_gone": 0,  # ... and twice in a row: gone, the owner told (once a week at most)
        "code_back": 0,
        "watched": 0,  # ruling R3-a: pending installs looked at, and those whose code showed up
        "watch_found": 0,
    }
    _pause_page_gone(conn, settings, i, n, out)
    watched = _watched(conn, i, n)  # ruling R3-a: the businesses with a pending install
    pending = set(watched)
    rows = conn.execute(
        "SELECT p.id, p.business_id, p.url, p.source, p.code_seen, p.code_missed_at, p.code_mailed_at, p.check_json, "
        "p.checked_at FROM page p JOIN business b ON b.id = p.business_id WHERE p.status = 'published' "
        f"AND p.url <> '' AND b.status = 'active' AND {shard_sql('p.business_id')} ORDER BY p.business_id",
        {"i": i, "n": n},
    ).fetchall()
    counted: list[tuple[dict, bool, str, dict]] = []
    # an owner page's id -> its address and whether the code with its token was there (None: no page to judge) -- the
    # result alone, never the page, which goes with its iteration (ruling R4-e)
    looks: dict[str, tuple[str, bool | None]] = {}
    looked: list[tuple[dict, bool | None]] = []  # the owner pages counted and their look: Q9 once all are in (F-10)
    for r in rows:  # every page fetched and counted first: whether all of them failed decides what happens next
        page = dict(r)
        owner = page["source"] == "owner"
        try:
            if owner:
                up, detail, html = landing.fetch_body(page["url"], transport=_page_transport(), resolver=crawl.resolve)
                looks[page["id"]] = (
                    page["url"],
                    landing.tracking_found(html, page["id"]) if html is not None else None,
                )
            else:
                up, detail = landing.reachable(page["url"], transport=_page_transport(), resolver=crawl.resolve)
            counts = _count_fetch(conn, page, up, detail)
        except Exception as e:  # noqa: BLE001 - one page never stops the run
            log.warning("page-check: %s on business %s", type(e).__name__, page["business_id"])
            out["errors"] += 1
            continue
        if counts is None:  # moved or taken down while it was fetched: its new address (if any) starts its own count
            continue
        if owner:
            looked.append((page, looks[page["id"]][1]))
        out["checked"] += 1
        if not up:
            out["down"] += 1
            if page["source"] in ("hosted", "admiry"):
                log.info("page-check: business %s's hosted page did not open: %s", page["business_id"], detail)
        counted.append((page, up, detail, counts))
    _code_looks(conn, looked, pending, i, n, out)  # Q9, unless every page that had the code came back without it (F-10)
    if out["checked"] >= ALL_DOWN_MIN and out["down"] == out["checked"]:
        out["all_down"] = True
        log.warning(
            "page-check: all %d pages of shard %d/%d did not open; Admiry's own network is the likely cause, so "
            "nothing was paused and nobody e-mailed",
            out["checked"],
            i,
            n,
        )
        return out
    _render_bundles(conn, settings, [page["id"] for page, up, _detail, _counts in counted if up], out)
    if counted:
        left = {c: _budget_left(conn, settings, c) for c in following}  # what each channel's resumes may still send
        with ExitStack() as stack:
            clients = _Clients(stack, out)
            for fetched in counted:
                try:
                    _act(conn, settings, fetched, clients, out, left)
                except Exception as e:  # noqa: BLE001 - one page never stops the run; its words may be the platform's
                    log.warning("page-check: %s on business %s", type(e).__name__, fetched[0]["business_id"])
                    out["errors"] += 1
    _watch_installs(conn, watched, looks, out)
    return out


RENDER_KEEP = 50  # problems kept with the live site's render
RENDER_SUBJECT = "Something on your site {host} needs fixing"


def _render_bundles(conn: psycopg.Connection, settings: config.Settings, up: list[str], out: dict) -> None:
    """A3p-3 batch 4 (R8, P36): the live site bundles among the pages that just opened (`up`; never in a run where every
    page failed: that is Admiry's own network) are opened again by the render check once a day each. The result is
    kept on the page (`render_json`: its problems; `rendered_at`), which get_page shows; when a site that had none
    now has problems, its owner is e-mailed once (`mail_render.txt.j2`) -- nothing is paused: the operator decides.
    A site Admiry built itself (`source` admiry, A3p-6 batch 2 Task 5) is opened too, but what is found on it is Admiry's
    template's fault, never the owner's to fix: it is logged at ERROR with the problems for the operator and nobody is
    e-mailed. A renderer that does not answer leaves the page due for the next hour (`render_errors`)."""
    if not render_check.on(settings) or not up:
        return
    rows = conn.execute(
        "SELECT p.id, p.business_id, p.hostname, p.source, p.render_json FROM page p "
        "JOIN page_version v ON v.page_id = p.id AND v.version = p.version "
        "WHERE p.id = ANY(%s) AND v.kind = 'bundle' AND p.source IN ('hosted', 'admiry') "
        "AND p.hostname IS NOT NULL AND (p.rendered_at IS NULL OR p.rendered_at < now() - interval '1 day')",
        (up,),
    ).fetchall()
    for r in rows:
        try:
            rendered = render_check.render(settings, f"https://{r['hostname']}/", r["id"])
        except render_check.Unavailable:
            out["render_errors"] += 1
            continue
        had = (r["render_json"] or {}).get("problems") or []
        found = rendered["problems"][:RENDER_KEEP]
        doc = {"problems": found, "partial": rendered["partial"]}
        conn.execute("UPDATE page SET render_json = %s, rendered_at = now() WHERE id = %s", (Jsonb(doc), r["id"]))
        out["rendered"] += 1
        if found and r["source"] == "admiry":  # the template's fault, not the owner's: the operator's log, no e-mail
            log.error(
                "page-check: the site Admiry built for business %s at %s has problems in the daily render: %s",
                r["business_id"],
                r["hostname"],
                "; ".join(f"{p.get('file', '')} ({p.get('rule', '')}): {p.get('message', '')}" for p in found[:10]),
            )
        elif found and not had:
            subject = RENDER_SUBJECT.format(host=r["hostname"])
            sent = mail.to_owner(
                conn, r["business_id"], "mail_render.txt.j2", subject, host=r["hostname"], problems=found[:10]
            )
            out["render_mailed"] += sent == "sent"


def hosting_minimum(conn: psycopg.Connection, i: int, n: int) -> dict:
    """The hosting minimum, once a day (spec 2026-10-03-admiry-pages-a3p-3-design §3.1; plan
    2026-10-04-agent-a3p-3-1-hosting-minimum-and-muse Q5): every published site Admiry hosts for an active business of
    this shard is judged (`hosting_minimum.judge`: exempt, covered, the countdown's start, its 7-day and 1-day e-mails,
    and at its end the takedown, the ads that point at the site paused first); then every site taken down for the
    minimum whose 90 days are over loses its files and its address (`hosting_minimum.purge`). One business's failure
    never stops the run (`errors`). The takedown's pauses are platform operations, counted like page-check's (`ops`,
    `ops_by_channel`; ruling S4: stopping spend is never held back by the day's budget). Returns {"checked", "exempt",
    "covered", "started", "reminded", "cleared", "taken_down", "purged", "errors", "ops", "ops_by_channel", "mails"}:
    `mails` counts the e-mails by outcome (sent, unconfigured, no_owner, failed)."""
    settings = config.load()
    out = minimum.new_run()
    args = {"i": i, "n": n}
    live = conn.execute(
        "SELECT p.id, p.business_id, p.hostname, p.source, p.status FROM page p JOIN business b ON b.id = p.business_id "
        "WHERE p.status = 'published' AND p.source IN ('hosted', 'admiry') AND p.hostname IS NOT NULL "
        f"AND b.status = 'active' AND {shard_sql('p.business_id')} ORDER BY p.business_id",
        args,
    ).fetchall()
    for r in live:
        try:
            minimum.judge(conn, settings, r["business_id"], dict(r), out)
        except Exception as e:  # noqa: BLE001 - one business never stops the run
            log.warning("hosting-minimum: %s on business %s", type(e).__name__, r["business_id"])
            out["errors"] += 1
    # a day's slack on the database's own date: each page is judged against its business's calendar
    due = conn.execute(
        "SELECT id, business_id FROM page WHERE taken_down_reason = 'hosting_minimum' AND purge_after IS NOT NULL "
        f"AND purge_after <= current_date + 1 AND {shard_sql('business_id')} ORDER BY business_id",
        args,
    ).fetchall()
    for r in due:
        try:
            minimum.purge(conn, r["business_id"], r["id"], out)
        except Exception as e:  # noqa: BLE001 - one business never stops the run
            log.warning("hosting-minimum: the purge failed (%s) on business %s", type(e).__name__, r["business_id"])
            out["errors"] += 1
    return out


def consult_expire(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Consultations past their expires_at (24 h anonymous, 7 days claimed, spec 2026-09-26 §4.1) that
    were never finished -> expired; any hand-off token they were holding dies with them, so a stale
    website link never resolves to an old session (A1b-4c P9)."""
    cur = conn.execute(
        f"UPDATE consult_session SET status = 'expired', handoff_hash = NULL, handoff_expires_at = NULL "
        f"WHERE status IN ('open', 'ready') AND expires_at < now() AND {shard_sql('id')}",
        {"i": i, "n": n},
    )
    return {"expired": cur.rowcount}


def oauth_prune(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Housekeeping for the MCP OAuth tables (spec 2026-09-26 §6, A1b-4c P9): authorization txns and
    codes are one-use and short-lived, so anything used or expired more than a day ago is safe to drop;
    oauth_hit is a rolling rate-limit window that is also pruned as it is read, but this sweeps whatever
    a quiet IP left behind, past its own one-hour window."""
    args = {"i": i, "n": n}
    stale = "((used_at IS NOT NULL AND used_at < now() - interval '1 day') OR (used_at IS NULL AND expires_at < now() - interval '1 day'))"
    txns = conn.execute(f"DELETE FROM oauth_txn WHERE {stale} AND {shard_sql('id')}", args)
    codes = conn.execute(f"DELETE FROM oauth_code WHERE {stale} AND {shard_sql('code_hash')}", args)
    hits = conn.execute(f"DELETE FROM oauth_hit WHERE ts < now() - interval '1 hour' AND {shard_sql('ip')}", args)
    return {"txns": txns.rowcount, "codes": codes.rowcount, "hits": hits.rowcount}


def email_retry(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Inbound e-mail left `received` for over RETRY_AFTER_MINUTES (a restart between the webhook's 200 and the
    background task), reprocessed up to MAX_ATTEMPTS in all (spec 2026-09-28 §5.1; mail_in.process marks a third
    failure `failed` and tells the operator). The channel off sleeps instead (finding I1): reprocessing would only
    reach mail_dispatch.handle's replies, which mail_out.deliver now refuses anyway with the channel off."""
    from . import mail_in

    if not config.load().email_inbound:
        return {"retried": 0, "off": True}

    rows = conn.execute(
        "SELECT id FROM email_message WHERE direction = 'in' AND outcome = 'received' AND attempts < %(max)s "
        "AND created_at < now() - make_interval(mins => %(mins)s) AND mod(abs(hashtext(id)::bigint), %(n)s) = %(i)s "
        "ORDER BY created_at",
        {"max": mail_in.MAX_ATTEMPTS, "mins": mail_in.RETRY_AFTER_MINUTES, "i": i, "n": n},
    ).fetchall()
    for r in rows:
        mail_in.process(r["id"])  # its own connection; a failure is logged and counted there
    return {"retried": len(rows)}


def email_purge(conn: psycopg.Connection, i: int, n: int) -> dict:
    """D62: message text older than 90 days is forgotten (the row, its metadata and the audit stay); /m links a day
    past use or expiry and per-message keys revoked more than a day ago are dropped."""
    args = {"i": i, "n": n}
    purged = conn.execute(
        "UPDATE email_message SET new_text = NULL, purged_at = now() WHERE new_text IS NOT NULL AND purged_at IS NULL "
        f"AND created_at < now() - interval '90 days' AND {shard_sql('id')}",
        args,
    )
    links = conn.execute(
        "DELETE FROM email_link WHERE ((used_at IS NOT NULL AND used_at < now() - interval '1 day') "
        f"OR (used_at IS NULL AND expires_at < now() - interval '1 day')) AND {shard_sql('token_hash')}",
        args,
    )
    keys_ = conn.execute(
        "DELETE FROM api_key WHERE source = 'email' AND revoked_at IS NOT NULL AND revoked_at < now() - interval '1 day' "
        f"AND {shard_sql('id')}",
        args,
    )
    return {"purged": purged.rowcount, "links": links.rowcount, "keys": keys_.rowcount}


def support_mail(conn: psycopg.Connection, i: int, n: int) -> dict:
    """Ticket mail (spec 2026-09-30 §5.2, D142): send again the outbox rows a request could not (pending or failed for
    over 5 minutes, at most support_mail.MAIL_ATTEMPTS tries each) -- the only retry of a failed row, since a request's
    flush sends pending rows alone, so a row's tries stay about an hour apart --, close tickets that waited 7 days for a
    contact (no_contact; only while, under the ticket's lock, it still waits: a claim may have opened it since the
    select), and delete closed tickets with no business 90 days after closing (D137). Only the resend depends on
    AGENT_SUPPORT: while it is off nothing is sent and the rows wait, but the two retention steps still run, because
    D137's deletion is promised whatever the switch says. {sent, no_contact, purged, resend: "on" | "off"}."""
    from . import support
    from . import support_mail as outbox

    resend = config.load().support
    args = {"i": i, "n": n}
    sent = 0
    if resend:
        due = conn.execute(
            "SELECT DISTINCT ticket_id FROM ticket_event WHERE mail_state IN ('pending', 'failed') "
            f"AND mail_attempts < %(max)s AND created_at < now() - interval '5 minutes' AND {shard_sql('ticket_id')}",
            {**args, "max": outbox.MAIL_ATTEMPTS},
        ).fetchall()
        sent = sum(outbox.flush(r["ticket_id"], retry=True)["sent"] for r in due)
    stale = conn.execute(
        "SELECT id FROM support_ticket WHERE status = 'awaiting_contact' "
        f"AND created_at < now() - interval '7 days' AND {shard_sql('id')}",
        args,
    ).fetchall()
    closed = 0
    for r in stale:
        t = support.set_status(
            conn, r["id"], "closed", actor="job:support-mail", resolution="no_contact", expect="awaiting_contact"
        )
        if t["status"] == "closed" and t["resolution"] == "no_contact":
            closed += 1
    purged = conn.execute(
        "DELETE FROM support_ticket WHERE business_id IS NULL AND status = 'closed' "
        f"AND closed_at < now() - interval '90 days' AND {shard_sql('id')}",
        args,
    ).rowcount
    return {"sent": sent, "no_contact": closed, "purged": purged, "resend": "on" if resend else "off"}


JOBS: dict[str, Callable[[psycopg.Connection, int, int], dict]] = {
    "proposal-expire": proposal_expire,
    "claim-expire": claim_expire,
    "session-expire": session_expire,
    "ads-link-sync": ads_link_sync,
    "spend-guard": spend_guard,
    "ads-launch": ads_launch,
    "ads-sync": ads_sync,
    "page-check": page_check,
    "wallet-settle": wallet_settle,
    "hosting-minimum": hosting_minimum,
    "conversion-upload": conversion_upload,
    "consult-expire": consult_expire,
    "oauth-prune": oauth_prune,
    "email-retry": email_retry,
    "email-purge": email_purge,
    "support-mail": support_mail,
    "research-run": roles_runner.research_run,
    "analyst-run": roles_analyst.analyst_run,
    "digest": digest_job,
    "refusal-summary": refusal_summary.job,
}
