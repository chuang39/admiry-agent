"""The public contract as data (spec 2026-10-01 §3, §9.7, §12; plan G1 Task 13): every public tool's name, class,
scope, approval, annotations, card and argument schema; the top-level keys of the six read results assistants build
on; the fixed choices an assistant passes or reads (`values`, each the constant the code checks it with); the
prompts and their arguments; the resources; and the SHA-256 digests of the words an assistant reads (`words`: the
instructions, every tool's description, every file of the skill, each prompt's text, and the connector docs' own
words -- the error list and the approval texts, which Muse saves when it builds its skill from /connect/docs). A
golden file per version pins it: a change to it, a word included, is a new version, never an edit. The commands
behind a tool are not part of it: they are internal (test_surface_contract's dispatch scan holds them to the code).

To publish a new contract: bump gate.SURFACE_VERSION (a second version on one date is 2026-10-01.2) and the two places
SKILL.md carries it, run test_surface_contract's golden test once with SURFACE_GOLDEN_WRITE=1 (it writes the file and
fails with its digest) and add the digest to PUBLISHED."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastmcp.prompts import Prompt

from .. import (
    assistant_skill,
    business,
    connect,
    consult,
    funnel,
    leads,
    mcp_app,
    page_builder,
    plans,
    profile,
    tools_business,
    tools_channels,
    tools_consult,
    tools_insights,
    tools_proposals,
    tools_research,
    tools_state,
    tools_support,
)
from ..channels import registry as channel_registry
from ..roles import analyst, research_material
from ..sites import families, intake, photos
from . import gate, load, registry, words
from .registry import SURFACE

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"
RESULT_KEYS: dict[str, list[str]] = {
    "get_marketing_state": list(tools_state.STATE_KEYS),
    "get_business_profile": [*profile.SECTIONS, "knowledge_version"],
    "get_marketing_plan": ["plan_id", "version", "status", "plan_url", "cards"],
    "get_campaigns": ["channels", "footer"],
    "get_campaign_performance": [*funnel.REPORT_KEYS, "goal"],
    "get_leads": ["leads", "counts", "awaiting_outcome"],
    "get_insights": list(tools_insights.KEYS),  # a platform key's answer adds `team`, Admiry's own (not contract)
    "get_research": list(tools_research.KEYS),  # a platform key's answer adds `team`, Admiry's own (not contract)
}
# A published version -> the SHA-256 of its golden file. Editing a published contract's file fails the test.
PUBLISHED: dict[str, str] = {
    "2026-10-01": "b4c3ecba82463fb7c4e502bf40894d8b2e4aa4152738c1494f7ae26f55707c3d",
    "2026-10-03": "d4a4e7069a297edf682f25583328d3c686836f18de674f2e1b67b806056039c7",
    "2026-10-03.2": "134eb88ca0688868988730ec1bb2190c466e0436c11bc1e73c786eb56f73f85b",
    "2026-10-04": "4c5c1fe5d9143947595ad03e0392052817fe204df1f1026dc50b636db7b38fdf",
    "2026-10-05": "c7661ae7f60365db1eb6aa9e40feec130edcac4f0f4565aef003ade430f7e164",
    "2026-10-05.2": "0b175c5872d48294e03ba971d5a72d3dea87eac96a43115ceae82eb3c1ded254",
    "2026-10-07": "fceabdd82a02026568e798c0d8b23936582b93edd0369cb7a82e104013bf18d0",
    "2026-10-07.2": "ce1101719fb83b41a7221a5121bfa6fab84dbe99e0979ebc913a673749472a6b",
    "2026-10-07.3": "1a7b95e2ea904869a4841077ce3f367813f3df3929db654e5ed3028f9d3cb9d6",
    "2026-10-08": "b304e892823ebd0d1bd5fd64c7611c7979ca79e5e7bc0e2e08de46078a0b0437",
    "2026-10-09": "3ad8a7bf8ee478a04dfdc786a06f2e56a18327989ab1f88a3fe6f96a268f07a7",
    "2026-10-10": "15d37e311b638d29fe015dee6cf9f0abcf1ad898c385a021881f0f840e8d6bae",
    "2026-10-11": "fe6d342adfd268bd3e83f82deda0b8ec7b2c6fc213b95d85ded89387cdf315f5",
    "2026-10-12": "81f1387de5c32548e82dc5b34a5b5788a99b23867c9d7c75393bb7b3f440adcd",
    "2026-10-13": "e489283a67da30425e57302a423b1fd671573712972e98a8465e820913dbf386",
    "2026-10-15": "9f2a2d27a6757048e3b7d7f67dfb8156bc10e3ee962ebac0570018f544bfa18c",
    "2026-11-04": "d6bf1f6077d212a7b3f9542500e09b5ec4ac48ef3c71a7ad937e2a08da452a0d",
}


def build() -> dict:
    """The contract, its tools in name order (the order they register in is no part of it). It loads the surface
    first: this module imports a command module, so a process that imports it first has mcp_app skip the public
    tools (they register through surface.load(), Task 9), and the contract would silently hold none."""
    load()
    tools = []
    for meta in sorted(SURFACE, key=lambda m: m.name):
        notes = meta.tool.annotations
        tools.append(
            {
                "name": meta.name,
                "class": meta.cls,
                "scope": meta.scope,
                "approval": meta.approval,
                "read_only": bool(notes.readOnlyHint) if notes else False,
                "destructive": bool(notes.destructiveHint) if notes else False,
                "idempotent": bool(notes.idempotentHint) if notes else False,
                "open_world": bool(notes.openWorldHint) if notes else False,
                "card": ((meta.tool.meta or {}).get("ui") or {}).get("resourceUri"),
                "input_schema": meta.tool.parameters,
            }
        )
    return {
        "version": gate.SURFACE_VERSION,
        "tools": tools,
        "result_keys": RESULT_KEYS,
        "values": _values(),
        "prompts": _prompts(),
        "resources": _resources(),
        "words": _words(),
    }


def _values() -> dict[str, list[str]]:
    """The fixed choices an assistant passes (an argument, a key inside one) or reads (a status in an answer), each
    read from the constant the code checks it with, never typed here: a lead's source is the database's to check, and
    its CHECK lists `leads.SOURCES` (test_surface_contract). The kinds of an issue are not among them: the platforms
    add their own."""
    from . import plan, supporting  # the public tools' own modules: load() has imported them

    return {
        "periods": list(funnel.PERIODS),
        "lead_statuses": list(leads.STATUSES),
        "lead_kinds": list(leads.KINDS),
        "lead_reasons": list(leads.REASONS),
        "contact_keys": list(leads.CONTACT_KEYS),
        "goal_types": list(business.GOAL_TYPES),
        "plan_levels": list(tools_consult.LEVEL_NAMES),
        "plan_objectives": list(plans.OBJECTIVE_WORDS),
        "plan_changes": list(plan.PLAN_CHANGES),
        "goal_changes": list(tools_business.GOAL_CHANGES),
        "autopilot": list(tools_business.AUTOPILOT_MODES),
        "preferred_outcomes": list(plan.OUTCOMES),
        "known_facts": list(consult.KNOWN_FACTS),
        "patch_keys": list(supporting.PUBLIC_PATCH),
        "location_keys": list(profile.LOCATION_KEYS),
        "page_views": list(supporting.PAGE_VIEWS),
        "page_actions": list(supporting.PAGE_ACTIONS),
        "billing_views": list(supporting.BILLING_VIEWS),
        "page_sections": list(page_builder.SECTIONS),
        "page_templates": list(page_builder.TREES),
        "connect_modes": list(tools_channels.MODES),
        # the channels the registry runs (an adapter is registered), in card order
        "channels": [channel for channel in channel_registry.ORDER if channel_registry.known(channel)],
        "launch_statuses": list(tools_proposals.LAUNCH_STATUSES),
        "connect_statuses": list(tools_channels.CONNECT_STATUSES),
        "scopes": list(registry.SCOPES),
        "profile_sections": list(profile.SECTIONS),  # get_business_profile's `include`
        "geo_models": list(profile.GEO_MODELS),  # a location's `geo_model`
        "support_reasons": list(tools_support.REASONS),  # contact_support's `reason`
        "lead_sources": list(leads.SOURCES),
        "pending_needs": list(tools_state.NEEDS),
        "limit_sources": list(business.LIMIT_SOURCES),
        "plan_statuses": list(plans.STATUSES),
        "account_statuses": list(channel_registry.ACCOUNT_STATUSES),
        # the website Admiry builds (A3p-6 batch 2, plan Q15): the keys build_site's `facts` takes, the families it
        # builds for and their looks (a family's own order, the families sorted), and the kinds of photo the link takes
        "site_facts": list(intake.SITE_FACTS),
        "site_families": sorted(families.FAMILIES),
        "site_styles": [style for family in sorted(families.FAMILIES) for style in families.get(family).styles],
        "photo_kinds": list(photos.KIND_IDS),
        # what a recommendation of Admiry's daily look can be (capability layer K2): its `kind`, each one public tool
        "recommendation_kinds": list(analyst.KINDS),
        # what request_research takes and get_research's status (capability layer K4)
        "research_topics": list(research_material.TOPICS),
        "research_statuses": list(tools_research.STATUSES),
    }


def _registered_prompts() -> dict[str, Prompt]:
    """The prompts told.py registers, by name."""
    from . import told  # registers the prompts: load() has imported it

    return {p.name: p for p in vars(told).values() if isinstance(p, Prompt)}


def _prompts() -> list[dict]:
    """Each prompt an assistant is offered, in told.PROMPTS' order, with its arguments' names in order."""
    from . import told

    found = _registered_prompts()
    return [{"name": name, "arguments": [a.name for a in found[name].arguments or []]} for name in told.PROMPTS]


def _sampled(prompt: Prompt) -> str:
    """The text `prompt` returns for fixed sample arguments: every required one "sample", every optional one None."""
    return prompt.fn(**{a.name: "sample" if a.required else None for a in prompt.arguments or []})


def _resources() -> list[str]:
    """What resources/list shows -- the state, the docs, the daily look, the research, the knowledge's index, every
    file of the skill, the two cards -- and the two templates' URI templates, sorted."""
    from . import told  # registers the resources: load() has imported it

    listed = [
        *told.STATE_RESOURCES,
        told.DOCS_RESOURCE,
        told.INSIGHTS_RESOURCE,
        told.RESEARCH_RESOURCE,
        told.KNOWLEDGE_RESOURCE,
        *(assistant_skill.uri(path) for path in assistant_skill.files()),
        mcp_app.PLAN_VIEW_URI,
        mcp_app.CHANNELS_VIEW_URI,
    ]
    return sorted([*listed, gate.PERFORMANCE_RESOURCE, told.KNOWLEDGE_TEMPLATE])


def _words() -> dict:
    """The digests of what an assistant reads: a changed word changes the contract, so a saved skill learns of a
    changed rule through the version. A prompt's words are the text it returns for fixed sample arguments
    (`_sampled`); the connector docs' own words are the error list and the approval texts, which Muse saves when it
    builds its skill from /connect/docs."""
    from . import told

    found = _registered_prompts()
    return {
        "instructions": digest(words.INSTRUCTIONS),
        "descriptions": {meta.name: digest(meta.tool.description or "") for meta in SURFACE},
        "skill": {path: digest(text) for path, text in assistant_skill.files().items()},
        "prompts": {name: digest(_sampled(found[name])) for name in told.PROMPTS},
        "connector_docs": digest(json.dumps([connect.ERRORS, connect.APPROVAL_TEXT], sort_keys=True)),
    }


def dumps(contract: dict) -> str:
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def golden_path(version: str) -> Path:
    return GOLDEN_DIR / f"surface_contract_{version}.json"
