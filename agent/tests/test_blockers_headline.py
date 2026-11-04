"""B0 "Fix the headline" (spec 2026-10-08 §3.1, §8.1, §8.2; plan P6): the plan now leads with another service than the
headline of Admiry's live page names, so the launch refuses Message-match's fix; Admiry puts the plan's lead service and
city on the page itself -- the starter page's headline (update_page), or the site built again with every other word
kept (no model call) -- as a preview the owner publishes with one more tap, and the next look finds nothing in the way."""

from psycopg.types.json import Jsonb

from agent import blockers, google_ads_link, keys, landing, plans, proposals
from agent.sites import intake
from agent.tests.test_home_api import (  # noqa: F401 -- `fake` is a fixture used by name
    IP,
    PAGES_DOMAIN,
    _checked_transport,
    _planned,
    fake,
    turn,
)
from agent.tests.test_home_sites import _build, _owner
from agent.tests.test_sites_build import _own, dental_library, renderer  # noqa: F401 -- fixtures used by name

HEADLINE = "Teeth whitening in Palo Alto"


def _biz(conn) -> str:
    return conn.execute("SELECT id FROM business").fetchone()["id"]


def _items(client) -> list[dict]:
    """The plan card's checklist now (GET /api/launch_check), where a home answer's `blocker` moved to (L1 Task 4)."""
    r = client.get("/api/launch_check", headers=IP)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    return r.json()["check"]["items"]


def _leads_with(conn, biz: str, service: str) -> None:
    """The latest version's consultation names another lead service (as a fresh plan for another service would)."""
    plan = plans.latest(conn, biz)
    secs = plan["sections"]
    secs["consultation"]["business"].update(service=service, services=[service])
    conn.execute("UPDATE plan SET json = jsonb_set(json, '{sections}', %s) WHERE id = %s", (Jsonb(secs), plan["id"]))


def _publish(client, csrf: str, token: str) -> dict:
    r = client.post("/api/page/publish", json={"csrf": csrf, "token": token}, headers=IP)
    assert r.status_code == 200, r.text
    return r.json()


def _live_and_linked(client, conn, monkeypatch, csrf: str, token: str) -> str:
    """The preview published (its after-commit check against the page served from the row), the account linked so the
    launch's check reaches the page, the Google switch on. Returns the business id."""
    _checked_transport(monkeypatch, conn)
    assert _publish(client, csrf, token)["ok"]
    biz = _biz(conn)
    assert landing.saved_report(landing.current(conn, biz))["ok"] is True, "Admiry's page passes its own check"
    google_ads_link.save(conn, biz, "1234567890", status="active")
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")
    return biz


def _upgraded(conn, biz: str) -> None:
    """`_planned`'s and `_owner`'s claim writes the row directly, the page card's own need; the blocker `/api/turn`
    carries is a signed-in *business* key's alone (home_api's `state` action, `rec.cls != "draft"`; never a draft's,
    which signs in first). `keys.upgrade_draft` is the same upgrade a real claim (`claims.claim`) would have made."""
    assert keys.upgrade_draft(conn, biz) is not None


def test_the_starter_pages_headline_is_fixed_and_the_next_look_finds_no_refusal(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    resend,
):
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", PAGES_DOMAIN)
    csrf = _planned(client, conn, google)
    _upgraded(conn, _biz(conn))
    created = client.post("/api/page/create", json={"csrf": csrf}, headers=IP).json()
    biz = _live_and_linked(client, conn, monkeypatch, csrf, created["token"])
    assert proposals.launch_refusal(conn, biz, plans.latest(conn, biz)["id"]) is None  # nothing in the way yet
    _leads_with(conn, biz, "Teeth whitening")
    found = next(i for i in _items(client) if i["resolution"] == "page_fix")
    assert found["actions"] == [blockers.FIX_HEADLINE], found
    copies = len(fake.page_calls)
    # Task 5's page will send the item's id (spec gap 4, review): exercised here, where the other headline test below
    # keeps posting with none, exercising the fallback (today's home.js, which names no item for "Fix the headline").
    r = client.post(
        "/api/blocker",
        json={"csrf": csrf, "action": "fix_headline", "id": found["id"], "plan_id": found["plan_id"]},
        headers=IP,
    )
    fixed = r.json()
    assert fixed["ok"], fixed
    assert fixed["result"]["say"] == blockers.HEADLINE_READY.format(headline=HEADLINE)
    assert "blocker" not in fixed["result"]["state"]
    # the live page stands until published: the item is still on the list
    assert any(i["id"] == found["id"] and i["resolution"] == "page_fix" for i in _items(client))
    assert len(fake.page_calls) == copies  # the headline alone: no model call (§8.2)
    published = _publish(client, csrf, fixed["result"]["publish"]["token"])
    assert published["ok"], published
    assert proposals.launch_refusal(conn, biz, plans.latest(conn, biz)["id"]) is None
    assert "blocker" not in turn(client, {"action": "state"}).json()["result"]
    assert _items(client) == []


def test_admirys_site_is_built_again_with_the_plans_headline_and_every_other_word_kept(
    client,
    conn,
    fake,  # noqa: F811 -- the fixture
    google,
    monkeypatch,
    resend,
    dental_library,  # noqa: F811 -- the fixture
    renderer,  # noqa: F811 -- the fixture
):
    monkeypatch.setenv("AGENT_SITES", "1")  # this test's alone: the starter-page test needs it off
    csrf = _owner(client, conn, google, monkeypatch, fake)
    biz = _biz(conn)
    _upgraded(conn, biz)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    token = _build(client, csrf, style="calm")["page"]["preview"]["token"]
    _live_and_linked(client, conn, monkeypatch, csrf, token)
    before = intake.draft(landing.current(conn, biz))["words"]
    _leads_with(conn, biz, "Teeth whitening")
    found = next(i for i in _items(client) if i["resolution"] == "page_fix")
    assert found["actions"] == [blockers.FIX_HEADLINE], found
    words = len(fake.site_calls)
    r = client.post(  # no `id`: the fallback (today's home.js), kept alive beside the test above's
        "/api/blocker", json={"csrf": csrf, "action": "fix_headline", "plan_id": found["plan_id"]}, headers=IP
    )
    fixed = r.json()
    assert fixed["ok"], fixed
    assert len(fake.site_calls) == words  # every other word kept: no model call
    after = intake.draft(landing.current(conn, biz))["words"]
    assert after["home"]["headline"] == HEADLINE
    assert {k: v for k, v in after["home"].items() if k != "headline"} == {
        k: v for k, v in before["home"].items() if k != "headline"
    }
    assert after["about"] == before["about"] and after["service_pages"] == before["service_pages"]
    assert _publish(client, csrf, fixed["result"]["publish"]["token"])["ok"]
    assert proposals.launch_refusal(conn, biz, plans.latest(conn, biz)["id"]) is None


def test_no_headline_fix_when_the_headline_is_not_what_stops_the_launch(client, conn, fake, google, monkeypatch):  # noqa: F811 -- the fixture
    """Final review m8: a "Fix the headline" tapped when the headline no longer stands in the way is stale, like a fix
    for a plan that moved (blockers.STALE): it answers where things stand -- the sentence and the state -- so the page
    spends the message and draws what is next, never a dead end with the button still live."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")
    csrf = _planned(client, conn, google)
    plan_id = plans.latest(conn, _biz(conn))["id"]
    r = client.post("/api/blocker", json={"csrf": csrf, "action": "fix_headline", "plan_id": plan_id}, headers=IP)
    out = r.json()
    assert out["ok"] is True and out["result"]["resolved"], out
    assert out["result"]["say"] == blockers.NO_HEADLINE_FIX and out["result"]["state"]["plan_id"] == plan_id
    assert "publish" not in out["result"]  # nothing built: nothing to publish


def test_the_headline_pattern_is_the_plans_lead_service_and_city(conn):
    from agent.tests.test_ads_run import BIZ, seed
    from agent.tests.test_review_launch import draft_sections

    seed(conn, status="draft", secs=draft_sections())
    assert landing.headline_pattern(conn, BIZ) == "Invisalign in Palo Alto"
    conn.execute("DELETE FROM plan")
    assert landing.headline_pattern(conn, BIZ) is None
