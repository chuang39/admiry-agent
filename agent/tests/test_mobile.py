"""Phones (the mobile audit, 2026-09-30): what a 375px or 320px screen needs from the owner's pages -- 44px targets,
16px fields (iOS Safari zooms into anything smaller, then pans sideways), light form controls on a dark-mode phone, no
sideways scroll, the conversation's input above the on-screen keyboard, and an agreement sheet that can always be
closed. The look is checked in a browser at 375 and 320 by hand; these pin the rules that make it."""

import re
from pathlib import Path

import agent.mcp_app  # noqa: F401 - before agent.web: entered at agent.connect, the connect/surface.told cycle fails
from agent.templating import env
from agent.tests.test_oauth_consent import _sign_in, _txn
from agent.tests.test_oauth_http import register
from agent.tests.test_pages import owner_plan
from agent.tests.test_web import sign_in

TEMPLATES = Path(__file__).parents[1] / "templates"
# every page written by hand rather than on the home's or Settings' stylesheets
HAND_WRITTEN = (
    "plan.html",
    "oauth_consent.html",
    "mail_link.html",
    "signin.html",
    "notice.html",
    "key_once.html",
    "code_once.html",
    "legal.html",
    "connect_docs.html",
)
# the three an owner acts on, usually on a phone: approve a plan, let an assistant in, continue from an e-mail -- and,
# since the founder's ruling of 2026-10-07 that every Admiry page looks like the front page, the rest of Admiry's own:
# sign-in, a notice, a new key, an approval code, the legal texts, the connector docs and the photo upload
ON_PAGE_CSS = (
    "plan.html",
    "oauth_consent.html",
    "mail_link.html",
    "signin.html",
    "notice.html",
    "key_once.html",
    "code_once.html",
    "legal.html",
    "connect_docs.html",
    "upload.html",
)


def phone_block(css: str) -> str:
    """Every `@media (max-width: 720px)` block's rules, in order."""
    return "\n".join(b.split("\n}")[0] for b in css.split("@media (max-width: 720px) {")[1:])


def rule(css: str, selector: str) -> str:
    """The declarations of every rule whose selector list contains `selector`, in order (comments dropped)."""
    found = [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
        if selector in [s.strip() for s in re.split(r",(?![^()]*\))", m.group(1))]
    ]
    assert found, f"no rule for {selector}"
    return " ".join(found)


def test_every_hand_written_page_keeps_its_controls_light_on_a_dark_mode_phone():
    """The consent page's buttons once drew as two empty boxes on the founder's dark-mode iPhone (2026-09-28): the
    home and Settings declare a light scheme; so does every page written by hand."""
    for name in HAND_WRITTEN:
        assert '<meta name="color-scheme" content="light">' in (TEMPLATES / name).read_text(), name


def test_the_pages_an_owner_acts_on_are_on_the_shared_stylesheets():
    """Admiry's own pages wear the front page's look: base.css (the website's palette, Geist, the header) then
    page.css, no style of their own, and the brand header that links home."""
    for name in ON_PAGE_CSS:
        html = (TEMPLATES / name).read_text()
        assert "<style" not in html and " style=" not in html, name
        assert html.index('href="/static/base.css"') < html.index('href="/static/page.css"'), name
        assert '{% include "_brand_header.html" %}' in html and '<body class="page">' in html, name


def test_page_css_gives_phones_44px_targets_and_full_width_actions(client):
    r = client.get("/static/page.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    phone = phone_block(r.text)
    assert "min-height: 44px" in rule(phone, ".btn")
    assert "flex-direction: column" in rule(phone, ".actions")
    assert "width: 100%" in rule(phone, ".actions .btn")
    assert "min-height: 44px" in rule(r.text, ".choice")  # a whole labelled row is the target, on every screen
    assert "padding: 16px" in rule(phone, ".card")  # the words beside a box keep their width at 320px


def test_on_the_plan_page_approving_is_the_one_primary_button_and_comes_first(client, conn, monkeypatch, google):
    plan_id, _ = owner_plan(client, conn, monkeypatch)
    sign_in(client, f"/plan/{plan_id}")
    html = client.get(f"/plan/{plan_id}").text
    approve, code = f'action="/plan/{plan_id}/approve"', f'action="/plan/{plan_id}/code"'
    assert html.index(approve) < html.index(code)  # stacked on a phone, approving is on top
    assert '<button class="btn btn-primary" type="submit">Approve and launch</button>' in html
    assert '<button class="btn" type="submit">Show my approval code</button>' in html
    assert html.count("btn-primary") == 1
    assert 'class="brand"' in html  # opened from an e-mail on a phone, the page says whose it is


def test_the_plan_page_says_when_it_was_made_in_words_not_an_iso_string(client, conn, monkeypatch):
    plan_id, _ = owner_plan(client, conn, monkeypatch)
    html = client.get(f"/plan/{plan_id}").text
    assert re.search(
        r'Created <time datetime="\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ" title="[^"]+ UTC">just now</time>', html
    )
    assert not re.search(r"\d\d:\d\d:\d\d\.\d{6}", html)  # no microseconds on screen


def test_plan_tables_scroll_inside_the_page_never_the_page(client, conn, monkeypatch):
    plan_id, _ = owner_plan(client, conn, monkeypatch)
    html = client.get(f"/plan/{plan_id}").text
    assert html.count("<table") >= 1 and html.count("<table") == html.count('<div class="wide"><table')
    css = client.get("/static/page.css").text
    assert "overflow-x: auto" in rule(css, ".wide")
    # break-word, not anywhere: `anywhere` shrinks a cell's min-content, so the table squeezes instead of scrolling
    assert "overflow-wrap: break-word" in rule(css, ".page-main td") and "anywhere" not in rule(css, ".page-main td")


def test_on_the_consent_page_allow_is_primary_and_every_choice_is_a_whole_row(client, conn, google):
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    _sign_in(client, google)
    page = client.get(f"/oauth/consent?txn={txn}").text
    assert '<button class="btn btn-primary" type="submit" name="decision" value="allow">Allow</button>' in page
    assert '<button class="btn" type="submit" name="decision" value="deny">Not now</button>' in page
    rows = re.findall(r'<label class="choice">\s*<input type="(radio|checkbox)"', page)
    assert rows.count("radio") == 1 and rows.count("checkbox") == 4  # "A new business" and the four scopes
    assert 'class="brand"' in page


def test_the_mail_link_page_ends_in_one_primary_button_and_whole_row_choices():
    scopes = [
        {"id": "read", "words": "See the plan and results", "fixed": True},
        {"id": "manage", "words": "Make changes within the monthly limit", "fixed": False},
    ]
    html = env.get_template("mail_link.html").render(
        name="Bright Smile Dental",
        email="o@example.com",
        token="t",
        csrf="c",
        ask=True,
        address="cue@example.com",
        scopes=scopes,
    )
    assert '<button class="btn btn-primary" type="submit">See my plan</button>' in html
    assert len(re.findall(r'<label class="choice">\s*<input type="checkbox"', html)) == 3  # allow + two scopes
    assert 'class="brand"' in html


def test_the_connector_docs_never_scroll_sideways_on_a_phone(client):
    html = client.get("/connect/docs").text
    assert html.count("<table") == html.count('<div class="wide"><table') >= 3
    css = client.get("/static/page.css").text
    assert "overflow-x: auto" in rule(css, ".wide")
    # a cell breaks a long value only when it must (break-word): `anywhere` would squeeze the columns until tool names
    # split mid-word, and the table would never scroll inside .wide
    assert "overflow-wrap: break-word" in rule(css, ".page-main td")
    assert "overflow-wrap: break-word" in rule(css, ".page-main td code")
    assert "overflow-wrap: anywhere" not in rule(css, ".page-main code")
    assert "white-space: pre-wrap" in rule(css, ".page-main pre")  # a long prompt wraps inside its box
    # a paragraph too: the guide's folder, `{base}/connect/references/` in <code>, is wider than a phone's line (373px on
    # the production host against 343) and the page would scroll sideways
    assert "overflow-wrap: anywhere" in rule(css, ".page-main p")  # breaks a long address in <code> too


def test_no_field_on_the_home_is_under_16px_on_a_phone(client):
    phone = phone_block(client.get("/static/home.css").text)
    for field in (".other input", ".prompt", ".page-change input"):  # the page card's "What should I change?" too
        assert "font-size: 16px" in rule(phone, field), field


def test_the_home_links_a_phone_can_miss_are_44px_tall(client):
    phone = phone_block(client.get("/static/home.css").text)
    for link in (".fine a", ".setup-how .linkish"):
        assert "min-height: 44px" in rule(phone, link), link


def test_the_agreement_sheet_keeps_close_in_reach_while_it_scrolls(client):
    css = client.get("/static/home.css").text
    head = rule(css, ".legal-head")
    assert "position: sticky" in head and "top: 0" in head
    js = client.get("/static/setup.js").text
    assert 'button("btn close", "Close"' not in js  # the old one: a word squeezed into a 44px circle
    assert 'setAttribute("aria-label", "Close")' in js and '"Done"' in js  # an icon up top, Done at the end
    assert 'sheet.setAttribute("aria-labelledby"' in js  # the dialog is named by its title
    base = css.index(".sheet.legal {")
    last_phone = css.rindex("@media (max-width: 720px) {")
    assert last_phone > base and "92dvh" in rule(css[last_phone:], ".sheet.legal")  # after the base rule, so it wins


def test_an_agreement_row_keeps_its_words_together_beside_the_box(client):
    css = client.get("/static/home.css").text
    assert "grid-template-columns: auto minmax(0, 1fr)" in rule(css, ".setup-doc")
    js = client.get("/static/setup.js").text
    assert 'el("span", "setup-doc-text"' in js
    assert "e.metaKey || e.ctrlKey || e.shiftKey" in js  # a modified click opens the text in a new tab as usual
    assert "min-height: 44px" in rule(phone_block(css), ".setup-doc")


def test_the_conversation_input_follows_the_keyboard(client):
    """Android Chrome shrinks the page for the keyboard only when asked (interactive-widget); iOS never does, so the
    fixed dock rides the visual viewport. A field in the conversation (another answer, a customer ID, an amount) hides
    the dock while it is being typed in: one input above the keyboard, never two."""
    html = client.get("/").text
    viewport = re.search(r'<meta name="viewport" content="([^"]+)">', html).group(1)
    assert "interactive-widget=resizes-content" in viewport
    site = client.get("/static/site.js").text
    assert "visualViewport" in site and "vv.scale" in site  # zoomed in, the dock is not chased around
    phone = phone_block(client.get("/static/home.css").text)
    # the budget slider (a range input in the plan card) opens no keyboard: touching it keeps the dock
    hide = 'body:has(.log input:not([type="checkbox"], [type="radio"], [type="range"]):focus) .dock'
    assert "display: none" in rule(phone, hide)


def test_the_reply_box_shows_no_scrollbar_under_its_cap_and_resizes_when_refilled(client):
    js = client.get("/static/home.js").text
    assert 'reply.style.overflowY = reply.scrollHeight > 160 ? "auto" : "hidden"' in js
    assert "reply.value = text;" not in js  # a refill (one tap to try again) goes through grow()
    assert 'addEventListener("resize", grow)' in js  # a rotation re-fits the box (it clips, not scrolls, under the cap)
