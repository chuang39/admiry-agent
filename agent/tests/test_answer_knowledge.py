"""The answerer reads the knowledge the plan was built with (capability layer K3, plan P8): `ask` hands the model the
files the plan cites -- not a new search -- and nothing while the switch is off or the plan cites none."""

from agent import models
from agent.tests.conftest import call_tool, tool_text
from agent.tests.fixtures import plan_draft_fixture
from agent.tests.surface_helpers import planned

QUESTION = "why no free exam in the ads?"


def _ask(client, key, walk_fake) -> dict:
    walk_fake.answers_to[QUESTION] = models.MailAnswer(text="Offers stay on your page.", grounded=True)
    res = call_tool(client, "ask_admiry", {"question": QUESTION}, key=key)
    assert not res["isError"], tool_text(res)
    return walk_fake.answer_calls[-1]["material"]


def test_ask_hands_the_answerer_the_files_the_plan_cites(client, conn, walk_fake, knowledge_on):
    walk_fake.draft = plan_draft_fixture().model_copy(update={"knowledge_ids": ["dentists/compliance"]})
    walk = planned(client, conn)
    told = _ask(client, walk["key"], walk_fake)["trade_knowledge"]
    assert told.startswith("== dentists/compliance ==") and "dentists/google-search" not in told


def test_the_answerer_reads_nothing_for_a_plan_that_cites_nothing(client, conn, walk_fake, knowledge_on):
    walk = planned(client, conn)  # the draft named no id
    assert "trade_knowledge" not in _ask(client, walk["key"], walk_fake)
    # a version from before K3 has no `knowledge_ids` at all: asked about it, the answerer reads nothing either
    conn.execute("UPDATE plan SET json = json #- '{sections,rationale,knowledge_ids}'")
    material = _ask(client, walk["key"], walk_fake)
    assert "knowledge_ids" not in material["rationale"] and "trade_knowledge" not in material


def test_the_answerer_reads_nothing_while_the_switch_is_off(client, conn, walk_fake, knowledge_on, monkeypatch):
    walk = planned(client, conn)
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,rationale,knowledge_ids}', '[\"dentists/compliance\"]')"
    )
    monkeypatch.delenv("AGENT_KNOWLEDGE")
    assert "trade_knowledge" not in _ask(client, walk["key"], walk_fake)
