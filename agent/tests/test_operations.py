"""The three kinds of operation (spec 2026-10-08 §2.1, §2.2; B0, shared with K6): every registered command is either
dangerous -- it moves money, starts or raises spend, shows something to the public, reaches a third party, or cannot be
undone -- or safe; and the never column names what no answer or context carries across a business."""

import ast
from pathlib import Path

import pytest

from agent import copy, operations


def test_every_registered_command_and_every_one_behind_a_public_tool_has_exactly_one_kind():
    from agent import mcp_app
    from agent.surface import registry

    assert not operations.DANGEROUS & operations.SAFE
    registered = {meta.name for meta in mcp_app.REGISTRY}
    public = {command for meta in registry.SURFACE for command in meta.commands}
    assert public <= registered
    classified = operations.DANGEROUS | operations.SAFE
    assert classified == registered, sorted(classified ^ registered)


@pytest.mark.parametrize(
    "command",
    [
        "approve_plan",
        "confirm_proposal",
        "resume",
        "connect_channel",
        "connect_google_ads",
        "set_landing_page",
        "publish_page",
        "unpublish_page",
        "send_install_steps",
        "request_payment_link",
        "revoke_key",
    ],
)
def test_what_moves_money_publishes_reaches_a_third_party_or_cannot_be_undone_is_dangerous(command):
    assert operations.kind(command) == "dangerous"


@pytest.mark.parametrize(
    "command",
    [
        "pause",
        "adjust",
        "change_plan",
        "revise_plan",
        "update_business",
        "build_site",
        "create_page",
        "update_page",
        "check_landing_page",
        "marketing_state",
        "contact_support",
    ],
)
def test_reads_draft_changes_previews_facts_and_stopping_spend_are_safe(command):
    assert operations.kind(command) == "safe"


def test_an_unknown_command_has_no_kind():
    with pytest.raises(KeyError):
        operations.kind("no_such_command")


def test_the_never_column_has_its_five_rows_and_its_sentence():
    assert [row["id"] for row in operations.NEVER] == [
        "other_business",
        "admiry_business",
        "team_working",
        "secrets",
        "acting_for_another",
    ]
    assert all(row["what"] and row["why"] for row in operations.NEVER)
    assert operations.NEVER_SAY == "I can only talk about your own business."
    assert copy.jargon(operations.NEVER_SAY) is None


def test_operations_is_a_leaf_any_module_may_read():
    tree = ast.parse((Path(__file__).parents[1] / "operations.py").read_text())
    imported = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.level or "agent" in (n.module or ""))]
    assert imported == []


def test_the_harness_own_dispatches_are_safe_and_apart_from_the_registry():
    """K6 slice 1 (spec 2026-10-10 §7): the intents that run no registered command, each non-dangerous. "ask" is the
    one exception: the Turn's own intent (answered from the material, dispatch.py Task 8) happens to share its name
    with the registered `ask` command (tools_state.ask, a research question) that predates this slice and that
    HARNESS_SAFE does not mean to name -- kind() agrees on "safe" either way (asserted below)."""
    assert operations.HARNESS_SAFE == {
        "start_over",
        "go_to",
        "budget",
        "explain",
        "ask",
        "skip",
        "smalltalk",
        "unclear",
    }
    assert operations.HARNESS_SAFE & (operations.SAFE | operations.DANGEROUS) == {"ask"}
    assert all(operations.kind(d) == "safe" for d in operations.HARNESS_SAFE)
