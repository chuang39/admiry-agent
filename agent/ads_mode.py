"""How Admiry runs a business's Google ads (spec 2026-09-25 rev. 2 D25, §3 "ads_mode chooser"): `own_account` (the
default — the owner's own Google Ads account linked to Admiry; the owner pays Google) or `managed` (prepaid: a
Google Ads account of Admiry's, picked from its pool by industry -- A3c-1, `google_managed` -- paid from the balance the
owner adds in the conversation). The owner
chooses in the home conversation (setup.choose, which withdraws a pending invitation or expires unpaid Checkouts
first); the choice stays put once something depends on it:
  - to managed: refused while the business's Google Ads link is pending or live (an invitation Google may still
    accept, or an account Admiry already manages for them);
  - to own_account: refused once money was credited or a payment may still land (a paid Checkout Session, or one
    opened in the last 24 hours — a Checkout Session's life).
Anything else goes through an operator. A draft business (its claim released) cannot choose. change() locks the
business row, and setup.pay re-reads the mode under the same lock before it sends the owner to Stripe."""

from __future__ import annotations

import psycopg

from . import agreements, google_ads_link

MODES = ("own_account", "managed")
# The founder's ruling (2026-10-13): the assistant never chooses how the ads run for the owner; it asks, with both ways
# side by side and neither marked first (the home's setup card keeps its own order). connect_account's own answer while
# no way is chosen (tools_channels.CHOOSE), and every `next` an assistant reads that leads there (get_marketing_state's
# account step, get_marketing_plan, a plan's change: surface/actions.translate), ask this question with these two
# choices; each choice's `args` are connect_account's, the own account's Customer ID a placeholder only the owner can
# fill.
WAY_QUESTION = (
    "How should your ads run? Admiry can run them for you from a prepaid balance, with no Google account needed, or "
    "they can run in your own Google Ads account. For your own, what is its 10-digit Customer ID?"
)
# What get_marketing_state says while no way is chosen (owner-chooses review, fix round 1, item 4), in place of the
# words that pick one -- Google Search's account line (its adapter's CONNECT_OWN_ACCOUNT) and the launch's refusal for
# a link not made (NOT_LINKED): neither way named first as the given one. A line's words, as those are: no end mark.
WAY_LINE = "Choose how your ads run: Admiry runs them from a prepaid balance, or in your own Google Ads account"
CUSTOMER_ID = "<Customer ID>"
WAY_CHOICES = (
    {"id": "managed", "label": "Admiry runs them, I prepay (no Google account needed)", "args": {"mode": "managed"}},
    {
        "id": "own_account",
        "label": "My own Google Ads account (the 10-digit Customer ID at the top of its page)",
        "args": {"account_id": CUSTOMER_ID},
    },
)
NOT_CLAIMED = "Claim this business first: open the sign-in link from your assistant, then choose here."
LINK_IN_PLACE = (
    "Your Google Ads account {display} is linked to Admiry or its invitation is on its way, so Admiry runs "
    "the ads there. To change that, ask Admiry."
)
MONEY_IN_PLACE = (
    "Payments were made for this business, or one may still arrive, so its ads stay prepaid. To change "
    "that, ask Admiry; unspent balance is refunded."
)


class ModeRefused(Exception):
    """An owner-readable reason the choice was not changed; `status` is the HTTP code a page answers with."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def current(conn: psycopg.Connection, business_id: str) -> str:
    """How Google Search runs for the business (M0 P6): its `channel_account` row's mode -- which `change` writes
    together with `business.ads_mode` -- or, before the business has that row, the column's; own_account for a
    business there is no row of."""
    row = conn.execute(
        "SELECT coalesce(a.mode, b.ads_mode) AS mode FROM business b LEFT JOIN channel_account a "
        "ON a.business_id = b.id AND a.channel = 'google_search' WHERE b.id = %s",
        (business_id,),
    ).fetchone()
    return row["mode"] if row else MODES[0]


def chosen(conn: psycopg.Connection, business_id: str) -> bool:
    """Whether the owner has chosen how Google Search runs -- the setup card's own reading (setup.state's `chosen`):
    Admiry running it (managed), a Google Ads link of any status, or the management agreement accepted (on the way to
    either). A business that has not is asked WAY_QUESTION before anything connects."""
    if current(conn, business_id) == "managed" or google_ads_link.get(conn, business_id) is not None:
        return True
    return agreements.accepted(conn, business_id)


def asks(conn: psycopg.Connection, biz: str, channel: str = "google_search", plan: dict | None = None) -> bool:
    """Whether the owner is still to be asked how the ads run (the founder's ruling of 2026-10-13: the owner chooses,
    never Admiry nor an assistant): `channel` is Google Search, which Admiry runs both ways (`plans.google_open`), and
    the business has chosen no way yet (`chosen`). Given a `plan`, also that the plan is a draft that funds Google
    Search -- the step a plan answer and get_marketing_state lead to. (tools_channels.asks_way is this rule.)"""
    from . import ads_run, plans  # read when asked: modules this one's own importers load (no cycle at import time)

    if channel != "google_search" or not plans.google_open():
        return False
    if plan is not None and (plan["status"] != plans.DRAFT or channel not in ads_run.funded_channels(plan)):
        return False
    return not chosen(conn, biz)


def neutral(text: str) -> str:
    """`text` with the words that pick a way while none is chosen said as WAY_LINE: Google Search's refusal for a link
    not made (its adapter's NOT_LINKED) and its line asking for the owner's Customer ID (CONNECT_OWN_ACCOUNT), alone or
    among other places' sentences; any other text as it is."""
    from .channels.google_search import adapter as google  # the adapter's own sentences, read when said

    return text.replace(google.NOT_LINKED, WAY_LINE).replace(google.CONNECT_OWN_ACCOUNT, WAY_LINE)


def unchosen_words(conn: psycopg.Connection, biz: str, text: str, plan: dict | None = None) -> str:
    """`text` as said to the owner of `biz`: `neutral` while no way is chosen (`asks`, for `plan` when given), else as
    it is -- once a way is chosen, today's sentences stand (Addendum 3 of the founder's ruling of 2026-10-13: the
    launch's refusal, get_connection_status's next step, the home's blocker card). Reads the database only when `text`
    carries words that pick a way."""
    said = neutral(text)  # once (final review, item 5)
    if said == text:
        return text
    return said if asks(conn, biz, plan=plan) else text


def choices() -> list[dict]:
    """WAY_CHOICES as an answer's data carries them, as the checklist's decide items do (launch_check.fix_for): each
    {label, args}, in their order, fresh copies."""
    return [{"label": c["label"], "args": dict(c["args"])} for c in WAY_CHOICES]


def way_actions() -> list[dict]:
    """WAY_CHOICES as `next` actions an assistant reads: connect_account with each choice's arguments, neither
    primary (fresh copies)."""
    return [
        {"id": c["id"], "label": c["label"], "tool": "connect_account", "args": dict(c["args"])} for c in WAY_CHOICES
    ]


def locked(conn: psycopg.Connection, business_id: str, current: str) -> str | None:
    """Why the business cannot leave `current` now (an owner sentence), or None. A bank transfer on its way is
    money in place (plan 2026-10-04-agent-m1m-1 P3)."""
    if current == "own_account":
        link = google_ads_link.get(conn, business_id)
        if link is not None and (link.live or link.status == "pending"):
            return LINK_IN_PLACE.format(display=link.display)
        return None
    moved = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM wallet_entry WHERE business_id = %(b)s) "
        "OR EXISTS (SELECT 1 FROM payment_session WHERE business_id = %(b)s AND (status IN ('paid', 'processing') "
        "OR (status = 'open' AND created_at > now() - interval '24 hours'))) AS moved",
        {"b": business_id},
    ).fetchone()
    return MONEY_IN_PLACE if moved["moved"] else None


def change(conn: psycopg.Connection, business_id: str, to: str) -> bool:
    """Set the business's ads_mode, and Google Search's `channel_account` mode with it (M0 P6), inside the caller's
    transaction (the business row is locked first): True when it changed, False when it already was `to`;
    ModeRefused otherwise."""
    if to not in MODES:
        raise ModeRefused("Choose one of the two ways.", status=400)
    row = conn.execute(
        "SELECT claim_state, ads_mode FROM business WHERE id = %s AND status = 'active' FOR UPDATE", (business_id,)
    ).fetchone()
    if row is None:
        raise ModeRefused("no such business", status=404)
    if row["ads_mode"] == to:
        return False
    if row["claim_state"] == "draft":
        raise ModeRefused(NOT_CLAIMED)
    reason = locked(conn, business_id, row["ads_mode"])
    if reason:
        raise ModeRefused(reason)
    conn.execute("UPDATE business SET ads_mode = %s, updated_at = now() WHERE id = %s", (to, business_id))
    google_ads_link.mirror(conn, business_id)  # Google Search's account row carries the mode too (M0 P6)
    return True
