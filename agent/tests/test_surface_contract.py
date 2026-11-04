"""The public surface as assistants see it (spec 2026-10-01 §4, §9.7, §12; plan G1 Task 13): exactly the public
tools, a retired name answered with its replacement, the contract pinned by a golden file, scopes enforced, and no
action or description that names a tool an assistant cannot call."""

import ast
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent import (
    assistant_skill,
    business,
    connect,
    consult,
    funnel,
    google_ads_link,
    keys,
    landing,
    launch_check,
    leads,
    mcp_app,
    page_builder,
    plan_review,
    plans,
    profile,
    tools_business,
    tools_channels,
    tools_consult,
    tools_proposals,
    tools_research,
    tools_state,
    tools_support,
)
from agent.channels import registry as channel_registry
from agent.roles import analyst, research_material
from agent.sites import families, intake, photos
from agent.surface import actions, contract, gate, registry, supporting, told
from agent.surface import plan as surface_plan
from agent.surface import words as surface_words
from agent.tests.conftest import call_tool, data, rpc, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import WALK_TEXT, planned, settle_page
from agent.tests.test_ads_run import CHECK_OK, CID, PAGE_URL

REPO = Path(__file__).resolve().parents[2]
PUBLIC = [
    "get_marketing_state",
    "get_business_profile",
    "get_connected_accounts",
    "get_campaign_performance",
    "get_insights",  # capability layer K2: the Analyst's daily look
    "get_research",  # capability layer K4: the Researcher's latest look into the market
    "request_research",
    "get_leads",
    "update_lead",
    "add_lead",
    "create_marketing_plan",
    "update_marketing_plan",
    "get_marketing_plan",
    "launch_marketing_plan",
    "ask_admiry",
    "get_campaigns",
    "pause_campaign",
    "resume_campaign",
    "update_marketing_budget",
    "update_business_profile",
    "connect_account",
    "get_landing_page",
    "update_landing_page",
    "get_site_templates",
    "get_billing",
    "get_activity_log",
    "contact_support",
    "suggest_ad_creatives",  # hackathon 2026-11-04: ads made from the owner's own photos
]
ON_MAIN = (  # the 47 tools assistants knew before this surface (spec Appendix A)
    "whoami",
    "list_keys",
    "revoke_key",
    "get_audit_log",
    "connect_google_ads",
    "get_report",
    "pause",
    "resume",
    "describe_business",
    "get_business",
    "update_business",
    "set_goal",
    "request_connect_link",
    "connect_channel",
    "get_connection_status",
    "get_channel_status",
    "get_status_issues",
    "get_platform_statement",
    "answer",
    "recommend",
    "adjust",
    "preview_page",
    "publish_page",
    "unpublish_page",
    "create_page",
    "update_page",
    "revise_page",
    "get_page_kit",
    "list_leads",
    "update_lead",
    "set_landing_page",
    "check_landing_page",
    "get_page",
    "get_page_stats",
    "create_plan",
    "get_plan",
    "revise_plan",
    "approve_plan",
    "list_proposals",
    "get_proposal",
    "confirm_proposal",
    "decline_proposal",
    "start",
    "contact_support",
    "get_balance",
    "request_payment_link",
    "list_payments",
)


def test_the_gate_is_closed_outside_the_test_process():
    done = subprocess.run(
        [sys.executable, "-c", "from agent.surface import gate; print(gate.EXPOSE_COMMANDS)"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.strip() == "False", done.stderr
    assert gate.EXPOSE_COMMANDS is True  # conftest.py opens it for the suite, and only there


def test_assistants_see_exactly_the_public_tools(client, conn, strict_surface):
    key = business_with_key(conn, claim_state="claimed")
    tools = rpc(client, "tools/list", key=key)["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert sorted(by_name) == sorted(PUBLIC) and len(tools) == 28
    assert sorted(m.name for m in registry.SURFACE) == sorted(PUBLIC) and gate.PUBLIC == set(PUBLIC)
    cards = {n: t["_meta"]["ui"]["resourceUri"] for n, t in by_name.items() if (t.get("_meta") or {}).get("ui")}
    assert cards == {
        "update_marketing_plan": "ui://admiry/plan",
        "get_marketing_plan": "ui://admiry/plan",
        "update_marketing_budget": "ui://admiry/plan",
        "get_campaigns": "ui://admiry/channels",
    }
    assert by_name["get_marketing_state"]["annotations"]["readOnlyHint"] is True
    assert by_name["launch_marketing_plan"]["annotations"]["readOnlyHint"] is False
    assert by_name["get_landing_page"]["annotations"]["readOnlyHint"] is False  # its kit view e-mails the owner
    # an annotation states the truth: a repeat of these two does something again (starts over; writes a new preview)
    not_idempotent = {n for n, t in by_name.items() if t["annotations"]["idempotentHint"] is False}
    assert not_idempotent == {"create_marketing_plan", "update_landing_page"}
    reads_the_web = {n for n, t in by_name.items() if t["annotations"]["openWorldHint"] is True}
    assert reads_the_web == {
        "create_marketing_plan",
        "update_business_profile",
        "get_landing_page",
        "update_landing_page",
    }
    # final review A m8: one tool can remove or replace what is live at once -- take the page down (its ads pause) or
    # move running ads to another address; a new start and a new reading keep every version
    destructive = {n for n, t in by_name.items() if t["annotations"]["destructiveHint"] is True}
    assert destructive == {"update_landing_page"}


def test_every_tool_assistants_knew_is_public_replaced_or_gone():
    assert len(ON_MAIN) == 47 and set(ON_MAIN) <= {m.name for m in mcp_app.REGISTRY}  # each is still a command
    assert set(gate.RETIRED) | set(gate.GONE) | {"update_lead", "contact_support"} == set(ON_MAIN)
    assert not set(gate.RETIRED) & set(gate.GONE)
    assert set(gate.RETIRED.values()) <= set(PUBLIC)


def test_a_retired_name_is_answered_with_its_replacement_and_runs_nothing(client, conn, strict_surface):
    key = business_with_key(conn, claim_state="claimed")
    for old, new in gate.RETIRED.items():
        res = call_tool(client, old, {}, key=key)
        assert res["isError"] and f"`{old}` was replaced by `{new}`" in tool_text(res), old
        assert f"(version {gate.SURFACE_VERSION})" in tool_text(res) and "/connect/docs" in tool_text(res)
    # the one name that did two things says where the other went: a page's yes is not a launch
    assert set(gate.ALSO) == {"confirm_proposal"} and "`update_landing_page`" in gate.ALSO["confirm_proposal"]
    assert gate.ALSO["confirm_proposal"] in tool_text(call_tool(client, "confirm_proposal", {}, key=key))
    for old, why in gate.GONE.items():
        res = call_tool(client, old, {}, key=key)
        assert res["isError"] and f"`{old}` is no longer a tool: {why}" in tool_text(res), old
    for hidden in ("marketing_state", "business_profile", "performance", "ask", "change_plan", "change_goal"):
        res = call_tool(client, hidden, {}, key=key)
        assert res["isError"] and f"Unknown tool: {hidden}" in tool_text(res), hidden
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_a_call_to_a_retired_name_leaves_a_line_in_the_log(client, conn, strict_surface, caplog):
    """It writes no audit row (nothing ran), so the log is where "an assistant keeps calling an old name" shows."""
    key = business_with_key(conn, claim_state="claimed")
    with caplog.at_level("INFO", logger="agent.surface.gate"):
        call_tool(client, "whoami", {}, key=key)
        call_tool(client, "list_keys", {}, key=key)
    said = [r.getMessage() for r in caplog.records if r.name == "agent.surface.gate"]
    assert any("whoami" in line for line in said) and any("list_keys" in line for line in said)


def _dispatches(tree: ast.AST) -> list[ast.Call]:
    """Every call of `dispatch` under `tree`, by its own name or as an attribute (`registry.dispatch(…)`)."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == "dispatch" or getattr(node.func, "attr", None) == "dispatch")
    ]


def _names_its_command(call: ast.Call) -> bool:
    """Whether a dispatch names its command as a string written out, the one thing the scan can read."""
    return bool(call.args) and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)


def test_a_public_tool_runs_exactly_the_commands_it_declares():
    """`commands` is what the contract says a tool may run. Every `dispatch("<name>", …)` in the tool's own function
    names one of them, and every one it declares is dispatched there: the golden file cannot say less, or more, than
    the code does. What the scan cannot read fails it: a dispatch whose command is not written out in the tool, and a
    dispatch anywhere else in the modules the public tools are written in."""
    for meta in registry.SURFACE:
        if meta.commands == (meta.name,):  # published as it is: the tool is the command
            continue
        calls = _dispatches(ast.parse(textwrap.dedent(inspect.getsource(meta.tool.fn))))
        unread = [ast.unparse(call) for call in calls if not _names_its_command(call)]
        assert not unread, (meta.name, "dispatches a command the scan cannot read", unread)
        called = {call.args[0].value for call in calls}
        assert called == set(meta.commands), (meta.name, sorted(called), sorted(meta.commands))
    public = {meta.name for meta in registry.SURFACE}
    written = set()
    for module in ("context.py", "creatives.py", "outcomes.py", "plan.py", "run.py", "supporting.py"):
        tree = ast.parse((REPO / "agent" / "surface" / module).read_text())
        tools = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in public]
        written |= {node.name for node in tools}
        # every dispatch of the module is in a public tool's own body: one anywhere else (a helper, a method, a lambda,
        # a statement, a def under an `if`) runs a command the loop above never reads
        everywhere = _dispatches(tree)
        inside = {id(call) for node in tools for call in _dispatches(node)}
        outside = [f"line {call.lineno}: {ast.unparse(call)}" for call in everywhere if id(call) not in inside]
        assert len(everywhere) == len(inside), (module, "dispatches outside a public tool", outside)
    # the modules read above are where every public tool that is not a command published as it is is written
    assert written == {meta.name for meta in registry.SURFACE if meta.commands != (meta.name,)}


def test_a_resource_runs_only_read_commands_written_out_where_it_reads_them():
    """told.py runs a command through `_data(command, …)`, which dispatches the name it is given: every name that
    reaches it is written out where `_data` is called (directly, or as the argument after it in `run_in_threadpool`),
    and each is a read-only command. A `_data` reached any other way fails, and told.py dispatches nowhere else."""
    tree = ast.parse((REPO / "agent" / "surface" / "told.py").read_text())
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    names, unread = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id == "_data"):
            continue
        call, given = parent[node], None
        direct = isinstance(call, ast.Call) and call.func is node
        pooled = isinstance(call, ast.Call) and getattr(call.func, "id", None) == "run_in_threadpool"
        if direct and call.args:  # _data("<name>", …)
            given = call.args[0]
        elif pooled and call.args[0] is node and len(call.args) > 1:  # run_in_threadpool(_data, "<name>", …)
            given = call.args[1]
        if isinstance(given, ast.Constant) and isinstance(given.value, str):
            names.append(given.value)
        else:
            unread.append(f"line {node.lineno}: {ast.unparse(call)}")
    assert not unread, ("a resource reaches _data with a command the scan cannot read", unread)
    assert {"marketing_state", "business_profile", "get_plan", "performance"} <= set(names)  # what it reads today
    classes = {meta.name: meta.cls for meta in mcp_app.REGISTRY}
    assert {name: classes.get(name) for name in names} == dict.fromkeys(names, "RO")
    dispatching = [
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _dispatches(n)
    ]
    assert dispatching == ["_data"] and len(_dispatches(tree)) == 1, dispatching


def test_no_public_description_names_a_tool_that_left():
    """A description is what an assistant reads before it calls: three commands are public as they are, so their own
    docstrings are checked with the rest."""
    left = [old for old in (*gate.RETIRED, *gate.GONE) if "_" in old or old == "whoami"]
    named = re.compile(r"(?<![A-Za-z_])(" + "|".join(left) + r")(?![A-Za-z_])")
    for meta in registry.SURFACE:
        found = named.findall(meta.tool.description or "")
        assert not found, (meta.name, found)


# How a new contract is published: the words of the golden test's docstring and of its failure
PUBLISH = (
    "bump gate.SURFACE_VERSION (a second version on one date is 2026-10-01.2), update the two places SKILL.md carries "
    'the version (its frontmatter and its line "This guide is version ..."), write the new golden file by running '
    "this test once with SURFACE_GOLDEN_WRITE=1 (it writes the file and fails with its digest), and add that digest "
    "to contract.PUBLISHED"
)


def test_the_contract_matches_its_golden_file():
    """The golden file holds the surface and the words an assistant reads: the tools, the fixed choices, the prompts
    and the resources, and a digest of the instructions, of every description, of every file of the skill, of each
    prompt's text and of the connector docs' own words (the error list and the approval texts). A
    changed word fails this test as a changed tool does: a saved skill learns that a rule changed only through the
    version. Before a version is first rolled anywhere its golden file is written again; after that, publish a new
    version: bump gate.SURFACE_VERSION (a second version on one date is 2026-10-01.2), update the two places SKILL.md
    carries the version (its frontmatter and its line "This guide is version ..."), write the new golden file by
    running this test once with SURFACE_GOLDEN_WRITE=1 (it writes the file and fails with its digest), and add that
    digest to contract.PUBLISHED."""
    text = contract.dumps(contract.build())
    path = contract.golden_path(gate.SURFACE_VERSION)
    if os.environ.get("SURFACE_GOLDEN_WRITE") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        pytest.fail(f"wrote {path.name}; its digest for contract.PUBLISHED is {contract.digest(text)}")
    golden = path.read_text(encoding="utf-8")
    assert contract.PUBLISHED[gate.SURFACE_VERSION] == contract.digest(golden), "a published contract was edited"
    assert text == golden, f"the surface or the words an assistant reads changed: to publish a new version, {PUBLISH}"
    for version, published in contract.PUBLISHED.items():  # an older contract, once published, is never edited either
        older = contract.golden_path(version).read_text(encoding="utf-8")
        assert contract.digest(older) == published, f"the published contract {version} was edited"


def test_a_tools_entry_names_no_command():
    """Final review C, Important 5c: the commands behind a public tool are internal -- the dispatch scan above holds
    them to the code -- and pinning them would make an internal change a public version."""
    built = contract.build()
    assert [entry["name"] for entry in built["tools"]] == sorted(PUBLIC)
    assert not [entry["name"] for entry in built["tools"] if "commands" in entry]


def test_every_fixed_choice_is_the_constant_the_code_checks_it_with(conn):
    """Final review C, Important 5a/b: the choices an assistant passes or reads are pinned, each the one constant the
    code enforces it with (never typed a second time). The kinds of an issue are not: platforms add their own. The
    re-review's I-A: the sections a profile read names, a location's way of working its area, a support request's
    reason, and what an answer reads back -- a lead's source, what a waiting change needs, whose limit the goal's is, a
    plan's status, an account's status."""
    values = contract.build()["values"]
    assert values == {
        "periods": list(funnel.PERIODS),
        "lead_statuses": list(leads.STATUSES),
        "lead_kinds": list(leads.KINDS),
        "lead_reasons": list(leads.REASONS),
        "contact_keys": list(leads.CONTACT_KEYS),
        "goal_types": list(business.GOAL_TYPES),
        "plan_levels": list(tools_consult.LEVEL_NAMES),
        "plan_objectives": list(plans.OBJECTIVE_WORDS),
        "plan_changes": list(surface_plan.PLAN_CHANGES),
        "goal_changes": list(tools_business.GOAL_CHANGES),
        "autopilot": list(tools_business.AUTOPILOT_MODES),
        "preferred_outcomes": list(surface_plan.OUTCOMES),
        "known_facts": list(consult.KNOWN_FACTS),
        "patch_keys": list(supporting.PUBLIC_PATCH),
        "location_keys": list(profile.LOCATION_KEYS),
        "page_views": list(supporting.PAGE_VIEWS),
        "page_actions": list(supporting.PAGE_ACTIONS),
        "billing_views": list(supporting.BILLING_VIEWS),
        "page_sections": list(page_builder.SECTIONS),
        "page_templates": list(page_builder.TREES),
        "connect_modes": list(tools_channels.MODES),
        "channels": [c for c in channel_registry.ORDER if channel_registry.known(c)],
        "launch_statuses": list(tools_proposals.LAUNCH_STATUSES),
        "connect_statuses": list(tools_channels.CONNECT_STATUSES),
        "scopes": list(registry.SCOPES),
        "profile_sections": list(profile.SECTIONS),
        "geo_models": list(profile.GEO_MODELS),
        "support_reasons": list(tools_support.REASONS),
        "lead_sources": list(leads.SOURCES),
        "pending_needs": list(tools_state.NEEDS),
        "limit_sources": list(business.LIMIT_SOURCES),
        "plan_statuses": list(plans.STATUSES),
        "account_statuses": list(channel_registry.ACCOUNT_STATUSES),
        # A3p-6 batch 2 (plan Q15): update_landing_page's build_site, what its facts may carry, the families and looks
        # it builds in and the kinds of photo the upload link takes
        "site_facts": list(intake.SITE_FACTS),
        "site_families": sorted(families.FAMILIES),
        "site_styles": [style for family in sorted(families.FAMILIES) for style in families.get(family).styles],
        "photo_kinds": list(photos.KIND_IDS),
        "recommendation_kinds": list(analyst.KINDS),  # capability layer K2
        "research_topics": list(research_material.TOPICS),  # capability layer K4: what request_research takes
        "research_statuses": list(tools_research.STATUSES),  # and get_research's status
    }
    # the constants made for the contract are the ones their checks hold to (each refused outside them elsewhere)
    assert values["contact_keys"] == ["name", "phone", "email"] and values["autopilot"] == ["off", "safe"]
    assert values["location_keys"] == ["geo_model", "areas", "hours", "phone", "phone_hours"]
    assert values["launch_statuses"] == ["proposed", "needs_verification"]
    # "choose" (the founder's ruling, 2026-10-13): no way chosen yet, so connect_account asks the owner which; and
    # "page_first" (the same day's): no page settled yet, so connect_account asks the page first
    assert values["connect_statuses"] == ["connected", "pending", "needs_agreement", "web_step", "choose", "page_first"]
    assert values["scopes"] == ["read", "manage", "leads"] and values["channels"] == ["google_search", "newsbreak"]
    assert {meta.scope for meta in registry.SURFACE} == set(values["scopes"])
    assert "issue_kinds" not in values
    assert values["pending_needs"] == ["code", "owner_yes"] and values["limit_sources"] == ["owner", "system"]
    assert values["plan_statuses"] == ["draft", "in_review", "approved", "superseded"]
    assert values["account_statuses"] == ["none", "pending", "active", "refused", "inactive", "needs_attention"]
    # the site Admiry builds: the page tool's own action and view, the facts a patch takes -- the names the question's
    # ids and the description's list use -- the two families, their six looks and the seven kinds of photo
    # follow-up 2026-10-12: fix_headline, the public tool's own fix for Admiry's own page (the home's button's body,
    # shared), is appended after build_site
    assert values["page_actions"][-2:] == ["build_site", "fix_headline"] and values["page_views"][-1] == "site"
    assert values["site_families"] == ["dental", "home_services"]
    assert sorted(values["site_styles"]) == ["bold", "boutique", "calm", "clean", "solid", "warm"]
    assert values["site_styles"][:3] == ["calm", "warm", "boutique"]  # the family order of sorted(site_families)
    assert values["photo_kinds"] == ["logo", "key", "place", "team", "work", "before_after", "dentist"]
    assert {"name", "phone", "place", "address", "service_area", "services"} <= set(values["site_facts"])
    assert set(intake.QUESTIONS) <= set(values["site_facts"])  # every question's answer key is a fact key
    assert len(values["site_facts"]) == len(set(values["site_facts"])) == 24  # insurance_claims left on 2026-10-15
    # the Researcher's work (capability layer K4): the topics an ask takes and the statuses an answer reads back, a
    # run that finished within the day (`recent`) before the one an owner reads (`ready`)
    assert values["research_topics"] == ["competitors", "search_phrases", "local_demand", "seasonality", "offer_angles"]
    assert values["research_statuses"] == ["not_on", "none", "recent", "queued", "running", "ready"]
    # the goal's writer and the account's writer refuse any other word, before they write
    with pytest.raises(ValueError, match="limit_source must be owner or system"):
        business.set_goal(conn, "biz_1", monthly_limit_usd=900, limit_source="admiry")
    with pytest.raises(ValueError, match="status must be one of none, pending, active"):
        channel_registry.save_account(conn, "biz_1", "google_search", mode="own_account", status="linked", external={})
    # a lead's source is the database's to check: its CHECK lists the same words, in the same order
    found = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid = 'lead'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE %s",
        ("%source%",),
    ).fetchall()
    assert [re.findall(r"'(\w+)'", r["d"]) for r in found] == [list(leads.SOURCES)]


def test_dropping_a_choice_from_the_code_changes_the_contract(monkeypatch):
    before = contract.dumps(contract.build())
    monkeypatch.setattr(funnel, "PERIODS", tuple(p for p in funnel.PERIODS if p != "last_month"))
    after = contract.build()
    assert after["values"]["periods"] == ["7d", "30d", "month"] and contract.dumps(after) != before


def test_a_changed_word_changes_the_contract(monkeypatch):
    """Final review C, Important 4b: what an assistant reads -- the instructions, each tool's description, each file of
    the skill -- is in the contract by its SHA-256 digest, so a changed sentence is a changed contract. The re-review's
    M-A: so is each prompt's text (as the prompt returns it for fixed sample arguments) and the connector docs' own
    words, the error list and the approval texts, which Muse saves when it builds its skill from /connect/docs."""
    words = contract.build()["words"]
    assert words["instructions"] == contract.digest(surface_words.INSTRUCTIONS)
    assert words["skill"] == {path: contract.digest(text) for path, text in assistant_skill.files().items()}
    assert set(words["descriptions"]) == set(PUBLIC)
    meta = next(m for m in registry.SURFACE if m.name == "pause_campaign")
    assert words["descriptions"]["pause_campaign"] == contract.digest(meta.tool.description)
    monkeypatch.setattr(meta.tool, "description", meta.tool.description + " One more sentence.")
    changed = contract.build()["words"]["descriptions"]
    assert {name for name in PUBLIC if changed[name] != words["descriptions"][name]} == {"pause_campaign"}
    monkeypatch.setattr(surface_words, "INSTRUCTIONS", surface_words.INSTRUCTIONS + " One more.")
    assert contract.build()["words"]["instructions"] != words["instructions"]
    # the prompts: every string argument "sample", every optional one None
    assert list(words["prompts"]) == list(told.PROMPTS)
    sampled = told.get_more_customers.fn(what_you_want="sample", website=None)
    assert sampled.startswith(assistant_skill.rules()) and "The owner says: sample\nBegin with" in sampled
    assert words["prompts"]["admiry-get-more-customers"] == contract.digest(sampled)
    said = told.record_lead_outcomes.fn
    monkeypatch.setattr(told.record_lead_outcomes, "fn", lambda: said() + " One more sentence.")
    changed = contract.build()["words"]["prompts"]
    assert {name for name in told.PROMPTS if changed[name] != words["prompts"][name]} == {"admiry-record-lead-outcomes"}
    # the connector docs' own words: a meaning in the error list, an approval text
    assert words["connector_docs"] == contract.digest(
        json.dumps([connect.ERRORS, connect.APPROVAL_TEXT], sort_keys=True)
    )
    errors = connect.ERRORS
    monkeypatch.setattr(connect, "ERRORS", [*errors[:-1], (errors[-1][0], "another meaning")])
    assert contract.build()["words"]["connector_docs"] != words["connector_docs"]
    monkeypatch.setattr(connect, "ERRORS", errors)
    monkeypatch.setattr(connect, "APPROVAL_TEXT", {**connect.APPROVAL_TEXT, "none": "runs at once"})
    assert contract.build()["words"]["connector_docs"] != words["connector_docs"]


def test_the_prompts_and_the_resources_are_what_the_server_lists(client, conn, strict_surface):
    key = business_with_key(conn, claim_state="claimed")
    built = contract.build()
    assert [p["name"] for p in built["prompts"]] == list(told.PROMPTS)
    listed = rpc(client, "prompts/list", key=key)["result"]["prompts"]
    assert sorted(built["prompts"], key=lambda p: p["name"]) == [
        {"name": p["name"], "arguments": [a["name"] for a in p.get("arguments") or []]}
        for p in sorted(listed, key=lambda p: p["name"])
    ]
    assert built["prompts"][0] == {"name": "admiry-get-more-customers", "arguments": ["what_you_want", "website"]}
    uris = [r["uri"] for r in rpc(client, "resources/list", key=key)["result"]["resources"]]
    found = rpc(client, "resources/templates/list", key=key)["result"]["resourceTemplates"]
    assert built["resources"] == sorted(uris + [t["uriTemplate"] for t in found])
    assert set(told.STATE_RESOURCES) | {gate.PERFORMANCE_RESOURCE, "admiry://docs"} <= set(built["resources"])


def test_the_results_have_the_keys_the_contract_names(client, conn, walk_fake, strict_surface):
    walk = planned(client, conn)
    for tool in contract.RESULT_KEYS:
        res = call_tool(client, tool, {}, key=walk["key"])
        assert not res["isError"], (tool, tool_text(res))
        assert set(contract.RESULT_KEYS[tool]) <= set(data(res)), tool
    state = data(call_tool(client, "get_marketing_state", {}, key=walk["key"]))
    assert list(state) == contract.RESULT_KEYS["get_marketing_state"]


def test_a_read_only_key_reads_everything_and_changes_nothing(client, conn, walk_fake, strict_surface):
    planned(client, conn)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    for tool in (
        "get_marketing_state",
        "get_business_profile",
        "get_connected_accounts",
        "get_marketing_plan",
        "get_campaigns",
        "get_campaign_performance",
        "get_landing_page",
        "get_activity_log",
    ):
        res = call_tool(client, tool, {}, key=reader)
        assert not res["isError"], (tool, tool_text(res))
    writes = (
        ("create_marketing_plan", {"message": WALK_TEXT}),
        ("update_marketing_plan", {"changes": {"monthly_budget_usd": 1500}}),
        ("launch_marketing_plan", {}),
        ("pause_campaign", {}),
        ("resume_campaign", {}),
        ("update_marketing_budget", {"monthly_usd": 1500}),
        ("update_business_profile", {"patch": {"name": "x"}}),
        ("connect_account", {}),
        ("update_landing_page", {"action": "use_url", "url": "https://brightsmile.example"}),
        ("get_billing", {"view": "topup_link"}),
    )
    keyless = ("create_marketing_plan", "get_billing")  # the two that take no idempotency_key
    for i, (tool, args) in enumerate(writes):
        sent = args if tool in keyless else {**args, "idempotency_key": f"ro-{i}"}
        res = call_tool(client, tool, sent, key=reader)
        assert res["isError"] and "lacks the 'manage' scope" in tool_text(res), (tool, tool_text(res))
    for tool, args in (
        ("get_leads", {}),
        ("update_lead", {"lead_id": "ld_x", "status": "won", "idempotency_key": "ro-l1"}),
        ("add_lead", {"kind": "call", "status": "won", "idempotency_key": "ro-l2"}),
    ):
        res = call_tool(client, tool, args, key=reader)
        assert res["isError"] and "lacks the 'leads' scope" in tool_text(res), tool
    assert plans.latest(conn, "biz_1")["version"] == 1 and plans.latest(conn, "biz_1")["status"] == "draft"


def test_the_owners_yes_never_launches(client, conn, walk_fake, strict_surface):
    """A launch spends money: only the code from the owner's page confirms it. No argument says "the owner agreed",
    the yes that confirms a page finds no launch to confirm, and a guessed code changes nothing."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "y1"}, key=walk["key"])
    assert data(asked)["status"] == "proposed" and data(asked)["needs_code"] is True
    launch = next(m for m in registry.SURFACE if m.name == "launch_marketing_plan")
    assert "owner_confirmed" not in launch.tool.parameters["properties"]
    for i, page in enumerate(
        ({"action": "unpublish"}, {"action": "publish", "token": "pv_any"}, {"action": "publish", "version": 1})
    ):
        yes = call_tool(
            client,
            "update_landing_page",
            {**page, "owner_confirmed": True, "idempotency_key": f"y2-{i}"},
            key=walk["key"],
        )
        assert yes["isError"] and "nothing is waiting for the owner's yes" in tool_text(yes), page
    guess = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "y3", "approval_code": "00-000"}, key=walk["key"]
    )
    assert guess["isError"] and "not valid for this proposal" in tool_text(guess)
    assert plans.latest(conn, "biz_1")["status"] == "draft"


def test_the_state_and_the_report_hold_no_person_for_a_key_without_leads(client, conn, walk_fake, strict_surface):
    walk = planned(client, conn, scopes=("read", "manage"))
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    leads.record(conn, page, {"name": "Ana Petrov", "phone": "6505550199", "message": "braces?"}, ("g1", "gclid"))
    for tool in ("get_marketing_state", "get_campaign_performance", "get_campaigns"):
        res = call_tool(client, tool, {}, key=walk["key"])
        assert not res["isError"], (tool, tool_text(res))  # a refusal would hold no person either
        text = str(res)
        assert "Ana" not in text and "6505550199" not in text and "braces" not in text, tool


def test_no_action_names_a_tool_an_assistant_cannot_call(client, conn, walk_fake, support_on, strict_surface):
    walk = planned(client, conn)
    seen: list[str] = []

    def note(tool: str, args: dict, key: str) -> None:
        res = call_tool(client, tool, args, key=key)
        offered = (res.get("structuredContent") or {}).get("next", {}).get("actions") or []
        seen.extend(a["tool"] for a in offered)

    # no page yet: the page's ways (the founder's ruling, 2026-10-13)
    note("get_marketing_state", {}, walk["key"])
    note("connect_account", {"idempotency_key": "d0"}, walk["key"])  # the same, as connect_account asks it
    settle_page(conn)  # then how the ads run, and a change
    note("get_marketing_state", {}, walk["key"])
    note("get_marketing_plan", {}, walk["key"])
    note("update_marketing_budget", {"monthly_usd": 1500, "idempotency_key": "d1"}, walk["key"])
    note("get_landing_page", {"view": "kit"}, walk["key"])
    note("get_landing_page", {}, walk["key"])
    note("launch_marketing_plan", {"idempotency_key": "d2"}, walk["key"])
    draft = business_with_key(conn, biz_id="biz_2", cls="draft")
    note("contact_support", {"message": "help", "summary": "", "reason": "human", "idempotency_key": "d3"}, draft)
    # the account linked and the page passing: the state's first thing left is the launch now
    google_ads_link.save(conn, "biz_1", CID, status="active")
    landing.save_check(conn, landing.set_owner_url(conn, "biz_1", PAGE_URL)["id"], CHECK_OK)
    note("get_marketing_state", {}, walk["key"])
    assert {"launch_marketing_plan", "update_marketing_plan", "update_landing_page", "connect_account"} <= set(seen)
    assert set(seen) <= gate.PUBLIC, sorted(set(seen) - gate.PUBLIC)


# Where a module writes a "tool" key whose value is not written out -- (file, the value as ast.unparse gives it) -- the
# places that exist today, and why each names no tool the scan below should read. Another value, a second place like
# one of these, a place anywhere else, or one of these gone fails that test: the list stays exactly what the code has.
UNREAD_TOOL_KEYS = {
    ("audit.py", "tool"): "the identity call_hash hashes: the call's own tool, not an action",
    ("envelope.py", "it['tool']"): "actions() copies the tool of the action it is given, written out where that is",
    ("surface/actions.py", "public"): "translate() writes the public tool from actions.TOOL, checked below",
    ("tools_account.py", "r['tool']"): "get_audit_log's detail for a platform key: the row's own command",
    ("tools_state.py", "PENDING_TOOL.get(pr['kind'])"): "a waiting change's confirming tool, checked below",
    ("tools_state.py", "ask['tool']"): "a launch refusal's ask (S0): the tool from plan_review.ASKS, checked below",
    # capability layer K2: a recommendation's action is its candidate's tool, from roles.analyst.KINDS, checked below
    ("roles/analyst.py", "c['tool']"): "the stored recommendation copies its candidate's tool (analyst.KINDS)",
    ("roles/analyst.py", "r['tool']"): "owner_view's action is the stored recommendation's tool (analyst.KINDS)",
    ("tools_insights.py", "r['action']['tool']"): "get_insights' next is its recommendations' actions (analyst.KINDS)",
    # spec 2026-10-11 §3: the checklist's fix -- one return point, so the tool it names is read once, checked below
    ("launch_check.py", "tool"): "fix_for's one return point: the tool from FIX_TOOLS, or a page in hand's step's",
    ("tools_state.py", "fix['tool']"): "_fix_next's one return point: fix_for's own tool (FIX_TOOLS, checked below)",
    # final review, item 1: a page choice's tool -- update_landing_page, or the one a page in hand's step writes out
    # (launch_check.page_in_hand's "tool" key, which this scan reads)
    ("launch_check.py", "choice.get('tool', 'update_landing_page')"): "page_actions: the choice's tool, written out",
}


def test_every_tool_an_action_can_name_is_public_or_translated():
    """The static half of the test above, for the paths it does not walk and for commands added later: every
    `"tool": "<name>"` a module writes into an action is a public tool or has a translation, and every translation
    lands on a public tool. A "tool" value the scan cannot read is allowed only where UNREAD_TOOL_KEYS says why
    (`None` names no tool)."""
    named: dict[str, set[str]] = {}
    unread: list[tuple[str, int, str]] = []
    for path in sorted((REPO / "agent").rglob("*.py")):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "tool"):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    named.setdefault(value.value, set()).add(path.name)
                elif not (isinstance(value, ast.Constant) and value.value is None):
                    unread.append((path.relative_to(REPO / "agent").as_posix(), value.lineno, ast.unparse(value)))
    assert sorted((place, value) for place, _, value in unread) == sorted(UNREAD_TOOL_KEYS), unread
    unknown = {name: sorted(files) for name, files in named.items() if name not in gate.PUBLIC | set(actions.TOOL)}
    assert not unknown, unknown
    assert {public for public, _ in actions.TOOL.values()} <= gate.PUBLIC
    # the state names the tool that confirms a waiting change by a table, not by a "tool" key
    assert set(tools_state.PENDING_TOOL.values()) <= gate.PUBLIC
    # and the tool each launch refusal's ask sends the owner's answer to (S0, plan_review.ASKS)
    assert {ask["tool"] for _, ask in plan_review.ASKS} <= gate.PUBLIC
    # and every tool a recommendation's action can name (capability layer K2: the candidates' kinds)
    from agent.roles import analyst

    assert set(analyst.KINDS.values()) <= gate.PUBLIC
    # and every tool the checklist's fix can name (spec 2026-10-11 §3, plan D7: launch_check.FIX_TOOLS)
    assert set(launch_check.FIX_TOOLS.values()) <= gate.PUBLIC


def test_the_sites_commands_in_a_next_action_are_the_page_tools_action_and_view():
    """A3p-6 batch 2 Task 6 (plan Q15): a site's `next` names its commands -- `build_site` ("Change the look"),
    `publish_page` and `get_site` -- and an assistant is told the public tool and the arguments that pick the same thing.
    What a real answer's `next` carries is walked in test_sites_flow."""
    for command, public, args in (
        ("build_site", "update_landing_page", {"action": "build_site"}),
        ("publish_page", "update_landing_page", {"action": "publish"}),
        ("get_site", "get_landing_page", {"view": "site"}),
    ):
        said = {"next": {"kind": "actions", "actions": [{"id": "x", "label": "x", "tool": command}]}}
        (translated,) = actions.translate(said)["next"]["actions"]
        assert (translated["tool"], translated["args"]) == (public, args), command
        assert translated["tool"] in gate.PUBLIC
        assert args.get("action", args.get("view")) in {*supporting.PAGE_ACTIONS, *supporting.PAGE_VIEWS}
