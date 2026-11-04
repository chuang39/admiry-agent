import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest

from agent import benchmark, hosting_minimum, pay
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.test_home_api import PICK, SITE, fake, turn  # noqa: F401 -- `fake` is a pytest fixture used by name
from agent.tests.test_web import sign_in

FORBIDDEN = re.compile(
    r"\b(campaign|ad group|audience|keyword|cpc|cpm|ctr|roas|google ads|tiktok ads)\b", re.IGNORECASE
)
# P8: nothing the page loads comes from another origin (a script, a stylesheet, an image, a frame)
CROSS_ORIGIN_LOAD = re.compile(r'<(?:script|link|img|iframe)[^>]+(?:src|href)="(?:https?:)?//')


def _first_visit_visible(html: str) -> str:
    """The page as a first-time visitor sees it: scripts and the <template> moments (wall, sheet, plan) removed."""
    return re.sub(
        r"<template.*?</template>", "", re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL), flags=re.DOTALL
    )


def test_first_visit_page_has_the_entry_and_none_of_the_forbidden_words(client):
    r = client.get("/")
    assert (
        r.status_code == 200
        and 'name="robots" content="noindex"' in r.text
        and r.headers["cache-control"] == "no-store"
    )
    # the website's front page (website/components/marketing/hero-section.tsx, hero-composer.tsx), restyled here
    # 2026-10-06: its headline, box, chips and fine print; the lead stays this page's own (no "Google Ads" here, P8)
    for text in (
        (
            '<h1 id="hero-title"><span>Tell Admiry what you do.</span>'
            '<span class="brandline">It finds your customers.</span></h1>'
        ),
        "<title>Admiry — Tell Admiry what you do. It finds your customers.</title>",
        '<p class="lead">Your AI marketing agent for local and service businesses.</p>',
        '<label for="ask" class="ask-label">Start with your business</label>',
        "Press Enter to send. Shift+Enter adds a new line.",
        'data-fill="I run a dental practice in Miami and want more new-patient appointments.">Dentists</button>',
        "Your business, your location, and the customers you want.",
        "$0 platform fee · No subscription · You approve everything before it runs",
        "Continue in Muse",
        '/capabilities">See everything Admiry can do',  # the website prefix (web.website_prefix) goes before it
        "or use the assistant you already have",
    ):
        assert text in r.text, text
    for gone in ('id="rotor"', 'class="eyebrow"', "Use Claude or ChatGPT instead", "More industries"):
        assert gone not in r.text, gone  # the v0 hero's rotating vertical and label; the website's own section link
    assert "Run a $1,000 test" not in r.text  # the example chips suggest no dollar amount (2026-09-30)
    assert "groomer" not in r.text.lower()  # the rotating verticals and chips (v0 redesign, 2026-10-03)
    visible = _first_visit_visible(r.text)
    assert not FORBIDDEN.search(visible) and not JARGON.search(visible)
    assert not ACCOUNT_JARGON.search(visible) and not MONEY_JARGON.search(visible)
    assert '<script src="/static/home.js"' in r.text and 'href="/static/home.css"' in r.text
    assert "https://" not in re.findall(r'<script[^>]+src="([^"]+)"', r.text)[0]  # no external script
    assert "http://testserver/mcp" in r.text  # the sheet's tokenless prompt
    assert 'href="/connect/google/start?next=%2F"' in visible  # the header's Sign in


def test_the_assistants_each_show_their_own_mark_served_from_this_origin(client):
    """2026-10-06: Muse, Claude and ChatGPT each carry their owner's mark, byte for byte as published
    (agent/static/brands/SOURCES.txt), from this origin (the home's img-src is 'self'), and decorative: the button
    already says the name. The hero's Muse button leads; Claude and ChatGPT sit under it; the sheet's tabs carry them
    too."""
    html = client.get("/").text
    assist = html.split('<div class="assist">')[1].split('<a class="more"')[0]
    for name, label in (("muse", "Continue in Muse"), ("claude", "Claude"), ("chatgpt", "ChatGPT")):
        button = assist.split(f'data-sheet="{name}"')[1].split("</button>")[0]
        assert f'<img class="logo logo-{name}" src="/static/brands/{name}.svg" alt=""' in button, name
        assert label in button, name
    assert (
        assist.index('data-sheet="muse"') < assist.index('data-sheet="claude"') < assist.index('data-sheet="chatgpt"')
    )
    sheet = html.split('id="t-sheet"')[1].split("</template>")[0]
    for name in ("muse", "claude", "chatgpt"):
        tab = sheet.split(f'data-tab="{name}" id="tab-{name}"')[1].split("</button>")[0]
        assert f'<img class="logo logo-{name}" src="/static/brands/{name}.svg" alt=""' in tab, name
    for name in ("muse", "claude", "chatgpt"):
        r = client.get(f"/static/brands/{name}.svg")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml"), name
        assert "<script" not in r.text and "href" not in r.text and "<image" not in r.text, name  # marks only
    assert 'fill="black"' in client.get("/static/brands/chatgpt.svg").text  # OpenAI's Blossom: black or white only
    assert client.get("/static/brands/SOURCES.txt").status_code == 200


def test_the_hero_box_types_its_examples_and_a_chip_fills_its_whole_sentence(client):
    """The website's box (hero-composer.tsx): three example sentences typed in turn as the placeholder, never under
    reduced motion or once the visitor is in the box; a chip names an industry and fills the whole sentence."""
    html = client.get("/").text
    ask = html.split('<textarea id="ask"')[1].split("</textarea>")[0]
    sentences = ask.split('data-examples="')[1].split('"')[0].split("|")
    assert len(sentences) == 3 and all(s.endswith("…") for s in sentences)
    assert f'placeholder="{sentences[0]}"' in ask  # without the script, the first one, whole
    js = client.get("/static/home.js").text
    assert "rotor" not in js and "b.dataset.fill" in js
    typing = js.split("// the hero box types its examples")[1].split("\n\n")[0]
    assert "reduced" in typing and "focus" in typing and "ask.value" in typing


def test_the_page_loads_nothing_from_elsewhere_and_holds_no_secret(client):
    r = client.get("/")
    assert not CROSS_ORIGIN_LOAD.search(r.text)  # no cross-origin fonts or CDN
    assert "ak_" not in r.text and "hs_" not in r.text and "continue session" not in r.text.lower()  # tokenless
    assert "http://testserver/connect/skill.md first, then sign me in." in r.text and "Bearer" not in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_static_assets_are_served_and_small(client):
    js, css = client.get("/static/home.js"), client.get("/static/home.css")
    assert js.status_code == 200 and css.status_code == 200
    assert (
        js.text.count("\n") < 1465 and css.text.count("\n") < 657
    )  # js: +11 multi-pick, +40 daily budget slider, +20 ad preview card (2026-09-29), +64 the page card (Task 7),
    # +4 plan thumbs (SP1, 2026-09-30), +4 the three question sentences (walk fixes 1, Task 4),
    # +3 the hero's rotating vertical (v0 redesign, 2026-10-03), +33 the hosting note, the Muse invitation and the
    # assistant's own page (A3p-3 batch 1), +2 its screenshot (A3p-3 batch 4), +20 the listing confirm and the v0 front
    # page (main, 2026-10-03/04: main's own budget of 787 was left behind at 805 lines), +82 the "Your website" card,
    # its steps view, Check now and the plan e-mail's links to it (A3p-5 batch 2 Task 2, 2026-10-05), +9 the link through
    # the sign-in and the fourth way's sentence (Task 2 fix round 1, rulings R2-f, R2-g), +34 the web person's address
    # field and Admiry's team's contact field and button, their answers on the card (Task 3 and its fix round 1, rulings
    # R0-f, R3-c, 2026-10-06), +17 the hero box typing its examples and a chip's whole sentence (the website's front
    # page, 2026-10-06), -3 the rotating vertical it replaces, +31 a signed-in owner's businesses to pick from and the
    # pick (no visitor cookie: ask first, list them; founder's dev walk, 2026-10-07), +3 the launch buttons by their
    # actions' ids (Admiry running the ads leads beside the own account, both connect_channel; ruling 2026-10-07), +8
    # the industry site card's hand-off (A3p-6 batch 2 Task 7), +26 the daily digest's report turn (/?report=, plan
    # 2026-10-07-agent-k2 Task 12), +144 what stops the launch, said as Admiry's own message -- a button, one
    # question or choices, one at a time, a fix leaving it spent and drawing the next (B0 Task 8, 2026-10-08), +24 the
    # setup link making the page its business's -- attached, its conversation drawn -- before its card opens, a
    # refused publish drawing the fix again, and a cleared conversation letting go of its setup card (B0 final review,
    # 2026-10-08), +12 back from Stripe the card is alone until it says the money is in, so no second pay form sits
    # beside it, +1 that draw waiting out a turn of the owner's already on its way (B0 follow-up R4), +19 a business
    # with no consultation keeps its card in view instead of the first visit's start page, and "Build your business
    # website" with no plan on the page builds it in Admiry's own message (B0 follow-up R5), +8 a page names the
    # business it shows on every request, refused with a reload if it no longer matches the visitor cookie's (B0
    # follow-up R1), +1 a card opened from the conversation under a plan passes the business its page shows, not the
    # cookie's (B0 follow-up R1 fix round 1), +6 a setup link over an unclaimed draft claims it before switching, so
    # the draft is not lost (B0 follow-up R2), +5 the composer hidden while a setup link's card stands without a
    # conversation -- the paid wait, a business with no consultation, a refused pick or claim -- and back with the
    # conversation (B0 follow-up final review C1), +8 back from Stripe the card moved below the conversation the money
    # draws and scrolled into view, and another business's state by then offered the reload instead of drawn (B0
    # follow-up final review M1, M2), +1 the words that start over taking the page to its first visit (K6 slice 1, 2026-10-10)
    # +1 a question's card keeping its id, which typed words post, so after a go_to they answer the question asked
    # again (K6 slice 1 final review C1)
    # +34 the plan card's own checklist -- Checking while GET /api/launch_check is read, one row per problem with its
    # suggestion and its field or one-tap fix, then Ready to launch with the promise -- replacing B0's single blocker
    # drawn in the conversation, which it retires (checkPlan, drawCheck, checkRow, resolve/resolved, recheck,
    # confirmPlan, reviewing; css: +10 the checking spinner and the checklist's rows) (L1 Task 5, spec 2026-10-10)
    # M0's channels (the plan card's rows, a level's split, the channels card) are channels.js, sized on its own below;
    # the industry site card (A3p-6 batch 2) is sites.js, sized on its own below too
    # +1 the heading never defaults to or stays on "Ready to launch" while checking or after a failed or stale check
    # (checkPlan, drawCheck; L1 final whole-branch review)
    # +9 the plan card's "Changes Admiry made" line, in review and once live (changesLine; assistant checklist
    # Task 8, spec 2026-10-11 §5.1)
    # +10 the reading's facts card drawn after Admiry's sentence (readingCard; R6, spec 2026-10-12 §3.3)
    # +115 the website card's 14 checks -- the page's line, the verdict, the bar and the legend, the three groups with
    # the tracking row's own buttons and the passing ones folded, "not checked yet", the foot and Check again -- read
    # once on draw and again by Check now, and the same list under the published page cards' line (websiteCard,
    # loadWebsite, showSiteCheck, siteLine, hostOf, when, groupChecks, checkRowOf, drawChecks, fixRow; css: +49 the
    # list's styles, scoped off .checks, .mark and .tag) (R3, spec 2026-10-12 §3.5), +7 the tracking row's buttons as
    # the row's own ways in its order, and a Check now answer never drawn over by the card's older load (fix round)
    # +6 no page card beside a website whose ads do not yet use an Admiry page -- "Use an Admiry page instead"
    # reveals it and loads its state (pageCard, chooseWay; R5, spec 2026-10-12 §3.7), the website card's ways row
    # shown only once the load (or its refusal) answers, never drawn and hidden again on the same draw (websiteCard,
    # loadWebsite), and a root path's own query is not the bare host (hostOf; WordPress's plain permalinks) (Task 8)
    # css: +2 the .btn.small and .passing > summary 44px phone targets, the website check list's base rules moved
    # before the phone block they now share (Task 8 review fix round)
    assert "innerHTML" not in js.text and "eval(" not in js.text  # dynamic strings go in as text
    # the daily digest's "Open Admiry" (Task 12): /?report= behind sign-in, the report fetched and shown as text
    report = js.text.split("async function showReport")[1].split("const reportId")[0]
    assert "/api/report?" in report and 'el("p", "msg"' in report and 'q.get("report")' in js.text
    assert "if (reportId && !S.signedIn) toSignIn();" in js.text
    assert "@media" in css.text and "720px" in css.text and "44px" in css.text
    assert "https://" not in css.text  # no third-party fonts (Geist is served from /static/fonts)
    assert js.headers["cache-control"] == "no-cache"  # a deploy's new script is never stale behind an old page


def test_page_copy_is_the_spec_copy(client):
    html = client.get("/").text
    for text in (
        "Your plan is ready",
        "Sign in to see it.",
        "Continue with Google",
        "You never copy a key",
        "Something else",
        "What the budget should do in 30 days",
        "Why Google Search first",
        "Why not TikTok yet",
        "Agree and launch with my Google Ads account",
        "Agree and pay",
        "Adjust the budget",
    ):
        assert text in html, text  # templates for every moment ship in the page; JS shows them
    assert 'href="/connect/google/start?next=%2F%3Fafter%3Dsignin"' in html  # the wall returns to the plan
    assert "Your plan" in html and "Runs on" in client.get("/static/home.js").text


def test_every_moment_of_the_page_passes_the_jargon_check(client):
    """Not only the first visit: the templates (wall, plan, sheet) and the script's own strings too. The
    header's Your ads link exists only for a signed-in owner (spec §3 header) and is not in this render."""
    html = re.sub(r"<script.*?</script>", "", client.get("/").text, flags=re.DOTALL)
    assert JARGON.findall(html) == [] and ACCOUNT_JARGON.findall(html) == [] and MONEY_JARGON.findall(html) == []
    assert "campaign" not in client.get("/static/home.js").text.lower()
    assert "campaign" not in client.get("/static/channels.js").text.lower()
    sites = client.get("/static/sites.js").text  # the industry site card's script, comments and all (A3p-6 batch 2)
    assert JARGON.findall(sites) == [] and ACCOUNT_JARGON.findall(sites) == [] and MONEY_JARGON.findall(sites) == []


def test_signed_in_header_shows_the_account_not_sign_in(client, google):
    sign_in(client, "/")
    html = client.get("/").text
    assert 'data-signed-in="1"' in html and 'aria-label="Your account"' in html and ">O</summary>" in html
    assert 'href="/ads">Your ads<' in html and "Insights" in html and 'href="/connect">Settings<' in html  # in the menu
    assert 'href="/connect/google/start?next=%2F"' not in html


def test_state_carries_the_open_question_so_a_reload_resumes_it(client, conn, fake):  # noqa: F811 -- the fixture
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist in Palo Alto, more Invisalign patients", "idempotency_key": "i1"})
    s = turn(client, {"action": "state"}).json()
    q = s["result"]["next"]["question"]
    # the website first: the words name none (plan 2026-10-05-agent-a3p-5-1)
    assert s["result"]["next"]["kind"] == "question" and q["id"] == "website" and q["index"] == 1 and q["options"]
    turn(client, SITE)
    s = turn(client, {"action": "state"}).json()
    q = s["result"]["next"]["question"]
    # then the business on Google Maps (plan 2026-10-03-agent-listing-confirm)
    assert s["result"]["next"]["kind"] == "question" and q["id"] == "listing" and q["index"] == 2 and q["options"]
    turn(client, PICK)
    s = turn(client, {"action": "state"}).json()
    q = s["result"]["next"]["question"]
    assert s["result"]["next"]["kind"] == "question" and q["id"] == "objective" and q["index"] == 3 and q["options"]
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    assert turn(client, {"action": "state"}).json()["result"]["next"]["question"]["id"] == "service"


def test_the_website_question_offers_the_no_website_pill_and_its_placeholder(client, conn, fake):  # noqa: F811 -- the fixture
    """A3p-5 batch 1 Task 4 (plan 2026-10-05-agent-a3p-5-1, Q14): no new homepage code -- home.js's question() already
    draws one pill per option and an input with the question's own placeholder (test_home_page's reading of that
    code: each of q.options becomes a <button class="pill">, and q.other.allowed shows q.other.placeholder) -- so
    this pins the website question's own shape rather than the renderer, which stays untouched."""
    benchmark.seed(conn)
    turn(client, {"action": "start", "text": "dentist in Palo Alto, more Invisalign patients", "idempotency_key": "i1"})
    q = turn(client, {"action": "state"}).json()["result"]["next"]["question"]
    assert q["id"] == "website"
    assert {"id": "none", "label": "No website"} in q["options"]  # drawn as a pill, like any other option
    assert q["other"] == {"allowed": True, "placeholder": "Paste your website's address"}


def test_the_fresh_start_lines_are_fixed_template_text(client):
    """I3 (final review): the page explains a fresh start after a reload from fixed copy, keyed by `reason`."""
    html = client.get("/").text
    assert 'data-reason="expired" hidden>Your earlier answers expired; tell me again.</p>' in html
    assert 'data-reason="moved" hidden>Your plan continues in your assistant.</p>' in html
    js = client.get("/static/home.js").text
    assert "r.signin" in js and "Sign in to continue" in js and "dataset.reason" in js


def test_the_start_over_notice_is_fixed_template_text_and_the_page_goes_to_its_first_visit(client):
    """K6 slice 1 (spec 2026-10-10 §4.5): the words that start over take the page back to its first box, with the
    notice keyed `over` as the expired and moved notices are (I3)."""
    html = client.get("/").text
    assert 'data-reason="over" hidden>Starting over. Tell me about your business.</p>' in html
    js = client.get("/static/home.js").text
    assert 'if (r.ok && r.start_over) return firstVisit(null, "over");' in js
    assert js.index("r.start_over") < js.index("if (!r.ok && q) spend(q, false);")  # before the open card is spent back


def test_typed_words_carry_the_id_of_the_question_card_they_were_typed_under(client):
    """C1 (K6 slice 1 final review): a question's card keeps its id and the words typed while it is open post it, so
    after a go_to they answer the question asked again, never the first unanswered one (the harness reads the id)."""
    js = client.get("/static/home.js").text
    assert "n.dataset.qid = q.id;" in js.split("function question(q)")[1].split("function spend")[0]
    assert 'const r = await turn("say", q ? { text, question_id: q.dataset.qid } : { text });' in js


def test_the_muse_sheet_says_the_plan_is_waiting(client):
    assert "<li>Sign in with Google. Your plan is waiting there.</li>" in client.get("/").text  # m9, the mockup


def test_the_plan_card_can_open_the_muse_sheet_on_a_phone(client):
    # the dock's "Continue in Muse instead" is hidden under 720px, so the ready card carries its own opener
    # (D41: the sheet must be reachable from a phone, where Share sends the prompt to Muse)
    html = client.get("/").text
    ready = html.split('id="t-plan"')[1].split("</template>")[0]
    assert 'data-sheet="muse"' in ready and "Continue in Muse" in ready
    css = client.get("/static/home.css").text
    assert ".small-only { display: none; }" in css and ".small-only { display: inline-flex" in css


def test_open_muse_copies_the_prompt_first(client):
    html = client.get("/").text
    assert 'data-part="open">Open Muse</a>' in html and "Open Muse copies it for you" in html
    js = client.get("/static/home.js").text
    assert 'open.addEventListener("click", () => void copy())' in js
    # a phone gets the store page (the one address that opens an installed Muse app), a computer the web app
    for url in (
        "https://apps.apple.com/us/app/muse-from-meta/id6760173601",
        "https://play.google.com/store/apps/details?id=com.facebook.aura",
        "https://muse.ai",
    ):
        assert url in js, url


@pytest.fixture
def split(monkeypatch):
    """The edge split is live: the web host serves the website's pages next to the home. http, so the TestClient
    (http://testserver) still sends the session cookie a signed-in test needs."""
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "http://web.test")


def test_header_is_the_websites(client, split):
    r = client.get("/")
    visible = _first_visit_visible(r.text)
    assert 'aria-label="Admiry.ai home"' in visible and 'class="mark"' in visible
    assert "Admiry<span>.ai</span>" in visible
    assert 'href="/programmatic"' in visible and ">Programmatic<" in visible
    assert 'class="signin" href="/connect/google/start?next=%2F"' in visible  # straight to Google
    assert "ads.admiry.ai" not in r.text  # the advertiser console's login is not on this page


def test_signed_in_header_keeps_the_tab_and_the_account(client, google, split):
    sign_in(client, next_path="/")
    visible = _first_visit_visible(client.get("/").text)
    assert 'href="/programmatic"' in visible and 'class="account-menu"' in visible and 'class="signin"' not in visible


def test_fonts_are_self_hosted(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "font-src 'self'" in csp
    css = client.get("/static/base.css").text  # the shared stylesheet (settings redesign §6)
    assert 'url("/static/fonts/geist-latin.woff2")' in css and 'url("/static/fonts/geist-mono-latin.woff2")' in css
    for name in ("geist-latin.woff2", "geist-mono-latin.woff2"):
        f = client.get(f"/static/fonts/{name}")
        assert f.status_code == 200 and f.content[:4] == b"wOF2"


def test_prod_home_is_indexable_with_a_canonical(client, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "prod")
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://admiry.ai")
    r = client.get("/")
    canonical = '<link rel="canonical" href="https://admiry.ai/">'
    assert "noindex" not in r.text and canonical in r.text
    assert '<meta name="description"' in r.text
    # P8 on the render visitors get too; the canonical names the page, it is not a fetch
    assert not CROSS_ORIGIN_LOAD.search(r.text.replace(canonical, ""))


def test_fonts_are_typed_without_a_system_mime_table(conn, monkeypatch):
    """python:3.12-slim has no /etc/mime.types and Python's own table has no .woff2 (a Mac's
    /etc/apache2/mime.types hides that): the app registers the type itself, or the fonts go out untyped."""
    import mimetypes

    from fastapi.testclient import TestClient

    from agent.app import create_app

    monkeypatch.setattr(mimetypes, "_db", mimetypes.MimeTypes())  # Python's built-in table only, as in the image
    with TestClient(create_app()) as c:
        for name in ("geist-latin.woff2", "geist-mono-latin.woff2"):
            assert c.get(f"/static/fonts/{name}").headers["content-type"] == "font/woff2", name


def test_the_header_fits_a_phone_without_a_menu(client, split):
    """Plan 2026-09-29 global constraint: no menu button; on a phone Programmatic and Sign in / the account stay,
    Capabilities and the "soon" items hide. The signed-in nav needs ~880px (Geist Mono: 0.6em a letter + 0.1em
    tracking), so they hide below 920px; under 360px the nav tightens so brand + Programmatic + Sign in keep one row. The account menu
    (settings redesign §3) is the signed-in avatar's own <details>, not a menu button; the header rules moved to
    base.css and the phone rule hides only the nav's own links, never the menu's."""
    header = client.get("/").text.split('<header class="top">')[1].split("</header>")[0]
    assert "<button" not in header and "<details" not in header
    assert '<a class="programmatic" href="/programmatic">Programmatic</a>' in header  # not the sheet's .tab pill
    css = client.get("/static/base.css").text
    assert '<a href="/capabilities">Capabilities</a>' in header  # hidden on a phone: the home links it
    wide = css.split("@media (max-width: 920px) {")[1].split("\n}")[0]
    assert ".nav > a:not(.signin):not(.programmatic) { display: none; }" in wide
    assert "white-space: nowrap" in css.split(".nav > a {")[1].split("}")[0]  # a label never breaks into two lines
    tiny = css.split("@media (max-width: 359px) {")[1].split("\n}")[0]  # after the 720px block, as specific as it
    assert css.index("@media (max-width: 720px)") < css.index("@media (max-width: 359px)")
    assert ".nav .programmatic, .nav .signin { font-size: 12px; letter-spacing: 0.05em; }" in tiny
    # the header is sticky and 64px: a turn scrolled to "start" (the plan's intro) must land below it
    assert "scroll-padding-top: 80px" in css.split(":root {")[1].split("}")[0]


def test_the_setup_script_is_served_small_and_loaded_before_the_home_script(client):
    js = client.get("/static/setup.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    # 320 before S0 (2026-10-07) added the review step: Admiry's second look, the phone it asks for, Confirm and launch;
    # +4 no pay form while a payment is being confirmed, and the sentence when the follow gives up (B0 follow-up final
    # review I2), +1 a follow look that fails keeps the card waiting (B0 follow-up final review M6)
    assert js.text.count("\n") < 385 and "innerHTML" not in js.text and "eval(" not in js.text
    assert "/api/setup" in js.text and "window.AdmirySetup" in js.text
    html = client.get("/").text
    assert html.index('src="/static/setup.js"') < html.index('src="/static/home.js"')


def test_the_channels_script_is_served_small_and_loaded_before_the_home_script(client):
    """The channels' rows, a level's split and the channels card (M0) are static/channels.js: home.js calls it for
    every plan it draws, so it loads first, as setup.js does."""
    js = client.get("/static/channels.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert js.text.count("\n") < 120 and "innerHTML" not in js.text and "eval(" not in js.text
    assert "window.AdmiryChannels" in js.text
    html = client.get("/").text
    assert html.index('src="/static/channels.js"') < html.index('src="/static/home.js"')


def test_the_sites_script_is_served_small_and_loaded_between_the_channels_and_the_home_script(client):
    """The industry site card (A3p-6 batch 2 Task 7, Q17) is static/sites.js: home.js hands a site's state to it from
    the page card, so it loads before home.js, after channels.js."""
    js = client.get("/static/sites.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert js.text.count("\n") < 340 and "innerHTML" not in js.text and "eval(" not in js.text
    assert "window.AdmirySites" in js.text and "window.AdmirySites.card(" in client.get("/static/home.js").text
    html = client.get("/").text
    assert html.index('src="/static/channels.js"') < html.index('src="/static/sites.js"')
    assert html.index('src="/static/sites.js"') < html.index('src="/static/home.js"')


def test_the_launch_choices_are_buttons_that_open_the_cards_in_place(client):
    html = client.get("/").text
    # the two launch ways, Admiry running the ads first (founder's ruling, 2026-10-07): keyed by the actions' ids, since
    # tools_consult.MANAGED_ACTION and LINK_ACTION share connect_channel (PREPAY_ACTION is request_payment_link)
    launch = html.split('<div class="launch-buttons">')[1].split("</div>")[0]
    # L1: "Confirm and launch" first, drawn once the checklist is clear (tools_consult.CONFIRM_ACTION)
    assert re.findall(r'data-id="(\w+)"', launch) == ["confirm", "prepay", "link_own_account", "sign_in"]
    assert "/connect?setup=ads" not in html
    # the funds are offered only for a place Admiry runs from the balance (NewsBreak), not Google Search: its line says
    # the platform's cost, not Google's (walk fixes 1, final review B M4)
    prepay = html.split('data-id="prepay"')[1].split("</button>")[0]
    assert (
        "<small>Admiry runs your Google Ads; you prepay what they spend, at the platform's exact cost</small>" in prepay
    )
    assert "Google's exact cost" not in html


def test_the_plan_template_ships_the_ad_preview_card(client):
    """A3 Task 5: "How your ad reads" (spec §7 ads_preview) ships in every page load, like every other card."""
    html = client.get("/").text
    ready = html.split('id="t-plan"')[1].split("</template>")[0]
    assert 'data-part="ad-preview"' in ready and "How your ad reads" in ready
    for part in ("ad-headlines", "ad-desc", "ad-host", "ad-status"):
        assert f'data-part="{part}"' in ready
    js = client.get("/static/home.js").text
    assert "adPreviewCard" in js and "ad-preview" in js


def test_the_state_action_carries_the_ad_preview_and_the_live_since_line(client, conn, fake, google):  # noqa: F811 (fixture)
    """A3 Task 5: cards.ads_preview.final_host is real once a page is set, and execution is the live badge --
    both computed in home_api.py, neither baked into plans.cards() itself."""
    from agent.tests.test_oauth_consent import _sign_in

    benchmark.seed(conn)
    turn(
        client,
        {"action": "start", "text": "dentist in Palo Alto within 5 miles, $2,000 a month", "idempotency_key": "i1"},
    )
    turn(client, SITE)  # the website, first: the words name none
    turn(client, PICK)  # the business on Google Maps, next: it centres the ads
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    turn(client, {"action": "answer", "answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "a2"})
    turn(client, {"action": "answer", "answers": {"customer_value": {"choice": "1000_plus"}}, "idempotency_key": "a3"})
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    s = turn(client, {"action": "state"}).json()["result"]
    preview = s["cards"]["ads_preview"]
    assert preview["headlines"] and preview["final_host"] is None  # no page on file yet
    assert s["execution"] is None  # nothing approved yet: plan.execution is not even set

    from psycopg.types.json import Jsonb

    from agent import landing

    biz_id = conn.execute("SELECT id FROM business LIMIT 1").fetchone()["id"]
    plan_id = conn.execute("SELECT id FROM plan ORDER BY version DESC LIMIT 1").fetchone()["id"]
    landing.set_owner_url(conn, biz_id, "https://brightsmile.example/invisalign")
    # a real approval sets plan.json.execution (proposals.apply_approve_plan); this test reaches the same
    # invariant directly rather than driving the whole verify/approval-code flow just to get there.
    conn.execute(
        "UPDATE plan SET status = 'approved', json = json || %s WHERE id = %s",
        (Jsonb({"execution": {"ads": "live"}}), plan_id),
    )
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, launched_at) "
        "VALUES (%s, 'google_search', %s, 'live', now())",
        (biz_id, plan_id),
    )
    s2 = turn(client, {"action": "state"}).json()["result"]
    assert s2["cards"]["ads_preview"]["final_host"] == "brightsmile.example"
    assert s2["execution"] == "Live since today"  # execution is still None on plan.json: this reads channel_campaign


def test_head_answers_like_get_on_the_browser_pages(client):
    """`curl -I https://admiry.ai/` and the edge's HEAD redirects (spec 2026-09-29 §4) land on the page, not on the
    MCP mount's 404: FastAPI's GET routes do not take HEAD by themselves (the app adds it; uvicorn sends no body)."""
    for path in (
        "/",
        "/connect",
        "/legal/management-agreement",
        "/connect/docs",
        "/health",
        "/pay/biz_x",
        "/oauth/consent",
    ):
        got, head = client.get(path, follow_redirects=False), client.head(path, follow_redirects=False)
        assert head.status_code == got.status_code and head.status_code in (200, 303), (path, head.status_code)
        assert head.headers.get("content-type") == got.headers.get("content-type"), path
        assert head.headers.get("location") == got.headers.get("location"), path
        assert head.content == b"", path


def test_the_front_page_links_the_policies_and_carries_the_websites_icon(client, split):
    """The home is admiry.ai's front page: its footer links Privacy and Terms, as the website's front page did (spec
    2026-09-24 §7 item 1; Google's brand verification looks for the homepage's link), and its icon is the website's
    (/privacy, /terms and /icon* are on the edge's website path list, deploy/tests/test_home_edge.py)."""
    visible = _first_visit_visible(client.get("/").text)
    foot = visible.split('<footer id="foot" class="foot">')[1].split("</footer>")[0]
    assert '<a href="/privacy">Privacy</a>' in foot and '<a href="/terms">Terms</a>' in foot
    # Google's third-party policy (founder 2026-10-03): its guide linked in the homepage's footer, as pay.py mails it
    assert f'<a href="{pay.THIRD_PARTY_GUIDE}" rel="noopener">Working with third parties (Google)</a>' in foot
    assert '<link rel="icon" href="/icon.svg?v=2" type="image/svg+xml">' in visible


STATIC = Path(__file__).parents[1] / "static"
CHANNELS_JS, HOME_JS = STATIC / "channels.js", STATIC / "home.js"
# channels.js and home.js under Node, in the page's order, with a permissive stub DOM (no jsdom here): every element
# records its text and children, a `[data-part=…]` lookup answers the same element each time, and `fetch` answers the
# `state` action with a planned session and the cards given. Prints what the plan card rendered: its rows, the level's
# split, and "Runs on".
HOME_HARNESS = r"""
const [channels, src] = [process.argv[1], process.argv[2]].map((f) => require("fs").readFileSync(f, "utf8"));
const state = JSON.parse(process.argv[3]);
const node = (tag) => {
  const n = { tag, className: "", text: "", kids: [], parts: {}, dataset: {}, hidden: false, style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
    append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
    replaceChildren(...cs) { this.kids = cs; }, get children() { return this.kids; },
    querySelector(sel) { const m = /data-part="([^"]+)"/.exec(sel), k = m ? m[1] : sel;
      return (this.parts[k] ||= node("part:" + k)); },
    querySelectorAll() { return []; }, closest() { return node("closest"); }, get parentElement() { return node("up"); },
    get content() { return { firstElementChild: node("template") }; }, cloneNode() { return node(tag); },
    remove() {}, replaceWith() {}, addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {},
    scrollIntoView() {}, isConnected: true };
  return n;
};
const byId = {};
const doc = { body: node("body"), activeElement: null, createElement: node, createTextNode: (t) => ({ text: t, kids: [] }),
  getElementById: (id) => (byId[id] ||= node("#" + id)), querySelectorAll: () => [], addEventListener() {} };
doc.body.dataset.signedIn = "1";
byId.prompts = node("#prompts");
byId.prompts.textContent = "{}";
Object.assign(global, { document: doc, matchMedia: () => ({ matches: true }), location: { search: "", pathname: "/" },
  history: { replaceState() {} }, window: { addEventListener() {} },
  fetch: async () => ({ status: 200, headers: { get: () => null }, json: async () => state }) });
eval(channels);
eval(src);
const started = Date.now();
const report = () => {  // once the plan is on the page (the boot is async), or after 10 s with an error
  const plan = byId.log && byId.log.kids.find((k) => k.parts && k.parts.channels);
  if (!plan) return Date.now() - started < 10000 ? setTimeout(report, 20) : process.exit(3);
  const rows = plan.parts.channels.kids.map((li) => li.kids[0].kids.map((p) => p.text).concat(li.kids[1].text));
  const summary = plan.parts.summary.kids.map((r) => r.kids.map((c) => c.text));
  console.log(JSON.stringify({ rows, split: plan.parts.split.text, monthly: plan.parts.monthly.text, summary }));
};
setTimeout(report, 20);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_plan_card_renders_every_row_and_the_levels_split(client, conn, monkeypatch, tmp_path):
    """M0 Task 5 (spec 2026-09-29 §7.2): the homepage's plan card shows one row per funded channel -- its amount and
    who runs it under the reason -- and the later lines; the budget card shows the split of the level it sits on;
    "Runs on" names every channel."""
    from agent import plans
    from agent.channels import registry
    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import FakeAdapter, creative_set
    from agent.tests.test_plans_channels import NB_WHY, home_plan

    monkeypatch.setitem(registry.CHANNELS, "microsoft_search", FakeAdapter("microsoft_search"))
    monkeypatch.setenv("AGENT_EXEC_MICROSOFT_SEARCH", "validate")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    monkeypatch.setattr(creative, "ROOT", creative_set(tmp_path / "creative"))  # NewsBreak needs the trade's picture
    sections, _ = home_plan(conn, prepaid=True)  # a prepaid business: the one kind whose plan can have NewsBreak
    cards = plans.cards({"id": "pl_1", "business_id": "biz_1", "business_name": "Summit Roofing", "sections": sections})
    state = {
        "ok": True,
        "visitor": {"signed_in": True, "claimed": True, "wall": False},
        "result": {"session": {"status": "planned"}, "cards": cards, "plan_id": "pl_0123456789abcdef"},
    }
    done = subprocess.run(
        ["node", "-e", HOME_HARNESS, str(CHANNELS_JS), str(HOME_JS), json.dumps(state)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["rows"][:3] == [
        [
            "Google Search",
            "Google Search gets most of it because people there are already looking for Roofing near you.",
            "$600 a month · Admiry runs it from your balance",
            "primary",
        ],
        ["NewsBreak", NB_WHY, "$300 a month · Admiry runs it from your balance", "test"],
        [
            "Bing Search",
            "Bing Search gets $100 a month: the same searches as on Google, at a lower price per click.",
            "$100 a month · your own Bing Search account",
            "secondary",
        ],
    ]
    assert [r[0] for r in got["rows"][3:]] == ["Facebook & Instagram", "ChatGPT", "Yelp", "TikTok"]
    assert all(r[-1] == "later" for r in got["rows"][3:])
    assert got["monthly"] == "about $1,000 a month"
    assert got["split"] == "Google Search $600 · NewsBreak $300 · Bing Search $100"  # the recommended level's
    assert ["Runs on", "Google Search, NewsBreak, Bing Search"] in got["summary"]


def test_home_labels_are_the_registrys():
    """The homepage (channels.js) names a level's channels with D76's owner names, as channels.registry.LABELS has
    them."""
    from agent.channels import registry

    js = CHANNELS_JS.read_text()
    table = js.split("const LABELS = {")[1].split("};")[0]
    assert dict(re.findall(r'(\w+): "([^"]+)"', table)) == registry.LABELS


# home.js's launch card under Node, read out of the script as test_feedback reads plan(): `grab` takes one of its
# functions (two spaces in, ending with a line of "  }"), `line` one line from the top of the script. The "Ready to
# launch" section is built as home.html ships it -- its two lines, the three launch buttons, the chips, the plan link --
# in a tree with just what the page's code touches, and `drawn` reports what is left of it: the line above the
# buttons, the sign-in line, the launch buttons and the chips.
LAUNCH_TREE = r"""
const vm = require("vm");
const lines = require("fs").readFileSync(process.argv[1], "utf8").split("\n");
const input = JSON.parse(process.argv[2]);
const grab = (name) => {
  const from = lines.findIndex((l) => l.startsWith(`  function ${name}(`) || l.startsWith(`  async function ${name}(`));
  const to = lines.findIndex((l, i) => i > from && l === "  }");
  if (from < 0 || to < 0) throw new Error(`home.js has no function ${name} in the shape this reads`);
  return lines.slice(from, to + 1).join("\n");
};
const line = (start) => {
  const found = lines.find((l) => l.startsWith(start));
  if (!found) throw new Error(`home.js has no line starting "${start}"`);
  return found;
};
class Node {
  constructor(data = {}, cls = "", text = "", tag = "P") {
    Object.assign(this, { dataset: data, cls: new Set(cls.split(" ").filter(Boolean)), text, tagName: tag, kids: [] });
    this.up = null;
  }
  get classList() {
    const c = this.cls;
    return { contains: (x) => c.has(x), toggle: (x, on) => (on ? c.add(x) : c.delete(x)) };
  }
  set textContent(v) { this.text = String(v); }
  get textContent() { return this.text; }
  get parentElement() { return this.up; }
  get isConnected() { let n = this; while (n.up) n = n.up; return n.cls.has("log"); }
  add(...kids) { kids.forEach((k) => { k.remove(); k.up = this; this.kids.push(k); }); return this; }
  append(...kids) { return this.add(...kids); }
  remove() { if (this.up) this.up.kids.splice(this.up.kids.indexOf(this), 1); this.up = null; }
  replaceWith(n) { const up = this.up; this.remove(); up.add(n); }
  addEventListener() {}
  all() { return this.kids.flatMap((k) => [k, ...k.all()]); }
  querySelectorAll(sel) { // ".class", "[data-x]" and "[data-x=\"y\"]"; a space for a descendant, a comma for either
    const one = (n, s) => {
      if (s.startsWith(".")) return n.cls.has(s.slice(1));
      const m = /^\[data-([a-z]+)(?:="([^"]*)")?\]$/.exec(s);
      return Boolean(m) && m[1] in n.dataset && (m[2] === undefined || n.dataset[m[1]] === m[2]);
    };
    const path = (p) => p.trim().split(" ").reduce((at, s) => at.flatMap((n) => n.all().filter((d) => one(d, s))), [this]);
    return [...new Set(sel.split(",").flatMap(path))];
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const button = (tool, cls, id) =>
  new Node(id ? { id, tool } : { tool }, cls, "", "BUTTON").add(new Node({ part: "label" }, "", "", "SPAN"));
const launchSection = () => new Node({}, "card launch", "", "SECTION").add(
  new Node({ part: "summary" }, "grid", "", "DL"),
  new Node({ when: "launch" }, "msg", input.words.launch),
  new Node({ when: "sign-in" }, "msg", input.words.signIn),
  new Node({}, "launch-buttons", "", "DIV").add(
    ...[["confirm", "launch_marketing_plan"], ["prepay", "request_payment_link"], ["link_own_account", "connect_channel"],
        ["sign_in", "request_connect_link"]].map(([id, t]) => button(t, "btn two", id))),
  new Node({}, "chips", "", "DIV").add(button("adjust", "ghost"), new Node({ say: "Change the area to " }, "ghost")),
  new Node({}, "fine").add(new Node({ part: "plan-link" }, "", "", "A")),
);
const drawn = (n) => {
  const said = (when) => (n.querySelector(`[data-when="${when}"]`) || { textContent: null }).textContent;
  const kept = (box) => n.querySelectorAll(`.${box} [data-tool]`);
  const tools = (bs) => bs.map((b) => b.dataset.id || b.dataset.tool); // a launch button by its action's id
  const dark = tools(kept("launch-buttons").filter((b) => b.cls.has("dark"))); // the primary style
  return { launch: said("launch"), signIn: said("sign-in"), buttons: tools(kept("launch-buttons")), dark,
    chips: tools(kept("chips")) };
};
const calls = [];
const helpers = { rows() {}, usd: String, span: String, cap: String, safeHref: (u) => u, act() {}, grow() {}, reply: {},
  checkPlan: () => calls.push("check"), reviewing: () => calls.push("reviewing"), changesLine() {}, // Task 8: not under test here
  OWN_WORDS: new Set(["prepay", "link_own_account"]) };
"""
# launchCard alone, once per case over a fresh section: the case's `next` and `note` (the answer's launch_note).
LAUNCH_HARNESS = (
    LAUNCH_TREE
    + r"""
const S = { planUrl: "/plan/pl_0123456789abcdef", planStatus: null, claimed: false };
const launchCard = vm.runInContext(
  [line("  const part = "), grab("launchCard"), "launchCard;"].join("\n"), vm.createContext({ S, ...helpers }));
console.log(JSON.stringify(input.cases.map(({ next, note, cards }) => {
  const n = launchSection();
  launchCard(n, cards || {}, next, note);
  return drawn(n);
})));
"""
)
# The same section drawn by the page's own plan() twice, each in a fresh page: as a build's answer arrives (show() hands
# plan() the answer's data and next), and as a reload's `state` answer arrives (resume()). Prints both.
RELOAD_HARNESS = (
    LAUNCH_TREE
    + r"""
const page = () => {
  const log = new Node({}, "log"), S = { planNode: null, planId: null, cards: null, wall: false, planStatus: null, claimed: false };
  const ctx = vm.createContext({
    ...helpers, S, log, PLAN_INTRO: "Here is the plan.", returning: false, BUILD: {}, CARD: {},
    window: { AdmiryChannels: { card: () => new Node({}, "channels") }, AdmirySupport: null },
    clone: () => new Node({}, "plan").add(launchSection()), add: (n) => log.add(n), admiry: () => new Node({}, "turn"),
    planCard() {}, budgetCard() {}, economicsCard() {}, whyCard() {}, adPreviewCard() {}, pageCard() {}, progress() {},
    reveal() {}, visitor() {}, moment() {}, you() {}, toWall() {}, oops() {}, firstVisit() {}, recommend() {},
    actions() {}, question() {}, blocker: () => null,
  });
  const code = [line("  const part = "), grab("launchCard"), grab("plan"), grab("resume"), "({ plan, resume });"];
  return { S, ...vm.runInContext(code.join("\n"), ctx) };
};
(async () => {
  const built = page(), reloaded = page();
  built.plan(input.built.data, input.built.next, "Here is the plan.");
  await reloaded.resume({ ok: true, result: input.state, visitor: {} }, false);
  console.log(JSON.stringify({ built: drawn(built.S.planNode), reloaded: drawn(reloaded.S.planNode) }));
})();
"""
)


def _launch_words(client) -> dict:
    """The "Ready to launch" section's two lines as the page ships them (its three launch buttons checked first)."""
    section = client.get("/").text.split('<section class="card launch"')[1].split("</section>")[0]
    buttons = section.split('<div class="launch-buttons">')[1].split("</div>")[0]
    assert re.findall(r'data-id="(\w+)"', buttons) == ["confirm", "prepay", "link_own_account", "sign_in"]
    return {
        "launch": re.search(r'<p class="msg" data-when="launch">([^<]*)</p>', section).group(1),
        "signIn": re.search(r'<p class="msg" data-when="sign-in">([^<]*)</p>', section).group(1),
    }


def _node(harness: str, payload: dict):
    """What `harness` prints for `payload`, run under Node over the page's home.js."""
    run = subprocess.run(
        ["node", "-e", harness, str(HOME_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_launch_card_says_what_is_offered_and_sign_in_only_to_a_draft(client):
    """Walk fixes 1, Task 6: the plan offers only ways that exist, and the "Ready to launch" card shows the actions it
    was given. The line above the launch buttons follows their number: the two ways for two, "Next step:" for one, and
    for none the answer's launch_note -- why nothing can launch yet -- or, without one, nothing. "Sign in first" is said
    only beside a draft key's sign-in (tools_consult.SIGN_IN_ACTIONS), never because the own account's link is not
    offered. A card given no actions draws none: the page keeps no list of its own (round 1). The one way there is
    leads in the primary style, prepaying alone too, as the own account's link does (round 2)."""
    from agent import envelope, tools_consult

    words = _launch_words(client)
    assert words["launch"] == "Everything is checked. Pick how your ads run:"
    link, prepay, adjust = tools_consult.LINK_ACTION, tools_consult.PREPAY_ACTION, tools_consult.ADJUST_ACTION
    waits = tools_consult.PREPAID_WAITS_SAY
    cases = [
        {"next": envelope.actions(link, adjust)},  # its own Google Ads account, Google Search alone
        {"next": envelope.actions(prepay, adjust)},  # prepays: NewsBreak runs from the balance
        {"next": envelope.actions(adjust), "note": waits},  # prepays, Google Search alone: why nothing can launch yet
        {"next": envelope.actions(adjust)},  # nothing to launch and no note: no line
        {"next": envelope.actions(link, prepay, adjust)},  # two ways
        # Admiry running it first, the own account second: both connect_channel, told apart by id (ruling 2026-10-07),
        # drawn alike -- neither in the primary style (founder 2026-10-13: the home shows both ways equally)
        {"next": envelope.actions(tools_consult.MANAGED_ACTION, tools_consult.OWN_ACCOUNT_ACTION, adjust)},
        {"next": tools_consult.SIGN_IN_ACTIONS},  # a draft key signs in first
        {"next": None},  # no actions in the answer: none drawn
    ]
    two = ["prepay", "link_own_account"]
    assert _node(LAUNCH_HARNESS, {"words": words, "cases": cases}) == [
        {
            "launch": "Next step:",
            "signIn": None,
            "buttons": ["link_own_account"],
            "dark": ["link_own_account"],
            "chips": ["adjust"],
        },
        {"launch": "Next step:", "signIn": None, "buttons": ["prepay"], "dark": ["prepay"], "chips": ["adjust"]},
        {"launch": waits, "signIn": None, "buttons": [], "dark": [], "chips": ["adjust"]},
        {"launch": None, "signIn": None, "buttons": [], "dark": [], "chips": ["adjust"]},
        {"launch": words["launch"], "signIn": None, "buttons": two, "dark": ["link_own_account"], "chips": ["adjust"]},
        {"launch": words["launch"], "signIn": None, "buttons": two, "dark": [], "chips": ["adjust"]},
        {
            "launch": None,
            "signIn": words["signIn"],
            "buttons": ["sign_in"],
            "dark": ["sign_in"],
            "chips": ["adjust"],
        },
        {"launch": None, "signIn": None, "buttons": [], "dark": [], "chips": []},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_launch_card_leaves_the_four_ways_to_the_website_card(client):
    """Ruling R2-c: the plan answer's `next` lists the four ways (three of them update_landing_page's) before the launch
    actions; the homepage shows them once, on the "Your website" card, so the launch card keyed by tool draws what the
    launch actions alone draw."""
    from agent import envelope, site_install, tools_consult

    words = _launch_words(client)
    link, adjust = tools_consult.LINK_ACTION, tools_consult.ADJUST_ACTION
    row = {"state": "needs_code", "say": "x", "platform": "other", "steps": [], "ways": site_install.WAYS}
    cards = {"plan": {"setup": {"website": row}}}
    cases = [
        {"next": envelope.actions(link, adjust)},
        {"next": envelope.actions(*site_install.WAYS, link, adjust), "cards": cards},
        {"next": envelope.actions(*site_install.WAYS, adjust), "cards": cards},
        {"next": envelope.actions(adjust)},
    ]
    seen = _node(LAUNCH_HARNESS, {"words": words, "cases": cases})
    assert seen[1] == seen[0] and seen[2] == seen[3]
    assert seen[1]["buttons"] == ["link_own_account"] and seen[1]["chips"] == ["adjust"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
@pytest.mark.parametrize("prepaid, google_open", [(False, True), (True, True), (True, False)])
@pytest.mark.usefixtures("fake")
def test_a_reload_draws_the_launch_card_the_build_drew(client, conn, google, monkeypatch, prepaid, google_open):
    """Walk fixes 1, Task 6 round 1: the `state` answer carries the plan's own actions and launch_note, so the card
    resume() draws on a reload is the one plan() drew from the build's answer -- for a business with its own Google Ads
    account its link and a change, never the prepaying the page's own old list offered; for one that prepays, with
    Google Search alone, the balance since A3c-1 (`plans.google_open`), and before it the line that says why nothing
    can launch yet."""
    from agent import plans, tools_consult
    from agent.tests.test_home_api import _claimed_plan

    monkeypatch.setattr(plans, "google_open", lambda: google_open)
    built = _claimed_plan(client, conn, google, prepaid=prepaid)
    state = turn(client, {"action": "state"}).json()["result"]
    seen = _node(RELOAD_HARNESS, {"words": _launch_words(client), "built": built, "state": state})
    assert seen["reloaded"] == seen["built"]
    if prepaid and not google_open:
        assert seen["reloaded"] == {
            "launch": tools_consult.PREPAID_WAITS_SAY,
            "signIn": None,
            "buttons": [],
            "dark": [],
            "chips": ["adjust"],
        }
    elif prepaid:
        assert seen["reloaded"] == {
            "launch": "Next step:",
            "signIn": None,
            "buttons": ["prepay"],
            "dark": ["prepay"],
            "chips": ["adjust"],
        }
    else:  # Admiry running it first, its own account second (2026-10-07), drawn alike (founder 2026-10-13)
        assert seen["reloaded"] == {
            "launch": _launch_words(client)["launch"],
            "signIn": None,
            "buttons": ["prepay", "link_own_account"],
            "dark": [],
            "chips": ["adjust"],
        }


# home.js's withoutQuestion alone (read out of the script as above), over each case's `say` and question
QUESTION_HARNESS = (
    LAUNCH_TREE
    + r"""
const withoutQuestion = vm.runInContext([grab("withoutQuestion"), "withoutQuestion;"].join("\n"), vm.createContext({}));
console.log(JSON.stringify(input.cases.map(({ say, question }) => withoutQuestion(say, question))));
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_question_drawn_as_a_card_is_not_also_in_the_message():
    """Walk fixes 1, Task 4 round 1: `say` ends with the question as consult.question_sentence writes it, for an
    assistant that shows only text; the page draws the question as a card, so its message keeps only what comes
    before. Each of the sentence's three forms: an open question (where the ads centre: no options), a multi one (how
    many it takes) and a single one -- and the places of the listing question, whose labels hold commas and are set
    apart with semicolons (final review B M1)."""
    from agent import consult, envelope

    location = next(q for q in consult.load_tree("dentists")["questions"] if q["id"] == "location")
    options = [{"id": "a", "label": "Invisalign"}, {"id": "b", "label": "Cleanings"}]
    offered = [
        {"id": "gm_1", "label": "Bright Smile Dental, 500 University Ave, Palo Alto, CA", "sublabel": "Google Maps"},
        {"id": "gm_2", "label": "Smile Studio, 1 Main St, Palo Alto, CA 94301", "sublabel": "Google Maps"},
        consult.LISTING_NONE,
    ]
    questions = [
        envelope.question(id="location", index=4, count=5, prompt=location["prompt"], input="address", options=[]),
        envelope.question(id="service", index=2, count=5, prompt="Which service?", input="multi", options=options),
        envelope.question(
            id="objective", index=1, count=5, prompt="What do you want?", input="single", options=options
        ),
        envelope.question(id="listing", index=1, count=6, prompt="Which place?", input="single", options=offered),
    ]
    said = "Centred on 123 Main St, Houston, TX 77002."
    cases = [{"say": f"{said} {consult.question_sentence(q)}", "question": q["question"]} for q in questions]
    assert _node(QUESTION_HARNESS, {"cases": cases}) == [said, said, said, said]


def test_before_the_edge_split_the_website_links_go_to_the_live_website(client):
    """An agent image can reach a box before its Caddyfile does (the release lane moves images only), and dev/local
    have no web host: without AGENT_WEB_BASE_URL the website's pages are not next to the home, so its links are
    absolute and the icon stays the empty one (img-src is 'self')."""
    visible = _first_visit_visible(client.get("/").text)
    assert 'href="https://admiry.ai/programmatic"' in visible
    assert 'href="https://admiry.ai/privacy"' in visible and 'href="https://admiry.ai/terms"' in visible
    assert '<link rel="icon" href="data:,">' in visible and "/icon.svg" not in visible


# ---------------------------------------------------------------- the channels card (M0 Task 6, spec 2026-09-29 §7.2)

MOCK = Path(__file__).parents[2] / "docs" / "superpowers" / "mocks" / "channels-card.html"
CHANNELS_VIEW = Path(__file__).parents[1] / "templates" / "channels_view.html"
# HOME_HARNESS's page with another report: the channels card home.js put at the top of the plan -- each row's dot, its
# lines and its actions (tag, accessible name -- its words and the screen-reader words after them -- and address) --
# its footer, where its Settings link goes, and what the money mount holds (M1m fills it; empty here).
CHANNELS_HARNESS = (
    HOME_HARNESS.split("const started = Date.now();")[0]
    + r"""
const started = Date.now();
const report = () => {
  const plan = byId.log && byId.log.kids.find((k) => k.parts && k.parts.channels);
  if (!plan) return Date.now() - started < 10000 ? setTimeout(report, 20) : process.exit(3);
  const card = plan.kids.find((k) => k.parts && k.parts.rows);
  if (!card) return console.log("null");
  const name = (b) => b.text + b.kids.map((k) => k.text).join("");
  const rows = card.parts.rows.kids.map((li) => ({ dot: li.kids[0].className,
    lines: li.kids[1].kids.map((p) => p.text), actions: li.kids.slice(2).map((b) => [b.tag, name(b), b.href || null]) }));
  console.log(JSON.stringify({ rows, footer: card.parts.footer.text, settings: card.parts.settings.href || null,
    money: card.parts.money ? card.parts.money.kids.length : 0 }));
};
setTimeout(report, 20);
"""
)


def _render_channels(state: dict) -> dict | None:
    """What the homepage (home.js, drawing with channels.js) shows for the channels card when the `state` action
    answers `state`."""
    done = subprocess.run(
        ["node", "-e", CHANNELS_HARNESS, str(CHANNELS_JS), str(HOME_JS), json.dumps(state)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _fixture() -> dict:
    """The mock's fixture MarketingState."""
    return json.loads(MOCK.read_text().split('<script type="application/json" id="fixture">')[1].split("</script>")[0])


def _mock_card() -> dict:
    """The mock's desktop card, read back the way the harness reports a card."""
    html = MOCK.read_text().split('<section class="frame desktop"')[1].split('<section class="frame phone"')[0]
    rows = []
    for li in html.split('<li class="where-row">')[1:]:
        lines = [unescape(t) for t in re.findall(r'<p class="w-(?:name|state|meta)">([^<]*)</p>', li)]
        button = r'<button type="button" class="ghost">([^<]*)(?:<span class="sr">([^<]*)</span>)?</button>'
        actions = [["button", unescape(shown + unseen), None] for shown, unseen in re.findall(button, li)]
        rows.append({"dot": re.search(r'<span class="(dot [a-z]+)"', li).group(1), "lines": lines, "actions": actions})
    footer = unescape(re.search(r'<p class="where-foot">([^<]*)</p>', html).group(1))
    return {"rows": rows, "footer": footer}


def _planned(cards: dict, business_id: str = "biz_mock") -> dict:
    """The `state` action's answer for a planned session with these cards."""
    return {
        "ok": True,
        "visitor": {"signed_in": True, "claimed": True, "wall": False},
        "result": {
            "session": {"status": "planned"},
            "cards": cards,
            "plan_id": "pl_0123456789abcdef",
            "business_id": business_id,
        },
    }


def _plan_cards(conn) -> dict:
    """A real plan's cards (plans.cards), which the homepage draws under the channels card."""
    from agent import plans
    from agent.tests.test_plans_channels import home_plan

    sections, _ = home_plan(conn)
    return plans.cards({"id": "pl_1", "business_id": "biz_1", "business_name": "Summit Roofing", "sections": sections})


def _with_the_plan(conn, fixture: dict) -> dict:
    """The fixture state's cards, with a real plan's parts in place of its empty ones."""
    from agent import state

    cards = state.MarketingState(**fixture).cards()
    return cards | {k: v for k, v in _plan_cards(conn).items() if k in state.PLAN_PARTS}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_channels_card_reads_as_the_mock(conn):
    """Plan P9: the card is built from the mock -- home.js draws the mock's fixture state with the mock's words, dots
    and buttons, the link to the business's own Your ads page, and the money mount left empty for M1m."""
    got = _render_channels(_planned(_with_the_plan(conn, _fixture())))
    assert {"rows": got["rows"], "footer": got["footer"]} == _mock_card()
    assert [a for r in got["rows"] for a in r["actions"]] == [  # a button names its channel to a screen reader
        ["button", "Pause on Google Search", None],
        ["button", "Resume on Bing Search", None],
    ]
    assert got["settings"] == "/ads?b=biz_mock" and got["money"] == 0
    words = " ".join([*(line for r in got["rows"] for line in r["lines"]), got["footer"]])
    assert not re.search(r"campaign|ad group|wallet", words, re.IGNORECASE)
    assert not JARGON.search(words) and not ACCOUNT_JARGON.search(words) and not MONEY_JARGON.search(words)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_connect_row_links_to_its_settings_row_and_an_issue_turns_the_dot_red(conn):
    fixture = _fixture()
    google, nb, _bing = fixture["channels"]
    google.update(state="waiting_account", state_text="Waiting for your account", actions=["connect"])
    google.update(today_usd=None, day_cap_usd=None, calls_7d=0, leads_7d=0)  # nothing built: nothing spends
    nb.update(state_text='Needs attention: "Your account is under review."', issues=[{"since": "2026-09-29"}])
    fixture["channels"] = [google, nb]
    got = _render_channels(_planned(_with_the_plan(conn, fixture)))
    # each action names its channel for a screen reader, as Settings' do (fix round 1, Minor 9)
    assert got["rows"][0]["actions"] == [["a", "Connect on Google Search", "/ads?b=biz_mock#google_search"]]
    assert (got["rows"][0]["dot"], got["rows"][1]["dot"]) == ("dot wait", "dot bad")
    assert got["rows"][0]["lines"] == ["Google Search", "Waiting for your account"]  # nothing spends: no numbers
    assert got["footer"] == "Month to date $742 of $1,516 · next report Monday"


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_plan_before_launch_has_no_channels_card(conn):
    assert _render_channels(_planned(_plan_cards(conn))) is None


def test_the_mock_is_a_state_the_model_takes_and_loads_nothing(client):
    from agent import state

    html = MOCK.read_text()
    assert state.MarketingState(**_fixture()).version == 1
    assert "@font-face" not in html and "fonts.googleapis" not in html  # no web fonts
    assert not re.search(r"""(src|href)=["']https?:""", html) and "<script src" not in html  # nothing loaded
    base = client.get("/static/base.css").text
    for name, value in re.findall(r"--(bg|card|fg|soft|muted|line|accent|ok|error): (#[0-9A-F]{6})", html):
        assert f"--{name}: {value}" in base, name  # the homepage's own tokens


def test_the_home_page_ships_the_channels_card(client):
    html = client.get("/").text
    card = html.split('<template id="t-channels">')[1].split("</template>")[0]
    for part in ("rows", "footer", "settings", "money"):
        assert f'data-part="{part}"' in card, part
    assert "Where your ads run" in card
    css = client.get("/static/home.css").text
    for rule in (".where-row {", ".dot.on {", ".dot.wait {", ".dot.bad {", ".w-meta {", ".where-foot {"):
        assert rule in css, rule
    js = client.get("/static/home.js").text
    assert "AdmiryChannels.card(" in js and "/connect/ads/" in client.get("/static/channels.js").text
    assert "Welcome back. Here is where your ads run." in js  # a reload after launch opens on the channels card


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js and the channels view with node")
def test_state_is_one_model_everywhere(client, conn, fake, google):  # noqa: F811 -- the fixtures
    """Review Focus 5 (plan P10, spec D102): the homepage's `state` action, GET /api/state, get_channel_status and the
    MCP channels view show the same rows, all from one MarketingState; the card the homepage draws from them says
    no "campaign", "ad group" or "wallet"."""
    from psycopg.types.json import Jsonb

    from agent import ads_run, config, keys, state
    from agent.tests.conftest import call_tool, data
    from agent.tests.test_mcp_view import HARNESS
    from agent.tests.test_oauth_consent import _sign_in

    benchmark.seed(conn)
    text = "dentist in Palo Alto within 5 miles, $2,000 a month"
    turn(client, {"action": "start", "text": text, "idempotency_key": "i1"})
    turn(client, SITE)  # the website, first: the words name none
    turn(client, PICK)  # the business on Google Maps, next: it centres the ads
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    turn(client, {"action": "answer", "answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "a2"})
    turn(client, {"action": "answer", "answers": {"customer_value": {"choice": "1000_plus"}}, "idempotency_key": "a3"})
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com'")
    _sign_in(client, google)
    turn(client, {"action": "recommend", "idempotency_key": "r1"})
    biz = conn.execute("SELECT id FROM business LIMIT 1").fetchone()["id"]
    plan_id = conn.execute("SELECT id FROM plan ORDER BY version DESC LIMIT 1").fetchone()["id"]
    conn.execute(  # approved, as proposals.apply_approve_plan leaves it (as the ad preview test above does)
        "UPDATE plan SET status = 'approved', json = json || %s WHERE id = %s",
        (Jsonb({"execution": {"ads": "live"}}), plan_id),
    )
    for channel, row_state, error in (
        ("google_search", "live", None),
        ("newsbreak", "waiting_account", ads_run.NO_FUNDS.format(label="NewsBreak")),
    ):
        conn.execute(
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, last_error, launched_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now())",
            (biz, channel, plan_id, row_state, Jsonb({"campaign": "c/1"} if row_state == "live" else {}), error),
        )
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, 'google_search', %s, 12500000, 90, 7, 2, 0, 1, 0, 'api')",
        (biz, ads_run.account_today(None)),
    )
    expected = state.build(conn, config.load(), biz).cards()["channels"]["rows"]
    assert [r["channel"] for r in expected] == ["google_search", "newsbreak"] and expected[0]["today_usd"] == 12.5

    home = turn(client, {"action": "state"}).json()
    assert home["result"]["cards"]["channels"]["rows"] == expected
    api = client.get("/api/state")
    assert api.status_code == 200 and api.headers["cache-control"] == "no-store"
    assert api.json()["state"]["channels"] == expected
    key, _ = keys.mint(conn, business_id=biz, cls="business", scopes=("read",), label="muse")
    tool = call_tool(client, "get_channel_status", {}, key=key)
    assert data(tool)["channels"] == expected

    view = subprocess.run(
        ["node", "-e", HARNESS, str(CHANNELS_VIEW), json.dumps({"structuredContent": tool["structuredContent"]})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert view.returncode == 0, view.stderr
    lines = view.stdout.splitlines()
    for row in expected:
        assert row["label"] in lines and row["state_text"] in lines, (row, lines)
    assert lines.index(expected[0]["label"]) < lines.index(expected[1]["label"])

    drawn = _render_channels(home)
    assert [r["lines"][:2] for r in drawn["rows"]] == [[r["label"], r["state_text"]] for r in expected]
    words = " ".join([*(line for r in drawn["rows"] for line in r["lines"]), drawn["footer"], view.stdout])
    assert not re.search(r"campaign|ad group|wallet", words, re.IGNORECASE)


def test_home_csp_allows_only_the_agent_host_as_a_frame_source(client):
    """H36: the one addition Task 7 makes to the homepage's own P8 policy -- the agent host, where /p/{token}
    previews are served -- and nothing else changes (still no script/style/font/connect from elsewhere, no
    ancestor may frame the homepage itself)."""
    csp = client.get("/").headers["content-security-policy"]
    assert "frame-src http://testserver" in csp  # AGENT_PUBLIC_BASE_URL, conftest.py
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_home_csp_frame_src_follows_a_split_public_base(client, monkeypatch):
    monkeypatch.setenv("AGENT_PUBLIC_BASE_URL", "https://agent.admiry.ai")
    csp = client.get("/").headers["content-security-policy"]
    assert "frame-src https://agent.admiry.ai" in csp


def test_the_page_card_template_ships_the_iframe_and_its_three_actions(client):
    """Task 7's t-page template: an iframe with no sandbox but referrerpolicy=no-referrer (H36), Publish,
    Change and Add photos, and a status line for after publishing -- plus the plan card's own button that
    builds it, before the Ready to launch section."""
    html = client.get("/").text
    plan_block = html.split('id="t-plan"')[1].split("</template>")[0]
    # the founder's words (2026-10-07): the button says what it makes and, under it, what it costs and does; the
    # hosting minimum stays beside it, word for word (hosting_minimum.MINIMUM, spec §3.2)
    build = plan_block.split('data-part="page-build"')[1].split("</button>")[0]
    assert "<span>Build your business website</span>" in build
    assert (
        "<small>Free to build and host. Improves your visibility and turns more visitors into customers.</small>"
        in build
    )
    assert hosting_minimum.MINIMUM in plan_block.split('data-part="page-intro"')[1].split("</p>")[0]
    assert "Build a page for these ads" not in html
    assert 'data-part="page"' in plan_block and 'data-part="page-build"' in plan_block
    assert plan_block.index('data-part="page"') < plan_block.index('class="card launch"')  # before Ready to launch
    assert 'id="t-page"' in html
    page_block = html.split('id="t-page"')[1].split("</template>")[0]
    assert "<iframe" in page_block and 'referrerpolicy="no-referrer"' in page_block and 'loading="lazy"' in page_block
    assert "sandbox" not in page_block
    assert 'data-part="publish">Publish this page<' in page_block
    assert 'data-part="change"' in page_block and 'data-part="change-input"' in page_block
    assert 'data-part="photos"' in page_block and 'target="_blank"' in page_block and 'rel="noopener"' in page_block
    assert 'data-part="status"' in page_block


def test_the_plan_template_ships_the_website_card(client):
    """A3p-5 batch 2 Task 2 (Q6): the "Your website" card ships in the plan, before "Your page": the row's sentence, a
    row for the four ways, the steps view (the steps, the code in a read-only box with Copy, the platform's help page,
    Check now) and a line for Check now's answer. Task 3: two fields, each in a form shown when its way is chosen -- the
    web person's e-mail address (Q7), and an optional address for Admiry's team with the button that asks it (Q8,
    ruling R3-c); no field asks for a password (W12)."""
    html = client.get("/").text
    plan_block = html.split('id="t-plan"')[1].split("</template>")[0]
    card = plan_block.split('data-part="website"')[1].split("</section>")[0]
    assert plan_block.index('data-part="website"') < plan_block.index('data-part="page"')
    assert '<p class="k">Your website</p>' in card
    for part in (
        "website-host",
        "website-platform",
        "website-say",
        "website-verdict",
        "website-verdict-line",
        "website-bar",
        "website-legend",
        "website-groups",
        "website-ways",
        "website-unchecked",
        "website-first",
        "website-send",
        "website-to",
        "website-team",
        "website-reach",
        "website-ask",
        "website-steps",
        "website-how",
        "website-needs",
        "website-list",
        "website-one",
        "website-code",
        "website-copy",
        "website-help",
        "website-check",
        "website-foot",
        "website-checked",
        "website-again",
        "website-answer",
    ):
        assert f'data-part="{part}"' in card, part
    assert 'data-part="website-steps" hidden' in card  # the steps open from "I'll add it"
    # spec 2026-10-12 §3.5: the list, "not checked yet" and the foot are hidden until the page's report is read
    for part in ("website-platform", "website-verdict", "website-groups", "website-unchecked", "website-foot"):
        assert f'data-part="{part}" hidden' in card, part
    assert "Admiry hasn't checked this page yet." in unescape(card) and ">Check again</button>" in card
    order = [card.index(f'data-part="{p}"') for p in ("website-say", "website-groups", "website-ways", "website-foot")]
    assert order == sorted(order)  # the sentence, the list, the ways (no report yet), then the foot
    # the published page cards list the same checks under their status line, by the same part names (drawChecks)
    for name in ("t-page", "t-site"):
        tpl = html.split(f'<template id="{name}">')[1].split("</template>")[0]
        at = [tpl.index(f'data-part="{p}"') for p in ("status", "website-verdict", "website-legend", "website-groups")]
        assert at == sorted(at), name
    assert "<textarea" in card and "readonly" in card and ">Check now</button>" in card and ">Copy</span>" in card
    assert 'data-part="website-send" hidden' in card  # the address field opens from "Send it to my web person"
    assert 'data-part="website-team" hidden' in card  # its field and button open from "Have Admiry's team do it"
    to, reach = re.findall(r"<input[^>]*>", card)
    assert 'type="email"' in to and 'data-part="website-to"' in to and 'autocomplete="off"' in to
    assert 'type="email"' in reach and 'data-part="website-reach"' in reach and "(optional)" in reach
    assert "Your web person's e-mail address" in card and ">Ask Admiry's team</button>" in card
    assert "password" not in card.lower()
    assert not JARGON.search(re.sub(r"<[^>]+>", " ", card))


def test_the_website_endpoint_carries_the_pages_judged_report(client, conn, fake, google):  # noqa: F811 - fixture
    """Spec 2026-10-12 §3.5: /api/page/website answers the page on file's judged report beside the row and the steps --
    null before any check; then every rule with its title and why, Message-match judged against the plan of now (on
    the owner's own page a failing match is advice, `ok` None, however the report was saved), and the time of the
    check, which the saved report does not carry, from the page row."""
    from agent import landing, site_install
    from agent.tests.test_home_api import IP
    from agent.tests.test_home_api import _planned as planned

    csrf = planned(client, conn, google)
    before = client.post("/api/page/website", json={"csrf": csrf}, headers=IP).json()
    assert before["ok"] and before["check"] is None and before["install"]
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/invisalign")
    rules = [{**c, "ok": c["id"] != "message_match", "detail": "d.", "fix": None} for c in landing.CHECKLIST]
    report = {"ok": False, "url": page["url"], "headline": "Fresh bread every morning", "rules": rules}
    landing.save_check(conn, page["id"], report)
    r = client.post("/api/page/website", json={"csrf": csrf}, headers=IP).json()
    assert r["ok"] and r["check"]["rules"][0]["title"] == "Opens securely and fast" and "why" in r["check"]["rules"][0]
    assert [x["id"] for x in r["check"]["rules"]] == [c["id"] for c in landing.CHECKLIST]
    judged = next(x for x in r["check"]["rules"] if x["id"] == "message_match")
    assert judged["ok"] is None and judged["fix"] and r["check"]["ok"] is True  # advice on the owner's own page (R4)
    assert r["check"]["checked_at"] == landing.current(conn, biz)["checked_at"].isoformat()
    assert r["website"] == site_install.row(conn, biz) and r["website"]["url"] == page["url"]


# home.js's list of a page's 14 checks (spec 2026-10-12 §3.5) read out of the script as the launch card is: VERDICTS and
# BLOCKING (`stmt`, a statement over the lines it takes) and drawChecks with what it calls, over LAUNCH_TREE's Node with
# what the list touches besides -- attributes, replaceChildren, a style, and each element's click kept so a case can
# press it. `listParts()` are the list's parts as home.html ships them; `listed(host)` reads back what was drawn in
# them: the verdict, the legend, the bar, each group's eyebrow with its rows' ids, each row (its mark, the mark's label
# and its lines; a passing row's detail after a "·", as the page shows it) and the buttons on the rows -- nulls while
# the list is hidden.
CHECKS_TREE = (
    LAUNCH_TREE
    + r"""
const stmt = (start) => {
  const from = lines.findIndex((l) => l.startsWith(start));
  if (from < 0) throw new Error(`home.js has no statement starting "${start}"`);
  let to = from;
  while (!/;\s*(\/\/.*)?$/.test(lines[to])) to += 1;
  return lines.slice(from, to + 1).join("\n");
};
const clicks = new Map();
Object.assign(Node.prototype, {
  addEventListener(type, fn) { if (type === "click") clicks.set(this, fn); },
  setAttribute(k, v) { (this.attrs ||= {})[k] = String(v); },
  replaceChildren(...kids) { [...this.kids].forEach((k) => k.remove()); this.add(...kids); },
});
const el = (tag, cls, text) => Object.assign(new Node({}, cls || "", text == null ? "" : text, tag.toUpperCase()), { style: {} });
const doc = { createTextNode: (t) => new Node({}, "", t, "#text") };
const CHECKS = [stmt("  const VERDICTS = "), stmt("  const BLOCKING = "),
  ...["drawChecks", "groupChecks", "checkRowOf", "fixRow", "fixButton"].map(grab)];
const P = (name, cls, tag = "P", hidden = false) => Object.assign(new Node({ part: name }, cls, "", tag), hidden ? { hidden } : {});
const listParts = () => [
  P("website-verdict", "checks-head", "DIV", true).add(P("website-verdict-line", "verdict"), P("website-bar", "bar", "DIV"),
    P("website-legend", "legend")),
  P("website-groups", "check-groups", "DIV", true)];
const words = (n) => n.text + n.kids.map(words).join("");
const listed = (host) => {
  const at = (name) => host.querySelector(`[data-part="${name}"]`), groups = at("website-groups");
  if (at("website-verdict").hidden || groups.hidden) return { verdict: null, legend: null, bar: null, groups: null, rows: null, buttons: null };
  const lines = (body) => body.kids.filter((k) => !k.cls.has("fix")).map((k) => [k.text, ...k.kids.map((s) => s.text)].join(" · "));
  return { verdict: at("website-verdict-line").text, legend: at("website-legend").kids.map(words),
    bar: at("website-bar").kids.map((s) => [[...s.cls].join(" "), s.style.width]),
    groups: groups.kids.map((g) => [words(g.kids[0]).replace(/^✓\s*/, ""), g.kids[1].kids.map((li) => li.dataset.id)]),
    rows: Object.fromEntries(groups.querySelectorAll("[data-id]").map((li) =>
      [li.dataset.id, [li.kids[0].text, li.kids[0].attrs["aria-label"], ...lines(li.kids[1])]])),
    buttons: groups.querySelectorAll(".btn").map((b) => b.text), primary: groups.querySelectorAll(".dark").map((b) => b.text) };
};
"""
)

# home.js's website card read out of the script the same way, over a section built as home.html ships it (A3p-5 batch 2
# Task 2; its line, the list and the foot, spec 2026-10-12 §3.5): `post` answers /api/page/website with the case's own
# answer (`answers`, beside `rows`; none: refused), and showSteps, showWay and chooseWay are recorders. A row with a
# Check now answer (`checks`) has Check now pressed at once, its card's own load answering after it. For each row,
# what is left -- the sentence, the ways' buttons (way id and label) while their row shows, whether the steps view is
# open, the page's host and platform tag, the list (`listed`), whether "not checked yet" shows, the foot's words, the
# answer line, whether the report was asked for and what the buttons on the rows did when pressed -- or null once the
# card is gone.
WEBSITE_HARNESS = (
    CHECKS_TREE
    + r"""
const did = [];
let answer = null, checked = null;
const ctx = vm.createContext({ el, doc, URL, S: {}, csrf: async () => "", work: (f) => f(), copyFrom() {}, announce() {},
  handle() {}, showSteps: () => did.push("steps"), showWay: (card, name) => did.push(name), chooseWay: (n, id) => did.push(id),
  post: async (path) => {
    did.push(path);
    if (path === "/api/page/check") return checked;
    if (checked) await new Promise((done) => setTimeout(done, 20)); // the card's own load, slower than Check now
    return answer || { ok: false, error: "Sign in first." };
  } });
const names = ["websiteCard", "wayForm", "siteLine", "loadWebsite", "showSiteCheck", "hostOf", "when", "checkNow", "said", "settle"];
const { websiteCard, checkNow } = vm.runInContext(
  [line("  const part = "), ...CHECKS, ...names.map(grab), "({ websiteCard, checkNow });"].join("\n"), ctx);
const section = () => P("website", "card", "SECTION").add(
  new Node({}, "site-line", "", "DIV").add(P("website-host", "site-host", "SPAN"), P("website-platform", "tag", "SPAN", true)),
  P("website-say", "msg"), ...listParts(), P("website-ways", "page-actions", "DIV"),
  P("website-unchecked", "checks-foot", "DIV", true).add(P("website-first", "btn small", "BUTTON")),
  P("website-send", "page-change", "FORM", true).add(P("website-to", "", "INPUT")),
  P("website-team", "page-change", "FORM", true).add(P("website-reach", "", "INPUT")),
  P("website-steps", "page-built", "DIV", true).add(P("website-copy", "btn", "BUTTON"), P("website-check", "btn dark", "BUTTON")),
  P("website-foot", "checks-foot", "DIV", true).add(P("website-checked", "note"), P("website-again", "btn small", "BUTTON")),
  P("website-answer", "note", "P", true));
(async () => {
  const out = [];
  for (const [i, row] of input.rows.entries()) {
    did.length = 0;
    answer = (input.answers || [])[i] || null;
    checked = (input.checks || [])[i] || null;
    const plan = new Node({}, "plan").add(section());
    new Node({}, "log").add(plan); // on the page: the report's answer is drawn only on a card still there
    websiteCard(plan, row);
    if (checked) await checkNow(plan);
    await new Promise((done) => setTimeout(done, checked ? 40 : 5));
    const card = plan.querySelector('[data-part="website"]');
    if (!card) { out.push(null); continue; }
    const at = (name) => card.querySelector(`[data-part="${name}"]`), ways = at("website-ways");
    const asked = did.includes("/api/page/website"), list = listed(card);
    did.length = 0;
    for (const b of at("website-groups").querySelectorAll(".btn")) clicks.get(b)();
    out.push({ say: at("website-say").text, ways: ways.hidden ? null : ways.kids.map((b) => [b.dataset.way, b.text]),
      steps: !at("website-steps").hidden, host: at("website-host").text,
      platform: at("website-platform").hidden ? null : at("website-platform").text, ...list,
      unchecked: !at("website-unchecked").hidden, foot: at("website-foot").hidden ? null : at("website-checked").text,
      said: at("website-answer").hidden ? null : at("website-answer").text, asked, pressed: [...did] });
  }
  console.log(JSON.stringify(out));
})();
"""
)


def _drawn(seen: dict) -> dict:
    """The card's sentence, ways and steps view, as the four ways' test reads them."""
    return {k: seen[k] for k in ("say", "ways", "steps")}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_website_card_draws_the_rows_sentence_and_the_four_ways_in_the_servers_order(conn):
    """Q5, Q6: the card shows the row's sentence in each state; only a site that needs the code gets its ways, in the
    order the row gives them (the Admiry page first for GoDaddy, Q3), with the steps view closed until "I'll add it"; no
    website of its own, no card (the page card stands for it). Ruling R3-f: support off, as here, the row has no
    "Have Admiry's team do it" and neither has the card; the four, as the server gives them with support on, are drawn
    all. The page's report is asked for on every card but an Admiry page's (spec 2026-10-12 §3.5); refused, as here
    (a visitor not signed in), the card stands as the row draws it."""
    from agent import site_install

    needs = {"state": "needs_code", "say": site_install.NEEDS_CODE, "platform": "other", "steps": ["s"]}
    rows = [
        {**needs, "ways": site_install.ways_for("other")},
        {**needs, "platform": "godaddy", "ways": site_install.ways_for("godaddy")},
        {"state": "found", "say": site_install.FOUND},
        {"state": "admiry_page", "say": site_install.ADMIRY_PAGE},
        None,
        {**needs, "ways": site_install.WAYS},
    ]
    seen = _node(WEBSITE_HARNESS, {"rows": rows})

    def pairs(ways: list[dict]) -> list[list[str]]:
        return [[w["id"], w["label"]] for w in ways]

    assert _drawn(seen[0]) == {"say": site_install.NEEDS_CODE, "ways": pairs(rows[0]["ways"]), "steps": False}
    assert [way for way, _label in seen[0]["ways"]] == ["install_self", "install_send", "admiry_page"]
    assert [way for way, _label in seen[1]["ways"]] == ["admiry_page", "install_self", "install_send"]
    assert _drawn(seen[2]) == {"say": site_install.FOUND, "ways": None, "steps": False}
    assert _drawn(seen[3]) == {"say": site_install.ADMIRY_PAGE, "ways": None, "steps": False}
    assert seen[4] is None
    assert seen[5]["ways"] == pairs(site_install.WAYS)
    assert [s["asked"] for s in seen[:4]] == [True, True, True, False]
    assert all(s["verdict"] is None and s["foot"] is None and not s["unchecked"] for s in seen[:4] + seen[5:])


def _rules(*spec: tuple) -> list[dict]:
    """A check's rules as landing.check writes them -- id, title, why, ok, detail, fix -- for the card's list."""
    return [{"id": i, "title": t, "why": "w.", "ok": ok, "detail": "d.", "fix": f} for i, t, ok, f in spec]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_website_card_lists_the_checks_grouped_with_the_verdict_and_the_tracking_rows_buttons():
    """Spec 2026-10-12 §3.5: with the page's report the card names the page the ads use and its builder, says the
    verdict, draws the bar and the legend by count, then the rules in three groups -- what stops the ads (a failed
    BLOCKING rule), what is worth doing (a warning, or a failed rule that blocks nothing), and the passing ones folded,
    each one line. While the code is missing the tracking row carries the ways as its own buttons -- the steps, the web
    person's field, the Admiry page -- and the ways row is not drawn; the foot says when it was checked. Without a
    report yet: the ways, "not checked yet" and its Check now."""
    from agent import site_install

    rules = _rules(
        ("reachable", "Opens securely and fast", True, None),
        ("tracking", "Admiry's code is on the page", False, "Paste it."),
        ("message_match", "Headline matches the ad", None, "Say the service."),
        ("one_goal", "Keeps visitors on the page", False, "Fewer links."),  # fails, and blocks nothing
        ("mobile", "Works on a phone", True, None),
    )
    row = {
        "state": "needs_code",
        "say": site_install.NEEDS_CODE,
        "platform": "wordpress",
        "platform_name": "WordPress",
        "url": "https://www.marinabayroofing.com/lp/roof-repair",
        "steps": ["s"],
        "ways": site_install.ways_for("wordpress"),
    }
    checked_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    check = {"ok": False, "rules": rules, "checked_at": checked_at}
    answers = [{"ok": True, "website": row, "install": None, "check": c} for c in (check, None)]
    seen, fresh = _node(WEBSITE_HARNESS, {"rows": [row, row], "answers": answers})
    assert seen["asked"] and seen["host"] == "marinabayroofing.com/lp/roof-repair" and seen["platform"] == "WordPress"
    assert seen["verdict"] == "One thing before the ads can run."
    assert seen["legend"] == ["1 before the ads can run", "2 worth doing", "2 pass"]
    assert seen["bar"] == [["stop", "20.00%"], ["warn", "40.00%"], ["pass", "40.00%"]]
    assert seen["groups"] == [
        ["Before the ads can run", ["tracking"]],
        ["Worth doing", ["message_match", "one_goal"]],
        ["2 checks pass", ["reachable", "mobile"]],
    ]
    assert seen["rows"]["tracking"] == ["✕", "Needs fixing", "Admiry's code is on the page", "d. Paste it.", "w."]
    assert seen["rows"]["message_match"] == ["!", "Worth doing", "Headline matches the ad", "d. Say the service.", "w."]
    assert seen["rows"]["reachable"] == ["✓", "Passes", "Opens securely and fast · d."]  # one line
    assert seen["buttons"] == ["Add the code", "Send it to my web person", "Use an Admiry page instead"]
    assert seen["primary"] == ["Add the code"]  # the row's first way
    assert seen["pressed"] == ["steps", "website-send", "admiry_page"]  # the steps, the field, the Admiry page
    assert seen["ways"] is None and seen["unchecked"] is False  # the ways moved onto the row
    assert seen["foot"] == "Checked 5 minutes ago"
    assert fresh["verdict"] is None and fresh["groups"] is None and fresh["foot"] is None
    assert fresh["ways"] == [[w["id"], w["label"]] for w in row["ways"]] and fresh["unchecked"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_website_cards_verdict_follows_what_blocks_and_what_warns():
    """The verdict's four sentences (spec 2026-10-12 §3.5): nothing failed or warned, warnings alone, one blocking
    failure, n. The code found: no buttons on any row. A report without its time is Check now's own: just made."""
    from agent import site_install

    found = {"state": "found", "say": site_install.FOUND, "url": "https://acme.example/", "platform_name": None}
    needs = {**found, "state": "needs_code", "say": site_install.NEEDS_CODE, "platform": "other", "steps": ["s"]}
    needs["ways"] = site_install.ways_for("other")
    passing = ("reachable", "Opens securely and fast", True, None)
    reports = [
        _rules(passing, ("tracking", "Admiry's code is on the page", True, None)),
        _rules(passing, ("speed", "Loads quickly", False, "Lighter."), ("tracking", "t", True, None)),
        _rules(("reachable", "r", False, "Fix."), ("phone", "p", False, "Call."), ("privacy", "v", None, "Link.")),
    ]
    rows = [found, found, needs]
    answers = [
        {"ok": True, "website": w, "check": {"ok": True, "rules": r}} for w, r in zip(rows, reports, strict=True)
    ]
    ok, warned, two = _node(WEBSITE_HARNESS, {"rows": rows, "answers": answers})
    assert ok["verdict"] == "Your ads can run here." and ok["groups"] == [["2 checks pass", ["reachable", "tracking"]]]
    assert ok["host"] == "acme.example" and ok["platform"] is None and ok["foot"] == "Checked just now"
    assert warned["verdict"] == "Your ads can run here. A few things are worth doing."
    assert warned["groups"] == [["Worth doing", ["speed"]], ["2 checks pass", ["reachable", "tracking"]]]
    assert ok["buttons"] == warned["buttons"] == [] and ok["ways"] is None and not ok["unchecked"]
    assert two["verdict"] == "2 things before the ads can run." and two["legend"] == [
        "2 before the ads can run",
        "1 worth doing",
    ]
    assert two["groups"] == [["Before the ads can run", ["reachable", "phone"]], ["Worth doing", ["privacy"]]]
    assert two["buttons"] == [] and two["ways"] is None  # only the tracking row carries the ways


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_tracking_rows_buttons_are_the_rows_own_ways_in_its_order():
    """The tracking row carries the row's own ways, in the server's order (Task 7 fix round): all four with support on
    -- "Have Admiry's team do it" opening its contact field and button, as the ways row's does -- and the Admiry page
    first where the builder keeps added code in a frame (GoDaddy, Q3); the first is the primary one."""
    from agent import site_install

    tracking = _rules(("tracking", "Admiry's code is on the page", False, None))
    row = {
        "state": "needs_code",
        "say": site_install.NEEDS_CODE,
        "platform": "other",
        "url": "acme.example",
        "steps": [],
    }
    rows = [
        {**row, "ways": site_install.WAYS},
        {**row, "platform": "godaddy", "ways": site_install.ways_for("godaddy")},
    ]
    answers = [{"ok": True, "website": w, "check": {"ok": False, "rules": tracking}} for w in rows]
    four, godaddy = _node(WEBSITE_HARNESS, {"rows": rows, "answers": answers})
    assert four["buttons"] == [
        "Add the code",
        "Send it to my web person",
        "Have Admiry's team do it",
        "Use an Admiry page instead",
    ]
    assert four["pressed"] == ["steps", "website-send", "website-team", "admiry_page"]
    assert godaddy["buttons"] == ["Use an Admiry page instead", "Add the code", "Send it to my web person"]
    assert godaddy["primary"] == ["Use an Admiry page instead"] and four["primary"] == ["Add the code"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_check_now_answer_is_never_drawn_over_by_the_cards_older_load():
    """Check now pressed before the card's own load answered (Task 7 fix round): its answer -- the code found, the
    page passing -- stands; the load's older report, answering later, is not drawn over it."""
    from agent import site_install

    needs = {"state": "needs_code", "say": site_install.NEEDS_CODE, "platform": "other", "url": "acme.example"}
    needs.update(steps=[], ways=site_install.ways_for("other"))
    found = {"state": "found", "say": site_install.FOUND, "url": "acme.example", "platform_name": None}
    older = {"ok": False, "rules": _rules(("tracking", "Admiry's code is on the page", False, None))}
    newer = {"ok": True, "rules": _rules(("tracking", "Admiry's code is on the page", True, None))}
    load = {"ok": True, "website": needs, "check": older}
    now = {"ok": True, "say": site_install.FOUND_IT, "website": found, "check": newer}
    seen = _node(WEBSITE_HARNESS, {"rows": [needs], "answers": [load], "checks": [now]})[0]
    assert seen["verdict"] == "Your ads can run here." and seen["groups"] == [["1 check passes", ["tracking"]]]
    assert seen["said"] == site_install.FOUND_IT and seen["buttons"] == [] and seen["ways"] is None
    assert seen["foot"] == "Checked just now" and seen["asked"]  # the load was asked, and answered too late


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_found_row_with_no_report_yet_offers_check_now():
    """Final review minor 7: the code found on a page with no report saved yet -- "Admiry hasn't checked this page yet."
    and its Check now, as a page that needs the code has; no ways (the code is there), no list and no foot."""
    from agent import site_install

    found = {"state": "found", "say": site_install.FOUND, "url": "https://acme.example/", "platform_name": None}
    seen = _node(WEBSITE_HARNESS, {"rows": [found], "answers": [{"ok": True, "website": found, "check": None}]})[0]
    assert seen["asked"] and seen["unchecked"] is True
    assert seen["ways"] is None and seen["verdict"] is None and seen["groups"] is None and seen["foot"] is None


# hostOf and when read out of the script alone: the card's line and the foot's time
LINE_HARNESS = (
    LAUNCH_TREE
    + r"""
const { hostOf, when } = vm.runInContext([grab("hostOf"), grab("when"), "({ hostOf, when });"].join("\n"), vm.createContext({ URL }));
console.log(JSON.stringify({ hosts: input.urls.map(hostOf), times: input.times.map(when) }));
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_website_cards_host_and_time_read_as_an_owner_says_them():
    """The page the ads use as its host without "www." and its path past "/" (an address without a scheme too); a
    root path keeps its own query string (WordPress's plain permalinks, "/?page_id=42" is not the bare host), while
    a non-root path's query is still dropped as before (polish item 3, Task 8); the time in words: just now under a
    minute, minutes under an hour, hours under a day, then the date."""
    now = datetime.now(UTC)

    def ago(**kw) -> str:
        return (now - timedelta(**kw)).isoformat()

    urls = [
        "https://www.marinabayroofing.com/lp/roof-repair",
        "acme.example",
        "https://acme.example/",
        "http://a.example:8443/x?y=1#z",
        "https://www.example.com/?page_id=42",
    ]
    times = [
        None,
        ago(seconds=10),
        ago(seconds=65),
        ago(minutes=42),
        ago(minutes=61),
        ago(hours=5),
        "2026-01-02T09:00:00.123456+00:00",
    ]
    seen = _node(LINE_HARNESS, {"urls": urls, "times": times})
    assert seen["hosts"] == [
        "marinabayroofing.com/lp/roof-repair",
        "acme.example",
        "acme.example",
        "a.example:8443/x",
        "example.com/?page_id=42",
    ]
    assert seen["times"][:6] == ["just now", "just now", "1 minute ago", "42 minutes ago", "1 hour ago", "5 hours ago"]
    assert "2026" in seen["times"][6] and "ago" not in seen["times"][6]


def test_the_website_cards_blocking_rules_are_landings():
    """What stops the ads on the card is what stops them on the server (landing.BLOCKING), in its order."""
    from agent import landing

    line = next(x for x in HOME_JS.read_text().splitlines() if x.startswith("  const BLOCKING = "))
    assert tuple(re.findall(r'"(\w+)"', line.split("//")[0])) == landing.BLOCKING


# home.js's starter page card (fillPage, and revisePage's new draft) read out of the script with what it calls, over the
# t-page parts they touch with the list's parts under the status line: what is listed after each page is filled, then
# after a change makes a new draft of the first.
PAGE_LIST_HARNESS = (
    CHECKS_TREE
    + r"""
const ctx = vm.createContext({ el, doc, safeHref: (u) => u, work() {}, csrf: async () => "", handle() {}, announce() {},
  post: async () => ({ ok: true, token: "t2", preview_url: "/p2", upload_url: null }) });
const names = ["fillPage", "setPublished", "setHosting", "setMuse", "revisePage"];
const { fillPage, revisePage } = vm.runInContext(
  [line("  const part = "), ...CHECKS, ...names.map(grab), "({ fillPage, revisePage });"].join("\n"), ctx);
const card = () => new Node({}, "page-built", "", "DIV").add(P("frame", "page-frame", "IFRAME"), P("status", "note", "P", true),
  ...listParts(), P("hosting", "note", "P", true), P("publish", "btn dark", "BUTTON"), P("photos", "ghost", "A"),
  P("download", "ghost", "A", true), P("muse", "page-muse", "DIV", true).add(P("muse-text", "msg soft")),
  P("change", "page-change", "FORM").add(P("change-input", "", "INPUT")));
(async () => {
  const out = input.pages.map((p) => { const n = card(); fillPage(n, p); return { status: n.querySelector('[data-part="status"]').text, ...listed(n) }; });
  const n = card();
  fillPage(n, input.pages[0]);
  await revisePage(n, "Make it blue");
  out.push(listed(n));
  console.log(JSON.stringify(out));
})();
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_published_starter_page_lists_its_checks_under_its_line():
    """Spec 2026-10-12 §3.5: the published Admiry page's status line has the same list under it, drawn by the same
    renderer, with no buttons (Admiry's own page carries the code); a page not published, and a new draft, none."""
    rules = _rules(
        ("reachable", "Opens securely and fast", True, None), ("terms", "Terms link in the footer", False, "Add it.")
    )
    page = {
        "token": "t1",
        "preview_url": "/p1",
        "upload_url": None,
        "status": "published",
        "url": "https://x.business.test/",
    }
    pages = [{**page, "check": {"ok": True, "rules": rules}}, {**page, "status": "preview", "check": None}]
    published, draft, revised = _node(PAGE_LIST_HARNESS, {"pages": pages})
    assert published["status"] == "Published at https://x.business.test/ (1 of 2 checks pass)"
    assert published["verdict"] == "Your ads can run here. A few things are worth doing."
    assert published["groups"] == [["Worth doing", ["terms"]], ["1 check passes", ["reachable"]]]
    assert published["buttons"] == []
    assert draft["verdict"] is None and revised["verdict"] is None and revised["groups"] is None


# home.js's reading card (R6, spec 2026-10-12 §3.3), read out of the script with its own `rows`, over a stub of the
# t-reading template (two parts: `facts`, a dl, and `pages`, a p hidden until there is something to say). Prints the
# facts rows (empty parts dropped, `rows`'s own rule) drawn into `facts`, and the pages line, or null when hidden.
READING_HARNESS = (
    LAUNCH_TREE
    + r"""
const el = (tag, cls, text) => new Node({}, cls || "", text == null ? "" : text, tag.toUpperCase());
const clone = () => new Node({}, "card reading", "", "SECTION").add(
  new Node({ part: "facts" }, "grid", "", "DL"),
  Object.assign(new Node({ part: "pages" }, "fine", "", "P"), { hidden: true }));
const added = [];
const add = (n) => (added.push(n), n);
const readingCard = vm.runInContext(
  [line("  const part = "), grab("rows"), grab("readingCard"), "readingCard;"].join("\n"),
  vm.createContext({ el, clone, add, S: {} }));
readingCard(input.view);
const n = added[0];
console.log(JSON.stringify({
  rows: n.querySelector('[data-part="facts"]').kids.map((row) => row.kids.map((c) => c.textContent)),
  pages: n.querySelector('[data-part="pages"]').hidden ? null : n.querySelector('[data-part="pages"]').textContent,
}));
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_reading_card_draws_the_parts_present_and_the_pages_line(client):
    """R6 (spec 2026-10-12 §3.3): the facts card draws only the parts a site's reading gave -- Area here is null, and
    `rows` drops it -- and, with pages read, the one line under them. "Built on" shows `platform` as given: the
    display name is the server's own (consult.reading_view, site_install's steps_for), nothing for the card to map.
    The template itself ships on the page too."""
    view = {
        "host": "acme.example",
        "name": "Acme",
        "services": ["Roofing"],
        "area": None,
        "phone": "(628) 233-8140",
        "platform": "WordPress",
        "pages": 9,
    }
    seen = _node(READING_HARNESS, {"view": view})
    assert seen["rows"] == [
        ["Business", "Acme"],
        ["Offers", "Roofing"],
        ["Phone", "(628) 233-8140"],
        ["Built on", "WordPress"],
    ]
    assert seen["pages"] == "Admiry read 9 pages."
    assert 'id="t-reading"' in client.get("/").text


# home.js's website card with the two ways Task 3 wires (A3p-5 batch 2, ruling R0-f), read out of the script with
# chooseWay, openWay and what they call, over a section built as home.html ships it, `post` answering each endpoint from
# the input. "Send it to my web person" is chosen on the card and sent with the address typed; then the plan e-mail's
# link for "Have Admiry's team do it" opens that way (openWay, as /?website=install_help does once the plan is drawn),
# and only the owner's tap on its button asks (ruling R3-c). Prints the way's form after each step -- open, what its
# field holds, the answer line -- and what was seen: the calls, the focus, what was shown and said.
TWO_WAYS_HARNESS = (
    LAUNCH_TREE
    + r"""
const seen = [], submits = {};
let pending = null;
Node.prototype.focus = function () { seen.push("focus " + (this.dataset.way || this.dataset.part)); };
const el = (tag, cls, text) => new Node({}, cls || "", text == null ? "" : text, tag.toUpperCase());
const form = (name, field, button) => {
  const f = new Node({ part: name }, "page-change", "", "FORM").add(
    Object.assign(new Node({ part: field }, "", "", "INPUT"), { value: "" }));
  if (button) f.add(new Node({ part: button }, "btn", "", "BUTTON"));
  f.addEventListener = (type, fn) => { if (type === "submit") submits[name] = fn; };
  return Object.assign(f, { hidden: true });
};
const send = form("website-send", "website-to"), team = form("website-team", "website-reach", "website-ask");
const answer = Object.assign(new Node({ part: "website-answer" }, "note"), { hidden: true });
const steps = Object.assign(new Node({ part: "website-steps" }, "page-built", "", "DIV").add(
  new Node({ part: "website-copy" }, "btn", "", "BUTTON"), new Node({ part: "website-check" }, "btn dark", "", "BUTTON")),
  { hidden: true });
const small = (name) => new Node({ part: name }, "btn small", "", "BUTTON"); // "not checked yet"'s Check now, Check again
const card = new Node({ part: "website" }, "card", "", "SECTION").add(new Node({ part: "website-say" }, "msg"),
  new Node({ part: "website-ways" }, "page-actions", "", "DIV"), small("website-first"), send, team, steps,
  small("website-again"), answer);
const plan = new Node({}, "plan").add(card);
const ctx = vm.createContext({ el, S: { planNode: plan }, location: { pathname: "/" },
  history: { replaceState: () => seen.push("cleaned") }, work: (fn) => (pending = fn()), csrf: async () => "t",
  post: async (path, body) => (seen.push(["post", path, body]), input.answers[path]),
  announce: (t) => seen.push("said " + t), reveal: (n) => seen.push("shown " + n.dataset.part),
  handle: () => seen.push("handled"), sentence: (s) => s, copyFrom() {}, checkNow() {}, showSteps() {},
  siteLine() {}, loadWebsite() {} }); // the card's line and its report: the website card's own test
const names = ["websiteCard", "wayForm", "chooseWay", "showWay", "wayAnswer", "said", "openWay"];
const code = [line("  const part = "), ...names.map(grab), "({ websiteCard, chooseWay, openWay });"];
const { websiteCard, chooseWay, openWay } = vm.runInContext(code.join("\n"), ctx);
const now = (f) => ({ open: !f.hidden, value: f.kids[0].value, answer: answer.hidden ? null : answer.text });
const tap = async (name) => {
  pending = null;
  submits[name]({ preventDefault: () => seen.push("prevented") });
  await pending;
};
(async () => {
  websiteCard(plan, input.row);
  chooseWay(plan, "install_send");
  const opened = now(send);
  send.kids[0].value = input.typed;
  await tap("website-send");
  const sent = now(send);
  seen.push("the e-mail's link");
  openWay("install_help");
  await new Promise((done) => setTimeout(done, 50)); // whatever a page load would post has been posted by now
  const linked = now(team);
  team.kids[0].value = input.contact;
  await tap("website-team");
  console.log(JSON.stringify({ opened, sent, linked, helped: now(team), seen }));
})();
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_web_person_and_admirys_team_answer_on_the_card():
    """Ruling R0-f (Q7, Q8): "Send it to my web person" opens the address field and focuses it; a send posts the address
    typed (trimmed) to /api/page/install-send and puts Q7's answer on the card, closing the field. Ruling R3-c: the plan
    e-mail's link for "Have Admiry's team do it" opens that way with its field and its button ready (the button
    focused) and posts nothing -- a link scanner or a prefetch opens no ticket; the owner's tap on the button posts to
    /api/page/install-help, with the address typed in the optional field, and puts Q8's answer on the card. A refusal is
    said on the card and the field keeps the address to fix; a sign-in answer is the page's own (handle)."""
    from agent import site_install

    row = {
        "state": "needs_code",
        "say": site_install.NEEDS_CODE,
        "platform": "wix",
        "steps": ["s"],
        "ways": site_install.WAYS,  # as the server offers them with support on (ruling R3-f)
    }
    sent_say = "Sent to web@agency.example. We'll tell you when the code shows up."
    help_say = "Our team will contact you at office@smile.example soon."
    answers = {
        "/api/page/install-send": {"ok": True, "say": sent_say},
        "/api/page/install-help": {"ok": True, "say": help_say},
    }
    payload = {"row": row, "typed": "  web@agency.example ", "contact": " office@smile.example ", "answers": answers}
    got = _node(TWO_WAYS_HARNESS, payload)
    assert got["opened"] == {"open": True, "value": "", "answer": None}
    assert got["sent"] == {"open": False, "value": "", "answer": sent_say}
    assert got["linked"] == {"open": True, "value": "", "answer": sent_say}  # opened, and nothing asked
    assert got["helped"] == {"open": False, "value": "", "answer": help_say}
    assert got["seen"] == [
        "shown website-send",
        "focus website-to",
        "prevented",
        ["post", "/api/page/install-send", {"csrf": "t", "email": "web@agency.example"}],
        f"said {sent_say}",
        "the e-mail's link",
        "cleaned",
        "shown website",
        "focus install_help",
        "shown website-team",
        "focus website-ask",
        "prevented",
        ["post", "/api/page/install-help", {"csrf": "t", "contact": "office@smile.example"}],
        f"said {help_say}",
    ]
    refusals = {
        "/api/page/install-send": {"ok": False, "error": site_install.OWN_ADDRESS},
        "/api/page/install-help": {"ok": False, "signin": True, "error": "Sign in to continue."},
    }
    payload = {"row": row, "typed": "owner@example.com", "contact": "", "answers": refusals}
    refused = _node(TWO_WAYS_HARNESS, payload)
    assert refused["sent"] == {"open": True, "value": "owner@example.com", "answer": site_install.OWN_ADDRESS}
    assert ["post", "/api/page/install-help", {"csrf": "t"}] in refused["seen"]  # the field left empty: the owner's
    assert refused["helped"]["answer"] == site_install.OWN_ADDRESS and refused["seen"][-1] == "handled"


# home.js's openWay read out of the script, over a plan whose "Your website" card shows the four ways: each link of the
# plan e-mail (/?website=<way id>) is tried in turn, and what it did is printed -- the address cleaned, the card shown,
# the way's button focused and the way chosen.
WAY_LINK_HARNESS = (
    LAUNCH_TREE
    + r"""
const seen = [];
const wayButton = (way) => Object.assign(new Node({ way }, "btn", way, "BUTTON"), { focus: () => seen.push("focus " + way) });
const card = new Node({ part: "website" }, "card", "", "SECTION").add(
  new Node({ part: "website-ways" }, "page-actions", "", "DIV").add(...input.ways.map(wayButton)));
const plan = new Node({}, "plan").add(card);
const ctx = vm.createContext({ S: { planNode: input.planless ? null : plan }, location: { pathname: "/" },
  history: { replaceState: () => seen.push("cleaned") }, reveal: (n) => seen.push("shown " + n.dataset.part),
  chooseWay: (n, id) => seen.push("chose " + id) });
const openWay = vm.runInContext([line("  const part = "), grab("openWay"), "openWay;"].join("\n"), ctx);
console.log(JSON.stringify(input.links.map((id) => { seen.length = 0; openWay(id); return [...seen]; })));
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_link_from_the_plan_email_opens_its_way_on_the_card():
    """Rulings R2-c, R2-g: the plan e-mail's four links (/?website=<way id>) open the homepage's card at that way, once
    the plan is drawn; the address loses the parameter once the card is found, and a way the card does not offer (or
    any other text) opens nothing. With no card on the page yet (the sign-in wall, a fresh start) the address keeps the
    way."""
    from agent import site_install

    ids = [w["id"] for w in site_install.WAYS]
    seen = _node(WAY_LINK_HARNESS, {"ways": ids, "links": [*ids, "nonsense", '"]x']})
    for way, did in zip(ids, seen, strict=False):
        assert did == ["cleaned", "shown website", f"focus {way}", f"chose {way}"]
    assert seen[-2:] == [["cleaned"], ["cleaned"]]
    assert _node(WAY_LINK_HARNESS, {"ways": ids, "links": ids[:1], "planless": True}) == [[]]


# home.js whole under Node, with a stub DOM like HOME_HARNESS's (a `[data-way]` lookup walks the elements below), the
# page's own address and sign-in, and `fetch` answering each endpoint: the state given, a csrf token, the card's steps.
# Prints where the page went (location.assign), every address it wrote (history.replaceState), the endpoints it called,
# whether the website card's steps view opened, and the wall's sign-in link.
LINK_FLOW_HARNESS = r"""
const [channels, src] = [process.argv[1], process.argv[2]].map((f) => require("fs").readFileSync(f, "utf8"));
const input = JSON.parse(process.argv[3]);
const node = (tag) => {
  const n = { tag, className: "", text: "", kids: [], parts: {}, dataset: {}, hidden: false, style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
    append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
    replaceChildren(...cs) { this.kids = cs; }, get children() { return this.kids; },
    querySelector(sel) { const m = /data-part="([^"]+)"/.exec(sel), k = m ? m[1] : sel;
      return (this.parts[k] ||= node("part:" + k)); },
    querySelectorAll(sel) {
      if (sel !== "[data-way]") return [];
      const out = [];
      const walk = (x) => [...(x.kids || []), ...Object.values(x.parts || {})].forEach((k) => {
        if (k.dataset && k.dataset.way) out.push(k);
        walk(k);
      });
      walk(this);
      return out;
    },
    closest() { return node("closest"); }, get parentElement() { return node("up"); },
    get content() { return { firstElementChild: node("template") }; }, cloneNode() { return node(tag); },
    remove() {}, replaceWith() {}, addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {},
    scrollIntoView() {}, isConnected: true };
  return n;
};
const byId = {}, went = [], wrote = [], called = [];
const doc = { body: node("body"), activeElement: null, createElement: node, createTextNode: (t) => ({ text: t, kids: [] }),
  getElementById: (id) => (byId[id] ||= node("#" + id)), querySelectorAll: () => [], addEventListener() {} };
doc.body.dataset.signedIn = input.signedIn ? "1" : "0";
byId.prompts = node("#prompts");
byId.prompts.textContent = "{}";
const answers = { "/api/turn": input.state, "/api/csrf": { csrf: "t" }, "/api/page/website": input.website };
Object.assign(global, { document: doc, matchMedia: () => ({ matches: true }),
  location: { search: input.search, pathname: "/", assign: (url) => went.push(url) },
  history: { replaceState: (s, t, url) => wrote.push(url) }, window: { addEventListener() {} },
  fetch: async (path) => (called.push(path),
    { status: 200, headers: { get: () => null }, json: async () => answers[path] || { ok: true, page: null } }) });
eval(channels);
eval(src);
const started = Date.now();
const report = () => {
  const plan = byId.log && byId.log.kids.find((k) => k.parts && k.parts.website);
  const wall = byId.log && byId.log.kids.find((k) => k.parts && k.parts["a.btn"]);
  const steps = plan && plan.parts.website.parts["website-steps"];
  const settled = went.length || wall || (steps && steps.hidden === false);
  if (!settled && Date.now() - started < 3000) return setTimeout(report, 20);
  console.log(JSON.stringify({ went, wrote, called, steps: Boolean(steps && steps.hidden === false),
    wall: wall ? wall.parts["a.btn"].href || null : null }));
};
setTimeout(report, 20);
"""


def _link_flow(search: str, *, signed_in: bool, state: dict) -> dict:
    """What home.js does with an address like the plan e-mail's link (`search`), signed in or not, over `state`."""
    from agent import site_install

    row = {
        "state": "needs_code",
        "say": site_install.NEEDS_CODE,
        "platform": "other",
        "steps": ["s"],
        "ways": site_install.ways_for(None),
    }
    install = {**site_install.install_part(None, "<script></script>"), "how": "Here is how to add it on your site."}
    done = subprocess.run(
        ["node", "-e", LINK_FLOW_HARNESS, str(CHANNELS_JS), str(HOME_JS)]
        + [
            json.dumps(
                {
                    "search": search,
                    "signedIn": signed_in,
                    "state": state,
                    "website": {"ok": True, "website": row, "install": install},
                }
            )
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_plan_emails_link_survives_the_sign_in():
    """Ruling R2-g (spec §3.2: "the homepage's card behind sign-in"): opened without a sign-in, the link goes to Google's
    sign-in first and comes back to itself, as /?setup=<id> does; the sign-in wall's link carries the way through its own
    sign-in, the address keeping it meanwhile; back from the wall's sign-in, the way stays in the address until the card
    is found, then that way opens on it."""
    from agent import site_install

    row = {
        "state": "needs_code",
        "say": site_install.NEEDS_CODE,
        "platform": "other",
        "steps": ["s"],
        "ways": site_install.ways_for(None),
    }
    planned = {
        "ok": True,
        "visitor": {"signed_in": True, "claimed": True, "wall": False},
        "result": {
            "session": {"status": "planned"},
            "cards": {"plan": {"goal": "g", "channels": [], "later": [], "setup": {"website": row}}},
            "plan_id": "pl_0123456789abcdef",
            "next": None,
        },
    }
    out = _link_flow("?website=install_self", signed_in=False, state=planned)
    assert out["went"] == ["/connect/google/start?next=%2F%3Fwebsite%3Dinstall_self"] and out["called"] == []
    walled = _link_flow("?website=install_self", signed_in=True, state={"ok": True, "signin": True})
    assert walled["wall"] == "/connect/google/start?next=%2F%3Fafter%3Dsignin%26website%3Dinstall_self"
    assert walled["wrote"] == [] and walled["went"] == []  # the address keeps the way while no card is found
    back = _link_flow("?after=signin&website=install_self", signed_in=True, state=planned)
    assert back["wrote"] == ["/?website=install_self", "/"]  # the sign-in's own mark goes, the way stays until the card
    assert "/api/page/website" in back["called"] and back["steps"] is True  # "I'll add it" open on the card


# home.js whole under Node, opened at a setup link (/?setup=biz_card: an assistant's, the receipt e-mail's, the plan
# page's, Stripe's return) by a signed-in owner, over LINK_FLOW_HARNESS's stub DOM -- its elements keeping their
# listeners here -- with `fetch` answering each `state` from `input.states` in turn (the last one again after that),
# `pick` with `input.pick`, `/api/launch_check` with `input.check` (the plan's own checklist, drawn as the plan is: L1),
# the csrf token, and anything else ok. The setup card is a stub that records when it opened (the calls made before it,
# whether a plan was drawn by then) and keeps its hooks, so the harness can press one of the checklist's buttons
# (`input.press`), type into the composer (`input.type`; what typed words get: `input.said`), let the card say the
# money is in (`input.settle`) and let the card go (`input.gone`). Prints the card's opening, every call, the calls made after the card opened, what
# the typing sent, whether the composer is shown at the end, the conversation's words, and whether the card is last
# in the conversation and the last thing scrolled into view.
SETUP_LINK_HARNESS = r"""
const [channels, src] = [process.argv[1], process.argv[2]].map((f) => require("fs").readFileSync(f, "utf8"));
const input = JSON.parse(process.argv[3]);
let revealed = null; // the element last scrolled into view
const node = (tag) => {
  const n = { tag, className: "", text: "", kids: [], parts: {}, dataset: {}, hidden: false, listeners: {},
    style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
    append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
    replaceChildren(...cs) { this.kids = cs; }, get children() { return this.kids; },
    get firstElementChild() { return this.kids[0] || null; },
    querySelector(sel) { const m = /data-part="([^"]+)"/.exec(sel), k = m ? m[1] : sel;
      return (this.parts[k] ||= node("part:" + k)); },
    querySelectorAll() { return []; }, closest() { return node("closest"); }, get parentElement() { return node("up"); },
    get content() { return { firstElementChild: node("template") }; }, cloneNode() { return node(tag); },
    remove() {}, replaceWith() {}, addEventListener(ev, fn) { this.listeners[ev] = fn; }, setAttribute() {},
    removeAttribute() {}, focus() {}, scrollIntoView() { revealed = this; }, isConnected: true };
  return n;
};
const byId = {}, called = [], opened = [], sent = [];
const doc = { body: node("body"), activeElement: null, createElement: node, createTextNode: (t) => ({ text: t, kids: [] }),
  getElementById: (id) => (byId[id] ||= node("#" + id)), querySelectorAll: () => [], addEventListener() {} };
doc.body.dataset.signedIn = "1";
byId.prompts = node("#prompts");
byId.prompts.textContent = "{}";
const states = [...input.states];
const answer = (path, body) =>
  path === "/api/csrf" ? { csrf: "t" }
  : path === "/api/claim" ? input.claim || { ok: true }
  : path === "/api/blocker" && input.blocker_answer ? input.blocker_answer
  : path === "/api/blocker" ? { ok: true, result: {} } // resolved() reads r.result, unlike the other routes here
  : path === "/api/launch_check" ? input.check || { ok: true, check: null } // the checklist's own look (checkPlan)
  : path !== "/api/turn" ? { ok: true, page: null }
  : body.action === "pick" ? input.pick
  : input.said && ["say", "start"].includes(body.action) ? input.said // what words typed into the composer get
  : states.length > 1 ? states.shift() : states[0];
const planned = () => Boolean(byId.log && byId.log.kids.some((k) => k.parts && k.parts.channels)); // a plan drawn
let card = null;
Object.assign(global, { document: doc, matchMedia: () => ({ matches: true }),
  location: { search: input.search, pathname: "/", assign() {}, reload() {} }, history: { replaceState() {} },
  window: { addEventListener() {}, AdmirySetup: { run: (o) => {
    card = o;
    opened.push({ business: o.businessId, page: o.page || null, before: [...called], planned: planned(),
      visible: !byId.convo.hidden });
    return { refresh: () => called.push("refresh") };
  } } },
  fetch: async (path, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    called.push(path === "/api/turn" ? ["turn", body.action, body.business_id].filter(Boolean).join(":") : path);
    sent.push({ path, page: (opts && opts.headers && opts.headers["X-Admiry-Business"]) || null });
    if (input.slow && path === "/api/blocker") {
      await new Promise((ok) => setTimeout(ok, input.slow));
      called.push("done:/api/blocker");
    }
    return { status: 200, headers: { get: () => null }, json: async () => answer(path, body) };
  } });
eval(channels);
eval(src);
const pause = (ms) => new Promise((ok) => setTimeout(ok, ms));
const buttons = (n, out = []) => {
  for (const k of [...(n.kids || []), ...Object.values(n.parts || {})]) {
    if (k.tag === "button") out.push(k);
    buttons(k, out);
  }
  return out;
};
const words = (n) => [n.text, ...[...(n.kids || []), ...Object.values(n.parts || {})].flatMap(words)].filter(Boolean);
// the owner types into the composer and sends it -- only while it is shown, as a person can: what went out, whether
// the dock (its Muse link) was there, and whether a plan is on the page after (follow-up final
// review C1)
const type = async (text) => {
  const at = called.length, shown = !byId.composer.hidden;
  if (shown) {
    byId.reply.value = text;
    byId.composer.listeners.submit({ preventDefault() {} });
    await pause(80);
  }
  return { shown, dock: !byId.dock.hidden, sent: called.slice(at), planned: planned() };
};
(async () => {
  for (const started = Date.now(); !card && Date.now() - started < 3000; ) await pause(10);
  await pause(20); // the page card's own load after the plan (loadPageState)
  const before = called.length;
  if (input.press) { // the checklist's own row (L1): already drawn, since the plan's own draw already checked it
    const press = buttons(byId.log).filter((b) => b.text === input.press).pop();
    if (press) { if (input.slow) press.listeners.click(); else await press.listeners.click(); }
    await pause(20);
  }
  const typed = input.type ? await type(input.type) : null; // on the page as the card left it
  if (input.settle) card.onSettled(), await pause(input.slow ? 200 : 50); // slow: room for draw()'s busy poll (100ms)
  if (input.gone) card.onGone(), await pause(50);
  const last = byId.log.kids[byId.log.kids.length - 1]; // the card's place: last in the conversation, and in view
  console.log(JSON.stringify({ opened, called, after: called.slice(before), planned_after: planned(), sent,
    labels: buttons(byId.log).map((b) => b.text), typed, composer: !byId.composer.hidden, said: words(byId.log),
    card_last: Boolean(card) && last === card.host, card_revealed: Boolean(card) && revealed === card.host }));
})();
"""


def _setup_link(states: list, pick: dict | None = None, **more) -> dict:
    """What home.js does at /?setup=biz_card for a signed-in owner, over the answers given (SETUP_LINK_HARNESS)."""
    done = subprocess.run(
        ["node", "-e", SETUP_LINK_HARNESS, str(CHANNELS_JS), str(HOME_JS)]
        + [json.dumps({"search": "?setup=biz_card", "states": states, "pick": pick, **more})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _visitor(business_id: str, session_id: str | None, claimed: bool = True) -> dict:
    """The visitor block every home answer carries, as `home_api._visitor_block` writes it (follow-up R1: whose page)."""
    return {
        "business_id": business_id,
        "signed_in": True,
        "claimed": claimed,
        "session_id": session_id,
        "wall": False,
    }


def _business_state(business_id: str) -> dict:
    """A reload's `state` answer for a planned business (the page reads whose it is from `result.business_id`)."""
    return {
        "ok": True,
        "visitor": _visitor(business_id, "cs_" + business_id[-4:]),
        "result": {
            "session": {"status": "planned"},
            "cards": {"plan": {"goal": "g", "channels": [], "later": [], "setup": {}}},
            "plan_id": "pl_" + business_id[-4:].rjust(16, "0"),
            "business_id": business_id,
            "next": None,
            "blocker": None,
        },
    }


PICKED = {"ok": True, "visitor": _visitor("biz_card", "cs_1")}


def _check_with(item: dict) -> dict:
    """L1: the `/api/launch_check` answer naming this one item on the active plan -- the checklist's own look
    (`checkPlan`), which the harness's `answer()` wires to `input.check`. Replaces B0's `onBlocker`, which handed a
    blocker into the conversation independent of any drawn plan (SETUP_LINK_HARNESS's stub `AdmirySetup.run`)."""
    return {"ok": True, "check": {"plan_id": item["plan_id"], "items": [item], "ready": False}}


def _turns(calls: list) -> list:
    return [c for c in calls if c.startswith("turn:")]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
@pytest.mark.parametrize(
    "first",
    [
        _business_state("biz_else"),  # the visitor cookie is another business of this owner's
        {"ok": True, "fresh": True, "choose": [{"id": "biz_card", "name": "Card", "state": "plan"}]},  # no cookie
        {"ok": True, "signin": True},  # the cookie's business is another owner's
    ],
    ids=["another-business", "no-cookie", "another-owner"],
)
def test_the_setup_link_attaches_its_business_and_draws_its_conversation_before_the_card_opens(first):
    """Final review C1 + I2: a setup link makes the page the card's business's -- `pick` attaches it (owner-checked by
    the server), its conversation is drawn, and only then does the card open -- so what the card hands the conversation
    is fixed for the card's business, and "Build your business website" builds on that plan. The card going then
    leaves the conversation as it is: nothing drawn twice."""
    out = _setup_link([first, _business_state("biz_card")], PICKED, gone=True)
    (opened,) = out["opened"]
    assert opened["business"] == "biz_card"
    assert _turns(opened["before"]) == ["turn:state", "turn:pick:biz_card", "turn:state"]
    assert opened["planned"]  # the card's business's plan was on the page before the card opened
    assert out["after"] == []  # its going draws nothing again


def _draft_state(business_id: str) -> dict:
    """A reload's `state` while a new business's questions are under way in this browser: an unclaimed draft."""
    return {
        "ok": True,
        "visitor": _visitor(business_id, "cs_" + business_id[-4:], claimed=False),
        "result": {
            "session": {"status": "open"},
            "cards": None,
            "plan_id": None,
            "business_id": business_id,
            "next": None,
            "blocker": None,
        },
    }


def _kept(calls: list) -> list:
    return [c for c in calls if c.startswith("turn:") or c == "/api/claim"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_setup_link_over_an_unclaimed_draft_claims_it_before_switching():
    """Follow-up R2: the draft's key lived only in the cookie the pick replaces -- it was lost; now it is claimed first
    (one of the owner's businesses, in the picker), then the page switches to the card's business."""
    out = _setup_link([_draft_state("biz_draft"), _business_state("biz_card")], PICKED, gone=True)
    (opened,) = out["opened"]
    assert _kept(opened["before"]) == ["turn:state", "/api/claim", "turn:pick:biz_card", "turn:state"]
    assert opened["planned"]
    # the claim names no business (follow-up final review I1): the page takes one only from the answers that make it
    # the link's -- the link's business named on the draft's claim would be refused as another page's (follow-up R1)
    assert [s["page"] for s in out["sent"] if s["path"] == "/api/claim"] == [None]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_draft_the_claim_cannot_keep_is_not_switched_away_from():
    """A claim the server refuses -- an error: the csrf token, the network, a race (another account's draft never gets
    this far: its `state` answers `signin`) -- switches nothing: the card alone, as a refused pick."""
    out = _setup_link([_draft_state("biz_draft")], PICKED, claim={"ok": False, "error": "x"}, gone=True)
    (opened,) = out["opened"]
    assert _kept(opened["before"]) == ["turn:state", "/api/claim"] and not opened["planned"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_setup_link_for_the_business_already_on_the_page_picks_nothing():
    out = _setup_link([_business_state("biz_card")], gone=True)
    (opened,) = out["opened"]
    assert _turns(out["called"]) == ["turn:state"] and opened["planned"]  # no pick: no key minted
    assert out["after"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_setup_link_for_a_business_not_this_owners_opens_the_card_alone_and_its_going_boots_the_home():
    """`pick` refused (not this owner's): today's behaviour -- the card alone, which says it is gone, and the home
    carries on with the visitor cookie's own conversation."""
    refused = {"ok": False, "error": "That business is not yours."}
    out = _setup_link([_business_state("biz_else")], refused, gone=True)
    (opened,) = out["opened"]
    assert _turns(opened["before"]) == ["turn:state", "turn:pick:biz_card"] and not opened["planned"]
    assert _turns(out["after"]) == ["turn:state"]  # the card gone: the home boots as it stands


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_build_your_business_website_builds_on_the_setup_links_page():
    """Final review I2: a prepaid business back from Stripe at /?setup=<id>&paid=1, whose second look's first refusal is
    NO_PAGE: the card hands it to the conversation, and its primary button builds the page (the page card's own
    build, on the plan the setup link drew)."""
    from agent import ads_run, blockers

    no_page = {
        "plan_id": "pl_000000000000card",
        "id": "page",
        "kind": "decide",
        "resolution": "page",
        "text": ads_run.NO_PAGE,
        "say": blockers.SAY.format(blocked=ads_run.NO_PAGE.rstrip(".")),
        "question": None,
        "actions": [blockers.BUILD_SITE, blockers.OWN_SITE],
    }
    out = _setup_link(
        [_business_state("biz_else"), _business_state("biz_card")],
        PICKED,
        check=_check_with(no_page),
        press=blockers.BUILD_SITE["label"],
    )
    assert "/api/page/create" in out["after"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_back_from_stripe_the_card_is_alone_until_the_money_lands():
    """Follow-up R4: back from Stripe (`&paid=1`) the page is the card's business's, but its conversation -- the plan's
    launch card with "Admiry runs it, I prepay" -- waits until the card says the money is in (`onSettled`), so no
    second pay form sits beside "being confirmed"."""
    out = _setup_link(
        [_business_state("biz_else"), _business_state("biz_card")],
        PICKED,
        search="?setup=biz_card&paid=1",
        settle=True,
    )
    (opened,) = out["opened"]
    assert _turns(opened["before"]) == ["turn:state", "turn:pick:biz_card"] and not opened["planned"]
    assert _turns(out["after"]) == ["turn:state"] and out["planned_after"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_back_from_stripe_the_conversation_is_drawn_once():
    """The money in and then the card gone: drawn once. The card gone before the money lands: drawn then."""
    paid = "?setup=biz_card&paid=1"
    out = _setup_link([_business_state("biz_card")], search=paid, settle=True, gone=True)
    assert _turns(out["after"]) == ["turn:state"] and out["planned_after"]
    out = _setup_link([_business_state("biz_card")], search=paid, gone=True)
    assert _turns(out["after"]) == ["turn:state"] and out["planned_after"]


def _write_copy_blocker() -> dict:
    from agent import blockers
    from agent.channels.google_search import adapter as google

    return {
        "plan_id": "pl_000000000000card",
        "id": "plan",
        "kind": "fix",
        "resolution": "write_copy",
        "text": google.TOO_LITTLE_COPY,
        "say": blockers.SAY.format(blocked=google.TOO_LITTLE_COPY.rstrip(".")),
        "question": None,
        "actions": [blockers.WRITE_COPY],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_every_write_names_the_business_its_page_shows():
    """Follow-up R1: the first look names no business (none is known yet); once the page knows whose it is, every
    request it sends names that business, so the server can refuse a write meant for another one."""
    from agent import blockers

    out = _setup_link(
        [_business_state("biz_card")], check=_check_with(_write_copy_blocker()), press=blockers.WRITE_COPY["label"]
    )
    assert next(s for s in out["sent"] if s["path"] == "/api/turn")["page"] is None  # the first look: none known yet
    assert [s["page"] for s in out["sent"] if s["path"] == "/api/blocker"] == ["biz_card"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_write_refused_for_another_business_offers_a_reload():
    from agent import blockers, home_api

    refused = {"ok": False, "error": home_api.OTHER_PAGE, "reload": True}
    out = _setup_link(
        [_business_state("biz_card")],
        check=_check_with(_write_copy_blocker()),
        press=blockers.WRITE_COPY["label"],
        blocker_answer=refused,
    )
    assert "Reload" in out["labels"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_card_opened_from_the_conversation_names_the_business_its_page_shows():
    from agent import ads_run, blockers

    funds = {
        "plan_id": "pl_000000000000card",
        "id": "funds",
        "kind": "decide",
        "resolution": "funds",
        "text": ads_run.NO_FUNDS,
        "say": blockers.SAY.format(blocked=ads_run.NO_FUNDS.rstrip(".")),
        "question": None,
        "actions": [blockers.ADD_FUNDS],
    }
    out = _setup_link([_business_state("biz_card")], check=_check_with(funds), press=blockers.ADD_FUNDS["label"])
    assert out["opened"][-1]["business"] is None and out["opened"][-1]["page"] == "biz_card"


def _no_consultation_state(business_id: str) -> dict:
    """A reload's `state` for a business with no consultation -- a plan an assistant made (create_marketing_plan)."""
    return {
        "ok": True,
        "visitor": _visitor(business_id, None),
        "result": {
            "session": None,
            "cards": None,
            "plan_id": None,
            "business_id": business_id,
            "next": None,
            "blocker": None,
        },
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_setup_link_for_a_business_with_no_consultation_keeps_its_card_in_view():
    """Follow-up R5: a business an assistant made a plan for has no consultation to draw. `resume` took it for a first
    visit and hid the conversation the card opens in -- the start page showed instead; now the card opens in view, and
    its going leaves the page as it is."""
    out = _setup_link([_business_state("biz_else"), _no_consultation_state("biz_card")], PICKED, gone=True)
    (opened,) = out["opened"]
    assert _turns(opened["before"]) == ["turn:state", "turn:pick:biz_card", "turn:state"]
    assert opened["visible"] and not opened["planned"]
    assert out["after"] == []


PAID = "?setup=biz_card&paid=1"
# what a change typed into the composer gets (`say` -> adjust): the plan drawn again, its cards
CHANGED = {
    "ok": True,
    "visitor": _visitor("biz_card", "cs_1"),
    "result": {
        "data": {
            "cards": {"plan": {"goal": "g", "channels": [], "later": [], "setup": {}}},
            "plan_id": "pl_000000000000card",
            "business_id": "biz_card",
        },
        "say": "Done.",
    },
}
# the composer hidden; the dock -- "Continue in Muse instead", "Talk to a person" -- still there; nothing sent or drawn
NOT_TYPED = {"shown": False, "dock": True, "sent": [], "planned": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
@pytest.mark.parametrize(
    ("states", "pick"),
    [([_business_state("biz_card")], None), ([_business_state("biz_else"), _business_state("biz_card")], PICKED)],
    ids=["cookie-already-on-it", "picked"],
)
def test_back_from_stripe_the_composer_waits_for_the_conversation(states, pick):
    """Follow-up final review C1: back from Stripe the card stands alone. Words typed then started a consultation (no
    session on the page: a keyed `start` -- an unasked model call, the plan's consultation superseded) or, after a pick,
    drew the plan and its launch card -- "Admiry runs it, I prepay" -- beside "being confirmed". The composer is hidden
    until the conversation is drawn; the dock's other ways stay."""
    out = _setup_link(states, pick, search=PAID, type="make it $20 a day", said=CHANGED, settle=True)
    assert out["typed"] == NOT_TYPED
    assert out["planned_after"] and out["composer"]  # the money in: the conversation drawn, the composer back


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
@pytest.mark.parametrize(
    ("states", "pick"),
    [
        ([_no_consultation_state("biz_card")], None),
        (
            [_business_state("biz_else"), _no_consultation_state("biz_card")],
            {"ok": True, "visitor": _visitor("biz_card", None)},
        ),
    ],
    ids=["cookie-already-on-it", "picked"],
)
def test_a_business_with_no_consultation_takes_no_words_in_the_composer(states, pick):
    """Follow-up final review C1: with no consultation to draw, the card hands over the phone or the address question
    while the composer said "Or just tell me in your own words…" -- and typed words started a consultation. The
    composer stays hidden for good: the question's own field takes the answer."""
    out = _setup_link(states, pick, type="my phone is 650-555-0100", gone=True)
    assert out["typed"] == NOT_TYPED
    assert not out["composer"]  # the card gone too: still no conversation to type into


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
@pytest.mark.parametrize(
    ("states", "pick", "claim"),
    [
        ([_business_state("biz_else")], {"ok": False, "error": "That business is not yours."}, None),
        ([_draft_state("biz_draft")], PICKED, {"ok": False, "error": "x"}),
    ],
    ids=["refused-pick", "refused-claim"],
)
def test_a_card_alone_takes_no_words_until_it_goes(states, pick, claim):
    """Follow-up final review C1: a refused pick or claim leaves the card alone over the visitor cookie's own business
    -- after a refused claim, the very draft R2 keeps -- which typed words would supersede with a new consultation.
    The composer comes back with the home the card's going boots."""
    out = _setup_link(states, pick, claim=claim, type="hello", gone=True)
    assert out["typed"] == NOT_TYPED
    assert _turns(out["after"]) == ["turn:state"] and out["composer"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
@pytest.mark.parametrize(
    ("states", "pick"),
    [([_business_state("biz_card")], None), ([_business_state("biz_else"), _business_state("biz_card")], PICKED)],
    ids=["cookie-already-on-it", "picked"],
)
def test_back_from_stripe_the_card_stays_in_view_below_the_conversation(states, pick):
    """Follow-up final review M1: the money in, the conversation was drawn below the card, and its plan's intro scrolled
    the card -- now at its review step, Confirm and launch -- out of view. The card moves below what was drawn and is
    scrolled into view."""
    out = _setup_link(states, pick, search=PAID, settle=True)
    assert out["planned_after"] and out["card_last"] and out["card_revealed"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_back_from_stripe_another_business_by_then_offers_the_reload():
    """Follow-up final review M2: while the card waited, another tab switched this browser to another business, and the
    look that draws the conversation drew that business's under this card. Now it offers the reload the server's own
    refusal offers (follow-up R1), and draws nothing."""
    from agent import home_api

    out = _setup_link([_business_state("biz_card"), _business_state("biz_else")], search=PAID, settle=True)
    assert home_api.OTHER_PAGE in out["said"] and "Reload" in out["labels"]
    assert not out["planned_after"] and not out["composer"]  # nothing drawn, so nothing to type into


# home.js's chooseWay read out of the script: "Use an Admiry page instead" on a plan with its page card (ruling R2-f)
CHOOSE_HARNESS = (
    LAUNCH_TREE
    + r"""
const seen = [];
const instead = new Node({ part: "page-instead" }, "msg", input.instead);
instead.hidden = true;
const build = Object.assign(new Node({ part: "page-build" }, "btn", "", "BUTTON"), { focus: () => seen.push("focus build") });
const page = new Node({ part: "page" }, "card", "", "SECTION").add(instead, build);
const plan = new Node({}, "plan").add(page);
const ctx = vm.createContext({ work: () => seen.push("work"), reveal: (n) => seen.push("shown " + n.dataset.part),
  announce: (t) => seen.push("said " + t) });
const chooseWay = vm.runInContext([line("  const part = "), grab("chooseWay"), "chooseWay;"].join("\n"), ctx);
chooseWay(plan, "admiry_page");
console.log(JSON.stringify({ seen, instead: instead.hidden ? null : instead.text }));
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_admiry_page_way_says_the_website_stays_on_the_page_card(client):
    """Ruling R2-f: the page ships spec §3.1's sentence for the fourth way (site_install.ADMIRY_PAGE_WAY), hidden on the
    page card; tapping "Use an Admiry page instead" shows and says it, and brings the page card with its build button."""
    from agent import site_install

    plan_block = client.get("/").text.split('id="t-plan"')[1].split("</template>")[0]
    page = plan_block.split('data-part="page"')[1].split("</section>")[0]
    assert f'<p class="msg" data-part="page-instead" hidden>{site_install.ADMIRY_PAGE_WAY}</p>' in page
    seen = _node(CHOOSE_HARNESS, {"instead": site_install.ADMIRY_PAGE_WAY})
    assert seen["instead"] == site_install.ADMIRY_PAGE_WAY
    assert seen["seen"] == [f"said {site_install.ADMIRY_PAGE_WAY}", "shown page", "focus build"]


# home.js's page card (pageCard, chooseWay) read out of the script the same way, over the plan template's own parts
# (page-instead, page-build, page-body), with loadPageState a recorder: R5 (spec 2026-10-12 §3.7) -- no page card
# beside a website of the business's own whose ads do not yet use an Admiry page, until "Use an Admiry page instead"
# (chooseWay) reveals it and loads its state as pageCard would have.
PAGE_HARNESS = (
    LAUNCH_TREE
    + r"""
const did = [];
const section = () => {
  const instead = new Node({ part: "page-instead" }, "msg", "", "P");
  instead.hidden = true;
  const build = Object.assign(new Node({ part: "page-build" }, "btn two", "", "BUTTON"), { focus() {} });
  return new Node({ part: "page" }, "card", "", "SECTION").add(instead, build, new Node({ part: "page-body" }, "", "", "DIV"));
};
const ctx = vm.createContext({ work: (f) => f(), reveal() {}, announce() {}, loadPageState: () => did.push("loaded") });
const { pageCard, chooseWay } = vm.runInContext(
  [line("  const part = "), grab("pageCard"), grab("chooseWay"), "({ pageCard, chooseWay });"].join("\n"), ctx);
console.log(JSON.stringify(input.cases.map(({ website, choose }) => {
  did.length = 0;
  const page = section(), plan = new Node({}, "plan").add(page);
  pageCard(plan, website);
  if (choose) chooseWay(plan, "admiry_page");
  return { hidden: Boolean(page.hidden), instead: !page.querySelector('[data-part="page-instead"]').hidden,
    loaded: did.includes("loaded") };
})));
"""
)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_no_page_card_beside_a_website_of_the_businesss_own():
    """R5, spec 2026-10-12 §3.7: while the plan's "Your website" card tracks a site of the business's own whose ads
    do not yet use an Admiry page (needs_code or found), the page card draws hidden and its state is not fetched --
    "Use an Admiry page instead" (chooseWay, the website card's fourth way) is what reveals it and loads its state, as
    pageCard would have on its own draw. A row that is already admiry_page, or no website row at all (a business
    with no website of its own), is as before: the page card shows and loads at once."""
    from agent import site_install

    needs = {"state": "needs_code", "say": site_install.NEEDS_CODE}
    cases = [
        {"website": needs},  # needs_code: hidden, its state not loaded
        {"website": needs, "choose": True},  # "Use an Admiry page instead": shown, the sentence too, state loaded
        {"website": {"state": "admiry_page", "say": site_install.ADMIRY_PAGE}},  # already an Admiry page: as before
        {"website": None},  # no website row: as before
    ]
    seen = _node(PAGE_HARNESS, {"cases": cases})
    assert seen[0] == {"hidden": True, "instead": False, "loaded": False}
    assert seen[1] == {"hidden": False, "instead": True, "loaded": True}
    assert seen[2] == {"hidden": False, "instead": False, "loaded": True}
    assert seen[3] == {"hidden": False, "instead": False, "loaded": True}


# ---------------------------------------------------------------- the plan's picture (M1 Task 7, plan P7)

# HOME_HARNESS's page with another report: what the ad preview's picture mount holds -- each child's tag, address,
# words for a screen reader and text -- and whether it, the ad's headline line and the whole preview were taken off the
# card.
PICTURE_HARNESS = (
    HOME_HARNESS.split("const started = Date.now();")[0].replace("remove() {}", "remove() { this.removed = true; }")
    + r"""
const started = Date.now();
const report = () => {
  const plan = byId.log && byId.log.kids.find((k) => k.parts && k.parts.channels);
  if (!plan) return Date.now() - started < 10000 ? setTimeout(report, 20) : process.exit(3);
  const fig = plan.parts["ad-image"] || { kids: [] }, gone = (k) => Boolean((plan.parts[k] || {}).removed);
  const kids = fig.kids.map((k) => [k.tag, k.src || null, k.alt || null, k.text || null]);
  console.log(JSON.stringify({ removed: Boolean(fig.removed), section: gone("ad-preview"),
    headlines: gone("ad-headlines"), kids }));
};
setTimeout(report, 20);
"""
)


def _render_picture(cards: dict) -> dict:
    """What the homepage (home.js, drawing with channels.js) puts in the ad preview's picture mount for these cards."""
    state = {
        "ok": True,
        "visitor": {"signed_in": True, "claimed": True, "wall": False},
        "result": {"session": {"status": "planned"}, "cards": cards, "plan_id": "pl_0123456789abcdef"},
    }
    done = subprocess.run(
        ["node", "-e", PICTURE_HARNESS, str(CHANNELS_JS), str(HOME_JS), json.dumps(state)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _newsbreak_cards(conn, monkeypatch, tmp_path) -> dict:
    """A roofer's plan funding Google Search and NewsBreak -- its version pinned the trade's picture when the set had
    one (M1 Task 7 fix round 1) -- its cards drawn with the generated licensed set."""
    from agent import plans
    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import creative_set
    from agent.tests.test_plans_channels import home_plan

    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    monkeypatch.setattr(creative, "ROOT", creative_set(tmp_path / "creative"))
    sections, _ = home_plan(conn, prepaid=True)
    return plans.cards({"id": "pl_1", "business_id": "biz_1", "business_name": "Summit Roofing", "sections": sections})


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_the_ad_preview_shows_the_picture_with_its_words_and_the_line_that_approving_approves_it(
    conn, monkeypatch, tmp_path
):
    """P7: "How your ad reads" shows the picture NewsBreak shows with the ad -- with the manifest's words for a screen
    reader -- and the line under it, drawn by channels.js (home.js only hands it the card's part)."""
    cards = _newsbreak_cards(conn, monkeypatch, tmp_path)
    got = _render_picture(cards)
    assert got == {
        "removed": False,
        "section": False,
        "headlines": False,
        "kids": [
            ["img", "http://testserver/static/creative/home_services/1.jpg", "A solid green test picture", None],
            ["figcaption", None, None, "NewsBreak shows this picture with your ad; approving the plan approves it"],
        ],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_picture_that_left_the_set_is_said_and_none_is_shown(conn, monkeypatch, tmp_path):
    """Fix round 1, item 4: the version's picture is no longer in Admiry's set -- the line says so, and no picture is
    drawn; nor is an address that is not the web's."""
    from agent import plans

    cards = _newsbreak_cards(conn, monkeypatch, tmp_path)
    gone = plans.IMAGE_GONE.format(label="NewsBreak")
    cards["ads_preview"].update(image=None, image_note=gone)
    shown = {"removed": False, "section": False, "headlines": False, "kids": [["figcaption", None, None, gone]]}
    assert _render_picture(cards) == shown
    cards["ads_preview"]["image"] = {"url": "javascript:alert(1)", "alt": "x"}
    assert _render_picture(cards)["kids"] == [["figcaption", None, None, gone]]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_with_no_headline_left_the_picture_and_its_line_are_still_shown(conn, monkeypatch, tmp_path):
    """Fix round 1, item 5 (review M6): no headline survived the length check, so there is no ad text to show -- but
    approving the plan still approves its picture, so the preview stays for the picture and its line; only an empty
    preview with no picture either goes."""
    cards = _newsbreak_cards(conn, monkeypatch, tmp_path)
    cards["ads_preview"].update(headlines=[], descriptions=[])
    got = _render_picture(cards)
    assert (got["section"], got["headlines"], got["removed"]) == (False, True, False)
    assert got["kids"] == [
        ["img", "http://testserver/static/creative/home_services/1.jpg", "A solid green test picture", None],
        ["figcaption", None, None, "NewsBreak shows this picture with your ad; approving the plan approves it"],
    ]
    del cards["ads_preview"]["image"], cards["ads_preview"]["image_note"]
    assert _render_picture(cards) == {"removed": False, "section": True, "headlines": False, "kids": []}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_plan_whose_channels_show_no_picture_has_no_picture_on_its_card(conn):
    assert _render_picture(_plan_cards(conn)) == {"removed": True, "section": False, "headlines": False, "kids": []}


def test_the_ad_preview_ships_a_mount_for_the_picture_and_channels_js_draws_it(client):
    html = client.get("/").text
    preview = html.split('data-part="ad-preview"')[1].split("</section>")[0]
    assert '<figure class="ad-image" data-part="ad-image"></figure>' in preview
    assert "AdmiryChannels.picture(" in client.get("/static/home.js").text
    assert "function picture(" in client.get("/static/channels.js").text
    assert ".ad-image" in client.get("/static/home.css").text
