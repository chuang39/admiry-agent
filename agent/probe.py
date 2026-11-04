"""A scripted MCP client for the connector (spec 2026-09-26 §11 A1b-4d, `docs/ops/muse-connector-validation.md`
Part A): what a real assistant sees from the server, recorded as dated rows so the assistant rounds only have to
answer what the assistant itself did.

    python -m agent.probe https://agent.dev.adx.admiry.ai --key-file ~/.config/admiry-probe/key --walk
    python -m agent.probe https://agent.dev.adx.admiry.ai --oauth --walk            # signs in through the browser
    python -m agent.probe https://agent.dev.adx.admiry.ai --oauth --rotation-check  # spends the refresh token twice
    python -m agent.probe https://agent.dev.adx.admiry.ai --key-file F --handoff-file H --walk

Checks: the 401 and its header, the two metadata documents, initialize (extensions), tools/list (exactly the names
/connect/tools.json publishes, no command among them, and which carry the plan card), prompts and resources,
get_marketing_state, then with --walk the
dental consultation end to end on the public surface (create_marketing_plan → update_marketing_plan with each answer →
get_marketing_plan builds → update_marketing_plan changes the budget → get_marketing_plan reads) and with
--rotation-check the refresh-token family rule (the 4b exit criterion). Credentials — the key, a hand-off token —
come from 0600 files or the OAuth flow's own 0600 store, never the command line, and are never printed; the
paste-back sign-in is the same shape as `agent.ops google-ads-auth`. Uses the MCP SDK's client and its
OAuthClientProvider (discovery → DCR → PKCE), i.e. the code path a third-party assistant runs. A step that fails
becomes a failing row, never a lost table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

RPC_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
TOOLS_JSON = "/connect/tools.json"  # the public tools the docs publish: tools/list must show exactly these names
UI_EXTENSION = "io.modelcontextprotocol/ui"
PLAN_VIEW_URI = "ui://admiry/plan"
SKILL_URI = "skill://admiry-marketing/SKILL.md"
PROMPTS = [
    "admiry-get-more-customers",
    "admiry-how-are-my-ads-doing",
    "admiry-promote-a-service",
    "admiry-record-lead-outcomes",
]
WALK_TOOLS = ("create_marketing_plan", "update_marketing_plan", "get_marketing_plan", "launch_marketing_plan")
PLAN_CARD_TOOLS = {"update_marketing_plan", "get_marketing_plan", "update_marketing_budget"}
WALK_TEXT = "I run a dental practice in Palo Alto and want more Invisalign patients"
OTHER_ANSWER = "around three thousand"  # the one free-text answer of the walk (the customer-value question)
FREE_TEXT_QUESTION = "customer_value"
# A3p-5 batch 1: the consultation's first question now asks for the website; without one it asks the name and what
# the business offers. Real-sounding words, not OTHER_ANSWER's amount, so the name reaches the Google Maps lookup and
# the services reach the model's reader meaningfully.
NAME_ANSWER = "Bright Smile Dental"
SERVICES_ANSWER = "Invisalign, cleanings, teeth whitening"
ADJUST_USD = 1500
LIMIT_REFUSAL = "monthly limit"  # tools_consult.build's refusal above the owner's limit: a valid answer, not a bug
MAX_QUESTIONS = 10
MAX_CALLS = 30  # spec §11 A1b-4a exit: 30 tool calls per session at most
REDIRECT_URI = "http://localhost:8765/callback"  # DCR accepts https or localhost; nothing listens: paste-back
STORE_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "admiry-probe"
RESULTS_HEADING = "### Results"  # --out inserts under this heading when the file has one, else appends


class ProbeError(Exception):
    """A tool answered isError (its text is the message)."""


@dataclass
class Row:
    id: str
    check: str
    observed: str
    ok: bool | None  # None: recorded, not judged


# ---------------------------------------------------------------- pure helpers (tested)


def pick_answer(question: dict, *, allow_other: bool) -> dict:
    """The walk's answer to a question: NAME_ANSWER and SERVICES_ANSWER on the two questions a "No website" tap
    brings (A3p-5 batch 1) -- real-sounding words, always, not gated on `allow_other` (neither counts as the walk's
    one free-text demonstration); the free-text OTHER_ANSWER once, on the customer-value question (every tree asks
    one, as bands) or else on the first `amount` question, when it allows typing; otherwise the first option (a list
    of one for a multi question) -- the website question itself needs no special case: the probe's own site, when the
    reading already named one, is offered first (Q5's `site_known`), "No website" otherwise, and either way the
    first option is the right tap. A question with no options and no typing gets the text anyway: the server's
    refusal is then the row."""
    if question.get("id") == "business_name":
        return {"other": NAME_ANSWER}
    if question.get("id") == "services_offered":
        return {"other": SERVICES_ANSWER}
    typed = question.get("id") == FREE_TEXT_QUESTION or question.get("input") == "amount"
    if allow_other and typed and (question.get("other") or {}).get("allowed"):
        return {"other": OTHER_ANSWER}
    options = question.get("options") or []
    if not options:
        return {"other": OTHER_ANSWER}
    first = options[0]["id"]
    return {"choice": [first] if question.get("input") == "multi" else first}


def walk_ready(res: dict) -> bool:
    """Whether a create/update result says the plan can be built: `data.ready`, or (a hand-off that picked up a
    fully answered session) the build action as `next`."""
    if (res.get("data") or {}).get("ready"):
        return True
    return any(a.get("tool") == "get_marketing_plan" for a in (res.get("next") or {}).get("actions", []))


def against_the_docs(listed: list[str], published: list[str] | None) -> tuple[bool, str]:
    """Whether tools/list shows exactly the tools the docs publish (TOOLS_JSON; final review C, minor 14), and the
    tools row's words for it: the names only one side has, each side named. None for `published`: the docs could not
    be read, which fails the row too."""
    if published is None:
        return False, f"{TOOLS_JSON} could not be read"
    extra, missing = sorted(set(listed) - set(published)), sorted(set(published) - set(listed))
    if not extra and not missing:
        return True, f"the names {TOOLS_JSON} publishes"
    said = ([f"listed, not in {TOOLS_JSON}: {extra}"] if extra else []) + (
        [f"in {TOOLS_JSON}, not listed: {missing}"] if missing else []
    )
    return False, "; ".join(said)


def _published(response: httpx.Response) -> list[str] | None:
    """The tool names in a TOOLS_JSON answer; None for an answer that is not one."""
    if response.status_code != 200:
        return None
    try:
        return [tool["name"] for tool in response.json()["tools"]]
    except (ValueError, KeyError, TypeError):
        return None


def render(rows: list[Row], *, base: str, when: date, title: str = "Probe") -> str:
    """The rows as a Markdown table under a dated heading (what the runbook records)."""
    lines = [f"#### {title} — {when.isoformat()} — {base}", "", "| # | Check | Observed | OK |", "|---|---|---|---|"]
    for r in rows:
        mark = "" if r.ok is None else ("yes" if r.ok else "**no**")
        lines.append(f"| {r.id} | {r.check} | {_cell(r.observed)} | {mark} |")
    return "\n".join(lines) + "\n"


def _cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def record(path: Path, text: str) -> None:
    """Put the table into the file: under RESULTS_HEADING (after any tables already there, before the next
    `## ` heading) when the file has one, at the end otherwise."""
    try:
        body = path.read_text()
    except FileNotFoundError:
        body = ""
    at = body.find(RESULTS_HEADING + "\n")
    if at < 0:
        path.write_text(body + ("\n" if body and not body.endswith("\n") else "") + "\n" + text)
        return
    after = at + len(RESULTS_HEADING) + 1
    nxt = body.find("\n## ", after)
    end = len(body) if nxt < 0 else nxt + 1
    path.write_text(body[:end].rstrip("\n") + "\n\n" + text + ("\n" if nxt >= 0 else "") + body[end:])


def parse_callback(raw: str) -> tuple[str, str | None]:
    """The code and state from the address the browser landed on after consent."""
    qs = parse_qs(urlparse(raw.strip()).query)
    if not qs.get("code"):
        raise ValueError("that address carries no code= parameter")
    return qs["code"][0], (qs.get("state") or [None])[0]


def read_secret(path: str, *, prefix: str, what: str) -> str:
    """One credential from a private file: refuses a file others can read, and a value of the wrong kind."""
    p = Path(path)
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"{p} is readable by others; chmod 600 it")
    value = p.read_text().strip()
    if not value.startswith(prefix):
        raise ValueError(f"{p} must hold one {what} (it starts with {prefix})")
    return value


# ---------------------------------------------------------------- OAuth pieces


class FileStorage(TokenStorage):
    """The SDK's token store on disk, 0600: client registration + tokens per server host."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)

    async def get_tokens(self) -> OAuthToken | None:
        t = self.load().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        d = self.load()
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self.save(d)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = self.load().get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        d = self.load()
        d["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self.save(d)


def store_for(base: str) -> FileStorage:
    return FileStorage(STORE_DIR / f"{urlparse(base).netloc.replace(':', '_')}.json")


def oauth_provider(base: str, storage: FileStorage) -> OAuthClientProvider:
    async def redirect(url: str) -> None:
        print("\nOpen this address in a browser, sign in with Google and allow:\n\n  " + url + "\n", flush=True)

    async def callback() -> tuple[str, str | None]:
        raw = input("Paste the address the browser landed on (it starts with " + REDIRECT_URI + "): ")
        return parse_callback(raw)

    return OAuthClientProvider(
        server_url=f"{base}/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="Admiry probe",
            redirect_uris=[AnyUrl(REDIRECT_URI)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect,
        callback_handler=callback,
    )


# ---------------------------------------------------------------- the checks


async def call(session: ClientSession, name: str, args: dict) -> dict:
    """One tool call; the envelope (structuredContent) on success, ProbeError with the text on isError."""
    res = await session.call_tool(name, args)
    if res.isError:
        text = " ".join(getattr(c, "text", "") for c in res.content).strip()
        raise ProbeError(text or f"{name}: error")
    return res.structuredContent or {}


async def _raw_checks(base: str, factory, rows: list[Row]) -> list[str] | None:
    """The checks before a session, with no credential; returns the tool names the docs publish (TOOLS_JSON, a public
    page), which the tools row compares with tools/list -- None when they cannot be read."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    async with factory() as http:
        r = await http.post(f"{base}/mcp", json=init, headers=RPC_HEADERS)
        www = r.headers.get("www-authenticate", "(none)")
        rows.append(
            Row(
                "P1",
                "POST /mcp without a credential",
                f"HTTP {r.status_code}; WWW-Authenticate: {www}",
                r.status_code == 401 and "resource_metadata" in www,
            )
        )
        prm = await http.get(f"{base}/.well-known/oauth-protected-resource/mcp")
        p = prm.json() if prm.status_code == 200 else {}
        rows.append(
            Row(
                "P2a",
                "protected-resource metadata",
                f"HTTP {prm.status_code}; resource {p.get('resource')}; authorization_servers {p.get('authorization_servers')}; "
                f"scopes {p.get('scopes_supported')}",
                prm.status_code == 200 and p.get("resource") == f"{base}/mcp",
            )
        )
        asm = await http.get(f"{base}/.well-known/oauth-authorization-server")
        a = asm.json() if asm.status_code == 200 else {}
        rows.append(
            Row(
                "P2b",
                "authorization-server metadata",
                f"HTTP {asm.status_code}; registration {bool(a.get('registration_endpoint'))}; "
                f"PKCE {a.get('code_challenge_methods_supported')}; grants {a.get('grant_types_supported')}",
                asm.status_code == 200 and "S256" in (a.get("code_challenge_methods_supported") or []),
            )
        )
        return _published(await http.get(f"{base}{TOOLS_JSON}"))


async def _session_checks(session: ClientSession, rows: list[Row], published: list[str] | None) -> None:
    init = await session.initialize()
    ext = (init.capabilities.model_extra or {}).get("extensions") or {}
    rows.append(
        Row(
            "P3",
            "initialize",
            f"{init.serverInfo.name} · protocol {init.protocolVersion} · extensions {sorted(ext) or 'none'} · "
            f"instructions {len(init.instructions or '')} chars",
            UI_EXTENSION in ext,
        )
    )
    tools = (await session.list_tools()).tools
    with_view = [t.name for t in tools if ((t.meta or {}).get("ui") or {}).get("resourceUri") == PLAN_VIEW_URI]
    names = [t.name for t in tools]
    hidden = "whoami" not in names  # one command stands for them all: an open gate lists every one (as smoke.sh checks)
    as_published, against = against_the_docs(names, published)  # the list itself, name for name
    rows.append(
        Row(
            "P4",
            "tools/list",
            f"{len(tools)} tools; the walk's {[n for n in WALK_TOOLS if n in names]}; "
            f"plan card on {sorted(with_view)}; commands hidden {hidden}; {against}",
            {"get_marketing_state", *WALK_TOOLS} <= set(names)
            and set(with_view) == PLAN_CARD_TOOLS
            and hidden
            and as_published,
        )
    )
    prompts = [p.name for p in (await session.list_prompts()).prompts]
    resources = {str(r.uri): r for r in (await session.list_resources()).resources}
    skill = await session.read_resource(AnyUrl(SKILL_URI))
    skill_text = getattr(skill.contents[0], "text", "") if skill.contents else ""
    view = await session.read_resource(AnyUrl(PLAN_VIEW_URI)) if PLAN_VIEW_URI in resources else None
    view_mime = view.contents[0].mimeType if view and view.contents else None
    rows.append(
        Row(
            "P5",
            "prompts and resources",
            f"prompts {prompts}; resources {sorted(resources)}; skill {len(skill_text)} chars; plan card mime {view_mime}",
            sorted(prompts) == PROMPTS
            and view_mime == "text/html;profile=mcp-app"
            and "get_marketing_state" in skill_text,
        )
    )
    state = await call(session, "get_marketing_state", {})
    d = state.get("data", state)
    caller, biz = d.get("caller") or {}, d.get("business") or {}
    rows.append(
        Row(
            "P6",
            "get_marketing_state",
            f"key class {caller.get('class')} · scopes {caller.get('scopes')} · business {biz.get('id')} "
            f"({biz.get('claim_state')}) · version {d.get('surface_version')} · "
            f"consultation {(d.get('consultation') or {}).get('status')}",
            bool(biz.get("id")) and bool(d.get("surface_version")),
        )
    )


async def _walk(session: ClientSession, handoff: str | None, rows: list[Row]) -> None:
    """The consultation end to end. A refusal or a missing field at any stage is that stage's failing row and
    ends the walk; the rows before it stand."""
    calls = 0
    says: list[str] = []
    kinds: list[str] = []

    def note(res: dict) -> dict:
        says.append(res.get("say") or "")
        kinds.append((res.get("next") or {}).get("kind", "?"))
        return res

    stage = "P7"
    try:
        args: dict[str, Any] = {"handoff": handoff} if handoff else {"message": WALK_TEXT}
        res = note(await call(session, "create_marketing_plan", args))
        calls += 1
        sid = res["data"]["session_id"]
        used_other = False
        asked = 0
        while (res.get("next") or {}).get("kind") == "question" and asked < MAX_QUESTIONS:
            q = res["next"]["question"]
            asked += 1
            ans = pick_answer(q, allow_other=not used_other)
            used_other = used_other or "other" in ans
            res = note(
                await call(
                    session,
                    "update_marketing_plan",
                    {"answers": {q["id"]: ans}, "idempotency_key": f"probe-a{asked}-{sid}"},
                )
            )
            calls += 1
        ready = walk_ready(res)
        rows.append(
            Row(
                "P7",
                "create → answers",
                f"session {sid}; {asked} question(s); free text used {used_other}; ready {ready}; next kinds {kinds}",
                ready,
            )
        )
        if not ready:
            return

        stage = "P8"
        rec = note(await call(session, "get_marketing_plan", {}))
        calls += 1
        plan_id = rec["data"]["plan_id"]
        cards = rec["data"]["cards"]
        actions = [a["tool"] for a in (rec.get("next") or {}).get("actions", [])]
        rows.append(
            Row(
                "P8",
                "get_marketing_plan builds",
                f"plan {plan_id}: {cards['plan']['goal']}; ${cards['budget']['monthly_usd']:,}/mo ({cards['budget']['level']}); "
                f"next {rec['next'].get('kind')} {actions}; {rec['data'].get('plan_url')}",
                bool(cards["plan"]["goal"]) and "not a forecast" in rec.get("say", ""),
            )
        )

        stage = "P9"
        try:
            adj = note(
                await call(
                    session,
                    "update_marketing_plan",
                    {"changes": {"monthly_budget_usd": ADJUST_USD}, "idempotency_key": f"probe-adj-{sid}"},
                )
            )
            plan_id = adj["data"]["plan_id"]
            observed = f"v{adj['data'].get('version')} at ${adj['data']['cards']['budget']['monthly_usd']:,}/mo"
            ok: bool | None = adj["data"]["cards"]["budget"]["monthly_usd"] == ADJUST_USD
        except ProbeError as e:
            # above the owner's limit is a valid refusal (spec §12), recorded and not judged; anything else failed
            observed, ok = f"refused: {e}", None if LIMIT_REFUSAL in str(e) else False
        calls += 1
        rows.append(Row("P9", f"change the budget to ${ADJUST_USD:,}", observed, ok))

        stage = "P10"
        gp = note(await call(session, "get_marketing_plan", {"plan_id": plan_id}))
        calls += 1
        rows.append(
            Row(
                "P10",
                "get_marketing_plan reads",
                f"{gp['data'].get('status')} v{gp['data'].get('version')}; cards {'cards' in gp['data']}; {gp['data'].get('plan_url')}",
                "cards" in gp["data"],
            )
        )
    except ProbeError as e:
        rows.append(Row(stage, f"the walk stopped at {stage}", f"refused: {e}", False))
    except (KeyError, TypeError) as e:
        rows.append(Row(stage, f"the walk stopped at {stage}", f"the result lacks {e!r}", False))
    finally:
        if says:
            rows.append(
                Row(
                    "P11",
                    "the walk's tool calls and words",
                    f"{calls} calls; every say non-empty {all(s.strip() for s in says)}; shortest say {min(len(s) for s in says)} chars",
                    calls <= MAX_CALLS and all(s.strip() for s in says),
                )
            )


async def _rotation(base: str, storage: FileStorage, factory, rows: list[Row]) -> None:
    """Spend the stored refresh token twice: the first rotates, the second (reuse) answers 401 invalid_grant and
    revokes the family, and the access token the first minted stops working. The store's tokens are dropped
    afterwards (the grant is dead)."""
    d = storage.load()
    tokens, client = d.get("tokens") or {}, d.get("client") or {}
    if not tokens.get("refresh_token") or not client.get("client_id"):
        rows.append(Row("P12", "refresh-token rotation", "no stored grant: run --oauth first", False))
        return
    async with factory() as http:
        asm = (await http.get(f"{base}/.well-known/oauth-authorization-server")).json()
        te = asm["token_endpoint"]
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        }
        r1 = await http.post(te, data=form)
        fresh = r1.json() if r1.status_code == 200 else {}
        rotated = bool(fresh.get("refresh_token")) and fresh.get("refresh_token") != tokens["refresh_token"]
        rows.append(Row("P12", "refresh once", f"HTTP {r1.status_code}; new refresh token {rotated}", rotated))
        r2 = await http.post(te, data=form)
        err = r2.json().get("error") if r2.headers.get("content-type", "").startswith("application/json") else None
        rows.append(
            Row(
                "P13",
                "refresh again with the spent token",
                f"HTTP {r2.status_code} {err or r2.text[:120]}",
                r2.status_code == 401 and err == "invalid_grant",
            )
        )
        if fresh.get("access_token"):
            who = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_marketing_state", "arguments": {}},
            }
            r3 = await http.post(
                f"{base}/mcp", json=who, headers={**RPC_HEADERS, "Authorization": f"Bearer {fresh['access_token']}"}
            )
            rows.append(
                Row("P14", "the rotated access token after the reuse", f"HTTP {r3.status_code}", r3.status_code == 401)
            )
        else:
            rows.append(Row("P14", "the rotated access token after the reuse", "no rotated token to test", False))
    d.pop("tokens", None)
    storage.save(d)


async def run(
    base: str,
    *,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | None = None,
    walk: bool = False,
    handoff: str | None = None,
    rotation: FileStorage | None = None,
    factory=None,
    rows: list[Row] | None = None,
) -> list[Row]:
    """Every check in order; `rows` (the caller's list) keeps what was recorded even if a later step raises."""
    from mcp.shared._httpx_utils import create_mcp_http_client

    factory = factory or create_mcp_http_client
    rows = rows if rows is not None else []
    base = base.rstrip("/")
    published = await _raw_checks(base, factory, rows)
    async with (
        factory(headers=headers, auth=auth, timeout=httpx.Timeout(30, read=300)) as http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await _session_checks(session, rows, published)
        if walk:
            await _walk(session, handoff, rows)
    if rotation is not None:
        await _rotation(base, rotation, factory, rows)
    return rows


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("base", help="the agent's public base URL, e.g. https://agent.dev.adx.admiry.ai")
    cred = parser.add_mutually_exclusive_group(required=True)
    cred.add_argument("--key-file", help="a 0600 file holding one ak_ key (a business key for --walk)")
    cred.add_argument("--oauth", action="store_true", help="sign in through the browser (DCR + PKCE); tokens kept 0600")
    parser.add_argument("--walk", action="store_true", help="run the dental consultation end to end")
    parser.add_argument(
        "--handoff-file",
        help="a 0600 file holding a website hand-off token (hs_...) to continue instead of starting fresh",
    )
    parser.add_argument("--rotation-check", action="store_true", help="with --oauth: spend the refresh token twice")
    parser.add_argument("--out", help="record the Markdown table in this file (under '### Results' when it has one)")
    parser.add_argument("--json", action="store_true", help="print JSON rows instead of Markdown")
    a = parser.parse_args(argv)

    if a.rotation_check and not a.oauth:
        print("--rotation-check needs --oauth", file=sys.stderr)
        return 2
    headers = auth = rotation = handoff = None
    try:
        if a.key_file:
            headers = {"Authorization": f"Bearer {read_secret(a.key_file, prefix='ak_', what='key')}"}
        if a.handoff_file:
            handoff = read_secret(a.handoff_file, prefix="hs_", what="hand-off token")
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    if a.oauth:
        storage = store_for(a.base)
        auth = oauth_provider(a.base, storage)
        rotation = storage if a.rotation_check else None

    rows: list[Row] = []
    failed: BaseException | None = None
    try:
        asyncio.run(run(a.base, headers=headers, auth=auth, walk=a.walk, handoff=handoff, rotation=rotation, rows=rows))
    except Exception as e:  # noqa: BLE001 - the rows so far are the report; the error (an ExceptionGroup too) is the last row
        failed = e
        rows.append(Row("P0", "the probe stopped", f"{type(e).__name__}: {e}"[:300], False))
    if a.json:
        print(json.dumps([r.__dict__ for r in rows], indent=1))
    else:
        text = render(
            rows,
            base=a.base,
            when=datetime.now(tz=timezone.utc).date(),
            title="Probe (" + ("OAuth" if a.oauth else "key") + ")",
        )
        print(text)
        if a.out:
            record(Path(a.out), text)
    return 1 if failed or any(r.ok is False for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
