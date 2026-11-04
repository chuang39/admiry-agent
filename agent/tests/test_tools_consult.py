import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from markupsafe import escape

from agent import (
    auth,
    benchmark,
    business,
    consult,
    crawl,
    envelope,
    geocode,
    listings,
    llm,
    models,
    places,
    plans,
    profile,
    site_install,
    tools_consult,
    tools_start,
)
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.fixtures import (
    READ_SITE,
    FakeGenerator,
    business_with_key,
    home_services_knowledge,
    knowledge_fixture,
    plan_draft_fixture,
)
from agent.tests.surface_helpers import settle_page

READS = {
    "around three thousand": models.AnswerRead(
        option_id="1000_plus", amount_usd=3000, miles=None, note=None, understood=True, services=[]
    ),
}


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads=dict(READS))  # a test's own
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    # start's lookup finds the business with its point on the map, close by its website and phone: the first question
    # offers it, and picking it centres the ads (plan 2026-10-03-agent-listing-confirm; `started` walks a business it
    # finds nothing for)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE])
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    return gen


def _start(
    client,
    conn,
    text="I want to find everyone interested in a dentist within 5 miles of my clinic.",
    *,
    biz_id="biz_1",
    **kw,
):
    """A keyed `start` on a freshly claimed `biz_id` (biz_1 unless a test names another; A1b-4b Task 4: `start` no
    longer works with no key); the key is stashed on the returned body as `["key"]` since `data.draft_key` is now
    always None."""
    key = business_with_key(conn, biz_id=biz_id, claim_state="claimed")
    res = call_tool(client, "start", {"text": text, "website_url": "https://brightsmile.example", **kw}, key=key)
    assert not res["isError"], tool_text(res)
    body = res["structuredContent"]
    body["key"] = key
    return body


def _answer(client, key, sid, answers, ik):
    res = call_tool(client, "answer", {"session_id": sid, "answers": answers, "idempotency_key": ik}, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def res_say(res: dict) -> str:
    """The `say` of a tool result's envelope."""
    return res["structuredContent"]["say"]


def res_next(res: dict) -> dict:
    """The `next` of a tool result's envelope."""
    return res["structuredContent"]["next"]


def launch_tools(env: dict) -> list[str]:
    """The tools of a plan answer's launch actions: after the four ways to put Admiry's code on the owner's site, which
    come first while the site has none -- the walks' business has a website (A3p-5 batch 2 Q6, ruling R2-c); support
    off, as in these tests, without Admiry's team (ruling R3-f)."""
    actions, ways = env["next"]["actions"], site_install.ways_for(None)
    assert actions[: len(ways)] == ways, actions
    return [a["tool"] for a in actions[len(ways) :]]


def launch_ids(env: dict) -> list[str]:
    """`launch_tools`, by the actions' ids: Admiry running the ads and the own account share connect_channel."""
    ways = site_install.ways_for(None)
    return [a["id"] for a in env["next"]["actions"][len(ways) :]]


# The Bright Smile listing start's lookup finds, with the point on the map Google Maps gives a place, and the website and
# phone that make it close to the business (listings.close)
BRIGHT_SMILE = places.Candidate(
    "ChIJ1",
    "Bright Smile Dental",
    "500 University Ave, Palo Alto, CA",
    37.4419,
    -122.143,
    website="https://brightsmile.example",
    phone="(650) 555-0100",
)
# The owner's answer to the first question: the business is the place offered first (plan
# 2026-10-03-agent-listing-confirm)
PICKED = {"listing": {"choice": "gm_1"}}
# What `started` answers before the question of where the ads centre: choices, so the model reads nothing (the business
# on Google Maps first, when start's lookup offered it)
BEFORE_LOCATION = {
    **PICKED,
    "objective": {"choice": "bookings"},
    "service": {"choice": "svc_invisalign"},
    "customer_value": {"choice": "1000_plus"},
}


def started(client, conn, fake, listing=False, before=BEFORE_LOCATION, **kw):
    """A dental consultation walked to the question of where the ads centre (walk fixes 1, Task 4): `start` on a freshly
    claimed biz_1 with words that name no radius, then the questions before it answered as `before` answers them
    (choices: BEFORE_LOCATION). start's lookup finds the Bright Smile listing with its point when `listing` is True,
    the candidate `listing` is when it is one (round 1), else nothing; a place found is offered first and picked (plan
    2026-10-03-agent-listing-confirm). Every lookup after it is the test's own Places fake (places_fake, places_none).
    `fake`, the model fake, must be in place. Returns start's envelope with `session_id` and `key`: its `say` is the
    opening turn's."""
    benchmark.seed(conn)
    found = BRIGHT_SMILE if listing is True else listing or None
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(places, "search", lambda query, key, **kw: [found] if found else [])
        body = _start(client, conn, "We are a family dentist and want more Invisalign patients.", **kw)
    sid = body["data"]["session_id"]
    turn = body
    while turn["next"]["kind"] == "question" and turn["next"]["question"]["id"] in before:
        qid = turn["next"]["question"]["id"]
        turn = _answer(client, body["key"], sid, {qid: before[qid]}, f"{sid}:{qid}")
    return {**body, "session_id": sid}


def knowledge_services(conn, biz: str) -> list[dict]:
    """The services on the business's latest knowledge as the profile reads them, `{"id", "name"}`: a service's id is
    the one stored on it, else the one its name gives (`profile.services`; a reading's services carry none)."""
    return profile.services(business.latest_knowledge(conn, biz)[1])


# Where the ads centre, in a session a test builds as answered in full: left for later (walk fixes 1, Task 4)
LATER = {"skipped": True, "raw": "later", "source": "other"}

# What the dental walk (test_the_dental_walk_through_ends_in_a_plan_with_cards) answers to each question it can be
# asked; the objective is the caller's. The free text is the one the `fake` reads (READS above) as $3,000. The business
# on Google Maps is picked first: it centres the ads, so where they centre is not asked.
WALK_ANSWERS = {
    **PICKED,
    "service": {"choice": "svc_invisalign"},
    "customer_value": {"other": "around three thousand"},
    "radius": {"choice": "r5"},
}


def answered_consultation(
    client, conn, fake, *, objective="calls", monthly_budget_usd=None, known_facts=None, business="biz_1"
):
    """The dental walk taken to `ready`, the plan not built: the benchmark seeded, `start` on a freshly claimed
    `business`, then every question still open answered as the walk does, the objective with `objective`.
    `monthly_budget_usd` and `known_facts` (which wins) are `start`'s known_facts: a fact told there pre-answers its
    question, so it is not asked and not answered here. `fake`, the model fake, must be in place (the test's own
    fixture): the walk's free text and the plan's draft come from it. Returns {"session_id", "key", "business_id"}."""
    benchmark.seed(conn)
    told = {**({} if monthly_budget_usd is None else {"monthly_budget_usd": monthly_budget_usd}), **(known_facts or {})}
    first = _start(client, conn, biz_id=business, **({"known_facts": told} if told else {}))
    key, sid = first["key"], first["data"]["session_id"]
    answers = {**WALK_ANSWERS, "objective": {"choice": objective}}
    turn = first
    for _ in answers:  # at most one turn per question the walk can answer
        if turn["next"]["kind"] != "question":
            break
        qid = turn["next"]["question"]["id"]
        turn = _answer(client, key, sid, {qid: answers[qid]}, f"{sid}:{qid}")
    assert turn["next"]["kind"] != "question", f"still asking {turn['next']['question']['id']} after the walk"
    return {"session_id": sid, "key": key, "business_id": first["data"]["business_id"]}


def recommended_with(
    client, conn, fake, *, objective="calls", monthly_budget_usd=None, known_facts=None, business="biz_1"
):
    """`answered_consultation`'s walk, then `recommend`: returns the plan's envelope (`data`, `say`, `next`)."""
    done = answered_consultation(
        client,
        conn,
        fake,
        objective=objective,
        monthly_budget_usd=monthly_budget_usd,
        known_facts=known_facts,
        business=business,
    )
    sid = done["session_id"]
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": f"{sid}:recommend"}, key=done["key"])
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def test_start_corrects_everyone_and_asks_the_first_question(client, conn, fake):
    benchmark.seed(conn)
    body = _start(client, conn)
    d = body["data"]
    assert d["session_id"].startswith("cs_") and d["draft_key"] is None
    assert d["understanding"]["name"] == "Bright Smile Dental" and d["understanding"]["services"] == [
        "Invisalign",
        "Cleanings",
    ]
    assert d["corrections"] and "everyone" in d["corrections"][0]
    assert body["say"].startswith("I can build that for you.") and "everyone" in body["say"]
    # radius came from the text: 5 remain, the business on Google Maps first (plan 2026-10-03-agent-listing-confirm)
    assert "five quick questions." in body["say"]
    assert body["say"].endswith(
        "Is your business one of these on Google Maps? Options: Bright Smile Dental, 500 University Ave, Palo Alto, "
        "CA; None of these — or answer in your own words."
    )
    q = body["next"]["question"]
    # radius was preanswered silently from the text: it is not one of the questions the owner is asked, so
    # count is 5 (6 tree questions minus the one known-fact pre-answer) and "listing" is #1 of those 5.
    assert (q["id"], q["index"], q["count"]) == ("listing", 1, 5) and q["other"]["allowed"] is True
    assert not JARGON.search(body["say"])
    picked = _answer(client, body["key"], d["session_id"], PICKED, "pick")
    assert picked["say"].endswith(
        "What do you want most from your ads? Options: New patient bookings, Phone calls, Website enquiries, "
        "Awareness, Any of these — or answer in your own words."
    )
    # the place picked centres the ads, a pre-answer too: "objective" is #2 of 4
    q = picked["next"]["question"]
    assert (q["id"], q["index"], q["count"]) == ("objective", 2, 4) and q["other"]["allowed"] is True
    assert not JARGON.search(picked["say"])
    assert conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"] == 0  # no plan before the questions
    s = consult.get(conn, d["business_id"], d["session_id"])
    assert s["answers"]["radius"]["miles"] == 5.0 and s["answers"]["radius"]["source"] == "known_fact"
    who = data(call_tool(client, "whoami", {}, key=body["key"]))
    assert who["open_session_id"] == d["session_id"]


def test_known_facts_skip_questions_and_a_full_set_is_ready_at_once(client, conn, fake):
    benchmark.seed(conn)
    body = _start(
        client,
        conn,
        "dentist in Palo Alto",
        known_facts={"objective": "bookings", "service": "Invisalign", "customer_value_usd": 3000, "radius_miles": 5},
    )
    # no fact answers which place is the business (plan 2026-10-03-agent-listing-confirm): only that is asked, and the
    # place picked centres the ads
    assert body["next"]["question"]["id"] == "listing"
    picked = _answer(client, body["key"], body["data"]["session_id"], PICKED, "pick")
    assert picked["next"]["kind"] == "actions" and picked["next"]["actions"][0]["tool"] == "recommend"
    assert picked["data"]["steps"][0] == {"label": "Understood the goal", "done": True}


def test_picking_a_place_links_it_and_centres_the_ads_on_it(client, conn, walk_fake):
    body = _start(client, conn)  # this file's helper: a started dental walk and its key
    s, key = body["data"]["session_id"], body["key"]
    res = call_tool(
        client,
        "answer",
        {"session_id": s, "answers": {"listing": {"choice": "gm_1"}}, "idempotency_key": "l1"},
        key=key,
    )
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"]["place_id"] == "ChIJ1" and k["listing"]["confirmed"] is True
    assert res_say(res).startswith(
        "Linked to Bright Smile Dental on Google Maps. The ads centre on 500 University Ave, Palo Alto, CA."
    )
    session = consult.get(conn, "biz_1", s)
    assert session["answers"]["location"]["source"] == "known_fact"  # where the ads centre: the place picked
    assert listings.offer(conn, "biz_1") == []


def test_none_of_these_links_nothing_and_asks_where_the_ads_centre(client, conn, walk_fake):
    body = _start(client, conn)
    s, key = body["data"]["session_id"], body["key"]
    res = call_tool(
        client,
        "answer",
        {"session_id": s, "answers": {"listing": {"choice": "none"}}, "idempotency_key": "l2"},
        key=key,
    )
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert "listing" not in k and k["listing_rejected"] == ["ChIJ1"]
    assert res_say(res).startswith(listings.NONE_NOTED)
    assert "location" in [q["id"] for q in consult.pending(consult.get(conn, "biz_1", s))]


def test_a_pick_after_the_offer_closed_links_nothing(client, conn, walk_fake):
    body = _start(client, conn)
    s, key = body["data"]["session_id"], body["key"]
    listings.drop_offer(conn, "biz_1")
    res = call_tool(
        client,
        "answer",
        {"session_id": s, "answers": {"listing": {"choice": "gm_1"}}, "idempotency_key": "l3"},
        key=key,
    )
    assert "listing" not in business.latest_knowledge(conn, "biz_1")[1]
    assert listings.OFFER_GONE in res_say(res)


def test_a_pick_links_only_the_place_the_owner_saw(client, conn, fake, monkeypatch):
    """Final review A1: the consultation's options were frozen at start; the owner's own search meanwhile (the profile
    tool's find, or Settings') replaced the offer and numbered its place gm_1 too. The pick of the gm_1 the owner saw
    links nothing -- not the place that took its number -- says so, and leaves the newer offer open."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    other = places.Candidate("ChIJ_OTHER", "Other Dental", "1 Elsewhere Rd, Fresno, CA", 36.7, -119.8)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [other])
    found = call_tool(
        client,
        "update_business",
        {"patch": {"google_listing": {"find": "Other Dental, Fresno"}}, "idempotency_key": "f1"},
        key=key,
    )
    assert data(found)["candidates"][0]["id"] == "gm_1"
    version = business.latest_knowledge(conn, "biz_1")[0]
    res = _listing(client, key, sid, {"choice": "gm_1"}, "pick")
    assert res["say"].startswith(listings.OFFER_GONE)
    assert business.latest_knowledge(conn, "biz_1")[0] == version  # nothing linked, no name taken
    assert "location" not in consult.get(conn, "biz_1", sid)["answers"]  # no centre in Fresno
    assert [c["place_id"] for c in listings.offer(conn, "biz_1")] == ["ChIJ_OTHER"]


def test_a_yes_links_the_one_place_offered(client, conn, fake):
    """Final review A2/B I1: one place offered, the owner types "Yes, that's us": that place is linked."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    res = _listing(client, key, sid, {"other": "Yes, that's us"}, "yes")
    assert res["say"].startswith("Linked to Bright Smile Dental on Google Maps.")
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"]["place_id"] == "ChIJ1"
    # fix round 2, Important 1: a listing "yes" is not an address in the words -- it makes no model call, and must
    # not burn one of the business's daily reads (it was once wrongly caught by _reads_a_site's own "yes" heuristic)
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'yes'")
    assert flag.fetchone()["f"] == "false"


def test_the_model_never_reads_an_offered_place(client, conn, walk_fake):
    _start(client, conn)
    assert all(call["listing"] is None for call in walk_fake.extract_calls)


# A second place close to the business (its phone), offered as gm_2 beside BRIGHT_SMILE
CARE = places.Candidate(
    "ChIJ5", "Bright Smile Dental Care", "200 Hamilton Ave, Palo Alto, CA", 37.44, -122.16, phone="(650) 555-0100"
)


def _listing(client, key: str, sid: str, value: dict, ik: str) -> dict:
    """`answer` with the listing question's answer alone; the envelope."""
    res = call_tool(
        client, "answer", {"session_id": sid, "answers": {"listing": value}, "idempotency_key": ik}, key=key
    )
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def test_the_listing_question_is_answered_once(client, conn, fake, monkeypatch):
    """Fix round 1 (the controller's ruling): which place is the business is answered once per consultation. A second
    answer -- "none", or another place -- records nothing and says where the listing is changed; the place picked stays
    linked and no knowledge version is written. Another question in the same call is answered as ever."""
    benchmark.seed(conn)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE, CARE])
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    assert [o["id"] for o in body["next"]["question"]["options"]] == ["gm_1", "gm_2", "none"]
    _listing(client, key, sid, {"choice": "gm_1"}, "l1")
    version = business.latest_knowledge(conn, "biz_1")[0]
    for i, value in enumerate(({"choice": "none"}, {"choice": "gm_2"}, {"other": "none of these"})):
        again = _listing(client, key, sid, value, f"again-{i}")
        assert consult.LISTING_SETTLED in again["say"] and again["data"]["recorded"] == {}
        assert business.latest_knowledge(conn, "biz_1")[0] == version  # nothing written
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"]["place_id"] == "ChIJ1" and k["listing"]["confirmed"] is True and not k["listing_rejected"]
    assert consult.get(conn, "biz_1", sid)["answers"]["listing"]["choice"] == "gm_1"
    both = {"listing": {"choice": "none"}, "objective": {"choice": "bookings"}}
    res = call_tool(client, "answer", {"session_id": sid, "answers": both, "idempotency_key": "both"}, key=key)
    assert data(res)["recorded"] == {"objective": {"choice": "bookings", "raw": "bookings", "source": "choice"}}
    assert consult.LISTING_SETTLED in res_say(res)


def test_none_of_these_after_the_offer_closed_promises_nothing(client, conn, fake):
    """Fix round 1: "None of these" whose places are no longer offered remembers none of them, so it says only that
    none is the business (NONE_GONE), never that Admiry will not offer them again."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    listings.drop_offer(conn, "biz_1")
    version = business.latest_knowledge(conn, "biz_1")[0]
    res = _listing(client, key, sid, {"choice": "none"}, "gone")
    assert res["say"].startswith(listings.NONE_GONE) and listings.NONE_NOTED not in res["say"]
    assert business.latest_knowledge(conn, "biz_1")[0] == version  # nothing remembered
    assert listings.NONE_GONE == "Noted: none of these is the business."


def test_the_listing_words_are_read_without_the_model(client, conn, fake, monkeypatch):
    """Fix round 1 (the global constraint): words answering the listing question build no generator, check no daily
    model limit and are not counted against it (the audit's free_text is false)."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    monkeypatch.setattr(llm, "generator", lambda settings=None: pytest.fail("no model for the listing question"))
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)  # checked, the limit would refuse
    res = _listing(client, key, sid, {"other": "It's the first one"}, "words")
    assert res["say"].startswith("Linked to Bright Smile Dental on Google Maps.")
    assert business.latest_knowledge(conn, "biz_1")[1]["listing"]["place_id"] == "ChIJ1"
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE tool = 'answer'").fetchone()
    assert flag["f"] == "false" and fake.read_calls == []


def test_unclear_listing_words_are_asked_again_then_link_nothing_and_remember_nothing(client, conn, fake):
    """Words that pick no place clearly are asked about again; a second time the question is answered with no place
    linked and nothing remembered as not the business -- the owner never said "None of these" (final review A2/B I2):
    the offer is closed, the owner reads where to link it later, and the question of where the ads centre follows."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    version = business.latest_knowledge(conn, "biz_1")[0]
    first = _listing(client, key, sid, {"other": "the blue building"}, "u1")
    assert consult.LISTING_BY_CHOICE in first["say"] and first["next"]["question"]["id"] == "listing"
    second = _listing(client, key, sid, {"other": "the blue building"}, "u2")
    assert second["say"].startswith(listings.UNCLEAR_NONE) and second["next"]["question"]["id"] == "objective"
    assert listings.UNCLEAR_NONE == (
        "I could not tell which place is the business, so none is linked. You can link it later in Settings, or tell "
        "your assistant."
    )
    assert business.latest_knowledge(conn, "biz_1")[0] == version  # nothing linked, nothing remembered
    assert listings.offer(conn, "biz_1") == []
    assert "location" in [q["id"] for q in consult.pending(consult.get(conn, "biz_1", sid))]
    assert fake.read_calls == []


def test_the_listing_question_is_one_turn_of_the_transcript(client, conn, fake, monkeypatch):
    """Final review B M1's knock-on: the transcript finds the first question in start's say as question_sentence writes
    it, places set apart with semicolons, so the listing question is not given a second turn."""
    from agent import transcript

    benchmark.seed(conn)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE, CARE])
    body = _start(client, conn)
    _listing(client, body["key"], body["data"]["session_id"], {"choice": "gm_2"}, "l1")
    texts = [t["text"] for t in transcript.build(conn, "biz_1")["turns"]]
    assert sum("Is your business one of these on Google Maps?" in t for t in texts) == 1
    assert "Bright Smile Dental Care, 200 Hamilton Ave, Palo Alto, CA" in texts  # the owner's pick, as its label


def test_the_listing_sentence_comes_first_whatever_order_the_answers_came_in(client, conn, fake):
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    answers = {"service": {"choice": "svc_invisalign"}, "listing": {"choice": "gm_1"}}
    res = call_tool(client, "answer", {"session_id": sid, "answers": answers, "idempotency_key": "o1"}, key=key)
    assert res_say(res).startswith("Linked to Bright Smile Dental on Google Maps.")


# ---------------------------------------------------------------- "not my business" in the owner's own words
# (plan 2026-10-03-agent-listing-not-mine-words, Task 1)

NOT_MINE = "that's not my business"


def _not_mine(**fields) -> models.AnswerRead:
    """A read of words that say the Google Maps place linked is not the owner's (D1); unclear otherwise."""
    blank = {"option_id": None, "amount_usd": None, "miles": None, "note": None, "understood": False, "services": []}
    return models.AnswerRead(**{**blank, "listing_not_mine": True, **fields})


def _linked_walk(client, conn) -> tuple[str, str]:
    """A started dental walk whose owner picked the place offered: linked, the ads centred on it. (key, session id)."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    _listing(client, key, sid, {"choice": "gm_1"}, "pick")
    assert consult.get(conn, "biz_1", sid)["answers"]["location"]["source"] == "known_fact"
    return key, sid


def test_words_that_say_the_listing_is_not_theirs_unlink_it_and_ask_the_question_again(client, conn, fake):
    """D4: an open consultation -- the words answer nothing, the listing is unlinked as Settings unlinks it, its centre
    goes (the question of where the ads centre comes back), and the question that waited is asked again after the
    sentences that say so. The read was a model call: today's cap counts it."""
    key, sid = _linked_walk(client, conn)
    fake.reads[NOT_MINE] = _not_mine()
    res = _answer(client, key, sid, {"objective": {"other": NOT_MINE}}, "nm1")
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert profile.business_listing(k) is None and k["listing_rejected"] == ["ChIJ1"]
    session = consult.get(conn, "biz_1", sid)
    assert "objective" not in session["answers"] and consult.RETRY_KEY not in session["answers"]  # nothing recorded
    assert "location" in [q["id"] for q in consult.pending(session)]
    nxt = consult.next_question(session)
    assert nxt["question"]["id"] == "objective" and res["next"] == nxt
    # the question that comes next is not where the ads centre: the sentence says it will be asked, it asks for no
    # address that nothing would take (final review M2)
    assert res["say"] == (
        "Unlinked Bright Smile Dental: Admiry will not use it, or offer it again. Admiry will ask where the ads should "
        "centre. To link the right one, use Find it in Settings, or give its name and address to your assistant. "
        f"{consult.question_sentence(nxt)}"
    )
    assert listings.NO_CENTRE_ASKED == " Admiry will ask where the ads should centre."
    assert res["data"]["listing"] == "unlink" and res["data"]["recorded"] == {} and res["data"]["ready"] is False
    assert res["data"]["remaining"] == [q["id"] for q in consult.pending(session)]
    row = conn.execute(
        "SELECT ok, args_redacted->>'free_text' AS f FROM agent_action WHERE tool = 'answer' AND idempotency_key = 'nm1'"
    ).fetchone()
    assert row["ok"] and row["f"] == "true"  # auth.check_llm_cap counts this row: the read was one of today's
    # Review focus 5: the same words again -- nothing is linked any more, so they are an answer like any other
    again = _answer(client, key, sid, {"objective": {"other": NOT_MINE}}, "nm2")
    assert "listing" not in again["data"] and again["say"].startswith("I did not quite get that.")
    assert consult.get(conn, "biz_1", sid)["answers"][consult.RETRY_KEY] == {"objective": 1}


def test_when_the_centre_is_the_question_that_comes_next_the_sentence_asks_for_the_address(client, conn, fake):
    """Final review M2, the other order: the walk answered, the consultation ready; the unlink opens it again with the
    question of where the ads centre the only one left, so it is the one asked next and the sentence asks for the
    address that question takes."""
    key, sid = _linked_walk(client, conn)
    for qid, choice in (("objective", "bookings"), ("service", "svc_invisalign"), ("customer_value", "1000_plus")):
        _answer(client, key, sid, {qid: {"choice": choice}}, f"w_{qid}")
    assert consult.get(conn, "biz_1", sid)["status"] == "ready"
    fake.reads[NOT_MINE] = _not_mine()
    res = _answer(client, key, sid, {"objective": {"other": NOT_MINE}}, "nm6")
    nxt = consult.next_question(consult.get(conn, "biz_1", sid))
    assert nxt["question"]["id"] == "location" and res["next"] == nxt
    assert res["say"] == (
        listings.UNLINKED.format(name="Bright Smile Dental")
        + listings.NO_CENTRE
        + listings.LINK_AGAIN
        + f" {consult.question_sentence(nxt)}"
    )
    assert res["data"]["listing"] == "unlink" and consult.get(conn, "biz_1", sid)["status"] == "open"


def test_a_listing_unlinked_meanwhile_reads_as_a_reply(client, conn, fake, monkeypatch):
    """The race (Task 1's deferred minor, final review): another path unlinks the listing after the words were read and
    before the command's body runs. Nothing is linked any more: the sentence says so and how to link the right place,
    then the question that waits -- and nothing is written, no listing change, no answer, no retry counted."""
    key, sid = _linked_walk(client, conn)
    fake.reads[NOT_MINE] = _not_mine()
    real = consult.parse

    def read_then_unlinked(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        finally:
            listings.unlink_listing(conn, "biz_1")  # Settings' "Not my business", between the read and the body

    monkeypatch.setattr(consult, "parse", read_then_unlinked)
    before = consult.get(conn, "biz_1", sid)["answers"]
    version = business.latest_knowledge(conn, "biz_1")[0]
    res = _answer(client, key, sid, {"objective": {"other": NOT_MINE}}, "nm7")
    nxt = consult.next_question(consult.get(conn, "biz_1", sid))
    assert res["say"] == listings.NOTHING_LINKED + listings.LINK_AGAIN + f" {consult.question_sentence(nxt)}"
    assert "listing" not in res["data"] and res["data"]["recorded"] == {} and res["next"] == nxt
    assert consult.get(conn, "biz_1", sid)["answers"] == before  # nothing recorded, no retry counted
    assert profile.business_listing(business.latest_knowledge(conn, "biz_1")[1]) is None
    assert business.latest_knowledge(conn, "biz_1")[1]["listing_rejected"] == ["ChIJ1"]  # the other path's, only
    assert business.latest_knowledge(conn, "biz_1")[0] == version + 1  # the other path's unlink, none of the answer's
    row = conn.execute("SELECT ok FROM agent_action WHERE tool = 'answer' AND idempotency_key = 'nm7'").fetchone()
    assert row["ok"]


def test_with_no_listing_linked_the_words_take_their_usual_path(client, conn, fake):
    """D3 / review focus 2: nothing linked, the flag changes nothing -- the read's other fields answer the question,
    and unclear words are asked about again; nothing is unlinked or written."""
    benchmark.seed(conn)
    body = _start(client, conn)
    key, sid = body["key"], body["data"]["session_id"]
    _listing(client, key, sid, {"choice": "none"}, "none")
    version = business.latest_knowledge(conn, "biz_1")[0]
    fake.reads[NOT_MINE] = _not_mine(option_id="calls", understood=True)
    picked = _answer(client, key, sid, {"objective": {"other": NOT_MINE}}, "nm3")
    assert picked["data"]["recorded"]["objective"]["choice"] == "calls" and "listing" not in picked["data"]
    fake.reads["the other one"] = _not_mine()
    unclear = _answer(client, key, sid, {"service": {"other": "the other one"}}, "nm4")
    assert unclear["say"].startswith("I did not quite get that.") and unclear["data"]["recorded"] == {}
    assert business.latest_knowledge(conn, "biz_1")[0] == version


def test_a_complaint_never_unlinks_the_listing(client, conn, fake):
    """Review focus 1: a read that sets only `support: dissatisfied` is no listing (Admiry's support off, the default:
    the words are an unclear answer, as before)."""
    key, sid = _linked_walk(client, conn)
    fake.reads["this is not what I wanted"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=False, support="dissatisfied", services=[]
    )
    res = _answer(client, key, sid, {"objective": {"other": "this is not what I wanted"}}, "nm5")
    assert "listing" not in res["data"] and res["say"].startswith("I did not quite get that.")
    assert profile.business_listing(business.latest_knowledge(conn, "biz_1")[1])["place_id"] == "ChIJ1"


def test_the_dental_walk_through_ends_in_a_plan_with_cards(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    a0 = _answer(client, key, sid, PICKED, "a0")  # the business on Google Maps, first: it centres the ads
    assert a0["next"]["question"]["id"] == "objective"
    a1 = _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "a1")
    assert a1["data"]["recorded"] == {"objective": {"choice": "bookings", "raw": "bookings", "source": "choice"}}
    assert a1["next"]["question"]["id"] == "service" and a1["next"]["question"]["options"][0]["label"] == "Invisalign"
    a2 = _answer(client, key, sid, {"service": {"choice": "svc_invisalign"}}, "a2")
    assert "book more patients than a general page" in a2["say"]  # the tree's `why` sentence follows the answer
    assert a2["next"]["question"]["prompt"] == "Roughly, what is one new Invisalign patient worth to you?"
    a3 = _answer(client, key, sid, {"customer_value": {"other": "around three thousand"}}, "a3")
    assert a3["data"]["ready"] is True and a3["next"]["kind"] == "actions"
    assert a3["data"]["recorded"]["customer_value"]["amount_usd"] == 3000.0
    # the model reads the free text against the prompt the owner saw, {service} filled in
    assert fake.read_calls[-1]["question"]["prompt"] == "Roughly, what is one new Invisalign patient worth to you?"
    assert [s["done"] for s in a3["data"]["steps"]] == [True, True, True, False, False]
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    r = res["structuredContent"]
    cards = r["data"]["cards"]
    assert cards["plan"]["goal"] == "More Invisalign bookings within 5 miles of Bright Smile Dental"
    assert cards["plan"]["channels"] == [
        {
            "platform": "google_search",
            "label": "Google Search",
            "role": "primary",
            "monthly_usd": 2000,
            "per_day": 66,
            "why": "Google Search gets all of it because people there are already looking for Invisalign near you.",
            "counts_as_result": ["consultation_booking", "phone_call_60s"],
            "mode_text": "your own Google Ads account",
        }
    ]  # Invisalign is meta_fit, but Facebook & Instagram has no adapter yet: the draft's Meta is a later line
    assert [(x["platform"], x["why"]) for x in cards["plan"]["later"]] == [
        ("meta", "Not in this plan yet; coming to Admiry."),
        ("chatgpt_ads", "Later: Admiry is trying it on its own ads first."),
        ("yelp", "Not now: Yelp opens its ads only to large partners."),
        ("tiktok", "Worth a test after 30 days of booking data. Not now."),
    ]
    assert cards["budget"] == {
        "monthly_usd": 2000,
        "per_day": 66,
        "level": "recommended",
        "levels": {
            level: {"monthly_usd": amount, "split": {"google_search": amount}}
            for level, amount in {"minimum": 300, "conservative": 900, "recommended": 2000, "aggressive": 2000}.items()
        },
    }  # the owner said $2,000/mo
    assert (
        cards["economics"]["columns"]["planning"]["spend"] == 2000
        and cards["economics"]["assumptions"][3]["basis"] == "your answer"
    )
    assert cards["rationale"]["example_searches"][0] == "invisalign palo alto"
    # `_start` now keys onto a claimed business (A1b-4b Task 4): the launch actions, not sign-in
    # (a draft key's sign-in gate: test_a_draft_key_is_asked_to_sign_in_before_launching).
    # Admiry running the ads leads, then the owner's own account, then a change (founder's ruling, 2026-10-07)
    assert r["next"]["kind"] == "actions" and launch_ids(r) == ["prepay", "link_own_account", "adjust"]
    assert "not a forecast" in r["say"] and not JARGON.search(r["say"])
    row = conn.execute("SELECT json FROM plan WHERE id = %s", (r["data"]["plan_id"],)).fetchone()["json"]
    assert (
        row["sections"]["consultation"]["objective"] == "bookings"
        and row["sections"]["consultation"]["geo"]["radius_miles"] == 5.0
    )
    assert [c["channel"] for c in row["sections"]["ads"]["channels"]] == ["google_search"]
    assert row["sections"]["rationale"]["why_primary"]
    assert consult.get(conn, b["data"]["business_id"], sid)["status"] == "planned"
    calls = conn.execute("SELECT count(*) AS c FROM agent_action WHERE ok").fetchone()["c"]
    assert calls <= 30


def test_the_forecast_calls_the_second_number_booked_whatever_the_objective(client, conn, fake):
    """The economics table's second row is "Estimated bookings"; the sentence said "12–24 phone calls" for a calls
    objective, which an owner reads as calls (the leads row). Both now say booked."""
    env = recommended_with(client, conn, fake, objective="calls")
    p = env["data"]["cards"]["economics"]["columns"]["planning"]
    expected = (
        f"bring {p['leads'][0]} to {p['leads'][1]} leads, {p['bookings'][0]} to {p['bookings'][1]} of them booked."
    )
    assert expected in env["say"], env["say"]
    assert "phone calls." not in env["say"].split("In 30 days")[1]


def test_recommend_hands_the_model_the_budget_the_plan_will_spend(client, conn, fake):
    """The model wrote `ads: []` for a goal with no monthly limit (the prompt's own rule), so no plan since September
    carried ad text. The draft is now asked with the recommended level as the goal's limit and the consultation's
    monthly_budget_usd."""
    fake.knowledge.goal_hint.monthly_budget_usd = None  # no limit stated: the fixture's own hint would state $2,000
    env = recommended_with(client, conn, fake, objective="calls")
    spend = env["data"]["cards"]["budget"]["levels"]["recommended"]["monthly_usd"]
    call = fake.plan_calls[-1]
    assert call["goal"]["monthly_limit_usd"] == spend
    assert call["consultation"]["monthly_budget_usd"] == spend and call["consultation"]["channel"] == "google_search"


def test_recommend_hands_the_model_the_owners_stated_limit_when_there_is_one(client, conn, fake):
    recommended_with(client, conn, fake, objective="calls", monthly_budget_usd=1500)  # a known fact at start
    call = fake.plan_calls[-1]
    assert call["goal"]["monthly_limit_usd"] == 1500 and call["consultation"]["monthly_budget_usd"] == 1500


# What tools_consult logs about the ad text of biz_1's plan draft: the retry, then the refusal (Task 2 round 1)
ASKED_AGAIN = "plan draft: the ad text was missing; asking once more (business biz_1)"
REFUSED = "plan draft: the ad text was missing twice; refused (business biz_1)"


def _ad_text_log(caplog) -> list[str]:
    """tools_consult's log lines about a plan draft's ad text, in order."""
    lines = [r.getMessage() for r in caplog.records if r.name == "agent.tools_consult"]
    return [line for line in lines if line.startswith("plan draft:")]


def test_a_draft_without_the_ad_text_is_asked_once_more(client, conn, fake, caplog):
    thin = fake.draft.model_copy(deep=True)
    thin.ads[0].headlines, thin.ads[0].descriptions = ["Only one"], []
    drafts = iter([thin, fake.draft])
    fake.draft_plan = lambda *a, **kw: (fake.plan_calls.append((a, kw)), next(drafts))[1]
    with caplog.at_level(logging.INFO, logger="agent.tools_consult"):
        env = recommended_with(client, conn, fake, objective="calls")
    assert len(fake.plan_calls) == 2 and fake.plan_calls[1] == fake.plan_calls[0]  # asked again exactly as before
    assert len(env["data"]["cards"]["ads_preview"]["headlines"]) == 3
    assert _ad_text_log(caplog) == [ASKED_AGAIN]


def test_a_draft_still_without_the_ad_text_is_refused_and_writes_no_plan(client, conn, fake, caplog):
    thin = fake.draft.model_copy(deep=True)
    thin.ads[0].headlines, thin.ads[0].descriptions = [], []
    fake.draft_plan = lambda *a, **kw: (fake.plan_calls.append(kw), thin)[1]
    walk = answered_consultation(client, conn, fake, objective="calls")  # the file's helper: ready, not built
    with caplog.at_level(logging.INFO, logger="agent.tools_consult"):
        res = call_tool(
            client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "rec-thin"}, key=walk["key"]
        )
    assert res["isError"] and tools_consult.NO_AD_TEXT in tool_text(res)
    assert (
        conn.execute("SELECT count(*) FROM plan WHERE business_id = %s", (walk["business_id"],)).fetchone()["count"]
        == 0
    )
    assert (
        conn.execute("SELECT status FROM consult_session WHERE id = %s", (walk["session_id"],)).fetchone()["status"]
        == "ready"
    )
    assert len(fake.plan_calls) == 2
    assert _ad_text_log(caplog) == [ASKED_AGAIN, REFUSED]


def test_draft_budget_reads_a_system_ceiling_as_no_stated_budget(conn):
    """A limit Admiry wrote (`limit_source` "system": recommend's aggressive level when the owner named none) is no
    budget the owner stated: a later draft is asked with the benchmark's recommended level, as `build` spends it, not
    with that ceiling. The same number stated by the owner is theirs."""
    benchmark.seed(conn)
    dentist, ceiling = {"category": "dentists"}, {"monthly_limit_usd": 3700.0, "limit_source": "system"}
    assert tools_consult.draft_budget(conn, "biz_1", dentist, ceiling) == 2200
    assert tools_consult.draft_budget(conn, "biz_1", dentist, {**ceiling, "limit_source": "owner"}) == 3700


class _Model:
    """A stand-in for the model's plan draft (`llm.Generator.draft_plan`): each call takes the next of `replies` (a
    draft, or an LLMError it raises; the last one repeats) and records its arguments in `calls`."""

    def __init__(self, *replies):
        self.replies, self.calls = replies, []

    def draft_plan(self, *args, **kw):
        self.calls.append((args, kw))
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _guarded(model):
    """`draft_with_copy` as recommend asks it, for the dental fixture business (the dentists' banned words)."""
    return tools_consult.draft_with_copy(
        model,
        knowledge_fixture().model_dump(),
        {"monthly_limit_usd": 2000},
        biz="biz_1",
        consultation={"channel": "google_search", "monthly_budget_usd": 2000},
    )


def _google_text(headlines, descriptions=None) -> models.PlanDraft:
    """The fixture draft with its Google Search entry's ad text replaced (its two descriptions kept unless given)."""
    draft = plan_draft_fixture()
    draft.ads[0].headlines = list(headlines)
    if descriptions is not None:
        draft.ads[0].descriptions = list(descriptions)
    return draft


CLEAN_HEADLINES = ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners, Family Care"]
# each one character over Google's 30 (plans.HEADLINE_MAX), and clean by every other rule
TOO_LONG_HEADLINES = [
    "Invisalign Near Palo Alto Today",
    "Same-Week Visits, Palo Alto, CA",
    "Clear Aligners From Family Care",
]
# the business's own number in each (knowledge_fixture's phone): within the length limit, and the launch's linter
# drops every one -- a phone number never runs in an ad's text
PHONE_HEADLINES = ["Call 650-555-0100 Today", "Invisalign: 650-555-0100", "Book at 650-555-0100"]


def test_ad_text_over_the_length_limit_is_refused_after_one_more_ask():
    assert {len(h) for h in TOO_LONG_HEADLINES} == {plans.HEADLINE_MAX + 1}
    model = _Model(_google_text(TOO_LONG_HEADLINES))
    with pytest.raises(ToolError) as refused:
        _guarded(model)
    assert str(refused.value) == tools_consult.NO_AD_TEXT and len(model.calls) == 2


def test_ad_text_the_launch_linter_drops_is_refused_and_the_same_draft_with_clean_lines_passes():
    """The guard judges the ad text as the launch's precheck does (`lint_copy` with the trade's banned words):
    headlines carrying the business's phone number fit the length limit, and the launch would refuse them as too
    little ad text (TOO_LITTLE_COPY)."""
    assert all(len(h) <= plans.HEADLINE_MAX for h in PHONE_HEADLINES)
    phoned = _Model(_google_text(PHONE_HEADLINES))
    with pytest.raises(ToolError) as refused:
        _guarded(phoned)
    assert str(refused.value) == tools_consult.NO_AD_TEXT and len(phoned.calls) == 2
    clean = _Model(_google_text(CLEAN_HEADLINES))
    assert _guarded(clean) is clean.replies[0] and len(clean.calls) == 1


def test_the_ad_text_guard_reads_google_named_twice_as_the_plan_merges_it():
    """`plans.normalize` merges a channel the draft names twice (its lines concatenated): 2 + 2 headlines and 1 + 1
    descriptions are the 4 and 2 the plan runs, so the draft passes at once. An entry with no money is dropped there,
    so its lines do not count."""
    draft = _google_text(CLEAN_HEADLINES[:2], ["Clear aligners from a family dentist. Book online."])
    more = {
        "headlines": [CLEAN_HEADLINES[2], "Invisalign Consults"],
        "descriptions": ["Book a same-week consultation."],
    }
    draft.ads.append(draft.ads[0].model_copy(update={**more, "monthly_usd": 300}))
    model = _Model(draft)
    assert _guarded(model) is draft and len(model.calls) == 1
    unfunded = draft.model_copy(deep=True)
    unfunded.ads[-1].monthly_usd = 0
    with pytest.raises(ToolError):
        _guarded(_Model(unfunded))


BUSY = "the assistant is busy; try again in a minute"


@pytest.mark.parametrize("asks", [1, 2], ids=["first_ask", "second_ask"])
def test_a_model_error_on_the_plan_draft_is_a_refusal_in_the_models_words(asks):
    model = _Model(*[_google_text(["Only one"], [])] * (asks - 1), llm.LLMError(BUSY))
    with pytest.raises(ToolError) as refused:
        _guarded(model)
    assert str(refused.value) == BUSY and len(model.calls) == asks


def test_a_model_error_on_recommend_reads_as_the_models_sentence_and_writes_no_plan(client, conn, fake):
    fake.plan_error = BUSY  # the fake's draft_plan raises llm.LLMError(BUSY)
    walk = answered_consultation(client, conn, fake, objective="calls")
    res = call_tool(
        client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "rec-busy"}, key=walk["key"]
    )
    assert res["isError"] and BUSY in tool_text(res) and _internal_errors(conn) == 0
    assert conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"] == 0


def _no_budget_plan(client, conn, fake):
    """recommend with no budget the owner stated: 30 × $72.97 → $2,200; levels 300 / 1,000 / 2,200 / 3,700."""
    fake.knowledge.goal_hint.monthly_budget_usd = None
    b = _start(
        client,
        conn,
        "dentist in Palo Alto",
        known_facts={"objective": "bookings", "service": "Invisalign", "customer_value_usd": 2500, "radius_miles": 5},
    )
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, PICKED, "pick")  # the business on Google Maps: the one question no fact answers
    r = data(call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key))
    assert r["cards"]["budget"]["monthly_usd"] == 2200
    assert _amounts(r["cards"]["budget"]["levels"]) == LEVELS_2200
    return key, r


LEVELS_2200 = {"minimum": 300, "conservative": 1000, "recommended": 2200, "aggressive": 3700}
NO_BENCHMARK = "no published benchmark; we will learn from the first 14 days"


def _amounts(levels: dict) -> dict[str, int]:
    """The level control's amounts (since M0 Task 5 each level carries its split beside its amount)."""
    return {level: v["monthly_usd"] for level, v in levels.items()}


def _adjust(client, key, plan_id, ik, **changes):
    res = call_tool(client, "adjust", {"plan_id": plan_id, "idempotency_key": ik, **changes}, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def fake_key(conn) -> str:
    """The plaintext of the walk's key is not stored; mint a second manage key for biz_1 for a direct tool call."""
    return business_with_key(conn, biz_id="biz_1", claim_state="claimed")


def _internal_errors(conn) -> int:
    return conn.execute(
        "SELECT count(*) AS c FROM agent_action WHERE NOT ok AND result->>'error' = 'internal error'"
    ).fetchone()["c"]


def test_adjust_without_a_stated_budget_keeps_the_levels_recommend_showed(client, conn, fake):
    benchmark.seed(conn)
    key, r = _no_budget_plan(client, conn, fake)
    goal = conn.execute("SELECT monthly_limit_usd, limit_source FROM goal ORDER BY created_at DESC LIMIT 1").fetchone()
    assert float(goal["monthly_limit_usd"]) == 3700 and goal["limit_source"] == "system"  # Admiry's ceiling

    wider = _adjust(client, key, r["plan_id"], "j1", radius_miles=10)["data"]
    assert (
        wider["cards"]["budget"]["monthly_usd"] == 2200 and _amounts(wider["cards"]["budget"]["levels"]) == LEVELS_2200
    )
    assert wider["cards"]["plan"]["setup"]["area"].startswith("within 10 miles")
    assert wider["version"] == 2 and wider["plan_id"] != r["plan_id"]
    assert (
        conn.execute("SELECT diff_from FROM plan WHERE id = %s", (wider["plan_id"],)).fetchone()["diff_from"]
        == r["plan_id"]
    )
    assert len(fake.plan_calls) == 1  # adjust never calls the model

    low = _adjust(client, key, wider["plan_id"], "j2", level="conservative")["data"]
    assert low["cards"]["budget"]["monthly_usd"] == 1000 and _amounts(low["cards"]["budget"]["levels"]) == LEVELS_2200
    high = _adjust(client, key, low["plan_id"], "j3", level="aggressive")["data"]
    assert high["cards"]["budget"]["monthly_usd"] == 3700
    assert high["cards"]["economics"]["columns"]["planning"]["spend"] == 3700

    plans_before = conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"]
    refused = call_tool(
        client, "adjust", {"plan_id": high["plan_id"], "monthly_budget_usd": 3800, "idempotency_key": "j4"}, key=key
    )
    assert refused["isError"] and "$3,700" in tool_text(refused) and "raise it first" in tool_text(refused)
    assert "Admiry set" in tool_text(refused)
    assert conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"] == plans_before

    g = call_tool(client, "set_goal", {"monthly_budget_usd": 5000, "idempotency_key": "g1"}, key=key)
    assert not g["isError"], tool_text(g)
    assert data(g)["goal"]["limit_source"] == "owner"
    more = _adjust(client, key, high["plan_id"], "j5", monthly_budget_usd=3800)["data"]
    assert more["cards"]["budget"]["monthly_usd"] == 3800 and more["cards"]["budget"]["level"] == "custom"


def test_a_changed_version_carries_the_money_line_the_plan_says_now(client, conn, fake):
    """Owner-chooses review, fix round 1 (item 3): a change keeps the version's own sections, but not its money line --
    a version written before the founder's ruling of 2026-10-13 named the owner's own account first; the version a change
    makes carries the line a plan says now (plans.DISCLOSURE: both ways side by side), as a fresh build writes it."""
    benchmark.seed(conn)
    key, r = _no_budget_plan(client, conn, fake)
    before = (
        "Google Ads charges are billed by Google to your account, or spent from your prepaid balance at cost when "
        "Admiry runs the ads; Admiry charges no management fee."
    )
    conn.execute(
        "UPDATE plan SET json = jsonb_set(jsonb_set(json, '{sections,disclosure,text}', to_jsonb(%(t)s::text)), "
        "'{lines,disclosure}', to_jsonb(%(t)s::text)) WHERE id = %(id)s",
        {"t": before, "id": r["plan_id"]},
    )
    changed = _adjust(client, key, r["plan_id"], "d1", radius_miles=10)["data"]
    plan = plans.get(conn, "biz_1", changed["plan_id"])
    assert plan["sections"]["disclosure"] == {"text": plans.DISCLOSURE} != {"text": before}
    assert plan["lines"]["disclosure"] == changed["lines"][-1] == plans.DISCLOSURE


def test_adjust_after_a_custom_budget_keeps_it_through_other_edits(client, conn, fake):
    benchmark.seed(conn)
    key, r = _no_budget_plan(client, conn, fake)
    custom = _adjust(client, key, r["plan_id"], "c1", monthly_budget_usd=1500)["data"]
    assert custom["cards"]["budget"]["monthly_usd"] == 1500 and custom["cards"]["budget"]["level"] == "custom"

    wider = _adjust(client, key, custom["plan_id"], "c2", radius_miles=10)["data"]
    assert wider["cards"]["budget"]["monthly_usd"] == 1500 and wider["version"] == 3
    assert wider["cards"]["plan"]["setup"]["area"].startswith("within 10 miles")

    offered = _adjust(client, key, wider["plan_id"], "c3", offer="Free scan")["data"]
    assert offered["cards"]["plan"]["setup"]["offer"] == "Free scan" and offered["version"] == 4
    assert offered["cards"]["budget"]["monthly_usd"] == 1500

    calls = _adjust(client, key, offered["plan_id"], "c4", objective="calls")
    cd = calls["data"]
    assert cd["cards"]["plan"]["setup"]["goal"] == "phone calls" and "phone calls" in cd["cards"]["plan"]["goal"]
    assert cd["cards"]["budget"]["monthly_usd"] == 1500 and cd["version"] == 5
    assert cd["cards"]["plan"]["setup"]["offer"] == "Free scan"  # earlier edits stay
    say = calls["say"]
    assert "phone calls" in say
    assert "of them booked." in say and "phone calls." not in say.split("In 30 days")[1]
    assert _internal_errors(conn) == 0


def test_every_question_turn_says_the_question(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    turns = [b]
    turns.append(_answer(client, key, sid, {"objective": {"other": "zzz"}}, "q1"))  # asked again: say repeats it
    turns.append(_answer(client, key, sid, {"objective": {"choice": "bookings"}}, "q2"))
    turns.append(_answer(client, key, sid, {"service": {"choice": "svc_invisalign"}}, "q3"))
    for t in turns:
        q = t["next"]["question"]
        assert q["prompt"] in t["say"], t["say"]
        assert all(o["label"] in t["say"] for o in q["options"]), t["say"]
        assert "your own words" in t["say"] and not JARGON.search(t["say"])


def test_an_llm_error_reading_free_text_is_a_readable_refusal(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    fake.error = "the assistant is busy; try again in a minute"
    res = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"objective": {"other": "more patients"}}, "idempotency_key": "l1"},
        key=key,
    )
    assert res["isError"] and "the assistant is busy; try again in a minute" in tool_text(res)
    assert _internal_errors(conn) == 0
    assert consult.get(conn, b["data"]["business_id"], sid)["answers"].get("objective") is None


def test_revise_plan_on_a_consulted_plan_stays_on_google_search(client, conn, fake):
    benchmark.seed(conn)
    key, r = _no_budget_plan(client, conn, fake)
    res = call_tool(
        client,
        "revise_plan",
        {"plan_id": r["plan_id"], "instruction": "focus on implants", "idempotency_key": "v1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    assert fake.plan_calls[-1]["instruction"] == "focus on implants"
    got = data(call_tool(client, "get_plan", {}, key=key))
    assert got["plan_id"] == data(res)["plan_id"] and got["version"] == 2
    assert [(c["channel"], c["monthly_usd"]) for c in got["ads"]["channels"]] == [("google_search", 2200.0)]
    assert (
        _amounts(got["cards"]["budget"]["levels"]) == LEVELS_2200 and got["cards"]["budget"]["level"] == "recommended"
    )
    row = conn.execute("SELECT json FROM plan WHERE id = %s", (got["plan_id"],)).fetchone()["json"]
    assert {"consultation", "rationale", "levels"} <= set(row["sections"])
    assert "columns" in row["sections"]["estimate"]
    after = _adjust(client, key, got["plan_id"], "v2", radius_miles=10)["data"]  # adjust still finds the session
    assert after["cards"]["budget"]["monthly_usd"] == 2200 and after["version"] == 3


def test_a_draft_key_is_asked_to_sign_in_before_launching(client, conn, fake):
    # A genuine draft key (not `_start`'s any more, which now keys onto an already-claimed business:
    # test_the_dental_walk_through_ends_in_a_plan_with_cards): a session built directly, the way
    # test_a_claimed_key_gets_the_google_ads_actions builds one for a claimed key.
    benchmark.seed(conn)
    key = business_with_key(conn, cls="draft")
    knowledge = knowledge_fixture().model_dump()
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist in Palo Alto",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": "svc_invisalign", "raw": "", "source": "choice"},
            "customer_value": {"amount_usd": 2500.0, "raw": "", "source": "known_fact"},
            "location": LATER,
            "radius": {"miles": 5.0, "raw": "", "source": "known_fact"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    r = data(call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key))
    res = call_tool(client, "adjust", {"plan_id": r["plan_id"], "radius_miles": 10, "idempotency_key": "d1"}, key=key)
    for env in (res["structuredContent"],):
        # ruling R2-e: the four ways' tools need a signed-in business, so a draft key's `next` is the sign-in alone; the
        # row's sentence still says where the code stands, in `say` and in the cards
        assert env["next"] == tools_consult.SIGN_IN_ACTIONS
        acts = env["next"]["actions"]
        assert [a["tool"] for a in acts] == ["request_connect_link", "adjust"] and acts[0]["primary"] is True
        assert site_install.NEEDS_CODE in env["say"] and env["say"].endswith("To launch it, the owner signs in first.")
        assert env["data"]["cards"]["plan"]["setup"]["website"]["state"] == "needs_code"


def test_a_claimed_key_gets_the_google_ads_actions(client, conn, fake):
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    knowledge = knowledge_fixture().model_dump()
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    business.set_goal(conn, "biz_1", monthly_limit_usd=2000)
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": "svc_invisalign", "raw": "", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "amount_usd": 3000.0, "raw": "", "source": "choice"},
            "location": LATER,
            "radius": {"miles": 5.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    res = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    # Admiry running the ads first, then the owner's own Google Ads account, then a change (founder's ruling,
    # 2026-10-07; before it, walk fixes 1 Task 6 offered the own account alone) -- and since 2026-10-13 the two ways
    # side by side, neither marked first (the home's plan card draws them alike)
    ways = site_install.ways_for(None)  # support off: without Admiry's team (ruling R3-f)
    assert env["next"] == envelope.actions(
        *ways, tools_consult.MANAGED_ACTION, tools_consult.OWN_ACCOUNT_ACTION, tools_consult.ADJUST_ACTION
    )
    assert launch_ids(env) == ["prepay", "link_own_account", "adjust"]
    lead, own = env["next"]["actions"][len(ways) : len(ways) + 2]
    assert "primary" not in lead and "primary" not in own
    # an assistant follows Admiry's way with connect_channel: the step on the web where the way is chosen, never a
    # refusal -- once the page is settled (the founder's ruling, 2026-10-13: the page comes first)
    res = call_tool(client, "connect_channel", {**lead["args"], "idempotency_key": "m0"}, key=key)
    assert res["structuredContent"]["data"]["status"] == "page_first", tool_text(res)
    settle_page(conn)
    res = call_tool(client, "connect_channel", {**lead["args"], "idempotency_key": "m1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"]["status"] == "web_step"
    assert "the owner signs in first" not in env["say"]


def test_the_plan_answer_says_the_row_and_offers_the_four_ways_before_the_launch_actions_only_while_needed(
    client, conn, fake
):
    """A3p-5 batch 2 Q5, Q6 (rulings R2-a, R2-c): the plan answer's `say` carries the "Your website" row's sentence; while
    the code is missing, its `next` lists the four ways -- Q6's actions, with their arguments -- before the launch
    actions; once the code is found on the page the ads use, or the ads use an Admiry page, the launch actions alone;
    with no website of its own, no row."""
    from agent import landing, site_install
    from agent.tests.test_site_install import ADMIRY_PAGE, FOUND, NEEDS_CODE, OFFERED, _checked, _hosted, _no_website

    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    knowledge = knowledge_fixture().model_dump()  # its website: https://brightsmile.example
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    business.set_goal(conn, "biz_1", monthly_limit_usd=2000)
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": "svc_invisalign", "raw": "", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "amount_usd": 3000.0, "raw": "", "source": "choice"},
            "location": LATER,
            "radius": {"miles": 5.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    launch = [tools_consult.MANAGED_ACTION, tools_consult.OWN_ACCOUNT_ACTION, tools_consult.ADJUST_ACTION]
    res = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["next"] == envelope.actions(*OFFERED, *launch)  # support off: without Admiry's team (R3-f)
    assert f" {NEEDS_CODE} Details: " in env["say"] and not JARGON.search(env["say"])
    assert env["data"]["cards"]["plan"]["setup"]["website"]["state"] == "needs_code"
    assert env["data"]["launch_note"] is None
    plan_id = env["data"]["plan_id"]

    def changed(ik: str, miles: float) -> dict:
        args = {"plan_id": plan_id, "radius_miles": miles, "idempotency_key": ik}
        got = call_tool(client, "adjust", args, key=key)
        assert not got["isError"], tool_text(got)
        return got["structuredContent"]

    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    _checked(conn, page["id"], found=True)
    env = changed("a1", 10)
    assert env["next"] == envelope.actions(*launch) and f" {FOUND} Details: " in env["say"]
    _hosted(conn, "biz_1")
    env = changed("a2", 12)
    assert env["next"] == envelope.actions(*launch) and f" {ADMIRY_PAGE} Details: " in env["say"]
    _no_website(conn, "biz_1")
    env = changed("a3", 15)
    assert env["next"] == envelope.actions(*launch)
    assert not any(words in env["say"] for words in (NEEDS_CODE, FOUND, ADMIRY_PAGE))
    assert site_install.row(conn, "biz_1") is None


def test_choice_only_answers_do_not_spend_the_draft_llm_cap(client, conn, fake, monkeypatch):
    # A genuine draft key (see test_a_draft_key_is_asked_to_sign_in_before_launching's note): the cap this
    # test bends (LLM_CALLS_PER_DAY_DRAFT) only applies to a draft-class key.
    benchmark.seed(conn)
    key = business_with_key(conn, cls="draft")
    knowledge = knowledge_fixture().model_dump()
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={},
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    sid = s["id"]
    for i in range(11):  # more than the draft cap of 10: choices never call the model
        _answer(client, key, sid, {"objective": {"choice": "nope"}}, f"ch{i}")
    # still under the cap; the free text sorts after a long choice, past what the audit row keeps of `answers`
    both = {"customer_value": {"choice": "not-an-option-" * 6}, "objective": {"other": "zzz"}}
    _answer(client, key, sid, both, "o1")
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY_DRAFT", 2)
    _answer(client, key, sid, {"objective": {"other": "zzz"}}, "o2")  # the first free text counted: 1 < 2
    res = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"service": {"other": "zzz"}}, "idempotency_key": "o3"},
        key=key,
    )
    assert res["isError"] and "today's limit of 2" in tool_text(res)  # two free-text answers counted


def test_adjust_above_the_stated_limit_is_refused_with_the_limit_path(client, conn, fake):
    benchmark.seed(conn)
    b = _start(
        client,
        conn,
        "dentist in Palo Alto, $2,000 a month",
        known_facts={"objective": "bookings", "service": "Invisalign", "customer_value_usd": 2500, "radius_miles": 5},
    )
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, PICKED, "pick")  # the business on Google Maps: the one question no fact answers
    r = data(call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key))
    a = call_tool(
        client, "adjust", {"plan_id": r["plan_id"], "monthly_budget_usd": 3000, "idempotency_key": "j1"}, key=key
    )
    assert a["isError"] and "$2,000" in tool_text(a) and "raise the limit first" in tool_text(a)
    assert conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"] == 1


def test_adjust_below_the_minimum_spend_is_refused_and_writes_no_version(client, conn, fake):
    benchmark.seed(conn)
    b = _start(
        client,
        conn,
        "dentist in Palo Alto",
        known_facts={"objective": "bookings", "service": "Invisalign", "customer_value_usd": 2500, "radius_miles": 5},
    )
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, PICKED, "pick")  # the business on Google Maps: the one question no fact answers
    r = data(call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key))
    a = call_tool(
        client, "adjust", {"plan_id": r["plan_id"], "monthly_budget_usd": 50, "idempotency_key": "j1"}, key=key
    )
    assert a["isError"] and "$100" in tool_text(a)
    assert conn.execute("SELECT count(*) AS c FROM plan").fetchone()["c"] == 1


# Values of the wrong type for adjust's fields, as an assistant may put them inside the public tools' `changes`
NUMBER_FIELDS = ("monthly_budget_usd", "radius_miles", "channel_monthly_usd")
TEXT_FIELDS = ("level", "offer", "objective", "channel")
NOT_NUMBERS = ("abc", "1,500", [1500], {"a": 1}, True)
NOT_TEXTS = (["calls"], {"a": 1}, 5)
WRONG_TYPES = [
    *[(field, value, f"{field} must be a number") for field in NUMBER_FIELDS for value in NOT_NUMBERS],
    *[(field, value, f"{field} must be text") for field in TEXT_FIELDS for value in NOT_TEXTS],
]


@pytest.mark.parametrize(("field", "value", "sentence"), WRONG_TYPES)
def test_adjust_prepare_refuses_a_value_of_the_wrong_type_before_anything_else(field, value, sentence):
    """Final review B, Important 6 (A m3): adjust_prepare is where every path meets -- `adjust` (typed by FastMCP),
    `change_plan` (typed by the reader's model) and the public tools, which pass on whatever an assistant put inside
    `changes`. A value of the wrong type is refused in a sentence that names its field, before anything else: no
    connection and no caller is looked at (both are None here). true is no number."""
    with pytest.raises(ToolError) as refused:
        tools_consult.adjust_prepare(None, None, "pl_any", **{field: value})
    assert str(refused.value) == sentence


def test_recommend_before_ready_returns_the_open_question(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r0"}, key=key)
    assert res["isError"] and "listing" in tool_text(res) and "5 question" in tool_text(res)
    _answer(client, key, sid, PICKED, "pick")  # the business on Google Maps first: it centres the ads
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key)
    assert res["isError"] and "objective" in tool_text(res) and "3 question" in tool_text(res)


def test_answer_refuses_unknown_questions_and_takes_a_correction(client, conn, fake):
    """A changed mind before the plan replaces the earlier answer (Muse round 2026-09-28: the owner tapped
    the wrong value option, then typed the right one, and the old refusal would have kept the wrong one)."""
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    res = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"colour": {"choice": "blue"}}, "idempotency_key": "x1"},
        key=key,
    )
    assert res["isError"] and "objective" in tool_text(res)
    _answer(client, key, sid, PICKED, "x1b")  # the business on Google Maps first: it centres the ads
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "x2")
    fixed = _answer(client, key, sid, {"objective": {"choice": "calls"}}, "x3")
    assert fixed["data"]["corrected"] == ["objective"] and fixed["say"].startswith("Updated: Phone calls.")
    assert fixed["next"]["question"]["id"] == "service"  # the correction does not re-ask anything
    assert consult.get(conn, b["data"]["business_id"], sid)["answers"]["objective"]["choice"] == "calls"


def test_a_ready_consultation_still_takes_a_correction_and_a_planned_one_points_at_adjust(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, PICKED, "c0")  # the business on Google Maps first: it centres the ads
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "c1")
    _answer(client, key, sid, {"service": {"choice": "svc_invisalign"}}, "c2")
    done = _answer(client, key, sid, {"customer_value": {"choice": "estimate"}}, "c3")
    assert done["data"]["ready"] is True
    fixed = _answer(client, key, sid, {"customer_value": {"other": "around three thousand"}}, "c4")
    assert fixed["data"]["corrected"] == ["customer_value"] and fixed["data"]["ready"] is True
    assert fixed["say"].startswith("Updated: $1,000 and up.")  # the band the words map to; the amount is kept
    assert consult.get(conn, b["data"]["business_id"], sid)["answers"]["customer_value"]["amount_usd"] == 3000.0
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"]["cards"]["economics"]["assumptions"][3]["basis"] == "your answer"
    late = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"customer_value": {"choice": "1000_plus"}}, "idempotency_key": "c5"},
        key=key,
    )
    assert late["isError"] and "planned; change the plan instead" in tool_text(late)


def test_unreadable_free_text_is_asked_again_once_then_kept_as_a_note(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, PICKED, "u0")  # the business on Google Maps first: it centres the ads
    first = _answer(client, key, sid, {"objective": {"other": "zzz"}}, "u1")
    assert first["data"]["recorded"] == {} and first["next"]["question"]["id"] == "objective" and "?" in first["say"]
    second = _answer(client, key, sid, {"objective": {"other": "zzz"}}, "u2")
    assert second["data"]["recorded"]["objective"] == {"note": "zzz", "raw": "zzz", "source": "other"}
    assert second["next"]["question"]["id"] == "service"


def test_answer_replays_and_refuses_reuse(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    one = _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "k1")
    again = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "k1"},
        key=key,
    )
    assert again["structuredContent"] == one
    other = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"service": {"choice": "svc_invisalign"}}, "idempotency_key": "k1"},
        key=key,
    )
    assert other["isError"] and "idempotency_key already used" in tool_text(other)


def test_answer_bounds_and_scope(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    too_many = {f"q{i}": {"choice": "x"} for i in range(11)}
    res = call_tool(client, "answer", {"session_id": sid, "answers": too_many, "idempotency_key": "m1"}, key=key)
    assert res["isError"] and "10" in tool_text(res)
    res = call_tool(
        client,
        "answer",
        {"session_id": "cs_nope", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "m2"},
        key=key,
    )
    assert res["isError"] and "no consultation" in tool_text(res)


def test_answer_on_an_expired_session_is_refused(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    conn.execute(
        "UPDATE consult_session SET expires_at = %s WHERE id = %s",
        (datetime.now(timezone.utc) - timedelta(hours=1), sid),
    )
    res = call_tool(
        client,
        "answer",
        {"session_id": sid, "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "e1"},
        key=key,
    )
    assert res["isError"] and "this consultation expired; begin a new one" in tool_text(res)


def test_answer_refuses_malformed_choice_and_other_shapes(client, conn, fake):
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    bad = [
        {"objective": {"choice": ["bookings"]}},  # a list on a single-choice question
        {"objective": {"choice": {"x": 1}}},  # not a string or a list at all
        {"objective": {"other": 5}},  # "other" must be text
    ]
    for i, answers in enumerate(bad):
        res = call_tool(
            client, "answer", {"session_id": sid, "answers": answers, "idempotency_key": f"bad{i}"}, key=key
        )
        assert res["isError"] and "objective" in tool_text(res)
    assert (
        conn.execute(
            "SELECT count(*) AS c FROM agent_action WHERE NOT ok AND result->>'error' = 'internal error'"
        ).fetchone()["c"]
        == 0
    )
    # the session is untouched: none of the three malformed answers were recorded
    assert consult.get(conn, b["data"]["business_id"], sid)["answers"].get("objective") is None


def test_whoami_surfaces_a_ready_session_until_it_is_planned(client, conn, fake):
    benchmark.seed(conn)
    b = _start(
        client,
        conn,
        "dentist in Palo Alto",
        known_facts={"objective": "bookings", "service": "Invisalign", "customer_value_usd": 3000, "radius_miles": 5},
    )
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, PICKED, "pick")  # the business on Google Maps: the one question no fact answers
    assert data(call_tool(client, "whoami", {}, key=key))["open_session_id"] == sid  # ready, not yet planned
    call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key)
    assert data(call_tool(client, "whoami", {}, key=key))["open_session_id"] is None


# ---------------------------------------------------------------- the lead services join the profile (walk fixes 1)


def test_a_fourth_pick_through_answer_is_a_clarification_and_records_nothing(client, conn, fake):
    """Four picks for the "lead with" question: the answer says to pick up to three and the same question waits. No
    answer is recorded, no retry is counted (a choice is not words the model could not read), no service is added."""
    first = _start(client, conn)
    key, sid = first["key"], first["data"]["session_id"]
    _answer(client, key, sid, PICKED, "four-0")  # the business on Google Maps first: it centres the ads
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "four-1")
    _answer(client, key, sid, {"service": {"other": "zzz"}}, "four-2")  # words not understood: one retry counted
    retried = consult.get(conn, "biz_1", sid)["answers"][consult.RETRY_KEY]
    version = business.latest_knowledge(conn, "biz_1")[0]
    four = ["svc_invisalign", "svc_cleanings", "svc_checkups", "svc_implants"]
    turn = _answer(client, key, sid, {"service": {"choice": four}}, "four-3")
    assert consult.TOO_MANY_PICKS in turn["say"]
    assert turn["next"]["kind"] == "question" and turn["next"]["question"]["id"] == "service"
    answers = consult.get(conn, "biz_1", sid)["answers"]
    assert turn["data"]["recorded"] == {} and "service" not in answers
    assert answers[consult.RETRY_KEY] == retried == {"service": 1}
    assert business.latest_knowledge(conn, "biz_1")[0] == version


def test_a_picked_default_service_joins_the_profile_once(client, conn, fake):
    """The walk picked the tree's default options the profile did not hold; the plan then named six services the
    business sells. A picked option not on file becomes a service on file, with the option's own id."""
    first = _start(client, conn)  # Bright Smile Dental: Invisalign on file; the tree's defaults shown beside it
    walk = {"session_id": first["data"]["session_id"], "key": first["key"]}
    frozen = conn.execute("SELECT options FROM consult_session WHERE id = %s", (walk["session_id"],)).fetchone()
    shown = {o["id"]: o["label"] for o in frozen["options"]["service"]}
    default = next(oid for oid in shown if oid not in {s.get("id") for s in knowledge_services(conn, "biz_1")})
    res = call_tool(
        client,
        "answer",
        {"session_id": walk["session_id"], "answers": {"service": {"choice": [default]}}, "idempotency_key": "svc-1"},
        key=walk["key"],
    )
    assert not res["isError"], tool_text(res)
    assert f"Added to your services: {shown[default]}." in res_say(res)
    on_file = knowledge_services(conn, "biz_1")
    assert any(s["id"] == default and s["name"] == shown[default] for s in on_file)
    before = len(on_file)
    res = call_tool(
        client,
        "answer",
        {
            "session_id": walk["session_id"],
            "answers": {"service": {"choice": [default, "svc_invisalign"]}},
            "idempotency_key": "svc-2",
        },
        key=walk["key"],
    )
    assert not res["isError"] and "Added to your services" not in res_say(res)
    assert len(knowledge_services(conn, "biz_1")) == before  # a correction adds nothing twice
    business_profile = data(call_tool(client, "business_profile", {}, key=walk["key"]))
    assert {s["id"] for s in business_profile["services"] if s["lead"]} == {default, "svc_invisalign"}


def test_picks_join_the_profile_up_to_its_cap_and_no_further(client, conn, fake):
    """The profile holds at most profile.MAX_SERVICES services: a pick past them is recorded as the answer, and the
    service is neither added nor said to be."""
    first = _start(client, conn)
    key, sid = first["key"], first["data"]["session_id"]
    latest = business.latest_knowledge(conn, "biz_1")[1]
    room = profile.MAX_SERVICES - len(latest["services"]) - 1  # the filler leaves one place
    filler = [{"name": f"Service {i}", "intent_phrases": [], "price_hint": None} for i in range(room)]
    business.add_knowledge(conn, "biz_1", {**latest, "services": [*latest["services"], *filler]}, source="owner")
    one_place = _answer(client, key, sid, {"service": {"choice": ["svc_checkups", "svc_implants"]}}, "cap-1")
    assert "Added to your services: General check-ups." in one_place["say"]  # Implants did not fit
    on_file = knowledge_services(conn, "biz_1")
    assert len(on_file) == profile.MAX_SERVICES and "svc_implants" not in {s["id"] for s in on_file}
    version = business.latest_knowledge(conn, "biz_1")[0]
    full = _answer(client, key, sid, {"service": {"choice": ["svc_implants"]}}, "cap-2")
    assert "Added to your services" not in full["say"]
    assert consult.get(conn, "biz_1", sid)["answers"]["service"]["choice"] == ["svc_implants"]  # the pick stands
    assert business.latest_knowledge(conn, "biz_1")[0] == version  # and no version is written


def test_the_same_pick_twice_joins_the_profile_once(client, conn, fake):
    first = _start(client, conn)
    turn = _answer(
        client, first["key"], first["data"]["session_id"], {"service": {"choice": ["svc_kids", "svc_kids"]}}, "twice"
    )
    assert "Added to your services: Kids dentistry." in turn["say"]
    assert [s["id"] for s in knowledge_services(conn, "biz_1")].count("svc_kids") == 1


def test_a_placeholder_pick_is_not_a_service(client, conn, fake):
    """The fallback tree's options ("My main service", "A special offer") stand for a service and name none: picking
    one records the answer and adds nothing to the profile."""
    fake.knowledge.category = "fallback"  # no tree of its own: the fallback tree's placeholders beside its services
    first = _start(client, conn)
    sid = first["data"]["session_id"]
    turn = _answer(client, first["key"], sid, {"service": {"choice": ["svc_main", "svc_offer"]}}, "placeholder")
    assert "Added to your services" not in turn["say"]
    assert consult.get(conn, "biz_1", sid)["answers"]["service"]["choice"] == ["svc_main", "svc_offer"]
    assert [s["id"] for s in knowledge_services(conn, "biz_1")] == ["svc_invisalign", "svc_cleanings"]


# ---------------------------------------------------------------- where the ads centre (walk fixes 1, Task 4)


def test_the_town_the_opening_words_named_is_one_tap_away(client, conn, fake, places_fake):
    """The fixture's words name Palo Alto (as the founder's walk named Miami, 2026-10-04): the location question asks
    where in it, and "All of Palo Alto" puts the town itself on the map, with no model call."""
    walk = started(client, conn, fake)
    nxt = consult.next_question(consult.get(conn, "biz_1", walk["session_id"]))
    q = nxt["question"]
    assert q["id"] == "location"
    assert q["prompt"].startswith("You said Palo Alto. Where in Palo Alto is the clinic?")
    assert [o["label"] for o in q["options"]] == ["All of Palo Alto"]
    assert consult.question_sentence(nxt).endswith("Options: All of Palo Alto — or answer in your own words.")
    reads = len(fake.read_calls)
    res = call_tool(
        client,
        "answer",
        {"session_id": walk["session_id"], "answers": {"location": {"choice": "stated_place"}}, "idempotency_key": "t"},
        key=walk["key"],
    )
    assert not res["isError"], tool_text(res)
    assert places_fake.calls == ["Palo Alto"] and len(fake.read_calls) == reads  # the town is what the map is asked
    assert "Centred on 123 Main St, Houston, TX 77002." in res_say(res)
    answered = consult.get(conn, "biz_1", walk["session_id"])["answers"]["location"]
    assert (answered["source"], answered["address"]) == ("choice", "123 Main St, Houston, TX 77002")


def test_a_placed_answer_writes_the_listing_and_centres_the_radius(client, conn, fake, places_fake):
    walk = started(client, conn, fake)  # the fixture business has no listing: the location question is asked
    fake.reads["123 Main St, Houston"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note="123 Main St, Houston", understood=True, services=[]
    )
    res = call_tool(
        client,
        "answer",
        {
            "session_id": walk["session_id"],
            "answers": {"location": {"other": "123 Main St, Houston"}},
            "idempotency_key": "loc-1",
        },
        key=walk["key"],
    )
    assert not res["isError"], tool_text(res)
    assert "Centred on 123 Main St, Houston, TX 77002." in res_say(res)
    assert places_fake.calls == ["123 Main St, Houston"]  # the reader's note is what the map is asked
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["listing"] == {
        "place_id": "ChIJ2",
        "name": "123 Main St",
        "address": "123 Main St, Houston, TX 77002",
        "lat": 29.76,
        "lng": -95.37,
        "source": "Google Maps",
        "kind": "address",
    }
    assert "Houston" in " ".join(k.get("service_area") or [])
    profile = data(call_tool(client, "business_profile", {"include": ["locations"]}, key=walk["key"]))
    assert profile["locations"][0]["address"] == "123 Main St, Houston, TX 77002" and profile["locations"][0][
        "center"
    ] == {"lat": 29.76, "lng": -95.37}
    nxt = res_next(res)
    assert (
        nxt["question"]["id"] == "radius"
        and nxt["question"]["prompt"] == "How far around 123 Main St, Houston, TX 77002 should the ads reach?"
    )


def test_the_owner_reads_a_true_sentence_for_each_way_the_place_is_left():
    """Walk fixes 1, final review B M1: no claim that the ads need the address (a launch with no map point targets the
    areas on file), and one sentence for "later", another for the place the map could not find twice."""
    assert tools_consult.PLACE_LATER == (
        "No problem; tell me the address whenever you like, and the ads will centre on it."
    )
    assert tools_consult.PLACE_NOT_FOUND == (
        "I could not place that on the map, so I will go on without it; tell me the address again whenever you like, "
        "and the ads will centre on it."
    )


def test_a_place_the_lookup_cannot_find_is_asked_once_more_then_skipped(client, conn, fake, places_none):
    walk = started(client, conn, fake)
    fake.reads["near the mall"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note="near the mall", understood=True, services=[]
    )
    before = business.latest_knowledge(conn, "biz_1")[0]
    first = call_tool(
        client,
        "answer",
        {
            "session_id": walk["session_id"],
            "answers": {"location": {"other": "near the mall"}},
            "idempotency_key": "loc-a",
        },
        key=walk["key"],
    )
    assert not first["isError"] and geocode.NOT_PLACED in res_say(first)
    assert res_next(first)["question"]["id"] == "location"  # still waits
    assert business.latest_knowledge(conn, "biz_1")[0] == before  # nothing written
    second = call_tool(
        client,
        "answer",
        {
            "session_id": walk["session_id"],
            "answers": {"location": {"other": "near the mall"}},
            "idempotency_key": "loc-b",
        },
        key=walk["key"],
    )
    assert not second["isError"] and tools_consult.PLACE_NOT_FOUND in res_say(second)
    assert tools_consult.PLACE_LATER not in res_say(second)  # the owner gave a place: not "no problem" (B M1)
    stored = conn.execute(
        "SELECT answers->'location' AS a FROM consult_session WHERE id = %s", (walk["session_id"],)
    ).fetchone()["a"]
    assert stored["skipped"] is True and stored["raw"] == "near the mall"
    assert res_next(second)["question"]["id"] == "radius"
    assert business.latest_knowledge(conn, "biz_1")[0] == before


def test_later_skips_the_location_question(client, conn, fake, places_fake):
    walk = started(client, conn, fake)
    fake.reads["I'll tell you later"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=False, services=[]
    )
    res = call_tool(
        client,
        "answer",
        {
            "session_id": walk["session_id"],
            "answers": {"location": {"other": "I'll tell you later"}},
            "idempotency_key": "loc-later",
        },
        key=walk["key"],
    )
    assert not res["isError"] and tools_consult.PLACE_LATER in res_say(res)
    assert tools_consult.PLACE_NOT_FOUND not in res_say(res)
    assert places_fake.calls == []  # no lookup for words that name no place
    assert res_next(res)["question"]["id"] == "radius"
    assert res_next(res)["question"]["prompt"] == "How far around the clinic should the ads reach?"


def test_an_understood_place_answer_with_no_place_named_is_asked_again(client, conn, fake, places_fake):
    """Walk fixes 1, final review A3: the reader understood the words as an answer but named no place (no `note`) --
    not a skip: the question is asked again in its own words (PLACE_BY_CHOICE), nothing stored and nothing looked up.
    Asked once more already, the owner's own words go to the map (as `parse` keeps words on a second miss)."""
    walk = started(client, conn, fake)
    words = "123 Main St, Houston TX 77002"
    fake.reads[words] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=True, services=[]
    )
    first = _answer(client, walk["key"], walk["session_id"], {"location": {"other": words}}, "loc-u1")
    assert consult.PLACE_BY_CHOICE in first["say"] and first["next"]["question"]["id"] == "location"
    assert "location" not in consult.get(conn, "biz_1", walk["session_id"])["answers"]
    assert places_fake.calls == []
    second = _answer(client, walk["key"], walk["session_id"], {"location": {"other": words}}, "loc-u2")
    assert places_fake.calls == [words] and "Centred on 123 Main St, Houston, TX 77002." in second["say"]


def test_a_business_with_a_listing_is_never_asked_where_it_is(client, conn, fake, places_fake):
    # start's lookup found the business, a point on the map, and the owner picked it first (plan
    # 2026-10-03-agent-listing-confirm): the opening counts where the ads centre, which the pick then answers
    walk = started(client, conn, fake, listing=True)
    assert "six quick questions" in walk["say"]
    pending = conn.execute("SELECT answers FROM consult_session WHERE id = %s", (walk["session_id"],)).fetchone()[
        "answers"
    ]
    assert pending["location"]["source"] == "known_fact" and pending["location"]["lat"] is not None
    q = next(q for q in consult.load_tree("dentists")["questions"] if q["id"] == "radius")
    session = consult.get(conn, "biz_1", walk["session_id"])
    assert consult.next_question(session)["question"]["id"] == "radius"  # never asked where: the radius is next
    assert consult.prompt_for(q, session) == f"How far around {pending['location']['address']} should the ads reach?"


def test_without_a_places_key_the_location_question_is_not_asked(client, conn, fake, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    walk = started(client, conn, fake)
    assert "four quick questions" in walk["say"]
    stored = conn.execute(
        "SELECT answers->'location' AS a FROM consult_session WHERE id = %s", (walk["session_id"],)
    ).fetchone()["a"]
    assert stored == {"skipped": True, "source": "known_fact"}


def test_without_a_places_key_an_address_answer_is_kept_and_the_owner_not_blamed(client, conn, fake, monkeypatch):
    """Walk fixes 1, final review A4: a server with no Places key skips the question at start, but an assistant may
    still send an address, corrected or not. It is stored as skipped with the owner's words kept -- nothing looked up,
    no NOT_PLACED ("Try a street address…") and no retry counted, every time -- and the owner reads that Admiry cannot
    place addresses on the map yet: what the server cannot do is not the owner's words' fault."""
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    walk = started(client, conn, fake)
    words = "123 Main St, Houston"
    fake.reads[words] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=words, understood=True, services=[]
    )
    monkeypatch.setattr(places, "search_text", lambda query, key: pytest.fail("no lookup without a key"))
    for ik in ("nokey-1", "nokey-2"):  # the second a correction of the first
        turn = _answer(client, walk["key"], walk["session_id"], {"location": {"other": words}}, ik)
        assert geocode.NOT_PLACED not in turn["say"] and f"{geocode.NO_MAP}." in turn["say"], turn["say"]
        assert tools_consult.PLACE_NOT_FOUND not in turn["say"] and tools_consult.PLACE_LATER not in turn["say"]
    answers = consult.get(conn, "biz_1", walk["session_id"])["answers"]
    assert answers["location"] == {"skipped": True, "raw": words, "note": words, "source": "other"}
    assert "location" not in (answers.get(consult.RETRY_KEY) or {})


def test_a_business_without_a_listing_is_asked_where_the_ads_centre(client, conn, fake, places_fake):
    """The fifth question, said with no options when the words named no town: an assistant that shows only `say` still
    asks it."""
    fake.knowledge.service_area = []
    walk = started(client, conn, fake)
    assert "five quick questions" in walk["say"]
    # a changed mind about the first answer: the question still waiting is where the ads centre
    turn = _answer(client, walk["key"], walk["session_id"], {"objective": {"choice": "calls"}}, "loc-ask")
    assert turn["next"]["question"]["id"] == "location"
    q = turn["next"]["question"]
    assert (q["input"], q["options"], q["index"], q["count"]) == ("address", [], 4, 5)
    assert turn["say"].endswith(f"{q['prompt']} Answer in your own words.")


def test_a_plan_with_no_name_vouched_for_centres_its_sentence_on_the_placed_address(client, conn, fake, places_fake):
    """No website and no business listing vouch for the name the model read from the owner's words: the plan's goal
    sentence centres on the address the owner placed, not on "your business" -- on recommend's card and get_plan's.
    The owner says there is no website (plan 2026-10-05-agent-a3p-5-1), so the name and the services are asked: the
    name skipped -- the owner's own answer to it would be vouched for (A3p-5 batch 2 Task 0, rulings R0-k and R0-l) --
    and the services answered in the owner's words."""
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})
    no_site = {
        **BEFORE_LOCATION,
        "website": {"choice": "none"},
        "business_name": {"other": "I'd rather not say"},
        "services_offered": {"other": "Invisalign"},
    }
    fake.reads["Invisalign"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=True, services=["Invisalign"]
    )
    walk = started(client, conn, fake, before=no_site, website_url=None)
    fake.reads["123 Main St, Houston"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note="123 Main St, Houston", understood=True, services=[]
    )
    key, sid = walk["key"], walk["session_id"]
    _answer(client, key, sid, {"location": {"other": "123 Main St, Houston"}}, "loc-1")
    assert _answer(client, key, sid, {"radius": {"choice": "r5"}}, "loc-2")["data"]["ready"] is True
    built = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "loc-r"}, key=key)
    assert not built["isError"], tool_text(built)
    goal = "More Invisalign bookings within 5 miles of 123 Main St, Houston, TX 77002"
    assert data(built)["cards"]["plan"]["goal"] == goal and res_say(built).startswith(f"Here is the plan: {goal}.")
    assert data(call_tool(client, "get_plan", {}, key=key))["cards"]["plan"]["goal"] == goal
    # the cards the model reads a question and a change against say so too
    assert not call_tool(client, "ask", {"question": "Why Google Search first?"}, key=key)["isError"]
    assert fake.answer_calls[-1]["material"]["cards"]["plan"]["goal"] == goal
    words = {"plan_id": data(built)["plan_id"], "words": "Lead with the free consultation", "idempotency_key": "loc-c"}
    assert not call_tool(client, "change_plan", words, key=key)["isError"]
    assert fake.adjust_calls[-1]["cards"]["plan"]["goal"] == goal


def test_a_placed_answer_moves_a_business_listing_without_a_point_and_keeps_its_name(client, conn, fake, monkeypatch):
    """Round 1: the consultation adopts a place as update_business does (profile.apply). A business listing Google Maps
    gives no point -- the owner picked it as the business (plan 2026-10-03-agent-listing-confirm) -- centres nothing,
    so the question is asked; the answer moves that listing to the place -- its name kept, and vouched for: the owner
    confirmed it -- and the place's town, read from the address as Google Maps formats it, joins the areas (never its
    state and ZIP code)."""
    legacy = places.Candidate("ChIJ1", "Bright Smile Dental", "500 University Ave, Palo Alto, CA")  # no point
    houston = places.Candidate("ChIJ2", "123 Main St", "123 Main St, Houston, TX 77002, USA", 29.76, -95.37)
    walk = started(client, conn, fake, listing=legacy)  # offered (its name, in Palo Alto) and picked
    monkeypatch.setattr(places, "search_text", lambda query, key: houston)
    fake.reads["123 Main St, Houston"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note="123 Main St, Houston", understood=True, services=[]
    )
    turn = _answer(client, walk["key"], walk["session_id"], {"location": {"other": "123 Main St, Houston"}}, "mv-1")
    # said without its country, in "Centred on …" and in the radius question alike (final review B M2)
    assert "Centred on 123 Main St, Houston, TX 77002." in turn["say"] and "USA" not in turn["say"]
    assert turn["next"]["question"]["prompt"] == "How far around 123 Main St, Houston, TX 77002 should the ads reach?"
    k = business.latest_knowledge(conn, "biz_1")[1]
    moved = {**legacy.as_json(), "address": houston.address, "lat": 29.76, "lng": -95.37, "placed": True}
    # marked: a later reading keeps the place (final review A1)
    assert k["listing"] == {**moved, "confirmed": True, "confirmed_at": k["listing"]["confirmed_at"]}
    assert plans.name_for({**k, "website": None}) == "Bright Smile Dental"  # the listing the owner confirmed
    assert k["service_area"] == ["Palo Alto", "Houston"]


def test_with_no_lookups_left_the_location_question_stays_open_and_no_retry_is_counted(
    client, conn, fake, places_fake, monkeypatch
):
    """Round 1: no lookups left today is not a place the map could not find. The answer says so, counts no retry and
    keeps the question open -- asked again, it says the same, never skipped -- and nothing is looked up. Round 2: it
    says the way on, too (geocode.NO_LOOKUPS_LATER: say later, or try again tomorrow)."""
    walk = started(client, conn, fake)
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "1")  # start's own lookup was the day's one Places call
    fake.reads["123 Main St, Houston"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note="123 Main St, Houston", understood=True, services=[]
    )
    for ik in ("spent-1", "spent-2"):
        turn = _answer(client, walk["key"], walk["session_id"], {"location": {"other": "123 Main St, Houston"}}, ik)
        assert geocode.NO_LOOKUPS_LATER in turn["say"], turn["say"]
        assert geocode.NO_LOOKUPS not in turn["say"] and geocode.NOT_PLACED not in turn["say"], turn["say"]
        assert turn["next"]["question"]["id"] == "location"
    answers = consult.get(conn, "biz_1", walk["session_id"])["answers"]
    assert "location" not in answers and "location" not in (answers.get(consult.RETRY_KEY) or {})
    assert places_fake.calls == []


def test_the_plans_draft_prompt_shows_a_placed_address_as_its_address_only(client, conn, monkeypatch):
    """Round 2: the prompt `recommend` sends the model, for a business whose only listing is an address the owner
    placed, carries the address and not the listing's name (here a shop's at that address) or its place id."""
    from agent.tests.test_llm import PLACED_LISTING, SEEN_ADDRESS, _prompt, completion
    from agent.tests.test_llm import generator as model

    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    knowledge = {**knowledge_fixture().model_dump(), "website": None, "listing": PLACED_LISTING}
    business.add_knowledge(conn, "biz_1", knowledge, source="owner")
    business.set_goal(conn, "biz_1", monthly_limit_usd=2000)
    tree = consult.load_tree("dentists")
    placed = {"address": PLACED_LISTING["address"], "lat": 29.76, "lng": -95.37, "source": "known_fact"}
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": "svc_invisalign", "raw": "", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "amount_usd": 3000.0, "raw": "", "source": "choice"},
            "location": placed,
            "radius": {"miles": 5.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    sent: list = []
    gen = model(completion(plan_draft_fixture().model_dump_json()), sent=sent)
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    res = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    prompt = _prompt(sent[0])
    assert SEEN_ADDRESS in prompt and "Joe's Coffee" not in prompt and "ChIJ2" not in prompt


# ---------------------------------------------------------------- channels (spec 2026-09-29 §6.2, §7.1; M0 Task 5)


def _home_plan(
    client, conn, *, service: str, limit: float = 1000, prepaid: bool = False, whole: bool = False
) -> tuple[str, dict]:
    """A claimed home-services business recommended from a ready consultation: (key, recommend's data); `prepaid`:
    the business prepays, the only way NewsBreak (run from a balance) can be in its plan in M0; `whole`: recommend's
    whole envelope (`data`, `say`, `next`) in place of its data."""
    key = business_with_key(conn, claim_state="claimed")
    if prepaid:
        conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    knowledge = home_services_knowledge("Emergency plumbing" if service == "svc_emergency" else "Roofing")
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    business.set_goal(conn, "biz_1", monthly_limit_usd=limit)
    tree = consult.load_tree("home_services")
    s = consult.create(
        conn,
        "biz_1",
        tree="home_services",
        locale="en-US",
        opening_text="home services",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": [service], "raw": "", "source": "choice"},
            "customer_value": {"choice": "300_1000", "amount_usd": 600.0, "raw": "", "source": "choice"},
            "location": LATER,
            "radius": {"miles": 10.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    res = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)
    assert not res["isError"], tool_text(res)
    return key, (res["structuredContent"] if whole else data(res))


def _rows(d: dict) -> list[tuple[str, int]]:
    return [(c["platform"], c["monthly_usd"]) for c in d["cards"]["plan"]["channels"]]


def _entries(conn, plan_id: str) -> dict[str, dict]:
    """The plan's stored channel entries (sections.ads.channels), by channel."""
    return {c["channel"]: c for c in plans.by_id(conn, plan_id)["sections"]["ads"]["channels"]}


def test_a_pin_on_a_channel_the_business_cannot_have_is_refused_with_why(client, conn, fake, monkeypatch, pictures):
    """Final review I2: "put $300 on NewsBreak" from a business that pays Google directly is refused -- NewsBreak runs
    only from a balance -- and, with NewsBreak's switch off, every business hears that it is coming to Admiry."""
    benchmark.seed(conn)
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    key, r = _home_plan(client, conn, service="svc_emergency")
    pin = {"plan_id": r["plan_id"], "channel": "newsbreak", "channel_monthly_usd": 300}
    res = call_tool(client, "adjust", {**pin, "idempotency_key": "p1"}, key=key)
    assert res["isError"]
    assert "NewsBreak: not in this plan; Admiry runs it only for businesses that prepay." in tool_text(res)
    monkeypatch.delenv("AGENT_EXEC_NEWSBREAK")
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    res = call_tool(client, "adjust", {**pin, "idempotency_key": "p2"}, key=key)
    assert res["isError"] and "NewsBreak: not in this plan yet; coming to Admiry." in tool_text(res)
    assert _internal_errors(conn) == 0


def test_a_pin_on_newsbreak_without_a_picture_for_the_trade_is_refused_with_why(
    client, conn, fake, newsbreak_on, no_pictures
):
    """M1 Task 7 fix round 1, item 3: NewsBreak shows a picture with every ad, so a plan has it only while Admiry's set
    has one for the trade -- "put $300 on NewsBreak" is refused the way a channel switched off is, in its later
    line's words."""
    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency", prepaid=True)
    pin = {"plan_id": r["plan_id"], "channel": "newsbreak", "channel_monthly_usd": 300, "idempotency_key": "p1"}
    res = call_tool(client, "adjust", pin, key=key)
    assert res["isError"]
    assert "NewsBreak: not in this plan yet; once Admiry has a picture for your trade." in tool_text(res)
    assert _internal_errors(conn) == 0


def test_adjust_pins_one_channel_and_re_splits_the_rest(client, conn, fake, newsbreak_on, pictures):
    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency", prepaid=True)  # a plumber: NewsBreak is not its trade
    assert _rows(r) == [("google_search", 1000)]
    pinned = _adjust(client, key, r["plan_id"], "p1", channel="newsbreak", channel_monthly_usd=300)
    assert _rows(pinned["data"]) == [("google_search", 700), ("newsbreak", 300)]
    nb = pinned["data"]["cards"]["plan"]["channels"][1]
    assert nb["why"].startswith("NewsBreak gets the $300 a month you set: local news readers")
    assert (
        nb["mode_text"] == "Admiry runs it from your balance"
        and pinned["data"]["cards"]["budget"]["monthly_usd"] == 1000
    )
    assert "NewsBreak gets the $300 a month you set" in pinned["say"] and not JARGON.search(pinned["say"])
    # the owner's amount stays through other changes; the rest re-splits around it
    wider = _adjust(client, key, pinned["data"]["plan_id"], "p2", radius_miles=20)
    assert _rows(wider["data"]) == [("google_search", 700), ("newsbreak", 300)]
    assert not call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)["isError"]
    more = _adjust(client, key, wider["data"]["plan_id"], "p3", monthly_budget_usd=2000)
    assert _rows(more["data"]) == [("google_search", 1700), ("newsbreak", 300)]
    levels = more["data"]["cards"]["budget"]["levels"]  # every level's split keeps the owner's amount
    assert levels["recommended"] == {"monthly_usd": 2000, "split": {"google_search": 1700, "newsbreak": 300}}
    assert levels["conservative"] == {"monthly_usd": 900, "split": {"google_search": 600, "newsbreak": 300}}
    plan_id = more["data"]["plan_id"]
    for i, (args, words) in enumerate(
        (
            ({"channel": "tiktok", "channel_monthly_usd": 300}, "unknown channel"),
            ({"channel": "meta", "channel_monthly_usd": 300}, "Facebook & Instagram is not in Admiry yet"),
            ({"channel": "newsbreak", "channel_monthly_usd": 200}, "NewsBreak needs at least $300 a month"),
            ({"channel": "newsbreak"}, "channel and channel_monthly_usd together"),
            ({"channel_monthly_usd": 300}, "channel and channel_monthly_usd together"),
            ({"channel": "newsbreak", "channel_monthly_usd": 5000}, "more than the plan's $2,000"),
        )
    ):
        res = call_tool(client, "adjust", {"plan_id": plan_id, "idempotency_key": f"x{i}", **args}, key=key)
        assert res["isError"] and words in tool_text(res), (args, tool_text(res))
    assert _internal_errors(conn) == 0


def test_a_newsbreak_pin_leaves_the_minimum_level_and_low_amounts_usable(client, conn, fake, newsbreak_on, pictures):
    """Fix round 1, IMPORTANT 1: NewsBreak pinned at $300, then the $300 minimum level: Google Search's $100 floor no
    longer fits, NewsBreak alone takes its $300 -- no published benchmark, so no leads estimated, and no error; at $350
    the $50 nobody can take is said."""
    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency", prepaid=True)
    pinned = _adjust(client, key, r["plan_id"], "p1", channel="newsbreak", channel_monthly_usd=300)["data"]
    low = _adjust(client, key, pinned["plan_id"], "p2", level="minimum")
    d = low["data"]
    assert _rows(d) == [("newsbreak", 300)] and d["cards"]["budget"]["monthly_usd"] == 300
    assert d["cards"]["economics"]["columns"]["planning"]["leads"] == [0, 0]
    assert {"name": "cost per lead, NewsBreak", "value": "not published", "basis": NO_BENCHMARK} in d["cards"][
        "economics"
    ]["assumptions"]
    assert f"No estimate yet: {NO_BENCHMARK}." in low["say"] and not JARGON.search(low["say"])
    # no Google Search in the plan: a place run from the balance says the business prepays, so no own account offered
    assert launch_tools(low) == ["request_payment_link", "adjust"]
    odd = _adjust(client, key, d["plan_id"], "p3", monthly_budget_usd=350)["data"]
    assert _rows(odd) == [("newsbreak", 300)]
    assert "$50 of your budget has nowhere to go; raise a channel's amount or lower the total." in odd["notes"]
    assert _internal_errors(conn) == 0


def test_google_search_keeps_its_own_ads_when_a_level_brings_it_back(client, conn, fake, newsbreak_on, pictures):
    """Final wave re-review: NewsBreak pinned at $300, the $300 minimum level (Google Search dropped), then the
    recommended level. Google Search comes back with its own ad text and no `copy_of` -- /plan shows under its name
    the words its build runs and the chat card previews -- and NewsBreak's section keeps its one line. A mark a version
    stored on Google Search before the fix is ignored by the page and not carried into the next version."""
    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency", prepaid=True)
    words = _entries(conn, r["plan_id"])["google_search"]["headlines"]
    pinned = _adjust(client, key, r["plan_id"], "p1", channel="newsbreak", channel_monthly_usd=300)["data"]
    low = _adjust(client, key, pinned["plan_id"], "p2", level="minimum")["data"]
    assert _rows(low) == [("newsbreak", 300)]
    back = _adjust(client, key, low["plan_id"], "p3", level="recommended")["data"]
    assert _rows(back) == [("google_search", 700), ("newsbreak", 300)]
    ads = _entries(conn, back["plan_id"])
    assert "copy_of" not in ads["google_search"] and ads["google_search"]["headlines"] == words
    assert ads["newsbreak"]["copy_of"] == "google_search"
    preview = back["cards"]["ads_preview"]["headlines"]
    assert preview and set(preview) <= set(words)

    def shown(plan_id: str) -> None:
        html = client.get(f"/plan/{plan_id}").text
        google = html.split("Google Search — $700/mo")[1].split("</section>")[0]
        nb = html.split("NewsBreak — $300/mo")[1].split("</section>")[0]
        assert str(escape(preview[0])) in google and "Its ads are written" not in google
        assert "Its ads are written from this plan when NewsBreak starts." in nb and "<table>" not in nb

    shown(back["plan_id"])
    conn.execute(  # what the code before the fix stored on the re-funded primary
        "UPDATE plan SET json = jsonb_set(json, '{sections,ads,channels,0,copy_of}', to_jsonb(%s::text)) WHERE id = %s",
        ("newsbreak", back["plan_id"]),
    )
    assert _entries(conn, back["plan_id"])["google_search"]["copy_of"] == "newsbreak"
    shown(back["plan_id"])
    wider = _adjust(client, key, back["plan_id"], "p4", radius_miles=20)["data"]
    assert "copy_of" not in _entries(conn, wider["plan_id"])["google_search"]
    assert _internal_errors(conn) == 0


def test_the_benchmark_layers_own_words_never_reach_the_owner(client, conn, fake, monkeypatch):
    # imported here, not at the top: a tool module imported before agent.mcp_app registers its tools after the ones
    # mcp_app's own import pulls in, and test_probe pins that order (this module is imported early by others)
    from agent import tools_consult

    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency")

    def unseeded(conn, category):
        raise benchmark.NotSeeded("benchmark table has no funnel rows; run benchmark.seed()")

    monkeypatch.setattr(benchmark, "funnel", unseeded)
    res = call_tool(client, "adjust", {"plan_id": r["plan_id"], "radius_miles": 20, "idempotency_key": "b1"}, key=key)
    assert res["isError"] and tools_consult.ESTIMATE_FAILED in tool_text(res)
    assert "seed" not in tool_text(res) and "benchmark" not in tool_text(res) and _internal_errors(conn) == 0


def test_a_bug_in_the_plan_math_is_an_internal_error_never_try_again(client, conn, fake, monkeypatch, caplog):
    """Final review I5: only the benchmark layer's "not seeded" (`benchmark.NotSeeded`) reads as "try again"; a bug
    -- here a KeyError from the allocator -- is the tools' internal error, logged with its traceback, never an
    estimate the owner is told to retry."""
    from agent import allocate, tools_consult

    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency")

    def broken(inputs):
        raise KeyError("google_search")

    monkeypatch.setattr(allocate, "initial", broken)
    res = call_tool(client, "adjust", {"plan_id": r["plan_id"], "radius_miles": 20, "idempotency_key": "k1"}, key=key)
    assert res["isError"] and tools_consult.ESTIMATE_FAILED not in tool_text(res)
    assert _internal_errors(conn) == 1


def test_a_business_before_the_benchmark_is_seeded_hears_try_again(client, conn, fake, caplog):
    """recommend with no stated budget reads Google Search's benchmark row for its levels: with the table empty (the
    app seeds it at boot, so this empties it after) there is none (`benchmark.NotSeeded`), and the owner reads the plain
    "try again" while the log keeps the cause."""
    from agent import tools_consult

    conn.execute("DELETE FROM benchmark")
    key = business_with_key(conn, claim_state="claimed")
    knowledge = home_services_knowledge("Emergency plumbing")
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    tree = consult.load_tree("home_services")
    s = consult.create(
        conn,
        "biz_1",
        tree="home_services",
        locale="en-US",
        opening_text="home services",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": ["svc_emergency"], "raw": "", "source": "choice"},
            "customer_value": {"choice": "300_1000", "amount_usd": 600.0, "raw": "", "source": "choice"},
            "location": LATER,
            "radius": {"miles": 10.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    with caplog.at_level(logging.WARNING, logger="agent.tools_consult"):
        res = call_tool(client, "recommend", {"session_id": s["id"], "idempotency_key": "r1"}, key=key)
    assert res["isError"], tool_text(res)
    assert tools_consult.ESTIMATE_FAILED in tool_text(res) and _internal_errors(conn) == 0, tool_text(res)
    failed = [r for r in caplog.records if r.name == "agent.tools_consult"]
    assert failed and failed[0].exc_info is not None  # the log keeps the cause, traceback and all


def test_revise_plan_keeps_the_whole_budget_and_the_owners_channel_amounts(client, conn, fake, newsbreak_on, pictures):
    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_emergency", prepaid=True)
    pinned = _adjust(client, key, r["plan_id"], "p1", channel="newsbreak", channel_monthly_usd=300)
    res = call_tool(
        client,
        "revise_plan",
        {"plan_id": pinned["data"]["plan_id"], "instruction": "focus on leaks", "idempotency_key": "v1"},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    got = data(call_tool(client, "get_plan", {}, key=key))
    assert [(c["channel"], c["monthly_usd"]) for c in got["ads"]["channels"]] == [
        ("google_search", 700.0),
        ("newsbreak", 300.0),
    ]
    assert got["ads"]["named"] == {"newsbreak": 300}


def test_set_goal_channel_caps_hold_the_split(client, conn, fake, newsbreak_on, pictures):
    benchmark.seed(conn)
    key, r = _home_plan(client, conn, service="svc_roofing", prepaid=True)  # a roofer: NewsBreak's test from $800
    assert _rows(r) == [("google_search", 700), ("newsbreak", 300)]
    g = call_tool(
        client,
        "set_goal",
        {"monthly_budget_usd": 1000, "channel_caps": {"newsbreak": 200}, "idempotency_key": "g1"},
        key=key,
    )
    assert not g["isError"], tool_text(g)
    assert data(g)["goal"]["channel_caps"] == {"newsbreak": 200.0}
    after = _adjust(client, key, r["plan_id"], "c1", radius_miles=20)["data"]
    assert _rows(after) == [("google_search", 1000)]
    assert (
        "NewsBreak: your limit of $200 a month is below its $300 minimum, so it is not in this plan." in after["notes"]
    )
    # a later goal that does not mention the caps keeps them; unknown ids and bad amounts are refused
    kept = data(call_tool(client, "set_goal", {"monthly_budget_usd": 1500, "idempotency_key": "g2"}, key=key))
    assert kept["goal"]["channel_caps"] == {"newsbreak": 200.0}
    for i, caps in enumerate(({"tiktok": 100}, {"newsbreak": -1}, {"newsbreak": "lots"})):
        bad = call_tool(
            client, "set_goal", {"monthly_budget_usd": 1000, "channel_caps": caps, "idempotency_key": f"b{i}"}, key=key
        )
        assert bad["isError"], caps
    # a channel pinned above the owner's own cap is refused with the way out
    over = call_tool(
        client,
        "adjust",
        {"plan_id": after["plan_id"], "channel": "newsbreak", "channel_monthly_usd": 300, "idempotency_key": "c2"},
        key=key,
    )
    assert (
        over["isError"] and "limited to $200 a month" in tool_text(over) and "raise that limit first" in tool_text(over)
    )
    cleared = data(
        call_tool(
            client, "set_goal", {"monthly_budget_usd": 1500, "channel_caps": {}, "idempotency_key": "g3"}, key=key
        )
    )
    assert cleared["goal"]["channel_caps"] == {}


# ---------------------------------------------------------------- the ways that exist (walk fixes 1, Task 6)


def test_the_ways_a_plan_runs_are_said_only_for_the_places_it_funds(monkeypatch):
    """Walk fixes 1, final review A5, A6 (carried items 4, 5): read off the plan card's rows, Google Search's way is said
    only for a plan with a Google Search row -- none without one, where the places Admiry runs from the balance say how
    they run (the e-mail's connect reply then reads those, as the plan answer does) -- and the owner's own Google Ads
    account is a way to link only for a plan with Google Search, for a business that does not prepay. Since A3c-1
    (`plans.google_open`) Google Search's sentence names both ways; before it, the own account, or the wait."""
    google = {"platform": "google_search", "label": "Google Search", "mode_text": "your own Google Ads account"}
    prepaid_google = {**google, "mode_text": plans.MANAGED_TEXT}
    newsbreak = {"platform": "newsbreak", "label": "NewsBreak", "mode_text": plans.MANAGED_TEXT}
    assert tools_consult.google_way([newsbreak]) is None
    assert tools_consult.balance_ways([newsbreak]) == ["Admiry runs NewsBreak from your balance."]
    assert tools_consult.google_way([google]) == tools_consult.PREPAY_SAY
    assert tools_consult.google_way([prepaid_google, newsbreak]) == tools_consult.PREPAID_RUNS_SAY
    assert tools_consult.balance_ways([prepaid_google, newsbreak]) == ["Admiry runs NewsBreak from your balance."]
    linked = [tools_consult.links_own_account(rows) for rows in ([google], [prepaid_google], [newsbreak], [])]
    assert linked == [True, False, False, False]
    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1
    assert tools_consult.google_way([google]) == tools_consult.OWN_ACCOUNT_SAY
    assert tools_consult.google_way([prepaid_google, newsbreak]) == tools_consult.PREPAID_WAITS_SAY


def test_a_google_only_plan_leads_with_admiry_running_it_then_the_own_account(client, conn, fake):
    """Founder's ruling, 2026-10-07 (A3c-1 offered the own account alone to a business that does not prepay yet): Admiry
    running the ads leads -- connect_channel with mode managed, whose answer is the setup step where the way is chosen
    (`request_payment_link` refuses a business that does not prepay) -- then its own account, then a change. The
    sentence leads with it too, and never says Admiry-run Google accounts are coming."""
    env = recommended_with(client, conn, fake, objective="calls")
    assert launch_ids(env) == ["prepay", "link_own_account", "adjust"]
    assert launch_tools(env) == ["connect_channel", "connect_channel", "adjust"]
    assert tools_consult.MANAGED_ACTION == {
        "id": "prepay",
        "label": "Admiry runs it, I prepay",
        "tool": "connect_channel",
        "args": {"channel": "google_search", "mode": "managed"},
    }
    assert tools_consult.PREPAY_SAY in env["say"] and "coming" not in env["say"]
    assert tools_consult.PREPAY_SAY.index("Admiry") < tools_consult.PREPAY_SAY.index("your own")


def test_where_both_ways_are_offered_neither_is_marked_first(client, conn, fake, monkeypatch):
    """Addendum 2 (founder 2026-10-13): the home shows both ways equally. Where the plan answer offers the two ways
    together -- Admiry running the ads and the owner's own account -- neither is primary (the home draws a primary one
    dark), their order kept: Admiry's first (the founder's 2026-10-07 ordering). Where only one way is possible
    (Admiry-run Google not open: the own account's link alone) it may lead."""
    env = recommended_with(client, conn, fake, objective="calls")
    both = [a for a in env["next"]["actions"] if a["id"] in ("prepay", "link_own_account")]
    assert [a["id"] for a in both] == ["prepay", "link_own_account"]
    assert not any(a.get("primary") for a in both)
    assert "primary" not in tools_consult.MANAGED_ACTION and "primary" not in tools_consult.OWN_ACCOUNT_ACTION
    monkeypatch.setattr(plans, "google_open", lambda: False)  # one way only: the own account's link leads
    assert tools_consult._launch_actions([{"platform": "google_search", "mode_text": "own"}])["actions"][0] == {
        "id": "link_own_account",
        "label": "I have a Google Ads account",
        "tool": "connect_channel",
        "primary": True,
    }


def test_a_google_only_plan_says_admiry_run_accounts_are_coming_while_google_is_closed(client, conn, fake, monkeypatch):
    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1
    env = recommended_with(client, conn, fake, objective="calls")
    assert launch_tools(env) == ["connect_channel", "adjust"]
    assert tools_consult.OWN_ACCOUNT_SAY in env["say"] and "prepay" not in env["say"]


def test_a_plan_with_a_place_admiry_runs_from_the_balance_offers_prepay(client, conn, fake, newsbreak_on, pictures):
    """A roofer that prepays (`_home_plan(prepaid=True)`): the dental walk never funds NewsBreak, no dentist service
    being its trade. The business prepays, so its own Google Ads account is not offered either."""
    benchmark.seed(conn)
    _, env = _home_plan(client, conn, service="svc_roofing", prepaid=True, whole=True)
    assert _rows(env["data"]) == [("google_search", 700), ("newsbreak", 300)]  # NewsBreak funded beside Google Search
    assert launch_tools(env) == ["request_payment_link", "adjust"]
    assert "Admiry runs NewsBreak from your balance." in env["say"]
    assert tools_consult.PREPAID_RUNS_SAY in env["say"]  # A3c-1: Admiry runs its Google Search from the balance too


def test_a_prepaid_google_only_plan_offers_no_way_that_cannot_run_yet(client, conn, fake, monkeypatch):
    """A business that prepays cannot link its own Google Ads account (google_ads_link.NOT_OWN_ACCOUNT), and, before
    A3c-1, Admiry-run Google accounts were not open: a plan that funds Google Search alone offers a change only, and
    says the wait in the controller's words."""
    monkeypatch.setattr(plans, "google_open", lambda: False)  # before A3c-1
    benchmark.seed(conn)
    _, env = _home_plan(client, conn, service="svc_emergency", prepaid=True, whole=True)  # NewsBreak switched off
    rows = env["data"]["cards"]["plan"]["channels"]
    assert [(r["platform"], r["mode_text"]) for r in rows] == [("google_search", plans.MANAGED_TEXT)]
    assert launch_tools(env) == ["adjust"]
    assert tools_consult.PREPAID_WAITS_SAY in env["say"]
    assert tools_consult.OWN_ACCOUNT_SAY not in env["say"] and tools_consult.PREPAY_SAY not in env["say"]


def test_a_prepaid_business_is_offered_its_balance_once_admiry_runs_google_search(client, conn, fake):
    assert plans.google_open()  # A3c-1: Google's adapter declares "managed"
    benchmark.seed(conn)
    _, env = _home_plan(client, conn, service="svc_emergency", prepaid=True, whole=True)
    assert launch_tools(env) == ["request_payment_link", "adjust"]
    assert tools_consult.PREPAID_RUNS_SAY in env["say"] and "link yours" not in env["say"]


def test_the_goal_counts_what_the_owner_asked_for_when_they_stated_no_type(client, conn, fake):
    recommended_with(client, conn, fake, objective="calls")
    assert business.latest_goal(conn, "biz_1")["goal_type"] == "calls"
    recommended_with(client, conn, fake, objective="enquiries", business="biz_2")
    assert business.latest_goal(conn, "biz_2")["goal_type"] == "customers"


def test_a_stated_goal_type_is_kept_whatever_the_objective(client, conn, fake):
    recommended_with(client, conn, fake, objective="calls", known_facts={"goal_type": "bookings"})
    assert business.latest_goal(conn, "biz_1")["goal_type"] == "bookings"


def test_a_goal_type_stated_as_customers_is_kept_when_the_objective_is_calls(client, conn, fake):
    """The guard on a stated type (create_marketing_plan's goal.type, start's known fact): even the default type,
    customers, once the owner stated it, is never rewritten from the objective."""
    recommended_with(client, conn, fake, objective="calls", known_facts={"goal_type": "customers"})
    assert business.latest_goal(conn, "biz_1")["goal_type"] == "customers"


def test_a_goal_the_owner_changes_while_the_plan_is_built_is_kept(client, conn, fake, monkeypatch):
    """The model's draft takes about 30 seconds, and the owner may change the goal meanwhile from another surface:
    recommend writes the goal from a fresh read, so the owner's target, limit and focus survive the type it writes, and
    no ceiling of Admiry's replaces the limit they set. (No limit at start: the type and the ceiling were both due.)"""
    fake.knowledge.goal_hint.monthly_budget_usd = None  # a goal of 20 customers a month, no limit
    real = tools_consult.build

    def build_then_the_owner_changes_the_goal(*args, **kwargs):
        built = real(*args, **kwargs)  # the goal it carries is read here; the owner's change lands after it
        business.set_goal(conn, "biz_1", target=35, monthly_limit_usd=2500, focus_services=["Implants"])
        return built

    monkeypatch.setattr(tools_consult, "build", build_then_the_owner_changes_the_goal)
    recommended_with(client, conn, fake, objective="calls")
    goal = business.latest_goal(conn, "biz_1")
    kept = (goal["goal_type"], goal["target"], goal["monthly_limit_usd"], goal["focus_services"], goal["limit_source"])
    assert kept == ("calls", 35, 2500.0, ["Implants"], "owner")


def test_a_goal_type_the_owner_set_is_kept_whatever_the_objective(client, conn, fake):
    """Only the default type, customers, follows the objective: a goal the owner set to count calls (change_goal) before
    the consultation still counts calls after the consultation answered bookings."""
    key = business_with_key(conn, claim_state="claimed")
    calls = call_tool(client, "change_goal", {"changes": {"goal_type": "calls"}, "idempotency_key": "cg-1"}, key=key)
    assert not calls["isError"], tool_text(calls)
    recommended_with(client, conn, fake, objective="bookings")
    assert business.latest_goal(conn, "biz_1")["goal_type"] == "calls"


def test_a_goal_first_written_with_the_plan_counts_what_the_owner_asked_for(client, conn, fake):
    """Nothing about the goal stated at start (no target, no budget, no type), so no goal before the plan: the one
    recommend writes, the "system" ceiling, counts the objective."""
    fake.knowledge.goal_hint.target_customers_per_month = None
    fake.knowledge.goal_hint.monthly_budget_usd = None
    walk = answered_consultation(client, conn, fake, objective="calls")
    assert business.latest_goal(conn, walk["business_id"]) is None
    res = call_tool(
        client, "recommend", {"session_id": walk["session_id"], "idempotency_key": "rec-goal"}, key=walk["key"]
    )
    assert not res["isError"], tool_text(res)
    goal = business.latest_goal(conn, walk["business_id"])
    assert (goal["goal_type"], goal["limit_source"]) == ("calls", "system")


# ---------------------------------------------------------------- the website before the quote (A3p-5 batch 1 Task 2)

FIRST_WORDS = "Family dentist in Palo Alto, we want more Invisalign patients"  # no website in them
SITE = "https://brightsmile.example"


@pytest.fixture
def crawled(monkeypatch):
    """The crawler faked: every site reads as the dental fixture's page; `.urls` records what was fetched, `.fail` (a
    crawl.CrawlError) makes every fetch fail, as an address the crawl refuses does, and `.empty` makes every fetch read
    no page, as a redirect to another host, a timeout or nothing but non-HTML do under the crawl's own rules."""
    box = SimpleNamespace(urls=[], fail=None, empty=False)

    def fetch(url):
        box.urls.append(url)
        if box.fail is not None:
            raise box.fail
        return [] if box.empty else [crawl.Page(url=url, title="Site", text="We do Invisalign")]

    monkeypatch.setattr(tools_start.crawl, "fetch_site", fetch)
    return box


def _homepage(conn, fake, found=()) -> dict:
    """The homepage's first turn (begin_anonymous) on words that name no website: the reading saw no site and named no
    service, and start's lookup finds `found`. Returns {"key", "business_id", "session_id", "say", "next"}."""
    fake.knowledge = fake.knowledge.model_copy(update={"website": None, "services": []})
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(places, "search", lambda query, key, **kw: list(found))
        res = tools_start.begin_anonymous(FIRST_WORDS, ip="198.51.100.9")
    d = res["data"]
    return {
        "key": d["draft_key"],
        "business_id": d["business_id"],
        "session_id": d["session_id"],
        "say": res["say"],
        "next": res["next"],
    }


def _position(turn: dict) -> tuple[str, int, int]:
    q = turn["next"]["question"]
    return q["id"], q["index"], q["count"]


def test_a_session_with_no_site_asks_for_the_website_first(client, conn, fake):
    """W1: words that name no website and a reading that saw none -- the first question is the website, its one option
    "No website" and its placeholder; the name and the services count from the first turn (Q3, ruling P2), and so
    does the listing question while the name may still find the business on Google Maps (ruling F1)."""
    first = _homepage(conn, fake)
    q = first["next"]["question"]
    assert (q["id"], q["input"], q["index"], q["count"]) == ("website", "website", 1, 9)
    assert q["prompt"] == consult.WEBSITE_PROMPT and q["options"] == [{"id": "none", "label": consult.NO_WEBSITE}]
    assert q["other"] == {"allowed": True, "placeholder": "Paste your website's address"}
    assert "Before I plan anything, a few quick questions." in first["say"]  # nine (ruling R0-h)
    assert first["say"].endswith(consult.question_sentence(first["next"]))
    assert not JARGON.search(first["say"])


def test_an_explicit_website_read_at_start_pre_answers_the_website_question(client, conn, fake):
    """Q4 (ruling P1): the site start read is the website question's pre-answer, so it is never asked or counted."""
    body = _start(client, conn)  # website_url https://brightsmile.example
    s = consult.get(conn, "biz_1", body["data"]["session_id"])
    assert s["known_facts"]["website"] == {"url": SITE, "read": True}
    assert s["answers"]["website"] == {"url": SITE, "read": True, "source": "known_fact"}
    assert body["next"]["question"]["id"] == "listing" and consult.site_read(s)


@pytest.mark.parametrize("found", [False, True], ids=["nothing on Google Maps", "the business on Google Maps"])
def test_a_site_answered_is_read_and_the_session_starts_again_in_place(client, conn, fake, crawled, monkeypatch, found):
    """W3/Q7: the crawl and one reading of the opening words with the site, as start's; the reading's own sentence
    (consult.reading_say) and a `reading` payload the home draws a facts card from (R6), then the next
    question -- the listing question when the lookup offers the business, else the trade's first. The session id is the
    same; the service question leads with the site's services; the name and the services are no longer asked, so the
    count went down; the reading counts toward the day's model calls."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    before = consult.get(conn, biz, sid)
    readings = len(fake.extract_calls)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE] if found else [])
    fake.knowledge = knowledge_fixture()  # what the site says: Invisalign and cleanings
    res = _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    assert crawled.urls == [SITE] and len(fake.extract_calls) == readings + 1
    assert fake.extract_calls[-1]["text"] == FIRST_WORDS and fake.extract_calls[-1]["pages"][0]["text"] == (
        "We do Invisalign"
    )
    assert res["say"] == f"{consult.reading_say(res['data']['reading'])} {consult.question_sentence(res['next'])}"
    assert res["data"]["reading"]["host"] == "brightsmile.example" and res["data"]["reading"]["services"][:1] == [
        "Invisalign"
    ]
    assert not JARGON.search(res["say"])
    assert res["data"]["session_id"] == sid
    assert _position(res) == (("listing", 2, 7) if found else ("objective", 2, 6))
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"] == {"url": SITE, "raw": "brightsmile.example", "source": "other", "read": True}
    assert (s["status"], s["step"], s["tree"]) == ("open", 0, "dentists")
    assert [o["label"] for o in s["options"]["service"]][:2] == ["Invisalign", "Cleanings"]
    assert [o["label"] for o in before["options"]["service"]][:2] == ["General check-ups", "Cleaning"]
    assert not {"business_name", "services_offered"} & set(res["data"]["remaining"])
    assert business.latest_knowledge(conn, biz)[1]["website"] == SITE
    assert [c["place_id"] for c in listings.offer(conn, biz)] == (["ChIJ1"] if found else [])
    assert conn.execute("SELECT count(*) AS n FROM consult_session").fetchone()["n"] == 1  # no second session
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'w1'")
    assert flag.fetchone()["f"] == "true"  # auth.check_llm_cap counts the reading


def test_a_site_that_cannot_be_read_is_asked_again_once_with_try_again_and_ask_instead(
    client, conn, fake, crawled, monkeypatch
):
    """Spec 2026-10-12 §3.2 (R1): the first failure records nothing, keeps the address on the session's known facts with
    one try spent, offers Try again and Ask me instead, and says why; the profile is not marked yet."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    readings = len(fake.extract_calls)
    crawled.fail = crawl.CrawlError("website_url must be a public address")
    res = _answer(client, first["key"], sid, {"website": {"other": "https://brightsmile.example/"}}, "w1")
    assert crawled.urls == [SITE] and len(fake.extract_calls) == readings
    said = consult.SITE_UNREAD_SAY.format(host="brightsmile.example")
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}"
    assert res["data"]["recorded"] == {} and _position(res) == ("website", 1, 9)
    q = res["next"]["question"]
    assert q["prompt"] == consult.UNOPENED_PROMPT and q["options"] == list(consult.RETRY_OPTIONS)
    s = consult.get(conn, biz, sid)
    assert "website" not in s["answers"] and s["known_facts"]["website"] == {"url": SITE, "read": False, "tries": 1}
    # no retry counted: the known fact's tries track the fetch, and the re-asked question's own miss counts (final
    # review Important 2)
    assert consult.RETRY_KEY not in s["answers"]
    assert business.latest_knowledge(conn, biz)[1].get("website_unread") is None


def test_try_again_fetches_once_more_then_the_site_is_recorded_unread_and_the_name_asked(
    client, conn, fake, crawled, monkeypatch
):
    """Try again fetches the same address again; a second failure records it unread, marks the profile (R2-e) and asks
    the name next, with SITE_UNREAD_AGAIN."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    res = _answer(client, first["key"], sid, {"website": {"choice": "retry"}}, "w2")
    assert crawled.urls == [SITE, SITE]
    assert (
        res["say"]
        == f"{consult.SITE_UNREAD_AGAIN.format(host='brightsmile.example')} {consult.question_sentence(res['next'])}"
    )
    assert _position(res) == ("business_name", 2, 9)
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"] == {
        "url": SITE,
        "retry": True,
        "raw": "retry",
        "source": "choice",
        "read": False,
        "again": True,
    }
    on_file = business.latest_knowledge(conn, biz)[1]
    assert on_file["website_unread"] == SITE and on_file.get("website") is None
    # a third time: not fetched
    third = _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w3")
    assert crawled.urls == [SITE, SITE] and third["data"]["recorded"]["website"]["read"] is False


def test_try_again_that_reads_the_site_starts_the_session_again(client, conn, fake, crawled, monkeypatch):
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid = first["session_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    res = _answer(client, first["key"], sid, {"website": {"choice": "retry"}}, "w2")
    assert crawled.urls == [SITE, SITE] and res["data"]["session_id"] == sid
    s = consult.get(conn, first["business_id"], sid)
    assert s["answers"]["website"]["read"] is True and s["tree"] == "dentists"
    assert consult.site_unopened(s.get("known_facts")) is None  # never "unread" of a site the answer says was read


def test_try_again_counts_toward_the_days_model_calls_and_ask_me_instead_does_not(
    client, conn, fake, crawled, monkeypatch
):
    """The global constraint: Try again fetches and reads the tried address as a typed one is, so its call counts toward
    the day's model calls (the audit's free_text, which auth.check_llm_cap counts); Ask me instead reads nothing."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid = first["session_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    _answer(client, first["key"], sid, {"website": {"choice": consult.RETRY}}, "w2")
    _answer(client, first["key"], sid, {"website": {"choice": consult.ASK_INSTEAD}}, "w3")
    rows = conn.execute(
        "SELECT idempotency_key AS k, args_redacted->>'free_text' AS f FROM agent_action "
        "WHERE idempotency_key IN ('w2', 'w3') ORDER BY idempotency_key"
    ).fetchall()
    assert [(r["k"], r["f"]) for r in rows] == [("w2", "true"), ("w3", "false")]


def test_ask_me_instead_records_the_site_unread_without_a_fetch(client, conn, fake, crawled, monkeypatch):
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    res = _answer(client, first["key"], sid, {"website": {"choice": "ask_instead"}}, "w2")
    assert crawled.urls == [SITE] and _position(res) == ("business_name", 2, 9)
    assert consult.SITE_UNREAD_AGAIN.format(host="brightsmile.example") not in res["say"]
    assert consult.get(conn, biz, sid)["answers"]["website"]["read"] is False
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == SITE


def test_another_address_after_a_failure_is_a_fresh_try(client, conn, fake, crawled, monkeypatch):
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid = first["session_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    res = _answer(client, first["key"], sid, {"website": {"other": "bright-smile.com"}}, "w2")
    assert crawled.urls == [SITE, "https://bright-smile.com"] and _position(res) == ("website", 1, 9)
    assert consult.get(conn, first["business_id"], sid)["known_facts"]["website"] == {
        "url": "https://bright-smile.com",
        "read": False,
        "tries": 1,
    }


def test_no_website_said_after_a_failure_takes_the_address_off(client, conn, fake, crawled, monkeypatch):
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    res = _answer(client, first["key"], sid, {"website": {"other": "no website"}}, "w2")
    assert _position(res) == ("business_name", 2, 9)
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"].get("none") is True and s["known_facts"].get("website") is None
    assert business.latest_knowledge(conn, biz)[1].get("website_unread") is None
    assert s["options"]["website"] == [{"id": "none", "label": consult.NO_WEBSITE}]  # the question's own (review)


def test_words_that_are_no_address_after_a_failure_keep_it_on_the_profile(client, conn, fake, crawled, monkeypatch):
    """Task 3 review (ruling I1b), as the start path does it: words that are no address after a first failure are asked
    about once (NOT_A_SITE: the first failure counted no retry, final review Important 2), and a second time end the
    question as no website by the retry rule (a note) -- the address tried staying on the profile as `website_unread`,
    as the start path keeps it (ruling R0-i2): only the owner's own "No website" takes it off."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    once = _answer(client, first["key"], sid, {"website": {"other": "I'm not sure"}}, "w2")
    assert once["say"] == f"{consult.NOT_A_SITE} {consult.question_sentence(once['next'])}"
    assert _position(once) == ("website", 1, 9) and once["next"]["question"]["options"] == list(consult.RETRY_OPTIONS)
    assert consult.get(conn, biz, sid)["answers"][consult.RETRY_KEY] == {"website": 1}  # this miss counts
    res = _answer(client, first["key"], sid, {"website": {"other": "I'm not sure"}}, "w3")
    assert crawled.urls == [SITE] and _position(res) == ("business_name", 2, 9)
    answer = consult.get(conn, biz, sid)["answers"]["website"]
    assert answer["none"] is True and "note" in answer and "url" not in answer
    on_file = business.latest_knowledge(conn, biz)[1]
    assert on_file["website_unread"] == SITE and on_file.get("website") is None


def test_try_again_typed_is_the_tap_and_counted(client, conn, fake, crawled, monkeypatch):
    """Task 3 review (ruling I1a), through the command: "Try again" typed -- as the home's harness sends what the owner
    types -- fetches the address tried once more, and the call counts toward the day's model calls."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid = first["session_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    res = _answer(client, first["key"], sid, {"website": {"other": "Try again"}}, "w2")
    assert crawled.urls == [SITE, SITE] and res["say"].startswith("I read brightsmile.example")
    assert consult.get(conn, first["business_id"], sid)["answers"]["website"]["read"] is True
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'w2'")
    assert flag.fetchone()["f"] == "true"


def test_a_corrected_address_whose_first_try_fails_asks_the_website_question_again(
    client, conn, fake, crawled, monkeypatch
):
    """Task 3 review (ruling I2): the website question answered (Ask me instead), the owner corrects it with another
    address -- the home's go_to, or an assistant answering it again -- that cannot be read: its first failure reopens
    the question (consult.forget_answer), asked again with Try again and Ask me instead, and the profile's
    `website_unread` is left as it was until the question resolves; Ask me instead then puts the new address there."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")
    _answer(client, key, sid, {"website": {"choice": consult.ASK_INSTEAD}}, "w2")
    res = _answer(client, key, sid, {"website": {"other": "bright-smile.com"}}, "w3")
    assert crawled.urls == [SITE, "https://bright-smile.com"]
    said = consult.SITE_UNREAD_SAY.format(host="bright-smile.com")
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}"
    assert _position(res) == ("website", 1, 9) and res["next"]["question"]["options"] == list(consult.RETRY_OPTIONS)
    s = consult.get(conn, biz, sid)
    assert "website" not in s["answers"] and s["status"] == "open"
    assert s["known_facts"]["website"] == {"url": "https://bright-smile.com", "read": False, "tries": 1}
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == SITE  # until the question resolves
    _answer(client, key, sid, {"website": {"choice": consult.ASK_INSTEAD}}, "w4")
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == "https://bright-smile.com"


def test_a_ready_session_whose_corrected_address_fails_is_open_again(client, conn, fake, crawled, monkeypatch):
    """Task 3 review (ruling I2): a session with every question answered (ready) whose website is corrected with an
    address that cannot be read is open again -- the website question pending, nothing to build a plan from yet."""
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    key = business_with_key(conn, claim_state="claimed")
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # the words named no site
    args = {"text": "Family dentist in Palo Alto", "known_facts": KNOWN_TRADE}
    sid = data(call_tool(client, "start", args, key=key))["session_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    fake.reads[SERVICES_WORDS] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=True, services=["Cleanings", "Invisalign"]
    )
    _answer(client, key, sid, {"business_name": {"other": NAME}}, "name")
    assert _answer(client, key, sid, {"services_offered": {"other": SERVICES_WORDS}}, "services")["data"]["ready"]
    crawled.empty = True
    res = _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w2")
    assert res["data"]["ready"] is False and res["next"]["question"]["id"] == "website"
    assert consult.get(conn, "biz_1", sid)["status"] == "open"


@pytest.mark.parametrize(
    ("words", "fetched"),
    [("try again please", True), ("ok try again", True), ("please retry", True), ("ask me instead please", False)],
)
def test_the_buttons_words_typed_another_way_are_the_tap(client, conn, fake, crawled, monkeypatch, words, fetched):
    """Final review Important 2: after a first failure, "try again please", "ok try again" or "please retry" fetch the
    address tried once more, and "ask me instead please" keeps it unread with no fetch -- never "no website" by the
    retry rule, the name asked with nothing fetched."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    res = _answer(client, first["key"], sid, {"website": {"other": words}}, "w2")
    assert crawled.urls == ([SITE, SITE] if fetched else [SITE]) and _position(res) == ("business_name", 2, 9)
    answer = consult.get(conn, biz, sid)["answers"]["website"]
    assert (answer["url"], answer["read"], answer.get("again"), answer.get("asked")) == (
        (SITE, False, True, None) if fetched else (SITE, False, None, True)
    )
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == SITE


def test_an_address_on_the_name_question_after_the_tries_are_spent_is_asked_again_once(
    client, conn, fake, crawled, monkeypatch
):
    """Final review Important 1(a): the address's two tries spent (a failure, Try again, a failure) and the name asked,
    words that carry the address -- no reroute, its tries spent -- are no name: NAME_AGAIN once, nothing fetched; the
    second time the words are the name (R0-l)."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")
    assert _position(_answer(client, key, sid, {"website": {"choice": consult.RETRY}}, "w2")) == ("business_name", 2, 9)
    words = "it's https://brightsmile.example, check again"
    once = _answer(client, key, sid, {"business_name": {"other": words}}, "n1")
    assert once["say"] == f"{consult.NAME_AGAIN} {consult.question_sentence(once['next'])}"
    assert _position(once) == ("business_name", 2, 9) and crawled.urls == [SITE, SITE]
    s = consult.get(conn, biz, sid)
    assert "business_name" not in s["answers"] and s["answers"][consult.RETRY_KEY]["business_name"] == 1
    twice = _answer(client, key, sid, {"business_name": {"other": words}}, "n2")
    assert consult.get(conn, biz, sid)["answers"]["business_name"]["name"] == words and crawled.urls == [SITE, SITE]
    assert _position(twice)[0] != "business_name"


def test_an_address_on_the_name_question_after_the_retry_rule_closed_the_website_is_fetched(
    client, conn, fake, crawled, monkeypatch
):
    """Final review Important 1(b): a first failure, then words that are no address twice -- the retry rule closes the
    website question as no website (a note), the address tried kept with a try left. A bare address pasted on the name
    question is the website question's answer (the reroute: the retry rule's note is not the owner's "No website"),
    fetched and read; never the business's name."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")
    _answer(client, key, sid, {"website": {"other": "I'm not sure"}}, "w2")
    closed = _answer(client, key, sid, {"website": {"other": "I'm not sure"}}, "w3")
    assert _position(closed) == ("business_name", 2, 9)
    assert consult.get(conn, biz, sid)["answers"]["website"].get("note") == "I'm not sure"
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    res = _answer(client, key, sid, {"business_name": {"other": SITE}}, "n1")
    assert crawled.urls == [SITE, SITE] and res["say"].startswith("I read brightsmile.example")
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"]["read"] is True and "business_name" not in s["answers"]
    assert business.latest_knowledge(conn, biz)[1].get("name") != SITE


def test_a_read_sites_answer_stands_when_a_correcting_address_fails_its_first_try(
    client, conn, fake, crawled, monkeypatch
):
    """Final review minor 4: only an unread website answer is reopened by a correcting address's failed first try
    (consult.forget_answer). A site read stands -- its answer, the session started from it, the questions it took off
    and the profile's website -- and the failing address is kept on the known facts, unread with its try counted, and
    said (SITE_UNREAD_SAY); the next question is the one asked before."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    fake.knowledge = knowledge_fixture()
    read = _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")
    before = consult.get(conn, biz, sid)
    assert consult.site_read(before)
    crawled.empty = True
    res = _answer(client, key, sid, {"website": {"other": "bright-smile.com"}}, "w2")
    assert crawled.urls == [SITE, "https://bright-smile.com"]
    said = consult.SITE_UNREAD_SAY.format(host="bright-smile.com")
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}"
    assert _position(res) == _position(read) and res["data"]["recorded"] == {}
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"] == before["answers"]["website"] and s["status"] == before["status"]
    assert consult.site_read(s) and not {"business_name", "services_offered"} & set(res["data"]["remaining"])
    assert s["known_facts"]["website"] == {"url": "https://bright-smile.com", "read": False, "tries": 1}
    on_file = business.latest_knowledge(conn, biz)[1]
    assert on_file["website"] == SITE and on_file.get("website_unread") is None


def test_an_address_in_a_sentence_answering_the_website_is_read_and_counted(client, conn, fake, crawled, monkeypatch):
    """Ruling R2-d through the command: "yes, it's www.brightsmile.example" gives the address in its words (`www.` in
    front: an address among words, ruling R2-f); it is read as a typed address is, and the call counts toward the day's
    model calls (the audit's free_text)."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    fake.knowledge = knowledge_fixture()
    words = {"website": {"other": "yes, it's www.brightsmile.example"}}
    res = _answer(client, first["key"], first["session_id"], words, "w1")
    assert crawled.urls == ["https://www.brightsmile.example"] and res["say"].startswith("I read brightsmile.example")
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'w1'")
    assert flag.fetchone()["f"] == "true"


def test_a_model_that_fails_reading_the_site_answers_nothing(client, conn, fake, crawled, monkeypatch):
    """The crawl worked and the reading failed: the owner reads why, nothing is recorded and the question still waits
    -- a site Admiry could open is never said to be one it could not."""
    first = _homepage(conn, fake)
    fake.error = "the model is busy; try again in a minute"
    res = call_tool(
        client,
        "answer",
        {"session_id": first["session_id"], "answers": {"website": {"other": SITE}}, "idempotency_key": "w1"},
        key=first["key"],
    )
    assert res["isError"] and "the model is busy" in tool_text(res)
    s = consult.get(conn, first["business_id"], first["session_id"])
    assert "website" not in s["answers"] and consult.next_question(s)["question"]["id"] == "website"


@pytest.mark.parametrize("value", [{"choice": "none"}, {"other": "We don't have one"}], ids=["the option", "words"])
def test_no_website_asks_the_name_next_without_the_model(client, conn, fake, crawled, monkeypatch, value):
    """Q6 and the global constraint: "No website" asks the name next; the website's words never reach the model, build
    no generator, check no daily limit and are not counted against it."""
    first = _homepage(conn, fake)
    monkeypatch.setattr(llm, "generator", lambda settings=None: pytest.fail("no model for the website question"))
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY_DRAFT", 0)  # checked, the limit would refuse
    res = _answer(client, first["key"], first["session_id"], {"website": value}, "w1")
    assert res["say"] == consult.question_sentence(res["next"]) and _position(res) == ("business_name", 2, 9)
    assert crawled.urls == [] and fake.read_calls == []
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'w1'")
    assert flag.fetchone()["f"] == "false"


@pytest.mark.parametrize(
    ("words", "said", "social"),
    [
        ("info@brightsmile.com", consult.NOT_A_SITE, None),
        ("facebook.com/brightsmile", consult.SOCIAL.format(host="facebook.com"), "https://facebook.com/brightsmile"),
        (
            "https://www.yelp.com/biz/bright-smile-dental",
            consult.SOCIAL.format(host="yelp.com"),
            "https://www.yelp.com/biz/bright-smile-dental",
        ),
    ],
    ids=["an e-mail address", "a facebook page", "a yelp page"],
)
def test_an_email_address_or_a_social_page_is_refused_and_nothing_is_fetched(
    client, conn, fake, crawled, words, said, social
):
    """Review focus: an e-mail address is not a site (NOT_A_SITE); a social page is not the business's own (SOCIAL,
    its address kept as the session's known fact `social`); either counts a retry (ruling F4). Nothing is fetched or
    read, nothing is recorded, and the owner can still paste a site or tap No website."""
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    readings = len(fake.extract_calls)
    res = _answer(client, first["key"], sid, {"website": {"other": words}}, "w1")
    assert crawled.urls == [] and len(fake.extract_calls) == readings and fake.read_calls == []
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}" and not JARGON.search(res["say"])
    assert res["data"]["recorded"] == {} and _position(res) == ("website", 1, 9)
    s = consult.get(conn, biz, sid)
    assert "website" not in s["answers"]
    assert s["known_facts"].get("social") == social
    assert (s["answers"].get(consult.RETRY_KEY) or {}).get("website") == 1
    again = _answer(client, first["key"], sid, {"website": {"choice": "none"}}, "w2")
    assert _position(again) == ("business_name", 2, 9)


def test_the_count_never_grows_from_the_first_turn_to_the_plan(client, conn, fake, crawled, monkeypatch):
    """Review focus: the first turn counts the name and the services, and the listing question while the name may
    still find the business (ruling F1); the site read takes them off; every turn after asks the next of the same
    number or fewer -- never "3 / 7" then "4 / 8"."""
    benchmark.seed(conn)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE])
    fake.knowledge = knowledge_fixture()
    walk = {
        "website": {"other": "brightsmile.example"},
        **PICKED,
        "objective": {"choice": "bookings"},
        "service": {"choice": "svc_invisalign"},
        "customer_value": {"choice": "1000_plus"},
        "radius": {"choice": "r5"},
    }
    seen, turn = [_position(first)], first
    while turn["next"]["kind"] == "question":
        qid = turn["next"]["question"]["id"]
        turn = _answer(client, first["key"], first["session_id"], {qid: walk[qid]}, f"walk-{qid}")
        if turn["next"]["kind"] == "question":
            seen.append(_position(turn))
    assert seen == [
        ("website", 1, 9),
        ("listing", 2, 7),
        ("objective", 3, 6),  # the place picked centres the ads: where is not asked
        ("service", 4, 6),
        ("customer_value", 5, 6),
        ("radius", 6, 6),
    ]
    assert turn["data"]["ready"] is True


def test_answers_given_before_the_website_are_kept_when_the_new_tree_still_offers_them(
    client, conn, fake, crawled, monkeypatch
):
    """Review focus: an assistant answered the objective and the value before the website; the site read starts the
    same session again with the reading's tree -- the objective, a choice the dental tree offers too, is kept; the value
    band, the fallback tree's own, is dropped and asked again in its turn."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    key = business_with_key(conn, claim_state="claimed")
    general = {"website": None, "category": "fallback", "category_detail": "local business", "services": []}
    fake.knowledge = fake.knowledge.model_copy(update=general)
    started_res = call_tool(client, "start", {"text": "We help people around Palo Alto"}, key=key)
    assert not started_res["isError"], tool_text(started_res)
    sid = data(started_res)["session_id"]
    assert consult.get(conn, "biz_1", sid)["tree"] == "fallback"
    early = {"objective": {"choice": "calls"}, "customer_value": {"choice": "lt_500"}}
    _answer(client, key, sid, early, "a1")
    fake.knowledge = knowledge_fixture()  # the site: a family dentist
    res = _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")
    s = consult.get(conn, "biz_1", sid)
    assert res["data"]["session_id"] == sid and s["tree"] == "dentists"
    assert s["answers"]["objective"] == {"choice": "calls", "raw": "calls", "source": "choice"}
    assert "customer_value" not in s["answers"]
    assert _position(res) == ("service", 3, 6)
    assert conn.execute("SELECT count(*) AS n FROM consult_session").fetchone()["n"] == 1


def test_a_reading_past_the_days_model_calls_is_refused_before_any_crawl(client, conn, fake, crawled, monkeypatch):
    """The global constraint: the reading of a site answered counts toward the day's model calls, checked before the
    crawl; "No website" is no reading and goes through."""
    first = _homepage(conn, fake)
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY_DRAFT", 1)  # the first turn's reading used it
    res = call_tool(
        client,
        "answer",
        {"session_id": first["session_id"], "answers": {"website": {"other": SITE}}, "idempotency_key": "w1"},
        key=first["key"],
    )
    assert res["isError"] and "today's limit" in tool_text(res) and crawled.urls == []
    ok = _answer(client, first["key"], first["session_id"], {"website": {"choice": "none"}}, "w2")
    assert _position(ok) == ("business_name", 2, 9)


def test_the_site_the_reading_saw_is_one_tap_and_read_like_an_address(client, conn, fake, crawled, monkeypatch):
    """Q5: the words named the site in a way the address pattern misses and the reading saw it (the dental fixture's
    website); the website question offers it first, and the tap reads it as a typed address would be."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = tools_start.begin_anonymous("Bright Smile Dental in Palo Alto, brightsmile dot example", ip="198.51.100.9")
    assert first["next"]["question"]["options"] == [
        {"id": "site_known", "label": "Yes: brightsmile.example", "url": SITE},
        {"id": "none", "label": consult.NO_WEBSITE},
    ]
    assert crawled.urls == []  # nothing was read at start
    d = first["data"]
    res = _answer(client, d["draft_key"], d["session_id"], {"website": {"choice": "site_known"}}, "t1")
    assert crawled.urls == [SITE] and res["say"].startswith("I read brightsmile.example")
    assert consult.get(conn, d["business_id"], d["session_id"])["answers"]["website"] == {
        "choice": "site_known",
        "url": SITE,
        "raw": "site_known",
        "source": "choice",
        "read": True,
    }


# The trade's questions known at start (R2-e's walks): with no map lookup on this server no place is offered and where
# the ads centre is skipped, so without a site only the name and the services wait
KNOWN_TRADE = {"objective": "bookings", "service": "Invisalign", "customer_value_usd": 3000, "radius_miles": 5}
NAME, SERVICES_WORDS = "Bright Smile Dental", "cleanings and Invisalign"


def _plan_without_a_site(client, conn, fake, key: str, sid: str, name: str = NAME) -> dict:
    """The name (`name`, answered in the owner's words, never read by the model) and the services (read by the model
    fake), then the plan built: its envelope."""
    fake.reads[SERVICES_WORDS] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=True, services=["Cleanings", "Invisalign"]
    )
    _answer(client, key, sid, {"business_name": {"other": name}}, "name")
    assert _answer(client, key, sid, {"services_offered": {"other": SERVICES_WORDS}}, "services")["data"]["ready"]
    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "plan"}, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


@pytest.mark.parametrize("path", ["website_url at start", "the website answer"])
def test_a_site_that_could_not_be_read_is_never_the_knowledges_website(client, conn, fake, crawled, monkeypatch, path):
    """Ruling R2-e: an address that could not be read is the knowledge's `website_unread`, never its `website` -- so the
    reply's steps do not say the reading was done, and no unread site vouches for the name the model took from the
    owner's words: the plan's goal sentence does not lead with it. The owner skips the name question here: their own
    answer to it would be vouched for (A3p-5 batch 2 Task 0, rulings R0-k and R0-l)."""
    benchmark.seed(conn)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    key = business_with_key(conn, claim_state="claimed")
    args = {"text": "Family dentist in Palo Alto", "known_facts": KNOWN_TRADE}
    crawled.empty = True  # nothing read: a redirect to another host, a timeout
    if path == "website_url at start":  # the reading's own website for the site is taken off too
        begun = call_tool(client, "start", {**args, "website_url": SITE}, key=key)
        assert not begun["isError"], tool_text(begun)
        sid = data(begun)["session_id"]
        assert begun["structuredContent"]["next"]["question"]["id"] == "website"  # asked again (ruling R0-i)
        turn = _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")  # the same: its last try
        assert crawled.urls == [SITE, SITE]  # start's try and this one (spec 2026-10-12 §3.2, R1)
        assert consult.SITE_UNREAD_AGAIN.format(host="brightsmile.example") in turn["say"]
    else:
        fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # the words named no site
        sid = data(call_tool(client, "start", args, key=key))["session_id"]
        once = _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")  # asked again (R1)
        assert consult.SITE_UNREAD_SAY.format(host="brightsmile.example") in once["say"]
        turn = _answer(client, key, sid, {"website": {"choice": consult.ASK_INSTEAD}}, "w2")  # no fetch
        assert crawled.urls == [SITE]
    assert turn["next"]["question"]["id"] == "business_name"
    assert turn["data"]["steps"][1] == {"label": "Read your website and Google listing", "done": False}
    on_file = business.latest_knowledge(conn, "biz_1")[1]
    assert on_file["website_unread"] == SITE and on_file.get("website") is None
    goal = _plan_without_a_site(client, conn, fake, key, sid, "I'd rather not say")["data"]["cards"]["plan"]["goal"]
    assert NAME not in goal and goal == "More Invisalign bookings within 5 miles of your business"


# An address in the words of any answer, while the site is unread: it answers the website question instead, never the
# question actually asked (spec 2026-10-12 §3.4, R2 -- the founder's walk, 2026-10-12)
def test_an_address_in_the_name_answer_re_answers_the_website_question(client, conn, fake, crawled, monkeypatch):
    """R2 (the founder's walk, 2026-10-12): the complaint "https://… works fine for me. Why cannot you open it" typed on
    the name question is a website answer, never the name; the site is read and the session starts again."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    _answer(client, first["key"], sid, {"website": {"choice": "ask_instead"}}, "w2")
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    words = "https://brightsmile.example works fine for me. Why cannot you open it"
    res = _answer(client, first["key"], sid, {"business_name": {"other": words}}, "w3")
    assert crawled.urls == [SITE, SITE] and res["data"]["session_id"] == sid
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"]["read"] is True and "business_name" not in s["answers"]
    assert business.latest_knowledge(conn, biz)[1].get("name") != words
    # the reroute's own read is counted toward the day's model calls, as any website reading is (review focus 3)
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'w3'")
    assert flag.fetchone()["f"] == "true"


def test_an_address_for_a_question_id_this_consultation_does_not_ask_is_still_refused(
    client, conn, fake, crawled, monkeypatch
):
    """Review focus 4: the reroute only runs over a qid the consultation actually asks (`by_id`) -- an unknown one
    still reaches the usual "no question" refusal, never silently becomes a website answer."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    res = call_tool(
        client,
        "answer",
        {
            "session_id": first["session_id"],
            "answers": {"nope": {"other": "see https://brightsmile.example"}},
            "idempotency_key": "w1",
        },
        key=first["key"],
    )
    assert res["isError"] and "no question 'nope' in this consultation" in tool_text(res)
    assert crawled.urls == []


def test_calls_the_model_counts_an_address_in_any_answers_words_never_a_yes_or_a_retry():
    """Review focus (fix round 2, Important 1): the free_text check added for a non-website answer is the
    address-in-words one alone (consult.site_in_words) -- never `_reads_a_site`'s "yes"/"Try again" heuristics, which
    belong to the website question's own options and must not reach the listing or business_name questions (whose
    literal regression was test_a_yes_links_the_one_place_offered's "Yes, that's us")."""
    assert tools_consult._calls_the_model("listing", {"other": "Yes, that's us"}) is False
    assert tools_consult._calls_the_model("business_name", {"other": "see https://brightsmile.example"}) is True


def test_two_answers_at_once_with_one_address_become_the_website_answer_alone(client, conn, fake, crawled, monkeypatch):
    """Minor (fix round 2): a call with two answers, only one of them carrying an address, becomes one website answer
    by design -- the reroute replaces the whole mapping, so the other answer is not recorded either."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid, biz = first["session_id"], first["business_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    _answer(client, first["key"], sid, {"website": {"choice": "ask_instead"}}, "w2")
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    res = _answer(
        client,
        first["key"],
        sid,
        {
            "business_name": {"other": "https://brightsmile.example works fine for me."},
            "services_offered": {"other": "cleanings"},
        },
        "w3",
    )
    assert crawled.urls == [SITE, SITE] and res["data"]["session_id"] == sid
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"]["read"] is True
    assert "business_name" not in s["answers"] and "services_offered" not in s["answers"]


def test_an_address_in_a_choice_questions_words_answers_the_website_and_leaves_the_question_open(
    client, conn, fake, crawled, monkeypatch
):
    """Review focus 3."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = _homepage(conn, fake)
    sid = first["session_id"]
    crawled.empty = True
    _answer(client, first["key"], sid, {"website": {"other": "brightsmile.example"}}, "w1")
    _answer(client, first["key"], sid, {"website": {"choice": "ask_instead"}}, "w2")
    _answer(client, first["key"], sid, {"business_name": {"other": "Bright Smile"}}, "w3")
    crawled.empty = False
    fake.knowledge = knowledge_fixture()
    res = _answer(
        client, first["key"], sid, {"services_offered": {"other": "see https://brightsmile.example for the list"}}, "w4"
    )
    assert (
        crawled.urls == [SITE, SITE]
        and "services_offered" not in consult.get(conn, first["business_id"], sid)["answers"]
    )
    assert res["data"]["reading"]["host"] == "brightsmile.example"


# Opening words that name the site start cannot open, and the website question that asks for it again (A3p-5 batch 2
# Task 0, ruling R0-i); and words with a typo in the address (fix round 1, ruling R0-i3)
UNOPENED_WORDS = "Family dentist at https://brightsmile.example in Palo Alto, we want more Invisalign patients"
TYPO = "https://brightsmlie.com"
TYPO_WORDS = "Family dentist at brightsmlie.com in Palo Alto, we want more Invisalign patients"


def _unopened(conn, fake, crawled, path: str = "words", words: str = UNOPENED_WORDS, site: str = SITE) -> dict:
    """A first turn on a site whose crawl reads no page (ruling R0-i): `site`, the address in the opening `words`, or a
    website_url passed beside them as an assistant passes one (the session is built the same way: the keyed `start`'s
    own path is test_a_site_that_could_not_be_read_is_never_the_knowledges_website's); the reading named no service,
    and start's lookup finds nothing. The crawl reads pages again afterwards. Returns {"key", "business_id",
    "session_id", "say", "next"}, as `_homepage` does."""
    fake.knowledge = fake.knowledge.model_copy(update={"website": None, "services": []})
    crawled.empty = True
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(places, "search", lambda query, key, **kw: [])
        if path == "words":
            res = tools_start.begin_anonymous(words, ip="198.51.100.9")
        else:
            res = tools_start.begin_anonymous(FIRST_WORDS, site, ip="198.51.100.9")
    crawled.empty = False
    said = consult.UNOPENED_PROMPT
    assert res["next"]["question"]["prompt"] == said and crawled.urls == [site]
    assert res["say"].count(consult.SITE_UNREAD_SAY.format(host=site.removeprefix("https://"))) == 1
    assert res["next"]["question"]["options"] == list(consult.RETRY_OPTIONS)
    d = res["data"]
    return {
        "key": d["draft_key"],
        "business_id": d["business_id"],
        "session_id": d["session_id"],
        "say": res["say"],
        "next": res["next"],
    }


@pytest.mark.parametrize("path", ["words", "website_url"])
def test_another_address_for_a_site_start_could_not_open_is_read(client, conn, fake, crawled, path):
    """Ruling R0-i (A3p-5 batch 2 Task 0, item d): the owner corrects the address start could not open, from the words
    or an assistant's website_url: the new one is read as any typed address is -- one crawl, one reading, the session
    started again in place -- and the address that could not be read leaves the profile with the reading that names a
    site (ruling F3)."""
    first = _unopened(conn, fake, crawled, path)
    assert _position(first) == ("website", 1, 9)
    readings = len(fake.extract_calls)
    fake.knowledge = knowledge_fixture().model_copy(update={"website": "https://bright-smile.com"})
    res = _answer(client, first["key"], first["session_id"], {"website": {"other": "bright-smile.com"}}, "w1")
    assert crawled.urls == [SITE, "https://bright-smile.com"] and len(fake.extract_calls) == readings + 1
    assert res["say"] == f"{consult.reading_say(res['data']['reading'])} {consult.question_sentence(res['next'])}"
    assert res["data"]["reading"]["host"] == "bright-smile.com"
    assert res["data"]["session_id"] == first["session_id"]
    assert consult.get(conn, first["business_id"], first["session_id"])["answers"]["website"] == {
        "url": "https://bright-smile.com",
        "raw": "bright-smile.com",
        "source": "other",
        "read": True,
    }
    on_file = business.latest_knowledge(conn, first["business_id"])[1]
    assert on_file["website"] == "https://bright-smile.com" and "website_unread" not in on_file


@pytest.mark.parametrize("path", ["words", "website_url"])
def test_the_same_address_again_is_fetched_once_more_then_unread_and_the_name_is_next(
    client, conn, fake, crawled, path
):
    """Spec 2026-10-12 §3.2 (R1): the owner answers with the address start could not open (`www.` aside: same_host):
    start's try was its first, so it is fetched once more -- and, read no better, nothing reaches the model, it is
    recorded unread, kept on the profile as `website_unread`, SITE_UNREAD_AGAIN said once, and the consultation goes on
    as without a website: the name next. The same address answered after that is unread without a fetch."""
    first = _unopened(conn, fake, crawled, path)
    readings = len(fake.extract_calls)
    crawled.empty = True  # still nothing to read
    words = "www.brightsmile.example"
    res = _answer(client, first["key"], first["session_id"], {"website": {"other": words}}, "w1")
    assert crawled.urls == [SITE, "https://www.brightsmile.example"] and len(fake.extract_calls) == readings
    said = consult.SITE_UNREAD_AGAIN.format(host="brightsmile.example")
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}"
    assert _position(res) == ("business_name", 2, 9)
    assert consult.get(conn, first["business_id"], first["session_id"])["answers"]["website"] == {
        "url": "https://www.brightsmile.example",
        "raw": words,
        "source": "other",
        "read": False,
        "again": True,
    }
    on_file = business.latest_knowledge(conn, first["business_id"])[1]
    assert on_file["website_unread"] == "https://www.brightsmile.example" and on_file.get("website") is None
    after = _answer(client, first["key"], first["session_id"], {"website": {"other": "brightsmile.example"}}, "w2")
    assert crawled.urls == [SITE, "https://www.brightsmile.example"]  # its tries spent: not fetched a third time
    assert after["data"]["recorded"]["website"]["read"] is False


@pytest.mark.parametrize(
    "value",
    [{"other": "No, we don't have one"}, {"other": "No"}, {"other": "No website"}],
    ids=["words", "a no", "no website in words"],
)
def test_no_website_takes_an_address_start_could_not_open_off_the_profile(client, conn, fake, crawled, value):
    """Ruling R0-i: "No website", said, is the owner's latest answer: the address start could not open comes off the
    profile in a version of the owner's, nothing is fetched, and the consultation goes on as without a website -- a no
    that names no address too (fix round 1, ruling R0-i3). The question offers no "No website" to tap while the site is
    unopened: Try again and Ask me instead (spec 2026-10-12 §3.2, R1)."""
    first = _unopened(conn, fake, crawled)
    biz = first["business_id"]
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == SITE
    res = _answer(client, first["key"], first["session_id"], {"website": value}, "w1")
    assert crawled.urls == [SITE] and _position(res) == ("business_name", 2, 9)
    version, on_file = business.latest_knowledge(conn, biz)
    assert "website_unread" not in on_file and on_file.get("website") is None
    row = conn.execute("SELECT source FROM knowledge WHERE business_id = %s AND version = %s", (biz, version))
    assert row.fetchone()["source"] == "owner"


def test_try_again_then_ask_me_instead_on_a_site_start_could_not_open(client, conn, fake, crawled):
    """Spec 2026-10-12 §3.2 (R1), the start path: start's try was the address's first, so Try again is its last --
    fetched once more and, read no better, recorded unread with SITE_UNREAD_AGAIN and the name next; Ask me instead
    after it changes the answer with no fetch, the address kept on the profile."""
    first = _unopened(conn, fake, crawled)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    crawled.empty = True
    res = _answer(client, key, sid, {"website": {"choice": consult.RETRY}}, "w1")
    assert crawled.urls == [SITE, SITE] and _position(res) == ("business_name", 2, 9)
    said = consult.SITE_UNREAD_AGAIN.format(host="brightsmile.example")
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}"
    instead = _answer(client, key, sid, {"website": {"choice": consult.ASK_INSTEAD}}, "w2")
    assert crawled.urls == [SITE, SITE] and _position(instead) == ("business_name", 2, 9)
    assert consult.get(conn, biz, sid)["answers"]["website"] == {
        "url": SITE,
        "read": False,
        "asked": True,
        "raw": consult.ASK_INSTEAD,
        "source": "choice",
    }
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == SITE


def test_a_no_that_corrects_an_address_start_could_not_open_reads_the_new_one(client, conn, fake, crawled):
    """Ruling R0-i3 (A3p-5 batch 2 Task 0, fix round 1): the question asks again for brightsmlie.com, which start could
    not open, and the owner writes "No, it's brightsmile.com": the correction is read as a typed address is -- one
    crawl, one reading counted toward the day's model calls, the session started again in place -- never taken as "No
    website"."""
    first = _unopened(conn, fake, crawled, words=TYPO_WORDS, site=TYPO)
    readings = len(fake.extract_calls)
    fake.knowledge = knowledge_fixture().model_copy(update={"website": "https://brightsmile.com"})
    words = "No, it's brightsmile.com"
    res = _answer(client, first["key"], first["session_id"], {"website": {"other": words}}, "w1")
    assert crawled.urls == [TYPO, "https://brightsmile.com"] and len(fake.extract_calls) == readings + 1
    assert res["say"] == f"{consult.reading_say(res['data']['reading'])} {consult.question_sentence(res['next'])}"
    assert res["data"]["reading"]["host"] == "brightsmile.com"
    assert consult.get(conn, first["business_id"], first["session_id"])["answers"]["website"] == {
        "url": "https://brightsmile.com",
        "raw": words,
        "source": "other",
        "read": True,
    }
    on_file = business.latest_knowledge(conn, first["business_id"])[1]
    assert on_file["website"] == "https://brightsmile.com" and "website_unread" not in on_file
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'w1'")
    assert flag.fetchone()["f"] == "true"


def test_a_no_that_names_the_address_again_is_fetched_once_more_and_keeps_it(client, conn, fake, crawled):
    """Ruling R0-i3: "No, brightsmlie.com is right" names the address start could not open again -- the same-site path
    (spec 2026-10-12 §3.2, R1): fetched once more, its last try; read no better, kept as `website_unread`,
    SITE_UNREAD_AGAIN said, and the name next."""
    first = _unopened(conn, fake, crawled, words=TYPO_WORDS, site=TYPO)
    readings = len(fake.extract_calls)
    crawled.empty = True  # still nothing to read
    words = "No, brightsmlie.com is right"
    res = _answer(client, first["key"], first["session_id"], {"website": {"other": words}}, "w1")
    assert crawled.urls == [TYPO, TYPO] and len(fake.extract_calls) == readings
    said = consult.SITE_UNREAD_AGAIN.format(host="brightsmlie.com")
    assert res["say"] == f"{said} {consult.question_sentence(res['next'])}"
    assert _position(res) == ("business_name", 2, 9)
    assert consult.get(conn, first["business_id"], first["session_id"])["answers"]["website"] == {
        "url": TYPO,
        "raw": words,
        "source": "other",
        "read": False,
        "again": True,
    }
    on_file = business.latest_knowledge(conn, first["business_id"])[1]
    assert on_file["website_unread"] == TYPO and on_file.get("website") is None


def test_words_that_end_the_question_without_a_no_keep_the_address(client, conn, fake, crawled):
    """Ruling R0-i2: only the owner's own "No website" takes the address off the profile -- words that are no address,
    sent twice, answer the question as no website by the retry rule and keep it (the site may only be down)."""
    first = _unopened(conn, fake, crawled)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    once = _answer(client, key, sid, {"website": {"other": "it is down right now"}}, "m1")
    assert once["say"].startswith(consult.NOT_A_SITE) and _position(once) == ("website", 1, 9)
    twice = _answer(client, key, sid, {"website": {"other": "it is down right now"}}, "m2")
    assert _position(twice) == ("business_name", 2, 9)
    assert business.latest_knowledge(conn, biz)[1]["website_unread"] == SITE


def test_none_of_these_answered_before_the_site_is_asked_again_with_the_places_the_site_finds(
    client, conn, fake, crawled, monkeypatch
):
    """Ruling R2-i: "None of these" answered for the places the words found; the site, answered and read, finds another
    place close to the business -- the restart drops the earlier answer, so the listing question asks about it."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE])
    sid = data(call_tool(client, "start", {"text": "Family dentist in Palo Alto"}, key=key))["session_id"]
    assert _listing(client, key, sid, {"choice": "none"}, "none")["say"].startswith(listings.NONE_NOTED)
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [BRIGHT_SMILE, CARE])  # ChIJ1 is remembered as not it
    res = _answer(client, key, sid, {"website": {"other": "brightsmile.example"}}, "w1")
    q = res["next"]["question"]
    assert res["say"].startswith("I read brightsmile.example") and q["id"] == "listing"
    assert [o["label"].split(", ")[0] for o in q["options"]] == ["Bright Smile Dental Care", "None of these"]
    assert "listing" not in consult.get(conn, "biz_1", sid)["answers"]


# ---------------------------------------------------------------- the name and the services (A3p-5 batch 1 Task 3)


def test_a_name_answer_looks_the_business_up_and_offers_it_next(client, conn, fake, places_search):
    """Q11 and ruling F1: the name runs one lookup (the Places fake's own query, the name and the service area), the
    offer is saved, and the listing question, with its places, is the next question -- right after the name, before
    the services."""
    places_search.found = [BRIGHT_SMILE]
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    res = _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    assert places_search.queries == ["Bright Smile Dental Palo Alto"]
    assert res["next"]["question"]["id"] == "listing"
    assert res["next"]["question"]["options"][0]["id"] == "gm_1"
    assert [c["place_id"] for c in listings.offer(conn, biz)] == ["ChIJ1"]


def test_a_name_with_nothing_found_goes_on_to_the_services(client, conn, fake, places_search):
    """A lookup that matches nothing offers no listing: the services question, not the listing, is next."""
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    res = _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    assert places_search.queries == ["Bright Smile Dental Palo Alto"]
    assert res["next"]["question"]["id"] == "services_offered"
    assert listings.offer(conn, biz) == []


def test_the_name_joins_the_known_facts_and_fills_an_empty_profile_name(client, conn, fake, places_none):
    """Q11: the name joins the session's known facts, and the profile's own name -- empty until now -- becomes it,
    as one new version, source owner."""
    fake.knowledge = fake.knowledge.model_copy(update={"name": ""})
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    version = business.latest_knowledge(conn, biz)[0]
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    s = consult.get(conn, biz, sid)
    assert s["known_facts"]["business_name"] == "Bright Smile Dental"
    new_version, knowledge = business.latest_knowledge(conn, biz)
    assert new_version == version + 1 and knowledge["name"] == "Bright Smile Dental"
    assert knowledge["name_source"] == "owner"  # ruling R0-k


def test_the_owners_typed_name_beats_the_readings_guess(client, conn, fake, places_none):
    """Ruling R0-l (A3p-5 batch 2 Task 0, item e): the owner's answer to "What is the business called?" becomes the
    profile's name whatever the reading guessed, as one version of the owner's, marked as theirs (`name_source` owner,
    ruling R0-k) -- so the plan's sentences lead with it (plans.name_for). Its known fact is the same as before."""
    first = _homepage(conn, fake)  # the reading guessed "Bright Smile Dental" from the words alone
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    version, guessed = business.latest_knowledge(conn, biz)
    assert (guessed["name"], guessed["name_source"]) == ("Bright Smile Dental", "words")
    assert plans.name_for(guessed) is None
    _answer(client, key, sid, {"business_name": {"other": "Sunny Smiles"}}, "name")
    new_version, knowledge = business.latest_knowledge(conn, biz)
    assert new_version == version + 1
    assert (knowledge["name"], knowledge["name_source"]) == ("Sunny Smiles", "owner")
    assert plans.name_for(knowledge) == "Sunny Smiles"
    row = conn.execute("SELECT source FROM knowledge WHERE business_id = %s AND version = %s", (biz, new_version))
    assert row.fetchone()["source"] == "owner"
    assert consult.get(conn, biz, sid)["known_facts"]["business_name"] == "Sunny Smiles"


def test_correcting_the_name_says_updated_with_the_new_one(client, conn, fake, places_none):
    """Ruling R3-a: the name answer keeps `raw`, so a changed mind's "Updated: …" names the business by its new
    name instead of rendering blank (`tools_consult._answer_label`)."""
    first = _homepage(conn, fake)
    key, sid = first["key"], first["session_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    res = _answer(client, key, sid, {"business_name": {"other": "Sunny Smiles Dental"}}, "name-2")
    assert "Updated: Sunny Smiles Dental." in res["say"]


def test_the_name_never_calls_the_model_and_is_not_counted(client, conn, fake, monkeypatch):
    """The global constraint: the name's words never reach the model, build no generator, check no daily limit and
    are not counted against it (the audit's free_text)."""
    first = _homepage(conn, fake)
    key, sid = first["key"], first["session_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    monkeypatch.setattr(llm, "generator", lambda settings=None: pytest.fail("no model for the name question"))
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY_DRAFT", 0)  # checked, the limit would refuse
    res = _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    assert res["next"]["question"]["id"] == "listing"  # the fake's Places finds the business: next (ruling F1)
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'name'")
    assert flag.fetchone()["f"] == "false"


def test_services_words_the_profile_lacks_are_added_and_lead_the_service_options(client, conn, fake, places_none):
    """Q12: the services the model reads become names; what the profile does not already hold is added in one new
    version, counted toward the day's model calls, the service question's options lead with them, and the reply
    names what was added."""
    fake.knowledge = fake.knowledge.model_copy(update={"services": []})
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    version = business.latest_knowledge(conn, biz)[0]
    fake.reads["cleanings and Invisalign"] = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=True,
        services=["Cleanings", "Invisalign"],
    )
    res = _answer(client, key, sid, {"services_offered": {"other": "cleanings and Invisalign"}}, "services")
    new_version, knowledge = business.latest_knowledge(conn, biz)
    assert new_version == version + 1
    assert [sv["name"] for sv in knowledge["services"]] == ["Cleanings", "Invisalign"]
    assert "Added to your services: Cleanings and Invisalign." in res["say"]
    s = consult.get(conn, biz, sid)
    assert [o["label"] for o in s["options"]["service"]][:2] == ["Cleanings", "Invisalign"]
    assert fake.read_calls[-1]["text"] == "cleanings and Invisalign"
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'services'")
    assert flag.fetchone()["f"] == "true"


def test_services_the_profile_already_holds_add_nothing(client, conn, fake, places_none):
    """What the profile already has (`consult._same_label`: case and a trailing "s" aside) is not re-added, and
    nothing is said about it."""
    biz = "biz_1"
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # Invisalign and Cleanings stay on file
    key = business_with_key(conn, claim_state="claimed")
    sid = data(call_tool(client, "start", {"text": "Family dentist in Palo Alto"}, key=key))["session_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    version = business.latest_knowledge(conn, biz)[0]
    fake.reads["invisalign and cleanings"] = models.AnswerRead(
        option_id=None,
        amount_usd=None,
        miles=None,
        note=None,
        understood=True,
        services=["invisalign", "Cleaning"],  # the same services, lower-cased and singular
    )
    res = _answer(client, key, sid, {"services_offered": {"other": "invisalign and cleanings"}}, "services")
    assert business.latest_knowledge(conn, biz)[0] == version  # nothing written
    assert "Added to your services" not in res["say"]


# ---------------------------------------------------------------- the final review's fixes (A3p-5 batch 1)


@pytest.mark.parametrize(
    ("on_file", "read", "added"),
    [
        (["Teeth Whitening"], ["Teeth-whitening", "Implants"], ["Implants"]),
        ([], ["Root canal", "Root-canal"], ["Root canal"]),
        (["Invisalign"], ["Invisalign\u2122", "Teeth whitening", "teeth  whitening"], ["Teeth whitening"]),
    ],
    ids=["a hyphen for a space on file", "the same service twice in one read", "a trademark sign, two spaces"],
)
def test_services_named_with_other_punctuation_or_spacing_add_only_the_new_ones(
    client, conn, fake, places_none, on_file, read, added
):
    """Ruling F8: the services the words name are told from those on file, and from each other, as the profile tells
    them apart (`profile.norm`: case, punctuation and spacing aside) and a trailing "s" aside -- one on file under
    another spelling is skipped, the same one twice in one read is added once -- so the answer is recorded, never
    refused with "... is already on file"."""
    services = [models.Service(name=n, intent_phrases=[], price_hint=None) for n in on_file]
    fake.knowledge = fake.knowledge.model_copy(update={"website": None, "services": services})
    key = business_with_key(conn, claim_state="claimed")
    sid = data(call_tool(client, "start", {"text": "Family dentist in Palo Alto"}, key=key))["session_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "name")
    fake.reads["what we do"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=True, services=read
    )
    res = _answer(client, key, sid, {"services_offered": {"other": "what we do"}}, "services")
    assert consult.get(conn, "biz_1", sid)["answers"]["services_offered"]["services"] == read
    assert [sv["name"] for sv in knowledge_services(conn, "biz_1")] == [*on_file, *added]
    assert f"Added to your services: {consult._phrase(added)}." in res["say"]


@pytest.mark.parametrize("found", [True, False], ids=["the name finds the business", "the name finds nothing"])
def test_the_count_never_grows_and_the_listing_question_follows_the_name(client, conn, fake, places_search, found):
    """Ruling F1 (the Review Focus rule, the runbook's step 4): the homepage's first turn counts the listing question
    while the name may still find the business on Google Maps, so the count holds when it does -- the listing question
    is then next, before the services -- and drops by one when it finds nothing: it never grows."""
    first = _homepage(conn, fake)
    key, sid = first["key"], first["session_id"]
    assert "Before I plan anything, a few quick questions." in first["say"]  # nine (ruling R0-h)
    seen = [_position(first), _position(_answer(client, key, sid, {"website": {"choice": "none"}}, "w1"))]
    places_search.found = [BRIGHT_SMILE] if found else []
    seen.append(_position(_answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "n1")))
    if found:  # the place picked centres the ads: where is not asked
        seen.append(_position(_answer(client, key, sid, PICKED, "l1")))
    fake.reads["Invisalign"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=True, services=["Invisalign"]
    )
    seen.append(_position(_answer(client, key, sid, {"services_offered": {"other": "Invisalign"}}, "s1")))
    if found:
        tail = [("listing", 3, 9), ("services_offered", 4, 8), ("objective", 5, 8)]
    else:
        tail = [("services_offered", 3, 8), ("objective", 4, 8)]
    assert seen == [("website", 1, 9), ("business_name", 2, 9), *tail]


# The business's own Google Maps listing, as the owner confirmed it (listings.link)
CONFIRMED = {
    "place_id": "ChIJ1",
    "name": "Bright Smile Dental",
    "address": "500 University Ave, Palo Alto, CA",
    "lat": 37.4419,
    "lng": -122.143,
    "source": "Google Maps",
    "confirmed": True,
}


def test_a_business_with_a_confirmed_listing_is_not_asked_its_name_nor_offered_its_place_again(
    client, conn, fake, places_search
):
    """Ruling F2: a business whose Google Maps listing the owner confirmed starts over without a site -- its name is
    the listing's (a known fact, not asked, so no listing question is counted ahead of it), "No website" goes on to
    the services, and a name sent anyway looks nothing up: its own place is never offered again, nor open to a "None
    of these" that would reject it while it stays linked."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    on_file = {**knowledge_fixture().model_dump(), "website": None, "listing": CONFIRMED}
    business.add_knowledge(conn, "biz_1", on_file, source="owner")
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})
    places_search.found = [BRIGHT_SMILE]
    begun = call_tool(client, "start", {"text": "Family dentist in Palo Alto, more cleanings please"}, key=key)
    assert not begun["isError"], tool_text(begun)
    sid = data(begun)["session_id"]
    assert _position(begun["structuredContent"]) == ("website", 1, 6)  # the name and where it is: known
    assert consult.get(conn, "biz_1", sid)["answers"]["business_name"] == {
        "name": "Bright Smile Dental",
        "source": "known_fact",
    }
    assert _position(_answer(client, key, sid, {"website": {"choice": "none"}}, "w1")) == ("services_offered", 2, 6)
    again = _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "n1")  # sent anyway
    assert places_search.queries == [] and listings.offer(conn, "biz_1") == []
    assert consult.get(conn, "biz_1", sid)["options"]["listing"] == [] and "listing" not in again["data"]["remaining"]
    assert profile.business_listing(business.latest_knowledge(conn, "biz_1")[1])["place_id"] == "ChIJ1"


def test_a_name_changed_after_the_listing_question_leaves_its_places_as_they_were(client, conn, fake, places_search):
    """Ruling F2 (ledger deferred 17 and 18): once the listing question is answered, a name sent again looks nothing up
    -- the offer and the question's places stay as the owner answered them, never replaced under that answer by the
    places a new name finds."""
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    places_search.found = [BRIGHT_SMILE]
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "n1")
    _listing(client, key, sid, {"choice": "none"}, "l1")  # None of these
    before = consult.get(conn, biz, sid)
    places_search.found = [CARE]  # what the new name would find
    res = _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental Care"}}, "n2")
    assert places_search.queries == ["Bright Smile Dental Palo Alto"]  # the first name's lookup only
    s = consult.get(conn, biz, sid)
    assert s["options"]["listing"] == before["options"]["listing"]
    assert s["answers"]["listing"] == before["answers"]["listing"] and listings.offer(conn, biz) == []
    assert "Updated: Bright Smile Dental Care." in res["say"]


@pytest.mark.parametrize("why", ["the day's lookups spent", "no Places key"])
def test_a_name_lookup_that_did_not_run_leaves_the_places_offered_as_they_were(
    client, conn, fake, places_search, monkeypatch, why
):
    """Ruling F2 (final review M4): a lookup by the name that did not run -- the day's Places budget spent, or no key
    -- found nothing because it looked for nothing: the places start's own lookup offered stay offered, and the listing
    question asks about them next."""
    first = _homepage(conn, fake, found=[BRIGHT_SMILE])  # start's lookup offers the business
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    if why == "no Places key":
        monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    else:
        monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "1")  # start's lookup was the day's one call
    places_search.found = [CARE]
    res = _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "n1")
    assert places_search.queries == []
    q = res["next"]["question"]
    assert q["id"] == "listing" and q["options"][0]["label"].startswith("Bright Smile Dental, 500 University Ave")
    assert [c["place_id"] for c in listings.offer(conn, biz)] == ["ChIJ1"]


def test_a_business_whose_site_was_read_is_offered_it_again_when_it_starts_over_with_plain_words(
    client, conn, fake, places_none, crawled
):
    """Ruling F3: the site read at the business's first start stays on its profile when it starts over with words that
    name none, so the website question offers it as one tap ("Yes: <host>", Q5) ahead of "No website"."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    begun = call_tool(client, "start", {"text": "Family dentist in Palo Alto", "website_url": SITE}, key=key)
    assert not begun["isError"], tool_text(begun)
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})  # the words alone name no site
    res = call_tool(client, "start", {"text": "Family dentist in Palo Alto, I want more implant patients"}, key=key)
    assert not res["isError"], tool_text(res)
    q = res["structuredContent"]["next"]["question"]
    assert q["id"] == "website" and q["options"] == [
        {"id": "site_known", "label": "Yes: brightsmile.example", "url": SITE},
        {"id": "none", "label": consult.NO_WEBSITE},
    ]
    assert business.latest_knowledge(conn, "biz_1")[1]["website"] == SITE


def test_a_name_from_plain_words_is_not_vouched_for_by_a_site_read_before(client, conn, fake, places_none, crawled):
    """Ruling R0-k (A3p-5 batch 2 Task 0, item c): the name a reading of the business's site gave is vouched for; when
    the business starts over with plain words, ruling F3 keeps the site on its profile, but the new reading's name --
    the model's label from the words -- is not vouched for by it: the first turn says "You run a ...", not the label."""
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="claimed")
    begun = call_tool(client, "start", {"text": "Family dentist in Palo Alto", "website_url": SITE}, key=key)
    assert not begun["isError"], tool_text(begun)
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert k["name_source"] == "site" and plans.name_for(k) == "Bright Smile Dental"
    assert data(begun)["understanding"]["name"] == "Bright Smile Dental"
    fake.knowledge = fake.knowledge.model_copy(update={"website": None, "name": "Palo Alto family dentist"})
    res = call_tool(client, "start", {"text": "Family dentist in Palo Alto, more implant patients"}, key=key)
    assert not res["isError"], tool_text(res)
    k = business.latest_knowledge(conn, "biz_1")[1]
    assert (k["website"], k["name"], k["name_source"]) == (SITE, "Palo Alto family dentist", "words")
    assert plans.name_for(k) is None and data(res)["understanding"]["name"] is None
    assert res["structuredContent"]["say"].startswith("I can build that for you. You run a family dentist.")


@pytest.mark.parametrize(
    "words",
    [
        "No website\n\nJane Doe\nBright Smile Dental\nfacebook.com/brightsmile",
        "No, we don't have one\n\nJane\nBook online: https://calendly.com/jane",
    ],
    ids=["a social page in the signature", "a booking link in the signature"],
)
def test_an_emailed_no_above_a_signature_answers_no_website_and_reads_nothing(client, conn, fake, crawled, words):
    """Ruling F4 (an e-mail owner can only type, and the mail channel strips only a "-- " signature): the no above the
    signature answers the website question -- no SOCIAL for a page in the signature, asked forever, and no third
    party's page crawled and read as the business's -- and the name is next."""
    first = _homepage(conn, fake)
    res = _answer(client, first["key"], first["session_id"], {"website": {"other": words}}, "w1")
    assert crawled.urls == [] and res["say"] == consult.question_sentence(res["next"])
    assert _position(res) == ("business_name", 2, 9)
    s = consult.get(conn, first["business_id"], first["session_id"])
    assert s["answers"]["website"] == {"none": True, "raw": words, "source": "other"}


def test_a_social_page_is_asked_about_once_and_a_second_one_answers_no_website(client, conn, fake, crawled):
    """Ruling F4: a page on a social or listing site counts a retry; a second one keeps the page (the session's known
    fact `social`, for A3p-6) and answers the question as "No website", so the loop ends and the name is next."""
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    once = _answer(client, key, sid, {"website": {"other": "facebook.com/brightsmile"}}, "s1")
    assert once["say"].startswith(consult.SOCIAL.format(host="facebook.com")) and _position(once) == ("website", 1, 9)
    assert consult.get(conn, biz, sid)["answers"][consult.RETRY_KEY] == {"website": 1}
    page = "https://www.facebook.com/brightsmile"
    twice = _answer(client, key, sid, {"website": {"other": page}}, "s2")
    assert _position(twice) == ("business_name", 2, 9) and crawled.urls == []
    s = consult.get(conn, biz, sid)
    assert s["answers"]["website"] == {"social": page, "none": True, "raw": page, "source": "other"}
    assert s["known_facts"]["social"] == page


def test_a_greeting_alone_is_asked_once_more_then_answers_no_website(client, conn, fake, crawled):
    """Ruling R0-m (A3p-5 batch 2 Task 0, item b): an e-mailed "Hi," with nothing after it is an empty answer, asked
    once more; sent again it answers the question as no website (the retry rule every free-text answer follows), so the
    name is next -- never asked without end."""
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    once = _answer(client, key, sid, {"website": {"other": "Hi,"}}, "g1")
    assert once["say"] == f"Send a choice or a few words. {consult.question_sentence(once['next'])}"
    assert _position(once) == ("website", 1, 9)
    twice = _answer(client, key, sid, {"website": {"other": "Hi,"}}, "g2")
    assert _position(twice) == ("business_name", 2, 9) and crawled.urls == []
    assert consult.get(conn, biz, sid)["answers"]["website"] == {
        "note": "Hi,",
        "none": True,
        "raw": "Hi,",
        "source": "other",
    }


def test_a_no_that_names_another_site_reads_it_in_place_of_the_one_offered(client, conn, fake, crawled, monkeypatch):
    """Ruling R0-m (A3p-5 batch 2 Task 0, item a): an owner offered "Yes: brightsmile.example" types "No, it's
    newsite.com": the new site is read as a typed address is -- one crawl, one reading, the session started again in
    place -- and the call counts toward the day's model calls."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = tools_start.begin_anonymous("Bright Smile Dental in Palo Alto, brightsmile dot example", ip="198.51.100.9")
    d = first["data"]
    assert first["next"]["question"]["options"][0]["label"] == "Yes: brightsmile.example"
    readings = len(fake.extract_calls)
    fake.knowledge = knowledge_fixture().model_copy(update={"website": "https://newsite.com"})  # what the new site says
    words = "No, it's newsite.com"
    res = _answer(client, d["draft_key"], d["session_id"], {"website": {"other": words}}, "n1")
    assert crawled.urls == ["https://newsite.com"] and len(fake.extract_calls) == readings + 1
    assert res["say"].startswith("I read newsite.com") and res["data"]["session_id"] == d["session_id"]
    assert consult.get(conn, d["business_id"], d["session_id"])["answers"]["website"] == {
        "url": "https://newsite.com",
        "raw": words,
        "source": "other",
        "read": True,
    }
    assert business.latest_knowledge(conn, d["business_id"])[1]["website"] == "https://newsite.com"
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'n1'")
    assert flag.fetchone()["f"] == "true"


def test_a_typed_yes_reads_the_site_the_reading_saw(client, conn, fake, crawled, monkeypatch):
    """Ruling F4: with the site the reading saw offered as "Yes: <host>", the owner's typed yes reads it as the tap
    does -- one crawl and one reading, counted toward the day's model calls."""
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [])
    first = tools_start.begin_anonymous("Bright Smile Dental in Palo Alto, brightsmile dot example", ip="198.51.100.9")
    d = first["data"]
    res = _answer(client, d["draft_key"], d["session_id"], {"website": {"other": "Yes, that's it"}}, "y1")
    assert crawled.urls == [SITE] and res["say"].startswith("I read brightsmile.example")
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 'y1'")
    assert flag.fetchone()["f"] == "true"


def test_a_skipped_name_looks_nothing_up_and_sets_nothing(client, conn, fake, places_search):
    """Ruling F5 (W4's "I would rather not say"): a skipped name runs no Google Maps lookup and sets nothing -- no
    known fact a later lookup would prefer, no profile name -- and the listing question it could have brought drops
    out of the count."""
    fake.knowledge = fake.knowledge.model_copy(update={"name": ""})
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    version = business.latest_knowledge(conn, biz)[0]
    places_search.found = [BRIGHT_SMILE]
    res = _answer(client, key, sid, {"business_name": {"other": "I would rather not say"}}, "n1")
    assert places_search.queries == [] and _position(res) == ("services_offered", 3, 8)
    s = consult.get(conn, biz, sid)
    assert s["answers"]["business_name"] == {"skipped": True, "raw": "I would rather not say", "source": "other"}
    assert "business_name" not in s["known_facts"] and business.latest_knowledge(conn, biz)[0] == version


def test_a_skipped_services_answer_calls_no_model_and_is_not_counted(client, conn, fake, places_none, monkeypatch):
    """Ruling F5: "Prefer not to say" to what the business offers is a skip, read without the model -- no generator, no
    daily limit checked or spent -- and nothing joins the profile."""
    first = _homepage(conn, fake)
    key, sid, biz = first["key"], first["session_id"], first["business_id"]
    _answer(client, key, sid, {"website": {"choice": "none"}}, "w1")
    _answer(client, key, sid, {"business_name": {"other": "Bright Smile Dental"}}, "n1")
    monkeypatch.setattr(llm, "generator", lambda settings=None: pytest.fail("no model for a skip"))
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY_DRAFT", 0)  # checked, the limit would refuse
    res = _answer(client, key, sid, {"services_offered": {"other": "Prefer not to say"}}, "s1")
    assert res["next"]["question"]["id"] == "objective" and "Added to your services" not in res["say"]
    s = consult.get(conn, biz, sid)
    assert s["answers"]["services_offered"] == {"skipped": True, "raw": "Prefer not to say", "source": "other"}
    flag = conn.execute("SELECT args_redacted->>'free_text' AS f FROM agent_action WHERE idempotency_key = 's1'")
    assert flag.fetchone()["f"] == "false"


# ---------------------------------------------------------------- the "Your ads" page's seam (spec 2026-10-06, Y12)

WORDS_H = ["One Headline Here", "Two Headline Here", "Three Headline Here"]
WORDS_D = ["The first description of the ad.", "The second description of the ad."]


def test_adjust_prepare_takes_the_business_id_and_swaps_a_platforms_words_before_the_rebuild(client, conn, fake):
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    ctx = tools_consult.adjust_prepare(
        conn, "biz_1", r["plan_id"], channel="google_search", headlines=WORDS_H, descriptions=WORDS_D
    )
    prev, session, edits, _knowledge, (sections, _basis, _notes, _goal) = ctx
    assert prev["id"] == r["plan_id"] and edits == {} and session["status"] == "planned"
    google = next(c for c in sections["ads"]["channels"] if c["channel"] == "google_search")
    assert google["headlines"] == WORDS_H and google["descriptions"] == WORDS_D
    assert google["monthly_usd"] == 2000.0  # the words move no money
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1  # prepare writes nothing


def test_adjust_commit_writes_the_version_from_the_context_and_marks_the_session(client, conn, fake):
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    ctx = tools_consult.adjust_prepare(
        conn, "biz_1", r["plan_id"], channel="google_search", headlines=WORDS_H, descriptions=WORDS_D
    )
    plan = tools_consult.adjust_commit(conn, "biz_1", ctx)
    assert plan["version"] == 2 and plan["status"] == "draft" and plan["diff_from"] == r["plan_id"]
    assert plan["sections"]["ads"]["channels"][0]["headlines"] == WORDS_H
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (r["plan_id"],)).fetchone()["status"] == "superseded"
    assert conn.execute("SELECT plan_id FROM consult_session").fetchone()["plan_id"] == plan["id"]


def test_the_words_need_a_channel_and_the_adjust_tool_does_not_take_them(client, conn, fake):
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    with pytest.raises(ToolError, match="which channel"):
        tools_consult.adjust_prepare(conn, "biz_1", r["plan_id"], headlines=WORDS_H, descriptions=WORDS_D)
    res = call_tool(
        client,
        "adjust",
        {"plan_id": r["plan_id"], "idempotency_key": "w1", "channel": "google_search", "headlines": WORDS_H},
        key=fake_key(conn),
    )
    assert res["isError"]  # FastMCP refuses an argument the tool does not declare


def test_words_for_a_platform_the_plan_does_not_fund_are_refused_before_anything_is_swapped(client, conn, fake):
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    with pytest.raises(ToolError, match="is not in your plan"):
        tools_consult.adjust_prepare(
            conn, "biz_1", r["plan_id"], channel="newsbreak", headlines=WORDS_H, descriptions=WORDS_D
        )


def test_confirm_action_is_the_review_only_for_a_first_launch(monkeypatch):
    """L1 review M2 (D6): the review gates a first launch only -- a newer version under an approved plan approves and
    launches at once even with AGENT_LAUNCH_REVIEW on, so its yes stays "Confirm and launch", never "Confirm this
    plan"."""
    from agent import config

    off = config.load()
    assert tools_consult.confirm_action(off, first=True) == tools_consult.CONFIRM_ACTION
    assert tools_consult.confirm_action(off, first=False) == tools_consult.CONFIRM_ACTION
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    on = config.load()
    assert tools_consult.confirm_action(on, first=True) == tools_consult.CONFIRM_REVIEW_ACTION
    assert tools_consult.confirm_action(on, first=True)["label"] == "Confirm this plan"
    assert tools_consult.confirm_action(on, first=False) == tools_consult.CONFIRM_ACTION
    assert tools_consult.confirm_action(on, first=False)["label"] == "Confirm and launch"
