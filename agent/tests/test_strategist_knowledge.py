"""A plan draft reads Admiry's knowledge and the plan keeps the ids it used (capability layer K3; spec 2026-10-07 §4's
Strategist row, §6.1; plan P2-P5): `recommend`, a rewrite and a change without a draft, through the public tools on
the dental walk (surface_helpers.planned), with the small knowledge tree of the `knowledge_on` fixture."""

import copy
import json
import logging

import pytest
from fastmcp.exceptions import ToolError

from agent import business, plan_review, plans, tools_consult
from agent import knowledge_index as ki
from agent.tests import knowledge_fixtures as kf
from agent.tests.conftest import call_tool, tool_text
from agent.tests.eval_fixtures import KNOWLEDGE, frozen_plan
from agent.tests.fixtures import FakeGenerator, knowledge_fixture, plan_draft_fixture
from agent.tests.surface_helpers import planned

CITED = ["dentists/compliance", "dentists/made-up", "dentists/compliance", "attorneys/compliance"]
GIVEN = ki.Retrieved("== dentists/compliance ==\n- Never promise a result.", ["dentists/compliance"])


def _draft(ids: list[str]):
    return plan_draft_fixture().model_copy(update={"knowledge_ids": ids})


def _latest(conn) -> dict:
    return plans.latest(conn, "biz_1")


def _guarded(gen, given):
    """`draft_with_copy` as recommend asks it, for the dental fixture business."""
    return tools_consult.draft_with_copy(
        gen,
        knowledge_fixture().model_dump(),
        {"monthly_limit_usd": 2000},
        biz="biz_1",
        consultation={"channel": "google_search", "monthly_budget_usd": 2000},
        given=given,
    )


def test_the_guard_hands_the_model_the_notes_and_keeps_only_the_ids_it_was_given(caplog):
    gen = FakeGenerator(draft=_draft(["dentists/compliance", "dentists/made-up", "attorneys/compliance"]))
    with caplog.at_level(logging.INFO, logger=tools_consult.log.name):
        got = _guarded(gen, GIVEN)
    assert gen.plan_calls[0]["trade_knowledge"] == GIVEN.text
    assert got.knowledge_ids == ["dentists/compliance"]
    assert gen.draft.knowledge_ids == ["dentists/compliance", "dentists/made-up", "attorneys/compliance"]  # untouched
    assert [r.getMessage() for r in caplog.records if "knowledge id" in r.getMessage()] == [
        "plan draft: 2 knowledge id(s) it was not given, or named twice, were dropped (business biz_1)"
    ]


def test_the_guard_hands_back_the_models_own_draft_when_all_its_ids_stand_or_it_names_none():
    for ids in (["dentists/compliance"], []):
        gen = FakeGenerator(draft=_draft(ids))
        assert _guarded(gen, GIVEN) is gen.draft


def test_the_guard_reads_nothing_and_keeps_no_id_without_notes_to_give():
    gen = FakeGenerator(draft=_draft(["dentists/compliance"]))
    assert _guarded(gen, None).knowledge_ids == [] and gen.plan_calls[0]["trade_knowledge"] is None


def test_the_second_ask_for_missing_ad_text_reads_the_same_notes():
    short = _draft(["dentists/compliance"])
    short.ads[0].headlines = ["Only one"]
    gen = FakeGenerator(draft=short)
    with pytest.raises(ToolError) as refused:
        _guarded(gen, GIVEN)
    assert str(refused.value) == tools_consult.NO_AD_TEXT
    assert [c["trade_knowledge"] for c in gen.plan_calls] == [GIVEN.text, GIVEN.text]


def test_the_draft_reads_the_trees_knowledge_and_the_plan_keeps_the_ids_it_was_given(
    client, conn, walk_fake, knowledge_on
):
    walk_fake.draft = _draft(CITED)
    planned(client, conn)
    told = walk_fake.plan_calls[-1]["trade_knowledge"]
    assert told.startswith("== dentists/compliance ==\n- Never promise a result.")
    assert "== dentists/google-search ==" in told and "== dentists/offers ==" in told
    assert "attorneys/" not in told and "[src-one]" not in told
    assert _latest(conn)["sections"]["rationale"]["knowledge_ids"] == ["dentists/compliance"]


def test_with_the_switch_off_the_draft_reads_nothing_and_the_plan_cites_nothing(
    client, conn, walk_fake, tmp_path, monkeypatch
):
    kf.use(monkeypatch, kf.tree(tmp_path))  # the files are there; AGENT_KNOWLEDGE is not set
    walk_fake.draft = _draft(["dentists/compliance"])
    planned(client, conn)
    assert walk_fake.plan_calls[-1]["trade_knowledge"] is None
    assert _latest(conn)["sections"]["rationale"]["knowledge_ids"] == []


def test_a_change_without_a_new_draft_keeps_the_versions_ids(client, conn, walk_fake, knowledge_on):
    walk_fake.draft = _draft(["dentists/google-search"])
    walk = planned(client, conn)
    drafts = len(walk_fake.plan_calls)
    res = call_tool(
        client, "update_marketing_plan", {"changes": {"radius_miles": 8}, "idempotency_key": "k3-r"}, key=walk["key"]
    )
    assert not res["isError"], tool_text(res)
    assert len(walk_fake.plan_calls) == drafts  # a change of the radius drafts nothing
    latest = _latest(conn)
    assert latest["id"] != walk["plan_id"]  # a new version
    assert latest["sections"]["rationale"]["knowledge_ids"] == ["dentists/google-search"]


def test_a_rewrite_reads_the_knowledge_again_and_keeps_its_own_ids(client, conn, walk_fake, knowledge_on):
    walk_fake.draft = _draft(["dentists/google-search"])
    walk = planned(client, conn)
    drafts = len(walk_fake.plan_calls)
    walk_fake.draft = _draft(["dentists/offers"])
    res = call_tool(
        client,
        "update_marketing_plan",
        {"message": "lead with the free consultation", "idempotency_key": "k3-w"},
        key=walk["key"],
    )
    assert not res["isError"], tool_text(res)
    assert len(walk_fake.plan_calls) > drafts
    assert "== dentists/offers ==" in walk_fake.plan_calls[-1]["trade_knowledge"]
    assert _latest(conn)["sections"]["rationale"]["knowledge_ids"] == ["dentists/offers"]


def test_a_version_from_before_k3_changes_without_ids(client, conn, walk_fake, knowledge_on):
    walk = planned(client, conn)
    conn.execute("UPDATE plan SET json = json #- '{sections,rationale,knowledge_ids}'")
    res = call_tool(
        client, "update_marketing_plan", {"changes": {"radius_miles": 8}, "idempotency_key": "k3-old"}, key=walk["key"]
    )
    assert not res["isError"], tool_text(res)
    assert _latest(conn)["sections"]["rationale"]["knowledge_ids"] == []


def test_the_judge_reads_the_plan_as_it_did_before_k3(client, conn, walk_fake, knowledge_on):
    walk_fake.draft = _draft(["dentists/compliance"])
    planned(client, conn)
    plan = _latest(conn)
    knowledge = business.latest_knowledge(conn, "biz_1")[1]
    # `material` fetches and hands over to `material_of`, which every judge path reads (the S0 look, the background
    # Critic, the eval, the grading page): neither carries the ids, in either copy of the rationale
    for material in (
        plan_review.material(conn, "biz_1", plan, money=None, blocked=None),
        plan_review.material_of(plan, knowledge, final_host=None, money=None, blocked=None),
    ):
        assert "knowledge_ids" not in material["rationale"] and "knowledge_ids" not in material["cards"]["rationale"]
        assert material["rationale"]["why_primary"] == plan["sections"]["rationale"]["why_primary"]
    assert plan["sections"]["rationale"]["knowledge_ids"] == ["dentists/compliance"]  # the stored plan is untouched


def test_the_ids_change_nothing_the_judge_reads():
    plain = frozen_plan()  # K5's frozen plan: its rationale names no knowledge
    cited = copy.deepcopy(plain)
    cited["sections"]["rationale"]["knowledge_ids"] = ["dentists/compliance", "dentists/offers"]
    before = copy.deepcopy(cited)
    a = plan_review.material_of(plain, KNOWLEDGE, final_host="example.com", money=None, blocked=None)
    b = plan_review.material_of(cited, KNOWLEDGE, final_host="example.com", money=None, blocked=None)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert cited == before  # nothing popped from the stored rationale, in the plan or in the cards built from it
    # and for a plan with no ids at all, the material is the cards and the rationale the plan holds, as before K3
    named = {**plain, "business_name": plans.name_for(KNOWLEDGE) or plans.address_for(KNOWLEDGE)}
    assert a["cards"] == plans.cards(named, final_host="example.com")
    assert a["rationale"] == plain["sections"]["rationale"]


def test_a_plan_with_no_rationale_reads_to_the_judge_as_before_k3():
    plan = frozen_plan()
    del plan["sections"]["rationale"]
    m = plan_review.material_of(plan, KNOWLEDGE, final_host=None, money=None, blocked=None)
    assert m["rationale"] is None and m["cards"]["rationale"] == {}  # the cards' own default, as plans.cards gives it


def test_a_pending_file_never_reaches_a_draft(client, conn, walk_fake, knowledge_on):
    kf.write(knowledge_on, "dentists", "offers", channels=(), reviewed_by="pending")
    ki.forget()
    walk_fake.draft = _draft(["dentists/offers"])
    planned(client, conn)
    assert "dentists/offers" not in walk_fake.plan_calls[-1]["trade_knowledge"]
    assert _latest(conn)["sections"]["rationale"]["knowledge_ids"] == []
