"""The digest over the shared sending core (digest spec 2026-09-30 §8; plan 2026-10-07-agent-k2 Task 10, Dg Task 7,
P3, P4): the owner's mail goes out whatever the e-mail channel says, a retry is one row under one Idempotency-Key with
the same bytes, a refusal is reported, the operator's preview needs only the ops address, the stored copy masks the
unsubscribe token, and a digest's own thread."""

import dataclasses
import json
import re

import httpx
import pytest

from agent import config, mail, mail_dispatch, mail_out
from agent.tests import digest_fixtures as df

UNSUB = "http://testserver/unsubscribe/vabc123.SECRETTOKEN?b=biz_1&f=off"
RENDERED = {
    "subject": "Yesterday: $23.10 spent, 2 calls, 1 form",
    "text": f"Yesterday: $23.10 spent.\n\nStop reports: {UNSUB}\n\nOpen: http://testserver/m/SECRETM",
    "html": "<p>Yesterday: $23.10 spent.</p>",
    "headers": {"List-Unsubscribe": f"<{UNSUB}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    "reply_to": None,
}


@pytest.fixture
def thread(conn, digest_env):
    df.seed_business(conn, "biz_1")
    return mail_dispatch.open_thread(conn, "biz_1", "Owner@Example.com", RENDERED["subject"])


def _deliver(conn, thread, digest_id="dg_1", rendered=RENDERED):
    return mail_out.deliver_digest(
        conn, config.load(), digest_id=digest_id, thread=thread, to="owner@example.com", rendered=rendered
    )


def test_open_thread(conn, thread):
    assert re.fullmatch(r"[0-9a-f]{32}", thread["token"])
    assert (thread["kind"], thread["business_id"], thread["counterpart"]) == ("digest", "biz_1", "owner@example.com")
    assert thread["id"].startswith("et_") and thread["subject"] == RENDERED["subject"]


def test_digest_goes_out_with_the_channel_off(conn, thread, digest_env, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    out = _deliver(conn, thread)
    assert out["outcome"] == "sent" and out["error"] is None
    [sent] = digest_env.sent
    assert "reply_to" not in sent
    assert sent["headers"]["List-Unsubscribe"] == f"<{UNSUB}>"
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert sent["headers"]["Message-ID"] == "<dg_1@agent.test>" and digest_env.keys == ["dg_1"]
    row = conn.execute("SELECT id, outcome, intent FROM email_message").fetchone()
    assert row == {"id": "dg_1", "outcome": "sent", "intent": {"kind": "digest", "digest_id": "dg_1"}}


def test_retry_updates_one_row_with_one_key(conn, thread, digest_env):
    digest_env.fail = httpx.Response(500, json={"message": "boom"})
    first = _deliver(conn, thread)
    digest_env.fail = None
    second = _deliver(conn, thread)
    assert (first["outcome"], first["error"], second["outcome"]) == ("failed", "mail refused (500)", "sent")
    assert conn.execute("SELECT count(*) AS n, min(outcome) AS o FROM email_message").fetchone() == {
        "n": 1,
        "o": "sent",
    }
    a, b = digest_env.requests
    assert a.headers["Idempotency-Key"] == b.headers["Idempotency-Key"] == "dg_1"
    assert json.loads(a.content) == json.loads(b.content)


def test_timeout_then_retry_same_key_and_bytes(conn, thread, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"id": "re_1"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    first, second = _deliver(conn, thread), _deliver(conn, thread)
    assert (first["outcome"], second["outcome"]) == ("failed", "sent")
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"] == "dg_1"
    assert seen[0].content == seen[1].content


def test_quota_refusal_is_reported(conn, thread, digest_env):
    digest_env.fail = httpx.Response(429, json={"message": "daily quota"})
    assert "(429)" in _deliver(conn, thread)["error"]


def test_digest_off_is_refused_without_a_call(conn, thread, digest_env, monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST", "off")
    out = _deliver(conn, thread)
    assert out["outcome"] == "failed" and digest_env.requests == []


def _operator(settings, **kw) -> str | None:
    args = {"subject": "[preview] x", "text": "t", "html": "<p>t</p>", "message_id": "<dg_9@agent.test>"}
    return mail_out.send_operator(settings, **{**args, "idempotency_key": "dg_9", **kw})


def test_send_operator_ignores_the_inbound_switch(digest_env, monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    assert _operator(config.load()) is None
    [sent] = digest_env.sent
    assert sent["to"] == ["ops@example.com"] and "reply_to" not in sent
    assert sent["headers"] == {"Message-ID": "<dg_9@agent.test>"} and digest_env.keys == ["dg_9"]


def test_send_operator_reports_a_quota_refusal(digest_env):
    digest_env.fail = httpx.Response(429, json={"message": "daily quota"})
    assert "(429)" in _operator(config.load())


def test_send_operator_needs_the_ops_address(digest_env):
    settings = config.load()
    assert _operator(dataclasses.replace(settings, ops_email=None)) == "no AGENT_OPS_EMAIL"
    assert _operator(dataclasses.replace(settings, resend_api_key=None)) == "RESEND_API_KEY unset"
    assert digest_env.requests == []


def test_mask_link_token_masks_both_prefixes(conn, thread, digest_env):
    out = _deliver(conn, thread)
    assert "SECRETTOKEN" in out["text"] and "SECRETM" in out["text"]  # the sent mail keeps them
    stored = conn.execute("SELECT new_text FROM email_message").fetchone()["new_text"]
    assert "SECRETTOKEN" not in stored and "SECRETM" not in stored
    assert "http://testserver/unsubscribe/…" in stored and "http://testserver/m/…" in stored
