"""What an assistant is told beyond the tools (spec 2026-10-01 §9.5, §9.6; plan G1 Task 12): four prompts in the
owner's words, the state as resources, the docs and the skill's files. The instructions are in words.py (mcp_app
needs them before this module can load). A state resource runs the same command as its tool: no second query.

A prompt's arguments are text and annotated `str` (no postponed annotations, no `str | None`): FastMCP describes any
other annotation as "a JSON string matching" its schema (final review C, minor 6), and reads an argument sent to it
as JSON first, which takes the quotes off words that happen to be a JSON string. So an argument's words, where a host
that shows a prompt as a form reads them (re-review C, M-J), are its default's `Field(description=...)`, never an
`Annotated[str, ...]`, which is such another annotation; an optional one defaults to ""."""

import json
import logging

from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import Resource, ResourceTemplate
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .. import assistant_skill, knowledge_index
from ..connect import _context
from ..mcp_app import mcp
from ..templating import env as _env
from . import gate
from .registry import dispatch

log = logging.getLogger(__name__)

PROMPTS = (
    "admiry-get-more-customers",
    "admiry-how-are-my-ads-doing",
    "admiry-promote-a-service",
    "admiry-record-lead-outcomes",
)
STATE_RESOURCES = ("admiry://marketing/state", "admiry://business/profile", "admiry://marketing/plan")
DOCS_RESOURCE = "admiry://docs"
INSIGHTS_RESOURCE = "admiry://marketing/insights"  # the Analyst's latest look (capability layer K2): get_insights' data
RESEARCH_RESOURCE = "admiry://marketing/research"  # the Researcher's latest look (layer K4): get_research's data
KNOWLEDGE_RESOURCE = "admiry://knowledge"  # capability layer K3: the curated knowledge's index, INDEX.md as it ships
KNOWLEDGE_TEMPLATE = "admiry://knowledge/{vertical}/{topic}"  # one reviewed file, verbatim
NO_KNOWLEDGE = "Admiry has no knowledge file {id}; admiry://knowledge lists them."


def _with_rules(first_step: str) -> str:
    return f"{assistant_skill.rules()}\n{first_step}"


@mcp.prompt(name=PROMPTS[0], description="Plan advertising for the owner's business with Admiry.")
def get_more_customers(
    what_you_want: str = Field(description="the owner's words"),
    website: str = Field("", description="their site, if any"),
) -> str:
    said = f"The owner says: {what_you_want}" + (f" Their website: {website}" if website else "")
    return _with_rules(
        f"{said}\nBegin with `get_marketing_state`. If nothing is begun, `create_marketing_plan` with the owner's "
        "words as `message` and their website as `website_url`; otherwise follow its `next`."
    )


@mcp.prompt(name=PROMPTS[1], description="How the owner's advertising is doing, and what needs them.")
def how_are_my_ads_doing(period: str = Field("30d", description="7d, 30d, month or last_month")) -> str:
    return _with_rules(
        "The owner asks how their advertising is doing. Call `get_marketing_state`, then `get_insights` for Admiry's "
        f"latest look and suggestions, then `get_campaign_performance` with period {period}. Show each `say` as "
        "written, offer the suggestions in their own words, and say how many leads still wait for an outcome."
    )


@mcp.prompt(name=PROMPTS[2], description="Put a service or an offer in front in the owner's advertising.")
def promote_a_service(
    service: str = Field(description="what to promote, in the owner's words"),
    offer: str = Field("", description="an offer to lead with, if any"),
) -> str:
    said = f"The owner wants to promote: {service}" + (f", with this offer: {offer}" if offer else "")
    return _with_rules(
        f"{said}.\nCall `get_marketing_state`. With a plan, send the owner's words to `update_marketing_plan` as "
        "`message`; with a consultation in progress, finish it first (its `next`); with nothing begun, begin with "
        "`create_marketing_plan`."
    )


@mcp.prompt(name=PROMPTS[3], description="Tell Admiry which leads became jobs.")
def record_lead_outcomes() -> str:
    return _with_rules(
        "The owner wants to say what their leads became. Call `get_leads` with awaiting_outcome true, go through "
        "them with the owner one at a time, and record each answer with `update_lead`: the value when a job was won, "
        "the reason when it was lost. For a customer who is not in the list, `add_lead`."
    )


def _data(command: str, **args) -> str:
    """A command's `data` as JSON: the resource of a read tool is that tool's own answer, refused in its words."""
    try:
        return json.dumps(dispatch(command, **args)["data"], default=str)
    except ToolError as e:
        raise ResourceError(str(e)) from e


def state_resource() -> str:
    return _data("marketing_state")


def profile_resource() -> str:
    return _data("business_profile")


def plan_resource() -> str:
    return _data("get_plan", plan_id=None)  # the latest plan; reading never builds one


def insights_resource() -> str:
    return _data("insights", day=None)  # the latest look the owner may read, as get_insights gives it


def research_resource() -> str:
    return _data("get_research")  # the latest research an owner may read, as get_research gives it


async def performance_resource(period: str) -> str:
    # FastMCP sends a fixed resource's sync function to a worker thread, but calls a template's function on the event
    # loop: the command waits on the database, so it goes to the same worker threads, in this context (the gate's
    # name, the request's headers).
    return await run_in_threadpool(_data, "performance", period=period)


def docs_resource() -> str:
    return _env.get_template("llms.txt.j2").render(**_context())


def knowledge_resource() -> str:
    # The files ship with the image: no command runs and no row is written, as for the skill's files. FastMCP sends
    # this fixed resource's sync function to a worker thread.
    return knowledge_index.served_index_text()  # INDEX.md as it ships, without a file waiting for its review (plan P10)


def _knowledge_file(vertical: str, topic: str) -> str:
    # A reviewed file verbatim: its frontmatter (sources, confidence, the review) at its head is its metadata (plan
    # P11). Found by id among the loaded files, never by a path built from the address; one waiting for its review is
    # none. A file that went after the load is refused as one nobody has, and logged.
    name = f"{vertical}/{topic}"
    entry = knowledge_index.get(name)
    if entry is None:
        raise ResourceError(NO_KNOWLEDGE.format(id=name))
    try:
        return knowledge_index.file_text(entry)
    except knowledge_index.KnowledgeError as e:
        log.error("knowledge: %s; this file is not served until it is fixed", e)
        raise ResourceError(NO_KNOWLEDGE.format(id=name)) from e


async def knowledge_file_resource(vertical: str, topic: str) -> str:
    # FastMCP calls a template's function on the event loop, and the first read parses every file: it goes to a worker
    # thread, as performance_resource's command does.
    return await run_in_threadpool(_knowledge_file, vertical, topic)


# Added as objects, as mcp_view adds its views: the `@mcp.resource` decorator's object mode is deprecated in FastMCP
# 3.4 and warns once per resource at import.
for _fn, _uri, _name, _mime in (
    (state_resource, STATE_RESOURCES[0], "Admiry marketing state", "application/json"),
    (profile_resource, STATE_RESOURCES[1], "Admiry business profile", "application/json"),
    (plan_resource, STATE_RESOURCES[2], "Admiry marketing plan", "application/json"),
    (docs_resource, DOCS_RESOURCE, "Admiry connector docs", "text/plain"),
    (insights_resource, INSIGHTS_RESOURCE, "Admiry daily look", "application/json"),
    (research_resource, RESEARCH_RESOURCE, "Admiry research", "application/json"),
    (knowledge_resource, KNOWLEDGE_RESOURCE, "Admiry knowledge", "text/markdown"),
):
    mcp.add_resource(Resource.from_function(_fn, _uri, name=_name, mime_type=_mime))
mcp.add_template(
    ResourceTemplate.from_function(
        performance_resource,
        gate.PERFORMANCE_RESOURCE,  # the template a read under it is recorded by (gate.resource_tool)
        name="Admiry marketing performance",
        mime_type="application/json",
    )
)
mcp.add_template(
    ResourceTemplate.from_function(
        knowledge_file_resource, KNOWLEDGE_TEMPLATE, name="Admiry knowledge file", mime_type="text/markdown"
    )
)


def _reader(path: str):
    def read() -> str:
        return assistant_skill.text(path)

    return read


for _path in assistant_skill.files():
    mcp.add_resource(
        Resource.from_function(
            _reader(_path), assistant_skill.uri(_path), name=f"Admiry skill: {_path}", mime_type="text/markdown"
        )
    )
