"""The support read in `answer` and the contact_support tool (spec 2026-09-30 §4.2, §4.3): a request for a person is not
an answer; any key bound to a business may ask; a draft gets the claim link; the ticket mail goes out after commit;
nothing while support is off; the owner's audit log says it in words."""

import json

import pytest

from agent import benchmark, crawl, keys, llm, models, support, tools_start
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture

WORDS = "I'd rather talk to a real person"


@pytest.fixture
def fake(monkeypatch):
    person = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=False,
        support="human",
        support_summary="Wants to talk to a person.",
        services=[],
    )
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={WORDS: person})
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    # the consultation's website (`consultation`) is crawled and read as the dental fixture: a crawl that read no page
    # would leave it unread (plan 2026-10-05-agent-a3p-5-1, ruling R2-a)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(tools_start.places, "search", lambda q, k, **kw: [])  # start's lookup finds no business
    return gen


def owned_key(conn) -> str:
    key = business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com'")
    return key


def consultation(client, conn, key) -> tuple[str, str]:
    """A consultation begun on words that name no website, and the website given (the fixture site, read: plan
    2026-10-05-agent-a3p-5-1): the website's words are read without the model, so a request for a person is looked for
    in the next question's. (The session id, the id of the question that waits.)"""
    benchmark.seed(conn)
    b = call_tool(client, "start", {"text": "dentist in Palo Alto, $2,000 a month"}, key=key)
    sid = data(b)["session_id"]
    site = {"session_id": sid, "answers": {"website": {"other": "https://brightsmile.example"}}, "idempotency_key": "w"}
    return sid, call_tool(client, "answer", site, key=key)["structuredContent"]["next"]["question"]["id"]


def test_asking_for_a_person_in_an_answer_opens_a_ticket_and_records_nothing(client, conn, fake, support_on):
    key = owned_key(conn)
    sid, qid = consultation(client, conn, key)
    res = call_tool(
        client, "answer", {"session_id": sid, "answers": {qid: {"other": WORDS}}, "idempotency_key": "a1"}, key=key
    )
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["support"]["status"] == "open" and env["data"]["recorded"] == {}
    assert env["say"].startswith("I've passed this to the Admiry team. They'll reply by e-mail to owner@example.com.")
    assert env["next"]["question"]["id"] == qid  # the question still waits
    assert qid not in conn.execute("SELECT answers FROM consult_session").fetchone()["answers"]
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (t["source"], t["reason"], t["summary"]) == ("mcp", "human", "Wants to talk to a person.")
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]  # after commit


def test_with_support_off_the_same_words_are_read_as_an_answer(client, conn, fake, resend):
    key = owned_key(conn)
    sid, qid = consultation(client, conn, key)
    res = call_tool(
        client, "answer", {"session_id": sid, "answers": {qid: {"other": WORDS}}, "idempotency_key": "a1"}, key=key
    )
    assert "support" not in res["structuredContent"]["data"]
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def draft_asks(client, conn, source: str, n: int) -> list[bool]:
    """n businesses' draft keys of `source` each ask `answer` for a person from the address "mail" (the one address the
    e-mail channel reaches every tool from); whether each ask was refused."""
    refused = []
    for i in range(n):
        conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'draft')", (f"biz_{i}",))
        label = "email" if source == "email" else "draft"
        key, _ = keys.mint(
            conn, business_id=f"biz_{i}", cls="draft", scopes=("read", "manage"), label=label, source=source
        )
        sid, qid = consultation(client, conn, key)
        res = call_tool(
            client,
            "answer",
            {"session_id": sid, "answers": {qid: {"other": WORDS}}, "idempotency_key": "a1"},
            key=key,
            extra_headers={"X-Forwarded-For": "mail"},
        )
        refused.append(res["isError"])
    return refused


def test_e_mail_asks_are_not_metered_per_address(client, conn, fake, support_on):
    """The e-mail channel has its own per-sender limits; its drafts' per-message keys are draft class and every call
    arrives from "mail", so counting the address would refuse the sixth e-mail ask of the day, on any business (§4.5)."""
    n = support.ANON_PER_IP_DAY + 1
    assert draft_asks(client, conn, "email", n) == [False] * n
    assert conn.execute("SELECT count(*) AS n FROM support_ticket WHERE source = 'email'").fetchone()["n"] == n


def test_other_drafts_are_still_metered_per_address(client, conn, fake, support_on):
    """The control: the same six asks from draft keys that are not e-mail's hit the address limit on the sixth."""
    n = support.ANON_PER_IP_DAY + 1
    assert draft_asks(client, conn, "key", n) == [False] * support.ANON_PER_IP_DAY + [True]


def test_contact_support_from_an_assistant(client, conn, fake, support_on):
    key = owned_key(conn)
    args = {
        "message": "My ads stopped, can someone look?",
        "summary": "Ads stopped.",
        "reason": "help",
        "context": "owner: my ads stopped\nassistant: let me check the status",
        "idempotency_key": "c1",
    }
    env = call_tool(client, "contact_support", args, key=key)["structuredContent"]
    assert env["data"]["support"]["status"] == "open" and "connect_url" not in env["data"]
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (t["source"], t["reason"], t["summary"]) == ("mcp", "help", "Ads stopped.")
    turns = t["snapshot"]["turns"]
    assert [x["who"] for x in turns[-2:]] == ["assistant", "owner"]
    assert turns[-1]["text"] == "My ads stopped, can someone look?"
    assert call_tool(client, "contact_support", args, key=key)["structuredContent"] == env  # a replay
    assert conn.execute("SELECT count(*) AS n FROM ticket_event WHERE kind = 'owner_words'").fetchone()["n"] == 1
    assert len(support_on.sent) == 2


def test_a_draft_key_gets_the_claim_link_and_no_mail(client, conn, fake, support_on):
    key = business_with_key(conn, cls="draft")
    args = {"message": "help", "summary": "", "reason": "human", "idempotency_key": "c1"}
    env = call_tool(client, "contact_support", args, key=key)["structuredContent"]
    assert env["data"]["support"]["status"] == "awaiting_contact" and "/connect?claim=" in env["data"]["connect_url"]
    # the claim link is a capability: `next` names the tool that makes a fresh one, never the URL itself
    assert env["next"]["kind"] == "actions" and [a["tool"] for a in env["next"]["actions"]] == ["connect_account"]
    assert "claim=" not in json.dumps(env["next"]) and support_on.sent == []
    row = conn.execute("SELECT result FROM agent_action WHERE tool = 'contact_support'").fetchone()
    assert "claim=" not in json.dumps(row["result"])  # nor does the audit row keep it: data.connect_url is scrubbed


@pytest.mark.parametrize(
    "scopes, claim_state",
    [
        (("read",), "claimed"),  # a read-only key on a business an operator claimed before anyone signed in
        (("read", "manage"), "claimed"),  # a manage key there: it is still no draft
        (("read",), "draft"),  # a draft business, but a key that cannot manage
    ],
)
def test_only_a_draft_s_manage_key_is_handed_a_claim_link(client, conn, fake, support_on, scopes, claim_state):
    """Whoever signs in through a claim link becomes the owner of a business that has none (claims.claim), and a business
    an operator claimed or verified has no owner_email either: `awaiting_contact` alone must not mint a link."""
    key = business_with_key(conn, scopes=scopes, claim_state=claim_state, cls="business")  # owner_email stays NULL
    args = {"message": "help", "summary": "", "reason": "human", "idempotency_key": "c1"}
    env = call_tool(client, "contact_support", args, key=key)["structuredContent"]
    assert env["data"]["support"]["status"] == "awaiting_contact"  # no address on file: the ticket waits, as before
    assert "connect_url" not in env["data"] and env["next"]["kind"] == "none"
    assert env["say"].startswith("I've passed this to the Admiry team.")  # the sentence is the same
    assert conn.execute("SELECT count(*) AS n FROM claim_link").fetchone()["n"] == 0


def test_contact_support_refusals(client, conn, fake, resend, monkeypatch):
    key = owned_key(conn)
    ask = {"message": "help", "summary": "", "reason": "human", "idempotency_key": "c1"}
    off = call_tool(client, "contact_support", ask, key=key)
    assert off["isError"] and "isn't available yet" in tool_text(off)
    monkeypatch.setenv("AGENT_SUPPORT", "on")
    bad = call_tool(client, "contact_support", {**ask, "reason": "angry", "idempotency_key": "c2"}, key=key)
    assert bad["isError"] and "reason must be one of" in tool_text(bad)
    empty = call_tool(client, "contact_support", {**ask, "message": " ", "idempotency_key": "c3"}, key=key)
    assert empty["isError"] and "message" in tool_text(empty)
    platform = business_with_key(conn, cls="platform", biz_id="biz_2")
    p = call_tool(client, "contact_support", {**ask, "idempotency_key": "c4"}, key=platform)
    assert p["isError"] and "not bound to a business" in tool_text(p)


def test_the_owner_s_audit_log_says_it_in_words(client, conn, fake, support_on):
    key = owned_key(conn)
    call_tool(
        client,
        "contact_support",
        {"message": "help", "summary": "", "reason": "human", "idempotency_key": "c1"},
        key=key,
    )
    entries = call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]["entries"]
    assert any(e["sentence"] == "Muse asked Admiry support for help" for e in entries)
