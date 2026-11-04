"""The Google Search adapter (spec 2026-09-29-admiry-channels-design §5.2; plan 2026-09-29-agent-m0-channel-framework
P1, P2, P3): the glue that makes A3's `client`, `build` and `conversions` one adapter behind the protocol in `base.py`.
Its account half says what Google Search can do, how the owner connects it and whether the link is ready (M0 Task 2);
its execution half is A3's Google steps, lifted out of `agent/ads_run.py` with their bodies (M0 Task 4) -- the name
lookup before any create (P8), the create of everything paused and the enable at the approved plan's budget (P7),
the money first and then the structural change of an update (S16), the pause and the owed pause (S11), the reads of
the metrics, the serving status and the ad reviews (A3 §6, S12, S14, S15). `ads_run` keeps the state machine: which
step a row needs, the lock, the row's state and sentence, the audit.

The Google Ads account is the one the business linked (`ads_link`, the source of truth for Google, mirrored into
`channel_account`) -- or, for a prepaid business, the one Admiry runs its ads in (A3c-1: `google_managed`, a pool
account under Admiry's manager account filed by industry, picked by the launch the first time; while no account of the
business's industry, nor root, has room, the launch waits on ACCOUNT_OPENING and the operator is told what to open); a
campaign keeps the Customer ID it was built in (`resources.customer_id`), and every later step addresses that account,
as A3's `ads_campaign.customer_id` did -- a re-link to another account moves nothing by itself. Every read and change
names the business's own campaign (`campaign.resource_name`, its name ending with the business id), so several
businesses share a pool account without seeing each other's ads or figures.

This adapter's own keys in `resources`, beside Google's resource names (`campaign`, `campaign_budget`, `ad_groups`,
`ads`, `keywords`, `negatives`, `geo_criteria`, `campaign_asset`, `call_asset`): `customer_id`, `creating_plan_id`
(the version a create in flight builds), `geo_cache` (what Google answered for the service-area names, ruling S10),
`time_zone` (the account's own, read once: Google's `segments.date` is that zone's calendar, ruling S14), `issues`
(the reasons and ad reviews the last sync read), and `final_url` (A3p-2 H9: the page address every ad carries, with
`src=gads` -- `carried_url`). Its ads follow the business's page (`follows_page`, M0 Task 9): `move_page` is A3p-2's
URL-only update, the running plan diffed against itself at the page's new address. Importing this module registers
the adapter (`agent.channels` imports it)."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
import psycopg
from fastmcp.exceptions import ToolError

from ... import (
    ads_refusal,
    ads_run,
    audit,
    business,
    config,
    google_ads_link,
    google_managed,
    landing,
    mail_out,
    plans,
    wallet,
)
from ...config import Settings
from .. import registry
from ..base import (
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
from . import build as ads_launch  # A3's module names for the moved modules, so the bodies below read as they did
from . import client as google_ads
from . import conversions as ads_conversions

log = logging.getLogger(__name__)

CHANNEL = "google_search"
MAX_AREAS = 5  # service-area names resolved to Google locations, at most (one suggest request each, and an
# area Google cannot place is asked again every hour until the owner fixes it)
ADS_HOST = urlsplit(google_ads.BASE_URL).hostname

# What the owner is told to do: their own account's link, or nothing at all (A3c-1: Admiry's own account runs them).
CONNECT_OWN_ACCOUNT = (
    "Give Admiry your 10-digit Customer ID; accept the invitation in Google Ads (Admin → Access and security)"
)
CONNECT_MANAGED = (
    "Nothing to connect: Admiry runs Google Search for you in its own Google Ads account, paid from your balance"
)

# Owner sentences (A3's, word for word): ToolError text, channel_campaign.last_error and the approval tools' `say`.
# D52 and the JARGON tests: "your ads", "Google Search", "budget" -- never campaign / ad group / keyword / CPC.
RETRYING = "Google did not answer; Admiry retries within the hour"
# A3c-1: a prepaid business's launch while no pool account of its industry has room -- the row waits, retried every
# hour, and the operator is told (`_ask_for_account`); never the owner's to act on
ACCOUNT_OPENING = "Admiry is setting up the Google Ads account your ads run in; they start once it is ready"
# founder 2026-10-03: Admiry gives a prepaid business a Google Ads account of its own only once it paid by card
# (`wallet.has_paid`; an operator's credit is no payment) -- the account check says so before anything is bound
FIRST_PAYMENT = "Google Search starts after your first payment to your Admiry balance"
NOT_LINKED = (
    "Your Google Ads account is not linked to Admiry yet; link it first, since your ads run in your own account"
)
LINK_PENDING = "Admiry's invitation is still waiting in your Google Ads account; accept it there first"
NOT_SEARCH_PLAN = "This plan cannot run on Google Search as it is; ask for a fresh plan and approve that one"
TOO_LITTLE_COPY = (
    "Your ad needs at least 3 headlines and 2 descriptions that follow Google's rules; revise the ad text first"
)
GONE = "Your ads were removed in Google Ads before they started; an Admiry operator will look into it"
PAUSE_OWED = "Google Ads was not told yet; an Admiry operator will pause your ads there"
REFUSED_CHANGE = "Google Ads did not take all of your latest change; approve a changed version"  # ruling S15
# A3 Task 5 (ads-sync, §6): ruling S12's mismatch notes, and P12's statement sentence, word for word.
ENABLED_IN_GOOGLE = "Someone turned your ads on in Google Ads; Admiry still shows them paused"
PAUSED_IN_GOOGLE = "Someone paused your ads in Google Ads; resume them there"
# The same note as sync wrote it before 2026-10-02 ("or tell Admiry to": Admiry's resume answers "Your ads are not
# paused" for such a row, so the owner was sent nowhere). A row may still carry it: it stays sync's own, so the next
# sync clears it, or writes the sentence above over it.
PAUSED_IN_GOOGLE_BEFORE = "Someone paused your ads in Google Ads; resume them there, or tell Admiry to"
# ruling S15: the only `last_error` sentences sync sets or clears
SYNC_NOTES = (ENABLED_IN_GOOGLE, PAUSED_IN_GOOGLE, PAUSED_IN_GOOGLE_BEFORE)
NO_FEE = "Google bills this account directly; Admiry charges no management fee"
# A3p-2 H28: the line while `resources.refused_url` stands -- a page address Google Ads would not take for the ads,
# which keep the page they had. The mark is the truth, not `last_error` (a resume's enable clears that).
REFUSED_ADDRESS = (
    "Google Ads did not take the page address {host}; the ads keep their earlier page — use a different address or "
    "fix the page"
)
WORDS = Words(
    pause_owed=PAUSE_OWED,
    refused_change=REFUSED_CHANGE,
    checked="had Google check your ads (a test run: nothing is shown or spent)",
    checked_status="Google checked your ads and found nothing to fix; they are not running yet",
    checked_badge="Checked with Google; turns on once execution is live",
    held_back="live, but Google is holding your ads back",
    repaused="finished pausing your ads in Google Ads",
    no_fee=NO_FEE,
    refused_address=REFUSED_ADDRESS,
    paused_elsewhere=PAUSED_IN_GOOGLE,
    retrying=RETRYING,
    account_wait=ACCOUNT_OPENING,
)


# ---------------------------------------------------------------- the HTTP client, its budget and its meter


class _Meter:
    """Google Ads operations a client sent (spec §8, ruling S4: Explorer's daily quota counts every search request
    and every mutate operation) -- counted from the requests themselves, `ads_conversions.ensure`'s own included.
    The token exchange is not an operation. Only counts: never keeps a body."""

    def __init__(self) -> None:
        self.ops = 0

    def __call__(self, request: httpx.Request) -> None:
        if request.url.host != ADS_HOST:
            return
        if not request.url.path.endswith(":mutate"):
            self.ops += 1  # a search page, a geo suggestion
            return
        try:
            operations = json.loads(request.content).get("mutateOperations")
        except (ValueError, AttributeError, httpx.RequestNotRead):
            operations = None
        self.ops += len(operations) if isinstance(operations, list) and operations else 1


def client(*, budget_s: float | None = None, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The Google Ads client for the ads steps, metered (`ops_sent`). Without `budget_s`, `google_ads.client()`'s
    timeouts (the sweeper). With it (P6: the approval request), one deadline for every request the client sends:
    each gets at most what is left of the budget as its httpx timeout, and once nothing is left the next request
    fails before it is sent -- as a timeout, which the Google client reports as Google not answering. `transport`
    is for tests."""
    meter = _Meter()
    if budget_s is None:
        return httpx.Client(
            timeout=httpx.Timeout(google_ads.TIMEOUT_S, connect=5.0),
            transport=transport,
            event_hooks={"request": [meter]},
        )
    deadline = time.monotonic() + budget_s

    def clamp(request: httpx.Request) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise httpx.TimeoutException("the launch's time budget is spent", request=request)
        request.extensions["timeout"] = httpx.Timeout(min(left, google_ads.TIMEOUT_S)).as_dict()

    return httpx.Client(timeout=httpx.Timeout(budget_s), transport=transport, event_hooks={"request": [clamp, meter]})


def ops_sent(http: httpx.Client) -> int:
    """The operations `http` has sent so far, when `client()` made it; 0 for any other client."""
    return sum(hook.ops for hook in http.event_hooks["request"] if isinstance(hook, _Meter))


# ---------------------------------------------------------------- errors


def _owner_error(e: google_ads.AdsError) -> str:
    """The sentence a failed Google call leaves on the row. Google not answering, or too busy to, is RETRYING: the
    owner has nothing to try again, Admiry does (Review Focus 1). The others already say who acts."""
    return RETRYING if e.owner in (google_ads.UNREACHABLE, google_ads.BUSY) else e.owner


def _retryable(e: google_ads.AdsError) -> bool:
    """Everything but an INVALID_ARGUMENT-class refusal -- HTTP 400 with Google Ads' own error codes: Google refused
    the request itself, so the same request would be refused again. Retryable: Google down or busy, a transport
    failure, a conflict (409), and an access problem an operator fixes (a refused token exchange is a 400 too, but
    carries no Google Ads code)."""
    return not (e.status == 400 and e.codes)


def _duplicate_name(e: google_ads.AdsError) -> bool:
    return any(code.endswith("DUPLICATE_CAMPAIGN_NAME") for code in e.codes)


def _platform(e: google_ads.AdsError) -> PlatformError:
    """A Google failure as the lifecycle takes it: the owner's sentence, whether trying again can help, and what
    Google refused in detail (spec 2026-10-11 §4: the Reviewer reads it)."""
    retryable = _retryable(e)
    return PlatformError(_owner_error(e) if retryable else str(e), retryable=retryable, details=e.details)


# ---------------------------------------------------------------- the account and the plan


def _prepaid(conn: psycopg.Connection, biz: str, account: Account | None) -> bool:
    """Admiry runs the business's Google Search from its balance: the account row's mode, or `business.ads_mode`
    (Google Search's mode for one more release, P6)."""
    return (account is not None and account.mode == "managed") or (business.row(conn, biz) or {}).get(
        "ads_mode"
    ) == "managed"


def _customer_id(conn: psycopg.Connection, biz: str, *, settings: Settings | None = None) -> str:
    """The Customer ID of the Google Ads account the business's ads are built in, as A3 read it for every create: for
    a prepaid business the pool account Admiry runs it in (A3c-1, `google_managed`) -- with `settings` (the launch)
    picked now when it has none (`google_managed.assign`: its trade's, its family's, else root), and when no account
    has room PlatformError(ACCOUNT_OPENING, retryable) with the operator told what to open (`_ask_for_account`);
    otherwise the one the business linked (`ads_link`) -- a row is only ever queued through a live link, so a missing
    one is refused with the precheck's own sentence rather than sent to Google."""
    if _prepaid(conn, biz, registry.account(conn, biz, CHANNEL)):
        bound = google_managed.get(conn, biz)
        if bound is None and settings is not None:
            try:
                bound, made = google_managed.assign(conn, biz)
            except google_managed.PoolRefused as e:  # not active, or no longer prepaid: nothing to build there
                log.warning("google_search: business %s gets no Google Ads account: %s", biz, e)
                raise PlatformError(ACCOUNT_OPENING, retryable=True) from None
            if bound is None:
                _ask_for_account(conn, settings, biz)
            elif made:
                _record_assign(conn, biz, bound)
        if bound is None:
            raise PlatformError(ACCOUNT_OPENING, retryable=True)
        return bound.customer_id
    link = google_ads_link.get(conn, biz)
    if link is None:
        raise ToolError(NOT_LINKED)
    return link.customer_id


def _record_assign(conn: psycopg.Connection, biz: str, bound: google_managed.Binding) -> None:
    """The allocator's pick, in the business's log (`job.google_account`; the owner reads that Admiry chose the account
    their ads run in -- never which one: a pool account is Admiry's, shared by industry)."""
    audit.record_job(
        conn,
        job="allocator",
        tool="job.google_account",
        business_id=biz,
        args={"family": bound.family, "trade": bound.trade},
        result={"customer_id": bound.customer_id, "binding": bound.id},
    )


def _ask_for_account(conn: psycopg.Connection, settings: Settings, biz: str) -> None:
    """A3c-1: the operator told that a prepaid business's Google Search waits because no pool account of its industry
    (nor root) has room -- which scopes it can run in, and the command that adds an account. `AGENT_OPS_EMAIL` unset: a
    log line only (the list is `agent.ops google-account waiting`). Once a day at most: the key holds a retry within
    the day to one mail."""
    family, trade = google_managed.industry(conn, biz)
    scopes = google_managed.candidates(family, trade)
    first, named = scopes[0], ", ".join(google_managed.group(sc) for sc in scopes)
    paragraphs = [
        (
            f"A prepaid business ({biz}, {first}) paid and approved Google Search ads; no Google Ads account in "
            f"Admiry's pool has room for it. It can run in {named} (the most specific first)."
        ),
        "Why: " + "; ".join(google_managed.reasons(conn, biz)) + ".",
        (
            "In Admiry's manager account, open a client account: US dollars, America/Los_Angeles, billed to Admiry's "
            "payments profile. One business per account (founder 2026-10-03): open a few at once, so the next "
            "businesses start without waiting -- Google sets up at most 15 card billings a week."
        ),
        (
            "Then on the box, for each: `python -m agent.ops google-account add <customer_id>` (capacity 1, any "
            "business; `--scope` files it under an industry). The next hourly step builds the ads there."
        ),
    ]
    key = f"google-account-{biz}-{ads_run.now():%Y-%m-%d}"
    sent = mail_out.send_plain(
        settings,
        to=settings.ops_email or "",
        subject=f"[Admiry] Google Ads: no account has room for {biz}",
        paragraphs=paragraphs,
        idempotency_key=key,
        gate=None if settings.ops_email else "AGENT_OPS_EMAIL unset",
        message_id=key,  # the same on every retry of the day: one request under its Idempotency-Key, never a 409
    )
    if not sent:
        log.warning("google_search: business %s waits for room in a Google Ads account (no notice sent)", biz)


def _built_in(conn: psycopg.Connection, biz: str, resources: dict) -> str:
    """The account a campaign was built in (`resources.customer_id`), for every step after the create -- a re-link
    to another account moves nothing by itself; the linked one for ads built before M0 kept it."""
    return resources.get("customer_id") or _customer_id(conn, biz)


def copy_short(plan: dict) -> bool:
    """Whether the plan's funded Google Search entry keeps fewer than plans.MIN_HEADLINES / MIN_DESCRIPTIONS once the
    copy linter has judged it with the trade's banned words: the launch's TOO_LITTLE_COPY, and the setup card's offer to
    have Admiry write the words (plan_review). False for a plan Google Search cannot run at all (NOT_SEARCH_PLAN)."""
    try:
        ch = _search_channel(plan)
    except ToolError:
        return False
    tree = (plan["sections"]["consultation"].get("business") or {}).get("category") or "fallback"
    headlines, descriptions, _notes = ads_launch.lint_copy(
        list(ch.get("headlines") or []), list(ch.get("descriptions") or []), landing.load_recipe(tree)["banned"]
    )
    return len(headlines) < plans.MIN_HEADLINES or len(descriptions) < plans.MIN_DESCRIPTIONS


def _search_channel(plan: dict) -> dict:
    """The plan's funded Google Search entry, from a consultation (the only plans `ads_launch` can build);
    ToolError(NOT_SEARCH_PLAN) otherwise -- a plan from `create_plan`, or one with no budget or no cost estimate.
    The check itself is `ads_launch.search_channel`'s (one body: the build, the Reviewer and ads_refusal read it)."""
    ch = ads_launch.search_channel(plan, CHANNEL)
    if ch is None:
        raise ToolError(NOT_SEARCH_PLAN)
    return ch


def _search_view(plan: dict) -> dict:
    """The plan as `ads_launch` (pure, golden-tested, written for plans with one channel) reads it: Google Search's
    entry first -- a plan funding several channels shows it only its own."""
    sections = plan["sections"]
    ads = {**sections["ads"], "channels": [_search_channel(plan)]}
    return {**plan, "sections": {**sections, "ads": ads}}


def _daily(plan: dict) -> int:
    return ads_launch.daily_micros(_search_channel(plan)["monthly_usd"])


def _area_names(knowledge: dict) -> list[str]:
    names = [n.strip() for n in knowledge.get("service_area") or [] if isinstance(n, str) and n.strip()]
    return names[:MAX_AREAS]


def _where_refusals(consultation: dict, knowledge: dict) -> list[str]:
    """P4 as a list (L1): what keeps the ads from a place to centre on -- the listing's point needs the plan's radius;
    without a point, service-area names for Google to place."""
    if ads_run.has_point(knowledge):
        return [] if (consultation.get("geo") or {}).get("radius_miles") else [ads_run.NO_RADIUS]
    return [] if _area_names(knowledge) else [ads_run.NO_LOCATION]


# ---------------------------------------------------------------- what a launch needs from Google and the database


def ads_url(url: str) -> str:
    """P3: a page address the way the ads carry it -- `src=gads` added to its query (Google appends gclid itself),
    unless it is there already. `resources.final_url` equals this of the page's address while the ads point at
    that page (page-check reads it so, A3p-2 Task 4)."""
    parts = urlsplit(url)
    if ("src", "gads") in parse_qsl(parts.query):
        return parts.geturl()
    return urlunsplit(parts._replace(query=f"{parts.query}&src=gads" if parts.query else "src=gads"))


def _page_url(conn: psycopg.Connection, biz: str) -> str:
    """P3: the published page's address as the ads carry it (`ads_url`)."""
    return ads_url(landing.current(conn, biz)["url"])


def _target(
    conn: psycopg.Connection, settings: Settings, biz: str, plan: dict, knowledge: dict, http: httpx.Client
) -> ads_launch.Target:
    """P4: a proximity circle around the listing's point, else each service-area name's best Google location (one
    `geoTargetConstants:suggest` per name, its first suggestion); neither -> ToolError(NO_LOCATION). Ruling S10: what
    Google answered for a set of names -- nothing included -- is kept in `resources.geo_cache` and asked again only
    when the names change, so an area Google cannot place is asked about once, not every hour (and `launch` skips
    its lookup for it too: `_unplaceable`)."""
    if ads_run.has_point(knowledge):
        radius = (plan["sections"]["consultation"].get("geo") or {}).get("radius_miles")
        if not radius:
            raise ToolError(ads_run.NO_RADIUS)
        listing = knowledge["listing"]
        return ads_launch.Target(
            kind="proximity",
            lat=float(listing["lat"]),
            lng=float(listing["lng"]),
            radius_miles=float(radius),
            geo_targets=(),
        )
    names = _area_names(knowledge)
    cache = (ads_run.get(conn, biz, CHANNEL) or {}).get("resources", {}).get("geo_cache") or {}
    if cache.get("names") == sorted(names):
        found = list(cache.get("targets") or [])
    else:
        found = []
        for name in names:
            for geo in google_ads.suggest_geo_targets(settings, [name], http=http)[:1]:
                if geo not in found:
                    found.append(geo)
        ads_run.remember(conn, biz, CHANNEL, {"geo_cache": {"names": sorted(names), "targets": found}})
    if not found:
        raise ToolError(ads_run.NO_LOCATION)
    return ads_launch.Target(kind="locations", lat=None, lng=None, radius_miles=None, geo_targets=tuple(found))


def _unplaceable(conn: psycopg.Connection, biz: str, knowledge: dict) -> bool:
    """Google already answered that it cannot place these service-area names (ruling S10's cache, same names)."""
    if ads_run.has_point(knowledge):
        return False
    cache = (ads_run.get(conn, biz, CHANNEL) or {}).get("resources", {}).get("geo_cache") or {}
    return cache.get("names") == sorted(_area_names(knowledge)) and not cache.get("targets")


def _account_rows(conn: psycopg.Connection, biz: str, customer_id: str) -> ads_launch.Account:
    """The `conversion_action` rows of this account (a row stored for an account the business linked before is
    not this one's)."""
    rows = conn.execute("SELECT kind, resource_name, label FROM conversion_action WHERE business_id = %s", (biz,))
    own = f"customers/{customer_id}"
    tag, actions = None, {}
    for r in rows.fetchall():
        if r["kind"] == "tag" and r["resource_name"] == own:
            tag = r["label"]
        elif r["kind"] != "tag" and r["resource_name"].startswith(own + "/"):
            actions[r["kind"]] = r["resource_name"]
    return ads_launch.Account(customer_id=customer_id, tag=tag, conversion_actions=actions)


def _account(
    conn: psycopg.Connection, settings: Settings, biz: str, customer_id: str, http: httpx.Client
) -> ads_launch.Account:
    """The account's tag and conversion actions for the call asset; `ads_conversions.ensure` first while the tag or
    any of its `KINDS` rows is missing (A3 Task 2 normally ran it when the link became active; A3 final review I1:
    under `validate` it stores only the tag, so the first live step is what creates the actions -- `ensure` is
    idempotent by name), keeping the account's time zone it reads along (ruling S14)."""
    account = _account_rows(conn, biz, customer_id)
    if account.tag is None or any(kind not in account.conversion_actions for kind in ads_conversions.KINDS):
        _keep_zone(conn, biz, ads_conversions.ensure(conn, settings, biz, customer_id, http=http).get("time_zone"))
        account = _account_rows(conn, biz, customer_id)
    return account


def _keep_zone(conn: psycopg.Connection, biz: str, zone: object) -> None:
    """The account's time zone kept on the row, the first time a step reads the customer (it never changes)."""
    if isinstance(zone, str) and zone:
        ads_run.remember(conn, biz, CHANNEL, {"time_zone": zone})


def _operations(
    conn: psycopg.Connection, settings: Settings, build: Build, customer_id: str, http: httpx.Client
) -> list[dict]:
    """The create mutate for a build: the location target and the account's conversion setup, resolved with Google
    now (a lookup that finds the campaign already built asks for neither), then `ads_launch.operations` (pure)."""
    biz, plan, knowledge = build.payload["business_id"], build.payload["plan"], build.payload["knowledge"]
    return ads_launch.operations(
        _search_view(plan),
        knowledge,
        page_url=build.payload["page_url"],
        target=_target(conn, settings, biz, plan, knowledge, http),
        account=_account(conn, settings, biz, customer_id, http),
        customer_id=customer_id,
        dropped=build.payload.get("dropped") or ads_launch.NOTHING_DROPPED,
    )


def _thin_ad(ops: list[dict]) -> bool:
    """Whether `ops` would create an ad with fewer lines than Google takes (plans.MIN_HEADLINES headlines,
    MIN_DESCRIPTIONS descriptions) -- `update`'s guard (ruling L2, final review): the lines Google refused for the
    business stay out of every later version's build, and an update is never reviewed."""
    for op in ops:
        ad = ((op.get("adGroupAdOperation") or {}).get("create") or {}).get("ad") or {}
        rsa = ad.get("responsiveSearchAd")
        if rsa is not None and (
            len(rsa.get("headlines") or []) < plans.MIN_HEADLINES
            or len(rsa.get("descriptions") or []) < plans.MIN_DESCRIPTIONS
        ):
            return True
    return False


# ---------------------------------------------------------------- reading the campaign back (P8)


def _gaql_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _gaql_like(value: str) -> str:
    """A LIKE pattern matching `value` literally: GAQL escapes its wildcards and brackets by bracketing them."""
    return _gaql_text("".join(f"[{ch}]" if ch in "[]%_" else ch for ch in value))


_CAMPAIGN = (
    "SELECT campaign.resource_name, campaign.name, campaign.status, campaign_budget.resource_name, "
    "campaign_budget.amount_micros FROM campaign WHERE "
)


def _found(row: dict) -> dict:
    campaign, budget = row.get("campaign") or {}, row.get("campaignBudget") or {}
    amount = budget.get("amountMicros")
    return {
        "campaign": campaign.get("resourceName"),
        "status": campaign.get("status"),
        "budget": budget.get("resourceName"),
        "amount": int(amount) if amount is not None else None,
    }


def _find_by_name(settings: Settings, customer_id: str, biz: str, http: httpx.Client) -> dict | None:
    """P8's lookup before any create: the business's campaign not removed, found by the business id every name
    `ads_launch.campaign_name` builds ends with -- so a business name that changed since the create still finds it."""
    where = f"campaign.name LIKE '{_gaql_like('Admiry · ')}%{_gaql_like(biz)}' AND campaign.status != 'REMOVED'"
    for row in google_ads.search(settings, customer_id, _CAMPAIGN + where, http=http):
        name = (row.get("campaign") or {}).get("name") or ""
        if name == f"Admiry · {biz}" or name.endswith(f" · {biz}"):
            found = _found(row)
            if found["campaign"] and found["status"] != "REMOVED":
                return found
    return None


def _find_by_resource(settings: Settings, customer_id: str, campaign: str, http: httpx.Client) -> dict | None:
    """The stored campaign as Google has it now (status, budget amount); None once it is removed there."""
    where = f"campaign.resource_name = '{_gaql_text(campaign)}'"
    for row in google_ads.search(settings, customer_id, _CAMPAIGN + where, http=http):
        found = _found(row)
        if found["campaign"] == campaign and found["status"] != "REMOVED":
            return found
    return None


def _read_resources(settings: Settings, customer_id: str, found: dict, http: httpx.Client) -> dict:
    """A campaign found by name (its create's response was lost) read back into `ads_launch.resources_from`'s
    shape, so a later `diff` can address it: ad groups and their ads and keywords by theme (an ad group is named
    after its theme), negatives by text, the location criteria, the call asset, and (A3p-2 H9) the final URL every
    ad already carries -- without it, `diff` cannot tell a page that has since moved from one it has not, and treats
    "unknown" as "moved" (refreshing an ad nobody asked for) rather than the other way around. Four searches, only
    on recovery."""
    where = f"campaign.resource_name = '{_gaql_text(found['campaign'])}'"
    out: dict = {
        "campaign": found["campaign"],
        "campaign_budget": found["budget"],
        "ad_groups": {},
        "ads": {},
        "keywords": {},
        "negatives": {},
        "geo_criteria": [],
    }
    ads = google_ads.search(
        settings,
        customer_id,
        "SELECT ad_group.resource_name, ad_group.name, ad_group_ad.resource_name, ad_group_ad.ad.final_urls "
        "FROM ad_group_ad WHERE " + where + " AND ad_group_ad.status = 'ENABLED' AND ad_group.status != 'REMOVED'",
        http=http,
    )
    for row in ads:
        group, ad = row.get("adGroup") or {}, row.get("adGroupAd") or {}
        if group.get("name") and group.get("resourceName"):
            out["ad_groups"][group["name"]] = group["resourceName"]
            if ad.get("resourceName"):
                out["ads"][group["name"]] = ad["resourceName"]
            # H9: every ad a recovered campaign has was built with the same page_url; any one confirms it,
            # so a later diff can tell whether a published page's address has moved since.
            final_urls = (ad.get("ad") or {}).get("finalUrls")
            if final_urls:
                out["final_url"] = final_urls[0]
    keywords = google_ads.search(
        settings,
        customer_id,
        "SELECT ad_group.resource_name, ad_group.name, ad_group_criterion.resource_name, "
        "ad_group_criterion.keyword.text FROM ad_group_criterion WHERE "
        + where
        + " AND ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.negative = FALSE "
        "AND ad_group_criterion.status != 'REMOVED'",
        http=http,
    )
    for row in keywords:
        group, criterion = row.get("adGroup") or {}, row.get("adGroupCriterion") or {}
        text = (criterion.get("keyword") or {}).get("text")
        if group.get("name") and text and criterion.get("resourceName"):
            out["ad_groups"].setdefault(group["name"], group.get("resourceName"))
            out["keywords"].setdefault(group["name"], {})[text] = criterion["resourceName"]
    criteria = google_ads.search(
        settings,
        customer_id,
        "SELECT campaign_criterion.resource_name, campaign_criterion.type, campaign_criterion.negative, "
        "campaign_criterion.keyword.text FROM campaign_criterion WHERE "
        + where
        + " AND campaign_criterion.status != 'REMOVED'",
        http=http,
    )
    for row in criteria:
        criterion = row.get("campaignCriterion") or {}
        name, kind = criterion.get("resourceName"), criterion.get("type")
        text = (criterion.get("keyword") or {}).get("text")
        if name and kind == "KEYWORD" and criterion.get("negative") and text:
            out["negatives"][text] = name
        elif name and kind in ("PROXIMITY", "LOCATION") and not criterion.get("negative"):
            out["geo_criteria"].append(name)
    assets = google_ads.search(
        settings,
        customer_id,
        "SELECT campaign_asset.resource_name, campaign_asset.asset FROM campaign_asset WHERE "
        + where
        + " AND campaign_asset.field_type = 'CALL' AND campaign_asset.status != 'REMOVED'",
        http=http,
    )
    for row in assets[:1]:
        link = row.get("campaignAsset") or {}
        if link.get("resourceName"):
            out["campaign_asset"] = link["resourceName"]
            out["call_asset"] = link.get("asset")
    return out


def _merge(resources: dict, ops: list[dict], responses: list[dict]) -> dict:
    """`resources` after a `diff` mutate: what it created added (keywords and ads under an ad group that already
    existed keyed by its theme too), what it removed dropped; an ad group paused for a theme that left the plan
    stays (Google keeps it, paused). `final_url` (A3p-2 H9) moves to whatever this mutate's own ads just carried,
    when it touched any -- a diff with no ad in it (a budget-only change, say) leaves the stored one alone, since
    that is still what every ad Google has actually carries."""
    fresh = ads_launch.resources_from(responses, ops, ad_groups=resources.get("ad_groups"))
    removed = {
        body["remove"]
        for op in ops
        for body in op.values()
        if isinstance(body, dict) and isinstance(body.get("remove"), str)
    }
    merged = dict(resources)
    merged["negatives"] = {
        text: name for text, name in (resources.get("negatives") or {}).items() if name not in removed
    } | fresh["negatives"]
    merged["geo_criteria"] = [n for n in resources.get("geo_criteria") or [] if n not in removed] + fresh[
        "geo_criteria"
    ]
    merged["ad_groups"] = {**(resources.get("ad_groups") or {}), **fresh["ad_groups"]}
    merged["ads"] = {**(resources.get("ads") or {}), **fresh["ads"]}
    keywords = {theme: dict(words) for theme, words in (resources.get("keywords") or {}).items()}
    for theme, words in fresh["keywords"].items():
        keywords.setdefault(theme, {}).update(words)
    merged["keywords"] = keywords
    if "final_url" in fresh:
        merged["final_url"] = fresh["final_url"]
    return merged


def _status_op(campaign: str, status: str) -> list[dict]:
    return [{"campaignOperation": {"update": {"resourceName": campaign, "status": status}, "updateMask": "status"}}]


def _mark_creating(conn: psycopg.Connection, biz: str, plan_id: str | None) -> None:
    """`resources.creating_plan_id`: the plan version a create sent (and possibly applied) builds; None clears it."""
    if plan_id is None:
        ads_run.forget(conn, biz, CHANNEL, "creating_plan_id")
    else:
        ads_run.remember(conn, biz, CHANNEL, {"creating_plan_id": plan_id})


# What a whole new `resources` keeps of this adapter's own bookkeeping on the row: the geo cache (ruling S10) and the
# account's time zone (S14) -- read from the row when the new one is handed back, since a step may have just
# written them. A create in flight, a refused version's mark and sync's issues go with the old one.
_KEPT = ("geo_cache", "time_zone")
# `diff` reads the target only when the two versions' locations differ (ads_launch.geo_key)
_SAME_PLACE = ads_launch.Target(kind="locations", lat=None, lng=None, radius_miles=None, geo_targets=())


def _with_kept(conn: psycopg.Connection, biz: str, resources: dict) -> dict:
    row = ads_run.get(conn, biz, CHANNEL) or {}
    kept = {k: v for k, v in (row.get("resources") or {}).items() if k in _KEPT}
    return {k: v for k, v in resources.items() if k not in _KEPT} | kept


# ---------------------------------------------------------------- sync's reads (A3 Task 5)


_METRICS = (
    "SELECT segments.date, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, "
    "metrics.phone_calls FROM campaign WHERE "
)
_ZONE = "SELECT customer.time_zone FROM customer"
_STATUS = "SELECT campaign.status, campaign.primary_status, campaign.primary_status_reasons FROM campaign WHERE "
_POLICY = (
    "SELECT ad_group_ad.resource_name, ad_group_ad.policy_summary.approval_status, "
    "ad_group_ad.policy_summary.review_status, ad_group_ad.policy_summary.policy_topic_entries "
    "FROM ad_group_ad WHERE "
)


def _day_row(r: dict) -> MetricDay | None:
    """One `_METRICS` result row -> `metric_daily`'s own shape (Google's phone calls are its `calls`); None for a
    row with no `segments.date` (defensive: every real row has one)."""
    seg, m = r.get("segments") or {}, r.get("metrics") or {}
    day = seg.get("date")
    if not day:
        return None
    return MetricDay(
        day=date.fromisoformat(day),
        cost_micros=int(m.get("costMicros") or 0),
        clicks=int(m.get("clicks") or 0),
        impressions=int(m.get("impressions") or 0),
        calls=int(m.get("phoneCalls") or 0),
        conversions=float(m.get("conversions") or 0),
    )


def _ad_issues(rows: list[dict]) -> list[dict]:
    """Disapproved ads from the `_POLICY` search: its resource name and Google's own topic/type pairs, kept
    exactly as Google wrote them (get_status_issues quotes them verbatim; nothing here is invented)."""
    out: list[dict] = []
    for r in rows:
        ad = r.get("adGroupAd") or {}
        summary = ad.get("policySummary") or {}
        if summary.get("approvalStatus") != "DISAPPROVED" or not ad.get("resourceName"):
            continue
        topics = [
            {"topic": t.get("topic"), "type": t.get("type")}
            for t in summary.get("policyTopicEntries") or []
            if isinstance(t, dict) and t.get("topic")
        ]
        out.append({"resource_name": ad["resourceName"], "topics": topics})
    return out


def _campaign_state(row: dict, status_rows: list[dict]) -> tuple[str, str | None, list[str]]:
    """(state, sync's own note, campaign-level reasons) for a row not currently `paused` (the caller handles that
    case itself): `ENABLED`+`ELIGIBLE` -> `live`; primary status `PAUSED` -- someone paused it directly in
    Google, since Admiry's own pause would already have this row `paused` -- -> `limited`, said plainly; any
    other primary status -> `limited` with Google's own reasons kept for get_status_issues. No status row at
    all (the campaign query answered nothing where one always should) changes nothing: the row keeps reading
    as it did rather than a `None` overwriting a real earlier error."""
    if not status_rows:
        return row["state"], row.get("last_error"), []
    campaign = status_rows[0].get("campaign") or {}
    status, primary = campaign.get("status"), campaign.get("primaryStatus")
    reasons = [r for r in campaign.get("primaryStatusReasons") or [] if isinstance(r, str)]
    if status == "ENABLED" and primary == "ELIGIBLE":
        return "live", None, []
    if primary == "PAUSED":
        return "limited", PAUSED_IN_GOOGLE, []
    return "limited", None, reasons


def _sync_note(current: str | None, note: str | None) -> str | None:
    """Ruling S15: sync sets, and clears, only its own notes (SYNC_NOTES). Any other `last_error` -- a change Google
    refused, a retry, a precheck's sentence -- is another step's to clear, and stays."""
    return current if current is not None and current not in SYNC_NOTES else note


def _time_zone(
    conn: psycopg.Connection, settings: Settings, biz: str, customer_id: str, resources: dict, http: httpx.Client
) -> object:
    """Ruling S14: the account's time zone -- kept on the row once a step has read the customer (the launch's
    conversion setup reads it along), else asked of Google here, once, and kept."""
    zone = resources.get("time_zone")
    if not zone:
        rows = google_ads.search(settings, customer_id, _ZONE, http=http)
        zone = (rows[0].get("customer") or {}).get("timeZone") if rows else None
        _keep_zone(conn, biz, zone)
    return zone


# ---------------------------------------------------------------- the adapter


class GoogleSearchAdapter:
    channel = CHANNEL
    words = WORDS

    def capabilities(self) -> Capabilities:
        return Capabilities(
            create=True,
            update=True,
            pause=True,
            metrics=True,
            conversions=False,  # the conversion upload is A3p-3
            leads=False,
            modes={"own_account", "managed"},  # managed: a pool account of Admiry's, by industry (A3c-1)
            min_monthly_usd=100,
            managed_min_monthly_usd=200,  # founder 2026-10-03: prepaid, cold start in Admiry's account, from $200
            geo="radius",
            lead_formats={"call", "form"},
            needs_image=False,
            validate="server",  # validateOnly
            follows_page=True,  # the ads carry the page's address (`final_url`) and move with it (A3p-2)
        )

    def settles_from(self, conn: psycopg.Connection, business_id: str) -> datetime | None:
        """A3c-1, wallet-settle's hook: when Admiry first ran the business's Google Search in a pool account of its own
        (`google_managed.first_bound`) -- the balance pays the days from then on; a day before it was the owner's own
        account's, with Google. None (never bound): no day is the balance's."""
        return google_managed.first_bound(conn, business_id)

    def connect_steps(self, account: Account | None, mode: str) -> list[Step]:
        if mode == "managed":
            return [Step(CONNECT_MANAGED)]
        url = f"{config.load().web_base_url}/?setup={account.business_id}" if account is not None else None
        return [Step(CONNECT_OWN_ACCOUNT, url)]

    def check_account(self, conn: psycopg.Connection, settings: Settings, account: Account | None) -> AccountStatus:
        """Whether the business's Google Ads account can carry ads now. A prepaid business first (A3c-1; the account
        row's mode, or `business.ads_mode`, Google Search's mode for one more release, P6): ready with a balance above
        zero -- the account is Admiry's, as NewsBreak's is, and the balance is checked here as NewsBreak checks it, so
        a business prepaid by `ads_mode` alone is held to it too: `active` once the launch picked one from the pool,
        `none` before (a launch while no account has room waits on ACCOUNT_OPENING, `_customer_id`); its Customer ID
        is shown only when the account is the business's alone (capacity 1, as the prepayment addendum v0 promises) --
        an account shared by an industry is Admiry's, and never named to an owner. Otherwise the `ads_link` row, the
        source of truth for Google: active or an operator's `manual` is ready, pending waits for the owner's
        acceptance, anything else (or no row, or no account) is not linked. The Customer ID is shown with dashes
        wherever there is a row, as the statement shows it."""
        if account is not None and _prepaid(conn, account.business_id, account):
            bound = google_managed.get(conn, account.business_id)
            status = "active" if bound else "none"
            pooled = google_managed.account(conn, bound.customer_id) if bound else None
            alone = pooled is not None and pooled.capacity == 1 and pooled.live == 1  # never two, even after a --force
            public = bound.display if alone else None  # the business's alone
            if wallet.balance_cents(conn, account.business_id) <= 0:  # as NewsBreak's: whatever the row's mode says
                no_funds = ads_run.NO_FUNDS.format(label=registry.label(CHANNEL))
                return AccountStatus(ready=False, status=status, reason=no_funds, account_public=public)
            if bound is None and not wallet.has_paid(conn, account.business_id):  # a credit alone: no account yet
                return AccountStatus(ready=False, status=status, reason=FIRST_PAYMENT, account_public=None)
            return AccountStatus(ready=True, status=status, reason=None, account_public=public)
        link = google_ads_link.get(conn, account.business_id) if account is not None else None
        if link is None:
            return AccountStatus(ready=False, status="none", reason=NOT_LINKED, account_public=None)
        if link.live:
            return AccountStatus(ready=True, status=link.owner_status, reason=None, account_public=link.display)
        reason = LINK_PENDING if link.status == "pending" else NOT_LINKED
        return AccountStatus(ready=False, status=link.status, reason=reason, account_public=link.display)

    def refusals(
        self, conn: psycopg.Connection, settings: Settings, business_id: str, plan: dict, knowledge: dict
    ) -> list[str]:
        """Every one of Google Search's own rules the plan fails, in the order `precheck` asks them (L1, spec 2026-10-10
        §4.1): a plan it can run (alone: nothing else is asked of one it cannot), enough ad text after the copy linter,
        then -- nothing built yet -- a phone for the call asset and a place to centre on, or -- ads built -- the place
        rule only when the plan's own location differs from the version built (that is when `update` resolves a new
        location). Database only (P11, P4; ruling S8)."""
        try:
            _search_channel(plan)
        except ToolError as e:
            return [str(e)]
        consultation = plan["sections"]["consultation"]
        out = [TOO_LITTLE_COPY] if copy_short(plan) else []
        row = ads_run.get(conn, business_id, CHANNEL)
        if row is not None and "campaign" in row["resources"]:
            running = plans.get(conn, business_id, row["plan_id"])
            if running is None or ads_launch.geo_key(running) != ads_launch.geo_key(plan):
                out += _where_refusals(consultation, knowledge)
            return out
        if len("".join(c for c in knowledge.get("phone") or "" if c.isdigit())) < 10:
            out.append(ads_run.NO_PHONE)
        return out + _where_refusals(consultation, knowledge)

    def precheck(
        self, conn: psycopg.Connection, settings: Settings, business_id: str, plan: dict, knowledge: dict
    ) -> None:
        """Google Search's own rules beyond the lifecycle's: `refusals`' first, refused."""
        found = self.refusals(conn, settings, business_id, plan, knowledge)
        if found:
            raise ToolError(found[0])

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
        """The inputs of a create or an update, from the database: the plan, the knowledge, the page's address with
        `src=gads` (P3) -- read once, here, and kept as `Build.page_url` without it, the address the lifecycle's
        write after the step compares the page with (A3p-2 H22). What needs Google -- the location of service-area
        names, the account's conversion setup -- is resolved by `launch` and `update` when they need it, after the
        name lookup (P8: a campaign already there is never built, nor asked about, twice)."""
        ch = _search_channel(plan)
        geo = plan["sections"]["consultation"].get("geo") or {}
        page_url = landing.current(conn, business_id)["url"]
        return Build(
            channel=CHANNEL,
            plan_id=plan["id"],
            payload={
                "business_id": business_id,
                "plan": plan,
                "knowledge": knowledge,
                "page_url": ads_url(page_url),
                # spec 2026-10-11 §5 (plan D3, ruling L2): what every Google build of the business leaves out -- a
                # line Google refused, by its exact text, whichever version it was refused in -- read once, here;
                # never by editing the version the owner agreed to
                "dropped": ads_refusal.dropped(conn, business_id, settings=settings),
            },
            summary={
                "daily_usd": google_ads.from_micros(ads_launch.daily_micros(ch["monthly_usd"])),
                "geo_text": (
                    f"within {geo['radius_miles']:g} miles"
                    if ads_run.has_point(knowledge) and geo.get("radius_miles")
                    else ", ".join(_area_names(knowledge))
                ),
                "ads": len(ch.get("themes") or []),
            },
            page_url=page_url,
        )

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
        """`validate_only` (P5): the create mutate, validateOnly -- Google checks every operation and builds nothing.
        Otherwise P7/P8: the one create mutate (everything PAUSED), or -- when the lookup finds the business's
        campaign, a response lost earlier -- its resources read back instead. The plan a create builds is written
        to the row before it is sent (`resources.creating_plan_id`), so a campaign adopted later is recorded as the
        version that built it (`Resources.plan_id`), never as a newer one approved meanwhile: the enable then
        applies the newer one first. `Resources.seen` is the campaign as Google has it (status, budget amount).
        Ruling Y1: an approved plan whose ad text an active block rule leaves fewer than `plans.MIN_HEADLINES`
        headlines or `plans.MIN_DESCRIPTIONS` descriptions is never sent -- PlatformError(TOO_LITTLE_COPY), not
        retried, nothing created -- the same guard `update` already carries."""
        try:
            return self._launch(conn, settings, build, existing, http, validate_only)
        except google_ads.AdsError as e:
            raise _platform(e) from None

    def _launch(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        build: Build,
        existing: dict,
        http: httpx.Client,
        validate_only: bool,
    ) -> Resources:
        biz, plan, knowledge = build.payload["business_id"], build.payload["plan"], build.payload["knowledge"]
        customer_id = _customer_id(conn, biz, settings=settings)
        if validate_only:
            ops = _operations(conn, settings, build, customer_id, http)
            google_ads.mutate(settings, customer_id, ops, validate_only=True, http=http)
            return Resources(ids={})
        built = existing.get("creating_plan_id")  # an earlier attempt's (its answer lost?)
        if built is None and _unplaceable(conn, biz, knowledge):
            raise ToolError(ads_run.NO_LOCATION)  # no create was ever sent (nothing to find), and none can be built
        found = _find_by_name(settings, customer_id, biz, http)
        if found is None:
            ops = _operations(conn, settings, build, customer_id, http)
            if _thin_ad(ops):  # ruling Y1: a block rule that turned active after the plan was approved can leave
                raise PlatformError(TOO_LITTLE_COPY, retryable=False)  # the create too thin too; nothing is sent
            _mark_creating(conn, biz, plan["id"])
            try:
                responses = google_ads.mutate(settings, customer_id, ops, validate_only=False, http=http)
            except google_ads.AdsError as e:
                if not _retryable(e):  # refused whole: this attempt built nothing, so the earlier mark stands
                    _mark_creating(conn, biz, built)
                if _duplicate_name(e):
                    found = _find_by_name(settings, customer_id, biz, http)  # an earlier attempt's (or another hand's)
                if found is None:
                    raise
            else:
                resources = ads_launch.resources_from(responses, ops)
                if resources.get("campaign") and resources.get("campaign_budget"):
                    return Resources(
                        ids=_with_kept(conn, biz, {**resources, "customer_id": customer_id}),
                        seen={"campaign": resources["campaign"], "status": "PAUSED", "amount": _daily(plan)},
                    )
                found = _find_by_name(settings, customer_id, biz, http)  # this call built it but named nothing
                if found is None:
                    raise google_ads.AdsError(google_ads.UNREACHABLE)
                built = plan["id"]
        # a campaign no attempt of Admiry's is known to have sent (built by hand under Admiry's name): taken as this
        # plan's; the enable still sets its budget to the plan's
        read = _read_resources(settings, customer_id, found, http)
        return Resources(
            ids=_with_kept(conn, biz, {**read, "customer_id": customer_id}), plan_id=built or plan["id"], seen=found
        )

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
        """P7's second call, the money rule: the budget set to the approved `plan`'s before (in the same mutate as)
        the campaign is turned on, so a campaign is never enabled with a budget over the plan the owner approved.
        An owner's pause that landed while the step ran (ruling S17: it never waits for the lock) is final: the row
        is read again right before anything is turned on, and a paused one is left off. `seen` is the campaign as
        the create step just saw it; None reads it by its stored resource name (PlatformError(GONE) once it was
        removed in Google Ads by hand: an operator decides what happens to it)."""
        biz = account.business_id
        try:
            customer_id = _built_in(conn, biz, resources)
            campaign = resources["campaign"]
            found = seen
            if found is None:
                found = _find_by_resource(settings, customer_id, campaign, http)
                if found is None:
                    raise PlatformError(GONE, retryable=False)
            ops: list[dict] = []
            daily = _daily(plan)
            if found.get("amount") != daily:
                ops.append(
                    {
                        "campaignBudgetOperation": {
                            "update": {"resourceName": resources["campaign_budget"], "amountMicros": daily},
                            "updateMask": "amountMicros",
                        }
                    }
                )
            if found.get("status") != "ENABLED":
                ops += ads_launch.enable(campaign)
            if ops:
                if ads_run.get(conn, biz, CHANNEL)["state"] == "paused":  # ruling S17: read again right before
                    return
                google_ads.mutate(settings, customer_id, ops, validate_only=False, http=http)
        except google_ads.AdsError as e:
            raise _platform(e) from None

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
        """P10: the newer approved plan (`build`'s) onto the resources `old_plan` built, in two mutates (ruling
        S16). The money first: `ads_launch.money`, the plan's budget and CPC ceiling, sent every time -- idempotent,
        and what keeps Google's budget the last approved plan's even when Google refuses the rest, or refused an
        earlier version's rest after taking its money. Then the structure: `diff` without the money, when the
        versions differ in anything else Google keeps. A location target is resolved only when the two versions'
        locations differ (ruling S8: a budget change never waits on Google placing the service area again). A
        structure whose new ad would carry fewer lines than Google takes is never sent (ruling L2, final review): the
        lines Google refused for the business are left out of every later version, and an update is never reviewed,
        so a version that still carries one can be left too thin -- PlatformError(TOO_LITTLE_COPY), not retried, after
        the money; the ads keep running the version they have."""
        biz, plan, knowledge = build.payload["business_id"], build.payload["plan"], build.payload["knowledge"]
        try:
            customer_id = _built_in(conn, biz, resources)
            if ads_launch.geo_key(old_plan) != ads_launch.geo_key(plan):
                target = _target(conn, settings, biz, plan, knowledge, http)
            else:
                target = _SAME_PLACE
            view = _search_view(plan)
            structure = ads_launch.diff(
                _search_view(old_plan),
                view,
                resources,
                page_url=build.payload["page_url"],
                target=target,
                account=_account_rows(conn, biz, customer_id),
                customer_id=customer_id,
                with_money=False,
                dropped=build.payload.get("dropped") or ads_launch.NOTHING_DROPPED,
            )
            money = ads_launch.money(view, resources)
            google_ads.mutate(settings, customer_id, money, validate_only=False, http=http)
            if _thin_ad(structure):
                raise PlatformError(TOO_LITTLE_COPY, retryable=False)
            if structure:
                responses = google_ads.mutate(settings, customer_id, structure, validate_only=False, http=http)
                resources = _merge(resources, structure, responses)
        except google_ads.AdsError as e:
            raise _platform(e) from None
        return Resources(ids=_with_kept(conn, biz, resources))

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
        """A3p-2 H9/H26/H27: the running ads moved to `build`'s page address and nothing else -- the running plan
        diffed against itself (identical content and money on both sides, so the only operations `diff` can make
        are a new ad at the new address, the old one paused), the location never resolved again. `with_money`
        (a URL-only update of the approved version, ruling S16: every update sends the money first) sends the
        running plan's budget and CPC ceiling first, idempotent; without it (H26/S15: the approved version is one
        Google refused, and the running one's money is in already) nothing but the address goes. A refusal of the
        money is the plan's (`PlatformError`); a refusal of the address mutate is `AddressRefused`, never the
        plan's (H27 S5b) -- the lifecycle records the address and pauses ads left on a page that is down. Ruling Y1:
        a block rule that turned active since the running ad was built, and now leaves the fresh ad fewer than
        `plans.MIN_HEADLINES` headlines or `plans.MIN_DESCRIPTIONS` descriptions, is never sent -- PlatformError(
        TOO_LITTLE_COPY), after the money, nothing but that goes."""
        biz = account.business_id
        try:
            customer_id = _built_in(conn, biz, resources)
            view = _search_view(running_plan)
            structure = ads_launch.diff(
                view,
                view,
                resources,
                page_url=build.payload["page_url"],
                target=_SAME_PLACE,
                account=_account_rows(conn, biz, customer_id),
                customer_id=customer_id,
                with_money=False,
                # ruling L2: what every Google build of the business leaves out -- a line Google refused, by its
                # exact text -- stays out of the fresh ad at the new address, whichever version it was refused in
                dropped=ads_refusal.dropped(conn, biz, settings=settings),
            )
            if with_money:
                google_ads.mutate(
                    settings, customer_id, ads_launch.money(view, resources), validate_only=False, http=http
                )
        except google_ads.AdsError as e:
            raise _platform(e) from None
        if _thin_ad(structure):  # ruling Y1: a block rule turned active after the running ad was built can leave
            raise PlatformError(TOO_LITTLE_COPY, retryable=False)  # the fresh ad at the new address too thin
        if structure:
            try:
                responses = google_ads.mutate(settings, customer_id, structure, validate_only=False, http=http)
            except google_ads.AdsError as e:
                if _retryable(e):
                    raise _platform(e) from None
                raise AddressRefused(str(e)) from None
            resources = _merge(resources, structure, responses)
        return Resources(ids=_with_kept(conn, biz, resources))

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
        """The campaign's status: PAUSED (an owner's pause, or one owed, ruling S11 -- to the stored campaign, or to
        the one the name lookup finds when none is stored, and nothing when Google has none) or ENABLED
        (`validate_only`: a resume under the `validate` switch, which only Google checks). A PAUSED Google refuses
        outright is read against Google's own listing of the campaign (M1 follow-up fix round 1, F1): one it lists as
        removed, or not at all -- removed by hand, so it takes no change -- serves nothing, and the stop is done; any
        other keeps the refusal. Never keyed on Google's error code, never a mark on the row: the listing decides each
        time."""
        biz = account.business_id
        try:
            customer_id = _built_in(conn, biz, resources)
            campaign = resources.get("campaign")
            if campaign is None and paused:
                found = _find_by_name(settings, customer_id, biz, http)
                campaign = found["campaign"] if found else None
            if campaign is not None:
                status = "PAUSED" if paused else "ENABLED"
                try:
                    google_ads.mutate(
                        settings, customer_id, _status_op(campaign, status), validate_only=validate_only, http=http
                    )
                except google_ads.AdsError as e:
                    refused_stop = paused and not validate_only and not _retryable(e)
                    if not refused_stop or _find_by_resource(settings, customer_id, campaign, http) is not None:
                        raise
        except google_ads.AdsError as e:
            raise _platform(e) from None

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
        """Cost, clicks, impressions, calls and conversions per day for the last `days` days, named by the account's
        own calendar (ruling S14: GAQL has no LAST_3_DAYS, so `segments.date BETWEEN` today-(days-1) and today in
        `resources.time_zone`, read from Google once when the row does not have it yet). Google's own failures
        propagate: the ads-sync job isolates them per business."""
        biz = account.business_id
        customer_id, campaign = _built_in(conn, biz, resources), resources["campaign"]
        where = f"campaign.resource_name = '{_gaql_text(campaign)}'"
        zone = _time_zone(conn, settings, biz, customer_id, resources, http)
        today = ads_run.now().astimezone(ads_run.zone(zone)).date()
        span = (
            f" AND segments.date BETWEEN '{(today - timedelta(days=days - 1)).isoformat()}' AND '{today.isoformat()}'"
        )
        found = (_day_row(r) for r in google_ads.search(settings, customer_id, _METRICS + where + span, http=http))
        return [d for d in found if d is not None]

    def pull_issues(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        row: dict,
        *,
        http: httpx.Client | None = None,
    ) -> Serving:
        """Google's `campaign.status`/`primary_status`/`primary_status_reasons` mapped to the row's state (rulings
        S11/S12, `_campaign_state`), sync's own notes the only `last_error` it sets or clears (S15), and disapproved
        ads' policy topics for `resources.issues.ads`. An owed pause is the sweeper's alone to finish and a
        `launching` row is PAUSED by design (P7): for either the status query is not sent. A paused row stays
        paused whatever Google says; one Google serves anyway gets a note (S12: never silently live)."""
        biz, resources = account.business_id, row["resources"]
        customer_id = _built_in(conn, biz, resources)
        where = f"campaign.resource_name = '{_gaql_text(resources['campaign'])}'"
        ads_issues = _ad_issues(google_ads.search(settings, customer_id, _POLICY + where, http=http))
        campaign_reasons: list[str] = []
        if resources.get("pause_owed") or row["state"] == "launching":
            state, last_error = row["state"], row["last_error"]
        elif row["state"] == "paused":
            status_rows = google_ads.search(settings, customer_id, _STATUS + where, http=http)
            mismatch = bool(status_rows) and (status_rows[0].get("campaign") or {}).get("status") != "PAUSED"
            state, last_error = "paused", _sync_note(row["last_error"], ENABLED_IN_GOOGLE if mismatch else None)
        else:
            status_rows = google_ads.search(settings, customer_id, _STATUS + where, http=http)
            state, note, campaign_reasons = _campaign_state(row, status_rows)
            last_error = _sync_note(row["last_error"], note)
        issues = {}
        if campaign_reasons:
            issues["campaign"] = campaign_reasons
        if ads_issues:
            issues["ads"] = ads_issues
        return Serving(state=state, last_error=last_error, issues=issues)

    def push_conversions(
        self, settings: Settings, account: Account, events: list[ConversionEvent], *, http: httpx.Client | None = None
    ) -> UploadResult:
        raise Unsupported("Google Search's conversion upload is A3p-3")

    def pull_leads(
        self, settings: Settings, account: Account, since: datetime, *, http: httpx.Client | None = None
    ) -> list[LeadIn]:
        raise Unsupported("Google Search has no lead forms to pull")

    def landing_url(self, page_url: str) -> str:
        return ads_url(page_url)

    def carried_url(self, resources: dict) -> str | None:
        return resources.get("final_url")

    def client(self, *, budget_s: float | None = None) -> httpx.Client:
        return client(budget_s=budget_s)

    def ops_sent(self, http: httpx.Client) -> int:
        return ops_sent(http)


ADAPTER = registry.register(GoogleSearchAdapter())
