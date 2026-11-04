"""Who is calling a tool, and may they? (spec 2026-09-24 §4.0 keys, §5.5 rate limits)

Every tool body runs under `auth.require(conn, scope)`. The key comes from
`Authorization: Bearer ak_…` or `X-API-Key` (Claude Desktop parity). Rate limits are
counted from agent_action rows — no extra state, so N replicas need no coordination.
The client IP is the first X-Forwarded-For entry (Caddy sets it) or "local".

Refusals are `Denied` (carrying the refused key when one was found, so run() can
attribute the attempt to it) or `RateLimited` (never recorded).

Cost ceilings (Ruling 13): anonymous `start` is limited per address (START_PER_HOUR) and
globally per day (check_start_budget); per business, the tools that call the model are capped per
day (check_llm_cap, lower on a draft key) and run one at a time (run()'s advisory lock), and
manage-scope changes are capped per hour (check_mutations, spec §5.5; account-scope tools such as
revoke_key are exempt, so the owner can always revoke a runaway key, and so is `pause` -- UNCAPPED -- so the owner
can always stop spending; the limit counts only the changes it applies to).
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from . import keys

RATE_PER_MINUTE = 60
RATE_PER_DAY = 2000
# The tools that call the model: one call each -- a page's words (create_page, revise_page) one more when the linter
# refuses the first, change_plan one more when it rewrites the plan. run() lets one of them run at a time per
# business, and check_llm_cap counts them per day. build_site (A3p-6 batch 2 Q6) is one so that a business builds one
# site at a time; it asks for the day's limit only before the model writes the site's words, and only its calls that
# had them written count (their audit's `model`, Task 4 fix round 1).
LLM_TOOLS = (
    "start",
    "describe_business",
    "create_plan",
    "revise_plan",
    "answer",
    "recommend",
    "create_page",
    "revise_page",
    "ask",
    "change_plan",
    "build_site",
)
LLM_CALLS_PER_DAY = 40  # per business, any key
LLM_CALLS_PER_DAY_DRAFT = 10  # per business, when the caller holds a draft key
MUTATIONS_PER_HOUR = 30  # spec §5.5: successful W/P calls per business
UNCAPPED = ("pause",)  # manage-scope tools never refused by that limit: stopping spend is always allowed (A3 §5)
# The longest idempotency key run() takes, in characters (final review A P2): the key is stored and indexed with the
# call's row, and one too long for the index let a command that acts in `prepare` act and then fail to record it
MAX_IDEMPOTENCY_KEY = 200
# get_http_headers() strips `authorization` unless asked for it explicitly.
HEADERS = {"authorization", "x-api-key", "x-forwarded-for"}
# The keyless refusal (require() and run()'s pre-connect check). Over HTTP a request with no bearer
# never reaches here any more (A1b-4b Task 2): FastMCP's auth middleware answers 401 first, and
# smoke.sh checks that status instead. This text is still what a direct, in-process call to run()
# with an empty header sees, and what the docs quote for that case.
MISSING_KEY = (
    "missing key: send `Authorization: Bearer ak_...` (or `X-API-Key: ak_...`). "
    "Owners get a key at {base}/connect, or connect through OAuth."
)
# A key whose business is no longer active (a draft never claimed that lapsed, or one a hand-off folded into another
# business): connect.ERRORS copies it
BUSINESS_EXPIRED = "this business has expired; begin again with a new plan"


class Denied(ToolError):
    """The caller may not run the tool. `key` is the refused key when one was found (revoked,
    expired, lacking the scope): run() records the attempt against it. None means there is
    nothing to attribute the call to (no key, unknown key) and nothing is recorded."""

    def __init__(self, message: str, *, key: keys.KeyRecord | None = None) -> None:
        super().__init__(message)
        self.key = key


class RateLimited(ToolError):
    """The key is over a limit. Never recorded: a rejection must not write rows (no amplification)."""


@dataclass(frozen=True)
class Principal:
    key: keys.KeyRecord
    ip: str

    @property
    def business_id(self) -> str | None:
        return self.key.business_id

    @property
    def actor(self) -> str:
        return f"key:{self.key.id}"


_HEADER_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar("agent_header_override", default=None)


@contextlib.contextmanager
def with_headers(headers: dict[str, str]):
    """Run tool functions in process as if these were the request headers (the homepage's client,
    A1b-4c: no real MCP request exists, so request_headers() cannot read one from FastMCP)."""
    token = _HEADER_OVERRIDE.set({k.lower(): v for k, v in headers.items()})
    try:
        yield
    finally:
        _HEADER_OVERRIDE.reset(token)


def request_headers() -> dict[str, str]:
    """Lower-cased headers of the current MCP request ({} outside a request), or the with_headers()
    override when one is set for this context."""
    override = _HEADER_OVERRIDE.get()
    if override is not None:
        return {k: v for k, v in override.items() if k in HEADERS}
    return get_http_headers(include=HEADERS)


def bearer(headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return (headers.get("x-api-key") or "").strip() or None


def client_ip(headers: dict[str, str]) -> str:
    forwarded = headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() if forwarded else "local"


def rate_check(conn: psycopg.Connection, rec: keys.KeyRecord) -> None:
    """Per-key limits (RateLimited, never recorded): require() checks every key it finds, and the homepage's routes
    that run no tool (/api/support, /api/feedback) check the visitor's key themselves. An OAuth access token (grant_id
    set) is counted with every other access token of its grant: each refresh mints a new api_key row, and the limits
    must not reset with it (final review I4)."""
    if rec.grant_id is not None:
        who, arg = "api_key_id IN (SELECT id FROM api_key WHERE grant_id = %s)", rec.grant_id
    else:
        who, arg = "api_key_id = %s", rec.id
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE ts > now() - interval '1 minute') AS minute, "
        "       count(*) AS day, "
        "       min(ts) FILTER (WHERE ts > now() - interval '1 minute') AS oldest_minute, "
        "       min(ts) FILTER (WHERE ts > now() - interval '1 day') AS oldest_day "
        f"FROM agent_action WHERE {who} AND ts > now() - interval '1 day'",
        (arg,),
    ).fetchone()
    if row["minute"] >= RATE_PER_MINUTE:
        age = (datetime.now(timezone.utc) - row["oldest_minute"]).total_seconds()
        raise RateLimited(f"rate limit: {RATE_PER_MINUTE} calls per minute; retry_after={max(1, 60 - int(age))}")
    if row["day"] >= RATE_PER_DAY:
        age = (datetime.now(timezone.utc) - row["oldest_day"]).total_seconds()
        raise RateLimited(f"rate limit: {RATE_PER_DAY} calls per day; retry_after={max(1, int(86400 - age))}")


_rate_check = rate_check  # its name before other modules called it


START_PER_HOUR = 3  # spec §4.0: `start` is rate-limited 3/IP/hour


def rate_check_ip(conn: psycopg.Connection, ip: str, tool: str) -> None:
    """Anonymous tools are limited per client address, counted from their own audit rows —
    successes and failures alike. Raises RateLimited; never recorded (no amplification)."""
    row = conn.execute(
        "SELECT count(*) AS n, min(ts) AS oldest FROM agent_action "
        "WHERE tool = %s AND ip = %s AND api_key_id IS NULL AND ts > now() - interval '1 hour'",
        (tool, ip),
    ).fetchone()
    if row["n"] >= START_PER_HOUR:
        age = (datetime.now(timezone.utc) - row["oldest"]).total_seconds()
        raise RateLimited(
            f"rate limit: {START_PER_HOUR} {tool} calls per hour per address; retry_after={max(1, int(3600 - age))}"
        )


def starts_today(conn: psycopg.Connection) -> int:
    """`start` calls since midnight (the database's time zone), successes and failures, keyed or
    anonymous: what the anonymous `start`'s own daily ceiling reads (check_start_budget)."""
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool = 'start' AND ts >= date_trunc('day', now())"
    ).fetchone()["n"]


# The calls to Google one business may make a day -- start's lookups of it and the places put on the map for it
# (geocode.lookups_left; walk fixes 1, Task 4 round 1, final review A2): the change cap does not bound a lookup the map
# could not place (a refusal is no change)
PLACES_LOOKUPS_PER_DAY = 20


def places_today(conn: psycopg.Connection) -> int:
    """Google Places calls since midnight (the database's time zone), for every business and every caller: one
    `places.lookup` row per call (geocode.record_lookup) -- start's lookup of the business, keyed or anonymous, and each
    place put on the map -- and nothing else, so a refused call that made none (a dead hand-off, a spent model cap)
    spends nothing (final review A2). What AGENT_PLACES_DAILY_BUDGET bounds, so the ceiling bounds total spend
    regardless of who is calling (walk fixes 1, Task 4 round 1)."""
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool = 'places.lookup' AND ts >= date_trunc('day', now())"
    ).fetchone()["n"]


def places_lookups_today(conn: psycopg.Connection, business_id: str | None) -> int:
    """The calls to Google made for one business since midnight (its `places.lookup` rows: start's lookups of it and the
    places put on the map for it): what PLACES_LOOKUPS_PER_DAY bounds. geocode.lookups_left asks it for a business
    only: lookups made before there is one are bounded by the day's budget alone."""
    return conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE tool = 'places.lookup' AND business_id IS NOT DISTINCT FROM %s "
        "AND ts >= date_trunc('day', now())",
        (business_id,),
    ).fetchone()["n"]


def check_start_budget(conn: psycopg.Connection, budget: int) -> None:
    """The global daily ceiling on anonymous `start` (AGENT_START_DAILY_BUDGET): however many
    addresses call, Places and LLM spend stays bounded. RateLimited; never recorded."""
    if starts_today(conn) >= budget:
        raise RateLimited("Admiry's free setup is busy today; try again tomorrow")


def check_llm_cap(conn: psycopg.Connection, principal: Principal) -> None:
    """Per-business daily ceiling on the tools that call the model (LLM_TOOLS): LLM_CALLS_PER_DAY, or
    LLM_CALLS_PER_DAY_DRAFT when the caller holds a draft key. Counts today's rows of those tools
    for the business — failures and refusals included, except a key's own denial — and leaves out `answer` rows that
    carried no free text: a choice never reaches the model (only an {"other": ...} answer calls read_answer), so it
    spends nothing. `answer` records that as the audit arg `free_text` (derived from `answers`; the stored
    `answers` value is clipped to audit.MAX_VALUE characters, too short to search). `answer` stays in
    LLM_TOOLS for run()'s per-business lock. `build_site` rows count only when their audit says `model` (A3p-6 batch 2
    Task 4 fix round 1): a question, a look asked, a build from words already written or a refusal never reaches the
    model; it too is one of LLM_TOOLS for the lock. So do the rows of a `create_page` that started a site (Task 5: for a
    family business it is `build_site`'s first step, and its audit says `site`); a starter page's row, which does not,
    always counts. A quick change refused on a site (Task 6: `revise_page`, `update_page`) refuses before the model is
    asked and its audit says `site`: such a row never counts either, an assistant that keeps asking leaves the day's
    drafts to the site's own words. A key's own denial (a missing scope, a revoked or expired key: run() marks its row
    `denied`) never reached the model, and counting it would let any key that may not run a model tool use up the
    owner's day (final review B). A ToolError: run() records it."""
    # The homepage's `say` action (A1b-4c Task 4) reads free text into read_adjust outside a tool: no
    # `run()` wraps that call, so it is not one of LLM_TOOLS (which also names run()'s per-business
    # advisory lock); home_api.py records it as its own `web.say` row instead, counted here by name.
    # The e-mail channel's reader and answerer (A1b-5) record mail.read / mail.answer rows the same way, and the setup
    # card's second look (web.review_plan) and the conversation's (B0) ad text (web.write_copy).
    # The home's harness (K6 slice 1, D7) records web.turn rows; one counts only when its result says counted: a model
    # turn unless its reroute hands answer words (answer's own free_text row counts that call), never a fast-path turn.
    check_llm_cap_for(conn, principal.business_id, draft=principal.key.cls == "draft")


def check_llm_cap_for(conn: psycopg.Connection, business_id: str, *, draft: bool = False) -> None:
    """check_llm_cap for a business by its id, for a model call no key makes: the conversation's (B0), where the
    owner's web session acts (blockers.write_copy). The same count and the same ceiling, ToolError at it."""
    cap = LLM_CALLS_PER_DAY_DRAFT if draft else LLM_CALLS_PER_DAY
    n = conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE business_id = %s "
        "AND ts >= date_trunc('day', now()) "
        "AND ((tool = ANY(%s) AND (tool <> 'answer' OR args_redacted->>'free_text' = 'true') "
        "AND ((tool <> 'build_site' AND NOT (tool IN ('create_page', 'revise_page', 'update_page') "
        "AND args_redacted->>'site' IS NOT DISTINCT FROM 'true')) "
        "OR args_redacted->>'model' = 'true')) "
        "OR tool IN ('web.say', 'mail.read', 'mail.answer', 'web.review_plan', 'web.write_copy') "
        "OR (tool = 'web.turn' AND result->>'counted' = 'true')) "
        "AND result->>'denied' IS DISTINCT FROM 'true'",
        (business_id, list(LLM_TOOLS)),
    ).fetchone()["n"]
    if n >= cap:
        raise ToolError(f"today's limit of {cap} drafts for this business is reached; try again tomorrow")


def check_mutations(conn: psycopg.Connection, business_id: str, tools: Sequence[str]) -> None:
    """At most MUTATIONS_PER_HOUR successful changes per business per hour, counted over the rows of `tools` -- the
    commands the limit applies to -- that carry an idempotency_key; replays and failures are not changes.
    RateLimited; never recorded. run() applies it to the manage-scope commands outside UNCAPPED and passes their
    names (the registry lives above this module): account-scope tools (revoke_key) stay available when another key
    has used up the business's changes — that is how an owner stops a runaway key — and what the limit does not
    apply to it does not count either, so leads recorded by a key that may only record them, support requests,
    revoked keys and pauses never use up the owner's changes (final review A I1)."""
    row = conn.execute(
        "SELECT count(*) AS n, min(ts) AS oldest FROM agent_action WHERE business_id = %s AND ok "
        "AND idempotency_key IS NOT NULL AND tool = ANY(%s) AND ts > now() - interval '1 hour'",
        (business_id, list(tools)),
    ).fetchone()
    if row["n"] >= MUTATIONS_PER_HOUR:
        age = (datetime.now(timezone.utc) - row["oldest"]).total_seconds()
        raise RateLimited(
            f"rate limit: {MUTATIONS_PER_HOUR} changes per hour per business; retry_after={max(1, int(3600 - age))}"
        )


def require(conn: psycopg.Connection, scope: str, *, web_base_url: str = "") -> Principal:
    """Resolve the caller from the request headers and enforce rate limits, key state and scope.

    The rate limit is checked for every key that was found, before its state: run() records
    the denials of a known key, and counting them keeps a revoked or expired key from
    writing unbounded rows."""
    headers = request_headers()
    token = bearer(headers)
    if not token:
        raise Denied(MISSING_KEY.format(base=web_base_url))
    rec = keys.lookup(conn, token)
    if rec is None:
        raise Denied("unknown key")
    _rate_check(conn, rec)
    if rec.revoked_at is not None:
        raise Denied("this key was revoked; get a new one at /connect", key=rec)
    if rec.is_expired():
        raise Denied("this key expired (draft keys last 24 hours); sign in at /connect for a lasting one", key=rec)
    if not rec.allows(scope):
        raise Denied(f"this key lacks the '{scope}' scope; keys are issued with scopes on /connect", key=rec)
    if rec.business_id is not None:
        business = conn.execute("SELECT status FROM business WHERE id = %s", (rec.business_id,)).fetchone()
        if business is None or business["status"] != "active":
            raise Denied(BUSINESS_EXPIRED, key=rec)
    keys.touch(conn, rec.id)
    return Principal(key=rec, ip=client_ip(headers))
