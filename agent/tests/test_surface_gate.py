"""agent/surface/gate.py and registry.py (spec 2026-10-01 §3, §9.7; plan G1 Task 1): one middleware decides what an
assistant sees and names the public tool of the current call; a public tool calls one command in process and the
audit row keeps both names."""

import ast
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ResourceError, ToolError

from agent import mcp_app, surface
from agent.surface import gate, registry
from agent.tests.conftest import call_tool, data, rpc, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import planned

REPO = Path(__file__).resolve().parents[2]
SEVEN = {
    "get_marketing_state",
    "get_business_profile",
    "get_connected_accounts",
    "get_campaign_performance",
    "get_leads",
    "update_lead",
    "add_lead",
}
# A process that imports a command module first (a job, an ops command): what the surface holds before load(), after
# it, and after a second load(), as JSON on the last line of stdout.
COMMAND_MODULE_FIRST = """
import json
import agent.tools_pages
from agent import surface
from agent.surface import gate, registry
seen = [sorted(gate.PUBLIC)]
surface.load()
seen.append([sorted(gate.PUBLIC), len(registry.SURFACE)])
surface.load()
seen.append([sorted(gate.PUBLIC), len(registry.SURFACE)])
print(json.dumps(seen))
"""
MCP_APP_FIRST = """
import json
import agent.mcp_app
from agent.surface import gate
print(json.dumps(sorted(gate.PUBLIC)))
"""
# The service in a process whose first import was a command module: the public tools before create_app() and after.
CREATE_APP_AFTER_A_COMMAND_MODULE = """
import json
import agent.tools_pages
from agent.app import create_app
from agent.surface import gate
before = sorted(gate.PUBLIC)
create_app()
print(json.dumps([before, sorted(gate.PUBLIC)]))
"""


def _fresh(code: str, **env: str) -> list:
    """Run `code` in a fresh interpreter from the repo root, in the environment this process has (the test database
    included; `env` overrides a variable), and read its JSON."""
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **env},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def _registers_commands(path: Path) -> bool:
    """Whether a module decorates a function with `agent_tool` (called with its arguments, or bare)."""
    for node in ast.walk(ast.parse(path.read_text())):
        for decorator in getattr(node, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if getattr(target, "id", None) == "agent_tool" or getattr(target, "attr", None) == "agent_tool":
                return True
    return False


@pytest.fixture
def echo(monkeypatch):
    """A public tool over `whoami`, registered for one test and removed after it."""
    monkeypatch.setattr(registry, "SURFACE", list(registry.SURFACE))
    monkeypatch.setattr(gate, "PUBLIC", set(gate.PUBLIC))

    @registry.public_tool(cls="RO", scope="read", commands=("whoami",))
    def g1_echo() -> dict:
        """The caller, through the command behind it."""
        return registry.dispatch("whoami")

    yield g1_echo
    mcp_app.mcp.local_provider.remove_tool("g1_echo")


@pytest.fixture
def strict(monkeypatch):
    """The gate as production runs it: commands are not part of the surface."""
    monkeypatch.setattr(gate, "EXPOSE_COMMANDS", False)


def test_a_public_tool_runs_one_command_and_the_row_keeps_both_names(client, conn, echo):
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(client, "g1_echo", {}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res)["business"]["id"] == "biz_1"
    rows = conn.execute("SELECT tool, surface_tool FROM agent_action ORDER BY id").fetchall()
    assert [(r["tool"], r["surface_tool"]) for r in rows] == [("whoami", "g1_echo")]


def test_a_command_called_directly_leaves_no_public_name(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    call_tool(client, "whoami", {}, key=key)
    assert conn.execute("SELECT surface_tool FROM agent_action").fetchone()["surface_tool"] is None


def test_the_strict_gate_lists_only_public_tools_and_refuses_the_rest(client, conn, echo, strict):
    key = business_with_key(conn, claim_state="claimed")
    names = [t["name"] for t in rpc(client, "tools/list", key=key)["result"]["tools"]]
    assert "g1_echo" in names and "whoami" not in names and set(names) == gate.PUBLIC
    hidden = call_tool(client, "marketing_state", {}, key=key)
    assert hidden["isError"] and "Unknown tool: marketing_state" in tool_text(hidden)
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0  # refused before any command
    assert not call_tool(client, "g1_echo", {}, key=key)["isError"]


def test_every_command_outside_the_public_tools_is_refused_by_name_and_runs_nothing(client, conn, strict):
    """Final review A m9 (a probe kept): not only the names assistants knew -- every command registered on the server
    and not published is refused under the gate as production runs it, by its name, before it runs: no audit row."""
    key = business_with_key(conn, scopes=("read", "manage", "leads", "account"), claim_state="claimed")
    hidden = sorted({meta.name for meta in mcp_app.REGISTRY} - gate.PUBLIC)
    assert {"marketing_state", "change_plan", "pause", "confirm_proposal", "revoke_key"} <= set(hidden)
    for name in hidden:
        text = tool_text(res := call_tool(client, name, {}, key=key))
        assert res["isError"] and (f"`{name}`" in text or f"Unknown tool: {name}" in text), (name, text)
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_a_wrongly_typed_argument_leaves_only_the_tools_name_in_the_log(client, conn, walk_fake, strict, caplog):
    """Final review A m6: FastMCP refuses a wrongly typed argument before the tool runs and logs the refusal with the
    arguments it was sent -- here a lead's name and number, and an approval code. The line keeps the tool's name and
    nothing it was sent; the assistant's own error is FastMCP's, as before."""
    walk = planned(client, conn)
    served = logging.getLogger("fastmcp")  # FastMCP's loggers write to their own handlers, not the root's
    served.addHandler(caplog.handler)
    try:
        person = {"kind": "call", "status": "won", "contact": "Jane Roe 555-0199", "idempotency_key": "l1"}
        lead = call_tool(client, "add_lead", person, key=walk["key"])
        code = call_tool(
            client, "launch_marketing_plan", {"idempotency_key": "c1", "approval_code": [48311]}, key=walk["key"]
        )
    finally:
        served.removeHandler(caplog.handler)
    assert lead["isError"] and "contact" in tool_text(lead), tool_text(lead)
    assert code["isError"] and "approval_code" in tool_text(code), tool_text(code)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for sent in ("Jane Roe", "555-0199", "48311"):
        assert sent not in logged, (sent, logged)
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Invalid arguments for tool")]
    assert said == ["Invalid arguments for tool add_lead", "Invalid arguments for tool launch_marketing_plan"], said
    assert {r.levelname for r in caplog.records if r.getMessage() in said} == {"WARNING"}
    ran = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool IN ('add_lead', 'confirm_proposal')")
    assert ran.fetchone()["n"] == 0  # neither tool ran


def test_a_failed_resource_read_logs_the_first_200_characters_of_its_address(client, conn, strict, caplog):
    """Re-review A, N1: FastMCP logs a failed resource read with the address as it was sent, and an address is as long
    as the request body allows. The line keeps the address's first 200 characters (gate.MAX_RESOURCE_NAME), its level
    and its exception; what the assistant is answered is unchanged."""
    key = business_with_key(conn, claim_state="claimed")
    uri = "admiry://marketing/performance/" + "x" * 20_000
    served = logging.getLogger("fastmcp")  # FastMCP's loggers write to their own handlers, not the root's
    served.addHandler(caplog.handler)
    try:
        body = rpc(client, "resources/read", {"uri": uri}, key=key)
    finally:
        served.removeHandler(caplog.handler)
    assert "result" not in body and "period must be one of" in body["error"]["message"]
    lines = caplog.text.splitlines()
    assert lines and max(len(line) for line in lines) <= 400, max(len(line) for line in lines)
    read = [r for r in caplog.records if r.getMessage().startswith("Error reading resource")]
    assert [r.getMessage() for r in read] == [f"Error reading resource {uri[: gate.MAX_RESOURCE_NAME]!r}"]
    assert read[0].levelname == "ERROR" and read[0].exc_info[0] is ResourceError


def test_a_retired_name_is_answered_with_its_replacement(client, conn, echo, strict, monkeypatch):
    monkeypatch.setitem(gate.RETIRED, "whoami", "g1_echo")
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(client, "whoami", {}, key=key)
    text = tool_text(res)
    assert res["isError"] and "`whoami` was replaced by `g1_echo`" in text
    assert "/connect/docs" in text and "update the Admiry skill you saved" in text


@pytest.mark.parametrize("exposed", [True, False], ids=["gate open", "gate closed"])
def test_an_old_prompt_or_resource_is_answered_with_what_replaced_it(client, conn, monkeypatch, exposed):
    """Final review C, minor 5: on main an assistant knew the prompt admiry-plan-a-campaign and the resources
    admiry://skill and admiry://page-kit, and got FastMCP's bare "Unknown" for them. Each is answered with what
    replaced it, closed by the sentence a retired tool's answer closes with -- the gate open (the suite) or closed:
    the old names exist in neither. Nothing runs."""
    monkeypatch.setattr(gate, "EXPOSE_COMMANDS", exposed)
    key = business_with_key(conn, claim_state="claimed")
    again = gate.again()
    assert f"(version {gate.SURFACE_VERSION})" in again and "/connect/docs" in again
    assert gate.retired_sentence("whoami").endswith(again)  # one closing sentence, written once
    prompt = rpc(client, "prompts/get", {"name": "admiry-plan-a-campaign", "arguments": {}}, key=key)
    assert "result" not in prompt
    assert prompt["error"]["message"] == (
        f"`admiry-plan-a-campaign` was replaced by the prompt `admiry-get-more-customers`. {again}"
    )
    skill = rpc(client, "resources/read", {"uri": "admiry://skill"}, key=key)
    assert "result" not in skill
    assert skill["error"]["message"] == f"`admiry://skill` was replaced by `skill://admiry-marketing/SKILL.md`. {again}"
    kit = rpc(client, "resources/read", {"uri": "admiry://page-kit"}, key=key)
    assert "result" not in kit
    assert kit["error"]["message"] == (
        "`admiry://page-kit` is no longer a resource: the page guide for this business comes from `get_landing_page` "
        f"with view kit. {again}"
    )
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_the_old_names_lead_to_what_the_server_lists(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    prompts = {p["name"] for p in rpc(client, "prompts/list", key=key)["result"]["prompts"]}
    resources = {r["uri"] for r in rpc(client, "resources/list", key=key)["result"]["resources"]}
    assert set(gate.RETIRED_PROMPTS.values()) <= prompts and not set(gate.RETIRED_PROMPTS) & prompts
    assert {new for new in gate.RETIRED_RESOURCES.values() if new} <= resources
    assert not set(gate.RETIRED_RESOURCES) & resources


def test_an_unknown_prompt_or_resource_still_gets_fastmcps_own_error(client, conn, strict):
    key = business_with_key(conn, claim_state="claimed")
    prompt = rpc(client, "prompts/get", {"name": "admiry-nope", "arguments": {}}, key=key)
    assert "Unknown prompt: 'admiry-nope'" in prompt["error"]["message"]
    resource = rpc(client, "resources/read", {"uri": "admiry://nope"}, key=key)
    assert "Unknown resource: 'admiry://nope'" in resource["error"]["message"]


def test_a_get_of_an_old_prompt_or_resource_leaves_a_line_in_the_log(client, conn, strict, caplog):
    """As a call to a retired tool: no audit row (nothing ran), so the log is where an old name shows."""
    key = business_with_key(conn, claim_state="claimed")
    with caplog.at_level("INFO", logger="agent.surface.gate"):
        rpc(client, "prompts/get", {"name": "admiry-plan-a-campaign", "arguments": {}}, key=key)
        rpc(client, "resources/read", {"uri": "admiry://skill"}, key=key)
        rpc(client, "resources/read", {"uri": "admiry://page-kit"}, key=key)
    said = [r.getMessage() for r in caplog.records if r.name == "agent.surface.gate"]
    for old in ("admiry-plan-a-campaign", "admiry://skill", "admiry://page-kit"):
        assert any(old in line and "turned away" in line for line in said), (old, said)


def test_dispatch_refuses_a_name_that_is_not_a_command():
    with pytest.raises(ToolError, match="no command named nope"):
        registry.dispatch("nope")


def test_a_public_tool_declares_real_commands():
    with pytest.raises(ValueError, match="nope"):

        @registry.public_tool(cls="RO", scope="read", commands=("nope",))
        def g1_bad() -> dict:
            """Never registered."""
            return {}


def test_passthrough_publishes_a_command_under_its_own_name(monkeypatch):
    monkeypatch.setattr(registry, "SURFACE", list(registry.SURFACE))
    monkeypatch.setattr(gate, "PUBLIC", set(gate.PUBLIC))
    # a command no public tool publishes (load() already publishes contact_support), so what the asserts below see is
    # this call's own doing
    assert "whoami" not in gate.PUBLIC
    registry.passthrough("whoami")
    meta = next(m for m in registry.SURFACE if m.name == "whoami")
    assert meta.commands == ("whoami",) and "whoami" in gate.PUBLIC
    assert meta.tool is registry.command("whoami")


def test_a_process_that_imports_a_command_module_first_loads_the_surface_when_asked():
    """A command module imported first is still loading when mcp_app's last lines run, so they register no public
    tool (its commands do not exist yet) and the import does not fail; surface.load() then registers all of them, and
    a second load() changes nothing."""
    before, once, twice = _fresh(COMMAND_MODULE_FIRST)
    assert before == []
    assert SEVEN <= set(once[0])
    assert twice == once


def test_a_process_that_imports_mcp_app_first_has_the_public_tools():
    """The service and the test process: importing mcp_app first registers the public tools, no load() needed."""
    assert SEVEN <= set(_fresh(MCP_APP_FIRST))


def test_create_app_loads_the_surface(conn, monkeypatch):
    """The serving process has the public tools whichever module it imported first: create_app() asks for them by
    name. This pins the wiring; the client fixture builds the app the same way."""
    from agent.app import create_app

    calls = []
    monkeypatch.setattr(surface, "load", lambda: calls.append("load"))
    create_app()
    assert calls == ["load"]


def test_the_service_has_the_public_tools_when_a_command_module_was_imported_first(conn):
    """The case create_app()'s load() exists for, end to end in a fresh interpreter: the first import is a command
    module, so mcp_app registered no public tool, and the app create_app() builds has all of them. The child keeps this
    process's environment, the test database included; only its public base becomes localhost, because the MCP SDK
    refuses a plain-http issuer anywhere else and conftest's relaxation of that check runs in this process only."""
    before, after = _fresh(CREATE_APP_AFTER_A_COMMAND_MODULE, AGENT_PUBLIC_BASE_URL="http://localhost")
    assert before == []
    assert SEVEN <= set(after)


def test_every_command_module_is_named_as_the_guard_expects():
    """mcp_app's _COMMANDS_LOADING recognises a command module that is still loading by its name, agent.tools_*: every
    module under agent/ (tests excluded) that registers commands with @agent_tool is mcp_app.py or a tools_*.py directly
    under agent/."""
    root = REPO / "agent"
    found = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "tests" not in path.relative_to(root).parts and _registers_commands(path)
    }
    assert {"mcp_app.py", "tools_pages.py"} <= found  # the scan sees the modules it is about
    assert [name for name in sorted(found) if name != "mcp_app.py" and not re.fullmatch(r"tools_\w+\.py", name)] == []
