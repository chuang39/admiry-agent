"""The agree-and-pay page (founder's ruling 3, 2026-10-13): the link an assistant gives the owner for the first payment
opens /agree/<plan_id> -- the plan's summary, the documents the owner ticks themselves (never pre-ticked) and one
"Agree and pay" button that goes straight to Stripe -- the home's own Agree and pay (setup.choose, accept, pay with the
plan), never a second money path. Signed-in owner only; refused with a sentence and the home's link while the page is
not settled, the plan cannot launch or the money is already in; back from Stripe, "paid -- in Admiry's review"."""

from html import unescape

import pytest

from agent import ads_mode, agreements, config, copy, google_ads_link, launch_review, legal, plans, setup, wallet
from agent.tests.conftest import JARGON
from agent.tests.test_ads_run import BIZ, _knowledge, seed
from agent.tests.test_review_launch import draft_sections
from agent.tests.test_setup_api import setup_call
from agent.tests.test_stripe_hook import deliver, event
from agent.tests.test_web import csrf_of, sign_in


def _ready(conn, *, page: bool = True) -> str:
    """A verified business whose owner chose no way yet (no link, nothing accepted, no money), with a draft plan that
    can launch and -- unless `page` is False -- its page settled. Returns the plan id."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(), link=None, page=page)
    return plan_id


def _signed_in(client, google) -> None:
    assert sign_in(client).status_code == 302  # signed in, sent on to Settings


def _csrf(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def _ticks() -> dict:
    return {d: agreements.version(d) for d in (legal.AGREEMENT, legal.ADDENDUM)}


def _price() -> wallet.Quote:
    """What the page asks: two weeks of the plan's daily budget ($1,000 a month: $33 a day -> $470), by card."""
    s = config.load()
    return wallet.quote(setup.suggested_topup_usd(33, s) * 100, "card", s)


def test_the_signed_in_owner_sees_the_plan_the_documents_unticked_and_one_button(client, conn, google, stripe):
    plan_id = _ready(conn)
    _signed_in(client, google)
    r = client.get(f"/agree/{plan_id}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    html = r.text
    assert '<meta name="robots" content="noindex">' in html
    q = _price()
    assert q.credit_cents == 47_000
    for words in (
        "Google Search",
        "$1,000 a month",
        "Within 5 miles of the business",
        wallet.usd(q.total_cents),
        wallet.usd(q.processing_cents),
        wallet.usd(q.credit_cents),
    ):
        assert words in html, words
    for d in (legal.AGREEMENT, legal.ADDENDUM):  # each a tick the owner makes, at the version shown
        assert f'type="checkbox" name="{d}" value="{agreements.version(d)}">' in html
        assert f'href="/legal/{d}"' in html
    assert "checked" not in html  # never pre-ticked
    assert html.count('type="submit"') == 1 and "Agree and pay</button>" in html
    assert f'action="/agree/{plan_id}"' in html and _csrf(html)
    assert JARGON.findall(html) == []


def test_a_visitor_signs_in_first_and_anyone_else_finds_nothing(client, conn, google):
    plan_id = _ready(conn)
    r = client.get(f"/agree/{plan_id}")  # anonymous: the owner pages' own sign-in, back to this page
    assert r.status_code == 200 and f'href="/connect/google/start?next=/agree/{plan_id}"' in r.text
    assert "Agree and pay" not in r.text
    google.email = "someone@example.com"  # signed in, not the owner: no such page, as Settings' own pages answer
    _signed_in(client, google)
    theirs = client.get(f"/agree/{plan_id}")
    assert theirs.status_code == 404
    csrf = _csrf(client.get("/connect").text)
    posted = client.post(f"/agree/{plan_id}", data={"csrf": csrf, **_ticks()})
    assert posted.status_code == 404
    unknown = "pl_0000000000000000"
    nothing = client.get(f"/agree/{unknown}")
    assert client.get("/agree/nope").status_code == 404
    # fix round 2b, item 2: someone else's plan answers exactly as an id that names no plan -- never whether it exists
    assert theirs.text == posted.text == nothing.text
    assert client.post(f"/agree/{unknown}", data={"csrf": csrf, **_ticks()}).text == nothing.text


@pytest.mark.parametrize("link", ["off", "on"])
def test_the_owner_agrees_and_goes_straight_to_the_checkout_the_home_creates(
    client, conn, google, stripe, monkeypatch, link
):
    monkeypatch.setenv("AGENT_CARD_LINK", link)
    plan_id = _ready(conn)
    _signed_in(client, google)
    csrf = _csrf(client.get(f"/agree/{plan_id}").text)
    r = client.post(f"/agree/{plan_id}", data={"csrf": csrf, **_ticks()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    form = stripe.form()
    q = _price()
    assert form["metadata[plan_id]"] == plan_id and form["line_items[0][price_data][unit_amount]"] == str(q.total_cents)
    # the card, and Link by Stripe beside it only with AGENT_CARD_LINK on (fix round 2b, item 1)
    types = [form[k] for k in form if k.startswith("payment_method_types")]
    assert types == (["card", "link"] if link == "on" else ["card"])
    # Stripe's way back is the page itself (on the web base: test_web_base)
    assert form["success_url"] == f"http://testserver/agree/{plan_id}?paid=1"
    assert form["cancel_url"] == f"http://testserver/agree/{plan_id}"
    # the home's own path: the way chosen, both documents accepted, the plan's payment session
    assert ads_mode.current(conn, BIZ) == "managed" and agreements.terms_accepted(conn, BIZ)
    row = conn.execute("SELECT plan_id, status, rail FROM payment_session").fetchone()
    assert row == {"plan_id": plan_id, "status": "open", "rail": "card"}
    tools = [r["tool"] for r in conn.execute("SELECT tool FROM agent_action ORDER BY id")]
    assert tools[-4:] == ["web.ads_mode", "web.agree", "web.agree_prepayment", "web.pay_start"]


def test_paying_over_a_pending_invitation_says_it_switches_the_way(client, conn, google, stripe):
    """Final review, item 4: paying on the agree page chooses Admiry running the ads (setup.choose), which withdraws an
    invitation to the owner's own Google Ads account still waiting -- never silently: while one waits, the form says so
    in one line of its fine print, and the payment then withdraws it. With none waiting, the line is not there."""
    plan_id = _ready(conn)
    _signed_in(client, google)
    assert setup.SWITCHES_WAY not in unescape(client.get(f"/agree/{plan_id}").text)
    google_ads_link.save(conn, BIZ, "1234567890", status="pending")
    html = unescape(client.get(f"/agree/{plan_id}").text)
    assert f'<p class="fine">{setup.SWITCHES_WAY}</p>' in html and "Agree and pay</button>" in html
    assert copy.jargon(setup.SWITCHES_WAY) is None and JARGON.findall(html) == []
    csrf = _csrf(html)
    r = client.post(f"/agree/{plan_id}", data={"csrf": csrf, **_ticks()}, follow_redirects=False)
    assert r.status_code == 303 and ads_mode.current(conn, BIZ) == "managed"
    assert google_ads_link.get(conn, BIZ).status != "pending"  # withdrawn, as the line said


@pytest.mark.parametrize("ticked", [{}, {legal.AGREEMENT: agreements.version(legal.AGREEMENT)}])
def test_without_every_tick_nothing_changes_and_no_checkout_starts(client, conn, google, stripe, ticked):
    plan_id = _ready(conn)
    _signed_in(client, google)
    csrf = _csrf(client.get(f"/agree/{plan_id}").text)
    r = client.post(f"/agree/{plan_id}", data={"csrf": csrf, **ticked})
    assert r.status_code == 400 and setup.TICK_ALL in r.text and "Agree and pay</button>" in r.text
    assert stripe.sent == [] and ads_mode.current(conn, BIZ) == "own_account"
    assert not agreements.accepted(conn, BIZ) and not agreements.accepted(conn, BIZ, legal.ADDENDUM)


def test_a_post_without_the_pages_csrf_is_refused(client, conn, google, stripe):
    plan_id = _ready(conn)
    _signed_in(client, google)
    assert client.post(f"/agree/{plan_id}", data={"csrf": "wrong", **_ticks()}).status_code == 403
    assert stripe.sent == [] and not agreements.accepted(conn, BIZ)


def _refused(client, plan_id: str, sentence: str, stripe) -> None:
    """The page says `sentence` with the home's link, and its button starts nothing either."""
    r = client.get(f"/agree/{plan_id}")
    assert r.status_code == 409 and sentence in r.text and 'href="/?setup=biz_1"' in r.text
    assert "Agree and pay</button>" not in r.text
    csrf = _csrf(client.get("/connect").text)
    r = client.post(f"/agree/{plan_id}", data={"csrf": csrf, **_ticks()})
    assert r.status_code == 409 and sentence in r.text
    assert stripe.sent == []


def test_no_page_no_payment(client, conn, google, stripe):
    plan_id = _ready(conn, page=False)
    _signed_in(client, google)
    _refused(client, plan_id, setup.PAGE_FIRST, stripe)
    assert not agreements.accepted(conn, BIZ) and ads_mode.current(conn, BIZ) == "own_account"


def test_a_plan_that_cannot_launch_is_not_paid_for(client, conn, google, stripe):
    plan_id = _ready(conn)
    _knowledge(conn, phone=None)
    _signed_in(client, google)
    _refused(client, plan_id, "What number should the calls ring", stripe)


def test_a_link_to_an_older_version_is_refused(client, conn, google, stripe):
    plan_id = _ready(conn)
    plans.create_version(conn, BIZ, goal_id=None, sections=draft_sections(), estimate_basis={}, rationale="", notes=[])
    _signed_in(client, google)
    _refused(client, plan_id, setup.LINK_MOVED, stripe)


def test_with_money_in_already_there_is_nothing_to_pay(client, conn, google, stripe):
    plan_id = _ready(conn)
    wallet.add_entry(conn, BIZ, kind="topup", amount_cents=50_000, ref="pi_before", created_by="stripe")
    _signed_in(client, google)
    _refused(client, plan_id, setup.ALREADY_PAID, stripe)


def _paid_by_the_page(client, conn, google, stripe) -> str:
    plan_id = _ready(conn)
    _signed_in(client, google)
    csrf = _csrf(client.get(f"/agree/{plan_id}").text)
    assert client.post(f"/agree/{plan_id}", data={"csrf": csrf, **_ticks()}, follow_redirects=False).status_code == 303
    return plan_id


def test_back_from_stripe_the_page_waits_for_the_payment_then_says_it_is_in_review(
    client, conn, google, stripe, monkeypatch
):
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    plan_id = _paid_by_the_page(client, conn, google, stripe)
    r = client.get(f"/agree/{plan_id}?paid=1")  # before Stripe's word: being confirmed, looking again by itself
    assert r.status_code == 200 and setup.CONFIRMING in r.text
    assert f'http-equiv="refresh" content="3;url=/agree/{plan_id}?paid=1&amp;look=1"' in r.text
    assert "Agree and pay</button>" not in r.text
    late = client.get(f"/agree/{plan_id}?paid=1&look={setup.LOOKS}")  # the home's twenty looks, then its words
    assert setup.NOT_YET in late.text and "http-equiv" not in late.text
    assert deliver(client, event(amount=_price().total_cents)).json()["credited"] is True
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.IN_REVIEW
    for url in (f"/agree/{plan_id}?paid=1", f"/agree/{plan_id}"):  # paid: the confirmation, whichever way it opens
        html = unescape(client.get(url).text)
        assert setup.PAID_IN_REVIEW in html and launch_review.IN_REVIEW_SAY in html, url
        assert setup.BACK_TO_ASSISTANT in html and "http-equiv" not in html and "Agree and pay</button>" not in html
        assert JARGON.findall(html) == []


def test_with_the_review_off_the_paid_page_points_one_way(client, conn, google, stripe):
    """Fix round 2b, item 3: with Admiry's review off (as both composes default) paying approves nothing, and the
    receipt's next-step sentence ends with the home's link -- beside "go back to your assistant" the page would point
    two ways. Paid, it says the payment is in and to go back to the assistant, and nothing else."""
    plan_id = _paid_by_the_page(client, conn, google, stripe)
    assert deliver(client, event(amount=_price().total_cents)).json()["credited"] is True
    assert plans.get(conn, BIZ, plan_id)["status"] == plans.DRAFT  # review off: the money approves nothing
    for url in (f"/agree/{plan_id}?paid=1", f"/agree/{plan_id}"):
        html = unescape(client.get(url).text)
        assert f'<h1 id="title">{setup.PAID}</h1>' in html, url
        assert f"<p>{setup.PAID_LINE}</p>" in html and f"<p>{setup.BACK_TO_ASSISTANT}</p>" in html, url
        assert "/?setup=" not in html and "Confirm here" not in html and "Agree and pay</button>" not in html, url


@pytest.mark.parametrize("page", [False, True])
def test_the_homes_own_agree_and_pay_takes_no_money_before_the_page(client, conn, google, stripe, page):
    """Addendum 4: the one money path both pay buttons share refuses while the page is not settled, and the home's card
    draws the sentence like its other refusals (`error`); with the page settled it goes to Stripe as before."""
    plan_id = _ready(conn, page=page)
    _signed_in(client, google)
    csrf = csrf_of(client)
    assert setup_call(client, "choose", business_id=BIZ, mode="managed", csrf=csrf).json()["ok"]
    assert setup_call(client, "accept", business_id=BIZ, documents=_ticks(), csrf=csrf).json()["ok"]
    r = setup_call(client, "pay", business_id=BIZ, amount_usd="470", plan_id=plan_id, csrf=csrf)
    if page:
        assert r.json()["ok"] and r.json()["url"].startswith("https://checkout.stripe.com/")
    else:
        assert r.status_code == 409 and r.json() == {"ok": False, "error": setup.PAGE_FIRST}
        assert stripe.sent == []


def test_the_pages_sentences_are_owner_words():
    for said in (
        setup.PAGE_FIRST,
        setup.LINK_MOVED,
        setup.ALREADY_PAID,
        setup.PAID_IN_REVIEW,
        setup.PAID_LINE,
        setup.BACK_TO_ASSISTANT,
        setup.CONFIRMING,
        setup.NOT_YET,
    ):
        assert copy.jargon(said) is None, said
