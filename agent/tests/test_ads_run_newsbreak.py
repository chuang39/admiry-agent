"""NewsBreak through the framework (plan 2026-09-29-agent-m1-newsbreak Task 5b, the controller's rulings R17-R20 and
R26; spec 2026-09-29-admiry-channels-design D77, D89): the approval of a plan that funds Google Search and NewsBreak,
through `confirm_proposal` and its launch in the same request (`ads_run.launch_now`), with Google Ads faked as in
test_ads_run and NewsBreak by `FakeNewsBreak`. A channel never blocks the others (D77, Corrections 10): NewsBreak's own
rules -- its page rules, the `zip` geo's point and radius, its far point (R9) -- park its row, rechecked every hour,
while Google Search launches; only the primary's (`allocate.PRIMARY`) still refuse the approval (R17). A saved page
report from before NewsBreak's rules existed is checked again before NewsBreak is judged on it -- by NewsBreak's own
step, never inside the approval (R18 as revised by the final review's Minor 1); the image waits
in `waiting_creative`, never in the precheck (R19); copy NewsBreak cannot take fails NewsBreak's row alone (R20); a
version that drops NewsBreak pauses it before anything is launched in the approval's own request (R26); and no secret
reaches a row, the audit, a mail or the log on the way."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_mode, ads_run, config, crawl, landing, proposals, tools_account
from agent.channels.google_search import adapter as google
from agent.channels.newsbreak import adapter as nb
from agent.channels.newsbreak import creative
from agent.tests import fixtures
from agent.tests.conftest import ADS_ENV, ADS_SECRETS
from agent.tests.fake_newsbreak import envelope as nb_answer
from agent.tests.test_ads_run import (  # noqa: F401 - gads and live are fixtures
    BIZ,
    CHECK_FAILED,
    CHECK_OK,
    DESCRIPTIONS,
    GOOGLE,
    HEADLINES,
    KNOWLEDGE,
    PAGE_URL,
    PHONE_FIX,
    StepClock,
    approve,
    gads,
    live,
    plain,
    row,
    sections,
)
from agent.tests.test_ads_run_channels import NB, add_plan_by_hand, funds, nb_row, newsbreak_only, seed_plan
from agent.tests.test_landing import GOOD
from agent.tests.test_page_builder import PUBLIC_IP

IMAGE = "dentists/1.jpg"
TERMS_LINK = '<a href="/terms">Terms</a> · '  # GOOD's own Terms link, in its footer
NO_TERMS_FIX = ads_run.PAGE_FIX.format(fix=landing.TERMS_FIX)
# a page report saved before M1 Task 4 added the terms and contact rules: it passed, and says nothing of either
STALE = {"ok": True, "url": PAGE_URL, "rules": [{"id": "reachable", "ok": True, "fix": None}]}
# a report that has both: terms only a warning (no Terms link), contact passed
NO_TERMS = {
    "ok": True,
    "url": PAGE_URL,
    "rules": [
        {"id": "reachable", "ok": True, "fix": None},
        {"id": "terms", "ok": None, "fix": landing.TERMS_FIX},
        {"id": "contact", "ok": True, "fix": None},
    ],
}
TORONTO = {**KNOWLEDGE, "listing": {**KNOWLEDGE["listing"], "lat": 43.6532, "lng": -79.3832}}
NO_POINT = {**KNOWLEDGE, "listing": None, "service_area": ["Palo Alto"]}


def nb_entry(secs: dict, *, image: str | None = IMAGE, headlines: list[str] = HEADLINES) -> dict:
    """`secs` with NewsBreak's entry given its own lines and its image approved on the plan (P7), as the approval
    stores them (`proposals.apply_approve_plan`), and the rest of what a consultation stores (the page kit reads it)."""
    entry = next(c for c in secs["ads"]["channels"] if c["channel"] == NB)
    entry.update(
        headlines=list(headlines), descriptions=list(DESCRIPTIONS), landing_service="Invisalign", mode="managed"
    )
    secs["creative"] = {"angles": [], **({"approved_image": {NB: image}} if image else {})}
    secs["consultation"].update({"conversions": ["phone_call_60s"], "duration_days": 30})
    return secs


def both(*, image: str | None = IMAGE, headlines: list[str] = HEADLINES, **kw) -> dict:
    """A plan funding Google Search ($1,000 a month) and NewsBreak ($300), each with its own lines."""
    secs = sections(monthly_usd=1000.0, **kw)
    secs["ads"]["channels"].append({"channel": NB, "monthly_usd": 300.0})
    return nb_entry(secs, image=image, headlines=headlines)


def nb_only(**kw) -> dict:
    return nb_entry(newsbreak_only(**kw))


@pytest.fixture
def newsbreak(monkeypatch, nbapi, tmp_path):
    """NewsBreak's switch `live`, NewsBreak faked and the licensed image set generated; returns the fake."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    monkeypatch.setattr(creative, "ROOT", fixtures.creative_set(tmp_path / "creative"))
    return nbapi


def stored_execution(conn, plan_id: str) -> dict:
    return conn.execute("SELECT json->'execution' AS e FROM plan WHERE id = %s", (plan_id,)).fetchone()["e"]


def refused_approval(conn, plan_id: str) -> str:
    """The owner's own click on Approve (`web_owner`), refused: its sentence."""
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    return str(e.value)


def serve(conn, monkeypatch, html: str) -> list[httpx.Request]:
    """The business's page as a check fetches it: `html` at PAGE_URL ({pid} its tracking token) through ads_run's own
    check transport (`_check_transport`), the host resolving to a public address. Returns the requests, as they come."""
    page_id = landing.current(conn, BIZ)["id"]
    fetched: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(request)
        if str(request.url) != PAGE_URL:
            return httpx.Response(404, text="")
        body = html.replace("{pid}", page_id)
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(ads_run, "_check_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(crawl, "resolve", lambda host: PUBLIC_IP)
    return fetched


# ---------------------------------------------------------------- one approval, two channels, each by its own state


def test_the_approval_of_a_two_channel_plan_launches_both_when_both_are_ready(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    key, plan_id = seed_plan(conn, both(), status="draft")
    funds(conn)
    envelope = approve(client, conn, key, plan_id)
    execution = stored_execution(conn, plan_id)
    assert (execution[GOOGLE], execution[NB]) == ("queued", "queued")
    assert (row(conn)["state"], nb_row(conn)["state"]) == ("live", "live")
    assert envelope["data"]["execution"][NB] == "live" and "campaign" in nb_row(conn)["resources"]
    assert envelope["say"] == "Confirmed. Google Search: live. NewsBreak: live."


def test_newsbreaks_own_page_rule_parks_its_row_while_google_search_launches(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """R17 (D77, Corrections 10): NewsBreak's page rules are its own; a page without a Terms link never refuses the
    approval -- Google Search launches, and NewsBreak's row waits with the fix as its sentence (nothing sent to
    NewsBreak). The hourly ads-launch looks again at no NewsBreak cost, and launches it the hour the page passes."""
    key, plan_id = seed_plan(conn, both(), status="draft", check=NO_TERMS)
    funds(conn)
    envelope = approve(client, conn, key, plan_id)
    parked = nb_row(conn)
    assert (row(conn)["state"], parked["state"], parked["last_error"]) == ("live", "queued", NO_TERMS_FIX)
    assert stored_execution(conn, plan_id)[NB] == "queued" and newsbreak.requests == []
    said = f"NewsBreak: your ads are waiting to launch. {NO_TERMS_FIX}"
    assert envelope["say"] == f"Confirmed. Google Search: live. {said}" and plain(said)
    assert ads_run.execution_line(ads_run.rows(conn, BIZ)).endswith(f"NewsBreak: Waiting to launch: {NO_TERMS_FIX}")
    due = f"SELECT business_id FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}"
    assert [r["business_id"] for r in conn.execute(due).fetchall()] == [BIZ]  # the hourly recheck
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"], out["ops"], newsbreak.requests) == ("queued", NO_TERMS_FIX, 0, [])
    conn.execute("UPDATE page SET check_json = %s WHERE business_id = %s", (Jsonb(CHECK_OK), BIZ))  # fixed, checked
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"]) == ("live", None)


@pytest.mark.parametrize(
    ("knowledge", "sentence"),
    [
        (NO_POINT, ads_run.NO_LOCATION),  # Google Search places the service area's names instead
        (TORONTO, nb.FAR_POINT),  # R9: NewsBreak reaches US ZIP codes only; Google Search runs anywhere
    ],
    ids=["no point", "a far point"],
)
def test_newsbreaks_own_geo_rules_park_its_row_and_never_refuse_the_approval(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    knowledge,
    sentence,
):
    """R17: the `zip` geo's rules (a point, the plan's radius) and NewsBreak's own far point (R9) park its row with
    their sentence while Google Search, the plan's primary, launches."""
    gads.geo = {"Palo Alto": ["geoTargetConstants/1014044"]}
    key, plan_id = seed_plan(conn, both(), status="draft", knowledge=knowledge)
    funds(conn)
    approve(client, conn, key, plan_id)
    parked = nb_row(conn)
    assert (parked["state"], parked["last_error"]) == ("queued", sentence) and newsbreak.requests == []
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    assert row(conn)["state"] == "live"


@pytest.mark.parametrize(
    ("secs", "knowledge", "check", "sentence"),
    [
        (nb_only(radius_miles=None), KNOWLEDGE, CHECK_OK, ads_run.NO_RADIUS),
        (nb_only(), TORONTO, CHECK_OK, nb.FAR_POINT),
        (nb_only(), KNOWLEDGE, NO_TERMS, NO_TERMS_FIX),
    ],
    ids=["no radius", "a far point", "no Terms link"],
)
def test_a_plan_newsbreak_alone_carries_is_refused_when_newsbreak_would_park(
    conn, newsbreak, secs, knowledge, check, sentence
):
    """Fix round 1, item 0 (the controller's ruling): parking is for a channel that would block another (D77: a
    channel never blocks the others); when every funded channel would park, nothing could launch, so the approval is
    refused with the first one's sentence -- as M0 refuses a plan none of whose accounts is ready -- and no code is
    used, no row queued, nothing sent."""
    _key, plan_id = seed_plan(conn, secs, status="draft", knowledge=knowledge, check=check)
    funds(conn)
    assert refused_approval(conn, plan_id) == sentence
    with pytest.raises(ToolError) as e, conn.transaction():  # said by the precheck that runs before a code is consumed
        proposals.precheck_approve_plan(conn, BIZ, {"plan_id": plan_id})
    assert str(e.value) == sentence
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert nb_row(conn) is None and newsbreak.requests == []


def test_google_searchs_own_rule_still_refuses_the_approval(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """Only the primary's rules refuse (R17): a page whose check failed refuses Google Search's launch, so the approval
    is refused with its fix -- the plan stays a draft and nothing is queued anywhere."""
    _key, plan_id = seed_plan(conn, both(), status="draft", check=CHECK_FAILED)
    funds(conn)
    assert refused_approval(conn, plan_id) == ads_run.PAGE_FIX.format(fix=PHONE_FIX)
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert ads_run.rows(conn, BIZ) == [] and newsbreak.requests == []


def test_an_approval_no_funded_channel_could_launch_from_is_refused_with_each_channels_reason(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """Final review Minor 2 (money): a channel waiting for its account waits, and so does a parked one -- so a plan none
    of whose funded channels is both ready and unparked has nothing that can launch: here Google Search waits for its
    account and NewsBreak, ready, parks on its page's Terms link. It is refused as the all-park refusal is, before a
    code is used; nothing is queued, nothing sent. Since the M1 follow-up 2 (W1) the sentence names what each channel
    waits for, in card order, each its own sentence -- Google Search's account, then NewsBreak's page fix -- never the
    first one's alone."""
    _key, plan_id = seed_plan(conn, both(), status="draft", check=NO_TERMS, link=None)
    funds(conn)
    waiting = ads_run.account_status(conn, config.load(), BIZ, GOOGLE)
    assert not waiting.ready and ads_run.account_status(conn, config.load(), BIZ, NB).ready
    said = (
        "Google Search: Your Google Ads account is not linked to Admiry yet; link it first, since your ads run in your "
        "own account. NewsBreak: Your page needs a fix before your ads can send people there: Add a Terms link in the "
        "footer."
    )
    assert refused_approval(conn, plan_id) == said
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.precheck_approve_plan(conn, BIZ, {"plan_id": plan_id})
    assert str(e.value) == said
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert ads_run.rows(conn, BIZ) == [] and newsbreak.requests == []


def test_a_prepaid_approval_nothing_could_launch_from_names_what_each_channel_waits_for(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """M1 follow-up 2, W1 (found by the G1 session): a prepaid business with no balance -- Google Search runs in
    Admiry's own account since A3c-1, and it waits for funds as NewsBreak does -- has nothing that can launch. The
    refusal names what each channel the approval would queue waits for, in card order and each in its own words, each
    its own sentence (fix round 1) -- a reason that already names its channel is said as it is -- never Google
    Search's alone."""
    _key, plan_id = seed_plan(conn, both(), status="draft", check=NO_TERMS, link=None)
    ads_mode.change(conn, BIZ, "managed")  # the prepaid way, in both places (P6)
    said = "Add funds to start Google Search. Add funds to start NewsBreak."
    assert refused_approval(conn, plan_id) == said and plain(said)
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.precheck_approve_plan(conn, BIZ, {"plan_id": plan_id})
    assert str(e.value) == said
    assert ads_run.rows(conn, BIZ) == [] and newsbreak.requests == []


def test_a_one_channel_approval_nothing_could_launch_from_is_refused_with_its_reason_alone(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """W1: a plan with one channel reads exactly as before -- its reason alone, unnamed: a prepaid business's
    Google-only plan with no balance, NewsBreak's page fix (its own full stop kept) for its NewsBreak-only plan."""
    _key, google_only = seed_plan(conn, sections(), status="draft", check=NO_TERMS, link=None)
    ads_mode.change(conn, BIZ, "managed")
    assert refused_approval(conn, google_only) == "Add funds to start Google Search"
    funds(conn)
    newsbreak_alone = add_plan_by_hand(conn, nb_only(), status="draft")
    assert refused_approval(conn, newsbreak_alone) == (
        "Your page needs a fix before your ads can send people there: Add a Terms link in the footer."
    )


def drop_from_set(file: str) -> None:
    """Admiry's set without `file`: the founder took the picture out (its manifest entry gone)."""
    path = creative.ROOT / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["entries"] = [e for e in manifest["entries"] if e["file"] != file]
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_a_plan_whose_picture_left_the_set_before_its_approval_parks_newsbreak_while_google_search_launches(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """R19 (Corrections 11): the image is `ads_run.image_ready`'s, never the precheck's. Since fix round 1 (items 3,
    4) a plan funds NewsBreak only with a picture, the one its card shows, and the approval stores exactly that one:
    a picture that left Admiry's set by the approval is never swapped for another -- NewsBreak waits in
    `waiting_creative`, in its own words, while Google Search launches, and the hourly job has nothing to do for it
    until a version showing a new picture is approved."""
    key, plan_id = seed_plan(conn, both(), status="draft")  # its picture: IMAGE
    funds(conn)
    drop_from_set(IMAGE)
    envelope = approve(client, conn, key, plan_id)
    waiting = nb_row(conn)
    assert (row(conn)["state"], waiting["state"], waiting["last_error"]) == ("live", "waiting_creative", None)
    assert stored_execution(conn, plan_id)[NB] == "waiting_creative" and newsbreak.requests == []
    stored = conn.execute("SELECT json->'sections'->'creative' AS c FROM plan WHERE id = %s", (plan_id,)).fetchone()
    assert "approved_image" not in stored["c"]  # nothing approved in its place
    sentence = nb.IMAGE_WAIT
    assert ads_run.status_sentence(waiting) == f"NewsBreak: {sentence}."
    assert envelope["say"] == f"Confirmed. Google Search: live. NewsBreak: {sentence}."
    assert ads_run.execution_line(ads_run.rows(conn, BIZ)) == f"Google Search: Live since today; NewsBreak: {sentence}"
    due = f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}"
    assert conn.execute(due).fetchall() == []


def test_a_picture_that_leaves_the_set_after_the_approval_holds_newsbreak_before_anything_is_sent(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
):
    """Fix round 1, item 3: Admiry never starts NewsBreak with a picture the owner did not approve (D89). A picture
    approved with the plan that left the set before the launch neither fails the row (nothing of the owner's to fix)
    nor is swapped: the step puts the row in `waiting_creative`, in NewsBreak's words, and sends nothing."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    key, plan_id = seed_plan(conn, both(), status="draft")
    funds(conn)
    approve(client, conn, key, plan_id)
    checked = nb_row(conn)
    assert (checked["state"], checked["validated_at"] is not None) == ("queued", True)
    drop_from_set(IMAGE)
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"], out["say"]) == ("waiting_creative", None, f"NewsBreak: {nb.IMAGE_WAIT}.")
    assert newsbreak.requests == [] and nb_row(conn)["validated_at"] is None
    due = f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}"
    assert conn.execute(due).fetchall() == []
    # the step's move is in the owner's activity like every other (final review, a deferred minor)
    (held,) = conn.execute(
        "SELECT * FROM agent_action WHERE business_id = %s AND tool = 'job.ads_launch' AND result->>'to' = %s",
        (BIZ, "waiting_creative"),
    ).fetchall()
    said = tools_account.sentence(dict(held), None)
    assert said == (
        "Admiry put your ads on NewsBreak on hold: the picture approved with your plan is no longer in Admiry's set"
    )
    assert plain(said)


def test_a_row_waiting_for_its_account_whose_picture_left_the_set_waits_for_a_picture_once_the_account_is_ready(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    key, plan_id = seed_plan(conn, both(), status="draft")
    approve(client, conn, key, plan_id)  # no balance yet: NewsBreak waits for funds while Google Search launches
    assert nb_row(conn)["state"] == "waiting_account"
    drop_from_set(IMAGE)
    funds(conn)
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["say"]) == ("waiting_creative", f"NewsBreak: {nb.IMAGE_WAIT}.")
    assert newsbreak.requests == []


def test_running_newsbreak_ads_take_a_new_version_showing_their_picture_though_it_left_the_set(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """Final review I3: v1 runs on both channels with its picture; v2, made while the set still had it (so pinned with
    it), changes NewsBreak's money and words, and the picture leaves the set before v2's approval -- which stores none.
    The running ads carry that picture (the owner approved it with v1: D89 holds), so the approval records NewsBreak's
    execution as queued, never `waiting_creative` for a row that stays live, and its step updates the ads to v2 on the
    picture NewsBreak has -- nothing uploaded."""
    key, v1 = seed_plan(conn, both(), status="draft")
    funds(conn)
    approve(client, conn, key, v1)
    assert (row(conn)["state"], nb_row(conn)["state"]) == ("live", "live")
    (asset,) = nb_row(conn)["resources"]["assets"].values()
    secs = both(headlines=["Gentle Care in Palo Alto", "Same-Day Crowns in Palo Alto"])
    secs["ads"]["channels"][1]["monthly_usd"] = 400.0
    secs["creative"]["proposed_image"] = {NB: IMAGE}
    v2 = add_plan_by_hand(conn, secs, status="draft")
    drop_from_set(IMAGE)
    prop = proposals.propose_plan(conn, BIZ, v2)
    with conn.transaction():  # the approval's own transaction: no launch step has run yet
        out = proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    assert out["execution"][NB] == "queued" and stored_execution(conn, v2)[NB] == "queued"
    assert nb_row(conn)["state"] == "live"
    newsbreak.sent.clear()
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    made = nb_row(conn)["resources"]["ads"].values()
    assert {newsbreak.ads[ad]["creative"]["content"]["assetUrl"] for ad in made} == {asset}
    assert "ad/uploadAssets" not in newsbreak.kinds()


def test_copy_newsbreak_cannot_take_fails_its_row_alone_and_the_approval_stands(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """R20: words over NewsBreak's limits are no refusal of the approval (NewsBreak's precheck never reads the copy):
    the launch builds nothing NewsBreak can take, so its row fails with NewsBreak's own sentence -- nothing sent --
    while Google Search launches; a new version starts NewsBreak over."""
    key, plan_id = seed_plan(conn, both(headlines=["x" * 91]), status="draft")
    funds(conn)
    envelope = approve(client, conn, key, plan_id)
    failed = nb_row(conn)
    assert (row(conn)["state"], failed["state"], failed["last_error"]) == ("live", "failed", nb.TOO_LITTLE_COPY)
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    assert newsbreak.requests == []
    assert envelope["say"] == (
        f"Confirmed. Google Search: live. NewsBreak: your ads could not be launched. {nb.TOO_LITTLE_COPY}."
    )


def test_newsbreak_approved_while_its_switch_was_off_is_queued_by_its_own_rules_once_it_is_on(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    nbapi,
    monkeypatch,
    tmp_path,
):
    """R17 on the step that queues a channel approved while its switch was off (`_advance` with no row): NewsBreak's
    own rule parks its row there too -- its sentence on the row, the hour's recheck due -- instead of leaving no row
    at all (the approval never checked NewsBreak)."""
    monkeypatch.setattr(creative, "ROOT", fixtures.creative_set(tmp_path / "creative"))
    gads.geo = {"Palo Alto": ["geoTargetConstants/1014044"]}
    key, plan_id = seed_plan(conn, both(), status="draft", knowledge=NO_POINT)
    funds(conn)
    approve(client, conn, key, plan_id)
    assert stored_execution(conn, plan_id)[NB] == "off" and nb_row(conn) is None
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"]) == ("queued", ads_run.NO_LOCATION) and nbapi.requests == []
    due = f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}"
    assert conn.execute(due).fetchall() != []


# ---------------------------------------------------------------- R18: a page report from before NewsBreak's rules


def test_a_report_saved_before_newsbreaks_rules_is_checked_again_before_newsbreak_is_judged(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
):
    """R18 as revised (final review Minor 1): the saved report passed, but has no word of the rules NewsBreak needs
    (saved before M1 Task 4 added terms and contact) -- so the page is checked again, once, before NewsBreak is judged:
    never inside the approval (which parks NewsBreak while Admiry checks), but by NewsBreak's own step, here the one the
    approval's request runs once it committed. The fresh report is saved (no later step fetches again) and NewsBreak
    is parked on what it says, while Google Search launches."""
    key, plan_id = seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)
    fetched = serve(conn, monkeypatch, GOOD.replace(TERMS_LINK, ""))
    approve(client, conn, key, plan_id)
    assert len(fetched) == 1
    report = landing.saved_report(landing.current(conn, BIZ))
    by_id = {r["id"]: r for r in report["rules"]}
    assert report["ok"] is True and (by_id["terms"]["ok"], by_id["contact"]["ok"]) == (None, True)
    parked = nb_row(conn)
    assert (row(conn)["state"], parked["state"], parked["last_error"]) == ("live", "queued", NO_TERMS_FIX)
    assert newsbreak.requests == []


def test_a_stale_report_of_a_page_that_has_both_lets_newsbreak_launch(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
):
    key, plan_id = seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)
    fetched = serve(conn, monkeypatch, GOOD)
    approve(client, conn, key, plan_id)
    assert len(fetched) == 1 and (row(conn)["state"], nb_row(conn)["state"]) == ("live", "live")


def test_the_approval_never_fetches_the_page_a_stale_report_parks_newsbreak_until_its_step_checks_it(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
):
    """Final review Minor 1 (revises R18): no page is fetched inside the approval's transaction -- it holds the
    proposal row, and a tool's body calls no outside service. A saved report from before NewsBreak's rules parks
    NewsBreak at the approval with "Admiry is checking your page for NewsBreak", nothing fetched, the report as it
    was; NewsBreak's own step, outside any approval (the hourly one here), fetches the page once, saves the fresh
    report and judges it -- and the step after judges that report with no fetch."""
    _key, plan_id = seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)
    fetched = serve(conn, monkeypatch, GOOD.replace(TERMS_LINK, ""))
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    with conn.transaction():  # the approval's own transaction: no launch step runs
        proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    checking = ads_run.CHECKING_PAGE.format(label="NewsBreak")
    parked = nb_row(conn)
    assert (parked["state"], parked["last_error"], fetched) == ("queued", checking, [])
    assert landing.saved_report(landing.current(conn, BIZ)) == STALE and plain(checking)
    assert ads_run.status_sentence(parked) == f"NewsBreak: your ads are waiting to launch. {checking}."
    out = ads_run.launch(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"], len(fetched)) == ("queued", NO_TERMS_FIX, 1)
    report = landing.saved_report(landing.current(conn, BIZ))
    assert {"terms", "contact"} <= {r["id"] for r in report["rules"]}
    assert ads_run.launch(conn, config.load(), BIZ, NB)["last_error"] == NO_TERMS_FIX and len(fetched) == 1
    assert newsbreak.requests == []


def test_a_plan_newsbreak_alone_carries_with_a_stale_report_is_refused_until_the_page_is_checked(
    conn, newsbreak, monkeypatch
):
    """Minor 1 beside the all-park refusal: a plan NewsBreak alone carries would park on a stale report, so it is
    refused -- nothing could launch -- and no row is queued that Admiry would check: the refusal asks for the page's
    check (the owner's own saves the fresh report), never "Admiry is checking". Nothing is fetched."""
    _key, plan_id = seed_plan(conn, nb_only(), status="draft", check=STALE)
    funds(conn)
    fetched = serve(conn, monkeypatch, GOOD)
    unchecked = ads_run.PAGE_UNCHECKED.format(label="NewsBreak")
    assert refused_approval(conn, plan_id) == unchecked
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.precheck_approve_plan(conn, BIZ, {"plan_id": plan_id})
    assert str(e.value) == unchecked and fetched == [] and nb_row(conn) is None


def test_the_primarys_stale_report_is_checked_again_at_the_approval_as_before(conn, monkeypatch):
    """Minor 1: the primary is unchanged -- were Google Search to need a rule its stale report lacks, its re-check
    still runs where it ran (only a secondary channel parks without one)."""
    seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)
    caps = google.GoogleSearchAdapter.capabilities
    monkeypatch.setattr(
        google.GoogleSearchAdapter,
        "capabilities",
        lambda self: caps(self).model_copy(update={"page_rules": ("terms",)}),
    )
    fetched = serve(conn, monkeypatch, GOOD)
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_g", "sections": sections()}, park=True, fetch=False)
    assert len(fetched) == 1


def test_a_report_is_checked_again_only_for_a_channel_that_needs_a_rule_it_lacks(conn, monkeypatch):
    """Google Search names no page rules: a stale report judges its launch as it is, with no fetch; a report that has
    NewsBreak's rules is judged as it is too; and one that failed already is refused by its own first fix without a
    fetch (the owner checks the page again once it is fixed)."""
    seed_plan(conn, nb_only(), status="draft", check=STALE)
    funds(conn)
    fetched = serve(conn, monkeypatch, GOOD)
    settings = config.load()
    ads_run.precheck(conn, settings, BIZ, {"id": "pl_g", "sections": sections()})
    assert fetched == []
    conn.execute("UPDATE page SET check_json = %s WHERE business_id = %s", (Jsonb(NO_TERMS), BIZ))
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, settings, BIZ, {"id": "pl_n", "sections": nb_only()})
    assert str(e.value) == NO_TERMS_FIX and fetched == []
    conn.execute("UPDATE page SET check_json = %s WHERE business_id = %s", (Jsonb(CHECK_FAILED), BIZ))
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, settings, BIZ, {"id": "pl_n", "sections": nb_only()})
    assert str(e.value) == ads_run.PAGE_FIX.format(fix=PHONE_FIX) and fetched == []


def test_a_page_admiry_cannot_check_again_itself_is_unchecked_for_newsbreak(conn, monkeypatch, caplog):
    """R18, failing closed: when Admiry cannot run the check itself (here the page's host no longer resolves to a
    public address, which the check refuses before any fetch), NewsBreak is never judged on the stale report -- its
    page is unchecked for it, and the owner's own check saves a fresh report. The log line keeps the check's own
    sentence (Admiry's words: no secret in it; fix round 2, item 2)."""
    seed_plan(conn, nb_only(), status="draft", check=STALE)
    funds(conn)
    fetched = serve(conn, monkeypatch, GOOD)
    monkeypatch.setattr(crawl, "resolve", lambda host: ["10.0.0.7"])
    with caplog.at_level(logging.INFO), pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_n", "sections": nb_only()})
    assert str(e.value) == ads_run.PAGE_UNCHECKED.format(label="NewsBreak") and fetched == []
    assert landing.saved_report(landing.current(conn, BIZ)) == STALE
    assert "could not be checked again for NewsBreak: the page address must be a public address" in caplog.text


def test_a_report_checked_again_that_passes_retries_the_address_a_platform_refused(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
):
    """Fix round 1, item 5 (A3p-2 H29): the re-check saves a report like every other check, and so does what follows a
    passing one -- the page's address is offered again where a platform refused it (here Google Search's running ads,
    refused it before): the refusal cleared, the move queued."""
    _key, plan_id = seed_plan(conn, both(), status="approved", check=STALE)
    funds(conn)
    ads_run.queue(conn, BIZ, GOOGLE, plan_id, ready=True)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    ads_run.remember(conn, BIZ, GOOGLE, {"refused_url": PAGE_URL})
    fetched = serve(conn, monkeypatch, GOOD)
    ads_run.precheck(conn, config.load(), BIZ, {"id": plan_id, "sections": both()}, channels=[NB])
    google_row = row(conn)
    assert len(fetched) == 1 and "refused_url" not in google_row["resources"]
    assert google_row["resources"]["url_stale"] is True


def test_a_secondary_channels_re_check_that_breaks_parks_it_and_never_refuses_the_approval(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
    caplog,
):
    """Item 6 (D77): whatever breaks while NewsBreak's stale page report is checked again -- by NewsBreak's own step
    since the final review's Minor 1, here the one the approval's request runs once it committed -- the approval
    stands: NewsBreak's page is unchecked for it and its row parks, Google Search launches -- and the log names the
    kind of failure, never its words."""
    key, plan_id = seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)

    def broken(*args, **kwargs):
        raise RuntimeError("the kit's own words")

    monkeypatch.setattr(landing, "kit", broken)
    with caplog.at_level(logging.INFO):
        approve(client, conn, key, plan_id)
    parked = nb_row(conn)
    unchecked = ads_run.PAGE_UNCHECKED.format(label="NewsBreak")
    assert (row(conn)["state"], parked["state"], parked["last_error"]) == ("live", "queued", unchecked)
    assert "RuntimeError" in caplog.text and "the kit's own words" not in caplog.text


def test_a_database_error_in_a_secondary_channels_re_check_parks_it_and_the_approval_stands(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    monkeypatch,
    caplog,
):
    """Fix round 2, item 2 (D77): the re-check runs in a savepoint of its own, so even a database error inside it --
    which aborts the transaction it runs in -- parks NewsBreak with its page unchecked and leaves the step's own
    transaction usable (the approval never re-checks since the final review's Minor 1: its step does): the approval
    goes through and Google Search launches. The log names the error's kind, never its words."""
    key, plan_id = seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)

    def broken(conn_, biz):
        conn_.execute("SELECT no_such_column FROM business WHERE id = %s", (biz,))

    monkeypatch.setattr(landing, "kit", broken)
    with caplog.at_level(logging.INFO):
        approve(client, conn, key, plan_id)
    parked = nb_row(conn)
    unchecked = ads_run.PAGE_UNCHECKED.format(label="NewsBreak")
    assert (row(conn)["state"], parked["state"], parked["last_error"]) == ("live", "queued", unchecked)
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    assert "UndefinedColumn" in caplog.text and "no_such_column" not in caplog.text


def test_the_primarys_re_check_admiry_cannot_run_refuses_its_page_unchecked_with_the_reason_logged(
    conn, monkeypatch, caplog
):
    """Item 2, the primary's side as it was: were Google Search to need a rule its stale report lacks, a check Admiry
    cannot run itself (a ToolError: here the host no longer resolves to a public address) leaves its page unchecked --
    its own rule, so the approval is refused -- and the log line keeps the check's own sentence."""
    seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)
    caps = google.GoogleSearchAdapter.capabilities
    monkeypatch.setattr(
        google.GoogleSearchAdapter,
        "capabilities",
        lambda self: caps(self).model_copy(update={"page_rules": ("terms",)}),
    )
    serve(conn, monkeypatch, GOOD)
    monkeypatch.setattr(crawl, "resolve", lambda host: ["10.0.0.7"])
    with caplog.at_level(logging.INFO), pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_g", "sections": sections()}, park=True)
    assert str(e.value) == ads_run.PAGE_UNCHECKED.format(label="Google Search")
    assert "could not be checked again for Google Search: the page address must be a public address" in caplog.text


def test_the_primarys_re_check_that_breaks_is_never_hidden(conn, monkeypatch):
    """Item 6, the primary's side unchanged: were Google Search to need a rule its stale report lacks, a failure of
    its re-check that is no refusal propagates -- only a secondary channel's is turned into its parked row."""
    seed_plan(conn, both(), status="draft", check=STALE)
    funds(conn)
    caps = google.GoogleSearchAdapter.capabilities
    monkeypatch.setattr(
        google.GoogleSearchAdapter,
        "capabilities",
        lambda self: caps(self).model_copy(update={"page_rules": ("terms",)}),
    )

    def broken(*args, **kwargs):
        raise RuntimeError("the kit's own words")

    monkeypatch.setattr(landing, "kit", broken)
    with pytest.raises(RuntimeError):
        ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_g", "sections": sections()}, park=True)


def test_running_newsbreak_ads_a_new_version_cannot_reach_keep_their_version_and_say_why(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
):
    """Item 10 (R17 on ads already built): v1 runs on both channels; the business's address moves abroad and v2 is
    approved. NewsBreak's far point parks its running row as it is, in the approval itself (before any step runs):
    its ads keep v1 (nothing sent to NewsBreak), the row says why, and every hourly step checks again at no NewsBreak
    cost -- while Google Search takes v2."""
    key, v1 = seed_plan(conn, both(), status="draft")
    funds(conn)
    approve(client, conn, key, v1)
    assert (row(conn)["state"], nb_row(conn)["state"]) == ("live", "live")
    conn.execute("UPDATE knowledge SET json = %s WHERE business_id = %s", (Jsonb(TORONTO), BIZ))
    secs = both()
    secs["ads"]["channels"][1]["monthly_usd"] = 400.0
    v2 = add_plan_by_hand(conn, secs, status="draft")
    newsbreak.sent.clear()
    prop = proposals.propose_plan(conn, BIZ, v2)
    with conn.transaction():  # the approval's own transaction: no launch step has run yet
        proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    parked = nb_row(conn)
    assert (parked["state"], parked["plan_id"], parked["last_error"]) == ("live", v1, nb.FAR_POINT)
    said = ads_run.status_sentence(parked)
    assert said == f"NewsBreak: live, but your latest change is not all in yet. {nb.FAR_POINT}." and plain(said)
    due = f"SELECT business_id FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}"
    assert [r["business_id"] for r in conn.execute(due).fetchall()] == [BIZ]
    for _hour in range(2):
        out = ads_run.launch(conn, config.load(), BIZ, NB)
        assert (out["state"], out["plan_id"], out["last_error"], out["ops"]) == ("live", v1, nb.FAR_POINT, 0)
    assert newsbreak.kinds() == []
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["plan_id"] == v2


# ---------------------------------------------------------------- R26: stops before starts in the approval's request


@pytest.fixture
def clocked(monkeypatch) -> StepClock:
    """One standing clock for the approval's budget (`ads_run.launch_now`) and both adapters' clients."""
    stepped = StepClock()
    for module in (ads_run, google, nb):
        monkeypatch.setattr(module, "time", stepped)
    return stepped


def test_a_version_that_drops_newsbreak_pauses_it_in_the_approval_even_when_google_searchs_launch_uses_the_budget(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    clocked,
):
    """R26: the approval's own request stops spend before it starts any: NewsBreak's pause for a version that leaves it
    out goes first, so Google Search's launch -- here one whose first answer outlasts the whole budget -- can never
    leave NewsBreak's ads running until the hourly job; the answer still reads in card order."""
    key, v1 = seed_plan(conn, nb_only(), status="approved")
    funds(conn)
    ads_run.queue(conn, BIZ, NB, v1, ready=True)
    assert ads_run.launch(conn, config.load(), BIZ, NB)["state"] == "live"
    v2 = add_plan_by_hand(conn, sections(), status="draft")  # Google Search alone
    gads.delay, gads.sleep = 25.0, clocked.sleep
    newsbreak.sent.clear()
    envelope = approve(client, conn, key, v2)
    dropped = nb_row(conn)
    assert (dropped["state"], dropped["paused_reason"], dropped["resources"].get("pause_owed")) == (
        "paused",
        "dropped",
        None,
    )
    assert newsbreak.bodies("campaign/updateStatus") == [{"status": "OFF"}]
    assert all(c["status"] == "OFF" for c in newsbreak.campaigns.values())
    assert (row(conn)["state"], row(conn)["last_error"]) == ("queued", google.RETRYING)  # out of budget: the hour's
    say = envelope["say"]
    assert say.index("Google Search:") < say.index("NewsBreak:")


# ---------------------------------------------------------------- the token, nowhere but in `settings`


def test_no_secret_reaches_a_row_the_audit_a_mail_or_the_log_through_the_framework(
    client,
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    newsbreak,
    resend,
    caplog,
):
    """ADS_SECRETS (ADS_ENV without the Organization's id, which httpx's own log line prints in a query) never reach
    the rows, the accounts, NewsBreak's review kept for the card, the audit, a mail or the log -- through an approval
    that launches both channels, an hour's read with an ad turned down in words that echo the token, a pause, and a
    NewsBreak refusal that echoes it."""
    key, plan_id = seed_plan(conn, both(), status="draft")
    funds(conn)
    token = ADS_ENV["NEWSBREAK_ACCESS_TOKEN"]
    with caplog.at_level(logging.DEBUG):
        approve(client, conn, key, plan_id)
        first = next(iter(newsbreak.ads))
        newsbreak.reject(first, f"Rejected. Ref {token}: the page does not load.")
        ads_run.sync(conn, config.load(), BIZ, NB)
        ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
        newsbreak.fail["ad-set/create"] = nb_answer(4033, f"Invalid access token {token}")
        _key2, other = seed_plan(conn, nb_only(), biz="biz_2", status="approved", cid="9876543210")
        funds(conn, biz="biz_2")
        ads_run.queue(conn, "biz_2", NB, other, ready=True)
        ads_run.launch(conn, config.load(), "biz_2", NB)
    issue = conn.execute("SELECT platform_text FROM channel_issue WHERE business_id = %s", (BIZ,)).fetchone()
    assert issue["platform_text"] == "Rejected. Ref [token]: the page does not load."
    stored = [
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM channel_campaign").fetchall()], default=str),
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM channel_account").fetchall()], default=str),
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM channel_issue").fetchall()], default=str),
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM agent_action").fetchall()], default=str),
        json.dumps(resend.sent),
        caplog.text,
    ]
    for secret in ADS_SECRETS:
        for text in stored:
            assert secret not in text
