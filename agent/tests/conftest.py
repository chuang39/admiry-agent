"""Fixtures for the agent service tests.

Tests run against a DEDICATED database (default bidx_agent_test, override with
AGENT_TEST_DB_URL), created on demand through the maintenance DB when this module
is imported — before any test module is, so a fresh machine needs no manual
createdb — with the schema applied once per session and every table truncated
before each test. The app under test is pointed at the same database via
AGENT_DATABASE_URL — set here, at import time, because agent.app.create_app()
reads the environment when called.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

TEST_DB_URL = os.environ.get("AGENT_TEST_DB_URL", "postgresql:///bidx_agent_test")
os.environ["AGENT_DATABASE_URL"] = TEST_DB_URL
os.environ["AGENT_PUBLIC_BASE_URL"] = (
    "http://testserver"  # not setdefault: a developer's export must not change the URL assertions
)
# unset = the public base above (a developer's export must not split the hosts); tests that split them set it
os.environ.pop("AGENT_WEB_BASE_URL", None)
# Owner-side tokens are sealed under this key (agent/secretbox.py, channel_account.secret_enc): a fixed test value of
# 64 hex characters (the shape of `openssl rand -hex 32`), set for every test; tests of the unset case delete it
TEST_ENCRYPTION_KEY = "0123456789abcdef" * 4
os.environ["AGENT_KEY_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
# the switch per channel (AGENT_EXEC_<CHANNEL>, D81): a developer's export must not switch a channel on in the tests
for _name in [n for n in os.environ if n.startswith("AGENT_EXEC_")]:
    os.environ.pop(_name)
for _name in (
    "GOOGLE_ADS_MCC_ID",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_LINK_MODE",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "AGENT_TOPUP_MIN_USD",
    "AGENT_TOPUP_MAX_USD",
    "AGENT_CARD_COST_BPS",
    "AGENT_CARD_COST_FIXED_CENTS",
    "AGENT_ACH_COST_BPS",
    "AGENT_ACH_COST_CAP_CENTS",
    "AGENT_ACH_TOPUPS",
    "AGENT_CARD_LINK",  # Link by Stripe: off unless a test turns it on
    "AGENT_ADS_EXECUTION",
    "AGENT_ROLES",
    "AGENT_KNOWLEDGE",  # capability layer K3: off unless a test turns it on
    "AGENT_HARNESS",  # capability layer K6 slice 1: off unless a test turns it on (the harness tests do)
    "AGENT_LAUNCH_REVIEW",  # L1: off unless a test turns it on
    "AGENT_ADMIN_EMAILS",  # L1: no admin list unless a test sets one
    "ADS_OPS_DAILY_BUDGET",
    "AGENT_EMAIL_INBOUND",
    "RESEND_API_KEY",
    "RESEND_WEBHOOK_SECRET",
    "AGENT_EMAIL_ADDRESS",
    "AGENT_OPS_EMAIL",
    "AGENT_MAIL_FROM",  # the sender is asserted as its default, Admiry <AGENT_EMAIL_ADDRESS> (D59)
    "AGENT_SUPPORT",
    "AGENT_SUPPORT_REPLY_TO",
    "AGENT_FEEDBACK_PLAN_PAGE_RATE",
    "AGENT_SITE_HELPER_EMAIL",  # A3p-5 batch 2: the help reply's words are asserted with both unset
    "AGENT_SITE_HELP_PROMISE",
    "NEWSBREAK_ACCESS_TOKEN",
    "NEWSBREAK_ORG_ID",
    "AGENT_PAGESPEED_API_KEY",  # A3p-6 batch 1 Q8: a developer's export must never call Google from the tests
    "AGENT_SITES",  # A3p-6 batch 2 Q22: off, as every test written before batch 2 expects; the site tests turn it on
    # the daily digest and the Analyst (plan 2026-10-07-agent-k2 Task 1): off and their defaults unless a test sets them
    "AGENT_DIGEST",
    "AGENT_DIGEST_LINK_SECRET",
    "AGENT_DIGEST_LINK_SECRET_PREVIOUS",
    "AGENT_DIGEST_RUN_SECONDS",
    "RESEND_EVENTS_WEBHOOK_SECRET",
    "AGENT_ANALYST_LLM_EFFORT",
    "AGENT_ANALYST_MAX_PER_DAY",
    "AGENT_EVAL_GRADERS",  # K5: the grading page's allowlist; a developer's export must not open it in the tests
):
    # a developer's export must not switch the tests into the api link mode, payments, the ads switch, e-mail or a
    # configured NewsBreak
    os.environ.pop(_name, None)

import mcp.server.auth.routes as _mcp_auth_routes

from agent import db

# The SDK refuses an issuer URL that is neither HTTPS nor localhost/127.0.0.1 (RFC 8414). Starlette's
# TestClient talks to the ASGI app directly (no real network), but its default host -- "testserver" --
# is what AGENT_PUBLIC_BASE_URL is pinned to above, so every test would trip this check when the OAuth
# provider builds its routes (A1b-4b Task 2). A real deployment's public base URL is an operator-set
# https:// host, so relaxing this check here has no effect outside the test process.
_mcp_auth_routes.validate_issuer_url = lambda url: None

# The tools register in the order their modules are first imported, and REGISTRY keeps it (tools/list, the connector
# docs, test_probe's pins). Production imports agent.mcp_app first (agent/app.py), whose last lines import every tool
# module in one fixed order; a test module that imports a tool module before it (test_plans_channels imports
# tools_consult) would register that module's tools ahead of the rest. Imported here, before pytest collects any test
# module, mcp_app decides the order in every run, whichever test files run and in whatever order (final wave 9).
import agent.mcp_app  # noqa: F401 - imported for its registration order, see above
from agent.channels.newsbreak import client as _newsbreak

_REAL_NEWSBREAK_CLIENT = _newsbreak.client  # the `nbapi` fake wraps this; `_no_real_newsbreak` guards every test

# The suite written before the public surface calls commands over /mcp (`call_tool(client, "start", …)`), through
# FastMCP's own validation and error shapes: the gate exposes them in this process only (spec 2026-10-01, plan G1
# Task 13). A test of the surface as an assistant sees it takes the `strict_surface` fixture.
from agent.surface import gate as _surface_gate

_surface_gate.EXPOSE_COMMANDS = True

TABLES = (
    "agent_action",
    "approval_code",
    "proposal",
    "plan",
    "goal",
    "knowledge",
    "oauth_hit",
    "oauth_token",
    "oauth_code",
    "oauth_grant",
    "oauth_txn",
    "oauth_client",
    "api_key",
    "job_run",
    "benchmark",
    "claim_link",
    "login_attempt",
    "web_session",
    "agreement_acceptance",
    "ads_link",
    "wallet_entry",
    "payment_session",
    "email_message",
    "email_link",
    "email_grant",
    "email_thread",
    "consult_session",
    "page_photo",
    "page_preview_file",
    "page_preview",
    "page_version_file",
    "page_version",
    "site_asset_file",
    "site_asset",
    "site_spec",
    "site_blob",
    "site_upload",
    "site_zip",
    "upload_token",
    "page",
    "page_event",
    "lead",
    "conversion_action",
    "ads_campaign",
    "ads_day",
    "channel_account",
    "channel_campaign",
    "metric_daily",
    "channel_issue",
    "allocation",
    "plan_feedback",
    "ticket_event",
    "support_ticket",
    "install_request",
    "listing_offer",
    "google_ads_binding",
    "google_ads_pool",
    "agent_job",
    "digest",
    "insight",
    "eval_grade",
    "research_run",
    "ads_refusal",
    "refusal_summary",
    "refusal_rule",
    "business",
)


def _ensure_test_db() -> None:
    dbname = psycopg.conninfo.conninfo_to_dict(TEST_DB_URL).get("dbname", "bidx_agent_test")
    try:
        psycopg.connect(TEST_DB_URL).close()
        return
    except psycopg.OperationalError:
        pass
    admin = psycopg.connect("postgresql:///postgres", autocommit=True)
    admin.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()


_ensure_test_db()  # at import: pytest loads conftest before collecting (importing) any test module


@pytest.fixture(scope="session", autouse=True)
def _schema():
    with db.connect(TEST_DB_URL) as c:
        db.apply_schema(c)


@pytest.fixture
def conn():
    """A fresh connection to the test DB with every table emptied."""
    with db.connect(TEST_DB_URL) as c:
        c.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        yield c


@pytest.fixture
def client(conn):
    """TestClient over a freshly created app (tables already truncated by `conn`)."""
    from agent.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def strict_surface(monkeypatch):
    """The gate as production runs it: assistants see the public tools and nothing else."""
    monkeypatch.setattr(_surface_gate, "EXPOSE_COMMANDS", False)


RPC_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc_raw(
    client: TestClient,
    method: str,
    params: dict | None = None,
    key: str | None = None,
    req_id: int = 1,
    extra_headers: dict | None = None,
) -> httpx.Response:
    """POST one JSON-RPC request to /mcp (stateless, json_response=True); returns the raw response
    (a missing or dead bearer answers 401, never a JSON-RPC body)."""
    headers = dict(RPC_HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    )


def rpc(
    client: TestClient,
    method: str,
    params: dict | None = None,
    key: str | None = None,
    req_id: int = 1,
    extra_headers: dict | None = None,
    expect: int = 200,
) -> dict:
    """POST one JSON-RPC request to /mcp and return the parsed body, asserting `expect`'s status code."""
    r = rpc_raw(client, method, params, key=key, req_id=req_id, extra_headers=extra_headers)
    assert r.status_code == expect, r.text
    return r.json()


def call_tool(
    client: TestClient,
    name: str,
    arguments: dict | None = None,
    key: str | None = None,
    extra_headers: dict | None = None,
) -> dict:
    """tools/call helper: returns the JSON-RPC `result` ({content, structuredContent?, isError})."""
    body = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, key=key, extra_headers=extra_headers)
    assert "result" in body, body
    return body["result"]


def tool_text(result: dict) -> str:
    return "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def data(result: dict) -> dict:
    """The `data` part of a tool's envelope (spec 2026-09-26 §5)."""
    return result["structuredContent"]["data"]


# Words owners never see -- advertising jargon (JARGON), the account structure's (ACCOUNT_JARGON; "manager account" is
# allowed once, inside the acceptance instructions, and tests count it separately) and the money plumbing's
# (MONEY_JARGON: pages say "balance", "add funds", "spent", "refund"). They live in agent/copy.py since the daily digest
# (plan 2026-10-07-agent-k2 Task 1), where the service checks its own words with them.
from agent.copy import (  # noqa: F401 - re-exported: the tests import them from here
    ACCOUNT_JARGON,
    JARGON,
    MONEY_JARGON,
)


def without_site_header(html: str) -> str:
    """A page without the shared header (spec 2026-09-29-admiry-settings-redesign §3): signed in, its nav names Your
    ads and Insights (soon); the jargon checks are about the page's words."""
    return re.sub(r'<header class="top">.*?</header>', "", html, flags=re.DOTALL)


# The api link mode's settings, and (M1) NewsBreak's; the values are what leak checks look for. NEWSBREAK_ORG_ID is no
# secret: it travels in the query string of ad-account/getGroupsByOrgIds, which httpx's own INFO log line prints, so a
# leak check over the log of a run that lists the ad accounts leaves it out.
ADS_ENV = {
    "GOOGLE_ADS_MCC_ID": "111-222-3333",
    "GOOGLE_ADS_DEVELOPER_TOKEN": "devtok_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN": "1//refresh_SECRET",
    "GOOGLE_OAUTH_CLIENT_ID": "123.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-x",
    "NEWSBREAK_ACCESS_TOKEN": "nbtok_SECRET",
    "NEWSBREAK_ORG_ID": "org_1",
}
# What the leak checks look for: every ADS_ENV value but NewsBreak's Organization id (above).
ADS_SECRETS = tuple(value for name, value in ADS_ENV.items() if name != "NEWSBREAK_ORG_ID")


@pytest.fixture
def google(monkeypatch):
    """Google sign-in mocked: every sign-in succeeds as `.email` (owner@example.com unless a test
    changes it); `.calls` records the arguments. Shared by the /connect tests."""
    from agent import google_auth, web

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-x")
    calls: list = []

    def fake_sign_in(client, **kw):
        calls.append(kw)
        return google_auth.Identity(email=fake_sign_in.email, sub="sub-" + fake_sign_in.email)

    fake_sign_in.email = "owner@example.com"
    monkeypatch.setattr(web.google_auth, "sign_in", fake_sign_in)
    fake_sign_in.calls = calls
    return fake_sign_in


WHSEC = "whsec_test"


@pytest.fixture
def newsbreak_on(monkeypatch):
    """NewsBreak's switch on (validate, as on the dev box). Unset -- as here by default, and prod's compose until M1
    passes -- no plan has NewsBreak and Settings lists it only where a business already has it (final review I2)."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")


@pytest.fixture
def walk_fake(monkeypatch):
    """The model, the crawler and Places faked for the dental walk the surface tests start from
    (surface_helpers.planned): the same canned outputs as test_tools_consult's `fake`. Returns the generator."""
    from agent import crawl, llm, models, places, tools_start
    from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture

    reads = {
        "around three thousand": models.AnswerRead(
            option_id="1000_plus", amount_usd=3000, miles=None, note=None, understood=True, services=[]
        )
    }
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads=reads)
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(
        places,
        "search",
        # the dental walk's own business, as Google Maps lists it: close by its website and its phone (plan
        # 2026-10-03-agent-listing-confirm), so the walk is asked to confirm it and answers gm_1
        lambda q, k, **kw: [
            places.Candidate(
                "ChIJ1",
                "Bright Smile Dental",
                "500 University Ave, Palo Alto, CA",
                37.4419,
                -122.143,
                website="https://brightsmile.example",
                phone="(650) 555-0100",
            )
        ],
    )
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    return gen


@pytest.fixture
def knowledge_on(tmp_path, monkeypatch):
    """Capability layer K3: AGENT_KNOWLEDGE on, with agent/tests/knowledge_fixtures.py's small reviewed tree
    (dentists' compliance, google-search and offers; attorneys' compliance) standing in for agent/knowledge."""
    from agent.tests import knowledge_fixtures as kf

    monkeypatch.setenv("AGENT_KNOWLEDGE", "on")
    return kf.use(monkeypatch, kf.tree(tmp_path / "knowledge"))


@pytest.fixture(autouse=True)
def no_real_places(request, monkeypatch):
    """No test calls Google (plan 2026-10-03-agent-listing-confirm): places.search answers [] unless the test patches
    it; geocode's search_text, built on it, then finds nothing. test_places tests the real one through a mock
    transport."""
    if request.module.__name__.endswith("test_places"):
        return
    from agent import places

    monkeypatch.setattr(places, "search", lambda query, key, **kw: [])


@pytest.fixture
def places_search(monkeypatch):
    """start's lookup of the business (`places.search`) answered from `.found`, a list the test sets; `.queries`
    records what was looked up (plan 2026-10-03-agent-listing-confirm)."""
    from agent import places

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    box = SimpleNamespace(found=[], queries=[])

    def search(query, key, **kw):
        box.queries.append(query)
        return list(box.found)

    monkeypatch.setattr(places, "search", search)
    return box


@pytest.fixture
def places_fake(monkeypatch):
    """Places answers every address put on the map with one Houston address and records the queries (walk fixes 1,
    Task 4: where the ads centre, and an address a profile takes); start's lookup of the business finds nothing by
    name, so the location question is asked (plan 2026-10-03-agent-listing-confirm)."""
    from agent import places

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    calls: list[str] = []

    def search(query, key):
        calls.append(query)
        return places.Candidate("ChIJ2", "123 Main St", "123 Main St, Houston, TX 77002", 29.76, -95.37)

    monkeypatch.setattr(places, "search", lambda query, key, **kw: [])
    monkeypatch.setattr(places, "search_text", search)
    return SimpleNamespace(calls=calls)


@pytest.fixture
def places_none(monkeypatch):
    """Places finds nothing: no business by name, no address on the map."""
    from agent import places

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    monkeypatch.setattr(places, "search", lambda query, key, **kw: [])
    monkeypatch.setattr(places, "search_text", lambda query, key: None)


@pytest.fixture
def pictures(tmp_path, monkeypatch):
    """Admiry's licensed set generated in place of the shipped one (`fixtures.creative_set`: home services and the
    fallback one picture each, dentists two). A plan funds NewsBreak only with a picture for its trade (M1 Task 7 fix
    round 1, item 3): a test that wants NewsBreak in a plan made by the allocator has one. Returns the set's folder."""
    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import creative_set

    root = creative_set(tmp_path / "creative")
    monkeypatch.setattr(creative, "ROOT", root)
    return root


@pytest.fixture
def no_pictures(tmp_path, monkeypatch):
    """Admiry's set with no picture at all -- what the shipped one is until the founder adds them, made explicit so a
    test does not change meaning the day the shipped set gets its first picture."""
    from agent.channels.newsbreak import creative

    root = tmp_path / "empty-set"
    root.mkdir()
    (root / "manifest.json").write_text('{"entries": []}', encoding="utf-8")
    monkeypatch.setattr(creative, "ROOT", root)
    return root


@pytest.fixture
def stripe(monkeypatch):
    """Stripe configured and mocked. POSTs (in `sent`; `form(i)` reads one): Checkout Sessions come back as
    cs_test_<n> and refunds as re_test_<n> for the posted amount (n = the POST's number); `fail` (a Response) answers
    every POST, `fail_on = n` makes only the n-th POST a 402 card error. GET /v1/refunds (in `listed`, never in
    `sent`) lists `refunds[payment_intent]` newest first — the refunds this fake made, plus any a test puts there
    (the Stripe dashboard's); `list_fail` (a Response, or an exception to raise) answers every list instead."""
    from agent import stripe_client

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)
    fake = SimpleNamespace(sent=[], listed=[], refunds={}, fail=None, fail_on=None, list_fail=None)

    def handler(request):
        if request.method == "GET" and request.url.path == "/v1/refunds":
            fake.listed.append(request)
            if isinstance(fake.list_fail, Exception):
                raise fake.list_fail
            if fake.list_fail is not None:
                return fake.list_fail
            made = fake.refunds.get(request.url.params["payment_intent"], [])
            return httpx.Response(200, json={"object": "list", "data": made[::-1], "has_more": False})
        fake.sent.append(request)
        n = len(fake.sent)
        if fake.fail is not None:
            return fake.fail
        if fake.fail_on == n:
            return httpx.Response(402, json={"error": {"message": "This charge has already been refunded."}})
        if request.url.path == "/v1/refunds":
            form = parse_qs(request.content.decode())
            refund = {"id": f"re_test_{n}", "amount": int(form["amount"][0]), "status": "succeeded"}
            fake.refunds.setdefault(form["payment_intent"][0], []).append(refund)
            return httpx.Response(200, json=refund)
        return httpx.Response(200, json={"id": f"cs_test_{n}", "url": f"https://checkout.stripe.com/c/pay/cs_test_{n}"})

    monkeypatch.setattr(stripe_client, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    fake.form = lambda i=-1: {k: v[0] for k, v in parse_qs(fake.sent[i].content.decode()).items()}
    return fake


@pytest.fixture
def ads_api(monkeypatch):
    """Every Google Ads setting present: the link mode is `api`. Returns the settings."""
    for name, value in ADS_ENV.items():
        monkeypatch.setenv(name, value)
    from agent import config

    return config.load()


@pytest.fixture(autouse=True)
def _no_real_newsbreak(monkeypatch):
    """No test reaches NewsBreak itself: since M1, `ads_api` configures NewsBreak too (ADS_ENV), so a test that forgot
    `nbapi` would send the test token to the internet. Every test's NewsBreak client factory refuses to make a client
    without a transport -- through `pytest.fail`, which no broad `except Exception` in the code under test swallows --
    until `nbapi` (or a test's own transport) supplies one."""

    def guarded(*args, transport=None, **kwargs):
        if transport is None:
            pytest.fail("a test reached NewsBreak without the nbapi fixture: the test token would leave the machine")
        return _REAL_NEWSBREAK_CLIENT(*args, transport=transport, **kwargs)

    monkeypatch.setattr(_newsbreak, "client", guarded)


@pytest.fixture
def nbapi(monkeypatch):
    """NewsBreak's Business API faked (`agent/tests/fake_newsbreak.py`) for every client the NewsBreak client module
    makes: its token and Organization set (ADS_ENV's values), and its `client()` factory -- the real one, under the
    guard above -- patched where it lives, with the fake as the transport, so the real request meter still counts.
    The adapter's pause between two requests of one business (one request a second, M1 Task 5) never sleeps here: what
    it was asked to wait is kept in the fake's `waits`, and each test starts with no business paced yet. Returns the
    fake."""
    from agent.channels.newsbreak import adapter as newsbreak_adapter
    from agent.tests.fake_newsbreak import FakeNewsBreak

    for name in ("NEWSBREAK_ACCESS_TOKEN", "NEWSBREAK_ORG_ID"):
        monkeypatch.setenv(name, ADS_ENV[name])
    fake = FakeNewsBreak(token=ADS_ENV["NEWSBREAK_ACCESS_TOKEN"], org_id=ADS_ENV["NEWSBREAK_ORG_ID"])
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(_newsbreak, "client", functools.partial(_REAL_NEWSBREAK_CLIENT, transport=transport))
    monkeypatch.setattr(newsbreak_adapter, "wait", fake.waits.append)
    monkeypatch.setattr(newsbreak_adapter, "_LAST_CALL", {})
    return fake


# The Svix scheme's secret: "whsec_" + base64 of the raw key (agent/mail_auth.py verify_svix).
RESEND_WHSEC = "whsec_" + base64.b64encode(b"k" * 32).decode()


@pytest.fixture
def resend(monkeypatch):
    """The e-mail channel on and Resend mocked (A1b-5). POST /emails lands in `sent` (parsed JSON bodies), its
    Idempotency-Key header in `keys` (keys[i] is sent[i]'s), and answers {"id": "re_<n>"}; GET
    /emails/receiving/<id> answers `received[id]` (a test puts the Receiving API's shape there;
    `raw.download_url` values are served from `raw[url]` as bytes); `fail` (a Response) answers every POST
    instead. Every Resend call in the service goes through mail.client(), the one client (plan P16). `requests` keeps
    every request as sent, headers and bytes (the daily digest's retries compare them)."""
    from agent import mail

    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "on")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_x")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", RESEND_WHSEC)
    monkeypatch.setenv("AGENT_EMAIL_ADDRESS", "hello@agent.test")
    monkeypatch.setenv("AGENT_OPS_EMAIL", "ops@example.com")
    fake = SimpleNamespace(sent=[], keys=[], received={}, raw={}, fail=None, requests=[])

    def handler(request):
        fake.requests.append(request)
        if request.method == "POST" and request.url.path == "/emails":
            fake.sent.append(json.loads(request.content))
            fake.keys.append(request.headers.get("Idempotency-Key"))
            if fake.fail is not None:
                return fake.fail
            return httpx.Response(200, json={"id": f"re_{len(fake.sent)}"})
        if request.method == "GET" and request.url.path.startswith("/emails/receiving/"):
            rid = request.url.path.rsplit("/", 1)[1]
            if rid in fake.received:
                return httpx.Response(200, json=fake.received[rid])
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "GET" and str(request.url) in fake.raw:
            return httpx.Response(200, content=fake.raw[str(request.url)])
        return httpx.Response(404, json={"message": f"unexpected {request.method} {request.url}"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return fake


@pytest.fixture
def support_on(resend, monkeypatch):
    """Tickets and feedback on (spec 2026-09-30 D144) over `resend`'s mock, with the e-mail channel on (ticket mail
    carries the thread's plus-address as Reply-To). Returns the same fake. A test of the channel-off Reply-To sets
    AGENT_EMAIL_INBOUND=off and AGENT_SUPPORT_REPLY_TO itself."""
    monkeypatch.setenv("AGENT_SUPPORT", "on")
    return resend


TEST_LINK_SECRET = "ab" * 32  # the digest's unsubscribe-link key in the tests (the shape of `openssl rand -hex 32`)


@pytest.fixture
def digest_env(monkeypatch, resend):
    """The daily digest on over `resend`'s mock (plan 2026-10-07-agent-k2 Task 1, Dg Task 1): the e-mail channel on,
    AGENT_DIGEST=on with its link key, a model key, and a default FakeGenerator installed whatever override a caller
    passes, so no test reaches the network. Returns the same fake."""
    from agent import llm
    from agent.tests.fixtures import FakeGenerator

    monkeypatch.setenv("AGENT_DIGEST", "on")
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", TEST_LINK_SECRET)
    monkeypatch.setenv("META_MODEL_API_KEY", "test")
    gen = FakeGenerator()
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    return resend


@pytest.fixture
def analyst_biz(conn, monkeypatch):
    """A Los Angeles business with an approved $600 Google Search plan, live since September and synced after local
    midnight: on Monday 2026-10-05 it spent $23.10 on 41 clicks with 2 calls and 1 form. The clock reads 08:02 on
    Tuesday in Los Angeles and the roles are on (capability layer K2, plan 2026-10-07-agent-k2 Task 7)."""
    from datetime import UTC, datetime

    from agent import ads_run, benchmark
    from agent.tests import digest_fixtures as df

    monkeypatch.setattr(ads_run, "now", lambda: df.ANALYST_NOW)
    monkeypatch.setenv("AGENT_ROLES", "on")
    benchmark.seed(conn)
    df.seed_business(conn, "biz_1", zone="America/Los_Angeles")
    df.seed_plan(conn, "biz_1", {"google_search": 600})
    df.seed_channel(
        conn,
        "biz_1",
        launched_at=datetime(2026, 9, 1, tzinfo=UTC),
        synced_at=df.ANALYST_NOW,
        resources={"campaign": "customers/1/campaigns/2", "time_zone": "America/Los_Angeles"},
    )
    df.seed_metric(conn, "biz_1", df.ANALYST_DAY, cost_usd=23.10, clicks=41, impressions=900, calls=2)
    df.seed_lead(conn, "biz_1", datetime(2026, 10, 5, 18, tzinfo=UTC))
    return "biz_1"
