"""The channel tools (spec 2026-09-29-admiry-channels-design §4, §7.1; plan 2026-09-29-agent-m0-channel-framework
Task 6): `connect_channel` (W) connects a business's account on one channel -- Google Search's invitation path, which
`tools_ads.connect_google_ads` is an alias of, or the step the owner takes on the web -- and four reading tools (RO,
database only), each a slice of the one read model (`agent/state.py`, D102): `get_connection_status` (every
registered channel's account), `get_channel_status` (a row per channel, drawn in the chat by `ui://admiry/channels`),
`get_status_issues` (the platforms' own words, per channel) and `get_platform_statement` (the monthly statement).
Google is called in run()'s prepare, outside the transaction."""

from __future__ import annotations

import re
from datetime import date

from fastmcp.exceptions import ToolError

from . import (
    ads_mode,
    ads_run,
    agreements,
    business,
    config,
    envelope,
    google_ads_link,
    launch_check,
    plans,
    setup,
    state,
    wallet,
)
from .channels import registry
from .mcp_app import CHANNELS_VIEW, agent_tool, need_business, run

GOOGLE = "google_search"
MODES = ("own_account", "managed")
# What connect_channel's answer carries as `status` (the public connect_account's; the public contract lists them):
# the account is linked, Google's invitation waits for the owner, the agreement comes first, the step is the owner's
# to take on the web, or -- no way chosen yet (founder's ruling, 2026-10-13) -- the owner chooses how the ads run, or
# -- no page settled yet (the same day's ruling: the page comes before the money) -- the owner chooses the page first
CONNECT_STATUSES = ("connected", "pending", "needs_agreement", "web_step", "choose", "page_first")
CONNECTED, PENDING, NEEDS_AGREEMENT, WEB_STEP, CHOOSE, PAGE_FIRST = CONNECT_STATUSES
SETUP_KEY = "a setup key cannot connect an account; the owner finishes setup at {base}/connect first"
BAD_MODE = "mode must be own_account or managed"
BAD_MONTH = "month must be a calendar month like 2026-09"
MONTH = re.compile(r"(\d{4})-(\d{2})")
MONTH_YEARS = range(2020, 2101)  # a statement's sane years: nothing ran before Admiry, and every next month exists
NEEDS_AGREEMENT_NOTE = (
    "the owner accepts the Google Ads Management Agreement on connect_url (the Admiry conversation opens at the setup "
    "step); then call again with a new idempotency_key"
)
NO_ADS_YET = "You have no ads yet."
NO_ISSUES = "No issues from any platform right now."
NO_STATEMENT = "Nothing has run yet, so there is no statement."


# ---------------------------------------------------------------- connect_channel


def connect(
    channel: str,
    idempotency_key: str,
    *,
    mode: str | None = None,
    account_id: str | None = None,
    tool: str = "connect_channel",
    args: dict | None = None,
) -> dict:
    """`connect_channel`, run under `tool`'s name and audit `args` (the Google alias keeps its own). Google Search
    with a Customer ID runs A3's invitation path whatever way the business runs, unless `mode` asks for the prepaid
    way itself: the link already live for that ID is `connected`; otherwise `check_invite` decides -- a prepaid
    business is refused there (NOT_OWN_ACCOUNT, as the alias always was), the agreement gate answers
    `needs_agreement`, then the invitation is sent (`pending`). Without an ID the answer is the step for the way the
    business runs (its own account: give the ID; prepaid: nothing, Admiry's own account, A3c-1) -- `connected` when
    its own link is live -- unless the owner has chosen no way yet (`asks_way`, the founder's ruling of 2026-10-13):
    then nothing changes and the answer is `choose`, the question to put to the owner and both ways side by side
    (ads_mode.WAY_QUESTION, WAY_CHOICES), neither marked first. Every other channel is the step the owner takes on
    the web (`web_step`): NewsBreak's "nothing to connect". A channel with nothing to connect that is not ready also
    says what it waits for (`next_step`, `state.account_waits`: get_connection_status's own words -- NewsBreak's
    funds, or NewsBreak getting the account ready; M1 follow-up 2 fix round 1, F2), for the way it runs. While the
    latest draft has no page settled (launch_check.page_first; the same day's ruling: the page comes before the
    money), an answer that would be `choose` or `web_step` is `page_first` instead: nothing changes, and the answer
    is the page question and its ways, none marked first -- or, a page made and not published yet, the step that
    publishes it (launch_check.page_ask). Refused: a
    setup key, an unknown channel, a mode the channel does not run."""
    args = (
        args
        if args is not None
        else {"channel": channel, "mode": mode, "account_id": account_id, "idempotency_key": idempotency_key}
    )

    def prepare(conn, p):
        found = step(conn, p)
        if found["status"] in (CHOOSE, WEB_STEP) and launch_check.page_first(conn, need_business(p)):
            # the page comes first (the founder's ruling, 2026-10-13): nothing toward the way or the money yet
            return {"status": PAGE_FIRST, "channel": channel, "label": registry.label(channel)}
        return found

    def step(conn, p):
        if p.key.cls == "draft":
            raise ToolError(SETUP_KEY.format(base=config.load().web_base_url))
        biz = need_business(p)
        adapter = registry.adapter(channel)  # ToolError("unknown channel")
        if mode is None and account_id is None and asks_way(conn, biz, channel):
            return {"status": CHOOSE, "channel": channel, "label": registry.label(channel)}
        account = registry.account(conn, biz, channel)
        runs = state.mode_of(conn, biz, channel, account)
        want = mode or runs  # the way asked for, else the one it runs
        if want not in MODES:
            raise ToolError(BAD_MODE)
        if want not in adapter.capabilities().modes and channel != GOOGLE:  # Google runs both ways (A3c-1)
            raise ToolError(ads_run.MODE_SOON.format(label=registry.label(channel)))
        how = state.steps(biz, channel, want, account)
        out = {"channel": channel, "label": registry.label(channel), "steps": how}
        # F2: what a channel with nothing to connect waits for while it is not ready, as get_connection_status says it
        # -- for the way it runs: another way asked about is no account the business has yet
        waits = state.account_waits(conn, config.load(), biz, channel, how) if want == runs else None
        if waits:
            out["next_step"] = waits
        if channel != GOOGLE or mode == "managed" or (account_id is None and want != "own_account"):
            return {**out, "status": WEB_STEP}
        link = google_ads_link.get(conn, biz)
        if link is not None and link.live and (account_id is None or _same(account_id, link.customer_id)):
            return {**out, "status": CONNECTED, "account_public": link.display, "customer_id": link.display}
        if account_id is None:
            return {**out, "status": WEB_STEP}
        try:  # check_invite's own order: the claim, the way, the agreement, then the number
            cid = google_ads_link.check_invite(conn, biz, account_id)
        except google_ads_link.LinkRefused as e:
            if e.code == "needs_agreement":
                return {**out, "status": NEEDS_AGREEMENT}
            raise ToolError(str(e)) from e
        settings = config.load()
        held = google_ads_link.held(conn, biz, cid)
        try:  # the connection is idle here: no transaction is open
            resource = google_ads_link.send(settings, cid, held=held)
        except google_ads_link.LinkRefused as e:  # already linked to Admiry, not by this business
            raise ToolError(str(e)) from e
        except google_ads_link.LinkError as e:
            google_ads_link.record_error(conn, biz, cid, str(e))
            raise ToolError(str(e)) from e
        return {**out, "status": PENDING, "customer_id": cid, "resource_name": resource}

    def body(conn, p, ctx):
        biz = need_business(p)
        # where the owner takes a step on the web (the setup step of the conversation): only when there is one -- an
        # invitation sent or an account connected has nothing left there, and the audit row keeps every answer
        # replayable as it was (it scrubs connect_url, as it always has)
        connect_url = f"{config.load().web_base_url}/?setup={biz}"
        if ctx["status"] == PAGE_FIRST:  # nothing to change: the owner's choice of page (or the page in hand's own
            # step: final review, item 1), asked as it is
            question, choices = launch_check.page_ask(conn, biz)
            data = {**ctx, "ask_owner": question, "choices": choices}
            return envelope.make(data, say=question, next=envelope.actions(*launch_check.page_actions(choices)))
        if ctx["status"] == CHOOSE:  # nothing to change: the owner's choice of way, asked as it is
            data = {**ctx, "ask_owner": ads_mode.WAY_QUESTION, "choices": ads_mode.choices()}
            return envelope.make(data, say=ads_mode.WAY_QUESTION, next=envelope.actions(*ads_mode.way_actions()))
        if ctx["status"] == NEEDS_AGREEMENT:
            return {**_public(ctx), "note": NEEDS_AGREEMENT_NOTE, "connect_url": connect_url}
        if ctx["status"] == PENDING:
            try:
                link = google_ads_link.save(
                    conn, biz, ctx["customer_id"], status="pending", resource_name=ctx["resource_name"]
                )
            except google_ads_link.LinkRefused as e:  # another business took this Customer ID meanwhile
                raise ToolError(str(e)) from e
            mode = config.load().google_ads_link_mode
            return {**_public(ctx), "customer_id": link.display, "instructions": google_ads_link.instructions(mode)}
        if ctx["status"] == CONNECTED:
            return _public(ctx)
        if state.nothing_to_connect(ctx["steps"]):  # the prepaid way: its step is the money -- for the first payment,
            # the plan's own agree page once its page is settled (the founder's ruling 3, 2026-10-13)
            connect_url = setup.pay_url(conn, config.load(), biz)
        return {**_public(ctx), "connect_url": connect_url}

    return run(tool, "manage", args, body, idempotency_key=idempotency_key, prepare=prepare)


# Whether the owner is still to be asked how the ads run (the founder's ruling of 2026-10-13): the rule itself lives in
# ads_mode, where modules that never import a command module at load time (blockers) read it too
asks_way = ads_mode.asks


def _same(text: str, customer_id: str) -> bool:
    """`text` names this Customer ID (dashes and spaces allowed)."""
    try:
        return google_ads_link.normalize_customer_id(text) == customer_id
    except google_ads_link.LinkRefused:
        return False


def _public(ctx: dict) -> dict:
    """What the answer shows of prepare's findings (never the invitation's resource name); the Customer ID of a link
    already in place too, which the alias's activity-log line names; and what a channel with nothing to connect waits
    for, while it does (`next_step`, F2)."""
    shown = ("status", "channel", "label", "account_public", "customer_id")
    waits = {"next_step": ctx["next_step"]} if ctx.get("next_step") else {}
    return {k: ctx[k] for k in shown if k in ctx} | {"steps": ctx["steps"]} | waits


@agent_tool(cls="W", scope="manage", approval="assistant")
def connect_channel(channel: str, idempotency_key: str, mode: str | None = None, account_id: str | None = None) -> dict:
    """Connect the business's account on one channel (google_search, newsbreak, ...) so Admiry can run its ads
    there. For Google Search on the owner's own account pass account_id, the 10-digit Customer ID from the top of
    their Google Ads page (123-456-7890): Admiry sends an invitation the owner accepts inside Google Ads, and the
    answer is status pending with the instructions, or needs_agreement with connect_url when the owner must accept
    Admiry's management agreement first (then call again with a new idempotency_key). NewsBreak needs nothing
    connected: the answer is status web_step with its step and connect_url and, while NewsBreak is not ready yet,
    next_step -- what it waits for, in the owner's words (as get_connection_status says it). mode (own_account or
    managed) asks for the other way a channel can run; for Google Search with neither mode nor account_id while the
    owner has chosen no way, nothing changes and the answer is status choose: the question to ask the owner and both
    ways (never choose for them). While the plan has no page settled, the page comes first: instead of choose or a
    web step the answer is status page_first, the page question and its three ways (or, with a page made and not
    published yet, the step that publishes it), and nothing changes.
    get_connection_status shows where every channel stands.
    IMPORTANT ACTION: confirm the account with the owner before calling. The 24-hour setup key from `start` cannot call
    this."""
    return connect(channel, idempotency_key, mode=mode, account_id=account_id)


# ---------------------------------------------------------------- the reading tools


@agent_tool(cls="RO", scope="read")
def get_connection_status() -> dict:
    """Where the business's account on every channel stands: for each (google_search, newsbreak, ...) how it runs
    (own_account or managed), its status (none, pending, active, refused, inactive or needs_attention), the account id
    the owner can look up, when Admiry last checked it and the next step while it is not connected; whether the
    management agreement is accepted and the business verified, and the balance when Admiry runs ads from it. Call
    it after connect_channel, or when the owner asks whether an account is connected."""

    def body(conn, p):
        biz = need_business(p)
        row = business.row(conn, biz)
        settings = config.load()
        out = {
            # Addendum 3 (founder 2026-10-13): the next step names neither way while none is chosen
            "channels": [_neutral(conn, biz, c.public()) for c in state.connections(conn, settings, biz)],
            "verified": row["claim_state"] == "verified",
            "agreement_accepted": agreements.accepted(conn, biz),
            "connect_url": f"{settings.web_base_url}/?setup={biz}",
        }
        if row["ads_mode"] == "managed" or registry.managed_channels(conn, biz):
            out["balance_usd"] = wallet.to_usd(wallet.balance_cents(conn, biz))
        return out

    return run("get_connection_status", "read", {}, body)


def _neutral(conn, biz: str, entry: dict) -> dict:
    """A get_connection_status entry with its next step in words that pick no way while the owner has chosen none
    (ads_mode.unchosen_words: the Customer ID's line is the neutral line then)."""
    said = entry.get("next_step")
    return {**entry, "next_step": ads_mode.unchosen_words(conn, biz, said)} if said else entry


@agent_tool(cls="RO", scope="read", app=CHANNELS_VIEW)
def get_channel_status() -> dict:
    """Where the business's ads stand on every channel right now: for each, its state and one sentence, today's
    spend against today's budget, the month so far against its monthly amount, this week's calls and forms, what the
    platform says is wrong, and what the owner can do (pause, resume, connect). Call it when the owner asks how their
    ads are doing, or before pause/resume."""

    def body(conn, p):
        card = state.channels(conn, config.load(), need_business(p))  # the homepage card's rows and footer
        say = " ".join(r["sentence"] for r in card["rows"]) or NO_ADS_YET
        return envelope.make({"channels": card["rows"], "footer": card["footer"]}, say=say)

    return run("get_channel_status", "read", {}, body)


def _ad_note(label: str, headline: str | None, topic: dict) -> str:
    quote = f'"{topic.get("topic")}"' if topic.get("topic") else "a policy issue"
    kind = f" ({topic['type']})" if topic.get("type") else ""
    where = f' on your ad ("{headline}")' if headline else ""
    return f"{label} disapproved{where}: {quote}{kind}"


@agent_tool(cls="RO", scope="read")
def get_status_issues() -> dict:
    """What each platform is unhappy with right now, if anything, in its own words: the reasons it gave for holding
    the ads back, its account and billing notices, and any disapproved ad with the platform's policy topic and type
    next to the ad's headline. Call it when get_channel_status mentions a problem, or the owner says something is
    wrong; relay the platform's words exactly, quotes included."""

    def body(conn, p):
        channels, parts = [], []
        for r in state.channel_rows(conn, config.load(), need_business(p)):
            reasons = [i["platform_text"] for i in r.issues if "platform_text" in i]
            ads = [{"headline": i["headline"], "topics": i["topics"]} for i in r.issues if "topics" in i]
            channels.append({"channel": r.channel, "label": r.label, "reasons": reasons, "ads": ads})
            if reasons:
                parts.append(f"{r.label} reports: " + ", ".join(f'"{reason}"' for reason in reasons))
            parts.extend(_ad_note(r.label, a["headline"], t) for a in ads for t in a["topics"])
        say = ". ".join(parts) + "." if parts else NO_ISSUES
        return envelope.make({"channels": channels}, say=say)

    return run("get_status_issues", "read", {}, body)


def _month(text: str | None) -> date | None:
    """`month` ("2026-09") as its first day; None for none. ToolError for anything else: another shape, a month
    outside 01-12, a year outside MONTH_YEARS (the statement reads up to the next month's first day, which must
    exist)."""
    if text is None:
        return None
    found = MONTH.fullmatch(text)
    if found is None:
        raise ToolError(BAD_MONTH)
    year, month = int(found.group(1)), int(found.group(2))
    if year not in MONTH_YEARS or not 1 <= month <= 12:
        raise ToolError(BAD_MONTH)
    return date(year, month, 1)


@agent_tool(cls="RO", scope="read")
def get_platform_statement(month: str | None = None) -> dict:
    """The monthly statement of every channel the ads ran on: what each platform charged, the clicks, impressions,
    calls and conversions, the account id the owner can look up, and each platform's billing sentence with Admiry's
    no-fee notice -- this month and the last, or the one calendar month given as month ("2026-09"). Read it to the
    owner exactly as written."""

    def body(conn, p):
        first = _month(month)
        statement = ads_run.statement(conn, need_business(p), first)
        if statement is None:
            return envelope.make({"month": month, "statement": None}, say=NO_STATEMENT)
        when = "so far this month" if first is None else f"in {first:%B %Y}"
        spent = [(e["label"], plans.usd(e["months"][0]["spent_usd"])) for e in statement["channels"]]
        (label, amount), *rest = spent
        say = (
            f"{amount} spent {when} on {label}{''.join(f', {a} on {lb}' for lb, a in rest)}. "
            f"{statement['no_fee_notice']}."
        )
        return envelope.make({"month": month, "statement": statement}, say=say)

    return run("get_platform_statement", "read", {"month": month}, body)
