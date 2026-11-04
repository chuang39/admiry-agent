"""What an owner's assistant reads and asks of the Researcher (capability layer K4; founder ruling R-K4; plan D2, D3,
D8, D14): get_research, request_research and admiry://marketing/research -- nothing of a run before AGENT_ROLES is on
and the Critic delivered it, everything of it for a platform key; one ask a day, none for a draft business, and no ask
offered that request_research would refuse (pre-flight F5)."""

import json
import logging

import pytest

from agent import copy, tools_research
from agent.surface import told
from agent.tests import research_fixtures as rf
from agent.tests.conftest import RPC_HEADERS, call_tool, rpc, tool_text
from agent.tests.fixtures import business_with_key

NO_ASK = {"kind": "none"}


def _call(client, key, name, **args) -> dict:
    res = call_tool(client, name, args, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def _ask_with_note(client, key, note: str, idempotency_key: str = "n1") -> dict:
    """request_research over /mcp with `note` written into the JSON as escapes (json.dumps keeps ASCII): the way a
    client's JSON names a NUL ("\\u0000") or half of a character ("\\ud800"), which the test client's own encoder would
    refuse to write out. The tool's result."""
    arguments = {"note": note, "idempotency_key": idempotency_key}
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "request_research", "arguments": arguments},
    }
    sent = client.post("/mcp", headers={**RPC_HEADERS, "Authorization": f"Bearer {key}"}, content=json.dumps(call))
    assert sent.status_code == 200, sent.text
    return sent.json()["result"]


def _read(client, key) -> dict:
    uri = {"uri": told.RESEARCH_RESOURCE}
    return json.loads(rpc(client, "resources/read", uri, key=key)["result"]["contents"][0]["text"])


@pytest.fixture
def owner(conn):
    rf.seed(conn)
    return business_with_key(conn)


def test_the_owner_reads_delivered_research_with_its_sources(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.delivered(conn)
    out = _call(client, owner, "get_research")
    assert set(out["data"]) == set(tools_research.KEYS) and out["data"]["status"] == "ready"
    r = out["data"]["research"]
    assert [f["text"] for f in r["findings"]] == ["A lead from search ads costs dentists about $72.97 on average."]
    assert r["findings"][0]["sources"][0]["url"].startswith("https://localiq.com/")
    assert r["searches_to_aim_for"] == ["invisalign palo alto"] and "Rival" not in json.dumps(r)
    assert out["say"].startswith("Admiry researched your market on ")
    assert out["say"].endswith(": 1 finding, each with where it comes from.") and out["next"] == NO_ASK


def test_the_say_names_the_day_and_counts_the_findings(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    first = rf.STORED["findings"][0]
    second = {**first, "text": "Search ads cost dentists about $72.97 a lead in this trade."}
    rf.delivered(conn, brief_json={**rf.STORED, "findings": [first, second]})
    conn.execute("UPDATE research_run SET created_at = '2026-10-05T12:00:00+00:00'")
    out = _call(client, owner, "get_research")
    assert out["data"]["research"]["day"] == "2026-10-05"
    assert (
        out["say"] == "Admiry researched your market on Monday, October 5: 2 findings, each with where it comes from."
    )


def test_validate_keeps_research_for_admirys_team(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    rf.delivered(conn, mode="validate")
    out = _call(client, owner, "get_research")
    assert (out["data"]["research"], out["data"]["status"], out["say"]) == (None, "not_on", tools_research.NOT_ON)
    assert out["next"] == NO_ASK
    assert _read(client, owner)["research"] is None
    platform = business_with_key(conn, cls="platform")
    conn.execute("UPDATE api_key SET business_id = %s WHERE class = 'platform'", (rf.BIZ,))
    team = _call(client, platform, "get_research")["data"]["team"]
    assert team[0]["mode"] == "validate" and team[0]["brief_json"]["findings"][1]["dropped"] == "named"


def test_with_the_roles_off_get_research_says_it_is_not_on(client, conn, owner):
    rf.delivered(conn)  # a run is stored, and the switch is off: nothing of it is read
    out = _call(client, owner, "get_research")
    assert (out["data"]["research"], out["data"]["status"], out["say"]) == (None, "not_on", tools_research.NOT_ON)
    assert out["next"] == NO_ASK


def test_research_built_under_validate_stays_the_teams_after_the_switch(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.delivered(conn, mode="validate")
    out = _call(client, owner, "get_research")
    assert out["data"]["research"] is None
    # it finished today, so request_research would refuse (RECENT): get_research says so and offers no ask (F5)
    assert (out["data"]["status"], out["say"], out["next"]) == ("recent", tools_research.RECENT, NO_ASK)
    asked = _call(client, owner, "request_research", idempotency_key="r1")
    assert (asked["data"]["queued"], asked["data"]["research"], asked["say"]) == (False, None, tools_research.RECENT)


def test_research_the_critic_did_not_deliver_is_not_read(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.delivered(conn, dropped="revise")
    out = _call(client, owner, "get_research")
    assert out["data"]["research"] is None and out["data"]["status"] == "recent" and out["next"] == NO_ASK
    assert _read(client, owner)["research"] is None


def test_a_business_never_researched_is_offered_the_ask(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    out = _call(client, owner, "get_research")
    assert (out["data"]["research"], out["data"]["status"], out["say"]) == (None, "none", tools_research.NONE)
    assert out["next"] == {"kind": "actions", "actions": [{**tools_research.ASK}]}
    assert out["next"]["actions"][0]["tool"] == "request_research"


def test_research_the_team_built_a_day_ago_no_longer_stops_an_ask(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.delivered(conn, mode="validate", days_ago=2)
    out = _call(client, owner, "get_research")
    assert (out["data"]["research"], out["data"]["status"], out["say"]) == (None, "none", tools_research.NONE)
    assert out["next"]["actions"][0]["tool"] == "request_research"
    asked = _call(client, owner, "request_research", idempotency_key="r1")  # the ask that was offered goes through
    assert (asked["data"]["queued"], asked["say"]) == (True, tools_research.QUEUED)


def _wait(conn, trigger: str, status: str = "queued") -> None:
    rf.job(conn, trigger=trigger)
    if status != "queued":
        conn.execute("UPDATE agent_job SET status = %s WHERE role = 'researcher'", (status,))


def _unread(conn) -> None:
    """The business signed in and Admiry knows nothing of it: no knowledge row, as when a Google sign-in made the
    business before its owner said what it does."""
    conn.execute("DELETE FROM knowledge WHERE business_id = %s", (rf.BIZ,))


def _unread_with_a_run_today(conn) -> None:
    rf.delivered(conn, mode="validate")
    _unread(conn)


@pytest.mark.parametrize(
    ("setup", "status", "offered", "refused"),
    [
        pytest.param(lambda conn: None, "none", True, None, id="no research"),
        # whatever the run's mode or verdict: request_research refuses an ask within a day of a finished run
        pytest.param(
            lambda conn: rf.delivered(conn, mode="validate"), "recent", False, None, id="the team's run today"
        ),
        pytest.param(
            lambda conn: rf.delivered(conn, dropped="review"), "recent", False, None, id="a run not delivered"
        ),
        pytest.param(
            lambda conn: rf.delivered(conn, mode="validate", days_ago=2), "none", True, None, id="a run 2 days ago"
        ),
        pytest.param(lambda conn: rf.delivered(conn), "ready", False, None, id="delivered today"),
        pytest.param(lambda conn: rf.failed(conn), "none", True, None, id="a run that failed today"),
        pytest.param(lambda conn: _wait(conn, "owner"), "queued", False, None, id="an ask waiting"),
        pytest.param(lambda conn: _wait(conn, "owner", "running"), "running", False, None, id="an ask running"),
        pytest.param(lambda conn: _wait(conn, "operator"), "queued", False, None, id="the operator's job waiting"),
        pytest.param(
            lambda conn: _wait(conn, "operator", "running"), "running", False, None, id="the operator's job running"
        ),
        pytest.param(lambda conn: _wait(conn, "auto"), "queued", False, None, id="the sweep's job waiting"),
        # a signed-in business Admiry has not read: a run would skip it, so the ask is not taken, nor offered
        pytest.param(_unread, "none", False, tools_research.UNREAD, id="no knowledge on file"),
        pytest.param(
            _unread_with_a_run_today, "recent", False, tools_research.UNREAD, id="no knowledge on file, a run today"
        ),
    ],
)
def test_an_ask_is_offered_only_where_request_research_queues_it(
    client, conn, owner, monkeypatch, setup, status, offered, refused
):
    """Pre-flight F5: get_research never offers an ask that request_research would refuse."""
    monkeypatch.setenv("AGENT_ROLES", "on")
    setup(conn)
    waiting = conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"]
    out = _call(client, owner, "get_research")
    assert out["data"]["status"] == status and status in tools_research.STATUSES
    assert (out["next"]["kind"] == "actions") is offered
    if offered:
        asked = _call(client, owner, "request_research", idempotency_key="asked")
        assert asked["data"]["queued"] is True and asked["say"] == tools_research.QUEUED
    if refused:
        assert out["say"] == refused  # the words the ask is turned away with are the words get_research gives
        res = call_tool(client, "request_research", {"idempotency_key": "asked"}, key=owner)
        assert res["isError"] and refused in tool_text(res)
        assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == waiting


def test_a_business_admiry_has_not_read_is_told_what_to_do_first(client, conn, monkeypatch):
    """Review of Task 5: the ask was taken, "usually ready within the hour" said, and research-run then skipped the job
    for want of knowledge -- and get_research offered the ask again."""
    monkeypatch.setenv("AGENT_ROLES", "on")
    key = business_with_key(conn, claim_state="claimed")  # signed in, and nothing on file
    out = _call(client, key, "get_research")
    assert (out["data"]["research"], out["data"]["status"]) == (None, "none")
    assert (out["say"], out["next"]) == (tools_research.UNREAD, NO_ASK)
    res = call_tool(client, "request_research", {"idempotency_key": "r1"}, key=key)
    assert res["isError"] and tools_research.UNREAD in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_a_draft_business_is_told_to_sign_in_before_anything_else(client, conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    key = business_with_key(conn, claim_state="draft")  # not signed in, and nothing on file either
    out = _call(client, key, "get_research")
    assert (out["say"], out["next"]) == (tools_research.SIGN_IN, NO_ASK)
    res = call_tool(client, "request_research", {"idempotency_key": "r1"}, key=key)
    assert res["isError"] and tools_research.SIGN_IN in tool_text(res)


def test_request_research_queues_once_and_a_repeat_says_it_is_on_it(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    out = _call(
        client, owner, "request_research", topics=["seasonality"], note="When are we busiest?", idempotency_key="r1"
    )
    assert (out["data"]["queued"], out["say"]) == (True, tools_research.QUEUED)
    brief = conn.execute("SELECT brief FROM agent_job WHERE role = 'researcher'").fetchone()["brief"]
    assert (brief["trigger"], brief["topics"], brief["note"]) == ("owner", ["seasonality"], "When are we busiest?")
    again = _call(client, owner, "request_research", idempotency_key="r2")
    assert (again["data"]["queued"], again["data"]["status"], again["say"]) == (False, "queued", tools_research.WORKING)
    status = _call(client, owner, "get_research")
    assert (status["data"]["status"], status["say"], status["next"]) == ("queued", tools_research.WORKING, NO_ASK)


def test_an_ask_while_the_sweeps_job_waits_puts_the_owners_words_in_it(client, conn, owner, monkeypatch):
    """Task 4: research the sweep queued is not the owner's question researched: the ask takes the job over."""
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.job(conn, trigger="auto", topics=["competitors"], note=None)
    out = _call(client, owner, "request_research", note="Do we need evening hours?", idempotency_key="r1")
    assert (out["data"]["queued"], out["say"]) == (True, tools_research.QUEUED)
    brief = conn.execute("SELECT brief FROM agent_job WHERE role = 'researcher'").fetchone()["brief"]
    assert (brief["trigger"], brief["note"]) == ("owner", "Do we need evening hours?")
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 1


def test_the_same_ask_twice_is_one_ask(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    first = _call(client, owner, "request_research", idempotency_key="same")
    assert _call(client, owner, "request_research", idempotency_key="same") == first
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 1


def test_a_request_within_a_day_of_the_last_research_is_not_queued(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.delivered(conn)
    out = _call(client, owner, "request_research", idempotency_key="r1")
    assert (out["data"]["queued"], out["say"]) == (False, tools_research.RECENT)
    assert out["data"]["research"]["findings"]  # the latest research comes with the answer
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_validate_takes_the_ask_for_the_team(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    out = _call(client, owner, "request_research", idempotency_key="r1")
    assert (out["data"]["queued"], out["say"]) == (True, tools_research.QUEUED_TEAM)
    assert out["data"]["research"] is None
    again = _call(client, owner, "request_research", idempotency_key="r2")  # the team's research is on its way already
    assert (again["data"]["queued"], again["say"]) == (False, tools_research.QUEUED_TEAM)


def test_with_the_roles_off_nothing_is_asked(client, conn, owner):
    out = _call(client, owner, "request_research", idempotency_key="r1")
    assert (out["data"]["queued"], out["say"]) == (False, tools_research.NOT_ON)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_a_draft_business_cannot_ask(client, conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    key = business_with_key(conn, claim_state="draft")
    res = call_tool(client, "request_research", {"idempotency_key": "r1"}, key=key)
    assert res["isError"] and tools_research.SIGN_IN in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


@pytest.mark.parametrize("runs", [False, True])
def test_a_draft_business_is_told_to_sign_in_and_offered_no_ask(client, conn, monkeypatch, runs):
    """Pre-flight F5: request_research refuses a draft business, so get_research offers it no ask and says why --
    also while a run of the team's finished today (its own say, RECENT, would promise an ask tomorrow)."""
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.seed(conn, claim_state="draft")
    if runs:
        rf.delivered(conn, mode="validate")
    out = _call(client, business_with_key(conn), "get_research")
    assert out["data"]["research"] is None and out["data"]["status"] == ("recent" if runs else "none")
    assert (out["say"], out["next"]) == (tools_research.SIGN_IN, NO_ASK)


@pytest.mark.parametrize(
    ("args", "words"),
    [({"topics": ["weather"]}, tools_research.BAD_TOPICS), ({"note": "x" * 501}, tools_research.LONG_NOTE)],
)
def test_bad_topics_and_long_notes_are_refused(client, conn, owner, monkeypatch, args, words):
    monkeypatch.setenv("AGENT_ROLES", "on")
    res = call_tool(client, "request_research", {**args, "idempotency_key": "r1"}, key=owner)
    assert res["isError"] and words in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_topics_are_kept_in_order_without_repeats_and_a_blank_note_is_none(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    topics = ["seasonality", "competitors", "seasonality"]
    _call(client, owner, "request_research", topics=topics, note="  ", idempotency_key="r1")
    brief = conn.execute("SELECT brief FROM agent_job WHERE role = 'researcher'").fetchone()["brief"]
    assert (brief["topics"], brief["note"]) == (["seasonality", "competitors"], None)


def test_a_note_of_the_longest_length_is_taken_whole(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    note = "x" * tools_research.MAX_NOTE
    out = _call(client, owner, "request_research", note=f"  {note}  ", idempotency_key="r1")  # the padding is no part
    assert out["data"]["queued"] is True
    brief = conn.execute("SELECT brief FROM agent_job WHERE role = 'researcher'").fetchone()["brief"]
    assert brief["note"] == note


def _stored_note(conn):
    return conn.execute("SELECT brief FROM agent_job WHERE role = 'researcher'").fetchone()["brief"]["note"]


# the characters JSON can name and no text can hold: a NUL, and the first and the last of the halves of a character
NUL, FIRST_HALF, LAST_HALF = "\x00", chr(0xD800), chr(0xDFFF)
SAID = "When are we busiest?"


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        pytest.param(f"When are{NUL} we busiest?", SAID, id="a NUL in the middle"),
        pytest.param(f"When are{FIRST_HALF} we busiest?", SAID, id="the first half of a character"),
        pytest.param(f"When are{LAST_HALF} we busiest?", SAID, id="the last half of a character"),
        pytest.param(f"{NUL}  {SAID}{NUL}", SAID, id="NULs at the ends, what is left stripped"),
        pytest.param(
            ("x" + NUL) * tools_research.MAX_NOTE, "x" * tools_research.MAX_NOTE, id="the limit counts what is kept"
        ),
        pytest.param(NUL * 3, None, id="nothing but NULs"),
        pytest.param(NUL + LAST_HALF + FIRST_HALF + NUL, None, id="nothing but halves"),
    ],
)
def test_a_character_no_text_can_hold_is_taken_out_of_the_note(client, conn, owner, monkeypatch, caplog, sent, kept):
    """Review of Task 5: jsonb holds no NUL and no lone half of a character, so the job's brief failed to insert -- an
    internal error, nothing queued, and psycopg's error context in the server log held the start of the owner's note.
    The audit row refuses a half too (it takes out the NUL only), so the note is cleaned before run() records it."""
    monkeypatch.setenv("AGENT_ROLES", "on")
    res = _ask_with_note(client, owner, sent)
    assert not res["isError"], tool_text(res)
    data = res["structuredContent"]
    assert (data["data"]["queued"], data["say"]) == (True, tools_research.QUEUED)
    assert _stored_note(conn) == kept
    assert [
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    ] == []  # nothing failed, nothing logged


def test_a_note_too_long_is_still_too_long_with_the_nuls_taken_out(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    res = _ask_with_note(client, owner, ("x" + NUL) * (tools_research.MAX_NOTE + 1))
    assert res["isError"] and tools_research.LONG_NOTE in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 0


def test_the_same_note_with_and_without_a_nul_is_one_ask(client, conn, owner, monkeypatch):
    """The call's identity is of the note kept: a repeat under the same key, the NUL dropped, is the same ask."""
    monkeypatch.setenv("AGENT_ROLES", "on")
    first = _ask_with_note(client, owner, f"When are{NUL} we busiest?", "same")
    again = _ask_with_note(client, owner, SAID, "same")
    assert not first["isError"] and not again["isError"] and again["structuredContent"] == first["structuredContent"]
    assert conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"] == 1


def test_the_research_resource_is_the_tools_answer(client, conn, owner, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.delivered(conn)
    assert _read(client, owner) == _call(client, owner, "get_research")["data"]


def test_every_say_passes_owner_words():
    says = (
        tools_research.NOT_ON,
        tools_research.NONE,
        tools_research.WORKING,
        tools_research.READY,
        tools_research.QUEUED,
        tools_research.QUEUED_TEAM,
        tools_research.RECENT,
        tools_research.SIGN_IN,
        tools_research.UNREAD,
        tools_research.ASK["label"],
    )
    for s in says:
        assert copy.jargon(s) is None, s
