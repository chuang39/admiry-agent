"""The prepaid balance of managed businesses (spec 2026-09-25 rev. 2 D29, D30, §4, §5 "Managed mode"; the wallet
design of rev. 1 §3–§4, commit 202668c): integer cents, append-only entries, refs unique per business, and
payment-as-verification through claims.verify. No Stripe here: wallet.topup is the webhook's whole effect."""

import threading
from datetime import date

import psycopg
import pytest

from agent import business, claims, config, db, wallet
from agent.tests.conftest import MONEY_JARGON, TEST_DB_URL
from agent.tests.test_claims import draft_business


def managed(conn, biz="biz_1", email="owner@example.com"):
    """A business claimed by `email` (draft → claimed) whose owner chose prepaid ads run by Admiry."""
    draft_business(conn, biz)
    claims.claim(conn, biz, email)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = %s", (biz,))


def open_session(conn, biz="biz_1", session_id="cs_1", cents=50000):
    """A Checkout Session this server created (what POST /pay leaves)."""
    conn.execute(
        "INSERT INTO payment_session (id, business_id, amount_cents) VALUES (%s, %s, %s)", (session_id, biz, cents)
    )


def fund(conn, biz="biz_1", cents=50000, session_id="cs_1", payment_intent="pi_1") -> dict:
    """A paid top-up exactly as the webhook records it."""
    open_session(conn, biz, session_id, cents)
    with conn.transaction():
        return wallet.topup(conn, session_id=session_id, amount_cents=cents, payment_intent=payment_intent)


def test_money_jargon_catches_the_words_owners_never_see():
    assert (
        MONEY_JARGON.search("Your Wallet") and MONEY_JARGON.search("two ledgers") and MONEY_JARGON.search("settlements")
    )
    assert not MONEY_JARGON.search("balance, add funds, spent, refund")


def test_schema_reapplies_and_guards_kinds_signs_and_creators(conn):
    db.apply_schema(conn)  # the appended ALTERs are idempotent like the rest
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    assert business.row(conn, "biz_1")["paused_reason"] is None
    conn.execute(
        "INSERT INTO agreement_acceptance (business_id, email, version) VALUES ('biz_1', 'o@example.com', 'v0')"
    )
    assert conn.execute("SELECT document FROM agreement_acceptance").fetchone()["document"] == "management-agreement"
    for kind, cents in (("topup", -100), ("spend", 100), ("refund", 100), ("adjustment", 0), ("bonus", 100)):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO wallet_entry (business_id, kind, amount_cents, ref, created_by) "
                "VALUES ('biz_1', %s, %s, %s, 'ops')",
                (kind, cents, f"r-{kind}"),
            )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO wallet_entry (business_id, kind, amount_cents, ref, created_by) "
            "VALUES ('biz_1', 'topup', 100, 'r', 'someone')"
        )


def test_balance_is_the_sum_and_refs_are_idempotent_per_business(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1'), ('biz_2')")
    assert wallet.balance_cents(conn, "biz_1") == 0
    assert (
        wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=50000, ref="cs_1", created_by="stripe") is not None
    )
    assert (
        wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=50000, ref="cs_1", created_by="stripe") is None
    )  # replay
    day = date(2026, 9, 24)
    assert wallet.spend(conn, "biz_1", channel="google_search", day=day, cost_cents=1234) is not None
    assert wallet.spend(conn, "biz_1", channel="google_search", day=day, cost_cents=1234) is None  # re-run job
    assert (
        wallet.spend(conn, "biz_2", channel="google_search", day=day, cost_cents=99) is not None
    )  # same ref, other business
    assert (
        wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 25), cost_cents=0) is None
    )  # nothing spent, no row
    assert wallet.balance_cents(conn, "biz_1") == 48766 and wallet.balance_cents(conn, "biz_2") == -99
    row = conn.execute(
        "SELECT kind, amount_cents, ref, created_by FROM wallet_entry WHERE business_id = 'biz_1' AND kind = 'spend'"
    ).fetchone()
    assert dict(row) == {"kind": "spend", "amount_cents": -1234, "ref": "google_search:2026-09-24", "created_by": "job"}
    with pytest.raises(ValueError):
        wallet.add_entry(conn, "biz_1", kind="bonus", amount_cents=1, ref="x", created_by="ops")


def test_adjust_needs_a_note_and_a_refund_is_recorded_against_its_payment(conn):
    managed(conn)
    fund(conn, cents=50000)
    for cents, note in ((500, ""), (500, "   "), (0, "zero")):
        with pytest.raises(ValueError):
            wallet.adjust(conn, "biz_1", cents, note=note)
    first = wallet.adjust(conn, "biz_1", -2550, note="late Google credit")
    second = wallet.adjust(conn, "biz_1", -2550, note="late Google credit")  # the same correction twice is two rows
    assert first != second
    assert wallet.refund(conn, "biz_1", refund_id="re_1", amount_cents=10000, session_id="cs_1") is not None
    assert wallet.refund(conn, "biz_1", refund_id="re_1", amount_cents=10000, session_id="cs_1") is None
    assert wallet.balance_cents(conn, "biz_1") == 50000 - 5100 - 10000
    assert (
        conn.execute("SELECT refunded_cents FROM payment_session WHERE id = 'cs_1'").fetchone()["refunded_cents"]
        == 10000
    )
    refs = [r["ref"] for r in conn.execute("SELECT ref FROM wallet_entry WHERE kind = 'adjustment'")]
    assert all(r.startswith("ops:") for r in refs) and len(set(refs)) == 2


def test_usd_formats_cents_for_owners_and_tools():
    assert wallet.usd(123456) == "$1,234.56" and wallet.usd(-2550) == "-$25.50" and wallet.usd(0) == "$0.00"
    assert wallet.to_usd(48766) == 487.66 and wallet.to_usd(-99) == -0.99


def test_parse_usd_reads_what_an_owner_types():
    bounds = {"min_usd": 200, "max_usd": 5000}
    assert wallet.parse_usd("500", **bounds) == 50000
    assert wallet.parse_usd(" $1,250.50 ", **bounds) == 125050
    assert wallet.parse_usd("200", **bounds) == 20000 and wallet.parse_usd("5000.00", **bounds) == 500000


@pytest.mark.parametrize("text", ["", "abc", "-500", "0", "199.99", "5000.01", "12.345", "1e3", "NaN", "٥٠٠"])
def test_parse_usd_refuses_anything_else(text):
    with pytest.raises(wallet.AmountRefused):
        wallet.parse_usd(text, min_usd=200, max_usd=5000)


def test_cents_from_usd_rounds_at_the_tool_boundary_and_bounds():
    bounds = {"min_usd": 200, "max_usd": 5000}
    assert wallet.cents_from_usd(250.5, **bounds) == 25050
    assert wallet.cents_from_usd(1000.1, **bounds) == 100010  # 1000.1 * 100 == 100009.99999999999
    assert wallet.cents_from_usd(300, **bounds) == 30000
    for bad in (-300, 0, 199.99, 5000.01, float("nan"), float("inf"), True, "500", 1e308, -1e308):
        with pytest.raises(wallet.AmountRefused):
            wallet.cents_from_usd(bad, **bounds)


def test_topup_credits_once_verifies_a_managed_business_and_lifts_the_pause(conn):
    managed(conn)
    conn.execute("UPDATE business SET paused_reason = 'no_funds' WHERE id = 'biz_1'")
    out = fund(conn, cents=50000)
    assert out == {
        "credited": True,
        "business_id": "biz_1",
        "amount_cents": 50000,
        "credited_cents": 50000,
        "paid_cents": 50000,
        "processing_cents": 0,
        "rail": "card",
        "verified_now": True,
        "resumed": True,
    }
    row = business.row(conn, "biz_1")
    assert row["claim_state"] == "verified" and row["verified_via"] == "payment" and row["verified_at"] is not None
    assert row["claim_expires_at"] is None and row["paused_reason"] is None
    s = conn.execute("SELECT status, paid_at, payment_intent FROM payment_session WHERE id = 'cs_1'").fetchone()
    assert s["status"] == "paid" and s["paid_at"] is not None and s["payment_intent"] == "pi_1"
    with conn.transaction():
        again = wallet.topup(conn, session_id="cs_1", amount_cents=50000, payment_intent="pi_1")
    assert again == {"credited": False, "reason": "already credited", "business_id": "biz_1"}
    assert wallet.balance_cents(conn, "biz_1") == 50000


def test_topup_leaves_a_verified_business_alone_and_claims_a_released_draft_again(conn):
    managed(conn, "biz_1")
    conn.execute(
        "UPDATE business SET claim_state = 'verified', verified_via = 'ops', "
        "verified_at = now() - interval '3 days' WHERE id = 'biz_1'"
    )
    before = business.row(conn, "biz_1")["verified_at"]
    assert fund(conn, "biz_1")["verified_now"] is False
    row = business.row(conn, "biz_1")
    assert (row["claim_state"], row["verified_via"], row["verified_at"]) == ("verified", "ops", before)
    managed(conn, "biz_2", email="two@example.com")
    conn.execute(
        "UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL WHERE id = 'biz_2'"
    )  # claim-expire
    out = fund(conn, "biz_2", cents=30000, session_id="cs_2")
    assert out["credited"] is True and out["verified_now"] is True  # its owner paid: claimed again, verified
    row = business.row(conn, "biz_2")
    assert (row["claim_state"], row["verified_via"], row["owner_email"]) == ("verified", "payment", "two@example.com")
    assert row["slug"] is not None and wallet.balance_cents(conn, "biz_2") == 30000


def test_verify_by_payment_needs_an_active_managed_business_with_an_owner(conn):
    draft_business(conn, "biz_own")
    claims.claim(conn, "biz_own", "owner@example.com")  # own_account, the default
    out = fund(conn, "biz_own", session_id="cs_own")
    assert out["credited"] is True and out["verified_now"] is False  # credited (the operator refunds), not verified
    assert business.row(conn, "biz_own")["claim_state"] == "claimed"
    managed(conn, "biz_gone", email="gone@example.com")
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_gone'")  # retired
    assert fund(conn, "biz_gone", session_id="cs_gone")["verified_now"] is False
    assert business.row(conn, "biz_gone")["claim_state"] == "claimed"


def test_claims_verify_is_the_one_way_to_verified(conn):
    draft_business(conn, "biz_1")
    assert claims.verify(conn, "biz_1", via="payment") is False  # nobody claimed it yet
    claims.claim(conn, "biz_1", "owner@example.com")
    assert claims.verify(conn, "biz_1", via="payment", ads_mode="managed") is False  # an own_account business
    assert claims.verify(conn, "biz_1", via="ads_link") is True
    row = business.row(conn, "biz_1")
    assert (row["claim_state"], row["verified_via"], row["claim_expires_at"]) == ("verified", "ads_link", None)
    assert claims.verify(conn, "biz_1", via="payment") is False  # never re-stamped
    assert business.row(conn, "biz_1")["verified_via"] == "ads_link"


def test_topup_of_a_session_this_server_did_not_create_writes_nothing(conn):
    managed(conn)
    with conn.transaction():
        out = wallet.topup(conn, session_id="cs_elsewhere", amount_cents=50000, payment_intent="pi_9")
    assert out == {"credited": False, "reason": "unknown session"}
    open_session(conn, session_id="cs_0")
    with conn.transaction():
        out = wallet.topup(conn, session_id="cs_0", amount_cents=0, payment_intent=None)
    assert out == {"credited": False, "reason": "nothing paid", "business_id": "biz_1"}
    assert conn.execute("SELECT count(*) AS n FROM wallet_entry").fetchone()["n"] == 0
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_two_deliveries_racing_for_one_session_credit_once(conn):
    managed(conn)
    open_session(conn)
    second: dict = {}

    def deliver_again():
        with db.connect(TEST_DB_URL) as other, other.transaction():
            second.update(wallet.topup(other, session_id="cs_1", amount_cents=50000, payment_intent="pi_1"))

    with conn.transaction():
        first = wallet.topup(conn, session_id="cs_1", amount_cents=50000, payment_intent="pi_1")
        racer = threading.Thread(target=deliver_again)
        racer.start()
        racer.join(0.5)
        assert racer.is_alive()  # waiting on the first delivery's row lock
    racer.join(10)
    assert first["credited"] is True
    assert second == {"credited": False, "reason": "already credited", "business_id": "biz_1"}
    assert conn.execute("SELECT count(*) AS n FROM wallet_entry").fetchone()["n"] == 1


def test_summary_and_entries_show_money_in_and_out_but_not_spend(conn):
    managed(conn)
    fund(conn, cents=50000, session_id="cs_1")
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=1234)
    wallet.refund(conn, "biz_1", refund_id="re_1", amount_cents=10000, session_id="cs_1")
    wallet.adjust(conn, "biz_1", 500, note="goodwill credit")
    open_session(conn, session_id="cs_2", cents=20000)  # started, not paid
    conn.execute(
        "INSERT INTO payment_session (id, business_id, amount_cents, created_at) "
        "VALUES ('cs_old', 'biz_1', 20000, now() - interval '2 days')"
    )  # Stripe let it lapse
    s = wallet.summary(conn, "biz_1")
    assert (
        s["balance_cents"] == 50000 - 1234 - 10000 + 500 and s["pending_topups"] == 1 and s["last_topup_at"] is not None
    )
    rows, cursor = wallet.entries(conn, "biz_1", limit=2)
    assert [r["kind"] for r in rows] == ["adjustment", "refund"] and cursor == rows[-1]["id"]
    rows, cursor = wallet.entries(conn, "biz_1", cursor=cursor, limit=2)
    assert [r["kind"] for r in rows] == ["topup"] and cursor is None
    assert rows[0]["amount_cents"] == 50000 and rows[0]["note"] == "Card payment of $500.00 ($0.00 transaction fee)"


def priced_session(conn, *, session_id="cs_q", credit=100000, processing=3330, rail="card", status="open"):
    """A Checkout Session as setup.pay records it since D92: Stripe charges credit + processing."""
    conn.execute(
        "INSERT INTO payment_session (id, business_id, amount_cents, credited_cents, processing_cents, rail, status) "
        "VALUES (%s, 'biz_1', %s, %s, %s, %s, %s)",
        (session_id, credit + processing, credit, processing, rail, status),
    )


def test_a_card_top_up_credits_the_chosen_amount_not_the_price(conn):
    managed(conn)  # this file's helper: biz_1 claimed and prepaid
    priced_session(conn)
    out = wallet.topup(conn, session_id="cs_q", amount_cents=103330, payment_intent="pi_q")
    assert out["credited"] and out["credited_cents"] == 100000 and out["paid_cents"] == 103330
    assert wallet.balance_cents(conn, "biz_1") == 100000
    note = conn.execute("SELECT note FROM wallet_entry WHERE ref = 'cs_q'").fetchone()["note"]
    assert note == "Card payment of $1,033.30 ($33.30 transaction fee)"
    s = conn.execute("SELECT status, amount_cents FROM payment_session WHERE id = 'cs_q'").fetchone()
    assert (s["status"], s["amount_cents"]) == ("paid", 103330)


def test_a_total_that_differs_from_ours_credits_nothing(conn):
    managed(conn)
    priced_session(conn)
    out = wallet.topup(conn, session_id="cs_q", amount_cents=100000, payment_intent="pi_q")
    assert out == {"credited": False, "reason": "amount mismatch", "business_id": "biz_1", "expected_cents": 103330}
    assert wallet.balance_cents(conn, "biz_1") == 0
    assert conn.execute("SELECT status FROM payment_session WHERE id = 'cs_q'").fetchone()["status"] == "open"


def test_a_session_from_before_the_payment_cost_credits_what_it_charged(conn):
    """Review Focus 3: a v0 row (no credited_cents) keeps v0's terms -- everything paid is credited."""
    managed(conn)
    open_session(conn, session_id="cs_v0", cents=50000)
    out = wallet.topup(conn, session_id="cs_v0", amount_cents=50000, payment_intent="pi_v0")
    assert out["credited"] and wallet.balance_cents(conn, "biz_1") == 50000


def test_a_bank_transfer_on_its_way_is_counted_as_pending(conn):
    managed(conn)
    priced_session(conn, session_id="cs_b", processing=500, rail="ach", status="processing")
    conn.execute("UPDATE payment_session SET created_at = now() - interval '3 days' WHERE id = 'cs_b'")
    s = wallet.summary(conn, "biz_1")
    assert s["pending_topups"] == 1 and s["transfer_pending_cents"] == 100000
    assert s["open_topups"] == 0  # a 'processing' bank transfer is never counted as 'open' (finding 1)


def test_open_topups_counts_only_open_sessions_never_processing_ones(conn):
    """Finding 1: the pay form's returning-from-Stripe poll must tell a card Checkout still waiting on Stripe
    (`open`) from a bank transfer already on its way (`processing`) -- open_topups counts only the former,
    transfer_pending_cents only the latter; pending_topups keeps counting both (other readers use it)."""
    managed(conn)
    open_session(conn, session_id="cs_card", cents=50000)  # a card Checkout, still open
    priced_session(conn, session_id="cs_bank", credit=100000, processing=500, rail="ach", status="processing")
    s = wallet.summary(conn, "biz_1")
    assert s["open_topups"] == 1
    assert s["pending_topups"] == 2
    assert s["transfer_pending_cents"] == 100000  # the processing one only, never the open card leg


def test_refund_plan_never_returns_the_payment_cost(conn):
    """Review Focus 5."""
    managed(conn)
    priced_session(conn)
    wallet.topup(conn, session_id="cs_q", amount_cents=103330, payment_intent="pi_q")
    plan = wallet.refund_plan(conn, "biz_1", 103330)
    assert [p["amount_cents"] for p in plan] == [100000]


def test_a_refund_of_a_bank_transfer_says_so(conn):
    managed(conn)
    priced_session(conn, session_id="cs_b", processing=500, rail="ach")
    wallet.topup(conn, session_id="cs_b", amount_cents=100500, payment_intent="pi_b")
    wallet.refund(conn, "biz_1", refund_id="re_b", amount_cents=1000, session_id="cs_b")
    assert conn.execute("SELECT note FROM wallet_entry WHERE ref = 're_b'").fetchone()["note"] == (
        "Refund to the bank account"
    )


def test_settings_read_stripe_and_the_topup_bounds(monkeypatch):
    for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "AGENT_TOPUP_MIN_USD", "AGENT_TOPUP_MAX_USD"):
        monkeypatch.setenv(var, "")
    s = config.load()
    assert (s.stripe_secret_key, s.stripe_webhook_secret, s.topup_min_usd, s.topup_max_usd) == (None, None, 200, 5000)
    assert s.payments_ready is False
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_SECRET")
    assert config.load().payments_ready is False  # without the webhook secret nothing could be credited
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_SECRET")
    monkeypatch.setenv("AGENT_TOPUP_MIN_USD", "100")
    monkeypatch.setenv("AGENT_TOPUP_MAX_USD", "900")
    s = config.load()
    assert s.payments_ready is True and (s.topup_min_usd, s.topup_max_usd) == (100, 900)
    assert "SECRET" not in repr(s)  # never printed with the settings
    for lo, hi in (("0", "900"), ("1000", "900")):
        monkeypatch.setenv("AGENT_TOPUP_MIN_USD", lo)
        monkeypatch.setenv("AGENT_TOPUP_MAX_USD", hi)
        with pytest.raises(ValueError):
            config.load()
