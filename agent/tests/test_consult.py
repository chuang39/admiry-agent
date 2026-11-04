import math
import re
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent import consult, envelope
from agent.tests.conftest import ACCOUNT_JARGON, JARGON
from agent.tests.fixtures import READ_SITE, FakeGenerator, knowledge_fixture, plan_draft_fixture

KNOWLEDGE = knowledge_fixture().model_dump()
LISTING = {
    "address": "500 University Ave, Palo Alto, CA",
    "lat": 37.44,
    "lng": -122.16,
    "place_id": "ChIJ1",
    "source": "Google Maps",
}
# The questions every tree asks first (plan 2026-10-05-agent-a3p-5-1, Q1); a session holding READ_SITE, the website read
# at start, asks the trade's questions next, as every session did before the website question
COMMON = ["website", "business_name", "services_offered"]
SITE = "https://brightsmile.example"


def _q(session: dict, qid: str) -> dict:
    """The question `qid` among those the session asks."""
    return next(q for q in consult.questions(session) if q["id"] == qid)


def test_every_tree_is_short_and_owner_worded():
    """Short: three to five questions of the trade's own, and before them the business on Google Maps (plan
    2026-10-03-agent-listing-confirm), asked only when start's lookup found places close to it, which are its options."""
    for name in ("dentists", "home_services", "attorneys", "fallback"):
        tree = consult.load_tree(name)
        first, *own = tree["questions"]
        assert (first["id"], first["input"], first["options_from"], first["options"]) == (
            "listing",
            "single",
            "listing",
            [],
        ), name
        assert first["prompt"] == "Is your business one of these on Google Maps?" and first["known_fact"] is None
        assert 3 <= len(own) <= 5, name
        for q in own:
            assert q.get("options_from") != "listing", q["id"]
            assert q["input"] in ("single", "multi", "amount", "radius", "address")
            assert q.get("allow_other", True) is True
            assert len(q["options"]) >= 3 or q["input"] in ("amount", "address"), q["id"]
        for q in tree["questions"]:
            assert not JARGON.search(q["prompt"]) and all(not JARGON.search(o["label"]) for o in q["options"])
        assert tree["defaults"]["customer_value_usd"] > 0 and tree["defaults"]["footnote_noun"]
    assert consult.tree_for("dentists") == "dentists" and consult.tree_for("physicians") == "fallback"


def test_options_are_built_from_the_business_services_and_frozen():
    tree = consult.load_tree("dentists")
    options = consult.build_options(tree, KNOWLEDGE)
    labels = [o["label"] for o in options["service"]]
    assert labels[:2] == ["Invisalign", "Cleanings"]  # the site's own services first
    assert "Cleaning" not in labels  # the default "Cleaning" is the site's "Cleanings": shown once
    assert labels == ["Invisalign", "Cleanings", "General check-ups", "Implants", "Kids dentistry", "Emergency"]
    assert options["service"][0]["id"] == "svc_invisalign"
    assert len(options["objective"]) == 5 and options["customer_value"][-1]["id"] == "estimate"


def test_known_facts_and_the_opening_text_skip_questions():
    tree = consult.load_tree("dentists")
    pre = consult.preanswers(
        tree, {"objective": "bookings", "radius_miles": 5}, KNOWLEDGE, "I want patients within 5 miles"
    )
    assert pre["objective"] == {"choice": "bookings", "raw": "bookings", "source": "known_fact"}
    assert pre["radius"] == {"miles": 5.0, "raw": 5, "source": "known_fact"}
    pre2 = consult.preanswers(tree, {}, KNOWLEDGE, "everyone within 3 miles of my clinic")
    assert pre2["radius"]["miles"] == 3.0 and pre2["radius"]["source"] == "known_fact"
    assert consult.preanswers(tree, {"objective": "not-an-option"}, KNOWLEDGE, "") == {}


def test_the_location_question_is_pre_answered_by_a_business_listing_and_not_counted():
    knowledge = {**KNOWLEDGE, "listing": LISTING}
    tree = consult.load_tree("dentists")
    out = consult.preanswers(tree, {}, knowledge, "", can_place=True)
    assert out["location"] == {
        "address": "500 University Ave, Palo Alto, CA",
        "lat": 37.44,
        "lng": -122.16,
        "source": "known_fact",
    }
    session = {"tree": "dentists", "options": consult.build_options(tree, knowledge), "answers": out}
    # the website, the name and the services, the trade's five questions and the listing question the name may still
    # find places for (ruling F1): nine, one answered by the listing's point (a lookup's, never confirmed: no name)
    assert consult.next_question(session)["question"]["count"] == 8


def test_the_listing_question_exists_only_for_a_session_offered_places():
    tree = consult.load_tree("dentists")
    offered = [
        {
            "id": "gm_1",
            "place_id": "ChIJ1",
            "name": "Bright Smile Dental",
            "address": "500 University Ave, Palo Alto, CA 94301, USA",
        }
    ]
    options = consult.build_options(tree, {}, candidates=offered)
    assert options["listing"] == [
        {
            "id": "gm_1",
            "label": "Bright Smile Dental, 500 University Ave, Palo Alto, CA 94301",
            "sublabel": "Google Maps",
        },
        consult.LISTING_NONE,
    ]
    assert consult.build_options(tree, {})["listing"] == []
    # the website read at start: the trade's questions are next, the listing question first among them
    asked = {"tree": "dentists", "answers": dict(READ_SITE), "options": options}
    old = {
        "tree": "dentists",
        "answers": dict(READ_SITE),
        "options": {k: v for k, v in options.items() if k != "listing"},
    }
    assert [q["id"] for q in consult.questions(asked)][:2] == ["website", "listing"]
    assert "listing" not in [q["id"] for q in consult.questions(old)]  # a session from before: never an empty question
    assert consult.next_question(asked)["question"]["id"] == "listing"
    assert consult.next_question(old)["question"]["count"] == consult.next_question(asked)["question"]["count"] - 1


def test_a_listing_answer_is_a_pick_of_the_places_or_none():
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    tree = consult.load_tree("dentists")
    offered = [{"id": "gm_1", "place_id": "ChIJ1", "name": "Bright Smile Dental", "address": "500 University Ave"}]
    s = {"tree": "dentists", "answers": {}, "options": consult.build_options(tree, {}, candidates=offered)}
    q = _q(s, "listing")
    assert consult.parse(s, q, {"choice": "gm_1"}, fake)[0]["choice"] == "gm_1"
    assert consult.parse(s, q, {"choice": "none"}, fake)[0]["choice"] == "none"
    assert consult.parse(s, q, {"choice": "gm_9"}, fake) == (None, consult.LISTING_BY_CHOICE)


# Two places offered (listings.numbered's shape), for the owner's words
OFFERED = [
    {"id": "gm_1", "place_id": "ChIJ1", "name": "Bright Smile Dental", "address": "500 University Ave, Palo Alto, CA"},
    {"id": "gm_2", "place_id": "ChIJ2", "name": "Smile Studio", "address": "1 Main St, Palo Alto, CA 94301, USA"},
]


def _offered_session(offered=OFFERED, answers: dict | None = None) -> dict:
    """A dental session offering `offered`, its website read at start: the listing question is the next one asked."""
    options = consult.build_options(consult.load_tree("dentists"), {}, candidates=offered)
    return {"tree": "dentists", "answers": {**READ_SITE, **(answers or {})}, "options": options}


@pytest.mark.parametrize(
    ("words", "choice"),
    [
        ("1", "gm_1"),
        ("2", "gm_2"),
        ("first", "gm_1"),
        ("The second one", "gm_2"),
        ("2nd", "gm_2"),
        ("option 2", "gm_2"),
        ("It's the first.", "gm_1"),
        ("We are Smile Studio", "gm_2"),
        ("bright smile dental, on University", "gm_1"),
        ("none", "none"),
        ("No", "none"),
        ("neither", "none"),
        ("not mine", "none"),
        ("None of these.", "none"),
        ("We're not listed", "none"),
        ("not on Google", "none"),
        ("neither of them", "none"),
        # a yes beside a number or a name says nothing more (final review A2/B I1)
        ("Yes, the first one", "gm_1"),
        ("yeah 2", "gm_2"),
        ("Yes, Smile Studio", "gm_2"),
        ("2, thanks", "gm_2"),
    ],
)
def test_the_owners_words_pick_a_place_or_none_without_the_model(words, choice):
    """Plan 2026-10-03-agent-listing-confirm (the controller's ruling on the global constraint): the model never reads a
    place a lookup found, so words are read here: a number or an ordinal among the places offered, an offered place's
    name in the words, or a way of saying none of these. Support asks are not looked for in them."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _offered_session()
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": words}, fake, support=True) == (
        {"choice": choice, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


@pytest.mark.parametrize(
    "words",
    [
        "3",  # two places offered: there is no third
        "the fourth",
        "the smile one",  # part of a name is no name
        "Bright Smile Dental or Smile Studio",  # two names: unclear
        "no, the second one",
        "not Bright Smile Dental",  # a name the words say no to is never picked
        "I would like to talk to a person",
        "yes",  # two places offered: a yes says not which (final review A2/B I1)
        "that's it",
    ],
)
def test_words_that_pick_nothing_clearly_are_asked_again_then_link_nothing(words):
    """Unclear words are asked about again (LISTING_BY_CHOICE); unclear a second time, the question is answered with
    no place picked (`skipped`) -- never a place the owner did not pick, and never "None of these", which they did not
    say either (final review A2/B I2)."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _offered_session()
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": words}, fake, support=True) == (None, consult.LISTING_BY_CHOICE)
    retried = _offered_session(answers={consult.RETRY_KEY: {"listing": 1}})
    assert consult.parse(retried, q, {"other": words}, fake) == (
        {"skipped": True, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


@pytest.mark.parametrize(
    "words",
    [
        "yes",
        "Yes.",
        "yeah",
        "Yep!",
        "yup",
        "correct",
        "Right",
        "sure",
        "That's it",
        "that's us",
        "That's me.",
        "that's mine",
        "that one",
        "This one",
        "it is",
        "Yes it is",
        "Yes, that's us!",
        "yes please",
        "Yes, thanks.",
        "Yes, the first one",
        "yes, Bright Smile Dental",
    ],
)
def test_a_yes_picks_the_one_place_offered(words):
    """Final review A2/B I1: "Is your business one of these on Google Maps?" is a yes/no question when one place is
    offered, and a yes -- the whole answer, or with "please" or "thanks" after it -- picks that place."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _offered_session(OFFERED[:1])
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": words}, fake) == ({"choice": "gm_1", "raw": words, "source": "other"}, None)
    assert fake.read_calls == []


@pytest.mark.parametrize("words", ["yes, the second one", "yes it is not", "that's not it", "yes but not that one"])
def test_a_yes_that_says_more_than_yes_is_read_as_its_words(words):
    """With one place offered, a yes that goes on to say something else is not taken for the place."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _offered_session(OFFERED[:1])
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": words}, fake) == (None, consult.LISTING_BY_CHOICE)


def test_a_name_inside_another_offered_name_gives_way_to_the_longer_one():
    """Carried item 5: "Smile Dental" and "Bright Smile Dental" both offered, the words "Bright Smile Dental" name the
    longer one -- the shorter is in them only as part of it."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    short = {"id": "gm_1", "place_id": "ChIJ3", "name": "Smile Dental", "address": "9 Elm St, Palo Alto, CA"}
    s = _offered_session([short, {**OFFERED[0], "id": "gm_2"}])
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": "We are Bright Smile Dental"}, fake)[0]["choice"] == "gm_2"
    assert consult.parse(s, q, {"other": "Smile Dental"}, fake)[0]["choice"] == "gm_1"


def test_the_listing_question_sentence_keeps_each_place_apart():
    """Final review B M1: a place's label is its name and address, commas and all; the first turn's `say` sets the
    places apart with semicolons so an assistant that shows only the text reads where one ends."""
    s = _offered_session()
    assert consult.question_sentence(consult.next_question(s)) == (
        "Is your business one of these on Google Maps? Options: Bright Smile Dental, 500 University Ave, Palo Alto, "
        "CA; Smile Studio, 1 Main St, Palo Alto, CA 94301; None of these — or answer in your own words."
    )


def test_two_places_of_one_name_are_told_apart_only_by_their_place_in_the_list():
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    twins = [OFFERED[0], {**OFFERED[0], "id": "gm_2", "place_id": "ChIJ9", "address": "9 Elm St, Palo Alto, CA"}]
    s = _offered_session(twins)
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": "Bright Smile Dental"}, fake) == (None, consult.LISTING_BY_CHOICE)
    assert consult.parse(s, q, {"other": "the second"}, fake)[0]["choice"] == "gm_2"


def test_listing_words_too_long_or_empty_are_refused():
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _offered_session()
    q = _q(s, "listing")
    assert consult.parse(s, q, {"other": "x" * 501}, fake) == (None, "Keep it under 500 characters.")
    assert consult.parse(s, q, {"other": "  "}, fake) == (None, consult.LISTING_BY_CHOICE)
    assert fake.read_calls == []


def test_the_location_question_is_skipped_where_the_server_cannot_place_addresses():
    tree = consult.load_tree("dentists")
    out = consult.preanswers(tree, {}, KNOWLEDGE, "", can_place=False)
    assert out["location"] == {"skipped": True, "source": "known_fact"}


def test_the_location_question_is_asked_when_there_is_no_point_and_the_server_can_place():
    tree = consult.load_tree("dentists")
    assert "location" not in consult.preanswers(tree, {}, KNOWLEDGE, "", can_place=True)
    # a listing without a point, and an address an assistant already knew (start looked it up), centre nothing
    pointless = {**KNOWLEDGE, "listing": {**LISTING, "lat": None, "lng": None}}
    assert "location" not in consult.preanswers(tree, {"address": "500 University Ave"}, pointless, "", can_place=True)


@pytest.fixture
def session_with_answers():
    """A home-services consultation holding `answers`, its options frozen from the dental fixture's knowledge."""
    options = consult.build_options(consult.load_tree("home_services"), KNOWLEDGE)
    return lambda answers: {"tree": "home_services", "options": options, "known_facts": {}, "answers": answers}


def test_the_radius_prompt_names_the_place(session_with_answers):
    session = session_with_answers(
        {"location": {"address": "123 Main St, Houston, TX", "lat": 29.7, "lng": -95.3, "source": "other"}}
    )
    q = next(q for q in consult.load_tree("home_services")["questions"] if q["id"] == "radius")
    assert consult.prompt_for(q, session) == "How far around 123 Main St, Houston, TX should the ads reach?"
    bare = session_with_answers({"location": {"skipped": True, "source": "other"}})
    assert consult.prompt_for(q, bare) == "How far around your business should the ads reach?"
    clinic = next(q for q in consult.load_tree("dentists")["questions"] if q["id"] == "radius")
    assert consult.prompt_for(clinic, session_with_answers({})) == "How far around the clinic should the ads reach?"


def test_a_question_without_options_is_said_as_an_open_question():
    q = next(q for q in consult.load_tree("dentists")["questions"] if q["id"] == "location")
    nxt = envelope.question(id="location", index=4, count=5, prompt=q["prompt"], input="address", options=[])
    assert consult.question_sentence(nxt) == f"{q['prompt']} Answer in your own words."


def test_clean_known_facts_coerces_numbers_and_bounds_text():
    assert consult.clean_known_facts(
        {"monthly_budget_usd": "$2,000", "radius_miles": 5, "customer_value_usd": "not-a-number"}
    ) == {"monthly_budget_usd": 2000.0, "radius_miles": 5.0}
    assert consult.clean_known_facts({"offer": {"a": 1}}) == {}  # a non-str value is dropped, not raised
    assert consult.clean_known_facts({"offer": "x" * 800})["offer"] == "x" * 500  # clipped like other free text
    assert consult.clean_known_facts({"objective": "  bookings  "}) == {"objective": "bookings"}
    assert consult.clean_known_facts({"monthly_budget_usd": 0, "radius_miles": -5, "customer_value_usd": True}) == {}
    assert consult.clean_known_facts({"unknown_key": "x"}) == {}
    assert consult.clean_known_facts(None) == {}


def test_next_question_counts_only_what_the_owner_is_asked():
    tree = consult.load_tree("dentists")
    options = consult.build_options(tree, KNOWLEDGE)
    # radius pre-answered from known_facts: 4 of the 5 questions are actually asked, starting at 1 (the knowledge has
    # no point on the map, so where the ads centre is one of them); the website read at start is a pre-answer too
    s = {
        "tree": "dentists",
        "options": options,
        "answers": {**READ_SITE, "radius": {"miles": 5.0, "raw": 5, "source": "known_fact"}},
    }
    nxt = consult.next_question(s)
    assert (nxt["question"]["id"], nxt["question"]["index"], nxt["question"]["count"]) == ("objective", 1, 4)
    # one genuine answer on top of the pre-answer: the next one asked is #2 of the same 4
    s["answers"]["objective"] = {"choice": "bookings", "raw": "bookings", "source": "choice"}
    nxt = consult.next_question(s)
    assert (nxt["question"]["id"], nxt["question"]["index"], nxt["question"]["count"]) == ("service", 2, 4)
    # a retry-count entry (RETRY_KEY) is bookkeeping, not an answer: it must not inflate either number
    s["answers"][consult.RETRY_KEY] = {"service": 1}
    nxt = consult.next_question(s)
    assert (nxt["question"]["id"], nxt["question"]["index"], nxt["question"]["count"]) == ("service", 2, 4)


def test_corrections_match_whole_words_in_any_case():
    tree = consult.load_tree("dentists")
    assert consult.corrections(tree, "I want EVERYONE interested in a dentist")
    assert consult.corrections(tree, "all people who need Invisalign")
    assert consult.corrections(tree, "more patients for my clinic") == []


def test_session_rows_pending_and_ready(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    tree = consult.load_tree("dentists")
    options = consult.build_options(tree, KNOWLEDGE)
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=options,
        answers=dict(READ_SITE),  # the website read at start: the trade's questions wait
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert s["status"] == "open" and [q["id"] for q in consult.pending(s)] == [
        "objective",
        "service",
        "customer_value",
        "location",
        "radius",
    ]
    nxt = consult.next_question(s)
    assert nxt["kind"] == "question" and nxt["question"]["index"] == 1 and nxt["question"]["count"] == 5
    assert nxt["question"]["options"] == options["objective"]
    s = consult.record(conn, s, {"objective": {"choice": "bookings", "raw": "bookings", "source": "choice"}})
    assert s["status"] == "open" and consult.next_question(s)["question"]["id"] == "service"
    s = consult.record(
        conn,
        s,
        {
            "service": {"choice": "svc_invisalign", "raw": "svc_invisalign", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "raw": "1000_plus", "source": "choice"},
            "radius": {"miles": 5.0, "raw": "5", "source": "choice"},
        },
    )
    assert s["status"] == "open" and consult.next_question(s)["question"]["id"] == "location"
    s = consult.record(conn, s, {"location": {"skipped": True, "raw": "later", "source": "other"}})
    assert s["status"] == "ready" and consult.next_question(s) is None
    assert consult.open_for(conn, "biz_1")["id"] == s["id"]  # ready is still surfaced, until planned
    facts = consult.facts(s, tree)
    assert facts == {
        "objective": "bookings",
        "service": "Invisalign",
        "services": ["Invisalign"],
        "customer_value_usd": 1500.0,  # "$1,000 and up" stands for $1,500
        "customer_value_source": "owner",
        "radius_miles": 5.0,
        "offer": None,
    }
    assert consult.value_for_band(tree, "estimate") == (2500.0, "benchmark")


def test_only_one_open_session_per_business(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    tree = consult.load_tree("fallback")
    kw = {
        "tree": "fallback",
        "locale": "en-US",
        "opening_text": "x",
        "known_facts": {},
        "options": consult.build_options(tree, {}),
        "answers": {},
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }
    a = consult.create(conn, "biz_1", **kw)
    consult.supersede_open(conn, "biz_1")
    b = consult.create(conn, "biz_1", **kw)
    assert (
        consult.get(conn, "biz_1", a["id"])["status"] == "superseded"
        and consult.open_for(conn, "biz_1")["id"] == b["id"]
    )
    with pytest.raises(psycopg.errors.UniqueViolation):  # the partial unique index refuses a second open row
        consult.create(conn, "biz_1", **kw)


def test_forget_centre_drops_only_a_centre_read_from_a_listing(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=consult.build_options(consult.load_tree("dentists"), {}),
        answers={
            "objective": {"choice": "bookings", "source": "choice"},
            "location": {"address": "500 University Ave", "lat": 1.0, "lng": 2.0, "source": "known_fact"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert consult.forget_centre(conn, "biz_1") is True
    fresh = consult.get(conn, "biz_1", s["id"])
    assert "location" not in fresh["answers"] and fresh["answers"]["objective"]["choice"] == "bookings"
    assert fresh["status"] == "open"
    conn.execute(
        "UPDATE consult_session SET answers = answers || %s WHERE id = %s",
        (Jsonb({"location": {"address": "12 Elm St", "lat": 1.0, "lng": 2.0, "source": "other"}}), s["id"]),
    )
    assert consult.forget_centre(conn, "biz_1") is False  # the owner's own answer stays


def test_forget_centre_opens_a_ready_consultation_again_and_has_nothing_to_drop_without_one(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    assert consult.forget_centre(conn, "biz_1") is False  # no consultation at all
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=consult.build_options(tree, KNOWLEDGE),
        answers={
            **READ_SITE,
            "objective": {"choice": "bookings", "source": "choice"},
            "service": {"choice": "svc_invisalign", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "source": "choice"},
            "radius": {"miles": 5.0, "source": "choice"},
            "location": {"address": "500 University Ave", "lat": 1.0, "lng": 2.0, "source": "known_fact"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert s["status"] == "ready"
    assert consult.forget_centre(conn, "biz_1") is True
    again = consult.get(conn, "biz_1", s["id"])
    assert again["status"] == "open" and "location" not in again["answers"]
    assert consult.forget_centre(conn, "biz_1") is False  # nothing left to drop


def test_the_service_question_takes_several_picks():
    for name in ("dentists", "home_services", "attorneys", "fallback"):
        q = next(q for q in consult.load_tree(name)["questions"] if q["id"] == "service")
        assert q["input"] == "multi", name


def test_several_services_read_as_one_phrase_and_a_list():
    tree = consult.load_tree("dentists")
    options = consult.build_options(tree, KNOWLEDGE)
    session = {"options": options, "known_facts": {}, "answers": {}}
    pick = lambda *ids: {**session, "answers": {"service": {"choice": list(ids), "raw": list(ids), "source": "choice"}}}
    one = consult.facts(pick("svc_invisalign"), tree)
    assert one["service"] == "Invisalign" and one["services"] == ["Invisalign"]
    two = consult.facts(pick("svc_implants", "svc_invisalign"), tree)
    assert two["service"] == "Invisalign and Implants"  # in the order the options are shown
    assert two["services"] == ["Invisalign", "Implants"]
    three = consult.facts(pick("svc_invisalign", "svc_cleanings", "svc_implants"), tree)
    assert three["service"] == "Invisalign, Cleanings and Implants"
    legacy = consult.facts({**session, "answers": {"service": {"choice": "svc_invisalign", "source": "choice"}}}, tree)
    assert legacy["service"] == "Invisalign" and legacy["services"] == [
        "Invisalign"
    ]  # a stored single pick still reads
    note = consult.facts({**session, "answers": {"service": {"note": "teeth whitening", "source": "other"}}}, tree)
    assert note["service"] == "teeth whitening" and note["services"] == ["teeth whitening"]
    assert consult.facts(session, tree)["services"] == []


def test_a_multi_question_sentence_says_more_than_one_may_be_picked():
    """Up to three (consult.MAX_LEAD_SERVICES), as `parse` takes them (walk fixes 1, Task 3's review)."""
    nxt = {"question": {"prompt": "Which service?", "input": "multi", "options": [{"label": "A"}, {"label": "B"}]}}
    assert consult.question_sentence(nxt) == (
        "Which service? Options: A, B — pick up to three, or answer in your own words."
    )
    nxt["question"]["input"] = "single"
    assert consult.question_sentence(nxt) == "Which service? Options: A, B — or answer in your own words."


def _question(tree: dict, qid: str) -> dict:
    """The tree's question `qid` (the listing question comes first: no other question has a fixed place but the last)."""
    return next(q for q in tree["questions"] if q["id"] == qid)


def _session(tree: dict, knowledge: dict, answers: dict | None = None) -> dict:
    return {
        "tree": tree["name"],
        "options": consult.build_options(tree, knowledge),
        "known_facts": {},
        "answers": answers or {},
    }


def test_value_bands_follow_what_one_customer_of_this_business_is_worth():
    tree = consult.load_tree("fallback")
    florist = consult.build_options(tree, {**KNOWLEDGE, "typical_customer_value_usd": 60})["customer_value"]
    assert [o["label"] for o in florist] == [
        "Under $20",
        "$20 to 50",
        "$50 to 100",
        "$100 to 500",
        "$500 and up",
        "Not sure, estimate for me",
    ]
    assert florist[-1] == {"id": "estimate", "label": "Not sure, estimate for me", "usd": 60.0}
    salon = consult.build_options(tree, {**KNOWLEDGE, "typical_customer_value_usd": 400})["customer_value"]
    assert [o["label"] for o in salon][:4] == ["Under $300", "$300 to 1,000", "$1,000 to 3,000", "$3,000 and up"]
    dentists = consult.load_tree("dentists")
    tree_own = [o["id"] for o in _question(dentists, "customer_value")["options"]]
    for k in (
        KNOWLEDGE,
        {**KNOWLEDGE, "typical_customer_value_usd": 2500},
        {**KNOWLEDGE, "typical_customer_value_usd": -3},
    ):
        assert [
            o["id"] for o in consult.build_options(dentists, k)["customer_value"]
        ] == tree_own  # the trade's own bands


def test_estimate_uses_this_business_and_a_band_is_the_owners_answer():
    tree = consult.load_tree("fallback")
    k = {**KNOWLEDGE, "typical_customer_value_usd": 60}
    est = _session(tree, k, {"customer_value": {"choice": "estimate", "amount_usd": 60.0, "source": "choice"}})
    f = consult.facts(est, tree)
    assert (f["customer_value_usd"], f["customer_value_source"]) == (60.0, "estimate")
    band = _session(tree, k, {"customer_value": {"choice": "20_50", "amount_usd": 35.0, "source": "choice"}})
    f = consult.facts(band, tree)
    assert (f["customer_value_usd"], f["customer_value_source"]) == (35.0, "owner")
    unknown = _session(tree, KNOWLEDGE, {"customer_value": {"choice": "estimate", "source": "choice"}})
    assert (
        consult.facts(unknown, tree)["customer_value_source"] == "benchmark"
    )  # no estimate for the business: the tree's


def test_the_value_question_names_a_service_only_when_one_real_service_was_picked():
    tree = consult.load_tree("fallback")
    value = _question(tree, "customer_value")
    ask = lambda *ids: consult.prompt_for(
        value, _session(tree, KNOWLEDGE, {"service": {"choice": list(ids), "source": "choice"}})
    )
    assert ask("svc_invisalign") == "Roughly, what is one new Invisalign customer worth to you?"
    assert (
        ask("svc_offer") == "Roughly, what is one new customer worth to you?"
    )  # not "one new A special offer customer"
    assert ask("svc_invisalign", "svc_cleanings") == "Roughly, what is one new customer worth to you?"
    assert consult.prompt_for(value, _session(tree, KNOWLEDGE)) == "Roughly, what is one new customer worth to you?"


def test_the_value_question_leaves_out_a_service_that_already_names_the_visit():
    """A service named for the visit itself, or for the person the question already names, would read twice: "one new
    New-patient dental appointment patient" (the founder's dev walk, 2026-10-04)."""
    tree = consult.load_tree("dentists")
    value = _question(tree, "customer_value")
    own = [{"name": "New-patient dental appointment"}, {"name": "Free consultations"}, {"name": "Invisalign"}]
    k = {**KNOWLEDGE, "services": own}
    ask = lambda answer: consult.prompt_for(value, _session(tree, k, {"service": answer}))
    pick = lambda svc_id: ask({"choice": [svc_id], "source": "choice"})
    assert pick("svc_new_patient_dental_appointment") == "Roughly, what is one new patient worth to you?"
    assert pick("svc_free_consultations") == "Roughly, what is one new patient worth to you?"
    assert pick("svc_invisalign") == "Roughly, what is one new Invisalign patient worth to you?"
    typed = ask({"note": "New patient appointments", "raw": "New patient appointments", "source": "other"})
    assert typed == "Roughly, what is one new patient worth to you?"
    # the person is the question's own noun: a customer for the fallback tree, so a "Patient transport" service stays
    fallback = consult.load_tree("fallback")
    k = {**KNOWLEDGE, "services": [{"name": "New customer special"}, {"name": "Patient transport"}]}
    ask = lambda svc_id: consult.prompt_for(
        _question(fallback, "customer_value"),
        _session(fallback, k, {"service": {"choice": [svc_id], "source": "choice"}}),
    )
    assert ask("svc_new_customer_special") == "Roughly, what is one new customer worth to you?"
    assert ask("svc_patient_transport") == "Roughly, what is one new Patient transport customer worth to you?"


# "Under $200", "$200 to 500", "$1,000 and up": the forms a value band's label takes
_BAND = re.compile(r"^(?:Under \$(?P<under>[\d,]+)|\$(?P<lo>[\d,]+) to (?P<hi>[\d,]+)|\$(?P<up>[\d,]+) and up)$")


def test_a_town_the_opening_words_named_starts_the_location_question():
    """The owner said Miami at the start (the founder's dev walk, 2026-10-04): the location question asks where in
    Miami, with "All of Miami" one tap away, instead of asking again from nothing."""
    tree = consult.load_tree("dentists")
    loc = _question(tree, "location")
    session = _session(tree, {**KNOWLEDGE, "service_area": ["Miami"]})
    assert session["options"]["location"] == [{"id": "stated_place", "label": "All of Miami", "place": "Miami"}]
    assert consult.prompt_for(loc, session) == (
        "You said Miami. Where in Miami is the clinic? A street address or ZIP code keeps the ads close to you."
    )
    stored, why = consult.parse(session, loc, {"choice": "stated_place"}, gen=None)
    assert why is None and stored == {"note": "Miami", "raw": "All of Miami", "source": "choice"}
    office = _question(consult.load_tree("attorneys"), "location")
    assert consult.prompt_for(office, session).startswith("You said Miami. Where in Miami is the office?")
    # nothing to start from -- two towns, a radius, a ZIP code, nowhere in particular, none -- asks as before
    for area in (["Miami", "Coral Gables"], ["a few miles around the clinic"], ["33130"], ["Nationwide"], []):
        s = _session(tree, {**KNOWLEDGE, "service_area": area})
        assert s["options"]["location"] == [], area
        assert consult.prompt_for(loc, s).startswith("Where should the ads centre?"), area
        assert consult.parse(s, loc, {"choice": "stated_place"}, gen=None) == (None, consult.PLACE_BY_CHOICE)
    # a business already on the map (its listing's point) offers no town
    pointed = {**KNOWLEDGE, "service_area": ["Miami"], "listing": {**LISTING}}
    assert _session(tree, pointed)["options"]["location"] == []


def _dollars(text: str) -> int:
    return int(text.replace(",", ""))


def test_every_value_band_carries_an_amount_inside_its_own_range():
    """Each trade's bands and each ladder start at $0, follow on without gaps, end open, and the amount a band stands
    for in the economics lies inside the range its label shows the owner."""
    lists = [
        (name, [o for o in _question(consult.load_tree(name), "customer_value")["options"] if o["id"] != "estimate"])
        for name in ("dentists", "attorneys", "home_services", "fallback")
    ] + [(f"ladder below {bound}", bands) for bound, bands in consult.VALUE_LADDERS]
    for name, bands in lists:
        floor = 0
        for band in bands:
            m = _BAND.match(band["label"])
            assert m, (name, band["label"])
            if m["under"]:
                lo, hi = 0, _dollars(m["under"])
            elif m["lo"]:
                lo, hi = _dollars(m["lo"]), _dollars(m["hi"])
            else:
                lo, hi = _dollars(m["up"]), math.inf
            assert lo == floor, (name, band["label"])
            assert lo <= band["usd"] < hi, (name, band)
            floor = hi
        assert floor == math.inf, name


def test_example_searches_take_turns_across_the_picked_services():
    from agent import plans

    k = {
        "services": [
            {"name": "Roses", "intent_phrases": ["roses near me", "red roses", "rose delivery"]},
            {"name": "Weddings", "intent_phrases": ["wedding florist", "bridal bouquet"]},
        ],
        "service_area": ["Austin"],
    }
    r = plans.rationale_section(
        k, {"objective": "any", "service": "Roses and Weddings", "services": ["Roses", "Weddings"]}
    )
    assert r["example_searches"] == ["roses near me", "wedding florist", "red roses", "bridal bouquet"]


# ---------------------------------------------------------------- the website before the quote (A3p-5 batch 1 Task 2)

TRADE = ["objective", "service", "customer_value", "location", "radius"]  # the dental tree's own, no place offered
NO_WEBSITE_OPTION = {"id": "none", "label": "No website"}


def _fresh(tree_name: str = "dentists", knowledge: dict | None = None, answers: dict | None = None, candidates=()):
    """A session of `tree_name` as `start` freezes it from `knowledge` (none: the reading saw no site), holding
    `answers`."""
    tree = consult.load_tree(tree_name)
    return {
        "tree": tree_name,
        "options": consult.build_options(tree, knowledge or {}, candidates=candidates),
        "known_facts": {},
        "answers": answers or {},
    }


def test_every_tree_asks_the_website_the_name_the_listing_and_the_services_first_and_its_file_is_unchanged():
    """Q1 and ruling F1: one file holds the website, the name and the services; every tree asks the website and the
    name first, then which Google Maps place is the business -- the trade's listing question, right after the name
    its lookup finds the places by -- then what the business offers, then the trade's own; load_tree reads the
    trade's file as it was (the listing question still its first)."""
    for name in ("dentists", "home_services", "attorneys", "fallback"):
        tree = consult.load_tree(name)
        asked = consult.tree_questions(tree)
        listing, *own = tree["questions"]
        assert [q["id"] for q in asked[:4]] == ["website", "business_name", "listing", "services_offered"], name
        assert asked[2] is listing and asked[4:] == own and listing["id"] == "listing", name
        assert all(asked[i]["common"] is True for i in (0, 1, 3)), name
        assert not any(q.get("common") for q in tree["questions"]), name
    website, name, _listing, services = consult.tree_questions(consult.load_tree("fallback"))[:4]
    assert (website["input"], website["known_fact"], website["placeholder"], website["options"], website["why"]) == (
        "website",
        None,
        "Paste your website's address",
        [NO_WEBSITE_OPTION],
        None,
    )
    assert (name["input"], name["known_fact"], name["placeholder"], name["options"], name["why"]) == (
        "text",
        "business_name",
        "Business name",
        [],
        None,
    )
    assert (services["input"], services["known_fact"], services["placeholder"], services["options"]) == (
        "text",
        None,
        "A few of your services",
        [],
    )
    assert (website["prompt"], name["prompt"], services["prompt"]) == (
        consult.WEBSITE_PROMPT,
        consult.NAME_PROMPT,
        consult.SERVICES_PROMPT,
    )
    assert website["options"][0]["label"] == consult.NO_WEBSITE


def test_the_website_words_are_the_specs_and_owner_worded():
    """Q13, verbatim (spec §2 and the plan's clarifications), and none of them jargon."""
    words = {
        consult.WEBSITE_PROMPT: (
            "Does the business have a website? Paste its address and I'll read it, or tap No website."
        ),
        consult.NO_WEBSITE: "No website",
        consult.SITE_UNREAD_SAY: "I couldn't read {host} just now. It happens with some sites; it is nothing you did.",
        consult.SITE_UNREAD_AGAIN: "Still couldn't read {host}. I'll ask instead.",
        consult.NAME_PROMPT: "What is the business called?",
        consult.SERVICES_PROMPT: "What do you offer? A few words are enough, like {examples}.",
        consult.NOT_A_SITE: "That doesn't look like a web address. Paste it like example.com, or tap No website.",
        consult.SOCIAL: (
            "That's a page on {host}. Does the business have a website of its own? Paste it, or tap No website."
        ),
    }
    for said, spec in words.items():
        assert said == spec
        assert not JARGON.search(said) and not ACCOUNT_JARGON.search(said), said


@pytest.mark.parametrize(
    ("website", "asked"),
    [
        (None, True),
        ({"choice": "none", "none": True, "raw": "none", "source": "choice"}, True),
        ({"note": "dunno", "none": True, "raw": "dunno", "source": "other"}, True),
        ({"url": SITE, "raw": "brightsmile.example", "source": "other", "read": False}, True),
        ({"url": SITE, "read": False, "source": "known_fact"}, True),
        ({"url": SITE, "raw": "brightsmile.example", "source": "other", "read": True}, False),
        ({"url": SITE, "read": True, "source": "known_fact"}, False),
    ],
    ids=[
        "unanswered",
        "no website",
        "no website after two misses",
        "a site that could not be read",
        "an address in the words that could not be read",
        "a site read",
        "a site read at start",
    ],
)
def test_the_name_and_the_services_are_asked_until_a_site_is_read(website, asked):
    """Q3: while the website is unanswered they count (so a site read later only removes them), and after an answer
    with no site read they are asked; never once a site was read. The website question itself stays."""
    s = _fresh(answers={"website": website} if website else {})
    assert [q["id"] for q in consult.questions(s)] == [*(COMMON if asked else ["website"]), *TRADE]
    assert consult.site_read(s) is not asked
    assert consult.website_answer(s) == website


def test_the_listing_question_comes_right_after_the_name_and_before_the_services():
    """Ruling F1: the places a lookup found are asked about right after the name, before what the business offers."""
    s = _fresh(candidates=OFFERED)
    assert [q["id"] for q in consult.questions(s)] == [
        "website",
        "business_name",
        "listing",
        "services_offered",
        *TRADE,
    ]
    assert consult.next_question(s)["question"]["id"] == "website"


def test_the_listing_question_counts_while_the_name_may_still_find_the_business():
    """Ruling F1: with no site read and no place offered yet, the listing question is counted while the name -- whose
    Google Maps lookup may offer places -- is unanswered, so the count never grows when it does. It is never asked
    without places (`pending`, `next_question`); it drops out once the name is answered and found nothing, or was
    skipped; a lookup that offers places makes it the question itself, right after the name."""
    s = _fresh()
    first = consult.next_question(s)["question"]
    assert (first["id"], first["index"], first["count"]) == ("website", 1, 9)
    assert consult.questions_left(s) == 9 and "listing" not in [q["id"] for q in consult.pending(s)]
    s["answers"]["website"] = {"choice": "none", "none": True, "raw": "none", "source": "choice"}
    nxt = consult.next_question(s)["question"]
    assert (nxt["id"], nxt["index"], nxt["count"]) == ("business_name", 2, 9)
    named = {"name": "Bright Smile Dental", "raw": "Bright Smile Dental", "source": "other"}
    for name in (named, {"skipped": True, "raw": "skip", "source": "other"}):  # nothing found, or nothing to look up
        after = {**s, "answers": {**s["answers"], "business_name": name}}
        nxt = consult.next_question(after)["question"]
        assert (nxt["id"], nxt["index"], nxt["count"]) == ("services_offered", 3, 8)
        assert consult.questions_left(after) == 6
    offered = consult.build_options(consult.load_tree("dentists"), {}, candidates=OFFERED)["listing"]
    found = {**s, "options": {**s["options"], "listing": offered}, "answers": {**s["answers"], "business_name": named}}
    nxt = consult.next_question(found)["question"]
    assert (nxt["id"], nxt["index"], nxt["count"]) == ("listing", 3, 9)


@pytest.mark.parametrize(
    ("case", "position"),
    [
        ("a site read", ("objective", 1, 5)),
        ("a name known at start", ("website", 1, 7)),
        ("a session from before the website question", ("objective", 1, 5)),
        ("no map lookup on this server", ("website", 1, 7)),
    ],
)
def test_no_listing_question_is_counted_when_no_lookup_by_the_name_can_offer_places(case, position):
    """Ruling F1: the listing question is counted ahead of its places only while a lookup by the name may still offer
    them -- never once a site is read (the name is not asked), for a name known at start (start's own lookup used it),
    in a session from before the website question (ruling R2-b), or on a server with no map lookup, where the question
    is skipped as where the ads centre is."""
    tree = consult.load_tree("dentists")
    s = _fresh()
    if case == "a site read":
        s["answers"] = dict(READ_SITE)
    elif case == "a name known at start":
        s["answers"] = consult.preanswers(tree, {"business_name": "Bright Smile Dental"}, {}, "")
    elif case == "a session from before the website question":
        s["options"] = {k: v for k, v in s["options"].items() if k not in COMMON}
    else:
        s["answers"] = consult.preanswers(tree, {}, {}, "", can_place=False)
        assert s["answers"]["listing"] == {"skipped": True, "source": "known_fact"}
    nxt = consult.next_question(s)["question"]
    assert (nxt["id"], nxt["index"], nxt["count"]) == position
    assert consult.questions_left(s) == position[2]


def test_a_listing_the_owner_confirmed_names_the_business():
    """Ruling F2: the business's name is pre-answered from the Google Maps listing the owner confirmed (a known fact),
    so a returning business is not asked it, no listing question is counted ahead, and no lookup by the name offers
    its own place again. A lookup's guess or an address the owner placed names no business; a name an assistant
    passed comes first."""
    tree = consult.load_tree("dentists")
    confirmed = {**LISTING, "name": "Bright Smile Dental", "confirmed": True}
    out = consult.preanswers(tree, {}, {**KNOWLEDGE, "listing": confirmed}, "")
    assert out["business_name"] == {"name": "Bright Smile Dental", "source": "known_fact"}
    for listing in (LISTING, {**LISTING, "name": "123 Main St", "kind": "address"}):
        assert "business_name" not in consult.preanswers(tree, {}, {**KNOWLEDGE, "listing": listing}, "")
    told = consult.preanswers(tree, {"business_name": "Sunny Smiles"}, {**KNOWLEDGE, "listing": confirmed}, "")
    assert told["business_name"] == {"name": "Sunny Smiles", "source": "known_fact"}
    s = {"tree": "dentists", "options": consult.build_options(tree, {}), "known_facts": {}, "answers": out}
    nxt = consult.next_question(s)["question"]
    assert (nxt["id"], nxt["index"], nxt["count"]) == ("website", 1, 6)  # the name and where it is: known


def test_a_session_from_before_the_website_question_never_shows_the_common_questions():
    """A session whose options were frozen before the common questions existed asks only the trade's, as it did."""
    s = _fresh()
    old = {**s, "options": {k: v for k, v in s["options"].items() if k not in COMMON}}
    assert [q["id"] for q in consult.questions(old)] == TRADE
    assert consult.next_question(old)["question"]["id"] == "objective"


def test_the_site_start_read_and_a_known_name_pre_answer_their_questions():
    """Q4 (ruling P1: the site start tried rides on the session's known facts as {"url", "read"}, outside KNOWN_FACTS;
    a site read pre-answers in that shape, without the `tries` an unread one counts); the business's name a caller knew
    answers the name question, up to 120 characters. A site start tried and could not open answers nothing: the
    website question asks for it again (A3p-5 batch 2 Task 0, ruling R0-i)."""
    tree = consult.load_tree("dentists")
    assert "website" not in consult.KNOWN_FACTS
    read = consult.preanswers(tree, {"website": {"url": SITE, "read": True}}, KNOWLEDGE, "")
    assert read["website"] == {"url": SITE, "read": True, "source": "known_fact"}
    assert "website" not in consult.preanswers(tree, {"website": {"url": SITE, "read": False}}, KNOWLEDGE, "")
    assert "website" not in consult.preanswers(tree, {}, KNOWLEDGE, "")
    assert "website" not in consult.preanswers(tree, {"website": "brightsmile.example"}, KNOWLEDGE, "")
    named = consult.preanswers(tree, {"business_name": "Bright Smile Dental"}, KNOWLEDGE, "")
    assert named["business_name"] == {"name": "Bright Smile Dental", "source": "known_fact"}
    assert "services_offered" not in named  # no known fact says what the business offers
    assert consult.preanswers(tree, {"business_name": "x" * 120}, KNOWLEDGE, "")["business_name"]["name"] == "x" * 120
    for name in ("x" * 121, "   ", ""):
        assert "business_name" not in consult.preanswers(tree, {"business_name": name}, KNOWLEDGE, ""), name


def test_a_site_start_could_not_open_is_asked_for_again_with_try_again_and_ask_instead():
    """Ruling R1 (spec 2026-10-12 §3.2, the founder's walk): the website question for a site start tried and could not
    open (its known fact, read false) is asked again, in UNOPENED_PROMPT's words -- no {host} of its own now -- with
    "Try again" and "Ask me instead" its options (RETRY_OPTIONS), never "No website", even when the reading named a
    site of its own (the one-tap "Yes: <host>" of ruling R0-i2 is not offered either while the site is unopened), and
    it counts toward the questions left. A session whose start opened its site, or tried none, asks the usual
    question."""
    tree = consult.load_tree("dentists")
    tried = {"website": {"url": "https://www.brightsmile.example/contact", "read": False}}
    s = {
        "tree": "dentists",
        "options": consult.build_options(tree, KNOWLEDGE, known_facts=tried),  # the reading named brightsmile.example
        "known_facts": tried,
        "answers": consult.preanswers(tree, tried, KNOWLEDGE, ""),
    }
    said = consult.UNOPENED_PROMPT
    assert said == "Paste the address again, or let me ask you instead."
    assert not JARGON.search(said) and not ACCOUNT_JARGON.search(said)
    q = consult.next_question(s)["question"]
    assert (q["id"], q["index"], q["count"], q["prompt"]) == ("website", 1, 9, said)
    assert q["options"] == list(consult.RETRY_OPTIONS) and consult.questions_left(s) == 9
    assert (
        consult.question_sentence({"question": q})
        == f"{said} Options: Try again, Ask me instead — or answer in your own words."
    )
    assert consult.build_options(tree, KNOWLEDGE, known_facts={"website": {"url": SITE, "read": True}})["website"] == [
        {"id": "site_known", "label": "Yes: brightsmile.example", "url": SITE},
        NO_WEBSITE_OPTION,
    ]
    assert consult.next_question(_fresh(knowledge=KNOWLEDGE))["question"]["prompt"] == consult.WEBSITE_PROMPT


def test_a_no_that_names_an_address_answers_the_question_that_asks_for_one_again():
    """Ruling R0-i3 (A3p-5 batch 2 Task 0, fix round 1): in the question that asks again for an address start could not
    open (brightsmlie.com, a typo), a first line that names a site of the business's own is the answer even after a no
    -- "No, it's brightsmile.com" corrects it, "No, brightsmlie.com is right" names it again (the command reads the one
    and never fetches the other twice) -- while a no that names none is still no website. may_read_a_site agrees
    (ruling R0-m1). With the site the reading saw offered as well (a restart from another address offers the site it
    read), the offered site's rule comes first: a no that names it stays a no (ruling R0-m)."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    tree = consult.load_tree("dentists")
    tried = {"website": {"url": "https://brightsmlie.com", "read": False}}
    s = {"tree": "dentists", "options": consult.build_options(tree, {}, known_facts=tried), "known_facts": tried}
    s["answers"] = {}
    q = _q(s, "website")
    for words, url in (
        ("No, it's brightsmile.com", "https://brightsmile.com"),
        ("No, brightsmlie.com is right", "https://brightsmlie.com"),
    ):
        assert consult.parse(s, q, {"other": words}, fake) == ({"url": url, "raw": words, "source": "other"}, None)
        assert consult.may_read_a_site(words) is True
    for words in ("No", "No website", "No, facebook.com/brightsmile"):
        assert consult.parse(s, q, {"other": words}, fake) == ({"none": True, "raw": words, "source": "other"}, None)
        assert consult.may_read_a_site(words) is False
    offered = {**s, "options": consult.build_options(tree, KNOWLEDGE)}  # "Yes: brightsmile.example" offered too
    words = "No, https://brightsmile.example is gone"
    assert consult.parse(offered, q, {"other": words}, fake) == ({"none": True, "raw": words, "source": "other"}, None)
    assert fake.read_calls == []


def test_a_site_the_reading_saw_is_offered_before_no_website():
    """Q5: a website on the knowledge that nobody read (the words named it in a way the address pattern missed) is
    offered as one tap; a social page or words that are no address are not."""
    tree = consult.load_tree("dentists")
    assert consult.build_options(tree, KNOWLEDGE)["website"] == [
        {"id": "site_known", "label": "Yes: brightsmile.example", "url": SITE},
        NO_WEBSITE_OPTION,
    ]
    for website in (None, "", "not a site", "https://www.facebook.com/brightsmile"):
        options = consult.build_options(tree, {**KNOWLEDGE, "website": website})
        assert options["website"] == [NO_WEBSITE_OPTION], website
        assert options["business_name"] == [] and options["services_offered"] == []


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ({"choice": "none"}, {"choice": "none", "none": True, "raw": "none", "source": "choice"}),
        ({"choice": "site_known"}, {"choice": "site_known", "url": SITE, "raw": "site_known", "source": "choice"}),
        ({"other": "brightsmile.example"}, {"url": SITE, "raw": "brightsmile.example", "source": "other"}),
        (
            {"other": "  https://BrightSmile.example/Contact.  "},
            {"url": f"{SITE}/Contact", "raw": "https://BrightSmile.example/Contact.", "source": "other"},
        ),
        (
            {"other": '"www.brightsmile.example"!'},
            {"url": "https://www.brightsmile.example", "raw": '"www.brightsmile.example"!', "source": "other"},
        ),
        (
            {"other": "facebook.com/brightsmile"},
            {"social": "https://facebook.com/brightsmile", "raw": "facebook.com/brightsmile", "source": "other"},
        ),
        (
            {"other": "https://www.yelp.com/biz/bright-smile-dental"},
            {
                "social": "https://www.yelp.com/biz/bright-smile-dental",
                "raw": "https://www.yelp.com/biz/bright-smile-dental",
                "source": "other",
            },
        ),
    ],
    ids=["no website", "the site the reading saw", "a bare domain", "a sentence's end", "quoted", "facebook", "yelp"],
)
def test_the_website_answer_is_read_without_the_model(value, stored):
    """Q6: a choice, or words read here -- never by the model. A page on a social or listing site is kept as `social`
    (the command turns it into the clarification SOCIAL); ruling T2-1 takes off the quotes around an address and the
    punctuation a sentence ends with."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh(knowledge=KNOWLEDGE)  # its reading saw brightsmile.example: offered as site_known
    assert consult.parse(s, _q(s, "website"), value, fake) == (stored, None)
    assert fake.read_calls == []


@pytest.mark.parametrize(
    "words",
    ["no", "No.", "none", "Nope", "not yet", "No website", "Don't have one", "I don't have one.", "We don't have one!"]
    + ["no site"],
)
def test_words_that_say_there_is_no_website_answer_no_website(words):
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    assert consult.parse(s, _q(s, "website"), {"other": words}, fake) == (
        {"none": True, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


@pytest.mark.parametrize(
    ("words", "stored"),
    [
        ("yes, it's brightsmile.com", {"url": "https://brightsmile.com"}),
        ("our site is https://bright.example/about.", {"url": "https://bright.example/about"}),
        ("brightsmile.com\n--\nDr. Lee, (650) 555-0100", {"url": "https://brightsmile.com"}),
        ("brightsmile.com\nthanks", {"url": "https://brightsmile.com"}),  # two lines are two words, never one host
        ("find us on facebook.com/brightsmile", {"social": "https://facebook.com/brightsmile"}),
        ("visit www.bright.example", {"url": "https://www.bright.example"}),  # www in front: an address (R2-f)
        ("brightsmile.photography", {"url": "https://brightsmile.photography"}),  # a whole answer: 2-12 letters
    ],
    ids=[
        "in a sentence",
        "ending a sentence",
        "above a signature",
        "on two lines",
        "a social page in a sentence",
        "www in a sentence",
        "a long TLD as the whole answer",
    ],
)
def test_the_first_address_in_the_answers_words_is_read(words, stored):
    """Ruling R2-d: an answer that is not itself an address gives the first address in its words, found by the scan the
    opening words get (consult.site_in_words) -- an e-mailed reply with a signature reads its site; a social page found
    that way is SOCIAL's. Never the model."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    assert consult.parse(s, _q(s, "website"), {"other": words}, fake) == (
        {**stored, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


@pytest.mark.parametrize(
    "words",
    ["info@brightsmile.com", "my site is down", "bright..com", "localhost", "10.0.0.1", "I paid 3.5"]
    + ["Ours is down.Sorry", "Coming soon.Promise", "We moved to St.Louis", "see brightsmile.example"],
)
def test_words_that_are_no_web_address_are_asked_once_more_then_kept_as_no_website(words):
    """Q6: an e-mail address, an IP address, localhost, a host with an empty label (ruling T2-1), a number (R2-d's scan
    finds no address in "I paid 3.5"), a run-on sentence or an address among words with no scheme, `www.` or common
    top-level domain (ruling R2-f) or any other words: NOT_A_SITE; the second miss keeps the words and the question
    goes on as no website."""
    assert consult.site_answer(words) is None
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    q = _q(s, "website")
    assert consult.parse(s, q, {"other": words}, fake) == (None, consult.NOT_A_SITE)
    retried = _fresh(answers={consult.RETRY_KEY: {"website": 1}})
    assert consult.parse(retried, q, {"other": words}, fake) == (
        {"note": words, "none": True, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


@pytest.mark.parametrize(
    "words",
    [
        "No, we don't have one",
        "We don't have a website",
        "nope, no website",
        "No website yet",
        "We do not have a website",
        "N/A",
        "no thanks",
        "Nah",
        "na",
        "None yet",
        "we dont have one",
        "No, we don\u2019t have one",
        "I don't have one.Thanks",
        "Not yet.Soon",
    ],
)
def test_a_no_first_answers_no_website_before_any_address(words):
    """Ruling F4: a first line that starts with a word that says no (no, nope, nah, none, n/a, na) or says "don't
    have", "do not have", "dont have", "not yet" or "no thanks" is "No website" -- before any address in the words is
    read, and with the site the reading saw offered too."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh(knowledge=KNOWLEDGE)
    assert consult.parse(s, _q(s, "website"), {"other": words}, fake) == (
        {"none": True, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


OLD_SITE = "https://oldsite.com"


def test_a_no_that_names_another_site_answers_with_that_site_while_one_is_offered():
    """Ruling R0-m (A3p-5 batch 2 Task 0, item a): while the website question offers the site the reading saw ("Yes:
    oldsite.com"), a first line that names a site of the business's own on another host is the answer even after a no
    -- "No, it's newsite.com" -- as any typed address is; a no alone, a no that names the offered site (`www.` aside:
    listings.same_site) or a social page is still no website (ruling F4), and with no site offered a no still comes
    first, whatever the line names."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh(knowledge={**KNOWLEDGE, "website": OLD_SITE})
    q = _q(s, "website")
    assert s["options"]["website"][0] == {"id": "site_known", "label": "Yes: oldsite.com", "url": OLD_SITE}
    words = "No, it's newsite.com"
    assert consult.parse(s, q, {"other": words}, fake) == (
        {"url": "https://newsite.com", "raw": words, "source": "other"},
        None,
    )
    assert consult.may_read_a_site(words) is True
    nos = ["No", "No website", "No, oldsite.com is gone", "No, https://www.oldsite.com is gone", "No, facebook.com/x"]
    for words in nos:
        assert consult.parse(s, q, {"other": words}, fake) == ({"none": True, "raw": words, "source": "other"}, None)
    for words in ("No", "No website", "No, facebook.com/x"):
        assert consult.may_read_a_site(words) is False, words
    # ruling R0-m1: the command's audit flag is told from the answer alone -- it is part of the call's replay hash, so
    # it cannot see which site the session offers -- and counts a no that names any site of the business's own as a
    # possible reading: over-counted by design, never short of the readings made
    assert consult.may_read_a_site("No, oldsite.com is gone") is True
    plain = _fresh()  # no site offered
    words = "No, it's newsite.com"
    assert consult.parse(plain, _q(plain, "website"), {"other": words}, fake) == (
        {"none": True, "raw": words, "source": "other"},
        None,
    )
    assert fake.read_calls == []


def test_a_booking_page_after_a_no_is_no_website_unless_a_site_is_offered():
    """Ruling F4 with R0-m2: with no site offered, a no comes first, so a booking page named after it is no website of
    the business's own; with a site offered, a page on another host named after a no is read as the business's own
    site, a booking page too (a known gap, parked: there is no list of booking hosts)."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    words = "No, but our booking page is https://calendly.com/jane"
    plain = _fresh()
    assert consult.parse(plain, _q(plain, "website"), {"other": words}, fake) == (
        {"none": True, "raw": words, "source": "other"},
        None,
    )
    offered = _fresh(knowledge=KNOWLEDGE)
    assert consult.parse(offered, _q(offered, "website"), {"other": words}, fake) == (
        {"url": "https://calendly.com/jane", "raw": words, "source": "other"},
        None,
    )


@pytest.mark.parametrize(
    ("words", "url"),
    [("na.example.com", "https://na.example.com"), ("nope.dental", "https://nope.dental")],
)
def test_an_address_that_begins_like_a_no_is_an_address(words, url):
    """Ruling F4's no-word is a whole word: an address that begins with one is still an address."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    assert consult.parse(s, _q(s, "website"), {"other": words}, fake) == (
        {"url": url, "raw": words, "source": "other"},
        None,
    )


@pytest.mark.parametrize(
    "words",
    [
        "Ask Jane\n\nJane Doe\nwww.janes-design.example",
        "Ask Jane\n-- \nJane Doe, www.janes-design.example",
        "Ask Jane\n\u2014Jane Doe, www.janes-design.example",
        "Ask Jane\nSent from www.mail.example",
    ],
    ids=["a blank line", "a -- signature", "an em-dash signature", "a sent-from line"],
)
def test_only_the_first_paragraph_of_the_answer_is_read(words):
    """Ruling F4: what follows the first blank line, or a line that starts with "--", an em dash or "Sent from" -- an
    e-mail's signature -- is never read for an address: the answer is the words above it, kept as the note on a second
    miss (`raw` keeps the words as sent)."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    q = _q(s, "website")
    assert consult.parse(s, q, {"other": words}, fake) == (None, consult.NOT_A_SITE)
    retried = _fresh(answers={consult.RETRY_KEY: {"website": 1}})
    assert consult.parse(retried, q, {"other": words}, fake) == (
        {"note": "Ask Jane", "none": True, "raw": words, "source": "other"},
        None,
    )


@pytest.mark.parametrize(
    ("words", "url"),
    [
        ("Hi,\n\nbrightsmile.com\n\nThanks, Ann", "https://brightsmile.com"),
        ("Hello Admiry team,\n\nwww.brightsmile.example", "https://www.brightsmile.example"),
        ("Good morning!\n\nOur site is https://brightsmile.example/about.", "https://brightsmile.example/about"),
        ("Dear Admiry,\n\n\nbrightsmile.com\n-- \nAnn", "https://brightsmile.com"),
        ("--\nbrightsmile.com", "https://brightsmile.com"),
        ("\u2014\nbrightsmile.com", "https://brightsmile.com"),
        ("Hi, it's brightsmile.com", "https://brightsmile.com"),
    ],
    ids=[
        "a greeting, the site, a sign-off",
        "a greeting to the team",
        "good morning",
        "dear, two blank lines, a signature",
        "a -- line first",
        "an em-dash line first",
        "a greeting that names the site",
    ],
)
def test_a_greeting_paragraph_is_skipped_and_an_answer_that_starts_with_a_signature_is_read_whole(words, url):
    """Ruling R0-m (A3p-5 batch 2 Task 0, item b) and R0-m3: a first paragraph that is only a greeting -- at most four
    words, the first hi, hello, hey, dear, greetings or good, naming no address and saying no no -- is skipped once and
    the next paragraph read; an answer that starts with a signature line has an empty first paragraph, so the whole of
    it is read. The command's audit flag reads the same words (`may_read_a_site`). Never the model."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    assert consult.parse(s, _q(s, "website"), {"other": words}, fake) == (
        {"url": url, "raw": words, "source": "other"},
        None,
    )
    assert consult.may_read_a_site(words) is True
    assert fake.read_calls == []


@pytest.mark.parametrize(
    "words",
    ["Hello Admiry team,\n\nNo website yet", "Hi,\n\nNope\n\nAnn", "Hey, don't have one"],
    ids=["a no after a greeting", "nope between a greeting and a name", "a greeting that says no"],
)
def test_a_no_after_a_greeting_or_inside_one_answers_no_website(words):
    """Ruling R0-m: the paragraph after a greeting is read as the answer, a no first (ruling F4); a greeting-like
    paragraph that says no is no greeting (R0-m3), so its no is read."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    assert consult.parse(s, _q(s, "website"), {"other": words}, fake) == (
        {"none": True, "raw": words, "source": "other"},
        None,
    )
    assert consult.may_read_a_site(words) is False


@pytest.mark.parametrize("words", ["Hi,", "Good morning!", "Dear Admiry,\n-- \nAnn", "Hello there,\n\n\u2014 Ann"])
def test_a_greeting_with_nothing_after_it_is_an_empty_answer_then_no_website(words):
    """Ruling R0-m: a greeting followed by nothing but a signature, or by nothing at all, is an empty answer -- asked
    once more, as an empty answer is -- and the second miss answers the question as no website, the answer's words kept
    as the note: never asked without end."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh()
    q = _q(s, "website")
    assert consult.parse(s, q, {"other": words}, fake) == (None, "Send a choice or a few words.")
    assert consult.may_read_a_site(words) is False
    retried = _fresh(answers={consult.RETRY_KEY: {"website": 1}})
    assert consult.parse(retried, q, {"other": words}, fake) == (
        {"note": words, "none": True, "raw": words, "source": "other"},
        None,
    )


@pytest.mark.parametrize("words", ["yes", "Yep", "Yes, that's it", "yes please", "Yes: brightsmile.example"])
def test_a_yes_picks_the_site_the_reading_saw(words):
    """Ruling F4: with the site the reading saw offered (site_known, "Yes: <host>"), a plain yes or that option's own
    label picks it, as the tap does; with none offered, a yes is no address."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    s = _fresh(knowledge=KNOWLEDGE)
    q = _q(s, "website")
    assert consult.parse(s, q, {"other": words}, fake) == (
        {"choice": "site_known", "url": SITE, "raw": words, "source": "other"},
        None,
    )
    assert consult.parse(_fresh(), q, {"other": words}, fake) == (None, consult.NOT_A_SITE)
    assert fake.read_calls == []


def test_a_second_social_page_answers_no_website_and_keeps_the_page():
    """Ruling F4: a page on a social or listing site is asked about once (SOCIAL; the command counts a retry), and a
    second answer that is no site of the business's own -- that page again -- ends the question as "No website", the
    page kept as the answer's `social`."""
    fake = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads={})
    retried = _fresh(answers={consult.RETRY_KEY: {"website": 1}})
    words = "facebook.com/brightsmile"
    assert consult.parse(retried, _q(retried, "website"), {"other": words}, fake) == (
        {"social": "https://facebook.com/brightsmile", "none": True, "raw": words, "source": "other"},
        None,
    )


def test_a_website_choice_not_offered_or_words_too_long_or_empty_are_refused():
    s = _fresh()  # the reading saw no site: "No website" is the only option
    q = _q(s, "website")
    pick = "Pick one of the options (none), or answer in your own words."
    assert consult.parse(s, q, {"choice": "site_known"}, None) == (None, pick)
    assert consult.parse(s, q, {"choice": "yes"}, None) == (None, pick)
    assert consult.parse(s, q, {"other": "  "}, None) == (None, "Send a choice or a few words.")
    assert consult.parse(s, q, {"other": "a" * 501}, None) == (None, "Keep it under 500 characters.")


def test_a_social_page_is_named_by_its_site():
    assert consult.social_say("https://www.yelp.com/biz/bright-smile") == consult.SOCIAL.format(host="yelp.com")
    assert consult.social_say("https://m.facebook.com/brightsmile") == consult.SOCIAL.format(host="facebook.com")
    assert consult.social_say("https://linktr.ee/brightsmile") == consult.SOCIAL.format(host="linktr.ee")


@pytest.mark.parametrize(
    ("tree", "examples"),
    [
        ("dentists", "cleanings, implants, Invisalign"),
        ("home_services", "roof repair, gutters, inspections"),
        ("attorneys", "wills, trusts, probate"),
    ],
)
def test_the_services_question_names_the_trades_example(tree, examples):
    """Q2: the tree's defaults.services_example fills {examples}."""
    s = _fresh(tree)
    assert consult.prompt_for(_q(s, "services_offered"), s) == (
        f"What do you offer? A few words are enough, like {examples}."
    )
    assert consult.load_tree(tree)["defaults"]["services_example"] == examples


def test_the_fallback_tree_names_no_example_and_its_services_question_asks_plainly():
    """M7: the fallback tree serves trades too different for one example (a florist, a tutor, a salon), so it carries
    no services_example and the services question drops its "like ..." clause."""
    assert "services_example" not in consult.load_tree("fallback")["defaults"]
    s = _fresh("fallback")
    assert consult.prompt_for(_q(s, "services_offered"), s) == "What do you offer? A few words are enough."


def test_each_common_question_shows_its_own_placeholder():
    """Q2: next_question passes a question's placeholder to the envelope (the homepage reads q.other.placeholder);
    the trade's questions keep the default."""
    s = _fresh()
    seen = []
    for qid, answer in (
        ("website", {"choice": "none", "none": True, "raw": "none", "source": "choice"}),
        ("business_name", {"name": "Bright Smile Dental", "raw": "Bright Smile Dental", "source": "other"}),
        ("services_offered", {"note": "cleanings", "raw": "cleanings", "source": "other"}),
        ("objective", None),
    ):
        q = consult.next_question(s)["question"]
        seen.append((q["id"], q["input"], q["other"]))
        if answer:
            s["answers"][qid] = answer
    assert seen == [
        ("website", "website", {"allowed": True, "placeholder": "Paste your website's address"}),
        ("business_name", "text", {"allowed": True, "placeholder": "Business name"}),
        ("services_offered", "text", {"allowed": True, "placeholder": "A few of your services"}),
        ("objective", "single", {"allowed": True, "placeholder": envelope.OTHER_PLACEHOLDER}),
    ]


def test_the_count_shows_the_name_and_services_and_never_grows_when_a_site_is_read():
    """Review focus: a session with no site counts the name and services questions from its first turn; a site read
    removes them, so the index goes up and the count down -- never "3 / 7" then "4 / 8"."""
    s = _fresh()
    first = consult.next_question(s)["question"]
    assert (first["id"], first["index"], first["count"]) == ("website", 1, 9)  # the listing counted too (ruling F1)
    s["answers"]["website"] = {"url": SITE, "raw": "brightsmile.example", "source": "other", "read": True}
    after = consult.next_question(s)["question"]
    assert (after["id"], after["index"], after["count"]) == ("objective", 2, 6)


def test_a_known_name_is_not_counted_once_a_site_read_removes_its_question():
    """The count is of the questions the session asks: a name a caller knew pre-answers a question a site read no longer
    asks, so it takes nothing off the count."""
    tree = consult.load_tree("dentists")
    facts = {"business_name": "Bright Smile Dental", "website": {"url": SITE, "read": True}}
    s = {"tree": "dentists", "options": consult.build_options(tree, KNOWLEDGE), "answers": {}}
    s["answers"] = consult.preanswers(tree, facts, KNOWLEDGE, "")
    assert set(s["answers"]) == {"website", "business_name"}
    nxt = consult.next_question(s)["question"]
    assert (nxt["id"], nxt["index"], nxt["count"]) == ("objective", 1, 5)


def test_restart_keeps_the_session_and_the_owners_answers_the_new_tree_still_offers(conn):
    """Q7: the same row takes the reading's tree, options and pre-answers, and the website answer; an earlier answer of
    the owner's whose question the new tree has and whose choice it still offers is kept (over a pre-answer), one
    whose choice it does not offer is dropped, and an earlier pre-answer gives way to the new ones."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    s = consult.create(
        conn,
        "biz_1",
        tree="fallback",
        locale="en-US",
        opening_text="we fix things",
        known_facts={"social": "https://facebook.com/brightsmile"},
        options=consult.build_options(consult.load_tree("fallback"), {}),
        answers={
            "objective": {"choice": "calls", "raw": "calls", "source": "choice"},
            "customer_value": {"choice": "lt_500", "raw": "lt_500", "source": "choice"},  # the fallback tree's band
            "location": {"skipped": True, "raw": "later", "source": "other"},
            "radius": {"miles": 25.0, "raw": 25, "source": "known_fact"},
            consult.RETRY_KEY: {"website": 1},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    s = consult.record(conn, s, {"business_name": {"name": "Bright Smile", "raw": "Bright Smile", "source": "other"}})
    dentists = consult.load_tree("dentists")
    options = consult.build_options(dentists, KNOWLEDGE)
    website = {"url": SITE, "raw": "brightsmile.example", "source": "other", "read": True}
    new = {
        "website": website,
        "objective": {"choice": "bookings", "raw": "bookings", "source": "known_fact"},
        "radius": {"miles": 3.0, "raw": "3 miles", "source": "known_fact"},
    }
    out = consult.restart(conn, s, tree="dentists", options=options, answers=new)
    assert out["id"] == s["id"] and out["business_id"] == "biz_1"
    assert (out["tree"], out["options"], out["step"], out["status"]) == ("dentists", options, 0, "open")
    assert out["answers"] == {
        "website": website,
        "objective": {"choice": "calls", "raw": "calls", "source": "choice"},  # the owner's, over the pre-answer
        "radius": {"miles": 3.0, "raw": "3 miles", "source": "known_fact"},  # the new pre-answer
        "location": {"skipped": True, "raw": "later", "source": "other"},
        "business_name": {"name": "Bright Smile", "raw": "Bright Smile", "source": "other"},  # in every tree
    }
    assert out["known_facts"] == {"social": "https://facebook.com/brightsmile"}
    assert consult.get(conn, "biz_1", s["id"]) == out
    nxt = consult.next_question(out)["question"]
    assert (nxt["id"], nxt["index"], nxt["count"]) == ("service", 4, 5)


def test_restart_asks_again_which_place_is_the_business_when_the_new_reading_offers_places(conn):
    """Ruling R2-i: an earlier answer to the listing question ("None of these") answered the places offered then; when
    the new reading's lookup offers places, it is dropped, so the owner confirms the new set. With nothing offered,
    the question is not asked and its answer goes too (its choice is no longer offered)."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=consult.build_options(tree, KNOWLEDGE, candidates=OFFERED[:1]),
        answers={"listing": {"choice": "none", "raw": "none", "source": "choice"}},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    website = {"url": SITE, "raw": "brightsmile.example", "source": "other", "read": True}
    offered = consult.build_options(tree, KNOWLEDGE, candidates=OFFERED[1:])  # another place, still with "None"
    out = consult.restart(conn, s, tree="dentists", options=offered, answers={"website": website})
    asked = consult.next_question(out)["question"]
    assert "listing" not in out["answers"] and asked["id"] == "listing"
    assert asked["options"][0]["label"].startswith("Smile Studio")
    nothing = consult.build_options(tree, KNOWLEDGE)
    again = consult.restart(conn, out, tree="dentists", options=nothing, answers={"website": website})
    assert "listing" not in again["answers"] and "listing" not in [q["id"] for q in consult.questions(again)]


def test_restart_settles_a_session_with_nothing_left_to_ask(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    tree = consult.load_tree("dentists")
    options = consult.build_options(tree, KNOWLEDGE)
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=options,
        answers={
            "objective": {"choice": "bookings", "raw": "bookings", "source": "choice"},
            "service": {"choice": ["svc_invisalign"], "raw": ["svc_invisalign"], "source": "choice"},
            "customer_value": {"choice": "1000_plus", "raw": "1000_plus", "source": "choice"},
            "location": {"skipped": True, "raw": "later", "source": "other"},
            "radius": {"choice": "r5", "raw": "r5", "miles": 5.0, "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert s["status"] == "open"  # the website, the name and the services wait
    website = {"url": SITE, "raw": "brightsmile.example", "source": "other", "read": True}
    out = consult.restart(conn, s, tree="dentists", options=options, answers={"website": website})
    assert out["status"] == "ready" and consult.next_question(out) is None


def test_set_options_replaces_one_questions_options(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    tree = consult.load_tree("dentists")
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="x",
        known_facts={},
        options=consult.build_options(tree, KNOWLEDGE),
        answers={},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert "listing" not in [q["id"] for q in consult.pending(s)]
    places_found = consult.build_options(tree, KNOWLEDGE, candidates=OFFERED)["listing"]
    out = consult.set_options(conn, s, "listing", places_found)
    assert out["id"] == s["id"] and out["options"] == {**s["options"], "listing": places_found}
    assert "listing" in [q["id"] for q in consult.pending(out)] and consult.get(conn, "biz_1", s["id"]) == out


# ---------------------------------------------------------------- a trade's own services to lead with (2026-10-08)
# The founder's dev walk, 2026-10-08: an AC contractor was offered "Remodels" and "Roofing" to lead its ads with (the
# home-services tree's defaults, after its own "AC services"), took all six, and the plan named them all.
def _labels(knowledge: dict) -> list[str]:
    return [o["label"] for o in consult.build_options(consult.load_tree("home_services"), knowledge)["service"]]


AC = {"category": "home_services", "category_detail": "AC contractor", "services": [{"name": "AC services"}]}


def test_a_home_services_trade_is_offered_its_own_services_after_the_businesss_own():
    labels = _labels(AC)
    assert labels[0] == "AC services" and len(labels) == consult.MAX_SERVICE_OPTIONS
    assert labels[1:] == ["AC repair", "Heating repair", "HVAC replacement", "Maintenance plans", "Emergency repairs"]
    plumber = _labels({"category": "home_services", "category_detail": "Plumber", "services": []})
    assert plumber[:3] == ["Leak repair", "Drain cleaning", "Water heaters"] and "Roofing" not in plumber
    electrician = _labels({"category": "home_services", "category_detail": "Electrician", "services": []})
    assert electrician[:3] == ["Panel upgrades", "Wiring repair", "Lighting"]
    roofer = _labels({"category": "home_services", "category_detail": "Roofing contractor", "services": []})
    assert roofer[:2] == ["Roof repair", "Roof replacement"] and "HVAC replacement" not in roofer
    # a trade the tree names no services for: the family's own, as before
    assert _labels({"category": "home_services", "category_detail": "Landscaper", "services": []})[:2] == [
        "Emergency repairs",
        "Installations",
    ]


def test_a_trades_bigger_jobs_carry_the_fit_flags_the_allocator_reads():
    tree = consult.load_tree("home_services")
    options = consult.build_options(tree, AC)
    session = {
        "tree": "home_services",
        "options": options,
        "answers": {"service": {"choice": ["svc_hvac_replacement"]}},
    }
    assert consult.service_flags(session, tree) == {"meta_fit", "newsbreak_fit"}
    session["answers"]["service"]["choice"] = ["svc_ac_repair"]
    assert consult.service_flags(session, tree) == set()
    for trade, entries in next(q for q in tree["questions"] if q["id"] == "service")["by_trade"].items():
        assert len(entries) >= consult.MAX_SERVICE_OPTIONS - 1 and len({e["id"] for e in entries}) == len(entries), (
            trade
        )
