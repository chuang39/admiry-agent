import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from agent import (
    auth,
    benchmark,
    business,
    config,
    consult,
    crawl,
    db,
    listings,
    llm,
    places,
    plans,
    proposals,
    tools_business,
    tools_start,
)
from agent.tests import listings_fakes
from agent.tests.conftest import TEST_DB_URL, call_tool, data, rpc, rpc_raw, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture

IP = "198.51.100.9"


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(
        places,
        "search",
        # the business as Google Maps lists it, close by its website and its phone (plan
        # 2026-10-03-agent-listing-confirm): offered to the owner, never stored until they pick it
        lambda query, key, **kw: [
            places.Candidate(
                "ChIJ1",
                "Bright Smile Dental",
                "500 University Ave, Palo Alto, CA",
                37.4419,
                -122.143,
                website="https://brightsmile.example",
                phone="(650) 555-0100",
            )
        ],
    )
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    return gen


# ---------------------------------------------------------------- start (keyed, A1b-4b Task 4)


def test_keyed_start_attaches_to_the_tokens_business(client, conn, fake):
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    first = call_tool(client, "start", {"text": "dentist in Palo Alto, $2,000 a month"}, key=key)
    assert not first["isError"], tool_text(first)
    d = first["structuredContent"]["data"]
    assert d["business_id"] == "biz_1" and d["draft_key"] is None and d["connect_url"] == "http://testserver/connect"
    sid = d["session_id"]
    second = call_tool(client, "start", {"text": "actually an orthodontist in Palo Alto"}, key=key)
    d2 = second["structuredContent"]["data"]
    assert d2["business_id"] == "biz_1" and d2["session_id"] != sid
    assert consult.get(conn, "biz_1", sid)["status"] == "superseded"
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM knowledge WHERE business_id = 'biz_1'").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE class = 'draft'").fetchone()["n"] == 0
    biz = conn.execute("SELECT owner_email, claim_state FROM business").fetchone()
    assert (biz["owner_email"], biz["claim_state"]) == ("owner@example.com", "claimed")


def test_keyed_start_never_erases_an_owner_stated_limit(client, conn, fake):
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    call_tool(client, "set_goal", {"monthly_budget_usd": 900, "idempotency_key": "g1"}, key=key)
    fake.knowledge.goal_hint.monthly_budget_usd = None
    call_tool(client, "start", {"text": "dentist"}, key=key)
    goal = conn.execute("SELECT monthly_limit_usd FROM goal ORDER BY created_at DESC LIMIT 1").fetchone()
    assert float(goal["monthly_limit_usd"]) == 900


def test_keyed_start_keeps_the_previous_focus_services_when_the_words_name_none(client, conn, fake):
    """Final review: a target without focus services carries the earlier goal's focus forward, like the limit."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(
        client,
        "set_goal",
        {"monthly_budget_usd": 900, "focus_services": ["Invisalign"], "idempotency_key": "g1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    fake.knowledge.goal_hint.monthly_budget_usd = None
    fake.knowledge.goal_hint.focus_services = []
    call_tool(client, "start", {"text": "dentist, 20 new patients a month"}, key=key)
    goal = conn.execute("SELECT target, focus_services FROM goal ORDER BY created_at DESC LIMIT 1").fetchone()
    assert goal["target"] == 20 and list(goal["focus_services"]) == ["Invisalign"]


def test_start_is_marked_open_world(client, conn):
    """start reads the website and Google Maps: openWorldHint is true although it is a W tool."""
    key = business_with_key(conn)
    tools = {t["name"]: t for t in rpc(client, "tools/list", key=key)["result"]["tools"]}
    assert tools["start"]["annotations"]["openWorldHint"] is True
    assert tools["set_goal"]["annotations"]["openWorldHint"] is False


def test_start_without_a_key_is_401(client):
    assert rpc_raw(client, "tools/call", {"name": "start", "arguments": {"text": "dentist"}}).status_code == 401


def test_platform_key_cannot_start(client, conn, fake):
    key = business_with_key(conn, cls="platform")
    res = call_tool(client, "start", {"text": "dentist"}, key=key)
    assert res["isError"] and "not bound to a business" in tool_text(res)


WORDS = "Family dentist in Palo Alto, we want more Invisalign patients"
NAME, ADDRESS = "Bright Smile Dental", "500 University Ave, Palo Alto, CA"
# start's lookup of the dental fixture's business: the reading's name, then the first area it names (listings.lookup)
LOOKED_UP = f"{NAME} Palo Alto"
PHONE = "650-555-0100"  # the dental fixture's phone: a place with it is close to the business (listings.close)


@pytest.mark.parametrize(
    ("known", "looked_up"),
    [
        ({"business_name": NAME, "address": ADDRESS}, [f"{NAME} {ADDRESS}", ADDRESS]),
        (
            {"business_name": "Smile Studio"},
            ["Smile Studio Palo Alto"],
        ),  # the name an assistant knows, not the reading's
        (
            {"business_name": f"  {NAME} ", "address": {"street": "500 University Ave"}},
            [f"{NAME} Palo Alto"],
        ),  # cleaned
        (  # cut like the owner's words are
            {"business_name": "N" * 150, "address": "A" * 150},
            [("N" * 150 + " " + "A" * 150)[: listings.QUERY_CHARS], "A" * 150],
        ),
        ({"address": ADDRESS}, [f"{NAME} {ADDRESS}", ADDRESS]),  # no name given: the reading's, at the address
        ({"business_name": "   "}, [f"{NAME} Palo Alto"]),  # a blank name is no name
        (None, [f"{NAME} Palo Alto"]),
    ],
    ids=["name and address", "name", "cleaned first", "cut", "address alone", "blank name", "no known facts"],
)
def test_start_looks_the_business_up_by_the_name_an_assistant_already_knows(
    client, conn, fake, places_search, known, looked_up
):
    """The business's lookup under the same daily budget, after the reading (plan 2026-10-03-agent-listing-confirm):
    by the name an assistant knows, else the reading's, and where it is -- the known address, else the first area the
    reading names; never the owner's own words. It finds nothing here, so a known address is then put on the map with
    a lookup of its own (round 1)."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    args = {"text": WORDS} | ({"known_facts": known} if known is not None else {})
    res = call_tool(client, "start", args, key=key)
    assert not res["isError"], tool_text(res)
    assert places_search.queries == looked_up


def test_start_looks_the_business_up_by_its_name_after_reading_it_and_stores_nothing_it_found(
    conn, fake, places_search
):
    benchmark.seed(conn)
    places_search.found = [listings_fakes.found(website="https://brightsmile.example")]
    res = tools_start.begin_anonymous("dentist in Palo Alto", "https://brightsmile.example", ip=IP)
    assert places_search.queries == ["Bright Smile Dental Palo Alto"]  # the reading's name, then where it is
    assert fake.extract_calls[0]["listing"] is None  # the reading saw no lookup
    biz = res["data"]["business_id"]
    assert "listing" not in business.latest_knowledge(conn, biz)[1]  # nothing linked until the owner picks
    assert [c["place_id"] for c in listings.offer(conn, biz)] == ["ChIJ1"]
    q = res["next"]["question"]
    assert q["id"] == "listing" and q["options"][-1] == consult.LISTING_NONE
    assert "Is your business one of these on Google Maps? Options: Bright Smile Dental, " in res["say"]
    assert "listing_question" not in res["data"]


def test_start_without_a_name_looks_nothing_up(conn, fake, places_search):
    benchmark.seed(conn)
    fake.knowledge = fake.knowledge.model_copy(update={"name": None})
    res = tools_start.begin_anonymous("dentist in Palo Alto", ip=IP)
    assert places_search.queries == [] and res["next"]["question"]["id"] != "listing"


def test_a_place_only_named_like_the_business_elsewhere_is_not_offered(conn, fake, places_search):
    benchmark.seed(conn)
    places_search.found = [listings_fakes.found(address="1 Main St, Austin, TX 78701, USA")]
    res = tools_start.begin_anonymous("dentist in Palo Alto", ip=IP)
    assert res["next"]["question"]["id"] != "listing"
    assert listings.offer(conn, res["data"]["business_id"]) == []


def test_a_business_with_a_confirmed_listing_is_not_looked_up_again(client, conn, fake, places_search):
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    business.add_knowledge(
        conn, "biz_1", listings.link({"name": "Bright Smile Dental"}, listings_fakes.offered()), source="owner"
    )
    res = call_tool(client, "start", {"text": "dentist in Palo Alto"}, key=key)
    assert not res["isError"], tool_text(res)
    assert places_search.queries == []


def test_a_new_start_never_offers_a_place_the_owner_said_is_not_theirs(client, conn, fake, places_search):
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    business.add_knowledge(
        conn, "biz_1", {"name": "Bright Smile Dental", "listing_rejected": ["ChIJ1"]}, source="owner"
    )
    places_search.found = [listings_fakes.found()]
    res = data(call_tool(client, "start", {"text": "dentist in Palo Alto"}, key=key))
    assert listings.offer(conn, "biz_1") == [] and res["session_id"]


def test_create_marketing_plan_looks_the_business_up_by_its_name_and_address(client, conn, fake, places_search):
    """Through the public tool: known_facts' business_name and location's address reach the lookup together (and,
    the business not found, the address is put on the map next: round 1)."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    args = {"message": WORDS, "known_facts": {"business_name": NAME}, "location": {"address": ADDRESS}}
    res = call_tool(client, "create_marketing_plan", args, key=key)
    assert not res["isError"], tool_text(res)
    assert places_search.queries == [f"{NAME} {ADDRESS}", ADDRESS]


# Round 1: an address an assistant passes (location.address), for a business Google Maps does not list
HOUSTON = places.Candidate("ChIJ2", "123 Main St", "123 Main St, Houston, TX 77002, USA", 29.76, -95.37)
KNOWN = "123 Main St, Houston"


def _searched(found: dict, queries: list[str]):
    """A Places fake on `places.search`, which an address put on the map goes through too (`search_text`): each query
    recorded, answered with the place `found` holds for it (none for any other)."""

    def search(query, key, **kw):
        queries.append(query)
        return [found[query]] if found.get(query) else []

    return search


def _begun_with_an_address(client, conn, monkeypatch, address_found) -> tuple[dict, list[str]]:
    """create_marketing_plan on a claimed biz_1 with the owner's words and location.address KNOWN: the business's own
    lookup finds nothing, the address's finds `address_found`. Returns (the answer's envelope, the queries made)."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    queries: list[str] = []
    monkeypatch.setattr(places, "search", _searched({KNOWN: address_found}, queries))
    res = call_tool(client, "create_marketing_plan", {"message": WORDS, "location": {"address": KNOWN}}, key=key)
    assert not res["isError"], tool_text(res)
    return {**res["structuredContent"], "key": key}, queries


def test_a_known_address_is_put_on_the_map_and_its_question_is_pre_answered(client, conn, fake, monkeypatch):
    """Walk fixes 1, Task 4 round 1 (the controller's ruling, which create_marketing_plan's words will promise): the
    address an assistant passes for a business Google Maps does not list is put on the map with the same lookup, under
    the same budget and cap; the profile adopts it as update_business adopts one -- an address, its town among the
    areas -- and the location question is pre-answered from its point. A later start puts it on the map again: the
    address an assistant passes always centres the ads (round 2)."""
    env, queries = _begun_with_an_address(client, conn, monkeypatch, HOUSTON)
    assert queries == [f"{NAME} {KNOWN}", KNOWN]  # the business (the reading's name, at the address), then the address
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"] == {**HOUSTON.as_json(), "kind": "address"} and k["service_area"] == ["Palo Alto", "Houston"]
    assert _location_answer(conn, env["data"]["session_id"]) == {
        "address": HOUSTON.address,
        "lat": 29.76,
        "lng": -95.37,
        "source": "known_fact",
    }
    # no website named: the website, the name, the services and the listing question the name may still find places
    # for (ruling F1), then four
    assert "a few quick questions" in env["say"]  # eight (ruling R0-h)
    assert auth.places_lookups_today(conn, "biz_1") == 2  # every call to Google counts: the business's, the address's
    again = call_tool(
        client, "create_marketing_plan", {"message": WORDS, "location": {"address": KNOWN}}, key=env["key"]
    )
    assert not again["isError"], tool_text(again)
    assert queries == [f"{NAME} {KNOWN}", KNOWN, f"{NAME} {KNOWN}", KNOWN]
    assert auth.places_lookups_today(conn, "biz_1") == 4
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"] == k["listing"]


def _placed_answer(conn, env) -> dict | None:
    return _location_answer(conn, env["data"]["session_id"])


HOUSTON_ANSWER = {"address": HOUSTON.address, "lat": 29.76, "lng": -95.37, "source": "known_fact"}
BRIGHT = places.Candidate("ChIJ1", NAME, ADDRESS, 37.4419, -122.143)  # the business on Google Maps, in Palo Alto


def test_an_address_in_the_owners_own_words_centres_the_ads_as_a_known_one(client, conn, fake, monkeypatch):
    """A street address or ZIP code in the owner's opening words (the reading's stated_address) is put on the map as the
    address an assistant passes is, and its question is not asked (the founder's dev walk, 2026-10-04). The reading's
    field itself is not kept on the profile: the place on the map is."""
    fake.knowledge.stated_address = KNOWN
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    queries: list[str] = []
    monkeypatch.setattr(places, "search", _searched({KNOWN: HOUSTON}, queries))
    res = call_tool(client, "create_marketing_plan", {"message": WORDS}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert queries == [f"{NAME} {KNOWN}", KNOWN]  # the business at the address its owner gave, then the address
    assert _location_answer(conn, env["data"]["session_id"]) == HOUSTON_ANSWER
    # no website named: the website, the name, the services and the listing question the name may still find places
    # for (ruling F1), then four
    assert "a few quick questions" in env["say"]  # eight (ruling R0-h)
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"] == {**HOUSTON.as_json(), "kind": "address"} and "stated_address" not in k


def test_the_homepages_first_turn_puts_the_owners_address_on_the_map_too(conn, fake, monkeypatch):
    """The homepage's first turn (begin_anonymous, no business yet) reads an address in the words the same way."""
    fake.knowledge.stated_address = KNOWN
    benchmark.seed(conn)
    queries: list[str] = []
    monkeypatch.setattr(places, "search", _searched({KNOWN: HOUSTON}, queries))
    res = tools_start.begin_anonymous(WORDS, ip=IP)
    assert queries == [f"{NAME} {KNOWN}", KNOWN]
    assert _location_answer(conn, res["data"]["session_id"]) == HOUSTON_ANSWER
    # the website, the name, the services and the listing question the name may still find places for (ruling F1)
    assert "Before I plan anything, a few quick questions." in res["say"]  # eight (ruling R0-h)
    k = business.latest_knowledge(conn, res["data"]["business_id"])[1]
    assert k["listing"] == {**HOUSTON.as_json(), "kind": "address"} and "stated_address" not in k


def _pick(client, key: str, session_id: str, choice: str = "gm_1") -> dict:
    """The owner's answer to the listing question: the envelope of `answer`."""
    args = {"session_id": session_id, "answers": {"listing": {"choice": choice}}, "idempotency_key": f"pick-{choice}"}
    res = call_tool(client, "answer", args, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def _site(client, key: str, session_id: str) -> dict:
    """The owner's answer to the website question, asked first when the words name none (plan
    2026-10-05-agent-a3p-5-1, Q15): the fixture site, which `fake` crawls and reads as the dental fixture, so the
    consultation goes on as before it. The envelope of `answer`."""
    args = {"session_id": session_id, "answers": {"website": {"other": "https://brightsmile.example"}}}
    res = call_tool(client, "answer", {**args, "idempotency_key": "site"}, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def test_a_known_address_centres_the_ads_and_the_business_its_name_finds_is_offered(client, conn, fake, monkeypatch):
    """Walk fixes 1, Task 4 round 2 (the controller's ruling): the address an assistant passes is where the ads centre,
    always. The business Google Maps finds by its name and that address is offered, never stored (plan
    2026-10-03-agent-listing-confirm): the knowledge holds the address alone until the owner picks it. Picked, the
    business's listing -- its name and place id -- is moved to the placed address and its point, and vouches for the
    name; the location question stays pre-answered from the address."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    queries: list[str] = []
    monkeypatch.setattr(places, "search", _searched({f"{NAME} {KNOWN}": BRIGHT, KNOWN: HOUSTON}, queries))
    args = {"message": WORDS, "known_facts": {"business_name": NAME}, "location": {"address": KNOWN}}
    res = call_tool(client, "create_marketing_plan", args, key=key)
    assert not res["isError"], tool_text(res)
    assert queries == [f"{NAME} {KNOWN}", KNOWN]
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"] == {**HOUSTON.as_json(), "kind": "address"}  # nothing the lookup found is stored
    assert [c["place_id"] for c in listings.offer(conn, "biz_1")] == ["ChIJ1"]
    assert _placed_answer(conn, res["structuredContent"]) == HOUSTON_ANSWER
    picked = _pick(client, key, res["structuredContent"]["data"]["session_id"])
    said = picked["say"]  # the ads already centre on the address: the pick does not move them
    assert said.startswith("Linked to Bright Smile Dental on Google Maps.") and "The ads centre on" not in said
    k = business.latest_knowledge(conn, "biz_1")[1]
    moved = {**BRIGHT.as_json(), "address": HOUSTON.address, "lat": 29.76, "lng": -95.37, "placed": True}
    assert k["listing"] == {**moved, "confirmed": True, "confirmed_at": k["listing"]["confirmed_at"]}
    assert plans.name_for({**k, "website": None}) == NAME  # the listing the owner confirmed vouches for the name
    assert k["service_area"] == ["Palo Alto", "Houston"]
    assert _placed_answer(conn, res["structuredContent"]) == HOUSTON_ANSWER


def test_a_known_address_centres_the_ads_and_another_business_found_is_not_offered(client, conn, fake, monkeypatch):
    """No business name given: the lookup, by the name the reading gave and the address, finds another business on the
    map -- not close to this one, so nothing is offered and nothing stored (plan 2026-10-03-agent-listing-confirm); the
    address the assistant passed is placed all the same and centres the ads."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    other = places.Candidate("ChIJ7", "Palo Alto Dental Care", "1 Main St, Palo Alto, CA 94301, USA", 37.44, -122.16)
    queries: list[str] = []
    monkeypatch.setattr(places, "search", _searched({f"{NAME} {KNOWN}": other, KNOWN: HOUSTON}, queries))
    res = call_tool(client, "create_marketing_plan", {"message": WORDS, "location": {"address": KNOWN}}, key=key)
    assert not res["isError"], tool_text(res)
    assert queries == [f"{NAME} {KNOWN}", KNOWN]
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"] == {**HOUSTON.as_json(), "kind": "address"} and listings.offer(conn, "biz_1") == []
    assert _placed_answer(conn, res["structuredContent"]) == HOUSTON_ANSWER


# Final review A1: a business Google Maps finds that is not this one (nearly any words find one)
OTHER = places.Candidate("ChIJ7", "Palo Alto Dental Care", "1 Main St, Palo Alto, CA 94301, USA", 37.44, -122.16)
AUSTIN = places.Candidate("ChIJ9", "4 Elm St", "4 Elm St, Austin, TX 78701, USA", 30.27, -97.74)


def _placed_by_the_owner(client, conn, monkeypatch, *, confirmed: bool) -> tuple[str, dict]:
    """A claimed biz_1 begun from WORDS, whose lookup offers the business (BRIGHT) -- which the owner picks when
    `confirmed` -- then KNOWN put on the map by the owner's profile change (update_business). Returns (the key, the
    Places fake's answers by query: a test changes them)."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    found = {f"{NAME} Palo Alto": BRIGHT, KNOWN: HOUSTON, "4 Elm St, Austin": AUSTIN}
    monkeypatch.setattr(places, "search", _searched(found, []))
    begun = call_tool(client, "start", {"text": WORDS}, key=key)
    assert not begun["isError"], tool_text(begun)
    if confirmed:
        _pick(client, key, data(begun)["session_id"])
    patch = {"patch": {"location": {"address": KNOWN}}, "idempotency_key": "placed-1"}
    placed = call_tool(client, "update_business", patch, key=key)
    assert not placed["isError"], tool_text(placed)
    return key, found


@pytest.mark.parametrize("confirmed", [False, True], ids=["an address placed alone", "the listing moved to it"])
def test_a_new_start_keeps_the_place_the_owner_put_on_the_map(client, conn, fake, monkeypatch, confirmed):
    """Walk fixes 1, final review A1: the owner put the ads' centre on the map -- an address placed alone, or the
    business's listing they confirmed moved to it. A new start does not displace it: the place is adopted again after
    the new reading (profile.apply), and the location question is pre-answered from it. The new start's lookup finds a
    business that is not this one, or none is made (a listing the owner confirmed is on file): nothing is offered and
    nothing stored (plan 2026-10-03-agent-listing-confirm). So again on the start after that."""
    key, found = _placed_by_the_owner(client, conn, monkeypatch, confirmed=confirmed)
    found[f"{NAME} Palo Alto"] = OTHER  # the business's name finds another business on the map now
    for _ in range(2):
        res = call_tool(client, "start", {"text": WORDS}, key=key)
        assert not res["isError"], tool_text(res)
        k = business.latest_knowledge(conn, "biz_1")[1]
        if confirmed:
            moved = {**BRIGHT.as_json(), "address": HOUSTON.address, "lat": 29.76, "lng": -95.37, "placed": True}
            assert k["listing"] == {**moved, "confirmed": True, "confirmed_at": k["listing"]["confirmed_at"]}
            assert plans.name_for({**k, "website": None}) == NAME
        else:
            assert k["listing"] == {**HOUSTON.as_json(), "kind": "address"}
            assert plans.name_for({**k, "website": None}) is None  # an address names no one
        assert listings.offer(conn, "biz_1") == []
        assert _placed_answer(conn, res["structuredContent"]) == HOUSTON_ANSWER


def test_a_new_start_that_names_an_address_centres_on_that_one(client, conn, fake, monkeypatch):
    """Final review A1: a start that states an address (location.address) puts that one on the map, as before: the
    place the owner put there earlier gives way to it, and the business's listing they confirmed moves to it."""
    key, _found = _placed_by_the_owner(client, conn, monkeypatch, confirmed=True)
    args = {"message": WORDS, "location": {"address": "4 Elm St, Austin"}}
    res = call_tool(client, "create_marketing_plan", args, key=key)
    assert not res["isError"], tool_text(res)
    k = business.latest_knowledge(conn, "biz_1")[1]
    moved = {**BRIGHT.as_json(), "address": AUSTIN.address, "lat": 30.27, "lng": -97.74, "placed": True}
    assert k["listing"] == {**moved, "confirmed": True, "confirmed_at": k["listing"]["confirmed_at"]}
    assert _placed_answer(conn, res["structuredContent"]) == {
        "address": AUSTIN.address,
        "lat": 30.27,
        "lng": -97.74,
        "source": "known_fact",
    }


@pytest.mark.parametrize("why", ["no place found", "no lookups left"])
def test_a_known_address_that_cannot_be_placed_leaves_its_question_to_ask(client, conn, fake, monkeypatch, why):
    if why == "no lookups left":
        monkeypatch.setattr(auth, "PLACES_LOOKUPS_PER_DAY", 0)
    env, _ = _begun_with_an_address(client, conn, monkeypatch, None if why == "no place found" else HOUSTON)
    assert _location_answer(conn, env["data"]["session_id"]) is None  # asked: the bare address answers nothing
    assert "listing" not in business.latest_knowledge(conn, "biz_1")[1]
    # no website named: the website, the name, the services and the listing question the name may still find places
    # for (ruling F1), then five: nine (ruling R0-h)
    assert "a few quick questions" in env["say"]


def test_without_a_places_key_a_known_address_is_not_looked_up_and_its_question_is_skipped(
    client, conn, fake, monkeypatch
):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    env, queries = _begun_with_an_address(client, conn, monkeypatch, HOUSTON)
    assert queries == []
    assert _location_answer(conn, env["data"]["session_id"]) == {"skipped": True, "source": "known_fact"}
    # no website named: the website, the name and the services, then four -- seven (ruling R0-h)
    assert "a few quick questions" in env["say"]


def _location_answer(conn, session_id: str) -> dict | None:
    return conn.execute("SELECT answers->'location' AS a FROM consult_session WHERE id = %s", (session_id,)).fetchone()[
        "a"
    ]


@pytest.mark.parametrize(
    ("found", "keyed", "answer", "count", "first"),
    [
        # nine, nine, nine and seven: "a few" (ruling R0-h)
        (listings_fakes.found(website="https://brightsmile.example"), True, None, "a few", "listing"),
        (None, True, None, "a few", "objective"),
        (OTHER, True, None, "a few", "objective"),
        (None, False, {"skipped": True, "source": "known_fact"}, "a few", "objective"),
    ],
    ids=["the business found", "nothing found", "another business found", "no map lookup on this server"],
)
def test_the_first_turn_asks_where_the_ads_centre_only_when_it_can_place_the_answer(
    conn, fake, monkeypatch, found, keyed, answer, count, first
):
    """Walk fixes 1, Task 4: a server with no map lookup skips the question; otherwise it is one of the questions (the
    homepage's first turn and start share this). A place the lookup finds answers nothing before the owner picks it
    (plan 2026-10-03-agent-listing-confirm): the business found is offered first among the trade's questions, another
    business not at all. The words name no website: the website, the name and the services come before them (plan
    2026-10-05-agent-a3p-5-1), and with no place offered the listing question counts while the name may still find one
    (ruling F1) -- never on a server with no map lookup, where no place can come."""
    benchmark.seed(conn)
    if not keyed:
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    monkeypatch.setattr(places, "search", lambda query, key, **kw: [found] if found else [])
    res = tools_start.begin_anonymous(WORDS, ip=IP)
    assert _location_answer(conn, res["data"]["session_id"]) == answer
    assert f"Before I plan anything, {count} quick questions." in res["say"]
    assert res["next"]["question"]["id"] == "website"
    assert _trades_first(conn, res) == first


def _trades_first(conn, res: dict) -> str:
    """The first of the trade's own questions a first turn's session waits on (the common ones aside)."""
    session = consult.get(conn, res["data"]["business_id"], res["data"]["session_id"])
    return next(q["id"] for q in consult.pending(session) if not q.get("common"))


# ---------------------------------------------------------------- begin_anonymous (the pre-Task-4 anonymous
# `start`, called directly now that it is no longer an /mcp tool)


def test_begin_anonymous_creates_a_draft_business_goal_session_and_key(client, conn, fake):
    benchmark.seed(conn)
    res = tools_start.begin_anonymous(
        "Family dentist in Palo Alto, want 20 new patients a month, $2,000/mo",
        "https://brightsmile.example",
        ip=IP,
    )
    d = res["data"]
    assert d["draft_key"].startswith("ak_") and d["connect_url"].startswith("http://testserver/connect?claim=")
    n = conn.execute("SELECT count(*) AS n FROM claim_link WHERE business_id = %s", (d["business_id"],)).fetchone()["n"]
    assert n == 1
    audited = conn.execute("SELECT result FROM agent_action WHERE tool = 'start' ORDER BY id DESC LIMIT 1").fetchone()[
        "result"
    ]
    assert audited["data"]["connect_url"] == "***" and "claim=" not in str(
        audited
    )  # the claim token never enters the audit row
    assert "listing_question" not in d and res["next"]["question"]["id"] == "listing"  # the place found is asked about
    assert d["session_id"].startswith("cs_") and res["next"]["kind"] == "question"
    assert conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"] == 0
    assert fake.plan_calls == []  # start no longer drafts a plan: recommend does, after the questions
    biz = conn.execute("SELECT claim_state, status FROM business WHERE id = %s", (d["business_id"],)).fetchone()
    assert (biz["claim_state"], biz["status"]) == ("draft", "active")
    # nothing the lookup found is stored: the offer holds it until the owner picks (plan 2026-10-03-agent-listing-confirm)
    assert conn.execute("SELECT json->'listing' AS l FROM knowledge").fetchone()["l"] is None
    assert [c["place_id"] for c in listings.offer(conn, d["business_id"])] == ["ChIJ1"]
    goal = conn.execute("SELECT target, monthly_limit_usd FROM goal").fetchone()
    assert (goal["target"], float(goal["monthly_limit_usd"])) == (
        20,
        2000.0,
    )  # the goal is still recorded from the words
    assert (
        fake.extract_calls[0]["pages"][0]["text"] == "We do Invisalign"
        and fake.extract_calls[0]["listing"] is None  # the lookup comes after the reading, which never sees it
    )
    key = conn.execute("SELECT class, scopes, expires_at > now() + interval '23 hours' AS day FROM api_key").fetchone()
    assert (key["class"], list(key["scopes"]), key["day"]) == ("draft", ["read", "manage"], True)
    who = data(call_tool(client, "whoami", {}, key=d["draft_key"]))
    assert who["business"]["id"] == d["business_id"] and who["key"]["class"] == "draft"
    assert who["open_session_id"] == d["session_id"]
    audit = conn.execute(
        "SELECT actor, business_id, result->'data'->>'draft_key' AS k FROM agent_action WHERE tool = 'start'"
    ).fetchone()
    assert (audit["actor"], audit["business_id"], audit["k"]) == ("ip:198.51.100.9", d["business_id"], "***")


def test_begin_anonymous_without_a_website_or_places_key_skips_those_steps(conn, fake, monkeypatch):
    benchmark.seed(conn)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    monkeypatch.setattr(tools_start.crawl, "fetch_site", lambda url: pytest.fail("no crawl without a website"))
    monkeypatch.setattr(places, "search", lambda q, k, **kw: pytest.fail("no lookup without a key"))
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # the model read no site either
    res = tools_start.begin_anonymous("dentist in Palo Alto", ip=IP)
    assert "listing_question" not in res["data"] and res["next"]["question"]["id"] != "listing"
    # no website and no listing vouch for the model's name, so the opening sentence does not lead with it
    # (Muse round, 2026-09-28: "Palo Alto dental practice is a dental practice.")
    assert res["data"]["understanding"]["name"] is None
    assert res["say"].startswith("I can build that for you. You run a family dentist.")


def test_begin_anonymous_skips_places_when_the_daily_budget_is_spent(conn, fake, monkeypatch):
    benchmark.seed(conn)
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "1")
    conn.execute(  # the day's one call to Google: another visitor's first turn looked its business up
        "INSERT INTO agent_action (actor, actor_class, tool, args_hash, ok) "
        "VALUES ('places', 'system', 'places.lookup', 'h', true)"
    )
    calls = []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: calls.append(q) or [])
    tools_start.begin_anonymous("dentist in Palo Alto", ip=IP)
    assert calls == []


def test_starts_lookup_reads_the_days_calls_to_google_and_nothing_else(conn, fake, monkeypatch):
    """Walk fixes 1, Task 4 round 2, final review A2: start's own lookup of the business reads the day's Places budget
    as every lookup does (auth.places_today): the day's calls to Google, a `places.lookup` row each -- an address put
    on the map, another start's lookup -- and nothing else; a `start` row is no call. With the budget at 2, a start row
    and one lookup today, the business is looked up, its own row the day's second; the next start's is not."""
    benchmark.seed(conn)
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "2")
    conn.execute(
        "INSERT INTO agent_action (actor, actor_class, tool, args_hash, ok, ip) "
        "VALUES ('ip:1.1.1.1', 'anonymous', 'start', 'h', true, '1.1.1.1')"
    )
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, actor_class, tool, args_hash, ok) "
        "VALUES ('biz_x', 'places', 'system', 'places.lookup', 'h', true)"
    )
    calls = []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: calls.append(q) or [listings_fakes.found(phone=PHONE)])
    first = tools_start.begin_anonymous("dentist in Palo Alto", ip=IP)
    assert calls == [LOOKED_UP] and auth.places_today(conn) == 2 and _trades_first(conn, first) == "listing"
    res = tools_start.begin_anonymous("dentist in Palo Alto", ip="198.51.100.10")
    assert calls == [LOOKED_UP] and _trades_first(conn, res) != "listing"


def test_starts_own_lookup_is_a_row_like_every_call_to_google(client, conn, fake):
    """Final review A2: start's lookup of the business is recorded as every call to Google is (geocode.record_lookup):
    one `places.lookup` row with the words looked up (the business's name and where it is: plan
    2026-10-03-agent-listing-confirm) and whether they found a place -- for the business, or for none on the homepage's
    first turn, which has no business yet. Those rows are all the day's Places budget counts."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    assert not call_tool(client, "start", {"text": WORDS}, key=key)["isError"]
    tools_start.begin_anonymous(WORDS, ip=IP)
    rows = conn.execute(
        "SELECT business_id, actor, args_redacted, result FROM agent_action WHERE tool = 'places.lookup' ORDER BY id"
    ).fetchall()
    assert [(r["business_id"], r["actor"], r["args_redacted"], r["result"]) for r in rows] == [
        ("biz_1", "places", {"text": LOOKED_UP}, {"found": True}),
        (None, "places", {"text": LOOKED_UP}, {"found": True}),
    ]
    assert auth.places_today(conn) == 2 and auth.places_lookups_today(conn, "biz_1") == 1


def test_a_refused_start_spends_none_of_the_places_budget(client, conn, fake, monkeypatch):
    """Final review A2: a `start` refused before any lookup -- a dead hand-off, refused before the model and Places --
    makes no call to Google, so it records no `places.lookup` row and leaves the day's Places budget as it was: refused
    calls cannot spend the budget every business's placements share."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    calls = []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: calls.append(q) or [])
    for _ in range(3):
        res = call_tool(client, "start", {"text": WORDS, "handoff": "hs_dead"}, key=key)
        assert res["isError"] and tools_start.HANDOFF_DEAD in tool_text(res)
    assert calls == [] and auth.places_today(conn) == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'places.lookup'").fetchone()["n"] == 0


def test_starts_lookup_keeps_to_the_businesss_cap_and_a_first_turn_to_the_days_budget(client, conn, fake, monkeypatch):
    """Final review A2: start's lookup is one of the business's calls to Google: a business whose lookups for the day
    are spent (auth.PLACES_LOOKUPS_PER_DAY) is not looked up again. The homepage's first turn has no business yet: its
    lookups (rows for no business) are bounded by the day's budget alone, however many of them there are."""
    benchmark.seed(conn)
    monkeypatch.setattr(auth, "PLACES_LOOKUPS_PER_DAY", 1)
    calls = []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: calls.append(q) or [])
    key = business_with_key(conn, claim_state="claimed")
    for _ in range(2):
        assert not call_tool(client, "start", {"text": WORDS}, key=key)["isError"]
    assert calls == [LOOKED_UP]  # the second start: the business's one lookup of the day is spent
    for ip in (IP, "198.51.100.10"):
        tools_start.begin_anonymous(WORDS, ip=ip)
    assert calls == [LOOKED_UP] * 3  # two first turns, two rows for no business: both looked up


def test_begin_anonymous_is_limited_per_address(conn, fake):
    benchmark.seed(conn)
    for _ in range(3):
        tools_start.begin_anonymous("dentist", ip=IP)
    with pytest.raises(ToolError, match="retry_after="):
        tools_start.begin_anonymous("dentist", ip=IP)
    tools_start.begin_anonymous("dentist", ip="198.51.100.10")  # a different address: not limited


def test_begin_anonymous_is_serialised_per_address(conn, fake):
    benchmark.seed(conn)
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('start:198.51.100.9'))")  # a start in flight
        with pytest.raises(ToolError, match="in progress"):
            tools_start.begin_anonymous("dentist", ip=IP)
        assert conn.execute("SELECT count(*) AS c FROM business").fetchone()["c"] == 0
        assert conn.execute("SELECT count(*) AS c FROM agent_action").fetchone()["c"] == 0  # unrecorded
        assert fake.extract_calls == []
        other.execute("SELECT pg_advisory_unlock(hashtext('start:198.51.100.9'))")
    tools_start.begin_anonymous("dentist", ip=IP)


def test_begin_anonymous_stops_at_the_global_daily_budget(conn, fake, monkeypatch):
    benchmark.seed(conn)
    monkeypatch.setenv("AGENT_START_DAILY_BUDGET", "1")
    conn.execute(
        "INSERT INTO agent_action (actor, actor_class, tool, args_hash, ok, ip) "
        "VALUES ('ip:1.1.1.1', 'anonymous', 'start', 'h', false, '1.1.1.1')"
    )  # failures count too
    with pytest.raises(ToolError, match="busy today; try again tomorrow"):
        tools_start.begin_anonymous("dentist", ip=IP)
    assert conn.execute("SELECT count(*) AS c FROM agent_action").fetchone()["c"] == 1  # the refusal is not recorded
    assert fake.extract_calls == []


def test_start_daily_budget_defaults_to_500_and_empty_counts_as_unset(monkeypatch):
    monkeypatch.delenv("AGENT_START_DAILY_BUDGET", raising=False)
    assert config.load().start_daily_budget == 500
    monkeypatch.setenv("AGENT_START_DAILY_BUDGET", "")  # compose passes "" for an unset var
    assert config.load().start_daily_budget == 500
    monkeypatch.setenv("AGENT_START_DAILY_BUDGET", "7")
    assert config.load().start_daily_budget == 7


def test_begin_anonymous_without_an_llm_key_fails_before_any_places_lookup(conn, monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    looked_up, crawled = [], []
    monkeypatch.setattr(places, "search", lambda q, k, **kw: looked_up.append(q) or [])
    monkeypatch.setattr(tools_start.crawl, "fetch_site", lambda url: crawled.append(url) or [])
    with pytest.raises(ToolError, match="not configured"):
        tools_start.begin_anonymous("dentist in Palo Alto", "https://brightsmile.example", ip=IP)
    assert looked_up == [] and crawled == []


def test_begin_anonymous_drops_an_out_of_range_goal_hint_with_a_note(conn, fake):
    benchmark.seed(conn)
    fake.knowledge.goal_hint.monthly_budget_usd = 1e9  # "a billion a month", as the model read it
    res = tools_start.begin_anonymous("dentist in Palo Alto, 20 patients a month, a billion a month", ip=IP)
    goal = conn.execute("SELECT target, monthly_limit_usd FROM goal").fetchone()
    assert (goal["target"], goal["monthly_limit_usd"]) == (20, None)
    assert fake.plan_calls == []  # start no longer drafts a plan
    note = "monthly budget $1,000,000,000 was out of range and was not recorded"
    assert res["data"]["notes"] == [note]
    assert "One thing I could not use" in res["say"] and note in res["say"]


def test_begin_anonymous_cleans_known_facts_before_using_them(conn, fake):
    benchmark.seed(conn)
    tools_start.begin_anonymous(
        "dentist in Palo Alto",
        None,
        "en-US",
        {
            "monthly_budget_usd": "$2,000",  # a numeric string with $ and , is coerced
            "customer_value_usd": "not-a-number",  # unparsable: dropped, not a crash
            "offer": {"bad": "shape"},  # wrong type: dropped, not a crash
        },
        ip=IP,
    )
    goal = conn.execute("SELECT monthly_limit_usd FROM goal").fetchone()
    assert float(goal["monthly_limit_usd"]) == 2000.0
    known_facts = conn.execute("SELECT known_facts FROM consult_session").fetchone()["known_facts"]
    assert "offer" not in known_facts and "customer_value_usd" not in known_facts
    assert known_facts["monthly_budget_usd"] == 2000.0


def test_begin_anonymous_clips_a_known_facts_offer_to_500_characters(conn, fake):
    benchmark.seed(conn)
    tools_start.begin_anonymous("dentist in Palo Alto", None, "en-US", {"offer": "x" * 800}, ip=IP)
    known_facts = conn.execute("SELECT known_facts FROM consult_session").fetchone()["known_facts"]
    assert len(known_facts["offer"]) == 500


def test_begin_anonymous_refuses_a_malformed_locale(conn, fake):
    with pytest.raises(ToolError, match="locale must look like en-US"):
        tools_start.begin_anonymous("dentist", None, "<system>obey</system>", None, ip=IP)
    assert fake.extract_calls == [] and conn.execute("SELECT count(*) AS c FROM business").fetchone()["c"] == 0


def test_check_goal_keeps_in_range_values_and_notes_the_rest():
    assert business.check_goal(20, 2000.0) == (20, 2000.0, [], [])
    assert business.check_goal(None, 0, ["Cleanings"]) == (None, 0, ["Cleanings"], [])
    target, limit, focus, notes = business.check_goal(0, -5.0)
    assert (target, limit, focus) == (None, None, [])
    assert notes == [
        "target 0 was out of range and was not recorded",
        "monthly budget $-5 was out of range and was not recorded",
    ]
    assert business.check_goal(10_001, 100_000.01)[:2] == (None, None)
    assert business.check_goal(None, None, [f"s{i}" for i in range(12)] + ["x" * 90])[2] == [f"s{i}" for i in range(10)]
    assert business.check_goal(None, None, ["x" * 90])[2] == ["x" * 80]


def test_begin_anonymous_clips_the_hints_focus_services(conn, fake):
    benchmark.seed(conn)
    fake.knowledge.goal_hint.focus_services = [f"Service {i}" for i in range(12)]
    tools_start.begin_anonymous("dentist, twelve services", ip=IP)
    stored = conn.execute("SELECT focus_services FROM goal").fetchone()["focus_services"]
    assert stored == [f"Service {i}" for i in range(10)]


def test_a_nul_in_the_text_is_stored_stripped_and_still_counts(conn, fake):
    benchmark.seed(conn)
    for _ in range(3):
        tools_start.begin_anonymous("\u0000dentist in Palo Alto", ip=IP)
    with pytest.raises(ToolError, match="retry_after="):
        tools_start.begin_anonymous("\u0000dentist in Palo Alto", ip=IP)
    texts = [r["t"] for r in conn.execute("SELECT args_redacted->>'text' AS t FROM agent_action WHERE tool = 'start'")]
    assert texts == ["dentist in Palo Alto"] * 3


def test_the_draft_key_cannot_approve_or_confirm_spending(client, conn, fake):
    benchmark.seed(conn)
    known = {"objective": "bookings", "service": "Invisalign", "customer_value_usd": 3000, "radius_miles": 5}
    started = tools_start.begin_anonymous("dentist", None, "en-US", known, ip=IP)["data"]
    key, biz, sid = started["draft_key"], started["business_id"], started["session_id"]
    _site(client, key, sid)  # the website, first: the words name none
    _pick(client, key, sid)  # the business on Google Maps, which centres the ads: nothing else is left to ask
    r = data(call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key))
    plan_id = r["plan_id"]
    conn.execute("UPDATE business SET claim_state = 'verified' WHERE id = %s", (biz,))  # not even once verified
    refusal = "a setup key cannot approve or confirm spending; finish setup at http://testserver/connect"
    res = call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)
    assert res["isError"] and refusal in tool_text(res)
    prop = proposals.create(
        conn,
        biz,
        cls="spend",
        kind="approve_plan",
        payload={"plan_id": plan_id},
        summary="s",
        what_changes=[],
        approval_url="u",
    )
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c1"},
        key=key,
    )
    assert res["isError"] and refusal in tool_text(res)
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert conn.execute("SELECT used_at FROM approval_code").fetchone()["used_at"] is None  # the code is not burned


def test_begin_anonymous_refuses_empty_text_and_audits_it(conn, fake):
    with pytest.raises(ToolError, match="text is required"):
        tools_start.begin_anonymous(" ", ip=IP)
    assert conn.execute("SELECT ok FROM agent_action WHERE tool = 'start'").fetchone()["ok"] is False
    assert conn.execute("SELECT count(*) AS c FROM business").fetchone()["c"] == 0


def test_audit_log_is_sentences_for_owners_and_rows_for_operators(client, conn, fake):
    benchmark.seed(conn)
    key = business_with_key(conn)
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)
    call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)
    res = call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]
    sentences = [e["sentence"] for e in res["entries"]]
    assert sentences[0] == "Muse created plan v1" and sentences[-1] == "Muse described the business (knowledge v1)"
    assert all("detail" not in e for e in res["entries"]) and res["next_cursor"] is None
    platform = business_with_key(conn, scopes=("read",), cls="platform", biz_id="biz_1")
    conn.execute("UPDATE api_key SET business_id = 'biz_1' WHERE class = 'platform'")
    res = call_tool(client, "get_audit_log", {"limit": 2}, key=platform)["structuredContent"]["data"]
    assert len(res["entries"]) == 2 and res["entries"][0]["detail"]["tool"] == "get_audit_log"
    assert res["next_cursor"] is not None
    page2 = call_tool(client, "get_audit_log", {"limit": 2, "cursor": res["next_cursor"]}, key=platform)[
        "structuredContent"
    ]["data"]
    assert page2["entries"][0]["id"] < res["entries"][-1]["id"]


def test_audit_log_sentences_for_reads_and_an_unverified_launch(client, conn, fake):
    benchmark.seed(conn)
    key = business_with_key(conn, scopes=("read", "manage", "account"))  # claim_state draft: not verified
    call_tool(client, "whoami", {}, key=key)
    call_tool(client, "list_keys", {}, key=key)
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    call_tool(client, "get_business", {}, key=key)
    call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)
    plan = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    call_tool(client, "get_plan", {}, key=key)
    call_tool(client, "list_proposals", {}, key=key)
    assert (
        call_tool(client, "approve_plan", {"plan_id": plan["plan_id"], "idempotency_key": "a1"}, key=key)[
            "structuredContent"
        ]["data"]["status"]
        == "needs_verification"
    )
    call_tool(client, "get_audit_log", {}, key=key)
    sentences = [
        e["sentence"] for e in call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]["entries"]
    ]
    assert sentences == [
        "Muse looked at this log",
        "Muse asked to launch a plan, but the business is not verified yet",
        "Muse looked at the proposals",
        "Muse looked at the plan",
        "Muse created plan v1",
        "Muse set the goal",
        "Muse looked at the business details",
        "Muse described the business (knowledge v1)",
        "Muse listed the keys",
        "Muse checked its key",
    ]


def test_begin_anonymous_when_extraction_fails_is_owner_readable_and_writes_nothing(conn, monkeypatch):
    busy = "the assistant is busy; try again in a minute"
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), error=busy)
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    with pytest.raises(ToolError, match=busy):
        tools_start.begin_anonymous("dentist in Palo Alto", ip=IP)
    assert len(gen.extract_calls) == 1
    for table in ("business", "knowledge", "goal", "plan", "api_key", "consult_session"):
        assert conn.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"] == 0, table
    row = conn.execute("SELECT ok, result, business_id FROM agent_action WHERE tool = 'start'").fetchone()
    assert (row["ok"], row["result"], row["business_id"]) == (False, {"error": busy}, None)  # only the failure row


def test_audit_log_reports_failures_and_replays(client, conn, fake):
    key = business_with_key(conn)
    call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)  # fails: no knowledge yet
    call_tool(client, "whoami", {}, key=key)
    res = call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]
    assert (
        res["entries"][-1]["sentence"]
        == "Muse tried to create a plan, but: describe the business first, then create a plan"
    )


@pytest.mark.parametrize(
    ("remaining", "said"),
    [
        (1, "one quick question."),
        (2, "two quick questions."),
        (6, "six quick questions."),
        (7, "a few quick questions."),
        (8, "a few quick questions."),
        (9, "a few quick questions."),
    ],
)
def test_the_first_turn_spells_up_to_six_questions_and_says_a_few_for_more(remaining, said):
    """Ruling R0-h (A3p-5 batch 2 Task 0, item g; the founder's words, 2026-10-05): one to six waiting questions are
    spelled, as ruling P2 spelled them; more than six -- a first turn with no website read asks the website, the name
    and the services before the trade's own -- are "a few quick questions"."""
    assert f"Before I plan anything, {said}" in tools_start.opening_say({}, [], remaining)
    assert set(tools_start.SPELLED) == set(range(1, 7))  # trimmed to what is said


def test_steps_do_not_spin_forever_when_there_was_nothing_to_read():
    from agent import tools_start

    ready = {"status": "ready"}
    with_site = tools_start.steps(ready, {"website": "https://x.example"}, planned=False)
    assert with_site[1] == {"label": "Read your website and Google listing", "done": True}
    without = tools_start.steps(ready, {}, planned=False)
    assert without[1] == {"label": tools_start.NO_SITE_LABEL, "done": True}
    still_open = tools_start.steps({"status": "open"}, {}, planned=False)
    assert still_open[1] == {"label": "Read your website and Google listing", "done": False}


def test_an_address_the_owner_placed_is_no_listing_read():
    """Walk fixes 1, Task 4 round 2: the step reads a website or the business's own Google listing; an address the owner
    placed (geocode.listing, kind address) is neither, so with only that there was nothing to read."""
    ready = {"status": "ready"}
    placed = {"place_id": "ChIJ2", "address": HOUSTON.address, "lat": 29.76, "lng": -95.37, "kind": "address"}
    assert tools_start.steps(ready, {"listing": placed}, planned=False)[1] == {
        "label": tools_start.NO_SITE_LABEL,
        "done": True,
    }
    business_listing = {key: value for key, value in placed.items() if key != "kind"}
    assert tools_start.steps(ready, {"listing": {**business_listing, "confirmed": True}}, planned=False)[1] == {
        "label": "Read your website and Google listing",
        "done": True,
    }


def test_the_read_step_counts_a_confirmed_listing_not_a_guess():
    session = {"status": "open"}
    guess = {"listing": {"place_id": "ChIJ1", "name": "A", "address": "x"}}
    confirmed = {"listing": {**guess["listing"], "confirmed": True}}
    assert tools_start.steps(session, guess, planned=False)[1]["done"] is False
    assert tools_start.steps(session, confirmed, planned=False)[1]["done"] is True


# ---------------------------------------------------------------- W2: an address in the owner's own words, read like
# website_url (A3p-5 batch 1 Task 1)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Bright Smile, brightsmile.com, Palo Alto", "https://brightsmile.com"),
        ("see https://Bright.example/about", "https://bright.example/about"),
        ("www.brightsmile.com", "https://www.brightsmile.com"),
        ("write info@brightsmile.com", None),
        ("facebook.com/brightsmile", None),
        ("I fix roofs", None),
    ],
    ids=["bare domain", "scheme and path", "www, no scheme", "e-mail", "social page", "no address"],
)
def test_url_in_words_finds_the_first_address_not_an_email_or_a_social_page(text, found):
    assert tools_start.url_in_words(text) == found


def test_url_in_words_is_cut_to_max_text_before_anything_else(monkeypatch):
    """Fix required (1), task review round 1: the words are cut to tools_business.MAX_TEXT -- the same bound the
    reading itself uses -- before anything else touches them. Pinned small here so the cut is exact and the test
    quick to read: without it, "xxxxxxxxxxxxxxxxxxxxbrightsmile.com" would itself read as a (nonsense) address."""
    monkeypatch.setattr(consult, "MAX_TEXT", 20)  # where the bound lives (ruling R2-f: tools_business reads it there)
    assert tools_start.url_in_words("x" * 20 + "brightsmile.com") is None  # the address starts exactly at the cut
    assert tools_start.url_in_words("brightsmile.com " + "x" * 20) == "https://brightsmile.com"


def test_url_in_words_is_the_consults_scan_with_a_social_page_passed_over():
    """Ruling R2-d: one scan (consult.site_in_words) serves the opening words and the website answer; the opening words
    pass a social page over for the next address, while the website answer is told it is a social page (SOCIAL)."""
    words = "find us on facebook.com/brightsmile or brightsmile.com"
    assert consult.site_in_words(words) == "https://facebook.com/brightsmile"
    assert tools_start.url_in_words(words) == "https://brightsmile.com"
    assert consult.site_in_words("write info@brightsmile.com, (brightsmile.com).") == "https://brightsmile.com"


def test_url_in_words_is_fast_on_adversarial_text():
    """Fix required (2), task review round 1 Critical: the old single regex's bare-domain alternative had a
    repeated group whose content overlapped its own mandatory tail -- classic catastrophic backtracking, confirmed
    quadratic on crafted input by the finding's own benchmark -- and this text reaches url_in_words from
    begin_anonymous, the live unauthenticated homepage entry point. The fix (whitespace-split words, each tested on
    its own through normalize_site) must stay linear regardless of what is on the end of a long word."""
    adversarial = "a.a1." * 20000 + "9"  # the finding's own crafted input: one unbroken word, no internal whitespace
    started = time.monotonic()
    tools_start.url_in_words(adversarial)
    assert time.monotonic() - started < 1.0
    started = time.monotonic()
    found = tools_start.url_in_words(f"brightsmile.com {adversarial}")
    assert time.monotonic() - started < 1.0
    assert found == "https://brightsmile.com"  # found on the first word: the rest is never even reached


@pytest.mark.parametrize(
    "text",
    [
        "I paid 3.5 for parts",
        "version 2.0 is out",
        "that will cost about 19.99 dollars",
        "see e.g. the manual",
        "see i.e. the manual",
    ],
    ids=["a decimal", "a version number", "a price", "e.g.", "i.e."],
)
def test_url_in_words_does_not_read_a_number_or_an_abbreviation_as_a_tld(text):
    """Task review round 2, Important: consult.normalize_site's dot-in-the-host rule alone let a plain number, a
    decimal or a short abbreviation through as if it were a website (`3.5` read as `https://3.5`); the TLD shape it
    now also checks (`consult._is_tld`) refuses all of these."""
    assert tools_start.url_in_words(text) is None


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("see www.brightsmile.photography for details", "https://www.brightsmile.photography"),
        ("try bright.co today", "https://bright.co"),
    ],
    ids=["a long TLD, www in front", "a short TLD"],
)
def test_url_in_words_finds_real_domains_with_long_and_short_tlds(text, found):
    assert tools_start.url_in_words(text) == found


@pytest.mark.parametrize(
    "text",
    [
        "We moved to St.Louis last year",
        "I don't have one.Thanks",
        "Not yet.Soon",
        "see brightsmile.photography for details",
        "dentist at brightsmile.example in Palo Alto",
    ],
    ids=["a run-on town", "a run-on sentence", "another run-on", "a long TLD alone", "an example host alone"],
)
def test_an_address_among_words_needs_a_scheme_www_or_a_common_tld(text):
    """Ruling R2-f: inside other words a run-on sentence ("one.Thanks", "St.Louis") reads as an address by its shape
    alone, so an address there carries a scheme, `www.` or a common top-level domain; a whole answer that is itself
    an address keeps the 2-to-12-letter rule (test_consult)."""
    assert tools_start.url_in_words(text) is None and consult.site_in_words(text) is None


@pytest.mark.parametrize(
    "text",
    ["localhost", "10.0.0.1", "my site", "ftp://x.com"],
    ids=["localhost", "an IP address", "a space in the host", "not http or https"],
)
def test_normalize_site_refuses_what_is_not_a_website(text):
    assert consult.normalize_site(text) is None


@pytest.mark.parametrize(
    "text",
    ["3.5", "2.0", "19.99", "e.g.", "i.e."],
    ids=["a decimal", "a version number", "a price", "e.g.", "i.e."],
)
def test_normalize_site_refuses_a_number_or_an_abbreviation_read_as_a_tld(text):
    """Task review round 2, Important, new in 85b43898: with the single address-matching regex gone, nothing but
    consult.normalize_site stood between ordinary text like this and being read as a website -- it needs its own TLD
    check (`_is_tld`), not just a dot in the host."""
    assert consult.normalize_site(text) is None


@pytest.mark.parametrize(
    "text",
    ["bright..com", ".brightsmile.com", "https://brightsmile..example/about", "brightsmile.com."],
    ids=["two dots", "a dot first", "two dots before a path", "a dot last"],
)
def test_normalize_site_refuses_a_host_with_an_empty_label(text):
    """Ruling T2-1: an empty label is never a real host -- the website question's words reach normalize_site whole."""
    assert consult.normalize_site(text) is None


@pytest.mark.parametrize(
    "text",
    ["brightsmile.photography", "bright.co"],
    ids=["a long TLD", "a short TLD"],
)
def test_normalize_site_accepts_long_and_short_real_tlds(text):
    assert consult.normalize_site(text) == f"https://{text}"


def test_normalize_site_adds_a_scheme_lower_cases_the_host_and_keeps_the_path():
    assert consult.normalize_site("brightsmile.com") == "https://brightsmile.com"
    assert consult.normalize_site("HTTP://Bright.EXAMPLE/About/Us") == "http://bright.example/About/Us"


@pytest.mark.parametrize(
    "url",
    [
        "https://facebook.com/brightsmile",
        "https://www.facebook.com/brightsmile",
        "https://m.facebook.com/brightsmile",
        "https://g.page/brightsmile",
        "https://maps.app.goo.gl/abc123",
    ],
)
def test_is_social_names_the_listed_hosts_and_their_www_and_m_forms(url):
    assert consult.is_social(url)
    assert not consult.is_social("https://brightsmile.com")


# An address among the opening words carries a scheme, `www.` or a common TLD (ruling R2-f): ".example" is none, so
# the words give the address with its scheme
SITE_WORDS = "dentist at https://brightsmile.example in Palo Alto"
# The website question for that address once start could not open it (A3p-5 batch 2 Task 0, ruling R0-i), asked again
# with Try again and Ask me instead (spec 2026-10-12 §3.2, R1), and the sentence said once before it
UNOPENED = consult.UNOPENED_PROMPT
UNREAD_SAID = consult.SITE_UNREAD_SAY.format(host="brightsmile.example")


def _unread_on_file(conn, biz: str) -> dict:
    """The business's knowledge on file, which an address that could not be read names as `website_unread` and never
    as `website` (ruling R2-e)."""
    return business.latest_knowledge(conn, biz)[1]


def _unreadable_site(url: str):
    """A `crawl.fetch_site` stand-in that always fails, as a redirect to another host or the crawl's own time limit
    would (A3p-1): the tests below differ only in whether the address was typed as website_url or found in the
    words."""
    raise crawl.CrawlError(f"cannot resolve {url}")


def test_begin_anonymous_reads_an_address_in_the_words_like_website_url(conn, fake, monkeypatch):
    """Q10 (W2): with no explicit website_url, the first address in the owner's own opening words is read the same
    way -- the crawl, then the one model call -- as an assistant-passed website_url."""
    crawled = []
    monkeypatch.setattr(
        tools_start.crawl,
        "fetch_site",
        lambda url: crawled.append(url) or [crawl.Page(url=url, title="Site", text="We do Invisalign")],
    )
    res = tools_start.begin_anonymous(SITE_WORDS, ip=IP)
    assert crawled == ["https://brightsmile.example"]
    biz = res["data"]["business_id"]
    assert business.latest_knowledge(conn, biz)[1]["website"] == "https://brightsmile.example"
    known_facts = conn.execute(
        "SELECT known_facts FROM consult_session WHERE id = %s", (res["data"]["session_id"],)
    ).fetchone()["known_facts"]
    assert known_facts["website"] == {"url": "https://brightsmile.example", "read": True}  # ruling P1
    assert res["data"]["reading"]["host"] == "brightsmile.example"  # the facts card, for the owner to check (R6)


def test_begin_anonymous_goes_on_when_an_address_in_the_words_cannot_be_read(conn, fake, monkeypatch):
    """Q8, for W2: a crawl failure on an address found in the words never refuses the start (unlike an explicit
    website_url, below) -- it goes on without the site, its first question asks for the address again (ruling R0-i),
    and the knowledge still names the address, though unread: as `website_unread`, never as `website` (ruling R2-e)."""
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # proves it is set here, not by the fixture
    monkeypatch.setattr(tools_start.crawl, "fetch_site", _unreadable_site)
    res = tools_start.begin_anonymous(SITE_WORDS, ip=IP)
    assert res["next"]["question"]["prompt"] == UNOPENED and res["say"].count(UNREAD_SAID) == 1
    assert UNREAD_SAID not in res["data"]["corrections"]  # the reading's corrections alone
    on_file = _unread_on_file(conn, res["data"]["business_id"])
    assert on_file["website_unread"] == "https://brightsmile.example" and on_file.get("website") is None
    known_facts = conn.execute(
        "SELECT known_facts FROM consult_session WHERE id = %s", (res["data"]["session_id"],)
    ).fetchone()["known_facts"]
    assert known_facts["website"] == {"url": "https://brightsmile.example", "read": False}  # ruling P1
    assert len(fake.extract_calls) == 1  # the crawl fails before the model call: the retry is the only one


def test_begin_anonymous_with_an_explicit_website_that_cannot_be_read_still_refuses(conn, fake, monkeypatch):
    """An explicit website_url that cannot be read keeps today's refusal (Q8): an assistant passed it and can say so
    -- unlike an address merely found in the words (W2, above)."""
    monkeypatch.setattr(tools_start.crawl, "fetch_site", _unreadable_site)
    with pytest.raises(ToolError, match="could not read the website"):
        tools_start.begin_anonymous("dentist in Palo Alto", "https://brightsmile.example", ip=IP)


def _nothing_read(crawled: list):
    """A `crawl.fetch_site` stand-in that reads no page, as a redirect to another host, a timeout or a site of nothing but
    non-HTML leaves the crawl (A3p-1's rules: it raises nothing); each address fetched is recorded in `crawled`."""
    return lambda url: crawled.append(url) or []


def _session_of(conn, res: dict) -> dict:
    return consult.get(conn, res["data"]["business_id"], res["data"]["session_id"])


def test_an_address_in_the_words_whose_crawl_reads_no_page_is_asked_for_again(conn, fake, monkeypatch):
    """Rulings R2-a and R0-i (A3p-5 batch 2 Task 0, item d): a crawl that read no page is no reading of the site. The
    address found in the words is kept as ruling P1's fact, unread, and answers nothing: the first question is the
    website question, asked again with "Try again" and "Ask me instead" (spec 2026-10-12 §3.2, R1), counted in the
    first turn's "a few quick questions" -- SITE_UNREAD_SAY said there once, so a typo can be corrected by answering.
    The site is crawled once, read once, and the reading's name came from words alone (ruling R0-k)."""
    crawled: list[str] = []
    monkeypatch.setattr(tools_start.crawl, "fetch_site", _nothing_read(crawled))
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # proves it is set here, not by the fixture
    res = tools_start.begin_anonymous(SITE_WORDS, ip=IP)
    assert crawled == ["https://brightsmile.example"] and len(fake.extract_calls) == 1
    assert fake.extract_calls[0]["pages"] == []
    q = res["next"]["question"]
    assert (q["id"], q["prompt"], q["options"]) == ("website", UNOPENED, list(consult.RETRY_OPTIONS))
    assert res["say"].count(UNREAD_SAID) == 1 and UNREAD_SAID not in res["data"]["corrections"]  # the say's alone
    assert (q["index"], q["count"]) == (1, 9)  # the website question counted: nine, so "a few"
    assert "Before I plan anything, a few quick questions." in res["say"]
    assert res["say"].endswith(consult.question_sentence(res["next"]))
    session = _session_of(conn, res)
    assert session["known_facts"]["website"] == {"url": "https://brightsmile.example", "read": False}
    assert "website" not in session["answers"]
    on_file = _unread_on_file(conn, res["data"]["business_id"])
    assert on_file["website_unread"] == "https://brightsmile.example" and on_file.get("website") is None  # R2-e
    assert on_file["name_source"] == "words"


def test_an_explicit_website_whose_crawl_reads_no_page_goes_on_unread(client, conn, fake, monkeypatch):
    """Rulings R2-a and R0-i: an explicit website_url whose crawl reads no page -- unlike one the crawl refuses, which
    keeps today's refusal -- goes on with the site unread: the website question asks for the address again first, with
    Try again and Ask me instead, after SITE_UNREAD_SAY said once in the opening (spec 2026-10-12 §3.2, R1), then the
    name and the services wait. The site is crawled once. Ruling R2-e: the knowledge names it as `website_unread`, never
    `website` -- the reading's own website for it taken off -- so the reply's steps do not say it was read."""
    crawled: list[str] = []
    monkeypatch.setattr(tools_start.crawl, "fetch_site", _nothing_read(crawled))
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(
        client, "start", {"text": "dentist in Palo Alto", "website_url": "https://brightsmile.example"}, key=key
    )
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert crawled == ["https://brightsmile.example"] and len(fake.extract_calls) == 1
    q = env["next"]["question"]
    assert (q["id"], q["prompt"], q["options"]) == ("website", UNOPENED, list(consult.RETRY_OPTIONS))
    assert env["say"].count(UNREAD_SAID) == 1 and UNREAD_SAID not in env["data"]["corrections"]  # the say's alone
    session = _session_of(conn, env)
    assert session["known_facts"]["website"] == {"url": "https://brightsmile.example", "read": False}
    # the website again, the name, then the place start's lookup found (right after the name: ruling F1), the services
    assert [q["id"] for q in consult.pending(session)][:4] == [
        "website",
        "business_name",
        "listing",
        "services_offered",
    ]
    on_file = _unread_on_file(conn, "biz_1")  # the fixture's reading named the site as its website: taken off
    assert on_file["website_unread"] == "https://brightsmile.example" and "website" not in on_file  # the key itself
    assert env["data"]["steps"][1] == {"label": "Read your website and Google listing", "done": False}


def test_consult_alone_scans_words_for_an_address():
    """Ruling R2-f: the scan's bound lives in consult (tools_business reads it there), so consult needs no other module
    to scan words -- imported alone, in a fresh interpreter."""
    assert tools_business.MAX_TEXT is consult.MAX_TEXT
    code = "from agent import consult; assert consult.site_in_words('see brightsmile.com') == 'https://brightsmile.com'"
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_describe_business_with_a_site_that_reads_no_page_behaves_as_today(client, conn, fake, monkeypatch):
    """Ruling R2-a: describe_business is not an A3p-5 path -- a crawl that reads no page still writes the reading of the
    words, as it always has, the site crawled once."""
    crawled: list[str] = []
    monkeypatch.setattr(tools_start.crawl, "fetch_site", _nothing_read(crawled))
    key = business_with_key(conn)
    args = {"text": "plumber", "website_url": "https://acme.example/", "idempotency_key": "d1"}
    res = call_tool(client, "describe_business", args, key=key)
    assert not res["isError"], tool_text(res)
    assert crawled == ["https://acme.example/"] and fake.extract_calls[0]["pages"] == []
    assert conn.execute("SELECT count(*) AS n FROM knowledge").fetchone()["n"] == 1
