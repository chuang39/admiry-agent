"""The "Your ads" page (spec 2026-10-06-admiry-your-ads-page-design.md): where the owner's ads run, what they spent
and brought, the ads themselves, and the four things the owner can change -- a platform's amount a month, the area's
radius, the words, pause / resume. Templates over the read model (channels spec D102) and POSTs over `plan_edit`,
the existing pause / resume routes (web.py) and the existing approval (proposals.confirm with the owner's click, the
plan page's path)."""

from __future__ import annotations

import re
from types import SimpleNamespace

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastmcp.exceptions import ToolError

from . import (
    ads_refusal,
    ads_run,
    audit,
    business,
    config,
    db,
    envelope,
    funnel,
    launch_review,
    plan_edit,
    plan_words,
    plans,
    proposals,
    sessions,
    setup,
    state,
    templating,
    tools_state,
)
from .channels import registry
from .web import CONNECT_CSP, NO_STORE, area_of, owned_business, page, switch_labels, website_prefix

router = APIRouter()
EDITS_PER_HOUR = 30  # auth.MUTATIONS_PER_HOUR, the number a key's manage commands get (spec Y14)
TOO_MANY = "That is a lot of changes for one hour; try again later."
NOT_VERIFIED = "The business must be verified before your ads can change."
AMOUNT_RULE = "A whole-dollar amount between $1 and $100,000."
RADIUS_RULE = "Between 1 and 100 miles."
RAISE_IT = "You can raise it in your assistant."
FOOT = (
    "Nothing changes until you approve it. Admiry sends an approved change to the platform right away and retries "
    "within the hour if it has to."
)
NEWER = "A newer version of your plan is waiting for your approval. A change you approve here replaces it."
_AMOUNT = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})*|\d+)$")


def money(n: float) -> str:
    """ "$38" for a whole number of dollars, "$9.80" otherwise (the channels card's own rule)."""
    n = float(n)
    return f"${n:,.0f}" if n.is_integer() else f"${n:,.2f}"


def status_word(c: state.Connection) -> str:
    """A channel's account status in the owner's words (the Settings redesign's §4 item 3, moved here with the rows):
    a platform's notice first, in its own words; then who runs it, whether it is connected, an invitation on its way."""
    word = envelope.account_word(c.mode, c.status)
    if word == "needs_attention":
        return f'Needs attention: "{c.note}"' if c.note else "Needs attention"
    return {"managed": "Run by Admiry", "connected": "Connected ✓", "pending": "Invitation sent"}.get(
        word, "Not connected"
    )


def one_action(c: state.Connection, locked: str | None, prepaid: bool) -> str | None:
    """The row's one action, into the conversation's setup step (D69): add funds to a channel Admiry runs (a prepaid
    business), check an invitation, link an account, or change the way it runs -- refused while money is in or a link
    is live (`locked`), when the row offers nothing and the lock's sentence says why."""
    if c.mode == "managed":
        return "Add funds" if prepaid else None
    if c.status == "pending":
        return "Check now"
    if c.ready:
        return None if locked else "Change"
    return "Link"


def _owner_businesses(conn, email: str) -> list[SimpleNamespace]:
    """The owner's active businesses, newest first, with the switcher's labels (Settings' rule)."""
    rows = conn.execute(
        "SELECT id, created_at FROM business WHERE owner_email = %s AND status = 'active' ORDER BY created_at DESC",
        (email,),
    ).fetchall()
    out = []
    for r in rows:
        latest = business.latest_knowledge(conn, r["id"])
        known = (latest[1] if latest else None) or {}
        out.append(
            SimpleNamespace(
                id=r["id"],
                created_at=r["created_at"],
                name=plans.name_for(known) or "Your business",
                area=area_of(known),
            )
        )
    switch_labels(out)
    return out


def _numbers(r: state.ChannelRow) -> str:
    """ "$9.80 of $13.15 today · $412 of $400 this month · 2 calls, 1 form this week" (the channels card's words)."""
    parts = []
    if r.state in state.SPENDING and r.today_usd is not None and r.day_cap_usd is not None:
        parts.append(f"{money(r.today_usd)} of {money(r.day_cap_usd)} today")
    if r.month_cap_usd is not None:
        parts.append(f"{money(r.month_usd)} of {money(r.month_cap_usd)} this month")
    else:
        parts.append(f"{money(r.month_usd)} this month")
    week = [
        f"{r.calls_7d} call{'' if r.calls_7d == 1 else 's'}" if r.calls_7d else None,
        f"{r.leads_7d} form{'' if r.leads_7d == 1 else 's'}" if r.leads_7d else None,
    ]
    if any(week):
        parts.append(", ".join(w for w in week if w) + " this week")
    return " · ".join(parts)


def _running(row: state.ChannelRow | None, raw: dict | None, approved: dict, label: str) -> dict:
    """The version the platform runs and, when it trails the approved one, why (spec §2 item 4). `live` is whether it
    spends now (a live or limited row): all that "running" may say, never of a platform with no row, a pause or a wait."""
    live = row is not None and row.state in state.SPENDING
    if row is None or row.plan_version is None or row.plan_version >= approved["version"]:
        return {
            "version": approved["version"] if row is None or row.plan_version is None else row.plan_version,
            "note": None,
            "live": live,
        }
    refused = ((raw or {}).get("resources") or {}).get("refused_plan_id") == approved["id"]
    if refused:
        note = f"Version {approved['version']} was not accepted; version {row.plan_version} keeps running."
    else:
        note = (
            f"Version {approved['version']} is approved; {label} still runs version {row.plan_version} — "
            "Admiry sends it within the hour."
        )
    return {"version": row.plan_version, "note": note, "live": live}


def _platform(channel, entry, row, conn_, raw, approved, locked, prepaid, biz_id) -> dict:
    label = registry.label(channel)
    copy = plans.copy_of(entry) if entry else None
    action = one_action(conn_, locked, prepaid) if conn_ else None
    return {
        "channel": channel,
        "label": label,
        "state_text": row.state_text if row else "Not launched yet",
        "sentence": row.sentence if row else ads_run.OFF_CHANNEL.format(label=label),
        "account": status_word(conn_) if conn_ else None,
        "account_public": conn_.account_public if conn_ else None,
        "action": {"label": action, "url": f"/?setup={biz_id}"} if action else None,
        # why the way a ready account runs cannot change: setup.state's lock sentence, a live link or money paid in
        "lock": locked if conn_ and conn_.ready and locked else None,
        "note": conn_.note if conn_ and conn_.note and conn_.status != "needs_attention" else None,
        "numbers": _numbers(row) if row else None,
        # every issue on the row in the owner's words: the platform's own, or an ad it did not approve with its headline
        # and the platform's policy topics (final review I2; spec §4)
        "issues": [tools_state.issue_text(label, i) for i in (row.issues if row else [])],
        "toggle": next((a for a in (row.actions if row else []) if a in ("pause", "resume")), None),
        "running": _running(row, raw, approved, label),
        "ads": (
            {
                "headlines": list(entry.get("headlines") or []),
                "descriptions": list(entry.get("descriptions") or []),
                "headlines_text": "\n".join(entry.get("headlines") or []),
                "descriptions_text": "\n".join(entry.get("descriptions") or []),
                "themes": list(entry.get("themes") or []),
                "landing": entry.get("landing_service") or "",
                "copy_of": registry.label(copy) if copy else None,
            }
            if entry
            else None
        ),
        "budget": (
            {
                "amount": int(float(entry["monthly_usd"])),
                "monthly": money(float(entry["monthly_usd"])),
                "day": money(plan_edit.day_usd(entry["monthly_usd"])),
            }
            if entry
            else None
        ),
        "limits": plan_words.limits(channel),
    }


def _context(
    conn, settings, session: dict, selected: str | None, *, approved_flash=None, notice=None, open_form=None
) -> dict:
    """The page's context (the keys agent/your_ads_mock.fixture carries): the owner's businesses and the one shown
    (`selected` when it is the owner's, else the newest), its approved plan and, from it and the read model, the
    month line, the area, one entry per platform in card order (Q7), the later lines."""
    all_ = _owner_businesses(conn, session["email"])
    shown = next((b for b in all_ if b.id == selected), all_[0] if all_ else None)
    ctx = {
        "session": session,
        "signed_in": True,
        "initial": session["email"][:1].upper(),
        "email": session["email"],
        "csrf": session["csrf"],
        "site": website_prefix(settings),
        "business": shown,
        "businesses": all_,
        "approved": None,
        "latest_plan": None,
        "newer_draft": None,
        "newer_words": NEWER,
        "changes": [],  # spec 2026-10-11 §5.1: what Admiry removed from the approved version's ads, as lines
        "changes_intro": ads_refusal.INTRO,
        "month": None,
        "area": None,
        "platforms": [],
        "later": [],
        "approved_version": None,
        "notice": notice,
        "open_form": open_form,
        "foot": FOOT,
        "disclosure": plans.DISCLOSURE,
    }
    if shown is None:
        return ctx
    biz = shown.id
    approved = ads_run.approved(conn, biz)
    latest = plans.latest(conn, biz)
    ctx["latest_plan"] = {"id": latest["id"], "version": latest["version"]} if latest else None
    ctx["in_review"] = launch_review.owner_words(conn, biz)  # L1: where it stands while Admiry checks a first launch
    if approved is None:
        return ctx
    secs = approved["sections"]
    ctx["approved"] = {"id": approved["id"], "version": approved["version"]}
    ctx["changes"] = [ads_refusal.line(c) for c in ads_refusal.changes(conn, biz, approved["id"])]
    if latest and latest["status"] == "draft" and latest["version"] > approved["version"]:
        ctx["newer_draft"] = {"id": latest["id"], "version": latest["version"]}
    f = funnel.month(conn, biz)
    if f["spend_usd"]:
        ctx["month"] = {
            "spent": money(f["spend_usd"]),
            "cap": money(plans.total_usd(secs)),
            "visits": f["visits"],
            "leads": f["leads"],
            "calls": f["calls_counted"],
        }
    radius = ((secs.get("consultation") or {}).get("geo") or {}).get("radius_miles")
    ctx["area"] = {
        "sentence": f"within {radius:g} miles of the business" if radius else "around the business",
        "address": shown.area,
        "radius": f"{radius:g}" if radius else None,
    }
    rows = {r.channel: r for r in state.channel_rows(conn, settings, biz)}
    conns = {c.channel: c for c in state.connections(conn, settings, biz)}
    raw = {r["channel"]: r for r in ads_run.rows(conn, biz)}
    way = setup.state(conn, settings, biz)
    locked, prepaid = way["locked"], way["mode"] == "managed"
    funded = ads_run.funded_channels(approved)
    listed = [c for c in registry.ORDER if registry.known(c) and (c in funded or c in rows)]
    ctx["platforms"] = [
        _platform(c, ads_run.entry(approved, c), rows.get(c), conns.get(c), raw.get(c), approved, locked, prepaid, biz)
        for c in listed
    ]
    ctx["later"] = plans.later_rows(secs)
    if approved_flash is not None and approved_flash == approved["version"]:
        ctx["approved_version"] = approved_flash
    return ctx


def _render(ctx: dict, status: int = 200) -> HTMLResponse:
    resp = page("your_ads.html", status=status, **ctx)
    resp.headers["Content-Security-Policy"] = CONNECT_CSP
    return resp


@router.get("/ads", response_class=HTMLResponse)
def your_ads(request: Request, b: str | None = None, approved: str | None = None) -> HTMLResponse:
    """The page. `?b=<id>` picks the business (the owner's only); `?approved=<version>` shows "Version N approved."
    after a change -- read as text and ignored unless it is the approved version's number (Q5)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return page("signin.html", next=sessions.safe_next("/ads"), configured=bool(settings.google_client_id))
        # ASCII digits of a version's size only: str.isdigit() lets "²" through and int() refuses it (and a long run)
        flash = int(approved) if approved and re.fullmatch(r"[0-9]{1,9}", approved) else None
        ctx = _context(conn, settings, session, b, approved_flash=flash)
    return _render(ctx)


# ---------------------------------------------------------------- the edits (spec §3)


def _owner(conn, request: Request, business_id: str, csrf: str | None) -> tuple[dict, dict]:
    """(session, business) for a POST from the signed-in owner of a verified business: 403 without a session or with
    a bad token, 404 for anyone else's business, 409 before verification (the plan page's own rule)."""
    session = sessions.of_request(conn, request)
    sessions.check_csrf(session, csrf)
    biz = owned_business(conn, session, business_id)
    if biz["claim_state"] != "verified":
        raise HTTPException(status_code=409, detail=NOT_VERIFIED)
    return session, biz


def _change(kind, channel, amount, radius, headlines, descriptions) -> plan_edit.Change:
    """The form's fields as a Change, or ToolError with the field's sentence (Q8: "$1,500" is an amount)."""
    if kind == "budget":
        m = _AMOUNT.match((amount or "").strip())
        try:
            value = int(m.group(1).replace(",", "")) if m else None
        except ValueError:  # int() refuses a run of digits past its 4,300-digit limit: a hand-edited post, no amount
            value = None
        if value is None or not 1 <= value <= 100_000:
            raise ToolError(AMOUNT_RULE)
        return plan_edit.Change(kind, channel=channel, amount=value)
    if kind == "area":
        try:
            miles = float((radius or "").strip())
        except ValueError:
            raise ToolError(RADIUS_RULE) from None
        if not 1 <= miles <= 100:
            raise ToolError(RADIUS_RULE)
        return plan_edit.Change(kind, radius=miles)
    if kind == "words":
        # a NUL (from a hand-edited post) is dropped, as the audit row's text drops it: jsonb refuses it in the version
        headlines, descriptions = ((t or "").replace("\x00", "") for t in (headlines, descriptions))
        return plan_edit.Change(
            kind,
            channel=channel,
            headlines=tuple(plan_words.clean_lines(headlines)),
            descriptions=tuple(plan_words.clean_lines(descriptions)),
        )
    raise ToolError("Say what to change.")


def _refusal(e: ToolError) -> str:
    """The sentence, and where to raise a limit when that is the refusal (spec §7): the refusal, written to read
    mid-sentence, gets its full stop first (the `sentence` filter's rule)."""
    s = str(e)
    return f"{templating.sentence(s)} {RAISE_IT}" if "monthly limit" in s or "ceiling" in s else s


def _lock_base(conn, biz: str, base: str) -> None:
    """The base version the page was rendered from, locked for the rest of this transaction (spec §3.3). The approval
    that gets the lock first supersedes the base before it commits; an overlapping one waits here, then finds no
    approved row with that id and is refused like any stale one with nothing written -- instead of landing on a base
    that is gone and undoing what the first wrote."""
    row = conn.execute(
        "SELECT 1 FROM plan WHERE id = %s AND business_id = %s AND status = 'approved' FOR UPDATE", (base, biz)
    ).fetchone()
    if row is None:
        raise ToolError(plan_edit.PLAN_CHANGED)


def _check_rate(conn, biz: str) -> None:
    n = conn.execute(
        "SELECT count(*) AS n FROM agent_action WHERE business_id = %s AND tool = 'web.ads_edit' "
        "AND ts > now() - interval '1 hour'",
        (biz,),
    ).fetchone()["n"]
    if n >= EDITS_PER_HOUR:
        raise ToolError(TOO_MANY)


def _review_context(
    settings, session: dict, biz: dict, base: str, change: plan_edit.Change, pv: plan_edit.Preview, fields: dict
) -> dict:
    label = registry.label(change.channel) if change.channel else None
    if change.kind == "budget":
        row = next(r for r in pv.rows if r["channel"] == change.channel)
        headline = f"{label}: {money(row['old_usd'])} → {money(row['new_usd'])} a month"
    elif change.kind == "area":
        headline = (
            f"Within {pv.radius_old:g} → {pv.radius_new:g} miles of the business"
            if pv.radius_old
            else f"Within {pv.radius_new:g} miles of the business"
        )
    else:
        headline = f"New words for {label}"
    # every kind of change shows a radius that moved (the net under plan_edit's pin, which fails open); a base that had
    # none read "any distance"
    radius = None
    if pv.radius_old != pv.radius_new:
        radius = {
            "old": f"{pv.radius_old:g}" if pv.radius_old else "any distance",
            "new": f"{pv.radius_new:g}" if pv.radius_new else "any distance",
        }
    return {
        "session": session,
        "signed_in": True,
        "initial": session["email"][:1].upper(),
        "email": session["email"],
        "csrf": session["csrf"],
        "site": website_prefix(settings),
        "business": {"id": biz["id"]},
        "headline": headline,
        "kind": change.kind,
        "channel": change.channel,
        "rows": [
            {
                "label": r["label"],
                "old": money(r["old_usd"]) if r["old_usd"] is not None else "—",
                "new": money(r["new_usd"]) if r["new_usd"] is not None else "—",
                "day": money(r["day_usd"]) if r["day_usd"] is not None else None,
                "changed": r["changed"],
            }
            for r in pv.rows
        ],
        "total": {"old": money(pv.total_old), "new": money(pv.total_new), "changed": pv.total_old != pv.total_new},
        "radius": radius,
        "words": list(pv.words.values()),
        "next_steps": pv.next_steps,
        "fields": {"base": base, **fields},
        "back": f"/ads?b={biz['id']}#{change.channel or 'area'}",
        "disclosure": plans.DISCLOSURE,
    }


def _render_review(ctx: dict) -> HTMLResponse:
    resp = page("your_ads_review.html", **ctx)
    resp.headers["Content-Security-Policy"] = CONNECT_CSP
    return resp


@router.post("/ads/review", response_class=HTMLResponse)
def review(
    request: Request,
    business_id: str = Form(...),
    csrf: str | None = Form(None),
    base: str = Form(...),
    kind: str = Form(...),
    channel: str | None = Form(None),
    amount: str | None = Form(None),
    radius: str | None = Form(None),
    headlines: str = Form(""),
    descriptions: str = Form(""),
):
    """Save: the change computed on the approved version and shown; nothing stored (spec §3.2). A refusal is the page
    again, 409, with the sentence and the form open (Q3)."""
    settings = config.load()
    fields = {
        "kind": kind,
        "channel": channel,
        "amount": amount,
        "radius": radius,
        "headlines": headlines,
        "descriptions": descriptions,
    }
    with db.connect(settings.database_url) as conn:
        session, biz = _owner(conn, request, business_id, csrf)
        try:
            change = _change(**fields)
            pv = plan_edit.preview(conn, biz["id"], base, change)
        except ToolError as e:
            return _render(
                _context(conn, settings, session, biz["id"], notice=_refusal(e), open_form=fields), status=409
            )
        ctx = _review_context(settings, session, biz, base, change, pv, fields)
    return _render_review(ctx)


@router.post("/ads/approve")
def approve(
    request: Request,
    business_id: str = Form(...),
    csrf: str | None = Form(None),
    base: str = Form(...),
    kind: str = Form(...),
    channel: str | None = Form(None),
    amount: str | None = Form(None),
    radius: str | None = Form(None),
    headlines: str = Form(""),
    descriptions: str = Form(""),
):
    """Approve: the same inputs computed again on the same base and, in one transaction, the base locked (an
    overlapping approval waits for it and is refused), the version written, proposed and confirmed with the owner's
    click (approval kind "web", the plan page's path), the audit row; then the launch in this request (`launch_now`
    never raises; the hourly job finishes what it could not) (spec §3.3)."""
    settings = config.load()
    fields = {
        "kind": kind,
        "channel": channel,
        "amount": amount,
        "radius": radius,
        "headlines": headlines,
        "descriptions": descriptions,
    }
    with db.connect(settings.database_url) as conn:
        session, biz = _owner(conn, request, business_id, csrf)
        try:
            change = _change(**fields)
            with conn.transaction():
                _lock_base(conn, biz["id"], base)
                _check_rate(conn, biz["id"])
                plan = plan_edit.commit(conn, biz["id"], base, change)
                prop = proposals.propose_plan(conn, biz["id"], plan["id"], created_by="web")
                proposals.confirm(conn, biz["id"], prop["proposal_id"], code=None, web_owner=True)
                audit.record_web(
                    conn,
                    tool="web.ads_edit",
                    email=session["email"],
                    ip=sessions.request_ip(request),
                    business_id=biz["id"],
                    args={**fields, "base": base},
                    result={"plan_id": plan["id"], "version": plan["version"], "proposal_id": prop["proposal_id"]},
                    approval_kind="web",
                    approval_ref=prop["proposal_id"],
                )
        except ToolError as e:
            return _render(
                _context(conn, settings, session, biz["id"], notice=_refusal(e), open_form=fields), status=409
            )
    ads_run.launch_now(biz["id"])
    return RedirectResponse(
        f"/ads?b={biz['id']}&approved={plan['version']}#{change.channel or 'area'}", status_code=303, headers=NO_STORE
    )
