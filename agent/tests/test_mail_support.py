"""The e-mail channel's support paths (spec 2026-09-30 §4.4, §5.3, D146): a request for a person after the plan, in the
middle of the consultation, and on a ready one; the owner's reply on a handed thread; email-reply on a ticket's thread;
and today's hand-off, untouched, while support is off."""

import httpx
import psycopg
import pytest

from agent import benchmark, claims, jobs, mail_dispatch, mail_grants, mail_in, models, ops, support, support_mail
from agent.tests.test_mail_dispatch import (  # noqa: F401 - fake is a fixture used by name
    CUE,
    OPENING,
    OWNER,
    SITE_WORDS,
    claim_and_plan,
    fake,
    last_to,
    mail,
    reply_to,
    thread_row,
    walk_to_ready,
)
from agent.tests.test_mail_in import inbound, outcome
from agent.tests.test_support import open_

OPS = ["ops@example.com"]


def notices(sent) -> list:
    return [m for m in sent if m["to"] == OPS]


def mid_consultation(client, conn, resend) -> dict:
    """The owner's consultation by e-mail, begun and its website given (the fixture site, read: plan
    2026-10-05-agent-a3p-5-1): the website's words are read without the model, so no request for a person is looked
    for in them; the next question's are. The thread."""
    inbound(client, resend, from_=OWNER, text=OPENING)
    t = thread_row(conn)
    inbound(client, resend, from_=OWNER, text=SITE_WORDS, to=reply_to(t))
    return t


def planned_thread(client, conn, resend, fake) -> dict:  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    claim_and_plan(conn, t["business_id"])
    fake.mails["I want to talk to a person"] = mail("other", support="human", support_summary="Wants a person.")
    return t


def test_after_the_plan_asking_for_a_person_is_a_ticket_on_the_thread(client, conn, support_on, fake):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, support_on, fake)
    inbound(client, support_on, from_=OWNER, text="I want to talk to a person", to=reply_to(t))
    assert mail_dispatch.HANDED in last_to(support_on, OWNER)["text"] and outcome(conn)["outcome"] == "queued"
    ticket = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (ticket["source"], ticket["reason"], ticket["email_thread_id"], ticket["contact_email"]) == (
        "email",
        "human",
        t["id"],
        OWNER,
    )
    assert len(notices(support_on.sent)) == 1
    assert notices(support_on.sent)[0]["subject"].startswith(f"[Admiry] ticket {ticket['id']}")
    assert thread_row(conn)["status"] == "operator"


def test_mid_consultation_the_answer_s_read_opens_the_ticket_and_only_handed_goes_out(client, conn, support_on, fake):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    fake.reads["Can someone just call me?"] = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=False,
        support="human",
        support_summary="x",
        services=[],
    )
    t = mid_consultation(client, conn, support_on)
    before = len(support_on.sent)
    inbound(client, support_on, from_=OWNER, text="Can someone just call me?", to=reply_to(t))
    to_owner = [m for m in support_on.sent[before:] if m["to"] == [OWNER]]
    assert len(to_owner) == 1 and mail_dispatch.HANDED in to_owner[0]["text"]  # no second acknowledgement
    ticket = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (ticket["email_thread_id"], ticket["status"], ticket["contact_email"]) == (t["id"], "open", OWNER)
    assert len(notices(support_on.sent[before:])) == 1


def test_a_ready_consultation_reads_the_words_first(client, conn, support_on, fake):  # noqa: F811 - fixture used by name
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, support_on)
    fake.supports["Please, a human"] = models.SupportRead(support="human", support_summary="Wants a person.")
    inbound(client, support_on, from_=OWNER, text="Please, a human", to=reply_to(t))
    assert outcome(conn)["intent"] == {"intent": "support"}
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1


def test_the_owner_s_reply_on_a_handed_thread_lands_on_the_ticket(client, conn, support_on, fake):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, support_on, fake)
    inbound(client, support_on, from_=OWNER, text="I want to talk to a person", to=reply_to(t))
    tid = conn.execute("SELECT id FROM support_ticket").fetchone()["id"]
    support.reply(conn, tid, "Hi, this is Admiry.", actor="ops:cli")
    support_mail.flush(tid)
    inbound(client, support_on, from_=OWNER, text="Thanks, one more question", to=reply_to(t))
    assert outcome(conn)["outcome"] == "queued"
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "open"
    body = conn.execute("SELECT body FROM ticket_event WHERE kind = 'reply_in'").fetchone()["body"]
    assert body == "Thanks, one more question" and len(notices(support_on.sent)) == 2


def test_email_reply_answers_the_ticket_and_closes_it_once_sent(client, conn, support_on, fake, tmp_path):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, support_on, fake)
    inbound(client, support_on, from_=OWNER, text="I want to talk to a person", to=reply_to(t))
    f = tmp_path / "reply.txt"
    f.write_text("Hi, this is Admiry.\n\nHere is what we found.")
    assert ops.main(["email-reply", t["id"], "--text-file", str(f)]) == 0
    assert conn.execute("SELECT status, resolution FROM support_ticket").fetchone() == {
        "status": "closed",
        "resolution": "resolved",
    }
    assert thread_row(conn)["status"] == "open"
    assert last_to(support_on, OWNER)["text"].startswith("Hi, this is Admiry.")
    row = conn.execute("SELECT result FROM agent_action WHERE tool = 'ops.email_reply'").fetchone()
    assert row["result"]["outcome"] == "sent"


def test_with_support_off_a_hand_off_is_today_s(client, conn, resend, fake):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, resend, fake)
    inbound(client, resend, from_=OWNER, text="Launch it now", to=reply_to(t))  # intent other: a person reads it
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0
    assert notices(resend.sent) and notices(resend.sent)[-1]["subject"].startswith("[Admiry] e-mail thread")


# ---------------------------------------------------------------- fix round 1 (the controller's four rulings)


def fail_once(monkeypatch, name: str) -> None:
    """support.<name> raises a database error the first time it is called, and works afterwards."""
    real, calls = getattr(support, name), []

    def flaky(*args, **kwargs):
        calls.append(name)
        if len(calls) == 1:
            raise psycopg.OperationalError("the database went away")
        return real(*args, **kwargs)

    monkeypatch.setattr(support, name, flaky)


def last_inbound_id(conn) -> str:
    return conn.execute(
        "SELECT id FROM email_message WHERE direction = 'in' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()["id"]


@pytest.mark.parametrize("breaker", ["limit", "loop"])
def test_a_breaker_caught_message_is_still_a_reply_on_the_ticket(client, conn, support_on, fake, monkeypatch, breaker):  # noqa: F811 - fixture used by name
    """mail_in's loop and limit breakers call to_operator for a thread that already waits for a person: the message is
    still the owner's reply on the ticket (spec §4.4), so an answered ticket is open again, with its notice."""
    t = planned_thread(client, conn, support_on, fake)
    inbound(client, support_on, from_=OWNER, text="I want to talk to a person", to=reply_to(t))
    tid = conn.execute("SELECT id FROM support_ticket").fetchone()["id"]
    support.reply(conn, tid, "Hi, this is Admiry.", actor="ops:cli")
    support_mail.flush(tid)
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "answered"
    notices_before = len(notices(support_on.sent))
    if breaker == "limit":
        monkeypatch.setenv("AGENT_EMAIL_PER_THREAD_HOUR", "3")  # the thread is over its hourly limit
        words = "Thanks, one more question"
    else:
        words = "I want to talk to a person"  # the same words again: the loop breaker's first rule
    inbound(client, support_on, from_=OWNER, text=words, to=reply_to(t))
    assert outcome(conn)["outcome"] == "queued" and outcome(conn)["intent"] == {"intent": breaker}
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "open"
    body = conn.execute("SELECT body FROM ticket_event WHERE kind = 'reply_in'").fetchone()["body"]
    assert body == words and len(notices(support_on.sent)) == notices_before + 1


def test_a_failed_email_reply_is_not_sent_twice_by_running_it_again(client, conn, support_on, fake, tmp_path, capsys):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, support_on, fake)
    inbound(client, support_on, from_=OWNER, text="I want to talk to a person", to=reply_to(t))
    tid = conn.execute("SELECT id FROM support_ticket").fetchone()["id"]
    f = tmp_path / "reply.txt"
    f.write_text("Hi, this is Admiry.\n\nHere is what we found.")
    support_on.fail = httpx.Response(500, json={"message": "down"})
    assert ops.main(["email-reply", t["id"], "--text-file", str(f)]) == 1
    said = capsys.readouterr().err
    assert "queued" in said and "support-mail job retries it" in said and "do not run email-reply again" in said
    support_on.fail = None  # Resend is back
    posts = len(support_on.sent)
    assert ops.main(["email-reply", t["id"], "--text-file", str(f)]) == 1  # run again anyway: refused, nothing sent
    assert "an earlier reply is still waiting to be sent" in capsys.readouterr().err
    assert len(support_on.sent) == posts
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'reply_out'").fetchone()["n"] == 1
    conn.execute("UPDATE ticket_event SET created_at = created_at - interval '10 minutes' WHERE ticket_id = %s", (tid,))
    assert jobs.support_mail(conn, 0, 1)["sent"] == 1  # the support-mail job's retry sends the one reply
    assert conn.execute("SELECT status, resolution FROM support_ticket").fetchone() == {
        "status": "closed",
        "resolution": "resolved",
    }
    posted = [m for m in support_on.sent if m["to"] == [OWNER] and m["text"].startswith("Hi, this is Admiry.")]
    assert len(posted) == 2 and len({m["headers"]["Message-ID"] for m in posted}) == 1  # the failed try, then its retry
    assert conn.execute("SELECT outcome FROM email_message WHERE direction = 'out' AND sent_by = 'ops'").fetchall() == [
        {"outcome": "sent"}
    ]  # one message in the thread: one copy reached the owner


def test_a_database_error_opening_the_ticket_leaves_the_thread_as_it_was(client, conn, support_on, fake, monkeypatch):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, support_on, fake)
    fail_once(monkeypatch, "open_or_append")
    sent = len(support_on.sent)
    inbound(client, support_on, from_=OWNER, text="I want to talk to a person", to=reply_to(t))
    assert outcome(conn)["outcome"] == "received"  # the crash leaves the row for email-retry
    assert thread_row(conn)["status"] == "open"  # the thread is as it was ...
    assert len(support_on.sent) == sent  # ... and nothing went out: no HANDED, no notice
    mail_in.process(last_inbound_id(conn))  # the retry redoes the whole hand-off
    assert outcome(conn)["outcome"] == "queued" and thread_row(conn)["status"] == "operator"
    ticket = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (ticket["email_thread_id"], ticket["status"], ticket["contact_email"]) == (t["id"], "open", OWNER)
    handed = [m for m in support_on.sent[sent:] if m["to"] == [OWNER] and mail_dispatch.HANDED in m["text"]]
    assert len(handed) == 1 and len(notices(support_on.sent[sent:])) == 1


def test_a_database_error_binding_the_ticket_leaves_the_thread_as_it_was(client, conn, support_on, fake, monkeypatch):  # noqa: F811 - fixture used by name
    """The mid-consultation path: the answer's own read opened the ticket, to_operator binds it to the thread."""
    benchmark.seed(conn)
    fake.reads["Can someone just call me?"] = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=False,
        support="human",
        support_summary="x",
        services=[],
    )
    t = mid_consultation(client, conn, support_on)
    fail_once(monkeypatch, "bind_thread")
    sent = len(support_on.sent)
    inbound(client, support_on, from_=OWNER, text="Can someone just call me?", to=reply_to(t))
    assert outcome(conn)["outcome"] == "received"
    assert thread_row(conn)["status"] == "open"  # the thread is as it was ...
    assert len(support_on.sent) == sent  # ... and nothing went out: no HANDED, no notice
    mail_in.process(last_inbound_id(conn))
    assert outcome(conn)["outcome"] == "queued" and thread_row(conn)["status"] == "operator"
    ticket = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (ticket["email_thread_id"], ticket["status"], ticket["contact_email"]) == (t["id"], "open", OWNER)
    handed = [m for m in support_on.sent[sent:] if m["to"] == [OWNER] and mail_dispatch.HANDED in m["text"]]
    assert len(handed) == 1 and len(notices(support_on.sent[sent:])) == 1


def test_an_unclear_hand_off_carries_the_owner_s_words(client, conn, support_on, fake):  # noqa: F811 - fixture used by name
    t = planned_thread(client, conn, support_on, fake)
    inbound(client, support_on, from_=OWNER, text="hmm", to=reply_to(t))
    assert thread_row(conn)["unclear"] == 1
    words = "hmm, I am not sure what to ask"
    inbound(client, support_on, from_=OWNER, text=words, to=reply_to(t))
    assert outcome(conn)["outcome"] == "queued" and outcome(conn)["intent"] == {"intent": "unclear"}
    assert conn.execute("SELECT body FROM ticket_event WHERE kind = 'owner_words'").fetchone()["body"] == words
    assert words in notices(support_on.sent)[-1]["text"]  # the ops notice quotes them too


# ---------------------------------------------------------------- the final review: a reply follows its thread (F1)

ASK = "Can someone just call me?"


def reply_by_ops(tmp_path, thread_id: str) -> int:
    f = tmp_path / "reply.txt"
    f.write_text("Hi, this is Admiry.\n\nHere is what we found.")
    return ops.main(["email-reply", thread_id, "--text-file", str(f)])


def replies_to(sent) -> list:
    """Who each of the person's replies was sent to."""
    return [m["to"] for m in sent if m["text"].startswith("Hi, this is Admiry.")]


def test_an_allowed_address_asking_mid_consultation_is_the_one_answered(client, conn, support_on, fake, tmp_path):  # noqa: F811 - fixture used by name
    """An address the owner allowed asks for a person in the middle of the owner's consultation: the answer's own read
    opens the ticket before the channel binds the thread, and the business alone would name its owner. The ticket, the
    ops notice and the person's reply all name the address that asked."""
    benchmark.seed(conn)
    fake.reads[ASK] = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=False,
        support="human",
        support_summary="x",
        services=[],
    )
    t = mid_consultation(client, conn, support_on)  # the owner starts a consultation by e-mail
    with conn.transaction():
        claims.claim(conn, t["business_id"], OWNER)  # ... and claims the business; the consultation goes on
    mail_grants.add(conn, t["business_id"], CUE, scopes=("read", "manage"), via="connect", created_by=OWNER)
    before = len(support_on.sent)
    inbound(client, support_on, from_=CUE, text=ASK, to=reply_to(t))
    assert outcome(conn)["outcome"] == "queued"
    ticket = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (ticket["contact_email"], ticket["email_thread_id"], ticket["status"]) == (CUE, t["id"], "open")
    new = support_on.sent[before:]
    assert [m["to"] for m in new if mail_dispatch.HANDED in m["text"]] == [[CUE]]
    assert [f"contact: {CUE}." in m["text"] for m in notices(new)] == [True]  # one notice, and it names who asked
    assert reply_by_ops(tmp_path, t["id"]) == 0
    assert replies_to(support_on.sent) == [[CUE]]


@pytest.mark.parametrize("asker", [OWNER, CUE])
def test_a_reply_never_goes_to_an_address_whose_grant_was_revoked(client, conn, support_on, fake, tmp_path, asker):  # noqa: F811 - fixture used by name
    """M1 with support on (test_mail_dispatch's support-off twin): the owner, or an address the owner allowed, hands the
    owner's thread to a person; the allowed address writes on it; the owner revokes the address. The reply's address is
    resolved when it is sent, the channel's rule: the owner, never the revoked address, whoever handed the thread
    over."""
    t = planned_thread(client, conn, support_on, fake)
    g = mail_grants.add(conn, t["business_id"], CUE, scopes=("read", "manage"), via="connect", created_by=OWNER)
    inbound(client, support_on, from_=asker, text="I want to talk to a person", to=reply_to(t))
    assert thread_row(conn)["status"] == "operator"
    inbound(client, support_on, from_=CUE, text="One more detail for the team", to=reply_to(t))
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'reply_in'").fetchone()["n"] == 1
    assert mail_grants.revoke(conn, g["id"], t["business_id"])
    assert reply_by_ops(tmp_path, t["id"]) == 0
    assert replies_to(support_on.sent) == [[OWNER]]


@pytest.mark.parametrize("which", ["first", "second"])
def test_a_ticket_with_two_threads_answers_each_on_its_own(client, conn, support_on, fake, tmp_path, which):  # noqa: F811 - fixture used by name
    """The owner's homepage ask made the ticket (its acknowledgement's thread is its first); an allowed address then
    handed the owner's e-mail thread over, and the same ticket took it as a second thread. email-reply on either thread
    answers on that thread, to that thread's own reply address."""
    t = planned_thread(client, conn, support_on, fake)
    open_(conn, business_id=t["business_id"])  # the homepage's ask: the owner is the contact
    first = conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"]
    mail_grants.add(conn, t["business_id"], CUE, scopes=("read", "manage"), via="connect", created_by=OWNER)
    inbound(client, support_on, from_=CUE, text="I want to talk to a person", to=reply_to(t))
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    held = conn.execute("SELECT data FROM ticket_event WHERE kind = 'thread'").fetchall()
    assert held == [{"data": {"thread_id": t["id"]}}]  # the same ticket holds both threads
    thread_id, to = (first, OWNER) if which == "first" else (t["id"], CUE)
    assert reply_by_ops(tmp_path, thread_id) == 0
    token = conn.execute("SELECT token FROM email_thread WHERE id = %s", (thread_id,)).fetchone()["token"]
    reply = [m for m in support_on.sent if m["text"].startswith("Hi, this is Admiry.")]
    assert [m["to"] for m in reply] == [[to]] and reply[0]["reply_to"] == f"hello+{token}@agent.test"
    assert conn.execute("SELECT thread_id FROM email_message WHERE sent_by = 'ops'").fetchall() == [
        {"thread_id": thread_id}
    ]
