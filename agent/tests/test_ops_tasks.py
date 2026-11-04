"""/ops/tasks (spec 2026-10-12 §4): the back office's task list -- the tabs, one ticket in full with the Help button's
context, a reply by e-mail and a close through support.reply / support.set_status, each an audit row in the admin's
name; the gate as /ops/review's, CSRF on every POST, and the warning on an address nobody verified."""

import pytest
from fastmcp.exceptions import ToolError

from agent import ops_tasks, support, support_mail
from agent.tests.test_ads_run import BIZ
from agent.tests.test_ops_review import _in_review
from agent.tests.test_support import biz, open_
from agent.tests.test_web import sign_in

ADMIN = "founder@example.com"
PILL = 'id="help-pill"'
REPORT = {
    "page": "/plan/pl_0123456789abcdef",
    "signed_in": True,
    "browser": "Safari on iPhone",
    "plan": {"id": "pl_0123456789abcdef", "version": 3, "status": "in_review"},
    "turns": [
        {
            "who": "admiry",
            "via": "home",
            "text": "Your ads are in review.",
            "at": "2026-10-12T09:00:00Z",
            "truncated": False,
        }
    ],
}
VISITOR = {
    "page": "/",
    "signed_in": False,
    "browser": "Chrome",
    "plan": None,
    "turns": [],
    "email": "maria@example.com",
}


@pytest.fixture
def admin(client, google, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    google.email = ADMIN
    sign_in(client, "/ops/tasks")
    return client


def _csrf(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def owner_ticket(conn) -> str:
    biz(conn)
    words = "I paid an hour ago and the card still says in review."
    return open_(conn, reason="help", words=words, report=REPORT)["ticket_id"]


def visitor_ticket(conn) -> str:
    return open_(
        conn,
        business_id=None,
        contact_email="maria@example.com",
        contact_verified=False,
        ip="198.51.100.9",
        reason="help",
        words="The plan stopped loading after I typed my address.",
        report=VISITOR,
    )["ticket_id"]


def test_the_tabs_hold_the_right_tickets_newest_first(conn, support_on):
    owner, visitor = owner_ticket(conn), visitor_ticket(conn)
    assistant = open_(
        conn,
        business_id=None,
        source="mcp",
        contact_email="peak@example.com",
        ip="198.51.100.3",
        words="Why did my ads stop?",
    )["ticket_id"]
    q = ops_tasks.queue_view(conn, "open")
    assert [r["id"] for r in q["rows"]] == [assistant, visitor, owner]
    assert q["tab"] == "open" and q["counts"] == {"open": 3, "waiting": 0, "closed": 0}
    by_id = {r["id"]: r for r in q["rows"]}
    assert by_id[owner]["origin"] == "Help button · /plan/pl_0123456789abcdef" and by_id[owner]["verified"] is True
    assert by_id[owner]["business_id"] == "biz_1" and by_id[owner]["first_line"].startswith("I paid an hour ago")
    assert by_id[visitor]["origin"] == "Help button · / · not signed in" and by_id[visitor]["verified"] is False
    assert by_id[visitor]["business_id"] is None and by_id[visitor]["contact"] == "maria@example.com"
    assert by_id[assistant]["origin"] == "Their assistant" and by_id[assistant]["age"] == "just now"
    support.reply(conn, owner, "Yes, that is right.", actor=f"admin:{ADMIN}")  # answered: waiting on the owner
    support.set_status(conn, visitor, "closed", actor=f"admin:{ADMIN}", resolution="not_support")
    assert [r["id"] for r in ops_tasks.queue_view(conn, "open")["rows"]] == [assistant]
    assert [r["id"] for r in ops_tasks.queue_view(conn, "waiting")["rows"]] == [owner]
    closed = ops_tasks.queue_view(conn, "closed")
    assert [r["id"] for r in closed["rows"]] == [visitor] and closed["rows"][0]["resolution"] == "not_support"
    assert closed["counts"] == {"open": 1, "waiting": 1, "closed": 1}
    with pytest.raises(ToolError):
        ops_tasks.queue_view(conn, "nope")


def test_origin_names_every_way_in():
    assert ops_tasks.origin("home", "help", {"help": {"page": "/ads", "signed_in": True}}) == "Help button · /ads"
    assert (
        ops_tasks.origin("home", "help", {"help": {"page": "/", "signed_in": False}})
        == "Help button · / · not signed in"
    )
    assert (
        ops_tasks.origin("home", "human", {}) == "The conversation"
        and ops_tasks.origin("home", "help", None) == "The conversation"
    )
    assert ops_tasks.origin("home", "website", {"context": "x"}) == "The website card"
    assert ops_tasks.origin("mcp", "human", {}) == "Their assistant"
    assert (
        ops_tasks.origin("email", "other", {}) == "E-mail"
        and ops_tasks.origin("feedback", "dissatisfied", {}) == "A 👎 on a plan"
    )


def test_the_detail_has_the_context_the_events_and_the_contact(conn, support_on):
    tid = owner_ticket(conn)
    d = ops_tasks.detail(conn, tid)
    assert d["contact"] == {"email": "owner@example.com", "verified": True}
    assert d["ticket"]["origin"] == "Help button · /plan/pl_0123456789abcdef" and d["ticket"]["status"] == "open"
    assert d["ticket"]["age"] == "just now" and isinstance(d["ticket"]["created_at"], str)
    assert [e["kind"] for e in d["events"]] == ["created", "owner_words", "ack", "notice"]
    words = d["events"][1]
    assert (
        words["body"].startswith("I paid an hour ago")
        and words["data"]["help"] == REPORT
        and words["age"] == "just now"
    )
    assert (
        d["business"]["id"] == "biz_1" and d["business"]["claim_state"] == "claimed" and d["business"]["plan"] is None
    )
    assert d["business"]["owner_email"] == "owner@example.com" and d["business"]["review"] is None
    assert d["snapshot_turns"][-1]["text"].startswith("I paid an hour ago") and d["snapshot_error"] is None
    assert d["resolutions"][0] == ("resolved", "Resolved") and len(d["resolutions"]) == len(support.RESOLUTIONS)
    assert ops_tasks.detail(conn, "tk_0000000000000000") is None


def test_the_detail_links_the_latest_plan_s_review(conn, support_on):
    review_id = _in_review(conn)
    tid = open_(conn, business_id=BIZ, reason="help", report=REPORT)["ticket_id"]
    d = ops_tasks.detail(conn, tid)
    assert d["business"]["plan"]["status"] == "in_review"
    assert d["business"]["review"] == {"id": review_id, "state": "queued"}


def test_the_detail_survives_a_failed_snapshot_and_a_business_with_no_knowledge(admin, conn, support_on):
    """Review Focus 5: a snapshot that failed when the ticket opened ({"error": …}, no turns) and a business row with
    no knowledge (no name) must not break the module or the page."""
    tid = owner_ticket(conn)
    conn.execute(
        """UPDATE support_ticket SET snapshot = '{"built_at": "2026-10-12T09:00:00Z", "error": "RuntimeError"}'"""
    )
    d = ops_tasks.detail(conn, tid)
    assert d["snapshot_turns"] == [] and d["snapshot_error"] == "RuntimeError" and d["business"]["name"] is None
    page = admin.get(f"/ops/tasks/{tid}")
    assert page.status_code == 200 and "The snapshot failed (RuntimeError)." in page.text and "biz_1" in page.text


def test_reply_and_close_write_audit_rows_in_the_admin_s_name(conn, support_on):
    tid = owner_ticket(conn)
    support_mail.flush(tid)
    out = ops_tasks.reply(conn, tid, admin=ADMIN, text="  Yes, that is right.  ", close=False)
    assert out == {"ticket_id": tid, "event_id": out["event_id"], "close": False}
    assert conn.execute("SELECT status FROM support_ticket").fetchone() == {"status": "answered"}
    assert conn.execute("SELECT body, mail_state FROM ticket_event WHERE kind = 'reply_out'").fetchone() == {
        "body": "Yes, that is right.",
        "mail_state": "pending",
    }
    row = conn.execute(
        "SELECT actor, actor_class, business_id FROM agent_action WHERE tool = 'admin.ticket_reply'"
    ).fetchone()
    assert row == {"actor": f"admin:{ADMIN}", "actor_class": "platform", "business_id": "biz_1"}
    assert support_mail.flush(tid) == {"sent": 1, "failed": 0} and support_on.sent[-1]["to"] == ["owner@example.com"]
    with pytest.raises(ToolError, match=ops_tasks.NEEDS_TEXT):
        ops_tasks.reply(conn, tid, admin=ADMIN, text="  ")
    with pytest.raises(ToolError, match="resolution must be one of"):
        ops_tasks.close(conn, tid, admin=ADMIN, resolution="nope")
    with pytest.raises(ToolError, match=ops_tasks.NOT_FOUND):
        ops_tasks.reply(conn, "tk_0000000000000000", admin=ADMIN, text="x")
    closed = ops_tasks.close(conn, tid, admin=ADMIN, resolution="resolved")
    assert closed == {"ticket_id": tid, "status": "closed", "resolution": "resolved"}
    assert conn.execute("SELECT actor FROM agent_action WHERE tool = 'admin.ticket_close'").fetchone() == {
        "actor": f"admin:{ADMIN}"
    }
    with pytest.raises(ToolError, match="closed"):
        ops_tasks.reply(conn, tid, admin=ADMIN, text="Once more")
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'admin.ticket_reply'").fetchone()["n"] == 1


def test_the_page_gate_tabs_detail_and_actions(client, conn, google, support_on, monkeypatch):
    owner, visitor = owner_ticket(conn), visitor_ticket(conn)
    assert client.get("/ops/tasks").status_code == 404 and client.get(f"/ops/tasks/{owner}").status_code == 404
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    page = client.get("/ops/tasks")
    assert page.status_code == 200 and "/connect/google/start?next=/ops/tasks" in page.text  # the sign-in page
    assert client.post(f"/ops/tasks/{owner}", data={"action": "close"}).status_code == 404
    sign_in(client, "/connect")  # owner@example.com: signed in, not listed
    assert client.get("/ops/tasks").status_code == 404
    assert client.post(f"/ops/tasks/{owner}", data={"action": "close"}).status_code == 404
    google.email = ADMIN
    sign_in(client, "/ops/tasks")
    q = client.get("/ops/tasks")
    assert (
        q.status_code == 200 and "Open · 2" in q.text and owner in q.text and visitor in q.text and PILL not in q.text
    )
    assert "A visitor · maria@example.com" in q.text and "not verified" in q.text
    assert (
        client.get("/ops/tasks?tab=waiting").status_code == 200 and client.get("/ops/tasks?tab=nope").status_code == 404
    )
    d = client.get(f"/ops/tasks/{visitor}")
    assert d.status_code == 200 and "Reply by e-mail" in d.text and "Send and close" in d.text and "Close as" in d.text
    assert (
        "This address is not verified" in d.text and "Help button · / · not signed in" in d.text and PILL not in d.text
    )
    assert client.get("/ops/tasks/tk_0000000000000000").status_code == 404
    assert (
        client.post(f"/ops/tasks/{visitor}", data={"action": "reply", "text": "Hello"}).status_code == 403
    )  # no token
    csrf = _csrf(d.text)
    r = client.post(f"/ops/tasks/{visitor}", data={"action": "reply", "text": "", "csrf": csrf})
    assert r.status_code == 409 and ops_tasks.NEEDS_TEXT in r.text
    assert client.post(f"/ops/tasks/{visitor}", data={"action": "nope", "csrf": csrf}).status_code == 400
    r = client.post(
        f"/ops/tasks/{visitor}",
        data={"action": "reply", "text": "Try again now.", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/ops/tasks/{visitor}?done=reply"
    assert conn.execute("SELECT status FROM support_ticket WHERE id = %s", (visitor,)).fetchone() == {
        "status": "answered"
    }
    assert support_on.sent[-1]["to"] == ["maria@example.com"]  # a person wrote to the unverified address, knowingly
    assert "Reply sent." in client.get(f"/ops/tasks/{visitor}?done=reply").text
    r = client.post(
        f"/ops/tasks/{owner}", data={"action": "reply_close", "text": "All set.", "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    assert conn.execute("SELECT status FROM support_ticket WHERE id = %s", (owner,)).fetchone() == {"status": "closed"}
    r = client.post(
        f"/ops/tasks/{visitor}",
        data={"action": "close", "resolution": "no_reply", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert conn.execute("SELECT status, resolution FROM support_ticket WHERE id = %s", (visitor,)).fetchone() == {
        "status": "closed",
        "resolution": "no_reply",
    }
    assert "Closed · 2" in client.get("/ops/tasks?tab=closed").text


def test_a_reply_to_a_ticket_with_no_address_is_refused_on_the_page(admin, conn, support_on):
    """Review Focus 5: a draft's ticket still waiting for an address -- support.reply refuses it; the page says so
    (409) and nothing is queued or sent."""
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, draft=True, ip="198.51.100.9")["ticket_id"]
    d = admin.get(f"/ops/tasks/{tid}")
    assert d.status_code == 200 and "No address yet" in d.text and "none yet" in d.text
    r = admin.post(f"/ops/tasks/{tid}", data={"action": "reply", "text": "Hello?", "csrf": _csrf(d.text)})
    assert r.status_code == 409 and "no contact yet" in r.text
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'reply_out'").fetchone()["n"] == 0
    assert support_on.sent == []


# ---------------------------------------------------------------- Task 5 review (ruling U1)


def test_reply_refuses_an_address_a_mail_cant_go_to(conn, support_on):
    tid = open_(conn, business_id=None, contact_email="a,b@example.com", contact_verified=False, ip="198.51.100.9")[
        "ticket_id"
    ]
    with pytest.raises(ToolError, match="can't go to"):
        ops_tasks.reply(conn, tid, admin=ADMIN, text="Hello")
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'reply_out'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'admin.ticket_reply'").fetchone()["n"] == 0


def test_close_refuses_an_already_closed_ticket_and_writes_no_audit_row(conn, support_on):
    tid = owner_ticket(conn)
    ops_tasks.close(conn, tid, admin=ADMIN, resolution="resolved")
    with pytest.raises(ToolError, match="already closed"):
        ops_tasks.close(conn, tid, admin=ADMIN, resolution="resolved")
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'admin.ticket_close'").fetchone()["n"] == 1
    assert conn.execute("SELECT status, resolution FROM support_ticket").fetchone() == {
        "status": "closed",
        "resolution": "resolved",
    }


def test_a_refused_reply_keeps_its_draft_in_the_textarea(admin, conn, support_on):
    tid = open_(conn, business_id=None, contact_email="a,b@example.com", contact_verified=False, ip="198.51.100.9")[
        "ticket_id"
    ]
    d = admin.get(f"/ops/tasks/{tid}")
    csrf = _csrf(d.text)
    r = admin.post(f"/ops/tasks/{tid}", data={"action": "reply", "text": "Draft <b>text</b> & more", "csrf": csrf})
    assert r.status_code == 409 and "characters a mail can" in r.text and "go to." in r.text
    assert "Draft &lt;b&gt;text&lt;/b&gt; &amp; more" in r.text


def test_prod_serves_the_page_and_the_review_page_names_the_tasks(admin, monkeypatch):
    assert "/ops/tasks" in admin.get("/ops/review").text
    monkeypatch.setenv("AGENT_ENV", "prod")
    empty = admin.get("/ops/tasks")
    assert empty.status_code == 200 and "Nothing here." in empty.text and "/ops/review" in empty.text
