"""The shared sending core (spec 2026-09-30 §5.2, D141): deliver() keeps the channel's own gate (finding I1); ticket
mail goes out under AGENT_SUPPORT even with the channel off, Reply-To then a person's mailbox; a retry with the same row
id updates one row and repeats its Message-ID; send_plain takes its gate as a parameter."""

import httpx

from agent import config, mail_out
from agent.tests.test_mail_out import thread


def test_ticket_mail_goes_out_with_the_channel_off_and_names_a_person(conn, support_on, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    monkeypatch.setenv("AGENT_SUPPORT_REPLY_TO", "support@example.com")
    t = thread(conn, status="operator")
    out = mail_out.deliver_ticket(
        conn,
        config.load(),
        thread=t,
        to="owner@example.com",
        subject="We got your message (tk_1)",
        paragraphs=["Thanks."],
        row_id="em_tk1",
        sent_by="admiry",
    )
    assert out["outcome"] == "sent" and out["message_id"] == "<em_tk1@agent.test>"
    sent = support_on.sent[0]
    assert sent["reply_to"] == "support@example.com" and sent["subject"] == "We got your message (tk_1)"
    assert sent["text"].startswith("Thanks.") and sent["text"].endswith(mail_out.FOOTER)
    row = conn.execute("SELECT * FROM email_message").fetchone()
    assert row["id"] == "em_tk1" and row["outcome"] == "sent" and row["sent_by"] == "admiry"
    assert row["intent"] == {"kind": "ticket"}


def test_ticket_mail_replies_to_the_thread_while_the_channel_is_on(conn, support_on):
    t = thread(conn, status="operator")
    mail_out.deliver_ticket(
        conn,
        config.load(),
        thread=t,
        to="owner@example.com",
        subject="s",
        paragraphs=["Hi."],
        row_id="em_a",
        sent_by="ops",
    )
    assert support_on.sent[0]["reply_to"] == "hello+" + "a" * 32 + "@agent.test"


def test_ticket_mail_is_refused_while_support_is_off(conn, resend):
    t = thread(conn)
    out = mail_out.deliver_ticket(
        conn,
        config.load(),
        thread=t,
        to="owner@example.com",
        subject="s",
        paragraphs=["Hi."],
        row_id="em_b",
        sent_by="admiry",
    )
    assert out["outcome"] == "failed" and out["error"] is None and resend.sent == []  # gated, not refused
    assert conn.execute("SELECT outcome FROM email_message WHERE id = 'em_b'").fetchone()["outcome"] == "failed"


def test_a_retry_with_the_same_row_id_updates_one_row(conn, support_on):
    t = thread(conn)
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, from_addr, to_addrs, outcome) "
        "VALUES ('em_in', 'et_1', 'in', '<one@example.com>', 'owner@example.com', '{hello@agent.test}', 'answered')"
    )
    support_on.fail = httpx.Response(503, json={"message": "try later"})
    args = {
        "thread": t,
        "to": "owner@example.com",
        "subject": "s",
        "paragraphs": ["Hi."],
        "row_id": "em_c",
        "sent_by": "ops",
    }
    first = mail_out.deliver_ticket(conn, config.load(), **args)
    support_on.fail = None
    second = mail_out.deliver_ticket(conn, config.load(), **args)
    assert (first["outcome"], second["outcome"]) == ("failed", "sent")
    assert isinstance(first["error"], str) and first["error"] and second["error"] is None  # Resend's refusal, then none
    assert first["message_id"] == second["message_id"] == "<em_c@agent.test>"
    rows = conn.execute("SELECT outcome, provider_id FROM email_message WHERE id = 'em_c'").fetchall()
    assert rows == [{"outcome": "sent", "provider_id": "re_2"}]
    # both tries must have identical headers for Resend idempotency; In-Reply-To and References must be the same
    assert support_on.sent[0]["headers"]["In-Reply-To"] == "<one@example.com>"
    assert support_on.sent[1]["headers"]["In-Reply-To"] == "<one@example.com>"
    assert support_on.sent[0]["headers"]["References"] == "<one@example.com>"
    assert support_on.sent[1]["headers"]["References"] == "<one@example.com>"
    assert support_on.sent[0]["headers"] == support_on.sent[1]["headers"]


def test_the_core_takes_extra_headers_and_no_reply_to(conn, support_on):
    t = thread(conn)
    text, html = mail_out.compose(["Hello."])
    out = mail_out.deliver_core(
        conn,
        config.load(),
        thread=t,
        to="owner@example.com",
        subject="s",
        text=text,
        html=html,
        gate=None,
        reply_to=None,
        row_id="em_d",
        idempotency_key="digest:1",
        extra_headers={"List-Unsubscribe": "<https://example.com/u>"},
    )
    sent = support_on.sent[0]
    assert out["outcome"] == "sent" and sent["headers"]["List-Unsubscribe"] == "<https://example.com/u>"
    assert "reply_to" not in sent and sent["headers"]["Message-ID"] == "<em_d@agent.test>"
    assert support_on.keys == ["digest:1"]  # the caller's Idempotency-Key, not the row id


def test_a_losing_try_never_turns_a_sent_row_failed(conn, support_on):
    """Two flushes of one outbox row can both send; Resend answers the later one with an error (a concurrent request
    under the same Idempotency-Key). The row that says the mail went stays sent, with its provider id; the losing try
    still reports its own outcome."""
    t = thread(conn)
    text, html = mail_out.compose(["Hello."])
    args = {"thread": t, "to": "owner@example.com", "subject": "s", "text": text, "html": html}
    first = mail_out.deliver_core(conn, config.load(), **args, gate=None, reply_to=None, row_id="em_e")
    support_on.fail = httpx.Response(409, json={"message": "concurrent idempotent requests"})
    second = mail_out.deliver_core(conn, config.load(), **args, gate=None, reply_to=None, row_id="em_e")
    assert (first["outcome"], second["outcome"]) == ("sent", "failed") and second["error"]
    row = conn.execute("SELECT outcome, provider_id FROM email_message WHERE id = 'em_e'").fetchone()
    assert row == {"outcome": "sent", "provider_id": first["provider_id"]}


def test_the_channel_s_own_mail_still_waits_for_the_channel_with_support_on(conn, support_on, monkeypatch):
    """What prod depends on (D141, finding I1): AGENT_SUPPORT=on opens ticket mail's gate only. With the e-mail channel
    off, deliver (the channel's replies) and the channel's plain notices still refuse, and nothing reaches Resend."""
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    monkeypatch.setenv("AGENT_SUPPORT_REPLY_TO", "support@example.com")  # config wants it while the channel is off
    settings = config.load()
    assert settings.support and not settings.email_inbound and settings.resend_api_key
    out = mail_out.deliver(conn, settings, thread=thread(conn), to="owner@example.com", paragraphs=["Hello."])
    assert out["outcome"] == "failed" and out["error"] is None  # gated, never tried
    assert conn.execute("SELECT outcome FROM email_message").fetchone() == {"outcome": "failed"}
    notice = {"to": "ops@example.com", "subject": "s", "paragraphs": ["hi"], "idempotency_key": "n1"}
    assert mail_out.send_plain(settings, **notice) is False
    assert support_on.sent == []


def test_send_plain_takes_its_gate(resend, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    s = config.load()
    assert mail_out.send_plain(s, to="ops@example.com", subject="s", paragraphs=["hi"], idempotency_key="n1") is False
    assert mail_out.send_plain(s, to="ops@example.com", subject="s", paragraphs=["hi"], idempotency_key="n2", gate=None)
    assert (
        mail_out.send_plain(
            s, to="ops@example.com", subject="s", paragraphs=["hi"], idempotency_key="n3", gate="support is off"
        )
        is False
    )
    assert [m["to"] for m in resend.sent] == [["ops@example.com"]]
