"""The homepage's site card (A3p-6 batch 2 Task 7; plan 2026-10-06-agent-a3p-6-2-facts-photos-publish Q17, Q18 and its
Review Focus "A returning owner whose preview expired, and a build during a build"). For a dental practice -- the
homepage's own dental walk, its owner signed in and the business claimed, as test_home_api's page tests have it --
`/api/page/state` answers the site card: the three looks, the facts still missing with their questions, the industry's
own facts and the build button's words; then the site, with the screenshots of its preview, its photo checklist and the
gate. `/api/site/build` builds it from the card (a look, the facts, the wordmark) and answers the card's state again;
`/api/site/extras` skips the industry's facts; `/api/page/shot?preview=1` serves the live site preview's screenshots to
its owner alone. static/sites.js draws the card in its three states under Node, from the template home.html ships.

The model is a FakeGenerator answering the dentist sample's words fitted to the business (test_tools_sites), the
renderer the stand-in of test_sites_build (three screenshots), the stock photos the dental library."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from agent import business, config, home_api, hosted, tools_sites
from agent.sites import build, intake, looks, photos
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_DB_URL
from agent.tests.test_home_api import IP, _planned, fake  # noqa: F401 -- `fake` is a pytest fixture used by name
from agent.tests.test_render_check import PHONE, TABLET
from agent.tests.test_sites_build import _own, dental_library, renderer  # noqa: F401 (fixtures)
from agent.tests.test_tools_sites import DENTAL_WORDS, _fitted

DOMAIN = "business.test"
STATIC = Path(__file__).parents[1] / "static"
SITES_JS, HOME_JS = STATIC / "sites.js", STATIC / "home.js"
# Q12's words, verbatim (the founder approves them with the plan)
PICK = "Pick a look. You can change words, colours and photos after."
EXTRAS_HEADING = "A few more facts make the site yours. Skip any you'd rather not give."
CHECKLIST_HEADING = "Photos make your site yours. Upload these, most important first:"
NO_PHOTOS_YET = (
    "No logo or photos yet? Your assistant (Muse, for example) can make your logo and help with your photos."
)
WORDMARK = "Use my business name as the logo"
EXPIRED = "Your preview expired; build it again."
BOTH_MISSING = "Your site needs your logo and one photo of your office before it goes live."
PHONE_QUESTION = "What phone number should the site show?"
TOO_MUCH = "That is more than a site's facts take; send fewer at a time."
BUSY = "another draft for this business is in progress; try again in a moment"
BUSY_SAID = "Another draft for this business is in progress; try again in a moment."  # as the card shows it (home.js)
SKIPPED = "Noted. You can add more facts any time."  # fix round 1: the card's Skip, answered
READY = "Your site is ready to look over."  # fix round 1: the built card's heading (Q12's, without its address)
LIVE = "Your site is live."  # ... and the live site's
NEWER = "Look at the new version"  # final review: a live site's newer preview, linked above Publish
BUILT = re.compile(r"^Your site is ready to look over: https://preview--[0-9a-f]{16}\.business\.test/ ")


@pytest.fixture(autouse=True)
def _sites_on(monkeypatch):
    """AGENT_SITES on (Q22): conftest's environment has it off, as every test written before batch 2 expects."""
    monkeypatch.setenv("AGENT_SITES", "1")


# ---------------------------------------------------------------- the owner and their card


def _owner(client, conn, google, monkeypatch, fake) -> str:  # noqa: F811 -- the fixture
    """The homepage's dental walk on a server with hosting on: planned, claimed, its owner signed in; the model answers
    the dentist sample's words fitted to it. Returns the owner's CSRF token."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    csrf = _planned(client, conn, google)
    fake.sites = [_fitted(*DENTAL_WORDS)]
    return csrf


def _biz(conn) -> str:
    return conn.execute("SELECT id FROM business").fetchone()["id"]


def _state(client, csrf: str) -> dict:
    r = client.post("/api/page/state", json={"csrf": csrf}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()


def _build(client, csrf: str, **args) -> dict:
    r = client.post("/api/site/build", json={"csrf": csrf, **args}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()


def _no_phone(conn) -> None:
    """The business's phone taken off its profile: the one required fact the site then lacks."""
    _version, knowledge = business.latest_knowledge(conn, _biz(conn))
    business.add_knowledge(conn, _biz(conn), {**knowledge, "phone": None}, source="owner")


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def _draft(conn) -> dict:
    return intake.draft(conn.execute("SELECT * FROM page WHERE business_id = %s", (_biz(conn),)).fetchone())


def _clean(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


def _extras(family: str) -> list[dict]:
    return json.loads(json.dumps(intake.EXTRAS[family]))


# ---------------------------------------------------------------- the state (Q17)


def test_a_dental_practice_with_no_page_gets_the_site_card_with_the_three_looks(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    pages = _count(conn, "page")
    body = _state(client, csrf)
    page = body["page"]
    assert body["ok"] and page["kind"] == "site" and page["family"] == "dental" and page["status"] == "none"
    assert page["looks"] == looks.looks(config.load(), "dental")
    assert [look["look_url"] for look in page["looks"]] == [
        f"http://testserver/static/sites/looks/dentist-{style}.jpg" for style in ("calm", "warm", "boutique")
    ]
    # its facts are all on file (the listing the walk confirmed, the phone, the services): nothing is asked
    assert page["missing"] == [] and page["prompts"] == {}
    assert page["extras"] == _extras("dental") and page["extras_asked"] is False
    assert page["build_label"] == "Build my site" and page["shots"] == {} and page["preview"] is None
    assert [item["id"] for item in page["checklist"]] == [kind["id"] for kind in photos.KINDS["dental"]]
    assert _count(conn, "page") == pages and _count(conn, "site_spec") == 0  # a read writes nothing


def test_the_facts_a_site_lacks_are_on_the_card_with_their_questions(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    _no_phone(conn)
    page = _state(client, csrf)["page"]
    assert page["missing"] == ["phone"] and page["prompts"] == {"phone": PHONE_QUESTION}


def test_with_the_switch_off_the_card_is_todays_page_card(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    """Q22: off, a dental practice gets the starter page as before batch 2 -- no page yet, then the one it writes."""
    monkeypatch.setenv("AGENT_SITES", "0")
    csrf = _owner(client, conn, google, monkeypatch, fake)
    assert _state(client, csrf) == {"ok": True, "page": None}
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    assert created["ok"] and created["token"] and "kind" not in created
    page = _state(client, csrf)["page"]
    assert page["managed_by"] == "admiry" and page["token"] == created["token"] and "kind" not in page


def test_with_the_switch_off_the_page_cards_button_writes_no_starter_page_over_a_site(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    """Final review I4: once the sites are off, a business whose page is a site's -- here its draft, the look asked
    from the card -- is answered create_page's refusal through `call`: SITES_OFF, no starter page over the site's row,
    no model asked."""
    csrf = _owner(client, conn, google, monkeypatch, fake)
    assert _build(client, csrf)["ok"] and _draft(conn)["family"] == "dental"  # the look asked: the site's draft
    monkeypatch.setenv("AGENT_SITES", "0")
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    assert created == {"ok": False, "error": tools_sites.SITES_OFF}
    page = conn.execute("SELECT sections, site_draft FROM page WHERE business_id = %s", (_biz(conn),)).fetchone()
    assert page["sections"] is None and page["site_draft"]["family"] == "dental" and fake.page_calls == []


def test_the_page_cards_build_button_shows_a_family_business_its_site_card(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    """Task 5's note: the starter page's button, pressed before the state came, answers the site card (no page
    written, no model asked) -- never the look question the starter page's card cannot draw."""
    csrf = _owner(client, conn, google, monkeypatch, fake)
    pages = _count(conn, "page")
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    assert created["ok"] and created["kind"] == "site" and created["status"] == "none"
    assert created == {"ok": True, **_state(client, csrf)["page"]}
    assert _count(conn, "page") == pages and fake.site_calls == [] and fake.page_calls == []


# ---------------------------------------------------------------- the build from the card (Q17)


def test_a_look_with_a_fact_missing_asks_it_and_builds_nothing(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    _no_phone(conn)
    got = _build(client, csrf, style="calm")
    page = got["page"]
    assert got["ok"] and got["say"] == PHONE_QUESTION
    assert (page["kind"], page["status"], page["style"]) == ("site", "none", "calm")
    assert page["missing"] == ["phone"] and page["prompts"] == {"phone": PHONE_QUESTION}
    assert _count(conn, "site_spec") == 0 and fake.site_calls == []


def test_the_facts_build_the_site_and_its_screenshots_are_its_owners(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
    renderer,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    _no_phone(conn)
    got = _build(client, csrf, style="calm", facts={"phone": "(650) 555-0100"})
    page = got["page"]
    assert got["ok"] and BUILT.match(got["say"]) and _clean(got["say"])
    assert page["kind"] == "site" and page["status"] == "preview" and page["missing"] == []
    token = page["preview"]["token"]
    assert page["shots"] == {
        size: f"/api/page/shot?size={size}&preview=1&v={token[:8]}" for size in ("phone", "tablet", "desktop")
    }
    assert page["key_photos"]["ok"] is False and page["key_photos"]["sentence"] == BOTH_MISSING
    assert page["upload_url"].startswith("http://testserver/u/") and page["build_label"] == "Build my site"
    # the card asks the industry's own facts itself: the build offers none in words and leaves them to the card
    assert tools_sites.EXTRAS_OFFER["dental"] not in got["say"] and page["extras_asked"] is False
    shot = client.get(page["shots"]["phone"])
    assert shot.status_code == 200 and shot.content == PHONE and shot.headers["content-type"] == "image/jpeg"
    assert (
        shot.headers["cache-control"] == "private, max-age=300" and shot.headers["x-content-type-options"] == "nosniff"
    )
    assert client.get(page["shots"]["tablet"]).content == TABLET
    assert TestClient(client.app).get(page["shots"]["phone"]).status_code == 404  # no cookie: nobody's screenshot
    assert client.get("/api/page/shot?size=phone").status_code == 404  # without preview=1: the assistant's page only
    assert client.get("/api/page/shot?size=huge&preview=1").status_code == 404


def test_without_a_renderer_the_card_has_no_screenshots(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    got = _build(client, csrf, style="warm")
    assert got["ok"] and got["page"]["status"] == "preview" and got["page"]["preview"] is not None
    assert got["page"]["shots"] == {}
    assert client.get("/api/page/shot?size=phone&preview=1").status_code == 404


def test_the_owners_business_name_stands_for_the_logo(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    _build(client, csrf, style="calm")
    got = _build(client, csrf, wordmark=True)
    page = got["page"]
    assert got["ok"] and tools_sites.WORDMARK_OK in got["say"]
    assert page["key_photos"]["wordmark_ok"] is True
    assert next(item for item in page["checklist"] if item["id"] == "logo")["done"] is True
    assert page["key_photos"]["sentence"] == "Your site needs one photo of your office before it goes live."


def test_the_site_routes_need_the_signed_in_owner_and_the_csrf_token(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    stranger = TestClient(client.app)  # no cookie
    for path, body in (("/api/site/build", {"style": "calm"}), ("/api/site/extras", {"skipped": True})):
        assert stranger.post(path, json={"csrf": "x", **body}, headers=IP).json() == home_api.SIGNIN, path
    csrf = _owner(client, conn, google, monkeypatch, fake)
    pages = _count(conn, "page")
    for path, body in (("/api/site/build", {"style": "calm"}), ("/api/site/extras", {"skipped": True})):
        r = client.post(path, json={"csrf": csrf + "x", **body}, headers=IP)
        assert r.status_code == 403, path
    assert _count(conn, "page") == pages and _draft(conn)["style"] is None


def test_what_the_card_sends_must_be_of_its_kind(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    for bad in ({"facts": "phone"}, {"facts": ["phone"]}, {"style": 5}, {"accent": ["#123456"]}, {"wordmark": "yes"}):
        r = client.post("/api/site/build", json={"csrf": csrf, **bad}, headers=IP)
        assert r.status_code == 400 and r.json() == home_api.BAD_REQUEST, bad
    body = '{"csrf": "' + csrf + '", "style": "calm\\ud800"}'  # a look's name no text can carry
    r = client.post("/api/site/build", content=body, headers={**IP, "content-type": "application/json"})
    assert r.status_code == 400 and r.json() == home_api.BAD_REQUEST
    for bad in ({}, {"skipped": False}, {"skipped": "true"}):
        r = client.post("/api/site/extras", json={"csrf": csrf, **bad}, headers=IP)
        assert r.status_code == 400 and r.json() == home_api.BAD_REQUEST, bad


# ---------------------------------------------------------------- the industry's own facts (Q4, Q17)


def test_skipping_the_industrys_facts_marks_them_asked_and_builds_nothing(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    _build(client, csrf, style="calm")
    specs = _count(conn, "site_spec")
    got = client.post("/api/site/extras", json={"csrf": csrf, "skipped": True}, headers=IP).json()
    assert got["ok"] and got["page"]["kind"] == "site" and got["page"]["extras_asked"] is True
    assert got["say"] == SKIPPED and _clean(SKIPPED)
    assert _draft(conn)["extras_asked"] is True and _count(conn, "site_spec") == specs
    assert _state(client, csrf)["page"]["extras_asked"] is True


def test_the_industrys_facts_given_on_the_card_are_the_sites_and_are_asked_no_more(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    _build(client, csrf, style="calm")
    given = {"new_patients": "New patients are seen within the week.", "insurance_plans": "Delta Dental, Cigna"}
    got = _build(client, csrf, facts=given)
    assert got["ok"] and got["page"]["extras_asked"] is True
    assert got["page"]["facts"] == {**given, "insurance_plans": ["Delta Dental", "Cigna"]}


def test_a_family_business_that_has_no_site_card_skips_nothing(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    monkeypatch.setenv("AGENT_SITES", "0")
    csrf = _owner(client, conn, google, monkeypatch, fake)
    got = client.post("/api/site/extras", json={"csrf": csrf, "skipped": True}, headers=IP).json()
    assert got == {"ok": False, "error": tools_sites.SITES_OFF} and _draft(conn)["extras_asked"] is False


# ---------------------------------------------------------------- Review Focus: an ended preview, a build during a build


def test_a_returning_owner_whose_preview_ended_builds_it_again_without_the_model(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
    renderer,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    first = _build(client, csrf, style="calm")["page"]
    asked = len(fake.site_calls)
    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute'")
    page = _state(client, csrf)["page"]
    assert (page["status"], page["preview"], page["shots"]) == ("preview", None, {})
    assert page["build_label"] == "Build it again"
    again = _build(client, csrf)  # the card's "Build it again": no look, no facts
    assert again["ok"] and BUILT.match(again["say"]) and len(fake.site_calls) == asked  # the draft's words
    page = again["page"]
    assert page["status"] == "preview" and page["preview"]["token"] != first["preview"]["token"]
    assert page["build_label"] == "Build my site" and set(page["shots"]) == {"phone", "tablet", "desktop"}


def test_a_second_build_while_one_runs_is_a_sentence_on_the_card(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    with psycopg.connect(TEST_DB_URL, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"llm:{_biz(conn)}",))
        got = _build(client, csrf, style="calm")
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"llm:{_biz(conn)}",))
    assert got == {"ok": False, "error": BUSY}
    assert _count(conn, "site_spec") == 0 and fake.site_calls == []


def test_publishing_from_the_card_waits_for_the_logo_and_the_key_photo(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    page = _build(client, csrf, style="calm")["page"]
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": page["preview"]["token"]}, headers=IP)
    assert r.status_code == 409 and r.json() == {"ok": False, "error": BOTH_MISSING}
    assert _count(conn, "proposal") == 0


def test_publishing_a_site_whose_preview_ended_says_build_it_again(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    """Fix round 1, item 6: the preview the card held has ended (24 hours, or a photo upload made a newer one): for a site
    the refusal is Q12's EXPIRED sentence, never the starter page's "build the page again"."""
    csrf = _owner(client, conn, google, monkeypatch, fake)
    token = _build(client, csrf, style="calm")["page"]["preview"]["token"]
    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute'")
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": token}, headers=IP)
    assert r.status_code == 409 and r.json() == {"ok": False, "error": EXPIRED}


def test_another_owner_signed_in_here_gets_no_screenshot_of_the_first(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
    renderer,  # noqa: F811 -- the fixture
):
    """Fix round 1, item 9: the screenshot's address names no business; the session does. Another owner, signed in with
    a business of their own, asks the first owner's address and is answered nothing."""
    from agent.tests.test_home_api import _answer_all, turn
    from agent.tests.test_oauth_consent import _sign_in

    csrf = _owner(client, conn, google, monkeypatch, fake)
    first = _biz(conn)
    shot = _build(client, csrf, style="calm")["page"]["shots"]["phone"]
    assert client.get(shot).status_code == 200
    other = TestClient(client.app)
    turn(other, {"action": "start", "text": "dentist within 5 miles, $2,000 a month", "idempotency_key": "o1"})
    _answer_all(other)
    second = conn.execute("SELECT id FROM business WHERE id <> %s", (first,)).fetchone()["id"]
    conn.execute(
        "UPDATE business SET claim_state = 'claimed', owner_email = 'second@example.com' WHERE id = %s", (second,)
    )
    _sign_in(other, google, email="second@example.com")
    assert other.post("/api/page/state", json={"csrf": "x"}, headers=IP).status_code == 403  # signed in, theirs
    assert other.get(shot).status_code == 404


# ---------------------------------------------------------------- the facts a card can send (Task 6's review)


def test_a_facts_object_nested_deeper_than_any_fact_is_too_much(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    deep = "[" * 3000 + '"x"' + "]" * 3000  # JSON reads it; a fact holds nothing like it
    body = '{"csrf": "' + csrf + '", "style": "calm", "facts": {"about": ' + deep + "}}"
    r = client.post("/api/site/build", content=body, headers={**IP, "content-type": "application/json"})
    assert r.status_code == 200 and r.json() == {"ok": False, "error": TOO_MUCH}
    assert _draft(conn)["style"] is None  # refused before anything was written


def test_a_looped_facts_object_is_too_much():
    looped: dict = {}
    looped["about"] = [looped]
    with pytest.raises(ToolError) as refused:
        intake.check(looped)
    assert str(refused.value) == TOO_MUCH


def test_a_lone_surrogate_is_taken_out_of_a_fact_as_a_nul_is(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    about = '"Family dentists\\ud800 since 1998\\udc00."'  # two halves of characters, each alone
    body = '{"csrf": "' + csrf + '", "style": "calm", "facts": {"about": ' + about + "}}"
    r = client.post("/api/site/build", content=body, headers={**IP, "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert _draft(conn)["facts"]["about"] == "Family dentists since 1998."
    assert intake.check({"warranty": "\ud83d", "licence": "L\udfff-1"}) == {"licence": "L-1"}


# ---------------------------------------------------------------- what the page ships


def _template(html: str, name: str) -> str:
    return html.split(f'<template id="{name}">')[1].split("</template>")[0]


def test_the_card_ships_q12s_words_and_the_scripts_load_in_order(client):
    html = client.get("/").text
    card = _template(html, "t-site")
    for words in (PICK, EXTRAS_HEADING, CHECKLIST_HEADING, NO_PHOTOS_YET, WORDMARK, EXPIRED):
        assert words.replace("'", "&#39;") in card or words in card, words
    assert (
        tools_sites.PICK_A_LOOK == PICK
        and tools_sites.EXPIRED == EXPIRED
        and tools_sites.NO_PHOTOS_YET == NO_PHOTOS_YET
    )
    for part in (
        "looks",
        "facts",
        "build",
        "tabs",
        "shot",
        "open",
        "checklist",
        "upload",
        "wordmark",
        "extras",
        "skip",
        "again",
        "newer",
    ):
        assert f'data-part="{part}"' in card, part
    assert NEWER in card  # final review: a live site's newer preview, linked in a new tab
    assert [t for t in re.findall(r">(Phone|Tablet|Computer)<", card)] == ["Phone", "Tablet", "Computer"]
    words = re.sub(r"<[^>]+>", " ", card)
    assert _clean(words) and "Build my site" not in card  # the build button's words are the state's
    assert READY in card and LIVE in card  # fix round 1: the built card's headings
    # fix round 1: the headings and the refusal line take focus after a redraw; the held Publish names the gate's line
    for part in ("lead", "ready", "expired", "live", "site-error"):
        assert 'tabindex="-1"' in re.search(rf'<[^>]*data-part="{part}"[^>]*>', card).group(0), part
    publish = re.search(r'<button[^>]*data-part="publish"[^>]*>', card).group(0)
    assert 'aria-describedby="site-gate"' in publish
    assert 'id="site-gate"' in re.search(r'<p[^>]*data-part="gate"[^>]*>', card).group(0)
    order = [html.index(f'src="/static/{name}.js"') for name in ("channels", "sites", "home")]
    assert order == sorted(order)


def test_the_script_is_small_plain_and_says_nothing_an_owner_should_not_read(client):
    js = client.get("/static/sites.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert js.text.count("\n") < 340 and "innerHTML" not in js.text and "eval(" not in js.text
    assert "window.AdmirySites" in js.text and _clean(js.text) and "campaign" not in js.text.lower()
    # final review: the button's words and the refusal the script knows it by are the server's, word for word
    assert f'"{home_api.BUILD_AGAIN}"' in js.text and f'"{build.REBUILD_FIRST}"' in js.text
    # fix round 1: a still mark of its own for what is still to upload -- never the progress ring that spins while busy
    css = client.get("/static/home.css").text
    assert '"ring"' not in js.text and ".checklist .hollow {" in css and ".busy .hollow" not in css
    for extras in intake.EXTRAS.values():
        for e in extras:
            labels = [e["label"], *(f["label"] for f in e.get("fields", ()))]
            assert all(_clean(label) for label in labels) and e["id"] in intake.SITE_FACTS, e


# ---------------------------------------------------------------- sites.js under Node (Q17)

# sites.js under Node with a small DOM of its own (no jsdom here): the card's template is home.html's, read into a tree by
# `_tree`; `api` is home.js's side, stood in -- `post` answers each path from the case's answers and records the call,
# `sentence` is home.js's own (read out of it, as test_home_page's LAUNCH_TREE reads its lines), `reveal` records what it
# scrolled to. Each case draws its state, then takes its steps (a look tapped, the facts typed and sent, a button
# pressed), and the harness prints what is on the card -- only what is shown -- the calls it made, what it said, focused
# and revealed. `api.checks` is home.js's own list of a page's 14 checks (drawChecks and what it calls, read out of it
# as test_home_page reads them), so `checks` is what a live site lists under its line (spec 2026-10-12 §3.5).
SITES_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8"), home = fs.readFileSync(process.argv[2], "utf8").split("\n");
const input = JSON.parse(process.argv[3]);
const line = (start) => home.find((l) => l.startsWith(start)) || "";
const sentence = vm.runInContext([line("  const cap = "), line("  const sentence = "), "sentence;"].join("\n"),
  vm.createContext({}));
const grab = (name) => {
  const from = home.findIndex((l) => l.startsWith(`  function ${name}(`)), to = home.findIndex((l, i) => i > from && l === "  }");
  if (from < 0 || to < 0) throw new Error(`home.js has no function ${name} in the shape this reads`);
  return home.slice(from, to + 1).join("\n");
};
const stmt = (start) => {
  const from = home.findIndex((l) => l.startsWith(start));
  if (from < 0) throw new Error(`home.js has no statement starting "${start}"`);
  let to = from;
  while (!/;\s*(\/\/.*)?$/.test(home[to])) to += 1;
  return home.slice(from, to + 1).join("\n");
};
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
class Node {
  constructor(tag, attrs = {}) {
    Object.assign(this, { tagName: tag.toUpperCase(), attrs: {}, dataset: {}, kids: [], up: null, text: "", on: {},
      hidden: false, cls: new Set(), focused: 0 });
    for (const [k, v] of Object.entries(attrs)) this.setAttribute(k, v);
  }
  setAttribute(k, v) {
    if (k === "hidden") return void (this.hidden = true);
    if (k === "class") return void (this.cls = new Set(String(v).split(" ").filter(Boolean)));
    if (k.startsWith("data-")) this.dataset[camel(k.slice(5))] = String(v);
    this.attrs[k] = String(v);
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  set className(v) { this.setAttribute("class", v); }
  get classList() { const c = this.cls; return { add: (x) => c.add(x), contains: (x) => c.has(x),
    toggle: (x, on) => ((on === undefined ? !c.has(x) : on) ? c.add(x) : c.delete(x)) }; }
  set textContent(v) { this.text = String(v); this.kids = []; }
  get textContent() { return this.text + this.kids.map((k) => k.textContent).join(""); }
  append(...kids) { for (const k of kids) { if (k.up) k.remove(); k.up = this; this.kids.push(k); } }
  replaceChildren(...kids) { this.kids = []; this.append(...kids); }
  remove() { if (this.up) this.up.kids.splice(this.up.kids.indexOf(this), 1); this.up = null; }
  addEventListener(type, fn) { (this.on[type] ||= []).push(fn); }
  fire(type) { return Promise.all((this.on[type] || []).map((fn) => fn({ preventDefault() {}, target: this }))); }
  focus() { this.focused += 1; }
  all() { return this.kids.flatMap((k) => [k, ...k.all()]); }
  is(sel) {
    const m = /^([a-z]*)((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)$/.exec(sel.trim());
    if (!m) throw new Error(`the harness reads no selector like ${sel}`);
    if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && !m[2].split(".").filter(Boolean).every((c) => this.cls.has(c))) return false;
    return [...m[3].matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)].every(([, k, v]) => {
      const got = k.startsWith("data-") ? this.dataset[camel(k.slice(5))] : this.attrs[k] ?? this[k];
      return got !== undefined && got !== null && (v === undefined || String(got) === v);
    });
  }
  querySelectorAll(sel) { return this.all().filter((n) => sel.split(",").some((s) => n.is(s))); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const tree = (t) => { const n = new Node(t.tag, t.attrs); n.text = t.text; t.kids.forEach((k) => n.append(tree(k))); return n; };
const shown = (n, top) => { for (let x = n; x && x !== top.up; x = x.up) if (x.hidden) return false; return true; };
const el = (tag, cls, text) => Object.assign(new Node(tag, cls ? { class: cls } : {}), { style: {} }, text != null ? { text: String(text) } : {});
const drawChecks = vm.runInContext([line("  const part = "), stmt("  const VERDICTS = "), stmt("  const BLOCKING = "),
  ...["drawChecks", "groupChecks", "checkRowOf", "fixRow", "fixButton"].map(grab), "drawChecks;"].join("\n"),
  vm.createContext({ el, doc: { createTextNode: (t) => Object.assign(new Node("#text"), { text: String(t) }) } }));
global.window = {};
eval(src);
(async () => {
  const out = [];
  for (const c of input.cases) {
    const calls = [], said = [], revealed = [], host = new Node("div");
    const api = {
      clone: (id) => (id === "t-site" ? tree(input.template) : new Node("svg", { class: "check" })),
      part: (root, name) => root.querySelector(`[data-part="${name}"]`),
      el, checks: (n, check) => drawChecks(n, check, null),
      post: async (path, body) => (calls.push([path, body]), (c.answers || {})[path] || { ok: false, error: "No answer here." }),
      csrf: async () => "t", work: async (fn) => { await fn(); }, handle: (r) => said.push(["handled", r]),
      announce: (t) => said.push(t), sentence, reveal: (x) => revealed.push(x.dataset.part || x.tagName),
      reload: () => said.push("reloaded"),
      safeHref: (u) => (typeof u === "string" && (/^https?:\/\//.test(u) || /^\/(?!\/)/.test(u)) ? u : null),
    };
    window.AdmirySites.card(host, c.state, api);
    const card = () => host.kids[0];
    const at = (name) => card().querySelector(`[data-part="${name}"]`);
    for (const [what, arg] of c.steps || []) {
      if (what === "look") await card().querySelector(`[data-look="${arg}"]`).fire("click");
      if (what === "type") for (const [name, value] of Object.entries(arg)) card().querySelector(`[name="${name}"]`).value = value;
      if (what === "submit") await at(arg).fire("submit");
      if (what === "press") await at(arg).fire("click");
      await new Promise((done) => setTimeout(done, 5)); // the answer drawn before the next step
    }
    const n = card(), see = (name) => { const x = at(name); return x && shown(x, n) ? x : null; };
    const text = (name) => (see(name) ? see(name).textContent : null);
    const fields = (name) => (see(name) ? see(name).kids.map((f) => [f.kids[0].textContent, f.kids[1] ? f.kids[1].tagName : null]) : null);
    out.push({
      intro: see("pick") ? see("pick").kids[0].textContent : null,
      looks: see("looks") ? see("looks").kids.map((b) => [b.dataset.look, b.querySelector("img").src, b.querySelector("b").textContent,
        b.querySelector("span").textContent, b.getAttribute("aria-pressed")]) : null,
      facts: see("facts") ? fields("fields") : null, build: text("build"),
      tabs: see("tabs") ? see("tabs").kids.filter((t) => !t.hidden).map((t) => [t.textContent, t.getAttribute("aria-pressed")]) : null,
      shot: see("shot") ? see("shot").src : null, open: see("open") ? see("open").href : null,
      expired: text("expired"), again: text("again"),
      newer: see("newer") ? [see("newer").href, ...["target", "rel"].map((a) => see("newer").getAttribute(a)), text("newer")] : null,
      checklist: see("checklist") ? see("checklist").kids.map((li) => [li.cls.has("done"), ...li.kids.slice(1).map((k) => k.textContent)]) : null,
      upload: see("upload") ? see("upload").href : null, wordmark: text("wordmark"),
      extras: see("extras") ? fields("extras-fields") : null, relook: see("relooks") ? see("relooks").kids.length : null,
      publish: see("publish") ? [see("publish").textContent, see("publish").getAttribute("aria-disabled")] : null,
      gate: text("gate"), status: text("status"), hosting: text("hosting"), muse: see("muse") ? text("muse-text") : null,
      download: see("download") ? see("download").href : null, error: text("site-error"), own: text("own-say"),
      hours: text("hours-shown"),
      checks: see("website-groups") ? { verdict: text("website-verdict-line"),
        legend: see("website-legend").kids.map((s) => s.textContent),
        groups: see("website-groups").kids.map((g) => [g.kids[0].textContent.replace(/^✓\s*/, ""), g.kids[1].kids.map((li) => li.dataset.id)]),
        buttons: see("website-groups").querySelectorAll(".btn").length } : null,
      calls, said, revealed,
      focused: n.all().filter((x) => x.focused).map((x) => x.name || x.dataset.part),
      heads: ["lead", "ready", "expired", "live"].filter((h) => see(h)).map((h) => [h, text(h)]),
      marks: see("checklist") ? see("checklist").kids.map((li) => [...li.kids[0].cls].join(" ")) : null,
      parts: n.all().filter((x) => x.dataset.part && shown(x, n)).map((x) => x.dataset.part),
    });
  }
  console.log(JSON.stringify(out));
})();
"""


class _Tree(HTMLParser):
    """A template's HTML as a tree of {tag, attrs, text, kids}: what the harness builds its DOM from."""

    VOID = frozenset({"img", "input", "br", "hr"})

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "text": "", "kids": []}
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "text": "", "kids": []}
        self.stack[-1]["kids"].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag not in self.VOID:
            while self.stack.pop()["tag"] != tag:
                pass

    def handle_data(self, data):
        if data.strip():
            self.stack[-1]["text"] += " ".join(data.split())


def _card(client, cases: list[dict]) -> list[dict]:
    """What sites.js draws for each case, from home.html's own template."""
    template = _Tree(_template(client.get("/").text, "t-site")).root["kids"][0]
    run = subprocess.run(
        ["node", "-e", SITES_HARNESS, str(SITES_JS), str(HOME_JS), json.dumps({"template": template, "cases": cases})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _site(**over) -> dict:
    """A dental practice's site card state as home_api answers it, before its first build."""
    settings = config.load()
    state = {
        "kind": "site",
        "family": "dental",
        "trade": "dentist",
        "status": "none",
        "style": None,
        "accent": None,
        "looks": looks.looks(settings, "dental"),
        "facts": {},
        "missing": [],
        "extras_asked": False,
        "preview": None,
        "checklist": [
            {**kind, "have": 0, "done": False, "blocks_publish": kind["id"] in photos.BLOCKING}
            for kind in photos.KINDS["dental"]
        ],
        "key_photos": {"ok": False, "logo": False, "key": False, "wordmark_ok": False, "sentence": BOTH_MISSING},
        "upload_url": None,
        "published": None,
        "hosting": None,
        "hosting_note": "Hosting is free while your ads with Admiry spend at least $200 a month.",
        "muse": None,
        "shots": {},
        "prompts": {},
        "extras": _extras("dental"),
        "build_label": "Build my site",
        "download_url": None,
        "newer_preview": False,
        "rebuild_needed": False,
    }
    return {**state, **over}


PREVIEW = {"url": "https://preview--0123456789abcdef.business.test/", "token": "pv_0123456789abcdef"}
SHOTS = {size: f"/api/page/shot?size={size}&preview=1&v=pv_01234" for size in ("phone", "tablet", "desktop")}
UPLOAD = "http://testserver/u/up_0123456789abcdef"


def _built(**over) -> dict:
    """The same practice once its site is built in Calm: its preview, its three screenshots, its upload link."""
    return _site(
        **{"status": "preview", "style": "calm", "preview": PREVIEW, "shots": SHOTS, "upload_url": UPLOAD, **over}
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_the_card_before_the_first_build_shows_the_three_looks_and_a_tap_builds(client):
    built = _built()
    none, tapped = _card(
        client,
        [
            {"state": _site()},
            {
                "state": _site(),
                "steps": [["look", "warm"]],
                "answers": {"/api/site/build": {"ok": True, "page": built, "say": "Built."}},
            },
        ],
    )
    assert none["intro"] == PICK
    assert none["looks"] == [
        [look["id"], look["look_url"], look["name"], look["line"], "false"]
        for look in looks.looks(config.load(), "dental")
    ]
    assert none["facts"] is None and none["tabs"] is None and none["publish"] is None and none["checklist"] is None
    assert none["hosting"] == _site()["hosting_note"]  # the hosting minimum, said before the site is built
    # nothing missing: the tap builds in that look at once, and the card is drawn again from the answer
    assert tapped["calls"] == [["/api/site/build", {"csrf": "t", "style": "warm"}]] and tapped["said"] == ["Built."]
    assert tapped["intro"] is None and tapped["tabs"] == [["Phone", "true"], ["Tablet", "false"], ["Computer", "false"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_a_tap_with_facts_missing_opens_them_on_one_card_and_build_sends_them_with_the_look(client):
    missing = {
        "missing": ["phone", "services"],
        "prompts": {"phone": PHONE_QUESTION, "services": intake.QUESTIONS["services"]},
    }
    refused = {"ok": False, "error": "phone must be a number with its area code, at least 10 digits"}
    tapped, sent, failed = _card(
        client,
        [
            {"state": _site(**missing), "steps": [["look", "calm"]]},
            {
                "state": _site(**missing),
                "steps": [
                    ["look", "calm"],
                    ["type", {"phone": " (650) 555-0100 ", "services": "Cleanings\nInvisalign"}],
                    ["submit", "facts"],
                ],
                "answers": {"/api/site/build": {"ok": True, "page": _built(), "say": "Built."}},
            },
            {
                "state": _site(**missing),
                "steps": [["look", "calm"], ["type", {"phone": "555", "services": "Cleanings"}], ["submit", "facts"]],
                "answers": {"/api/site/build": refused},
            },
        ],
    )
    assert tapped["facts"] == [[PHONE_QUESTION, "INPUT"], [intake.QUESTIONS["services"], "TEXTAREA"]]
    assert tapped["build"] == "Build my site" and tapped["calls"] == [] and "phone" in tapped["focused"]
    assert [look[-1] for look in tapped["looks"]] == ["true", "false", "false"]
    facts = {"phone": "(650) 555-0100", "services": "Cleanings\nInvisalign"}
    assert sent["calls"] == [["/api/site/build", {"csrf": "t", "style": "calm", "facts": facts}]]
    assert sent["tabs"] is not None and sent["facts"] is None
    assert sent["heads"] == [["ready", READY]] and sent["focused"] == ["ready"] and sent["revealed"] == ["ready"]
    # refused: a sentence on the card, in view and focused, and what was typed stays to fix
    assert failed["error"] == "Phone must be a number with its area code, at least 10 digits."
    assert failed["facts"] is not None and failed["build"] == "Build my site"
    assert "site-error" in failed["focused"] and failed["revealed"] == ["site-error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_the_built_card_shows_the_three_sizes_the_checklist_and_publish_held_until_the_photos_are_in(client):
    held, ready = _card(
        client,
        [
            {"state": _built()},
            {
                "state": _built(
                    key_photos={"ok": True, "logo": True, "key": True, "wordmark_ok": False, "sentence": None},
                    extras_asked=True,
                    checklist=[
                        {**kind, "have": kind["need"], "done": True, "blocks_publish": kind["id"] in photos.BLOCKING}
                        for kind in photos.KINDS["dental"]
                    ],
                )
            },
        ],
    )
    assert held["tabs"] == [["Phone", "true"], ["Tablet", "false"], ["Computer", "false"]]
    assert held["shot"] == SHOTS["phone"] and held["open"] == PREVIEW["url"] and held["upload"] == UPLOAD
    assert held["checklist"][0] == [False, "Your logo", "0 of 1", "Needed to publish"]
    assert held["marks"] == ["hollow"] * len(photos.KINDS["dental"])  # its own still mark, never the busy ring
    assert held["checklist"][2] == [False, "The waiting room or a treatment room", "0 of 2"]
    assert held["publish"] == ["Publish your site", "true"] and held["gate"] == BOTH_MISSING
    assert held["wordmark"] == WORDMARK and held["relook"] == 3 and held["expired"] is None
    assert [field[0] for field in held["extras"]] == [e["label"] for e in _extras("dental")]
    assert held["hosting"] == _site()["hosting_note"] and held["status"] is None and held["muse"] is None
    assert ready["publish"] == ["Publish your site", "false"] and ready["gate"] is None
    assert ready["wordmark"] is None and ready["extras"] is None  # the logo is in; the industry's facts were asked
    assert ready["checklist"][0][:3] == [True, "Your logo", "1 of 1"] and set(ready["marks"]) == {"check"}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_the_buttons_of_the_built_card_post_what_they_say(client):
    page = _built()
    answers = {
        "/api/site/build": {"ok": True, "page": page, "say": "Built again."},
        "/api/site/extras": {"ok": True, "page": _built(extras_asked=True), "say": SKIPPED},
    }
    cases = [
        {"state": page, "steps": [["press", "wordmark"]], "answers": answers},
        {"state": page, "steps": [["press", "skip"]], "answers": answers},
        {
            "state": page,
            "steps": [["type", {"new_patients": "Welcome!", "insurance_plans": "Delta"}], ["submit", "extras-form"]],
            "answers": answers,
        },
        {"state": page, "steps": [["press", "publish"]], "answers": answers},  # held: nothing is sent
        {"state": page, "answers": {"/api/site/build": {"ok": False, "error": BUSY}}, "steps": [["press", "wordmark"]]},
    ]
    wordmark, skip, extras, held, busy = _card(client, cases)
    assert wordmark["calls"] == [["/api/site/build", {"csrf": "t", "wordmark": True}]]
    assert wordmark["focused"] == ["ready"] and wordmark["revealed"] == ["ready"]  # the card drawn again, its heading
    assert skip["calls"] == [["/api/site/extras", {"csrf": "t", "skipped": True}]] and skip["extras"] is None
    assert skip["said"] == [SKIPPED] and skip["focused"] == ["ready"]
    facts = {"new_patients": "Welcome!", "insurance_plans": "Delta"}
    assert extras["calls"] == [["/api/site/build", {"csrf": "t", "facts": facts}]]
    assert held["calls"] == [] and held["said"] == [BOTH_MISSING]  # the held button says why it is held
    assert busy["error"] == BUSY_SAID and BUSY_SAID in busy["said"] and busy["wordmark"] == WORDMARK
    assert busy["focused"] == ["site-error"] and busy["revealed"] == ["site-error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_an_ended_preview_says_so_and_builds_again_and_a_live_site_shows_where_it_is(client):
    live = {
        "url": "https://bright-smile-dental.business.test/",
        "hostname": "bright-smile-dental.business.test",
        "version": 1,
        "check": {"rules": [{"ok": True}, {"ok": True}, {"ok": False}]},
    }
    ended, published = _card(
        client,
        [
            {
                "state": _built(preview=None, shots={}, build_label="Build it again"),
                "steps": [["press", "again"]],
                "answers": {"/api/site/build": {"ok": False, "error": "x"}},
            },
            {
                "state": _built(
                    status="published",
                    published=live,
                    hosting={"say": "Your site is covered while your ads spend $200 a month."},
                    muse=home_api.MUSE_INVITE,
                    download_url="http://testserver/connect/site?b=biz_1",
                )
            },
        ],
    )
    assert ended["expired"] == EXPIRED and ended["again"] == "Build it again" and ended["tabs"] is None
    assert ended["publish"] is None and ended["open"] is None
    assert ended["calls"] == [["/api/site/build", {"csrf": "t"}]]
    assert published["status"] == f"Published at {live['url']} (2 of 3 checks pass)"
    assert published["open"] == live["url"] and published["tabs"] is not None
    assert published["hosting"] == "Your site is covered while your ads spend $200 a month."
    assert (
        published["muse"] == home_api.MUSE_INVITE and published["download"] == "http://testserver/connect/site?b=biz_1"
    )
    assert published["publish"] is None and published["checklist"] is None and published["extras"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_the_industrys_facts_are_sent_in_the_shapes_a_site_takes(client):
    """The extras form's answers -- yes or no, a review (a list of one), the social pages and the dentist (a fact of a few
    parts), an address typed without https -- are the facts build_site takes: `intake.check` reads them as given."""
    settings = config.load()
    home = _built(family="home_services", style="solid", looks=looks.looks(settings, "home_services"))
    home["extras"] = _extras("home_services")
    typed = {
        "years": "12",
        "insured": "yes",
        "reviews.text": "Fast and fair.",
        "reviews.first_name": "Dana",
        "reviews.source": "Google",
        "social.facebook": "facebook.com/oakline",
        "social.instagram": "http://instagram.com/oakline",
        "email": "office@oakline.example",
    }
    dentist = {"dentist.name": "Dr. Ana Ruiz", "dentist.credentials": "DDS", "insurance_plans": "Delta Dental\nCigna"}
    answers = {"/api/site/build": {"ok": False, "error": "x"}}
    steps = [["submit", "extras-form"]]
    sent_home, sent_dental = (
        case["calls"][0][1]["facts"]
        for case in _card(
            client,
            [
                {"state": home, "steps": [["type", typed], *steps], "answers": answers},
                {"state": _built(), "steps": [["type", dentist], *steps], "answers": answers},
            ],
        )
    )
    assert sent_home == {
        "years": "12",
        "insured": True,
        "reviews": [{"text": "Fast and fair.", "first_name": "Dana", "source": "Google"}],
        "social": {"facebook": "https://facebook.com/oakline", "instagram": "https://instagram.com/oakline"},
        "email": "office@oakline.example",
    }
    assert intake.check(sent_home) == {**sent_home, "years": 12}
    assert sent_dental == {
        "dentist": {"name": "Dr. Ana Ruiz", "credentials": "DDS"},
        "insurance_plans": "Delta Dental\nCigna",
    }
    assert intake.check(sent_dental) == {**sent_dental, "insurance_plans": ["Delta Dental", "Cigna"]}


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_publish_on_the_card_draws_the_live_site_or_says_why_not(client):
    """Publish, once the logo and the key photo are in: the site's token sent, the card drawn again from the state and
    the publish's own sentence read out; refused (a photo came in after the last build), a sentence on the card."""
    ready = _built(key_photos={"ok": True, "logo": True, "key": True, "wordmark_ok": False, "sentence": None})
    live = {"url": "https://bright-smile-dental.business.test/", "check": {"rules": [{"ok": True}]}}
    say = "Published at https://bright-smile-dental.business.test/. The page passes all 14 checks."
    answers = {
        "/api/page/publish": {"ok": True, "page": {"url": live["url"]}, "say": say},
        "/api/page/state": {"ok": True, "page": _built(status="published", published=live)},
    }
    refused = {
        "/api/page/publish": {"ok": False, "error": build.REBUILD_FIRST},
        "/api/page/state": {"ok": True, "page": ready},
    }
    ended = _built(preview=None, shots={}, build_label="Build it again")
    gone = {"/api/page/publish": {"ok": False, "error": EXPIRED}, "/api/page/state": {"ok": True, "page": ended}}
    done, held, expired = _card(
        client,
        [
            {"state": ready, "steps": [["press", "publish"]], "answers": answers},
            {"state": ready, "steps": [["press", "publish"]], "answers": refused},
            {"state": ready, "steps": [["press", "publish"]], "answers": gone},
        ],
    )
    assert done["calls"] == [
        ["/api/page/publish", {"csrf": "t", "token": PREVIEW["token"]}],
        ["/api/page/state", {"csrf": "t"}],
    ]
    assert done["status"] == f"Published at {live['url']} (1 of 1 checks pass)" and done["said"] == [say]
    assert done["publish"] is None and done["open"] == live["url"]
    assert done["heads"] == [["live", LIVE]] and done["focused"] == ["live"]
    assert [path for path, _ in held["calls"]] == ["/api/page/publish", "/api/page/state"]  # drawn again, then said
    assert held["error"] == build.REBUILD_FIRST and held["said"] == [build.REBUILD_FIRST]
    assert held["publish"] == ["Publish your site", "false"] and held["focused"] == ["site-error"]
    # the preview it held has ended: drawn again, the card says so itself and offers "Build it again" -- once
    assert expired["heads"] == [["expired", EXPIRED]] and expired["again"] == "Build it again"
    assert expired["error"] is None and expired["focused"] == ["expired"] and expired["publish"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_a_fact_that_goes_missing_after_the_build_is_asked_on_the_card(client):
    """Fix round 1, item 4: a required fact gone from the profile after the site was built (a build, a look or the
    wordmark then answers with the question): the card asks it, with the looks, whatever the status -- never a dead end."""
    asked = _built(missing=["phone"], prompts={"phone": PHONE_QUESTION})
    answers = {"/api/site/build": {"ok": True, "page": asked, "say": PHONE_QUESTION}}
    drawn, answered = _card(
        client,
        [{"state": asked}, {"state": _built(), "steps": [["press", "wordmark"]], "answers": answers}],
    )
    for card in (drawn, answered):
        assert card["facts"] == [[PHONE_QUESTION, "INPUT"]] and card["build"] == "Build my site"
        assert [look[-1] for look in card["looks"]] == ["true", "false", "false"]  # the look kept, pressed
        assert card["tabs"] is not None and card["relook"] is None  # the site below; one row of looks, not two
    assert answered["said"] == [PHONE_QUESTION] and answered["focused"] == ["lead"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_a_live_site_with_a_newer_preview_shows_its_photos_and_publish_above_the_published_line(client):
    """Fix round 1, item 5 (ruling): photos added after the site went live built a newer preview: the live card shows the
    checklist and Publish as the preview's does, above the published line (publishing a live token again is answered
    without a second proposal, H40)."""
    live = {"url": "https://bright-smile-dental.business.test/", "check": {"rules": [{"ok": True}]}}
    newer, plain = _card(
        client,
        [
            {"state": _built(status="published", published=live, newer_preview=True)},
            {"state": _built(status="published", published=live)},  # the preview just published: nothing newer
        ],
    )
    assert newer["heads"] == [["live", LIVE]] and newer["checklist"] is not None
    assert newer["publish"] == ["Publish your site", "true"] and newer["gate"] == BOTH_MISSING
    assert newer["parts"].index("publish") < newer["parts"].index("status")
    assert newer["status"] == f"Published at {live['url']} (1 of 1 checks pass)" and newer["open"] == live["url"]
    assert newer["extras"] is None and newer["relook"] is None
    assert plain["checklist"] is None and plain["publish"] is None and plain["status"] is not None
    assert plain["tabs"] is not None  # its screenshots, from the preview it was published from


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_a_preview_that_lacks_the_photos_on_file_offers_build_it_again_and_so_does_its_refused_publish(client):
    """Final review: the state's `rebuild_needed` (the logo and the key photo are in, the preview was built before them)
    shows "Build it again", which posts no look and no facts; a Publish refused with build.REBUILD_FIRST shows it as
    well, beside the sentence -- once, whatever the state it drew the card from says."""
    ready = _built(key_photos={"ok": True, "logo": True, "key": True, "wordmark_ok": False, "sentence": None})
    needed = {**ready, "rebuild_needed": True, "build_label": "Build it again"}
    built = {"/api/site/build": {"ok": True, "page": ready, "say": "Built."}}
    refused = {"/api/page/publish": {"ok": False, "error": build.REBUILD_FIRST}}
    flagged, pressed, plain, after, again = _card(
        client,
        [
            {"state": needed},
            {"state": needed, "steps": [["press", "again"]], "answers": built},
            {"state": ready},
            {
                "state": ready,
                "steps": [["press", "publish"]],
                "answers": {**refused, "/api/page/state": {"ok": True, "page": ready}},
            },
            {
                "state": needed,
                "steps": [["press", "publish"], ["press", "again"]],
                "answers": {**refused, **built, "/api/page/state": {"ok": True, "page": needed}},
            },
        ],
    )
    assert flagged["again"] == "Build it again" and flagged["publish"] == ["Publish your site", "false"]
    assert flagged["parts"].index("again") < flagged["parts"].index("publish")
    assert pressed["calls"] == [["/api/site/build", {"csrf": "t"}]] and pressed["said"] == ["Built."]
    assert plain["again"] is None
    assert after["again"] == "Build it again" and after["error"] == build.REBUILD_FIRST
    assert after["said"] == [build.REBUILD_FIRST] and after["focused"] == ["site-error"]
    assert [path for path, _ in again["calls"]].count("/api/site/build") == 1  # one button, one build


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_a_live_site_with_a_newer_preview_links_to_the_new_version_above_publish(client):
    """Final review: on a live card "Open your site" opens the live site, so a newer preview has a link of its own --
    "Look at the new version", in a new tab, through safeHref -- above Publish; none without a newer preview, nor for an
    address that is no web address."""
    live = {"url": "https://bright-smile-dental.business.test/", "check": {"rules": [{"ok": True}]}}
    odd = {**PREVIEW, "url": "javascript:alert(1)"}
    newer, plain, bad = _card(
        client,
        [
            {"state": _built(status="published", published=live, newer_preview=True)},
            {"state": _built(status="published", published=live)},
            {"state": _built(status="published", published=live, newer_preview=True, preview=odd)},
        ],
    )
    assert newer["newer"] == [PREVIEW["url"], "_blank", "noopener", NEWER] and newer["open"] == live["url"]
    assert newer["parts"].index("newer") < newer["parts"].index("publish")
    assert plain["newer"] is None and bad["newer"] is None


def test_the_state_says_a_rebuild_is_needed_while_the_preview_lacks_the_photos_on_file(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    """Final review: the logo and the key photo came in after the build and the preview does not show them (a rebuild
    that failed, or one that waited for the words): publishing it is refused with build.REBUILD_FIRST, so the state says
    `rebuild_needed` and the button says "Build it again" -- the build it posts (no look, no facts, the words kept) gives
    a preview that carries them. Before the photos are in, the gate's own sentence holds Publish: nothing to rebuild."""
    csrf = _owner(client, conn, google, monkeypatch, fake)
    page = _build(client, csrf, style="calm")["page"]
    assert page["rebuild_needed"] is False and page["build_label"] == "Build my site"
    _own(conn, _biz(conn), "logo")
    _own(conn, _biz(conn), "key")  # on file, and not in the preview
    page = _state(client, csrf)["page"]
    assert page["rebuild_needed"] is True and page["build_label"] == "Build it again"
    assert build.publish_gate(conn, hosted.preview(conn, page["preview"]["token"])) == build.REBUILD_FIRST
    asked = len(fake.site_calls)
    again = _build(client, csrf)["page"]
    assert again["rebuild_needed"] is False and again["build_label"] == "Build my site"
    assert again["preview"]["token"] != page["preview"]["token"] and len(fake.site_calls) == asked


def test_a_live_site_names_a_newer_preview_only_once_one_was_built_since(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811 -- the fixture
):
    """Fix round 1, item 5: the preview just published stays live for 24 hours, and is no newer than the site; a build
    since (a photo, another look) is -- `newer_preview` says which, from the documents the two were drawn from."""
    csrf = _owner(client, conn, google, monkeypatch, fake)
    token = _build(client, csrf, style="calm")["page"]["preview"]["token"]
    conn.execute(  # published, as a publish leaves the page row: the preview's sections on it
        "UPDATE page SET status = 'published', url = 'https://bright-smile-dental.business.test/', "
        "hostname = 'bright-smile-dental.business.test', "
        "sections = (SELECT sections FROM page_preview WHERE token = %s) WHERE business_id = %s",
        (token, _biz(conn)),
    )
    page = _state(client, csrf)["page"]
    assert page["status"] == "published" and page["preview"]["token"] == token and page["newer_preview"] is False
    page = _build(client, csrf, style="warm")["page"]  # another look after going live: a newer preview to publish
    assert page["status"] == "published" and page["preview"]["token"] != token and page["newer_preview"] is True
    assert _state(client, csrf)["page"]["newer_preview"] is True


# ---------------------------------------------------------------- "I have a website" on the card (2026-10-08)
# The founder's dev walk, 2026-10-08: an owner who has a website could only pick a look. The card takes its address:
# set_landing_page names it the ads' page (its check, its report, its e-mail), the knowledge keeps it as the website
# (the "Your website" card, its ways to add Admiry's code), and the card then says where the ads send people.
OWN = "https://smile-studio.example"


def test_an_owner_with_a_website_names_it_on_the_card_instead_of_a_look(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    from agent import landing, profile
    from agent.tests.test_home_api import _check_as

    csrf = _owner(client, conn, google, monkeypatch, fake)
    checked = _check_as(monkeypatch, ok=False, tracking=False)
    r = client.post("/api/page/own-site", json={"csrf": csrf, "url": "smile-studio.example"}, headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"], body
    assert checked == [OWN]  # an address without its scheme is read as https
    page = landing.current(conn, _biz(conn))
    assert (page["url"], page["source"], page["status"]) == (OWN, "owner", "published")
    assert profile.own_site(business.latest_knowledge(conn, _biz(conn))[1]) == OWN
    assert body["website"]["state"] == "needs_code" and body["say"] and _clean(body["say"])
    state = _state(client, csrf)["page"]
    assert state["kind"] == "site" and state["own_site_url"] == OWN


def test_the_card_refuses_an_address_it_cannot_use_and_keeps_the_profile(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
):
    csrf = _owner(client, conn, google, monkeypatch, fake)
    before = business.latest_knowledge(conn, _biz(conn))[0]
    for url in ("", "   ", None, 7, "x" * 400):
        r = client.post("/api/page/own-site", json={"csrf": csrf, "url": url}, headers=IP).json()
        assert r == {"ok": False, "error": home_api.OWN_SITE_NEEDED}, url
    private = client.post("/api/page/own-site", json={"csrf": csrf, "url": "http://127.0.0.1/"}, headers=IP).json()
    assert private["ok"] is False and private["error"]  # set_landing_page's own refusal, before any fetch
    assert business.latest_knowledge(conn, _biz(conn))[0] == before  # nothing written
    stranger = TestClient(client.app)
    assert stranger.post("/api/page/own-site", json={"csrf": "x", "url": OWN}, headers=IP).json() == home_api.SIGNIN
    assert client.post("/api/page/own-site", json={"csrf": csrf + "x", "url": OWN}, headers=IP).status_code == 403


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_the_card_takes_the_owners_website_and_then_says_where_the_ads_go(client):
    answer = {
        "ok": True,
        "say": "Your website needs Admiry's code before your ads can send people there.",
        "website": {"state": "needs_code", "say": "x"},
        "check": None,
    }
    typed, own, instead = _card(
        client,
        [
            {
                "state": _site(),
                "answers": {"/api/page/own-site": answer},
                "steps": [["type", {"own_url": "smile-studio.example"}], ["submit", "own-form"]],
            },
            {"state": _site(own_site_url=OWN)},
            {"state": _site(own_site_url=OWN), "steps": [["press", "own-instead"]]},
        ],
    )
    assert "own-form" in typed["parts"] and typed["calls"] == [
        ["/api/page/own-site", {"csrf": "t", "url": "smile-studio.example"}]
    ]
    assert answer["say"] in typed["said"] and "reloaded" in typed["said"]
    assert own["looks"] is None and own["own"] == f"Your ads send people to your website, {OWN}."
    assert instead["looks"] is not None and "own" not in instead["parts"]


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_a_live_site_lists_its_checks_under_where_it_is(client):
    """Spec 2026-10-12 §3.5: the site card's "Published at …" line has the page's checks under it, drawn by home.js's
    own renderer (`api.checks`): the verdict, the legend, what stops the ads, the passing ones folded, and no buttons --
    Admiry's own site carries the code. A site not live yet lists nothing."""
    rules = [
        {
            "id": "reachable",
            "title": "Opens securely and fast",
            "why": "w.",
            "ok": True,
            "detail": "https",
            "fix": None,
        },
        {
            "id": "phone",
            "title": "Tap-to-call number at the top",
            "why": "w.",
            "ok": False,
            "detail": "d.",
            "fix": "Top.",
        },
    ]
    live = {"url": "https://bright-smile-dental.business.test/", "version": 1, "check": {"ok": False, "rules": rules}}
    published, preview = _card(client, [{"state": _built(status="published", published=live)}, {"state": _built()}])
    assert published["status"] == f"Published at {live['url']} (1 of 2 checks pass)"
    assert published["checks"] == {
        "verdict": "One thing before the ads can run.",
        "legend": ["1 before the ads can run", "1 pass"],
        "groups": [["Before the ads can run", ["phone"]], ["1 check passes", ["reachable"]]],
        "buttons": 0,
    }
    at = [published["parts"].index(p) for p in ("status", "website-verdict", "website-groups")]
    assert at == sorted(at) and preview["checks"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="drives sites.js with node")
def test_the_built_card_says_the_hours_the_site_shows_for_the_owner_to_check(client):
    """2026-10-08: the hours the words wrote tidily, said on the card under the site: the owner sees what the model made
    of what they typed, and says so if it is wrong. None written: nothing said."""
    tidy, plain = _card(
        client,
        [
            {"state": _built(hours_shown=["Monday to Friday, 9 am to 5 pm", "Sunday, closed"])},
            {"state": _built(hours_shown=[])},
        ],
    )
    assert tidy["hours"] == "Opening hours on your site: Monday to Friday, 9 am to 5 pm; Sunday, closed."
    assert plain["hours"] is None


def test_the_state_carries_the_hours_the_words_wrote(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    dental_library,  # noqa: F811
    renderer,  # noqa: F811
):
    from agent import models

    csrf = _owner(client, conn, google, monkeypatch, fake)
    words = fake.sites[0]
    fake.sites = [models.SiteWords.model_validate({**words.model_dump(), "hours": ["Monday to Friday, 8 am to 5 pm"]})]
    built = _build(client, csrf, style="calm")
    assert built["ok"], built
    assert built["page"]["hours_shown"] == ["Monday to Friday, 8 am to 5 pm"]
