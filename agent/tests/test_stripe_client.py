"""Stripe over a mock transport (spec 2026-09-25 rev. 1 §9, kept by rev. 2 §9): what is posted, what is accepted
back, and Stripe's documented webhook signature scheme. No network."""

import base64
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qs

import httpx
import pytest

from agent import stripe_client, wallet

KEY = "sk_test_x"
SECRET = "whsec_test"
PAYLOAD = b'{"id": "evt_1", "type": "checkout.session.completed"}'
SESSION = httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"})
CHECKOUT = {
    "secret_key": KEY,
    "business_id": "biz_1",
    "quote": wallet.Quote("card", 50000, 1680, 51680),
    "product_name": "Advertising balance for Bright Smile Dental",
    "success_url": "http://testserver/pay/biz_1?done=1",
    "cancel_url": "http://testserver/pay/biz_1?cancelled=1",
    "customer_email": "owner@example.com",
    "idempotency_key": "pay-abc",
}


def mock(reply, sent: list) -> httpx.Client:
    def handler(request):
        sent.append(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx.Client(transport=httpx.MockTransport(handler))


def form(request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def header(payload=PAYLOAD, secret=SECRET, t=None, extra=""):
    t = int(time.time()) if t is None else t
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}{extra}"


def test_checkout_posts_the_documented_form_with_basic_auth_and_an_idempotency_key():
    sent = []
    http = mock(
        httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}), sent
    )
    out = stripe_client.create_checkout(http, **CHECKOUT)
    assert out == stripe_client.Checkout(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")
    req = sent[0]
    assert req.method == "POST" and str(req.url) == "https://api.stripe.com/v1/checkout/sessions"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"sk_test_x:").decode()
    assert req.headers["idempotency-key"] == "pay-abc"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert form(req) == {
        "mode": "payment",
        "managed_payments[enabled]": "false",
        "payment_method_types[0]": "card",  # the card alone: Link only with AGENT_CARD_LINK on (test below)
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": "51680",
        "line_items[0][price_data][product_data][name]": "Advertising balance for Bright Smile Dental",
        "line_items[0][price_data][product_data][description]": (
            "Adds $500.00 to your Admiry balance. Includes a $16.80 transaction fee, what Stripe charges to "
            "process the payment; Admiry adds no fee."
        ),
        "line_items[0][quantity]": "1",
        "client_reference_id": "biz_1",
        "metadata[business_id]": "biz_1",
        "metadata[credit_cents]": "50000",
        "metadata[rail]": "card",
        "payment_intent_data[metadata][business_id]": "biz_1",
        "success_url": "http://testserver/pay/biz_1?done=1",
        "cancel_url": "http://testserver/pay/biz_1?cancelled=1",
        "customer_email": "owner@example.com",
    }


def test_a_card_checkout_charges_the_price_and_names_the_credit():
    sent = []
    q = wallet.Quote("card", 100000, 3330, 103330)
    stripe_client.create_checkout(mock(SESSION, sent), **{**CHECKOUT, "quote": q})
    f = form(sent[0])
    assert f["line_items[0][price_data][unit_amount]"] == "103330"
    assert [k for k in f if k.startswith("payment_method_types")] == ["payment_method_types[0]"]
    assert f["payment_method_types[0]"] == "card"
    assert (f["metadata[credit_cents]"], f["metadata[rail]"]) == ("100000", "card")
    assert f["line_items[0][price_data][product_data][description]"] == (
        "Adds $1,000.00 to your Admiry balance. Includes a $33.30 transaction fee, what Stripe charges to process the "
        "payment; Admiry adds no fee."
    )
    assert not any(k.startswith("payment_method_options") for k in f)


@pytest.mark.parametrize("link", [False, True])
def test_link_by_stripe_is_beside_the_card_only_when_switched_on(link):
    """Fix round 2b, item 1: Stripe refuses a Checkout naming a payment method type the account has not activated, so
    Link by Stripe goes beside the card only with AGENT_CARD_LINK on (`link`), at the card's price; off, the card
    alone. The bank transfer is us_bank_account alone either way."""
    sent = []
    stripe_client.create_checkout(mock(SESSION, sent), **CHECKOUT, link=link)
    types = {k: v for k, v in form(sent[0]).items() if k.startswith("payment_method_types")}
    assert types == (
        {"payment_method_types[0]": "card", "payment_method_types[1]": "link"}
        if link
        else {"payment_method_types[0]": "card"}
    )
    q = wallet.Quote("ach", 100000, 500, 100500)
    stripe_client.create_checkout(mock(SESSION, sent), **{**CHECKOUT, "quote": q}, link=link)
    types = {k: v for k, v in form(sent[1]).items() if k.startswith("payment_method_types")}
    assert types == {"payment_method_types[0]": "us_bank_account"}


def test_a_bank_checkout_is_a_us_bank_account_verified_instantly():
    sent = []
    q = wallet.Quote("ach", 100000, 500, 100500)
    stripe_client.create_checkout(mock(SESSION, sent), **{**CHECKOUT, "quote": q})
    f = form(sent[0])
    assert f["payment_method_types[0]"] == "us_bank_account"
    assert [k for k in f if k.startswith("payment_method_types")] == ["payment_method_types[0]"]  # no Link here
    assert f["line_items[0][price_data][unit_amount]"] == "100500"
    assert f["payment_method_options[us_bank_account][verification_method]"] == "instant"
    assert f["payment_method_options[us_bank_account][financial_connections][permissions][0]"] == "payment_method"
    assert f["managed_payments[enabled]"] == "false"


@pytest.mark.parametrize(
    "reply, message",
    [
        (
            httpx.Response(
                402,
                json={
                    "error": {"message": "Your account cannot accept payments yet.", "type": "invalid_request_error"}
                },
            ),
            "Your account cannot accept payments yet.",
        ),
        (httpx.Response(500, text="<html>oops</html>"), "Stripe answered 500"),
        (httpx.ConnectError("boom"), "could not reach Stripe (ConnectError)"),
        (httpx.Response(200, json={"id": "cs_test_1"}), "Stripe returned no payment page"),
    ],
)
def test_checkout_failures_are_readable(reply, message):
    with pytest.raises(stripe_client.StripeError, match=re.escape(message)) as e:
        stripe_client.create_checkout(mock(reply, []), **CHECKOUT)
    assert KEY not in str(e.value)


def test_refund_posts_the_payment_intent_and_amount():
    sent = []
    http = mock(httpx.Response(200, json={"id": "re_1", "amount": 20000, "status": "succeeded"}), sent)
    out = stripe_client.create_refund(
        http, secret_key=KEY, payment_intent="pi_2", amount_cents=20000, idempotency_key="refund-cs_2-0"
    )
    assert out == stripe_client.Refund(id="re_1", amount_cents=20000, status="succeeded")
    assert str(sent[0].url) == "https://api.stripe.com/v1/refunds"
    assert sent[0].headers["idempotency-key"] == "refund-cs_2-0"
    assert form(sent[0]) == {"payment_intent": "pi_2", "amount": "20000", "reason": "requested_by_customer"}


def test_a_refund_reply_without_an_id_is_an_error():
    with pytest.raises(stripe_client.StripeError, match="no refund"):
        stripe_client.create_refund(
            mock(httpx.Response(200, json={"status": "succeeded"}), []),
            secret_key=KEY,
            payment_intent="pi_2",
            amount_cents=100,
            idempotency_key="k",
        )


@pytest.mark.parametrize(
    "reply, unreachable, message",
    [
        (
            httpx.Response(500, json={"error": {"message": "An unknown error occurred", "type": "api_error"}}),
            True,
            "An unknown error occurred",
        ),
        (httpx.Response(503, text="<html>down</html>"), True, "Stripe answered 503"),
        (httpx.ReadTimeout("slow"), True, "could not reach Stripe (ReadTimeout)"),
        (
            httpx.Response(400, json={"error": {"message": "Refund amount is greater than unrefunded amount."}}),
            False,
            "Refund amount is greater than unrefunded amount.",
        ),
        (
            httpx.Response(402, json={"error": {"message": "This charge has already been refunded."}}),
            False,
            "This charge has already been refunded.",
        ),
    ],
)
def test_only_an_answer_that_may_have_taken_effect_is_unreachable(reply, unreachable, message):
    """No answer or a 5xx may or may not have taken effect (Stripe treats a 5xx as indeterminate); a 4xx is a
    refusal. Both are StripeError."""
    with pytest.raises(stripe_client.StripeError, match=re.escape(message)) as e:
        stripe_client.create_refund(
            mock(reply, []), secret_key=KEY, payment_intent="pi_2", amount_cents=100, idempotency_key="k"
        )
    assert isinstance(e.value, stripe_client.StripeUnreachable) is unreachable and KEY not in str(e.value)


def test_list_refunds_gets_one_payments_refunds_with_no_idempotency_key():
    sent = []
    http = mock(
        httpx.Response(
            200,
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {"id": "re_2", "amount": 5000, "status": "pending"},
                    {"id": "pyr_1", "amount": 100, "status": "succeeded"},
                    {"id": "re_1", "amount": 20000, "status": "succeeded"},
                    "not a refund",
                ],
            },
        ),
        sent,
    )
    out = stripe_client.list_refunds(http, secret_key=KEY, payment_intent="pi_2")
    assert out == [
        stripe_client.Refund(id="re_2", amount_cents=5000, status="pending"),
        stripe_client.Refund(id="re_1", amount_cents=20000, status="succeeded"),
    ]  # only re_ ids
    req = sent[0]
    assert req.method == "GET" and str(req.url) == "https://api.stripe.com/v1/refunds?payment_intent=pi_2&limit=100"
    assert dict(req.url.params) == {"payment_intent": "pi_2", "limit": "100"}
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"sk_test_x:").decode()
    assert "idempotency-key" not in req.headers and req.content == b""


@pytest.mark.parametrize(
    "reply, message",
    [
        (
            httpx.Response(200, json={"object": "list", "has_more": True, "data": []}),
            "more than 100 refunds on one payment; reconcile it in the Stripe dashboard",
        ),
        (httpx.Response(200, json={"object": "list", "has_more": False}), "could not be read"),
        (httpx.Response(200, json={"object": "list", "data": []}), "could not be read"),
        (
            httpx.Response(
                200,
                json={
                    "object": "list",
                    "has_more": False,
                    "data": [{"id": "re_1", "amount": "5000", "status": "succeeded"}],
                },
            ),
            "could not be read",
        ),
        (httpx.Response(503, text="<html>down</html>"), "Stripe answered 503"),
    ],
)
def test_a_refund_list_that_cannot_be_trusted_whole_is_an_error(reply, message):
    with pytest.raises(stripe_client.StripeError, match=re.escape(message)):
        stripe_client.list_refunds(mock(reply, []), secret_key=KEY, payment_intent="pi_2")


def test_signature_accepts_stripes_documented_scheme():
    stripe_client.verify_signature(PAYLOAD, header(), SECRET)
    stripe_client.verify_signature(PAYLOAD, header(extra=",v0=deadbeef"), SECRET)  # other schemes are ignored
    now = int(time.time())
    rolled = header(t=now).replace(",v1=", f",v1={'0' * 64},v1=")  # a rolled secret: any v1 may match
    stripe_client.verify_signature(PAYLOAD, rolled, SECRET)


@pytest.mark.parametrize(
    "payload, sig, secret",
    [
        (PAYLOAD, lambda: header(secret="whsec_other"), SECRET),
        (PAYLOAD + b" ", lambda: header(), SECRET),  # the body changed after signing
        (PAYLOAD, lambda: header(t=int(time.time()) - 400), SECRET),  # stale
        (PAYLOAD, lambda: header(t=int(time.time()) + 400), SECRET),  # from the future
        (PAYLOAD, lambda: None, SECRET),  # no header at all
        (PAYLOAD, lambda: "v1=" + "0" * 64, SECRET),  # no timestamp
        (PAYLOAD, lambda: f"t={int(time.time())}", SECRET),  # no v1
        (PAYLOAD, lambda: header().replace("t=", "t=x"), SECRET),  # timestamp not a number
        (PAYLOAD, lambda: header(secret=""), ""),  # no secret configured
        (PAYLOAD, lambda: f"t={'9' * 400},v1={'0' * 64}", SECRET),  # an absurd timestamp
        (PAYLOAD, lambda: f"t={int(time.time())},v1=é", SECRET),  # not hex, not even ASCII
    ],
)
def test_signature_refusals(payload, sig, secret):
    with pytest.raises(stripe_client.BadSignature):
        stripe_client.verify_signature(payload, sig(), secret)


def test_the_client_gives_up_quickly():
    c = stripe_client.client()
    assert c.timeout.read == stripe_client.TIMEOUT_S == 15.0 and c.timeout.connect == 5.0


def test_expire_checkout_posts_to_the_session_with_an_idempotency_key():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "cs_test_1", "status": "expired"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    stripe_client.expire_checkout(http, secret_key="sk_test_x", session_id="cs_test_1", idempotency_key="expire-1")
    assert seen[0].method == "POST" and seen[0].url.path == "/v1/checkout/sessions/cs_test_1/expire"
    assert seen[0].headers["idempotency-key"] == "expire-1"


def test_expire_checkout_on_a_session_that_is_no_longer_open_is_a_stripe_error():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "Only Checkout Sessions with a status in [open] can be expired."}}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(stripe_client.StripeError, match="can be expired"):
        stripe_client.expire_checkout(http, secret_key="sk_test_x", session_id="cs_test_1", idempotency_key="k")
