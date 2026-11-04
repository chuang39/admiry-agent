"""What an assistant sees (spec 2026-10-01 §3, §9.7): the public tools, never the commands behind them.

One middleware on the FastMCP server: `tools/list` shows only `PUBLIC`; a call to a public tool runs with
`SURFACE_TOOL` set, which `mcp_app.run()` writes into the audit row beside the command's own name, and with
`SURFACE_ARGS` set, which with it is the call's identity (`call()`, hashed onto the same row); a call to a
name assistants knew is answered with its replacement, or with why it has none; any other name is unknown. A prompt or
a resource assistants knew is answered the same way, the gate open or closed: the old names exist in neither. A
command authenticates, authorizes and audits itself (`run()`), so this gate is the product's contract, not its
security.

`EXPOSE_COMMANDS` puts the commands back on the surface. It is False in production. The test process sets it True
(conftest.py): the suite written before the surface calls commands over /mcp, through FastMCP's own validation.

This module imports only `config` from `agent` (a leaf): `mcp_app` imports it before it creates the server."""

from __future__ import annotations

import copy
import logging
from contextvars import ContextVar

from fastmcp.exceptions import PromptError, ResourceError, ToolError
from fastmcp.server.middleware import Middleware

from .. import config

log = logging.getLogger(__name__)

SURFACE_TOOL: ContextVar[str | None] = ContextVar("agent_surface_tool", default=None)
# The arguments of the public call in progress, as the assistant sent them (Task 18): with the tool's name they are
# the call's identity, which a repeat of the same call is found by.
SURFACE_ARGS: ContextVar[dict | None] = ContextVar("agent_surface_args", default=None)
SECRET_ARGS = ("approval_code",)  # never part of a stored identity: a code is a secret
EXPOSE_COMMANDS = False  # production: assistants see the public tools only. conftest.py sets it True for the suite.
# What a resource read stores as its public name (final review A m1): the address is the caller's, of any length. The
# performance template (surface/told.py registers it here) is named by its template, a fixed resource by its address,
# anything else by the first MAX_RESOURCE_NAME characters of what was sent. A knowledge file (the other template,
# told.KNOWLEDGE_TEMPLATE) is named that last way, and no row carries it: its read runs no command.
PERFORMANCE_RESOURCE = "admiry://marketing/performance/{period}"
MAX_RESOURCE_NAME = 200
PUBLIC: set[str] = set()  # filled by registry.public_tool and registry.passthrough
# A tool assistants knew (the 47 on main before this surface, spec Appendix A) -> the public tool to use now.
RETIRED: dict[str, str] = {
    "whoami": "get_marketing_state",
    "list_proposals": "get_marketing_state",
    "get_proposal": "get_marketing_state",
    "get_audit_log": "get_activity_log",
    "describe_business": "update_business_profile",
    "update_business": "update_business_profile",
    "get_business": "get_business_profile",
    "request_connect_link": "connect_account",
    "connect_channel": "connect_account",
    "connect_google_ads": "connect_account",
    "get_connection_status": "get_connected_accounts",
    "get_channel_status": "get_campaigns",
    "get_status_issues": "get_campaigns",
    "get_report": "get_campaign_performance",
    "get_platform_statement": "get_campaign_performance",
    "get_page_stats": "get_campaign_performance",
    "pause": "pause_campaign",
    "resume": "resume_campaign",
    "start": "create_marketing_plan",
    "create_plan": "create_marketing_plan",
    "answer": "update_marketing_plan",
    "adjust": "update_marketing_plan",
    "revise_plan": "update_marketing_plan",
    "set_goal": "update_marketing_plan",
    "recommend": "get_marketing_plan",
    "get_plan": "get_marketing_plan",
    "approve_plan": "launch_marketing_plan",
    "confirm_proposal": "launch_marketing_plan",
    "get_page": "get_landing_page",
    "check_landing_page": "get_landing_page",
    "get_page_kit": "get_landing_page",
    "create_page": "update_landing_page",
    "revise_page": "update_landing_page",
    "update_page": "update_landing_page",
    "set_landing_page": "update_landing_page",
    "preview_page": "update_landing_page",
    "publish_page": "update_landing_page",
    "unpublish_page": "update_landing_page",
    "list_leads": "get_leads",
    "get_balance": "get_billing",
    "list_payments": "get_billing",
    "request_payment_link": "get_billing",
}
# A tool assistants knew that no tool replaces -> why
GONE: dict[str, str] = {
    "list_keys": "keys are managed on Admiry's Settings page",
    "revoke_key": "keys are managed on Admiry's Settings page",
    "decline_proposal": "a change the owner does not confirm simply expires",
}
# A retired name that did two things -> the sentence for the one its replacement does not do (the launch's code goes
# to launch_marketing_plan; a page's yes goes to the page's own tool)
ALSO: dict[str, str] = {
    "confirm_proposal": (
        "That is where a launch is confirmed, with the owner's approval code; a page is published or taken down with "
        "`update_landing_page`, called again with owner_confirmed true."
    ),
}
# The prompt and the resources assistants knew on main (final review C, minor 5) -> what replaced each; None: no
# resource does (the page kit, one business's own, comes from a tool now: PAGE_KIT_NOW says which)
RETIRED_PROMPTS: dict[str, str] = {"admiry-plan-a-campaign": "admiry-get-more-customers"}
RETIRED_RESOURCES: dict[str, str | None] = {
    "admiry://skill": "skill://admiry-marketing/SKILL.md",
    "admiry://page-kit": None,
}
PAGE_KIT_NOW = "the page guide for this business comes from `get_landing_page` with view kit"
# The public contract's name (spec 2026-10-01 §9.7): the docs, the skill and get_marketing_state carry it; it changes
# whenever the contract's golden file does (surface/contract.py). Here, in the leaf module, so a command can read it.
SURFACE_VERSION = "2026-11-04.2"


def again() -> str:
    """The sentence every answer to a name assistants knew closes with -- a tool's, a prompt's, a resource's: the
    tools changed, and the docs are where to read them again."""
    docs = f"{config.load().public_base_url}/connect/docs"  # where connect.py serves them
    return (
        f"Admiry's tools changed (version {SURFACE_VERSION}); read {docs} again and update the Admiry skill you saved."
    )


def retired_sentence(name: str) -> str:
    """What a call to a name assistants knew is answered with: the tool to use now (or why there is none), and that
    the docs changed."""
    if name in GONE:
        return f"`{name}` is no longer a tool: {GONE[name]}. {again()}"
    also = f" {ALSO[name]}" if name in ALSO else ""
    return f"`{name}` was replaced by `{RETIRED[name]}`.{also} {again()}"


def retired_prompt_sentence(name: str) -> str:
    """What a get of a prompt assistants knew is answered with: the prompt to use now, and that the docs changed."""
    return f"`{name}` was replaced by the prompt `{RETIRED_PROMPTS[name]}`. {again()}"


def retired_resource_sentence(uri: str) -> str:
    """What a read of a resource assistants knew is answered with: the resource to read now, or, for the page kit,
    where it comes from now; and that the docs changed."""
    new = RETIRED_RESOURCES[uri]
    if new is None:
        return f"`{uri}` is no longer a resource: {PAGE_KIT_NOW}. {again()}"
    return f"`{uri}` was replaced by `{new}`. {again()}"


def call() -> tuple[str, dict] | None:
    """The public call in progress -- its tool and its arguments as sent, an approval code blanked in whatever form it
    came (a number too, 0 included) -- or None outside one (a command the homepage calls in process, a resource
    read)."""
    name, sent = SURFACE_TOOL.get(), SURFACE_ARGS.get()
    if name is None or sent is None:
        return None
    return name, {k: ("***" if k in SECRET_ARGS and v not in (None, "") else v) for k, v in sent.items()}


def resource_tool(uri) -> str:
    """What a read of the resource at `uri` stores as its public name: the template of the performance resource for any
    address under it, else the address -- whole for every fixed resource, cut to MAX_RESOURCE_NAME characters for
    anything longer, which is no resource of Admiry's. A knowledge file's address is named like any other: its read
    runs no command, so no row is written to carry the name."""
    sent = str(uri)
    if sent.startswith(PERFORMANCE_RESOURCE.removesuffix("{period}")):
        return f"resource:{PERFORMANCE_RESOURCE}"
    return f"resource:{sent[:MAX_RESOURCE_NAME]}"


class SurfaceGate(Middleware):
    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return tools if EXPOSE_COMMANDS else [t for t in tools if t.name in PUBLIC]

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        if name in PUBLIC:
            token = SURFACE_TOOL.set(name)
            # its own copy, all the way down: nothing a tool or a command does to an argument changes the identity
            sent = SURFACE_ARGS.set(copy.deepcopy(context.message.arguments or {}))
            try:
                return await call_next(context)
            finally:
                SURFACE_ARGS.reset(sent)
                SURFACE_TOOL.reset(token)
        if EXPOSE_COMMANDS:
            return await call_next(context)
        if name in RETIRED or name in GONE:
            log.info("a call to the retired name %s was turned away", name)
            raise ToolError(retired_sentence(name))
        raise ToolError(f"Unknown tool: {name}")

    async def on_get_prompt(self, context, call_next):
        name = context.message.name
        if name in RETIRED_PROMPTS:  # the gate open or closed: no prompt has this name any more
            log.info("a get of the retired prompt %s was turned away", name)
            raise PromptError(retired_prompt_sentence(name))
        return await call_next(context)

    async def on_read_resource(self, context, call_next):
        uri = str(context.message.uri)
        if uri in RETIRED_RESOURCES:  # the gate open or closed: no resource has this address any more
            log.info("a read of the retired resource %s was turned away", uri)
            raise ResourceError(retired_resource_sentence(uri))
        token = SURFACE_TOOL.set(resource_tool(context.message.uri))
        try:
            return await call_next(context)
        finally:
            SURFACE_TOOL.reset(token)
