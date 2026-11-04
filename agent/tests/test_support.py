"""Support tickets, database only (spec 2026-09-30 §3–§5): open with and without a contact, append, the anonymous and
hourly limits, the claim, the thread binding, replies, status, a reply on the thread, and the race."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import claims, db, support
from agent.tests.conftest import TEST_DB_URL


def biz(conn, biz_id="biz_1", claim_state="claimed", owner="owner@example.com") -> str:
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email) VALUES (%s, %s, %s)", (biz_id, claim_state, owner)
    )
    return biz_id


def open_(conn, business_id="biz_1", **kw) -> dict:
    args = {
        "business_id": business_id,
        "source": "home",
        "reason": "human",
        "words": "Can a person call me?",
        "summary": "",
        "actor": "key:k1",
    }
    args.update(kw)
    return support.open_or_append(conn, **args)


def events(conn, ticket_id) -> list[tuple]:
    return [
        (r["kind"], r["mail_state"])
        for r in conn.execute(
            "SELECT kind, mail_state FROM ticket_event WHERE ticket_id = %s ORDER BY created_at", (ticket_id,)
        )
    ]


def email_thread(conn, thread_id, status="operator") -> None:
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, status) "
        "VALUES (%s, 'biz_1', 'owner@example.com', %s, %s)",
        (thread_id, "tok_" + thread_id, status),
    )


def waiting_on_a_lock(conn, pid, timeout=5.0) -> bool:
    """True once the backend `pid` waits for a lock another transaction holds (asked from a second connection)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = conn.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
        if row and row["wait_event_type"] == "Lock":
            return True
        time.sleep(0.01)
    return False


def test_a_known_owner_s_ask_opens_a_ticket_with_the_mail_queued(conn):
    biz(conn)
    out = open_(conn)
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert out == {"ticket_id": t["id"], "status": "open", "appended": False, "contact_email": "owner@example.com"}
    assert (t["source"], t["reason"], t["summary"]) == ("home", "human", "Can a person call me?")
    assert events(conn, t["id"]) == [
        ("created", None),
        ("owner_words", None),
        ("ack", "pending"),
        ("notice", "pending"),
    ]
    thread = conn.execute("SELECT * FROM email_thread WHERE id = %s", (t["email_thread_id"],)).fetchone()
    assert thread["status"] == "operator" and thread["counterpart"] == "owner@example.com"
    assert t["id"] in thread["subject"]
    assert t["snapshot"]["error"] is None and t["snapshot"]["turns"][-1]["text"] == "Can a person call me?"
    assert support.say_for(out) == "I've passed this to the Admiry team. They'll reply by e-mail to owner@example.com."
    assert support.envelope_for(out)["next"] == {"kind": "none"}


def test_a_draft_waits_for_a_contact_and_the_claim_opens_it(conn):
    biz(conn, claim_state="draft", owner=None)
    out = open_(conn, draft=True, ip="198.51.100.9")
    assert out["status"] == "awaiting_contact" and out["contact_email"] is None
    assert events(conn, out["ticket_id"]) == [("created", None), ("owner_words", None)]
    env = support.envelope_for(out)
    assert env["say"] == support.SAY_NEW_ANON and env["data"]["support"]["status"] == "awaiting_contact"
    assert env["next"] == {"kind": "link", "link": {"url": support.SIGN_IN_URL, "label": "Sign in with Google"}}
    with conn.transaction():
        claims.claim(conn, "biz_1", "Owner@Example.com")
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert t["status"] == "open" and t["contact_email"] == "owner@example.com" and t["email_thread_id"]
    assert [k for k, _ in events(conn, t["id"])][-3:] == ["status", "ack", "notice"]


def test_a_later_ask_is_appended_not_a_second_ticket(conn):
    biz(conn)
    first = open_(conn)
    again = open_(conn, words="Still waiting", source="mcp", context="assistant: I suggested the cheaper level")
    assert again["ticket_id"] == first["ticket_id"] and again["appended"] is True
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    assert support.say_for(again, planned=True) == f"{support.SAY_APPENDED} {support.SAY_PLAN_TAIL}"
    kinds = [k for k, _ in events(conn, first["ticket_id"])]
    assert kinds.count("notice") == 1  # an append to an open ticket sends nothing new
    ctx = conn.execute("SELECT data FROM ticket_event WHERE body = 'Still waiting'").fetchone()["data"]
    assert ctx == {"context": "assistant: I suggested the cheaper level"}


def test_a_nul_is_dropped_from_the_words_the_summary_and_the_context(conn):
    """Postgres text and jsonb refuse a NUL and a caller can put one in any string, so none is ever written."""
    biz(conn)
    first = open_(conn, words="Call\x00 me", summary="Wants a\x00 call", context="assistant:\x00 hello")
    again = open_(conn, words="Still\x00 waiting", context="assistant:\x00 again")
    assert (first["appended"], again["appended"]) == (False, True)
    assert conn.execute("SELECT summary FROM support_ticket").fetchone() == {"summary": "Wants a call"}
    rows = conn.execute("SELECT body, data FROM ticket_event WHERE kind = 'owner_words' ORDER BY created_at")
    assert [(r["body"], r["data"]) for r in rows] == [
        ("Call me", {"context": "assistant: hello"}),
        ("Still waiting", {"context": "assistant: again"}),
    ]


def test_admirys_team_for_the_owners_website_is_a_reason_of_its_own(conn):
    """A3p-5 batch 2 (plan Q8, spec W12): `website` -- Admiry's team putting Admiry's code on the owner's site, which
    A3p-6's building help uses too -- opens a ticket like any reason; the table checks no reason, so no DDL follows.
    contact_support's own reasons, its public argument pinned by the contract, stay as they were."""
    from agent import tools_support

    assert support.REASONS == ("help", "human", "dissatisfied", "other", "website")
    assert tools_support.REASONS == ("help", "human", "dissatisfied")
    biz(conn)
    out = open_(conn, reason="website", words="Install Admiry's code on https://brightsmile.example (Wix)")
    t = conn.execute("SELECT reason, status, contact_email FROM support_ticket WHERE id = %s", (out["ticket_id"],))
    assert t.fetchone() == {"reason": "website", "status": "open", "contact_email": "owner@example.com"}
    checks = conn.execute(
        "SELECT count(*) AS n FROM pg_constraint WHERE conrelid = 'support_ticket'::regclass AND contype = 'c'"
    ).fetchone()
    assert checks["n"] == 0


def test_an_ask_that_brings_a_contact_opens_the_ticket_that_was_waiting(conn):
    """A draft visitor who signed in since (D133) asks again: the address is the ticket's, it opens, and the
    acknowledgement quotes the words that came with the address."""
    biz(conn, claim_state="draft", owner=None)
    first = open_(conn, draft=True, ip="198.51.100.9")
    assert first["status"] == "awaiting_contact"
    again = open_(conn, words="I signed in, please reply", contact_email="owner@example.com")
    assert again == {
        "ticket_id": first["ticket_id"],
        "status": "open",
        "appended": True,
        "contact_email": "owner@example.com",
    }
    assert events(conn, first["ticket_id"]) == [
        ("created", None),
        ("owner_words", None),
        ("owner_words", None),
        ("status", None),
        ("ack", "pending"),
        ("notice", "pending"),
    ]
    ack = conn.execute("SELECT body FROM ticket_event WHERE kind = 'ack'").fetchone()
    assert ack == {"body": "I signed in, please reply"}  # the new words are recorded before the acknowledgement quotes
    thread = conn.execute("SELECT status, counterpart FROM email_thread").fetchone()
    assert thread == {"status": "operator", "counterpart": "owner@example.com"}
    assert support.say_for(again) == support.SAY_APPENDED  # no "sign in" hint: the ticket has its address
    open_(conn, words="thanks", contact_email="owner@example.com")  # opened once: a later ask sends nothing new
    kinds = [k for k, _ in events(conn, first["ticket_id"])]
    assert kinds.count("ack") == 1 and kinds.count("notice") == 1


def test_the_contact_is_taken_even_when_the_words_are_over_the_hourly_limit(conn):
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, draft=True, ip="198.51.100.9")["ticket_id"]
    for n in range(support.APPENDS_PER_HOUR - 1):  # the first ask counts toward the hour, so this fills it
        open_(conn, words=f"again {n}")
    out = open_(conn, words="over the limit", contact_email="owner@example.com")
    assert (out["status"], out["contact_email"]) == ("open", "owner@example.com")
    n = conn.execute(
        "SELECT count(*) AS n FROM ticket_event WHERE ticket_id = %s AND kind = 'owner_words'", (tid,)
    ).fetchone()["n"]
    assert n == support.APPENDS_PER_HOUR  # the words are dropped, as ever
    ack = conn.execute("SELECT body FROM ticket_event WHERE kind = 'ack'").fetchone()
    assert ack == {"body": f"again {support.APPENDS_PER_HOUR - 2}"}  # the acknowledgement quotes the last it kept


def test_a_contact_that_came_with_a_thread_opens_the_waiting_ticket_once(conn):
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, draft=True, ip="198.51.100.9")["ticket_id"]
    email_thread(conn, "et_2")
    out = open_(
        conn,
        source="email",
        words="by mail",
        email_thread_id="et_2",
        contact_email="owner@example.com",
        actor="email:owner@example.com",
    )
    assert (out["ticket_id"], out["status"]) == (tid, "open")
    kinds = [k for k, _ in events(conn, tid)]
    # bind_thread opened it (the channel's reply is the acknowledgement): the address is not taken a second time
    assert kinds.count("notice") == 1 and kinds.count("status") == 1 and "ack" not in kinds


def test_the_anonymous_limit_per_address(conn):
    for n in range(support.ANON_PER_IP_DAY):
        biz(conn, f"biz_{n}", claim_state="draft", owner=None)
        open_(conn, business_id=f"biz_{n}", draft=True, ip="198.51.100.9")
    biz(conn, "biz_x", claim_state="draft", owner=None)
    with pytest.raises(support.Limited):
        open_(conn, business_id="biz_x", draft=True, ip="198.51.100.9")
    assert open_(conn, business_id="biz_x", draft=True, ip="198.51.100.10")["appended"] is False


def test_a_visitor_with_no_business_keeps_one_open_ticket(conn):
    first = open_(conn, business_id=None, contact_email="x@example.com", ip="198.51.100.9")
    again = open_(conn, business_id=None, contact_email="x@example.com", ip="198.51.100.9", words="Still waiting")
    assert (first["appended"], again["appended"]) == (False, True) and again["ticket_id"] == first["ticket_id"]
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    kinds = [k for k, _ in events(conn, first["ticket_id"])]
    assert kinds.count("ack") == 1 and kinds.count("notice") == 1  # the second ask sends nothing new
    other = open_(conn, business_id=None, contact_email="y@example.com", ip="198.51.100.9")  # another contact
    assert other["ticket_id"] != first["ticket_id"] and other["appended"] is False


def test_tickets_with_no_business_count_toward_the_address_limit(conn):
    for n in range(support.ANON_PER_IP_DAY):
        open_(conn, business_id=None, contact_email=f"v{n}@example.com", ip="198.51.100.9")
    with pytest.raises(support.Limited):
        open_(conn, business_id=None, contact_email="v9@example.com", ip="198.51.100.9")
    # an earlier visitor's next ask is an append, which the limit never touches; another address is not limited
    assert open_(conn, business_id=None, contact_email="v0@example.com", ip="198.51.100.9")["appended"] is True
    assert open_(conn, business_id=None, contact_email="v9@example.com", ip="198.51.100.10")["appended"] is False


def test_appends_past_the_hourly_limit_are_dropped(conn):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    for n in range(support.APPENDS_PER_HOUR + 3):
        assert open_(conn, words=f"again {n}")["appended"] is True
    n = conn.execute(
        "SELECT count(*) AS n FROM ticket_event WHERE ticket_id = %s AND kind = 'owner_words'", (tid,)
    ).fetchone()["n"]
    assert n == support.APPENDS_PER_HOUR  # the first ask counts toward the hour too


def test_a_thread_is_recorded_even_when_the_words_are_over_the_hourly_limit(conn):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    for n in range(support.APPENDS_PER_HOUR):  # the first ask counts toward the hour, so this fills it
        open_(conn, words=f"again {n}")
    email_thread(conn, "et_2")
    out = open_(
        conn,
        source="email",
        words="over the limit, by mail",
        email_thread_id="et_2",
        contact_email="owner@example.com",
        actor="email:owner@example.com",
    )
    assert out["appended"] is True
    assert (support.open_for_thread(conn, "et_2") or {}).get("id") == tid  # the thread is kept, the words are dropped
    dropped = conn.execute("SELECT count(*) AS n FROM ticket_event WHERE body = 'over the limit, by mail'").fetchone()
    assert dropped["n"] == 0


def test_a_reply_answers_and_the_owner_s_next_words_reopen(conn):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    out = support.reply(conn, tid, "Hi, this is Admiry support.", actor="ops:cli")
    row = conn.execute("SELECT status, first_reply_at FROM support_ticket").fetchone()
    assert row["status"] == "answered" and row["first_reply_at"] is not None
    ev = conn.execute("SELECT kind, mail_state, data FROM ticket_event WHERE id = %s", (out["event_id"],)).fetchone()
    assert ev == {"kind": "reply_out", "mail_state": "pending", "data": {"close": False}}
    open_(conn, words="That did not help")
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "open"
    assert [k for k, _ in events(conn, tid)].count("notice") == 2


def test_close_needs_a_resolution_and_hands_the_thread_back(conn):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    thread_id = conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"]
    with pytest.raises(support.Refused):
        support.set_status(conn, tid, "closed", actor="ops:cli")
    support.set_status(conn, tid, "closed", actor="ops:cli", resolution="not_support")
    row = conn.execute("SELECT status, resolution, closed_at FROM support_ticket").fetchone()
    assert (row["status"], row["resolution"]) == ("closed", "not_support") and row["closed_at"] is not None
    assert conn.execute("SELECT status FROM email_thread WHERE id = %s", (thread_id,)).fetchone()["status"] == "open"
    with pytest.raises(support.Refused, match=r"^this ticket is closed$"):  # no "reopen it first" -- nothing can
        support.reply(conn, tid, "late", actor="ops:cli")
    second = open_(conn, words="a new problem")  # after closed, a new ask opens a new ticket
    assert second["ticket_id"] != tid and second["appended"] is False
    with pytest.raises(support.Refused, match="another request"):
        support.set_status(conn, tid, "open", actor="ops:cli")


def test_an_e_mail_ticket_keeps_its_thread_and_queues_no_acknowledgement(conn):
    biz(conn, claim_state="draft", owner=None)
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, status) "
        "VALUES ('et_1', 'biz_1', 'owner@example.com', 'tok', 'operator')"
    )
    out = open_(
        conn,
        source="email",
        reason="other",
        words="(a loop)",
        email_thread_id="et_1",
        contact_email="owner@example.com",
        actor="email:owner@example.com",
    )
    assert out["status"] == "open"
    assert events(conn, out["ticket_id"]) == [("created", None), ("owner_words", None), ("notice", "pending")]


def test_an_answer_s_ticket_is_bound_to_its_thread_later(conn):
    biz(conn, claim_state="draft", owner=None)
    out = open_(conn, source="email", actor="key:k_mail")
    assert out["status"] == "awaiting_contact"
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token, status) "
        "VALUES ('et_1', 'biz_1', 'owner@example.com', 'tok', 'operator')"
    )
    support.bind_thread(conn, out["ticket_id"], "et_1", "owner@example.com")
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (t["status"], t["email_thread_id"], t["contact_email"]) == ("open", "et_1", "owner@example.com")
    kinds = [k for k, _ in events(conn, t["id"])]
    assert kinds[-1] == "notice" and "ack" not in kinds


def test_a_ticket_keeps_every_thread_it_took(conn):
    biz(conn)
    tid = open_(conn)["ticket_id"]  # a home ticket with a contact: its acknowledgement made it a thread
    own = conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"]
    email_thread(conn, "et_2")  # the owner's e-mail thread, waiting for a person
    support.bind_thread(conn, tid, "et_2", "owner@example.com")
    support.bind_thread(conn, tid, own, "owner@example.com")  # its own thread again: nothing to record
    assert conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"] == own
    assert conn.execute("SELECT actor, data FROM ticket_event WHERE kind = 'thread'").fetchall() == [
        {"actor": "admiry", "data": {"thread_id": "et_2"}}
    ]
    assert (support.open_for_thread(conn, "et_2") or {}).get("id") == tid
    assert (support.open_for_thread(conn, own) or {}).get("id") == tid
    support.set_status(conn, tid, "closed", actor="ops:cli", resolution="resolved")
    statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM email_thread")}
    assert statuses == {own: "open", "et_2": "open"}


def test_a_message_on_the_ticket_s_thread_is_a_reply_in(conn):
    biz(conn)
    tid = open_(conn)["ticket_id"]
    thread_id = conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"]
    support.reply(conn, tid, "Hi", actor="ops:cli")
    row = {"id": "em_in", "from_addr": "owner@example.com"}
    assert support.on_inbound(conn, thread_id, row, "Thanks, one more thing") == tid
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "open"
    assert support.on_inbound(conn, "et_none", {"id": "em_x", "from_addr": "x@example.com"}, "hello") is None


def test_replies_past_the_hourly_limit_are_dropped_and_change_nothing(conn, caplog):
    """The e-mail channel's breakers and its plain messages both land here, so one ticket's flood is bounded here: the
    owner's words and replies together, APPENDS_PER_HOUR an hour; the next is logged, records nothing, moves no status."""
    biz(conn)
    tid = open_(conn)["ticket_id"]  # the first ask counts toward the hour
    thread_id = conn.execute("SELECT email_thread_id FROM support_ticket").fetchone()["email_thread_id"]
    for n in range(support.APPENDS_PER_HOUR - 1):  # ... so this fills it
        row = {"id": f"em_{n}", "from_addr": "owner@example.com"}
        assert support.on_inbound(conn, thread_id, row, f"again {n}") == tid
    support.reply(conn, tid, "Hi", actor="ops:cli")  # answered: the owner's next words would reopen it, with a notice
    before = events(conn, tid)
    over = {"id": "em_over", "from_addr": "owner@example.com"}
    with caplog.at_level("WARNING", logger="agent.support"):
        assert support.on_inbound(conn, thread_id, over, "over the limit") == tid  # the ticket still holds the thread
    assert events(conn, tid) == before
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "answered"
    assert "dropped" in caplog.text


def test_a_second_reply_waits_until_the_first_is_sent_or_given_up(conn):
    """A reply that failed to send stays in the outbox for the support-mail job; a second one now would reach the owner
    twice. Only a reply_out counts: the acknowledgement and the notice are pending too."""
    biz(conn)
    tid = open_(conn)["ticket_id"]
    first = support.reply(conn, tid, "First", actor="ops:cli")["event_id"]
    mark = "UPDATE ticket_event SET mail_state = %s, mail_attempts = %s WHERE id = %s"
    for state, tries in (("pending", 0), ("failed", 1), ("failed", support.MAIL_ATTEMPTS - 1)):
        conn.execute(mark, (state, tries, first))
        with pytest.raises(support.Refused, match="an earlier reply is still waiting to be sent"):
            support.reply(conn, tid, "Second", actor="ops:cli")
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'reply_out'").fetchone()["n"] == 1
    conn.execute(mark, ("failed", support.MAIL_ATTEMPTS, first))  # the job gave up on it: a person may write again
    assert support.reply(conn, tid, "Second", actor="ops:cli")["event_id"]
    conn.execute("UPDATE ticket_event SET mail_state = 'sent' WHERE kind = 'reply_out'")  # both went out
    assert support.reply(conn, tid, "Third", actor="ops:cli")["event_id"]


def test_two_racing_asks_make_one_ticket(conn):
    biz(conn)
    results, barrier = [], threading.Barrier(2)

    def ask(n):
        with db.connect(TEST_DB_URL) as c:
            barrier.wait()
            results.append(open_(c, words=f"ask {n}"))

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    assert sorted(r["appended"] for r in results) == [False, True]


def test_a_claim_racing_an_ask_never_strands_the_ticket(conn):
    """An ask that read no owner while a claim was in flight (D133): the ticket must not stay awaiting_contact for a
    business that is claimed by the time it exists. The claim's transaction stays open until the ask is provably
    waiting on its row lock, so every run interleaves the two the same way."""
    biz(conn, claim_state="draft", owner=None)
    pids = queue.Queue()

    def ask():
        with db.connect(TEST_DB_URL) as b:
            pids.put(b.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            return open_(b, draft=True)

    with db.connect(TEST_DB_URL) as a, ThreadPoolExecutor(max_workers=1) as pool:
        with a.transaction():
            claims.claim(a, "biz_1", "owner@example.com")  # holds the business row; its on_claim found no ticket
            asked = pool.submit(ask)
            assert waiting_on_a_lock(a, pids.get(timeout=5)), "the ask never reached the claim's lock"
        asked.result(timeout=10)
    rows = conn.execute("SELECT id, status, contact_email FROM support_ticket").fetchall()
    assert [(r["status"], r["contact_email"]) for r in rows] == [("open", "owner@example.com")]
    assert events(conn, rows[0]["id"]) == [
        ("created", None),
        ("owner_words", None),
        ("ack", "pending"),
        ("notice", "pending"),
    ]


def test_an_ask_does_not_wait_for_a_plain_update_of_the_business_row(conn):
    """The owner read takes a key-share lock: it conflicts with the claim's FOR UPDATE (the race above) but not with an
    ordinary UPDATE of the row, which other flows do all the time; a share lock would make the ask wait for them."""
    biz(conn)

    def ask():
        with db.connect(TEST_DB_URL) as b:
            return open_(b)

    with db.connect(TEST_DB_URL) as a, ThreadPoolExecutor(max_workers=1) as pool, a.transaction():
        a.execute("UPDATE business SET updated_at = now() WHERE id = 'biz_1'")  # plain, and still uncommitted
        assert pool.submit(ask).result(timeout=5)["status"] == "open"  # the ask is done while it is open


# ---------------------------------------------------------------- the final review


def test_an_e_mail_ticket_waits_for_its_thread_s_address(conn):
    """F1: the answer tool's read opens an e-mail ticket before the channel binds its thread (§4.4). Even on a business
    with an owner it waits for that thread's address -- whoever asked may be an address the owner allowed -- and opens,
    with its notice, when the thread brings it."""
    biz(conn)  # claimed: the owner's address is known
    out = open_(conn, source="email", actor="key:k_mail")
    assert (out["status"], out["contact_email"]) == ("awaiting_contact", None)
    assert events(conn, out["ticket_id"]) == [("created", None), ("owner_words", None)]  # nothing to send yet
    email_thread(conn, "et_1")
    support.bind_thread(conn, out["ticket_id"], "et_1", "cue@example.com")
    t = conn.execute("SELECT status, contact_email, email_thread_id FROM support_ticket").fetchone()
    assert t == {"status": "open", "contact_email": "cue@example.com", "email_thread_id": "et_1"}
    assert events(conn, out["ticket_id"])[-1] == ("notice", "pending")


def test_the_first_thread_a_ticket_takes_brings_its_address(conn):
    """F1: a ticket open with an address and no thread yet (an e-mail ticket a claim opened before the channel bound its
    thread) takes the address of the first thread it is given: that is who asked. A later thread changes nothing."""
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, source="email", actor="key:k_mail")["ticket_id"]
    support.on_claim(conn, "biz_1", "owner@example.com")  # open with the owner's address, still without a thread
    email_thread(conn, "et_1")
    support.bind_thread(conn, tid, "et_1", "cue@example.com")
    assert conn.execute("SELECT contact_email FROM support_ticket").fetchone() == {"contact_email": "cue@example.com"}
    email_thread(conn, "et_2")
    support.bind_thread(conn, tid, "et_2", "other@example.com")
    assert conn.execute("SELECT contact_email FROM support_ticket").fetchone() == {"contact_email": "cue@example.com"}


def test_a_reply_goes_on_a_thread_the_ticket_holds_and_no_other(conn):
    """F1: email-reply names the thread it answers; it must be one of the ticket's, and the reply keeps it."""
    biz(conn)
    tid = open_(conn)["ticket_id"]
    email_thread(conn, "et_2")
    support.bind_thread(conn, tid, "et_2", "owner@example.com")
    email_thread(conn, "et_9")  # the business's, but never this ticket's
    with pytest.raises(support.Refused, match="that thread is not this ticket's"):
        support.reply(conn, tid, "Hi", actor="ops:cli", thread_id="et_9")
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'reply_out'").fetchone()["n"] == 0
    assert conn.execute("SELECT status FROM support_ticket").fetchone()["status"] == "open"
    out = support.reply(conn, tid, "Hi", actor="ops:cli", thread_id="et_2")
    data = conn.execute("SELECT data FROM ticket_event WHERE id = %s", (out["event_id"],)).fetchone()["data"]
    assert data == {"close": False, "thread_id": "et_2"}


def test_a_second_thread_is_recorded_once(conn):
    """F13: three messages that each bring the ticket its second thread record it once."""
    biz(conn)
    tid = open_(conn)["ticket_id"]
    email_thread(conn, "et_2")
    for n in range(3):
        open_(
            conn,
            source="email",
            words=f"by mail {n}",
            email_thread_id="et_2",
            contact_email="owner@example.com",
            actor="email:owner@example.com",
        )
    assert conn.execute("SELECT data FROM ticket_event WHERE kind = 'thread'").fetchall() == [
        {"data": {"thread_id": "et_2"}}
    ]
    assert (support.open_for_thread(conn, "et_2") or {}).get("id") == tid


def test_an_ask_that_brings_an_address_waits_for_a_claim_without_a_deadlock(conn):
    """F12, Task 8's reproduction: an ask that brings an address to the draft's waiting ticket makes its acknowledgement
    thread, whose foreign key reads the business row -- the row a claim holds first, before on_claim locks the ticket.
    open_or_append takes the business before the ticket, the claim's order, so the ask waits for the claim instead of
    holding the ticket the claim needs. The claim holds the row until the ask provably waits on it."""
    biz(conn, claim_state="draft", owner=None)
    tid = open_(conn, draft=True, ip="198.51.100.9")["ticket_id"]  # awaiting_contact
    pids = queue.Queue()

    def ask():
        with db.connect(TEST_DB_URL) as b:
            pids.put(b.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            return open_(b, words="signed in, reply please", contact_email="owner@example.com")

    with db.connect(TEST_DB_URL) as a, ThreadPoolExecutor(max_workers=1) as pool:
        with a.transaction():
            a.execute("SELECT id FROM business WHERE id = 'biz_1' FOR UPDATE")  # claims.claim's first statement
            asked = pool.submit(ask)
            assert waiting_on_a_lock(conn, pids.get(timeout=5)), "the ask never waited on the claim's business row"
            assert support.on_claim(a, "biz_1", "owner@example.com") == tid  # claims.claim's last statement
        out = asked.result(timeout=15)
    assert (out["ticket_id"], out["status"], out["appended"]) == (tid, "open", True)
    kinds = [k for k, _ in events(conn, tid)]
    assert kinds.count("ack") == 1 and kinds.count("notice") == 1 and kinds.count("owner_words") == 2


def test_source_of_a_key():
    from agent.keys import KeyRecord

    def key(**kw):
        base = {"id": "k", "business_id": "b", "label": "muse", "cls": "business", "scopes": ("read",)}
        base.update(expires_at=None, revoked_at=None, last_used_at=None, source="key", grant_id=None)
        base.update(kw)
        return KeyRecord(**base)

    assert support.source_of(key(source="email", cls="draft", label="email")) == "email"
    assert support.source_of(key(cls="draft", label="draft")) == "home"
    assert support.source_of(key(label="website")) == "home"
    assert support.source_of(key(source="oauth", label="claude")) == "mcp"


# ---------------------------------------------------------------- spec 2026-10-12 §3.3, §3.5: an address nobody verified

HELP = {"page": "/", "signed_in": False, "browser": "Safari on iPhone", "plan": None, "turns": []}


def typed(conn, address="maria@example.com", **kw) -> dict:
    """A Help report on the draft biz_1 by a visitor who is not signed in: the address is typed, not verified."""
    args = {
        "reason": "help",
        "contact_email": address,
        "contact_verified": False,
        "draft": True,
        "ip": "198.51.100.9",
        "report": {**HELP, "email": address},
    }
    args.update(kw)
    return open_(conn, **args)


def test_a_typed_address_opens_a_ticket_that_gets_no_acknowledgement(conn):
    """Spec §3.5: an address nobody verified is mailed nothing on its own -- the notice goes to ops, no acknowledgement
    exists, no thread is made; the ticket is open (a person can reply knowingly) and marked unverified; the report's
    context is on its owner_words event, apart from an assistant's `context`."""
    biz(conn, claim_state="draft", owner=None)
    out = typed(conn)
    t = conn.execute("SELECT status, contact_email, contact_verified, email_thread_id FROM support_ticket").fetchone()
    assert out["status"] == "open" and out["contact_email"] == "maria@example.com"
    assert t == {
        "status": "open",
        "contact_email": "maria@example.com",
        "contact_verified": False,
        "email_thread_id": None,
    }
    assert events(conn, out["ticket_id"]) == [("created", None), ("owner_words", None), ("notice", "pending")]
    data = conn.execute("SELECT data FROM ticket_event WHERE kind = 'owner_words'").fetchone()["data"]
    assert data["help"] == {**HELP, "email": "maria@example.com"} and "context" not in data


def test_every_earlier_ticket_and_every_signed_in_address_is_verified(conn):
    biz(conn)
    out = open_(conn)  # SP1's own path: the owner's address, which Google verified at the claim
    assert conn.execute("SELECT contact_verified FROM support_ticket").fetchone() == {"contact_verified": True}
    assert events(conn, out["ticket_id"])[2:] == [("ack", "pending"), ("notice", "pending")]


def test_a_typed_address_never_joins_a_verified_ticket_and_gets_no_mail(conn):
    """Review Focus 1: someone types an owner's own address. The owner's open ticket is not theirs to add to, and the
    owner is mailed nothing: a second ticket, keyed by the address but unverified, with the ops notice alone; a later
    typed report joins that one, never the owner's."""
    first = open_(conn, business_id=None, contact_email="owner@example.com", ip="198.51.100.1")
    second = typed(conn, "owner@example.com", business_id=None, draft=False, ip="198.51.100.2")
    assert first["ticket_id"] != second["ticket_id"] and not second["appended"]
    rows = conn.execute("SELECT contact_verified FROM support_ticket ORDER BY created_at").fetchall()
    assert [r["contact_verified"] for r in rows] == [True, False]
    assert events(conn, second["ticket_id"]) == [("created", None), ("owner_words", None), ("notice", "pending")]
    third = typed(conn, "owner@example.com", business_id=None, draft=False, ip="198.51.100.2")
    assert third["ticket_id"] == second["ticket_id"] and third["appended"]
    assert events(conn, first["ticket_id"]) == [
        ("created", None),
        ("owner_words", None),
        ("ack", "pending"),
        ("notice", "pending"),
    ]


def test_a_signed_in_address_verifies_the_ticket_it_joins_and_the_acknowledgement_goes_then(conn):
    """A visitor typed an address on their draft, then signed in with the same browser and reported again: the ask
    that brings the verified address makes it the ticket's, and SP1's acknowledgement goes now -- once, with no second
    ops notice (a person has the ticket already)."""
    biz(conn, claim_state="draft", owner=None)
    first = typed(conn)
    second = open_(
        conn,
        reason="help",
        contact_email="maria@gmail.example",
        draft=True,
        ip="198.51.100.9",
        report={**HELP, "signed_in": True},
    )
    assert second == {
        "ticket_id": first["ticket_id"],
        "status": "open",
        "appended": True,
        "contact_email": "maria@gmail.example",
    }
    t = conn.execute("SELECT contact_verified, email_thread_id FROM support_ticket").fetchone()
    assert t["contact_verified"] and t["email_thread_id"] is not None
    assert events(conn, first["ticket_id"]) == [
        ("created", None),
        ("owner_words", None),
        ("notice", "pending"),
        ("owner_words", None),
        ("contact", None),
        ("ack", "pending"),
    ]
    assert conn.execute("SELECT counterpart FROM email_thread").fetchone() == {"counterpart": "maria@gmail.example"}
    contact = conn.execute("SELECT data FROM ticket_event WHERE kind = 'contact'").fetchone()["data"]
    assert contact == {"email": "maria@gmail.example", "was": "maria@example.com", "verified": True}


def test_a_later_typed_address_replaces_an_unverified_one_never_a_verified_one(conn):
    biz(conn, claim_state="draft", owner=None)
    tid = typed(conn, "a@example.com")["ticket_id"]
    typed(conn, "b@example.com")
    assert conn.execute("SELECT contact_email, contact_verified FROM support_ticket").fetchone() == {
        "contact_email": "b@example.com",
        "contact_verified": False,
    }
    open_(conn, reason="help", contact_email="owner@example.com", draft=True, ip="198.51.100.9", report=HELP)
    typed(conn, "c@example.com")
    assert conn.execute("SELECT contact_email, contact_verified FROM support_ticket").fetchone() == {
        "contact_email": "owner@example.com",
        "contact_verified": True,
    }
    assert [k for k, _ in events(conn, tid)].count("ack") == 1


def test_the_claim_verifies_a_typed_address_once(conn):
    """Review Focus 4 and spec §3.3: the visitor who typed an address claims the business with Google (claims.claim's
    on_claim): the ticket's address becomes the verified one and the acknowledgement goes then, once; a second claim
    finds nothing to do."""
    biz(conn, claim_state="draft", owner=None)
    tid = typed(conn)["ticket_id"]
    assert support.on_claim(conn, "biz_1", "Maria@Gmail.example") == tid
    t = conn.execute("SELECT status, contact_email, contact_verified FROM support_ticket").fetchone()
    assert t == {"status": "open", "contact_email": "maria@gmail.example", "contact_verified": True}
    assert events(conn, tid)[-2:] == [("contact", None), ("ack", "pending")]
    assert support.on_claim(conn, "biz_1", "maria@gmail.example") is None
    assert [k for k, _ in events(conn, tid)].count("ack") == 1


def test_a_person_s_earlier_thread_follows_the_verified_address(conn):
    """A person replied to the typed address (support.reply made a thread for it); the visitor then claims: the thread
    talks to the verified address from now on (its counterpart), so the next reply goes there."""
    biz(conn, claim_state="draft", owner=None)
    tid = typed(conn)["ticket_id"]
    support.reply(conn, tid, "Looking into it.", actor="admin:founder@example.com")
    assert conn.execute("SELECT counterpart FROM email_thread").fetchone() == {"counterpart": "maria@example.com"}
    support.on_claim(conn, "biz_1", "maria@gmail.example")
    assert conn.execute("SELECT counterpart FROM email_thread").fetchone() == {"counterpart": "maria@gmail.example"}


def test_reports_today_counts_help_reports_by_their_address(conn):
    biz(conn, claim_state="draft", owner=None)
    assert support.reports_today(conn, "maria@example.com", verified=False) == 0
    typed(conn)
    typed(conn)
    open_(conn)  # SP1's own path on the same ticket: words without a report are no report
    assert support.reports_today(conn, "maria@example.com", verified=False) == 2
    assert support.reports_today(conn, "owner@example.com", verified=False) == 0


# ---------------------------------------------------------------- fix round 1 (review)


def test_a_typed_address_replacement_moves_the_threads_counterpart_with_it(conn):
    """Review fix 1: typed a, a person replies (the thread's counterpart is a), typed b (the thread must follow,
    never keep mailing a), claim c (the thread follows again) -- every later reply reaches the address the ticket
    actually holds now."""
    biz(conn, claim_state="draft", owner=None)
    tid = typed(conn, "a@example.com")["ticket_id"]
    support.reply(conn, tid, "Looking into it.", actor="admin:founder@example.com")
    assert conn.execute("SELECT counterpart FROM email_thread").fetchone() == {"counterpart": "a@example.com"}
    typed(conn, "b@example.com")
    assert conn.execute("SELECT counterpart FROM email_thread").fetchone() == {"counterpart": "b@example.com"}
    support.on_claim(conn, "biz_1", "c@gmail.example")
    assert conn.execute("SELECT counterpart FROM email_thread").fetchone() == {"counterpart": "c@gmail.example"}


def test_reports_today_counts_verified_and_typed_reports_apart(conn):
    """Review fix 2: a stranger typing the owner's own address need not touch the owner's verified count -- a typed
    caller's count and a verified caller's count are kept on opposite sides of contact_verified."""
    biz(conn, claim_state="draft", owner=None)
    for i in range(20):
        typed(conn, "owner@example.com", ip=f"198.51.100.{i}")
    assert support.reports_today(conn, "owner@example.com", verified=False) == 20
    assert support.reports_today(conn, "owner@example.com", verified=True) == 0


def test_verify_skips_the_acknowledgement_once_a_person_has_replied(conn):
    """Review fix 3: a person already replied before the claim or sign-in that verifies the address -- nothing left
    to acknowledge, so _verify queues no ack event."""
    biz(conn, claim_state="draft", owner=None)
    tid = typed(conn)["ticket_id"]
    support.reply(conn, tid, "Looking into it.", actor="admin:founder@example.com")
    support.on_claim(conn, "biz_1", "maria@gmail.example")
    assert [k for k, _ in events(conn, tid)].count("ack") == 0


def test_current_prefers_the_verified_ticket_when_more_than_one_matches(conn):
    """Review fix 4: a signed-in ask's own ticket must never lose to an older, unverified one of the same address --
    _current orders the verified match first."""
    conn.execute(
        "INSERT INTO support_ticket (id, business_id, source, reason, status, contact_email, contact_verified, "
        "summary, snapshot, created_at) VALUES "
        "('tk_old', NULL, 'home', 'help', 'open', 'x@example.com', false, '', '{}'::jsonb, now() - interval '1 hour'), "
        "('tk_new', NULL, 'home', 'help', 'open', 'x@example.com', true, '', '{}'::jsonb, now())"
    )
    found = support._current(conn, None, None, "x@example.com", verified=True)
    assert found["id"] == "tk_new"
