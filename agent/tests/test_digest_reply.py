"""Replies to a daily digest (plan 2026-10-07-agent-k2 Task 15; Dg plan Task 12; digest spec §8): the reply is read with
the report's numbers and the day's look, never taken for an answer to a consultation and never the start of one; a
change needs a plan the owner finished, and what Admiry cannot place goes to a person with the report's link."""

import json
from datetime import date

import pytest
from psycopg.types.json import Jsonb

from agent import benchmark, config, crawl, db, digest_links, llm, mail_dispatch, models, tools_start
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.digest_fixtures import seed_business, seed_plan
from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture
from agent.tests.test_mail_dispatch import (
    OWNER,
    claim_and_plan,
    last_sent,
    last_to,
    mail,
    read,
    walk_to_ready,
)
from agent.tests.test_mail_in import inbound, outcome

DAY = date(2026, 10, 5)
BODY = {
    "edition": "daily",
    "day": DAY.isoformat(),
    "rows": [{"channel": "google_search", "spend_usd": 23.1, "clicks": 41, "calls": 2, "leads": 1}],
}
QUESTION = "How many calls did I get?"
AGAIN = "And the clicks?"  # a second question: the same words twice are the loop breaker's (mail_in), not a reply
SUPPORT = "Can someone call me about this?"


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
            "Change something": mail("adjust"),
            "Show me my plan": mail("plan"),
            QUESTION: mail("question", question=QUESTION),
            AGAIN: mail("question", question=AGAIN),
            "Launch it now": mail("other"),
            SUPPORT: models.MailRead(
                **{**mail("question", question=SUPPORT).model_dump(), "support": "human"},
            ),
        },
        answers_to={
            QUESTION: models.MailAnswer(text="Two calls came in on Monday.", grounded=True),
            AGAIN: models.MailAnswer(text="41 clicks on Monday.", grounded=True),
        },
    )
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(tools_start.places, "search", lambda q, k, **kw: [])
    return gen


def digest_thread(conn, biz: str, *, body: dict | None = BODY, status: str = "sent") -> dict:
    """The thread the digest job opens for an owner mail, and that day's digest row on it."""
    th = mail_dispatch.open_thread(conn, biz, OWNER, "Your Admiry report for Mon, Oct 5")
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status, body, thread_id) "
        "VALUES (%s, %s, %s, 'daily', 'America/Los_Angeles', 'on', %s, %s, %s)",
        (db.new_id("dg"), biz, DAY, status, Jsonb(body) if body is not None else None, th["id"]),
    )
    return th


def to(th: dict) -> list[str]:
    return ["hello+" + th["token"] + "@agent.test"]


def thread(conn, th: dict) -> dict:
    return dict(conn.execute("SELECT * FROM email_thread WHERE id = %s", (th["id"],)).fetchone())


def report_url(biz: str) -> str:
    return digest_links.report_url(config.load(), biz, DAY)


def planned(client, conn, resend) -> str:
    """A business whose consultation finished with a plan (the e-mail walk, then the web step's effect)."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    return t["business_id"]


def open_consultation(conn, biz: str) -> str:
    """A second consultation still asking its questions, as if the owner had started over on the homepage."""
    conn.execute(
        "INSERT INTO consult_session (id, business_id, tree, locale, opening_text, known_facts, answers, options, "
        "expires_at) SELECT 'cs_again', business_id, tree, locale, opening_text, known_facts, '{}'::jsonb, options, "
        "now() + interval '1 day' FROM consult_session WHERE business_id = %s LIMIT 1",
        (biz,),
    )
    return "cs_again"


def sessions(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM consult_session").fetchone()["n"]


def test_reply_is_not_consumed_by_an_open_consultation(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    open_consultation(conn, biz)
    th = digest_thread(conn, biz)
    asked = len(fake.read_calls)
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", to=to(th))
    assert len(fake.read_calls) == asked  # never read as an answer to the open consultation's question
    assert thread(conn, th)["session_id"] is None
    answers = conn.execute("SELECT answers FROM consult_session WHERE id = 'cs_again'").fetchone()["answers"]
    assert answers == {}
    sent = last_sent(resend)
    # the business is in the middle of a new consultation: no change by e-mail, the way to change it instead
    assert mail_dispatch.CHANGE_PLAN in sent["text"] and report_url(biz) in sent["text"]
    assert outcome(conn)["intent"] == {"intent": "adjust", "planned": False}


def test_reply_never_starts_a_consultation(client, conn, resend, fake):
    seed_business(conn, "biz_1")
    th = digest_thread(conn, "biz_1")
    n = sessions(conn)
    inbound(client, resend, from_=OWNER, text="I run a bakery and want more customers", to=to(th))
    assert sessions(conn) == n == 0
    sent = last_sent(resend)
    assert mail_dispatch.UNCLEAR in sent["text"] and report_url("biz_1") in sent["text"]
    assert outcome(conn)["intent"] == {"intent": "unclear"}


def test_question_is_answered_from_the_reports_numbers(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    inbound(client, resend, from_=OWNER, text=QUESTION, to=to(th))
    assert "Two calls came in on Monday." in last_sent(resend)["text"]
    material = fake.answer_calls[0]["material"]
    assert material["report"] == BODY and "cards" in material and "knowledge" in material
    assert fake.mail_calls[-1]["cards"]["report"] == BODY and "plan" in fake.mail_calls[-1]["cards"]
    assert outcome(conn)["intent"] == {"intent": "question"}


def test_a_report_still_marked_ready_is_read_too(client, conn, resend, fake):
    """P17: Resend may have taken the mail before the job could mark it sent; the owner's reply proves it arrived."""
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz, status="ready")
    inbound(client, resend, from_=OWNER, text=QUESTION, to=to(th))
    assert fake.answer_calls[0]["material"]["report"] == BODY


def test_question_on_a_business_without_a_plan_is_answered_from_the_report(client, conn, resend, fake):
    seed_business(conn, "biz_1")
    th = digest_thread(conn, "biz_1")
    inbound(client, resend, from_=OWNER, text=QUESTION, to=to(th))
    assert "Two calls came in on Monday." in last_sent(resend)["text"]
    material = fake.answer_calls[0]["material"]
    assert material["report"] == BODY and material["cards"] is None and material["ways"] == []


def _look(conn, biz: str, verdict: str = "deliver") -> None:
    look = {"summary": "Two calls from 41 clicks.", "summary_dropped": None, "findings": [], "recommendations": []}
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status, insight, verdict) "
        "VALUES ('in_1', %s, %s, 'on', 'done', %s, %s)",
        (biz, DAY, json.dumps(look), verdict),
    )


def test_question_material_carries_the_look_only_when_an_owner_may_read_it(client, conn, resend, fake, monkeypatch):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    _look(conn, biz)
    monkeypatch.setenv("AGENT_ROLES", "validate")
    inbound(client, resend, from_=OWNER, text=QUESTION, to=to(th))
    assert fake.answer_calls[0]["material"]["insight"] is None
    assert fake.mail_calls[-1]["cards"]["insight"] is None
    monkeypatch.setenv("AGENT_ROLES", "on")
    inbound(client, resend, from_=OWNER, text=AGAIN, to=to(th))
    view = fake.answer_calls[1]["material"]["insight"]
    assert view is not None and view["summary"] == "Two calls from 41 clicks."
    assert fake.mail_calls[-1]["cards"]["insight"] == view


def test_adjust_with_and_without_a_planned_session(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    inbound(client, resend, from_=OWNER, text="Make it $1,500 a month", to=to(th))
    sent = last_sent(resend)
    assert "$1,500" in sent["text"] and mail_dispatch.UNDO_HINT in sent["text"]
    assert thread(conn, th)["last_change"]["tool"] == "adjust"
    inbound(client, resend, from_=OWNER, text="Undo.", to=to(th))  # the hint holds on the report's thread too
    assert mail_dispatch.UNDO_DONE.format(what="$2,000") in last_sent(resend)["text"]

    seed_business(conn, "biz_2", owner="other@example.com")
    seed_plan(conn, "biz_2", {"google_search": 900})
    th2 = mail_dispatch.open_thread(conn, "biz_2", "other@example.com", "Your Admiry report")
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status, body, thread_id) "
        "VALUES ('dg_2', 'biz_2', %s, 'daily', 'America/Los_Angeles', 'on', 'sent', %s, %s)",
        (DAY, Jsonb(BODY), th2["id"]),
    )
    plans_before = conn.execute("SELECT count(*) AS n FROM plan WHERE business_id = 'biz_2'").fetchone()["n"]
    inbound(client, resend, from_="other@example.com", text="Make it $1,500 a month", to=to(th2))
    sent = last_to(resend, "other@example.com")
    assert mail_dispatch.CHANGE_PLAN in sent["text"] and report_url("biz_2") in sent["text"]
    assert conn.execute("SELECT count(*) AS n FROM plan WHERE business_id = 'biz_2'").fetchone()["n"] == plans_before


def test_adjust_without_fields_is_unclear(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    inbound(client, resend, from_=OWNER, text="Change something", to=to(th))
    sent = last_sent(resend)
    assert mail_dispatch.UNCLEAR in sent["text"] and report_url(biz) in sent["text"]
    assert thread(conn, th)["unclear"] == 1 and outcome(conn)["intent"] == {"intent": "unclear"}


def test_unclear_twice_goes_to_a_person_with_the_link(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    inbound(client, resend, from_=OWNER, text="hmm what", to=to(th))
    assert mail_dispatch.UNCLEAR in last_sent(resend)["text"]
    inbound(client, resend, from_=OWNER, text="not sure", to=to(th))
    sent = last_to(resend, OWNER)
    assert sent["text"].startswith(mail_dispatch.HANDED) and report_url(biz) in sent["text"]
    assert thread(conn, th)["status"] == "operator" and outcome(conn)["outcome"] == "queued"


def test_other_goes_to_a_person_with_the_link(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    inbound(client, resend, from_=OWNER, text="Launch it now", to=to(th))
    sent = last_to(resend, OWNER)
    assert sent["text"].startswith(mail_dispatch.HANDED) and report_url(biz) in sent["text"]
    assert outcome(conn)["intent"] == {"intent": "other"} and outcome(conn)["outcome"] == "queued"
    assert any(m["to"] == ["ops@example.com"] for m in resend.sent)


def test_operator_thread_stays_queued_then_reopens(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    conn.execute("UPDATE email_thread SET status = 'operator' WHERE id = %s", (th["id"],))
    n, asked = len(resend.sent), len(fake.mail_calls)
    inbound(client, resend, from_=OWNER, text=QUESTION, to=to(th))
    assert outcome(conn)["outcome"] == "queued" and len(resend.sent) == n and len(fake.mail_calls) == asked
    conn.execute("UPDATE email_thread SET status = 'open' WHERE id = %s", (th["id"],))  # the person answered
    inbound(client, resend, from_=OWNER, text=AGAIN, to=to(th))
    assert "41 clicks on Monday." in last_sent(resend)["text"]
    assert fake.answer_calls[-1]["material"]["report"] == BODY


def test_support_field_goes_to_a_person(client, conn, resend, fake, support_on):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    inbound(client, resend, from_=OWNER, text=SUPPORT, to=to(th))
    assert outcome(conn)["intent"] == {"intent": "support"} and outcome(conn)["outcome"] == "queued"
    assert thread(conn, th)["status"] == "operator" and fake.answer_calls == []
    ticket = conn.execute("SELECT reason, email_thread_id FROM support_ticket").fetchone()
    assert ticket["reason"] == "human" and ticket["email_thread_id"] == th["id"]


def test_a_reader_without_authority_is_told_it_may_only_read(client, conn, resend, fake):
    biz = planned(client, conn, resend)
    th = digest_thread(conn, biz)
    conn.execute("UPDATE business SET owner_email = NULL WHERE id = %s", (biz,))  # the claim released
    conn.execute("UPDATE email_thread SET counterpart = 'stranger@example.com' WHERE id = %s", (th["id"],))
    inbound(client, resend, from_="stranger@example.com", text=QUESTION, to=to(th))
    assert mail_dispatch.READ_ONLY in last_to(resend, "stranger@example.com")["text"]
    assert fake.answer_calls == []


def test_new_sentences_pass_owner_words():
    for s in (mail_dispatch.CHANGE_PLAN,):
        assert not JARGON.search(s) and not ACCOUNT_JARGON.search(s) and not MONEY_JARGON.search(s)
