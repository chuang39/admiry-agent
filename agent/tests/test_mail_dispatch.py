"""The state machine and the dispatcher (spec 2026-09-28 §3, §4, §5.6–§5.9, §6.2–§6.3, D53, D57, D58): the
consultation by e-mail up to the sign-in link, the plan by e-mail afterwards, authority per sender, undo, the
operator queue, and what is never reachable by e-mail."""

import inspect
import json
import re

import pytest

from agent import (
    benchmark,
    business,
    claims,
    consult,
    crawl,
    home_api,
    keys,
    listings,
    llm,
    mail_dispatch,
    mail_grants,
    models,
    plans,
    profile,
    tools_consult,
    tools_start,
)
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture
from agent.tests.test_mail_in import inbound, outcome

OWNER = "owner@example.com"
CUE = "cue-agent@agentmail.example"
OPENING = "dentist in Palo Alto within 5 miles, $2,000 a month, more Invisalign patients"
SECRET = re.compile(r"\b(ak|oa|hs|whsec|re)_[0-9a-zA-Z]+|claim=")


def read(option, usd=None):
    return models.AnswerRead(option_id=option, amount_usd=usd, miles=None, note=None, understood=True, services=[])


def mail(intent, confidence=0.9, **fields):
    base = {
        "monthly_budget_usd": None,
        "level": None,
        "radius_miles": None,
        "offer": None,
        "objective": None,
        "question": None,
    }
    return models.MailRead(intent=intent, confidence=confidence, **{**base, **fields})


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(
        knowledge=knowledge_fixture(),
        draft=plan_draft_fixture(),
        reads={
            "Bookings": read("bookings"),
            "Invisalign": read("svc_invisalign"),
            "Around $3,000": read("1000_plus", 3000),
        },
        mails={
            "Make it $1,500 a month": mail("adjust", monthly_budget_usd=1500),
            "Show me my plan": mail("plan"),
            "How do I connect?": mail("connect"),
            "Why this budget?": mail("question", question="Why this budget?"),
            "What's the weather?": mail("question", question="What's the weather?"),
            "Launch it now": mail("other"),
            "hmm": mail("adjust", confidence=0.2),
        },
        answers_to={
            "Why this budget?": models.MailAnswer(text="The budget follows what similar dentists spend.", grounded=True)
        },
    )
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    # the walk's website (SITE_WORDS) is crawled and read as the dental fixture: a crawl that read no page would leave
    # it unread (plan 2026-10-05-agent-a3p-5-1, ruling R2-a)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(tools_start.places, "search", lambda q, k, **kw: [])  # start's lookup finds no business
    return gen


def thread_row(conn, n=0):
    return dict(conn.execute("SELECT * FROM email_thread ORDER BY created_at OFFSET %s LIMIT 1", (n,)).fetchone())


def reply_to(thread):
    return ["hello+" + thread["token"] + "@agent.test"]


def last_sent(resend):
    return resend.sent[-1]


def last_to(resend, address):
    """The last mail to one address (the operator notice may follow a reply to the sender)."""
    return next(m for m in reversed(resend.sent) if m["to"] == [address])


# The reply to the website question the consultation asks first when the opening names no website (plan
# 2026-10-05-agent-a3p-5-1, Q15): the fixture site, which the fakes crawl and read as the dental fixture
SITE_WORDS = "https://brightsmile.example"


def walk_to_ready(client, conn, resend, sender=OWNER):
    inbound(client, resend, from_=sender, text=OPENING)
    t = thread_row(conn)
    for words in (SITE_WORDS, "Bookings", "Invisalign", "Around $3,000"):
        inbound(client, resend, from_=sender, text=words, to=reply_to(t))
    return thread_row(conn)


def claim_and_plan(conn, biz_id, email=OWNER):
    """The web step's effect without the web (Task 7 tests the page): the draft claimed, the plan built."""
    with conn.transaction():
        claims.claim(conn, biz_id, email)
    key, _ = keys.mint(conn, business_id=biz_id, cls="business", scopes=("read", "manage"), label="t")
    session = consult.open_for(conn, biz_id)
    env = home_api.call("recommend", {"session_id": session["id"], "idempotency_key": "r1"}, key, ip="t")
    assert "error" not in env, env
    return env["data"]["plan_id"]


def test_unknown_sender_walks_the_consultation_to_the_sign_in_link(client, conn, resend, fake):
    benchmark.seed(conn)
    inbound(client, resend, from_=f"Owner <{OWNER}>", text=OPENING, cc=["friend@example.com"])
    t = thread_row(conn)
    assert (
        t["business_id"]
        and t["session_id"]
        and t["counterpart"] == OWNER
        and outcome(conn)["intent"] == {"intent": "start"}
    )
    first = last_sent(resend)
    assert first["to"] == [OWNER] and "Options:" in first["text"] and mail_dispatch.ONLY_YOU in first["text"]
    assert first["reply_to"] == reply_to(t)[0]
    lease = conn.execute(
        "SELECT expires_at > now() + interval '6 days' AS long FROM api_key WHERE class = 'draft'"
    ).fetchone()
    assert lease["long"] is True  # plan P3: the draft lives 7 days by e-mail
    for words in (SITE_WORDS, "Bookings", "Invisalign"):  # the website first: the opening names none
        inbound(client, resend, from_=OWNER, text=words, to=reply_to(t))
        assert outcome(conn)["outcome"] == "answered" and "Options:" in last_sent(resend)["text"]
    inbound(client, resend, from_=OWNER, text="Around $3,000", to=reply_to(t))
    sent = last_sent(resend)
    assert mail_dispatch.SIGN_IN in sent["text"] and "/m/" in sent["text"] and outcome(conn)["intent"]["ready"] is True
    assert conn.execute("SELECT count(*) AS n FROM email_link").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 0  # the plan waits for the sign-in (D39)
    assert (
        conn.execute("SELECT count(*) AS n FROM api_key WHERE source = 'email' AND revoked_at IS NULL").fetchone()["n"]
        == 0
    )
    rows = conn.execute(
        "SELECT key_id FROM email_message WHERE direction = 'in' AND intent->>'intent' = 'answer'"
    ).fetchall()
    assert all(r["key_id"] for r in rows)
    assert (
        conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'answer' AND ip = 'mail'").fetchone()["n"]
        == 4
    )
    assert not any(SECRET.search(m["text"]) for m in resend.sent)


def test_a_reply_saying_there_is_no_website_asks_the_business_name_next(client, conn, resend, fake):
    """A3p-5 batch 1 Task 4: the website question is now the first the opening e-mail asks (its words name none);
    Q6's whole-answer "no" words read "we don't have one" as `none` with no crawl and no model call, and the next
    e-mail asks the business's name (Q3: the name and the services stay in the count until a site is read)."""
    benchmark.seed(conn)
    inbound(client, resend, from_=OWNER, text=OPENING)
    t = thread_row(conn)
    assert consult.WEBSITE_PROMPT in last_sent(resend)["text"]
    inbound(client, resend, from_=OWNER, text="we don't have one", to=reply_to(t))
    sent = last_sent(resend)["text"]
    assert consult.NAME_PROMPT in sent and consult.WEBSITE_PROMPT not in sent
    assert outcome(conn)["outcome"] == "answered"


def test_an_emailed_website_address_is_read_and_the_question_is_not_asked_again(client, conn, resend, fake):
    """An e-mailed address answers the website question the way the homepage does: one crawl, one reading (the fake
    fixture crawls SITE_WORDS and reads it as the dental fixture), and the next e-mail moves on rather than asking
    the website question again."""
    benchmark.seed(conn)
    inbound(client, resend, from_=OWNER, text=OPENING)
    t = thread_row(conn)
    inbound(client, resend, from_=OWNER, text=SITE_WORDS, to=reply_to(t))
    sent = last_sent(resend)["text"]
    assert "I read brightsmile.example" in sent and consult.WEBSITE_PROMPT not in sent
    assert outcome(conn)["outcome"] == "answered"


def test_the_wall_mails_capability_link_is_masked_in_storage_not_in_the_sent_mail(client, conn, resend, fake):
    """finding I7: the /m/{token} capability must not sit in email_message.new_text, in plaintext, for the 90
    days D62 keeps message text -- only the stored copy is masked; the mail Resend actually sent keeps the token
    (mail_out.deliver's payload is built before the masking, and is what the sender's mail client receives)."""
    benchmark.seed(conn)
    walk_to_ready(client, conn, resend)
    sent = last_sent(resend)
    token = re.search(r"/m/([A-Za-z0-9_-]+)", sent["text"]).group(1)
    row = conn.execute(
        "SELECT new_text FROM email_message WHERE direction = 'out' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert f"/m/{token}" not in row["new_text"] and "/m/…" in row["new_text"]
    assert f"/m/{token}" in sent["text"]


def test_a_short_question_from_a_stranger_gets_the_intro(client, conn, resend, fake):
    inbound(client, resend, from_="curious@example.com", text="What is Admiry?")
    assert last_sent(resend)["text"].startswith(mail_dispatch.INTRO) and outcome(conn)["intent"] == {"intent": "intro"}
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 0


def test_an_inbound_attachment_gets_the_fixed_cannot_open_sentence(client, conn, resend, fake):
    """spec §5.5: attachments are never fetched or read; the reply says so (finding I8)."""
    inbound(client, resend, from_="curious@example.com", text="See attached", attachments=[{"filename": "photo.jpg"}])
    assert mail_dispatch.ATTACHMENTS in last_sent(resend)["text"]


def test_the_fourth_start_today_is_refused_even_when_earlier_ones_were_ready_at_once(
    client, conn, resend, fake, monkeypatch
):
    """A start that pre-answers every question finishes through _wall, not the question branch; it must still
    count toward STARTS_PER_DAY, or an opening that happens to need no follow-up never hits the daily cap."""
    benchmark.seed(conn)
    real_begin = tools_start.begin_anonymous

    def ready_at_once(text, website_url, locale, known_facts, *, ip):
        env = real_begin(text, website_url, locale, known_facts, ip=ip)
        return {**env, "next": {"kind": "actions", "actions": []}}

    monkeypatch.setattr(tools_start, "begin_anonymous", ready_at_once)
    for _ in range(3):
        inbound(client, resend, from_=OWNER, text=OPENING)
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 3
    starts = conn.execute(
        "SELECT intent FROM email_message WHERE direction = 'in' AND from_addr = %s ORDER BY created_at", (OWNER,)
    ).fetchall()
    assert [s["intent"] for s in starts] == [{"intent": "start", "ready": True}] * 3
    inbound(client, resend, from_=OWNER, text=OPENING)
    assert mail_dispatch.FULL in last_sent(resend)["text"] and outcome(conn)["outcome"] == "refused:limit"
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 3


def test_owner_adjusts_by_email_and_undoes(client, conn, resend, fake):
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    plan_id = claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", to=reply_to(t))
    sent = last_sent(resend)
    assert "$1,500" in sent["text"] and mail_dispatch.UNDO_HINT in sent["text"] and "/plan/" in sent["text"]
    change = thread_row(conn)["last_change"]
    assert change["tool"] == "adjust" and change["plan_before"] == plan_id
    assert (
        conn.execute("SELECT grant_id FROM api_key WHERE source = 'email' ORDER BY created_at DESC LIMIT 1").fetchone()[
            "grant_id"
        ]
        is None
    )
    inbound(client, resend, from_=OWNER, text="Undo.", to=reply_to(t))
    sent = last_sent(resend)
    assert mail_dispatch.UNDO_DONE.format(what="$2,000") in sent["text"] and outcome(conn)["intent"] == {
        "intent": "undo"
    }
    assert thread_row(conn)["last_change"] is None
    assert fake.mail_calls[-1]["text"] == "Make it $1,500 a month"  # undo never reached the reader
    inbound(client, resend, from_=OWNER, text="revert", to=reply_to(t))
    assert mail_dispatch.UNDO_NOTHING in last_sent(resend)["text"]


def test_undo_puts_back_the_whole_budget_of_a_plan_with_two_channels(
    client, conn, resend, fake, newsbreak_on, pictures
):
    """The plan's amount is every funded channel's, not the first row's (M0 Task 5): undo restores all of it."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    plan_id = claim_and_plan(conn, t["business_id"])
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = %s", (t["business_id"],))  # can have NewsBreak
    key, _ = keys.mint(conn, business_id=t["business_id"], cls="business", scopes=("read", "manage"), label="t2")
    args = {"plan_id": plan_id, "channel": "newsbreak", "channel_monthly_usd": 300, "idempotency_key": "p1"}
    assert "error" not in home_api.call("adjust", args, key, ip="t")
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", to=reply_to(t))
    inbound(client, resend, from_=OWNER, text="Undo.", to=reply_to(t))
    assert mail_dispatch.UNDO_DONE.format(what="$2,000") in last_sent(resend)["text"]
    latest = plans.latest(conn, t["business_id"])["sections"]["ads"]["channels"]
    assert [(c["channel"], c["monthly_usd"]) for c in latest] == [("google_search", 1700.0), ("newsbreak", 300.0)]


def test_fit_answer_cuts_at_a_paragraph_boundary_or_hard_cuts():
    short = "Bookings"
    assert mail_dispatch._fit_answer(short) == short
    with_disclaimer = "Bookings" + "\n\n" + "x" * 600
    assert mail_dispatch._fit_answer(with_disclaimer) == "Bookings"
    no_boundary = "y" * 600
    assert mail_dispatch._fit_answer(no_boundary) == "y" * consult.MAX_OTHER


def test_long_email_answers_are_cut_to_fit_instead_of_refused(client, conn, resend, fake):
    """consult.parse refuses anything over consult.MAX_OTHER outright (finding I4): a one-word answer plus an
    auto-appended disclaimer paragraph must still reach the operator's reader, cut at the paragraph boundary."""
    benchmark.seed(conn)
    inbound(client, resend, from_=OWNER, text=OPENING)
    t = thread_row(conn)
    disclaimer = "x" * 600
    inbound(client, resend, from_=OWNER, text=f"Bookings\n\n{disclaimer}", to=reply_to(t))
    assert outcome(conn)["outcome"] == "answered" and "Options:" in last_sent(resend)["text"]


def test_owner_new_thread_lands_on_the_planned_business(client, conn, resend, fake):
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="Show me my plan", subject="my plan")
    assert thread_row(conn, 1)["business_id"] == t["business_id"]
    sent = last_sent(resend)
    assert "/plan/" in sent["text"] and sent["subject"] == "Re: my plan" and outcome(conn)["intent"]["intent"] == "plan"
    inbound(client, resend, from_=OWNER, text="How do I connect?", to=reply_to(thread_row(conn, 1)))
    connect = last_sent(resend)["text"]  # this thread's business's setup step, kept across sign-in by the home
    # the plan answer's own sentence (walk fixes 1, Task 6 round 2): since A3c-1 its own Google Ads account, or prepaid
    assert tools_consult.PREPAY_SAY in connect and "coming" not in connect
    assert f"http://testserver/?setup={t['business_id']}" in connect
    assert "/connect?setup=" not in connect


def test_the_connect_reply_says_google_search_waits_for_a_business_that_prepays(
    client, conn, resend, fake, monkeypatch
):
    """Walk fixes 1, Task 6 round 2: asked how to connect while Admiry-run Google accounts were not open (before
    A3c-1), a business that prepays reads the plan answer's own sentence -- its Google Search waits for Admiry-run
    accounts -- never the old offer to prepay."""
    from agent import plans

    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = %s", (t["business_id"],))  # before the plan
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="How do I connect?", to=reply_to(t))
    connect = last_sent(resend)["text"]
    assert tools_consult.PREPAID_WAITS_SAY in connect, connect
    assert tools_consult.PREPAY_SAY not in connect and tools_consult.OWN_ACCOUNT_SAY not in connect
    # nothing to connect: no "Connect Google Ads" link under the sentence that says so (final review A6, carried 5)
    assert "?setup=" not in connect and "Connect Google Ads" not in connect


def test_the_connect_reply_for_a_business_that_prepays_never_says_google_waits(client, conn, resend, fake):
    """A3c-1: Admiry runs a prepaid business's Google Search in a pool account of its own, so the reply is the plan
    answer's sentence for that, never the wait; and still no own account to link."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = %s", (t["business_id"],))  # before the plan
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="How do I connect?", to=reply_to(t))
    connect = last_sent(resend)["text"]
    assert tools_consult.PREPAID_RUNS_SAY in connect and "link yours" not in connect, connect
    assert "Connect Google Ads" not in connect


def test_the_connect_reply_on_a_plan_without_google_search_speaks_of_no_google(client, conn, resend, fake, monkeypatch):
    """Walk fixes 1, final review A5 (carried item 4): a plan with no Google Search row (NewsBreak alone) says how its
    places run, in the plan answer's words -- Admiry runs them from the balance -- and nothing of Google: no wait for
    Admiry-run accounts, no own account, no "Connect Google Ads" link."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    real = home_api._planned_cards

    def newsbreak_only(conn, biz):  # the plan card a NewsBreak-only plan draws (NewsBreak is off in tests)
        latest, cards = real(conn, biz)
        row = {**cards["plan"]["channels"][0], "platform": "newsbreak", "label": "NewsBreak"}
        return latest, {**cards, "plan": {**cards["plan"], "channels": [{**row, "mode_text": plans.MANAGED_TEXT}]}}

    monkeypatch.setattr(home_api, "_planned_cards", newsbreak_only)
    inbound(client, resend, from_=OWNER, text="How do I connect?", to=reply_to(t))
    connect = last_sent(resend)["text"]
    assert "Admiry runs NewsBreak from your balance." in connect and "Google" not in connect, connect
    assert outcome(conn)["intent"] == {"intent": "connect"}


def test_questions_are_answered_from_material_or_handed_over(client, conn, resend, fake):
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="Why this budget?", to=reply_to(t))
    assert "similar dentists" in last_sent(resend)["text"] and outcome(conn)["intent"] == {"intent": "question"}
    assert "cards" in fake.answer_calls[0]["material"] and "knowledge" in fake.answer_calls[0]["material"]
    # how each place the plan funds runs, from the one builder `ask` reads too (walk fixes 1, Task 6 round 2)
    assert fake.answer_calls[0]["material"]["ways"] == [
        "Google Search: your own Google Ads account; Admiry can also run it from a prepaid balance"
    ]
    assert (
        conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool IN ('mail.read', 'mail.answer')").fetchone()[
            "n"
        ]
        == 2
    )
    inbound(client, resend, from_=OWNER, text="What's the weather?", to=reply_to(t))
    assert mail_dispatch.HANDED in last_to(resend, OWNER)["text"] and outcome(conn)["outcome"] == "queued"
    assert thread_row(conn)["status"] == "operator"
    assert any(m["to"] == ["ops@example.com"] for m in resend.sent)


def test_other_and_unclear_reach_the_operator_and_the_thread_stays_quiet(client, conn, resend, fake):
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="hmm", to=reply_to(t))
    assert mail_dispatch.UNCLEAR in last_sent(resend)["text"] and thread_row(conn)["unclear"] == 1
    inbound(client, resend, from_=OWNER, text="hmm", to=reply_to(t), message_id="<hmm2@example.com>")
    # the second identical text is the loop breaker's first rule (mail_in): queued, handed over
    assert outcome(conn)["outcome"] == "queued" and thread_row(conn)["status"] == "operator"
    n = len(resend.sent)
    inbound(client, resend, from_=OWNER, text="anything else", to=reply_to(t))
    assert outcome(conn)["outcome"] == "queued" and len(resend.sent) == n  # quiet until an operator answers


def test_loop_stops_at_the_operator_and_stays_quiet(client, conn, resend, fake):
    benchmark.seed(conn)
    inbound(client, resend, from_=CUE, text=OPENING)
    t = thread_row(conn)
    inbound(client, resend, from_=CUE, text="I am an assistant and cannot answer that.", to=reply_to(t))
    inbound(client, resend, from_=CUE, text="I am an assistant and cannot answer that.", to=reply_to(t))
    assert thread_row(conn)["status"] == "operator" and last_to(resend, CUE)["text"].startswith(mail_dispatch.HANDED)
    n = len(resend.sent)
    inbound(client, resend, from_=CUE, text="I am an assistant and cannot answer that.", to=reply_to(t))
    assert outcome(conn)["outcome"] == "queued" and len(resend.sent) == n


def test_known_sender_with_two_businesses_is_asked_which(client, conn, resend, fake):
    benchmark.seed(conn)
    for biz, slug in (("biz_a", "acme-plumbing"), ("biz_b", "bright-smile")):
        conn.execute(
            "INSERT INTO business (id, claim_state, owner_email, slug) VALUES (%s, 'claimed', %s, %s)",
            (biz, OWNER, slug),
        )
    inbound(client, resend, from_=OWNER, text="Show me my plan")
    assert mail_dispatch.WHICH.split("{")[0] in last_sent(resend)["text"] and thread_row(conn)["business_id"] is None
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0
    inbound(client, resend, from_=OWNER, text="bright-smile please", to=reply_to(thread_row(conn)))
    assert (
        thread_row(conn)["business_id"] == "biz_b"
        and mail_dispatch.CHOSEN.format(name="bright-smile") in last_sent(resend)["text"]
    )


def test_grant_scopes_bound_what_an_address_may_do(client, conn, resend, fake):
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    g = mail_grants.add(conn, t["business_id"], CUE, scopes=("read",), via="connect", created_by=OWNER)
    inbound(client, resend, from_=CUE, text="Make it $1,500 a month", to=reply_to(t))
    assert mail_dispatch.READ_ONLY in last_sent(resend)["text"]
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'adjust'").fetchone()["n"] == 0
    inbound(client, resend, from_=CUE, text="Show me my plan", to=reply_to(t))
    assert "/plan/" in last_sent(resend)["text"]
    assert (
        conn.execute("SELECT grant_id FROM api_key WHERE source = 'email' ORDER BY created_at DESC LIMIT 1").fetchone()[
            "grant_id"
        ]
        == g["id"]
    )
    assert (
        conn.execute("SELECT last_used_at FROM email_grant WHERE id = %s", (g["id"],)).fetchone()["last_used_at"]
        is not None
    )
    mail_grants.add(conn, t["business_id"], CUE, scopes=("read", "manage"), via="connect", created_by=OWNER)
    inbound(client, resend, from_=CUE, text="Make it $1,500 a month", to=reply_to(t))
    assert "$1,500" in last_sent(resend)["text"]
    assert mail_grants.revoke(conn, g["id"], t["business_id"])
    to_owner_before = sum(1 for m in resend.sent if m["to"] == [OWNER])
    inbound(client, resend, from_=CUE, text="Make it $1,500 a month", to=reply_to(t))
    # a revoked address is a stranger again: its message starts a fresh consultation on a new draft, not the plan
    assert thread_row(conn, 1)["counterpart"] == CUE and thread_row(conn, 1)["business_id"] != t["business_id"]
    # the authority check runs before mail_in's own loop/limit breakers (a stranger holding the token must never
    # bind to or freeze the owner's own thread, nor put the owner's name on words the stranger sent)
    assert conn.execute("SELECT status FROM email_thread WHERE id = %s", (t["id"],)).fetchone()["status"] == "open"
    assert sum(1 for m in resend.sent if m["to"] == [OWNER]) == to_owner_before


# ---------------------------------------------------------------- "not my business" in the owner's own words
# (plan 2026-10-03-agent-listing-not-mine-words, Task 2)

NOT_MINE = "That place on Google Maps is not my business"
# the listing's own point was the centre: it goes with it, and after the plan no consultation asks where the ads centre,
# so a new version centres on the areas the business serves (Task 2 round 1)
UNLINKED_SAY = listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE_LATER + listings.LINK_AGAIN


def link_listing(conn, biz_id):
    """The business's own Google Maps listing, as its owner confirmed it (listings.link): the e-mail walk finds none."""
    chosen = {
        "place_id": "ChIJ1",
        "name": "Bright Smile Dental",
        "address": "500 University Ave, Palo Alto, CA",
        "lat": 37.4419,
        "lng": -122.143,
    }
    knowledge = business.latest_knowledge(conn, biz_id)[1]
    business.add_knowledge(conn, biz_id, listings.link(knowledge, chosen), source="owner")


def listing_on_file(conn, biz_id):
    return profile.business_listing(business.latest_knowledge(conn, biz_id)[1])


def count(conn, tool):
    return conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = %s", (tool,)).fetchone()["n"]


def test_an_owner_who_writes_that_the_place_is_not_theirs_has_it_unlinked(client, conn, resend, fake):
    """D4/D5: after the plan, an owner's e-mail that calls the listing not theirs runs the command every unlink runs
    (update_business with google_listing null) under the message's own key, before any intent: the reply is the
    unlink's sentences and how to link the right place, and the message is answered as `listing_not_mine`."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    link_listing(conn, t["business_id"])
    fake.mails[NOT_MINE] = mail("other", listing_not_mine=True)  # its intent alone would go to the operator
    inbound(client, resend, from_=OWNER, text=NOT_MINE, to=reply_to(t))
    assert UNLINKED_SAY in last_sent(resend)["text"] and listings.NOTHING_ELSE not in last_sent(resend)["text"]
    assert outcome(conn)["outcome"] == "answered" and outcome(conn)["intent"] == {"intent": "listing_not_mine"}
    assert listing_on_file(conn, t["business_id"]) is None and thread_row(conn)["status"] == "open"
    row = conn.execute("SELECT ok, ip, args_redacted FROM agent_action WHERE tool = 'update_business'").fetchone()
    assert row["ok"] and row["ip"] == "mail" and json.loads(row["args_redacted"]["patch"]) == {"google_listing": None}
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT count(*) AS n FROM api_key WHERE source = 'email' AND revoked_at IS NULL").fetchone()["n"]
        == 0
    )


def test_undo_after_an_unlink_reverts_no_earlier_change(client, conn, resend, fake):
    """Final review M3: "undo" is what an owner writes after an unlink they did not mean, but the thread's last change is
    an earlier budget change they did not mean to take back. The unlink clears it: undo answers that there is nothing
    to undo (the reply to the unlink already says how to link the place again)."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    link_listing(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", to=reply_to(t))
    assert thread_row(conn)["last_change"]["tool"] == "adjust"
    fake.mails[NOT_MINE] = mail("other", listing_not_mine=True)
    inbound(client, resend, from_=OWNER, text=NOT_MINE, to=reply_to(t))
    assert listing_on_file(conn, t["business_id"]) is None and thread_row(conn)["last_change"] is None
    inbound(client, resend, from_=OWNER, text="Undo.", to=reply_to(t))
    assert mail_dispatch.UNDO_NOTHING in last_sent(resend)["text"]
    assert outcome(conn)["intent"] == {"intent": "undo", "nothing": True}
    latest = plans.latest(conn, t["business_id"])
    assert latest["version"] == 2 and plans.total_usd(latest["sections"]) == 1500  # the budget change stands


def test_a_change_named_in_the_same_e_mail_is_not_made_and_the_reply_says_so(client, conn, resend, fake):
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    link_listing(conn, t["business_id"])
    words = "That place is not my business, and make it $1,500 a month"
    fake.mails[words] = mail("adjust", monthly_budget_usd=1500, listing_not_mine=True)
    inbound(client, resend, from_=OWNER, text=words, to=reply_to(t))
    assert UNLINKED_SAY + listings.NOTHING_ELSE in last_sent(resend)["text"]
    assert count(conn, "adjust") == 0 and plans.latest(conn, t["business_id"])["version"] == 1


def test_a_listing_unlinked_meanwhile_reads_as_a_reply(client, conn, resend, fake, monkeypatch):
    """The race (Task 2 round 1): another path unlinks the listing after the message saw it and before the command
    runs. Nothing changes, and the reply reads as change_plan's: nothing is linked, and how to link the right place."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    link_listing(conn, t["business_id"])
    real = home_api.listing_linked

    def seen_then_unlinked(c, biz_id):
        linked = real(c, biz_id)
        listings.unlink_listing(conn, biz_id)  # Settings' "Not my business", between the check and the command
        return linked

    monkeypatch.setattr(home_api, "listing_linked", seen_then_unlinked)
    fake.mails[NOT_MINE] = mail("other", listing_not_mine=True)
    version = business.latest_knowledge(conn, t["business_id"])[0]
    inbound(client, resend, from_=OWNER, text=NOT_MINE, to=reply_to(t))
    assert last_sent(resend)["text"].startswith(listings.NOTHING_LINKED + listings.LINK_AGAIN)
    assert outcome(conn)["outcome"] == "answered" and outcome(conn)["intent"] == {"intent": "listing_not_mine"}
    assert business.latest_knowledge(conn, t["business_id"])[0] == version + 1  # the other path's unlink alone


def test_an_address_that_may_only_read_cannot_unlink_the_listing(client, conn, resend, fake):
    """Review focus 4: the sender's scope decides, not the read."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    link_listing(conn, t["business_id"])
    mail_grants.add(conn, t["business_id"], CUE, scopes=("read",), via="connect", created_by=OWNER)
    fake.mails[NOT_MINE] = mail("other", listing_not_mine=True)
    inbound(client, resend, from_=CUE, text=NOT_MINE, to=reply_to(t))
    assert mail_dispatch.READ_ONLY in last_sent(resend)["text"]
    assert outcome(conn)["intent"] == {"intent": "listing_not_mine", "denied": True}
    assert listing_on_file(conn, t["business_id"])["place_id"] == "ChIJ1" and count(conn, "update_business") == 0


def test_with_no_listing_linked_the_e_mail_takes_its_usual_intent(client, conn, resend, fake):
    """Review focus 2: nothing linked, the flag changes nothing -- the budget it names is adjust's change, as ever."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    fake.mails[NOT_MINE] = mail("adjust", monthly_budget_usd=1500, listing_not_mine=True)
    inbound(client, resend, from_=OWNER, text=NOT_MINE, to=reply_to(t))
    assert "$1,500" in last_sent(resend)["text"] and outcome(conn)["intent"] == {"intent": "adjust"}
    assert count(conn, "update_business") == 0


def test_handed_and_operator_reply_follow_the_owner_not_a_revoked_agent(client, conn, resend, fake):
    """M1: the owner revokes an address's grant, then writes directly into that address's own thread (its
    plus-token). HANDED must follow the owner -- the last sender the thread still accepts -- not
    `thread["counterpart"]`, which stays fixed at whoever started the thread and by now has no authority left."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend, sender=CUE)
    claim_and_plan(conn, t["business_id"], email=OWNER)
    g = mail_grants.add(conn, t["business_id"], CUE, scopes=("read", "manage"), via="connect", created_by=OWNER)
    assert mail_grants.revoke(conn, g["id"], t["business_id"])
    cue_before = sum(1 for m in resend.sent if m["to"] == [CUE])
    inbound(client, resend, from_=OWNER, text="Show me my plan", to=reply_to(t))
    inbound(client, resend, from_=OWNER, text="Show me my plan", to=reply_to(t))  # identical text: the loop breaker
    assert thread_row(conn)["status"] == "operator"
    assert mail_dispatch.HANDED in last_to(resend, OWNER)["text"]
    assert sum(1 for m in resend.sent if m["to"] == [CUE]) == cue_before  # nothing new to the revoked agent


def test_a_released_claim_is_not_a_draft_for_the_counterpart_only_rule(conn):
    """M2: jobs.claim_expire releases an unverified claim back to claim_state 'draft' but keeps owner_email (spec
    §4.0). may_continue must not then fall back to "only the counterpart may continue" -- the owner still has
    authority over the thread, and a stranger holding its old token still does not."""
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'draft', %s, 'acme')", (OWNER,)
    )
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token) VALUES ('et_1', 'biz_1', 'stranger@example.com', 'tok')"
    )
    t = thread_row(conn)
    assert mail_dispatch.may_continue(conn, t, OWNER) is True
    assert mail_dispatch.may_continue(conn, t, "stranger@example.com") is False


def test_unverified_known_sender_gets_one_link_a_day(client, conn, resend, fake):
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', %s)", (OWNER,))
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", dmarc="fail")
    assert outcome(conn)["outcome"] == "refused:auth" and mail_dispatch.UNVERIFIED in last_sent(resend)["text"]
    assert "Admiry: http://testserver/" in last_sent(resend)["text"]
    n = len(resend.sent)
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", dmarc="fail")
    assert len(resend.sent) == n and conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_unverified_reply_never_reuses_the_forged_subject(client, conn, resend, fake):
    """mail_dispatch.unverified_reply must not put a forged Subject header into a DKIM-signed Admiry mail, nor
    onto the thread it creates -- finding M3."""
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', %s)", (OWNER,))
    inbound(
        client,
        resend,
        from_=OWNER,
        text="Make it $1,500 a month",
        dmarc="fail",
        subject="URGENT your ads are suspended, call +1 555 0100",
    )
    sent = last_sent(resend)
    assert sent["subject"] == mail_dispatch.UNVERIFIED_SUBJECT
    assert thread_row(conn)["subject"] == mail_dispatch.UNVERIFIED_SUBJECT


def test_an_expired_draft_thread_starts_over(client, conn, resend, fake):
    benchmark.seed(conn)
    inbound(client, resend, from_=OWNER, text=OPENING)
    t = thread_row(conn)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = %s", (t["business_id"],))
    inbound(client, resend, from_=OWNER, text="Bookings", to=reply_to(t))
    assert mail_dispatch.EXPIRED in last_sent(resend)["text"] and thread_row(conn)["business_id"] is None
    inbound(client, resend, from_=OWNER, text=OPENING, to=reply_to(t))
    assert thread_row(conn)["business_id"] not in (None, t["business_id"])


def test_spend_and_account_tools_are_never_named_here():
    src = inspect.getsource(mail_dispatch)
    for name in (
        "approve_plan",
        "confirm_proposal",
        "set_autopilot",
        "set_goal",
        "list_keys",
        "revoke_key",
        "request_payment_link",
    ):
        assert name not in src, name


def test_fixed_sentences_use_owner_words():
    for name, value in vars(mail_dispatch).items():
        if name.isupper() and isinstance(value, str):
            assert not JARGON.search(value) and not ACCOUNT_JARGON.search(value) and not MONEY_JARGON.search(value), (
                name
            )


def test_an_unclear_reply_on_a_conversation_thread_is_unchanged_by_the_digest(client, conn, resend, fake):
    """The digest's reply branch adds a link to its own unclear reply only (plan 2026-10-07-agent-k2 Task 15, D12): the
    conversation's stays byte for byte what it was."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="hmm", to=reply_to(t))
    assert last_sent(resend)["text"] == (
        "I did not quite get that. Tell me in a few words what you would like.\n\n"
        "Admiry never asks for passwords, codes or card numbers by e-mail."
    )


def test_a_question_by_mail_reads_the_knowledge_the_plan_cites(client, conn, resend, fake, knowledge_on):
    """Capability layer K3 (plan P8): the e-mail answerer reads what `ask` reads."""
    fake.draft = plan_draft_fixture().model_copy(update={"knowledge_ids": ["dentists/compliance"]})
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="Why this budget?", to=reply_to(t))
    assert fake.answer_calls[0]["material"]["trade_knowledge"].startswith("== dentists/compliance ==")


def test_a_question_by_mail_reads_no_notes_for_a_plan_that_cites_none(client, conn, resend, fake, knowledge_on):
    benchmark.seed(conn)  # the draft names no id: the switch is on, the tree is there, the plan cites nothing
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    inbound(client, resend, from_=OWNER, text="Why this budget?", to=reply_to(t))
    assert "trade_knowledge" not in fake.answer_calls[0]["material"]
