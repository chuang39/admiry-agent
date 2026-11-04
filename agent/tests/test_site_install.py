"""A3p-5 batch 2 Task 2: the plan's "Your website" row (site_install.row, plan Q5) and the four ways (WAYS, Q6), and
the places they show -- the plan card's setup, /plan/{id}, the plan view (ui://admiry/plan), `get_plan`'s `say`, the
status's `website` block and the kit e-mail's YOUR WEBSITE section. Ruling R2-b: the row judges the page the ads use,
never a site the ads do not use; R1-f: no website of its own, no row and no install part.

Task 3: two of the ways -- the web person's e-mail (send_install_steps, plan Q7) and Admiry's team (request_install_help,
Q8) -- the data they keep (Q10) and no password anywhere (W12). The final review: the help ticket carries the page and
the code to paste (ruling F-2), and the help reply's contact stays out of the ledger (F-4)."""

import hashlib
import json
import re
import shutil
from html import unescape
from pathlib import Path

import httpx
import pytest

from agent import business, config, crawl, envelope, jobs, keys, landing, mail, plans, site_install, tick
from agent.surface import actions, registry
from agent.tests.conftest import JARGON, TEST_DB_URL, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_landing import _plan, fake  # noqa: F401 - fake is a pytest fixture used by name
from agent.tests.test_mcp_view import VIEW_FILE, _draw
from agent.tests.test_site_platform import IN_A_FRAME, _read_as

SITE = "https://brightsmile.example"
ELSEWHERE = "https://offer.example/smile"  # an owner page on another host than the website (ruling R2-b)
HOSTED = "https://bright-smile.business.test/"
Q6 = [
    {"id": "install_self", "label": "I'll add it", "tool": "get_landing_page", "args": {"view": "kit"}},
    {
        "id": "install_send",
        "label": "Send it to my web person",
        "tool": "update_landing_page",
        "args": {"action": "send_install_steps"},
    },
    {
        "id": "install_help",
        "label": "Have Admiry's team do it",
        "tool": "update_landing_page",
        "args": {"action": "request_install_help"},
    },
    {
        "id": "admiry_page",
        "label": "Use an Admiry page instead",
        "tool": "update_landing_page",
        "args": {"action": "write"},
    },
]
# the ways offered while support is off -- every test's setting but those with `support_on` -- without Admiry's team
# (ruling R3-f)
OFFERED = [way for way in Q6 if way["id"] != "install_help"]
NEEDS_CODE = (
    "Your website needs Admiry's code before the ads can run. It lets Google count calls and forms from your site."
)
FOUND = "Admiry's code is on your site."
ADMIRY_PAGE = "Your ads use your Admiry page; your website stays as it is."


def _business(conn, **knowledge) -> str:
    business_with_key(conn)
    business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental", **knowledge}, source="owner")
    return "biz_1"


def _checked(conn, page_id: str, *, found: bool) -> None:
    """A saved check whose tracking rule found Admiry's code on the page, or did not (the rest is test_landing's)."""
    url = landing.get(conn, page_id)["url"]
    rule = {
        "id": "tracking",
        "title": "Admiry's code is on the page",
        "ok": found,
        "detail": "d",
        "fix": None if found else landing.TRACKING_FIX,
    }
    report = {"ok": found, "url": url, "rules": [rule], "fetched": {"status": 200, "bytes": 1, "seconds": 0.1}}
    landing.save_check(conn, page_id, report)


def _hosted(conn, biz: str) -> None:
    """The business's one page row (H25) becomes a page Admiry hosts, as a publish leaves it."""
    page = landing.current(conn, biz) or landing.ensure_page_for_kit(conn, biz)
    conn.execute(
        "UPDATE page SET source = 'admiry', status = 'published', hostname = %s, url = %s WHERE id = %s",
        ("bright-smile.business.test", HOSTED, page["id"]),
    )


def _no_website(conn, biz: str) -> None:
    latest = business.latest_knowledge(conn, biz)[1]
    kept = {k: v for k, v in latest.items() if k not in ("website", "website_unread", "website_platform")}
    business.add_knowledge(conn, biz, kept, source="owner")


# ---------------------------------------------------------------- the four ways (Q6, Q3)


def test_the_four_ways_are_q6s_actions_and_the_fourth_comes_first_where_the_builder_frames_code(support_on):
    assert site_install.WAYS == Q6
    ways = site_install.ways_for("wix")
    assert ways == Q6
    ways[0]["args"]["view"] = "status"  # a copy a caller may change
    assert site_install.WAYS == Q6
    # GoDaddy keeps added code in a frame (Task 1's `in_frame`): the row offers the fourth way first (Q3)
    framed = site_install.ways_for("godaddy")
    assert [w["id"] for w in framed] == ["admiry_page", "install_self", "install_send", "install_help"]
    assert [w["id"] for w in site_install.ways_for(None)] == [w["id"] for w in Q6]  # `other`, as an unknown one
    assert not JARGON.search(" ".join(w["label"] for w in Q6))


def test_a_draft_key_signs_in_before_the_four_ways():
    """Ruling R2-e: the four ways' tools need a signed-in business, so with the sign-in actions (a draft key) `next`
    carries no way; for a signed-in one, the ways come first."""
    from agent import tools_consult

    needs = {
        "state": "needs_code",
        "say": NEEDS_CODE,
        "platform": "other",
        "steps": [],
        "ways": site_install.ways_for(None),
    }
    assert tools_consult.launch_next([], draft=True, website=needs) == (tools_consult.SIGN_IN_ACTIONS, None)
    nxt, _note = tools_consult.launch_next([], draft=False, website=needs)
    assert nxt == envelope.actions(*OFFERED, tools_consult.LINK_ACTION, tools_consult.ADJUST_ACTION)


def test_the_fourth_way_says_the_website_stays_when_admiry_writes_the_page(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    """Ruling R2-f: update_landing_page's "write" (the command create_page) leads with spec §3.1's sentence for a business
    with a website of its own; one without hears about the page as before."""
    assert site_install.ADMIRY_PAGE_WAY == "Your website stays as it is. Admiry builds a page for your ads."
    key, biz, _plan_id = _plan(client, conn, fake)
    got = call_tool(client, "create_page", {}, key=key)
    assert not got["isError"], tool_text(got)
    assert got["structuredContent"]["say"].startswith(f"{site_install.ADMIRY_PAGE_WAY} Here is the page I wrote for")
    _no_website(conn, biz)
    again = call_tool(client, "create_page", {}, key=key)
    assert not again["isError"], tool_text(again)
    assert again["structuredContent"]["say"].startswith("Here is the page I wrote for")


def test_the_sources_admiry_hosts_are_one_list():
    from agent import hosting_minimum

    assert site_install.HOSTED == hosting_minimum.HOSTED


# ---------------------------------------------------------------- the row (Q5, ruling R2-b)


def test_no_website_of_its_own_has_no_row_no_status_and_no_install_part(conn):
    """Q5 and ruling R1-f: a business with neither a website nor an address it gave has no row (the page card stands
    for it), no `website` in the status and no `install` part in its kit."""
    biz = _business(conn)
    assert site_install.row(conn, biz) is None and site_install.status(conn, biz) is None
    assert site_install.kit_part({}, "<script></script>") is None
    assert site_install.kit_part({"website_platform": "wix"}, "<script></script>") is None
    assert site_install.email_section(conn, biz) is None


def test_the_row_judges_the_page_the_ads_use(conn):
    """Q5 with ruling R2-b: a hosted page is `admiry_page`; an owner page -- on any host -- whose saved check found the
    code is `found`; otherwise `needs_code`, with the website's platform steps while the ads have no page or theirs is
    on the website, and `other`'s for an owner page somewhere else. Spec 2026-10-12 §3.5: `found` and `needs_code` name
    the page the ads use (`url`: the website until the ads have a page with an address) and the builder it is on
    (`platform_name`, None for `other`'s), for the card's line."""
    biz = _business(conn, website=SITE, website_platform="wix")
    wix, other = site_install.steps_for("wix"), site_install.steps_for(None)
    on_site = "https://www.brightsmile.example/invisalign"
    needs_wix = {
        "state": "needs_code",
        "say": NEEDS_CODE,
        "platform": "wix",
        "url": SITE,
        "platform_name": "Wix",
        "steps": wix["steps"],
        "ways": OFFERED,
    }
    needs_other = {**needs_wix, "platform": "other", "url": ELSEWHERE, "platform_name": None, "steps": other["steps"]}
    assert site_install.row(conn, biz) == needs_wix  # no page yet
    landing.ensure_page_for_kit(conn, biz)  # the kit's placeholder has no address: no page the ads use yet
    assert site_install.row(conn, biz) == needs_wix
    # an owner page on the website ("www." aside): found once its saved check found the code
    page = landing.set_owner_url(conn, biz, on_site)
    assert site_install.row(conn, biz) == {**needs_wix, "url": on_site}  # not checked yet
    _checked(conn, page["id"], found=False)
    assert site_install.row(conn, biz) == {**needs_wix, "url": on_site}
    _checked(conn, page["id"], found=True)
    assert site_install.row(conn, biz) == {"state": "found", "say": FOUND, "url": on_site, "platform_name": "Wix"}
    # the ads on an owner page on another host: that page is judged, never the website the ads do not use
    landing.set_owner_url(conn, biz, ELSEWHERE)  # the same row (H25), its old check cleared
    assert site_install.row(conn, biz) == needs_other
    _checked(conn, page["id"], found=True)
    assert site_install.row(conn, biz) == {"state": "found", "say": FOUND, "url": ELSEWHERE, "platform_name": None}
    _checked(conn, page["id"], found=False)
    assert site_install.row(conn, biz) == needs_other
    # Review Focus: the ads on a page Admiry hosts, the website elsewhere -- the row never asks for code on the website
    _hosted(conn, biz)
    assert site_install.row(conn, biz) == {"state": "admiry_page", "say": ADMIRY_PAGE}
    conn.execute("UPDATE page SET status = 'unpublished' WHERE id = %s", (page["id"],))  # taken down: still theirs
    assert site_install.row(conn, biz) == {"state": "admiry_page", "say": ADMIRY_PAGE}


def test_an_address_admiry_never_read_has_the_generic_steps_and_a_framing_builder_its_admiry_page_first(conn):
    """Q13: the owner's site is `website`, else the address it gave that could not be read (`website_unread`), which
    has no platform: `other`'s steps. A builder that keeps added code in a frame (GoDaddy) offers the fourth way first
    (Q3)."""
    biz = _business(conn, website_unread=SITE)
    row = site_install.row(conn, biz)
    assert row["state"] == "needs_code" and row["platform"] == "other" and row["platform_name"] is None
    assert row["steps"] == site_install.STEPS["other"]["steps"] and row["ways"] == OFFERED and row["url"] == SITE
    business.add_knowledge(
        conn, biz, {"name": "Bright Smile Dental", "website": SITE, "website_platform": "godaddy"}, source="owner"
    )
    row = site_install.row(conn, biz)
    assert row["platform"] == "godaddy" and row["steps"] == [IN_A_FRAME]
    assert row["platform_name"] == "GoDaddy Websites + Marketing"
    assert [w["id"] for w in row["ways"]] == ["admiry_page", "install_self", "install_send"]  # support off: R3-f


def test_the_kits_install_part_follows_the_page_the_ads_use():
    """Ruling R2-b, for the steps `install_self` points at (the kit's `install`): the website's platform, unless the ads
    use an owner page somewhere else, whose builder Admiry never read -- `other`'s. A page Admiry hosts already carries
    the code: the owner's own site is still the website."""
    k = {"website": SITE, "website_platform": "wix"}
    on = {"source": "owner", "url": "https://brightsmile.example/x"}
    for page, platform in (
        (None, "wix"),
        ({"source": "owner", "url": ""}, "wix"),
        (on, "wix"),
        ({"source": "owner", "url": ELSEWHERE}, "other"),
        ({"source": "admiry", "url": HOSTED}, "wix"),
    ):
        assert site_install.kit_part(k, "S", page)["platform"] == platform, page
    assert site_install.kit_part(k, "S", on) == {**site_install.install_part("wix", "S")}
    assert site_install.kit_part({"website_unread": SITE}, "S")["platform"] == "other"


# ---------------------------------------------------------------- where the row shows (Q5)


def test_the_plan_card_puts_the_row_in_its_setup(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    _key, biz, plan_id = _plan(client, conn, fake)
    plan = plans.get(conn, biz, plan_id)
    row = site_install.row(conn, biz)
    assert row["state"] == "needs_code"
    assert plans.cards(plan, website=row)["plan"]["setup"]["website"] == row
    assert plans.cards(plan)["plan"]["setup"]["website"] is None  # a caller with none to give: pure, as ever


def _states(conn, biz: str):
    """The plan's row in each of its four states, in an order each can be reached from the one before: needs code (the
    website, no page), found (an owner page on the website whose check found the code), an Admiry page (the same row
    hosted), no website (a knowledge without one)."""
    yield "needs_code", NEEDS_CODE
    page = landing.set_owner_url(conn, biz, f"{SITE}/invisalign")
    _checked(conn, page["id"], found=True)
    yield "found", FOUND
    _hosted(conn, biz)
    yield "admiry_page", ADMIRY_PAGE
    _no_website(conn, biz)
    yield None, None


def test_the_plan_page_and_get_plans_say_carry_the_rows_sentence(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    """Q5: /plan/{id} shows the row's sentence; `get_plan`'s `say` carries it, and a read's `next` stays empty (ruling
    R2-c: get_marketing_plan's description says so). No website: no row anywhere."""
    key, biz, plan_id = _plan(client, conn, fake)
    for state, sentence in _states(conn, biz):
        page = unescape(client.get(f"/plan/{plan_id}").text)
        got = call_tool(client, "get_plan", {}, key=key)
        assert not got["isError"], tool_text(got)
        env = got["structuredContent"]
        shown = env["data"]["cards"]["plan"]["setup"]["website"]
        assert env["next"] == envelope.NONE, state
        if state is None:
            assert shown is None
            assert not any(s in page or s in env["say"] for s in (NEEDS_CODE, FOUND, ADMIRY_PAGE))
            continue
        assert shown["state"] == state and sentence in page, state
        assert env["say"].endswith(f"{sentence} Details and approval: {plans.plan_url(plan_id)}"), env["say"]
        assert not JARGON.search(env["say"])


@pytest.mark.skipif(shutil.which("node") is None, reason="drives the view's script with node")
def test_the_plan_view_shows_the_rows_sentence(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    """Q5: the plan card an assistant renders in the chat (ui://admiry/plan) shows the row's sentence -- the four ways
    are the tool answer's `next`, not buttons in the card."""
    key, biz, _plan_id = _plan(client, conn, fake)
    for state, sentence in _states(conn, biz):
        drawn = _draw(VIEW_FILE, call_tool(client, "get_plan", {}, key=key))
        if state is None:
            assert not any(s in drawn for s in (NEEDS_CODE, FOUND, ADMIRY_PAGE))
        else:
            assert sentence in drawn, (state, drawn)
            assert not any(w["label"] in drawn for w in Q6)


def test_the_status_carries_the_website_block(client, conn, fake):  # noqa: F811 - the fixture shadows the imported name
    """Spec §4: get_landing_page's status gains `website: {url, platform, code, checked_at, way}` -- the page the ads
    use is what `code` and `checked_at` say (ruling R2-b, R2-c); `way` is "admiry_page" for a page Admiry hosts (ruling
    R0-c; Task 3 adds the other ways). None without a website of its own; there too before any page is set."""
    key, biz, _plan_id = _plan(client, conn, fake)

    def website() -> dict | None:
        got = call_tool(client, "get_page", {}, key=key)
        assert not got["isError"], tool_text(got)
        return data(got)["website"]

    # the walk read the site, whose platform could not be told: the generic steps' platform
    assert website() == {"url": SITE, "platform": "other", "code": "missing", "checked_at": None, "way": None}
    page = landing.set_owner_url(conn, biz, ELSEWHERE)
    _checked(conn, page["id"], found=True)
    found = website()
    assert found["code"] == "found" and found["checked_at"] and found["url"] == SITE and found["way"] is None
    _hosted(conn, biz)
    hosted = website()
    assert hosted["way"] == "admiry_page" and hosted["url"] == SITE
    _no_website(conn, biz)
    assert website() is None


# ---------------------------------------------------------------- the plan e-mail (rulings R0-e, R1-f)


def _section(text: str) -> list[str]:
    return text.split("\nYOUR WEBSITE\n", 1)[1].split("\nTHE WORDS THE ADS USE\n", 1)[0].splitlines()


def test_the_kit_email_carries_the_row_the_four_ways_and_the_code(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    """Spec §3.2 (ruling R0-e): the plan e-mail's YOUR WEBSITE section has the row's sentence, the four ways as links
    to the homepage's card (ruling R2-c: `{AGENT_WEB_BASE_URL}/?website=<way id>`), the platform's steps and the code;
    a row that needs nothing has its sentence alone; no website, no section (ruling R1-f)."""
    monkeypatch.setattr(crawl, "fetch_site", _read_as("squarespace"))
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    web = config.load().web_base_url
    squarespace = site_install.STEPS["squarespace"]
    text = mail.render("mail_kit.txt.j2", kit=kit, website=site_install.email_section(conn, biz))
    assert _section(text) == [
        NEEDS_CODE,
        *[f"- {w['label']}: {web}/?website={w['id']}" for w in OFFERED],  # support off: no team (R3-f)
        "",
        squarespace["requirement"],
        "On Squarespace:",
        *[f"{n}. {step}" for n, step in enumerate(squarespace["steps"], 1)],
        f"Help: {squarespace['help_url']}",
        "",
        "```",
        kit["tracking"]["snippet"],
        "```",
        "",
    ]
    assert not JARGON.search("\n".join(_section(text)))
    # GoDaddy: the Admiry page first, and its one sentence said, not numbered
    latest = business.latest_knowledge(conn, biz)[1]
    business.add_knowledge(conn, biz, {**latest, "website_platform": "godaddy"}, source="owner")
    godaddy = _section(
        mail.render("mail_kit.txt.j2", kit=landing.kit(conn, biz), website=site_install.email_section(conn, biz))
    )
    assert godaddy[1] == f"- Use an Admiry page instead: {web}/?website=admiry_page"
    assert godaddy[len(OFFERED) + 2 : len(OFFERED) + 4] == ["On GoDaddy Websites + Marketing:", IN_A_FRAME]
    # the code found on the page the ads use: the sentence alone
    page = landing.set_owner_url(conn, biz, f"{SITE}/offer")
    _checked(conn, page["id"], found=True)
    found = mail.render("mail_kit.txt.j2", kit=landing.kit(conn, biz), website=site_install.email_section(conn, biz))
    assert _section(found) == [FOUND, ""]
    _hosted(conn, biz)
    hosted = mail.render("mail_kit.txt.j2", kit=landing.kit(conn, biz), website=site_install.email_section(conn, biz))
    assert _section(hosted) == [ADMIRY_PAGE, ""]
    _no_website(conn, biz)
    kit = landing.kit(conn, biz)
    assert kit["install"] is None  # ruling R1-f
    assert "YOUR WEBSITE" not in mail.render("mail_kit.txt.j2", kit=kit, website=site_install.email_section(conn, biz))


def test_get_page_kit_emails_the_rows_section(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    """The kit e-mail get_page_kit sends carries the section (the template is given the row with the kit)."""
    key, biz, _plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
        ),
    )
    got = call_tool(client, "get_page_kit", {}, key=key)
    assert not got["isError"], tool_text(got)
    text = json.loads(sent[0].read())["text"]
    lines = _section(text)
    assert lines[0] == NEEDS_CODE and lines[1] == f"- I'll add it: {config.load().web_base_url}/?website=install_self"
    assert data(got)["install"]["snippet"] in text.split("\nYOUR WEBSITE\n", 1)[1].split("\nTHE WORDS THE ADS USE\n")[0]


# ---------------------------------------------------------------- the plan answer's `next` on the public surface


def test_the_ways_reach_an_assistant_as_q6s_public_actions(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    """Q6: the four ways name the public tools and the action or view that picks each, and the surface passes them on
    as they are (surface/actions.translate) -- before the launch actions, which it translates."""
    key, _biz, plan_id = _plan(client, conn, fake)
    built = call_tool(client, "adjust", {"plan_id": plan_id, "level": "conservative", "idempotency_key": "w1"}, key=key)
    assert not built["isError"], tool_text(built)
    out = actions.translate(built["structuredContent"])["next"]["actions"]
    assert out[: len(OFFERED)] == OFFERED  # support off: no team (ruling R3-f)
    # the owner chooses how the ads run (founder's ruling, 2026-10-13): connect_account's own two choices, side by side
    # -- Admiry running the ads (mode managed), the own account (its Customer ID) -- neither marked first
    assert [(a["tool"], a.get("args"), "primary" in a) for a in out[len(OFFERED) :]] == [
        ("connect_account", {"mode": "managed"}, False),
        ("connect_account", {"account_id": "<Customer ID>"}, False),
        ("update_marketing_plan", None, False),
    ]


# ---------------------------------------------------------------- the web person's e-mail and Admiry's team (Task 3)

OWNER = "owner@example.com"
SENT = "Sent to {address}. We'll tell you when the code shows up."  # Q7's reply


def _owned(conn, *, biz_id: str = "biz_1", owner: str | None = OWNER, **knowledge) -> str:
    """A claimed business with a website of its own, built on Wix unless a test says otherwise, and its owner's sign-in
    address; returns its key (read and manage)."""
    key = business_with_key(conn, biz_id=biz_id, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = %s WHERE id = %s", (owner, biz_id))
    facts = {"name": "Bright Smile Dental", "website": SITE, "website_platform": "wix", "name_source": "site"}
    business.add_knowledge(conn, biz_id, {**facts, **knowledge}, source="owner")
    return key


def _send(client, key: str, email: str, ik: str = "s1") -> dict:
    """The way as an assistant takes it from `next`: update_landing_page with the action Q6 names, and the address."""
    sent = {"action": "send_install_steps", "email": email, "idempotency_key": ik}
    return call_tool(client, "update_landing_page", sent, key=key)


def _help(client, key: str, ik: str = "h1", **extra) -> dict:
    sent = {"action": "request_install_help", "idempotency_key": ik, **extra}
    return call_tool(client, "update_landing_page", sent, key=key)


def _requests(conn) -> list[tuple]:
    rows = conn.execute("SELECT business_id, way, to_hash FROM install_request ORDER BY created_at, id").fetchall()
    return [(r["business_id"], r["way"], r["to_hash"]) for r in rows]


def _sha(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()


def _ledger(conn) -> str:
    """Every agent_action row's stored arguments and result -- its say included -- as text, in lower case."""
    rows = conn.execute("SELECT args_redacted, result FROM agent_action ORDER BY id").fetchall()
    return json.dumps([dict(r) for r in rows], default=str).lower()


def _ticket(conn) -> dict:
    return conn.execute("SELECT * FROM support_ticket").fetchone()


def _words(conn, ticket_id: str) -> list[dict]:
    """The owner_words events of a ticket, oldest first: their words and their data (the context)."""
    rows = conn.execute(
        "SELECT body, data FROM ticket_event WHERE ticket_id = %s AND kind = 'owner_words' ORDER BY created_at",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_the_web_persons_email_is_the_fixed_message_with_reply_to_the_owner(client, conn, resend, strict_surface):
    """Q7 (W11), ruling R2-i: one fixed message -- the business's name, the site, the steps for its platform and the
    code, the very steps and code the homepage's card shows (site_install.steps_view) -- from Admiry's sender, reply-to
    the owner's sign-in address, to the one address given; the install_request row keeps the address only as its
    SHA-256; an audit row. The reply is Q7's. Ruling R3-e: the address never lands in agent_action -- not its stored
    arguments, data or say; a retry with the same key answers the stored sentence and sends nothing."""
    key = _owned(conn)
    got = _send(client, key, "  Web@Agency.example ")
    assert not got["isError"], tool_text(got)
    assert got["structuredContent"]["say"] == SENT.format(address="Web@Agency.example")  # this call's own answer
    assert got["structuredContent"]["data"] == {"way": "send"}
    assert got["structuredContent"]["next"] == envelope.NONE
    [sent] = resend.sent
    install = site_install.steps_view(conn, "biz_1", site_install.row(conn, "biz_1"))  # the card's steps and code
    wix = site_install.STEPS["wix"]
    assert install["steps"] == wix["steps"] and install["platform"] == "wix"
    assert sent["to"] == ["Web@Agency.example"] and sent["reply_to"] == OWNER
    assert sent["from"] == config.load().mail_from == "Admiry <hello@agent.test>"
    assert sent["subject"] == "How to add Admiry's code to Bright Smile Dental's website"
    assert sent["text"] == "\n".join(
        [
            f"{OWNER} asked Admiry to send you the steps to add Admiry's code to Bright Smile Dental's website, {SITE}.",
            "The code lets Google count calls and forms from the site for the business's ads.",
            "",
            "Here is how to add it on Wix.",
            wix["requirement"],
            *[f"{n}. {step}" for n, step in enumerate(wix["steps"], 1)],
            f"Help: {wix['help_url']}",
            "",
            "```",
            install["snippet"],
            "```",
            "",
            (
                f"Admiry checks the site every hour and tells {OWNER} when the code shows up. A reply to this e-mail "
                f"goes to {OWNER}."
            ),
        ]
    )
    assert 'data-page="' + landing.current(conn, "biz_1")["id"] + '"' in sent["text"]
    assert not JARGON.search(sent["text"]) and not JARGON.search(sent["subject"])
    assert _requests(conn) == [("biz_1", "send", _sha("web@agency.example"))]
    stored = json.dumps([dict(r) for r in conn.execute("SELECT * FROM install_request").fetchall()], default=str)
    assert "agency" not in stored.lower()  # the address only as its SHA-256 there
    row = conn.execute(
        "SELECT tool, surface_tool, ok, args_redacted, result FROM agent_action ORDER BY id DESC LIMIT 1"
    )
    stored = row.fetchone()
    assert (stored["tool"], stored["surface_tool"], stored["ok"]) == ("send_install_steps", "update_landing_page", True)
    assert stored["args_redacted"] == {"recipient": "***"}  # audit.SECRET_FIELDS
    assert (
        stored["result"]["say"]
        == site_install.SENT_RECORDED
        == ("Sent to your web person. We'll tell you when the code shows up.")
    )
    assert stored["result"]["data"] == {"way": "send"}
    assert "agency" not in _ledger(conn)
    # the same call again (a host's retry) is answered from the first, the stored sentence: nothing is sent twice
    again = _send(client, key, "  Web@Agency.example ")
    assert not again["isError"] and again["structuredContent"]["say"] == site_install.SENT_RECORDED
    assert len(resend.sent) == 1 and "agency" not in _ledger(conn)
    # the same key with another address is a key reused for another request: refused, nothing sent
    other = _send(client, key, "someone@else.example")
    assert other["isError"] and "idempotency_key already used" in tool_text(other) and len(resend.sent) == 1


def test_the_owners_own_address_an_invalid_one_and_a_fourth_in_a_day_are_refused_in_a_sentence(
    client, conn, resend, strict_surface
):
    """Review Focus: the owner's own address (in any case), one the e-mail channel's address rule would not take
    (mail_grants.valid) and a fourth e-mail in the last 24 hours are each refused in a sentence, and nothing is sent; a
    day on, the oldest no longer counts."""
    key = _owned(conn)
    for i, (email, sentence) in enumerate(
        (
            ("not an address", "That is not an e-mail address."),
            ("web@", "That is not an e-mail address."),
            ("a@b@agency.example", "That is not an e-mail address."),
            ("x" * 250 + "@agency.example", "That is not an e-mail address."),
            ("  Owner@Example.COM ", site_install.OWN_ADDRESS),
        )
    ):
        got = _send(client, key, email, ik=f"bad{i}")
        assert got["isError"] and tool_text(got) == sentence, (email, tool_text(got))
    assert resend.sent == [] and _requests(conn) == []
    for i in range(3):
        sent = _send(client, key, f"web{i}@agency.example", ik=f"ok{i}")
        assert not sent["isError"], tool_text(sent)
    fourth = _send(client, key, "web3@agency.example", ik="ok3")
    assert fourth["isError"] and tool_text(fourth) == site_install.TOO_MANY
    assert site_install.TOO_MANY == "Admiry sends the steps at most 3 times a day; try again tomorrow."
    assert len(resend.sent) == 3 and len(_requests(conn)) == 3
    conn.execute(
        "UPDATE install_request SET created_at = now() - interval '25 hours' WHERE id = "
        "(SELECT id FROM install_request ORDER BY created_at, id LIMIT 1)"
    )
    later = _send(client, key, "web3@agency.example", ik="ok4")
    assert not later["isError"], tool_text(later)
    assert len(resend.sent) == 4
    for sentence in (site_install.OWN_ADDRESS, site_install.TOO_MANY, site_install.NOT_AN_ADDRESS):
        assert not JARGON.search(sentence)
    # ruling R3-e: the refusals' rows keep no address either, the owner's included
    assert "agency" not in _ledger(conn) and "owner@example.com" not in _ledger(conn)


def test_a_failed_send_counts_nothing_and_says_so(client, conn, resend, strict_surface):
    """Resend refusing the message: the reply says it did not go out, and the attempt does not count toward the day's
    three (its install_request row is gone)."""
    key = _owned(conn)
    resend.fail = httpx.Response(422, json={"message": "nope"})
    got = _send(client, key, "web@agency.example")
    assert got["isError"] and tool_text(got) == site_install.NOT_SENT
    assert _requests(conn) == []
    resend.fail = None
    assert not _send(client, key, "web@agency.example", ik="s2")["isError"]
    assert len(_requests(conn)) == 1


def test_only_the_callers_own_business_is_acted_on(client, conn, support_on, strict_surface):
    """W11/W12, `manage` and the caller's own business only: the business is the key's, never named by the caller -- a
    key of another business sends its own site's steps from its own owner and asks help for its own site; a key bound
    to no business, and one without the manage scope, are refused and nothing is sent or asked."""
    _owned(conn)
    other = _owned(conn, biz_id="biz_2", owner="other@example.com", name="Other Smile", website="https://other.example")
    got = _send(client, other, "web@agency.example")
    assert not got["isError"], tool_text(got)
    [sent] = support_on.sent
    assert sent["reply_to"] == "other@example.com" and "https://other.example" in sent["text"]
    assert "Bright Smile" not in sent["text"] and SITE not in sent["text"]
    assert _requests(conn) == [("biz_2", "send", _sha("web@agency.example"))]
    asked = _help(client, other)
    assert not asked["isError"], tool_text(asked)
    assert _ticket(conn)["business_id"] == "biz_2"
    platform = business_with_key(conn, cls="platform", scopes=("read", "manage"))
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    for key, refusal in ((platform, "not bound to a business"), (reader, "lacks the 'manage' scope")):
        for got in (_send(client, key, "web@agency.example", ik="x1"), _help(client, key, ik="x2")):
            assert got["isError"] and refusal in tool_text(got), tool_text(got)
    assert len(support_on.sent) == 1 + 2  # biz_2's e-mail, then its ticket's acknowledgement and the ops notice
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    assert [r[:2] for r in _requests(conn)] == [("biz_2", "send"), ("biz_2", "help")]


def test_a_help_request_is_a_website_ticket_with_the_platforms_invite_steps(client, conn, support_on, strict_surface):
    """Q8 (W12): SP1's ticket with reason `website`, its words Q8's, its context the platform's invitation steps for the
    staff (never a password), the owner's sign-in address its contact; SP1's acknowledgement and the ops notice go once
    the call has committed. The reply follows the two settings, both unset here: "soon", and no helper sentence."""
    key = _owned(conn)
    got = _help(client, key)
    assert not got["isError"], tool_text(got)
    env = got["structuredContent"]
    assert env["say"] == "Our team will contact you at owner@example.com soon." and env["next"] == envelope.NONE
    t = _ticket(conn)
    assert (t["reason"], t["status"], t["contact_email"], t["source"]) == ("website", "open", OWNER, "mcp")
    assert env["data"]["support"] == {"ticket_id": t["id"], "status": "open", "appended": False}
    [words] = _words(conn, t["id"])
    assert words["body"] == f"Install Admiry's code on {SITE} (Wix)" == t["summary"]
    context = words["data"]["context"]
    assert context.splitlines()[0] == "install"
    for line in site_install.STEPS["wix"]["invite"]:
        assert f"- {line}" in context
    assert site_install.STAFF_NEVER in context and f"Site: {SITE}" in context
    assert "Helper's address: not set yet (AGENT_SITE_HELPER_EMAIL)" in context
    acks = [m for m in support_on.sent if m["to"] == [OWNER]]
    notices = [m for m in support_on.sent if m["to"] == ["ops@example.com"]]
    assert len(acks) == 1 and acks[0]["subject"] == f"We got your message ({t['id']})"
    assert f"Install Admiry's code on {SITE} (Wix)" in acks[0]["text"]
    assert len(notices) == 1 and "(website, mcp)" in notices[0]["subject"]
    assert _requests(conn) == [("biz_1", "help", None)]
    assert site_install.status(conn, "biz_1")["way"] == "help"  # ruling R0-c: the latest way asked for


def test_a_help_request_with_no_contact_on_file_asks_for_one_and_waits(client, conn, support_on, strict_surface):
    """Review Focus: no contact given and none on file (a business nobody signed in for) -- the reply asks for one and
    claims nobody will call; the ticket waits in SP1's awaiting_contact, no mail goes; an address the e-mail channel
    would not take is refused; the next ask with an address opens the same ticket, its acknowledgement to that address."""
    key = _owned(conn, owner=None)
    got = _help(client, key)
    assert not got["isError"], tool_text(got)
    env = got["structuredContent"]
    assert env["say"] == site_install.HELP_CONTACT
    assert "will contact" not in env["say"] and "call" not in env["say"]
    install_help = next(w for w in site_install.WAYS if w["id"] == "install_help")
    assert env["next"] == envelope.actions(install_help)  # the same way again, with the address
    t = _ticket(conn)
    assert (t["status"], t["contact_email"], t["reason"]) == ("awaiting_contact", None, "website")
    assert support_on.sent == []
    bad = _help(client, key, ik="h2", contact="not an address")
    assert bad["isError"] and tool_text(bad) == "That is not an e-mail address."
    given = _help(client, key, ik="h3", contact="  Jane@Smile.example ")
    assert not given["isError"], tool_text(given)
    assert given["structuredContent"]["say"] == "Our team will contact you at jane@smile.example soon."
    again = _ticket(conn)
    assert (again["id"], again["status"], again["contact_email"]) == (t["id"], "open", "jane@smile.example")
    assert [m["to"] for m in support_on.sent] == [["jane@smile.example"], ["ops@example.com"]]
    assert "Contact the owner gave: jane@smile.example" in _words(conn, t["id"])[-1]["data"]["context"]


def test_a_contact_given_is_the_new_tickets_and_the_reply_follows_the_two_settings(
    client, conn, support_on, strict_surface, monkeypatch
):
    """Q8: the contact given, else the owner's sign-in address, is the ticket's; the reply's promise is
    AGENT_SITE_HELP_PROMISE ("soon" without it) and its second sentence names AGENT_SITE_HELPER_EMAIL, dropped
    without it (spec §8 item 1)."""
    monkeypatch.setenv("AGENT_SITE_HELPER_EMAIL", " Help@Admiry.example ")
    monkeypatch.setenv("AGENT_SITE_HELP_PROMISE", "within one business day.")
    key = _owned(conn)
    got = _help(client, key, contact="office@smile.example")
    assert not got["isError"], tool_text(got)
    assert got["structuredContent"]["say"] == (
        "Our team will contact you at office@smile.example within one business day. They'll ask you to add "
        "help@admiry.example as a helper on Wix, never for your password."
    )
    assert _ticket(conn)["contact_email"] == "office@smile.example"
    assert "Helper's address: help@admiry.example" in _words(conn, _ticket(conn)["id"])[0]["data"]["context"]
    # the four settings, on the reply alone
    for helper, promise, said in (
        ("", "", "Our team will contact you at a@b.example soon."),
        ("", "within one business day", "Our team will contact you at a@b.example within one business day."),
        (
            "help@admiry.example",
            "",
            (
                "Our team will contact you at a@b.example soon. They'll ask you to add help@admiry.example as a helper "
                "on your site, never for your password."
            ),
        ),
    ):
        monkeypatch.setenv("AGENT_SITE_HELPER_EMAIL", helper)
        monkeypatch.setenv("AGENT_SITE_HELP_PROMISE", promise)
        assert site_install.help_say("a@b.example", site_install.STEPS["other"]["name"]) == said
    monkeypatch.setenv("AGENT_SITE_HELPER_EMAIL", "not-an-address")
    with pytest.raises(ValueError, match="AGENT_SITE_HELPER_EMAIL"):
        config.load()


def test_a_help_ticket_carries_the_page_and_the_code_to_paste_and_the_watch_has_a_page(
    client, conn, support_on, strict_surface, monkeypatch
):
    """Ruling F-2 (the final review): asking Admiry's team makes sure of the business's one page row, as sending the
    steps does -- here for a business whose plan never fetched the kit, with no page row yet -- and the ticket's
    context carries its id and the kit's install part: the steps and the exact code to paste, the very code the owner's
    card shows. So the staff install from the ticket, and the hourly watch of the ask (R3-a) has a page to watch: the
    website is read for the code."""
    key = _owned(conn)
    assert landing.current(conn, "biz_1") is None
    got = _help(client, key)
    assert not got["isError"], tool_text(got)
    page = landing.current(conn, "biz_1")
    assert page is not None and (page["status"], page["url"]) == ("preview", "")  # the kit's placeholder
    context = _words(conn, _ticket(conn)["id"])[0]["data"]["context"].splitlines()
    install = site_install.steps_view(conn, "biz_1", site_install.row(conn, "biz_1"))  # the card's steps and code
    assert f'data-page="{page["id"]}"' in install["snippet"] and install["snippet"] in context
    assert f"Page: {page['id']}" in context
    wix = site_install.STEPS["wix"]
    steps = [f"{n}. {step}" for n, step in enumerate(wix["steps"], 1)]
    at = context.index(wix["requirement"])
    assert context[at : at + len(steps) + 2] == [wix["requirement"], *steps, f"Help: {wix['help_url']}"]
    assert site_install.STAFF_NEVER in context and not JARGON.search(got["structuredContent"]["say"])

    fetched: list[str] = []

    def web(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text="<html><body>Bright Smile Dental</body></html>")

    monkeypatch.setattr(crawl, "resolve", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(jobs, "_page_transport", lambda: httpx.MockTransport(web))
    details = tick.run("page-check", shard="0/1", url=TEST_DB_URL)
    assert details["watched"] == 1 and fetched == [SITE]


def test_the_help_replys_contact_never_lands_in_the_ledger(client, conn, support_on, strict_surface):
    """Ruling F-4 (the final review; R3-e's pattern): the contact typed for Admiry's team may be anyone's address, so no
    agent_action row keeps it -- the stored answer, which a replay gives, is Q8's sentence without it ("Our team will
    contact you soon."), and only this call's own answer names it. With no contact typed, the owner's own address is
    left out of the stored answer the same way."""
    key = _owned(conn)
    got = _help(client, key, contact="Office@Smile.example")
    assert not got["isError"], tool_text(got)
    assert got["structuredContent"]["say"] == "Our team will contact you at office@smile.example soon."
    assert "office@smile.example" not in _ledger(conn)
    stored = conn.execute("SELECT result FROM agent_action WHERE tool = 'request_install_help'").fetchone()["result"]
    assert stored["say"] == "Our team will contact you soon." == site_install.HELP_RECORDED.format(promise="soon")
    sent = len(support_on.sent)
    again = _help(client, key, contact="Office@Smile.example")  # the same call again: answered from the first
    assert not again["isError"] and again["structuredContent"]["say"] == "Our team will contact you soon."
    assert "office@smile.example" not in _ledger(conn) and len(support_on.sent) == sent  # nothing sent twice
    other = _owned(conn, biz_id="biz_2", owner="other@example.com", website="https://other.example")
    mine = _help(client, other, ik="h2")
    assert mine["structuredContent"]["say"] == "Our team will contact you at other@example.com soon."
    assert "other@example.com" not in _ledger(conn)


def test_the_two_ways_act_only_while_the_site_needs_the_code(client, conn, support_on, strict_surface):
    """Q6: the ways exist while the row needs the code. The code found, an Admiry page for the ads (the website stays as
    it is), no website of its own: each refused in the row's sentence or one of its own; nothing sent, no ticket."""
    key = _owned(conn)
    page = landing.set_owner_url(conn, "biz_1", f"{SITE}/invisalign")
    _checked(conn, page["id"], found=True)
    for got in (_send(client, key, "web@agency.example", ik="f1"), _help(client, key, ik="f2")):
        assert got["isError"] and tool_text(got) == FOUND
    _hosted(conn, "biz_1")
    for got in (_send(client, key, "web@agency.example", ik="a1"), _help(client, key, ik="a2")):
        assert got["isError"] and tool_text(got) == ADMIRY_PAGE
    _no_website(conn, "biz_1")
    for got in (_send(client, key, "web@agency.example", ik="n1"), _help(client, key, ik="n2")):
        assert got["isError"] and tool_text(got) == site_install.NO_SITE
    assert support_on.sent == [] and _requests(conn) == []
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def test_admirys_team_is_offered_only_while_support_is_on(conn, monkeypatch):
    """Ruling R3-f: with support off (AGENT_SUPPORT off, as on prod today) "Have Admiry's team do it" is offered nowhere
    -- not in the row's ways, which the homepage card draws (test_home_page), not in the plan answer's `next`, not in
    the plan e-mail's links; with support on it is back in its place. Asked anyway (an assistant that kept the action),
    it refuses in contact_support's own sentence."""
    from fastmcp.exceptions import ToolError

    from agent import support, tools_consult

    biz = _business(conn, website=SITE, website_platform="wix")
    way_ids = {w["id"] for w in Q6}
    with pytest.raises(ToolError, match=support.NOT_AVAILABLE):
        site_install.ask_team(conn, biz, None, source="mcp", actor="key:k1", ip=None, draft=False)

    def offered() -> tuple[list, list, list]:
        found = site_install.row(conn, biz)
        nxt, _note = tools_consult.launch_next([], draft=False, website=found)
        return (
            [w["id"] for w in found["ways"]],
            [a["id"] for a in nxt["actions"] if a["id"] in way_ids],
            [w["id"] for w in site_install.email_section(conn, biz)["ways"]],
        )

    without = ["install_self", "install_send", "admiry_page"]
    assert offered() == (without, without, without)
    for name, value in (
        ("AGENT_SUPPORT", "on"),  # what config needs beside it: Resend, the ops inbox, a reply-to while inbound is off
        ("RESEND_API_KEY", "re_test_x"),
        ("AGENT_OPS_EMAIL", "ops@example.com"),
        ("AGENT_SUPPORT_REPLY_TO", "support@example.com"),
    ):
        monkeypatch.setenv(name, value)
    every = [w["id"] for w in Q6]
    assert offered() == (every, every, every)


def test_the_e_mails_steps_follow_the_page_the_ads_use(client, conn, resend, strict_surface):
    """Ruling R2-i: an owner page on another host for the ads -- the e-mail names that page and gives `other`'s steps,
    as the row and the card do (ruling R2-b); a business that nobody signed in for cannot send (no reply-to)."""
    key = _owned(conn)
    landing.set_owner_url(conn, "biz_1", ELSEWHERE)
    got = _send(client, key, "web@agency.example")
    assert not got["isError"], tool_text(got)
    text = resend.sent[0]["text"]
    assert f"Bright Smile Dental's website, {ELSEWHERE}." in text and "Here is how to add it on your site." in text
    assert site_install.STEPS["other"]["steps"][0] in text and "Wix" not in text
    nobody = _owned(conn, biz_id="biz_2", owner=None)
    refused = _send(client, nobody, "web@agency.example", ik="n1")
    assert refused["isError"] and tool_text(refused) == site_install.NO_OWNER and len(resend.sent) == 1


def test_an_assistant_reaches_both_ways_from_the_plans_next(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    support_on,
):
    """Ruling R0-f: the two update_landing_page actions WAYS names are wired -- the plan answer's `next` ways, sent as
    they are with the address (or nothing more), run send_install_steps and request_install_help."""
    key, biz, plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = %s WHERE id = %s", (OWNER, biz))
    built = call_tool(client, "adjust", {"plan_id": plan_id, "level": "conservative", "idempotency_key": "w1"}, key=key)
    offered = {a["id"]: a for a in actions.translate(built["structuredContent"])["next"]["actions"]}
    send, help_ = offered["install_send"], offered["install_help"]
    sent = {**send["args"], "email": "web@agency.example", "idempotency_key": "w2"}
    got = call_tool(client, send["tool"], sent, key=key)
    assert not got["isError"], tool_text(got)
    asked = call_tool(client, help_["tool"], {**help_["args"], "idempotency_key": "w3"}, key=key)
    assert not asked["isError"], tool_text(asked)
    tools = [r["tool"] for r in conn.execute("SELECT tool FROM agent_action ORDER BY id DESC LIMIT 2").fetchall()]
    assert tools == ["request_install_help", "send_install_steps"]


def test_the_schema_keeps_the_requests_and_the_code_seen(conn):
    """Q10 (ruling R0-g): install_request (a way of send or help, its address only as a hash) indexed by business and
    time, and page.code_seen for Task 4; the final review's ruling F-1 adds `found_at`, when a check first found the
    code after the ask."""
    cols = conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'install_request' "
        "ORDER BY ordinal_position"
    ).fetchall()
    assert [(c["column_name"], c["data_type"], c["is_nullable"]) for c in cols] == [
        ("id", "text", "NO"),
        ("business_id", "text", "NO"),
        ("way", "text", "NO"),
        ("to_hash", "text", "YES"),
        ("created_at", "timestamp with time zone", "NO"),
        ("found_at", "timestamp with time zone", "YES"),
    ]
    found = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid = 'install_request'::regclass "
        "AND contype = 'c'"
    ).fetchall()
    assert [re.findall(r"'(\w+)'", r["d"]) for r in found] == [["send", "help"]]
    index = conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'install_request_business'").fetchone()
    assert index and "(business_id, created_at)" in index["indexdef"]
    seen = conn.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'page' AND column_name = 'code_seen'"
    ).fetchone()
    assert seen == {"data_type": "boolean"}


# ---------------------------------------------------------------- never a password (W12)

AGENT = Path(__file__).resolve().parents[1]
PASSWORD = re.compile(r"password", re.IGNORECASE)
# A sentence that says what is never asked for: "never for your password", "Never ask the owner for a password"
NEVER_A_PASSWORD = re.compile(r"\bnever\b[^.!?\n]*\bpasswords?\b", re.IGNORECASE)


def _asks_for_a_password(text: str) -> list[str]:
    """Every sentence of `text` that names a password without saying it is never asked for."""
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if PASSWORD.search(sentence) and not NEVER_A_PASSWORD.search(sentence):
            found.append(sentence)
    return found


def _js_function(src: str, name: str) -> str:
    """One of home.js's functions (two spaces in, ending with a line of "  }"), as test_home_page reads them."""
    lines = src.split("\n")
    start = next(i for i, x in enumerate(lines) if x.startswith((f"  function {name}(", f"  async function {name}(")))
    end = next(i for i, x in enumerate(lines) if i > start and x == "  }")
    return "\n".join(lines[start : end + 1])


def test_no_field_sentence_or_argument_asks_for_a_password(client):
    """W12 and the batch's Global Constraint: no field, sentence or argument of this task asks for a password -- the
    e-mail template, the homepage's "Your website" card and the script that draws it, the two commands' words and the
    public tool's arguments, and (Task 5) the words assistants read: the two page tools' descriptions and every file of
    the skill. "never ... password" is the one way a password is named."""
    from agent import assistant_skill, mcp_app

    texts = {
        "mail_install.txt.j2": (AGENT / "templates" / "mail_install.txt.j2").read_text(),
        "home.html card": client.get("/").text.split('data-part="website"')[1].split("</section>")[0],
    }
    js = (AGENT / "static" / "home.js").read_text()
    for name in ("websiteCard", "wayForm", "chooseWay", "showWay", "wayAnswer", "said"):
        texts[f"home.js {name}"] = _js_function(js, name)
    for name, value in vars(site_install).items():
        if name.isupper() and isinstance(value, str):
            texts[f"site_install.{name}"] = value
    commands = {m.name: m.tool for m in mcp_app.REGISTRY}
    for name in ("send_install_steps", "request_install_help"):
        texts[f"{name} description"] = commands[name].description
        assert not [p for p in commands[name].parameters["properties"] if PASSWORD.search(p)], name
    page_tool = next(m.tool for m in registry.SURFACE if m.name == "update_landing_page")
    assert not [p for p in page_tool.parameters["properties"] if PASSWORD.search(p)]
    for meta in registry.SURFACE:
        if meta.name in ("get_landing_page", "update_landing_page"):
            texts[f"{meta.name} description"] = meta.tool.description
    for path, text in assistant_skill.files().items():
        texts[f"skill {path}"] = text
    for where, text in texts.items():
        assert not _asks_for_a_password(text), (where, _asks_for_a_password(text))
    card = texts["home.html card"]
    assert 'type="password"' not in card and not re.search(r"<input[^>]*password", card, re.IGNORECASE)
    # the sentences that do name it say it is never asked for -- the help reply, the staff's line, and the words an
    # assistant reads about Admiry's team -- and the scan finds one that asks
    assert NEVER_A_PASSWORD.search(site_install.HELP_HELPER) and NEVER_A_PASSWORD.search(site_install.STAFF_NEVER)
    assert NEVER_A_PASSWORD.search(page_tool.description) and NEVER_A_PASSWORD.search(texts["skill SKILL.md"])
    assert _asks_for_a_password("Send us your password.") == ["Send us your password."]
