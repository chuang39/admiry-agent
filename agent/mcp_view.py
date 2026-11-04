"""The cards as MCP Apps views (spec 2026-09-26 §5.2, A1b-4d; spec 2026-09-29-admiry-channels-design §7.2): `ui://`
resources that hosts with the `io.modelcontextprotocol/ui` extension (Claude, ChatGPT) render in the chat when a tool
that points at one answers -- the plan card for the public tools `get_marketing_plan`, `update_marketing_plan` and
`update_marketing_budget`, the channels card for `get_campaigns` (G1 Task 13); every other host ignores the tool's
`_meta.ui` and reads the envelope as before. The commands behind them (`recommend`, `adjust` and `get_plan`;
`get_channel_status`) still carry the same pointer, which nothing reads: assistants are not shown commands
(surface/gate.py), the homepage runs `recommend` and `adjust` in process and draws its cards from the command's own
answer right after a build or a change (from the state's own on a reload), and the mail path runs those and
`get_plan` in process. Each page is a single file with inline style and script (the host's
default CSP loads nothing else) and speaks the extension's postMessage protocol itself, so the server ships no bundle --
the plan card's one fetch is the plan's picture (M1 Task 7), from the web host, the one origin its resource declares
(`csp.resourceDomains`). `/plan/{id}` stays the fallback for every client. The resources are added as objects rather
than with the `@mcp.resource` decorator, whose object mode is deprecated in FastMCP 3.4.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastmcp.apps import UI_MIME_TYPE, AppConfig, ResourceCSP, app_config_to_meta_dict
from fastmcp.resources import Resource

from . import config
from .mcp_app import CHANNELS_VIEW_URI, PLAN_VIEW_URI, mcp

_TEMPLATES = Path(__file__).parent / "templates"
_BORDER = {"ui": app_config_to_meta_dict(AppConfig(prefers_border=True))}  # the card sits in a bordered frame


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# the plan card shows the picture a channel shows with the ad (`cards.ads_preview.image`, plans.py), which the web host
# serves at /static/creative/: the host lets the card load it from there, and from nowhere else
_PLAN = {
    "ui": app_config_to_meta_dict(
        AppConfig(prefers_border=True, csp=ResourceCSP(resource_domains=[_origin(config.load().web_base_url)]))
    )
}


def view_html(name: str = "plan_view.html") -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def plan_view() -> str:
    return view_html("plan_view.html")


def channels_view() -> str:
    return view_html("channels_view.html")


mcp.add_resource(
    Resource.from_function(
        plan_view,
        PLAN_VIEW_URI,
        name="Admiry plan card",
        description="The plan as a card: goal, budget levels, the two-column 30-day estimate and its basis.",
        mime_type=UI_MIME_TYPE,
        meta=_PLAN,
    )
)
mcp.add_resource(
    Resource.from_function(
        channels_view,
        CHANNELS_VIEW_URI,
        name="Admiry channels card",
        description="Where the ads run: one row per channel with its state, today's spend and this week's calls.",
        mime_type=UI_MIME_TYPE,
        meta=_BORDER,
    )
)
