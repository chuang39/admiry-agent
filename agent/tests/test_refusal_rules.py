"""spec 2026-10-12-admiry-google-refusal-knowledge-design §2.2, §2.3 (Part B phase 1): the rule table, the fixed set
of pattern shapes, one `local_rules.check`, the switch; then (Tasks 2 and 3) the build, the Reviewer's stamp, the
checklist's items and the plan writer's words to avoid."""

import re

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_refusal, blockers, config, copy, db, launch_check, launch_review, llm, local_rules, plans
from agent.channels.google_search import adapter as google
from agent.channels.google_search import build as google_build
from agent.tests.conftest import JARGON
from agent.tests.surface_helpers import planned
from agent.tests.test_ads_refusal import detail
from agent.tests.test_ads_run import (  # noqa: F401 -- `gads` is a fixture used by name
    BIZ,
    DESCRIPTIONS,
    HEADLINES,
    KNOWLEDGE,
    add_plan,
    gads,
    seed,
)
from agent.tests.test_checklist_fixes import _follow, _gone, _item
from agent.tests.test_never_elsewhere import ELSEWHERE
from agent.tests.test_review_launch import draft_sections
from agent.tests.test_roles_reviewer import (  # noqa: F401 -- `on` and `page_ok` are fixtures used by name
    FOUR,
    _detail,
    _google,
    _in_review_with,
    _mails,
    _model,
    _refusal,
    _run,
    on,
    page_ok,
)


def rule(conn, value, *, kind="word", applies_to="any", level="block", status="active", why="a word Google refuses"):
    """One refusal_rule row, by hand (an admin's), as /ops/rules will write it."""
    rule_id = db.new_id("rr")
    conn.execute(
        "INSERT INTO refusal_rule (id, kind, applies_to, value, why, level, status, proposed_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (rule_id, kind, applies_to, value, why, level, status, "admin:founder@example.com"),
    )
    return rule_id


@pytest.fixture
def rules_on(monkeypatch):
    monkeypatch.setenv("AGENT_REFUSAL_RULES", "on")


# ---------------------------------------------------------------- §2.2 the table, §4 the switch


def test_the_schema_applies_twice_and_a_rules_row_cascades_off_the_refusal(conn):
    db.apply_schema(conn)
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    rule_id = rule(conn, "deal")
    (ar,) = ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(detail("headline", "Invisalign Deal"), "ad_text", "dropped")],
    )
    conn.execute("UPDATE ads_refusal SET rule_id = %s WHERE id = %s", (rule_id, ar))
    conn.execute("DELETE FROM refusal_rule WHERE id = %s", (rule_id,))
    assert conn.execute("SELECT rule_id FROM ads_refusal WHERE id = %s", (ar,)).fetchone()["rule_id"] is None


def test_one_live_rule_per_key_and_a_rejected_one_does_not_hold_the_key(conn):
    import psycopg

    rule(conn, "deal", applies_to="headline")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        rule(conn, "deal", applies_to="headline", status="proposed")
    rule(conn, "deal", applies_to="headline", status="rejected")  # a decided one leaves the key free
    assert conn.execute("SELECT count(*) AS n FROM refusal_rule").fetchone()["n"] == 2


def test_the_switch_reads_as_the_other_switches_do(monkeypatch):
    assert config.load().refusal_rules is False
    for raw, want in (("", False), ("off", False), ("0", False), ("on", True), (" ON ", True), ("1", True)):
        monkeypatch.setenv("AGENT_REFUSAL_RULES", raw)
        assert config.load().refusal_rules is want, raw
    monkeypatch.setenv("AGENT_REFUSAL_RULES", "validate")
    with pytest.raises(ValueError, match="AGENT_REFUSAL_RULES must be on or off"):
        config.load()


# ---------------------------------------------------------------- the rule's value


@pytest.mark.parametrize(
    ("kind", "raw", "stored"),
    [
        ("word", "  Best  DEAL ", "best deal"),
        ("word", "mom's", "mom's"),
        ("word", "same-day", "same-day"),
        ("pattern", "Repeated_Symbols", "repeated_symbols"),
    ],
)
def test_a_value_is_stored_lower_cased_and_collapsed(kind, raw, stored):
    assert local_rules.validate(kind, raw) == stored


@pytest.mark.parametrize(
    ("kind", "raw", "sentence"),
    [
        ("word", "x", local_rules.BAD_WORD),
        ("word", "a" * 41, local_rules.BAD_WORD),
        ("word", "100", local_rules.BAD_WORD),
        ("word", "deal!", local_rules.BAD_WORD),
        ("word", "(?i)deal", local_rules.BAD_WORD),
        ("word", "the", local_rules.STOP_WORD),
        ("pattern", "[!?]{2,}", local_rules.BAD_SHAPE.format(shapes=", ".join(local_rules.SHAPES))),
        ("regex", "deal", local_rules.BAD_KIND),
    ],
)
def test_a_value_the_rules_refuse(kind, raw, sentence):
    with pytest.raises(ValueError, match=re.escape(sentence)):
        local_rules.validate(kind, raw)


# ---------------------------------------------------------------- the shapes (D1): bounded, in code, never from input


@pytest.mark.parametrize(
    ("shape", "hit", "clean"),
    [
        ("repeated_symbols", "Call now!!", "Call now!"),
        ("repeated_symbols", "Save $$ today", "Save $50 today"),
        ("symbol_in_word", "$ave on braces", "Save on braces"),
        ("spaced_letters", "S A L E this week", "Sale this week"),
        ("number_one", "#1 dentist in town", "Open 1 day a week"),
        ("number_one", "No. 1 in Palo Alto", "No 10% fee"),
        ("ellipsis", "Call today...", "Call today."),
        ("web_address", "Book at brightsmile.com", "Book with Bright Smile"),
        ("web_address", "See www.example.org", "See the example"),
        ("all_caps_run", "BOOK NOW TODAY", "Book NOW today"),
    ],
)
def test_each_shape_matches_its_tell_and_not_a_clean_line(shape, hit, clean):
    regex = local_rules.SHAPES[shape].regex
    assert regex.search(hit) is not None
    assert regex.search(clean) is None


def test_the_shapes_are_the_seven_of_d1_each_with_a_tell():
    assert list(local_rules.SHAPES) == [
        "repeated_symbols",
        "symbol_in_word",
        "spaced_letters",
        "number_one",
        "ellipsis",
        "web_address",
        "all_caps_run",
    ]
    assert all(s.tell and s.id == key for key, s in local_rules.SHAPES.items())


# ---------------------------------------------------------------- §2.3 one check


def _rule(value, *, kind="word", applies_to="any", level="warn", rule_id="rr_1"):
    return local_rules.compile_rule(
        {
            "id": rule_id,
            "kind": kind,
            "applies_to": applies_to,
            "value": value,
            "level": level,
            "why": "why",
            "topic": None,
        }
    )


def test_a_word_rule_matches_whole_words_in_any_case_where_it_applies():
    headline_only = _rule("deal", applies_to="headline")
    lines = [("headline", "Best DEAL in town"), ("headline", "Dealers welcome"), ("description", "A deal for you")]
    found = local_rules.check(lines, [headline_only])
    assert [(f.line_kind, f.text, f.rule_id, f.level) for f in found] == [
        ("headline", "Best DEAL in town", "rr_1", "warn")
    ]


def test_a_phrase_rule_and_a_symbol_led_word_are_whole_matches_too():
    assert local_rules.check([("description", "the best deal around")], [_rule("best deal")])
    assert local_rules.check([("headline", "#1 dentist")], [_rule("#1")])
    assert not local_rules.check([("headline", "dentist #10")], [_rule("#1")])


def test_one_finding_per_line_and_block_outranks_warn():
    warn = _rule("deal", level="warn", rule_id="rr_warn")
    block = _rule("repeated_symbols", kind="pattern", level="block", rule_id="rr_block")
    found = local_rules.check([("headline", "Deal!! today"), ("headline", "Deal today")], [warn, block])
    assert [(f.rule_id, f.level, f.text) for f in found] == [
        ("rr_block", "block", "Deal!! today"),
        ("rr_warn", "warn", "Deal today"),
    ]
    assert found[0].value == "repeated_symbols" and found[1].value == "deal"


def test_a_pattern_rule_on_search_phrases_matches_phrases_alone():
    found = local_rules.check(
        [("keyword", "braces www.example.com"), ("headline", "See www.example.com")],
        [_rule("web_address", kind="pattern", applies_to="keyword", level="block")],
    )
    assert [f.line_kind for f in found] == ["keyword"]


def test_no_rules_no_findings():
    assert local_rules.check([("headline", "Deal!!")], []) == []


# ---------------------------------------------------------------- load and the switch; stamp; with_rules; tells; view


def test_load_reads_active_rules_only_and_nothing_while_the_switch_is_off(conn, monkeypatch):
    rule(conn, "deal", level="warn")
    rule(conn, "repeated_symbols", kind="pattern", level="block")
    rule(conn, "proposed", status="proposed")
    rule(conn, "retired", status="retired")
    assert local_rules.load(conn, config.load()) == []
    monkeypatch.setenv("AGENT_REFUSAL_RULES", "on")
    loaded = local_rules.load(conn, config.load())
    assert [(r.value, r.level) for r in loaded] == [("deal", "warn"), ("repeated_symbols", "block")]
    assert [r.value for r in local_rules.block_rules(conn, config.load())] == ["repeated_symbols"]
    assert all(r.id.startswith("rr_") for r in loaded)


def test_stamp_writes_the_matching_rule_on_the_refusal_rows(conn, rules_on):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    deal = rule(conn, "deal", level="warn")
    ids = ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[
            (detail("headline", "Invisalign Deal"), "ad_text", "dropped"),
            (detail("headline", "Clear Aligners"), "ad_text", "dropped"),
            (detail("url", None, topic="DESTINATION_NOT_WORKING"), "destination", "held"),
        ],
    )
    assert local_rules.stamp(conn, ids, local_rules.load(conn, config.load())) == 1
    rows = conn.execute("SELECT text, rule_id FROM ads_refusal ORDER BY created_at, id").fetchall()
    assert [(r["text"], r["rule_id"]) for r in rows] == [
        ("Invisalign Deal", deal),
        ("Clear Aligners", None),
        (None, None),
    ]
    assert local_rules.stamp(conn, ids, []) == 0


def test_with_rules_puts_the_block_rules_alone_on_dropped():
    from agent.channels.google_search import build as google_build

    rules = [_rule("deal", level="warn"), _rule("ellipsis", kind="pattern", level="block", rule_id="rr_b")]
    out = local_rules.with_rules(google_build.NOTHING_DROPPED, rules)
    assert [r.id for r in out.rules] == ["rr_b"] and out.headlines == frozenset()
    assert local_rules.with_rules(google_build.NOTHING_DROPPED, []).rules == ()


def test_tells_are_the_words_to_avoid_and_view_is_the_admins_row(conn):
    assert local_rules.tells([_rule("best deal"), _rule("ellipsis", kind="pattern")]) == [
        "best deal",
        local_rules.SHAPES["ellipsis"].tell,
    ]
    rule(conn, "ellipsis", kind="pattern")
    row = conn.execute(f"SELECT {local_rules.COLS} FROM refusal_rule").fetchone()
    shown = local_rules.view(row)
    assert shown["tell"] == local_rules.SHAPES["ellipsis"].tell and shown["decided_at"] is None
    assert isinstance(shown["created_at"], str)


# ---------------------------------------------------------------- Task 2: the build leaves a block rule's line out (D3)


def _block(value, *, kind="word", applies_to="any", rule_id="rr_block"):
    return _rule(value, kind=kind, applies_to=applies_to, level="block", rule_id=rule_id)


def _refuse(conn, plan_id, kind, text, *, topic="TRADEMARKS_IN_AD_TEXT", cls="ad_text", channel="google_search") -> str:
    """One dropped row for the business, as the Reviewer records it."""
    return ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel=channel,
        review_id=None,
        found=[(detail(kind, text, topic=topic), cls, "dropped")],
    )[0]


def test_kept_lines_leaves_out_a_block_rules_line_as_it_leaves_out_a_refused_one():
    dropped = google_build.Dropped(headlines=frozenset({"Clear Aligners Near You"}), rules=(_block("deal"),))
    kept_h, kept_d, notes = google_build.kept_lines(
        ["Invisalign Deal", "Same-Week Appointments", "Clear Aligners Near You"],
        ["A deal for you.", "Book online."],
        [],
        dropped,
    )
    assert kept_h == ["Same-Week Appointments"] and kept_d == ["Book online."] and notes == []
    assert dropped and dropped.holds("keyword", "invisalign deal") and not dropped.holds("keyword", "braces")
    assert google_build.Dropped(rules=(_block("deal"),))  # rules alone make it truthy
    warn_only = google_build.Dropped(rules=(_rule("deal", level="warn"),))
    assert not warn_only.holds("headline", "Invisalign Deal")  # only a block rule ever holds a line


def test_the_search_phrases_leave_out_a_block_rules_phrase(conn):
    _key, plan_id = seed(
        conn, status="draft", secs=draft_sections(themes=("invisalign palo alto", "cheap braces deal"))
    )
    plan = plans.get(conn, BIZ, plan_id)
    dropped = google_build.Dropped(rules=(_block("deal", applies_to="keyword"),))
    built = google_build.built_keywords(plan, KNOWLEDGE, dropped)
    assert "invisalign palo alto" in built and "cheap braces deal" not in built
    assert "cheap braces deal" in google_build.built_keywords(plan, KNOWLEDGE)


def test_dropped_with_settings_carries_the_block_rules_only_while_the_switch_is_on(conn, monkeypatch):
    rule(conn, "deal", level="block")
    rule(conn, "cheap", level="warn")
    assert ads_refusal.dropped(conn, BIZ, settings=config.load()).rules == ()
    monkeypatch.setenv("AGENT_REFUSAL_RULES", "on")
    assert [r.value for r in ads_refusal.dropped(conn, BIZ, settings=config.load()).rules] == ["deal"]
    assert ads_refusal.dropped(conn, BIZ).rules == ()  # no settings: the exact texts alone, as Part A reads it


# ---------------------------------------------------------------- what a draft carries, what is flagged, the words to avoid


def test_carried_is_what_the_draft_would_send_by_kind(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    out = ads_refusal.carried(conn, BIZ, plans.get(conn, BIZ, plan_id))
    assert out["headline"] == HEADLINES and out["description"] == DESCRIPTIONS
    assert "invisalign palo alto" in out["keyword"]
    secs = draft_sections()
    secs["ads"]["channels"][0]["monthly_usd"] = 0  # a $0 entry: no build could run it, nothing is carried
    assert ads_refusal.carried(conn, BIZ, plans.get(conn, BIZ, add_plan(conn, secs, status="draft"))) == {}


def test_flagged_splits_the_exact_refused_lines_from_the_rule_findings(conn, rules_on):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"]))
    plan = plans.get(conn, BIZ, plan_id)
    _refuse(conn, plan_id, "headline", "Same-Week Appointments")
    rule(conn, "deal", level="warn")
    rule(conn, "appointments", level="block")  # the refused line matches it too: it is exact, never a finding as well
    exact, found = ads_refusal.flagged(conn, config.load(), BIZ, plan)
    assert exact == [("headline", "Same-Week Appointments")]
    assert [(f.line_kind, f.text, f.level) for f in found] == [("headline", "Invisalign Deal", "warn")]


def test_flagged_is_empty_for_a_draft_with_no_runnable_entry_and_sees_no_rule_while_off(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"]))
    rule(conn, "deal", level="block")
    assert ads_refusal.flagged(conn, config.load(), BIZ, plans.get(conn, BIZ, plan_id)) == ([], [])
    secs = draft_sections()
    secs["ads"]["channels"][0]["monthly_usd"] = 0
    assert ads_refusal.flagged(conn, config.load(), BIZ, plans.get(conn, BIZ, add_plan(conn, secs))) == ([], [])


def test_why_for_is_the_earliest_drops_sentence_and_avoid_words_lead_with_the_business_own_lines(conn, rules_on):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    _refuse(conn, plan_id, "headline", "Same-Week Appointments")
    _refuse(conn, plan_id, "keyword", "invisalign palo alto", topic=None, cls="keyword")
    _refuse(conn, plan_id, "headline", "Same-Week Appointments", topic=None)  # a later, plainer row: the first wins
    rule(conn, "deal", level="warn")
    rule(conn, "ellipsis", kind="pattern", level="block")
    assert ads_refusal.why_for(conn, BIZ, "headline", "Same-Week Appointments") == "a name Google treats as a trademark"
    assert ads_refusal.why_for(conn, BIZ, "headline", "never refused") == ads_refusal.WHY_BY_CLASS["other"]
    assert ads_refusal.avoid_words(conn, config.load(), BIZ) == [
        "Same-Week Appointments",
        "invisalign palo alto",
        "deal",
        local_rules.SHAPES["ellipsis"].tell,
    ]


def test_why_for_reads_google_search_alone_like_dropped_and_avoid_words(conn):
    """Ruling Y1: `why_for`'s query carried no channel filter, unlike `dropped` and `avoid_words` -- a row recorded
    for another channel with the same (kind, text) must never answer for a Google Search line."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    _refuse(conn, plan_id, "headline", "Evening Hours", cls="other", channel="newsbreak")
    assert ads_refusal.why_for(conn, BIZ, "headline", "Evening Hours") == ads_refusal.WHY_BY_CLASS["other"]
    _refuse(conn, plan_id, "headline", "Evening Hours", cls="ad_text")  # the google_search row: now it answers
    assert ads_refusal.why_for(conn, BIZ, "headline", "Evening Hours") == ads_refusal.why(
        "ad_text", "TRADEMARKS_IN_AD_TEXT"
    )


def test_avoid_words_are_the_business_own_lines_alone_while_the_switch_is_off_and_capped(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    for n in range(45):
        _refuse(conn, plan_id, "headline", f"Line {n}")
    rule(conn, "deal", level="warn")
    out = ads_refusal.avoid_words(conn, config.load(), BIZ)
    assert len(out) == local_rules.AVOID_MAX and out[0] == "Line 0" and "deal" not in out


# ---------------------------------------------------------------- the Reviewer (§2.3): the stamp, and the block rule on every build


def test_the_reviewer_stamps_the_rule_a_refused_line_matched_and_carries_the_block_rules(
    conn,
    on,  # noqa: F811 - on is a fixture used by name
    rules_on,
    gads,  # noqa: F811 - gads is a fixture used by name
    page_ok,  # noqa: F811 - page_ok is a fixture used by name
    monkeypatch,
):
    _model(monkeypatch)
    _mails(monkeypatch)
    deal = rule(conn, "deal", level="warn")
    rule(conn, "ellipsis", kind="pattern", level="block")
    calls = _google(monkeypatch, _refusal(_detail(text="Invisalign Deal")), None)
    review = _in_review_with(conn, headlines=[*FOUR, "Invisalign Deal", "Call today..."])
    _run(conn)
    assert launch_review.get(conn, review["id"])["state"] == "passed"
    assert [r.value for r in calls[0].payload["dropped"].rules] == ["ellipsis"]  # on the first build already
    assert calls[1].payload["dropped"].headlines == frozenset({"Invisalign Deal"})
    row = conn.execute("SELECT text, rule_id FROM ads_refusal").fetchone()
    assert (row["text"], row["rule_id"]) == ("Invisalign Deal", deal)


def test_off_the_reviewer_stamps_nothing(conn, on, gads, page_ok, monkeypatch):  # noqa: F811 - fixtures used by name
    _model(monkeypatch)
    _mails(monkeypatch)
    rule(conn, "deal", level="warn")
    _google(monkeypatch, _refusal(_detail(text="Invisalign Deal")), None)
    _in_review_with(conn, headlines=[*FOUR, "Invisalign Deal"])
    _run(conn)
    assert conn.execute("SELECT rule_id FROM ads_refusal").fetchone()["rule_id"] is None


# ---------------------------------------------------------------- Task 3: the checklist (§2.1 the business's own lines; §2.3 the rules)


def _check(conn) -> dict | None:
    return launch_check.check(conn, config.load(), BIZ)


def _items(conn) -> dict[str, dict]:
    return {i["id"]: i for i in _check(conn)["items"]}


def test_a_refused_line_still_carried_makes_an_item_with_the_rewrite_fix_not_blocking_when_enough_remains(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"]))
    _refuse(conn, plan_id, "headline", "Invisalign Deal")
    out = _check(conn)
    (item,) = out["items"]
    assert item["id"] == "refused_line" and item["blocking"] is False and out["ready"] is True
    assert item["text"] == "Google refused “Invisalign Deal” before: a name Google treats as a trademark."
    assert item["note"] == launch_check.WARN_NOTE and item["suggestion"] == launch_check.SUGGESTIONS["write_copy"]
    assert (item["kind"], item["resolution"], item["actions"]) == ("fix", "write_copy", [blockers.WRITE_COPY])
    assert (item["channel"], item["label"], item["question"]) == ("google_search", "Google Search", None)
    public = launch_check.public(conn, config.load(), BIZ)["items"][0]
    assert public["fix"] == {
        "tool": "update_marketing_plan",
        "args": {"message": blockers.COPY_INSTRUCTION},
        "ask_owner": None,
        "label": blockers.WRITE_COPY["label"],
    }
    assert public["problem"] == item["text"] and public["blocking"] is False


def test_a_refused_line_blocks_when_leaving_it_out_leaves_too_little(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())  # exactly 3 headlines: one gone is too few
    _refuse(conn, plan_id, "headline", "Same-Week Appointments")
    out = _check(conn)
    (item,) = out["items"]
    assert item["blocking"] is True and item["note"] is None and out["ready"] is False
    assert launch_check.blocked(conn, config.load(), BIZ, plan_id) == item["text"]
    assert not google.copy_short(plans.get(conn, BIZ, plan_id))  # the linter alone is content: this item blocks


def test_when_the_linter_alone_leaves_too_little_the_linters_item_blocks_and_the_refused_line_only_names(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=HEADLINES[:2]))
    _refuse(conn, plan_id, "headline", HEADLINES[0])
    items = _items(conn)
    assert items["write_copy"]["blocking"] is True and items["refused_line"]["blocking"] is False


def test_two_refused_lines_are_two_items_in_the_drafts_order(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "A", "B"]))
    _refuse(conn, plan_id, "headline", "B")
    _refuse(conn, plan_id, "description", DESCRIPTIONS[1])
    _refuse(conn, plan_id, "headline", "A")
    assert [(i["id"], i["text"].split("“")[1].split("”")[0]) for i in _check(conn)["items"]] == [
        ("refused_line", "A"),
        ("refused_line-2", "B"),
        ("refused_line-3", DESCRIPTIONS[1]),
    ]


def test_a_rewritten_line_is_new_text_and_makes_no_item(conn):
    """Review Focus 3: the owner changed one word; Part A's exclusion is by exact text, so is the item."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    _refuse(conn, plan_id, "headline", "Same-Week Appointment")
    assert _check(conn)["items"] == []


def test_a_warn_rule_lists_a_non_blocking_item_and_a_block_rule_a_blocking_one(conn, rules_on):
    _key, _plan_id = seed(
        conn,
        status="draft",
        secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"], descriptions=[*DESCRIPTIONS, "Call today..."]),
    )
    deal = rule(conn, "deal", level="warn", why="a word Google often reads as a price claim")
    dots = rule(conn, "ellipsis", kind="pattern", level="block", why="a run of dots")
    items = _items(conn)
    assert list(items) == [f"rule:{deal}", f"rule:{dots}"]
    warn, block = items[f"rule:{deal}"], items[f"rule:{dots}"]
    assert (
        warn["text"]
        == "Google often refuses headlines like “Invisalign Deal”: a word Google often reads as a price claim."
    )
    assert warn["blocking"] is False and warn["note"] == launch_check.WARN_NOTE
    assert block["text"] == "Google often refuses descriptions like “Call today...”: a run of dots."
    assert block["blocking"] is True and block["note"] is None
    assert all(i["resolution"] == "write_copy" and i["actions"] == [blockers.WRITE_COPY] for i in items.values())


def test_a_rule_on_a_search_phrase_lists_and_never_blocks(conn, rules_on):
    _key, _plan_id = seed(conn, status="draft", secs=draft_sections(themes=("invisalign palo alto", "braces deal")))
    deal = rule(conn, "deal", applies_to="keyword", level="block")
    (item,) = _check(conn)["items"]
    assert item["id"] == f"rule:{deal}" and item["blocking"] is False
    assert item["text"] == "Google often refuses search phrases like “braces deal”: a word Google refuses."


def test_a_keyword_only_item_carries_no_write_copy_action_and_the_gate_still_answers_in_place(conn, rules_on):
    """Ruling BB1: write_copy never touches search phrases (they come from the themes), so a keyword-only finding
    offers no WRITE_COPY action -- suggestion text only -- and the gate (blockers.write_copy) still answers
    COPY_IN_PLACE for a draft with no headline or description flagged, keyword finding or not."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(themes=("invisalign palo alto", "braces deal")))
    deal = rule(conn, "deal", applies_to="keyword", level="block")
    item = _items(conn)[f"rule:{deal}"]
    assert item["actions"] == [] and item["resolution"] == "write_copy"
    assert item["suggestion"] == launch_check.SUGGESTIONS["write_copy"]
    with pytest.raises(ToolError, match=re.escape(blockers.COPY_IN_PLACE)):
        blockers.write_copy(config.load(), BIZ, plan_id, email="owner@example.com", ip=None)


def test_a_headline_flagged_item_is_unchanged_by_the_keyword_fix(conn, rules_on):
    _key, _plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"]))
    deal = rule(conn, "deal", level="warn", why="a word Google often reads as a price claim")
    (item,) = _check(conn)["items"]
    assert item["id"] == f"rule:{deal}"
    assert item["actions"] == [blockers.WRITE_COPY] and item["resolution"] == "write_copy"


def test_a_block_rule_that_leaves_too_little_blocks_the_refused_line_item_too(conn, rules_on):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"]))
    _refuse(conn, plan_id, "headline", "Same-Week Appointments")
    rule(conn, "deal", level="block")
    items = _items(conn)
    assert items["refused_line"]["blocking"] is True  # 4 - 1 refused - 1 blocked = 2 < 3


def test_off_the_rules_do_nothing_and_the_refused_line_item_still_shows(conn):
    """Review Focus 2: the switch gates rules alone (D6)."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections(headlines=[*HEADLINES, "Invisalign Deal"]))
    _refuse(conn, plan_id, "headline", "Same-Week Appointments")
    rule(conn, "deal", level="block")
    items = _items(conn)
    assert list(items) == ["refused_line"] and items["refused_line"]["blocking"] is False
    assert not ads_refusal.dropped(conn, BIZ, settings=config.load()).holds("headline", "Invisalign Deal")
    assert ads_refusal.avoid_words(conn, config.load(), BIZ) == ["Same-Week Appointments"]
    assert ads_refusal.carried(conn, BIZ, plans.get(conn, BIZ, plan_id))["headline"] == [*HEADLINES, "Invisalign Deal"]


def test_every_new_sentence_is_owner_words():
    lines = [
        launch_check.REFUSED_TEXT.format(line="x", why="y"),
        launch_check.WARN_NOTE,
        *(t.format(line="x", why="y") for t in launch_check.RULE_TEXT.values()),
    ]
    for text in lines:
        assert not JARGON.search(text) and not ELSEWHERE.search(text) and copy.jargon(text) is None, text


# ---------------------------------------------------------------- the rewrite fix clears the item (§5: 2.1), on the home and over MCP


def _with_refused_line(client, conn) -> tuple[dict, dict]:
    """The dental walk's consulted draft with one more headline, which Google refused before."""
    walk = planned(client, conn)
    plan = plans.latest(conn, "biz_1")
    heads = plan["sections"]["ads"]["channels"][0]["headlines"]
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,ads,channels,0,headlines}', %s) WHERE id = %s",
        (Jsonb([*heads, "Call Now Dental Deals"]), plan["id"]),
    )
    _refuse(conn, plan["id"], "headline", "Call Now Dental Deals")
    return walk, plans.latest(conn, "biz_1")


def test_an_assistant_following_the_fix_clears_the_refused_line_item(client, conn, walk_fake):
    walk, plan = _with_refused_line(client, conn)
    assert not google.copy_short(plan)  # a complete draft: only the refused line is wrong with it
    item = _item(client, walk["key"], "refused_line")
    assert item["fix"]["args"] == {"message": blockers.COPY_INSTRUCTION}
    _follow(client, walk["key"], item)
    _gone(client, walk["key"], "refused_line")
    assert walk_fake.plan_calls[-1]["avoid"] == ["Call Now Dental Deals"]
    assert "Call Now Dental Deals" not in plans.latest(conn, "biz_1")["sections"]["ads"]["channels"][0]["headlines"]


def test_the_homes_rewrite_runs_on_a_complete_draft_with_a_refused_line_and_never_writes_it_back(
    client, conn, walk_fake
):
    _walk, plan = _with_refused_line(client, conn)
    walk_fake.draft.ads[0].headlines.append("Call Now Dental Deals")  # the model writes it back: the filter drops it
    written = blockers.write_copy(config.load(), "biz_1", plan["id"], email="owner@example.com", ip=None)
    heads = plans.get(conn, "biz_1", written["id"])["sections"]["ads"]["channels"][0]["headlines"]
    assert written["version"] == plan["version"] + 1 and "Call Now Dental Deals" not in heads and len(heads) >= 3
    assert walk_fake.plan_calls[-1]["avoid"] == ["Call Now Dental Deals"]
    assert not [
        i
        for i in launch_check.check(conn, config.load(), "biz_1")["items"]
        if i["id"].startswith(("refused_line", "rule:"))
    ]


def test_the_homes_rewrite_still_refuses_a_draft_nothing_is_wrong_with(client, conn, walk_fake):
    planned(client, conn)
    plan = plans.latest(conn, "biz_1")
    with pytest.raises(ToolError, match=re.escape(blockers.COPY_IN_PLACE)):
        blockers.write_copy(config.load(), "biz_1", plan["id"], email="owner@example.com", ip=None)


# ---------------------------------------------------------------- the prompt (§2.3)


def test_the_prompt_carries_the_words_to_avoid_only_when_given(monkeypatch):
    from agent.tests.fixtures import plan_draft_fixture

    gen = llm.Generator(client=None, model="m")
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(gen, "_parse", lambda system, user, fmt: seen.append((system, user)) or plan_draft_fixture())
    gen.draft_plan(KNOWLEDGE, {"monthly_limit_usd": 500})
    gen.draft_plan(KNOWLEDGE, {"monthly_limit_usd": 500}, avoid=["Call Now Dental Deals", "deal"])
    (plain_system, plain_user), (system, user) = seen
    assert llm.AVOID_RULE not in plain_system and "<avoid>" not in plain_user
    assert system == f"{plain_system}\n{llm.AVOID_RULE}"
    assert "<avoid>" in user and "Call Now Dental Deals" in user and user.startswith(plain_user)
