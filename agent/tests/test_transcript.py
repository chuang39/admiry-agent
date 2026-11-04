"""Conversation snapshots (spec 2026-09-30 §7, D147): the consultation as one block, later turns by time, e-mail both
ways (purged text marked), the cut at a plan version, the bounds, and a failure that leaves the caller's transaction
usable."""

from datetime import datetime, timedelta, timezone

from agent import audit, benchmark, consult, listings, tools_start, transcript
from agent.tests.test_home_api import _answer_all, fake, turn  # noqa: F401 - fake is a fixture used by name
from agent.tests.test_oauth_consent import _sign_in

OPENING = "dentist within 5 miles, $2,000 a month"


def planned(client, conn, google) -> tuple[str, str]:
    """A homepage consultation through recommend, its owner signed in; (business id, the session's csrf)."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": OPENING, "idempotency_key": "i1"})
    _answer_all(client)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    csrf = _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    return conn.execute("SELECT id FROM business").fetchone()["id"], csrf


def test_the_homepage_conversation_and_its_plan(client, conn, fake, google):  # noqa: F811 - fixture used by name
    biz, _ = planned(client, conn, google)
    snap = transcript.build(conn, biz)
    assert snap["error"] is None and snap["business"]["id"] == biz and snap["dropped"] == 0
    who = [t["who"] for t in snap["turns"]]
    texts = [t["text"] for t in snap["turns"]]
    assert who[:2] == ["owner", "admiry"] and texts[0] == OPENING
    first_q = next(n for n, t in enumerate(texts) if "Options:" in t)
    assert who[first_q : first_q + 2] == ["admiry", "owner"]
    assert "svc_invisalign" not in " ".join(texts)  # the owner's pick reads as its label, never an option id
    assert who[-1] == "admiry" and texts[-1].startswith("Here is the plan")  # recommend's say
    assert snap["plan"]["version"] == 1 and snap["plan"]["cards"]["budget"]["monthly_usd"] == 2000
    assert {t["via"] for t in snap["turns"]} == {"home"}


def test_a_site_start_could_not_open_is_asked_about_once_in_its_own_words(client, conn, fake, monkeypatch):  # noqa: F811 - fixture used by name
    """Ruling R0-i (A3p-5 batch 2 Task 0, item d): the website question for an address start could not open is asked in
    its own words in start's say ("I couldn't open ..."); the transcript reads each question's words with the session's
    known facts, so it never adds the usual website question, which the owner was not asked."""
    monkeypatch.setattr(tools_start.crawl, "fetch_site", lambda url: [])  # nothing read: a timeout, say
    turn(
        client,
        {"action": "start", "text": "dentist at https://brightsmile.example in Palo Alto", "idempotency_key": "i1"},
    )
    answer = {"website": {"other": "brightsmile.example"}}
    turn(client, {"action": "answer", "answers": answer, "idempotency_key": "w1"})
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    texts = [t["text"] for t in transcript.build(conn, biz)["turns"]]
    assert consult.UNOPENED_PROMPT.format(host="brightsmile.example") in texts[1]  # start's say
    assert texts[2] == "brightsmile.example"  # the owner's answer, right after it
    assert not any(consult.WEBSITE_PROMPT in t for t in texts)


def test_free_text_after_the_plan_is_kept_whole_and_older_rows_are_marked(client, conn, fake, google):  # noqa: F811 - fixture used by name
    biz, _ = planned(client, conn, google)
    words = " ".join(["please make it a bit cheaper for us"] * 6)  # longer than the audit's 80-character arguments
    turn(client, {"action": "say", "text": words, "idempotency_key": "s1"})
    row = conn.execute("SELECT result FROM agent_action WHERE tool = 'web.say'").fetchone()
    assert row["result"]["text"] == words
    audit.record(
        conn,
        tool="web.say",
        args={"text": "an older row " * 10},
        result={"understood": False},
        ok=True,
        business_id=biz,
        actor="key:k_old",
        actor_class="business",
        api_key_id=None,
        ip=None,
    )
    owner = [t for t in transcript.build(conn, biz)["turns"] if t["who"] == "owner"]
    assert owner[-2]["text"] == words and owner[-2]["truncated"] is False
    assert len(owner[-1]["text"]) <= 80 and owner[-1]["truncated"] is True


def test_an_unlink_by_the_owners_words_shows_admirys_reply(client, conn, fake, google):  # noqa: F811 - fixture used by name
    """Final review M4: the owner's "not my business" is a web.say row; the reply is the say of the update_business
    command the turn ran, so a ticket's or a vote's snapshot shows it after the words and not an unanswered message."""
    from agent.tests.test_home_api import AFTER_PLAN_SAY, NOT_MINE, _adjust_read

    biz, _ = planned(client, conn, google)
    fake.adjusts[NOT_MINE] = _adjust_read(listing_not_mine=True)
    turn(client, {"action": "say", "text": NOT_MINE, "idempotency_key": "s1"})
    turns = transcript.build(conn, biz)["turns"]
    assert [(t["who"], t["via"]) for t in turns[-2:]] == [("owner", "home"), ("admiry", "home")]
    assert turns[-2]["text"] == NOT_MINE
    assert AFTER_PLAN_SAY.removesuffix(listings.LINK_AGAIN) == turns[-1]["text"]  # the command's say, before `then`


def _mail(conn, id_, direction, text, sent_by, outcome, at, thread="et_m"):
    conn.execute(
        "INSERT INTO email_message (id, thread_id, direction, message_id, from_addr, new_text, sent_by, outcome, "
        "created_at) VALUES (%s, %s, %s, %s, 'owner@example.com', %s, %s, %s, %s)",
        (id_, thread, direction, f"<{id_}@example.com>", text, sent_by, outcome, at),
    )


def test_e_mail_both_ways_and_a_purged_message(conn):
    conn.execute("INSERT INTO business (id, claim_state) VALUES ('biz_m', 'claimed')")
    conn.execute(
        "INSERT INTO email_thread (id, business_id, counterpart, token) VALUES ('et_m', 'biz_m', 'owner@example.com', 't1')"
    )
    t0 = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    _mail(conn, "em_1", "in", "Can I get a call?", None, "answered", t0)
    _mail(conn, "em_2", "out", "I have passed this on", "admiry", "sent", t0 + timedelta(minutes=1))
    _mail(conn, "em_3", "out", "Hi, this is Admiry support.", "ops", "sent", t0 + timedelta(minutes=2))
    _mail(conn, "em_4", "in", None, None, "queued", t0 + timedelta(minutes=3))
    _mail(conn, "em_5", "in", "an auto-reply", None, "dropped:auto", t0 + timedelta(minutes=4))
    _mail(conn, "em_6", "out", "never delivered", "admiry", "failed", t0 + timedelta(minutes=5))
    turns = transcript.build(conn, "biz_m")["turns"]
    assert [(t["who"], t["text"]) for t in turns] == [
        ("owner", "Can I get a call?"),
        ("admiry", "I have passed this on"),
        ("ops", "Hi, this is Admiry support."),
        ("owner", transcript.PURGED),
    ]
    assert {t["via"] for t in turns} == {"email"}


def test_a_thread_without_a_business(conn):
    conn.execute("INSERT INTO email_thread (id, counterpart, token) VALUES ('et_n', 'x@example.com', 't2')")
    _mail(conn, "em_n", "in", "hello?", None, "answered", datetime.now(timezone.utc), thread="et_n")
    snap = transcript.build(conn, None, thread_id="et_n")
    assert snap["business"] is None and snap["plan"] is None and [t["text"] for t in snap["turns"]] == ["hello?"]


def test_a_vote_s_snapshot_stops_at_its_plan_version(client, conn, fake, google):  # noqa: F811 - fixture used by name
    biz, _ = planned(client, conn, google)
    v1 = conn.execute("SELECT id FROM plan").fetchone()["id"]
    turn(client, {"action": "adjust", "level": "conservative", "idempotency_key": "j1"})
    snap = transcript.build(conn, biz, plan_id=v1)
    latest = transcript.build(conn, biz)
    assert snap["plan"]["id"] == v1 and latest["plan"]["version"] == 2
    assert snap["turns"][-1]["text"].startswith("Here is the plan")  # recommend's say: the call that made v1
    assert len(latest["turns"]) == len(snap["turns"]) + 1  # adjust's say comes after v1


def test_the_plans_cards_centre_on_a_placed_address_when_no_name_is_vouched_for(conn):
    """Walk fixes 1, Task 4 round 1: the plan version as the owner saw it. A business with no website whose only
    listing is an address it placed: the cards' sentence centres on the address, as recommend's did, not on "your
    business"."""
    from agent.tests.test_ads_run import KNOWLEDGE, sections, seed
    from agent.tests.test_state import consulted

    placed = {**KNOWLEDGE["listing"], "kind": "address"}
    seed(conn, status="draft", secs=consulted(sections()), knowledge={**KNOWLEDGE, "website": None, "listing": placed})
    goal = transcript.build(conn, "biz_1")["plan"]["cards"]["plan"]["goal"]
    assert goal == "More Invisalign bookings within 5 miles of 500 University Ave, Palo Alto, CA"


def test_the_bounds(conn):
    conn.execute("INSERT INTO business (id, claim_state) VALUES ('biz_b', 'claimed')")
    for n in range(transcript.MAX_TURNS + 5):
        audit.record(
            conn,
            tool="web.say",
            args={"text": "x"},
            result={"text": f"words {n} " + "y" * 3000},
            ok=True,
            business_id="biz_b",
            actor="key:k",
            actor_class="business",
            api_key_id=None,
            ip=None,
        )
    snap = transcript.build(conn, "biz_b")
    assert snap["dropped"] == 5 and len(snap["turns"]) == transcript.MAX_TURNS
    first = snap["turns"][0]
    assert first["text"].startswith("words 5 ") and len(first["text"]) == transcript.MAX_TEXT and first["truncated"]


def test_a_failure_is_an_error_and_the_caller_s_transaction_goes_on(conn, monkeypatch):
    conn.execute("INSERT INTO business (id, claim_state) VALUES ('biz_f', 'claimed')")

    def broken(conn, business_id):
        conn.execute("SELECT * FROM no_such_table")

    monkeypatch.setattr(transcript, "_consultations", broken)
    with conn.transaction():
        snap = transcript.build(conn, "biz_f")
        assert snap["error"] == "UndefinedTable" and "turns" not in snap
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1  # only the savepoint rolled back
