"""The industry site's commands (A3p-6 batch 2 Task 4; plan 2026-10-06-agent-a3p-6-2-facts-photos-publish Q4-Q6, Q10,
Q12, Q21, Q22 and its Review Focus "a name the model derived from words" and "a bad patch writes nothing"):
`build_site` asks which look the owner wants (the three samples' screenshots), then the facts Admiry still needs, one
at a time, then builds the site into the business's bundle preview -- the model writes the words only when what it reads
changed -- and shows it with its photo checklist and the phone link; `get_site` says where the site stands; `get_site_templates` shows the
looks. A business of another kind is told Admiry writes it a page instead, and with AGENT_SITES off the three say the
sites are not on yet.

Two businesses: a roofer Admiry read from the owner's words (`_roofer`: `test_sites_build._biz` and its knowledge
written with `business.add_knowledge`, its name the model's own unless a test vouches for it) and the dental walk
(`test_tools_hosting._hosted_biz`: a plan, a confirmed Google listing, so it can publish). The model is a FakeGenerator
answering the sample's words fitted to the business (`_fitted`), the renderer the stand-in of test_sites_build."""

from __future__ import annotations

import json
import logging
import re

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent import audit, auth, business, hosted, hosting_minimum, keys, landing, llm, uploads
from agent.home_api import MUSE_INVITE
from agent.sites import build, intake, looks, photos, samples
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_DB_URL, call_tool, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_render_check import BROKEN
from agent.tests.test_site_upload import _get, _publish, _serving
from agent.tests.test_sites_build import _biz, _own, dental_library, renderer  # noqa: F401 (fixtures)
from agent.tests.test_sites_render import add_photo, jpeg, library  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import DOMAIN, HOSTNAME, URL, _hosted_biz, _mailbox

ROOFER = "biz_roofer"
OAKLINE = "Oakline Roofing"
WARRANTY = "10-year workmanship warranty"
PREVIEW_URL = re.compile(r"^https://preview--[0-9a-f]{16}\.business\.test/$")
NAME_QUESTION = "What is the business called?"
PLACE_QUESTION = "Where is the business? Its address, or the towns it serves."
BUILT = "Your site is ready to look over: {url} (the link works for 24 hours)."
PHOTOS = "Photos make your site yours — add your logo and photos from a phone: {url}."
NO_PHOTOS_YET = (
    "No logo or photos yet? Your assistant (Muse, for example) can make your logo and help with your photos."
)
HOME_EXTRAS = "Want to add your licence, years in business, a warranty or a review? Say them and I'll add them."
DENTAL_EXTRAS = "Want to add the dentist's name, the insurance you take or a review? Say them and I'll add them."
PICK = "Pick a look. You can change words, colours and photos after."
SITES_OFF = "Admiry's own sites are not on for this server yet."
NOT_A_FAMILY = "Admiry builds sites for home services and dental practices; for this business it writes a page instead."
BUILD_FAILED = "Admiry couldn't build your site just now. Try again in a few minutes."
WORDMARK_OK = "Your business name stands as the logo, in the look you picked."
EXPIRED = "Your preview expired; build it again."
TEMPLATES = "Admiry builds a site in one of these looks; pick one and Admiry fills it with the business's own facts."
BOTH_MISSING = "Your site needs your logo and one photo of your own work before it goes live."
BUSY = "another draft for this business is in progress; try again in a moment"
# fix round 1: two sentences of the coordinator's, for the founder's list
NOT_BUILT = "Your look is picked; build the site to see it."
STARTER_PAGE = "Your ads already use the page Admiry wrote. A site in its place comes with the next release."
STATE_NONE = "Admiry can build a site for this business; pick a look."
PLACE_NOT_TEXT = "place must be text: the address, or the towns the business serves."


@pytest.fixture(autouse=True)
def _sites_on(monkeypatch):
    """AGENT_SITES on (Q22): conftest's environment has it off, as every test written before batch 2 expects."""
    monkeypatch.setenv("AGENT_SITES", "1")


# ---------------------------------------------------------------- the businesses and the model


def _knowledge(**more) -> dict:
    """A roofer Admiry read from the owner's words: a phone, an address the owner placed, two services, the towns -- and
    a name the model derived (`name_source: "words"`: no site reading, no confirmed listing vouches for it)."""
    return {
        "name": "Charlotte Roofer",
        "name_source": "words",
        "category": "home_services",
        "category_detail": "roofer",
        "service_area": ["Charlotte"],
        "services": [
            {"name": "Roof replacement", "intent_phrases": [], "price_hint": None},
            {"name": "Roof repair", "intent_phrases": [], "price_hint": None},
        ],
        "hours": "Mon–Fri 7am–6pm",
        "phone": "(704) 555-0142",
        "pricing_hints": [],
        "usp": [],
        "listing": {"kind": "address", "address": "9 Elm Ct, Charlotte, NC 28203, USA", "lat": 35.2, "lng": -80.8},
        "locale": "en-US",
        **more,
    }


def _roofer(conn, monkeypatch, *, biz: str = ROOFER, **more) -> str:
    """The roofer on a server with hosting on (`test_sites_build._biz`), its knowledge as `_knowledge(**more)`; its
    key."""
    _biz(conn, monkeypatch, biz)
    business.add_knowledge(conn, biz, _knowledge(**more), source="owner")
    return business_with_key(conn, biz_id=biz)


def _named(conn, monkeypatch) -> str:
    """The roofer with a name the owner gave: nothing is missing."""
    return _roofer(conn, monkeypatch, name=OAKLINE, name_source="owner")


def _fitted(sample: str, headline: str, leads: tuple[str, ...]):
    """The sample's words as the model would write them for a business: the home headline naming its lead service and
    its city the way the ads do, and a page for each of its lead services (the linter asks both)."""
    words = samples.load(sample, style="solid" if sample == "roofer" else "calm").words.model_copy(deep=True)
    words.home.headline = headline
    words.service_pages = [p for p in words.service_pages if p.slug in leads]
    return words


ROOFER_WORDS = ("roofer", "Roof replacement and repair in Charlotte", ("roof-replacement",))
DENTAL_WORDS = ("dentist", "Invisalign in Palo Alto", ("invisalign",))


@pytest.fixture
def model(monkeypatch) -> FakeGenerator:
    gen = FakeGenerator(sites=[_fitted(*ROOFER_WORDS)])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    return gen


# ---------------------------------------------------------------- helpers


def _call(client, key: str, tool: str = "build_site", **args) -> dict:
    return call_tool(client, tool, args, key=key)


def _env(res: dict) -> dict:
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def _page(conn, biz: str = ROOFER) -> dict | None:
    return conn.execute("SELECT * FROM page WHERE business_id = %s", (biz,)).fetchone()


def _draft(conn, biz: str = ROOFER) -> dict:
    return intake.draft(_page(conn, biz))


def _knowledge_now(conn, biz: str = ROOFER) -> tuple[int, dict]:
    return business.latest_knowledge(conn, biz)


def _specs(conn, biz: str = ROOFER) -> list[dict]:
    return conn.execute(
        "SELECT version, style, data FROM site_spec WHERE business_id = %s ORDER BY version", (biz,)
    ).fetchall()


def _clean(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


def _built(client, conn, monkeypatch, style: str = "clean") -> tuple[str, dict]:
    """The roofer with its name, built once in `style`: (key, the build's envelope)."""
    key = _named(conn, monkeypatch)
    return key, _env(_call(client, key, style=style))


# ---------------------------------------------------------------- the look first, then the site (Q5, Q6)


def test_a_business_with_its_facts_and_no_look_is_asked_to_pick_one_of_the_three(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    model,
):
    key = _named(conn, monkeypatch)
    env = _env(_call(client, key))
    question = env["next"]["question"]
    assert env["next"]["kind"] == "question" and env["say"] == PICK
    assert (question["id"], question["index"], question["count"], question["input"]) == ("style", 1, 1, "single")
    assert question["prompt"] == PICK and question["other"]["allowed"] is False
    assert question["options"] == [
        {"id": look["id"], "label": look["name"], "sublabel": look["line"]} for look in looks.LOOKS["home_services"]
    ]
    shown = env["data"]["looks"]
    assert [look["id"] for look in shown] == ["solid", "clean", "bold"]
    assert all(look["look_url"].startswith("http://testserver/static/sites/looks/roofer-") for look in shown)
    assert env["data"]["site"]["status"] == "none" and env["data"]["site"]["missing"] == []
    assert _count(conn, "site_spec") == 0 and model.site_calls == []  # nothing is built before the look
    assert _draft(conn)["family"] == "home_services"  # the page is a site's now: its upload link asks the kinds
    assert _clean(env["say"])


def test_a_name_the_model_derived_is_asked_and_the_owners_answer_builds_the_site(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """Review Focus: a phone, an address and services, but a name nobody vouches for. The look comes first (the spec's §2
    flow: pick a look, then the facts that are missing), kept; the name is then the one question; once the owner gives
    it, nothing else is asked and the site is built."""
    key = _roofer(conn, monkeypatch)
    first = _env(_call(client, key))  # with neither the look nor the name, the look is asked: one of two questions left
    asked_first = first["next"]["question"]
    assert (asked_first["id"], asked_first["index"], asked_first["count"]) == ("style", 1, 2)
    assert first["data"]["site"]["missing"] == ["name"] and _count(conn, "site_spec") == 0
    env = _env(_call(client, key, style="clean"))
    question = env["next"]["question"]
    assert env["next"]["kind"] == "question" and env["say"] == NAME_QUESTION
    assert (question["id"], question["index"], question["count"], question["input"]) == ("name", 2, 2, "text")
    assert question["prompt"] == NAME_QUESTION and question["options"] == [] and question["other"]["allowed"] is True
    assert env["data"]["missing"] == ["name"] and env["data"]["site"]["style"] == "clean"
    assert _count(conn, "site_spec") == 0 and model.site_calls == []  # nothing built, the model not asked
    assert _draft(conn)["style"] == "clean"
    seen = _env(_call(client, key, "get_site"))  # fix round 1: the look is picked, the site not built yet
    assert seen["say"] == NOT_BUILT and seen["data"]["status"] == "none" and _clean(NOT_BUILT)

    res = _call(client, key, facts={"name": OAKLINE})
    env = _env(res)
    view = env["data"]
    # the profile's name is the owner's now, and nothing else was asked
    _version, knowledge = _knowledge_now(conn)
    assert (knowledge["name"], knowledge["name_source"]) == (OAKLINE, "owner")
    assert view["missing"] == [] and view["facts"] == {}  # the name is the profile's, not the draft's
    # the site: version 1 of its document, a preview on the business's own preview host
    (spec,) = _specs(conn)
    assert (spec["version"], spec["style"], spec["data"]["facts"]["name"]) == (1, "clean", OAKLINE)
    page = _page(conn)
    assert PREVIEW_URL.match(view["preview_url"]) and view["preview_url"] == f"https://{page['preview_host']}/"
    assert view["preview"]["url"] == view["preview_url"] and view["preview"]["token"] == view["token"]
    assert view["preview"]["render"] == {"loads": 3, "partial": False}
    assert view["status"] == "preview" and view["style"] == "clean" and view["family"] == "home_services"
    # a fresh upload link for the logo and the photos
    (token,) = [r["token"] for r in conn.execute("SELECT token FROM upload_token").fetchall()]
    assert view["upload_url"] == f"http://testserver/u/{token}"
    # the checklist: nothing in yet, the logo and the key photo keep it from going live
    assert [(item["id"], item["done"]) for item in view["checklist"]] == [
        (kind["id"], False) for kind in photos.KINDS["home_services"]
    ]
    assert view["key_photos"]["ok"] is False and view["key_photos"]["sentence"] == BOTH_MISSING
    # what the owner reads: the address, the hosting minimum (a first build), the photos, the extras once
    assert env["say"] == " ".join(
        [
            BUILT.format(url=view["preview_url"]),
            hosting_minimum.MINIMUM,
            PHOTOS.format(url=view["upload_url"]),
            NO_PHOTOS_YET,
            HOME_EXTRAS,
        ]
    )
    assert _clean(env["say"])
    assert env["next"] == {"kind": "link", "link": {"url": view["upload_url"], "label": "Add your logo and photos"}}
    # the model wrote the words once, from the name and the services and never the phone
    (asked,) = model.site_calls
    assert asked["facts"]["name"] == OAKLINE and "phone" not in asked["facts"]
    assert [s["name"] for s in asked["facts"]["services"]] == ["Roof replacement", "Roof repair"]
    assert _draft(conn)["words"] == _fitted(*ROOFER_WORDS).model_dump(mode="json")
    assert _draft(conn)["extras_asked"] is True
    # the audit row names the facts sent, never what they said
    row = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'build_site' ORDER BY id DESC").fetchone()
    assert json.loads(row["args_redacted"]["facts"]) == ["name"] and OAKLINE not in json.dumps(row["args_redacted"])


def test_a_new_look_is_built_from_the_drafts_words_without_the_model(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    key, first = _built(client, conn, monkeypatch)
    env = _env(_call(client, key, style="bold"))
    view = env["data"]
    assert [(s["version"], s["style"]) for s in _specs(conn)] == [(1, "clean"), (2, "bold")]
    assert len(model.site_calls) == 1  # the draft's words: the look is no fact the model reads
    assert _specs(conn)[1]["data"]["words"] == _specs(conn)[0]["data"]["words"]
    assert env["say"].startswith(f"Here is your site in the Bold look: {view['preview_url']}.")
    assert hosting_minimum.MINIMUM not in env["say"] and HOME_EXTRAS not in env["say"]  # said once, at the first build
    assert PHOTOS.format(url=view["upload_url"]) in env["say"]  # the photos are still to come
    assert view["upload_url"] == first["data"]["upload_url"]  # the link still valid, not a new one
    assert _count(conn, "upload_token") == 1
    # the same look again is built again, and says so as a build
    again = _env(_call(client, key, style="bold"))
    assert again["say"].startswith(BUILT.format(url=again["data"]["preview_url"]))
    assert len(model.site_calls) == 1


def test_a_fact_the_model_reads_has_the_words_written_again(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    key, _first = _built(client, conn, monkeypatch)
    env = _env(_call(client, key, facts={"warranty": WARRANTY}))
    assert len(model.site_calls) == 2 and model.site_calls[1]["facts"]["warranty"] == WARRANTY
    assert _specs(conn)[-1]["data"]["facts"]["warranty"] == WARRANTY
    assert env["data"]["facts"] == {"warranty": WARRANTY}  # the draft's own facts, never the profile's
    # the same warranty again changes nothing the model reads: no third call
    _env(_call(client, key, facts={"warranty": WARRANTY}))
    assert len(model.site_calls) == 2
    # a phone is never read by the model (the templates place it): a new one builds without it
    _env(_call(client, key, facts={"phone": "(704) 555-0199"}))
    assert len(model.site_calls) == 2
    assert _specs(conn)[-1]["data"]["facts"]["phone"] == "(704) 555-0199"


def test_where_the_business_is_is_asked_and_read_from_the_owners_words(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """A business whose only place is a radius is asked where it is (the controller's ruling): the answer to the place
    question comes back as `place`, read as an address or the towns it names."""
    key = _roofer(conn, monkeypatch, name=OAKLINE, name_source="owner", listing=None, service_area=["within 25 miles"])
    env = _env(_call(client, key, style="solid"))
    assert env["next"]["question"]["id"] == "place" and env["say"] == PLACE_QUESTION
    env = _env(_call(client, key, facts={"place": "Charlotte and Matthews"}))
    assert env["data"]["status"] == "preview" and env["data"]["missing"] == []
    assert _knowledge_now(conn)[1]["service_area"] == ["Charlotte", "Matthews"]
    assert _specs(conn)[-1]["data"]["facts"]["city"] == "Charlotte"
    row = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'build_site' ORDER BY id DESC").fetchone()
    assert json.loads(row["args_redacted"]["facts"]) == ["place"] and "Matthews" not in json.dumps(row["args_redacted"])


def test_the_look_comes_first_then_each_missing_fact_one_at_a_time_then_the_site(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """The coordinator's ruling (Task 6): the spec's §2 flow and the homepage card pick the look first, then ask only the
    facts that are missing; an assistant must match. A business that has none of the four facts is asked the look
    (one of five questions left), and once a look is picked each call asks the next missing fact -- in REQUIRED's order,
    one at a time, the count falling -- until nothing is missing and the site is built. A fact given beside a look, or
    before it, is kept: only what is still missing is asked."""
    key = _roofer(conn, monkeypatch, phone=None, listing=None, service_area=[], services=[])
    first = _env(_call(client, key))
    look = first["next"]["question"]
    assert first["next"]["kind"] == "question" and (look["id"], look["index"], look["count"]) == ("style", 1, 5)
    assert first["data"]["site"]["missing"] == ["name", "phone", "place", "services"]
    assert [o["id"] for o in look["options"]] == ["solid", "clean", "bold"] and look["other"]["allowed"] is False

    # one question after another, each told its place among the five of this intake (fix round 1)
    asked = _env(_call(client, key, style="solid"))
    name = asked["next"]["question"]
    assert (name["id"], name["index"], name["count"]) == ("name", 2, 5) and asked["say"] == NAME_QUESTION
    assert asked["data"]["missing"] == ["name", "phone", "place", "services"] and _draft(conn)["style"] == "solid"
    phone = _env(_call(client, key, facts={"name": OAKLINE}))["next"]["question"]
    assert (phone["id"], phone["index"], phone["count"]) == ("phone", 3, 5)
    place = _env(_call(client, key, facts={"phone": "(704) 555-0142"}))["next"]["question"]
    assert (place["id"], place["index"], place["count"]) == ("place", 4, 5)
    services = _env(_call(client, key, facts={"place": "Charlotte and Matthews"}))["next"]["question"]
    assert (services["id"], services["index"], services["count"]) == ("services", 5, 5)
    assert _count(conn, "site_spec") == 0 and model.site_calls == []  # nothing built while a fact is missing
    assert (_draft(conn)["asked"], _draft(conn)["asked_last"]) == (5, "services")

    built = _env(_call(client, key, facts={"services": "Roof replacement\nRoof repair"}))
    assert built["data"]["status"] == "preview" and built["data"]["missing"] == [] and len(model.site_calls) == 1
    assert (built["data"]["style"], _specs(conn)[-1]["data"]["facts"]["name"]) == ("solid", OAKLINE)
    assert (_draft(conn)["asked"], _draft(conn)["asked_last"]) == (0, None)  # the site is built: the count starts again


def test_a_fact_given_before_the_look_is_kept_and_only_the_look_is_asked_for(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """Facts first, look after: the answers are written as they come, whichever question was asked, so the look is the
    one question left -- and with the look the call that follows builds the site."""
    key = _roofer(conn, monkeypatch)  # the name is the one fact missing
    env = _env(_call(client, key, facts={"name": OAKLINE}))
    assert env["next"]["question"]["id"] == "style" and (env["next"]["question"]["count"]) == 1
    assert env["data"]["site"]["missing"] == [] and _knowledge_now(conn)[1]["name"] == OAKLINE
    built = _env(_call(client, key, style="bold"))
    assert built["data"]["status"] == "preview" and built["data"]["style"] == "bold" and len(model.site_calls) == 1


def test_an_answer_that_fills_two_facts_at_once_shortens_the_count_of_the_questions_left(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """Fix round 1 (the numbering): a question's index is the number of questions asked so far in this intake plus one,
    and its count that index plus the facts still missing after it -- so a call that answers more than the question
    asked drops the count, and the index goes on from where it was. From nothing on file: the look is "1 of 5"; the look
    with the name is "2 of 4" (the phone), the phone with the place "3 of 3" (the services)."""
    key = _roofer(conn, monkeypatch, phone=None, listing=None, service_area=[], services=[])
    first = _env(_call(client, key))["next"]["question"]
    assert (first["id"], first["index"], first["count"]) == ("style", 1, 5)

    phone = _env(_call(client, key, style="solid", facts={"name": OAKLINE}))["next"]["question"]
    assert (phone["id"], phone["index"], phone["count"]) == ("phone", 2, 4)
    services = _env(_call(client, key, facts={"phone": "(704) 555-0142", "place": "Charlotte"}))["next"]["question"]
    assert (services["id"], services["index"], services["count"]) == ("services", 3, 3)
    assert _count(conn, "site_spec") == 0


def test_a_question_asked_again_keeps_its_number(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """The same call sent again (a retry after a timeout, an answer that said nothing new) asks the same question: it is
    no new question of this intake, so it is no higher a number than it was."""
    key = _roofer(conn, monkeypatch, phone=None, listing=None, service_area=[], services=[])
    for _again in range(3):
        look = _env(_call(client, key))["next"]["question"]
        assert (look["id"], look["index"], look["count"]) == ("style", 1, 5)
    for _again in range(2):
        name = _env(_call(client, key, style="clean"))["next"]["question"]
        assert (name["id"], name["index"], name["count"]) == ("name", 2, 5)
    again = _env(_call(client, key, style="bold"))["next"]["question"]  # another look: still the name, still second
    assert (again["id"], again["index"], again["count"]) == ("name", 2, 5) and _draft(conn)["style"] == "bold"
    assert _draft(conn)["asked"] == 2


def test_a_look_picked_before_the_facts_are_given_one_by_one_through_the_page_command_is_numbered_as_well(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """`create_page` hands a family business to the same intake: its questions are numbered the same way."""
    key = _roofer(conn, monkeypatch, phone=None, listing=None, service_area=[], services=[])
    look = _env(_call(client, key, "create_page"))["next"]["question"]
    assert (look["id"], look["index"], look["count"]) == ("style", 1, 5)
    name = _env(_call(client, key, "build_site", style="solid"))["next"]["question"]
    assert (name["id"], name["index"], name["count"]) == ("name", 2, 5)


# ---------------------------------------------------------------- what is refused, and writes nothing


@pytest.mark.parametrize(
    ("facts", "key"),
    [
        ({"colour": "red"}, "colour"),  # a key outside SITE_FACTS
        ({"phone": "555-0142"}, "phone"),  # 7 digits
        ({"services": [f"Service {n}" for n in range(40)]}, "services"),  # 40 services
        (
            {"reviews": [{"text": "x" * 700, "first_name": "Ann", "source": "Google"}]},
            "reviews",
        ),  # a 700-character review
    ],
)
def test_a_bad_patch_is_refused_in_one_sentence_and_changes_nothing(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
    facts,
    key,
):
    """Review Focus: each refused in one sentence naming the key; the knowledge, the draft, the photos and the site's
    documents stand as they were, and the model was not asked."""
    owner_key, _first = _built(client, conn, monkeypatch)
    _own(conn, ROOFER, "key")
    before = (_knowledge_now(conn), _draft(conn), _count(conn, "site_spec"), _count(conn, "page_photo"))
    res = _call(client, owner_key, style="bold", facts=facts)
    assert res["isError"] and key in tool_text(res)
    assert len(re.findall(r"[.;] [A-Z]", tool_text(res))) == 0  # one sentence
    after = (_knowledge_now(conn), _draft(conn), _count(conn, "site_spec"), _count(conn, "page_photo"))
    assert after == before and len(model.site_calls) == 1


def test_a_look_or_a_colour_that_does_not_fit_is_refused_and_nothing_is_written(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    key = _named(conn, monkeypatch)
    res = _call(client, key, style="calm")
    assert (
        res["isError"]
        and tool_text(res) == "There is no look called “calm” for this business; use solid, clean or bold."
    )
    res = _call(client, key, style="solid", accent="orange")
    assert (
        res["isError"] and tool_text(res) == "The accent colour must be written like #1D4ED8, six hexadecimal digits."
    )
    res = _call(client, key, style="solid", accent="#FFFF00")
    assert res["isError"] and tool_text(res) == "That colour is too light for the button's words; pick a darker one."
    assert _page(conn) is None and _count(conn, "site_spec") == 0  # not even the page row
    # a look named as the owner reads it is the look
    env = _env(_call(client, key, style="Solid", accent="#1d4ed8"))
    assert (_draft(conn)["style"], _draft(conn)["accent"]) == ("solid", "#1D4ED8")
    assert env["data"]["style"] == "solid" and env["data"]["accent"] == "#1D4ED8"


def test_a_colour_that_does_not_read_on_a_new_looks_button_gives_way_to_the_looks_own(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """An accent picked for one look is kept for the next while its button's words still read on it, and dropped -- the
    new look's own colour -- when they would not, rather than holding every build of that look back."""
    key = _named(conn, monkeypatch)
    _env(_call(client, key, style="solid", accent="#1D4ED8"))
    _env(_call(client, key, style="clean"))
    assert _draft(conn)["accent"] == "#1D4ED8"
    _env(_call(client, key, style="bold"))  # Bold's button words are dark: a dark blue under them does not read
    assert _draft(conn)["accent"] is None and _specs(conn)[-1]["data"]["accent"] is None


def test_a_build_that_fails_its_checks_is_admirys_to_fix_and_says_so(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
    caplog,
):
    key = _named(conn, monkeypatch)
    renderer.problems = [BROKEN]
    with caplog.at_level(logging.ERROR, logger="agent.tools_sites"):
        res = _call(client, key, style="solid")
    assert res["isError"] and tool_text(res) == BUILD_FAILED
    (record,) = [r for r in caplog.records if r.name == "agent.tools_sites"]
    assert record.levelno == logging.ERROR and BROKEN["message"] in record.getMessage()
    assert _draft(conn)["words"] is None  # words are kept only from a build that passed
    renderer.problems, renderer.status = [], 503  # the renderer down is Admiry's too
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="agent.tools_sites"):
        res = _call(client, key, style="solid")
    assert res["isError"] and tool_text(res) == BUILD_FAILED
    assert [r.levelno for r in caplog.records if r.name == "agent.tools_sites"] == [logging.ERROR]
    # fix round 1: the words the first failed build paid for were kept: neither retry asked the model again
    renderer.status = 200
    assert _env(_call(client, key, style="solid"))["data"]["status"] == "preview"
    assert len(model.site_calls) == 1 and _draft(conn)["words"] is not None


def test_a_business_of_another_kind_is_told_admiry_writes_it_a_page(client, conn, monkeypatch, model):
    key = _roofer(conn, monkeypatch, category="attorneys", category_detail="personal injury lawyer")
    for tool in ("build_site", "get_site"):
        res = _call(client, key, tool)
        assert res["isError"] and tool_text(res) == NOT_A_FAMILY, tool
    assert _page(conn) is None
    assert not _call(client, key, "get_site_templates")["isError"]  # the looks are anyone's to see


def test_with_the_switch_off_the_three_say_the_sites_are_not_on(client, conn, monkeypatch, model):
    monkeypatch.delenv("AGENT_SITES")
    key = _named(conn, monkeypatch)
    for tool in ("build_site", "get_site", "get_site_templates"):
        res = _call(client, key, tool)
        assert res["isError"] and tool_text(res) == SITES_OFF, tool
    assert _page(conn) is None and model.site_calls == []
    assert _clean(SITES_OFF) and _clean(NOT_A_FAMILY) and _clean(BUILD_FAILED)


def test_one_build_of_a_business_at_a_time(client, conn, monkeypatch, model):
    """`build_site` is one of the tools that call the model: `run` holds the business's lock while it builds, and a
    second call meanwhile is answered in a sentence."""
    assert "build_site" in auth.LLM_TOOLS
    key = _named(conn, monkeypatch)
    with psycopg.connect(TEST_DB_URL, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"llm:{ROOFER}",))
        res = _call(client, key, style="solid")
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"llm:{ROOFER}",))
    assert res["isError"] and tool_text(res) == BUSY
    assert _count(conn, "site_spec") == 0 and model.site_calls == []


def test_the_days_model_limit_is_looked_at_only_before_the_model_writes(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    key, _first = _built(client, conn, monkeypatch)
    for _n in range(auth.LLM_CALLS_PER_DAY):  # the day's drafts used up
        audit.record(
            conn,
            tool="revise_page",
            args={},
            result={},
            ok=True,
            business_id=ROOFER,
            actor="test",
            actor_class="business",
            api_key_id=None,
            ip=None,
        )
    assert _env(_call(client, key, style="bold"))["data"]["style"] == "bold"  # no model call: built
    res = _call(client, key, facts={"warranty": WARRANTY})
    assert res["isError"] and "today's limit of" in tool_text(res)
    assert len(model.site_calls) == 1 and [s["style"] for s in _specs(conn)] == ["clean", "bold"]


# ---------------------------------------------------------------- the wordmark (Q12, T13)


def test_the_owners_business_name_can_stand_for_the_logo(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    key = _named(conn, monkeypatch)
    env = _env(_call(client, key, style="solid", wordmark=True))
    view = env["data"]
    assert _draft(conn)["wordmark_ok"] is True
    logo = next(item for item in view["checklist"] if item["id"] == "logo")
    assert logo["done"] is True and logo["have"] == 0
    assert view["key_photos"]["wordmark_ok"] is True and view["key_photos"]["ok"] is False  # the key photo is to come
    assert view["key_photos"]["sentence"] == "Your site needs one photo of your own work before it goes live."
    assert WORDMARK_OK in env["say"] and env["next"]["kind"] == "link"
    assert _specs(conn)[-1]["data"]["checklist"]["wordmark_ok"] is True


# ---------------------------------------------------------------- the looks (get_site_templates)


def test_the_looks_of_both_families_with_the_photo_each_needs_most(client, conn, monkeypatch, model):
    key = _named(conn, monkeypatch)
    env = _env(_call(client, key, "get_site_templates"))
    assert env["say"] == TEMPLATES and _clean(env["say"]) and env["next"] == {"kind": "none"}
    home, dental = env["data"]["families"]
    assert (home["id"], home["name"], home["trades"]) == (
        "home_services",
        "Home services",
        ["roofing", "plumbing", "hvac", "electrical"],
    )
    assert home["key_photo"] == "One photo of your own work, your team or your truck"
    assert [s["id"] for s in home["styles"]] == ["solid", "clean", "bold"]
    assert (dental["id"], dental["name"], dental["trades"], dental["key_photo"]) == (
        "dental",
        "Dental",
        ["dentist"],
        "Your office, outside or in",
    )
    assert [(s["name"], s["look_url"]) for s in dental["styles"]] == [
        (name, f"http://testserver/static/sites/looks/dentist-{name.lower()}.jpg")
        for name in ("Calm", "Warm", "Boutique")
    ]
    assert _count(conn, "page") == 0  # a read


# ---------------------------------------------------------------- where the site stands (get_site, Q10)


def test_the_site_view_before_and_after_the_build_and_once_its_preview_has_ended(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    key = _named(conn, monkeypatch)
    env = _env(_call(client, key, "get_site"))
    view = env["data"]
    assert env["say"] == "Admiry can build a site for this business; pick a look." and env["next"] == {"kind": "none"}
    assert (view["status"], view["preview"], view["published"], view["style"], view["facts"]) == (
        "none",
        None,
        None,
        None,
        {},
    )
    assert view["family"] == "home_services" and view["trade"] == "roofing" and view["missing"] == []
    assert [look["id"] for look in view["looks"]] == ["solid", "clean", "bold"]
    assert view["upload_url"] is None and view["muse"] is None and view["hosting"] is None
    assert view["hosting_note"].startswith(hosting_minimum.MINIMUM)
    assert _page(conn) is None  # a read writes nothing, not even the page row

    built = _env(_call(client, key, style="clean", facts={"warranty": WARRANTY}))
    env = _env(_call(client, key, "get_site"))
    view = env["data"]
    assert env["say"] == f"Your site is ready to look over: {built['data']['preview_url']}. {BOTH_MISSING}"
    assert view["status"] == "preview" and view["facts"] == {"warranty": WARRANTY}
    assert view["preview"] == built["data"]["preview"] and view["preview"]["token"] == built["data"]["token"]
    assert view["preview"]["expires_at"] and view["upload_url"] == built["data"]["upload_url"]
    assert view["checklist"] == built["data"]["checklist"] and view["key_photos"]["ok"] is False
    for key_name in ("family", "trade", "status", "style", "accent", "looks", "facts", "missing", "extras_asked"):
        assert key_name in view
    assert {"preview", "checklist", "key_photos", "upload_url", "published", "hosting", "hosting_note", "muse"} <= set(
        view
    )

    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute'")
    env = _env(_call(client, key, "get_site"))
    assert env["say"] == EXPIRED and env["data"]["status"] == "preview" and env["data"]["preview"] is None
    assert _clean(EXPIRED)


def test_a_dental_practice_builds_its_site_and_publishes_it(
    client,
    conn,
    monkeypatch,
    dental_library,  # noqa: F811
    renderer,  # noqa: F811
    fake,  # noqa: F811
):
    """The dental walk: its facts all on file, its logo and its office photo in. The site is built in Calm and offered
    to publish; once it is live, `get_site` says where."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    fake.sites = [_fitted(*DENTAL_WORDS)]
    _own(conn, biz, "logo")
    _own(conn, biz, "key")

    env = _env(_call(client, key, style="calm"))
    view = env["data"]
    assert view["family"] == "dental" and view["trade"] == "dentist" and view["key_photos"]["ok"] is True
    assert env["say"].endswith(DENTAL_EXTRAS) and PHOTOS.split(":")[0] not in env["say"]
    assert env["next"] == {
        "kind": "actions",
        "actions": [
            {"id": "publish", "label": "Publish your site", "tool": "publish_page", "primary": True},
            {"id": "look", "label": "Change the look", "tool": "build_site"},
        ],
    }
    assert build.publish_gate(conn, hosted.preview(conn, view["token"])) is None

    _publish(client, conn, monkeypatch, key, view["token"], "site")
    env = _env(_call(client, key, "get_site"))
    view = env["data"]
    assert env["say"] == f"Your site is live at {URL}." and view["status"] == "published"
    assert {k: view["published"][k] for k in ("url", "hostname", "version")} == {
        "url": URL,
        "hostname": HOSTNAME,
        "version": 1,
    }
    assert view["published"]["check"] is not None and view["muse"] == MUSE_INVITE
    assert view["hosting"] is not None and view["hosting"]["minimum_usd"] == hosting_minimum.MIN_MONTHLY_USD


def test_with_the_switch_off_create_page_writes_no_starter_page_over_a_live_site(
    client,
    conn,
    monkeypatch,
    dental_library,  # noqa: F811
    renderer,  # noqa: F811
    fake,  # noqa: F811
):
    """Final review I4: a site published while Admiry's own sites were on stays the business's page once they are off --
    create_page (the `write` action, the state's page step) answers SITES_OFF before anything is written: no model call,
    no starter page over the site's row, nothing new served. Its row says it was a site's: a refusal never counts toward
    the day's drafts."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    fake.sites = [_fitted(*DENTAL_WORDS)]
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    _publish(client, conn, monkeypatch, key, _env(_call(client, key, style="calm"))["data"]["token"], "site")
    before, served = _page(conn, biz), _get(client, HOSTNAME).text
    monkeypatch.setenv("AGENT_SITES", "0")

    res = _call(client, key, "create_page")

    assert res["isError"] and tool_text(res) == SITES_OFF
    after = _page(conn, biz)
    assert after["sections"]["kind"] == "site" and after["sections"] == before["sections"]
    assert (after["status"], after["version"]) == ("published", before["version"])
    assert fake.page_calls == [] and _get(client, HOSTNAME).text == served
    row = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'create_page'").fetchone()
    assert row["args_redacted"]["site"] == "true"


def test_the_view_is_the_businesss_own(client, conn, monkeypatch, library, renderer, model):  # noqa: F811
    """Q21: each business sees its own site; another business's preview, draft and link are never in it."""
    _key, first = _built(client, conn, monkeypatch)
    other = _roofer(conn, monkeypatch, biz="biz_other", name="Summit Roofing", name_source="owner")
    env = _env(_call(client, other, "get_site"))
    assert env["data"]["status"] == "none" and env["data"]["preview"] is None and env["data"]["upload_url"] is None
    assert first["data"]["token"] not in json.dumps(env["data"])
    assert uploads.latest_upload_url(conn, _page(conn)["id"], "http://testserver") == first["data"]["upload_url"]
    assert DOMAIN in first["data"]["preview_url"]


def test_the_upload_link_is_shown_only_to_a_key_that_may_manage(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """Final review I2: the phone link changes the site's photos, so get_site hands it only to a key that may manage; a
    key that may only read sees the rest of the view and `upload_url` null."""
    key, first = _built(client, conn, monkeypatch)
    reader, _ = keys.mint(conn, business_id=ROOFER, cls="business", scopes=("read",), label="claude")
    seen = _env(_call(client, reader, "get_site"))["data"]
    assert seen["upload_url"] is None
    assert seen["status"] == "preview" and seen["preview"]["token"] == first["data"]["token"]
    managing = _env(_call(client, key, "get_site"))["data"]
    assert managing["upload_url"] is not None and managing["upload_url"] == first["data"]["upload_url"]


def test_the_gallery_of_looks_and_a_live_page_that_is_no_site_are_not_the_sites(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """The site's preview is a bundle preview whose sections say `kind: site` and that is no gallery of looks; the site
    is live only when the live page is the site (its sections say so), never an owner's page or a starter page."""
    key, _first = _built(client, conn, monkeypatch)
    conn.execute("""UPDATE page_preview SET sections = sections || '{"gallery": ["roofer-solid"]}'::jsonb""")
    env = _env(_call(client, key, "get_site"))
    assert env["data"]["preview"] is None and env["say"] == EXPIRED
    conn.execute(
        "UPDATE page SET status = 'published', source = 'owner', url = 'https://oakline.example/', sections = %s "
        "WHERE business_id = %s",
        (Jsonb({"kind": "owner"}), ROOFER),  # the owner's own page is the live one
    )
    env = _env(_call(client, key, "get_site"))
    assert env["data"]["status"] == "preview" and env["data"]["published"] is None and env["data"]["muse"] is None


def test_without_hosting_nothing_is_asked_or_built(client, conn, monkeypatch, model):
    key = _named(conn, monkeypatch)
    monkeypatch.delenv("AGENT_PAGES_DOMAIN")
    res = _call(client, key, style="solid")
    assert res["isError"] and tool_text(res) == "Hosting is not set up on this server."
    assert _page(conn) is None and model.site_calls == []


# ---------------------------------------------------------------- fix round 1


def test_the_first_build_that_passes_says_the_hosting_minimum_and_the_failed_one_paid_for_the_words(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,
):
    """Items 1, 5 and 6: a build that fails has shown the owner nothing, so the next one that passes is the first and
    says the hosting minimum -- even though the failed one kept its document (version 1); the words it paid for are
    kept, so the retry asks the model nothing; and until a build passes, the look is picked and the site not built."""
    key = _named(conn, monkeypatch)
    renderer.status = 503
    res = _call(client, key, style="solid")
    assert res["isError"] and tool_text(res) == BUILD_FAILED and len(model.site_calls) == 1
    env = _env(_call(client, key, "get_site"))
    assert env["say"] == NOT_BUILT and env["data"]["status"] == "none" and env["data"]["preview"] is None
    renderer.status = 200
    env = _env(_call(client, key, style="solid"))
    assert hosting_minimum.MINIMUM in env["say"] and env["say"].startswith(BUILT.format(url=env["data"]["preview_url"]))
    assert len(model.site_calls) == 1 and [s["version"] for s in _specs(conn)] == [1, 2]
    again = _env(_call(client, key, style="clean"))  # a build after one that passed is no first
    assert hosting_minimum.MINIMUM not in again["say"]


def test_the_activity_log_keeps_no_reviewers_name_and_no_e_mail_of_the_site(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """Item 2: what the owner tells the site -- a reviewer's first name and words, the e-mail it shows -- is in the
    answer the caller reads, and in no audit row: the question's, the build's and the view's."""
    key = _roofer(conn, monkeypatch)  # the name still to give: the first call answers with the question
    review = {"text": "They fixed our roof in a day.", "first_name": "Dana", "source": "Google"}
    facts = {"email": "office@oakline.example", "reviews": [review]}
    asked = _env(_call(client, key, style="solid", facts=facts))
    assert asked["data"]["site"]["facts"]["email"] == "office@oakline.example"
    built = _env(_call(client, key, facts={"name": OAKLINE}))
    assert built["data"]["facts"]["reviews"] == [review]  # the caller reads what it gave
    _env(_call(client, key, "get_site"))
    rows = conn.execute(
        "SELECT tool, args_redacted, result FROM agent_action WHERE tool IN ('build_site', 'get_site') ORDER BY id"
    ).fetchall()
    assert [r["tool"] for r in rows] == ["build_site", "build_site", "get_site"]
    for row in rows:
        stored = json.dumps(row["result"]) + json.dumps(row["args_redacted"])
        for said in ("Dana", "fixed our roof", "office@oakline.example"):
            assert said not in stored, (row["tool"], said)
    assert rows[1]["result"]["data"]["facts"] == {"email": "***", "reviews": "***"}


def test_a_look_of_another_family_on_the_draft_is_no_look(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """Item 3: the business's kind changed after a look was picked: the look is none of its family's, so it is none --
    no error, no model call, the look asked again."""
    key = _named(conn, monkeypatch)
    page = landing.ensure_page_for_kit(conn, ROOFER)
    stale = {**intake.draft(None), "family": "dental", "trade": "dentist", "style": "calm", "accent": "#0F766E"}
    intake.save_draft(conn, page["id"], stale)
    seen = _env(_call(client, key, "get_site"))
    assert seen["data"]["style"] is None and seen["say"] == STATE_NONE
    env = _env(_call(client, key))
    assert env["next"]["question"]["id"] == "style" and env["say"] == PICK
    assert model.site_calls == [] and _count(conn, "site_spec") == 0
    assert (_draft(conn)["family"], _draft(conn)["style"]) == ("home_services", None)


def test_only_a_build_that_writes_words_counts_toward_the_days_limit(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """Item 4: a question, a look asked, a refusal -- none is one of the day's model calls; a build that writes the
    site's words is, and its row says so (`model`)."""
    key = _named(conn, monkeypatch)
    for _n in range(auth.LLM_CALLS_PER_DAY - 1):  # the day's model calls all but one used
        audit.record(
            conn,
            tool="revise_page",
            args={},
            result={},
            ok=True,
            business_id=ROOFER,
            actor="test",
            actor_class="business",
            api_key_id=None,
            ip=None,
        )
    for _n in range(9):  # the look asked nine times
        assert _env(_call(client, key))["next"]["question"]["id"] == "style"
    assert _call(client, key, style="calm")["isError"]  # a refusal
    assert _env(_call(client, key, style="solid"))["data"]["status"] == "preview"  # the words written: the last one
    res = _call(client, key, facts={"warranty": WARRANTY})
    limit = "today's limit of 40 drafts for this business is reached; try again tomorrow"
    assert res["isError"] and tool_text(res) == limit
    assert len(model.site_calls) == 1
    flags = conn.execute(
        "SELECT args_redacted->>'model' AS m FROM agent_action WHERE tool = 'build_site' ORDER BY id"
    ).fetchall()
    assert [r["m"] for r in flags] == [None] * 10 + ["true", None]


def test_a_business_whose_ads_use_the_page_admiry_wrote_keeps_it(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811
):
    """Item 8: a starter page Admiry wrote is the business's page: build_site refuses before anything is written, and
    the page, its photos and its upload link stand as they were; get_site says the same, with nothing built."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    monkeypatch.delenv("AGENT_SITES")  # the page was written before Admiry's own sites were on
    _env(call_tool(client, "create_page", {}, key=key))
    monkeypatch.setenv("AGENT_SITES", "1")
    page_id = _page(conn, biz)["id"]
    add_photo(conn, page_id, "ph_starter", jpeg(900, 600, 80))  # a photo the starter page took
    before = (
        conn.execute("SELECT sections, site_draft, site_spec_id FROM page WHERE id = %s", (page_id,)).fetchone(),
        conn.execute("SELECT token FROM upload_token ORDER BY token").fetchall(),
        _count(conn, "page_photo"),
        _knowledge_now(conn, biz),
    )
    res = _call(client, key, style="calm", facts={"warranty": WARRANTY}, wordmark=True)
    assert res["isError"] and tool_text(res) == STARTER_PAGE and _clean(STARTER_PAGE)
    after = (
        conn.execute("SELECT sections, site_draft, site_spec_id FROM page WHERE id = %s", (page_id,)).fetchone(),
        conn.execute("SELECT token FROM upload_token ORDER BY token").fetchall(),
        _count(conn, "page_photo"),
        _knowledge_now(conn, biz),
    )
    assert after == before and _count(conn, "site_spec") == 0 and fake.site_calls == []
    seen = _env(_call(client, key, "get_site"))
    assert seen["say"] == STARTER_PAGE and seen["data"]["status"] == "none"
    # a site built while Admiry's own sites were on, before the starter page: the starter page is the page still
    built_before = {
        **intake.draft(None),
        "family": "dental",
        "style": "calm",
        "words": _fitted(*DENTAL_WORDS).model_dump(),
    }
    intake.save_draft(conn, page_id, built_before)
    seen = _env(_call(client, key, "get_site"))
    assert seen["say"] == STARTER_PAGE and seen["data"]["status"] == "none"


def test_an_address_with_a_street_number_is_where_the_business_is_and_names_its_town(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """Item 9: asked where it is, the owner of a business that only says how far it goes types an address Admiry
    cannot place: it is where the business is -- the question is not asked again -- and the town after the street is
    the site's city."""
    key = _roofer(conn, monkeypatch, name=OAKLINE, name_source="owner", listing=None, service_area=["within 25 miles"])
    assert _env(_call(client, key, style="solid"))["next"]["question"]["id"] == "place"
    env = _env(_call(client, key, facts={"place": "9 Elm Ct, Charlotte NC"}))
    assert env["data"]["status"] == "preview" and env["data"]["missing"] == []
    assert _specs(conn)[-1]["data"]["facts"]["city"] == "Charlotte"


def test_a_place_that_is_no_text_a_cleared_colour_and_a_withdrawn_wordmark(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,
):
    """Item 10: the place's answer must be words; an empty accent gives the look its own colour back; the wordmark
    can be withdrawn, and the logo is to upload again."""
    key = _named(conn, monkeypatch)
    res = _call(client, key, style="solid", facts={"place": 28202})
    assert res["isError"] and tool_text(res) == PLACE_NOT_TEXT and _page(conn) is None
    _env(_call(client, key, style="solid", accent="#1D4ED8", wordmark=True))
    assert (_draft(conn)["accent"], _draft(conn)["wordmark_ok"]) == ("#1D4ED8", True)
    env = _env(_call(client, key, accent="", wordmark=False))
    assert (_draft(conn)["accent"], _draft(conn)["wordmark_ok"]) == (None, False)
    assert _specs(conn)[-1]["data"]["accent"] is None and env["data"]["accent"] is None
    logo = next(item for item in env["data"]["checklist"] if item["id"] == "logo")
    assert logo["done"] is False and env["data"]["key_photos"]["wordmark_ok"] is False
    assert WORDMARK_OK not in env["say"]
