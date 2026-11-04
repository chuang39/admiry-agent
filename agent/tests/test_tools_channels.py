"""The channel tools (agent/tools_channels.py; spec 2026-09-29-admiry-channels-design §4, §7.1; plan
2026-09-29-agent-m0-channel-framework Task 6): connect_channel and its Google alias connect_google_ads, and the four
reading tools -- get_connection_status, get_channel_status, get_status_issues, get_platform_statement -- each a slice
of the one read model (agent/state.py)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from psycopg.types.json import Jsonb

from agent import (
    ads_launch,
    ads_mode,
    ads_run,
    agreements,
    config,
    envelope,
    google_ads,
    google_ads_link,
    keys,
    mcp_app,
    plans,
    state,
    tools_channels,
)
from agent.channels import registry
from agent.channels.google_search import adapter as google
from agent.channels.newsbreak import adapter as newsbreak
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_ads_run import BIZ, CID, GOOGLE, gads, launched, live, row  # noqa: F401 - gads, live: fixtures
from agent.tests.test_ads_run_channels import NB, funds
from agent.tests.test_ads_sync import add_days
from agent.tests.test_state import live_google_and_waiting_newsbreak, plain
from agent.tests.test_tools_ads import owner_key

# ---------------------------------------------------------------- get_channel_status


def test_get_channel_status_is_the_states_rows_and_one_clause_each(client, conn):
    live_google_and_waiting_newsbreak(conn)
    key = business_with_key(conn, biz_id=BIZ)
    res = call_tool(client, "get_channel_status", {}, key=key)
    assert not res["isError"], tool_text(res)
    channels = state.build(conn, config.load(), BIZ).cards()["channels"]
    assert data(res) == {"channels": channels["rows"], "footer": channels["footer"]}
    say = res["structuredContent"]["say"]
    assert say == "Google Search: live · $38 of $40 today. NewsBreak: waiting for funds." and plain(say)


def test_the_chat_footer_and_the_homepage_footer_come_from_one_place(client, conn, monkeypatch):
    """Fix round 1 (Minor 2): the day M6 names the next report, get_channel_status's footer and the homepage's say
    the same -- both read `state.next_report_day`."""
    live_google_and_waiting_newsbreak(conn)
    key = business_with_key(conn, biz_id=BIZ)
    monkeypatch.setattr(state, "next_report_day", lambda conn, settings, business_id: date(2026, 10, 5))
    chat = data(call_tool(client, "get_channel_status", {}, key=key))["footer"]
    home = state.build(conn, config.load(), BIZ).cards()["channels"]["footer"]
    assert chat == home and chat["next_report_at"] == "2026-10-05"
    assert state.build(conn, config.load(), BIZ).next_report_at == date(2026, 10, 5)


def test_get_channel_status_reports_today_and_the_week_when_live(client, conn, gads, live):  # noqa: F811 (fixture)
    """A3's live line, ported: today's spend against today's budget, this week's calls (ruling S14's calendar)."""
    key, _plan_id = launched(conn, gads)
    day = ads_run.account_today(row(conn))
    add_days(conn, (day, 7800000, 5, 80, 2, 2), (day - timedelta(days=1), 6000000, 4, 60, 1, 1))
    res = call_tool(client, "get_channel_status", {}, key=key)
    (g,) = data(res)["channels"]
    budget = google_ads.from_micros(ads_launch.daily_micros(1000.0))
    assert (g["state"], g["today_usd"], g["day_cap_usd"], g["calls_7d"]) == ("live", 7.8, budget, 3)
    say = res["structuredContent"]["say"]
    assert say == f"Google Search: live · {plans.usd(7.8)} of {plans.usd(budget)} today." and plain(say)


def test_get_channel_status_with_no_ads_yet(client, conn):
    key = business_with_key(conn)
    res = call_tool(client, "get_channel_status", {}, key=key)
    assert data(res)["channels"] == [] and res["structuredContent"]["say"] == tools_channels.NO_ADS_YET
    assert plain(tools_channels.NO_ADS_YET)


def test_get_channel_status_names_a_disapproved_ad(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    issues = {"ads": [{"resource_name": "x", "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}]}]}
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = %s",
        (Jsonb({"issues": issues}), BIZ),
    )
    say = call_tool(client, "get_channel_status", {}, key=key)["structuredContent"]["say"]
    assert say.endswith("Google Search needs a change to one of your ads.") and plain(say)


def test_pause_then_get_channel_status_says_paused_by_you_today(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    paused = call_tool(client, "pause", {"idempotency_key": "p1"}, key=key)
    assert not paused["isError"], tool_text(paused)
    d = data(paused)  # the pause tool's own answer, as A3's test had it (fix round 1, Minor 6)
    assert d["state"] == "paused" and "say" not in d
    assert plain(paused["structuredContent"]["say"])
    res = call_tool(client, "get_channel_status", {}, key=key)
    assert res["structuredContent"]["say"] == "Google Search: paused by you today."
    (google_row,) = data(res)["channels"]
    assert (google_row["state_text"], google_row["actions"]) == ("Paused by you today", ["resume"])


# ---------------------------------------------------------------- get_status_issues


def test_get_status_issues_with_nothing_wrong(client, conn, gads, live):  # noqa: F811 (fixture)
    key, _plan_id = launched(conn, gads)
    res = call_tool(client, "get_status_issues", {}, key=key)
    assert data(res) == {"channels": [{"channel": GOOGLE, "label": "Google Search", "reasons": [], "ads": []}]}
    assert res["structuredContent"]["say"] == tools_channels.NO_ISSUES and plain(tools_channels.NO_ISSUES)


def test_get_status_issues_quote_each_platform_per_channel(client, conn, gads, live):  # noqa: F811 (fixture)
    """Quoted verbatim, nothing invented, and the ad's headline named -- per channel, in card order."""
    key, plan_id = launched(conn, gads)
    issues = {
        "campaign": ["MISCONFIGURED"],
        "ads": [{"resource_name": "x", "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}]}],
    }
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = %s",
        (Jsonb({"issues": issues}), BIZ),
    )
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'waiting_account')",
        (BIZ, NB, plan_id),
    )
    words = "Your account needs a payment method on file."
    conn.execute(
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text) VALUES (%s, %s, 'billing', %s)",
        (BIZ, NB, words),
    )
    res = call_tool(client, "get_status_issues", {}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res)["channels"] == [
        {
            "channel": GOOGLE,
            "label": "Google Search",
            "reasons": ["MISCONFIGURED"],
            "ads": [{"headline": "Invisalign in Palo Alto", "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}]}],
        },
        {"channel": NB, "label": "NewsBreak", "reasons": [words], "ads": []},
    ]
    say = res["structuredContent"]["say"]
    assert '"MISCONFIGURED"' in say and '"Alcohol"' in say and "PROHIBITED" in say
    assert '"Invisalign in Palo Alto"' in say and f'NewsBreak reports: "{words}"' in say
    assert say.index("Google Search") < say.index("NewsBreak")


def test_get_status_issues_names_each_channels_own_headline(client, conn, gads, live):  # noqa: F811 (fixture)
    """M0 Task 5, ported: Google's issues name Google's ad, wherever its entry sits among the plan's channels."""
    key, plan_id = launched(conn, gads)
    plan = conn.execute("SELECT json FROM plan WHERE id = %s", (plan_id,)).fetchone()["json"]
    google_entry = plan["sections"]["ads"]["channels"][0]
    nb_entry = {**google_entry, "channel": NB, "monthly_usd": 300.0, "headlines": ["Local news readers"]}
    plan["sections"]["ads"]["channels"] = [nb_entry, google_entry]
    conn.execute("UPDATE plan SET json = %s WHERE id = %s", (Jsonb(plan), plan_id))
    issues = {"ads": [{"resource_name": "x", "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}]}]}
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = %s AND channel = %s",
        (Jsonb({"issues": issues}), BIZ, GOOGLE),
    )
    ads = data(call_tool(client, "get_status_issues", {}, key=key))["channels"][0]["ads"]
    assert ads[0]["headline"] == "Invisalign in Palo Alto"


# ---------------------------------------------------------------- get_connection_status


def test_get_connection_status_lists_every_registered_channel_with_its_next_step(client, conn, newsbreak_on):
    key = owner_key(conn)
    res = call_tool(client, "get_connection_status", {}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res) == {
        "channels": [
            {
                "channel": GOOGLE,
                "label": "Google Search",
                "mode": "own_account",
                "status": "none",
                "account_public": None,
                "checked_at": None,
                "next_step": google.CONNECT_OWN_ACCOUNT,
            },
            {
                "channel": NB,
                "label": "NewsBreak",
                "mode": "managed",
                "status": "none",
                "account_public": None,
                "checked_at": None,
                "next_step": "Add funds to start NewsBreak",  # nothing to connect: what it waits for (W3)
            },
        ],
        "verified": False,
        "agreement_accepted": True,
        "connect_url": "http://testserver/?setup=biz_1",
    }
    assert plain(res["structuredContent"]["say"])
    google_ads_link.save(conn, "biz_1", CID, status="manual")
    conn.execute("UPDATE business SET claim_state = 'verified' WHERE id = 'biz_1'")
    got = data(call_tool(client, "get_connection_status", {}, key=key))
    g = got["channels"][0]
    assert got["verified"] and (g["status"], g["account_public"], g["next_step"]) == ("active", "123-456-7890", None)
    assert g["checked_at"] is not None


def test_get_connection_status_names_neither_way_while_none_is_chosen(client, conn):
    """Addendum 3 (founder 2026-10-13): while the owner has chosen no way (no link, no agreement, Admiry not running
    the ads), Google Search's next step names neither -- the neutral line (ads_mode.WAY_LINE), never the Customer ID's
    line, which picks the owner's own account; once a way is chosen, the Customer ID's line as before (the test
    above)."""
    key = business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")

    def google_step() -> str | None:
        res = call_tool(client, "get_connection_status", {}, key=key)
        assert not res["isError"], tool_text(res)
        return next(c["next_step"] for c in data(res)["channels"] if c["channel"] == GOOGLE)

    assert google_step() == ads_mode.WAY_LINE
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    assert google_step() == google.CONNECT_OWN_ACCOUNT


def test_get_connection_status_leaves_out_a_channel_switched_off(client, conn):
    """NewsBreak's switch unset: the tool names Google Search alone -- the assistant never offers the owner a channel
    Admiry cannot run yet."""
    key = owner_key(conn)
    got = data(call_tool(client, "get_connection_status", {}, key=key))
    assert [c["channel"] for c in got["channels"]] == [GOOGLE]


def test_a_platforms_notice_reads_the_same_in_settings_and_in_the_chat(client, conn, newsbreak_on):
    """Final review M8: Settings' status and get_connection_status's sentence come from one account word
    (`envelope.account_word`): a managed channel with a platform's notice needs attention in both -- the chat no
    longer says "run by Admiry" where Settings shows the notice."""
    key = owner_key(conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    registry.save_account(
        conn, "biz_1", NB, mode="managed", status="needs_attention", external={}, last_error="Under review."
    )
    say = call_tool(client, "get_connection_status", {}, key=key)["structuredContent"]["say"]
    assert "NewsBreak needs attention." in say and "NewsBreak is run by Admiry" not in say
    words = {
        c.channel: envelope.account_word(c.mode, c.status) for c in state.connections(conn, config.load(), "biz_1")
    }
    assert words == {GOOGLE: "managed", NB: "needs_attention"}


def test_get_connection_status_carries_the_balance_for_a_business_admiry_runs_ads_for(client, conn):
    key = owner_key(conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    funds(conn, 12_345)
    got = data(call_tool(client, "get_connection_status", {}, key=key))
    assert got["balance_usd"] == 123.45 and got["channels"][0]["mode"] == "managed"


def test_the_connection_say_is_true_for_every_link_status(client, conn, newsbreak_on):
    """The say names each channel's state in plain words; a link attempt never reads as "not connected yet"."""
    key = owner_key(conn)
    cases = {
        "pending": "waiting for you to accept",
        "manual": "is connected",
        "active": "is connected",
        "refused": "refused",
        "cancelled": "inactive",
        "inactive": "inactive",
    }
    say = call_tool(client, "get_connection_status", {}, key=key)["structuredContent"]["say"]
    assert "Google Search is not connected yet." in say and "NewsBreak is run by Admiry from your balance." in say
    for status, expect in cases.items():
        google_ads_link.save(conn, "biz_1", CID, status=status)
        say = call_tool(client, "get_connection_status", {}, key=key)["structuredContent"]["say"]
        first = say.split(". ")[0]
        assert expect in first.lower() and "not connected" not in first, (status, say)
        assert plain(say) and "123-456-7890" in first, (status, say)


# ---------------------------------------------------------------- connect_channel


def test_connect_channel_newsbreak_is_a_web_step_with_nothing_to_connect(client, conn):
    """NewsBreak's step is the setup step on the web (connect_url, where the balance is added); since the M1 follow-up
    2 (W3) the step itself never asks for money, and the answer says what NewsBreak waits for while it is not ready
    (fix round 1, F2: `next_step`, get_connection_status's own) -- with no balance, the funds."""
    key = owner_key(conn)
    res = call_tool(client, "connect_channel", {"channel": NB, "idempotency_key": "c1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res) == {
        "status": "web_step",
        "channel": NB,
        "label": "NewsBreak",
        "steps": [{"text": newsbreak.CONNECT, "url": None}],
        "next_step": "Add funds to start NewsBreak",
        "connect_url": "http://testserver/?setup=biz_1",
    }
    say = res["structuredContent"]["say"]
    assert say == (
        "NewsBreak: Nothing to connect: Admiry runs NewsBreak from your balance. Add funds to start NewsBreak."
    )
    assert plain(say)
    assert conn.execute("SELECT count(*) AS n FROM channel_account WHERE channel = 'newsbreak'").fetchone()["n"] == 0


@pytest.mark.parametrize(
    ("waiting", "next_step", "said"),
    [
        (
            True,
            "NewsBreak is getting your account ready; your ads start once it is",
            (
                "NewsBreak: Nothing to connect: Admiry runs NewsBreak from your balance. NewsBreak is getting your "
                "account ready; your ads start once it is."
            ),
        ),
        (False, None, "NewsBreak: Nothing to connect: Admiry runs NewsBreak from your balance."),
    ],
    ids=["NewsBreak gets the account ready", "ready"],
)
def test_connect_channel_newsbreak_with_a_balance_says_what_it_waits_for_never_funds(
    client, conn, newsbreak_on, waiting, next_step, said
):
    """Fix round 1 (F2): with money on the balance, connect_channel never asks for funds -- while NewsBreak gets the
    account ready (R11's wait, an operator's work) the answer says that, as get_connection_status's next step does;
    once NewsBreak is ready, the step alone."""
    from datetime import UTC, datetime

    key = owner_key(conn)
    funds(conn, biz="biz_1")
    if waiting:
        external = {"account_status": "PENDING", "account_checked_at": datetime.now(UTC).isoformat()}
        registry.save_account(conn, "biz_1", NB, mode="managed", status="needs_attention", external=external)
    res = call_tool(client, "connect_channel", {"channel": NB, "idempotency_key": "c1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res).get("next_step") == next_step and data(res)["steps"] == [{"text": newsbreak.CONNECT, "url": None}]
    say = res["structuredContent"]["say"]
    assert say == said and "funds" not in say and plain(say)
    listed = {
        c["channel"]: c["next_step"] for c in data(call_tool(client, "get_connection_status", {}, key=key))["channels"]
    }
    assert listed.get(NB) == next_step  # one source: get_connection_status says the same


def test_connect_channel_refuses_a_channel_it_does_not_know(client, conn):
    key = owner_key(conn)
    for channel in ("tiktok", "meta"):  # never heard of; named, but no adapter yet
        res = call_tool(client, "connect_channel", {"channel": channel, "idempotency_key": f"c_{channel}"}, key=key)
        assert res["isError"] and "unknown channel" in tool_text(res)


def test_connect_channel_refuses_a_mode_the_channel_does_not_run(client, conn):
    key = owner_key(conn)
    res = call_tool(client, "connect_channel", {"channel": NB, "mode": "own_account", "idempotency_key": "c1"}, key=key)
    assert res["isError"] and ads_run.MODE_SOON.format(label="NewsBreak") in tool_text(res)
    res = call_tool(client, "connect_channel", {"channel": NB, "mode": "sideways", "idempotency_key": "c2"}, key=key)
    assert res["isError"] and tools_channels.BAD_MODE in tool_text(res)


def test_connect_channel_google_asks_for_the_agreement_then_sends_the_invitation(client, conn):
    key = owner_key(conn, agree=False)
    args = {"channel": GOOGLE, "account_id": "123-456-7890", "idempotency_key": "g1"}
    res = call_tool(client, "connect_channel", args, key=key)
    assert data(res)["status"] == "needs_agreement" and data(res)["connect_url"] == "http://testserver/?setup=biz_1"
    assert data(res)["note"] == tools_channels.NEEDS_AGREEMENT_NOTE and google_ads_link.get(conn, "biz_1") is None
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    res = call_tool(client, "connect_channel", {**args, "idempotency_key": "g2"}, key=key)
    assert not res["isError"], tool_text(res)
    got = data(res)
    assert (got["status"], got["customer_id"], got["instructions"]) == (
        "pending",
        "123-456-7890",
        google_ads_link.instructions("manual"),
    )
    assert got["steps"] == [{"text": google.CONNECT_OWN_ACCOUNT, "url": "http://testserver/?setup=biz_1"}]
    assert google_ads_link.get(conn, "biz_1").status == "pending" and plain(res["structuredContent"]["say"])


def test_connect_channel_google_already_linked_is_connected(client, conn):
    key = owner_key(conn)
    google_ads_link.save(conn, "biz_1", CID, status="active")
    for i, args in enumerate(({}, {"account_id": "123 456 7890"})):  # no id, or the linked one again
        res = call_tool(client, "connect_channel", {"channel": GOOGLE, "idempotency_key": f"k{i}", **args}, key=key)
        assert not res["isError"], tool_text(res)
        assert (data(res)["status"], data(res)["account_public"]) == ("connected", "123-456-7890")
        assert res["structuredContent"]["say"] == "Google Search is connected: account 123-456-7890."
    res = call_tool(
        client, "connect_channel", {"channel": GOOGLE, "account_id": "9876543210", "idempotency_key": "k9"}, key=key
    )
    assert res["isError"] and "already linked to Google Ads account 123-456-7890" in tool_text(res)


def test_the_activity_log_names_the_account_a_connected_answer_is_about(client, conn):
    """Fix round 1 (Minor 5): the alias's line reads "asked to link Google Ads account 123-456-7890", never "…?",
    when the link was already in place."""
    key = owner_key(conn)
    google_ads_link.save(conn, "biz_1", CID, status="active")
    res = call_tool(client, "connect_google_ads", {"customer_id": "123-456-7890", "idempotency_key": "k1"}, key=key)
    assert (data(res)["status"], data(res)["customer_id"]) == ("connected", "123-456-7890")
    sentences = [e["sentence"] for e in data(call_tool(client, "get_audit_log", {}, key=key))["entries"]]
    assert sentences[0] == "Muse asked to link Google Ads account 123-456-7890", sentences  # the key is Muse's


def test_connect_channel_google_without_an_id_is_the_step_to_give_one(client, conn):
    key = owner_key(conn)
    res = call_tool(client, "connect_channel", {"channel": GOOGLE, "idempotency_key": "g1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert (data(res)["status"], data(res)["steps"][0]["text"]) == ("web_step", google.CONNECT_OWN_ACCOUNT)
    managed = call_tool(
        client, "connect_channel", {"channel": GOOGLE, "mode": "managed", "idempotency_key": "g2"}, key=key
    )
    assert data(managed)["status"] == "web_step" and data(managed)["steps"] == [
        {"text": google.CONNECT_MANAGED, "url": None}
    ]
    assert google_ads_link.get(conn, "biz_1") is None  # nothing sent, nothing changed


def test_a_customer_id_on_a_prepaid_business_takes_the_invitation_path_and_is_refused(client, conn):
    """Fix round 1 (Important 1): an account id for Google Search always goes through the existing invite path --
    on a prepaid business it is refused there (NOT_OWN_ACCOUNT), never answered with the prepaid way's "coming" step
    while the number is dropped; only an explicit mode="managed" asks for that step."""
    key = owner_key(conn)
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    for tool, args in (
        ("connect_google_ads", {"customer_id": "123-456-7890"}),
        ("connect_channel", {"channel": GOOGLE, "account_id": "123-456-7890"}),
        ("connect_channel", {"channel": GOOGLE, "account_id": "123-456-7890", "mode": "own_account"}),
    ):
        res = call_tool(client, tool, {**args, "idempotency_key": f"{tool}-{len(args)}"}, key=key)
        assert res["isError"] and google_ads_link.NOT_OWN_ACCOUNT in tool_text(res), (tool, args, tool_text(res))
    asked = call_tool(
        client,
        "connect_channel",
        {"channel": GOOGLE, "account_id": "123-456-7890", "mode": "managed", "idempotency_key": "m1"},
        key=key,
    )
    assert (data(asked)["status"], data(asked)["steps"]) == (
        "web_step",
        [{"text": google.CONNECT_MANAGED, "url": None}],
    )
    assert google_ads_link.get(conn, "biz_1") is None  # nothing sent, nothing saved


def test_connect_google_ads_is_connect_channel_for_google_search(client, conn):
    """The alias answers exactly as connect_channel("google_search", account_id=…) does, under its own name."""
    key = owner_key(conn)
    alias = call_tool(client, "connect_google_ads", {"customer_id": "123-456-7890", "idempotency_key": "a1"}, key=key)
    assert not alias["isError"], tool_text(alias)
    assert data(alias) == {
        "status": "pending",
        "channel": GOOGLE,
        "label": "Google Search",
        "customer_id": "123-456-7890",
        "instructions": google_ads_link.instructions("manual"),
        "steps": [{"text": google.CONNECT_OWN_ACCOUNT, "url": "http://testserver/?setup=biz_1"}],
    }
    rows = conn.execute("SELECT tool, args_redacted FROM agent_action WHERE tool LIKE 'connect%%'").fetchall()
    assert [(r["tool"], r["args_redacted"]["customer_id"]) for r in rows] == [("connect_google_ads", "123-456-7890")]
    again = call_tool(
        client, "connect_channel", {"channel": GOOGLE, "account_id": CID, "idempotency_key": "a2"}, key=key
    )
    assert again["isError"] and "already on its way" in tool_text(again)  # the same invitation, not sent twice


def test_connect_channel_refuses_the_setup_key(client, conn):
    owner_key(conn)
    draft, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    res = call_tool(client, "connect_channel", {"channel": NB, "idempotency_key": "d1"}, key=draft)
    assert res["isError"] and "setup key" in tool_text(res)


# ---------------------------------------------------------------- whoami


def test_whoami_names_the_channels_connected(client, conn):
    key = owner_key(conn)
    assert data(call_tool(client, "whoami", {}, key=key))["channels_connected"] == []
    google_ads_link.save(conn, "biz_1", CID, status="pending")
    assert data(call_tool(client, "whoami", {}, key=key))["channels_connected"] == []
    google_ads_link.save(conn, "biz_1", CID, status="active")
    assert data(call_tool(client, "whoami", {}, key=key))["channels_connected"] == [GOOGLE]


# ---------------------------------------------------------------- get_platform_statement


def test_get_platform_statement_is_every_channels_statement(client, conn, gads, live):  # noqa: F811 (fixture)
    key, plan_id = launched(conn, gads)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'live')",
        (BIZ, NB, plan_id),
    )
    today = ads_run.account_today(row(conn))  # each channel's own calendar (ruling S14)
    nb_today = ads_run.account_today(ads_run.get(conn, BIZ, NB))
    last_month = today.replace(day=1) - timedelta(days=1)
    spend = ((GOOGLE, today, 842_000_000), (NB, nb_today, 120_000_000), (GOOGLE, last_month, 5_000_000))
    for channel, day, micros in spend:
        conn.execute(
            "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
            "conversions, booked, source) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'api')",
            (BIZ, channel, day, micros),
        )
    res = call_tool(client, "get_platform_statement", {}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res) == {"month": None, "statement": ads_run.statement(conn, BIZ)}
    assert res["structuredContent"]["say"] == (
        f"$842 spent so far this month on Google Search, $120 on NewsBreak. {data(res)['statement']['no_fee_notice']}."
    )
    one = data(call_tool(client, "get_platform_statement", {"month": last_month.strftime("%Y-%m")}, key=key))
    assert [(c["label"], [m["month"] for m in c["months"]]) for c in one["statement"]["channels"]] == [
        ("Google Search", [last_month.strftime("%B %Y")]),
        ("NewsBreak", [last_month.strftime("%B %Y")]),
    ]
    assert one["statement"]["channels"][0]["months"][0]["spent_usd"] == 5.0
    # fix round 1 (Minor 4): anything but a sane YYYY-MM is refused plainly, never an internal error (9999-12's next
    # month is past the calendar)
    for month in ("last month", "9999-12", "2026-13", "2026-00", "2026-9", "12-2026", "1999-12", "2026-09-01"):
        bad = call_tool(client, "get_platform_statement", {"month": month}, key=key)
        assert bad["isError"] and tools_channels.BAD_MONTH in tool_text(bad), (month, tool_text(bad))


def test_get_platform_statement_before_any_ads(client, conn):
    key = business_with_key(conn)
    res = call_tool(client, "get_platform_statement", {}, key=key)
    assert (
        data(res) == {"month": None, "statement": None}
        and res["structuredContent"]["say"] == tools_channels.NO_STATEMENT
    )


def test_every_new_owner_sentence_reads_plainly():
    """JARGON, ACCOUNT_JARGON and MONEY_JARGON over the words M0 Task 6 adds: the tools' sentences, the Your ads page's
    status words and actions (Settings' until YA), the connection clauses."""
    from agent import envelope, your_ads
    from agent.state import Connection

    sentences = [
        tools_channels.NO_ADS_YET,
        tools_channels.NO_ISSUES,
        tools_channels.NO_STATEMENT,
        tools_channels.BAD_MODE,
        tools_channels.BAD_MONTH,
        tools_channels.SETUP_KEY,
        tools_channels.NEEDS_AGREEMENT_NOTE,
    ]
    for mode in ("own_account", "managed"):
        for status in ("none", "pending", "active", "refused", "inactive", "needs_attention"):
            for ready in (False, True):
                c = Connection(
                    channel=GOOGLE,
                    label="Google Search",
                    mode=mode,
                    status=status,
                    account_public="123-456-7890",
                    checked_at=None,
                    ready=ready,
                    reason=None,
                    note="Your account is under review.",
                    steps=[],
                    next_step=None,
                )
                action = your_ads.one_action(c, None, True)
                sentences += [your_ads.status_word(c), action or "", envelope._connection_clause(c.public())]
    for text in sentences:
        assert plain(text), text


# ---------------------------------------------------------------- registration


@pytest.mark.parametrize(
    ("name", "shape"),
    [
        ("connect_channel", ("W", "manage", "assistant")),
        ("connect_google_ads", ("W", "manage", "assistant")),
        ("get_connection_status", ("RO", "read", "none")),
        ("get_channel_status", ("RO", "read", "none")),
        ("get_status_issues", ("RO", "read", "none")),
        ("get_platform_statement", ("RO", "read", "none")),
    ],
)
def test_the_channel_tools_are_registered_with_their_classes(name, shape):
    meta = next(m for m in mcp_app.REGISTRY if m.name == name)
    assert (meta.cls, meta.scope, meta.approval) == shape
    assert plain(meta.tool.description)


def test_connect_channel_takes_the_channel_and_the_key(client):
    meta = next(m for m in mcp_app.REGISTRY if m.name == "connect_channel")
    assert meta.tool.parameters["required"] == ["channel", "idempotency_key"]
    assert set(meta.tool.parameters["properties"]) == {"channel", "idempotency_key", "mode", "account_id"}
    docs = client.get("/connect/docs").text
    assert "<code>connect_account</code>" in docs and "<code>get_campaign_performance</code>" in docs
