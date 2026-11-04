"""Outbound mail (spec 2026-09-28 §5.8, D59, D60): from hello@, Reply-To with the thread token, the same thread,
the footer on every message, one recipient, no Auto-Submitted header, a failure recorded rather than raised."""

import httpx
import pytest

from agent import config, mail, mail_out
from agent.tests.conftest import JARGON


def thread(conn, **over) -> dict:
    row = {
        "id": "et_1",
        "business_id": None,
        "session_id": None,
        "counterpart": "owner@example.com",
        "token": "a" * 32,
        "subject": "More customers",
        "status": "open",
    }
    row.update(over)
    conn.execute(
        "INSERT INTO email_thread (id, business_id, session_id, counterpart, token, subject, status) "
        "VALUES (%(id)s, %(business_id)s, %(session_id)s, %(counterpart)s, %(token)s, %(subject)s, %(status)s)",
        row,
    )
    return row


def test_compose_text_and_html_carry_the_same_words_and_the_footer():
    text, html = mail_out.compose(
        ["Here is <the plan>.", "  ", "Reply with changes."], [("Your plan", "http://x/plan/pl_1")]
    )
    assert text == "Here is <the plan>.\n\nReply with changes.\n\nYour plan: http://x/plan/pl_1\n\n" + mail_out.FOOTER
    assert "<p>Here is &lt;the plan&gt;.</p>" in html and '<a href="http://x/plan/pl_1">Your plan</a>' in html
    assert mail_out.FOOTER in html and not JARGON.search(mail_out.FOOTER)


def test_reply_address_and_subject():
    assert mail_out.reply_address("hello@agent.test", "b" * 32) == "hello+" + "b" * 32 + "@agent.test"
    assert mail_out.reply_subject("More customers") == "Re: More customers"
    assert mail_out.reply_subject("RE: More customers") == "RE: More customers"
    assert mail_out.reply_subject(None) == "Your marketing plan"


def test_deliver_sends_in_thread_and_records_the_row(conn, resend):
    t = thread(conn)
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, from_addr, to_addrs, outcome) "
        "VALUES ('em_in', 'et_1', 'in', '<one@example.com>', 'owner@example.com', '{hello@agent.test}', 'answered')"
    )
    out = mail_out.deliver(
        conn,
        config.load(),
        thread=t,
        to="owner@example.com",
        paragraphs=["Hello."],
        links=[("Sign in", "http://testserver/m/tok")],
        others_on_copy=True,
    )
    assert out["outcome"] == "sent" and out["provider_id"] == "re_1" and out["message_id"].startswith("<em_")
    sent = resend.sent[0]
    assert sent["from"] == "Admiry <hello@agent.test>" and sent["to"] == ["owner@example.com"]
    assert sent["reply_to"] == "hello+" + "a" * 32 + "@agent.test" and sent["subject"] == "Re: More customers"
    assert (
        sent["headers"]["In-Reply-To"] == "<one@example.com>" and sent["headers"]["References"] == "<one@example.com>"
    )
    assert sent["headers"]["Message-ID"] == out["message_id"] and "Auto-Submitted" not in sent["headers"]
    assert mail_out.ONLY_YOU in sent["text"] and sent["text"].endswith(mail_out.FOOTER)
    assert "http://testserver/m/tok" in sent["text"]
    row = conn.execute("SELECT * FROM email_message WHERE direction = 'out'").fetchone()
    assert row["outcome"] == "sent" and row["provider_id"] == "re_1" and row["sent_by"] == "admiry"
    assert row["to_addrs"] == ["owner@example.com"]
    # the /m/ capability link is masked in the stored copy only (finding I7); the sent mail keeps the real token
    assert "http://testserver/m/…" in row["new_text"] and "tok" not in row["new_text"]
    assert row["new_text"] != sent["text"]
    # … and otherwise the whole sent body is what is stored
    masked = sent["text"].replace("http://testserver/m/tok", "http://testserver/m/…")
    assert row["new_text"] == mail_out._mask_link_token(sent["text"], config.load()) == masked
    assert conn.execute("SELECT last_out_at FROM email_thread WHERE id = 'et_1'").fetchone()["last_out_at"] is not None


def test_deliver_masks_only_the_stored_link_token(conn, resend):
    """A message with no /m/ link at all is stored unmasked -- _mask_link_token has nothing to do."""
    t = thread(conn)
    out = mail_out.deliver(conn, config.load(), thread=t, to="owner@example.com", paragraphs=["Hello."])
    row = conn.execute("SELECT new_text FROM email_message WHERE direction = 'out'").fetchone()
    assert row["new_text"] == out["text"] == "Hello.\n\n" + mail_out.FOOTER


def test_deliver_refuses_when_the_channel_is_off(conn, resend, monkeypatch):
    """RESEND_API_KEY stays set (prod compose gives the agent one for the website) but AGENT_EMAIL_INBOUND=off
    must still refuse to send -- finding I1."""
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    t = thread(conn)
    out = mail_out.deliver(conn, config.load(), thread=t, to="owner@example.com", paragraphs=["Hello."])
    assert out["outcome"] == "failed" and resend.sent == []
    assert conn.execute("SELECT outcome FROM email_message").fetchone()["outcome"] == "failed"


def test_send_plain_refuses_when_the_channel_is_off(resend, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    ok = mail_out.send_plain(
        config.load(), to="ops@example.com", subject="s", paragraphs=["hi"], idempotency_key="off-1"
    )
    assert ok is False and resend.sent == []


def test_deliver_records_a_failure_instead_of_raising(conn, resend):
    t = thread(conn)
    resend.fail = httpx.Response(422, json={"message": "The from address is not verified"})
    out = mail_out.deliver(conn, config.load(), thread=t, to="owner@example.com", paragraphs=["Hello."])
    assert out["outcome"] == "failed" and out["provider_id"] is None
    assert conn.execute("SELECT outcome FROM email_message").fetchone()["outcome"] == "failed"


def test_deliver_without_a_key_is_a_failed_row(conn, resend, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    monkeypatch.delenv("RESEND_API_KEY")
    t = thread(conn)
    out = mail_out.deliver(conn, config.load(), thread=t, to="owner@example.com", paragraphs=["Hello."])
    assert out["outcome"] == "failed" and resend.sent == []


def test_send_errors_are_readable_and_never_carry_the_key(resend):
    """Through mail.send, the one client (P16): its MailError is ours, and a 2xx without an id is one too."""
    assert mail_out.MailError is mail.MailError
    payload = {"to": ["a@b"], "subject": "s", "text": "t"}
    resend.fail = httpx.Response(500, text="boom")
    with pytest.raises(mail_out.MailError) as e:
        mail_out.send(payload, idempotency_key="em_1")
    assert "500" in str(e.value) and "re_test_x" not in str(e.value)
    resend.fail = httpx.Response(200, json={})
    with pytest.raises(mail_out.MailError, match="without an id"):
        mail_out.send(payload, idempotency_key="em_2")


def test_send_hands_every_field_to_the_one_client(monkeypatch):
    """P16: mail_out.send is a thin call into mail.send -- the payload's fields and the Idempotency-Key, nothing lost
    or added on the way."""
    seen = {}
    monkeypatch.setattr(mail, "send", lambda *a, **k: (seen.update(args=a, kw=k), "re_9")[1])
    payload = {
        "from": "Admiry <hello@agent.test>",
        "to": ["owner@example.com"],
        "subject": "Re: More customers",
        "text": "Hello.",
        "html": "<p>Hello.</p>",
        "headers": {"Message-ID": "<em_1@agent.test>"},
        "reply_to": "hello+" + "a" * 32 + "@agent.test",
    }
    assert mail_out.send(payload, idempotency_key="em_1") == "re_9"
    assert seen["args"] == ("owner@example.com", "Re: More customers", "Hello.")
    assert seen["kw"] == {
        "html": "<p>Hello.</p>",
        "headers": {"Message-ID": "<em_1@agent.test>"},
        "reply_to": "hello+" + "a" * 32 + "@agent.test",
        "idempotency_key": "em_1",
        "from_addr": "Admiry <hello@agent.test>",
    }


def test_send_plain_goes_to_one_address_with_the_footer(resend):
    ok = mail_out.send_plain(
        config.load(),
        to="ops@example.com",
        subject="[Admiry] queue",
        paragraphs=["Thread et_1 waits."],
        idempotency_key="ops-1",
    )
    assert ok and resend.sent[0]["to"] == ["ops@example.com"] and "reply_to" not in resend.sent[0]
    assert resend.sent[0]["text"].endswith(mail_out.FOOTER)
