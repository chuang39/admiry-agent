"""The harness's audit row (spec 2026-10-10 K6 slice 1 §8, D7): a web.turn counts toward the day's model calls only
when it says so, and the transcript reads it."""

import pytest
from fastmcp.exceptions import ToolError

from agent import audit, auth, business, transcript


def _row(conn, biz, tool, result, ok=True):
    audit.record(
        conn,
        tool=tool,
        args={"text": "x"},
        result=result,
        ok=ok,
        business_id=biz,
        actor="key:k",
        actor_class="draft",
        api_key_id=None,
        ip=None,
    )


def test_a_counted_turn_spends_one_of_the_days_calls_and_an_uncounted_one_does_not(conn):
    biz = business.create(conn)
    for _ in range(auth.LLM_CALLS_PER_DAY_DRAFT - 1):
        _row(conn, biz, "web.turn", {"intent": "ask", "counted": True, "fast": False, "text": "x"})
    _row(conn, biz, "web.turn", {"intent": "start_over", "counted": False, "fast": True, "text": "restart"})
    _row(
        conn, biz, "web.turn", {"intent": "answer", "counted": False, "fast": False, "dispatch": "answer", "text": "x"}
    )
    auth.check_llm_cap_for(conn, biz, draft=True)  # nine counted: room for one more
    _row(conn, biz, "web.turn", {"intent": "ask", "counted": True, "fast": False, "text": "x"})
    with pytest.raises(ToolError):
        auth.check_llm_cap_for(conn, biz, draft=True)


def test_a_failed_turn_counts_as_web_say_does_and_a_denied_one_never(conn):
    biz = business.create(conn)
    for _ in range(auth.LLM_CALLS_PER_DAY_DRAFT):
        _row(conn, biz, "web.turn", {"error": "busy", "counted": True, "fast": False, "text": "x"}, ok=False)
    with pytest.raises(ToolError):
        auth.check_llm_cap_for(conn, biz, draft=True)


def test_the_transcript_shows_a_turns_words_and_admirys_reply_but_not_an_answers_turn(conn):
    biz = business.create(conn)
    _row(
        conn,
        biz,
        "web.turn",
        {"intent": "ask", "dispatch": "say", "say": "That comes with the plan.", "text": "how much will it cost?"},
    )
    _row(conn, biz, "web.turn", {"intent": "answer", "dispatch": "answer", "say": None, "text": "bookings mostly"})
    _row(
        conn,
        biz,
        "web.turn",
        {"intent": "support", "dispatch": "say", "say": "Noted.", "ticket_id": "t_1", "text": "a person please"},
    )
    turns = [(t["who"], t["text"]) for t in transcript.build(conn, biz)["turns"]]
    assert ("owner", "how much will it cost?") in turns and ("admiry", "That comes with the plan.") in turns
    assert turns.index(("owner", "how much will it cost?")) < turns.index(("admiry", "That comes with the plan."))
    assert ("owner", "bookings mostly") not in turns  # the consultation's own turn holds an answer's words
    assert ("owner", "a person please") not in turns  # the ticket's turn holds them
