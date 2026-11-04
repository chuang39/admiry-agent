"""The plan card as an MCP Apps view (spec 2026-09-26 §5.2 probe, A1b-4d)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent import plans
from agent.channels import registry
from agent.tests.conftest import rpc
from agent.tests.fixtures import FakeAdapter, business_with_key

VIEW = "ui://admiry/plan"
VIEW_FILE = Path(__file__).parents[1] / "templates" / "plan_view.html"
CHANNELS = "ui://admiry/channels"
CHANNELS_FILE = Path(__file__).parents[1] / "templates" / "channels_view.html"
# The view's own script under Node with a stub DOM: a tool result is posted to it as the host would, and every text
# it put on the page is printed, one element per line.
HARNESS = r"""
const html = require("fs").readFileSync(process.argv[1], "utf8");
const script = html.split("<script>")[1].split("</script>")[0];
const el = (tag) => ({ tag, className: "", text: "", children: [], style: {},
  set textContent(v) { this.text = String(v); }, get textContent() { return this.text; },
  appendChild(c) { this.children.push(c); return c; }, removeChild(c) { this.children = this.children.filter((x) => x !== c); },
  get firstChild() { return this.children[0] || null; }, setAttribute() {}, addEventListener() {} });
const nodes = { body: el("div"), meta: el("span"), waiting: el("p") };
const on = {};
global.window = { parent: { postMessage() {} }, addEventListener: (type, f) => { on[type] = f; } };
global.document = { createElement: el, getElementById: (id) => nodes[id],
  documentElement: { style: {}, setAttribute() {}, getBoundingClientRect: () => ({ height: 100 }) } };
eval(script);
on.message({ source: window.parent,
  data: { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: JSON.parse(process.argv[2]) } });
const lines = [];
const walk = (n) => {  // a picture as "img <address> | <its words>" (M1 Task 7)
  if (n.tag === "img") lines.push("img " + n.src + " | " + n.alt);
  if (n.text) lines.push(n.text);
  n.children.forEach(walk);
};
walk(nodes.body);
console.log(lines.join("\n"));
"""


def test_the_plan_tools_point_at_the_card_and_get_campaigns_at_the_channels_card(client, conn, strict_surface):
    key = business_with_key(conn)
    tools = rpc(client, "tools/list", key=key)["result"]["tools"]
    with_view = {t["name"]: t["_meta"]["ui"] for t in tools if (t.get("_meta") or {}).get("ui")}
    assert with_view == {
        name: {"resourceUri": VIEW}
        for name in ("update_marketing_plan", "get_marketing_plan", "update_marketing_budget")
    } | {"get_campaigns": {"resourceUri": CHANNELS}}
    # the tools' descriptions did not change shape: annotations still say what class each tool is
    assert next(t for t in tools if t["name"] == "get_campaigns")["annotations"]["readOnlyHint"] is True


def test_the_card_is_a_single_file_html_resource(client, conn):
    key = business_with_key(conn)
    resources = {r["uri"]: r for r in rpc(client, "resources/list", key=key)["result"]["resources"]}
    assert {VIEW, CHANNELS} <= set(resources)
    assert resources[VIEW]["mimeType"] == "text/html;profile=mcp-app"
    # M1 Task 7: the plan's picture comes from the web host (conftest's base), the one origin it declares to the host
    assert resources[VIEW]["_meta"]["ui"] == {"prefersBorder": True, "csp": {"resourceDomains": ["http://testserver"]}}
    content = rpc(client, "resources/read", {"uri": VIEW}, key=key)["result"]["contents"][0]
    assert content["mimeType"] == "text/html;profile=mcp-app"
    html = content["text"]
    # nothing but the page itself in its markup: no external script, style, image or font (the picture's address is the
    # tool result's, set by the script)
    assert not re.search(r"""(src|href)=["']https?:""", html)
    assert "<script" in html and "<style" in html
    # it speaks the extension's protocol itself (no bundle): the handshake and the result notification
    assert '"clientInfo"' not in html and "appInfo" in html  # the handshake names the app, as the extension requires
    for token in (
        "ui/initialize",
        "ui/notifications/initialized",
        "ui/notifications/tool-result",
        "ui/notifications/size-changed",
        "ui/open-link",
    ):
        assert token in html, token
    # it renders the cards without innerHTML, and it can only show what the tool answered
    assert "innerHTML" not in html and "cards" in html and "plan_url" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="drives the view's script with node")
def test_the_card_shows_every_funded_row_the_levels_and_the_later_lines(conn, monkeypatch, pictures):
    """M0 Task 5 (spec 2026-09-29 §7.2): a roofer's plan with Bing shipped funds three channels; the card in the chat
    shows each with its amount, reason and who runs it, each level's daily amount, and every later line."""
    from agent.tests.test_plans_channels import NB_WHY, home_plan

    monkeypatch.setitem(registry.CHANNELS, "microsoft_search", FakeAdapter("microsoft_search"))
    monkeypatch.setenv("AGENT_EXEC_MICROSOFT_SEARCH", "validate")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    sections, _ = home_plan(conn, prepaid=True)  # a prepaid business: the one kind whose plan can have NewsBreak
    cards = plans.cards({"id": "pl_1", "business_id": "biz_1", "business_name": "Summit Roofing", "sections": sections})
    result = {"structuredContent": {"data": {"cards": cards, "version": 1, "status": "draft"}}}
    done = subprocess.run(
        ["node", "-e", HARNESS, str(VIEW_FILE), json.dumps(result)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    text = done.stdout
    for line in (
        "Google Search · $600 a month",
        "NewsBreak · $300 a month",
        "Bing Search · $100 a month",
        NB_WHY + " Admiry runs it from your balance.",
        (
            "Google Search gets most of it because people there are already looking for Roofing near you. Admiry runs "
            "it from your balance."
        ),
        "Recommended $33/day",
        "Facebook & Instagram — Not in this plan yet; coming to Admiry.",
        "Yelp — Not now: Yelp opens its ads only to large partners.",
        "TikTok — Worth a test after 30 days of booking data. Not now.",
    ):
        assert line in text.splitlines(), (line, text)


@pytest.mark.skipif(shutil.which("node") is None, reason="drives the view's script with node")
def test_the_card_in_the_chat_shows_the_plans_picture_with_its_words_and_its_line(conn, monkeypatch, tmp_path):
    """M1 Task 7 (plan P7): the same picture as the homepage's ad preview -- one `<img>` with the manifest's words for a
    screen reader, and the line under it; once the version's picture left the set, only the line that says so."""
    from agent.tests.test_home_page import _newsbreak_cards

    cards = _newsbreak_cards(conn, monkeypatch, tmp_path)
    shown = "NewsBreak shows this picture with your ad; approving the plan approves it"
    drawn = _draw(VIEW_FILE, {"structuredContent": {"data": {"cards": cards, "version": 1, "status": "draft"}}})
    assert "img http://testserver/static/creative/home_services/1.jpg | A solid green test picture" in drawn
    assert drawn.index(
        "img http://testserver/static/creative/home_services/1.jpg | A solid green test picture"
    ) + 1 == (drawn.index(shown))
    waiting = plans.IMAGE_GONE.format(label="NewsBreak")  # the version's picture left the set (fix round 1)
    for image in (None, {"url": "javascript:alert(1)", "alt": "x"}):  # none, and an address that is not the web's
        cards["ads_preview"].update(image=image, image_note=waiting)
        drawn = _draw(VIEW_FILE, {"structuredContent": {"data": {"cards": cards}}})
        assert waiting in drawn and not [line for line in drawn if line.startswith("img ")]
    del cards["ads_preview"]["image"], cards["ads_preview"]["image_note"]  # a plan whose channels show no picture
    drawn = _draw(VIEW_FILE, {"structuredContent": {"data": {"cards": cards}}})
    assert waiting not in drawn and not [line for line in drawn if line.startswith("img ")]


# ---------------------------------------------------------------- the channels card (M0 Task 6, spec 2026-09-29 §7.2)


def _draw(path: Path, result: dict) -> list[str]:
    """Every text a view put on the page for this tool result, one element per line."""
    done = subprocess.run(
        ["node", "-e", HARNESS, str(path), json.dumps(result)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def test_the_channels_card_is_a_single_file_html_resource(client, conn):
    key = business_with_key(conn)
    resources = {r["uri"]: r for r in rpc(client, "resources/list", key=key)["result"]["resources"]}
    assert resources[CHANNELS]["mimeType"] == "text/html;profile=mcp-app"
    assert resources[CHANNELS]["_meta"]["ui"] == {"prefersBorder": True}
    html = rpc(client, "resources/read", {"uri": CHANNELS}, key=key)["result"]["contents"][0]["text"]
    assert html == CHANNELS_FILE.read_text()
    assert not re.search(r"""(src|href)=["']https?:""", html) and "@font-face" not in html  # nothing fetched
    assert "<script" in html and "<style" in html and "innerHTML" not in html and "fetch(" not in html
    for token in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result", "appInfo"):
        assert token in html, token
    assert "data.channels" in html or "d.channels" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="drives the view's script with node")
def test_the_channels_card_in_the_chat_draws_every_row_and_the_footer():
    """The view draws get_channel_status's answer the way the homepage's card does (the mock's words)."""
    from agent import state
    from agent.tests.test_home_page import _fixture

    card = state.MarketingState(**_fixture()).cards()["channels"]
    result = {"structuredContent": {"data": {"channels": card["rows"], "footer": card["footer"]}, "say": "x"}}
    lines = _draw(CHANNELS_FILE, result)
    for line in (
        "Where your ads run",
        "Google Search",
        "Live",
        "$38 of $40 today · 2 calls, 1 form this week",
        "NewsBreak",
        "Waiting for funds",
        "Bing Search",
        "Paused by you on Tuesday",
        "1 call this week",
        "Month to date $842 of $1,816 · next report Monday",
    ):
        assert line in lines, (line, lines)
    assert lines.index("Google Search") < lines.index("NewsBreak") < lines.index("Bing Search")


@pytest.mark.skipif(shutil.which("node") is None, reason="drives the view's script with node")
def test_the_channels_card_in_the_chat_says_so_when_nothing_runs_yet():
    empty = {"structuredContent": {"data": {"channels": [], "footer": {}}, "say": "You have no ads yet."}}
    assert _draw(CHANNELS_FILE, empty) == ["You have no ads yet."]
    refused = {"structuredContent": {"say": "this key lacks the 'read' scope"}}
    assert _draw(CHANNELS_FILE, refused) == ["this key lacks the 'read' scope"]
