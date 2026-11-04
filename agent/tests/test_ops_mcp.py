"""L1 Task 12 (spec 2026-10-10 §8): the admin MCP -- the review queue's tools over stdio for a listed admin, published
nowhere; it refuses to start for anyone else, and its stdout carries the protocol alone."""

import asyncio
import os
import sys

import fastmcp
import pytest
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from agent import ads_run, launch_review, ops_mcp, ops_review, ops_tasks, plans, support_mail
from agent.tests.test_ads_run import BIZ, seed
from agent.tests.test_review_launch import draft_sections
from agent.tests.test_support import biz, open_

ADMIN = "founder@example.com"
TOOLS = {
    "review_queue",
    "review_get",
    "review_approve",
    "review_return",
    "review_reject",
    "review_run",
    "ticket_queue",
    "ticket_get",
    "ticket_reply",
    "ticket_close",
    "rules_list",
    "rule_decide",
}


def _call(server, name: str, args: dict | None = None):
    async def go():
        async with fastmcp.Client(server) as c:
            return await c.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(go())


@pytest.fixture
def review_id(conn, monkeypatch) -> str:
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    seed(conn, status="draft", secs=draft_sections())
    return launch_review.submit(conn, BIZ, plans.latest(conn, BIZ)["id"], approval={"kind": "web"})["review_id"]


def test_the_tools_are_the_queues_and_nothing_else(review_id):
    async def names():
        async with fastmcp.Client(ops_mcp.build(ADMIN)) as c:
            return {t.name for t in await c.list_tools()}

    assert asyncio.run(names()) == TOOLS


def test_the_queue_and_a_return_in_the_admins_name(conn, review_id):
    server = ops_mcp.build(ADMIN)
    queue = _call(server, "review_queue")
    assert not queue.is_error and queue.data["open"][0]["id"] == review_id
    assert _call(server, "review_return", {"review_id": review_id, "note": " "}).is_error  # a note is needed
    back = _call(server, "review_return", {"review_id": review_id, "note": "Add an end date"})
    assert not back.is_error and launch_review.get(conn, review_id)["decided_by"] == f"admin:{ADMIN}"


def test_approve_launches(conn, review_id, monkeypatch):
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **k: {"channels": {}, "say": ""})
    out = _call(ops_mcp.build(ADMIN), "review_approve", {"review_id": review_id})
    assert not out.is_error and launch_review.get(conn, review_id)["state"] == "passed"


def test_it_refuses_to_start_for_an_address_not_listed(review_id, capsys):
    assert ops_mcp.main(["--as", "owner@example.com"]) == 2
    assert ops_mcp.NOT_LISTED in capsys.readouterr().err


def test_over_stdio_the_protocol_alone_is_on_stdout(review_id):
    """The command an admin's client runs (python -m agent.ops_mcp --as …): a session starts and lists the tools --
    nothing else ever written to stdout would break it."""
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "agent.ops_mcp", "--as", ADMIN],
        env={**os.environ, "AGENT_ADMIN_EMAILS": ADMIN},
        cwd=str(os.getcwd()),
    )

    async def names():
        async with fastmcp.Client(transport) as c:
            return {t.name for t in await c.list_tools()}

    assert asyncio.run(names()) == TOOLS


# ---------------------------------------------------------------- not found, nothing left to run, and R24 again


def test_review_get_on_an_unknown_id_answers_not_found(conn):
    out = _call(ops_mcp.build(ADMIN), "review_get", {"review_id": "lr_0000000000000000"})
    assert out.is_error and ops_review.NOT_FOUND in out.content[0].text


def test_review_run_with_no_job_waiting_answers_nothing_waits(conn, review_id):
    """decide it first (the owner's own version no longer waits for the Reviewer), then run it once (the stale job
    just skips: the review is already decided, so it finishes) -- a review whose job is done. Calling it again now
    finds no job at all."""
    from agent.roles import reviewer

    server = ops_mcp.build(ADMIN)
    assert not _call(server, "review_reject", {"review_id": review_id, "note": "Not a fit for Admiry"}).is_error
    first = _call(server, "review_run", {"review_id": review_id})
    assert not first.is_error
    second = _call(server, "review_run", {"review_id": review_id})
    assert second.is_error and reviewer.NOTHING_WAITS in second.content[0].text


def test_review_approve_refused_holds_and_launches_nothing(conn, review_id, monkeypatch):
    """R24 through the MCP: the approval's own check (proposals.apply_approve_plan) can refuse an admin's Approve --
    held for a person, with that sentence, never reported as launched."""
    from agent import proposals
    from agent.roles import reviewer

    def refuse(conn, biz, payload):
        raise ToolError("refused")

    monkeypatch.setattr(proposals, "apply_approve_plan", refuse)
    launched = []
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **k: launched.append(biz) or {"channels": {}, "say": ""})
    out = _call(ops_mcp.build(ADMIN), "review_approve", {"review_id": review_id})
    assert out.is_error and reviewer.APPROVAL_REFUSED.format(sentence="refused") in out.content[0].text
    held = launch_review.get(conn, review_id)
    assert held["state"] == "held" and held["hold_reason"] == reviewer.APPROVAL_REFUSED.format(sentence="refused")
    assert launched == []


# ---------------------------------------------------------------- spec 2026-10-12 §6: the four ticket tools


def test_the_ticket_tools_read_reply_and_close_in_the_admins_name(conn, support_on, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    biz(conn)
    report = {"page": "/", "signed_in": True, "browser": "x", "plan": None, "turns": []}
    tid = open_(conn, reason="help", report=report)["ticket_id"]
    support_mail.flush(tid)  # the acknowledgement and the notice; what follows is the reply alone
    server = ops_mcp.build(ADMIN)
    queue = _call(server, "ticket_queue", {"tab": "open"})
    assert not queue.is_error and [r["id"] for r in queue.data["rows"]] == [tid]
    assert queue.data["counts"]["open"] == 1 and queue.data["rows"][0]["origin"] == "Help button · /"
    got = _call(server, "ticket_get", {"ticket_id": tid})
    assert not got.is_error and got.data["contact"] == {"email": "owner@example.com", "verified": True}
    assert [e["kind"] for e in got.data["events"]] == ["created", "owner_words", "ack", "notice"]
    assert got.data["events"][1]["data"]["help"] == report
    assert _call(server, "ticket_reply", {"ticket_id": tid, "text": " "}).is_error  # the reply is empty
    sent = _call(server, "ticket_reply", {"ticket_id": tid, "text": "Yes, that is right."})
    assert not sent.is_error and sent.data["mail"] == {"sent": 1, "failed": 0}
    assert support_on.sent[-1]["to"] == ["owner@example.com"] and "Yes, that is right." in support_on.sent[-1]["text"]
    assert conn.execute("SELECT actor FROM agent_action WHERE tool = 'admin.ticket_reply'").fetchone() == {
        "actor": f"admin:{ADMIN}"
    }
    closed = _call(server, "ticket_close", {"ticket_id": tid, "resolution": "resolved"})
    assert not closed.is_error and closed.data["status"] == "closed"
    assert conn.execute("SELECT status, resolution FROM support_ticket").fetchone() == {
        "status": "closed",
        "resolution": "resolved",
    }
    missing = _call(server, "ticket_get", {"ticket_id": "tk_0000000000000000"})
    assert missing.is_error and ops_tasks.NOT_FOUND in missing.content[0].text
    assert _call(server, "ticket_queue", {"tab": "nope"}).is_error
