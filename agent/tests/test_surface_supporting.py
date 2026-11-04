"""The supporting tools (spec 2026-10-01 §4.2, §4.4; plan G1 Task 11): the profile's changes, the accounts, the page,
the billing, the activity log, a person."""

import pytest
from fastmcp.exceptions import ToolError

from agent import (
    ads_mode,
    agreements,
    business,
    consult,
    crawl,
    envelope,
    geocode,
    google_ads_link,
    jobs,
    keys,
    landing,
    launch_check,
    listings,
    places,
    plans,
    proposals,
    site_install,
    uploads,
)
from agent.channels.google_search import adapter as google
from agent.sites import intake
from agent.surface import registry, supporting
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key, knowledge_fixture
from agent.tests.surface_helpers import WALK_TEXT, planned, settle_page, started

# A routable address, not a documentation one: the crawl guard accepts only global addresses (RFC 5737's ranges are
# not). Nothing is fetched from it: the `offline` fixture below patches the check that would fetch.
PUBLIC_IP = ["93.184.216.34"]
SITE = "https://brightsmile.example"
PAGE_HTML = "<html><body><h1>Invisalign in Palo Alto</h1><p>Call (650) 555-0100 to book.</p></body></html>"
# What a public patch takes, in the order update_business_profile's description names it
PATCH_TAKES = (
    "name, category_detail, website, pricing_hints, usp, locale, services, offers, locations, google_listing, notes"
)
PATCH_OR_DESCRIPTION = "send a patch, or a description (with or without a website) to read again, not both"
# A page's direction of exactly 1,000 characters (page_builder.MAX_DIRECTION), its last one not a space
DIRECTION = ("Lead with the same-week appointments, " * 30)[:999] + "."


def _last(conn) -> tuple:
    r = conn.execute("SELECT tool, surface_tool FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    return r["tool"], r["surface_tool"]


def _count(conn) -> int:
    """The audit rows so far: a refusal of the tool's own runs no command, so it leaves this as it was."""
    return conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]


def _passing(url: str) -> dict:
    """A landing.check()-shaped report that passes (test_tools_pages.py's `_report`): these tests are about the tools
    around the check, and its twelve rules are test_landing.py's."""
    rule = {"id": "reachable", "title": "Opens over https", "ok": True, "detail": "detail", "fix": None}
    fetched = {"status": 200, "bytes": 10, "seconds": 0.1, "final_url": url}
    return {"ok": True, "url": url, "rules": [rule], "fetched": fetched}


@pytest.fixture
def offline(monkeypatch):
    """What test_tools_pages.py patches for set_landing_page, for every test here that reaches the page check: a
    resolver that reads the reserved `.example` names as public (RFC 2606: they never resolve for real), and the
    check itself, which fetches the page, answered with a passing report for the address it was given."""
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    monkeypatch.setattr(landing, "check", lambda url, *args, **kwargs: _passing(url))


def test_a_public_patch_becomes_the_commands_by_id_changes():
    assert supporting.command_patch(
        {
            "name": "Bright Smile Dental",
            "services": [{"id": "svc_cleanings", "name": "Cleanings and exams"}],
            "offers": [{"text": "Free consultation"}],
            "locations": [{"id": "loc_1", "phone_hours": "Mon-Fri 8-6"}],
            "notes": ["Closed on Sundays."],
        }
    ) == {
        "name": "Bright Smile Dental",
        "service_changes": [{"id": "svc_cleanings", "name": "Cleanings and exams"}],
        "offer_changes": [{"text": "Free consultation"}],
        "location": {"id": "loc_1", "phone_hours": "Mon-Fri 8-6"},
        "notes_add": ["Closed on Sundays."],
    }
    with pytest.raises(ToolError, match="locations takes one object"):
        supporting.command_patch({"locations": [{"id": "loc_1"}, {"id": "loc_2"}]})


@pytest.mark.parametrize(
    ("patch", "sentence"),
    [
        ({"hours": "9-5"}, f"unknown patch fields: ['hours']; a patch takes {PATCH_TAKES}"),
        (
            {"name": "Bright Smile Dental", "phone": "650-555-0100", "ai_crawlers": False},
            f"unknown patch fields: ['ai_crawlers', 'phone']; a patch takes {PATCH_TAKES}",
        ),
        (
            {"service_changes": [{"name": "Whitening"}]},
            f"unknown patch fields: ['service_changes']; a patch takes {PATCH_TAKES}",
        ),
        ({"services": "Invisalign"}, "services must be a list of objects"),
        ({"services": ["Invisalign"]}, "services must be a list of objects"),
        ({"offers": {"text": "Free consultation"}}, "offers must be a list of objects"),
        ({"offers": [{"text": "Free consultation"}, "Free X-rays"]}, "offers must be a list of objects"),
        ({"notes": "Closed on Sundays."}, "notes must be a list of sentences"),
        ({"notes": ["Closed on Sundays.", "  "]}, "notes must be a list of sentences"),
        ({"notes": [5]}, "notes must be a list of sentences"),
        ({}, PATCH_OR_DESCRIPTION),
    ],
)
def test_a_public_patch_takes_exactly_the_keys_its_description_names(patch, sentence):
    """Final review C, minor 1 and B, Important 6 (the controller's ruling): the command takes more keys than the
    tool's description names, and those were never described to an assistant (hours and phone go inside locations): a
    key the description does not name is refused with the list it does. services, offers and notes are checked in the
    tool's own names (the command's are service_changes, offer_changes and notes_add). An empty patch changes
    nothing."""
    with pytest.raises(ToolError) as refused:
        supporting.command_patch(patch)
    assert str(refused.value) == sentence


def test_a_patch_the_tool_refuses_runs_no_command_and_hours_go_inside_locations(client, conn, walk_fake):
    """The tool's own refusals of a patch run no command and write no row; the hours the owner states go inside
    locations, and are stored."""
    walk = planned(client, conn)
    rows = _count(conn)
    for patch, sentence in (
        ({"hours": "9-5"}, f"unknown patch fields: ['hours']; a patch takes {PATCH_TAKES}"),
        ({"notes": "Closed on Sundays."}, "notes must be a list of sentences"),
        ({}, PATCH_OR_DESCRIPTION),
    ):
        res = call_tool(
            client, "update_business_profile", {"patch": patch, "idempotency_key": "pp-bad"}, key=walk["key"]
        )
        assert res["isError"] and tool_text(res) == sentence, (patch, tool_text(res))
    assert _count(conn) == rows  # refused by the tool: no command ran
    stored = call_tool(
        client,
        "update_business_profile",
        {"patch": {"locations": [{"hours": "9-5"}]}, "idempotency_key": "pp-ok"},
        key=walk["key"],
    )
    assert not stored["isError"], tool_text(stored)
    assert _last(conn) == ("update_business", "update_business_profile")
    assert business.latest_knowledge(conn, "biz_1")[1]["hours"] == "9-5"


def test_an_empty_patch_beside_a_description_is_no_second_kind_of_change(client, conn, walk_fake):
    """Final review B, minor: as blank words beside a patch are not, an empty patch beside a description is no second
    kind of change: the description is read."""
    walk = planned(client, conn)
    read = call_tool(
        client,
        "update_business_profile",
        {"patch": {}, "description": "A family dentist in Palo Alto, now with implants.", "idempotency_key": "ep1"},
        key=walk["key"],
    )
    assert not read["isError"], tool_text(read)
    assert _last(conn) == ("describe_business", "update_business_profile")


def test_a_name_that_is_not_text_is_refused_and_one_stored_before_reads_as_your_business(client, conn, walk_fake):
    """Final review B, Important 6 (open item B3): a name patched as an object is refused -- by the command, so every
    path that patches is covered. One stored as an object before that check never reaches a card: the plan's goal
    sentence says "your business"."""
    walk = planned(client, conn)
    res = call_tool(
        client,
        "update_business_profile",
        {"patch": {"name": {"en": "Bright Smile"}}, "idempotency_key": "n1"},
        key=walk["key"],
    )
    assert res["isError"] and tool_text(res) == "name must be text", tool_text(res)
    assert _last(conn) == ("update_business", "update_business_profile")
    latest = business.latest_knowledge(conn, "biz_1")[1]
    assert latest["name"] == "Bright Smile Dental"  # nothing was written
    business.add_knowledge(conn, "biz_1", {**latest, "name": {"en": "Bright Smile"}}, source="owner")
    assert plans.name_for(business.latest_knowledge(conn, "biz_1")[1]) is None
    goal = data(call_tool(client, "get_marketing_plan", {}, key=walk["key"]))["cards"]["plan"]["goal"]
    assert goal.endswith("within 5 miles of your business"), goal


def test_a_new_reading_keeps_the_listing_and_a_start_never_replaces_a_confirmed_one(
    client, conn, walk_fake, strict_surface, monkeypatch
):
    """Final review B, Important 9 (open item C2): reading the business again from a description looks nothing up. The
    Google listing on file -- the address and the map point the ads centre on -- goes to the reading (the controller's
    ruling: the model names the business with it in front of it) and stays on the new version: the profile's location
    still shows the address, and so does the next plan version's. A start looks the business up only while it has no
    listing the owner confirmed (plan 2026-10-03-agent-listing-confirm): the one on file stays, whatever Google Maps
    would find."""
    walk = planned(client, conn)
    key = walk["key"]
    listing = business.latest_knowledge(conn, "biz_1")[1]["listing"]
    # the walk picked it: the business's own, confirmed by the owner, the only kind a reading is shown
    assert listing["address"] == "500 University Ave, Palo Alto, CA" and listing["confirmed"] is True
    read = call_tool(
        client,
        "update_business_profile",
        {"description": "We are a family dentist in Palo Alto and now also do implants.", "idempotency_key": "d1"},
        key=key,
    )
    assert not read["isError"], tool_text(read)
    assert walk_fake.extract_calls[-1]["listing"] == listing
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"] == listing
    where = data(call_tool(client, "get_business_profile", {"include": ["locations"]}, key=key))["locations"][0]
    assert where["address"] == "500 University Ave, Palo Alto, CA"
    budget = call_tool(client, "update_marketing_budget", {"level": "conservative", "idempotency_key": "b1"}, key=key)
    assert not budget["isError"], tool_text(budget)
    assert plans.latest(conn, "biz_1")["sections"]["consultation"]["business"]["location"] == listing["address"]
    # the name the reading gave (the fake's, read with the listing in front of it) is the one the card leads with
    assert data(budget)["cards"]["plan"]["goal"].endswith("within 5 miles of Bright Smile Dental")
    other = places.Candidate("ChIJ2", "Bright Smile Dental", "1 Hamilton Ave, Palo Alto, CA")
    looked_up: list[str] = []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: looked_up.append(q) or [other])
    again = call_tool(client, "create_marketing_plan", {"message": WALK_TEXT}, key=key)
    assert not again["isError"], tool_text(again)
    assert looked_up == [] and listings.offer(conn, "biz_1") == []  # nothing looked up, nothing offered
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"] == listing
    where = data(call_tool(client, "get_business_profile", {"include": ["locations"]}, key=key))["locations"][0]
    assert where["address"] == "500 University Ave, Palo Alto, CA"


def test_an_assistant_unlinks_the_listing_through_the_profile_tool(client, conn, walk_fake, strict_surface):
    """Plan 2026-10-03-agent-listing-confirm, Task 5: the owner says the Google Maps listing Admiry linked is not their
    business, the assistant sends `google_listing: null`, and the profile shows none afterwards."""
    walk = planned(client, conn)
    shown = data(call_tool(client, "get_business_profile", {"include": ["business"]}, key=walk["key"]))
    assert shown["business"]["google_listing"]["name"] == "Bright Smile Dental"  # the walk linked it
    res = call_tool(
        client,
        "update_business_profile",
        {"patch": {"google_listing": None}, "idempotency_key": "gl1"},
        key=walk["key"],
    )
    assert not res["isError"], tool_text(res)
    assert _last(conn) == ("update_business", "update_business_profile")
    shown = data(call_tool(client, "get_business_profile", {"include": ["business"]}, key=walk["key"]))
    assert shown["business"]["google_listing"] is None


def test_unlinking_the_listing_the_ads_centre_was_read_from_asks_where_they_centre_again(
    client, conn, walk_fake, strict_surface
):
    walk = started(client, conn)
    key = walk["key"]
    picked = call_tool(
        client,
        "update_marketing_plan",
        {"answers": {"listing": {"choice": "gm_1"}}, "idempotency_key": "gl2"},
        key=key,
    )
    assert not picked["isError"], tool_text(picked)
    assert consult.get(conn, "biz_1", walk["session_id"])["answers"]["location"]["source"] == "known_fact"
    res = call_tool(
        client, "update_business_profile", {"patch": {"google_listing": None}, "idempotency_key": "gl3"}, key=key
    )
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["say"].endswith(listings.NO_CENTRE)
    asked = [q["id"] for q in consult.pending(consult.get(conn, "biz_1", walk["session_id"]))]
    assert "location" in asked


def _on_file(conn) -> str:
    """A claimed biz_1 with the dental fixture's knowledge on file (version 1, no listing); its key."""
    key = business_with_key(conn, claim_state="claimed")
    business.add_knowledge(conn, "biz_1", knowledge_fixture().model_dump(), source="extract")
    return key


def _address(client, key: str, address: str, ik: str) -> dict:
    patch = {"locations": [{"address": address}]}
    return call_tool(client, "update_business_profile", {"idempotency_key": ik, "patch": patch}, key=key)


def test_an_address_patch_places_the_business(client, conn, places_fake, strict_surface):
    """Walk fixes 1, Task 4: the location's address, put on the map with start's own lookup: the profile shows the
    address and the point found, and the plan's sentences may centre on it."""
    key = _on_file(conn)
    res = _address(client, key, "123 Main St, Houston", "addr-1")
    assert not res["isError"], tool_text(res)
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"]["kind"] == "address" and k["listing"]["lat"] == 29.76
    where = data(call_tool(client, "get_business_profile", {"include": ["locations"]}, key=key))["locations"][0]
    assert (where["address"], where["center"]) == ("123 Main St, Houston, TX 77002", {"lat": 29.76, "lng": -95.37})
    assert plans.address_for(k) == "123 Main St, Houston, TX 77002"
    assert plans.name_for(k) == "Bright Smile Dental"  # the website still vouches for the name


def test_an_address_the_map_cannot_place_or_a_server_without_one_is_refused_in_words(
    client, conn, places_none, strict_surface, monkeypatch
):
    key = _on_file(conn)
    res = _address(client, key, "near the mall", "addr-2")
    assert res["isError"] and tool_text(res) == geocode.NOT_PLACED
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    res = _address(client, key, "123 Main St, Houston", "addr-3")
    assert res["isError"] and tool_text(res) == geocode.NO_MAP
    assert business.latest_knowledge(conn, "biz_1")[0] == 1  # neither wrote a version


def test_a_page_direction_over_the_limit_is_refused_and_no_preview_is_made(client, conn, walk_fake):
    """Final review C, minor 13: update_landing_page says "Text over a limit is refused, never cut". A brief or an
    instruction of 1,000 characters reaches the model whole and makes a preview; one of 1,001 is refused in its own
    sentence and none is made."""
    walk = planned(client, conn)

    def previews() -> int:
        return conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"]

    def page(args: dict, ik: str) -> dict:
        return call_tool(client, "update_landing_page", {**args, "idempotency_key": ik}, key=walk["key"])

    assert len(DIRECTION) == 1000
    over = page({"action": "write", "brief": DIRECTION + "!"}, "w1")
    assert over["isError"] and tool_text(over) == "the brief takes at most 1,000 characters", tool_text(over)
    assert previews() == 0 and walk_fake.page_calls == []
    written = page({"action": "write", "brief": DIRECTION}, "w2")
    assert not written["isError"], tool_text(written)
    assert previews() == 1 and walk_fake.page_calls[-1]["brief"] == DIRECTION
    over = page({"action": "revise", "instruction": DIRECTION + "!"}, "r1")
    assert over["isError"] and tool_text(over) == "the instruction takes at most 1,000 characters", tool_text(over)
    assert previews() == 1 and len(walk_fake.page_calls) == 1
    revised = page({"action": "revise", "instruction": DIRECTION}, "r2")
    assert not revised["isError"], tool_text(revised)
    assert previews() == 2 and walk_fake.page_calls[-1]["instruction"] == DIRECTION


def test_the_profile_takes_a_patch_or_a_new_description(client, conn, walk_fake):
    walk = planned(client, conn)
    patched = call_tool(
        client,
        "update_business_profile",
        {"patch": {"locations": [{"geo_model": "storefront", "phone_hours": "Mon-Fri 8-6"}]}, "idempotency_key": "ub1"},
        key=walk["key"],
    )
    assert not patched["isError"], tool_text(patched)
    stored = business.latest_knowledge(conn, "biz_1")[1]
    assert (stored["geo_model"], stored["phone_hours"]) == ("storefront", "Mon-Fri 8-6")
    assert _last(conn) == ("update_business", "update_business_profile")
    described = call_tool(
        client,
        "update_business_profile",
        {"description": "A family dentist in Palo Alto, now with implants.", "idempotency_key": "ub2"},
        key=walk["key"],
    )
    assert not described["isError"] and _last(conn) == ("describe_business", "update_business_profile")
    assert business.latest_knowledge(conn, "biz_1")[1]["phone_hours"] == "Mon-Fri 8-6"  # a new reading keeps it
    rows = _count(conn)
    both = call_tool(
        client,
        "update_business_profile",
        {"patch": {"name": "x"}, "description": "y", "idempotency_key": "ub3"},
        key=walk["key"],
    )
    assert both["isError"] and "send a patch, or a description" in tool_text(both)
    assert _count(conn) == rows  # refused by the tool: no command ran


def test_a_new_reading_needs_a_description(client, conn, walk_fake):
    """Reading the business again needs the owner's words, with the website when there is one. A website alone, or
    blank words, is refused by the tool before any command: describe_business would refuse it for a parameter the
    public tool does not have, and that refusal would count against the business's model calls for the day. A
    patch beside a website is two kinds of change at once."""
    walk = planned(client, conn)
    rows = _count(conn)
    for args, text in (
        ({"website_url": SITE}, "a description is needed: one or two sentences on what the business does and where"),
        ({"description": "   ", "website_url": SITE}, "a description is needed"),
        ({"description": ""}, "a description is needed"),
        ({"patch": {"name": "x"}, "website_url": SITE}, "send a patch, or a description"),
    ):
        res = call_tool(client, "update_business_profile", {**args, "idempotency_key": "ub-bad"}, key=walk["key"])
        assert res["isError"] and text in tool_text(res), args
    assert _count(conn) == rows  # refused by the tool: no command ran, nothing counted against the day
    read = call_tool(
        client,
        "update_business_profile",
        {"description": "A family dentist in Palo Alto.", "website_url": SITE, "idempotency_key": "ub4"},
        key=walk["key"],
    )
    assert not read["isError"], tool_text(read)
    row = conn.execute("SELECT tool, surface_tool, args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["tool"], row["surface_tool"]) == ("describe_business", "update_business_profile")
    assert row["args_redacted"]["website_url"] == SITE


def test_blank_words_beside_a_patch_are_not_a_second_kind_of_change(client, conn, walk_fake):
    """A client that fills the strings it does not use with "" sends a patch, and the patch runs. A call with nothing
    at all is still told what to send, and no command runs for it."""
    walk = planned(client, conn)
    patched = call_tool(
        client,
        "update_business_profile",
        {"patch": {"name": "Bright Smile Dental"}, "description": "", "website_url": " ", "idempotency_key": "ub5"},
        key=walk["key"],
    )
    assert not patched["isError"], tool_text(patched)
    assert _last(conn) == ("update_business", "update_business_profile")
    rows = _count(conn)
    nothing = call_tool(client, "update_business_profile", {"idempotency_key": "ub6"}, key=walk["key"])
    assert nothing["isError"] and "send a patch, or a description" in tool_text(nothing)
    assert _count(conn) == rows


def test_connecting_an_account_runs_the_channel_command_and_a_draft_gets_the_sign_in_link(client, conn, walk_fake):
    walk = planned(client, conn)
    res = call_tool(client, "connect_account", {"channel": "newsbreak", "idempotency_key": "ca1"}, key=walk["key"])
    assert _last(conn) == ("connect_channel", "connect_account"), tool_text(res)
    draft = business_with_key(conn, biz_id="biz_2", cls="draft")
    link = call_tool(client, "connect_account", {"idempotency_key": "ca2"}, key=draft)
    assert not link["isError"] and "/connect?claim=" in data(link)["connect_url"]
    assert _last(conn) == ("request_connect_link", "connect_account")


def test_a_business_whose_claim_lapsed_gets_its_sign_in_link(client, conn, walk_fake, strict_surface):
    """Final review C, Important 1: a claim nobody verified in 14 days lapses back to a draft (jobs.claim_expire) while
    the owner's own keys stay valid, and get_marketing_state tells the owner to sign in. Asked to connect, such a
    business gets its sign-in link -- request_connect_link's own answer, whatever account the call named -- as a
    setup key does; a claimed business reaches its account's own step."""
    walk = planned(client, conn, claim_state="claimed")
    claimed = call_tool(client, "connect_account", {"idempotency_key": "ca1"}, key=walk["key"])
    assert _last(conn) == ("connect_channel", "connect_account"), tool_text(claimed)
    conn.execute(
        "UPDATE business SET owner_email = 'owner@example.com', claim_expires_at = now() - interval '1 minute' "
        "WHERE id = 'biz_1'"
    )
    assert jobs.claim_expire(conn, 0, 1)["claims_released"] == 1
    args = {"idempotency_key": "ca2", "channel": "google_search", "account_id": "123-456-7890"}
    lapsed = call_tool(client, "connect_account", args, key=walk["key"])
    assert not lapsed["isError"], tool_text(lapsed)
    assert _last(conn) == ("request_connect_link", "connect_account")
    assert set(data(lapsed)) == {"connect_url", "expires_at"}
    assert data(lapsed)["connect_url"].startswith("http://testserver/connect?claim=")
    assert google_ads_link.get(conn, "biz_1") is None  # the account the call named is not asked for


# The founder's ruling (2026-10-13): the owner chooses how the ads run -- the two ways, side by side, neither leading
WAY_CHOICES = [
    {"label": "Admiry runs them, I prepay (no Google account needed)", "args": {"mode": "managed"}},
    {
        "label": "My own Google Ads account (the 10-digit Customer ID at the top of its page)",
        "args": {"account_id": "<Customer ID>"},
    },
]


PAGE_CHOICES = [
    {"id": "admiry_builds", "label": "Admiry builds it for me", "args": {"action": "write"}},
    {"id": "assistant_designs", "label": "My assistant designs it, Admiry hosts it", "args": {"action": "upload_site"}},
    {
        "id": "own_website",
        "label": "I already have a website",
        "args": {"action": "use_url", "url": "<the owner's page address>"},
    },
]


@pytest.mark.parametrize(
    "args",
    [{}, {"mode": "managed"}, {"mode": "own_account"}, {"channel": "newsbreak"}],
    ids=["no way", "managed", "own account", "newsbreak"],
)
def test_while_the_page_is_not_settled_connect_account_asks_for_the_page_first(
    client, conn, walk_fake, monkeypatch, args
):
    """The founder's ruling (2026-10-13, from a real Muse walk: the connect-and-prepay link came before the page): an
    owner without a page settles it before Admiry asks how the ads run or takes any money. While the latest draft has
    no page settled, connect_account -- where it would ask the way or answer a step on the web -- changes nothing and
    answers the page question and its three ways (status page_first), each update_landing_page's arguments, none
    marked first. The way question comes once the page is settled."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # Admiry hosts: the assistant-designed way is offered
    walk = planned(client, conn)  # claimed, no page, no way chosen
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    rows = _count(conn)
    res = call_tool(client, "connect_account", {**args, "idempotency_key": "p1"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    channel = args.get("channel", "google_search")
    assert data(res) == {
        "status": "page_first",
        "channel": channel,
        "label": {"google_search": "Google Search", "newsbreak": "NewsBreak"}[channel],
        "ask_owner": launch_check.PAGE_QUESTION,
        "choices": PAGE_CHOICES,
    }
    assert env["say"] == launch_check.PAGE_QUESTION and not JARGON.search(env["say"])
    assert env["next"] == envelope.actions(*launch_check.page_actions(PAGE_CHOICES))
    assert all(a["tool"] == "update_landing_page" and "primary" not in a for a in env["next"]["actions"])
    # nothing written but the command's own row: no way, no link, no agreement, no payment
    assert _count(conn) == rows + 1 and _last(conn) == ("connect_channel", "connect_account")
    assert not ads_mode.chosen(conn, "biz_1") and google_ads_link.get(conn, "biz_1") is None
    assert conn.execute("SELECT count(*) AS n FROM payment_session").fetchone()["n"] == 0
    log = data(call_tool(client, "get_activity_log", {}, key=walk["key"]))["entries"]
    assert log[0]["sentence"].endswith(" asked how you want the page for your ads"), log[0]
    # the page settled: the way question, as before
    settle_page(conn)
    again = call_tool(client, "connect_account", {"idempotency_key": "p2"}, key=walk["key"])
    assert data(again)["status"] == "choose", tool_text(again)


def test_a_customer_id_the_owner_gave_is_no_question_of_the_page(client, conn, walk_fake):
    """The page question stands in for the way question and for a step on the web only: a Customer ID the owner gave
    goes the invitation's way (the agreement first) as before."""
    walk = planned(client, conn)
    args = {"account_id": "123-456-7890", "idempotency_key": "p3"}
    res = call_tool(client, "connect_account", args, key=walk["key"])
    assert data(res)["status"] == "needs_agreement", tool_text(res)


def test_with_no_way_chosen_connect_account_asks_the_owner_which_way_and_changes_nothing(client, conn, walk_fake):
    """The founder's ruling (2026-10-13): never choose for the owner. Asked to connect Google Search with neither a mode
    nor a Customer ID, for a business that has chosen no way yet (its page settled: the page comes first),
    connect_account changes nothing: it answers the question to put to the owner, in their words, and the two ways side
    by side -- each with the arguments that pick it, neither marked first."""
    walk = planned(client, conn)  # claimed, no way chosen: no link, no agreement, the default mode
    settle_page(conn)
    res = call_tool(client, "connect_account", {"idempotency_key": "w1"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert data(res) == {
        "status": "choose",
        "channel": "google_search",
        "label": "Google Search",
        "ask_owner": ads_mode.WAY_QUESTION,
        "choices": WAY_CHOICES,
    }
    question = ads_mode.WAY_QUESTION
    assert question.startswith("How should your ads run?") and "prepaid balance" in question
    assert "no Google account needed" in question and "your own Google Ads account" in question
    assert env["say"] == question and not JARGON.search(question)
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {"id": c["id"], "label": w["label"], "tool": "connect_account", "args": w["args"]}
            for c, w in zip(ads_mode.WAY_CHOICES, WAY_CHOICES, strict=True)
        ],
    }
    assert not any("primary" in a for a in env["next"]["actions"])  # neither way is chosen for the owner
    # nothing changed: no link, no way, no agreement -- and one row, the command's own
    assert google_ads_link.get(conn, "biz_1") is None and not ads_mode.chosen(conn, "biz_1")
    assert ads_mode.current(conn, "biz_1") == "own_account" and not agreements.accepted(conn, "biz_1")
    assert _last(conn) == ("connect_channel", "connect_account")
    log = data(call_tool(client, "get_activity_log", {}, key=walk["key"]))["entries"]
    assert log[0]["sentence"].endswith(" asked how you want your ads to run"), log[0]  # never "asked to connect"


def test_each_way_the_owner_picks_takes_its_own_path(client, conn, walk_fake):
    """A choice's arguments, sent back with the owner's own value, take the path that existed before the question:
    Admiry running the ads answers the plan's agree-and-pay link with nothing changed yet; the owner's own account goes the
    invitation's way (the agreement first, then the invitation). A placeholder sent as it is is refused, nothing
    written. (The page is settled: it comes first.)"""
    walk = planned(client, conn)
    settle_page(conn)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    managed, own = (
        c["args"]
        for c in data(call_tool(client, "connect_account", {"idempotency_key": "w1"}, key=walk["key"]))["choices"]
    )
    run_it = call_tool(client, "connect_account", {**managed, "idempotency_key": "w2"}, key=walk["key"])
    assert not run_it["isError"], tool_text(run_it)
    assert data(run_it)["status"] == "web_step" and data(run_it)["steps"] == [
        {"text": google.CONNECT_MANAGED, "url": None}
    ]
    # the first payment's link: the plan's own agree-and-pay page, its page settled (founder's ruling 3, 2026-10-13)
    assert data(run_it)["connect_url"] == f"http://testserver/agree/{walk['plan_id']}"
    rows = _count(conn)
    placeholder = call_tool(client, "connect_account", {**own, "idempotency_key": "w3"}, key=walk["key"])
    assert placeholder["isError"] and tool_text(placeholder) == supporting.CUSTOMER_ID_PLACEHOLDER
    assert _count(conn) == rows and google_ads_link.get(conn, "biz_1") is None
    mine = {**own, "account_id": "123-456-7890"}
    first = call_tool(client, "connect_account", {**mine, "idempotency_key": "w4"}, key=walk["key"])
    assert data(first)["status"] == "needs_agreement", tool_text(first)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    sent = call_tool(client, "connect_account", {**mine, "idempotency_key": "w5"}, key=walk["key"])
    assert data(sent)["status"] == "pending" and google_ads_link.get(conn, "biz_1").status == "pending"


@pytest.mark.parametrize("way", ["managed", "linked", "agreed"])
def test_a_business_that_already_runs_one_way_is_not_asked_again(client, conn, walk_fake, way):
    """A way already chosen -- Admiry running the ads, an account linked, or the agreement the owner accepted on the
    way to their own account -- is not asked about again: connect_account answers as it did before the question (the
    page settled: it comes first)."""
    walk = planned(client, conn)
    settle_page(conn)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    if way == "managed":
        ads_mode.change(conn, "biz_1", "managed")
    if way == "linked":
        google_ads_link.save(conn, "biz_1", "1234567890", status="active")
    if way == "agreed":
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    res = call_tool(client, "connect_account", {"idempotency_key": "n1"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert ads_mode.chosen(conn, "biz_1") and "choices" not in data(res)
    expected = {
        "managed": ("web_step", google.CONNECT_MANAGED),
        "linked": ("connected", None),
        "agreed": ("web_step", google.CONNECT_OWN_ACCOUNT),
    }[way]
    assert data(res)["status"] == expected[0]
    if expected[1]:
        assert data(res)["steps"][0]["text"] == expected[1]


def test_the_page_is_read_three_ways(client, conn, walk_fake, offline):
    walk = planned(client, conn)
    status = call_tool(client, "get_landing_page", {}, key=walk["key"])
    website = {"url": "https://brightsmile.example", "platform": "other", "code": "missing", "checked_at": None}
    assert not status["isError"] and data(status) == {
        "page": None,
        "check": None,
        "upload": None,
        "website": {**website, "way": None},  # spec §4 (A3p-5 batch 2): the site, before any page is set
    }
    assert (
        status["structuredContent"]["say"]
        == "No landing page is set yet. Admiry can write one, or you can name your own."
    )
    assert _last(conn) == ("get_page", "get_landing_page")
    kit = call_tool(client, "get_landing_page", {"view": "kit"}, key=walk["key"])
    assert not kit["isError"] and "facts" in data(kit) and _last(conn) == ("get_page_kit", "get_landing_page")
    assert [(a["tool"], a["args"]) for a in kit["structuredContent"]["next"]["actions"]] == [
        ("update_landing_page", {"action": "use_url"})
    ]
    check = call_tool(client, "get_landing_page", {"view": "check"}, key=walk["key"])
    assert check["isError"] and "There is no page to check yet" in tool_text(check)
    candidate = f"{SITE}/invisalign"
    tried = call_tool(client, "get_landing_page", {"view": "check", "candidate_url": candidate}, key=walk["key"])
    assert not tried["isError"], tool_text(tried)
    row = conn.execute("SELECT tool, surface_tool, args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["tool"], row["surface_tool"], row["args_redacted"]["url"]) == (
        "check_landing_page",
        "get_landing_page",
        candidate,
    )
    rows = _count(conn)
    for args, text in (
        ({"view": "everything"}, "view must be status, check, kit, source or site"),
        ({"candidate_url": "https://example.com"}, "candidate_url goes with view check"),
    ):
        bad = call_tool(client, "get_landing_page", args, key=walk["key"])
        assert bad["isError"] and text in tool_text(bad)
    assert _count(conn) == rows  # refused by the tool: no command ran


def test_each_page_action_runs_its_command_and_says_what_it_lacks(client, conn, walk_fake, offline):
    walk = planned(client, conn)
    written = call_tool(client, "update_landing_page", {"action": "write", "idempotency_key": "pg1"}, key=walk["key"])
    assert _last(conn) == ("create_page", "update_landing_page"), tool_text(written)
    rows = _count(conn)
    for args, text in (
        ({"action": "revise"}, "revise needs instruction"),
        ({"action": "edit", "section": "headline"}, "edit needs section and text"),
        ({"action": "edit", "text": ""}, "edit needs section and text"),
        ({"action": "use_url"}, "use_url needs url"),
        ({"action": "preview_html"}, "preview_html needs html"),
        ({"action": "send_install_steps"}, "send_install_steps needs email"),  # A3p-5 batch 2: the web person's
        ({"action": "send_install_steps", "email": ""}, "send_install_steps needs email"),
        (
            {"action": "burn"},
            (
                "action must be one of write, revise, edit, use_url, preview_html, upload_site, publish, unpublish, "
                "send_install_steps, request_install_help, build_site"
            ),
        ),
        ({"action": "publish", "owner_confirmed": True}, "send the same token or version you asked with"),
        ({"action": "publish", "owner_confirmed": True, "token": "pv_nope"}, "nothing is waiting for the owner's yes"),
        ({"action": "publish", "owner_confirmed": True, "version": 9}, "nothing is waiting for the owner's yes"),
        ({"action": "unpublish", "owner_confirmed": True}, "nothing is waiting for the owner's yes"),
    ):
        bad = call_tool(client, "update_landing_page", {**args, "idempotency_key": "pg-bad"}, key=walk["key"])
        assert bad["isError"] and text in tool_text(bad), args
    assert _count(conn) == rows  # each refused by the tool: no command ran
    used = call_tool(
        client,
        "update_landing_page",
        {"action": "use_url", "url": "https://brightsmile.example/invisalign", "idempotency_key": "pg2"},
        key=walk["key"],
    )
    assert _last(conn) == ("set_landing_page", "update_landing_page"), tool_text(used)


def test_every_page_action_reaches_its_command(client, conn, walk_fake, offline):
    """Each way update_landing_page goes, once, through the tool: the audit row names the command and the public tool.
    What the command then answers is its own business (nothing is hosted in tests, so publishing is refused, and
    there is no hosted page to take down; the page Admiry wrote is the ads' now, so the two ways to Admiry's code on the
    owner's own site refuse in the row's words, A3p-5 batch 2; and Admiry's own sites are off, so build_site is turned
    away, A3p-6 batch 2)."""
    walk = planned(client, conn)
    written = call_tool(client, "update_landing_page", {"action": "write", "idempotency_key": "pa1"}, key=walk["key"])
    assert not written["isError"], tool_text(written)
    paths = (
        ({"action": "revise", "instruction": "make it warmer"}, "revise_page"),
        ({"action": "edit", "section": "hours", "text": ""}, "update_page"),  # empty: the business's own hours again
        ({"action": "preview_html", "html": PAGE_HTML}, "preview_page"),
        ({"action": "upload_site"}, "request_site_upload"),  # A3p-3 batch 3: refused (nothing is hosted in tests)
        ({"action": "publish", "token": data(written)["token"]}, "publish_page"),
        ({"action": "publish", "version": 1}, "publish_page"),
        ({"action": "unpublish"}, "unpublish_page"),
        ({"action": "send_install_steps", "email": "web@agency.example"}, "send_install_steps"),
        ({"action": "request_install_help", "contact": "owner@example.com"}, "request_install_help"),
        ({"action": "build_site", "style": "clean"}, "build_site"),  # A3p-6 batch 2 (refused: the sites are off)
    )
    reached = []
    for i, (args, _command) in enumerate(paths):
        rows = _count(conn)
        call_tool(client, "update_landing_page", {**args, "idempotency_key": f"pa{i + 2}"}, key=walk["key"])
        assert _count(conn) == rows + 1, args  # this call's own row: a refusal before dispatch would leave none
        reached.append(_last(conn))
    assert reached == [(command, "update_landing_page") for _args, command in paths]


def test_the_owners_yes_confirms_the_page_it_names_not_the_newest(client, conn, walk_fake):
    """Two previews wait for the owner's yes. A yes sent with the first one's token runs confirm_proposal on the
    first one's proposal. (What the command then does is its own business: these tokens are no real previews, so it
    refuses, and the refusal's audit row names the proposal that was asked for.)"""
    walk = planned(client, conn)
    waiting = {"cls": "nonspend", "kind": "publish_page", "what_changes": []}
    first = proposals.create(
        conn,
        "biz_1",
        payload={"token": "pv_one", "hostname": "bright.example"},
        summary="Publish one",
        approval_url="https://agent.example/p/pv_one",
        **waiting,
    )
    proposals.create(
        conn,
        "biz_1",
        payload={"token": "pv_two", "hostname": "bright.example"},
        summary="Publish two",
        approval_url="https://agent.example/p/pv_two",
        **waiting,
    )
    call_tool(
        client,
        "update_landing_page",
        {"action": "publish", "token": "pv_one", "owner_confirmed": True, "idempotency_key": "yes-1"},
        key=walk["key"],
    )
    row = conn.execute("SELECT tool, surface_tool, args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["tool"], row["surface_tool"]) == ("confirm_proposal", "update_landing_page")
    assert row["args_redacted"]["proposal_id"] == first["proposal_id"]


def test_the_owners_yes_to_a_version_and_to_taking_the_page_down(client, conn, walk_fake):
    """The yes to republishing a version runs confirm_proposal on that version's proposal, and the yes to taking the
    page down on the open unpublish_page proposal (one hosted page per business). The command then refuses both
    (nothing is hosted in tests); each refusal's audit row names the proposal that was asked for."""
    walk = planned(client, conn)
    waiting = {"cls": "nonspend", "what_changes": [], "approval_url": "https://bright.example/"}
    republish = proposals.create(
        conn,
        "biz_1",
        kind="publish_page",
        payload={"version": 3, "hostname": "bright.example"},
        summary="Republish version 3",
        **waiting,
    )
    take_down = proposals.create(
        conn, "biz_1", kind="unpublish_page", payload={}, summary="Take down the page", **waiting
    )
    for i, (args, proposal) in enumerate(
        (({"action": "publish", "version": 3}, republish), ({"action": "unpublish"}, take_down))
    ):
        call_tool(
            client,
            "update_landing_page",
            {**args, "owner_confirmed": True, "idempotency_key": f"yes-{i}"},
            key=walk["key"],
        )
        row = conn.execute(
            "SELECT tool, surface_tool, args_redacted FROM agent_action ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (row["tool"], row["surface_tool"]) == ("confirm_proposal", "update_landing_page"), args
        assert row["args_redacted"]["proposal_id"] == proposal["proposal_id"], args


def test_a_yes_from_a_key_the_tool_cannot_resolve_gets_the_commands_refusal(client, conn):
    """The tool says "nothing is waiting" only to a caller it knows (Task 10's rule). A key without the manage scope
    reaches confirm_proposal, which refuses it in its own words and records the attempt under both names."""
    business_with_key(conn, claim_state="verified")
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    for i, args in enumerate(({"action": "publish", "token": "pv_one"}, {"action": "unpublish"})):
        res = call_tool(
            client, "update_landing_page", {**args, "owner_confirmed": True, "idempotency_key": f"r{i}"}, key=reader
        )
        assert res["isError"] and "lacks the 'manage' scope" in tool_text(res), args
        assert _last(conn) == ("confirm_proposal", "update_landing_page"), args


def test_fix_headline_from_a_key_the_tool_cannot_resolve_gets_the_commands_refusal(client, conn):
    """Review round 1, I1: fix_headline reads the business through `where` before it has anything to dispatch, so a
    key `where` cannot resolve (here, one without the manage scope) must still be handed to a real command -- else
    it would be refused in the wrong words (blockers.PLAN_MOVED, from a plan_id of None) with no row recorded, the
    way update_landing_page's own owner's-yes branches already avoid for confirm_proposal."""
    business_with_key(conn, claim_state="verified")
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    res = call_tool(client, "update_landing_page", {"action": "fix_headline", "idempotency_key": "r1"}, key=reader)
    assert res["isError"] and "lacks the 'manage' scope" in tool_text(res)
    assert _last(conn) == ("build_site", "update_landing_page")
    row = conn.execute("SELECT ok, result FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ok"] is False and row["result"].get("denied") is True


def test_billing_has_three_views_and_the_log_reads_as_sentences(client, conn, walk_fake):
    walk = planned(client, conn)
    for view, command in (
        ("position", "get_balance"),
        ("payments", "list_payments"),
        ("topup_link", "request_payment_link"),
    ):
        call_tool(client, "get_billing", {"view": view}, key=walk["key"])
        assert _last(conn) == (command, "get_billing"), view
    rows = _count(conn)
    bad = call_tool(client, "get_billing", {"view": "invoice"}, key=walk["key"])
    assert bad["isError"] and "view must be position, payments or topup_link" in tool_text(bad)
    assert _count(conn) == rows  # refused by the tool: no command ran
    log = call_tool(client, "get_activity_log", {"limit": 5}, key=walk["key"])
    assert not log["isError"] and len(data(log)["entries"]) == 5
    assert _last(conn) == ("get_audit_log", "get_activity_log")


def test_the_descriptions_say_what_the_tools_do_and_which_read_the_web():
    """get_billing: the whole topup_link view needs the manage scope, and amount_usd is only checked against the
    allowed range (the link carries no amount). update_landing_page: the page comes down and the ads are paused with
    it. update_business_profile: a new reading needs a description. The three tools that crawl a site or fetch a page
    say so (openWorldHint), as create_marketing_plan does."""
    tools = {meta.name: meta.tool for meta in registry.SURFACE}
    said = {name: " ".join(tool.description.split()) for name, tool in tools.items()}
    assert 'view "topup_link" (needs the manage scope)' in said["get_billing"]
    assert "amount_usd, when given, is only checked against that range" in said["get_billing"]
    assert "suggests" not in said["get_billing"]
    assert (
        "take Admiry's hosted page down; the ads that send people to it are paused with it"
        in said["update_landing_page"]
    )
    assert "pause first" not in said["update_landing_page"]
    assert "`description` (required:" in said["update_business_profile"]
    assert "with it, `website_url` when there is a site" in said["update_business_profile"]
    for name in ("update_business_profile", "get_landing_page", "update_landing_page"):
        assert tools[name].annotations.openWorldHint is True, name


def test_the_page_tools_describe_admirys_code_on_the_owners_site(conn):
    """A3p-5 batch 2 (plan Q12, spec §4): update_landing_page's description names its two new actions -- each with its
    argument, what it does and what it answers -- and get_landing_page's names every key of the status's `website`
    block and of the kit's `install` part, as the code builds them (site_install.status, site_install.kit_part)."""
    said = {meta.name: " ".join((meta.tool.description or "").split()) for meta in registry.SURFACE}
    page = said["update_landing_page"]
    # the web person's e-mail (plan Q7, spec W11): one address the owner gives, a fixed message, replies to the owner
    assert "- \"send_install_steps\": while the owner's website still needs Admiry's code" in page
    assert "at `email` (the one address the owner gives)" in page
    assert "one fixed message from Admiry, with no words of yours in it, and replies go to the owner" in page
    assert f"at most {site_install.SENDS_PER_DAY} a day" in page
    assert "the owner's own address or a fourth in a day is refused in a sentence, and nothing is sent" in page
    assert "never send the steps to an address the owner did not give you" in page
    # Admiry's team (plan Q8, spec W12): a person reaches the owner, who adds a helper -- never a password
    assert '- "request_install_help": while the owner\'s website still needs the code' in page
    assert "at `contact` (an e-mail address), else at their sign-in address" in page
    assert "add a helper in the platform's own settings, never for a password" in page
    assert "the same call with `contact` sends the request on" in page
    # the status's `website` block and the kit's `install` part (spec §4), key by key
    look = said["get_landing_page"]
    business_with_key(conn)
    business.add_knowledge(conn, "biz_1", {"name": "Bright Smile Dental", "website": SITE}, source="owner")
    block = site_install.status(conn, "biz_1")
    part = site_install.kit_part({"website": SITE}, "<script></script>")
    assert set(block) == {"url", "platform", "code", "checked_at", "way"}
    for key in ("website", "install", *block, *part):
        assert f"`{key}`" in look, key
    assert "(`code`: missing or found)" in look
    assert "(`way`: send or help; admiry_page while the ads use an Admiry page; null before any)" in look
    assert "and `say` says so when Admiry's hourly look finds the code gone from the owner's page" in look


# ---------------------------------------------------------------- the site Admiry builds (A3p-6 batch 2 Task 6, plan Q15)

SITES_OFF = "Admiry's own sites are not on for this server yet."


def test_the_site_view_is_read_through_the_page_tool_and_says_when_sites_are_off(client, conn, walk_fake, monkeypatch):
    """Plan Q15: `get_landing_page` with view "site" runs get_site -- the dental walk's business is a family of sites --
    and, with Admiry's own sites off, answers the command's sentence in the row's words. candidate_url still goes with
    view check only."""
    walk = planned(client, conn)
    off = call_tool(client, "get_landing_page", {"view": "site"}, key=walk["key"])
    assert off["isError"] and tool_text(off) == SITES_OFF
    assert _last(conn) == ("get_site", "get_landing_page")
    monkeypatch.setenv("AGENT_SITES", "1")
    seen = call_tool(client, "get_landing_page", {"view": "site"}, key=walk["key"])
    assert not seen["isError"], tool_text(seen)
    assert _last(conn) == ("get_site", "get_landing_page")
    view = data(seen)
    assert (view["family"], view["status"]) == ("dental", "none")
    assert [look["id"] for look in view["looks"]] == ["calm", "warm", "boutique"]
    rows = _count(conn)
    bad = call_tool(client, "get_landing_page", {"view": "site", "candidate_url": SITE}, key=walk["key"])
    assert bad["isError"] and "candidate_url goes with view check" in tool_text(bad)
    assert _count(conn) == rows  # refused by the tool: no command ran


def test_the_site_view_hands_the_upload_link_only_to_a_key_that_may_manage(client, conn, walk_fake, monkeypatch):
    """Final review I2: the phone link changes the site's photos -- a key that may only read sees the view with
    `upload_url` null, the walk's key (it may manage) sees the link, and the description says so."""
    monkeypatch.setenv("AGENT_SITES", "1")
    walk = planned(client, conn)
    token = uploads.new_token(conn, landing.ensure_page_for_kit(conn, "biz_1")["id"])  # a link the owner was given
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    seen = data(call_tool(client, "get_landing_page", {"view": "site"}, key=reader))
    assert seen["family"] == "dental" and seen["upload_url"] is None
    managing = data(call_tool(client, "get_landing_page", {"view": "site"}, key=walk["key"]))
    assert managing["upload_url"] is not None and managing["upload_url"].endswith(f"/u/{token}")
    look = " ".join((next(m for m in registry.SURFACE if m.name == "get_landing_page").tool.description or "").split())
    assert "only for a key that may manage" in look


def test_build_site_hands_its_four_arguments_to_the_command(client, conn, walk_fake, monkeypatch):
    """Plan Q15: `update_landing_page` with action build_site passes style, accent, facts and wordmark on as they are, and
    every argument is optional. The command keeps what it is given on the page's draft -- the colour, the facts, the
    wordmark -- and asks the look (the dental walk's business has every fact Admiry needs); a look of the other family is
    refused in the command's own words. Its row names the command and the tool, and the keys of the facts -- never what
    they say."""
    monkeypatch.setenv("AGENT_SITES", "1")
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")  # hosting is on: the command checks it before anything
    walk = planned(client, conn)
    said = {"years": 12, "warranty": "Every filling is guaranteed for a year."}
    args = {"action": "build_site", "accent": "#1d4ed8", "facts": said, "wordmark": True}

    asked = call_tool(client, "update_landing_page", {**args, "idempotency_key": "bs1"}, key=walk["key"])

    assert not asked["isError"], tool_text(asked)
    assert asked["structuredContent"]["next"]["question"]["id"] == "style"
    assert _last(conn) == ("build_site", "update_landing_page")
    draft = intake.draft(landing.current(conn, "biz_1"))
    assert (draft["style"], draft["accent"], draft["wordmark_ok"], draft["facts"]) == (None, "#1D4ED8", True, said)
    row = conn.execute("SELECT args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()["args_redacted"]
    # a row keeps each argument as JSON text: the facts as the sorted keys alone
    assert (row["style"], row["accent"], row["facts"], row["wordmark"]) == (
        "null",
        "#1d4ed8",
        '["warranty", "years"]',
        "true",
    )
    assert "Every filling" not in str(row)
    wrong = call_tool(
        client,
        "update_landing_page",
        {"action": "build_site", "style": "bold", "idempotency_key": "bs2"},
        key=walk["key"],
    )
    assert wrong["isError"] and "calm, warm or boutique" in tool_text(wrong)
    assert _last(conn) == ("build_site", "update_landing_page")
    start = call_tool(
        client, "update_landing_page", {"action": "build_site", "idempotency_key": "bs3"}, key=walk["key"]
    )
    assert not start["isError"]  # every argument is optional: a call with none asks the next thing
    assert start["structuredContent"]["next"]["question"]["id"] == "style"


def test_a_nul_character_in_a_fact_is_taken_out_and_never_an_internal_error(client, conn, walk_fake, monkeypatch):
    """Fix round 1: JSON can say a NUL character ("\\u0000") and JSONB cannot hold one, so `facts={"about": "a\\u0000b"}`
    sent through the public tool used to end in an internal error. Every text of a patch -- a fact, an item of a list, a
    part of an object, the answer to the place question -- comes out of it clean, and the call answers as it would have."""
    monkeypatch.setenv("AGENT_SITES", "1")
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)
    nul = "\x00"
    said = {
        "about": f"a{nul}b",
        "languages": [f"Span{nul}ish", "English"],
        "reviews": [{"text": f"Fast{nul} and fair.", "first_name": f"Da{nul}na", "source": "Google"}],
        "place": f"500 University{nul} Ave, Palo Alto",
    }

    res = call_tool(
        client,
        "update_landing_page",
        {"action": "build_site", "facts": said, "idempotency_key": "nul1"},
        key=walk["key"],
    )

    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["next"]["question"]["id"] == "style"  # answered: the look is the next question
    facts = intake.draft(landing.current(conn, "biz_1"))["facts"]
    assert facts["about"] == "ab" and facts["languages"] == ["Spanish", "English"]
    assert facts["reviews"] == [{"text": "Fast and fair.", "first_name": "Dana", "source": "Google"}]
    assert "\x00" not in str(facts) and "\x00" not in str(business.latest_knowledge(conn, "biz_1")[1])
    assert _last(conn) == ("build_site", "update_landing_page")


def test_the_looks_are_a_read_tool_published_as_it_is(client, conn, walk_fake, monkeypatch, strict_surface):
    """Plan Q15: get_site_templates is the command itself on the surface -- same name, a read tool a read key may call
    -- and, with Admiry's own sites off, answers the same sentence."""
    planned(client, conn)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    meta = next(m for m in registry.SURFACE if m.name == "get_site_templates")
    assert (meta.cls, meta.scope, meta.approval, meta.commands) == ("RO", "read", "none", ("get_site_templates",))
    assert meta.tool.annotations.readOnlyHint is True and meta.tool.annotations.openWorldHint is False
    off = call_tool(client, "get_site_templates", {}, key=reader)
    assert off["isError"] and tool_text(off) == SITES_OFF
    monkeypatch.setenv("AGENT_SITES", "1")
    res = call_tool(client, "get_site_templates", {}, key=reader)
    assert not res["isError"], tool_text(res)
    families = data(res)["families"]
    assert [(f["id"], [look["id"] for look in f["styles"]]) for f in families] == [
        ("home_services", ["solid", "clean", "bold"]),
        ("dental", ["calm", "warm", "boutique"]),
    ]
    assert all(
        look["look_url"].startswith("http://testserver/static/sites/looks/") for f in families for look in f["styles"]
    )
    assert _last(conn) == ("get_site_templates", "get_site_templates")


def test_the_page_tools_describe_the_site_admiry_builds(client, conn, walk_fake, monkeypatch):
    """Plan Q15, Q16 (spec T26): update_landing_page's description names the build_site action with its four arguments
    and the keys a patch of facts takes (`intake.SITE_FACTS`, in the code's order), says what each call answers, that a
    photo reaches Admiry only through the upload link and that an upload ends the earlier preview, where a profile fact is
    read back and where `write` and `revise` stand for a family business -- or for one whose page Admiry already wrote;
    its safety line comes first, so a host that cuts a long description cuts a bullet, never that. get_landing_page's
    names the site view and every key of what the view answers, as the code builds it."""
    said = {meta.name: " ".join((meta.tool.description or "").split()) for meta in registry.SURFACE}
    page = said["update_landing_page"]
    assert '- "build_site": for a home-services business' in page
    for argument in ("`style`", "`accent`", "`facts`", "`wordmark: true`"):
        assert argument in page, argument
    assert f"(keys: {', '.join(intake.SITE_FACTS)})" in page and "`place` is the answer to the place question" in page
    assert "A fact is text unless it is: `services` a list of names" in page and "`reviews` a list of {text" in page
    assert (
        "its `id` is name, phone, place or services" in page
        and "then each fact Admiry still needs, one at a time" in page
    )
    # the look comes first, then the facts (the coordinator's ruling, the spec's §2 flow)
    assert page.index("which look the owner wants (its `id` is style") < page.index("then each fact Admiry still needs")
    assert "a photo reaches Admiry only through that link" in page
    assert "what still keeps it from going live" in page
    assert '"write" starts "build_site" instead' in page and "do not apply to a site Admiry built" in page
    assert "Publishing takes the owner's yes in the same two calls as any page" in page
    # fix round 1: an upload rebuilds the preview and ends the earlier one -- the token is the view's, not the build's
    assert (
        "The owner's uploads rebuild the preview and end the earlier link: when they say the photos are in, read "
        '`get_landing_page` with `view: "site"` and publish with the `token` it shows now.'
    ) in page
    # ... a page Admiry already wrote ...
    assert "A business whose page Admiry already wrote is told a site in its place comes with the next release" in page
    assert '"write" rewrites that page (brief and template apply)' in page
    # ... the facts the profile holds are read back from the profile, not from the view's `facts`
    held = f"{', '.join(intake.PROFILE_FACTS[:-1])} and {intake.PROFILE_FACTS[-1]}"
    assert f"{held} are written to the business's profile, which the ads and the pages share" in page
    assert "read them back with get_business_profile, not from the view's `facts`" in page
    # the safety line first: before the first bullet, once, and the tail keeps the rest
    line = (
        "IMPORTANT ACTION: never set owner_confirmed without the owner's yes, never name a page with use_url that the owner "
        "did not give you, and never send the steps to an address the owner did not give you."
    )
    assert page.startswith(f"Give the ads their page. {line} action is one of: - ")
    assert page.count("IMPORTANT ACTION") == 1 and page.endswith(
        "The yes confirms that page and no other. Use a new idempotency_key per call."
    )
    # the view's keys, read from a real answer: a dental practice with nothing built, then a checklist's items
    monkeypatch.setenv("AGENT_SITES", "1")
    walk = planned(client, conn)
    view = data(call_tool(client, "get_landing_page", {"view": "site"}, key=walk["key"]))
    look = said["get_landing_page"]
    assert 'view "site": the website Admiry builds' in look
    assert set(view["checklist"][0]) == {"id", "label", "need", "have", "done", "blocks_publish"}
    assert set(view["key_photos"]) == {"ok", "logo", "key", "wordmark_ok", "sentence"}
    for key in (*view, *view["checklist"][0], *view["key_photos"]):
        assert f"`{key}`" in look, key
    for key in ("url", "token", "expires_at", "hostname", "version", "check"):  # the preview's and the live site's own
        assert f"`{key}`" in look, key
    # `preview.render` (a preview's own: `loads` and `partial`, null when the render check did not run)
    assert "`render`" in look and "`loads`" in look and "`partial`" in look


def test_the_arguments_of_another_action_sent_beside_build_site_neither_reach_the_command_nor_change_the_answer(
    client, conn, walk_fake, monkeypatch
):
    """Fix round 1: a caller that sends `owner_confirmed`, a `token` and a `version` beside action build_site -- an
    assistant that fills every argument it has -- gets the answer it would have had without them: nothing is published or
    confirmed, no proposal is made, and the command's row holds the four arguments it takes and no other."""
    monkeypatch.setenv("AGENT_SITES", "1")
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)
    plain = call_tool(
        client, "update_landing_page", {"action": "build_site", "idempotency_key": "st1"}, key=walk["key"]
    )
    plain_row = conn.execute("SELECT args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    stray = {
        "owner_confirmed": True,
        "token": "pv_somebody_elses",
        "version": 3,
        "email": "x@y.example",
        "contact": "z",
    }
    crowded = call_tool(
        client, "update_landing_page", {"action": "build_site", **stray, "idempotency_key": "st2"}, key=walk["key"]
    )
    crowded_row = conn.execute("SELECT args_redacted FROM agent_action ORDER BY id DESC LIMIT 1").fetchone()
    assert not plain["isError"] and not crowded["isError"], (tool_text(plain), tool_text(crowded))
    assert crowded["structuredContent"] == plain["structuredContent"]
    assert _last(conn) == ("build_site", "update_landing_page")
    assert crowded_row["args_redacted"] == plain_row["args_redacted"]
    assert set(crowded_row["args_redacted"]) == {"style", "accent", "facts", "wordmark"}
    for tool in ("confirm_proposal", "publish_page", "send_install_steps", "request_install_help"):
        assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = %s", (tool,)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 0
