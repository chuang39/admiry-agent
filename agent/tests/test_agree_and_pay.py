"""L1 Task 3 (spec 2026-10-10 §5): the setup card has no review step; the owner's yes is the plan card's (setup.approve,
refused while the checklist blocks) and Agree and pay names the plan on its Checkout."""

import pytest
from psycopg.types.json import Jsonb

from agent import ads_run, agreements, config, launch_check, legal, plans, proposals, setup, wallet
from agent.tests.test_ads_run import BIZ, _knowledge, _prepaid, seed
from agent.tests.test_review_launch import OWNER, draft_sections
from agent.tests.test_stripe_hook import deliver, event


def _owner() -> setup.Owner:
    return setup.Owner(**OWNER)


def test_approve_is_refused_while_the_list_blocks_and_approves_nothing(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    seed(conn, status="draft", secs=draft_sections())
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    _knowledge(conn, phone=None)
    plan_id = plans.latest(conn, BIZ)["id"]
    with pytest.raises(setup.Refused, match="What number should the calls ring"):
        setup.approve(config.load(), _owner(), BIZ, plan_id)
    assert plans.latest(conn, BIZ)["status"] == plans.DRAFT


def test_approve_names_the_plan_the_card_shows(conn):
    seed(conn, status="draft", secs=draft_sections())
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    with pytest.raises(setup.Refused, match=setup.PLAN_CHANGED):
        setup.approve(config.load(), _owner(), BIZ, "pl_0000000000000000")


def test_nothing_waits_for_a_yes_when_the_account_step_is_not_done(conn):
    seed(conn, status="draft", secs=draft_sections(), link=None)  # an own-account business with no live link
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    with pytest.raises(setup.Refused, match=setup.NOTHING_TO_REVIEW):
        setup.approve(config.load(), _owner(), BIZ, plans.latest(conn, BIZ)["id"])


def test_agree_and_pay_names_the_plan_on_the_checkout_and_its_session(conn, stripe):
    seed(conn, status="draft", secs=draft_sections())
    _prepaid(conn)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    plan_id = plans.latest(conn, BIZ)["id"]
    setup.pay(config.load(), _owner(), BIZ, "300", plan_id=plan_id)
    assert stripe.form()["metadata[plan_id]"] == plan_id
    assert conn.execute("SELECT plan_id FROM payment_session").fetchone()["plan_id"] == plan_id


def test_agree_and_pay_is_refused_for_a_plan_with_a_problem_left_and_starts_no_checkout(conn, stripe):
    seed(conn, status="draft", secs=draft_sections())
    _prepaid(conn)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    _knowledge(conn, phone=None)
    with pytest.raises(setup.Refused, match="What number should the calls ring"):
        setup.pay(config.load(), _owner(), BIZ, "300", plan_id=plans.latest(conn, BIZ)["id"])
    assert stripe.sent == []
    assert launch_check.blocked(conn, config.load(), BIZ, plans.latest(conn, BIZ)["id"]) == ads_run.NO_PHONE


def test_add_funds_names_no_plan(conn, stripe):
    seed(conn, status="draft", secs=draft_sections())
    _prepaid(conn)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    setup.pay(config.load(), _owner(), BIZ, "300")
    assert "metadata[plan_id]" not in stripe.form()
    assert conn.execute("SELECT plan_id FROM payment_session").fetchone()["plan_id"] is None


# ---------------------------------------------------------------- Agree and pay approves its plan (L1 Task 8)
def _agreed_and_paying(conn) -> str:
    seed(conn, status="draft", secs=draft_sections())
    _prepaid(conn)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    plan_id = plans.latest(conn, BIZ)["id"]
    setup.pay(config.load(), _owner(), BIZ, "300", plan_id=plan_id)  # cs_test_1, $310.20
    return plan_id


def test_the_money_for_a_plan_sends_it_to_review(client, conn, stripe, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    plan_id = _agreed_and_paying(conn)
    r = deliver(client, event(amount=31020))
    assert r.json()["credited"] is True
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW
    row = conn.execute("SELECT approval_kind, actor FROM agent_action WHERE tool = 'web.approve_plan'").fetchone()
    assert (row["approval_kind"], row["actor"]) == ("paid", "web:owner@example.com")


def test_money_for_a_plan_that_changed_meanwhile_approves_nothing(client, conn, stripe, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    plan_id = _agreed_and_paying(conn)
    plans.create_version(conn, BIZ, goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[])
    assert deliver(client, event(amount=31020)).json()["credited"] is True
    assert wallet.balance_cents(conn, BIZ) == 50_000 + 30_000
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.SUPERSEDED
    assert plans.latest(conn, BIZ)["status"] == plans.DRAFT
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.approve_plan'").fetchone()["n"] == 0


def test_with_the_review_off_the_money_approves_nothing(client, conn, stripe):
    plan_id = _agreed_and_paying(conn)
    assert deliver(client, event(amount=31020)).json()["credited"] is True
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.DRAFT


def test_a_refused_approval_never_undoes_the_money(client, conn, stripe, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    _agreed_and_paying(conn)

    def refuse(*a, **k):
        raise proposals.ToolError("refused")

    monkeypatch.setattr(proposals, "approve_on_web", refuse)
    assert deliver(client, event(amount=31020)).json()["credited"] is True
    assert wallet.balance_cents(conn, BIZ) == 80_000


# ---------------------------------------------------------------- no money before the page (founder 2026-10-13)
def _prepaid_draft(conn, *, page: bool) -> str:
    """A prepaid business with both documents accepted and a draft that funds Google Search -- its page published
    and checked, or none -- and no money in yet. Returns the plan id."""
    seed(conn, status="draft", secs=draft_sections(), page=page)
    _prepaid(conn)
    conn.execute("DELETE FROM wallet_entry")
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    return plans.latest(conn, BIZ)["id"]


@pytest.mark.parametrize("named", [True, False])  # Agree and pay (the plan named), and Add funds (none)
def test_no_checkout_starts_while_the_latest_drafts_page_is_not_settled(conn, stripe, named):
    plan_id = _prepaid_draft(conn, page=False)
    assert launch_check.page_first(conn, BIZ)
    with pytest.raises(setup.Refused, match=setup.PAGE_FIRST):
        setup.pay(config.load(), _owner(), BIZ, "300", plan_id=plan_id if named else None)
    assert stripe.sent == [] and conn.execute("SELECT count(*) AS n FROM payment_session").fetchone()["n"] == 0


@pytest.mark.parametrize("named", [True, False])
def test_with_the_page_settled_the_money_goes_through(conn, stripe, named):
    plan_id = _prepaid_draft(conn, page=True)
    url = setup.pay(config.load(), _owner(), BIZ, "300", plan_id=plan_id if named else None)
    assert url.startswith("https://checkout.stripe.com/") and len(stripe.sent) == 1


def test_a_top_up_for_a_business_already_running_is_never_refused_for_the_page(conn, stripe):
    """Its ads built on its live page and a newer draft waiting: adding funds is never stopped for the page."""
    seed(conn, status="approved", secs=draft_sections())
    _prepaid(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES (%s, 'google_search', %s, 'live', %s)",
        (BIZ, plans.latest(conn, BIZ)["id"], Jsonb({"campaign": "customers/1/campaigns/2"})),
    )
    plans.create_version(conn, BIZ, goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[])
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    assert not launch_check.page_first(conn, BIZ)
    assert setup.pay(config.load(), _owner(), BIZ, "300").startswith("https://checkout.stripe.com/")


def running_with_a_new_place(conn) -> tuple[str, str]:
    """A prepaid business whose Google Search ads are built and running, its page taken down (the address stays on
    file), with both documents accepted and a newer draft that adds NewsBreak, which has no page yet: the latest
    draft's page is not settled. Returns (key, the draft's id)."""
    key, _plan_id = seed(conn, status="approved", secs=draft_sections())
    _prepaid(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES (%s, 'google_search', %s, 'live', %s)",
        (BIZ, plans.latest(conn, BIZ)["id"], Jsonb({"campaign": "customers/1/campaigns/2"})),
    )
    conn.execute("UPDATE page SET status = 'unpublished'")
    secs = draft_sections()
    secs["ads"]["channels"].append({"channel": "newsbreak", "monthly_usd": 300.0})
    plans.create_version(conn, BIZ, goal_id=None, sections=secs, estimate_basis={}, rationale="", notes=[])
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    return key, plans.latest(conn, BIZ)["id"]


def test_a_top_up_while_ads_are_built_is_never_refused_for_a_new_drafts_page(conn, stripe):
    """Fix round 2b, item 4: Google Search's ads are built and spend the balance, the page is down, and a newer draft
    adds a place with no page -- the page comes first for that draft (launch_check.page_first), but Add funds (no plan
    named: a top-up) still goes to Stripe; the draft's own payment (Agree and pay, the plan named) waits for the page."""
    _key, plan_id = running_with_a_new_place(conn)
    assert launch_check.page_first(conn, BIZ) and ads_run.page_rows(conn, BIZ)
    assert setup.pay(config.load(), _owner(), BIZ, "300").startswith("https://checkout.stripe.com/")
    with pytest.raises(setup.Refused, match=setup.PAGE_FIRST):
        setup.pay(config.load(), _owner(), BIZ, "300", plan_id=plan_id)
    assert len(stripe.sent) == 1


def test_the_first_payments_plan_asks_the_page_by_the_one_predicate(conn, monkeypatch):
    """Fix round 2b, item 6: "a draft whose page is settled" is launch_check.page_first's to say, with the plan
    first_payment_plan already read -- never spelled out again."""
    plan_id = _prepaid_draft(conn, page=True)
    asked: list[str | None] = []
    real = launch_check.page_first

    def page_first(c, biz, plan=None):
        asked.append(plan["id"] if plan is not None else None)
        return real(c, biz, plan)

    monkeypatch.setattr(launch_check, "page_first", page_first)
    assert setup.first_payment_plan(conn, BIZ) == plan_id and asked == [plan_id]
    conn.execute("DELETE FROM page")
    asked.clear()
    assert setup.first_payment_plan(conn, BIZ) is None and asked == [plan_id]
