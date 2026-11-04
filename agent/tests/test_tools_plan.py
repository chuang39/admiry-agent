import threading

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import auth, benchmark, copy, db, llm, plans
from agent.tests.conftest import TEST_DB_URL, call_tool, tool_text
from agent.tests.fixtures import READ_SITE, FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture

LINES = [
    "Landing page: yours, or one Admiry builds for you",
    "Local SEO/GEO: $0",
    "Google Search: $1,300/mo · Facebook & Instagram: $700/mo",
    "Ads and images: made by Admiry, you approve",
    "Call, form and appointment tracking: connected",
    "Automatic optimization: safe changes only",  # no e-mail promised in a stored line (Dg P8)
    "Estimated monthly leads: 20–40 (typical range for similar businesses, not a guarantee)",
    "Admiry runs the ads from your prepaid balance at cost, or Google bills your own Google Ads account; Admiry charges no management fee.",
]


def test_the_disclosure_names_both_ways_the_ads_run_in_owner_words():
    """The founder's ruling (2026-10-13): the plan's own money line names both ways side by side -- Admiry running the
    ads from the prepaid balance, the owner's own Google Ads account billed by Google -- with its meaning kept (at cost,
    no management fee), in the owner's words."""
    said = plans.DISCLOSURE
    assert said == LINES[-1] and copy.jargon(said) is None
    assert "prepaid balance at cost" in said and "Google bills your own Google Ads account" in said
    assert said.endswith("Admiry charges no management fee.")


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    return gen


def described(client, conn, budget=2000, target=20):
    key = business_with_key(conn)
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    goal = {"idempotency_key": "g1"}
    if budget is not None:
        goal["monthly_budget_usd"] = budget
    if target is not None:
        goal["target"] = target
    call_tool(client, "set_goal", goal, key=key)
    return key


def test_create_plan_lines_match_the_spec_worked_example(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn)
    res = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)
    assert not res["isError"], tool_text(res)
    body = res["structuredContent"]["data"]
    assert body["lines"] == LINES and body["version"] == 1 and body["status"] == "draft"
    assert body["plan_url"] == f"http://testserver/plan/{body['plan_id']}" and body["notes"] == []
    assert (
        fake.plan_calls[0]["goal"]["monthly_limit_usd"] == 2000.0
        and fake.plan_calls[0]["knowledge"]["name"] == "Bright Smile Dental"
    )
    row = conn.execute("SELECT status, estimate_basis, json FROM plan WHERE id = %s", (body["plan_id"],)).fetchone()
    assert row["status"] == "draft" and row["estimate_basis"]["basis"] == "benchmark"
    assert set(row["estimate_basis"]["benchmark_rows"]) == {"dentists:google_search", "dentists:meta"}
    assert row["json"]["sections"]["ads"]["channels"][0]["headlines"] == [
        "Invisalign in Palo Alto",
        "Same-Week Appointments",
        "Clear Aligners, Family Care",
    ]


def test_budgets_over_the_limit_are_scaled_in_code(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn, budget=1000)
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["lines"][2] == "Google Search: $650/mo · Facebook & Instagram: $350/mo"
    assert body["notes"] == ["budgets scaled to the $1,000 monthly limit"]


def test_over_length_ad_lines_are_dropped_with_a_note(client, conn, fake):
    benchmark.seed(conn)
    fake.draft.ads[0].headlines.append("This headline is far too long to run")  # 36 characters
    key = described(client, conn)
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["notes"] == ["Google Search: 1 ad line(s) over the length limit were dropped"]
    row = conn.execute("SELECT json FROM plan").fetchone()
    assert len(row["json"]["sections"]["ads"]["channels"][0]["headlines"]) == 3


def test_no_budget_means_no_ads_and_no_estimate(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn, budget=None, target=10)
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["lines"][2] == "Ads: none until a budget is set"
    assert body["lines"][6] == "Estimated monthly leads: not estimated yet (no advertising budget)"
    assert body["notes"] == ["no monthly limit set: advertising budgets were removed"]
    assert conn.execute("SELECT estimate_basis FROM plan").fetchone()["estimate_basis"] == {}


def test_get_plan_returns_the_latest_or_by_id(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn)
    none = call_tool(client, "get_plan", {}, key=key)
    assert none["isError"] and "no plan yet" in tool_text(none)
    first = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    res = call_tool(client, "get_plan", {}, key=key)["structuredContent"]["data"]
    assert (
        res["plan_id"] == first["plan_id"]
        and res["estimate"]["low"] == 20
        and res["ads"]["monthly_limit_usd"] == 2000.0
    )
    by_id = call_tool(client, "get_plan", {"plan_id": first["plan_id"]}, key=key)["structuredContent"]["data"]
    assert by_id["lines"] == LINES
    other = call_tool(client, "get_plan", {"plan_id": "pl_nope"}, key=key)
    assert other["isError"]


def test_revise_plan_creates_a_new_version_and_supersedes_the_draft(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn)
    first = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    fake.draft = plan_draft_fixture(google=2000, meta=0)
    res = call_tool(
        client, "revise_plan", {"plan_id": first["plan_id"], "instruction": "no Meta", "idempotency_key": "r1"}, key=key
    )
    assert not res["isError"], tool_text(res)
    body = res["structuredContent"]["data"]
    assert body["version"] == 2 and body["lines"][2] == "Google Search: $2,000/mo"
    assert (
        fake.plan_calls[1]["instruction"] == "no Meta"
        and fake.plan_calls[1]["previous"]["ads"]["channels"][1]["channel"] == "meta"
    )
    rows = {
        r["version"]: (r["status"], r["diff_from"]) for r in conn.execute("SELECT version, status, diff_from FROM plan")
    }
    assert rows == {1: ("superseded", None), 2: ("draft", first["plan_id"])}


def test_plan_tools_need_knowledge_and_a_goal_first(client, conn, fake):
    key = business_with_key(conn)
    res = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)
    assert res["isError"] and "describe the business first, then create a plan" in tool_text(res)
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    res = call_tool(client, "create_plan", {"idempotency_key": "p2"}, key=key)
    assert res["isError"] and "set a goal first: a monthly budget alone is enough" in tool_text(res)
    assert fake.plan_calls == []


def test_channels_scaled_below_a_dollar_are_dropped(client, conn, fake):
    benchmark.seed(conn)
    fake.draft = plan_draft_fixture(google=1990, meta=10)
    key = described(client, conn, budget=100)
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["lines"][2] == "Google Search: $99/mo"
    assert body["notes"] == [
        "budgets scaled to the $100 monthly limit",
        "Facebook & Instagram: below $1 after scaling, removed",
    ]


def test_revise_plan_with_an_unknown_plan_is_refused(client, conn, fake):
    key = business_with_key(conn)
    res = call_tool(
        client, "revise_plan", {"plan_id": "pl_nope", "instruction": "no Meta", "idempotency_key": "r1"}, key=key
    )
    assert res["isError"] and "no plan with that id" in tool_text(res)
    assert fake.plan_calls == []


def test_the_goals_autopilot_wins_over_the_drafts(client, conn, fake):
    benchmark.seed(conn)
    fake.draft.autopilot = "all"  # the model asked for more than the goal allows
    key = described(client, conn)  # goal autopilot: the default 'safe'
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["lines"][5] == "Automatic optimization: safe changes only"
    assert (
        conn.execute("SELECT json->'sections'->'optimization'->>'autopilot' AS a FROM plan").fetchone()["a"] == "safe"
    )


def test_a_channel_named_twice_is_merged(client, conn, fake):
    benchmark.seed(conn)
    fake.draft = plan_draft_fixture(google=1000, meta=500)
    fake.draft.ads.append(
        fake.draft.ads[0].model_copy(
            update={"monthly_usd": 300, "headlines": ["Invisalign Consults"], "landing_service": "Cleanings"}
        )
    )
    key = described(client, conn)
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["lines"][2] == "Google Search: $1,300/mo · Facebook & Instagram: $500/mo" and body["notes"] == []
    google = conn.execute("SELECT json->'sections'->'ads'->'channels'->0 AS c FROM plan").fetchone()["c"]
    assert google["headlines"] == [
        "Invisalign in Palo Alto",
        "Same-Week Appointments",
        "Clear Aligners, Family Care",
        "Invisalign Consults",
    ]
    assert google["landing_service"] == "Invisalign"  # the first one's


def test_an_estimate_under_five_reads_fewer_than_5():
    sections, _ = plans.normalize(plan_draft_fixture(google=50, meta=0), 50)
    sections["estimate"] = {"low": 0, "high": 0, "basis": "benchmark", "confidence": "low"}  # rounded to 5s
    assert (
        plans.owner_lines(sections)["estimate"]
        == "Estimated monthly leads: fewer than 5 (typical range for similar businesses, not a guarantee)"
    )


def test_revise_drafts_against_the_latest_goal(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn)  # $2,000
    first = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    goal = call_tool(client, "set_goal", {"monthly_budget_usd": 500, "idempotency_key": "g2"}, key=key)[
        "structuredContent"
    ]["data"]["goal"]
    res = call_tool(
        client,
        "revise_plan",
        {"plan_id": first["plan_id"], "instruction": "same, cheaper", "idempotency_key": "r1"},
        key=key,
    )
    body = res["structuredContent"]["data"]
    assert body["lines"][2] == "Google Search: $325/mo · Facebook & Instagram: $175/mo" and body["notes"] == [
        "budgets scaled to the $500 monthly limit"
    ]
    assert fake.plan_calls[1]["goal"]["monthly_limit_usd"] == 500.0
    assert (
        conn.execute("SELECT goal_id FROM plan WHERE id = %s", (body["plan_id"],)).fetchone()["goal_id"] == goal["id"]
    )


def test_one_draft_at_a_time_per_business(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn)
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('llm:biz_1'))")  # a draft in flight
        res = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)
        assert res["isError"] and "another draft for this business is in progress; try again in a moment" in tool_text(
            res
        )
        assert fake.plan_calls == []
        failed = conn.execute("SELECT ok FROM agent_action WHERE tool = 'create_plan'").fetchone()
        assert failed["ok"] is False  # audited like any refusal
        other.execute("SELECT pg_advisory_unlock(hashtext('llm:biz_1'))")
        res = call_tool(client, "create_plan", {"idempotency_key": "p2"}, key=key)
        assert not res["isError"], tool_text(res)


def test_autopilot_can_be_turned_off_through_set_goal(client, conn, fake):
    benchmark.seed(conn)
    key = described(client, conn)
    res = call_tool(
        client, "set_goal", {"monthly_budget_usd": 2000, "autopilot": "off", "idempotency_key": "g2"}, key=key
    )
    assert not res["isError"] and res["structuredContent"]["data"]["goal"]["autopilot"] == "off", tool_text(res)
    body = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    assert body["lines"][5] == "Automatic optimization: off"
    res = call_tool(
        client, "set_goal", {"monthly_budget_usd": 2000, "autopilot": "all", "idempotency_key": "g3"}, key=key
    )
    assert res[
        "isError"
    ] and "turning automatic optimization fully on needs set_autopilot, which arrives later" in tool_text(res)
    res = call_tool(client, "set_goal", {"monthly_budget_usd": 1500, "idempotency_key": "g4"}, key=key)
    assert res["structuredContent"]["data"]["goal"]["autopilot"] == "off"  # kept until the owner changes it


def test_llm_tools_are_capped_per_business_per_day_lower_for_draft_keys(client, conn, fake, monkeypatch):
    benchmark.seed(conn)
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY_DRAFT", 1)
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 3)
    draft = business_with_key(conn, biz_id="biz_d", cls="draft")
    assert not call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=draft)[
        "isError"
    ]
    res = call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d2"}, key=draft)
    assert res["isError"] and "today's limit of 1 drafts for this business is reached; try again tomorrow" in tool_text(
        res
    )
    key = described(client, conn)  # biz_1, business key: describe_business is LLM call 1
    first = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]  # 2
    res = call_tool(
        client, "revise_plan", {"plan_id": first["plan_id"], "instruction": "no Meta", "idempotency_key": "r1"}, key=key
    )
    assert not res["isError"], tool_text(res)  # 3: biz_d's calls are not counted for biz_1
    res = call_tool(
        client, "revise_plan", {"plan_id": first["plan_id"], "instruction": "no Meta", "idempotency_key": "r2"}, key=key
    )
    assert res["isError"] and "today's limit of 3 drafts" in tool_text(res)
    assert len(fake.extract_calls) == 2 and len(fake.plan_calls) == 2  # the refused calls never reached the model


def test_concurrent_plan_versions_fail_loudly(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    sections, _ = plans.normalize(plan_draft_fixture(), 2000)
    plans.add_estimate(conn, sections, "dentists")  # table empty here: basis none, which is fine
    kw = {"goal_id": None, "sections": sections, "estimate_basis": {}, "rationale": "r", "notes": []}
    plans.create_version(conn, "biz_1", **kw)
    outcome = []

    def append_v2():
        try:
            plans.create_version(conn, "biz_1", **kw)
            outcome.append(None)
        except ToolError as e:
            outcome.append(e)

    # an uncommitted version 2: the max() cannot see it, the unique index can
    with db.connect(TEST_DB_URL) as other, other.transaction():
        other.execute(
            "INSERT INTO plan (id, business_id, version, json) VALUES ('pl_x', 'biz_1', 2, %s)",
            (Jsonb({"sections": {}, "lines": {}}),),
        )
        t = threading.Thread(target=append_v2)
        t.start()
        t.join(timeout=1)
        assert t.is_alive()  # blocked on the unique index until the other transaction commits
    t.join(timeout=5)
    assert isinstance(outcome[0], ToolError) and "same moment" in str(outcome[0])


def _ready_consultation(client, conn) -> tuple[str, dict]:
    """A keyed business with knowledge, a $2,000 goal and a dental consultation every question of which is answered:
    (key, session), ready for `recommend`."""
    benchmark.seed(conn)
    key = business_with_key(conn)
    conn.execute(
        "INSERT INTO knowledge (business_id, version, json, source) VALUES ('biz_1', 1, %s, 'extract')",
        (Jsonb(knowledge_fixture().model_dump()),),
    )
    call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)
    from datetime import datetime, timedelta, timezone

    from agent import consult

    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, knowledge_fixture().model_dump()),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": "svc_invisalign", "raw": "", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "amount_usd": 3000.0, "raw": "", "source": "choice"},
            # where the ads centre, left for later (walk fixes 1, Task 4)
            "location": {"skipped": True, "raw": "later", "source": "other"},
            "radius": {"miles": 5.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert s["status"] == "ready"
    return key, s


def test_get_plan_returns_the_cards_for_a_consulted_plan(client, conn, fake):
    key, s = _ready_consultation(client, conn)
    r = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)["structuredContent"][
        "data"
    ]
    g = call_tool(client, "get_plan", {"plan_id": r["plan_id"]}, key=key)["structuredContent"]["data"]
    assert (
        g["cards"]["budget"]["monthly_usd"] == 2000 and g["cards"]["economics"]["columns"]["planning"]["spend"] == 2000
    )
    assert g["cards"]["plan"]["goal"].startswith("More Invisalign bookings within 5 miles of Bright Smile Dental")
    # A3 final review (spec §7): the Muse/Claude cards name where the ads send visitors, like the homepage's
    assert r["cards"]["ads_preview"]["final_host"] is None and g["cards"]["ads_preview"]["final_host"] is None
    from agent import landing

    landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    g = call_tool(client, "get_plan", {"plan_id": r["plan_id"]}, key=key)["structuredContent"]["data"]
    assert g["cards"]["ads_preview"]["final_host"] == "brightsmile.example"


def test_recommend_names_the_pages_host_on_its_ad_preview(client, conn, fake):
    """A3 final review (spec §7): `recommend`'s own cards carry the landing host once the business has a page."""
    from agent import landing

    key, s = _ready_consultation(client, conn)
    landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    r = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)
    assert not r["isError"], tool_text(r)
    assert r["structuredContent"]["data"]["cards"]["ads_preview"]["final_host"] == "brightsmile.example"
