"""The public MCP surface (spec 2026-10-01-admiry-mcp-gateway-design §3, §4): what an assistant sees and calls.

Nothing is imported here at module level: `mcp_app` imports `gate` before it creates the server, and `gate` must load
without `mcp_app`. The surface's tool modules are loaded through `load()` -- which `mcp_app` calls once every command
has registered, and `create_app()` calls again -- or by importing `agent.mcp_app` first. Importing
`agent.surface.registry` or a tool module directly as the first import of a process is not supported: the registry
and `mcp_app` import each other."""


def load() -> None:
    """Register the public surface: the tool modules below and what assistants are told (`told`: the prompts and the
    resources), imported once (a module registers on its first import, so a second call does nothing). Called at the
    end of `mcp_app`'s import when every command module has loaded, and by `create_app()` always, so the serving
    process has the public tools whichever module it imported first. Later tasks add their modules here and nowhere
    else."""
    from . import context, outcomes, plan, run, supporting, told  # noqa: F401 - imported to register the surface
