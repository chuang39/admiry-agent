"""The never column (spec 2026-10-08 §2.2, R3; agent/operations.py NEVER), a test per row: no public command and no
home answer, given one business's key, carries another business's data, Admiry's own accounts, the team's own working
or a secret -- and a write that names another business's ids changes nothing of it. Two businesses: the dental walk
through the public tools (surface_helpers.planned, `biz_1`, its key) and a plumber seeded beside it (`biz_zebra`: no
random id -- "biz_" and hex -- can hold it) whose every field carries a marker no answer of the first may hold."""

from __future__ import annotations

import json

import pytest
from psycopg.types.json import Jsonb

from agent import business, db, google_ads_link, models, operations, proposals, uploads, wallet
from agent.tests.conftest import call_tool, data
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import planned
from agent.tests.test_ads_run import add_plan, sections
from agent.tests.test_home_api import fake  # noqa: F401 -- a fixture used by name

OTHER = "biz_zebra"  # final review m16: "biz_2" sat inside a random "biz_" + 16 hex id one walk in 16
MARKERS = (OTHER, "Zebra Plumbing", "415-555-0199", "zebra.example", "Quincy Marker")
# every read a business key can make through the public tools, each view of the tools that have views
READS = (
    ("get_marketing_state", {}),
    ("get_business_profile", {}),
    ("get_connected_accounts", {}),
    ("get_campaign_performance", {"period": "30d"}),
    ("get_insights", {}),
    ("get_research", {}),
    ("get_leads", {}),
    ("get_marketing_plan", {}),
    ("get_campaigns", {}),
    ("get_landing_page", {"view": "status"}),
    ("get_landing_page", {"view": "kit"}),
    ("get_landing_page", {"view": "source"}),
    ("get_billing", {"view": "position"}),
    ("get_billing", {"view": "payments"}),
    ("get_activity_log", {}),
    ("get_site_templates", {}),
)


def test_the_rows_are_the_ones_tested_here():
    assert [row["id"] for row in operations.NEVER] == [
        "other_business",
        "admiry_business",
        "team_working",
        "secrets",
        "acting_for_another",
    ]


def _other(conn) -> dict:
    """The plumber beside the walk: its key, its knowledge, a draft plan and a lead, each with a marker."""
    key = business_with_key(conn, biz_id=OTHER, claim_state="verified")
    conn.execute("UPDATE business SET owner_email = 'owner@zebra.example' WHERE id = %s", (OTHER,))
    knowledge = {
        "name": "Zebra Plumbing Co",
        "phone": "415-555-0199",
        "website": "https://zebra.example",
        "category": "home_services",
        "services": [{"name": "Drain cleaning", "intent_phrases": []}],
    }
    business.add_knowledge(conn, OTHER, knowledge, source="owner")
    plan_id = add_plan(conn, sections(), biz=OTHER, status="draft")
    conn.execute(
        "INSERT INTO lead (id, business_id, kind, fields) VALUES ('ld_other', %s, 'form', %s)",
        (OTHER, Jsonb({"name": "Quincy Marker", "phone": "415-555-0199"})),
    )
    return {"key": key, "plan_id": plan_id, "lead_id": "ld_other"}


def _answers(client, key: str) -> str:
    """Every read of READS with `key`, as one text: what each tool returned -- its words and its data."""
    return "\n".join(json.dumps(call_tool(client, name, args, key=key), sort_keys=True) for name, args in READS)


def test_another_business_is_never_in_an_answer_nor_in_what_the_model_reads(client, conn, walk_fake):
    """Prepaid, like `test_admirys_own_accounts_are_never_named_to_an_owner`: own_account biz_1 makes both
    `get_billing` views (position, payments) refuse for having no balance at all, so a leak there could never show.
    Prepaid, they are real reads -- proven by the positive control -- and the plumber's own topup note is the
    marker that would show through `list_payments` if the ledger were not scoped by business_id."""
    mine = planned(client, conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    google_ads_link.mirror(conn, "biz_1")
    wallet.add_entry(
        conn, "biz_1", kind="topup", amount_cents=50_000, ref="pi_mine_never", created_by="stripe", note="MINE-NOTE-7f3"
    )
    other = _other(conn)
    wallet.add_entry(
        conn, OTHER, kind="topup", amount_cents=30_000, ref="pi_other_never", created_by="stripe", note="ZEBRA-NOTE-7f3"
    )
    said = _answers(client, mine["key"])
    assert "MINE-NOTE-7f3" in said  # positive control: biz_1's own billing views are real reads now, not refusals
    for marker in (*MARKERS, other["plan_id"], other["key"], "ZEBRA-NOTE-7f3"):
        assert marker not in said, marker
    question = "How are the other plumbers near me doing?"
    walk_fake.answers_to = {question: models.MailAnswer(text=operations.NEVER_SAY, grounded=True)}
    asked = call_tool(client, "ask_admiry", {"question": question}, key=mine["key"])
    material = json.dumps(walk_fake.answer_calls, default=str) + json.dumps(asked)
    for marker in (*MARKERS, other["plan_id"]):
        assert marker not in material, marker


def test_admirys_own_accounts_are_never_named_to_an_owner(client, conn, walk_fake):
    """A shared account Admiry runs a trade's ads from is Admiry's: a prepaid owner whose ads run in it never reads its
    id (the Google adapter's check_account names an account only when it is the business's alone)."""
    mine = planned(client, conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    google_ads_link.mirror(conn, "biz_1")
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=50_000, ref="pi_never", created_by="stripe")
    conn.execute(
        "INSERT INTO google_ads_pool (customer_id, scope, capacity, status, added_by) "
        "VALUES ('5550001234', 'dentists', NULL, 'open', 'ops:cli')"
    )
    conn.execute(
        "INSERT INTO google_ads_binding (business_id, customer_id, family, trade, bound_by, note) "
        "VALUES ('biz_1', '5550001234', 'dentists', NULL, 'job:ads-launch', NULL)"
    )
    said = _answers(client, mine["key"])
    assert "5550001234" not in said and "555-000-1234" not in said


def test_the_teams_own_working_is_never_in_an_answer(client, conn, walk_fake):
    """The Critic's grades, verdict and score and what it found missing -- on this business's own plan -- are the
    team's (K1): no answer the owner's key gets holds them, nor the insight view the team reads. A guard over the
    public reads, none of which reads `plan_review` -- nor does anything the owner reads now that the setup card's
    own second look is gone (`test_the_teams_own_working_is_never_on_the_owners_surfaces`'s planted row proves it)."""
    mine = planned(client, conn)
    review = {
        "summary": "s",
        "checks": [],
        "grounded": True,
        "grades": [{"dimension": "fit", "grade": 2, "note": "GRADE-NOTE-7f3"}],
        "missing": ["MISSING-NOTE-7f3"],
    }
    conn.execute(
        "INSERT INTO plan_review (id, plan_id, business_id, review, model, score, verdict, actor) "
        "VALUES (%s, %s, 'biz_1', %s, 'm', 0.25, 'revise', 'job:critic')",
        (db.new_id("rv"), mine["plan_id"], Jsonb(review)),
    )
    said = _answers(client, mine["key"])
    for marker in ("GRADE-NOTE-7f3", "MISSING-NOTE-7f3", '"grades"', '"verdict"', '"score"'):
        assert marker not in said, marker
    assert "team" not in data(call_tool(client, "get_insights", {}, key=mine["key"]))


def test_the_teams_own_working_is_never_on_the_owners_surfaces(conn):
    """L1 Task 4: the setup card's own second look is gone (`setup.review` and setup_api's "review" action, Task 3);
    the two surfaces the home now draws a draft's status from are `setup.state` and the launch checklist
    (`launch_check.check`), and neither reads `plan_review` at all -- not the model's summary and checks, let alone
    the Critic's grades, score, verdict or what it found missing. Where the old test (`test_the_teams_own_working_
    is_never_on_the_setup_card`) kept a positive control -- the model's summary got through where the Critic's own
    working did not -- there is no surface left for *either* to reach: this plants the same `plan_review` row
    (summary, checks, and the Critic's grades/score/verdict/missing alike) and asserts plainly that none of it
    -- the model's part included -- shows up in what either function returns."""
    from agent import config, launch_check, plan_review, plans, setup
    from agent.tests.test_ads_run_channels import add_plan_by_hand
    from agent.tests.test_review_launch import draft_sections, funded_prepaid

    funded_prepaid(conn)
    plan_id = add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    settings = config.load()
    plan = plans.get(conn, "biz_1", plan_id)
    cents, _ = plan_review.money(conn, "biz_1", plan)
    blocked = proposals.launch_refusal(conn, "biz_1", plan_id)
    review = {
        "summary": "SUMMARY-7f3",
        "checks": [],
        "grounded": True,
        "grades": [{"dimension": "fit", "grade": 2, "note": "GRADE-NOTE-7f3"}],
        "missing": ["MISSING-NOTE-7f3"],
    }
    conn.execute(
        "INSERT INTO plan_review (id, plan_id, business_id, balance_cents, blocked, review, model, score, verdict, "
        "actor) VALUES (%s, %s, 'biz_1', %s, %s, %s, 'm', 0.25, 'revise', 'job:critic')",
        (db.new_id("rv"), plan_id, cents, blocked, Jsonb(review)),
    )
    state = setup.state(conn, settings, "biz_1")
    assert state["step"] == "done" and state["plan_status"] == "draft"  # the draft, waiting for the owner's yes
    out = json.dumps({"state": state, "check": launch_check.check(conn, settings, "biz_1")})
    markers = ("SUMMARY-7f3", "GRADE-NOTE-7f3", "MISSING-NOTE-7f3", '"grades"', '"missing"', '"verdict"', '"score"')
    for marker in markers:
        assert marker not in out, marker


def test_secrets_are_never_in_an_answer(client, conn, walk_fake, monkeypatch):
    """Keys, tokens, approval codes and another owner's links never come back: not the caller's own key, not the other
    business's key or upload link, not a code issued for the caller's own plan, not one of Admiry's own secrets."""
    secrets = {
        "STRIPE_SECRET_KEY": "sk_test_NEVER0b0",
        "GOOGLE_ADS_DEVELOPER_TOKEN": "devtok-NEVER0b0",
        "GOOGLE_ADS_REFRESH_TOKEN": "1//NEVER0b0",
        "RESEND_API_KEY": "re_NEVER0b0",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(proposals, "new_code", lambda: "CODE-NEVER0b0")
    mine = planned(client, conn)
    other = _other(conn)
    prop = proposals.propose_plan(conn, "biz_1", mine["plan_id"])
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    page = conn.execute(
        "INSERT INTO page (id, business_id, source, url, status) VALUES ('pg_other', %s, 'owner', '', 'preview') "
        "RETURNING id",
        (OTHER,),
    ).fetchone()
    upload = uploads.new_token(conn, page["id"])
    said = _answers(client, mine["key"])
    for secret in (*secrets.values(), mine["key"], other["key"], code, upload):
        assert secret not in said, secret


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_marketing_plan", {"plan_id": "{plan_id}"}),
        ("update_lead", {"lead_id": "{lead_id}", "status": "won", "idempotency_key": "n1"}),
        ("launch_marketing_plan", {"plan_id": "{plan_id}", "idempotency_key": "n2"}),
        ("update_marketing_plan", {"plan_id": "{plan_id}", "changes": {"level": "minimum"}, "idempotency_key": "n3"}),
    ],
)
def test_acting_for_another_business_changes_nothing_of_it(client, conn, walk_fake, tool, args):
    mine = planned(client, conn)
    other = _other(conn)
    sent = json.loads(json.dumps(args).replace("{plan_id}", other["plan_id"]).replace("{lead_id}", other["lead_id"]))
    versions = conn.execute("SELECT count(*) AS n FROM plan WHERE business_id = %s", (OTHER,)).fetchone()["n"]
    said = json.dumps(call_tool(client, tool, sent, key=mine["key"]))
    assert all(marker not in said for marker in MARKERS)
    assert conn.execute("SELECT status FROM lead WHERE id = 'ld_other'").fetchone()["status"] == "new"
    assert conn.execute("SELECT count(*) AS n FROM plan WHERE business_id = %s", (OTHER,)).fetchone()["n"] == versions
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (other["plan_id"],)).fetchone()["status"] == "draft"


def test_the_homes_answers_are_the_visitors_business_alone(client, conn, google, monkeypatch, fake):  # noqa: F811
    """The home's own answers -- a reload, MarketingState, the setup card, the page card, a fix -- for the signed-in
    owner of one business carry nothing of the plumber beside it."""
    from agent.tests.test_home_api import IP, _claimed_plan, turn

    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")
    _claimed_plan(client, conn, google)
    _other(conn)
    csrf = client.get("/api/csrf").json()["csrf"]
    said = json.dumps(
        [
            turn(client, {"action": "state"}).json(),
            client.get("/api/state", headers=IP).json(),
            client.post("/api/setup", json={"action": "state"}, headers=IP).json(),
            client.post("/api/page/state", json={"csrf": csrf}, headers=IP).json(),
            client.post("/api/blocker", json={"csrf": csrf, "action": "check_page"}, headers=IP).json(),
        ]
    )
    for marker in MARKERS:
        assert marker not in said, marker
