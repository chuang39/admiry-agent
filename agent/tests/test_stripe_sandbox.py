"""Stripe sandbox for allowlisted owners (Task 8b, founder ruling 2026-11-04; R17, R18): an owner on
AGENT_LAUNCH_ALLOWLIST pays with the sandbox keys (STRIPE_SANDBOX_SECRET_KEY / STRIPE_SANDBOX_WEBHOOK_SECRET) when
both are set; everyone else with the regular keys, as before. /hooks/stripe verifies with the regular secret, else
with the sandbox one, and applies a sandbox-only event only for an allowlisted owner's business. Stripe is the
`stripe` fixture's mock; no network."""

import base64
import json
import logging

import pytest

from agent import config, ops, setup, stripe_mode, wallet
from agent.tests.conftest import WHSEC, call_tool
from agent.tests.test_managed_mode import OWNER, managed_owner
from agent.tests.test_stripe_hook import count, deliver, event, started, status
from agent.tests.test_tools_wallet import prepaid_key
from agent.tests.test_wallet import fund, managed

SANDBOX_KEY = "sk_test_sandbox_founder"
SANDBOX_WHSEC = "whsec_sandbox_founder"


@pytest.fixture
def sandbox(monkeypatch):
    """The sandbox keys set and owner@example.com (the tests' owner) on the allowlist."""
    monkeypatch.setenv("STRIPE_SANDBOX_SECRET_KEY", SANDBOX_KEY)
    monkeypatch.setenv("STRIPE_SANDBOX_WEBHOOK_SECRET", SANDBOX_WHSEC)
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com, Owner@Example.com")


def no_regular_keys(monkeypatch):
    """prod today: the regular keys unset (compose passes "")."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")


def key_of(request) -> str:
    """The secret key an HTTP call to Stripe carried (Basic auth, the key as the user name)."""
    scheme, _, token = request.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    return base64.b64decode(token).decode().split(":", 1)[0]


def pay_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "agent.pay"]


def sandbox_event(livemode=False, **kw) -> bytes:
    """test_stripe_hook's event with the `livemode` a sandbox (test-mode) event carries; None leaves it out."""
    e = json.loads(event(**kw))
    if livemode is not None:
        e["livemode"] = livemode
    return json.dumps(e).encode()


def test_allowlisted_owner_gets_sandbox_keys(monkeypatch, sandbox):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_regular")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_regular")
    s = config.load()
    keys = stripe_mode.for_owner(s, " OWNER@example.com ")
    assert keys == stripe_mode.StripeKeys(secret_key=SANDBOX_KEY, webhook_secret=SANDBOX_WHSEC, sandbox=True)
    assert keys.ready
    for text in (repr(s), repr(keys)):  # never printed
        assert SANDBOX_KEY not in text and SANDBOX_WHSEC not in text and "sk_live_regular" not in text


def test_other_owner_gets_regular_keys(monkeypatch, sandbox):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_regular")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_regular")
    s = config.load()
    regular = stripe_mode.StripeKeys(secret_key="sk_live_regular", webhook_secret="whsec_regular", sandbox=False)
    for owner in ("someone@example.com", None, "", "owner@example.com.evil"):
        assert stripe_mode.for_owner(s, owner) == regular
    no_regular_keys(monkeypatch)
    keys = stripe_mode.for_owner(config.load(), "someone@example.com")
    assert keys == stripe_mode.StripeKeys(secret_key=None, webhook_secret=None, sandbox=False) and not keys.ready


def test_no_sandbox_keys_means_regular_for_everyone(monkeypatch, sandbox):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_regular")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_regular")
    regular = stripe_mode.StripeKeys(secret_key="sk_live_regular", webhook_secret="whsec_regular", sandbox=False)
    for unset in ("STRIPE_SANDBOX_SECRET_KEY", "STRIPE_SANDBOX_WEBHOOK_SECRET"):  # one alone is no sandbox
        with monkeypatch.context() as m:
            m.setenv(unset, "")
            assert stripe_mode.for_owner(config.load(), "owner@example.com") == regular
    monkeypatch.delenv("AGENT_LAUNCH_ALLOWLIST")  # no list: nobody is a sandbox user
    assert stripe_mode.for_owner(config.load(), "owner@example.com") == regular


def test_checkout_for_allowlisted_owner_uses_sandbox_key(client, conn, stripe, google, sandbox):
    managed_owner(client, conn, google)
    assert stripe_mode.for_business(conn, config.load(), "biz_1").sandbox
    assert setup.pay(config.load(), OWNER, "biz_1", "500").startswith("https://checkout.stripe.com/")
    (sent,) = stripe.sent
    assert sent.url.path == "/v1/checkout/sessions" and key_of(sent) == SANDBOX_KEY
    (args,) = [
        r["args_redacted"] for r in conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'web.pay_start'")
    ]
    assert args["sandbox"] == "true"  # the payment's audit row says it was a sandbox payment (args are stored as text)


def test_checkout_for_other_owner_uses_regular_key(client, conn, stripe, google, sandbox, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    managed_owner(client, conn, google)
    setup.pay(config.load(), OWNER, "biz_1", "500")
    (sent,) = stripe.sent
    assert key_of(sent) == "sk_test_x"  # the stripe fixture's regular key
    (args,) = [
        r["args_redacted"] for r in conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'web.pay_start'")
    ]
    assert "sandbox" not in args  # the regular audit row is as it was


def test_webhook_sandbox_signature_credits_allowlisted_owner(client, conn, stripe, google, sandbox, monkeypatch):
    no_regular_keys(monkeypatch)  # prod today: only the sandbox keys
    started(client, conn, google)
    r = deliver(client, sandbox_event(), secret=SANDBOX_WHSEC)
    assert r.status_code == 200 and r.json() == {"received": True, "credited": True}
    assert wallet.balance_cents(conn, "biz_1") == 50000 and status(conn) == "paid"
    (a,) = conn.execute(
        "SELECT actor, args_redacted AS args, result FROM agent_action WHERE tool = 'web.topup'"
    ).fetchall()
    assert a["actor"] == "stripe" and (a["args"]["sandbox"], a["args"]["livemode"]) == ("true", "false")
    assert a["result"] == {"amount_usd": 500.0, "verified_now": True}  # recorded like any other payment


@pytest.mark.parametrize(
    "kind,payment_status",
    [
        ("checkout.session.completed", "paid"),
        ("checkout.session.completed", "unpaid"),
        ("checkout.session.async_payment_succeeded", "paid"),
        ("checkout.session.async_payment_failed", "unpaid"),
        ("checkout.session.expired", "unpaid"),
    ],
)
def test_webhook_sandbox_signature_ignored_for_other_owner(
    client, conn, stripe, google, sandbox, monkeypatch, caplog, kind, payment_status
):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    started(client, conn, google)  # owner@example.com, not on the list: a regular Checkout
    caplog.set_level(logging.INFO, logger="agent.pay")
    r = deliver(client, sandbox_event(kind=kind, payment_status=payment_status), secret=SANDBOX_WHSEC)
    assert r.status_code == 200 and r.json() == {"received": True, "ignored": "sandbox"}
    assert status(conn) == "open" and wallet.balance_cents(conn, "biz_1") == 0
    assert count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 0
    (line,) = pay_logs(caplog)
    assert "cs_test_1" in line and "biz_1" in line and SANDBOX_WHSEC not in line


def test_webhook_regular_signature_unchanged(client, conn, stripe, google, sandbox, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    started(client, conn, google)
    assert deliver(client, event(), secret="whsec_other").status_code == 400  # neither secret: refused as before
    r = deliver(client, event(), secret=WHSEC)
    assert r.status_code == 200 and r.json() == {"received": True, "credited": True}
    assert wallet.balance_cents(conn, "biz_1") == 50000
    (a,) = conn.execute("SELECT args_redacted AS args FROM agent_action WHERE tool = 'web.topup'").fetchall()
    assert a["args"] == {"session_id": "cs_test_1", "event_id": "evt_1"}  # no sandbox mark
    # an allowlisted owner's business is not refused a regular-signed event either (R18 gates the sandbox secret only)
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "owner@example.com")
    assert setup.pay(config.load(), OWNER, "biz_1", "500")  # cs_test_2, by the sandbox key
    r = deliver(client, event(session_id="cs_test_2", event_id="evt_2"), secret=WHSEC)
    assert r.json() == {"received": True, "credited": True} and wallet.balance_cents(conn, "biz_1") == 100000


@pytest.mark.parametrize("secret", [WHSEC, SANDBOX_WHSEC], ids=["regular", "sandbox"])
@pytest.mark.parametrize(
    "kind,payment_status",
    [
        ("checkout.session.completed", "paid"),
        ("checkout.session.completed", "unpaid"),
        ("checkout.session.async_payment_succeeded", "paid"),
        ("checkout.session.async_payment_failed", "unpaid"),
        ("checkout.session.expired", "unpaid"),
    ],
)
def test_webhook_unknown_session_is_ignored_with_200(
    client, conn, stripe, google, sandbox, caplog, secret, kind, payment_status
):
    """The sandbox has two endpoints (dev's and prod's): every sandbox event reaches both boxes, each signed with
    that box's own secret. A Checkout another box made is unknown here: 200 (Stripe would retry a 4xx/5xx and in
    the end disable the endpoint), one log line, nothing written."""
    started(client, conn, google)
    caplog.set_level(logging.INFO, logger="agent.pay")
    payload = sandbox_event(kind=kind, payment_status=payment_status, session_id="cs_other_box")
    r = deliver(client, payload, secret=secret)
    assert r.status_code == 200 and r.json() == {"received": True, "ignored": "unknown session"}
    assert status(conn) == "open" and count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 0
    (line,) = pay_logs(caplog)
    assert "cs_other_box" in line


def test_live_key_in_sandbox_slot_is_not_used_as_sandbox(monkeypatch, sandbox, caplog):
    """A live key pasted into STRIPE_SANDBOX_SECRET_KEY must never charge an allowlisted owner real money labelled
    sandbox: only an sk_test_ or rk_test_ key is used as the sandbox; anything else falls back to the regular keys,
    with one warning that never quotes the key."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_regular")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_regular")
    regular = stripe_mode.StripeKeys(secret_key="sk_live_regular", webhook_secret="whsec_regular", sandbox=False)
    caplog.set_level(logging.WARNING, logger="agent.stripe_mode")
    for live in ("sk_live_pasted_by_mistake", "rk_live_restricted", "pk_test_publishable", "whsec_wrong_line"):
        monkeypatch.setenv("STRIPE_SANDBOX_SECRET_KEY", live)
        caplog.clear()
        assert stripe_mode.for_owner(config.load(), "owner@example.com") == regular
        (line,) = [r.getMessage() for r in caplog.records if r.name == "agent.stripe_mode"]
        assert "STRIPE_SANDBOX_SECRET_KEY" in line and live not in line and live[:8] not in line
    monkeypatch.setenv("STRIPE_SANDBOX_SECRET_KEY", "rk_test_restricted")  # a restricted test key is a sandbox key
    caplog.clear()
    assert stripe_mode.for_owner(config.load(), "owner@example.com").sandbox
    assert not [r for r in caplog.records if r.name == "agent.stripe_mode"]


@pytest.mark.parametrize("livemode", [True, None, "false", 0], ids=["live", "missing", "text", "zero"])
def test_sandbox_only_event_with_livemode_true_is_refused_400(
    client, conn, stripe, google, sandbox, monkeypatch, caplog, livemode
):
    """An event only the sandbox secret verified must say livemode false, exactly: anything else is refused 400 (a
    live event can never pass as sandbox; Stripe retries and alerts, as for a wrong secret), one error line, nothing
    written -- for an allowlisted owner's own Checkout too."""
    no_regular_keys(monkeypatch)
    started(client, conn, google)  # cs_test_1, by the sandbox key
    caplog.set_level(logging.INFO, logger="agent.pay")
    for kind, paid in (("checkout.session.completed", "paid"), ("checkout.session.expired", "unpaid")):
        caplog.clear()
        r = deliver(client, sandbox_event(livemode, kind=kind, payment_status=paid), secret=SANDBOX_WHSEC)
        assert r.status_code == 400
        (rec,) = [r for r in caplog.records if r.name == "agent.pay"]
        assert rec.levelno == logging.ERROR and SANDBOX_WHSEC not in rec.getMessage()
    assert status(conn) == "open" and count(conn, "SELECT count(*) AS n FROM wallet_entry") == 0
    assert count(conn, "SELECT count(*) AS n FROM agent_action WHERE tool = 'web.topup'") == 0


def test_both_secrets_set_sandbox_verifies_allowlisted_owner_event(client, conn, stripe, google, sandbox):
    """dev: the regular keys and the sandbox keys both set. An allowlisted owner's sandbox Checkout is credited by
    the event its sandbox endpoint signs; another owner's regular one by the regular endpoint's, unmarked."""
    started(client, conn, google)  # owner@example.com, allowlisted: cs_test_1 by the sandbox key
    assert key_of(stripe.sent[-1]) == SANDBOX_KEY
    r = deliver(client, sandbox_event(), secret=SANDBOX_WHSEC)
    assert r.status_code == 200 and r.json() == {"received": True, "credited": True}
    assert wallet.balance_cents(conn, "biz_1") == 50000
    (a,) = conn.execute("SELECT args_redacted AS args FROM agent_action WHERE tool = 'web.topup'").fetchall()
    assert (a["args"]["sandbox"], a["args"]["livemode"]) == ("true", "false")


def test_equal_secrets_are_regular_verified_with_a_warning(client, conn, stripe, google, sandbox, monkeypatch, caplog):
    """The two webhook secrets the same string: the event counts as verified by the regular secret (so the sandbox
    rules -- livemode false, R18's allowlist gate -- are never what decides it), with one warning per event."""
    monkeypatch.setenv("STRIPE_SANDBOX_WEBHOOK_SECRET", WHSEC)
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    started(client, conn, google)  # not allowlisted: a regular Checkout
    caplog.set_level(logging.INFO, logger="agent.pay")
    r = deliver(client, event(), secret=WHSEC)  # no livemode at all, as before
    assert r.status_code == 200 and r.json() == {"received": True, "credited": True}
    (a,) = conn.execute("SELECT args_redacted AS args FROM agent_action WHERE tool = 'web.topup'").fetchall()
    assert a["args"] == {"session_id": "cs_test_1", "event_id": "evt_1"}  # regular: no sandbox mark
    (rec,) = [r for r in caplog.records if r.name == "agent.pay" and r.levelno >= logging.WARNING]  # not the mail's
    assert rec.levelno == logging.WARNING and "STRIPE_SANDBOX_WEBHOOK_SECRET" in rec.getMessage()
    assert WHSEC not in rec.getMessage()


def test_payment_step_offered_to_allowlisted_owner_without_regular_keys(
    client, conn, stripe, google, sandbox, monkeypatch
):
    no_regular_keys(monkeypatch)
    managed_owner(client, conn, google)
    st = setup.state(conn, config.load(), "biz_1")
    assert st["step"] == "pay" and st["payments_ready"] is True
    assert setup.pay(config.load(), OWNER, "biz_1", "500").startswith("https://checkout.stripe.com/")
    assert key_of(stripe.sent[-1]) == SANDBOX_KEY
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")  # everyone else: not set up, as before
    assert setup.state(conn, config.load(), "biz_1")["payments_ready"] is False
    with pytest.raises(setup.Refused, match=setup.NOT_SET_UP):
        setup.pay(config.load(), OWNER, "biz_1", "500")
    assert len(stripe.sent) == 1


def test_balance_tools_offer_the_payment_to_allowlisted_owner_without_regular_keys(client, conn, sandbox, monkeypatch):
    no_regular_keys(monkeypatch)
    key = prepaid_key(conn)
    assert "note" not in call_tool(client, "get_balance", {}, key=key)["structuredContent"]["data"]
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert sc["pay_url"] == "http://testserver/?setup=biz_1" and "note" not in sc
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert sc["pay_url"] is None and sc["note"] == "payments are not set up on this server yet"
    assert call_tool(client, "get_balance", {}, key=key)["structuredContent"]["data"]["note"] == sc["note"]


def test_leaving_prepaid_expires_sandbox_checkouts_with_the_sandbox_key(client, conn, stripe, google, sandbox):
    started(client, conn, google)  # cs_test_1, by the sandbox key
    setup.choose(config.load(), OWNER, "biz_1", "own_account")
    expire = stripe.sent[-1]
    assert expire.url.path == "/v1/checkout/sessions/cs_test_1/expire" and key_of(expire) == SANDBOX_KEY
    assert status(conn) == "expired"


def test_refund_for_allowlisted_owner_uses_the_sandbox_key(conn, stripe, sandbox, monkeypatch):
    no_regular_keys(monkeypatch)
    managed(conn)  # owner@example.com
    fund(conn, cents=30000, session_id="cs_1", payment_intent="pi_1")
    assert ops.main(["refund", "biz_1", "100"]) == 0
    assert [key_of(r) for r in stripe.listed] == [SANDBOX_KEY] and [key_of(r) for r in stripe.sent] == [SANDBOX_KEY]
    assert wallet.balance_cents(conn, "biz_1") == 20000


def test_refund_for_other_owner_without_regular_keys_refuses_as_before(conn, stripe, sandbox, monkeypatch, capsys):
    no_regular_keys(monkeypatch)
    managed(conn, email="someone@example.com")
    fund(conn, cents=30000, session_id="cs_1", payment_intent="pi_1")
    assert ops.main(["refund", "biz_1", "100"]) == 1
    assert "STRIPE_SECRET_KEY is not set" in capsys.readouterr().err
    assert stripe.sent == [] and stripe.listed == []
