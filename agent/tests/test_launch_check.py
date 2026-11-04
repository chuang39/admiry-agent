"""L1 Task 2 (spec 2026-10-10 §4.1, §4.2): the checklist -- every reason the latest draft cannot launch yet, at once,
each with what Admiry suggests and its field or one-tap fix (B0's resolutions) -- for test_ads_run's ready business."""

from psycopg.types.json import Jsonb

from agent import ads_run, blockers, config, landing, launch_check, launch_review, plans
from agent.tests.test_ads_run import BIZ, CHECK_OK, PHONE_FIX, _check_failed, _knowledge, _no_link, seed
from agent.tests.test_review_launch import draft_sections


def _check(conn) -> dict | None:
    return launch_check.check(conn, config.load(), BIZ)


def test_a_ready_draft_has_an_empty_list(conn):
    seed(conn, status="draft", secs=draft_sections())
    assert _check(conn) == {"plan_id": plans.latest(conn, BIZ)["id"], "items": [], "ready": True}


def test_every_problem_is_listed_at_once_in_the_launchs_order(conn):
    seed(conn, status="draft", secs=draft_sections(headlines=["Invisalign in Palo Alto"]))
    _knowledge(conn, phone=None)
    _check_failed(conn)
    out = _check(conn)
    assert [i["id"] for i in out["items"]] == ["write_copy", "phone", "page:phone", "page:tracking"]
    assert out["ready"] is False and all(i["blocking"] for i in out["items"])
    copy, phone, page = out["items"][:3]
    assert copy["actions"] == [blockers.WRITE_COPY] and copy["suggestion"] == launch_check.SUGGESTIONS["write_copy"]
    assert phone["kind"] == "ask" and phone["text"] == ads_run.NO_PHONE and phone["actions"] == []
    assert phone["question"] == {"input": "tel", "placeholder": "The business phone, with its area code"}
    assert page["text"] == ads_run.PAGE_FIX.format(fix=PHONE_FIX) and page["actions"] == [blockers.CHECK_AGAIN]
    assert page["suggestion"] == launch_check.CHECK_AGAIN_SUGGESTION and page["plan_id"] == out["plan_id"]


def test_the_money_and_the_account_are_not_on_the_list(conn):
    seed(conn, status="draft", secs=draft_sections())
    _no_link(conn)  # the approval would refuse NOT_LINKED; the list leaves it to Agree and pay / Agree and launch
    assert _check(conn)["items"] == []


def test_the_switch_never_hides_a_problem(conn):
    seed(
        conn, status="draft", secs=draft_sections()
    )  # conftest leaves every AGENT_EXEC_* and AGENT_ADS_EXECUTION unset
    _knowledge(conn, phone=None)
    assert [i["id"] for i in _check(conn)["items"]] == ["phone"]


def test_a_secondary_places_own_rule_is_listed_and_does_not_block(conn):
    secs = draft_sections()
    secs["ads"]["channels"].append(
        {"channel": "newsbreak", "monthly_usd": 300.0, "headlines": [], "descriptions": [], "themes": []}
    )
    seed(conn, status="draft", secs=secs)
    rules = [r for r in CHECK_OK["rules"] if r["id"] != "terms"] + [
        {"id": "terms", "ok": False, "fix": landing.TERMS_FIX}
    ]
    conn.execute("UPDATE page SET check_json = %s", (Jsonb({**CHECK_OK, "rules": rules}),))
    out = _check(conn)
    assert [(i["id"], i["blocking"], i["label"]) for i in out["items"]] == [
        ("page:terms@newsbreak", False, "NewsBreak")
    ]
    assert out["items"][0]["note"] == launch_check.CAN_WAIT.format(label="NewsBreak") and out["ready"] is True


def test_a_plan_with_no_google_search_has_every_item_blocking(conn):
    """D4's edge (review Finding 2a): NewsBreak alone, with no Google Search in the plan at all -- its own page rule
    blocks, unlike the same rule beside Google Search (test_a_secondary_places_own_rule_is_listed_and_does_not_block,
    `blocking` False there)."""
    secs = draft_sections()
    secs["ads"]["channels"] = [
        {"channel": "newsbreak", "monthly_usd": 300.0, "headlines": [], "descriptions": [], "themes": []}
    ]
    seed(conn, status="draft", secs=secs)
    rules = [r for r in CHECK_OK["rules"] if r["id"] != "terms"] + [
        {"id": "terms", "ok": False, "fix": landing.TERMS_FIX}
    ]
    conn.execute("UPDATE page SET check_json = %s", (Jsonb({**CHECK_OK, "rules": rules}),))
    out = _check(conn)
    assert [(i["id"], i["blocking"], i["label"], i["note"]) for i in out["items"]] == [
        ("page:terms@newsbreak", True, "NewsBreak", None)
    ]
    assert out["ready"] is False


def test_an_asks_id_is_its_bare_resolution_even_from_a_secondary_place(conn):
    """review Finding 1 / ruling R10: a phone missing for a secondary place alone -- no Google Search in the plan at
    all -- is still the bare id "phone": B0's readers (blockers.read_answer, .SHAPES, ._saved, .read_typed) key on
    the id unmodified, so the old rule's `@newsbreak` suffix left it unanswerable in words or in the form (POST
    /api/blocker 500: KeyError on blockers.SHAPES[id]). The item's own `channel` and `label` still name the place."""
    secs = draft_sections()
    secs["ads"]["channels"] = [
        {"channel": "newsbreak", "monthly_usd": 300.0, "headlines": [], "descriptions": [], "themes": []}
    ]
    seed(conn, status="draft", secs=secs)
    _knowledge(conn, phone=None)
    out = _check(conn)
    phone = next(i for i in out["items"] if i["resolution"] == "phone")
    assert (phone["id"], phone["channel"], phone["label"]) == ("phone", "newsbreak", "NewsBreak")
    assert phone["kind"] == "ask" and phone["blocking"] is True


def test_two_refusals_no_row_knows_collide_as_other_and_other_2(conn):
    """review Finding 2b: two places Admiry has no adapter for at all (REPORT_ONLY, a refusal no row knows) collide
    on `_item_id`'s "other" and are told apart by the suffix."""
    secs = draft_sections()
    secs["ads"]["channels"] += [
        {"channel": "meta", "monthly_usd": 200.0, "headlines": [], "descriptions": [], "themes": []},
        {"channel": "nextdoor", "monthly_usd": 200.0, "headlines": [], "descriptions": [], "themes": []},
    ]
    seed(conn, status="draft", secs=secs)
    out = _check(conn)
    assert [(i["id"], i["channel"], i["blocking"], i["text"]) for i in out["items"]] == [
        ("other", None, True, ads_run.REPORT_ONLY.format(label="Facebook & Instagram")),
        ("other-2", None, True, ads_run.REPORT_ONLY.format(label="Nextdoor")),
    ]
    assert out["ready"] is False


def test_a_refusal_two_places_share_is_google_searchs_item(conn):
    """review Finding 1 / ruling R9: a page rule failing for both places is Google Search's item -- its id, label and
    channel -- whatever the plan's own channel order, never the secondary's just because the plan lists it first."""
    secs = draft_sections()
    secs["ads"]["channels"] = [
        {"channel": "newsbreak", "monthly_usd": 300.0, "headlines": [], "descriptions": [], "themes": []},
        secs["ads"]["channels"][0],
    ]
    seed(conn, status="draft", secs=secs)
    _check_failed(conn)
    out = _check(conn)
    assert [(i["id"], i["channel"], i["label"], i["blocking"]) for i in out["items"]] == [
        ("page:phone", "google_search", "Google Search", True),
        ("page:tracking", "google_search", "Google Search", True),
    ]
    assert out["ready"] is False


def test_no_list_for_a_plan_that_is_no_draft(conn):
    seed(conn, status="approved", secs=draft_sections())
    assert _check(conn) is None


def test_blocked_is_the_first_blocking_problem_or_a_moved_plan(conn):
    seed(conn, status="draft", secs=draft_sections(radius_miles=None))
    _knowledge(conn, phone=None)
    plan_id = plans.latest(conn, BIZ)["id"]
    assert launch_check.blocked(conn, config.load(), BIZ, plan_id) == ads_run.NO_PHONE
    assert launch_check.blocked(conn, config.load(), BIZ, "pl_0000000000000000") == blockers.PLAN_MOVED
    assert launch_check.first_ask(conn, config.load(), BIZ)["id"] == "phone"
    _knowledge(conn)  # the phone back: the miles are the question now
    assert launch_check.first_ask(conn, config.load(), BIZ)["id"] == "radius"
    assert launch_check.blocked(conn, config.load(), BIZ, plan_id) == ads_run.NO_RADIUS


# ---------------------------------------------------------------- spec 2026-10-11 §5 (plan D5): a returned note with its fix


def _returned(conn, note: str, resolution: str | None, *, biz: str = BIZ) -> str:
    plan_id = plans.latest(conn, biz)["id"]
    review_id = launch_review.submit(conn, biz, plan_id, approval={"kind": "web"})["review_id"]
    launch_review.decide(conn, review_id, state="returned", by="reviewer", note=note, resolution=resolution)
    return plan_id


def test_a_returned_note_with_the_phones_resolution_is_the_phone_ask(conn):
    seed(conn, status="draft", secs=draft_sections())
    _returned(
        conn,
        "Google did not accept the phone number your ads would show; give Admiry the number your customers call.",
        "phone",
    )
    out = _check(conn)
    (item,) = out["items"]
    assert (item["id"], item["kind"], item["resolution"], item["blocking"]) == ("phone", "ask", "phone", False)
    assert item["text"].startswith("Admiry looked at your ads before they went live: Google did not accept")
    assert item["question"] == {"input": "tel", "placeholder": "The business phone, with its area code"}
    assert item["actions"] == [] and item["suggestion"] == launch_check.SUGGESTIONS["phone"] and out["ready"] is True
    assert launch_check.first_ask(conn, config.load(), BIZ)["id"] == "phone"


def test_a_returned_note_with_the_page_checks_resolution_offers_the_check(conn):
    seed(conn, status="draft", secs=draft_sections())
    _returned(
        conn, "Google could not use your page for these ads as it is; fix the page, then check it again.", "check_page"
    )
    (item,) = _check(conn)["items"]
    assert (item["id"], item["kind"], item["resolution"]) == ("review_note", "fix", "check_page")
    assert item["actions"] == [blockers.CHECK_NOW] and item["question"] is None
    assert item["suggestion"] == launch_check.SUGGESTIONS["check_page"]


def test_an_admins_note_and_a_note_beside_the_plain_ask_stay_plain_notes(conn):
    seed(conn, status="draft", secs=draft_sections())
    _returned(conn, "Name your licence.", None)
    (item,) = _check(conn)["items"]
    assert (item["id"], item["kind"], item["resolution"], item["actions"], item["question"]) == (
        "review_note",
        "note",
        None,
        [],
        None,
    )
    assert item["suggestion"] == launch_check.NOTE_SUGGESTION
    # the phone gone from the business after a phone return: the plain ask asks it; the note stays a note
    seed(conn, biz="biz_2", cid="2222222222", status="draft", secs=draft_sections(), email="two@example.com")
    conn.execute("UPDATE knowledge SET json = json - 'phone' WHERE business_id = 'biz_2'")
    _returned(conn, "Give Admiry the number.", "phone", biz="biz_2")
    out = launch_check.check(conn, config.load(), "biz_2")
    assert [(i["id"], i["kind"]) for i in out["items"]] == [("review_note", "note"), ("phone", "ask")]


def test_a_phone_resolution_note_stands_beside_a_distinct_blocking_item(conn):
    """Ruling F1's third item: a returned note naming `phone` beside a DIFFERENT blocking item (here `radius`, no
    collision) -- the note stays the bare `phone` ask, and the other item keeps its own id."""
    seed(conn, status="draft", secs=draft_sections(radius_miles=None))
    _returned(conn, "Give Admiry the number.", "phone")
    out = _check(conn)
    assert [(i["id"], i["kind"], i["resolution"], i["blocking"]) for i in out["items"]] == [
        ("phone", "ask", "phone", False),
        ("radius", "ask", "radius", True),
    ]
    assert out["ready"] is False


def test_a_stored_resolution_outside_the_list_is_read_as_the_plain_note(conn):
    """Ruling F1: launch_review.decide never writes a resolution outside launch_review.RESOLUTIONS, but a row set by
    hand (or by an older version of the code) might -- the checklist reads it as the plain note instead of routing
    it to blockers.controls, which would raise KeyError on an unknown resolution (`_controls`'s own `fixed[...]`)."""
    seed(conn, status="draft", secs=draft_sections())
    plan_id = _returned(conn, "Name your licence.", "check_page")
    conn.execute("UPDATE launch_review SET resolution = 'nope' WHERE plan_id = %s", (plan_id,))
    (item,) = _check(conn)["items"]
    assert (item["id"], item["kind"], item["resolution"]) == ("review_note", "note", None)
    assert item["actions"] == [] and item["question"] is None and item["suggestion"] == launch_check.NOTE_SUGGESTION


# ---------------------------------------------------------------- task review (fix round 1): launch_check.fix_for


def test_fix_for_never_carries_a_headline_placeholder_with_no_ask_owner(conn):
    """Ruling H2b (replaces H2's check-again fallback): the page_fix branch offers Admiry's own page's headline fix
    (FIX_HEADLINE present) only when `landing.headline_pattern` can compute it -- which needs a consulted plan.
    Without one (no plan at all here), `fix_for` gives `fix: null`: never a `<…>` placeholder with `ask_owner` None,
    and never the check-again fix either -- re-checking an unchanged page only fails again, which would loop an
    assistant that follows "fix, then read the state again"; the item's own suggestion (HEADLINE_SUGGESTION) already
    says what Admiry does, and the assistant relays the problem and the suggestion instead (as the public words say
    for any fix-less item)."""
    seed(conn, status="draft", secs=draft_sections())
    conn.execute(
        "UPDATE page SET sections = %s WHERE business_id = %s", (Jsonb({"copy": {"headline": "placeholder"}}), BIZ)
    )
    conn.execute("DELETE FROM plan WHERE business_id = %s", (BIZ,))  # no consulted plan: headline_pattern is None
    assert landing.headline_pattern(conn, BIZ) is None
    assert blockers.admiry_page(conn, landing.current(conn, BIZ)) == "starter"
    item = {"resolution": "page_fix", "actions": [blockers.FIX_HEADLINE]}
    assert launch_check.fix_for(conn, BIZ, item) is None


def test_fix_for_returns_copies_mutating_one_never_changes_the_next(conn):
    """Ruling H3: `fix_for` used to return ASK_ARGS's own nested dict by reference -- an in-process caller filling in
    the owner's answer would carry it into every later business's fix. Each call now gets its own deep copy."""
    seed(conn, status="draft", secs=draft_sections())
    _knowledge(conn, phone=None)
    item = {"resolution": "phone", "actions": []}
    first = launch_check.fix_for(conn, BIZ, item)
    first["args"]["patch"]["locations"][0]["phone"] = "650-555-0100"  # an assistant filling in the owner's answer
    second = launch_check.fix_for(conn, BIZ, item)
    assert second["args"] == {"patch": {"locations": [{"phone": "<the number customers call>"}]}}
    assert launch_check.ASK_ARGS["phone"] == {"patch": {"locations": [{"phone": "<the number customers call>"}]}}


# ---------------------------------------------------------------- re-review (fix round 3): ruling I4


def test_headline_pattern_collapses_stray_whitespace_so_the_pending_fix_advances_to_publish(conn):
    """Ruling I4: `_message_of` (landing.py) filters a consultation's services on `s.strip()` but keeps the
    un-stripped `s`, so a service written with extra spaces ("  Invisalign  ") used to build a headline_pattern
    that could never byte-match page_builder.edit's own collapsed words (`_line`: `" ".join(text.split())`) --
    `fix_for`'s pending-preview check (ruling I3) would then never see a match and kept offering the edit forever.
    `headline_pattern` now collapses its own return the same way, so the two sides are built alike."""
    secs = draft_sections()
    secs["consultation"]["business"]["services"] = ["  Invisalign  "]
    seed(conn, status="draft", secs=secs)
    headline = landing.headline_pattern(conn, BIZ)
    assert headline == "Invisalign in Palo Alto"  # collapsed: no doubled or edge whitespace left over
    page = landing.current(conn, BIZ)
    conn.execute(  # admiry_page: "starter" once sections.copy is set (a minimal one -- fix_for never reads it)
        "UPDATE page SET sections = %s WHERE id = %s", (Jsonb({"copy": {"headline": "placeholder"}}), page["id"])
    )
    conn.execute(  # the edit already made this preview -- its own words carry the same, already-collapsed headline
        "INSERT INTO page_preview (token, page_id, html, title, sections, expires_at) VALUES "
        "(%s, %s, 'x', 'x', %s, now() + interval '1 hour')",
        ("pv_i4test1", page["id"], Jsonb({"copy": {"headline": headline}})),
    )
    item = {"resolution": "page_fix", "actions": [blockers.FIX_HEADLINE]}
    assert launch_check.fix_for(conn, BIZ, item) == {
        "tool": "update_landing_page",
        "args": {"action": "publish", "token": "pv_i4test1"},
        "ask_owner": None,
        "label": "Publish the page",
    }
