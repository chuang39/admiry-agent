"""Stripe's webhook (spec 2026-09-25 rev. 2 §5 "Managed mode", D28–D31): POST /hooks/stripe is the source of truth
for money in — a paid Checkout credits the balance once. Adding funds itself is setup.pay, in the home conversation
(spec 2026-09-29); GET /pay/{business_id} only sends old links there. Owner copy: balance, add funds, spent, refund."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import audit, config, db, mail, mail_out, plan_review, setup, stripe_client, wallet
from .web import NO_STORE

log = logging.getLogger(__name__)
router = APIRouter()
PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
FAILED_EVENT = "checkout.session.async_payment_failed"  # a bank debit that did not go through (plan P3)
# Google's third-party policy, transparency (founder 2026-10-03): when 80% or more of a partner's customers spend under
# $1,000 a month on Google Ads, the partner links Google's "Advertiser guide: Working with third parties" on its site and
# tells each customer of it at every new sale or renewal -- for Admiry, every credited top-up (`_tell_owner_paid`)
THIRD_PARTY_GUIDE = "https://support.google.com/adspolicy/answer/9457109"


@router.get("/pay/{business_id}")
def pay_page(business_id: str) -> RedirectResponse:
    """Old links (assistants, Stripe returns of Checkouts started before 2026-09-29): adding funds is in the
    conversation now; the conversation checks the session and the owner."""
    return RedirectResponse(f"/?setup={business_id}", status_code=303, headers=NO_STORE)


@router.post("/hooks/stripe")
async def stripe_hook(request: Request) -> JSONResponse:
    """Stripe's webhook. The raw body is read here (the signature covers the exact bytes); the work runs in the
    thread pool like every other route (psycopg is blocking). No session and no CSRF: the signature is the
    authentication."""
    payload = await request.body()
    return await run_in_threadpool(handle_event, payload, request.headers.get("stripe-signature"))


def handle_event(payload: bytes, signature: str | None) -> JSONResponse:
    """Verify, then act: a paid Checkout (completed, or an async method that succeeded later) credits the balance
    once through wallet.topup with its audit row in the same transaction; a completed-but-unpaid Checkout is a bank
    debit on its way (`_on_its_way`, `processing` until Stripe says paid or failed); an expired Checkout marks the
    session; a failed bank debit is `_failed`; everything else is acknowledged (200) so Stripe stops retrying. Bad
    signature -> 400, nothing written."""
    settings = config.load()
    if not settings.stripe_webhook_secret:
        return JSONResponse({"error": "payments are not set up"}, status_code=503)
    try:
        stripe_client.verify_signature(payload, signature, settings.stripe_webhook_secret)
        event = json.loads(payload)
    except (stripe_client.BadSignature, ValueError):
        return JSONResponse({"error": "bad signature"}, status_code=400)
    kind = event.get("type") if isinstance(event, dict) else None
    data = event.get("data") if isinstance(event, dict) else None
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
        return JSONResponse({"received": True, "ignored": str(kind)})
    if kind in PAID_EVENTS:
        amount, intent = obj.get("amount_total"), obj.get("payment_intent")
        if kind == "checkout.session.completed" and obj.get("payment_status") == "unpaid":
            return _on_its_way(settings, obj["id"])  # a bank debit: paid days later, or failed
        if obj.get("payment_status") != "paid" or not isinstance(amount, int):
            return JSONResponse({"received": True, "ignored": "not paid"})
        with db.connect(settings.database_url) as conn, conn.transaction():
            out = wallet.topup(
                conn,
                session_id=obj["id"],
                amount_cents=amount,
                payment_intent=intent if isinstance(intent, str) else None,
            )
            if out["credited"]:
                audit.record_system(
                    conn,
                    tool="web.topup",
                    actor="stripe",
                    business_id=out["business_id"],
                    args={"session_id": obj["id"], "event_id": event.get("id")},
                    result={"amount_usd": wallet.to_usd(out["amount_cents"]), "verified_now": out["verified_now"]},
                )
        if out["credited"]:
            setup.approve_paid(settings, obj["id"])  # Agree and pay: the plan the Checkout named (L1 §5)
            _tell_owner_paid(settings, out)
        elif out.get("reason") == "amount mismatch":
            _tell_operator_mismatch(settings, obj["id"], out, amount)
        return JSONResponse({"received": True, "credited": out["credited"]})
    if kind == "checkout.session.expired":
        with db.connect(settings.database_url) as conn:
            conn.execute(
                "UPDATE payment_session SET status = 'expired' WHERE id = %s AND status = 'open'", (obj["id"],)
            )
        return JSONResponse({"received": True})
    if kind == FAILED_EVENT:
        return _failed(settings, obj["id"])
    return JSONResponse({"received": True, "ignored": kind})


def _on_its_way(settings: config.Settings, session_id: str) -> JSONResponse:
    """A completed Checkout that is not paid yet -- a bank debit (plan 2026-10-04-agent-m1m-1 P3): `processing` until
    Stripe says paid or failed. Only an `open` row moves: an outcome delivered first stays as it is."""
    with db.connect(settings.database_url) as conn:
        conn.execute(
            "UPDATE payment_session SET status = 'processing' WHERE id = %s AND status = 'open'", (session_id,)
        )
    return JSONResponse({"received": True, "processing": True})


def _failed(settings: config.Settings, session_id: str) -> JSONResponse:
    """A bank debit that did not go through: nothing was credited, so nothing is taken back; the owner is told once
    (the row moves once -- a redelivery finds it failed already and sends nothing)."""
    with db.connect(settings.database_url) as conn:
        row = conn.execute(
            "UPDATE payment_session SET status = 'failed' WHERE id = %s AND status IN ('open', 'processing') "
            "RETURNING business_id, amount_cents",
            (session_id,),
        ).fetchone()
    if row is not None:
        _mail_owner(
            settings,
            row["business_id"],
            "mail_transfer_failed.txt.j2",
            "Your bank transfer to Admiry did not go through",
            paid=wallet.usd(row["amount_cents"]),
        )
    return JSONResponse({"received": True, "failed": True})


def _tell_operator_mismatch(settings: config.Settings, session_id: str, out: dict, charged: int) -> None:
    """Stripe charged something other than the price we set: nothing is credited, so the mismatched session never
    reaches wallet.refund_plan (status 'paid' with a payment_intent) -- `python -m agent.ops refund` would instead
    pull money off the business's OTHER, correctly credited payments (controller ruling R4). The operator refunds
    the mismatched charge in the Stripe dashboard (nothing came off the balance, so nothing is owed back), or credits
    the business by hand with `agent.ops adjust`. One mail per session: the Idempotency-Key is the session id. The
    mail itself is best-effort like `_mail_owner`'s (finding 3): logged first, so the session and the business are
    on record even with no operator address or Resend to send it to, and never let to 500 the webhook -- Stripe's
    refusal to credit has already landed by the time this runs."""
    log.warning(
        "pay: Checkout %s for business %s was paid %s but priced %s; nothing was credited",
        session_id,
        out["business_id"],
        wallet.usd(charged),
        wallet.usd(out["expected_cents"]),
    )
    key = f"stripe-mismatch-{session_id}"
    try:
        mail_out.send_plain(
            settings,
            to=settings.ops_email or "",
            subject=f"[Admiry] Stripe charged an unexpected amount for {out['business_id']}",
            paragraphs=[
                (
                    f"Checkout {session_id} for business {out['business_id']} was paid {wallet.usd(charged)}, but "
                    f"Admiry priced it at {wallet.usd(out['expected_cents'])}. Nothing was added to the balance."
                ),
                (
                    f"Find payment {session_id} in the Stripe dashboard and refund it there (nothing was added to "
                    "the balance, so nothing comes off it), or credit the business by hand with "
                    "`python -m agent.ops adjust`."
                ),
            ],
            idempotency_key=key,
            gate=None if settings.ops_email else "AGENT_OPS_EMAIL unset",
            message_id=key,
        )
    except Exception as e:  # noqa: BLE001 - Stripe's refusal to credit is already decided; a mail that cannot go out is only logged
        log.warning("pay: mismatch mail for %s not sent: %s", session_id, type(e).__name__)


def _tell_owner_paid(settings: config.Settings, out: dict) -> None:
    """The owner's e-mail for a credited top-up, after its transaction committed: the amount credited, what was
    charged and the transaction fee within it (D92: the fee is Stripe's, never refunded), and Google's guide to
    working with third parties (THIRD_PARTY_GUIDE)."""
    way = "by card" if out["rail"] == "card" else "by bank transfer"
    next_step = None  # what comes next for a plan that waits: checked, in review, or the owner's yes, by its link
    try:
        with db.connect(settings.database_url) as conn:
            next_step = plan_review.next_step_words(conn, out["business_id"], settings.web_base_url)
    except Exception as e:  # noqa: BLE001 - the receipt goes out without its next step rather than not at all
        log.warning("pay: next step for %s not read: %s", out["business_id"], type(e).__name__)
    _mail_owner(
        settings,
        out["business_id"],
        "mail_payment.txt.j2",
        f"Admiry received your payment of {wallet.usd(out['credited_cents'])}",
        amount=wallet.usd(out["credited_cents"]),
        paid=wallet.usd(out["paid_cents"]),
        cost=wallet.usd(out["processing_cents"]),
        way=way,
        guide=THIRD_PARTY_GUIDE,
        next_step=next_step,
    )


def _mail_owner(settings: config.Settings, business_id: str, template: str, subject: str, **ctx) -> None:
    """One best-effort e-mail to the business's owner: `mail.to_owner` never raises for the mail, and nothing here
    may fail Stripe's delivery, which has already landed (crediting or failing) by the time this runs -- a retry
    would change nothing and send nothing again."""
    try:
        with db.connect(settings.database_url) as conn:
            said = mail.to_owner(conn, business_id, template, subject, **ctx)
    except Exception as e:  # noqa: BLE001 - the webhook's own effect is already committed; a mail that cannot go out is only logged
        log.warning("pay: mail for %s not sent: %s", business_id, type(e).__name__)
        return
    if said != "sent":
        log.info("pay: mail for %s not sent: %s", business_id, said)
