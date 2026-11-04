"""The balance tools (spec 2026-09-25 rev. 2 §3: get_balance, request_payment_link, list_payments) — for a business
whose ads Admiry runs in an account it opens for it, prepaid (ads_mode managed); a business whose owner chose their own
account pays Google directly and gets a readable refusal, and one whose owner has chosen no way yet is asked first, as
connect_account asks (final review, item 2). All read-only: the assistant never moves money. request_payment_link
returns the plan's own agree page for a first payment (setup.pay_url; the founder's ruling 3, 2026-10-13: the plan,
the documents the owner ticks, one button to Stripe), else the Admiry conversation at the payment step, where the
owner signs in and pays on Stripe's page. Owner words: balance, add funds, spent, refund."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import ads_mode, agreements, business, config, envelope, launch_check, setup, stripe_mode, wallet
from .mcp_app import agent_tool, need_business, run
from .util import iso

NOT_SET_UP = "payments are not set up on this server yet"
# No field and no link the answer does not carry (final review C, minor 1; open item A5)
NOT_MANAGED = (
    "this business runs its ads in the owner's own Google Ads account and pays Google directly, so it has no Admiry "
    "balance"
)
CLAIM_FIRST = "the owner must claim this business first: give them the sign-in link"
NEEDS_TERMS = (
    "the owner accepts the management agreement and the prepayment terms in the Admiry conversation (connect_url) "
    "first; then ask for the payment link again"
)
# Page-first review, item 8: the page for the ads comes before the money (setup.PAGE_FIRST refuses the payment) -- its
# question and ways, or the page in hand's own step (final review, item 1)
PAGE_FIRST = "the page for the ads comes first (ask_owner, choices); then ask for the payment link again"
# Final review, item 2: no way chosen yet -- no balance, and not the owner's own account either; the answer asks as
# connect_account does (the page first while it comes first, then how the ads run)
NOT_CHOSEN = (
    "the owner has not chosen how the ads run, so there is no balance yet: put ask_owner and its choices to the owner "
    "and send their answer as next says"
)


def prices(conn, settings, business_id: str, amount_usd: float | None) -> tuple[dict, str]:
    """D92: what adding `amount_usd` (or the conversation's suggested amount) costs each way the owner can pay, and
    the sentence that says it -- the same quote the pay form and Stripe's page show."""
    if amount_usd is None:
        credit = setup.suggested_topup_usd(setup.daily_usd(conn, business_id), settings) * 100
    else:
        credit = wallet.cents_from_usd(amount_usd, min_usd=settings.topup_min_usd, max_usd=settings.topup_max_usd)
    quotes = {r: wallet.quote(credit, r, settings) for r in wallet.rails(settings)}
    out = {"amount_usd": wallet.to_usd(credit), "card_usd": wallet.to_usd(quotes["card"].total_cents)}
    card = quotes["card"]
    ways = (
        f"{wallet.usd(card.total_cents)} by card, which includes a {wallet.usd(card.processing_cents)} transaction fee"
    )
    if "ach" in quotes:  # P8: the bank transfer is less than the card price, never the card more than it
        bank = quotes["ach"]
        out["bank_usd"] = wallet.to_usd(bank.total_cents)
        ways += (
            f"; or {wallet.usd(bank.total_cents)} by bank transfer, {wallet.usd(card.total_cents - bank.total_cents)} "
            "less, arriving in about 4 business days"
        )
    note = (
        f"Add {wallet.usd(credit)}: {ways}. Admiry adds no fee: the transaction fee is what Stripe charges to process "
        "the payment, and it is not refunded."
    )
    return out, note


def _ask_page(conn, business_id: str, out: dict) -> dict:
    """The answer while the page comes first (launch_check.page_first): `out` with the page's question and its ways, or
    the page in hand's own step (launch_check.page_ask), as connect_account's page_first answers; nothing changes."""
    question, choices = launch_check.page_ask(conn, business_id)
    out.update(ask_owner=question, choices=choices)
    return envelope.make(out, say=question, next=envelope.actions(*launch_check.page_actions(choices)))


def _prepaid(conn, business_id: str) -> tuple[dict, dict | None]:
    """(the business row, None) for a business whose ads Admiry runs from its balance. While the owner has chosen no
    way (ads_mode.asks; final review, item 2: naming the own account would choose it for them), (the row, the answer)
    instead, every view alike: as connect_account's own question -- the page first while it comes first
    (`_ask_page`), else how the ads run (WAY_QUESTION, both ways side by side, neither marked first); nothing changes.
    ToolError NOT_MANAGED once the owner chose their own account."""
    row = business.row(conn, business_id)
    if row["ads_mode"] == "managed":
        return row, None
    if not ads_mode.asks(conn, business_id):
        raise ToolError(NOT_MANAGED)
    if launch_check.page_first(conn, business_id):
        return row, _ask_page(conn, business_id, {"note": NOT_CHOSEN})
    out = {"note": NOT_CHOSEN, "ask_owner": ads_mode.WAY_QUESTION, "choices": ads_mode.choices()}
    return row, envelope.make(out, say=ads_mode.WAY_QUESTION, next=envelope.actions(*ads_mode.way_actions()))


@agent_tool(cls="RO", scope="read")
def get_balance() -> dict:
    """This business's prepaid advertising balance in USD, when Admiry runs its ads in an account it opens for it
    (whoami's ads_mode is managed): balance_usd, pending_topups (payments started in the last 24 hours that are not
    confirmed yet, and bank transfers on their way (they take about 4 business days)), transfer_pending_usd (only
    while one is on its way), last_topup_at, verified (the first payment verifies the business) and paused (true
    when the balance ran out). Call it before suggesting a change that spends; when the balance reaches zero Admiry
    pauses the ads and the owner adds funds through request_payment_link. Every dollar is spent at Google's exact
    cost."""

    def body(conn, p):
        biz = need_business(p)
        row, asked = _prepaid(conn, biz)
        if asked is not None:
            return asked
        s = wallet.summary(conn, biz)
        out = {
            "balance_usd": wallet.to_usd(s["balance_cents"]),
            "pending_topups": s["pending_topups"],
            "last_topup_at": iso(s["last_topup_at"]),
            "verified": row["claim_state"] == "verified",
            "paused": row["paused_reason"] == "no_funds",
        }
        if s["transfer_pending_cents"]:
            out["transfer_pending_usd"] = wallet.to_usd(s["transfer_pending_cents"])
        if not stripe_mode.for_owner(config.load(), row["owner_email"]).ready:  # per owner (Task 8b)
            out["note"] = NOT_SET_UP
        return out

    return run("get_balance", "read", {}, body)


@agent_tool(cls="RO", scope="manage")
def request_payment_link(amount_usd: float | None = None) -> dict:
    """The link where the owner adds funds to the prepaid advertising balance later, with the allowed range
    min_usd..max_usd. The first payment's link is connect_account's for Admiry's way (connect_channel with mode
    managed: the plan's own agree page -- the plan, the management agreement and the prepayment terms the owner ticks
    there, one button to Stripe; the founder's ruling 3 of 2026-10-13; final review, item 2); asked here for a first
    payment of a draft plan whose page is settled, pay_url is that same page (setup.pay_url). Otherwise pay_url opens
    the Admiry conversation at the payment step: the owner signs in with Google, accepts the terms there if needed and
    pays by card on Stripe's page. amount_usd (optional, USD, at most two decimals) must lie in that range; the
    conversation suggests the amount itself (two weeks of the budget). pay_url is null, with a note saying what comes
    first, until the owner can pay: a business still in setup is claimed first (request_connect_link); the management
    agreement and the prepayment terms are accepted on connect_url, unless pay_url is the agree page, which asks them;
    while the latest draft's page is not settled, the page comes first (the payment would be refused:
    setup.PAGE_FIRST), so the answer is the page question and its ways, or the page in hand's own step (`ask_owner`,
    `choices`, and `next`, as connect_account's page_first) -- unless ads are built, whose top-up is never held for a
    newer draft's page (setup.waits_for_page). While the owner has chosen no way, it asks as connect_account does
    (`_prepaid`). The first payment verifies a claimed business.
    prices: what that amount (or the suggested one) costs by card and, when offered, by bank transfer; price_note
    says it -- relay it as written. Never ask for card details in the conversation. Needs the manage scope."""

    def body(conn, p):
        biz = need_business(p)
        row, asked = _prepaid(conn, biz)
        if asked is not None:
            return asked
        s = config.load()
        agree = setup.first_payment_plan(conn, biz)  # the plan's agree page asks the terms itself
        url = setup.agree_url(s, agree) if agree else f"{s.web_base_url}/?setup={biz}"
        out = {"pay_url": url, "min_usd": s.topup_min_usd, "max_usd": s.topup_max_usd}
        if amount_usd is not None:  # still checked, so a bad value is refused; the conversation suggests its own amount
            try:
                wallet.cents_from_usd(amount_usd, min_usd=s.topup_min_usd, max_usd=s.topup_max_usd)
            except wallet.AmountRefused as e:
                raise ToolError(str(e)) from None
        out["prices"], out["price_note"] = prices(conn, s, biz, amount_usd)
        if row["claim_state"] == "draft":
            out.update(pay_url=None, note=CLAIM_FIRST)
        elif setup.waits_for_page(conn, biz):  # the home's own rule: no link it refuses, none it takes held back
            out.update(pay_url=None, note=PAGE_FIRST)
            return _ask_page(conn, biz, out)
        elif not agree and not agreements.terms_accepted(conn, biz):
            out.update(pay_url=None, connect_url=url, note=NEEDS_TERMS)
        elif not stripe_mode.for_owner(s, row["owner_email"]).ready:  # the sandbox for an allowlisted owner (Task 8b)
            out.update(pay_url=None, note=NOT_SET_UP)
        return out

    return run("request_payment_link", "manage", {"amount_usd": amount_usd}, body)


@agent_tool(cls="RO", scope="read")
def list_payments(cursor: int | None = None, limit: int = 50) -> dict:
    """Money added to and returned from the prepaid advertising balance, newest first: card payments (kind topup),
    refunds and corrections (kind adjustment), each with amount_usd, note and created_at — never the daily ad spend
    detail (get_balance has the balance). Paginate with next_cursor; limit at most 100."""

    def body(conn, p):
        biz = need_business(p)
        _row, asked = _prepaid(conn, biz)
        if asked is not None:
            return asked
        rows, next_cursor = wallet.entries(conn, biz, cursor=cursor, limit=max(1, min(int(limit), 100)))
        return {
            "payments": [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "amount_usd": wallet.to_usd(r["amount_cents"]),
                    "note": r["note"],
                    "created_at": iso(r["created_at"]),
                }
                for r in rows
            ],
            "next_cursor": next_cursor,
        }

    return run("list_payments", "read", {"cursor": cursor, "limit": limit}, body)
