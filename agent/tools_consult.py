"""answer, recommend, adjust (spec 2026-09-26 §4.2): the consultation after `start`."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError

from . import (
    ads_refusal,
    ads_run,
    auth,
    benchmark,
    business,
    config,
    consult,
    envelope,
    geocode,
    knowledge_index,
    landing,
    listings,
    llm,
    places,
    plan_words,
    plans,
    plans_channels,
    profile,
    site_install,
    support,
    support_mail,
    tools_start,
)
from .channels import registry
from .channels.google_search import build as ads_launch  # the launch's copy linter, which the draft's guard runs
from .mcp_app import PLAN_VIEW, agent_tool, need_business, run
from .models import PlanDraft
from .roles import researcher, strategist
from .tools_start import steps

LEVEL_NAMES = ("minimum", "conservative", "recommended", "aggressive")
RECOMMEND_ACTION = envelope.actions({"id": "recommend", "label": "Build my plan", "tool": "recommend", "primary": True})
# How the plan answer says Google Search runs, and what it offers (`_launch_actions`): only ways that exist today.
OWN_ACCOUNT_SAY = (
    "To run it, Admiry needs the business's own Google Ads account: link yours. Admiry-run Google accounts are coming."
)
# Founder's ruling, 2026-10-07: Admiry running the ads leads (MANAGED_ACTION), the owner's own account second
PREPAY_SAY = (
    "To run it, Admiry can run the ads for you from a prepaid balance, with no Google account needed; or link your own "
    "Google Ads account."
)
# A3c-1: a business that prepays, once Admiry runs Google Search itself (`plans.google_open`): it cannot link its own
# account (google_ads_link.NOT_OWN_ACCOUNT), so only the balance -- the adapter's own connect step, CONNECT_MANAGED
PREPAID_RUNS_SAY = (
    "Admiry runs Google Search for you in its own Google Ads account, paid from your balance; there is nothing to link."
)
# The controller's wording (walk fixes 1, Task 6): a business that prepays cannot link its own account
# (google_ads_link.NOT_OWN_ACCOUNT), and Admiry-run Google accounts are not open yet (`plans.google_open`).
PREPAID_WAITS_SAY = (
    "Google Search for a business that prepays waits for Admiry-run accounts, which are coming; your balance is safe."
)
# M0 Task 6: connect_channel (for Google Search: its Customer ID) leads
LINK_ACTION = {
    "id": "link_own_account",
    "label": "I have a Google Ads account",
    "tool": "connect_channel",
    "primary": True,
}
PREPAY_ACTION = {"id": "prepay", "label": "Admiry runs it, I prepay", "tool": "request_payment_link"}
# Founder's ruling, 2026-10-07: for a business that does not prepay yet, once Admiry runs Google Search itself
# (`plans.google_open`), Admiry running it comes first. Its tool is connect_channel with mode managed -- the setup step
# on the web, where the way is chosen and the terms accepted -- since request_payment_link never makes a business
# prepay: it asks the way while none is chosen and refuses one whose owner chose their own account
# (tools_wallet.NOT_MANAGED). The same id as PREPAY_ACTION: one choice, whichever tool reaches it. Offered
# only beside OWN_ACCOUNT_ACTION, and since the founder's ruling of 2026-10-13 neither is primary: the home's plan card
# shows both ways equally (the order stays), where LINK_ACTION, the one way while Admiry-run Google is not open, leads.
MANAGED_ACTION = {
    "id": "prepay",
    "label": "Admiry runs it, I prepay",
    "tool": "connect_channel",
    "args": {"channel": "google_search", "mode": "managed"},
}
OWN_ACCOUNT_ACTION = {k: v for k, v in LINK_ACTION.items() if k != "primary"}  # second, beside MANAGED_ACTION
ADJUST_ACTION = {"id": "adjust", "label": "Adjust the budget", "tool": "adjust"}
# L1 (spec 2026-10-10 §5): the home's launch card for a business whose money is in (or whose own account is live)
# while its plan is a draft -- the owner's yes, which approves the plan (or, with AGENT_LAUNCH_REVIEW on, sends a first
# launch to Admiry's review). The home alone offers it (`launch_next(funded=True)`, home_api) and draws it by its id;
# its tool is the public launch, which an assistant's `next` (MarketingState's) says by the same tool.
CONFIRM_ACTION = {"id": "confirm", "label": "Confirm and launch", "tool": "launch_marketing_plan", "primary": True}
CONFIRM_REVIEW_ACTION = {**CONFIRM_ACTION, "label": "Confirm this plan"}  # L1: the yes that starts Admiry's review


def confirm_action(settings, *, first: bool) -> dict:
    """The home's yes (L1 §5): "Confirm this plan" while AGENT_LAUNCH_REVIEW is on and this is the business's first
    launch (`first`, D6: the review gates a first launch only), else "Confirm and launch" -- including a newer
    version under an already-approved plan, which approves and launches at once whatever the switch says (L1 review
    M2). Callers pass `first=ads_run.approved(conn, business_id) is None`."""
    return CONFIRM_REVIEW_ACTION if settings.launch_review and first else CONFIRM_ACTION


# A 24-hour draft key cannot launch (both launch tools refuse it): the owner signs in first.
SIGN_IN_ACTIONS = envelope.actions(
    {"id": "sign_in", "label": "Sign in to keep this plan", "tool": "request_connect_link", "primary": True},
    {"id": "adjust", "label": "Adjust the budget", "tool": "adjust"},
)
SIGN_IN_SAY = "To launch it, the owner signs in first."
# The benchmark layer's own wording ("run benchmark.seed()", "no benchmark row …") never reaches the owner (M0 Task 5
# fix round 1): this is what they read instead, and the log keeps the cause.
ESTIMATE_FAILED = "Admiry could not work out the estimates for this plan just now; try again in a few minutes."
# What the owner reads when the model's draft still lacks Google's minimum ad text after it was asked twice
# (`draft_with_copy`, walk fixes 1): no plan version is written with an empty ad preview.
NO_AD_TEXT = "Admiry could not write the ad text for this plan just now; try again in a few minutes."
# What `answer` says when the question of where the ads centre is skipped (walk fixes 1, Task 4; final review B M1):
# the owner would rather say later, or the map found no place for their words twice (the skip keeps the reader's
# `note` then). True either way: a launch with no map point targets the areas on file.
PLACE_LATER = "No problem; tell me the address whenever you like, and the ads will centre on it."
PLACE_NOT_FOUND = (
    "I could not place that on the map, so I will go on without it; tell me the address again whenever you like, and "
    "the ads will centre on it."
)
# ... and when the server has no map to place it on (no Places key: final review A4) -- the owner's words were fine
NO_MAP_SAY = f"{geocode.NO_MAP}."
log = logging.getLogger(__name__)


def _estimate_failed(biz: str, error: benchmark.NotSeeded) -> ToolError:
    """The owner's "try again" for the benchmark layer's one expected failure, its rows not seeded (final review I5);
    called inside the `except`, so the log keeps the traceback."""
    log.warning("plan estimate failed for %s: %s", biz, error, exc_info=error)
    return ToolError(ESTIMATE_FAILED)


def _session(conn, biz: str, session_id: str) -> dict:
    s = consult.get(conn, biz, session_id)
    if s is None:
        raise ToolError(
            "no consultation with that session_id for this business; begin a new one, or look up the open one"
        )
    if datetime.fromisoformat(s["expires_at"]) < datetime.now(timezone.utc):
        raise ToolError("this consultation expired; begin a new one")
    return s


def _knowledge(conn, biz: str) -> dict:
    latest = business.latest_knowledge(conn, biz)
    if latest is None:
        raise ToolError("describe the business first")
    return latest[1]


def _check_value(qid: str, q: dict, value: dict) -> dict:
    """{"choice": ...} / {"other": ...} in the shape consult.parse expects, or ToolError naming qid —
    consult.parse puts a non-string choice into a set membership check (`chosen not in ids`), which
    raises an unhandled TypeError for a list or an object instead of an owner-readable refusal."""
    if "choice" in value:
        chosen = value["choice"]
        if isinstance(chosen, str):
            return value
        if q["input"] == "multi" and isinstance(chosen, list) and chosen and all(isinstance(c, str) for c in chosen):
            return value
        expected = "a string or a list of strings" if q["input"] == "multi" else "a single string"
        raise ToolError(f"the choice for '{qid}' must be {expected}")
    if "other" in value and not isinstance(value["other"], str):
        raise ToolError(f"'other' for '{qid}' must be text, not {type(value['other']).__name__}")
    return value


def _reads_a_site(value: dict) -> bool:
    """Whether an answer to the website question may name a site to read (Q7: the reading is one model call): the site
    the reading saw (consult.SITE_KNOWN), Try again (consult.RETRY: the address tried, fetched once more -- spec
    2026-10-12 §3.2, R1; over-counted when its tries are spent), or words that may, told as the parse reads them but
    without the session (consult.may_read_a_site: an address of the business's own, a no whose line names one (A3p-5
    batch 2 Task 0, ruling R0-m1: over-counted when it names the offered site), or a yes that picks the site the reading
    saw; never a plain no, nor a social page). Website-only: its "yes"/"Try again" heuristics read the website
    question's own options, so never reused for another question (fix round 2, Important 1 -- `_calls_the_model` below
    has the one check that is safe anywhere)."""
    if value.get("choice") in (consult.SITE_KNOWN, consult.RETRY):
        return True
    return isinstance(value.get("other"), str) and consult.may_read_a_site(value["other"])


def _calls_the_model(qid: str, value: dict) -> bool:
    """Whether one answer may call the model: a website answer that may name a site (`_reads_a_site`) -- or, for any
    other question, an address anywhere in its own words alone (consult.site_in_words, a social page passed over),
    which the words-in-any-answer reroute sends to the website question instead and may read the same way (spec
    2026-10-12 §3.4; over-counted once the address named was already read, the accepted direction Task 3's ruling for
    the retry choice is too) -- never `_reads_a_site`'s other checks, which read the website question's own "yes" and
    "Try again" and so must not fire for another question's words (fix round 2, Important 1) -- else words: the
    listing and the business's name questions' aside, read without it (consult._parse_listing, consult._parse_text),
    and a skip of what the business offers (consult.skips, ruling F5)."""
    if qid == consult.WEBSITE:
        return _reads_a_site(value)
    other = value.get("other")
    if isinstance(other, str) and consult.site_in_words(other, social=False) is not None:
        return True
    if qid in ("listing", "business_name") or "other" not in value:
        return False
    return not (qid == "services_offered" and consult.skips(value["other"]))


def _free_text(answers) -> bool:
    """Whether an `answer` call may call the model (`_calls_the_model`, for any of its answers): what auth.check_llm_cap
    counts."""
    return isinstance(answers, dict) and any(
        _calls_the_model(qid, v) for qid, v in answers.items() if isinstance(v, dict)
    )


def _looks_up_the_name(conn, settings, session: dict, biz: str, *, linked: bool, answers: dict) -> bool:
    """Whether the name answered is looked up on Google Maps (Q11, ruling F2): never for a business with a listing the
    owner confirmed (its own place would be offered again, and "None of these" would reject it while it stays linked),
    never once the listing question is answered or while this call answers it (its places stay as the owner saw them),
    and never when no lookup can run -- no Places key, the day's lookups spent: a lookup that did not run found nothing,
    so what is offered stays as it was."""
    if linked or "listing" in session["answers"] or "listing" in answers:
        return False
    return geocode.can_place(settings) and geocode.lookups_left(conn, settings, biz)


def _adopt_services(conn, biz: str, knowledge: dict, session: dict) -> list[str]:
    """The services the owner picked to lead with that the profile does not hold: added to it, each with the
    option's own id and label (spec rev. 2 item 5: a service's id is the consultation's), as a new knowledge version
    the way update_business adds one. Returns the names added; nothing when every pick is on file or the cap
    (`profile.MAX_SERVICES`) is reached. A placeholder option (`generic`: the fallback tree's "My main service")
    names no service and is never added; an id picked twice is added once."""
    picks = consult._picks((session.get("answers") or {}).get("service") or {})
    shown = {o["id"]: o for o in (session.get("options") or {}).get("service", [])}
    on_file = profile._readable(knowledge.get("services"))
    ids = {profile.service_id(s) for s in on_file}
    names = {profile.norm(s["name"]) for s in on_file}
    added = []
    for pick in picks:
        opt = shown.get(pick)
        if opt is None or opt.get("generic") or pick in ids or profile.norm(opt["label"]) in names:
            continue
        if len(on_file) + len(added) >= profile.MAX_SERVICES:
            break
        added.append({"id": pick, "name": opt["label"], "intent_phrases": [], "price_hint": None})
        ids.add(pick)
        names.add(profile.norm(opt["label"]))
    if added:
        business.add_knowledge(conn, biz, {**knowledge, "services": [*on_file, *added]}, source="owner")
    return [s["name"] for s in added]


def _adopt_named_services(conn, biz: str, knowledge: dict, names: list[str]) -> list[str]:
    """What the services question's words named (Q12) that the profile does not already hold -- told apart as
    `profile.apply` tells them (`consult._same_label`: `profile.norm`, so case, punctuation and spacing aside, and a
    trailing "s" ignored: ruling F8), the same one named twice added once, a name with nothing to tell it by (no letter
    or digit) not at all -- added to it, each under a new id of its own, as one new knowledge version (`profile.apply`,
    source owner, which so never refuses one as on file already). Returns the names added; nothing when every one is
    on file already, or (as `_adopt_services`) the business is already at `profile.MAX_SERVICES`."""
    on_file = profile._readable(knowledge.get("services"))
    seen = {consult._same_label(s["name"]) for s in on_file}
    added = []
    for name in names:
        label = consult._same_label(name)
        if not label or label in seen:
            continue
        if len(on_file) + len(added) >= profile.MAX_SERVICES:
            break
        added.append(name)
        seen.add(label)
    if added:
        business.add_knowledge(
            conn, biz, profile.apply(knowledge, {"service_changes": [{"name": n} for n in added]}), source="owner"
        )
    return added


@agent_tool(cls="W", scope="manage")
def answer(session_id: str, answers: dict, idempotency_key: str) -> dict:
    """Record the owner's answers to the consultation questions and get the next one. `answers` maps a
    question id (from next.question.id) to {"choice": "<option id>"} (a list of ids for a multi question)
    or {"other": "<the owner's own words, up to 500 characters>"}; several questions at once are fine.
    When the owner typed instead of picking, send their words as "other" exactly as typed — never choose
    an option for them and never confirm a value yourself; Admiry reads the words and `say` states what
    it will use. If the owner corrects an earlier answer before the plan is built, call answer again with
    that question id: the new answer replaces the old one (after the plan, use adjust). Show `say` to the
    owner as written, then present next.question's options as choices (always with a way to type an
    answer). When data.ready is true, call recommend. If the owner's words ask for a person, nothing is
    recorded and data.support is set: show `say` as written (the request is with the Admiry team, the reply
    comes by e-mail); the same question still waits."""

    def prepare(conn, p):
        nonlocal answers  # the reroute below may replace the whole mapping (spec 2026-10-12 §3.4)
        biz = need_business(p)
        s = _session(conn, biz, session_id)
        if s["status"] not in ("open", "ready"):  # ready still takes a correction; planned takes adjust
            raise ToolError(
                f"this consultation is {s['status']}; "
                + ("change the plan instead" if s["status"] == "planned" else "begin a new one")
            )
        if not isinstance(answers, dict) or not answers:
            raise ToolError("answers must map question ids to a choice or other text")
        if len(answers) > consult.MAX_ANSWERS:
            raise ToolError(f"at most {consult.MAX_ANSWERS} answers per call")
        tree = consult.load_tree(s["tree"])
        by_id = {q["id"]: q for q in consult.questions(s)}  # the questions this session asks
        open_ids = [q["id"] for q in consult.pending(s)]
        # an address in the words of any answer answers the website question instead, while the site is unread (spec
        # 2026-10-12 §3.4, R2): the question asked keeps its words unrecorded; a read starts the session again. Only
        # over a qid this consultation actually asks (`by_id`): an unknown one still reaches its usual refusal below,
        # never silently becomes a website answer (review focus 4)
        for qid, value in answers.items():
            if (
                qid in by_id
                and qid != consult.WEBSITE
                and isinstance(value, dict)
                and isinstance(value.get("other"), str)
                and consult.site_reanswer(s, value["other"]) is not None
            ):
                answers = {consult.WEBSITE: {"other": value["other"]}}
                break
        parsed, clarifications, retries = {}, {}, dict(s["answers"].get(consult.RETRY_KEY) or {})
        corrected = [qid for qid in answers if qid in by_id and qid in s["answers"]]  # a changed mind, replaced
        gen = None
        settings = config.load()
        # words that call the place linked not the owner's unlink it only when the owner confirmed one (plan
        # 2026-10-03-agent-listing-not-mine-words, D2); otherwise they are read as any answer
        linked = profile.business_listing((business.latest_knowledge(conn, biz) or (None, {}))[1]) is not None
        for qid, value in answers.items():
            if qid not in by_id:
                raise ToolError(f"no question '{qid}' in this consultation; open: {', '.join(open_ids)}")
            if not isinstance(value, dict):
                raise ToolError(f"answer for '{qid}' must be an object with choice or other")
            value = _check_value(qid, by_id[qid], value)
            listing = by_id[qid].get("options_from") == "listing"
            if listing and qid in s["answers"]:  # answered once per consultation: later changes are Settings' own
                clarifications[qid] = consult.LISTING_SETTLED
                continue
            # the listing, the website and the name questions' words, and a skip of what the business offers: no
            # model (a site's reading is checked below; ruling F5)
            skip = qid == "services_offered" and consult.skips(value.get("other"))
            words_only = listing or by_id[qid]["input"] == "website" or qid == "business_name" or skip
            # a reading the home's harness handed in (consult.with_reading, K6 slice 1 D2): its one Turn call read the
            # words, so no generator is built and no cap checked here
            if "other" in value and gen is None and not words_only and consult.reading_for(qid) is None:
                auth.check_llm_cap(conn, p)
                try:
                    gen = llm.generator()
                except llm.LLMError as e:
                    raise ToolError(str(e)) from e
            try:
                stored, why = consult.parse(s, by_id[qid], value, gen, support=settings.support, linked=linked)
            except consult.SupportAsk as ask:  # the words ask for a person: nothing is recorded (spec 2026-09-30 §4.2)
                return s, tree, {}, {}, retries, [], ask, None, None, None, None, None
            except consult.ListingNotMine as not_mine:  # the place linked is not theirs: unlinked in the body, nothing
                return s, tree, {}, {}, retries, [], not_mine, None, None, None, None, None  # recorded (not mine, D4)
            except llm.LLMError as e:  # read_answer failed: the owner reads why, not "internal error"
                raise ToolError(str(e)) from e
            if stored is not None:
                parsed[qid] = stored
            else:
                clarifications[qid] = why
                if "other" in value:
                    retries[qid] = retries.get(qid, 0) + 1
        # the website (Q6-Q8): a page on a social or listing site is no answer -- SOCIAL, a retry counted, its address
        # kept for the body to note (A3p-6); a second one answers the question as no website, the page noted too
        # (ruling F4). A site is read here, outside the transaction, as `start` reads one (the crawl and one reading,
        # under the day's model calls). A crawl that refuses it or reads no page (ruling R2-a) is asked again once, with
        # Try again and Ask me instead, nothing recorded; the address's last try failing too leaves it unread (Q8) --
        # `start`'s own try counts as one (consult.site_tries_left, MAX_SITE_TRIES: spec 2026-10-12 §3.2, R1). A model
        # that fails is the owner's to read, nothing recorded. Ask me instead, or an address whose tries are spent, is
        # unread with nothing fetched or read, so the day's model calls are not checked (rulings R0-i and R0-i2).
        site, social, reading, unread_try = parsed.get(consult.WEBSITE), None, None, None
        if site is not None and "social" in site:
            social = site["social"]
            if not site.get("none"):
                del parsed[consult.WEBSITE]
                clarifications[consult.WEBSITE] = consult.social_say(social)
                retries[consult.WEBSITE] = retries.get(consult.WEBSITE, 0) + 1
        elif site is not None and "url" in site:
            left = consult.site_tries_left(s, site["url"])
            if site.get("asked") or left == 0:  # Ask me instead, or an address whose tries are spent: unread, no fetch
                parsed[consult.WEBSITE] = {**site, "read": False}
            else:
                auth.check_llm_cap(conn, p)
                try:
                    reading = tools_start.read_site(conn, biz, s, site["url"])
                except tools_start.SiteUnreadable:
                    pass
                if reading is not None:
                    parsed[consult.WEBSITE] = {**site, "read": True}
                else:  # a try spent, counted on the known facts: the last one too, so the address is not fetched again
                    unread_try = {"url": site["url"], "tries": consult.MAX_SITE_TRIES - left + 1}
                    if left > 1:  # the first failure of this address: asked again (spec 2026-10-12 §3.2, R1)
                        # no retry counted: the known fact's `tries` tracks the fetch, so a reply the re-asked question
                        # does not understand gets NOT_A_SITE once before the retry rule ends it (final review I2)
                        del parsed[consult.WEBSITE]
                        host = consult.host_of(site["url"])
                        clarifications[consult.WEBSITE] = consult.SITE_UNREAD_SAY.format(host=host)
                    else:  # the last try failed too: recorded unread, said so (SITE_UNREAD_AGAIN in the body)
                        parsed[consult.WEBSITE] = {**site, "read": False, "again": True}
        # where the ads centre: the place the owner's words name, put on the map here, outside the transaction
        placed_note = None
        loc = parsed.get("location")
        if loc is not None and "note" in loc and "address" not in loc and not geocode.can_place(settings):
            # no map on this server (no Places key; final review A4): the words kept, nothing looked up, no retry -- and
            # the owner reads why, never NOT_PLACED's "try a street address"
            parsed["location"] = {"skipped": True, "raw": loc["raw"], "note": loc["note"], "source": "other"}
            clarifications["location"] = NO_MAP_SAY
        elif loc is not None and "note" in loc and "address" not in loc:
            try:
                found = geocode.place(conn, settings, loc["note"], business_id=biz)
            except geocode.NoLookups:  # no lookups left today: the question stays open, no retry counted, and the
                del parsed["location"]  # owner reads the way on (say later, or try again tomorrow)
                clarifications["location"] = geocode.NO_LOOKUPS_LATER
            else:
                if found is not None:
                    parsed["location"] = {
                        **loc,
                        "address": found.address,
                        "lat": found.lat,
                        "lng": found.lng,
                        "place_id": found.place_id,
                    }
                    placed_note = found
                else:
                    tries = retries.get("location", 0)
                    if tries >= 1:  # asked once more already: skipped, the owner's words kept
                        parsed["location"] = {
                            "skipped": True,
                            "raw": loc["raw"],
                            "note": loc["note"],
                            "source": "other",
                        }
                    else:
                        del parsed["location"]
                        clarifications["location"] = geocode.NOT_PLACED
                        retries["location"] = tries + 1
        # the name (Q11): looked up on Google Maps here, outside the transaction, as start's own lookup is -- only when
        # a lookup may offer places and replace what is offered (`_looks_up_the_name`, ruling F2)
        offered = None
        name_answer = parsed.get("business_name")
        named = name_answer is not None and "name" in name_answer
        if named and _looks_up_the_name(conn, settings, s, biz, linked=linked, answers=answers):
            knowledge = _knowledge(conn, biz)
            facts = {**(s.get("known_facts") or {}), "business_name": name_answer["name"]}
            offered = listings.lookup(
                conn, settings, knowledge, facts, business_id=biz, rejected=knowledge.get("listing_rejected") or []
            )
        return (
            s,
            tree,
            parsed,
            clarifications,
            retries,
            corrected,
            None,
            placed_note,
            reading,
            social,
            offered,
            unread_try,
        )

    def body(conn, p, ctx):
        s, tree, parsed, clarifications, retries, corrected, ask, placed_note, reading, social, offered, unread_try = (
            ctx
        )
        if isinstance(ask, consult.ListingNotMine):
            return _not_mine_answer(conn, p, s)
        if ask is not None:
            return _support_answer(conn, p, s, ask)
        update = dict(parsed)
        if retries:
            update[consult.RETRY_KEY] = retries
        # the owner's pick of the places offered (plan 2026-10-03-agent-listing-confirm): linked, and the ads centred on
        # it when nothing else has placed them -- in this transaction with the answer
        listing_said = None
        if "listing" in parsed:
            pick = parsed["listing"].get("choice")
            if parsed["listing"].get("skipped"):  # unclear twice: nothing linked or remembered, the offer closed
                listings.drop_offer(conn, p.business_id)
                listing_said = listings.UNCLEAR_NONE
            elif pick != consult.LISTING_NONE["id"]:
                # the place the owner saw under that id: a find since may offer another under it (final review A1)
                seen = next((o["label"] for o in s["options"].get("listing") or [] if o["id"] == pick), None)
                linked = listings.confirm(conn, p.business_id, pick, label=seen) if seen is not None else None
                if linked is None:  # the offer closed (a day passed, another pick) or changed: nothing linked
                    listing_said = listings.OFFER_GONE
                else:
                    listing_said = listings.LINKED.format(name=linked["name"])
                    centred = "location" not in s["answers"] and "location" not in parsed and not linked.get("placed")
                    if centred and linked.get("lat") is not None and linked.get("lng") is not None:
                        update["location"] = {
                            "address": linked["address"],
                            "lat": linked["lat"],
                            "lng": linked["lng"],
                            "source": "known_fact",
                        }
                        listing_said += listings.CENTRED.format(address=places.display(linked["address"]))
            else:  # remembered only when still offered: a closed offer promises nothing
                listing_said = listings.NONE_NOTED if listings.reject_offer(conn, p.business_id) else listings.NONE_GONE
        fresh = consult.record(conn, s, update) if update else s
        if unread_try is not None:  # the address tried and unread, its tries counted (ruling P1)
            before = fresh["answers"].get(consult.WEBSITE)
            if consult.WEBSITE not in parsed and before is not None and before.get("read") is not True:
                # an address correcting an answer with no site read, its first try failed: the question reopened, the
                # answer before taken back, the profile's website_unread left until it resolves (Task 3 review, ruling
                # I2). A site read stands -- its answer, the session started from it -- and the failing address is only
                # kept on the known facts, unread with its try counted (final review minor 4)
                fresh = consult.forget_answer(conn, fresh, consult.WEBSITE)
            fresh = consult.add_known_facts(conn, fresh, {consult.WEBSITE: {**unread_try, "read": False}})
            if consult.WEBSITE not in parsed:  # Try again and Ask me instead (R1): asked again, or beside a site read
                # that stands, for the question gone back to (its prompt, UNOPENED_PROMPT, follows the known fact)
                fresh = consult.set_options(conn, fresh, consult.WEBSITE, list(consult.RETRY_OPTIONS))
        if social is not None:  # a page on a social or listing site (Q6): kept for A3p-6, nothing recorded
            fresh = consult.add_known_facts(conn, fresh, {"social": social})
        site = parsed.get(consult.WEBSITE) or {}
        view = None
        if reading is not None:  # a site read (Q7): the same session started again from its reading, in place
            view = consult.reading_view(reading[0])  # what was taken from the site, for the owner to check (R6)
            fresh = _restart(conn, p.business_id, fresh, site, reading)
            # the known fact says read too (ruling P1), so site_tried / site_unopened never call it unread after a
            # failed try (spec 2026-10-12 §3.2, R1)
            fresh = consult.add_known_facts(conn, fresh, {consult.WEBSITE: {"url": site["url"], "read": True}})
            tree = consult.load_tree(fresh["tree"])
            parsed = {qid: a for qid, a in parsed.items() if fresh["answers"].get(qid) == a}  # what the restart kept
        knowledge = _knowledge(conn, p.business_id)
        # a site that could not be read (Q8) -- or, when the retry rule ended the question with no address (a note, a
        # social page), the address tried, which stays as start keeps it (ruling R0-i2; Task 3 review, ruling I1b)
        unread = None
        if reading is None and site and not consult.no_website_said(site):
            unread = site.get("url") or consult.site_unopened(fresh.get("known_facts"))
        if unread is not None:  # on the profile as unread (R2-e)
            marked = tools_start.with_unread_site(knowledge, unread)
            if marked != knowledge:
                business.add_knowledge(conn, p.business_id, marked, source="owner")
                knowledge = _knowledge(conn, p.business_id)
        if consult.no_website_said(site) and (
            "website_unread" in knowledge or consult.site_tried(fresh.get("known_facts"))
        ):
            # "No website", tapped or said, is the owner's latest answer: an address that could not be read comes off
            # the profile (A3p-5 batch 2 Task 0, ruling R0-i) and off the session's known facts, where a failed try
            # kept it for the question asked again (spec 2026-10-12 §3.2, R1)
            fresh = consult.add_known_facts(conn, fresh, {consult.WEBSITE: None})
            if (fresh.get("options") or {}).get(consult.WEBSITE) == list(consult.RETRY_OPTIONS):
                # nothing left to try again: the question's own options back, should it be asked again (review)
                own = next(q["options"] for q in consult.tree_questions(tree) if q["id"] == consult.WEBSITE)
                fresh = consult.set_options(conn, fresh, consult.WEBSITE, [dict(o) for o in own])
            if "website_unread" in knowledge:
                unsaid = {key: value for key, value in knowledge.items() if key != "website_unread"}
                business.add_knowledge(conn, p.business_id, unsaid, source="owner")
                knowledge = _knowledge(conn, p.business_id)
        if placed_note is not None:  # the place found becomes the profile's, as update_business adopts an address
            where = {"location": {"address": placed_note.address, "listing": geocode.listing(placed_note)}}
            business.add_knowledge(conn, p.business_id, profile.apply(knowledge, where), source="owner")
            knowledge = _knowledge(conn, p.business_id)
        if offered is not None and "listing" not in fresh["answers"]:
            # a name (Q11): the offer, replacing any before, and the listing options it freezes -- never under an
            # answered listing question (ruling F2)
            listings.save_offer(conn, p.business_id, offered)
            fresh = consult.set_options(
                conn, fresh, "listing", consult.build_options(tree, knowledge, candidates=offered)["listing"]
            )
        name_answer = parsed.get("business_name") or {}
        if "name" in name_answer:
            fresh = consult.add_known_facts(conn, fresh, {"business_name": name_answer["name"]})
            # the owner's words become the profile's name, whatever a reading guessed (A3p-5 batch 2 Task 0, ruling
            # R0-l), and the name is theirs (ruling R0-k: plans.name_for vouches for it)
            named = {**knowledge, "name": name_answer["name"], "name_source": "owner"}
            if named != knowledge:
                business.add_knowledge(conn, p.business_id, named, source="owner")
                knowledge = _knowledge(conn, p.business_id)
        added = _adopt_services(conn, p.business_id, knowledge, fresh) if "service" in parsed else []
        if added:
            knowledge = _knowledge(conn, p.business_id)  # the version just written: the steps read it
        svc_words = parsed.get("services_offered") or {}
        if svc_words.get("services"):  # the services (Q12): what the profile lacks, in one more version
            more = _adopt_named_services(conn, p.business_id, knowledge, svc_words["services"])
            if more:
                knowledge = _knowledge(conn, p.business_id)
                if "service" not in fresh["answers"]:  # frozen already: the owner's pick stands, as is
                    fresh = consult.set_options(
                        conn, fresh, "service", consult.build_options(tree, knowledge)["service"]
                    )
            added = added + more
        nxt = consult.next_question(fresh)
        ready = nxt is None
        remaining = [q["id"] for q in consult.pending(fresh)]
        said = [listing_said] if listing_said else []  # which place is the business: said first, whatever the order
        for qid in parsed:
            if qid == "listing":
                continue
            if qid == consult.WEBSITE and "url" in parsed[qid]:  # a site: read, or one that could not be (Q7, Q8)
                if parsed[qid]["read"]:
                    said.append(consult.reading_say(view))
                elif parsed[qid].get("again"):
                    said.append(consult.SITE_UNREAD_AGAIN.format(host=consult.host_of(parsed[qid]["url"])))
                continue
            if qid == "location":  # where the ads centre: the place found, or the question left for later
                loc = parsed[qid]
                if not loc.get("skipped"):
                    said.append(f"Centred on {places.display(loc['address'])}.")
                elif "location" not in clarifications:  # a skip with its own words (no map here) says only those
                    said.append(PLACE_NOT_FOUND if "note" in loc else PLACE_LATER)  # a place given, or "later"
                continue
            if qid in corrected:  # the owner changed their mind: say what now stands, not the why again
                said.append(f"Updated: {_answer_label(tree, qid, parsed[qid])}.")
                continue
            why = next((q.get("why") for q in consult.tree_questions(tree) if q["id"] == qid), None)
            if why:
                said.append(why.replace("{service}", consult.facts(fresh, tree).get("service") or "this service"))
        if added:
            said.append(f"Added to your services: {consult._phrase(added)}.")
        said.extend(clarifications.values())
        if ready:
            said.append("I have what I need. Say the word and I will build the plan.")
        else:  # say stands on its own: the question and its options, for an assistant that shows only text
            said.append(consult.question_sentence(nxt))
        payload = {
            "session_id": fresh["id"],
            "recorded": parsed,
            "corrected": [qid for qid in corrected if qid in parsed],
            "remaining": remaining,
            "ready": ready,
            "steps": steps(fresh, knowledge, planned=False),
        }
        if reading is not None:  # what was taken from the site, for the home's facts card (R6)
            payload["reading"] = view
        return envelope.make(payload, say=" ".join(said), next=nxt or RECOMMEND_ACTION)

    return run(
        "answer",
        "manage",
        {
            "session_id": session_id,
            "answers": answers,
            "idempotency_key": idempotency_key,
            "free_text": _free_text(answers),  # auth.check_llm_cap counts only these rows
        },
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
        after=support_mail.after_tool,
    )


def _restart(conn, biz: str, session: dict, site: dict, reading: tuple) -> dict:
    """A site answered and read (Q7, W3): its reading adopted as `start` adopts one (tools_start.adopt_reading, without
    superseding), then the same session row started again from it (consult.restart) -- the reading's tree, the options
    its knowledge gives (the places its lookup found among them), their pre-answers, and `site`, the website answer,
    read. In the body's transaction; the session as stored."""
    data, goal, facts, _notes, offered, placed = reading
    knowledge = tools_start.adopt_reading(conn, biz, data, goal, facts, offered, placed)
    name = consult.tree_for(knowledge.get("category", "fallback"))
    tree = consult.load_tree(name)
    options = consult.build_options(tree, knowledge, candidates=offered)
    can_place = geocode.can_place(config.load())
    pre = consult.preanswers(tree, facts, knowledge, session["opening_text"], can_place=can_place)
    return consult.restart(conn, session, tree=name, options=options, answers={**pre, consult.WEBSITE: site})


def _answer_label(tree: dict, qid: str, stored: dict) -> str:
    """What a recorded answer reads as to the owner: the option's label for a choice, else their own words."""
    q = next(q for q in consult.tree_questions(tree) if q["id"] == qid)
    choice = stored.get("choice")
    if isinstance(choice, list):
        labels = [o["label"] for o in q["options"] if o["id"] in choice]
        return ", ".join(labels) or str(stored.get("raw") or "")
    for o in q["options"]:
        if o["id"] == choice:
            return o["label"]
    return str(stored.get("raw") or choice or "")


def _support_answer(conn, p, s: dict, ask: consult.SupportAsk) -> dict:
    """`answer`'s reply when the words asked for a person (spec 2026-09-30 §4.2): the business's ticket opened or
    appended, nothing recorded, and the pending question asked again after the acknowledgement."""
    source = support.source_of(p.key)
    # Every e-mail call arrives from the one address "mail" and its drafts' keys are draft class: counting the address
    # would put all senders in one daily bucket, and the channel meters its senders itself.
    ip = None if source == "email" else p.ip
    try:
        out = support.open_or_append(
            conn,
            business_id=p.business_id,
            source=source,
            reason=ask.kind,
            words=ask.text,
            summary=ask.summary or ask.text,
            actor=p.actor,
            ip=ip,
            draft=p.key.cls == "draft",
        )
    except support.Limited as e:
        raise ToolError(str(e)) from e
    nxt = consult.next_question(s)
    say = support.say_for(out) + (f" {consult.question_sentence(nxt)}" if nxt else "")
    payload = {
        "session_id": s["id"],
        "recorded": {},
        "corrected": [],
        "remaining": [q["id"] for q in consult.pending(s)],
        "ready": nxt is None,
        "steps": steps(s, _knowledge(conn, p.business_id), planned=False),
        "support": support.public(out),
    }
    return envelope.make(payload, say=say, next=nxt or RECOMMEND_ACTION)


def _not_mine_answer(conn, p, s: dict) -> dict:
    """`answer`'s reply when the owner's words said the Google Maps place linked to the business is not theirs (plan
    2026-10-03-agent-listing-not-mine-words, D4): the listing unlinked as every other path unlinks it
    (listings.unlink_and_say), in this command's transaction, nothing recorded for the question, and the first question
    that waits asked again after the sentences that say so -- the one the words came for, or one before it; the
    question of where the ads centre is pending again, in its turn, when the centre was the listing's own.
    `listing: "unlink"` in the answer is what the activity log reads (D5). Unlinked meanwhile by another path, nothing
    is linked any more: the sentence says so and nothing is written."""
    unlinked, said = listings.unlink_and_say(conn, p.business_id, asks_next=True)
    fresh = consult.get(conn, p.business_id, s["id"])  # forget_centre may have dropped a centre and opened it again
    nxt = consult.next_question(fresh)
    payload = {
        "session_id": fresh["id"],
        "recorded": {},
        "corrected": [],
        "remaining": [q["id"] for q in consult.pending(fresh)],
        "ready": nxt is None,
        "steps": steps(fresh, _knowledge(conn, p.business_id), planned=False),
    }
    if unlinked is not None:
        payload["listing"] = "unlink"
    say = said + listings.LINK_AGAIN + (f" {consult.question_sentence(nxt)}" if nxt else "")
    return envelope.make(payload, say=say, next=nxt or RECOMMEND_ACTION)


def plan_session(conn, biz: str, plan_id: str) -> dict:
    """The consultation behind a plan (adjust, and revise_plan on a consulted plan): the session that
    produced plan_id, or the business's latest planned one."""
    row = conn.execute(
        "SELECT id FROM consult_session WHERE plan_id = %s OR (business_id = %s AND status = 'planned') "
        "ORDER BY created_at DESC LIMIT 1",
        (plan_id, biz),
    ).fetchone()
    s = consult.get(conn, biz, row["id"]) if row else None
    if s is None:
        raise ToolError("the consultation behind this plan is gone; begin a new one")
    return s


def draft_budget(conn, biz: str, knowledge: dict, goal: dict | None) -> int:
    """The monthly amount the model writes a consultation plan's ads for: the owner's own limit when the goal holds
    one (`limit_source` owner), else the benchmark's recommended level -- the amount `build` spends at level
    "recommended". The plan prompt writes no ads for a null or zero limit (founder's walk, 2026-10-03: every plan
    since 2026-09-27 had none), so the draft is asked with this number, never with an empty goal."""
    ceiling = float(goal["monthly_limit_usd"]) if goal and goal.get("monthly_limit_usd") else None
    stated = ceiling if ceiling is not None and goal.get("limit_source", "owner") == "owner" else None
    try:
        recommended = benchmark.recommend_budget(conn, knowledge.get("category", "fallback"), stated)
    except benchmark.NotSeeded as e:
        raise _estimate_failed(biz, e) from e
    return plans.levels(recommended, stated)["recommended"]


def _copy_complete(draft: PlanDraft, banned: list[str]) -> bool:
    """Whether the draft's Google Search ad text keeps Google's minimum (`plans.MIN_HEADLINES` / `MIN_DESCRIPTIONS`)
    after the copy linter the launch's precheck runs (`ads_launch.lint_copy` with the trade's `banned` words: the
    length limits, those words, "!", phone numbers, e-mail addresses, shouting), over every funded Google Search entry
    merged as `plans.normalize` merges a channel the draft names twice."""
    entries = [ch for ch in draft.ads if ch.channel == "google_search" and ch.monthly_usd > 0]
    headlines, descriptions, _notes = ads_launch.lint_copy(
        [h for ch in entries for h in ch.headlines], [d for ch in entries for d in ch.descriptions], banned
    )
    return len(headlines) >= plans.MIN_HEADLINES and len(descriptions) >= plans.MIN_DESCRIPTIONS


def draft_with_copy(
    gen,
    knowledge: dict,
    goal: dict,
    *,
    biz: str,
    consultation: dict,
    instruction: str | None = None,
    previous: dict | None = None,
    given: knowledge_index.Retrieved | None = None,
    research: dict | None = None,
    avoid: list[str] | None = None,
) -> PlanDraft:
    """The model's draft for a consultation plan, with its ad text: a draft whose Google Search text is short of
    `plans.MIN_HEADLINES` / `MIN_DESCRIPTIONS` once the launch's linter has judged it (`_copy_complete`) is asked once
    more (the same call), and refused with NO_AD_TEXT when still short -- no plan version carries ad text the launch
    would refuse as too little. The retry and the refusal are logged with `biz`, the business. `given` is the curated
    knowledge the draft reads (capability layer K3, `strategist.knowledge_for`; None: none), and the draft keeps only
    ids it was given (`strategist.kept`). `research` is the latest delivered research (capability layer K4,
    `researcher.for_strategist`; None: none). `avoid` is the plan writer's words to avoid (ads_refusal.avoid_words;
    None: none). Calls the model: prepare only."""
    banned = landing.load_recipe(knowledge.get("category") or "fallback")["banned"]  # as the precheck reads them
    told = given.text if given else None
    try:
        draft = gen.draft_plan(
            knowledge,
            goal,
            instruction=instruction,
            previous=previous,
            consultation=consultation,
            trade_knowledge=told,
            research=research,
            avoid=avoid,
        )
        if not _copy_complete(draft, banned):
            log.info("plan draft: the ad text was missing; asking once more (business %s)", biz)
            draft = gen.draft_plan(
                knowledge,
                goal,
                instruction=instruction,
                previous=previous,
                consultation=consultation,
                trade_knowledge=told,
                research=research,
                avoid=avoid,
            )
    except llm.LLMError as e:
        raise ToolError(str(e)) from e
    if not _copy_complete(draft, banned):
        log.info("plan draft: the ad text was missing twice; refused (business %s)", biz)
        raise ToolError(NO_AD_TEXT)
    ids = strategist.kept(draft.knowledge_ids, given)
    if len(ids) != len(draft.knowledge_ids):
        log.info(
            "plan draft: %d knowledge id(s) it was not given, or named twice, were dropped (business %s)",
            len(draft.knowledge_ids) - len(ids),
            biz,
        )
    # the model's own draft when its ids all stand, else a copy that keeps the ones that may; never the draft changed in
    # place (a generator may hand the same object back on every call)
    return draft if ids == draft.knowledge_ids else draft.model_copy(update={"knowledge_ids": ids})


def build(
    conn,
    biz: str,
    session: dict,
    knowledge: dict,
    *,
    level: str,
    monthly_usd: float | None,
    draft,
    previous: dict | None,
    named: dict[str, int] | None = None,
    research_id: str | None = None,
):
    """Sections for a plan from a ready session (recommend and revise_plan: draft from the model; adjust:
    previous sections). `monthly_usd` is the spend; None spends `level`'s amount. The spend is split across the
    channels by spec 2026-09-29 §6.2 (`plans_channels.fund`), `named` holding the amounts the owner set per channel.
    `research_id` is the id of the delivered research the draft read (capability layer K4, None: it read none): the
    rationale records it for a new draft, and a change without one keeps the version's own.

    Only a limit the owner stated (goal.limit_source "owner") is the recommendation and clips the levels. The
    ceiling recommend wrote when the owner named none ("system", its aggressive level) is enforced but not read
    back as a stated budget: the levels stay anchored to the benchmark recommendation the owner saw."""
    tree = consult.load_tree(session["tree"])
    facts = consult.facts(session, tree)
    goal = business.latest_goal(conn, biz)
    ceiling = float(goal["monthly_limit_usd"]) if goal and goal.get("monthly_limit_usd") else None
    owners = ceiling is not None and goal.get("limit_source", "owner") == "owner"
    stated = ceiling if owners else None
    category = knowledge.get("category", "fallback")
    try:
        recommended = benchmark.recommend_budget(conn, category, stated)
    except benchmark.NotSeeded as e:
        raise _estimate_failed(biz, e) from e
    lv = plans.levels(recommended, stated)
    spend = float(monthly_usd) if monthly_usd is not None else float(lv[level])
    if ceiling and spend > ceiling:
        if owners:
            raise ToolError(
                f"{plans.usd(spend)} a month is above your {plans.usd(ceiling)} monthly limit; raise the limit first"
            )
        raise ToolError(
            f"{plans.usd(spend)} a month is above the {plans.usd(ceiling)} ceiling Admiry set for this plan; "
            "raise it first"
        )
    # the plan's line about automatic changes says what the goal says (final review B, Important 10): not the model's
    # draft, nor the version this one starts from, once the owner turned them off -- or on again
    autopilot = (goal or {}).get("autopilot")
    if draft is not None:
        sections, notes = plans.normalize(draft, max(spend, stated or 0), autopilot=autopilot)
    else:
        sections, notes = (
            {k: v for k, v in previous.items() if k not in ("estimate", "consultation", "rationale", "levels")},
            [],
        )
        # the money line is the one a plan says now, as a fresh build writes it (plans.normalize), never the version's
        # own: one written before the founder's ruling of 2026-10-13 named the owner's own account first
        # (owner-chooses review, fix round 1, item 3)
        sections["disclosure"] = {"text": plans.DISCLOSURE}
        if autopilot:
            sections["optimization"] = {**sections["optimization"], "autopilot": autopilot}
    try:
        notes += plans_channels.fund(
            conn,
            biz,
            sections,
            session=session,
            tree=tree,
            knowledge=knowledge,
            goal=goal,
            spend=spend,
            levels=lv,
            named=named,
        )
        split = {c["channel"]: c["monthly_usd"] for c in plans.funded(sections)}
        est = plans.estimate_columns(
            conn,
            category,
            split=split,
            recommended_split=sections["ads"]["splits"].get("recommended") or split,
            customer_value_usd=facts["customer_value_usd"],
            value_source=facts["customer_value_source"],
            footnote_noun=tree["defaults"]["footnote_noun"],
        )
    except plans.BelowMinimum as e:  # a spend below plans.MIN_ESTIMATE_SPEND: its own owner sentence
        raise ToolError(str(e)) from e
    except benchmark.NotSeeded as e:  # the benchmark rows are not there: never its own words; any other error is a bug
        raise _estimate_failed(biz, e) from e
    sections["estimate"] = est
    sections["consultation"] = plans.consultation_section(
        facts, knowledge, level=level, listing=knowledge.get("listing")
    )
    sections["rationale"] = plans.rationale_section(knowledge, facts)
    # the curated knowledge the draft read (capability layer K3, plan P2): a new draft's own ids; a change that keeps
    # the version's words (adjust, the "Your ads" page) keeps the ids they were written with ([] before K3)
    sections["rationale"]["knowledge_ids"] = (
        list(draft.knowledge_ids)
        if draft is not None
        else list(((previous or {}).get("rationale") or {}).get("knowledge_ids") or [])
    )
    # the delivered research the draft read (capability layer K4, plan D18): a new draft's run id (None without one); a
    # change that keeps the version's words (adjust, the "Your ads" page) keeps the id they were written with
    sections["rationale"]["research_id"] = (
        research_id if draft is not None else ((previous or {}).get("rationale") or {}).get("research_id")
    )
    sections["levels"] = lv
    basis = {k: est[k] for k in ("basis", "confidence", "benchmark_rows")}
    return sections, basis, notes, goal


def _prepays(rows: list[dict]) -> bool:
    """Whether the business prepays, read off the plan card's rows: its Google Search row runs from the balance (a row
    carries the business's own way, `plans_channels.modes`). A plan without Google Search funds a place that runs from
    the balance only for a business that prepays (`plans_channels.usable_modes`)."""
    google = [r for r in rows if r["platform"] == "google_search"]
    return any(r["mode_text"] == plans.MANAGED_TEXT for r in google or rows)


def _launch_actions(rows: list[dict]) -> dict:
    """The plan answer's actions: only ways that exist (a prepay for a Google-only plan led to "no Admiry balance":
    founder's walk, 2026-10-03). Once Admiry runs Google Search itself (`plans.google_open`, A3c-1), a plan with Google
    Search for a business that does not prepay yet offers Admiry running it (MANAGED_ACTION), then its own account --
    side by side, neither primary (founder's rulings, 2026-10-07 and 2026-10-13) -- then a change. Otherwise the owner's
    own Google Ads account first, unless the business prepays (it cannot link one: `google_ads_link.NOT_OWN_ACCOUNT`);
    the funds when the plan funds a place other than Google Search that Admiry runs from the balance, or, once Admiry
    runs Google Search, for a business that prepays; then a change."""
    prepays = _prepays(rows)
    if not prepays and _has_google(rows) and plans.google_open():
        return envelope.actions(MANAGED_ACTION, OWN_ACCOUNT_ACTION, ADJUST_ACTION)
    from_balance = any(r["mode_text"] == plans.MANAGED_TEXT and r["platform"] != "google_search" for r in rows)
    funds = from_balance or (prepays and plans.google_open())
    return envelope.actions(*([] if prepays else [LINK_ACTION]), *([PREPAY_ACTION] if funds else []), ADJUST_ACTION)


def _has_google(rows: list[dict]) -> bool:
    return any(r["platform"] == "google_search" for r in rows)


def google_way(rows: list[dict]) -> str | None:
    """How Google Search runs for this business, in the plan answer's words (the e-mail channel's connect reply says
    the same): once Admiry runs Google Search itself (`plans.google_open`, A3c-1), for a business that prepays Admiry
    running it from the balance, and for one that does not both ways, its own account or a prepaid one; until then its
    own Google Ads account, and for a business that prepays the wait. Read off the plan card's rows (`_prepays`); None
    for a plan without Google Search, whose places say how they run in `balance_ways` (final review A5)."""
    if not _has_google(rows):
        return None
    if plans.google_open():
        return PREPAID_RUNS_SAY if _prepays(rows) else PREPAY_SAY
    return PREPAID_WAITS_SAY if _prepays(rows) else OWN_ACCOUNT_SAY


def balance_ways(rows: list[dict]) -> list[str]:
    """The plan answer's sentence for each place other than Google Search that Admiry runs from the balance."""
    return [
        f"Admiry runs {r['label']} from your balance."
        for r in rows
        if r["mode_text"] == plans.MANAGED_TEXT and r["platform"] != "google_search"
    ]


def links_own_account(rows: list[dict]) -> bool:
    """Whether the business's own Google Ads account is a way this plan runs, as LINK_ACTION offers it: a plan with
    Google Search, for a business that does not prepay (one that does cannot link one: google_ads_link.NOT_OWN_ACCOUNT).
    The e-mail's "Connect Google Ads" link follows it (final review A6)."""
    return _has_google(rows) and not _prepays(rows)


def launch_next(
    rows: list[dict], *, draft: bool, website: dict | None = None, funded: bool = False, first: bool = True
) -> tuple[dict, str | None]:
    """The plan answer's `next` and `launch_note`, from the plan card's rows: the build's answer and the home page's
    reload (home_api's `state`) alike, so a reload draws the same launch card. A draft key signs in first; else the
    ways that exist (`_launch_actions`), and when none of them launches anything (a business that prepays, with Google
    Search alone, while Admiry-run Google accounts are not open) `PREPAID_WAITS_SAY` says why; else no note. While the
    plan's "Your website" row (`website`, site_install.row) needs Admiry's code, the four ways come first, in the row's
    order (A3p-5 batch 2 Q6; rulings R2-a, R2-c) -- but not beside the sign-in: their tools need a signed-in business
    (ruling R2-e). The note reads the launch actions alone. `funded` (L1, the home alone): the account step is done
    and the plan waits for the owner's go-ahead, so the card offers the owner's yes (`confirm_action`) and a change;
    `first` (L1 review M2, D6) is whether this would be the business's first launch -- only then does
    AGENT_LAUNCH_REVIEW change that yes's label; its default (True) is right wherever `funded` is never True (the
    build's own answer, never reached with it on)."""
    if draft:
        nxt = SIGN_IN_ACTIONS
    elif funded:
        nxt = envelope.actions(confirm_action(config.load(), first=first), ADJUST_ACTION)
    else:
        nxt = _launch_actions(rows)
    nothing = all(a["tool"] == ADJUST_ACTION["tool"] for a in nxt["actions"])
    ways = (website or {}).get("ways") if (website or {}).get("state") == "needs_code" else None
    if ways and not draft:
        nxt = envelope.actions(*ways, *nxt["actions"])
    return nxt, (PREPAID_WAITS_SAY if nothing else None)


def _result(conn, plan: dict, knowledge: dict, session: dict, principal) -> dict:
    plan["business_name"] = plans.name_for(knowledge) or plans.address_for(knowledge)
    # the plan's "Your website" row (A3p-5 batch 2 Q5): in the cards' setup, its sentence in `say`, its ways in `next`
    website = site_install.row(conn, plan["business_id"])
    c = plans.cards(plan, final_host=landing.final_host(conn, plan["business_id"]), website=website)
    est = plan["sections"]["estimate"]
    p = est["columns"]["planning"]
    draft = principal.key.cls == "draft"
    # the primary first, then every other funded row in its own words; who runs what, when it is not the Google account
    rows = c["plan"]["channels"]
    first = plans.primary(plan["sections"]).get("channel", "google_search")
    others = "".join(f" {r['why']}" for r in rows if r["platform"] != first)
    if est.get("basis") == "none":  # no funded channel has a published cost per lead (NewsBreak alone)
        forecast = f"No estimate yet: {est['reason']}."
    else:
        # the funnel's second number is the economics table's "Estimated bookings" row: booked, whatever the owner
        # asked for (named by the objective it read as calls, the leads row's word; founder's walk, 2026-10-03)
        forecast = (
            f"In 30 days that should bring {p['leads'][0]} to {p['leads'][1]} leads, "
            f"{p['bookings'][0]} to {p['bookings'][1]} of them booked. {est['footnote']}"
        )
    run_it = balance_ways(rows)
    way = google_way(rows)
    if way is not None:  # how Google Search runs for this business, today
        run_it.insert(0, way)
    if website is not None:  # then where Admiry's code stands on the page the ads use (ruling R2-b)
        run_it.append(website["say"])
    say = (
        f"Here is the plan: {c['plan']['goal']}. {registry.label(first)} first, {plans.usd(c['budget']['per_day'])} "
        f"a day, about {plans.usd(c['budget']['monthly_usd'])} a month.{others} {forecast} {' '.join(run_it)} "
        f"Details: {plans.plan_url(plan['id'])}" + (f" {SIGN_IN_SAY}" if draft else "")
    )
    nxt, launch_note = launch_next(rows, draft=draft, website=website)
    payload = {
        "plan_id": plan["id"],
        "plan_url": plans.plan_url(plan["id"]),
        "version": plan["version"],
        "status": plan["status"],
        "lines": [plan["lines"][k] for k in plans.SECTION_ORDER],
        "cards": c,
        "notes": plan["notes"],
        "steps": steps(session, knowledge, planned=True),
        "launch_note": launch_note,  # why nothing offered can launch yet; null when something can
    }
    return envelope.make(
        payload,
        say=say,
        next=nxt,
        notes="Show every card and the footnote; never add numbers of your own.",
    )


@agent_tool(cls="W", scope="manage", app=PLAN_VIEW)
def recommend(session_id: str, idempotency_key: str) -> dict:
    """Build the plan from a finished consultation: the goal, the channels with each one's amount and reason
    (Google Search first), the budget with its levels, what the budget should do in 30 days (two columns, every assumption with
    its basis), and the way to launch. Show every card and the footnote to the owner; never add numbers of
    your own. Call it when answer returned ready: true (or whoami shows an open session that is ready)."""

    def prepare(conn, p):
        auth.check_llm_cap(conn, p)
        biz = need_business(p)
        s = _session(conn, biz, session_id)
        # an open session builds once nothing is left to ask (its questions changed under it), as a ready one does
        todo = consult.pending(s) if s["status"] == "open" else []
        if todo:
            many = len(todo) != 1
            raise ToolError(
                f"{len(todo)} question{'s' if many else ''} still open ({', '.join(q['id'] for q in todo)}); "
                + ("they need answers first" if many else "it needs an answer first")
            )
        if s["status"] not in ("open", "ready"):
            raise ToolError(
                f"this consultation is {s['status']}; begin a new consultation for a new plan, or change this one"
            )
        knowledge = _knowledge(conn, biz)
        goal = business.latest_goal(conn, biz)
        tree = consult.load_tree(s["tree"])
        spend = draft_budget(conn, biz, knowledge, goal)
        try:
            gen = llm.generator()
        except llm.LLMError as e:
            raise ToolError(str(e)) from e
        facts = {**consult.facts(s, tree), "channel": "google_search", "monthly_budget_usd": spend}
        given = strategist.knowledge_for(config.load(), s["tree"], knowledge, facts, consult.service_flags(s, tree))
        research = researcher.for_strategist(conn, config.load(), biz)
        draft = draft_with_copy(
            gen,
            knowledge,
            {**(goal or {}), "monthly_limit_usd": spend},
            biz=biz,
            consultation=facts,
            given=given,
            research=research,
            avoid=ads_refusal.avoid_words(conn, config.load(), biz),
        )
        return (
            s,
            knowledge,
            tree,
            build(
                conn,
                biz,
                s,
                knowledge,
                level="recommended",
                monthly_usd=None,
                draft=draft,
                previous=None,
                research_id=research["id"] if research else None,
            ),
        )

    def body(conn, p, ctx):
        s, knowledge, tree, (sections, basis, notes, _built_on) = ctx
        # the goal as it stands now, read again: prepare read it around the model's draft (about 30 seconds), so a
        # target, a limit or a focus the owner changed meanwhile from another surface is kept -- every goal written
        # below is written from this row, under change_goal's own lock (one change at a time per business)
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"goal:{p.business_id}",))
        goal = business.latest_goal(conn, p.business_id)
        # the goal counts what the owner asked the ads for, unless they stated what it counts (a known fact): only the
        # default type follows the objective, so a type the owner set (change_goal) is never touched
        wanted = {"calls": "calls", "bookings": "bookings"}.get(consult.facts(s, tree)["objective"], "customers")
        stated = (s.get("known_facts") or {}).get("goal_type")
        if goal and not stated and goal["goal_type"] == "customers" and wanted != "customers":
            goal = business.set_goal(
                conn,
                p.business_id,
                goal_type=wanted,
                target=goal["target"],
                monthly_limit_usd=goal["monthly_limit_usd"],
                focus_services=goal["focus_services"],
                limit_source=goal["limit_source"],
            )
        if not goal or not goal.get("monthly_limit_usd"):
            # No budget the owner stated: the aggressive level (the largest number the card shows)
            # becomes the ceiling adjust can reach, marked "system" so build() never reads it back as the
            # owner's budget; the plan itself still spends the recommended level (sections["ads"]'s channel).
            # With no goal before this plan, the owner stated nothing about it (a stated type writes one at start),
            # so the goal written here counts what they asked for; an earlier goal's type is kept.
            goal = business.set_goal(
                conn,
                p.business_id,
                goal_type=business.KEEP if goal else wanted,
                target=goal.get("target") if goal else None,
                monthly_limit_usd=sections["levels"]["aggressive"],
                focus_services=goal.get("focus_services", []) if goal else [],
                limit_source="system",
            )
        plan = plans.create_version(
            conn,
            p.business_id,
            goal_id=goal["id"],
            sections=sections,
            estimate_basis=basis,
            rationale=sections["rationale"]["why_primary"],
            notes=notes,
        )
        consult.mark_planned(conn, s["id"], plan["id"])
        return _result(conn, plan, knowledge, {**s, "status": "planned"}, p)

    return run(
        "recommend",
        "manage",
        {"session_id": session_id, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
    )


ADJUST_FIELDS = (
    "monthly_budget_usd",
    "level",
    "radius_miles",
    "offer",
    "objective",
    "channel",
    "channel_monthly_usd",
)


def adjust_prepare(
    conn,
    biz: str,
    plan_id: str,
    *,
    monthly_budget_usd: float | None = None,
    level: str | None = None,
    radius_miles: float | None = None,
    offer: str | None = None,
    objective: str | None = None,
    channel: str | None = None,
    channel_monthly_usd: float | None = None,
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
):
    """What `adjust` works out before its transaction (and `change_plan`, when the owner's words name one of
    ADJUST_FIELDS; and the "Your ads" page, spec 2026-10-06 Y12): the checks, the consultation with the edits, and the
    rebuilt sections. `biz` is the business id (the tools pass `need_business(p)`; the page the owner's own).
    `headlines` / `descriptions`, with `channel`, replace that entry's words before the rebuild (`plan_words.swapped`:
    every entry copying them follows) -- the page's own fields, never the MCP tool's (main spec principle 2). Words
    for a platform the plan does not fund are refused: there is no entry to put them on. The values' types are
    checked first, here where every path meets: `adjust` is typed by FastMCP and `change_plan` by the reader's model,
    but the public tools pass on whatever an assistant put inside `changes` (final review B, Important 6)."""
    numbers = {
        "monthly_budget_usd": monthly_budget_usd,
        "radius_miles": radius_miles,
        "channel_monthly_usd": channel_monthly_usd,
    }
    for name, value in numbers.items():  # true is no number, though Python counts a bool as an int
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ToolError(f"{name} must be a number")
    for name, value in {"level": level, "offer": offer, "objective": objective, "channel": channel}.items():
        if value is not None and not isinstance(value, str):
            raise ToolError(f"{name} must be text")
    prev = plans.get(conn, biz, plan_id)
    if prev is None or "consultation" not in prev["sections"]:
        raise ToolError("no plan with that id for this business that came from a consultation")
    words = headlines is not None or descriptions is not None
    if words:
        if channel is None:
            raise ToolError("say which channel the words are for")
        if ads_run.entry(prev, channel) is None:
            raise ToolError(f"{registry.label(channel)} is not in your plan")
        prev = {**prev, "sections": plan_words.swapped(prev["sections"], channel, headlines or [], descriptions or [])}
    if level is not None and level not in LEVEL_NAMES:
        raise ToolError("level must be minimum, conservative, recommended or aggressive")
    if monthly_budget_usd is not None and not 0 < float(monthly_budget_usd) <= 100_000:
        raise ToolError("monthly_budget_usd must be between 1 and 100,000")
    if radius_miles is not None and not 0 < float(radius_miles) <= 100:
        raise ToolError("radius_miles must be between 1 and 100")
    if objective is not None and objective not in plans.OBJECTIVE_WORDS:
        raise ToolError(f"objective must be one of {', '.join(plans.OBJECTIVE_WORDS)}")
    changes = (
        monthly_budget_usd,
        level,
        radius_miles,
        offer,
        objective,
        channel,
        channel_monthly_usd,
        headlines,
        descriptions,
    )
    if all(v is None for v in changes):
        raise ToolError(
            "say what to change: monthly_budget_usd, level, radius_miles, offer, objective, or a channel's amount"
        )
    named = plans.pins(prev["sections"])
    s = plan_session(conn, biz, plan_id)
    knowledge = _knowledge(conn, biz)
    if channel_monthly_usd is not None or (channel is not None and not words):  # a channel named for its amount
        category, service = plans_channels.trade(s, consult.load_tree(s["tree"]), knowledge)
        pictured = plans.choose_picture(category, service) is not None
        named[channel] = _pin(conn, biz, channel, channel_monthly_usd, pictured=pictured)
    edits = {}
    if radius_miles is not None:
        edits["radius"] = {"miles": float(radius_miles), "raw": radius_miles, "source": "choice"}
    if offer is not None:
        edits["offer"] = {"note": str(offer).strip()[: consult.MAX_OTHER], "raw": offer, "source": "choice"}
    if objective is not None:
        edits["objective"] = {"choice": objective, "raw": objective, "source": "choice"}
    s = {**s, "answers": {**s["answers"], **edits}}
    if monthly_budget_usd is not None:
        lvl, spend = "custom", float(monthly_budget_usd)
    elif level is not None:
        lvl, spend = level, None
    else:  # no budget change: the previous spend carries, whatever its label ("custom" is only a label)
        lvl = prev["sections"]["consultation"]["level"]
        spend = plans.total_usd(prev["sections"])
    return (
        prev,
        s,
        edits,
        knowledge,
        build(
            conn,
            biz,
            s,
            knowledge,
            level=lvl,
            monthly_usd=spend,
            draft=None,
            previous=prev["sections"],
            named=named,
        ),
    )


def adjust_commit(conn, biz: str, ctx) -> dict:
    """`adjust`'s writes, and the "Your ads" page's (spec 2026-10-06 Y12): the edits recorded on the consultation, the
    new version written from the context (`diff_from` the version it starts from; `create_version` supersedes an open
    draft), the session marked with it. Returns the plan, a draft."""
    prev, s, edits, _, (sections, basis, notes, goal) = ctx
    if edits:
        consult.record(conn, s, edits)
    plan = plans.create_version(
        conn,
        biz,
        goal_id=goal["id"] if goal else None,
        sections=sections,
        estimate_basis=basis,
        rationale=sections["rationale"]["why_primary"],
        notes=notes,
        diff_from=prev["id"],
    )
    consult.mark_planned(conn, s["id"], plan["id"])
    return plan


def adjust_body(conn, p, ctx) -> dict:
    """`adjust`'s transaction: the version written (`adjust_commit`), the plan's envelope."""
    _prev, s, _edits, knowledge, _built = ctx
    plan = adjust_commit(conn, p.business_id, ctx)
    return _result(conn, plan, knowledge, s, p)


@agent_tool(cls="W", scope="manage", app=PLAN_VIEW)
def adjust(
    plan_id: str,
    idempotency_key: str,
    monthly_budget_usd: float | None = None,
    level: str | None = None,
    radius_miles: float | None = None,
    offer: str | None = None,
    objective: str | None = None,
    channel: str | None = None,
    channel_monthly_usd: float | None = None,
) -> dict:
    """Change the plan without starting over: another monthly budget or level (minimum = $10 a day /
    conservative / recommended / aggressive), another radius, offer or objective, or how much one channel gets
    (`channel`, a row's platform in the plan card such as newsbreak, with `channel_monthly_usd`: that amount stays
    set through later changes and the rest of the budget is split around it). Returns a new version with the cards
    recomputed; the old version is kept. A budget above the owner's monthly limit is refused (raise it with set_goal)."""
    fields = {
        "monthly_budget_usd": monthly_budget_usd,
        "level": level,
        "radius_miles": radius_miles,
        "offer": offer,
        "objective": objective,
        "channel": channel,
        "channel_monthly_usd": channel_monthly_usd,
    }

    def prepare(conn, p):
        return adjust_prepare(conn, need_business(p), plan_id, **fields)

    return run(
        "adjust",
        "manage",
        {"plan_id": plan_id, "idempotency_key": idempotency_key, **fields},
        adjust_body,
        idempotency_key=idempotency_key,
        prepare=prepare,
    )


def _pin(conn, biz: str, channel: str | None, amount: float | None, *, pictured: bool = True) -> int:
    """The whole-dollar amount the owner sets for one channel (spec 2026-09-29 §7.1 adjust), or ToolError with the
    way forward: a channel with an adapter that this business can have (`plans_channels.held`: switched on, a picture
    for the trade when its ads show one -- `pictured`, M1 Task 7 fix round 1 -- a way to pay for it), at least its
    minimum, within the owner's own cap for it."""
    if channel is None or amount is None:
        raise ToolError("give channel and channel_monthly_usd together")
    if channel not in registry.LABELS:
        raise ToolError("unknown channel")
    label = registry.label(channel)
    if not registry.known(channel):
        raise ToolError(f"{label} is not in Admiry yet")
    usable = plans_channels.usable_modes(conn, biz)
    why = plans_channels.held(config.load(), usable, pictured=pictured).get(channel)
    if why is not None:
        raise ToolError(plans.held_note(channel, why))
    if not 0 < float(amount) <= 100_000:
        raise ToolError("channel_monthly_usd must be between 1 and 100,000")
    floor = registry.adapter(channel).capabilities().floor(plans_channels.modes(conn, biz).get(channel))  # A3c-1
    if float(amount) < floor:
        raise ToolError(f"{label} needs at least {plans.usd(floor)} a month")
    cap = ((business.latest_goal(conn, biz) or {}).get("channel_caps") or {}).get(channel)
    if cap is not None and float(amount) > cap:
        raise ToolError(f"{label} is limited to {plans.usd(cap)} a month by your goal; raise that limit first")
    return int(amount)
