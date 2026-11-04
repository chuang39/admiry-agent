"""Settings in layout A (spec 2026-09-29-admiry-settings-redesign §4, §6): the shared header, a section nav, one
business at a time — its overview with the one next step, billing, assistants, e-mail, the folded keys — and the
account card."""

import re
from datetime import date
from html import unescape

import pytest

from agent import (
    agreements,
    business,
    claims,
    google_ads_link,
    jobs,
    keys,
    legal,
    mail_grants,
    mcp_app,  # noqa: F401 - mcp_app first: import cycle
    oauth,
    wallet,
)
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, call_tool, without_site_header
from agent.tests.fixtures import knowledge_fixture
from agent.tests.test_ads_run import add_plan, sections
from agent.tests.test_claims import draft_business
from agent.tests.test_oauth_consent import _sign_in
from agent.tests.test_settings_backend import _two_businesses
from agent.tests.test_wallet import fund, priced_session
from agent.tests.test_web import claimed_business, csrf_of, sign_in
from agent.tests.test_web_grants import _grant

PRIMARY = re.compile(r'<a class="btn btn-primary" href="/\?setup=biz_1">([^<]+)</a>')
TIME = re.compile(
    r'<time datetime="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z" title="[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{2}:\d{2} UTC">'
    r"([^<]+)</time>"
)


def _section(html: str, id_: str) -> str:
    """The card with this id: a <section> (or the <details> the keys fold into), up to its closing tag."""
    tag = "details" if id_ == "advanced" else "section"
    start = html.index(f'<{tag} id="{id_}"')
    return html[start : html.index(f"</{tag}>", start)]


def _csrf(conn) -> str:
    return conn.execute("SELECT csrf FROM web_session WHERE revoked_at IS NULL").fetchone()["csrf"]


def test_settings_wears_the_shared_header_and_loads_only_its_own_files(client, conn, google):
    claimed_business(client, conn, google)
    html = client.get("/connect").text
    header = html.split('<header class="top">')[1].split("</header>")[0]
    assert '<details class="account-menu">' in header and '<summary aria-label="Your account">O</summary>' in header
    assert "owner@example.com" in header and '<a class="menu-item" href="/connect">Settings</a>' in header
    assert f'<input type="hidden" name="csrf" value="{_csrf(conn)}">' in header
    assert html.index('<link rel="stylesheet" href="/static/base.css">') < html.index(
        '<link rel="stylesheet" href="/static/settings.css">'
    )
    assert '<script src="/static/site.js" defer></script>' in html
    assert "<style" not in html and re.findall(r"<script(?![^>]*\bsrc=)", html) == []  # the CSP allows no inline
    css = client.get("/static/settings.css")
    assert css.status_code == 200 and len(css.text) < 12000
    for rule in (".settings-wrap {", ".settings-side {", ".section-nav a {", ".row {", ".next-step {", ".card-fold"):
        assert rule in css.text, rule
    assert "@media (max-width: 720px) {" in css.text and "scroll-margin-top" in css.text
    assert re.search(r"\.card p \{[^}]*overflow-wrap: anywhere", css.text)  # a long e-mail wraps at 320px
    assert "https://" not in css.text
    page = without_site_header(html)  # the shared header's nav is the product's own menu, not the page's words
    assert JARGON.findall(page) == [] and ACCOUNT_JARGON.findall(page) == [] and MONEY_JARGON.findall(page) == []


def _nav(html: str) -> list[str]:
    nav = html.split('<nav class="section-nav" data-sections')[1].split("</nav>")[0]
    return re.findall(r'href="#([a-z]+)"', nav)


def test_the_section_nav_lists_the_cards_in_order(client, conn, google):
    claimed_business(client, conn, google)
    html = client.get("/connect").text
    assert _nav(html) == ["overview", "billing", "assistants", "advanced", "account"]
    for id_ in _nav(html):
        assert f'id="{id_}"' in html, id_
    assert 'id="email"' not in html  # the e-mail channel is off


def test_the_email_card_joins_the_nav_when_the_channel_is_on(client, conn, google, resend):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'claimed', 'owner@example.com', 'acme')"
    )
    g = mail_grants.add(
        conn, "biz_1", "cue@example.com", scopes=("read",), via="connect", created_by="owner@example.com"
    )
    _sign_in(client, google)
    html = client.get("/connect").text
    assert _nav(html) == ["overview", "billing", "assistants", "email", "advanced", "account"]
    email = _section(html, "email")
    assert "Write to Admiry at" in email and 'action="/connect/addresses"' in email and ">Add address</button>" in email
    for name in ("address", "manage", "leads"):
        assert f'name="{name}"' in email, name
    assert '<p class="row-title">cue@example.com</p>' in email and TIME.findall(email) == ["just now"]  # added
    form = email.split(f'action="/connect/addresses/{g["id"]}/revoke"')[1].split("</form>")[0]
    assert 'data-confirm="Revoke cue@example.com? Admiry will stop answering e-mail from it for this business."' in form
    assert f'name="csrf" value="{_csrf(conn)}"' in form
    assert '>Revoke<span class="sr"> cue@example.com</span></button>' in form  # each Revoke names what it ends


@pytest.mark.parametrize(
    ("state", "label"),
    [("nothing", "Set up ads"), ("own_account_live", "Open setup"), ("managed_funded", "Add funds")],
)
def test_the_one_primary_button_is_the_next_step(client, conn, google, state, label):
    claimed_business(client, conn, google)
    if state == "own_account_live":
        google_ads_link.save(conn, "biz_1", "1234567890", status="active")
    elif state == "managed_funded":
        conn.execute("UPDATE business SET ads_mode = 'managed'")
        fund(conn, cents=25000)
    html = client.get("/connect").text
    assert html.count("btn-primary") == 1
    assert PRIMARY.findall(_section(html, "overview")) == [label]
    assert f'href="/?setup=biz_1">{label}</a>' in _section(html, "billing")  # repeated there, as a secondary link


def _bar(html: str) -> str:
    """The next-step bar's words (its <p>), without the button."""
    return re.sub(r"\s+", " ", html.split('<div class="next-step">')[1].split("</p>")[0].split("<p>")[1]).strip()


@pytest.mark.parametrize(
    ("state", "label", "words"),
    [
        (
            "never_funded",
            "Set up ads",
            "This business is yours. Nothing is spent until you set up ads and approve a plan.",
        ),
        ("funded", "Add funds", "Admiry runs your ads from your balance."),
        ("spent_to_zero", "Set up ads", "Your ads are paused until you add funds."),
    ],
)
def test_the_next_step_bar_words_follow_the_prepaid_balance(client, conn, google, state, label, words):
    """Spec §4.1 / §9: prepaid with and without balance. A paused business is one whose balance ran out (the spend
    guard pauses at or below zero and a top-up lifts it), so its button is D69's "Set up ads" — the bar says why."""
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    if state != "never_funded":
        fund(conn, cents=10000)
    if state == "spent_to_zero":
        wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 28), cost_cents=10000)
        assert jobs.spend_guard(conn, 0, 1)["paused"] == 1
    html = client.get("/connect").text
    assert html.count("btn-primary") == 1 and PRIMARY.findall(_section(html, "overview")) == [label]
    assert _bar(html) == words
    billing = _section(html, "billing")
    assert ("paused until you add funds" in billing) is (state == "spent_to_zero")  # the Billing card agrees with it


LINK = "Google Ads account 123-456-7890: "
NOTE = 'There is a note on it under <a href="/ads?b=biz_1">Your ads</a>.'


@pytest.mark.parametrize(
    ("status", "error", "words"),
    [
        ("pending", None, LINK + "waiting for you to accept Admiry's invitation in Google Ads."),
        (
            "pending",
            google_ads_link.UNREACHABLE,  # the hourly sync could not read it: kept on the row until a read succeeds
            LINK + "waiting for you to accept Admiry's invitation in Google Ads. " + NOTE,
        ),
        ("cancelled", google_ads_link.NO_INVITATION, LINK + "the invitation was withdrawn or has expired. " + NOTE),
        ("refused", None, LINK + "the invitation was declined."),
    ],
)
def test_the_next_step_bar_says_where_an_own_account_link_stands(client, conn, google, status, error, words):
    """An owner who has asked for the link is not told "Nothing is spent until you set up ads" as if nothing had
    begun: the bar says where the invitation stands and points at a note the Your ads page holds, on the Google Search
    card. The button stays D69's "Set up ads" (a link that is not live is not ready), into the conversation's link
    step."""
    claimed_business(client, conn, google)
    google_ads_link.save(conn, "biz_1", "1234567890", status=status)
    if error:
        google_ads_link.record_error(conn, "biz_1", "1234567890", error)
    html = client.get("/connect").text
    assert html.count("btn-primary") == 1 and PRIMARY.findall(_section(html, "overview")) == ["Set up ads"]
    assert unescape(_bar(html)) == words and "Nothing is spent" not in html
    add_plan(conn, sections(), biz="biz_1", status="approved")  # a plan funding Google Search gives /ads its card
    warn = re.findall(r'<p class="warn">([^<]+)</p>', _section(client.get("/ads").text, "google_search"))
    assert warn == ([error[0].upper() + error[1:] + "."] if error else [])  # a sentence of its own


def test_the_overview_names_the_area_when_the_knowledge_has_one(client, conn, google):
    claimed_business(client, conn, google)  # its knowledge serves Palo Alto
    overview = _section(client.get("/connect").text, "overview")
    assert re.search(r'<h2>Bright Smile Dental</h2>\s*<p class="area">Palo Alto</p>', overview)
    data = knowledge_fixture().model_dump() | {"service_area": [], "listing": {"address": "1 Main St, Austin, TX"}}
    business.add_knowledge(conn, "biz_1", data, source="owner")
    overview = _section(client.get("/connect").text, "overview")
    assert '<p class="area">1 Main St, Austin, TX</p>' in overview  # no stated area: the listing's address
    business.add_knowledge(conn, "biz_1", data | {"listing": None}, source="owner")
    assert 'class="area"' not in client.get("/connect").text  # nothing known: no line


def test_knowledge_an_assistant_wrote_in_odd_shapes_never_breaks_settings(client, conn, google):
    """update_business bounds service_area's size, not its shape, and a name stored before it checked a name's shape
    may be anything: a text service_area, entries that are not text and a name that is not text still render
    Settings -- the page where that assistant's access is revoked -- with a sensible area line or none, and the
    switcher's labels (built for every business) too."""
    claimed_business(client, conn, google)
    _two_businesses(conn)  # biz_a and biz_b as well: the switcher shows, biz_1 (the newest) is the one on the page
    known = knowledge_fixture().model_dump()  # its website vouches for its name
    for patch, area in (
        ({"service_area": "Brooklyn, NY"}, "Brooklyn, NY"),  # one text is one area, not its first letter
        ({"service_area": [{"city": "Austin"}, "  ", "Queens"]}, "Queens"),  # entries that are not text are skipped
        ({"service_area": [{"city": "Austin"}]}, None),
        ({"service_area": {"city": "Austin"}, "listing": {"address": {"line1": "1 Main St"}}}, None),
        ({"service_area": 7, "listing": ["1 Main St"]}, None),
    ):
        business.add_knowledge(conn, "biz_1", known | patch, source="owner")
        r = client.get("/connect")
        assert r.status_code == 200, patch
        overview = _section(r.text, "overview")
        assert "<h2>Bright Smile Dental</h2>" in overview, patch
        if area:
            assert f'<p class="area">{area}</p>' in overview, patch
            assert f'<span class="sr">Business: </span>Bright Smile Dental · {area}</summary>' in r.text, patch
        else:
            assert 'class="area"' not in overview, patch
            assert '<span class="sr">Business: </span>Bright Smile Dental</summary>' in r.text, patch
    for name in ({"en": "Bright Smile Dental"}, 42, "   "):  # a vouched name that is not text (or is blank)
        business.add_knowledge(conn, "biz_1", known | {"name": name}, source="owner")
        r = client.get("/connect")
        assert r.status_code == 200, name
        assert "<h2>Your business</h2>" in _section(r.text, "overview"), name
        assert '<span class="sr">Business: </span>Your business · Palo Alto</summary>' in r.text, name


def test_a_draft_has_no_next_step_button_only_the_way_back(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL")
    html = client.get("/connect").text
    overview = _section(html, "overview")
    assert "btn-primary" not in html and 'href="/?setup=biz_1"' not in html
    assert "Not claimed" in overview and "link to claim it again." in overview


@pytest.mark.parametrize(
    ("claim_state", "tone", "words"),
    [("draft", "warning", "Not claimed"), ("claimed", "neutral", "Claimed"), ("verified", "success", "Verified")],
)
def test_the_status_badge_names_the_claim_state(client, conn, google, claim_state, tone, words):
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET claim_state = %s", (claim_state,))
    overview = _section(client.get("/connect").text, "overview")
    assert f'<span class="badge badge-{tone}">{words}</span>' in overview


def test_the_light_says_how_lately_an_assistant_checked_in_never_that_none_is_connected(client, conn, google):
    """Spec §4.1's sentences: the gray light means no check-in in the last 10 minutes. An assistant connected days ago
    that works only when asked (it does not poll) is listed under Assistants; the Overview above must not deny it."""
    access_token, _ = _grant(client, conn)
    call_tool(client, "get_marketing_state", {}, key=access_token)
    conn.execute(
        "UPDATE oauth_grant SET created_at = now() - interval '3 days', last_used_at = now() - interval '2 hours'"
    )
    conn.execute("UPDATE agent_action SET ts = now() - interval '2 hours' WHERE tool = 'marketing_state'")
    _sign_in(client, google)
    html = client.get("/connect").text
    overview = _section(html, "overview")
    assert '<span class="light" aria-hidden="true"></span>' in overview
    assert "No assistant has checked in in the last 10 minutes." in overview and "connected yet" not in html.lower()
    assert TIME.findall(_section(html, "assistants"))[1] == "2 hours ago"  # last used, on the same page
    conn.execute("UPDATE agent_action SET ts = now() - interval '4 minutes' WHERE tool = 'marketing_state'")
    overview = _section(client.get("/connect").text, "overview")
    assert '<span class="light on" aria-hidden="true"></span>' in overview
    assert re.search(r"An assistant checked in <time [^>]+>4 minutes ago</time>\.", overview)


def test_assistants_show_chips_readable_times_and_a_revoke_that_asks_first(client, conn, google):
    access_token, grant_id = _grant(client, conn)
    call_tool(client, "whoami", {}, key=access_token)
    _sign_in(client, google)
    assistants = _section(client.get("/connect").text, "assistants")
    assert "Connected assistants" in assistants and '<p class="row-title">Claude</p>' in assistants
    for scope in ("read", "manage", "leads"):
        assert f'<span class="chip">{oauth.SCOPE_WORDS[scope]}</span>' in assistants, scope
    assert TIME.findall(assistants) == ["just now", "just now"]  # connected, last used
    form = assistants.split(f'action="/connect/grants/{grant_id}/revoke"')[1].split("</form>")[0]
    assert 'data-confirm="Revoke access for Claude? It will stop working until it connects again."' in form
    assert f'name="csrf" value="{_csrf(conn)}"' in form and '>Revoke<span class="sr"> Claude</span></button>' in form
    assert '<details class="connect-more">' in assistants  # folded: an assistant is already connected


def test_two_grants_with_the_same_name_stay_two_rows(client, conn, google):
    _grant(client, conn)
    _grant(client, conn)  # the same assistant connected twice: two independent grants (spec §4.3)
    conn.execute(
        "UPDATE oauth_grant SET created_at = now() - interval '3 days' WHERE id = (SELECT min(id) FROM oauth_grant)"
    )
    _sign_in(client, google)
    assistants = _section(client.get("/connect").text, "assistants")
    ids = [r["id"] for r in conn.execute("SELECT id FROM oauth_grant")]
    assert len(ids) == 2 and assistants.count('<p class="row-title">Claude</p>') == 2
    assert all(f'action="/connect/grants/{i}/revoke"' in assistants for i in ids)
    # the times tell the rows apart on screen; each Revoke's spoken name and its confirm say which one it ends
    spoken = re.findall(r'>Revoke<span class="sr"> (Claude \(connected [^)]+ UTC\))</span>', assistants)
    asked = re.findall(r'data-confirm="Revoke access for (Claude \(connected [^)]+ UTC\))\? It will stop', assistants)
    assert len(set(spoken)) == 2 and asked == spoken


def test_keys_count_in_words(client, conn, google):
    claimed_business(client, conn, google)
    for label in ("muse", "chatgpt"):
        keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label=label)
    advanced = _section(client.get("/connect").text, "advanced")
    assert "For assistants that can't sign in with Google · 2 keys</span>" in advanced
    assert '<p class="row-title">ChatGPT</p>' in advanced


def test_no_assistant_yet_opens_the_connect_block(client, conn, google):
    claimed_business(client, conn, google)
    assistants = _section(client.get("/connect").text, "assistants")
    assert "Connect Admiry to the assistant you use" in assistants
    assert '<details class="connect-more" open>' in assistants


def test_the_muse_prompt_has_a_copy_button_and_the_guide_link(client, conn, google):
    claimed_business(client, conn, google)
    html = client.get("/connect").text
    assistants = _section(html, "assistants")
    prompt = assistants.split('<pre id="muse-prompt">')[1].split("</pre>")[0]
    assert "Add Admiry as a connector" in prompt
    assert '<button class="btn" type="button" data-copy="#muse-prompt">Copy</button>' in assistants
    assert 'href="/connect/docs"' in assistants and "Set up in Meta Muse" not in html


def test_keys_fold_into_advanced_with_their_rows_and_the_create_form(client, conn, google):
    claimed_business(client, conn, google)
    _, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="muse")
    html = client.get("/connect").text
    opening = re.search(r'<details id="advanced"[^>]*>', html).group(0)
    assert "open" not in opening and 'class="card card-fold"' in opening
    advanced = _section(html, "advanced")
    assert "Keys" in advanced and "For assistants that can't sign in with Google · 1 key</span>" in advanced
    assert '<p class="row-title">Muse</p>' in advanced  # the assistant's name; the label stays the form's value
    for scope in ("read", "manage"):  # a key's chips read like a grant's (they share scope ids)
        assert f'<span class="chip">{oauth.SCOPE_WORDS[scope]}</span>' in advanced, scope
    form = advanced.split(f'action="/connect/keys/{rec.id}/revoke"')[1].split("</form>")[0]
    assert (
        'data-confirm="Revoke this Muse key? The assistant using it will stop working until it gets a new key."' in form
    )
    assert 'name="csrf"' in form and '>Revoke<span class="sr"> the Muse key</span>' in form
    # the fold's title is a heading in the markup; whether heading navigation reaches it inside <summary> depends on
    # the browser and screen reader (Safari with VoiceOver may drop it): that is checked by hand, not here
    assert '<summary><h2 class="fold-title">Keys</h2>' in advanced
    create = advanced.split('action="/connect/keys"')[1].split("</form>")[0]
    assert 'name="business_id" value="biz_1"' in create and '<select name="label"' in create
    for option, name in (("muse", "Muse"), ("claude", "Claude"), ("chatgpt", "ChatGPT")):
        assert f'<option value="{option}">{name}</option>' in create, option
    for box in ("manage", "leads", "account"):
        assert f'type="checkbox" name="{box}"' in create, box
    # the leads box says what it permits, in the consent's own words (final review A, I2)
    assert f'name="leads" checked> {oauth.SCOPE_WORDS["leads"]}</label>' in create
    assert oauth.SCOPE_WORDS["leads"] == "See your leads and record what became of them"
    assert ">Create key</button>" in create


def test_the_account_card_holds_the_email_a_new_business_and_sign_out(client, conn, google):
    claimed_business(client, conn, google)
    html = client.get("/connect").text
    account = _section(html, "account")
    assert "owner@example.com" in account and 'action="/connect/businesses/new"' in account
    assert ">Start a new business</button>" in account and 'action="/connect/signout"' in account
    assert html.count("Signed in as") == 1 and "Signed in as" in account  # the old top line is gone


def test_the_switcher_appears_only_with_more_than_one_business(client, conn, google):
    claimed_business(client, conn, google)
    html = client.get("/connect").text
    assert 'class="switcher"' not in html and '<p class="biz-name">Bright Smile Dental</p>' in html
    conn.execute("UPDATE business SET owner_email = 'someone@example.com'")  # only biz_a and biz_b are theirs now
    _two_businesses(conn)
    html = client.get("/connect?b=biz_a").text
    switcher = html.split('<details class="switcher">')[1].split("</details>")[0]
    assert '<a href="/connect?b=biz_a" aria-current="page">' in switcher
    assert '<a href="/connect?b=biz_b">' in switcher and "biz_c" not in html and "biz_1" not in html
    assert 'action="/connect/businesses/new"' in switcher


def test_the_switcher_tells_same_named_businesses_apart(client, conn, google):
    claimed_business(client, conn, google)  # Bright Smile Dental, serving Palo Alto
    _two_businesses(conn)  # biz_a and biz_b: nothing known about either, so both are "Your business"
    html = client.get("/connect?b=biz_a").text
    switcher = html.split('<details class="switcher">')[1].split("</details>")[0]
    entries = dict(re.findall(r'<a href="/connect\?b=(biz_\w+)"[^>]*>([^<]+)</a>', switcher))
    assert entries.pop("biz_1") == "Bright Smile Dental · Palo Alto"  # the area, when the knowledge has one
    assert entries.pop("biz_b") == "Your business · started yesterday"  # the same name: the day each was started
    shown = entries.pop("biz_a")
    assert re.fullmatch(r"Your business · started [A-Z][a-z]{2} \d{1,2}(, \d{4})?", shown)
    assert f'<span class="sr">Business: </span>{shown}</summary>' in switcher  # the closed switcher reads the same


def test_the_switcher_numbers_businesses_whose_labels_still_match(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET owner_email = 'someone@example.com'")  # only the two below are theirs
    conn.execute(  # two "Start a new business" clicks seconds apart: both "started 3 minutes ago"
        "INSERT INTO business (id, owner_email, claim_state, slug, created_at) VALUES "
        "('biz_a', 'owner@example.com', 'claimed', 'a', now() - interval '200 seconds'), "
        "('biz_b', 'owner@example.com', 'claimed', 'b', now() - interval '190 seconds')"
    )
    html = client.get("/connect?b=biz_a").text
    switcher = html.split('<details class="switcher">')[1].split("</details>")[0]
    entries = dict(re.findall(r'<a href="/connect\?b=(biz_\w+)"[^>]*>([^<]+)</a>', switcher))
    assert entries == {  # numbered in the order they were started
        "biz_a": "Your business · started 3 minutes ago (1)",
        "biz_b": "Your business · started 3 minutes ago (2)",
    }
    assert '<span class="sr">Business: </span>Your business · started 3 minutes ago (1)</summary>' in switcher


def test_the_new_key_page_links_back_to_this_business_keys(client, conn, google):
    claimed_business(client, conn, google)
    r = client.post("/connect/keys", data={"business_id": "biz_1", "label": "muse", "csrf": csrf_of(client)})
    assert r.status_code == 200 and '<a href="/connect?b=biz_1#advanced">your keys page</a>' in r.text


def test_agreements_link_to_their_texts(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    billing = _section(client.get("/connect").text, "billing")
    assert "Google Ads Management Agreement v1" in billing and "Prepayment Addendum v0.2" in billing
    assert 'href="/legal/management-agreement"' in billing and 'href="/legal/prepayment-addendum"' in billing
    assert "Balance: <strong>$0.00</strong>" in billing
    conn.execute("UPDATE agreement_acceptance SET accepted_at = '2026-09-05 12:00+00'")
    billing = _section(client.get("/connect").text, "billing")
    assert billing.count("Accepted Sep 5, 2026</p>") == 2  # written like every other date here, not "Sep 05"


def test_a_top_ups_row_shows_what_it_cost(client, conn, google):
    """Task 1 writes the price and the cost into a credited top-up's note; Settings shows that note verbatim, not
    the generic PAYMENT_WORDS "Card payment" a v0 row (no note) still falls back to."""
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    priced_session(conn)
    wallet.topup(conn, session_id="cs_q", amount_cents=103330, payment_intent="pi_q")
    html = client.get("/connect").text
    billing = _section(html, "billing")
    assert "Card payment of $1,033.30 ($33.30 transaction fee)" in billing
    assert '<td class="num">$1,000.00</td>' in billing
    page = without_site_header(html)
    assert JARGON.findall(page) == [] and ACCOUNT_JARGON.findall(page) == [] and MONEY_JARGON.findall(page) == []


def test_a_note_explains_the_transaction_fee_only_when_a_row_carries_one(client, conn, google):
    """Finding 4: the payments table's rows carry "transaction fee" with nothing nearby saying what it is -- one
    note under the table, and only once some row actually has a fee (a refund or an operator correction never
    does)."""
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    wallet.adjust(conn, "biz_1", 500, note="goodwill credit")
    billing = _section(client.get("/connect").text, "billing")
    assert "Correction" in billing and "transaction fee" not in billing
    priced_session(conn)
    wallet.topup(conn, session_id="cs_q", amount_cents=103330, payment_intent="pi_q")
    billing = _section(client.get("/connect").text, "billing")
    assert "transaction fee" in billing
    assert "The transaction fee is what Stripe charges to process the payment; Admiry adds no fee." in billing


def test_site_js_opens_a_folded_card_the_address_points_at(client):
    js = client.get("/static/site.js").text  # the key-revoke redirect and the nav's Advanced both land on #advanced
    assert "hashchange" in js and ".open = true" in js
    # only strings: the behaviour (the unfold, the nav's hold and its release on any scroll) is checked in a browser
    assert '"scrollend"' in js and "landed" in js


def test_a_link_styled_as_a_button_hovers_like_a_button(client):
    css = client.get(
        "/static/settings.css"
    ).text  # an agreement's Read and the Billing card's setup link are <a class="btn">
    assert "a.btn:not(.btn-primary):hover { color: var(--fg); }" in css


@pytest.mark.parametrize("others", [False, True])
def test_the_claim_question_for_a_business_not_yet_theirs_stands_alone(client, conn, google, others):
    """Opening a claim link for a business nobody owns yet: the question, then only the account card. Not "No
    business yet" (the owner is claiming one) and not another business's cards, whose primary button would set up
    that other business under "Claim Bright Smile Dental?"."""
    draft_business(conn)  # biz_1, Bright Smile Dental, nobody's yet
    if others:
        _two_businesses(conn)  # biz_a and biz_b are the signer's
    url, _ = claims.new_link(conn, "biz_1")
    sign_in(client)
    html = client.get(f"/connect?claim={url.split('claim=')[1]}").text
    assert "Claim <strong>Bright Smile Dental</strong> as owner@example.com?" in html
    assert "btn-primary" not in html and "/?setup=" not in html and 'id="overview"' not in html
    assert "No business yet" not in html and "biz_a" not in html and "biz_b" not in html
    assert _nav(html) == ["account"] and 'action="/connect/businesses/new"' in _section(html, "account")


def test_a_refused_claim_is_an_alert_and_news_is_a_status_both_in_sentence_case(client, conn, google):
    draft_business(conn)
    claims.claim(conn, "biz_1", "first@example.com")
    url, _ = claims.new_link(conn, "biz_1")
    token = url.split("claim=")[1]
    sign_in(client)
    refused = (
        '<p class="banner banner-alert" role="alert">'
        "This business was claimed by another Google account; sign in with that account.</p>"
    )
    assert refused in client.get(f"/connect?claim={token}").text
    r = client.post("/connect/claim", data={"claim": token, "csrf": csrf_of(client)})
    assert r.status_code == 409 and refused in r.text
    news = client.get("/connect?claimed=1").text
    assert '<p class="banner" role="status">This business is now yours.</p>' in news


def test_the_email_card_names_listed_addresses_only_when_there_are_some(client, conn, google, resend):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'claimed', 'owner@example.com', 'acme')"
    )
    _sign_in(client, google)
    email = _section(client.get("/connect").text, "email")
    assert "</strong> from your sign-in address. Nothing is spent" in email and "listed here" not in email
    mail_grants.add(
        conn, "biz_1", "cue@example.com", scopes=("read", "manage"), via="connect", created_by="owner@example.com"
    )
    email = _section(client.get("/connect").text, "email")
    assert "from your sign-in address, or from any address listed here. Nothing" in email
    for scope in ("read", "manage"):  # sentence case, like the other chips on the page
        word = mail_grants.SCOPE_WORDS[scope]
        assert f'<span class="chip">{word[0].upper() + word[1:]}</span>' in email, scope
