"""Prepaid mode (spec 2026-09-25 rev. 2 D25, D28, §3; the setup service since 2026-09-29): the choice and its rules,
the Prepayment Addendum (the file, its page, the acceptance per document), and the link path closed to a prepaid
business."""

from urllib.parse import parse_qs

import pytest

from agent import ads_mode, agreements, business, config, google_ads_link, legal, ops, setup, tools_account, wallet
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, without_site_header
from agent.tests.test_google_ads_link import CID, claimed_agreed
from agent.tests.test_wallet import fund, managed, open_session
from agent.tests.test_web import claimed_business

OWNER = setup.Owner(email="owner@example.com", ip="local")


def choose(client, mode, biz="biz_1"):
    return setup.choose(config.load(), OWNER, biz, mode)


def accept(client, document, version="v1", biz="biz_1"):
    return setup.accept(config.load(), OWNER, biz, {document: version})


def managed_owner(client, conn, google, *, terms=True):
    """biz_1 claimed by the signed-in owner@example.com and switched to prepaid; the management agreement and the
    Prepayment Addendum accepted when `terms`."""
    claimed_business(client, conn, google)
    choose(client, "managed")
    if terms:
        setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v1", legal.ADDENDUM: "v0.2"})


def audited(conn, tool):
    return [r["result"] for r in conn.execute("SELECT result FROM agent_action WHERE tool = %s ORDER BY id", (tool,))]


def plain(text: str) -> bool:
    return (
        not JARGON.search(text)
        and not ACCOUNT_JARGON.search(text)
        and not MONEY_JARGON.search(text)
        and "manager account" not in text
    )


def test_the_shipped_addendum_is_a_marked_draft_with_every_required_part():
    version, html = legal.current(legal.ADDENDUM)
    assert version == "v0.2" == agreements.ADDENDUM_VERSION and legal.current()[0] == agreements.CURRENT_VERSION
    assert "DRAFT" not in html and "[to be completed by counsel]" in html
    for part in (
        "Google Ads Management Agreement",
        "never shared",
        "exact cost",
        "statement shows",
        "refundable",
        "earns no interest",
        "pauses your ads",
        "$0 platform fee",
        "No management fee during early access",
        "Governing law",
    ):
        assert part in html, part
    assert plain(html)


def test_the_addendum_page_is_public_versioned_and_noindex(client):
    r = client.get("/legal/prepayment-addendum")
    assert r.status_code == 200 and r.headers["x-robots-tag"] == "noindex"
    assert "<title>Admiry — Prepayment Addendum</title>" in r.text and "Version v0.2" in r.text
    assert '<meta name="robots" content="noindex">' in r.text and plain(r.text)


def test_acceptance_is_per_document_and_the_terms_need_both(conn, monkeypatch):
    claimed_agreed(conn, agree=False)
    assert (
        agreements.accept(conn, business_id="biz_1", email="Owner@Example.com", ip=None, document=legal.ADDENDUM)
        == "v0.2"
    )
    assert agreements.accepted(conn, "biz_1", legal.ADDENDUM) and not agreements.accepted(conn, "biz_1")
    assert not agreements.terms_accepted(conn, "biz_1")  # the addendum alone is not enough
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    assert agreements.terms_accepted(conn, "biz_1")
    rows = conn.execute("SELECT document, email, version FROM agreement_acceptance ORDER BY id").fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("prepayment-addendum", "owner@example.com", "v0.2"),
        ("management-agreement", "owner@example.com", "v1"),
    ]
    monkeypatch.setattr(agreements, "ADDENDUM_VERSION", "v1")  # counsel's addendum ships: ask again
    assert agreements.accepted(conn, "biz_1") and not agreements.terms_accepted(conn, "biz_1")


def test_the_owner_switches_to_prepaid_and_accepts_both_texts(client, conn, google):
    claimed_business(client, conn, google)
    st = choose(client, "managed")
    assert st["mode"] == "managed" and st["step"] == "accept"
    assert business.row(conn, "biz_1")["ads_mode"] == "managed"
    assert audited(conn, "web.ads_mode") == [{"ads_mode": "managed"}]
    assert [d["id"] for d in st["documents"]] == [legal.AGREEMENT, legal.ADDENDUM]
    st = setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v1", legal.ADDENDUM: "v0.2"})
    assert st["step"] == "pay" and agreements.terms_accepted(conn, "biz_1")
    assert audited(conn, "web.agree") == [{"version": "v1"}]
    assert audited(conn, "web.agree_prepayment") == [{"version": "v0.2"}]
    page = client.get("/connect").text  # Settings: the balance and both accepted texts, no forms
    assert "Balance: <strong>$0.00</strong>" in page and "Prepayment Addendum v0.2" in page
    assert plain(without_site_header(page))


def test_choosing_the_way_already_in_place_changes_nothing(client, conn, google):
    claimed_business(client, conn, google)
    assert choose(client, "own_account")["mode"] == "own_account"
    assert audited(conn, "web.ads_mode") == []


def test_the_choice_needs_a_claim_and_a_known_way(client, conn, google):
    claimed_business(client, conn, google)
    with pytest.raises(setup.Refused) as e:
        choose(client, "shared")
    assert e.value.status == 400
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL WHERE id = 'biz_1'")
    with pytest.raises(setup.Refused, match=ads_mode.NOT_CLAIMED):
        choose(client, "managed")
    assert {r_["ads_mode"] for r_ in conn.execute("SELECT ads_mode FROM business")} == {"own_account"}
    assert audited(conn, "web.ads_mode") == []


def test_prepaid_is_refused_while_a_google_ads_link_is_live(client, conn, google):
    claimed_business(client, conn, google)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    for status in ("active", "manual"):
        google_ads_link.save(conn, "biz_1", CID, status=status)
        with pytest.raises(setup.Refused, match=ads_mode.LINK_IN_PLACE.format(display="123-456-7890")):
            choose(client, "managed")
    # a live link: Settings' one button goes on to the setup step; the lock's sentence and the missing Change moved
    # to the Your ads page with the account line (test_your_ads_page)
    assert ">Open setup<" in client.get("/connect").text
    google_ads_link.save(conn, "biz_1", CID, status="refused")  # declined in Google Ads: free to switch
    assert choose(client, "managed")["mode"] == "managed"
    assert business.row(conn, "biz_1")["ads_mode"] == "managed" and len(audited(conn, "web.ads_mode")) == 1


def test_leaving_prepaid_is_refused_once_money_moved(client, conn, google):
    managed_owner(client, conn, google, terms=False)
    assert choose(client, "own_account")["mode"] == "own_account"
    assert choose(client, "managed")["mode"] == "managed"
    fund(conn, session_id="cs_paid")  # money in: prepaid for good
    with pytest.raises(setup.Refused, match=ads_mode.MONEY_IN_PLACE):
        choose(client, "own_account")
    assert business.row(conn, "biz_1")["ads_mode"] == "managed"
    assert [a["ads_mode"] for a in audited(conn, "web.ads_mode")] == ["managed", "own_account", "managed"]
    managed(conn, "biz_2", email="two@example.com")
    assert ads_mode.locked(conn, "biz_2", "managed") is None
    wallet.adjust(conn, "biz_2", 500, note="goodwill credit")  # any money, not only card payments
    assert ads_mode.locked(conn, "biz_2", "managed") == ads_mode.MONEY_IN_PLACE


def test_the_prepayment_terms_are_only_for_a_prepaid_business(client, conn, google):
    claimed_business(client, conn, google)  # own_account
    for given in ({legal.ADDENDUM: "v0.1"}, {"terms-of-sale": "v0.1"}):
        with pytest.raises(setup.Refused, match=setup.NOT_NEEDED) as e:
            setup.accept(config.load(), OWNER, "biz_1", given)
        assert e.value.status == 400
    assert conn.execute("SELECT count(*) AS n FROM agreement_acceptance").fetchone()["n"] == 0


def test_a_link_turning_active_never_verifies_a_prepaid_business(conn):
    claimed_agreed(conn)
    google_ads_link.save(conn, "biz_1", CID, status="pending")
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")  # prepaid by now (an operator's switch)
    with conn.transaction():
        out = google_ads_link.apply(conn, "biz_1", CID, google_ads_link.Remote(status="active", resource_name=None))
    assert out.became_active and not out.verified_now  # D29: prepaid verifies by payment
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_ops_ads_link_refuses_a_prepaid_business(conn, capsys):
    claimed_agreed(conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    assert ops.main(["ads-link", "biz_1", "123-456-7890", "--status", "active"]) == 1
    assert "nothing to link" in capsys.readouterr().err
    assert google_ads_link.get(conn, "biz_1") is None and business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_the_choice_and_the_prepayment_terms_read_plainly_in_the_audit_log():
    row = {"actor": "web:owner@example.com", "actor_class": "business", "ok": True}
    assert tools_account.sentence({**row, "tool": "web.ads_mode", "result": {"ads_mode": "managed"}}, None) == (
        "The owner (web) chose how Admiry runs the ads: prepaid, in a Google Ads account Admiry runs"
    )
    assert tools_account.sentence({**row, "tool": "web.ads_mode", "result": {"ads_mode": "own_account"}}, None) == (
        "The owner (web) chose how Admiry runs the ads: in the owner's own Google Ads account"
    )
    assert tools_account.sentence({**row, "tool": "web.agree_prepayment", "result": {"version": "v0.1"}}, None) == (
        "The owner (web) accepted the prepayment terms (v0.1)"
    )


def test_the_row_lock_backstop_still_counts_a_pending_invitation_and_a_recent_open_checkout(conn):
    """ads_mode.locked is what ads_mode.change enforces under the row lock: if setup.choose's undo (withdraw the
    invitation, expire the Checkout) races a new invitation or Checkout, the switch is still refused."""
    claimed_agreed(conn)
    google_ads_link.save(conn, "biz_1", CID, status="pending")
    assert ads_mode.locked(conn, "biz_1", "own_account") == ads_mode.LINK_IN_PLACE.format(display="123-456-7890")
    managed(conn, "biz_2", email="two@example.com")
    open_session(conn, "biz_2", session_id="cs_now")
    assert ads_mode.locked(conn, "biz_2", "managed") == ads_mode.MONEY_IN_PLACE
    conn.execute("UPDATE payment_session SET created_at = now() - interval '25 hours' WHERE id = 'cs_now'")
    assert ads_mode.locked(conn, "biz_2", "managed") is None  # past a Checkout Session's 24-hour life


def test_pay_by_bank_records_the_price_the_credit_and_the_rail(client, conn, stripe, google, monkeypatch):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    managed_owner(client, conn, google)
    url = setup.pay(config.load(), OWNER, "biz_1", "1000", "ach")
    assert url.startswith("https://checkout.stripe.com/")
    row = conn.execute(
        "SELECT amount_cents, credited_cents, processing_cents, rail, status FROM payment_session"
    ).fetchone()
    assert dict(row) == {
        "amount_cents": 100500,
        "credited_cents": 100000,
        "processing_cents": 500,
        "rail": "ach",
        "status": "open",
    }
    assert parse_qs(stripe.sent[-1].content.decode())["payment_method_types[0]"] == ["us_bank_account"]


def test_pay_by_bank_is_refused_while_bank_transfers_are_off(client, conn, stripe, google):
    managed_owner(client, conn, google)
    with pytest.raises(setup.Refused, match="Bank transfers are not offered yet"):
        setup.pay(config.load(), OWNER, "biz_1", "1000", "ach")
    assert stripe.sent == []


def test_pay_without_a_rail_is_a_card_payment_at_the_card_price(client, conn, stripe, google):
    managed_owner(client, conn, google)
    setup.pay(config.load(), OWNER, "biz_1", "500")
    row = conn.execute("SELECT amount_cents, credited_cents, rail FROM payment_session").fetchone()
    assert (row["amount_cents"], row["credited_cents"], row["rail"]) == (51680, 50000, "card")


def test_an_unknown_rail_is_refused_before_stripe(client, conn, stripe, google):
    managed_owner(client, conn, google)
    with pytest.raises(setup.Refused) as e:
        setup.pay(config.load(), OWNER, "biz_1", "500", "crypto")
    assert e.value.status == 400 and stripe.sent == []


def test_a_bank_transfer_on_its_way_keeps_the_business_prepaid(client, conn, stripe, google, monkeypatch):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    managed_owner(client, conn, google)
    setup.pay(config.load(), OWNER, "biz_1", "1000", "ach")
    conn.execute("UPDATE payment_session SET status = 'processing', created_at = now() - interval '2 days'")
    with pytest.raises(setup.Refused, match="one may still arrive"):
        setup.choose(config.load(), OWNER, "biz_1", "own_account")
    assert conn.execute("SELECT status FROM payment_session").fetchone()["status"] == "processing"
    # finding 10: unlike an 'open' Checkout (a Checkout Session's 24-hour life), a 'processing' bank transfer locks
    # the business at any age -- ads_mode.locked is what the row lock actually enforces (setup.choose's refusal above
    # is read through setup.Refused's message only)
    assert ads_mode.locked(conn, "biz_1", "managed") == ads_mode.MONEY_IN_PLACE


def test_a_failed_bank_transfer_does_not_lock_the_way(client, conn, stripe, google, monkeypatch):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    managed_owner(client, conn, google)
    setup.pay(config.load(), OWNER, "biz_1", "1000", "ach")
    conn.execute("UPDATE payment_session SET status = 'failed', created_at = now() - interval '2 days'")
    assert setup.choose(config.load(), OWNER, "biz_1", "own_account")["mode"] == "own_account"
