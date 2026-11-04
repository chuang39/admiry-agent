"""Ticket mail (spec 2026-09-30 §5, D141, D142): the outbox sent after commit, the acknowledgement on its own thread, the
ops notice, the channel-off Reply-To, retries and their cap, a reply that closes when sent, the support-mail job, no
mail while support is off, and the four claim sites (the homepage, the claim link, the OAuth consent, the e-mail step)
that send the acknowledgement a ticket was waiting to send."""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent import benchmark, claims, db, jobs, mail_out, support, support_mail
from agent.tests.conftest import TEST_DB_URL
from agent.tests.test_home_api import IP, fake, turn  # noqa: F401 - fake is a fixture used by name
from agent.tests.test_mail_dispatch import CUE, walk_to_ready
from agent.tests.test_mail_dispatch import fake as mail_fake  # noqa: F401 - the e-mail channel's fake, used by name
from agent.tests.test_mail_web import link_of
from agent.tests.test_oauth_consent import _sign_in, _txn
from agent.tests.test_oauth_http import register
from agent.tests.test_support import biz, open_


def states(conn) -> set:
    return {r["mail_state"] for r in conn.execute("SELECT mail_state FROM ticket_event WHERE mail_state IS NOT NULL")}


def ticket(conn) -> tuple:
    row = conn.execute("SELECT status, resolution FROM support_ticket").fetchone()
    return row["status"], row["resolution"]


def stuck_reply(conn, support_on) -> str:
    """An open ticket (acknowledgement and notice sent) answered by a person's reply that asks to close, which Resend
    refused: the reply waits, failed, in the outbox, and the ticket is `answered`. Returns the ticket's id."""
    biz(conn)
    tid = open_(conn)["ticket_id"]
    support_mail.flush(tid)
    support_on.fail = httpx.Response(503, json={"message": "down"})
    support.reply(conn, tid, "Here is the answer.", actor="ops:cli", close=True)
    assert support_mail.flush(tid) == {"sent": 0, "failed": 1}
    assert ticket(conn) == ("answered", None)
    return tid


def test_flush_sends_the_acknowledgement_and_the_notice_once(conn, support_on):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    assert support_mail.flush(tid) == {"sent": 2, "failed": 0}
    ack, notice = support_on.sent
    token = conn.execute("SELECT token FROM email_thread").fetchone()["token"]
    assert ack["to"] == ["owner@example.com"] and ack["subject"] == f"We got your message ({tid})"
    assert support_mail.ACK in ack["text"] and "Can a person call me?" in ack["text"]
    assert ack["reply_to"] == f"hello+{token}@agent.test"
    assert notice["to"] == ["ops@example.com"] and notice["subject"] == f"[Admiry] ticket {tid} (human, home)"
    assert "reply_to" not in notice
    line = f"Open it at http://testserver/ops/tasks/{tid}"  # spec 2026-10-12 §5: the back office's task list
    assert line in notice["text"].split("\n\n")
    assert states(conn) == {"sent"}
    # the Resend Idempotency-Key: the notice's own event id; the acknowledgement's out row id, derived from its event id
    ids = {r["kind"]: r["id"] for r in conn.execute("SELECT id, kind FROM ticket_event WHERE mail_state IS NOT NULL")}
    assert support_on.keys == ["em_" + ids["ack"].removeprefix("te_"), ids["notice"]]
    assert support_mail.flush(tid) == {"sent": 0, "failed": 0}


def test_with_the_channel_off_the_acknowledgement_names_a_person(conn, support_on, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    monkeypatch.setenv("AGENT_SUPPORT_REPLY_TO", "support@example.com")
    biz(conn)
    support_mail.flush(open_(conn)["ticket_id"])
    assert support_on.sent[0]["reply_to"] == "support@example.com" and len(support_on.sent) == 2


def test_a_failure_is_retried_by_the_job_on_the_same_row(conn, support_on):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    support_on.fail = httpx.Response(503, json={"message": "down"})
    assert support_mail.flush(tid) == {"sent": 0, "failed": 2}
    conn.execute("UPDATE ticket_event SET created_at = created_at - interval '10 minutes'")
    support_on.fail = None
    assert jobs.support_mail(conn, 0, 1)["sent"] == 2
    assert conn.execute("SELECT count(*) AS n FROM email_message WHERE direction = 'out'").fetchone()["n"] == 1
    assert states(conn) == {"sent"}


def test_a_request_s_flush_leaves_a_failed_row_to_the_job(conn, support_on):
    """Spec §5.2: every request that changes the ticket flushes it, so during a Resend outage each of the owner's
    messages would cost another try. A request's flush sends pending rows only: a failed row keeps its tries for the
    hourly job, which retries it."""
    biz(conn)
    tid = open_(conn)["ticket_id"]
    support_on.fail = httpx.Response(503, json={"message": "down"})
    assert support_mail.flush(tid) == {"sent": 0, "failed": 2}
    open_(conn, words="Hello? Is anyone there?")  # the owner's next message on the ticket, and its request's flush
    assert support_mail.flush(tid) == {"sent": 0, "failed": 0}
    rows = conn.execute("SELECT mail_state, mail_attempts FROM ticket_event WHERE mail_state IS NOT NULL").fetchall()
    assert rows and all(r == {"mail_state": "failed", "mail_attempts": 1} for r in rows)
    conn.execute("UPDATE ticket_event SET created_at = created_at - interval '10 minutes'")
    support_on.fail = None
    assert jobs.support_mail(conn, 0, 1)["sent"] == 2
    assert states(conn) == {"sent"}


def test_a_notice_s_retry_is_the_same_message(conn, support_on):
    """A retry must be byte for byte the try before, under the same Idempotency-Key: the notice's Message-ID comes from
    its event id, like the thread mail's row id, not a new one per try."""
    biz(conn)
    tid = open_(conn)["ticket_id"]
    support_on.fail = httpx.Response(503, json={"message": "down"})
    support_mail.flush(tid)
    conn.execute("UPDATE ticket_event SET created_at = created_at - interval '10 minutes'")
    jobs.support_mail(conn, 0, 1)  # the job's retry, Resend still down
    tries = [(m, k) for m, k in zip(support_on.sent, support_on.keys) if m["to"] == ["ops@example.com"]]
    assert len(tries) == 2 and tries[0] == tries[1]
    notice = conn.execute("SELECT id FROM ticket_event WHERE kind = 'notice'").fetchone()["id"]
    assert tries[0][0]["headers"]["Message-ID"] == f"<{notice}@agent.test>"


def test_the_no_contact_close_leaves_a_ticket_a_claim_just_opened(conn, support_on, monkeypatch):
    """The job selects a ticket that waited 7 days for an address, and the owner signs in before the job closes it: the
    claim opened it, with its acknowledgement and notice. The job closes a ticket only while, under its lock, it is
    still waiting."""
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, draft=True)["ticket_id"]
    conn.execute("UPDATE support_ticket SET created_at = now() - interval '8 days'")
    real = support.set_status

    def a_claim_lands_between_the_select_and_the_close(c, ticket_id, status, **kw):
        with db.connect(TEST_DB_URL) as other, other.transaction():
            claims.claim(other, "biz_1", "owner@example.com")
        return real(c, ticket_id, status, **kw)

    monkeypatch.setattr(support, "set_status", a_claim_lands_between_the_select_and_the_close)
    assert jobs.support_mail(conn, 0, 1)["no_contact"] == 0
    assert conn.execute("SELECT status, resolution, contact_email FROM support_ticket").fetchone() == {
        "status": "open",
        "resolution": None,
        "contact_email": "owner@example.com",
    }
    assert support_mail.flush(tid) == {"sent": 2, "failed": 0}
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_tries_stop_at_the_cap(conn, support_on):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    support_on.fail = httpx.Response(503, json={"message": "down"})
    for _ in range(support_mail.MAIL_ATTEMPTS + 2):
        support_mail.flush(tid, retry=True)  # the job's flush: a request's never retries a failed row
    rows = conn.execute("SELECT mail_state, mail_attempts FROM ticket_event WHERE mail_state IS NOT NULL").fetchall()
    assert rows and all(r == {"mail_state": "failed", "mail_attempts": support_mail.MAIL_ATTEMPTS} for r in rows)


def test_a_reply_that_closes_closes_when_sent_and_not_before(conn, support_on):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    support_mail.flush(tid)
    support_on.fail = httpx.Response(503, json={"message": "down"})
    support.reply(conn, tid, "Here is the answer.\n\nBest, Admiry", actor="ops:cli", close=True)
    support_mail.flush(tid)
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "answered"
    assert conn.execute("SELECT status FROM email_thread").fetchone()["status"] == "operator"
    support_on.fail = None
    support_mail.flush(tid, retry=True)  # the support-mail job's retry
    assert conn.execute("SELECT status, resolution FROM support_ticket").fetchone() == {
        "status": "closed",
        "resolution": "resolved",
    }
    assert conn.execute("SELECT status FROM email_thread").fetchone()["status"] == "open"
    reply = support_on.sent[-1]
    assert reply["text"].startswith("Here is the answer.") and reply["subject"] == f"Re: Your request to Admiry ({tid})"
    assert reply["to"] == ["owner@example.com"]  # a homepage ticket's reply reaches its contact, on its own thread
    assert (
        conn.execute("SELECT sent_by FROM email_message ORDER BY created_at DESC LIMIT 1").fetchone()["sent_by"]
        == "ops"
    )


def test_a_late_failure_never_overwrites_a_row_another_flush_marked_sent(conn, support_on, monkeypatch):
    biz(conn)
    tid = open_(conn)["ticket_id"]

    def rival_flush_wins_first(settings, **kw):
        # while this send is in flight another flush sends the notice and marks it; this one only saw a failure
        conn.execute("UPDATE ticket_event SET mail_state = 'sent' WHERE kind = 'notice'")
        return False

    monkeypatch.setattr(mail_out, "send_plain", rival_flush_wins_first)
    assert support_mail.flush(tid) == {"sent": 1, "failed": 1}  # the acknowledgement went; this flush lost the notice
    assert conn.execute("SELECT mail_state FROM ticket_event WHERE kind = 'notice'").fetchone()["mail_state"] == "sent"


def test_a_delayed_close_never_closes_a_ticket_the_owner_reopened(conn, support_on):
    tid = stuck_reply(conn, support_on)
    open_(conn, words="Still not working")  # the owner writes again: the ticket is open, with one more notice
    assert ticket(conn) == ("open", None)
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'notice'").fetchone()["n"] == 2
    support_on.fail = None
    assert support_mail.flush(tid, retry=True) == {"sent": 2, "failed": 0}  # the job: the reply, then the new notice
    sent = conn.execute("SELECT mail_state FROM ticket_event WHERE kind = 'reply_out'").fetchone()["mail_state"]
    assert sent == "sent"
    assert ticket(conn) == ("open", None)
    assert conn.execute("SELECT status FROM email_thread").fetchone()["status"] == "operator"


def a_backend_waits_on_a_lock(conn, timeout=5.0) -> bool:
    """True once another backend of this database waits for a lock. Ask from a connection with no transaction open:
    pg_stat_activity is a snapshot fixed at the first read of a transaction, so inside one it never changes."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
        ).fetchone()
        if row["n"]:
            return True
        time.sleep(0.01)
    return False


def test_a_delayed_close_waits_for_an_append_in_flight_and_then_stands_down(conn, support_on):
    """The ticket is locked before the close is decided: an owner's append still in flight (its transaction holds the
    ticket) is waited for, so the close sees it. The append's transaction stays open until the close is provably
    waiting on the ticket's lock, so every run interleaves the two the same way."""
    tid = stuck_reply(conn, support_on)
    support_on.fail = None
    with db.connect(TEST_DB_URL) as a, ThreadPoolExecutor(max_workers=1) as pool:
        with a.transaction():
            open_(a, words="Still not working")  # answered -> open; the ticket stays locked until this commits
            flushed = pool.submit(support_mail.flush, tid, retry=True)  # the job's retry of the failed reply
            assert a_backend_waits_on_a_lock(conn), "the close never reached the ticket's lock"
        # the reply went out; the append's notice was not committed yet, so this flush never saw it
        assert flushed.result(timeout=10) == {"sent": 1, "failed": 0}
    assert ticket(conn) == ("open", None)


def test_a_delayed_close_leaves_a_ticket_a_person_reopened_open(conn, support_on):
    tid = stuck_reply(conn, support_on)
    support.set_status(conn, tid, "open", actor="ops:cli")  # a person keeps it open after all: no owner words at all
    support_on.fail = None
    assert support_mail.flush(tid, retry=True) == {"sent": 1, "failed": 0}  # the job's retry of the failed reply
    assert ticket(conn) == ("open", None)
    assert conn.execute("SELECT status FROM email_thread").fetchone()["status"] == "operator"


@pytest.mark.parametrize("writes", ["owner_words", "reply_in"])
def test_a_delayed_close_does_not_close_over_the_owner_s_later_words(conn, support_on, writes):
    tid = stuck_reply(conn, support_on)
    if writes == "owner_words":
        open_(conn, words="Still not working")
    else:  # the owner answers by e-mail, on the ticket's thread
        thread_id = conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"]
        support.on_inbound(conn, thread_id, {"id": "em_in", "from_addr": "owner@example.com"}, "Still not working")
    # support.reply refuses a second reply while the first still waits in the outbox (both would reach the owner), so the
    # state this pins -- answered again by a newer reply that does not ask to close, the closing one still to be sent --
    # is built directly: what is under test is the delayed close's own guard, whatever led here
    conn.execute(
        "INSERT INTO ticket_event (id, ticket_id, kind, actor, via, body, data, mail_state, created_at) "
        "VALUES ('te_second', %s, 'reply_out', 'ops:cli', 'ops', 'Second answer.', %s, 'pending', clock_timestamp())",
        (tid, Jsonb({"close": False})),
    )
    conn.execute("UPDATE support_ticket SET status = 'answered' WHERE id = %s", (tid,))
    assert ticket(conn) == ("answered", None)
    support_on.fail = None
    support_mail.flush(tid, retry=True)  # the job's retry: the failed closing reply, then the pending one
    assert ticket(conn) == ("answered", None)
    replies = conn.execute("SELECT mail_state FROM ticket_event WHERE kind = 'reply_out' ORDER BY created_at")
    assert [r["mail_state"] for r in replies] == ["sent", "sent"]


def test_while_support_is_off_the_job_still_keeps_the_retention_promises(conn, resend):
    biz(conn, claim_state="draft", owner=None)
    waiting = open_(conn, draft=True)["ticket_id"]  # no contact yet
    biz(conn, "biz_2")
    open_(conn, business_id="biz_2")  # an open ticket: its acknowledgement and notice wait in the outbox
    conn.execute("UPDATE support_ticket SET created_at = now() - interval '8 days' WHERE id = %s", (waiting,))
    conn.execute("UPDATE ticket_event SET created_at = created_at - interval '10 minutes'")  # old enough to resend
    conn.execute(
        "INSERT INTO support_ticket (id, business_id, source, reason, status, resolution, summary, snapshot, closed_at) "
        "VALUES ('tk_old', NULL, 'email', 'other', 'closed', 'resolved', 's', '{}', now() - interval '91 days'), "
        "('tk_kept', 'biz_2', 'home', 'help', 'closed', 'resolved', 's', '{}', now() - interval '91 days')"
    )
    assert jobs.support_mail(conn, 0, 1) == {"sent": 0, "no_contact": 1, "purged": 1, "resend": "off"}
    assert resend.sent == [] and states(conn) == {"pending"}  # nothing was sent; the rows wait for support to be on
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE mail_attempts > 0").fetchone()["n"] == 0
    assert conn.execute("SELECT status, resolution FROM support_ticket WHERE id = %s", (waiting,)).fetchone() == {
        "status": "closed",
        "resolution": "no_contact",
    }
    kept = {r["id"] for r in conn.execute("SELECT id FROM support_ticket")}
    assert "tk_old" not in kept and "tk_kept" in kept  # only a closed ticket with no business is deleted


def test_the_job_closes_old_waiting_tickets_and_purges_old_ones_without_a_business(conn, support_on):
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, draft=True)["ticket_id"]
    conn.execute("UPDATE support_ticket SET created_at = now() - interval '8 days'")
    conn.execute(
        "INSERT INTO support_ticket (id, business_id, source, reason, status, resolution, summary, snapshot, closed_at) "
        "VALUES ('tk_old', NULL, 'email', 'other', 'closed', 'resolved', 's', '{}', now() - interval '91 days')"
    )
    out = jobs.support_mail(conn, 0, 1)
    assert out["no_contact"] == 1 and out["purged"] == 1 and out["resend"] == "on"
    assert conn.execute("SELECT status, resolution FROM support_ticket WHERE id = %s", (tid,)).fetchone() == {
        "status": "closed",
        "resolution": "no_contact",
    }


def test_after_tool_flushes_the_ticket_a_result_names(conn, support_on):
    biz(conn)
    out = open_(conn)
    support_mail.after_tool({"data": {"support": support.public(out)}})
    support_mail.after_tool({"data": {"cards": {}}})  # no ticket: nothing
    assert len(support_on.sent) == 2


def test_the_homepage_claim_sends_the_acknowledgement_that_was_waiting(client, conn, support_on, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    biz_id = conn.execute("SELECT id FROM business").fetchone()["id"]
    open_(conn, business_id=biz_id, draft=True, ip="198.51.100.9")
    assert support_on.sent == []
    csrf = _sign_in(client, google)
    assert client.post("/api/claim", json={"csrf": csrf}, headers=IP).json()["ok"]
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_a_flush_while_support_is_off_touches_no_row_and_sends_nothing(conn, support_on, monkeypatch):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    monkeypatch.setenv("AGENT_SUPPORT", "off")
    assert support_mail.flush(tid) == {"sent": 0, "failed": 0}
    assert support_mail.flush(business_id="biz_1") == {"sent": 0, "failed": 0}
    rows = conn.execute("SELECT mail_state, mail_attempts FROM ticket_event WHERE mail_state IS NOT NULL").fetchall()
    assert len(rows) == 2 and all(r == {"mail_state": "pending", "mail_attempts": 0} for r in rows)
    assert support_on.sent == []
    monkeypatch.setenv("AGENT_SUPPORT", "on")
    assert support_mail.flush(tid) == {"sent": 2, "failed": 0}
    assert states(conn) == {"sent"}


def test_the_claim_link_sends_the_acknowledgement_that_was_waiting(client, conn, support_on, google):
    biz(conn, claim_state="draft", owner=None)
    open_(conn, draft=True)
    token = claims.new_link(conn, "biz_1")[0].split("claim=")[1]
    csrf = _sign_in(client, google)
    assert support_on.sent == []
    r = client.post("/connect/claim", data={"claim": token, "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_the_consent_allow_sends_the_acknowledgement_that_was_waiting(client, conn, support_on, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist", "idempotency_key": "i1"})
    biz_id = conn.execute("SELECT id FROM business").fetchone()["id"]
    open_(conn, business_id=biz_id, draft=True, ip="198.51.100.9")
    txn, _ = _txn(client, conn, register(client))
    csrf = _sign_in(client, google)
    assert support_on.sent == []
    form = {"txn": txn, "csrf": csrf, "business": f"website:{biz_id}", "scopes": ["read", "manage", "leads"]}
    assert client.post("/oauth/consent", data={**form, "decision": "allow"}, follow_redirects=False).status_code == 303
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_the_mail_step_sends_the_acknowledgement_that_was_waiting(client, conn, support_on, mail_fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    thread = walk_to_ready(client, conn, support_on, sender=CUE)
    token = link_of(support_on)
    open_(conn, business_id=thread["business_id"], draft=True, ip="198.51.100.9")
    before = len(support_on.sent)
    csrf = _sign_in(client, google)
    form = {"csrf": csrf, "allow": "yes", "scopes": ["read", "manage"]}
    r = client.post(f"/m/{token}", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/plan/pl_")
    assert [m["to"] for m in support_on.sent[before : before + 2]] == [["owner@example.com"], ["ops@example.com"]]
