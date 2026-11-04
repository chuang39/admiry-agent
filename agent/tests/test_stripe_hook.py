"""POST /hooks/stripe (spec 2026-09-25 rev. 2 §5; rev. 1 §4 "Webhook", §9): signed events only; a paid Checkout
credits the balance once and verifies a claimed prepaid business; every other event is acknowledged and changes
nothing it should not."""

import hashlib
import hmac
import json
import logging
import time
from datetime import date

import pytest

from agent import business, config, pay, setup, wallet
from agent.tests.conftest import WHSEC
from agent.tests.test_managed_mode import OWNER, managed_owner


def event(
    kind="checkout.session.completed", session_id="cs_test_1", amount=51680, payment_status="paid", event_id="evt_1"
) -> bytes:
    obj = {
        "id": session_id,
        "object": "checkout.session",
        "payment_status": payment_status,
        "amount_total": amount,
        "client_reference_id": "biz_1",
        "metadata": {"business_id": "biz_1"},
        "payment_intent": "pi_1",
    }
    return json.dumps({"id": event_id, "type": kind, "data": {"object": obj}}).encode()


def deliver(client, payload: bytes, *, secret=WHSEC, t=None, body=None):
    """POST the event signed with Stripe's scheme (t=<unix>,v1=hex HMAC-SHA256 of "<t>.<payload>"); `body` sends
    other bytes than the ones signed."""
    t = int(time.time()) if t is None else t
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return client.post(
        "/hooks/stripe",
        content=body if body is not None else payload,
        headers={"Stripe-Signature": f"t={t},v1={v1}", "Content-Type": "application/json"},
    )


def started(client, conn, google, amount="500"):
    """biz_1 claimed by the signed-in owner, prepaid with both texts accepted, with one Checkout started (cs_test_1)."""
    managed_owner(client, conn, google)
    assert setup.pay(config.load(), OWNER, "biz_1", amount).startswith("https://checkout.stripe.com/")


def started_by_bank(client, conn, google, monkeypatch, amount="1000"):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    managed_owner(client, conn, google)
    assert setup.pay(config.load(), OWNER, "biz_1", amount, "ach").startswith("https://checkout.stripe.com/")


def status(conn, sid="cs_test_1") -> str:
    return conn.execute("SELECT status FROM payment_session WHERE id = %s", (sid,)).fetchone()["status"]


def count(conn, sql) -> int:
    return conn.execute(sql).fetchone()["n"]


def test_a_paid_checkout_credits_the_balance_and_verifies_the_business(client, conn, stripe, google):
    started(client, conn, google)
    r = deliver(client, event())
    assert r.status_code == 200 and r.json() == {"received": True, "credited": True}
    assert wallet.balance_cents(conn, "biz_1") == 50000
    row = business.row(conn, "biz_1")
    assert (row["claim_state"], row["verified_via"]) == ("verified", "payment") and row["verified_at"] is not None
    assert row["claim_expires_at"] is None
    s = conn.execute("SELECT status, payment_intent, paid_at FROM payment_session WHERE id = 'cs_test_1'").fetchone()
    assert s["status"] == "paid" and s["payment_intent"] == "pi_1" and s["paid_at"] is not None
    a = conn.execute(
        "SELECT actor, actor_class, api_key_id, approval_kind, result, business_id FROM agent_action "
        "WHERE tool = 'web.topup'"
    ).fetchone()
    assert (a["actor"], a["actor_class"], a["api_key_id"], a["approval_kind"], a["business_id"]) == (
        "stripe",
        "system",
        None,
        None,
        "biz_1",
    )
    assert a["result"] == {"amount_usd": 500.0, "verified_now": True}
    html = client.get("/connect").text  # Settings
    assert "Balance: <strong>$500.00</strong>" in html and "Card payment" in html and "$500.00</td>" in html


def test_a_replayed_delivery_adds_nothing(client, conn, stripe, google):
    started(client, conn, google)
    assert deliver(client, event()).json()["credited"] is True
    r = deliver(client, event(event_id="evt_2"))  # Stripe's retry, or the dashboard's "Resend"
    assert r.status_code == 200 and r.json() == {"received": True, "credited": False}
    assert wallet.balance_cents(conn, "biz_1") == 50000
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 1
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 1


def test_a_credited_payment_mails_the_owner_googles_guide_to_working_with_third_parties(
    client, conn, stripe, google, resend
):
    """Google's third-party policy (founder 2026-10-03): most of Admiry's customers spend under $1,000 a month, so at
    every sale or renewal -- each credited top-up -- the owner is sent Google's "Working with third parties" guide; a
    replayed delivery credits nothing and sends nothing more."""
    started(client, conn, google)
    assert deliver(client, event()).json()["credited"] is True
    (mail,) = [m for m in resend.sent if m["to"] == ["owner@example.com"]]
    assert mail["subject"] == "Admiry received your payment of $500.00"
    assert pay.THIRD_PARTY_GUIDE in mail["text"] and "$500.00" in mail["text"]
    assert pay.THIRD_PARTY_GUIDE == "https://support.google.com/adspolicy/answer/9457109"
    deliver(client, event(event_id="evt_2"))
    assert len([m for m in resend.sent if m["to"] == ["owner@example.com"]]) == 1


@pytest.mark.parametrize("how", ["wrong secret", "stale", "unsigned", "tampered"])
def test_a_bad_signature_is_400_and_writes_nothing(client, conn, stripe, google, how):
    started(client, conn, google)
    payload = event()
    if how == "wrong secret":
        r = deliver(client, payload, secret="whsec_other")
    elif how == "stale":
        r = deliver(client, payload, t=int(time.time()) - 400)
    elif how == "unsigned":
        r = client.post("/hooks/stripe", content=payload, headers={"Content-Type": "application/json"})
    else:
        r = deliver(client, payload, body=event(amount=5_000_000))
    assert r.status_code == 400
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"
    assert conn.execute("SELECT status FROM payment_session").fetchone()["status"] == "open"
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 0


def test_an_unpaid_completion_is_a_bank_debit_on_its_way(client, conn, stripe, google):
    """A `checkout.session.completed` with `payment_status` still "unpaid" is Stripe's own signal of an async
    payment method (plan 2026-10-04-agent-m1m-1 P3): `processing`, not ignored -- superseding the pre-P3 behavior
    this test used to assert (A1b-3 Task 5), since Stripe never completes a card Checkout unpaid."""
    started(client, conn, google)
    r = deliver(client, event(payment_status="unpaid"))
    assert r.status_code == 200 and r.json() == {"received": True, "processing": True}
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"
    assert status(conn) == "processing"


def test_async_payment_success_credits_like_a_completion(client, conn, stripe, google):
    started(client, conn, google)
    assert deliver(client, event(kind="checkout.session.async_payment_succeeded")).json()["credited"] is True
    assert wallet.balance_cents(conn, "biz_1") == 50000


def test_a_verified_business_stays_as_it_was(client, conn, stripe, google):
    started(client, conn, google)
    conn.execute(
        "UPDATE business SET claim_state = 'verified', verified_via = 'ops', "
        "verified_at = now() - interval '3 days' WHERE id = 'biz_1'"
    )
    before = business.row(conn, "biz_1")["verified_at"]
    deliver(client, event())
    row = business.row(conn, "biz_1")
    assert (row["verified_via"], row["verified_at"]) == ("ops", before) and wallet.balance_cents(conn, "biz_1") == 50000
    assert (
        conn.execute("SELECT result FROM agent_action WHERE tool = 'web.topup'").fetchone()["result"]["verified_now"]
        is False
    )


def test_an_expired_session_only_changes_its_status(client, conn, stripe, google):
    started(client, conn, google)
    r = deliver(client, event(kind="checkout.session.expired", payment_status="unpaid"))
    assert r.status_code == 200 and r.json() == {"received": True}
    assert conn.execute("SELECT status FROM payment_session WHERE id = 'cs_test_1'").fetchone()["status"] == "expired"
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"
    assert "Card payment" not in client.get("/connect").text  # Settings lists money in, not unpaid Checkouts


def test_other_events_are_acknowledged_and_ignored(client, conn, stripe, google):
    started(client, conn, google)
    r = deliver(client, event(kind="payment_intent.succeeded"))
    assert r.status_code == 200 and r.json() == {"received": True, "ignored": "payment_intent.succeeded"}
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0


def test_a_session_this_server_did_not_create_is_acknowledged_without_a_row(client, conn, stripe, google):
    started(client, conn, google)
    r = deliver(client, event(session_id="cs_from_another_server"))
    # Task 8b: said as ignored, with one log line (test_stripe_sandbox: every writing event, either secret)
    assert r.status_code == 200 and r.json() == {"received": True, "ignored": "unknown session"}
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 0


def test_a_payment_that_lands_after_the_claim_was_released_is_credited_and_verifies_its_owner_again(
    client, conn, stripe, google
):
    started(client, conn, google)
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL WHERE id = 'biz_1'")
    assert deliver(client, event()).json()["credited"] is True
    row = business.row(conn, "biz_1")
    assert wallet.balance_cents(conn, "biz_1") == 50000 and row["slug"] is not None
    assert (row["claim_state"], row["verified_via"], row["owner_email"]) == ("verified", "payment", "owner@example.com")


def test_a_payment_for_a_business_no_longer_prepaid_is_credited_but_does_not_verify(client, conn, stripe, google):
    started(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'own_account' WHERE id = 'biz_1'")  # an operator switched it
    assert deliver(client, event()).json()["credited"] is True
    assert wallet.balance_cents(conn, "biz_1") == 50000 and business.row(conn, "biz_1")["claim_state"] == "claimed"
    assert conn.execute("SELECT result FROM agent_action WHERE tool = 'web.topup'").fetchone()["result"] == {
        "amount_usd": 500.0,
        "verified_now": False,
    }


def test_a_top_up_lifts_the_no_funds_pause_only_once_the_balance_is_positive(client, conn, stripe, google):
    started(client, conn, google)  # cs_test_1: $500
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=70000)
    conn.execute("UPDATE business SET paused_reason = 'no_funds' WHERE id = 'biz_1'")
    deliver(client, event())
    assert wallet.balance_cents(conn, "biz_1") == -20000 and business.row(conn, "biz_1")["paused_reason"] == "no_funds"
    assert setup.pay(config.load(), OWNER, "biz_1", "500").startswith("https://checkout.stripe.com/")  # cs_test_2
    deliver(client, event(session_id="cs_test_2", event_id="evt_2"))
    assert wallet.balance_cents(conn, "biz_1") == 30000 and business.row(conn, "biz_1")["paused_reason"] is None


def test_without_the_webhook_secret_the_hook_is_503_and_writes_nothing(client, conn, stripe, google, monkeypatch):
    started(client, conn, google)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    assert deliver(client, event()).status_code == 503
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0


def test_crafted_signature_headers_are_400_not_a_crash(client, conn, stripe, google):
    started(client, conn, google)
    payload = event()
    r1 = client.post(
        "/hooks/stripe",
        content=payload,
        headers={
            "Stripe-Signature": f"t={int(time.time())},v1=".encode() + b"\xe9",
            "Content-Type": "application/json",
        },
    )
    r2 = client.post(
        "/hooks/stripe",
        content=payload,
        headers={"Stripe-Signature": f"t={'9' * 400},v1={'0' * 64}", "Content-Type": "application/json"},
    )
    assert r1.status_code == 400 and r2.status_code == 400
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 0


def test_a_card_payment_credits_the_chosen_amount_and_mails_what_was_paid(client, conn, stripe, google, resend):
    started(client, conn, google)
    assert deliver(client, event()).json() == {"received": True, "credited": True}
    assert wallet.balance_cents(conn, "biz_1") == 50000
    text = resend.sent[-1]["text"]  # resend.sent: Resend's parsed JSON bodies ("to" is a list)
    assert "$500.00 is now on your Admiry balance" in text
    assert "You paid $516.80 by card, including a $16.80 transaction fee" in text


def test_a_total_that_is_not_our_price_credits_nothing_and_tells_the_operator(client, conn, stripe, google, resend):
    started(client, conn, google)  # the resend fixture sets AGENT_OPS_EMAIL=ops@example.com
    assert deliver(client, event(amount=50000)).json() == {"received": True, "credited": False}
    assert wallet.balance_cents(conn, "biz_1") == 0
    ops = [(m, k) for m, k in zip(resend.sent, resend.keys) if m["to"] == ["ops@example.com"]]
    assert len(ops) == 1 and "cs_test_1" in ops[0][0]["text"] and "$516.80" in ops[0][0]["text"]
    assert ops[0][1] == "stripe-mismatch-cs_test_1"  # a redelivery reuses the key: one mail at Resend
    # R4: a mismatched session never reaches wallet.refund_plan (no 'paid' status, no payment_intent stored), so
    # `agent.ops refund` would pull money off the business's other, correctly credited payments -- the mail must
    # send the operator to the Stripe dashboard instead.
    assert "Stripe dashboard" in ops[0][0]["text"] and "agent.ops refund" not in ops[0][0]["text"]


def test_a_mismatch_with_no_ops_email_still_logs_the_session_and_the_business(client, conn, stripe, google, caplog):
    """Finding 3: with AGENT_OPS_EMAIL unset (no `resend` fixture here) a mismatch used to leave no record naming
    the session or the business -- only send_plain's own generic "notice to  not sent" line, with nobody's address
    to show for it. `_tell_operator_mismatch` must log the session, the business and both amounts itself, before
    it even tries to mail anyone."""
    started(client, conn, google)
    with caplog.at_level(logging.WARNING, logger="agent.pay"):
        r = deliver(client, event(amount=50000))
    assert r.status_code == 200 and r.json() == {"received": True, "credited": False}
    assert "cs_test_1" in caplog.text and "biz_1" in caplog.text
    assert "$516.80" in caplog.text and "$500.00" in caplog.text


def test_a_mismatch_mail_that_raises_still_answers_200(client, conn, stripe, google, resend, monkeypatch):
    """Finding 3: an exception raised while telling the operator must never 500 the webhook -- Stripe's delivery
    already landed (nothing was credited) by the time this runs, same as `_mail_owner`'s own best-effort send."""
    started(client, conn, google)
    monkeypatch.setattr(pay.mail_out, "send_plain", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = deliver(client, event(amount=50000))
    assert r.status_code == 200 and r.json() == {"received": True, "credited": False}
    assert wallet.balance_cents(conn, "biz_1") == 0


def test_a_bank_transfer_is_on_its_way_then_credited(client, conn, stripe, google, monkeypatch, resend):
    started_by_bank(client, conn, google, monkeypatch)
    r = deliver(client, event(amount=100500, payment_status="unpaid"))
    assert r.json() == {"received": True, "processing": True} and status(conn) == "processing"
    assert wallet.balance_cents(conn, "biz_1") == 0
    ok = event(kind="checkout.session.async_payment_succeeded", amount=100500, event_id="evt_2")
    assert deliver(client, ok).json() == {"received": True, "credited": True}
    assert status(conn) == "paid" and wallet.balance_cents(conn, "biz_1") == 100000
    assert "You paid $1,005.00 by bank transfer" in resend.sent[-1]["text"]


def test_a_bank_transfer_that_failed_credits_nothing_and_tells_the_owner(
    client, conn, stripe, google, monkeypatch, resend
):
    started_by_bank(client, conn, google, monkeypatch)
    deliver(client, event(amount=100500, payment_status="unpaid"))
    bad = event(kind="checkout.session.async_payment_failed", amount=100500, payment_status="unpaid", event_id="evt_3")
    assert deliver(client, bad).json() == {"received": True, "failed": True}
    assert status(conn) == "failed" and wallet.balance_cents(conn, "biz_1") == 0
    mail = resend.sent[-1]
    assert mail["subject"] == "Your bank transfer to Admiry did not go through"
    assert "nothing was added to your balance" in mail["text"]
    assert deliver(client, bad).json() == {"received": True, "failed": True}  # redelivered: still one mail
    assert sum(m["subject"] == mail["subject"] for m in resend.sent) == 1


@pytest.mark.parametrize("final", ["checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed"])
def test_bank_events_in_any_order(client, conn, stripe, google, monkeypatch, final):
    """Review Focus 2: Stripe may deliver the outcome before `completed`; the late `completed` changes nothing."""
    started_by_bank(client, conn, google, monkeypatch)
    deliver(client, event(kind=final, amount=100500, event_id="evt_a"))
    deliver(client, event(amount=100500, payment_status="unpaid", event_id="evt_b"))
    assert status(conn) == ("paid" if final.endswith("succeeded") else "failed")
    assert wallet.balance_cents(conn, "biz_1") == (100000 if final.endswith("succeeded") else 0)
