"""Proposals and approval codes (spec §4.0 "Confirmation by consequence", §4.4). A P tool creates a
proposal; nothing happens until confirm_proposal. Spend class needs an approval code issued
out-of-band (the plan page after sign-in — A1b — or `python -m agent.ops issue-code` today);
non-spend class is confirmed by the assistant after the owner says yes, capped per business per
day. Codes are NN-NNN, hashed with the proposal id as salt, single use; five wrong tries in an
hour lock the proposal. confirm()/decline() run inside run()'s transaction."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import ads_run, audit, bundles, business, config, db, hosted, hosting_minimum, landing, launch_review, plans
from .channels import registry
from .util import iso

log = logging.getLogger(__name__)

SPEND_TTL = timedelta(days=7)
NONSPEND_TTL = timedelta(days=3)
DAILY_CONFIRM_CAP = 20  # business.operator_flags.daily_confirm_cap overrides
CODE_ATTEMPTS_PER_HOUR = 5
DECLINE_REASON_MAX = 200  # characters kept of the owner's reason (longer ones are cut, not refused)
EXECUTABLE = ("presence", "discovery", "ads", "creative", "conversion")
GOOGLE = "google_search"  # the channel `execution.ads` mirrors for one release (M0)
COLS = "id, business_id, class, kind, payload, summary, status, created_by, expires_at, resolved_at, created_at"
WRONG_CODE = "that approval code is not valid for this proposal (wrong, already used or expired)"
LOCKED = "too many wrong codes for this proposal; try again in an hour"
# The day's cap on changes confirmed on an assistant's word (a page's publish or take-down): the docs copy it, with N
DAILY_LIMIT = "today's limit of {cap} assistant-confirmed changes is reached; try again tomorrow"
# L1 (spec 2026-10-10 §5): a second yes on a plan already sent to Admiry's review -- precheck_approve_plan's own words
IN_REVIEW_REFUSAL = "this plan is already in review; its ads go live once Admiry passes it"


def normalize_code(code: str) -> str:
    """'48-311', '48311' and '48 311' (as relayed from voice) are the same code."""
    digits = "".join(ch for ch in code if ch.isdigit())
    return f"{digits[:2]}-{digits[2:]}" if len(digits) == 5 else code.strip()


def code_hash(proposal_id: str, code: str) -> str:
    return hashlib.sha256(f"{proposal_id}:{normalize_code(code)}".encode()).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(100):02d}-{secrets.randbelow(1000):03d}"


def _json(r) -> dict:
    return {
        "proposal_id": r["id"],
        "class": r["class"],
        "kind": r["kind"],
        "status": r["status"],
        "summary": r["summary"],
        "what_changes": r["payload"].get("what_changes", []),
        "approval_url": r["payload"].get("approval_url"),
        "needs_code": r["class"] == "spend",
        "created_by": r["created_by"],
        "expires_at": iso(r["expires_at"]),
        "resolved_at": iso(r["resolved_at"]),
    }


def create(
    conn: psycopg.Connection,
    business_id: str,
    *,
    cls: str,
    kind: str,
    payload: dict,
    summary: str,
    what_changes: list[str],
    approval_url: str,
    created_by: str = "assistant",
) -> dict:
    pid = db.new_id("pr")
    ttl = SPEND_TTL if cls == "spend" else NONSPEND_TTL
    r = conn.execute(
        f"INSERT INTO proposal (id, business_id, class, kind, payload, summary, created_by, expires_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s) RETURNING {COLS}",
        (
            pid,
            business_id,
            cls,
            kind,
            Jsonb({**payload, "what_changes": what_changes, "approval_url": approval_url}),
            summary,
            created_by,
            ttl,
        ),
    ).fetchone()
    return _json(r)


def get(conn: psycopg.Connection, business_id: str, pid: str) -> dict | None:
    r = conn.execute(f"SELECT {COLS} FROM proposal WHERE id = %s AND business_id = %s", (pid, business_id)).fetchone()
    return _json(r) if r else None


def list_open(conn: psycopg.Connection, business_id: str, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        f"SELECT {COLS} FROM proposal WHERE business_id = %s AND status = 'proposed' AND expires_at > now() "
        f"ORDER BY created_at DESC LIMIT %s",
        (business_id, limit),
    ).fetchall()
    return [_json(r) for r in rows]


def propose_plan(conn: psycopg.Connection, business_id: str, plan_id: str, *, created_by: str = "assistant") -> dict:
    """The approve_plan proposal for a draft plan (the assistant's approve_plan and the owner's page share it);
    ToolError when the plan is unknown or not a draft -- IN_REVIEW_REFUSAL for a plan already in review (L1 review
    I1: it needs neither creating nor revising, only waiting). Verification is the caller's check."""
    plan = plans.get(conn, business_id, plan_id)
    if plan is None:
        raise ToolError("no plan with that id for this business")
    if plan["status"] == plans.IN_REVIEW:
        raise ToolError(IN_REVIEW_REFUSAL)
    if plan["status"] != "draft":
        raise ToolError(f"this plan is {plan['status']}; create or revise a plan first")
    lines = [plan["lines"][k] for k in plans.SECTION_ORDER]
    return create(
        conn,
        business_id,
        cls="spend",
        kind="approve_plan",
        payload={"plan_id": plan_id},
        summary=f"Launch plan v{plan['version']}: {lines[2]}",
        what_changes=lines,
        approval_url=plans.plan_url(plan_id),
        created_by=created_by,
    )


def open_for_plan(conn: psycopg.Connection, business_id: str, plan_id: str) -> dict | None:
    """The newest live approve_plan proposal for a plan, or None."""
    r = conn.execute(
        f"SELECT {COLS} FROM proposal WHERE business_id = %s AND kind = 'approve_plan' AND status = 'proposed' "
        "AND expires_at > now() AND payload->>'plan_id' = %s ORDER BY created_at DESC LIMIT 1",
        (business_id, plan_id),
    ).fetchone()
    return _json(r) if r else None


def open_for_publish_token(conn: psycopg.Connection, business_id: str, token: str) -> dict | None:
    """The newest live publish_page proposal naming this token, or None (H40: publish_page's own
    idempotency -- a second call with the same still-unconfirmed token reuses this rather than proposing
    a duplicate, the way open_for_plan is for approve_plan)."""
    r = conn.execute(
        f"SELECT {COLS} FROM proposal WHERE business_id = %s AND kind = 'publish_page' AND status = 'proposed' "
        "AND expires_at > now() AND payload->>'token' = %s ORDER BY created_at DESC LIMIT 1",
        (business_id, token),
    ).fetchone()
    return _json(r) if r else None


def open_for_publish_version(conn: psycopg.Connection, business_id: str, version: int) -> dict | None:
    """The newest live publish_page proposal that republishes this version, or None. A version republish proposes
    afresh every time (H7), so several may be open: the public surface confirms the one for the version the call
    names (spec 2026-10-01 §4.4)."""
    r = conn.execute(
        f"SELECT {COLS} FROM proposal WHERE business_id = %s AND kind = 'publish_page' AND status = 'proposed' "
        "AND expires_at > now() AND payload->>'version' = %s ORDER BY created_at DESC LIMIT 1",
        (business_id, str(version)),
    ).fetchone()
    return _json(r) if r else None


def issue_code(conn: psycopg.Connection, pid: str, *, via: str) -> str:
    """A fresh single-use code for a proposed spend proposal (earlier unused codes stay valid)."""
    r = conn.execute("SELECT class, status FROM proposal WHERE id = %s", (pid,)).fetchone()
    if r is None:
        raise ValueError("no such proposal")
    if r["class"] != "spend":
        raise ValueError("only spend-class proposals take an approval code")
    if r["status"] != "proposed":
        raise ValueError(f"proposal is {r['status']}")
    code = new_code()
    conn.execute(
        "INSERT INTO approval_code (proposal_id, code_hash, issued_via) VALUES (%s, %s, %s)",
        (pid, code_hash(pid, code), via),
    )
    return code


def _check_attempts(conn: psycopg.Connection, business_id: str, pid: str) -> None:
    """Only this business's wrong-code failures count (another business naming the id, no-code
    calls and other refusals never lock a proposal); the business index serves the query."""
    n = conn.execute(
        "SELECT count(*) AS c FROM agent_action WHERE business_id = %s AND tool = 'confirm_proposal' "
        "AND NOT ok AND args_redacted->>'proposal_id' = %s AND result->>'error' = %s "
        "AND ts > now() - interval '1 hour'",
        (business_id, pid, WRONG_CODE),
    ).fetchone()["c"]
    if n >= CODE_ATTEMPTS_PER_HOUR:
        raise ToolError(LOCKED)


def _check_daily_cap(conn: psycopg.Connection, business_id: str) -> None:
    row = conn.execute(
        "SELECT (operator_flags->>'daily_confirm_cap')::int AS cap FROM business WHERE id = %s", (business_id,)
    ).fetchone()
    cap = row["cap"] if row and row["cap"] is not None else DAILY_CONFIRM_CAP
    n = conn.execute(
        "SELECT count(*) AS c FROM proposal WHERE business_id = %s AND class = 'nonspend' "
        "AND status = 'confirmed' AND resolved_at > now() - interval '1 day'",
        (business_id,),
    ).fetchone()["c"]
    # a page's publish or take-down cannot be approved on the plan page: tomorrow is the way (open item A5)
    if n >= cap:
        raise ToolError(DAILY_LIMIT.format(cap=cap))


def precheck_approve_plan(conn: psycopg.Connection, business_id: str, payload: dict) -> None:
    """Runs before a code is consumed: a stale proposal must never burn the owner's code -- nor one whose ads could
    not launch (A3 P5, per channel: `ads_run.precheck`'s owner sentence refuses it, over the funded channels whose
    execution switch is not `off`; with every one off nothing is checked). A channel that is not the plan's primary
    and fails only its own rules never refuses it while another channel can launch (M1 Task 5b, R17; D77): its row is
    parked by `apply_approve_plan` (`ads_run.precheck_approval`). A plan Admiry's review already sent back refuses
    too (L1 review R15 b): IN_REVIEW_REFUSAL while it is in review, and -- unchanged until a new version replaces it
    -- the admin's own note once it was REJECTED (never RETURNED, which does not block: launch_check.items). Every
    yes path (the plan page, a code, a payment, the assistant) runs this check, so none can slip a rejected version
    back into review unchanged."""
    plan = plans.get(conn, business_id, payload["plan_id"])
    if plan is None:
        raise ToolError("the plan in this proposal no longer exists")
    if plan["status"] == "superseded":
        raise ToolError("this plan was revised; approve the newest version instead")
    if plan["status"] == plans.IN_REVIEW:
        raise ToolError(IN_REVIEW_REFUSAL)
    from . import launch_check  # proposals -> launch_check -> blockers -> plan_review -> proposals: deferred here

    said = launch_review.note_for(conn, business_id, plan["id"])
    if said and said["state"] == "rejected":
        raise ToolError(f"{launch_check.note_text(said['note'])} {launch_check.NOTE_SUGGESTION}")
    if plan["status"] == "approved":
        raise ToolError("this plan is already approved")
    settings = config.load()
    on = [
        ch for ch in ads_run.funded_channels(plan) if registry.exec_mode_for(conn, settings, business_id, ch) != "off"
    ]
    if on:
        ads_run.precheck_approval(conn, settings, business_id, plan, channels=on)


def launch_refusal(conn: psycopg.Connection, business_id: str, plan_id: str) -> str | None:
    """`precheck_approve_plan` (what a launch runs before the owner's code is used) asked of the plan now, as a read:
    it writes nothing, running inside a transaction that is rolled back (an adapter may open its account row while it
    is asked, as `state._readiness` reads it). None when the check lets the plan through, else its refusal, the
    sentence the launch would answer with. MarketingState's `blocked` (tools_state._setup) and the setup card's
    second look (plan_review.review, S0) read it."""
    # The check fetches no page today: the approval asks it with fetch=False, which keeps every place but the primary
    # from checking a stale page report again, and the primary (Google Search) has no page rules of its own, so its
    # saved report is never stale. A page rule on the primary would make this read fetch the page inside the caller's
    # connection, as the approval path itself would inside its own transaction (M1's exposure).
    try:
        with conn.transaction() as tx:
            precheck_approve_plan(conn, business_id, {"plan_id": plan_id})
            raise psycopg.Rollback(tx)
    except ToolError as e:
        return str(e)
    return None


def approve_on_web(
    conn: psycopg.Connection, business_id: str, plan_id: str, *, email: str, ip: str | None, kind: str = "web"
) -> dict:
    """The signed-in owner's own click approving a plan -- on `/plan/{id}` (pages.plan_approve) or the setup card
    (setup.approve, S0): that click is the out-of-band approval, so the plan's proposal (created here when the
    assistant has not) is confirmed without a code, in one transaction with its audit row (`web.approve_plan`,
    approval kind `kind`). `kind` is "paid" for Agree and pay's own approval (setup.approve_paid, L1 §5); every other
    caller takes the default, "web". ToolError with the owner's sentence when the check refuses it; the caller
    launches what was queued after the commit (`ads_run.launch_now`). Returns `confirm`'s answer."""
    with conn.transaction():
        prop = open_for_plan(conn, business_id, plan_id) or propose_plan(conn, business_id, plan_id, created_by="web")
        out = confirm(conn, business_id, prop["proposal_id"], code=None, web_owner=True, web_kind=kind)
        audit.record_web(
            conn,
            tool="web.approve_plan",
            email=email,
            ip=ip,
            business_id=business_id,
            args={"plan_id": plan_id, "proposal_id": prop["proposal_id"]},
            result=out,
            approval_kind=kind,
            approval_ref=prop["proposal_id"],
        )
    return out


def apply_approve_plan(conn: psycopg.Connection, business_id: str, payload: dict) -> dict:
    """The approval's own transaction (spec 2026-09-28 A3 §5 step 1; spec 2026-09-29 §5.3 step 1, D77): the plan
    approved (a draft, or a version in Admiry's review: the Reviewer's pass approves it here, L1), the one before it
    superseded, and one `channel_campaign` row per funded channel of the version that has an adapter and its
    execution switch on -- `queued` when the channel's account is ready
    (`ads_run.account_status`), else `waiting_account` with the reason; `waiting_creative` for a channel still
    waiting for its image. A channel that is not the plan's primary and fails its own rules (M1 Task 5b, R17) is
    `queued` with that rule's sentence (`ads_run.precheck`'s `park`), checked again every hour. `execution[channel]`
    says which, or `off` for a channel whose switch is off (P5: nothing queued there). A funded channel no adapter runs
    is left out, with a log line. `execution.ads` mirrors the Google Search entry for one release (plan.html,
    plans.summary() and the connector docs read it). The confirming request launches what was queued after the commit
    (P6). Approving the plan approves the picture its card shows for a channel that shows one with the ad (M1 Task 7,
    P7): exactly the one pinned on the version when it was made (`plans.approved_creative`; fix round 1, item 4) goes
    on it as `sections.creative.approved_image` before its rows are queued, where `ads_run.image_ready` reads it --
    or none when that picture left Admiry's set, never another in its place, and that channel waits in
    `waiting_creative` for a version showing a new one; ads already running there keep the picture they carry (final
    review I3), so their row stays as it is and its execution reads `queued`."""
    plan = plans.get(conn, business_id, payload["plan_id"])
    creative = plans.approved_creative(plan["sections"])
    if creative is not None:
        plan["sections"]["creative"] = creative
    conn.execute(
        "UPDATE plan SET status = %s WHERE business_id = %s AND status = %s AND id <> %s",
        (plans.SUPERSEDED, business_id, plans.APPROVED, plan["id"]),
    )
    settings = config.load()
    execution = {s: "queued_for_operator" for s in EXECUTABLE} | {"optimization": "on"}
    queued: dict[str, tuple[bool, str | None, bool]] = {}  # channel -> (account ready, its reason, image approved)
    for channel in ads_run.funded_channels(plan):
        if not registry.known(channel):
            log.info("approve_plan %s: %s is funded but has no adapter; not queued", plan["id"], channel)
            continue
        if registry.exec_mode_for(conn, settings, business_id, channel) == "off":
            execution[channel] = "off"
            continue
        status = ads_run.account_status(conn, settings, business_id, channel)
        # ads already running keep the picture they carry: only ads not built yet wait for one (final review I3)
        image = ads_run.image_ready(plan, channel) or ads_run.has_ads(conn, business_id, channel)
        queued[channel] = (status.ready, status.reason, image)
        execution[channel] = "waiting_account" if not status.ready else "queued" if image else "waiting_creative"
    execution["ads"] = execution.get(GOOGLE, "off")
    # R17: the sentence of each secondary channel's own rule this version fails, for its row (the precheck above let
    # the approval through on the same rows, in this transaction; it fetches no page -- a stale page report parks the
    # channel until its own step checks it again: final review Minor 1)
    parked = ads_run.precheck_approval(conn, settings, business_id, plan, channels=list(queued)) if queued else {}
    merged = "json || %s" if creative is None else "jsonb_set(json || %s, '{sections,creative}', %s)"
    cur = conn.execute(
        f"UPDATE plan SET status = %s, json = {merged} WHERE id = %s AND status = ANY(%s)",
        (
            plans.APPROVED,
            Jsonb({"execution": execution}),
            *(() if creative is None else (Jsonb(creative),)),
            plan["id"],
            [plans.DRAFT, plans.IN_REVIEW],
        ),
    )
    if cur.rowcount == 0:  # revised or approved by a concurrent call since the precheck: roll everything back
        raise ToolError("this plan changed while confirming; ask for a fresh proposal")
    for channel, (ready, reason, image) in queued.items():
        ads_run.queue(
            conn,
            business_id,
            channel,
            plan["id"],
            ready=ready,
            reason=reason,
            image=image,
            parked=parked.get(channel),
        )
    return {"plan_id": plan["id"], "plan_status": plans.APPROVED, "execution": execution}


HOSTING_OFF = "Hosting is not set up on this server"  # H1, word for word
# H22 review: named so home_api.py's web path (H40) can match it by reference and answer in the homepage's own
# words ("build the page again") without duplicating the literal sentence.
PREVIEW_UNKNOWN = "This preview has expired or is unknown; make a new preview."


def _hosted_page_view(page: dict) -> dict:
    """publish_page/unpublish_page's own page view (tools_hosting.py's after-commit hooks build their `say`
    from it): the fields set_landing_page/check_landing_page never had a reason to carry."""
    return {
        "id": page["id"],
        "url": page["url"],
        "hostname": page.get("hostname"),
        "source": page.get("source"),
        "status": page["status"],
        "version": page.get("version"),
    }


def _publish_source(
    conn: psycopg.Connection, business_id: str, payload: dict
) -> tuple[str, str, str | None, str, dict | None]:
    """(page_id, html, title, source, sections) a publish_page proposal's payload names: a still-valid preview
    token whose own page belongs to this business (H22 review: a leaked token must read exactly like an expired or
    unknown one, never "yes, but it's someone else's"), or an existing version on the business's own page row -- H25
    (round 2): the business has exactly one page row, ever, whatever `source` it currently reads, so this is
    simply `landing.current` (no more hunting a `source IN ('hosted', 'admiry')` row that a detour through the
    owner's own address could leave behind -- there is no second row left behind to hunt for; `preview_page`
    resolves to this identical row too, via `ensure_page_for_kit`). ToolError (one sentence) otherwise --
    checked here by both precheck_publish_page (before a code could be consumed for spend-class proposals, and
    before the daily cap is spent for this nonspend one) and apply_publish_page (state can move between the
    two: above all, a preview token is good for hosted.PREVIEW_TTL, far shorter than a proposal's own
    NONSPEND_TTL). `source` (H7) is 'admiry' for a preview Admiry wrote (create_page/update_page/revise_page: it
    carries the `sections` it was built from) and 'hosted' for an assistant's own HTML; a version keeps the source
    it was published with, and carries no sections (the page row keeps the ones it has)."""
    token, version = payload.get("token"), payload.get("version")
    if token:
        prev = hosted.preview(conn, token)
        if prev is not None:
            owner = conn.execute("SELECT business_id FROM page WHERE id = %s", (prev["page_id"],)).fetchone()
            if owner is None or owner["business_id"] != business_id:
                prev = None
        if prev is None:
            raise ToolError(PREVIEW_UNKNOWN)
        sections = prev.get("sections")
        return prev["page_id"], prev["html"], prev["title"], "admiry" if sections else "hosted", sections
    page = landing.current(conn, business_id)
    if page is None:
        raise ToolError("There is no Admiry-hosted page yet; make a preview first.")
    row = conn.execute(
        "SELECT html, title, source FROM page_version WHERE page_id = %s AND version = %s", (page["id"], version)
    ).fetchone()
    if row is None:
        raise ToolError(f"There is no version {version} for this page.")
    return page["id"], row["html"], row["title"], row["source"], None


def _bundle_files(conn: psycopg.Connection, page_id: str, payload: dict) -> dict[str, str] | None:
    """A3p-3 B10: the files (path -> SHA-256) a publish names when it is a site bundle's -- a bundle preview's token,
    or a version of kind bundle -- else None (one page). Read after `_publish_source`, which already refused a token
    or a version that does not resolve."""
    token, version = payload.get("token"), payload.get("version")
    if token:
        prev = hosted.preview(conn, token)
        return bundles.preview_files(conn, token) if prev is not None and prev["kind"] == "bundle" else None
    row = conn.execute(
        "SELECT kind FROM page_version WHERE page_id = %s AND version = %s", (page_id, version)
    ).fetchone()
    return bundles.version_files(conn, page_id, version) if row is not None and row["kind"] == "bundle" else None


def _already_published_url(
    conn: psycopg.Connection,
    business_id: str,
    page_id: str,
    html: str,
    token: str | None,
    files: dict[str, str] | None = None,
) -> str | None:
    """H40/H41: the live URL if publishing `token`'s content right now would change nothing -- the page's
    *current* live version (`page.version`, H7's "the row keeps the live one") is byte-identical to what
    publishing it now would produce (`hosted.inject`'s snippet is idempotent either way, so this is a fair
    comparison even though `html` itself never carries one). Scoped to that one live version, never any
    older one: H41 review -- publish A (v1), then B (v2, live), then A's exact bytes again (a brand-new
    token, or token A itself re-submitted within its still-live 24 h preview window) is a real republish --
    a new version -- because the live page is B's now, not A's; content decides, not history. For the same
    reason there is no confirmed-proposal shortcut at all any more (H40 had one; H41 dropped it): a token's
    own proposal having been confirmed once, long since superseded by a later publish, says nothing about
    whether ITS content is what is live now. None when there is nothing to compare (the page has never
    published at all) or when `token` is falsy: this is scoped to the token path only -- `publish_page
    (version=n)` stays the deliberate, repeatable rollback H7 already documents, so it is never
    short-circuited here. Read by both `_propose_publish` (tools_hosting.py, before a second proposal is
    ever created) and `precheck_publish_page` just below (the rare race: two open proposals for the same
    token, the first already confirmed by the time the second one is).

    An industry site Admiry built (A3p-6: the preview's sections say `kind: site`) goes live drawn again, not as the
    preview's files, so what publishing it now would produce is `build.live` at the address it has: the page's live
    version holds exactly those files (name and hash) or it does not.

    Nothing is live unless the page is published (A3p-6 batch 2 final review I1): a page taken down keeps its last
    version, which a rebuild of a site draws file for file, but its address answers 410 -- so that is a publish, never
    "already published"."""
    if not token:
        return None
    page = landing.get(conn, page_id)
    if page is None or not page.get("hostname") or page.get("status") != "published":
        return None
    settings = config.load()
    sections = (hosted.preview(conn, token) or {}).get("sections") or {}
    if files is not None and sections.get("kind") == "site":
        from .sites import build  # it reaches this module through landing and hosting: read when asked

        would = build.live(conn, settings, business_id, page_id, sections, hostname=page["hostname"]).named()
        return (
            f"https://{page['hostname']}/" if bundles.version_files(conn, page_id, page["version"]) == would else None
        )
    tag = landing.google_tag(conn, business_id)
    snippet = landing.snippet(page_id, settings.public_base_url, tag)
    injected = hosted.inject(html, snippet=snippet, noindex=False)
    same = conn.execute(
        "SELECT 1 FROM page_version WHERE page_id = %s AND version = %s AND html = %s",
        (page_id, page["version"], injected),
    ).fetchone()
    return f"https://{page['hostname']}/" if same else None


def precheck_publish_page(conn: psycopg.Connection, business_id: str, payload: dict) -> None:
    """H8: hosting must still be on, the business active, a plan on file (the check apply_publish_page runs
    needs get_page_kit's own kit), the token or version this proposal named must still resolve to this
    business's own page (H22), and -- for a first-ever hosted publish, `payload["hostname"]` the address
    publish_page reserved -- that address must still be the one `hosted.hostname_for` would hand out now
    (another business could have taken the slug between propose and confirm). All re-checked here, not just
    when publish_page proposed it: a preview token can expire (24 h) well before this nonspend proposal does
    (NONSPEND_TTL, 3 days). H40: also refuses a token that got published by a *different* proposal while
    this one sat open (the race _propose_publish's own check cannot fully close) -- apply_publish_page must
    never run twice for the same content."""
    settings = config.load()
    if not settings.pages_domain:
        raise ToolError(HOSTING_OFF)
    biz_row = business.row(conn, business_id)
    if biz_row is None or biz_row["status"] != "active":
        raise ToolError("This business is not active.")
    plan = plans.latest(conn, business_id)
    if plan is None or "consultation" not in plan["sections"]:
        raise ToolError(landing.NO_PLAN)
    page_id, html, _title, _source, _sections = _publish_source(conn, business_id, payload)
    if payload.get("token"):
        from .sites import build  # it reaches this module through landing and hosting: read when asked

        held = build.publish_gate(conn, hosted.preview(conn, payload["token"]))
        if held:  # A3p-6 Q11: an industry site waits for its logo and key photo, and for a preview that shows them
            raise ToolError(held)
    files = _bundle_files(conn, page_id, payload)
    live = _already_published_url(conn, business_id, page_id, html, payload.get("token"), files)
    if live:
        raise ToolError(f"This page is already published at {live}.")
    # A3p-3 P17 (plan Q8): a site taken down for the hosting minimum, or whose countdown has ended, comes back only
    # with the ads ready at $200 a month (or covered, or exempt)
    hosting_minimum.precheck_publish(conn, settings, business_id, landing.get(conn, page_id))
    reserved = payload.get("hostname")
    if reserved:
        knowledge = business.latest_knowledge(conn, business_id)
        name = plans.name_for(knowledge[1] if knowledge else {})
        if hosted.hostname_for(conn, business_id, name, settings.pages_domain) != reserved:
            raise ToolError(f"The address {reserved} is no longer free; ask again")


def apply_publish_page(conn: psycopg.Connection, business_id: str, payload: dict) -> dict:
    """H7/H8's writes, inside confirm()'s own transaction (database only -- the check, the report e-mail and
    ads_run.launch_now/resume are tools_hosting.py's after-commit job, run from confirm_proposal once this
    returns): sanitise the chosen HTML again (own_host is now known, once this is not the first publish),
    inject the tracking snippet (idempotently -- already there on a version this republishes), a new
    page_version, and page.hostname/url/source/status/html/version/check_json/checked_at on the business's one
    page row (H25: `_publish_source` already resolved `page_id` to it -- there is no other row left to
    unpublish first; the old check is cleared with the rest so a crash before the after-commit check can never
    leave the previous address's report sitting under the new one). The hostname is the one `publish_page`
    reserved in `payload` -- precheck_publish_page just confirmed it is still free -- not recomputed here; a
    payload from before that reservation existed falls back to `hosted.hostname_for`. The version's and the row's
    `source` is `_publish_source`'s (H7: 'admiry' for a page Admiry wrote), and an Admiry-written preview's
    `sections` become the row's own (A3p-2 Task 5), so update_page/revise_page start from the page now live.

    `"ads_by_channel"` (H22 review; M0 Task 9 D12, one entry per channel whose ads follow the page and are built)
    is a reply-only classification, judged after `url_changed` below: `"paused"` (the row is paused -- an owner's
    pause is never treated as a reason to move anything), `"off"` (that channel's switch is off), `"unchanged"`
    (built, live, but the address did not move), or `"moved"` (built, live, the switch is on, and the address
    moved). `"ads"` is Google Search's entry for one release (A3p-2's single-channel key; `"not built"` without
    one). H26 (round 2): whether `resources.url_stale` gets set follows `url_changed` alone, not this
    classification -- a paused or switched-off row's ads still need to catch up to the new address once they are
    live again, so the flag is set whatever the state or the switch -- `ads_run.flag_moved_page` is the write,
    shared with `tools_pages.set_landing_page` (H27, round 3) rather than copied. `"resume"` names the channels
    whose ads Admiry paused for their page (`ads_run.page_pause`: `page_down` or `unreachable`), with that reason
    -- a publish makes a page live again (H23/H29/H30), so it ends that pause; never an owner's pause, and never a
    pause of Admiry's for money (M0 Task 9 D2: a month cap, a missing balance or a dispute stays).
    tools_hosting.py's after-commit hook is what actually calls `ads_run.resume` for each, never this function (no
    network from inside a transaction).

    A site bundle (A3p-3 B10: a bundle preview's token, or a bundle version) becomes a bundle version with the same
    files, injected with nothing -- its pages carry Admiry's script tag themselves (P30) -- and not checked again: its
    files are content-addressed and were checked for this page when they were uploaded, with no host or this one.
    `page.html` is its home page, as for every reader of the live page's HTML.

    An industry site Admiry built (A3p-6 batch 2: the preview's sections say `kind: site`) is the exception, and only
    it -- an assistant's bundle and a starter page are as above: its preview is for looking at (a label on every stock
    photo, stock shown as the business's own work, no address to be canonical for), so the version is made from the
    same document drawn again for live (`build.live`: the preview's stock photos, this host, the kit's snippet as it is
    now, the static checks again), its files stored here -- database and CPU only, so inside this transaction -- and the
    version's `html` and title are that home page's. The preview's screenshots are still copied (`copy_shots`): they
    are of the preview's drawing, which differs from the live one only by those labels, the stock-only sections and the
    canonical links. A template fault is answered as `build.PUBLISH_FAILED` and publishes nothing."""
    page_id, html, title, source, sections = _publish_source(conn, business_id, payload)
    page = landing.get(conn, page_id)
    settings = config.load()
    agent_base = settings.public_base_url
    files = _bundle_files(conn, page_id, payload)
    if files is None:
        problems = hosted.sanitise(html, page_id=page_id, own_host=page["hostname"], agent_base=agent_base)
        if problems:
            raise ToolError(" ".join(problems))
    hostname = payload.get("hostname")
    if not hostname:  # defensive: every payload publish_page builds now reserves one
        knowledge = business.latest_knowledge(conn, business_id)
        name = plans.name_for(knowledge[1] if knowledge else {})
        hostname = hosted.hostname_for(conn, business_id, name, settings.pages_domain)
    if files is None:
        tag = landing.google_tag(conn, business_id)
        snippet = landing.snippet(page_id, agent_base, tag)
        injected = hosted.inject(html, snippet=snippet, noindex=False)
        version = hosted.new_version(conn, page_id, injected, title, source=source)
    else:
        if (sections or {}).get("kind") == "site":
            from .sites import build  # it reaches this module through landing and hosting: read when asked

            drawn = build.live(conn, settings, business_id, page_id, sections, hostname=hostname)
            files = {**bundles.store(conn, drawn.files), **drawn.refs}
            html, title = drawn.home, drawn.title
        injected = html
        bundles.touch(conn, files)  # the collection never takes a file a version is about to name
        version = hosted.new_version(conn, page_id, html, title, source=source, kind="bundle")
        bundles.add_version_files(conn, page_id, version, files)
        bundles.copy_shots(conn, page_id, version, payload)  # A3p-3 batch 4: the render check's screenshots
    url = f"https://{hostname}/"
    built = ads_run.page_rows(conn, business_id)
    url_changed = ads_run.flag_moved_page(conn, business_id, page["url"], url)
    by_channel = {r["channel"]: _ads_state(settings, r, url_changed) for r in built}
    resume = {r["channel"]: ads_run.page_pause(r) for r in built if ads_run.page_pause(r)}
    conn.execute(
        "UPDATE page SET hostname = %s, url = %s, source = %s, status = 'published', html = %s, "
        "version = %s, sections = COALESCE(%s, sections), check_json = NULL, checked_at = NULL, "
        "render_json = NULL, rendered_at = NULL WHERE id = %s",
        (hostname, url, source, injected, version, Jsonb(sections) if sections is not None else None, page_id),
    )
    # A3p-3 (plan Q4, Q8): the site is back (the takedown fields cleared); a countdown keeps running through a
    # republish, and one starts here when the site is not covered -- the publish request named its date
    hosting_minimum.on_publish(conn, business_id, page_id)
    return {
        "page": _hosted_page_view(landing.get(conn, page_id)),
        "ads": by_channel.get(GOOGLE, "not built"),
        "ads_by_channel": by_channel,
        "resume": resume,
    }


def _ads_state(settings: config.Settings, row: dict, url_changed: bool) -> str:
    """apply_publish_page's reply-only word for one channel's built ads (see there)."""
    if row["state"] == "paused":
        return "paused"
    if registry.exec_mode(settings, row["channel"]) == "off":
        return "off"
    return "moved" if url_changed else "unchanged"


def precheck_unpublish_page(conn: psycopg.Connection, business_id: str, payload: dict) -> None:
    page = landing.current(conn, business_id)
    if page is None or page["status"] != "published" or page.get("source") not in ("hosted", "admiry"):
        raise ToolError("There is no published Admiry-hosted page to take down.")


def apply_unpublish_page(conn: psycopg.Connection, business_id: str, payload: dict) -> dict:
    """H11's write: `status = 'unpublished'` (the hostname answers 410 from then on; the version history and
    the hostname itself stay -- publish_page(version=n) brings it back). H23: the page goes down in this same
    transaction; the pause -- tools_hosting.py's after-commit hook, since a pause calls the platform when the switch
    is live -- follows in the same request (or is left owed if the platform cannot be reached). `"pause"` names the
    channels it pauses: every channel whose ads follow the page, are built and carry this page's hosted address
    (`ads_run.on_host`) -- paused with `page_down` even when the owner's own pause already stands (M0 Task 9 D5,
    replacing H24's "left as it is"): both pauses stand, so the owner's resume can never put the ads back on a page
    that answers "gone". A pause the hourly page-check made (the page stopped answering) becomes Admiry's page-down
    pause here (`ads_run.mark_page_down`, A3p-2 Task 4 review): the page is gone now, page-check never fetches an
    unpublished page, and the status line must not promise the ads come back once it opens again."""
    page = landing.current(conn, business_id)
    if page is None or page["status"] != "published" or page.get("source") not in ("hosted", "admiry"):
        raise ToolError("There is no published Admiry-hosted page to take down.")
    conn.execute("UPDATE page SET status = 'unpublished' WHERE id = %s", (page["id"],))
    hosting_minimum.on_unpublish(conn, page["id"])  # A3p-3: the owner's own takedown, its files kept (plan Q7)
    ads_run.mark_page_down(conn, business_id)
    pause = [r["channel"] for r in ads_run.on_host(conn, business_id, page.get("hostname"))]
    return {"page": _hosted_page_view(landing.get(conn, page["id"])), "pause": pause}


PRECHECK = {
    "approve_plan": precheck_approve_plan,
    "publish_page": precheck_publish_page,
    "unpublish_page": precheck_unpublish_page,
}
APPLY = {
    "approve_plan": apply_approve_plan,
    "publish_page": apply_publish_page,
    "unpublish_page": apply_unpublish_page,
}


def _apply(conn: psycopg.Connection, business_id: str, r: dict, approval: dict) -> dict:
    """The proposal's own change -- but a business's first launch while AGENT_LAUNCH_REVIEW is on (L1, spec 2026-10-10 §5,
    plan D5, D6): the owner's yes, whichever way it came (the plan card, the plan page, a code, a payment), sends the
    plan to Admiry's review (launch_review.submit), whose pass approves it (roles/reviewer.py). The cheap checks
    first (L1 review M6): `ads_run.approved`'s read is spent only for an approve_plan proposal with the switch on."""
    if r["kind"] == "approve_plan" and config.load().launch_review and ads_run.approved(conn, business_id) is None:
        return launch_review.submit(conn, business_id, r["payload"]["plan_id"], approval=approval)
    return APPLY[r["kind"]](conn, business_id, r["payload"])


def confirm(
    conn: psycopg.Connection,
    business_id: str,
    pid: str,
    *,
    code: str | None,
    web_owner: bool = False,
    web_kind: str = "web",
) -> dict:
    """Carry out a proposed change; ToolError with an owner-readable reason otherwise. The change
    is checked (PRECHECK) before a code is consumed, so a stale proposal never burns a code.
    `web_owner`: the signed-in owner clicked Approve on /plan/{id} — that click is the out-of-band
    approval, so a spend proposal needs no code (approval kind `web_kind`, "web" unless the caller
    names another -- "paid" for Agree and pay's own approval, L1 §5)."""
    r = conn.execute(
        "SELECT id, class, kind, payload, status, expires_at < now() AS expired FROM proposal "
        "WHERE id = %s AND business_id = %s FOR UPDATE",
        (pid, business_id),
    ).fetchone()
    if r is None:
        raise ToolError("no proposal with that id for this business")
    if r["status"] != "proposed":
        raise ToolError(f"this proposal is already {r['status']}")
    if r["expired"]:
        raise ToolError("this proposal expired; ask for a new one")
    PRECHECK.get(r["kind"], lambda *_: None)(conn, business_id, r["payload"])
    if r["class"] == "spend" and web_owner:
        approval = {"kind": web_kind, "ref": pid}
    elif r["class"] == "spend":
        if not code:
            raise ToolError(
                "this change spends money: it needs the approval code from the owner's plan page (approval_url)"
            )
        _check_attempts(conn, business_id, pid)
        used = conn.execute(
            "UPDATE approval_code SET used_at = now() WHERE proposal_id = %s AND code_hash = %s "
            "AND used_at IS NULL RETURNING proposal_id",
            (pid, code_hash(pid, code)),
        ).fetchone()
        if used is None:
            raise ToolError(WRONG_CODE)
        approval = {"kind": "code", "ref": pid}
    else:
        _check_daily_cap(conn, business_id)
        approval = {"kind": "assistant", "ref": pid}
    effect = _apply(conn, business_id, r, approval)
    conn.execute("UPDATE proposal SET status = 'confirmed', resolved_at = now() WHERE id = %s", (pid,))
    # "kind" last: tools_proposals.confirm_proposal's after-commit dispatch (publish_page/unpublish_page) reads
    # it to pick the hook, and it must be this proposal's real kind whatever an APPLY effect dict happens to hold.
    return {"status": "confirmed", "proposal_id": pid, **effect, "kind": r["kind"], "approval": approval}


def decline(conn: psycopg.Connection, business_id: str, pid: str, *, reason: str | None) -> dict:
    r = conn.execute(
        "UPDATE proposal SET status = 'declined', resolved_at = now(), payload = payload || %s "
        "WHERE id = %s AND business_id = %s AND status = 'proposed' RETURNING id",
        (Jsonb({"decline_reason": (reason or "")[:DECLINE_REASON_MAX]}), pid, business_id),
    ).fetchone()
    if r is None:
        raise ToolError("no open proposal with that id for this business")
    return {"status": "declined", "proposal_id": pid}
