"""Owner-facing pages an assistant cannot render (spec §4.5). `/plan/{id}`: the id is an opaque 64-bit
token (the capability) that shows the plan to anyone who holds it; the approval block — the code shown
once, or Approve — appears only to the signed-in owner of a verified business. A malformed id is refused
before the database; the page is never cached on the way (no-store), noindex, and shows no secrets."""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastmcp.exceptions import ToolError
from markupsafe import Markup

from . import (
    ads_refusal,
    ads_run,
    agreements,
    audit,
    business,
    config,
    connect,
    db,
    feedback,
    google_ads_link,
    home_api,
    hosting_minimum,
    launch_review,
    legal,
    plans,
    proposals,
    sessions,
    setup,
    site_install,
    timefmt,
    wallet,
)
from .channels import registry
from .templating import env as _env
from .web import NO_STORE, notice, owned_business, page, website_prefix

router = APIRouter()
PLAN_ID = re.compile(r"pl_[0-9a-f]{16}")  # db.new_id("pl")


# The homepage talks only to its own origin and loads nothing from elsewhere (P8): no third-party fonts (Geist is
# vendored in /static/fonts), no CDN -- except the one frame Task 7 adds: the page-builder card's iframe, which
# shows a preview served from the agent's own host, never anywhere else.
def home_csp(settings: config.Settings) -> str:
    """The homepage's own CSP (H36): `frame-src <public base>` (where /p/{token} previews are served) is the
    one addition Task 7 makes; everything else stays exactly P8's no-third-party, no-CDN policy. A function of
    settings, not a module constant, because dev and prod have different public bases."""
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
        f"frame-src {settings.public_base_url}; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


@router.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> HTMLResponse:
    """The conversation (spec 2026-09-26 §3, D36): one static page; home.js drives every moment through
    /api/turn. Rendered per request only for what the header and the sheet need before the first call:
    whether a web session exists (the account menu or Sign in), the tokenless paste prompts (a hand-off
    token is minted only when the sheet opens), and whether this browser has started a consultation before
    (`returning`: the page waits for `state` instead of flashing the first-visit entry). noindex outside prod
    (the page is admiry.ai's front door in prod); no-store: the header differs per visitor."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        web_session = sessions.of_request(conn, request)
    base = settings.public_base_url
    html = _env.get_template(
        "home.html"
    ).render(
        mcp_url=f"{base}/mcp",
        prompts=connect.prompts(base, None),
        signed_in=web_session is not None,
        initial=web_session["email"][:1].upper() if web_session else None,
        email=web_session["email"] if web_session else None,  # the account menu (settings redesign §3)
        csrf=web_session["csrf"] if web_session else None,  # its Sign out form
        anon_turns=settings.anon_turns,
        returning=home_api.BIZ_COOKIE in request.cookies,  # presence only; the value is never read here
        # the front door since 2026-09-29 (admiry.ai/): indexed in prod only; dev and local stay noindex
        indexable=settings.env == "prod",
        canonical=f"{settings.web_base_url}/",
        site=website_prefix(settings),
        support=settings.support,  # the plan thumbs' script and sheet (spec 2026-09-30 §6; the dock's link left with spec 2026-10-12 H4)
        help=settings.support,  # the Help button (spec 2026-10-12 §3.1): _help.html, last in <body>
        hosting_minimum=hosting_minimum.MINIMUM,  # A3p-3 P15: the page card says it before building
        admiry_page_way=site_install.ADMIRY_PAGE_WAY,  # A3p-5 batch 2 (ruling R2-f): the fourth way, chosen
    )
    return HTMLResponse(html, headers={**NO_STORE, "Content-Security-Policy": home_csp(settings)})


def _legal_page(document: str) -> HTMLResponse:
    version, body = legal.current(document)
    html = _env.get_template("legal.html").render(
        title=legal.TITLES[document],
        version=version,
        body=Markup(body),  # legal.parse escaped every text
        help=config.load().support,  # the Help button (spec 2026-10-12 §3.1)
    )
    return HTMLResponse(html, headers={"X-Robots-Tag": "noindex"})


@router.get("/legal/management-agreement", response_class=HTMLResponse)
def management_agreement() -> HTMLResponse:
    """The Google Ads Management Agreement owners accept in the home conversation (spec 2026-09-25 D28): public,
    versioned by the file's front matter, noindex until counsel signs off (spec §6)."""
    return _legal_page(legal.AGREEMENT)


@router.get("/legal/prepayment-addendum", response_class=HTMLResponse)
def prepayment_addendum() -> HTMLResponse:
    """The Prepayment Addendum the owner of a prepaid business accepts in the home conversation before adding funds
    (spec 2026-09-25 D28): public, versioned like the agreement, noindex until counsel signs off."""
    return _legal_page(legal.ADDENDUM)


def _viewer(session: dict | None, biz: dict) -> str:
    """anonymous | unclaimed (signed in, the business has no owner yet) | owner | other."""
    if session is None:
        return "anonymous"
    if not biz["owner_email"]:
        return "unclaimed"
    return "owner" if biz["owner_email"] == session["email"] else "other"


@router.get("/plan/{plan_id}", response_class=HTMLResponse)
def plan_page(request: Request, plan_id: str) -> HTMLResponse:
    if not PLAN_ID.fullmatch(plan_id):
        raise HTTPException(status_code=404, detail="no such plan")
    with db.connect(config.load().database_url) as conn:
        plan = plans.by_id(conn, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no such plan")
        biz = business.row(conn, plan["business_id"])
        latest = business.latest_knowledge(conn, plan["business_id"])
        name = (latest[1].get("name") if latest else None) or "your business"
        session = sessions.of_request(conn, request)
        viewer = _viewer(session, biz)
        # Task 5: the "ads" row of the Execution list, live from the business's `channel_campaign` rows rather than
        # the value frozen into the plan's json at approval time (execution["ads"] there never changes once written).
        ads_line = ads_run.execution_line(ads_run.rows(conn, plan["business_id"])) if plan["execution"] else None
        # the plan's "Your website" row (A3p-5 batch 2 Q5): where Admiry's code stands on the page the ads use
        website = site_install.row(conn, plan["business_id"])
        approval = None
        if viewer == "owner":
            approval = {
                "verified": biz["claim_state"] == "verified",
                "csrf": session["csrf"],
                "open": proposals.open_for_plan(conn, biz["id"], plan_id),
                "ads_mode": biz["ads_mode"],
                "setup": f"/?setup={biz['id']}",  # ads setup and adding funds live in the conversation
            }
        rate = None
        settings = config.load()
        if viewer == "owner" and settings.support:
            given = feedback.vote_of(conn, plan_id, f"web:{session['email']}")
            if given is not None or feedback.sampled(plan_id, settings.feedback_plan_page_rate):
                rate = {"plan_id": plan_id, "vote": given}
        in_review = launch_review.owner_words(conn, biz["id"])  # L1: where it stands while Admiry checks it
        # spec 2026-10-11 §5.1: what Admiry removed from this version's ads after Google refused it, in owner words
        changes = [ads_refusal.line(c) for c in ads_refusal.changes(conn, plan["business_id"], plan_id)]
    html = _env.get_template("plan.html").render(
        plan=plan,
        name=name,
        created=timefmt.when(datetime.fromisoformat(plan["created_at"])),  # never the raw ISO string on screen
        order=plans.SECTION_ORDER,
        channel_names=plans.CHANNEL_NAMES,
        channel_ids=registry.LABELS,  # plan.execution's per-channel entries: the "ads" line speaks for them (M0)
        copy_of=plans.copy_of,  # never the primary's: its section always shows the ad text its build runs
        usd=plans.usd,
        viewer=viewer,
        approval=approval,
        rate=rate,
        ads_line=ads_line,
        sign_in=f"/connect/google/start?next=/plan/{plan_id}",
        cards=plans.cards({**plan, "business_name": name}, website=website)
        if "consultation" in plan["sections"]
        else None,
        in_review=in_review,
        changes=changes,
        changes_intro=ads_refusal.INTRO,
        help=settings.support,  # the Help button (spec 2026-10-12 §3.1)
    )
    return HTMLResponse(html, headers=NO_STORE)


def _owner_plan(conn, request: Request, plan_id: str, csrf: str | None) -> tuple[dict, dict, dict]:
    """(session, business, plan) for a POST from the signed-in owner of a verified business with a draft plan;
    403 otherwise (404 for an unknown plan, 409 when the business or plan is not ready)."""
    session = sessions.of_request(conn, request)
    sessions.check_csrf(session, csrf)
    plan = plans.by_id(conn, plan_id) if PLAN_ID.fullmatch(plan_id) else None
    if plan is None:
        raise HTTPException(status_code=404, detail="no such plan")
    biz = business.row(conn, plan["business_id"])
    if _viewer(session, biz) != "owner":
        raise HTTPException(status_code=403, detail="this plan belongs to another Google account")
    if biz["claim_state"] != "verified":
        raise HTTPException(status_code=409, detail="the business must be verified before a plan can be approved")
    if plan["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"this plan is {plan['status']}")
    return session, biz, plan


@router.post("/plan/{plan_id}/code", response_class=HTMLResponse)
def plan_code(request: Request, plan_id: str, csrf: str | None = Form(None)) -> HTMLResponse:
    """Show the owner a fresh approval code for this plan's proposal (created here when the assistant has not)."""
    with db.connect(config.load().database_url) as conn:
        session, biz, plan = _owner_plan(conn, request, plan_id, csrf)
        try:  # a concurrent revise or approve surfaces here as a refusal, not a 500
            with conn.transaction():
                prop = proposals.open_for_plan(conn, biz["id"], plan_id) or proposals.propose_plan(
                    conn, biz["id"], plan_id, created_by="web"
                )
                code = proposals.issue_code(conn, prop["proposal_id"], via="plan_page")
                audit.record_web(
                    conn,
                    tool="web.code_issue",
                    email=session["email"],
                    ip=sessions.request_ip(request),
                    business_id=biz["id"],
                    args={"plan_id": plan_id, "proposal_id": prop["proposal_id"]},
                    result={"proposal_id": prop["proposal_id"]},
                    approval_kind="code",
                    approval_ref=prop["proposal_id"],
                )
        except (ToolError, ValueError) as e:
            return notice(str(e), status=409, back=f"/plan/{plan_id}", back_text="Back to the plan")
    return page("code_once.html", code=code, plan=plan, proposal=prop)


@router.post("/plan/{plan_id}/approve")
def plan_approve(request: Request, plan_id: str, csrf: str | None = Form(None)):
    """The owner's own click: confirm this plan's proposal (created here when the assistant has not); once the
    approval has committed, its Google Search ads launch in this request (A3 P6: `ads_run.launch_now`, which never
    raises -- what does not finish is the hourly ads-launch's) -- but only once it actually approved the plan: with
    AGENT_LAUNCH_REVIEW on, a business's first launch goes to Admiry's review instead (`plan_status` in_review) and
    nothing launches here."""
    with db.connect(config.load().database_url) as conn:
        session, biz, _plan = _owner_plan(conn, request, plan_id, csrf)
        try:  # the setup card's Confirm and launch takes the same path (proposals.approve_on_web, S0)
            out = proposals.approve_on_web(
                conn, biz["id"], plan_id, email=session["email"], ip=sessions.request_ip(request)
            )
        except ToolError as e:
            return notice(str(e), status=409, back=f"/plan/{plan_id}", back_text="Back to the plan")
    if out.get("plan_status") == plans.APPROVED:
        ads_run.launch_now(biz["id"])
    return RedirectResponse(f"/plan/{plan_id}", status_code=303, headers=NO_STORE)


# ---------------------------------------------------------------- the agree page (founder's ruling 3, 2026-10-13)
# The first payment's link (setup.agree_url): the plan's summary, the documents to tick, one button to Stripe -- the
# home's own Agree and pay (setup.agree_and_pay). Signed-in owner only, as Settings' own pages: a visitor signs in
# first, anyone else finds no such page. No script runs on it; it is never framed.
AGREE_CSP = (
    "default-src 'self'; script-src 'none'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
    "connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def _agree_target(conn, session: dict, plan_id: str) -> tuple[dict, dict]:
    """(plan, business) for the signed-in owner's own plan; 404 otherwise -- someone else's plan answers exactly as an
    id that names no plan (fix round 2b, item 2: owned_business's own "no such business" would say the plan exists)."""
    plan = plans.by_id(conn, plan_id) if PLAN_ID.fullmatch(plan_id) else None
    if plan is not None:
        try:
            return plan, owned_business(conn, session, plan["business_id"])
        except HTTPException:
            pass  # not this owner's: the unknown id's own answer, below
    raise HTTPException(status_code=404, detail="no such plan")


def _agree(
    conn, settings: config.Settings, session: dict, plan: dict, biz: dict, *, paid=False, look=0, error=None, status=200
) -> HTMLResponse:
    """The agree page as it stands: paid (this plan's payment is in: the confirmation), being confirmed (back from
    Stripe before its word, looking again every LOOK_SECONDS up to LOOKS times, as the home's card waits), refused
    (setup.agree_refusal: the sentence and the home's link), or the form."""
    found = conn.execute(
        "SELECT bool_or(status = 'paid') AS paid, bool_or(status = 'open') AS open FROM payment_session "
        "WHERE business_id = %s AND plan_id = %s",
        (biz["id"], plan["id"]),
    ).fetchone()
    latest = business.latest_knowledge(conn, biz["id"])
    view: dict = {"name": plans.name_for(latest[1] if latest else None) or "your business"}
    view["back_line"] = setup.BACK_TO_ASSISTANT
    if found["paid"]:
        # in review: where its review stands; else the payment is in, and the next step is the assistant's to ask --
        # never the receipt's sentence, which ends with the home's link (fix round 2b, item 3: one way to go)
        review = launch_review.owner_words(conn, biz["id"])
        said = review or setup.PAID_LINE
        view |= {"view": "paid", "title": setup.PAID_IN_REVIEW if review else setup.PAID, "said": said}
    elif paid and found["open"]:
        more = look < setup.LOOKS
        refresh = f"{setup.LOOK_SECONDS};url=/agree/{plan['id']}?paid=1&look={look + 1}" if more else None
        view |= {"view": "confirming", "said": setup.CONFIRMING if more else setup.NOT_YET, "refresh": refresh}
    else:
        problem = setup.agree_refusal(conn, settings, biz["id"], plan["id"])
        if problem is not None:
            return notice(problem, status=409, back=f"/?setup={biz['id']}", back_text="Open Admiry")
        view |= _agree_form(conn, settings, plan, biz["id"]) | {"csrf": session["csrf"], "error": error}
    html = _env.get_template("agree.html").render(plan_id=plan["id"], **view)
    return HTMLResponse(html, status_code=status, headers={**NO_STORE, "Content-Security-Policy": AGREE_CSP})


def _agree_form(conn, settings: config.Settings, plan: dict, business_id: str) -> dict:
    """The form's parts: where the ads run and for how much, the area, what the payment costs and adds to the balance
    (by card: Link is priced as the card), and the documents to tick -- each one the prepaid way needs and the
    business has not accepted, as the setup card asks them. The area is the plan card's (a plan built by a
    consultation); the places, every plan's own. While an invitation to the owner's own Google Ads account waits, the
    line that says paying here switches the way and withdraws it (`switches`: setup.SWITCHES_WAY; final review, item
    4) -- setup.choose withdraws it when the owner pays."""
    rows = plans.cards(plan)["plan"] if "consultation" in plan["sections"] else None
    channels = [(registry.label(c["channel"]), c["monthly_usd"]) for c in plans.funded(plan["sections"])]
    credit = setup.agree_amount_usd(conn, settings, business_id) * 100
    quote = wallet.quote(credit, "card", settings)
    daily = setup.daily_usd(conn, business_id)
    waiting = google_ads_link.get(conn, business_id)
    docs = [
        {"id": d, "title": legal.TITLES[d], "version": agreements.version(d)}
        for d in setup.documents("managed")
        if not agreements.accepted(conn, business_id, d)
    ]
    return {
        "view": "form",
        # each place's own amount only when there are several: one place's is the monthly budget below it
        "channels": [
            label if len(channels) == 1 else f"{label} {plans.usd(amount)} a month" for label, amount in channels
        ],
        "monthly": plans.usd(plans.total_usd(plan["sections"])),
        "area": rows["setup"]["area"][:1].upper() + rows["setup"]["area"][1:] if rows else None,
        "price": wallet.usd(quote.total_cents),
        "fee": wallet.usd(quote.processing_cents),
        "credit": wallet.usd(quote.credit_cents),
        "days": credit // 100 // daily if daily else None,
        "documents": docs,
        "review": settings.launch_review,
        "switches": setup.SWITCHES_WAY if waiting is not None and waiting.status == "pending" else None,
    }


def _look(text: str | None) -> int:
    return int(text) if text and text.isascii() and text.isdigit() and len(text) < 4 else 0


@router.get("/agree/{plan_id}", response_class=HTMLResponse)
def agree_page(request: Request, plan_id: str, paid: str | None = None, look: str | None = None) -> HTMLResponse:
    """The agree page (see above); `?paid=1` is Stripe's way back (setup.pay's `back`), `look` how many times it has
    looked for the payment since."""
    if not PLAN_ID.fullmatch(plan_id):
        raise HTTPException(status_code=404, detail="no such plan")
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        if session is None:
            return page(
                "signin.html", next=sessions.safe_next(f"/agree/{plan_id}"), configured=bool(settings.google_client_id)
            )
        plan, biz = _agree_target(conn, session, plan_id)
        return _agree(conn, settings, session, plan, biz, paid=paid == "1", look=_look(look))


@router.post("/agree/{plan_id}", response_class=HTMLResponse, response_model=None)
def agree_pay(
    request: Request,
    plan_id: str,
    csrf: str | None = Form(None),
    agreement: str | None = Form(None, alias=legal.AGREEMENT),
    addendum: str | None = Form(None, alias=legal.ADDENDUM),
) -> HTMLResponse | RedirectResponse:
    """Agree and pay: the owner's ticks (each document's field carries the version the page showed), then Stripe --
    setup.agree_and_pay, the home's own path; refused on the page itself, as it stands, with the reason."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        session = sessions.of_request(conn, request)
        sessions.check_csrf(session, csrf)
        plan, biz = _agree_target(conn, session, plan_id)
    owner = setup.Owner(email=session["email"], ip=sessions.request_ip(request))
    try:
        url = setup.agree_and_pay(
            settings, owner, biz["id"], plan_id, {legal.AGREEMENT: agreement, legal.ADDENDUM: addendum}
        )
    except setup.Refused as e:
        with db.connect(settings.database_url) as conn:
            return _agree(conn, settings, session, plan, biz, error=str(e), status=e.status)
    return RedirectResponse(url, status_code=303, headers=NO_STORE)
