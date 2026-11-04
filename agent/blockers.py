"""What stops a launch, said in the conversation (spec 2026-10-08 B0 §3): the latest draft's refusal -- the first the
launch's own check meets (`proposals.launch_refusal`) -- with what resolves it, by its row in plan_review.RESOLUTIONS:
a button when Admiry can do it (fix), one question when only the owner knows (ask), choices when the owner decides
(decide). The plan card's launch checklist is the one surface left for it (L1, spec 2026-10-10 §4): `launch_check.
items` calls this module's own `describe` once a refusal, so the card's list shows every one at once; the setup
card's old `review` step and its `blocker` are gone -- the yes is the plan card's own, or Agree and pay's. Mostly
reads: what Admiry then runs for the owner is an existing command (home_api's /api/blocker), counted as ever -- except
its own `write_copy`, which writes a new plan version and asks the model once, counted toward the owner's day like
any other draft."""

from __future__ import annotations

import math
import re
from collections.abc import Callable

import psycopg
from fastmcp.exceptions import ToolError

from . import ads_mode, ads_refusal, ads_run, audit, auth, business, config, db, landing, places, plan_review, plans
from .channels import registry
from .channels.google_search import adapter as google

SAY = "Before your ads can start: {blocked}."
# the controls: what Admiry runs (home_api's /api/blocker), or a card the home already has (static/home.js `fix`)
WRITE_COPY = {"id": "write_copy", "label": "Write the ad text", "primary": True}
FIX_HEADLINE = {"id": "fix_headline", "label": "Fix the headline", "primary": True}
CHECK_NOW = {"id": "check_page", "label": "Check my page now", "primary": True}
CHECK_AGAIN = {"id": "check_page", "label": "Check again", "primary": True}
# the two ways to a page, always offered together (`_controls`' "page"): neither leads -- same style, Admiry's first
# (fix round 2b, item 5: Addendum 2's rule for the ads' ways, 2026-10-13, for the page's)
BUILD_SITE = {"id": "build_site", "label": "Build your business website"}
OWN_SITE = {"id": "own_site", "label": "Use my website"}
ADD_FUNDS = {"id": "add_funds", "label": "Add funds", "primary": True}
PREPAY = {"id": "prepay", "label": "Admiry runs it, I prepay"}
LINK = {"id": "link_own_account", "label": "Link my Google Ads account"}
FRESH_PLAN = {"id": "fresh_plan", "label": "Draw a fresh plan", "primary": True}
# the question's field on the home, by the ask's id: a phone, an address, a number of miles
INPUTS = {"phone": "tel", "location": "address", "radius": "miles"}
CHOICES = 3  # spec §3.1: "the two or three budget choices that pass"
# What each control runs, and how the owner confirms it when a command is dangerous (operations.DANGEROUS; R2): the
# home runs a safe one at the owner's tap or from their words; a dangerous one is always the owner's own act on
# Admiry's surface -- a payment at Stripe, the setup card's steps, the page's Publish it, the address they type.
RUNS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "answer": (("update_business", "adjust"), None),  # the phone and the address; the miles
    "write_copy": (("adjust",), None),  # a draft's words (blockers.write_copy keeps adjust's word swap)
    "fix_headline": (("build_site", "update_page"), None),  # a preview; publishing it is "publish"
    "check_page": (("check_landing_page",), None),
    "fresh_plan": (("revise_plan",), None),
    "budget": (("adjust",), None),
    "build_site": (("create_page", "build_site"), None),  # the page card's own build: a preview
    "own_site": (("set_landing_page",), "the owner types the address and taps Use it"),
    "publish": (("publish_page", "confirm_proposal"), "the owner taps Publish it"),
    "add_funds": (("request_payment_link",), "the owner pays at Stripe"),
    "prepay": (("connect_channel",), "the setup card: the documents, then the payment"),
    "link_own_account": (("connect_channel",), "the setup card: the Customer ID, then Google's invitation"),
}


def admiry_page(conn: psycopg.Connection, page: dict | None) -> str | None:
    """Which of Admiry's own pages the ads use, live: "site" (the business's site, A3p-6), "starter" (the one-page page
    Admiry wrote, create_page); None for the owner's own page, an assistant's upload, or nothing published."""
    if page is None or page["status"] != "published":
        return None
    row = conn.execute("SELECT sections FROM page WHERE id = %s", (page["id"],)).fetchone()
    sections = (row or {}).get("sections") or {}
    if sections.get("kind") == "site":
        return "site"
    return "starter" if sections.get("copy") else None


def headline_fix(conn: psycopg.Connection, business_id: str, blocked: str) -> str | None:
    """`admiry_page`'s kind when `blocked` asks Message-match's fix -- the page's headline judged against the plan of
    now (landing.judged_report) -- and the page is Admiry's own: Admiry can put the plan's headline there itself;
    None otherwise (another rule's fix, or the owner's own page: the fix sentence and Check again)."""
    page = landing.current(conn, business_id)
    rules = (landing.judged_report(conn, page) or {}).get("rules") or []
    rule = next((r for r in rules if r.get("id") == "message_match"), None)
    if rule is None or rule.get("ok") is not False or not rule.get("fix") or rule["fix"] not in blocked:
        return None
    return admiry_page(conn, page)


def run_headline_fix(
    settings: config.Settings,
    business_id: str,
    found: dict | None,
    plan_id: object,
    *,
    site: Callable[[str], dict],
    starter: Callable[[str], dict],
) -> tuple[str, dict]:
    """The one body behind B0's "Fix the headline" (home_api._fix_headline) and the public update_landing_page's
    "fix_headline" action (follow-up 2026-10-12, founder: "好"): which of Admiry's own pages the ads use decides the
    one command that runs -- `site(headline)` for the business's own site (tools_sites.fix_headline), `starter
    (headline)` for the starter page Admiry wrote (update_page) -- each the caller's own way of running that one
    command, under its own command machinery (audit, lock, rate: `run()`). `found` is the checklist item named for
    the fix_headline action (its own `text` read against Message-match's own fix, `headline_fix`) and `plan_id` the
    plan it was read for, checked against the latest draft the same way any other fix is (`draft_named`). Returns
    the headline (the caller's own words for what was done) and the chosen command's envelope. ToolError
    (NO_HEADLINE_FIX) when the headline of Admiry's own page is no longer what stops the launch."""
    with db.connect(settings.database_url) as conn:
        plan = draft_named(conn, business_id, plan_id)
        named = found is not None and found["plan_id"] == plan["id"]
        kind = headline_fix(conn, business_id, found["text"]) if named else None
        headline = landing.headline_pattern(conn, business_id)
    if kind is None or not headline:
        raise ToolError(NO_HEADLINE_FIX)
    return headline, (site(headline) if kind == "site" else starter(headline))


def _limit(conn: psycopg.Connection, business_id: str, plan: dict) -> float | None:
    """The monthly limit the launch holds the plan to (ads_run.precheck): the lower of the goal's and the plan's own."""
    goal = business.latest_goal(conn, business_id) or {}
    ads = (plan.get("sections") or {}).get("ads") or {}
    limits = [float(x) for x in (goal.get("monthly_limit_usd"), ads.get("monthly_limit_usd")) if x]
    return min(limits) if limits else None


def _under_floor(conn: psycopg.Connection, business_id: str, plan: dict) -> tuple[str, int] | None:
    """The first funded place under its floor, among the places the launch's own check reads -- its switch not off
    for this business (`registry.exec_mode_for`, as `proposals.precheck_approve_plan` narrows them before
    `ads_run.precheck_approval` ever runs) -- the floor of the way it runs for this business (A3c-1), read the way
    `ads_run.precheck`'s own floor loop reads it (`ads_run._account`'s mode); None when every such place is at or
    over its own."""
    settings = config.load()
    for channel in ads_run.funded_channels(plan):
        if not registry.known(channel) or registry.exec_mode_for(conn, settings, business_id, channel) == "off":
            continue
        floor = registry.adapter(channel).capabilities().floor(ads_run._account(conn, business_id, channel).mode)
        if float(ads_run.entry(plan, channel)["monthly_usd"]) < floor:
            return channel, floor
    return None


def _daily(monthly: float) -> str:
    return f"{plans.usd(int(monthly) // plans.DAYS)} a day"  # the plan card's "a day": the month ÷ DAYS, floored


def budget_choices(conn: psycopg.Connection, business_id: str, plan: dict, template: str | None) -> list[dict]:
    """The budgets that pass the rule the refusal names (spec §3.1: a draft change, non-dangerous; plan P13), at most
    CHOICES, each with adjust's arguments: over the monthly limit, the limit itself and the plan's levels at or under it,
    nearest the plan's own amount first; a place under its floor, that place at its floor -- the whole plan at it when
    the place is the plan's only one -- then the cheapest levels that give it as much. Two at the same daily amount are
    one choice; the first leads. [] when the rule no longer holds."""
    levels = plans.level_rows(plan["sections"])
    picks: list[tuple[str, dict]] = []
    if template == ads_run.OVER_LIMIT:
        limit = _limit(conn, business_id, plan)
        if limit is None:
            return []
        top = math.floor(limit)
        under = [(float(r["monthly_usd"]), name) for name, r in levels.items() if float(r["monthly_usd"]) <= limit]
        picks = [(_daily(top), {"monthly_budget_usd": top})]
        picks += [(_daily(amount), {"level": name}) for amount, name in sorted(under, key=lambda p: -p[0])]
    else:
        found = _under_floor(conn, business_id, plan)
        if found is None:
            return []
        channel, floor = found
        if ads_run.funded_channels(plan) == [channel]:
            picks = [(_daily(floor), {"monthly_budget_usd": floor})]
        else:
            label = f"{registry.label(channel)} at {plans.usd(floor)} a month"
            picks = [(label, {"channel": channel, "channel_monthly_usd": floor})]
        enough = [
            (float(r["monthly_usd"]), name)
            for name, r in levels.items()
            if float((r.get("split") or {}).get(channel) or 0) >= floor
        ]
        picks += [(_daily(amount), {"level": name}) for amount, name in sorted(enough)]
    out: list[dict] = []
    for label, args in picks:
        if len(out) == CHOICES:
            break
        if label not in {c["label"] for c in out}:
            out.append({"id": "budget", "label": label, "args": args})
    return [{**out[0], "primary": True}, *out[1:]] if out else []


def _ways(plan: dict) -> list[dict]:
    """The ways the setup card offers for the plan (setup.ways' rule): Admiry running it first (founder 2026-10-07), then
    the owner's own Google Ads account -- both offered, neither leads (founder 2026-10-13: both ways shown equally);
    one alone leads."""
    funded = [ch for ch in ads_run.funded_channels(plan) if registry.known(ch)]
    modes = set().union(*(registry.adapter(ch).capabilities().modes for ch in funded)) or {"managed", "own_account"}
    out = [action for mode, action in (("managed", PREPAY), ("own_account", LINK)) if mode in modes]
    return [{**out[0], "primary": True}] if len(out) == 1 else out


def _controls(conn: psycopg.Connection, business_id: str, plan: dict, blocked: str, resolution: str) -> dict:
    """The question, or the buttons, for one resolution (plan_review.RESOLUTIONS)."""
    if resolution in INPUTS:
        return {"question": {"input": INPUTS[resolution], "placeholder": plan_review.PLACEHOLDERS[resolution]}}
    if resolution == "budget":
        return {"actions": budget_choices(conn, business_id, plan, plan_review.template_for(blocked))}
    if resolution == "page_fix":
        return {"actions": [FIX_HEADLINE if headline_fix(conn, business_id, blocked) else CHECK_AGAIN]}
    if resolution == "account":
        return {"actions": _ways(plan)}
    fixed = {
        "write_copy": [WRITE_COPY],
        "check_page": [CHECK_NOW],
        "page": [BUILD_SITE, OWN_SITE],
        "funds": [ADD_FUNDS],
        "fresh_plan": [FRESH_PLAN],
    }
    return {"actions": fixed[resolution]}


def controls(conn: psycopg.Connection, business_id: str, plan: dict, resolution: str) -> dict:
    """The question or the buttons for `resolution` on its own -- a returned note's (spec 2026-10-11 §5: the phone's
    question, the page check's button; launch_check.items). `_controls` with no refusal sentence, which only the
    budget's choices and the headline's fix read (never a return's resolution)."""
    return _controls(conn, business_id, plan, "", resolution)


# the resolutions whose fix runs `adjust` (the budget choices, the miles, Admiry's ad text: RUNS), which takes only a
# plan a consultation made -- an assistant's own plan (create_marketing_plan) has none, so its refusal is said alone
# (follow-up R5, as P12's refusal no row knows): never a button that cannot run
NEEDS_CONSULTATION = frozenset({"budget", "radius", "write_copy"})


def describe(conn: psycopg.Connection, business_id: str, plan: dict, blocked: str | None) -> dict | None:
    """The blocker for `plan` refused with `blocked`: {plan_id, id (the ask's), kind, resolution, text (the refusal),
    say (Admiry's sentence), question (None, or its input and placeholder), actions ([] or the buttons)}; None with no
    refusal. A refusal no row knows is said alone, with nothing to press (B0 P12) -- never a sentence that sends the
    owner elsewhere. A fix that runs `adjust` is not offered for a plan no consultation made (NEEDS_CONSULTATION):
    said alone too. While the owner has chosen no way, the words that would pick one (a link not made: "since your
    ads run in your own account") are said as the neutral line (ads_mode.unchosen_words; Addendum 3 of the founder's
    ruling of 2026-10-13) -- its resolution and its ask's id read off the refusal as the launch gave it (page-first
    review, item 2: the neutral line is no refusal a row knows)."""
    if not blocked:
        return None
    given = blocked  # the refusal as the launch gave it: what its resolution and its ask's id are read off
    found = plan_review.resolution_for(given)
    blocked = ads_mode.unchosen_words(conn, business_id, given, plan=plan)
    if found and found["resolution"] in NEEDS_CONSULTATION and "consultation" not in plan["sections"]:
        found = None  # its fix would run adjust on a plan no consultation made (follow-up R5)
    out = {
        "plan_id": plan["id"],
        "id": plan_review.ask_id_for(given) if found else None,
        "kind": found["kind"] if found else None,
        "resolution": found["resolution"] if found else None,
        "text": blocked,
        "say": SAY.format(blocked=blocked.rstrip(".")),
        "question": None,
        "actions": [],
    }
    return {**out, **_controls(conn, business_id, plan, given, found["resolution"])} if found else out


# ---------------------------------------------------------------- what Admiry runs from the message (/api/blocker)

# a question answered in the owner's words, read in code (spec §3.3; plan P10)
PHONE_SHAPE = "Tell me the business phone with its area code, like (650) 555-0100."
PLACE_SHAPE = "Tell me the address the ads should centre on, like 500 University Ave, Palo Alto, CA."
MILES_SHAPE = "Tell me a number of miles from 1 to 100, like 10."
SHAPES = {"phone": PHONE_SHAPE, "location": PLACE_SHAPE, "radius": MILES_SHAPE}
MAX_ANSWER = 200  # geocode.MAX_PLACE_CHARS: longer is no answer to one question
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_MILES = re.compile(r"(?:(?:within|about|around|up to)\s+)?(\d{1,3}(?:\.\d+)?)\s*(?:mi|miles?)?\.?", re.IGNORECASE)
# what Admiry says once a fix is done (the next look follows it: the next blocker, or nothing)
SAVED_PHONE = "Saved. Calls from your ads will ring {phone}."
SAVED_PLACE = "Saved. Your ads centre on {place}."
SAVED_MILES = "Saved. Your ads reach {miles} around the business."
CHECKED_OK = "Checked: your page is ready for your ads."
WROTE_COPY = "Admiry wrote your ad text. It reads under “How your ad reads” above."
FRESH_DRAWN = "Here is a fresh plan, drawn again from your answers."
PLAN_MOVED = "Your plan changed meanwhile; here is where it stands now."
NOTHING_ASKED = "There is no question waiting for an answer."
# The ad text the launch asks for, written by Admiry (the setup card's, moved to the conversation: B0 P3) -- the model's
# instruction, and the refusal when the plan's words are in place; and a fresh plan's
COPY_INSTRUCTION = (
    "Write the Google Search ad text: the headlines and descriptions, from what Admiry knows about the business. "
    "Change nothing else."
)
COPY_IN_PLACE = "The ad text is already written."
FRESH_INSTRUCTION = (
    "Draw the plan again from the consultation's answers, at the same budget, so Google Search can run it."
)


def read_answer(ask_id: str, text: object) -> str | float | None:
    """The answer to the question `ask_id` in the owner's words, read in code (spec §3.3): the phone -- the first run of
    ten digits in a phone's shape (its area code; a leading 1 or +1 kept) --, the address -- the words as written, for
    the map to place --, the miles -- a number from 1 to 100, alone or with "miles" -- or None: not this answer."""
    words = " ".join(text.split()) if isinstance(text, str) else ""
    if not words or len(words) > MAX_ANSWER:
        return None
    if ask_id == "phone":
        found = _PHONE.search(words)
        return found.group(0) if found else None
    if ask_id == "location":
        return words
    if ask_id == "radius":
        found = _MILES.fullmatch(words)
        miles = float(found.group(1)) if found else None
        return miles if miles is not None and 1 <= miles <= 100 else None
    return None


def draft_named(conn: psycopg.Connection, business_id: str, plan_id: object) -> dict:
    """The latest plan, when it is a draft and the version the message was about (`plan_id`); ToolError PLAN_MOVED
    otherwise (Review Focus 2: a fix never acts on a version the owner has changed since)."""
    plan = plans.latest(conn, business_id)
    if plan is None or plan["status"] != plans.DRAFT or not isinstance(plan_id, str) or plan_id != plan["id"]:
        raise ToolError(PLAN_MOVED)
    return plan


def write_copy(settings: config.Settings, business_id: str, plan_id: object, *, email: str, ip: str | None) -> dict:
    """The ad text the launch asks for (TOO_LITTLE_COPY), written by Admiry (the setup card's write_copy, moved to the
    conversation: B0 P3): the model drafts the plan again with COPY_INSTRUCTION (tools_plan.draft_consulted, whose draft
    always carries enough words or is refused) and only its Google Search words are kept -- `adjust`'s own word swap
    writes the new version, so the budget, the area and the rest stand. One model call, counted toward the owner's day
    (`web.write_copy`, auth.check_llm_cap_for). ToolError PLAN_MOVED (`draft_named`), COPY_IN_PLACE (a complete draft
    that carries no line Google refused and none an active rule catches, spec 2026-10-12 §2.1), the day's limit or
    the draft's own refusal. Returns the new version."""
    from . import tools_consult, tools_plan  # tool modules: imported here, as home_api is, not at import time

    with db.connect(settings.database_url) as conn:

        def record(result: dict) -> None:
            audit.record_web(
                conn,
                tool="web.write_copy",
                email=email,
                ip=ip,
                business_id=business_id,
                args={"plan_id": plan_id},
                result=result,
            )

        prev = draft_named(conn, business_id, plan_id)
        exact, found = ads_refusal.flagged(conn, settings, business_id, prev)  # spec 2026-10-12 §2.1, §2.3 (plan D9)
        # ruling BB1: write_copy rewrites headlines and descriptions alone (never the search phrases, which come
        # from the themes), so a keyword-only exact line or rule finding leaves nothing for it to fix -- the gate
        # reads ad text alone
        copy_exact = [e for e in exact if e[0] != "keyword"]
        copy_found = [f for f in found if f.line_kind != "keyword"]
        if not google.copy_short(prev) and not copy_exact and not copy_found:
            raise ToolError(COPY_IN_PLACE)
        auth.check_llm_cap_for(conn, business_id)  # refused at the day's limit: no model asked, no row
        try:
            drafted = tools_plan.draft_consulted(conn, business_id, prev, COPY_INSTRUCTION)
            words = google._search_channel({"sections": drafted[0]})
            left_out = ads_refusal.dropped(conn, business_id, settings=settings)  # a rewrite never writes one back
            ctx = tools_consult.adjust_prepare(
                conn,
                business_id,
                prev["id"],
                channel=google.CHANNEL,
                headlines=[h for h in words["headlines"] if not left_out.holds("headline", h)],
                descriptions=[d for d in words["descriptions"] if not left_out.holds("description", d)],
            )
        except ToolError as e:
            record({"error": str(e)})  # the model was asked: the call counts toward the day, as a tool's failure does
            raise
        with conn.transaction():
            plan = tools_consult.adjust_commit(conn, business_id, ctx)
            record({"plan_id": plan["id"], "version": plan["version"]})
    return plan


# "Fix the headline" (plan P6): the preview is ready; publishing it is the owner's own tap (spec §8.1)
HEADLINE_READY = "Your page's new headline reads “{headline}”. Publish it, and your ads can send people there."
NO_HEADLINE_FIX = "Your page's headline is not what stands in the way now."
# A fix that can no longer run, but answers where things stand rather than a dead end (fix round 1; the headline's,
# final review m8): /api/blocker answers these `ok: true`, with the state, like any other fix -- never
# `{"ok": false, "error": ...}`, which would leave the message's button live over nothing to do.
STALE = (PLAN_MOVED, NOTHING_ASKED, NO_HEADLINE_FIX)


# an address typed in the conversation is taken only when the WHOLE text -- after at most one short lead-in ("my
# address is …", "we're at …", "at …") -- is one of three shapes (plan P10): a street address (`_STREET`) -- a
# street number, up to four words, a street word, then only an address's own further parts, each optional and in
# this order -- a unit (a number or a letter), a town (capitalised words), a state (two capitals), a ZIP, "USA";
# a town (`_TOWN`) -- capitalised words, a comma, a state's two capitals, an optional ZIP; or a ZIP code alone
# (`_ZIP`). The map is never asked about words that ask for something else (Review Focus 3) -- the question's own
# field (INPUTS["location"]) still takes any address typed straight into it, unchecked. What is taken is the address
# after its lead-in (`address`, final review I4), and its names must be names, not words (`_named`, final review m6):
# a state one of the real two-letter codes, a town more than reply words -- "Yes, OK" is no town in Oklahoma.
_LEAD = (
    r"(?:(?:my|our|the)\s+(?:business\s+)?(?:address|location)\s+is\s+"
    r"|(?:it|we)(?:'s|'re|\s+is|\s+are)\s+(?:(?:at|in)\s+)?"
    r"|at\s+)?"
)
_STREET = re.compile(
    _LEAD
    + r"(?P<address>\d+[a-z]?\s+(?:[\w.'-]+\s+){0,4}?(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|"
    r"way|ct|court|pl|place|pkwy|parkway|hwy|highway|cir|circle)\b\.?"
    r"(?:\s*,?\s*(?:#|apt\.?|apartment|suite|ste\.?|unit)\s*(?:\d+[a-z]?|[a-z]\d*)\b)?"  # a unit: a number or a letter
    r"(?:\s*,\s*(?P<town>(?-i:[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})))?"  # a town: capitalised words
    r"(?:\s*,?\s*(?P<state>(?-i:[A-Z]{2}))\b)?"  # a state: two capitals
    r"(?:\s+\d{5}(?:-\d{4})?)?"  # a ZIP
    r"(?:\s*,\s*(?:usa|united states))?\.?)",  # the country, a final period
    re.IGNORECASE,
)
_TOWN = re.compile(
    r"(?i:" + _LEAD + r")(?P<address>(?P<town>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3}),\s*(?P<state>[A-Z]{2})"
    r"(?:\s+\d{5}(?:-\d{4})?)?\.?)"
)
_ZIP = re.compile(r"(?P<address>\d{5}(?:-\d{4})?)")
# the states' own two-letter codes: the 50, DC, and the territories the ads can centre on
_STATES = frozenset(
    {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY"}
    | {"LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND"}
    | {"OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
    | {"DC", "PR", "VI", "GU", "AS", "MP"}
)
_REPLY = r"(?:yes|no|ok|okay|sure|thanks|thank\s+you|hi|hello|fine|great|cool|yep|yeah|nope|please)"
_REPLIES = re.compile(_REPLY + r"(?:\s+" + _REPLY + r")*\.?", re.IGNORECASE)  # a town of these is no town
# a phone typed in the conversation is the whole text: at most a short lead-in -- up to six words with no digits, each
# may end in ":" ("my phone is …", "call me at …", "here's my number: …", "tel: …"), and none of them a negation or
# another number's name (`_NOT_A_LEAD`: "don't use …", "my old number …", a Google Ads Customer ID's "my customer id
# is …", follow-up final review M3) -- the phone, and nothing after it but a period (final review m7; follow-up R3):
# "my number is …, and make it $20 a day" is a change the adjust read takes; the question's own field reads the phone
# leniently (`read_answer`)
_PHONE_LEAD = r"(?P<lead>(?:[a-z][a-z'-]*:?\s+){0,6})"
_NOT_A_LEAD = re.compile(r"\b(?:not|don't|dont|never|no|old|wrong|instead|except|id|account|customer)\b", re.IGNORECASE)
_TYPED_PHONE = re.compile(_PHONE_LEAD + r"(?P<phone>" + _PHONE.pattern + r")\.?", re.IGNORECASE)


def _named(found: re.Match) -> bool:
    """Whether an address's names are names (final review m6): its state, when it has one, one of the real two-letter
    codes (`_STATES`), and its town, when it has one, more than reply words (`_REPLIES`)."""
    state, town = found.groupdict().get("state"), found.groupdict().get("town")
    return (state is None or state in _STATES) and (town is None or not _REPLIES.fullmatch(town))


def read_typed(blocker: dict, text: str) -> str | float | None:
    """Free text read against the open question of what stops the launch (spec §3.3), before the adjust read: the
    phone only when the whole text is one after a short lead-in without a negation (`_TYPED_PHONE`), the address only
    when the whole text is an address's shape with names for names (`_STREET`, `_TOWN`, `_ZIP`, `_named`: a request
    that merely ends like an address, or merely contains one, is not taken -- Review Focus 3) and then the address
    after its lead-in, the miles as `read_answer` reads them (whole); None for any other kind of blocker, or words
    that are not the answer (today's path reads them)."""
    if blocker.get("kind") != "ask":
        return None
    # a phone keyboard's curly apostrophes read as straight ones ("it's at …", "it's 650 …"; follow-up R3)
    words = " ".join((text or "").replace("‘", "'").replace("’", "'").split())
    if not words or len(words) > MAX_ANSWER:
        return None
    if blocker["id"] == "phone":
        found = _TYPED_PHONE.fullmatch(words)
        return found.group("phone") if found and not _NOT_A_LEAD.search(found.group("lead")) else None
    if blocker["id"] == "location":
        found = next((m for m in (p.fullmatch(words) for p in (_STREET, _TOWN, _ZIP)) if m and _named(m)), None)
        return found.group("address") if found else None
    return read_answer(blocker["id"], words)


def placed(knowledge: dict | None) -> str | None:
    """The address the map placed for the business -- the location update_business stored, its listing's address -- as
    a sentence says it (places.display: without the country); None when none is stored (final review I4)."""
    listing = (knowledge or {}).get("listing")
    address = listing.get("address") if isinstance(listing, dict) else None
    return places.display(address) if isinstance(address, str) and address.strip() else None
