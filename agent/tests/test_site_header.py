"""The shared header and its account menu (spec 2026-09-29-admiry-settings-redesign §3, §6): one partial, the avatar
opens a native <details> menu (e-mail, Your ads, Settings, Sign out) instead of a separate SETTINGS item; the shared
styles and the progressive enhancements live in base.css and site.js."""

import re

from agent.tests.test_home_page import split  # noqa: F401 - a pytest fixture, used by name
from agent.tests.test_web import sign_in


def _header(html: str) -> str:
    return html.split('<header class="top">')[1].split("</header>")[0]


def test_signed_out_header_offers_sign_in_and_no_menu(client):
    header = _header(client.get("/").text)
    assert ">Programmatic</a>" in header
    assert 'class="signin" href="/connect/google/start?next=%2F"' in header
    assert "account-menu" not in header and "<form" not in header


def test_signed_in_header_has_the_account_menu(client, conn, google, split):  # noqa: F811 - the fixture
    sign_in(client, next_path="/")
    header = _header(client.get("/").text)
    assert '<details class="account-menu">' in header
    assert '<summary aria-label="Your account">O</summary>' in header
    menu = header.split('<details class="account-menu">')[1].split("</details>")[0]
    csrf = conn.execute("SELECT csrf FROM web_session WHERE revoked_at IS NULL").fetchone()["csrf"]
    assert "owner@example.com" in menu
    assert '<a class="menu-item" href="/connect">Settings</a>' in menu
    assert '<form method="post" action="/connect/signout">' in menu
    assert f'<input type="hidden" name="csrf" value="{csrf}">' in menu
    assert '<button class="menu-item" type="submit">Sign out</button>' in menu
    # the separate SETTINGS item is gone: outside the menu the nav has no link to /connect
    outside = re.sub(r'<details class="account-menu">.*?</details>', "", header, flags=re.DOTALL)
    assert ">Settings</a>" not in outside and 'href="/connect"' not in outside
    assert 'href="/programmatic"' in outside and '<a href="/ads">Your ads</a>' in outside and ">Insights<" in outside
    assert '<a class="menu-item" href="/ads">Your ads</a>' in menu
    assert "Campaigns" not in header
    assert 'class="signin"' not in header


def test_the_menus_sign_out_lands_on_the_home_signed_out(client, conn, google, split):  # noqa: F811 - the fixture
    sign_in(client, next_path="/")
    menu = _header(client.get("/").text).split('<details class="account-menu">')[1]
    csrf = menu.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/connect/signout", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    header = _header(client.get("/").text)
    assert "account-menu" not in header and 'class="signin"' in header


def test_base_css_and_site_js_are_served_and_linked_before_the_page_files(client):
    base, site, home_css = client.get("/static/base.css"), client.get("/static/site.js"), client.get("/static/home.css")
    assert base.status_code == 200 and site.status_code == 200
    assert base.headers["cache-control"] == "no-cache" and site.headers["cache-control"] == "no-cache"
    html = client.get("/").text
    assert html.index('<link rel="stylesheet" href="/static/base.css">') < html.index('href="/static/home.css"')
    assert '<script src="/static/site.js" defer></script>' in html
    assert html.index('src="/static/site.js"') < html.index('src="/static/home.js"')
    # moved: the fonts, the tokens and the header live in base.css only
    assert ".wordmark {" not in home_css.text and "@font-face" not in home_css.text and "--accent:" not in home_css.text
    # the website's front page's brand blue since 2026-10-06 (website/app/globals.css --color-brand)
    assert ".wordmark {" in base.text and base.text.count("@font-face") == 2 and "--accent: #345BFF" in base.text
    assert "https://" not in base.text  # no third-party fonts


def test_base_css_carries_the_shared_components_and_the_phone_menu(client):
    css = client.get("/static/base.css").text
    for rule in (
        ".account-menu {",
        ".account-menu > summary {",
        ".account-menu .menu {",
        ".menu-email {",
        ".menu-item {",
        ".btn {",
        ".btn-primary {",
        ".btn-danger-quiet {",
        ".badge {",
        ".badge-success {",
        ".badge-warning {",
        ".badge-neutral {",
        ".chip {",
        ".card {",
        ".sr {",
        ".muted {",
    ):
        assert rule in css, rule
    phone = css.split("@media (max-width: 720px) {")[1].split("\n}")[0]
    assert "position: fixed; left: 12px; right: 12px; top: 64px;" in phone and "min-height: 44px" in phone


def test_site_js_is_small_dependency_free_and_keyed_on_the_shared_attributes(client):
    js = client.get("/static/site.js").text
    assert js.startswith('"use strict";') and js.count("\n") <= 120
    assert "innerHTML" not in js and "eval(" not in js and "import " not in js and "https://" not in js
    for hook in ("details.account-menu", "form[data-confirm]", "button[data-copy]", "nav[data-sections]"):
        assert hook in js, hook
    assert '"Escape"' in js and "IntersectionObserver" in js and "aria-current" in js and "window.confirm" in js
