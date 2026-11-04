"""The channel framework's shared types and the adapter protocol (spec 2026-09-29-admiry-channels-design §5.2; plan
2026-09-29-agent-m0-channel-framework P3). `agent/ads_run.py` speaks only to this protocol, so a channel is a
directory, not a code path. A capability that is False is honest: the lifecycle never calls the method, the card
never offers the action, and the allocator never proposes a channel whose `create` is false. Report-only adapters
implement `connect_steps`, `check_account` and `pull_metrics`.

`build` is not pure at this boundary (P3): Google's reads the page URL (database), and its `launch` / `update`
resolve the rest after the name lookup -- the account's conversion setup (its rows; `conversions.ensure` while one is
missing) and, without a listing point, the location through one `geoTargetConstants:suggest` call (HTTP). The pure
layer stays in each channel's own `build.py` (`operations()`, `diff()`), golden-tested; the adapter's methods are the
glue that feeds it.

The execution half takes the connection too (M0 Task 4): a platform's steps keep their own bookkeeping on the row
between calls -- Google's writes the create it is about to send before sending it, reads the row again right before
it turns anything on (an owner's pause never waits for a lock), keeps the account's time zone and what Google said
about a service area -- so a lost answer is recovered and never repeated (A3 P7, P8, S10, S11, S14, S17). The
lifecycle (`agent/ads_run.py`) owns the row's state, plan version, dates and owner sentence, and its own keys in
`resources` (`paused_by`, `owner_paused`, `pause_owed`, `refused_plan_id`, `month_cap_at`, `stop_owed`, and for a
channel whose ads follow the business's page, `url_stale` and `refused_url`); every other key there is the adapter's.

The page the ads send people to (A3p-2, M0 Task 9): an adapter whose `Capabilities.follows_page` is true carries the
business's page address on its ads and can move them to a new one. It implements three hooks the lifecycle calls only
then: `landing_url` (the page's address as its ads carry it), `carried_url` (the address the built ads carry now, from
the row's `resources`) and `move_page` (the running ads moved to the page's address, nothing else changed; a refusal
of the address itself is `AddressRefused`). The others raise `Unsupported` there and the lifecycle never asks."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict

from ..config import Settings

with warnings.catch_warnings():
    # The spec names the field below `validate`, which shadows pydantic's deprecated BaseModel.validate (removed in v3).
    # On an instance the field wins (caps.validate == "server"); nothing here calls the old classmethod.
    warnings.filterwarnings("ignore", message='Field name "validate"', category=UserWarning)

    class Capabilities(BaseModel):
        """What one adapter can do (§5.2). A misspelt or unknown declaration is refused, not dropped."""

        model_config = ConfigDict(extra="forbid")

        create: bool
        update: bool
        pause: bool
        metrics: bool
        conversions: bool
        leads: bool
        modes: set[Literal["own_account", "managed"]]
        min_monthly_usd: int  # the floor the allocator respects
        # the floor when Admiry runs the channel from a balance, when higher (A3c-1, founder 2026-10-03: Google Search
        # prepaid from $300, an account of Admiry's per business); `floor` reads the right one for a mode
        managed_min_monthly_usd: int | None = None
        geo: Literal["radius", "zip", "city", "metro"]
        lead_formats: set[Literal["call", "form", "message"]]
        needs_image: bool = False  # NewsBreak, Facebook & Instagram, Nextdoor: an approved image before launch (D89)
        validate: Literal["server", "dry"] = "dry"  # Google: validateOnly; others: build and record, call nothing
        page_rules: tuple[str, ...] = ()  # landing-check rule ids beyond BLOCKING that this channel needs (M1)
        # the ads carry the business's page address and the adapter can move them (A3p-2's page follow, M0 Task 9):
        # `landing_url`, `carried_url` and `move_page` are implemented, and the page's moves and outages reach them
        follows_page: bool = False

        def floor(self, mode: str | None) -> int:
            """The least a month the channel takes run the business's way (`mode`; None: the owner's own account)."""
            if mode == "managed" and self.managed_min_monthly_usd is not None:
                return self.managed_min_monthly_usd
            return self.min_monthly_usd


@dataclass(frozen=True)
class Account:
    """A business's account on one channel (a `channel_account` row). `secret` is the owner-side token, decrypted;
    None when there is none or it cannot be read. It never appears in repr, logs or errors."""

    business_id: str
    channel: str
    mode: str  # own_account | managed
    status: str  # none | pending | active | refused | inactive | needs_attention
    external: dict  # the platform's ids: customer_id, ad_account_id, page_id, ...
    secret: str | None = field(repr=False)


@dataclass(frozen=True)
class AccountStatus:
    ready: bool
    status: str
    reason: str | None  # the card's one sentence when not ready
    account_public: str | None  # the account id the owner recognises, for the card and the statement


@dataclass(frozen=True)
class Step:
    """One owner-facing step of connecting a channel: text and, when there is one, where to do it."""

    text: str
    url: str | None = None


@dataclass(frozen=True)
class Build:
    channel: str
    plan_id: str
    payload: dict  # the adapter's own (Google Search: business_id, plan, knowledge, page_url)
    summary: dict  # owner-facing: {"daily_usd", "geo_text", "ads": n}
    # the page's own address this build was made against, as `landing.current` read it (no tracking parameter) --
    # what the lifecycle's write after a step compares the page with at write time (A3p-2 H22); None for an adapter
    # whose ads do not follow the page
    page_url: str | None = None


@dataclass(frozen=True)
class Resources:
    """What a launch or an update leaves on the platform, as `channel_campaign.resources` keeps it: `ids` is all of
    it, the adapter's own keys (the lifecycle keeps its own beside them), and names the top object under `campaign` --
    that key is how the lifecycle knows ads are built. `plan_id` is the plan version they were built from when it is not the
    one asked for (a campaign found by name after a lost answer is the version whose create sent it); `seen` is the
    top object as the platform showed it just now (Google: its status and budget), so the enable that follows needs
    no second read."""

    ids: dict
    plan_id: str | None = None
    seen: dict | None = None


@dataclass(frozen=True)
class Serving:
    """What a sync's read of the platform says (A3 §6, per channel): the row's `state` and `last_error` after it --
    the read's own notes set or cleared, a sentence another step left kept (A3 S15) -- and the platform's `issues`,
    quoted as it wrote them, for `resources.issues`. `repause` (M1 Task 5b): the platform serves ads the row shows
    paused (switched on there) -- on a channel the owner cannot reach (Admiry runs it), the lifecycle owes the platform
    the pause again (`resources.pause_owed`) and the hourly ads-launch sends it, never a note alone."""

    state: str
    last_error: str | None
    issues: dict
    repause: bool = False


# The monthly statement's billing sentence for a channel Admiry runs from the business's balance (`managed`), from its
# label (D76): the lifecycle's `statement` says it for every managed account, whatever the channel's own
# `Words.no_fee` says for the owner's own account (M1 follow-up 2, W2) -- and NewsBreak's `no_fee` is it
MANAGED_NO_FEE = "{label}'s cost is taken from your balance at cost; Admiry charges no fee"


@dataclass(frozen=True)
class Words:
    """A platform in the owner's words (D52, no jargon) for the lifecycle's own sentences about the ads there."""

    pause_owed: str  # the platform was not told of a pause yet (A3 S11)
    refused_change: str  # the platform did not take all of the latest change (A3 S15)
    checked: str  # the audit note of a `validate` run
    checked_status: str  # the status line of ads checked under `validate`, not running yet
    checked_badge: str  # the plan card's badge for them
    held_back: str  # a `limited` row's status line
    repaused: str  # the audit note of an owed pause the platform took
    no_fee: str  # the monthly statement's billing sentence for an own account (a managed one's: MANAGED_NO_FEE)
    # A3p-2 H28, for a channel that follows the page: the platform would not take the page's address ({host}) for
    # the ads, which keep the page they had
    refused_address: str | None = None
    # A3p-2 H33: sync's note on a row someone paused by hand on the platform itself -- no pause of Admiry's claims
    # such a row and no resume of Admiry's turns it on
    paused_elsewhere: str | None = None
    # the platform's own "did not answer; Admiry retries within the hour" sentence (a URL move's skip clears it)
    retrying: str | None = None
    # M1 Task 7, for a channel Admiry runs (NewsBreak's), where M0's clauses would make the platform's work read as the
    # owner's: a row waiting for its account at the platform -- not for funds -- in the platform's words (never
    # "waiting for your account"; as the row's `last_error` too, on a `queued` row the launch found waiting, it is the
    # whole clause and badge, said once: fix round 1); the hourly read's note on a `limited` row when the platform
    # shows ads Admiry runs switched off -- the row's whole clause, never "live" beside it -- and the plan's badge for
    # that row (`paused_badge`, fix round 1); and its note on a `live` row whose month at the platform Admiry could not
    # set -- said as it is, never as "your latest change is not all in yet"
    account_wait: str | None = None
    paused_there: str | None = None
    paused_badge: str | None = None
    month_note: str | None = None
    # M1 Task 7 fix round 1: a `waiting_creative` row's clause and badge -- a plan funds a channel that shows a picture
    # only with one, so it waits only when the picture approved with the plan left Admiry's set: nothing the owner can
    # supply (None: M0's "waiting for your image")
    image_wait: str | None = None


class PlatformError(Exception):
    """A platform call that did not do what was asked, as the lifecycle needs it: str(e) is the owner's sentence for
    the row (never the platform's own words, never a secret); `retryable` is False when the platform refused the
    request itself, so the same request would be refused again -- a launch fails, an update of ads already running
    is marked refused, and neither is tried again. `details` (spec 2026-10-11 §4): what the platform refused, one
    entry each, as its adapter read them (Google Search: client.RefusalDetail) -- the Reviewer acts on them; `()` for
    a platform or a failure that names nothing."""

    def __init__(self, sentence: str, *, retryable: bool, details: tuple = ()) -> None:
        super().__init__(sentence)
        self.retryable = retryable
        self.details = tuple(details)


class AddressRefused(PlatformError):
    """`move_page`'s refusal of the page address itself (A3p-2 H27): the platform would not take the address for the
    ads, which keep the one they had. Never a refusal of the plan -- the lifecycle records the address
    (`resources.refused_url`), never the plan version, and never tries that address again while the page stays at
    it."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence, retryable=False)


@dataclass(frozen=True)
class MetricDay:
    day: date
    cost_micros: int
    impressions: int
    clicks: int
    calls: int
    conversions: float


@dataclass(frozen=True)
class Issue:
    kind: str  # disapproval | billing | verification | account | suspended
    platform_text: str  # the platform's own sentence, quoted verbatim
    resource: str | None = None


@dataclass(frozen=True)
class ConversionEvent:
    lead_id: str
    kind: str
    at: datetime
    click_id: str | None
    value_usd: float | None


@dataclass(frozen=True)
class UploadResult:
    accepted: int
    rejected: int
    note: str | None


@dataclass(frozen=True)
class LeadIn:
    external_id: str
    at: datetime
    fields: dict


class Unsupported(Exception):
    """The adapter does not do this: a report-only channel asked to build, a channel without lead forms asked to
    pull leads. The lifecycle checks the capability first and never calls; this is for the caller that forgot."""


@runtime_checkable
class Adapter(Protocol):
    """One place the ads run. The lifecycle calls the execution half only for what `capabilities()` says the
    adapter does, under the business's step lock, with the client `client()` made (M0 Task 4)."""

    channel: str
    words: Words  # the platform in the owner's words, for the lifecycle's sentences about it

    def capabilities(self) -> Capabilities: ...

    def connect_steps(self, account: Account | None, mode: str) -> list[Step]: ...

    def check_account(self, conn: psycopg.Connection, settings: Settings, account: Account | None) -> AccountStatus: ...

    def precheck(  # the channel's own launch rules beyond the lifecycle's; ToolError with one owner sentence
        self, conn: psycopg.Connection, settings: Settings, business_id: str, plan: dict, knowledge: dict
    ) -> None: ...

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
    ) -> Build: ...

    def launch(  # create paused; `existing` is the row's resources, so a lost response can be recovered
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        build: Build,
        existing: dict,
        *,
        http: httpx.Client | None = None,
        validate_only: bool,
    ) -> Resources: ...

    def enable(  # turn on what `launch` built, at the approved plan's budget; `seen` saves reading it again
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        plan: dict,
        resources: dict,
        *,
        http: httpx.Client | None = None,
        seen: dict | None = None,
    ) -> None: ...

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
    ) -> Resources: ...

    def set_paused(  # `validate_only`: the platform checks the change and makes none
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        resources: dict,
        paused: bool,
        *,
        http: httpx.Client | None = None,
        validate_only: bool = False,
    ) -> None: ...

    def pull_metrics(  # the last `days` days of the account's own calendar, today included
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        resources: dict,
        days: int,
        *,
        http: httpx.Client | None = None,
    ) -> list[MetricDay]: ...

    def pull_issues(  # `row` is the channel_campaign row as the sync read it
        self,
        conn: psycopg.Connection,
        settings: Settings,
        account: Account,
        row: dict,
        *,
        http: httpx.Client | None = None,
    ) -> Serving: ...

    def push_conversions(
        self, settings: Settings, account: Account, events: list[ConversionEvent], *, http: httpx.Client | None = None
    ) -> UploadResult: ...

    def pull_leads(
        self, settings: Settings, account: Account, since: datetime, *, http: httpx.Client | None = None
    ) -> list[LeadIn]: ...

    # the page the ads send people to -- called only when `capabilities().follows_page` (M0 Task 9)
    def landing_url(self, page_url: str) -> str: ...  # the page's address as this channel's ads carry it

    def carried_url(self, resources: dict) -> str | None: ...  # the address the built ads carry; None unknown

    def move_page(  # the running ads onto `build`'s page address, nothing else changed; AddressRefused on a refusal
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
    ) -> Resources: ...

    def client(self, *, budget_s: float | None = None) -> httpx.Client: ...  # the adapter's own client

    def ops_sent(self, http: httpx.Client) -> int: ...  # what `http` sent in the platform's quota unit; 0 uncounted
