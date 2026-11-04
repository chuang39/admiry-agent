"""POST /api/help (spec 2026-10-12 §3.3, §3.5): who is writing -- the page's business, the signed-in owner's latest, or
a visitor keyed by a typed address nobody verified -- what Admiry adds by itself (the page, the plan, signed in or not,
the browser, the last turns), the limits, and an answer that says only "sent"."""

import logging

from agent import benchmark, sessions, support, support_api, support_mail, tools_account
from agent.tests.test_home_api import IP, fake, turn  # noqa: F401 - fake is a fixture used by name
from agent.tests.test_oauth_consent import _sign_in
from agent.tests.test_support import biz, open_
from agent.tests.test_transcript import planned

OPENING = "dentist within 5 miles, $2,000 a month"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
WORDS = "The plan stopped loading after I typed my address."


def report(client, text=WORDS, page="/", **more):
    return client.post("/api/help", json={"text": text, "page": page, **more}, headers={**IP, "User-Agent": UA})


def ticket(conn) -> dict:
    return conn.execute(
        "SELECT business_id, status, contact_email, contact_verified, source, reason FROM support_ticket"
    ).fetchone()


def latest_help(conn) -> dict:
    row = conn.execute(
        "SELECT data FROM ticket_event WHERE kind = 'owner_words' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["data"]["help"]


def test_a_cold_visitor_reports_with_a_typed_address_and_only_the_notice_goes(client, conn, support_on):
    r = report(client, email=" Maria@Example.com ")
    assert r.status_code == 200 and r.json() == {"ok": True, "sent_to": "maria@example.com"}
    assert ticket(conn) == {
        "business_id": None,
        "status": "open",
        "contact_email": "maria@example.com",
        "contact_verified": False,
        "source": "home",
        "reason": "help",
    }
    assert [m["to"] for m in support_on.sent] == [["ops@example.com"]]  # the notice; no acknowledgement to it
    assert latest_help(conn) == {
        "page": "/",
        "signed_in": False,
        "browser": UA[: support_api.BROWSER_CHARS],
        "plan": None,
        "turns": [],
        "email": "maria@example.com",
    }
    row = conn.execute("SELECT actor, actor_class, business_id FROM agent_action WHERE tool = 'web.help'").fetchone()
    assert row == {"actor": "visitor:maria@example.com", "actor_class": "anonymous", "business_id": None}
    assert tools_account.VERBS["web.help"][0] == "{who} asked Admiry support for help"


def test_a_draft_visitor_s_report_carries_the_conversation_and_the_sign_in_verifies_it(
    client,
    conn,
    fake,  # noqa: F811 - fake is a fixture used by name
    support_on,
    google,
):
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    assert report(client, email="maria@example.com").json() == {"ok": True, "sent_to": "maria@example.com"}
    draft = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert ticket(conn) == {
        "business_id": draft,
        "status": "open",
        "contact_email": "maria@example.com",
        "contact_verified": False,
        "source": "home",
        "reason": "help",
    }
    help_ = latest_help(conn)
    assert help_["signed_in"] is False and help_["plan"] is None and help_["email"] == "maria@example.com"
    assert any(t["who"] == "owner" and t["text"] == OPENING for t in help_["turns"])
    assert 1 <= len(help_["turns"]) <= support_api.LAST_TURNS
    assert [m["to"] for m in support_on.sent] == [["ops@example.com"]]
    csrf = _sign_in(client, google)  # the same browser signs in: the draft is still nobody's, the address is Google's
    assert report(client, text="I signed in now.", csrf=csrf).json() == {"ok": True, "sent_to": "owner@example.com"}
    assert ticket(conn)["contact_verified"] is True
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    assert latest_help(conn)["signed_in"] is True and "email" not in latest_help(conn)
    assert [m["to"] for m in support_on.sent] == [
        ["ops@example.com"],
        ["owner@example.com"],
    ]  # the acknowledgement, now


def test_a_signed_in_owner_s_report_lands_on_the_page_s_business_with_its_plan(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    biz_id, csrf = planned(client, conn, google)
    plan = conn.execute("SELECT id, version, status FROM plan").fetchone()
    assert report(client, csrf=csrf).json() == {
        "ok": True,
        "sent_to": "owner@example.com",
    }  # the homepage: its latest plan
    assert ticket(conn) == {
        "business_id": biz_id,
        "status": "open",
        "contact_email": "owner@example.com",
        "contact_verified": True,
        "source": "home",
        "reason": "help",
    }
    help_ = latest_help(conn)
    assert help_["plan"] == {"id": plan["id"], "version": plan["version"], "status": plan["status"]}
    assert help_["signed_in"] is True and help_["browser"] == UA and 1 <= len(help_["turns"]) <= support_api.LAST_TURNS
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]
    r = report(client, text="And from the plan page.", page=f"/plan/{plan['id']}", plan_id=plan["id"], csrf=csrf)
    assert r.json() == {"ok": True, "sent_to": "owner@example.com"}  # appended: the page learns no more than that
    assert latest_help(conn)["page"] == f"/plan/{plan['id']}" and latest_help(conn)["plan"]["id"] == plan["id"]
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1


def test_another_business_s_plan_id_is_ignored(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """The id is read server-side and only when the plan is the business's own: a stranger's plan, or an id that is no
    plan, leaves the context without one (the page is not the homepage, so no latest either), and a malformed id is a
    400."""
    _, csrf = planned(client, conn, google)
    biz(conn, "biz_other", owner="someone@example.com")
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json) VALUES ('pl_feedfeedfeedfeed', 'biz_other', 1, '{}')"
    )
    for plan_id in ("pl_feedfeedfeedfeed", "pl_0000000000000000"):
        r = report(client, page=f"/plan/{plan_id}", plan_id=plan_id, csrf=csrf)
        assert r.json()["ok"] and latest_help(conn)["plan"] is None, plan_id
    assert conn.execute("SELECT business_id FROM support_ticket").fetchone()["business_id"] != "biz_other"
    assert report(client, plan_id="not-a-plan", csrf=csrf).status_code == 400


def test_a_report_on_an_answered_ticket_reopens_it_with_one_notice(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Review Focus 3: a person replied (the ticket answered) and the owner reports again: appended, the ticket back
    to open, one more ops notice, and no acknowledgement to anyone."""
    _, csrf = planned(client, conn, google)
    assert report(client, csrf=csrf).json()["ok"]
    tid = conn.execute("SELECT id FROM support_ticket").fetchone()["id"]
    support.reply(conn, tid, "Looking into it.", actor="admin:founder@example.com")
    support_mail.flush(tid)
    assert conn.execute("SELECT status FROM support_ticket").fetchone() == {"status": "answered"}
    sent_before = len(support_on.sent)
    assert report(client, text="Still broken.", csrf=csrf).json() == {"ok": True, "sent_to": "owner@example.com"}
    assert conn.execute("SELECT status FROM support_ticket").fetchone() == {"status": "open"}
    kinds = [
        r["kind"]
        for r in conn.execute("SELECT kind FROM ticket_event WHERE mail_state IS NOT NULL ORDER BY created_at")
    ]
    assert kinds == ["ack", "notice", "reply_out", "notice"]
    assert [m["to"] for m in support_on.sent[sent_before:]] == [["ops@example.com"]]


def test_a_visitor_without_a_usable_address_is_asked_for_one_and_nothing_is_written(client, conn, support_on):
    """Review Focus 2: no session, no cookie -- the address is the only way back. None, one that is not an address,
    one too long: refused in a sentence, nothing written, nothing mailed; and the other fields are checked too. A NUL
    in the words is dropped, not refused (support._clean)."""
    r = report(client)
    assert r.status_code == 400 and r.json() == {"ok": False, "error": support_api.NEED_EMAIL}
    for email in ("nope", "a@", "x" * 250 + "@example.com"):
        r = report(client, email=email)
        assert r.status_code == 400 and r.json() == {"ok": False, "error": support_api.NOT_AN_ADDRESS}, email
    for bad in (
        {"text": ""},
        {"text": "x" * (support_api.MAX_TEXT + 1)},
        {"text": 5},
        {"page": "/?x=1"},
        {"page": "http://x/"},
        {"page": "x"},
        {"page": 5},
        {"page": "/" + "a" * 200},
        {"email": 5},
        {"plan_id": "pl_zz"},
    ):
        body = {"text": WORDS, "page": "/", "email": "maria@example.com", **bad}
        assert client.post("/api/help", json=body, headers=IP).status_code == 400, bad
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0 and support_on.sent == []
    assert report(client, text="help\x00 me", email="maria@example.com").json()["ok"]
    assert conn.execute("SELECT summary FROM support_ticket").fetchone() == {"summary": "help me"}


def test_the_limits_per_ip_and_per_address(client, conn, support_on):
    """§3.5: a caller with no business gets SP1's five tickets a day per address; on the typed path this is dropped
    like D5's per-address limit (review round 1 #1: the page says only Sent, never LIMITED, so an IP over its limit
    cannot be told apart from an address with a report simply dropped). An address gets REPORTS_PER_ADDRESS_DAY
    reports a day, after which a report is dropped and logged while the page still says Sent -- a limit sentence
    would say the address has been busy (D5)."""
    for n in range(support.ANON_PER_IP_DAY):
        open_(
            conn,
            business_id=None,
            contact_email=f"v{n}@example.com",
            contact_verified=False,
            ip=IP["X-Forwarded-For"],
            report={"page": "/", "email": f"v{n}@example.com"},
        )
    r = report(client, email="one-more@example.com")
    assert r.status_code == 200 and r.json() == {"ok": True, "sent_to": "one-more@example.com"}
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == support.ANON_PER_IP_DAY
    for _ in range(support.REPORTS_PER_ADDRESS_DAY):  # one ticket, twenty reports: appends are not counted per IP
        open_(
            conn,
            business_id=None,
            contact_email="maria@example.com",
            contact_verified=False,
            ip="198.51.100.77",
            report={"page": "/", "email": "maria@example.com"},
        )
    assert support.reports_today(conn, "maria@example.com", verified=False) == support.REPORTS_PER_ADDRESS_DAY
    events = conn.execute("SELECT count(*) AS n FROM ticket_event").fetchone()["n"]
    r = report(client, email="maria@example.com")
    assert r.status_code == 200 and r.json() == {"ok": True, "sent_to": "maria@example.com"}
    assert conn.execute("SELECT count(*) AS n FROM ticket_event").fetchone()["n"] == events


def test_twenty_typed_reports_on_an_owner_s_address_dont_mute_the_owner(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Task 1's review fix: reports_today counts verified and typed reports apart, so a stranger typing an owner's
    own address 20 times does not stop the signed-in owner's own report."""
    _, csrf = planned(client, conn, google)
    for _ in range(support.REPORTS_PER_ADDRESS_DAY):
        open_(
            conn,
            business_id=None,
            contact_email="owner@example.com",
            contact_verified=False,
            ip="198.51.100.88",
            report={"page": "/", "email": "owner@example.com"},
        )
    assert support.reports_today(conn, "owner@example.com", verified=False) == support.REPORTS_PER_ADDRESS_DAY
    assert support.reports_today(conn, "owner@example.com", verified=True) == 0
    assert report(client, csrf=csrf).json() == {"ok": True, "sent_to": "owner@example.com"}


def test_off_it_answers_404_and_a_session_needs_its_token(client, conn, resend, google, monkeypatch):
    assert report(client, email="maria@example.com").status_code == 404
    monkeypatch.setenv("AGENT_SUPPORT", "on")
    _sign_in(client, google)
    assert report(client).status_code == 403  # a web session: its CSRF token is required, before anything is read
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def test_a_claimed_business_s_cookie_without_its_owner_s_session_files_nothing_under_it(
    client,
    conn,
    fake,  # noqa: F811 - fake is a fixture used by name
    support_on,
    google,
):
    """As /api/support: a browser holding someone else's business cookie never puts words on that business. Not signed
    in: a business-less ticket keyed by the typed address; signed in as another account: one keyed by the signer's."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'someone@example.com'")
    assert report(client, email="maria@example.com").json() == {"ok": True, "sent_to": "maria@example.com"}
    assert ticket(conn)["business_id"] is None and ticket(conn)["contact_verified"] is False
    csrf = _sign_in(client, google)  # owner@example.com, not someone@
    assert report(client, csrf=csrf).json() == {"ok": True, "sent_to": "owner@example.com"}
    rows = conn.execute(
        "SELECT business_id, contact_email, contact_verified FROM support_ticket ORDER BY created_at"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {"business_id": None, "contact_email": "maria@example.com", "contact_verified": False},
        {"business_id": None, "contact_email": "owner@example.com", "contact_verified": True},
    ]


def test_an_ip_over_its_limit_answers_the_same_whether_the_address_has_a_ticket_or_not(client, conn, support_on):
    """Review round 1 #1 (Important): open_or_append only checks the per-IP limit when no ticket already matches the
    typed address, so a caller over the limit got LIMITED for a fresh address but Sent for one that already has a
    ticket -- telling a caller whether an address had asked before, which §3.5 forbids. On the typed path this must
    answer exactly like D5's silent per-address drop: Sent, whichever address, nothing new written."""
    open_(
        conn,
        business_id=None,
        contact_email="has-ticket@example.com",
        contact_verified=False,
        ip=IP["X-Forwarded-For"],
        report={"page": "/", "email": "has-ticket@example.com"},
    )
    for n in range(support.ANON_PER_IP_DAY - 1):
        open_(
            conn,
            business_id=None,
            contact_email=f"v{n}@example.com",
            contact_verified=False,
            ip=IP["X-Forwarded-For"],
            report={"page": "/", "email": f"v{n}@example.com"},
        )
    before = conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"]
    r1 = report(client, email="has-ticket@example.com")
    r2 = report(client, email="brand-new@example.com")
    assert r1.status_code == 200 and r1.json() == {"ok": True, "sent_to": "has-ticket@example.com"}
    assert r2.status_code == 200 and r2.json() == {"ok": True, "sent_to": "brand-new@example.com"}
    # has-ticket's report appended to its own ticket (no new row); brand-new's was dropped like D5 (no new row either)
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == before


def test_a_protocol_relative_page_is_refused(client, conn, support_on):
    """Review round 1 #2: a page starting with two slashes is not a path on this site -- PAGE must refuse it."""
    assert report(client, page="//evil.com/x", email="maria@example.com").status_code == 400
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def test_a_typed_address_must_be_plain_ascii_with_no_markup_characters(client, conn, support_on):
    """Review round 1 #3: mail_grants.valid's shape regex alone lets a local part carry HTML or non-ASCII through --
    /api/help needs its own check on top of it."""
    for email in ("<svg/onload=x>@example.com", "héllo@example.com", "a,b@example.com", 'a"b@example.com'):
        r = report(client, email=email)
        assert r.status_code == 400 and r.json() == {"ok": False, "error": support_api.NOT_AN_ADDRESS}, email
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def test_a_typed_address_never_learns_a_verified_owner_s_address(client, conn, fake, support_on, google):  # noqa: F811 - fake is a fixture used by name
    """Review round 1 #4: once the draft's ticket has a verified contact (an earlier sign-in on the same browser), a
    later typed report on the same draft cookie (session gone, key cookie kept) must still answer with the address
    that was typed, never the ticket's real, verified contact -- support._current's business match finds that ticket
    whatever its verified state, and D2 says a typed address reaching a verified ticket changes nothing on it, so the
    raw contact_email must never reach the response on the typed path."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    assert report(client, email="maria@example.com").json()["ok"]
    csrf = _sign_in(client, google)
    assert report(client, text="I signed in now.", csrf=csrf).json() == {"ok": True, "sent_to": "owner@example.com"}
    client.cookies.delete(sessions.COOKIE)  # the session is gone; the draft's own key cookie stays
    r = report(client, text="A different visitor, same browser.", email="stranger@example.com")
    assert r.status_code == 200 and r.json() == {"ok": True, "sent_to": "stranger@example.com"}
    assert ticket(conn)["contact_email"] == "owner@example.com" and ticket(conn)["contact_verified"] is True


def test_logs_keep_only_the_address_s_domain(client, conn, support_on, caplog):
    """Review round 1 #5: the dropped-report and dropped-by-IP-limit log lines must never carry the full address."""
    caplog.set_level(logging.WARNING, logger="agent.support_api")
    for _ in range(support.REPORTS_PER_ADDRESS_DAY):
        open_(
            conn,
            business_id=None,
            contact_email="secret@example.com",
            contact_verified=False,
            ip="198.51.100.99",
            report={"page": "/", "email": "secret@example.com"},
        )
    report(client, email="secret@example.com")
    for n in range(support.ANON_PER_IP_DAY):
        open_(
            conn,
            business_id=None,
            contact_email=f"w{n}@example.com",
            contact_verified=False,
            ip=IP["X-Forwarded-For"],
            report={"page": "/", "email": f"w{n}@example.com"},
        )
    report(client, email="brand-new-2@example.com", page="/")
    text = "\n".join(r.message for r in caplog.records)
    assert "secret@example.com" not in text and "brand-new-2@example.com" not in text
    assert "example.com" in text
