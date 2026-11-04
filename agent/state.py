"""The one read model (spec 2026-09-29-admiry-channels-design D102, §4, §7.1, §7.2; plan
2026-09-29-agent-m0-channel-framework P10, Task 6): `build` reads what the business's cards, tools and views show --
the latest plan's cards, where the ads run on each channel, the platforms' open issues -- into one versioned
`MarketingState`, and every reader renders that object: the homepage's `state` action returns `cards()`, `GET
/api/state` serves the whole state, the MCP channels view draws its rows, and the tools return slices of it. A
reader never computes a number the model does not have.

`connections` is the account side of the same read, for every registered channel in card order: Settings' "Where
your ads run", the setup cards' steps and `get_connection_status`.

Nothing here writes. Whether an account can carry ads is the lifecycle's own answer (`ads_run.account_status`); an
adapter may open its account row while it is asked (NewsBreak's does, the first time a plan funds it), so that
answer is read inside a transaction that is rolled back. An account's token (`Account.secret`) never enters the
model. `money`, `report`, `refund_request` and `next_report_at` stay None in M0: M1m and M6 fill them."""

from __future__ import annotations

from datetime import date

import psycopg
from pydantic import BaseModel

from . import ads_mode, ads_run, business, config, landing, plans, plans_channels, site_install
from .channels import registry
from .channels.base import Account, AccountStatus
from .util import iso

GOOGLE = "google_search"
PLAN_PARTS = ("plan", "budget", "economics", "rationale", "ads_preview")  # plans.cards()'s parts, as it returns them
SPENDING = ("live", "limited")  # the rows the card and Settings offer Pause for


class ChannelRow(BaseModel):
    """One channel the business's ads run on (its `channel_campaign` row), as the channels card shows it (spec
    §7.2) and `get_channel_status` returns it (§7.1)."""

    channel: str
    label: str  # the owner's name for it (D76)
    mode: str  # own_account | managed
    account_status: str  # its channel_account row's: none | pending | active | refused | inactive | needs_attention
    account_public: str | None  # the account id the owner can look up on the platform (Google: the Customer ID)
    state: str  # channel_campaign.state
    state_text: str  # the card's one sentence: "Live", "Waiting for funds", "Paused by you on Tuesday", ...
    sentence: str  # the whole owner line (ads_run.status_sentence): Settings, and get_channel_status's `say`
    today_usd: float | None  # spent today, while something is built
    day_cap_usd: float | None  # today's budget: the channel's monthly amount / 30.4
    month_usd: float  # spent so far this month
    month_cap_usd: float | None  # the channel's monthly amount in the approved plan (ads_run.month_amount)
    leads_7d: int  # the leads it brought this week (the lead store)
    calls_7d: int  # the calls the platform counted this week
    issues: list[dict]  # the platform's words: open channel_issue rows, the reasons and ad reviews a sync read
    actions: list[str]  # pause | resume | connect
    # The plan version this place's ads come from (founder, 2026-10-01): the one it runs or, for a place that has
    # not started, the one approved for it. Reading that version shows the ad text, also while a newer version of
    # the plan waits to be launched
    plan_id: str | None = None
    plan_version: int | None = None


class Connection(BaseModel):
    """A business's account on one registered channel (spec §4): Settings' row, a setup card's step and one entry of
    `get_connection_status`."""

    channel: str
    label: str
    mode: str
    status: str  # its channel_account row's (Google Search's mirrors the link); none without a row
    account_public: str | None
    checked_at: str | None  # when Admiry last checked the account
    ready: bool  # it can carry ads now (ads_run.account_status)
    reason: str | None  # why not, in the owner's words
    note: str | None  # what the last check left on the row (its last_error)
    steps: list[dict]  # how the owner connects it (adapter.connect_steps): [{"text", "url"}]
    next_step: str | None  # what it waits for while it is not ready (`_next_step`): the steps' words, or its reason

    def public(self) -> dict:
        """The entry get_connection_status returns (spec §4 item 4)."""
        return self.model_dump(
            include={"channel", "label", "mode", "status", "account_public", "checked_at", "next_step"}
        )


class MarketingState(BaseModel):
    """Everything the owner's cards show, in one versioned object (D102). Not closed: a later plan adds its own key
    (the support session's `feedback`) without breaking this one."""

    version: int = 1
    business: dict  # {id, name, verified, claim_state}
    plan: dict | None  # plans.cards()'s parts for the latest plan, unchanged
    budget: dict | None
    economics: dict | None
    rationale: dict | None
    ads_preview: dict | None
    plan_id: str | None
    execution: str | None  # the "Live since …" line, once a plan was approved
    channels: list[ChannelRow]
    issues: list[dict]  # the platforms' open issues, every channel
    money: dict | None = None  # M1m
    report: dict | None = None  # M6
    refund_request: dict | None = None  # M1m
    next_report_at: date | None = None  # M6

    def cards(self) -> dict:
        """`data.cards` as the homepage and the MCP views render it (spec §7.2): the plan's parts, the channels card,
        the money and the report."""
        return {
            **{part: getattr(self, part) for part in PLAN_PARTS},
            "channels": channels_card(self.channels, self.next_report_at),
            "money": self.money,
            "report": self.report,
        }


def channels_card(rows: list[ChannelRow], next_report_at: date | None = None) -> dict:
    """The channels card (spec §7.2) as `cards()` and get_channel_status give it: its rows, and a footer of the month
    so far against the channels' monthly amounts and the next report's date."""
    caps = [r.month_cap_usd for r in rows if r.month_cap_usd is not None]
    return {
        "rows": [r.model_dump(mode="json") for r in rows],
        "footer": {
            "month_usd": round(float(sum(r.month_usd for r in rows)), 2),
            "month_cap_usd": round(float(sum(caps)), 2) if caps else None,
            "next_report_at": next_report_at.isoformat() if next_report_at else None,
        },
    }


# ---------------------------------------------------------------- reads


def _readiness(conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str) -> AccountStatus:
    """`ads_run.account_status` -- the lifecycle's own "can this account carry ads now" -- as a pure read: whatever
    the adapter writes on the way is rolled back."""
    with conn.transaction() as tx:
        status = ads_run.account_status(conn, settings, biz, channel)
        raise psycopg.Rollback(tx)
    return status


def mode_of(conn: psycopg.Connection, biz: str, channel: str, account: Account | None) -> str:
    """How the channel runs: its account row's mode (`account`); before there is one, Google Search's is the
    business's (`ads_mode.current`, P6) and any other channel's is its default (`plans.default_mode`)."""
    if account is not None:
        return account.mode
    return ads_mode.current(conn, biz) if channel == GOOGLE else plans.default_mode(channel)


def steps(biz: str, channel: str, mode: str, account: Account | None) -> list[dict]:
    """How the owner connects the channel that way (the adapter's `connect_steps`), asked about the account row
    (`account`) or the one the business would have (nothing stored): [{"text", "url"}]."""
    asked = account or Account(business_id=biz, channel=channel, mode=mode, status="none", external={}, secret=None)
    return [{"text": s.text, "url": s.url} for s in registry.adapter(channel).connect_steps(asked, mode)]


def nothing_to_connect(how: list[dict]) -> bool:
    """No step of the channel's (`steps`) carries a link to follow -- NewsBreak's, or Google Search's while Admiry runs
    it: the owner has nothing to connect there, so what it waits for is its account's own reason (M1 follow-up 2, W3),
    as `get_connection_status`'s next step and the setup card's line under the step say it."""
    return not any(s["url"] for s in how)


def waiting_for(ready: bool, reason: str | None, how: list[dict]) -> str | None:
    """What a channel with nothing to connect (`nothing_to_connect(how)`) waits for while its account is not ready: the
    account's own reason (M1 follow-up 2, W3: NewsBreak's funds, NewsBreak getting the account ready -- never "Add
    funds" with money on the balance). The one source of `Connection.next_step`'s text for such a channel, the setup
    card's `reason` line and connect_channel's `next_step` (fix round 1, F2); None while ready, or with a link."""
    return reason if not ready and nothing_to_connect(how) else None


def account_waits(
    conn: psycopg.Connection, settings: config.Settings, biz: str, channel: str, how: list[dict]
) -> str | None:
    """`waiting_for` the business's account on `channel` as it stands now, read as `connections` reads it
    (`_readiness`: rolled back) -- connect_channel's `next_step` (fix round 1, F2); None for steps with a link to follow
    (nothing is read then)."""
    if not nothing_to_connect(how):
        return None
    status = _readiness(conn, settings, biz, channel)
    return waiting_for(status.ready, status.reason, how)


def _next_step(status: AccountStatus, how: list[dict]) -> str | None:
    """What the channel waits for while its account is not ready (`Connection.next_step`): the steps' words when one
    carries a link to follow (the owner's own account to connect), else its account's own reason (`waiting_for`)."""
    if status.ready:
        return None
    return waiting_for(status.ready, status.reason, how) or " ".join(s["text"] for s in how) or None


def _open_issues(conn: psycopg.Connection, biz: str) -> list[dict]:
    """The platforms' open issues (`channel_issue`, not resolved), in card order, newest first within a channel."""
    found = conn.execute(
        "SELECT channel, kind, platform_text, since FROM channel_issue WHERE business_id = %s AND resolved_at IS NULL "
        "ORDER BY array_position(%s::text[], channel) NULLS LAST, channel, since DESC, id DESC",
        (biz, list(registry.ORDER)),
    ).fetchall()
    return [
        {
            "channel": r["channel"],
            "label": registry.label(r["channel"]),
            "kind": r["kind"],
            "platform_text": r["platform_text"],
            "since": iso(r["since"]),
        }
        for r in found
    ]


def _entry(plan: dict | None, channel: str) -> dict | None:
    """The channel's entry in a plan version's ads (funded or not)."""
    entries = (((plan or {}).get("sections") or {}).get("ads") or {}).get("channels") or []
    return next((c for c in entries if c.get("channel") == channel), None)


def _state_text(row: dict, label: str, account_status: str, platform: list[dict]) -> str:
    """The card's one sentence (spec §7.2): a platform's open issue in its own words, quoted; "Launching" until the
    ads are on -- but for a refused row whose stop is owed (`stop_owed`), which says the lifecycle's clause (the
    refusal and that Admiry is switching the ads off: M1 follow-up 2, W5); the invitation the owner has not accepted
    yet; else the lifecycle's own clause for the row (`ads_run.state_clause`: "Live", "Waiting for funds", "Paused by
    you on Tuesday", a channel's own `Words`)."""
    if platform:
        return f'Needs attention: "{platform[0]["platform_text"]}"'
    state, owed = row["state"], (row.get("resources") or {}).get("stop_owed")
    if (state == "launching" and not owed) or (
        state == "queued" and not row.get("last_error") and not row.get("validated_at")
    ):
        return "Launching"
    if state == "waiting_account" and account_status == "pending":
        return f"Waiting for you to accept the {label} invitation"
    clause = ads_run.state_clause(row)
    return clause[:1].upper() + clause[1:]


def _actions(row: dict, how: list[dict]) -> list[str]:
    """What the card offers on the row: Pause while the ads spend; Resume only where the owner's own pause is the
    only one (a pause of Admiry's is Admiry's to lift, and a Resume would only answer PAUSED_BY_ADMIRY); Connect
    while the row waits for an account the owner can connect somewhere (a step, `how`, with an address)."""
    state = row["state"]
    if state in SPENDING:
        return ["pause"]
    if state == "paused":
        return ["resume"] if ads_run.owner_paused(row) and not row.get("paused_reason") else []
    if state == "waiting_account" and any(s["url"] for s in how):
        return ["connect"]
    return []


def _channel_row(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    row: dict,
    platform: list[dict],
    approved: dict | None,
) -> ChannelRow:
    """One row of the card: the ads of the version the row runs (its headline for an ad's issue), the month against
    the approved plan's amount (`ads_run.month_amount`, spend-guard's cap: one source, final review I3)."""
    channel = row["channel"]
    label = registry.label(channel)
    account = registry.account(conn, biz, channel)
    mode = mode_of(conn, biz, channel, account)
    status = _readiness(conn, settings, biz, channel)
    running = plans.get(conn, biz, row["plan_id"])
    entry = _entry(running, channel)
    today = ads_run.today_spend(conn, biz, channel, row)
    read = (row.get("resources") or {}).get("issues") or {}
    headline = ((entry or {}).get("headlines") or [None])[0]
    issues = (
        [{"kind": i["kind"], "platform_text": i["platform_text"], "since": i["since"]} for i in platform]
        + [{"kind": "serving", "platform_text": reason} for reason in read.get("campaign") or []]
        + [
            {"kind": "disapproval", "headline": headline, "topics": ad.get("topics") or []}
            for ad in read.get("ads") or []
        ]
    )
    sentence = ads_run.status_sentence(row, today=today, account=account)
    if read.get("ads"):
        sentence = f"{sentence.rstrip('.')}. {label} needs a change to one of your ads."
    account_status = account.status if account else "none"
    return ChannelRow(
        channel=channel,
        label=label,
        mode=mode,
        account_status=account_status,
        account_public=status.account_public,
        state=row["state"],
        state_text=_state_text(row, label, account_status, platform),
        sentence=sentence,
        today_usd=today["spent_usd"] if today else None,
        day_cap_usd=today["budget_usd"] if today else None,
        month_usd=ads_run.month_spend(conn, biz, channel, row),
        month_cap_usd=ads_run.month_amount(approved, channel),
        leads_7d=ads_run.week_leads(conn, biz, channel, row),
        calls_7d=ads_run.week_totals(conn, biz, channel, row)["calls"],
        issues=issues,
        actions=_actions(row, steps(biz, channel, mode, account)),
        plan_id=row["plan_id"],
        plan_version=running["version"] if running else None,
    )


def _rows(
    conn: psycopg.Connection, settings: config.Settings, biz: str, rows: list[dict], issues: list[dict]
) -> list[ChannelRow]:
    """A row per channel with a `channel_campaign` row, in card order; a channel no adapter runs is left out."""
    approved = ads_run.approved(conn, biz)
    return [
        _channel_row(conn, settings, biz, row, [i for i in issues if i["channel"] == row["channel"]], approved)
        for row in rows
        if registry.known(row["channel"])
    ]


def channel_rows(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> list[ChannelRow]:
    """Where the business's ads run, one row per channel (the channels card, get_channel_status, Settings)."""
    return _rows(conn, settings, business_id, ads_run.rows(conn, business_id), _open_issues(conn, business_id))


def next_report_day(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> date | None:
    """When the owner's next report goes out -- tomorrow morning, or the next Monday for a weekly reader -- while the
    daily digest is on for the business: the one source of `MarketingState.next_report_at` and of every channels
    card's footer. None otherwise (plan 2026-10-07-agent-k2 Task 14)."""
    from . import digest  # digest reaches modules that import this one: imported when asked

    return digest.next_report_at(conn, settings, business_id)


def report(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> dict | None:
    """`MarketingState.report`: the latest owner's digest of the last days, while the digest is on for the business."""
    from . import digest

    return digest.report_for_state(conn, settings, business_id)


def channels(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> dict:
    """The channels card on its own -- the same rows and footer as `build(...).cards()["channels"]`, without the
    plan's cards: get_channel_status and the channels view in the chat."""
    return channels_card(channel_rows(conn, settings, business_id), next_report_day(conn, settings, business_id))


def connections(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> list[Connection]:
    """The business's account on every registered channel, in card order (spec §4): Settings' "Where your ads run",
    the setup cards' steps and get_connection_status. A channel whose switch is off is left out unless the business
    already has its account row or a row of its ads there (final review I2; `plans_channels.switched_on`: Google
    Search always). `registry.account` reads the account's token with it (and decrypts it); the token never enters a
    Connection."""
    stored = {
        r["channel"]: r
        for r in conn.execute(
            "SELECT channel, status, checked_at, last_error FROM channel_account WHERE business_id = %s",
            (business_id,),
        ).fetchall()
    }
    running = {r["channel"] for r in ads_run.rows(conn, business_id)}
    listed = [
        c
        for c in registry.ORDER
        if registry.known(c) and (plans_channels.switched_on(settings, c) or c in stored or c in running)
    ]
    out = []
    for channel in listed:
        account = registry.account(conn, business_id, channel)
        mode = mode_of(conn, business_id, channel, account)
        status = _readiness(conn, settings, business_id, channel)
        how = steps(business_id, channel, mode, account)
        row = stored.get(channel) or {}
        out.append(
            Connection(
                channel=channel,
                label=registry.label(channel),
                mode=mode,
                status=row.get("status") or "none",
                account_public=status.account_public,
                checked_at=iso(row.get("checked_at")),
                ready=status.ready,
                reason=status.reason,
                note=row.get("last_error"),
                steps=how,
                next_step=_next_step(status, how),
            )
        )
    return out


def build(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> MarketingState:
    """Every read the cards need, in one place (D102): the business, the latest plan's cards (a plan from a
    consultation; the ad preview's host from the page on file), the "Live since" line once a plan was approved,
    a row per channel and the platforms' open issues."""
    biz = business.row(conn, business_id)
    knowledge = (business.latest_knowledge(conn, business_id) or (None, {}))[1]
    name = plans.name_for(knowledge)  # a name or null, never an address: only the cards' sentence centres on one
    latest = plans.latest(conn, business_id)
    parts: dict = dict.fromkeys(PLAN_PARTS)
    if latest is not None and "consultation" in latest["sections"]:
        parts = plans.cards(
            {**latest, "business_name": name or plans.address_for(knowledge)},
            final_host=landing.final_host(conn, business_id),
            website=site_install.row(conn, business_id),  # the plan's "Your website" row (A3p-5 batch 2 Q5)
        )
    rows = ads_run.rows(conn, business_id)
    issues = _open_issues(conn, business_id)
    return MarketingState(
        business={
            "id": business_id,
            "name": name,
            "verified": biz["claim_state"] == "verified",
            "claim_state": biz["claim_state"],
        },
        **parts,
        plan_id=latest["id"] if latest else None,
        execution=ads_run.execution_line(rows) if latest and latest["execution"] else None,
        channels=_rows(conn, settings, business_id, rows, issues),
        issues=issues,
        report=report(conn, settings, business_id),
        next_report_at=next_report_day(conn, settings, business_id),
    )
