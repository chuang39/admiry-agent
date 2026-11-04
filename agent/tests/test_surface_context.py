"""The first public tools (spec 2026-10-01 §4.2; plan G1 Task 9): each runs one command and leaves one row with both
names; `where()` reads what a tool needs before it picks its command and writes no row; a command's next actions
come out in the public surface's names."""

import pytest
from psycopg.types.json import Jsonb

from agent import auth, keys, landing, leads, proposals
from agent.surface import actions, gate, registry
from agent.surface.where import NOWHERE, where
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import planned, started


def _rows(conn) -> list[tuple]:
    return [
        (r["tool"], r["surface_tool"]) for r in conn.execute("SELECT tool, surface_tool FROM agent_action ORDER BY id")
    ]


def _key(conn, **kw) -> tuple[str, keys.KeyRecord]:
    """A key for biz_1 (fixtures.business_with_key, claimed) and its record, whose id the api_key and agent_action
    rows refer to."""
    plaintext = business_with_key(conn, claim_state="claimed", **kw)
    return plaintext, keys.lookup(conn, plaintext)


def test_the_state_the_profile_and_the_accounts_each_run_their_command(client, conn, walk_fake):
    walk = planned(client, conn)
    conn.execute("DELETE FROM agent_action")
    state = call_tool(client, "get_marketing_state", {}, key=walk["key"])
    profile = call_tool(client, "get_business_profile", {"include": ["services"]}, key=walk["key"])
    accounts = call_tool(client, "get_connected_accounts", {}, key=walk["key"])
    for res in (state, profile, accounts):
        assert not res["isError"], tool_text(res)
    assert data(state)["plan"]["id"] == walk["plan_id"]
    assert list(data(profile)) == ["services", "knowledge_version"]
    assert data(accounts)["channels"][0]["channel"] == "google_search"
    assert _rows(conn) == [
        ("marketing_state", "get_marketing_state"),
        ("business_profile", "get_business_profile"),
        ("get_connection_status", "get_connected_accounts"),
    ]


def test_performance_and_leads_and_the_two_published_commands(client, conn, walk_fake):
    walk = planned(client, conn)
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    lid = leads.record(conn, page, {"name": "Ana", "phone": "6505550199"}, ("g1", "gclid"))["id"]
    conn.execute("DELETE FROM agent_action")
    perf = call_tool(client, "get_campaign_performance", {"period": "7d"}, key=walk["key"])
    assert not perf["isError"] and data(perf)["period"] == "7d"
    waiting = call_tool(client, "get_leads", {"awaiting_outcome": True}, key=walk["key"])
    assert [x["id"] for x in data(waiting)["leads"]] == [lid] and data(waiting)["awaiting_outcome"] == 1
    won = call_tool(
        client,
        "update_lead",
        {"lead_id": lid, "status": "won", "value_usd": 4200, "idempotency_key": "u1"},
        key=walk["key"],
    )
    assert data(won)["value_usd"] == 4200.0
    added = call_tool(
        client, "add_lead", {"kind": "call", "status": "booked", "idempotency_key": "a1"}, key=walk["key"]
    )
    assert data(added)["source"] == "owner"
    assert _rows(conn) == [
        ("performance", "get_campaign_performance"),
        ("list_leads", "get_leads"),
        ("update_lead", "update_lead"),
        ("add_lead", "add_lead"),
    ]


def test_the_registry_knows_each_tool_and_its_commands():
    by_name = {m.name: m for m in registry.SURFACE}
    assert by_name["get_marketing_state"].commands == ("marketing_state",)
    assert (by_name["get_leads"].cls, by_name["get_leads"].scope) == ("RO", "leads")
    assert by_name["update_lead"].tool is registry.command("update_lead")
    assert {"get_marketing_state", "get_leads", "update_lead", "add_lead"} <= gate.PUBLIC
    assert by_name["get_campaign_performance"].tool.annotations.readOnlyHint is True


def test_where_reads_the_open_consultation_and_writes_no_row(client, conn, walk_fake):
    walk = started(client, conn)
    conn.execute("DELETE FROM agent_action")
    with auth.with_headers({"Authorization": f"Bearer {walk['key']}"}):
        w = where()
    # the first question: which place on Google Maps is the business (plan 2026-10-03-agent-listing-confirm)
    assert (w.business_id, w.key_class, w.plan, w.pending_question) == ("biz_1", "business", None, "listing")
    assert w.session["id"] == walk["session_id"] and w.session["status"] == "open" and w.ready_to_build is False
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_where_says_when_the_plan_can_be_built(client, conn, walk_fake):
    """Ready, or still open with nothing left to ask (its questions changed under it: `recommend` builds from both,
    Task 6's ruling)."""
    walk = started(client, conn)
    rest = {  # every question the walk asks, as the answers stand once the place picked centred the ads
        "listing": {"choice": "gm_1", "raw": "gm_1", "source": "choice"},
        "objective": {"choice": "bookings"},
        "service": {"choice": "svc_invisalign"},
        "customer_value": {"choice": "estimate"},
        "location": {
            "address": "500 University Ave, Palo Alto, CA",
            "lat": 37.4419,
            "lng": -122.143,
            "source": "known_fact",
        },
    }
    conn.execute("UPDATE consult_session SET answers = answers || %s WHERE id = %s", (Jsonb(rest), walk["session_id"]))
    with auth.with_headers({"Authorization": f"Bearer {walk['key']}"}):
        still_open = where()
    assert still_open.session["status"] == "open" and still_open.pending_question is None
    assert still_open.ready_to_build is True
    conn.execute("UPDATE consult_session SET status = 'ready' WHERE id = %s", (walk["session_id"],))
    with auth.with_headers({"Authorization": f"Bearer {walk['key']}"}):
        assert where().ready_to_build is True
    assert NOWHERE.ready_to_build is False


def test_where_finds_the_plan_and_the_proposal_waiting_for_it(client, conn, walk_fake):
    walk = planned(client, conn)
    asked = call_tool(client, "approve_plan", {"plan_id": walk["plan_id"], "idempotency_key": "ap-1"}, key=walk["key"])
    with auth.with_headers({"Authorization": f"Bearer {walk['key']}"}):
        w = where()
    assert w.plan["id"] == walk["plan_id"] and w.session is None and w.pending_question is None
    assert w.launch_proposal["proposal_id"] == data(asked)["proposal_id"]
    assert (
        w.proposal("approve_plan")["proposal_id"] == data(asked)["proposal_id"] and w.proposal("publish_page") is None
    )


def test_where_reads_the_plan_asked_about_and_whether_the_hosted_page_is_up(client, conn, walk_fake):
    """What the owner's yes needs before it answers from an earlier call (Task 18): the plan the call names -- read for
    the caller's own business -- else the latest, with its status; and whether the business's Admiry-hosted page is
    up (published, and Admiry's: an owner's own page is not), and at which version. Nothing is recorded."""
    walk = planned(client, conn)
    other = business_with_key(conn, biz_id="biz_2", claim_state="claimed")
    conn.execute("DELETE FROM agent_action")
    mine = {"Authorization": f"Bearer {walk['key']}"}
    with auth.with_headers(mine):
        latest, named = where(scope="manage"), where(scope="manage", plan_id=walk["plan_id"])
        unknown = where(scope="manage", plan_id="pl_nope")
    assert latest.asked_plan["id"] == named.asked_plan["id"] == walk["plan_id"]
    assert named.asked_plan["status"] == "draft" and unknown.asked_plan is None
    with auth.with_headers({"Authorization": f"Bearer {other}"}):
        assert where(scope="manage", plan_id=walk["plan_id"]).asked_plan is None  # another business's plan
    seen = []
    for status, source in ((None, None), ("published", "hosted"), ("unpublished", "hosted"), ("published", "owner")):
        if status is not None:
            conn.execute(
                "INSERT INTO page (id, business_id, source, url, hostname, version, status) VALUES ('pg_1', 'biz_1', "
                "%s, 'https://bright.business.test/', 'bright.business.test', 4, %s) "
                "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, source = EXCLUDED.source",
                (source, status),
            )
        with auth.with_headers(mine):
            w = where()
        seen.append((w.hosted_page_live, w.hosted_page_version))
    assert seen == [(False, None), (True, 4), (False, None), (False, None)]  # a version only while the page is up
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_where_reads_where_the_ads_run_and_where_the_owner_paused_them(conn):
    """What a pause or a resume sent again is held against (final review A P1): the places the business's ads have a
    row for, and those of them under the owner's pause now -- their mark, or an A3 pause that carries none. A pause
    of Admiry's own is not the owner's. Nothing is recorded."""
    key = business_with_key(conn, claim_state="claimed")
    for channel, reason, resources in (
        ("google_search", None, '{"owner_paused": true}'),
        ("newsbreak", "month_cap", "{}"),
    ):
        conn.execute(
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state, paused_reason, resources) "
            "VALUES ('biz_1', %s, 'pl_1', 'paused', %s, %s)",
            (channel, reason, resources),
        )
    with auth.with_headers({"Authorization": f"Bearer {key}"}):
        w = where(scope="manage")
    assert (w.channels, w.owner_paused) == (("google_search", "newsbreak"), ("google_search",))
    conn.execute("UPDATE channel_campaign SET paused_reason = NULL, resources = '{}' WHERE channel = 'newsbreak'")
    with auth.with_headers({"Authorization": f"Bearer {key}"}):
        assert where(scope="manage").owner_paused == ("google_search", "newsbreak")  # an A3 pause: the owner's
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_where_finds_the_publish_proposal_of_the_page_that_was_asked_about(conn):
    """Two pages wait for the owner's yes. The one a call names is the one found, never merely the newest: the
    owner's yes confirms the page the owner was shown (spec §4.4)."""
    key = business_with_key(conn, claim_state="claimed")
    waiting = {"cls": "nonspend", "kind": "publish_page", "what_changes": []}
    first = proposals.create(
        conn,
        "biz_1",
        payload={"token": "pv_one", "hostname": "bright.example"},
        summary="Publish one",
        approval_url="https://agent.example/p/pv_one",
        **waiting,
    )
    second = proposals.create(
        conn,
        "biz_1",
        payload={"version": 3, "hostname": "bright.example"},
        summary="Republish version 3",
        approval_url="https://bright.example/",
        **waiting,
    )
    with auth.with_headers({"Authorization": f"Bearer {key}"}):
        assert where(publish={"token": "pv_one"}).publish_proposal["proposal_id"] == first["proposal_id"]
        assert where(publish={"version": 3}).publish_proposal["proposal_id"] == second["proposal_id"]
        assert where(publish={"token": "pv_other"}).publish_proposal is None
        assert where(publish={"version": 4}).publish_proposal is None
        named_none = where()
    assert named_none.publish_proposal is None and len(named_none.proposals) == 2
    assert named_none.proposal("publish_page")["proposal_id"] == second["proposal_id"]  # the newest, by kind


def test_where_is_nowhere_without_a_key_or_with_a_dead_one(conn):
    business_with_key(conn)
    with auth.with_headers({}):
        assert where() is NOWHERE
    with auth.with_headers({"Authorization": "Bearer ak_unknown"}):
        assert where() is NOWHERE


def test_where_is_nowhere_for_every_caller_it_cannot_resolve(conn):
    """A revoked key, an expired one, one without the read scope, and a key whose business is not active: each is
    NOWHERE, and none leaves a row (the command the tool then runs refuses, or not, under its own scope)."""
    revoked, rec = _key(conn)
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE id = %s", (rec.id,))
    expired, rec = _key(conn)
    conn.execute("UPDATE api_key SET expires_at = now() - interval '1 minute' WHERE id = %s", (rec.id,))
    leads_only, _ = _key(conn, scopes=("leads",))
    for key in (revoked, expired, leads_only):
        with auth.with_headers({"Authorization": f"Bearer {key}"}):
            assert where() is NOWHERE, key
    alive, _ = _key(conn)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_1'")
    with auth.with_headers({"Authorization": f"Bearer {alive}"}):
        assert where() is NOWHERE
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_where_raises_a_rate_limit(conn):
    """The one refusal raised from where(), exactly as run() raises it, unrecorded: the per-minute limit, tripped the
    way test_auth trips it."""
    plaintext, rec = _key(conn)
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, api_key_id, tool, args_hash, ts) "
        "SELECT 'biz_1', %s, %s, 'whoami', 'h', now() - (g * interval '1 millisecond') FROM generate_series(1, 60) g",
        (f"key:{rec.id}", rec.id),
    )
    with auth.with_headers({"Authorization": f"Bearer {plaintext}"}), pytest.raises(auth.RateLimited):
        where()
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 60


def test_where_gives_a_key_bound_to_no_business_its_class(conn):
    """A platform key is resolved (it passes every scope) but names no business: business_id None, its key_class."""
    key = business_with_key(conn, cls="platform")
    with auth.with_headers({"Authorization": f"Bearer {key}"}):
        w = where()
    assert w is not NOWHERE and (w.business_id, w.key_class) == (None, "platform")
    assert (w.session, w.plan, w.proposals, w.launch_proposal, w.publish_proposal) == (None, None, (), None, None)


def test_a_publish_version_that_is_not_an_integer_finds_nothing(conn):
    """A bool (True == 1 in Python), a string and a float are not a page's version: no error and no match, though
    proposals for versions 1 and 3 wait."""
    key = business_with_key(conn, claim_state="claimed")
    for version in (1, 3):
        proposals.create(
            conn,
            "biz_1",
            cls="nonspend",
            kind="publish_page",
            payload={"version": version, "hostname": "bright.example"},
            summary=f"Republish version {version}",
            what_changes=[],
            approval_url="https://bright.example/",
        )
    with auth.with_headers({"Authorization": f"Bearer {key}"}):
        assert where(publish={"version": 1}).publish_proposal is not None  # the proposals are there
        for odd in (True, "3", 3.0):
            assert where(publish={"version": odd}).publish_proposal is None, odd


def test_a_commands_actions_come_out_in_public_names():
    env = {
        "data": {},
        "say": "x",
        "next": {
            "kind": "actions",
            "actions": [
                {
                    "id": "link_own_account",
                    "label": "I have a Google Ads account",
                    "tool": "connect_channel",
                    "primary": True,
                },
                {"id": "prepay", "label": "Admiry runs it, I prepay", "tool": "request_payment_link"},
                {"id": "adjust", "label": "Adjust the budget", "tool": "adjust"},
            ],
        },
    }
    out = actions.translate(env)["next"]["actions"]
    assert [(a["tool"], a.get("args")) for a in out] == [
        ("connect_account", None),
        ("get_billing", {"view": "topup_link"}),
        ("update_marketing_plan", None),
    ]
    assert (
        out[0]["primary"] is True and env["next"]["actions"][0]["tool"] == "connect_channel"
    )  # the input is untouched
    question = {"data": {}, "say": "x", "next": {"kind": "question", "question": {"id": "objective"}}}
    assert actions.translate(question) is question


def test_the_two_ways_a_plan_offers_come_out_as_the_owners_choice():
    """The founder's ruling (2026-10-13): the owner chooses how the ads run. The two ways a plan answer offers in the
    home's names -- Admiry running the ads (MANAGED_ACTION, first on the home) and the own account -- reach an
    assistant as connect_account's own question's two choices, in their place, side by side and neither marked first;
    the other actions come out as before, and the command's own envelope is untouched."""
    from agent import ads_mode, tools_consult

    home = [tools_consult.MANAGED_ACTION, tools_consult.OWN_ACCOUNT_ACTION, tools_consult.ADJUST_ACTION]
    env = {"data": {}, "say": "x", "next": {"kind": "actions", "actions": [dict(a) for a in home]}}
    out = actions.translate(env)["next"]["actions"]
    assert out == [
        *ads_mode.way_actions(),
        {"id": "adjust", "label": "Adjust the budget", "tool": "update_marketing_plan"},
    ]
    assert [a["args"] for a in out[:2]] == [{"mode": "managed"}, {"account_id": "<Customer ID>"}]
    assert not any("primary" in a for a in out) and env["next"]["actions"] == [dict(a) for a in home]


def test_a_business_that_chose_its_way_gets_one_connect_and_is_asked_only_beside_both_ways():
    """Owner-chooses review, fix round 1 (item 1): for a business that has already chosen how its ads run (`chosen`,
    ads_mode.chosen as the caller's business reads it), the two ways come out as one connect_account -- the own
    account's, with no arguments, where the first of the two stood -- never the question again, nor Admiry's way in
    its place. What the business chose matters only to an envelope that offers both ways: one way alone is translated
    as it is, whatever the business chose. The business is read once a call (page-first review, item 3), and only for
    an envelope that offers a way."""
    from agent import tools_consult

    home = [tools_consult.MANAGED_ACTION, tools_consult.OWN_ACCOUNT_ACTION, tools_consult.ADJUST_ACTION]
    env = {"data": {}, "say": "x", "next": {"kind": "actions", "actions": [dict(a) for a in home]}}
    asked = []

    def owner() -> actions.Owner:
        asked.append(True)
        return actions.Owner(page=None, chosen=True)

    assert actions.translate(env, owner=owner)["next"]["actions"] == [
        {"id": "link_own_account", "label": "I have a Google Ads account", "tool": "connect_account"},
        {"id": "adjust", "label": "Adjust the budget", "tool": "update_marketing_plan"},
    ]
    assert asked == [True]
    one_way = {"data": {}, "say": "x", "next": {"kind": "actions", "actions": [dict(tools_consult.LINK_ACTION)]}}
    assert actions.translate(one_way, owner=owner)["next"]["actions"] == [
        {**tools_consult.LINK_ACTION, "tool": "connect_account"}
    ]
    assert asked == [True, True]  # read for the page; one way alone is no choice between the two
    adjust = {"data": {}, "say": "x", "next": {"kind": "actions", "actions": [dict(tools_consult.ADJUST_ACTION)]}}
    actions.translate(adjust, owner=owner)
    assert asked == [True, True]  # no way offered: nothing to read


def test_while_the_page_comes_first_a_plan_answer_offers_its_question_in_place_of_the_ways():
    """The founder's ruling (2026-10-13): the page comes before the money. An envelope that offers a way the ads run --
    the two ways, the own account's link alone, or the funds (PREPAY_ACTION, the same id) -- while the one read of the
    caller's business (`owner()`: where.owner_choice) gives the page question (no page settled for its latest draft)
    says it after its own words and offers the page's three ways alone, never a way or the money; a read with no page
    question leaves the ways as they were. An envelope that offers no way (a draft key's sign-in) reads nothing."""
    from agent import launch_check, tools_consult

    ways = launch_check.page_actions(
        [
            {"id": w["id"], "label": w["label"], "args": dict(w["args"] or {"action": "write"})}
            for w in launch_check.PAGE_WAYS
        ]
    )
    asked: list[str] = []

    def owner() -> actions.Owner:
        asked.append("owner")
        return actions.Owner(page=(launch_check.PAGE_QUESTION, ways), chosen=False)

    for offered in (
        [tools_consult.MANAGED_ACTION, tools_consult.OWN_ACCOUNT_ACTION, tools_consult.ADJUST_ACTION],
        [tools_consult.LINK_ACTION, tools_consult.ADJUST_ACTION],
        [tools_consult.PREPAY_ACTION, tools_consult.ADJUST_ACTION],
    ):
        asked.clear()
        env = {
            "data": {},
            "say": "Here is the plan.",
            "next": {"kind": "actions", "actions": [dict(a) for a in offered]},
        }
        out = actions.translate(env, owner=owner)
        assert out["say"] == f"Here is the plan. {launch_check.PAGE_QUESTION}"
        assert out["next"] == {"kind": "actions", "actions": ways} and asked == ["owner"]
        settled = actions.translate(env, owner=lambda: actions.Owner(page=None, chosen=False))
        assert settled["next"]["actions"][-1]["id"] == "adjust"
    asked.clear()
    sign_in = {"data": {}, "say": "x", "next": tools_consult.SIGN_IN_ACTIONS}
    assert actions.translate(sign_in, owner=owner)["say"] == "x" and asked == []


def test_an_action_with_no_entry_and_a_link_pass_through():
    """A tool `TOOL` has no entry for is already public (marketing_state names its own): it comes out unchanged, with
    no args. A `link` next is not actions: the envelope comes back as the same object."""
    named = {"id": "record_outcomes", "label": "Say what the leads became", "tool": "get_leads", "primary": True}
    env = {"data": {}, "say": "x", "next": {"kind": "actions", "actions": [named]}}
    assert actions.translate(env)["next"]["actions"] == [named]
    link = {"data": {}, "say": "x", "next": {"kind": "link", "link": {"url": "https://example.com/", "label": "Open"}}}
    assert actions.translate(link) is link


def test_every_action_a_command_names_has_a_public_tool():
    """The commands' own action tools, as the scan of Task 8 found them: each is translated or already public."""
    named = {
        "recommend",
        "adjust",
        "connect_channel",
        "request_payment_link",
        "request_connect_link",
        "set_landing_page",
        "check_landing_page",
        "approve_plan",
        "publish_page",
        "revise_page",
        "contact_support",
    }
    assert named - {"contact_support"} <= set(actions.TOOL)
