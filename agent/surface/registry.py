"""The public tools and the commands behind them (spec 2026-10-01 §3, §4.2).

A public tool is a thin function registered on the same FastMCP server as the commands: it checks its arguments,
picks exactly one command and returns that command's envelope (`dispatch`), or the first answer of the same call sent
again (`replayed`, for a tool that picks its command from where the business stands; `earlier_confirmation`, for the
owner's yes). `passthrough` publishes a command under its own name when the two would be the same function. `SURFACE`
is what /connect/docs, tools.json and the contract test read; `mcp_app.REGISTRY` stays the command registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError

from ..mcp_app import REGISTRY, mcp, replay_public
from . import actions, gate
from .where import owner_choice


@dataclass(frozen=True)
class SurfaceMeta:
    name: str
    cls: str  # RO | W | P
    scope: str  # read | manage | leads
    approval: str  # none | assistant | code
    commands: tuple[str, ...]  # the commands this tool may run (exactly one per call)
    tool: Any  # the FunctionTool: .description, .parameters, .annotations, .fn


SURFACE: list[SurfaceMeta] = []
# The scopes a public tool may ask a key for (the public contract lists them): a key's `account` is no public tool's
SCOPES = ("read", "manage", "leads")


def command(name: str):
    """The command's FunctionTool (`.fn` runs it in process, under the current request's headers)."""
    for meta in REGISTRY:
        if meta.name == name:
            return meta.tool
    raise ToolError(f"no command named {name}")


def dispatch(name: str, **args) -> dict:
    """Run one command in process and return its envelope, its next actions in public names -- the two ways a plan
    offers as the owner's choice unless the caller's business has already chosen, and the page question in their place
    while the business has no page settled (`owner_choice`, one read of both). `run()` inside it authenticates the
    caller, replays an idempotent repeat and writes the one audit row, under the command's name and the public
    tool's."""
    return actions.translate(command(name).fn(**args), owner=owner_choice)


def replayed(
    idempotency_key: str | None,
    *,
    scope: str = "manage",
    same_call_only: bool = False,
    still_stands: Callable[[dict], bool] | None = None,
) -> dict | None:
    """The first answer of this same public call, its next actions in public names, when the assistant sends the
    call again with the same idempotency key and that answer still stands (`still_stands(answer)`, when given); None
    for a first call. A tool that picks its command from where the business stands returns this before it routes. A
    key that gives no answer -- used for another request, or for this one, whose answer no longer stands -- is
    refused as a reused key (ToolError, recorded): the tool routes nothing, so no command's own replay can give the
    answer that was declined. The owner's yes asks only when nothing waits for it, and for it such a key is no refusal
    either: None (`same_call_only`, see `earlier_confirmation`)."""
    earlier = replay_public(idempotency_key, scope, same_call_only=same_call_only, still_stands=still_stands)
    return actions.translate(earlier, owner=owner_choice) if earlier is not None else None


def data_of(answer: Any) -> dict:
    """The data of a stored answer, for a tool that asks whether the answer still stands; {} when it holds none (a
    row keeps whatever its command answered)."""
    made = answer.get("data") if isinstance(answer, dict) else None
    return made if isinstance(made, dict) else {}


def earlier_confirmation(
    waiting: dict | None,
    *,
    stands: bool,
    known: bool,
    nothing: str,
    idempotency_key: str,
    confirmed: Callable[[dict], bool] | None = None,
) -> dict | None:
    """For the owner's yes (a launch with its code, a page to publish or take down), before the tool runs
    confirm_proposal. While a request waits, None: the tool confirms it, and a key already used for another request
    meets that command's own refusal. With nothing waiting, the call is answered from an earlier one only if this same
    call went through (its identity leaves the code out; a row of another call with the key is no answer and no
    refusal) and only while what its answer says still stands: `stands` (the plan asked about is approved or already
    in Admiry's review -- L1 -- the hosted page is up, or down) and, when given, `confirmed(answer)` (that answer
    approved this very plan, or published the version that is live). Then its first answer. Otherwise a caller the
    tool knows (`known`) is told `nothing` (ToolError), as before Task 18, and for one it cannot resolve this is
    None: the command refuses it in its own words."""
    if waiting is not None:
        return None
    earlier = replayed(idempotency_key, scope="manage", same_call_only=True, still_stands=confirmed) if stands else None
    if earlier is None and known:
        raise ToolError(nothing)
    return earlier


def public_tool(
    *,
    cls: str,
    scope: str,
    commands: tuple[str, ...],
    approval: str = "none",
    destructive: bool = False,
    open_world: bool = False,
    idempotent: bool = True,
    **tool_kwargs,
):
    """Register a public tool. `commands` names every command it may dispatch to; a name that is not a command is a
    programming error caught at import. `idempotent` sets the hint an assistant reads: False for a tool whose repeat
    does it again (a repeated create_marketing_plan starts over). `destructive` is the hint a host reads to decide
    whether to ask the owner first: True for a tool that can remove or replace what is live for the owner at once --
    update_landing_page can take the live page down (pausing the ads that send people to it) and move running ads to
    another address -- and False for one whose every change keeps the version before it (a new start, a new reading
    of the business)."""
    assert cls in ("RO", "W", "P"), cls
    assert scope in SCOPES, scope
    known = {m.name for m in REGISTRY}
    unknown = [c for c in commands if c not in known]
    if unknown:
        raise ValueError(f"public tool names commands that do not exist: {unknown}")
    annotations = {
        "readOnlyHint": cls == "RO",
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }

    def decorate(fn: Callable):
        tool = mcp.tool(fn, annotations=annotations, **tool_kwargs)
        SURFACE.append(
            SurfaceMeta(name=tool.name, cls=cls, scope=scope, approval=approval, commands=tuple(commands), tool=tool)
        )
        gate.PUBLIC.add(tool.name)
        return tool

    return decorate


def passthrough(name: str) -> None:
    """Publish a command as it is: same name, same arguments, same result."""
    meta = next((m for m in REGISTRY if m.name == name), None)
    if meta is None:
        raise ValueError(f"no command named {name}")
    assert meta.scope in SCOPES, meta.scope  # a public tool asks for a scope a public tool may ask for
    cls = "W" if meta.cls == "A" else meta.cls
    SURFACE.append(
        SurfaceMeta(name=name, cls=cls, scope=meta.scope, approval=meta.approval, commands=(name,), tool=meta.tool)
    )
    gate.PUBLIC.add(name)
