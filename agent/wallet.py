"""The prepaid advertising balance of a managed business (spec 2026-09-25 rev. 2 D29, D30, §5 "Managed mode"; the
wallet design of rev. 1 §3–§4, commit 202668c). Append-only wallet_entry rows in integer cents; balance =
SUM(amount_cents). Every row carries a ref unique per business, so a replayed Stripe event, a re-run job or a
repeated operator step adds nothing: the Checkout Session id for a top-up, "{channel}:{day}" for one day's spend
(A3's wallet-settle), the Stripe refund id for a refund, ops:<ns>:<hex> for an operator correction.
Only businesses whose ads Admiry runs, prepaid (ads_mode 'managed'), take money in: /pay, the balance tools and
agent.ops adjust check it. A top-up credits the amount the owner chose (D92: what the way of paying costs is
Stripe's), even if the business stopped being prepaid meanwhile (the operator refunds it). Payment is verification
(D29) for managed businesses only: the first paid top-up verifies through claims.verify (verified_via 'payment'). A
no-funds pause (jobs.spend_guard) lifts only once the balance is above zero. USD floats exist only at the tool
boundary (cents_from_usd, to_usd). Owner copy says "balance", "add funds", "spent", "refund" — never this module's
name."""

from __future__ import annotations

import math
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg

from . import claims

KINDS = ("topup", "spend", "refund", "adjustment")
CREATORS = ("stripe", "job", "ops")
RAILS = ("card", "ach")
TOPUP_WORDS = {"card": "Card payment", "ach": "Bank transfer"}
REFUND_WORDS = {"card": "Refund to the card", "ach": "Refund to the bank account"}
_OWNER_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")  # ASCII digits only: no 1e3, NaN or other scripts
_OPS_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]{1,2})?")


@dataclass(frozen=True)
class Quote:
    """What adding `credit_cents` costs by `rail` (D92): the credit plus the payment cost, which Stripe keeps."""

    rail: str
    credit_cents: int
    processing_cents: int
    total_cents: int


def quote(credit_cents: int, rail: str, settings) -> Quote:
    """The payment cost at the door (channels spec D92, plan P1): by card `card_cost_bps` of the credit rounded half
    up plus `card_cost_fixed_cents`; by bank transfer `ach_cost_bps` of it, at most `ach_cost_cap_cents`. Integer
    arithmetic only; static/setup.js `quoteCents` mirrors it to the cent (test_setup's parity test)."""
    if rail == "card":
        cost = (credit_cents * settings.card_cost_bps + 5000) // 10000 + settings.card_cost_fixed_cents
    elif rail == "ach":
        cost = min((credit_cents * settings.ach_cost_bps + 5000) // 10000, settings.ach_cost_cap_cents)
    else:
        raise ValueError(f"unknown rail {rail!r}")
    return Quote(rail, credit_cents, cost, credit_cents + cost)


def rails(settings) -> tuple[str, ...]:
    """The ways of paying on offer: the card always, the bank transfer when AGENT_ACH_TOPUPS is on (plan P6)."""
    return RAILS if settings.ach_topups else ("card",)


def topup_note(rail: str, total_cents: int, processing_cents: int) -> str:
    """The ledger note of a credited top-up (the owner reads it in Settings and list_payments)."""
    return f"{TOPUP_WORDS[rail]} of {usd(total_cents)} ({usd(processing_cents)} transaction fee)"


def refund_note(rail: str | None) -> str:
    return REFUND_WORDS.get(rail or "card", REFUND_WORDS["card"])


class AmountRefused(ValueError):
    """An owner-readable reason an amount is not accepted (a sentence ending in a period)."""


def usd(cents: int) -> str:
    """Owner text: '$1,234.56', '-$25.50', '$0.00'."""
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def to_usd(cents: int) -> float:
    """Tool results: USD with two decimals."""
    return round(cents / 100, 2)


def _out_of_bounds(min_usd: int, max_usd: int) -> AmountRefused:
    return AmountRefused(f"The amount must be between ${min_usd:,} and ${max_usd:,}.")


def _bounded(cents: int, min_usd: int, max_usd: int) -> int:
    if not min_usd * 100 <= cents <= max_usd * 100:
        raise _out_of_bounds(min_usd, max_usd)
    return cents


def parse_usd(text: str, *, min_usd: int, max_usd: int) -> int:
    """Cents from what the owner typed on /pay ("500", "$1,250.50"): digits with at most two decimals, within
    min_usd..max_usd; AmountRefused for anything else (negative, zero, 12.345, 1e3, NaN, other scripts)."""
    s = (text or "").strip().replace(",", "").removeprefix("$").strip()
    if not _OWNER_AMOUNT.fullmatch(s):
        raise AmountRefused("Enter an amount in dollars, like 500 or 1250.50.")
    return _bounded(int(Decimal(s) * 100), min_usd, max_usd)


def cents_from_usd(amount_usd, *, min_usd: int, max_usd: int) -> int:
    """The tool boundary: a USD number -> cents with round(x * 100), then the page's bounds. The dollars are
    bounded before rounding too: round(1e308 * 100) would overflow instead of being refused."""
    if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float)) or not math.isfinite(amount_usd):
        raise AmountRefused("amount_usd must be a number of dollars.")
    if not min_usd <= amount_usd <= max_usd:
        raise _out_of_bounds(min_usd, max_usd)
    return _bounded(round(amount_usd * 100), min_usd, max_usd)


def ops_cents(text: str, *, signed: bool) -> int:
    """An operator's amount (agent.ops refund / adjust): dollars with at most two decimals, never zero; a minus
    sign only when `signed`."""
    s = (text or "").strip()
    if not _OPS_AMOUNT.fullmatch(s) or (s.startswith("-") and not signed):
        raise AmountRefused(
            "amount must be dollars with at most two decimals" + ("" if signed else ", above zero") + "."
        )
    cents = int(Decimal(s) * 100)
    if cents == 0:
        raise AmountRefused("amount must not be zero.")
    return cents


def balance_cents(conn: psycopg.Connection, business_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(sum(amount_cents), 0)::bigint AS b FROM wallet_entry WHERE business_id = %s", (business_id,)
    ).fetchone()["b"]


def add_entry(
    conn: psycopg.Connection,
    business_id: str,
    *,
    kind: str,
    amount_cents: int,
    ref: str,
    created_by: str,
    note: str | None = None,
) -> int | None:
    """Insert one entry: its id, or None when this business already has an entry with this ref (a replay)."""
    if kind not in KINDS or created_by not in CREATORS:
        raise ValueError(f"unknown kind {kind!r} or creator {created_by!r}")
    row = conn.execute(
        "INSERT INTO wallet_entry (business_id, kind, amount_cents, ref, note, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (business_id, ref) DO NOTHING RETURNING id",
        (business_id, kind, amount_cents, ref, note, created_by),
    ).fetchone()
    return row["id"] if row else None


def spend(conn: psycopg.Connection, business_id: str, *, channel: str, day: date, cost_cents: int) -> int | None:
    """One channel's cost for one day (the A3 wallet-settle contract): -cost_cents with ref "{channel}:{day}";
    a re-run adds nothing and a zero-cost day adds no row. Late corrections are adjustments, never rewrites."""
    if cost_cents <= 0:
        return None
    return add_entry(
        conn, business_id, kind="spend", amount_cents=-cost_cents, ref=f"{channel}:{day.isoformat()}", created_by="job"
    )


def adjust(conn: psycopg.Connection, business_id: str, amount_cents: int, *, note: str) -> int:
    """An operator's signed correction; the note is required (the owner reads it in list_payments)."""
    if not (note or "").strip() or amount_cents == 0:
        raise ValueError("an adjustment needs a non-zero amount and a note")
    entry = add_entry(
        conn,
        business_id,
        kind="adjustment",
        amount_cents=amount_cents,
        ref=f"ops:{time.time_ns()}:{secrets.token_hex(3)}",
        created_by="ops",
        note=note.strip(),
    )
    assert entry is not None  # the ref is fresh
    return entry


def refund(
    conn: psycopg.Connection,
    business_id: str,
    *,
    refund_id: str,
    amount_cents: int,
    session_id: str,
    note: str | None = None,
) -> int | None:
    """One Stripe refund against one paid session: -amount_cents with ref = the refund id (a repeat adds
    nothing); the session's refunded_cents grows by the same amount. Inside the caller's transaction."""
    rail = conn.execute(
        "SELECT rail FROM payment_session WHERE id = %s AND business_id = %s", (session_id, business_id)
    ).fetchone()
    entry = add_entry(
        conn,
        business_id,
        kind="refund",
        amount_cents=-amount_cents,
        ref=refund_id,
        created_by="ops",
        note=(note or "").strip() or refund_note(rail["rail"] if rail else None),
    )
    if entry is not None:
        conn.execute(
            "UPDATE payment_session SET refunded_cents = refunded_cents + %s WHERE id = %s AND business_id = %s",
            (amount_cents, session_id, business_id),
        )
    return entry


def has_paid(conn: psycopg.Connection, business_id: str) -> bool:
    """Whether the business ever paid Admiry -- a card top-up credited (Stripe's webhook); an operator's credit is no
    payment. A3c-1, founder 2026-10-03: only such a business gets a Google Ads account of Admiry's."""
    row = conn.execute(
        "SELECT 1 FROM wallet_entry WHERE business_id = %s AND kind = 'topup' LIMIT 1", (business_id,)
    ).fetchone()
    return row is not None


def verify_by_payment(conn: psycopg.Connection, business_id: str) -> bool:
    """A prepaid business's first paid top-up verifies it (D29): claims.verify with verified_via 'payment' and
    ads_mode 'managed' — an own_account business verifies by its Google Ads link, never by money."""
    return claims.verify(conn, business_id, via="payment", ads_mode="managed")


def resume_if_funded(conn: psycopg.Connection, business_id: str) -> bool:
    """Lift a no-funds pause once the balance is above zero (the Google adapter's resume call lands in A3)."""
    row = conn.execute(
        "UPDATE business SET paused_reason = NULL, updated_at = now() WHERE id = %s "
        "AND paused_reason = 'no_funds' AND (SELECT COALESCE(sum(amount_cents), 0) FROM wallet_entry "
        "WHERE business_id = %s) > 0 RETURNING id",
        (business_id, business_id),
    ).fetchone()
    return row is not None


def topup(conn: psycopg.Connection, *, session_id: str, amount_cents: int, payment_intent: str | None) -> dict:
    """The webhook's whole effect for a paid Checkout Session, inside the caller's transaction: credit the balance
    once (ref = session id) with the amount the owner chose -- our row's credited_cents, never what Stripe charged
    (D92: the payment cost is Stripe's) -- mark the session paid, verify a claimed prepaid business, lift a no-funds
    pause when the balance is positive again. The business and the amounts come from our payment_session row (locked
    FOR UPDATE, so two deliveries of one session queue here), never from the event. Not credited: a session this
    server did not create, a zero amount, one already credited, or a total that differs from the price we set (the
    caller tells the operator). A row from before D92 (credited_cents NULL) credits what it charged, as v0 promised."""
    s = conn.execute(
        "SELECT business_id, amount_cents, COALESCE(credited_cents, amount_cents) AS credit, "
        "COALESCE(processing_cents, 0) AS cost, COALESCE(rail, 'card') AS rail "
        "FROM payment_session WHERE id = %s FOR UPDATE",
        (session_id,),
    ).fetchone()
    if s is None:
        return {"credited": False, "reason": "unknown session"}
    biz = s["business_id"]
    if amount_cents <= 0:
        return {"credited": False, "reason": "nothing paid", "business_id": biz}
    if amount_cents != s["amount_cents"]:
        return {"credited": False, "reason": "amount mismatch", "business_id": biz, "expected_cents": s["amount_cents"]}
    note = topup_note(s["rail"], s["amount_cents"], s["cost"])
    if (
        add_entry(conn, biz, kind="topup", amount_cents=s["credit"], ref=session_id, created_by="stripe", note=note)
        is None
    ):
        return {"credited": False, "reason": "already credited", "business_id": biz}
    conn.execute(
        "UPDATE payment_session SET status = 'paid', paid_at = now(), payment_intent = %s WHERE id = %s",
        (payment_intent, session_id),
    )
    verified_now = verify_by_payment(conn, biz)
    return {
        "credited": True,
        "business_id": biz,
        "amount_cents": s["credit"],
        "credited_cents": s["credit"],
        "paid_cents": s["amount_cents"],
        "processing_cents": s["cost"],
        "rail": s["rail"],
        "verified_now": verified_now,
        "resumed": resume_if_funded(conn, biz),
    }


def summary(conn: psycopg.Connection, business_id: str) -> dict:
    """balance_cents, pending_topups (sessions opened in the last 24 hours — a Checkout Session's life — that
    Stripe has not confirmed, or any age away still 'processing' -- a bank transfer takes days), open_topups (only
    the 'open' ones among those -- a card session still waiting on Stripe, never a bank transfer already
    'processing'; the pay form's returning-from-Stripe poll tells the two apart, finding 1), transfer_pending_cents
    (the credit of the 'processing' ones only: a bank transfer is money moving, not yet money) and last_topup_at."""
    row = conn.execute(
        "SELECT COALESCE(sum(amount_cents), 0)::bigint AS balance, "
        "max(created_at) FILTER (WHERE kind = 'topup') AS last_topup FROM wallet_entry WHERE business_id = %s",
        (business_id,),
    ).fetchone()
    pending = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status = 'open') AS open_n, "
        "COALESCE(sum(COALESCE(credited_cents, amount_cents)) FILTER "
        "(WHERE status = 'processing'), 0)::bigint AS transfer FROM payment_session WHERE business_id = %s "
        "AND (status = 'processing' OR (status = 'open' AND created_at > now() - interval '24 hours'))",
        (business_id,),
    ).fetchone()
    return {
        "balance_cents": row["balance"],
        "pending_topups": pending["n"],
        "open_topups": pending["open_n"],
        "transfer_pending_cents": pending["transfer"],
        "last_topup_at": row["last_topup"],
    }


def entries(
    conn: psycopg.Connection, business_id: str, *, cursor: int | None = None, limit: int = 50
) -> tuple[list[dict], int | None]:
    """Money in and out, newest first — top-ups, refunds, adjustments; never the daily spend rows.
    Returns (rows, next_cursor)."""
    rows = conn.execute(
        "SELECT id, kind, amount_cents, note, created_at FROM wallet_entry WHERE business_id = %s "
        "AND kind <> 'spend' AND (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
        (business_id, cursor, cursor, limit + 1),
    ).fetchall()
    page = [dict(r) for r in rows[:limit]]
    return page, (page[-1]["id"] if len(rows) > limit and page else None)


@contextmanager
def lock(conn: psycopg.Connection, business_id: str) -> Iterator[None]:
    """Serialise balance-changing work for one business across processes: a session advisory lock on
    "wallet:<id>" (agent.ops refund / adjust now; A3's wallet-settle takes the same lock)."""
    key = f"wallet:{business_id}"
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))


def refund_plan(conn: psycopg.Connection, business_id: str, cents: int) -> list[dict]:
    """How to return `cents` to the payments that paid: the most recently paid sessions first, each up to what was
    credited from it and is not refunded yet (D92: never the payment cost).
    [{"session_id", "payment_intent", "refunded_before", "amount_cents"}]; the amounts sum to less than `cents`
    when the payments cannot cover it (the caller refuses)."""
    rows = conn.execute(
        "SELECT id, payment_intent, COALESCE(credited_cents, amount_cents) AS refundable, refunded_cents "
        "FROM payment_session WHERE business_id = %s AND status = 'paid' AND payment_intent IS NOT NULL "
        "AND refunded_cents < COALESCE(credited_cents, amount_cents) ORDER BY paid_at DESC, id DESC",
        (business_id,),
    ).fetchall()
    plan, left = [], cents
    for r in rows:
        if left <= 0:
            break
        take = min(left, r["refundable"] - r["refunded_cents"])
        plan.append(
            {
                "session_id": r["id"],
                "payment_intent": r["payment_intent"],
                "refunded_before": r["refunded_cents"],
                "amount_cents": take,
            }
        )
        left -= take
    return plan
