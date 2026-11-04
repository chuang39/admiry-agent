"""marketing_state (spec 2026-10-01 §4.3; plan G1 Task 6): one read that tells an assistant where the business stands
and what to do next -- and never carries a customer's name or number."""

from datetime import UTC, datetime

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import (
    ads_mode,
    ads_refusal,
    ads_run,
    agreements,
    auth,
    blockers,
    business,
    config,
    consult,
    copy,
    envelope,
    google_ads_link,
    jobs,
    keys,
    landing,
    launch_check,
    launch_review,
    leads,
    plan_review,
    plans,
    proposals,
    setup,
    state,
    tools_state,
    wallet,
)
from agent.channels import registry
from agent.channels.google_search.adapter import CONNECT_OWN_ACCOUNT, NOT_LINKED, TOO_LITTLE_COPY
from agent.channels.google_search.client import RefusalDetail
from agent.surface import gate
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import ANSWERS, planned, settle_page, started
from agent.tests.test_ads_run import (
    CHECK_FAILED,
    CHECK_OK,
    CID,
    PAGE_URL,
    PHONE_FIX,
    _check_failed,
    _knowledge,
    add_plan,
    sections,
    seed,
)
from agent.tests.test_ads_run_channels import funds, newsbreak_only, seed_plan, two_channels
from agent.tests.test_sites_build import _own, dental_library, renderer  # noqa: F401 - pytest fixtures, by name
from agent.tests.test_state import campaign, consulted
from agent.tests.test_tools_hosting import GOOD_HTML
from agent.tests.test_tools_sites import DENTAL_WORDS, _fitted
from agent.tests.test_wallet import fund

LAUNCH = {"id": "launch", "label": "Launch the plan", "tool": "launch_marketing_plan", "primary": True}
SIGN_IN = {"id": "sign_in", "label": "Sign in to keep this plan", "tool": "connect_account", "primary": True}
CONNECT = {"id": "connect", "label": "Connect where the ads run", "tool": "connect_account", "primary": True}
PAGE = {"id": "page", "label": "Give the ads their page", "tool": "update_landing_page", "primary": True}
# A saved check that passed Google Search's rules and only warns on NewsBreak's Terms link and contact (final review B,
# Important 2a): the page's own verdict passes, NewsBreak's own rules do not (landing.passed)
WARNS = {
    "ok": True,
    "url": PAGE_URL,
    "rules": [
        {"id": "reachable", "ok": True, "fix": None},
        {"id": "terms", "ok": None, "fix": landing.TERMS_FIX},
        {"id": "contact", "ok": None, "fix": landing.CONTACT_FIX},
    ],
}


def _state(client, key) -> dict:
    res = call_tool(client, "marketing_state", {}, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def _reader(conn) -> str:
    """A second key for biz_1 that can only read: no manage, no leads."""
    key, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="reader")
    return key


def _read_only_sees(client, reader: str, env: dict) -> None:
    """The same read with a key that can only read: the same sentence, and no step offered that it could not run
    itself (each step's public tool needs manage, or leads)."""
    seen = _state(client, reader)
    assert seen["say"] == env["say"] and seen["next"] == envelope.NONE


def _approve(client, conn, key: str, plan_id: str) -> None:
    """The owner's code relayed by the assistant (test_ads_run.approve): the approve_plan proposal, confirmed."""
    prop = proposals.propose_plan(conn, "biz_1", plan_id)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    args = {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": f"confirm-{plan_id}"}
    confirmed = call_tool(client, "confirm_proposal", args, key=key)
    assert not confirmed["isError"], tool_text(confirmed)


def test_a_business_with_nothing_is_told_to_begin(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    env = _state(client, key)
    d = env["data"]
    assert tuple(d) == tools_state.STATE_KEYS
    assert d["surface_version"] == gate.SURFACE_VERSION == "2026-10-15"
    assert d["caller"]["class"] == "business" and d["caller"]["scopes"] == ["read", "manage"]
    assert d["business"] == {"id": "biz_1", "name": None, "trade": None, "claim_state": "claimed", "locale": "en-US"}
    assert (d["consultation"], d["goal"], d["plan"], d["money"]) == (None, None, None, None)
    assert d["channels"] == [] and d["pending"] == [] and d["opportunities"] == [] and d["issues"] == []
    assert d["leads"] == {"awaiting_outcome": 0}
    assert d["changes"] == [] and d["setup"]["checklist"] is None
    # the page before the account (Addendum 4 of the founder's ruling of 2026-10-13: the page comes before the money)
    assert [s["id"] for s in d["setup"]["steps"]] == ["sign_in", "plan", "page", "account", "approval"]
    assert [s["done"] for s in d["setup"]["steps"]] == [True, False, False, False, False]
    assert d["setup"]["ready_to_launch"] is False and d["setup"]["blocked"] is None  # no plan: nothing to launch
    # the welcome (founder, 2026-10-13): who is speaking, what it can do, and what to tell it -- in the owner's words
    assert env["say"] == (
        "Welcome to Admiry \u2014 I'm your marketing assistant. I can build an ad plan for your business, show you the "
        "plan and how your ads are doing. Tell me what the business does and what you want."
    )
    assert not JARGON.search(env["say"]) and "Nothing is set up" not in env["say"]
    assert [a["tool"] for a in env["next"]["actions"]] == ["create_marketing_plan"]
    _read_only_sees(client, _reader(conn), env)  # beginning a plan needs manage


def _picked(client, walk: dict) -> None:
    """The walk's first answer (surface_helpers.ANSWERS): the business on Google Maps, which centres the ads (plan
    2026-10-03-agent-listing-confirm)."""
    qid, value = ANSWERS[0]
    res = call_tool(
        client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": "pick"}, key=walk["key"]
    )
    assert not res["isError"], tool_text(res)


def test_an_open_consultation_comes_back_with_its_question(client, conn, walk_fake):
    walk = started(client, conn)
    env = _state(client, walk["key"])
    c = env["data"]["consultation"]
    # the business on Google Maps is asked about first (plan 2026-10-03-agent-listing-confirm)
    assert c == {"session_id": walk["session_id"], "status": "open", "phase": "intake", "pending_question": "listing"}
    assert env["next"]["kind"] == "question" and env["next"]["question"]["id"] == "listing"
    assert env["say"].startswith(
        "A consultation for Bright Smile Dental is in progress. Is your business one of these on Google Maps?"
    )
    assert env["data"]["business"]["name"] == "Bright Smile Dental"
    assert _state(client, _reader(conn))["next"] == env["next"]  # a question is no action: every key is asked it
    _picked(client, walk)
    env = _state(client, walk["key"])
    assert env["data"]["consultation"]["pending_question"] == "objective"
    assert env["next"]["kind"] == "question" and env["next"]["question"]["id"] == "objective"
    assert env["say"].startswith("A consultation for Bright Smile Dental is in progress. What do you want most")


def test_a_built_plan_shows_its_split_the_goal_and_the_steps_left(client, conn, walk_fake, monkeypatch):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the assistant-designed way is offered
    walk = planned(client, conn)
    env = _state(client, walk["key"])
    d = env["data"]
    assert d["consultation"]["status"] == "planned" and d["consultation"]["phase"] == "proposal"
    assert d["plan"]["id"] == walk["plan_id"] and d["plan"]["status"] == "draft"
    assert d["plan"]["monthly_usd"] == sum(c["monthly_usd"] for c in d["plan"]["split"]) > 0
    assert d["plan"]["split"][0]["label"] == "Google Search"
    # the walk asked for bookings and stated no goal type: the goal counts bookings (walk fixes 1, Task 6)
    assert d["goal"]["type"] == "bookings" and d["goal"]["progress"] == {
        "so_far": 0,
        "month": d["goal"]["progress"]["month"],
        "counted": "booked leads",
    }
    done = {s["id"]: s["done"] for s in d["setup"]["steps"]}
    assert done == {"sign_in": True, "plan": True, "account": False, "page": False, "approval": False}
    assert [s["tool"] for s in d["setup"]["steps"]] == [
        None,
        "get_marketing_plan",
        "update_landing_page",
        "connect_account",
        "launch_marketing_plan",
    ]
    account = d["setup"]["steps"][3]  # its lines are the homepage's own, one or more per channel the plan funds
    assert {c["channel"] for c in account["channels"]} == {"google_search"}
    assert not any(c["done"] for c in account["channels"])
    assert all(
        set(c) == {"channel", "label", "text", "done"} for c in account["channels"]
    )  # no link: the tool gives it
    # the founder's ruling (2026-10-13): no way is chosen yet, so the step is the owner's choice -- never one way: its
    # label names no way, and it carries connect_account's own question and both choices, as the checklist's decide
    # items carry theirs
    assert account["label"] == tools_state.WAY_STEP and not any(
        w in account["label"] for w in ("Customer ID", "prepa", "balance")
    )
    # the founder's ruling (2026-10-13, a real Muse walk): the page comes first -- while the business has no page, the
    # page step carries its question and three ways, and the account step's question waits for it
    assert "ask_owner" not in account and "choices" not in account
    page = d["setup"]["steps"][2]
    assert page["ask_owner"] == launch_check.PAGE_QUESTION and len(page["choices"]) == 3
    amount = plans.usd(d["plan"]["monthly_usd"])
    said = f"The plan for Bright Smile Dental is ready: {amount} a month. {launch_check.PAGE_QUESTION}"
    assert env["say"] == said and not JARGON.search(env["say"])
    assert env["next"] == envelope.actions(*launch_check.page_actions(page["choices"]))
    _read_only_sees(client, _reader(conn), env)
    # the page settled: the account step carries connect_account's own question and both choices, as the checklist's
    # decide items carry theirs, and it is the first thing left -- the question, said after the plan, and its two ways
    # side by side, neither marked first (final review B, minor: not the launch)
    settle_page(conn)
    env = _state(client, walk["key"])
    account = env["data"]["setup"]["steps"][3]
    assert "ask_owner" not in env["data"]["setup"]["steps"][2]
    assert account["ask_owner"] == ads_mode.WAY_QUESTION
    assert account["choices"] == [{"label": c["label"], "args": c["args"]} for c in ads_mode.WAY_CHOICES]
    said = f"The plan for Bright Smile Dental is ready: {amount} a month. {ads_mode.WAY_QUESTION}"
    assert env["say"] == said and not JARGON.search(env["say"])
    assert env["next"] == envelope.actions(*ads_mode.way_actions())
    assert not any("primary" in a for a in env["next"]["actions"])
    _read_only_sees(client, _reader(conn), env)


def test_the_page_comes_first_with_three_ways_none_marked_first(client, conn, walk_fake, monkeypatch):
    """The founder's ruling (2026-10-13, from a real Muse walk): an owner without a page settles the page before Admiry
    asks how the ads run or takes any money. While the latest draft has no page settled -- the checklist's page item
    is on the list (launch_check.page_settled) -- get_marketing_state's `next` is the page question and its three ways,
    in the owner's words, side by side and none marked first: Admiry builds it, the owner's assistant designs it and
    Admiry hosts it, or the website the owner already has; `say` names it after the plan. Each way is
    update_landing_page with arguments it takes as a first call."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the assistant-designed way is offered
    walk = planned(client, conn)
    env = _state(client, walk["key"])
    page = env["data"]["setup"]["steps"][2]
    assert [i["id"] for i in env["data"]["setup"]["checklist"]["items"]] == ["page"]
    assert page["choices"] == [
        {"id": "admiry_builds", "label": "Admiry builds it for me", "args": {"action": "write"}},
        {
            "id": "assistant_designs",
            "label": "My assistant designs it, Admiry hosts it",
            "args": {"action": "upload_site"},
        },
        {
            "id": "own_website",
            "label": "I already have a website",
            "args": {"action": "use_url", "url": "<the owner's page address>"},
        },
    ]
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {
                "id": "admiry_builds",
                "label": "Admiry builds it for me",
                "tool": "update_landing_page",
                "args": {"action": "write"},
            },
            {
                "id": "assistant_designs",
                "label": "My assistant designs it, Admiry hosts it",
                "tool": "update_landing_page",
                "args": {"action": "upload_site"},
            },
            {
                "id": "own_website",
                "label": "I already have a website",
                "tool": "update_landing_page",
                "args": {"action": "use_url", "url": "<the owner's page address>"},
            },
        ],
    }
    question = launch_check.PAGE_QUESTION
    assert env["say"].endswith(f" a month. {question}") and ads_mode.WAY_QUESTION not in env["say"]
    for said in (question, *(c["label"] for c in page["choices"])):
        assert copy.jargon(said) is None and not JARGON.search(said), said
    assert "Admiry" in question and "your assistant" in question and "website you already have" in question


def test_a_page_in_hand_leads_to_publishing_it_not_the_three_ways(client, conn, walk_fake, monkeypatch):
    """Final review, item 1: a page made and not published yet -- here the one page the owner's assistant put on a
    private preview (preview_html) -- is the business's own page still to publish. While the page comes first the page
    step leads to publishing it, never to the three ways again: its ask_owner (and `say`, after the plan) is one
    sentence in the owner's words, and its one choice is the first of the two calls that publish it,
    update_landing_page's "publish" with the preview's token (the owner's yes comes after). The checklist's page item
    and connect_account say the same; a key that only reads hears the sentence and is offered nothing."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)
    args = {"action": "preview_html", "html": GOOD_HTML, "idempotency_key": "in-hand"}
    made = call_tool(client, "update_landing_page", args, key=walk["key"])
    assert not made["isError"], tool_text(made)
    publish = {"action": "publish", "token": data(made)["token"]}
    env = _state(client, walk["key"])
    page = env["data"]["setup"]["steps"][2]
    assert page["done"] is False and page["ask_owner"] == launch_check.PAGE_READY
    assert page["choices"] == [{"id": "publish", "label": "Publish the page", "args": publish}]
    amount = plans.usd(env["data"]["plan"]["monthly_usd"])
    assert env["say"] == f"The plan for Bright Smile Dental is ready: {amount} a month. {launch_check.PAGE_READY}"
    lead = envelope.actions(
        {"id": "publish", "label": "Publish the page", "tool": "update_landing_page", "args": publish}
    )
    assert env["next"] == lead and launch_check.PAGE_QUESTION not in env["say"]
    fix = env["data"]["setup"]["checklist"]["items"][0]["fix"]
    assert (fix["tool"], fix["args"], fix["ask_owner"]) == ("update_landing_page", publish, launch_check.PAGE_READY)
    for said in (launch_check.PAGE_READY, "Publish the page"):
        assert copy.jargon(said) is None and not JARGON.search(said), said
    _read_only_sees(client, _reader(conn), env)
    asked = call_tool(client, "connect_account", {"idempotency_key": "in-hand-c"}, key=walk["key"])
    assert data(asked)["status"] == "page_first" and data(asked)["ask_owner"] == launch_check.PAGE_READY
    assert data(asked)["choices"] == page["choices"] and asked["structuredContent"]["next"] == lead


def test_a_site_under_way_leads_to_its_own_next_step_not_the_three_ways(
    client,
    conn,
    walk_fake,
    monkeypatch,
    dental_library,  # noqa: F811 - the pytest fixture imported above
    renderer,  # noqa: F811 - the pytest fixture imported above
):
    """Final review, item 1: Admiry's own site for a dental practice, its look chosen and built, its logo and office
    photo still to come, is the business's page in hand: the page step says where the site stands in the site's own
    words and leads to its own next step -- the site's view, which carries the link where the owner adds the photos --
    not the three ways. With the photos in, the site is built once more; then it is ready to publish, with its token. A
    read of the plan asks the same."""
    monkeypatch.setenv("AGENT_SITES", "1")
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)
    walk_fake.sites = [_fitted(*DENTAL_WORDS)]
    built = call_tool(
        client,
        "update_landing_page",
        {"action": "build_site", "style": "calm", "idempotency_key": "s1"},
        key=walk["key"],
    )
    assert not built["isError"], tool_text(built)
    preview = data(built)["preview_url"]
    env = _state(client, walk["key"])
    page = env["data"]["setup"]["steps"][2]
    said = (
        f"Your site is ready to look over: {preview}. Your site needs your logo and one photo of your office before it "
        "goes live."
    )
    site = {"id": "site", "label": "Add your logo and photos", "tool": "get_landing_page", "args": {"view": "site"}}
    assert page["ask_owner"] == said and page["choices"] == [site]
    assert env["say"].endswith(f" a month. {said}") and env["next"] == envelope.actions(site)
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])["structuredContent"]
    assert read["say"].endswith(f" {said}") and read["next"] == env["next"]
    _own(conn, "biz_1", "logo")
    _own(conn, "biz_1", "key")
    again = {"id": "build_site", "label": "Build it again", "args": {"action": "build_site"}}
    page = _state(client, walk["key"])["data"]["setup"]["steps"][2]
    assert page["ask_owner"].endswith(" Your photos are in; build the site once more, then publish.")
    assert page["choices"] == [again]
    rebuilt = call_tool(
        client, "update_landing_page", {"action": "build_site", "idempotency_key": "s2"}, key=walk["key"]
    )
    assert not rebuilt["isError"], tool_text(rebuilt)
    env = _state(client, walk["key"])
    publish = {"action": "publish", "token": data(rebuilt)["token"]}
    assert env["data"]["setup"]["steps"][2]["ask_owner"] == launch_check.PAGE_READY
    assert env["next"] == envelope.actions(
        {"id": "publish", "label": "Publish the page", "tool": "update_landing_page", "args": publish}
    )
    assert launch_check.PAGE_QUESTION not in env["say"]


def test_a_way_already_chosen_keeps_the_account_step_as_it_was(client, conn, walk_fake):
    """A business on its way to its own account (the agreement accepted, no link yet) is not asked how the ads run
    again: its account step is said by its own words, the Customer ID to give, and connect_account is offered (the
    page settled: it comes first)."""
    walk = planned(client, conn)
    settle_page(conn)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    env = _state(client, walk["key"])
    account = env["data"]["setup"]["steps"][3]
    assert account["label"] == account["channels"][0]["text"] and "choices" not in account
    amount = plans.usd(env["data"]["plan"]["monthly_usd"])
    said = f"The plan for Bright Smile Dental is ready: {amount} a month. It still needs: {account['label']}."
    assert env["say"] == said and env["next"] == {"kind": "actions", "actions": [CONNECT]}


@pytest.mark.parametrize("unsigned", ["a draft business", "a draft key"])
def test_a_plan_nobody_signed_in_for_is_offered_sign_in_not_launch(client, conn, walk_fake, unsigned):
    """A draft builds a plan but cannot launch it (both launch tools refuse it): the state says to sign in, as recommend
    does. A setup key still live after the business was claimed is a draft key all the same."""
    walk = planned(client, conn, claim_state="draft" if unsigned == "a draft business" else "claimed")
    key = walk["key"]
    if unsigned == "a draft key":
        key, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    env = _state(client, key)
    # which condition fires: the business's claim with a business key, or the key's own class on a claimed business
    assert env["data"]["caller"]["class"] == ("business" if unsigned == "a draft business" else "draft")
    assert env["data"]["business"]["claim_state"] == ("draft" if unsigned == "a draft business" else "claimed")
    amount = plans.usd(env["data"]["plan"]["monthly_usd"])
    assert (
        env["say"] == f"The plan for Bright Smile Dental is ready: {amount} a month. Sign in to keep it and launch it."
    )
    assert env["next"] == {"kind": "actions", "actions": [SIGN_IN]}
    assert "launch_marketing_plan" not in str(env["next"]) and not JARGON.search(env["say"])
    setup = env["data"]["setup"]  # unchanged: its sign-in step and link follow the business's claim, not the key
    if unsigned == "a draft business":
        assert setup["steps"][0]["done"] is False and setup["sign_in_url"] == "http://testserver/connect"
    else:
        assert setup["steps"][0]["done"] is True and "sign_in_url" not in setup


def test_an_open_proposal_names_the_tool_that_confirms_it(client, conn, walk_fake):
    walk = planned(client, conn)
    asked = call_tool(client, "approve_plan", {"plan_id": walk["plan_id"], "idempotency_key": "ap-1"}, key=walk["key"])
    assert not asked["isError"], tool_text(asked)
    pending = _state(client, walk["key"])["data"]["pending"]
    assert [(p["kind"], p["needs"], p["tool"]) for p in pending] == [("approve_plan", "code", "launch_marketing_plan")]
    assert pending[0]["approval_url"].endswith(f"/plan/{walk['plan_id']}") and "proposal_id" not in pending[0]


def test_pending_holds_only_the_launch_request_the_owner_can_still_use(client, conn, walk_fake):
    """Final review B, Important 11: both versions were asked to launch and a budget change made version 2. Version
    1's request is still open, but no code can approve a version a later one replaced: only version 2's is listed."""
    walk = planned(client, conn)
    calls = (
        ("launch_marketing_plan", {"idempotency_key": "ask-1"}),  # version 1 asked to launch
        ("update_marketing_budget", {"idempotency_key": "b1", "level": "conservative"}),  # version 2
        ("launch_marketing_plan", {"idempotency_key": "ask-2"}),  # version 2 asked to launch
    )
    for tool, args in calls:
        res = call_tool(client, tool, args, key=walk["key"])
        assert not res["isError"], tool_text(res)
    d = _state(client, walk["key"])["data"]
    assert d["plan"]["version"] == 2 and len(proposals.list_open(conn, "biz_1")) == 2  # both requests are open
    assert [(p["kind"], p["summary"].split(":")[0]) for p in d["pending"]] == [("approve_plan", "Launch plan v2")]
    assert d["pending"][0]["approval_url"].endswith(f"/plan/{d['plan']['id']}")


def test_pending_holds_no_launch_request_once_the_latest_plan_is_approved(client, conn, walk_fake):
    """A launch request is listed only while the latest plan is a draft. Two requests for one plan (made before a
    second ask answered with the first), the owner approves on their page through one: the other stays open, but
    nothing is left for it to launch, and it is not listed."""
    walk = planned(client, conn)
    first = proposals.propose_plan(conn, "biz_1", walk["plan_id"])
    second = proposals.propose_plan(conn, "biz_1", walk["plan_id"])
    with conn.transaction():  # the owner's own click on the page
        proposals.confirm(conn, "biz_1", first["proposal_id"], code=None, web_owner=True)
    assert proposals.get(conn, "biz_1", second["proposal_id"])["status"] == "proposed"  # the leftover is still open
    d = _state(client, walk["key"])["data"]
    assert d["plan"]["status"] == "approved"
    assert [p for p in d["pending"] if p["kind"] == "approve_plan"] == []


def test_the_state_counts_leads_and_never_names_a_person(client, conn, walk_fake):
    walk = planned(client, conn, scopes=("read", "manage"))  # no leads scope
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    leads.record(conn, page, {"name": "Ana Petrov", "phone": "6505550199", "message": "braces?"}, ("g1", "gclid"))
    res = call_tool(client, "marketing_state", {}, key=walk["key"])
    assert data(res)["leads"] == {"awaiting_outcome": 1}
    assert {"kind": "leads_waiting", "channel": None, "label": None, "text": "1 lead has no outcome yet"} in data(res)[
        "issues"
    ]
    text = str(res)
    assert "Ana" not in text and "6505550199" not in text and "braces" not in text


def test_an_approved_plan_says_where_its_ads_stand_and_the_month_so_far(client, conn, walk_fake):
    walk = planned(client, conn)
    _approve(client, conn, walk["key"], walk["plan_id"])
    ads_run.queue(conn, "biz_1", "google_search", walk["plan_id"], ready=False)  # what a switch that is on leaves
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    leads.record(conn, page, {"name": "Ana Petrov", "phone": "6505550199"}, ("g1", "gclid"))
    env = _state(client, walk["key"])
    d = env["data"]
    assert d["plan"]["status"] == "approved" and d["setup"]["steps"][4]["done"] is True
    assert d["setup"]["ready_to_launch"] is False  # nothing is left to approve
    assert d["setup"]["blocked"] is None  # an approved latest plan: no launch is asked for
    assert [(r["channel"], r["state"]) for r in d["channels"]] == [("google_search", "waiting_account")]
    assert (d["funnel_month"]["spend_usd"], d["funnel_month"]["leads"]) == (0, 1)
    said = f"{d['channels'][0]['sentence']} This month so far: $0 spent, 1 lead. 1 lead waits for an outcome."
    assert env["say"] == said and not JARGON.search(said)
    assert [a["tool"] for a in env["next"]["actions"]] == ["get_leads"]
    assert "Ana" not in str(env) and "6505550199" not in str(env)  # the channel rows count leads, never name one
    no_leads, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="no leads")
    seen = _state(client, no_leads)  # recording outcomes needs leads: still counted, never offered
    assert seen["say"] == said and seen["next"] == envelope.NONE


def test_a_changed_plan_that_waits_for_approval_is_not_hidden_by_the_approved_one(client, conn, walk_fake):
    """The owner changed a running plan: the new version is a draft and the approved one stays in place until the new
    one is approved. The read says so, offers the launch, and the approval step is the latest version's own."""
    walk = planned(client, conn)
    google_ads_link.save(conn, "biz_1", CID, status="active")  # the account linked and the page checked: every step
    registry.save_account(  # before the approval is done
        conn, "biz_1", "google_search", mode="own_account", status="active", external={"customer_id": CID}
    )
    landing.save_check(conn, landing.set_owner_url(conn, "biz_1", PAGE_URL)["id"], CHECK_OK)
    _approve(client, conn, walk["key"], walk["plan_id"])
    approved = _state(client, walk["key"])
    assert [s["done"] for s in approved["data"]["setup"]["steps"]] == [True, True, True, True, True]
    assert approved["data"]["setup"]["ready_to_launch"] is False  # nothing is left to approve
    nothing_runs = "The plan is approved; nothing is running yet. This month so far: $0 spent, 0 leads."
    assert approved["say"] == nothing_runs and approved["next"] == envelope.NONE  # no channel row, no lead waits
    args = {"plan_id": walk["plan_id"], "radius_miles": 10, "idempotency_key": "adj-1"}
    changed = call_tool(client, "adjust", args, key=walk["key"])
    assert not changed["isError"], tool_text(changed)
    env = _state(client, walk["key"])
    d = env["data"]
    assert (d["plan"]["version"], d["plan"]["status"]) == (2, "draft")
    waits = (
        f" A changed version of the plan waits for your approval: {plans.usd(d['plan']['monthly_usd'])} a month."
        " Until then the approved one stays in place."
    )
    assert env["say"] == nothing_runs + waits and not JARGON.search(env["say"])
    assert env["next"] == {"kind": "actions", "actions": [LAUNCH]}
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    assert d["setup"]["ready_to_launch"] is True  # the four steps before it are done: the change can be approved
    assert d["setup"]["blocked"] is None
    draft_key, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    assert _state(client, draft_key)["next"] == {"kind": "actions", "actions": [SIGN_IN]}  # R1's rule holds here too


def test_a_changed_version_the_approval_would_refuse_says_why_and_offers_no_launch(client, conn, google_on):
    """The owner changed a running plan and the change is over the monthly limit: the read says the change waits, and
    why it cannot launch yet (the approval's own refusal), and offers no launch the approval would refuse -- it offers
    the change that fixes the refusal (S0: a refusal's ask, tools_state._ask_next)."""
    key, _approved = seed(conn, secs=consulted(sections()))  # approved, the account linked, the page checked
    plan_id = add_plan(conn, consulted(sections(monthly_usd=1500.0)), status="draft")
    business.set_goal(conn, "biz_1", monthly_limit_usd=1200)
    env = _state(client, key)
    d = env["data"]
    assert (d["plan"]["id"], d["plan"]["status"]) == (plan_id, "draft")
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    assert d["setup"]["ready_to_launch"] is False
    assert d["setup"]["blocked"] == ads_run.OVER_LIMIT == _refusal(conn, plan_id)
    assert env["say"] == (
        "The plan is approved; nothing is running yet. This month so far: $0 spent, 0 leads. A changed version of the "
        "plan waits for your approval: $1,500 a month. Until then the approved one stays in place. It cannot launch "
        f"yet: {ads_run.OVER_LIMIT}."
    )
    first = d["setup"]["checklist"]["items"][0]
    assert (first["id"], first["blocking"], first["fix"]["tool"]) == ("limit", True, "update_marketing_budget")
    assert env["next"]["kind"] == "actions" and env["next"]["actions"][0] == {
        "id": "limit",
        "label": "$40 a day",
        "tool": "update_marketing_budget",
        "args": {"monthly_usd": 1200},
        "primary": True,
    }


@pytest.fixture
def google_on(monkeypatch):
    """Google Search's switch on (validate), as `newsbreak_on` is NewsBreak's: the approval checks a funded place only
    while its switch is on (proposals.precheck_approve_plan), and the suite's default for Google Search is off."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")


def _two_places(conn, *, link: str | None = "active", picture: str | None = None) -> tuple[str, str]:
    """`seed`'s verified business, its page checked, with a draft plan funding Google Search and NewsBreak
    (test_ads_run_channels' two_channels, with a consultation's answers): Google Search ready while its link is
    active (`link`), NewsBreak waiting for funds; `picture` pins NewsBreak's picture on the plan. Returns (key,
    plan_id)."""
    secs = consulted(two_channels())
    if picture is not None:
        secs["creative"]["proposed_image"] = {"newsbreak": picture}
    return seed_plan(conn, secs, status="draft", link=link)


def _refusal(conn, plan_id: str) -> str | None:
    """What the approval's own check (proposals.precheck_approve_plan, before a code is used) says of the plan: None
    when it lets the launch through, else its refusal."""
    try:
        proposals.precheck_approve_plan(conn, "biz_1", {"plan_id": plan_id})
    except ToolError as e:
        return str(e)
    return None


def test_the_account_step_is_done_once_one_place_is_ready(client, conn, newsbreak_on, google_on):
    """D77: a place that is not ready waits and never holds the others back. Both places switched on, Google Search
    linked and NewsBreak waiting for funds: the account step is done, the plan is ready to launch with sign-in, the
    plan and a page that passed, and the approval's own check lets it through."""
    key, plan_id = _two_places(conn)
    d = _state(client, key)["data"]
    account = d["setup"]["steps"][3]
    assert account["id"] == "account" and account["done"] is True
    assert account["label"] == "Connect where the ads run"
    assert [(c["channel"], c["done"]) for c in account["channels"]] == [("google_search", True), ("newsbreak", False)]
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    assert d["setup"]["ready_to_launch"] is True and d["setup"]["blocked"] is None
    assert _refusal(conn, plan_id) is None


def test_the_account_step_waits_while_no_place_is_ready(client, conn, newsbreak_on, google_on):
    key, plan_id = _two_places(conn, link=None)  # Google Search not linked, NewsBreak without funds
    d = _state(client, key)["data"]
    account = d["setup"]["steps"][3]
    assert [(c["channel"], c["done"]) for c in account["channels"]] == [("google_search", False), ("newsbreak", False)]
    # Google Search's way is not chosen yet (no link, no agreement): the step is the owner's choice of way (founder's
    # ruling, 2026-10-13), and Google Search's line names neither way (owner-chooses review, fix round 1, item 4)
    assert account["done"] is False and account["label"] == tools_state.WAY_STEP
    assert account["choices"] == ads_mode.choices()
    assert [c["text"] for c in account["channels"]] == [
        ads_mode.WAY_LINE,
        "Nothing to connect: Admiry runs NewsBreak from your balance",
    ]
    assert d["setup"]["ready_to_launch"] is False
    # every place's wait, one sentence each (M1 follow-up 2, `ads_run._nothing_launches`): Google Search's link, then
    # NewsBreak's funds, whose sentence names its place itself -- Google Search's said, in the state, as the choice of
    # way the link waits for, since no way is chosen yet
    refused = f"Google Search: {NOT_LINKED}. {ads_run.NO_FUNDS.format(label='NewsBreak')}."
    assert _refusal(conn, plan_id) == refused
    assert d["setup"]["blocked"] == f"Google Search: {ads_mode.WAY_LINE}. {ads_run.NO_FUNDS.format(label='NewsBreak')}."


def test_a_ready_place_whose_switch_is_off_leaves_the_step_undone(client, conn, newsbreak_on):
    """The approval checks only the funded places whose switch is on: Google Search is linked but switched off, and
    NewsBreak, switched on, waits for funds. The launch is refused, so the step is not done and reads what NewsBreak
    waits for."""
    key, plan_id = _two_places(conn)
    d = _state(client, key)["data"]
    account = d["setup"]["steps"][3]
    assert [(c["channel"], c["done"]) for c in account["channels"]] == [("google_search", True), ("newsbreak", False)]
    assert account["done"] is False and account["label"] == account["channels"][1]["reason"]
    assert d["setup"]["ready_to_launch"] is False
    assert _refusal(conn, plan_id) == ads_run.NO_FUNDS.format(label="NewsBreak") == d["setup"]["blocked"]


def test_a_place_with_nothing_to_connect_names_what_it_waits_for(client, conn, newsbreak_on):
    """No place ready: the account step's label is the first open line's `reason` when it has one -- a place with
    nothing to connect says what it waits for, never its "Nothing to connect" -- and the state's sentence carries it.
    A line with a link to follow (Google Search's own account) has no `reason` and reads its words (the tests above)."""
    key, _plan_id = seed_plan(conn, consulted(newsbreak_only()), status="draft", link=None)  # NewsBreak alone, no funds
    env = _state(client, key)
    d = env["data"]
    account = d["setup"]["steps"][3]
    (line,) = account["channels"]
    assert (line["channel"], line["done"], line["text"]) == (
        "newsbreak",
        False,
        "Nothing to connect: Admiry runs NewsBreak from your balance",
    )
    assert account["done"] is False
    assert account["label"] == line["reason"] == ads_run.NO_FUNDS.format(label="NewsBreak")
    amount = plans.usd(d["plan"]["monthly_usd"])
    said = f"The plan for Bright Smile Dental is ready: {amount} a month. It still needs: Add funds to start NewsBreak."
    assert env["say"] == said and not JARGON.search(said)
    assert env["next"] == {"kind": "actions", "actions": [CONNECT]}


def test_with_every_place_switched_off_the_step_follows_the_lines(client, conn):
    """Every funded place switched off: the approval checks nothing and nothing would run, so the step reads the lines
    as they are -- done once one is. NewsBreak is listed by the account row the business already has there."""
    key, plan_id = _two_places(conn)
    registry.save_account(conn, "biz_1", "newsbreak", mode="managed", status="none", external={})
    account = _state(client, key)["data"]["setup"]["steps"][3]
    assert [(c["channel"], c["done"]) for c in account["channels"]] == [("google_search", True), ("newsbreak", False)]
    assert account["done"] is True and account["label"] == "Connect where the ads run"
    assert _refusal(conn, plan_id) is None  # nothing is checked


def test_a_place_that_shows_a_picture_carries_its_line(client, conn, newsbreak_on, pictures):
    """M1: a place that shows a picture with the ad (NewsBreak) has the plan's line about it on its setup step; the
    account step's entry for it carries that line, and an entry for a place without a picture has no `image`."""
    key, _plan_id = _two_places(conn, picture="dentists/1.jpg")
    line = next(s["image"] for s in setup.state(conn, config.load(), "biz_1")["steps"] if s["channel"] == "newsbreak")
    assert line == plans.IMAGE_STEP.format(label="NewsBreak")  # the plan's own picture, approved with it
    google, newsbreak = _state(client, key)["data"]["setup"]["steps"][3]["channels"]
    assert (newsbreak["channel"], newsbreak["image"]) == ("newsbreak", line)
    assert google["channel"] == "google_search" and "image" not in google
    assert "url" not in newsbreak  # no link: the tool gives it


def test_a_places_line_carries_what_it_waits_for_and_its_page_fix(client, conn, newsbreak_on, google_on):
    """M1 follow-up 2: a place with nothing to connect carries what it waits for while it is not ready (`reason`), and
    a place not built yet whose own page rules the saved check does not pass carries the page's fix (`page`) -- each
    as the homepage's line for it says it (`setup.state`), and neither with a link."""
    key, _plan_id = _two_places(conn)  # Google Search linked, NewsBreak without funds

    def lines() -> tuple[dict, dict, dict]:
        homepage = {s["channel"]: s for s in setup.state(conn, config.load(), "biz_1")["steps"]}
        google, newsbreak = _state(client, key)["data"]["setup"]["steps"][3]["channels"]
        return homepage["newsbreak"], google, newsbreak

    line, google, newsbreak = lines()
    assert newsbreak["reason"] == line["reason"] == ads_run.NO_FUNDS.format(label="NewsBreak")
    assert "page" not in newsbreak and set(google) == {"channel", "label", "text", "done"}
    funds(conn)
    landing.save_check(conn, landing.current(conn, "biz_1")["id"], WARNS)  # Terms and contact only warnings
    line, google, newsbreak = lines()
    assert newsbreak["page"] == line["page"] == ads_run.PAGE_FIX.format(fix=landing.TERMS_FIX)
    assert "reason" not in newsbreak and "url" not in newsbreak
    assert set(google) == {"channel", "label", "text", "done"}


def test_a_hosted_page_taken_down_leaves_the_page_step_undone(client, conn, google_on, monkeypatch):
    """Final review B, Important 2b: taking the hosted page down keeps its address and its passing check on the row,
    and the launch refuses it (NO_PAGE): the page step reads done only while the page is published."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "pages.test")  # Admiry hosts: the assistant-designed way is offered
    key, plan_id = seed(conn, secs=consulted(sections()), status="draft")  # every step before the approval done
    conn.execute(
        "UPDATE page SET source = 'admiry', hostname = 'bright.pages.test', status = 'unpublished' "
        "WHERE business_id = 'biz_1'"
    )
    env = _state(client, key)
    d = env["data"]
    page = d["setup"]["steps"][2]
    assert (page["id"], page["done"]) == ("page", False) and d["setup"]["steps"][3]["done"] is True
    assert d["setup"]["ready_to_launch"] is False
    assert d["setup"]["blocked"] == ads_run.NO_PAGE == _refusal(conn, plan_id)
    # the first thing left: no page settled (the founder's ruling, 2026-10-13), so its question and three ways
    assert page["ask_owner"] == launch_check.PAGE_QUESTION
    assert env["next"] == envelope.actions(*launch_check.page_actions(page["choices"]))


def test_the_page_step_is_not_done_while_the_page_comes_first(client, conn, newsbreak_on, google_on):
    """Page-first review, item 5: ads built on one place (Google Search) count a page address on file as its page, so
    the page step read done while a second funded place (NewsBreak, nothing built) still had no published page -- the
    page not settled (launch_check.page_first), so the state led to the account while connect_account answered the
    page question. The page step is done only once the page comes first no more: here it is not done, it leads with its
    question, and connect_account asks the same."""
    key, plan_id = _two_places(conn, link=None)  # no way chosen for Google Search, NewsBreak without funds
    campaign(conn, "google_search", plan_id, "live", resources={"campaign": "customers/1234567890/campaigns/1"})
    conn.execute("UPDATE page SET status = 'unpublished'")  # the address stays on file, the page is down
    assert ads_run.page_rows(conn, "biz_1") and landing.current(conn, "biz_1")["url"]
    assert launch_check.page_first(conn, "biz_1")
    env = _state(client, key)
    page = next(s for s in env["data"]["setup"]["steps"] if s["id"] == "page")
    assert page["done"] is False and page["ask_owner"] == launch_check.page_question(page["choices"])
    assert env["say"].endswith(page["ask_owner"])
    assert env["next"] == envelope.actions(*launch_check.page_actions(page["choices"]))
    assert env["data"]["setup"]["ready_to_launch"] is False
    asked = call_tool(client, "connect_account", {"idempotency_key": "loop"}, key=key)
    assert data(asked)["status"] == "page_first" and data(asked)["choices"] == page["choices"]


def test_the_state_and_a_read_of_the_plan_ask_the_page_by_one_predicate(client, conn, walk_fake, monkeypatch):
    """Page-first review, item 4: "the latest plan is a draft and its page is not settled" is spelled once
    (launch_check.page_first, which takes the plan a caller already read): the state's own steps and a read of the plan
    (tools_state.owner_question) ask it with the plan they hold."""
    walk = planned(client, conn)
    plan = plans.latest(conn, "biz_1")
    assert launch_check.page_first(conn, "biz_1", plan) is True
    asked: list[str | None] = []
    real = launch_check.page_first

    def page_first(c, biz, plan=None):
        asked.append(plan["id"] if plan is not None else None)
        return real(c, biz, plan)

    monkeypatch.setattr(launch_check, "page_first", page_first)
    _state(client, walk["key"])
    assert asked and set(asked) == {plan["id"]}
    asked.clear()
    read = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert read["structuredContent"]["next"]["actions"][0]["tool"] == "update_landing_page"
    assert plan["id"] in asked
    settle_page(conn)
    assert real(conn, "biz_1", plans.latest(conn, "biz_1")) is False and real(conn, "biz_1") is False


def test_ads_already_built_need_only_the_pages_address(client, conn, google_on):
    """Re-review B, minor 1: the owner changed a running plan, and the hourly check has found the page failing since.
    Ads already built need only the page's address to take a new version (ads_run._launch_rules): the page step is
    done, the launch's own check lets the change through, the launch is what is offered, and the owner's code launches
    it. Before anything was built the rule was the whole one: the page published, at an address, its check passing."""
    key, plan_id = seed(conn, secs=consulted(sections()))  # verified, linked, its page checked, the plan approved
    landing.save_check(conn, landing.current(conn, "biz_1")["id"], CHECK_FAILED)
    draft = add_plan(conn, consulted(sections(monthly_usd=800.0)), status="draft")
    env = _state(client, key)  # nothing built yet: the failing check holds the page step, and the launch
    assert [s["done"] for s in env["data"]["setup"]["steps"]] == [True, True, False, True, False]
    assert env["data"]["setup"]["blocked"] == _refusal(conn, draft) == ads_run.PAGE_FIX.format(fix=PHONE_FIX)
    assert env["next"] == {"kind": "actions", "actions": [PAGE]}
    campaign(conn, "google_search", plan_id, "live", resources={"campaign": "customers/1234567890/campaigns/1"})
    env = _state(client, key)
    d = env["data"]
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    assert _refusal(conn, draft) is None and d["setup"]["blocked"] is None
    assert d["setup"]["ready_to_launch"] is True
    assert env["next"] == {"kind": "actions", "actions": [LAUNCH]}
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask"}, key=key)
    assert not asked["isError"], tool_text(asked)
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="plan_page")
    go = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=key)
    assert not go["isError"], tool_text(go)
    assert (plans.latest(conn, "biz_1")["id"], plans.latest(conn, "biz_1")["status"]) == (draft, "approved")


def test_a_plan_over_the_monthly_limit_is_not_ready_and_says_why(client, conn, google_on):
    """Every step before the approval done, and the approval's own check refuses (final review B, Important 2c): the
    plan is not ready, `blocked` is the refusal, the sentence says it, and no launch is offered -- the change that
    fixes the refusal is (S0: a refusal's ask, tools_state._ask_next; test_review_launch walks every refusal's)."""
    key, plan_id = seed(conn, secs=consulted(sections()), status="draft")  # $1,000 a month
    business.set_goal(conn, "biz_1", monthly_limit_usd=500)
    env = _state(client, key)
    d = env["data"]
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    assert d["setup"]["ready_to_launch"] is False
    assert d["setup"]["blocked"] == ads_run.OVER_LIMIT == _refusal(conn, plan_id)
    amount = plans.usd(d["plan"]["monthly_usd"])
    said = f"The plan for Bright Smile Dental is ready: {amount} a month. It cannot launch yet: {ads_run.OVER_LIMIT}."
    assert env["say"] == said and not JARGON.search(said)
    (first,) = d["setup"]["checklist"]["items"]
    assert first["fix"]["choices"] == [{"label": "$16 a day", "args": {"monthly_usd": 500}}]
    assert first["fix"]["ask_owner"] == "Which budget: $16 a day?"
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {
                "id": "limit",
                "label": "$16 a day",
                "tool": "update_marketing_budget",
                "args": {"monthly_usd": 500},
                "primary": True,
            }
        ],
    }
    _read_only_sees(client, _reader(conn), env)


def test_a_launch_no_place_could_start_is_not_ready_though_every_step_reads_done(client, conn, google_on, newsbreak_on):
    """Final review B, Important 2a: a business whose Google Search waits for its own account's link while NewsBreak
    is funded (A3c-1: a prepaid business's Google Search is ready in Admiry's own account, so the link stands in for
    the wait) -- whose page passed Google's check and only warns on NewsBreak's Terms link and contact. Every step
    before the approval reads done, but the launch would be refused: the read says so in the refusal's own words, one
    full stop at its end, and offers no launch."""
    key, plan_id = seed_plan(conn, consulted(two_channels()), status="draft", link=None, check=WARNS)
    funds(conn)
    env = _state(client, key)
    d = env["data"]
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    refused = _refusal(conn, plan_id)
    assert refused is not None and d["setup"]["blocked"] == refused and d["setup"]["ready_to_launch"] is False
    amount = plans.usd(d["plan"]["monthly_usd"])
    head = f"The plan for Bright Smile Dental is ready: {amount} a month. It cannot launch yet: "
    assert env["say"] == head + refused.rstrip(".") + "." and ".." not in env["say"]
    assert env["next"] == envelope.NONE


def test_a_business_not_verified_yet_is_offered_the_launch_that_finishes_it(client, conn, google_on):
    """Every step before the approval done and the approval's own check passes, but the business is claimed and not
    verified: asking to launch answers with the page where the owner finishes that, so the launch is offered, and the
    sentence says what it waits for."""
    key, plan_id = seed(conn, secs=consulted(sections()), status="draft")
    conn.execute("UPDATE business SET claim_state = 'claimed' WHERE id = 'biz_1'")
    env = _state(client, key)
    d = env["data"]
    assert [s["done"] for s in d["setup"]["steps"]] == [True, True, True, True, False]
    assert _refusal(conn, plan_id) is None
    assert d["setup"]["ready_to_launch"] is False and d["setup"]["blocked"] == tools_state.NOT_VERIFIED
    amount = plans.usd(d["plan"]["monthly_usd"])
    blocked = tools_state.NOT_VERIFIED
    assert (
        env["say"] == f"The plan for Bright Smile Dental is ready: {amount} a month. It cannot launch yet: {blocked}."
    )
    assert env["next"] == {"kind": "actions", "actions": [LAUNCH]}
    _read_only_sees(client, _reader(conn), env)


def test_next_is_the_first_thing_left_before_the_launch(client, conn, walk_fake, monkeypatch):
    """Final review B, minor: the account linked and the page not set, the page is what is offered -- its question and
    three ways since the founder's ruling of 2026-10-13; with every step done, the launch. A key that can only read
    reads the same sentences and is offered neither."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the assistant-designed way is offered
    walk = planned(client, conn)
    reader = _reader(conn)
    google_ads_link.save(conn, "biz_1", CID, status="active")
    registry.save_account(
        conn, "biz_1", "google_search", mode="own_account", status="active", external={"customer_id": CID}
    )
    env = _state(client, walk["key"])
    amount = plans.usd(env["data"]["plan"]["monthly_usd"])
    assert [s["done"] for s in env["data"]["setup"]["steps"]] == [True, True, False, True, False]
    page_left = launch_check.PAGE_QUESTION
    assert env["say"] == f"The plan for Bright Smile Dental is ready: {amount} a month. {page_left}"
    assert env["next"] == envelope.actions(*launch_check.page_actions(env["data"]["setup"]["steps"][2]["choices"]))
    _read_only_sees(client, reader, env)
    landing.save_check(conn, landing.set_owner_url(conn, "biz_1", PAGE_URL)["id"], CHECK_OK)
    env = _state(client, walk["key"])
    assert env["data"]["setup"]["ready_to_launch"] is True and env["data"]["setup"]["blocked"] is None
    assert env["say"] == f"The plan for Bright Smile Dental is ready and waits for your approval: {amount} a month."
    assert env["next"] == {"kind": "actions", "actions": [LAUNCH]}
    _read_only_sees(client, reader, env)


def test_a_key_with_no_business_is_told_so(client, conn):
    key = business_with_key(conn, cls="platform")
    env = _state(client, key)
    assert env["data"]["business"] is None and env["data"]["caller"]["class"] == "platform"
    assert env["say"] == "This key is not tied to a business."


def test_a_name_stored_as_something_other_than_text_is_never_said(client, conn, walk_fake):
    """A name stored as an object (an assistant's patch could store one before update_business checked the name's
    shape) is no name, in the state and in its sentence, as in business_profile's."""
    walk = started(client, conn)
    _picked(client, walk)  # the question after the business on Google Maps is the one the sentence names
    latest = business.latest_knowledge(conn, "biz_1")[1]
    business.add_knowledge(conn, "biz_1", {**latest, "name": {"en": "Bright Smile"}}, source="owner")
    env = _state(client, walk["key"])
    assert env["data"]["business"]["name"] is None
    assert env["say"].startswith("A consultation for your business is in progress. What do you want most")
    profile = call_tool(client, "business_profile", {}, key=walk["key"])
    assert data(profile)["business"]["name"] is None  # one vetted name in both reads (profile.clean_name)
    assert profile["structuredContent"]["say"].startswith("Here is what I know about your business: ")


def test_a_question_about_a_business_with_nothing_on_file_is_refused_before_the_model(
    client, conn, walk_fake, monkeypatch
):
    """Final review B, minor: no knowledge and no plan, so nothing to answer from. ask refuses in business_profile's
    own sentence before the day's model limit is looked at -- here already reached -- and the model is never asked. A
    business with something on file is answered as before."""
    key = business_with_key(conn, claim_state="claimed")
    limit = auth.LLM_CALLS_PER_DAY
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)
    res = call_tool(client, "ask", {"question": "why Google Search first?"}, key=key)
    assert res["isError"] and tool_text(res) == tools_state.NOTHING_ON_FILE, tool_text(res)
    assert walk_fake.answer_calls == []
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", limit)
    business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental"}, source="owner")
    known = call_tool(client, "ask", {"question": "why Google Search first?"}, key=key)
    assert not known["isError"], tool_text(known)
    assert len(walk_fake.answer_calls) == 1


def test_the_answerer_reads_how_each_place_the_plan_funds_runs_and_the_other_way(client, conn, walk_fake):
    """Walk fixes 1, Task 6: asked how to switch to Admiry running the ads, the answerer had nothing about how a place
    runs. Its material now says, for every place the plan funds, how it runs and the other way it could (`ways`);
    nothing before there is a plan."""
    walk = planned(client, conn)
    question = {"question": "Can Admiry run my Google ads for me instead?"}
    res = call_tool(client, "ask", question, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert walk_fake.answer_calls[-1]["material"]["ways"] == [
        "Google Search: your own Google Ads account; Admiry can also run it from a prepaid balance"
    ]
    other = business_with_key(conn, biz_id="biz_2", claim_state="claimed")
    business.add_knowledge(conn, "biz_2", {"name": "Bright Smile Dental"}, source="owner")  # no plan
    assert not call_tool(client, "ask", question, key=other)["isError"]
    assert walk_fake.answer_calls[-1]["material"]["ways"] is None


def test_the_answerer_reads_how_a_prepaid_businesss_google_search_runs(client, conn, walk_fake, monkeypatch):
    """Walk fixes 1, Task 6 round 2: a business that prepays has its Google Search row in Admiry's way; since A3c-1
    Admiry runs it from the balance (`plans.google_open`) and the answerer reads so -- before it, that it waits, never
    that Admiry runs it from the balance."""
    walk = started(client, conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")  # prepays before the plan is built
    for i, (qid, value) in enumerate(ANSWERS, 1):
        res = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"w-a{i}"}, key=walk["key"]
        )
        assert not res["isError"], tool_text(res)
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert data(built)["cards"]["plan"]["channels"][0]["mode_text"] == plans.MANAGED_TEXT, tool_text(built)
    question = {"question": "Who runs my Google ads?"}
    assert not call_tool(client, "ask", question, key=walk["key"])["isError"]
    assert walk_fake.answer_calls[-1]["material"]["ways"] == ["Google Search: Admiry runs it from your balance"]
    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1
    assert not call_tool(client, "ask", question, key=walk["key"])["isError"]
    assert walk_fake.answer_calls[-1]["material"]["ways"] == [
        "Google Search: waits for Admiry-run accounts, which are coming"
    ]


def test_the_ways_lines_say_how_each_place_runs_and_the_other_way(monkeypatch):
    """`plans.ways`, the one builder `ask` and the e-mail channel's answerer read: a place Admiry runs from the balance
    says so; the owner's own account adds the prepaid way -- open for Google Search since A3c-1 (`plans.google_open`),
    and before it coming, while a prepaid business's Google Search waited for Admiry-run accounts."""
    own = {"platform": "google_search", "label": "Google Search", "mode_text": "your own Google Ads account"}
    prepaid = {**own, "mode_text": plans.MANAGED_TEXT}
    newsbreak = {"platform": "newsbreak", "label": "NewsBreak", "mode_text": plans.MANAGED_TEXT}
    assert plans.ways([own, newsbreak]) == [
        "Google Search: your own Google Ads account; Admiry can also run it from a prepaid balance",
        "NewsBreak: Admiry runs it from your balance",
    ]
    assert plans.ways([prepaid, newsbreak]) == [
        "Google Search: Admiry runs it from your balance",
        "NewsBreak: Admiry runs it from your balance",
    ]
    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1
    assert plans.ways([own, newsbreak]) == [
        "Google Search: your own Google Ads account; Admiry running it from a prepaid balance is coming",
        "NewsBreak: Admiry runs it from your balance",
    ]
    assert plans.ways([prepaid, newsbreak]) == [
        "Google Search: waits for Admiry-run accounts, which are coming",
        "NewsBreak: Admiry runs it from your balance",
    ]


def test_the_answerer_reads_a_placed_address_as_its_address_only(client, conn, monkeypatch):
    """Walk fixes 1, Task 4 round 2: the knowledge in `ask`'s material shows an address the owner placed as the address
    alone -- never the listing's name (a shop's at that address) or its place id."""
    from agent import llm
    from agent.tests.test_ads_run import KNOWLEDGE
    from agent.tests.test_llm import PLACED_LISTING, SEEN_ADDRESS, _prompt, completion
    from agent.tests.test_llm import generator as model

    placed = {**KNOWLEDGE, "website": None, "listing": PLACED_LISTING}
    key, _plan_id = seed(conn, status="draft", secs=consulted(sections()), knowledge=placed)
    sent: list = []
    gen = model(completion('{"text": "Because people search there.", "grounded": true}'), sent=sent)
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    res = call_tool(client, "ask", {"question": "Why Google Search first?"}, key=key)
    assert not res["isError"], tool_text(res)
    prompt = _prompt(sent[0])
    assert SEEN_ADDRESS in prompt and "Joe's Coffee" not in prompt and "ChIJ2" not in prompt


def test_a_sentence_names_the_business_only_when_a_site_or_a_listing_vouches_for_it(client, conn):
    """plans.name_for (the founder's walk of 2026-09-28): a name the model derived from the owner's own words reads as a
    label, not a place, so a sentence says "your business"; the data still holds the name."""
    key = business_with_key(conn, claim_state="claimed")
    business.add_knowledge(
        conn, "biz_1", {"name": "H1B Visa Lawyer", "services": [{"name": "Work visas"}]}, source="owner"
    )
    env = _state(client, key)
    assert env["data"]["business"]["name"] == "H1B Visa Lawyer"
    assert env["say"] == tools_state.WELCOME.format(who="your business")
    profile = call_tool(client, "business_profile", {}, key=key)
    assert data(profile)["business"]["name"] == "H1B Visa Lawyer"
    assert profile["structuredContent"]["say"] == "Here is what I know about your business: 1 service on file."
    found = {"place_id": "ChIJ1", "address": "1 Main St, Austin, TX"}
    business.add_knowledge(conn, "biz_1", {"name": "H1B Visa Lawyer", "listing": found}, source="owner")
    assert _state(client, key)["say"] == tools_state.WELCOME.format(who="your business")  # a guess names no one
    # a Google listing the owner confirmed stands behind the name now
    listed = {"name": "H1B Visa Lawyer", "listing": {**found, "confirmed": True}}
    business.add_knowledge(conn, "biz_1", listed, source="owner")
    assert (
        _state(client, key)["say"]
        == "Welcome to Admiry \u2014 I'm your marketing assistant. I can build an ad plan for H1B Visa Lawyer, show you "
        "the plan and how your ads are doing. Tell me what the business does and what you want."
    )
    profile = call_tool(client, "business_profile", {}, key=key)
    assert profile["structuredContent"]["say"] == "Here is what I know about H1B Visa Lawyer: 0 services on file."


def test_an_open_consultation_with_nothing_left_to_ask_reads_as_ready_and_builds(client, conn, walk_fake):
    """A session still open though its questions are all answered (the questions changed under it) reads as answer
    reads it, ready, and is never an error; recommend builds from it as from a ready one. With a question still open,
    recommend refuses as before."""
    walk = started(client, conn)
    _picked(client, walk)  # the business on Google Maps, first: it centres the ads
    early = call_tool(client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "r0"}, key=walk["key"])
    refused = "3 questions still open (objective, service, customer_value); they need answers first"
    assert early["isError"] and refused in tool_text(early)
    rest = {
        "objective": {"choice": "bookings"},
        "service": {"choice": "svc_invisalign"},
        "customer_value": {"choice": "estimate"},
    }
    conn.execute("UPDATE consult_session SET answers = answers || %s WHERE id = %s", (Jsonb(rest), walk["session_id"]))
    env = _state(client, walk["key"])
    assert env["data"]["consultation"]["status"] == "open" and env["data"]["consultation"]["pending_question"] is None
    assert env["say"] == "The questions are answered; the plan is ready to be built."
    assert [a["tool"] for a in env["next"]["actions"]] == ["get_marketing_plan"]
    built = call_tool(client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "r1"}, key=walk["key"])
    assert not built["isError"], tool_text(built)
    assert consult.get(conn, "biz_1", walk["session_id"])["status"] == "planned"
    assert _state(client, walk["key"])["data"]["plan"]["id"] == data(built)["plan_id"]


def test_recommend_with_one_question_open_says_it_needs_an_answer(client, conn, walk_fake):
    """The refusal's tail agrees with the count: one open question "needs an answer", several "need answers"."""
    walk = started(client, conn)
    _picked(client, walk)  # the business on Google Maps, first: it centres the ads
    two = {"objective": {"choice": "bookings"}, "service": {"choice": "svc_invisalign"}}
    conn.execute("UPDATE consult_session SET answers = answers || %s WHERE id = %s", (Jsonb(two), walk["session_id"]))
    res = call_tool(client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "r0"}, key=walk["key"])
    assert res["isError"] and "1 question still open (customer_value); it needs an answer first" in tool_text(res)


def _paused(conn, plan_id: str, reason: str | None, **marks) -> None:
    """Google Search's row, built and then paused (test_state's rows): by Admiry for `reason`, or by the owner's mark."""
    resources = {"campaign": "customers/1234567890/campaigns/1", **marks}
    campaign(
        conn, "google_search", plan_id, "paused", resources=resources, paused_reason=reason, paused_at=datetime.now(UTC)
    )


@pytest.mark.parametrize(
    ("reason", "marks", "kind"),
    [
        ("page_down", {}, "page_down"),
        ("unreachable", {}, "page_down"),  # either of the page's reasons reads as the page being down
        ("no_funds", {}, "no_funds"),
        (None, {"owner_paused": True}, None),  # the owner's own pause is no issue
        ("month_cap", {}, None),  # Admiry's other pauses stay in their channel rows
    ],
)
def test_admirys_own_pauses_for_the_page_and_the_balance_are_issues(client, conn, reason, marks, kind):
    key, plan_id = seed(conn, secs=consulted(sections()))  # verified, linked, its page checked, the plan approved
    _paused(conn, plan_id, reason, **marks)
    d = _state(client, key)["data"]
    row = d["channels"][0]
    assert (row["channel"], row["state"]) == ("google_search", "paused")
    expected = {"kind": kind, "channel": "google_search", "label": "Google Search", "text": row["sentence"]}
    assert d["issues"] == ([expected] if kind else [])


def test_the_issues_are_the_platforms_own_words_then_admirys_then_the_leads_waiting(client, conn):
    key, plan_id = seed(conn, secs=consulted(sections()))
    _paused(conn, plan_id, "page_down")
    words = "Your ad account is under review. Ads will not run until the review is complete."
    conn.execute(
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text) VALUES (%s, %s, 'verification', %s)",
        ("biz_1", "google_search", words),
    )
    leads.record(conn, landing.current(conn, "biz_1"), {"name": "Ana Petrov"}, ("g1", "gclid"))
    d = _state(client, key)["data"]
    assert d["issues"] == [
        {"kind": "verification", "channel": "google_search", "label": "Google Search", "text": words},
        {
            "kind": "page_down",
            "channel": "google_search",
            "label": "Google Search",
            "text": d["channels"][0]["sentence"],
        },
        {"kind": "leads_waiting", "channel": None, "label": None, "text": "1 lead has no outcome yet"},
    ]


def test_what_google_search_says_on_its_row_is_among_the_issues_each_once(client, conn):
    """Final review B, Important 4: Google's reasons for holding the ads back and the ads it did not approve live on
    the channel row (ads-sync writes them), not in `channel_issue`: each is an issue, in the row's order, an ad as one
    sentence with its headline and Google's topics. An open `channel_issue` row is listed once, also for a place with
    no row of its ads."""
    key, plan_id = seed(conn, secs=consulted(sections()))
    read = {
        "campaign": ["Limited by budget"],
        "ads": [{"topics": [{"topic": "Unsupported claims", "type": "PROHIBITED"}]}, {"topics": []}],
    }
    resources = {"campaign": "customers/1234567890/campaigns/1", "issues": read}
    campaign(conn, "google_search", plan_id, "live", resources=resources)
    review = "Your ad account is under review."
    held = "Your NewsBreak account needs a business document."  # NewsBreak has no row: nothing was built there
    for channel, kind, words in (("google_search", "verification", review), ("newsbreak", "account", held)):
        conn.execute(
            "INSERT INTO channel_issue (business_id, channel, kind, platform_text) VALUES ('biz_1', %s, %s, %s)",
            (channel, kind, words),
        )
    google = {"channel": "google_search", "label": "Google Search"}
    ad = 'Google Search needs a change to one of your ads ("Invisalign in Palo Alto")'
    assert _state(client, key)["data"]["issues"] == [
        {"kind": "verification", **google, "text": review},
        {"kind": "serving", **google, "text": "Limited by budget"},
        {"kind": "disapproval", **google, "text": f"{ad}: Unsupported claims."},
        {"kind": "disapproval", **google, "text": f"{ad}."},  # Google named no topic
        {"kind": "account", "channel": "newsbreak", "label": "NewsBreak", "text": held},
    ]
    no_headline = {"kind": "disapproval", "headline": None, "topics": []}
    assert tools_state.issue_text("NewsBreak", no_headline) == "NewsBreak needs a change to one of your ads."


def _empty_balance(conn) -> None:
    """The prepaid business of final review B's probe (Important 3): its balance spent to $0, then spend-guard's own
    run, which flags the business (`business.paused_reason`) and pauses no row."""
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=5000, ref="pi_0", created_by="stripe")
    wallet.add_entry(conn, "biz_1", kind="spend", amount_cents=-5000, ref="newsbreak:2026-10-01", created_by="job")
    jobs.spend_guard(conn, 0, 1)
    assert business.row(conn, "biz_1")["paused_reason"] == "no_funds"


def test_an_empty_balance_is_an_issue_until_a_top_up_lifts_it(client, conn, newsbreak_on):
    """The balance ran out while NewsBreak's ads run: the issue says so, asks for funds and says the ads Admiry runs
    from the balance are paused -- spec 2026-09-29 D94, which spend-guard's funds stop carries out since A3c-1 (M1
    only flagged the business). A top-up lifts the flag, and the issue with it."""
    key, plan_id = seed(conn, secs=consulted(newsbreak_only()), link=None)
    campaign(conn, "newsbreak", plan_id, "live", resources={"campaign": "1000002"})
    _empty_balance(conn)
    d = _state(client, key)["data"]
    empty = {"kind": "no_funds", "channel": None, "label": None, "text": tools_state.NO_FUNDS_ISSUE}
    assert d["issues"][0] == empty and "add funds" in empty["text"].lower() and "paused" in empty["text"]
    fund(conn, cents=5000, session_id="cs_2", payment_intent="pi_2")  # the webhook's whole effect: it lifts the flag
    assert business.row(conn, "biz_1")["paused_reason"] is None
    assert _state(client, key)["data"]["issues"] == []


def test_the_empty_balance_comes_after_the_platforms_words_and_before_admirys_pauses(client, conn, newsbreak_on):
    key, plan_id = seed(conn, secs=consulted(newsbreak_only()), link=None)
    now = datetime.now(UTC)
    campaign(
        conn,
        "newsbreak",
        plan_id,
        "paused",
        resources={"campaign": "1000002"},
        paused_reason="page_down",
        paused_at=now,
    )
    words = "Your NewsBreak account needs a business document."
    conn.execute(
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text) "
        "VALUES ('biz_1', 'newsbreak', 'account', %s)",
        (words,),
    )
    leads.record(conn, landing.current(conn, "biz_1"), {"name": "Ana Petrov"}, ("g1", "gclid"))
    _empty_balance(conn)
    d = _state(client, key)["data"]
    assert [(i["kind"], i["text"]) for i in d["issues"]] == [
        ("account", words),
        ("no_funds", tools_state.NO_FUNDS_ISSUE),
        ("page_down", d["channels"][0]["sentence"]),
        ("leads_waiting", "1 lead has no outcome yet"),
    ]


def test_a_pause_is_said_only_with_the_sentence_of_a_row_that_reads_paused(conn):
    """A8: the raw rows are read again after the state's own; a pause that commits between the two reads is never
    paired with the sentence of a row that read otherwise -- the next read says it."""
    _key, plan_id = seed(conn, secs=consulted(sections()))
    _paused(conn, plan_id, "page_down")
    paused = state.build(conn, config.load(), "biz_1").channels[0]
    assert paused.state == "paused"
    assert [i["kind"] for i in tools_state._paused_by_admiry(conn, "biz_1", [paused])] == ["page_down"]
    earlier = paused.model_copy(update={"state": "live", "sentence": "Google Search: live."})  # read before the pause
    assert tools_state._paused_by_admiry(conn, "biz_1", [earlier]) == []


def test_the_goal_says_whose_monthly_limit_it_is(client, conn, walk_fake):
    """Final review B, minor: a limit Admiry set as the plan's ceiling (the owner named no budget) reads "system"; once
    the owner sets one, "owner"."""
    walk_fake.knowledge.goal_hint.monthly_budget_usd = None  # no budget in the owner's words
    walk = planned(client, conn)
    goal = _state(client, walk["key"])["data"]["goal"]
    assert goal["monthly_limit_usd"] is not None and goal["limit_source"] == "system"
    args = {"changes": {"monthly_limit_usd": 2500}, "idempotency_key": "g1"}
    changed = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    assert not changed["isError"], tool_text(changed)
    goal = _state(client, walk["key"])["data"]["goal"]
    assert (goal["monthly_limit_usd"], goal["limit_source"]) == (2500.0, "owner")


# ---------------------------------------------------------------- spec 2026-10-11 §3: the checklist on the public surface

FIX_KEYS = {"tool", "args", "ask_owner", "label"}


def test_the_checklist_lists_each_item_with_its_fix(client, conn):
    key, _ = seed(conn, status="draft", secs=consulted(sections(headlines=["Invisalign in Palo Alto"])))
    _knowledge(conn, phone=None)
    _check_failed(conn)
    env = _state(client, key)
    chk = env["data"]["setup"]["checklist"]
    assert chk["plan_id"] == plans.latest(conn, "biz_1")["id"] and chk["ready"] is False
    assert [i["id"] for i in chk["items"]] == ["write_copy", "phone", "page:phone", "page:tracking"]
    copy, phone, page = chk["items"][:3]
    assert set(copy) == {"id", "place", "problem", "suggestion", "blocking", "fix"} and set(copy["fix"]) == FIX_KEYS
    assert (copy["place"], copy["problem"], copy["blocking"]) == ("Google Search", TOO_LITTLE_COPY, True)
    assert copy["fix"] == {
        "tool": "update_marketing_plan",
        "args": {"message": blockers.COPY_INSTRUCTION},
        "ask_owner": None,
        "label": "Write the ad text",
    }
    assert phone["fix"] == {
        "tool": "update_business_profile",
        "args": {"patch": {"locations": [{"phone": "<the number customers call>"}]}},
        "ask_owner": "What phone number should people call?",
        "label": "Give the phone number",
    }
    assert page["problem"] == ads_run.PAGE_FIX.format(fix=PHONE_FIX)
    assert page["fix"] == {
        "tool": "get_landing_page",
        "args": {"view": "check"},
        "ask_owner": None,
        "label": "Check again",
    }
    for item in chk["items"]:
        assert not JARGON.search(item["problem"]) and not (item["suggestion"] and JARGON.search(item["suggestion"]))
    # the checklist's first blocking item's fix, as one action (D8) -- read via _fix_next directly: this scenario's
    # page check just failed, so the page *step* (setup.steps) is itself undone, and the account/page steps still
    # lead env["next"] while undone, as before (D8; test_a_decide_items_choices_are_next_s_actions_and_the_page_asks_
    # which_way exercises that same rule) -- not a contradiction, just a different item leading at the top level
    assert tools_state._fix_next(chk["items"][0]) == {
        "kind": "actions",
        "actions": [
            {
                "id": "write_copy",
                "label": "Write the ad text",
                "tool": "update_marketing_plan",
                "args": {"message": blockers.COPY_INSTRUCTION},
                "primary": True,
            }
        ],
    }
    assert env["next"] == {"kind": "actions", "actions": [PAGE]}
    assert env["say"].endswith("It still needs: A page for the ads that passes its check.")
    _read_only_sees(client, _reader(conn), env)


def test_an_ask_item_leads_next_as_a_question(client, conn):
    key, _ = seed(conn, status="draft", secs=consulted(sections()))
    _knowledge(conn, phone=None)
    env = _state(client, key)
    assert env["next"] == envelope.question(
        id="phone",
        index=1,
        count=1,
        prompt="What phone number should people call?",
        input="text",
        options=[],
        placeholder=plan_review.PLACEHOLDERS["phone"],
        tool="update_business_profile",
    )
    assert _state(client, _reader(conn))["next"] == env["next"]  # a question is asked of every key


def test_a_decide_items_choices_are_next_s_actions_and_the_page_asks_which_way(client, conn, monkeypatch):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the assistant-designed way is offered
    key, _ = seed(conn, status="draft", secs=consulted(sections()), page=False)
    env = _state(client, key)
    (item,) = env["data"]["setup"]["checklist"]["items"]
    assert item["id"] == "page" and item["fix"]["tool"] == "update_landing_page"
    # the founder's ruling (2026-10-13): three ways to a page, in the owner's words -- Admiry builds it (here the page
    # Admiry writes: Admiry's own sites are off), the owner's assistant designs it and Admiry hosts it, or the website
    # the owner already has
    assert item["fix"]["choices"] == [
        {"id": "admiry_builds", "label": "Admiry builds it for me", "args": {"action": "write"}},
        {
            "id": "assistant_designs",
            "label": "My assistant designs it, Admiry hosts it",
            "args": {"action": "upload_site"},
        },
        {
            "id": "own_website",
            "label": "I already have a website",
            "args": {"action": "use_url", "url": "<the owner's page address>"},
        },
    ]
    assert item["fix"]["args"] == {"action": "write"} and item["fix"]["ask_owner"] == launch_check.PAGE_QUESTION
    # no page settled: the page question leads `next`, its three ways side by side, none marked first
    assert env["next"] == envelope.actions(*launch_check.page_actions(item["fix"]["choices"]))
    assert [a["id"] for a in env["next"]["actions"]] == ["admiry_builds", "assistant_designs", "own_website"]
    assert not any("primary" in a for a in env["next"]["actions"])


def test_an_item_no_tool_fixes_has_a_null_fix_though_the_account_step_still_leads_env_next(client, conn):
    secs = consulted(sections())
    secs["ads"]["channels"] = [
        {"channel": "nextdoor", "monthly_usd": 200.0, "headlines": [], "descriptions": [], "themes": []}
    ]
    key, _ = seed(conn, status="draft", secs=secs)
    env = _state(client, key)
    (item,) = env["data"]["setup"]["checklist"]["items"]
    assert item["id"] == "other" and item["fix"] is None and item["blocking"] is True
    # an item no tool fixes gives _fix_next NONE -- here the account step is itself undone too (nextdoor is not a
    # known channel: setup.state has nothing to connect through it), and the steps still lead env["next"] while
    # undone, as before (D8); test_the_first_blocking_item_with_no_fix_leaves_next_none_once_the_steps_are_done
    # proves the end-to-end "next is none" case this test's old name promised but did not, by itself, assert
    assert tools_state._fix_next(item) == envelope.NONE
    assert env["next"] == {"kind": "actions", "actions": [CONNECT]}


# ---------------------------------------------------------------- task review (fix round 1): end-to-end, steps done


def test_next_is_the_checklist_fix_once_the_account_and_page_steps_are_done(client, conn):
    """The same scenario as test_the_checklist_lists_each_item_with_its_fix, without `_check_failed`: the page passes
    (CHECK_OK), so the account and page steps are both done and the checklist's first blocking item (`write_copy`)
    leads `env["next"]` directly, through `get_marketing_state` -- not a step's action standing in for it."""
    key, _ = seed(conn, status="draft", secs=consulted(sections(headlines=["Invisalign in Palo Alto"])))
    _knowledge(conn, phone=None)
    env = _state(client, key)
    chk = env["data"]["setup"]["checklist"]
    assert [i["id"] for i in chk["items"]] == ["write_copy", "phone"]
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {
                "id": "write_copy",
                "label": "Write the ad text",
                "tool": "update_marketing_plan",
                "args": {"message": blockers.COPY_INSTRUCTION},
                "primary": True,
            }
        ],
    }
    assert env["say"].endswith(f"It cannot launch yet: {TOO_LITTLE_COPY}.")


def test_the_first_blocking_item_with_no_fix_leaves_next_none_once_the_steps_are_done(client, conn):
    """Google Search alone is fully compliant (account and page steps both done); Nextdoor, funded beside it, is not
    a place Admiry can start ads on itself, so its own plan-wide refusal (`channel` None: always blocking, R9) is
    the list's only, first item, with no fix -- and, with both steps done, `env["next"]` is genuinely NONE, end to
    end, unlike test_an_item_no_tool_fixes_has_a_null_fix_though_the_account_step_still_leads_env_next above."""
    secs = consulted(sections())
    secs["ads"]["channels"].append(
        {"channel": "nextdoor", "monthly_usd": 200.0, "headlines": [], "descriptions": [], "themes": []}
    )
    key, _ = seed(conn, status="draft", secs=secs)
    env = _state(client, key)
    chk = env["data"]["setup"]["checklist"]
    assert [i["id"] for i in chk["items"]] == ["other"]
    (item,) = chk["items"]
    assert item["fix"] is None and item["blocking"] is True
    assert env["next"] == envelope.NONE
    assert env["say"].endswith(f"It cannot launch yet: {ads_run.REPORT_ONLY.format(label='Nextdoor')}.")


# ---------------------------------------------------------------- task review (fix round 1): ruling H4


def test_say_names_the_checklists_own_problem_even_when_the_approval_finds_a_different_one(client, conn):
    """Ruling H4: `say` and `next` used to be able to disagree -- `say` could name the approval's own refusal
    (`blocked`) while `next` offered the checklist's first blocking item's fix for a DIFFERENT problem. Here the
    business is not verified (NOT_VERIFIED, from the approval's own check) while the checklist's first item is the
    ad text (TOO_LITTLE_COPY, unrelated); `say` now names the same problem `next` fixes."""
    key, _ = seed(conn, status="draft", secs=consulted(sections(headlines=["Invisalign in Palo Alto"])))
    conn.execute("UPDATE business SET claim_state = 'claimed' WHERE id = 'biz_1'")
    env = _state(client, key)
    chk = env["data"]["setup"]["checklist"]
    assert chk["items"][0]["id"] == "write_copy"
    assert env["data"]["setup"]["blocked"] == tools_state.NOT_VERIFIED  # the approval's own, different problem
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {
                "id": "write_copy",
                "label": "Write the ad text",
                "tool": "update_marketing_plan",
                "args": {"message": blockers.COPY_INSTRUCTION},
                "primary": True,
            }
        ],
    }
    assert env["say"].endswith(f"It cannot launch yet: {TOO_LITTLE_COPY}.") and "verified" not in env["say"]


def test_say_on_an_approved_plans_newer_draft_names_the_checklists_problem_too(client, conn):
    """Ruling H4, fix round 2: the same disagreement, on the OTHER branch that builds `say` -- `_next_step`'s own,
    for an approved plan's newer draft (not `_launch_left` directly, which test_say_names_the_checklists_own_problem_
    even_when_the_approval_finds_a_different_one above already covers). An earlier version is approved; the newer
    draft's first blocking item is the ad text (TOO_LITTLE_COPY), while the approval's own check finds a different
    problem (NOT_VERIFIED, the business not verified) -- `say` names the ad text, the same problem `next` fixes."""
    key, _plan_id = seed(conn, secs=consulted(sections()))  # approved: the account linked, the page checked
    draft_id = add_plan(conn, consulted(sections(headlines=["Invisalign in Palo Alto"])), status="draft")
    conn.execute("UPDATE business SET claim_state = 'claimed' WHERE id = 'biz_1'")  # not verified
    env = _state(client, key)
    d = env["data"]
    assert (d["plan"]["id"], d["plan"]["status"]) == (draft_id, "draft")
    assert d["setup"]["checklist"]["items"][0]["id"] == "write_copy"
    assert d["setup"]["blocked"] == tools_state.NOT_VERIFIED  # the approval's own, different problem
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {
                "id": "write_copy",
                "label": "Write the ad text",
                "tool": "update_marketing_plan",
                "args": {"message": blockers.COPY_INSTRUCTION},
                "primary": True,
            }
        ],
    }
    assert env["say"].endswith(f"It cannot launch yet: {TOO_LITTLE_COPY}.") and "verified" not in env["say"]


def test_cannot_launch_keeps_the_problems_own_end_mark():
    """Ruling H4: a blind `rstrip('.') + '.'` turned "Why?" into "Why?." -- `_cannot_launch` keeps the problem's own
    end mark instead, as launch_check.note_text does, and adds a "." only when the problem has none of its own."""
    assert tools_state._cannot_launch("Change the plan first") == "It cannot launch yet: Change the plan first."
    assert tools_state._cannot_launch("Change the plan first.") == "It cannot launch yet: Change the plan first."
    assert (
        tools_state._cannot_launch("Why did you pick this number?")
        == "It cannot launch yet: Why did you pick this number?"
    )
    assert tools_state._cannot_launch("Fix this now!") == "It cannot launch yet: Fix this now!"


def test_a_ready_draft_has_an_empty_list_and_the_launch(client, conn):
    key, _ = seed(conn, status="draft", secs=consulted(sections()))
    env = _state(client, key)
    assert env["data"]["setup"]["checklist"] == {
        "plan_id": plans.latest(conn, "biz_1")["id"],
        "ready": True,
        "items": [],
    }
    assert env["next"] == {"kind": "actions", "actions": [LAUNCH]}


def test_the_money_and_the_account_stay_with_the_steps(client, conn, google_on):
    key, _plan_id = seed(conn, status="draft", secs=consulted(sections()), link=None)
    env = _state(client, key)
    # no way chosen yet (no link, no agreement): the launch's own refusal would pick one ("since your ads run in your own
    # account"), and Google Search's line too ("Give Admiry your 10-digit Customer ID"); the state says the neutral line
    # in their place (owner-chooses review, fix round 1, item 4), in the owner's words
    setup_view = env["data"]["setup"]
    assert setup_view["checklist"]["items"] == [] and setup_view["blocked"] == ads_mode.WAY_LINE
    assert [c["text"] for c in setup_view["steps"][3]["channels"]] == [ads_mode.WAY_LINE]
    assert copy.jargon(ads_mode.WAY_LINE) is None and not JARGON.search(ads_mode.WAY_LINE)
    assert "prepaid balance" in ads_mode.WAY_LINE and "your own Google Ads account" in ads_mode.WAY_LINE
    # the account step leads, as before -- and with no way chosen yet it is the owner's choice of way (founder's ruling,
    # 2026-10-13), both side by side, never one
    assert env["next"] == envelope.actions(*ads_mode.way_actions())


def test_once_a_way_is_chosen_the_account_step_says_what_it_said(client, conn, google_on):
    """Owner-chooses review, fix round 1 (item 4): the agreement accepted on the way to the owner's own account is a way
    chosen (ads_mode.chosen) -- the launch's refusal, Google Search's line and the step's label say what they said before
    the founder's ruling, and the step leads as it led: connect_account, with no arguments."""
    key, _plan_id = seed(conn, status="draft", secs=consulted(sections()), link=None)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    env = _state(client, key)
    setup_view = env["data"]["setup"]
    account = setup_view["steps"][3]
    assert setup_view["blocked"] == NOT_LINKED
    assert [c["text"] for c in account["channels"]] == [CONNECT_OWN_ACCOUNT]
    assert account["label"] == CONNECT_OWN_ACCOUNT and "choices" not in account
    assert env["next"] == tools_state.CONNECT


def test_no_list_in_review_or_approved_and_a_returned_note_is_an_item(client, conn):
    key, plan_id = seed(conn, status="draft", secs=consulted(sections()))
    assert _state(client, key)["data"]["setup"]["checklist"]["ready"] is True
    review_id = launch_review.submit(conn, "biz_1", plan_id, approval={"kind": "web"})["review_id"]
    assert _state(client, key)["data"]["setup"]["checklist"] is None
    launch_review.decide(
        conn,
        review_id,
        state="returned",
        by="reviewer",
        note="Fix the page, then check it again.",
        resolution="check_page",
    )
    chk = _state(client, key)["data"]["setup"]["checklist"]
    (item,) = chk["items"]
    assert (item["id"], item["blocking"], chk["ready"]) == ("review_note", False, True)
    assert item["fix"] == {
        "tool": "get_landing_page",
        "args": {"view": "check"},
        "ask_owner": None,
        "label": "Check my page now",
    }
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (plan_id,))
    assert _state(client, key)["data"]["setup"]["checklist"] is None


def test_changes_list_what_admiry_removed_for_the_latest_version_and_say_mentions_it(client, conn):
    key, plan_id = seed(conn, status="approved", secs=consulted(sections()))
    found = RefusalDetail(
        code="policyFindingError: POLICY_FINDING",
        operation=5,
        kind="headline",
        position=1,
        field="mutate_operations[5].x",
        topic="TRADEMARKS_IN_AD_TEXT",
        type="PROHIBITED",
        exemptible=False,
        certificate=False,
        text="Same-Week Appointments",
    )
    ads_refusal.record(
        conn,
        business_id="biz_1",
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(found, "ad_text", "dropped")],
    )
    env = _state(client, key)
    (change,) = env["data"]["changes"]
    assert (
        change["what"] == "the headline “Same-Week Appointments”"
        and change["why"] == "a name Google treats as a trademark"
    )
    # privacy pin: never Google's own code, topic or field -- only the owner's words (task review, minor)
    assert all(set(c) == {"what", "why", "when"} for c in env["data"]["changes"])
    assert env["say"].endswith(ads_refusal.say(env["data"]["changes"])) and "POLICY" not in env["say"]
    # another business reads none of it (Review Focus 4)
    other = seed(
        conn, biz="biz_2", cid="2222222222", status="approved", secs=consulted(sections()), email="two@example.com"
    )[0]
    assert _state(client, other)["data"]["changes"] == []
