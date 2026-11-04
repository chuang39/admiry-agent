"""The launch checklist (L1, spec 2026-10-10 §4): every reason the latest draft cannot launch yet, at once -- each with
what Admiry suggests and the field or the one-tap fix that resolves it (B0's resolutions, agent/blockers.py) -- read by
the plan card before it says Ready to launch (`GET /api/launch_check`), by the plan card's yes and Agree and pay (the
first blocking problem refuses them), and by typed words (the first open question takes them). Database only: no page
is fetched -- a page with no report, or one that predates a rule a place needs, is an item whose fix runs the check.
The money and the account are no items: they are the step after the list (Agree and pay, Agree and launch). Every place
the plan funds is read whatever its switch says: the switch decides what is sent to Google, never what is checked. A
place other than Google Search never blocks the others (D77): its own rules are listed, `blocking` false."""

from __future__ import annotations

import copy

import psycopg
from fastmcp.exceptions import ToolError

from . import ads_refusal, ads_run, allocate, blockers, business, config, landing, launch_review, local_rules, plans
from .channels import registry
from .channels.google_search import adapter as google
from .channels.google_search import build as google_build

# L1 (plan D8): the admin's word on a version Admiry returned or rejected, the first item of its list -- blocking
# only once rejected (R15 c): a returned one may need nothing on the plan itself (the owner's own page, say), and
# agreeing again sends it back to review (Task 9's Reviewer holds a version a person returned before).
NOTE_TEXT = "Admiry looked at your ads before they went live: {note}"
NOTE_SUGGESTION = "Change the plan, or write to hello@admiry.ai."


def note_text(note: str) -> str:
    """NOTE_TEXT with `note` ending in exactly one end mark: its own ".", "!" or "?" when it already has one, else a
    period added (L1 review M5: a blind `rstrip(".") + "."` turned "Why?" into "Why?.")."""
    said = note.strip()
    if not said.endswith((".", "!", "?")):
        said += "."
    return NOTE_TEXT.format(note=said)


# What Admiry suggests for each resolution (plan_review.RESOLUTIONS' ids), one line under the problem
SUGGESTIONS = {
    "write_copy": "Admiry writes the headlines and descriptions from what it knows about your business.",
    "check_page": "Admiry checks your page now; it takes a few seconds.",
    # page-first review, item 1: the three ways, the owner's to choose -- never "free" (hosting is free only while the
    # ads spend at least $200 a month: hosting_minimum.MINIMUM)
    "page": (
        "Admiry can build your page, your assistant can design one that Admiry hosts, or the ads can use the website "
        "you already have."
    ),
    "phone": "Calls from your ads ring this number.",
    "location": "Your ads centre on this address.",
    "radius": "How far around the business your ads reach.",
    "budget": "Pick a budget that fits; you can change it any time before you agree.",
    "fresh_plan": "Admiry draws the plan again from your answers.",
}
HEADLINE_SUGGESTION = "Admiry puts the service and the city in your page's headline; you publish it."
CHECK_AGAIN_SUGGESTION = "Once it is changed, check again."
CAN_WAIT = "{label} starts once this is fixed; the rest can start without it."

# Part B phase 1 (spec 2026-10-12 §2.1, §2.3; plan D7): a line Google refused for this business and the draft still
# carries, and a line an active rule catches -- each an item with the rewrite fix, in the owner's words
REFUSED_TEXT = "Google refused “{line}” before: {why}."
RULE_TEXT = {
    "headline": "Google often refuses headlines like “{line}”: {why}.",
    "description": "Google often refuses descriptions like “{line}”: {why}.",
    "keyword": "Google often refuses search phrases like “{line}”: {why}.",
}
WARN_NOTE = "Your ads can start as they are; Admiry can rewrite this line first."


def _own(adapter, conn: psycopg.Connection, settings: config.Settings, biz: str, plan: dict, knowledge: dict) -> list:
    """A place's own rules, every one: the adapter's `refusals` when it lists them (Google Search), else the one its
    `precheck` refuses with."""
    listed = getattr(adapter, "refusals", None)
    if listed is not None:
        return listed(conn, settings, biz, plan, knowledge)
    try:
        adapter.precheck(conn, settings, biz, plan, knowledge)
    except ToolError as e:
        return [str(e)]
    return []


def _places(plan: dict) -> list[str]:
    """The places the list reads: every one the plan funds that Admiry can start itself, Google Search first (R9)."""
    places = [c for c in ads_run.funded_channels(plan) if ads_run._startable(c)]
    return sorted(places, key=lambda c: c != allocate.PRIMARY)  # R9: Google Search first, wins a shared refusal


def _found(conn: psycopg.Connection, settings: config.Settings, biz: str, plan: dict) -> list[dict]:
    """{text, rule, channel, blocking} for every refusal, in the launch's order (the plan's own rules, then each place's
    own, its page's and its place-and-phone rules), each sentence once -- blocking when any place it came from does.
    Google Search is read before the rest, whatever the plan's own channel order (review Finding 1, ruling R9): a
    refusal two places share (the same page rule failing both) is credited to Google Search -- its id, label,
    channel and blocking -- never to a secondary place picked only because the plan happened to list it first."""
    knowledge = (business.latest_knowledge(conn, biz) or (None, {}))[1]
    found = [{"text": t, "rule": None, "channel": None} for t in ads_run.plan_refusals(conn, biz, plan)]
    places = _places(plan)
    for channel in places:
        listed = [(t, None) for t in _own(registry.adapter(channel), conn, settings, biz, plan, knowledge)]
        listed += ads_run.page_refusals(conn, biz, channel)
        if not ads_run.has_ads(conn, biz, channel):  # ruling S8: ads already built need only a page address
            listed += [(t, None) for t in ads_run.place_and_phone(plan, channel, knowledge)]
        found += [{"text": t, "rule": rule, "channel": channel} for t, rule in listed]
    primary = allocate.PRIMARY in places
    once: dict[str, dict] = {}
    for f in found:
        f["blocking"] = f["channel"] in (None, allocate.PRIMARY) or not primary
        if f["text"] in once:
            once[f["text"]]["blocking"] |= f["blocking"]
        else:
            once[f["text"]] = f
    return list(once.values())


def _item_id(f: dict, described: dict) -> str:
    """The item's id: an `ask` (phone, location, radius) is its bare resolution whatever place the refusal came from
    (review Finding 1, ruling R10) -- B0's readers (blockers.read_answer, .SHAPES, ._saved, .read_typed) key on the
    id unmodified, and an ask's sentence is place-independent text (ads_run.NO_PHONE and friends: never formatted
    with a place's label), so `_found`'s dedup by text already keeps one ask of each resolution on the list -- the
    bare id never collides. Every other kind still gets `@place` suffixed when the place is not Google Search, so
    two different problems from two places don't collide."""
    if f["rule"]:
        base = f"page:{f['rule']}"
    elif described["resolution"] == "budget":
        base = "limit" if f["text"] == ads_run.OVER_LIMIT else "floor"
    else:
        base = described["resolution"] or "other"
    if described["kind"] == "ask" or f["channel"] in (None, allocate.PRIMARY):
        return base
    return f"{base}@{f['channel']}"


def _suggestion(described: dict) -> str | None:
    if described["resolution"] == "page_fix":
        headline = any(a["id"] == blockers.FIX_HEADLINE["id"] for a in described["actions"])
        return HEADLINE_SUGGESTION if headline else CHECK_AGAIN_SUGGESTION
    return SUGGESTIONS.get(described["resolution"] or "")


def _unique(base: str, ids: set[str]) -> str:
    """`base`, or base-2, base-3, … -- the first not in `ids`, which it joins."""
    item_id, n = base, 2
    while item_id in ids:
        item_id, n = f"{base}-{n}", n + 1
    ids.add(item_id)
    return item_id


def _rewrite_item(plan: dict, item_id: str, text: str, *, blocking: bool, line_kind: str) -> dict:
    """An item of Google Search's whose fix is Admiry's rewrite of the ad text (resolution write_copy, B0's button;
    the assistant's update_marketing_plan with COPY_INSTRUCTION through fix_for) -- except for a search phrase
    (`line_kind == "keyword"`): write_copy only rewrites headlines and descriptions (a search phrase comes from the
    themes, never from write_copy's draft), so a keyword item carries no WRITE_COPY action -- its suggestion stands
    alone, and `fix_for` gives no fix for it either (ruling BB1)."""
    return {
        "id": item_id,
        "plan_id": plan["id"],
        "channel": allocate.PRIMARY,
        "label": registry.label(allocate.PRIMARY),
        "text": text,
        "suggestion": SUGGESTIONS["write_copy"],
        "kind": "fix",
        "resolution": "write_copy",
        "question": None,
        "actions": [] if line_kind == "keyword" else [blockers.WRITE_COPY],
        "blocking": blocking,
        "note": None if blocking else WARN_NOTE,
    }


def _thin_after(conn: psycopg.Connection, settings: config.Settings, business_id: str, plan: dict) -> bool:
    """Whether leaving out what Google refused and what the block rules catch -- after the copy linter, as a build
    does (google_build.kept_lines) -- leaves fewer lines than Google takes."""
    sent = ads_refusal.carried(conn, business_id, plan)
    business_ = (plan["sections"].get("consultation") or {}).get("business") or {}
    kept_h, kept_d, _notes = google_build.kept_lines(
        sent.get("headline", []),
        sent.get("description", []),
        landing.load_recipe(business_.get("category") or "fallback")["banned"],
        ads_refusal.dropped(conn, business_id, settings=settings),
    )
    return len(kept_h) < plans.MIN_HEADLINES or len(kept_d) < plans.MIN_DESCRIPTIONS


def refused_items(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, plan: dict, ids: set[str]
) -> list[dict]:
    """Spec 2026-10-12 §2.1 and §2.3 (plan D7, D8): one item per line of the draft that Google refused for this
    business before, by its exact text (`refused_line`, `refused_line-2`, …: "Google refused “…” before: <why>",
    `ads_refusal.why_for`), then one per line an active rule catches (`rule:<id>`, …: "Google often refuses
    headlines like “…”: <why>"), each with the rewrite fix. A refused line blocks only when the exclusion leaves fewer
    lines than Google takes (`_thin_after`) and the copy linter alone does not already (then TOO_LITTLE_COPY's own
    item blocks: `google.copy_short`); a `block` finding on a headline or a description blocks; a `warn` finding, and
    any finding on a search phrase (the build leaves it out; the home's rewrite swaps the ad text alone), never does.
    `ids` is the list's ids so far, which these join. Nothing for a draft with no runnable Google Search entry."""
    exact, found = ads_refusal.flagged(conn, settings, business_id, plan)
    if not exact and not found:
        return []
    # _thin_after costs a build's own kept_lines read -- worth it only when a refused line is actually carried
    # (`exact`); a rule finding alone never blocks on it (see the loop below)
    thin = bool(exact) and _thin_after(conn, settings, business_id, plan) and not google.copy_short(plan)
    out: list[dict] = []
    for kind, text in exact:
        why = ads_refusal.why_for(conn, business_id, kind, text)
        out.append(
            _rewrite_item(
                plan,
                _unique("refused_line", ids),
                REFUSED_TEXT.format(line=text, why=why),
                blocking=thin,
                line_kind=kind,
            )
        )
    for f in found:
        blocking = f.level == local_rules.BLOCK and f.line_kind != "keyword"
        text = RULE_TEXT[f.line_kind].format(line=f.text, why=f.why)
        out.append(
            _rewrite_item(plan, _unique(f"rule:{f.rule_id}", ids), text, blocking=blocking, line_kind=f.line_kind)
        )
    return out


def items(conn: psycopg.Connection, settings: config.Settings, business_id: str, plan: dict) -> list[dict]:
    """The plan's items: a returned or rejected review's note first when there is one (plan D8), blocking only once
    rejected (R15 c); then the rest in the launch's order (see the module docstring)."""
    out: list[dict] = []
    ids: set[str] = set()
    for f in _found(conn, settings, business_id, plan):
        described = blockers.describe(conn, business_id, plan, f["text"])
        item_id, n = _item_id(f, described), 2
        while item_id in ids:  # two refusals no row knows: other, other-2
            item_id, n = f"{_item_id(f, described)}-{n}", n + 1
        ids.add(item_id)
        label = registry.label(f["channel"]) if f["channel"] else None
        out.append(
            {
                "id": item_id,
                "plan_id": plan["id"],
                "channel": f["channel"],
                "label": label,
                "text": f["text"],
                "suggestion": _suggestion(described),
                "kind": described["kind"],
                "resolution": described["resolution"],
                "question": described["question"],
                "actions": described["actions"],
                "blocking": f["blocking"],
                "note": None if f["blocking"] else CAN_WAIT.format(label=label),
            }
        )
    out += refused_items(conn, settings, business_id, plan, ids)  # spec 2026-10-12 §2.1, §2.3
    said = launch_review.note_for(conn, business_id, plan["id"])
    if (
        said and said["note"]
    ):  # a person's or the Reviewer's word on this version comes first (plan D8); blocking only once rejected
        resolution = said.get("resolution")
        if resolution not in launch_review.RESOLUTIONS:
            # a resolution decide() no longer writes (a hand-written or an older row): the plain note, never a
            # KeyError from blockers.controls's own fixed[resolution] (ruling F1, task review)
            resolution = None
        # spec 2026-10-11 §5 (plan D5): the Reviewer's return names its fix -- an ask (the phone) is the ask item, by its
        # bare id (R10), unless the plain ask is listed already; the page check is a fix item; an admin's note is a note
        kind = "ask" if resolution in blockers.INPUTS else "fix" if resolution else "note"
        if kind == "ask" and any(i["id"] == resolution for i in out):
            kind = "note"
        controls = blockers.controls(conn, business_id, plan, resolution) if kind != "note" else {}
        out.insert(
            0,
            {
                "id": resolution if kind == "ask" else "review_note",
                "plan_id": plan["id"],
                "channel": None,
                "label": None,
                "text": note_text(said["note"]),
                "suggestion": (SUGGESTIONS.get(resolution or "") if kind != "note" else None) or NOTE_SUGGESTION,
                "kind": kind,
                "resolution": resolution if kind != "note" else None,
                "question": controls.get("question"),
                "actions": controls.get("actions", []),
                "blocking": said["state"] == "rejected",
                "note": None,
            },
        )
    return out


def check(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> dict | None:
    """The latest plan's checklist while it is a draft: {plan_id, items, ready} -- ready when no item blocks; None for
    no plan, or one that is no draft (in review, approved)."""
    plan = plans.latest(conn, business_id)
    if plan is None or plan["status"] != plans.DRAFT:
        return None
    found = items(conn, settings, business_id, plan)
    return {"plan_id": plan["id"], "items": found, "ready": not any(i["blocking"] for i in found)}


def blocked(conn: psycopg.Connection, settings: config.Settings, business_id: str, plan_id: str) -> str | None:
    """What keeps the owner from agreeing to `plan_id` now: blockers.PLAN_MOVED when it is no longer the latest draft,
    else the first blocking problem, else None."""
    out = check(conn, settings, business_id)
    if out is None or out["plan_id"] != plan_id:
        return blockers.PLAN_MOVED
    return next((i["text"] for i in out["items"] if i["blocking"]), None)


def first_ask(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> dict | None:
    """The list's first open question (an `ask` item): what typed words are read against (B0 §3.3)."""
    out = check(conn, settings, business_id)
    return next((i for i in (out or {}).get("items", []) if i["kind"] == "ask"), None)


# ---------------------------------------------------------------- the list as an assistant reads it (spec 2026-10-11 §3)
# One public tool per resolution, its arguments -- `<…>` where only the owner knows the value, with `ask_owner`, the
# question an assistant asks first -- and the words of its action, as the home shows them (plan D7). Money and the
# account are no items (the steps); a refusal no row knows and a plain note have no fix: relay their problem, and
# their suggestion when they have one.
FIX_TOOLS = {
    "phone": "update_business_profile",
    "location": "update_business_profile",
    "radius": "update_marketing_plan",
    "write_copy": "update_marketing_plan",
    "fresh_plan": "update_marketing_plan",
    "budget": "update_marketing_budget",
    "check_page": "get_landing_page",
    "page_fix": "update_landing_page",
    "page": "update_landing_page",
}
# The founder's ruling (2026-10-13, from a real Muse walk): the page comes before the money -- an owner without a page
# settles it before Admiry asks how the ads run or takes any payment, and before any launch -- by one of three ways, the
# owner's to choose (never preselected): Admiry builds it (`update_landing_page` build_site, Admiry's own website for a
# home-services business or a dental practice, else write, the page Admiry writes: `page_choices`); the owner's
# assistant designs it and Admiry hosts it (upload_site: the first call answers the upload's link, the command to run,
# the script tag every page carries and the limits -- preview_html, one page, is the other road there, and publish
# follows both); or the website the owner already has (use_url, its address only the owner knows) -- each offered only
# where Admiry can take it (`page_choices`: what Admiry hosts needs hosting on), the question naming only those
# (`page_question`). The page item's fix asks it (`fix_for`), and while the latest draft has no page settled
# (`page_first`) get_marketing_state, get_marketing_plan's and update_marketing_plan's answers and connect_account ask
# it in place of how the ads run, and get_billing's payment link in place of the link -- each by `page_ask`, which,
# once a page is made and waits to be published (`page_in_hand`; final review, item 1), leads to publishing it instead.
PAGE_WAYS = (
    # `says`: the way in the page question's words (`page_question`), in this order
    {"id": "admiry_builds", "label": "Admiry builds it for me", "args": None, "says": "Admiry can build one for you"},
    {
        "id": "assistant_designs",
        "label": "My assistant designs it, Admiry hosts it",
        "args": {"action": "upload_site"},
        "says": "your assistant can design one and Admiry hosts it",
    },
    {
        "id": "own_website",
        "label": "I already have a website",
        "args": {"action": "use_url", "url": "<the owner's page address>"},
        "says": "the ads can use the website you already have",
    },
)
PAGE_ASK = "Which page should your ads send people to?"
PAGE_ADDRESS = "For your own website, what is its address?"
# the page item's fix names no way (page-first review, item 1): its choices are the ways, none first
PAGE_LABEL = "Choose the page for your ads"
# A page in hand (final review, item 1): a page made and not published yet is not asked for again -- the page step leads
# to publishing it, by the first of the two calls update_landing_page's "publish" takes (with its preview's token), the
# step the checklist's headline fix already offers for a preview it made (`fix_for`)
PAGE_READY = "Your page is ready to look at. Publish it when you are happy with it, then the ads can use it."
PUBLISH_LABEL = "Publish the page"
# Admiry's own site under way and not ready to publish (`page_in_hand`): the site's own next step -- build_site again
# (its next question, or a build once more), or its view, which carries the link where the owner adds the photos
SITE_BUILD = "Build my site"  # the home's site card says the same (home_api.BUILD_SITE, BUILD_AGAIN)
SITE_AGAIN = "Build it again"


def _way(choice: dict) -> dict:
    """The way of PAGE_WAYS a choice (`page_choices`) is, by its id -- never its label, which may be said in new words
    (final review, item 3)."""
    return next(way for way in PAGE_WAYS if way["id"] == choice["id"])


def page_question(choices: list[dict]) -> str:
    """The page question for the ways offered (`page_choices`), each in its own words, in their order: PAGE_QUESTION
    when all three are."""
    said = [_way(choice)["says"] for choice in choices]
    ways = said[0] if len(said) == 1 else f"{', '.join(said[:-1])}, or {said[-1]}"
    return f"{PAGE_ASK} {ways[0].upper()}{ways[1:]}. {PAGE_ADDRESS}"


PAGE_QUESTION = page_question([{"id": way["id"]} for way in PAGE_WAYS])
ASK_OWNER = {
    "phone": "What phone number should people call?",
    "location": "What address should the ads centre on?",
    "radius": "How many miles around the business should the ads reach?",
    "budget": "Which budget: {choices}?",
}
ASK_ARGS = {
    "phone": {"patch": {"locations": [{"phone": "<the number customers call>"}]}},
    "location": {"patch": {"locations": [{"address": "<the address, or the city and ZIP code>"}]}},
    "radius": {"changes": {"radius_miles": "<the miles, 1 to 100>"}},
}
ASK_LABELS = {"phone": "Give the phone number", "location": "Give the address", "radius": "Say how far"}
BUDGET_ARGS = {"monthly_budget_usd": "monthly_usd"}  # adjust's names -> update_marketing_budget's
CHECK_ARGS = {"view": "check"}


def page_settled(conn: psycopg.Connection, business_id: str, plan: dict) -> bool:
    """Whether the business's page is settled for `plan` -- the checklist's page item passes: every place the list
    reads has a page to send people to (`ads_run.has_page`, the NO_PAGE of `ads_run.page_refusals`). Whether that page
    passes its check is the list's other items."""
    return all(ads_run.has_page(conn, business_id, channel) for channel in _places(plan))


def page_first(conn: psycopg.Connection, business_id: str, plan: dict | None = None) -> bool:
    """Whether the page question comes first now: the latest plan -- `plan`, when the caller already read it, else read
    here -- is a draft (the list's own scope) and its page is not settled (`page_settled`). A business with no plan yet
    has nothing to build a page from (landing.NO_PLAN). The one spelling of this rule (page-first review, item 4): the
    state's steps, a read of the plan, the plan answers, connect_account and the money all ask it here."""
    if plan is None:
        plan = plans.latest(conn, business_id)
    return plan is not None and plan["status"] == plans.DRAFT and not page_settled(conn, business_id, plan)


def page_choices(conn: psycopg.Connection, business_id: str) -> list[dict]:
    """PAGE_WAYS as the owner's choices ({id, label, args}, in their order, fresh copies; the way's `id` is what finds
    it again, final review item 3), each offered only where Admiry can take it (page-first review, item 6): Admiry's
    way is build_site where `write` would start it itself (tools_sites.takes_over: Admiry's own sites on, a
    home-services business or a dental practice, no page Admiry wrote yet) -- a site Admiry hosts, so only while
    hosting is on (AGENT_PAGES_DOMAIN) -- else write; the assistant-designed way is a site Admiry hosts (upload_site),
    offered only while hosting is on; the owner's own website always."""
    from . import tools_sites  # a tool module: read here, never at import time (see fix_for's own import)

    hosting = bool(config.load().pages_domain)
    site = tools_sites.takes_over(conn, business_id)
    builds = {"action": "build_site"} if site else {"action": "write"}
    offered = {"admiry_builds": hosting or not site, "assistant_designs": hosting, "own_website": True}
    return [
        {"id": way["id"], "label": way["label"], "args": dict(way["args"] or builds)}
        for way in PAGE_WAYS
        if offered[way["id"]]
    ]


def page_in_hand(conn: psycopg.Connection, business_id: str) -> tuple[str, list[dict]] | None:
    """The business's own page still to publish (final review, item 1), as what the page step says and its one choice
    ({id, label, args}, and `tool` when it is not update_landing_page); None when there is none -- nothing made yet, or
    a page already published. The page on file (landing.current: the published one, else the newest) not published:
    - Admiry's own site under way (tools_sites.takes_over, and the page a site's: photos.is_site), where its view says
      it stands (tools_sites.site_view): a preview nothing holds back from going live (sites.build.publish_gate) is
      ready to publish -- PAGE_READY and its token; else the site's own words (its view's own sentence) and its own
      next step: its view, which carries the link where the owner adds the logo and the photo it still waits for, or
      build_site, which asks the look or the next fact, or builds it once more (an ended preview; the photos in since);
    - any other page with a preview that has not ended (landing.newest_preview: a page Admiry wrote, one the owner's
      assistant put on a preview or a site it uploaded whole) is ready to publish -- PAGE_READY and that preview's
      token: update_landing_page's "publish", whose first call says what will change and whose second, with the
      owner's yes, publishes it."""
    page = landing.current(conn, business_id)
    if page is None or page["status"] == "published":
        return None
    from . import hosted, tools_sites  # tool modules: read here, never at import time (see fix_for's own import)
    from .sites import build, photos

    if photos.is_site(page) and tools_sites.takes_over(conn, business_id):
        view = tools_sites.site_view(conn, config.load(), business_id)
        preview = view["preview"]
        if preview is not None and build.publish_gate(conn, hosted.preview(conn, preview["token"])) is None:
            token = preview["token"]
        else:
            if preview is not None and not view["key_photos"]["ok"]:  # the logo or the key photo still to add
                step = {
                    "id": "site",
                    "label": tools_sites.UPLOAD_LABEL,
                    "tool": "get_landing_page",
                    "args": {"view": "site"},
                }
            else:  # the look or a fact to ask, a site to build, an ended preview, or the photos in since its build
                label = SITE_AGAIN if view["status"] == "preview" else SITE_BUILD
                step = {"id": "build_site", "label": label, "args": {"action": "build_site"}}
            return tools_sites.state_say(conn, view), [step]
    else:
        token = landing.newest_preview(conn, page["id"])
        if token is None:
            return None
    return PAGE_READY, [{"id": "publish", "label": PUBLISH_LABEL, "args": {"action": "publish", "token": token}}]


def page_ask(conn: psycopg.Connection, business_id: str) -> tuple[str, list[dict]]:
    """What the page step asks while the page comes first (`page_first`), as (ask_owner, choices): the page in hand's
    own step (`page_in_hand`), else the page question and the ways Admiry can take (`page_question`,
    `page_choices`). Every place that asks the page reads it here: the state's page step, the checklist's page item,
    a plan answer (surface/where.owner_choice), connect_account's page_first and get_billing."""
    in_hand = page_in_hand(conn, business_id)
    if in_hand is not None:
        return in_hand
    choices = page_choices(conn, business_id)
    return page_question(choices), choices


def page_actions(choices: list[dict]) -> list[dict]:
    """The page's choices (`page_ask`) as `next` actions an assistant reads: update_landing_page (or the choice's own
    `tool`) with each one's arguments, side by side, none marked first."""
    return [
        {
            "id": choice["id"],
            "label": choice["label"],
            "tool": choice.get("tool", "update_landing_page"),
            "args": dict(choice["args"]),
        }
        for choice in choices
    ]


def _public_args(args: dict) -> dict:
    return {BUDGET_ARGS.get(key, key): value for key, value in args.items()}


def _or(labels: list[str]) -> str:
    return f"{', '.join(labels[:-1])} or {labels[-1]}" if len(labels) > 1 else labels[0]


def fix_for(
    conn: psycopg.Connection, business_id: str, item: dict, *, page: tuple[str, list[dict]] | None = None
) -> dict | None:
    """The one public tool that fixes `item` (an entry of `items`), its arguments and the question to ask first
    (spec 2026-10-11 §3; plan D7): {tool, args, ask_owner, label} -- `choices` too for a budget and for the page, each
    {label, args}, the first of them in `args` (the page's also carry their `id`, and the page in hand's one step its
    `tool` when it is not update_landing_page: `page_ask`, which `page` is when the caller already read it). None for an
    item no tool fixes: relay its problem, and its suggestion when it has one -- a page_fix with no consulted plan to
    read a headline from is one such item, H2b. One success return point, so the tool it names is read once (FIX_TOOLS,
    checked whole against the public surface in test_surface_contract.py). `args` is always a fresh copy (deep, for the
    module's own nested tables: ASK_ARGS) -- never the module's own dict, which an in-process caller filling it in would
    otherwise mutate for every later call, carrying one business's phone into the next (task review H3).

    Follow-up 2026-10-12: Admiry's own SITE now gets a fix too (ruling I3's own gap) -- the public
    update_landing_page's "fix_headline" action (no arguments: it finds the same item itself) -- converging to
    publish the same way the starter page does (ruling I3a) once the site's own last build that passed already
    carries this headline and its preview has not ended (tools_sites.pending_preview, which a site's preview must
    read differently than landing.pending_preview does for the starter page: see its own docstring).

    Follow-up (ruling BB1): a `write_copy` item with no actions is a search-phrase finding (write_copy rewrites
    headlines and descriptions alone) -- no tool fixes it, so this gives None too, same as any other item no fix
    reaches; its suggestion is the only thing an assistant can relay."""
    resolution = item["resolution"]
    if resolution not in FIX_TOOLS or (resolution == "write_copy" and not item["actions"]):
        return None
    tool = FIX_TOOLS[resolution]
    label = item["actions"][0]["label"] if item["actions"] else ASK_LABELS.get(resolution, "Fix it")
    choices = None
    if resolution in ASK_ARGS:
        args, ask_owner = copy.deepcopy(ASK_ARGS[resolution]), ASK_OWNER[resolution]
    elif resolution == "write_copy":
        args, ask_owner = {"message": blockers.COPY_INSTRUCTION}, None
    elif resolution == "fresh_plan":
        args, ask_owner = {"message": blockers.FRESH_INSTRUCTION}, None
    elif resolution == "check_page":
        args, ask_owner = dict(CHECK_ARGS), None
    elif resolution == "page_fix":
        page = landing.current(conn, business_id)
        if not any(a["id"] == blockers.FIX_HEADLINE["id"] for a in item["actions"]):  # the owner's own page
            tool, args, ask_owner = FIX_TOOLS["check_page"], dict(CHECK_ARGS), None
        else:
            headline = landing.headline_pattern(conn, business_id)
            if headline is None:
                # no consulted plan to compute it from: fix: null (H2b) -- never a placeholder with no ask_owner,
                # and never the check-again fix either (re-checking an unchanged page only fails again, looping an
                # assistant that follows "fix, then read the state again"); the item's own suggestion already says
                # what Admiry does, so the assistant relays the problem and the suggestion instead
                return None
            if blockers.admiry_page(conn, page) == "site":
                # a tool module: imported here, not at import time (review round 1, M1 -- as blockers.write_copy
                # imports its own tool modules) -- a module-level import here would make importing launch_check (or
                # agent.setup) load mcp_app and every tools_* module before surface.load() can run, leaving the
                # public surface empty for a process that imports launch_check first.
                #
                # follow-up 2026-10-12: the public fix_headline action now fixes Admiry's own site too (ruling I3's
                # own gap) -- the same convergence the starter page has below (ruling I3a), read the site's own way
                # (tools_sites.pending_preview)
                from . import tools_sites

                pending = tools_sites.pending_preview(conn, page["id"], headline) if page else None
                if pending is not None:
                    args, ask_owner, label = {"action": "publish", "token": pending["token"]}, None, PUBLISH_LABEL
                else:
                    args, ask_owner = {"action": "fix_headline"}, None
            else:
                pending = landing.pending_preview(conn, page["id"]) if page else None
                if pending is not None and pending["headline"] == headline:
                    # ruling I3: an edit already made the preview Admiry would ask for again -- what is left is the
                    # owner's yes to publish it, not the same edit once more (fix, then the state, then the same
                    # fix would otherwise loop forever); `tool` is already "update_landing_page"
                    # (FIX_TOOLS["page_fix"])
                    args, ask_owner, label = {"action": "publish", "token": pending["token"]}, None, PUBLISH_LABEL
                else:
                    args, ask_owner = {"action": "edit", "section": "headline", "text": headline}, None
    elif resolution == "budget":
        choices = [{"label": a["label"], "args": _public_args(a["args"])} for a in item["actions"]]
        if not choices:
            return None
        args = dict(choices[0]["args"])
        ask_owner = ASK_OWNER["budget"].format(choices=_or([c["label"] for c in choices]))
    elif resolution == "page":  # decide, no page yet: which of the ways (the founder's ruling, 2026-10-13)
        ask_owner, choices = page if page is not None else page_ask(conn, business_id)
        first = choices[0]
        tool, args = first.get("tool", tool), dict(first["args"])
        # the label names no way (page-first review, item 1): the choices are the ways, none first; a page in hand's
        # one step (final review, item 1) is said by its own label
        label = PAGE_LABEL if first["id"] in {way["id"] for way in PAGE_WAYS} else first["label"]
    else:
        return None
    out = {"tool": tool, "args": args, "ask_owner": ask_owner, "label": label}
    if choices is not None:
        out["choices"] = choices
    return out


def public(
    conn: psycopg.Connection,
    settings: config.Settings,
    business_id: str,
    *,
    page: tuple[str, list[dict]] | None = None,
) -> dict | None:
    """The checklist as an assistant reads it (get_marketing_state's `setup.checklist`): `check`'s list with each
    item's problem and suggestion (owner words), its place, whether it blocks, and its fix (`fix_for`, given `page`:
    what the page step asks, `page_ask`, when the caller already read it -- tools_state._setup does; final review,
    item 5); None without a draft."""
    found = check(conn, settings, business_id)
    if found is None:
        return None
    items_ = [
        {
            "id": i["id"],
            "place": i["label"],
            "problem": i["text"],
            "suggestion": i["suggestion"],
            "blocking": i["blocking"],
            "fix": fix_for(conn, business_id, i, page=page),
        }
        for i in found["items"]
    ]
    return {"plan_id": found["plan_id"], "ready": found["ready"], "items": items_}
