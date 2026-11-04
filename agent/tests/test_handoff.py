from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from agent import benchmark, business, consult, geocode, listings, places, site_install
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_home_api import IP, PICK, SITE, fake, turn  # noqa: F401 (fixture)
from agent.tests.test_oauth_consent import _sign_in, _txn
from agent.tests.test_oauth_http import exchange, register


def _visitor_session(client, conn, *, picked: bool = True):
    """The website's consultation: begun, the website given (the fixture site, read: the consultation started again from
    it, plan 2026-10-05-agent-a3p-5-1), the business on Google Maps picked (it centres the ads; not when `picked` is
    False, the question still waiting), and the objective answered."""
    turn(
        client,
        {"action": "start", "text": "dentist in Palo Alto within 5 miles, $2,000 a month", "idempotency_key": "i1"},
    )
    turn(client, SITE)
    if picked:
        turn(client, PICK)
    turn(client, {"action": "answer", "answers": {"objective": {"choice": "bookings"}}, "idempotency_key": "a1"})
    # ORDER BY created_at DESC: this is called a second time in test_handoff_is_one_use_and_expires for a
    # fresh visitor, by which point an earlier (moved) session row is also in the table.
    return conn.execute("SELECT id, business_id FROM consult_session ORDER BY created_at DESC LIMIT 1").fetchone()


def test_handoff_mints_a_token_and_the_prompts(client, conn, fake):  # noqa: F811 (fixture)
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    r = client.post("/api/handoff", json={}, headers=IP)
    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"]
        and body["token"].startswith("hs_")
        and body["expires_in"] == 1800
        and body["mcp_url"] == "http://testserver/mcp"
    )
    muse = body["prompts"]["muse"]
    assert muse.startswith(
        "Build a custom integration to Admiry, my advertising agent. MCP server http://testserver/mcp"
    )
    assert muse.endswith(f"then sign me in and continue session {body['token']}.")
    assert "key" not in muse.lower()  # the sheet promises the owner never copies a key
    assert "claude mcp add" in body["prompts"]["claude"] and body["token"] in body["prompts"]["chatgpt"]
    row = conn.execute(
        "SELECT handoff_hash, handoff_expires_at FROM consult_session WHERE id = %s", (s["id"],)
    ).fetchone()
    assert row["handoff_hash"] and body["token"] not in row["handoff_hash"]
    # m8 (final review): the anonymous mint is the visitor's draft key's act, like web.say
    draft = conn.execute("SELECT id FROM api_key WHERE class = 'draft'").fetchone()
    audit = conn.execute(
        "SELECT actor, actor_class, api_key_id FROM agent_action WHERE tool = 'web.handoff'"
    ).fetchone()
    assert (audit["actor"], audit["actor_class"], audit["api_key_id"]) == (f"key:{draft['id']}", "draft", draft["id"])


def test_keyed_start_with_a_handoff_moves_the_session_to_the_tokens_business(
    client,
    conn,
    fake,  # noqa: F811 (fixture)
):
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    res = call_tool(client, "start", {"handoff": token}, key=key)
    assert not res["isError"], tool_text(res)
    d = res["structuredContent"]["data"]
    assert d["business_id"] == "biz_owner" and d["session_id"] == s["id"]
    assert res["structuredContent"]["next"]["question"]["id"] == "service"  # continues where the website left off
    assert "website" in res["structuredContent"]["say"]
    moved = consult.get(conn, "biz_owner", s["id"])
    assert moved["answers"]["objective"]["choice"] == "bookings"
    assert (
        conn.execute("SELECT status FROM business WHERE id = %s", (s["business_id"],)).fetchone()["status"] == "expired"
    )
    assert conn.execute("SELECT count(*) AS n FROM knowledge WHERE business_id = 'biz_owner'").fetchone()["n"] >= 1
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM api_key WHERE business_id = %s AND revoked_at IS NULL", (s["business_id"],)
        ).fetchone()["n"]
        == 0
    )


def test_a_handoff_before_the_place_was_picked_brings_the_places_offered(
    client,
    conn,
    fake,  # noqa: F811 (fixture)
):
    """Plan 2026-10-03-agent-listing-confirm: the visitor left the website before saying which place is the business.
    The moved consultation still asks it, and the places offered go with it: picked on the business it landed on, the
    place is linked there."""
    benchmark.seed(conn)
    s = _visitor_session(client, conn, picked=False)
    assert [c["place_id"] for c in listings.offer(conn, s["business_id"])] == ["ChIJ1"]
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    res = call_tool(client, "start", {"handoff": token}, key=key)
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["next"]["question"]["id"] == "listing"
    assert listings.offer(conn, s["business_id"]) == []
    assert [c["place_id"] for c in listings.offer(conn, "biz_owner")] == ["ChIJ1"]
    args = {"session_id": s["id"], "answers": {"listing": {"choice": "gm_1"}}, "idempotency_key": "l1"}
    picked = call_tool(client, "answer", args, key=key)
    assert not picked["isError"], tool_text(picked)
    listing = business.latest_knowledge(conn, "biz_owner")[1]["listing"]
    assert listing["place_id"] == "ChIJ1" and listing["confirmed"] is True


HOUSTON = places.Candidate("ChIJ2", "123 Main St", "123 Main St, Houston, TX 77002, USA", 29.76, -95.37)


def _landing_with_a_place(client, conn) -> tuple[dict, str]:
    """The website's consultation (its lookup found Bright Smile Dental, which the visitor picked: its point centred
    the ads) and a claimed biz_owner whose owner put HOUSTON on the map. Returns (the visitor's session row, biz_owner's
    key)."""
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    held = {"name": "Bright Smile Dental", "category": "dentists", "listing": geocode.listing(HOUSTON)}
    business.add_knowledge(conn, "biz_owner", held, source="owner")
    return s, key


def test_a_handoff_landing_keeps_the_place_its_owner_put_on_the_map(client, conn, fake):  # noqa: F811 (fixture)
    """Walk fixes 1, final review A1: the website's draft brings the business listing its lookup found, with its own
    point; the business it lands on holds the address its owner put on the map. The landing keeps the owner's place:
    the draft's listing is moved to it, its name kept, and the moved consultation's pre-answer of where the ads centre
    names it (the radius question reads it)."""
    s, key = _landing_with_a_place(client, conn)
    before = consult.get(conn, s["business_id"], s["id"])["answers"]["location"]
    assert before["source"] == "known_fact" and before["lat"] == 37.4419  # the website's lookup's point
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    res = call_tool(client, "start", {"handoff": token}, key=key)
    assert not res["isError"], tool_text(res)
    listing = business.latest_knowledge(conn, "biz_owner")[1]["listing"]
    assert (listing["name"], listing["address"], listing["lat"], listing["lng"]) == (
        "Bright Smile Dental",
        HOUSTON.address,
        29.76,
        -95.37,
    )
    assert consult.get(conn, "biz_owner", s["id"])["answers"]["location"] == {
        "address": HOUSTON.address,
        "lat": 29.76,
        "lng": -95.37,
        "source": "known_fact",
    }


def test_a_place_the_visitor_put_on_the_map_wins_over_the_landings(client, conn, fake):  # noqa: F811 (fixture)
    """Final review A1: the newer statement wins -- a place the visitor put on the map on the website (a listing of kind
    address on the draft) is what the landing keeps, not the one its owner placed before."""
    s, key = _landing_with_a_place(client, conn)
    austin = places.Candidate("ChIJ9", "4 Elm St", "4 Elm St, Austin, TX 78701, USA", 30.27, -97.74)
    draft = business.latest_knowledge(conn, s["business_id"])[1]
    business.add_knowledge(conn, s["business_id"], {**draft, "listing": geocode.listing(austin)}, source="owner")
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    assert not call_tool(client, "start", {"handoff": token}, key=key)["isError"]
    assert business.latest_knowledge(conn, "biz_owner")[1]["listing"] == geocode.listing(austin)


def test_handoff_is_one_use_and_expires(client, conn, fake):  # noqa: F811 (fixture)
    benchmark.seed(conn)
    _visitor_session(client, conn)
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    assert not call_tool(client, "start", {"handoff": token}, key=key)["isError"]
    key2 = business_with_key(conn, biz_id="biz_other", claim_state="claimed")
    res = call_tool(client, "start", {"handoff": token}, key=key2)
    assert res["isError"] and "already used or expired" in tool_text(res)
    moved = conn.execute("SELECT id, business_id FROM consult_session ORDER BY created_at DESC LIMIT 1").fetchone()
    assert moved["business_id"] == "biz_owner"  # the refused second redeem moved nothing
    s2 = _visitor_session(client, conn)  # a new visitor (cookie replaced by the new start)
    token2 = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    conn.execute(
        "UPDATE consult_session SET handoff_expires_at = now() - interval '1 minute' WHERE id = %s", (s2["id"],)
    )
    res = call_tool(client, "start", {"handoff": token2}, key=key2)
    assert res["isError"] and "already used or expired" in tool_text(res)
    row = conn.execute("SELECT business_id FROM consult_session WHERE id = %s", (s2["id"],)).fetchone()
    assert row["business_id"] == s2["business_id"]  # the late redeem moved nothing either
    assert conn.execute("SELECT status FROM business WHERE id = %s", (s2["business_id"],)).fetchone()["status"] == (
        "active"
    )


def test_consent_offers_the_website_plan_and_claims_it(client, conn, fake, google):  # noqa: F811 (fixture)
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    cid = register(client)
    txn, verifier = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    page = client.get(f"/oauth/consent?txn={txn}")
    assert (
        "Continue the plan you started on the website" in page.text
        and f'value="website:{s["business_id"]}"' in page.text
    )
    r = client.post(
        "/oauth/consent",
        data={
            "txn": txn,
            "csrf": csrf,
            "business": f"website:{s['business_id']}",
            "scopes": ["read", "manage", "leads"],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    biz = conn.execute("SELECT owner_email, claim_state FROM business WHERE id = %s", (s["business_id"],)).fetchone()
    assert (biz["owner_email"], biz["claim_state"]) == ("owner@example.com", "claimed")
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1  # no blank business created
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, cid, code, verifier).json()
    who = data(call_tool(client, "whoami", {}, key=tok["access_token"]))
    assert who["business"]["id"] == s["business_id"] and who["open_session_id"] == s["id"]


def test_consent_cannot_claim_a_business_that_is_no_longer_a_draft(client, conn, fake, google):  # noqa: F811 (fixture)
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    conn.execute("UPDATE business SET claim_state = 'claimed', owner_email = 'other@example.com'")
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent",
        data={"txn": txn, "csrf": csrf, "business": f"website:{s['business_id']}", "decision": "allow"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_start_handoff_for_the_same_business_does_not_move_or_expire_it(
    client,
    conn,
    fake,  # noqa: F811 (fixture)
    google,
):
    """C1 (fix round 1): the consent page already claimed the website draft for the signer, so the
    token's session is already on the caller's own business by the time the assistant pastes it into
    `start` — nothing should be superseded, expired or revoked just because the ids match."""
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    cid = register(client)
    txn, verifier = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent",
        data={
            "txn": txn,
            "csrf": csrf,
            "business": f"website:{s['business_id']}",
            "scopes": ["read", "manage", "leads"],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, cid, code, verifier).json()
    res = call_tool(client, "start", {"handoff": token}, key=tok["access_token"])
    assert not res["isError"], tool_text(res)
    d = res["structuredContent"]["data"]
    assert d["business_id"] == s["business_id"] and d["session_id"] == s["id"]
    biz = conn.execute("SELECT status, claim_state FROM business WHERE id = %s", (s["business_id"],)).fetchone()
    assert biz["status"] == "active" and biz["claim_state"] == "claimed"
    who = data(call_tool(client, "whoami", {}, key=tok["access_token"]))
    assert who["business"]["id"] == s["business_id"] and who["open_session_id"] == s["id"]


def test_start_handoff_refuses_a_claimed_foreign_business(client, conn, fake, google):  # noqa: F811 (fixture)
    """C1 (fix round 1): a claimed business's session must never be moved out from under its owner just
    because someone else pastes its hand-off token — refused, nothing changed, and the token stays
    redeemable afterward by a key actually bound to that same business (the refusal's rollback undoes
    take_handoff's own clearing)."""
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    conn.execute(
        "UPDATE business SET claim_state = 'claimed', owner_email = 'owner@example.com' WHERE id = %s",
        (s["business_id"],),
    )
    csrf = _sign_in(client, google)  # a claimed visitor mints only beside its owner's web session (I1)
    token = client.post("/api/handoff", json={"csrf": csrf}, headers=IP).json()["token"]
    foreign_key = business_with_key(conn, biz_id="biz_other", claim_state="claimed")
    res = call_tool(client, "start", {"handoff": token}, key=foreign_key)
    assert res["isError"] and "signed-in account" in tool_text(res)
    assert conn.execute("SELECT status FROM business WHERE id = %s", (s["business_id"],)).fetchone()["status"] == (
        "active"
    )
    own_key = business_with_key(conn, biz_id=s["business_id"], claim_state="claimed")
    res2 = call_tool(client, "start", {"handoff": token}, key=own_key)
    assert not res2["isError"], tool_text(res2)
    assert res2["structuredContent"]["data"]["session_id"] == s["id"]


def test_a_finished_plan_hands_off_as_the_plan_itself(client, conn, fake):  # noqa: F811 (fixture)
    """Founder's phone walk (2026-09-28): the sheet opens after the plan is built, so a planned session must
    hand off too — the assistant's `start(handoff)` answers with the plan (cards, launch actions), never the
    questions again. The sheet's mint prefers an open/ready session and falls back to the latest planned one."""
    benchmark.seed(conn)
    key = business_with_key(conn, biz_id="biz_owner", scopes=("read", "manage"), claim_state="claimed")
    res = call_tool(client, "start", {"text": "dentist in Palo Alto within 5 miles, $2,000 a month"}, key=key)
    sid = data(res)["session_id"]
    for qid, value in (
        *SITE["answers"].items(),  # the website, first: the words name none
        ("listing", {"choice": "gm_1"}),  # the business on Google Maps, next: it centres the ads
        ("objective", {"choice": "bookings"}),
        ("service", {"choice": "svc_invisalign"}),
        ("customer_value", {"choice": "1000_plus"}),
    ):
        call_tool(client, "answer", {"session_id": sid, "answers": {qid: value}, "idempotency_key": qid}, key=key)
    rec = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "r1"}, key=key)
    assert not rec["isError"], tool_text(rec)
    assert consult.get(conn, "biz_owner", sid)["status"] == "planned"
    assert consult.handoff_for(conn, "biz_owner")["id"] == sid  # the sheet's pick: the plan just built
    token = consult.mint_handoff(conn, sid)
    assert consult.peek_handoff(conn, token)
    res = call_tool(client, "start", {"handoff": token}, key=key)  # the same business: consumed, nothing moved
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["say"].startswith(
        "I picked up the plan you built on the website. Here is the plan: More Invisalign bookings"
    )
    assert env["data"]["plan_id"] == data(rec)["plan_id"] and env["data"]["cards"]["budget"]["monthly_usd"] == 2000
    # the ways to put Admiry's code on the owner's site first, while it has none (A3p-5 batch 2 Q6; support off, as
    # here: without Admiry's team, ruling R3-f)
    ways = site_install.ways_for(None)
    assert env["next"]["kind"] == "actions" and env["next"]["actions"][: len(ways)] == ways
    # Admiry running the ads leads, then the owner's own account, then a change (founder's ruling, 2026-10-07)
    assert [a["id"] for a in env["next"]["actions"][len(ways) :]] == ["prepay", "link_own_account", "adjust"]
    assert not consult.peek_handoff(conn, token)  # one use
    assert consult.get(conn, "biz_owner", sid)["status"] == "planned"  # still the same plan, not re-asked


def test_the_sheet_mints_for_the_website_plan_once_it_is_built(client, conn, fake):  # noqa: F811 (fixture)
    """/api/handoff on a planned visitor session answers with a token (it used to say "Nothing to hand off")."""
    benchmark.seed(conn)
    s = _visitor_session(client, conn)
    conn.execute("UPDATE consult_session SET status = 'planned' WHERE id = %s", (s["id"],))
    r = client.post("/api/handoff", json={}, headers=IP)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["token"].startswith("hs_")


def test_handoff_token_stops_redeeming_once_its_session_is_superseded(client, conn, fake):  # noqa: F811 (fixture)
    """I2 (fix round 1): take_handoff's own WHERE also requires status open/ready, so a token minted for
    a session that a later `start` supersedes stops redeeming even though it has not expired."""
    benchmark.seed(conn)
    _visitor_session(client, conn)
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    turn(client, {"action": "start", "text": "orthodontist in Menlo Park", "idempotency_key": "i2"})
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    res = call_tool(client, "start", {"handoff": token}, key=key)
    assert res["isError"] and "already used or expired" in tool_text(res)


def test_keyed_start_with_a_handoff_of_a_ready_session_offers_the_plan(client, conn, fake):  # noqa: F811 (fixture)
    """_handoff_step's ready branch: a fully answered website consultation lands on the recommend action."""
    benchmark.seed(conn)
    _visitor_session(client, conn)
    for qid, choice in (("service", "svc_invisalign"), ("customer_value", "1000_plus")):
        turn(client, {"action": "answer", "answers": {qid: {"choice": choice}}, "idempotency_key": qid})
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    res = call_tool(client, "start", {"handoff": token}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["next"]["kind"] == "actions" and [a["tool"] for a in env["next"]["actions"]] == ["recommend"]
    assert env["say"] == "I picked up the plan you started on the website. Say the word and I will build the plan."


def test_state_after_a_handoff_says_the_plan_moved(client, conn, fake):  # noqa: F811 (fixture)
    """I3 (final review): once the draft moved to an assistant, the page's reload says so, not "expired"."""
    benchmark.seed(conn)
    _visitor_session(client, conn)
    token = client.post("/api/handoff", json={}, headers=IP).json()["token"]
    key = business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    assert not call_tool(client, "start", {"handoff": token}, key=key)["isError"]
    assert turn(client, {"action": "state"}).json() == {"ok": True, "fresh": True, "reason": "moved"}


# ---------------------------------------------------------------- what a hand-off does to the destination's goal
# Final review open item A2 (A and B): a draft's goal lands on a business whose owner already set one. A draft states
# only what the visitor said; what it leaves at its default never replaces what the owner set.
GOAL_FIELDS = ("goal_type", "target", "monthly_limit_usd", "limit_source", "focus_services", "autopilot")


def _land(conn, *, draft: dict, owner: dict | None) -> dict:
    """A draft with goal `draft` handed off to biz_owner, whose own goal is `owner` (None: it has none). Returns the
    goal biz_owner holds afterwards."""
    business_with_key(conn, biz_id="biz_draft")
    business_with_key(conn, biz_id="biz_owner", claim_state="claimed")
    if owner is not None:
        business.set_goal(conn, "biz_owner", **owner)
    business.set_goal(conn, "biz_draft", **draft)
    tree = consult.load_tree("fallback")
    session = consult.create(
        conn,
        "biz_draft",
        tree="fallback",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, {}),
        answers={},
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert consult.move_session(conn, session, to_business="biz_owner")["business_id"] == "biz_owner"
    return business.latest_goal(conn, "biz_owner")


OWNERS = {
    "goal_type": "calls",
    "target": 20,
    "monthly_limit_usd": 900,
    "focus_services": ["Invisalign"],
    "autopilot": "off",
}


def test_a_hand_off_keeps_what_the_destinations_owner_set_when_the_draft_states_nothing(conn):
    goal = _land(conn, draft={}, owner=OWNERS)  # the draft's goal: every default
    assert {k: goal[k] for k in GOAL_FIELDS} == {**OWNERS, "monthly_limit_usd": 900.0, "limit_source": "owner"}


def test_what_a_draft_states_replaces_the_owners_and_the_rest_stays(conn):
    goal = _land(conn, draft={"target": 30, "monthly_limit_usd": 2000}, owner=OWNERS)  # the visitor's own limit
    assert {k: goal[k] for k in GOAL_FIELDS} == {
        **OWNERS,
        "target": 30,
        "monthly_limit_usd": 2000.0,
        "limit_source": "owner",
    }


def test_a_limit_admiry_set_for_the_draft_leaves_the_owners_limit_in_place(conn):
    goal = _land(conn, draft={"monthly_limit_usd": 3600, "limit_source": "system"}, owner=OWNERS)
    assert {k: goal[k] for k in GOAL_FIELDS} == {**OWNERS, "monthly_limit_usd": 900.0, "limit_source": "owner"}


def test_a_destination_with_no_goal_takes_the_drafts_whole(conn):
    draft = {"target": 30, "monthly_limit_usd": 3600, "limit_source": "system", "focus_services": ["Whitening"]}
    goal = _land(conn, draft=draft, owner=None)
    assert {k: goal[k] for k in GOAL_FIELDS} == {
        "goal_type": "customers",
        "target": 30,
        "monthly_limit_usd": 3600.0,
        "limit_source": "system",
        "focus_services": ["Whitening"],
        "autopilot": "safe",
    }
