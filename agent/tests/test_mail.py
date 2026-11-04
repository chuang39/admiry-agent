import json

import httpx
import pytest

from agent import mail


def test_unconfigured_mail_sends_nothing_and_says_so(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert mail.configured() is False
    assert mail.send("o@example.com", "Hi", "text") is None


def test_send_posts_to_resend_and_returns_the_id(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "em_1"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert mail.send("o@example.com", "Your page kit", "hello", http=http) == "em_1"
    r = seen[0]
    assert r.url == "https://api.resend.com/emails" and r.headers["authorization"] == "Bearer re_test_SECRET"
    body = r.read().decode()
    # D59 (P16): the one sender, Admiry <AGENT_EMAIL_ADDRESS>, hello@agent.admiry.ai by default; no-reply@ is retired
    assert '"to": ["o@example.com"]' in body and '"from": "Admiry <hello@agent.admiry.ai>"' in body


def test_the_channel_fields_go_out_only_when_given(monkeypatch):
    """P16: the e-mail channel (mail_out) sends through this client: html, headers and reply_to join the body, the
    Idempotency-Key the request, and from_addr replaces the sender -- each only when given, so without them the
    request is exactly the one above."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": f"re_{len(seen)}"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert mail.send("o@example.com", "Your page kit", "hello", http=http) == "re_1"
    assert set(json.loads(seen[0].read())) == {"from", "to", "subject", "text"}
    assert "idempotency-key" not in seen[0].headers
    sent = mail.send(
        "o@example.com",
        "Re: More customers",
        "Hello.",
        http=http,
        html="<p>Hello.</p>",
        headers={"Message-ID": "<em_1@agent.test>", "In-Reply-To": "<one@example.com>"},
        reply_to="hello+tok@agent.test",
        idempotency_key="em_1",
        from_addr="Admiry <hello@agent.test>",
    )
    assert sent == "re_2" and seen[1].headers["idempotency-key"] == "em_1"
    assert json.loads(seen[1].read()) == {
        "from": "Admiry <hello@agent.test>",
        "to": ["o@example.com"],
        "subject": "Re: More customers",
        "text": "Hello.",
        "html": "<p>Hello.</p>",
        "headers": {"Message-ID": "<em_1@agent.test>", "In-Reply-To": "<one@example.com>"},
        "reply_to": "hello+tok@agent.test",
    }


def test_a_refusal_is_a_mail_error(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"message": "bad from"})))
    with pytest.raises(mail.MailError):
        mail.send("o@example.com", "x", "y", http=http)


def test_a_2xx_that_is_not_a_json_object_is_a_mail_error(monkeypatch):
    """Resend answering 2xx without its JSON object is a MailError (the callers' one refusal path), never a raw
    ValueError or AttributeError; so is a refusal whose JSON is not an object."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    for answer in (httpx.Response(200, text="OK"), httpx.Response(200, json=["em_1"])):
        http = httpx.Client(transport=httpx.MockTransport(lambda r, a=answer: a))
        with pytest.raises(mail.MailError, match="unreadable"):
            mail.send("o@example.com", "x", "y", http=http)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(422, json=["bad"])))
    with pytest.raises(mail.MailError, match="refused"):
        mail.send("o@example.com", "x", "y", http=http)


def test_the_subject_is_one_clean_line():
    """I10: control characters (a CR/LF pair is a header injection) become single spaces; at most 200."""
    assert (
        mail.subject_line("New lead: Ana\r\nBcc: x@example.com\t\x00\u2028ok") == "New lead: Ana Bcc: x@example.com ok"
    )
    assert len(mail.subject_line("x" * 500)) == mail.MAX_SUBJECT
