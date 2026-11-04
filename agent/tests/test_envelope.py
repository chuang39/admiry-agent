import pytest

from agent import envelope
from agent.mcp_app import REGISTRY
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key


def test_make_and_helpers_have_the_spec_shape():
    q = envelope.question(
        id="objective",
        index=1,
        count=3,
        prompt="What do you want most?",
        input="single",
        options=[{"id": "bookings", "label": "New patient bookings", "sublabel": "Booked online or by phone"}],
    )
    assert q == {
        "kind": "question",
        "question": {
            "id": "objective",
            "index": 1,
            "count": 3,
            "prompt": "What do you want most?",
            "input": "single",
            "options": [{"id": "bookings", "label": "New patient bookings", "sublabel": "Booked online or by phone"}],
            "other": {"allowed": True, "placeholder": "Something else…"},
        },
    }
    a = envelope.actions({"id": "x", "label": "Do x", "tool": "recommend", "primary": True})
    assert a["kind"] == "actions" and a["actions"][0]["primary"] is True
    assert envelope.link("http://t/plan/1", "Review") == {
        "kind": "link",
        "link": {"url": "http://t/plan/1", "label": "Review"},
    }
    e = envelope.make({"a": 1}, say="Done.", next=a, notes="present as buttons")
    assert e == {"data": {"a": 1}, "say": "Done.", "next": a, "assistant_notes": "present as buttons"}
    assert envelope.is_envelope(e) and not envelope.is_envelope({"a": 1})


def test_actions_are_capped_at_seven_and_say_must_be_text():
    """Ruling R2-a: the four ways to put Admiry's code on the owner's site (A3p-5 batch 2, Q6) come before up to three
    launch actions, so `next` lists up to seven; an action's arguments travel with it."""
    assert envelope.MAX_ACTIONS == 7
    seven = envelope.actions(*[{"id": str(i), "label": "l", "tool": "whoami"} for i in range(7)])
    assert len(seven["actions"]) == 7
    with pytest.raises(ValueError):
        envelope.actions(*[{"id": str(i), "label": "l", "tool": "whoami"} for i in range(8)])
    with_args = envelope.actions({"id": "x", "label": "Do x", "tool": "get_landing_page", "args": {"view": "kit"}})
    assert with_args["actions"] == [{"id": "x", "label": "Do x", "tool": "get_landing_page", "args": {"view": "kit"}}]
    assert "args" not in envelope.actions({"id": "y", "label": "Do y", "tool": "whoami"})["actions"][0]
    with pytest.raises(ValueError):
        envelope.make({}, say="")


def test_every_registered_tool_has_a_say():
    for meta in REGISTRY:
        assert meta.name in envelope.SAY or meta.name in envelope.EXPLICIT, meta.name


def test_wrap_writes_say_from_the_table_and_passes_envelopes_through():
    plain = envelope.wrap("whoami", {"business": {"id": "biz_1", "claim_state": "draft"}, "key": {"class": "draft"}})
    assert plain["data"]["business"]["id"] == "biz_1" and plain["next"] == envelope.NONE
    assert "draft" in plain["say"]
    made = envelope.make({"x": 1}, say="Hi.")
    assert envelope.wrap("whoami", made) is made
    with pytest.raises(KeyError):
        envelope.wrap("no_such_tool", {})


def _connection_status(status: str, account_public: str | None = None, mode: str = "own_account") -> dict:
    """The shape get_connection_status returns (agent/tools_channels.py, M0 Task 6): one entry per channel, its
    status the account row's (Google Search's mirrors the link: manual reads active, cancelled inactive)."""
    google = {"channel": "google_search", "label": "Google Search", "mode": mode, "status": status}
    return {
        "channels": [
            {**google, "account_public": account_public, "checked_at": None, "next_step": None},
            {"channel": "newsbreak", "label": "NewsBreak", "mode": "managed", "status": "none"},
        ],
        "agreement_accepted": True,
        "verified": False,
        "connect_url": "http://testserver/connect",
    }


def test_status_say_is_true_for_every_link_status():
    say = envelope.SAY["get_connection_status"]
    cid = "123-456-7890"
    cases = {
        "none": (_connection_status("none"), "not connected yet"),
        "active": (_connection_status("active", cid), "is connected"),
        "pending": (_connection_status("pending", cid), "waiting for you to accept"),
        "refused": (_connection_status("refused", cid), "refused the invitation"),
        "inactive": (_connection_status("inactive", cid), "is inactive or was withdrawn"),
        "needs_attention": (_connection_status("needs_attention", cid), "needs attention"),
        "managed": (_connection_status("none", mode="managed"), "run by admiry from your balance"),
    }
    for status, (d, expect) in cases.items():
        text = say(d)
        google, newsbreak = text.split(". ", 1)
        assert expect in google.lower() and google.startswith("Google Search"), (status, text)
        assert newsbreak == "NewsBreak is run by Admiry from your balance.", (status, text)
        assert not JARGON.search(text) and not ACCOUNT_JARGON.search(text), (status, text)
    # every real status but "none" is a link attempt that happened: it must never read as "never connected"
    for status, (d, _) in cases.items():
        if status != "none":
            assert "not connected yet" not in say(d).lower(), status
    assert say({"channels": []}) == "No channel is set up yet."


def test_tools_answer_in_the_envelope_and_replays_return_it(client, conn):
    key = business_with_key(conn)
    res = call_tool(client, "whoami", {}, key=key)
    assert not res["isError"]
    body = res["structuredContent"]
    assert set(body) >= {"data", "say", "next"} and body["next"] == {"kind": "none"}
    assert data(res)["business"]["id"] == "biz_1" and not JARGON.search(body["say"])
    first = call_tool(client, "set_goal", {"monthly_budget_usd": 900, "idempotency_key": "g1"}, key=key)
    again = call_tool(client, "set_goal", {"monthly_budget_usd": 900, "idempotency_key": "g1"}, key=key)
    assert again["structuredContent"] == first["structuredContent"]  # the stored envelope, replayed
    row = conn.execute("SELECT result FROM agent_action WHERE tool = 'set_goal' ORDER BY id LIMIT 1").fetchone()
    assert set(row["result"]) >= {"data", "say", "next"}


def test_every_tool_result_in_a_walk_through_is_a_clean_envelope(client, conn, monkeypatch):
    from agent import benchmark, crawl, llm, tools_start
    from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture

    benchmark.seed(conn)
    monkeypatch.setattr(
        llm, "generator", lambda settings=None: FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    )
    monkeypatch.setattr(tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="x")])
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    results = []
    key = business_with_key(conn, claim_state="claimed")
    b = call_tool(
        client,
        "start",
        {
            "text": "dentist in Palo Alto, $2,000 a month",
            "known_facts": {
                "objective": "bookings",
                "customer_value_usd": 2500,
                "radius_miles": 5,
            },
        },
        key=key,
    )
    results.append(b)
    sid = b["structuredContent"]["data"]["session_id"]
    assert b["structuredContent"]["next"]["kind"] == "question"  # the website and the service are still open
    site = {"website": {"other": "https://brightsmile.example"}}  # the words name no website: it is asked first
    for name, args in (
        ("answer", {"session_id": sid, "answers": site, "idempotency_key": "w"}),
        ("answer", {"session_id": sid, "answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "a"}),
        ("whoami", {}),
        ("recommend", {"session_id": sid, "idempotency_key": "r"}),
        ("get_plan", {}),
        ("get_business", {}),
        ("list_proposals", {}),
        ("get_audit_log", {}),
        ("get_connection_status", {}),
    ):
        results.append(call_tool(client, name, args, key=key))
    for res in results:
        assert not res["isError"], tool_text(res)
        env = res["structuredContent"]
        assert (
            isinstance(env["data"], dict)
            and env["say"].strip()
            and env["next"]["kind"] in ("none", "question", "actions", "link")
        )
        assert env["say"].count("\n\n") <= 2 and not JARGON.search(env["say"])
        if env["next"]["kind"] == "actions":
            assert len(env["next"]["actions"]) <= envelope.MAX_ACTIONS  # ruling R2-a: seven
        if env["next"]["kind"] == "question":  # say stands on its own: it carries the question
            assert env["next"]["question"]["prompt"] in env["say"], env["say"]
