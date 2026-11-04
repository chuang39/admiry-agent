"""The ads lifecycle over `channel_campaign`, one row per business and channel, for every channel an adapter runs
(spec 2026-09-29-admiry-channels-design §5.3, §6.4, §8, D77, D87; plan 2026-09-29-agent-m0-channel-framework P2-P4,
Tasks 3-4; A3's spec 2026-09-28 admiry-google-search-execution-design §5, §6, §8 and its rulings S4-S18, which hold
for every channel). This module decides which step a row needs, when, under which switch, and keeps the row that
says where the ads are; the platform's own steps are the channel's adapter's (`registry.adapter(channel)`, the
protocol in `channels/base.py`): it builds, launches (everything paused), enables, updates, pauses and reads the
platform, and keeps its own bookkeeping in `resources`. Nothing here names a platform.

One row per business and channel (D87). Its `state`:

  waiting_account   approved while the channel's account was not ready (`account_status`: the adapter's
                    `check_account`, a mode the channel runs, a balance for a managed one); ads-launch re-checks it
                    every hour and launches it the hour the account is ready (D77). `last_error` holds the reason
  waiting_creative  approved, waiting for an image approved on the plan that Admiry's set has (D89; channels that
                    need one). A plan funds such a channel only with a picture, so this is one that left the set --
                    before the approval, or after it and before the launch (`_await_image`); never re-checked, a new
                    approval moves it on (M1 Task 7 fix round 1)
  queued     approved; nothing built on the platform yet (or nothing Admiry knows of: a lost response is found by
             name). With `validated_at` set it was checked under the `validate` switch (A3's `validated`, plan P4)
             and nothing was built; it launches like any `queued` row once the switch is `live` (ruling S9). A new
             approved version, or a resume, of ads never built is plain `queued` again (validated_at cleared), so
             `validate` checks that version too
  launching  built on the platform, everything paused, `resources` stored; the enable has not happened (P7, D50)
  live       enabled
  paused     paused by the owner, by Admiry, or by both (M0 Task 4 fix round 1, the controller's ruling): the
             owner's pause (their own, or their assistant's) is the mark `resources.owner_paused`; Admiry's is
             `paused_reason`, Admiry's reason only -- `dispute`, `stop_requested`, `no_funds`, `month_cap`, then the
             page's (A3p-2, M0 Task 9 D1): `page_down` (the page the ads point at is gone) and `unreachable` (the
             hourly page-check could not open it twice in a row), then `no_leads`, `dropped` (a version that no
             longer funds the channel) -- strongest first: a stronger one replaces a weaker one, never the reverse,
             so a page's pause never replaces, nor lifts, one for money. Nothing launches, updates or turns it on
             until every pause on it is lifted, each by its own resume (`resume_channels`): an owner's pause is never
             lifted by Admiry's resume, nor Admiry's by the owner's, and while one stands the row stays paused and
             says whose pause is left. A3's pauses (no mark) were all the owner's. When the platform could not be told,
             `resources.pause_owed` says so (ruling S11): the hourly ads-launch sends the pause under the `live`
             switch
  limited    the platform serves with a problem (billing, verification, disapprovals) -- set by ads-sync
  failed     the platform refused the request itself: the same request would be refused again, so nothing retries
             it; the next approved plan version starts over. Never while anything of it may be on at the platform
             (M1 follow-up R1): nothing reads a failed row, so ads built are stopped first (`_stop_then_fail`), the
             stop owed (`stop_owed`) while the platform does not take it
  operator   a report-only channel an Admiry operator runs (spec §5.3); never launched from here

and `plan_id` is the plan version built on the platform (or queued to be). A newer approved version than `plan_id`
on ads already built is applied by the adapter's `update` -- before the enable when it arrives mid-launch, so ads are
never enabled with a budget over the plan the owner approved last.

The page the ads send people to (A3p-2, M0 Task 9), for a channel whose adapter `follows_page`: a move of the page's
address under ads already built (publish_page, set_landing_page) is queued on the row (`resources.url_stale`) and
sent by the next step -- a URL-only `update` through the adapter's `move_page` (the money first, ruling S16), or,
when the approved version is one the platform refused, `refresh_url` (the address alone, never the refused content:
H26/S15). An address the platform refuses is recorded (`resources.refused_url`) and never sent again while the page
stays at it; ads left on a page that is down are paused (`page_down`). Before anything turns ads on, a launch or a
resume, they are moved to the page's address -- and, when the page they carry is down, paused instead (D1).

Every step is idempotent and recoverable (P8): the adapter stores what a create needs to be recovered before it sends
it, and looks the campaign up by the business id its name carries before any create; `resources` are stored before
the enable is sent. One step at a time per business (a session advisory lock): the approval's own request
(`launch_now`) and the hourly `ads-launch` never race. A pause never waits for that lock (ruling S17): when a step
holds it, the pause is recorded at once and owed to the platform, and no step turns on, or writes over, a row that
became `paused` while it ran.

Errors: a platform failure (`base.PlatformError`) leaves `last_error` (its owner sentence) and the state as it was --
the sweeper retries within the hour -- unless the platform refused the request itself, which is `failed` for a launch
(once the channel's stop went through, when ads are built: a stop that fails leaves the row `launching` with the stop
owed, sent alone by the next step under every hold, R1 and its fix rounds; a newer version approved while the request
or the stop was out is the one the row goes on to launch instead, `_stopped`) and, for an update of ads already running,
a mark that stops retrying that plan version (`resources.refused_plan_id`; the ads keep running the version they
have). A precheck refusal also stays on the row as `last_error` and is checked again every hour at no platform cost.
At approval, a channel never blocks the others (D77; M1 Task 5b, R17): a channel that is not the plan's primary
(`allocate.PRIMARY`) and fails its own rules -- its adapter's, its page rules, its geo -- is parked that way (`queued`
with the rule's sentence) instead of refusing the approval; only the primary's own rules, and the plan's, refuse it.
The page's check is a launch rule (ruling S8): it gates building ads and turning them on the first time, never a
change to ads already built.

`resources` belong to the adapter, but for the lifecycle's own keys: `paused_by` (who paused last: the owner, the
assistant, a job), `owner_paused` (the owner's pause stands -- the status line's "paused by you"), `pause_owed`,
`refused_plan_id`, `month_cap_at` (when spend-guard's month cap was set: a later pause moves `paused_at`, never
this; lifting Admiry's pause forgets it), `stop_owed` (a refusal whose stop the platform did not take yet -- its
sentence and the version it refused: the next step sends that stop alone, then fails the row or, a newer version
approved meanwhile, leaves it `launching` to launch that one; a pause the platform takes ends it; an approval keeps it
-- M1 follow-up fix rounds 1 and 2), and the page's: `url_stale` (the page moved since the ads were built or last
moved, H9/H26) and `refused_url` (an address the platform would not take for the ads, H27/H28: cleared once the ads
carry another, or a check that passes on the page there retries it). An approval never touches the adapter's keys.

Audit rows (`job.ads_launch`, actor job:ads-launch) record each transition -- states, the plan id, the operation
count and the owner sentence -- never a request body.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import allocate, audit, business, config, crawl, db, landing, mail, plans, wallet
from .channels import registry
from .channels.base import (
    MANAGED_NO_FEE,
    Account,
    AccountStatus,
    AddressRefused,
    Build,
    MetricDay,
    PlatformError,
    Unsupported,
    Words,
)

log = logging.getLogger(__name__)

STATES = (  # spec 2026-09-29 §5.3, the CHECK on channel_campaign.state; A3's `validated` is `queued` + validated_at
    "waiting_account",
    "waiting_creative",
    "queued",
    "launching",
    "live",
    "paused",
    "limited",
    "failed",
    "operator",
)
OWNER = "owner"  # the owner's pause (their own, or their assistant's): its own mark, resources.owner_paused
# Admiry's pauses for the page the ads send people to (A3p-2): `page_down` -- the page they carry is gone (unpublished,
# or the business's hosted address while the owner's own page is the live one, H29) -- and `unreachable` -- the hourly
# page-check could not open it twice in a row, the only pause page-check ever undoes. Publishing a page, or pointing
# the ads at another, lifts these and nothing else (M0 Task 9 D2)
PAGE_REASONS = ("page_down", "unreachable")
# Admiry's reasons for a pause (`paused_reason`, one at a time), strongest first: a stronger one replaces a weaker one,
# never the reverse (the controller's ruling, M0 Task 4 fix round 1). The money's first, then the page's (M0 Task 9
# D1): a page's event never replaces, and never lifts, a pause for money
ADMIRY_REASONS = ("dispute", "stop_requested", "no_funds", "month_cap", *PAGE_REASONS, "no_leads", "dropped")
REASONS = (OWNER, *ADMIRY_REASONS)
# the owner's words for why Admiry paused (the status line's "paused by Admiry: …")
PAUSE_WORDS = {
    "dispute": "a payment dispute is open",
    "stop_requested": "you asked to stop and get your money back",
    "no_funds": "the balance ran out",
    "month_cap": "this month's budget is spent",
    "page_down": "the page your ads point at is down",
    "unreachable": "your page did not open for Admiry's hourly check twice in a row",
    "no_leads": "it spent without bringing a lead",
    "dropped": "the plan you approved leaves it out",
}
# and what ends a page's pause, the sentence after it (M0 Task 9 D3: the owner's resume never lifts Admiry's pause --
# a page that is up again does)
PAUSE_ADVICE = {
    "page_down": "They stay off until you publish your page again or point your ads at a page that is up",
    "unreachable": "Your ads come back on once it opens again",
}
COLS = (
    "business_id, channel, plan_id, state, paused_reason, resources, last_error, validated_at, launched_at, "
    "paused_at, synced_at, updated_at"
)
SYNC_DAYS = 3  # ads-sync reads the last three days: late conversions land within the window (A3 §6)
DAYS_IN_MONTH = 30.4  # a channel's daily budget is its monthly amount / 30.4, rounded down to the cent (spec §6.4)
# the steps (`_work`) that stop spend: `launch_now` takes them before any other (R26), and the hourly ads-launch sends
# them under every day hold -- `stop` is a refused row's owed stop (M1 follow-up fix round 1, F2)
STOPS = ("drop", "repause", "stop")
# the lifecycle's own keys in `resources`; everything else there is the adapter's
_OWN = (
    "paused_by",
    "pause_owed",
    "owner_paused",
    "refused_plan_id",
    "month_cap_at",
    "url_stale",
    "refused_url",
    "stop_owed",
    "demo_paused",
)


def _keep_sql(keys: tuple[str, ...]) -> str:
    """The row's own current values of `keys` (server-side, at write time), for `resources = <new> || this`."""
    return "jsonb_strip_nulls(jsonb_build_object(" + ", ".join(f"'{k}', resources->'{k}'" for k in keys) + "))"


# what a whole new `resources` from an adapter keeps of the row's own: who paused it, the owner's pause mark, a pause
# the platform was not told yet and when the month cap was set, all of which a pause (and spend-guard right after its
# own) writes without waiting for the step's lock (ruling S17) -- and a move of the page queued meanwhile (`url_stale`,
# A3p-2 H22: a publish commits without the lock), which only a write that finds the page where the step built it
# clears (`_write_resources`)
_KEPT = ("paused_by", "pause_owed", "owner_paused", "month_cap_at", "url_stale")
_KEEP = _keep_sql(_KEPT)
# The page's address as `landing.current` reads it, inside a write's own CASE (server-side, at write time): a move
# that commits while a step's request is out keeps its `url_stale` through that step's write (A3p-2 H22, H28)
_PAGE_URL_NOW = (
    "(SELECT p.url FROM page p WHERE p.business_id = %(b)s "
    "ORDER BY (p.status = 'published') DESC, p.created_at DESC LIMIT 1)"
)
# `paused_reason` after an Admiry pause for %(reason)s: the stronger of the two (ADMIRY_REASONS' order)
_STRONGER = (
    "CASE WHEN paused_reason IS NULL OR array_position(%(order)s::text[], paused_reason) IS NULL "
    "OR array_position(%(order)s::text[], %(reason)s::text) < array_position(%(order)s::text[], paused_reason) "
    "THEN %(reason)s::text ELSE paused_reason END"
)
# A3p-2 H33 (M0 Task 9 D6), a write's own WHERE: the row carries the adapter's note that someone paused the ads by hand
# on the platform (`%(elsewhere)s`, None for a channel with no such note) -- and the writer is Admiry (`%(admiry)s`)
_ELSEWHERE = "(%(admiry)s AND COALESCE(last_error = %(elsewhere)s::text, false))"
# `ResumeIf.page_url`, in a resume's own WHERE: the business's page still published at that address (or no condition)
_STILL_PUBLISHED = (
    "(%(page_url)s::text IS NULL OR EXISTS (SELECT 1 FROM page p WHERE p.business_id = %(b)s "
    "AND p.status = 'published' AND p.url = %(page_url)s::text))"
)
# a paused row whose one pause was Admiry's `dropped` (a version left the channel out): lifted by a version that
# funds the channel again -- unless the owner's mark stands too
_ONLY_DROPPED = (
    "(channel_campaign.paused_reason IS NOT DISTINCT FROM 'dropped' "
    "AND NOT (channel_campaign.resources ? 'owner_paused'))"
)

# Owner sentences: ToolError text, channel_campaign.last_error and the approval tools' `say`. D52 and the JARGON
# tests: "your ads", the channel's own name (D76), "budget" -- never campaign / ad group / keyword / CPC.
OVER_LIMIT = "This plan's budget is above your monthly limit; change the plan or the limit first"
NO_PAGE = (
    "Your ads need a page to send people to: set up your page first (your own page's address, or one built with "
    "Admiry's kit)"
)
PAGE_UNCHECKED = "Your page has not been checked yet: check it first, so Admiry knows it is ready for {label}"
# a secondary channel's saved page report that predates one of its rules, at the approval -- which never fetches a page
# (final review Minor 1): its row parks with this, and its own step checks the page again
CHECKING_PAGE = "Admiry is checking your page for {label}"
PAGE_FIX = "Your page needs a fix before your ads can send people there: {fix}"
NO_PHONE = "What number should the calls ring? Tell me the business phone first"
NO_LOCATION = "Tell me the address the ads should centre on"  # plan P4, word for word
NO_RADIUS = "Tell me how far around the business your ads should reach, in miles"
REPORT_ONLY = "Admiry cannot start ads on {label} itself; ask for a plan without it"
UNDER_FLOOR = "{label} needs at least {floor} a month; raise its amount or leave it out of the plan"
NO_FUNDS = "Add funds to start {label}"  # a managed channel with no balance (spec §7.2: "Waiting for funds")
MODE_SOON = "Admiry cannot run {label} that way yet; your balance is safe"
NOT_READY = "{label} is not ready yet"  # an account not ready whose adapter gave no reason of its own
NOT_BUILT = "Your ads are not on {label} yet; there is nothing to change there"
NO_ADS = "You have no ads yet; there is nothing to pause"
NOT_PAUSED = "Your ads are not paused"
PAUSED_BY_ADMIRY = "Admiry paused these ads itself; they come back on once the reason is gone"
BUSY_NOW = "Admiry is changing your ads right now; try again in a minute"
SWITCHED_OFF = "Admiry cannot change your ads on {label} right now; an Admiry operator can do it for you"
MOVED = "The linked account changed; ask Admiry to move your ads"  # a re-link never moves built ads by itself
# a refused row whose stop the platform did not take yet (`stop_owed`): after the refusal's own sentence, never "turn
# on within the hour" (the M1 follow-up's fix round 2, G3) -- and its plan card's badge, never "Launching" (M1
# follow-up 2, W5; the channels card says the line's own clause)
STOPPING_ADS = "Admiry is switching your ads on {label} off"
SWITCHING_OFF = "Switching off"
# N4 (spec 2026-11-04-muse-verified-creatives-google-ads), the `live_paused` switch (config.LIVE_PAUSED, Google Search
# only): the real create, everything built paused, and nothing ever turns it on -- not the launch step (`_work` has no
# `enable` for it, `_create` stops after storing), not a resume (DEMO_PAUSED), not the hourly job (it steps through
# `_work`), and `_enable` and the Google adapter refuse behind them. Its row stays `launching`, marked
# `resources.demo_paused`: its clause, badge and audit note say so instead of "turns on"
DEMO_PAUSED = "Demo mode: these ads were built paused in Google Ads and are not turned on."
DEMO_CLAUSE = "built in Google Ads, paused — demo mode, nothing spends"
DEMO_BADGE = "Built in Google Ads, paused (demo mode)"
RETRY_SOON = "Admiry retries within the hour"
LAUNCH_LATER = "Your ads are waiting to launch; Admiry retries within the hour."
OFF = "Your ads are not launched yet, and nothing is spent on them until they are."
# A page moved under ads already built while the channel's switch is `off` (publish_page, set_landing_page -- A3p-2
# H22 minor, H28): `url_stale` is queued, and the move is sent once execution is on.
OFF_OLD_ADDRESS = "{label}: your ads keep their old address until execution is on."
# The owner's e-mail when Admiry pauses ads for a page that is no longer there (page-check's backstop, A3p-2 H43; and a
# move onto such a page held back, `_onto_page_down`): `mail_page_gone.txt.j2`
MAIL_PAGE_GONE = "Your ads point at a page that is no longer there, so they are paused"
# the same e-mail for a move held back: the ads still carry their old page, so it says so (`held`)
MAIL_PAGE_HELD = "Your page is no longer there, so your ads are paused instead of moving to it"
# A3p-2 H29: the other half of a refused address's advice -- a check that passes on the page at that address is the
# retry (`retry_refused_address`), so check_landing_page says it is on its way
ADDRESS_RETRY = "Now that the page passes, Admiry will try its address for your ads on {labels} again"
OFF_CHANNEL = "{label}: your ads are not launched yet, and nothing is spent on them until they are."
# The plan card's and `/plan/{id}`'s badge (`execution_line`) for a plan approved while the switch was `off`: that
# approval queued nothing, so nothing launches when the switch flips -- a new approval is the deliberate act (S18).
NOT_LAUNCHED_OFF = "Not launched (execution off): approve a new version once execution is on"
# a row whose channel has no adapter (none reaches owner text in production: rows are queued for adapters only)
_PLATFORM = Words(
    pause_owed="The platform was not told yet; an Admiry operator will pause your ads there",
    refused_change="The platform did not take all of your latest change; approve a changed version",
    checked="checked your ads (a test run: nothing is shown or spent)",
    checked_status="your ads were checked and found fine; they are not running yet",
    checked_badge="Checked; turns on once execution is live",
    held_back="live, but the platform is holding your ads back",
    repaused="finished pausing your ads",
    no_fee="Admiry charges no management fee",
)


# ---------------------------------------------------------------- the row


def get(conn: psycopg.Connection, biz: str, channel: str) -> dict | None:
    """The business's row for one channel; None without one."""
    r = conn.execute(
        f"SELECT {COLS} FROM channel_campaign WHERE business_id = %s AND channel = %s", (biz, channel)
    ).fetchone()
    return dict(r) if r else None


def rows(conn: psycopg.Connection, biz: str) -> list[dict]:
    """Every channel row of the business, in card order (`registry.ORDER`; a channel it does not name last)."""
    found = conn.execute(
        f"SELECT {COLS} FROM channel_campaign WHERE business_id = %s "
        "ORDER BY array_position(%s::text[], channel) NULLS LAST, channel",
        (biz, list(registry.ORDER)),
    ).fetchall()
    return [dict(r) for r in found]


def queue(
    conn: psycopg.Connection,
    biz: str,
    channel: str,
    plan_id: str,
    *,
    ready: bool,
    reason: str | None = None,
    image: bool = True,
    parked: str | None = None,
) -> None:
    """Spec §5.3 step 1, inside the approval's own transaction (`proposals.apply_approve_plan`): a new approved plan
    version for the business's ads on `channel`. Nothing built yet (no row, `queued` -- checked or not --, a waiting
    row, a `failed` launch) -> `queued` with this plan; `waiting_account` when the channel's account is not `ready`
    (its `reason`, check_account's sentence, kept in `last_error` for the card); `waiting_creative` when it is but
    the channel's image is not approved on the plan yet (`image`). `validated_at` is cleared, so `validate` checks
    this version as A3 checked every newly queued one (P4). Something built (`resources.campaign`) -> the row keeps
    the plan version that built it -- `launch` applies the newer one -- and its state, except that a `failed` enable
    goes back to `launching` so the new version is applied and turned on. A pause stays (approving a version is not
    resuming) -- but a `dropped` one (the plan left the channel out) is gone once a version funds the channel again:
    alone, the row comes back as if it had never been paused; beside the owner's mark, that pause stands. The last
    error is cleared, and so is the mark of a version the platform refused (`refused_plan_id`): the new version
    supersedes it. A refusal's stop still owed stays (`stop_owed`, M1 follow-up fix round 2, G2): the ads may still
    serve, so the next step sends that stop first -- and, the refused version no longer the approved one, leaves the
    row `launching` with its ads off for the step after it to launch this one. The adapter's own keys in `resources`
    are left alone (ruling S11).

    `parked` (M1 Task 5b, R17): the sentence of a secondary channel's own rule this version fails (`precheck`'s
    `park`) -- kept in `last_error` on a row that waits for its step (`queued`, or ads built), where every hourly step
    checks the rule again at no platform cost and goes ahead the hour it passes; a row waiting for its account or its
    image says that instead (the rule is checked again once it moves on)."""
    state = "queued" if ready and image else "waiting_creative" if ready else "waiting_account"
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%(b)s, %(ch)s, %(p)s, %(s)s) "
        "ON CONFLICT (business_id, channel) DO UPDATE SET "
        "plan_id = CASE WHEN channel_campaign.resources ? 'campaign' THEN channel_campaign.plan_id "
        "ELSE EXCLUDED.plan_id END, "
        f"state = CASE WHEN channel_campaign.state = 'paused' AND NOT {_ONLY_DROPPED} THEN 'paused' "
        "WHEN NOT (channel_campaign.resources ? 'campaign') THEN EXCLUDED.state "
        "WHEN channel_campaign.state IN ('failed', 'paused') THEN 'launching' ELSE channel_campaign.state END, "
        f"paused_at = CASE WHEN {_ONLY_DROPPED} THEN NULL ELSE channel_campaign.paused_at END, "
        "paused_reason = CASE WHEN channel_campaign.paused_reason = 'dropped' THEN NULL "
        "ELSE channel_campaign.paused_reason END, "
        "validated_at = CASE WHEN channel_campaign.resources ? 'campaign' THEN channel_campaign.validated_at END, "
        "last_error = NULL, "
        f"resources = CASE WHEN {_ONLY_DROPPED} "
        "THEN channel_campaign.resources - 'paused_by' - 'pause_owed' ELSE channel_campaign.resources END "
        "- 'refused_plan_id', updated_at = now()",
        {"b": biz, "ch": channel, "p": plan_id, "s": state},
    )
    if reason is not None:
        conn.execute(
            "UPDATE channel_campaign SET last_error = %s WHERE business_id = %s AND channel = %s "
            "AND state = 'waiting_account'",
            (reason, biz, channel),
        )
    if parked is not None:
        conn.execute(
            "UPDATE channel_campaign SET last_error = %s WHERE business_id = %s AND channel = %s "
            "AND state IN ('queued', 'launching', 'live', 'limited')",
            (parked, biz, channel),
        )


def remember(conn: psycopg.Connection, biz: str, channel: str, values: dict) -> None:
    """An adapter's own bookkeeping merged into its row's `resources` in the middle of a step (a create about to be
    sent, what the platform answered about a place, the account's time zone) -- or spend-guard's `month_cap_at`."""
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s, updated_at = now() "
        "WHERE business_id = %s AND channel = %s",
        (Jsonb(values), biz, channel),
    )


def forget(conn: psycopg.Connection, biz: str, channel: str, key: str) -> None:
    """One of an adapter's own `resources` keys dropped from its row."""
    conn.execute(
        "UPDATE channel_campaign SET resources = resources - %s, updated_at = now() "
        "WHERE business_id = %s AND channel = %s",
        (key, biz, channel),
    )


def follows_page(channel: str) -> bool:
    """Whether `channel`'s ads carry the business's page address and move with it (`Capabilities.follows_page`, M0
    Task 9 D4): every page path -- flagging a move, moving, refusing an address, a page's pause, page-check -- is for
    these channels only."""
    return registry.known(channel) and registry.adapter(channel).capabilities().follows_page


def _following() -> list[str]:
    return [c for c in registry.CHANNELS if follows_page(c)]


def page_rows(conn: psycopg.Connection, biz: str) -> list[dict]:
    """The business's rows with ads built on a channel that follows the page (`follows_page`), in card order: the
    ones a move of the page, or the page going down, concerns."""
    return [r for r in rows(conn, biz) if follows_page(r["channel"]) and _built(r)]


def on_host(conn: psycopg.Connection, biz: str, hostname: str | None) -> list[dict]:
    """`page_rows` whose ads carry an address on `hostname` (the adapter's `carried_url`): the ones the page there
    going down leaves without a page (unpublish_page, M0 Task 9 D5)."""
    if not hostname:
        return []
    out = []
    for row in page_rows(conn, biz):
        carried = registry.adapter(row["channel"]).carried_url(row["resources"])
        if carried and urlsplit(carried).hostname == hostname:
            out.append(row)
    return out


def flag_moved_page(conn: psycopg.Connection, biz: str, old_url: str, new_url: str) -> bool:
    """A3p-2 H9/H26/H27: the business's page moved from `old_url` to `new_url` -- every channel row with ads built
    whose ads follow the page (`follows_page`) gets `resources.url_stale`, whatever its state or switch (a paused or
    switched-off row's ads still catch up once they run again), so the lifecycle's next step and `launch_now` pick
    the move up. Shared by every writer that can move the page's address (`proposals.apply_publish_page`,
    `tools_pages.set_landing_page`), inside its own transaction and without the step lock (the flag is the
    lifecycle's, kept through every write of a step, `_KEEP`). True when a row was flagged; nothing (False) for a
    business with nothing built on such a channel, or an address that did not change."""
    if old_url == new_url:
        return False
    flagged = conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s, updated_at = now() "
        "WHERE business_id = %s AND channel = ANY(%s) AND resources ? 'campaign' RETURNING channel",
        (Jsonb({"url_stale": True}), biz, _following()),
    ).fetchall()
    return bool(flagged)


def retry_refused_address(conn: psycopg.Connection, biz: str, url: str) -> list[str]:
    """A3p-2 H29 ("fix the page" is the retry): a saved check that passes on the page at `url` -- check_landing_page,
    set_landing_page's own, publish_page's after-commit one -- while `url` is the address a platform refused for the
    ads (`resources.refused_url`, on any channel that follows the page) clears that refusal and queues the move
    (`url_stale`), so the next step sends the address again (launch_now, the hourly sweep, a resume). One
    conditional UPDATE, so a refusal recorded meanwhile for another address is never touched. The channels it did
    this for, in card order (empty -- false -- when nothing was written)."""
    retried = conn.execute(
        "UPDATE channel_campaign SET resources = (resources - 'refused_url') || %s, updated_at = now() "
        "WHERE business_id = %s AND channel = ANY(%s) AND resources->>'refused_url' = %s RETURNING channel",
        (Jsonb({"url_stale": True}), biz, _following(), url),
    ).fetchall()
    found = {r["channel"] for r in retried}
    return [c for c in registry.ORDER if c in found]


def mark_page_down(conn: psycopg.Connection, biz: str) -> bool:
    """A3p-2 Task 4 review: Admiry's pause for a page that stopped answering (page-check's `unreachable`) becomes its
    `page_down` pause on every channel -- the page is gone now (`proposals.apply_unpublish_page`, inside its own
    transaction), page-check never fetches an unpublished page, and the status line must not promise the ads come
    back once it opens again. Any other pause is left exactly as it is (an owner's mark beside it stands). True when
    it changed a row."""
    changed = conn.execute(
        "UPDATE channel_campaign SET paused_reason = 'page_down', updated_at = now() "
        "WHERE business_id = %s AND state = 'paused' AND paused_reason = 'unreachable' RETURNING channel",
        (biz,),
    ).fetchall()
    return bool(changed)


def page_pause(row: dict | None) -> str | None:
    """Admiry's pause of the row when it is one for the page (PAGE_REASONS) -- what publishing a page, or pointing
    the ads at another, may lift (M0 Task 9 D2); None for any other row, a pause for money above all."""
    reason = (row or {}).get("paused_reason")
    return reason if (row or {}).get("state") == "paused" and reason in PAGE_REASONS else None


def approved(conn: psycopg.Connection, biz: str) -> dict | None:
    """The business's approved plan (approving one supersedes the one before: there is at most one)."""
    r = conn.execute(
        "SELECT id FROM plan WHERE business_id = %s AND status = 'approved' ORDER BY version DESC LIMIT 1", (biz,)
    ).fetchone()
    return plans.get(conn, biz, r["id"]) if r else None


def _knowledge(conn: psycopg.Connection, biz: str) -> dict:
    latest = business.latest_knowledge(conn, biz)
    return latest[1] if latest else {}


def _set_error(conn: psycopg.Connection, biz: str, channel: str, sentence: str) -> None:
    conn.execute(
        "UPDATE channel_campaign SET last_error = %s, updated_at = now() WHERE business_id = %s AND channel = %s",
        (sentence, biz, channel),
    )


def _built(row: dict) -> bool:
    """Something is built on the platform: an adapter names its top object `campaign` in `resources`."""
    return "campaign" in (row.get("resources") or {})


def _entries(plan: dict | None) -> list[dict]:
    """The plan's funded channel entries (`sections.ads.channels[]` with an amount), in the plan's own order."""
    channels = (((plan or {}).get("sections") or {}).get("ads") or {}).get("channels") or []
    return [c for c in channels if c.get("channel") and c.get("monthly_usd")]


def funded_channels(plan: dict | None) -> list[str]:
    """The channels the plan version puts money on, in its own order."""
    return [c["channel"] for c in _entries(plan)]


def entry(plan: dict | None, channel: str) -> dict | None:
    return next((c for c in _entries(plan) if c["channel"] == channel), None)


def month_amount(plan: dict | None, channel: str) -> float | None:
    """The channel's monthly amount in `plan` -- the approved one (`approved`): the one source of spend-guard's month
    cap and of the channels card's `month_cap_usd` (final review I3). None when the plan puts nothing on it."""
    found = entry(plan, channel)
    return float(found["monthly_usd"]) if found else None


def has_point(knowledge: dict) -> bool:
    """The business's listing has a point on the map (a radius, or the ZIP codes of one, can centre on it)."""
    listing = knowledge.get("listing") or {}
    return listing.get("lat") is not None and listing.get("lng") is not None


def image_ready(plan: dict | None, channel: str) -> bool:
    """D89: a channel that needs an image has one approved on the plan (`sections.creative.approved_image`, keyed by
    channel; M1 writes it at approval) that Admiry's set still has (`plans.in_set`: Admiry never starts a channel with
    a picture the owner did not approve, so one that left the set waits for a new approval -- M1 Task 7 fix round 1);
    every other channel is ready as it is."""
    if not registry.adapter(channel).capabilities().needs_image:
        return True
    images = (((plan or {}).get("sections") or {}).get("creative") or {}).get("approved_image")
    return isinstance(images, dict) and plans.in_set(images.get(channel))


def has_ads(conn: psycopg.Connection, biz: str, channel: str) -> bool:
    """Ads are built on the platform for the business's row on `channel` (`_built`): `image_ready`'s rule is for ads
    not built yet -- ads already running keep the picture they carry (final review I3)."""
    row = get(conn, biz, channel)
    return row is not None and _built(row)


def _account(conn: psycopg.Connection, biz: str, channel: str) -> Account:
    """The business's account on `channel`: its `channel_account` row, or -- none yet -- the one it would have,
    status `none` in the channel's own mode (the only one it has, else the owner's own account, D72). An adapter
    reads its own source of truth from there (Google Search: the link)."""
    found = registry.account(conn, biz, channel)
    if found is not None:
        return found
    modes = registry.adapter(channel).capabilities().modes
    mode = next(iter(modes)) if len(modes) == 1 else "own_account"
    return Account(business_id=biz, channel=channel, mode=mode, status="none", external={}, secret=None)


def account_status(conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str) -> AccountStatus:
    """Whether the business's account on `channel` can carry ads now (spec §5.3 step 2), database only: the adapter's
    `check_account`, then the lifecycle's own rules -- a mode the channel runs (D72), and a balance above zero for a
    managed one (M1m moves this to `available`). Not ready: `reason` is the card's sentence."""
    adapter = registry.adapter(channel)
    account = _account(conn, biz, channel)
    status = adapter.check_account(conn, settings, account)
    label = registry.label(channel)
    if not status.ready:
        return status if status.reason else AccountStatus(False, status.status, NOT_READY.format(label=label), None)
    if account.mode not in adapter.capabilities().modes:
        return AccountStatus(False, status.status, MODE_SOON.format(label=label), status.account_public)
    if account.mode == "managed" and wallet.balance_cents(conn, biz) <= 0:
        return AccountStatus(False, status.status, NO_FUNDS.format(label=label), status.account_public)
    return status


# ---------------------------------------------------------------- owner sentences


def labels(channels: list[str]) -> str:
    """The channels' owner names in one phrase ("Google Search", "Google Search and NewsBreak", "A, B and C")."""
    names = [registry.label(c) for c in channels]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _paused_by_admiry(paused: list[dict]) -> str:
    """PAUSED_BY_ADMIRY for an owner's resume of Admiry's pause -- and, when the pauses it names share what ends them
    (a page's pause: PAUSE_ADVICE, M0 Task 9 D3, which never lifts by itself), that too."""
    advice = {PAUSE_ADVICE[r["paused_reason"]] for r in paused if r.get("paused_reason") in PAUSE_ADVICE}
    return _say(PAUSED_BY_ADMIRY, *advice) if len(advice) == 1 else PAUSED_BY_ADMIRY


def _say(*parts: str | None) -> str:
    """Owner sentences joined into one line, each ending in exactly one full stop."""
    return " ".join(p.rstrip(". ") + "." for p in parts if p)


def _when(ts: datetime | None) -> str:
    if ts is None:
        return "earlier"
    days = (datetime.now(UTC).date() - ts.astimezone(UTC).date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"on {ts:%A}" if days < 7 else f"on {ts:%B} {ts.day}"


def _since_word(ts: datetime | None) -> str:
    """`_when` without its leading "on " -- "Live since Tuesday" (design doc §6), never "Live since on Tuesday"."""
    return _when(ts).removeprefix("on ")


def _checked(row: dict) -> bool:
    """A3's `validated` (plan P4): a `queued` row checked under the `validate` switch (validated_at set)."""
    return row["state"] == "queued" and row.get("validated_at") is not None


def _words(channel: str | None) -> Words:
    return registry.adapter(channel).words if channel and registry.known(channel) else _PLATFORM


def _no_funds(row: dict) -> bool:
    """A managed channel waiting for its balance (spec §7.2: "Waiting for funds")."""
    return row.get("last_error") == NO_FUNDS.format(label=registry.label(row.get("channel") or ""))


def owner_paused(row: dict) -> bool:
    """The owner's pause stands: their mark (`resources.owner_paused`) -- or, on a paused row with neither mark (A3's
    pauses, before the marks), a pause that `paused_by` does not say was Admiry's."""
    return bool((row.get("resources") or {}).get("owner_paused")) or _a3_owner_pause(row)


def _a3_owner_pause(row: dict) -> bool:
    """A pause of A3's, before the marks: a paused row with neither the owner's mark nor a reason of Admiry's, which
    `paused_by` does not say was Admiry's -- every A3 pause was the owner's."""
    resources = row.get("resources") or {}
    return (
        row.get("state") == "paused"
        and not resources.get("owner_paused")
        and not row.get("paused_reason")
        and resources.get("paused_by") in (None, OWNER, "assistant")
    )


def _moved(row: dict, account: Account | None) -> bool:
    """The channel's account now names another platform account than the one the ads were built in: a key both the
    account's `external` ids and the row's `resources` carry (Google Search: `customer_id`), with different values."""
    resources = row.get("resources") or {}
    return account is not None and any(k in resources and resources[k] != v for k, v in account.external.items())


def _execution_clause(row: dict) -> str:
    state, err = row["state"], row.get("last_error")
    words = _words(row.get("channel"))
    if state == "paused":
        return "Paused"
    # the platform switched off ads Admiry runs: its own badge, never "Live since" (M1 Task 7 fix round 1)
    if state == "limited" and err and err == words.paused_there and words.paused_badge:
        return words.paused_badge
    if state in ("live", "limited"):
        return f"Live since {_since_word(row['launched_at'])}"
    if state == "failed":
        return f"Not running: {err}" if err else "Not running"
    if state == "queued" and err:  # the platform's own wait for the account is its whole badge, said once
        return err if err == words.account_wait else f"Waiting to launch: {err}"
    if _checked(row):
        return words.checked_badge
    if state == "queued":
        return "Turns on within the hour"
    if state == "waiting_account":
        if _no_funds(row):
            return "Waiting for funds"
        # the platform's own wait for the account, said once -- only when that is the row's wait (final review Minor 7)
        return err if err and err == words.account_wait else "Waiting for your account"
    if state == "waiting_creative":
        return words.image_wait or "Waiting for your image"
    if state == "operator":
        return "Run by Admiry from the platform's own report"
    if state == "launching" and (row.get("resources") or {}).get("stop_owed"):
        return SWITCHING_OFF  # a refused row whose stop is owed: its ads are going off, never launching (W5)
    if state == "launching" and (row.get("resources") or {}).get("demo_paused"):
        return DEMO_BADGE  # N4: built paused under `live_paused`, never turned on
    return "Launching"


def execution_line(rows: list[dict]) -> str:
    """The plan card's and `/plan/{id}`'s compact ads-execution badge (design doc §6: "Live since Tuesday",
    "Launching", "Paused") -- distinct from `status_sentence`'s fuller owner sentence, and never DB/HTTP: callers
    pass the rows they already have (`rows`). One clause per row, joined by "; ": a single row reads exactly as A3's
    badge did, several name their channel ("Google Search: Live since Tuesday; NewsBreak: Waiting for funds"). A
    `launching` row is its enable step's to finish (P7): its platform status is normally paused by design until then,
    so this never calls that "Paused" -- it reads as still building, unless it is a refused row whose stop is owed
    (`stop_owed`), which reads SWITCHING_OFF as its owner line says Admiry is switching the ads off (M1 follow-up 2,
    W5). The rest say where they really are (A3 final review): a launch that failed and why, a plan only checked, one
    waiting on something (its sentence), one about to launch, and no row at all -- approved while the switch was
    `off`, which never launches by itself (ruling S18)."""
    if not rows:
        return NOT_LAUNCHED_OFF
    if len(rows) == 1:
        return _execution_clause(rows[0])
    return "; ".join(f"{registry.label(r['channel'])}: {_execution_clause(r)}" for r in rows)


def state_clause(row: dict, channel: str | None = None) -> str:
    """Where a row stands, in one clause of the owner's words without the channel's name ("live", "waiting for
    funds", "paused by you on Tuesday", "paused by Admiry today: this month's budget is spent") -- the head of
    `status_sentence`, and the channels card's sentence as it is (agent/state.py). `channel` names the platform
    when the row does not. A channel Admiry runs speaks for itself where M0's clause would be wrong (M1 Task 7): its
    account waiting at the platform (`Words.account_wait` -- also the whole clause of a `queued` row whose launch found
    it waiting, said once: fix round 1), its ads the platform shows switched off while Admiry runs them
    (`Words.paused_there`, the read's note, as the whole clause: never "live" beside it), and a row waiting for a
    picture Admiry owes it (`Words.image_wait`, fix round 1). A refused row whose stop is owed (`stop_owed`, the M1
    follow-up's fix round 2, G3) says the refusal and that Admiry is switching the ads off (STOPPING_ADS), never that
    they turn on."""
    words = _words(row.get("channel") or channel)
    state, err = row["state"], row.get("last_error")
    if state == "queued" and err:  # a checked plan launching under `live` (ruling S9) waits too
        return err if err == words.account_wait else "your ads are waiting to launch"
    if state == "queued" and not _checked(row):
        return "your ads are being set up"
    owed = (row.get("resources") or {}).get("stop_owed")
    if state == "launching" and owed:
        label = registry.label(row.get("channel") or channel or "")
        return f"{owed['sentence'].rstrip('. ')}; {STOPPING_ADS.format(label=label)}"
    if state == "launching" and (row.get("resources") or {}).get("demo_paused"):
        return DEMO_CLAUSE  # N4: built paused under `live_paused`, and nothing turns them on
    if state == "launching":
        return "your ads are built and turn on within the hour"
    if state == "queued":  # A3's `validated` (plan P4)
        return words.checked_status
    if state == "live":
        return "live"
    if state == "paused":  # who paused, both when both pauses stand, and Admiry's reason in the owner's words
        owner, why, when = (
            owner_paused(row),
            PAUSE_WORDS.get(row.get("paused_reason") or ""),
            _when(row.get("paused_at")),
        )
        if why:
            return f"paused by {'you and by ' if owner else ''}Admiry {when}: {why}"
        return f"paused by {'you' if owner else 'Admiry'} {when}"
    if state == "limited":
        return err if err and err == words.paused_there else words.held_back
    if state == "waiting_account":  # never launched until the account is ready (D77)
        if _no_funds(row):
            return "waiting for funds"
        # the platform's own wait is the clause only when it is the row's wait, as `status_sentence` reads it (final
        # review Minor 7): another reason -- a mode the channel does not run -- reads as M0's, said after it
        return err if err and err == words.account_wait else "waiting for your account"
    if state == "waiting_creative":
        return words.image_wait or "waiting for your image"
    if state == "operator":
        return "run by Admiry from the platform's own report"
    return "your ads could not be launched"


def status_sentence(
    row: dict | None, today: dict | None = None, account: Account | None = None, channel: str | None = None
) -> str:
    """The owner line for where the business's ads are on one channel (the approval tools' `say`, get_channel_status,
    /connect): the channel's owner name (D76; `channel` names it when there is no row) and the row's `state_clause`,
    then what else the owner needs to know. `today` ({"spent_usd", "budget_usd"}, from ads-sync) adds today's spend
    to a live line. While a plan version the platform refused is marked (`resources.refused_plan_id`), the line says
    so and what to do (ruling S15), whatever `last_error` holds by then -- and so does a page address the platform
    refused for the ads (`resources.refused_url`, A3p-2 H28), on every line of ads that are built; a pause of
    Admiry's for the page says what ends it (PAUSE_ADVICE). With the channel's `account` at hand, a re-link to another
    account than the one the ads were built in is said too (nothing moves them by itself)."""
    ch = (row or {}).get("channel") or channel
    label = registry.label(ch) if ch else "Your ads"
    if row is None:
        return _say(f"{label}: no ads yet")
    words = _words(ch)
    state, err = row["state"], row.get("last_error")
    resources = row.get("resources") or {}
    refused = words.refused_change if resources.get("refused_plan_id") else None
    address = _refused_address(words, resources.get("refused_url"))
    moved = MOVED if _moved(row, account) else None
    head = f"{label}: {state_clause(row, ch)}"
    waited = err is not None and err == words.account_wait  # the platform's wait for the account: the clause itself
    if state == "queued":
        return _say(head, None if waited else err, moved)
    if state == "launching":
        return _say(head, address, err, moved)
    if state == "live":
        if today:
            head += f" · {plans.usd(today['spent_usd'])} of {plans.usd(today['budget_usd'])} today"
        # an update still on its way: its money may be in already (S16) -- unless a refusal says what happened
        # instead; a refused address does only while no retry is owed (a newer version's own, say: the retry words);
        # and never the platform's month Admiry could not set, which is no change of the owner's (M1 Task 7)
        if err and err != words.month_note and not (refused or (address and err != words.retrying)):
            head += ", but your latest change is not all in yet"
        return _say(head, refused, address, err, moved)
    if state == "paused":
        advice = PAUSE_ADVICE.get(row.get("paused_reason") or "")
        owed = words.pause_owed if resources.get("pause_owed") else None  # ruling S11: whatever `last_error` holds
        return _say(head, advice, owed or err, refused, address, moved)
    if state == "limited":  # the platform's own pause is the clause itself (M1 Task 7): said once
        return _say(head, refused, address, None if err and err == words.paused_there else err, moved)
    if state == "waiting_account":  # a missing balance, or the platform's own wait, is said by the clause itself
        return _say(head) if _no_funds(row) or waited else _say(head, err)
    if state in ("waiting_creative", "operator"):
        return _say(head, err)
    return _say(head, err, moved)


def _refused_address(words: Words, url: str | None) -> str | None:
    """The channel's words for `resources.refused_url`, naming the page by its host (A3p-2 H28); None when nothing is
    marked (or the channel has no such words)."""
    if not url or not words.refused_address:
        return None
    return words.refused_address.format(host=urlsplit(url).hostname or url)


# ---------------------------------------------------------------- the account's calendar (ruling S14)


def now() -> datetime:
    """The clock every account date is read from (tests set it)."""
    return datetime.now(UTC)


def zone(name: object) -> tzinfo:
    """The account's time zone by its IANA name (`resources.time_zone`); UTC while it is unknown."""
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("ads: unknown account time zone %r; dates read as UTC", name)
    return UTC


def _row_zone(row: dict | None) -> tzinfo:
    return zone(((row or {}).get("resources") or {}).get("time_zone"))


def account_zone(conn: psycopg.Connection, biz: str, channel: str) -> tzinfo:
    """The time zone of the business's account on `channel` (`resources.time_zone` on its row, kept by the adapter the
    first time a step reads it): the calendar the platform keys its days by, and so `metric_daily.day`'s -- a lead is
    counted on the day it was created in this zone. UTC while it is unknown (no row, or never read yet)."""
    return _row_zone(get(conn, biz, channel))


def account_today(row: dict | None) -> date:
    """Today in the account's own time zone (`resources.time_zone`): the calendar a platform keys its days by, and so
    `metric_daily.day` -- the sync's days end on it, and it is every reader's "today" (`today_spend`,
    `week_totals`, `day_rows`, `statement`). UTC while the zone is unknown (no row, or never read yet)."""
    return now().astimezone(_row_zone(row)).date()


@contextmanager
def _locked(conn: psycopg.Connection, biz: str) -> Iterator[bool]:
    """One lifecycle step per business at a time, across processes (a session advisory lock; never waits)."""
    key = f"ads:{biz}"
    got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (key,)).fetchone()["ok"]
    try:
        yield got
    finally:
        if got:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))


# ---------------------------------------------------------------- the precheck (spec §5.3 step 2; A3 P3, P4, S8)


def _first_fix(check: dict, rules: tuple[str, ...] = ()) -> str:
    """The page check's first fix: of a failed rule, or -- `rules` given -- of the first of those not passed."""
    for rule in check.get("rules") or []:
        failed = rule.get("id") in rules and rule.get("ok") is not True if rules else rule.get("ok") is False
        if failed and rule.get("fix"):
            return rule["fix"]
    return "open it and check it again"


def page_fix(check: dict, rules: tuple[str, ...] = ()) -> str | None:
    """The fix a saved page report `check` asks for before a channel that needs `rules` too (its `page_rules`) may
    launch -- PAGE_FIX with the first failed rule's fix when the report's own verdict fails, else with the first of
    `rules` it did not pass outright (`landing.passed`) -- or None when it lets the channel launch: `_page_ready`'s
    sentence on the channel's row, and the setup card's line for the channel (M1 follow-up 2, W4)."""
    if not check.get("ok"):
        return PAGE_FIX.format(fix=_first_fix(check))
    if not landing.passed(check, rules):
        return PAGE_FIX.format(fix=_first_fix(check, rules))
    return None


def _check_transport() -> httpx.BaseTransport | None:
    """The transport a page check run from here fetches the page through (`_page_ready`'s re-check of a stale report):
    the real network (None). A test sets a MockTransport here, as tools_hosting's own `_check_transport`."""
    return None


def _page_ready(conn: psycopg.Connection, biz: str, channel: str, *, fetch: bool = True) -> None:
    """P3, a launch rule: the business's page is published and its last check passed -- for the channel's own
    `page_rules` too, which must have passed outright (`landing.passed`) -- else the check's first fix.

    M1 Task 5b (R18): a saved report that passed but has no word of a rule the channel needs and the check has (one
    saved before M1 Task 4 added `terms` and `contact`) is stale for that channel: the page is checked again first
    (`landing.check`, the 14 rules) and the fresh report saved -- so no later step fetches it again -- with what every
    other check's save does after a passing check (A3p-2 H29: `retry_refused_address`), and judged. A check Admiry
    cannot run itself (no page kit, an address the check refuses) leaves the page unchecked for the channel
    (PAGE_UNCHECKED: the owner's check saves a fresh report; the log line keeps the check's own sentence) -- and for a
    channel that is not the primary, so does anything else that breaks it (logged by its kind alone): its re-check
    never refuses an approval (D77). The check runs in a savepoint of its own (M1 Task 5b fix round 2), so a failure
    inside it -- a database error too -- leaves the caller's transaction usable. M0's "a rule the report does not have
    blocks nothing" stays for a rule the check has no rule for (`landing.passed`).

    `fetch` False -- the approval's (final review Minor 1, revising R18): no page is fetched inside the approval's
    transaction, which holds the proposal row; a channel other than the primary with a stale report is CHECKING_PAGE
    instead -- its row parks, and its own step, outside any approval, checks the page again. The primary is
    unchanged."""
    page = landing.current(conn, biz)
    if page is None or page["status"] != "published" or not page["url"]:
        raise ToolError(NO_PAGE)
    # A3p-2 H18: page-check's own counters alone are no check; Message-match judged on the plan of now (2026-10-08)
    check = landing.judged_report(conn, page)
    label = registry.label(channel)
    if check is None:
        raise ToolError(PAGE_UNCHECKED.format(label=label))
    rules = registry.adapter(channel).capabilities().page_rules
    known = {c["id"] for c in landing.CHECKLIST}
    ran = {r.get("id") for r in check.get("rules") or []}
    if check.get("ok") and any(rule in known and rule not in ran for rule in rules):
        if not fetch and channel != allocate.PRIMARY:
            raise ToolError(CHECKING_PAGE.format(label=label))
        try:
            # a savepoint of its own: whatever breaks in the check -- a database error too -- is rolled back to it,
            # and the caller's transaction (a step's, or for the primary the approval's) goes on usable
            with conn.transaction():
                check = landing.check(
                    page["url"],
                    page,
                    landing.kit(conn, biz),
                    conn=conn,
                    transport=_check_transport(),
                    resolver=crawl.resolve,
                    extra_rules=rules,
                )
        except ToolError as e:  # a check Admiry cannot run itself: its sentence is Admiry's own words, no secret
            log.info("ads: business %s's page could not be checked again for %s: %s", biz, label, e)
            raise ToolError(PAGE_UNCHECKED.format(label=label)) from None
        except Exception as e:
            # a secondary channel's re-check never refuses the approval (D77), logged by the failure's kind alone (its
            # words may be anything's); the primary's own trouble is raised, as ever
            if channel == allocate.PRIMARY:
                raise
            log.info("ads: business %s's page could not be checked again for %s (%s)", biz, label, type(e).__name__)
            raise ToolError(PAGE_UNCHECKED.format(label=label)) from None
        landing.save_check(conn, page["id"], check)
        log.info("ads: business %s's page report had no %s rule; checked again", biz, label)
        if check.get("ok"):  # H29, as every save of a passing check: an address a platform refused is offered again
            retry_refused_address(conn, biz, page["url"])
    fix = page_fix(check, rules)
    if fix is not None:
        raise ToolError(fix)


# ---------------------------------------------------------------- the launch's rules as lists (L1, spec 2026-10-10 §4.1)
# `precheck` and `_launch_rules` refuse with the first of each; the checklist (agent/launch_check.py) reads every one.


def _startable(channel: str) -> bool:
    """A place Admiry can start ads on itself: an adapter whose `create` is true."""
    return registry.known(channel) and registry.adapter(channel).capabilities().create


def plan_refusals(conn: psycopg.Connection, biz: str, plan: dict, *, channels: list[str] | None = None) -> list[str]:
    """`precheck`'s rules on the plan as a whole, every one it fails, in the order `precheck` asks them: each place Admiry
    can start (REPORT_ONLY), each amount at least the floor of the way its place runs for this business (UNDER_FLOOR;
    A3c-1: Google prepaid from $200), the whole within the monthly limit (OVER_LIMIT). `channels` narrows the places,
    as `precheck`'s own."""
    entries = _entries(plan)
    checked = [e for e in entries if channels is None or e["channel"] in channels]
    out = [REPORT_ONLY.format(label=registry.label(e["channel"])) for e in checked if not _startable(e["channel"])]
    for e in checked:
        if not _startable(e["channel"]):
            continue
        floor = registry.adapter(e["channel"]).capabilities().floor(_account(conn, biz, e["channel"]).mode)
        if float(e["monthly_usd"]) < floor:
            out.append(UNDER_FLOOR.format(label=registry.label(e["channel"]), floor=plans.usd(floor)))
    goal = business.latest_goal(conn, biz) or {}
    ads = (plan.get("sections") or {}).get("ads") or {}
    limits = [x for x in (goal.get("monthly_limit_usd"), ads.get("monthly_limit_usd")) if x]
    if limits and sum(float(e["monthly_usd"]) for e in entries) > min(float(x) for x in limits):
        out.append(OVER_LIMIT)
    return out


def place_and_phone(plan: dict, channel: str, knowledge: dict) -> list[str]:
    """`_launch_rules`' own rules after the page, every one the plan fails: a location the place's `geo` can express
    (zip: a point, then a radius; city or metro: area names; radius is the adapter's own rule) and a phone when the plan
    counts calls. Database free."""
    caps = registry.adapter(channel).capabilities()
    consultation = (plan.get("sections") or {}).get("consultation") or {}
    out = []
    if caps.geo == "zip":
        if not has_point(knowledge):
            out.append(NO_LOCATION)
        elif not (consultation.get("geo") or {}).get("radius_miles"):
            out.append(NO_RADIUS)
    elif caps.geo in ("city", "metro") and not any(
        isinstance(n, str) and n.strip() for n in knowledge.get("service_area") or []
    ):
        out.append(NO_LOCATION)
    counts_calls = any(str(c).startswith("phone_call") for c in consultation.get("conversions") or [])
    if counts_calls and len("".join(c for c in knowledge.get("phone") or "" if c.isdigit())) < 10:
        out.append(NO_PHONE)
    return out


def has_page(conn: psycopg.Connection, biz: str, channel: str) -> bool:
    """Whether `channel`'s ads have a page to send people to -- no NO_PAGE among `page_refusals`: a page published at
    an address, or, for ads already built there, a page address on file (ruling S8, as `_launch_rules`). Whether it
    passes its check is the rest of `page_refusals`. The one test of a page being there: the checklist's page item
    (launch_check.page_settled) reads it."""
    return _page_there(get(conn, biz, channel), landing.current(conn, biz))


def _page_there(row: dict | None, page: dict | None) -> bool:
    """`has_page`'s rule on the place's row and the business's page, already read (page-first review, item 3:
    `page_refusals` reads each once)."""
    if row is not None and _built(row):
        return page is not None and bool(page["url"])
    return page is not None and page["status"] == "published" and bool(page["url"])


def page_refusals(conn: psycopg.Connection, biz: str, channel: str) -> list[tuple[str, str | None]]:
    """`_page_ready` as a list, with no fetch: (the refusal, the page rule it is about) for every reason the page stops
    `channel`'s launch now -- NO_PAGE (`has_page`); PAGE_UNCHECKED for no report, or one that predates a rule the place
    needs (the owner's "Check my page now" saves a fresh one); else one PAGE_FIX per failing rule, in the order
    `page_fix` asks them (R7, fix round 1): every BLOCKING rule the report's own verdict fails outright first, then
    every rule the place needs that did not pass outright and was not already listed -- each pass in the report's own
    order, each with that rule's own fix. Ads already built need only a page address on file (ruling S8, as
    `_launch_rules`)."""
    row, page = get(conn, biz, channel), landing.current(conn, biz)  # each read once (page-first review, item 3)
    if not _page_there(row, page):
        return [(NO_PAGE, None)]
    if row is not None and _built(row):
        return []
    check = landing.judged_report(conn, page)
    label = registry.label(channel)
    if check is None:
        return [(PAGE_UNCHECKED.format(label=label), None)]
    rules = registry.adapter(channel).capabilities().page_rules
    known = {c["id"] for c in landing.CHECKLIST}
    ran = {r.get("id") for r in check.get("rules") or []}
    if check.get("ok") and any(rule in known and rule not in ran for rule in rules):
        return [(PAGE_UNCHECKED.format(label=label), None)]

    def fix(r: dict) -> tuple[str, str | None]:
        return (PAGE_FIX.format(fix=r.get("fix") or "open it and check it again"), r.get("id"))

    out = [fix(r) for r in check.get("rules") or [] if r.get("id") in landing.BLOCKING and r.get("ok") is False]
    listed = {rule_id for _, rule_id in out}
    out += [
        fix(r)
        for r in check.get("rules") or []
        if r.get("id") in rules and r.get("ok") is not True and r.get("id") not in listed
    ]
    return out


def _launch_rules(
    conn: psycopg.Connection, biz: str, plan: dict, channel: str, knowledge: dict, *, fetch: bool = True
) -> None:
    """The rules for launching `channel` (ruling S8: never for a change to ads already built, which need only a page
    address on file): the page ready for it (`_page_ready`, with its `fetch`), a location its `geo` can express
    (radius is the adapter's own rule -- Google Search also places service-area names; zip: a point and a radius;
    city or metro: area names), and a phone when the plan counts calls."""
    row = get(conn, biz, channel)
    if row is not None and _built(row):
        page = landing.current(conn, biz)
        if page is None or not page["url"]:
            raise ToolError(NO_PAGE)
        return
    _page_ready(conn, biz, channel, fetch=fetch)
    found = place_and_phone(plan, channel, knowledge)
    if found:
        raise ToolError(found[0])


def _nothing_launches(reasons: dict[str, str]) -> str:
    """The refusal of a plan nothing in could launch (`precheck`, `precheck_approval`; M1 follow-up 2, W1): what each
    channel it would queue waits for (`reasons`, {channel: its own sentence}), in card order -- one sentence per
    channel, ending in its full stop (`_say`; fix round 1), "Google Search: <its reason>. NewsBreak: <its reason>.", a
    reason that already names its channel said as it is. One channel reads as it always did: its reason alone."""
    order = sorted(reasons, key=lambda c: registry.ORDER.index(c) if c in registry.ORDER else len(registry.ORDER))
    if len(order) == 1:
        return reasons[order[0]]
    labels = {c: registry.label(c) for c in order}
    return _say(*(reasons[c] if labels[c] in reasons[c] else f"{labels[c]}: {reasons[c]}" for c in order))


def precheck(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    plan: dict,
    *,
    channels: list[str] | None = None,
    park: bool = False,
    fetch: bool = True,
) -> dict[str, str]:
    """Whether this approved (or about to be approved) plan can launch: ToolError with one owner sentence if not.
    Database only (but for a stale page report's re-check in a step, `_page_ready`; `fetch` False -- the approval's --
    fetches no page: final review Minor 1) -- it runs inside the approval's transaction
    (`proposals.precheck_approve_plan`, before a code is consumed, so a doomed approval never burns the owner's code,
    for the channels whose switch is on) and again before every step of a row (`channels=[its channel]`). For the
    plan's funded channels (`channels` narrows them), in order: a channel Admiry can start (an adapter whose `create`
    is true; REPORT_ONLY otherwise), each amount at least its channel's floor, the plan's whole amount within the
    monthly limit, each adapter's own rules (`precheck`), the accounts -- D77: an account not ready waits
    (`waiting_account`) and never blocks the others, so only a plan none of whose accounts is ready is refused, with
    what each one waits for (`_nothing_launches`: its account's reason, M1 follow-up 2, W1) -- and each channel's
    launch rules (`_launch_rules`). A channel that needs an image and has none approved waits for it
    (`waiting_creative`); that is not a refusal.

    `park` -- the approval's (M1 Task 5b, R17; D77, Corrections 10): a channel never blocks the others, so a channel
    that is not the plan's primary (`allocate.PRIMARY`) and fails its own rules -- its adapter's `precheck`, its launch
    rules (the page with its `page_rules`, the geo, the phone) -- is parked instead of refusing: its sentence is
    returned under its id, {channel: sentence}, for its row (`queue`'s `parked`), and its later rules are not asked.
    The primary's own rules, and the plan's (report-only, floor, limit, the accounts), still refuse. Without `park` (a
    row's step) the channel's own rule refuses as ever: the sentence stays on its row, checked again every hour."""
    if business.row(conn, biz) is None:
        raise ToolError("no such business")
    refused = plan_refusals(conn, biz, plan, channels=channels)
    if refused:
        raise ToolError(refused[0])
    checked = [e for e in _entries(plan) if channels is None or e["channel"] in channels]
    knowledge = _knowledge(conn, biz)
    parked: dict[str, str] = {}

    def own(channel: str, rule) -> None:
        """One of `channel`'s own rules: a secondary channel's failure parks it under `park`, else it refuses."""
        if channel in parked:
            return
        try:
            rule()
        except ToolError as e:
            if not park or channel == allocate.PRIMARY:
                raise
            parked[channel] = str(e)

    for e in checked:
        own(e["channel"], lambda ch=e["channel"]: registry.adapter(ch).precheck(conn, settings, biz, plan, knowledge))
    statuses = {e["channel"]: account_status(conn, settings, biz, e["channel"]) for e in checked}
    if statuses and not any(s.ready for s in statuses.values()):
        raise ToolError(_nothing_launches({c: s.reason for c, s in statuses.items()}))
    for e in checked:
        own(e["channel"], lambda ch=e["channel"]: _launch_rules(conn, biz, plan, ch, knowledge, fetch=fetch))
    return parked


def precheck_approval(
    conn: psycopg.Connection, settings: config.Settings, biz: str, plan: dict, *, channels: list[str]
) -> dict[str, str]:
    """The approval's own precheck (`proposals.precheck_approve_plan`, `apply_approve_plan`): `precheck` with `park`
    over the channels the approval queues (`channels`, those whose switch is on) -- a secondary channel's own rule
    parks its row while another channel can launch (D77: a channel never blocks the others). When none of them is both
    ready and unparked, nothing could launch -- every one would park (the controller's ruling, M1 Task 5b fix round 1),
    or wait for its account (final review Minor 2: a channel whose account is not ready launches no sooner than a
    parked one) -- so the approval is refused, as M0 refuses a plan none of whose accounts is ready, with what each of
    them waits for (`_nothing_launches`, M1 follow-up 2, W1: never the first one's alone -- Google Search's Admiry-run
    account is no help to a prepaid business whose NewsBreak needs a page fix): each one's parked sentence, or else its
    account's. The approval fetches no page (`fetch` False, final review Minor 1): a stale page report parks a channel
    with CHECKING_PAGE, which a queued row's step keeps; refused, there is no row to check, so its reason is
    PAGE_UNCHECKED -- the owner's own check saves a fresh report. Returns the parked sentences."""
    parked = precheck(conn, settings, biz, plan, channels=channels, park=True, fetch=False)
    named = [c for c in funded_channels(plan) if c in channels]
    if not named:
        return parked
    statuses = {c: account_status(conn, settings, biz, c) for c in named}
    if not any(statuses[c].ready and c not in parked for c in named):
        reasons = {}
        for c in named:  # each one's own: its parked sentence, or else its account's
            label, reason = registry.label(c), parked.get(c) or statuses[c].reason
            reasons[c] = PAGE_UNCHECKED.format(label=label) if reason == CHECKING_PAGE.format(label=label) else reason
        raise ToolError(_nothing_launches(reasons))
    return parked


# ---------------------------------------------------------------- the steps


def _store_built(conn: psycopg.Connection, biz: str, channel: str, plan_id: str, ids: dict, build: Build) -> None:
    """P7: `resources` stored before the enable is sent -- whatever happens next, a retry finds them. The row is
    `launching` now, unless a pause landed while the create ran (ruling S17): that stays, and so does who paused.
    A move of the page queued while the create was out (`url_stale`) is kept unless the page is still at the
    address `build` was made against (A3p-2 H22, `_resources_sql`); the enable moves ads built at another first."""
    fresh = {k: v for k, v in ids.items() if k not in _OWN}
    conn.execute(
        "UPDATE channel_campaign SET state = CASE WHEN state = 'paused' THEN state ELSE 'launching' END, "
        f"plan_id = %(plan_id)s, resources = {_resources_sql(_KEEP)}, "
        "last_error = CASE WHEN state = 'paused' THEN last_error END, updated_at = now() "
        "WHERE business_id = %(b)s AND channel = %(ch)s",
        {"plan_id": plan_id, "fresh": Jsonb(fresh), "page_url": build.page_url, "b": biz, "ch": channel},
    )


def _resources_sql(keep: str) -> str:
    """A whole new `resources` (`%(fresh)s`, the adapter's keys) with the row's own `keep` restored server-side, and
    `url_stale` dropped only while the page's address at write time is still `%(page_url)s` -- the one the step built
    against (`Build.page_url`): a publish that committed while the step's request was out keeps its flag, and the
    next step sends that move (A3p-2 H9/H22). `%(page_url)s` NULL (a channel that does not follow the page) drops
    nothing."""
    return (
        f"CASE WHEN {_PAGE_URL_NOW} = %(page_url)s::text THEN (%(fresh)s::jsonb || {keep}) - 'url_stale' "
        f"ELSE %(fresh)s::jsonb || {keep} END"
    )


def _write_resources(
    conn: psycopg.Connection,
    biz: str,
    channel: str,
    ids: dict,
    build: Build,
    *,
    plan_id: str | None = None,
    keep_refused_plan_id: bool = False,
) -> None:
    """The race-safe write an update and a page move end on (A3p-2 `_write_resources`, M0 Task 9 D9): the adapter's
    new `resources`, the lifecycle's own keys restored from the row as it is at write time, never from the step's
    copy -- `url_stale` cleared only while the page is still at `build.page_url` (`_resources_sql`); `refused_url`
    dropped once the ads carry the page's address (moved there, or already there: a refusal of an address is moot
    then, H28), else kept; `refused_plan_id` dropped (a newer version supersedes it) unless `keep_refused_plan_id`
    (`_refresh_url`, which never means to touch it, H27 round 3 S9). `plan_id` advances only when given (the newer
    version taking hold); a page move leaves it as it is. `last_error` is cleared, except on a row a pause reached
    meanwhile (ruling S17)."""
    adapter = registry.adapter(channel)
    at_page = (
        follows_page(channel)
        and build.page_url is not None
        and adapter.carried_url(ids) == adapter.landing_url(build.page_url)
    )
    keep = _KEPT + (() if at_page else ("refused_url",)) + (("refused_plan_id",) if keep_refused_plan_id else ())
    conn.execute(
        "UPDATE channel_campaign SET plan_id = COALESCE(%(plan_id)s, plan_id), "
        f"resources = {_resources_sql(_keep_sql(keep))}, "
        "last_error = CASE WHEN state = 'paused' THEN last_error END, updated_at = now() "
        "WHERE business_id = %(b)s AND channel = %(ch)s",
        {
            "plan_id": plan_id,
            "fresh": Jsonb({k: v for k, v in ids.items() if k not in _OWN}),
            "page_url": build.page_url,
            "b": biz,
            "ch": channel,
        },
    )


def _validate(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, account: Account, plan: dict, http
) -> None:
    """P5 `validate`: the adapter's launch, validate-only -- the platform checks it (Google's validateOnly) or the
    adapter records what it would send (`validate = "dry"`) -- and nothing is built. The row stays `queued` with
    `validated_at` set (plan P4: A3's `validated`); what a dry run recorded joins its resources."""
    adapter = registry.adapter(channel)
    build = adapter.build(conn, settings, biz, plan, _knowledge(conn, biz), account, http=http)
    done = adapter.launch(
        conn, settings, account, build, get(conn, biz, channel)["resources"], http=http, validate_only=True
    )
    conn.execute(  # a pause that landed meanwhile stays (ruling S17)
        "UPDATE channel_campaign SET state = CASE WHEN state = 'paused' THEN state ELSE 'queued' END, plan_id = %s, "
        "validated_at = now(), last_error = CASE WHEN state = 'paused' THEN last_error END, "
        "resources = resources || %s, updated_at = now() WHERE business_id = %s AND channel = %s",
        (plan["id"], Jsonb({k: v for k, v in done.ids.items() if k not in _OWN}), biz, channel),
    )


def _create(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, account: Account, plan: dict, http
) -> None:
    """P7/P8: the adapter's launch (everything paused, or a lost create found by name and read back), stored, then
    turned on by the enable step -- except under `live_paused` (N4): stored, marked `demo_paused`, and left paused."""
    adapter = registry.adapter(channel)
    build = adapter.build(conn, settings, biz, plan, _knowledge(conn, biz), account, http=http)
    done = adapter.launch(
        conn, settings, account, build, get(conn, biz, channel)["resources"], http=http, validate_only=False
    )
    _store_built(conn, biz, channel, done.plan_id or plan["id"], done.ids, build)
    if registry.exec_mode(settings, channel) == config.LIVE_PAUSED:
        conn.execute(
            "UPDATE channel_campaign SET resources = resources || '{\"demo_paused\": true}'::jsonb, updated_at = now() "
            "WHERE business_id = %s AND channel = %s",
            (biz, channel),
        )
        return
    _enable(conn, settings, biz, channel, account, plan, http, done.seen)


def _update(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, account: Account, plan: dict, http
) -> None:
    """P10: the newer approved `plan` onto the resources the row's own plan version built (the adapter's `update`:
    for Google Search the money first, then the structure, ruling S16) -- its ads moved to the page's address too.
    `plan_id` advances once the platform took it; a refusal is the caller's to mark.

    The same plan version (A3p-2 H9/H27, `url_only`: the page moved under ads whose plan did not change) is the
    adapter's `move_page`, the money first (S16) and then the address alone: its refusal is the address's, never the
    plan's (H27 S5b) -- `_refuse_url` records it and nothing marks the version. While the page is still at an address
    the platform refused (`resources.refused_url`, H28), nothing is sent at all and only the flag that asked for the
    move goes (`_skip_refused_url`). The write after is `_write_resources` (H22: a move that committed meanwhile keeps
    its flag). Neither ever carries the ads to a page that is down (`_onto_page_down`, D1 for moves): nothing is sent,
    the ads are paused for the page instead, and the move or the version stays pending for the publish that lifts it."""
    adapter = registry.adapter(channel)
    row = get(conn, biz, channel)
    old = plans.get(conn, biz, row["plan_id"])
    build = adapter.build(conn, settings, biz, plan, _knowledge(conn, biz), account, http=http)
    if _onto_page_down(conn, settings, biz, channel, row, build, http):
        return
    if follows_page(channel) and old is not None and old["id"] == plan["id"]:
        if build.page_url == row["resources"].get("refused_url"):
            _skip_refused_url(conn, biz, channel, build.page_url)
            return
        try:
            done = adapter.move_page(conn, settings, account, row["resources"], old, build, http=http, with_money=True)
        except AddressRefused as e:
            _refuse_url(conn, settings, biz, channel, build.page_url, str(e), http)
            return
    else:
        done = adapter.update(conn, settings, account, row["resources"], old, build, http=http)
    _write_resources(conn, biz, channel, done.ids, build, plan_id=plan["id"])


def _onto_page_down(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, row: dict, build: Build, http
) -> bool:
    """D1 for moves (M0 Task 9 fix round 1): `build` would carry the ads to another address than the one they carry
    (a URL-only move, the refused version's refresh, or a newer version's update) and that address is a page that is
    down (`_page_down`: the business's hosted address with no hosted page live there -- unpublished while the move
    waited for the switch or for the platform). Nothing is sent: the ads are paused for the page (`page_down`, by
    Admiry), and the move (`url_stale`) or the newer version stays pending -- publishing the page again lifts the
    pause, and the resume sends it. The owner gets page-check's backstop e-mail (MAIL_PAGE_GONE, naming the channel)
    once, when this pause is the transition: before this guard the backstop paused those ads, and told them, an hour
    later. True when it held the change back."""
    if not follows_page(channel) or build.page_url is None:
        return False
    adapter = registry.adapter(channel)
    target = adapter.landing_url(build.page_url)
    if target == adapter.carried_url(row["resources"]) or not _page_down(conn, biz, target):
        return False
    pause(conn, settings, biz, channel, http=http, by="admiry", reason="page_down")
    after = get(conn, biz, channel) or {}
    if after.get("paused_reason") == "page_down" and row.get("paused_reason") != "page_down":
        log.warning("ads: business %s's %s move to %s held back: no page there; paused", biz, channel, build.page_url)
        email = mail.to_owner(
            conn,
            biz,
            "mail_page_gone.txt.j2",
            MAIL_PAGE_HELD,
            url=build.page_url,
            held=True,
            status=status_sentence(after),
            labels=labels([channel]),
        )
        log.info("ads: business %s told of the page-down pause on %s: %s", biz, channel, email)
    return True


def _skip_refused_url(conn: psycopg.Connection, biz: str, channel: str, page_url: str | None) -> None:
    """A3p-2 H28: the page is still at the address the platform refused (`resources.refused_url` == `page_url`), so
    a URL-only move -- `_update`'s, `_refresh_url`, `_enable`'s before it turns ads on -- sends nothing, and only the
    flag that asked for one goes: `url_stale` is dropped while the page is still at `page_url` (kept when it moved
    again meanwhile: that move is the next step's). With it goes the channel's retry sentence in `last_error` (round
    5): the retry it promised was the move this skip just made moot. Any other sentence stays; `refused_url` stays
    until the ads carry another address or a passing check retries it."""
    conn.execute(
        f"UPDATE channel_campaign SET resources = CASE WHEN {_PAGE_URL_NOW} = %(page_url)s::text "
        "THEN resources - 'url_stale' ELSE resources END, "
        f"last_error = CASE WHEN {_PAGE_URL_NOW} = %(page_url)s::text AND last_error = %(retrying)s THEN NULL "
        "ELSE last_error END, updated_at = now() WHERE business_id = %(b)s AND channel = %(ch)s "
        "AND resources ? 'url_stale'",
        {"b": biz, "ch": channel, "page_url": page_url, "retrying": _words(channel).retrying},
    )


def _refuse_url(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    channel: str,
    url: str | None,
    sentence: str,
    http: httpx.Client | None,
) -> None:
    """A3p-2 H27 (round 3, S5/S5b): the platform refused a URL-only move (`move_page`'s `AddressRefused`) -- a
    problem with `url`, not with the plan: `refused_plan_id` is never marked for it (the version it would mark is
    the one running fine). `resources.refused_url` records the address instead, and `url_stale` goes while the page
    is still at `url` (H28 Y2: a move to another address that committed meanwhile keeps its flag, and the next step
    sends that one); every URL-only path then skips it at no platform cost while the page keeps that address. The
    refusal is `last_error`.

    H29: when the page the ads keep is down (`_page_down`) -- the business's hosted address, once the owner's own
    page is the live one or the hosted page was taken down -- keeping them would send paid clicks to a page that is
    not there (spec §8): Admiry pauses them (`pause_page_down`, under the lock this step holds). A caller that was
    about to turn the ads on (`_enable`) reads the row again and finds them paused."""
    conn.execute(
        f"UPDATE channel_campaign SET resources = CASE WHEN {_PAGE_URL_NOW} = %(page_url)s::text "
        "THEN (resources || %(mark)s) - 'url_stale' ELSE resources || %(mark)s END, "
        "last_error = %(error)s, updated_at = now() WHERE business_id = %(b)s AND channel = %(ch)s",
        {"mark": Jsonb({"refused_url": url}), "error": sentence, "page_url": url, "b": biz, "ch": channel},
    )
    pause_page_down(conn, settings, biz, channel, http=http)


def _page_down(conn: psycopg.Connection, biz: str, carried: str | None) -> bool:
    """A3p-2 H29: whether the address the ads carry (`carried`, the adapter's `carried_url`) is the business's own
    hosted one while no hosted page is live there -- the owner's own page is the live one (the hosted address
    answers "not found", H25), or the hosted page was taken down ("gone"). Any other address is the owner's own
    site, which Admiry cannot see is down; an unknown one (None) is not the hosted address (no ads carried one
    before A3p-2 recorded it)."""
    host = urlsplit(carried).hostname if carried else None
    page = landing.current(conn, biz)
    if not host or not page or page.get("hostname") != host:
        return False
    return not (page["source"] in ("hosted", "admiry") and page["status"] == "published")


def pause_page_down(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    channel: str,
    *,
    http: httpx.Client | None = None,
    by: str = "admiry",
) -> bool:
    """A3p-2 H29: the ads on `channel` carry the business's hosted address while no hosted page is live there
    (`_page_down`) -- paid clicks would land on a page that is not there (spec §8) -- so Admiry pauses them
    (`pause(reason="page_down")`: sent under the `live` switch, owed under `off`/`validate`, ruling S11).
    `_refuse_url` calls it when the platform refuses the move off that address; set_landing_page
    (`tools_pages._ads_follow`) when the switch keeps the move from being sent at all; unpublish_page's after-commit
    hook and page-check's backstop (`jobs._pause_page_gone`, `by="job:page-check"`) for a page taken down. Only for
    a channel that follows the page, with ads built. Two pauses can stand (M0 Task 9 D5): beside the owner's, this
    one stacks, so the owner's resume never puts ads back on a page that is gone; a pause of Admiry's for money
    stays the row's reason (D1), and a row someone paused by hand on the platform is left alone (H33, `pause`).
    True when the ads are paused for the page now."""
    if not follows_page(channel):
        return False
    row = get(conn, biz, channel)
    if row is None or not _built(row):
        return False
    if not _page_down(conn, biz, registry.adapter(channel).carried_url(row["resources"])):
        return False
    pause(conn, settings, biz, channel, http=http, by=by, reason="page_down")
    return (get(conn, biz, channel) or {}).get("paused_reason") == "page_down"


def _refresh_url(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, account: Account, http
) -> None:
    """A3p-2 H26/S15 (round 2): the approved version is the one the platform already refused
    (`resources.refused_plan_id`) -- never sent again (ruling S15) -- but the page moved since (`url_stale`, or
    `_enable`'s own check of the address the ads carry). The row's own *running* version (`plan_id`: what is live
    on the platform) is moved to the page's address by the adapter's `move_page` without any money (unchanged, so
    none is owed): the refused content is never what is sent. `plan_id` stays; `refused_plan_id` is kept server-side
    (H27 round 3 S9: only a genuinely newer approval clears it). A refusal of the address is `_refuse_url`'s, never a
    `_fail`: whoever called this carries on with the ads as they are; while the page is still at an address already
    refused, nothing is sent (`_skip_refused_url`)."""
    adapter = registry.adapter(channel)
    row = get(conn, biz, channel)
    running = plans.get(conn, biz, row["plan_id"])
    build = adapter.build(conn, settings, biz, running, _knowledge(conn, biz), account, http=http)
    if _onto_page_down(conn, settings, biz, channel, row, build, http):
        return
    if build.page_url == row["resources"].get("refused_url"):
        _skip_refused_url(conn, biz, channel, build.page_url)
        return
    try:
        done = adapter.move_page(conn, settings, account, row["resources"], running, build, http=http, with_money=False)
    except AddressRefused as e:
        _refuse_url(conn, settings, biz, channel, build.page_url, str(e), http)
        return
    _write_resources(conn, biz, channel, done.ids, build, keep_refused_plan_id=True)


def _enable(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    channel: str,
    account: Account,
    plan: dict,
    http,
    seen: dict | None,
) -> None:
    """P7's second call, the money rule first: the approved plan is read again (a version approved while this step
    ran counts), a newer one than the version built is applied while everything is still paused, and the adapter
    sets the approved plan's budget as it turns the ads on -- so they are never enabled with a budget over the plan's.
    A version the platform already refused is not sent again (ruling S15). A version approved meanwhile that leaves
    the channel out is final too: nothing is turned on -- the row stays `launching`, and the next step pauses it
    (`dropped`). Ads never turned on before need the page to pass its check first (P3, a launch rule; ruling S8). A
    pause that landed while this step ran (ruling S17: it never waits for the lock) is final: a paused row is left off
    and never marked live (the adapter reads the row again right before it turns anything on). `seen` is the top
    object as the create step just saw it.

    The page (A3p-2 H9/H22/R5, M0 Task 9), for a channel that follows it: ads are never turned on at an old address.
    A move queued on the row (`url_stale`) -- or ads that carry another address than the page's, read again here
    whatever the flag says (a publish can commit between the create and this read, before anything was built to
    flag) -- is sent first, exactly like a newer version: `_update` (the money, then the address), or, when the
    approved version is one the platform refused, `_refresh_url` (the address alone, never the refused content,
    H26/S15). A refusal of the address is not a failure: the ads come back on at the address they have
    (`_refuse_url`), unless that page is down, which pauses them. While the page is still at an address the platform
    refused (`resources.refused_url`), nothing is sent and only a stray flag goes (H28). Last, D1's guard: ads that
    carry a page that is down (`_page_down`) are paused for it (`page_down`) instead of turned on -- a launch, any
    resume, the new month's lift of a month cap alike. Under `live_paused` (N4) it turns nothing on, whoever calls it:
    `_work` and `_create` never do, and this is the wall behind them."""
    if registry.exec_mode(settings, channel) == config.LIVE_PAUSED:
        log.warning("ads enable for business %s on %s refused: the switch is live_paused", biz, channel)
        return
    latest = approved(conn, biz)
    if latest is not None and channel not in funded_channels(latest):
        return
    plan = latest or plan
    row = get(conn, biz, channel)
    adapter = registry.adapter(channel)
    follows = follows_page(channel)
    refused = plan["id"] == row["resources"].get("refused_plan_id")
    newer = row["plan_id"] != plan["id"] and not refused
    page = landing.current(conn, biz) if follows else None
    base = page["url"] if page and page["url"] else None
    at_refused = base is not None and base == row["resources"].get("refused_url")
    moved = base is not None and adapter.carried_url(row["resources"]) != adapter.landing_url(base)
    stale = follows and (bool(row["resources"].get("url_stale")) or moved) and not at_refused
    if newer or stale:
        if refused:
            _refresh_url(conn, settings, biz, channel, account, http)
        else:
            _update(conn, settings, biz, channel, account, plan, http)
        seen = None  # the budget may have moved
        row = get(conn, biz, channel)
    elif follows and row["resources"].get("url_stale"):
        _skip_refused_url(conn, biz, channel, base)
        row = get(conn, biz, channel)
    if row["state"] == "paused":
        return
    if row["launched_at"] is None:
        _page_ready(conn, biz, channel)
    if follows and _page_down(conn, biz, adapter.carried_url(row["resources"])):  # D1: never on a page that is down
        pause(conn, settings, biz, channel, http=http, by="admiry", reason="page_down")
        return
    adapter.enable(conn, settings, account, plan, row["resources"], http=http, seen=seen)
    conn.execute(
        "UPDATE channel_campaign SET state = 'live', launched_at = COALESCE(launched_at, now()), paused_at = NULL, "
        "paused_reason = NULL, last_error = NULL, "
        "resources = resources - 'paused_by' - 'owner_paused' - 'demo_paused', "
        "updated_at = now() WHERE business_id = %s AND channel = %s AND state <> 'paused'",
        (biz, channel),
    )


def _repause(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, account: Account, http
) -> None:
    """Ruling S11: the pause the platform was not told of (`resources.pause_owed`), sent now; the flag cleared once
    the platform took it (or has nothing to pause) -- and a refused row's owed stop with it (`stop_owed`, M1 follow-up
    fix round 1): the row is paused, not failed."""
    row = get(conn, biz, channel)
    registry.adapter(channel).set_paused(conn, settings, account, row["resources"], True, http=http)
    conn.execute(
        "UPDATE channel_campaign SET resources = resources - 'pause_owed' - 'stop_owed', last_error = NULL, "
        "updated_at = now() WHERE business_id = %s AND channel = %s",
        (biz, channel),
    )


def _recheck(conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, plan: dict) -> bool:
    """D77, ads-launch every hour: a row waiting for its account moves on the hour the account is ready -- `queued`,
    or `waiting_creative` while it has no approved picture in the set (`image_ready`) -- and True; still not ready,
    the reason is brought up to date for the card and False."""
    status = account_status(conn, settings, biz, channel)
    if not status.ready:
        conn.execute(
            "UPDATE channel_campaign SET last_error = %s, updated_at = now() WHERE business_id = %s AND channel = %s "
            "AND state = 'waiting_account' AND last_error IS DISTINCT FROM %s",
            (status.reason, biz, channel, status.reason),
        )
        return False
    conn.execute(
        "UPDATE channel_campaign SET state = %s, last_error = NULL, validated_at = NULL, updated_at = now() "
        "WHERE business_id = %s AND channel = %s AND state = 'waiting_account'",
        ("queued" if image_ready(plan, channel) else "waiting_creative", biz, channel),
    )
    return True


def _await_image(conn: psycopg.Connection, biz: str, channel: str) -> None:
    """D89 (M1 Task 7 fix round 1, item 3): ads about to be built whose approved picture left Admiry's set since the
    approval wait in `waiting_creative` -- never failed (nothing of the owner's to fix) and never started with another
    picture (the owner approved this one) -- until a version showing a new one is approved; a check under `validate`
    (`validated_at`) no longer stands for what will run. Nothing is sent; the step's activity line says why
    (`_note`)."""
    conn.execute(
        "UPDATE channel_campaign SET state = 'waiting_creative', last_error = NULL, validated_at = NULL, "
        "updated_at = now() WHERE business_id = %s AND channel = %s AND state = 'queued'",
        (biz, channel),
    )


def _fail(conn: psycopg.Connection, biz: str, channel: str, sentence: str) -> None:
    """A launch the platform refused, or ads gone from the platform before they were turned on: `failed` with the
    sentence -- unless a pause landed while the step ran (ruling S17), which stays. A stop owed for it is done
    (`stop_owed` goes: called only once nothing of the row is on, or nothing was built)."""
    conn.execute(
        "UPDATE channel_campaign SET state = CASE WHEN state = 'paused' THEN state ELSE 'failed' END, "
        "last_error = %s, resources = resources - 'stop_owed', updated_at = now() "
        "WHERE business_id = %s AND channel = %s",
        (sentence, biz, channel),
    )


def _send_stop(
    conn: psycopg.Connection,
    settings: config.Settings,
    channel: str,
    account: Account,
    row: dict,
    http,
    *,
    cannot: str,
) -> str | None:
    """The channel's stop for the ads `row` built: the adapter's pause, which stopping spend always allows (no
    time-budget floor). None once it went through; else the owner sentence of why not -- the platform's, or no linked
    account to tell (ToolError, as in `pause`) -- and `cannot` for a channel that cannot pause (none builds ads
    without it: the lifecycle never calls what a capability says a channel lacks)."""
    adapter = registry.adapter(channel)
    if not adapter.capabilities().pause:
        return cannot
    try:
        adapter.set_paused(conn, settings, account, row["resources"], True, http=http)
    except (PlatformError, ToolError) as e:
        return str(e)
    return None


def _owe_stop(conn: psycopg.Connection, biz: str, channel: str, owed: dict, stop: str) -> None:
    """A refused row's stop the platform did not take, owed (fix rounds 1 and 2, F2/G2, as a pause is by ruling S11):
    `resources.stop_owed` keeps the refusal's sentence and the version it refused (`{"sentence", "plan_id"}`), and
    `last_error` the stop's own sentence; the next step, `stop` (one of STOPS: sent under every day hold), sends that
    stop alone."""
    log.warning(
        "ads %s for business %s: refused (%s), and the stop before failing the row did not go through (%s); the stop "
        "is owed and the next step sends it",
        channel,
        biz,
        owed["sentence"],
        stop,
    )
    conn.execute(
        "UPDATE channel_campaign SET last_error = %s, resources = resources || %s, updated_at = now() "
        "WHERE business_id = %s AND channel = %s",
        (stop, Jsonb({"stop_owed": owed}), biz, channel),
    )


def _stop_then_fail(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    channel: str,
    account: Account,
    sentence: str,
    plan_id: str,
    http,
) -> None:
    """`_fail` with the refusal's `sentence` -- never while anything of the row may be on at the platform (M1 follow-up
    R1, the controller's ruling): a `failed` row is read by nothing (no ads-sync, no metrics, no wallet entry, no month
    cap, no Pause on the card). Ads built (`_built`) may serve whatever the row's state says -- an enable cut short
    after an ON went through stays `launching`, and so does one whose switch the platform refused (NewsBreak's, final
    review C1, raised with no OFF of its own: fix round 2, G1) -- so the channel's stop goes first (`_send_stop`). Once
    it went through, the row fails -- or, a newer version approved while the stop was out, stays `launching` to launch
    that one (`_stopped`, M1 follow-up 2, W6). A stop that fails, retryable or not, fails nothing: the row stays as it
    is (`launching`) and the stop is owed (`_owe_stop`) with the refusal and `plan_id`, the version it refused (the
    approved one the step began with); the `stop` step (`_send_owed_stop`) sends it alone. Ads never built (a create
    refused before anything was made, a check under `validate`) have nothing on: nothing is sent, and the row decides
    at once as a stop that went through does (`_stopped`, M1 follow-up 2 fix round 1, F3) -- it fails, or, a newer
    version approved during the refused request, stays where that approval put it, for its next step to launch it."""
    row = get(conn, biz, channel)
    if row is None or not _built(row):
        _stopped(conn, biz, channel, sentence, plan_id)
        return
    stop = _send_stop(conn, settings, channel, account, row, http, cannot=sentence)
    if stop is None:
        _stopped(conn, biz, channel, sentence, plan_id)
        return
    _owe_stop(conn, biz, channel, {"sentence": sentence, "plan_id": plan_id}, stop)


def _send_owed_stop(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, account: Account, http
) -> None:
    """The `stop` step: a refused row's owed stop (`resources.stop_owed`), sent alone -- no money, no switch ON, no
    precheck, as an owed pause is. Once it went through, `_stopped` decides with the version the refusal was for. A
    stop that fails again stays owed, with its own sentence."""
    row = get(conn, biz, channel)
    owed = row["resources"]["stop_owed"]
    stop = _send_stop(conn, settings, channel, account, row, http, cannot=owed["sentence"])
    if stop is not None:
        _owe_stop(conn, biz, channel, owed, stop)
        return
    _stopped(conn, biz, channel, owed["sentence"], owed["plan_id"])


def _stopped(conn: psycopg.Connection, biz: str, channel: str, sentence: str, plan_id: str) -> None:
    """A refused row with nothing of it left on: its stop went through -- at once (`_stop_then_fail`) or owed
    (`_send_owed_stop`) -- or nothing was built to stop (fix round 1, F3). While the version the refusal was for
    (`plan_id`) is still the approved one (read now: an approval may land while the request or the stop is out), the
    row fails with the refusal's `sentence`; a newer approved version supersedes it instead (fix round 2, G2; M1
    follow-up 2, W6) -- no stop is owed and the row stays where it is, `launching` with its ads off or where the
    approval put ads never built, for its next step to launch that version the ordinary way (`enable`: the money
    first, S16). A failed row is due for nothing, so failing it would strand the newer version until yet another
    approval.

    The row is held (`FOR UPDATE`, in a transaction of its own) before the approved version is read (fix round 1, F4):
    an approval whose transaction has requeued the row (`queue`) but not committed yet is waited for, so the version it
    approves is the one read -- never a `failed` written onto the row it requeued. The wait cannot deadlock: the step
    holds no row while it waits (its business lock is a session advisory lock no approval takes), and the approval --
    its proposal, the plans, then this row -- never waits for the step."""
    with conn.transaction():
        conn.execute(
            "SELECT 1 FROM channel_campaign WHERE business_id = %s AND channel = %s FOR UPDATE", (biz, channel)
        )
        latest = approved(conn, biz)
        if latest is None or latest["id"] == plan_id:
            _fail(conn, biz, channel, sentence)
            return
        conn.execute(  # a pause that landed meanwhile keeps its sentence (ruling S17)
            "UPDATE channel_campaign SET resources = resources - 'stop_owed', "
            "last_error = CASE WHEN state = 'paused' THEN last_error END, updated_at = now() "
            "WHERE business_id = %s AND channel = %s",
            (biz, channel),
        )


def _work(settings: config.Settings, row: dict, plan: dict) -> str | None:
    """What one step does for this row: validate | create | enable | stop | update | refresh_url | repause | recheck |
    drop | nothing (None). `validate` never touches what is really built, nor checks twice (P4). A paused row does
    nothing but finish an owed pause (ruling S11). A row the approved version no longer funds is paused (`drop`, spec
    §5.3 step 4) -- unless it never ran or failed. One waiting for its account is re-checked (`recheck`); one waiting
    for an image waits for a new approval. A `launching` row whose refusal's stop the platform did not take
    (`stop_owed`, M1 follow-up fix round 1, F2) sends that stop alone (`stop`) before anything else is tried. Running
    ads whose page moved (`url_stale`, A3p-2 H9; a channel that follows the page) are moved: a URL-only `update`, or
    `refresh_url` when the approved version is one the platform refused (H26/S15: never sent again)."""
    channel, state, resources = row["channel"], row["state"], row["resources"]
    funded = channel in funded_channels(plan)
    mode = registry.exec_mode(settings, channel)
    if mode == "validate":
        if not funded:
            return None
        if state == "waiting_account":
            return "recheck"
        return "validate" if state == "queued" and row["validated_at"] is None else None
    if state == "paused":
        return "repause" if resources.get("pause_owed") else None
    if not funded:
        return (
            "drop"
            if state in ("waiting_account", "waiting_creative", "queued", "launching", "live", "limited")
            else None
        )
    if state == "waiting_account":
        return "recheck"
    if state == "queued":  # ruling S9: a plan checked (validated_at set) launches once the switch is live
        return "create"
    if state == "launching":  # N4: under `live_paused` nothing is ever turned on -- an owed stop is still sent
        if resources.get("stop_owed"):
            return "stop"
        return None if mode == config.LIVE_PAUSED else "enable"
    refused = plan["id"] == resources.get("refused_plan_id")
    if state not in ("live", "limited") or not _built(row):
        return None
    if plan["id"] != row["plan_id"] and not refused:  # a genuinely newer, untried version
        return "update"
    if resources.get("url_stale") and follows_page(channel):
        return "refresh_url" if refused else "update"
    return None


def stops_spend(conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str) -> bool:
    """Whether the next step for the business's row on `channel` stops spend (`STOPS`: a pause owed to the platform, a
    version that leaves the channel out, which pauses it, or a refused row's owed stop, M1 follow-up fix round 1) --
    what the hourly ads-launch still sends under either of the jobs' day holds, a channel's spent day budget or
    NewsBreak's two throttles in a row (M1 Task 6, T6-4 as revised in fix round 1: stopping spend is always allowed, a
    4034 may be a per-minute throttle, and a stop that fails stays owed; `jobs._stop` says the holds), as `launch_now`
    takes it first (R26). False without an approved plan or a row."""
    plan, row = approved(conn, biz), get(conn, biz, channel)
    return plan is not None and row is not None and _work(settings, row, plan) in STOPS


def due(settings: config.Settings, channel: str) -> str:
    """The rows (alias `c`) of `channel` the hourly ads-launch has a step for -- `_work`'s cases in SQL, under the
    channel's own switch. `live`: a launch not finished (`queued`, `launching` -- a refused row's owed stop included,
    M1 follow-up fix round 1), a plan checked under `validate` (a `queued` row with validated_at set, plan P4; ruling
    S9), a row waiting for its account (D77: launched the hour it is ready), running ads with a newer approved plan not
    yet applied or refused (a version that dropped the channel included), a row waiting for its image that the approved
    version leaves out (to pause it, `dropped`), and a `paused` row only while it owes the platform its pause (ruling
    S11) -- never to launch, update or turn it on -- and, for a channel that follows the page, running ads whose page
    moved (`url_stale`, A3p-2 H9: normally sent at once by `launch_now`; this is the retry). `validate`: `queued` rows
    not checked yet and rows waiting for their account (validate never touches what is really built). Never a `failed`
    row: only a refusal of the request itself fails a row, and a new approval requeues it; never a row waiting for an
    image of a channel the plan funds (a new approval brings the image). `live_paused` (N4) reads as `live` here: its
    `launching` rows are due, and `_work` answers them with an owed stop, a `drop`, or nothing -- never `enable`. The
    channel id goes into the SQL text, so only an id `registry.LABELS` names is taken (ValueError otherwise)."""
    if channel not in registry.LABELS:
        raise ValueError(f"unknown channel {channel!r}")
    mine = f"c.channel = '{channel}'"
    if registry.exec_mode(settings, channel) == "validate":
        return f"{mine} AND (c.state = 'waiting_account' OR (c.state = 'queued' AND c.validated_at IS NULL))"
    moved = "c.resources @> '{\"url_stale\": true}' OR " if follows_page(channel) else ""
    return (
        f"{mine} AND (c.state IN ('queued', 'launching', 'waiting_account') "
        "OR (c.state = 'paused' AND c.resources @> '{\"pause_owed\": true}') "
        "OR (c.state IN ('live', 'limited') "
        "AND c.resources ? 'campaign' "
        f"AND ({moved}EXISTS (SELECT 1 FROM plan p WHERE p.business_id = c.business_id AND p.status = 'approved' "
        "AND p.id <> c.plan_id AND p.id IS DISTINCT FROM c.resources->>'refused_plan_id'))) "
        "OR (c.state = 'waiting_creative' "
        "AND EXISTS (SELECT 1 FROM plan p WHERE p.business_id = c.business_id AND p.status = 'approved' "
        "AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(p.json->'sections'->'ads'->'channels', "
        "'[]'::jsonb)) e WHERE e->>'channel' = c.channel AND COALESCE((e->>'monthly_usd')::numeric, 0) > 0))))"
    )


def _advance(conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, http) -> None:
    """One step of the state machine for the business's row on `channel` (the caller holds its lock). Refusals and
    the platform's failures end up on the row (see the module docstring); a step an adapter cannot take
    (`Unsupported`) leaves the row as it is, with a log line; anything else is a bug and propagates. A channel the
    approved plan funds with no row yet (approved while its switch was off) is queued now if it can launch at all --
    parked with its sentence when, not being the primary, it fails its own rules (R17), as the approval parks it."""
    plan = approved(conn, biz)
    if plan is None:
        return
    row = get(conn, biz, channel)
    if row is None:  # approved while the switch was off: queue it now, if it can launch at all
        if channel not in funded_channels(plan):
            return
        try:
            parked = precheck(conn, settings, biz, plan, channels=[channel], park=True)
        except ToolError:
            return  # no row to keep the reason on; the approval's own precheck says the same
        queue(conn, biz, channel, plan["id"], ready=True, image=image_ready(plan, channel), parked=parked.get(channel))
        row = get(conn, biz, channel)
    work = _work(settings, row, plan)
    if work is None:
        return
    account = _account(conn, biz, channel)
    try:
        if work == "repause":  # stopping spend is always allowed: no precheck
            _repause(conn, settings, biz, channel, account, http)
            return
        if work == "stop":  # a refused row's owed stop (fix rounds 1-2): sent alone, no precheck, as an owed pause
            _send_owed_stop(conn, settings, biz, channel, account, http)
            return
        if work == "drop":
            pause(conn, settings, biz, channel, http=http, by="admiry", reason="dropped")
            return
        if work == "recheck":
            if not _recheck(conn, settings, biz, channel, plan):
                return
            work = _work(settings, get(conn, biz, channel), plan)
            if work is None:
                return
        if work in ("validate", "create") and not image_ready(plan, channel):
            _await_image(conn, biz, channel)  # the approved picture left the set since the approval: nothing is sent
            return
        precheck(conn, settings, biz, plan, channels=[channel])
        if work == "validate":
            _validate(conn, settings, biz, channel, account, plan, http)
        elif work == "create":
            _create(conn, settings, biz, channel, account, plan, http)
        elif work == "enable":
            _enable(conn, settings, biz, channel, account, plan, http, None)
        elif work == "refresh_url":
            _refresh_url(conn, settings, biz, channel, account, http)
        else:
            _update(conn, settings, biz, channel, account, plan, http)
    except ToolError as e:
        _set_error(conn, biz, channel, str(e))
    except Unsupported as e:
        log.info("ads %s for business %s: %s; the row waits as it is", channel, biz, e)
    except PlatformError as e:
        # an owed pause stays owed (and paused) whatever the platform said; a page move of running ads never fails
        # them (a refusal of the address itself is `_refuse_url`'s, inside the move)
        if e.retryable or work in ("repause", "refresh_url"):
            _set_error(conn, biz, channel, str(e))
        elif work == "update":  # a refusal of the content: the ads keep running the version they have (its money too,
            # S16); not tried again. A URL-only update's refusal of the address never reaches here (`_update`)
            conn.execute(
                "UPDATE channel_campaign SET last_error = %s, resources = resources || jsonb_build_object("
                "'refused_plan_id', %s::text), updated_at = now() WHERE business_id = %s AND channel = %s",
                (str(e), plan["id"], biz, channel),
            )
        else:  # a launch refused -- or the newer version it applies first: the row fails once nothing of it is on (R1)
            _stop_then_fail(conn, settings, biz, channel, account, str(e), plan["id"], http)


def _note(conn: psycopg.Connection, biz: str, before: dict | None, after: dict) -> str | None:
    """The audit row's owner phrase for what one step changed (tools_account.VERBS "job.ads_launch"); None when it
    changed nothing worth a row (a retryable failure only updates `last_error`). A check under `validate` (A3's move
    to `validated`) sets validated_at and leaves the state as it was (plan P4)."""
    label, words = registry.label(after["channel"]), _words(after["channel"])
    frm, to = (before or {}).get("state"), after["state"]
    if to == "queued" and after.get("validated_at") and not (before or {}).get("validated_at"):
        return words.checked
    if to != frm:
        if to == "live":  # launched before: a resume (it goes through `launching` again)
            if (before or {}).get("launched_at"):
                return f"turned your ads on {label} back on"
            return f"launched your ads on {label}"
        if to == "launching" and after["resources"].get("demo_paused"):  # N4: never turned on
            return f"built your ads on {label}, paused in Google Ads: demo mode, nothing spends"
        if to == "launching":
            return f"built your ads on {label}; they turn on within the hour"
        if to == "failed":
            return f"could not launch your ads on {label}: {after['last_error']}"
        if to == "paused" and after.get("paused_reason") == "dropped":
            return f"paused your ads on {label}: the plan you approved leaves it out"
        if to == "paused" and after.get("paused_reason") == "page_down":  # A3p-2 H29, D1: Admiry's own pause
            return f"paused your ads on {label}: the page they point at is down"
        if to == "waiting_creative":  # `_await_image` or `_recheck` (final review, a deferred minor)
            return f"put your ads on {label} on hold: the picture approved with your plan is no longer in Admiry's set"
        return None
    if before["resources"].get("pause_owed") and not after["resources"].get("pause_owed"):
        return words.repaused
    version = (plans.get(conn, biz, after["plan_id"]) or {}).get("version", "?")
    if before["plan_id"] != after["plan_id"]:
        return f"updated your ads on {label} to plan v{version}"
    refused = after["resources"].get("refused_plan_id")
    if refused and refused != before["resources"].get("refused_plan_id"):
        refused_version = (plans.get(conn, biz, refused) or {}).get("version", "?")
        return f"could not update your ads on {label} to plan v{refused_version}: {after['last_error']}"
    return None


def _record(conn: psycopg.Connection, biz: str, before: dict | None, after: dict | None, ops: int) -> None:
    if after is None:
        return
    note = _note(conn, biz, before, after)
    if note is None:
        return
    audit.record_job(
        conn,
        job="ads-launch",
        tool="job.ads_launch",
        business_id=biz,
        args={},
        result={
            "from": (before or {}).get("state"),
            "to": after["state"],
            "plan_id": after["plan_id"],
            "ops": ops,
            "note": note,
        },
    )


def _outcome(row: dict | None, *, ops: int, channel: str | None = None, account: Account | None = None) -> dict:
    return {
        "state": row["state"] if row else None,
        "say": status_sentence(row, account=account, channel=channel),
        "ops": ops,
        "plan_id": row["plan_id"] if row else None,
        "last_error": row["last_error"] if row else None,
    }


def _step(conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, http) -> dict:
    """`_advance` under the business's lock, metered (the adapter's own count of what its client sent: ruling S4)
    and audited; `busy` when another step holds the lock."""
    adapter = registry.adapter(channel)
    with _locked(conn, biz) as mine:
        if not mine:
            return {**_outcome(get(conn, biz, channel), ops=0, channel=channel), "busy": True}
        before = get(conn, biz, channel)
        sent = adapter.ops_sent(http)
        try:
            _advance(conn, settings, biz, channel, http)
        finally:
            after = get(conn, biz, channel)
            ops = adapter.ops_sent(http) - sent
            _record(conn, biz, before, after, ops)
        return _outcome(after, ops=ops, channel=channel)


# ---------------------------------------------------------------- the entry points


def launch(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, *, http: httpx.Client | None = None
) -> dict:
    """Bring the business's ads on `channel` one step closer to its approved plan: queued -> checked (`validate`:
    still `queued`, validated_at set, plan P4), queued -> launching -> live (`live`: the create, then the enable),
    launching -> live, waiting for an account that is ready now -> queued and on, a newer approved version applied
    to ads already built (`update`), a channel the approved version leaves out paused. Idempotent: a step with
    nothing to do does nothing, and a repeat after any failure converges on one campaign. Returns {"state", "say",
    "ops", "plan_id", "last_error"} (plus "busy" when another step for the business is running); `ops` is what this
    step sent (ruling S4). With the channel's switch `off` for this business (exec_mode_for), nothing is called."""
    if registry.exec_mode_for(conn, settings, biz, channel) == "off":
        return _outcome(get(conn, biz, channel), ops=0, channel=channel)
    if http is None:
        with registry.adapter(channel).client() as own:
            return _step(conn, settings, biz, channel, own)
    return _step(conn, settings, biz, channel, http)


def update(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, *, http: httpx.Client | None = None
) -> dict:
    """P10: a newer approved plan than the row's `plan_id` onto ads already built (for ads still `launching`, before
    they are turned on). Nothing for a business with nothing built on `channel`; `launch` does that."""
    row = get(conn, biz, channel)
    if row is None or not _built(row):
        return _outcome(row, ops=0, channel=channel)
    return launch(conn, settings, biz, channel, http=http)


def launch_now(biz: str, *, budget_s: float = 20.0) -> dict:
    """P6, the approval hook: right after an approval commits (confirm_proposal, the plan page's Approve), one launch
    step in the request, on its own connection, for each channel the approved version funds (and each it now leaves
    out, whose ads are paused), within `budget_s` seconds of platform time in all -- a channel whose switch is `off`
    is named as such, and one reached after the budget is spent is left as it is. Stops come first (M1 Task 5b, R26):
    every channel whose step is a pause -- the version leaves it out, or a pause is owed -- or a refused row's owed
    stop (M1 follow-up fix round 1) is taken before any launch or update, so another channel's launch never spends
    the time a stop needed. Never raises: whatever does not finish stays on its row for the hourly `ads-launch`.
    Returns {"channels": {channel: {"state", "say"}}, "say"}: the channels it could name, in card order, and their
    sentences joined (none when nothing could be read)."""
    try:
        settings = config.load()
        if all(registry.exec_mode(settings, ch) == "off" for ch in registry.CHANNELS):
            return {"channels": {}, "say": OFF}
        deadline = time.monotonic() + budget_s
        out: dict[str, dict] = {}
        with db.connect(settings.database_url) as conn:
            # a business off AGENT_LAUNCH_ALLOWLIST is answered as if every switch were off
            if all(registry.exec_mode_for(conn, settings, biz, ch) == "off" for ch in registry.CHANNELS):
                return {"channels": {}, "say": OFF}
            plan = approved(conn, biz)
            funded = funded_channels(plan)
            present = {r["channel"]: r for r in rows(conn, biz)}
            named = [c for c in registry.ORDER if registry.known(c) and (c in funded or c in present)]
            stops = [c for c in named if plan and c in present and _work(settings, present[c], plan) in STOPS]
            for channel in [*stops, *(c for c in named if c not in stops)]:
                if registry.exec_mode_for(conn, settings, biz, channel) == "off":
                    if channel in funded:
                        out[channel] = {"state": "off", "say": OFF_CHANNEL.format(label=registry.label(channel))}
                    continue
                left = deadline - time.monotonic()
                try:
                    if left <= 0:  # the budget is spent: the hourly ads-launch takes it from here
                        row = get(conn, biz, channel)
                        out[channel] = {
                            "state": row["state"] if row else "queued",
                            "say": status_sentence(row, channel=channel),
                        }
                        continue
                    with registry.adapter(channel).client(budget_s=left) as http:
                        step = launch(conn, settings, biz, channel, http=http)
                    out[channel] = {"state": step["state"], "say": step["say"]}
                except Exception as e:  # noqa: BLE001 - the approval is committed; the sweeper finishes what this could not
                    log.warning("ads launch_now: %s for business %s on %s", type(e).__name__, biz, channel)
                    retrying = {"channel": channel, "state": "queued", "last_error": RETRY_SOON}
                    out[channel] = {"state": "queued", "say": status_sentence(retrying)}
        return _launched({channel: out[channel] for channel in named if channel in out})  # in card order
    except Exception as e:  # noqa: BLE001 - the approval is committed; the sweeper finishes what this could not
        log.warning("ads launch_now: %s for business %s", type(e).__name__, biz)
        return {"channels": {}, "say": LAUNCH_LATER}


def _launched(channels: dict[str, dict]) -> dict:
    """The answer over several channels: the outcome per channel, and the owner's sentences joined into one."""
    return {"channels": channels, "say": " ".join(c["say"] for c in channels.values() if c.get("say"))}


def pause(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    channel: str,
    *,
    http: httpx.Client | None = None,
    by: str,
    reason: str = OWNER,
) -> dict:
    """Spec §5.3 step 5 for one channel (W, no approval: stopping spend is always allowed). The pause is always
    recorded: the row says `paused`, and nothing launches, updates or turns on a paused row (`_work`, `due`) until
    every pause on it is lifted. Two pauses can stand at once (the controller's ruling, M0 Task 4 fix round 1): the
    owner's (`reason` OWNER -- the owner or their assistant), a mark of its own (`resources.owner_paused`), and one of
    Admiry's (`paused_reason`, ADMIRY_REASONS), which a stronger reason of Admiry's replaces and a weaker one never
    does. Anything built on the platform is paused there too, whatever the row says -- `launching` included: its
    enable may have reached the platform with the answer lost. When the platform cannot be told -- the switch is
    `off`, `validate` never changes what is real, or the platform failed -- `resources.pause_owed` records it (ruling
    S11; the owner reads the adapter's `pause_owed` words): the hourly ads-launch sends the pause under the `live`
    switch and clears the flag, and it is logged for the operator. `live_paused` (N4) sends it as `live` does: a stop
    is always allowed. Ads never built change only here. It never waits for, or fails on, the lock a running step
    holds (ruling S17): the pause is then recorded without it, owed whatever is built (a create may be in flight), and
    that step reads the row again before it turns anything on and never writes a state over `paused`. `by` ("owner",
    "assistant", "admiry", "job:<name>", "stripe", ...) is kept as `resources.paused_by`. A row already paused gets
    this pause's mark too, and its `paused_at` moves -- so a resume
    that read it before loses (ruling S17); one a resume turned back on after this pause read it is paused for real,
    and a pause that lands between this one's read and its write keeps its own mark. A pause the platform takes on a
    refused row whose stop was owed (`stop_owed`, M1 follow-up fix round 1) is that stop: the row is paused, not
    failed, and the owed stop goes (an owed pause the hourly step sends later ends it the same way, `_repause`).

    A3p-2 H33, every reason of Admiry's (M0 Task 9 D6): a row someone paused by hand on the platform itself (sync's
    note, the adapter's `words.paused_elsewhere`, on a `limited` row) is theirs -- no pause of Admiry's claims it,
    neither on the row read here nor in the write's own WHERE (a note that lands between the two keeps the row as it
    is), so no resume of Admiry's ever turns those ads on; the outcome is the row as it stands. An adapter without
    those words (NewsBreak's: the owner has no hand on a platform Admiry runs, M1 Task 5b) has no such row."""
    adapter = registry.adapter(channel)
    if http is None:
        with adapter.client() as own:
            return pause(conn, settings, biz, channel, http=own, by=by, reason=reason)
    owner = reason == OWNER
    marks = {"paused_by": by} | ({"owner_paused": True} if owner else {})
    # the pause's reason on the row as it is when this writes (a pause without the lock can land between the read
    # and the write): fresh on a row not paused; beside another pause, the owner's keeps Admiry's and Admiry's keeps
    # the stronger of the two
    merged = f"CASE WHEN state <> 'paused' THEN %(reason)s::text WHEN %(owner)s THEN paused_reason ELSE {_STRONGER} END"
    elsewhere = adapter.words.paused_elsewhere
    args = {
        "owner": owner,
        "order": list(ADMIRY_REASONS),
        "reason": None if owner else reason,
        "admiry": not owner,
        "elsewhere": elsewhere,
        "b": biz,
        "ch": channel,
    }
    with _locked(conn, biz) as mine:
        row = get(conn, biz, channel)
        if row is None:
            raise ToolError(NOT_BUILT.format(label=registry.label(channel)))
        if not owner and elsewhere and row["last_error"] == elsewhere:  # H33, D6: paused by hand on the platform
            return _outcome(row, ops=0, channel=channel)
        if row["state"] == "paused":
            # an A3 pause (no mark) is the owner's: marked now, so a reason of Admiry's beside it never hides it. A mark
            # that is there already is left to the write's merge: re-adding it could undo the owner's lift of it that
            # landed after this read (final review)
            added = marks | ({"owner_paused": True} if _a3_owner_pause(row) else {})
            cur = conn.execute(
                f"UPDATE channel_campaign SET paused_at = now(), paused_reason = {merged}, "
                "resources = resources || %(marks)s, updated_at = now() "
                f"WHERE business_id = %(b)s AND channel = %(ch)s AND state = 'paused' AND NOT {_ELSEWHERE}",
                args | {"marks": Jsonb(added)},
            )
            if cur.rowcount:
                return _outcome(get(conn, biz, channel), ops=0, channel=channel)
            # a resume turned the row back on after this read it (ruling S17: the pause wins): pause it for real
            row = get(conn, biz, channel)
        ops, owed = 0, None
        mode = registry.exec_mode(settings, channel)
        if not mine:
            owed = "another step for the business is running"
        elif _built(row):
            owed = f"the switch is {mode}"
            if mode in ("live", config.LIVE_PAUSED) and adapter.capabilities().pause:  # N4: a stop is always sent
                sent = adapter.ops_sent(http)
                try:
                    adapter.set_paused(conn, settings, _account(conn, biz, channel), row["resources"], True, http=http)
                    owed = None
                except (PlatformError, ToolError, Unsupported) as e:  # ToolError: no linked account to tell
                    owed = str(e)
                ops = adapter.ops_sent(http) - sent
            elif mode in ("live", config.LIVE_PAUSED):
                owed = "the platform is paused by hand for this channel"
        if owed:
            log.warning(
                "ads pause for business %s not sent to %s (%s): ads-launch retries it under the live switch; "
                "an operator must act while it stays owed",
                biz,
                registry.label(channel),
                owed,
            )
        # a pause the platform took is a refused row's owed stop too (`stop_owed`, M1 follow-up fix round 1): it goes
        conn.execute(
            f"UPDATE channel_campaign SET state = 'paused', paused_at = now(), paused_reason = {merged}, "
            "last_error = %(error)s, resources = CASE WHEN %(owed)s THEN resources || %(marks)s "
            "ELSE (resources || %(marks)s) - 'stop_owed' END, updated_at = now() "
            f"WHERE business_id = %(b)s AND channel = %(ch)s AND NOT {_ELSEWHERE}",
            args
            | {
                "error": adapter.words.pause_owed if owed else None,
                "marks": Jsonb(marks | ({"pause_owed": True} if owed else {})),
                "owed": bool(owed),
            },
        )
        return _outcome(get(conn, biz, channel), ops=ops, channel=channel)


def pause_channels(
    conn: psycopg.Connection,
    settings: config.Settings,
    business_id: str,
    *,
    channels: list[str] | None = None,
    by: str,
    reason: str,
) -> dict:
    """Pause the business's ads on `channels` (None: every channel it has a row for), each through `pause`, for
    `reason` (REASONS: OWNER for the owner or their assistant, or one of Admiry's own). A channel with no row has
    nothing to pause and is left out; nothing is refused. Returns {"channels": {channel: outcome}, "say"}."""
    if reason not in REASONS:
        raise ValueError(f"unknown pause reason {reason!r}")
    out: dict[str, dict] = {}
    for row in rows(conn, business_id):
        channel = row["channel"]
        if (channels is None or channel in channels) and registry.known(channel):
            out[channel] = pause(conn, settings, business_id, channel, by=by, reason=reason)
    return _launched(out)


@dataclass(frozen=True)
class ResumeIf:
    """`resume`'s optional condition (A3p-2 Task 4 review, ruling H33; page-check's): what decides is the row as it is
    under the business's lock and in the UPDATE's own WHERE, never what the caller read earlier. `carried`: the
    address the ads must still carry (the adapter's `carried_url`; checked under the lock -- every writer of it holds
    that lock); `page_url`: an address the business's page must still be published at (the one the caller found
    open; checked in the UPDATE itself, since a publish or an unpublish commits without the lock)."""

    carried: str | None = None
    page_url: str | None = None


def _resume_holds(conn: psycopg.Connection, biz: str, channel: str, row: dict, only_if: ResumeIf) -> bool:
    """`only_if` on the row `resume` read under the lock (the page half is its UPDATE's WHERE too)."""
    if only_if.carried is not None and registry.adapter(channel).carried_url(row["resources"]) != only_if.carried:
        return False
    if only_if.page_url is None:
        return True
    live = conn.execute(
        "SELECT 1 FROM page WHERE business_id = %s AND status = 'published' AND url = %s", (biz, only_if.page_url)
    ).fetchone()
    return live is not None


def resume(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    channel: str,
    *,
    http: httpx.Client | None = None,
    reason: str = OWNER,
    only_if: ResumeIf | None = None,
) -> dict:
    """Spec §5.3 step 5 for one channel (P, non-spend: the assistant confirms with the owner first): lift `reason`'s
    pause -- the owner's mark, or Admiry's reason when it is the one standing. While the other pause stands, only this
    one is lifted: the row stays paused and its sentence says whose pause is left. With none left, paused ads that
    were built go back to `launching` and are turned on by the same enable step as a launch (a newer approved version
    applied first, the budget checked; what does not finish now, the hourly ads-launch does); ads never built go back
    to where an approval would put them -- `queued` (not checked yet, plan P4), or waiting for the account or the
    image -- and launch. Under the `validate` switch a built campaign's resume is only checked by the platform and the
    row stays paused; under `live_paused` (N4) it is refused (DEMO_PAUSED) and the row stays paused. Lifting Admiry's
    pause forgets the month cap's mark (`month_cap_at`), whether the row turns on or stays paused for the owner: the
    cap is gone either way. A pause that lands while this runs wins (ruling S17: every
    write here is conditioned on the `paused_at` it read, and a lift of Admiry's on the reason it read). ToolError with
    an owner sentence when that pause is not there (NOT_PAUSED, or PAUSED_BY_ADMIRY for an owner's resume of Admiry's
    pause) or the ads cannot be changed now.

    `only_if` (page-check's, A3p-2 H33): lifted only while the condition holds on the row read under the lock and in
    the UPDATE's own WHERE -- a page moved or taken down since the caller looked leaves the row exactly as it is, and
    the outcome says so (still `paused`, no operation). No resume of Admiry's lifts its pause on a row someone paused
    by hand on the platform itself (D6, the adapter's `words.paused_elsewhere`): left as it is, the same way."""
    adapter = registry.adapter(channel)
    if http is None:
        with adapter.client() as own:
            return resume(conn, settings, biz, channel, http=own, reason=reason, only_if=only_if)
    elsewhere = adapter.words.paused_elsewhere
    cond = only_if or ResumeIf()
    with _locked(conn, biz) as mine:
        if not mine:
            raise ToolError(BUSY_NOW)
        row = get(conn, biz, channel)
        if row is None or row["state"] != "paused":
            raise ToolError(NOT_PAUSED)
        owner, admiry = owner_paused(row), row["paused_reason"]
        if reason == OWNER and not owner:
            raise ToolError(_paused_by_admiry([row]))
        if reason != OWNER and admiry != reason:
            raise ToolError(NOT_PAUSED)
        if reason != OWNER and elsewhere and row["last_error"] == elsewhere:  # H33, D6: theirs, never Admiry's to lift
            return _outcome(row, ops=0, channel=channel)
        if not _resume_holds(conn, biz, channel, row, cond):  # not the pause the caller meant any more: as it is
            return _outcome(row, ops=0, channel=channel)
        guard = {
            "b": biz,
            "ch": channel,
            "paused_at": row["paused_at"],
            "admiry": reason != OWNER,
            "elsewhere": elsewhere,
            "page_url": cond.page_url,
        }
        if admiry if reason == OWNER else owner:  # the other pause stands: this one alone is lifted
            # ...while that other pause is still the one this read (final review): an approval funding a `dropped`
            # channel again lifts `dropped` without the lock and without moving `paused_at`, and a lift of this pause
            # after it would leave the row paused with neither -- stranded. Else nothing is written: the row stays as
            # the last writer left it, and the next resume sees it.
            lift, other = (
                ("resources = resources - 'owner_paused'", "paused_reason IS NOT DISTINCT FROM %(reason)s")
                if reason == OWNER
                else (
                    "paused_reason = NULL, resources = resources - 'month_cap_at'",
                    "resources ? 'owner_paused' AND paused_reason IS NOT DISTINCT FROM %(reason)s",
                )
            )
            conn.execute(
                f"UPDATE channel_campaign SET {lift}, updated_at = now() WHERE business_id = %(b)s "
                f"AND channel = %(ch)s AND paused_at IS NOT DISTINCT FROM %(paused_at)s AND {other} "
                f"AND NOT {_ELSEWHERE} AND {_STILL_PUBLISHED}",
                guard | {"reason": admiry},
            )
            return _outcome(get(conn, biz, channel), ops=0, channel=channel)
        built, mode = _built(row), registry.exec_mode(settings, channel)
        if built and mode == "off":
            raise ToolError(SWITCHED_OFF.format(label=registry.label(channel)))
        if built and mode == config.LIVE_PAUSED:  # N4: nothing turns these ads on; ads never built may still be built
            raise ToolError(DEMO_PAUSED)
        if built and mode == "validate":
            sent = adapter.ops_sent(http)
            account = _account(conn, biz, channel)
            try:
                adapter.set_paused(conn, settings, account, row["resources"], False, http=http, validate_only=True)
            except PlatformError as e:
                raise ToolError(str(e)) from None
            return _outcome(row, ops=adapter.ops_sent(http) - sent, channel=channel)
        state, waiting = "launching", None
        if not built:
            status = account_status(conn, settings, biz, channel)
            ready = image_ready(approved(conn, biz), channel)
            state = "waiting_account" if not status.ready else "queued" if ready else "waiting_creative"
            waiting = None if status.ready else status.reason
        cur = conn.execute(
            "UPDATE channel_campaign SET state = %(state)s, paused_at = NULL, paused_reason = NULL, "
            "validated_at = CASE WHEN %(built)s THEN validated_at END, "
            "resources = resources - 'paused_by' - 'pause_owed' - 'owner_paused' - 'month_cap_at', "
            "last_error = %(waiting)s, updated_at = now() WHERE business_id = %(b)s AND channel = %(ch)s "
            "AND paused_at IS NOT DISTINCT FROM %(paused_at)s AND paused_reason IS NOT DISTINCT FROM %(reason)s "
            f"AND NOT {_ELSEWHERE} AND {_STILL_PUBLISHED}",
            guard | {"state": state, "built": built, "waiting": waiting, "reason": admiry},
        )
        # a pause landed after this resume read the row (ruling S17: the pause wins), or `only_if` stopped holding in
        # that moment: the row stays as it is
        if cur.rowcount == 0:
            return _outcome(get(conn, biz, channel), ops=0, channel=channel)
    return launch(conn, settings, biz, channel, http=http)


def resume_channels(
    conn: psycopg.Connection,
    settings: config.Settings,
    business_id: str,
    *,
    reason: str,
    by: str,
    channels: list[str] | None = None,
) -> dict:
    """Lift the pauses made for `reason` on `channels` (None: every channel) -- only those, each through `resume`: the
    owner's mark (OWNER; a paused row with neither mark is A3's, and every A3 pause was the owner's), or Admiry's
    reason where it is the one standing. An owner's pause is never lifted by a job, nor Admiry's by the owner: while
    the other stands the row stays paused and its outcome says whose pause is left. A row that cannot be resumed now
    keeps its pause and says why (its outcome's `error`). Returns {"channels": {channel: outcome}, "say"}; `by` is
    logged."""
    if reason not in REASONS:
        raise ValueError(f"unknown pause reason {reason!r}")
    out: dict[str, dict] = {}
    for row in rows(conn, business_id):
        channel = row["channel"]
        mine = owner_paused(row) if reason == OWNER else row["paused_reason"] == reason
        named = channels is None or channel in channels
        if row["state"] != "paused" or not mine or not named or not registry.known(channel):
            continue
        try:
            out[channel] = resume(conn, settings, business_id, channel, reason=reason)
        except ToolError as e:
            out[channel] = {**_outcome(get(conn, business_id, channel), ops=0, channel=channel), "error": str(e)}
    if out:
        log.info("ads resume (%s) by %s for business %s: %s", reason, by, business_id, ", ".join(out))
    return _launched(out)


def pause_by_owner(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str = "all", *, by: str
) -> dict:
    """The owner's pause -- their assistant's `pause` tool, /connect's button (D102: one command, thin callers):
    `channel` is a channel id or "all". ToolError("unknown channel") for an id no adapter runs; NOT_BUILT (NO_ADS for
    "all") when there is nothing to pause."""
    if channel != "all":
        registry.adapter(channel)
    out = pause_channels(conn, settings, biz, channels=None if channel == "all" else [channel], by=by, reason=OWNER)
    if not out["channels"]:
        raise ToolError(NO_ADS if channel == "all" else NOT_BUILT.format(label=registry.label(channel)))
    return out


def resume_by_owner(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str = "all", *, by: str
) -> dict:
    """The owner's resume (the `resume` tool, /connect's button): their own pauses only, on `channel` or "all" -- a
    pause Admiry made itself is lifted by Admiry (PAUSED_BY_ADMIRY when it is the only one); where both stand, the
    owner's is lifted and the ads stay off, and the answer says why. ToolError NOT_PAUSED when nothing is paused, and
    the reason one channel cannot be resumed now (BUSY_NOW, SWITCHED_OFF) when none could be."""
    if channel != "all":
        registry.adapter(channel)
    paused = [r for r in rows(conn, biz) if r["state"] == "paused" and channel in ("all", r["channel"])]
    if not paused:
        raise ToolError(NOT_PAUSED)
    if not any(owner_paused(r) for r in paused):
        raise ToolError(_paused_by_admiry(paused))
    if channel != "all":
        return _launched({channel: resume(conn, settings, biz, channel, reason=OWNER)})
    out = resume_channels(conn, settings, biz, reason=OWNER, by=by)
    errors = [o["error"] for o in out["channels"].values() if "error" in o]
    if errors and len(errors) == len(out["channels"]):
        raise ToolError(errors[0])
    return out


def all_paused(conn: psycopg.Connection, business_id: str, channels: list[str]) -> bool:
    """Every named channel's row is paused and the platform was told (no `pause_owed`); a channel with no row has
    nothing running and counts as paused."""
    if not channels:
        return True
    left = conn.execute(
        "SELECT count(*) AS n FROM channel_campaign WHERE business_id = %s AND channel = ANY(%s) "
        "AND (state <> 'paused' OR resources @> '{\"pause_owed\": true}')",
        (business_id, list(channels)),
    ).fetchone()["n"]
    return left == 0


def _daily_cents(monthly_usd: float) -> int:
    """Spec §6.4: a channel's daily budget is its monthly amount / 30.4, whole cents, rounded down."""
    return math.floor(float(monthly_usd) / DAYS_IN_MONTH * 100)


def daily_budgets(conn: psycopg.Connection, business_id: str) -> dict[str, int]:
    """Cents a day per channel for the rows that are, or are about to be, spending (`queued`, `launching`, `live`),
    from each row's own plan version (the one running or queued): M1m's reserve and days-left read it."""
    out: dict[str, int] = {}
    for row in rows(conn, business_id):
        if row["state"] not in ("queued", "launching", "live"):
            continue
        found = entry(plans.get(conn, business_id, row["plan_id"]), row["channel"])
        if found is not None:
            out[row["channel"]] = _daily_cents(found["monthly_usd"])
    return out


def _upsert_day(conn: psycopg.Connection, biz: str, channel: str, day: MetricDay) -> None:
    """The platform's numbers for one day of `channel`, from its API. `leads` and `booked` are the lead store's to
    count (plan P5, `_count_leads`): a new day starts them at 0, and the platform's numbers never touch them."""
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%(b)s, %(ch)s, %(day)s, %(cost_micros)s, %(impressions)s, %(clicks)s, "
        "%(calls)s, 0, %(conversions)s, 0, 'api') "
        "ON CONFLICT (business_id, channel, day) DO UPDATE SET cost_micros = EXCLUDED.cost_micros, "
        "impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks, calls = EXCLUDED.calls, "
        "conversions = EXCLUDED.conversions, source = EXCLUDED.source, synced_at = now()",
        {"b": biz, "ch": channel, **asdict(day)},
    )


def _count_leads(conn: psycopg.Connection, biz: str, channel: str, days: list[date]) -> None:
    """Plan P5 (M0 Task 7): `metric_daily.leads` of each of `days` = the business's `lead` rows attributed to `channel`
    (`lead.channel`) created that day in the account's own calendar (`account_zone`: from that day's midnight to the
    next), whatever their status -- spam included; `booked` = those of them booked or won. Only the days given are
    counted again, and only the ones with a row."""
    tz = account_zone(conn, biz, channel)
    for day in days:
        conn.execute(
            "UPDATE metric_daily SET leads = c.leads, booked = c.booked FROM (SELECT count(*) AS leads, "
            "count(*) FILTER (WHERE status IN ('booked', 'won')) AS booked FROM lead WHERE business_id = %(b)s "
            "AND channel = %(ch)s AND created_at >= %(start)s AND created_at < %(end)s) c "
            "WHERE metric_daily.business_id = %(b)s AND metric_daily.channel = %(ch)s AND metric_daily.day = %(day)s",
            {
                "b": biz,
                "ch": channel,
                "day": day,
                "start": datetime.combine(day, datetime.min.time(), tzinfo=tz),
                "end": datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz),
            },
        )


def sync(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, *, http: httpx.Client | None = None
) -> dict:
    """Metrics, serving status and reviews into `metric_daily` and the row (A3 §6, per channel): the adapter's
    `pull_metrics` for the last SYNC_DAYS days of the account's own calendar (late conversions land within the
    window), each day it returned then counted again for its `leads` and `booked` from the lead store (plan P5,
    `_count_leads`, in the zone this very read may just have kept) -- only those days: an older one keeps what its
    last sync counted, and a day the platform returned nothing for has no row -- then its `pull_issues` -- the row's
    state and sentence as the platform's view makes them (the adapter's own notes the only ones it sets or clears,
    ruling S15) and the issues into `resources.issues`. Read-only against the platform but for the adapter's own
    upkeep of what it built, under the `live` switch alone (NewsBreak's, M1 Task 5b: ads on their way out turned off,
    the ad set's end date, the month's spending cap, ads switched off at the old cap turned back on) -- never the row's
    state, plan or the lifecycle's own keys; nothing for a row with nothing built. A platform that serves ads the row
    shows paused (`Serving.repause`: NewsBreak's, a channel the owner cannot reach) has the pause owed to it again --
    `pause_owed` and the adapter's `pause_owed` sentence, on a row still paused when this writes -- for the hourly
    ads-launch to send. One step at a time per business, like every other lifecycle step
    (`_locked`); `busy` when ads-launch's own step holds it instead -- only a pause never waits for it (ruling S17),
    and sync's own state write gives way to one that landed while the platform answered. With the channel's switch
    `off`, nothing is called (a Global Constraint); under `validate` sync still reads: a read is never a write. The
    platform's own failures propagate for the ads-sync job to isolate. Returns {"state", "say", "ops", "plan_id",
    "last_error"} (plus "busy")."""
    if registry.exec_mode(settings, channel) == "off":
        return _outcome(get(conn, biz, channel), ops=0, channel=channel)
    adapter = registry.adapter(channel)
    if http is None:
        with adapter.client() as own:
            return sync(conn, settings, biz, channel, http=own)
    with _locked(conn, biz) as mine:
        if not mine:
            return {**_outcome(get(conn, biz, channel), ops=0, channel=channel), "busy": True}
        row = get(conn, biz, channel)
        if row is None or not _built(row):
            return _outcome(row, ops=0, channel=channel)
        account = _account(conn, biz, channel)
        sent = adapter.ops_sent(http)
        days = adapter.pull_metrics(conn, settings, account, row["resources"], SYNC_DAYS, http=http)
        for day in days:
            _upsert_day(conn, biz, channel, day)
        _count_leads(conn, biz, channel, [d.day for d in days])
        seen = adapter.pull_issues(conn, settings, account, row, http=http)
        # the platform serves ads the row shows paused (`Serving.repause`, M1 Task 5b): the pause is owed to it again,
        # as `pause` owes one the platform was not told of (ruling S11) -- the hourly ads-launch sends it
        repause = seen.repause and row["state"] == "paused"
        if repause:
            log.warning(
                "ads: %s serves business %s's ads the row shows paused; the pause is owed to it again",
                registry.label(channel),
                biz,
            )
        # the state and the sentence move only if nobody else moved them while the platform answered: a pause (ruling
        # S17: it never waits for this step's lock) or an approval stays as it was written; the owed pause only on a row
        # still paused when this writes
        conn.execute(
            "UPDATE channel_campaign SET resources = (resources - 'issues') || %(issues)s "
            "|| CASE WHEN %(repause)s AND state = 'paused' THEN %(owed)s::jsonb ELSE '{}'::jsonb END, "
            "synced_at = now(), updated_at = now(), "
            "state = CASE WHEN state = %(was)s AND last_error IS NOT DISTINCT FROM %(was_error)s "
            "THEN %(state)s ELSE state END, "
            "last_error = CASE WHEN state = %(was)s AND last_error IS NOT DISTINCT FROM %(was_error)s "
            "THEN %(error)s ELSE last_error END "
            "WHERE business_id = %(b)s AND channel = %(ch)s",
            {
                "issues": Jsonb({"issues": seen.issues} if seen.issues else {}),
                "repause": repause,
                "owed": Jsonb({"pause_owed": True}),
                "was": row["state"],
                "was_error": row["last_error"],
                "state": seen.state,
                "error": adapter.words.pause_owed if repause else seen.last_error,
                "b": biz,
                "ch": channel,
            },
        )
        return _outcome(get(conn, biz, channel), ops=adapter.ops_sent(http) - sent, channel=channel)


def sync_stops(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, *, http: httpx.Client | None = None
) -> dict:
    """ads-sync's pass on a day the jobs hold a channel (M1 Task 6 fix round 1, item 1: NewsBreak's day budget, or two
    throttled answers in a row): what its read does that stops spend, and nothing else -- through the adapter's
    `pull_stops` hook, for an adapter that has one (NewsBreak's: ads on their way out turned OFF, and whether the
    platform serves ads the row shows paused). A row it finds served while paused has the pause owed to the platform
    again (`pause_owed` and the adapter's `pause_owed` sentence, on a row still paused when this writes), which the
    hourly ads-launch sends past either hold. No metrics, no state, no issues, no read time: the next whole read's.
    Under the business's lock like every step (`busy` when another step holds it); nothing with the switch `off`.
    Returns {"state", "say", "ops", "plan_id", "last_error"} (plus "busy")."""
    if registry.exec_mode(settings, channel) == "off":
        return _outcome(get(conn, biz, channel), ops=0, channel=channel)
    adapter = registry.adapter(channel)
    stops = getattr(adapter, "pull_stops", None)
    if http is None:
        with adapter.client() as own:
            return sync_stops(conn, settings, biz, channel, http=own)
    with _locked(conn, biz) as mine:
        if not mine:
            return {**_outcome(get(conn, biz, channel), ops=0, channel=channel), "busy": True}
        row = get(conn, biz, channel)
        if stops is None or row is None or not _built(row):
            return _outcome(row, ops=0, channel=channel)
        sent = adapter.ops_sent(http)
        if stops(conn, settings, _account(conn, biz, channel), row, http=http) and row["state"] == "paused":
            log.warning(
                "ads: %s serves business %s's ads the row shows paused; the pause is owed to it again",
                registry.label(channel),
                biz,
            )
            conn.execute(
                "UPDATE channel_campaign SET resources = resources || %(owed)s, last_error = %(error)s, "
                "updated_at = now() WHERE business_id = %(b)s AND channel = %(ch)s AND state = 'paused'",
                {"owed": Jsonb({"pause_owed": True}), "error": adapter.words.pause_owed, "b": biz, "ch": channel},
            )
        return _outcome(get(conn, biz, channel), ops=adapter.ops_sent(http) - sent, channel=channel)


# ---------------------------------------------------------------- reporting (get_channel_status, get_report,
# get_status_issues, /connect)


def _usd(micros: int) -> float:
    return float(Decimal(micros) / Decimal(1_000_000))


def today_spend(conn: psycopg.Connection, biz: str, channel: str, row: dict | None) -> dict | None:
    """{"spent_usd", "budget_usd"} for `status_sentence`'s live line and get_channel_status's `today` -- `channel`'s
    spend today in the account's own calendar (ruling S14), the day the platform is still adding to, against the
    daily budget of the channel's entry in the row's own plan version (spec §6.4); None without anything built or a
    plan entry to read the budget from."""
    if row is None or not _built(row):
        return None
    found = entry(plans.get(conn, biz, row["plan_id"]), channel)
    if found is None:
        return None
    spent = conn.execute(
        "SELECT cost_micros FROM metric_daily WHERE business_id = %s AND channel = %s AND day = %s",
        (biz, channel, account_today(row)),
    ).fetchone()
    return {
        "spent_usd": _usd(int(spent["cost_micros"])) if spent else 0.0,
        "budget_usd": _daily_cents(found["monthly_usd"]) / 100,
    }


def week_totals(conn: psycopg.Connection, biz: str, channel: str, row: dict | None) -> dict:
    """`channel`'s clicks/calls/leads (conversions, rounded) over the last 7 days of the account's calendar, today
    included (ruling S14) -- get_channel_status's compact weekly line."""
    today = account_today(row)
    r = conn.execute(
        "SELECT coalesce(sum(clicks), 0) AS clicks, coalesce(sum(calls), 0) AS calls, "
        "coalesce(sum(conversions), 0) AS conversions FROM metric_daily "
        "WHERE business_id = %s AND channel = %s AND day BETWEEN %s AND %s",
        (biz, channel, today - timedelta(days=6), today),
    ).fetchone()
    return {"clicks": int(r["clicks"]), "calls": int(r["calls"]), "leads": round(float(r["conversions"]))}


def week_leads(conn: psycopg.Connection, biz: str, channel: str, row: dict | None) -> int:
    """The leads `channel` brought -- `lead` rows attributed to it (`lead.channel`: the lead store, plan P5) -- over
    the same seven days of the account's calendar as `week_totals`, today included: the channels card's "1 form this
    week" (M0 Task 6; every lead a page records today is a form), in the zone `metric_daily.leads` is counted in."""
    tz = account_zone(conn, biz, channel)
    first = datetime.combine(account_today(row) - timedelta(days=6), datetime.min.time(), tzinfo=tz)
    return conn.execute(
        "SELECT count(*) AS n FROM lead WHERE business_id = %s AND channel = %s AND created_at >= %s",
        (biz, channel, first),
    ).fetchone()["n"]


def month_spend(conn: psycopg.Connection, biz: str, channel: str, row: dict | None) -> float:
    """`channel`'s spend so far this calendar month of the account's own calendar (ruling S14), from `metric_daily`:
    the channels card's month-to-date and its footer (M0 Task 6)."""
    today = account_today(row)
    r = conn.execute(
        "SELECT coalesce(sum(cost_micros), 0) AS cost_micros FROM metric_daily WHERE business_id = %s "
        "AND channel = %s AND day BETWEEN %s AND %s",
        (biz, channel, today.replace(day=1), today),
    ).fetchone()
    return _usd(int(r["cost_micros"]))


def day_rows(conn: psycopg.Connection, biz: str, channel: str, limit: int, row: dict | None) -> list[dict]:
    """`channel`'s last `limit` calendar days ending today, newest first -- get_report's `days`. A day the platform
    never returned anything for (no activity) is zero-filled (Fix round 1; `statement()` already zero-fills a
    quiet month the same way), so the period always has exactly `limit` rows and `totals` never has to guess. One
    calendar, the account's own (ruling S14: `metric_daily.day` is a day in the account's time zone) -- never the
    database's session time zone, which can read a different day in the evening."""
    today = account_today(row)
    found = conn.execute(
        "SELECT gs.day::date AS day, d.cost_micros, d.clicks, d.impressions, d.calls, d.conversions "
        "FROM generate_series(%(first)s::timestamp, %(today)s::timestamp, interval '1 day') "
        "AS gs(day) LEFT JOIN metric_daily d ON d.business_id = %(biz)s AND d.channel = %(ch)s "
        "AND d.day = gs.day::date ORDER BY gs.day DESC",
        {"biz": biz, "ch": channel, "first": today - timedelta(days=limit - 1), "today": today},
    ).fetchall()
    return [
        {
            "day": r["day"].isoformat(),
            "spent_usd": _usd(int(r["cost_micros"])) if r["cost_micros"] is not None else 0.0,
            "clicks": int(r["clicks"]) if r["clicks"] is not None else 0,
            "impressions": int(r["impressions"]) if r["impressions"] is not None else 0,
            "calls": int(r["calls"]) if r["calls"] is not None else 0,
            "conversions": float(r["conversions"]) if r["conversions"] is not None else 0.0,
        }
        for r in found
    ]


def totals(days: list[dict]) -> dict:
    """get_report's summed line over whatever `day_rows` returned."""
    return {
        "spent_usd": round(sum(d["spent_usd"] for d in days), 2),
        "clicks": sum(d["clicks"] for d in days),
        "impressions": sum(d["impressions"] for d in days),
        "calls": sum(d["calls"] for d in days),
        "conversions": round(sum(d["conversions"] for d in days), 2),
    }


def scaled_planning(plan: dict, days: int) -> dict | None:
    """The plan's own planning column (a full `plans.DAYS`-day month) scaled pro-rata to `days` -- get_report's
    7d/30d view beside the real numbers; None when the plan carries no `estimate.columns` (only a
    consultation-built plan does)."""
    planning = ((plan.get("sections") or {}).get("estimate") or {}).get("columns", {}).get("planning")
    if not planning:
        return None
    factor = days / plans.DAYS
    return {
        "spend": round(planning["spend"] * factor, 2),
        "leads": [round(x * factor, 1) for x in planning["leads"]],
        "bookings": [round(x * factor, 1) for x in planning["bookings"]],
        "customers": [round(x * factor, 1) for x in planning["customers"]],
        "revenue": [round(x * factor) for x in planning["revenue"]],
    }


def _month_start(d: date, months_back: int) -> date:
    total = d.year * 12 + (d.month - 1) - months_back
    return date(total // 12, total % 12 + 1, 1)


def _months(conn: psycopg.Connection, biz: str, row: dict, month: date | None = None) -> list[dict]:
    """This and the previous calendar month of the row's channel -- or only `month` (its first day), when asked for
    one -- summed from `metric_daily` (a quiet month zero-filled), in the account's own calendar (ruling S14): one
    clock for both the rows and the names."""
    today = account_today(row)
    starts = [month] if month is not None else [_month_start(today, back) for back in (0, 1)]
    found = conn.execute(
        "SELECT date_trunc('month', day)::date AS month, sum(cost_micros) AS cost_micros, sum(clicks) AS clicks, "
        "sum(impressions) AS impressions, sum(calls) AS calls, sum(conversions) AS conversions "
        "FROM metric_daily WHERE business_id = %s AND channel = %s AND day >= %s AND day < %s GROUP BY 1",
        (biz, row["channel"], min(starts), _month_start(max(starts), -1)),
    ).fetchall()
    by_month = {r["month"]: r for r in found}
    months = []
    for start in starts:
        r = by_month.get(start)
        months.append(
            {
                "month": start.strftime("%B %Y"),
                "spent_usd": _usd(int(r["cost_micros"])) if r else 0.0,
                "clicks": int(r["clicks"]) if r else 0,
                "impressions": int(r["impressions"]) if r else 0,
                "calls": int(r["calls"]) if r else 0,
                "conversions": float(r["conversions"]) if r else 0.0,
            }
        )
    return months


def statement(conn: psycopg.Connection, biz: str, month: date | None = None) -> dict | None:
    """P12, per channel: for every channel with a row (card order), this and the previous calendar month summed from
    `metric_daily` -- or only `month` (its first day: get_platform_statement's month) -- its label and the account id
    the owner recognises (`account_public`, the adapter's: Google Search shows the Customer ID), and the channels'
    billing sentences -- /connect, get_report(period="month") and get_platform_statement. None when the business has
    no channel row (nothing launched: no account to show). A billing sentence follows the way the channel's account
    runs (M1 follow-up 2, W2; `state.mode_of`, the read model's one answer): the owner's own account says the channel's
    own `Words.no_fee` (Google bills it directly), an account Admiry runs from the balance says MANAGED_NO_FEE -- a
    prepaid business never reads that Google bills it."""
    from . import state  # the read model imports this module

    settings = config.load()
    entries, notices = [], []
    for r in rows(conn, biz):
        channel = r["channel"]
        account = registry.account(conn, biz, channel) if registry.known(channel) else None
        public = None
        if account is not None:  # a read: only an account on file is asked about
            public = registry.adapter(channel).check_account(conn, settings, account).account_public
        entries.append(
            {
                "channel": channel,
                "label": registry.label(channel),
                "account_public": public,
                "months": _months(conn, biz, r, month),
            }
        )
        managed = state.mode_of(conn, biz, channel, account) == "managed"
        notice = MANAGED_NO_FEE.format(label=registry.label(channel)) if managed else _words(channel).no_fee
        if notice not in notices:
            notices.append(notice)
    return {"channels": entries, "no_fee_notice": "; ".join(notices)} if entries else None
