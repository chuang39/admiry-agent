"""Public connector documentation (spec 2026-09-24 §4.0 "Public docs", §5.2).

What an assistant needs to build its own bridge to /mcp: the endpoint, the auth header,
the tool list with approval expectations, the paste-into-Muse prompt and the error
semantics. Rendered from the public surface's own registry, so it cannot drift from the code.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import assistant_skill, config

# a leaf (it imports only config), so safe at module level, unlike the surface's registry (tool_docs imports it late)
from .surface import gate
from .templating import env as _env

router = APIRouter()

APPROVAL_TEXT = {
    "none": "runs as soon as it is called",
    "assistant": "ask the owner first, then call",  # resume_campaign spends again: no "(no spend)" here
    "code": (
        "asks first; runs once the owner approves on their plan page: there, or with the approval code they read to "
        "you (spend)"
    ),
}

ERRORS = [
    (
        "401 with WWW-Authenticate",
        "no bearer: connect through your assistant's connector settings (OAuth), or paste a key from /connect",
    ),
    ("this key lacks the 'X' scope", "the owner connected with fewer permissions; they can connect again and allow it"),
    (
        "rate limit: … retry_after=N",
        (
            "wait N seconds (60 calls per minute and 2,000 per day per key; 30 changes per hour per business, "
            "which never stops pause_campaign)"
        ),
    ),
    (
        "today's limit of N drafts for this business is reached",
        (
            "the tools that have Admiry write or read at length are capped per business per day "
            "(lower before the owner signs in): try tomorrow"
        ),
    ),
    (
        "today's limit of N assistant-confirmed changes is reached; try again tomorrow",
        "a page was published or taken down on the owner's word 20 times today; tell the owner, it works again tomorrow",
    ),
    (
        "a setup key cannot approve or confirm spending",
        "the owner has not signed in yet; connect_account returns their sign-in link",
    ),
    (
        "unknown key / this key was revoked / this key expired",
        "the key is wrong, revoked or past its life; the owner connects again",
    ),
    (
        "this business has expired; begin again with a new plan",
        (
            "the 24-hour draft this key belonged to lapsed, or was folded into the business the owner signed in for, "
            "and the key with it: the owner connects you again through their sign-in"
        ),
    ),
    (
        "idempotency_key already used for a different request; use a new key",
        (
            "each distinct call needs its own key: only the same call sent again within a day is answered with its "
            "first answer, and not once things changed since (a pause that was lifted, a plan version replaced); an "
            "empty key and one over 200 characters are refused in their own sentences"
        ),
    ),
    (
        "{status: 'proposed', approval_url}",
        (
            "a change that needs the owner. A launch: the owner approves on approval_url, or reads you its code and "
            "you call the same tool again with it. A page: call the same tool again with owner_confirmed: true once "
            "they agree (to publish, with the same token or version)"
        ),
    ),
    ("{status: 'needs_verification', verify_url}", "the business is not verified yet; send the owner to verify_url"),
    (
        "nothing is waiting for an approval code on this plan",
        "no launch was asked for on this plan, or the request expired: ask to launch first",
    ),
    (
        "this plan is already approved: nothing is left to confirm, and the code was not used",
        "the owner approved on their page; get_campaigns says where the ads stand",
    ),
    (
        "that approval code is not valid for this proposal (wrong, already used or expired)",
        "ask the owner to read the code again from their approval page; never try codes",
    ),
    (
        "too many wrong codes for this proposal; try again in an hour",
        "five wrong codes within an hour lock that approval for up to an hour",
    ),
    (
        "{status: 'needs_agreement', connect_url}",
        "connect_account: the owner accepts the agreement on connect_url first; then call again with a new idempotency_key",
    ),
    (
        "{status: 'web_step', steps, connect_url}",
        (
            "connect_account: nothing is connected by a call for this place; relay the steps and, when the answer has "
            "one, next_step (what the place waits for), with the link"
        ),
    ),
    (
        (
            "this business runs its ads in the owner's own Google Ads account and pays Google directly, so it has no "
            "Admiry balance"
        ),
        "get_billing is for a business whose ads Admiry runs from a prepaid balance; this one pays Google directly",
    ),
    (
        "{pay_url: null, note}",
        (
            "get_billing with view topup_link: the owner cannot pay yet; the note says what comes first "
            "(sign in, accept the terms on connect_url, or payments are not set up on this server)"
        ),
    ),
    (
        "`<name>` was replaced by `<tool>`",
        "Admiry's tools changed since you saved your skill: read these docs again and use the tool it names",
    ),
    (
        "`<name>` is no longer a tool: <why>",
        "no tool replaces it: the sentence says where that is done now; read these docs again",
    ),
]


def paste_prompt(base: str) -> str:
    return (
        f"Add Admiry as a connector: MCP server {base}/mcp. It will ask you to sign in with Google. "
        f"Read {base}/connect/docs and {base}/connect/skill.md first, then follow skill.md when you talk to me about ads.\n"
        f"Do not ask me for a key: connect with no credential and follow the 401's OAuth discovery; "
        f"only if you cannot open a browser for me, I will paste a key from {base}/connect as the Bearer token."
    )


def prompts(base: str, token: str | None) -> dict:
    """The assistant sheet's three paste prompts (spec §3 D41): Muse gets the generic connector prompt;
    Claude also gets the connector-settings steps and the `claude mcp add` line; ChatGPT (and other MCP
    clients) get the developer-mode line. A hand-off token, when given, rides in each ("continue session
    <token>", valid 30 minutes) so the assistant picks up the consultation started on the website instead of
    asking the owner to repeat themselves. The Muse text has no key line (unlike paste_prompt, which /connect
    keeps): the sheet promises the owner never copies a key, and Muse signs in through OAuth."""
    suffix = f" Continue session {token}." if token else ""
    muse = (
        f"Build a custom integration to Admiry, my advertising agent. MCP server {base}/mcp (streamable HTTP). "
        f"Read {base}/connect/docs and {base}/connect/skill.md first, then sign me in"
        + (f" and continue session {token}." if token else ".")
    )
    claude = (
        f"Add Admiry as a connector: Settings → Connectors → Add custom connector → {base}/mcp. "
        f"Claude Code: claude mcp add --transport http admiry {base}/mcp. "
        "It will ask you to sign in with Google." + suffix
    )
    chatgpt = (
        f"Add Admiry as a connector: MCP server {base}/mcp (developer mode, or any MCP client that "
        "supports OAuth 2.1). It will ask you to sign in with Google." + suffix
    )
    return {"muse": muse, "claude": claude, "chatgpt": chatgpt}


# The order an assistant calls them in; the rest of tool_docs() stays alphabetical so the list is easy to scan.
FIRST = (
    "get_marketing_state",
    "create_marketing_plan",
    "update_marketing_plan",
    "get_marketing_plan",
    "launch_marketing_plan",
)


def tool_docs() -> list[dict]:
    """The public tools (spec 2026-10-01 §4), from the surface's own registry so the docs cannot drift from it."""
    # imported here, not at the top: mcp_app's own last lines (surface.load(), which imports surface.told) import this
    # module, so a module-level import of the surface made `import agent.connect` (or agent.web, which imports it)
    # fail half-way (final wave 9).
    # mcp_app first: the surface's registry imports it, and mcp_app's last lines load the surface back, so a
    # process that asks for the registry before mcp_app meets it half-initialised (Task 1's review). Then load():
    # a process whose first import was a command module got mcp_app without the surface (Task 9).
    from . import mcp_app, surface  # noqa: F401

    surface.load()
    from .surface.registry import SURFACE

    docs = []
    ordered = sorted(SURFACE, key=lambda m: (FIRST.index(m.name) if m.name in FIRST else len(FIRST), m.name))
    for m in ordered:
        description = " ".join((m.tool.description or "").split())  # docstrings wrap; docs want one line
        summary = description.split(". ")[0].rstrip(".") + "."
        docs.append(
            {
                "name": m.name,
                "class": m.cls,
                "scope": m.scope,
                "approval": m.approval,
                "approval_text": APPROVAL_TEXT[m.approval],
                "summary": summary,
                "description": description,
                "input_schema": m.tool.parameters,
            }
        )
    return docs


def _context() -> dict:
    base = config.load().public_base_url
    return {
        "base": base,
        "mcp_url": f"{base}/mcp",
        "prompt": paste_prompt(base),
        "tools": tool_docs(),
        "errors": ERRORS,
        "skill_url": f"{base}/connect/skill.md",
        "version": gate.SURFACE_VERSION,
        "skill_description": assistant_skill.frontmatter()["description"],
    }


@router.get("/connect/docs", response_class=HTMLResponse)
def docs() -> str:
    return _env.get_template("connect_docs.html").render(**_context())


@router.get("/connect/skill.md")
def skill_md() -> PlainTextResponse:
    """The skill's own `SKILL.md` (spec 2026-10-01 §9.3): what an assistant that reads before it calls saves."""
    return PlainTextResponse(assistant_skill.files()["SKILL.md"], media_type="text/markdown")


@router.get("/connect/references/{name}")
def skill_reference(name: str) -> PlainTextResponse:
    """One of the skill's other files, where `SKILL.md`'s relative `references/<name>` leads from /connect/skill.md.
    `name` is looked up among the skill's files, never on disk."""
    found = assistant_skill.text(f"references/{name}")
    if found is None:
        raise HTTPException(status_code=404)
    return PlainTextResponse(found, media_type="text/markdown")


@router.get("/connect/llms.txt", response_class=PlainTextResponse)
def llms_txt() -> str:
    return _env.get_template("llms.txt.j2").render(**_context())


@router.get("/connect/tools.json")
def tools_json() -> JSONResponse:
    ctx = _context()
    return JSONResponse(
        {
            "mcp_url": ctx["mcp_url"],
            "version": ctx["version"],
            "auth": "OAuth 2.1 (401 + WWW-Authenticate resource_metadata, DCR, PKCE S256; do not ask the owner for a key); "
            "fallback only: Authorization: Bearer ak_... (or X-API-Key)",
            "transport": "streamable-http",
            "tools": ctx["tools"],
        }
    )
