"""spec 2026-10-11 §3, §9: an assistant following each item's `fix` through the public tools clears the item -- one
test per resolution -- and a `<…>` placeholder sent as a value is refused with nothing written (Review Focus 5, plan
D15)."""

import pytest
from psycopg.types.json import Jsonb

from agent import blockers, business, copy, landing, launch_check, launch_review, places, plans
from agent.channels.google_search import adapter as google
from agent.surface import supporting
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import planned
from agent.tests.test_ads_run import PAGE_URL, _check_failed, _knowledge, _no_page, _unchecked, sections, seed
from agent.tests.test_blockers_headline import _biz, _leads_with, _live_and_linked, _owner, _upgraded
from agent.tests.test_home_api import fake  # noqa: F401 -- a fixture used by name
from agent.tests.test_home_sites import _build
from agent.tests.test_sites_build import _own, dental_library, renderer  # noqa: F401 -- fixtures used by name
from agent.tests.test_tools_pages import _PUBLIC_IP, _report


def _checklist(client, key) -> dict:
    res = call_tool(client, "get_marketing_state", {}, key=key)
    assert not res["isError"], tool_text(res)
    return data(res)["setup"]["checklist"]


def _item(client, key, item_id: str) -> dict:
    found = next((i for i in _checklist(client, key)["items"] if i["id"] == item_id), None)
    assert found is not None, f"{item_id} is not on the list"
    return found


def _follow(client, key, item: dict, args: dict | None = None, *, idem: str = "fix-1") -> dict:
    """The fix as an assistant sends it: its tool with its args (`args` replaces them where the owner answered)."""
    fix = item["fix"]
    sent = dict(args if args is not None else fix["args"])
    if fix["tool"] != "get_landing_page":
        sent["idempotency_key"] = idem
    res = call_tool(client, fix["tool"], sent, key=key)
    assert not res["isError"], tool_text(res)
    return res


def _gone(client, key, item_id: str) -> None:
    assert not any(i["id"] == item_id for i in _checklist(client, key)["items"]), item_id


@pytest.fixture
def page_checks(monkeypatch):
    """The page check faked to pass, as test_tools_pages fakes it (nothing is fetched)."""
    from agent import crawl, tools_pages

    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))


def test_the_phone_fix_clears_the_phone_item(client, conn):
    key, _ = seed(conn, status="draft", secs=sections())
    _knowledge(conn, phone=None)
    item = _item(client, key, "phone")
    assert item["fix"]["ask_owner"] == "What phone number should people call?"
    _follow(client, key, item, {"patch": {"locations": [{"phone": "(650) 555-0100"}]}})
    _gone(client, key, "phone")
    assert business.latest_knowledge(conn, "biz_1")[1]["phone"] == "(650) 555-0100"


def test_the_address_fix_clears_the_location_item(client, conn, monkeypatch):
    key, _ = seed(conn, status="draft", secs=sections())
    _knowledge(conn, listing=None, service_area=[])
    item = _item(client, key, "location")
    assert item["fix"]["tool"] == "update_business_profile"
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    found = places.Candidate("ChIJ9", "4 Elm St", "4 Elm St, Austin, TX 78701, USA", 30.27, -97.74)
    monkeypatch.setattr(places, "search_text", lambda query, key: found)
    _follow(client, key, item, {"patch": {"locations": [{"address": "4 Elm St, Austin"}]}})
    _gone(client, key, "location")


def test_the_miles_fix_clears_the_radius_item(client, conn, walk_fake):
    walk = planned(client, conn)
    plan = plans.latest(conn, "biz_1")
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,consultation,geo,radius_miles}', 'null') WHERE id = %s",
        (plan["id"],),
    )
    item = _item(client, walk["key"], "radius")
    assert item["fix"]["tool"] == "update_marketing_plan"
    _follow(client, walk["key"], item, {"changes": {"radius_miles": 10}})
    _gone(client, walk["key"], "radius")
    assert plans.latest(conn, "biz_1")["sections"]["consultation"]["geo"]["radius_miles"] == 10


def test_the_budget_choice_clears_the_limit_item(client, conn, walk_fake):
    walk = planned(client, conn)
    business.set_goal(conn, "biz_1", monthly_limit_usd=500)
    item = _item(client, walk["key"], "limit")
    assert item["fix"]["choices"][0]["args"] == {"monthly_usd": 500}
    _follow(client, walk["key"], item, item["fix"]["choices"][0]["args"])
    _gone(client, walk["key"], "limit")
    assert plans.total_usd(plans.latest(conn, "biz_1")["sections"]) == 500


def test_the_ad_text_fix_clears_the_write_copy_item(client, conn, walk_fake):
    walk = planned(client, conn)
    plan = plans.latest(conn, "biz_1")
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,ads,channels,0,headlines}', '[]') WHERE id = %s",
        (plan["id"],),
    )
    item = _item(client, walk["key"], "write_copy")
    assert item["fix"]["tool"] == "update_marketing_plan"
    assert item["fix"]["args"] == {"message": blockers.COPY_INSTRUCTION}
    _follow(client, walk["key"], item)  # change_plan: words that name no field are a rewrite (the fake draft's words)
    _gone(client, walk["key"], "write_copy")
    assert not google.copy_short(plans.latest(conn, "biz_1"))


def test_the_fresh_plan_fix_clears_the_fresh_plan_item(client, conn, walk_fake):
    walk = planned(client, conn)
    plan = plans.latest(conn, "biz_1")
    conn.execute(  # no cost per lead anywhere in the estimate: Google Search cannot run this version as it is
        "UPDATE plan SET json = jsonb_set(json, '{sections,estimate}', ((json->'sections'->'estimate') - 'cpl_avg' "
        "- 'cpl_low' - 'cpl_high') || %s) WHERE id = %s",
        (Jsonb({"assumptions": []}), plan["id"]),
    )
    item = _item(client, walk["key"], "fresh_plan")
    assert item["fix"]["tool"] == "update_marketing_plan"
    assert item["fix"]["args"] == {"message": blockers.FRESH_INSTRUCTION}
    _follow(client, walk["key"], item)
    _gone(client, walk["key"], "fresh_plan")
    assert plans.latest(conn, "biz_1")["version"] == plan["version"] + 1


def test_the_page_check_fix_clears_an_unchecked_page(client, conn, page_checks):
    key, _ = seed(conn, status="draft", secs=sections())
    _unchecked(conn)
    item = _item(client, key, "check_page")
    assert item["fix"] == {
        "tool": "get_landing_page",
        "args": {"view": "check"},
        "ask_owner": None,
        "label": "Check my page now",
    }
    _follow(client, key, item)
    assert _checklist(client, key)["items"] == []


def test_the_owners_page_fix_is_the_check_again_once_the_page_is_changed(client, conn, page_checks):
    key, _ = seed(conn, status="draft", secs=sections())
    _check_failed(conn)
    item = _item(client, key, "page:phone")
    assert item["fix"] == {
        "tool": "get_landing_page",
        "args": {"view": "check"},
        "ask_owner": None,
        "label": "Check again",
    }
    _follow(client, key, item)  # the page fixed meanwhile: the faked check passes
    assert _checklist(client, key)["items"] == []


def test_the_headline_fix_clears_the_headline_item(client, conn, monkeypatch, page_checks):
    """Ruling I1 (task review), then Ruling I3 (fix round 2): Admiry's own starter page whose headline no longer
    names the plan's lead service and city -- the page_fix/FIX_HEADLINE state test_blockers_headline.py's and
    test_launch_check.py's own tests build (a published page with `sections.copy`, so blockers.admiry_page reads
    "starter", and a failed message_match rule; launch_check.py's page_fix branch) -- offers update_landing_page's
    `edit` of the headline first, with launch_check.HEADLINE_SUGGESTION as the item's suggestion. Round 1 found
    that following the edit alone never clears the item: `page.check_json` -- what the checklist reads -- is left
    exactly as it was, the edit's own new words landing only in a fresh preview. Ruling I3 closes that loop:
    fix_for, read again, finds that very preview (its headline already the one it would have asked for) and
    offers its publish instead -- the owner's two-call confirm (update_landing_page's own "publish": the first
    call only proposes, nothing changes; the same token with owner_confirmed: true confirms it, which also
    re-checks the now-live address) -- so fix, read the state, fix again converges rather than looping, and
    following it through clears the item for good."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    key, _ = seed(conn, status="draft", secs=sections())
    conn.execute(
        "UPDATE page SET sections = %s, check_json = %s, source = 'admiry' WHERE business_id = %s",
        (
            # a lint-clean starter page's words, naming only what test_ads_run.KNOWLEDGE gives Bright Smile Dental
            # (no price, year, rating or licence claim the facts do not back) -- admiry_page reads "starter" once
            # sections.copy is set
            Jsonb(
                {
                    "copy": {
                        "headline": "Invisalign in Palo Alto",
                        "subheadline": "Clear aligners for patients in Palo Alto.",
                        "offer_line": "",
                        "trust_points": ["Serving Palo Alto", "Ask about Invisalign"],
                        "service_paragraphs": [
                            (
                                "Invisalign straightens teeth with clear aligners you change every few weeks, "
                                "with visits to check how it is going."
                            )
                        ],
                        "faq": [{"q": "Is there parking?", "a": "Call us and we tell you where to park."}],
                        "cta_label": "Call to book",
                    },
                    "tree": "dentists",
                }
            ),
            Jsonb(
                {
                    "ok": False,
                    "url": PAGE_URL,
                    "rules": [
                        {"id": "reachable", "ok": True, "fix": None},
                        {"id": "message_match", "ok": False, "fix": "Say the service and the city in the headline."},
                    ],
                }
            ),
            "biz_1",
        ),
    )
    item = _item(client, key, "page:message_match")
    assert item["suggestion"] == launch_check.HEADLINE_SUGGESTION
    assert item["fix"] == {
        "tool": "update_landing_page",
        "args": {"action": "edit", "section": "headline", "text": landing.headline_pattern(conn, "biz_1")},
        "ask_owner": None,
        "label": "Fix the headline",
    }
    edited = _follow(client, key, item)
    token = data(edited)["token"]

    # ruling I3: read again, the fix is now publishing that very preview -- the edit already made the words
    item = _item(client, key, "page:message_match")
    assert item["fix"] == {
        "tool": "update_landing_page",
        "args": {"action": "publish", "token": token},
        "ask_owner": None,
        "label": "Publish the page",
    }
    before_publish = conn.execute("SELECT check_json FROM page WHERE business_id = 'biz_1'").fetchone()["check_json"]
    proposed = call_tool(
        client, "update_landing_page", {"action": "publish", "token": token, "idempotency_key": "pub-1"}, key=key
    )
    assert not proposed["isError"], tool_text(proposed)
    # the first call only proposes: nothing published yet, the stale report untouched
    after_propose = conn.execute("SELECT check_json FROM page WHERE business_id = 'biz_1'").fetchone()["check_json"]
    assert after_propose == before_publish
    confirmed = call_tool(
        client,
        "update_landing_page",
        {"action": "publish", "token": token, "owner_confirmed": True, "idempotency_key": "pub-2"},
        key=key,
    )
    assert not confirmed["isError"], tool_text(confirmed)
    assert _checklist(client, key)["items"] == []


def test_the_sites_headline_fix_clears_the_headline_item_through_the_public_tool(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    resend,
    dental_library,  # noqa: F811 -- the fixture
    renderer,  # noqa: F811 -- the fixture
):
    """Like test_the_headline_fix_clears_the_headline_item, but for Admiry's own SITE (follow-up 2026-10-12, founder:
    "好" -- ruling I3's own gap): the public fix_headline action runs tools_sites.fix_headline under its own command
    machinery (build_site's own run()), converges to publish the same way once the site's own newest build already
    carries the fixed headline (ruling I3a), and following it through clears the item -- the home's own button
    (test_blockers_headline.py) keeps working, unchanged, sharing the same body (blockers.run_headline_fix)."""
    monkeypatch.setenv("AGENT_SITES", "1")
    csrf = _owner(client, conn, google, monkeypatch, fake)
    biz = _biz(conn)
    _upgraded(conn, biz)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    built_token = _build(client, csrf, style="calm")["page"]["preview"]["token"]
    _live_and_linked(client, conn, monkeypatch, csrf, built_token)
    # review round 1, M4: the precondition this whole test leans on -- message_match only blocks a page whose
    # source is Admiry's own (landing.py), which a built site's page always is
    assert landing.current(conn, biz)["source"] in ("hosted", "admiry")
    _leads_with(conn, biz, "Teeth whitening")
    key = business_with_key(conn, biz_id=biz, scopes=("read", "manage"), claim_state="verified")

    item = _item(client, key, "page:message_match")
    assert item["fix"] == {
        "tool": "update_landing_page",
        "args": {"action": "fix_headline"},
        "ask_owner": None,
        "label": "Fix the headline",
    }
    fixed = _follow(client, key, item)
    token = data(fixed)["token"]
    assert token and token != built_token

    # ruling I3a: read again, the fix is now publishing that very preview -- the rebuild already made the words
    item = _item(client, key, "page:message_match")
    assert item["fix"] == {
        "tool": "update_landing_page",
        "args": {"action": "publish", "token": token},
        "ask_owner": None,
        "label": "Publish the page",
    }
    proposed = call_tool(
        client, "update_landing_page", {"action": "publish", "token": token, "idempotency_key": "sfh-pub-1"}, key=key
    )
    assert not proposed["isError"], tool_text(proposed)
    confirmed = call_tool(
        client,
        "update_landing_page",
        {"action": "publish", "token": token, "owner_confirmed": True, "idempotency_key": "sfh-pub-2"},
        key=key,
    )
    assert not confirmed["isError"], tool_text(confirmed)
    _gone(client, key, "page:message_match")


def test_the_site_fix_headline_action_is_refused_when_the_headline_stops_nothing(client, conn):
    """Follow-up 2026-10-12: the public fix_headline action refuses (blockers.NO_HEADLINE_FIX) when no checklist
    item is the headline's own -- here, nothing stops the launch at all."""
    key, _ = seed(conn, status="draft", secs=sections())
    res = call_tool(client, "update_landing_page", {"action": "fix_headline", "idempotency_key": "nf-1"}, key=key)
    assert res["isError"] and blockers.NO_HEADLINE_FIX in tool_text(res)


def test_the_site_fix_headline_action_is_refused_not_as_a_moved_plan_with_no_draft(client, conn):
    """Review round 1, M2: a business with no draft at all (here, an approved plan: seed's own default) is refused
    blockers.NO_HEADLINE_FIX, never blockers.PLAN_MOVED -- fix_headline takes no plan_id to have moved under, so a
    plan that is simply not a draft must read the same as nothing on the headline stopping the launch, not as a
    stale claim about which version was asked for."""
    key, _ = seed(conn)  # status="approved" by default: no draft to read a checklist from at all
    res = call_tool(client, "update_landing_page", {"action": "fix_headline", "idempotency_key": "ap-1"}, key=key)
    assert res["isError"] and blockers.NO_HEADLINE_FIX in tool_text(res)
    assert blockers.PLAN_MOVED not in tool_text(res)


def test_the_page_choice_clears_the_page_item(client, conn, page_checks):
    key, _ = seed(conn, status="draft", secs=sections())
    _no_page(conn)
    item = _item(client, key, "page")
    own = item["fix"]["choices"][-1]["args"]  # the owner's own website, last whatever else is offered
    assert own == {"action": "use_url", "url": "<the owner's page address>"}
    _follow(client, key, item, {**own, "url": PAGE_URL})
    _gone(client, key, "page")


def test_each_way_to_a_page_is_a_first_call_update_landing_page_takes(
    client, conn, walk_fake, page_checks, monkeypatch
):
    """The founder's ruling (2026-10-13): the page item's fix is three ways, in the owner's words, the owner's to
    choose -- Admiry builds it, the owner's assistant designs it and Admiry hosts it, or the website the owner already
    has. Each choice's arguments, sent as they are (the owner's address in the placeholder's place), are a first call
    update_landing_page takes and answers with what comes next: the page Admiry wrote, as a private preview; the link
    the assistant uploads its site to, with the script tag every page carries and the command to run; the owner's own
    address, which settles the page."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the upload's link is on
    walk = planned(client, conn)
    item = _item(client, walk["key"], "page")
    assert item["fix"]["ask_owner"] == launch_check.PAGE_QUESTION and item["blocking"] is True
    choices = item["fix"]["choices"]
    assert [c["label"] for c in choices] == [
        "Admiry builds it for me",
        "My assistant designs it, Admiry hosts it",
        "I already have a website",
    ]
    written = _follow(client, walk["key"], item, choices[0]["args"], idem="way-1")
    assert data(written)["token"] and "preview_url" in data(written)
    upload = _follow(client, walk["key"], item, choices[1]["args"], idem="way-2")
    assert data(upload)["upload_url"] and "<script" in data(upload)["snippet"] and data(upload)["how"]
    _item(client, walk["key"], "page")  # a preview and a link settle nothing: the item stays
    _follow(client, walk["key"], item, {**choices[2]["args"], "url": PAGE_URL}, idem="way-3")
    _gone(client, walk["key"], "page")


def test_admiry_builds_the_site_where_it_builds_one_for_the_business(client, conn, walk_fake, monkeypatch):
    """Admiry's way is its own website for a home-services business or a dental practice where Admiry's own sites are
    on (tools_sites.takes_over: what `write` hands over to) -- build_site, whose first call with no other arguments
    asks which look the owner wants -- and the page Admiry writes otherwise."""
    walk = planned(client, conn)  # a dental practice
    assert _item(client, walk["key"], "page")["fix"]["choices"][0]["args"] == {"action": "write"}
    monkeypatch.setenv("AGENT_SITES", "on")
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts the site it builds
    item = _item(client, walk["key"], "page")
    assert item["fix"]["choices"][0]["args"] == {"action": "build_site"} == item["fix"]["args"]
    asked = _follow(client, walk["key"], item, idem="way-4")
    assert asked["structuredContent"]["next"]["kind"] == "question"


def test_the_page_items_fix_names_no_way_first_and_its_suggestion_the_three_ways(client, conn, walk_fake, monkeypatch):
    """Page-first review, item 1: the page item's fix is the owner's choice between three ways, so its label names none
    of them (it was the home's first button, "Build your business website") and Admiry's suggestion under it names the
    three -- never "free": hosting is free only while the ads spend at least $200 a month."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)
    item = _item(client, walk["key"], "page")
    assert item["fix"]["label"] == "Choose the page for your ads" == launch_check.PAGE_LABEL
    assert item["suggestion"] == (
        "Admiry can build your page, your assistant can design one that Admiry hosts, or the ads can use the website "
        "you already have."
    )
    assert launch_check.SUGGESTIONS["page"] == item["suggestion"] and "free" not in item["suggestion"].lower()
    assert copy.jargon(item["suggestion"]) is None and copy.jargon(item["fix"]["label"]) is None


def test_with_hosting_off_the_page_ways_are_only_those_admiry_can_take(client, conn, walk_fake, monkeypatch):
    """Page-first review, item 6: without Admiry hosting (AGENT_PAGES_DOMAIN unset) the assistant-designed way, which
    Admiry hosts (upload_site), is not offered -- two choices, and the question names only those two; Admiry's way is
    offered where Admiry can build the page (commit (1)'s rule: build_site where Admiry builds the business a site,
    else write) -- its own website needs hosting, so a business it would build one for is offered its own website
    alone. Every place that asks reads the same choices: the checklist, the state's page step and its `next`, and
    connect_account."""
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    walk = planned(client, conn)  # a dental practice, Admiry's own sites off: write
    item = _item(client, walk["key"], "page")
    choices = item["fix"]["choices"]
    assert choices == [
        {"id": "admiry_builds", "label": "Admiry builds it for me", "args": {"action": "write"}},
        {
            "id": "own_website",
            "label": "I already have a website",
            "args": {"action": "use_url", "url": "<the owner's page address>"},
        },
    ]
    question = item["fix"]["ask_owner"]
    assert question == (
        "Which page should your ads send people to? Admiry can build one for you, or the ads can use the website you "
        "already have. For your own website, what is its address?"
    )
    assert "assistant" not in question and copy.jargon(question) is None
    state = call_tool(client, "get_marketing_state", {}, key=walk["key"])["structuredContent"]
    page = next(s for s in state["data"]["setup"]["steps"] if s["id"] == "page")
    assert page["choices"] == choices and page["ask_owner"] == question and question in state["say"]
    assert [a["id"] for a in state["next"]["actions"]] == ["admiry_builds", "own_website"]
    asked = call_tool(client, "connect_account", {"idempotency_key": "h1"}, key=walk["key"])
    assert data(asked)["choices"] == choices and asked["structuredContent"]["say"] == question
    monkeypatch.setenv("AGENT_SITES", "on")  # Admiry would build its own website, which needs hosting
    item = _item(client, walk["key"], "page")
    assert item["fix"]["choices"] == [choices[-1]] and item["fix"]["args"] == choices[-1]["args"]
    assert item["fix"]["ask_owner"] == (
        "Which page should your ads send people to? The ads can use the website you already have. For your own "
        "website, what is its address?"
    )


def test_the_page_ways_are_known_by_their_id_so_new_words_cannot_break_a_read(client, conn, walk_fake, monkeypatch):
    """Final review, item 3: each page choice carries its way's `id`, and the question and the actions find the way by
    it -- never by its label, so a label said in new words (an edit to PAGE_WAYS, or choices read back from before it)
    asks the same question and offers the same ways instead of failing the read."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    planned(client, conn)
    choices = launch_check.page_choices(conn, "biz_1")
    relabelled = [{**c, "label": f"{c['label']} (in new words)"} for c in choices]
    assert launch_check.page_question(relabelled) == launch_check.page_question(choices) == launch_check.PAGE_QUESTION
    offered = launch_check.page_actions(relabelled)
    assert (
        [a["id"] for a in offered]
        == [c["id"] for c in choices]
        == ["admiry_builds", "assistant_designs", "own_website"]
    )
    assert [a["label"] for a in offered] == [c["label"] for c in relabelled]


def test_a_returned_notes_fix_is_followed_and_the_note_stays_until_the_yes(client, conn):
    key, plan_id = seed(conn, status="draft", secs=sections())
    review_id = launch_review.submit(conn, "biz_1", plan_id, approval={"kind": "web"})["review_id"]
    launch_review.decide(
        conn,
        review_id,
        state="returned",
        by="reviewer",
        note="Give Admiry the number your customers call.",
        resolution="phone",
    )
    item = _item(client, key, "phone")
    assert item["blocking"] is False and item["fix"]["tool"] == "update_business_profile"
    _follow(client, key, item, {"patch": {"locations": [{"phone": "(650) 555-0199"}]}})
    assert business.latest_knowledge(conn, "biz_1")[1]["phone"] == "(650) 555-0199"
    assert (
        _checklist(client, key)["ready"] is True and _item(client, key, "phone")["blocking"] is False
    )  # D5: until the yes


def test_a_placeholder_sent_as_a_value_is_refused_and_nothing_is_written(client, conn):
    """Review Focus 5 (plan D15)."""
    key, _ = seed(conn, status="draft", secs=sections())
    before = business.latest_knowledge(conn, "biz_1")
    for patch in (
        {"locations": [{"phone": "<the number customers call>"}]},
        {"locations": [{"address": "<the address, or the city and ZIP code>"}]},
        {"name": " <the owner's name> "},
    ):
        res = call_tool(client, "update_business_profile", {"patch": patch, "idempotency_key": "ph-1"}, key=key)
        assert res["isError"] and supporting.PLACEHOLDER_SENT in tool_text(res)
    assert business.latest_knowledge(conn, "biz_1") == before
    res = call_tool(
        client,
        "update_landing_page",
        {"action": "use_url", "url": "<the owner's page address>", "idempotency_key": "ph-2"},
        key=key,
    )
    assert res["isError"] and supporting.PLACEHOLDER_SENT in tool_text(res)
    n = conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool IN ('update_business', 'set_landing_page')"
    ).fetchone()["n"]
    assert n == 0


def test_a_placeholder_in_marketing_plan_changes_is_refused_and_nothing_is_written(client, conn, walk_fake):
    """Ruling H5: D15's placeholder refusal also covers update_marketing_plan's `changes` -- a `<…>` string anywhere
    in `changes` (not only the ask's own radius_miles placeholder) is refused with the same sentence before any
    command runs; today, unguarded, it would reach float() in tools_consult and raise ValueError instead of a
    sentence."""
    walk = planned(client, conn)
    before = plans.latest(conn, "biz_1")
    for changes in (
        {"radius_miles": "<the miles, 1 to 100>"},  # the ask's own placeholder (launch_check.ASK_ARGS["radius"])
        {"offer": "<the owner's offer>"},  # a placeholder anywhere in changes, not only the one ASK_ARGS sends
    ):
        res = call_tool(
            client, "update_marketing_plan", {"changes": changes, "idempotency_key": "ph-3"}, key=walk["key"]
        )
        assert res["isError"] and supporting.PLACEHOLDER_SENT in tool_text(res)
    assert plans.latest(conn, "biz_1")["version"] == before["version"]
    n = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool IN ('adjust', 'change_goal')").fetchone()["n"]
    assert n == 0
