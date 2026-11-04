"""spec 2026-10-11 §6, §5.1: every refusal Google answers with, recorded with what Admiry did; what every Google build
of the business leaves out (ruling L2); the owner's words for what Admiry removed -- the drops in effect for a version
(never Google's own words or codes, never another business's text)."""

import logging
import re

import psycopg
import pytest

from agent import ads_refusal, copy, db, plans
from agent.channels.google_search import build as google_build
from agent.channels.google_search.client import RefusalDetail
from agent.tests.conftest import JARGON
from agent.tests.test_ads_run import BIZ, DESCRIPTIONS, add_plan, seed
from agent.tests.test_review_launch import draft_sections

RAW = re.compile(r"POLICY_FINDING|policyFindingError|TRADEMARKS_IN_AD_TEXT")  # Google's own words never reach an owner


def detail(kind: str, text: str | None, *, topic: str | None = "TRADEMARKS_IN_AD_TEXT") -> RefusalDetail:
    return RefusalDetail(
        code="policyFindingError: POLICY_FINDING",
        operation=5,
        kind=kind,
        position=None,
        field=f"mutate_operations[5].{kind}",
        topic=topic,
        type="PROHIBITED",
        exemptible=False,
        certificate=False,
        text=text,
    )


def test_the_schema_applies_twice_and_the_rows_cascade_with_the_plan(conn):
    db.apply_schema(conn)
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    ids = ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(detail("headline", "Invisalign Deals"), "ad_text", "dropped")],
    )
    assert len(ids) == 1 and ids[0].startswith("ar_")
    row = conn.execute("SELECT * FROM ads_refusal").fetchone()
    assert (row["business_id"], row["plan_id"], row["channel"], row["review_id"]) == (
        BIZ,
        plan_id,
        "google_search",
        None,
    )
    assert (row["code"], row["topic"], row["type"], row["kind"], row["text"]) == (
        "policyFindingError: POLICY_FINDING",
        "TRADEMARKS_IN_AD_TEXT",
        "PROHIBITED",
        "headline",
        "Invisalign Deals",
    )
    assert (row["class"], row["action"]) == ("ad_text", "dropped") and row["created_at"] is not None
    conn.execute("DELETE FROM plan WHERE id = %s", (plan_id,))
    assert conn.execute("SELECT count(*) AS n FROM ads_refusal").fetchone()["n"] == 0


def test_an_unknown_class_or_action_is_a_programming_error(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    for cls, action in (("ads", "dropped"), ("ad_text", "deleted")):
        try:
            ads_refusal.record(
                conn,
                business_id=BIZ,
                plan_id=plan_id,
                channel="google_search",
                review_id=None,
                found=[(detail("headline", "x"), cls, action)],
            )
        except ValueError:
            continue
        raise AssertionError(f"{cls}/{action} was recorded")


def test_a_refusal_is_recorded_whole_or_not_at_all(conn):
    """Ruling E1: every (class, action) in `found` is checked before anything is written -- a later tuple's bad
    class leaves no row behind for an earlier, valid tuple in the same call either."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    try:
        ads_refusal.record(
            conn,
            business_id=BIZ,
            plan_id=plan_id,
            channel="google_search",
            review_id=None,
            found=[
                (detail("headline", "Invisalign Deals"), "ad_text", "dropped"),
                (detail("keyword", "cheap braces"), "ads", "dropped"),  # "ads" is not a class ads_refusal takes
            ],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("an invalid tuple did not raise")
    assert conn.execute("SELECT count(*) AS n FROM ads_refusal").fetchone()["n"] == 0


def test_dropped_lists_what_every_build_of_the_business_leaves_out_by_kind(conn):
    """Ruling L2 (final review): what every Google build of the business leaves out -- each line Google refused and
    Admiry dropped, by its exact text and kind, whichever version it was refused in; never a held or a returned row,
    never another channel's."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    later = add_plan(conn, draft_sections(), status="draft")
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[
            (detail("headline", "Invisalign Deals"), "ad_text", "dropped"),
            (detail("description", "Best aligners!"), "ad_text", "dropped"),
            (detail("keyword", "invisalign deals"), "keyword", "dropped"),
            (detail("headline", "Kept Headline"), "ad_text", "held"),  # held, not dropped: still sent
            (detail("url", "https://x.example"), "destination", "returned"),
        ],
    )
    ads_refusal.record(  # a later version's own drop counts for every build of the business too
        conn,
        business_id=BIZ,
        plan_id=later,
        channel="google_search",
        review_id=None,
        found=[(detail("keyword", "invisalign cost", topic=None), "keyword", "dropped")],
    )
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=later,
        channel="newsbreak",
        review_id=None,
        found=[(detail("headline", "Another Place's Line"), "ad_text", "dropped")],
    )
    out = ads_refusal.dropped(conn, BIZ)
    assert out == google_build.Dropped(
        headlines=frozenset({"Invisalign Deals"}),
        descriptions=frozenset({"Best aligners!"}),
        keywords=frozenset({"invisalign deals", "invisalign cost"}),
    )
    assert bool(out) and not google_build.NOTHING_DROPPED
    assert ads_refusal.dropped(conn, BIZ, channel="newsbreak") == google_build.Dropped(
        headlines=frozenset({"Another Place's Line"})
    )


def test_a_change_is_read_for_its_own_business_and_the_versions_own_lines(conn):
    """Review Focus 4: an owner's own ad text is never shown to another owner -- every read is by business; and a later
    version that no longer carries the line Google refused lists nothing for it (ruling L2)."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    other = add_plan(conn, draft_sections(), status="draft")  # its headlines never held "Invisalign Deals"
    seed(conn, biz="biz_2", cid="2222222222", status="draft", secs=draft_sections(), email="other@example.com")
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(detail("headline", "Invisalign Deals"), "ad_text", "dropped")],
    )
    assert len(ads_refusal.changes(conn, BIZ, plan_id)) == 1
    assert ads_refusal.changes(conn, BIZ, other) == [] and ads_refusal.changes(conn, "biz_2", plan_id) == []
    assert ads_refusal.dropped(conn, "biz_2") == google_build.NOTHING_DROPPED


def test_a_drop_stays_in_effect_for_a_later_version_that_still_carries_its_line(conn):
    """Ruling L2 (final review): `changes` lists the drops in effect for the version, oldest first -- its own, and an
    earlier version's whose line it would still send (a headline or a description its Google Search entry still
    holds, a search phrase its build still makes), at that drop's own time. Once a later version rewrote the line it
    is no longer listed; an earlier version's list never takes a later one's drops; another business's rows never
    appear, either way."""
    _key, v1 = seed(conn, status="superseded", secs=draft_sections())
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=v1,
        channel="google_search",
        review_id=None,
        found=[
            (detail("headline", "Same-Week Appointments"), "ad_text", "dropped"),
            (detail("keyword", "invisalign cost", topic=None), "keyword", "dropped"),
        ],
    )
    _key2, two = seed(
        conn, biz="biz_2", cid="2222222222", status="draft", secs=draft_sections(), email="other@example.com"
    )
    ads_refusal.record(  # another business's drop of a line this business's v2 carries too
        conn,
        business_id="biz_2",
        plan_id=two,
        channel="google_search",
        review_id=None,
        found=[(detail("headline", "Clear Aligners Near You"), "ad_text", "dropped")],
    )
    v2 = add_plan(conn, draft_sections(monthly_usd=600.0), status="draft")  # the same lines, another budget

    def whats(plan_id: str, biz: str = BIZ) -> list[str]:
        return [c["what"] for c in ads_refusal.changes(conn, biz, plan_id)]

    first = ads_refusal.changes(conn, BIZ, v1)
    assert whats(v2) == ["the headline “Same-Week Appointments”", "the search phrase “invisalign cost”"]
    assert [c["when"] for c in ads_refusal.changes(conn, BIZ, v2)] == [c["when"] for c in first]
    # v2's own drop joins its list, oldest first -- and never v1's
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=v2,
        channel="google_search",
        review_id=None,
        found=[(detail("description", DESCRIPTIONS[1]), "ad_text", "dropped")],
    )
    assert whats(v2) == [
        "the headline “Same-Week Appointments”",
        "the search phrase “invisalign cost”",
        f"the description “{DESCRIPTIONS[1]}”",
    ]
    assert ads_refusal.changes(conn, BIZ, v1) == first
    # v3: the owner rewrote the headline and no longer asks for the phrase -- neither is in effect for it any more
    secs = draft_sections(headlines=["Invisalign in Palo Alto", "Same-Day Appointments", "Clear Aligners Near You"])
    secs["rationale"]["example_searches"] = ["invisalign palo alto"]
    v3 = add_plan(conn, secs, status="draft")
    assert whats(v3) == [f"the description “{DESCRIPTIONS[1]}”"]
    # business isolation: biz_2's drop never shows here, and none of this business's shows there
    assert whats(two, "biz_2") == ["the headline “Clear Aligners Near You”"]
    assert ads_refusal.changes(conn, "biz_2", v2) == [] and ads_refusal.changes(conn, "biz_2", v3) == []
    assert ads_refusal.dropped(conn, BIZ).headlines == frozenset({"Same-Week Appointments"})


def test_a_zero_google_entry_inherits_no_drop_but_keeps_its_own(conn):
    """Ruling N1 (final review): `_still_sent` picks the version's Google Search entry the way a build does
    (`google_build.search_channel`) -- a version whose entry is $0 could never be sent to Google, so it is not
    `_still_sent`, and it lists no earlier version's drop under "Changes Admiry made" even when it still carries
    the same headline text; its own rows, if any, still show."""
    _key, v1 = seed(conn, status="superseded", secs=draft_sections())
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=v1,
        channel="google_search",
        review_id=None,
        found=[(detail("headline", "Same-Week Appointments"), "ad_text", "dropped")],
    )
    v2 = add_plan(conn, draft_sections(monthly_usd=0.0), status="draft")  # same headlines, but $0 -- never sent
    assert ads_refusal.changes(conn, BIZ, v2) == []
    ads_refusal.record(  # v2's own drop still shows, even though it inherits none of v1's
        conn,
        business_id=BIZ,
        plan_id=v2,
        channel="google_search",
        review_id=None,
        found=[(detail("description", "Best aligners!"), "ad_text", "dropped")],
    )
    assert [c["what"] for c in ads_refusal.changes(conn, BIZ, v2)] == ["the description “Best aligners!”"]


def test_a_version_that_cannot_be_read_lists_its_own_drops_and_never_raises(conn, monkeypatch, caplog):
    """Ruling L2: `changes` is a page's read -- when what the version would still send cannot be read, it lists the
    version's own drops alone (logged), never raising."""
    _key, v1 = seed(conn, status="superseded", secs=draft_sections())
    v2 = add_plan(conn, draft_sections(), status="draft")
    for plan_id, found in ((v1, detail("headline", "Same-Week Appointments")), (v2, detail("description", "x"))):
        ads_refusal.record(
            conn,
            business_id=BIZ,
            plan_id=plan_id,
            channel="google_search",
            review_id=None,
            found=[(found, "ad_text", "dropped")],
        )

    def unreadable(*a, **k):
        raise psycopg.OperationalError("the database went away")

    monkeypatch.setattr(plans, "get", unreadable)
    with caplog.at_level(logging.WARNING, logger="agent.ads_refusal"):
        assert [c["what"] for c in ads_refusal.changes(conn, BIZ, v2)] == ["the description “x”"]
    assert "could not be read: OperationalError" in caplog.text


def test_the_owners_words_name_the_line_and_a_plain_why_never_googles(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[
            (detail("headline", "Invisalign Deals"), "ad_text", "dropped"),
            (detail("keyword", "cheap braces", topic=None), "keyword", "dropped"),
        ],
    )
    first, second = ads_refusal.changes(conn, BIZ, plan_id)
    assert first == {
        "what": "the headline “Invisalign Deals”",
        "why": "a name Google treats as a trademark",
        "when": first["when"],
    }
    assert second["what"] == "the search phrase “cheap braces”" and second["why"] == "Google's rules for search phrases"
    assert first["when"] <= second["when"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", first["when"])
    assert ads_refusal.line(first) == "Removed the headline “Invisalign Deals”: a name Google treats as a trademark."
    one = ads_refusal.say([first])
    assert (
        one
        == "Admiry removed one part of your ads that Google refused (a name Google treats as a trademark); the rest stay as you agreed."
    )
    assert (
        ads_refusal.say([first, second])
        == "Admiry removed 2 parts of your ads that Google refused; the rest stay as you agreed."
    )
    assert ads_refusal.say([]) is None
    for text in (first["what"], first["why"], second["why"], ads_refusal.line(first), one, ads_refusal.INTRO):
        assert not JARGON.search(text) and not RAW.search(text), text
    assert (
        ads_refusal.what("description", None) == "a description" and ads_refusal.why("other", None) == "Google's rules"
    )


def test_a_line_dropped_twice_is_one_change(conn):
    """A headline two policy topics both blocked is recorded as two rows (one per topic, D13's "at least one per
    error") but is one change on the owner's Changes list, at its earliest why."""
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[
            (detail("headline", "Invisalign Deals", topic="TRADEMARKS_IN_AD_TEXT"), "ad_text", "dropped"),
            (detail("headline", "Invisalign Deals", topic="SOME_OTHER_POLICY"), "ad_text", "dropped"),
            (detail("description", "Best aligners!"), "ad_text", "dropped"),
        ],
    )
    out = ads_refusal.changes(conn, BIZ, plan_id)
    assert len(out) == 2
    assert (
        out[0]["what"] == "the headline “Invisalign Deals”" and out[0]["why"] == "a name Google treats as a trademark"
    )
    assert out[1]["what"] == "the description “Best aligners!”"


def test_the_admin_reads_a_reviews_rows_whole(conn):
    _key, plan_id = seed(conn, status="draft", secs=draft_sections())
    conn.execute(
        "INSERT INTO launch_review (id, plan_id, business_id) VALUES ('lr_0000000000000001', %s, %s)", (plan_id, BIZ)
    )
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id="lr_0000000000000001",
        found=[(detail("headline", "Invisalign Deals"), "ad_text", "dropped")],
    )
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(detail("budget", None, topic=None), "other", "held")],
    )
    (row,) = ads_refusal.for_review(conn, "lr_0000000000000001")
    assert {"id", "code", "topic", "type", "field", "kind", "text", "class", "action", "created_at"} <= set(row)
    assert row["text"] == "Invisalign Deals" and row["class"] == "ad_text" and isinstance(row["created_at"], str)
    assert ads_refusal.for_review(conn, "lr_0000000000000009") == []


@pytest.mark.parametrize("cls", ads_refusal.CLASSES)
def test_every_classs_fallback_why_is_plain_and_never_googles(cls):
    """`WHY_BY_CLASS`'s own sentence, for a topic no `WHY_BY_TOPIC` entry matches -- all five classes, not just the
    ones the other tests happen to exercise (ad_text's trademark case, keyword's, other's)."""
    text = ads_refusal.why(cls, "SOME_UNLISTED_POLICY_TOPIC")
    assert copy.jargon(text) is None and not RAW.search(text), text
