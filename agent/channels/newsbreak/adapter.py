"""The NewsBreak adapter (spec 2026-09-29-admiry-channels-design D84, D88-D90, §5.2; plan 2026-09-29-agent-m1-newsbreak
Task 5 and the controller's rulings: R1-R12 for its write path, R13-R16 and R21-R28 for its hourly read).
NewsBreak is `managed` only: Admiry runs it from the business's balance, in an ad account per business under Admiry's
NewsBreak Organization that the launch makes (P3), so the owner connects nothing (D84, D88). Importing this module
registers the adapter (`agent.channels` imports it).

What it does with NewsBreak's Business API (`client.py`, Task 2: every fact and constant comes from there, R1):
  launch   validate: the bodies recorded under `resources.validated`, nothing called (R12, D90). live: the ad account
           -- the Organization's accounts by name first, then a create -- its lead event looked for (R10), the
           approved image uploaded once per account and file (`resources.assets`), then the campaign, the ad set and
           the ads, every one created `status: "OFF"` (NewsBreak's default is ON, R2), and the account's spending cap
           (R5). Each id goes to the row as it is made (`ads_run.remember`), so a run cut short -- a throttle, a lost
           answer -- is resumed at the step it missed; an earlier attempt's object is looked up by name before any
           create (Review Focus 2). The campaign's id waits under `campaign_id` until the launch returns: `campaign`
           is the lifecycle's mark that ads are built (`ads_run._built`), and nothing is until the whole set is. A
           launch NewsBreak refused fails its row; the next version approved builds its own ads in the campaign and ad
           set already made (the failed attempt's ads go to `leftover`, its version mark goes) -- and so does a version
           approved after an attempt whose own version no longer builds (its image gone from the set): a version's ads
           never run under another one.
  enable   the approved plan's whole money (budget and bid, with the whole targeting) and cap first, then the campaign,
           the ad set and every ad ON -- after reading the row again: a pause that landed meanwhile wins (A3 S17). An ad
           NewsBreak will not turn on leaves the set for `leftover` while the others run; nothing of the set on at all
           is the enable refused (final review C1), raised: the lifecycle's stop switches the campaign OFF before the
           row fails, owed while NewsBreak does not take it (the M1 follow-up's fix round 2).
  update   the new version's whole money and area first, every time (S16, final review I1: one ad-set update carrying
           the budget, the bid and the whole targeting -- R4: NewsBreak resets an omitted one to everyone), then
           `build.diff`'s ads: changed words are new ads, each created OFF and remembered as it is made (`updating`),
           all turned ON once the last is made, and the old ads are left ON (`resources.replaced`) until every new one
           passes NewsBreak's review -- the hourly read turns them off then (R15) -- so there is never a moment with
           nothing running, and never an ad running that the row does not list (an attempt superseded or refused: its
           ads OFF, into `leftover`, which the hourly read turns off at once if one is still ON); the running ads moved
           to the page's address when it moved meanwhile (`diff` cannot see a page); the cap set again. The row keeps
           the ZIP codes and the address NewsBreak carries once each went through, the change refused afterwards or not.
  pause    the campaign OFF (or ON); a resume under `validate` calls nothing (D90).
  page     the ads' `clickThroughUrl` is `build.page_url` of the page (`src=nb`, the click id macro, R7); `move_page`
           moves every ad that serves -- `ads`, an update's in flight (`updating`), and a finished change's old ads
           still ON beside the new ones (`replaced`, fix round 2) -- to a new address with a URL-only update of each ad
           (R8), `AddressRefused` when NewsBreak refuses the address itself; an old ad whose move NewsBreak refuses (one
           deleted by hand) is logged and left, never the address refused. An ad left over (`leftover`) is never
           moved: OFF or soon OFF, and perhaps gone from NewsBreak, it must never make the address look refused.
  read     ads-sync's, hourly (`ads_run.sync`): the HOURLY report summed per day of the account's calendar (R13);
           NewsBreak's review of the ads that serve in its own words -- a `channel_issue` per sentence, resolved once
           the review clears -- and the campaign's and ad set's status as the row's (live, limited, paused; R14): ads
           NewsBreak shows OFF while Admiry runs them read PAUSED_IN_NEWSBREAK, an operator mailed once each time, and
           a campaign NewsBreak shows ON while the row is paused has the pause owed again (`Serving.repause`). Under
           `live` it keeps what Admiry built in order too: a finished update's old ads (`replaced`) turned OFF once
           every new one passed NewsBreak's review, any other ad on its way out (`leftover`) at once (R15, R21, R25),
           the ad set's end date moved a year out when 30 days near (R23), the month's cap set on the first read of
           each account month (R16, R22) and ads NewsBreak switched off at the old cap switched back on -- each of
           these writes with its audit row (job:ads-sync, 5b fix round 1). On a day the jobs hold NewsBreak (its day
           budget, or two throttles in a row: `jobs._stop`), the read's stops alone (`pull_stops`, Task 6 fix round 1).
  conversions  NewsBreak's postback by click id, as the account's lead event (`client.ADMIRY_LEAD_EVENT`), whose id the
           hourly conversion upload looks up and keeps (`conversion_account`, M1 Task 6: no API makes the event).

One request a second per business at most, from each request's start and across steps and clients (`_Calls`,
`_LAST_CALL`). Within the approval's own time budget (`ads_run.launch_now`), a request that cannot fit is never sent:
the step stops on STARTING ("Admiry finishes within the hour") and the hourly job resumes at the missing step. A call
that stops spend -- a pause, an ad turned OFF -- is always sent (fix round 2): with whatever time is left, after only
NewsBreak's own pace (`NEWSBREAK_PACE_S`); a pause the budget cuts or NewsBreak fails stays owed, an ad's OFF stays
listed (`leftover`, `replaced`). A row never carries the client's "try again": NewsBreak busy or not answering is
RETRYING, Admiry's to retry; the token's trouble is ACCESS_FIXING ("Admiry is fixing its access to NewsBreak"), the
client's own sentence staying for its callers.

The spending cap (R5, as amended in fix round 1) is NewsBreak's LIFETIME cap for the account: a recorded base -- the
lifetime total at the launch, `resources.cap_base_cents`, recorded anew by the first read of each account month
(`resources.cap_month` says which month it is for, R22) -- plus this month's amount, so an update mid-month sets the
same exact cap; never at or below the live total (NewsBreak takes only a cap above it). NewsBreak may deliver a little
past its own cap at a fully spent month: a total above the last cap Admiry set by at most `cap_overshoot_cents` (the
larger of $5 and 1 % of that cap, fix round 2) is that overshoot, and the next cap is set from the base as usual. A
larger gap is figures that disagree -- a unit, or spend that should not have happened -- and sets no cap at all (fail
closed): the step waits on SPEND_CHECK (the hourly read's new month on its own MONTH_CHECK, M1 Task 7) and one operator
mail goes, saying what to check and how to clear it.

The account's own trouble is operator work, never the owner's problem (R10, R11): the lead event cannot be made by API
(an operator makes it by hand; its absence never blocks the ads, and the operator is mailed once per account), and an
account NewsBreak makes in a state other than ACTIVE, or refuses to make (the Organization's allowance), leaves the
ads waiting -- ACCOUNT_WAIT, "NewsBreak is getting your account ready; your ads start once it is" (never "funds",
M1 Task 7 fix round 1) -- with one operator mail per account and state; the launch looks again an hour later.
`check_account` reads only the database (`channel_account.external`): it runs inside the approval's transaction and
every channels-card render (`state._readiness` rolls it back), so it never reaches NewsBreak or the operator's mail --
the launch, under the step's lock and meter, does both and keeps what it learns there. NewsBreak documents no read of
an account's status (Get Ad Accounts gives id, name and createTime): the launch takes one from that list when
NewsBreak gives it, and otherwise an operator clears the mark once the account is fixed (`python -m agent.ops
nb-account-ready`, M1 Task 6, which the operator mail names -- it clears a SPEND_CHECK too).

`channel_account.external`, replaced whole by `registry.save_account` (read, merged, written back): `ad_account_id`,
`account_status` (NewsBreak's word, when it gave one), `account_checked_at` and `account_mailed` (a waiting account),
`event_id` or `event_mail_sent_at` (R10), `spending_cap_cents`, `spend_check_mailed_at` (R5's sanity check).
`resources`: `ad_account_id`, `campaign`, `ad_set_id`, `ads` ({"<slot>": id}), `assets` ({"<file>": NewsBreak's CDN
URL}), `cap_cents`, `cap_base_cents`, `cap_month` (the account month that base is for), `time_zone`
(America/Los_Angeles, NewsBreak's reporting zone, R6), `location_ids`, `click_url`, the ads on their way out until the
hourly read sees each OFF or gone -- `replaced` (a finished update's old ads, ON until every new one passes NewsBreak's
review) and `leftover` (any other: a failed or refused attempt's, a gone version's, turned OFF at once); a row written
before the two lists has `retiring`, read as `leftover` -- `told` (the operator mails the read sent, per kind and
occurrence), `end_unknown` (an ad set NewsBreak lists without an end date, reported once), `undated_report` (the
account day a report row with a cost on no day Admiry can read was last logged, once a day), `costless_report` (the
same for a row with no cost Admiry can read, final review Minor 3), `issues` (the read's:
NewsBreak's words for what holds the ads back, the lifecycle's to store), `validated` under `validate`; in flight
`campaign_id`, `creating_plan_id` (the version the ad set is made from) and `updating` (an update's version, page
address and the ads it has made). Never the lifecycle's own keys, and never the token: it stays in `settings`."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import NoReturn

import httpx
import psycopg
from fastmcp.exceptions import ToolError

from ... import ads_run, audit, business, geo, landing, mail_out, plans, wallet
from ...config import Settings
from .. import registry
from ..base import (
    MANAGED_NO_FEE,
    Account,
    AccountStatus,
    AddressRefused,
    Build,
    Capabilities,
    ConversionEvent,
    LeadIn,
    MetricDay,
    PlatformError,
    Resources,
    Serving,
    Step,
    Unsupported,
    UploadResult,
    Words,
)
from . import build as nb_build
from . import client as newsbreak
from . import creative

log = logging.getLogger(__name__)

CHANNEL = "newsbreak"
TIME_ZONE = "America/Los_Angeles"  # R6: NewsBreak's own reporting zone, the calendar of the account's days
PACE_S = 1.0  # the Global Constraint: one request a second per business at most, from each request's start
CALL_MIN_S = 1.0  # what is left of a step's time budget for one more request to be worth sending
# how long an account waiting at NewsBreak is not asked about again: less than the hourly job's period (fix round 1), so
# each hour's step looks once more -- an hour's mark would leave it to every other hour
RECHECK = timedelta(minutes=50)
FAR_SLACK_MILES = 10.0  # R9: the nearest ZIP centroid may lie this much beyond the plan's radius
END_DAYS = 365  # an ad set's `endTime` (required by NewsBreak) is a year after its start
ACTIVE = "ACTIVE"  # a usable account, in NewsBreak's word (Create An Ad Account's example answer)
CREATE_REFUSED = "CREATE_REFUSED"  # Admiry's own word in `account_status`: NewsBreak refused to make the account
NEEDS_ATTENTION = "needs_attention"  # the channel_account status while the account waits at NewsBreak
# R1: a failure Admiry retries -- NewsBreak busy or not answering, or the token's trouble an operator fixes; any other
# is NewsBreak refusing the request itself, which the same request would meet again
RETRYABLE = (newsbreak.BUSY, newsbreak.UNREACHABLE, newsbreak.ACCESS_TROUBLE)

# Owner sentences (D52, the JARGON tests: "NewsBreak", "your ads", "the image" -- never campaign / ad set / CPC)
# the account's own step; the picture is the plan's -- its card shows it, and the "Ready to launch" card says which
# (`plans.image_step`, M1 Task 7). It never asks for money itself (M1 follow-up 2, W3): a step with nothing to connect
# says what the account waits for beside it -- its reason, `check_account`'s: funds, or NewsBreak getting it ready
CONNECT = "Nothing to connect: Admiry runs NewsBreak from your balance"
# a row whose mode is not `managed` (nothing sets one; an operator's hand could): funds do not help, so the reason names
# what NewsBreak needs instead of asking for money
ONLY_MANAGED = "NewsBreak runs only from your balance, in an account Admiry opens for your business"
# R11: the account waits at NewsBreak (made other than ACTIVE, or not made: the allowance, verification, credit) --
# Admiry's work, never the owner's, and never "funds" (the owner's word for their balance). One sentence for the row
# the launch found waiting (`queued`) and the one approved while it waits (`waiting_account`): the row's clause and
# the plan's badge, said once (`Words.account_wait`; M1 Task 7 fix round 1, item 2)
ACCOUNT_WAIT = "NewsBreak is getting your account ready; your ads start once it is"
FAR_POINT = "NewsBreak reaches people in US ZIP codes only; the address your ads centre on is too far from one"
TOO_LITTLE_COPY = (
    "Your NewsBreak ad needs at least one headline and one description that fit NewsBreak's limits; revise the ad "
    "text first"
)
# a picture that left Admiry's set (`_asset`): the serving ads keep the one they carry (final review I3), and any other
# -- uploaded earlier or not, or carried by ads that never served -- must still be in the set (M1 follow-up R2)
NO_IMAGE = "The image approved for your NewsBreak ad is no longer in Admiry's set; ask for a fresh plan and approve it"
NO_ZIPS = "NewsBreak cannot show ads in the ZIP codes around the address your ads centre on; tell me another address"
NO_LEAD_EVENT = "NewsBreak has no lead event for this account yet; an Admiry operator is setting it up"
REFUSED_ADDRESS = (
    "NewsBreak did not take the page address {host}; the ads keep their earlier page — use a different address or fix "
    "the page"
)
# NewsBreak shows the ads OFF though Admiry runs them (someone in Admiry's Organization, or NewsBreak itself, switched
# them off): the owner has no NewsBreak access and nothing to do there -- an operator is told (fix round 1, item 2)
PAUSED_IN_NEWSBREAK = "NewsBreak has paused your ads; an Admiry operator is looking into it"
# the plan's badge for that row (M1 Task 7 fix round 1, item 1): never "Live since"
PAUSED_BADGE = "Paused at NewsBreak"
# a row waiting for a picture (M1 Task 7 fix round 1, item 3): a plan funds NewsBreak only with one, so it waits only
# when the picture approved with the plan left Admiry's set before the launch -- Admiry's to replace, and the owner's to
# approve (D89) on a new version: nothing puts a new picture on the approved version's card (final review I3)
IMAGE_WAIT = (
    "NewsBreak is waiting for a new picture from Admiry; ask for a new version of your plan once Admiry has one, and "
    "approve it"
)
# A3 S15: the only `last_error` sentence the hourly read sets or clears
SYNC_NOTES = (PAUSED_IN_NEWSBREAK,)
# A row never carries the client's "try again" (fix round 1): NewsBreak busy or not answering is Admiry's to retry,
# as Google's adapter says it (its RETRYING), and Admiry's own spent time budget -- the approval's request launches
# within one (`ads_run.launch_now`) -- is no NewsBreak failure at all: the hourly job finishes from the missing step
RETRYING = "NewsBreak did not answer; Admiry retries within the hour"
STARTING = "NewsBreak is setting up your ads; Admiry finishes within the hour"
# R5's sanity check: NewsBreak shows more spent than the last cap Admiry set -- a unit or a spend mismatch to look at
SPEND_CHECK = "Admiry's spend figures for NewsBreak need a check; an Admiry operator will look into it"
# the same check stopping the hourly read's cap for a new account month (R22): no change of the owner's waits, but
# NewsBreak keeps last month's cap, which may stop the ads early -- the read's own note (`Words.month_note`, M1 Task 7)
MONTH_CHECK = (
    "This month's budget is not set at NewsBreak yet and your ads may stop early; an Admiry operator is checking the "
    "spend figures first"
)
# The token's trouble on a row (fix round 2): the client's ACCESS_TROUBLE ("... try again later") is for its callers; a
# row says Admiry is at it -- ads not running yet start once it is fixed, a change to running ads goes out then
ACCESS_FIXING = "Admiry is fixing its access to NewsBreak; your ads start once it is fixed"
ACCESS_FIXING_CHANGE = "Admiry is fixing its access to NewsBreak; your latest change goes out once it is fixed"
# NewsBreak's own words the hourly read goes by (Get Ads, Get Campaigns, Get Ad Sets): an object's `status`, an ad's
# review (`auditStatus`), and the campaign's and the ad set's online status that is no trouble -- READY is an ad set
# about to start; any other (WARNING, INACTIVE, COMPLETED, DELETED) holds the ads back
ON = "ON"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CAMPAIGN_OK = ("ACTIVE",)
AD_SET_OK = ("ACTIVE", "READY")
DELETED = "DELETED"  # an object NewsBreak still lists, deleted (its online status): no ad NewsBreak has any more
DISAPPROVAL = "disapproval"  # the `channel_issue.kind` of an ad NewsBreak turned down (R14)
# what the hourly read tells an operator of, once per occurrence (`_tell_once`, fix round 1, item 2): NewsBreak shows
# the ads OFF though Admiry runs them, or ON though Admiry paused them
PAUSED_THERE = "paused_there"
RUNNING_THERE = "running_there"
EXTEND_DAYS = 30  # R23: an ad set's end date this close or closer is moved a year out (END_DAYS) by the read
# 5a's one list of ads on their way out, split since (fix round 1, item 1) into `replaced` (a finished change's old ads,
# kept ON until every new ad has passed review) and `leftover` (an attempt's that will not finish, OFF at once): a row
# written before still carries it, read as `leftover` -- it cannot say which a finished change put there
LEGACY_OUTGOING = "retiring"
# NewsBreak's own limit between two requests (Rate Limits: Basic 10 QPS): all a call that stops spend waits for
NEWSBREAK_PACE_S = 0.1
# NewsBreak's own delivery past its cap at a fully spent month, no mismatch (fix round 2): up to the larger of this and
# 1 % of the last cap Admiry set (`cap_overshoot_cents`)
CAP_OVERSHOOT_CENTS = 500


def cap_overshoot_cents(last_cap_cents: int) -> int:
    """How far NewsBreak's lifetime total may run past the last cap Admiry set and still be NewsBreak's own delivery
    overshoot rather than a unit or a spend mismatch: the larger of CAP_OVERSHOOT_CENTS and 1 % of that cap."""
    return max(CAP_OVERSHOOT_CENTS, last_cap_cents // 100)


WORDS = Words(
    # a pause NewsBreak has not taken -- not sent yet, or NewsBreak shows the ads ON again (the read's repause): the
    # hourly step sends it itself, so the line promises no operator's hand (M1 Task 7)
    pause_owed="Your ads may still show on NewsBreak; Admiry is pausing them there",
    refused_change="NewsBreak did not take all of your latest change; approve a changed version",
    checked="checked your NewsBreak ads without sending them (a test run: nothing is shown or spent)",
    checked_status="Admiry checked your ads and found nothing to fix; they are not running yet",
    checked_badge="Checked; turns on once execution is live",
    held_back="live, but NewsBreak is holding your ads back",
    repaused="finished pausing your ads in NewsBreak",
    no_fee=MANAGED_NO_FEE.format(label=registry.label(CHANNEL)),  # every managed channel's (M1 follow-up 2, W2)
    refused_address=REFUSED_ADDRESS,
    # no `paused_elsewhere`: on a channel Admiry runs, NewsBreak's own pause is no owner's hand that Admiry's pauses
    # must leave alone (M0's H33) -- stopping spend is always allowed (fix round 1, item 2)
    retrying=RETRYING,
    account_wait=ACCOUNT_WAIT,
    paused_there=PAUSED_IN_NEWSBREAK,
    paused_badge=PAUSED_BADGE,
    month_note=MONTH_CHECK,
    image_wait=IMAGE_WAIT,
)
# when each business's last request to NewsBreak started (`time.monotonic`), for the whole process: one request a
# second per business across steps and clients -- a launch, its enable, a pause right after
_LAST_CALL: dict[str, float] = {}
_LAST_LOCK = threading.Lock()


def wait(seconds: float) -> None:
    """The pause before a business's next request (one request a second per business at most). Tests replace it -- the
    `nbapi` fixture keeps what it was asked in the fake's `waits` -- so none sleeps."""
    time.sleep(seconds)


def platform_error(e: newsbreak.NewsBreakError, *, running: bool = False) -> PlatformError:
    """R1 (as amended in fix rounds 1 and 2): a NewsBreak failure as the lifecycle takes it, a row's sentence that never
    says "try again". NewsBreak busy or not answering is RETRYING (Admiry retries); the token's trouble an operator
    fixes is ACCESS_FIXING -- ACCESS_FIXING_CHANGE for a change to `running` ads -- retried too (keyed on the sentence:
    the Org-Admin cap failure carries no code); a request NewsBreak refused keeps its own sentence ("an Admiry
    operator will look into it") and is not retried."""
    if e.owner in (newsbreak.BUSY, newsbreak.UNREACHABLE):
        return PlatformError(RETRYING, retryable=True)
    if e.owner == newsbreak.ACCESS_TROUBLE:
        return PlatformError(ACCESS_FIXING_CHANGE if running else ACCESS_FIXING, retryable=True)
    return PlatformError(e.owner, retryable=e.owner in RETRYABLE)


class _Deadline:
    """A client's time budget (`client(budget_s=...)`, the approval's own launch): each request gets at most what is left
    as its timeout, and one with nothing left is never sent."""

    def __init__(self, budget_s: float) -> None:
        self.at = time.monotonic() + budget_s

    def left(self) -> float:
        return self.at - time.monotonic()

    def __call__(self, request: httpx.Request) -> None:
        left = self.left()
        if left <= 0:
            raise httpx.TimeoutException("the launch's time budget is spent", request=request)
        request.extensions["timeout"] = httpx.Timeout(min(left, newsbreak.TIMEOUT_S)).as_dict()


def _time_left(http: httpx.Client | None) -> float | None:
    """What is left of `http`'s time budget; None for a client without one (the hourly jobs')."""
    if http is None:
        return None
    return next((hook.left() for hook in http.event_hooks["request"] if isinstance(hook, _Deadline)), None)


class _Calls:
    """One step's calls to NewsBreak for one business: `self(fn, *args)` is the client module's
    `fn(settings, *args, http=http)`, sent a second after the business's previous request started at the earliest
    (`_LAST_CALL`, whatever step sent it). A request that would not fit in what is left of the client's time budget
    -- the pause, then CALL_MIN_S of its own -- is not sent: the step stops on STARTING, retried, and the hourly job
    resumes from there. `stop(fn, *args)` is a call that stops spend -- a pause, an ad OFF (fix round 2): always sent,
    with whatever time is left, after only NewsBreak's own pace; if the budget cuts it or NewsBreak fails it, the
    caller keeps it owed as for any failure."""

    def __init__(self, settings: Settings, http: httpx.Client | None, biz: str) -> None:
        self.settings = settings
        self.http = http
        self.biz = biz

    def __call__(self, fn, *args, **kwargs):
        return self._send(fn, args, kwargs, pace=PACE_S, floor=CALL_MIN_S)

    def stop(self, fn, *args, **kwargs):
        return self._send(fn, args, kwargs, pace=NEWSBREAK_PACE_S, floor=None)

    def _send(self, fn, args: tuple, kwargs: dict, *, pace: float, floor: float | None):
        with _LAST_LOCK:
            last = _LAST_CALL.get(self.biz)
        pause = 0.0 if last is None else max(0.0, pace - (time.monotonic() - last))
        left = _time_left(self.http)
        if floor is not None and left is not None and left - pause < floor:
            raise PlatformError(STARTING, retryable=True)
        if pause > 0:
            wait(pause)
        with _LAST_LOCK:
            _LAST_CALL[self.biz] = time.monotonic()  # the pace runs from each request's start, not its answer
        return fn(self.settings, *args, http=self.http, **kwargs)


# ---------------------------------------------------------------- small helpers


def _ours(name: object, biz: str) -> bool:
    """An object of this business's, by the business id every Admiry name ends with (`build.campaign_name`): found even
    when the business's name changed since it was made."""
    return isinstance(name, str) and (
        name == f"Admiry · {biz}" or (name.startswith("Admiry · ") and name.endswith(f" · {biz}"))
    )


def _named(rows: list[dict], biz: str, **where: str) -> dict | None:
    """The first of `rows` (a getList's) named for this business, among those whose `where` fields match."""
    for row in rows:
        if _ours(row.get("name"), biz) and all(str(row.get(k)) == str(v) for k, v in where.items()) and row.get("id"):
            return row
    return None


def _cents(monthly_usd: object) -> int:
    return round(Decimal(str(monthly_usd)) * 100)


def _recent(at: object) -> bool:
    try:
        return ads_run.now() - datetime.fromisoformat(str(at)) < RECHECK
    except (TypeError, ValueError):
        return False


def _ad_ids(resources: dict) -> list[str]:
    """The running ads' ids, in the order of their slots."""
    ads = resources.get("ads") or {}
    return [
        str(ads[slot]) for slot in sorted(ads, key=lambda s: (not str(s).isdigit(), int(s) if str(s).isdigit() else 0))
    ]


def _in_flight(resources: dict) -> list[str]:
    """The ads of an update still in flight (`updating`, made and perhaps turned ON already): they serve too (R21)."""
    return [str(ad) for ad in ((resources.get("updating") or {}).get("ads") or {}).values()]


def _replaced(resources: dict) -> list[str]:
    """The old ads a finished change of words replaced (`resources.replaced`): ON beside the new ones until every new
    ad has passed NewsBreak's review, whatever their own review says (R15; fix round 1, item 1)."""
    return [str(ad) for ad in resources.get("replaced") or []]


def _leftover(resources: dict) -> list[str]:
    """The ads of an attempt that will not finish (`resources.leftover`): a refused or superseded change's, a failed
    launch's, a gone version's -- OFF at once, whatever their own review says (R25; fix round 1, item 1). A row 5a's
    code wrote keeps them under LEGACY_OUTGOING, read here as left over."""
    found = [*(resources.get("leftover") or []), *(resources.get(LEGACY_OUTGOING) or [])]
    return list(dict.fromkeys(str(ad) for ad in found))


def _keep_leftover(conn: psycopg.Connection, biz: str, leftover: list[str]) -> None:
    """`leftover` on the row (dropped when empty), written before the legacy key goes: an ad listed twice for a moment,
    never nowhere."""
    if leftover:
        _remember(conn, biz, {"leftover": leftover})
    else:
        ads_run.forget(conn, biz, CHANNEL, "leftover")
    ads_run.forget(conn, biz, CHANNEL, LEGACY_OUTGOING)


def _month(zone_name: object = None) -> str:
    """This month of the account's own calendar, "YYYY-MM" (`resources.time_zone`; NewsBreak's own zone, TIME_ZONE,
    while unknown): the month a recorded cap base is for (`resources.cap_month`, R16/R22)."""
    return ads_run.now().astimezone(ads_run.zone(zone_name or TIME_ZONE)).strftime("%Y-%m")


def _found(rows: list[dict], ident: object) -> dict | None:
    """The object of a getList's `rows` with this id; None when NewsBreak lists none (deleted in Ads Manager)."""
    return next((r for r in rows if ident and str(r.get("id")) == str(ident)), None)


def _whole(value: object) -> int | None:
    """A whole number NewsBreak gave as a number or a numeric string (Unix seconds, cents); None for anything else."""
    try:
        return int(Decimal(str(value)))
    except (ArithmeticError, ValueError):  # decimal.InvalidOperation is an ArithmeticError
        return None


def _review(conn: psycopg.Connection, settings: Settings, biz: str, serving: list[dict]) -> bool:
    """R14 (Review Focus 4): the ads that serve, as NewsBreak listed them, against the business's open NewsBreak
    disapprovals (`channel_issue`): each sentence NewsBreak gives for an ad it turned down (`statusTxt`, verbatim but
    for the token, cut out by the client's own `scrub`; its own word REJECTED when it gives none) is one open row,
    written the first read it appears; an open row whose sentence no serving ad carries any more (the review cleared,
    or the ad left) is resolved. True while NewsBreak turns any of them down."""
    said = [ad.get("statusTxt") for ad in serving if ad.get("auditStatus") == REJECTED]
    words = [newsbreak.scrub(settings, text) if isinstance(text, str) and text.strip() else None for text in said]
    texts = list(dict.fromkeys(text or REJECTED for text in words))
    found = conn.execute(
        "SELECT id, platform_text FROM channel_issue WHERE business_id = %s AND channel = %s AND kind = %s "
        "AND resolved_at IS NULL",
        (biz, CHANNEL, DISAPPROVAL),
    ).fetchall()
    standing = {r["platform_text"] for r in found}
    for text in texts:
        if text not in standing:
            conn.execute(
                "INSERT INTO channel_issue (business_id, channel, kind, platform_text) VALUES (%s, %s, %s, %s)",
                (biz, CHANNEL, DISAPPROVAL, text),
            )
    cleared = [r["id"] for r in found if r["platform_text"] not in texts]
    if cleared:
        conn.execute("UPDATE channel_issue SET resolved_at = now() WHERE id = ANY(%s)", (cleared,))
    return bool(texts)


_AS_IS = object()  # `_serving`'s note for a row whose sentence the read leaves exactly as it is


def _serving(row: dict, campaign: dict | None, ad_set: dict | None, turned_down: bool) -> tuple[str, object, list[str]]:
    """(the row's state, the read's own note -- or _AS_IS --, NewsBreak's words for what holds the ads back) from the
    campaign and the ad set as NewsBreak listed them (R14). A row whose pause is owed, or still `launching` (its enable
    turns things on), is the steps' alone; one NewsBreak lists neither object of (deleted in Ads Manager) reads as it
    did -- as Google's does without a status row. A paused row stays paused (whether NewsBreak serves it anyway is
    `pull_issues`'s re-pause); a campaign or ad set OFF that Admiry did not pause is NewsBreak's own pause or a hand in
    Admiry's Organization (PAUSED_IN_NEWSBREAK); an online status other than ACTIVE (READY, too, for an ad set about
    to start) or an ad turned down is `limited`; else `live`."""
    state = row["state"]
    if (row.get("resources") or {}).get("pause_owed") or state == "launching":
        return state, _AS_IS, []
    if state == "paused":
        return "paused", None, []
    if campaign is None or ad_set is None:
        return state, _AS_IS, []
    if campaign.get("status") != ON or ad_set.get("status") != ON:
        return "limited", PAUSED_IN_NEWSBREAK, []
    words = [
        str(word)
        for word, fine in ((campaign.get("onlineStatus"), CAMPAIGN_OK), (ad_set.get("onlineStatus"), AD_SET_OK))
        if word and word not in fine
    ]
    held = list(dict.fromkeys(words))
    return ("limited" if held or turned_down else "live"), None, held


def _turn_off(
    calls: _Calls, biz: str, ads: list[str], listed: dict[str, dict], serving: set[str], *, now: bool
) -> tuple[list[str], list[str]]:
    """(the ads still on their way out, the ads turned OFF by this read) of `ads`, as NewsBreak listed them: one gone
    (or listed as DELETED), OFF already, or serving after all leaves the list; an ON one is turned OFF when `now`,
    else kept; one whose OFF NewsBreak does not take is kept for the next read."""
    kept: list[str] = []
    off: list[str] = []
    for ad in ads:
        seen = listed.get(ad)
        if seen is None or seen.get("onlineStatus") == DELETED or seen.get("status") != ON or ad in serving:
            continue
        if not now:
            kept.append(ad)
            continue
        try:
            calls.stop(newsbreak.ad_status, ad, False)
        except newsbreak.NewsBreakError as e:
            log.warning("newsbreak: business %s's ad %s was not turned off (%s); it stays listed", biz, ad, e.owner)
            kept.append(ad)
            continue
        off.append(ad)
    return kept, off


def _carry_replaced(calls: _Calls, biz: str, ads: list[str], target: str) -> None:
    """A finished change's old ads (`replaced`) moved to the page's new address `target`, after the ads that serve took
    it (fix round 2, item 4): ON beside the new ones until those pass NewsBreak's review (R15), they follow the page --
    no click of theirs goes to the old address, perhaps a page that is gone. NewsBreak busy or not answering is a
    retryable PlatformError, as for any move; any other refusal of one (an ad deleted by hand in Ads Manager) is
    logged and left -- the address went through for the running ads, and the hourly read turns the old ones off
    within the day: an ad on its way out never makes the address look refused (probe R2b)."""
    for ad in ads:
        try:
            calls(newsbreak.update_ad, ad, {"creative": {"clickThroughUrl": target}})
        except newsbreak.NewsBreakError as e:
            if e.owner in RETRYABLE:
                raise platform_error(e, running=True) from None
            log.warning(
                "newsbreak: business %s's old ad %s was not moved to the new page (%s); it goes off once the new "
                "ads pass review",
                biz,
                ad,
                e.owner,
            )


def _set_aside(conn: psycopg.Connection, biz: str, refused: list[tuple[str, newsbreak.NewsBreakError]]) -> None:
    """C1: ads NewsBreak would not turn on while others of the set did -- each logged with NewsBreak's refusal -- leave
    the running set for `leftover`, listed there before they leave `ads` (R28's order: listed twice for a moment, never
    nowhere); the hourly read turns any still ON off and drops each once NewsBreak shows it OFF or gone (R25)."""
    for ad, e in refused:
        log.warning("newsbreak: business %s's ad %s was not turned on (%s); the others run", biz, ad, e.owner)
    out = {ad for ad, _e in refused}
    resources = (ads_run.get(conn, biz, CHANNEL) or {}).get("resources") or {}
    _keep_leftover(conn, biz, list(dict.fromkeys([*_leftover(resources), *out])))
    running = {slot: ad for slot, ad in (resources.get("ads") or {}).items() if str(ad) not in out}
    _remember(conn, biz, {"ads": running})


def _pause_stands(conn: psycopg.Connection, biz: str) -> bool:
    """Whether a pause stands on the business's NewsBreak row as it is now -- read again, never the row a step began
    with (A3 S17: a pause never waits for the step's lock): a row not live or limited, a pause reason of Admiry's
    (`paused_reason`), the owner's (`owner_paused`) or a pause still owed to NewsBreak (`pause_owed`)."""
    row = ads_run.get(conn, biz, CHANNEL)
    if row is None:
        return True
    resources = row.get("resources") or {}
    return (
        row["state"] not in ("live", "limited")
        or bool(row.get("paused_reason"))
        or bool(resources.get("owner_paused"))
        or bool(resources.get("pause_owed"))
    )


def _audit(conn: psycopg.Connection, biz: str, tool: str, result: dict) -> None:
    """An audit row for a money-relevant write of the hourly read's own (fix round 1, item 7): actor job:ads-sync, the
    owner's sentence from `tools_account.VERBS[tool]`."""
    audit.record_job(conn, job="ads-sync", tool=tool, business_id=biz, args={"channel": CHANNEL}, result=result)


def _back_on_note(went: list[str], *, every: bool) -> str:
    """The owner's words for one switch back on (`_back_on`, M1 Task 7, A5): what went through -- the ads (the
    campaign), their daily budget (the ad set) or both -- and, when not every switch that was off did, the one that did
    not, by name (fix round 1, M10: of two switches, the other one)."""
    if went == ["campaign", "ad_set"]:
        what = "your ads on NewsBreak and their daily budget"
    elif went == ["campaign"]:
        what = "your ads on NewsBreak"
    else:
        what = "the daily budget of your ads on NewsBreak"
    note = f"turned {what} back on for the new month"
    if every:
        return note
    rest = "their daily budget" if went == ["campaign"] else "the ads themselves"
    return f"{note}; {rest} did not come back on"


def _notice(kind: str, biz: str, row: dict) -> tuple[str, list[str]]:
    """The operator mail's subject and paragraphs for what the read found standing (`_tell_once`)."""
    resources = row["resources"]
    where = (
        f"ad account {resources.get('ad_account_id')}, campaign {resources.get('campaign')}, "
        f"ad set {resources.get('ad_set_id')}"
    )
    if kind == PAUSED_THERE:
        return (
            f"[Admiry] NewsBreak: the ads of business {biz} are switched off at NewsBreak",
            [
                (
                    f"NewsBreak shows the ads of business {biz} ({where}) switched OFF -- the campaign or its ad "
                    f'set -- while Admiry runs them. The owner reads: "{PAUSED_IN_NEWSBREAK}".'
                ),
                (
                    "Check in NewsBreak Ads Manager who switched them off and why: someone in Admiry's "
                    "Organization, an ad review, or NewsBreak at the account's spending cap. Admiry switches them back "
                    "on by itself only once it sets a new month's cap; otherwise switch them on there once the reason "
                    "is cleared, and the next hourly read shows them live again."
                ),
            ],
        )
    why = row.get("paused_reason") or ("the owner's pause" if resources.get("owner_paused") else "a pause")
    return (
        f"[Admiry] NewsBreak: the paused ads of business {biz} run at NewsBreak",
        [
            (
                f"NewsBreak shows the campaign of business {biz} ({where}) ON while its ads are paused in Admiry "
                f"({why}). Admiry sends the pause again within the hour."
            ),
            (
                "Check in NewsBreak Ads Manager who switched it on: only someone in Admiry's Organization, or "
                "NewsBreak, can."
            ),
        ],
    )


def _lead_event(events: list[dict]) -> dict | None:
    """The account's event Admiry's leads go back as: made by hand under ADMIRY_LEAD_EVENT (its name or its type)."""
    wanted = newsbreak.ADMIRY_LEAD_EVENT
    return next((e for e in events if wanted in (e.get("eventType"), e.get("name")) and e.get("id")), None)


def _fits(content: dict) -> bool:
    """R3: an ad's words within NewsBreak's widths, measured once more before anything is created."""
    limits = (
        ("headline", newsbreak.HEADLINE_MAX),
        ("description", newsbreak.DESCRIPTION_MAX),
        ("brandName", newsbreak.BRAND_NAME_MAX),
        ("callToAction", newsbreak.CALL_TO_ACTION_MAX),
    )
    return all(0 < nb_build.width(str(content.get(field) or "")) <= limit for field, limit in limits)


def _money(payload: dict) -> dict:
    """What an update sends first, every time (S16, final review I1): the version's whole money and area, as its build
    has them -- the daily budget, the bid and the whole targeting (R4: an omitted one resets to everyone)."""
    ad_set = payload["ad_set"]
    return {"budget": ad_set["budget"], "bidRate": ad_set["bidRate"], "targeting": ad_set["targeting"]}


def _picture(plan: dict) -> str | None:
    """The licensed picture the version shows with its ads (P7): the one it pinned when it was made (`plans.pinned`),
    approved with it -- whether or not the set still has it, which matters for any picture but the one the serving ads
    carry (`_asset`; final review I3 as the M1 follow-up R2 narrowed it): running ads keep the picture they carry. A
    launch is never built on one that left the set: `ads_run.image_ready` keeps such a row in `waiting_creative`
    (IMAGE_WAIT) -- one that left after the approval included (M1 Task 7 fix round 1, item 3). None for a version that
    shows none."""
    return plans.pinned(plan.get("sections") or {})


def _carried(conn: psycopg.Connection, biz: str, running: dict) -> str | None:
    """The picture the serving ads carry (M1 follow-up R2): the running version's (`running`, whose build made them)
    once the row's ads went live (`launched_at`, the lifecycle's record that they did) -- None for ads that never
    served, such as a fresh launch whose enable NewsBreak refused (C1) started again by a new approval: their picture
    is still a launch's, which must be in Admiry's set (D89)."""
    return _picture(running) if (ads_run.get(conn, biz, CHANNEL) or {}).get("launched_at") else None


def _remember(conn: psycopg.Connection, biz: str, values: dict) -> None:
    ads_run.remember(conn, biz, CHANNEL, values)


def _stored(conn: psycopg.Connection, biz: str) -> tuple[str, str, dict]:
    """The business's NewsBreak account row: (mode, status, a copy of `external`)."""
    row = registry.account(conn, biz, CHANNEL)
    return (row.mode, row.status, dict(row.external)) if row else ("managed", "none", {})


def _keep_account(conn: psycopg.Connection, biz: str, *, status: str | None = None, **changes: object) -> None:
    """`channel_account.external` merged with `changes` (None drops a key), and `status` when given: the row is
    replaced whole by `registry.save_account`, so it is read first."""
    mode, current, external = _stored(conn, biz)
    for key, value in changes.items():
        if value is None:
            external.pop(key, None)
        else:
            external[key] = value
    registry.save_account(conn, biz, CHANNEL, mode=mode, status=status or current, external=external)


def _tell_operator(settings: Settings, *, key: str, subject: str, paragraphs: list[str]) -> bool:
    """One notice to the operator (AGENT_OPS_EMAIL): True once it went out. Without the address it is skipped with a
    log line, and the caller tries again on its next step; `key` makes a retry within a day the same mail."""
    sent = mail_out.send_plain(
        settings,
        to=settings.ops_email or "",
        subject=subject,
        paragraphs=paragraphs,
        idempotency_key=key,
        gate=None if settings.ops_email else "AGENT_OPS_EMAIL unset",
    )
    if not sent and settings.ops_email:
        log.warning("newsbreak: the operator notice %s was not sent", key)
    return sent


# ---------------------------------------------------------------- the adapter


class NewsBreakAdapter:
    channel = CHANNEL
    words = WORDS

    def capabilities(self) -> Capabilities:
        return Capabilities(
            create=True,
            update=True,
            pause=True,
            metrics=True,
            conversions=True,
            leads=False,
            modes={"managed"},
            min_monthly_usd=300,  # NewsBreak's $10-a-day ad-set minimum, per business (D87)
            geo="zip",
            lead_formats={"call", "form"},
            needs_image=True,
            validate="dry",
            page_rules=("terms", "contact"),  # the landing check's rules NewsBreak requires (D89, Task 4)
            follows_page=True,  # the ads carry the page (`clickThroughUrl`, src=nb) and move with it (R8)
        )

    def connect_steps(self, account: Account | None, mode: str) -> list[Step]:
        return [Step(CONNECT)]

    def check_account(self, conn: psycopg.Connection, settings: Settings, account: Account | None) -> AccountStatus:
        """From the database alone (see the module docstring): "Add funds to start NewsBreak" without a balance,
        ONLY_MANAGED on a row in another mode, ACCOUNT_WAIT for the hour after the launch found the account waiting
        at NewsBreak (R11) -- then ready again, so the next step looks once more -- else ready: the ad account itself is
        the launch's to make. `account_public` is NewsBreak's id for the account once there is one (D88: the owner's
        statement shows it). The business's `channel_account(newsbreak)` row -- `managed`, status `none` -- is opened
        the first time this is asked, so the money paths (`registry.managed_channels`) see the channel from then on;
        an existing row is never rewritten here."""
        no_funds = ads_run.NO_FUNDS.format(label=registry.label(CHANNEL))
        if account is None:
            return AccountStatus(ready=False, status="none", reason=no_funds, account_public=None)
        biz = account.business_id
        row = registry.account(conn, biz, CHANNEL)
        if row is None:
            registry.save_account(conn, biz, CHANNEL, mode="managed", status="none", external={})
            row = registry.account(conn, biz, CHANNEL)
        if row.mode != "managed":
            return AccountStatus(ready=False, status=row.status, reason=ONLY_MANAGED, account_public=None)
        public = row.external.get("ad_account_id")
        if wallet.balance_cents(conn, biz) <= 0:
            return AccountStatus(ready=False, status=row.status, reason=no_funds, account_public=public)
        said = row.external.get("account_status")
        if said not in (None, ACTIVE) and _recent(row.external.get("account_checked_at")):
            return AccountStatus(ready=False, status=row.status, reason=ACCOUNT_WAIT, account_public=public)
        return AccountStatus(ready=True, status=row.status, reason=None, account_public=public)

    def precheck(
        self, conn: psycopg.Connection, settings: Settings, business_id: str, plan: dict, knowledge: dict
    ) -> None:
        """NewsBreak's own rule beyond the lifecycle's (the zip area's point and radius, the page's terms and contact
        through `Capabilities.page_rules`, the image through `image_ready`): R9, a point NewsBreak can reach -- its
        nearest US ZIP centroid no farther than the plan's radius plus FAR_SLACK_MILES (a point abroad, or a latitude
        and longitude swapped, lands here). A ToolError here parks NewsBreak's row with its sentence beside a channel
        that can launch (R17, D77: `ads_run.precheck`'s `park`), and refuses only an approval nothing else in could
        launch (`ads_run.precheck_approval`). Never the copy or the image (Corrections 5, 11): those are the build's and
        `image_ready`'s."""
        if not ads_run.has_point(knowledge):
            return  # NO_LOCATION: the lifecycle's rule for a `zip` channel
        geo_ = (((plan.get("sections") or {}).get("consultation") or {}).get("geo")) or {}
        radius = geo_.get("radius_miles")
        if not radius:
            return  # NO_RADIUS: the lifecycle's too
        listing = knowledge["listing"]
        try:
            _zip, away = geo.nearest(float(listing["lat"]), float(listing["lng"]))
        except (TypeError, ValueError):
            raise ToolError(FAR_POINT) from None
        if away > float(radius) + FAR_SLACK_MILES:
            raise ToolError(FAR_POINT)

    def build(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        business_id: str,
        plan: dict,
        knowledge: dict,
        account: Account,
        *,
        http: httpx.Client | None = None,
    ) -> Build:
        """What a launch or an update sends, from the database and the plan (nothing reaches NewsBreak here): the ZIP
        codes inside the plan's radius around the listing's point and their NewsBreak ids (`geo`, the unmapped ones
        kept beside them), the ad account's create body, the campaign, ad set and ad bodies (`build.py`, every create
        OFF; the ads' picture address is NewsBreak's own, known once the launch uploads it), the linted copy they come
        from (R3: `build.ad_copy`, never the plan's raw row, measured once more), the version's picture file
        (`_picture`: whether or not the set still has it -- `_asset` asks for it there unless the serving ads carry it,
        final review I3 and the M1 follow-up R2), the click URL (`build.page_url`, R7) and the month's amount for the
        cap. Not buildable: no ad words left or words over NewsBreak's widths (TOO_LITTLE_COPY) or a business name
        NewsBreak cannot show (`BrandNameError`'s sentence) -- a PlatformError that is not retried: a launch fails, an
        update is refused; no point, radius, page or NewsBreak ZIP -- a ToolError the row waits on."""
        biz = business_id
        entry = nb_build.entry(plan)
        page = landing.current(conn, biz)
        if page is None or not page.get("url"):
            raise ToolError(ads_run.NO_PAGE)
        consultation = plan["sections"].get("consultation") or {}
        radius = (consultation.get("geo") or {}).get("radius_miles")
        if not ads_run.has_point(knowledge):
            raise ToolError(ads_run.NO_LOCATION)
        if not radius:
            raise ToolError(ads_run.NO_RADIUS)
        listing = knowledge["listing"]
        try:
            zips = geo.zips_within(float(listing["lat"]), float(listing["lng"]), float(radius))
        except (TypeError, ValueError):
            raise ToolError(FAR_POINT) from None
        ids, unmapped = geo.newsbreak_ids(zips)
        if not ids:
            raise ToolError(NO_ZIPS)
        try:
            shown = nb_build.brand_name(knowledge.get("name"))
        except nb_build.BrandNameError as e:
            raise PlatformError(str(e), retryable=False) from None
        headlines, descriptions, _notes = nb_build.ad_copy(plan)
        cta = nb_build.call_to_action(plan)
        click = nb_build.page_url(page["url"])
        ads = nb_build.ad_bodies(
            ad_set_id=None,
            brand=shown,
            headlines=headlines,
            descriptions=descriptions,
            cta=cta,
            asset_url=None,
            logo_url=None,
            click_url=click,
        )
        if not ads or not all(_fits(ad["creative"]) for ad in ads):
            raise PlatformError(TOO_LITTLE_COPY, retryable=False)
        image = _picture(plan)
        name = nb_build.campaign_name(plans.name_for(knowledge), biz)
        category = (consultation.get("business") or {}).get("category") or knowledge.get("category")
        daily = nb_build.daily_cents(entry["monthly_usd"])
        start = int(ads_run.now().timestamp())
        payload = {
            "account": {
                "name": name,
                "company": " ".join(str(knowledge.get("name") or "").split()),
                "industry": newsbreak.industry(category),
            },
            "campaign": nb_build.campaign_body(name=name),
            "ad_set": nb_build.ad_set_body(
                campaign_id=None,
                name=name,
                daily_cents=daily,
                cpc_cents=nb_build.cpc_cents(plan["sections"].get("estimate") or {}),
                location_ids=ids,
                language=nb_build.LANGUAGE,
                start=start,
                end=start + END_DAYS * 86_400,
            ),
            "ads": ads,
            "copy": {"headlines": headlines, "descriptions": descriptions, "cta": cta, "brand": shown},
            "image": image,
            "location_ids": ids,
            "unmapped_zips": unmapped,
            "click_url": click,
            "cap_cents": _cents(entry["monthly_usd"]),
        }
        return Build(
            channel=CHANNEL,
            plan_id=plan["id"],
            payload=payload,
            summary={
                "daily_usd": daily / 100,
                "geo_text": f"{len(ids)} ZIP codes within {float(radius):g} miles",
                "ads": len(ads),
            },
            page_url=page["url"],
        )

    # ---------------------------------------------------------------- launch

    def launch(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        build: Build,
        existing: dict,
        *,
        http: httpx.Client | None = None,
        validate_only: bool,
    ) -> Resources:
        """`validate_only` (R12, D90): the bodies recorded, nothing called -- no client is made. Otherwise the module
        docstring's live launch, everything OFF. `Resources.plan_id` is the version the ad set was made from when an
        earlier attempt made it from another one (its ads are finished from that same version, and the lifecycle's
        enable applies the newer one first; a version that no longer builds is dropped instead, `_launch`);
        `Resources.seen` says what the enable need not set again (the ad set's budget as made, the cap and the month's
        amount it was set for)."""
        if validate_only:
            return Resources(ids={"validated": build.payload})
        try:
            return self._launch(conn, settings, account, build, existing, http)
        except newsbreak.NewsBreakError as e:
            error = platform_error(e)
            if not error.retryable:  # the launch fails: the next version approved builds its own ads (fix round 1)
                self._abandon(conn, account.business_id)
            raise error from None

    def _abandon(self, conn: psycopg.Connection, biz: str) -> None:
        """A launch NewsBreak refused fails its row, and only a newer approved version starts it again (Important 3, fix
        round 1): that version builds its own ads in the campaign and ad set already made -- never the failed one's
        again. The failed attempt's version mark (`creating_plan_id`) goes, and its ads -- all still OFF: a launch turns
        nothing on -- move to `leftover` (listed until the hourly read sees them OFF); the enable then sets the newer
        version's whole money on the ad set, which an attempt of another version made."""
        resources = (ads_run.get(conn, biz, CHANNEL) or {}).get("resources") or {}
        made = [str(ad) for ad in (resources.get("ads") or {}).values()]
        if made:
            _keep_leftover(conn, biz, list(dict.fromkeys([*_leftover(resources), *made])))
        ads_run.forget(conn, biz, CHANNEL, "ads")
        ads_run.forget(conn, biz, CHANNEL, "creating_plan_id")

    def _launch(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        build: Build,
        existing: dict,
        http: httpx.Client | None,
    ) -> Resources:
        biz, asked = account.business_id, build.plan_id
        if not settings.newsbreak_ready:  # no Organization to find or make the account in: the operator's to set
            raise newsbreak.NewsBreakError(newsbreak.ACCESS_TROUBLE, detail="NEWSBREAK_ORG_ID or the token is not set")
        calls = _Calls(settings, http, account.business_id)
        payload = build.payload
        account_id, fresh = self._ad_account(conn, calls, biz, payload)
        _remember(conn, biz, {"ad_account_id": account_id, "time_zone": TIME_ZONE})
        self._check_event(conn, calls, biz, account_id, payload["account"]["company"])
        assets = dict(existing.get("assets") or {})
        self._asset(conn, calls, biz, account_id, payload["image"], assets)
        campaign, fresh = self._campaign(conn, calls, biz, account_id, payload, existing.get("campaign_id"), fresh)
        ad_set, budget, fresh = self._ad_set(conn, calls, biz, account_id, campaign, build, existing, fresh)
        # the version whose money and area the ad set holds: this one's when made now, else the mark the attempt that
        # made it left (`creating_plan_id`; none after a failed launch, `_abandon`, or for one found with no mark)
        version = asked if fresh else existing.get("creating_plan_id")
        money = version is not None
        leftover, replaced = _leftover(existing), _replaced(existing)
        known = existing.get("ads") or {}
        if version and version != asked:  # an earlier attempt made the ad set from another version: its ads finish it
            older = self._older_build(conn, settings, account, version)
            if older is None:
                # that version is gone (fix round 2): its ads never run under this one -- the ones it made (all OFF: a
                # launch turns nothing on) go to `leftover`, this version makes all its own (and owns the attempt from
                # now on), and the enable sets its whole money on the ad set the gone one made. `leftover` is written
                # before `ads` is forgotten, as `_abandon` does (R28): a step stopped between the two leaves the ads
                # listed twice, never nowhere
                leftover = list(dict.fromkeys([*leftover, *(str(ad) for ad in known.values())]))
                known, version, money = {}, asked, False
                if leftover:
                    _keep_leftover(conn, biz, leftover)
                ads_run.forget(conn, biz, CHANNEL, "ads")
                _remember(conn, biz, {"creating_plan_id": asked})
            else:
                payload = older.payload
        url = self._asset(conn, calls, biz, account_id, payload["image"], assets)
        ads = self._ads(conn, calls, biz, account_id, ad_set, payload, url, known, fresh, {*leftover, *replaced})
        capped = self._cap(conn, calls, biz, account_id, payload["cap_cents"])
        ids = (
            {
                "ad_account_id": account_id,
                "campaign": campaign,
                "ad_set_id": ad_set,
                "ads": ads,
                "assets": assets,
                **capped,  # the cap, its base and the account month the base is for
                "time_zone": TIME_ZONE,
                "location_ids": payload["location_ids"],
                "click_url": payload["click_url"],
            }
            | ({"leftover": leftover} if leftover else {})
            | ({"replaced": replaced} if replaced else {})
        )
        # `money` False: the ad set may hold another version's money, and the enable sets the whole of it (budget, bid
        # and targeting); `budget` None: not known, and the enable sets that money too
        seen = {"budget": budget, "money": money, "cap_cents": capped["cap_cents"], "month_cents": payload["cap_cents"]}
        return Resources(ids=ids, plan_id=version if version and version != asked else None, seen=seen)

    def _ad_account(self, conn: psycopg.Connection, calls: _Calls, biz: str, payload: dict) -> tuple[str, bool]:
        """The business's ad account (P3) and whether this call made it: the one on file; else the Organization's by
        name (a create whose answer was lost), else a new one. One NewsBreak did not make ACTIVE, or refused to make,
        waits (`_waiting`, R11); a later look takes its status from NewsBreak's list when the list carries one."""
        settings = calls.settings
        _mode, _status, external = _stored(conn, biz)
        known, said = external.get("ad_account_id"), external.get("account_status")
        if known and said in (None, ACTIVE):
            return str(known), False
        listed = calls(newsbreak.ad_accounts, settings.newsbreak_org_id)
        if known:  # made earlier, not ACTIVE when NewsBreak last said
            said = next((a.get("status") for a in listed if str(a.get("id")) == str(known)), None) or said
            if said != ACTIVE:
                self._waiting(conn, settings, biz, payload, external, said, account=str(known))
            _keep_account(conn, biz, status="active", account_status=ACTIVE, account_checked_at=None)
            return str(known), False
        made, fresh = _named(listed, biz), False
        if made is None:
            body = payload["account"]
            try:
                made = calls(
                    newsbreak.create_ad_account,
                    org_id=settings.newsbreak_org_id,
                    name=body["name"],
                    company_name=body["company"],
                    industry=body["industry"],
                )
            except newsbreak.NewsBreakError as e:
                if e.owner in RETRYABLE:
                    raise
                self._waiting(conn, settings, biz, payload, external, CREATE_REFUSED, detail=e.detail)
            fresh = True
            if not made.get("id"):  # made, but the answer named nothing
                made, fresh = _named(calls(newsbreak.ad_accounts, settings.newsbreak_org_id), biz), False
                if made is None:
                    raise newsbreak.NewsBreakError(newsbreak.UNREACHABLE)
        account_id, said = str(made["id"]), made.get("status")
        usable = said in (None, ACTIVE)
        _keep_account(
            conn,
            biz,
            status="active" if usable else NEEDS_ATTENTION,
            ad_account_id=account_id,
            account_status=said,
            account_checked_at=None,
        )
        if not usable:
            self._waiting(conn, settings, biz, payload, external, said, account=account_id)
        return account_id, fresh

    def _waiting(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        biz: str,
        payload: dict,
        external: dict,
        said: str,
        *,
        account: str | None = None,
        detail: str | None = None,
    ) -> NoReturn:
        """R11: the account cannot carry ads yet -- NewsBreak made it in a state other than ACTIVE (`said`), or refused
        to make it (CREATE_REFUSED, with NewsBreak's own words in `detail`). Kept on the account row (`check_account`
        says ACCOUNT_WAIT for an hour, then the next step looks again), one operator mail per account and state, and
        the step ends on the same sentence -- retried, never the owner's to act on."""
        now = ads_run.now()
        mailed = dict(external.get("account_mailed") or {})
        company = payload["account"]["company"] or biz
        if said not in mailed:
            if said == CREATE_REFUSED:
                what = "NewsBreak refused to make its ad account" + (f" -- NewsBreak said: {detail}" if detail else ".")
            else:
                what = f"NewsBreak made its ad account {account} with the status {said}, so Admiry has built nothing there."
            paragraphs = [
                f"Business {company} ({biz}) is waiting to start its NewsBreak ads: {what}",
                (
                    "At NewsBreak: allocate credit from the Organization to the account, finish the Organization's "
                    "business verification, or ask adsupport@newsbreak.com to raise the Organization's ad-account "
                    "allowance."
                ),
                (
                    "Admiry tries again every hour and starts the ads by itself once NewsBreak makes the account, or "
                    "shows it ACTIVE in its list of the Organization's accounts. NewsBreak's list is not documented to "
                    "carry a status: once the account is fixed at NewsBreak, clear the wait on the box with "
                    f"`python -m agent.ops nb-account-ready {biz}`, and the next hourly step goes on."
                ),
            ]
            subject = f"[Admiry] NewsBreak: the ad account for {company} waits at NewsBreak ({said})"
            if _tell_operator(settings, key=f"nb-account-{biz}-{said}", subject=subject, paragraphs=paragraphs):
                mailed[said] = now.isoformat()
        _keep_account(
            conn,
            biz,
            status=NEEDS_ATTENTION,
            ad_account_id=account,
            account_status=said,
            account_checked_at=now.isoformat(),
            account_mailed=mailed or None,
        )
        raise PlatformError(ACCOUNT_WAIT, retryable=True)

    def _check_event(self, conn: psycopg.Connection, calls: _Calls, biz: str, account_id: str, company: str) -> None:
        """R10: the account's lead event (ADMIRY_LEAD_EVENT), made by hand -- no API makes one. Found: its id kept.
        Missing: the ads go on without it (website traffic needs no tracking) and the operator is told once per
        account. Asked only until one or the other is on file; NewsBreak's failure here never stops the launch."""
        _mode, _status, external = _stored(conn, biz)
        if external.get("event_id") or external.get("event_mail_sent_at"):
            return
        try:
            found = _lead_event(calls(newsbreak.events, account_id))
        except newsbreak.NewsBreakError as e:
            log.warning("newsbreak: business %s's lead event was not looked up (%s)", biz, e.owner)
            return
        if found is not None:
            _keep_account(conn, biz, event_id=str(found["id"]))
            return
        event = newsbreak.ADMIRY_LEAD_EVENT
        paragraphs = [
            (
                f"Business {company or biz} ({biz}) runs on NewsBreak in ad account {account_id}, which has no event "
                f"named {event}: its ads run without it, but the leads they bring cannot be sent back to NewsBreak."
            ),
            (
                "In NewsBreak Ads Manager, open that account's Tools -> Event Management -> Create/test web event -> "
                f"Postback, make the event {event} and run its test (NewsBreak saves an event only after a successful "
                "test). Admiry finds it by itself; nothing else is needed."
            ),
        ]
        subject = f"[Admiry] NewsBreak: make the lead event {event} for {company or biz}"
        if _tell_operator(calls.settings, key=f"nb-event-{account_id}", subject=subject, paragraphs=paragraphs):
            _keep_account(conn, biz, event_mail_sent_at=ads_run.now().isoformat())

    def _asset(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        account_id: str,
        file: str | None,
        assets: dict,
        *,
        carried: str | None = None,
    ) -> str:
        """The picture's address on NewsBreak's CDN for this account: uploaded once per account and file (P6), kept in
        `assets` and on the row. Admiry's set is skipped only for the picture the serving ads already carry (`carried`,
        which NewsBreak has: final review I3, narrowed as its D89 clause intends by the M1 follow-up R2) -- every other
        one must still be in the set, uploaded earlier or not; one that left it (or none at all) is NO_IMAGE, not
        retried: a launch fails, an update is refused."""
        if not file or (creative.find(file) is None and (file != carried or file not in assets)):
            raise PlatformError(NO_IMAGE, retryable=False)
        if file not in assets:
            assets[file] = calls(newsbreak.upload_asset, account_id, creative.ROOT / file)
            _remember(conn, biz, {"assets": assets})
        return assets[file]

    def _campaign(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        account_id: str,
        payload: dict,
        known: str | None,
        fresh_account: bool,
    ) -> tuple[str, bool]:
        """The campaign (it carries no plan, so any attempt's will do) and whether this call made it: the one an earlier
        attempt made, else the account's by name (none in an account this call made), else a new one, OFF."""
        if known:
            return str(known), False
        if not fresh_account:
            found = _named(calls(newsbreak.campaigns, account_id), biz)
            if found is not None:
                _remember(conn, biz, {"campaign_id": str(found["id"])})
                return str(found["id"]), False
        made = calls(newsbreak.create_campaign, {**payload["campaign"], "adAccountId": account_id})
        campaign = made.get("id") or (_named(calls(newsbreak.campaigns, account_id), biz) or {}).get("id")
        if not campaign:
            raise newsbreak.NewsBreakError(newsbreak.UNREACHABLE)
        _remember(conn, biz, {"campaign_id": str(campaign)})
        return str(campaign), True

    def _ad_set(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        account_id: str,
        campaign: str,
        build: Build,
        existing: dict,
        fresh_campaign: bool,
    ) -> tuple[str, int | None, bool]:
        """(the ad set, its budget when known, whether this call made it): the one an earlier attempt made, else the
        campaign's by name (none in a campaign this call made), else a new one, OFF -- the version it is made from
        written to the row first (`creating_plan_id`), so a later attempt knows which version its ads belong to."""
        if existing.get("ad_set_id"):
            return str(existing["ad_set_id"]), None, False
        if not fresh_campaign:
            found = _named(calls(newsbreak.ad_sets, account_id), biz, campaignId=campaign)
            if found is not None:
                _remember(conn, biz, {"ad_set_id": str(found["id"])})
                return str(found["id"]), found.get("budget"), False
        _remember(conn, biz, {"creating_plan_id": build.plan_id})
        made = calls(newsbreak.create_ad_set, {**build.payload["ad_set"], "campaignId": campaign})
        if not made.get("id"):
            raise newsbreak.NewsBreakError(newsbreak.UNREACHABLE)  # made, maybe: the next attempt finds it by name
        _remember(conn, biz, {"ad_set_id": str(made["id"])})
        return str(made["id"]), build.payload["ad_set"]["budget"], True

    def _ads(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        account_id: str,
        ad_set: str,
        payload: dict,
        url: str,
        known: dict,
        fresh_ad_set: bool,
        outgoing: set[str],
    ) -> dict:
        """The ads, {"<slot>": id}: each the payload's copy on the uploaded picture, OFF -- the ones an earlier attempt
        made kept, and, in an ad set this call did not make, the account's ads of the same name (a name carries what
        the ad shows) taken before any create -- never one on its way out (`leftover`, `replaced`: `outgoing`)."""
        ads = {str(slot): str(ad) for slot, ad in known.items()}
        bodies = nb_build.ad_bodies(
            ad_set_id=ad_set,
            brand=payload["copy"]["brand"],
            headlines=payload["copy"]["headlines"],
            descriptions=payload["copy"]["descriptions"],
            cta=payload["copy"]["cta"],
            asset_url=url,
            logo_url=None,
            click_url=payload["click_url"],
        )
        missing = [str(i) for i in range(len(bodies)) if str(i) not in ads]
        named: dict = {}
        if missing and not fresh_ad_set:
            named = {
                a["name"]: str(a["id"])
                for a in calls(newsbreak.ads, account_id)
                if a.get("name") and a.get("id") and str(a.get("adSetId")) == ad_set and str(a["id"]) not in outgoing
            }
        for slot in missing:
            body = bodies[int(slot)]
            ad = named.get(body["name"]) or calls(newsbreak.create_ad, body).get("id")
            if not ad:
                raise newsbreak.NewsBreakError(newsbreak.UNREACHABLE)
            ads[slot] = str(ad)
            _remember(conn, biz, {"ads": ads})
        return ads

    def _cap(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        account_id: str,
        month_cents: int,
        base: int | None = None,
        *,
        zone: object = TIME_ZONE,
        base_month: str | None = None,
    ) -> dict:
        """R5 (as amended in fix round 1): the account's spending cap -- NewsBreak's lifetime cap -- at a recorded base
        plus this month's amount: `base` is NewsBreak's lifetime total when it was recorded (`resources.cap_base_cents`:
        the launch's and the first read of each account month's (R16/R22), None here, take the total read now, for this
        month of the account's calendar in `zone` -- `resources.cap_month`), so an update in the middle of the month
        sets the same exact cap -- never one from `metric_daily`'s month-to-date, which lags NewsBreak's live total. A
        base recorded for another month (`base_month`, the row's `cap_month`: an update or a resume early in a month,
        before its first read) is never used: this month's is taken (5b fix round 1, item 3); a row without the mark
        keeps its base (it cannot say which month it is for: a cap that stops early, never one that lets more through).
        Never at or below the total (NewsBreak takes only a cap above it): a month lowered under what it spent gets the
        total plus a cent. An account Admiry never capped shows no total: nothing of Admiry's has run there, since every
        launch sets the cap before its enable.

        The sanity check: NewsBreak's total above the last cap Admiry set by more than its own delivery overshoot
        (`cap_overshoot_cents`, fix round 2) means figures that disagree (a unit, or spend that should not have
        happened) -- no cap is set from them (fail closed), the step waits on SPEND_CHECK, retried, and one operator
        mail goes until a cap is set again; within the overshoot the cap is set as usual. Returns what it kept on the
        row -- the cap, its base, and for a base taken now the month it is for -- for the caller's whole set of keys;
        the cap is kept on the account too."""
        total = calls(newsbreak.total_spend, account_id) or 0
        _mode, _status, external = _stored(conn, biz)
        last = external.get("spending_cap_cents")
        if last is not None and total > int(last) + cap_overshoot_cents(int(last)):  # beyond NewsBreak's own overshoot
            self._spend_check(conn, calls.settings, biz, account_id, total, int(last), external)
        this_month = _month(zone)
        fresh = base is None or (base_month is not None and base_month != this_month)
        base = total if fresh else int(base)
        cap = max(base + month_cents, total + 1)
        calls(newsbreak.set_spending_cap, account_id, cap)
        _keep_account(conn, biz, spending_cap_cents=cap, spend_check_mailed_at=None)
        kept = {"cap_cents": cap, "cap_base_cents": base} | ({"cap_month": this_month} if fresh else {})
        _remember(conn, biz, kept)
        return kept

    def _spend_check(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        biz: str,
        account_id: str,
        total: int,
        last: int,
        external: dict,
    ) -> NoReturn:
        """R5's sanity check failed: one operator mail (`spend_check_mailed_at` until a cap is set again) that says what
        to check and how to clear it -- spend only grows, so the figures never come to agree by themselves -- and the
        step stops on SPEND_CHECK, retried every hour until an operator clears it."""
        if not external.get("spend_check_mailed_at"):
            paragraphs = [
                (
                    f"Business {biz}: NewsBreak shows {total} cents spent in ad account {account_id}, more than its own "
                    f"overshoot above the {last}-cent spending cap Admiry last set there. Admiry sets no cap from "
                    "figures that disagree, so its changes to these ads wait."
                ),
                (
                    "Check two things: the unit of NewsBreak's figure (Admiry reads Get Account Spending Cap's "
                    "accountTotalSpend as cents) and the account's real spend in NewsBreak Ads Manager. If the unit is "
                    "not cents, every cap Admiry set there is wrong: leave the business as it is and report it."
                ),
                (
                    f"If the spend is real, clear it on the box with `python -m agent.ops nb-account-ready {biz}`: it "
                    "records NewsBreak's total as the last cap Admiry set (spending_cap_cents on the account row). The "
                    "next hourly step then sets the cap from the recorded base and carries on."
                ),
            ]
            subject = f"[Admiry] NewsBreak: the spend of business {biz} needs a check"
            if _tell_operator(settings, key=f"nb-spend-{account_id}-{last}", subject=subject, paragraphs=paragraphs):
                _keep_account(conn, biz, spend_check_mailed_at=ads_run.now().isoformat())
        raise PlatformError(SPEND_CHECK, retryable=True)

    def _older_build(
        self, conn: psycopg.Connection, settings: Settings, account: Account, plan_id: str
    ) -> Build | None:
        """The build of the version an earlier attempt made the ad set from; None when that version is gone, no longer
        builds, or its picture left the set (a launch never starts on one: `ads_run.image_ready`'s rule) -- the
        approved one then makes all its own ads, and the gone one's go to `leftover` (fix round 2)."""
        older = plans.get(conn, account.business_id, plan_id)
        latest = business.latest_knowledge(conn, account.business_id)
        if older is None or latest is None or creative.find(_picture(older)) is None:
            return None
        try:
            return self.build(conn, settings, account.business_id, older, latest[1], account)
        except (ValueError, ToolError, PlatformError):
            return None

    # ---------------------------------------------------------------- enable, update, pause

    def enable(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        plan: dict,
        resources: dict,
        *,
        http: httpx.Client | None = None,
        seen: dict | None = None,
    ) -> None:
        """The money first -- the approved plan's whole money on the ad set (its daily budget and its bid, with the whole
        targeting, R4) and the cap (R5, from the recorded base) for its month's amount, each unless the launch just set
        it (`seen`: an ad set made from this version, its budget known) -- then the campaign, the ad set and every ad ON,
        after reading the row again: a pause that landed while the step ran wins (A3 S17).

        A switch NewsBreak refuses (final review C1) never leaves ads serving on a `failed` row, which nothing reads --
        no sync, no metrics, no cap, no Pause on the card: one ad's ON refused while another of the set comes on is no
        failure (logged; that ad leaves the running set for `leftover`, `_set_aside`, and the row goes live); the
        campaign's or the ad set's refused, or no ad on at all, is the enable refused, raised and not retried -- with no
        OFF of the adapter's own: the lifecycle's stop (`ads_run._stop_then_fail`: `set_paused`, the campaign OFF with
        whatever is left of a time budget) goes before the row fails, owed and sent alone under every hold while
        NewsBreak does not take it (the M1 follow-up's fix round 2, G1). NewsBreak busy or not answering is retried as
        ever (the row stays `launching`, read hourly)."""
        biz = account.business_id
        calls = _Calls(settings, http, biz)
        entry = nb_build.entry(plan)
        month, daily = _cents(entry["monthly_usd"]), nb_build.daily_cents(entry["monthly_usd"])
        campaign = resources["campaign"]
        try:
            if seen is None or not seen.get("money") or seen.get("budget") != daily:
                body = {
                    "budget": daily,
                    "bidRate": nb_build.cpc_cents((plan.get("sections") or {}).get("estimate") or {}),
                    "targeting": nb_build.targeting(resources["location_ids"]),
                }
                calls(newsbreak.update_ad_set, resources["ad_set_id"], body)
            if seen is None or seen.get("month_cents") != month or seen.get("cap_cents") is None:
                self._cap(
                    conn,
                    calls,
                    biz,
                    resources["ad_account_id"],
                    month,
                    resources.get("cap_base_cents"),
                    zone=resources.get("time_zone") or TIME_ZONE,
                    base_month=resources.get("cap_month"),
                )
            if (ads_run.get(conn, biz, CHANNEL) or {}).get("state") == "paused":
                return
            calls(newsbreak.campaign_status, campaign, True)  # refused: the enable's refusal (the lifecycle stops it)
            calls(newsbreak.ad_set_status, resources["ad_set_id"], True)
            refused: list[tuple[str, newsbreak.NewsBreakError]] = []
            came_on = 0
            for ad in _ad_ids(resources):
                try:
                    calls(newsbreak.ad_status, ad, True)
                except newsbreak.NewsBreakError as e:
                    if e.owner in RETRYABLE:
                        raise
                    refused.append((ad, e))
                    continue
                came_on += 1
            if refused and not came_on:  # nothing of the set came on: the enable is refused (the lifecycle stops it)
                raise refused[0][1]
            if refused:
                _set_aside(conn, biz, refused)
        except newsbreak.NewsBreakError as e:
            raise platform_error(e) from None

    def update(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        resources: dict,
        old_plan: dict,
        build: Build,
        *,
        http: httpx.Client | None = None,
    ) -> Resources:
        """The newer approved version (`build`'s) onto what `old_plan` built (`build.diff`): the new version's whole
        money and area first, every time -- its daily budget, its bid and the whole targeting (R4) in one ad-set update,
        whatever `diff` says moved (S16 as Google's adapter does it, final review I1: a version NewsBreak refused
        further down after taking its money never stays the money of the one after it); new words a new set of ads --
        each created OFF and remembered on the row as it is made (`updating`: this version, its page address, its ads),
        all turned ON only once the last is made -- with the running ones left ON beside them (`replaced`) until every
        new one passes NewsBreak's review (the hourly read, R15); every ad that serves moved to the page's address when
        it moved meanwhile -- the running ones and a finished change's old ones still ON (`replaced`,
        `_carry_replaced`), never one left over (the lifecycle's update carries the ads there, and its write forgets the
        move; `diff` cannot see a page); the cap set again (R5). The row keeps what NewsBreak carries once each part
        went through, refused afterwards or not: the ZIP codes (`location_ids`) once the money and area went, the
        address (`click_url`) once every ad that serves moved.

        No ad ever runs that the row does not list (fix round 1): a retry of the same attempt takes the ads it made (and
        finds a create whose answer was lost by name, never an ad running now); an attempt of another version or for
        another page address is settled first -- its ads turned OFF and listed in `leftover` -- and so is this attempt's
        when NewsBreak refuses the change (the lifecycle then marks the version refused). Words NewsBreak cannot take
        are TOO_LITTLE_COPY, not retried: the ads keep running the version they have. So is a picture that left
        Admiry's set (NO_IMAGE, after the money: S16) -- unless it is the one the serving ads carry (M1 follow-up R2,
        `_carried`): any other, uploaded earlier or not, and ads that never served, need the set to have it."""
        biz, payload = account.business_id, build.payload
        calls = _Calls(settings, http, biz)
        new_plan = plans.get(conn, biz, build.plan_id) or {}
        account_id, ad_set = resources["ad_account_id"], resources["ad_set_id"]
        assets = dict(resources.get("assets") or {})
        leftover, replaced = _leftover(resources), _replaced(resources)
        target = payload["click_url"]
        attempt = resources.get("updating") or {}
        made: dict[str, str] = {}
        turned_on = False
        try:
            if attempt and (attempt.get("plan_id"), attempt.get("click_url")) != (build.plan_id, target):
                leftover = self._retire(conn, calls, biz, (attempt.get("ads") or {}).values(), leftover, turn_off=True)
                attempt = {}
            made = {str(slot): str(ad) for slot, ad in (attempt.get("ads") or {}).items()}
            ops = self._diff(old_plan, new_plan, resources, payload, assets.get(payload["image"]))
            calls(newsbreak.update_ad_set, ad_set, _money(payload))  # S16: the whole money and area, every time
            _remember(conn, biz, {"location_ids": payload["location_ids"]})  # what NewsBreak carries now
            carried = _carried(conn, biz, old_plan)
            if carried is None or any(kind == "ad_create" for kind, _item in ops):
                # R2: the picture new ads show, or ads that never served would turn on with, is in the set unless the
                # serving ads carry it; refused, what an earlier run of this attempt made on it goes OFF (`leftover`)
                try:
                    url = self._asset(conn, calls, biz, account_id, payload["image"], assets, carried=carried)
                except PlatformError:
                    if attempt:
                        self._retire(conn, calls, biz, made.values(), leftover, turn_off=True)
                    raise
                ops = self._diff(old_plan, new_plan, resources, payload, url)
            creates = [item for kind, item in ops if kind == "ad_create"]
            missing = [str(i) for i in range(len(creates)) if str(i) not in made]
            named: dict = {}
            if missing and attempt:  # this attempt's earlier run: a create whose answer was lost is found by name
                running = {*_ad_ids(resources), *leftover, *replaced, *made.values()}  # never one running now
                named = {
                    a["name"]: str(a["id"])
                    for a in calls(newsbreak.ads, account_id)
                    if a.get("name")
                    and a.get("id")
                    and str(a.get("adSetId")) == str(ad_set)
                    and str(a["id"]) not in running
                }
            for slot in missing:
                if not made:  # the attempt goes on the row before its first create
                    _remember(conn, biz, {"updating": {"plan_id": build.plan_id, "click_url": target, "ads": {}}})
                body = creates[int(slot)]
                ad = named.get(body["name"]) or calls(newsbreak.create_ad, body).get("id")
                if not ad:
                    raise newsbreak.NewsBreakError(newsbreak.UNREACHABLE)  # made, maybe: found by name next time
                made[slot] = str(ad)
                _remember(conn, biz, {"updating": {"plan_id": build.plan_id, "click_url": target, "ads": made}})
            turned_on = bool(made)
            for slot in sorted(made, key=int):  # only now that every new ad is made
                calls(newsbreak.ad_status, made[slot], True)
            # what this version replaces: the ads running now, once it finishes -- never recorded before it does (a
            # refusal leaves them the running set); they wait for the new ones' review (`replaced`, R15)
            retired = list(dict.fromkeys([*replaced, *(str(item) for kind, item in ops if kind == "ad_off")]))
            # the page moved too: every ad that serves moves there -- the running ones (`ads` as the update starts,
            # left ON beside new ones until those pass review) and a finished change's old ones still ON (`replaced`,
            # fix round 2, item 4); the new ones are made there; never one left over (`leftover`: OFF, or soon OFF,
            # perhaps gone from NewsBreak)
            if resources.get("click_url") != target:
                for ad in _ad_ids(resources):
                    calls(newsbreak.update_ad, ad, {"creative": {"clickThroughUrl": target}})
                _carry_replaced(calls, biz, replaced, target)
                _remember(conn, biz, {"click_url": target})  # what every ad that serves carries now, refused or not
            capped = self._cap(
                conn,
                calls,
                biz,
                account_id,
                payload["cap_cents"],
                resources.get("cap_base_cents"),
                zone=resources.get("time_zone") or TIME_ZONE,
                base_month=resources.get("cap_month"),
            )
        except newsbreak.NewsBreakError as e:
            error = platform_error(e, running=True)
            if not error.retryable:  # the change refused: what this attempt made never runs unlisted
                self._retire(conn, calls, biz, made.values(), leftover, turn_off=turned_on)
            raise error from None
        out = {k: v for k, v in resources.items() if k not in ("updating", "leftover", "replaced", LEGACY_OUTGOING)}
        out |= {"assets": assets, "click_url": target, "location_ids": payload["location_ids"]} | capped
        if made:
            out["ads"] = made
        if leftover:
            out["leftover"] = leftover
        if retired:
            out["replaced"] = retired
        return Resources(ids=out)

    def _retire(
        self, conn: psycopg.Connection, calls: _Calls, biz: str, ads, leftover: list[str], *, turn_off: bool
    ) -> list[str]:
        """Ads of an update that will not finish (another version arrived, or NewsBreak refused the change) turned OFF
        when they may be on, and listed in `leftover` on the row -- the hourly read turns any still ON off at once and
        drops each once NewsBreak shows it OFF or gone (R21, R25); the attempt itself (`updating`) forgotten. An OFF
        stops spend: sent with whatever time is left (fix round 2); a NewsBreak that does not answer it leaves the ad
        listed all the same: never running unlisted. Returns the list as written."""
        ads = [str(ad) for ad in ads]
        for ad in ads if turn_off else ():
            try:
                calls.stop(newsbreak.ad_status, ad, False)
            except newsbreak.NewsBreakError as e:
                log.warning("newsbreak: business %s's ad %s was not turned off (%s); it stays listed", biz, ad, e.owner)
        leftover = list(dict.fromkeys([*leftover, *ads]))
        _keep_leftover(conn, biz, leftover)
        ads_run.forget(conn, biz, CHANNEL, "updating")
        return leftover

    @staticmethod
    def _diff(old_plan: dict, new_plan: dict, resources: dict, payload: dict, url: str | None) -> list:
        """`build.diff` with the build's own inputs, the ads' operations alone (the update sends the whole money and
        area itself, S16); new words NewsBreak cannot take (none left) are TOO_LITTLE_COPY."""
        try:
            return nb_build.diff(
                old_plan,
                new_plan,
                resources,
                location_ids=payload["location_ids"],
                asset_url=url,
                click_url=payload["click_url"],
                brand=payload["copy"]["brand"],
                with_money=False,
            )
        except ValueError:
            raise PlatformError(TOO_LITTLE_COPY, retryable=False) from None

    def set_paused(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        resources: dict,
        paused: bool,
        *,
        http: httpx.Client | None = None,
        validate_only: bool = False,
    ) -> None:
        """The campaign OFF (or ON). `validate_only` -- a resume under `validate` -- calls nothing: NewsBreak has no
        check-only call (D90). Nothing built (a launch cut short) has nothing on: every create is OFF. A pause stops
        spend, which is always allowed (fix round 2): it is sent with whatever is left of a time budget, and a cut or a
        failure leaves it owed."""
        campaign = resources.get("campaign")
        if validate_only or not campaign:
            return
        calls = _Calls(settings, http, account.business_id)
        try:
            (calls.stop if paused else calls)(newsbreak.campaign_status, campaign, not paused)
        except newsbreak.NewsBreakError as e:
            raise platform_error(e, running=True) from None

    def pull_metrics(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        resources: dict,
        days: int,
        *,
        http: httpx.Client | None = None,
    ) -> list[MetricDay]:
        """R13: NewsBreak's HOURLY report -- its settlement basis, never REALTIME -- for the last `days` days of the
        account's own calendar (`resources.time_zone`, R6; the client's REPORT_TIMEZONE while unknown), today included,
        summed per day over every ad of the account: the cost in cents (the report's integer `cost`) x 10,000 as
        micros, impressions, clicks and NewsBreak's conversions; calls 0 -- NewsBreak reports none. A day the report has
        no row for has no MetricDay. A row with a cost and no day Admiry can read (NewsBreak's key or format not the one
        Admiry reads) adds to no day -- none is made up -- and is logged once per account and day (`undated_report`, M1
        Task 6 fix round 1, item 3); so is a row with no cost Admiry can read (`costless_report`, final review Minor 3):
        never counted as nothing spent, it adds to no day at all -- no metric row is made of it, and a day an earlier
        read wrote keeps what it had. The operator's nb-reconcile fails on either. NewsBreak's failures propagate as a
        PlatformError (ads-sync isolates them)."""
        biz = account.business_id
        zone_name = resources.get("time_zone") or newsbreak.REPORT_TIMEZONE
        today = ads_run.now().astimezone(ads_run.zone(zone_name)).date()
        first = today - timedelta(days=days - 1)
        calls = _Calls(settings, http, biz)
        try:
            rows = calls(newsbreak.report, resources.get("ad_account_id"), start=first, end=today, timezone=zone_name)
        except newsbreak.NewsBreakError as e:
            raise platform_error(e) from None
        summed: dict[date, dict] = {}
        nowhere, costless = 0, False
        for row in rows:
            try:
                day: date | None = date.fromisoformat(str(row.get("date")))
            except ValueError:
                day = None
            if day is not None and not first <= day <= today:
                continue
            cost = _whole(row.get("cost"))
            if cost is None:  # money Admiry cannot read is never none spent: the row adds to no day
                costless = True
                continue
            if day is None:
                nowhere += cost  # a row without a day of its own adds to no day
                continue
            totals = summed.setdefault(day, {"cost": 0, "impressions": 0, "clicks": 0, "conversions": 0})
            totals["cost"] += cost
            totals["impressions"] += _whole(row.get("impression")) or 0
            totals["clicks"] += _whole(row.get("click")) or 0
            totals["conversions"] += _whole(row.get("conversion")) or 0
        if nowhere and resources.get("undated_report") != today.isoformat():
            log.warning(
                "newsbreak: business %s's report holds %s cents on no day Admiry can read; nothing counts them",
                biz,
                nowhere,
            )
            _remember(conn, biz, {"undated_report": today.isoformat()})
        if costless and resources.get("costless_report") != today.isoformat():
            log.warning(
                "newsbreak: business %s's report holds a row with no cost Admiry can read; it counts nowhere", biz
            )
            _remember(conn, biz, {"costless_report": today.isoformat()})
        return [
            MetricDay(
                day=day,
                cost_micros=totals["cost"] * 10_000,
                impressions=totals["impressions"],
                clicks=totals["clicks"],
                calls=0,
                conversions=float(totals["conversions"]),
            )
            for day, totals in sorted(summed.items())
        ]

    def pull_issues(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        row: dict,
        *,
        http: httpx.Client | None = None,
    ) -> Serving:
        """R14: what NewsBreak shows of the ads -- every ad's review (`auditStatus`, `statusTxt`) and the campaign's and
        ad set's `status` and online status -- as the row's state: `limited` while NewsBreak turns down an ad that
        serves (`ads`, or an update's in flight, R21), holds the ads back (an online status other than ACTIVE, its own
        word kept for the card in `resources.issues.campaign`) or shows them switched OFF though Admiry runs them
        (PAUSED_IN_NEWSBREAK: NewsBreak's own pause, or a hand in Admiry's Organization -- the owner has no access, so
        the row asks nothing of them, and an operator is mailed once per occurrence); `live` otherwise. A paused row
        stays paused; when NewsBreak serves its ads anyway, the pause is owed to NewsBreak again (`Serving.repause`: the
        lifecycle's `pause_owed`, sent by the hourly ads-launch) and an operator is mailed once per occurrence. A row
        whose pause is owed, or still `launching`, keeps its state and sentence (the steps' to finish). An ad NewsBreak
        turns down is a `channel_issue(kind='disapproval')` row in NewsBreak's own words, one per sentence while it
        stands, resolved once the review clears (the card quotes the first open one, Review Focus 4). The read sets and
        clears only its own note (A3 S15).

        Under the `live` switch alone (every NewsBreak write is), the read keeps the ads in order too (`_upkeep`): the
        ads on their way out turned OFF -- a finished change's old ads once every new ad passed review, an attempt's
        left over at once (R15, R21, R25) -- the ad set's end date moved a year out (R23), the month's cap set on the
        first read of each account month (R16, R22) and then ads NewsBreak shows switched OFF, which nobody paused,
        switched back ON (NewsBreak may switch a capped account's ads off at its cap). Each money-relevant write is
        audited (`job.ads_sync_*`). Its own failures never stop the read -- each is tried again an hour later -- but
        for figures that disagree, whose stop the row then says until a cap is set again (MONTH_CHECK: no change of the
        owner's waits, so not an update's SPEND_CHECK; M1 Task 7). NewsBreak's failures to answer the read itself
        propagate as a PlatformError."""
        biz, resources = account.business_id, row["resources"]
        account_id = resources.get("ad_account_id")
        if not account_id:  # a row Admiry never built: nothing of it to read
            raise PlatformError(newsbreak.REFUSED, retryable=False)
        calls = _Calls(settings, http, biz)
        try:
            listed = {str(ad["id"]): ad for ad in calls(newsbreak.ads, account_id) if ad.get("id")}
            campaign = _found(calls(newsbreak.campaigns, account_id), resources.get("campaign"))
            ad_set = _found(calls(newsbreak.ad_sets, account_id), resources.get("ad_set_id"))
        except newsbreak.NewsBreakError as e:
            raise platform_error(e) from None
        serving = [listed[ad] for ad in dict.fromkeys([*_ad_ids(resources), *_in_flight(resources)]) if ad in listed]
        turned_down = _review(conn, settings, biz, serving)
        cap = None
        if registry.exec_mode(settings, CHANNEL) == "live":
            cap = self._upkeep(conn, calls, biz, row, listed, ad_set)
            if cap == "set":
                self._back_on(conn, calls, biz, row, campaign, ad_set)
        state, note, held = _serving(row, campaign, ad_set, turned_down)
        repause = row["state"] == "paused" and not resources.get("pause_owed") and (campaign or {}).get("status") == ON
        self._tell_once(
            conn,
            settings,
            biz,
            row,
            {PAUSED_THERE: note == PAUSED_IN_NEWSBREAK, RUNNING_THERE: repause},
        )
        current = row["last_error"]
        if cap == "set" and current in (SPEND_CHECK, MONTH_CHECK):  # a cap is set again: the stop is over
            current = None
        if note is _AS_IS:
            last_error = current
        else:
            last_error = current if current is not None and current not in SYNC_NOTES else note
        if cap == SPEND_CHECK and last_error is None:  # the month's own stop, in its own words (M1 Task 7)
            last_error = MONTH_CHECK
        return Serving(state=state, last_error=last_error, issues={"campaign": held} if held else {}, repause=repause)

    def pull_stops(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        row: dict,
        *,
        http: httpx.Client | None = None,
    ) -> bool:
        """The hourly read's stops alone, for a day the jobs hold NewsBreak (M1 Task 6 fix round 1, item 1;
        `ads_run.sync_stops` finds this hook by name: NewsBreak's alone): the ads on their way out turned OFF (`replaced`
        once every running ad passed review, `leftover` at once -- `_settle_outgoing`, under `live`), and True when
        NewsBreak shows ON the campaign of a row Admiry shows paused, no pause owed yet (the lifecycle then owes NewsBreak
        the pause again) -- with the whole read's operator mail, once per occurrence under the same mark
        (`_tell_once`, RUNNING_THERE; final review, a deferred minor). It reads only what those need -- the ads' status
        while ads are on their way out, the campaigns' while the row is paused -- and nothing at all for a row that owes
        neither; no report, no review, no month's cap, no end date. NewsBreak's failures to answer those reads propagate
        as a PlatformError (an ad's OFF NewsBreak does not take stays listed, as in the whole read)."""
        resources = row["resources"]
        account_id = resources.get("ad_account_id")
        outgoing = bool(_replaced(resources) or _leftover(resources))
        owed = row["state"] == "paused" and not resources.get("pause_owed")
        if not account_id or not (outgoing or owed):
            return False
        calls = _Calls(settings, http, account.business_id)
        try:
            if outgoing and registry.exec_mode(settings, CHANNEL) == "live":
                listed = {str(ad["id"]): ad for ad in calls(newsbreak.ads, account_id) if ad.get("id")}
                self._settle_outgoing(conn, calls, account.business_id, resources, listed)
            if owed:
                campaign = _found(calls(newsbreak.campaigns, account_id), resources.get("campaign"))
                running = (campaign or {}).get("status") == ON
                self._tell_once(conn, settings, account.business_id, row, {RUNNING_THERE: running})
                return running
        except newsbreak.NewsBreakError as e:
            raise platform_error(e) from None
        return False

    def _upkeep(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        row: dict,
        listed: dict[str, dict],
        ad_set: dict | None,
    ) -> str | None:
        """The hourly read's own writes to NewsBreak (`pull_issues`, under `live`): the ads on their way out, the ad
        set's end date, the month's cap. Each failure is logged and left for the next read. Returns "set" when the
        month's cap was set, SPEND_CHECK when figures that disagree stopped it, else None."""
        resources = row["resources"]
        self._settle_outgoing(conn, calls, biz, resources, listed)
        self._extend(conn, calls, biz, resources, ad_set)
        return self._new_month(conn, calls, biz, row)

    def _settle_outgoing(
        self, conn: psycopg.Connection, calls: _Calls, biz: str, resources: dict, listed: dict[str, dict]
    ) -> None:
        """R15, R21, R25 (fix round 1, item 1): the ads on their way out, two lists kept apart. `replaced` -- the old
        ads a finished change of words replaced -- stay ON until every running ad (`ads`) has passed NewsBreak's
        review, whatever their own review says (one under review, or back under it after a page move, is still what
        NewsBreak shows the moment that clears): never a moment with nothing running, at most a day of both; then they
        go OFF. `leftover` -- a refused or superseded change's, a failed launch's, a gone version's, and a row 5a's code
        wrote (`retiring`) -- go OFF at once, whatever their own review says: no running ad waits for them. One
        NewsBreak shows OFF, or no longer has (gone from its list, or listed as DELETED), leaves its list; one whose
        OFF NewsBreak does not take stays on it for the next read. An OFF stops spend: always sent (`_Calls.stop`);
        each read's OFFs are audited (`job.ads_sync_off`)."""
        replaced, leftover = _replaced(resources), _leftover(resources)
        if not replaced and not leftover and LEGACY_OUTGOING not in resources:
            return
        serving = {*_ad_ids(resources), *_in_flight(resources)}
        running = [
            listed[ad] for ad in _ad_ids(resources) if ad in listed and listed[ad].get("onlineStatus") != DELETED
        ]
        passed = bool(running) and all(ad.get("auditStatus") == APPROVED for ad in running)
        kept_replaced, off_replaced = _turn_off(calls, biz, replaced, listed, serving, now=passed)
        kept_leftover, off_leftover = _turn_off(calls, biz, leftover, listed, serving, now=True)
        if kept_replaced != replaced:
            if kept_replaced:
                _remember(conn, biz, {"replaced": kept_replaced})
            else:
                ads_run.forget(conn, biz, CHANNEL, "replaced")
        if kept_leftover != leftover or LEGACY_OUTGOING in resources:
            _keep_leftover(conn, biz, kept_leftover)
        if off_replaced:
            n = len(off_replaced)
            older = "your older ad" if n == 1 else f"{n} older ads"
            note = f"turned off {older} on NewsBreak: the new ones passed NewsBreak's review"
            _audit(conn, biz, "job.ads_sync_off", {"note": note, "ads": off_replaced})
        if off_leftover:
            n = len(off_leftover)
            some = "an ad" if n == 1 else f"{n} ads"
            note = f"turned off {some} on NewsBreak left from a change that did not go through"
            _audit(conn, biz, "job.ads_sync_off", {"note": note, "ads": off_leftover})

    def _extend(self, conn: psycopg.Connection, calls: _Calls, biz: str, resources: dict, ad_set: dict | None) -> None:
        """R23: NewsBreak stops an ad set at its end date (`endTime`, a year after the launch): once that is EXTEND_DAYS
        away or less, it is moved a year out from today, with the whole targeting (R4: NewsBreak resets an omitted one
        to everyone). An ad set NewsBreak lists without an end date cannot be read: one warning line (marked
        `resources.end_unknown` until it shows one), never a silent read that moves nothing."""
        end = _whole((ad_set or {}).get("endTime"))
        if end is None:
            if ad_set is not None and not resources.get("end_unknown"):
                log.warning(
                    "newsbreak: business %s's ad set %s shows no end date; Admiry cannot move it before it ends",
                    biz,
                    resources.get("ad_set_id"),
                )
                _remember(conn, biz, {"end_unknown": True})
            return
        if resources.get("end_unknown"):
            ads_run.forget(conn, biz, CHANNEL, "end_unknown")
        now = int(ads_run.now().timestamp())
        if end - now > EXTEND_DAYS * 86_400:
            return
        body = {"endTime": now + END_DAYS * 86_400, "targeting": nb_build.targeting(resources["location_ids"])}
        try:
            calls(newsbreak.update_ad_set, resources["ad_set_id"], body)
        except (newsbreak.NewsBreakError, PlatformError) as e:
            log.warning("newsbreak: business %s's end date was not moved (%s); the next read tries again", biz, e)

    def _new_month(self, conn: psycopg.Connection, calls: _Calls, biz: str, row: dict) -> str | None:
        """R16 as amended by R22: the first read of each account month (`resources.time_zone`'s calendar) records
        NewsBreak's lifetime total as the new base and caps the account at it plus the month's amount of the approved
        plan -- spend-guard's month cap's one source; a plan that funds NewsBreak no more gets no new month (fix round
        1, item 4) -- through `_cap`, which keeps the month in `resources.cap_month`: last month's spend never counts
        against this month's. Once a month, audited (`job.ads_sync_cap`); 5a's sanity rule holds (figures that disagree
        set no cap: one operator mail, and SPEND_CHECK, returned). A row from before the month was kept is never set
        again in the middle of a month: its month is recorded as this one (a cap that stops early, never one that lets
        more through). Returns "set", SPEND_CHECK or None."""
        resources = row["resources"]
        zone = resources.get("time_zone") or TIME_ZONE
        month, kept = _month(zone), resources.get("cap_month")
        if kept == month:
            return None
        if kept is None:
            _remember(conn, biz, {"cap_month": month})
            return None
        try:
            entry = nb_build.entry(ads_run.approved(conn, biz) or {})
        except ValueError:  # the approved plan funds NewsBreak no more (its pause is the lifecycle's): no new month
            return None
        month_cents = _cents(entry["monthly_usd"])
        try:
            self._cap(conn, calls, biz, resources["ad_account_id"], month_cents, zone=zone)
        except PlatformError as e:
            if str(e) == SPEND_CHECK:
                return SPEND_CHECK
            log.warning(
                "newsbreak: business %s's cap for %s was not set (%s); the next read tries again", biz, month, e
            )
            return None
        except newsbreak.NewsBreakError as e:
            log.warning(
                "newsbreak: business %s's cap for %s was not set (%s); the next read tries again", biz, month, e.owner
            )
            return None
        _audit(conn, biz, "job.ads_sync_cap", {"cost_cents": month_cents, "month": month})
        return "set"

    def _back_on(
        self,
        conn: psycopg.Connection,
        calls: _Calls,
        biz: str,
        row: dict,
        campaign: dict | None,
        ad_set: dict | None,
    ) -> None:
        """Fix round 1, item 2 (d): once the new month's cap is set, ads Admiry runs (a live or limited row: no pause
        of the owner's or Admiry's stands) that NewsBreak shows switched OFF -- the campaign or the ad set, as NewsBreak
        may do to a capped account at its cap (a pilot check) -- are switched back ON; what this read says of them then
        is ON. A pause never waits for the step's lock (A3 S17: one that lands while this read runs is recorded owed,
        `ads_run.pause`), so the row is read again right before each switch, as `enable` reads it before it turns
        anything on, and nothing more is switched once any pause stands (`_pause_stands`; fix round 2, item 1). The
        switches NewsBreak took are audited whatever becomes of the next (item 3) -- in one row for the event, once it
        ends (`job.ads_sync_on`, `switched` naming them, its line saying what went through: M1 Task 7, A5). A failure
        is logged and the row reads the rest OFF (PAUSED_IN_NEWSBREAK)."""
        if row["state"] not in ("live", "limited"):
            return
        resources = row["resources"]
        off = [
            (switch, obj, ident, part)
            for switch, obj, ident, part in (
                (newsbreak.campaign_status, campaign, resources.get("campaign"), "campaign"),
                (newsbreak.ad_set_status, ad_set, resources.get("ad_set_id"), "ad_set"),
            )
            if obj is not None and obj.get("status") != ON
        ]
        went: list[str] = []
        for switch, obj, ident, part in off:
            if _pause_stands(conn, biz):
                log.info(
                    "newsbreak: business %s's ads were paused while the read ran; nothing more is switched on", biz
                )
                break
            try:
                calls(switch, ident, True)
            except (newsbreak.NewsBreakError, PlatformError) as e:
                log.warning("newsbreak: business %s's ads were not switched back on (%s); the next month tries", biz, e)
                break
            obj["status"] = ON
            went.append(part)
        if went:
            note = _back_on_note(went, every=len(went) == len(off))
            _audit(conn, biz, "job.ads_sync_on", {"switched": went, "note": note})

    def _tell_once(
        self, conn: psycopg.Connection, settings: Settings, biz: str, row: dict, standing: dict[str, bool]
    ) -> None:
        """Fix round 1, item 2: one operator mail per occurrence of what the read found standing -- NewsBreak shows the
        ads OFF though Admiry runs them (PAUSED_THERE), or ON though Admiry paused them (RUNNING_THERE). Kept in
        `resources.told` as {kind: {"n": occurrences told, "open": it stands}}: a mail goes when one starts standing,
        under an idempotency key of its own occurrence (`n`: a retry of the same mail is the same key, the next
        occurrence a new one), and `open` closes once it no longer stands. Without an operator address the mail is
        skipped with a log line and tried again on the next read."""
        resources = row["resources"]
        told = {kind: dict(mark) for kind, mark in (resources.get("told") or {}).items() if isinstance(mark, dict)}
        before = {kind: dict(mark) for kind, mark in told.items()}
        for kind, now_so in standing.items():
            mark = told.get(kind) or {"n": 0, "open": False}
            if not now_so:
                if mark["open"]:
                    told[kind] = {**mark, "open": False}
                continue
            if mark["open"]:
                continue
            n = int(mark.get("n") or 0) + 1
            subject, paragraphs = _notice(kind, biz, row)
            if _tell_operator(settings, key=f"nb-{kind}-{biz}-{n}", subject=subject, paragraphs=paragraphs):
                told[kind] = {"n": n, "open": True}
        if told != before:
            _remember(conn, biz, {"told": told})

    def conversion_account(
        self, conn: psycopg.Connection, settings: Settings, account: Account, *, http: httpx.Client | None = None
    ) -> Account | None:
        """The account as the hourly conversion upload sends leads back to it (M1 Task 6, T6-3; `jobs.conversion_upload`
        finds this hook by name: NewsBreak's alone). NewsBreak attributes a lead only as the account's lead event,
        which an operator makes by hand -- no API makes one, and the launch mailed the operator once (R10) -- so its id
        is looked up (`client.events`, one request, paced with the business's others) and kept on the account row once
        NewsBreak lists it (`external.event_id`, merged into what the row holds: `push_conversions` has no connection
        to keep it), never asked for again. None while the account has no ad account, or no such event: its leads wait,
        retried within NewsBreak's 7 days. NewsBreak's failures are a PlatformError in the words for ads already
        running (`platform_error(..., running=True)`), should an owner ever see one."""
        external = account.external or {}
        if external.get("event_id"):
            return account
        account_id = external.get("ad_account_id")
        if not account_id:
            return None
        try:
            found = _lead_event(_Calls(settings, http, account.business_id)(newsbreak.events, account_id))
        except newsbreak.NewsBreakError as e:
            raise platform_error(e, running=True) from None
        if found is None:
            return None
        event_id = str(found["id"])
        _keep_account(conn, account.business_id, event_id=event_id)
        return dataclasses.replace(account, external={**external, "event_id": event_id})

    def push_conversions(
        self, settings: Settings, account: Account, events: list[ConversionEvent], *, http: httpx.Client | None = None
    ) -> UploadResult:
        """Leads back to NewsBreak by click id (P9: leads only, no identifier of the person, D84), as the account's lead
        event (`client.ADMIRY_LEAD_EVENT`). Rejected, never sent: an event that is not a lead or has no click id, and
        every event of an account whose lead event does not exist yet (looked up when `external` has no `event_id` --
        the conversion upload looks it up first and keeps it, `conversion_account`; NO_LEAD_EVENT is the note). A
        click NewsBreak no longer takes (its 7 days are over) is rejected and the others go on; NewsBreak busy or not
        answering, or the token's trouble, stops the rest as a retryable PlatformError in the words for ads already
        running (T6-3)."""
        external = account.external or {}
        leads = [e for e in events if e.kind == "lead" and e.click_id]
        rejected, accepted = len(events) - len(leads), 0
        if not leads:
            return UploadResult(accepted=0, rejected=rejected, note=None)
        calls = _Calls(settings, http, account.business_id)
        try:
            if not external.get("event_id"):
                account_id = external.get("ad_account_id")
                if not account_id or _lead_event(calls(newsbreak.events, account_id)) is None:
                    return UploadResult(accepted=0, rejected=len(events), note=NO_LEAD_EVENT)
            for event in leads:
                value = round(Decimal(str(event.value_usd)) * 100) if event.value_usd else 0
                try:
                    calls(
                        newsbreak.send_conversion,
                        click_id=event.click_id,
                        event_type=newsbreak.ADMIRY_LEAD_EVENT,
                        value_cents=max(0, value),
                    )
                    accepted += 1
                except newsbreak.NewsBreakError as e:
                    if e.owner in RETRYABLE:
                        raise
                    rejected += 1
        except newsbreak.NewsBreakError as e:
            raise platform_error(e, running=True) from None
        return UploadResult(accepted=accepted, rejected=rejected, note=None)

    def pull_leads(
        self, settings: Settings, account: Account, since: datetime, *, http: httpx.Client | None = None
    ) -> list[LeadIn]:
        raise Unsupported("NewsBreak has no lead forms to pull: leads arrive on the business's page")

    # ---------------------------------------------------------------- the page the ads send people to (R7, R8)

    def landing_url(self, page_url: str) -> str:
        return nb_build.page_url(page_url)

    def carried_url(self, resources: dict) -> str | None:
        return resources.get("click_url")

    def move_page(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        resources: dict,
        running_plan: dict,
        build: Build,
        *,
        http: httpx.Client | None = None,
        with_money: bool,
    ) -> Resources:
        """Every ad that serves moved to `build`'s page address and nothing else: a URL-only update of each -- the
        running ones, an update's in flight, then a finished change's old ones still ON beside the new (`replaced`,
        `_carry_replaced`; fix round 2, item 4); never one left over (`leftover`). `with_money` sends the running
        plan's daily budget and bid first (with the whole targeting, R4), as every update does (S16); its refusal is
        the plan's (PlatformError). A refusal of a running ad's new address is AddressRefused (the lifecycle records the
        address, never the plan); NewsBreak busy or not answering is a retryable PlatformError."""
        calls = _Calls(settings, http, account.business_id)
        target = build.payload["click_url"]
        if with_money:
            entry = nb_build.entry(running_plan)
            body = {
                "budget": nb_build.daily_cents(entry["monthly_usd"]),
                "bidRate": nb_build.cpc_cents((running_plan.get("sections") or {}).get("estimate") or {}),
                "targeting": nb_build.targeting(resources["location_ids"]),
            }
            try:
                calls(newsbreak.update_ad_set, resources["ad_set_id"], body)
            except newsbreak.NewsBreakError as e:
                raise platform_error(e, running=True) from None
        # every ad that serves: the running ones and an update's in flight first -- a refusal of theirs is the
        # address's -- then a finished change's old ones still ON (`replaced`); never one left over (`leftover`: OFF,
        # or soon OFF, and perhaps gone from NewsBreak, which must never read as the address refused)
        in_flight = ((resources.get("updating") or {}).get("ads") or {}).values()
        serving = list(dict.fromkeys([*_ad_ids(resources), *(str(ad) for ad in in_flight)]))
        for ad in serving:
            try:
                calls(newsbreak.update_ad, ad, {"creative": {"clickThroughUrl": target}})
            except newsbreak.NewsBreakError as e:
                if e.owner in RETRYABLE:
                    raise platform_error(e, running=True) from None
                raise AddressRefused(e.owner) from None
        _carry_replaced(calls, account.business_id, [ad for ad in _replaced(resources) if ad not in serving], target)
        return Resources(ids={**resources, "click_url": target})

    # ---------------------------------------------------------------- the client

    def client(self, *, budget_s: float | None = None) -> httpx.Client:
        """NewsBreak's client (`client.client()`), metered for `ops_sent`. With `budget_s` (the approval's own request,
        `ads_run.launch_now`), one deadline for every request it sends (`_Deadline`): each gets at most what is left as
        its timeout; the adapter sends no request that cannot fit in what is left and stops its step on STARTING (the
        hourly job finishes it), and a request that finds nothing left fails before it is sent."""
        http = newsbreak.client()
        if budget_s is None:
            return http
        http.event_hooks = {**http.event_hooks, "request": [_Deadline(budget_s), *http.event_hooks["request"]]}
        return http

    def ops_sent(self, http: httpx.Client) -> int:
        return newsbreak.ops_sent(http)


ADAPTER = registry.register(NewsBreakAdapter())
