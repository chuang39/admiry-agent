"""The Researcher's run (capability layer K4, spec 2026-10-07 §4, §4.1; founder ruling R-K4): one research_run per
job, the guards on every sentence and search, the Critic's grade, at most two rounds -- revise with what was wrong,
fetch_more after reading more -- every round's verdict stored; owners read it only with AGENT_ROLES on, a run built
on and the Critic's deliver; the business's day, the day's runs and the time research-run gives it. A business nearby
is described, never named, in anything stored or shown (D5): its name is in memory for the run."""

import json
import logging
import types

import pytest

from agent import audit, benchmark, business, config, llm, models, places
from agent.roles import base, contracts, queue, research_material, researcher, runner
from agent.tests import research_fixtures as rf
from agent.tests.fixtures import knowledge_fixture, research_fixture, research_review_fixture

BUSY = "the assistant is busy; try again in a minute"
MASK = "[a business nearby]"


@pytest.fixture
def world(conn, monkeypatch):
    """AGENT_ROLES validate; the dental business, six businesses nearby with their pages, no guides."""
    monkeypatch.setenv("AGENT_ROLES", "validate")
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_pages(monkeypatch)
    return rf.fake_places(monkeypatch)


def _run(conn) -> dict:
    return runner.research_run(conn, 0, 1)


def _finding(text: str, *ids: str, confidence: str = "medium") -> models.ResearchFinding:
    return models.ResearchFinding(text=text, source_ids=list(ids), confidence=confidence)


def _stored(conn, run_id: str) -> dict:
    return conn.execute("SELECT * FROM research_run WHERE id = %s", (run_id,)).fetchone()


def _remembered(row: dict) -> str:
    """Everything a run keeps for 13 months, as lower-case text: the research, the Critic's rounds, what was read."""
    keep = {k: row[k] for k in ("brief", "brief_json", "review", "materials_digest")}
    return json.dumps(keep, default=str).lower()


def test_one_run_with_its_round_and_verdict_stored(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch)
    jid = rf.job(conn, topics=["competitors"])
    out = _run(conn)
    assert (out["done"], out["failed"], out["left"], out["mode"]) == (1, 0, 0, "validate")
    row = rf.run_row(conn)
    assert (row["status"], row["verdict"], row["score"], row["confidence"], row["dropped"]) == (
        "done",
        "deliver",
        1.0,
        "high",
        None,
    )
    assert (row["rounds"], row["calls"], row["mode"], row["trigger"], row["job_id"]) == (
        1,
        2,
        "validate",
        "operator",
        jid,
    )
    assert row["brief"] == {"topics": ["competitors"], "note": None} and row["vertical"] == "dentists"
    grades = {"grounded": 5, "numbers": 5, "relevant": 5}
    assert row["review"]["rounds"] == [
        {
            "round": 1,
            "verdict": "deliver",
            "score": 1.0,
            "grades": grades,
            "issues": [],
            "missing": [],
            "problems": [],
            "fetched": {"shown": 5, "pages": 5, "knowledge": 0, "fetched_more": False},
        }
    ]
    assert row["review"]["final"] == {
        "grades": grades,
        "score": 1.0,
        "verdict": "deliver",
        "issues": [],
        "missing": [],
    }
    assert row["materials_digest"]["counts"]["pages"] == 5
    assert gen.research_calls[0]["feedback"] is None and len(gen.research_calls[0]["pages"]) == 5
    job = conn.execute("SELECT status, result FROM agent_job WHERE id = %s", (jid,)).fetchone()
    assert job["status"] == "done"
    assert job["result"] == {"run": row["id"], "verdict": "deliver", "score": 1.0, "confidence": "high"}


def test_owners_read_it_only_with_the_roles_on_a_run_built_on_and_the_critics_deliver(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch)
    rf.job(conn)
    _run(conn)
    built_validate = rf.run_row(conn)
    assert researcher.owner_view(config.load(), built_validate) is None
    team = researcher.team_view(built_validate)
    assert team["brief_json"]["findings"][0]["text"] == research_fixture().findings[0].text
    monkeypatch.setenv("AGENT_ROLES", "on")
    assert researcher.owner_view(config.load(), built_validate) is None  # built under validate: the team's for good
    rf.job(conn)
    _run(conn)
    view = researcher.owner_view(config.load(), rf.run_row(conn))
    assert [f["text"] for f in view["findings"]] == [f.text for f in research_fixture().findings]
    assert view["findings"][0]["sources"][0]["id"] == "benchmark:dentists:google_search"
    assert view["searches_to_aim_for"] == ["invisalign palo alto", "teeth cleaning near me"]
    assert (view["confidence"], view["topics"]) == ("high", list(research_material.TOPICS)) and "id" not in view
    monkeypatch.setenv("AGENT_ROLES", "validate")
    assert researcher.owner_view(config.load(), rf.run_row(conn)) is None


def test_a_revision_runs_one_more_round_with_what_was_wrong(conn, world, monkeypatch):
    gen = rf.fake_llm(
        monkeypatch, research_reviews=[research_review_fixture(low={"relevant": 2}), research_review_fixture()]
    )
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    assert (row["rounds"], row["calls"], row["verdict"], row["dropped"]) == (2, 4, "deliver", None)
    assert [r["verdict"] for r in row["review"]["rounds"]] == ["revise", "deliver"]
    assert "relevant: relevant note" in gen.research_calls[1]["feedback"]
    assert row["materials_digest"]["fetched_more"] is False
    audited = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'job.researcher' ORDER BY id")
    assert [(a["args_redacted"]["round"], a["args_redacted"]["call"]) for a in audited.fetchall()] == [
        ("1", "write"),
        ("1", "review"),
        ("2", "write"),
        ("2", "review"),
    ]  # every model call is on the business's day before it is made (the audit trail keeps the round as text)
    assert runner.runs_today(conn, rf.BIZ) == row["calls"] == 4


def test_fetch_more_reads_more_before_the_second_round(conn, world, monkeypatch):
    calls = rf.fake_places(monkeypatch, rf.nearby(8))
    missing = research_review_fixture(low={"grounded": 2}, missing=["more businesses nearby"])
    gen = rf.fake_llm(monkeypatch, research_reviews=[missing, research_review_fixture()])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    assert [r["verdict"] for r in row["review"]["rounds"]] == ["fetch_more", "deliver"]
    first, second = row["review"]["rounds"]
    assert first["missing"] == ["more businesses nearby"] and first["issues"] == [
        {"dimension": "grounded", "text": "grounded note"}
    ]
    assert first["fetched"] == {"shown": 5, "pages": 5, "knowledge": 0, "fetched_more": False}
    assert second["fetched"] == {"shown": 8, "pages": 8, "knowledge": 0, "fetched_more": True}
    assert row["materials_digest"]["fetched_more"] is True
    assert [len(c["material"]["competitors"]) for c in gen.research_calls] == [5, 8]
    assert [len(c["pages"]) for c in gen.research_calls] == [5, 8]
    assert "missing: more businesses nearby" in gen.research_calls[1]["feedback"] and len(calls) == 1


@pytest.mark.parametrize(
    ("low", "score", "confidence"),
    [({}, 1.0, "high"), ({"relevant": 4}, 0.92, "high"), ({"numbers": 4, "relevant": 4}, 0.83, "medium")],
)
def test_a_delivered_runs_confidence_follows_its_score(conn, world, monkeypatch, low, score, confidence):
    """HIGH_SCORE (0.85) and above is high, below it medium; the Critic delivered all three (0.7, no grade under 3)."""
    rf.fake_llm(monkeypatch, research_reviews=[research_review_fixture(low=low)])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    assert (row["verdict"], row["score"], row["confidence"], row["dropped"]) == ("deliver", score, confidence, None)


def test_two_rounds_at_most_and_what_falls_short_stays_with_the_team(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch, research_reviews=[research_review_fixture(low={"numbers": 2})])
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    assert (row["rounds"], row["calls"], row["verdict"], row["dropped"], row["confidence"]) == (
        2,
        4,
        base.REVISE,
        base.REVISE,
        "low",
    )
    assert researcher.owner_view(config.load(), row) is None


def test_a_source_the_material_does_not_hold_is_dropped_and_fed_back(conn, world, monkeypatch):
    stray = research_fixture(findings=[_finding("A practice opened nearby.", "place:9"), *research_fixture().findings])
    gen = rf.fake_llm(
        monkeypatch,
        researches=[stray],
        research_reviews=[research_review_fixture(low={"grounded": 2}), research_review_fixture()],
    )
    rf.job(conn)
    _run(conn)
    assert rf.run_row(conn)["brief_json"]["findings"][0] == {
        "text": "A practice opened nearby.",
        "source_ids": [],
        "confidence": "medium",
        "dropped": "unsourced",
    }
    assert "finding 1: 'place:9' is not one of the sources" in gen.research_calls[1]["feedback"]


def test_a_number_its_cited_sources_do_not_hold_is_dropped(conn, world, monkeypatch):
    wrong = research_fixture(
        findings=[
            _finding("Dentists here pay about $45 a lead.", "benchmark:dentists:google_search"),
            research_fixture().findings[1],
        ]
    )
    gen = rf.fake_llm(
        monkeypatch,
        researches=[wrong],
        research_reviews=[research_review_fixture(low={"numbers": 2}), research_review_fixture()],
    )
    rf.job(conn)
    _run(conn)
    findings = rf.run_row(conn)["brief_json"]["findings"]
    assert (findings[0]["dropped"], findings[1]["dropped"]) == ("number", None)
    assert "finding 1: it stated a number its sources do not hold" in gen.research_calls[1]["feedback"]


def test_a_number_from_another_source_than_the_one_cited_is_dropped(conn, world, monkeypatch):
    mixed = research_fixture(findings=[_finding("Leads cost $72.97 here.", "business"), research_fixture().findings[1]])
    rf.fake_llm(monkeypatch, researches=[mixed])
    rf.job(conn)
    _run(conn)
    assert rf.run_row(conn)["brief_json"]["findings"][0]["dropped"] == "number"


def test_the_counts_the_material_gives_may_be_stated_and_no_other(conn, world, monkeypatch):
    """The material gives how many businesses nearby it shows (five) and found (six), how many pages it read (five) and
    the plan's radius (ten miles): a finding may state those without any source writing them, as the judge's rubric
    allows; a count it does not give is a number its sources do not hold."""
    counted = research_fixture(
        findings=[
            _finding("Five of the six nearby practices are on the map.", "place:1"),
            _finding("Nearby practices within 10 miles say they offer free whitening.", "page:1"),
            _finding("Seven nearby practices say they offer free whitening.", "page:1"),
        ]
    )
    rf.fake_llm(monkeypatch, researches=[counted])
    rf.job(conn)
    _run(conn)
    findings = rf.run_row(conn)["brief_json"]["findings"]
    assert [f["dropped"] for f in findings] == [None, None, "number"]


def test_a_finding_may_state_how_many_businesses_nearby_its_own_sources_are_from(conn, world, monkeypatch):
    """The final review's I2: the prompt's own phrasing ("two of the five nearby practices") states how many businesses
    nearby the finding's own sources are from -- a place and its own page are one business -- which no source writes;
    a count past them is still a number its sources do not hold. No page holds a numeral here, and place:3 is three
    miles away: neither can hold the counts by chance."""
    rf.fake_pages(monkeypatch, {f"https://rival{k}.example": "Free whitening with every exam." for k in range(1, 11)})
    counted = research_fixture(
        findings=[
            _finding("Two of the five nearby practices offer free whitening.", "page:1", "page:2"),
            _finding("Seven of the five nearby practices offer free whitening.", "page:1", "page:2"),
            _finding("One nearby practice is open on Saturdays.", "place:3"),
            _finding(
                "Two nearby practices offer free whitening.", "page:3", "place:3"
            ),  # one business, its two sources
        ]
    )
    rf.fake_llm(monkeypatch, researches=[counted])
    rf.job(conn)
    _run(conn)
    assert [f["dropped"] for f in rf.run_row(conn)["brief_json"]["findings"]] == [None, "number", None, "number"]


def test_a_finding_citing_a_stray_source_beside_a_real_one_keeps_the_real_one(conn, world, monkeypatch):
    mixed = research_fixture(
        findings=[_finding("Same-week appointments set the practice apart.", " business", "place:9", "business ")]
    )
    rf.fake_llm(monkeypatch, researches=[mixed])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    [finding] = row["brief_json"]["findings"]
    assert (finding["source_ids"], finding["dropped"]) == (["business"], None)  # stripped, once, the stray left out
    assert "finding 1: 'place:9' is not one of the sources" in row["review"]["rounds"][0]["problems"]


def test_an_offer_angle_is_an_offer_of_this_business_so_its_numbers_come_from_the_business_alone(
    conn, world, monkeypatch
):
    """The prompt defines an offer angle as something the business itself offers. A price on a rival's page, or the id
    of a source, is no price of this business: its numbers must be in what the material says of the business -- its
    own knowledge and its plan -- and nowhere else."""
    rf.fake_pages(monkeypatch, {"https://rival2.example": "New patient special: $49 exam and cleaning."})
    angles = [
        "New patients pay $49 for an exam and cleaning.",  # a rival's price, on its page
        "Get 3 free whitening sessions a year.",  # held only by an id: place:3, page:3
        "Take $4 off every cleaning.",  # held only by an id too
        "Cleanings for $120, booked the same week.",  # the business's own price: its knowledge says it
        "Same-week appointments for cleanings.",
    ]
    gen = rf.fake_llm(
        monkeypatch,
        researches=[research_fixture(offer_angles=angles)],
        research_reviews=[research_review_fixture(low={"numbers": 2}), research_review_fixture()],
    )
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    assert [a["dropped"] for a in row["brief_json"]["offer_angles"]] == ["number", "number", "number", None, None]
    told = [f for f in gen.research_calls[1]["feedback"] if f.startswith("offer angle")]
    said = "it stated a number the business's own prices and offers do not hold"
    assert told == [f"offer angle {n}: {said}" for n in (1, 2, 3)]
    assert researcher.owner_view(config.load(), row)["offer_angles"] == angles[3:]


def test_an_offer_angle_may_state_what_the_plan_itself_offers(conn, world, monkeypatch):
    """What the material says of this business is its knowledge and its plan: the plan's own offer is the business's."""
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,consultation,offer}', to_jsonb(%s::text))",
        ("$59 first visit",),
    )
    angles = ["A first visit for $59.", "A first visit for $60."]
    rf.fake_llm(monkeypatch, researches=[research_fixture(offer_angles=angles)])
    rf.job(conn)
    _run(conn)
    offers = rf.run_row(conn)["brief_json"]["offer_angles"]
    assert [(o["text"], o["dropped"]) for o in offers] == [(angles[0], None), (angles[1], "number")]


def test_an_offer_angle_is_not_licensed_by_a_numeral_of_the_business_that_is_no_offer(conn, world, monkeypatch):
    """The business's facts hold numerals no offer rests on -- its hours (8-5), the customers a month it wants (20), its
    monthly budget (2000), its phone (0100) -- and guard.known takes every numeral of the text it is given as held, so
    that text is only what the business says it offers and charges: a service's price hint is one, those are not."""
    angles = [
        "Take $5 off every cleaning.",  # the 5 of the hours
        "Take $20 off a first visit.",  # the 20 customers a month the owner asked for
        "Save $2000 on Invisalign.",  # the monthly budget
        "Take $100 off a cleaning.",  # the 0100 of the phone
        "Cleanings for $120, booked the same week.",  # a service's price hint
    ]
    rf.fake_llm(monkeypatch, researches=[research_fixture(offer_angles=angles)])
    rf.job(conn)
    _run(conn)
    offers = rf.run_row(conn)["brief_json"]["offer_angles"]
    assert [o["dropped"] for o in offers] == ["number", "number", "number", "number", None]


def test_an_offer_angle_may_state_what_the_business_says_it_offers_and_charges(conn, world, monkeypatch):
    """The prices and offers the business states (`pricing_hints`; `offers`, their text and their terms), its reasons to
    be chosen (`usp`): its own words about what it offers and charges. The date an offer ends is no offer."""
    knowledge = {
        **knowledge_fixture().model_dump(),
        "listing": rf.LISTING,
        "pricing_hints": ["cleanings $120", "whitening $350"],
        "usp": ["Same-week appointments", "Over 15 years in business"],
        "offers": [
            {
                "id": "off_1",
                "text": "New patient exam for $29",
                "service_id": None,
                "valid_until": "2026-12-31",
                "terms": "Book within 30 days",
            }
        ],
    }
    business.add_knowledge(conn, rf.BIZ, knowledge, source="owner")
    angles = [
        "A new patient exam for $29.",  # an offer the owner stated
        "Book a new patient exam within 30 days.",  # its terms
        "Whitening for $350.",  # a price it states
        "Trusted for over 15 years.",  # a reason to choose it
        "Take $31 off a new patient exam.",  # the 31 of the date its offer ends
    ]
    rf.fake_llm(monkeypatch, researches=[research_fixture(offer_angles=angles)])
    rf.job(conn)
    _run(conn)
    offers = rf.run_row(conn)["brief_json"]["offer_angles"]
    assert [o["dropped"] for o in offers] == [None, None, None, None, "number"]


def test_what_the_business_offers_is_read_out_of_whatever_shape_was_stored(conn, world):
    """What a business stored can be any shape: a service that is no object, a list that is no list, an offer without
    words -- they say nothing, and reading them raises nothing (the run is built by then)."""
    g = research_material.gather(conn, config.load(), rf.BIZ, {})
    g.base["business"] = {
        "services": [{"name": "Cleanings", "price_hint": "$120", "intent_phrases": ["x 99"]}, "x", 7],
        "pricing_hints": ["cleanings $120", 5, None],
        "offers": [1, "x", {"text": "Exam for $29", "terms": None, "valid_until": "2026-12-31"}],
        "usp": [None, 3, "Same-week appointments"],
        "hours": "Mon-Fri 8-5",
        "phone": "650-555-0100",
        "goal_hint": {"monthly_budget_usd": 2000},
    }
    g.base["plan"] = {"offer": "First visit $59", "kept_out": ["free 12"], "searches": ["x 34"]}
    assert json.loads(researcher._offered(g)) == {
        "services": [["Cleanings", "$120"]],
        "prices": ["cleanings $120"],
        "offers": [["Exam for $29", None]],
        "reasons": ["Same-week appointments"],
        "plan_offer": "First visit $59",
    }
    for odd in (None, "x", {"services": "x", "pricing_hints": 5, "offers": {"text": "x"}, "usp": "x"}):
        g.base["business"], g.base["plan"] = odd, odd
        assert json.loads(researcher._offered(g)) == {
            "services": [],
            "prices": [],
            "offers": [],
            "reasons": [],
            "plan_offer": None,
        }


def test_a_count_the_material_gives_is_a_findings_number_never_an_offers(conn, world, monkeypatch):
    """The plan's radius (ten miles) may be stated in a finding about the area; an offer is what the business itself
    offers, and nothing says it offers anything within ten miles."""
    research = research_fixture(
        findings=[_finding("Nearby practices within 10 miles say they offer free whitening.", "page:1")],
        offer_angles=["Serves patients within 10 miles."],
    )
    rf.fake_llm(monkeypatch, researches=[research])
    rf.job(conn)
    _run(conn)
    b = rf.run_row(conn)["brief_json"]
    assert (b["findings"][0]["dropped"], b["offer_angles"][0]["dropped"]) == (None, "number")


def test_an_unknown_is_checked_against_the_sources_own_words_and_the_counts_alone(conn, monkeypatch):
    """An unknown may state a number the words of a source hold, or a count the material gives (six businesses found)
    -- not one that only an id holds (`place:3`, `page:3`), which the model reads in the list of sources."""
    monkeypatch.setenv("AGENT_ROLES", "validate")
    rf.seed(conn, knowledge={**knowledge_fixture().model_dump(), "listing": None})  # no point on the map: no distances
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    pages = {f"https://rival{k}.example": "Free whitening with every cleaning." for k in range(1, 11)}
    rf.fake_pages(monkeypatch, {**pages, "https://rival2.example": "New patient special: $49 exam and cleaning."})
    unknowns = [
        "Whether 3 practices nearby open on Sundays.",  # no source's words say 3
        "Whether the six practices found nearby open on Sundays.",  # the material gives six: the businesses it found
        "Whether the $49 special nearby covers X-rays.",  # a page's own words say $49
        "Whether any practice nearby opens on Sundays.",
    ]
    rf.fake_llm(monkeypatch, researches=[research_fixture(unknowns=unknowns)])
    rf.job(conn)
    _run(conn)
    unknown = rf.run_row(conn)["brief_json"]["unknowns"]
    assert [(u["text"], u["dropped"]) for u in unknown] == [
        (unknowns[0], "number"),
        (unknowns[1], None),
        (unknowns[2], None),
        (unknowns[3], None),
    ]


def test_a_distance_is_no_count_of_practices_an_unknown_may_state(conn, world, monkeypatch):
    """A business nearby is shown with how far it is: place:3's words are {"miles": 3.0}. That 3 is miles, not a count
    of practices, whatever number a rival happens to be away at -- so no place source's words license an unknown's
    number (a count the material gives still does)."""
    pages = {f"https://rival{k}.example": "Free whitening with every cleaning." for k in range(1, 11)}
    rf.fake_pages(monkeypatch, pages)  # no numeral on any page: the 3 is held by the distance alone
    unknowns = ["Whether 3 practices nearby open on Sundays.", "Whether the five practices shown open on Sundays."]
    rf.fake_llm(monkeypatch, researches=[research_fixture(unknowns=unknowns)])
    rf.job(conn)
    _run(conn)
    assert [u["dropped"] for u in rf.run_row(conn)["brief_json"]["unknowns"]] == ["number", None]


def test_a_search_with_a_phone_number_is_refused_as_a_sentence_with_one_is(conn, world, monkeypatch):
    """The words the model is given for a refused search say a phone number is one: a rival's, in a search to aim for,
    names it as surely as its name does (so does a bare @, a handle)."""
    research = research_fixture(
        suggested_themes=[
            "dentist 650-555-0100",
            "(650) 555 0100 dentist",
            "dentist 650.555.0100",
            "dentist near 94301",
        ],
        suggested_negatives=["dental jobs @brightsmile"],
    )
    rf.fake_llm(monkeypatch, researches=[research])
    rf.job(conn)
    _run(conn)
    b = rf.run_row(conn)["brief_json"]
    assert [t["dropped"] for t in b["suggested_themes"]] == ["link", "link", "link", None]  # a zip code is no phone
    assert b["suggested_negatives"] == [{"text": "dental jobs @brightsmile", "dropped": "link"}]


def test_each_kind_of_sentence_has_its_own_limits_and_the_owner_guards(conn, world, monkeypatch):
    def padded(n: int) -> str:  # one sentence of exactly n characters
        return "Nearby " + "x" * (n - len("Nearby "))

    two = "Nearby practices are listed. Several have websites."
    three = f"{two} A few have none."
    research = research_fixture(
        findings=[
            *(_finding(t, "business") for t in (two, three, padded(300), padded(301))),
            _finding("See www.brightsmile.example for hours.", "business"),
            _finding("Nearby practices run a campaign for whitening.", "business"),
            _finding("   ", "business"),
        ],
        offer_angles=[two, three, padded(200), padded(201)],
        unknowns=[padded(200), two, padded(201)],
    )
    rf.fake_llm(monkeypatch, researches=[research])
    rf.job(conn)
    _run(conn)
    b = rf.run_row(conn)["brief_json"]
    assert [f["dropped"] for f in b["findings"]] == [None, "long", None, "long", "link", "jargon", "empty"]
    assert [a["dropped"] for a in b["offer_angles"]] == [None, "long", None, "long"]
    assert [u["dropped"] for u in b["unknowns"]] == [None, "long", "long"]


def test_research_that_says_the_material_gives_nothing_true_to_say_shows_nothing(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch, researches=[research_fixture(grounded=False)])
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert b["grounded"] is False
    assert {x["dropped"] for x in b["findings"] + b["offer_angles"] + b["unknowns"]} == {"ungrounded"}
    assert (row["rounds"], row["dropped"]) == (2, "empty")
    assert researcher.owner_view(config.load(), row) is None


def test_a_search_is_kept_only_as_short_plain_words_that_name_no_one_and_use_no_jargon(conn, world, monkeypatch):
    """The searches a customer types, or the ads keep out: lower case and single-spaced, at most SEARCH_CHARS long, with
    no link, address or markup, no business nearby named and no advertising jargon -- each dropped one says why, and the
    same search twice is one."""
    stay, too_long = "x" * researcher.SEARCH_CHARS, "x" * (researcher.SEARCH_CHARS + 1)
    research = research_fixture(
        suggested_themes=[
            "  Invisalign   PALO Alto ",
            "invisalign palo alto",
            "www.brightsmile.example dentist",
            "dentist@example.com",
            "<b>dentist</b>",
            stay,
            too_long,
            "keyword research for dentists",
            "   ",
            "rival dental 2 palo alto",
        ],
        suggested_negatives=["https://example.com/dentist", "campaign jobs", "dental jobs", "Dental  Jobs"],
    )
    rf.fake_llm(monkeypatch, researches=[research])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert b["suggested_themes"] == [
        {"text": "invisalign palo alto", "dropped": None},
        {"text": "www.brightsmile.example dentist", "dropped": "link"},
        {"text": "dentist@example.com", "dropped": "link"},
        {"text": "<b>dentist</b>", "dropped": "link"},
        {"text": stay, "dropped": None},
        {"text": too_long, "dropped": "long"},
        {"text": "keyword research for dentists", "dropped": "jargon"},
        {"text": "", "dropped": "empty"},
        {"text": f"{MASK} palo alto", "dropped": "named"},
    ]
    assert b["suggested_negatives"] == [
        {"text": "https://example.com/dentist", "dropped": "link"},
        {"text": "campaign jobs", "dropped": "jargon"},
        {"text": "dental jobs", "dropped": None},
    ]
    problems = row["review"]["rounds"][0]["problems"]
    assert (
        "search to aim for 'dentist@example.com': it carried a link, an address, a phone number or markup" in problems
    )
    assert "search to keep out 'campaign jobs': it used advertising jargon" in problems
    assert "search to aim for '': it was empty" in problems


def test_the_research_is_cut_to_the_most_the_planner_reads(conn, world, monkeypatch):
    letters = "abcdefghijkl"
    many = research_fixture(
        findings=[_finding(f"Nearby practice {c} is on the map.", "place:1") for c in letters[:10]],
        suggested_themes=[f"theme {c}" for c in letters],
        suggested_negatives=[f"negative {c}" for c in letters],
        offer_angles=[f"Offer {c} for cleanings." for c in letters[:7]],
        unknowns=[f"Question {c} is open." for c in letters[:7]],
    )
    rf.fake_llm(monkeypatch, researches=[many])
    rf.job(conn)
    _run(conn)
    b = rf.run_row(conn)["brief_json"]
    most = (researcher.MAX_FINDINGS, researcher.MAX_SEARCHES, researcher.MAX_ANGLES, researcher.MAX_UNKNOWNS)
    assert most == (8, 10, 5, 5)
    assert [f["text"] for f in b["findings"]] == [f"Nearby practice {c} is on the map." for c in "abcdefgh"]
    assert [x["text"] for x in b["suggested_themes"]] == [f"theme {c}" for c in "abcdefghij"]
    assert [x["text"] for x in b["suggested_negatives"]] == [f"negative {c}" for c in "abcdefghij"]
    assert [x["text"] for x in b["offer_angles"]] == [f"Offer {c} for cleanings." for c in "abcde"]
    assert [x["text"] for x in b["unknowns"]] == [f"Question {c} is open." for c in "abcde"]


def test_a_nearby_business_named_is_dropped_everywhere(conn, world, monkeypatch):
    named = research_fixture(
        findings=[
            _finding("Rival Dental 2 offers free whitening with every cleaning.", "page:2"),
            *research_fixture().findings,
        ],
        suggested_themes=["rival dental 2 palo alto", "invisalign palo alto"],
        offer_angles=["Unlike Rival Dental 3, same-week appointments.", "Same-week appointments for cleanings."],
    )
    gen = rf.fake_llm(
        monkeypatch,
        researches=[named],
        research_reviews=[research_review_fixture(low={"relevant": 2}), research_review_fixture()],
    )
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert b["findings"][0]["dropped"] == "named" and b["offer_angles"][0]["dropped"] == "named"
    assert b["suggested_themes"][0] == {"text": f"{MASK} palo alto", "dropped": "named"}  # R6: kept without the name
    view = researcher.owner_view(config.load(), row)
    assert "Rival" not in str(view) and view["searches_to_aim_for"] == ["invisalign palo alto"]
    assert view["offer_angles"] == ["Same-week appointments for cleanings."]
    assert [f["text"] for f in view["findings"]] == [f.text for f in research_fixture().findings]
    # the second round is told what it named a business nearby in, never the name (the re-review's N1)
    feedback = gen.research_calls[1]["feedback"]
    assert "finding 1: it named a business nearby; describe it instead" in feedback
    assert not any(
        "Rival" in f for f in feedback
    ) and "search to aim for '[a business nearby] palo alto': it named" in (" ".join(feedback))


def test_research_that_mentions_admiry_is_dropped_and_fed_back_wherever_it_is(conn, world, monkeypatch):
    """Founder ruling 2026-10-09 (the B0 R3 boundary): research never says whether a business nearby uses Admiry, so a
    finding, a search, an offer angle or an unknown that mentions Admiry -- any case, a whole word -- is dropped as
    named and fed back; the rest is shown."""
    said = "it named Admiry, which research never mentions"
    mentions = research_fixture(
        findings=[_finding("Two nearby practices run their ads with Admiry.", "page:1"), *research_fixture().findings],
        suggested_themes=["admiry dentist palo alto", "invisalign palo alto"],
        suggested_negatives=["Admiry.ai jobs", "dental jobs"],
        offer_angles=["Unlike practices on ADMIRY, same-week appointments.", "Same-week appointments for cleanings."],
        unknowns=["Whether a practice nearby uses Admiry's pages.", "How busy the clinics nearby are on weekends."],
    )
    gen = rf.fake_llm(
        monkeypatch,
        researches=[mentions],
        research_reviews=[research_review_fixture(low={"relevant": 2}), research_review_fixture()],
    )
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    for key in ("findings", "suggested_themes", "suggested_negatives", "offer_angles", "unknowns"):
        assert [x["dropped"] for x in b[key]][:2] == ["named", None], key
    feedback = gen.research_calls[1]["feedback"]
    assert (
        f"finding 1: {said}" in feedback and f"offer angle 1: {said}" in feedback and f"unknown 1: {said}" in feedback
    )
    assert f"search to aim for 'admiry dentist palo alto': {said}" in feedback
    assert f"search to keep out 'admiry.ai jobs': {said}" in feedback
    view = researcher.owner_view(config.load(), row)
    said_to_owner = [f["text"] for f in view["findings"]] + [
        x for key in ("searches_to_aim_for", "searches_to_keep_out", "offer_angles", "unknowns") for x in view[key]
    ]
    assert not [x for x in said_to_owner if "admiry" in x.lower()]  # (the business's own source is labelled by name)
    assert view["searches_to_aim_for"] == ["invisalign palo alto"] and view["searches_to_keep_out"] == ["dental jobs"]


def test_a_run_the_critic_finds_still_naming_a_business_nearby_is_not_delivered(conn, world, monkeypatch):
    """The final review's I1: the Critic's serious `unnamed` check is the backstop for a name the code's forms do not
    know (a short brand learnt from a page): it sends the research back, and research that still names one after the
    second round stays with Admiry's team."""
    unnamed = research_review_fixture(failed={"grounded": ["unnamed"]})
    gen = rf.fake_llm(monkeypatch, research_reviews=[unnamed, unnamed])
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    assert (row["status"], row["rounds"], row["verdict"], row["dropped"]) == ("done", 2, "revise", "revise")
    assert "grounded: grounded note" in gen.research_calls[1]["feedback"]
    assert row["review"]["final"]["issues"] == [
        {"dimension": "grounded", "text": "grounded note", "failed": ["unnamed"]}
    ]
    assert researcher.owner_view(config.load(), row) is None


def test_a_one_word_name_first_in_a_sentence_is_dropped_and_kept_nowhere_a_run_stores(conn, world, monkeypatch):
    """The re-review's N1: a business nearby called "Zenith", or "Sonrisa Dental" (whose one word is "Sonrisa"), named
    first in a sentence is dropped and masked -- and neither the guard's feedback nor the Critic's notes keep the name,
    however they write it ("(Sonrisa)", "Zenith is named ...")."""
    one_word = [
        places.Candidate("ChIJz", "Zenith", "9 Main St, Palo Alto, CA", 37.45, -122.143),
        places.Candidate("ChIJs", "Sonrisa Dental", "8 Main St, Palo Alto, CA", 37.45, -122.143),
    ]
    rf.fake_places(monkeypatch, [*rf.nearby(2), *one_word])
    named = research_fixture(
        findings=[
            _finding("Zenith is open on Saturdays.", "place:1"),
            _finding("Sonrisa offers free exams.", "place:1"),
            *research_fixture().findings,
        ]
    )
    judge = models.ResearchReview(
        grades=[
            models.ResearchGrade(
                dimension=d,
                failed=[],
                grade=2 if d == "relevant" else 5,
                note="Zenith is named in finding 1." if d == "relevant" else f"{d} note",
            )
            for d in contracts.RESEARCH.ids
        ],
        missing=["the website of (Sonrisa)"],
    )
    gen = rf.fake_llm(monkeypatch, researches=[named], research_reviews=[judge, research_review_fixture()])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert [(f["text"], f["dropped"]) for f in b["findings"][:2]] == [
        (f"{MASK} is open on Saturdays.", "named"),
        (f"{MASK} offers free exams.", "named"),
    ]
    first = row["review"]["rounds"][0]
    assert first["issues"] == [{"dimension": "relevant", "text": f"{MASK} is named in finding 1."}]
    assert first["missing"] == [f"the website of ({MASK})"]
    assert "finding 2: it named a business nearby; describe it instead" in first["problems"]
    assert not {"zenith", "sonrisa"} & set(research_material.words(_remembered(row)))
    assert not any("Zenith" in f or "Sonrisa" in f for f in gen.research_calls[1]["feedback"])


def test_a_dropped_sentence_or_search_keeps_no_nearby_business_name(conn, world, monkeypatch):
    """R6 (D5: a name lives in memory for one run): a sentence or a search the guards drop is kept for the team 13
    months with every name of a business nearby masked -- whatever the reason it was dropped for (no source, too long)
    -- and so is what the Critic wrote of it and the feedback the second round read."""
    long_search = "rival dental 3 " + "dentist near me " * 5
    named = research_fixture(
        findings=[
            _finding("Rival Dental 2 offers free whitening with every cleaning.", "page:2"),
            _finding("Rival Dental 4 opened a second office.", "place:9"),
            *research_fixture().findings,
        ],
        suggested_themes=["rival dental 2 palo alto", long_search, "invisalign palo alto"],
        suggested_negatives=["rival dental 5 reviews"],
        offer_angles=["Unlike Rival Dental 3, same-week appointments."],
        unknowns=["Does Rival Dental 6 take walk-ins?"],
    )
    judge = models.ResearchReview(
        grades=[
            models.ResearchGrade(
                dimension=d,
                failed=[],
                grade=2 if d == "relevant" else 5,
                note="Finding 1 is about Rival Dental 2" if d == "relevant" else f"{d} note",
            )
            for d in contracts.RESEARCH.ids
        ],
        missing=["the website of Rival Dental 4"],
    )
    rf.fake_llm(monkeypatch, researches=[named], research_reviews=[judge, research_review_fixture()])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert b["findings"][0] == {
        "text": f"{MASK} offers free whitening with every cleaning.",
        "source_ids": ["page:2"],
        "confidence": "medium",
        "dropped": "named",
    }
    assert (b["findings"][1]["text"], b["findings"][1]["dropped"]) == (f"{MASK} opened a second office.", "unsourced")
    assert b["suggested_themes"][1] == {"text": " ".join([MASK, *["dentist near me"] * 5]), "dropped": "long"}
    assert b["suggested_negatives"] == [{"text": f"{MASK} reviews", "dropped": "named"}]
    assert b["offer_angles"] == [{"text": f"Unlike {MASK}, same-week appointments.", "dropped": "named"}]
    assert b["unknowns"] == [{"text": f"Does {MASK} take walk-ins?", "dropped": "named"}]
    first = row["review"]["rounds"][0]
    assert first["issues"] == [{"dimension": "relevant", "text": f"Finding 1 is about {MASK}"}]
    assert first["missing"] == [f"the website of {MASK}"]
    assert "finding 1: it named a business nearby; describe it instead" in first["problems"]
    assert "rival" not in _remembered(row)  # nowhere in the research, the review, the digest or the brief asked


def test_a_website_of_a_business_nearby_is_masked_like_its_name_in_all_a_run_keeps(conn, world, monkeypatch):
    """K4 Task 4 fix round 1 (review item 8; D5): the Critic can write a rival's website into what it says is missing or
    into a note, and a host names a business as its name does -- kept with the same mask, with or without `www.`."""
    judge = models.ResearchReview(
        grades=[
            models.ResearchGrade(
                dimension=d,
                failed=[],
                grade=2 if d == "relevant" else 5,
                note="Mentions Rival Dental 3 at rival3.example." if d == "relevant" else f"{d} note",
            )
            for d in contracts.RESEARCH.ids
        ],
        missing=["rival3.example hours", "the prices on WWW.Rival1.Example/fees"],
    )
    rf.fake_llm(monkeypatch, research_reviews=[judge, research_review_fixture()])
    rf.job(conn)
    _run(conn)
    row = rf.run_row(conn)
    first = row["review"]["rounds"][0]
    assert first["missing"] == [f"{MASK} hours", f"the prices on {MASK}/fees"]
    assert first["issues"] == [{"dimension": "relevant", "text": f"Mentions {MASK} at {MASK}."}]
    remembered = _remembered(row)
    assert "rival" not in remembered and "example" not in remembered  # no name, no host, anywhere a run keeps


def test_a_sentence_or_search_naming_a_rivals_website_is_dropped_like_one_naming_its_name(conn, world, monkeypatch):
    """K4 Task 4 fix round 2 (re-review): a bare host (`rival3.example`, no scheme, no `www.`) passes the link guard, and
    kept it was a junk keyword for the owner and the planner. A host names a business: a sentence or a search that holds
    one as a host is written is dropped (`named`) and fed back, wherever else it is a link -- and what is kept of it has
    the host masked."""
    named = research_fixture(
        findings=[
            _finding("Visit www.Rival3.Example for prices.", "page:2"),
            *research_fixture().findings,
        ],
        suggested_themes=["rival3.example near me", "invisalign palo alto"],
        suggested_negatives=["www.rival1.example jobs", "dental jobs"],
        offer_angles=["Unlike rival2.example, same-week appointments.", "Same-week appointments for cleanings."],
        unknowns=[
            "Does rival4 . example open on Saturdays?",
            "How many people nearby search for Invisalign each month.",
        ],
    )
    gen = rf.fake_llm(
        monkeypatch,
        researches=[named],
        research_reviews=[research_review_fixture(low={"relevant": 2}), research_review_fixture()],
    )
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert b["findings"][0] == {
        "text": f"Visit {MASK} for prices.",
        "source_ids": ["page:2"],
        "confidence": "medium",
        "dropped": "named",
    }
    assert b["suggested_themes"][:2] == [
        {"text": f"{MASK} near me", "dropped": "named"},
        {"text": "invisalign palo alto", "dropped": None},
    ]
    # a rival's address is named before it is a link: it is the business it names that is the fault, not the form
    assert b["suggested_negatives"] == [
        {"text": f"{MASK} jobs", "dropped": "named"},
        {"text": "dental jobs", "dropped": None},
    ]
    assert b["offer_angles"][0] == {"text": f"Unlike {MASK}, same-week appointments.", "dropped": "named"}
    assert b["unknowns"][0] == {"text": f"Does {MASK} open on Saturdays?", "dropped": "named"}
    view = researcher.owner_view(config.load(), row)
    assert view["searches_to_aim_for"] == ["invisalign palo alto"] and view["searches_to_keep_out"] == ["dental jobs"]
    assert view["offer_angles"] == ["Same-week appointments for cleanings."]
    assert "rival" not in str(view) and "example" not in str(view)
    feedback = gen.research_calls[1]["feedback"]  # what it named in, never the name or the host (the re-review's N1)
    assert "finding 1: it named a business nearby; describe it instead" in feedback
    assert f"search to aim for '{MASK} near me': it named a business nearby" in feedback
    assert f"search to keep out '{MASK} jobs': it named a business nearby" in feedback
    assert "offer angle 1: it named a business nearby; describe it instead" in feedback
    assert "unknown 1: it named a business nearby; describe it instead" in feedback
    assert not any("rival" in f or "example" in f for f in feedback)
    first = row["review"]["rounds"][0]
    assert "finding 1: it named a business nearby; describe it instead" in first["problems"]
    remembered = _remembered(row)
    assert "rival" not in remembered and "example" not in remembered  # no host in anything a run keeps


def test_what_is_kept_of_a_text_masks_a_host_before_a_name_that_is_part_of_it():
    """K4 Task 4 fix round 2: a business called Gentle whose website is gentle.dental is masked whole, not as
    `[a business nearby].dental`."""
    kept = researcher._unnamed(
        {"missing": ["see gentle.dental/x, and call Gentle"]},
        names=research_material.names_of(["Gentle"]),
        hosts=("gentle.dental",),
    )
    assert kept == {"missing": [f"see {MASK}/x, and call {MASK}"]}


def test_the_words_of_a_rivals_host_are_no_host_so_a_search_made_of_them_is_kept(conn, world, monkeypatch):
    """K4 Task 4 fix round 2 (re-review): a practice whose website is gentle.dental is named by that address, not by two
    common words -- `gentle dental care` is kept as written, and `gentle.dental` is dropped."""
    gentle = places.Candidate(
        "ChIJgd", "Smile Studio", "4 Main St, Palo Alto, CA", 37.4419, -122.143, website="https://www.gentle.dental/"
    )
    rf.fake_places(monkeypatch, [*rf.nearby(), gentle])
    research = research_fixture(
        findings=[
            _finding("Gentle dental care is what nearby practices talk about.", "page:1"),
            *research_fixture().findings,
        ],
        suggested_themes=["gentle dental care palo alto", "gentle.dental near me", "invisalign palo alto"],
    )
    rf.fake_llm(monkeypatch, researches=[research])
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    b = row["brief_json"]
    assert b["findings"][0] == {
        "text": "Gentle dental care is what nearby practices talk about.",
        "source_ids": ["page:1"],
        "confidence": "medium",
        "dropped": None,
    }
    assert b["suggested_themes"] == [
        {"text": "gentle dental care palo alto", "dropped": None},
        {"text": f"{MASK} near me", "dropped": "named"},
        {"text": "invisalign palo alto", "dropped": None},
    ]
    view = researcher.owner_view(config.load(), row)
    assert view["searches_to_aim_for"] == ["gentle dental care palo alto", "invisalign palo alto"]
    assert view["findings"][0]["text"] == "Gentle dental care is what nearby practices talk about."


def test_a_competitors_own_page_alone_never_makes_a_finding_high(conn, world, monkeypatch):
    """What a business says of itself is its claim, not a fact: resting on pages and listings alone, a finding is at
    most medium; beside a published figure it keeps what the model gave. The first finding counts as the prompt does:
    its two are the businesses its own sources are from, its five those shown (the final review's I2 restores the
    phrasing pre-flight F29 had reworded) -- no page holds a numeral, so the guard passes it on that rule alone."""
    rf.fake_pages(
        monkeypatch, {f"https://rival{k}.example": "Free whitening with every cleaning." for k in range(1, 11)}
    )
    sure = research_fixture(
        findings=[
            _finding(
                "Two of the five nearby practices offer free whitening with every cleaning.",
                "page:1",
                "page:2",
                confidence="high",
            ),
            _finding("A nearby practice is listed on the map.", "place:1", confidence="high"),
            _finding(
                "Nearby practices advertise free whitening; this business advertises same-week appointments.",
                "page:1",
                "business",
                confidence="high",
            ),
        ]
    )
    rf.fake_llm(monkeypatch, researches=[sure])
    rf.job(conn)
    _run(conn)
    findings = rf.run_row(conn)["brief_json"]["findings"]
    assert [(f["confidence"], f["dropped"]) for f in findings] == [("medium", None), ("medium", None), ("high", None)]


def test_a_finding_that_cites_a_page_shows_its_owner_the_place_not_the_business(conn, world, monkeypatch):
    """R3 (D5, pre-flight F1): a source an owner reads is described from its place and links to it on Google Maps --
    never the business's name, its page's title or its website."""
    cites = research_fixture(
        findings=[_finding("A nearby practice offers free whitening with every cleaning.", "page:1", "place:1")]
    )
    rf.fake_llm(monkeypatch, researches=[cites])
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    view = researcher.owner_view(config.load(), rf.run_row(conn))
    [finding] = view["findings"]
    page, place = finding["sources"]
    assert (page["id"], page["kind"], place["id"], place["kind"]) == ("page:1", "page", "place:1", "place")
    assert page["label"].startswith(research_material.NEARBY_PAGE_LABEL)
    assert place["label"].startswith(research_material.NEARBY_LABEL)
    assert page["url"] == place["url"] == places.maps_url(None, "ChIJ1")
    shown = json.dumps(view)
    for leak in ("Rival", "DDS", "example", "rival"):  # a name, a page's title, a website
        assert leak not in shown, leak


def test_a_busy_model_fails_the_run_and_the_job_waits_for_the_next_hour(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch, research_error=BUSY)
    jid = rf.job(conn)
    assert _run(conn)["retried"] == 1
    row = rf.run_row(conn)
    assert (row["status"], row["dropped"], row["calls"]) == ("failed", "llm", 1)
    assert conn.execute("SELECT status FROM agent_job WHERE id = %s", (jid,)).fetchone()["status"] == "queued"
    gen.research_error = None
    assert _run(conn)["done"] == 1
    again = rf.run_row(conn)
    assert (again["id"], again["status"]) == (row["id"], "done")


def test_without_a_model_the_run_fails_before_any_lookup_or_crawl(conn, world, monkeypatch):
    """R1 (pre-flight F8): a run that cannot call a model must not spend the day's Places lookups nor crawl a stranger's
    site -- each of the job's attempts would."""
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    tried = rf.fake_pages(monkeypatch)
    jid = rf.job(conn)
    for _ in range(runner.ATTEMPTS - 1):
        assert _run(conn)["retried"] == 1
    assert _run(conn)["failed"] == 1
    assert world == [] and tried == []  # no Places search, no page fetched, in any attempt
    row = rf.run_row(conn)
    assert (row["status"], row["dropped"], row["calls"], row["rounds"], row["materials_digest"]) == (
        "failed",
        "llm",
        0,
        0,
        {},
    )
    assert conn.execute("SELECT status FROM agent_job WHERE id = %s", (jid,)).fetchone()["status"] == "failed"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'places.lookup'").fetchone()["n"] == 0
    assert runner.runs_today(conn, rf.BIZ) == 0


def test_a_retried_job_starts_its_row_again_with_the_brief_it_holds_now(conn, world, monkeypatch):
    """R2 (pre-flight F25): enqueue replaces a waiting job's brief; the attempt that follows is asked that, not what the
    earlier attempt was."""
    gen = rf.fake_llm(monkeypatch, research_error=BUSY)
    rf.job(conn, topics=["competitors"], note="first")
    _run(conn)
    first = rf.run_row(conn)
    assert (first["brief"], first["trigger"], first["vertical"]) == (
        {"topics": ["competitors"], "note": "first"},
        "operator",
        "dentists",
    )
    brief = {"trigger": "owner", "topics": ["seasonality"], "note": "second"}
    queue.enqueue(conn, business_id=rf.BIZ, role="researcher", kind="business", brief=brief)
    moved = {**knowledge_fixture().model_dump(), "listing": rf.LISTING, "category": "attorneys"}
    business.add_knowledge(conn, rf.BIZ, moved, source="owner")
    gen.research_error = None
    assert _run(conn)["done"] == 1
    again = rf.run_row(conn)
    assert again["id"] == first["id"] and again["status"] == "done"
    assert (again["brief"], again["trigger"], again["vertical"]) == (
        {"topics": ["seasonality"], "note": "second"},
        "owner",
        "attorneys",
    )
    assert [t["id"] for t in gen.research_calls[-1]["material"]["topics"]] == ["seasonality"]


def test_a_retried_job_starts_its_row_again_clean(conn, world, monkeypatch):
    """The earlier attempt's outcome is not the new attempt's while it builds: nothing says it failed, or when."""
    gen = rf.fake_llm(monkeypatch, research_error=BUSY)
    rf.job(conn)
    _run(conn)
    failed = rf.run_row(conn)
    assert (failed["status"], failed["dropped"]) == ("failed", "llm") and failed["finished_at"] is not None
    seen: list[dict] = []
    real = researcher._build

    def watch(conn, settings, job, row):
        seen.append(dict(row))
        return real(conn, settings, job, row)

    monkeypatch.setattr(researcher, "_build", watch)
    gen.research_error = None
    _run(conn)
    [started] = seen
    assert (started["id"], started["status"], started["dropped"], started["finished_at"]) == (
        failed["id"],
        "building",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("asked", "stored"),
    [("owner", "owner"), ("operator", "operator"), ("auto", "auto"), ("sneaky", "auto"), (None, "auto"), (7, "auto")],
)
def test_a_trigger_the_run_does_not_know_is_stored_as_auto(conn, world, monkeypatch, asked, stored):
    rf.fake_llm(monkeypatch)
    queue.enqueue(conn, business_id=rf.BIZ, role="researcher", kind="business", brief={"trigger": asked})
    _run(conn)
    assert rf.run_row(conn)["trigger"] == stored


def test_without_the_critic_the_research_stays_with_the_team(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch, research_review_error=BUSY)
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    row = rf.run_row(conn)
    assert (row["status"], row["verdict"], row["dropped"], row["confidence"]) == ("done", None, "review", "low")
    [only] = row["review"]["rounds"]
    assert (only["round"], only["verdict"], only["score"], row["review"]["final"]) == (1, None, None, None)
    assert row["brief_json"]["findings"] and (row["rounds"], row["calls"]) == (1, 2)  # what it wrote is kept
    assert researcher.owner_view(config.load(), row) is None


def test_a_second_round_the_model_cannot_write_ends_the_run_with_the_first(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch, research_reviews=[research_review_fixture(low={"numbers": 2})])
    rf.job(conn)
    real = gen.write_research

    def second_fails(material, pages, feedback=None):
        if feedback is not None:
            raise llm.LLMError(BUSY)
        return real(material, pages, feedback)

    gen.write_research = second_fails
    _run(conn)
    row = rf.run_row(conn)
    assert (row["status"], row["rounds"], row["calls"], row["verdict"], row["dropped"]) == (
        "done",
        1,
        3,
        "revise",
        "revise",
    )


def test_a_delivered_run_with_no_finding_the_guards_passed_is_the_teams(conn, world, monkeypatch):
    nothing = research_fixture(findings=[_finding("Dentists here pay about $45 a lead.", "business")])
    rf.fake_llm(monkeypatch, researches=[nothing])
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)  # the Critic delivers every round, and every round the only finding is dropped
    row = rf.run_row(conn)
    assert (row["rounds"], row["verdict"], row["dropped"], row["confidence"]) == (2, "deliver", "empty", "low")
    assert row["review"]["rounds"][0]["problems"][-1].startswith("no finding could be shown")
    assert researcher.owner_view(config.load(), row) is None


def test_a_business_past_its_background_calls_is_skipped(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch)
    for _ in range(runner.ROLE_RUNS_PER_DAY - 3):  # three left: a run needs four
        audit.record_job(
            conn, job="research-run", tool="job.researcher", business_id=None, args={"business": rf.BIZ}, result={}
        )
    rf.job(conn)
    assert _run(conn)["skipped"] == 1 and gen.research_calls == []
    assert conn.execute("SELECT result FROM agent_job").fetchone()["result"] == {"reason": "capped"}


def test_a_business_nothing_is_known_of_is_skipped(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "validate")
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'claimed')", (rf.BIZ,))
    gen = rf.fake_llm(monkeypatch)
    rf.job(conn)
    assert _run(conn)["skipped"] == 1 and gen.research_calls == []
    assert conn.execute("SELECT result FROM agent_job").fetchone()["result"] == {"reason": "no knowledge"}
    assert rf.run_row(conn) is None


def test_the_researchers_calls_count_toward_the_business_day_and_name_no_business(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch)
    rf.job(conn)
    _run(conn)
    rows = conn.execute(
        "SELECT business_id, args_redacted FROM agent_action WHERE tool = 'job.researcher' ORDER BY id"
    ).fetchall()
    assert [r["business_id"] for r in rows] == [None, None]
    assert [(r["args_redacted"]["business"], r["args_redacted"]["call"]) for r in rows] == [
        (rf.BIZ, "write"),
        (rf.BIZ, "review"),
    ]
    assert runner.runs_today(conn, rf.BIZ) == 2


def test_the_researcher_asks_at_its_effort_and_the_critic_at_the_default(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch)
    asked: list = []
    monkeypatch.setattr(llm, "generator", lambda settings=None, effort=None: asked.append(effort) or gen)
    rf.job(conn)
    _run(conn)
    assert researcher.EFFORT == "medium" and asked == ["medium", None]  # the Researcher's, then the Critic's


def _untouched(conn, job_id: str) -> bool:
    """Whether nothing ever claimed the job: a waiting job's `updated_at` is its `created_at` until something does."""
    return conn.execute(
        "SELECT status = 'queued' AND rounds = 0 AND updated_at = created_at AS untouched FROM agent_job WHERE id = %s",
        (job_id,),
    ).fetchone()["untouched"]


def test_research_stops_at_its_time_and_leaves_the_rest_waiting(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch)
    monkeypatch.setattr(researcher, "RUN_SECONDS", 0)
    jid = rf.job(conn)
    assert _run(conn)["left"] == 1 and gen.research_calls == []
    job = conn.execute("SELECT status, rounds FROM agent_job WHERE id = %s", (jid,)).fetchone()
    assert (job["status"], job["rounds"]) == ("queued", 0) and _untouched(conn, jid)  # never even claimed


def test_research_runs_job_after_job_until_its_time_is_up(conn, world, monkeypatch):
    rf.seed(conn, "biz_2")
    gen = rf.fake_llm(monkeypatch)
    first, second = rf.job(conn), rf.job(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (first,))
    # research_run reads the clock through the same `runner.time`: twice for its own Reviewer pass first (L1;
    # nothing is queued for it here, so it reads the clock and finds no work), then twice for the Researcher's --
    # when its pass began, and when the first job was due.
    readings = iter([0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(
        runner, "time", types.SimpleNamespace(monotonic=lambda: next(readings, researcher.RUN_SECONDS + 1.0))
    )
    out = _run(conn)
    assert (out["done"], out["left"]) == (1, 1) and len(gen.research_calls) == 1
    rows = {r["id"]: (r["status"], r["rounds"]) for r in conn.execute("SELECT id, status, rounds FROM agent_job")}
    assert rows == {first: ("done", 1), second: ("queued", 0)} and _untouched(conn, second)


def test_a_run_that_dies_with_a_job_in_hand_loses_that_job_only(conn, world, monkeypatch):
    """A container recreated while research-run runs (prod runs the tick through `docker compose exec`) must not take
    every waiting job with it: jobs claimed ahead of their turn would stay `running` and fail for good as stale two
    hours later. The jobs are taken one at a time, so only the one in hand is lost."""
    rf.seed(conn, "biz_2")
    first, second = rf.job(conn), rf.job(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (first,))

    def killed(conn, settings, job):
        raise KeyboardInterrupt  # not an Exception, so _handle lets it through, as it would the process going away

    monkeypatch.setitem(runner.HANDLERS, ("researcher", "business"), killed)
    with pytest.raises(KeyboardInterrupt):
        _run(conn)
    rows = {r["id"]: (r["status"], r["rounds"]) for r in conn.execute("SELECT id, status, rounds FROM agent_job")}
    assert rows == {first: ("running", 1), second: ("queued", 0)} and _untouched(conn, second)


def test_a_job_the_model_could_not_answer_waits_for_the_next_run_not_the_next_claim(conn, world, monkeypatch):
    """Jobs are taken one at a time and a job tried again waits again: the same run must not take it back at once and
    spend its attempts (runner.ATTEMPTS) on a busy model."""
    rf.seed(conn, "biz_2")
    gen = rf.fake_llm(monkeypatch, research_error=BUSY)
    first, second = rf.job(conn), rf.job(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (first,))
    out = _run(conn)
    assert (out["retried"], out["failed"], out["done"], out["left"]) == (2, 0, 0, 0)  # each was asked once
    assert len(gen.research_calls) == 2
    rows = {r["id"]: (r["status"], r["rounds"]) for r in conn.execute("SELECT id, status, rounds FROM agent_job")}
    assert rows == {first: ("queued", 1), second: ("queued", 1)}


def test_the_critics_jobs_do_not_wait_on_the_researchers_time(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch)
    monkeypatch.setattr(researcher, "RUN_SECONDS", 0)
    monkeypatch.setitem(runner.HANDLERS, ("critic", "plan"), lambda conn, settings, job: (queue.DONE, {"seen": 1}))
    queue.enqueue(conn, business_id=rf.BIZ, role="critic", kind="plan", brief={})
    rf.job(conn)
    out = _run(conn)
    assert (out["done"], out["left"]) == (1, 1)
    statuses = {r["role"]: r["status"] for r in conn.execute("SELECT role, status FROM agent_job")}
    assert statuses == {"critic": "done", "researcher": "queued"}


def test_left_counts_the_research_jobs_waiting_and_no_other_work(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch)

    def unanswered(conn, settings, job):
        return queue.FAILED, {"reason": queue.MODEL_UNAVAILABLE}

    monkeypatch.setitem(runner.HANDLERS, ("critic", "plan"), unanswered)
    queue.enqueue(conn, business_id=rf.BIZ, role="critic", kind="plan", brief={})
    rf.job(conn)
    out = _run(conn)
    assert (out["retried"], out["done"], out["left"]) == (1, 1, 0)  # the Critic's job waits again; it is not research
    assert conn.execute("SELECT status FROM agent_job WHERE role = 'critic'").fetchone()["status"] == "queued"


def test_the_days_research_runs_are_capped(conn, world, monkeypatch):
    gen = rf.fake_llm(monkeypatch)
    for _ in range(researcher.MAX_RUNS_PER_DAY):
        rf.delivered(conn)
    jid = rf.job(conn)
    out = _run(conn)
    assert gen.research_calls == [] and out["left"] == 1  # it waits for tomorrow, and nothing even took it
    assert _untouched(conn, jid)


def test_the_runs_taking_the_days_last_slot_leave_the_next_job_waiting(conn, world, monkeypatch):
    rf.seed(conn, "biz_2")
    gen = rf.fake_llm(monkeypatch)
    monkeypatch.setattr(researcher, "MAX_RUNS_PER_DAY", 1)
    first, second = rf.job(conn), rf.job(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (first,))
    out = _run(conn)
    assert (out["done"], out["left"]) == (1, 1) and len(gen.research_calls) == 1
    assert _untouched(conn, second)


def test_a_run_takes_the_researchers_jobs_a_batch_at_most(conn, world, monkeypatch):
    rf.seed(conn, "biz_2")
    rf.fake_llm(monkeypatch)
    monkeypatch.setattr(researcher, "BATCH", 1)
    first, second = rf.job(conn), rf.job(conn, "biz_2")
    conn.execute("UPDATE agent_job SET created_at = now() - interval '1 minute' WHERE id = %s", (first,))
    out = _run(conn)
    assert (out["done"], out["left"]) == (1, 1)
    status = conn.execute("SELECT status FROM agent_job WHERE id = %s", (first,)).fetchone()["status"]
    assert status == "done" and _untouched(conn, second)


def test_a_stale_building_run_fails_and_a_thirteen_month_old_run_goes(conn, world, monkeypatch):
    old = rf.delivered(conn, days_ago=400)
    kept = rf.delivered(conn, days_ago=390)
    stuck = rf.delivered(conn)
    fresh = rf.delivered(conn)
    for run, started in ((stuck, "130 minutes"), (fresh, "110 minutes")):  # either side of STALE_AFTER (2 hours)
        conn.execute(
            "UPDATE research_run SET status = 'building', started_at = now() - %s::interval WHERE id = %s",
            (started, run),
        )
    monkeypatch.setenv("AGENT_ROLES", "off")
    out = _run(conn)
    assert (out["stale"], out["purged"], out["mode"]) == (1, 1, "off")  # each said apart: one failed, one gone
    row = conn.execute("SELECT status, dropped FROM research_run WHERE id = %s", (stuck,)).fetchone()
    assert (row["status"], row["dropped"]) == ("failed", "stale")
    assert conn.execute("SELECT status FROM research_run WHERE id = %s", (fresh,)).fetchone()["status"] == "building"
    assert conn.execute("SELECT 1 FROM research_run WHERE id = %s", (old,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM research_run WHERE id = %s", (kept,)).fetchone() is not None


def test_each_shard_purges_and_fails_only_its_own_businesses_runs(conn):
    def biz_in(shard: int) -> str:  # a business id that hashes into `shard` of 2
        for k in range(200):
            biz = f"biz_s{k}"
            if conn.execute("SELECT mod(abs(hashtext(%s)::bigint), 2) AS s", (biz,)).fetchone()["s"] == shard:
                return biz
        raise AssertionError("no id found")

    mine, theirs = biz_in(0), biz_in(1)
    for biz in (mine, theirs):
        rf.seed(conn, biz)
    old = {biz: rf.delivered(conn, biz, days_ago=400) for biz in (mine, theirs)}
    stuck = {biz: rf.delivered(conn, biz) for biz in (mine, theirs)}
    conn.execute(
        "UPDATE research_run SET status = 'building', started_at = now() - interval '3 hours' WHERE id = ANY(%s)",
        (list(stuck.values()),),
    )
    assert (researcher.fail_stale(conn, 0, 2), researcher.purge(conn, 0, 2)) == (1, 1)
    assert conn.execute("SELECT 1 FROM research_run WHERE id = %s", (old[mine],)).fetchone() is None
    assert conn.execute("SELECT 1 FROM research_run WHERE id = %s", (old[theirs],)).fetchone() is not None
    status = {biz: _stored(conn, stuck[biz])["status"] for biz in (mine, theirs)}
    assert status == {mine: "failed", theirs: "building"}
    assert (researcher.fail_stale(conn, 1, 2), researcher.purge(conn, 1, 2)) == (1, 1)
    assert _stored(conn, stuck[theirs])["status"] == "failed"


def test_an_unexpected_error_fails_the_run_and_the_job(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(researcher, "_guarded", broken)
    rf.job(conn)
    assert _run(conn)["failed"] == 1
    row = rf.run_row(conn)
    assert (row["status"], row["dropped"]) == ("failed", "error")
    assert conn.execute("SELECT error FROM agent_job").fetchone()["error"] == "RuntimeError: boom"


@pytest.mark.parametrize(
    ("stored", "error", "lookups"),
    [
        ({"listing": "x"}, "AttributeError", 1),  # a listing that is no object: found out after the lookup
        ({"listing": {**rf.LISTING, "lat": "north"}}, "TypeError", 1),  # a latitude that is no number: the same
        ({"service_area": 5}, "TypeError", 0),  # a service area that is no list: found out before it
    ],
    ids=["listing", "latitude", "service_area"],
)
def test_a_material_that_cannot_be_gathered_fails_the_run_and_the_job_is_not_tried_again(
    conn, monkeypatch, caplog, stored, error, lookups
):
    """R8: what a business stored can be any shape, and gather can raise on one after the day's Places lookup is spent.
    That is a failed run (`material`), never a crashed job and never another attempt at another lookup; the log says
    which kind of error and for which business, nothing of what was stored."""
    monkeypatch.setenv("AGENT_ROLES", "validate")
    rf.seed(conn, knowledge={**knowledge_fixture().model_dump(), "listing": rf.LISTING, **stored})
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    gen = rf.fake_llm(monkeypatch)
    jid = rf.job(conn)
    with caplog.at_level(logging.INFO):
        out = _run(conn)
    assert (out["failed"], out["retried"], out["done"]) == (1, 0, 0)
    row = rf.run_row(conn)
    assert (row["status"], row["dropped"], row["calls"], row["brief_json"]) == ("failed", "material", 0, None)
    job = conn.execute("SELECT status, result, error FROM agent_job WHERE id = %s", (jid,)).fetchone()
    assert (job["status"], job["result"], job["error"]) == (
        "failed",
        {"run": row["id"], "reason": "material"},
        None,
    )
    assert _run(conn)["failed"] == 0 and len(calls) == lookups  # no attempt waits to look up again
    assert gen.research_calls == []
    lines = [r.getMessage() for r in caplog.records if r.name == researcher.log.name]
    assert len(lines) == 1 and error in lines[0] and rf.BIZ in lines[0]
    assert all(word not in caplog.text for word in ("north", "Palo Alto", "Rival"))


def test_reading_more_that_fails_ends_the_run_failed_with_what_round_one_wrote(conn, world, monkeypatch, caplog):
    missing = research_review_fixture(low={"grounded": 2}, missing=["more businesses nearby"])
    gen = rf.fake_llm(monkeypatch, research_reviews=[missing])

    def broken(g):
        raise ValueError("secret-rival.example is down")

    monkeypatch.setattr(research_material, "more", broken)
    rf.job(conn)
    with caplog.at_level(logging.INFO):
        out = _run(conn)
    assert (out["failed"], out["retried"]) == (1, 0)
    row = rf.run_row(conn)
    assert (row["status"], row["dropped"], row["rounds"], row["calls"], row["confidence"]) == (
        "failed",
        "material",
        1,
        2,
        None,
    )
    assert row["brief_json"]["findings"] and [r["verdict"] for r in row["review"]["rounds"]] == ["fetch_more"]
    assert len(gen.research_calls) == 1
    assert "ValueError" in caplog.text and "secret-rival" not in caplog.text


def test_a_failed_run_is_never_read_by_an_owner_whatever_it_kept(conn, world, monkeypatch):
    rf.fake_llm(monkeypatch)
    rf.job(conn)
    monkeypatch.setenv("AGENT_ROLES", "on")
    _run(conn)
    rid = rf.run_row(conn)["id"]
    conn.execute("UPDATE research_run SET status = 'failed' WHERE id = %s", (rid,))
    assert researcher.owner_view(config.load(), _stored(conn, rid)) is None


def test_the_owner_view_is_what_the_guards_passed_with_the_sources_they_rest_on(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.seed(conn)
    row = _stored(conn, rf.delivered(conn))
    assert researcher.owner_view(config.load(), row) == {
        "day": row["created_at"].date().isoformat(),
        "topics": ["competitors"],
        "findings": [
            {
                "text": "A lead from search ads costs dentists about $72.97 on average.",
                "confidence": "medium",
                "sources": [
                    {
                        "id": "benchmark:dentists:google_search",
                        "kind": "benchmark",
                        "label": "Published cost-per-lead averages for dentists on Google Search (2026)",
                        "url": benchmark.LOCALIQ_SEARCH,
                    }
                ],
            }
        ],
        "searches_to_aim_for": ["invisalign palo alto"],
        "searches_to_keep_out": ["dental jobs"],
        "offer_angles": ["Same-week appointments for cleanings."],
        "unknowns": ["How many people nearby search for Invisalign each month."],
        "confidence": "high",
    }


@pytest.mark.parametrize(
    "change",
    ["status = 'building'", "status = 'failed'", "brief_json = NULL", "dropped = 'revise'", "mode = 'validate'"],
)
def test_a_run_not_done_not_delivered_or_not_built_on_is_the_teams(conn, monkeypatch, change):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.seed(conn)
    rid = rf.delivered(conn)
    conn.execute(f"UPDATE research_run SET {change} WHERE id = %s", (rid,))
    assert researcher.owner_view(config.load(), _stored(conn, rid)) is None
    assert researcher.owner_view(config.load(), None) is None


def test_a_run_none_of_whose_findings_the_guards_passed_shows_an_owner_nothing(conn, monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "on")
    rf.seed(conn)
    findings = [{**f, "dropped": "number"} for f in rf.STORED["findings"]]
    rid = rf.delivered(conn, brief_json={**rf.STORED, "findings": findings})
    assert researcher.owner_view(config.load(), _stored(conn, rid)) is None


def test_the_team_view_is_the_whole_row_with_its_times_as_text(conn):
    rf.seed(conn)
    row = _stored(conn, rf.delivered(conn, dropped="revise"))
    team = researcher.team_view(row)
    assert (team["dropped"], team["brief_json"], team["mode"]) == ("revise", rf.STORED, "on")
    assert isinstance(team["created_at"], str) and isinstance(team["finished_at"], str)
    assert json.loads(json.dumps(team)) == team


def test_the_job_kind_is_registered():
    assert runner.HANDLERS[("researcher", "business")] is researcher.run_job
    assert runner.ROLE_TOOLS == ("job.critic", "job.researcher", "job.reviewer")
