"""agent/smoke.sh driven as a real script (real curl) against stub hosts shaped like the edge since 2026-09-29
(spec 2026-09-29 §3, §4): the API host (AGENT_PUBLIC_BASE_URL) keeps /health, the connector docs, the Stripe hook
and /mcp, and 302s the browser pages to the web host (AGENT_WEB_BASE_URL), which renders them. Both hosts render
the connector docs with the API host's /mcp, the way the agent does."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent import mcp_app
from agent.surface import gate

SMOKE = Path(__file__).resolve().parents[1] / "smoke.sh"
BROWSER_PAGES = ("/connect", "/legal/", "/plan/", "/pay/")  # what the edge 302s among the paths smoke.sh reads
KEY = "ak_smoke_test"  # the bearer the stub answers /mcp for, as the server answers a live key

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="smoke.sh drives curl")
KEYED = pytest.mark.skipif(shutil.which("python3") is None, reason="smoke.sh reads /mcp's answers with python3")


def _handler(site: dict[str, str], role: str) -> type[BaseHTTPRequestHandler]:
    """role: "api" (302s browser pages when site["redirect"]), "web" or "one" (a single host that is both)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _send(self, code: int, body: str = "", location: str | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path, api = self.path, site["api"]
            if path == "/health":
                return self._send(200, '{"ok":true,"sha":"test"}')
            if path == "/connect/docs":
                return self._send(200, f"<p>Add {api}/mcp to your assistant.</p>")
            if path == "/connect/llms.txt":
                return self._send(200, f"# Admiry\nMCP: {api}/mcp\n")
            if path == "/connect/skill.md":
                return self._send(200, "---\nname: admiry-marketing\n---\n")
            if role == "api" and site["redirect"] and path.startswith(BROWSER_PAGES):
                return self._send(302, location=site["web"] + path)
            if path.startswith("/pay/"):  # old pay links open the conversation (relative, like agent/pay.py)
                return self._send(303, location="/?setup=" + path.removeprefix("/pay/"))
            pages = {
                "/connect": "<h1>Sign in</h1>",
                "/legal/management-agreement": 'Google Ads Management Agreement <meta name="robots" content="noindex">',
                "/legal/prepayment-addendum": 'Prepayment Addendum <meta name="robots" content="noindex">',
            }
            return self._send(200, pages[path]) if path in pages else self._send(404, "not found")

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/mcp" and site.get("key") and self.headers.get("Authorization") == f"Bearer {site['key']}":
                return self._send(200, json.dumps(_mcp_answer(json.loads(body), site.get("gate", ""))))
            self._send({"/hooks/stripe": 400, "/mcp": 401}.get(self.path, 404))

    return Handler


def _mcp_answer(request: dict, mode: str) -> dict:
    """/mcp's JSON-RPC answer to what smoke.sh sends with AGENT_KEY, in the server's shape and its own names: the public
    tools, get_marketing_state, and a retired name refused in the gate's words. `mode` gets one of them wrong: "open"
    lists every command too (an open gate), "forgets" answers a retired name as an unknown tool."""
    params = request["params"]
    if request["method"] == "tools/list":
        names = gate.PUBLIC | ({meta.name for meta in mcp_app.REGISTRY} if mode == "open" else set())
        result: dict = {"tools": [{"name": name} for name in sorted(names)]}
    elif params["name"] == "get_marketing_state":
        made = {"data": {"surface_version": gate.SURFACE_VERSION, "caller": {"class": "business"}}}
        result = {"content": [{"type": "text", "text": json.dumps(made)}], "structuredContent": made, "isError": False}
    else:
        said = f"Unknown tool: {params['name']}" if mode == "forgets" else gate.retired_sentence(params["name"])
        result = {"content": [{"type": "text", "text": said}], "isError": True}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


@pytest.fixture
def host() -> Iterator[Callable[[str, dict[str, str]], str]]:
    servers: list[ThreadingHTTPServer] = []

    def start(role: str, site: dict[str, str]) -> str:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(site, role))
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def split(host: Callable[[str, dict[str, str]], str]) -> Callable[..., tuple[str, str]]:
    def make(*, redirect: bool = True) -> tuple[str, str]:
        site = {"redirect": "1" if redirect else ""}
        site["api"] = host("api", site)
        site["web"] = host("web", site)
        return site["api"], site["web"]

    return make


def _smoke(*args: str, key: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k.lower() not in {"agent_key", "http_proxy", "all_proxy"}}
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1"
    if key:
        env["AGENT_KEY"] = key
    return subprocess.run(["bash", str(SMOKE), *args], capture_output=True, text=True, env=env, timeout=60, check=False)


def _failed_at(run: subprocess.CompletedProcess[str]) -> str:
    """The last check the script announced before it stopped."""
    assert run.returncode != 0, run.stdout
    assert "smoke FAILED" in run.stderr, run.stderr
    return [line for line in run.stdout.splitlines() if line.startswith("== ")][-1]


def test_one_host_passes(host: Callable[[str, dict[str, str]], str]) -> None:
    site = {"redirect": ""}
    site["api"] = site["web"] = host("one", site)
    run = _smoke(site["api"])
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.rstrip().endswith("smoke passed")


def test_split_hosts_pass_with_the_web_base(split: Callable[..., tuple[str, str]]) -> None:
    api, web = split()
    run = _smoke(api, web)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"== {api}/connect -> {web}/connect" in run.stdout
    assert run.stdout.rstrip().endswith("smoke passed")


def test_split_hosts_fail_on_the_api_base_alone(split: Callable[..., tuple[str, str]]) -> None:
    api, _ = split()
    assert _failed_at(_smoke(api)) == "== connect"  # the 302, not the page


def test_split_hosts_fail_on_the_web_base_alone(split: Callable[..., tuple[str, str]]) -> None:
    _, web = split()
    assert _failed_at(_smoke(web)) == "== docs"  # the docs name the API host's /mcp


def test_web_base_needs_the_api_host_to_redirect(split: Callable[..., tuple[str, str]]) -> None:
    api, web = split(redirect=False)  # an edge that still serves the pages on the API host
    assert _failed_at(_smoke(api, web)).startswith(f"== {api}/connect -> ")


@KEYED
def test_with_a_key_the_public_tools_the_state_and_a_retired_name_pass(
    host: Callable[[str, dict[str, str]], str],
) -> None:
    site = {"redirect": "", "key": KEY}
    site["api"] = site["web"] = host("one", site)
    run = _smoke(site["api"], key=KEY)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"ok {len(gate.PUBLIC)} tools" in run.stdout and f"ok business {gate.SURFACE_VERSION}" in run.stdout
    assert run.stdout.rstrip().endswith("== a retired name is answered with its replacement\nok\nsmoke passed")


@KEYED
def test_with_a_key_a_listing_that_holds_the_commands_fails(host: Callable[[str, dict[str, str]], str]) -> None:
    site = {"redirect": "", "key": KEY, "gate": "open"}  # every command listed, as an open gate lists them
    site["api"] = site["web"] = host("one", site)
    run = _smoke(site["api"], key=KEY)
    assert _failed_at(run) == "== tools/list"
    assert "AssertionError" in run.stderr and "'whoami'" in run.stderr  # the listing's own check, not an HTTP error


@KEYED
def test_with_a_key_a_retired_name_not_answered_with_its_replacement_fails(
    host: Callable[[str, dict[str, str]], str],
) -> None:
    site = {"redirect": "", "key": KEY, "gate": "forgets"}  # the listing and the state right; whoami an unknown tool
    site["api"] = site["web"] = host("one", site)
    run = _smoke(site["api"], key=KEY)
    assert _failed_at(run) == "== a retired name is answered with its replacement"
    assert "AssertionError" in run.stderr and "Unknown tool: whoami" in run.stderr
