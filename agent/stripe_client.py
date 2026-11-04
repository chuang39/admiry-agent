"""Stripe over plain httpx (spec 2026-09-25 rev. 2 D30; rev. 1 §4, §9) — no SDK. Checkout Sessions for top-ups
(mode=payment, one rail per Checkout -- card or a US bank account by ACH, D92 -- one line item priced in cents, the
business id as client_reference_id and metadata), refunds and the list of one payment's refunds for the operator
CLI, and the webhook signature check (Stripe's
documented scheme: header `t=<unix>,v1=<hex>[,v1=…]`, v1 = HMAC-SHA256 of "<t>.<raw body>" keyed with the endpoint's
signing secret, compared in constant time, 300 s tolerance either way). Every request carries the secret key as
Basic auth; every POST is form-encoded and carries an Idempotency-Key (the list is a GET). Failures raise StripeError
whose text is Stripe's own message or a short network reason, never the key — StripeUnreachable when the request may
or may not have taken effect (no answer, or a 5xx), plain StripeError when Stripe refused it (a 4xx); owner pages
print OWNER_ERROR instead. Tests replace client() with an httpx.MockTransport client."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

import httpx

from .wallet import usd  # a plain formatter; wallet imports only `claims`, so this creates no cycle

API = "https://api.stripe.com/v1"
METHOD_TYPES = {"card": "card", "ach": "us_bank_account"}  # wallet.RAILS -> Stripe's payment method type
LINK = "link"  # Link by Stripe: on the card's Checkout only, with AGENT_CARD_LINK on, priced as the card
TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 5.0
TOLERANCE_S = 300
OWNER_ERROR = "Payments are unavailable right now; nothing was charged. Try again in a few minutes."


class StripeError(Exception):
    """Stripe refused the request or could not be reached (readable; never carries the key)."""


class StripeUnreachable(StripeError):
    """Stripe could not be reached, did not answer, or failed on its side (a 5xx): the request may or may not have
    taken effect."""


class BadSignature(Exception):
    """A webhook whose Stripe-Signature is missing, malformed, stale or wrong: nothing may be written."""


@dataclass(frozen=True)
class Checkout:
    id: str  # cs_…
    url: str  # Stripe's hosted page: a capability, never logged or audited (audit.SECRET_FIELDS "checkout_url")


@dataclass(frozen=True)
class Refund:
    id: str  # re_…
    amount_cents: int
    status: str  # succeeded | pending | requires_action | failed | canceled


def client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S))


def _request(
    http: httpx.Client,
    method: str,
    path: str,
    *,
    secret_key: str,
    form: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """One call, its answer as a JSON object. A POST sends `form` and its Idempotency-Key; a GET sends `params` and
    none. No answer or a 5xx -> StripeUnreachable (Stripe treats a 5xx as indeterminate); a 4xx or a body that is
    not a JSON object -> StripeError."""
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key is not None else None
    try:
        r = http.request(method, f"{API}{path}", data=form, params=params, auth=(secret_key, ""), headers=headers)
    except httpx.HTTPError as e:
        raise StripeUnreachable(f"could not reach Stripe ({type(e).__name__})") from None
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code >= 400 or not isinstance(body, dict):
        err = body.get("error") if isinstance(body, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        text = message if isinstance(message, str) and message else f"Stripe answered {r.status_code}"
        raise (StripeUnreachable if r.status_code >= 500 else StripeError)(text)
    return body


def create_checkout(
    http: httpx.Client,
    *,
    secret_key: str,
    business_id: str,
    quote,  # wallet.Quote (not imported: only its `usd` formatter is, to keep this module free of the DB layer)
    product_name: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None,
    idempotency_key: str,
    plan_id: str | None = None,
    link: bool = False,
) -> Checkout:
    """A one-time Checkout Session at `quote.total_cents` USD for one way of paying (D92, plan 2026-10-04-agent-m1m-1
    P2): a card (Apple Pay and Google Pay count as card), or a US bank account debited by ACH and linked through
    Stripe's instant verification only -- no microdeposits (P7). Nothing is kept on file. The credit and the rail go
    in the metadata for a person reading the Stripe dashboard; our payment_session row is the truth. The line's
    description says what the balance gets and that the price includes the processing (the presentation rule, P8).
    Stripe's Managed Payments (on by default for new accounts, 2026) refuses an explicit payment_method_types, so it
    is switched off per request rather than in each account's dashboard. With `link` (AGENT_CARD_LINK on: Stripe
    refuses a Checkout naming a type the account has not activated), the card's Checkout offers Link by Stripe beside
    the card (founder 2026-10-13), at the card's price: the quote is the card's; the bank transfer never does.
    `plan_id` (Agree and pay, L1 spec 2026-10-10 §5), when given, also goes in the metadata: the plan version this
    Checkout's credit approves."""
    form = {
        "mode": "payment",
        "managed_payments[enabled]": "false",
        "payment_method_types[0]": METHOD_TYPES[quote.rail],
        **({"payment_method_types[1]": LINK} if link and quote.rail == "card" else {}),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(quote.total_cents),
        "line_items[0][price_data][product_data][name]": product_name,
        "line_items[0][price_data][product_data][description]": (
            f"Adds {usd(quote.credit_cents)} to your Admiry balance. Includes a {usd(quote.processing_cents)} "
            "transaction fee, what Stripe charges to process the payment; Admiry adds no fee."
        ),
        "line_items[0][quantity]": "1",
        "client_reference_id": business_id,
        "metadata[business_id]": business_id,
        "metadata[credit_cents]": str(quote.credit_cents),
        "metadata[rail]": quote.rail,
        "payment_intent_data[metadata][business_id]": business_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if plan_id:
        form["metadata[plan_id]"] = plan_id
    if quote.rail == "ach":
        form["payment_method_options[us_bank_account][verification_method]"] = "instant"
        form["payment_method_options[us_bank_account][financial_connections][permissions][0]"] = "payment_method"
    if customer_email:
        form["customer_email"] = customer_email
    body = _request(
        http, "POST", "/checkout/sessions", secret_key=secret_key, form=form, idempotency_key=idempotency_key
    )
    sid, url = body.get("id"), body.get("url")
    if not (isinstance(sid, str) and sid.startswith("cs_") and isinstance(url, str) and url.startswith("https://")):
        raise StripeError("Stripe returned no payment page")
    return Checkout(id=sid, url=url)


def expire_checkout(http: httpx.Client, *, secret_key: str, session_id: str, idempotency_key: str) -> None:
    """Expire an open Checkout Session so nobody can pay it any more (spec 2026-09-29 D67: the owner leaves prepaid
    before paying). A session that is no longer open — paid meanwhile, or already expired — is refused by Stripe with
    a 4xx: StripeError, and the caller keeps the business prepaid."""
    _request(
        http,
        "POST",
        f"/checkout/sessions/{session_id}/expire",
        secret_key=secret_key,
        form={},
        idempotency_key=idempotency_key,
    )


def create_refund(
    http: httpx.Client, *, secret_key: str, payment_intent: str, amount_cents: int, idempotency_key: str
) -> Refund:
    """Refund `amount_cents` of one payment back to the card that made it."""
    body = _request(
        http,
        "POST",
        "/refunds",
        secret_key=secret_key,
        idempotency_key=idempotency_key,
        form={"payment_intent": payment_intent, "amount": str(amount_cents), "reason": "requested_by_customer"},
    )
    rid, amount, status = body.get("id"), body.get("amount"), body.get("status")
    if not (isinstance(rid, str) and rid.startswith("re_")):
        raise StripeError("Stripe returned no refund")
    return Refund(
        id=rid,
        amount_cents=amount if isinstance(amount, int) else amount_cents,
        status=status if isinstance(status, str) else "unknown",
    )


def list_refunds(http: httpx.Client, *, secret_key: str, payment_intent: str) -> list[Refund]:
    """Every refund Stripe holds for one payment, newest first, whoever made it (this CLI or the Stripe dashboard),
    in any status. More than one page (100) is refused rather than read in part; so is a list that cannot be read."""
    body = _request(
        http, "GET", "/refunds", secret_key=secret_key, params={"payment_intent": payment_intent, "limit": "100"}
    )
    data = body.get("data")
    if body.get("has_more") is True:
        raise StripeError("more than 100 refunds on one payment; reconcile it in the Stripe dashboard")
    if not isinstance(data, list) or body.get("has_more") is not False:
        raise StripeError("Stripe returned a refund list that could not be read")
    out = []
    for item in data:
        rid = item.get("id") if isinstance(item, dict) else None
        if not (isinstance(rid, str) and rid.startswith("re_")):
            continue
        amount, status = item.get("amount"), item.get("status")
        if type(amount) is not int or amount <= 0 or not isinstance(status, str):
            raise StripeError("Stripe returned a refund list that could not be read")
        out.append(Refund(id=rid, amount_cents=amount, status=status))
    return out


def verify_signature(
    payload: bytes, header: str | None, secret: str, *, tolerance: int = TOLERANCE_S, now: float | None = None
) -> None:
    """Raise BadSignature unless `header` carries a timestamp within `tolerance` seconds of now and a v1
    signature equal to HMAC-SHA256(secret, "<t>." + payload)."""
    if not header or not secret:
        raise BadSignature("missing signature or secret")
    t, signatures = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            t = value
        elif key == "v1":
            signatures.append(value)
    if not t or not (t.isascii() and t.isdigit()) or len(t) > 12 or not signatures:
        raise BadSignature("malformed signature header")
    if abs((time.time() if now is None else now) - int(t)) > tolerance:
        raise BadSignature("timestamp outside the tolerance")
    expected = hmac.new(secret.encode(), t.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected.encode(), s.encode()) for s in signatures):
        raise BadSignature("signature mismatch")
