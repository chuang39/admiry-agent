"""Append-only ledger of tool calls (spec 2026-09-24 §5.5) and idempotent replay (§4.0).

agent_action is also what rate limits count (auth._rate_check) and what
get_audit_log renders as sentences (part 2). Arguments are stored redacted: every
value stringified and cut to 80 chars; the full hash lets an operator match a call
without the values. Results should be JSON-native (ISO strings for times); anything else is
stored via str(). Strings are stored without NUL characters (_storable): Postgres text and jsonb
cannot hold one, and a failed insert would leave a call without its row. A repeat of
an idempotent call is its own row: ok=true, no idempotency_key, result {"replay_of": id}.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

MAX_VALUE = 80
SECRET_FIELDS = frozenset(
    {
        "draft_key",
        "plaintext",
        "approval_code",
        "api_key",
        "access_token",
        "refresh_token",
        "code",  # OAuth authorization code (A1b-4b)
        "developer_token",
        "password",
        "secret",
        "connect_url",  # carries the claim token (A1b-1); the audit row does not need the URL
        "checkout_url",
        "handoff",  # the hs_ hand-off token (A1b-4c Task 3): live only in /api/handoff's response and the prompts
        "contact",  # a lead's name/phone/e-mail (A3p-1 P6): never in an audit row
        # A3p-1 I6: the rest of a lead's own words, and the owner's note on it (update_lead audits `note` as
        # `lead_note`). Not the bare `note` / `name` / `phone` / `email`: other tools' results carry those for the
        # business itself (ops.adjust's note the owner reads in get_audit_log; describe_business's name and
        # phone, which an idempotent replay answers from this very row), and a lead's contact is only ever
        # under `contact`.
        "lead_note",
        # A3p-3 (P19, P23): a hosted page's whole source (get_page_source answers it, up to a megabyte): the
        # assistant reads it; the audit row only needs that it was read
        "html",
        # A3p-3 batch 3: links that are capabilities -- an upload link (a site's, or create_page's photo link, and
        # the curl command that carries it) and the source's one-hour zip link
        "upload_url",
        "how",
        "zip_url",
        "message",
        "preferred_time",
        "zip",  # a lead's ZIP code, where the visitor is (get_leads answers it since 2026-10-08)
        "resend_api_key",  # Resend's API key (A3p-1 Task 2)
        "resend_webhook_secret",  # the Svix signing secret of Resend's inbound webhook (A1b-5)
        # A3p-5 batch 2 (ruling R3-e): the web person's address send_install_steps e-mails -- someone else's, never in
        # the ledger (spec §5: install_request keeps only its hash). The replay digest still hashes it unscrubbed
        "recipient",
        # A3p-6 batch 2 Task 4 fix round 1: an industry site's reviews -- a reviewer's first name and words, someone
        # else's, as a lead's contact is (build_site and get_site answer them back in the site's facts)
        "reviews",
    }
)  # Stripe's hosted payment page (A1b-3): a capability, never stored
# Secret only inside a `facts` object (A3p-6 batch 2 Task 4 fix round 1): the e-mail address an industry site shows.
# Anywhere else `email` is how a mail went ("sent", "no_address": set_landing_page's answer, which a replay gives back,
# and the jobs' rows), so a global entry would blank it
FACTS_SECRET_FIELDS = frozenset({"email"})


def scrub(obj: Any, *, _facts: bool = False) -> Any:
    """A copy of obj with every value under a secret-named key replaced by '***', at any depth —
    a key or a code must never sit in the audit table (spec §5.5 Credentials) — and, in the object a `facts` key
    holds, every value under a FACTS_SECRET_FIELDS key too."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            secret = k in SECRET_FIELDS or (_facts and k in FACTS_SECRET_FIELDS)
            out[k] = "***" if secret and v not in (None, "") else scrub(v, _facts=k == "facts")
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def _dumps(obj: Any) -> str:
    """JSON for the audit columns: a datetime or Decimal in a result must never fail the insert."""
    return json.dumps(obj, default=str)


def _storable(obj: Any) -> Any:
    """A copy of obj with "\\x00" removed from every string, dict keys included, at any depth (dicts,
    lists, tuples): Postgres text and jsonb refuse NUL, and a caller can send one in any argument."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {(_storable(k) if isinstance(k, str) else k): _storable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_storable(v) for v in obj]
    return obj


KEY_REUSED = "idempotency_key already used for a different request; use a new key"


@dataclass(frozen=True)
class Replay:
    id: int  # the agent_action row that holds the original result
    result: Any


def redact(args: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in sorted(args):
        v = args[k]
        s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
        out[k] = s if len(s) <= MAX_VALUE else s[: MAX_VALUE - 1] + "…"
    return out


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def call_hash(tool: str, args: dict[str, Any]) -> str:
    """The identity of a public call: its tool and the arguments the assistant sent (the gate blanks a code), as the
    call means them, not as its JSON happened to be written -- a host may re-serialise its retry. An argument sent as
    null is the argument left out (at the top level only: inside an object null means something, as in `changes`,
    where it removes the target), and a whole number written with a decimal point is that number, at any depth
    (1500.0 is 1500; 12.5 and booleans stay as they are). Nothing else is normalised: strings are the owner's words,
    compared exactly."""
    sent = {key: _whole(value) for key, value in args.items() if value is not None}
    return args_hash({"tool": tool, "args": sent})


def _whole(value: Any) -> Any:
    """`value` with every float that holds a whole number made that integer, inside objects and lists too."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _whole(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_whole(item) for item in value]
    return value


# How long a call can be answered again by its idempotency key: a public call found again (find_surface, G1 Task 18)
# and a command's own replay (replay; final review A P1) alike. A host's retry comes within minutes; a key an assistant
# reuses days later must not be answered with an old result, which may no longer be true. After the window a public
# call is routed afresh, and the command it reaches refuses a key it already holds as reused.
SURFACE_REPLAY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class SurfaceReplay:
    id: int  # the agent_action row that holds the first answer
    tool: str  # the command the first call ran
    result: Any
    call_hash: str | None  # None on a row written before the column existed


def find_surface(
    conn: psycopg.Connection,
    business_id: str,
    surface_tool: str,
    idempotency_key: str,
    *,
    call_hash: str | None = None,
) -> SurfaceReplay | None:
    """An earlier successful public call of this business with this idempotency key, whichever command it ran, made
    within SURFACE_REPLAY_WINDOW: the row of this same call (`call_hash`) when there is one -- one key may carry the
    ask and then the confirmation of a change -- else the latest row with the key. Never a row without a business."""
    row = conn.execute(
        "SELECT id, tool, result, surface_call_hash FROM agent_action WHERE business_id = %s AND surface_tool = %s "
        "AND idempotency_key = %s AND ok AND ts > now() - %s "
        "ORDER BY (surface_call_hash = %s) IS TRUE DESC, id DESC LIMIT 1",
        (business_id, surface_tool, _storable(idempotency_key), SURFACE_REPLAY_WINDOW, call_hash),
    ).fetchone()
    if row is None:
        return None
    return SurfaceReplay(id=row["id"], tool=row["tool"], result=row["result"], call_hash=row["surface_call_hash"])


def replay(
    conn: psycopg.Connection, business_id: str | None, tool: str, idempotency_key: str | None, *, args_hash: str
) -> Replay | None:
    """The earlier successful call with the same idempotency key (its row id and stored result), made within
    SURFACE_REPLAY_WINDOW.

    Raises ToolError(KEY_REUSED) when that call had different arguments (args_hash): its result would
    answer a request that never ran; and when its row is older than the window: an answer that old may no longer be
    true (a pause answered "paused" nine days on, while the ads ran), and the unique index never lets this command
    store the key again, so a new key is the way on."""
    if idempotency_key is None:  # run() rejects ""; any other string is a key
        return None
    row = conn.execute(
        "SELECT id, result, args_hash, ts > now() - %s AS fresh FROM agent_action "
        "WHERE business_id IS NOT DISTINCT FROM %s AND tool = %s AND idempotency_key = %s AND ok",
        (SURFACE_REPLAY_WINDOW, business_id, tool, _storable(idempotency_key)),
    ).fetchone()  # stored without NUL: look it up the same way
    if row is None:
        return None
    if row["args_hash"] != args_hash or not row["fresh"]:
        raise ToolError(KEY_REUSED)
    return Replay(id=row["id"], result=row["result"])


def record(
    conn: psycopg.Connection,
    *,
    tool: str,
    args: dict[str, Any],
    result: Any,
    ok: bool,
    business_id: str | None,
    actor: str,
    actor_class: str | None,
    api_key_id: str | None,
    ip: str | None,
    idempotency_key: str | None = None,
    approval_kind: str | None = None,
    approval_ref: str | None = None,
    surface_tool: str | None = None,
    surface_call_hash: str | None = None,
) -> int:
    """Insert one row. Caller-supplied strings (args, result, idempotency_key, the address, the public name) are made
    storable first; args_hash is taken from the args as received, like run()'s replay digest. `surface_tool` is the
    public tool an assistant called when `tool` is the command behind it (spec 2026-10-01 §3), or the resource it read
    (gate.resource_tool); `surface_call_hash` is that public call's identity (`call_hash`)."""
    row = conn.execute(
        "INSERT INTO agent_action (business_id, actor, actor_class, api_key_id, tool, args_redacted, "
        "args_hash, result, ok, approval_kind, approval_ref, idempotency_key, ip, surface_tool, surface_call_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            business_id,
            _storable(actor),
            actor_class,
            api_key_id,
            tool,
            Jsonb(redact(scrub(_storable(args))), dumps=_dumps),
            args_hash(args),
            Jsonb(scrub(_storable(result)), dumps=_dumps),
            ok,
            approval_kind,
            approval_ref,
            _storable(idempotency_key),
            _storable(ip),
            _storable(surface_tool),
            surface_call_hash,
        ),
    ).fetchone()
    return row["id"]


def record_web(
    conn: psycopg.Connection,
    *,
    tool: str,
    email: str,
    ip: str | None,
    business_id: str | None,
    args: dict[str, Any],
    result: Any,
    approval_kind: str | None = None,
    approval_ref: str | None = None,
) -> int:
    """A row for something the signed-in owner did on a page: actor web:<email>, class business, no key."""
    return record(
        conn,
        tool=tool,
        args=args,
        result=result,
        ok=True,
        business_id=business_id,
        actor=f"web:{email}",
        actor_class="business",
        api_key_id=None,
        ip=ip,
        approval_kind=approval_kind,
        approval_ref=approval_ref,
    )


def record_job(
    conn: psycopg.Connection, *, job: str, tool: str, business_id: str | None, args: dict[str, Any], result: Any
) -> int:
    """A row for something a scheduled job did by itself (ads-link-sync verifying a business): actor job:<name>,
    class job, no key, no address."""
    return record(
        conn,
        tool=tool,
        args=args,
        result=result,
        ok=True,
        business_id=business_id,
        actor=f"job:{job}",
        actor_class="job",
        api_key_id=None,
        ip=None,
    )


def record_system(
    conn: psycopg.Connection, *, tool: str, actor: str, business_id: str | None, args: dict[str, Any], result: Any
) -> int:
    """A row for something an outside system did, with no person or key in the request — the Stripe webhook (actor
    "stripe"): class system, no key, no address, no approval. Scheduled jobs use record_job."""
    return record(
        conn,
        tool=tool,
        args=args,
        result=result,
        ok=True,
        business_id=business_id,
        actor=actor,
        actor_class="system",
        api_key_id=None,
        ip=None,
    )
