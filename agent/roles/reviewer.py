"""The Reviewer (L1, spec 2026-10-10 §6.2): a first launch the owner agreed to (and paid for), reviewed before its ads go
live -- one background job per review (role "reviewer", kind "launch"; queued by launch_review.submit, taken by
research-run whatever AGENT_ROLES says and whatever AGENT_LAUNCH_REVIEW says now -- ruling R16: the switch gates only
whether a yes goes to review; a review once queued is finished). Its steps, each recorded on the review row: the
creative (the Critic's look at this version, the ad text the launch would take, the page checked afresh); the
configuration (the ads built as the launch builds them and sent to Google with validateOnly while the place's switch
for the business is not off); the check (the configured ads against the plan the owner agreed to, in code and by one
model call); then the pass (`approve`): the approval itself -- proposals.apply_approve_plan, as every approval -- and
the launch (ads_run.launch_now). What it cannot pass holds the review for an admin (/ops/review, the admin MCP), with
one e-mail to the operator and one to the owner; a page only the owner can change goes back to them, and a version an
admin already returned once is held for a person again rather than passed a second time without their look (ruling
R17). Nothing a person paid for waits unseen (ruling R19): a plan with no funded Google Search entry, an unexpected
error, and a review a dead run left with no job to finish it are each held for a person too, never left stranded or
failed silently. At most two model calls a version (none for the Critic when its look is stored), counted as
job.reviewer toward ROLE_RUNS_PER_DAY, never the owner's day.

Google's refusals (spec 2026-10-11 §5): a refused validateOnly is read in detail and classified -- ad text and search
phrases Google refuses are dropped (ads_refusal, the owner told once) and the ads sent once more; the owner's own page
or a fact about the business goes back to the owner with its fix; Admiry's own page, a second refusal, a spent count
of VALIDATIONS_PER_VERSION answered calls, or anything Admiry cannot place holds for a person."""

from __future__ import annotations

import logging
from dataclasses import replace

import psycopg
from fastmcp.exceptions import ToolError

from .. import (
    ads_refusal,
    ads_run,
    allocate,
    audit,
    blockers,
    business,
    config,
    landing,
    launch_review,
    llm,
    local_rules,
    mail,
    mail_out,
    plan_review,
    plans,
)
from ..channels import registry
from ..channels.base import Build, PlatformError
from ..channels.google_search import adapter as google
from ..channels.google_search import build as google_build
from ..channels.google_search import client as google_ads
from ..channels.google_search import refusals
from ..channels.google_search.client import RefusalDetail
from . import queue

log = logging.getLogger(__name__)
ROLE, KIND = launch_review.ROLE, launch_review.KIND
TOOL = "job.reviewer"  # the Reviewer's model calls, counted toward ROLE_RUNS_PER_DAY (the Critic's look records it too)
PASS_TOOL = "job.review_pass"  # the pass's own row: no model call, not counted
BUDGET_S = 60.0  # Google's time per validateOnly call -- up to two a run (a drop's own re-validate)
REVIEW_SECONDS = 600  # one research-run's time for reviews, one at a time
# why a review waits for a person, in the admin's words (the owner reads launch_review.HELD_SAY)
CRITIC_SAYS = "The Critic's verdict on this version is {verdict} (score {score:.2f})"
NO_COPY = "Too little ad text survives the copy linter"
PAGE_FAILS = "The page check fails: {fix}"
NO_PAGE_CHECK = "The page could not be checked: {why}"
ACCOUNT_WAITS = "The Google Ads account is not ready: {reason}"
GOOGLE_REFUSED = "Google refused the configuration: {error}"
DIFFERS = "The configured ads differ from the agreed plan: {what}"
MODEL_SAYS = "The configured-ads check found issues: {issues}"
MODEL_DOWN = "The model could not answer after {n} tries"
CAPPED = "The business's background model calls for today are spent"
APPROVAL_REFUSED = "The approval's own check refused it: {sentence}"
# R17: a person (an admin) returned this version to its owner before -- the Reviewer holds it for a person again
# instead of passing it a second time without their look (launch_review.returned_before)
RETURNED_BEFORE = "An admin returned this version before: {note}"
# R19b: a plan with no funded Google Search entry would strand configured_material (google._search_channel raises
# NOT_SEARCH_PLAN) -- held at once, before any step, instead
NO_SEARCH = "This plan runs no Google Search ads for the Reviewer to check"
# R19a: a paid review must reach a person, never wait unseen -- any exception review_plan did not itself expect
# (a bug, a dependency down in a way none of its own try/excepts name) holds the review instead of failing silently
UNEXPECTED = "The Reviewer stopped on an error: {error}"
# R19c: a review the Reviewer took and never finished -- no job waits or runs for it any more (a run that died, a
# retry that could not be queued) -- held for a person so no paid review waits unseen
STRANDED = "The Reviewer's run stopped before it finished"
NOTHING_WAITS = "No job waits for that review."
HOLD_SUBJECT = "Admiry is checking one thing about your ads"
RETURN_SUBJECT = "Your ads need one change before they go live"
# spec 2026-10-11 §5: at most this many validateOnly calls Google answered, a plan version (launch_review.validations:
# every review of the version; Part B's check before the payment spends from the same count) -- the Reviewer's own
# calls alone; approve's own ads_run.launch_now, right after the pass, is the launch itself, never counted here. The
# fourth validateOnly is never made.
VALIDATIONS_PER_VERSION = 3
CHANGED_STEP = "changed"  # the step that marks the owner told of what Admiry dropped (plan D10)
# why a review waits for a person, in the admin's words (the owner reads launch_review.HELD_SAY)
BUDGET_SPENT = "Google was asked about this version {n} times already; no further validation is sent"
GOOGLE_REFUSED_AGAIN = "Google refused the configuration again after Admiry dropped what it refused: {error}"
TOO_LITTLE_AFTER_DROP = (
    "Too little ad text survives Google's refusals and Admiry's rules: {h} headlines and {d} descriptions left"
)
DESTINATION_HELD = "Google refused Admiry's own page for the ads ({topic})"
# the owner's notes on a return (spec §5, plan D6) -- owner words, never Google's -- each with the resolution the
# checklist fixes it with (launch_check.items: the phone's question, the page check; none for a fact only a person
# can name)
PHONE_REFUSED_NOTE = (
    "Google did not accept the phone number your ads would show; give Admiry the number your customers call."
)
BUSINESS_NOTE = (
    "Google needs something about your business before these ads can run; write to hello@admiry.ai and Admiry "
    "will say what."
)
PAGE_REFUSED_NOTE = "Google could not use your page for these ads as it is; fix the page, then check it again."
# follow-up 2026-10-12: the destination detail's own topic, in owner words -- first match wins; an unknown topic, or
# none, keeps PAGE_REFUSED_NOTE unchanged
PAGE_REFUSED_WHY = {
    "DESTINATION_NOT_WORKING": "it did not open when Google tried it",
    "DESTINATION_MISMATCH": "its address is not the website your ads show",
    "DESTINATION_NOT_CRAWLABLE": "Google is not allowed to read it",
}
PAGE_REFUSED_WHY_NOTE = "Google could not use your page for these ads: {why}. Fix the page, then check it again."
CHANGED_SUBJECT = "A change Admiry made to your ads"


class _TryAgain(Exception):
    """A step that could not run this time (the model, or Google, busy): the job waits for the next run."""


def run_job(conn: psycopg.Connection, settings: config.Settings, job: dict) -> tuple[str, dict]:
    """research-run's handler for a review's job: skipped once the review is decided or its plan left review (R19d:
    withdrawn first, so an admin's own decision meanwhile is never overwritten); else its steps (`review_plan`),
    tried again at the next run when the model or Google could not answer (queue.MODEL_UNAVAILABLE, up to
    runner.ATTEMPTS; the last try holds the review instead). R19a: a ToolError whose text is launch_review.DECIDED
    (an admin decided meanwhile -- re-raised by `approve`, raised by `back_to_owner`'s decide) is skipped the same
    way; any other exception holds the review for a person instead of failing silently -- a paid review must reach
    a person, never wait unseen."""
    from . import runner  # runner imports this module for its handlers

    review = launch_review.get(conn, (job["brief"] or {}).get("review_id") or "")
    if review is None or review["state"] not in launch_review.RUNNING:
        return queue.SKIPPED, {"reason": "decided"}
    plan = plans.get(conn, review["business_id"], review["plan_id"])
    if plan is None or plan["status"] != plans.IN_REVIEW:
        try:
            launch_review.decide(conn, review["id"], state="withdrawn", by="reviewer")
        except ToolError as e:
            if str(e) != launch_review.DECIDED:
                raise
        return queue.SKIPPED, {"reason": "not in review"}
    if runner.runs_today(conn, review["business_id"]) >= runner.ROLE_RUNS_PER_DAY:
        return queue.DONE, hold(conn, settings, review, CAPPED)
    try:
        return queue.DONE, review_plan(conn, settings, review, plan, last=job["rounds"] >= runner.ATTEMPTS)
    except _TryAgain:
        return queue.FAILED, {"reason": queue.MODEL_UNAVAILABLE}
    except Exception as e:  # noqa: BLE001 - a paid review must reach a person, never wait unseen
        if isinstance(e, ToolError) and str(e) == launch_review.DECIDED:
            return queue.SKIPPED, {"reason": "decided"}
        log.warning("reviewer: review %s stopped on an unexpected error: %s", review["id"], type(e).__name__)
        return queue.DONE, hold(conn, settings, review, UNEXPECTED.format(error=f"{type(e).__name__}: {e}"[:300]))


def review_plan(conn: psycopg.Connection, settings: config.Settings, review: dict, plan: dict, *, last: bool) -> dict:
    """The steps (see the module docstring); what happened, for the job's result."""
    from . import runner

    biz, rid = review["business_id"], review["id"]
    # R19b: a plan with no funded Google Search entry would strand configured_material; held at once, before any
    # step (plan_review.look reads any funded channel, so this must come first)
    if allocate.PRIMARY not in ads_run.funded_channels(plan):
        return hold(conn, settings, review, NO_SEARCH)
    # 1. the creative
    looked = plan_review.look(conn, settings, biz, by=plan_review.By.job(ROLE), plan_id=plan["id"])
    if looked is None:
        return {"skipped": "superseded"}
    if looked.review_id is None:
        if not last:
            raise _TryAgain
        return hold(conn, settings, review, MODEL_DOWN.format(n=runner.ATTEMPTS))
    try:
        fix, owners = _page_now(conn, biz)
    except ToolError as e:
        launch_review.step(conn, rid, name="creative", ok=False, detail={"page": str(e)})
        return hold(conn, settings, review, NO_PAGE_CHECK.format(why=e))
    headlines, descriptions, dropped = _ad_copy(plan, ads_refusal.dropped(conn, biz, settings=settings))
    short = len(headlines) < plans.MIN_HEADLINES or len(descriptions) < plans.MIN_DESCRIPTIONS
    # ruling L2 (final review): a line Google refused for any version of the business stays out of this one too --
    # when the copy linter alone leaves enough, it is Google's refusals that leave too little, held as that
    no_copy = (
        NO_COPY if google.copy_short(plan) else TOO_LITTLE_AFTER_DROP.format(h=len(headlines), d=len(descriptions))
    )
    detail = {
        "review_id": looked.review_id,
        "verdict": looked.verdict,
        "score": looked.score,
        "copy_short": short,
        "dropped": dropped,  # D1: the dropped lines, for the admin, when short holds it
        "page": fix,
    }
    if fix and owners:  # the owner's own page: only they can change it
        launch_review.step(conn, rid, name="creative", ok=False, detail=detail)
        return back_to_owner(conn, settings, review, state="returned", by="reviewer", note=fix)
    reason = (
        PAGE_FAILS.format(fix=fix)
        if fix
        else no_copy
        if short
        else None
        if looked.verdict == "deliver"
        else CRITIC_SAYS.format(verdict=looked.verdict, score=looked.score or 0)
    )
    launch_review.step(conn, rid, name="creative", ok=reason is None, detail=detail, state="creative")
    if reason:
        return hold(conn, settings, review, reason)
    # 2. the configuration
    built: Build | None = None
    if registry.exec_mode_for(conn, settings, biz, allocate.PRIMARY) != "off":
        status = ads_run.account_status(conn, settings, biz, allocate.PRIMARY)
        if not status.ready:
            launch_review.step(conn, rid, name="configured", ok=False, detail={"account": status.reason})
            return hold(conn, settings, review, ACCOUNT_WAITS.format(reason=status.reason))
        validated = _validate(conn, settings, review, plan, last=last)
        if isinstance(validated, dict):  # held, or returned to the owner
            return validated
        built = validated
    sent = {"sent": built is not None, **(built.summary if built else {})}
    launch_review.step(conn, rid, name="configured", ok=True, detail=sent, state="configured")
    # 3. the check
    material = configured_material(conn, settings, biz, plan, built)
    what = differs(material)
    if what:
        launch_review.step(
            conn,
            rid,
            name="checked",
            ok=False,
            detail={"differs": what, "agreed": material["agreed"], "configured": material["configured"]},
        )
        return hold(conn, settings, review, DIFFERS.format(what=what))
    try:
        read = llm.generator(settings).review_configured(material)
    except llm.LLMError as e:
        _model_row(conn, review, {"model": "unavailable", "error": str(e)})
        if not last:
            raise _TryAgain from None
        return hold(conn, settings, review, MODEL_DOWN.format(n=runner.ATTEMPTS))
    _model_row(conn, review, {"model": "ok", "matches": read.matches, "issues": len(read.issues)})
    fine = read.matches and not read.issues
    # D1 (spec §7): the comparison itself on the row, for the admin -- never shown to the owner
    launch_review.step(
        conn,
        rid,
        name="checked",
        ok=fine,
        detail={"agreed": material["agreed"], "configured": material["configured"], **read.model_dump()},
        state="checked",
    )
    if not fine:
        return hold(conn, settings, review, MODEL_SAYS.format(issues="; ".join(read.issues) or "it does not match"))
    # R17: a person sent this version back before -- it is held for a person again, never passed on the Reviewer's own
    again = launch_review.returned_before(conn, plan["id"], rid)
    if again:
        return hold(conn, settings, review, RETURNED_BEFORE.format(note=again.rstrip(".")))
    # 4. the pass
    return approve(conn, settings, launch_review.get(conn, rid), by="reviewer")


def _validate(
    conn: psycopg.Connection, settings: config.Settings, review: dict, plan: dict, *, last: bool
) -> Build | dict:
    """Step 2's Google calls (spec 2026-10-11 §5): the ads built as the launch builds them -- every line Google refused
    for the business left out, by its exact text (adapter.build reads ads_refusal.dropped; ruling L2) -- and sent
    with validateOnly, at most VALIDATIONS_PER_VERSION answered calls a version (launch_review.validations; plan D1):
    once those are counted no call is made and the review is held (BUDGET_SPENT). A refusal with details goes to
    `_refused`: ad text and search phrases Google refused are dropped and the ads sent once more in this run (plan
    D2); what only the owner can give goes back to them; the rest holds. A non-retryable refusal with no details
    never answered a mutate (ruling B1/G7, task review) -- held, but never counted. Google not answering is the job's
    own retry, as before -- no step, not counted. Every answered call is counted (`launch_review.count_validation`)
    before anything else runs, whatever the review's state turns out to be right after -- ruling G1: an admin's
    decision (or a new version withdrawing this review) landing while Google was answering wins at once
    (`_require_running`, ToolError DECIDED): nothing else is recorded or sent for a review that is no longer the
    Reviewer's to run. Returns the Build Google accepted, or the hold's / return's result."""
    biz, rid = review["business_id"], review["id"]
    adapter = registry.adapter(allocate.PRIMARY)
    account = ads_run._account(conn, biz, allocate.PRIMARY)
    knowledge = (business.latest_knowledge(conn, biz) or (None, {}))[1]
    dropped_now = False
    while True:
        spent = launch_review.validations(conn, plan["id"])
        if spent >= VALIDATIONS_PER_VERSION:
            launch_review.step(conn, rid, name="configured", ok=False, detail={"validations": spent})
            return hold(conn, settings, review, BUDGET_SPENT.format(n=spent))
        try:
            with adapter.client(budget_s=BUDGET_S) as http:
                built = adapter.build(conn, settings, biz, plan, knowledge, account, http=http)
                adapter.launch(conn, settings, account, built, {}, http=http, validate_only=True)
        except PlatformError as e:
            if e.retryable:  # Google did not answer: it processed nothing, and nothing is counted
                if not last:
                    raise _TryAgain from None
                launch_review.step(conn, rid, name="configured", ok=False, detail={"google": str(e)})
                return hold(conn, settings, review, GOOGLE_REFUSED.format(error=e))
            if not e.details:  # ruling B1/G7: not a mutate's answer -- held, never counted toward the budget
                launch_review.step(conn, rid, name="configured", ok=False, detail={"google": str(e)})
                return hold(conn, settings, review, GOOGLE_REFUSED.format(error=e))
            final, drops, rest = _classify(conn, settings, biz, plan, e)
            launch_review.count_validation(conn, rid, ok=False, detail={"google": str(e), "refusals": final})
            _require_running(conn, rid)
            outcome = _refused(conn, settings, review, plan, e, drops=drops, rest=rest, again=dropped_now)
            if outcome is not None:
                return outcome
            dropped_now = True  # dropped: the ads once more, within the budget
        except ToolError as e:
            launch_review.step(conn, rid, name="configured", ok=False, detail={"google": str(e)})
            return hold(conn, settings, review, GOOGLE_REFUSED.format(error=e))
        else:
            launch_review.count_validation(conn, rid, ok=True, detail={"validations": spent + 1})
            _require_running(conn, rid)
            return built


def _require_running(conn: psycopg.Connection, review_id: str) -> None:
    """Ruling G1 (task review fix round 1): right after an answered validateOnly call is counted, an admin's
    decision -- or a new version withdrawing this review -- that landed while Google was still answering wins:
    ToolError DECIDED, so `run_job` ends the job SKIPPED (R19a) with nothing else recorded or e-mailed for a review
    that is no longer the Reviewer's to run."""
    if (launch_review.get(conn, review_id) or {}).get("state") not in launch_review.RUNNING:
        raise ToolError(launch_review.DECIDED)


def _classify(
    conn: psycopg.Connection, settings: config.Settings, biz: str, plan: dict, e: PlatformError
) -> tuple[list[dict], list[tuple[RefusalDetail, str]], list[tuple[RefusalDetail, str]]]:
    """Every detail Google's refusal carries (spec 2026-10-11 §5; plan D6, D13), classified (refusals.classify)
    then, for ad_text/keyword, matched against what this call actually sent (ruling G6, task review fix round 1):
    the lines `_ad_copy` still keeps -- never the agreed text before the copy linter or an earlier drop -- and the
    search phrases `google_build.built_keywords` actually builds over the same search view the build sends
    (`google._search_view`, ruling N1, final review: `built_keywords` reads `channels[0]`, so a multi-channel draft
    listing another channel first must still see Google Search's entry there, not whichever the owner funded
    first). An ad_text/keyword detail with no match is `other` (a person's), never a wasted drop the owner is told
    about for nothing. Returns, in `e.details`' own order, the final class of every detail (ruling G8: what the
    validated step's own record uses, so it never shows a class the ads_refusal row does not), the lines/phrases to
    drop (one ad_text detail naming no single line may expand into several plan lines), and everything else to
    hold or return."""
    knowledge = (business.latest_knowledge(conn, biz) or (None, {}))[1]
    dropped_already = ads_refusal.dropped(conn, biz, settings=settings)
    headlines, descriptions, _notes = _ad_copy(plan, dropped_already)
    built = google_build.built_keywords(google._search_view(plan), knowledge, dropped_already)
    final: list[dict] = []
    drops: list[tuple[RefusalDetail, str]] = []
    rest: list[tuple[RefusalDetail, str]] = []
    for d in e.details:
        c = refusals.classify(d)
        if c == refusals.KEYWORD and d.text and d.text in built:
            drops.append((d, c))
        elif c == refusals.AD_TEXT and (lines := _refused_lines(d, headlines, descriptions)):
            drops += [(line, c) for line in lines]
        elif c in (refusals.AD_TEXT, refusals.KEYWORD):
            c = refusals.OTHER  # a line or a phrase Admiry cannot find in what was actually sent: a person's
            rest.append((d, c))
        else:
            rest.append((d, c))
        final.append({**d.as_dict(), "class": c})
    return final, drops, rest


def _refused(
    conn: psycopg.Connection,
    settings: config.Settings,
    review: dict,
    plan: dict,
    e: PlatformError,
    *,
    drops: list[tuple[RefusalDetail, str]],
    rest: list[tuple[RefusalDetail, str]],
    again: bool,
) -> dict | None:
    """What Google's already-classified refusal (`_classify`) means for the review (spec 2026-10-11 §5; plan D6,
    amended by ruling G4, task review fix round 1): a second refusal in this run (`again`) holds the whole
    classified set (GOOGLE_REFUSED_AGAIN), whatever its class -- ad text is otherwise dropped first whatever else is
    refused, so a resubmitted version is not refused for it again. Else `drops` (ad text, search phrases: Admiry's
    to fix) are recorded; when that leaves too little ad text, the review holds for the back office
    (TOO_LITTLE_AFTER_DROP) *before* anything in `rest` is looked at -- ruling G4: a too-thin draft must never reach
    the owner's checklist as a return (plan D3), even when the same call also named a business fact only the owner
    could answer -- and `rest`, if any, is recorded `held` too in that case, never `returned`. Otherwise, among
    `rest`: any `other` holds (GOOGLE_REFUSED); a `destination` while the ads use Admiry's own page holds
    (DESTINATION_HELD); else one return with its note and resolution (`back_to_owner`). None when only `drops` was
    found and enough ad text survives: the caller sends the ads once more."""
    biz, rid, pid = review["business_id"], review["id"], plan["id"]
    rules = local_rules.load(conn, settings)  # spec 2026-10-12 §2.3: the stamp -- the rule a refused line matched

    def recorded(rows: list[tuple[RefusalDetail, str]], action: str) -> None:
        ids = ads_refusal.record(
            conn,
            business_id=biz,
            plan_id=pid,
            channel=allocate.PRIMARY,
            review_id=rid,
            found=[(d, c, action) for d, c in rows],
        )
        local_rules.stamp(conn, ids, rules)

    if again:
        recorded([*drops, *rest], ads_refusal.HELD)
        return hold(conn, settings, review, GOOGLE_REFUSED_AGAIN.format(error=e))
    if drops:
        recorded(drops, ads_refusal.DROPPED)
        kept_h, kept_d, _notes = _ad_copy(plan, ads_refusal.dropped(conn, biz, settings=settings))
        if len(kept_h) < plans.MIN_HEADLINES or len(kept_d) < plans.MIN_DESCRIPTIONS:
            if rest:  # ruling G4: nothing is returned once the drop itself leaves too little to run
                recorded(rest, ads_refusal.HELD)
            return hold(conn, settings, review, TOO_LITTLE_AFTER_DROP.format(h=len(kept_h), d=len(kept_d)))
    if rest:
        if any(c == refusals.OTHER for _d, c in rest):
            recorded(rest, ads_refusal.HELD)
            return hold(conn, settings, review, GOOGLE_REFUSED.format(error=e))
        destination = [d for d, c in rest if c == refusals.DESTINATION]
        if destination and blockers.admiry_page(conn, landing.current(conn, biz)) is not None:
            recorded(rest, ads_refusal.HELD)
            topic = destination[0].topic or destination[0].code
            return hold(conn, settings, review, DESTINATION_HELD.format(topic=topic))
        note, resolution = _owner_note([d for d, c in rest if c == refusals.BUSINESS], destination)
        recorded(rest, ads_refusal.RETURNED)
        return back_to_owner(conn, settings, review, state="returned", by="reviewer", note=note, resolution=resolution)
    return None


def _refused_lines(detail: RefusalDetail, headlines: list[str], descriptions: list[str]) -> list[RefusalDetail]:
    """The plan's own lines a refused ad-text detail names, each as its own detail (kind headline or description,
    text the line; plan D6): the line as sent when Google's path named one (the client reads the sent line, D16),
    else every line that holds the text Google quoted, case-insensitively. [] when nothing in the plan matches:
    the caller holds it for a person."""
    out: list[RefusalDetail] = []
    if not detail.text:
        return out
    for kind, pool in (("headline", headlines), ("description", descriptions)):
        if detail.kind not in (kind, "ad"):
            continue
        hits = [t for t in pool if t == detail.text] or [t for t in pool if detail.text.lower() in t.lower()]
        out += [replace(detail, kind=kind, text=t) for t in hits]
    return out


def _owner_note(business_facts: list[RefusalDetail], destination: list[RefusalDetail]) -> tuple[str, str | None]:
    """The return's note and its resolution (plan D6): the call asset's phone first (the owner answers it on the
    list), another fact about the business (the owner writes to Admiry), else the owner's own page (checked again) --
    with why Google refused it, in owner words, from the first destination detail whose topic PAGE_REFUSED_WHY
    knows (follow-up 2026-10-12), skipping any earlier one it does not; no detail with a known topic, or none at
    all, keeps PAGE_REFUSED_NOTE's shape unchanged."""
    if any(d.kind == "phone" for d in business_facts):
        return PHONE_REFUSED_NOTE, "phone"
    if business_facts:
        return BUSINESS_NOTE, None
    for d in destination:
        why = PAGE_REFUSED_WHY.get((d.topic or "").upper())
        if why:
            return PAGE_REFUSED_WHY_NOTE.format(why=why), "check_page"
    return PAGE_REFUSED_NOTE, "check_page"


def _tell_changed(conn: psycopg.Connection, settings: config.Settings, review: dict) -> None:
    """spec 2026-10-11 §5.1 (plan D10, amended by ruling G5, task review fix round 1): the owner e-mailed once a
    plan version that dropped something -- at the pass, right after its launch (`approve`'s own call, whatever decided
    it: the Reviewer's own, or an admin's), never at the drop itself (a hold or a return sends only its own mail, and
    a drop later undone by a hold is never mailed) -- checked across every review row of the version
    (`launch_review.has_step`), so a resubmitted version that passes later is never told twice. Every line removed
    that is in effect for the version (ads_refusal.changes: its own drops, and an earlier version's it still carries
    -- ruling L2), why in plain words, that the rest stays as agreed and there is nothing to do. Never
    raises (ruling L1, final review): a mail that cannot go out is only logged, and its `changed` step records
    `mail: "failed"` (`ok` only for a mail sent); a read or a write that fails -- the changes, the step -- is only
    logged too, and when the changes could not be read no step is written, so the version is never taken as told
    with nothing said. The pass, its launch and the drop already recorded are never undone by this."""
    biz, rid, pid = review["business_id"], review["id"], review["plan_id"]
    try:
        changes = ads_refusal.changes(conn, biz, pid)
        if not changes or launch_review.has_step(conn, pid, CHANGED_STEP):
            return
        try:
            result = mail.to_owner(
                conn,
                biz,
                "mail_review_changed.txt.j2",
                CHANGED_SUBJECT,
                changes=changes,
                link=f"{settings.web_base_url}/?setup={biz}",
            )
        except Exception as e:  # noqa: BLE001 - the pass stands; a mail that cannot go out is only logged
            log.warning("reviewer: review %s's change was not e-mailed: %s", rid, type(e).__name__)
            result = "failed"
        launch_review.append_step(
            conn, rid, name=CHANGED_STEP, ok=result == "sent", detail={"changes": changes, "mail": result}
        )
    except Exception as e:  # noqa: BLE001 - never raises: the pass and its launch stand whatever this read or write does
        log.warning("reviewer: review %s's change step was not recorded: %s", rid, type(e).__name__)


def _page_now(conn: psycopg.Connection, biz: str) -> tuple[str | None, bool]:
    """The page checked afresh and saved (tools_pages.check_on_file, as Check my page now does), then judged for Google
    Search: (its fix, PAGE_FIX's sentence, or None; whether the page is the owner's own -- not one Admiry wrote).
    ToolError when there is no page with an address to check."""
    from .. import tools_pages  # a tool module: imported when needed, as blockers does

    tools_pages.check_on_file(conn, biz)
    page = landing.current(conn, biz)
    rules = registry.adapter(allocate.PRIMARY).capabilities().page_rules
    return ads_run.page_fix(landing.judged_report(conn, page) or {}, rules), blockers.admiry_page(conn, page) is None


def _ad_copy(
    plan: dict, dropped: google_build.Dropped = google_build.NOTHING_DROPPED
) -> tuple[list[str], list[str], list[str]]:
    """The plan's funded Google Search ad text as a build sends it (google_build.kept_lines: the copy linter with the
    tree's banned words, then the lines Admiry dropped after Google refused them -- `dropped`, ads_refusal.dropped:
    what every Google build of the business leaves out, a line Google refused, by its exact text, ruling L2): the
    headlines and descriptions that survive, and one plain note per line the linter dropped.
    Shared by the creative step (which records the linter's drops for the admin, D1), `_refused` (enough left after a
    drop?) and `configured_material` (which sends the survivors to Google and the model) -- the linting happens
    once."""
    ch = google._search_channel(plan)
    tree = plan["sections"]["consultation"]["business"].get("category") or "fallback"
    return google_build.kept_lines(
        list(ch.get("headlines") or []),
        list(ch.get("descriptions") or []),
        landing.load_recipe(tree)["banned"],
        dropped,
    )


def configured_material(
    conn: psycopg.Connection, settings: config.Settings, biz: str, plan: dict, built: Build | None
) -> dict:
    """What the last look reads: the business, the plan as agreed, and the ads as configured -- the lines the copy linter
    keeps (and the ones it drops), the daily budget, the area and the page from the build when Google was asked."""
    ch = google._search_channel(plan)
    # spec 2026-10-11 §5.1: what Admiry dropped after Google refused it -- the drops in effect for this version (its
    # own, and an earlier version's it still carries: ruling L2), every one of them left out of the build
    removed = ads_refusal.changes(conn, biz, plan["id"])
    headlines, descriptions, dropped = _ad_copy(plan, ads_refusal.dropped(conn, biz, settings=settings))
    page = landing.current(conn, biz)
    monthly = float(ch["monthly_usd"])
    agreed = {
        "monthly_usd": monthly,
        # R21: the build's own figure (floored to the cent, as Google takes it), not a separate rounding of the
        # same ratio -- what `differs` and the build's own summary.daily_usd (adapter.py's build()) agree on
        "daily_usd": google_ads.from_micros(google_build.daily_micros(monthly)),
        "service": ch.get("landing_service"),
        "page": page["url"] if page else None,
        "radius_miles": (plan["sections"]["consultation"].get("geo") or {}).get("radius_miles"),
    }
    configured = {
        "headlines": headlines,
        "descriptions": descriptions,
        "dropped": dropped,
        "removed": [c["what"] for c in removed],
        "daily_usd": built.summary["daily_usd"] if built else None,
        "area": built.summary["geo_text"] if built else None,
        "page": built.page_url if built else agreed["page"],
        "sent_to_google": built is not None,
    }
    return {
        "business": (business.latest_knowledge(conn, biz) or (None, {}))[1],
        "agreed": agreed,
        "configured": configured,
    }


def differs(material: dict) -> str | None:
    """The configured ads against the agreed plan, in code: the page, enough ad text, the daily budget (against
    the agreed figure, within a dollar); what differs, or None."""
    a, c = material["agreed"], material["configured"]
    if c["page"] != a["page"]:
        return "the page"
    if len(c["headlines"]) < plans.MIN_HEADLINES or len(c["descriptions"]) < plans.MIN_DESCRIPTIONS:
        return "the ad text"
    if c["daily_usd"] is not None and abs(float(c["daily_usd"]) - a["daily_usd"]) > 1:
        return "the daily budget"
    return None


def _model_row(conn: psycopg.Connection, review: dict, result: dict) -> None:
    """The last look's audit row (job.reviewer: counted toward the roles' own day, as the Critic's job.critic is)."""
    audit.record(
        conn,
        tool=TOOL,
        args={"plan_id": review["plan_id"], "review_id": review["id"]},
        result=result,
        ok=True,
        business_id=review["business_id"],
        actor="job:reviewer",
        actor_class="job",
        api_key_id=None,
        ip=None,
    )


def _refuse_inside_transaction(conn: psycopg.Connection, who: str) -> None:
    """R22: the pass must commit before its launch, so an outer transaction would hold the launch back behind a
    lock (or undo it on a later rollback) -- `approve` refuses to run inside one, and so does anything that leads
    straight to it without a chance to commit in between (`run_now`: Task 12's MCP body runs in its own
    transaction, and `approve`'s RuntimeError, caught by `run_job`'s own `except Exception`, would turn a review
    that should have passed into a false UNEXPECTED hold)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(f"the pass commits before its launch: call reviewer.{who} outside a transaction")


def approve(
    conn: psycopg.Connection, settings: config.Settings, review: dict, *, by: str, ip: str | None = None
) -> dict:
    """The pass -- the Reviewer's, or an admin's (`by` admin:<email>, from /ops/review or the admin MCP): in one
    transaction the review `passed`, the plan approved as every approval approves one (proposals.apply_approve_plan:
    the version before it superseded, its rows queued) and its audit row (job.review_pass, or admin.review_approve;
    approval kind "review") -- then its ads launched (ads_run.launch_now, which never raises) and, after the launch,
    the owner told once a version of anything Admiry dropped along the way (`_tell_changed`, ruling G5, task review
    fix round 1; ruling L1, final review: never raises, so nothing it reads or writes can hold back the launch or
    turn the pass into an error). The approval's own check may refuse it now (the page broke since): the review is
    held with that sentence and nothing is approved. ToolError launch_review.DECIDED when it was decided meanwhile.
    RuntimeError (R22) when called inside an open transaction: the pass must commit before its launch -- callers run
    it outside one (`_refuse_inside_transaction`)."""
    _refuse_inside_transaction(conn, "approve")
    from .. import proposals  # the approval imports what imports this module's callers

    biz = review["business_id"]
    reviewer = by == "reviewer"
    try:
        with conn.transaction():
            launch_review.decide(conn, review["id"], state="passed", by=by)
            out = proposals.apply_approve_plan(conn, biz, {"plan_id": review["plan_id"]})
            audit.record(
                conn,
                tool=PASS_TOOL if reviewer else "admin.review_approve",
                args={"review_id": review["id"], "plan_id": review["plan_id"]},
                result={"plan_status": out["plan_status"], "execution": out["execution"]},
                ok=True,
                business_id=biz,
                actor="job:reviewer" if reviewer else by,
                actor_class="job" if reviewer else "platform",
                api_key_id=None,
                ip=ip,
                approval_kind="review",
                approval_ref=review["id"],
            )
    except ToolError as e:
        if str(e) == launch_review.DECIDED:
            raise
        return hold(conn, settings, review, APPROVAL_REFUSED.format(sentence=e))
    launched = ads_run.launch_now(biz)
    _tell_changed(conn, settings, review)
    return {"passed": True, "launch": launched}


def hold(conn: psycopg.Connection, settings: config.Settings, review: dict, reason: str) -> dict:
    """The review waits for a person (launch_review.hold): the operator is told once, with the admin page's link, and
    the owner once (HELD_SAY's e-mail). Never raises for a mail."""
    if launch_review.hold(conn, review["id"], reason):
        _tell_held(conn, settings, review, reason)
    return {"held": reason}


def _tell_held(conn: psycopg.Connection, settings: config.Settings, review: dict, reason: str) -> None:
    """The operator told once, and the owner once (HELD_SAY's e-mail) -- each its own try, so one failing never skips
    the other. Never raises: the name read sits inside the operator notice's own try, so a database error reading it
    (after the hold already committed) only costs that notice, never escapes to the caller or skips the owner's."""
    biz, key = review["business_id"], f"review-hold-{review['id']}"
    try:
        name = plans.name_for((business.latest_knowledge(conn, biz) or (None, {}))[1])
        label = f"{name} ({biz})" if name else biz  # no name: say the id once, never "biz_x (biz_x)"
        mail_out.send_plain(
            settings,
            to=settings.ops_email or "",
            subject=f"[Admiry] A launch waits for review: {label}",
            paragraphs=[
                f"Admiry's Reviewer held the first launch of {label}, plan {review['plan_id']}.",
                f"Why: {reason.rstrip('.')}.",
                "Approve it, return it to the owner with a note, or reject it, on the admin page or with the admin MCP.",
            ],
            links=[("Open the review", f"{settings.web_base_url}/ops/review/{review['id']}")],
            idempotency_key=key,
            gate=None if settings.ops_email else "AGENT_OPS_EMAIL unset",
            message_id=key,
        )
    except Exception as e:  # noqa: BLE001 - the hold is recorded; the name read failing here never skips the owner's
        log.warning(
            "reviewer: the hold of review %s was not e-mailed to the operator: %s", review["id"], type(e).__name__
        )
    try:
        mail.to_owner(conn, biz, "mail_review_hold.txt.j2", HOLD_SUBJECT, link=f"{settings.web_base_url}/?setup={biz}")
    except Exception as e:  # noqa: BLE001 - the hold is recorded; the owner's mail failing never skips the operator's
        log.warning("reviewer: the hold of review %s was not e-mailed to the owner: %s", review["id"], type(e).__name__)


def hold_stranded(conn: psycopg.Connection, settings: config.Settings) -> int:
    """Reviews the Reviewer took and never finished -- no job waits or runs for them any more (a run that died, a job
    that failed) -- held for a person (R19), so no paid review waits unseen. How many."""
    found = conn.execute(
        f"SELECT {launch_review.COLS} FROM launch_review r WHERE r.state = ANY(%s) AND NOT EXISTS (SELECT 1 FROM "
        "agent_job j WHERE j.role = %s AND j.kind = %s AND j.status IN ('queued', 'running') "
        "AND j.brief->>'review_id' = r.id)",
        (list(launch_review.RUNNING), ROLE, KIND),
    ).fetchall()
    for row in found:
        hold(conn, settings, dict(row), STRANDED)
    return len(found)


def tell_back(conn: psycopg.Connection, settings: config.Settings, review: dict, *, state: str, note: str) -> None:
    """The owner e-mailed the note, with the home's link -- the plan already back with them (launch_review.decide
    already committed: `back_to_owner`'s own call, or an admin's, ops_review.send_back, whose audit row must commit
    with the decision before the owner hears anything, R25). Never raises: a mail that cannot go out is only
    logged."""
    template = "mail_review_return.txt.j2" if state == "returned" else "mail_review_reject.txt.j2"
    try:
        mail.to_owner(
            conn,
            review["business_id"],
            template,
            RETURN_SUBJECT if state == "returned" else "Admiry cannot run these ads",
            note=note,
            link=f"{settings.web_base_url}/?setup={review['business_id']}",
        )
    except Exception as e:  # noqa: BLE001 - the decision is recorded; a mail that cannot go out is only logged
        log.warning("reviewer: review %s's note was not e-mailed: %s", review["id"], type(e).__name__)


def back_to_owner(
    conn: psycopg.Connection,
    settings: config.Settings,
    review: dict,
    *,
    state: str,
    by: str,
    note: str,
    resolution: str | None = None,
) -> dict:
    """The plan goes back to the owner (launch_review.decide: returned or rejected, the plan a draft, `note` first on
    its list, `resolution` the fix the list offers for it -- spec 2026-10-11 §5), and the owner e-mailed the note
    (`tell_back`). ToolError DECIDED when decided already."""
    launch_review.decide(conn, review["id"], state=state, by=by, note=note, resolution=resolution)
    tell_back(conn, settings, review, state=state, note=note)
    return {state: note}


def run_now(conn: psycopg.Connection, settings: config.Settings, review_id: str) -> dict:
    """An admin's "run it now" (the admin MCP's review_run): the review's waiting job, taken at once as research-run
    would take it. ToolError NOTHING_WAITS when no job waits for it. RuntimeError (R22) when called inside an open
    transaction, refused up front, before the job is even claimed: run_now runs outside one, same as `approve`
    itself (`_refuse_inside_transaction`)."""
    _refuse_inside_transaction(conn, "run_now")
    from . import runner

    job = conn.execute(
        "UPDATE agent_job SET status = 'running', rounds = rounds + 1, started_at = now(), updated_at = now() "
        "WHERE id = (SELECT id FROM agent_job WHERE status = 'queued' AND role = %s AND kind = %s "
        "AND brief->>'review_id' = %s LIMIT 1 FOR UPDATE SKIP LOCKED) "
        "RETURNING id, business_id, role, kind, brief, rounds, created_at",
        (ROLE, KIND, review_id),
    ).fetchone()
    if job is None:
        raise ToolError(NOTHING_WAITS)
    out = {"done": 0, "skipped": 0, "failed": 0, "retried": 0}
    runner._handle(conn, settings, dict(job), out)
    return {"review": (launch_review.get(conn, review_id) or {}).get("state"), **out}
