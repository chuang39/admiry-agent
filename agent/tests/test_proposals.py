import pytest
from fastmcp.exceptions import ToolError

from agent import ads_mode, agreements, benchmark, business, crawl, landing, llm, ops, proposals, tools_start
from agent.channels.google_search import adapter as google
from agent.tests.conftest import TEST_DB_URL, call_tool, data, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture
from agent.tests.test_ads_run import seed


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(
        llm, "generator", lambda settings=None: FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    )


def planned(client, conn, claim_state="verified"):
    """A described business with a goal and a draft plan; returns (key, plan_id)."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state=claim_state)
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)
    plan = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    return key, plan["plan_id"]


def proposed(client, conn):
    key, plan_id = planned(client, conn)
    res = call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)
    assert not res["isError"], tool_text(res)
    return key, plan_id, res["structuredContent"]["data"]


def test_approve_plan_needs_a_verified_business(client, conn, fake):
    key, plan_id = planned(client, conn, claim_state="draft")
    res = call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"] == {
        "status": "needs_verification",
        "verify_url": "http://testserver/connect",
    }
    assert conn.execute("SELECT count(*) AS c FROM proposal").fetchone()["c"] == 0


def test_approve_plan_returns_a_spend_proposal(client, conn, fake):
    _key, plan_id, prop = proposed(client, conn)
    assert prop["status"] == "proposed" and prop["class"] == "spend" and prop["needs_code"] is True
    assert prop["approval_url"] == f"http://testserver/plan/{plan_id}"
    assert prop["what_changes"][2] == "Google Search: $1,300/mo · Facebook & Instagram: $700/mo" and prop[
        "summary"
    ].startswith("Launch plan v1")
    row = conn.execute(
        "SELECT kind, payload->>'plan_id' AS plan_id, expires_at > now() + interval '6 days' AS week FROM proposal"
    ).fetchone()
    assert (row["kind"], row["plan_id"], row["week"]) == ("approve_plan", plan_id, True)


def test_confirm_without_or_with_a_wrong_code_is_refused_and_audited(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c1"}, key=key)
    assert res["isError"] and "approval code" in tool_text(res)
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": "00-000", "idempotency_key": "c2"},
        key=key,
    )
    assert res["isError"] and "not valid" in tool_text(res)
    rows = conn.execute(
        "SELECT ok, args_redacted FROM agent_action WHERE tool = 'confirm_proposal' ORDER BY id"
    ).fetchall()
    assert [r["ok"] for r in rows] == [False, False] and rows[1]["args_redacted"]["approval_code"] == "***"
    assert conn.execute("SELECT status FROM plan").fetchone()["status"] == "draft"


def test_the_operator_code_confirms_once_and_approves_the_plan(client, conn, fake, capsys, monkeypatch):
    key, _plan_id, prop = proposed(client, conn)
    monkeypatch.setenv("AGENT_DATABASE_URL", TEST_DB_URL)
    assert ops.main(["issue-code", prop["proposal_id"]]) == 0
    code = capsys.readouterr().out.strip()
    assert len(code) == 6 and code[2] == "-"
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    body = res["structuredContent"]["data"]
    assert body["status"] == "confirmed" and body["plan_status"] == "approved"
    assert body["execution"] == {  # the ads switch is off here (A3 P5): nothing is queued for Google Search
        "presence": "queued_for_operator",
        "discovery": "queued_for_operator",
        "ads": "off",
        "creative": "queued_for_operator",
        "conversion": "queued_for_operator",
        "optimization": "on",
        "google_search": "off",  # M0: one entry per funded channel ("ads" mirrors it for one release)
    }
    assert conn.execute("SELECT status, json->'execution'->>'ads' AS ads FROM plan").fetchone() == {
        "status": "approved",
        "ads": "off",
    }
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0
    assert conn.execute("SELECT used_at IS NOT NULL AS used FROM approval_code").fetchone()["used"] is True
    audit = conn.execute(
        "SELECT approval_kind, approval_ref FROM agent_action WHERE tool = 'confirm_proposal' AND ok"
    ).fetchone()
    assert (audit["approval_kind"], audit["approval_ref"]) == ("code", prop["proposal_id"])
    again = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c2"},
        key=key,
    )
    assert again["isError"] and "already confirmed" in tool_text(again)
    replay = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert replay["structuredContent"]["data"]["status"] == "confirmed"  # idempotent repeat


def test_a_newer_approval_supersedes_the_older_approved_plan(client, conn, fake):
    key, plan_id, _prop = proposed(client, conn)
    conn.execute("UPDATE proposal SET status = 'confirmed', resolved_at = now()")
    conn.execute("UPDATE plan SET status = 'approved'")
    second = call_tool(
        client, "revise_plan", {"plan_id": plan_id, "instruction": "no Meta", "idempotency_key": "r1"}, key=key
    )["structuredContent"]["data"]
    prop2 = call_tool(client, "approve_plan", {"plan_id": second["plan_id"], "idempotency_key": "a2"}, key=key)[
        "structuredContent"
    ]["data"]
    code = proposals.issue_code(conn, prop2["proposal_id"], via="ops")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop2["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    statuses = {r["version"]: r["status"] for r in conn.execute("SELECT version, status FROM plan")}
    assert statuses == {1: "superseded", 2: "approved"}


def test_expired_and_declined_proposals_cannot_be_confirmed(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    conn.execute("UPDATE proposal SET expires_at = now() - interval '1 minute'")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert res["isError"] and "expired" in tool_text(res)
    conn.execute("UPDATE proposal SET expires_at = now() + interval '1 day'")
    res = call_tool(
        client,
        "decline_proposal",
        {"proposal_id": prop["proposal_id"], "reason": "not now", "idempotency_key": "x1"},
        key=key,
    )
    assert res["structuredContent"]["data"]["status"] == "declined"
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c2"},
        key=key,
    )
    assert res["isError"] and "already declined" in tool_text(res)


def test_another_business_cannot_decline_a_proposal(client, conn, fake):
    _key, _plan_id, prop = proposed(client, conn)
    other = business_with_key(conn, biz_id="biz_2")
    res = call_tool(
        client, "decline_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "x1"}, key=other
    )
    assert res["isError"] and "no open proposal with that id for this business" in tool_text(res)
    assert conn.execute("SELECT status FROM proposal").fetchone()["status"] == "proposed"


def test_the_plaintext_code_never_reaches_the_audit_table(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    n = conn.execute(
        "SELECT count(*) AS c FROM agent_action WHERE args_redacted::text LIKE %s OR result::text LIKE %s",
        (f"%{code}%", f"%{code}%"),
    ).fetchone()["c"]
    assert n == 0


def test_a_long_decline_reason_is_truncated_not_refused(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    res = call_tool(
        client,
        "decline_proposal",
        {"proposal_id": prop["proposal_id"], "reason": "r" * 300, "idempotency_key": "x1"},
        key=key,
    )
    assert not res["isError"] and res["structuredContent"]["data"]["status"] == "declined", tool_text(res)
    reason = conn.execute("SELECT payload->>'decline_reason' AS r FROM proposal").fetchone()["r"]
    assert reason == "r" * proposals.DECLINE_REASON_MAX == "r" * 200


def test_five_wrong_codes_lock_the_proposal_for_an_hour(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    for i in range(proposals.CODE_ATTEMPTS_PER_HOUR):
        call_tool(
            client,
            "confirm_proposal",
            {"proposal_id": prop["proposal_id"], "approval_code": "99-999", "idempotency_key": f"w{i}"},
            key=key,
        )
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "ok"},
        key=key,
    )
    assert res["isError"] and "too many" in tool_text(res)


def test_nonspend_proposals_are_capped_per_day(client, conn, fake):
    key, plan_id = planned(client, conn)
    for i in range(proposals.DAILY_CONFIRM_CAP):
        conn.execute(
            "INSERT INTO proposal (id, business_id, class, kind, payload, summary, status, expires_at, resolved_at) "
            "VALUES (%s, 'biz_1', 'nonspend', 'approve_plan', '{}', 's', 'confirmed', now() + interval '1 day', now())",
            (f"pr_{i}",),
        )
    prop = proposals.create(
        conn,
        "biz_1",
        cls="nonspend",
        kind="approve_plan",
        payload={"plan_id": plan_id},
        summary="s",
        what_changes=[],
        approval_url="http://testserver/plan/x",
    )
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c1"}, key=key)
    # open item A5: the sentence sends the owner nowhere a page's publish could not be approved (no plan page)
    assert res["isError"] and tool_text(res) == (
        "today's limit of 20 assistant-confirmed changes is reached; try again tomorrow"
    ), tool_text(res)
    conn.execute("UPDATE business SET operator_flags = '{\"daily_confirm_cap\": 21}'::jsonb WHERE id = 'biz_1'")
    res = call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c2"}, key=key)
    assert not res["isError"], tool_text(res)
    audit = conn.execute("SELECT approval_kind FROM agent_action WHERE tool = 'confirm_proposal' AND ok").fetchone()
    assert audit["approval_kind"] == "assistant"


def test_list_and_get_proposals(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    listed = call_tool(client, "list_proposals", {}, key=key)["structuredContent"]["data"]["proposals"]
    assert [p["proposal_id"] for p in listed] == [prop["proposal_id"]] and listed[0]["needs_code"] is True
    one = call_tool(client, "get_proposal", {"proposal_id": prop["proposal_id"]}, key=key)["structuredContent"]["data"]
    assert one["what_changes"][0] == "Landing page: yours, or one Admiry builds for you" and one["status"] == "proposed"
    other = business_with_key(conn, biz_id="biz_2")
    assert call_tool(client, "get_proposal", {"proposal_id": prop["proposal_id"]}, key=other)["isError"]


def test_ops_verify_marks_a_business_verified(conn, capsys, monkeypatch):
    business_with_key(conn)
    monkeypatch.setenv("AGENT_DATABASE_URL", TEST_DB_URL)
    assert ops.main(["verify", "biz_1"]) == 0
    row = conn.execute(
        "SELECT claim_state, verified_via, verified_at IS NOT NULL AS at FROM business WHERE id = 'biz_1'"
    ).fetchone()
    assert (row["claim_state"], row["verified_via"], row["at"]) == ("verified", "ops", True)
    assert ops.main(["verify", "biz_nope"]) == 1
    assert ops.main(["issue-code", "pr_nope"]) == 1


def test_ops_verify_upgrades_the_draft_key_and_the_ops_commands_are_audited(client, conn, fake, capsys, monkeypatch):
    benchmark.seed(conn)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    known = {"objective": "bookings", "service": "Invisalign", "customer_value_usd": 3000, "radius_miles": 5}
    started = tools_start.begin_anonymous(
        "dentist in Palo Alto, $2,000 a month", None, "en-US", known, ip="198.51.100.9"
    )["data"]
    biz, key, sid = started["business_id"], started["draft_key"], started["session_id"]
    # the website, asked first (the words name none): the fixture site, read -- the known facts answer the rest
    site = {
        "session_id": sid,
        "answers": {"website": {"other": "https://brightsmile.example"}},
        "idempotency_key": "w1",
    }
    assert data(call_tool(client, "answer", site, key=key))["ready"] is True
    plan_id = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key)[
        "structuredContent"
    ]["data"]["plan_id"]
    assert ops.main(["verify", biz]) == 0
    row = conn.execute("SELECT id, class, expires_at FROM api_key WHERE business_id = %s", (biz,)).fetchone()
    assert (row["class"], row["expires_at"]) == ("business", None)
    assert f"key {row['id']} upgraded to business" in capsys.readouterr().err
    prop = call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)[
        "structuredContent"
    ]["data"]
    assert prop["status"] == "proposed"
    assert ops.main(["proposals", biz]) == 0
    listing = capsys.readouterr().out
    assert listing.startswith(f"{prop['proposal_id']} approve_plan spend ") and "Launch plan v1" in listing
    assert ops.main(["issue-code", prop["proposal_id"]]) == 0
    code = capsys.readouterr().out.strip()
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert not res["isError"] and res["structuredContent"]["data"]["plan_status"] == "approved", tool_text(res)
    rows = conn.execute(
        "SELECT tool, actor_class, business_id, api_key_id, result FROM agent_action "
        "WHERE actor = 'ops:cli' ORDER BY id"
    ).fetchall()
    assert [(r["tool"], r["actor_class"], r["business_id"], r["api_key_id"]) for r in rows] == [
        ("ops.verify", "platform", biz, None),
        ("ops.issue-code", "platform", biz, None),
    ]
    assert rows[0]["result"] == {"verified": biz, "key_upgraded": row["id"]}
    assert rows[1]["result"] == {"proposal_id": prop["proposal_id"]} and code not in str(rows)


def test_issue_code_only_for_proposed_spend_proposals(conn):
    business_with_key(conn)
    prop = proposals.create(
        conn, "biz_1", cls="nonspend", kind="approve_plan", payload={}, summary="s", what_changes=[], approval_url="u"
    )
    with pytest.raises(ValueError, match="spend"):
        proposals.issue_code(conn, prop["proposal_id"], via="ops")


def test_the_lock_counts_only_this_businesss_wrong_codes(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    other = business_with_key(conn, biz_id="biz_2")
    for i in range(proposals.CODE_ATTEMPTS_PER_HOUR):  # another business naming this id: refused, never a lock
        call_tool(
            client,
            "confirm_proposal",
            {"proposal_id": prop["proposal_id"], "approval_code": "11-111", "idempotency_key": f"o{i}"},
            key=other,
        )
    for i in range(
        proposals.CODE_ATTEMPTS_PER_HOUR
    ):  # the owner's assistant trying without a code: refused, never a lock
        call_tool(client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": f"n{i}"}, key=key)
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "ok"},
        key=key,
    )
    assert not res["isError"], tool_text(res)


def test_the_lock_lifts_after_an_hour_without_burning_the_code(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    for i in range(proposals.CODE_ATTEMPTS_PER_HOUR):
        call_tool(
            client,
            "confirm_proposal",
            {"proposal_id": prop["proposal_id"], "approval_code": "99-999", "idempotency_key": f"w{i}"},
            key=key,
        )
    locked = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "l1"},
        key=key,
    )
    assert locked["isError"] and "try again in an hour" in tool_text(locked)
    assert conn.execute("SELECT used_at FROM approval_code").fetchone()["used_at"] is None
    conn.execute("UPDATE agent_action SET ts = ts - interval '2 hours' WHERE tool = 'confirm_proposal'")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "l2"},
        key=key,
    )
    assert not res["isError"], tool_text(res)


def test_codes_relayed_without_the_dash_still_work(client, conn, fake):
    key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    spoken = code.replace("-", " ")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": spoken, "idempotency_key": "c1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)


def test_a_stale_second_proposal_cannot_reapprove_the_plan(client, conn, fake):
    """A second request for one plan: a second ask answers with the first (final review B, Important 1), so it is made
    here directly, as one made before that rule (or the owner's page racing an ask) leaves it."""
    key, plan_id, first = proposed(client, conn)
    second = proposals.propose_plan(conn, "biz_1", plan_id)
    code1 = proposals.issue_code(conn, first["proposal_id"], via="ops")
    code2 = proposals.issue_code(conn, second["proposal_id"], via="ops")
    assert not call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": first["proposal_id"], "approval_code": code1, "idempotency_key": "c1"},
        key=key,
    )["isError"]
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": second["proposal_id"], "approval_code": code2, "idempotency_key": "c2"},
        key=key,
    )
    assert res["isError"] and "already approved" in tool_text(res)
    assert (
        conn.execute("SELECT used_at FROM approval_code WHERE proposal_id = %s", (second["proposal_id"],)).fetchone()[
            "used_at"
        ]
        is None
    )


# ---------------------------------------------------------------- A3 Task 4: the ads precheck and queue (P5, P6)


def test_an_approval_google_search_cannot_launch_is_refused_before_the_code_is_used(client, conn, fake, monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    key, _plan_id, prop = proposed(client, conn)  # create_plan's plan: no consultation for Google Search to run
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert res["isError"] and google.NOT_SEARCH_PLAN in tool_text(res)
    assert conn.execute("SELECT used_at FROM approval_code").fetchone()["used_at"] is None  # the code still works
    assert conn.execute("SELECT status FROM plan").fetchone()["status"] == "draft"
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0


def test_the_ads_precheck_refusals_come_back_through_confirm_one_sentence_each(client, conn, monkeypatch):
    from agent import ads_run
    from agent.tests.test_ads_run import seed

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    key, plan_id = seed(conn, status="draft", link=None)
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    args = {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"}
    # Addendum 3 (founder 2026-10-13): no way chosen yet (no link, no agreement), so the refusal for a link not made
    # names neither way -- never "since your ads run in your own account"
    res = call_tool(client, "confirm_proposal", args, key=key)
    assert res["isError"] and tool_text(res) == ads_mode.WAY_LINE
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")  # the owner's own account
    res = call_tool(client, "confirm_proposal", {**args, "idempotency_key": "c1b"}, key=key)
    assert res["isError"] and tool_text(res) == google.NOT_LINKED
    conn.execute("UPDATE business SET ads_mode = 'managed'")  # A3c-1: prepaid, Admiry's own account -- the balance
    res = call_tool(client, "confirm_proposal", {**args, "idempotency_key": "c2"}, key=key)
    assert res["isError"] and tool_text(res) == ads_run.NO_FUNDS.format(label="Google Search")
    assert conn.execute("SELECT used_at FROM approval_code").fetchone()["used_at"] is None


def test_an_approval_queues_the_ads_for_the_linked_account(conn, monkeypatch):
    from agent import ads_run, google_ads_link
    from agent.tests.test_ads_run import CID, GOOGLE, seed

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn, status="draft")
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    with conn.transaction():
        out = proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    assert out["execution"]["ads"] == "queued" and out["execution"]["presence"] == "queued_for_operator"
    row = ads_run.get(conn, "biz_1", GOOGLE)
    # the row keeps no account of its own (M0): the linked account is the business's link, as it was when queued
    assert (row["state"], row["plan_id"], row["channel"], row["resources"]) == ("queued", plan_id, GOOGLE, {})
    assert google_ads_link.get(conn, "biz_1").customer_id == CID
    assert conn.execute("SELECT json->'execution'->>'ads' AS ads FROM plan").fetchone()["ads"] == "queued"


def test_the_execution_record_has_an_entry_per_funded_channel_and_ads_mirrors_googles(conn, monkeypatch):
    from agent import ads_run
    from agent.tests.test_ads_run import GOOGLE, seed

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn, status="draft")
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    with conn.transaction():
        out = proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    assert (out["execution"][GOOGLE], out["execution"]["ads"]) == ("queued", "queued")
    stored = conn.execute("SELECT json->'execution' AS e FROM plan WHERE id = %s", (plan_id,)).fetchone()["e"]
    assert stored == out["execution"]
    assert [r["channel"] for r in ads_run.rows(conn, "biz_1")] == [GOOGLE]


def test_an_account_that_is_not_ready_waits_while_another_goes_ahead_and_alone_is_refused(conn, monkeypatch):
    """Spec §5.3 step 1, D77: a channel whose account is not ready is recorded `waiting_account`, never queued, when
    another funded channel can go ahead -- here Google Search's own link waits for the owner's acceptance. M0 Task 4:
    the precheck asks the adapter's `check_account` too (the link-status copy it kept is gone), so a plan none of whose
    accounts is ready is refused with the account's own sentence before the code is used -- here a prepaid business
    with no balance (A3c-1: Admiry's own account is ready, the balance is not)."""
    from fastmcp.exceptions import ToolError

    from agent import ads_run, wallet
    from agent.tests.test_ads_run import GOOGLE, sections, seed
    from agent.tests.test_ads_run_channels import add_plan_by_hand

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    _key, plan_id = seed(conn, status="draft")
    conn.execute("UPDATE channel_account SET mode = 'managed' WHERE business_id = 'biz_1'")
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    no_funds = ads_run.NO_FUNDS.format(label="Google Search")
    assert str(e.value) == no_funds and ads_run.get(conn, "biz_1", GOOGLE) is None

    conn.execute("UPDATE channel_account SET mode = 'own_account' WHERE business_id = 'biz_1'")
    conn.execute("UPDATE ads_link SET status = 'pending'")
    secs = sections()
    secs["ads"]["channels"].append({"channel": "newsbreak", "monthly_usd": 300.0})
    both = add_plan_by_hand(conn, secs, status="draft")
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=50_000, ref="pi_1", created_by="stripe")
    prop = proposals.propose_plan(conn, "biz_1", both)
    with conn.transaction():
        out = proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    assert (out["execution"][GOOGLE], out["execution"]["ads"]) == ("waiting_account", "waiting_account")
    assert out["execution"]["newsbreak"] == "waiting_creative"  # ready, but no image approved on the plan (D89)
    row = ads_run.get(conn, "biz_1", GOOGLE)
    assert (row["state"], row["last_error"]) == ("waiting_account", google.LINK_PENDING)


def _approve_with_newsbreak(conn, monkeypatch, tmp_path, *, change=None) -> tuple[str, dict]:
    """A dental plan funding Google Search and NewsBreak, with the picture its version pinned when it was made
    (`creative.proposed_image`: Invisalign's, as `plans_channels.fund` pins it for an Invisalign plan), approved by its
    owner with funds in place and the generated licensed set (a general dentists' picture, then Invisalign's);
    `change(secs)` edits the plan first. Returns the plan's id and the approval's answer."""
    from agent import wallet
    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import creative_set
    from agent.tests.test_ads_run import sections
    from agent.tests.test_ads_run_channels import add_plan_by_hand

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    monkeypatch.setattr(creative, "ROOT", creative_set(tmp_path / "creative"))
    seed(conn, status="superseded")
    secs = sections()
    secs["ads"]["channels"].append({"channel": "newsbreak", "monthly_usd": 300.0})
    secs["creative"]["proposed_image"] = {"newsbreak": "dentists/2.jpg"}
    if change:
        change(secs)
    plan_id = add_plan_by_hand(conn, secs, status="draft")
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=50_000, ref="pi_1", created_by="stripe")
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    with conn.transaction():
        out = proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    return plan_id, out


def _stored_creative(conn, plan_id: str) -> dict:
    return conn.execute("SELECT json->'sections'->'creative' AS c FROM plan WHERE id = %s", (plan_id,)).fetchone()["c"]


def test_approving_the_plan_approves_the_picture_its_card_shows_and_newsbreak_is_queued(conn, monkeypatch, tmp_path):
    """P7 (M1 Task 7): the picture on the plan card -- the one its version pinned -- is stored on the version approved
    (`sections.creative.approved_image`) before its rows are queued, so `ads_run.image_ready` finds it and NewsBreak's
    row is queued rather than waiting for a picture."""
    from agent import ads_run, plans

    plan_id, out = _approve_with_newsbreak(conn, monkeypatch, tmp_path)
    assert out["execution"]["newsbreak"] == "queued"
    assert _stored_creative(conn, plan_id) == {
        "angles": [],
        "proposed_image": {"newsbreak": "dentists/2.jpg"},
        "approved_image": {"newsbreak": "dentists/2.jpg"},
    }
    assert ads_run.get(conn, "biz_1", "newsbreak")["state"] == "queued"
    approved = plans.get(conn, "biz_1", plan_id)
    assert approved["status"] == "approved" and ads_run.image_ready(approved, "newsbreak")


def test_the_approval_stores_exactly_the_pinned_picture_whatever_the_set_would_pick_now(conn, monkeypatch, tmp_path):
    """Fix round 1, item 4 (review M8): approving approves the picture the card showed -- the version's pin -- never
    the one the set would pick at the moment of the approval (here Invisalign's own, for an Invisalign plan whose
    version pinned the general one): the owner never approves a picture they did not see."""

    def general(secs):
        secs["creative"]["proposed_image"] = {"newsbreak": "dentists/1.jpg"}

    plan_id, out = _approve_with_newsbreak(conn, monkeypatch, tmp_path, change=general)
    assert out["execution"]["newsbreak"] == "queued"
    assert _stored_creative(conn, plan_id)["approved_image"] == {"newsbreak": "dentists/1.jpg"}


def test_a_version_carrying_an_earlier_approved_picture_approves_its_own_pin(conn, monkeypatch, tmp_path):
    """adjust's version carries the creative section of the approved version it comes from -- its `approved_image` --
    beside the pin the new version got: the pin is what its card shows, and what approving it approves."""

    def carried(secs):  # the earlier version's picture, still in the set
        secs["creative"]["approved_image"] = {"newsbreak": "dentists/1.jpg"}

    plan_id, out = _approve_with_newsbreak(conn, monkeypatch, tmp_path, change=carried)
    assert out["execution"]["newsbreak"] == "queued"
    assert _stored_creative(conn, plan_id)["approved_image"] == {"newsbreak": "dentists/2.jpg"}


def test_a_version_made_before_the_pin_approves_the_picture_approved_with_it(conn, monkeypatch, tmp_path):
    def carried(secs):  # an approved version's creative section, as a version made before the pin carries it
        del secs["creative"]["proposed_image"]
        secs["creative"]["approved_image"] = {"newsbreak": "dentists/1.jpg"}

    plan_id, out = _approve_with_newsbreak(conn, monkeypatch, tmp_path, change=carried)
    assert out["execution"]["newsbreak"] == "queued"
    assert _stored_creative(conn, plan_id)["approved_image"] == {"newsbreak": "dentists/1.jpg"}


def test_a_pinned_picture_that_left_the_set_is_never_swapped_and_newsbreak_waits_for_a_new_one(
    conn, monkeypatch, tmp_path
):
    """Items 3 and 4: the version's picture is no longer in Admiry's set -- nothing is approved in its place (the trade
    still has pictures; the owner saw none of them), Google Search goes ahead, and NewsBreak waits in
    `waiting_creative` in its own words, for a version showing a new picture."""
    from agent import ads_run
    from agent.channels.newsbreak import adapter as nb

    def gone(secs):
        secs["creative"]["proposed_image"] = {"newsbreak": "dentists/9.jpg"}

    plan_id, out = _approve_with_newsbreak(conn, monkeypatch, tmp_path, change=gone)
    assert out["execution"]["newsbreak"] == "waiting_creative"
    assert _stored_creative(conn, plan_id) == {"angles": [], "proposed_image": {"newsbreak": "dentists/9.jpg"}}
    waiting = ads_run.get(conn, "biz_1", "newsbreak")
    assert ads_run.status_sentence(waiting) == f"NewsBreak: {nb.IMAGE_WAIT}."


def test_a_plan_whose_channels_show_no_picture_keeps_its_creative_section_as_it_was(conn, monkeypatch, tmp_path):
    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import creative_set

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    monkeypatch.setattr(creative, "ROOT", creative_set(tmp_path / "creative"))
    _key, plan_id = seed(conn, status="draft")  # Google Search alone
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    with conn.transaction():
        proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    assert _stored_creative(conn, plan_id) == {"angles": []}


def test_a_funded_channel_without_an_adapter_is_skipped_and_logged(conn, monkeypatch, caplog):
    import logging

    from agent import ads_run
    from agent.tests.test_ads_run import GOOGLE, add_plan, sections, seed

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, _plan_id = seed(conn, status="superseded")
    secs = sections()
    secs["ads"]["channels"].append({**secs["ads"]["channels"][0], "channel": "meta", "monthly_usd": 300.0})
    plan_id = add_plan(conn, secs, status="draft")
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    with caplog.at_level(logging.INFO, logger="agent.proposals"), conn.transaction():
        out = proposals.confirm(conn, "biz_1", prop["proposal_id"], code=None, web_owner=True)
    assert out["execution"][GOOGLE] == "queued" and "meta" not in out["execution"]
    assert [r["channel"] for r in ads_run.rows(conn, "biz_1")] == [GOOGLE]
    assert "meta" in caplog.text and "no adapter" in caplog.text


# ---------------------------------------------------------------- A3p-2 Task 3: publish_page/unpublish_page prechecks


def test_confirm_carries_the_proposals_own_kind(client, conn, fake):
    """tools_proposals.confirm_proposal's after-commit dispatch (publish_page/unpublish_page) reads
    proposals.confirm's own "kind" to pick its hook; approve_plan must keep getting one too."""
    _key, _plan_id, prop = proposed(client, conn)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    with conn.transaction():
        out = proposals.confirm(conn, "biz_1", prop["proposal_id"], code=code)
    assert out["kind"] == "approve_plan"


def test_precheck_publish_page_refuses_without_a_plan(conn, monkeypatch):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    biz = business.create(conn)

    with pytest.raises(ToolError, match="Build the plan first"):
        proposals.precheck_publish_page(conn, biz, {"token": "pv_nope"})


def test_precheck_publish_page_refuses_when_hosting_is_off(conn, monkeypatch):
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    seed(conn, link=None, page=False, conversions=False)

    with pytest.raises(ToolError, match="Hosting is not set up on this server"):
        proposals.precheck_publish_page(conn, "biz_1", {"token": "pv_nope"})


def test_precheck_publish_page_refuses_a_version_that_does_not_exist(conn, monkeypatch):
    """H25 (round 2): the version lookup is simply the business's one page row (`landing.current`) -- round 1
    filtered it to a `source IN ('hosted', 'admiry')` row, which no longer exists as a distinct case once a
    business has only ever one row -- so even ensure_page_for_kit's bare owner-source placeholder is enough to
    exercise "no version N"; nothing here needs to be hosted first."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    seed(conn, link=None, page=False, conversions=False)
    landing.ensure_page_for_kit(conn, "biz_1")

    with pytest.raises(ToolError, match="no version 5"):
        proposals.precheck_publish_page(conn, "biz_1", {"version": 5})


def test_precheck_publish_page_refuses_a_token_that_expired_after_the_proposal_was_made(client, conn, monkeypatch):
    """H8: a preview token lives PREVIEW_TTL (24 h), far shorter than this nonspend proposal's own
    NONSPEND_TTL (3 days) -- so publish_page succeeding is no guarantee confirm_proposal still can."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    key, _plan_id = seed(conn, link=None, page=False, conversions=False)
    html = (
        "<html><body><p>"
        + "Real content about the business. " * 20
        + '</p><a href="tel:+16505550100">Call</a></body></html>'
    )
    prev = call_tool(client, "preview_page", {"html": html}, key=key)
    token = data(prev)["token"]
    prop = call_tool(client, "publish_page", {"idempotency_key": "pp1", "token": token}, key=key)
    proposal_id = data(prop)["proposal_id"]
    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute' WHERE token = %s", (token,))

    res = call_tool(client, "confirm_proposal", {"proposal_id": proposal_id, "idempotency_key": "c1"}, key=key)

    assert res["isError"] and "expired or is unknown" in tool_text(res)
    assert conn.execute("SELECT status FROM proposal WHERE id = %s", (proposal_id,)).fetchone()["status"] == "proposed"


def test_precheck_unpublish_page_refuses_without_a_published_hosted_page(conn):
    seed(conn, link=None, page=False, conversions=False)

    with pytest.raises(ToolError, match="no published Admiry-hosted page"):
        proposals.precheck_unpublish_page(conn, "biz_1", {})
