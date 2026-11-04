"""The MCP server: tool registry, the call wrapper every tool uses, and the account tools.

Descriptions are written for an LLM (spec §4.0): what · when · call-first · units, and
"IMPORTANT ACTION: confirm with the owner before calling" where it applies. Access
classes: RO (read) · W (write, no spend, no public effect) · P (proposal); every tool
needs a key now (A1b-4b Task 4 moved `start` off the anonymous class — its old anonymous
body is `tools_start.begin_anonymous`, a plain function, not a tool). Tools are plain
`def`s: FastMCP runs them in a thread pool, so psycopg's blocking calls never sit on the
event loop.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import fastmcp
import psycopg
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import audit, auth, config, db, envelope, google_ads_link, keys, oauth, surface, wallet
from .channels import registry
from .surface import gate, words
from .util import iso

# v3 default decorator_mode returns the plain function; "object" gives FunctionTool
# objects (.name/.description/.parameters/.fn), which the registry and the tests use.
fastmcp.settings.decorator_mode = "object"
log = logging.getLogger(__name__)

INSTRUCTIONS = words.INSTRUCTIONS  # spec 2026-10-01 §9.4: the router and the rules that never bend

INVALID_ARGUMENTS = "Invalid arguments for tool"  # how FastMCP's line for an argument of the wrong type begins
READING_RESOURCE = "Error reading resource"  # how its line for a failed resource read begins: the address follows


class ArgumentsKeptOut(logging.Filter):
    """FastMCP refuses an argument of the wrong type before the tool runs and logs the refusal with what it was sent
    -- an approval code, a lead's name and number (final review A m6). The line keeps the tool's name and nothing
    else, at its own level; what the assistant is answered is FastMCP's, unchanged. Its line for a failed resource
    read carries the address as it was sent, as long as the request body allows (re-review A, N1): the line keeps the
    address's first gate.MAX_RESOURCE_NAME characters, its level and its exception."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg.startswith(INVALID_ARGUMENTS):
            name = record.args[0] if isinstance(record.args, tuple) and record.args else None
            record.msg = f"{INVALID_ARGUMENTS} {name}" if isinstance(name, str) else INVALID_ARGUMENTS
            record.args = ()
        elif isinstance(record.msg, str) and record.msg.startswith(READING_RESOURCE):
            # the address as sent, in quotes (FastMCP writes its repr), read from the line as it would be printed
            sent = record.getMessage().removeprefix(READING_RESOURCE).strip().strip("'\"")
            record.msg = f"{READING_RESOURCE} {sent[: gate.MAX_RESOURCE_NAME]!r}"
            record.args = ()
        return True


# Constructed at import (no database connection: AdmiryOAuthProvider opens one only per request).
provider = oauth.AdmiryOAuthProvider(base_url=config.load().public_base_url)
mcp = FastMCP("Admiry", instructions=INSTRUCTIONS, mask_error_details=True, auth=provider)
# The logger FastMCP's server writes those lines on (fastmcp/server/server.py), filtered once, with the server
logging.getLogger("fastmcp.server.server").addFilter(ArgumentsKeptOut())
# What an assistant sees and what a call is called (spec 2026-10-01 §3): see surface/gate.py.
mcp.add_middleware(gate.SurfaceGate())

INTERNAL_ERROR = {"error": "internal error"}  # the audit result an unexpected exception leaves
# run()'s refusal of a key too long to store with the call's row (final review A P2)
KEY_TOO_LONG = f"idempotency_key is too long (over {auth.MAX_IDEMPOTENCY_KEY} characters); send a shorter unique key"
# The plan card view (mcp_view.py): the public tools that answer with the plan's `cards` -- get_marketing_plan,
# update_marketing_plan, update_marketing_budget -- point at it through `_meta.ui` (A1b-4d MCP Apps probe, G1 Task 13);
# a host without the extension ignores the pointer. The commands behind them (recommend, adjust, get_plan) still carry
# it, where nothing reads it: assistants are not shown commands (surface/gate.py), the homepage runs recommend and
# adjust in process and draws its cards from the command's own answer right after a build or a change (from the
# state's own on a reload), and the mail path runs those and get_plan in process.
PLAN_VIEW_URI = "ui://admiry/plan"
PLAN_VIEW = {"resourceUri": PLAN_VIEW_URI}
# The channels card view (M0 Task 6, spec 2026-09-29 §7.2): get_campaigns' rows, drawn the same way. Its command,
# get_channel_status, carries the pointer too, unread: assistants are not shown it and only get_campaigns runs it.
CHANNELS_VIEW_URI = "ui://admiry/channels"
CHANNELS_VIEW = {"resourceUri": CHANNELS_VIEW_URI}


@dataclass(frozen=True)
class ToolMeta:
    name: str
    cls: str  # A | RO | W | P
    scope: str  # read | manage | leads | account | none
    approval: str  # none | assistant | code
    tool: Any  # the FunctionTool: .description, .parameters (JSON schema), .fn


REGISTRY: list[ToolMeta] = []


def agent_tool(
    *,
    cls: str,
    scope: str,
    approval: str = "none",
    destructive: bool = False,
    open_world: bool | None = None,
    **tool_kwargs,
):
    """Register a tool with FastMCP and remember its access class for /connect/docs. `open_world` defaults to
    the class (A tools reach the outside world); a keyed tool that reads the web too (`start`) sets it."""
    assert cls in ("A", "RO", "W", "P"), cls
    assert scope in ("read", "manage", "leads", "account", "none"), scope
    annotations = {
        "readOnlyHint": cls == "RO",
        "destructiveHint": destructive,
        "idempotentHint": True,
        "openWorldHint": cls == "A" if open_world is None else open_world,
    }

    def decorate(fn: Callable):
        tool = mcp.tool(fn, annotations=annotations, **tool_kwargs)
        REGISTRY.append(ToolMeta(name=tool.name, cls=cls, scope=scope, approval=approval, tool=tool))
        return tool

    return decorate


def run(
    tool_name: str,
    scope: str,
    args: dict[str, Any],
    body: Callable[..., dict],
    *,
    idempotency_key: str | None = None,
    prepare: Callable[[Any, auth.Principal], Any] | None = None,
    after: Callable[[dict], None] | None = None,
) -> dict:
    """Authenticate → replay an idempotent repeat → prepare (outside the transaction) → run the
    body inside one transaction with its audit row.

    `prepare(conn, principal)` is where a tool talks to the outside world (the LLM, a website,
    Places): it runs after auth and the replay check and before the transaction; its return value
    is passed to `body(conn, principal, ctx)`. Without `prepare`, `body(conn, principal)`.

    `after(result)` runs once the transaction has committed, with no connection held -- never for a
    replay or a failure; the change is already committed, so an exception there is logged, not raised
    (spec 2026-09-30 D142: ticket mail goes out here, and the support-mail job retries what does not).

    The body and its ok=true audit row commit in one transaction: a crash cannot leave a
    change without its row, a ToolError rolls back what the body already wrote, and a
    concurrent identical call sees the winner's change and row together or not at all.
    Bodies must not call external APIs inside this block (that is what `prepare` is for).
    A loser — its body fails, or its audit insert hits the idempotency index — rolls back and
    answers with the winner's stored result.

    Every call made with a known key that ends in a result or an error leaves one
    agent_action row: the result, the error (ok=false, including the key's own denial -- marked
    {"denied": true}, which auth.check_llm_cap does not count -- and a rejected idempotency_key: empty,
    longer than auth.MAX_IDEMPOTENCY_KEY, reused for another request or past audit.SURFACE_REPLAY_WINDOW),
    or {"replay_of": id} for a repeat, which counts toward the
    rate limit. An unexpected exception leaves an ok=false {"error": "internal error"} row (the
    text the client sees is FastMCP's masked one; the traceback is logged server-side). Strings
    are stored without NUL characters (audit._storable), and if a failure row still cannot be
    written, a minimal one (no args, "internal error") is; only when even that fails does the
    call end without a row, with its own error. Calls with no key (refused before a connection
    is opened) or an unknown key, and calls over a rate limit (auth.RateLimited, including the
    per-business change limit checked here for manage-scope calls that carry an idempotency_key —
    a replay is not a change; account-scope tools such as revoke_key are exempt, and so is `pause`,
    auth.UNCAPPED; it counts the changes of the commands it applies to, and no other), leave none
    (nothing to attribute; no write amplification). The tools that call the model (auth.LLM_TOOLS)
    run one at a time per business (an advisory lock taken before prepare; a concurrent one is a
    recorded ToolError). A result carrying {"approval": {"kind": "code"|"assistant", "ref":
    <proposal id>}} fills the row's approval_kind / approval_ref (confirm_proposal).
    Secret-named fields are scrubbed."""
    settings = config.load()
    if not auth.bearer(auth.request_headers()):  # nothing to attribute: refused before opening a connection
        raise auth.Denied(auth.MISSING_KEY.format(base=settings.web_base_url))
    digest = audit.args_hash(args)
    with db.connect(settings.database_url) as conn:
        try:
            principal = auth.require(conn, scope, web_base_url=settings.web_base_url)
        except auth.Denied as e:
            if e.key is not None:  # a real key was refused: attribute the attempt to it, marked as its own denial
                refused = auth.Principal(key=e.key, ip=auth.client_ip(auth.request_headers()))
                _audit_failure(conn, tool_name, args, refused, {"error": str(e), "denied": True}, e)
            raise
        if idempotency_key == "":  # "" would be stored and indexed but never replayed
            e = ToolError("idempotency_key must not be empty; send a fresh unique key")
            _audit_failure(conn, tool_name, args, principal, {"error": str(e)}, e)
            raise e
        if idempotency_key is not None and len(idempotency_key) > auth.MAX_IDEMPOTENCY_KEY:
            e = ToolError(KEY_TOO_LONG)  # before the lookup and before prepare: nothing acts on a key it cannot store
            _audit_failure(conn, tool_name, args, principal, {"error": str(e)}, e)
            raise e
        replayed = _replay(conn, tool_name, args, principal, idempotency_key, digest)
        if replayed is not None:
            return replayed
        capped = scope == "manage" and tool_name not in auth.UNCAPPED
        if idempotency_key is not None and principal.business_id is not None and capped:
            auth.check_mutations(conn, principal.business_id, _capped())  # RateLimited: unrecorded
        lock = f"llm:{principal.business_id}" if tool_name in auth.LLM_TOOLS and principal.business_id else None
        locked = False
        try:
            if lock is not None:
                locked = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (lock,)).fetchone()["ok"]
                if not locked:
                    raise ToolError("another draft for this business is in progress; try again in a moment")
            ctx = prepare(conn, principal) if prepare is not None else None
            with conn.transaction():
                raw = body(conn, principal, ctx) if prepare is not None else body(conn, principal)
                result = envelope.wrap(tool_name, raw)
                _audit(conn, tool_name, args, principal, result, ok=True, idempotency_key=idempotency_key)
        except ToolError as e:
            # Rolled back. A concurrent identical call may have just succeeded: its stored
            # result is the answer this caller wanted, not an error.
            replayed = _replay(conn, tool_name, args, principal, idempotency_key, digest)
            if replayed is not None:
                return replayed
            _audit_failure(conn, tool_name, args, principal, {"error": str(e)}, e)
            raise
        except psycopg.errors.UniqueViolation as e:
            if e.diag.constraint_name != "agent_action_idem":
                # the body's own constraint, not an idempotency race: an unexpected failure
                _audit_failure(conn, tool_name, args, principal, INTERNAL_ERROR, e)
                raise
            # Both bodies succeeded; the winner committed first and this block rolled back.
            replayed = _replay(conn, tool_name, args, principal, idempotency_key, digest)
            if replayed is None:  # unreachable: the row that refused this insert is committed
                raise ToolError("concurrent identical call; retry") from None
            return replayed
        except Exception as e:
            _audit_failure(conn, tool_name, args, principal, INTERNAL_ERROR, e)
            raise  # FastMCP masks it ("Error calling tool …") and logs the traceback
        finally:
            if locked:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))
    if after is not None:
        try:
            after(result)
        except Exception:  # the change is committed; a failed after-step is retried by its own job
            log.warning("after-commit step of %s failed", tool_name, exc_info=True)
    return result


def run_anonymous(
    tool_name: str,
    args: dict[str, Any],
    *,
    prepare: Callable[[Any, str], Any],
    body: Callable[[Any, str, Any], dict],
    ip: str | None = None,
) -> dict:
    """`begin_anonymous`'s path: no key. The client address stands in for the principal — normally the
    current request's (`ip=None`); `begin_anonymous`'s own tests, and any caller outside an MCP request
    (A1b-4c's homepage), pass `ip` directly, since there is no request to read it from. One call per
    address at a time (a session advisory lock on "<tool>:<addr>"; a concurrent one is refused),
    and, under that lock, auth.rate_check_ip (counted from this tool's own rows, failures included)
    and the global daily budget (auth.check_start_budget) — these refusals are never recorded.
    Every call past them leaves one row, audited as actor ip:<addr>: the result with the business
    the body created (result["business_id"]), or the error (ToolError text, or "internal error"
    for an unexpected exception) with no business — NUL-free, with the same minimal-row fallback
    as run(). Same prepare / transaction split as run(); no idempotency (a repeat is a new draft)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        ip = ip if ip is not None else auth.client_ip(auth.request_headers())
        lock = f"{tool_name}:{ip}"
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (lock,)).fetchone()["ok"]:
            raise auth.RateLimited("another request from this address is in progress; retry in a moment")
        try:
            # checked under the lock: two calls from one address can never both pass the count
            auth.rate_check_ip(conn, ip, tool_name)
            auth.check_start_budget(conn, settings.start_daily_budget)
            try:
                ctx = prepare(conn, ip)
                with conn.transaction():
                    result = envelope.wrap(tool_name, body(conn, ip, ctx))
                    _audit_anonymous(
                        conn, tool_name, args, ip, result, ok=True, business_id=result["data"].get("business_id")
                    )
            except ToolError as e:
                _record_failure(
                    lambda a, r: _audit_anonymous(conn, tool_name, a, ip, r, ok=False), args, {"error": str(e)}, e
                )
                raise
            except Exception as e:
                _record_failure(
                    lambda a, r: _audit_anonymous(conn, tool_name, a, ip, r, ok=False), args, INTERNAL_ERROR, e
                )
                raise
            return result
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))


def need_business(p: auth.Principal) -> str:
    """The caller's business id; ToolError for a key bound to none (an unbound platform key)."""
    if p.business_id is None:
        raise ToolError("this key is not bound to a business (platform key)")
    return p.business_id


def _capped() -> list[str]:
    """The commands the hourly change limit applies to, and so the only ones whose changes it counts: the
    manage-scope commands outside auth.UNCAPPED (auth.check_mutations; auth cannot read this registry)."""
    return [meta.name for meta in REGISTRY if meta.scope == "manage" and meta.name not in auth.UNCAPPED]


def _record_failure(
    write: Callable[[dict[str, Any], Any], Any], args: dict[str, Any], result: Any, original: BaseException
) -> None:
    """Write a failure row with write(args, result). If that insert fails, write a minimal one (no args,
    "internal error") with the same attribution; if even that fails, raise `original` — the call's own
    error, never the audit's."""
    try:
        write(args, result)
    except Exception:
        log.warning("agent_action failure row not written; writing a minimal one", exc_info=True)
        try:
            write({}, INTERNAL_ERROR)
        except Exception:  # noqa: BLE001 - any audit failure surfaces the call's own error instead
            raise original


def _audit_failure(
    conn, tool_name: str, args: dict[str, Any], p: auth.Principal, result: Any, original: BaseException
) -> None:
    _record_failure(lambda a, r: _audit(conn, tool_name, a, p, r, ok=False), args, result, original)


def _call_hash() -> str | None:
    """The identity of the public call in progress, for its audit row; None outside one."""
    called = gate.call()
    return audit.call_hash(*called) if called else None


def _audit_anonymous(
    conn, tool_name: str, args: dict[str, Any], ip: str, result: Any, *, ok: bool, business_id: str | None = None
) -> None:
    audit.record(
        conn,
        tool=tool_name,
        args=args,
        result=result,
        ok=ok,
        business_id=business_id,
        actor=f"ip:{ip}",
        actor_class="anonymous",
        api_key_id=None,
        ip=ip,
        surface_tool=gate.SURFACE_TOOL.get(),
        surface_call_hash=_call_hash(),
    )


def _audit(
    conn,
    tool_name: str,
    args: dict[str, Any],
    p: auth.Principal,
    result: Any,
    *,
    ok: bool,
    idempotency_key: str | None = None,
) -> None:
    inner = result.get("data", result) if isinstance(result, dict) else None
    approval = inner.get("approval") if isinstance(inner, dict) else None
    audit.record(
        conn,
        tool=tool_name,
        args=args,
        result=result,
        ok=ok,
        business_id=p.business_id,
        actor=p.actor,
        actor_class=p.key.cls,
        api_key_id=p.key.id,
        ip=p.ip,
        idempotency_key=idempotency_key,
        approval_kind=approval.get("kind") if isinstance(approval, dict) else None,
        approval_ref=approval.get("ref") if isinstance(approval, dict) else None,
        surface_tool=gate.SURFACE_TOOL.get(),
        surface_call_hash=_call_hash(),
    )


def _replay(
    conn, tool_name: str, args: dict[str, Any], p: auth.Principal, idempotency_key: str | None, digest: str
) -> dict | None:
    """The earlier result for this idempotency key, recording this call as its replay; None for a
    new key. A key reused with different arguments, or whose row is older than audit.SURFACE_REPLAY_WINDOW (an answer
    that old may no longer be true), is refused as reused (audit.replay), and the refusal recorded."""
    try:
        cached = audit.replay(conn, p.business_id, tool_name, idempotency_key, args_hash=digest)
    except ToolError as e:
        _audit_failure(conn, tool_name, args, p, {"error": str(e)}, e)
        raise
    if cached is None:
        return None
    _audit(conn, tool_name, args, p, {"replay_of": cached.id}, ok=True)
    return cached.result


def replay_public(
    idempotency_key: str | None,
    scope: str,
    *,
    same_call_only: bool = False,
    still_stands: Callable[[Any], bool] | None = None,
) -> dict | None:
    """The first answer of the public call in progress, when the assistant already made this same call -- the same
    tool, the same arguments, the same idempotency key -- and it succeeded, within audit.SURFACE_REPLAY_WINDOW, and
    that answer still stands (`still_stands(answer)`, when given: a plan change's answer while the version it made is
    the latest). A tool that picks its command from where the business stands asks before it routes (the owner's yes:
    when nothing waits for it): the first call changed where the business stands, so run()'s own replay, keyed on the
    command's arguments, would not know the repeat -- or, for a call that names what it changes, would know it and
    give an answer that no longer stands. The repeat is recorded as its own row, as run() records one.

    A key that gives no answer was still used: for another request, or for this one, whose answer no longer stands.
    It is refused as a reused key (audit.KEY_REUSED), the refusal recorded under the command the key ran, and the tool
    routes nothing -- except with `same_call_only` (the owner's yes), where such a key is no answer and no refusal:
    None, and the tool says that nothing waits. None for a first call, outside a public call, without a key, for a
    key longer than auth.MAX_IDEMPOTENCY_KEY, with no lookup (the command refuses it: KEY_TOO_LONG), for a
    caller that cannot be resolved here under `scope` (the command the tool then runs refuses in its own words), and
    for a key bound to no business, which has no earlier call of its own. A rate limit raises, unrecorded, as in
    run()."""
    called = gate.call()
    if not idempotency_key or called is None or len(idempotency_key) > auth.MAX_IDEMPOTENCY_KEY:
        return None
    surface, sent = called
    settings = config.load()
    if not auth.bearer(auth.request_headers()):
        return None
    with db.connect(settings.database_url) as conn:
        try:
            principal = auth.require(conn, scope, web_base_url=settings.web_base_url)
        except auth.RateLimited:
            raise
        except auth.Denied:
            return None
        if principal.business_id is None:
            return None
        identity = audit.call_hash(surface, sent)
        earlier = audit.find_surface(conn, principal.business_id, surface, idempotency_key, call_hash=identity)
        if earlier is None or earlier.call_hash is None:
            return None
        answers = earlier.call_hash == identity and (still_stands is None or still_stands(earlier.result))
        if not answers:  # the key was used: for another request, or for this one, whose answer no longer stands
            if same_call_only:
                return None
            e = ToolError(audit.KEY_REUSED)
            _audit_failure(conn, earlier.tool, sent, principal, {"error": str(e)}, e)
            raise e
        _audit(conn, earlier.tool, sent, principal, {"replay_of": earlier.id}, ok=True)
        return earlier.result


# ---------------------------------------------------------------- account tools


@agent_tool(cls="RO", scope="read")
def whoami() -> dict:
    """Who is calling: the business this key belongs to (id, claim state draft / claimed /
    verified, status, locale) and the key's class, scopes, label and expiry. Call this first
    in a session. If the key is a 24-hour draft key, call request_connect_link and give the
    owner that link to claim the business (connect_url is the page itself); nothing publishes
    or spends before the business is verified. ads_mode and google_ads_status say whether the
    owner's Google Ads account is linked yet (details: get_connection_status); balance_usd is
    the prepaid advertising balance when Admiry runs the ads, prepaid (ads_mode managed;
    details: get_balance), else null. channels_connected names the channels whose account is connected
    (google_search, ...; details: get_connection_status). open_session_id is set when a consultation from `start` is
    open or ready; call answer with that session_id to continue it, or recommend once it is ready."""

    def body(conn, p: auth.Principal) -> dict:
        business, ads_mode, ads_status, connected = None, None, None, []
        if p.business_id:
            row = conn.execute(
                "SELECT id, slug, claim_state, status, locale, ads_mode FROM business WHERE id = %s", (p.business_id,)
            ).fetchone()
            if row:
                business = dict(row)
                ads_mode = business.pop("ads_mode")
                link = google_ads_link.get(conn, p.business_id)
                ads_status = link.owner_status if link else None
                connected = [
                    r["channel"]
                    for r in conn.execute(
                        "SELECT channel FROM channel_account WHERE business_id = %s AND status = 'active' "
                        "ORDER BY array_position(%s::text[], channel) NULLS LAST, channel",
                        (p.business_id, list(registry.ORDER)),
                    )
                ]
        from . import (
            consult,
        )  # local import: consult imports envelope, mcp_app imports envelope; no cycle through tools

        open_session = consult.open_for(conn, p.business_id) if p.business_id else None
        return {
            "business": business,
            "key": {
                "id": p.key.id,
                "class": p.key.cls,
                "scopes": list(p.key.scopes),
                "label": p.key.label,
                "expires_at": iso(p.key.expires_at),
            },
            "connect_url": f"{config.load().web_base_url}/connect",
            "ads_mode": ads_mode,
            "google_ads_status": ads_status,
            "channels_connected": connected,
            "balance_usd": wallet.to_usd(wallet.balance_cents(conn, p.business_id)) if ads_mode == "managed" else None,
            "open_session_id": open_session["id"] if open_session else None,
        }

    return run("whoami", "read", {}, body)


@agent_tool(cls="RO", scope="account")
def list_keys() -> dict:
    """List the keys issued for this business — ids, labels (muse / claude / chatgpt / web /
    draft), scopes, expiry, last use — never the keys themselves. Needs the `account` scope,
    which owners grant on /connect (off by default)."""

    def body(conn, p: auth.Principal) -> dict:
        if p.business_id is None:
            raise ToolError("this key is not bound to a business (platform key); nothing to list")
        return {"keys": keys.list_for_business(conn, p.business_id)}

    return run("list_keys", "account", {}, body)


@agent_tool(cls="W", scope="account", approval="assistant", destructive=True)
def revoke_key(key_id: str, idempotency_key: str) -> dict:
    """Revoke one of this business's keys immediately (for example a lost draft key or a
    connector the owner no longer uses). Needs the `account` scope. IMPORTANT ACTION:
    confirm with the owner before calling. Use a new idempotency_key per intended
    revocation; repeating one returns the earlier result without acting again."""

    def body(conn, p: auth.Principal) -> dict:
        if p.business_id is None:
            raise ToolError("platform keys cannot revoke through this tool")
        if not keys.revoke(conn, key_id, p.business_id):
            raise ToolError("no active key with that id for this business")
        rec = keys.get(conn, key_id)
        if rec is not None and rec.grant_id is not None:  # an OAuth access token: revoke its whole grant too
            oauth.revoke_grant(conn, rec.grant_id)
        return {"revoked": key_id}

    return run(
        "revoke_key",
        "account",
        {"key_id": key_id, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------- tool modules (register on import)
# mcp_view registers on `mcp` and imports it (the view URIs too) from this module, so it is a separate, last import
# statement, not merged alphabetically into the block above: it loads after those names exist and after every command
# has registered, so nothing it pulls in can reorder the tools (test_probe pins the order). The surface's prompts and
# resources (surface.told) register through surface.load(), with the public tools. connect, which surface.told
# imports, reads the tools only when it renders (a lazy import in connect.tool_docs since final wave 9), not when it
# is imported.
# A command module (tools_*) already in sys.modules here has not finished loading: each one imports this module before
# it defines its first command, so a process that imports one first (a job, an ops command) runs this module inside
# that import.
_COMMANDS_LOADING = [name for name in list(sys.modules) if name.startswith(f"{__package__}.tools_")]
from . import (  # noqa: F401 - imported to register the commands
    tools_account,
    tools_ads,
    tools_ads_run,
    tools_business,
    tools_channels,
    tools_consult,
    tools_creatives,
    tools_hosting,
    tools_insights,
    tools_pages,
    tools_plan,
    tools_proposals,
    tools_research,
    tools_sites,
    tools_start,
    tools_state,
    tools_support,
    tools_wallet,
)

# The public tools (spec 2026-10-01 §4): registered after every command, which they name and call in process. Not
# while a command module is still loading (_COMMANDS_LOADING): its commands do not exist yet. create_app() loads the
# surface again for that case, so the service has the public tools whichever module it imported first; the test
# process imports this module before any command module.
if not _COMMANDS_LOADING:
    surface.load()
from . import mcp_view  # noqa: F401
