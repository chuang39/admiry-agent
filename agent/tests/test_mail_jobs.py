"""email-retry, email-purge (spec 2026-09-28 §5.1, §7, D62) and the operator commands (§5.9)."""

from pathlib import Path

import httpx

from agent import jobs, mail_grants, ops
from agent.tests.test_mail_dispatch import CUE, OWNER, fake, last_sent  # noqa: F401 - fixture
from agent.tests.test_mail_in import inbound


def test_email_retry_reprocesses_stale_received_rows(client, conn, resend, fake, monkeypatch):  # noqa: F811 - fixture
    from agent import mail_in

    calls = []
    monkeypatch.setattr(mail_in, "process", lambda row_id: calls.append(row_id))
    inbound(client, resend, from_="owner@example.com", text="hi")  # processed by the patched no-op
    conn.execute(
        "UPDATE email_message SET outcome = 'received', attempts = 1, created_at = now() - interval '11 minutes'"
    )
    conn.execute(
        "INSERT INTO email_message (id, direction, message_id, from_addr, outcome, attempts, created_at) "
        "VALUES ('em_fresh', 'in', '<fresh@x>', 'a@b', 'received', 0, now()), "
        "('em_spent', 'in', '<spent@x>', 'a@b', 'received', 3, now() - interval '1 hour')"
    )
    calls.clear()
    assert jobs.email_retry(conn, 0, 1) == {"retried": 1}
    assert calls and calls[0] not in ("em_fresh", "em_spent")


def test_email_retry_sleeps_when_the_channel_is_off(conn, monkeypatch):
    """RESEND_API_KEY may still be set (prod compose gives the agent one for the website), so the check must be
    email_inbound itself, not the key -- finding I1."""
    from agent import mail_in

    calls = []
    monkeypatch.setattr(mail_in, "process", lambda row_id: calls.append(row_id))
    conn.execute(
        "INSERT INTO email_message (id, direction, message_id, from_addr, outcome, attempts, created_at) "
        "VALUES ('em_stale', 'in', '<stale@x>', 'a@b', 'received', 0, now() - interval '11 minutes')"
    )
    assert jobs.email_retry(conn, 0, 1) == {"retried": 0, "off": True}
    assert calls == []


def test_email_purge_forgets_text_and_stale_links_and_keys(conn):
    from agent import keys

    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token) VALUES ('et_1', 'biz_1', 'a@b', 'tok')"
    )
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, from_addr, new_text, outcome, created_at) VALUES "
        "('em_old', 'et_1', 'in', '<old@x>', 'a@b', 'old words', 'answered', now() - interval '91 days'), "
        "('em_new', 'et_1', 'in', '<new@x>', 'a@b', 'new words', 'answered', now() - interval '89 days')"
    )
    conn.execute(
        "INSERT INTO email_link (token_hash, thread_id, business_id, expires_at, used_at) VALUES "
        "('h_used', 'et_1', 'biz_1', now() + interval '1 day', now() - interval '2 days'), "
        "('h_expired', 'et_1', 'biz_1', now() - interval '2 days', NULL), "
        "('h_live', 'et_1', 'biz_1', now() + interval '1 day', NULL)"
    )
    _, old = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="email", source="email")
    conn.execute("UPDATE api_key SET revoked_at = now() - interval '2 days' WHERE id = %s", (old.id,))
    _, kept = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="email", source="email")
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE id = %s", (kept.id,))
    assert jobs.email_purge(conn, 0, 1) == {"purged": 1, "links": 2, "keys": 1}
    rows = {r["id"]: r for r in conn.execute("SELECT id, new_text, purged_at FROM email_message")}
    assert (
        rows["em_old"]["new_text"] is None
        and rows["em_old"]["purged_at"] is not None
        and rows["em_new"]["new_text"] == "new words"
    )
    assert [r["token_hash"] for r in conn.execute("SELECT token_hash FROM email_link")] == ["h_live"]
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE source = 'email'").fetchone()["n"] == 1


def test_ops_queue_reply_and_stats(client, conn, resend, fake, capsys, tmp_path):  # noqa: F811 - fixture
    conn.execute(
        "INSERT INTO email_thread (id, counterpart, token, subject, status) VALUES ('et_1', 'owner@example.com', 'tok', 'plan', 'operator')"
    )
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, from_addr, new_text, outcome, intent) VALUES "
        "('em_1', 'et_1', 'in', '<q@x>', 'owner@example.com', 'Can you also run radio ads for me?', 'queued', '{\"intent\": \"other\"}'), "
        "('em_2', NULL, 'in', '<d@x>', 'spam@x', NULL, 'dropped:auth', NULL)"
    )
    assert ops.main(["email-queue"]) == 0
    out = capsys.readouterr().out
    assert "et_1" in out and "owner@example.com" in out and "radio ads" in out
    f = tmp_path / "reply.txt"
    f.write_text("Not yet — radio is not something Admiry runs. The plan stays on Google Search.\n")
    assert ops.main(["email-reply", "et_1", "--text-file", str(f)]) == 0
    sent = last_sent(resend)
    assert sent["to"] == ["owner@example.com"] and "radio" in sent["text"] and sent["headers"]["In-Reply-To"] == "<q@x>"
    row = conn.execute("SELECT sent_by FROM email_message WHERE direction = 'out'").fetchone()
    assert row["sent_by"] == "ops"
    assert conn.execute("SELECT status, unclear FROM email_thread").fetchone() == {"status": "open", "unclear": 0}
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.email_reply' AND actor = 'ops:cli'"
        ).fetchone()["n"]
        == 1
    )
    assert ops.main(["email-reply", "et_nope", "--text-file", str(f)]) == 1
    assert ops.main(["email-stats"]) == 0
    out = capsys.readouterr().out
    assert "queued 1" in out and "dropped:auth 1" in out and "sent 1" in out


def test_ops_email_reply_failure_leaves_the_thread_with_the_operator(conn, resend, tmp_path):
    conn.execute(
        "INSERT INTO email_thread (id, counterpart, token, subject, status) VALUES "
        "('et_1', 'owner@example.com', 'tok', 'plan', 'operator')"
    )
    f = tmp_path / "reply.txt"
    f.write_text("Not yet.\n")
    resend.fail = httpx.Response(500, text="boom")
    assert ops.main(["email-reply", "et_1", "--text-file", str(f)]) == 1
    assert conn.execute("SELECT status FROM email_thread WHERE id = 'et_1'").fetchone()["status"] == "operator"
    row = conn.execute("SELECT outcome FROM email_message WHERE direction = 'out'").fetchone()
    assert row["outcome"] == "failed"


def test_ops_email_reply_goes_to_the_owner_not_a_revoked_counterpart(conn, resend, tmp_path):
    """M1: a thread whose counterpart is a revoked address, but whose last authorized inbound message came from
    the owner, must have `ops email-reply` answer the owner (mail_dispatch.reply_address), not thread.counterpart."""
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', %s)", (OWNER,))
    g = mail_grants.add(conn, "biz_1", CUE, scopes=("read", "manage"), via="connect", created_by=OWNER)
    assert mail_grants.revoke(conn, g["id"], "biz_1")
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, subject, status) VALUES "
        "('et_1', 'biz_1', %s, 'tok', 'plan', 'operator')",
        (CUE,),
    )
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, from_addr, new_text, outcome, created_at) "
        "VALUES "
        "('em_1', 'et_1', 'in', '<a@x>', %s, 'hello', 'answered', now() - interval '1 hour'), "
        "('em_2', 'et_1', 'in', '<b@x>', %s, 'please reply', 'queued', now())",
        (CUE, OWNER),
    )
    f = tmp_path / "reply.txt"
    f.write_text("Noted.\n")
    assert ops.main(["email-reply", "et_1", "--text-file", str(f)]) == 0
    assert last_sent(resend)["to"] == [OWNER]


def test_every_mail_job_is_in_the_dag(conn):
    src = Path("airflow/dags/agent_ticks.py").read_text()
    assert '"email-retry"' in src and '"email-purge"' in src
    assert "email-retry" in jobs.JOBS and "email-purge" in jobs.JOBS
