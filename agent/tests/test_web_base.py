"""Two base URLs (spec 2026-09-29 §3): browser pages, links and cookies on AGENT_WEB_BASE_URL (admiry.ai);
MCP, the OAuth issuer and the connector docs on AGENT_PUBLIC_BASE_URL (agent.admiry.ai)."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from mcp.server.auth.provider import AuthorizationParams
from pydantic import AnyHttpUrl

from agent import (
    agreements,
    auth,
    benchmark,
    claims,
    config,
    consult,
    home_api,
    keys,
    landing,
    legal,
    mcp_app,
    oauth,
    plans,
    sessions,
    setup,
    web,
)
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_mail_dispatch import claim_and_plan, last_sent, reply_to, thread_row, walk_to_ready
from agent.tests.test_mail_dispatch import fake as mail_fake  # noqa: F401 -- a pytest fixture, used by name
from agent.tests.test_mail_in import inbound
from agent.tests.test_oauth_provider import _client
from agent.tests.test_tools_start import fake  # noqa: F401 -- a pytest fixture, used by name

WEB = "https://web.example"
AGENT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def split(monkeypatch):
    monkeypatch.setenv("AGENT_WEB_BASE_URL", WEB + "/")  # trailing slash is stripped


def test_unset_web_base_falls_back_to_the_public_base():
    s = config.load()
    assert s.web_base_url == s.public_base_url == "http://testserver"


def test_browser_links_use_the_web_base(split, conn):
    s = config.load()
    assert s.web_base_url == WEB and s.public_base_url == "http://testserver"
    assert web.redirect_uri(s) == f"{WEB}/connect/google/callback"
    assert plans.plan_url("pl_0123456789abcdef") == f"{WEB}/plan/pl_0123456789abcdef"
    conn.execute("INSERT INTO business (id, claim_state, owner_email) VALUES ('biz_1', 'claimed', 'owner@example.com')")
    url, _ = claims.new_link(conn, "biz_1")
    assert url.startswith(f"{WEB}/connect?claim=")


def test_cookies_follow_the_web_base(split):
    s = config.load()  # web https, public http: Secure must follow the host that sets the cookie
    assert sessions.cookie_args(s)["secure"] is True
    assert sessions.login_cookie_args(s)["secure"] is True
    assert home_api.cookie_args(s, max_age=60)["secure"] is True
    assert home_api.biz_cookie_args(s, max_age=60)["secure"] is True


def test_consent_url_is_on_the_web_base_and_mcp_stays_public(split, conn):
    # imported here, after home_api has loaded agent.mcp_app: imported first (this file run alone), connect's
    # connect -> mcp_app -> surface.told -> connect cycle fails
    from agent import connect

    p = oauth.AdmiryOAuthProvider(base_url="http://testserver")
    asyncio.run(p.register_client(_client()))
    url = asyncio.run(
        p.authorize(
            _client(),
            AuthorizationParams(
                state="st",
                scopes=["read"],
                code_challenge="c" * 43,
                redirect_uri=AnyHttpUrl("https://claude.ai/api/mcp/auth_callback"),
                redirect_uri_provided_explicitly=True,
                resource="http://testserver/mcp",
            ),
        )
    )
    assert url.startswith(f"{WEB}/oauth/consent?txn=txn_")
    ctx = connect._context()
    assert ctx["mcp_url"] == "http://testserver/mcp" and ctx["base"] == "http://testserver"


# Setup in conversation (spec 2026-09-29 setup-in-conversation): the owner sets up ads at /?setup=<business> in the
# home, so those links and Stripe's return are browser URLs like any other and belong on the web host.


def test_the_ads_tools_send_the_owner_to_the_setup_step_on_the_web_base(split, client, conn):
    from agent.tests.test_tools_ads import owner_key

    key = owner_key(conn, agree=False)
    args = {"customer_id": "123-456-7890", "idempotency_key": "l1"}
    res = call_tool(client, "connect_google_ads", args, key=key)["structuredContent"]["data"]
    assert (res["status"], res["connect_url"]) == ("needs_agreement", f"{WEB}/?setup=biz_1")
    res = call_tool(client, "get_connection_status", {}, key=key)["structuredContent"]["data"]
    assert res["connect_url"] == f"{WEB}/?setup=biz_1"


def test_payment_links_and_the_stripe_return_are_on_the_web_base(split, client, conn, stripe):
    from agent.tests.test_tools_wallet import prepaid_key

    key = prepaid_key(conn, terms=False)
    res = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert (res["pay_url"], res["connect_url"]) == (None, f"{WEB}/?setup=biz_1")  # the terms come first
    for document in (legal.AGREEMENT, legal.ADDENDUM):
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=document)
    res = call_tool(client, "request_payment_link", {}, key=key)["structuredContent"]["data"]
    assert res["pay_url"] == f"{WEB}/?setup=biz_1"
    setup.pay(config.load(), setup.Owner(email="owner@example.com", ip=None), "biz_1", "300")
    form = stripe.form()
    assert form["success_url"] == f"{WEB}/?setup=biz_1&paid=1"
    assert form["cancel_url"] == f"{WEB}/?setup=biz_1&cancelled=1"


def test_the_agree_page_and_its_stripe_return_are_on_the_web_base(split, client, conn, stripe):
    """The first payment's page (founder's ruling 3, 2026-10-13): the link an assistant gets, and Stripe's way back."""
    from agent.tests.test_ads_run import seed
    from agent.tests.test_review_launch import draft_sections

    key, plan_id = seed(conn, status="draft", secs=draft_sections(), link=None)
    assert setup.agree_url(config.load(), plan_id) == f"{WEB}/agree/{plan_id}"
    args = {"channel": "google_search", "mode": "managed", "idempotency_key": "m1"}
    res = call_tool(client, "connect_channel", args, key=key)
    assert data(res)["connect_url"] == f"{WEB}/agree/{plan_id}"
    owner = setup.Owner(email="owner@example.com", ip=None)
    ticks = {d: agreements.version(d) for d in (legal.AGREEMENT, legal.ADDENDUM)}
    setup.agree_and_pay(config.load(), owner, "biz_1", plan_id, ticks)
    form = stripe.form()
    assert form["success_url"] == f"{WEB}/agree/{plan_id}?paid=1" and form["cancel_url"] == f"{WEB}/agree/{plan_id}"


def test_the_tools_send_the_owner_to_settings_on_the_web_base(split, client, conn):
    key = business_with_key(conn, claim_state="claimed")
    assert data(call_tool(client, "whoami", key=key))["connect_url"] == f"{WEB}/connect"
    assert data(call_tool(client, "request_connect_link", key=key))["connect_url"] == f"{WEB}/connect"  # claimed
    args = {"plan_id": "pl_0123456789abcdef", "idempotency_key": "a1"}
    res = data(call_tool(client, "approve_plan", args, key=key))
    assert res == {"status": "needs_verification", "verify_url": f"{WEB}/connect"}
    draft, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    for tool, args in (
        ("approve_plan", {"plan_id": "pl_0123456789abcdef", "idempotency_key": "a2"}),
        ("connect_google_ads", {"customer_id": "123-456-7890", "idempotency_key": "l1"}),
    ):
        res = call_tool(client, tool, args, key=draft)  # the setup key's refusals name where setup finishes
        assert res["isError"] and f"setup at {WEB}/connect" in tool_text(res), tool
    with pytest.raises(auth.Denied, match=re.escape(f"{WEB}/connect")):  # a keyless in-process call: MISSING_KEY
        mcp_app.run("whoami", "read", {}, lambda conn, p: {})


def test_start_sends_the_owner_to_settings_on_the_web_base(split, client, conn, fake):  # noqa: F811 -- the fixture
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    started = data(call_tool(client, "start", {"text": "dentist in Palo Alto, $2,000 a month"}, key=key))
    assert started["connect_url"] == f"{WEB}/connect"  # a keyed start
    token = consult.mint_handoff(conn, started["session_id"])
    moved = data(call_tool(client, "start", {"handoff": token}, key=key))
    assert moved["session_id"] == started["session_id"] and moved["connect_url"] == f"{WEB}/connect"  # a hand-off


# Every remaining public_base_url use must be an MCP/docs one. A new browser link built from it fails here. Each
# allowed line is allowed only in its own file, so an alias (`base = settings.public_base_url`) elsewhere fails too.
ALLOWED_PUBLIC = (
    ("config.py", re.compile(r"public_base_url: str$")),  # Settings
    ("config.py", re.compile(r"public_base_url=public_base,$")),  # load
    ("connect.py", re.compile(r"base = config\.load\(\)\.public_base_url$")),  # _context (docs, mcp_url)
    ("pages.py", re.compile(r"base = settings\.public_base_url$")),  # home_page (mcp prompts)
    ("web.py", re.compile(r'return "" if settings\.web_base_url != settings\.public_base_url else WEBSITE$')),  # split?
    ("home_api.py", re.compile(r"base = settings\.public_base_url$")),  # the handoff (mcp prompts)
    (
        "web.py",
        re.compile(r"render\(\{\"base\": settings\.public_base_url"),
    ),  # page -> connect_docs template (help button Task 3: the dict form)
    ("web.py", re.compile(r"paste_prompt\(settings\.public_base_url\)")),  # the connector paste prompt
    ("mcp_app.py", re.compile(r"AdmiryOAuthProvider\(base_url=config\.load\(\)\.public_base_url\)")),  # issuer
    # Admiry Pages: the form action and the tracking snippet live on the owner's own site, cookie-free
    ("landing.py", re.compile(r'form_action = f"\{settings\.public_base_url\}/t/form/')),
    ("landing.py", re.compile(r'"snippet": snippet\(page\["id"\], settings\.public_base_url, tag\)')),
    # A3p-2 Task 2: a tenant page's CSP (H6) trusts the agent host for scripts, the beacon and the tracking
    # form's post -- the same host the snippet above already names, never the browser-facing web base.
    ("hosted.py", re.compile(r"^agent_base = settings\.public_base_url")),
    # A3p-2 Task 3: apply_publish_page's CSP/snippet host, the same call hosted.py's own line above makes.
    ("proposals.py", re.compile(r"^agent_base = settings\.public_base_url$")),
    # A3p-2 Task 3: preview_page's own sanitise() call passes the same agent-host CSP parameter.
    ("tools_hosting.py", re.compile(r'own_host=page\.get\("hostname"\), agent_base=settings\.public_base_url$')),
    # A3p-2 Task 5: an Admiry-written page's form posts to, and its snippet loads from, the agent host -- the kit's own.
    ("page_builder.py", re.compile(r"^agent_base = settings\.public_base_url$")),
    # A3p-2 Task 3: a preview's link is deliberately the agent host (H3: previews never use the pages domain),
    # not a browser-facing web-base page -- preview_page and publish_page's own summary both build it this way.
    ("tools_hosting.py", re.compile(r'preview_url = f"\{settings\.public_base_url\}/p/\{token\}"$')),
    # A3p-2 Task 3 round 5 (H29): set_landing_page refuses Admiry's own hosts, the agent's and the browser's -- a
    # host comparison, never a link.
    (
        "tools_pages.py",
        re.compile(
            r"^if name in \{urlparse\(settings\.public_base_url\)\.hostname, "
            r"urlparse\(settings\.web_base_url\)\.hostname\}:$"
        ),
    ),
    # A3p-2 Task 6: after a photo change, the fresh preview's own link -- the same agent-host address
    # preview_page/publish_page's summary already builds above (H3: previews never use the pages domain);
    # photos change the page, never which host previews it.
    ("uploads.py", re.compile(r'^preview_url = f"\{config\.load\(\)\.public_base_url\}/p/\{preview_token\}"$')),
    # A3p-2 Task 7: /api/page/state's own fresh-preview link, for a returning visitor -- the same agent-host
    # address every other preview link above builds; the homepage frames it (H16), it never navigates there.
    ("home_api.py", re.compile(r'^return f"\{settings\.public_base_url\}/p/\{token\}", token$')),
    # A3p-2 Task 7 (H36): the homepage's own CSP trusts the agent host as a frame-src -- where /p/{token}
    # previews are served -- the same host name every CSP-building line above already trusts, never a link.
    ("pages.py", re.compile(r'^f"frame-src \{settings\.public_base_url\}; connect-src')),
    # A3p-2 Task 7 review (H40): _already_published_url's own idempotency check re-derives the tracking
    # snippet exactly as apply_publish_page does, to compare -- the same agent-host snippet, never a link.
    ("proposals.py", re.compile(r"^snippet = landing\.snippet\(page_id, settings\.public_base_url, tag\)$")),
    # A3p-2 final review (H42): a preview and its photos answer on the agent host only -- a host comparison,
    # never a link.
    (
        "hosted.py",
        re.compile(r"^return _clean_host\(host\) == _clean_host\(urlsplit\(settings\.public_base_url\)\.netloc\)$"),
    ),
    # H42: the upload page (a browser page, on the web host) shows its thumbnails from the agent host by full address
    # -- a preview's own photos, which answer nowhere else -- and its CSP names that host for images; never a link.
    ("uploads.py", re.compile(r"^agent_base = config\.load\(\)\.public_base_url$")),
    # A3p-3 batch 3: a site bundle's one-time upload link and its source's one-hour zip are agent-host capabilities
    # (PUT /s/<token>, GET /s/zip/<token>, a command line's or an assistant's, never a browser page); the link's answer
    # and the upload's check carry the kit's own snippet, form address and CSP host.
    (
        "tools_hosting.py",
        re.compile(r'^link = site_upload\.new_link\(conn, biz, page\["id"\], settings\.public_base_url\)$'),
    ),
    ("tools_hosting.py", re.compile(r'^"snippet": landing\.snippet\(page\["id"\], settings\.public_base_url, tag\),$')),
    ("tools_hosting.py", re.compile(r'^"form_action": f"\{settings\.public_base_url\}/t/form/\{page\[\'id\'\]\}",$')),
    (
        "site_upload.py",
        re.compile(r'^"snippet": landing\.snippet\(row\["page_id"\], settings\.public_base_url, tag\),$'),
    ),
    ("site_upload.py", re.compile(r"^agent_base=settings\.public_base_url,$")),
    # A3p-5 batch 2 Task 2: the homepage's "Your website" card shows the kit's own snippet to paste on the owner's site
    # (site_install._code, for steps_view, and since the final review's ruling F-2 for Admiry's team's ticket too) --
    # the agent host its script loads from, as landing.kit names it; never a link.
    (
        "site_install.py",
        re.compile(
            r'^code = landing\.snippet\(page\["id"\], config\.load\(\)\.public_base_url, '
            r"landing\.google_tag\(conn, biz\)\)$"
        ),
    ),
    (
        "tools_pages.py",
        re.compile(
            r'^found\.update\(site_upload\.zip_link\(conn, found\["page_id"\], found\["version"\], '
            r"settings\.public_base_url\)\)$"
        ),
    ),
)


def test_no_browser_link_is_built_from_the_public_base():
    offenders = []
    for path in sorted(AGENT_DIR.glob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            allowed = any(name == path.name and p.search(line.strip()) for name, p in ALLOWED_PUBLIC)
            if "public_base_url" in line and not allowed:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


def test_the_email_sign_in_link_is_on_the_web_base_and_masked_there(split):
    from agent import mail_out

    s = config.load()
    text = f"Sign in: {WEB}/m/tok_secret123 then reply."
    assert mail_out._mask_link_token(text, s) == f"Sign in: {WEB}/m/… then reply."


def test_email_links_are_on_the_web_base_and_the_page_kit_on_the_public_base(split, client, conn, resend, mail_fake):  # noqa: F811 -- the fixture
    """What the e-mail channel and Admiry Pages actually hand out with the two bases split (spec 2026-09-29 §3): the
    /m/ sign-in step, its masked stored copy, the Connect and plan links are browser pages on the web host; the page
    kit's script and form action are pasted onto the owner's own site and stay on the agent host."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    wall = last_sent(resend)["text"]
    assert f"{WEB}/m/" in wall and "http://testserver/m/" not in wall
    stored = conn.execute(
        "SELECT new_text FROM email_message WHERE direction = 'out' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()["new_text"]
    assert f"{WEB}/m/…" in stored  # the capability masked where it is kept, on the host it was sent with
    biz = t["business_id"]
    claim_and_plan(conn, biz)
    inbound(client, resend, from_="owner@example.com", text="How do I connect?", to=reply_to(t))
    assert f"{WEB}/?setup={biz}" in last_sent(resend)["text"]
    inbound(client, resend, from_="owner@example.com", text="Show me my plan", to=reply_to(thread_row(conn)))
    assert f"{WEB}/plan/pl_" in last_sent(resend)["text"]
    kit = landing.kit(conn, biz)
    assert kit["tracking"]["snippet"].startswith('<script async src="http://testserver/t/admiry.js"')
    assert kit["tracking"]["form_action"].startswith("http://testserver/t/form/pg_")
    assert WEB not in json.dumps(kit["tracking"])
