"""A3c-1, managed Google (plan 2026-10-03-agent-a3c-1-managed-google; founder decision 2026-10-03): the pool of
Google Ads accounts Admiry runs prepaid businesses' Google Search in, filed by industry (root, a family, a family's
trade); the allocator a launch calls (the trade's account, the family's, else root; capacity, closed, the fewest live
first); a business bound to exactly one account at a time with its history kept; several businesses' ads in one
account, each addressing only its own; the operator's commands; and the balance's guard over the spend Google reports
before wallet-settle posts it."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import (
    ads_mode,
    ads_run,
    config,
    google_ads_link,
    google_managed,
    jobs,
    ops,
    plans_channels,
    tick,
    tools_account,
    tools_consult,
    wallet,
)
from agent.channels import registry
from agent.channels.google_search import adapter as google
from agent.channels.google_search import client as google_ads
from agent.tests.conftest import ADS_ENV, TEST_DB_URL
from agent.tests.test_ads_run import (  # noqa: F401 - clock, gads and live are fixtures
    BIZ,
    CID,
    GOOGLE,
    KNOWLEDGE,
    clock,
    gads,
    live,
    plain,
    queued,
    sections,
    seed,
)
from agent.tests.test_ops_ads_probe import fake_transport

ROOT_CID = CID  # the fake Google names every resource in this account (test_ads_run.FakeGoogle)
FAMILY_CID = "2222222222"
TRADE_CID = "3333333333"


def _pool(conn, cid: str, scope: str, *, capacity: int | None = None, status: str = "open", at: str | None = None):
    conn.execute(
        "INSERT INTO google_ads_pool (customer_id, scope, capacity, status, added_by, added_at) "
        "VALUES (%s, %s, %s, %s, 'ops:cli', coalesce(%s::timestamptz, now()))",
        (cid, scope, capacity, status, at),
    )


def _prepaid(conn, biz: str = BIZ, *, knowledge: dict | None = None, funds: int = 50_000, **kw) -> str:
    """A verified business that prepays (no link of its own; Google Search's account row managed, P6) with a balance,
    an approved plan funding Google Search, a published page and the conversion setup in ROOT_CID. Returns the
    plan's id."""
    _key, plan_id = seed(conn, biz=biz, link=None, knowledge=knowledge, **kw)
    ads_mode.change(conn, biz, "managed")
    if funds:
        wallet.add_entry(conn, biz, kind="topup", amount_cents=funds, ref=f"pi_{biz}", created_by="stripe")
    return plan_id


def _trade(trade: str | None, category: str = "dentists") -> dict:
    return {**KNOWLEDGE, "category": category, **({"trade": trade} if trade else {})}


def test_the_pool_module_imports_before_anything_else():
    """Any entry point may import the pool first (a script, an ops command): the Google adapter, which the import pulls
    in through `agent.channels`, must not read google_managed's names while google_managed is half loaded."""
    root = Path(__file__).parents[2]
    for module in ("agent.google_managed", "agent.wallet", "agent.geo"):
        run = [sys.executable, "-c", f"import {module}"]
        done = subprocess.run(run, cwd=root, capture_output=True, text=True, check=False)
        assert done.returncode == 0, (module, done.stderr[-800:])


# ---------------------------------------------------------------- scopes and industries


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("root", "root"),
        ("attorneys", "attorneys"),
        ("fallback", "fallback"),
        ("attorneys/personal_injury", "attorneys/personal_injury"),
        ("account_attorneys_personal_injury", "attorneys/personal_injury"),  # as the account is named in Google Ads
        ("Home_Services_Plumbing", "home_services/plumbing"),
        ("account_root", "root"),
    ],
)
def test_a_scope_is_root_a_family_or_a_familys_trade_in_either_spelling(text, scope):
    assert google_managed.normalize_scope(text) == scope
    assert google_managed.group(scope) == "account_" + scope.replace("/", "_")


@pytest.mark.parametrize("text", ["", "lawyers", "attorneys/plumbing", "dentists/", "home_services_plumber", "a/b/c"])
def test_a_scope_outside_the_families_and_their_trades_is_refused(text):
    with pytest.raises(google_managed.PoolRefused):
        google_managed.normalize_scope(text)


@pytest.mark.parametrize(
    ("knowledge", "industry"),
    [
        (_trade(None), ("dentists", None)),
        (_trade("dentists/orthodontics_invisalign"), ("dentists", "orthodontics_invisalign")),
        (_trade("orthodontics_invisalign"), ("dentists", "orthodontics_invisalign")),
        (_trade("plumbing"), ("dentists", None)),  # not one of the family's trades
        (_trade(None, category="bakeries"), ("fallback", None)),  # not a family: fallback
    ],
)
def test_the_industry_is_the_knowledges_family_and_one_of_its_trades(conn, knowledge, industry):
    _prepaid(conn, knowledge=knowledge)
    assert google_managed.industry(conn, BIZ) == industry
    family, trade = industry
    assert google_managed.candidates(family, trade) == ([f"{family}/{trade}"] if trade else []) + [family, "root"]


# ---------------------------------------------------------------- the pool


def test_add_reads_back_and_refuses_the_manager_an_owners_account_and_a_second_add(conn, monkeypatch):
    for name, value in ADS_ENV.items():
        monkeypatch.setenv(name, value)
    settings = config.load()
    seed(conn, biz="biz_own", cid="4444444444")  # an owner's own linked account
    added = google_managed.add(
        conn,
        settings,
        ROOT_CID,
        "account_root",
        capacity=None,
        by="ops:cli",
        found={"descriptive_name": "account_root", "time_zone": "America/Los_Angeles"},
    )
    assert (added.scope, added.capacity, added.status, added.live, added.has_room) == ("root", None, "open", 0, True)
    assert (added.descriptive_name, added.time_zone, added.group) == (
        "account_root",
        "America/Los_Angeles",
        "account_root",
    )
    for cid, sentence in (
        ("1112223333", google_managed.IS_MANAGER.format(cid="111-222-3333")),
        ("4444444444", google_managed.OWNERS_OWN.format(cid="444-444-4444")),
        (ROOT_CID, google_managed.IN_POOL.format(cid="123-456-7890", scope="root")),
    ):
        with pytest.raises(google_managed.PoolRefused) as e:
            google_managed.add(conn, settings, cid, "dentists", capacity=1, by="ops:cli")
        assert str(e.value) == sentence
    for bad in (0, -1, True):
        with pytest.raises(google_managed.PoolRefused, match="capacity"):
            google_managed.add(conn, settings, FAMILY_CID, "dentists", capacity=bad, by="ops:cli")
    assert [a.customer_id for a in google_managed.pool(conn)] == [ROOT_CID]
    # the other way round (A3c-1 review): a pool account is never an owner's link -- an operator's manual one included
    # -- or that business's ads would run on Admiry's card, never taken from a balance
    with pytest.raises(google_ads_link.LinkRefused) as e:
        google_ads_link.save(conn, "biz_own", ROOT_CID, status="manual")
    assert str(e.value) == google_ads_link.ALREADY_LINKED
    assert google_ads_link.get(conn, "biz_own").customer_id == "4444444444"


# ---------------------------------------------------------------- the managed floor and the first payment


def test_google_searchs_floor_is_200_when_admiry_runs_it_and_100_on_the_owners_own_account(conn):
    """Founder 2026-10-03: Admiry runs Google Search from a balance only from $200 a month (the cold start, spec
    2026-10-03 §3.2); the owner's own account keeps $100 -- on the plan's split, the launch's check and the owner's own
    amount for the channel alike."""
    caps = google.GoogleSearchAdapter().capabilities()
    assert (caps.floor("managed"), caps.floor("own_account"), caps.floor(None)) == (200, 100, 100)
    floors = {f.channel: f.min_monthly_usd for f in plans_channels.facts(conn, "dentists", modes={GOOGLE: "managed"})}
    assert floors[GOOGLE] == 200
    assert {f.channel: f.min_monthly_usd for f in plans_channels.facts(conn, "dentists")}[GOOGLE] == 100
    _prepaid(conn)
    plan = {"id": "pl_x", "sections": sections(monthly_usd=150.0)}
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, plan)
    assert str(e.value) == ads_run.UNDER_FLOOR.format(label="Google Search", floor="$200")
    with pytest.raises(ToolError, match=r"Google Search needs at least \$200 a month"):
        tools_consult._pin(conn, BIZ, GOOGLE, 150)
    seed(conn, biz="biz_own", cid="5555555555")  # its own linked account: $200 is above $100
    ads_run.precheck(conn, config.load(), "biz_own", {"id": "pl_y", "sections": sections(monthly_usd=200.0)})
    assert tools_consult._pin(conn, "biz_own", GOOGLE, 200) == 200


def test_no_google_ads_account_for_a_business_that_never_paid(conn):
    """Founder 2026-10-03: Admiry gives a business a Google Ads account of its own only once the business paid -- a
    card top-up credited; an operator's credit is no payment. The account check says the first payment comes first,
    the allocator and an operator's bind (even forced) refuse, and `waiting` lists only businesses that paid."""
    _pool(conn, ROOT_CID, "root")
    _prepaid(conn, funds=0)
    wallet.add_entry(conn, BIZ, kind="adjustment", amount_cents=50_000, ref="ops_credit", created_by="ops")
    status = google.GoogleSearchAdapter().check_account(conn, config.load(), registry.account(conn, BIZ, GOOGLE))
    assert (status.ready, status.reason) == (False, google.FIRST_PAYMENT) and plain(google.FIRST_PAYMENT)
    with pytest.raises(google_managed.PoolRefused) as e:
        google_managed.assign(conn, BIZ)
    assert str(e.value) == google_managed.NOT_PAID.format(biz=BIZ)
    with pytest.raises(google_managed.PoolRefused, match="has not paid"):
        google_managed.bind(conn, config.load(), BIZ, ROOT_CID, by="ops:cli", force=True)
    assert google_managed.waiting(conn) == []
    assert conn.execute("SELECT count(*) AS n FROM google_ads_binding").fetchone()["n"] == 0
    wallet.add_entry(conn, BIZ, kind="topup", amount_cents=30_000, ref="pi_first", created_by="stripe")
    assert [w["business_id"] for w in google_managed.waiting(conn)] == [BIZ]
    assert google.GoogleSearchAdapter().check_account(conn, config.load(), registry.account(conn, BIZ, GOOGLE)).ready
    assert google_managed.assign(conn, BIZ)[0].customer_id == ROOT_CID


# ---------------------------------------------------------------- competitors in a shared account (spec cold start §3.3)

PALO_ALTO = (37.4419, -122.143)
MENLO_PARK = (37.453, -122.1817)  # about 2.3 miles from Palo Alto
SAN_JOSE = (37.3382, -121.8863)  # about 16 miles from Palo Alto
# five cities far enough apart that no two 5-mile dentists in them compete: tests of something else share accounts
FAR_APART = [PALO_ALTO, (38.5816, -121.4944), (36.7378, -119.7871), (34.0522, -118.2437), (32.7157, -117.1611)]


def _fp(biz="biz_x", family="dentists", trade=None, point=PALO_ALTO, radius=5.0, names=()):
    return google_managed.Footprint(biz, family, trade, point, radius, frozenset(names))


@pytest.mark.parametrize(
    ("a", "b", "rivals"),
    [
        (_fp("a"), _fp("b", point=MENLO_PARK), True),  # two dentists 2 miles apart, 5-mile circles
        (_fp("a"), _fp("b", point=SAN_JOSE), False),  # 16 miles apart: 5 + 5 do not reach
        (_fp("a", radius=10.0), _fp("b", point=SAN_JOSE, radius=10.0), True),  # 10 + 10 do
        (_fp("a"), _fp("b", family="home_services", point=MENLO_PARK), False),  # a dentist and a plumber
        (_fp("a", family="fallback"), _fp("b", family="fallback", point=MENLO_PARK), False),  # no industry
        (_fp("a", trade="orthodontics_invisalign"), _fp("b", trade="pediatric_dentistry", point=MENLO_PARK), False),
        (_fp("a", trade="orthodontics_invisalign"), _fp("b", point=MENLO_PARK), True),  # a trade on one side only
        (_fp("a", point=None, names={"palo alto"}), _fp("b", point=None, names={"palo alto", "menlo park"}), True),
        (_fp("a", point=None, names={"palo alto"}), _fp("b", point=None, names={"san jose"}), False),
        (_fp("a"), _fp("b", point=None, names={"palo alto"}), False),  # a point against names: not comparable
        (_fp("a"), _fp("b", point=MENLO_PARK, radius=None), False),  # no radius: not comparable
        (_fp("a"), _fp("a", point=MENLO_PARK), False),  # the business itself
    ],
)
def test_two_businesses_compete_in_one_industry_with_overlapping_areas(a, b, rivals):
    assert google_managed.competes(a, b) is rivals and google_managed.competes(b, a) is rivals


def test_a_footprint_reads_the_listing_point_the_approved_radius_and_the_area_names(conn):
    knowledge = {**KNOWLEDGE, "service_area": [" Palo Alto ", "MENLO PARK", "", 7]}
    _prepaid(conn, knowledge=knowledge, secs=sections(radius_miles=8.0))
    fp = google_managed.footprint(conn, BIZ)
    assert (fp.business_id, fp.family, fp.trade, fp.point, fp.radius_miles, fp.names) == (
        BIZ,
        "dentists",
        None,
        PALO_ALTO,
        8.0,
        frozenset({"palo alto", "menlo park"}),
    )
    as_bound = google_managed.footprint(conn, BIZ, family="attorneys", trade="family_law")  # the binding's own
    assert (as_bound.family, as_bound.trade) == ("attorneys", "family_law")


@pytest.mark.parametrize(
    ("listing", "point"),
    [
        ({**KNOWLEDGE["listing"], "lat": "37.4419", "lng": "-122.143"}, PALO_ALTO),  # numbers stored as text
        (None, None),
        ({}, None),
        ({"lat": "", "lng": ""}, None),
        ({"lat": "abc", "lng": -122.1}, None),
        ({"lat": 95.0, "lng": -122.1}, None),  # off the Earth
        ({"lat": "nan", "lng": 1.0}, None),
    ],
)
def test_a_listing_without_a_usable_point_has_none_and_never_raises(conn, listing, point):
    _prepaid(conn, knowledge={**KNOWLEDGE, "listing": listing})
    assert google_managed.footprint(conn, BIZ).point == point


@pytest.mark.parametrize(
    ("service_area", "names"),
    [
        ("Palo Alto", {"palo alto"}),  # one text is one area, never its letters (profile._listed's rule)
        (94301, set()),
        (25.0, set()),
        (True, set()),
        ({"city": "Palo Alto"}, set()),
        (None, set()),
    ],
)
def test_a_footprint_reads_any_stored_service_area_as_whole_names_and_never_raises(conn, service_area, names):
    _prepaid(conn, knowledge={**KNOWLEDGE, "service_area": service_area})
    assert google_managed.footprint(conn, BIZ).names == frozenset(names)


def test_an_odd_tenant_never_stops_the_allocator_or_waiting(conn):
    """Final review I1 and the Review Focus: a live tenant of root whose stored area is no list, or whose approved plan
    no longer gives a radius, never crashes the allocator or `waiting` and never keeps a newcomer out."""
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO)
    _dentist(conn, "biz_c", SAN_JOSE)
    google_managed.assign(conn, "biz_a")
    google_managed.assign(conn, "biz_c")
    conn.execute("UPDATE knowledge SET json = jsonb_set(json, '{service_area}', '94301') WHERE business_id = 'biz_a'")
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,consultation,geo,radius_miles}', 'null') "
        "WHERE business_id = 'biz_c'"
    )
    _prepaid(conn, "biz_p", knowledge={**KNOWLEDGE, "category": "home_services"})
    _dentist(conn, "biz_far", FAR_APART[3])  # Los Angeles
    assert sorted(w["business_id"] for w in google_managed.waiting(conn)) == ["biz_far", "biz_p"]
    assert google_managed.assign(conn, "biz_p")[0].customer_id == ROOT_CID
    assert google_managed.assign(conn, "biz_far")[0].customer_id == ROOT_CID


def test_a_footprint_without_an_approved_radius_is_not_comparable(conn):
    _prepaid(conn, secs=sections(radius_miles=None))
    mine = google_managed.footprint(conn, BIZ)
    assert mine.radius_miles is None and not google_managed.competes(mine, _fp("b", point=MENLO_PARK))


def _dentist(conn, biz: str, point: tuple[float, float], *, radius: float = 5.0) -> str:
    """A paying prepaid dentist whose listing is at `point`, with a `radius`-mile plan; returns the plan's id."""
    listing = {**KNOWLEDGE["listing"], "lat": point[0], "lng": point[1]}
    return _prepaid(conn, biz, knowledge={**KNOWLEDGE, "listing": listing}, secs=sections(radius_miles=radius))


RIVAL_A = "account_root 123-456-7890 already runs biz_a, which competes with it (dentists, areas overlap)"


@pytest.mark.parametrize(
    ("a", "b", "why"),
    [
        (_fp("a"), _fp("b", point=None, names={"palo alto"}), "a point against area names"),
        (_fp("a"), _fp("b", point=MENLO_PARK, radius=None), "no radius"),
        (_fp("a"), _fp("b", point=SAN_JOSE), None),  # compared: they do not compete
        (_fp("a"), _fp("b", family="home_services", point=None), None),  # another industry: nothing to compare
        (_fp("a", family="fallback"), _fp("b", family="fallback", point=None), None),  # no industry
    ],
)
def test_two_businesses_of_one_industry_that_cannot_be_compared_say_why(a, b, why):
    assert google_managed.unjudged(a, b) == why and google_managed.unjudged(b, a) == why


NOT_COMPARED_A = "not compared with biz_a (dentists): a point against area names"


def test_a_business_that_cannot_be_compared_with_a_tenant_joins_with_a_note_saying_so(conn, capsys):
    """Final review I2 (spec 2026-10-03 cold start §3.3): two businesses of one industry whose areas cannot be compared
    do not compete, and the binding's note says so, so the operator can move one later; `show` and `list --businesses`
    print it."""
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO, radius=10.0)
    google_managed.assign(conn, "biz_a")
    _prepaid(conn, "biz_b", knowledge={**KNOWLEDGE, "listing": None, "service_area": ["Palo Alto"]})  # names only
    bound, made = google_managed.assign(conn, "biz_b")
    assert (made, bound.customer_id, bound.note) == (True, ROOT_CID, NOT_COMPARED_A)
    assert google_managed.get(conn, "biz_a").note is None  # the first one in had nobody to be compared with
    assert ops.main(["google-account", "show", "biz_b"]) == 0
    assert NOT_COMPARED_A in capsys.readouterr().out
    assert ops.main(["google-account", "list", "--businesses"]) == 0
    assert NOT_COMPARED_A in capsys.readouterr().out


def test_an_operators_bind_keeps_its_own_note_and_adds_the_comparison_it_could_not_make(conn):
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO)
    google_managed.assign(conn, "biz_a")
    _prepaid(conn, "biz_b", knowledge={**KNOWLEDGE, "listing": None, "service_area": ["Palo Alto"]})
    bound = google_managed.bind(conn, config.load(), "biz_b", ROOT_CID, by="ops:cli", note="E2E")
    assert bound.note == f"E2E; {NOT_COMPARED_A}"


def test_a_competitor_in_the_shared_root_sends_the_next_one_to_wait(conn):
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO)
    _dentist(conn, "biz_b", MENLO_PARK)
    _prepaid(conn, "biz_p", knowledge={**KNOWLEDGE, "category": "home_services"})  # a plumber at the same address
    _dentist(conn, "biz_s", SAN_JOSE)
    assert google_managed.assign(conn, "biz_a")[0].customer_id == ROOT_CID
    assert google_managed.assign(conn, "biz_b") == (None, False)
    assert google_managed.assign(conn, "biz_p")[0].customer_id == ROOT_CID  # another industry
    assert google_managed.assign(conn, "biz_s")[0].customer_id == ROOT_CID  # a dentist 16 miles away
    assert google_managed.reasons(conn, "biz_b") == [RIVAL_A]
    waiting = {w["business_id"]: w for w in google_managed.waiting(conn)}
    assert list(waiting) == ["biz_b"] and (waiting["biz_b"]["room"], waiting["biz_b"]["reasons"]) == (False, [RIVAL_A])


def test_a_business_released_from_root_no_longer_keeps_a_competitor_out(conn):
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO)
    _dentist(conn, "biz_b", MENLO_PARK)
    google_managed.assign(conn, "biz_a")
    google_managed.release(conn, "biz_a")
    assert google_managed.assign(conn, "biz_b")[0].customer_id == ROOT_CID


def test_why_no_account_takes_a_business_full_closed_or_none_yet(conn):
    _dentist(conn, "biz_a", PALO_ALTO)
    assert google_managed.reasons(conn, "biz_a") == [google_managed.NO_SCOPE_ACCOUNT]
    _pool(conn, ROOT_CID, "root", capacity=1)
    _prepaid(conn, "biz_p", knowledge={**KNOWLEDGE, "category": "home_services"})
    google_managed.assign(conn, "biz_p")
    assert google_managed.reasons(conn, "biz_a") == ["account_root 123-456-7890 is full (1 of 1)"]
    google_managed.set_status(conn, ROOT_CID, "closed")
    assert google_managed.reasons(conn, "biz_a") == ["account_root 123-456-7890 is closed to new businesses"]
    google_managed.set_status(conn, ROOT_CID, "open")
    google_managed.set_capacity(conn, ROOT_CID, 2)
    assert google_managed.reasons(conn, "biz_a") == []  # room: the next launch step binds it


def test_an_operator_binds_a_competitor_into_a_shared_account_only_with_force(conn):
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO)
    _dentist(conn, "biz_b", MENLO_PARK)
    google_managed.assign(conn, "biz_a")
    with pytest.raises(google_managed.PoolRefused) as e:
        google_managed.bind(conn, config.load(), "biz_b", ROOT_CID, by="ops:cli")
    assert str(e.value) == (
        "business biz_b competes with biz_a in 123-456-7890 (dentists, areas overlap); pass --force to bind all the same"
    )
    assert google_managed.bind(conn, config.load(), "biz_b", ROOT_CID, by="ops:cli", force=True).customer_id == ROOT_CID


def test_the_no_room_mail_and_waiting_name_the_competitor_that_keeps_a_business_out_of_root(
    conn,
    capsys,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    monkeypatch.setenv("AGENT_OPS_EMAIL", "ops@admiry.ai")
    mails: list[dict] = []
    monkeypatch.setattr(google.mail_out, "send_plain", lambda settings, **kw: mails.append(kw) or True)
    _pool(conn, ROOT_CID, "root", capacity=20)
    _dentist(conn, "biz_a", PALO_ALTO)
    google_managed.assign(conn, "biz_a")
    queued(conn, _dentist(conn, "biz_b", MENLO_PARK), biz="biz_b")
    out = ads_run.launch(conn, config.load(), "biz_b", GOOGLE)
    assert out["state"] == "queued" and ads_run.get(conn, "biz_b", GOOGLE)["last_error"] == google.ACCOUNT_OPENING
    (mail,) = mails
    assert f"Why: {RIVAL_A}." in " ".join(mail["paragraphs"])
    assert ops.main(["google-account", "waiting"]) == 0
    printed = capsys.readouterr().out
    assert "biz_b since" in printed and f"no room: {RIVAL_A}" in printed


# ---------------------------------------------------------------- the allocator


def test_the_allocator_takes_the_trades_account_then_the_familys_then_root(conn):
    _pool(conn, ROOT_CID, "root")
    _pool(conn, FAMILY_CID, "dentists")
    _pool(conn, TRADE_CID, "dentists/orthodontics_invisalign")
    _prepaid(conn, "biz_t", knowledge=_trade("orthodontics_invisalign"))
    _prepaid(conn, "biz_f", knowledge=_trade(None))
    _prepaid(conn, "biz_r", knowledge=_trade(None, category="attorneys"))
    picked = {biz: google_managed.assign(conn, biz) for biz in ("biz_t", "biz_f", "biz_r")}
    assert {biz: (b.customer_id, made) for biz, (b, made) in picked.items()} == {
        "biz_t": (TRADE_CID, True),
        "biz_f": (FAMILY_CID, True),
        "biz_r": (ROOT_CID, True),  # no attorneys' account yet: root
    }
    b = picked["biz_t"][0]
    assert (b.family, b.trade, b.bound_by, b.released_at) == (
        "dentists",
        "orthodontics_invisalign",
        "job:allocator",
        None,
    )
    # one account per business, kept: asking again binds nothing new
    again, made = google_managed.assign(conn, "biz_t")
    assert (again, made) == (b, False)
    assert conn.execute("SELECT count(*) AS n FROM google_ads_binding").fetchone()["n"] == 3


def test_the_allocator_skips_closed_and_full_accounts_and_fills_the_emptiest_first(conn):
    _pool(conn, "5000000001", "dentists", capacity=1, at="2026-10-01")
    _pool(conn, "5000000002", "dentists", at="2026-10-02")
    _pool(conn, "5000000003", "dentists", at="2026-10-03")
    _pool(conn, "5000000004", "dentists", status="closed", at="2026-09-30")
    for i, point in enumerate(FAR_APART, 1):  # no two of them compete (spec 2026-10-03 cold start §3.3)
        _dentist(conn, f"biz_{i}", point)
    got = [google_managed.assign(conn, f"biz_{i}")[0].customer_id for i in range(1, 6)]
    # the earliest added first; then whichever open one runs the fewest; the full and the closed ones never
    assert got == ["5000000001", "5000000002", "5000000003", "5000000002", "5000000003"]
    running = {a.customer_id: a.live for a in google_managed.pool(conn)}
    assert running == {"5000000001": 1, "5000000002": 2, "5000000003": 2, "5000000004": 0}


def test_the_allocator_binds_nothing_without_room_and_nothing_for_a_business_that_does_not_prepay(conn):
    _pool(conn, ROOT_CID, "root", capacity=1)
    _prepaid(conn, "biz_a")
    _prepaid(conn, "biz_b")
    assert google_managed.assign(conn, "biz_a")[1] is True
    assert google_managed.assign(conn, "biz_b") == (None, False)  # root is full
    seed(conn, biz="biz_own", cid="4444444444")
    with pytest.raises(google_managed.PoolRefused, match="its own Google Ads account"):
        google_managed.assign(conn, "biz_own")


# ---------------------------------------------------------------- an operator's binding, the release, the history


def test_bind_refuses_what_the_policy_and_the_built_ads_rule_out_and_force_fills_a_full_account(conn, ads_api):
    _pool(conn, ROOT_CID, "root", capacity=1)
    _pool(conn, FAMILY_CID, "dentists", status="closed")
    _prepaid(conn, "biz_a")
    _prepaid(conn, "biz_b")
    google_managed.bind(conn, ads_api, "biz_a", ROOT_CID, by="ops:cli")
    with pytest.raises(google_managed.PoolRefused, match="already runs in"):
        google_managed.bind(conn, ads_api, "biz_a", ROOT_CID, by="ops:cli")
    with pytest.raises(google_managed.PoolRefused, match="release that one first"):
        google_managed.bind(conn, ads_api, "biz_a", FAMILY_CID, by="ops:cli", force=True)
    with pytest.raises(google_managed.PoolRefused, match="is full"):
        google_managed.bind(conn, ads_api, "biz_b", ROOT_CID, by="ops:cli")
    with pytest.raises(google_managed.PoolRefused, match="is closed"):
        google_managed.bind(conn, ads_api, "biz_b", FAMILY_CID, by="ops:cli")
    with pytest.raises(google_managed.PoolRefused, match="not in the pool"):
        google_managed.bind(conn, ads_api, "biz_b", "9999999999", by="ops:cli")
    conn.execute(  # ads built in another account stay there
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) VALUES ('biz_b', %s, 'pl_x', "
        "'live', %s)",
        (GOOGLE, Jsonb({"campaign": "customers/7777777777/campaigns/1", "customer_id": "7777777777"})),
    )
    with pytest.raises(google_managed.PoolRefused, match="built in 777-777-7777"):
        google_managed.bind(conn, ads_api, "biz_b", ROOT_CID, by="ops:cli", force=True)
    conn.execute("DELETE FROM channel_campaign")
    forced = google_managed.bind(conn, ads_api, "biz_b", ROOT_CID, by="ops:cli", force=True, note="E2E")
    assert (forced.customer_id, forced.bound_by, forced.note) == (ROOT_CID, "ops:cli", "E2E")
    assert google_managed.account(conn, ROOT_CID).live == 2


def test_the_binding_is_mirrored_and_a_release_keeps_the_history(conn, ads_api):
    _pool(conn, ROOT_CID, "root")
    _pool(conn, FAMILY_CID, "dentists")
    _prepaid(conn)
    assert (registry.account(conn, BIZ, GOOGLE).mode, registry.account(conn, BIZ, GOOGLE).status) == ("managed", "none")
    first, _ = google_managed.assign(conn, BIZ)
    account = registry.account(conn, BIZ, GOOGLE)
    assert (account.mode, account.status, account.external) == ("managed", "active", {"customer_id": first.customer_id})
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES (%s, %s, 'pl_x', 'live', %s)",
        (
            BIZ,
            GOOGLE,
            Jsonb({"campaign": f"customers/{first.customer_id}/campaigns/1", "customer_id": first.customer_id}),
        ),
    )
    with pytest.raises(google_managed.PoolRefused, match="pass --force"):
        google_managed.release(conn, BIZ)
    released = google_managed.release(conn, BIZ, force=True)
    assert released.released_at is not None and google_managed.get(conn, BIZ) is None
    assert (registry.account(conn, BIZ, GOOGLE).status, registry.account(conn, BIZ, GOOGLE).external) == ("none", {})
    with pytest.raises(google_managed.PoolRefused, match="no Google Ads account"):
        google_managed.release(conn, BIZ)
    conn.execute("DELETE FROM channel_campaign")
    second = google_managed.bind(conn, ads_api, BIZ, FAMILY_CID, by="ops:cli")
    assert [(b.customer_id, b.released_at is None) for b in google_managed.history(conn, BIZ)] == [
        (first.customer_id, False),
        (second.customer_id, True),
    ]
    assert google_managed.first_bound(conn, BIZ) == first.bound_at


def test_waiting_lists_prepaid_businesses_whose_google_search_has_no_account(conn):
    _prepaid(conn, "biz_w", knowledge=_trade("general_dentistry"))
    _prepaid(conn, "biz_b", knowledge=_trade(None, category="home_services"))  # not biz_w's competitor
    seed(conn, biz="biz_own", cid="4444444444")  # its own account: not Admiry's to open
    _prepaid(conn, "biz_draft", status="draft")  # nothing approved yet
    _pool(conn, ROOT_CID, "root", capacity=1)
    google_managed.assign(conn, "biz_b")
    (w,) = google_managed.waiting(conn)
    assert (w["business_id"], w["scopes"], w["room"]) == (
        "biz_w",
        ["dentists/general_dentistry", "dentists", "root"],
        False,
    )
    google_managed.set_capacity(conn, ROOT_CID, None)
    assert google_managed.waiting(conn)[0]["room"] is True


# ---------------------------------------------------------------- the launch


def test_a_prepaid_launch_picks_a_pool_account_and_builds_there(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
):
    _pool(conn, ROOT_CID, "root")
    plan_id = _prepaid(conn)
    queued(conn, plan_id)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live", out
    bound = google_managed.get(conn, BIZ)
    assert (bound.customer_id, bound.bound_by) == (ROOT_CID, "job:allocator")
    accounts = {q.url.path.split("/")[3] for q in gads.requests if q.url.path.startswith("/v25/customers/")}
    assert accounts == {ROOT_CID}
    assert ads_run.get(conn, BIZ, GOOGLE)["resources"]["customer_id"] == ROOT_CID
    (audited,) = conn.execute("SELECT * FROM agent_action WHERE tool = 'job.google_account'").fetchall()
    assert (audited["actor"], audited["result"]["customer_id"]) == ("job:allocator", ROOT_CID)
    said = tools_account.sentence(audited, None)
    assert said == "Admiry chose the Admiry Google Ads account your Google Search ads run in" and plain(said)
    assert ROOT_CID not in said and "123-456-7890" not in said  # a pool account is never named to the owner


def test_two_businesses_share_one_account_each_building_and_reading_only_its_own_ads(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
):
    _pool(conn, ROOT_CID, "root")
    for biz, point in (("biz_1", PALO_ALTO), ("biz_2", SAN_JOSE)):  # two dentists that do not compete
        queued(conn, _dentist(conn, biz, point), biz=biz)
        assert ads_run.launch(conn, config.load(), biz, GOOGLE)["state"] == "live"
    one, two = (ads_run.get(conn, biz, GOOGLE)["resources"] for biz in ("biz_1", "biz_2"))
    assert one["customer_id"] == two["customer_id"] == ROOT_CID and one["campaign"] != two["campaign"]
    names = sorted(c["name"] for c in gads.campaigns.values())
    assert names == ["Admiry · Bright Smile Dental · biz_1", "Admiry · Bright Smile Dental · biz_2"]
    assert google_managed.account(conn, ROOT_CID).live == 2
    # every read after the launch names the business's own campaign
    gads.sent.clear()
    ads_run.pause(conn, config.load(), "biz_2", GOOGLE, by="owner")
    (pause,) = gads.bodies("pause")
    assert pause["mutateOperations"][0]["campaignOperation"]["update"]["resourceName"] == two["campaign"]
    assert gads.campaigns[one["campaign"]]["status"] == "ENABLED"


def test_a_launch_with_no_room_waits_tells_the_operator_and_goes_on_once_an_account_is_added(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
    caplog,
):
    plan_id = _prepaid(conn, knowledge=_trade("general_dentistry"))
    queued(conn, plan_id)
    with caplog.at_level("WARNING"):
        out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = ads_run.get(conn, BIZ, GOOGLE)
    assert (r["state"], r["last_error"]) == ("queued", google.ACCOUNT_OPENING) and plain(google.ACCOUNT_OPENING)
    assert out["state"] == "queued" and gads.sent == [] and google_managed.get(conn, BIZ) is None
    assert "waits for room in a Google Ads account" in caplog.text  # AGENT_OPS_EMAIL unset: a log line
    mails: list[dict] = []
    monkeypatch.setenv("AGENT_OPS_EMAIL", "ops@admiry.ai")
    monkeypatch.setattr(google.mail_out, "send_plain", lambda settings, **kw: mails.append(kw) or True)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    (mail,) = mails
    assert mail["to"] == "ops@admiry.ai" and mail["idempotency_key"].startswith(f"google-account-{BIZ}-")
    text = " ".join(mail["paragraphs"])
    assert "account_dentists_general_dentistry, account_dentists, account_root" in text
    assert "Why: no account of its scopes is in the pool yet." in text
    # founder 2026-10-03, one advertiser per account: open a few empty accounts at once and add each with the default
    assert "One business per account" in text and "`python -m agent.ops google-account add <customer_id>`" in text
    _pool(conn, FAMILY_CID, "dentists")
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live" and google_managed.get(conn, BIZ).customer_id == FAMILY_CID


def test_a_validate_launch_binds_the_account_it_checks_with(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _pool(conn, ROOT_CID, "root")
    queued(conn, _prepaid(conn))
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "queued" and gads.kinds() == ["validate_create"]
    assert google_managed.get(conn, BIZ).customer_id == ROOT_CID


# ---------------------------------------------------------------- the balance's guard (spend-guard)


def _live_prepaid(conn, *, funds: int) -> None:
    _pool(conn, ROOT_CID, "root")
    queued(conn, _prepaid(conn, funds=funds))
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"


def _cost(conn, day: date, cents: int) -> None:
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source, synced_at) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'api', now()) "
        "ON CONFLICT (business_id, channel, day) DO UPDATE SET cost_micros = EXCLUDED.cost_micros",
        (BIZ, GOOGLE, day, cents * 10_000),
    )


def test_spend_guard_pauses_google_search_once_unposted_spend_reaches_the_balance_and_resumes_after_a_top_up(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    """The spend Google reports today is not posted to the balance until wallet-settle takes the closed day, while
    Admiry pays Google meanwhile: the guard counts it. Under the balance: nothing; at it: the business is flagged and
    Google Search paused with `no_funds` (Google is told); a top-up lifts the flag and the next run turns the ads back
    on -- that pause alone."""
    clock(datetime(2026, 10, 3, 18, 0, tzinfo=UTC))
    _live_prepaid(conn, funds=10_000)
    _cost(conn, date(2026, 10, 3), 9_999)
    assert tick.run("spend-guard", url=TEST_DB_URL)["funds_paused"] == 0
    _cost(conn, date(2026, 10, 3), 10_000)
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert (out["paused"], out["funds_paused"], out["ops_by_channel"]) == (1, 1, {GOOGLE: out["ops"]})
    r = ads_run.get(conn, BIZ, GOOGLE)
    assert (r["state"], r["paused_reason"]) == ("paused", "no_funds") and "pause" in gads.kinds()
    flagged = conn.execute("SELECT paused_reason FROM business WHERE id = %s", (BIZ,)).fetchone()["paused_reason"]
    assert flagged == "no_funds"
    # the balance itself still shows $100.00 here: the owner never reads "balance is $0" (A3c-1 review)
    audited = conn.execute("SELECT * FROM agent_action WHERE tool = 'job.spend_guard' ORDER BY id").fetchall()
    assert [tools_account.sentence(r, None) for r in audited] == [
        "Admiry paused your ads: recent ad spend, not yet taken from your balance, uses it up. Add funds to resume.",
        "Admiry paused Google Search until you add funds.",
    ]
    assert tick.run("spend-guard", url=TEST_DB_URL)["funds_paused"] == 0  # once
    wallet.add_entry(conn, BIZ, kind="topup", amount_cents=20_000, ref="pi_more", created_by="stripe")
    assert wallet.resume_if_funded(conn, BIZ)
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert out["funds_resumed"] == 1
    r = ads_run.get(conn, BIZ, GOOGLE)
    assert (r["state"], r["paused_reason"]) == ("live", None)


def test_spend_guard_lifts_the_flag_itself_when_a_top_up_landed_after_its_read(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    """A3c-1 review: the Stripe webhook takes no wallet lock, so a top-up can commit between a run's read of the balance
    and its flag, after `resume_if_funded` found nothing to lift; the next run lifts the flag itself (the balance above
    the spend not posted yet) and turns the ads back on."""
    clock(datetime(2026, 10, 3, 18, 0, tzinfo=UTC))
    _live_prepaid(conn, funds=10_000)
    _cost(conn, date(2026, 10, 3), 10_000)
    assert tick.run("spend-guard", url=TEST_DB_URL)["funds_paused"] == 1
    wallet.add_entry(conn, BIZ, kind="topup", amount_cents=20_000, ref="pi_late", created_by="stripe")  # no lift
    out = tick.run("spend-guard", url=TEST_DB_URL)
    assert out["funds_resumed"] == 1
    assert conn.execute("SELECT paused_reason FROM business WHERE id = %s", (BIZ,)).fetchone()["paused_reason"] is None
    r = ads_run.get(conn, BIZ, GOOGLE)
    assert (r["state"], r["paused_reason"]) == ("live", None)


def test_spend_before_the_first_account_is_never_counted_against_the_balance(
    conn,
    gads,  # noqa: F811 - the fixture shadows the imported name
    live,  # noqa: F811 - the fixture shadows the imported name
    clock,  # noqa: F811 - the fixture shadows the imported name
):
    clock(datetime(2026, 10, 3, 18, 0, tzinfo=UTC))
    _live_prepaid(conn, funds=10_000)
    conn.execute("UPDATE google_ads_binding SET bound_at = '2026-10-02 12:00+00'")
    _cost(conn, date(2026, 10, 1), 50_000)  # the owner's own account's, before Admiry ran it
    assert jobs._unposted_cents(conn, BIZ, jobs.settleable()) == 0
    _cost(conn, date(2026, 10, 3), 2_500)
    assert jobs._unposted_cents(conn, BIZ, jobs.settleable()) == 2_500
    assert tick.run("spend-guard", url=TEST_DB_URL)["funds_paused"] == 0


# ---------------------------------------------------------------- the operator's commands


CUSTOMER = {
    "descriptiveName": "account_dentists",
    "manager": False,
    "currencyCode": "USD",
    "timeZone": "America/Los_Angeles",
    "status": "ENABLED",
    "testAccount": False,
}


@pytest.fixture
def probe_google(monkeypatch):
    """Google answering the account read `google-account add` sends (`customer` is what it says)."""
    for name, value in ADS_ENV.items():
        monkeypatch.setenv(name, value)
    said = {"customer": dict(CUSTOMER)}
    sent: list = []

    def client():
        reply = httpx.Response(200, json={"results": [{"customer": said["customer"]}]})
        return fake_transport(sent, {"googleAds:search": reply})

    monkeypatch.setattr(google_ads, "client", client)
    return said


def test_ops_add_reads_the_account_from_google_and_adds_it_under_its_scope(conn, capsys, probe_google):
    assert ops.main(["google-account", "add", "222-222-2222", "--scope", "account_dentists", "--capacity", "5"]) == 0
    out = capsys.readouterr().out
    assert "currency_code: USD" in out and "account_dentists 222-222-2222 open live 0/5" in out
    (row,) = conn.execute("SELECT * FROM google_ads_pool").fetchall()
    assert (row["scope"], row["capacity"], row["descriptive_name"], row["time_zone"]) == (
        "dentists",
        5,
        "account_dentists",
        "America/Los_Angeles",
    )
    assert conn.execute("SELECT tool FROM agent_action").fetchone()["tool"] == "ops.google_pool_add"


def test_ops_add_files_one_business_per_account_under_root_unless_told_otherwise(conn, capsys, probe_google):
    """Founder 2026-10-03: one advertiser, one Google Ads account -- `add` takes capacity 1 and the root scope by
    default, so the week's batch of empty accounts is `google-account add <customer_id>` each; `--capacity none`
    shares an account by industry, as before."""
    assert ops.main(["google-account", "add", "222-222-2222"]) == 0
    assert ops.main(["google-account", "add", "333-333-3333", "--scope", "dentists", "--capacity", "none"]) == 0
    rows = conn.execute("SELECT customer_id, scope, capacity FROM google_ads_pool ORDER BY customer_id").fetchall()
    assert [tuple(r.values()) for r in rows] == [("2222222222", "root", 1), ("3333333333", "dentists", None)]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"manager": True}, "a manager account cannot run ads"),
        ({"currencyCode": "EUR"}, "its currency is EUR, not USD"),
        ({"status": "SUSPENDED"}, "its status is SUSPENDED, not ENABLED"),
    ],
)
def test_ops_add_refuses_an_account_that_cannot_carry_prepaid_ads(conn, capsys, probe_google, change, problem):
    probe_google["customer"] = {**CUSTOMER, **change}
    assert ops.main(["google-account", "add", "2222222222", "--scope", "root"]) == 1
    assert f"refused: {problem}" in capsys.readouterr().err
    assert google_managed.pool(conn) == []


def test_ops_bind_release_show_list_and_waiting(conn, capsys, ads_api):
    _pool(conn, ROOT_CID, "root", capacity=2)
    _prepaid(conn)
    _prepaid(conn, "biz_w")
    assert ops.main(["google-account", "waiting"]) == 0
    waiting = capsys.readouterr().out.splitlines()
    assert [(w.split()[0], w.split(" scopes ")[1]) for w in waiting] == [
        (BIZ, "dentists,root room"),
        ("biz_w", "dentists,root room"),
    ]
    assert ops.main(["google-account", "bind", BIZ, "123-456-7890", "--note", "E2E"]) == 0
    assert ops.main(["google-account", "list", "--businesses"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert listed[0].startswith("account_root 123-456-7890 open live 1/2") and listed[1].startswith(f"  {BIZ} bound")
    assert (
        ops.main(["google-account", "close", ROOT_CID]) == 0
        and ops.main(["google-account", "capacity", ROOT_CID, "none"]) == 0
    )
    assert (
        google_managed.account(conn, ROOT_CID).status == "closed"
        and google_managed.account(conn, ROOT_CID).capacity is None
    )
    assert ops.main(["google-account", "bind", "biz_w", ROOT_CID]) == 1
    assert "closed" in capsys.readouterr().err
    assert ops.main(["google-account", "release", BIZ]) == 0
    assert ops.main(["google-account", "show", BIZ]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith("123-456-7890 dentists bound ") and " by ops:cli released " in line
    tools = [
        r["tool"] for r in conn.execute("SELECT tool FROM agent_action WHERE business_id = %s ORDER BY id", (BIZ,))
    ]
    assert tools[-2:] == ["ops.google_account_bind", "ops.google_account_release"]
    for tool in tools[-2:]:
        row = conn.execute("SELECT * FROM agent_action WHERE tool = %s", (tool,)).fetchone()
        assert plain(tools_account.sentence(row, None))
