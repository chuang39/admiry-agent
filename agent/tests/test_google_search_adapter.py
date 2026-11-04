"""agent/channels/google_search: A3's Google modules re-homed behind the channel protocol (plan
2026-09-29-agent-m0-channel-framework P1, Task 2). The adapter's account half -- what it can do, how the owner connects,
whether the link is ready -- the client it hands out, and the old module paths, which still lead to the moved code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agent import ads_conversions, ads_launch, ads_run, config, google_ads, google_ads_link, google_auth, wallet
from agent.channels import base, registry
from agent.channels.google_search import adapter as google_adapter
from agent.channels.google_search import build, conversions
from agent.channels.google_search import client as gclient
from agent.channels.google_search.adapter import ADAPTER, GoogleSearchAdapter
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON

BIZ = "biz_1"
CID = "1234567890"
OWN_ACCOUNT_STEP = (
    "Give Admiry your 10-digit Customer ID; accept the invitation in Google Ads (Admin → Access and security)"
)
MANAGED_STEP = (
    "Nothing to connect: Admiry runs Google Search for you in its own Google Ads account, paid from your balance"
)


def _business(conn, biz: str = BIZ) -> None:
    conn.execute("INSERT INTO business (id) VALUES (%s) ON CONFLICT DO NOTHING", (biz,))


def _link(conn, status: str, biz: str = BIZ, cid: str = CID) -> None:
    _business(conn, biz)
    google_ads_link.save(conn, biz, cid, status=status)


def _account(mode: str = "own_account", biz: str = BIZ) -> base.Account:
    return base.Account(business_id=biz, channel="google_search", mode=mode, status="none", external={}, secret=None)


def _no_jargon(sentence: str) -> None:
    for jargon in (JARGON, ACCOUNT_JARGON, MONEY_JARGON):
        assert not jargon.search(sentence), sentence


# ---------------------------------------------------------------- registration


def test_importing_the_channels_package_registers_google_search():
    assert isinstance(ADAPTER, GoogleSearchAdapter) and isinstance(ADAPTER, base.Adapter)
    assert ADAPTER.channel == "google_search"
    assert registry.CHANNELS["google_search"] is ADAPTER
    assert registry.adapter("google_search") is ADAPTER
    assert registry.known("google_search")


# ---------------------------------------------------------------- capabilities and connect steps


def test_capabilities_are_what_the_allocator_and_the_cards_read():
    assert ADAPTER.capabilities() == base.Capabilities(
        create=True,
        update=True,
        pause=True,
        metrics=True,
        conversions=False,  # the conversion upload is A3p-3
        leads=False,
        modes={"own_account", "managed"},  # managed: a pool account of Admiry's, by industry (A3c-1)
        min_monthly_usd=100,
        managed_min_monthly_usd=200,  # founder 2026-10-03: prepaid Google Search from $200
        geo="radius",
        lead_formats={"call", "form"},
        needs_image=False,
        validate="server",  # validateOnly
        follows_page=True,  # its ads carry the page's address and move with it (A3p-2; M0 Task 9 D4)
    )


def test_connect_steps_for_the_owners_own_account_ask_for_the_customer_id_and_the_invitation():
    assert ADAPTER.connect_steps(None, "own_account") == [base.Step(OWN_ACCOUNT_STEP, None)]


def test_connect_steps_point_at_the_businesss_setup_when_there_is_an_account():
    steps = ADAPTER.connect_steps(_account(), "own_account")
    assert steps == [base.Step(OWN_ACCOUNT_STEP, f"{config.load().web_base_url}/?setup={BIZ}")]


def test_connect_steps_for_a_prepaid_business_say_there_is_nothing_to_connect():
    assert ADAPTER.connect_steps(_account("managed"), "managed") == [base.Step(MANAGED_STEP)]
    assert ADAPTER.connect_steps(None, "managed") == [base.Step(MANAGED_STEP)]


def test_connect_steps_carry_no_jargon():
    for step in (*ADAPTER.connect_steps(_account(), "own_account"), *ADAPTER.connect_steps(_account(), "managed")):
        _no_jargon(step.text)


# ---------------------------------------------------------------- check_account


@pytest.mark.parametrize(
    ("row_status", "ready", "status", "reason"),
    [
        ("active", True, "active", None),
        ("manual", True, "active", None),  # an operator's word is simply active to everyone else
        ("pending", False, "pending", google_adapter.LINK_PENDING),
        ("refused", False, "refused", google_adapter.NOT_LINKED),
        ("cancelled", False, "cancelled", google_adapter.NOT_LINKED),
        ("inactive", False, "inactive", google_adapter.NOT_LINKED),
    ],
)
def test_check_account_follows_the_link_row(conn, row_status, ready, status, reason):
    _link(conn, row_status)
    got = ADAPTER.check_account(conn, config.load(), _account())
    # the Customer ID with dashes, as the statement shows it: the owner recognises it on every row that has one
    assert got == base.AccountStatus(ready, status, reason, "123-456-7890")


def test_check_account_without_a_link_row_is_not_linked(conn):
    _business(conn)
    got = ADAPTER.check_account(conn, config.load(), _account())
    assert got == base.AccountStatus(False, "none", google_adapter.NOT_LINKED, None)


def test_check_account_without_an_account_is_not_linked(conn):
    assert ADAPTER.check_account(conn, config.load(), None) == base.AccountStatus(
        False, "none", google_adapter.NOT_LINKED, None
    )


def test_check_account_reads_the_row_of_the_business_it_is_asked_about(conn):
    _link(conn, "active", "biz_2", "9876543210")
    _business(conn)  # biz_1 has no link
    assert ADAPTER.check_account(conn, config.load(), _account()).ready is False
    assert ADAPTER.check_account(conn, config.load(), _account(biz="biz_2")).account_public == "987-654-3210"


def test_check_account_of_a_prepaid_business_is_ready_and_names_only_an_account_of_its_own(conn):
    """A3c-1: the account is Admiry's (a pool account, picked by the launch): ready with a balance, before it is
    picked (`none`) and after (`active`); named to the owner only when it is the business's alone (capacity 1) -- a
    link left over from before the switch changes nothing."""
    _link(conn, "active")
    got = ADAPTER.check_account(conn, config.load(), _account("managed"))
    no_funds = ads_run.NO_FUNDS.format(label="Google Search")
    assert got == base.AccountStatus(False, "none", no_funds, None)  # the balance first, as NewsBreak's
    wallet.add_entry(conn, "biz_1", kind="topup", amount_cents=20_000, ref="pi_1", created_by="stripe")
    got = ADAPTER.check_account(conn, config.load(), _account("managed"))
    assert got == base.AccountStatus(True, "none", None, None)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    conn.execute(
        "INSERT INTO google_ads_pool (customer_id, scope, added_by) VALUES ('5555555555', 'root', 'ops:cli');"
        "INSERT INTO google_ads_binding (business_id, customer_id, family, bound_by) "
        "VALUES ('biz_1', '5555555555', 'dentists', 'job:allocator')"
    )
    got = ADAPTER.check_account(conn, config.load(), _account("managed"))
    assert got == base.AccountStatus(True, "active", None, None)  # shared by an industry: never named
    conn.execute("UPDATE google_ads_pool SET capacity = 1")  # the business's alone: its number is the owner's to see
    got = ADAPTER.check_account(conn, config.load(), _account("managed"))
    assert got == base.AccountStatus(True, "active", None, "555-555-5555")


def test_check_account_reasons_are_the_two_owner_sentences_and_carry_no_jargon(conn):
    reasons = set()
    for status in ("pending", "refused", "cancelled", "inactive"):
        _link(conn, status)
        reasons.add(ADAPTER.check_account(conn, config.load(), _account()).reason)
    assert reasons == {google_adapter.LINK_PENDING, google_adapter.NOT_LINKED}
    for reason in reasons:
        _no_jargon(reason)


# ---------------------------------------------------------------- the client


def test_the_adapters_client_is_its_own_factorys(monkeypatch):
    made: list[dict] = []
    monkeypatch.setattr(google_adapter, "client", lambda **kw: made.append(kw) or "a client")
    assert ADAPTER.client(budget_s=3.0) == "a client"
    assert ADAPTER.client() == "a client"
    assert made == [{"budget_s": 3.0}, {"budget_s": None}]


def test_the_adapters_client_counts_every_operation_it_sends(ads_api):
    """Ruling S4, moved with the client (M0 Task 4): Explorer's daily quota counts each search request and each
    mutate operation; the token exchange is not one. A client the adapter did not make counts nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == google_auth.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.x", "expires_in": 3599})
        return httpx.Response(200, json={"results": [], "mutateOperationResponses": []})

    with google_adapter.client(transport=httpx.MockTransport(handler)) as http:
        assert ADAPTER.ops_sent(http) == 0
        gclient.search(ads_api, CID, "SELECT campaign.id FROM campaign", http=http)
        gclient.mutate(ads_api, CID, [{"a": {}}, {"b": {}}, {"c": {}}], validate_only=False, http=http)
        assert ADAPTER.ops_sent(http) == 4
    with httpx.Client() as other:
        assert ADAPTER.ops_sent(other) == 0


def test_the_adapters_client_is_a_real_http_client_that_the_caller_closes():
    with ADAPTER.client() as http:
        assert isinstance(http, httpx.Client)
    with ADAPTER.client(budget_s=5) as http:
        assert isinstance(http, httpx.Client)


# ---------------------------------------------------------------- the execution half (M0 Task 4)


@pytest.mark.parametrize(("method", "positional"), [("push_conversions", 3), ("pull_leads", 3)])
def test_what_google_search_does_not_do_here_refuses_rather_than_pretends(method, positional):
    """The conversion upload is A3p-3's; Google Search has no lead forms to pull. Its capabilities say so (the
    lifecycle never calls them); a caller that forgets gets Unsupported."""
    with pytest.raises(base.Unsupported):
        getattr(ADAPTER, method)(*[None] * positional)


def test_its_owner_words_are_a3s_and_carry_no_jargon():
    words = ADAPTER.words
    assert (words.pause_owed, words.refused_change, words.no_fee) == (
        google_adapter.PAUSE_OWED,
        google_adapter.REFUSED_CHANGE,
        google_adapter.NO_FEE,
    )
    assert words.checked == "had Google check your ads (a test run: nothing is shown or spent)"
    assert words.repaused == "finished pausing your ads in Google Ads"
    # M1 Task 7's words are a channel Admiry runs (NewsBreak's); A3c-1 adds the wait for room in Admiry's own account
    assert words.account_wait == google_adapter.ACCOUNT_OPENING
    assert (words.paused_there, words.paused_badge, words.month_note, words.image_wait) == (None, None, None, None)
    for sentence in vars(words).values():
        if sentence is not None:
            _no_jargon(sentence)


# ---------------------------------------------------------------- the old paths


@pytest.mark.parametrize(
    ("old", "new", "private"),
    [
        (google_ads, gclient, ("_access_token", "_call", "_codes", "_failure", "_json", "_session")),
        (ads_launch, build, ("_phrase",)),
        (ads_conversions, conversions, ("_upsert",)),
    ],
    ids=["google_ads", "ads_launch", "ads_conversions"],
)
def test_an_old_module_path_hands_out_the_moved_modules_own_objects(old, new, private):
    public = [name for name in vars(new) if not name.startswith("_")]
    assert public
    for name in (*public, *private):
        assert getattr(old, name) is getattr(new, name), name


IMPORT_ORDERS = """
import importlib, sys, traceback

failures = []
for entry in sys.argv[1:]:
    for name in [m for m in sys.modules if m == "agent" or m.startswith("agent.")]:
        del sys.modules[name]  # a fresh start for the agent package; the third-party modules stay loaded
    try:
        importlib.import_module(entry)
    except Exception:
        failures.append(entry + "\\n" + traceback.format_exc())
print("\\n".join(failures))
sys.exit(1 if failures else 0)
"""

ENTRY_POINTS = [
    "agent.channels",
    "agent.channels.registry",
    "agent.channels.google_search.client",
    "agent.channels.google_search.build",
    "agent.channels.google_search.conversions",
    "agent.channels.google_search.adapter",
    "agent.channels.newsbreak.adapter",
    "agent.google_ads",
    "agent.ads_launch",
    "agent.ads_conversions",
    "agent.google_ads_link",
    "agent.ads_run",
    "agent.plans",
    "agent.plans_channels",
    "agent.allocate",
    "agent.landing",
    "agent.jobs",
    "agent.ops",
    "agent.setup",
    "agent.state",  # M0 Task 6: the read model, and the channel tools over it
    "agent.tools_channels",
    "agent.mcp_app",
    "agent.app",
]


def test_every_entry_point_imports_first_whatever_the_order():
    """The package registers its adapter when it is imported, and the modules the adapter serves import the package:
    whichever of them a process (the service, a job, an ops command) imports first, none meets another half-loaded."""
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        [sys.executable, "-c", IMPORT_ORDERS, *ENTRY_POINTS],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
