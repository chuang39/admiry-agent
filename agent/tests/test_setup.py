"""setup.py (spec 2026-09-29 setup-in-conversation): the state the home's setup cards follow, one Accept for every
document a way needs (D66), the first top-up (D68) and the choice of way."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from agent import ads_mode, agreements, business, config, google_ads_link, legal, setup, stripe_client, wallet
from agent.channels import registry
from agent.channels.google_search import adapter as google
from agent.channels.google_search import conversions as ads_conversions
from agent.channels.google_search.client import BUSY, AdsError  # by name: `google_ads` here is a test fixture
from agent.tests.conftest import JARGON
from agent.tests.test_claims import draft_business
from agent.tests.test_google_ads_link import (
    CID,
    MANAGED,
    RESOURCE,
    SECRETS,
    claimed_agreed,
    failure,
    google_ads,
    pending_link,
)
from agent.tests.test_managed_mode import managed_owner
from agent.tests.test_wallet import fund, managed, open_session

OWNER = setup.Owner(email="owner@example.com", ip="203.0.113.5")


def audited(conn, tool):
    return [r["result"] for r in conn.execute("SELECT result FROM agent_action WHERE tool = %s ORDER BY id", (tool,))]


def test_the_first_top_up_is_two_weeks_of_the_daily_budget_within_the_bounds():
    s = config.load()  # defaults: $200..$5,000
    assert setup.suggested_topup_usd(66, s) == 930  # 66 × 14 = 924, up to the next $10
    assert setup.suggested_topup_usd(10, s) == 200  # $140 is under the floor
    assert setup.suggested_topup_usd(None, s) == 200
    assert setup.suggested_topup_usd(1000, s) == 5000


def test_documents_follow_the_way():
    assert setup.documents("own_account") == [legal.AGREEMENT]
    assert setup.documents("managed") == [legal.AGREEMENT, legal.ADDENDUM]


def test_the_steps_from_claim_to_link(conn):
    draft_business(conn)
    s = config.load()
    assert setup.state(conn, s, "biz_1")["step"] == "claim"
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    st = setup.state(conn, s, "biz_1")
    assert st["step"] == "choose" and st["mode"] == "own_account" and st["locked"] is None
    assert st["name"] == "Bright Smile Dental" and st["topup"] is None and st["balance_usd"] is None
    assert setup.state(conn, s, "biz_1", chosen=True)["step"] == "accept"
    assert st["documents"] == [
        {
            "id": "management-agreement",
            "title": "Google Ads Management Agreement",
            "version": "v1",
            "url": "/legal/management-agreement",
            "accepted": False,
        }
    ]
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    assert setup.state(conn, s, "biz_1")["step"] == "link"  # an accepted agreement means the way was chosen


def test_a_pending_link_stays_on_link_and_a_prepaid_business_pays_until_funded(conn):
    pending_link(conn)
    st = setup.state(conn, config.load(), "biz_1")
    assert st["step"] == "link" and st["link"]["status"] == "pending" and st["locked"] is None
    conn.execute("DELETE FROM ads_link")
    ads_mode.change(conn, "biz_1", "managed")  # both places (P6): the column alone no longer moves the way
    st = setup.state(conn, config.load(), "biz_1")
    assert st["step"] == "accept" and [d["accepted"] for d in st["documents"]] == [True, False]
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    st = setup.state(conn, config.load(), "biz_1")
    assert (
        st["step"] == "pay"
        and st["balance_usd"] == 0
        and st["topup"]
        == {
            "suggested_usd": 200,
            "min_usd": 200,
            "max_usd": 5000,
            "daily_usd": None,
            "rails": ["card"],
            "rates": {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500},
        }
    )
    fund(conn, cents=30000)
    assert setup.state(conn, config.load(), "biz_1")["step"] == "done"


def test_the_card_offers_only_the_ways_the_latest_plan_can_run(conn):
    """Walk fixes 1, final review B I3: the server decides which ways the card offers, by the plan answer's rule
    (plans.ways, plans.google_open): the business's own Google Ads account for a plan with Google Search; Admiry running
    the ads from a prepaid balance for a plan that funds a place Admiry runs from it (NewsBreak), and for Google Search
    since A3c-1 (a pool account of Admiry's), so Google Search alone offers both now; before any plan, both, as the card
    always offered. MODE_WAITS is the sentence for a plan whose places leave the prepaid way out."""
    from agent.tests.test_ads_run import sections
    from agent.tests.test_ads_run_channels import add_plan_by_hand, newsbreak_only, two_channels

    claimed_agreed(conn, agree=False)
    s = config.load()

    def offered() -> tuple[list[str], str | None]:
        st = setup.state(conn, s, "biz_1")
        return st["ways"], st["ways_note"]

    assert offered() == (["own_account", "managed"], None)  # no plan yet
    add_plan_by_hand(conn, sections(), status="draft")  # Google Search alone: both ways since A3c-1
    assert offered() == (["own_account", "managed"], None)
    assert setup.MODE_WAITS == (
        "Google Search runs in your own Google Ads account for now; Admiry-run Google accounts are coming."
    )
    add_plan_by_hand(conn, two_channels(), status="draft")
    assert offered() == (["own_account", "managed"], None)
    add_plan_by_hand(conn, newsbreak_only(), status="draft")
    assert offered() == (["managed"], None)


def test_a_prepaid_business_keeps_its_way_when_the_card_no_longer_offers_it(conn, monkeypatch):
    """B I3: nothing is undone -- a business already prepaying keeps its way, and the card its row with Change, while a
    plan whose places do not run from a balance leaves the prepaid way out of the choice (Google Search alone before
    A3c-1, its adapter declaring only the owner's own account)."""
    from agent.tests.test_ads_run import sections
    from agent.tests.test_ads_run_channels import add_plan_by_hand

    own_only = google.GoogleSearchAdapter().capabilities().model_copy(update={"modes": {"own_account"}})
    monkeypatch.setattr(google.GoogleSearchAdapter, "capabilities", lambda self: own_only)  # before A3c-1
    managed(conn)
    add_plan_by_hand(conn, sections(), status="draft")
    st = setup.state(conn, config.load(), "biz_1")
    assert st["mode"] == "managed" and st["step"] != "choose" and st["ways"] == ["own_account"]


def test_one_accept_records_every_needed_document_in_one_go(conn):
    managed(conn)
    st = setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v1", legal.ADDENDUM: "v0.2"})
    assert st["step"] == "pay"
    rows = conn.execute("SELECT document, email, version, ip FROM agreement_acceptance ORDER BY id").fetchall()
    assert [dict(r) for r in rows] == [
        {"document": legal.AGREEMENT, "email": "owner@example.com", "version": "v1", "ip": "203.0.113.5"},
        {"document": legal.ADDENDUM, "email": "owner@example.com", "version": "v0.2", "ip": "203.0.113.5"},
    ]
    assert audited(conn, "web.agree") == [{"version": "v1"}] and audited(conn, "web.agree_prepayment") == [
        {"version": "v0.2"}
    ]


@pytest.mark.parametrize(
    ("given", "text", "status"),
    [
        ({legal.AGREEMENT: "v0.1"}, setup.TICK_ALL, 400),  # prepaid needs the Addendum too
        ({legal.AGREEMENT: "v0.1", legal.ADDENDUM: "v0"}, setup.CHANGED, 409),
        ({legal.AGREEMENT: "v0.1", legal.ADDENDUM: "v0.1", "other": "v1"}, setup.NOT_NEEDED, 400),
        ("yes", setup.NOT_NEEDED, 400),
    ],
)
def test_accept_is_all_or_nothing(conn, given, text, status):
    managed(conn)
    with pytest.raises(setup.Refused) as e:
        setup.accept(config.load(), OWNER, "biz_1", given)
    assert (str(e.value), e.value.status) == (text, status)
    assert conn.execute("SELECT count(*) AS n FROM agreement_acceptance").fetchone()["n"] == 0


def test_the_addendum_is_not_needed_on_the_owners_own_account(conn):
    claimed_agreed(conn, agree=False)
    with pytest.raises(setup.Refused, match="not needed"):
        setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v0.1", legal.ADDENDUM: "v0.1"})


def test_accepting_again_is_harmless(conn):
    claimed_agreed(conn)  # the agreement is already accepted
    st = setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v0.1"})
    assert st["step"] == "link"
    assert conn.execute("SELECT count(*) AS n FROM agreement_acceptance").fetchone()["n"] == 1


def test_choose_sets_the_way_and_audits_only_a_change(conn):
    claimed_agreed(conn, agree=False)
    st = setup.choose(config.load(), OWNER, "biz_1", "own_account")
    assert st["step"] == "accept" and audited(conn, "web.ads_mode") == []  # already the way: nothing changes
    st = setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert st["mode"] == "managed" and audited(conn, "web.ads_mode") == [{"ads_mode": "managed"}]
    with pytest.raises(setup.Refused) as e:
        setup.choose(config.load(), OWNER, "biz_1", "tiktok")
    assert e.value.status == 400


def test_a_draft_business_cannot_choose_or_accept(conn):
    draft_business(conn)
    with pytest.raises(setup.Refused, match="Claim this business first"):
        setup.choose(config.load(), OWNER, "biz_1", "managed")
    with pytest.raises(setup.Refused, match="Claim this business first"):
        setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v0.1"})


def test_choosing_prepaid_withdraws_a_pending_invitation_first(conn, monkeypatch, ads_api):
    pending_link(conn)
    calls = []
    monkeypatch.setattr(google_ads_link, "withdraw", lambda settings, link, http=None: calls.append(link.customer_id))
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: pytest.fail("read"))
    st = setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert calls == [CID] and st["mode"] == "managed" and st["step"] == "accept"
    assert google_ads_link.get(conn, "biz_1").status == "cancelled"
    assert audited(conn, "web.ads_link_withdraw") == [{"customer_id": "123-456-7890"}]


def test_in_manual_mode_the_pending_invitation_is_marked_withdrawn_without_calling_google(conn, monkeypatch):
    pending_link(conn)
    monkeypatch.setattr(google_ads_link, "withdraw", lambda *a, **k: pytest.fail("withdraw"))
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: pytest.fail("read"))
    assert setup.choose(config.load(), OWNER, "biz_1", "managed")["mode"] == "managed"
    assert google_ads_link.get(conn, "biz_1").status == "cancelled"
    assert audited(conn, "web.ads_link_withdraw") == [{"customer_id": "123-456-7890"}]


def test_a_withdrawal_google_refuses_changes_nothing(conn, monkeypatch, ads_api):
    pending_link(conn)

    def refuse(settings, link, http=None):
        raise google_ads_link.LinkError(google_ads_link.REFUSED)

    monkeypatch.setattr(google_ads_link, "withdraw", refuse)
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: google_ads_link.Remote("pending", RESOURCE))
    with pytest.raises(setup.Refused) as e:
        setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert e.value.status == 502
    assert google_ads_link.get(conn, "biz_1").status == "pending"
    assert conn.execute("SELECT ads_mode FROM business").fetchone()["ads_mode"] == "own_account"


def test_an_invitation_stored_without_a_name_is_read_and_withdrawn_by_the_name_google_shows(conn, monkeypatch, ads_api):
    pending_link(conn, cid="9876543210")  # send returned no name: the invitation was already open at Google
    calls, reads = [], []
    monkeypatch.setattr(google_ads_link, "withdraw", lambda settings, link, http=None: calls.append(link.resource_name))
    monkeypatch.setattr(
        google_ads_link,
        "read",
        lambda settings, cid, http=None: reads.append(cid) or google_ads_link.Remote("pending", RESOURCE),
    )
    st = setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert reads == ["9876543210"] and calls == [RESOURCE] and st["mode"] == "managed"
    assert google_ads_link.get(conn, "biz_1").status == "cancelled"
    assert audited(conn, "web.ads_link_withdraw") == [{"customer_id": "987-654-3210"}]


def test_an_invitation_without_a_name_that_google_shows_accepted_is_recorded_and_refused(conn, monkeypatch, ads_api):
    pending_link(conn, cid="9876543210")
    monkeypatch.setattr(google_ads_link, "withdraw", lambda *a, **k: pytest.fail("withdraw"))
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: google_ads_link.Remote("active", RESOURCE))
    with pytest.raises(setup.Refused, match="is linked to Admiry") as e:
        setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert e.value.status == 409
    assert conn.execute("SELECT ads_mode FROM business").fetchone()["ads_mode"] == "own_account"
    assert google_ads_link.get(conn, "biz_1").status == "active"
    assert audited(conn, "web.ads_link_withdraw") == [] and len(audited(conn, "web.ads_link_active")) == 1


def test_a_withdrawal_google_refuses_because_it_is_already_cancelled_lets_the_switch_through(
    conn, monkeypatch, ads_api
):
    pending_link(conn)

    def refuse(settings, link, http=None):
        raise google_ads_link.LinkError(google_ads_link.REFUSED)

    monkeypatch.setattr(google_ads_link, "withdraw", refuse)
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: google_ads_link.Remote("cancelled", RESOURCE))
    st = setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert st["mode"] == "managed"
    assert google_ads_link.get(conn, "biz_1").status == "cancelled"
    assert audited(conn, "web.ads_link_withdraw") == []


def test_a_google_read_failure_while_withdrawing_is_a_502_and_changes_nothing(conn, monkeypatch, ads_api):
    pending_link(conn, cid="9876543210")

    def down(settings, cid, http=None):
        raise google_ads_link.LinkError(google_ads_link.UNREACHABLE)

    monkeypatch.setattr(google_ads_link, "read", down)
    with pytest.raises(setup.Refused) as e:
        setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert e.value.status == 502 and str(e.value) == google_ads_link.UNREACHABLE
    assert google_ads_link.get(conn, "biz_1").status == "pending"
    assert conn.execute("SELECT ads_mode FROM business").fetchone()["ads_mode"] == "own_account"


def test_a_live_link_is_never_withdrawn(conn, monkeypatch):
    pending_link(conn)
    conn.execute("UPDATE ads_link SET status = 'active'")
    monkeypatch.setattr(google_ads_link, "withdraw", lambda *a, **k: pytest.fail("withdraw called"))
    with pytest.raises(setup.Refused, match="is linked to Admiry"):
        setup.choose(config.load(), OWNER, "biz_1", "managed")


def test_withdrawing_a_link_google_shows_active_calls_ads_conversions_ensure(conn, monkeypatch, ads_api):
    """A3 Task 2: the same became_active path that refuses the switch (LINK_IN_PLACE) also makes sure the
    account has its conversion actions, outside the transaction that recorded the activation. A stored link
    with no resource name (the invitation was already open at Google) makes `_withdraw_at_google` read instead
    of withdraw, matching test_an_invitation_without_a_name_that_google_shows_accepted_is_recorded_and_refused."""
    pending_link(conn, cid="9876543210")
    monkeypatch.setattr(google_ads_link, "withdraw", lambda *a, **k: pytest.fail("withdraw"))
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: google_ads_link.Remote("active", RESOURCE))
    calls = []
    monkeypatch.setattr(ads_conversions, "ensure", lambda conn_, settings, biz, cid, **kw: calls.append((biz, cid)))
    with pytest.raises(setup.Refused, match="is linked to Admiry"):
        setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert calls == [("biz_1", "9876543210")]


def test_withdrawing_a_link_that_google_cancels_does_not_call_ensure(conn, monkeypatch, ads_api):
    pending_link(conn)
    monkeypatch.setattr(google_ads_link, "withdraw", lambda settings, link, http=None: None)
    monkeypatch.setattr(ads_conversions, "ensure", lambda *a, **k: pytest.fail("ensure called"))
    st = setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert st["mode"] == "managed"


def test_withdrawing_a_link_logs_a_conversion_setup_failure_without_failing_the_switch(
    conn, monkeypatch, ads_api, caplog
):
    pending_link(conn, cid="9876543210")
    monkeypatch.setattr(google_ads_link, "withdraw", lambda *a, **k: pytest.fail("withdraw"))
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: google_ads_link.Remote("active", RESOURCE))

    def boom(conn_, settings, biz, cid, **kw):
        raise AdsError(BUSY)

    monkeypatch.setattr(ads_conversions, "ensure", boom)
    with pytest.raises(setup.Refused, match="is linked to Admiry"):  # unaffected by the conversions failure
        setup.choose(config.load(), OWNER, "biz_1", "managed")
    assert google_ads_link.get(conn, "biz_1").status == "active"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "biz_1" in logged and BUSY in logged


def test_leaving_prepaid_expires_unpaid_checkouts_first(conn, stripe):
    managed(conn)
    open_session(conn, session_id="cs_open_1")
    st = setup.choose(config.load(), OWNER, "biz_1", "own_account")
    assert st["mode"] == "own_account"
    assert [r.url.path for r in stripe.sent] == ["/v1/checkout/sessions/cs_open_1/expire"]
    assert conn.execute("SELECT status FROM payment_session").fetchone()["status"] == "expired"
    assert audited(conn, "web.pay_expire") == [{"expired": True}]


def test_a_checkout_older_than_a_day_is_expired_here_without_calling_stripe(conn, stripe):
    managed(conn)
    open_session(conn, session_id="cs_old")
    conn.execute("UPDATE payment_session SET created_at = now() - interval '25 hours'")
    st = setup.choose(config.load(), OWNER, "biz_1", "own_account")
    assert st["mode"] == "own_account" and stripe.sent == []
    assert conn.execute("SELECT status FROM payment_session").fetchone()["status"] == "expired"


def test_each_expire_attempt_uses_a_fresh_idempotency_key(conn, stripe):
    managed(conn)
    open_session(conn, session_id="cs_open_1")
    stripe.fail = httpx.Response(500, json={"error": {"message": "try again"}})
    for _ in range(2):
        with pytest.raises(setup.Refused):
            setup.choose(config.load(), OWNER, "biz_1", "own_account")
    keys = [r.headers["idempotency-key"] for r in stripe.sent]
    assert len(keys) >= 2 and len(set(keys)) == len(keys)


def test_leaving_prepaid_after_a_payment_is_refused_without_calling_stripe(conn, stripe):
    managed(conn)
    fund(conn)
    with pytest.raises(setup.Refused, match="Payments were made"):
        setup.choose(config.load(), OWNER, "biz_1", "own_account")
    assert stripe.sent == []


def test_a_checkout_that_completed_meanwhile_wins(conn, stripe):
    managed(conn)
    open_session(conn, session_id="cs_open_1")
    stripe.fail = httpx.Response(
        400, json={"error": {"message": "Only Checkout Sessions with a status in [open] can be expired."}}
    )
    with pytest.raises(setup.Refused) as e:
        setup.choose(config.load(), OWNER, "biz_1", "own_account")
    assert str(e.value) == setup.PAYMENT_MAY_LAND
    assert conn.execute("SELECT ads_mode FROM business").fetchone()["ads_mode"] == "managed"


def test_link_sends_the_invitation_and_records_it(conn, monkeypatch):
    claimed_agreed(conn)
    monkeypatch.setattr(google_ads_link, "send", lambda settings, cid, held=False, http=None: RESOURCE)
    st = setup.link(config.load(), OWNER, "biz_1", "123-456-7890")
    assert st["step"] == "link" and st["link"]["status"] == "pending" and st["link"]["customer_id"] == "123-456-7890"
    assert audited(conn, "web.ads_link_invite")[0]["status"] == "pending"


def test_link_refusals_are_owner_sentences(conn, monkeypatch):
    claimed_agreed(conn, agree=False)
    monkeypatch.setattr(google_ads_link, "send", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(setup.Refused, match="Management Agreement"):
        setup.link(config.load(), OWNER, "biz_1", "123-456-7890")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    with pytest.raises(setup.Refused) as e:
        setup.link(config.load(), OWNER, "biz_1", "12-34")
    assert e.value.status == 400


def test_a_google_failure_on_link_is_kept_on_the_row_and_is_a_502(conn, monkeypatch):
    pending_link(conn)

    def down(settings, cid, held=False, http=None):
        raise google_ads_link.LinkError(google_ads_link.UNREACHABLE)

    monkeypatch.setattr(google_ads_link, "send", down)
    with pytest.raises(setup.Refused) as e:
        setup.link(config.load(), OWNER, "biz_1", "987-654-3210")
    assert e.value.status == 502


def test_check_in_manual_mode_or_without_a_link_calls_nothing(conn, monkeypatch):
    claimed_agreed(conn)
    monkeypatch.setattr(google_ads_link, "read", lambda *a, **k: pytest.fail("read"))
    with pytest.raises(setup.Refused, match="Send the invitation first"):
        setup.check(config.load(), OWNER, "biz_1")
    google_ads_link.save(conn, "biz_1", CID, status="pending")
    with pytest.raises(setup.Refused) as e:
        setup.check(config.load(), OWNER, "biz_1")
    assert str(e.value) == setup.MANUAL_CHECK


def test_check_marks_the_link_active_and_verifies(conn, monkeypatch, ads_api):
    pending_link(conn)
    monkeypatch.setattr(
        google_ads_link, "read", lambda settings, cid, http=None: google_ads_link.Remote("active", RESOURCE)
    )
    st = setup.check(config.load(), OWNER, "biz_1")
    assert st["step"] == "done" and st["link"]["status"] == "active"
    assert audited(conn, "web.ads_link_active")[0]["verified_now"] is True


def test_check_is_limited_per_hour(conn, monkeypatch, ads_api):
    pending_link(conn)
    monkeypatch.setattr(
        google_ads_link, "read", lambda settings, cid, http=None: google_ads_link.Remote("pending", RESOURCE)
    )
    for _ in range(setup.CHECKS_PER_HOUR):
        setup.check(config.load(), OWNER, "biz_1")
    with pytest.raises(setup.Refused) as e:
        setup.check(config.load(), OWNER, "biz_1")
    assert e.value.status == 429


def test_check_that_becomes_active_calls_ads_conversions_ensure(conn, monkeypatch, ads_api):
    pending_link(conn)
    monkeypatch.setattr(
        google_ads_link, "read", lambda settings, cid, http=None: google_ads_link.Remote("active", RESOURCE)
    )
    calls = []
    monkeypatch.setattr(ads_conversions, "ensure", lambda conn_, settings, biz, cid, **kw: calls.append((biz, cid)))
    st = setup.check(config.load(), OWNER, "biz_1")
    assert st["step"] == "done"
    assert calls == [("biz_1", CID)]


def test_check_that_stays_pending_does_not_call_ensure(conn, monkeypatch, ads_api):
    pending_link(conn)
    monkeypatch.setattr(
        google_ads_link, "read", lambda settings, cid, http=None: google_ads_link.Remote("pending", RESOURCE)
    )
    monkeypatch.setattr(ads_conversions, "ensure", lambda *a, **k: pytest.fail("ensure called"))
    setup.check(config.load(), OWNER, "biz_1")


def test_check_logs_a_conversion_setup_failure_without_failing_the_request(conn, monkeypatch, ads_api, caplog):
    pending_link(conn)
    monkeypatch.setattr(
        google_ads_link, "read", lambda settings, cid, http=None: google_ads_link.Remote("active", RESOURCE)
    )

    def boom(conn_, settings, biz, cid, **kw):
        raise AdsError(BUSY)

    monkeypatch.setattr(ads_conversions, "ensure", boom)
    st = setup.check(config.load(), OWNER, "biz_1")  # does not raise despite the conversions failure
    assert st["step"] == "done" and st["link"]["status"] == "active"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "biz_1" in logged and BUSY in logged


def test_pay_starts_a_checkout_that_returns_to_the_conversation(conn, stripe):
    managed(conn)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    url = setup.pay(config.load(), OWNER, "biz_1", "300")
    assert url == "https://checkout.stripe.com/c/pay/cs_test_1"
    form = stripe.form()
    assert form["line_items[0][price_data][unit_amount]"] == "31020"
    assert form["success_url"] == "http://testserver/?setup=biz_1&paid=1"
    assert form["cancel_url"] == "http://testserver/?setup=biz_1&cancelled=1"
    assert conn.execute("SELECT id, status FROM payment_session").fetchone() == {"id": "cs_test_1", "status": "open"}
    assert audited(conn, "web.pay_start") == [{"session_id": "cs_test_1", "amount_usd": 300.0, "price_usd": 310.2}]


@pytest.mark.parametrize(
    ("prep", "amount", "text"),
    [
        ("own", "300", setup.NOT_MANAGED),
        ("terms", "300", setup.NEEDS_TERMS),
        ("ready", "50", "between"),
    ],
)
def test_pay_refusals(conn, stripe, prep, amount, text):
    if prep == "own":
        claimed_agreed(conn)
    else:
        managed(conn)
        if prep == "ready":
            agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
            agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    with pytest.raises(setup.Refused, match=text):
        setup.pay(config.load(), OWNER, "biz_1", amount)
    assert stripe.sent == []


def test_the_way_is_google_searchs_account_row_written_with_the_business(conn):
    """P6 (M0 Task 6): choose() writes the way to both places -- business.ads_mode and Google Search's
    channel_account row -- and state() reads the row (the column only while there is no row)."""
    claimed_agreed(conn, agree=False)
    s = config.load()
    assert setup.state(conn, s, "biz_1")["mode"] == "own_account"  # no row yet: the column's
    st = setup.choose(s, OWNER, "biz_1", "managed")
    assert st["mode"] == "managed" and business.row(conn, "biz_1")["ads_mode"] == "managed"
    assert registry.account(conn, "biz_1", "google_search").mode == "managed"
    conn.execute("UPDATE channel_account SET mode = 'own_account' WHERE channel = 'google_search'")
    assert setup.state(conn, s, "biz_1")["mode"] == "own_account"  # the row is the truth, not the column


def test_ready_to_launch_lists_one_step_per_planned_channel(conn, newsbreak_on):
    """Spec 2026-09-29 §4 item 2: a Google Search and NewsBreak plan shows the Customer ID step and NewsBreak's
    "nothing to connect", each done once its account is ready -- NewsBreak's, while not, with what it waits for (M1
    follow-up 2, W3: `reason`, here the funds); reading them opens no account row."""
    from agent.tests.test_ads_run_channels import add_plan_by_hand, funds, two_channels

    claimed_agreed(conn)
    s = config.load()
    assert setup.state(conn, s, "biz_1")["steps"] == []  # no plan yet: nothing to connect for
    add_plan_by_hand(conn, two_channels(), status="draft")
    google_step = {
        "channel": "google_search",
        "label": "Google Search",
        "text": google.CONNECT_OWN_ACCOUNT,
        "url": "http://testserver/?setup=biz_1",
        "done": False,
    }
    nb_step = {
        "channel": "newsbreak",
        "label": "NewsBreak",
        "text": "Nothing to connect: Admiry runs NewsBreak from your balance",
        "url": None,
        "done": False,
        # M1 Task 7: the picture is the plan's, and this version (built by hand) pinned none (fix round 1, item 4)
        "image": "This version of your plan has no picture for your NewsBreak ad, so NewsBreak waits for a new version",
    }
    waiting = {"reason": "Add funds to start NewsBreak"}
    assert setup.state(conn, s, "biz_1")["steps"] == [google_step, nb_step | waiting]
    google_ads_link.save(conn, "biz_1", CID, status="active")
    funds(conn)
    assert setup.state(conn, s, "biz_1")["steps"] == [google_step | {"done": True}, nb_step | {"done": True}]
    assert registry.account(conn, "biz_1", "newsbreak") is None


def test_ready_to_launch_says_what_newsbreak_waits_for_and_never_asks_for_money_it_has(conn, newsbreak_on):
    """M1 follow-up 2, W3 (found by the G1 session): NewsBreak's step never asks for money itself, and while NewsBreak
    is not ready it carries what it waits for (`reason`, as `get_connection_status`'s next step says it) -- with money
    on the balance and NewsBreak still getting the account ready (an operator's work), that, never "Add funds".
    Google Search's own account keeps its step as it was: its words and the link to follow, no `reason`."""
    from datetime import UTC, datetime

    from agent.tests.test_ads_run_channels import add_plan_by_hand, funds, two_channels

    claimed_agreed(conn)
    add_plan_by_hand(conn, two_channels(), status="draft")
    funds(conn)
    waiting = {"account_status": "PENDING", "account_checked_at": datetime.now(UTC).isoformat()}  # R11's wait
    registry.save_account(conn, "biz_1", "newsbreak", mode="managed", status="needs_attention", external=waiting)
    google_step, nb_step = setup.state(conn, config.load(), "biz_1")["steps"]
    assert (nb_step["text"], nb_step["done"], nb_step["reason"]) == (
        "Nothing to connect: Admiry runs NewsBreak from your balance",
        False,
        "NewsBreak is getting your account ready; your ads start once it is",
    )
    assert "funds" not in json.dumps(nb_step) and not JARGON.search(nb_step["reason"])
    assert (google_step["text"], google_step["url"]) == (google.CONNECT_OWN_ACCOUNT, "http://testserver/?setup=biz_1")
    assert "reason" not in google_step


def test_ready_to_launch_says_the_picture_on_the_plan_goes_with_newsbreaks_ad(
    conn, newsbreak_on, monkeypatch, tmp_path
):
    """Spec 2026-09-29 §4 item 2: NewsBreak needs a funded balance "and the image approved with the plan" (D89, M1 Task
    7) -- the step's own line, from the plan: the picture its card shows (the one its version pinned) is approved with
    it; one that left the set since is said instead (fix round 1, item 4)."""
    import json

    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import creative_set
    from agent.tests.test_ads_run_channels import add_plan_by_hand, two_channels

    root = creative_set(tmp_path / "creative")
    monkeypatch.setattr(creative, "ROOT", root)
    claimed_agreed(conn)
    secs = two_channels()
    secs["creative"]["proposed_image"] = {"newsbreak": "dentists/1.jpg"}
    add_plan_by_hand(conn, secs, status="draft")
    google_step, nb_step = setup.state(conn, config.load(), "biz_1")["steps"]
    assert "image" not in google_step  # Google Search shows no picture
    assert nb_step["image"] == "NewsBreak shows the picture on your plan with your ad; approving the plan approves it"
    assert not JARGON.search(nb_step["image"]) and not JARGON.search(nb_step["text"])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"] = [e for e in manifest["entries"] if e["file"] != "dentists/1.jpg"]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _google, nb_step = setup.state(conn, config.load(), "biz_1")["steps"]
    assert nb_step["image"] == (
        "The picture for your NewsBreak ad is no longer in Admiry's set: NewsBreak ads already running keep their "
        "current picture while a new version of your plan waits for a new one"
    )


def _report(*, terms: bool | None = True, phone: bool = True) -> dict:
    """A saved page check (the 14-rule report, these rules of it): the Terms link passed or only a warning (None, which
    leaves the report's own verdict passing), the phone -- a rule every channel needs -- passed or failed."""
    call_fix = "Put 650-555-0100 at the top as a tap-to-call link."
    return {
        "ok": phone,
        "url": "https://brightsmile.example/invisalign",
        "rules": [
            {"id": "reachable", "ok": True, "fix": None},
            {"id": "phone", "ok": phone, "fix": None if phone else call_fix},
            {"id": "terms", "ok": terms, "fix": None if terms else "Add a Terms link in the footer."},
            {"id": "contact", "ok": True, "fix": None},
        ],
    }


def test_ready_to_launch_says_the_page_fix_newsbreak_waits_for(conn, newsbreak_on):
    """M1 follow-up 2, W4 (found by the G1 session): NewsBreak needs the page check's Terms link and contact rules
    passed outright (`page_rules`), which the check reports as warnings that leave the page's own verdict passing --
    Google Search does not need them (D77) -- so the page reads as checked while NewsBreak waits. NewsBreak's step says
    the page's fix as its row would say it (`page`) until a saved check passes them; nothing while there is no saved
    check (the page step asks for one), and never on Google Search's step."""
    from psycopg.types.json import Jsonb

    from agent import db
    from agent.tests.test_ads_run_channels import add_plan_by_hand, funds, two_channels

    claimed_agreed(conn)
    add_plan_by_hand(conn, two_channels(), status="draft")
    funds(conn)
    page_id = db.new_id("pg")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status) VALUES (%s, 'biz_1', 'owner', %s, 'published')",
        (page_id, "https://brightsmile.example/invisalign"),
    )

    def checked(report: dict | None) -> list[dict]:
        conn.execute("UPDATE page SET check_json = %s WHERE id = %s", (Jsonb(report) if report else None, page_id))
        return setup.state(conn, config.load(), "biz_1")["steps"]

    assert all("page" not in s for s in checked(None))  # no saved check: the page step asks for one
    google_step, nb_step = checked(_report(terms=None))
    assert nb_step["page"] == (
        "Your page needs a fix before your ads can send people there: Add a Terms link in the footer."
    )
    assert nb_step["done"] and "page" not in google_step and not JARGON.search(nb_step["page"])
    google_step, nb_step = checked(_report(phone=False))  # a fix every channel needs: the one NewsBreak's row says
    assert nb_step["page"] == (
        "Your page needs a fix before your ads can send people there: Put 650-555-0100 at the top as a tap-to-call "
        "link."
    )
    assert "page" not in google_step
    assert all("page" not in s for s in checked(_report()))  # both rules passed: nothing waits on the page


def test_ready_to_launch_has_no_page_fix_for_ads_already_running(conn, newsbreak_on):
    """Fix round 1 (F1, the review's probe): the page gates building ads and turning them on the first time, never ads
    already built (ruling S8: `_launch_rules`). NewsBreak live, a later check that counts the Terms link only a warning
    holds nothing back -- its step carries no page fix (`ads_run.has_ads`), while a funded channel not built yet still
    does."""
    from psycopg.types.json import Jsonb

    from agent import db
    from agent.tests.test_ads_run_channels import add_plan_by_hand, funds, two_channels

    claimed_agreed(conn)
    plan_id = add_plan_by_hand(conn, two_channels(), status="approved")
    funds(conn)
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, 'biz_1', 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), "https://brightsmile.example/invisalign", Jsonb(_report(terms=None))),
    )
    _google, nb_step = setup.state(conn, config.load(), "biz_1")["steps"]
    assert nb_step["page"].endswith("Add a Terms link in the footer.")  # nothing built yet: the page waits
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, launched_at) "
        "VALUES ('biz_1', 'newsbreak', %s, 'live', %s, now())",
        (plan_id, Jsonb({"campaign": "1000002"})),
    )
    _google, nb_step = setup.state(conn, config.load(), "biz_1")["steps"]
    assert nb_step["done"] and "page" not in nb_step


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_setup_card_shows_the_steps_above_the_step_at_hand():
    steps = [
        {
            "channel": "google_search",
            "label": "Google Search",
            "text": "Give Admiry your ID",
            "url": "/x",
            "done": True,
        },
        {"channel": "newsbreak", "label": "NewsBreak", "text": "Nothing to connect", "url": None, "done": False},
    ]
    shown = _setup_card({"step": "link", "mode": "own_account", "link": None, "steps": steps})
    assert shown == [["done", "✓ Google Search", "Give Admiry your ID"], ["todo", "NewsBreak", "Nothing to connect"]]
    assert _setup_card({"step": "choose", "mode": "own_account", "link": None, "steps": steps}) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_setup_card_shows_a_steps_line_about_the_picture_under_it():
    """M1 Task 7: a channel that shows a picture with the ad has a line about it under its step."""
    steps = [
        {
            "channel": "newsbreak",
            "label": "NewsBreak",
            "text": "Nothing to connect",
            "url": None,
            "done": True,
            "image": "NewsBreak shows the picture on your plan with your ad; approving the plan approves it",
        }
    ]
    shown = _setup_card({"step": "pay", "mode": "managed", "link": None, "steps": steps})
    assert shown == [
        [
            "done",
            "✓ NewsBreak",
            "Nothing to connect",
            "NewsBreak shows the picture on your plan with your ad; approving the plan approves it",
        ]
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_setup_card_shows_what_a_step_waits_for_under_it():
    """M1 follow-up 2, W3: a step with nothing to connect that is not done yet says what its channel waits for under
    it, in the picture line's style; a step without one draws as it did."""
    steps = [
        {
            "channel": "google_search",
            "label": "Google Search",
            "text": "Give Admiry your ID",
            "url": "/x",
            "done": False,
        },
        {
            "channel": "newsbreak",
            "label": "NewsBreak",
            "text": "Nothing to connect",
            "url": None,
            "done": False,
            "reason": "NewsBreak is getting your account ready; your ads start once it is",
        },
    ]
    shown = _setup_card({"step": "pay", "mode": "managed", "link": None, "steps": steps})
    assert shown == [
        ["todo", "Google Search", "Give Admiry your ID"],
        [
            "todo",
            "NewsBreak",
            "Nothing to connect",
            "NewsBreak is getting your account ready; your ads start once it is",
        ],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_setup_card_shows_the_page_fix_a_step_waits_for_under_it():
    """M1 follow-up 2, W4: a step whose channel waits for a fix to the page says the fix under it -- where the
    picture's line is, in its style, before it -- done or not (the account is ready; the page is what waits)."""
    fix = "Your page needs a fix before your ads can send people there: Add a Terms link in the footer."
    picture = "NewsBreak shows the picture on your plan with your ad; approving the plan approves it"
    steps = [
        {
            "channel": "newsbreak",
            "label": "NewsBreak",
            "text": "Nothing to connect",
            "url": None,
            "done": True,
            "page": fix,
            "image": picture,
        }
    ]
    shown = _setup_card({"step": "done", "mode": "managed", "link": None, "balance_usd": 300, "steps": steps})
    assert shown == [["done", "✓ NewsBreak", "Nothing to connect", fix, picture]]


# setup.js under Node with a stub DOM: the state answers every call; prints the per-channel steps list the card drew
# (each item's class and texts), or null when it drew none.
SETUP_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const state = JSON.parse(process.argv[2]);
const node = (tag) => ({ tag, className: "", text: "", kids: [], style: {}, dataset: {},
  classList: { add() {}, remove() {}, toggle() {} },
  set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
  append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
  replaceChildren(...cs) { this.kids = cs; }, setAttribute() {}, addEventListener() {}, remove() {} });
Object.assign(global, { window: {}, document: { createElement: node, body: node("body") },
  location: { assign() {} }, fetch: async () => ({ json: async () => ({ ok: true, state }) }) });
eval(src);
const host = node("host");
window.AdmirySetup.run({ host, businessId: "biz_1", pick: null, paid: false, csrf: async () => "t" });
setTimeout(() => {
  const list = (host.kids[0].kids || []).find((k) => k.className === "setup-steps");
  console.log(JSON.stringify(list ? list.kids.map((li) => [li.className, ...li.kids.map((k) => k.text)]) : null));
}, 50);
"""


def _setup_card(st: dict, harness: str = SETUP_HARNESS):
    state = {"name": "Bright Smile Dental", "documents": [], "link_mode": "manual", "locked": None, **st}
    js = Path(__file__).parents[1] / "static" / "setup.js"
    done = subprocess.run(
        ["node", "-e", harness, str(js), json.dumps(state)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# The same stub DOM; prints every part of the card in order: a choice of ways as ["options", "<label> — <its line>"…],
# anything else as [its class, its text and its children's]
PARTS_HARNESS = (
    SETUP_HARNESS.split("setTimeout(")[0]
    + r"""setTimeout(() => {
  const said = host.kids[0].kids.map((k) => k.className === "options cards"
    ? ["options", ...k.kids.map((b) => b.kids.map((c) => c.text).join(" — "))]
    : [k.className, k.text + (k.kids || []).map((c) => c.text).join("")]);
  console.log(JSON.stringify(said));
}, 50);
"""
)
OWN_WAY = "I have a Google Ads account — Your own account; Google bills you directly."
PREPAID_WAY = "Admiry runs the ads, I prepay — No Google account needed. Spent on the ads at the platform's exact cost."


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_card_draws_only_the_ways_the_server_offers():
    """Walk fixes 1, final review B I3: setup.js draws the ways `state` names, in its own order -- Admiry running the
    ads first since the founder's ruling of 2026-10-07 -- and the server's sentence under them when the prepaid way is
    left out (text only); a state that names none draws both, as before. A business already prepaying keeps its row
    with Change."""
    choose = {"step": "choose", "mode": "own_account", "link": None, "steps": []}
    google_only = _setup_card({**choose, "ways": ["own_account"], "ways_note": setup.MODE_WAITS}, PARTS_HARNESS)
    assert google_only[1:] == [["options", OWN_WAY], ["note", setup.MODE_WAITS]]
    both = _setup_card({**choose, "ways": ["managed", "own_account"], "ways_note": None}, PARTS_HARNESS)
    assert both[1:] == [["options", PREPAID_WAY, OWN_WAY]] == _setup_card(choose, PARTS_HARNESS)[1:]
    prepaid = {"step": "accept", "mode": "managed", "link": None, "steps": [], "ways": ["own_account"]}
    assert _setup_card(prepaid, PARTS_HARNESS)[1] == ["setup-how", "Admiry runs the ads, I prepay · Change"]


def test_change_locks_only_what_choose_cannot_undo(conn):
    s = config.load()
    pending_link(conn)  # an invitation on its way: choose() withdraws it, so nothing is locked
    assert setup.state(conn, s, "biz_1")["locked"] is None
    conn.execute("UPDATE ads_link SET status = 'active'")  # a live link stays put
    live = setup.state(conn, s, "biz_1")["locked"]
    assert live and live.startswith("Your Google Ads account") and "ask Admiry" in live
    conn.execute("DELETE FROM ads_link")
    conn.execute("DELETE FROM business")
    managed(conn)
    open_session(conn)  # an unpaid Checkout: choose() expires it
    assert setup.state(conn, s, "biz_1")["locked"] is None
    fund(conn, session_id="cs_2", payment_intent="pi_2")  # money in
    assert setup.state(conn, s, "biz_1")["locked"].startswith("Payments were made")


# Ported from the removed route tests (test_web_ads.py, test_pay.py, 2026-09-29): what they asserted beyond the above.


def test_link_calls_google_with_no_database_connection_open_and_normalises_the_id(conn, monkeypatch, ads_api):
    claimed_agreed(conn)
    opened, seen = [], {}
    real = setup.db.connect

    def tracking(url=None):
        c = real(url)
        opened.append(c)
        return c

    def fake_send(settings, customer_id, *, held=False, http=None):
        seen.update(
            open=[c for c in opened if not c.closed], customer_id=customer_id, mode=settings.google_ads_link_mode
        )
        return RESOURCE

    monkeypatch.setattr(setup.db, "connect", tracking)
    monkeypatch.setattr(google_ads_link, "send", fake_send)
    setup.link(config.load(), OWNER, "biz_1", "123 456 7890")
    assert seen == {"open": [], "customer_id": CID, "mode": "api"}
    assert google_ads_link.get(conn, "biz_1").resource_name == RESOURCE
    assert audited(conn, "web.ads_link_invite") == [{"customer_id": "123-456-7890", "status": "pending", "mode": "api"}]


def test_bad_customer_ids_are_400_and_record_nothing(conn, monkeypatch):
    claimed_agreed(conn)
    monkeypatch.setattr(google_ads_link, "send", lambda *a, **k: pytest.fail("sent"))
    for bad in ("12345", "abc-def-ghij", "123-456-78901", 1234567890):
        with pytest.raises(setup.Refused, match="10 digits") as e:
            setup.link(config.load(), OWNER, "biz_1", bad)
        assert e.value.status == 400, bad
    assert google_ads_link.get(conn, "biz_1") is None and audited(conn, "web.ads_link_invite") == []


def test_a_customer_id_taken_by_another_business_is_409_and_nothing_is_sent(conn, monkeypatch):
    pending_link(conn, "biz_other", email="other@example.com")
    claimed_agreed(conn)
    monkeypatch.setattr(google_ads_link, "send", lambda *a, **k: pytest.fail("sent an invitation for a taken ID"))
    with pytest.raises(setup.Refused, match="already linked to another business") as e:
        setup.link(config.load(), OWNER, "biz_1", CID)
    assert e.value.status == 409 and "biz_other" not in str(e.value)
    assert google_ads_link.get(conn, "biz_1") is None and audited(conn, "web.ads_link_invite") == []


def test_a_released_claim_cannot_link(conn, monkeypatch):
    claimed_agreed(conn)
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL WHERE id = 'biz_1'")  # claim-expire
    monkeypatch.setattr(google_ads_link, "send", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(setup.Refused, match=google_ads_link.NOT_CLAIMED):
        setup.link(config.load(), OWNER, "biz_1", CID)
    assert conn.execute("SELECT count(*) AS n FROM ads_link").fetchone()["n"] == 0


def test_a_google_failure_on_link_keeps_the_old_status_stores_the_reason_and_audits_nothing(conn, monkeypatch, ads_api):
    claimed_agreed(conn)
    google_ads_link.save(conn, "biz_1", CID, status="refused")  # declined once; the owner sends again

    def failing(settings, customer_id, *, held=False, http=None):
        raise google_ads_link.LinkError(google_ads_link.ACCESS_TROUBLE)

    monkeypatch.setattr(google_ads_link, "send", failing)
    with pytest.raises(setup.Refused) as e:
        setup.link(config.load(), OWNER, "biz_1", CID)
    assert e.value.status == 502 and "needs attention" in str(e.value)
    assert not any(s in str(e.value) for s in SECRETS)
    link = google_ads_link.get(conn, "biz_1")
    assert (link.status, link.last_error) == ("refused", google_ads_link.ACCESS_TROUBLE)
    assert audited(conn, "web.ads_link_invite") == []


def test_an_account_already_managed_by_admiry_is_409_and_nothing_is_saved(conn, monkeypatch, ads_api):
    claimed_agreed(conn)
    monkeypatch.setattr(google_ads_link, "client", lambda: google_ads([], mutate=failure(400, MANAGED)))
    with pytest.raises(setup.Refused, match=google_ads_link.ALREADY_LINKED) as e:
        setup.link(config.load(), OWNER, "biz_1", CID)
    assert e.value.status == 409 and not any(s in str(e.value) for s in SECRETS)
    assert google_ads_link.get(conn, "biz_1") is None and audited(conn, "web.ads_link_invite") == []
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_this_businesss_own_link_already_managed_counts_as_sent(conn, monkeypatch, ads_api):
    claimed_agreed(conn)
    google_ads_link.save(conn, "biz_1", CID, status="inactive")  # the row went stale; Google still has it
    monkeypatch.setattr(google_ads_link, "client", lambda: google_ads([], mutate=failure(400, MANAGED)))
    setup.link(config.load(), OWNER, "biz_1", CID)
    link = google_ads_link.get(conn, "biz_1")
    assert (link.customer_id, link.status, link.last_error) == (CID, "pending", None)  # the next read settles it


def test_check_audits_what_it_read_and_the_cap_refusal_leaves_no_row(conn, monkeypatch, ads_api):
    pending_link(conn)
    calls = []

    def fake_read(settings, customer_id, *, http=None):
        calls.append(customer_id)
        return google_ads_link.Remote("pending", RESOURCE)

    monkeypatch.setattr(google_ads_link, "read", fake_read)
    for _ in range(setup.CHECKS_PER_HOUR):
        setup.check(config.load(), OWNER, "biz_1")
    with pytest.raises(setup.Refused, match="every hour"):
        setup.check(config.load(), OWNER, "biz_1")
    assert (
        len(calls) == 6
        and audited(conn, "web.ads_link_check") == [{"customer_id": "123-456-7890", "status": "pending"}] * 6
    )


def test_check_after_the_owner_unlinked_in_google_keeps_verified(conn, monkeypatch, ads_api):
    pending_link(conn)
    conn.execute("UPDATE ads_link SET status = 'active'")
    conn.execute(
        "UPDATE business SET claim_state = 'verified', verified_via = 'ads_link', verified_at = now() "
        "WHERE id = 'biz_1'"
    )
    monkeypatch.setattr(
        google_ads_link, "read", lambda settings, cid, http=None: google_ads_link.Remote("inactive", RESOURCE)
    )
    st = setup.check(config.load(), OWNER, "biz_1")
    assert st["link"]["status"] == "inactive" and st["step"] == "link"
    assert business.row(conn, "biz_1")["claim_state"] == "verified" and audited(conn, "web.ads_link_active") == []


def terms(conn):
    managed(conn)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)


def sessions_count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM payment_session").fetchone()["n"]


def test_pay_needs_both_documents_and_a_live_claim(conn, stripe):
    managed(conn)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)  # the agreement alone
    with pytest.raises(setup.Refused, match=setup.NEEDS_TERMS):
        setup.pay(config.load(), OWNER, "biz_1", "500")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL")  # claim-expire
    with pytest.raises(setup.Refused, match="Claim this business first"):
        setup.pay(config.load(), OWNER, "biz_1", "500")
    assert stripe.sent == [] and sessions_count(conn) == 0


@pytest.mark.parametrize("amount", ["12.345", "abc", "", None, "100000"])
def test_pay_amounts_not_in_dollars_or_out_of_bounds_are_400(conn, stripe, amount):
    terms(conn)
    with pytest.raises(setup.Refused) as e:
        setup.pay(config.load(), OWNER, "biz_1", amount)
    assert e.value.status == 400 and stripe.sent == [] and sessions_count(conn) == 0


def test_pay_bounds_follow_the_settings(conn, stripe, monkeypatch):
    monkeypatch.setenv("AGENT_TOPUP_MIN_USD", "300")
    monkeypatch.setenv("AGENT_TOPUP_MAX_USD", "600")
    terms(conn)
    with pytest.raises(setup.Refused, match="between"):
        setup.pay(config.load(), OWNER, "biz_1", "250")
    assert setup.pay(config.load(), OWNER, "biz_1", "600").startswith("https://checkout.stripe.com/")


def test_pay_without_stripe_keys_is_503(conn, stripe, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    terms(conn)
    with pytest.raises(setup.Refused, match=setup.NOT_SET_UP) as e:
        setup.pay(config.load(), OWNER, "biz_1", "500")
    assert e.value.status == 503 and stripe.sent == []


def test_a_stripe_failure_is_a_readable_502_and_leaves_nothing(conn, stripe, caplog):
    terms(conn)
    stripe.fail = httpx.Response(500, text="oops")
    with pytest.raises(setup.Refused) as e:
        setup.pay(config.load(), OWNER, "biz_1", "500")
    assert e.value.status == 502 and "nothing was charged" in str(e.value) and "Stripe answered" not in str(e.value)
    assert "Stripe checkout for business biz_1 failed: Stripe answered 500" in caplog.text  # for the operator
    assert "sk_test_x" not in caplog.text
    assert sessions_count(conn) == 0 and audited(conn, "web.pay_start") == []


def test_payment_starts_are_capped_per_business_per_hour(conn, stripe):
    terms(conn)
    for i in range(setup.PAY_STARTS_PER_HOUR):
        open_session(conn, session_id=f"cs_old_{i}")
    with pytest.raises(setup.Refused, match="try again later") as e:
        setup.pay(config.load(), OWNER, "biz_1", "500")
    assert e.value.status == 429 and stripe.sent == []


def test_a_business_switched_away_while_the_checkout_started_is_not_sent_to_stripe(conn, stripe, monkeypatch):
    terms(conn)
    real = stripe_client.create_checkout

    def switched_meanwhile(*args, **kwargs):
        conn.execute("UPDATE business SET ads_mode = 'own_account' WHERE id = 'biz_1'")  # the owner's other tab
        return real(*args, **kwargs)

    monkeypatch.setattr(stripe_client, "create_checkout", switched_meanwhile)
    with pytest.raises(setup.Refused, match=setup.NOT_MANAGED):
        setup.pay(config.load(), OWNER, "biz_1", "500")
    row = conn.execute("SELECT id, status FROM payment_session").fetchone()
    assert dict(row) == {"id": "cs_test_1", "status": "expired"}  # can never be paid: its URL never left the server
    stopped = conn.execute("SELECT actor, result FROM agent_action WHERE tool = 'web.pay_stopped'").fetchall()
    assert len(stopped) == 1 and stopped[0]["actor"] == "web:owner@example.com"
    assert stopped[0]["result"] == {"session_id": "cs_test_1", "amount_usd": 500.0, "price_usd": 516.8}
    assert audited(conn, "web.pay_start") == []
    assert ads_mode.locked(conn, "biz_1", "managed") is None  # the stopped session does not lock it


def test_the_checkout_carries_the_business_the_owner_and_the_name_and_its_url_is_never_stored(conn, stripe):
    terms(conn)
    setup.pay(config.load(), OWNER, "biz_1", "500")
    sent = stripe.form()
    assert sent["mode"] == "payment" and sent["client_reference_id"] == "biz_1"
    assert sent["metadata[business_id]"] == "biz_1" and sent["customer_email"] == "owner@example.com"
    assert sent["line_items[0][price_data][product_data][name]"] == "Advertising balance for Bright Smile Dental"
    actor = conn.execute("SELECT actor FROM agent_action WHERE tool = 'web.pay_start'").fetchone()["actor"]
    assert actor == "web:owner@example.com"
    assert "checkout.stripe.com" not in str(conn.execute("SELECT args_redacted, result FROM agent_action").fetchall())


# ---------------------------------------------------------------- the pay form shows both prices (M1m-1 Task 4)

QUOTE_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const cases = JSON.parse(process.argv[2]);
Object.assign(global, { window: {}, document: { createElement: () => ({}) } });
eval(src);
console.log(JSON.stringify(cases.map(([c, r, rates]) => window.AdmirySetup.quoteCents(c, r, rates))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_pay_form_prices_match_the_server():
    """Review Focus 4: the form's arithmetic is wallet.quote's, to the cent, for both rails."""
    s = config.load()
    rates = {
        "card_bps": s.card_cost_bps,
        "card_fixed_cents": s.card_cost_fixed_cents,
        "ach_bps": s.ach_cost_bps,
        "ach_cap_cents": s.ach_cost_cap_cents,
    }
    credits = [20000, 25055, 33333, 62499, 62500, 62501, 100000, 123456, 500000]
    cases = [[c, r, rates] for c in credits for r in ("card", "ach")]
    js = Path(__file__).parents[1] / "static" / "setup.js"
    done = subprocess.run(
        ["node", "-e", QUOTE_HARNESS, str(js), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [wallet.quote(c, r, s).processing_cents for c, r, _ in cases]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_pay_form_shows_the_card_price_and_the_bank_saving():
    topup = {
        "suggested_usd": 1000,
        "min_usd": 200,
        "max_usd": 5000,
        "daily_usd": None,
        "rails": ["card", "ach"],
        "rates": {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500},
    }
    said = _setup_card(
        {
            "step": "pay",
            "mode": "managed",
            "link": None,
            "steps": [],
            "payments_ready": True,
            "topup": topup,
            "balance_usd": None,
            "transfer_pending_usd": None,
        },
        PARTS_HARNESS,
    )
    # ensure_ascii=False: a plain json.dumps backslash-escapes the middle dot, which would never match the
    # literal one below (P8's copy uses one, as "setup-how · Change" already does elsewhere on this card)
    text = json.dumps(said, ensure_ascii=False)
    assert "Pay $1,033.30 by card" in text and "includes a $33.30 transaction fee" in text
    assert "Pay $1,005.00 by bank transfer" in text and "save $28.30 · arrives in about 4 business days" in text
    assert "Admiry adds no fee: the transaction fee is what Stripe charges to process the payment" in text
    assert "surcharge" not in text.lower() and "+ $" not in text  # P8: never "amount + fee" for the card


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_pay_form_offers_only_the_card_while_bank_transfers_are_off():
    topup = {
        "suggested_usd": 500,
        "min_usd": 200,
        "max_usd": 5000,
        "daily_usd": None,
        "rails": ["card"],
        "rates": {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500},
    }
    text = json.dumps(
        _setup_card(
            {
                "step": "pay",
                "mode": "managed",
                "link": None,
                "steps": [],
                "payments_ready": True,
                "topup": topup,
                "transfer_pending_usd": None,
            },
            PARTS_HARNESS,
        )
    )
    assert "Pay $516.80 by card" in text and "includes a $16.80 transaction fee" in text
    assert "bank transfer" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_the_pay_form_says_a_bank_transfer_is_on_its_way():
    topup = {
        "suggested_usd": 500,
        "min_usd": 200,
        "max_usd": 5000,
        "daily_usd": None,
        "rails": ["card", "ach"],
        "rates": {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500},
    }
    text = json.dumps(
        _setup_card(
            {
                "step": "pay",
                "mode": "managed",
                "link": None,
                "steps": [],
                "payments_ready": True,
                "topup": topup,
                "transfer_pending_usd": 1000,
            },
            PARTS_HARNESS,
        )
    )
    # finding 5: the sentence names the credit ($1,000.00), never "a bank transfer of $1,000.00" -- the debit
    # itself (what Stripe takes from the bank account) is a different, larger amount
    assert "$1,000.00 is on its way to your balance by bank transfer" in text
    assert "A bank transfer of $1,000.00" not in text


def test_state_carries_the_rates_the_rails_and_a_transfer_on_its_way(client, conn, stripe, google, monkeypatch):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    managed_owner(client, conn, google)
    setup.pay(config.load(), OWNER, "biz_1", "1000", "ach")
    conn.execute("UPDATE payment_session SET status = 'processing'")
    st = setup.state(conn, config.load(), "biz_1")
    assert st["topup"]["rails"] == ["card", "ach"]
    assert st["topup"]["rates"] == {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500}
    assert st["transfer_pending_usd"] == 1000.0


# -------------------------------------------- the pay-form poll tells open from processing apart (finding 1)

# A stub DOM (as SETUP_HARNESS's) with `fetch` counting its calls and a `setTimeout` that keeps the module's
# 3-second retry delay in order but shrinks it to 0ms (the harness's own short wait at the end is left alone) --
# real timers, in the right order, just fast enough for a test.
WAIT_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const state = JSON.parse(process.argv[2]);
const node = (tag) => ({ tag, className: "", text: "", kids: [], style: {}, dataset: {},
  classList: { add() {}, remove() {}, toggle() {} },
  set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
  append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
  replaceChildren(...cs) { this.kids = cs; }, setAttribute() {}, addEventListener() {}, remove() {} });
let calls = 0;
const real = setTimeout;
Object.assign(global, { window: {}, document: { createElement: node, body: node("body") },
  location: { assign() {} },
  setTimeout: (fn, ms) => real(fn, ms >= 1000 ? 0 : ms),
  fetch: async () => { calls++; return { json: async () => ({ ok: true, state }) }; } });
eval(src);
const host = node("host");
window.AdmirySetup.run({ host, businessId: "biz_1", pick: null, paid: true, csrf: async () => "t" });
setTimeout(() => {
  const note = host.kids[0].kids.some((k) => k.className === "note setup-note-live");
  console.log(JSON.stringify({ calls, note }));
}, 300);
"""


def _waited(state: dict) -> dict:
    full = {
        "name": "Bright Smile Dental",
        "documents": [],
        "link_mode": "manual",
        "locked": None,
        "mode": "managed",
        "step": "done",
        "steps": [],
        "paused": False,
        **state,
    }
    js = Path(__file__).parents[1] / "static" / "setup.js"
    done = subprocess.run(
        ["node", "-e", WAIT_HARNESS, str(js), json.dumps(full)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_a_pending_transfer_plus_an_open_card_session_keeps_waiting():
    """Finding 1: `landed()` used to go true the moment ANY bank transfer is 'processing', so an owner returning
    from a CARD payment got no poll and no note while their own session was merely 'open'. `open_topups` tells
    the two apart: with one still open, the poll keeps going for all 20 retries (21 state calls total) and the
    note stays shown."""
    out = _waited({"balance_usd": None, "transfer_pending_usd": 1000, "open_topups": 1})
    assert out == {"calls": 21, "note": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_a_pending_transfer_alone_stops_waiting():
    out = _waited({"balance_usd": None, "transfer_pending_usd": 1000, "open_topups": 0})
    assert out == {"calls": 1, "note": False}


# ---------------------------------------------------------------- a price button validates first (fix round 1, R5)

# A stub DOM that actually keeps the listeners it is given (SETUP_HARNESS's `addEventListener() {}` throws every
# listener away, so it cannot simulate a click) and a stub `input.reportValidity()` the test controls; `fetch`
# records every call's parsed body so the test can tell a "pay" call happened from one that did not.
CLICK_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const state = JSON.parse(process.argv[2]);
const which = process.argv[3]; // "card" or "ach": which price button to click
const valid = process.argv[4] === "valid";
const calls = [];
const node = (tag) => ({ tag, className: "", text: "", kids: [], style: {}, dataset: {}, listeners: {},
  classList: { add() {}, remove() {}, toggle() {} },
  set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
  append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
  replaceChildren(...cs) { this.kids = cs; }, setAttribute() {}, remove() {},
  addEventListener(ev, fn) { this.listeners[ev] = fn; },
  reportValidity() { return valid; } });
Object.assign(global, { window: {}, document: { createElement: node, body: node("body") },
  location: { assign() {} },
  fetch: async (url, opts) => { calls.push(JSON.parse(opts.body)); return { json: async () => ({ ok: true, state }) }; } });
eval(src);
const host = node("host");
window.AdmirySetup.run({ host, businessId: "biz_1", pick: null, paid: false, csrf: async () => "t" });
setTimeout(() => {
  const prices = host.kids[0].kids.find((k) => k.className === "setup-prices");
  const needle = which === "ach" ? "by bank transfer" : "by card";
  const btn = prices.kids.find((k) => k.tag === "button" && k.text.includes(needle));
  btn.listeners.click();
  setTimeout(() => console.log(JSON.stringify(calls.filter((c) => c.action === "pay"))), 20);
}, 50);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js with node")
def test_a_price_button_checks_the_amount_like_the_form_does_before_it_opens_stripe():
    """Fix round 1, R5: a price button sits beside the form, not inside it, so a click used to skip the browser's
    native min/max check that the Enter-key path (a real form submit) still gets; it must run the same check
    first, same as if the amount were typed and Enter pressed on an out-of-range value."""
    topup = {
        "suggested_usd": 1000,
        "min_usd": 200,
        "max_usd": 5000,
        "daily_usd": None,
        "rails": ["card", "ach"],
        "rates": {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500},
    }
    state = {
        "name": "Bright Smile Dental",
        "step": "pay",
        "mode": "managed",
        "link": None,
        "documents": [],
        "link_mode": "manual",
        "locked": None,
        "steps": [],
        "payments_ready": True,
        "topup": topup,
        "balance_usd": None,
        "transfer_pending_usd": None,
    }
    js = Path(__file__).parents[1] / "static" / "setup.js"

    def clicked(which: str, validity: str) -> list:
        done = subprocess.run(
            ["node", "-e", CLICK_HARNESS, str(js), json.dumps(state), which, validity],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    assert clicked("card", "invalid") == []  # the native check refuses first: no pay call reaches the server
    paid = clicked("ach", "valid")
    assert len(paid) == 1 and paid[0]["rail"] == "ach" and paid[0]["amount_usd"] == "1000"
