"""agent/channels/newsbreak/adapter.py: the NewsBreak adapter (plan 2026-09-29-agent-m1-newsbreak Task 5: 5a's write
path, the controller's rulings R1-R12, and 5b's read path, R13-R16 and R21-R28; spec 2026-09-29-admiry-channels-design
D84, D88-D90) over `FakeNewsBreak`. What the channel is and what the owner connects (nothing); whether the business's
account can carry ads; the far point NewsBreak's precheck refuses (R9); the build (the ZIP codes, the linted copy, the
approved image, the name it shows); `validate` recording the bodies and calling nothing (R12); a live launch -- an ad
account per business, everything created OFF and turned ON only by the enable (Review Focus 1, R2), a lost answer
recovered by name (Review Focus 2), a throttle resumed at the missing step, one request a second -- the spending cap as
a lifetime cap (R5), the lead event and the account's own trouble as operator work (R10, R11); the update (R4: one
ad-set update with the whole targeting; new ads ON beside the old ones), the pause and the owed pause; the page follow
(R7, R8); the hourly read -- the HOURLY report per day (R13), NewsBreak's review in its own words (Review Focus 4,
R14), the old ads turned off once the new ones pass review (R15, R21, R25), the month's cap re-set (R16, R22), the ad
set's end date (R23); the conversions back by click id; and the token nowhere but in `settings`."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import psycopg
import pytest
from fastmcp.exceptions import ToolError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent import ads_run, business, config, db, geo, plans, state, tools_account, wallet
from agent.channels import base, registry
from agent.channels.newsbreak import adapter as nb
from agent.channels.newsbreak import build as nb_build
from agent.channels.newsbreak import client as newsbreak
from agent.channels.newsbreak import creative
from agent.channels.newsbreak.adapter import ADAPTER, ONLY_MANAGED, NewsBreakAdapter
from agent.tests import fixtures
from agent.tests.conftest import ACCOUNT_JARGON, ADS_SECRETS, JARGON, MONEY_JARGON, TEST_DB_URL
from agent.tests.fake_newsbreak import envelope, ok
from agent.tests.test_ads_run import CHECK_OK, KNOWLEDGE, PAGE_URL, StepClock, approve, sections
from agent.tests.test_newsbreak_build import DESCRIPTIONS, HEADLINES, NEW_HEADLINES

BIZ = "biz_1"
NB = "newsbreak"
NO_FUNDS = "Add funds to start NewsBreak"
# the account's own step; the picture is the plan's (its card, and the "Ready to launch" card's line: M1 Task 7). It
# never asks for money itself: while NewsBreak is not ready, what it waits for is its account's reason (M1 follow-up
# 2, W3: funds, or NewsBreak getting the account ready)
CONNECT = "Nothing to connect: Admiry runs NewsBreak from your balance"
CLICK_URL = PAGE_URL + "?src=nb&newsbreak_cid=__CALLBACK_PARAM__"
IMAGE = "dentists/1.jpg"
ACCOUNT_NAME = "Admiry · Bright Smile Dental · biz_1"
LA = "America/Los_Angeles"
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)
# what a fresh launch sends, in order (Review Focus 1): the account looked up by name (none) and made, its lead event
# looked for (R10), the image uploaded, the campaign, the ad set and the two ads made, the spending cap read and set
# (R5) -- and then the enable's switches
LAUNCH = [
    "ad-account/getGroupsByOrgIds",
    "ad-account/create",
    "event/getList",
    "ad/uploadAssets",
    "campaign/create",
    "ad-set/create",
    "ad/create",
    "ad/create",
    "balance/getAccountBudgetInfo",
    "balance/updateAccountsBudget",
]
ENABLE = ["campaign/updateStatus", "ad-set/updateStatus", "ad/updateStatus", "ad/updateStatus"]


def _plain(sentence: str) -> bool:
    return not any(j.search(sentence) for j in (JARGON, ACCOUNT_JARGON, MONEY_JARGON))


def _business(conn, biz: str = BIZ) -> None:
    conn.execute("INSERT INTO business (id) VALUES (%s) ON CONFLICT DO NOTHING", (biz,))


def _account(mode: str = "managed", biz: str = BIZ) -> base.Account:
    return base.Account(business_id=biz, channel=NB, mode=mode, status="none", external={}, secret=None)


def _funds(conn, cents: int = 50_000, biz: str = BIZ) -> None:
    wallet.add_entry(conn, biz, kind="topup", amount_cents=cents, ref=f"pi_{biz}_{cents}", created_by="stripe")


def nb_sections(
    *,
    monthly_usd: float = 600.0,
    headlines: list[str] = HEADLINES,
    descriptions: list[str] = DESCRIPTIONS,
    image: str | None = IMAGE,
    radius_miles: float = 5.0,
) -> dict:
    """A consultation plan funding NewsBreak alone (the pilot's shape), its own lines and its approved image."""
    secs = sections(radius_miles=radius_miles)
    secs["ads"]["channels"] = [
        {
            "channel": NB,
            "monthly_usd": monthly_usd,
            "headlines": list(headlines),
            "descriptions": list(descriptions),
            "landing_service": "Invisalign",
            "mode": "managed",
        }
    ]
    secs["creative"] = {"angles": [], **({"approved_image": {NB: image}} if image else {})}
    return secs


def add_plan(conn, secs: dict, *, status: str = "approved", biz: str = BIZ) -> str:
    """A plan version (the one before superseded when this one is approved)."""
    if status == "approved":
        conn.execute("UPDATE plan SET status = 'superseded' WHERE business_id = %s AND status = 'approved'", (biz,))
    version = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 AS v FROM plan WHERE business_id = %s", (biz,)
    ).fetchone()["v"]
    plan_id = db.new_id("pl")
    body = {"sections": secs, "lines": plans.owner_lines(secs), "rationale": "", "notes": []}
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES (%s, %s, %s, %s, %s)",
        (plan_id, biz, version, Jsonb(body), status),
    )
    return plan_id


def seed(
    conn, *, knowledge: dict | None = None, secs: dict | None = None, biz: str = BIZ, status: str = "approved"
) -> str:
    """A business ready to launch NewsBreak: knowledge with a listing point in Palo Alto, a $2,000 goal, a funded
    balance, a published page whose check passed, and a plan (approved unless `status` says otherwise) putting $600 a
    month on NewsBreak. Returns the plan id."""
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'verified') ON CONFLICT DO NOTHING", (biz,))
    conn.execute(
        "INSERT INTO knowledge (business_id, version, json, source) VALUES (%s, 1, %s, 'extract')",
        (biz, Jsonb(KNOWLEDGE if knowledge is None else knowledge)),
    )
    business.set_goal(conn, biz, monthly_limit_usd=2000)
    _funds(conn, biz=biz)
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, %s, 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), biz, PAGE_URL, Jsonb(CHECK_OK)),
    )
    return add_plan(conn, secs or nb_sections(), biz=biz, status=status)


def queue(conn, plan_id: str, biz: str = BIZ) -> None:
    ads_run.queue(conn, biz, NB, plan_id, ready=True)


def unlisted_on(conn, fake) -> set[str]:
    """The ads running at NewsBreak that the row does not list -- `ads`, the ads on their way out (`replaced`: a
    finished change's old ads; `leftover`: an attempt's that will not finish), or an update in flight (`updating`,
    each ad remembered as it is made): none may ever be (fix round 1)."""
    resources = nb_row(conn)["resources"]
    listed = set((resources.get("ads") or {}).values()) | set(resources.get("replaced") or [])
    listed |= set(resources.get("leftover") or [])
    listed |= set(((resources.get("updating") or {}).get("ads") or {}).values())
    return {ad for ad, body in fake.ads.items() if body["status"] == "ON"} - listed


def answer_ad_creates(fake, when, answer) -> None:
    """NewsBreak answers `answer` to every ad create whose body `when` picks (the reviewer's probe's own device)."""
    real = fake._ad_create

    def guarded(ident, payload):
        return answer if when(payload) else real(ident, payload)

    fake._ad_create = guarded


def nth(n: int):
    """A `when` for `answer_ad_creates`: the n-th ad create from now on."""
    seen = {"count": 0}

    def pick(payload) -> bool:
        seen["count"] += 1
        return seen["count"] == n

    return pick


def refuse_on(fake, kind: str, ids: set[str] | None = None) -> None:
    """NewsBreak refuses to switch ON (code 41926, the final review's probe) the objects of `kind` -- "ad", "ad-set" or
    "campaign" -- whose ids are in `ids` (every one when None); an OFF always goes through."""
    name = "_" + kind.replace("-", "_") + "_updateStatus"
    real = getattr(fake, name)

    def refusing(ident, payload):
        if payload.get("status") == "ON" and (ids is None or ident in ids):
            return envelope(41926, "this cannot be turned on")
        return real(ident, payload)

    setattr(fake, name, refusing)


def nb_row(conn, biz: str = BIZ) -> dict:
    return ads_run.get(conn, biz, NB)


def external(conn, biz: str = BIZ) -> dict:
    return registry.account(conn, biz, NB).external


def step(conn) -> dict:
    return ads_run.launch(conn, config.load(), BIZ, NB)


@pytest.fixture
def images(tmp_path, monkeypatch):
    """The licensed image set, generated (Task 3's fixture): dentists/1.jpg is the dental business's."""
    monkeypatch.setattr(creative, "ROOT", fixtures.creative_set(tmp_path / "creative"))


@pytest.fixture
def nb_live(monkeypatch, nbapi, images):
    """NewsBreak faked and its switch `live`, the clock pinned (`ads_run.now`); returns the fake."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    monkeypatch.setattr(ads_run, "now", lambda: NOW)
    return nbapi


@pytest.fixture
def launched(conn, nb_live) -> str:
    """A business whose NewsBreak ads are live (plan v1, $600 a month). Returns v1's id."""
    v1 = seed(conn)
    queue(conn, v1)
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", None), out
    nb_live.sent.clear()
    return v1


def built(conn) -> tuple[base.Account, dict, base.Build]:
    """The account, the approved plan and its build, as the lifecycle hands them to the adapter."""
    settings = config.load()
    plan = ads_run.approved(conn, BIZ)
    account = ads_run._account(conn, BIZ, NB)
    knowledge = business.latest_knowledge(conn, BIZ)[1]
    return account, plan, ADAPTER.build(conn, settings, BIZ, plan, knowledge, account)


# ---------------------------------------------------------------- what NewsBreak is, and the account half


def test_importing_the_channels_package_registers_newsbreak_after_google_search():
    assert isinstance(ADAPTER, NewsBreakAdapter) and isinstance(ADAPTER, base.Adapter)
    assert registry.adapter("newsbreak") is ADAPTER and registry.known("newsbreak")
    assert list(registry.CHANNELS) == ["google_search", "newsbreak"]  # registry.ORDER


def test_capabilities_are_the_specs_newsbreak_and_its_ads_follow_the_page():
    assert ADAPTER.capabilities() == base.Capabilities(
        create=True,
        update=True,
        pause=True,
        metrics=True,
        conversions=True,
        leads=False,
        modes={"managed"},
        min_monthly_usd=300,
        geo="zip",
        lead_formats={"call", "form"},
        needs_image=True,
        validate="dry",
        page_rules=("terms", "contact"),
        follows_page=True,  # Corrections 12 / R8: the ads carry the page, and move with it
    )
    assert ads_run.follows_page(NB)


@pytest.mark.parametrize("mode", ["managed", "own_account"])
def test_there_is_nothing_to_connect(mode):
    for account in (None, _account(mode)):
        assert ADAPTER.connect_steps(account, mode) == [base.Step(CONNECT)]
    assert _plain(CONNECT) and "funds" not in CONNECT  # W3: the step never asks for money the balance may have


def test_check_account_without_funds_says_add_funds_and_opens_the_managed_account_row(conn):
    _business(conn)
    got = ADAPTER.check_account(conn, config.load(), _account())
    assert got == base.AccountStatus(ready=False, status="none", reason=NO_FUNDS, account_public=None)
    assert NO_FUNDS == ads_run.NO_FUNDS.format(label="NewsBreak") and _plain(NO_FUNDS)  # the card's "waiting for funds"
    row = registry.account(conn, BIZ, "newsbreak")
    assert (row.mode, row.status, row.external) == ("managed", "none", {})
    assert registry.managed_channels(conn, BIZ) == ["newsbreak"]  # the money paths see it from now on


def test_check_account_is_ready_once_the_business_has_a_balance_and_shows_the_ad_account_once_there_is_one(conn):
    """Ready with a balance -- the ad account itself is made by the launch -- and from then on the statement shows
    NewsBreak's id for it (D88: the account id appears on the owner's statement)."""
    _business(conn)
    _funds(conn)
    got = ADAPTER.check_account(conn, config.load(), _account())
    assert (got.ready, got.reason, got.account_public) == (True, None, None)
    registry.save_account(conn, BIZ, NB, mode="managed", status="active", external={"ad_account_id": "1000001"})
    got = ADAPTER.check_account(conn, config.load(), registry.account(conn, BIZ, NB))
    assert (got.ready, got.status, got.reason, got.account_public) == (True, "active", None, "1000001")


def test_check_account_is_never_ready_on_a_row_that_is_not_managed_and_says_why(conn):
    _business(conn)
    _funds(conn)
    registry.save_account(conn, BIZ, "newsbreak", mode="own_account", status="active", external={})
    got = ADAPTER.check_account(conn, config.load(), _account("own_account"))
    assert (got.ready, got.reason) == (False, ONLY_MANAGED) and _plain(ONLY_MANAGED)
    assert "balance" in ONLY_MANAGED and "Add funds" not in ONLY_MANAGED
    assert registry.account(conn, BIZ, "newsbreak").mode == "own_account"  # an existing row is never rewritten


def test_check_account_without_an_account_is_not_ready():
    assert ADAPTER.check_account(None, config.load(), None) == base.AccountStatus(False, "none", NO_FUNDS, None)


def test_check_account_reads_only_the_database(conn, monkeypatch):
    """It runs inside the approval's transaction and every channels-card render (which rolls it back): it never
    builds a client, so it never reaches NewsBreak or the operator's mail from there."""
    _business(conn)
    _funds(conn)
    external_ = {"ad_account_id": "1000001", "account_status": "PENDING", "account_checked_at": NOW.isoformat()}
    registry.save_account(conn, BIZ, NB, mode="managed", status="needs_attention", external=external_)
    monkeypatch.setattr(ads_run, "now", lambda: NOW)
    got = ADAPTER.check_account(conn, config.load(), registry.account(conn, BIZ, NB))  # no nbapi: a client would fail
    assert (got.ready, got.reason) == (False, nb.ACCOUNT_WAIT)


# ---------------------------------------------------------------- R9: a far or foreign point


TORONTO = {"lat": 43.6532, "lng": -79.3832}


def _knowledge_at(point: dict) -> dict:
    return {**KNOWLEDGE, "listing": {**KNOWLEDGE["listing"], **point}}


def test_precheck_passes_a_point_among_us_zip_codes(conn):
    assert ADAPTER.precheck(conn, config.load(), BIZ, {"sections": nb_sections()}, KNOWLEDGE) is None


@pytest.mark.parametrize(
    "point",
    [TORONTO, {"lat": -122.143, "lng": 37.4419}],
    ids=["Toronto: 34 miles from the nearest ZIP", "latitude and longitude swapped"],
)
def test_precheck_refuses_a_point_farther_from_any_us_zip_than_the_radius_and_ten_miles(conn, point):
    with pytest.raises(ToolError) as e:
        ADAPTER.precheck(conn, config.load(), BIZ, {"sections": nb_sections(radius_miles=5)}, _knowledge_at(point))
    assert str(e.value) == nb.FAR_POINT and _plain(nb.FAR_POINT) and "NewsBreak" in nb.FAR_POINT


def test_the_far_point_rule_allows_the_radius_plus_ten_miles(conn):
    """Toronto's nearest ZIP centroid is ~34 miles away: a 25-mile radius (35 with the slack) reaches it."""
    _zip, away = geo.nearest(TORONTO["lat"], TORONTO["lng"])
    assert 30 < away < 35 and nb.FAR_SLACK_MILES == 10
    plan = {"sections": nb_sections(radius_miles=25)}
    assert ADAPTER.precheck(conn, config.load(), BIZ, plan, _knowledge_at(TORONTO)) is None
    with pytest.raises(ToolError):
        ADAPTER.precheck(conn, config.load(), BIZ, {"sections": nb_sections(radius_miles=20)}, _knowledge_at(TORONTO))


def test_precheck_leaves_a_missing_point_or_radius_to_the_lifecycles_rule(conn):
    """No point (NO_LOCATION) and no radius (NO_RADIUS) are the lifecycle's own rules for a `zip` channel."""
    no_point = {k: v for k, v in KNOWLEDGE.items() if k != "listing"}
    assert ADAPTER.precheck(conn, config.load(), BIZ, {"sections": nb_sections()}, no_point) is None
    secs = nb_sections()
    secs["consultation"]["geo"] = {}
    assert ADAPTER.precheck(conn, config.load(), BIZ, {"sections": secs}, KNOWLEDGE) is None


# ---------------------------------------------------------------- the build


def test_the_build_is_the_bodies_newsbreak_will_get_for_the_zip_codes_in_the_radius(conn, images, monkeypatch):
    monkeypatch.setattr(ads_run, "now", lambda: NOW)
    seed(conn)
    _account, plan, build = built(conn)
    payload = build.payload
    zips = geo.zips_within(KNOWLEDGE["listing"]["lat"], KNOWLEDGE["listing"]["lng"], 5.0)
    ids, unmapped = geo.newsbreak_ids(zips)
    assert (build.channel, build.plan_id, build.page_url) == (NB, plan["id"], PAGE_URL)
    assert (payload["location_ids"], payload["unmapped_zips"]) == (ids, unmapped) and ids
    assert payload["account"] == {
        "name": ACCOUNT_NAME,
        "company": "Bright Smile Dental",
        "industry": newsbreak.industry("dentists"),
    }
    assert payload["campaign"] == nb_build.campaign_body(name=ACCOUNT_NAME)
    start = int(NOW.timestamp())
    assert payload["ad_set"] == nb_build.ad_set_body(
        campaign_id=None,
        name=ACCOUNT_NAME,
        daily_cents=1973,
        cpc_cents=150,
        location_ids=ids,
        language="en_us",
        start=start,
        end=start + nb.END_DAYS * 86_400,
    )
    headlines, descriptions, _notes = nb_build.ad_copy(plan)  # R3: the linted copy, never the plan's raw row
    assert payload["ads"] == nb_build.ad_bodies(
        ad_set_id=None,
        brand="Bright Smile Dental",
        headlines=headlines,
        descriptions=descriptions,
        cta=nb_build.call_to_action(plan),
        asset_url=None,  # NewsBreak's CDN address exists only once the launch uploads the image
        logo_url=None,
        click_url=CLICK_URL,
    )
    assert all(body["status"] == "OFF" for body in (payload["campaign"], payload["ad_set"], *payload["ads"]))
    assert (payload["image"], payload["click_url"], payload["cap_cents"]) == (IMAGE, CLICK_URL, 60_000)
    assert build.summary == {"daily_usd": 19.73, "geo_text": f"{len(ids)} ZIP codes within 5 miles", "ads": 2}
    json.dumps(payload)  # it is what `validate` records in `resources`


def test_the_build_drops_copy_newsbreak_would_turn_down_and_refuses_when_none_is_left(conn, images):
    seed(conn, secs=nb_sections(headlines=["x" * 91, "Invisalign in Palo Alto"], descriptions=["Book today."]))
    *_rest, build = built(conn)
    assert [ad["creative"]["headline"] for ad in build.payload["ads"]] == ["Invisalign in Palo Alto"]
    add_plan(conn, nb_sections(headlines=["x" * 91], descriptions=["Book today."]))
    with pytest.raises(base.PlatformError) as e:
        built(conn)
    assert (str(e.value), e.value.retryable) == (nb.TOO_LITTLE_COPY, False) and _plain(nb.TOO_LITTLE_COPY)


def test_a_blank_line_is_no_ad_copy(conn, images):
    """`build.lint` keeps a blank line; an ad of one would go to NewsBreak with nothing to read: dropped here."""
    seed(conn, secs=nb_sections(headlines=["", "   ", "Invisalign in Palo Alto"], descriptions=["Book today.", " "]))
    *_rest, build = built(conn)
    ads = [(ad["creative"]["headline"], ad["creative"]["description"]) for ad in build.payload["ads"]]
    assert ads == [("Invisalign in Palo Alto", "Book today.")]


def test_a_business_name_newsbreak_cannot_show_is_a_refusal_that_is_never_retried(conn, images):
    """R3: `build.BrandNameError` (no word of the name fits NewsBreak's 25-character advertiser name) becomes the owner's
    sentence on a PlatformError that is not retryable: the launch fails with it, and the next approval starts over."""
    seed(conn, knowledge={**KNOWLEDGE, "name": "Supercalifragilisticexpialidocious Dentistry"})
    with pytest.raises(base.PlatformError) as e:
        built(conn)
    assert str(e.value) == nb_build.BRAND_TOO_LONG.format(max=newsbreak.BRAND_NAME_MAX)
    assert e.value.retryable is False


def test_the_build_names_the_versions_picture_whether_or_not_the_set_still_has_it(conn, images):
    """Final review I3: a picture must be in Admiry's set only to be uploaded (`_asset`), so the build -- which every
    launch, update and page move makes -- never refuses for it: running ads keep the picture they carry. Its picture
    is the version's own, pinned when it was made, whether or not its approval could store it."""
    seed(conn, secs=nb_sections(image="dentists/9.jpg"))
    *_rest, build = built(conn)
    assert build.payload["image"] == "dentists/9.jpg"
    secs = nb_sections(image=None)
    secs["creative"]["proposed_image"] = {NB: "dentists/9.jpg"}  # its approval stored none: the picture left the set
    add_plan(conn, secs)
    *_rest, build = built(conn)
    assert build.payload["image"] == "dentists/9.jpg"


def test_a_place_with_no_zip_newsbreak_lists_waits_with_its_sentence(conn, images, monkeypatch):
    seed(conn)
    monkeypatch.setattr(geo, "newsbreak_ids", lambda zips: ([], list(zips)))
    with pytest.raises(ToolError) as e:
        built(conn)
    assert str(e.value) == nb.NO_ZIPS and _plain(nb.NO_ZIPS)


# ---------------------------------------------------------------- R12: validate records and calls nothing


def test_validate_records_the_bodies_on_the_row_and_calls_nothing(conn, nbapi, images, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    monkeypatch.setattr(ads_run, "now", lambda: NOW)
    v1 = seed(conn)
    queue(conn, v1)
    out = step(conn)
    row = nb_row(conn)
    assert (out["state"], out["ops"], row["validated_at"] is not None) == ("queued", 0, True)
    *_rest, build = built(conn)
    assert row["resources"] == {"validated": build.payload}
    assert nbapi.requests == []  # nothing reached NewsBreak, the account included


def test_validate_builds_no_client_at_all(conn, images):
    """The adapter's own `launch(validate_only=True)` makes no client -- here without the fake, where any client the
    NewsBreak module made would fail the test."""
    seed(conn)
    account, _plan, build = built(conn)
    done = ADAPTER.launch(conn, config.load(), account, build, {}, http=None, validate_only=True)
    assert done == base.Resources(ids={"validated": build.payload})


# ---------------------------------------------------------------- Review Focus 1: paused, then on


def test_live_launch_creates_paused_then_enables(conn, nb_live):
    v1 = seed(conn)
    queue(conn, v1)
    out = step(conn)
    assert (out["state"], out["last_error"], out["ops"]) == ("live", None, len(LAUNCH) + len(ENABLE))
    assert nb_live.kinds() == LAUNCH + ENABLE
    (account_id,) = nb_live.accounts
    assert nb_live.bodies("ad-account/create") == [
        {
            "adAccountName": ACCOUNT_NAME,
            "companyName": "Bright Smile Dental",
            "industry": "Health and Medical Services//Dental Care",
            "accountCategory": "LEADGEN-LOCAL",
            "orgId": "org_1",
        }
    ]
    # R2: every create went OFF (NewsBreak's default is ON), and only the enable turned anything ON
    creates = [*nb_live.bodies("campaign/create"), *nb_live.bodies("ad-set/create"), *nb_live.bodies("ad/create")]
    assert len(creates) == 4 and all(body["status"] == "OFF" for body in creates)
    switches = nb_live.bodies("campaign/updateStatus") + nb_live.bodies("ad-set/updateStatus")
    switches += nb_live.bodies("ad/updateStatus")
    assert switches == [{"status": "ON"}] * 4
    (ad_set,) = nb_live.ad_sets.values()
    ids, _unmapped = geo.newsbreak_ids(geo.zips_within(37.4419, -122.143, 5.0))
    assert (ad_set["budget"], ad_set["bidType"], ad_set["bidRate"]) == (1973, "CPC", 150)
    assert ad_set["targeting"]["location"] == {"positive": ids}
    (asset,) = nb_live.assets
    for ad in nb_live.ads.values():
        content = ad["creative"]["content"]
        assert (content["assetUrl"], content["clickThroughUrl"], content["brandName"]) == (
            asset,
            CLICK_URL,
            "Bright Smile Dental",
        )
    assert nb_live.caps == {account_id: 60_000}  # R5: no spend yet, so the month's $600
    assert all(o["status"] == "ON" for o in (*nb_live.campaigns.values(), ad_set, *nb_live.ads.values()))
    row = nb_row(conn)
    (campaign,) = nb_live.campaigns
    first, second = nb_live.ads  # in the order they were made
    assert row["plan_id"] == v1 and row["resources"] == {
        "ad_account_id": account_id,
        "campaign": campaign,
        "ad_set_id": ad_set["id"],
        "ads": {"0": first, "1": second},
        "assets": {IMAGE: asset},
        "cap_cents": 60_000,
        "cap_base_cents": 0,  # NewsBreak's lifetime total at the launch (R5 as amended in fix round 1)
        "cap_month": "2026-10",  # the account month that base is for: the first sync of the next one re-sets it (R22)
        "time_zone": LA,
        "location_ids": ids,
        "click_url": CLICK_URL,
    }
    assert external(conn) == {"ad_account_id": account_id, "account_status": "ACTIVE", "spending_cap_cents": 60_000}
    assert registry.account(conn, BIZ, NB).status == "active"


def test_a_business_sends_at_most_one_request_a_second(conn, nb_live, monkeypatch):
    """The Global Constraint, per business and across steps (fix round 1): one pause between every two requests of a
    business -- the launch's last and the enable's first, a pause right after, included -- measured from each request's
    start; never a real sleep in a test (`nbapi` replaces `adapter.wait`, keeping what it was asked in `waits`)."""
    v1 = seed(conn)
    queue(conn, v1)
    step(conn)
    assert len(nb_live.requests) == len(LAUNCH) + len(ENABLE)
    assert len(nb_live.waits) == len(nb_live.requests) - 1
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")  # another step, another client: the same business
    assert len(nb_live.waits) == len(nb_live.requests) - 1 == len(LAUNCH) + len(ENABLE)
    assert all(0 < waited <= nb.PACE_S for waited in nb_live.waits)


def test_the_pace_runs_from_each_requests_start(conn, nb_live, clock, monkeypatch):
    """Answers that take half a second leave half a second to wait: one request a second, never one a second and a
    half (the pace measured from each answer)."""
    monkeypatch.setattr(nb, "wait", lambda seconds: (nb_live.waits.append(seconds), clock.sleep(seconds)))
    nb_live.on_request.append(lambda request: clock.sleep(0.5))
    v1 = seed(conn)
    queue(conn, v1)
    start = clock.now
    step(conn)
    count = len(LAUNCH) + len(ENABLE)
    assert nb_live.waits == [pytest.approx(0.5)] * (count - 1)
    assert clock.now - start == pytest.approx(count - 1 + 0.5)  # a request each second, the last one's answer


def test_two_businesses_do_not_wait_for_each_other(conn, nb_live):
    v1 = seed(conn)
    queue(conn, v1)
    v2 = seed(conn, biz="biz_2")
    ads_run.queue(conn, "biz_2", NB, v2, ready=True)
    step(conn)
    ads_run.launch(conn, config.load(), "biz_2", NB)
    assert len(nb_live.requests) == 2 * (len(LAUNCH) + len(ENABLE))
    assert len(nb_live.waits) == len(nb_live.requests) - 2  # each business's first request goes at once


# ---------------------------------------------------------------- Review Focus 2: a lost answer never duplicates


def test_lost_responses_are_recovered_by_name(conn, nb_live):
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.lose = {"ad-account/create"}
    first = step(conn)
    assert (first["state"], first["last_error"]) == ("queued", nb.RETRYING)
    assert len(nb_live.accounts) == 1 and not nb_live.campaigns  # made at NewsBreak, its answer lost
    nb_live.sent.clear()
    nb_live.lose = {"campaign/create"}
    second = step(conn)
    assert (second["state"], second["last_error"]) == ("queued", nb.RETRYING)
    assert nb_live.kinds() == [
        "ad-account/getGroupsByOrgIds",  # found by name: no second account
        "event/getList",
        "ad/uploadAssets",
        "campaign/getList",  # an account made earlier may hold the campaign already
        "campaign/create",
    ]
    assert len(nb_live.campaigns) == 1 and not nb_live.ad_sets
    nb_live.sent.clear()
    third = step(conn)
    assert third["state"] == "live"
    assert "ad-account/create" not in nb_live.kinds() and "campaign/create" not in nb_live.kinds()
    assert nb_live.kinds()[:4] == ["event/getList", "campaign/getList", "ad-set/getList", "ad-set/create"]
    assert (len(nb_live.accounts), len(nb_live.campaigns), len(nb_live.ad_sets), len(nb_live.ads)) == (1, 1, 1, 2)
    (account_id,) = nb_live.accounts
    assert nb_row(conn)["resources"]["ad_account_id"] == account_id == external(conn)["ad_account_id"]


def test_a_lost_ad_set_answer_is_found_by_name_and_the_ads_are_made_once(conn, nb_live):
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.lose = {"ad-set/create"}
    assert step(conn)["state"] == "queued"
    nb_live.lose = {"ad/create"}
    assert step(conn)["state"] == "queued"
    nb_live.sent.clear()
    assert step(conn)["state"] == "live"
    assert nb_live.kinds()[:4] == ["event/getList", "ad/getList", "ad/create", "balance/getAccountBudgetInfo"]
    assert (len(nb_live.ad_sets), len(nb_live.ads)) == (1, 2)


# ---------------------------------------------------------------- a throttle mid-launch


def test_a_throttle_mid_launch_leaves_the_row_queued_with_busy_and_the_next_run_resumes_at_the_missing_step(
    conn, nb_live
):
    """NewsBreak busy (4034): the row is queued with Admiry's retry sentence (fix round 1: never the client's "try
    again" -- Admiry retries), and the next run resumes at the step it missed."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.throttle_endpoints = {"ad-set/create"}
    nb_live.throttle = 1
    out = step(conn)
    row = nb_row(conn)
    assert (out["state"], out["last_error"]) == ("queued", nb.RETRYING)
    assert (row["state"], row["last_error"]) == ("queued", nb.RETRYING)
    assert "campaign" not in row["resources"]  # nothing counts as built until the launch returns
    made = row["resources"]  # what was made is on the row (`ads_run.remember`) as it was made
    assert made["ad_account_id"] in nb_live.accounts and made["campaign_id"] in nb_live.campaigns
    assert made["assets"] and made["creating_plan_id"] == v1
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert nb_live.kinds() == [
        "event/getList",  # the lead event is still looked for: no operator could be told yet (no address here)
        "ad-set/getList",  # an earlier attempt may have made it: looked up by name first
        "ad-set/create",
        "ad/create",
        "ad/create",
        "balance/getAccountBudgetInfo",
        "balance/updateAccountsBudget",
        *ENABLE,
    ]
    assert (len(nb_live.accounts), len(nb_live.campaigns), len(nb_live.ad_sets)) == (1, 1, 1)
    assert set(nb_row(conn)["resources"]) >= {"campaign", "ad_set_id", "ads"}
    assert "campaign_id" not in nb_row(conn)["resources"] and "creating_plan_id" not in nb_row(conn)["resources"]


def test_a_newsbreak_without_its_organisation_sends_nothing_and_waits_for_an_operator(conn, nb_live, monkeypatch):
    """Both settings or nothing: without the Organization the account cannot be found or made -- the token's side of
    things, an operator's to fix -- so nothing is sent and the row says Admiry is fixing its access (fix round 2: the
    client's ACCESS_TROUBLE, "try again later", stays for callers)."""
    monkeypatch.delenv("NEWSBREAK_ORG_ID")
    v1 = seed(conn)
    queue(conn, v1)
    out = step(conn)
    assert (out["state"], out["last_error"], out["ops"]) == ("queued", nb.ACCESS_FIXING, 0)
    assert nb_live.requests == [] and "account_status" not in (registry.account(conn, BIZ, NB) or _account()).external


def test_a_refused_create_fails_the_launch_and_is_not_retried(conn, nb_live):
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.fail["campaign/create"] = envelope(41001, "the campaign name is not allowed")
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("failed", newsbreak.REFUSED_WITH_REASON.format(41001))
    nb_live.sent.clear()
    assert step(conn)["state"] == "failed" and nb_live.kinds() == []  # a failed row is never retried by itself


def test_a_create_refused_while_a_newer_version_is_approved_leaves_the_row_to_launch_it(conn, nb_live):
    """M1 follow-up 2 fix round 1 (F3, the review's probe C4): NewsBreak refuses a launch's create before anything is
    built, while a newer version is approved during the (paced, seconds-long) create. Nothing is on, so there is no
    stop to send -- the row decides as one whose stop went through does (`_stopped`): the version the refusal was for
    is no longer the approved one, so the row is not failed (a failed row is due for nothing, and the newer version
    would be stranded); it stays queued on the newer version, and the next step launches it."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.fail["campaign/create"] = envelope(41001, "the campaign name is not allowed")
    made: dict = {}

    def approving(request):
        if nb_live.endpoint(request.url.path)[0] == "campaign/create" and not made:
            made["v"] = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
            queue(conn, made["v"])  # the approval (`proposals.apply_approve_plan` -> `ads_run.queue`)

    nb_live.on_request.append(approving)
    out = step(conn)
    nb_live.on_request.remove(approving)
    del nb_live.fail["campaign/create"]
    assert (out["state"], out["plan_id"], out["last_error"]) == ("queued", made["v"], None)
    due = conn.execute(f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}").fetchall()
    assert len(due) == 1  # the hourly step has it
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", made["v"], None)


def test_a_launch_refused_on_an_ad_is_repaired_by_approving_a_new_version(conn, nb_live):
    """Fix round 1, Important 3 (the reviewer's probe B): NewsBreak refused v1's first ad, so the launch failed; v2,
    approved with other words, builds its own ads in the campaign and ad set already made -- never v1's again, never
    a second campaign or ad set -- and the enable sends v2's whole money, since the ad set holds v1's."""
    v1 = seed(conn)
    queue(conn, v1)
    answer_ad_creates(
        nb_live,
        lambda body: body["creative"]["headline"] == "Invisalign in Palo Alto",
        envelope(41926, "the ad is not allowed"),
    )
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("failed", newsbreak.REFUSED_WITH_REASON.format(41926))
    words = ["Straighter Teeth, Sooner", "Gentle Care in Palo Alto"]
    v2 = add_plan(conn, nb_sections(headlines=words))
    queue(conn, v2)
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    assert {body["creative"]["headline"] for body in nb_live.bodies("ad/create")} == set(words)
    assert (len(nb_live.campaigns), len(nb_live.ad_sets), len(nb_live.ads)) == (1, 1, 2)
    resources = nb_row(conn)["resources"]
    (money,) = nb_live.bodies("ad-set/update")
    assert money == {"budget": 1973, "bidRate": 150, "targeting": nb_build.targeting(resources["location_ids"])}
    assert unlisted_on(conn, nb_live) == set()


def test_a_gone_versions_partial_ads_retire_and_the_approved_version_makes_all_its_own(conn, nb_live, monkeypatch):
    """Fix round 2, item 2 (the re-review's probe R1): v1 made its first ad, then NewsBreak was busy; v1's image left
    the set (v1 no longer builds) and v2 was approved. v2 makes all its own ads -- never v1's first beside v2's second
    -- and v1's ad, OFF, goes to `leftover`; the enable sets v2's whole money on the ad set v1 made."""
    v1 = seed(conn)
    queue(conn, v1)
    answer_ad_creates(nb_live, nth(2), envelope(4034, "Rate limit exceeded"))
    assert step(conn)["last_error"] == nb.RETRYING
    (v1_first,) = nb_row(conn)["resources"]["ads"].values()
    shown = creative.manifest
    monkeypatch.setattr(creative, "manifest", lambda: [e for e in shown() if e["file"] != IMAGE])
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    queue(conn, v2)
    nb_live.sent.clear()
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    running = [nb_live.ads[ad] for ad in resources["ads"].values()]
    assert {ad["creative"]["content"]["headline"] for ad in running} == set(NEW_HEADLINES)
    assert v1_first not in resources["ads"].values() and resources["leftover"] == [v1_first]
    assert nb_live.ads[v1_first]["status"] == "OFF" and unlisted_on(conn, nb_live) == set()
    (money,) = nb_live.bodies("ad-set/update")
    assert set(money) == {"budget", "bidRate", "targeting"}


@pytest.mark.parametrize("cut", [1, 2], ids=["at its first ad", "at its second ad"])
def test_the_approved_version_cut_short_after_a_gone_versions_ads_retire_resumes_with_its_own(
    conn, nb_live, monkeypatch, cut
):
    """Item 2 across a cut: v1's partial ad retired, v2's own launch is cut (NewsBreak busy) at its first or its second
    ad; the next run finishes v2 with v2's own ads -- never v1's again (the row lists none of v1's as v2's), and never
    v2's own sent to `leftover` (the attempt is v2's from the first run on)."""
    v1 = seed(conn)
    queue(conn, v1)
    answer_ad_creates(nb_live, nth(2), envelope(4034, "Rate limit exceeded"))
    assert step(conn)["last_error"] == nb.RETRYING
    (v1_first,) = nb_row(conn)["resources"]["ads"].values()
    shown = creative.manifest
    monkeypatch.setattr(creative, "manifest", lambda: [e for e in shown() if e["file"] != IMAGE])
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    queue(conn, v2)
    answer_ad_creates(nb_live, nth(cut), envelope(4034, "Rate limit exceeded"))
    assert step(conn)["last_error"] == nb.RETRYING
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    running = [nb_live.ads[ad] for ad in resources["ads"].values()]
    assert {ad["creative"]["content"]["headline"] for ad in running} == set(NEW_HEADLINES)
    assert resources["leftover"] == [v1_first] and nb_live.ads[v1_first]["status"] == "OFF"
    v2_made = [ad for ad in nb_live.ads.values() if ad["creative"]["content"]["headline"] in NEW_HEADLINES]
    assert len(v2_made) == 2 and unlisted_on(conn, nb_live) == set()


def test_a_gone_versions_ads_are_listed_on_their_way_out_before_they_leave_the_running_list(conn, nb_live, monkeypatch):
    """R28: the gone version's partial ads go to `leftover` before the row forgets them as its `ads` -- the order
    `_abandon` keeps -- so a step stopped between the two writes leaves every ad the business has at NewsBreak on the
    row, never one listed nowhere."""
    v1 = seed(conn)
    queue(conn, v1)
    answer_ad_creates(nb_live, nth(2), envelope(4034, "Rate limit exceeded"))
    assert step(conn)["last_error"] == nb.RETRYING
    (v1_first,) = nb_row(conn)["resources"]["ads"].values()
    shown = creative.manifest
    monkeypatch.setattr(creative, "manifest", lambda: [e for e in shown() if e["file"] != IMAGE])
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    queue(conn, v2)
    real = nb._remember

    def stopped_at_leftover(conn_, biz, values):
        if "leftover" in values:
            raise RuntimeError("the step stopped here")
        real(conn_, biz, values)

    monkeypatch.setattr(nb, "_remember", stopped_at_leftover)
    with pytest.raises(RuntimeError):
        step(conn)
    resources = nb_row(conn)["resources"]
    assert v1_first in {*(resources.get("ads") or {}).values(), *(resources.get("leftover") or [])}


def test_a_gone_versions_ad_set_found_by_name_gets_the_approved_versions_whole_money(conn, nb_live, monkeypatch):
    """Item 2's money: v1's attempt made the ad set but its answer was lost, so v2's launch finds it by name -- with
    v1's budget, the same daily amount as v2's. v1 no longer builds: v2 owns the attempt, and the enable sets v2's
    whole money on the ad set v1 made (the bid and the area are v1's until then), never trusting a budget that only
    happens to match."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.lose = {"ad-set/create"}
    assert step(conn)["last_error"] == nb.RETRYING
    nb_live.lose = set()
    shown = creative.manifest
    monkeypatch.setattr(creative, "manifest", lambda: [e for e in shown() if e["file"] != IMAGE])
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    queue(conn, v2)
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    assert "ad-set/getList" in nb_live.kinds() and len(nb_live.ad_sets) == 1  # v1's, found by name
    (money,) = nb_live.bodies("ad-set/update")
    assert set(money) == {"budget", "bidRate", "targeting"}


def test_the_failed_attempts_ads_are_never_taken_for_the_new_versions(conn, nb_live):
    """The failed launch made v1's first ad before NewsBreak refused its second; v2 keeps that first pair of lines (the
    same ad, by name), yet makes its own: the failed attempt's ad is on its way out (`leftover`), and an ad is never
    both running and on its way out."""
    v1 = seed(conn)
    queue(conn, v1)
    answer_ad_creates(
        nb_live,
        lambda body: body["creative"]["headline"] == "Clear Aligners Near You",
        envelope(41926, "the ad is not allowed"),
    )
    assert step(conn)["state"] == "failed"
    (v1_first,) = nb_live.ads
    assert nb_row(conn)["resources"]["leftover"] == [v1_first]
    v2 = add_plan(conn, nb_sections(headlines=["Invisalign in Palo Alto", "Same-Week Visits"]))
    queue(conn, v2)
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["plan_id"]) == ("live", v2) and len(nb_live.ads) == 3
    assert v1_first not in resources["ads"].values() and resources["leftover"] == [v1_first]
    assert nb_live.ads[v1_first]["status"] == "OFF" and unlisted_on(conn, nb_live) == set()


def test_an_ad_set_found_with_no_version_mark_gets_the_plans_whole_money(conn, nb_live):
    """An ad set of the business's found by name that no attempt of Admiry's marked (made by hand, say) may hold any
    money: even with the plan's budget on it, the enable sets the plan's whole money -- the bid and the ZIP codes too."""
    account_id = nb_live.add_account(ACCOUNT_NAME)
    campaign = nb_live.add_campaign(account_id, ACCOUNT_NAME, status="OFF")
    everyone = {dimension: {"positive": ["all"]} for dimension in nb_build.TARGETING_DIMENSIONS}
    nb_live.ad_sets["777"] = {
        "id": "777",
        "name": ACCOUNT_NAME,
        "campaignId": campaign,
        "adAccountId": account_id,
        "budget": 1973,  # the plan's own daily budget
        "bidType": "CPC",
        "bidRate": 40,
        "status": "OFF",
        "targeting": everyone,
    }
    v1 = seed(conn)
    queue(conn, v1)
    assert step(conn)["state"] == "live"
    (money,) = nb_live.bodies("ad-set/update")
    ids = nb_row(conn)["resources"]["location_ids"]
    assert money == {"budget": 1973, "bidRate": 150, "targeting": nb_build.targeting(ids)}
    assert nb_live.ad_sets["777"]["targeting"]["location"] == {"positive": ids}


# ---------------------------------------------------------------- the approval's time budget, NewsBreak not answering


@pytest.fixture
def clock(monkeypatch) -> StepClock:
    """The monotonic clock of the approval's own launch (`ads_run.launch_now`'s budget) and of the adapter's pacing and
    budget, standing still but for what a test moves."""
    stepped = StepClock()
    monkeypatch.setattr(ads_run, "time", stepped)
    monkeypatch.setattr(nb, "time", stepped)
    return stepped


@pytest.mark.parametrize(
    ("latency", "made", "where", "head"),
    [
        (4.0, LAUNCH[:5], "queued", "NewsBreak: your ads are waiting to launch"),
        (1.5, [*LAUNCH, *ENABLE[:3]], "launching", "NewsBreak: your ads are built and turn on within the hour"),
    ],
    ids=["in the launch", "in the enable"],
)
def test_a_launch_the_approvals_time_budget_cuts_short_says_admiry_finishes_it(
    client, conn, nb_live, clock, latency, made, where, head
):
    """Fix round 1, Important 1: the approval launches within its own 20 s (`launch_now`); a call that cannot fit in
    what is left is never sent, and the row says Admiry finishes within the hour -- in the reply, the card and
    `last_error` -- never that NewsBreak is down. The hourly step then finishes from the missing step."""
    nb_live.on_request.append(lambda request: clock.sleep(latency))  # NewsBreak's answers take `latency` seconds
    key = fixtures.business_with_key(conn, biz_id=BIZ, claim_state="verified")
    plan_id = seed(conn, status="draft")
    envelope_ = approve(client, conn, key, plan_id)
    row = nb_row(conn)
    assert (row["state"], row["last_error"], nb_live.kinds()) == (where, nb.STARTING, made)
    said = ads_run.status_sentence(row)
    assert said == f"{head}. {nb.STARTING}." and said in envelope_["say"]
    (card,) = state.channel_rows(conn, config.load(), BIZ)
    assert card.sentence == said
    for text in (envelope_["say"], card.sentence, card.state_text, ads_run.execution_line([row])):
        assert "try again" not in text and "unreachable" not in text.lower(), text
    nb_live.on_request.clear()
    assert step(conn)["state"] == "live"  # the hourly step, with no budget of its own


def test_a_request_needs_its_pause_and_a_second_of_the_budget_left(conn, nb_live, clock, monkeypatch):
    """The budget check counts the pause before a request: with answers of half a second and half a second of pause,
    10.7 s carry the launch's ten requests and not the enable's first (0.7 s would be left after its pause)."""
    monkeypatch.setattr(nb, "wait", lambda seconds: (nb_live.waits.append(seconds), clock.sleep(seconds)))
    nb_live.on_request.append(lambda request: clock.sleep(0.5))
    v1 = seed(conn)
    queue(conn, v1)
    with ADAPTER.client(budget_s=10.7) as http:
        out = ads_run.launch(conn, config.load(), BIZ, NB, http=http)
    assert (out["state"], out["last_error"], nb_live.kinds()) == ("launching", nb.STARTING, LAUNCH)


def test_an_owed_pause_goes_out_with_whatever_time_is_left(conn, nb_live, clock, launched):
    """Fix round 2, item 1 (the re-review's probe R3): stopping spend is always allowed -- a pause needs no second of
    the budget, only what NewsBreak's own limit asks between two requests; with 0.9 s left the owed pause is sent."""
    nb_live.down = True
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    assert nb_row(conn)["resources"]["pause_owed"] is True
    nb_live.down = False
    nb_live.sent.clear()
    with ADAPTER.client(budget_s=0.9) as http:
        out = ads_run.launch(conn, config.load(), BIZ, NB, http=http)
    assert (out["state"], nb_live.kinds()) == ("paused", ["campaign/updateStatus"])
    assert "pause_owed" not in nb_row(conn)["resources"]
    assert all(campaign["status"] == "OFF" for campaign in nb_live.campaigns.values())
    assert all(waited <= nb.NEWSBREAK_PACE_S for waited in nb_live.waits[-1:])  # NewsBreak's own pace, not a second


def test_a_pause_with_the_time_budget_wholly_spent_stays_owed_and_the_next_hourly_step_sends_it(
    conn, nb_live, clock, launched
):
    """R27 (b): a stop is exempt from the budget's floor, not from the budget -- one that finds nothing left is never
    sent; the pause is recorded all the same, owed to NewsBreak, and the hourly ads-launch (no budget of its own) sends
    it."""
    nb_live.requests.clear()
    with ADAPTER.client(budget_s=0) as http:
        out = ads_run.pause(conn, config.load(), BIZ, NB, http=http, by="owner")
    row = nb_row(conn)
    assert (out["state"], row["resources"]["pause_owed"], row["last_error"]) == (
        "paused",
        True,
        ADAPTER.words.pause_owed,
    )
    assert nb_live.requests == [] and all(c["status"] == "ON" for c in nb_live.campaigns.values())
    out = step(conn)
    assert (out["state"], nb_live.kinds()) == ("paused", ["campaign/updateStatus"])
    assert nb_live.bodies("campaign/updateStatus") == [{"status": "OFF"}]
    assert "pause_owed" not in nb_row(conn)["resources"] and nb_row(conn)["last_error"] is None
    assert all(c["status"] == "OFF" for c in nb_live.campaigns.values())


def test_a_version_that_drops_newsbreak_stops_its_ads_with_whatever_time_is_left(conn, nb_live, clock, launched):
    """Probe R3b: the approval of a version without NewsBreak pauses its ads (`dropped`) in the same request, even with
    under a second of its budget left -- never left ON until the hourly job."""
    add_plan(conn, sections())  # Google Search alone
    nb_live.sent.clear()
    with ADAPTER.client(budget_s=0.9) as http:
        out = ads_run.launch(conn, config.load(), BIZ, NB, http=http)
    row = nb_row(conn)
    assert (out["state"], row["paused_reason"], nb_live.kinds()) == ("paused", "dropped", ["campaign/updateStatus"])
    assert "pause_owed" not in row["resources"]
    assert all(campaign["status"] == "OFF" for campaign in nb_live.campaigns.values())


def test_a_refused_change_turns_its_ads_off_even_with_the_budget_spent(conn, nb_live, clock, monkeypatch):
    """Probe R4: NewsBreak refuses the cap after v2's new ads went ON, with less than a second of the budget left for
    the OFFs: they are sent all the same (stopping spend), so v2's ads are OFF -- not ON for up to an hour."""
    monkeypatch.setattr(nb, "wait", lambda seconds: (nb_live.waits.append(seconds), clock.sleep(seconds)))
    v1 = seed(conn)
    queue(conn, v1)
    assert step(conn)["state"] == "live"
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    (account_id,) = nb_live.accounts
    refusal = {"adAccountId": account_id, "message": "Update failed. Budget must be between 0 cents and 10000000000"}
    nb_live.fail["balance/updateAccountsBudget"] = ok({"list": [refusal]})
    nb._LAST_CALL.clear()
    nb_live.sent.clear()
    with ADAPTER.client(budget_s=7.0) as http:  # the update's seven requests a second apart, then 0.9 s, then 0.8 s
        out = ads_run.launch(conn, config.load(), BIZ, NB, http=http)
    resources = nb_row(conn)["resources"]
    assert (out["last_error"], resources["refused_plan_id"]) == (newsbreak.REFUSED, v2)
    assert nb_live.kinds()[-2:] == ["ad/updateStatus", "ad/updateStatus"]
    assert nb_live.bodies("ad/updateStatus")[-2:] == [{"status": "OFF"}, {"status": "OFF"}]
    v2_ads = [ad for ad in nb_live.ads if ad not in resources["ads"].values()]
    assert len(v2_ads) == 2 and all(nb_live.ads[ad]["status"] == "OFF" for ad in v2_ads)
    assert set(resources["leftover"]) == set(v2_ads) and unlisted_on(conn, nb_live) == set()


def test_turning_ads_on_keeps_the_budget_floor_and_a_second_between_requests(conn, nb_live, launched, clock):
    """Only stopping spend is exempt (item 1): the campaign turned back ON (`set_paused(False)`) waits a second from
    the business's last request -- a stop's too, which marks the pace like any request -- and needs a second of the
    budget after that wait, like every request that does not stop spend."""
    account, _plan, _build = built(conn)
    resources = nb_row(conn)["resources"]
    settings = config.load()
    nb._LAST_CALL.clear()
    nb_live.waits.clear()
    nb_live.sent.clear()
    ADAPTER.set_paused(conn, settings, account, resources, True)  # a stop: nothing to wait for, its start marked
    with ADAPTER.client(budget_s=1.5) as http, pytest.raises(base.PlatformError) as cut:
        ADAPTER.set_paused(conn, settings, account, resources, False, http=http)
    assert (str(cut.value), cut.value.retryable) == (nb.STARTING, True)
    assert nb_live.kinds() == ["campaign/updateStatus"]  # the ON not sent: a second's wait leaves half a second
    ADAPTER.set_paused(conn, settings, account, resources, False)  # no budget: sent a second after the stop
    assert nb_live.waits == [nb.PACE_S] and nb_live.bodies("campaign/updateStatus") == [
        {"status": "OFF"},
        {"status": "ON"},
    ]


def test_newsbreak_not_answering_is_admirys_to_retry(conn, nb_live):
    """A real outage: the row says Admiry retries within the hour (RETRYING, the adapter's `words.retrying`) -- never
    the client's "try again"."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.down = True
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("queued", nb.RETRYING)
    assert out["say"] == f"NewsBreak: your ads are waiting to launch. {nb.RETRYING}." and "try again" not in out["say"]
    assert ADAPTER.words.retrying == nb.RETRYING


def test_a_version_approved_between_two_attempts_finishes_with_the_version_that_made_the_ad_set(conn, nb_live):
    """The ad set carries a version's money and area: once one attempt made it, a later attempt finishes the ads from
    that same version and says so, and the enable's update brings the newer version on -- never a mix."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.lose = {"ad/create"}  # v1's ad set made, its first ad made with the answer lost
    assert step(conn)["state"] == "queued"
    v2 = add_plan(conn, nb_sections(monthly_usd=900, headlines=NEW_HEADLINES))
    queue(conn, v2)
    out = step(conn)
    assert (out["state"], out["plan_id"]) == ("live", v2)
    (ad_set,) = nb_live.ad_sets.values()
    assert ad_set["budget"] == nb_build.daily_cents(900)  # the update applied v2's money before the enable
    assert len(nb_live.ads) == 4  # v1's two finished once each (the lost one found by name), then v2's two
    resources = nb_row(conn)["resources"]
    v2_ads = [nb_live.ads[i] for i in resources["ads"].values()]
    v1_ads = [a for a in nb_live.ads.values() if a["id"] not in resources["ads"].values()]
    assert {a["creative"]["content"]["headline"] for a in v2_ads} == set(NEW_HEADLINES)
    assert {a["creative"]["content"]["headline"] for a in v1_ads} <= set(HEADLINES)
    assert all(a["status"] == "OFF" for a in v1_ads) and all(a["status"] == "ON" for a in v2_ads)
    assert sorted(resources["replaced"]) == sorted(a["id"] for a in v1_ads)


# ---------------------------------------------------------------- R5: the spending cap is a lifetime cap


def _month_spend(conn, usd: float) -> None:
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'api')",
        (BIZ, NB, NOW.date().replace(day=1), int(usd * 1_000_000)),
    )


def test_the_cap_is_the_lifetime_total_at_launch_plus_the_months_amount_and_a_mid_month_update_keeps_its_base(
    conn, nb_live
):
    """R5 as amended (fix round 1): the launch records NewsBreak's lifetime total as the base (`cap_base_cents`) and
    caps the account at base + the month's amount; a mid-month update re-sets the cap from that same base -- never
    from `metric_daily`, which lags NewsBreak's live total, so the cap can never let more than the month through."""
    account_id = nb_live.add_account(ACCOUNT_NAME)  # an account with a past: capped and spent before
    nb_live.caps[account_id], nb_live.spent[account_id] = 200_000, 123_456
    v1 = seed(conn)
    queue(conn, v1)
    assert step(conn)["state"] == "live"
    resources = nb_row(conn)["resources"]
    assert (resources["cap_base_cents"], resources["cap_cents"]) == (123_456, 123_456 + 60_000)
    assert nb_live.caps[account_id] == 183_456 == external(conn)["spending_cap_cents"]
    nb_live.spent[account_id] += 20_000  # spent since; ads-sync has not read it yet
    _month_spend(conn, 50.0)  # what metric_daily knows: less than NewsBreak's live figure
    v2 = add_plan(conn, nb_sections(monthly_usd=900))
    out = step(conn)
    assert (out["state"], out["plan_id"]) == ("live", v2)
    assert nb_live.caps[account_id] == 123_456 + 90_000  # the base, never the total plus a lagging month-to-date
    assert nb_row(conn)["resources"]["cap_cents"] == nb_live.caps[account_id] == external(conn)["spending_cap_cents"]


def test_a_month_lowered_under_what_it_spent_is_capped_a_cent_above_the_total(conn, nb_live, launched):
    """NewsBreak takes only a cap above the total spend: a plan lowered under what the month already spent gets the
    total plus one cent -- it stops the ads as near as NewsBreak allows."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 50_000  # under the $600 cap
    add_plan(conn, nb_sections(monthly_usd=300, headlines=NEW_HEADLINES))
    step(conn)
    assert nb_live.caps[account_id] == 50_001


def test_newsbreak_showing_more_spent_than_admirys_cap_sets_no_cap_and_tells_an_operator_once(
    conn, nb_live, launched, resend
):
    """The sanity check: NewsBreak's total above the last cap Admiry set by more than NewsBreak's own overshoot (fix
    round 2: `CAP_OVERSHOOT_CENTS`, 600 on a 60,000 cap) is a unit or a spend mismatch -- no cap is set from figures
    that disagree (fail closed), the row waits on an operator sentence, and one operator mail says what to check and
    how to clear it."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 61_000  # the launch capped it at 60,000
    add_plan(conn, nb_sections(monthly_usd=900))
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", nb.SPEND_CHECK) and _plain(nb.SPEND_CHECK)
    assert "balance/updateAccountsBudget" not in nb_live.kinds() and nb_live.caps[account_id] == 60_000
    (mail,) = resend.sent
    assert mail["to"] == ["ops@example.com"] and account_id in mail["text"] and "61000" in mail["text"]
    assert "Ads Manager" in mail["text"] and "nb-account-ready" in mail["text"]  # what to check, how to clear it
    assert "spending_cap_cents" in mail["text"] and BIZ in mail["text"]
    nb_live.sent.clear()
    step(conn)  # the next hour: still off, no second mail
    assert "balance/updateAccountsBudget" not in nb_live.kinds() and len(resend.sent) == 1


@pytest.mark.parametrize("spent", [60_001, 60_600], ids=["a cent over", "the tolerance's edge"])
def test_newsbreaks_own_small_overshoot_of_a_spent_month_is_no_mismatch(conn, nb_live, launched, resend, spent):
    """NewsBreak may deliver a little past its own cap at a fully spent month: within `CAP_OVERSHOOT_CENTS` (the larger
    of $5 and 1 % of the last cap) the step goes on and sets the next cap from the recorded base as usual -- never a
    channel stopped for good on NewsBreak's own overshoot."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = spent
    assert nb.cap_overshoot_cents(60_000) == 600 and nb.cap_overshoot_cents(20_000) == nb.CAP_OVERSHOOT_CENTS == 500
    add_plan(conn, nb_sections(monthly_usd=900))
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", None) and resend.sent == []
    assert nb_live.caps[account_id] == 90_000  # the base (0) + the month, above the total


def test_the_first_cent_past_newsbreaks_own_overshoot_is_a_mismatch(conn, nb_live, launched, resend):
    """R28, the stop side of the tolerance's edge: a total one cent above the last cap plus its overshoot (60,000 +
    600 + 1) sets no cap, waits on SPEND_CHECK and tells an operator."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 60_000 + nb.cap_overshoot_cents(60_000) + 1
    add_plan(conn, nb_sections(monthly_usd=900))
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", nb.SPEND_CHECK)
    assert "balance/updateAccountsBudget" not in nb_live.kinds() and nb_live.caps[account_id] == 60_000
    assert len(resend.sent) == 1


def test_a_token_that_may_not_set_the_cap_leaves_the_launch_waiting_for_an_operator(conn, nb_live):
    """R5: Org Admin only; the refusal is the token's (ACCESS_TROUBLE), retryable once an operator swaps it -- on the
    row in the owner's words (fix round 2)."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.org_admin = False
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("queued", nb.ACCESS_FIXING)
    assert (
        out["say"] == f"NewsBreak: your ads are waiting to launch. {nb.ACCESS_FIXING}."
        and "try again" not in out["say"]
    )
    assert all(o["status"] == "OFF" for o in (*nb_live.campaigns.values(), *nb_live.ad_sets.values()))
    nb_live.org_admin = True
    nb_live.sent.clear()
    assert step(conn)["state"] == "live"
    assert nb_live.kinds() == [
        "event/getList",
        "balance/getAccountBudgetInfo",
        "balance/updateAccountsBudget",
        "ad-set/update",  # the enable sets the budget of an ad set an earlier run made (its budget is not on the row)
        *ENABLE,
    ]  # everything else was made on the first run, and is on the row


# ---------------------------------------------------------------- R10: the lead event is an operator's to make


def test_a_missing_lead_event_never_blocks_the_ads_and_the_operator_is_told_once(conn, nb_live, resend):
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.lose = {"campaign/create"}  # the launch is cut short after the account, its event looked up
    assert step(conn)["state"] == "queued"
    (account_id,) = nb_live.accounts
    (mail,) = resend.sent
    assert mail["to"] == ["ops@example.com"] and "submit_form" in mail["text"] and account_id in mail["text"]
    assert "Bright Smile Dental" in mail["subject"]
    assert external(conn)["event_mail_sent_at"]
    nb_live.sent.clear()
    assert step(conn)["state"] == "live"  # the ads go on without it
    assert "event/getList" not in nb_live.kinds() and len(resend.sent) == 1  # once per account, never per attempt
    add_plan(conn, nb_sections(monthly_usd=900))
    assert step(conn)["state"] == "live" and len(resend.sent) == 1


def test_the_lead_event_made_by_hand_is_found_and_kept(conn, nb_live, resend):
    account_id = nb_live.add_account(ACCOUNT_NAME)  # made earlier (an answer lost), its event made by hand since
    nb_live.events[account_id] = [
        {"id": "77", "name": "Admiry lead", "type": "POSTBACK", "eventType": newsbreak.ADMIRY_LEAD_EVENT}
    ]
    v1 = seed(conn)
    queue(conn, v1)
    assert step(conn)["state"] == "live"
    assert "ad-account/create" not in nb_live.kinds()
    assert external(conn)["event_id"] == "77" and resend.sent == []


# ---------------------------------------------------------------- R11: the account's own trouble is operator work


def test_a_new_account_newsbreak_does_not_make_active_waits_for_newsbreak_to_get_it_ready(conn, nb_live, resend):
    """R11 (M1 Task 7 fix round 1, item 2): the account's wait at NewsBreak is one NewsBreak sentence that never names
    funds -- the owner's word for their balance, and two of its three causes (the allowance, verification) are no
    money at all -- said once on the row's line, the card and the plan's badge."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.account_status = "PENDING"
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("queued", nb.ACCOUNT_WAIT)
    assert nb.ACCOUNT_WAIT == "NewsBreak is getting your account ready; your ads start once it is"
    assert _plain(nb.ACCOUNT_WAIT) and "fund" not in nb.ACCOUNT_WAIT.lower()
    waiting = nb_row(conn)
    assert _line(waiting) == (f"NewsBreak: {nb.ACCOUNT_WAIT}.", nb.ACCOUNT_WAIT)
    assert out["say"] == f"NewsBreak: {nb.ACCOUNT_WAIT}." and ads_run.execution_line([waiting]) == nb.ACCOUNT_WAIT
    assert nb_live.kinds() == ["ad-account/getGroupsByOrgIds", "ad-account/create"] and not nb_live.campaigns
    (account_id,) = nb_live.accounts
    stored = external(conn)
    assert (stored["ad_account_id"], stored["account_status"]) == (account_id, "PENDING")
    assert registry.account(conn, BIZ, NB).status == "needs_attention"
    (mail,) = resend.sent
    assert "PENDING" in mail["text"] and account_id in mail["text"] and mail["to"] == ["ops@example.com"]
    status = ads_run.account_status(conn, config.load(), BIZ, NB)
    assert (status.ready, status.reason, status.account_public) == (False, nb.ACCOUNT_WAIT, account_id)
    # the hourly step finds it still waiting at no cost to NewsBreak
    nb_live.sent.clear()
    assert step(conn)["last_error"] == nb.ACCOUNT_WAIT and nb_live.kinds() == []


def test_an_hour_later_the_account_is_asked_about_again_and_launches_once_newsbreak_shows_it_active(
    conn, nb_live, resend, monkeypatch
):
    """The mark lasts less than the hourly job's period (fix round 1: 50 minutes), so the next hour's step looks again
    -- an hour's mark would leave it to every other hour, while the operator mail says every hour."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.account_status = "PENDING"
    step(conn)
    (account_id,) = nb_live.accounts
    assert nb.RECHECK == timedelta(minutes=50)
    monkeypatch.setattr(ads_run, "now", lambda: NOW + timedelta(minutes=49))
    assert not ads_run.account_status(conn, config.load(), BIZ, NB).ready
    later = NOW + timedelta(minutes=51)
    monkeypatch.setattr(ads_run, "now", lambda: later)
    assert ads_run.account_status(conn, config.load(), BIZ, NB).ready  # due for another look
    nb_live.sent.clear()
    assert step(conn)["last_error"] == nb.ACCOUNT_WAIT  # NewsBreak's list shows no status: still waiting
    assert nb_live.kinds() == ["ad-account/getGroupsByOrgIds"] and len(resend.sent) == 1  # no second mail
    listed = {"id": account_id, "name": ACCOUNT_NAME, "createTime": "1", "status": "ACTIVE"}
    nb_live.fail["ad-account/getGroupsByOrgIds"] = ok({"list": [{"id": "org_1", "adAccounts": [listed]}]})
    monkeypatch.setattr(ads_run, "now", lambda: later + timedelta(hours=1, minutes=1))
    assert step(conn)["state"] == "live"
    assert external(conn)["account_status"] == "ACTIVE" and registry.account(conn, BIZ, NB).status == "active"


def test_a_refused_account_create_waits_for_an_operator_and_is_tried_again_an_hour_later(
    conn, nb_live, resend, monkeypatch
):
    """NewsBreak caps the ad accounts of an Organization (raised by an e-mail to NewsBreak): a create it refuses is
    never the owner's problem -- the row waits with the same sentence, the operator gets NewsBreak's own words once,
    and the create is tried again an hour later."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.fail["ad-account/create"] = envelope(41003, "ad account limit reached for this organization")
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("queued", nb.ACCOUNT_WAIT) and not nb_live.accounts
    (mail,) = resend.sent
    assert "ad account limit reached for this organization" in mail["text"]
    for stored in (nb_row(conn), external(conn)):  # NewsBreak's words reach the operator alone
        assert "ad account limit" not in json.dumps(stored, default=str)
    assert not ads_run.account_status(conn, config.load(), BIZ, NB).ready
    monkeypatch.setattr(ads_run, "now", lambda: NOW + timedelta(hours=1, minutes=1))
    assert step(conn)["last_error"] == nb.ACCOUNT_WAIT and len(resend.sent) == 1  # refused again: no second mail
    del nb_live.fail["ad-account/create"]
    monkeypatch.setattr(ads_run, "now", lambda: NOW + timedelta(hours=2, minutes=2))
    assert step(conn)["state"] == "live" and len(nb_live.accounts) == 1


def test_without_an_operator_address_the_notice_is_skipped_with_a_log_line(conn, nb_live, caplog):
    v1 = seed(conn)
    queue(conn, v1)
    with caplog.at_level(logging.WARNING):
        assert step(conn)["state"] == "live"
    assert "AGENT_OPS_EMAIL unset" in caplog.text
    assert "event_mail_sent_at" not in external(conn)  # not sent, so not marked: the next step tries again


# ---------------------------------------------------------------- the update (R4, R5)


def test_update_after_a_budget_change_sends_one_ad_set_update_with_the_whole_targeting_and_resets_the_cap(
    conn, nb_live, launched
):
    v2 = add_plan(conn, nb_sections(monthly_usd=900))
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    assert nb_live.kinds() == ["ad-set/update", "balance/getAccountBudgetInfo", "balance/updateAccountsBudget"]
    ((body),) = nb_live.bodies("ad-set/update")
    ids = nb_row(conn)["resources"]["location_ids"]
    assert body["budget"] == nb_build.daily_cents(900) and body["targeting"] == nb_build.targeting(ids)
    (ad_set,) = nb_live.ad_sets.values()
    assert ad_set["targeting"]["location"] == {"positive": ids}  # never widened to everyone
    assert nb_live.caps[nb_row(conn)["resources"]["ad_account_id"]] == 90_000


def test_changed_words_make_new_ads_off_then_on_and_leave_the_old_ones_on(conn, nb_live, launched):
    """Corrections/Task 3 ruling: the old ads stay ON until the new ones pass NewsBreak's review (5b turns them off
    then) -- never a moment with nothing running. Every new ad is made OFF first, and turned ON only once the last one
    is made (fix round 1); the version's whole money and area go first, as on every update (S16, final review I1)."""
    old = dict(nb_row(conn)["resources"]["ads"])
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    out = step(conn)
    assert (out["state"], out["plan_id"]) == ("live", v2)
    assert nb_live.kinds() == [
        "ad-set/update",
        "ad/create",
        "ad/create",
        "ad/updateStatus",
        "ad/updateStatus",
        "balance/getAccountBudgetInfo",
        "balance/updateAccountsBudget",
    ]
    assert all(body["status"] == "OFF" for body in nb_live.bodies("ad/create"))
    assert nb_live.bodies("ad/updateStatus") == [{"status": "ON"}, {"status": "ON"}]
    resources = nb_row(conn)["resources"]
    assert set(resources["ads"].values()).isdisjoint(old.values())
    assert sorted(resources["replaced"]) == sorted(old.values())
    assert all(nb_live.ads[ad]["status"] == "ON" for ad in [*old.values(), *resources["ads"].values()])


def test_a_lost_answer_in_an_update_never_makes_an_ad_twice(conn, nb_live, launched):
    """v2 keeps v1's second pair of lines, so its second new ad has the name of v1's second running ad: the retry takes
    only what its own earlier attempt made, never an ad running now (which is on its way out)."""
    old = set(nb_row(conn)["resources"]["ads"].values())
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    nb_live.lose = {"ad/create"}
    out = step(conn)
    assert out["last_error"] == nb.RETRYING
    nb_live.sent.clear()
    assert step(conn)["last_error"] is None
    assert nb_live.kinds()[:5] == [
        "ad-set/update",  # the version's whole money first, on every attempt (S16)
        "ad/getList",
        "ad/create",
        "ad/updateStatus",
        "ad/updateStatus",
    ]  # the lost one found
    assert len(nb_live.ads) == 4
    resources = nb_row(conn)["resources"]
    assert set(resources["ads"].values()).isdisjoint(old) and set(resources["replaced"]) == old
    assert "updating" not in resources


def test_a_refused_second_create_leaves_no_ad_running_that_the_row_does_not_list(conn, nb_live, launched):
    """Fix round 1, Important 2 (the reviewer's probe A): NewsBreak refuses v2's second new ad. v2's first one was made
    OFF and stays OFF (new ads go ON only once the last is made), listed in `leftover`; v1's keep running; and v3,
    approved after, leaves no ad running that the row does not list either."""
    old = set(nb_row(conn)["resources"]["ads"].values())
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    answer_ad_creates(nb_live, nth(2), envelope(41926, "the ad is not allowed"))
    out = step(conn)
    row = nb_row(conn)
    assert (out["state"], out["last_error"]) == ("live", newsbreak.REFUSED_WITH_REASON.format(41926))
    assert row["resources"]["refused_plan_id"] == v2 and set(row["resources"]["ads"].values()) == old
    (made,) = set(nb_live.ads) - old
    assert nb_live.ads[made]["status"] == "OFF" and row["resources"]["leftover"] == [made]
    assert "updating" not in row["resources"] and unlisted_on(conn, nb_live) == set()
    v3 = add_plan(conn, nb_sections(headlines=["Gentle Care in Palo Alto", "Same-Day Crowns in Palo Alto"]))
    out = step(conn)
    assert (out["state"], out["plan_id"]) == ("live", v3) and unlisted_on(conn, nb_live) == set()


def test_an_update_attempt_for_a_page_that_moved_since_is_settled_and_made_again_at_the_new_address(
    conn, nb_live, launched
):
    """v2's first new ad was made at the page's old address, then NewsBreak was busy; the page moves before the retry.
    The attempt's ad (OFF) goes to `leftover`, and v2's ads are made again carrying the new address."""
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    answer_ad_creates(nb_live, nth(2), envelope(4034, "Rate limit exceeded"))
    assert step(conn)["last_error"] == nb.RETRYING
    (first,) = nb_row(conn)["resources"]["updating"]["ads"].values()
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    assert step(conn)["last_error"] is None
    resources = nb_row(conn)["resources"]
    assert first in resources["leftover"] and nb_live.ads[first]["status"] == "OFF"
    target = nb_build.page_url(new)
    assert all(nb_live.ads[ad]["creative"]["content"]["clickThroughUrl"] == target for ad in resources["ads"].values())
    assert resources["click_url"] == target and unlisted_on(conn, nb_live) == set()


def test_a_change_refused_after_its_ads_went_on_takes_them_off_and_leaves_the_running_ads_alone(
    conn, nb_live, launched
):
    """NewsBreak refuses the cap at the end of v2's update, after v2's ads were made and turned on: v2's ads go OFF and
    into `leftover`; v1's ads -- the ones the refused version would have replaced -- stay the running set, never
    `replaced` (the hourly read turns those off once the ads beside them pass review)."""
    old = set(nb_row(conn)["resources"]["ads"].values())
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    (account_id,) = nb_live.accounts
    refusal = {"adAccountId": account_id, "message": "Update failed. Budget must be between 0 cents and 10000000000"}
    nb_live.fail["balance/updateAccountsBudget"] = ok({"list": [refusal]})
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["last_error"], resources["refused_plan_id"]) == ("live", newsbreak.REFUSED, v2)
    v2_ads = set(nb_live.ads) - old
    assert len(v2_ads) == 2 and all(nb_live.ads[ad]["status"] == "OFF" for ad in v2_ads)
    assert set(resources["ads"].values()) == old and set(resources["leftover"]) == v2_ads
    assert "replaced" not in resources
    assert all(nb_live.ads[ad]["status"] == "ON" for ad in old) and unlisted_on(conn, nb_live) == set()


def test_a_newer_version_arriving_turns_the_earlier_attempts_ads_off_and_lists_them(conn, nb_live, launched):
    """Fix round 1, Important 2: v2's update made and turned on its ads, then stopped at the cap (a token an operator
    must swap); v3 arrives before v2 is retried. v2's ads, ON and on the row only as v2's attempt (`updating`), are
    turned OFF and listed in `leftover`; v3's run, v1's replaced by them; nothing runs that the row does not list."""
    old = set(nb_row(conn)["resources"]["ads"].values())
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    nb_live.org_admin = False
    out = step(conn)
    assert out["last_error"] == nb.ACCESS_FIXING_CHANGE  # running ads: the change waits for the access, not the ads
    attempt = nb_row(conn)["resources"]["updating"]
    v2_ads = set(attempt["ads"].values())
    assert len(v2_ads) == 2 and all(nb_live.ads[ad]["status"] == "ON" for ad in v2_ads)
    assert unlisted_on(conn, nb_live) == set()  # on the row as v2's attempt (`updating`) until a step settles them
    nb_live.org_admin = True
    v3 = add_plan(conn, nb_sections(headlines=["Gentle Care in Palo Alto", "Same-Day Crowns in Palo Alto"]))
    nb_live.sent.clear()
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v3, None)
    assert nb_live.bodies("ad/updateStatus")[:2] == [{"status": "OFF"}, {"status": "OFF"}]  # v2's, first
    assert all(nb_live.ads[ad]["status"] == "OFF" for ad in v2_ads) and v2_ads == set(resources["leftover"])
    assert old == set(resources["replaced"]) and "updating" not in resources
    assert unlisted_on(conn, nb_live) == set()


def test_new_words_newsbreak_cannot_take_are_refused_and_the_ads_keep_running(conn, nb_live, launched):
    v2 = add_plan(conn, nb_sections(headlines=["x" * 91]))
    out = step(conn)
    row = nb_row(conn)
    assert (out["state"], out["last_error"]) == ("live", nb.TOO_LITTLE_COPY)
    assert (row["plan_id"], row["resources"]["refused_plan_id"]) == (launched, v2)  # A3 S15: marked, not retried


def test_every_update_sends_the_new_versions_whole_money_first_so_a_refused_ones_never_sticks(conn, nb_live, launched):
    """Final review I1 (S16, as Google's adapter does): every update sends the new version's whole money and area
    first -- its daily budget, its bid and the whole targeting -- whatever `diff` says moved. v2 doubles the money and
    changes the words, and NewsBreak refuses its first new ad: NewsBreak keeps v2's money, the last the owner approved.
    v3 goes back to v1's money and words, which `diff` against the row's version (v1) calls no change -- yet v3's
    money goes out first, so NewsBreak's budget is never the refused version's once a newer one runs."""
    (ad_set_id,) = nb_live.ad_sets
    v2 = add_plan(conn, nb_sections(monthly_usd=1200, headlines=NEW_HEADLINES))
    answer_ad_creates(nb_live, nth(1), envelope(41926, "the ad is not allowed"))
    out = step(conn)
    assert (out["state"], nb_row(conn)["resources"]["refused_plan_id"]) == ("live", v2)
    assert nb_live.ad_sets[ad_set_id]["budget"] == nb_build.daily_cents(1200)
    v3 = add_plan(conn, nb_sections(monthly_usd=600))
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v3, None)
    ids = nb_row(conn)["resources"]["location_ids"]
    money = {"budget": nb_build.daily_cents(600), "bidRate": 150, "targeting": nb_build.targeting(ids)}
    assert nb_live.kinds()[0] == "ad-set/update" and nb_live.bodies("ad-set/update") == [money]
    assert nb_live.ad_sets[ad_set_id]["budget"] == nb_build.daily_cents(600)


def test_a_refused_change_leaves_the_row_with_the_zip_codes_newsbreak_carries(conn, nb_live, launched):
    """I1: v2 widens the area to 25 miles and changes the words; NewsBreak takes its area (the money and area go
    first) and refuses its first new ad. The row says the ZIP codes NewsBreak carries -- v2's, never v1's -- and v3,
    back to v1's five miles, puts v1's area back at NewsBreak and on the row."""
    (ad_set_id,) = nb_live.ad_sets
    narrow = list(nb_row(conn)["resources"]["location_ids"])
    add_plan(conn, nb_sections(radius_miles=25, headlines=NEW_HEADLINES))
    answer_ad_creates(nb_live, nth(1), envelope(41926, "the ad is not allowed"))
    step(conn)
    wide = nb_live.ad_sets[ad_set_id]["targeting"]["location"]["positive"]
    assert len(wide) > len(narrow) and nb_row(conn)["resources"]["location_ids"] == wide
    add_plan(conn, nb_sections(radius_miles=5))
    step(conn)
    assert nb_live.ad_sets[ad_set_id]["targeting"]["location"]["positive"] == narrow
    assert nb_row(conn)["resources"]["location_ids"] == narrow


def test_a_change_refused_after_the_ads_moved_to_a_new_page_leaves_the_row_with_the_address_they_carry(
    conn, nb_live, launched
):
    """I1: the update carried the running ads to the page's new address, then NewsBreak refused the change (its cap):
    the row says the address the running ads carry now -- the page-down guards read it -- never the old one."""
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    (account_id,) = nb_live.accounts
    refusal = {"adAccountId": account_id, "message": "Update failed. Budget must be between 0 cents and 10000000000"}
    nb_live.fail["balance/updateAccountsBudget"] = ok({"list": [refusal]})
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["last_error"], resources["refused_plan_id"]) == (newsbreak.REFUSED, v2)
    target = nb_build.page_url(new)
    assert {nb_live.ads[ad]["creative"]["content"]["clickThroughUrl"] for ad in resources["ads"].values()} == {target}
    assert ADAPTER.carried_url(resources) == target


# ---------------------------------------------------------------- pause and resume


def test_set_paused_sends_off_once(conn, nb_live, launched):
    out = ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    assert (out["state"], out["ops"]) == ("paused", 1)
    assert nb_live.kinds() == ["campaign/updateStatus"] and nb_live.bodies("campaign/updateStatus") == [
        {"status": "OFF"}
    ]
    assert all(c["status"] == "OFF" for c in nb_live.campaigns.values())


def test_an_owed_pause_is_sent_once_newsbreak_answers_again(conn, nb_live, launched):
    nb_live.down = True
    out = ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    row = nb_row(conn)
    assert (out["state"], row["resources"]["pause_owed"]) == ("paused", True)
    assert row["last_error"] == ADAPTER.words.pause_owed
    nb_live.down = False
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], nb_row(conn)["resources"].get("pause_owed")) == ("paused", None)
    assert nb_live.bodies("campaign/updateStatus") == [{"status": "OFF"}]


def test_an_owed_pause_the_token_cannot_send_says_the_pause_waits_for_the_access(conn, nb_live, launched):
    """Item 5 on a pause: NewsBreak refuses the token (code 4033), so the pause stays owed, and the row says the change
    -- the pause -- goes out once Admiry's access is fixed: never "your ads start once it is fixed" over running ads,
    never the client's "try again"."""
    nb_live.fail["campaign/updateStatus"] = envelope(4033, "invalid access token")
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    assert nb_row(conn)["resources"]["pause_owed"] is True
    step(conn)  # the hourly job tries the owed pause again
    row = nb_row(conn)
    assert (row["state"], row["last_error"], row["resources"]["pause_owed"]) == (
        "paused",
        nb.ACCESS_FIXING_CHANGE,
        True,
    )
    del nb_live.fail["campaign/updateStatus"]
    step(conn)
    row = nb_row(conn)
    assert (row["last_error"], row["resources"].get("pause_owed")) == (None, None)


def test_a_resume_under_validate_calls_nothing(conn, nb_live, launched, monkeypatch):
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    nb_live.sent.clear()
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    assert (out["state"], out["ops"]) == ("paused", 0) and nb_live.kinds() == []  # D90: nothing to check with


def test_a_resume_turns_everything_back_on_after_the_budget_and_cap(conn, nb_live, launched):
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    nb_live.sent.clear()
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    assert out["state"] == "live"
    assert nb_live.kinds() == [
        "ad-set/update",
        "balance/getAccountBudgetInfo",
        "balance/updateAccountsBudget",
        *ENABLE,
    ]
    ((money),) = nb_live.bodies("ad-set/update")  # the plan's whole money (fix round 1): the budget and the bid
    targeting = nb_build.targeting(nb_row(conn)["resources"]["location_ids"])
    assert money == {"budget": 1973, "bidRate": 150, "targeting": targeting}


def test_a_pause_that_lands_while_the_enable_runs_wins(conn, nb_live, launched, monkeypatch):
    """A3 S17: the enable reads the row again right before it turns anything on -- here a pause that lands while the
    resume's enable sets the cap (a pause never waits for the step's lock)."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    real = newsbreak.set_spending_cap

    def cap_then_paused(settings, *args, **kwargs):
        answer = real(settings, *args, **kwargs)
        conn.execute("UPDATE channel_campaign SET state = 'paused' WHERE business_id = %s AND channel = %s", (BIZ, NB))
        return answer

    monkeypatch.setattr(newsbreak, "set_spending_cap", cap_then_paused)
    nb_live.sent.clear()
    ads_run.resume(conn, config.load(), BIZ, NB)
    assert nb_row(conn)["state"] == "paused"
    assert "balance/updateAccountsBudget" in nb_live.kinds()
    assert not [k for k in nb_live.kinds() if k.endswith("updateStatus")]  # nothing turned on


def test_one_ad_newsbreak_will_not_turn_on_leaves_the_others_running_on_a_live_row(conn, nb_live, launched):
    """Final review C1 (its probe): NewsBreak refuses to turn one ad back on (41926) while the campaign, the ad set and
    the other ad come on. A `failed` row would be read by nothing -- no sync, no metrics, no cap, no Pause on the card
    -- while those serve, so the row is live, and the refused ad leaves the running set for `leftover` (listed, never
    running unlisted), which the hourly read turns off: it was on before the pause."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    first, second = serving(conn)
    refuse_on(nb_live, "ad", {second})
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    row = nb_row(conn)
    assert (out["state"], row["state"], row["last_error"]) == ("live", "live", None)
    assert row["resources"]["ads"] == {"0": first} and row["resources"]["leftover"] == [second]
    (campaign,) = nb_live.campaigns.values()
    (ad_set,) = nb_live.ad_sets.values()
    assert (campaign["status"], ad_set["status"], nb_live.ads[first]["status"]) == ("ON", "ON", "ON")
    assert unlisted_on(conn, nb_live) == set()
    sync(conn)
    assert nb_live.ads[second]["status"] == "OFF" and "leftover" not in nb_row(conn)["resources"]


@pytest.mark.parametrize("kind", ["ad", "ad-set", "campaign"], ids=["every ad", "the ad set", "the campaign"])
def test_a_refused_enable_raises_its_refusal_and_switches_nothing_off_itself(conn, nb_live, launched, kind):
    """C1 as the M1 follow-up's fix round 2 (G1) has it: NewsBreak refuses the campaign's switch, the ad set's, or every
    ad's -- nothing of the set can run, so the enable raises the refusal, not retried. The stop is the lifecycle's
    (`ads_run._stop_then_fail`, owed while NewsBreak does not take it): the adapter switches nothing OFF itself."""
    account, plan, _build = built(conn)
    resources = nb_row(conn)["resources"]
    refuse_on(nb_live, kind)
    nb_live.sent.clear()
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.enable(conn, config.load(), account, plan, resources, http=http)
    assert (str(e.value), e.value.retryable) == (newsbreak.REFUSED_WITH_REASON.format(41926), False)
    assert {"status": "OFF"} not in nb_live.bodies("campaign/updateStatus")


@pytest.mark.parametrize("kind", ["ad", "ad-set", "campaign"], ids=["every ad", "the ad set", "the campaign"])
def test_an_enable_that_turns_no_ad_on_switches_the_campaign_off_once_before_the_row_fails(
    conn, nb_live, launched, kind
):
    """C1, through the lifecycle: NewsBreak refuses the campaign's switch, the ad set's, or every ad's -- nothing of the
    set can run. The lifecycle's stop switches the campaign OFF -- once (G1: never the adapter's OFF and then the
    lifecycle's) -- before the row fails, so a failed row, which nothing reads, never serves."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    refuse_on(nb_live, kind)
    nb_live.sent.clear()
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    refused = newsbreak.REFUSED_WITH_REASON.format(41926)
    assert (out["state"], nb_row(conn)["state"], out["last_error"]) == ("failed", "failed", refused)
    switches = nb_live.bodies("campaign/updateStatus")
    assert switched(nb_live)[0] == "OFF" and switches[-1] == {"status": "OFF"}
    assert switches.count({"status": "OFF"}) == 1


def test_a_launch_whose_ads_newsbreak_will_not_turn_on_fails_with_its_campaign_off(conn, nb_live):
    """C1 on a fresh launch: every ad's switch refused after the campaign and the ad set came on -- the campaign goes
    OFF, once, before the launch fails."""
    v1 = seed(conn)
    queue(conn, v1)
    refuse_on(nb_live, "ad")
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("failed", newsbreak.REFUSED_WITH_REASON.format(41926))
    assert nb_live.bodies("campaign/updateStatus") == [{"status": "ON"}, {"status": "OFF"}]


def test_a_refused_enables_stop_newsbreak_does_not_take_is_owed_and_the_next_step_sends_it_alone(
    conn, nb_live, launched
):
    """G1 (the re-review's probe A2): the resume's enable is refused (the ad set's switch) and the lifecycle's stop is
    answered busy -- the campaign may still serve, so the row stays `launching` with the stop owed (the refusal and the
    version it refused), and the next step sends that stop alone -- no money, no cap, no switch ON again -- and fails
    the row with the campaign OFF."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    refuse_on(nb_live, "ad-set")
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    back()
    refused = newsbreak.REFUSED_WITH_REASON.format(41926)
    assert (out["state"], out["last_error"], switched(nb_live)[0]) == ("launching", nb.RETRYING, "ON")
    assert nb_row(conn)["resources"].get("stop_owed") == {"sentence": refused, "plan_id": launched}
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["last_error"], nb_live.kinds()) == ("failed", refused, ["campaign/updateStatus"])
    assert switched(nb_live)[0] == "OFF"


def answer_switch(fake, kind: str, status: str, code: int):
    """NewsBreak answers code `code` to every switch of the `kind` objects ("campaign", "ad-set" or "ad") to `status`
    ("ON" or "OFF"); every other switch goes through. Returns what puts NewsBreak's own answer back."""
    name = "_" + kind.replace("-", "_") + "_updateStatus"
    real = getattr(fake, name)

    def answering(ident, payload):
        return envelope(code, "not switched") if payload.get("status") == status else real(ident, payload)

    setattr(fake, name, answering)
    return lambda: setattr(fake, name, real)


def switched(fake) -> tuple[str, str, int]:
    """(the campaign's switch, the ad set's, how many ads are ON) at NewsBreak."""
    (campaign,) = fake.campaigns.values()
    (ad_set,) = fake.ad_sets.values()
    return campaign["status"], ad_set["status"], sum(1 for ad in fake.ads.values() if ad["status"] == "ON")


def _cut_after_the_campaigns_on(conn, fake) -> None:
    """The re-review's first shape: the owner's pause, then a resume NewsBreak cut short (busy on the ad set's switch)
    after the campaign's ON went through -- the row stays `launching`, by design, for the next step to finish."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    back = answer_switch(fake, "ad-set", "ON", 4034)
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    back()
    assert (out["state"], out["last_error"]) == ("launching", nb.RETRYING)


def _stop_not_taken(conn, fake) -> None:
    """Its second: a resume whose ad set NewsBreak refuses to turn on, the stop answered busy -- the row stays
    `launching` with the campaign ON and, since fix round 2 (G1: the stop is the lifecycle's), the stop owed."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    refuse_on(fake, "ad-set")
    back = answer_switch(fake, "campaign", "OFF", 4034)
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    back()
    assert (out["state"], out["last_error"]) == ("launching", nb.RETRYING)


def test_a_version_refused_while_the_ads_may_serve_switches_the_campaign_off_before_the_row_fails(
    conn, nb_live, launched
):
    """M1 follow-up R1 (the re-review's probe): a `launching` row may carry ads that serve -- an enable cut short after
    the campaign's ON went through. A newer version NewsBreak cannot take (words over its widths) is refused inside
    the step's enable, before anything is sent; the row fails -- read by nothing then: no sync, no metrics, no cap, no
    Pause on the card -- only once one stop switched the campaign OFF."""
    _cut_after_the_campaigns_on(conn, nb_live)
    assert switched(nb_live) == ("ON", "ON", 2)  # the ads serve
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], nb_row(conn)["state"], out["last_error"]) == ("failed", "failed", nb.TOO_LITTLE_COPY)
    assert switched(nb_live)[0] == "OFF" and nb_live.kinds() == ["campaign/updateStatus"]
    assert nb_live.bodies("campaign/updateStatus") == [{"status": "OFF"}]  # one stop, sent before the row failed


def test_a_newer_version_refused_after_an_owed_stop_fails_the_row_once_both_stops_went_through(conn, nb_live, launched):
    """The re-review's second shape, as fix round 2 has it (G1, G2): a resume whose ad set NewsBreak refused left its
    stop owed (NewsBreak busy on the OFF), and a newer version NewsBreak cannot take is approved meanwhile. The owed
    stop goes first, alone -- the version it was for no longer the approved one, nothing fails yet -- and the next
    step's refusal of the newer version fails the row, its campaign OFF."""
    _stop_not_taken(conn, nb_live)
    assert switched(nb_live) == ("ON", "ON", 2)  # the ads serve
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["last_error"], nb_live.kinds()) == ("launching", None, ["campaign/updateStatus"])
    assert switched(nb_live)[0] == "OFF"
    out = step(conn)
    assert (out["state"], out["last_error"], switched(nb_live)[0]) == ("failed", nb.TOO_LITTLE_COPY, "OFF")


@pytest.mark.parametrize(
    ("code", "sentence"),
    [(4034, nb.RETRYING), (41926, newsbreak.REFUSED_WITH_REASON.format(41926))],
    ids=["busy", "refused"],
)
def test_a_stop_newsbreak_does_not_take_is_owed_and_sent_alone_until_one_goes_through(
    conn, nb_live, launched, code, sentence
):
    """R1: the stop can fail too -- NewsBreak busy, or refusing it: the campaign may still serve, so the row is not
    failed. Fix round 1 (F2): it stays `launching` with the stop owed (`stop_owed` keeps the refusal and the version it
    refused, fix round 2) and the stop's own sentence on the row -- a stop, due under every hold -- and the next step
    sends that stop alone and, that version still the approved one, fails the row with the refusal and the campaign
    OFF."""
    _cut_after_the_campaigns_on(conn, nb_live)
    v2 = add_plan(conn, nb_sections(headlines=["x" * 91]))
    back = answer_switch(nb_live, "campaign", "OFF", code)
    out = step(conn)
    back()
    owed = nb_row(conn)["resources"].get("stop_owed")
    assert (out["state"], out["last_error"], owed) == (
        "launching",
        sentence,
        {"sentence": nb.TOO_LITTLE_COPY, "plan_id": v2},
    )
    assert switched(nb_live) == ("ON", "ON", 2)
    due = conn.execute(f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}").fetchall()
    assert len(due) == 1 and ads_run.stops_spend(conn, config.load(), BIZ, NB)  # owed, and a stop
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["last_error"], switched(nb_live)[0]) == ("failed", nb.TOO_LITTLE_COPY, "OFF")
    assert nb_live.kinds() == ["campaign/updateStatus"] and "stop_owed" not in nb_row(conn)["resources"]


def test_while_a_stop_is_owed_the_owner_reads_the_refusal_and_that_admiry_is_switching_the_ads_off(
    conn, nb_live, launched
):
    """G3 (the re-review's probe E): a `launching` row whose stop is owed never promises the ads turn on -- the owner
    reads the refusal and that Admiry is switching the ads off, then the stop's own sentence."""
    _cut_after_the_campaigns_on(conn, nb_live)
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    out = step(conn)
    back()
    stopping = "Admiry is switching your ads on NewsBreak off"
    assert out["say"] == f"NewsBreak: {nb.TOO_LITTLE_COPY}; {stopping}. {nb.RETRYING}."
    assert "turn on" not in out["say"] and _plain(out["say"])
    assert ads_run.status_sentence(nb_row(conn)) == out["say"]


def test_while_a_stop_is_owed_the_plan_badge_and_the_channels_card_say_the_ads_are_being_switched_off(
    conn, nb_live, launched
):
    """M1 follow-up 2, W5 (the money follow-up's review): the owner line of a `launching` row whose stop is owed says
    the refusal and that Admiry is switching the ads off (G3) -- and so do the plan card's compact badge ("Switching
    off") and the channels card's sentence (the line's own clause), never "Launching". An ordinary `launching` row
    (an enable NewsBreak cut short) still reads "Launching" in both."""
    _cut_after_the_campaigns_on(conn, nb_live)
    settings = config.load()
    (card,) = state.channel_rows(conn, settings, BIZ)
    assert (ads_run.execution_line(ads_run.rows(conn, BIZ)), card.state_text) == ("Launching", "Launching")
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    step(conn)
    back()
    assert nb_row(conn)["state"] == "launching" and nb_row(conn)["resources"]["stop_owed"]
    (card,) = state.channel_rows(conn, settings, BIZ)
    assert ads_run.execution_line(ads_run.rows(conn, BIZ)) == "Switching off"
    assert card.state_text == (
        "Your NewsBreak ad needs at least one headline and one description that fit NewsBreak's limits; revise the ad "
        "text first; Admiry is switching your ads on NewsBreak off"
    )
    assert _plain(card.state_text) and "Launching" not in card.sentence


@pytest.mark.parametrize("how", ["at once", "owed"])
def test_a_pause_that_goes_through_ends_an_owed_stop_and_a_resume_tries_the_version_again(
    conn, nb_live, launched, monkeypatch, how
):
    """F2: a pause NewsBreak takes on a row whose stop is owed is that stop -- the row is paused, not failed, and the
    owed stop goes, at once or once the hourly step sends a pause that was owed itself. A resume then tries the approved
    version again: here its picture is back in Admiry's set, and the ads go live on it."""
    _cut_after_the_campaigns_on(conn, nb_live)
    shown = creative.manifest
    _out_of_the_set(monkeypatch, "dentists/2.jpg")
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    out = step(conn)
    owed = nb_row(conn)["resources"].get("stop_owed")
    assert (out["state"], owed) == ("launching", {"sentence": nb.NO_IMAGE, "plan_id": v2})
    if how == "at once":
        back()
        assert ads_run.pause(conn, config.load(), BIZ, NB, by="owner")["state"] == "paused"
    else:
        ads_run.pause(conn, config.load(), BIZ, NB, by="owner")  # NewsBreak busy: the pause is owed as well
        assert nb_row(conn)["resources"]["pause_owed"] is True
        back()
        step(conn)  # the hourly step sends the owed pause
    row = nb_row(conn)
    assert (row["state"], switched(nb_live)[0]) == ("paused", "OFF")
    assert "stop_owed" not in row["resources"] and "pause_owed" not in row["resources"]
    monkeypatch.setattr(creative, "manifest", shown)  # the picture is back in the set
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)


def test_an_approval_while_a_stop_is_owed_keeps_it_and_launches_once_the_stop_went_through(conn, nb_live, launched):
    """G2: a newer approval never drops an owed stop -- the ads may still serve on the refused version's money until it
    goes through. The next step sends that stop alone; the refused version no longer the approved one, nothing fails:
    the row stays `launching` with its campaign OFF, and the step after it launches the newer version the ordinary way
    (its money first, S16)."""
    _cut_after_the_campaigns_on(conn, nb_live)
    v2 = add_plan(conn, nb_sections(headlines=["x" * 91]))
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    step(conn)
    back()
    v3 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    queue(conn, v3)  # the approval (`proposals.apply_approve_plan` -> `ads_run.queue`)
    assert nb_row(conn)["resources"].get("stop_owed") == {"sentence": nb.TOO_LITTLE_COPY, "plan_id": v2}
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["last_error"], nb_live.kinds()) == ("launching", None, ["campaign/updateStatus"])
    assert switched(nb_live)[0] == "OFF" and "stop_owed" not in nb_row(conn)["resources"]
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"], switched(nb_live)[0]) == ("live", v3, None, "ON")


def _approve_while_the_off_is_out(conn, fake, headlines: list[str]):
    """A newer version approved (`ads_run.queue`, as the approval queues it) while the campaign's OFF is out at
    NewsBreak, which then takes the OFF. Returns ({"v": that version's id} once it is made, what puts NewsBreak's own
    answer back)."""
    real = fake._campaign_updateStatus
    made: dict = {}

    def approving(ident, payload):
        if payload.get("status") == "OFF" and not made:
            made["v"] = add_plan(conn, nb_sections(headlines=headlines))
            queue(conn, made["v"])
        return real(ident, payload)

    fake._campaign_updateStatus = approving
    return made, lambda: setattr(fake, "_campaign_updateStatus", real)


def test_an_approval_while_the_refusals_own_stop_is_out_leaves_the_row_to_launch_the_newer_version(
    conn, nb_live, launched
):
    """M1 follow-up 2, W6 (the re-review's probe P2, a race older than the owed stop): a newer version is approved while
    the refused step's own stop is out at NewsBreak, and the stop goes through. The version the refusal was for is no
    longer the approved one, so the row does not fail -- a failed row is due for nothing, and the newer version would
    wait for yet another approval -- it stays `launching` with its campaign OFF and nothing owed, as an owed stop's own
    step leaves it (G2), due for the next step, which launches the newer version the ordinary way."""
    _cut_after_the_campaigns_on(conn, nb_live)
    add_plan(conn, nb_sections(headlines=["x" * 91]))  # refused inside the enable, before anything is sent
    made, back = _approve_while_the_off_is_out(conn, nb_live, NEW_HEADLINES)
    out = step(conn)
    back()
    assert (out["state"], out["last_error"], switched(nb_live)[0]) == ("launching", None, "OFF")
    assert "v" in made and "stop_owed" not in nb_row(conn)["resources"]
    due = conn.execute(f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}").fetchall()
    assert len(due) == 1  # the hourly step has it: the newer version is not stranded
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"], switched(nb_live)[0]) == ("live", made["v"], None, "ON")
    assert unlisted_on(conn, nb_live) == set()


def test_an_approval_that_commits_just_after_the_stop_came_back_still_launches(conn, nb_live, launched):
    """M1 follow-up 2 fix round 1 (F4, the review's probe W6x): the approval's own transaction -- another connection --
    queued the row while the refused step's stop was out, and commits a moment after the stop came back. The row is
    held (`FOR UPDATE`) before the approved version is read, so the step waits for that commit and reads the newer
    version: the row stays `launching`, due, and the next step launches it -- never `failed` with the older refusal on
    the row the approval requeued."""
    _cut_after_the_campaigns_on(conn, nb_live)
    add_plan(conn, nb_sections(headlines=["x" * 91]))  # refused inside the enable, before anything is sent
    real = nb_live._campaign_updateStatus
    other = psycopg.connect(TEST_DB_URL, row_factory=dict_row)  # not autocommit: the approval's one transaction
    made: dict = {}
    commits: list[threading.Thread] = []

    def approving(ident, payload):
        if payload.get("status") == "OFF" and not made:
            made["v"] = add_plan(other, nb_sections(headlines=NEW_HEADLINES))
            ads_run.queue(other, BIZ, NB, made["v"], ready=True)  # the row held by the approval's transaction
            commits.append(threading.Thread(target=lambda: (time.sleep(1.0), other.commit()), daemon=True))
            commits[0].start()
        return real(ident, payload)

    nb_live._campaign_updateStatus = approving
    try:
        out = step(conn)
    finally:
        nb_live._campaign_updateStatus = real
        for t in commits:
            t.join()
        other.close()
    assert (out["state"], out["last_error"], switched(nb_live)[0]) == ("launching", None, "OFF")
    due = conn.execute(f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}").fetchall()
    assert len(due) == 1
    out = step(conn)
    assert (out["state"], out["plan_id"], switched(nb_live)[0]) == ("live", made["v"], "ON")


def test_a_channel_that_cannot_pause_never_fails_a_row_whose_ads_may_serve(conn, nb_live, launched, monkeypatch):
    """R1 and the protocol's honest capabilities (the lifecycle never calls what a channel says it lacks): a channel
    whose built ads it cannot pause has no stop to send, so the refusal leaves the row as it is -- `launching`, with
    the refusal's sentence -- and nothing is sent."""
    _cut_after_the_campaigns_on(conn, nb_live)
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    no_pause = ADAPTER.capabilities().model_copy(update={"pause": False})
    monkeypatch.setattr(ADAPTER, "capabilities", lambda: no_pause)
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["last_error"], nb_live.kinds()) == ("launching", nb.TOO_LITTLE_COPY, [])


# ---------------------------------------------------------------- the page follow (R7, R8)


def test_landing_url_is_the_builds_click_url_and_carried_url_is_what_the_ads_carry():
    assert ADAPTER.landing_url(PAGE_URL) == nb_build.page_url(PAGE_URL) == CLICK_URL
    assert ADAPTER.carried_url({"click_url": CLICK_URL}) == CLICK_URL and ADAPTER.carried_url({}) is None


def test_move_page_moves_every_running_ad_to_the_new_address_and_nothing_else(conn, nb_live, launched):
    account, plan, _build = built(conn)
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    build = ADAPTER.build(conn, config.load(), BIZ, plan, business.latest_knowledge(conn, BIZ)[1], account)
    resources = nb_row(conn)["resources"]
    with ADAPTER.client() as http:
        done = ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=False)
    target = nb_build.page_url(new)
    assert done.ids == {**resources, "click_url": target}
    assert nb_live.kinds() == ["ad/update", "ad/update"]
    assert nb_live.bodies("ad/update") == [{"creative": {"clickThroughUrl": target}}] * 2
    assert all(ad["creative"]["content"]["clickThroughUrl"] == target for ad in nb_live.ads.values())


def test_move_page_with_money_sends_the_running_plans_budget_first(conn, nb_live, launched):
    account, plan, build = built(conn)
    resources = nb_row(conn)["resources"]
    with ADAPTER.client() as http:
        ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=True)
    assert nb_live.kinds() == ["ad-set/update", "ad/update", "ad/update"]
    ((money),) = nb_live.bodies("ad-set/update")
    assert money == {"budget": 1973, "bidRate": 150, "targeting": nb_build.targeting(resources["location_ids"])}


def test_a_refused_address_is_address_refused_and_a_busy_newsbreak_is_retried(conn, nb_live, launched):
    account, plan, build = built(conn)
    resources = nb_row(conn)["resources"]
    nb_live.fail["ad/update"] = envelope(41926, "clickThroughUrl is not allowed")
    with ADAPTER.client() as http, pytest.raises(base.AddressRefused) as e:
        ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=False)
    assert str(e.value) == newsbreak.REFUSED_WITH_REASON.format(41926) and e.value.retryable is False
    nb_live.fail["ad/update"] = envelope(4034, "Rate limit exceeded")
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=False)
    assert not isinstance(e.value, base.AddressRefused) and (str(e.value), e.value.retryable) == (nb.RETRYING, True)


def test_a_page_move_the_token_cannot_send_says_the_change_waits_for_the_access(conn, nb_live, launched):
    """Item 5 on a move: the token's trouble (code 4033) is no refused address -- a retryable stop whose sentence says
    the change goes out once Admiry's access is fixed (the ads run meanwhile)."""
    account, plan, build = built(conn)
    resources = nb_row(conn)["resources"]
    nb_live.fail["ad/update"] = envelope(4033, "invalid access token")
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=False)
    assert not isinstance(e.value, base.AddressRefused)
    assert (str(e.value), e.value.retryable) == (nb.ACCESS_FIXING_CHANGE, True)


def test_a_newer_version_with_the_page_moved_carries_the_running_ads_to_the_new_address(conn, nb_live, launched):
    """The lifecycle's update also moves the ads to the page's address (`ads_run._update`), and its write then forgets
    the move (`url_stale`): a version that keeps the words keeps the ads, so they are moved there as `move_page`
    would."""
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    v2 = add_plan(conn, nb_sections(monthly_usd=900))
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    target = nb_build.page_url(new)
    assert nb_live.kinds() == [
        "ad-set/update",
        "ad/update",
        "ad/update",
        "balance/getAccountBudgetInfo",
        "balance/updateAccountsBudget",
    ]
    assert nb_live.bodies("ad/update") == [{"creative": {"clickThroughUrl": target}}] * 2
    resources = nb_row(conn)["resources"]
    assert resources["click_url"] == target and "url_stale" not in resources


def _moved_after_a_refused_change(conn, fake) -> str:
    """v2's second new ad refused (its first, made OFF, left over), then the page moved: returns the left-over ad."""
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    answer_ad_creates(fake, nth(2), envelope(41926, "the ad is not allowed"))
    step(conn)
    (left_over,) = nb_row(conn)["resources"]["leftover"]
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    fake.requests.clear()
    fake.sent.clear()
    return left_over


def _ads_updated(fake) -> list[str]:
    return [request.url.path.rsplit("/", 1)[-1] for request in fake.requests if "/ad/update/" in request.url.path]


def test_a_page_move_moves_only_the_ads_that_serve(conn, nb_live, launched):
    """Fix round 2, item 3 (the re-review's probe R2): an ad on its way out (`leftover`, here OFF: a refused change's)
    is never moved -- only the running ads (`ads`, and an update's in flight)."""
    running = set(nb_row(conn)["resources"]["ads"].values())
    left_over = _moved_after_a_refused_change(conn, nb_live)
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert set(_ads_updated(nb_live)) == running and left_over not in _ads_updated(nb_live)
    assert nb_row(conn)["resources"]["click_url"] == nb_build.page_url("https://brightsmile.example/new")


def test_a_left_over_ad_newsbreak_no_longer_has_never_makes_the_address_refused(conn, nb_live, launched):
    """Probe R2b: the left-over ad was deleted by hand in Ads Manager; the page move is untouched by it -- no refused
    address, no false sentence for the owner."""
    left_over = _moved_after_a_refused_change(conn, nb_live)
    del nb_live.ads[left_over]
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["last_error"], resources.get("refused_url")) == ("live", None, None)
    assert resources["click_url"] == nb_build.page_url("https://brightsmile.example/new")


def test_new_words_with_the_page_moved_carry_the_ads_left_running_to_the_new_address(conn, nb_live, launched):
    """Item 3 in an update: the new ads are made at the page's new address, and the running ads (`ads` when the update
    starts) -- left ON beside the new ones until those pass NewsBreak's review -- move there too, so none serves the old
    address; an ad already on its way out (`leftover`, here a refused change's, OFF) is never moved."""
    running = set(nb_row(conn)["resources"]["ads"].values())
    left_over = _moved_after_a_refused_change(conn, nb_live)
    v3 = add_plan(conn, nb_sections(headlines=["Gentle Care in Palo Alto", "Same-Day Crowns in Palo Alto"]))
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v3, None)
    target = nb_build.page_url("https://brightsmile.example/new")
    resources = nb_row(conn)["resources"]
    made = set(resources["ads"].values())
    assert len(made) == 2 and not made & running
    assert {nb_live.ads[ad]["creative"]["content"]["clickThroughUrl"] for ad in made | running} == {target}
    assert set(_ads_updated(nb_live)) == running and left_over not in _ads_updated(nb_live)
    assert resources["leftover"] == [left_over] and set(resources["replaced"]) == running
    assert resources["click_url"] == target


def test_a_page_move_moves_an_updates_ads_in_flight_too(conn, nb_live, launched):
    """Item 3: the ads that serve are `ads` and an update's in flight (`updating`: made, and turned ON once the last is
    made) -- a move carries both to the new address, never one on its way out."""
    running = set(nb_row(conn)["resources"]["ads"].values())
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    answer_ad_creates(nb_live, nth(2), envelope(4034, "Rate limit exceeded"))  # v2's first ad made, its second busy
    assert step(conn)["last_error"] == nb.RETRYING
    resources = nb_row(conn)["resources"]
    (in_flight,) = resources["updating"]["ads"].values()
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    account, plan, build = built(conn)
    nb_live.requests.clear()
    with ADAPTER.client() as http:
        done = ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=False)
    target = nb_build.page_url(new)
    assert set(_ads_updated(nb_live)) == running | {in_flight} and done.ids["click_url"] == target
    assert nb_live.ads[in_flight]["creative"]["content"]["clickThroughUrl"] == target


def _words_changed_then_the_page_moved(conn, fake) -> tuple[set[str], set[str], str]:
    """v2's new words made and turned on beside v1's ads -- `replaced`, serving until v2's pass NewsBreak's review
    (R15) -- and then the page moved. Returns (v1's ads, v2's, the new address as the ads carry it)."""
    old = set(serving(conn))
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    assert step(conn)["last_error"] is None
    new = set(serving(conn))
    assert set(nb_row(conn)["resources"]["replaced"]) == old and not old & new
    url = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (url, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, url)
    fake.requests.clear()
    fake.sent.clear()
    return old, new, nb_build.page_url(url)


def test_a_page_move_while_new_words_are_reviewed_moves_the_old_ads_that_still_serve(conn, nb_live, launched):
    """Fix round 2, item 4: the old ads a finished change of words replaced serve beside the new ones until those pass
    NewsBreak's review (R15) -- so a page move carries them to the new address with the running ones, and no click
    goes to the old address (perhaps a page that is gone)."""
    old, new, target = _words_changed_then_the_page_moved(conn, nb_live)
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert set(_ads_updated(nb_live)) == old | new
    assert {nb_live.ads[ad]["creative"]["content"]["clickThroughUrl"] for ad in old | new} == {target}
    resources = nb_row(conn)["resources"]
    assert resources["click_url"] == target and set(resources["replaced"]) == old


def test_an_old_ad_newsbreak_no_longer_has_never_makes_the_address_refused(conn, nb_live, launched):
    """Item 4 with probe R2b's guard: an old ad still listed as replaced was deleted by hand in Ads Manager -- it serves
    nothing, and its failed move never reads as NewsBreak refusing the address: the others move, the row stays live
    with no sentence."""
    old, _new, target = _words_changed_then_the_page_moved(conn, nb_live)
    gone, kept = sorted(old)
    del nb_live.ads[gone]
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["last_error"], resources.get("refused_url")) == ("live", None, None)
    assert resources["click_url"] == target
    assert nb_live.ads[kept]["creative"]["content"]["clickThroughUrl"] == target


def test_newsbreak_busy_on_an_old_ads_move_has_the_move_retried(conn, nb_live, launched):
    """Item 4: NewsBreak busy on an old ad's move is a retryable stop, as for any move -- never the address refused --
    so the move is sent again and no old ad stays behind on the old address."""
    old, _new, _target = _words_changed_then_the_page_moved(conn, nb_live)
    busy = min(old)
    real = nb_live._ad_update

    def busy_for_one(ident, payload):
        return envelope(4034, "Rate limit exceeded") if ident == busy else real(ident, payload)

    nb_live._ad_update = busy_for_one
    account, plan, build = built(conn)
    resources = nb_row(conn)["resources"]
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.move_page(conn, config.load(), account, resources, plan, build, http=http, with_money=False)
    assert not isinstance(e.value, base.AddressRefused) and (str(e.value), e.value.retryable) == (nb.RETRYING, True)


def test_a_newer_version_with_the_page_moved_carries_the_old_ads_that_still_serve_too(conn, nb_live, launched):
    """Item 4 in an update: v3 keeps v2's words (a new budget) while v1's ads still serve beside v2's; the page moved,
    so the update carries every ad that serves -- v2's and v1's -- to the new address."""
    old, new, target = _words_changed_then_the_page_moved(conn, nb_live)
    v3 = add_plan(conn, nb_sections(monthly_usd=900, headlines=NEW_HEADLINES))
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v3, None)
    assert set(_ads_updated(nb_live)) == old | new
    assert {nb_live.ads[ad]["creative"]["content"]["clickThroughUrl"] for ad in old | new} == {target}
    resources = nb_row(conn)["resources"]
    assert resources["click_url"] == target and set(resources["replaced"]) == old


def _out_of_the_set(monkeypatch, file: str) -> None:
    """Admiry's set without `file`: the founder took the picture out."""
    shown = creative.manifest
    monkeypatch.setattr(creative, "manifest", lambda: [e for e in shown() if e["file"] != file])


def test_a_page_move_of_ads_whose_picture_left_the_set_moves_them_and_refuses_nothing(
    conn, nb_live, launched, monkeypatch
):
    """Final review I3 (its probe): running ads keep the picture they carry -- a picture must be in Admiry's set only to
    be uploaded -- so a URL-only move of ads whose approved picture left the set since moves them and never marks the
    running version refused; and a resume with the page moved again moves them there before turning them on, never
    failing the row."""
    _out_of_the_set(monkeypatch, IMAGE)
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["last_error"], resources.get("refused_plan_id")) == ("live", None, None)
    assert resources["click_url"] == nb_build.page_url(new) and "url_stale" not in resources
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    newer = "https://brightsmile.example/newer"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (newer, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, new, newer)
    out = ads_run.resume(conn, config.load(), BIZ, NB)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert nb_row(conn)["resources"]["click_url"] == nb_build.page_url(newer)


def test_a_version_showing_the_picture_the_running_ads_carry_goes_through_though_it_left_the_set(
    conn, nb_live, launched, monkeypatch
):
    """I3 (D89 holds: the owner approved that picture with v1): v2 keeps v1's picture, pinned when v2 was made, and
    changes the money and the words; the picture left the set before v2's approval, which stored none. Nothing needs
    uploading -- NewsBreak has it -- so v2 goes through, its new ads on the picture the running ones carry."""
    (asset,) = nb_row(conn)["resources"]["assets"].values()
    secs = nb_sections(monthly_usd=900, headlines=NEW_HEADLINES, image=None)
    secs["creative"]["proposed_image"] = {NB: IMAGE}
    v2 = add_plan(conn, secs)
    _out_of_the_set(monkeypatch, IMAGE)
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", v2, None)
    made = nb_row(conn)["resources"]["ads"].values()
    assert {nb_live.ads[ad]["creative"]["content"]["assetUrl"] for ad in made} == {asset}
    assert "ad/uploadAssets" not in nb_live.kinds()


def test_a_version_that_keeps_its_words_and_the_carried_picture_makes_no_new_ads(conn, nb_live, launched, monkeypatch):
    """I3: the picture a version shows is the one it pinned, whether or not its approval could store it -- so a new
    budget on the same words and the carried picture is the money alone, no new ads for NewsBreak to review."""
    secs = nb_sections(monthly_usd=900, image=None)
    secs["creative"]["proposed_image"] = {NB: IMAGE}
    v2 = add_plan(conn, secs)
    _out_of_the_set(monkeypatch, IMAGE)
    nb_live.sent.clear()
    out = step(conn)
    assert (out["state"], out["plan_id"]) == ("live", v2)
    assert nb_live.kinds() == ["ad-set/update", "balance/getAccountBudgetInfo", "balance/updateAccountsBudget"]


def test_a_new_picture_that_left_the_set_before_its_upload_refuses_the_version_and_the_ads_keep_theirs(
    conn, nb_live, launched, monkeypatch
):
    """I3: a picture must be in Admiry's set to be uploaded -- v2 shows another one (never uploaded to this account),
    which left the set: v2 is refused with NO_IMAGE (its money went first, S16), nothing is uploaded or made, and the
    running ads keep their version and their picture."""
    running = set(nb_row(conn)["resources"]["ads"].values())
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    _out_of_the_set(monkeypatch, "dentists/2.jpg")
    nb_live.sent.clear()
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["last_error"], resources["refused_plan_id"]) == ("live", nb.NO_IMAGE, v2)
    assert nb_live.kinds() == ["ad-set/update"] and set(resources["ads"].values()) == running


def test_a_failed_launch_started_again_after_its_picture_left_the_set_turns_no_ad_on(conn, nb_live, monkeypatch):
    """M1 follow-up R2 (the re-review's probe): the set is skipped only for the picture ads that served carry. A fresh
    launch whose enable NewsBreak refused failed with its campaign OFF (C1); its picture then left the set, and a new
    approval starts the row again (ads are built: `launching`). Those ads never served, so their picture is no running
    ads' -- the version showing it is refused with NO_IMAGE before anything turns on, and the row fails with the
    campaign OFF and no ad ON."""
    v1 = seed(conn)
    queue(conn, v1)
    back = answer_switch(nb_live, "ad", "ON", 41926)
    out = step(conn)
    back()
    assert (out["state"], switched(nb_live)) == ("failed", ("OFF", "ON", 0))
    _out_of_the_set(monkeypatch, IMAGE)
    secs = nb_sections(image=None)  # the approval stored none: the picture left the set
    secs["creative"]["proposed_image"] = {NB: IMAGE}
    v2 = add_plan(conn, secs)
    queue(conn, v2)  # as the approval queues a row whose ads are built (`ads_run.has_ads`)
    assert nb_row(conn)["state"] == "launching"
    out = step(conn)
    assert (out["state"], out["last_error"], switched(nb_live)) == ("failed", nb.NO_IMAGE, ("OFF", "ON", 0))


def test_a_version_going_back_to_an_earlier_picture_that_left_the_set_is_refused_and_the_ads_keep_theirs(
    conn, nb_live, launched, monkeypatch
):
    """R2: a picture uploaded earlier is no running ads' once others replaced it -- v2's ads show another picture, and
    v3 goes back to v1's, which left the set since: v3 is refused with NO_IMAGE (its money first, S16), no ad is made
    on the picture that left, and the running ads keep v2 and its picture."""
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    assert step(conn)["plan_id"] == v2
    running = set(nb_row(conn)["resources"]["ads"].values())
    _out_of_the_set(monkeypatch, IMAGE)
    secs = nb_sections(image=None)
    secs["creative"]["proposed_image"] = {NB: IMAGE}
    v3 = add_plan(conn, secs)
    nb_live.sent.clear()
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["plan_id"], out["last_error"], resources.get("refused_plan_id")) == (
        "live",
        v2,
        nb.NO_IMAGE,
        v3,
    )
    assert nb_live.kinds() == ["ad-set/update"] and set(resources["ads"].values()) == running


def test_an_update_cut_short_whose_picture_then_left_the_set_never_finishes_and_its_ads_go_off(
    conn, nb_live, launched, monkeypatch
):
    """R2 on a change in flight: v2's first new ad was made on its picture, then NewsBreak was busy; the picture left
    the set before the next step. It is no running ads' (theirs is v1's), so v2 is refused with NO_IMAGE before
    anything more is made -- and the ad the cut attempt made goes OFF into `leftover`, never left in flight."""
    running = set(nb_row(conn)["resources"]["ads"].values())
    v2 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES, image="dentists/2.jpg"))
    answer_ad_creates(nb_live, nth(2), envelope(4034, "Rate limit exceeded"))
    out = step(conn)
    (made,) = nb_row(conn)["resources"]["updating"]["ads"].values()
    assert (out["state"], out["plan_id"], out["last_error"]) == ("live", launched, nb.RETRYING)
    _out_of_the_set(monkeypatch, "dentists/2.jpg")
    nb_live.sent.clear()
    out = step(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], out["last_error"], resources.get("refused_plan_id")) == ("live", nb.NO_IMAGE, v2)
    assert nb_live.kinds() == ["ad-set/update", "ad/updateStatus"] and nb_live.ads[made]["status"] == "OFF"
    assert resources["leftover"] == [made] and "updating" not in resources
    assert set(resources["ads"].values()) == running and unlisted_on(conn, nb_live) == set()


def test_a_page_move_reaches_the_newsbreak_row_through_the_lifecycle(conn, nb_live, launched):
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    out = step(conn)
    assert out["state"] == "live" and nb_row(conn)["resources"]["click_url"] == nb_build.page_url(new)
    assert "url_stale" not in nb_row(conn)["resources"] and "ad/update" in nb_live.kinds()


# ---------------------------------------------------------------- the hourly read (Task 5b: R13-R16, R21-R25)

# NewsBreak's own review sentences (`statusTxt`), as the tests make NewsBreak say them
TURNED_DOWN = "Rejected: the landing page does not match the ad. Edit the ad and submit it again."
TURNED_DOWN_TOO = "Rejected: the image has text over more than 20% of it."
# what ads-sync reads of NewsBreak for one row: the report (pull_metrics), then the ads, the campaign and the ad set
READS = ["reports/getIntegratedReport", "ad/getList", "campaign/getList", "ad-set/getList"]


def sync(conn) -> dict:
    """ads-sync's read of the business's NewsBreak row (`ads_run.sync`: pull_metrics, then pull_issues)."""
    return ads_run.sync(conn, config.load(), BIZ, NB)


def serving(conn) -> list[str]:
    """The row's running ads (`resources.ads`), in the order of their slots."""
    ads = nb_row(conn)["resources"]["ads"]
    return [ads[slot] for slot in sorted(ads, key=int)]


def open_issues(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT channel, kind, platform_text FROM channel_issue WHERE business_id = %s AND resolved_at IS NULL "
            "ORDER BY id",
            (BIZ,),
        ).fetchall()
    ]


def metric_days(conn) -> list[tuple]:
    found = conn.execute(
        "SELECT day, cost_micros, impressions, clicks, calls, conversions FROM metric_daily WHERE business_id = %s "
        "AND channel = %s ORDER BY day",
        (BIZ, NB),
    ).fetchall()
    return [
        (r["day"], r["cost_micros"], r["impressions"], r["clicks"], r["calls"], float(r["conversions"])) for r in found
    ]


def test_pull_metrics_sums_the_hourly_report_per_day_of_the_accounts_calendar(conn, nb_live, launched):
    """R13 (and Review Focus 3's first half): NewsBreak's HOURLY report by ad and day -- its settlement basis, never
    REALTIME -- read for the last three days of the account's own calendar (America/Los_Angeles, R6; NOW is 10:00 there
    on October 1) and summed per day: two ads on one day are one `metric_daily` row, the cost in cents x 10,000 as
    micros; calls are 0 (NewsBreak reports none)."""
    first, second = serving(conn)
    nb_live.add_report_row(first, "2026-10-01", cost=1234, impression=500, click=7, conversion=1)
    nb_live.add_report_row(second, "2026-10-01", cost=766, impression=300, click=3)
    nb_live.add_report_row(first, "2026-09-30", cost=99, impression=40, click=1)
    nb_live.add_report_row(first, "2026-09-28", cost=5000, impression=900, click=20)  # before the three days
    out = sync(conn)
    assert out["state"] == "live" and nb_live.kinds() == READS and out["ops"] == len(READS)
    (asked,) = nb_live.bodies("reports/getIntegratedReport")
    assert (asked["timezone"], asked["startDate"], asked["endDate"], asked["dataSource"]) == (
        LA,
        "2026-09-29",
        "2026-10-01",
        "HOURLY",
    )
    assert metric_days(conn) == [
        (date(2026, 9, 30), 990_000, 40, 1, 0, 0.0),
        (date(2026, 10, 1), 20_000_000, 800, 10, 0, 1.0),
    ]


def test_a_report_row_of_a_day_not_asked_for_never_overwrites_that_day(conn, nb_live, launched):
    """A row NewsBreak answers outside the three days asked for (a misbehaving answer) is no figure for its day: it may
    be one ad's alone, and would overwrite the day's whole sum that is on file."""
    first, _second = serving(conn)
    nb_live.add_report_row(first, "2026-10-01", cost=100)
    real = nb_live._reports_getIntegratedReport

    def with_a_stray_day(ident, payload):
        answer = real(ident, payload).json()
        answer["data"]["rows"].append({**answer["data"]["rows"][0], "date": "2026-09-20", "cost": 7})
        return ok(answer["data"])

    nb_live._reports_getIntegratedReport = with_a_stray_day
    sync(conn)
    assert [day for day, *_rest in metric_days(conn)] == [date(2026, 10, 1)]


def test_an_account_whose_zone_is_unknown_is_read_in_the_clients_zone(conn, nb_live, launched):
    """R6: a report's days are its zone's, and a row that never kept one is read in `client.REPORT_TIMEZONE` (UTC,
    the zone M0's wallet-settle reads an unknown one in) -- never NewsBreak's default by omission."""
    ads_run.forget(conn, BIZ, NB, "time_zone")
    sync(conn)
    (asked,) = nb_live.bodies("reports/getIntegratedReport")
    assert (asked["timezone"], asked["endDate"]) == (newsbreak.REPORT_TIMEZONE, "2026-10-01")


def test_report_rows_on_no_day_are_never_counted_and_logged_once_a_day(conn, nb_live, launched, caplog, monkeypatch):
    """M1 Task 6 fix round 1, item 3 (review M1): a report row whose cost NewsBreak puts on no day Admiry can read (its
    day under another key, or in another format) adds to no day -- no metric row is made up -- and is logged once per
    account and day, never every hour: the operator's nb-reconcile fails on it."""
    first, _second = serving(conn)
    nb_live.add_report_row(first, "2026-10-01", cost=250)
    real = nb_live._reports_getIntegratedReport

    def undated(ident, payload):
        data = real(ident, payload).json()["data"]
        data["rows"] = [{**{k: v for k, v in r.items() if k != "date"}, "statDate": r["date"]} for r in data["rows"]]
        return ok(data)

    nb_live._reports_getIntegratedReport = undated
    with caplog.at_level(logging.WARNING):
        sync(conn)
        sync(conn)
        assert caplog.text.count("on no day") == 1 and metric_days(conn) == []
        monkeypatch.setattr(ads_run, "now", lambda: NOW + timedelta(days=1))
        sync(conn)
        assert caplog.text.count("on no day") == 2


def test_report_rows_without_a_cost_count_as_no_money_and_are_logged_once_a_day(
    conn, nb_live, launched, caplog, monkeypatch
):
    """Final review Minor 3: a report row with no cost Admiry can read is never $0 spent -- it adds to no day: no
    metric row is made of it, and a day the sync read before keeps what it had (never overwritten with nothing); it is
    logged once per account and day, and the operator's nb-reconcile fails on it."""
    first, second = serving(conn)
    nb_live.add_report_row(first, "2026-09-30", cost=400, click=2)
    nb_live.add_report_row(second, "2026-10-01", cost=250, click=1)
    sync(conn)
    read = [(date(2026, 9, 30), 4_000_000, 0, 2, 0, 0.0), (date(2026, 10, 1), 2_500_000, 0, 1, 0, 0.0)]
    assert metric_days(conn) == read
    nb_live.add_report_row(first, "2026-09-29", cost=0, click=5)
    for row in nb_live.report_rows:
        if row["date"] in ("2026-09-29", "2026-10-01"):
            del row["cost"]  # NewsBreak's answer carries no cost for these rows now
    with caplog.at_level(logging.WARNING):
        sync(conn)
        sync(conn)
        assert caplog.text.count("no cost") == 1 and metric_days(conn) == read
        monkeypatch.setattr(ads_run, "now", lambda: NOW + timedelta(days=1))
        sync(conn)
        assert caplog.text.count("no cost") == 2


def test_rejected_ad_is_quoted_verbatim(conn, nb_live, launched):
    """Review Focus 4 (R14): NewsBreak turns an ad down (`auditStatus` REJECTED) and says why (`statusTxt`). ads-sync
    writes a `channel_issue(kind='disapproval')` row with NewsBreak's sentence verbatim -- nothing in M0 writes one --
    the row reads `limited`, and the card says `Needs attention: "..."` with it; once NewsBreak's review clears, the
    issue is resolved and the ads read live again."""
    first, _second = serving(conn)
    nb_live.reject(first, TURNED_DOWN)
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("limited", None)
    assert open_issues(conn) == [{"channel": NB, "kind": "disapproval", "platform_text": TURNED_DOWN}]
    (card,) = state.channel_rows(conn, config.load(), BIZ)
    assert card.state_text == f'Needs attention: "{TURNED_DOWN}"'
    assert {"kind": "disapproval", "platform_text": TURNED_DOWN} in [
        {k: i[k] for k in ("kind", "platform_text")} for i in card.issues if "platform_text" in i
    ]
    assert sync(conn)["state"] == "limited" and len(open_issues(conn)) == 1  # one row while it stands, not one an hour
    nb_live.approve(first)
    out = sync(conn)
    assert out["state"] == "live" and open_issues(conn) == []
    (resolved,) = conn.execute("SELECT resolved_at FROM channel_issue WHERE business_id = %s", (BIZ,)).fetchall()
    assert resolved["resolved_at"] is not None
    (card,) = state.channel_rows(conn, config.load(), BIZ)
    assert card.state_text == "Live"


def test_one_open_issue_per_sentence_newsbreak_gives_each_resolved_on_its_own(conn, nb_live, launched):
    first, second = serving(conn)
    nb_live.reject(first, TURNED_DOWN)
    nb_live.reject(second, TURNED_DOWN)
    sync(conn)
    assert [i["platform_text"] for i in open_issues(conn)] == [TURNED_DOWN]
    nb_live.reject(second, TURNED_DOWN_TOO)
    sync(conn)
    assert [i["platform_text"] for i in open_issues(conn)] == [TURNED_DOWN, TURNED_DOWN_TOO]
    nb_live.approve(first)
    assert sync(conn)["state"] == "limited"
    assert [i["platform_text"] for i in open_issues(conn)] == [TURNED_DOWN_TOO]


def test_an_ad_turned_down_without_a_sentence_is_quoted_by_newsbreaks_own_word(conn, nb_live, launched):
    first, _second = serving(conn)
    nb_live.reject(first, "")
    sync(conn)
    assert [i["platform_text"] for i in open_issues(conn)] == ["REJECTED"]


def test_ads_newsbreak_shows_off_read_as_paused_there_and_an_operator_is_told_once_each_time(
    conn, nb_live, launched, resend
):
    """Fix round 1, item 2 (a): NewsBreak shows the ads OFF though Admiry runs them -- someone in Admiry's Organization,
    or NewsBreak itself, switched them off. The owner has no NewsBreak access, so the row says NewsBreak paused them and
    an operator is looking into it (it never asks the owner to act there), and one operator mail goes per occurrence;
    once they run again the row is live, and the next occurrence is told again."""
    (campaign,) = nb_live.campaigns.values()
    campaign["status"] = "OFF"
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("limited", nb.PAUSED_IN_NEWSBREAK)
    assert nb.PAUSED_IN_NEWSBREAK == "NewsBreak has paused your ads; an Admiry operator is looking into it"
    assert _plain(nb.PAUSED_IN_NEWSBREAK) and "resume" not in nb.PAUSED_IN_NEWSBREAK
    (mail,) = resend.sent
    assert mail["to"] == ["ops@example.com"] and BIZ in mail["text"] and campaign["id"] in mail["text"]
    sync(conn)
    assert len(resend.sent) == 1  # told once while it stands
    campaign["status"] = "ON"
    (ad_set,) = nb_live.ad_sets.values()
    ad_set["status"] = "OFF"  # the ad set too (Admiry only ever pauses the campaign): the ads were never back on
    assert (sync(conn)["state"], nb_row(conn)["last_error"]) == ("limited", nb.PAUSED_IN_NEWSBREAK)
    assert len(resend.sent) == 1
    ad_set["status"] = "ON"
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    campaign["status"] = "OFF"  # another time
    sync(conn)
    assert len(resend.sent) == 2 and resend.keys[0] != resend.keys[1]


def test_admirys_own_pause_and_the_owners_reach_ads_newsbreak_paused(conn, nb_live, launched):
    """Item 2 (b) (the review's probe C): NewsBreak's note is no hand on the platform that only the owner may lift (M0's
    H33 is for a channel the owner runs in their own account): NewsBreak has no `paused_elsewhere` words, so Admiry's
    own pause -- here for a balance that ran out -- is recorded and sent, as the owner's is; stopping spend is always
    allowed."""
    assert ADAPTER.words.paused_elsewhere is None
    (campaign,) = nb_live.campaigns.values()
    campaign["status"] = "OFF"
    sync(conn)
    nb_live.sent.clear()
    out = ads_run.pause(conn, config.load(), BIZ, NB, by="job:spend-guard", reason="no_funds")
    assert (out["state"], out["ops"], nb_live.kinds()) == ("paused", 1, ["campaign/updateStatus"])
    row = nb_row(conn)
    assert (row["paused_reason"], row["last_error"], row["resources"].get("pause_owed")) == ("no_funds", None, None)
    nb_live.sent.clear()
    out = ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    assert out["state"] == "paused" and nb_row(conn)["resources"]["owner_paused"] is True


def test_ads_newsbreak_runs_while_admiry_shows_them_paused_are_paused_again_within_the_hour(
    conn, nb_live, launched, resend
):
    """Item 2 (c): the ads are paused in Admiry but NewsBreak shows them ON (switched on there) -- never only a note:
    the pause is owed to NewsBreak again (`pause_owed`; the owner's line says the ads may still show there and Admiry
    is pausing them, M1 Task 7) and the hourly ads-launch sends it; one operator mail per occurrence."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    (campaign,) = nb_live.campaigns.values()
    campaign["status"] = "ON"
    out = sync(conn)
    row = nb_row(conn)
    assert (out["state"], row["resources"]["pause_owed"], row["last_error"]) == (
        "paused",
        True,
        ADAPTER.words.pause_owed,
    )
    assert row["resources"]["owner_paused"] is True
    (mail,) = resend.sent
    assert mail["to"] == ["ops@example.com"] and BIZ in mail["text"] and campaign["id"] in mail["text"]
    sync(conn)
    assert len(resend.sent) == 1
    due = f"SELECT 1 FROM channel_campaign c WHERE {ads_run.due(config.load(), NB)}"
    assert conn.execute(due).fetchall() != []
    nb_live.sent.clear()
    out = step(conn)  # the hourly ads-launch
    assert (out["state"], nb_live.bodies("campaign/updateStatus")) == ("paused", [{"status": "OFF"}])
    row = nb_row(conn)
    assert ("pause_owed" in row["resources"], row["last_error"], campaign["status"]) == (False, None, "OFF")
    sync(conn)
    assert len(resend.sent) == 1


def test_ads_newsbreak_switched_off_at_its_cap_come_back_on_with_the_new_months_cap(
    conn, nb_live, launched, monkeypatch
):
    """Item 2 (d): NewsBreak may switch a capped account's ads off at its spending cap (a pilot check). On the first
    read of the new month, once the new cap is set, ads Admiry runs and nobody paused are switched back on -- the
    campaign and the ad set -- with one audit row naming both (M1 Task 7, A5: one activity line per event), and the
    row reads live."""
    (campaign,) = nb_live.campaigns.values()
    (ad_set,) = nb_live.ad_sets.values()
    campaign["status"] = ad_set["status"] = "OFF"
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    nb_live.sent.clear()
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert nb_live.kinds()[len(READS) :] == [
        "balance/getAccountBudgetInfo",
        "balance/updateAccountsBudget",
        "campaign/updateStatus",
        "ad-set/updateStatus",
    ]
    assert (campaign["status"], ad_set["status"]) == ("ON", "ON")
    assert sync_audit(conn) == [
        "Admiry let NewsBreak spend up to $600.00 this month",
        "Admiry turned your ads on NewsBreak and their daily budget back on for the new month",
    ]
    assert all(_plain(said) for said in sync_audit(conn))


@pytest.mark.parametrize(
    ("off", "said"),
    [
        ("campaign", "Admiry turned your ads on NewsBreak back on for the new month"),
        ("ad_set", "Admiry turned the daily budget of your ads on NewsBreak back on for the new month"),
    ],
)
def test_the_back_on_line_names_the_one_switch_that_was_off(conn, nb_live, launched, monkeypatch, off, said):
    """A5: the event's one line names what went through, in the owner's words: the ads, their daily budget, or both."""
    (campaign,) = nb_live.campaigns.values()
    (ad_set,) = nb_live.ad_sets.values()
    {"campaign": campaign, "ad_set": ad_set}[off]["status"] = "OFF"
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    assert sync(conn)["state"] == "live" and (campaign["status"], ad_set["status"]) == ("ON", "ON")
    assert sync_audit(conn) == ["Admiry let NewsBreak spend up to $600.00 this month", said]
    on_rows = conn.execute(
        "SELECT result FROM agent_action WHERE business_id = %s AND tool = 'job.ads_sync_on'", (BIZ,)
    ).fetchall()
    assert [r["result"]["switched"] for r in on_rows] == [[off]]


def test_a_switch_back_on_that_did_not_all_go_through_names_what_did_not():
    """Fix round 1, M10: when one of the two switches that were off did not go through, the line names it -- their
    daily budget (the ad set) or the ads themselves (the campaign) -- never "the rest"."""
    assert nb._back_on_note(["campaign"], every=False) == (
        "turned your ads on NewsBreak back on for the new month; their daily budget did not come back on"
    )
    assert nb._back_on_note(["ad_set"], every=False) == (
        "turned the daily budget of your ads on NewsBreak back on for the new month; the ads themselves did not come "
        "back on"
    )
    assert all(_plain(nb._back_on_note(went, every=False)) for went in (["campaign"], ["ad_set"]))


def test_the_new_months_cap_never_turns_on_ads_someone_paused(conn, nb_live, launched, monkeypatch):
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    nb_live.sent.clear()
    out = sync(conn)
    assert out["state"] == "paused" and "campaign/updateStatus" not in nb_live.kinds()
    assert "balance/updateAccountsBudget" in nb_live.kinds()  # the month's cap is set all the same


def _a_pause_lands() -> None:
    """The owner's pause from another request while the read holds the business's step lock: `ads_run.pause` never
    waits for it (A3 S17) -- it records the pause, owed to NewsBreak, for the next ads-launch to send."""
    with db.connect(TEST_DB_URL) as other:
        out = ads_run.pause(other, config.load(), BIZ, NB, by="owner")
    assert out["state"] == "paused" and out["ops"] == 0


@pytest.mark.parametrize("when", ["before the switches", "between the two switches"])
def test_a_pause_that_lands_while_the_read_runs_is_never_undone_by_the_new_months_cap(
    conn, nb_live, launched, monkeypatch, when
):
    """Fix round 2, item 1 (the re-review's probe): NewsBreak switched the ads off at the old cap; on the first read of
    the new month the owner's pause lands while the read runs, recorded owed (A3 S17). The read reads the row again
    before each switch back on, as `enable` does: nothing more goes ON once the pause stands -- no switch, no audit row
    saying so -- and the next ads-launch sends the pause."""
    (campaign,) = nb_live.campaigns.values()
    (ad_set,) = nb_live.ad_sets.values()
    campaign["status"] = ad_set["status"] = "OFF"
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    real_month, real_switch = nb.NewsBreakAdapter._new_month, nb.newsbreak.campaign_status

    def new_month_then_a_pause(self, conn_, calls, biz, row):
        out = real_month(self, conn_, calls, biz, row)
        _a_pause_lands()
        return out

    def campaign_on_then_a_pause(settings, ident, on, *, http=None):
        out = real_switch(settings, ident, on, http=http)
        _a_pause_lands()
        return out

    if when == "before the switches":
        monkeypatch.setattr(nb.NewsBreakAdapter, "_new_month", new_month_then_a_pause)
    else:
        monkeypatch.setattr(nb.newsbreak, "campaign_status", campaign_on_then_a_pause)
    nb_live.sent.clear()
    out = sync(conn)
    resources = nb_row(conn)["resources"]
    assert (out["state"], resources["owner_paused"], resources["pause_owed"]) == ("paused", True, True)
    assert "ad-set/updateStatus" not in nb_live.kinds() and ad_set["status"] == "OFF"
    cap = "Admiry let NewsBreak spend up to $600.00 this month"
    if when == "before the switches":
        assert "campaign/updateStatus" not in nb_live.kinds() and campaign["status"] == "OFF"
        assert sync_audit(conn) == [cap]
    else:  # the campaign went ON before the pause landed: that switch is audited, and the pause turns it off
        assert campaign["status"] == "ON"
        assert sync_audit(conn) == [
            cap,
            "Admiry turned your ads on NewsBreak back on for the new month; their daily budget did not come back on",
        ]
        monkeypatch.setattr(nb.newsbreak, "campaign_status", real_switch)
    nb_live.sent.clear()
    after = step(conn)  # the hourly ads-launch
    assert (after["state"], nb_live.bodies("campaign/updateStatus")) == ("paused", [{"status": "OFF"}])
    assert campaign["status"] == "OFF" and "pause_owed" not in nb_row(conn)["resources"]


@pytest.mark.parametrize(
    ("state", "reason", "marks", "stands"),
    [
        ("live", None, {}, False),
        ("limited", None, {}, False),
        ("paused", None, {}, True),
        ("live", "no_funds", {}, True),
        ("live", None, {"owner_paused": True}, True),
        ("live", None, {"pause_owed": True}, True),
    ],
    ids=["live", "limited", "paused", "a reason of Admiry's", "the owner's mark", "a pause owed"],
)
def test_a_pause_stands_on_any_of_its_marks(conn, nb_live, launched, state, reason, marks, stands):
    """Item 1: what the read reads again before each switch back on -- the row's state, Admiry's pause reason, the
    owner's mark and a pause owed to NewsBreak: any one of them stops it."""
    conn.execute(
        "UPDATE channel_campaign SET state = %s, paused_reason = %s, resources = resources || %s "
        "WHERE business_id = %s AND channel = %s",
        (state, reason, Jsonb(marks), BIZ, NB),
    )
    assert nb._pause_stands(conn, BIZ) is stands


def test_a_switch_back_on_that_went_through_is_audited_when_the_next_one_fails(conn, nb_live, launched, monkeypatch):
    """Fix round 2, item 3: every switch back on that NewsBreak took is a money-relevant write that is audited, whatever
    happens to the next -- here the campaign went ON and the ad set's switch failed (NewsBreak busy): the event's one
    row names the campaign and the daily budget that did not come back on (A5; fix round 1, M10), the ad set stays OFF
    and the row says NewsBreak has the ads paused."""
    (campaign,) = nb_live.campaigns.values()
    (ad_set,) = nb_live.ad_sets.values()
    campaign["status"] = ad_set["status"] = "OFF"
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    nb_live.fail["ad-set/updateStatus"] = envelope(4034, "Rate limit exceeded")
    out = sync(conn)
    assert (campaign["status"], ad_set["status"]) == ("ON", "OFF")
    assert (out["state"], out["last_error"]) == ("limited", nb.PAUSED_IN_NEWSBREAK)
    assert sync_audit(conn) == [
        "Admiry let NewsBreak spend up to $600.00 this month",
        "Admiry turned your ads on NewsBreak back on for the new month; their daily budget did not come back on",
    ]


def test_newsbreaks_own_word_for_ads_it_holds_back_is_kept_for_the_owner(conn, nb_live, launched):
    """R14: the campaign's or ad set's online status other than ACTIVE (NewsBreak's own word, e.g. WARNING for limited
    delivery) reads `limited` and is kept, quoted, for the card (`resources.issues.campaign`); ACTIVE again, live."""
    (campaign,) = nb_live.campaigns.values()
    campaign["onlineStatus"] = "WARNING"
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("limited", None)
    assert nb_row(conn)["resources"]["issues"] == {"campaign": ["WARNING"]}
    (card,) = state.channel_rows(conn, config.load(), BIZ)
    assert {"kind": "serving", "platform_text": "WARNING"} in card.issues
    campaign["onlineStatus"] = "ACTIVE"
    assert sync(conn)["state"] == "live" and "issues" not in nb_row(conn)["resources"]


def test_the_read_keeps_a_sentence_another_step_left(conn, nb_live, launched):
    """A3 S15: the read sets and clears only its own notes; a change NewsBreak did not answer for stays on the row."""
    conn.execute(
        "UPDATE channel_campaign SET last_error = %s WHERE business_id = %s AND channel = %s", (nb.RETRYING, BIZ, NB)
    )
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", nb.RETRYING)


def sync_audit(conn) -> list[str]:
    """The owner sentences of the audit rows the hourly read wrote (`job.ads_sync_*`), oldest first."""
    found = conn.execute(
        "SELECT * FROM agent_action WHERE business_id = %s AND tool LIKE 'job.ads\\_sync\\_%%' ORDER BY id", (BIZ,)
    ).fetchall()
    return [tools_account.sentence(dict(r), None) for r in found]


def test_old_ads_stay_on_until_every_new_ad_passes_newsbreaks_review_then_go_off(conn, nb_live, launched):
    """R15: changed words are new ads beside the old ones (`replaced`, as the finished change left them); the old ads
    keep the business showing while any new ad is under review or turned down (the rejection on the card) -- never a
    moment with nothing running -- and go OFF the first read after every new ad passed, with an audit row; the list is
    then empty."""
    old = serving(conn)
    for ad in old:
        nb_live.approve(ad)
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    assert step(conn)["last_error"] is None
    new = serving(conn)
    resources = nb_row(conn)["resources"]
    assert sorted(resources["replaced"]) == sorted(old) and "leftover" not in resources
    for review in (lambda: None, lambda: nb_live.approve(new[0]), lambda: nb_live.reject(new[1], TURNED_DOWN)):
        review()
        nb_live.sent.clear()
        sync(conn)
        assert nb_live.kinds() == READS  # nothing turned off yet
        assert all(nb_live.ads[ad]["status"] == "ON" for ad in old)
        assert sorted(nb_row(conn)["resources"]["replaced"]) == sorted(old)
    assert open_issues(conn) == [{"channel": NB, "kind": "disapproval", "platform_text": TURNED_DOWN}]
    assert sync_audit(conn) == []
    nb_live.approve(new[1])
    nb_live.sent.clear()
    out = sync(conn)
    assert (out["state"], nb_live.kinds()) == ("live", [*READS, "ad/updateStatus", "ad/updateStatus"])
    assert nb_live.bodies("ad/updateStatus") == [{"status": "OFF"}, {"status": "OFF"}]
    assert all(nb_live.ads[ad]["status"] == "OFF" for ad in old) and all(
        nb_live.ads[ad]["status"] == "ON" for ad in new
    )
    assert "replaced" not in nb_row(conn)["resources"] and open_issues(conn) == []
    (said,) = sync_audit(conn)
    assert said == "Admiry turned off 2 older ads on NewsBreak: the new ones passed NewsBreak's review" and _plain(said)
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS


def test_old_ads_still_in_their_first_review_keep_running_until_the_new_ones_pass(conn, nb_live, launched):
    """Fix round 1, item 1 (the review's probe B): whether the ads a finished change replaced wait is never their own
    review -- here v1's ads are still under NewsBreak's first review when v2's words arrive: they stay ON while v2's
    are under review, and go OFF once v2's passed."""
    old = serving(conn)  # under review, never approved
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    step(conn)
    new = serving(conn)
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and all(nb_live.ads[ad]["status"] == "ON" for ad in old)
    for ad in new:
        nb_live.approve(ad)
    sync(conn)
    assert all(nb_live.ads[ad]["status"] == "OFF" for ad in old) and "replaced" not in nb_row(conn)["resources"]


def test_old_ads_back_under_review_after_a_page_move_keep_running_until_the_new_ones_pass(conn, nb_live, launched):
    """Item 1 (the review's probe A): v1's ads passed review; the page moved and v2 changed the words, so the update
    carried v1's ads to the new address -- NewsBreak reviews them again -- and made v2's. Nothing has passed review
    then, yet v1's ads stay ON (they are what NewsBreak shows the moment their review clears) until v2's passed."""
    old = serving(conn)
    for ad in old:
        nb_live.approve(ad)
    new_url = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new_url)
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    assert step(conn)["last_error"] is None
    assert all(nb_live.ads[ad]["auditStatus"] == "PENDING" for ad in old)  # moved: under review again
    new = serving(conn)
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and all(nb_live.ads[ad]["status"] == "ON" for ad in old)
    for ad in new:
        nb_live.approve(ad)
    sync(conn)
    assert all(nb_live.ads[ad]["status"] == "OFF" for ad in old) and "replaced" not in nb_row(conn)["resources"]


@pytest.mark.parametrize("how", ["gone", "deleted"], ids=["gone from its list", "listed as deleted"])
def test_old_ads_stay_on_while_newsbreak_lists_none_of_the_new_ones(conn, nb_live, launched, how):
    """R15's never a moment with nothing running: a read that finds none of the running ads -- gone from NewsBreak's
    list (deleted in Ads Manager), or listed as DELETED however their review reads -- has no new ad that passed review,
    so the old ones stay ON and listed."""
    old = serving(conn)
    for ad in old:
        nb_live.approve(ad)
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    assert step(conn)["last_error"] is None
    for ad in serving(conn):
        if how == "gone":
            del nb_live.ads[ad]
        else:
            nb_live.approve(ad)
            nb_live.ads[ad]["onlineStatus"] = "DELETED"
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and all(nb_live.ads[ad]["status"] == "ON" for ad in old)
    assert sorted(nb_row(conn)["resources"]["replaced"]) == sorted(old) and sync_audit(conn) == []


def _left_on(conn, fake) -> str:
    """5a's probe R4 as the read finds it: a change NewsBreak refused after its ads went ON (the cap), whose OFF for
    one of them never reached NewsBreak (the approval's time budget was spent) -- that ad ON, listed in `leftover`.
    Returns it."""
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    (account_id,) = fake.accounts
    refusal = {"adAccountId": account_id, "message": "Update failed. Budget must be between 0 cents and 10000000000"}
    fake.fail["balance/updateAccountsBudget"] = ok({"list": [refusal]})
    step(conn)
    del fake.fail["balance/updateAccountsBudget"]
    left, _off = nb_row(conn)["resources"]["leftover"]
    fake.ads[left]["status"] = "ON"
    return left


@pytest.mark.parametrize("passed", [True, False], ids=["it passed review itself", "under review itself"])
def test_an_ad_left_over_from_a_change_that_did_not_go_through_goes_off_at_once(conn, nb_live, launched, passed):
    """R25 and item 1: an ad of a change NewsBreak refused (`leftover`) is no old ad keeping the business showing, so
    it waits for nothing -- OFF the next read, whatever its own review says, while the running ads are still under
    review -- with an audit row."""
    left = _left_on(conn, nb_live)  # v1's running ads are still under review here
    if passed:
        nb_live.approve(left)
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.bodies("ad/updateStatus") == [{"status": "OFF"}] and nb_live.ads[left]["status"] == "OFF"
    assert "leftover" not in nb_row(conn)["resources"] and unlisted_on(conn, nb_live) == set()
    assert sync_audit(conn) == ["Admiry turned off an ad on NewsBreak left from a change that did not go through"]
    assert all(_plain(said) for said in sync_audit(conn))


def test_an_ad_left_on_beside_a_refused_change_goes_off_at_the_next_sync(conn, nb_live, launched):
    """R27 (a), R25: a left-over ad NewsBreak shows ON, on a row with `refused_plan_id` and running ads that passed
    review, is waiting for nothing: the next read turns it OFF at once and the list is empty; the refusal's mark
    stays (the version is not tried again)."""
    for ad in serving(conn):
        nb_live.approve(ad)
    left = _left_on(conn, nb_live)
    v2 = nb_row(conn)["resources"]["refused_plan_id"]
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == [*READS, "ad/updateStatus"] and nb_live.bodies("ad/updateStatus") == [{"status": "OFF"}]
    assert nb_live.ads[left]["status"] == "OFF" and unlisted_on(conn, nb_live) == set()
    resources = nb_row(conn)["resources"]
    assert "leftover" not in resources and resources["refused_plan_id"] == v2


def test_a_left_over_ad_already_off_or_gone_from_newsbreak_leaves_the_list_without_a_request(conn, nb_live, launched):
    """R21, R25: a refused change's ads are OFF already (5a's `_retire`) and one of them was deleted by hand in Ads
    Manager: the read drops both from `leftover` and sends nothing for them."""
    add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    (account_id,) = nb_live.accounts
    refusal = {"adAccountId": account_id, "message": "Update failed. Budget must be between 0 cents and 10000000000"}
    nb_live.fail["balance/updateAccountsBudget"] = ok({"list": [refusal]})
    step(conn)
    del nb_live.fail["balance/updateAccountsBudget"]
    off, gone = nb_row(conn)["resources"]["leftover"]
    assert nb_live.ads[off]["status"] == "OFF"
    del nb_live.ads[gone]
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and "leftover" not in nb_row(conn)["resources"] and sync_audit(conn) == []


@pytest.mark.parametrize("kind", ["leftover", "replaced"])
def test_an_ad_on_its_way_out_newsbreak_lists_as_deleted_leaves_the_list_without_a_request(
    conn, nb_live, launched, kind
):
    """R25: NewsBreak's lists show a deleted ad by its online status (DELETED, one of Get Ads' own words), whatever its
    switch says -- it is no ad NewsBreak has any more: dropped from either list, never sent an OFF every hour."""
    if kind == "leftover":
        gone = _left_on(conn, nb_live)
    else:
        add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
        step(conn)
        gone, _other = nb_row(conn)["resources"]["replaced"]
    nb_live.ads[gone]["onlineStatus"] = "DELETED"
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and gone not in (nb_row(conn)["resources"].get(kind) or [])


def test_a_left_over_ad_newsbreak_does_not_turn_off_stays_listed_for_the_next_sync(conn, nb_live, launched):
    left = _left_on(conn, nb_live)
    nb_live.fail["ad/updateStatus"] = envelope(4034, "Rate limit exceeded")
    out = sync(conn)
    assert out["state"] == "live" and nb_row(conn)["resources"]["leftover"] == [left] and sync_audit(conn) == []
    del nb_live.fail["ad/updateStatus"]
    sync(conn)
    assert nb_live.ads[left]["status"] == "OFF" and "leftover" not in nb_row(conn)["resources"]


def test_a_row_written_before_the_two_lists_has_its_retiring_ads_go_off_at_once(conn, nb_live, launched):
    """Item 1's migration: a row 5a's code wrote keeps every ad on its way out under one key, `retiring`, and cannot
    say which a finished change replaced -- they are read as left over (OFF at once, whatever their review), and the
    old key goes with the first write."""
    for ad in serving(conn):
        nb_live.approve(ad)
    left = _left_on(conn, nb_live)
    nb_live.approve(left)
    leftover = nb_row(conn)["resources"]["leftover"]
    conn.execute(
        "UPDATE channel_campaign SET resources = (resources - 'leftover') || %s "
        "WHERE business_id = %s AND channel = %s",
        (Jsonb({"retiring": leftover}), BIZ, NB),
    )
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.bodies("ad/updateStatus") == [{"status": "OFF"}] and nb_live.ads[left]["status"] == "OFF"
    resources = nb_row(conn)["resources"]
    assert "retiring" not in resources and "leftover" not in resources


def test_an_update_of_a_row_written_before_the_two_lists_keeps_its_retiring_ads_as_left_over(conn, nb_live, launched):
    left = _left_on(conn, nb_live)
    leftover = nb_row(conn)["resources"]["leftover"]
    conn.execute(
        "UPDATE channel_campaign SET resources = (resources - 'leftover') || %s "
        "WHERE business_id = %s AND channel = %s",
        (Jsonb({"retiring": leftover}), BIZ, NB),
    )
    add_plan(conn, nb_sections(headlines=["Gentle Care in Palo Alto", "Same-Day Crowns in Palo Alto"]))
    assert step(conn)["last_error"] is None
    resources = nb_row(conn)["resources"]
    assert "retiring" not in resources and set(leftover) <= set(resources["leftover"]) and left in resources["leftover"]
    assert unlisted_on(conn, nb_live) == set()


def test_the_first_sync_of_an_account_month_re_records_the_base_and_sets_that_months_cap(
    conn, nb_live, launched, monkeypatch
):
    """R16 as amended by R22: the base recorded at the launch is for its account month (`cap_month`); the first sync of
    the next month (Los Angeles' calendar) records NewsBreak's lifetime total as the new base and caps the account at
    it plus the month's amount -- once, the month's later syncs leave it -- so last month's spend never counts against
    this month's."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 55_000  # October's spend: NewsBreak's lifetime total now
    sync(conn)  # still October: nothing to set
    assert "balance/updateAccountsBudget" not in nb_live.kinds()
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))  # 09:00 on Nov 2 in LA
    nb_live.sent.clear()
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert nb_live.kinds() == [*READS, "balance/getAccountBudgetInfo", "balance/updateAccountsBudget"]
    resources = nb_row(conn)["resources"]
    assert (resources["cap_base_cents"], resources["cap_cents"], resources["cap_month"]) == (55_000, 115_000, "2026-11")
    assert nb_live.caps[account_id] == 115_000 == external(conn)["spending_cap_cents"]
    assert sync_audit(conn) == ["Admiry let NewsBreak spend up to $600.00 this month"]  # item 7: audited
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and len(sync_audit(conn)) == 1


@pytest.mark.parametrize("step_", ["an update", "a resume"])
def test_an_update_or_a_resume_early_in_a_month_caps_from_that_months_base(conn, nb_live, launched, monkeypatch, step_):
    """Fix round 1, item 3: a step that sets the cap in a month whose first read has not run yet -- an update, or a
    resume -- never caps from last month's base (`cap_month` is last month's): it records this month's base (NewsBreak's
    lifetime total now) and caps at it plus the month's amount, so last month's spend never counts against this one."""
    (account_id,) = nb_live.accounts
    if step_ == "a resume":
        ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    nb_live.spent[account_id] = 55_000  # October's spend
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    if step_ == "an update":
        add_plan(conn, nb_sections(monthly_usd=900))
        assert step(conn)["state"] == "live"
        month = 90_000
    else:
        assert ads_run.resume(conn, config.load(), BIZ, NB)["state"] == "live"
        month = 60_000
    resources = nb_row(conn)["resources"]
    assert (resources["cap_base_cents"], resources["cap_month"]) == (55_000, "2026-11")
    assert nb_live.caps[account_id] == 55_000 + month == resources["cap_cents"]


def test_a_new_month_sets_no_cap_for_ads_the_approved_plan_no_longer_funds(conn, nb_live, launched, monkeypatch):
    """Item 4: the month's cap follows the approved plan (spend-guard's month cap's one source): a version that leaves
    NewsBreak out -- approved, its pause not sent yet -- gets no new month's cap; a version that funds it gets its own
    amount, before its update runs."""
    (account_id,) = nb_live.accounts
    add_plan(conn, sections())  # Google Search alone: NewsBreak dropped
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and nb_row(conn)["resources"]["cap_month"] == "2026-10"
    add_plan(conn, nb_sections(monthly_usd=900))  # funded again, at $900
    sync(conn)
    assert nb_live.caps[account_id] == 90_000 and nb_row(conn)["resources"]["cap_month"] == "2026-11"


def test_a_row_that_never_kept_its_caps_month_is_not_set_again_in_the_middle_of_one(
    conn, nb_live, launched, monkeypatch
):
    """A row built before the month was kept (`cap_month`) cannot say which month its base is for: its first read
    records this month and sets nothing -- a base taken mid-month would let the month's amount through once more --
    and the next month's first read sets the cap as usual."""
    ads_run.forget(conn, BIZ, NB, "cap_month")
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    nb_live.sent.clear()
    sync(conn)
    assert nb_live.kinds() == READS and nb_row(conn)["resources"]["cap_month"] == "2026-11"
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 12, 2, 17, tzinfo=UTC))
    sync(conn)
    assert "balance/updateAccountsBudget" in nb_live.kinds() and nb_row(conn)["resources"]["cap_month"] == "2026-12"


def test_a_new_months_cap_from_figures_that_disagree_waits_for_an_operator(
    conn, nb_live, launched, resend, monkeypatch
):
    """R22 keeps 5a's sanity rule: NewsBreak's total above the last cap Admiry set by more than NewsBreak's own
    overshoot sets no new cap; the row says this month's budget is not set and the figures are being checked (its own
    sentence, M1 Task 7: no change of the owner's waits), one operator mail goes, and the month's later syncs try again
    without a second mail -- until an operator clears it, when the next sync sets the cap."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 61_000  # the launch capped it at 60,000
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", nb.MONTH_CHECK)
    assert nb_live.caps[account_id] == 60_000 and nb_row(conn)["resources"]["cap_month"] == "2026-10"
    (mail,) = resend.sent
    assert mail["to"] == ["ops@example.com"] and "61000" in mail["text"]
    sync(conn)
    assert nb_row(conn)["last_error"] == nb.MONTH_CHECK and len(resend.sent) == 1
    cleared = {**external(conn), "spending_cap_cents": 61_000}
    cleared.pop("spend_check_mailed_at")
    registry.save_account(conn, BIZ, NB, mode="managed", status="active", external=cleared)  # the operator's fix
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", None)
    assert nb_live.caps[account_id] == 121_000 and nb_row(conn)["resources"]["cap_month"] == "2026-11"


def test_the_ad_sets_end_date_moves_a_year_out_once_it_is_thirty_days_away(conn, nb_live, launched):
    """R23: NewsBreak stops an ad set at its end date; the sync moves it a year out once it is 30 days away or less,
    with the whole targeting (R4: an ad-set update without it would widen the ads to everyone)."""
    (ad_set,) = nb_live.ad_sets.values()
    now = int(NOW.timestamp())
    ad_set["endTime"] = now + 30 * 86_400 + 1
    sync(conn)
    assert "ad-set/update" not in nb_live.kinds()
    ad_set["endTime"] = now + 30 * 86_400
    nb_live.sent.clear()
    sync(conn)
    ids = nb_row(conn)["resources"]["location_ids"]
    assert nb_live.bodies("ad-set/update") == [
        {"endTime": now + nb.END_DAYS * 86_400, "targeting": nb_build.targeting(ids)}
    ]
    assert ad_set["targeting"]["location"] == {"positive": ids} and ad_set["budget"] == 1973
    nb_live.sent.clear()
    sync(conn)
    assert "ad-set/update" not in nb_live.kinds()


def test_an_ad_set_newsbreak_lists_without_an_end_date_is_reported_once(conn, nb_live, launched, caplog):
    """Fix round 1, item 9: an ad set NewsBreak lists with no end date cannot be moved before it ends -- one warning
    line, never a read that silently moves nothing; the mark goes once it shows one again."""
    (ad_set,) = nb_live.ad_sets.values()
    ad_set.pop("endTime")
    with caplog.at_level(logging.WARNING):
        sync(conn)
        sync(conn)
    assert caplog.text.count("shows no end date") == 1 and nb_row(conn)["resources"]["end_unknown"] is True
    ad_set["endTime"] = int(NOW.timestamp()) + 400 * 86_400
    sync(conn)
    assert "end_unknown" not in nb_row(conn)["resources"]


def test_under_validate_the_sync_reads_and_changes_nothing_at_newsbreak(conn, nb_live, launched, monkeypatch):
    """Every NewsBreak write under `live` only: under `validate` the sync still reads (a read is never a write), keeps
    NewsBreak's review in its own words, and sends nothing -- no ad off, no end date, no month's cap."""
    for ad in serving(conn):
        nb_live.approve(ad)
    left = _left_on(conn, nb_live)
    (ad_set,) = nb_live.ad_sets.values()
    ad_set["endTime"] = int(NOW.timestamp()) + 86_400
    first, _second = serving(conn)
    nb_live.reject(first, TURNED_DOWN)
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    nb_live.sent.clear()
    out = sync(conn)
    assert (out["state"], nb_live.kinds()) == ("limited", READS)
    assert nb_live.ads[left]["status"] == "ON" and len(open_issues(conn)) == 1
    assert nb_row(conn)["resources"]["cap_month"] == "2026-10"


# ---------------------------------------------------------------- the conversions back by click id


def _event(lead: str, click: str | None, kind: str = "lead") -> base.ConversionEvent:
    return base.ConversionEvent(lead_id=lead, kind=kind, at=NOW, click_id=click, value_usd=None)


def test_conversions_are_sent_by_click_id_as_the_lead_event(nbapi):
    account = base.Account(BIZ, NB, "managed", "active", {"ad_account_id": "1000001", "event_id": "77"}, None)
    with ADAPTER.client() as http:
        got = ADAPTER.push_conversions(
            config.load(), account, [_event("l1", "nvss_1"), _event("l2", None), _event("l3", "nvss_3")], http=http
        )
    assert got == base.UploadResult(accepted=2, rejected=1, note=None)
    assert nbapi.conversions == [
        {"callback": "nvss_1", "event_type": "submit_form"},
        {"callback": "nvss_3", "event_type": "submit_form"},
    ]


def test_an_account_without_its_lead_event_sends_nothing(nbapi):
    nbapi.add_account(ACCOUNT_NAME)
    (account_id,) = nbapi.accounts
    account = base.Account(BIZ, NB, "managed", "active", {"ad_account_id": account_id}, None)
    with ADAPTER.client() as http:
        got = ADAPTER.push_conversions(config.load(), account, [_event("l1", "nvss_1")], http=http)
    assert (got.accepted, got.rejected) == (0, 1) and got.note == nb.NO_LEAD_EVENT
    assert nbapi.conversions == [] and nbapi.kinds() == ["event/getList"]
    nbapi.events[account_id] = [{"id": "77", "name": "submit_form", "type": "POSTBACK", "eventType": "submit_form"}]
    with ADAPTER.client() as http:
        got = ADAPTER.push_conversions(config.load(), account, [_event("l1", "nvss_1")], http=http)
    assert (got.accepted, got.rejected) == (1, 0) and nbapi.conversions == [
        {"callback": "nvss_1", "event_type": "submit_form"}
    ]


def test_a_click_newsbreak_no_longer_takes_is_rejected_and_the_others_still_go(nbapi):
    nbapi.rejected_clicks = {"nvss_old"}
    account = base.Account(BIZ, NB, "managed", "active", {"ad_account_id": "1000001", "event_id": "77"}, None)
    with ADAPTER.client() as http:
        got = ADAPTER.push_conversions(
            config.load(), account, [_event("l1", "nvss_old"), _event("l2", "nvss_new")], http=http
        )
    assert (got.accepted, got.rejected) == (1, 1)
    nbapi.down = True
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.push_conversions(config.load(), account, [_event("l3", "nvss_3")], http=http)
    assert (str(e.value), e.value.retryable) == (nb.RETRYING, True)  # the job tries those again


def test_only_leads_are_sent_back(nbapi):
    """P9: the Conversion API sends leads, nothing else (booked/won are M6's)."""
    account = base.Account(BIZ, NB, "managed", "active", {"ad_account_id": "1000001", "event_id": "77"}, None)
    with ADAPTER.client() as http:
        got = ADAPTER.push_conversions(config.load(), account, [_event("l1", "nvss_1", "booked")], http=http)
    assert (got.accepted, got.rejected) == (0, 1) and nbapi.conversions == []


def _stored_account(conn, external_: dict) -> base.Account:
    """The business's NewsBreak account row as the launch leaves it, with `external_`."""
    _business(conn)
    registry.save_account(conn, BIZ, NB, mode="managed", status="active", external=external_)
    return registry.account(conn, BIZ, NB)


LEAD_EVENT = {"id": "77", "name": "submit_form", "type": "POSTBACK", "eventType": "submit_form"}


def test_the_upload_finds_the_hand_made_lead_event_once_and_keeps_its_id_on_the_account(conn, nbapi):
    """T6-3: NewsBreak has no API that makes an event, so an operator makes the lead event by hand; the conversion
    upload looks it up -- one request, paced with the business's others -- and keeps its id on the account row
    (`push_conversions` has no connection to keep it), merged into what the row holds; from then on it is never asked
    for again."""
    account_id = nbapi.add_account(ACCOUNT_NAME)
    nbapi.events[account_id] = [
        {"id": "12", "name": "page_view", "type": "PIXEL", "eventType": "page_view"},
        LEAD_EVENT,
    ]
    held = {"ad_account_id": account_id, "account_status": "ACTIVE", "spending_cap_cents": 60_000}
    account = _stored_account(conn, held)
    with ADAPTER.client() as http:
        ready = ADAPTER.conversion_account(conn, config.load(), account, http=http)
        assert ready.external == {**held, "event_id": "77"} and external(conn) == {**held, "event_id": "77"}
        assert nbapi.kinds() == ["event/getList"] and nbapi.waits == []
        got = ADAPTER.push_conversions(config.load(), ready, [_event("l1", "nvss_1")], http=http)
    assert got.accepted == 1 and nbapi.kinds() == ["event/getList", "tracking/attribute"]
    assert len(nbapi.waits) == 1 and 0.9 < nbapi.waits[0] <= nb.PACE_S  # a second after the lookup started
    nbapi.sent.clear()
    with ADAPTER.client() as http:
        assert ADAPTER.conversion_account(conn, config.load(), registry.account(conn, BIZ, NB), http=http) == (
            registry.account(conn, BIZ, NB)
        )
    assert nbapi.sent == []


def test_an_account_without_an_ad_account_or_the_lead_event_has_nothing_to_send_to(conn, nbapi):
    """No ad account yet: nothing to look up, nothing asked. An ad account whose events hold no lead event (the
    operator has not made it yet; the launch mailed them once): one look, nothing kept -- its leads wait, retried within
    NewsBreak's 7 days."""
    with ADAPTER.client() as http:
        assert ADAPTER.conversion_account(conn, config.load(), _stored_account(conn, {}), http=http) is None
        assert nbapi.sent == []
        account_id = nbapi.add_account(ACCOUNT_NAME)
        nbapi.events[account_id] = [{"id": "12", "name": "page_view", "type": "PIXEL", "eventType": "page_view"}]
        account = _stored_account(conn, {"ad_account_id": account_id})
        assert ADAPTER.conversion_account(conn, config.load(), account, http=http) is None
    assert nbapi.kinds() == ["event/getList"] and external(conn) == {"ad_account_id": account_id}


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [
        (envelope(4033, "Invalid access token"), nb.ACCESS_FIXING_CHANGE),
        (envelope(4034, "Rate limit exceeded"), nb.RETRYING),
    ],
    ids=["the token", "a throttle"],
)
def test_newsbreak_failing_the_lead_event_lookup_reads_in_the_running_ads_words(conn, nbapi, answer, sentence):
    """T6-3: the upload's failures read in the words for ads already running, should an owner ever see one -- never
    "your ads start once it is fixed" -- and are retried an hour later."""
    account_id = nbapi.add_account(ACCOUNT_NAME)
    nbapi.fail["event/getList"] = answer
    account = _stored_account(conn, {"ad_account_id": account_id})
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.conversion_account(conn, config.load(), account, http=http)
    assert (str(e.value), e.value.retryable) == (sentence, True)
    assert external(conn) == {"ad_account_id": account_id}


def test_a_postback_refused_for_admirys_access_reads_in_the_running_ads_words(nbapi):
    """T6-3: `push_conversions` takes the running ads' wording (5a's minor): the token's trouble is "your latest change
    goes out once it is fixed", retried, never the not-started ads' "your ads start once it is fixed"."""
    nbapi.fail["tracking/attribute"] = httpx.Response(403, text="forbidden")
    account = base.Account(BIZ, NB, "managed", "active", {"ad_account_id": "1000001", "event_id": "77"}, None)
    with ADAPTER.client() as http, pytest.raises(base.PlatformError) as e:
        ADAPTER.push_conversions(config.load(), account, [_event("l1", "nvss_1")], http=http)
    assert (str(e.value), e.value.retryable) == (nb.ACCESS_FIXING_CHANGE, True)


def _clears_with_the_ready_command(mail: dict) -> bool:
    """T6-5: an operator mail asking to clear a mark names `agent.ops nb-account-ready` with the business, and carries
    no SQL to run by hand any more (nor the "Task 6 adds it" of the time before the command)."""
    text = mail["text"]
    return (
        f"python -m agent.ops nb-account-ready {BIZ}" in text
        and "UPDATE channel_account" not in text
        and "jsonb" not in text
        and "Task 6" not in text
    )


def test_the_waiting_accounts_operator_mail_clears_with_the_ready_command(conn, nb_live, resend):
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.account_status = "PENDING"
    assert step(conn)["last_error"] == nb.ACCOUNT_WAIT
    (mail,) = resend.sent
    assert _clears_with_the_ready_command(mail)


def test_the_spend_checks_operator_mail_clears_with_the_ready_command(conn, nb_live, launched, resend):
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 61_000  # the launch capped it at 60,000
    add_plan(conn, nb_sections(monthly_usd=900))
    assert step(conn)["last_error"] == nb.SPEND_CHECK
    (mail,) = resend.sent
    assert _clears_with_the_ready_command(mail)


# ---------------------------------------------------------------- R1: NewsBreak's failures as the lifecycle takes them


@pytest.mark.parametrize(
    ("owner", "sentence", "retryable"),
    [
        (newsbreak.BUSY, nb.RETRYING, True),  # fix round 1: Admiry retries; the row never says "try again"
        (newsbreak.UNREACHABLE, nb.RETRYING, True),
        (newsbreak.ACCESS_TROUBLE, nb.ACCESS_FIXING, True),  # fix round 2: the client's "try again later" stays its own
        (newsbreak.REFUSED, newsbreak.REFUSED, False),
        (newsbreak.REFUSED_WITH_REASON.format(41926), newsbreak.REFUSED_WITH_REASON.format(41926), False),
        (newsbreak.ASSET_UNREADABLE, newsbreak.ASSET_UNREADABLE, False),
    ],
)
def test_a_newsbreak_failure_reads_as_the_lifecycle_takes_it(owner, sentence, retryable):
    e = nb.platform_error(newsbreak.NewsBreakError(owner, code=None, status=None))
    assert isinstance(e, base.PlatformError) and (str(e), e.retryable) == (sentence, retryable)
    assert "try again" not in str(e)


def test_access_trouble_on_running_ads_says_the_change_waits():
    e = nb.platform_error(newsbreak.NewsBreakError(newsbreak.ACCESS_TROUBLE), running=True)
    assert (str(e), e.retryable) == (nb.ACCESS_FIXING_CHANGE, True)
    for running in (False, True):  # the others read the same whether the ads run or not
        busy = nb.platform_error(newsbreak.NewsBreakError(newsbreak.BUSY), running=running)
        assert str(busy) == nb.RETRYING


# ---------------------------------------------------------------- never NewsBreak's


def test_there_are_no_leads_to_pull_from_newsbreak():
    """NewsBreak has no lead forms: leads arrive on the business's page (`capabilities().leads` is False)."""
    with pytest.raises(base.Unsupported):
        ADAPTER.pull_leads(None, None, datetime.now(UTC))


# ---------------------------------------------------------------- the client and its meter


def test_the_client_is_newsbreaks_metered_one(nbapi):
    with ADAPTER.client() as http:
        assert ADAPTER.ops_sent(http) == 0
        newsbreak.ad_accounts(config.load(), "org_1", http=http)
        assert ADAPTER.ops_sent(http) == 1


def test_a_client_with_a_time_budget_sends_nothing_once_it_is_spent(nbapi):
    with ADAPTER.client(budget_s=0) as http, pytest.raises(newsbreak.NewsBreakError) as e:
        newsbreak.ad_accounts(config.load(), "org_1", http=http)
    assert e.value.owner == newsbreak.UNREACHABLE and nbapi.requests == [] and ADAPTER.ops_sent(http) == 0
    with ADAPTER.client(budget_s=30) as http:
        newsbreak.ad_accounts(config.load(), "org_1", http=http)
        assert ADAPTER.ops_sent(http) == 1


# ---------------------------------------------------------------- words and secrets


def test_its_owner_words_carry_no_jargon_and_name_newsbreak():
    words = ADAPTER.words
    for sentence in vars(words).values():
        assert sentence is None or _plain(sentence), sentence
    for sentence in (
        nb.ACCOUNT_WAIT,
        nb.FAR_POINT,
        nb.TOO_LITTLE_COPY,
        nb.NO_IMAGE,
        nb.NO_ZIPS,
        nb.NO_LEAD_EVENT,
        nb.RETRYING,
        nb.STARTING,
        nb.SPEND_CHECK,
        nb.ACCESS_FIXING,
        nb.ACCESS_FIXING_CHANGE,
        nb.PAUSED_IN_NEWSBREAK,
    ):
        assert _plain(sentence) and "NewsBreak" in sentence and "try again" not in sentence, sentence
    for sentence in (nb.RETRYING, nb.STARTING):  # Admiry's to finish: the owner has nothing to try again
        assert "within the hour" in sentence
    # the page sentences, now that the ads carry the page (M0 Task 9 D4)
    assert words.refused_address.format(host="brightsmile.example").startswith("NewsBreak did not take the page")
    assert words.retrying == nb.RETRYING
    # no `paused_elsewhere` (5b fix round 1, item 2): NewsBreak's pause is no owner's hand Admiry's pauses leave alone
    assert words.paused_elsewhere is None and "NewsBreak" in words.pause_owed
    assert _plain(nb.MONTH_CHECK) and "NewsBreak" in nb.MONTH_CHECK and "try again" not in nb.MONTH_CHECK


# ---------------------------------------------------------------- NewsBreak's states in its own words (M1 Task 7, A4)


def _line(row: dict) -> tuple[str, str]:
    """A NewsBreak row's owner line (Settings, get_channel_status, the approval's say) and its channels-card sentence."""
    return ads_run.status_sentence(row), state._state_text(row, "NewsBreak", "needs_attention", [])


def _nb_row(state_: str, last_error: str | None, **resources) -> dict:
    return {
        "channel": NB,
        "state": state_,
        "last_error": last_error,
        "paused_reason": None,
        "paused_at": datetime.now(UTC),
        "launched_at": datetime.now(UTC),
        "resources": resources,
    }


def test_an_account_newsbreak_is_getting_ready_reads_as_newsbreaks_work_never_as_the_owners():
    """A managed channel waiting for its account at the platform is Admiry's work (R11): never "waiting for your
    account", which reads as the owner's to do, nor funds -- one NewsBreak sentence (fix round 1, item 2), said once,
    for a row the launch found waiting (`queued`) and one approved while it waits (`waiting_account`): on the line,
    the card and the plan's badge -- for that wait alone (final review Minor 7)."""
    sentence = "NewsBreak is getting your account ready; your ads start once it is"
    for state_ in ("waiting_account", "queued"):
        waiting = _nb_row(state_, nb.ACCOUNT_WAIT)
        assert _line(waiting) == (f"NewsBreak: {sentence}.", sentence), state_
        assert ads_run.execution_line([waiting]) == sentence, state_
        two = [{**waiting, "channel": "google_search", "state": "live", "last_error": None}, waiting]
        assert ads_run.execution_line(two) == f"Google Search: Live since today; NewsBreak: {sentence}"
    # the clause is the account's wait alone (final review Minor 7, as `status_sentence` reads it): a row waiting for
    # another reason reads as M0's, its sentence said after; a queued row's own wait reads as M0's too
    only_managed = _nb_row("waiting_account", nb.ONLY_MANAGED)
    assert _line(only_managed) == (
        f"NewsBreak: waiting for your account. {nb.ONLY_MANAGED}.",
        "Waiting for your account",
    )
    assert (
        ads_run.execution_line([only_managed]) == "Waiting for your account" and sentence not in _line(only_managed)[0]
    )
    assert _line(_nb_row("queued", nb.RETRYING))[0] == f"NewsBreak: your ads are waiting to launch. {nb.RETRYING}."
    assert ads_run.execution_line([_nb_row("queued", nb.RETRYING)]) == f"Waiting to launch: {nb.RETRYING}"
    # a balance still to add is the owner's: said as M0 says it
    assert _line(_nb_row("waiting_account", NO_FUNDS)) == ("NewsBreak: waiting for funds.", "Waiting for funds")
    # Google Search's account is the owner's own: its wait says so still
    assert ads_run.state_clause({"channel": "google_search", "state": "waiting_account", "last_error": None}) == (
        "waiting for your account"
    )


def test_ads_newsbreak_paused_while_admiry_runs_them_never_read_as_live():
    """NewsBreak shows the ads switched off though Admiry runs them (the read's note on a `limited` row): one sentence,
    never "live" beside "paused"; a row NewsBreak holds back for another reason keeps M0's line."""
    paused_there = _nb_row("limited", nb.PAUSED_IN_NEWSBREAK)
    sentence, card = _line(paused_there)
    assert sentence == "NewsBreak: NewsBreak has paused your ads; an Admiry operator is looking into it."
    assert card == "NewsBreak has paused your ads; an Admiry operator is looking into it"
    assert "live" not in sentence.lower() and "live" not in card.lower()
    # the plan's badge too (fix round 1, item 1): in NewsBreak's words, never "Live since" beside the line
    assert ads_run.execution_line([paused_there]) == "Paused at NewsBreak"
    google_live = {**paused_there, "channel": "google_search", "state": "live", "last_error": None}
    assert ads_run.execution_line([google_live, paused_there]) == (
        "Google Search: Live since today; NewsBreak: Paused at NewsBreak"
    )
    assert _line(_nb_row("limited", None))[0] == "NewsBreak: live, but NewsBreak is holding your ads back."
    assert ads_run.execution_line([_nb_row("limited", None)]) == "Live since today"  # held back: it does serve


def test_ads_waiting_for_a_picture_say_admiry_owes_one_never_the_owner():
    """Fix round 1, item 3 (the controller's ruling): a plan funds NewsBreak only with a picture for the trade, so a row
    waits for one only when the picture approved with the plan left Admiry's set -- nothing the owner can supply. The
    line, the card and the plan's badge say so in NewsBreak's words (`Words.image_wait`), and what moves it on -- a
    new version once Admiry has a picture, approved (final review I3: nothing puts a new one on the approved plan's
    card by itself); a channel without such words keeps M0's."""
    sentence = (
        "NewsBreak is waiting for a new picture from Admiry; ask for a new version of your plan once Admiry has one, "
        "and approve it"
    )
    assert nb.IMAGE_WAIT == ADAPTER.words.image_wait == sentence
    waiting = _nb_row("waiting_creative", None)
    assert _line(waiting) == (f"NewsBreak: {sentence}.", sentence)
    assert ads_run.execution_line([waiting]) == sentence
    assert "your image" not in _line(waiting)[0] and _plain(sentence)
    google = {**waiting, "channel": "google_search"}
    assert (ads_run.state_clause(google), ads_run.execution_line([google])) == (
        "waiting for your image",
        "Waiting for your image",
    )


def test_ads_newsbreak_may_still_show_while_admiry_shows_them_paused_say_admiry_is_pausing_them():
    """The pause owed to NewsBreak -- not sent yet, or NewsBreak shows the ads ON again (5b's repause) -- is sent by
    the hourly step itself: the line promises no operator's hand, and never says "live"."""
    owed = _nb_row("paused", ADAPTER.words.pause_owed, owner_paused=True, pause_owed=True)
    assert ads_run.status_sentence(owed) == (
        "NewsBreak: paused by you today. Your ads may still show on NewsBreak; Admiry is pausing them there."
    )
    assert "operator" not in ADAPTER.words.pause_owed and "live" not in ADAPTER.words.pause_owed.lower()


def test_a_months_budget_admiry_could_not_set_at_newsbreak_reads_as_its_own_not_as_a_change():
    """The month's cap the read could not set (figures that disagree, R22) is no change of the owner's: its own
    sentence, never M0's "your latest change is not all in yet" -- which an update's own spend check still reads."""
    held = _nb_row("live", nb.MONTH_CHECK)
    sentence = (
        "NewsBreak: live. This month's budget is not set at NewsBreak yet and your ads may stop early; an Admiry "
        "operator is checking the spend figures first."
    )
    assert _line(held) == (sentence, "Live")
    assert _line(_nb_row("live", nb.SPEND_CHECK))[0].startswith(
        "NewsBreak: live, but your latest change is not all in yet."
    )


def test_the_token_never_reaches_resources_the_account_the_audit_or_the_log(conn, nb_live, resend, caplog):
    v1 = seed(conn)
    queue(conn, v1)
    with caplog.at_level(logging.DEBUG):
        assert step(conn)["state"] == "live"
        add_plan(conn, nb_sections(monthly_usd=900, headlines=NEW_HEADLINES))
        step(conn)
        ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
        nb_live.fail["ad-set/create"] = envelope(4033, "Invalid access token nbtok_SECRET")
        seed(conn, biz="biz_2")
        queue(conn, ads_run.approved(conn, "biz_2")["id"], biz="biz_2")
        ads_run.launch(conn, config.load(), "biz_2", NB)
    stored = [
        json.dumps(nb_row(conn), default=str),
        json.dumps(nb_row(conn, "biz_2"), default=str),
        json.dumps(external(conn)),
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM agent_action").fetchall()], default=str),
        json.dumps(resend.sent),
        caplog.text,
    ]
    for secret in ADS_SECRETS:
        for text in stored:
            assert secret not in text
