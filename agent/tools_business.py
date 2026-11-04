"""Business Agent tools (spec §4.2 row 1): describe_business, get_business, update_business,
set_goal. Descriptions are read by the assistant: what · when · call-first · units."""

from __future__ import annotations

import json
import re

from fastmcp.exceptions import ToolError

from . import ads_run, auth, business, claims, config, consult, crawl, envelope, geocode, listings, llm, plans, profile
from .channels import registry
from .mcp_app import agent_tool, need_business, run
from .util import iso

MAX_TEXT = consult.MAX_TEXT  # the most of the owner's words the reading takes: the address scan's bound too (R2-f)
PATCHABLE = {
    "name",
    "category_detail",
    "service_area",
    "services",
    "hours",
    "phone",
    "website",
    "pricing_hints",
    "usp",
    "reviews_summary",
    "compliance_flags",
    "locale",
}
LOCALE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")  # fullmatch: en-US, es-419, zh-Hant-TW
PATCH_MAX_CHARS, PATCH_MAX_STRING, PATCH_MAX_ITEMS = 8000, 500, 50
# A location's address with nothing in it (update_business's prepare; walk fixes 1, Task 4 round 1)
ADDRESS_EMPTY = "address is empty: give a street address, or a city and ZIP code"
# The business's Google Maps listing (plan 2026-10-03-agent-listing-confirm, D5): one key, alone in a patch, in one of
# three shapes
LISTING_KEY = "google_listing"
LISTING_ALONE = "google_listing goes alone in a patch"
LISTING_SHAPE = (
    'google_listing takes null (unlink it), {"find": "<the name and the address>"} or {"choose": "<an id from the '
    'last find>"}'
)


_need_business = need_business  # the older name, kept for existing importers


def check_locale(locale) -> str:
    """A language tag shaped like en-US (the value reaches the model's prompt); ToolError otherwise."""
    if not isinstance(locale, str) or not LOCALE_RE.fullmatch(locale):
        raise ToolError("locale must look like en-US")
    return locale


def check_patch(patch: dict) -> None:
    """Bounds on what an assistant writes into the knowledge every plan is drafted from: the whole
    patch, every text and every list at any depth (the error names the top-level field)."""
    if len(json.dumps(patch, ensure_ascii=False, default=str)) > PATCH_MAX_CHARS:
        raise ToolError(f"patch is too large (over {PATCH_MAX_CHARS:,} characters); send fewer or shorter fields")
    for field, value in patch.items():
        stack = [value]
        while stack:
            v = stack.pop()
            if isinstance(v, str) and len(v) > PATCH_MAX_STRING:
                raise ToolError(f"{field}: text over {PATCH_MAX_STRING} characters; shorten it")
            if isinstance(v, list):
                if len(v) > PATCH_MAX_ITEMS:
                    raise ToolError(f"{field}: more than {PATCH_MAX_ITEMS} items")
                stack.extend(v)
            elif isinstance(v, dict):
                stack.extend(v.values())


def _sentence(value) -> bool:
    """Text with something in it."""
    return isinstance(value, str) and bool(value.strip())


def check_shapes(patch: dict) -> None:
    """The fields a plan's card, its sentences and the page read back, in the shapes every extraction writes
    (models.Knowledge): the name and the trade text with something in it, the website that or null (null removes it),
    the reasons to choose the business and the price hints lists of sentences (final review B, Important 6; open item
    B3: a name patched as an object reached the plan card). ToolError naming the field."""
    for field in ("name", "category_detail"):
        if field in patch and not _sentence(patch[field]):
            raise ToolError(f"{field} must be text")
    if patch.get("website") is not None and not _sentence(patch["website"]):
        raise ToolError("website must be text")
    for field in ("usp", "pricing_hints"):
        if field in patch and not (isinstance(patch[field], list) and all(_sentence(v) for v in patch[field])):
            raise ToolError(f"{field} must be a list of sentences")


def _check_alone(patch: dict) -> None:
    """update_business's checks of what a patch reads alone, in the order they refuse: empty, a field it does not take,
    sizes (`check_patch`), shapes (`check_shapes`), the locale. Its prepare runs them before it puts an address on the
    map, so a patch they refuse spends no lookup (walk fixes 1, Task 4 round 2)."""
    if not patch:
        raise ToolError("patch is empty: give at least one field to change")
    unknown = set(patch) - PATCHABLE - set(profile.CHANGE_KEYS) - {"ai_crawlers"}
    if unknown:
        raise ToolError(f"unknown fields: {sorted(unknown)}; patch only the Business Knowledge fields")
    check_patch(patch)
    check_shapes(patch)
    if "locale" in patch:
        check_locale(patch["locale"])


def _listing_change(value) -> tuple[str, str | None]:
    """The patch's google_listing as (what, its value): ("unlink", None), ("find", the owner's words) or ("choose", an
    id). ToolError(LISTING_SHAPE) for anything else."""
    if value is None:
        return "unlink", None
    if isinstance(value, dict) and len(value) == 1:
        ((what, text),) = value.items()
        if what in ("find", "choose") and isinstance(text, str) and text.strip():
            return what, text.strip()
    raise ToolError(LISTING_SHAPE)


def _view(conn, biz: str) -> dict | None:
    """The business's Google Maps listing as the profile shows it after a change (profile.google_listing)."""
    latest = business.latest_knowledge(conn, biz)
    return profile.google_listing(latest[1] if latest else None)


def _listing_body(conn, biz: str, what: str, value: str | None, found: list[dict] | None) -> dict:
    """The owner's word on the business's Google Maps listing (plan 2026-10-03-agent-listing-confirm, D5): unlink it,
    the places a find found (offered, not linked), or the one chosen. The answer carries the profile's view after, and
    `listing`: what was done ("unlink", "find", "choose" -- a place linked -- or "none" -- "None of these"), which the
    owner's activity log reads (tools_account.VARIANTS)."""
    if what == "find":
        listings.save_offer(conn, biz, found or [])
        candidates = [{"id": c["id"], "name": c["name"], "address": c["address"]} for c in found or []]
        say = listings.found_say(found) if found else listings.NOTHING_CLOSE
        answer = {"listing": "find", "candidates": candidates, "google_listing": _view(conn, biz)}
        return envelope.make(answer, say=say)
    if what == "choose" and value.lower() == consult.LISTING_NONE["id"]:  # "None" or "NONE" as a model may write it
        # as the consultation says it: nothing is promised when no place was open (its offer lapsed or none was made)
        said = listings.NONE_NOTED if listings.reject_offer(conn, biz) else listings.NONE_GONE
        return envelope.make({"listing": "none", "google_listing": _view(conn, biz)}, say=said)
    if what == "choose":  # linking moves no centre the owner gave: listings.link keeps a placed one
        linked = listings.confirm(conn, biz, value)
        if linked is None:
            raise ToolError(listings.OFFER_GONE)
        answer = {"listing": "choose", "google_listing": _view(conn, biz)}
        return envelope.make(answer, say=listings.LINKED.format(name=linked["name"]))
    unlinked, say = listings.unlink_and_say(conn, biz)  # the one rule every unlink follows
    if unlinked is None:
        raise ToolError(listings.NOTHING_LINKED)
    return envelope.make({"listing": "unlink", "google_listing": _view(conn, biz)}, say=say)


def check_text(text: str) -> str:
    """The owner's description, stripped; ToolError when there is none."""
    if not text or not text.strip():
        raise ToolError("text is required: one or two sentences on what the business does and where")
    return text.strip()


def reading_listing(listing) -> dict | None:
    """The listing a reading of the business shows the model: exactly what `profile.model_listing` shows every prompt --
    the business's own, as the owner confirmed it, whole; an address the owner placed as its address alone; None for a
    listing nobody confirmed and for no listing (plan 2026-10-03-agent-listing-confirm). One rule, in one place."""
    return profile.model_listing(listing)


def site_pages(website_url: str) -> list[dict]:
    """The pages of `website_url` a reading takes (crawl.fetch_site), as plain dicts: none when the crawl read no page
    -- a redirect to another host, a timeout, nothing but non-HTML. ToolError ("could not read the website: …") when the
    crawl refuses the address. Calls the site: prepare only."""
    try:
        return [p.__dict__ for p in crawl.fetch_site(website_url)]
    except crawl.CrawlError as e:
        raise ToolError(f"could not read the website: {e}") from e


def extract(
    text: str,
    website_url: str | None,
    locale: str,
    *,
    listing: dict | None = None,
    gen: llm.Generator | None = None,
    pages: list[dict] | None = None,
) -> dict:
    """Website (optional) + one LLM call → knowledge JSON. External calls only: call it from prepare.
    `gen` is the caller's generator (start builds one for both of its calls); None builds one. `listing` is the
    listing on file that describe_business passes, which the new version keeps; the model reads it only as
    `reading_listing` (profile.model_listing) shows it. `start` passes none: it looks the business up after the reading
    (plan 2026-10-03-agent-listing-confirm). `pages` are the site's pages when the caller crawled it already
    (`site_pages`: `start` tells a site that read no page from one that did, A3p-5 ruling R2-a), so it is crawled once;
    None crawls `website_url` here. The reading records where its name came from (`name_source`, A3p-5 batch 2 Task 0,
    ruling R0-k): "site" when it read at least one page of a site, "words" when it read none -- the owner's words alone,
    or a site whose crawl read no page -- so plans.name_for never vouches for a name a site never gave. It records the
    platform the site is built on too (`website_platform`, A3p-5 batch 2 Q1: the crawl read it from the first page),
    when the reading's website is the site it read (listings.same_site): never one site's platform for another. It
    counts the pages read too (`pages_read`, R6, spec 2026-10-12 §3.3), for the facts card that tells the owner what
    Admiry took from the site -- 0 when `pages` is empty, the owner's words alone."""
    text = check_text(text)
    if pages is None:
        pages = site_pages(website_url) if website_url else []
    try:
        gen = gen if gen is not None else llm.generator()
        knowledge = gen.extract_knowledge(text[:MAX_TEXT], pages=pages, listing=reading_listing(listing), locale=locale)
    except llm.LLMError as e:
        raise ToolError(str(e)) from e
    data = knowledge.model_dump()
    data["name_source"] = "site" if pages else "words"
    data["pages_read"] = len(pages)  # what the reading's facts card counts (R6, spec 2026-10-12 §3.3)
    if website_url and not data.get("website"):
        data["website"] = website_url
    platform = pages[0].get("platform") if pages else None
    if platform and listings.same_site(data.get("website"), website_url):
        data["website_platform"] = platform
    if listing:
        data["listing"] = listing
    return data


@agent_tool(cls="W", scope="manage")
def describe_business(
    text: str, idempotency_key: str, website_url: str | None = None, locale: str | None = None
) -> dict:
    """Tell Admiry what the business does: one or two sentences in the owner's words (what, where,
    any budget or goal), plus the website if there is one. Admiry reads up to 20 pages of the site
    and writes the Business Knowledge every plan is built from: services, area, hours, phone,
    prices it found. Call this first for a new business, or again when things change. Give
    `locale` (e.g. 'es-US') only when the owner's language differs from what is on file. Returns
    the knowledge version and what was found."""

    def prepare(conn, p):
        auth.check_llm_cap(conn, p)
        if locale:
            check_locale(locale)
        biz = need_business(p)
        current = business.row(conn, biz)["locale"]
        # nothing is looked up here: the listing on file goes to the reading and the new version keeps it whole; the
        # model sees it only as reading_listing (profile.model_listing) shows it -- the business's own Google listing the
        # owner confirmed, whole, so it names the business with that in front of it; an address the owner placed as its
        # address alone; anything else not at all
        latest = business.latest_knowledge(conn, biz)
        listing = (latest[1] if latest else {}).get("listing")
        data = extract(text, website_url, locale or current, listing=listing if isinstance(listing, dict) else None)
        data.pop(
            "stated_address", None
        )  # start's, to put on the map (tools_start._owners_address): never the profile's
        return data

    def body(conn, p, data):
        biz = need_business(p)
        version = business.add_knowledge(conn, biz, data, source="extract")
        if locale:
            conn.execute("UPDATE business SET locale = %s, updated_at = now() WHERE id = %s", (locale, biz))
        # the version on file, not the extraction before it: the ids an assistant reads here are the ones it changes by
        return {"version": version, "knowledge": business.latest_knowledge(conn, biz)[1]}

    return run(
        "describe_business",
        "manage",
        {"text": text, "website_url": website_url, "locale": locale, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
    )


@agent_tool(cls="RO", scope="read")
def get_business() -> dict:
    """What Admiry knows about this business: the latest Business Knowledge (name, services, area,
    hours, phone, prices, what makes it different), its claim state and the current goal. Call it
    before proposing changes so you quote what is on file."""

    def body(conn, p):
        biz = need_business(p)
        row = business.row(conn, biz)
        latest = business.latest_knowledge(conn, biz)
        return {
            "business": {
                "id": row["id"],
                "slug": row["slug"],
                "locale": row["locale"],
                "status": row["status"],
                "claim_state": row["claim_state"],
                "ai_crawlers": row["ai_crawlers"],
            },
            "knowledge_version": latest[0] if latest else None,
            "knowledge": latest[1] if latest else None,
            "goal": business.latest_goal(conn, biz),
        }

    return run("get_business", "read", {}, body)


@agent_tool(cls="W", scope="manage")
def update_business(patch: dict, idempotency_key: str) -> dict:
    """Correct or add facts the owner tells you (a new service, new hours, a phone number, an area,
    prices) as a patch of Business Knowledge fields: name, category_detail, service_area, services,
    hours, phone, website, pricing_hints, usp, reviews_summary, compliance_flags, locale;
    `ai_crawlers: false` turns AI crawlers off for the storefront.
    By id, without resending a whole list: `service_changes` and `offer_changes` (a list of objects: one with an
    `id` changes that service or offer, or removes it with `remove: true`; one without an id adds it), `location`
    (geo_model storefront or service_area, areas, hours, phone, phone_hours) and `notes_add` (sentences to keep).
    Creates a new knowledge version the owner can check with get_business. Confirm the wording with the owner
    first."""

    def prepare(conn, p):
        """The patch's own checks first (`_check_alone`: what it reads alone), so no lookup is spent on a patch they
        refuse (walk fixes 1, Task 4 round 2); then an `address` in the location's change, put on the map before the
        transaction with start's own lookup (`geocode`): the location takes the place found as its `listing` (Task 4).
        Only this lookup writes a listing: one the patch itself carries is refused, and so is an address with nothing in
        it; the location's other fields are checked before the lookup too. A spent Places budget or cap is refused in
        its own words (geocode.NO_LOOKUPS). Any other patch passes unchanged.

        A `google_listing` patch goes alone (LISTING_ALONE) and in one of three shapes (`_listing_change`); a find is
        the owner's search on Google Maps, made here before the transaction (`listings.find`: a spent day is refused in
        geocode.NO_LOOKUPS' words, no key in geocode.NO_MAP), and what it found reaches the body to be offered."""
        biz = need_business(p)
        if LISTING_KEY in patch:
            if len(patch) > 1:
                raise ToolError(LISTING_ALONE)
            what, value = _listing_change(patch[LISTING_KEY])
            found = None
            if what == "find":
                settings = config.load()
                if not geocode.can_place(settings):
                    raise ToolError(geocode.NO_MAP)
                latest = business.latest_knowledge(conn, biz)
                found = listings.find(conn, settings, value, business_id=biz, knowledge=latest[1] if latest else {})
            return {LISTING_KEY: (what, value, found)}
        _check_alone(patch)
        location = patch.get("location")
        if not isinstance(location, dict):
            return patch
        if "listing" in location:
            raise ToolError("unknown location fields: ['listing']")
        if "address" not in location:
            return patch
        address = location["address"]
        if not (isinstance(address, str) and address.strip()):
            raise ToolError(ADDRESS_EMPTY)
        # its other fields, checked as the change will apply them (the address aside: only the lookup places it)
        profile.apply({}, {"location": {k: v for k, v in location.items() if k != "address"}})
        settings = config.load()
        if not geocode.can_place(settings):
            raise ToolError(geocode.NO_MAP)
        found = geocode.place(conn, settings, address, business_id=biz)
        if found is None:
            raise ToolError(geocode.NOT_PLACED)
        return {**patch, "location": {**location, "listing": geocode.listing(found)}}

    def body(conn, p, patch):  # the patch prepare checked (`_check_alone`) and placed
        biz = need_business(p)
        if LISTING_KEY in patch:  # what prepare made of the owner's word on the listing
            return _listing_body(conn, biz, *patch[LISTING_KEY])
        if "ai_crawlers" in patch:
            conn.execute(
                "UPDATE business SET ai_crawlers = %s, updated_at = now() WHERE id = %s",
                (bool(patch["ai_crawlers"]), biz),
            )
        fields = {k: v for k, v in patch.items() if k in PATCHABLE}
        changes = {k: v for k, v in patch.items() if k in profile.CHANGE_KEYS}
        version = None
        if fields or changes:
            latest = business.latest_knowledge(conn, biz)
            if latest is None:
                raise ToolError("describe the business first, before changing its details")
            merged = {**latest[1], **fields}
            if "name" in fields:  # the owner's own name, vouched for as theirs (plans.name_for, ruling R0-k2)
                merged["name_source"] = "owner"
            # another website than the one on file is a site Admiry has not read: it has no platform yet (A3p-5 batch 2,
            # Q13), and the one read from the old site is not its own
            if "website" in fields and not listings.same_site(fields["website"], latest[1].get("website")):
                merged.pop("website_platform", None)
            if changes:
                merged = profile.apply(merged, changes)
            version = business.add_knowledge(conn, biz, merged, source="owner")
            if "locale" in fields:  # one source of truth for the owner's language
                conn.execute(
                    "UPDATE business SET locale = %s, updated_at = now() WHERE id = %s", (fields["locale"], biz)
                )
        return {"version": version, "updated": sorted(patch)}

    return run(
        "update_business",
        "manage",
        {"patch": patch, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
    )


@agent_tool(cls="W", scope="manage")
def set_goal(
    idempotency_key: str,
    goal_type: str = "customers",
    target: int | None = None,
    monthly_budget_usd: float | None = None,
    focus_services: list[str] | None = None,
    autopilot: str | None = None,
    channel_caps: dict[str, float] | None = None,
    max_cost_per_customer_usd: float | None = None,
) -> dict:
    """Record what the owner wants: new customers per month (target) and/or the monthly advertising
    budget in USD (monthly_budget_usd) — a budget alone is a valid goal — plus services to focus
    on. The budget becomes the monthly limit every plan and every automatic change stays within;
    raising it later needs the owner's approval code. `autopilot`: 'off' (no automatic changes) or
    'safe' (changes within the limit, the default); it stays as set until changed — send the
    current target or budget with it. `channel_caps`: the most the owner allows on a channel a month,
    {"newsbreak": 200} (0 keeps a channel out); the plan's split stays under each, and they too stay as set
    until changed ({} clears them). `max_cost_per_customer_usd`: the most a new customer may cost; it stays as set
    until changed. Call create_plan afterwards (or adjust, for a plan from a consultation)."""

    def body(conn, p):
        biz = need_business(p)
        if goal_type not in ("customers", "calls", "bookings"):
            raise ToolError("goal_type must be customers, calls or bookings")
        if target is None and monthly_budget_usd is None:
            raise ToolError("give a target (customers per month) or a monthly_budget_usd, or both")
        if not business.target_ok(target):
            raise ToolError(f"target must be between 1 and {business.TARGET_MAX:,} customers per month")
        if not business.budget_ok(monthly_budget_usd):
            raise ToolError(f"monthly_budget_usd must be between 0 and {business.BUDGET_MAX:,}")
        if max_cost_per_customer_usd is not None and not business.cost_ok(max_cost_per_customer_usd):
            raise ToolError(f"max_cost_per_customer_usd must be between 1 and {business.COST_MAX:,}")
        focus = focus_services or []
        if len(focus) > business.FOCUS_MAX_ITEMS or any(len(s) > business.FOCUS_MAX_CHARS for s in focus):
            raise ToolError(
                f"focus_services: at most {business.FOCUS_MAX_ITEMS} services of at most "
                f"{business.FOCUS_MAX_CHARS} characters each"
            )
        if autopilot == "all":
            raise ToolError("turning automatic optimization fully on needs set_autopilot, which arrives later")
        if autopilot is not None and autopilot not in AUTOPILOT_MODES:
            raise ToolError("autopilot must be off or safe")
        caps = _check_caps(channel_caps)
        current = business.latest_goal(conn, biz) if autopilot is None else None
        mode = autopilot or (current["autopilot"] if current else "safe")  # omitted: keep the owner's setting
        goal = business.set_goal(
            conn,
            biz,
            goal_type=goal_type,
            target=target,
            monthly_limit_usd=monthly_budget_usd,
            focus_services=focus,
            autopilot=mode,
            channel_caps=caps,  # None (omitted): business.set_goal keeps the owner's caps
            max_cost_per_customer_usd=(
                max_cost_per_customer_usd if max_cost_per_customer_usd is not None else business.KEEP
            ),
        )
        return {"goal": goal}

    return run(
        "set_goal",
        "manage",
        {
            "goal_type": goal_type,
            "target": target,
            "monthly_budget_usd": monthly_budget_usd,
            "focus_services": focus_services,
            "autopilot": autopilot,
            "channel_caps": channel_caps,
            "max_cost_per_customer_usd": max_cost_per_customer_usd,
            "idempotency_key": idempotency_key,
        },
        body,
        idempotency_key=idempotency_key,
    )


def _check_caps(caps: dict | None) -> dict[str, float] | None:
    """The owner's channel ceilings as the goal keeps them: channel ids of the registry's table, amounts within the
    budget bounds; ToolError naming what is wrong."""
    if caps is None:
        return None
    out = {}
    for channel, amount in caps.items():
        if channel not in registry.LABELS:
            raise ToolError("unknown channel")
        if isinstance(amount, bool) or not isinstance(amount, int | float) or not business.budget_ok(float(amount)):
            raise ToolError(f"channel_caps: {channel} must be between 0 and {business.BUDGET_MAX:,} dollars a month")
        out[channel] = float(amount)
    return out


# What a change to the goal may name (change_goal; the public update_marketing_plan reads this list too)
GOAL_CHANGES = (
    "goal_type",
    "target",
    "monthly_limit_usd",
    "focus_services",
    "max_cost_per_customer_usd",
    "channel_caps",
    "autopilot",
)
# What a goal's autopilot may be set to (change_goal and set_goal check it; the public contract lists it): no automatic
# changes, or changes within the limit. "all" arrives later, with its own tool.
AUTOPILOT_MODES = ("off", "safe")
GOAL_WORDS = {"customers": "new customers", "calls": "calls", "bookings": "bookings"}
LIMIT_BELOW_PLAN = (
    "the plan spends {plan} a month, more than a {limit} limit: lower the plan's budget first (and launch that version "
    "if ads are running), then set the limit"
)


def _plan_spends(conn, biz: str) -> float | None:
    """What the plan spends a month: the larger of the latest version's amount and the approved version's -- the ads
    spend the approved one's until a cheaper version is launched. None for a business with no plan."""
    found = [p for p in (plans.latest(conn, biz), ads_run.approved(conn, biz)) if p is not None]
    return max((plans.total_usd(p["sections"]) for p in found), default=None)


def _goal_sentence(goal: dict) -> str:
    """The goal as the owner reads it, everything the owner set: what is wanted, what a customer may cost, what a month
    may cost, what each place may cost (in the order the plan's cards show them), and whether Admiry may change things
    by itself. A limit Admiry set for the plan is not the owner's and is not read back as one."""
    parts = []
    if goal["target"] is not None:
        what = GOAL_WORDS.get(goal["goal_type"], goal["goal_type"])  # a type a later feature writes reads as itself
        parts.append(f"{goal['target']:,} {what} a month")
    if goal["max_cost_per_customer_usd"] is not None:
        parts.append(f"at most {plans.usd(goal['max_cost_per_customer_usd'])} for each new customer")
    if goal["monthly_limit_usd"] is not None and goal["limit_source"] == "owner":
        parts.append(f"no more than {plans.usd(goal['monthly_limit_usd'])} a month")
    caps = goal["channel_caps"]
    for channel in registry.ORDER:  # a cap for a place the registry does not name is left out, not an error
        if channel in caps:
            parts.append(f"no more than {plans.usd(caps[channel])} a month on {registry.label(channel)}")
    if goal["autopilot"] == "off":  # `safe` is the default and says nothing; so does a value this code does not know
        parts.append("no automatic changes")
    if not parts:
        return "The goal has no target and no limit of yours now."
    return "The goal now reads: " + ", ".join(parts) + "."


def _number(value, low: float, high: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and low <= value <= high


@agent_tool(cls="W", scope="manage")
def change_goal(changes: dict, idempotency_key: str) -> dict:
    """Change part of the goal and keep the rest. `changes` names only what changes: goal_type (customers, calls or
    bookings), target (how many a month; null removes it), monthly_limit_usd (the owner's own monthly limit from
    then on; null removes it), max_cost_per_customer_usd (the most a new customer may cost; null removes it),
    focus_services, channel_caps ({"newsbreak": 200}; {} clears them), autopilot (off or safe). What is not named
    stays as it is, a limit Admiry set for the plan included. The plan itself does not change. Use a new
    idempotency_key per change."""

    def body(conn, p):
        biz = need_business(p)
        # read-modify-write of the latest goal: one change at a time per business, so two patches never lose one
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"goal:{biz}",))
        if not isinstance(changes, dict) or not changes:
            raise ToolError(f"changes must name at least one of {', '.join(GOAL_CHANGES)}")
        unknown = sorted(set(changes) - set(GOAL_CHANGES))
        if unknown:
            raise ToolError(f"unknown goal fields: {unknown}; a goal has {', '.join(GOAL_CHANGES)}")
        held = business.latest_goal(conn, biz) or {}
        # only what a change names is checked: what the goal holds passes through as it is
        goal_type = held.get("goal_type") or "customers"
        if "goal_type" in changes:
            goal_type = changes["goal_type"]
            if not isinstance(goal_type, str) or goal_type not in GOAL_WORDS:  # a list or object is unhashable
                raise ToolError("goal_type must be customers, calls or bookings")
        target = held.get("target")
        if "target" in changes:  # null removes it; 20.0 is 20, 12.5 and true are not whole numbers
            target = changes["target"]
            if target is not None:
                if not _number(target, 1, business.TARGET_MAX) or not float(target).is_integer():
                    raise ToolError(f"target must be a whole number between 1 and {business.TARGET_MAX:,} a month")
                target = int(target)
        limit, source = held.get("monthly_limit_usd"), held.get("limit_source") or "owner"
        if "monthly_limit_usd" in changes:  # the owner's own limit from now on; null removes it
            limit, source = changes["monthly_limit_usd"], "owner"
            # 0 is no limit: build and recommend read it so, while the goal's sentence would read "no more than $0"
            if limit is not None and not (_number(limit, 0, business.BUDGET_MAX) and limit > 0):
                raise ToolError(
                    f"monthly_limit_usd must be above 0 and at most {business.BUDGET_MAX:,}; null removes the limit"
                )
            spends = _plan_spends(conn, biz) if limit is not None else None
            if spends is not None and limit < spends:  # final review B, Important 7: never a limit the plan breaks
                raise ToolError(LIMIT_BELOW_PLAN.format(plan=plans.usd(spends), limit=plans.usd(limit)))
        ceiling = changes.get("max_cost_per_customer_usd", business.KEEP)
        if ceiling is not business.KEEP and not business.cost_ok(ceiling):
            raise ToolError(f"max_cost_per_customer_usd must be between 1 and {business.COST_MAX:,}")
        focus = held.get("focus_services") or []
        if "focus_services" in changes:
            focus = changes["focus_services"]
            if (
                not isinstance(focus, list)
                or len(focus) > business.FOCUS_MAX_ITEMS
                or any(not isinstance(s, str) or len(s) > business.FOCUS_MAX_CHARS for s in focus)
            ):
                raise ToolError(
                    f"focus_services: a list of at most {business.FOCUS_MAX_ITEMS} services of at most "
                    f"{business.FOCUS_MAX_CHARS} characters each"
                )
        autopilot = held.get("autopilot") or "safe"
        if "autopilot" in changes:
            autopilot = changes["autopilot"]
            if autopilot not in AUTOPILOT_MODES:
                raise ToolError("autopilot must be off or safe")
        caps = changes.get("channel_caps")
        if caps is not None and not isinstance(caps, dict):
            raise ToolError("channel_caps must be an object: a channel and its dollars a month")
        goal = business.set_goal(
            conn,
            biz,
            goal_type=goal_type,
            target=target,
            monthly_limit_usd=float(limit) if limit is not None else None,
            focus_services=focus,
            autopilot=autopilot,
            limit_source=source,
            channel_caps=_check_caps(caps),  # None (not named): business.set_goal keeps the owner's caps
            max_cost_per_customer_usd=ceiling,
        )
        return envelope.make({"goal": goal}, say=_goal_sentence(goal))

    return run(
        "change_goal",
        "manage",
        {"changes": changes, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
    )


@agent_tool(cls="RO", scope="manage")
def request_connect_link() -> dict:
    """A fresh link for the owner to sign in with Google and claim this business (24 hours), when the
    one from `start` was lost or expired; every call makes a new one (earlier links stay valid until used
    or expired). After the claim, the owner manages keys and approves plans on connect_url themselves;
    returns the plain /connect page once the business is claimed. Needs the manage scope, so read-only
    keys cannot hand out claim links."""

    def body(conn, p):
        biz = need_business(p)
        if business.row(conn, biz)["claim_state"] == "draft":
            url, expires_at = claims.new_link(conn, biz)
            return {"connect_url": url, "expires_at": iso(expires_at)}
        return {
            "connect_url": f"{config.load().web_base_url}/connect",
            "expires_at": None,
            "note": "this business is already claimed; the owner signs in at connect_url to manage it",
        }

    return run("request_connect_link", "manage", {}, body)
