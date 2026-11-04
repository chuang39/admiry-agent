"""Ads setup for the signed-in owner (spec 2026-09-29 setup-in-conversation, D64–D70): the state the home's setup
cards follow — the server decides the step, the page never infers it — and the owner's actions: choose how the ads
run, accept every document that way needs in one step (D66), link their own Google Ads account and check it, start a
payment. Web-session actions only (D70): setup_api.py checks the session, CSRF and ownership, and every function here
takes the owner's e-mail and address for the audit row. Google and Stripe are called with no database connection
open."""

from __future__ import annotations

import dataclasses
import logging
import math
import secrets
from dataclasses import dataclass

from fastmcp.exceptions import ToolError

from . import (
    ads_mode,
    ads_run,
    agreements,
    audit,
    blockers,
    business,
    config,
    db,
    google_ads_link,
    landing,
    launch_check,
    launch_review,
    legal,
    plans,
    proposals,
    stripe_client,
    wallet,
)
from . import state as read_model  # `state` is this module's own function
from .channels import registry
from .channels.google_search import client as google_ads  # A3's names for the modules moved in M0
from .channels.google_search import conversions as ads_conversions

log = logging.getLogger(__name__)
TWO_WEEKS = 14  # D68: the first top-up covers two weeks of the plan's daily budget
AGREE_TOOLS = {legal.AGREEMENT: "web.agree", legal.ADDENDUM: "web.agree_prepayment"}
TICK_ALL = "Tick every box to accept."
CHANGED = "The text changed while this page was open; read the new version and accept it again."
NOT_NEEDED = "That document is not needed for the way you chose."
CHECKS_PER_HOUR = 6  # spec 2026-09-25 §5: Check now, per business
PAY_STARTS_PER_HOUR = 20  # Checkout Sessions per business per hour (each is a Stripe API call)
MANUAL_CHECK = (
    "During early access an Admiry operator sends and checks the invitation; "
    "this page shows the change once it is accepted."
)
TOO_MANY_CHECKS = (
    f"You checked {CHECKS_PER_HOUR} times in the last hour; "
    "Admiry also checks every hour by itself, so there is nothing more to do."
)
NOT_MANAGED = (
    "This business runs its ads in your own Google Ads account and pays Google directly, so there is nothing to add "
    "funds for."
)
NEEDS_TERMS = "Accept the agreement and the prepayment terms first."
NOT_SET_UP = "Payments are not set up on this server yet."
BANK_OFF = "Bank transfers are not offered yet; pay by card."
TOO_MANY_PAYMENTS = "Too many payment attempts for this business in the last hour; try again later."
WITHDRAW_FAILED = "Admiry could not take back the Google Ads invitation right now; try again in a few minutes."
PAYMENT_MAY_LAND = "A payment you started may still be completing; try again in a few minutes, or ask Admiry."
# The ways the card can offer, in its order (static/setup.js MODES), and what it says when the plan leaves the prepaid
# one out (walk fixes 1, final review B I3: the controller's wording)
WAYS = ("own_account", "managed")
MODE_WAITS = "Google Search runs in your own Google Ads account for now; Admiry-run Google accounts are coming."
# L1: the plan card's yes (approve) and Agree and pay (pay with a plan)
NOTHING_TO_REVIEW = "There is no plan waiting for your yes right now."
PLAN_CHANGED = "The plan changed while this card was open; Admiry will look at the new one. Reload to see it."
NOT_VERIFIED = "Admiry has to verify the business before its ads can start; your first payment does that."
# The founder's ruling (2026-10-13): the page comes before the money -- no Checkout starts while the latest draft's page
# is not settled (`pay`, the one money path the home's card and the agree page share)
PAGE_FIRST = "Your ads need a page to send people to before you pay. Set up the page first, then pay."
# The agree page (/agree/<plan_id>, the same day's ruling 3): the link an assistant gives the owner for the first
# payment -- the plan, the documents the owner ticks, one button to Stripe -- and what it says when it takes nothing
LINK_MOVED = "Your plan changed after this link was made. Ask your assistant for a new link."
ALREADY_PAID = (
    "A payment is already in for this business, so there is nothing to pay here. Go back to your assistant for what "
    "comes next."
)
# Final review, item 4: paying there chooses Admiry running the ads (`choose`), which withdraws an invitation to the
# owner's own Google Ads account still waiting (`_withdraw`) -- the page says so in its fine print, never silently
SWITCHES_WAY = (
    "Paying here switches your ads to Admiry running them from your balance, and withdraws the invitation to link your "
    "own Google Ads account."
)
PAID_IN_REVIEW = "Paid — in Admiry's review"
PAID = "Paid"
PAID_LINE = "Your payment is in."
BACK_TO_ASSISTANT = "You can close this page and go back to your assistant."
# back from Stripe, the page waits for the payment as the home's card does (static/setup.js: twenty looks, three
# seconds apart, then its own words)
CONFIRMING = "Your payment is being confirmed; this page updates when it lands."
NOT_YET = "Your payment has not shown up yet. Reload this page in a few minutes before you pay again."
LOOKS = 20
LOOK_SECONDS = 3


class Refused(Exception):
    """An owner sentence saying why the action did not happen; `status` is the HTTP code setup_api answers with."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Owner:
    email: str
    ip: str | None


def documents(mode: str) -> list[str]:
    """The texts a business accepts for its way of running ads: the agreement always, the addendum when prepaid."""
    return [legal.AGREEMENT, legal.ADDENDUM] if mode == "managed" else [legal.AGREEMENT]


def suggested_topup_usd(daily_usd: int | None, settings: config.Settings) -> int:
    """D68: two weeks of the daily budget, up to the next $10, within the configured top-up bounds."""
    amount = math.ceil((daily_usd or 0) * TWO_WEEKS / 10) * 10
    return max(settings.topup_min_usd, min(settings.topup_max_usd, amount))


def daily_usd(conn, business_id: str) -> int | None:
    """The latest plan's daily budget (plans.cards' per_day: the month ÷ plans.DAYS, floored), or None."""
    latest = plans.latest(conn, business_id)
    channels = ((latest or {}).get("sections") or {}).get("ads", {}).get("channels") or []
    monthly = sum(float(c.get("monthly_usd") or 0) for c in channels)
    return int(monthly) // plans.DAYS if monthly > 0 else None


def _hard_lock(conn, business_id: str, mode: str) -> str | None:
    """Why the owner cannot switch away from `mode` at all (spec D67): a live link, or money paid in. A pending
    invitation or an unpaid Checkout is not a lock here: choose() withdraws or expires it itself. A bank transfer on
    its way is money in place (plan 2026-10-04-agent-m1m-1 P3)."""
    if mode == "own_account":
        link = google_ads_link.get(conn, business_id)
        return ads_mode.LINK_IN_PLACE.format(display=link.display) if link is not None and link.live else None
    paid = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM wallet_entry WHERE business_id = %(b)s) OR EXISTS (SELECT 1 FROM "
        "payment_session WHERE business_id = %(b)s AND status IN ('paid', 'processing')) AS paid",
        {"b": business_id},
    ).fetchone()["paid"]
    return ads_mode.MONEY_IN_PLACE if paid else None


def _steps(conn, settings: config.Settings, business_id: str) -> list[dict]:
    """The "Ready to launch" card's steps (spec 2026-09-29 §4 item 2): one per channel the latest plan funds, in card
    order -- how the owner connects it (the adapter's connect_steps) and whether its account is ready yet; a channel
    with nothing to connect that is not ready has what it waits for too, `reason` (M1 follow-up 2, W3:
    `state.waiting_for`, its account's own reason as get_connection_status's next step says it -- NewsBreak's funds, or
    NewsBreak getting the account ready); a channel not built yet whose page rules the saved page report does not pass
    has the page's fix, `page` (`_page_line`, W4); a channel that shows a picture with the ad (NewsBreak) has the
    plan's line about it, `image` (M1 Task 7: the picture is approved with the plan; fix round 1: or, once it left
    Admiry's set, the line that says so)."""
    plan = plans.latest(conn, business_id)
    funded = set(ads_run.funded_channels(plan))
    shown = set(plans.image_channels(plan["sections"])) if plan else set()
    line = plans.image_step(plan["sections"]) if shown else None
    report = landing.judged_report(conn, landing.current(conn, business_id))  # as the launch judges it
    listed = [c for c in read_model.connections(conn, settings, business_id) if c.channel in funded]
    fixes = {c.channel: _page_line(conn, business_id, report, c.channel) for c in listed}
    waits = {c.channel: read_model.waiting_for(c.ready, c.reason, c.steps) for c in listed}
    return [
        {"channel": c.channel, "label": c.label, "text": step["text"], "url": step["url"], "done": c.ready}
        | ({"reason": waits[c.channel]} if waits[c.channel] else {})
        | ({"page": fixes[c.channel]} if fixes[c.channel] else {})
        | ({"image": line} if c.channel in shown else {})
        for c in listed
        for step in c.steps
    ]


def ways(conn, business_id: str) -> list[str]:
    """The ways the card offers (walk fixes 1, final review B I3): only those that exist for the latest plan, by the
    rule the plan answer's ways follow (`plans.ways`, `plans.google_open`) -- the ways the places it funds run, as their
    adapters declare them: the business's own Google Ads account for Google Search; Admiry running the ads from a
    prepaid balance for a place Admiry runs from it (NewsBreak), and for Google Search once Admiry runs it itself. Both
    before there is a plan that funds anything, as the card always offered."""
    funded = [ch for ch in ads_run.funded_channels(plans.latest(conn, business_id)) if registry.known(ch)]
    modes = set().union(*(registry.adapter(ch).capabilities().modes for ch in funded))
    return [way for way in WAYS if way in modes] or list(WAYS)


def _page_line(conn, business_id: str, report: dict | None, channel: str) -> str | None:
    """The page's fix a funded channel waits for (M1 follow-up 2, W4): its own `page_rules` -- NewsBreak's Terms link
    and contact, which the saved report counts as warnings that leave the page's own verdict passing, since Google
    Search does not need them (D77) -- not passed outright, in the words its row carries (`ads_run.page_fix`). None
    for a channel that names no page rules, while no report is saved (the page step asks for a check), once the
    report lets the channel launch, or once its ads are built (fix round 1, F1: the page gates building ads and turning
    them on the first time, never ads already built -- ruling S8, `ads_run.has_ads` as `_launch_rules` reads it)."""
    rules = registry.adapter(channel).capabilities().page_rules
    if not rules or report is None or ads_run.has_ads(conn, business_id, channel):
        return None
    return ads_run.page_fix(report, rules)


def state(conn, settings: config.Settings, business_id: str, *, chosen: bool = False) -> dict:
    """Where this business's setup stands. `chosen` is True right after the owner picked a way: the default way
    (own_account) leaves no trace until the agreement is accepted, so a reload asks again (one tap). The way is
    Google Search's account row's (M0 P6: `ads_mode.change` writes it with `business.ads_mode`; the column only while
    there is no row), and `steps` is one step per channel the plan funds. `ways` are the ways the card offers (`ways`),
    with `ways_note` saying why when the prepaid one is left out; a way already chosen stays whatever they say."""
    row = business.row(conn, business_id)
    mode = ads_mode.current(conn, business_id)
    link = google_ads_link.get(conn, business_id)
    docs = [
        {
            "id": d,
            "title": legal.TITLES[d],
            "version": agreements.version(d),
            "url": f"/legal/{d}",
            "accepted": agreements.accepted(conn, business_id, d),
        }
        for d in documents(mode)
    ]
    money = wallet.summary(conn, business_id) if mode == "managed" else None
    chosen = chosen or mode == "managed" or link is not None or docs[0]["accepted"]
    if row["claim_state"] == "draft":
        step = "claim"
    elif not chosen:
        step = "choose"
    elif not all(d["accepted"] for d in docs):
        step = "accept"
    elif mode == "own_account":
        step = "done" if link is not None and link.live else "link"
    else:
        step = "done" if money["balance_cents"] > 0 else "pay"
    plan = plans.latest(conn, business_id)
    latest = business.latest_knowledge(conn, business_id)
    daily = daily_usd(conn, business_id) if mode == "managed" else None
    offered = ways(conn, business_id)
    return {
        "business_id": business_id,
        "name": plans.name_for(latest[1] if latest else None) or "your business",
        "step": step,
        "plan_id": plan["id"] if plan else None,  # the latest plan: what `approve` approves (Agree and pay may name)
        "plan_url": plans.plan_url(plan["id"]) if plan else None,  # the card's "See the full plan page"
        "plan_status": plan["status"] if plan else None,  # L1: the yes waits on a draft (approve)
        "in_review": launch_review.owner_words(conn, business_id),  # L1: where it stands while Admiry checks it
        "mode": mode,
        "locked": None if row["claim_state"] == "draft" else _hard_lock(conn, business_id, mode),
        "documents": docs,
        "link": link.public(settings.google_ads_link_mode) if link else None,
        "link_mode": settings.google_ads_link_mode,
        "balance_usd": wallet.to_usd(money["balance_cents"]) if money else None,
        "pending_topups": money["pending_topups"] if money else 0,
        "open_topups": money["open_topups"] if money else 0,
        "transfer_pending_usd": (
            wallet.to_usd(money["transfer_pending_cents"]) if money and money["transfer_pending_cents"] else None
        ),
        "paused": row["paused_reason"] == "no_funds",
        "topup": (
            {
                "suggested_usd": suggested_topup_usd(daily, settings),
                "min_usd": settings.topup_min_usd,
                "max_usd": settings.topup_max_usd,
                "daily_usd": daily,
                "rails": list(wallet.rails(settings)),
                "rates": {
                    "card_bps": settings.card_cost_bps,
                    "card_fixed_cents": settings.card_cost_fixed_cents,
                    "ach_bps": settings.ach_cost_bps,
                    "ach_cap_cents": settings.ach_cost_cap_cents,
                },
            }
            if mode == "managed"
            else None
        ),
        "payments_ready": settings.payments_ready,
        "steps": _steps(conn, settings, business_id),
        "ways": offered,
        "ways_note": None if "managed" in offered else MODE_WAITS,
    }


def _locked_row(conn, business_id: str) -> dict:
    row = conn.execute(
        "SELECT claim_state, ads_mode FROM business WHERE id = %s AND status = 'active' FOR UPDATE", (business_id,)
    ).fetchone()
    if row is None:
        raise Refused("no such business", status=404)
    if row["claim_state"] == "draft":
        raise Refused(ads_mode.NOT_CLAIMED)
    return row


def accept(settings: config.Settings, owner: Owner, business_id: str, given: object) -> dict:
    """D66: `given` maps each ticked document to the version the page showed. Every document the way needs and has
    not accepted must be there at its current version; all are recorded in one transaction, or none."""
    with db.connect(settings.database_url) as conn:
        with conn.transaction():
            row = _locked_row(conn, business_id)
            wanted = documents(row["ads_mode"])
            if not isinstance(given, dict) or any(d not in wanted for d in given):
                raise Refused(NOT_NEEDED, status=400)
            needed = [d for d in wanted if not agreements.accepted(conn, business_id, d)]
            if any(d not in given for d in needed):
                raise Refused(TICK_ALL, status=400)
            if any(given[d] != agreements.version(d) for d in needed):
                raise Refused(CHANGED)
            for d in needed:
                version = agreements.accept(conn, business_id=business_id, email=owner.email, ip=owner.ip, document=d)
                audit.record_web(
                    conn,
                    tool=AGREE_TOOLS[d],
                    email=owner.email,
                    ip=owner.ip,
                    business_id=business_id,
                    args={"business_id": business_id, "version": version},
                    result={"version": version},
                )
        return state(conn, settings, business_id, chosen=True)


def _count(conn, business_id: str, tool: str) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE business_id = %s AND tool = %s AND ts > now() - interval '1 hour'",
        (business_id, tool),
    ).fetchone()["n"]


def _withdraw_at_google(settings: config.Settings, link: google_ads_link.Link) -> google_ads_link.Remote | None:
    """Api mode: withdraw the pending invitation at Google, no connection open. None when Admiry withdrew it; else
    what Google shows for it (refused / cancelled / inactive / no invitation), which the caller records and moves on.
    The stored resource name can be missing (send returns none when the invitation was already open at Google, or the
    account was already a client held by this business) and the stored name can be stale, so either reads Google."""
    if link.resource_name:
        try:
            google_ads_link.withdraw(settings, link)
            return None
        except google_ads_link.LinkError as e:
            log.warning("withdrawing the invitation for business %s failed: %s; reading it", link.business_id, e)
    try:
        remote = google_ads_link.read(settings, link.customer_id)
    except google_ads_link.LinkError as e:
        raise Refused(str(e), status=502) from None
    if remote is None or remote.status is None:  # Google said UNKNOWN: nothing to act on safely
        raise Refused(WITHDRAW_FAILED, status=502)
    if remote.status == "pending":
        if not remote.resource_name:
            raise Refused(WITHDRAW_FAILED, status=502)
        try:
            google_ads_link.withdraw(settings, dataclasses.replace(link, resource_name=remote.resource_name))
        except google_ads_link.LinkError as e:
            raise Refused(str(e), status=502) from None
        return None
    return remote


def _withdraw(settings: config.Settings, owner: Owner, business_id: str, link: google_ads_link.Link) -> None:
    """D67: take back the pending invitation before switching away. Manual mode marks it cancelled here (an operator
    withdraws it in the MCC). Api mode withdraws it at Google; when Google shows it accepted meanwhile, that is
    recorded and the switch is refused (LINK_IN_PLACE); when Google shows it already gone, that is recorded and the
    switch goes on."""
    remote = _withdraw_at_google(settings, link) if settings.google_ads_link_mode == "api" else None
    with db.connect(settings.database_url) as conn:
        with conn.transaction():
            if remote is not None:
                out = google_ads_link.apply(conn, business_id, link.customer_id, remote)
                if out.became_active:
                    audit.record_web(
                        conn,
                        tool="web.ads_link_active",
                        email=owner.email,
                        ip=owner.ip,
                        business_id=business_id,
                        args={"business_id": business_id},
                        result={"customer_id": link.display, "verified_now": out.verified_now},
                    )
                moved = 0
            else:
                moved = conn.execute(
                    "UPDATE ads_link SET status = 'cancelled', checked_at = now() "
                    "WHERE business_id = %s AND customer_id = %s AND status = 'pending'",
                    (business_id, link.customer_id),
                ).rowcount
                if moved:
                    google_ads_link.mirror(conn, business_id)  # the account row follows the link (M0)
            if moved:
                audit.record_web(
                    conn,
                    tool="web.ads_link_withdraw",
                    email=owner.email,
                    ip=owner.ip,
                    business_id=business_id,
                    args={"business_id": business_id},
                    result={"customer_id": link.display},
                )
        if remote is not None and out.became_active:  # outside the transaction, same connection (A3 Task 2)
            try:
                ads_conversions.ensure(conn, settings, business_id, link.customer_id)
            except google_ads.AdsError as e:
                log.warning("ads_link withdraw: conversion setup for business %s: %s", business_id, e)
    if remote is not None and remote.status == "active":  # recorded above; the transaction committed
        raise Refused(ads_mode.LINK_IN_PLACE.format(display=link.display))


def _expire(settings: config.Settings, owner: Owner, business_id: str, session_ids: list[str]) -> None:
    """Expire each unpaid Checkout opened in the last 24 hours at Stripe (older ones died at Stripe already; choose
    marks them expired itself). A fresh idempotency key per attempt: a key reused within 24 hours would make Stripe
    replay a transient failure instead of trying again."""
    for sid in session_ids:
        try:  # no connection open
            stripe_client.expire_checkout(
                stripe_client.client(),
                secret_key=settings.stripe_secret_key or "",
                session_id=sid,
                idempotency_key=f"expire-{secrets.token_hex(16)}",
            )
        except stripe_client.StripeError as e:  # paid meanwhile, or Stripe down: the business stays prepaid
            log.warning("expiring Checkout %s for business %s failed: %s", sid, business_id, e)
            raise Refused(PAYMENT_MAY_LAND) from None
        with db.connect(settings.database_url) as conn, conn.transaction():
            conn.execute("UPDATE payment_session SET status = 'expired' WHERE id = %s AND status = 'open'", (sid,))
            audit.record_web(
                conn,
                tool="web.pay_expire",
                email=owner.email,
                ip=owner.ip,
                business_id=business_id,
                args={"business_id": business_id, "session_id": sid},
                result={"expired": True},
            )


def approve(settings: config.Settings, owner: Owner, business_id: str, plan_id: object) -> dict:
    """The owner's yes, from the plan card (L1, spec 2026-10-10 §5) once the money is in or the owner's own account is
    live (the card done): the latest draft, named by `plan_id` so a plan that changed under an open card is never
    approved unseen, with nothing on its checklist that blocks it (launch_check.blocked) -- approved the way the plan
    page's Approve does (proposals.approve_on_web: no code, approval kind "web"). Its ads launch in this request
    (ads_run.launch_now, which never raises) when that approved it; with AGENT_LAUNCH_REVIEW on a first launch goes to
    Admiry's review instead (proposals.confirm -> launch_review.submit) and nothing launches here. Refused in the
    owner's words: nothing waits for a yes, the plan changed, the business is not verified, the list's first blocking
    problem, or the launch's own check."""
    with db.connect(settings.database_url) as conn:
        st = state(conn, settings, business_id)
        if st["step"] != "done" or st["plan_status"] != plans.DRAFT:
            raise Refused(NOTHING_TO_REVIEW)
        if not isinstance(plan_id, str) or plan_id != st["plan_id"]:
            raise Refused(PLAN_CHANGED)
        if business.row(conn, business_id)["claim_state"] != "verified":
            raise Refused(NOT_VERIFIED)
        problem = launch_check.blocked(conn, settings, business_id, plan_id)
        if problem is not None:
            raise Refused(PLAN_CHANGED if problem == blockers.PLAN_MOVED else problem)
        try:
            approved = proposals.approve_on_web(conn, business_id, plan_id, email=owner.email, ip=owner.ip)
        except ToolError as e:
            raise Refused(str(e)) from e
    launch = ads_run.launch_now(business_id) if approved.get("plan_status") == plans.APPROVED else None
    with db.connect(settings.database_url) as conn:
        return {"state": state(conn, settings, business_id), "launch": launch, "approved": approved}


def approve_paid(settings: config.Settings, session_id: str) -> dict | None:
    """Agree and pay (L1, spec 2026-10-10 §5; plan D12): a credited Checkout that named a plan approves it -- right
    after the credit committed, in its own transaction -- while AGENT_LAUNCH_REVIEW is on, the plan is still the
    latest draft with nothing on its list that blocks it and the business is verified: the owner's own yes on
    Admiry's surface (proposals.approve_on_web, approval kind "paid"), which sends a first launch to review; a newer
    version of a business already running is approved and launched (plan D6). Anything else approves nothing: the
    card and the receipt say what is next (the list, or "Confirm this plan"). Never raises: the money has landed
    whatever happens here. The approval's answer, or None."""
    if not settings.launch_review:
        return None
    try:
        with db.connect(settings.database_url) as conn:
            row = conn.execute(
                "SELECT business_id, plan_id FROM payment_session WHERE id = %s", (session_id,)
            ).fetchone()
            if row is None or not row["plan_id"]:
                return None
            biz, plan_id = row["business_id"], row["plan_id"]
            owner = business.row(conn, biz)
            if owner["claim_state"] != "verified" or launch_check.blocked(conn, settings, biz, plan_id) is not None:
                return None
            approved = proposals.approve_on_web(
                conn, biz, plan_id, email=owner["owner_email"] or "owner", ip=None, kind="paid"
            )
    except ToolError as e:
        log.info("approve_paid: the plan of session %s was not approved: %s", session_id, e)
        return None
    except Exception as e:  # noqa: BLE001 - the payment landed; the card offers the yes again
        log.warning("approve_paid: %s for session %s", type(e).__name__, session_id)
        return None
    if approved.get("plan_status") == plans.APPROVED:
        ads_run.launch_now(biz)
    return approved


def choose(settings: config.Settings, owner: Owner, business_id: str, to: object) -> dict:
    """Set how the ads run (ads_mode.change holds the rules). Changing one's mind (D67): before switching away, a
    pending invitation is withdrawn at Google and unpaid Checkouts are expired at Stripe; a live link or money in
    is refused first, with nothing called. A way already in place changes nothing."""
    if to not in ads_mode.MODES:
        raise Refused("Choose one of the two ways.", status=400)
    with db.connect(settings.database_url) as conn:
        row = business.row(conn, business_id)
        if row["claim_state"] == "draft":
            raise Refused(ads_mode.NOT_CLAIMED)
        link = google_ads_link.get(conn, business_id)
        hard = _hard_lock(conn, business_id, row["ads_mode"])
        open_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM payment_session WHERE business_id = %s AND status = 'open' "
                "AND created_at > now() - interval '24 hours' ORDER BY created_at",
                (business_id,),
            )
        ]
    if row["ads_mode"] != to:
        if hard:
            raise Refused(hard)
        if row["ads_mode"] == "own_account" and link is not None and link.status == "pending":
            _withdraw(settings, owner, business_id, link)
        if row["ads_mode"] == "managed" and open_ids:
            _expire(settings, owner, business_id, open_ids)
    with db.connect(settings.database_url) as conn:
        try:
            with conn.transaction():  # ads_mode.change locks the row and checks again: a payment that landed wins
                if row["ads_mode"] == "managed" and to != "managed":  # past a Checkout's 24 h life: dead at Stripe
                    conn.execute(
                        "UPDATE payment_session SET status = 'expired' WHERE business_id = %s AND status = 'open' "
                        "AND created_at <= now() - interval '24 hours'",
                        (business_id,),
                    )
                if ads_mode.change(conn, business_id, to):
                    audit.record_web(
                        conn,
                        tool="web.ads_mode",
                        email=owner.email,
                        ip=owner.ip,
                        business_id=business_id,
                        args={"business_id": business_id, "ads_mode": to},
                        result={"ads_mode": to},
                    )
        except ads_mode.ModeRefused as e:
            raise Refused(str(e), status=e.status) from None
        return state(conn, settings, business_id, chosen=True)


def link(settings: config.Settings, owner: Owner, business_id: str, customer_id: object) -> dict:
    """Send the invitation to the owner's Customer ID (api) or record it for an operator (manual)."""
    with db.connect(settings.database_url) as conn:
        try:
            cid = google_ads_link.check_invite(conn, business_id, customer_id if isinstance(customer_id, str) else "")
        except google_ads_link.LinkRefused as e:
            raise Refused(str(e), status=e.status) from None
        held = google_ads_link.held(conn, business_id, cid)
    try:  # no connection open
        resource = google_ads_link.send(settings, cid, held=held)
    except google_ads_link.LinkRefused as e:  # already linked to Admiry, not by this business: nothing is saved
        raise Refused(str(e), status=e.status) from None
    except google_ads_link.LinkError as e:
        with db.connect(settings.database_url) as conn:
            google_ads_link.record_error(conn, business_id, cid, str(e))
        raise Refused(str(e), status=502) from None
    with db.connect(settings.database_url) as conn:
        try:
            with conn.transaction():
                saved = google_ads_link.save(conn, business_id, cid, status="pending", resource_name=resource)
                audit.record_web(
                    conn,
                    tool="web.ads_link_invite",
                    email=owner.email,
                    ip=owner.ip,
                    business_id=business_id,
                    args={"business_id": business_id, "customer_id": cid},
                    result={"customer_id": saved.display, "status": "pending", "mode": settings.google_ads_link_mode},
                )
        except google_ads_link.LinkRefused as e:  # another business took this Customer ID meanwhile
            raise Refused(str(e), status=e.status) from None
        return state(conn, settings, business_id, chosen=True)


def check(settings: config.Settings, owner: Owner, business_id: str) -> dict:
    """Check now: read the invitation's status from Google and record it (api mode, CHECKS_PER_HOUR per business)."""
    with db.connect(settings.database_url) as conn:
        current = google_ads_link.get(conn, business_id)
        if current is None:
            raise Refused("Send the invitation first.")
        if settings.google_ads_link_mode != "api":
            raise Refused(MANUAL_CHECK)
        if _count(conn, business_id, "web.ads_link_check") >= CHECKS_PER_HOUR:  # refused before Google, not recorded
            raise Refused(TOO_MANY_CHECKS, status=429)
    try:  # no connection open
        remote = google_ads_link.read(settings, current.customer_id)
    except google_ads_link.LinkError as e:
        with db.connect(settings.database_url) as conn, conn.transaction():
            google_ads_link.record_error(conn, business_id, current.customer_id, str(e))
            audit.record_web(
                conn,
                tool="web.ads_link_check",
                email=owner.email,
                ip=owner.ip,
                business_id=business_id,
                args={"business_id": business_id},
                result={"customer_id": current.display, "status": current.owner_status, "error": str(e)},
            )
        raise Refused(str(e), status=502) from None
    with db.connect(settings.database_url) as conn:
        with conn.transaction():
            out = google_ads_link.apply(conn, business_id, current.customer_id, remote)
            now = out.link or current
            audit.record_web(
                conn,
                tool="web.ads_link_check",
                email=owner.email,
                ip=owner.ip,
                business_id=business_id,
                args={"business_id": business_id},
                result={"customer_id": current.display, "status": now.owner_status},
            )
            if out.became_active:
                audit.record_web(
                    conn,
                    tool="web.ads_link_active",
                    email=owner.email,
                    ip=owner.ip,
                    business_id=business_id,
                    args={"business_id": business_id},
                    result={"customer_id": current.display, "verified_now": out.verified_now},
                )
        if out.became_active:  # outside the transaction, same connection (A3 Task 2)
            try:
                ads_conversions.ensure(conn, settings, business_id, current.customer_id)
            except google_ads.AdsError as e:
                log.warning("ads_link check: conversion setup for business %s: %s", business_id, e)
        return state(conn, settings, business_id, chosen=True)


def waits_for_page(conn, business_id: str, plan_id: object = None) -> bool:
    """Whether a payment waits for the page: the latest draft's page is not settled (launch_check.page_first) -- unless
    it is a top-up (no plan named: Add funds, get_billing's link) while ads are built (`ads_run.page_rows`). Those ads
    spend the balance, so a newer draft adding a place with no page never stops the money that keeps them running
    (fix round 2b, item 4); that draft's own payment (Agree and pay, the plan named) still waits for its page."""
    if plan_id is None and ads_run.page_rows(conn, business_id):
        return False
    return launch_check.page_first(conn, business_id)


def _business_name(conn, business_id: str) -> str:
    latest = business.latest_knowledge(conn, business_id)
    return (latest[1].get("name") if latest else None) or "your business"


def pay(
    settings: config.Settings,
    owner: Owner,
    business_id: str,
    amount_usd: object,
    rail: object = "card",
    plan_id: object = None,
    *,
    back: str | None = None,
) -> str:
    """Start a Stripe Checkout for a prepaid business at the price of `rail` (D92: the amount plus what that way of
    paying costs; wallet.quote) and return its page's URL; Stripe sends the owner back to the conversation
    (`/?setup=<id>&paid=1` or `&cancelled=1`), or to `back`, a path on the web host (`<back>?paid=1`, or `back` as
    it is: the agree page's). The webhook is the source of truth for the balance. `plan_id`, given by Agree and pay
    (L1, spec 2026-10-10 §5), must be the latest draft with nothing on its checklist that blocks it
    (launch_check.blocked); it rides on the payment_session row and the Checkout's metadata, so the credit can
    approve that plan once it lands. None (Add funds) names no plan. No Checkout starts while the latest draft's
    page is not settled (`waits_for_page`: the founder's ruling of 2026-10-13, the page before the money) -- except
    a top-up while ads are built, which they spend."""
    if rail not in wallet.RAILS:
        raise Refused("Choose to pay by card or by bank transfer.", status=400)
    if rail not in wallet.rails(settings):
        raise Refused(BANK_OFF, status=400)
    with db.connect(settings.database_url) as conn:
        row = business.row(conn, business_id)
        if row["ads_mode"] != "managed":
            raise Refused(NOT_MANAGED)
        if row["claim_state"] == "draft":
            raise Refused(ads_mode.NOT_CLAIMED)
        if not agreements.terms_accepted(conn, business_id):
            raise Refused(NEEDS_TERMS)
        if not settings.payments_ready:
            raise Refused(NOT_SET_UP, status=503)
        if waits_for_page(conn, business_id, plan_id):  # the page comes before the money (founder, 2026-10-13)
            raise Refused(PAGE_FIRST)
        if plan_id is not None:  # Agree and pay (L1 §5): the plan the card shows, with nothing left on its list
            if not isinstance(plan_id, str):
                raise Refused(PLAN_CHANGED)
            problem = launch_check.blocked(conn, settings, business_id, plan_id)
            if problem is not None:
                raise Refused(PLAN_CHANGED if problem == blockers.PLAN_MOVED else problem)
        try:
            cents = wallet.parse_usd(
                str(amount_usd if amount_usd is not None else ""),
                min_usd=settings.topup_min_usd,
                max_usd=settings.topup_max_usd,
            )
        except wallet.AmountRefused as e:
            raise Refused(str(e), status=400) from None
        recent = conn.execute(
            "SELECT count(*) AS n FROM payment_session WHERE business_id = %s AND created_at > now() - interval '1 hour'",
            (business_id,),
        ).fetchone()["n"]
        if recent >= PAY_STARTS_PER_HOUR:
            raise Refused(TOO_MANY_PAYMENTS, status=429)
        name = _business_name(conn, business_id)
    q = wallet.quote(cents, rail, settings)
    # Stripe sends the owner's browser back here: the web host, where the session cookie lives (spec 2026-09-29 §3)
    if back is None:
        home = f"{settings.web_base_url}/?setup={business_id}"
        success, cancel = f"{home}&paid=1", f"{home}&cancelled=1"
    else:
        success, cancel = f"{settings.web_base_url}{back}?paid=1", f"{settings.web_base_url}{back}"
    try:  # no connection open
        checkout = stripe_client.create_checkout(
            stripe_client.client(),
            secret_key=settings.stripe_secret_key or "",
            business_id=business_id,
            quote=q,
            product_name=f"Advertising balance for {name}",
            success_url=success,
            cancel_url=cancel,
            customer_email=owner.email,
            idempotency_key=f"pay-{secrets.token_hex(16)}",
            plan_id=plan_id if isinstance(plan_id, str) else None,
            link=settings.card_link,
        )
    except stripe_client.StripeError as e:  # the owner reads OWNER_ERROR; the operator reads Stripe's reason here
        log.warning("Stripe checkout for business %s failed: %s", business_id, e)
        raise Refused(stripe_client.OWNER_ERROR, status=502) from None
    with db.connect(settings.database_url) as conn, conn.transaction():
        # read again, locked (ads_mode.change takes the same lock): an owner who switched away from prepaid meanwhile
        # is not sent to Stripe, and the session is recorded expired
        still = (
            conn.execute("SELECT ads_mode FROM business WHERE id = %s FOR UPDATE", (business_id,)).fetchone()[
                "ads_mode"
            ]
            == "managed"
        )
        conn.execute(
            "INSERT INTO payment_session (id, business_id, amount_cents, credited_cents, processing_cents, rail, "
            "status, plan_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                checkout.id,
                business_id,
                q.total_cents,
                q.credit_cents,
                q.processing_cents,
                rail,
                "open" if still else "expired",
                plan_id,
            ),
        )
        audit.record_web(
            conn,
            tool="web.pay_start" if still else "web.pay_stopped",
            email=owner.email,
            ip=owner.ip,
            business_id=business_id,
            args={"business_id": business_id, "amount_usd": wallet.to_usd(cents), "rail": rail, "plan_id": plan_id},
            result={
                "session_id": checkout.id,
                "amount_usd": wallet.to_usd(cents),
                "price_usd": wallet.to_usd(q.total_cents),
            },
        )
    if not still:
        raise Refused(NOT_MANAGED)
    return checkout.url


# ---------------------------------------------------------------- the agree page (founder's ruling 3, 2026-10-13)
# The link an assistant gives the owner for the first payment opens /agree/<plan_id> (pages.py): the plan's summary, the
# documents the prepaid way needs that the business has not accepted -- each a tick the owner makes, never pre-ticked --
# and one Agree and pay button straight to Stripe. It is the home's own Agree and pay (choose, accept, pay with the
# plan: L1, paying approves the version and sends it to Admiry's review), in one request; never a second money path.


def agree_url(settings: config.Settings, plan_id: str) -> str:
    """The plan's agree page, on the web host where the owner's session cookie lives (spec 2026-09-29 §3)."""
    return f"{settings.web_base_url}/agree/{plan_id}"


def first_payment_plan(conn, business_id: str) -> str | None:
    """The plan the first payment agrees to: the latest plan while it is a draft whose page does not come first
    (launch_check.page_first, the one predicate: fix round 2b, item 6) and no money is in yet (no credit, no
    payment paid or on its way: `_hard_lock`'s prepaid test); None otherwise -- a top-up, no draft, or the page
    still to come."""
    plan = plans.latest(conn, business_id)
    if plan is None or plan["status"] != plans.DRAFT or launch_check.page_first(conn, business_id, plan):
        return None
    return None if _hard_lock(conn, business_id, "managed") else plan["id"]


def pay_url(conn, settings: config.Settings, business_id: str) -> str:
    """Where an assistant sends the owner to pay (connect_account's prepaid answer, get_billing's topup_link): the agree
    page of the plan the first payment agrees to (`first_payment_plan`), else the conversation's payment step."""
    plan_id = first_payment_plan(conn, business_id)
    return agree_url(settings, plan_id) if plan_id else f"{settings.web_base_url}/?setup={business_id}"


def agree_refusal(conn, settings: config.Settings, business_id: str, plan_id: str) -> str | None:
    """Why the agree page of `plan_id` takes no payment now, in the owner's words, or None: the link is for a version
    that is no longer the latest (LINK_MOVED); the latest is no draft (NOTHING_TO_REVIEW); its page is not settled
    (PAGE_FIRST); the first blocking item of its checklist (launch_check.blocked, as `pay` refuses it); the business
    cannot move to the prepaid way (its own account linked: `_hard_lock`); money is in already (ALREADY_PAID: the
    first payment was made, the home adds funds); payments are not set up here."""
    latest = plans.latest(conn, business_id)
    if latest is None or latest["id"] != plan_id:
        return LINK_MOVED
    if latest["status"] != plans.DRAFT:
        return NOTHING_TO_REVIEW
    if launch_check.page_first(conn, business_id):
        return PAGE_FIRST
    problem = launch_check.blocked(conn, settings, business_id, plan_id)
    if problem is not None:
        return problem
    mode = business.row(conn, business_id)["ads_mode"]
    locked = _hard_lock(conn, business_id, mode) if mode != "managed" else None
    if locked:
        return locked
    if _hard_lock(conn, business_id, "managed"):
        return ALREADY_PAID
    return None if settings.payments_ready else NOT_SET_UP


def agree_amount_usd(conn, settings: config.Settings, business_id: str) -> int:
    """What the agree page asks for: the first top-up the home's card suggests (D68: two weeks of the daily budget)."""
    return suggested_topup_usd(daily_usd(conn, business_id), settings)


def agree_and_pay(settings: config.Settings, owner: Owner, business_id: str, plan_id: str, ticked: dict) -> str:
    """The agree page's button: the home's own Agree and pay in one request, its Checkout's URL returned. Refused as
    the page is (`agree_refusal`), or -- with nothing changed -- when a document the prepaid way needs and the business
    has not accepted is not ticked (TICK_ALL) or was shown at another version (CHANGED). Then the way, when the
    business does not prepay yet (`choose`), the documents (`accept`), and the plan's Checkout at the suggested amount
    by card (`pay`, which checks everything again), back to the page."""
    with db.connect(settings.database_url) as conn:
        problem = agree_refusal(conn, settings, business_id, plan_id)
        if problem is not None:
            raise Refused(problem)
        needed = [d for d in documents("managed") if not agreements.accepted(conn, business_id, d)]
        mode = business.row(conn, business_id)["ads_mode"]
        amount = agree_amount_usd(conn, settings, business_id)
    given = {d: ticked.get(d) for d in needed}
    if not all(isinstance(v, str) and v for v in given.values()):
        raise Refused(TICK_ALL, status=400)
    if any(given[d] != agreements.version(d) for d in needed):
        raise Refused(CHANGED)
    if mode != "managed":
        choose(settings, owner, business_id, "managed")
    if needed:
        accept(settings, owner, business_id, given)
    return pay(settings, owner, business_id, amount, "card", plan_id, back=f"/agree/{plan_id}")
