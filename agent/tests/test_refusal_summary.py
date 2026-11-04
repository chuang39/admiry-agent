"""spec 2026-10-12 §2.4 (Part B phase 1): a day of ads_refusal rows and the live disapprovals ads-sync keeps become one
refusal_summary row -- counts, the lines refused, the rules' hits and misses -- and up to five proposed rules from one
model call under the Analyst's budget, each re-verified in code (plan D13, D14); with no model, the counts alone."""

from datetime import UTC, date, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from agent import ads_refusal, config, jobs, llm, local_rules, models, refusal_summary
from agent.roles import analyst
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_ads_refusal import detail
from agent.tests.test_ads_run import BIZ, add_plan, seed
from agent.tests.test_refusal_rules import rule
from agent.tests.test_review_launch import draft_sections

DAY = date(2026, 10, 11)
NOON = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
NEXT = datetime(2026, 10, 12, 3, 37, tzinfo=UTC)  # when the job runs for DAY


def proposal(**kw) -> models.RuleProposal:
    args = {
        "kind": "word",
        "applies_to": "any",
        "value": "deal",
        "topic": None,
        "why": "a word Google reads as a price promise",
        "level": "warn",
        "line_ids": [],
        "reason": "three lines of two businesses carry it",
    }
    args.update(kw)
    return models.RuleProposal(**args)


@pytest.fixture
def model(monkeypatch):
    """The model faked; a test sets `gen.rule_proposals` before the run."""
    gen = FakeGenerator()
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    return gen


def _refuse(
    conn, biz, plan_id, kind, text, *, at: datetime, cls="ad_text", action="dropped", topic="TRADEMARKS_IN_AD_TEXT"
) -> str:
    """One row, dated `at` (the rows' order in the summary is their created_at)."""
    (rid,) = ads_refusal.record(
        conn,
        business_id=biz,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(detail(kind, text, topic=topic), cls, action)],
    )
    conn.execute("UPDATE ads_refusal SET created_at = %s WHERE id = %s", (at, rid))
    return rid


def a_day(conn) -> dict[str, str]:
    """Two businesses, four refusals on DAY (three lines with "deal", one page refusal with no text) and one live
    disapproval ads-sync keeps for biz_1."""
    _key, plan_1 = seed(conn, status="draft", secs=draft_sections())
    conn.execute("INSERT INTO business (id) VALUES ('biz_2')")
    plan_2 = add_plan(conn, draft_sections(), biz="biz_2", status="draft")
    t = [NOON + timedelta(seconds=n) for n in range(4)]
    rows = {
        "r1": _refuse(conn, BIZ, plan_1, "headline", "Invisalign Deal", at=t[0]),
        "r2": _refuse(conn, "biz_2", plan_2, "headline", "Best deal on braces", at=t[1], topic=None),
        "r3": _refuse(conn, "biz_2", plan_2, "keyword", "braces deal", at=t[2], cls="keyword", topic=None),
        "r4": _refuse(
            conn, BIZ, plan_1, "url", None, at=t[3], cls="destination", action="held", topic="DESTINATION_NOT_WORKING"
        ),
    }
    issues = {"ads": [{"resource_name": "customers/1/adGroupAds/2~3", "topics": [{"topic": "TRADEMARKS_IN_AD_TEXT"}]}]}
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, synced_at) "
        "VALUES (%s, 'google_search', %s, 'limited', %s, %s) ON CONFLICT (business_id, channel) "
        "DO UPDATE SET resources = EXCLUDED.resources, synced_at = EXCLUDED.synced_at",
        (BIZ, plan_1, Jsonb({"issues": issues}), NEXT),
    )
    return rows


def _summary(conn) -> dict:
    return conn.execute("SELECT * FROM refusal_summary").fetchone()


def _rules(conn) -> list[dict]:
    return conn.execute(f"SELECT {local_rules.COLS} FROM refusal_rule ORDER BY created_at, id").fetchall()


def test_a_day_of_rows_becomes_one_summary_with_counts_lines_misses_and_a_verified_proposal(conn, model):
    rows = a_day(conn)
    model.rule_proposals = [
        models.RuleProposals(
            proposals=[
                proposal(line_ids=[rows["r1"], rows["r2"], rows["r3"], "ar_notthere"]),
                proposal(
                    kind="pattern", value="repeated_symbols", level="block", line_ids=[rows["r1"]]
                ),  # matches none
                proposal(value="the", line_ids=[rows["r1"], rows["r2"]]),  # a stop word
                proposal(value="deal", applies_to="headline", line_ids=[rows["r3"]]),  # one business, one line: thin
            ]
        )
    ]
    out = refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    s = _summary(conn)
    assert s["day"] == DAY and s["model"] == "proposed" and s["calls"] == 1
    assert s["counts"]["refusals"] == 4 and s["counts"]["businesses"] == 2
    assert s["counts"]["by_class"] == {"ad_text": 2, "keyword": 1, "destination": 1}
    assert s["counts"]["by_action"] == {"dropped": 3, "held": 1}
    assert s["counts"]["by_topic"] == {"TRADEMARKS_IN_AD_TEXT": 1, "none": 2, "DESTINATION_NOT_WORKING": 1}
    assert s["counts"]["live"] == {"ads": 1, "businesses": 1, "by_topic": {"TRADEMARKS_IN_AD_TEXT": 1}}
    assert [line["text"] for line in s["lines"]] == ["Invisalign Deal", "Best deal on braces", "braces deal"]
    assert s["rules"] == {"hits": {}, "misses": 3}
    (made,) = _rules(conn)
    assert s["proposals"] == [made["id"]] and out["proposed"] == [made["id"]]
    assert (made["kind"], made["applies_to"], made["value"], made["level"], made["status"]) == (
        "word",
        "any",
        "deal",
        "warn",
        "active",
    )
    assert made["proposed_by"] == "summary:2026-10-11" and made["why"] == "Google's rules for ad text"
    assert made["evidence"] == {
        "refusal_ids": [rows["r1"], rows["r2"], rows["r3"]],
        "count": 3,
        "businesses": 2,
        "note": "three lines of two businesses carry it",
        "model_why": "a word Google reads as a price promise",
    }
    assert out["model"] == "proposed" and out["misses"] == 3 and out["refusals"] == 4


def test_what_the_model_reads_names_no_business(conn, model):
    rows = a_day(conn)
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    (material,) = model.rule_calls
    assert [line["business"] for line in material["lines"]] == ["b1", "b2", "b2"]
    assert [line["id"] for line in material["lines"]] == [rows["r1"], rows["r2"], rows["r3"]]
    assert "biz_" not in str(material) and "Bright Smile" not in str(material)
    assert [s["id"] for s in material["shapes"]] == list(local_rules.SHAPES)
    assert material["needs"] == {"businesses": 2, "refusals": 3} and material["day"] == "2026-10-11"


def test_a_trademark_is_warn_only_and_a_block_rule_waits_proposed(conn, model):
    rows = a_day(conn)
    conn.execute("UPDATE ads_refusal SET text = 'Invisalign braces deal' WHERE id = %s", (rows["r2"],))
    every = [rows["r1"], rows["r2"], rows["r3"]]
    model.rule_proposals = [
        models.RuleProposals(
            proposals=[
                proposal(
                    value="invisalign", topic="TRADEMARKS_IN_AD_TEXT", level="block", line_ids=every
                ),  # 2 businesses
                proposal(kind="pattern", value="repeated_symbols", level="block", line_ids=every),  # matches none
                proposal(value="deal", level="block", line_ids=every),  # three lines: a block rule, waiting
            ]
        )
    ]
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    made = {r["value"]: r for r in _rules(conn)}
    assert made["invisalign"]["level"] == "warn" and made["invisalign"]["status"] == "active"
    assert made["invisalign"]["topic"] == "TRADEMARKS_IN_AD_TEXT" and made["invisalign"]["evidence"]["businesses"] == 2
    assert made["deal"]["level"] == "block" and made["deal"]["status"] == "proposed"
    assert "repeated_symbols" not in made and len(made) == 2
    assert local_rules.load(conn, config.load()) == []  # the switch is off: nothing acts yet


def test_a_proposal_the_code_cannot_verify_is_dropped_and_the_summary_stands(conn, model):
    """Review Focus 1: a stop word, a shape that is not one of SHAPES, lines the rule does not match -- nothing is
    written, the day's row still is."""
    rows = a_day(conn)
    model.rule_proposals = [
        models.RuleProposals(
            proposals=[
                proposal(value="the", line_ids=list(rows.values())),
                proposal(kind="pattern", value="[!?]{2,}", line_ids=list(rows.values())),
                proposal(value="braces", line_ids=[rows["r1"]]),  # r1 has no "braces": evidence 0
                proposal(value="deal", applies_to="headline", line_ids=[rows["r3"]]),  # a keyword line: 0
            ]
        )
    ]
    out = refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    assert _rules(conn) == [] and out["proposed"] == [] and _summary(conn)["model"] == "proposed"


def test_a_why_with_jargon_becomes_the_class_sentence_and_a_known_key_is_never_proposed_twice(conn, model):
    rows = a_day(conn)
    rule(conn, "deal", status="rejected")  # (word, any, deal) was decided once: never proposed again (D14)
    model.rule_proposals = [
        models.RuleProposals(
            proposals=[
                proposal(line_ids=[rows["r1"], rows["r2"], rows["r3"]]),  # the known key
                proposal(applies_to="headline", why="a keyword Google refuses", line_ids=[rows["r1"], rows["r2"]]),
            ]
        )
    ]
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    made = [r for r in _rules(conn) if r["status"] != "rejected"]
    assert [(r["value"], r["applies_to"], r["topic"], r["why"]) for r in made] == [
        ("deal", "headline", None, "Google's rules for ad text")  # r1 and r2 share no topic: the class sentence
    ]


def test_an_invented_topic_is_never_trusted_unless_the_evidence_carries_it(conn, model):
    """Review AA1 #2: a topic no evidence row carries is dropped to None (or the one topic every row shares), never
    promoted onto the rule from the model's say-so alone -- an invented "TRADEMARK..." can't force a rule warn by
    virtue of its topic alone when the evidence does not back it."""
    rows = a_day(conn)
    model.rule_proposals = [
        models.RuleProposals(
            proposals=[proposal(topic="SOME_TOPIC_NO_ROW_HAS", line_ids=[rows["r1"], rows["r2"], rows["r3"]])]
        )
    ]
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    (made,) = _rules(conn)
    assert made["topic"] is None  # r1 carries TRADEMARKS_IN_AD_TEXT, r2 and r3 carry none: not shared, not proposed


def test_a_proposed_topic_the_evidence_does_carry_is_kept(conn, model):
    """Review AA1 #2, the accepted side: a topic one evidence row does carry is kept as-is."""
    rows = a_day(conn)
    model.rule_proposals = [
        models.RuleProposals(
            proposals=[
                proposal(topic="trademarks_in_ad_text", line_ids=[rows["r1"], rows["r2"], rows["r3"]]),
            ]
        )
    ]
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    (made,) = _rules(conn)
    assert made["topic"] == "TRADEMARKS_IN_AD_TEXT"  # r1 carries it; the model's casing is normalized, not rejected


def test_one_proposal_raising_inside_accept_still_writes_the_days_row(conn, model, monkeypatch):
    """Review AA1 #3: a proposal that blows up inside _accept is dropped, never loses the day's summary row or its
    counted call."""
    rows = a_day(conn)
    model.rule_proposals = [models.RuleProposals(proposals=[proposal(line_ids=list(rows.values()))])]

    def boom(cls, topic):
        raise RuntimeError("boom")

    monkeypatch.setattr(refusal_summary.ads_refusal, "why", boom)
    out = refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    s = _summary(conn)
    assert s["day"] == DAY and s["calls"] == 1 and s["model"] == "proposed"
    assert out["proposed"] == [] and _rules(conn) == []


def test_a_hit_is_a_refused_line_an_active_rule_already_matched(conn, model):
    rows = a_day(conn)
    deal = rule(conn, "deal", level="warn")
    conn.execute("UPDATE ads_refusal SET rule_id = %s WHERE id = ANY(%s)", (deal, [rows["r1"], rows["r2"]]))
    out = refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    assert _summary(conn)["rules"] == {"hits": {deal: 2}, "misses": 1} and out["hits"] == 2
    assert [line["rule_id"] for line in _summary(conn)["lines"]] == [deal, deal, None]


def test_no_missed_line_means_no_model_call(conn, model):
    a_day(conn)
    deal = rule(conn, "deal", level="warn")
    conn.execute("UPDATE ads_refusal SET rule_id = %s WHERE text IS NOT NULL", (deal,))
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    assert _summary(conn)["model"] == "nothing_to_propose" and model.rule_calls == []
    refusal_summary.run(conn, config.load(), day=date(2026, 10, 10), now=NEXT)  # a day with no rows at all
    empty = conn.execute("SELECT * FROM refusal_summary WHERE day = %s", (date(2026, 10, 10),)).fetchone()
    assert empty["model"] == "nothing_to_propose" and empty["counts"]["refusals"] == 0 and empty["lines"] == []


def test_without_a_model_the_counts_are_written_alone(conn, monkeypatch):
    a_day(conn)

    def down(settings=None, **_):
        raise llm.LLMError("no key")

    monkeypatch.setattr(llm, "generator", down)
    out = refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    s = _summary(conn)
    assert s["model"] == "no_model" and s["calls"] == 0 and s["counts"]["refusals"] == 4 and out["proposed"] == []


def test_a_failed_call_counts_and_writes_the_counts(conn, monkeypatch):
    a_day(conn)
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: FakeGenerator(error="busy"))
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    s = _summary(conn)
    assert s["model"] == "failed" and s["calls"] == 1 and _rules(conn) == []


def test_the_call_is_under_the_analysts_budget_and_counts_toward_it(conn, model, monkeypatch):
    a_day(conn)
    monkeypatch.setenv("AGENT_ANALYST_MAX_PER_DAY", "0")
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    assert _summary(conn)["model"] == "budget" and model.rule_calls == []
    monkeypatch.setenv("AGENT_ANALYST_MAX_PER_DAY", "1")
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    assert _summary(conn)["model"] == "proposed" and analyst.used_today(conn, datetime.now(UTC)) == 1
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)  # the day's one call is spent
    assert _summary(conn)["model"] == "budget"


def test_a_rerun_for_the_same_day_rewrites_the_row_and_proposes_nothing_twice(conn, model):
    """Review Focus 4."""
    rows = a_day(conn)
    model.rule_proposals = [models.RuleProposals(proposals=[proposal(line_ids=list(rows.values()))])]
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    assert conn.execute("SELECT count(*) AS n FROM refusal_summary").fetchone()["n"] == 1
    (made,) = _rules(conn)
    s = _summary(conn)
    assert s["proposals"] == [made["id"]] and s["calls"] == 2 and s["model"] == "proposed"


def test_the_job_is_registered_runs_on_shard_0_alone_and_reads_yesterday(conn, model, monkeypatch):
    assert jobs.JOBS["refusal-summary"] is refusal_summary.job
    assert refusal_summary.job(conn, 1, 2) == {"skipped": "shard"}
    a_day(conn)
    monkeypatch.setattr(refusal_summary, "_now", lambda: NEXT)
    out = refusal_summary.job(conn, 0, 1)
    assert out["day"] == "2026-10-11" and _summary(conn)["day"] == DAY


def test_the_analysts_effort_is_passed_to_the_generator(conn, monkeypatch):
    """Review AA1 #4: the daily summary's one call rides the Analyst's configured effort, the way roles/analyst.py
    does its own call."""
    a_day(conn)
    calls = []

    def fake_generator(settings=None, **kw):
        calls.append(kw)
        return FakeGenerator()

    monkeypatch.setattr(llm, "generator", fake_generator)
    settings = config.load()
    refusal_summary.run(conn, settings, day=DAY, now=NEXT)
    assert calls and calls[0].get("effort") == settings.analyst_llm_effort


def test_latest_lists_the_last_days_newest_first(conn, model):
    a_day(conn)
    refusal_summary.run(conn, config.load(), day=date(2026, 10, 10), now=NEXT)
    refusal_summary.run(conn, config.load(), day=DAY, now=NEXT)
    shown = refusal_summary.latest(conn)
    assert [s["day"] for s in shown] == ["2026-10-11", "2026-10-10"]
    assert shown[0]["counts"]["refusals"] == 4 and isinstance(shown[0]["created_at"], str)
