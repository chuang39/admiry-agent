"""The owner's pages (spec §4.5 /connect, §5.4 web hand-off 1): Google sign-in, the claim, the one key page
(keys with scopes, shown once; the paste prompt; the connection light), sign-out. Sessions and CSRF come
from sessions.py; every change leaves an agent_action row as web:<email>.
Ads setup (choose, accept, link, pay) happens in the home conversation (setup.py, setup_api.py); this page is
Settings: read-only status and the account."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastmcp.exceptions import ToolError

from . import (
    ads_run,
    audit,
    bundles,
    business,
    claims,
    config,
    consult,
    db,
    geocode,
    google_ads_link,
    google_auth,
    hosted,
    keys,
    legal,
    listings,
    mail_grants,
    oauth,
    places,
    plans,
    profile,
    sessions,
    site_upload,
    support_mail,
    timefmt,
    wallet,
)
from .connect import paste_prompt
from .templating import env

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}
# Settings loads nothing from elsewhere and every form on it posts to its own origin (spec
# 2026-09-29-admiry-settings-redesign §5.4): the home's policy (pages.home_csp()), no inline script or style.
CONNECT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
KEY_LABELS = ("muse", "claude", "chatgpt")
KEY_NAMES = {"muse": "Muse", "claude": "Claude", "chatgpt": "ChatGPT"}  # what the owner reads for a label
# the browser's own key (label "draft" from the homepage, "website" once the claim upgrades it): not a "key" to the owner
WEBSITE_KEYS = ("draft", "website")
LINK_DEAD = "That sign-in link has expired or was already used. Ask your assistant for a new one."


def page(template: str, *, status: int = 200, **ctx) -> HTMLResponse:
    """Render an owner-facing page. `base` (the public base) and `help` (the Help button's switch, spec 2026-10-12
    §3.4: AGENT_SUPPORT) are in every context; _help.html is included by the pages of that spec's §3.1 alone, so the
    value is harmless on the rest."""
    settings = config.load()
    html = env.get_template(template).render({"base": settings.public_base_url, "help": settings.support, **ctx})
    return HTMLResponse(html, status_code=status, headers=NO_STORE)


def notice(message: str, *, status: int = 200, back: str = "/connect", back_text: str = "Back") -> HTMLResponse:
    return page("notice.html", status=status, message=message, back=back, back_text=back_text)


def redirect_uri(settings: config.Settings) -> str:
    return f"{settings.web_base_url}/connect/google/callback"


# The website's pages the header and the home link (Programmatic, the policies, the icon) sit next to them only once
# the edge splits admiry.ai by path (spec 2026-09-29 §4) and AGENT_WEB_BASE_URL names that host. Until then -- an
# image released ahead of the edge change (the release lane moves images, never the Caddyfile), or a box without the
# web host -- the links go to the live website instead of 404ing on the agent host.
WEBSITE = "https://admiry.ai"


def website_prefix(settings: config.Settings) -> str:
    """The prefix of the website links in the shared header and the home: "" once the edge split is live (relative
    links), else the live website. Here, not in pages.py, so Settings can use it (pages imports web, not back)."""
    return "" if settings.web_base_url != settings.public_base_url else WEBSITE


def owned_business(conn, session: dict, business_id: str) -> dict:
    """The owner's active business by id ({id, slug, claim_state, ads_mode}), or 404 (never reveals whether the id
    exists)."""
    row = conn.execute(
        "SELECT id, slug, claim_state, ads_mode FROM business WHERE id = %s AND owner_email = %s AND status = 'active'",
        (business_id, session["email"]),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such business")
    return dict(row)


LINK_WORDS = {
    "pending": "waiting for you to accept Admiry's invitation in Google Ads",
    "active": "linked",
    "manual": "linked",
    "refused": "the invitation was declined",
    "cancelled": "the invitation was withdrawn or has expired",
    "inactive": "no longer linked",
}
PAYMENT_WORDS = {"topup": "Card payment", "refund": "Refund", "adjustment": "Correction"}  # every non-spend kind


def _at(text: str | None) -> datetime | None:
    """keys.list_for_business hands its times back as ISO text (the list_keys tool's contract); Settings shows
    them through timefmt.when like every other time."""
    return datetime.fromisoformat(text) if text else None


def _day(dt: datetime) -> str:
    """A date as the rest of Settings writes one: "Sep 5, 2026" (UTC; strftime's %d would pad it to "Sep 05")."""
    dt = dt.astimezone(UTC)
    return f"{dt:%b} {dt.day}, {dt.year}"


def _text(v) -> str | None:
    """A knowledge value an assistant may have written, as text for the page: a non-blank string, stripped, else
    None. update_business checks the shape of the name (and of category_detail, website, usp and pricing_hints) but
    only the size of the rest, the areas among them, and a name stored before it checked one may be anything."""
    return v.strip() if isinstance(v, str) and v.strip() else None


def area_of(known: dict) -> str | None:
    """The city/area under the name (spec §4.1): the first area the knowledge states, else the Google listing's
    address (plans.consultation_section reads the same two). A single text is one area (not its letters), and a
    value that is not text is skipped, so an odd shape never breaks the page."""
    stated = known.get("service_area")
    stated = [stated] if isinstance(stated, str) else stated if isinstance(stated, list) else []
    listing = known.get("listing")
    return next(filter(None, map(_text, stated)), None) or (
        _text(listing.get("address")) if isinstance(listing, dict) else None
    )


def _shown_listing(listing: dict | None) -> dict | None:
    """profile.google_listing as the card shows it: its address as a sentence says it (places.display, final review B
    M7); the profile tool's data keeps the stored one."""
    return listing and {**listing, "address": places.display(listing.get("address"))}


def switch_labels(businesses: list[SimpleNamespace]) -> None:
    """The switcher tells same-named businesses apart (several unvouched ones all read "Your business"): by area,
    then by when each was started (a relative time), then, when even that reads the same (two started minutes
    apart), by a number in the order they were started. `businesses` is newest first."""
    names = [x.name + (f" · {x.area}" if x.area else "") for x in businesses]
    for x, name in zip(businesses, names, strict=True):
        x.switch_label = name if names.count(name) == 1 else f"{name} · started {timefmt.when(x.created_at)['rel']}"
    tied, nth = Counter(x.switch_label for x in businesses), Counter()
    for x in reversed(businesses):
        label = x.switch_label
        if tied[label] > 1:
            nth[label] += 1
            x.switch_label = f"{label} ({nth[label]})"


def _businesses(conn, email: str) -> list[SimpleNamespace]:
    """One entry per business, as attribute-access objects: a plain dict with a "keys" field would have
    that field shadowed by dict.keys (the built-in method) under Jinja's getattr-then-getitem lookup."""
    rows = conn.execute(
        "SELECT id, slug, claim_state, verified_at, ads_mode, paused_reason, created_at, digest_frequency, "
        "notify_email_status, operator_flags, owner_email FROM business "
        "WHERE owner_email = %s AND status = 'active' ORDER BY created_at DESC",
        (email,),
    ).fetchall()
    settings = config.load()
    out = []
    for r in rows:
        latest = business.latest_knowledge(conn, r["id"])
        seen = conn.execute(
            "SELECT max(ts) AS ts FROM agent_action WHERE business_id = %s AND tool = 'marketing_state' AND ok "
            "AND ts > now() - interval '10 minutes'",
            (r["id"],),
        ).fetchone()["ts"]
        listed = [
            {
                **k,
                "name": KEY_NAMES.get(k["label"], k["label"]),
                # the grants' words: a key and a grant share scope ids, so the page reads one vocabulary
                "scope_words": [oauth.SCOPE_WORDS.get(s, s) for s in k["scopes"]],
                "created": timefmt.when(_at(k["created_at"])),
                "last_used": timefmt.when(_at(k["last_used_at"])),
            }
            for k in keys.list_for_business(conn, r["id"], include_oauth=False)
            if not k["revoked_at"] and k["label"] not in WEBSITE_KEYS and k["source"] != "email"
        ]
        grants = [
            {
                **g,
                "scope_words": [oauth.SCOPE_WORDS[s] for s in g["scopes"]],
                "created_at": timefmt.when(g["created_at"]),
                "last_used_at": timefmt.when(g["last_used_at"]),
            }
            for g in oauth.grants_for_business(conn, r["id"])
            if not g["revoked_at"]
        ]
        # two grants of one assistant are two rows (spec §4.3) the times tell apart on screen; a Revoke's spoken name
        # and its confirm carry the time too
        named = Counter(g["client_name"] for g in grants)
        for g in grants:
            g["which"] = g["client_name"] + (
                f" (connected {g['created_at']['abs']})" if named[g["client_name"]] > 1 else ""
            )
        link = google_ads_link.get(conn, r["id"])
        accepted = [
            {
                "title": legal.TITLES[a["document"]],
                "version": a["version"],
                "date": _day(a["accepted_at"]),
                "url": f"/legal/{a['document']}",  # its Read link (the /legal/<document> pages)
            }
            for a in conn.execute(
                "SELECT DISTINCT ON (document) document, version, accepted_at FROM agreement_acceptance "
                "WHERE business_id = %s ORDER BY document, accepted_at DESC",
                (r["id"],),
            )
        ]
        payments = (
            [
                {
                    "words": (
                        words := (
                            e["note"] if e["kind"] == "topup" and e["note"] else PAYMENT_WORDS.get(e["kind"], e["kind"])
                        )
                    ),
                    "amount": wallet.usd(e["amount_cents"]),
                    "when": timefmt.when(e["created_at"]),
                    "has_fee": "transaction fee" in words,  # finding 4: the note under the table names only this
                }
                for e in wallet.entries(conn, r["id"], limit=5)[0]
            ]
            if r["ads_mode"] == "managed"
            else []
        )
        ready = (r["ads_mode"] == "managed" and wallet.balance_cents(conn, r["id"]) > 0) or (
            link is not None and link.live
        )
        known = (latest[1] if latest else None) or {}
        out.append(
            SimpleNamespace(
                **dict(r),
                name=_text(plans.name_for(known)) or "Your business",
                area=area_of(known),
                # the business's own Google Maps listing as the owner confirmed it, its address as a sentence says it
                # (places.display: no ", USA"), or the places waiting for the owner's pick (plan
                # 2026-10-03-agent-listing-confirm, D5)
                google_listing=_shown_listing(profile.google_listing(known)),
                listing_offer=[{"id": c["id"], "label": listings.label(c)} for c in listings.offer(conn, r["id"])],
                keys=listed,
                grants=grants,
                addresses=[
                    {
                        **g,
                        "scope_words": [mail_grants.SCOPE_WORDS[s] for s in g["scopes"]],
                        "created_at": timefmt.when(g["created_at"]),
                        "last_used_at": timefmt.when(g["last_used_at"]),
                    }
                    for g in mail_grants.for_business(conn, r["id"])
                ],
                # the light only means something once an assistant holds a key or a grant (or checked in anyway)
                show_light=bool(listed or grants or seen),
                connected_at=timefmt.when(seen),  # "never" (iso "") when no assistant checked in lately
                link=link,
                balance=(wallet.usd(wallet.balance_cents(conn, r["id"])) if r["ads_mode"] == "managed" else None),
                paused=r["paused_reason"] == "no_funds",
                accepted=accepted,
                payments=payments,
                link_words=LINK_WORDS.get(link.status) if link else None,
                ads_statement=ads_run.statement(conn, r["id"]),
                # a live link on the owner's own account cannot change from here (only Admiry can): no "Change"
                setup_label=(
                    "Add funds" if r["ads_mode"] == "managed" and ready else "Open setup" if ready else "Set up ads"
                ),
                reports=_reports(conn, settings, r),
            )
        )
    return out


def _reports(conn, settings, row) -> SimpleNamespace:
    """Settings' Reports card (daily digest spec §9): shown while the digest is on for the business, or its address
    bounced (the resume is there); the frequency, and when mail to the address bounced."""
    from . import digest  # digest imports the job modules: imported when Settings is drawn

    bounced = row["notify_email_status"] == "bounced"
    on = conn.execute(
        "SELECT max(ts) AS ts FROM agent_action WHERE business_id = %s AND tool = 'system.email_bounced'", (row["id"],)
    ).fetchone()["ts"]
    return SimpleNamespace(
        shown=digest.effective_mode(settings, row) == "on" or bounced,
        frequency=row["digest_frequency"],
        bounced=bounced,
        bounced_on=f"{on:%B} {on.day}" if on else None,
        address=row["owner_email"],
    )


CLAIMED = {"1": "This business is now yours.", "already": "This business is already yours."}
# Settings' cards a link may open (`?section=`): the daily digest's links name reports and advanced
SECTIONS = ("overview", "billing", "assistants", "reports", "email", "advanced", "account")


def _connect(
    conn,
    settings,
    session: dict,
    *,
    message: str | None = None,
    alert: bool = False,
    confirm: dict | None = None,
    status: int = 200,
    selected: str | None = None,
) -> HTMLResponse:
    """Settings shows one business at a time (spec 2026-09-29-admiry-settings-redesign §4, §5.1): `selected` when
    it is one of the owner's, else the most recent; `businesses` (all of them) feeds the switcher. `alert` marks
    `message` as something that went wrong (a refused or dead claim link) rather than news.
    The claim question (`confirm`) for a business that is not yet the signer's stands alone: no other business's
    cards (and their next step) under it."""
    all_ = _businesses(conn, session["email"])
    shown = next((b for b in all_ if b.id == selected), all_[0] if all_ else None)
    if confirm is not None and shown is not None and shown.id != selected:
        shown = None
    switch_labels(all_)
    resp = page(
        "connect.html",
        status=status,
        session=session,
        message=message,
        alert=alert,
        confirm=confirm,
        email_address=settings.email_address,
        email_inbound=settings.email_inbound,  # I5: /connect must not advertise the inbox while the channel is off
        businesses=all_,
        business=shown,
        prompt=paste_prompt(settings.public_base_url),
        labels=KEY_NAMES,
        # the shared header (_site_header.html) with the account menu
        signed_in=True,
        initial=session["email"][:1].upper(),
        email=session["email"],
        csrf=session["csrf"],
        site=website_prefix(settings),
    )
    resp.headers["Content-Security-Policy"] = CONNECT_CSP
    return resp


@router.get("/connect", response_class=HTMLResponse)
def connect_page(
    request: Request,
    claim: str | None = None,
    claimed: str | None = None,
    ads: str | None = None,
    setup: str | None = None,
    b: str | None = None,
    section: str | None = None,
) -> HTMLResponse:
    """Settings. `?b=<id>` picks the business shown (the owner's only; anything else shows the most recent).
    Opening a claim link only asks (business name + the signed-in e-mail); the claim is POST /connect/claim. The
    old setup links (`setup=ads` from plan buttons, `ads=<outcome>` from the removed forms' redirects) open the
    conversation at the setup step of the owner's newest business. `section` (the daily digest's links: Settings
    opened on its Reports or Advanced card) is kept through the sign-in and becomes the page's own #anchor."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            kept = {k: v for k, v in (("claim", claim), ("b", b), ("section", section)) if v}
            next_path = "/connect" + (f"?{urlencode(kept)}" if kept else "")
            return page("signin.html", next=sessions.safe_next(next_path), configured=bool(settings.google_client_id))
        if section in SECTIONS:
            anchor = f"/connect?{urlencode({'b': b})}#{section}" if b else f"/connect#{section}"
            return RedirectResponse(anchor, status_code=303, headers=NO_STORE)
        if setup or ads:  # the old setup links (plan buttons, assistants, bookmarks): setup is in the conversation now
            first = conn.execute(
                "SELECT id FROM business WHERE owner_email = %s AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                (session["email"],),
            ).fetchone()
            return RedirectResponse(
                f"/?setup={first['id']}" if first else "/connect", status_code=303, headers=NO_STORE
            )
        message, alert, confirm, selected = CLAIMED.get(claimed or ""), False, None, b
        if claim:
            live = claims.take(conn, claim)
            owner = (
                live
                and conn.execute("SELECT owner_email FROM business WHERE id = %s", (live["business_id"],)).fetchone()
            )
            if live is None or owner is None:
                message, alert = LINK_DEAD, True
            elif owner["owner_email"] and owner["owner_email"] != session["email"]:
                message, alert = claims.OTHER_ACCOUNT, True
            else:
                latest = business.latest_knowledge(conn, live["business_id"])
                confirm = {"token": claim, "name": (latest[1].get("name") if latest else None) or "this business"}
                selected = live["business_id"]  # shown below the question once it is theirs ("already yours")
        return _connect(conn, settings, session, message=message, alert=alert, confirm=confirm, selected=selected)


# A3p-3 P19 (plan Q11): the owner's own site, to take away -- the link the hosting minimum's e-mails carry
NO_SITE = "Admiry hosts no site for this business, so there is nothing to download."


@router.get("/connect/site", response_model=None)
def site_download(request: Request, b: str = "") -> Response:
    """The site Admiry hosts for the business `b`, as a file to keep (A3p-3 P19): the HTML of the live version, or
    of the version that was live while a site taken down is kept -- a site uploaded whole, its files as one zip. Google sign-in first (the e-mails link here, and a
    link in an e-mail is never the site itself), then the signed-in owner's own business only -- any other id is a
    404, exactly like an unknown one. Served as an attachment named after the site's address, under a CSP that runs
    nothing should a browser show it anyway."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return page(
                "signin.html",
                next=sessions.safe_next(f"/connect/site?b={b}"),
                configured=bool(settings.google_client_id),
            )
        owned_business(conn, session, b)
        found = hosted.source(conn, b)
        if found is not None and found["kind"] == "bundle":  # A3p-3 B11: a site uploaded whole, as its zip
            data = bundles.zip_of(conn, bundles.version_files(conn, found["page_id"], found["version"]))
            return site_upload.zip_response(data, found["hostname"])
    if found is None:
        return notice(NO_SITE, status=404)
    name = f"{found['hostname']}.html"
    return Response(
        found["html"].encode("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={
            **NO_STORE,
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/connect/claim")
def claim_business(request: Request, claim: str = Form(""), csrf: str | None = Form(None)):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        live = claims.take(conn, claim)
        if live is None:
            return _connect(conn, settings, session, message=LINK_DEAD, alert=True, status=409)
        try:
            with conn.transaction():
                out = claims.claim(conn, live["business_id"], session["email"], token_hash=live["token_hash"])
                audit.record_web(
                    conn,
                    tool="web.claim",
                    email=session["email"],
                    ip=sessions.request_ip(request),
                    business_id=live["business_id"],
                    args={"business_id": live["business_id"]},
                    result=out,
                )
        except claims.ClaimRefused as e:
            return _connect(
                conn, settings, session, message=str(e), alert=True, status=409, selected=live["business_id"]
            )
    support_mail.flush(business_id=live["business_id"])  # the acknowledgement a waiting ticket was holding back
    return RedirectResponse(
        f"/connect?b={live['business_id']}&claimed=" + ("1" if out["claimed_now"] else "already"),
        status_code=303,
        headers=NO_STORE,
    )


@router.get("/connect/google/start")
def google_start(request: Request, next: str | None = None):
    settings = config.load()
    if not settings.google_client_id or not settings.google_client_secret:
        return notice("Sign-in is not configured on this server yet.", status=503)
    with db.connect(settings.database_url) as conn:
        try:
            state, challenge, nonce = sessions.begin_login(conn, ip=sessions.request_ip(request), next_path=next)
        except sessions.TooManySignIns:
            return notice("Too many sign-in attempts from your network; try again in a minute.", status=429)
    url = google_auth.authorize_url(
        client_id=settings.google_client_id, redirect_uri=redirect_uri(settings), state=state, challenge=challenge
    )
    response = RedirectResponse(url, status_code=302, headers=NO_STORE)
    response.set_cookie(sessions.LOGIN_COOKIE, nonce, **sessions.login_cookie_args(settings))
    return response


def _end_login(response):
    """Every callback outcome clears the sign-in binding cookie."""
    response.delete_cookie(sessions.LOGIN_COOKIE, path=sessions.LOGIN_PATH)
    return response


@router.get("/connect/google/callback")
def google_callback(request: Request, state: str | None = None, code: str | None = None, error: str | None = None):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        attempt = sessions.finish_login(conn, state or "", request.cookies.get(sessions.LOGIN_COOKIE))
    if attempt is None:  # unknown, used, expired, or started in another browser
        return _end_login(notice("This sign-in link is no longer valid; start again.", status=400))
    if error or not code:
        return _end_login(
            notice(
                "Google sign-in was cancelled. Nothing changed.",
                status=400,
                back=attempt["next_path"],
                back_text="Try again",
            )
        )
    try:  # Google is called with no database connection open
        who = google_auth.sign_in(
            google_auth.client(),
            code=code,
            verifier=attempt["code_verifier"],
            redirect_uri=redirect_uri(settings),
            client_id=settings.google_client_id or "",
            client_secret=settings.google_client_secret or "",
        )
    except google_auth.SignInError as e:
        return _end_login(notice(str(e), status=502, back=attempt["next_path"], back_text="Try again"))
    ip = sessions.request_ip(request)
    with db.connect(settings.database_url) as conn, conn.transaction():
        sessions.revoke(conn, request.cookies.get(sessions.COOKIE))  # the session this sign-in replaces, if any
        token, _ = sessions.create(conn, email=who.email, sub=who.sub, ip=ip)
        audit.record_web(
            conn, tool="web.sign_in", email=who.email, ip=ip, business_id=None, args={}, result={"email": who.email}
        )
    response = RedirectResponse(sessions.safe_next(attempt["next_path"]), status_code=302, headers=NO_STORE)
    response.set_cookie(sessions.COOKIE, token, **sessions.cookie_args(settings))
    return _end_login(response)


@router.post("/connect/keys")
def create_key(
    request: Request,
    business_id: str = Form(...),
    label: str = Form("muse"),
    manage: str | None = Form(None),
    leads: str | None = Form(None),
    account: str | None = Form(None),
    csrf: str | None = Form(None),
):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        if label not in KEY_LABELS:
            raise HTTPException(status_code=400, detail="unknown assistant")
        biz = owned_business(conn, session, business_id)
        scopes = (
            ("read",)
            + (("manage",) if manage else ())
            + (("leads",) if leads else ())
            + (("account",) if account else ())
        )
        with conn.transaction():
            plaintext, rec = keys.mint(conn, business_id=biz["id"], cls="business", scopes=scopes, label=label)
            audit.record_web(
                conn,
                tool="web.key_create",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"business_id": biz["id"], "label": label, "scopes": list(scopes)},
                result={"key_id": rec.id, "label": label},
            )
    return page("key_once.html", key=plaintext, record=rec, business=biz, prompt=paste_prompt(settings.public_base_url))


@router.post("/connect/keys/{key_id}/revoke")
def revoke_key(request: Request, key_id: str, csrf: str | None = Form(None)):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        row = conn.execute("SELECT business_id FROM api_key WHERE id = %s", (key_id,)).fetchone()
        biz = owned_business(conn, session, row["business_id"] if row and row["business_id"] else "")
        with conn.transaction():
            done = keys.revoke(conn, key_id, biz["id"])
            if done:
                rec = keys.get(conn, key_id)
                if rec is not None and rec.grant_id is not None:  # an OAuth access token: end its grant too
                    oauth.revoke_grant(conn, rec.grant_id)
            audit.record_web(
                conn,
                tool="web.key_revoke",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"key_id": key_id},
                result={"revoked": done},
            )
    return RedirectResponse(f"/connect?b={biz['id']}#advanced", status_code=303, headers=NO_STORE)


@router.post("/connect/grants/{grant_id}/revoke")
def revoke_grant(request: Request, grant_id: str, csrf: str | None = Form(None)):
    """Revoke a connected assistant's OAuth grant (and every access/refresh token it minted)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        row = conn.execute("SELECT business_id FROM oauth_grant WHERE id = %s", (grant_id,)).fetchone()
        biz = owned_business(conn, session, row["business_id"] if row and row["business_id"] else "")
        with conn.transaction():
            n = oauth.revoke_grant(conn, grant_id)
            audit.record_web(
                conn,
                tool="web.grant_revoke",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"grant_id": grant_id},
                result={"revoked": n > 0},
            )
    return RedirectResponse(f"/connect?b={biz['id']}#assistants", status_code=303, headers=NO_STORE)


def _without_say(out: dict) -> dict:
    return {ch: {k: v for k, v in o.items() if k != "say"} for ch, o in out["channels"].items()}


def _wants_json(request: Request) -> bool:
    """The homepage's channels card posts these forms too and reads the answer as JSON (M0 Task 6); a form sent from
    Settings gets the page back."""
    return "application/json" in request.headers.get("accept", "")


@router.post("/connect/ads/pause")
def pause_ads(
    request: Request, business_id: str = Form(...), csrf: str | None = Form(None), channel: str = Form("all")
):
    """The owner's own "Pause my ads" click (spec §6): stopping spend needs no approval, same as the assistant's
    `pause` tool -- on one channel, or on every one ("all") -- and the pause is recorded on the row whatever the
    platform answers (ruling S11)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        biz = owned_business(conn, session, business_id)
        try:
            out = ads_run.pause_by_owner(conn, settings, biz["id"], channel, by="owner")
        except ToolError as e:
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": str(e)}, status_code=409, headers=NO_STORE)
            return notice(str(e), status=409, back=f"/ads?b={biz['id']}")
        audit.record_web(
            conn,
            tool="web.ads_pause",
            email=session["email"],
            ip=sessions.request_ip(request),
            business_id=biz["id"],
            args={"channel": channel},
            result={"channels": _without_say(out)},
        )
    if _wants_json(request):
        return JSONResponse({"ok": True}, headers=NO_STORE)
    return RedirectResponse(
        f"/ads?b={biz['id']}" + (f"#{channel}" if channel != "all" else ""), status_code=303, headers=NO_STORE
    )


@router.post("/connect/ads/resume")
def resume_ads(
    request: Request, business_id: str = Form(...), csrf: str | None = Form(None), channel: str = Form("all")
):
    """The owner's own "Resume my ads" click, on one channel or every one ("all"): the signed-in owner's click is
    itself the approval (as /plan/{id}/approve's web path is), so this resumes directly -- no assistant confirmation
    needed, unlike the `resume` tool's own approval="assistant" pattern. Only the owner's own pauses."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        biz = owned_business(conn, session, business_id)
        try:
            out = ads_run.resume_by_owner(conn, settings, biz["id"], channel, by="owner")
        except ToolError as e:
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": str(e)}, status_code=409, headers=NO_STORE)
            return notice(str(e), status=409, back=f"/ads?b={biz['id']}")
        audit.record_web(
            conn,
            tool="web.ads_resume",
            email=session["email"],
            ip=sessions.request_ip(request),
            business_id=biz["id"],
            args={"channel": channel},
            result={"channels": _without_say(out)},
        )
    if _wants_json(request):
        return JSONResponse({"ok": True}, headers=NO_STORE)
    return RedirectResponse(
        f"/ads?b={biz['id']}" + (f"#{channel}" if channel != "all" else ""), status_code=303, headers=NO_STORE
    )


def _listing_done(biz_id: str) -> RedirectResponse:
    return RedirectResponse(f"/connect?b={biz_id}#overview", status_code=303, headers=NO_STORE)


@router.post("/connect/listing/unlink")
def unlink_listing(request: Request, business_id: str = Form(...), csrf: str | None = Form(None)):
    """The owner's "Not my business" (plan 2026-10-03-agent-listing-confirm, D5): the linked Google Maps listing
    removed and never offered again; a centre that was the listing's own goes, and an open consultation asks again."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        biz = owned_business(conn, session, business_id)
        with conn.transaction():
            unlinked, _said = listings.unlink_and_say(conn, biz["id"])  # the one rule every unlink follows
            if unlinked is None:
                return notice(listings.NOTHING_LINKED, status=409)
            audit.record_web(
                conn,
                tool="web.listing_unlink",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={},
                result={"unlinked": unlinked.get("place_id")},
            )
    return _listing_done(biz["id"])


@router.post("/connect/listing/find")
def find_listing(request: Request, business_id: str = Form(...), csrf: str | None = Form(None), words: str = Form("")):
    """The owner's own search for the business on Google Maps: the close places offered as buttons on the card, none
    linked until the owner picks one. One counted call to Google (made outside any transaction, so its row stays)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        biz = owned_business(conn, session, business_id)
        words = words.strip()
        if not words:  # a search of nothing: no lookup, the offer as it was, nothing logged (carried item 11)
            return _listing_done(biz["id"])
        if not geocode.can_place(settings):
            return notice(geocode.NO_MAP + ".", status=409)
        latest = business.latest_knowledge(conn, biz["id"])
        try:
            found = listings.find(conn, settings, words, business_id=biz["id"], knowledge=latest[1] if latest else {})
        except geocode.NoLookups:
            return notice(geocode.NO_LOOKUPS, status=429)
        listings.save_offer(conn, biz["id"], found)
        audit.record_web(
            conn,
            tool="web.listing_find",
            email=session["email"],
            ip=sessions.request_ip(request),
            business_id=biz["id"],
            args={"words": words[: listings.QUERY_CHARS]},
            result={"offered": [c["place_id"] for c in found]},
        )
        if not found:
            return notice(listings.NOTHING_CLOSE, back=f"/connect?b={biz['id']}#overview")
    return _listing_done(biz["id"])


@router.post("/connect/listing/choose")
def choose_listing(
    request: Request, business_id: str = Form(...), csrf: str | None = Form(None), choice: str = Form("")
):
    """The owner's pick among the places offered (or "none"): only an id this business was offered, within a day. "None
    of these" with nothing open (the offer lapsed, or was closed in another tab) is no error: there is nothing to
    reject, and the card shows the search again."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        biz = owned_business(conn, session, business_id)
        with conn.transaction():
            if choice == consult.LISTING_NONE["id"]:
                result = {"none": True, "rejected": listings.reject_offer(conn, biz["id"])}
            else:
                linked = listings.confirm(conn, biz["id"], choice)
                if linked is None:
                    return notice(listings.OFFER_GONE, status=409)
                result = {"linked": linked.get("place_id")}
            audit.record_web(
                conn,
                tool="web.listing_choose",
                email=session["email"],
                ip=sessions.request_ip(request),
                business_id=biz["id"],
                args={"choice": choice[:20]},
                result=result,
            )
    return _listing_done(biz["id"])


class NewBusinessBusy(Exception):
    """Another request from the same signer holds the newbiz lock (a new business is being started)."""


BUSY_TEXT = "Another request is already starting a business for you; try again in a moment."


def blank_or_new_business(conn, email: str) -> tuple[str, bool]:
    """The signer's blank business (active, no knowledge row yet) if there is one, else a new business,
    claimed at once for `email`. Returns (business_id, created). Shared by /connect/businesses/new, which
    refuses (409) when `created` is False, and the OAuth consent page's "A new business", which reuses it —
    one blank business per signer, never an unbounded queue of them (owner_email is set at once, so
    claim_expire never releases one).

    Must be called inside a transaction: the check and the create run under the transaction-scoped advisory
    lock `newbiz:<email>`, held until that transaction commits, so two concurrent requests from one signer can
    never both see "no blank business" and both create one. A lock already held raises NewBusinessBusy rather
    than waiting (the caller answers 409 and its transaction rolls back)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
        raise RuntimeError("blank_or_new_business needs an open transaction (the lock is transaction-scoped)")
    lock = f"newbiz:{email}"
    if not conn.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok", (lock,)).fetchone()["ok"]:
        raise NewBusinessBusy(lock)
    blank = conn.execute(
        "SELECT id FROM business WHERE owner_email = %s AND status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM knowledge WHERE business_id = business.id) "
        "ORDER BY created_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    if blank is not None:
        return blank["id"], False
    biz_id = business.create(conn, locale="en-US")
    claims.claim(conn, biz_id, email)
    return biz_id, True


@router.post("/connect/businesses/new")
def start_new_business(request: Request, csrf: str | None = Form(None)):
    """Start a new business owned by the signer, claimed at once — for the owner who wants to give an
    assistant a business to work on before it exists (the key path on /connect, next to OAuth). Refused
    (409) while an earlier one from this signer is still blank (no knowledge row yet), whether this route or
    the consent page made it: the owner clicked "new", so it is never silently reused here
    (blank_or_new_business holds the per-signer lock; a lock already held is refused, not waited on)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)

        try:
            with conn.transaction():
                biz_id, created = blank_or_new_business(conn, session["email"])
                if created:
                    audit.record_web(
                        conn,
                        tool="web.business_new",
                        email=session["email"],
                        ip=sessions.request_ip(request),
                        business_id=biz_id,
                        args={},
                        result={"business_id": biz_id},
                    )
        except NewBusinessBusy:
            return notice(BUSY_TEXT, status=409)
    if not created:  # a blank business has no knowledge row, hence no name of its own yet
        return notice("Finish setting up your new business first.", status=409)
    return RedirectResponse(f"/connect?b={biz_id}#overview", status_code=303, headers=NO_STORE)


@router.post("/connect/signout")
def sign_out(request: Request, csrf: str | None = Form(None)):
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        sessions.revoke(conn, request.cookies.get(sessions.COOKIE))
    # I1 (final review): the homepage's visitor cookies go too — after the wall's claim they carry a lasting
    # business key, which must not outlive the sign-out in this browser. Local import: home_api imports the
    # tool registry, which this module must not pull in at import time.
    from .home_api import BIZ_COOKIE, VISIT_COOKIE

    # the home, signed out (spec 2026-09-29-admiry-settings-redesign §3); /connect would only show the sign-in page
    response = RedirectResponse("/", status_code=303, headers=NO_STORE)
    response.delete_cookie(sessions.COOKIE, path="/")
    response.delete_cookie(VISIT_COOKIE, path="/api")
    response.delete_cookie(BIZ_COOKIE, path="/")
    return response
