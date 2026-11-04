"""Balance tools and the balance on Settings (/connect), whoami and /plan (spec 2026-09-25 rev. 2 §3; rev. 1 §2):
prepaid businesses only, read-only, plain words."""

import json
from datetime import date

import pytest

from agent import ads_mode, agreements, config, envelope, google_ads_link, launch_check, legal, setup, wallet
from agent.connect import tool_docs
from agent.mcp_app import REGISTRY
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, call_tool, tool_text, without_site_header
from agent.tests.fixtures import business_with_key
from agent.tests.test_managed_mode import OWNER, choose
from agent.tests.test_wallet import fund, open_session
from agent.tests.test_web import claimed_business

NOT_MANAGED = (
    "this business runs its ads in the owner's own Google Ads account and pays Google directly, so it has no Admiry "
    "balance"
)


@pytest.fixture
def payments(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")


def prepaid_key(conn, *, terms=True, **kw) -> str:
    """biz_1 claimed by owner@example.com and prepaid (its terms accepted when `terms`), and a key for it."""
    key = business_with_key(conn, **{"claim_state": "claimed", **kw})
    conn.execute("UPDATE business SET ads_mode = 'managed', owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    if terms:
        for document in (legal.AGREEMENT, legal.ADDENDUM):
            agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=document)
    return key


def test_get_balance_reports_the_balance_what_is_pending_and_verification(client, conn, payments):
    key = prepaid_key(conn)
    fund(conn, cents=50000)  # verifies it
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=1234)
    open_session(conn, session_id="cs_2", cents=20000)
    res = call_tool(client, "get_balance", {}, key=key)
    assert not res["isError"], tool_text(res)
    sc = res["structuredContent"]["data"]
    assert (sc["balance_usd"], sc["pending_topups"], sc["verified"], sc["paused"]) == (487.66, 1, True, False)
    assert sc["last_topup_at"] and "note" not in sc


def test_get_balance_of_a_new_business_is_zero_and_says_when_payments_are_off(client, conn, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    key = prepaid_key(conn, scopes=("read",))
    sc = call_tool(client, "get_balance", {}, key=key)["structuredContent"]["data"]
    assert sc == {
        "balance_usd": 0.0,
        "pending_topups": 0,
        "last_topup_at": None,
        "verified": False,
        "paused": False,
        "note": "payments are not set up on this server yet",
    }


def test_request_payment_link_opens_the_conversation_at_the_payment_step(client, conn, payments):
    key = prepaid_key(conn)
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert sc == {
        "pay_url": "http://testserver/?setup=biz_1",
        "min_usd": 200,
        "max_usd": 5000,
        "prices": {"amount_usd": 200.0, "card_usd": 206.9},
        "price_note": (
            "Add $200.00: $206.90 by card, which includes a $6.90 transaction fee. Admiry adds no fee: the "
            "transaction fee is what Stripe charges to process the payment, and it is not refunded."
        ),
    }
    sc = call_tool(client, "request_payment_link", {"amount_usd": 250.5}, key=key)["structuredContent"]["data"]
    assert sc["pay_url"] == "http://testserver/?setup=biz_1"  # the conversation suggests its own amount


def test_the_payment_link_carries_both_prices_for_the_amount(client, conn, payments, monkeypatch):
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    key = prepaid_key(conn)
    sc = call_tool(client, "request_payment_link", {"amount_usd": 1000}, key=key)["structuredContent"]["data"]
    assert sc["prices"] == {"amount_usd": 1000.0, "card_usd": 1033.3, "bank_usd": 1005.0}
    assert sc["price_note"] == (
        "Add $1,000.00: $1,033.30 by card, which includes a $33.30 transaction fee; or $1,005.00 by bank transfer, "
        "$28.30 less, arriving in about 4 business days. Admiry adds no fee: the transaction fee is what Stripe "
        "charges to process the payment, and it is not refunded."
    )
    assert not MONEY_JARGON.search(sc["price_note"])


def test_without_an_amount_the_prices_are_for_the_suggested_one(client, conn, payments):
    key = prepaid_key(conn)
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert sc["prices"] == {"amount_usd": 200.0, "card_usd": 206.9}  # no plan: the minimum; ACH off by default
    assert sc["price_note"] == (
        "Add $200.00: $206.90 by card, which includes a $6.90 transaction fee. Admiry adds no fee: the transaction "
        "fee is what Stripe charges to process the payment, and it is not refunded."
    )


def test_get_balance_names_a_bank_transfer_on_its_way(client, conn, payments):
    key = prepaid_key(conn)
    conn.execute(
        "INSERT INTO payment_session (id, business_id, amount_cents, credited_cents, processing_cents, rail, status) "
        "VALUES ('cs_b', 'biz_1', 100500, 100000, 500, 'ach', 'processing')"
    )
    sc = call_tool(client, "get_balance", {}, key=key)["structuredContent"]["data"]
    assert (sc["pending_topups"], sc["transfer_pending_usd"]) == (1, 1000.0)


@pytest.mark.parametrize("amount", [-50, 0, 199.99, 5000.01])
def test_request_payment_link_refuses_amounts_outside_the_bounds(client, conn, payments, amount):
    key = prepaid_key(conn)
    res = call_tool(client, "request_payment_link", {"amount_usd": amount}, key=key)
    assert res["isError"] and "between $200 and $5,000" in tool_text(res)


def test_request_payment_link_says_what_comes_first(client, conn, payments, monkeypatch):
    res = call_tool(client, "request_payment_link", {}, key=prepaid_key(conn, scopes=("read",)))
    assert res["isError"] and "lacks the 'manage' scope" in tool_text(res)
    key = business_with_key(conn, biz_id="biz_2")  # a draft
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_2'")
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert (
        sc["pay_url"] is None and "the owner must claim this business first: give them the sign-in link" in sc["note"]
    )
    conn.execute("DELETE FROM agreement_acceptance WHERE document = 'prepayment-addendum'")
    sc = call_tool(client, "request_payment_link", {}, key=prepaid_key(conn, terms=False))["structuredContent"]["data"]
    assert (
        sc["pay_url"] is None
        and sc["connect_url"] == "http://testserver/?setup=biz_1"
        and "prepayment terms" in sc["note"]
    )
    for document in (legal.AGREEMENT, legal.ADDENDUM):
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=document)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    sc = call_tool(client, "request_payment_link", {}, key=business_with_key(conn))["structuredContent"]["data"]
    assert sc["pay_url"] is None and sc["note"] == "payments are not set up on this server yet"


def test_list_payments_shows_money_in_and_out_but_never_spend(client, conn):
    key = prepaid_key(conn)
    fund(conn, cents=50000)
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=1234)
    wallet.refund(conn, "biz_1", refund_id="re_1", amount_cents=10000, session_id="cs_1")
    wallet.adjust(conn, "biz_1", 500, note="goodwill credit")
    sc = call_tool(client, "list_payments", {"limit": 2}, key=key)["structuredContent"]["data"]
    assert [(p["kind"], p["amount_usd"]) for p in sc["payments"]] == [("adjustment", 5.0), ("refund", -100.0)]
    assert sc["payments"][0]["note"] == "goodwill credit" and sc["next_cursor"] == sc["payments"][-1]["id"]
    sc = call_tool(client, "list_payments", {"cursor": sc["next_cursor"], "limit": 2}, key=key)["structuredContent"][
        "data"
    ]
    assert [(p["kind"], p["amount_usd"]) for p in sc["payments"]] == [("topup", 500.0)] and sc["next_cursor"] is None


def test_whoami_carries_the_balance_of_a_prepaid_business(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    assert call_tool(client, "whoami", {}, key=key)["structuredContent"]["data"]["balance_usd"] is None  # own_account
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    assert call_tool(client, "whoami", {}, key=key)["structuredContent"]["data"]["balance_usd"] == 0.0
    fund(conn, cents=20000)
    assert call_tool(client, "whoami", {}, key=key)["structuredContent"]["data"]["balance_usd"] == 200.0


@pytest.mark.parametrize("chose", ["the agreement", "a pending invitation"])
def test_the_money_tools_refuse_an_own_account_business(client, conn, payments, chose):
    """Open item A5 (final review C, minor 1): the refusal names no field and no link the answer does not carry. Final
    review, item 2: only a business whose owner chose their own account is refused so -- on the way there (the
    agreement accepted) or there (its invitation sent)."""
    key = business_with_key(conn, claim_state="claimed")  # own_account, the default
    if chose == "the agreement":
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    else:
        google_ads_link.save(conn, "biz_1", "1234567890", status="pending")
    for name, args in (("get_balance", {}), ("request_payment_link", {}), ("list_payments", {})):
        res = call_tool(client, name, args, key=key)
        assert res["isError"] and tool_text(res) == NOT_MANAGED, (name, tool_text(res))


def test_while_no_way_is_chosen_every_billing_view_asks_as_connect_account_does(client, conn, walk_fake, monkeypatch):
    """Final review, item 2: a business whose owner has chosen no way yet has no balance and is not one that "runs its
    ads in the owner's own Google Ads account" either -- naming that way would choose it for them. While no way is
    chosen, every view of get_billing changes nothing and answers as connect_account's own question does: the page
    first while it comes first (its question and its ways), then how the ads run (the question and both ways side by
    side, neither marked first). A business Admiry runs the ads for answers as before."""
    from agent.tests.surface_helpers import planned, settle_page

    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)  # claimed, a draft, no page, no way chosen
    views = ("position", "payments", "topup_link")
    choices = launch_check.page_choices(conn, "biz_1")
    for view in views:
        res = call_tool(client, "get_billing", {"view": view}, key=walk["key"])
        assert not res["isError"], (view, tool_text(res))
        env = res["structuredContent"]
        assert env["say"] == launch_check.PAGE_QUESTION == env["data"]["ask_owner"], view
        assert env["data"]["choices"] == choices and env["next"] == envelope.actions(
            *launch_check.page_actions(choices)
        )
        assert NOT_MANAGED not in env["say"] and "Google Ads account" not in env["data"]["note"]
    settle_page(conn)
    for view in views:
        env = call_tool(client, "get_billing", {"view": view}, key=walk["key"])["structuredContent"]
        assert env["say"] == ads_mode.WAY_QUESTION == env["data"]["ask_owner"], view
        assert env["data"]["choices"] == ads_mode.choices()
        assert env["next"] == envelope.actions(*ads_mode.way_actions())
        assert not any("primary" in a for a in env["next"]["actions"])
        assert "pay_url" not in env["data"] and "balance_usd" not in env["data"]
    assert not ads_mode.chosen(conn, "biz_1")  # nothing written
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    position = call_tool(client, "get_billing", {"view": "position"}, key=walk["key"])["structuredContent"]
    assert position["data"]["balance_usd"] == 0.0 and "ask_owner" not in position["data"]


def test_the_money_tools_are_read_only_with_plain_descriptions():
    meta = {m.name: m for m in REGISTRY}
    assert [(meta[n].cls, meta[n].scope) for n in ("get_balance", "request_payment_link", "list_payments")] == [
        ("RO", "read"),
        ("RO", "manage"),
        ("RO", "read"),
    ]
    # the commands' own descriptions, and the public tool the docs list them under
    names = ("get_balance", "request_payment_link", "list_payments", "whoami")
    texts = [" ".join((meta[n].tool.description or "").split()) for n in names]
    texts.append({d["name"]: d for d in tool_docs()}["get_billing"]["description"])
    for text in texts:
        assert MONEY_JARGON.findall(text) == [] and JARGON.findall(text) == [] and ACCOUNT_JARGON.findall(text) == []


def test_settings_shows_the_balance_and_add_funds_only_for_a_prepaid_business(client, conn, google):
    claimed_business(client, conn, google)  # own_account
    html = client.get("/connect").text
    assert "Balance:" not in html and "Add funds" not in html and 'href="/?setup=biz_1">Set up ads<' in html
    choose(client, "managed")
    setup.accept(config.load(), OWNER, "biz_1", {legal.AGREEMENT: "v1", legal.ADDENDUM: "v0.2"})
    html = client.get("/connect").text
    assert "Balance: <strong>$0.00</strong>" in html and 'href="/?setup=biz_1">Set up ads<' in html
    assert "Nothing is spent until you set up ads and approve a plan" in html
    fund(conn, cents=50000)
    html = client.get("/connect").text
    assert (
        "Balance: <strong>$500.00</strong>" in html and "Verified" in html and 'href="/?setup=biz_1">Add funds<' in html
    )
    page = without_site_header(html)  # the shared header's nav is the product's own menu, not the page's words
    assert JARGON.findall(page) == [] and MONEY_JARGON.findall(page) == [] and ACCOUNT_JARGON.findall(page) == []
    conn.execute("UPDATE business SET paused_reason = 'no_funds'")
    assert "paused until you add funds" in client.get("/connect").text
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL")  # a released claim
    assert 'href="/?setup=biz_1"' not in client.get("/connect").text


@pytest.mark.parametrize("terms", [True, False])
def test_the_first_payments_link_is_the_plans_agree_and_pay_page(client, conn, payments, terms):
    """Founder's ruling 3 (2026-10-13): for the first payment of a draft whose page is settled, the link opens the
    plan's own agree-and-pay page -- where the owner ticks the documents themselves, so the terms come with it; with
    money in already (a top-up) or no page settled, the conversation's payment step as before."""
    from agent.tests.test_ads_run import _prepaid, seed
    from agent.tests.test_review_launch import draft_sections

    key, plan_id = seed(conn, status="draft", secs=draft_sections())
    _prepaid(conn)
    conn.execute("DELETE FROM wallet_entry")
    if terms:
        for document in (legal.AGREEMENT, legal.ADDENDUM):
            agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=document)
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert sc["pay_url"] == f"http://testserver/agree/{plan_id}" and "note" not in sc and "connect_url" not in sc
    fund(conn, cents=50000)
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert sc["pay_url"] == ("http://testserver/?setup=biz_1" if terms else None)
    conn.execute("DELETE FROM wallet_entry")
    conn.execute("DELETE FROM payment_session")
    conn.execute("DELETE FROM page")
    sc = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    # no page settled: the page question, no link (page-first review, item 8)
    assert sc["pay_url"] is None and "connect_url" not in sc and sc["ask_owner"].startswith("Which page")


def test_with_the_page_still_to_choose_the_payment_link_asks_the_page_first(client, conn, payments, monkeypatch):
    """Page-first review, item 8: while the latest draft's page is not settled, the home takes no payment
    (setup.PAGE_FIRST), so get_billing's topup_link hands no link to it: it answers the page question instead, the
    page's ways as `next` (update_landing_page, none marked first), as connect_account's page_first does. Once the page
    is settled, the first payment's link is the plan's agree page again."""
    from agent.tests.surface_helpers import settle_page
    from agent.tests.test_ads_run import _prepaid, seed
    from agent.tests.test_review_launch import draft_sections

    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    key, plan_id = seed(conn, status="draft", secs=draft_sections())
    _prepaid(conn)
    for document in (legal.AGREEMENT, legal.ADDENDUM):  # the terms are in: only the page stands in the way
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=document)
    conn.execute("DELETE FROM wallet_entry")
    conn.execute("DELETE FROM page")
    res = call_tool(client, "get_billing", {"view": "topup_link"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    choices = launch_check.page_choices(conn, "biz_1")
    assert len(choices) == 3 and env["data"]["choices"] == choices
    assert env["data"]["pay_url"] is None and "connect_url" not in env["data"]
    assert env["say"] == launch_check.PAGE_QUESTION == env["data"]["ask_owner"]
    assert env["next"] == envelope.actions(*launch_check.page_actions(choices))
    assert not any("primary" in a for a in env["next"]["actions"])
    assert "/agree/" not in json.dumps(env) and "?setup=" not in json.dumps(env)
    with pytest.raises(setup.Refused) as refused:  # what the home would answer
        setup.pay(config.load(), setup.Owner(email="owner@example.com", ip=None), "biz_1", "470", plan_id=plan_id)
    assert str(refused.value) == setup.PAGE_FIRST
    settle_page(conn)
    res = call_tool(client, "get_billing", {"view": "topup_link"}, key=key)
    assert res["structuredContent"]["data"]["pay_url"] == f"http://testserver/agree/{plan_id}", tool_text(res)


def test_a_top_up_link_while_ads_are_built_is_never_held_for_a_new_drafts_page(client, conn, payments):
    """Fix round 2b, item 4, with page-first review item 8's rule (no link the home would refuse, and none held back
    that it takes): Google Search's ads built and spending, the page down, a newer draft adding a place with no page --
    the home's Add funds goes to Stripe, so get_billing's topup_link hands its link, not the page question."""
    from agent.tests.test_agree_and_pay import running_with_a_new_place

    key, _plan_id = running_with_a_new_place(conn)
    assert launch_check.page_first(conn, "biz_1")
    res = call_tool(client, "get_billing", {"view": "topup_link"}, key=key)
    assert not res["isError"], tool_text(res)
    out = res["structuredContent"]["data"]
    assert out["pay_url"] == "http://testserver/?setup=biz_1" and "ask_owner" not in out and "note" not in out
