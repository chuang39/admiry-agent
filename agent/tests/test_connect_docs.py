import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent import audit, auth, proposals
from agent.connect import ERRORS
from agent.mcp_app import REGISTRY
from agent.surface import gate, supporting
from agent.surface.plan import ALREADY_APPROVED, NO_LAUNCH_REQUEST
from agent.surface.registry import SURFACE
from agent.tests.conftest import JARGON

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module", ["agent.connect", "agent.web", "agent.pages"])
def test_the_module_imports_first_in_a_fresh_interpreter(module):
    """Final wave 9: connect.py reads the surface's registry only when it lists the tools, so importing it first -- or
    web or pages, which import it -- no longer meets mcp_app's own last import (surface.told) asking a half-imported
    connect for its names. A fresh interpreter: this process has long imported everything."""
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"], cwd=REPO, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr


def test_docs_page_has_endpoint_prompt_and_every_tool(client):
    r = client.get("/connect/docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "http://testserver/mcp" in html
    assert (
        "Add Admiry as a connector: MCP server http://testserver/mcp. It will ask you to sign in with Google." in html
    )
    assert "Authorization: Bearer" in html
    for meta in SURFACE:
        assert f"<code>{meta.name}</code>" in html
    assert "retry_after" in html and "needs_verification" in html and "proposed" in html
    # the policies on the apex, not www, which only 301s there (spec 2026-09-29 §4)
    assert 'href="https://admiry.ai/terms"' in html and 'href="https://admiry.ai/privacy"' in html


def test_llms_txt_is_plain_text_with_the_same_facts(client):
    r = client.get("/connect/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "MCP: http://testserver/mcp" in r.text
    assert "Read http://testserver/connect/docs first." in r.text
    lines = r.text.splitlines()
    for meta in SURFACE:
        bullet = next(line for line in lines if line.startswith(f"- {meta.name} [{meta.cls}"))
        assert bullet.endswith(")")  # summary and approval text stay on the bullet's own line


def test_tools_json_lists_the_public_tools_with_schemas_and_classes(client):
    r = client.get("/connect/tools.json")
    body = r.json()
    assert body["mcp_url"] == "http://testserver/mcp" and body["version"] == gate.SURFACE_VERSION
    by_name = {t["name"]: t for t in body["tools"]}
    assert len(by_name) == 27 and "whoami" not in by_name and "start" not in by_name
    assert by_name["get_site_templates"]["class"] == "RO" and by_name["get_site_templates"]["scope"] == "read"
    assert by_name["get_marketing_state"]["class"] == "RO" and by_name["get_marketing_state"]["approval"] == "none"
    assert by_name["launch_marketing_plan"]["class"] == "P" and by_name["launch_marketing_plan"]["approval"] == "code"
    assert by_name["resume_campaign"]["approval"] == "assistant" and by_name["get_leads"]["scope"] == "leads"
    assert by_name["update_lead"]["input_schema"]["required"] == ["lead_id", "status", "idempotency_key"]
    assert all("\n" not in t["summary"] and t["summary"].endswith(".") for t in body["tools"])
    assert json.dumps(body)  # JSON-native throughout


def test_docs_never_use_ad_tech_jargon(client):
    text = client.get("/connect/docs").text + client.get("/connect/llms.txt").text
    # "margin" is on the constraint list too, but the page's stylesheet uses it; prose is checked by eye
    assert JARGON.findall(text) == []
    assert JARGON.search("Campaign keywords") and not JARGON.search("instructions, structure")  # the regex itself


def test_docs_and_llms_txt_teach_the_order_the_version_and_what_to_save(client):
    txt = client.get("/connect/llms.txt").text
    assert (
        txt.index("- get_marketing_state [")
        < txt.index("- create_marketing_plan [")
        < txt.index("- update_marketing_plan [")
        < txt.index("- get_marketing_plan [")
        < txt.index("- launch_marketing_plan [")
    )  # the order an assistant calls them
    html = client.get("/connect/docs").text
    for page in (txt, html):
        assert "/connect/skill.md" in page and gate.SURFACE_VERSION in page
        assert "surface_version" in page and "Plan, launch, run and report on advertising" in page
        assert "(no spend)" not in page  # resume_campaign asks the owner first because it spends
        # a launch also runs when the owner approves on their own page, with no call of yours after it
        assert "(on their page, or by your calling again with their approval)" in page
    assert "{data, say, next}" in html


def test_the_docs_name_no_tool_that_left(client):
    text = client.get("/connect/docs").text + client.get("/connect/llms.txt").text
    # the names assistants knew, and the commands behind the public tools that never were a tool of their own
    behind = {m.name for m in REGISTRY} - {m.name for m in SURFACE}
    left = sorted({*gate.RETIRED, *gate.GONE, *behind})
    not_english = [old for old in left if "_" in old or old == "whoami"]
    # three of update_landing_page's actions share their names with the commands they run (A3p-5 batch 2, plan Q6-Q8;
    # A3p-6 batch 2, plan Q15: build_site): words assistants pass, so the docs name them -- quoted, as the action ("…"
    # in llms.txt, &#34;…&#34; in the HTML), never bare as a tool of their own
    shared = sorted(set(supporting.PAGE_ACTIONS) & behind)
    assert shared == ["build_site", "request_install_help", "send_install_steps"]
    quote = r'(?:"|&#34;|&quot;)'
    as_actions = rf"{quote}(?:{'|'.join(shared)}){quote}"
    named = r"(?<![A-Za-z_])(" + "|".join(not_english) + r")(?![A-Za-z_])"
    assert re.findall(named, re.sub(as_actions, "", text)) == []
    # the quoted action is let through, the same name bare is still found
    bare = re.sub(as_actions, "", '"send_install_steps" &#34;request_install_help&#34; <code>send_install_steps</code>')
    assert re.findall(named, bare) == ["send_install_steps"]
    assert "start → answer" not in text
    # the site's own command behind the page tool's view is no tool of its own either (it is `get_landing_page`'s view
    # "site"); the looks' read tool, published as it is, is one
    assert "get_site" in left and "get_site_templates" not in left
    # a name that is also an English word (start, answer, pause, ...) is a name when it stands alone in code, opens a
    # tool's line in llms.txt or comes right before an arrow; "start over" or "(pause, resume, connect)" is English
    plain = "|".join(old for old in left if old not in not_english)
    as_a_name = rf"<code>(?:{plain})</code>|^- (?:{plain}) \[|(?<![A-Za-z_])(?:{plain})\s*→"
    assert re.findall(as_a_name, text, re.MULTILINE) == []
    # the pattern itself: it finds each of the three shapes in a copy of the real text, and leaves English alone
    poisoned = text + "<code>start</code>\n- pause [W]\nrecommend → x"
    assert len(re.findall(as_a_name, poisoned, re.MULTILINE)) == 3
    assert re.findall(as_a_name, "start over; answered; (pause, resume, connect)", re.MULTILINE) == []


def test_the_error_sentences_the_docs_copy_are_the_codes_own():
    """The docs' error list copies sentences the code says, and nothing else ties the two: a sentence the code
    rewords would leave the docs listing one that no call returns. Each is the code's own (the launch's: its first
    clause, the one before its colon; the daily limit's with N for its number)."""
    listed = [left for left, _ in ERRORS]
    daily = proposals.DAILY_LIMIT.format(cap="N")
    for sentence in (
        proposals.WRONG_CODE,
        proposals.LOCKED,
        audit.KEY_REUSED,
        NO_LAUNCH_REQUEST.split(":")[0],
        ALREADY_APPROVED,
        daily,
        auth.BUSINESS_EXPIRED,
    ):
        assert sentence in listed, sentence
    # only a command says this one (a launch asks through approve_plan, and with a code passes it on): no public tool
    # returns it, so the docs do not list it
    assert not [left for left in listed if "this change spends money" in left]
    # what each means (open item A5; final review C, minor 10): the numbers in them are the code's own
    meaning = dict(ERRORS)
    assert meaning[daily] == (
        "a page was published or taken down on the owner's word 20 times today; tell the owner, it works again tomorrow"
    )
    assert f"{proposals.DAILY_CONFIRM_CAP} times today" in meaning[daily]
    assert meaning[ALREADY_APPROVED] == "the owner approved on their page; get_campaigns says where the ads stand"
    # not "start over with create_marketing_plan": the same key is refused on every call; and only a business that is
    # no longer active says it -- a never-claimed draft that lapsed, or one a hand-off folded into the owner's (a
    # lapsed unverified claim goes back to draft and stays active)
    assert meaning[auth.BUSINESS_EXPIRED] == (
        "the 24-hour draft this key belonged to lapsed, or was folded into the business the owner signed in for, and "
        "the key with it: the owner connects you again through their sign-in"
    )
    # an empty key and a key over the limit are each refused in a sentence of their own, never in this row's (re-review
    # C, M-F): the meaning says so
    assert meaning[audit.KEY_REUSED] == (
        "each distinct call needs its own key: only the same call sent again within a day is answered with its first "
        "answer, and not once things changed since (a pause that was lifted, a plan version replaced); an empty key and "
        "one over 200 characters are refused in their own sentences"
    )
    assert f"over {auth.MAX_IDEMPOTENCY_KEY} characters" in meaning[audit.KEY_REUSED]
    assert meaning[NO_LAUNCH_REQUEST.split(":")[0]] == (
        "no launch was asked for on this plan, or the request expired: ask to launch first"
    )


def test_the_tool_list_builds_in_a_process_that_imported_connect_first():
    """`agent.connect` may be the first module of the package a process imports (a script, a test collected alone):
    asking it for the tools must load the server and the surface in the order that works."""
    code = "import agent.connect as c; names = [t['name'] for t in c.tool_docs()]; print(len(names), names[0])"
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["27", "get_marketing_state"]


def test_docs_teach_oauth_and_google_sign_in(client):
    html = client.get("/connect/docs").text
    txt = client.get("/connect/llms.txt").text
    assert "oauth-protected-resource" in html and "oauth-protected-resource" in txt
    assert "sign in with Google" in html and "sign in with Google" in txt


def test_docs_lead_with_oauth_and_tell_the_assistant_not_to_ask_for_a_key(client):
    """Founder's Muse round (2026-09-28): with "OAuth or a key" Muse built the integration and asked the owner
    for an ak_ key; the docs now spell out the OAuth steps and say a key is the fallback for a client that
    cannot open a browser."""
    for path in ("/connect/llms.txt", "/connect/docs"):
        text = client.get(path).text
        assert "Do not ask the owner for a key" in text, path
        assert "resource_metadata" in text and "PKCE S256" in text and "refresh token" in text, path
        assert text.index("Do not ask the owner for a key") < text.index("Bearer ak_"), path  # OAuth first
    tools = client.get("/connect/tools.json").json()
    assert tools["auth"].startswith("OAuth 2.1") and "fallback only" in tools["auth"]
    assert "Do not ask me for a key" in client.get("/connect/llms.txt").text  # the Muse paste prompt


def test_the_docs_name_the_site_the_action_the_view_and_the_tool(client):
    """A3p-6 batch 2 Task 6: the connector docs (the page, and llms.txt in plain text) teach the website Admiry builds
    for a home-services business or a dental practice: the looks' tool, the page tool's action -- quoted, as the action,
    never a tool of its own -- and its view, and that a photo reaches Admiry only through the upload link. The page's
    tool rows carry the full descriptions: the action's, the view's, and the looks' own."""
    html = " ".join(client.get("/connect/docs").text.split())  # the page's paragraphs wrap
    txt = client.get("/connect/llms.txt").text
    said = "the page the ads use can be a whole website Admiry builds"
    assert said in html and said in txt
    assert (
        '<code>get_site_templates</code> shows its three looks, <code>update_landing_page</code> with <code>action</code> "build_site"'
        in html
    )
    assert (
        '(or "write") asks its questions one at a time (the look first, then the facts it lacks) and builds it' in html
    )
    assert '<code>get_landing_page</code> with <code>view</code> "site" shows where it stands' in html
    assert 'get_site_templates shows its three looks, update_landing_page with action "build_site"' in txt
    assert (
        '(or "write") asks its questions one at a time (the look first, then the facts it lacks) and builds it' in txt
    )
    assert 'get_landing_page with view "site" shows where it stands' in txt
    for page in (html, txt):
        assert "A photo reaches Admiry only through the upload link the answer carries." in page
    assert 'and on every "build_site" that builds, whatever the key.' in html  # a new preview each time, as write's
    assert "- get_site_templates [RO · read] — " in txt
    assert "<td><code>get_site_templates</code></td>" in html
    row = html[html.index("<td><code>update_landing_page</code></td>") :]
    assert "&#34;build_site&#34;: for a home-services business" in row[: row.index("</tr>")]
    row = html[html.index("<td><code>get_landing_page</code></td>") :]
    assert "view &#34;site&#34;: the website Admiry builds" in row[: row.index("</tr>")]
