import json
import re
import time
import tracemalloc
import zlib

import httpx
import pytest
from fastmcp.exceptions import ToolError

from agent import ads_conversions, assistant_skill, benchmark, config, copy, db, landing, mail, plans, track
from agent.tests.conftest import JARGON
from agent.tests.fixtures import business_with_key, plan_draft_fixture
from agent.tests.test_ads_conversions import CID, actions_response, customer_response, fake_client
from agent.tests.test_tools_consult import _answer, _start, fake  # noqa: F401 (fixture)

TREES = ("dentists", "home_services", "attorneys", "fallback")


def test_every_tree_has_a_recipe_with_the_same_shape():
    for tree in TREES:
        r = landing.load_recipe(tree)
        assert (
            r["tree"] == tree
            and isinstance(r["sensitive"], bool)
            and r["cta"] in ("call", "call_or_book", "call_or_quote")
        )
        ids = [s["id"] for s in r["sections"]]
        assert ids[0] == "hero" and "trust" in ids and "faq" in ids and "hours" in ids and ids[-1] == "footer"
        for s in r["sections"]:
            assert s["title"] and s["guide"] and not JARGON.search(s["guide"]), (tree, s["id"])
            assert not JARGON.search(s.get("example", "")), (tree, s["id"], "example")
        assert r["form_fields"][:2] == ["name", "phone"]
        # I9: every field a recipe asks for is one the form handler stores
        assert set(r["form_fields"]) <= set(track.FIELDS), (tree, r["form_fields"])
        for rule in r["form_rules"]:
            assert not JARGON.search(rule), (tree, "form_rules", rule)
        for q in r["faq"]:
            assert not JARGON.search(q), (tree, "faq", q)
        assert all({"text", "when"} <= set(line) for line in r["required_lines"])
        assert "guaranteed" in r["banned"]
        # A3 Task 3: the campaign-level negative keywords ads_launch.operations adds on top of the plan's own
        assert r["negatives"] and all(isinstance(w, str) and w == w.lower() for w in r["negatives"])
    assert landing.load_recipe("no-such-tree")["tree"] == "fallback"


def test_the_one_goal_rule_keeps_the_links_the_page_needs():
    """Open item C9: "No menu, no links away from it" stood beside the call, booking, map and privacy links the same
    guide asks for. The rule names the links a page keeps; the generated page guide carries it."""
    rule = (
        "Keep one goal on the page: call, or request a call. No menu, and no links but the ones the page needs: to "
        "call, to book, to the map, and to the privacy and terms pages."
    )
    assert landing.GOOGLE_RULES[3] == rule
    guide = assistant_skill.page_guide()
    assert f"- {rule}" in guide and "no links away from it" not in guide


def test_dentists_recipe_is_sensitive_and_has_no_health_question():
    r = landing.load_recipe("dentists")
    assert r["sensitive"] is True and r["cta"] == "call_or_book"
    assert "health" not in " ".join(r["form_fields"]).lower()
    assert any("never ask a health question" in rule.lower() for rule in r["form_rules"])


def test_no_recipe_carries_a_line_the_law_dropped_or_a_bare_insurance_claim():
    """The K3 review of the knowledge files (2026-10-15): New York replaced its lawyer-advertising rules on 2026-06-01
    (agent/knowledge/attorneys/compliance.md, Numbers), so "Attorney Advertising" and the prior-results line are no
    longer lines a page must carry -- a New York page follows the ABA-style Key points like every other state's; and
    California disciplines "insured" without the type of coverage, and "bonded", on a website too
    (agent/knowledge/home_services/compliance.md, Key point 3), so a recipe names the coverage ("general liability
    insured") and never says "bonded"."""
    assert landing.load_recipe("attorneys")["required_lines"] == []
    for tree in TREES:
        r = landing.load_recipe(tree)
        text = json.dumps(r).lower()
        assert "attorney advertising" not in text and "prior results" not in text, tree
        for claim in re.finditer(r"\b(?:general liability )?insured\b", text):
            assert claim.group(0).startswith("general liability"), (tree, claim.group(0))
        # "bonded" is never shown or asked for; a guide may name it only to say never
        shown = json.dumps([[s["title"], s.get("example", "")] for s in r["sections"]]).lower()
        shown += json.dumps([r["required_lines"], r["form_rules"], r["faq"]]).lower()
        assert "bonded" not in shown, tree
        for s in r["sections"]:
            if "bonded" in s["guide"].lower():
                assert "never" in s["guide"].lower(), (tree, s["id"])


def _plan(client, conn, fake):  # noqa: F811 (fixture)
    """A consulted, recommended plan on biz_1 (the dental walk of test_tools_consult)."""
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, {"listing": {"choice": "gm_1"}}, "k0")  # the business on Google Maps: centres the ads
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "k1")
    _answer(client, key, sid, {"service": {"choice": "svc_invisalign"}}, "k2")
    _answer(client, key, sid, {"customer_value": {"choice": "1000_plus"}}, "k3")
    from agent.tests.conftest import call_tool

    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "kr"}, key=key)
    assert not res["isError"]
    return key, b["data"]["business_id"], res["structuredContent"]["data"]["plan_id"]


def _plan_with_services(client, conn, fake, picks: list[str]):  # noqa: F811 (fixture)
    """`_plan`, the service question answered with several picks (a multi answer's list)."""
    benchmark.seed(conn)
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, {"listing": {"choice": "gm_1"}}, "k0")
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "k1")
    _answer(client, key, sid, {"service": {"choice": picks}}, "k2")
    _answer(client, key, sid, {"customer_value": {"choice": "1000_plus"}}, "k3")
    from agent.tests.conftest import call_tool

    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "kr"}, key=key)
    assert not res["isError"]
    return key, b["data"]["business_id"]


def test_the_kit_leads_with_one_picked_service_and_the_check_takes_any_of_them(client, conn, fake):  # noqa: F811
    """Founder's dev walk, 2026-10-07: several picked services joined into one phrase made a headline pattern no
    60-character headline could hold. The kit names the picks (`lead_services`) and patterns the headline on the first;
    the check's Message-match rule takes a headline that names any of them with the city."""
    _key, biz = _plan_with_services(client, conn, fake, ["svc_invisalign", "svc_implants", "svc_emergency"])
    kit = landing.kit(conn, biz)
    assert kit["facts"]["service"] == "Invisalign, Implants and Emergency"  # the paragraphs still read all of them
    assert kit["facts"]["lead_services"] == ["Invisalign", "Implants", "Emergency"]
    assert kit["message"]["headline_pattern"] == "Invisalign in Palo Alto"
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/implants")
    for h1, ok in (
        ("Implants in Palo Alto", True),
        ("Invisalign in Palo Alto", True),
        ("A dentist in Palo Alto", None),  # R4: a mismatch on the owner's own page warns, never blocks
    ):
        t = _serve({page["url"]: (200, GOOD.replace("{pid}", page["id"]).replace("Invisalign in Palo Alto", h1))})
        mm = next(r for r in _check(conn, page, kit, t)["rules"] if r["id"] == "message_match")
        assert mm["ok"] is ok, (h1, mm)


def test_kit_is_built_from_the_plan_and_the_recipe(client, conn, fake):  # noqa: F811 (fixture)
    _key, biz, plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    assert kit["plan_id"] == plan_id and kit["tree"] == "dentists"
    f = kit["facts"]
    assert f["name"] == "Bright Smile Dental" and f["phone"] == "650-555-0100" and f["service"] == "Invisalign"
    assert f["area"].startswith("within 5 miles") and "Palo Alto" in f["location"]
    assert kit["message"]["headline_pattern"] == "Invisalign in Palo Alto"
    assert kit["message"]["themes"] and kit["message"]["headlines"]  # the ads' own words
    assert kit["structure"][0]["id"] == "hero"
    assert kit["rules"]["sensitive"] is True and any("privacy" in r.lower() for r in kit["rules"]["google"])
    assert kit["tracking"]["snippet"].startswith('<script async src="http://testserver/t/admiry.js"')
    assert 'data-page="pg_' in kit["tracking"]["snippet"] and "data-gads" not in kit["tracking"]["snippet"]
    assert kit["tracking"]["form_action"].startswith("http://testserver/t/form/pg_")
    # I4: the kit says where the form posts (the script sends only such a form) and what its inputs are named
    assert kit["tracking"]["form"].startswith(
        f'Make the form <form method="post" action="{kit["tracking"]["form_action"]}"> with inputs named '
        "name, phone, service, preferred_time:"
    )
    assert [c["id"] for c in kit["checklist"]][:3] == ["reachable", "mobile", "speed"]
    text = json.dumps(kit)
    assert not JARGON.search(kit["structure"][0]["guide"]) and "campaign" not in text.lower()


def test_the_kit_speaks_with_the_primary_rows_words(client, conn, fake):  # noqa: F811 (fixture)
    """M0 Task 5: a plan funds several channels; the page is built for the primary's ads (Google Search), wherever
    its row sits among them."""
    from psycopg.types.json import Jsonb

    _key, biz, plan_id = _plan(client, conn, fake)
    plan = conn.execute("SELECT json FROM plan WHERE id = %s", (plan_id,)).fetchone()["json"]
    google = plan["sections"]["ads"]["channels"][0]
    newsbreak = {**google, "channel": "newsbreak", "role": "test", "headlines": ["Local news"], "themes": ["news"]}
    plan["sections"]["ads"]["channels"] = [newsbreak, google]
    conn.execute("UPDATE plan SET json = %s WHERE id = %s", (Jsonb(plan), plan_id))
    kit = landing.kit(conn, biz)
    assert (kit["message"]["headlines"], kit["message"]["themes"]) == (google["headlines"], google["themes"])


def test_kit_handles_sparse_knowledge_without_crashing(client, conn, fake):  # noqa: F811 (fixture)
    """No phone, no usp, no pricing hints and an empty services list still build a kit: the consultation's own
    answer (not knowledge.services) supplies the service, and every knowledge-only fact is None/empty instead
    of raising."""
    benchmark.seed(conn)
    fake.knowledge = fake.knowledge.model_copy(update={"phone": None, "usp": [], "services": [], "pricing_hints": []})
    b = _start(client, conn)
    key, sid = b["key"], b["data"]["session_id"]
    _answer(client, key, sid, {"listing": {"choice": "gm_1"}}, "s0")  # the business on Google Maps: centres the ads
    _answer(client, key, sid, {"objective": {"choice": "bookings"}}, "s1")
    _answer(client, key, sid, {"service": {"choice": "svc_invisalign"}}, "s2")
    _answer(client, key, sid, {"customer_value": {"choice": "1000_plus"}}, "s3")
    from agent.tests.conftest import call_tool

    res = call_tool(client, "recommend", {"session_id": sid, "idempotency_key": "sr"}, key=key)
    assert not res["isError"]
    kit = landing.kit(conn, b["data"]["business_id"])
    f = kit["facts"]
    assert f["phone"] is None and f["usp"] == [] and f["pricing_hints"] == []
    assert f["service"] == "Invisalign"  # the consultation's own answer, not knowledge.services
    assert kit["message"]["headline_pattern"] and kit["message"]["headline_pattern"].startswith(f["service"])
    assert kit["tracking"]["snippet"] and "admiry.js" in kit["tracking"]["snippet"]


@pytest.mark.parametrize(
    ("location", "city"),
    [
        ("500 University Ave, Palo Alto, CA 94301, USA", "Palo Alto"),  # as Google Maps formats it
        ("123 Main St, Houston, TX 77002, USA", "Houston"),
        ("500 University Ave, Palo Alto, CA", "Palo Alto"),
        ("Paris, France", "Paris"),  # no state to read: the first part, as before
        ("Palo Alto", "Palo Alto"),
        (None, None),
    ],
)
def test_the_kits_city_is_the_addresss_town(location, city):
    """Walk fixes 1, Task 4 round 1: the page's city is places.town's (the part before the state), not the part before
    the last one, which for an address Google formats is the state and ZIP; a location it cannot read keeps the old
    reading."""
    assert landing._city(location) == city


def test_kit_needs_a_plan(conn):
    biz = "biz_1"
    business_with_key(conn, biz_id=biz)
    with pytest.raises(ToolError, match="Build the plan first"):
        landing.kit(conn, biz)


def test_snippet_carries_the_google_tag_only_when_there_is_one():
    plain = landing.snippet("pg_1", "https://agent.example", None)
    assert plain == '<script async src="https://agent.example/t/admiry.js" data-page="pg_1"></script>'
    tagged = landing.snippet(
        "pg_1", "https://agent.example", {"id": "AW-123", "labels": {"form": "abc", "call": "def"}}
    )
    assert (
        'data-gads="AW-123"' in tagged
        and "data-labels='{&quot;form&quot;: &quot;abc&quot;, &quot;call&quot;: &quot;def&quot;}'" in tagged
    )


def test_google_tag_and_the_snippet_reflect_what_ads_conversions_stored(conn, ads_api, monkeypatch):
    """A3 Task 2 -> A3p-1 integration: once `ads_conversions.ensure` has run, `landing.google_tag` finds the
    account's tag and every label, and `landing.snippet` renders `data-gads` for that business."""
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    labels = {"form": "F1", "call_ad": "CA1", "booked": "B1", "won": "W1", "call": "C1", "book": "BK1", "mail": "M1"}
    http = fake_client(
        [], customer=customer_response(tracking_id="123"), actions=[actions_response({}), actions_response(labels)]
    )
    ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)

    tag = landing.google_tag(conn, "biz_1")
    assert tag == {"id": "AW-123", "labels": labels}

    tagged = landing.snippet("pg_1", "https://agent.example", tag)
    assert 'data-gads="AW-123"' in tagged
    assert "data-labels=" in tagged and "F1" in tagged and "M1" in tagged


def test_owner_url_is_one_page_row_updated_in_place(conn):
    """R11: a business has one owner-page row whatever address it names; a new address (here only a trailing
    slash apart) updates it in place -- same id, so the pasted snippet keeps working -- with the version up by
    one and the old check cleared; the same address changes nothing."""
    biz = "biz_1"
    business_with_key(conn, biz_id=biz)
    first = landing.set_owner_url(conn, biz, "https://BrightSmile.example/invisalign")
    assert first["source"] == "owner" and first["status"] == "published" and first["id"].startswith("pg_")
    # H22 (A3p-2 Task 3 review): hostname is Admiry's hosted hostname only -- never the owner's own host.
    assert first["version"] == 1 and first["hostname"] is None
    assert landing.final_host(conn, biz) == "brightsmile.example"  # derived from url instead
    assert landing.current(conn, biz)["id"] == first["id"]
    landing.save_check(conn, first["id"], {"ok": True, "rules": []})
    again = landing.set_owner_url(conn, biz, "https://BrightSmile.example/invisalign")
    assert again["id"] == first["id"] and again["version"] == 1 and again["check_json"] == {"ok": True, "rules": []}
    second = landing.set_owner_url(conn, biz, "https://BrightSmile.example/invisalign/")
    assert second["id"] == first["id"] and second["url"] == "https://BrightSmile.example/invisalign/"
    assert second["status"] == "published" and second["version"] == 2
    assert second["check_json"] is None and second["checked_at"] is None
    assert conn.execute("SELECT count(*) AS n FROM page WHERE business_id = %s", (biz,)).fetchone()["n"] == 1


def test_set_owner_url_keeps_the_kit_page_id(client, conn, fake):  # noqa: F811 (fixture)
    """R11 (was R5): ensure_page_for_kit's preview row is what the assistant's pasted snippet already points at;
    naming the owner's own URL must publish that same row, not mint an id the snippet and the check will never
    match -- and the first address is version 1, not a replacement."""
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/invisalign")
    assert page["id"] == kit["page_id"] and page["status"] == "published" and page["version"] == 1


def test_two_owner_pages_can_share_one_external_host(conn):
    """H22 (A3p-2 Task 3 review): set_owner_url never writes `hostname` (Admiry's hosted hostname only), so two
    businesses whose own pages happen to share a host -- two "sites.google.com/view/…" pages, say -- never
    collide on `page_hostname_one`'s partial unique index the way they did when this wrote the owner's own host
    into that column too."""
    business_with_key(conn, biz_id="biz_a")
    business_with_key(conn, biz_id="biz_b")
    a = landing.set_owner_url(conn, "biz_a", "https://sites.google.com/view/bright-smile")
    b = landing.set_owner_url(conn, "biz_b", "https://sites.google.com/view/other-biz")
    assert a["hostname"] is None and b["hostname"] is None
    assert (a["status"], b["status"]) == ("published", "published")
    assert landing.final_host(conn, "biz_a") == landing.final_host(conn, "biz_b") == "sites.google.com"


def test_final_host_ignores_a_hostname_a_pre_h22_owner_row_still_carries(conn):
    """H25 (round 2, R7): a row from before H22 -- A3p-1's `set_owner_url` used to write the owner's own domain
    into `hostname` -- is trusted by nothing now, whatever it still carries: `final_host` checks `source` before
    it ever reads the column (H22 alone was not enough, since a row that was never re-keyed can carry a stale
    value forever), so a row like this falls through to deriving the host from `url`, exactly like a row that
    was never hosted at all."""
    biz = "biz_1"
    business_with_key(conn, biz_id=biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/invisalign")
    conn.execute("UPDATE page SET hostname = 'brightsmile.example' WHERE id = %s", (page["id"],))  # what A3p-1 wrote
    landing.set_owner_url(conn, biz, "https://newsite.example/")
    assert landing.final_host(conn, biz) == "newsite.example"


def test_the_boot_scrub_keeps_a_kept_slug_but_still_wipes_a_legacy_one(conn):
    """H27 (round 3, S1b): `schema.sql`'s boot-time scrub of `page.hostname` for a `source = 'owner'` row runs
    at *every* boot (it is schema.sql, applied every session -- there is no separate migrations system), so it
    must tell apart the two reasons an owner row can carry a hostname: H25's own kept slug (a row that went
    hosted, so it has `page_version` history, then came back to owner -- meant to survive, so a later hosted
    republish keeps it) from a pre-H22 legacy row (A3p-1's `set_owner_url` wrote the owner's own domain
    straight into `hostname`; never hosted, so never any `page_version` row). Before the `NOT EXISTS` guard, a
    boot wiped *both* alike -- freeing a business's kept slug for any other business to mint next, while this
    one's own live ads kept pointing at the address that slug used to be (a cross-tenant collision, not merely
    stale data)."""
    kept_biz, legacy_biz = "biz_kept", "biz_legacy"
    business_with_key(conn, biz_id=kept_biz)
    business_with_key(conn, biz_id=legacy_biz)
    kept = landing.set_owner_url(conn, kept_biz, "https://brightsmile.example/invisalign")
    conn.execute(
        "UPDATE page SET hostname = 'bright-smile-dental.business.test' WHERE id = %s", (kept["id"],)
    )  # what a hosted publish minted, before the business came back to its own address (H25)
    conn.execute(
        "INSERT INTO page_version (page_id, version, html, source) VALUES (%s, 1, '<html></html>', 'hosted')",
        (kept["id"],),
    )
    legacy = landing.set_owner_url(conn, legacy_biz, "https://other-example.example/")
    conn.execute("UPDATE page SET hostname = 'other-example.example' WHERE id = %s", (legacy["id"],))  # A3p-1

    db.apply_schema(conn)  # every session's own boot, not just the first one ever

    kept_now = conn.execute("SELECT hostname FROM page WHERE id = %s", (kept["id"],)).fetchone()
    legacy_now = conn.execute("SELECT hostname FROM page WHERE id = %s", (legacy["id"],)).fetchone()
    assert kept_now["hostname"] == "bright-smile-dental.business.test"
    assert legacy_now["hostname"] is None


def test_presence_line_no_longer_promises_a_page_admiry_builds():
    sections, _ = plans.normalize(plan_draft_fixture(), 2000)
    sections["estimate"] = {"low": 0, "high": 0, "basis": "benchmark", "confidence": "low"}  # owner_lines needs it
    # owner words (founder, 2026-10-13): Admiry builds it for the owner; no "kit", no assistant doing Admiry's work
    line = plans.owner_lines(sections)["presence"]
    assert line == "Landing page: yours, or one Admiry builds for you" and copy.jargon(line) is None


# ---------------------------------------------------------------- the check (Task 4)

GOOD = """<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Invisalign in Palo Alto</title></head>
<body><h1>Invisalign in Palo Alto — free consultation</h1><a href="tel:+16505550100" data-admiry="call">Call (650) 555-0100</a>
<p>Bright Smile Dental, Mon-Fri 8-5, same-week appointments, within 5 miles of Palo Alto. Cleanings $120.</p>
<form><input name="name"><input name="phone"><button>Request a call</button></form>
<a href="https://maps.google.com/?q=x">Map</a>
<footer><a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> · (650) 555-0100</footer>
<script async src="http://testserver/t/admiry.js" data-page="{pid}"></script></body></html>"""


def _serve(pages: dict[str, tuple[int, str]]):
    def handler(request):
        status, body = pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})

    return httpx.MockTransport(handler)


TITLES = {  # spec 2026-10-12 §3.5: the owner's words, one source for the card, the e-mail and the MCP report
    "reachable": "Opens securely and fast",
    "mobile": "Works on a phone",
    "speed": "Loads quickly",
    "phone": "Tap-to-call number at the top",
    "one_goal": "Keeps visitors on the page",
    "message_match": "Headline matches the ad",
    "tracking": "Admiry's code is on the page",
    "beacon": "Seen working once",
    "privacy": "Privacy notice by the form",
    "terms": "Terms link in the footer",
    "contact": "Phone or e-mail in the footer",
    "facts": "Real facts about the business",
    "compliance": "No claims the rules forbid",
    "freshness": "Matches your current plan",
}


def test_every_check_has_the_owners_title_and_a_why_and_the_fix_strings_stand():
    assert {c["id"]: c["title"] for c in landing.CHECKLIST} == TITLES
    assert all(c["why"].endswith(".") and not JARGON.search(c["why"]) for c in landing.CHECKLIST)
    assert landing.message_fix({"message": {"headline_pattern": "Roofing in San Rafael"}}) == (
        "Say the service and the city in the headline, like the ad: “Roofing in San Rafael”."
    )


def test_check_passes_a_good_page(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/invisalign")
    conn.execute("UPDATE page SET kit_plan_id = %s WHERE id = %s", (kit["plan_id"], page["id"]))
    conn.execute("INSERT INTO page_event (page_id, kind) VALUES (%s, 'visit')", (page["id"],))
    t = _serve({"https://brightsmile.example/invisalign": (200, GOOD.replace("{pid}", page["id"]))})
    report = landing.check(page["url"], page, kit, conn=conn, transport=t, resolver=lambda h: ["93.184.216.34"])
    failed = [r for r in report["rules"] if r["ok"] is False]
    assert not failed, failed
    ids = [r["id"] for r in report["rules"]]
    assert ids == [c["id"] for c in landing.CHECKLIST]
    # M1 Task 4: fourteen rules, the terms-of-use link and the contact line after privacy
    assert ids == [
        "reachable",
        "mobile",
        "speed",
        "phone",
        "one_goal",
        "message_match",
        "tracking",
        "beacon",
        "privacy",
        "terms",
        "contact",
        "facts",
        "compliance",
        "freshness",
    ]
    warned = {r["id"] for r in report["rules"] if r["ok"] is None}
    # A dental page is a sensitive category (P8): keeping the form needed for the privacy-link rule means
    # "compliance" warns by design here (counsel's sign-off note), not a hard pass; see the dedicated
    # sensitive-form-warning test below. "speed" is tolerated too, for a slow test machine's wall clock.
    # R7: a warning never blocks -- only an outright False on a BLOCKING rule does -- so this warning
    # (compliance is a BLOCKING id) does not keep report["ok"] from being True: a launch needs every row but
    # Speed and Facts to pass; the rest may warn (spec §6).
    assert warned <= {"speed", "compliance"} and "compliance" in warned
    assert report["ok"] is True


def test_check_flags_a_foreign_phone(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    html_ = (
        GOOD.replace("{pid}", page["id"])
        .replace("tel:+16505550100", "tel:+14085550000")
        .replace("(650) 555-0100", "(408) 555-0000")
    )
    report = landing.check(
        page["url"],
        page,
        kit,
        conn=conn,
        transport=_serve({page["url"]: (200, html_)}),
        resolver=lambda h: ["93.184.216.34"],
    )
    phone = next(r for r in report["rules"] if r["id"] == "phone")
    assert phone["ok"] is False and "not the business number" in phone["fix"] and report["ok"] is False


def test_check_warns_about_forms_on_a_sensitive_page(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)  # dentists: sensitive
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    report = landing.check(
        page["url"],
        page,
        kit,
        conn=conn,
        transport=_serve({page["url"]: (200, GOOD.replace("{pid}", page["id"]))}),
        resolver=lambda h: ["93.184.216.34"],
    )
    comp = next(r for r in report["rules"] if r["id"] == "compliance")
    assert comp["ok"] is None and "nothing about health" in comp["detail"]


def test_check_fails_missing_snippet_privacy_and_banned_words(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    html_ = (
        GOOD.replace("{pid}", "pg_other")
        .replace('<a href="/privacy">Privacy</a>', "")
        .replace("free consultation", "painless, guaranteed results")
    )
    report = landing.check(
        page["url"],
        page,
        kit,
        conn=conn,
        transport=_serve({page["url"]: (200, html_)}),
        resolver=lambda h: ["93.184.216.34"],
    )
    by = {r["id"]: r for r in report["rules"]}
    assert by["tracking"]["ok"] is False and "this page's token" in by["tracking"]["fix"]
    assert by["privacy"]["ok"] is False and by["compliance"]["ok"] is False and "painless" in by["compliance"]["detail"]
    assert report["ok"] is False


def test_check_refuses_a_redirect_to_another_site_and_a_private_host(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    t = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://other.example/"}))
    report = landing.check(page["url"], page, kit, conn=conn, transport=t, resolver=lambda h: ["93.184.216.34"])
    assert next(r for r in report["rules"] if r["id"] == "reachable")["ok"] is False
    with pytest.raises(ToolError, match="public address"):
        landing.check("http://127.0.0.1/x", page, kit, conn=conn, transport=t)


def test_check_flags_more_than_one_redirect(client, conn, fake):  # noqa: F811
    """Two chained same-domain redirects exhaust the fetch loop's budget before any page is ever reached: the
    report must still carry a "reachable" row (not silently drop to 11) and say so plainly, not leave every
    later rule to judge an empty page as if it had really been fetched."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    hops = {
        "https://brightsmile.example/x": "https://brightsmile.example/y",
        "https://brightsmile.example/y": "https://brightsmile.example/z",
    }
    t = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": hops[str(r.url)]}))
    report = landing.check(page["url"], page, kit, conn=conn, transport=t, resolver=lambda h: ["93.184.216.34"])
    assert [r["id"] for r in report["rules"]] == [c["id"] for c in landing.CHECKLIST]
    reach = report["rules"][0]
    assert reach["id"] == "reachable" and reach["ok"] is False
    assert "redirects more than once" in reach["detail"] and "final address" in reach["fix"]
    assert report["ok"] is False
    assert report["fetched"]["status"] == 302 and report["fetched"]["final_url"] == "https://brightsmile.example/z"
    # the reachable fix is the first thing an owner is told to fix
    fixes = [r["fix"] for r in report["rules"] if r["ok"] is False and r["id"] in landing.BLOCKING]
    assert fixes and fixes[0] == reach["fix"]


def _check(conn, page, kit, transport, resolver=lambda h: ["93.184.216.34"], **kw):
    return landing.check(page["url"], page, kit, conn=conn, transport=transport, resolver=resolver, **kw)


def test_check_reports_carry_title_and_why_on_every_rule(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    report = _check(conn, page, kit, _serve({page["url"]: (200, GOOD.replace("{pid}", page["id"]))}))
    assert all(r["title"] == TITLES[r["id"]] and r["why"] for r in report["rules"])


def test_message_match_takes_the_service_stem_and_any_area_served():
    kit = {
        "facts": {
            "service": "roofing",
            "lead_services": ["roofing"],
            "city": "San Rafael",
            "areas": ["Marin County", "San Francisco Bay Area"],
        },
        "message": {"headline_pattern": "Roofing in San Rafael", "offer": None},
    }
    assert landing.message_match(kit, "San Francisco Bay Area & Marin County Roof Repair Experts")
    assert landing.message_match(kit, "Roofing in San Rafael")
    assert not landing.message_match(kit, "Marin County Plumbers")  # no service
    assert not landing.message_match(kit, "Roof repair done right")  # no area
    dental = {
        "facts": {"service": "Invisalign", "lead_services": ["Invisalign"], "city": "Palo Alto", "areas": []},
        "message": {"headline_pattern": "Invisalign in Palo Alto", "offer": "Free consultation"},
    }
    assert landing.message_match(dental, "Invisalign: free consultation")  # the offer stands in for the area, as before
    # review fix round 1, Minor 2: the city and an area must be a whole word, not a substring -- a short code like
    # "LA" must not match inside "Atlanta"; "Marin County" (a multi-word area, line 539 above) still matches whole
    la = {
        "facts": {"service": "roofing", "lead_services": ["roofing"], "city": "LA", "areas": ["LA"]},
        "message": {"headline_pattern": "Roofing in LA", "offer": None},
    }
    assert not landing.message_match(la, "Roofing done right in Atlanta")
    assert landing.message_match(la, "LA Roof Repair Experts")
    # review fix round 2: a term that itself ends (or begins) in punctuation has no \b there to anchor on, so
    # "l.a." and "washington, d.c." must still match where they appear verbatim -- a plain \b...\b lost these
    abbreviated = {
        "facts": {"service": "roofing", "lead_services": ["roofing"], "city": "l.a.", "areas": []},
        "message": {"headline_pattern": "Roofing in L.A.", "offer": None},
    }
    assert landing.message_match(abbreviated, "We cover l.a. metro Roof Repair")
    dc = {
        "facts": {"service": "roofing", "lead_services": ["roofing"], "city": "washington, d.c.", "areas": []},
        "message": {"headline_pattern": "Roofing in Washington, D.C.", "offer": None},
    }
    assert landing.message_match(dc, "Now serving Roof Repair in Washington, D.C.")  # the term ends the headline


def test_message_match_warns_on_the_owners_page_and_blocks_on_admirys(client, conn, fake):  # noqa: F811
    """R4: the owner's own page (source owner) gets advice; an Admiry-hosted page is blocked. Both carry the fix."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    off = GOOD.replace("{pid}", page["id"]).replace("Invisalign in Palo Alto", "Welcome to our practice")
    report = _check(conn, page, kit, _serve({page["url"]: (200, off)}))
    mm = next(r for r in report["rules"] if r["id"] == "message_match")
    assert mm["ok"] is None and mm["fix"] == landing.message_fix(kit) and report["ok"] is True
    hosted = {**page, "source": "hosted"}
    report = _check(conn, hosted, kit, _serve({page["url"]: (200, off)}))
    mm = next(r for r in report["rules"] if r["id"] == "message_match")
    assert mm["ok"] is False and report["ok"] is False
    rejudged = landing.judged(report, kit, owner=True)
    assert next(r for r in rejudged["rules"] if r["id"] == "message_match")["ok"] is None  # judged as the owner's


def test_the_check_mail_prints_each_rules_why(client, conn, fake):  # noqa: F811
    """Review fix round 1, Minor 6: `mail_check.txt.j2` (as `tools_pages._mail_report` renders it) prints a `Why:`
    line under each rule, from the `why` `rule()` now carries."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    report = _check(conn, page, kit, _serve({page["url"]: (200, GOOD.replace("{pid}", page["id"]))}))
    marked = [{**r, "mark": "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "!")} for r in report["rules"]]
    text = mail.render("mail_check.txt.j2", report=report, rules=marked, passed=12, total=14, shots=False)
    reachable = next(r for r in report["rules"] if r["id"] == "reachable")
    assert f"{reachable['title']} -- {reachable['detail']}\n   Why: {reachable['why']}" in text


@pytest.mark.parametrize(
    "location, address",
    [
        ("https://internal.brightsmile.example/admin", "10.0.0.5"),
        ("https://meta.brightsmile.example/latest/meta-data/", "169.254.169.254"),
    ],
)
def test_check_never_fetches_a_redirect_to_a_private_address(client, conn, fake, location, address):  # noqa: F811
    """C1: the one allowed hop is vetted like the address itself -- a same-site name that resolves to a private
    or link-local address is refused before anything fetches it."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "brightsmile.example":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, text="<html><body>SECRET</body></html>")

    resolver = {"brightsmile.example": ["93.184.216.34"], httpx.URL(location).host: [address]}.__getitem__
    report = _check(conn, page, kit, httpx.MockTransport(handler), resolver=resolver)
    reach = report["rules"][0]
    assert reach["id"] == "reachable" and reach["ok"] is False and report["ok"] is False
    assert reach["detail"] == "The address forwards to an address we will not fetch (it must be a public address)."
    assert reach["fix"] == "Give the ads the page's final public address."
    assert seen == ["https://brightsmile.example/x"]  # exactly one request: the private address was never contacted


def test_check_survives_a_malformed_redirect_and_a_malformed_link(client, conn, fake):  # noqa: F811
    """I11: `https://[oops` (urlparse raises ValueError on it) as a Location or as a link on the page is a
    rule's finding or a skipped link, never an exception out of the check."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    bounce = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://[oops"}))
    reach = _check(conn, page, kit, bounce)["rules"][0]
    assert reach["ok"] is False and "will not fetch (it must be a valid http(s) address)" in reach["detail"]
    html_ = GOOD.replace("{pid}", page["id"]).replace("</body>", '<a href="https://[oops">x</a></body>')
    report = _check(conn, page, kit, _serve({page["url"]: (200, html_)}))
    assert [r["id"] for r in report["rules"]] == [c["id"] for c in landing.CHECKLIST]
    assert next(r for r in report["rules"] if r["id"] == "one_goal")["ok"] is True


def test_check_reads_a_huge_streamed_page_only_up_to_the_cap(client, conn, fake):  # noqa: F811
    """C2: the body is streamed and cut at MAX_HTML inflated bytes, never read whole first -- neither a 7 MB
    page (reading stops a few chunks past the cap) nor a gzip bomb (100 MB of zeros in ~100 KB, sent as one
    network read: httpx's own decoder would inflate all of it at once). The check asks only for encodings it
    inflates itself."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    asked, pulled = [], []

    def huge():
        yield GOOD.replace("{pid}", page["id"]).replace("</body></html>", "").encode()
        for i in range(1000):
            pulled.append(i)
            yield b"<p>" + b"filler " * 1000 + b"</p>"

    def handler(request):
        asked.append(request.headers["accept-encoding"])
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=huge())

    report = _check(conn, page, kit, httpx.MockTransport(handler))
    assert report["fetched"]["bytes"] == landing.MAX_HTML and asked == ["gzip, deflate"]
    assert len(pulled) < 400  # ~290 chunks of 7 KB reach the 2 MB cap; the other ~700 were never read
    speed = next(r for r in report["rules"] if r["id"] == "speed")
    assert speed["ok"] is None and speed["detail"].startswith("more than 2000 KB")
    assert next(r for r in report["rules"] if r["id"] == "tracking")["ok"] is True  # the head of the page was read

    z = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    bomb = b"".join(z.compress(b"\0" * 1_000_000) for _ in range(100)) + z.flush()
    gz = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, content=iter([bomb])))
    tracemalloc.start()
    try:
        report = _check(conn, page, kit, gz)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert report["fetched"]["bytes"] == landing.MAX_HTML
    assert peak < 20_000_000, peak  # inflated whole, the 100 MB would be in memory at once


def test_check_judges_a_page_whose_body_starts_past_800_kb_from_its_body(client, conn, fake):  # noqa: F811
    """The founder's walk, 2026-10-12: under the 300 KB cap the check saw the <head> alone -- no phone, no footer, the
    <title> as the headline. The phone, the footer and the H1 are judged from the body now; `speed` still warns above
    300 KB and says the size."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    heavy = GOOD.replace("{pid}", page["id"]).replace(
        "<head>", "<head><style>" + "a{color:#000}" * 70_000 + "</style>", 1
    )
    report = _check(conn, page, kit, _serve({page["url"]: (200, heavy)}))
    by_id = {r["id"]: r for r in report["rules"]}
    assert by_id["phone"]["ok"] is True and by_id["tracking"]["ok"] is True and by_id["message_match"]["ok"] is True
    assert by_id["speed"]["ok"] is None and by_id["speed"]["detail"].startswith(f"{len(heavy) // 1024} KB")
    assert report["fetched"]["bytes"] == len(
        heavy.encode()
    )  # GOOD has an em dash and two middle dots: multi-byte UTF-8


def test_check_parses_unclosed_headlines_in_linear_time(client, conn, fake):  # noqa: F811
    """C2: MAX_HTML of unclosed <h1> (the lazy `<h1>(.*?)</h1>` regex took about a minute on 300 KB of it) finishes
    in a few seconds, not minutes; the headline comes from one HTML parser pass. (2026-10-12: MAX_HTML grew to
    2 MB, so this now-larger worst case takes longer in absolute terms, but still linearly.)"""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    t = _serve({page["url"]: (200, "<h1>" * (landing.MAX_HTML // 4))})
    started = time.monotonic()
    report = _check(conn, page, kit, t)
    assert time.monotonic() - started < 5.0
    assert report["fetched"]["bytes"] == landing.MAX_HTML
    mm = next(r for r in report["rules"] if r["id"] == "message_match")
    assert mm["ok"] is None and mm["detail"] == "Headline: “”."  # R4: a warning on the owner's own page


def test_check_catches_banned_words_that_start_with_a_symbol(client, conn, fake):  # noqa: F811
    """`\\b` cannot sit before "#", so "#1" was never caught; (?<!\\w)…(?!\\w) catches it, still as a whole word."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)  # dentists: "#1" and "best" are banned
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    html_ = GOOD.replace("{pid}", page["id"]).replace("Cleanings $120.", "The #1 dentist, bestowed with care.")
    comp = next(
        r for r in _check(conn, page, kit, _serve({page["url"]: (200, html_)}))["rules"] if r["id"] == "compliance"
    )
    assert comp["ok"] is False and comp["detail"].startswith("Banned words: #1.")  # "bestowed" is not "best"


def test_a_callers_extra_rules_are_judged_by_passed_and_the_stored_verdict_stays_the_checks(client, conn, fake, caplog):  # noqa: F811
    """M0 Task 4 (fix round 1): a stored report keeps the blocking rules' verdict only -- `check(extra_rules=…)` never
    rewrites `ok` or a rule, since the report is every channel's. A caller judges its own rules (a channel's
    Capabilities.page_rules) with `passed`: they must pass outright, a warning is not enough; a rule id the check has
    no rule for blocks nothing, with a log line. (M1 Task 4: `terms` and `contact` are rules now -- test_landing_newsbreak.py;
    this test stood for them while the check did not have them, and now uses an id that never will.)"""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/invisalign")
    html_ = GOOD.replace('<meta name="viewport" content="width=device-width">', "").replace("{pid}", page["id"])

    def check(**kw):
        return _check(conn, page, kit, _serve({page["url"]: (200, html_)}), **kw)

    plain = check()
    assert next(r for r in plain["rules"] if r["id"] == "mobile")["ok"] is None and plain["ok"] is True
    with caplog.at_level("INFO", logger="agent.landing"):
        asked = check(extra_rules=("mobile", "terms", "contact", "no_such_rule"))
    assert asked["ok"] is True and asked["rules"] == plain["rules"]  # the verdict and the rules as the check made them
    assert "'no_such_rule'" in caplog.text  # a rule the check has none for
    assert "'terms'" not in caplog.text and "'contact'" not in caplog.text  # the check has these now
    assert landing.passed(plain) is True and landing.passed(plain, ("mobile",)) is False
    assert landing.passed(plain, ("reachable",)) is True
    caplog.clear()
    with caplog.at_level("INFO", logger="agent.landing"):
        assert landing.passed(plain, ("no_such_rule",)) is True  # never ran: blocks nothing
    assert "'no_such_rule'" in caplog.text
    assert landing.passed(plain, ("terms", "contact")) is True  # GOOD has both
    assert landing.passed({**plain, "ok": False}, ()) is False  # a blocking rule failed: nobody launches


def test_check_reads_a_required_line_as_the_laws_wording_never_a_claim(client, conn, fake):  # noqa: F811
    """A3p-2 Task 5: a required line that carries a banned word -- New York's former "Prior results do not guarantee
    a similar outcome" (gone from the attorneys' recipe with New York's rules, 2026-06-01) says their banned
    "guarantee" -- printed verbatim (Admiry's template does), it passes; the same word anywhere else still fails."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    kit["rules"] = {
        **kit["rules"],
        "sensitive": False,
        "banned_words": ["guarantee"],
        "required_lines": [{"text": "Prior results do not guarantee a similar outcome", "when": "NY", "always": True}],
    }
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    line = "<p>Prior results do not guarantee a similar outcome</p></body>"
    html_ = GOOD.replace("{pid}", page["id"]).replace("</body>", line)
    comp = next(
        r for r in _check(conn, page, kit, _serve({page["url"]: (200, html_)}))["rules"] if r["id"] == "compliance"
    )
    assert comp["ok"] is True, comp
    claim = html_.replace("Cleanings $120.", "We guarantee a result.")
    comp = next(
        r for r in _check(conn, page, kit, _serve({page["url"]: (200, claim)}))["rules"] if r["id"] == "compliance"
    )
    assert comp["ok"] is False and comp["detail"].startswith("Banned words: guarantee.")


def test_check_reads_the_business_name_as_a_name_never_a_claim(client, conn, fake):  # noqa: F811
    """The founder's dev walk, 2026-10-11: "Best Plumbing Service" failed its own Admiry-built page on "best". The
    business's own name is a name, never a claim; the same word anywhere else still fails, and the fix names only
    what is wrong."""
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)  # dentists: "best" is banned
    kit["facts"] = {**kit["facts"], "name": "Best Smile Dental"}
    kit["rules"] = {**kit["rules"], "sensitive": False}
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    html_ = GOOD.replace("{pid}", page["id"]).replace("</body>", "<footer>© Best Smile Dental</footer></body>")

    def comp(h):
        report = _check(conn, page, kit, _serve({page["url"]: (200, h)}))
        return next(r for r in report["rules"] if r["id"] == "compliance")

    assert comp(html_)["ok"] is True, comp(html_)
    claimed = comp(html_.replace("Cleanings $120.", "The best cleanings in town."))
    assert claimed["ok"] is False and claimed["detail"].startswith("Banned words: best.")
    assert claimed["fix"] == "Remove the banned words: best."


# ---------------------------------------------------------------- the headline judged against the plan of now
# The founder's dev walk, 2026-10-08: a site published under a plan located at "94306" failed Message-match ("AC
# services in Sunnyvale"); the next version of the plan named Sunnyvale, but the saved report stood -- page-check never
# runs the rule again -- and the launch stayed refused, asking for "AC services in 94306".
def _relocated(conn, biz: str, location: str) -> None:
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,consultation,business,location}', to_jsonb(%s::text)) "
        "WHERE business_id = %s",
        (location, biz),
    )


def test_the_report_keeps_the_headline_it_read(client, conn, fake):  # noqa: F811
    _key, biz, _ = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    report = _check(conn, page, kit, _serve({page["url"]: (200, GOOD.replace("{pid}", page["id"]))}))
    assert report["headline"] == "Invisalign in Palo Alto — free consultation"


@pytest.mark.parametrize("kept", [True, False], ids=["headline-kept", "saved-before-it-was-kept"])
def test_a_saved_report_is_judged_again_against_the_plan_of_now(client, conn, fake, kept):  # noqa: F811
    """A headline that names the city of the plan of now passes Message-match whatever the plan said when the page was
    checked; the saved report itself is left as it was. A report saved before the headline was kept is read from its
    rule's own sentence. R4 (2026-10-12): this is the owner's own page, so a mismatch only ever warns -- `report["ok"]`
    stays True throughout; the rule's own `ok` (None while it mismatches) and `fix` are what move."""
    _key, biz, _ = _plan(client, conn, fake)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    sunnyvale = GOOD.replace("{pid}", page["id"]).replace("Invisalign in Palo Alto", "Invisalign in Sunnyvale")
    report = _check(conn, page, landing.kit(conn, biz), _serve({page["url"]: (200, sunnyvale)}))
    mm0 = next(r for r in report["rules"] if r["id"] == "message_match")
    assert report["ok"] is True and mm0["ok"] is None  # the plan is in Palo Alto; a warning, not a block
    if not kept:
        report.pop("headline")
    landing.save_check(conn, page["id"], report)
    _relocated(conn, biz, "837 S Wolfe Rd, Sunnyvale, CA 94086, USA")
    judged = landing.judged_report(conn, landing.current(conn, biz))
    mm = next(r for r in judged["rules"] if r["id"] == "message_match")
    assert judged["ok"] is True and mm["ok"] is True and mm["fix"] is None
    assert landing.saved_report(landing.current(conn, biz))["ok"] is True  # nothing written
    _relocated(conn, biz, "500 University Ave, Palo Alto, CA 94301, USA")  # and back: it warns again, as it should
    again = landing.judged_report(conn, landing.current(conn, biz))
    mm = next(r for r in again["rules"] if r["id"] == "message_match")
    assert again["ok"] is True and mm["ok"] is None
    assert mm["fix"] == "Say the service and the city in the headline, like the ad: “Invisalign in Palo Alto”."


def test_a_plan_located_at_a_bare_zip_names_the_business_s_town(client, conn, fake):  # noqa: F811
    """A ZIP code is no city to write in a headline: the kit names the business's own town (its Google Maps listing's
    address) instead, and the check asks for that."""
    _key, biz, _ = _plan(client, conn, fake)
    _relocated(conn, biz, "94301")
    kit = landing.kit(conn, biz)
    assert kit["facts"]["city"] == "Palo Alto" and kit["message"]["headline_pattern"] == "Invisalign in Palo Alto"


def test_message_of_reads_areas_as_whole_entries_zips_dropped_town_added_deduped():
    """Review fix round 1 (Important 1, Minor 3): `service_area` may be stored as one text or a list -- either way
    `_message_of` reads whole entries through `profile._listed`, never a text's own letters (the bug: iterating a
    bare string split it into one-character "areas"); a ZIP code is no area, whether it is a served place or the
    listing's own town; the city itself and a duplicate are left out."""
    consultation = {
        "business": {"service": "Roofing", "services": ["Roofing"], "location": "Palo Alto, CA"},
        "offer": None,
    }
    # one text, not a list: read as one area, not split into letters; the listing's town (also Palo Alto) is the
    # plan's own city, so it is left out
    knowledge = {
        "service_area": "Marin County",
        "listing": {"address": "500 University Ave, Palo Alto, CA 94301, USA"},
    }
    assert landing._message_of(consultation, knowledge)["facts"]["areas"] == ["Marin County"]

    # a list with a ZIP among the served places, and a bare-ZIP listing address: both ZIPs are dropped, the city
    # (Palo Alto, from the served places this time) is still excluded
    knowledge = {
        "service_area": ["Marin County", "94301", "San Francisco", "Palo Alto"],
        "listing": {"address": "94301"},
    }
    assert landing._message_of(consultation, knowledge)["facts"]["areas"] == ["Marin County", "San Francisco"]

    # the same area twice, once as a served place and once as the listing's own town: one entry, not two
    knowledge = {"service_area": ["Marin County", "Marin County"], "listing": {"address": "Marin County"}}
    assert landing._message_of(consultation, knowledge)["facts"]["areas"] == ["Marin County"]
