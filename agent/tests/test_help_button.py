"""The Help button on the pages (spec 2026-10-12 §3.1, §3.2, §3.4): the partial at the end of <body> on every page an
owner or a visitor reads while support is on, on none of the excluded ones, its words (the mockup's, owner words), and
the script's and the sheet's manners."""

import re
from pathlib import Path

from agent.templating import env
from agent.tests.conftest import JARGON
from agent.tests.test_home_api import fake  # noqa: F401 - a fixture used by name (planned() drives the model)
from agent.tests.test_hosted_serving import HOST, _business, _page, pages_client  # noqa: F401 - a fixture used by name
from agent.tests.test_mobile import phone_block, rule
from agent.tests.test_never_elsewhere import ELSEWHERE
from agent.tests.test_transcript import planned
from agent.tests.test_web import sign_in

PILL = 'id="help-pill"'
WORDS = (
    "What's wrong, or what do you need?",
    "A person at Admiry reads every message.",
    "Say it in your own words: what you were doing, and what happened.",
    "Your e-mail",
    "So a person can write back. Signed in, we already have it.",
    "We add the page you are on and your last few messages, so you don't have to explain them.",
    "Cancel",
    "Send",
    "Help",
)


def _partial() -> str:
    return env.get_template("_help.html").render(help=True)


def test_the_pill_is_on_every_page_of_the_spec_once_and_last(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    for path in ("/", "/legal/management-agreement", "/legal/prepayment-addendum", "/connect", "/ads"):
        html = client.get(path).text  # a visitor: the home, the legal texts, the sign-in page (/connect, /ads)
        assert PILL in html and "/static/help.js" in html and "/static/help.css" in html, path
        assert html.count(PILL) == 1 and html.rindex(PILL) > html.rindex("</main>"), path
    planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    for path in ("/", f"/plan/{plan_id}", "/ads", "/connect"):
        html = client.get(path).text  # signed in: the same pages, and Your ads and Settings themselves
        assert html.count(PILL) == 1 and html.rindex(PILL) > html.rindex("</main>"), path


def test_the_pill_is_absent_while_support_is_off(client, resend):
    for path in ("/", "/legal/management-agreement", "/connect"):
        html = client.get(path).text
        assert PILL not in html and "/static/help.js" not in html and "/static/help.css" not in html, path


def test_the_pill_is_on_none_of_the_excluded_pages(client, conn, google, support_on, monkeypatch, pages_client):  # noqa: F811 - fixture used by name
    """H2, §3.1: never on an owner's hosted page (their customers read it), a preview, the admin pages or the
    assistants' docs."""
    assert PILL not in client.get("/connect/docs").text
    missing = client.get("/p/nope")
    assert missing.status_code == 404 and PILL not in missing.text
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    hosted = pages_client.get("/")
    assert hosted.status_code == 200 and PILL not in hosted.text and "/static/help.js" not in hosted.text
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", "founder@example.com")
    google.email = "founder@example.com"
    sign_in(client, "/ops/review")
    admin = client.get("/ops/review")
    assert admin.status_code == 200 and PILL not in admin.text


def test_the_pill_is_absent_on_the_admin_and_eval_sign_in_screens(client, support_on, monkeypatch):
    """Task 3 review (T1): /ops/review and /ops/eval render the shared signin.html too, but as admin pages §3.1
    excludes them, whoever is asking -- the plan's remark that they count as "the sign-in page" is overruled. A
    plain visitor sign-in (/connect, Your ads' /ads) still shows it (support_on alone, no admin env needed there)."""
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", "founder@example.com")
    review = client.get("/ops/review")
    assert review.status_code == 200 and PILL not in review.text and "/static/help.js" not in review.text
    monkeypatch.setenv("AGENT_EVAL_GRADERS", "founder@example.com")
    ev = client.get("/ops/eval")
    assert ev.status_code == 200 and PILL not in ev.text and "/static/help.js" not in ev.text
    connect = client.get("/connect")
    assert PILL in connect.text and "/static/help.js" in connect.text


def test_the_words_are_the_mockup_s_and_owner_words():
    html = _partial()
    text = re.sub(r"<[^>]+>", " ", re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL))
    for words in WORDS:
        assert words in text, words
    assert JARGON.search(text) is None and ELSEWHERE.search(text) is None
    assert 'aria-expanded="false"' in html and 'aria-controls="help-card"' in html
    assert 'role="region"' in html and 'aria-labelledby="help-title"' in html
    assert 'maxlength="2000"' in html and 'type="email"' in html
    assert env.get_template("_help.html").render(help=False).strip() == ""


def test_the_script_changes_text_through_the_dom_and_handles_the_keyboard(client, support_on):
    js = client.get("/static/help.js").text
    assert "innerHTML" not in js and "eval(" not in js and "window.Admiry" not in js
    assert '"/api/csrf"' in js and '"/api/help"' in js
    assert 'sentText.textContent = "A person at Admiry will write back to " + r.sent_to + "."' in js
    assert 'e.key === "Escape"' in js and "pill.focus()" in js and "text.focus()" in js
    assert 'pill.setAttribute("aria-expanded", "true")' in js and 'pill.setAttribute("aria-expanded", "false")' in js
    assert "emailRow.hidden = signedIn" in js and "email.required = !signedIn" in js
    assert "location.pathname" in js and "plan_id" in js


def test_the_script_never_shows_a_raw_detail_list_on_a_422(client, support_on):
    """A 422's `detail` is a list of FastAPI validation errors, not a sentence; showing it as-is would render
    "[object Object]" in the card. Only a string `detail` may reach the card; anything else falls back to FAILED."""
    js = client.get("/static/help.js").text
    assert 'typeof r.detail === "string" ? r.detail : null' in js


# what help.css may style: the button's own (#help…, .help…) and the homepage's dock reached through its body class
OWN = re.compile(r"^(?:#help|\.help|body\.talking \.help)")


def test_help_css_reaches_no_page_but_its_own_and_sits_where_the_mockup_says(client, support_on):
    css = client.get("/static/help.css").text
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    selectors = [
        s.strip() for m in re.finditer(r"([^{}]+)\{[^{}]*\}", bare) for s in re.split(r",(?![^()]*\))", m.group(1))
    ]
    assert [s for s in selectors if not OWN.match(s)] == []
    pill = rule(css, ".help-pill")
    assert "position: fixed" in pill and "right: 24px" in pill and "bottom: 24px" in pill and "height: 44px" in pill
    phone = phone_block(css)
    assert "bottom: calc(84px + env(safe-area-inset-bottom))" in rule(phone, "body.talking .help-pill")
    card = rule(phone, ".help-card")
    assert "bottom: 0" in card and "border-radius: 24px 24px 0 0" in card
    assert "display: none" in rule(phone, ".help.open .help-pill")


def test_the_static_files_are_served_small(client):
    js, css = client.get("/static/help.js"), client.get("/static/help.css")
    assert js.status_code == 200 and css.status_code == 200
    assert js.text.count("\n") < 101 and css.text.count("\n") < 81


def test_the_privacy_page_names_the_page_the_browser_and_the_last_messages():
    """Spec §3.5: the website's privacy page (website/app/privacy/page.tsx, released with the website) says what a
    report carries beside the words."""
    page = Path(__file__).resolve().parents[2] / "website" / "app" / "privacy" / "page.tsx"
    assert "the page you were on, your browser and your last few messages" in re.sub(r"\s+", " ", page.read_text())
