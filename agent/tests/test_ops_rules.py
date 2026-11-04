"""spec 2026-10-12 §2.5 (Part B phase 1): /ops/rules -- the proposed rules with their evidence (approve as warn, approve
as block, reject), the active rules (change the level, retire), add a rule, the last summaries; the gate as
/ops/tasks's, CSRF on every POST, an audit row in the admin's name for every decision; and the admin MCP's two tools."""

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import config, llm, local_rules, ops_mcp, ops_rules, refusal_summary
from agent.tests.conftest import JARGON
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_never_elsewhere import ELSEWHERE
from agent.tests.test_ops_mcp import _call
from agent.tests.test_refusal_rules import rule
from agent.tests.test_refusal_summary import DAY, NEXT, a_day
from agent.tests.test_web import sign_in

ADMIN = "founder@example.com"
PILL = 'id="help-pill"'


@pytest.fixture
def admin(client, google, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    google.email = ADMIN
    sign_in(client, "/ops/rules")
    return client


def _csrf(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def proposed(conn, value="deal", level="block") -> str:
    """A rule the summary proposed, with its evidence."""
    rule_id = rule(conn, value, level=level, status="proposed", why="a word Google reads as a price promise")
    conn.execute(
        "UPDATE refusal_rule SET proposed_by = 'summary:2026-10-11', topic = 'MISLEADING_CLAIMS', evidence = %s "
        "WHERE id = %s",
        (Jsonb({"refusal_ids": ["ar_1", "ar_2", "ar_3"], "count": 3, "businesses": 2, "note": "three lines"}), rule_id),
    )
    return rule_id


def _audit(conn, tool: str) -> list[dict]:
    return conn.execute(
        "SELECT actor, business_id, args_redacted, result FROM agent_action WHERE tool = %s ORDER BY id", (tool,)
    ).fetchall()


def _row(conn, rule_id: str) -> dict:
    return conn.execute(f"SELECT {local_rules.COLS} FROM refusal_rule WHERE id = %s", (rule_id,)).fetchone()


# ---------------------------------------------------------------- the module


@pytest.fixture
def model(monkeypatch):
    """The model faked with no proposals: a summary row with its counts and `model` proposed."""
    gen = FakeGenerator()
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    return gen


def test_page_view_lists_the_proposed_the_active_and_the_summaries(conn, model):
    a_day(conn)
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    block = proposed(conn)
    warn = rule(conn, "ellipsis", kind="pattern", level="warn")
    rule(conn, "old", status="retired")
    v = ops_rules.page_view(conn)
    assert [r["id"] for r in v["proposed"]] == [block] and v["proposed"][0]["evidence"]["businesses"] == 2
    assert [r["id"] for r in v["active"]] == [warn] and v["active"][0]["tell"] == local_rules.SHAPES["ellipsis"].tell
    assert [s["day"] for s in v["summaries"]] == ["2026-10-11"] and v["summaries"][0]["model"] == "proposed"
    assert [s["id"] for s in v["shapes"]] == list(local_rules.SHAPES)
    assert (v["kinds"], v["applies"], v["levels"]) == (local_rules.KINDS, local_rules.APPLIES, local_rules.LEVELS)
    assert ops_rules.rules(conn, "retired")[0]["value"] == "old"
    with pytest.raises(ToolError, match=ops_rules.BAD_STATUS):
        ops_rules.rules(conn, "nope")


def test_approve_as_warn_or_block_reject_level_and_retire_each_write_an_audit_row(conn):
    a, b, c = proposed(conn, "deal"), proposed(conn, "cheap"), proposed(conn, "best")
    out = ops_rules.decide(conn, a, admin=ADMIN, action="approve", level="warn", ip="198.51.100.9")
    assert (out["status"], out["level"], out["decided_by"]) == ("active", "warn", f"admin:{ADMIN}")
    assert out["decided_at"] is not None and out["why"] == "a word Google reads as a price promise"
    ops_rules.decide(conn, b, admin=ADMIN, action="approve", level="block", why="a price Google wants proven")
    assert (_row(conn, b)["level"], _row(conn, b)["why"]) == ("block", "a price Google wants proven")
    ops_rules.decide(conn, c, admin=ADMIN, action="reject")
    assert _row(conn, c)["status"] == "rejected"
    ops_rules.decide(conn, a, admin=ADMIN, action="level", level="block")
    assert _row(conn, a)["level"] == "block" and _row(conn, a)["status"] == "active"
    ops_rules.decide(conn, b, admin=ADMIN, action="retire")
    assert _row(conn, b)["status"] == "retired"
    rows = _audit(conn, "admin.rule_decide")  # audit.redact writes a None as "null"
    assert [(r["args_redacted"]["action"], r["args_redacted"]["level"], r["result"]["status"]) for r in rows] == [
        ("approve", "warn", "active"),
        ("approve", "block", "active"),
        ("reject", "null", "rejected"),
        ("level", "block", "active"),
        ("retire", "null", "retired"),
    ]
    assert all(r["actor"] == f"admin:{ADMIN}" and r["business_id"] is None for r in rows)
    assert [r["args_redacted"]["rule_id"] for r in rows] == [a, b, c, a, b]


def test_a_decision_from_the_wrong_status_is_refused_with_no_row(conn):
    """Review Focus 5."""
    a = proposed(conn, "deal")
    active = rule(conn, "cheap", level="warn")
    for rule_id, action, sentence in (
        (active, "approve", ops_rules.NOT_PROPOSED),
        (active, "reject", ops_rules.NOT_PROPOSED),
        (a, "retire", ops_rules.NOT_ACTIVE),
        (a, "level", ops_rules.NOT_ACTIVE),
        (a, "approve", ops_rules.NEEDS_LEVEL),
        (a, "nope", ops_rules.BAD_ACTION),
        ("rr_0000000000000000", "reject", ops_rules.NOT_FOUND),
    ):
        level = None if sentence == ops_rules.NEEDS_LEVEL else "warn"
        with pytest.raises(ToolError, match=sentence):
            ops_rules.decide(conn, rule_id, admin=ADMIN, action=action, level=level)
    ops_rules.decide(conn, a, admin=ADMIN, action="approve", level="warn")
    with pytest.raises(ToolError, match=ops_rules.NOT_PROPOSED):  # twice
        ops_rules.decide(conn, a, admin=ADMIN, action="approve", level="warn")
    assert (_row(conn, a)["status"], _row(conn, active)["status"]) == ("active", "active")
    assert len(_audit(conn, "admin.rule_decide")) == 1


def test_a_level_action_to_the_rules_own_level_is_refused_with_no_row(conn):
    active = rule(conn, "cheap", level="warn")
    with pytest.raises(ToolError, match=ops_rules.NOOP_LEVEL):
        ops_rules.decide(conn, active, admin=ADMIN, action="level", level="warn")
    assert _row(conn, active)["level"] == "warn"
    assert _audit(conn, "admin.rule_decide") == []


def test_why_is_written_on_approve_and_level_never_on_reject_or_retire(conn):
    a, b = proposed(conn, "deal"), proposed(conn, "cheap")
    original_why = _row(conn, a)["why"]
    ops_rules.decide(conn, a, admin=ADMIN, action="reject", why="a different sentence a reject never writes")
    assert _row(conn, a)["why"] == original_why  # unchanged: reject never takes a why
    ops_rules.decide(conn, b, admin=ADMIN, action="approve", level="warn", why="a claim Google wants proven")
    assert _row(conn, b)["why"] == "a claim Google wants proven"
    ops_rules.decide(conn, b, admin=ADMIN, action="retire", why="the owner stopped running it")
    assert _row(conn, b)["why"] == "a claim Google wants proven"  # retire never takes a why
    active = rule(conn, "best", level="warn")
    ops_rules.decide(conn, active, admin=ADMIN, action="level", level="block", why="Google blocks it outright")
    assert _row(conn, active)["why"] == "Google blocks it outright"


def test_an_admins_own_rule_is_active_at_once_and_its_words_are_checked(conn):
    out = ops_rules.add(
        conn,
        admin=ADMIN,
        kind="word",
        value="  Best  Deal ",
        applies_to="headline",
        level="warn",
        why="a claim Google wants proven",
        topic="misleading_claims",
    )
    assert (out["status"], out["value"], out["topic"], out["proposed_by"]) == (
        "active",
        "best deal",
        "MISLEADING_CLAIMS",
        f"admin:{ADMIN}",
    )
    assert out["decided_by"] == f"admin:{ADMIN}" and out["evidence"] == {}
    (row,) = _audit(conn, "admin.rule_add")
    assert row["args_redacted"] == {"kind": "word", "applies_to": "headline", "value": "best deal", "level": "warn"}
    assert row["result"] == {"rule_id": out["id"]}
    for kw, sentence in (
        ({"value": "the"}, local_rules.STOP_WORD),
        ({"kind": "pattern", "value": "[!?]{2,}"}, "A pattern rule is one of the shapes"),
        ({"applies_to": "ad"}, local_rules.BAD_APPLIES),
        ({"level": "drop"}, local_rules.BAD_LEVEL),
        ({"why": " "}, ops_rules.NEEDS_WHY),
        ({"why": "a keyword Google refuses"}, ops_rules.WHY_JARGON),
        ({"value": "best deal"}, ops_rules.EXISTS),
    ):
        args = {"kind": "word", "value": "cheap", "applies_to": "headline", "level": "warn", "why": "a plain why"}
        args.update(kw)
        with pytest.raises(ToolError, match=sentence):
            ops_rules.add(conn, admin=ADMIN, **args)
    assert (
        len(_audit(conn, "admin.rule_add")) == 1
        and conn.execute("SELECT count(*) AS n FROM refusal_rule").fetchone()["n"] == 1
    )


def test_an_admins_typed_topic_is_capped_like_other_free_text(conn):
    out = ops_rules.add(
        conn,
        admin=ADMIN,
        kind="word",
        value="Guaranteed",
        applies_to="headline",
        level="warn",
        why="a claim Google wants proven",
        topic="x" * 200,
    )
    assert out["topic"] == "X" * ops_rules.TOPIC_MAX


def test_every_sentence_of_the_module_is_plain(conn):
    for text in (
        ops_rules.NOT_FOUND,
        ops_rules.BAD_ACTION,
        ops_rules.NEEDS_LEVEL,
        ops_rules.NOT_PROPOSED,
        ops_rules.NOT_ACTIVE,
        ops_rules.NEEDS_WHY,
        ops_rules.WHY_JARGON,
        ops_rules.EXISTS,
        ops_rules.BAD_STATUS,
    ):
        assert not ELSEWHERE.search(text), text


# ---------------------------------------------------------------- the page


def test_the_page_gate_is_ops_tasks_gate(client, conn, google, monkeypatch):
    assert client.get("/ops/rules").status_code == 404
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    page = client.get("/ops/rules")
    assert page.status_code == 200 and "/connect/google/start?next=/ops/rules" in page.text and PILL not in page.text
    assert client.post("/ops/rules", data={"action": "reject", "rule_id": "rr_0000000000000000"}).status_code == 404
    sign_in(client, "/connect")  # owner@example.com: signed in, not listed
    assert client.get("/ops/rules").status_code == 404
    google.email = ADMIN
    sign_in(client, "/ops/rules")
    assert client.get("/ops/rules").status_code == 200


def test_the_page_shows_decides_adds_and_refuses_in_a_sentence(admin, conn, model):
    a_day(conn)
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    block = proposed(conn, "deal")
    page = admin.get("/ops/rules")
    assert page.status_code == 200 and block in page.text and "three lines" in page.text and PILL not in page.text
    assert "2026-10-11" in page.text and "MISLEADING_CLAIMS" in page.text  # the admin reads Google's topic
    assert not JARGON.search(page.text.replace("keyword", ""))  # the template's own words; a row's value is data
    csrf = _csrf(page.text)
    assert admin.post("/ops/rules", data={"action": "reject", "rule_id": block}).status_code == 403  # no CSRF
    assert admin.post("/ops/rules", data={"action": "nope", "rule_id": block, "csrf": csrf}).status_code == 400
    assert admin.post("/ops/rules", data={"action": "reject", "rule_id": "nope", "csrf": csrf}).status_code == 404
    r = admin.post(
        "/ops/rules",
        data={"action": "approve", "rule_id": block, "level": "block", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/ops/rules?done=approve"
    assert _row(conn, block)["status"] == "active" and "Done." in admin.get("/ops/rules?done=approve").text
    again = admin.post("/ops/rules", data={"action": "approve", "rule_id": block, "level": "warn", "csrf": csrf})
    assert again.status_code == 409 and ops_rules.NOT_PROPOSED in again.text
    r = admin.post(
        "/ops/rules",
        data={
            "action": "add",
            "kind": "pattern",
            "value": "ellipsis",
            "applies_to": "any",
            "level": "warn",
            "why": "a run of dots",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = admin.get("/ops/rules")
    assert local_rules.SHAPES["ellipsis"].tell in page.text and "Retire" in page.text
    bad = admin.post(
        "/ops/rules",
        data={
            "action": "add",
            "kind": "word",
            "value": "the",
            "applies_to": "any",
            "level": "warn",
            "why": "x",
            "csrf": csrf,
        },
    )
    assert bad.status_code == 409 and local_rules.STOP_WORD in bad.text
    assert len(_audit(conn, "admin.rule_decide")) == 1 and len(_audit(conn, "admin.rule_add")) == 1


def test_the_nav_names_rules_on_every_ops_page(admin, conn):
    for path in ("/ops/rules", "/ops/tasks", "/ops/review"):
        page = admin.get(path)
        assert page.status_code == 200 and 'href="/ops/rules"' in page.text, path


# ---------------------------------------------------------------- the admin MCP


def test_the_two_tools_list_and_decide_in_the_admins_name(conn, model, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    a_day(conn)
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    block = proposed(conn, "deal")
    server = ops_mcp.build(ADMIN)
    listed = _call(server, "rules_list", {})
    assert not listed.is_error and [r["id"] for r in listed.data["rules"]] == [block]
    assert listed.data["status"] == "proposed" and [s["day"] for s in listed.data["summaries"]] == ["2026-10-11"]
    assert listed.data["rules"][0]["evidence"]["count"] == 3
    decided = _call(server, "rule_decide", {"rule_id": block, "action": "approve", "level": "warn"})
    assert not decided.is_error and decided.data["status"] == "active" and decided.data["level"] == "warn"
    assert _audit(conn, "admin.rule_decide")[0]["actor"] == f"admin:{ADMIN}"
    assert _call(server, "rules_list", {"status": "active"}).data["rules"][0]["id"] == block
    assert _call(server, "rules_list", {"status": "nope"}).is_error
    refused = _call(server, "rule_decide", {"rule_id": block, "action": "approve", "level": "warn"})
    assert refused.is_error and ops_rules.NOT_PROPOSED in refused.content[0].text
    missing = _call(server, "rule_decide", {"rule_id": "rr_0000000000000000", "action": "reject"})
    assert missing.is_error and ops_rules.NOT_FOUND in missing.content[0].text
