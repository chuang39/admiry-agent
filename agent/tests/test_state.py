"""The one read model (agent/state.py; spec 2026-09-29-admiry-channels-design D102, §4, §7.2; plan
2026-09-29-agent-m0-channel-framework P10, Task 6): `build` reads a business's plan cards, where its ads run on each
channel, its accounts and the platforms' issues into one versioned `MarketingState`; `cards()` is what the homepage
and the views render; `connections()` is every registered channel's account for Settings, the setup cards and
`get_connection_status`. Nothing here writes."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from psycopg.types.json import Jsonb

from agent import ads_run, config, landing, plans, site_install, state
from agent.channels import base, registry
from agent.channels.google_search import adapter as google
from agent.channels.newsbreak import adapter as newsbreak
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.fixtures import FakeAdapter, business_with_key
from agent.tests.test_ads_run import BIZ, GOOGLE, KNOWLEDGE, sections, seed
from agent.tests.test_ads_run_channels import NB, add_plan_by_hand, funds, two_channels

BING = "microsoft_search"
GOOGLE_MONTHLY = 1216.0  # ÷ 30.4 = $40.00 a day, to the cent
NB_MONTHLY = 300.0


class Bing(FakeAdapter):
    """A third channel whose account is always ready (the mock's third row is Bing Search, paused by its owner)."""

    def __init__(self):
        super().__init__(BING)

    def check_account(self, conn, settings, account):
        return base.AccountStatus(ready=True, status="active", reason=None, account_public="B-42")


def plain(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


def consulted(secs: dict) -> dict:
    """test_ads_run's sections with the rest of a consultation's answers (`plans.consultation_section`), so the plan
    renders as cards the way every plan a consultation builds does."""
    secs["consultation"].update({"conversions": plans.CONVERSIONS["bookings"], "duration_days": 30, "offer": None})
    return secs


def campaign(
    conn,
    channel: str,
    plan_id: str,
    row_state: str,
    *,
    resources: dict | None = None,
    paused_reason: str | None = None,
    last_error: str | None = None,
    paused_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, paused_reason, last_error, "
        "launched_at, paused_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s)",
        (BIZ, channel, plan_id, row_state, Jsonb(resources or {}), paused_reason, last_error, paused_at),
    )


def day(conn, channel: str, when: date, *, cost_usd: float = 0.0, calls: int = 0) -> None:
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, %s, %s, %s, 0, 0, %s, 0, 0, 0, 'api')",
        (BIZ, channel, when, round(cost_usd * 1_000_000), calls),
    )


def lead(conn, lead_id: str, channel: str | None, *, days_ago: int = 0) -> None:
    conn.execute(
        "INSERT INTO lead (id, business_id, kind, fields, click_id, click_id_kind, channel, created_at) "
        "VALUES (%s, %s, 'form', '{}', %s, %s, %s, now() - make_interval(days => %s))",
        (lead_id, BIZ, "c_" + lead_id if channel else None, "gclid" if channel else None, channel, days_ago),
    )


def live_google_and_waiting_newsbreak(conn) -> str:
    """The mock's first two rows: Google Search live at $40 a day ($38 spent today, 2 calls and 1 form this week),
    NewsBreak approved and waiting for funds. Returns the plan id."""
    seed(conn, status="superseded")
    plan_id = add_plan_by_hand(conn, consulted(two_channels(google_usd=GOOGLE_MONTHLY, newsbreak_usd=NB_MONTHLY)))
    campaign(conn, GOOGLE, plan_id, "live", resources={"campaign": "customers/1234567890/campaigns/1"})
    campaign(conn, NB, plan_id, "waiting_account", last_error=ads_run.NO_FUNDS.format(label="NewsBreak"))
    today = ads_run.account_today(None)
    day(conn, GOOGLE, today, cost_usd=38.0, calls=1)
    day(conn, GOOGLE, today - timedelta(days=3), cost_usd=40.0, calls=1)
    day(conn, GOOGLE, today - timedelta(days=9), cost_usd=40.0, calls=5)  # outside the week
    lead(conn, "ld_1", GOOGLE)
    lead(conn, "ld_2", GOOGLE, days_ago=10)  # outside the week
    lead(conn, "ld_3", None)  # organic: no channel's
    return plan_id


# ---------------------------------------------------------------- the model


def test_a_business_with_nothing_yet_has_an_empty_state(conn):
    business_with_key(conn, claim_state="claimed")
    st = state.build(conn, config.load(), BIZ)
    assert st.version == 1 and st.business == {"id": BIZ, "name": None, "verified": False, "claim_state": "claimed"}
    assert (st.plan, st.budget, st.economics, st.rationale, st.ads_preview) == (None, None, None, None, None)
    assert (st.plan_id, st.execution, st.channels, st.issues) == (None, None, [], [])
    assert (st.money, st.report, st.refund_request, st.next_report_at) == (None, None, None, None)
    assert st.cards() == {
        "plan": None,
        "budget": None,
        "economics": None,
        "rationale": None,
        "ads_preview": None,
        "channels": {"rows": [], "footer": {"month_usd": 0.0, "month_cap_usd": None, "next_report_at": None}},
        "money": None,
        "report": None,
    }


def test_the_plan_parts_are_the_plan_cards_unchanged(conn):
    seed(conn, status="draft", secs=consulted(sections()))
    conn.execute("UPDATE page SET hostname = 'brightsmile.example'")  # the page the ads send people to
    st = state.build(conn, config.load(), BIZ)
    latest = plans.latest(conn, BIZ)
    assert landing.final_host(conn, BIZ) == "brightsmile.example"
    row = site_install.row(conn, BIZ)  # the plan's "Your website" row (A3p-5 batch 2 Q5), in the plan's setup
    expected = plans.cards(
        {**latest, "business_name": "Bright Smile Dental"}, final_host="brightsmile.example", website=row
    )
    assert {k: st.cards()[k] for k in expected} == expected and st.plan["setup"]["website"] == row
    assert st.business["name"] == "Bright Smile Dental" and st.business["verified"] is True
    assert st.plan_id == latest["id"] and st.execution is None  # nothing approved yet: no "Live since" line


def test_a_placed_address_centres_the_cards_but_is_never_the_business_name(conn):
    """Walk fixes 1, Task 4 (the controller's ruling): with no website and only an address the owner placed, nothing
    vouches for a name -- the state's business name stays null, never an address -- while the plan card's sentence
    centres on the address instead of "your business"."""
    placed = {**KNOWLEDGE["listing"], "kind": "address"}
    seed(conn, status="draft", secs=consulted(sections()), knowledge={**KNOWLEDGE, "website": None, "listing": placed})
    st = state.build(conn, config.load(), BIZ)
    assert st.business["name"] is None
    assert st.cards()["plan"]["goal"] == "More Invisalign bookings within 5 miles of 500 University Ave, Palo Alto, CA"


def test_a_plan_without_a_consultation_has_an_id_but_no_cards(conn):
    business_with_key(conn, claim_state="claimed")
    secs = sections()
    del secs["consultation"]
    plan_id = add_plan_by_hand(conn, secs, status="draft")
    st = state.build(conn, config.load(), BIZ)
    assert st.plan_id == plan_id and st.plan is None and st.budget is None


def test_the_execution_line_is_the_channel_rows_once_a_plan_was_approved(conn):
    plan_id = live_google_and_waiting_newsbreak(conn)
    conn.execute("UPDATE plan SET json = json || %s WHERE id = %s", (Jsonb({"execution": {"ads": "live"}}), plan_id))
    st = state.build(conn, config.load(), BIZ)
    assert st.execution == ads_run.execution_line(ads_run.rows(conn, BIZ))
    assert st.execution == "Google Search: Live since today; NewsBreak: Waiting for funds"


def test_each_channel_row_carries_the_cards_numbers_and_one_sentence(conn):
    """Spec §7.2's channels card: a row per channel -- its state and one sentence, today's spend against today's
    budget, the month against the channel's monthly amount, this week's calls and forms -- and the actions."""
    plan_id = live_google_and_waiting_newsbreak(conn)
    version = plans.get(conn, BIZ, plan_id)["version"]
    st = state.build(conn, config.load(), BIZ)
    google_row, nb_row = st.channels
    assert google_row.model_dump() == {
        "channel": GOOGLE,
        "label": "Google Search",
        "mode": "own_account",
        "account_status": "active",
        "account_public": "123-456-7890",
        "state": "live",
        "state_text": "Live",
        "sentence": "Google Search: live · $38 of $40 today.",
        "today_usd": 38.0,
        "day_cap_usd": 40.0,
        "month_usd": google_row.month_usd,  # the month so far: below
        "month_cap_usd": GOOGLE_MONTHLY,
        "leads_7d": 1,
        "calls_7d": 2,
        "issues": [],
        "actions": ["pause"],
        "plan_id": plan_id,
        "plan_version": version,
    }
    today = ads_run.account_today(None)
    spent = {today: 38.0, today - timedelta(days=3): 40.0, today - timedelta(days=9): 40.0}  # the fixture's days
    assert google_row.month_usd == sum(usd for d, usd in spent.items() if d >= today.replace(day=1))
    assert nb_row.model_dump() == {
        "channel": NB,
        "label": "NewsBreak",
        "mode": "managed",
        "account_status": "none",
        "account_public": None,
        "state": "waiting_account",
        "state_text": "Waiting for funds",
        "sentence": "NewsBreak: waiting for funds.",
        "today_usd": None,  # nothing built: nothing spends today
        "day_cap_usd": None,
        "month_usd": 0.0,
        "month_cap_usd": NB_MONTHLY,
        "leads_7d": 0,
        "calls_7d": 0,
        "issues": [],
        "actions": [],  # nothing to connect: its funds are the money block's (M1m)
        "plan_id": plan_id,
        "plan_version": version,
    }
    for row in st.channels:
        assert plain(row.state_text) and plain(row.sentence)


def test_a_row_whose_plan_is_gone_still_builds_and_names_no_version(conn):
    """`channel_campaign.plan_id` has no foreign key, so a row can name a plan that is not there: the row still builds
    and names the id it holds, with no version."""
    seed(conn, status="superseded", secs=consulted(sections()))
    campaign(conn, GOOGLE, "pl_gone", "live", resources={"campaign": "customers/1234567890/campaigns/1"})
    (row,) = state.build(conn, config.load(), BIZ).channels
    assert (row.plan_id, row.plan_version) == ("pl_gone", None)


def test_the_footer_is_the_month_against_the_channels_amounts(conn):
    live_google_and_waiting_newsbreak(conn)
    st = state.build(conn, config.load(), BIZ)
    footer = st.cards()["channels"]["footer"]
    assert footer == {
        "month_usd": round(sum(r.month_usd for r in st.channels), 2),
        "month_cap_usd": GOOGLE_MONTHLY + NB_MONTHLY,
        "next_report_at": None,  # the weekly report is M6's
    }
    fixture = st.model_copy(update={"next_report_at": date(2026, 10, 5)})
    assert fixture.cards()["channels"]["footer"]["next_report_at"] == "2026-10-05"


def test_resume_is_offered_only_where_the_owners_own_pause_is_the_only_one(conn, monkeypatch):
    """A Task 4 review item: Settings and the card never offer a Resume that would answer PAUSED_BY_ADMIRY."""
    monkeypatch.setitem(registry.CHANNELS, BING, Bing())
    live_google_and_waiting_newsbreak(conn)
    plan_id = plans.latest(conn, BIZ)["id"]
    conn.execute("DELETE FROM channel_campaign")
    now = datetime.now(UTC)
    earlier = now.replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=3)  # noon: one weekday anywhere
    owners = {"campaign": "c/1", "owner_paused": True}
    campaign(conn, GOOGLE, plan_id, "paused", resources=owners, paused_at=earlier)
    campaign(conn, NB, plan_id, "paused", resources={"campaign": "n/1"}, paused_reason="month_cap", paused_at=now)
    both = {"campaign": "b/1", "owner_paused": True}
    campaign(conn, BING, plan_id, "paused", resources=both, paused_reason="no_funds", paused_at=now)
    rows = {r.channel: r for r in state.build(conn, config.load(), BIZ).channels}
    assert list(rows) == [GOOGLE, NB, BING]  # card order (registry.ORDER)
    assert rows[GOOGLE].actions == ["resume"] and rows[GOOGLE].state_text == f"Paused by you on {earlier:%A}"
    assert rows[NB].actions == [] and rows[NB].state_text == "Paused by Admiry today: this month's budget is spent"
    assert rows[BING].actions == []  # both pauses stand: the owner's resume would leave Admiry's
    assert rows[BING].state_text == "Paused by you and by Admiry today: the balance ran out"


def test_connect_is_offered_to_a_row_waiting_for_an_account_the_owner_can_connect(conn):
    seed(conn, status="superseded", link="pending")
    plan_id = add_plan_by_hand(conn, consulted(two_channels()))
    campaign(conn, GOOGLE, plan_id, "waiting_account", last_error=google.LINK_PENDING)
    campaign(conn, NB, plan_id, "waiting_account", last_error=ads_run.NO_FUNDS.format(label="NewsBreak"))
    google_row, nb_row = state.build(conn, config.load(), BIZ).channels
    assert google_row.actions == ["connect"]
    assert google_row.state_text == "Waiting for you to accept the Google Search invitation"
    assert google_row.account_status == "pending" and google_row.account_public == "123-456-7890"
    assert nb_row.actions == []  # "Nothing to connect": the card has no Connect for it


def test_a_platform_issue_reads_needs_attention_with_the_platforms_own_words(conn):
    live_google_and_waiting_newsbreak(conn)
    words = "Your ad account is under review. Ads will not run until the review is complete."
    conn.execute(
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text) VALUES (%s, %s, 'verification', %s)",
        (BIZ, GOOGLE, words),
    )
    conn.execute(  # fixed already: not shown
        "INSERT INTO channel_issue (business_id, channel, kind, platform_text, resolved_at) "
        "VALUES (%s, %s, 'billing', 'Old words', now())",
        (BIZ, GOOGLE),
    )
    st = state.build(conn, config.load(), BIZ)
    google_row = st.channels[0]
    assert google_row.state_text == f'Needs attention: "{words}"'  # quoted verbatim, never paraphrased
    assert [(i["kind"], i["platform_text"]) for i in google_row.issues] == [("verification", words)]
    assert [(i["channel"], i["label"], i["kind"], i["platform_text"]) for i in st.issues] == [
        (GOOGLE, "Google Search", "verification", words)
    ]
    assert st.channels[1].issues == []


def test_a_rows_one_sentence_is_the_lifecycles_own_clause(conn):
    """Fix round 1 (Minor 10): ads_run says where a row stands in one clause (`state_clause`), which its owner line
    is made of and which the card shows as it is -- no reading of the owner line's punctuation."""
    now = datetime.now(UTC)
    cases = [
        ({"state": "waiting_account", "last_error": ads_run.NO_FUNDS.format(label="NewsBreak")}, "waiting for funds"),
        (
            {"state": "waiting_account", "last_error": "Your account is not linked. Link it."},
            "waiting for your account",
        ),
        ({"state": "waiting_creative", "last_error": None}, "waiting for your image"),
        ({"state": "paused", "paused_at": now, "resources": {"owner_paused": True}}, "paused by you today"),
        (
            {"state": "paused", "paused_at": now, "paused_reason": "month_cap"},
            "paused by Admiry today: " + ads_run.PAUSE_WORDS["month_cap"],
        ),
        ({"state": "limited", "last_error": "Google says: fix it. Soon."}, google.WORDS.held_back),
        ({"state": "failed", "last_error": "Refused. Try again."}, "your ads could not be launched"),
        ({"state": "operator", "last_error": None}, "run by Admiry from the platform's own report"),
        ({"state": "live", "last_error": "An update is on its way. Wait."}, "live"),
    ]
    for fields, clause in cases:
        channel = NB if (fields.get("last_error") or "").startswith("Add funds") else GOOGLE
        row = {"channel": channel, "last_error": None, "resources": {}, "paused_reason": None, **fields}
        assert ads_run.state_clause(row) == clause, fields
        label = registry.label(channel)
        assert ads_run.status_sentence(row).startswith(f"{label}: {clause}"), (fields, ads_run.status_sentence(row))
        if fields["state"] != "live":  # the card's sentence is the clause itself (live reads "Live")
            assert state._state_text(row, label, "none", []) == clause[:1].upper() + clause[1:], fields


def test_what_a_sync_read_from_the_platform_is_among_the_rows_issues(conn):
    """Google's own reasons for holding the ads back and a disapproved ad's policy topics (resources.issues, written
    by ads-sync) are the row's issues, quoted, with the headline of that channel's own ad."""
    plan_id = live_google_and_waiting_newsbreak(conn)
    issues = {
        "campaign": ["MISCONFIGURED"],
        "ads": [{"resource_name": "x", "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}]}],
    }
    conn.execute(
        "UPDATE channel_campaign SET state = 'limited', resources = resources || %s WHERE channel = %s",
        (Jsonb({"issues": issues}), GOOGLE),
    )
    google_row = state.build(conn, config.load(), BIZ).channels[0]
    assert google_row.issues == [
        {"kind": "serving", "platform_text": "MISCONFIGURED"},
        {
            "kind": "disapproval",
            "headline": "Invisalign in Palo Alto",
            "topics": [{"topic": "Alcohol", "type": "PROHIBITED"}],
        },
    ]
    assert google_row.state_text == "Live, but Google is holding your ads back"
    assert google_row.actions == ["pause"]
    assert google_row.sentence.endswith("Google Search needs a change to one of your ads.")
    assert plans.get(conn, BIZ, plan_id) is not None


def test_a_channel_nothing_runs_on_is_left_out(conn):
    live_google_and_waiting_newsbreak(conn)
    plan_id = plans.latest(conn, BIZ)["id"]
    campaign(conn, "reddit", plan_id, "operator")  # no adapter registered for it
    assert [r.channel for r in state.build(conn, config.load(), BIZ).channels] == [GOOGLE, NB]


def test_the_state_is_json_and_carries_no_secret(conn):
    live_google_and_waiting_newsbreak(conn)
    registry.save_account(conn, BIZ, NB, mode="managed", status="none", external={}, secret="tok_SECRET_nb")
    st = state.build(conn, config.load(), BIZ)
    dumped = json.dumps(st.model_dump(mode="json")) + json.dumps(st.cards())
    assert "tok_SECRET_nb" not in dumped and "secret" not in dumped
    accounts = json.dumps([c.model_dump() for c in state.connections(conn, config.load(), BIZ)])
    assert "tok_SECRET_nb" not in accounts and "secret" not in accounts  # read with the account, never kept
    assert json.loads(json.dumps(st.cards()))["channels"]["rows"][0]["channel"] == GOOGLE


def test_a_later_key_does_not_break_the_model():
    """Room for a later plan's key (the support session's `feedback`): the model is not closed."""
    st = state.MarketingState(
        business={"id": BIZ, "name": None, "verified": False, "claim_state": "claimed"},
        plan=None,
        budget=None,
        economics=None,
        rationale=None,
        ads_preview=None,
        plan_id=None,
        execution=None,
        channels=[],
        issues=[],
        feedback={"open": 1},
    )
    assert st.version == 1


# ---------------------------------------------------------------- the accounts (connections)


def test_every_registered_channel_has_a_connection_in_card_order(conn, newsbreak_on):
    business_with_key(conn, claim_state="claimed")
    got = state.connections(conn, config.load(), BIZ)
    assert [c.channel for c in got] == [c for c in registry.ORDER if registry.known(c)] == [GOOGLE, NB]
    g, nb = got
    assert g.public() == {
        "channel": GOOGLE,
        "label": "Google Search",
        "mode": "own_account",
        "status": "none",
        "account_public": None,
        "checked_at": None,
        "next_step": google.CONNECT_OWN_ACCOUNT,
    }
    assert g.steps == [{"text": google.CONNECT_OWN_ACCOUNT, "url": f"http://testserver/?setup={BIZ}"}]
    assert nb.steps == [{"text": newsbreak.CONNECT, "url": None}]
    # nothing to connect: what NewsBreak waits for is its account's own reason (W3)
    assert (g.ready, nb.ready, nb.mode, nb.status, nb.next_step) == (
        False,
        False,
        "managed",
        "none",
        "Add funds to start NewsBreak",
    )


def test_a_channel_with_nothing_to_connect_has_what_it_waits_for_as_its_next_step(conn, newsbreak_on):
    """M1 follow-up 2, W3 (found by the G1 session): NewsBreak's step carries no link -- the owner has nothing to
    connect -- so while it is not ready its next step is what its account waits for, never the step's own words:
    funds while the balance is empty, then, with money on the balance, NewsBreak getting the account ready (an
    operator's work: never "Add funds"). Google Search's own account, a step with a link to follow, keeps its step."""
    business_with_key(conn, claim_state="claimed")
    g, nb = state.connections(conn, config.load(), BIZ)
    assert (nb.next_step, g.next_step) == ("Add funds to start NewsBreak", google.CONNECT_OWN_ACCOUNT)
    funds(conn)
    waiting = {"account_status": "PENDING", "account_checked_at": datetime.now(UTC).isoformat()}  # R11's wait
    registry.save_account(conn, BIZ, NB, mode="managed", status="needs_attention", external=waiting)
    g, nb = state.connections(conn, config.load(), BIZ)
    assert (nb.ready, nb.next_step) == (False, "NewsBreak is getting your account ready; your ads start once it is")
    assert g.next_step == google.CONNECT_OWN_ACCOUNT
    assert all(plain(c.next_step) for c in (g, nb))


def test_a_connected_account_has_no_next_step_and_shows_its_id(conn):
    seed(conn)
    g = state.connections(conn, config.load(), BIZ)[0]
    assert (g.ready, g.status, g.account_public, g.next_step) == (True, "active", "123-456-7890", None)
    assert g.checked_at is not None  # the link's own last check, mirrored


def test_a_funded_managed_channel_is_ready_and_reading_it_writes_nothing(conn, newsbreak_on):
    """NewsBreak's adapter opens its account row the first time a plan funds it; a read never does."""
    seed(conn, status="superseded")
    add_plan_by_hand(conn, consulted(two_channels()), status="draft")
    count = "SELECT count(*) AS n FROM channel_account WHERE channel = 'newsbreak'"
    assert state.connections(conn, config.load(), BIZ)[1].ready is False
    funds(conn)
    assert state.connections(conn, config.load(), BIZ)[1].ready is True
    state.build(conn, config.load(), BIZ)
    assert conn.execute(count).fetchone()["n"] == 0


def test_a_switched_off_channel_is_listed_only_where_the_business_already_has_it(conn):
    """Final review I2 (c): NewsBreak's switch off (unset here), Settings' rows, the setup cards' steps and
    get_connection_status leave it out -- unless the business already has its account row or a row of its ads there,
    which keep it listed; Google Search is listed whatever its own switch says."""
    business_with_key(conn, claim_state="claimed")
    listed = [c.channel for c in state.connections(conn, config.load(), BIZ)]
    assert listed == [GOOGLE]
    registry.save_account(conn, BIZ, NB, mode="managed", status="none", external={})
    assert [c.channel for c in state.connections(conn, config.load(), BIZ)] == [GOOGLE, NB]
    conn.execute("DELETE FROM channel_account WHERE channel = %s", (NB,))
    campaign(conn, NB, "pl_x", "waiting_account")
    assert [c.channel for c in state.connections(conn, config.load(), BIZ)] == [GOOGLE, NB]


def test_google_searchs_mode_falls_back_to_the_business_until_its_row_exists(conn):
    business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    assert state.connections(conn, config.load(), BIZ)[0].mode == "managed"  # no row yet: business.ads_mode
    registry.save_account(conn, BIZ, GOOGLE, mode="own_account", status="none", external={})
    assert state.connections(conn, config.load(), BIZ)[0].mode == "own_account"  # the row is the truth (P6)
