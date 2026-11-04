"""Resend's bounce and complaint events (digest spec 2026-09-30 §9, Dg P12; plan 2026-10-07-agent-k2 Task 13): a
permanent bounce of our own mail pauses mail to the address for every business it owns, a complaint about a digest
turns that address's reports off, everything else -- suppressions, transient bounces, other environments' mail -- is
answered and ignored without a word about the addresses."""

import base64
import hashlib
import hmac
import json
import logging
import time

import pytest

from agent import leads, tools_account
from agent.tests import digest_fixtures as df
from agent.tests.conftest import RESEND_WHSEC

OURS = "Admiry <hello@agent.test>"


def post_event(client, kind: str, data: dict, *, event_id: str = "evt_1", secret: str = RESEND_WHSEC):
    payload = json.dumps({"type": kind, "created_at": "2026-10-06T15:00:00Z", "data": data}).encode()
    ts = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{event_id}.{ts}.".encode() + payload, hashlib.sha256).digest()).decode()
    headers = {"svix-id": event_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}
    return client.post("/hooks/resend-events", content=payload, headers={**headers, "Content-Type": "application/json"})


def bounce(to="owner@example.com", kind="Permanent", subtype="General", sender=OURS) -> dict:
    return {
        "email_id": "re_1",
        "from": sender,
        "to": [to],
        "subject": "x",
        "bounce": {"type": kind, "subType": subtype, "message": "550"},
    }


@pytest.fixture
def events(conn, digest_env, monkeypatch):
    monkeypatch.setenv("RESEND_EVENTS_WEBHOOK_SECRET", RESEND_WHSEC)
    df.seed_business(conn, "biz_1")
    df.seed_business(conn, "biz_2", owner="OWNER@example.com")
    return digest_env


def _status(conn) -> dict:
    return {
        r["id"]: (r["notify_email_status"], r["digest_frequency"])
        for r in conn.execute("SELECT id, notify_email_status, digest_frequency FROM business ORDER BY id")
    }


def test_secret_empty_is_404(client, conn, digest_env):
    assert post_event(client, "email.bounced", bounce()).status_code == 404


def test_bad_signature_is_400(client, events):
    other = "whsec_" + base64.b64encode(b"z" * 32).decode()
    assert post_event(client, "email.bounced", bounce(), secret=other).status_code == 400


def test_permanent_bounce_pauses_every_business_of_the_address(client, conn, events):
    r = post_event(client, "email.bounced", bounce())
    assert r.status_code == 200 and r.json()["action"] == "bounced"
    assert _status(conn) == {"biz_1": ("bounced", "daily"), "biz_2": ("bounced", "daily")}
    lead_id = df.seed_lead(conn, "biz_1", df.ANALYST_NOW)
    lead = conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert leads.notify(conn, lead) is None
    notice = [json.loads(r.content) for r in events.requests]
    assert len(notice) == 1 and notice[0]["to"] == ["ops@example.com"] and "bounced" in notice[0]["subject"]
    assert events.requests[0].headers["Idempotency-Key"] == "evt-evt_1"


def test_suppressed_subtype_does_not_flip(client, conn, events):
    post_event(client, "email.bounced", bounce(subtype="Suppressed"))
    assert _status(conn)["biz_1"] == ("ok", "daily")


def test_email_suppressed_is_ignored(client, conn, events):
    r = post_event(client, "email.suppressed", {"email_id": "re_1", "from": OURS, "to": ["owner@example.com"]})
    assert r.json()["action"] == "ignored" and _status(conn)["biz_1"] == ("ok", "daily")


def test_transient_bounce_is_ignored(client, conn, events):
    r = post_event(client, "email.bounced", bounce(kind="Transient"))
    assert r.json()["action"] == "ignored" and _status(conn)["biz_1"] == ("ok", "daily")


def test_other_environments_sender_is_ignored(client, conn, events):
    r = post_event(client, "email.bounced", bounce(sender="Admiry <hello@agent.dev.adx.admiry.ai>"))
    assert r.json()["action"] == "ignored" and _status(conn)["biz_1"] == ("ok", "daily")


def test_ignored_events_log_no_addresses(client, conn, events, caplog):
    with caplog.at_level(logging.INFO):
        post_event(client, "email.bounced", bounce(to="secret.person@example.com", kind="Transient"))
    assert "secret.person" not in caplog.text and "hello@agent.test" not in caplog.text


def _digest_mail(conn) -> None:
    from agent import mail_dispatch

    thread = mail_dispatch.open_thread(conn, "biz_1", "owner@example.com", "Yesterday")
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, provider_id, from_addr, to_addrs, subject, "
        "intent, outcome) VALUES ('dg_1', %s, 'out', '<dg_1@agent.test>', 're_9', 'hello@agent.test', "
        "ARRAY['owner@example.com'], 'Yesterday', %s, 'sent')",
        (thread["id"], json.dumps({"kind": "digest", "digest_id": "dg_1"})),
    )


def test_complaint_on_a_digest_turns_reports_off_for_the_address(client, conn, events):
    _digest_mail(conn)
    data = {"email_id": "re_9", "from": OURS, "to": ["owner@example.com"], "subject": "Yesterday"}
    r = post_event(client, "email.complained", data)
    assert r.json()["action"] == "complaint"
    assert _status(conn) == {"biz_1": ("ok", "off"), "biz_2": ("ok", "off")}
    rows = conn.execute("SELECT * FROM agent_action WHERE tool = 'system.digest_complaint'").fetchall()
    assert len(rows) == 2
    assert tools_account.sentence(rows[0], None) == (
        "Admiry turned the reports off after the e-mail was marked as spam"
    )


def test_complaint_on_other_mail_notifies_the_operator_once(client, conn, events):
    data = {"email_id": "re_other", "from": OURS, "to": ["owner@example.com"], "subject": "New lead"}
    post_event(client, "email.complained", data, event_id="evt_7")
    post_event(client, "email.complained", data, event_id="evt_7")
    assert [r.headers["Idempotency-Key"] for r in events.requests] == ["evt-evt_7", "evt-evt_7"]
    assert _status(conn)["biz_1"] == ("ok", "daily")


def test_bounce_sentence_in_the_audit_log(client, conn, events):
    post_event(client, "email.bounced", bounce())
    row = conn.execute(
        "SELECT * FROM agent_action WHERE tool = 'system.email_bounced' AND business_id = 'biz_1'"
    ).fetchone()
    assert tools_account.sentence(row, None) == ("E-mails to your address bounced; lead alerts and reports are paused")
