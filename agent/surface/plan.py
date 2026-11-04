"""The conversation and the plan (spec 2026-10-01 §4.2-§4.4; plan G1 Task 10). Admiry leads: these tools carry the
owner's words in and Admiry's words out. Each call runs exactly one command."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import WrapValidator

from .. import plans, proposals
from ..business import GOAL_TYPES
from ..mcp_app import PLAN_VIEW
from ..tools_business import GOAL_CHANGES
from ..tools_consult import ADJUST_FIELDS
from .registry import data_of, dispatch, earlier_confirmation, public_tool, replayed
from .supporting import _no_placeholders
from .where import Where, where

# What `changes` may name: the plan's own fields (the command `adjust`'s ADJUST_FIELDS), or the goal's (`change_goal`'s
# GOAL_CHANGES); each the one list of them
PLAN_CHANGES = ADJUST_FIELDS
OUTCOMES = {"calls": "calls", "forms": "enquiries", "bookings": "bookings"}  # preferred_outcome -> the objective
ONE_OF = "send exactly one of answers, message or changes"
SEE_THE_PLAN_FIRST = "the questions are answered: show the plan first, then say what to change"
SEE_THE_PLAN_THEN_LAUNCH = "the questions are answered: show the plan first, then launch it"  # a launch asked too soon
# What a tool says itself to a caller it knows when the thing the call needs is missing: no command runs, no row is
# written. A caller where() cannot resolve is refused by the command instead, in the command's words.
ALREADY_ANSWERED = "the questions are already answered: send what should change as message or changes"
NO_CONSULTATION = "there is no consultation yet: begin a plan first"
NO_PLAN = "there is no plan yet: begin a plan first"
QUESTIONS_FIRST = "there is no plan yet: the consultation's questions come first"
NO_LAUNCH_REQUEST = (
    "nothing is waiting for an approval code on this plan: ask to launch it first, then send the code the owner reads "
    "on their approval page"
)
ALREADY_APPROVED = "this plan is already approved: nothing is left to confirm, and the code was not used"
CANNOT_BUILD = "the questions are answered and the plan is not built yet; building it needs the manage permission"
# No words of the owner's and no hand-off, said in the tool's own names (start's refusal names `text`, which the tool
# does not have; final review C, minor 1)
MESSAGE_REQUIRED = (
    "message is required: what the owner said about the business, in their own words (or pass handoff alone)"
)
CODE_AS_READ = 'approval_code must be the code as the owner read it, for example "48-311"'  # a boolean is no code


def _as_sent(value, handler):
    """A boolean is passed through FastMCP's check as it was sent, so the launch refuses it in its own words: read as a
    number (pydantic's lax reading of str | int), true would arrive as 1, a wrong code "00001" that counts toward the
    lock. Anything else is checked as the annotation says."""
    return value if isinstance(value, bool) else handler(value)


# The launch's approval_code: the code as the owner read it ("48-311"), or that code as a number (48311), which a host
# may send because the code is five digits (final review A m6: a number refused by FastMCP was logged, still valid)
ApprovalCode = Annotated[str | int | None, WrapValidator(_as_sent)]


def code_as_read(code: str | int | None) -> str | None:
    """The code as the owner read it: a number is its five digits, zero-padded (48311 is "48311"; 4831 is "04831", the
    code "04-831" with its leading zero lost on the way), which proposals.normalize_code reads as it reads "48-311". A
    boolean is no code: ToolError(CODE_AS_READ)."""
    if isinstance(code, bool):
        raise ToolError(CODE_AS_READ)
    if isinstance(code, int):
        return f"{code:05d}"
    return code


def _object(value, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolError(f"{name} must be an object")
    return value


def _before_the_plan(w: Where, *, answered: str) -> str:
    """What comes first when there is no plan yet, for a change or a launch: a plan begun when nothing is, the
    consultation's questions while it asks them, and once they are answered `answered` (show the plan, then say what to
    change, or then launch it)."""
    if w.session is None:
        return NO_PLAN
    return answered if w.ready_to_build else QUESTIONS_FIRST


def still_the_latest(w: Where) -> Callable[[dict], bool]:
    """Whether the first answer of a plan's turn, sent again, still stands (Task 18): an answer that made a plan
    version (its data has a plan_id and a version) while that version is still the business's latest plan; one that
    made none (an answer to a question, words that changed nothing, a goal change) always. A change whose version a
    later one replaced is not given again, and nothing is routed: its key was used, and the tool refuses it as a
    reused key (`replayed`), before a command that knows the call by its own arguments could give that answer."""
    latest = (w.plan or {}).get("id")

    def stands(answer: dict) -> bool:
        made = data_of(answer)
        if "plan_id" not in made or "version" not in made:
            return True
        return made["plan_id"] == latest

    return stands


def _approved_the_plan_asked(w: Where) -> Callable[[dict], bool]:
    """Whether a launch's first answer, sent again, approved the plan asked about now or sent it to Admiry's review
    (Task 18; L1 review): its data names the plan either way (confirm_proposal's `plan_id`, whichever effect ran --
    apply_approve_plan's or launch_review.submit's). A key that launched an earlier version is not answered with
    that launch once a later version is the approved (or in-review) one."""
    asked = (w.asked_plan or {}).get("id")

    def confirmed(answer: dict) -> bool:
        return asked is not None and data_of(answer).get("plan_id") == asked

    return confirmed


def asking_the_owner(answer: dict, w: Where) -> dict:
    """A read of the latest plan while get_marketing_state's own `next` for this key is the owner's choice (`w.asks`,
    tools_state.owner_question; the founder's ruling of 2026-10-13, owner-chooses review fix round 1, item 2) -- the
    page's three ways (or the page in hand's own step) while no page is settled (the page comes before the money),
    else how the ads run while no way is chosen, and that step comes first: not a consultation under way, the sign-in,
    a place already ready, nor a key that cannot run the step's tool. Its words end with that question, as
    get_marketing_state and connect_account ask it, and its `next` is the same choices side by side, none marked
    first. Any other read as it is: a read's `next` is empty, and get_marketing_state says what comes next."""
    latest = (w.plan or {}).get("id")
    if not (w.asks and latest and (w.asked_plan or {}).get("id") == latest):
        return answer
    question, nxt = w.asks
    return {**answer, "say": f"{answer['say']} {question}", "next": nxt}


def facts_from(
    *,
    goal: dict | None = None,
    service: dict | None = None,
    location: dict | None = None,
    budget: dict | None = None,
    preferred_outcome: str | None = None,
    known_facts: dict | None = None,
) -> dict:
    """The structured arguments as the consultation's known facts (spec §4.3). A fact that answers a question skips
    it; the goal's type, the target and the most-per-customer go onto the goal."""
    facts = dict(_object(known_facts, "known_facts"))
    goal, service = _object(goal, "goal"), _object(service, "service")
    location, budget = _object(location, "location"), _object(budget, "budget")
    goal_type = goal.get("type")
    if goal_type is not None and goal_type not in GOAL_TYPES:  # a tuple: a list or an object compares unequal
        raise ToolError("goal.type must be customers, calls or bookings")
    if preferred_outcome is not None and preferred_outcome not in OUTCOMES:
        raise ToolError("preferred_outcome must be calls, forms or bookings")
    pairs = (
        ("goal_type", goal_type),
        ("target_customers_per_month", goal.get("target")),
        ("max_cost_per_customer_usd", goal.get("max_cost_per_customer_usd")),
        ("service", service.get("name") or service.get("service_id")),
        ("address", location.get("address")),
        ("radius_miles", location.get("radius_miles")),
        ("monthly_budget_usd", budget.get("monthly_usd")),
        (
            "objective",
            OUTCOMES.get(preferred_outcome) or {"calls": "calls", "bookings": "bookings"}.get(goal_type),
        ),
    )
    facts.update({key: value for key, value in pairs if value is not None})
    return facts


# not destructive (final review A m8): every version is kept: a new start or a new reading replaces nothing for good,
# and the approved plan and the running ads are untouched
@public_tool(cls="W", scope="manage", commands=("start",), open_world=True, idempotent=False)  # a repeat starts over
def create_marketing_plan(
    message: str = "",
    website_url: str | None = None,
    description: str | None = None,
    goal: dict | None = None,
    service: dict | None = None,
    location: dict | None = None,
    budget: dict | None = None,
    preferred_outcome: str | None = None,
    known_facts: dict | None = None,
    handoff: str | None = None,
    locale: str = "en-US",
) -> dict:
    """Begin planning the business's advertising, or begin again from new words. `message` is what the owner said,
    as they said it; unless you pass `handoff` alone, the call needs some of the owner's words (message or
    description). Pass what you already know so Admiry does not ask for it: website_url (the full address of the
    business's public site, with https:// or http://: one without either, or that does not resolve, refuses the
    call); description (more of the owner's words about the business, read with message); goal ({type: customers |
    calls | bookings, target: how many a month, max_cost_per_customer_usd}); service ({name} or {service_id} from
    get_business_profile); location ({address, radius_miles}: the address, or the city and ZIP code, the ads centre
    on; Admiry places it on the map and skips its own question about it, or asks when it could not place it and has
    no map point for the business); budget ({monthly_usd}); preferred_outcome (calls, forms or bookings); known_facts
    (business_name,
    customer_value_usd, offer). Without a website_url, Admiry first asks whether the business has one. Admiry reads
    the site, then looks the business up on Google Maps by its name
    (business_name, else the name it read) and where it is -- without a name it looks nothing up -- says what it
    understood and asks its first question (or, when what you passed answers them all, offers to build the plan):
    show `say` as written and present next.question's options as choices, always with a way to answer in the owner's
    own words; when a question asks which Google Maps place is the business, Admiry links only the place the owner
    picks ("None of these" links nothing) and never offers one they said is not theirs; send the answer with
    update_marketing_plan. `handoff` is a session code the owner brings from Admiry's website
    ("continue session hs_…"): pass it alone and the consultation resumes where the website left it. For a business
    that already has a consultation or a plan, calling this starts over and reads the business again from the new
    words: do that only when the owner asks to start again. Nothing is launched here. USD, miles."""
    facts = facts_from(
        goal=goal,
        service=service,
        location=location,
        budget=budget,
        preferred_outcome=preferred_outcome,
        known_facts=known_facts,
    )
    text = "\n\n".join(part.strip() for part in (message, description or "") if part and part.strip())
    if not text and not handoff:
        raise ToolError(MESSAGE_REQUIRED)
    return dispatch(
        "start", text=text, website_url=website_url, locale=locale, known_facts=facts or None, handoff=handoff
    )


@public_tool(cls="W", scope="manage", commands=("answer", "change_plan", "adjust", "change_goal"), app=PLAN_VIEW)
def update_marketing_plan(
    idempotency_key: str,
    answers: dict | None = None,
    message: str | None = None,
    changes: dict | None = None,
    session_id: str | None = None,
    plan_id: str | None = None,
) -> dict:
    """One turn of the conversation about the plan. Send exactly one of: `answers`, when the owner picked options
    ({"<question id>": {"choice": "<option id>"}}, a list of up to three ids for a multi question, several questions
    at once are fine), and to correct an earlier answer before the plan is built (that question's id again, with
    {"choice": …} or, in the owner's own words, {"other": "<their words>"}: the new answer replaces the old one);
    `message`, the owner's own words exactly as typed -- before the plan is built they answer the question that is
    waiting (once every question is answered, show the plan with get_marketing_plan first), after it they say what
    to change ("make it $1,500 a month", "lead with the free consultation"), and words that ask for no change (a
    yes, a thank-you) change nothing and the answer says so -- never pick an option or confirm a value for them (an
    answer takes up to 500 characters and a change up to 1,000; longer words are refused, never cut); words that say
    the Google Maps listing Admiry linked is not the owner's business unlink it, as update_business_profile's
    google_listing null does; the question about where the ads centre takes an address, or a city and ZIP code, as
    message, and Admiry places it on the map; an owner who would rather say later says so, and gives the address
    later with update_business_profile;
    `changes`, when you already know the field: the plan's monthly_budget_usd, level (minimum, conservative,
    recommended, aggressive), radius_miles (1 to 100), offer, objective (bookings, calls, enquiries, awareness or
    any), channel (google_search or newsbreak) with channel_monthly_usd; or the goal's goal_type (customers, calls
    or bookings), target (how many a month), max_cost_per_customer_usd (the most a new customer may cost),
    monthly_limit_usd (the owner's monthly limit: above 0, and never below what the plan spends, so lower the plan's
    budget first), focus_services (a list of services' names), channel_caps ({"google_search": 500}: the most each
    place may get a month; send every cap the goal keeps, because the object replaces them all, and {} clears them),
    autopilot (off or safe). A goal change names only what changes and the rest of the goal stays; null removes a
    target, a most-per-customer or the monthly limit. The plan's fields and the goal's go in separate calls.
    `session_id` (which consultation `answers` are for) and `plan_id` (which plan a change is for) are optional: the
    open consultation and the latest plan when omitted. A message or changes sent with a plan_id is a change to that
    plan, also while a new consultation asks its questions. Show `say` as written, then present `next`. A change to
    the plan makes a new version (the old one is kept) that needs launching again with launch_marketing_plan, which
    this answer's `next` does not offer (it offers the ways the plan can run -- for Google Search the owner's choice,
    side by side, between Admiry running the ads from a prepaid balance and the business's own Google Ads account,
    or the funds for a place Admiry runs from the balance -- and another change, after the ways to put Admiry's code
    on the business's own website while it still needs it; while the business has no page for its ads yet, the page
    comes first: `say` ends with the page question and `next` is its three ways alone, or, with a page made and not
    published yet, the step that publishes it); a change to the goal leaves the plan as it is. Use a new idempotency_key
    per turn. USD, miles."""
    given = [
        name
        for name, value in (("answers", answers), ("message", message), ("changes", changes))
        if value not in (None, "", {})
    ]
    if len(given) != 1:
        raise ToolError(ONE_OF)
    w = where(scope="manage", plan_id=plan_id)
    # the same turn sent again: its first answer, while the plan version it made (if any) is still the latest; after
    # that its key is refused as a reused one, here, and nothing is routed
    earlier = replayed(idempotency_key, scope="manage", still_stands=still_the_latest(w))
    if earlier is not None:
        return earlier
    known = w.business_id is not None  # a caller where() cannot resolve gets the command's own refusal instead
    session = (w.session or {}).get("id")
    latest = plan_id or (w.plan or {}).get("id") or ""
    if given == ["answers"]:
        if known and session is None and not session_id:  # no consultation is open or ready
            raise ToolError(ALREADY_ANSWERED if w.plan is not None else NO_CONSULTATION)
        return dispatch(
            "answer", session_id=session_id or session or "", answers=answers, idempotency_key=idempotency_key
        )
    if given == ["message"]:
        # words that name a plan are a change to that plan, also while a new consultation asks its questions: the
        # question that waits takes the words, and the plan is shown first or begun, only when no plan is named
        if not plan_id:
            waiting = w.pending_question
            if waiting is not None:
                return dispatch(
                    "answer", session_id=session, answers={waiting: {"other": message}}, idempotency_key=idempotency_key
                )
            if w.session is not None:  # ready: every question answered, the plan not built yet
                raise ToolError(SEE_THE_PLAN_FIRST)
            if known and w.plan is None:  # no consultation waiting and no plan to change
                raise ToolError(NO_PLAN)
        return dispatch("change_plan", plan_id=latest, words=message, idempotency_key=idempotency_key)
    if not isinstance(changes, dict):
        raise ToolError("changes must be an object")
    _no_placeholders(changes)  # ruling H5 (plan D15): a `<…>` anywhere in changes, not only the ask's own radius
    unknown = sorted(set(changes) - set(PLAN_CHANGES) - set(GOAL_CHANGES))
    if unknown:
        raise ToolError(
            f"unknown changes: {unknown}; the plan takes {', '.join(PLAN_CHANGES)} "
            f"and the goal {', '.join(GOAL_CHANGES)}"
        )
    to_plan = {key: changes[key] for key in PLAN_CHANGES if key in changes}
    to_goal = {key: changes[key] for key in GOAL_CHANGES if key in changes}
    if to_plan and to_goal:
        raise ToolError("send the plan's changes and the goal's changes in separate calls")
    if to_goal:
        return dispatch("change_goal", changes=to_goal, idempotency_key=idempotency_key)
    # no plan to change: with nothing begun, whatever the call names (Task 10); while a consultation is under way, when
    # the call names no other plan (Task 18)
    if known and w.plan is None and (w.session is None or not plan_id):
        raise ToolError(_before_the_plan(w, answered=SEE_THE_PLAN_FIRST))
    return dispatch("adjust", plan_id=latest, idempotency_key=idempotency_key, **to_plan)


@public_tool(cls="W", scope="read", commands=("get_plan", "recommend"), app=PLAN_VIEW)
def get_marketing_plan(plan_id: str | None = None) -> dict:
    """The plan, built first when the questions are answered and it does not exist yet (that takes half a minute or
    more; building needs the manage scope). While a consultation still asks its questions, this shows the plan
    there was before, if any. It returns the cards: the goal, where the ads run with each place's amount and
    reason, the budget and its levels, what the budget should do in 30 days with every assumption and its basis,
    the reason for the main channel, and the ad as it will read -- for a plan with NewsBreak, with the picture
    NewsBreak shows (image, described by its alt) and the line about it (image_note): relay that line; approving
    the plan approves the picture. Show `say` and every card, the footnote included; never add a number of your
    own. To change it, update_marketing_plan; to run it, launch_marketing_plan. When this call builds the plan, the
    answer's `next` offers the ways it can run -- for Google Search the owner's choice, side by side, between Admiry
    running the ads from a prepaid balance and the business's own Google Ads account (ask the owner; never choose for
    them), or the funds for a place Admiry runs from the balance -- and a change, not the launch, after the ways to
    put Admiry's code on the business's own website while it still needs it; when no way can run yet, `say` says why
    (for a business that prepays, Google Search waits for Admiry-run accounts). The page comes before the money: while
    the business has no page for its ads yet, `say` ends with the page question and `next` is its three ways alone --
    Admiry builds it, the owner's assistant designs it and Admiry hosts it, or the website the owner has (ask the
    owner; never choose for them) -- or, with a page made and not published yet, the step that publishes it. A read's
    `next` is empty, except while the owner's choice -- the page, then how the ads run -- is the next step (as
    get_marketing_state says it): then `say` ends with that question and `next` is its choices. get_marketing_state says
    what comes next. `plan_id` reads one version: an earlier one, or the one get_campaigns names for a place (the
    version it runs or, for a place that has not started, the one approved for it)."""
    w = where(plan_id=plan_id, question=True)
    if plan_id:
        return asking_the_owner(dispatch("get_plan", plan_id=plan_id), w)
    if w.ready_to_build:
        if "manage" not in w.scopes:  # recommend would refuse it, and its refusal counts against the day's model calls
            raise ToolError(CANNOT_BUILD)
        sid = w.session["id"]
        return dispatch("recommend", session_id=sid, idempotency_key=f"build-{sid}")
    return asking_the_owner(dispatch("get_plan", plan_id=None), w)


@public_tool(cls="P", scope="manage", approval="code", commands=("approve_plan", "confirm_proposal"))
def launch_marketing_plan(idempotency_key: str, plan_id: str | None = None, approval_code: ApprovalCode = None) -> dict:
    """Launch the plan. It spends the owner's money, so it takes the owner's approval. The first call, without
    approval_code, asks: it returns approval_url (also as `next`), the owner's own page, where they read the plan
    and either approve it there or read you the approval code it shows. Asking again returns the same request, so
    a code the owner already read stays good. If they read you the code, send it in a second call exactly as they
    say it (for example "48-311"); never guess a code or reuse one. If they approved on the page, there is nothing
    more to send: a code sent then is answered "already approved". A business's first launch may go to Admiry's
    review once the owner says yes; a launch asked for or a code sent while it is in review is answered that it is
    in review, and there is nothing to send. Each place the ads run starts when it is ready
    (its account or its funds; NewsBreak also its picture and its page's Terms link and phone or e-mail), and a
    place whose account is not ready does not hold the others back: the answer says where each stands, and
    get_campaigns says it later. A launch that no place could start from is refused with what each place waits
    for, one sentence each, and the code is not used up; a place that waits only for its picture does not refuse
    it. get_marketing_state's setup says beforehand whether the steps are done and the launch's own check passes
    (ready_to_launch), and carries that check's refusal (blocked). If the business is not verified yet, the first
    call returns where the owner finishes that. IMPORTANT ACTION: only on the owner's own words. Use a new
    idempotency_key per call."""
    approval_code = code_as_read(approval_code)  # before anything else: a number is the code, a boolean is none
    w = where(scope="manage", plan_id=plan_id)
    known = w.business_id is not None  # a caller where() cannot resolve gets the command's own refusal instead
    if not approval_code:
        # asking: approve_plan's own replay answers a repeat (its arguments are the caller's or the latest plan's). With
        # nothing to launch and no plan named, the tool says what comes first -- not to a setup key: approve_plan's own
        # answer tells it to sign in (Task 18)
        if known and w.key_class != "draft" and w.plan is None and not plan_id:
            raise ToolError(_before_the_plan(w, answered=SEE_THE_PLAN_THEN_LAUNCH))
        return dispatch(
            "approve_plan", plan_id=plan_id or (w.plan or {}).get("id") or "", idempotency_key=idempotency_key
        )
    # the code: confirm the launch request that waits; with none waiting (never asked, expired, the plan changed
    # since, approved on the owner's page, sent to Admiry's review meanwhile, or this same call already confirmed
    # it), the first answer of this same call while the plan asked about still stands (still approved, or still in
    # review) and is the plan that answer confirmed; else, for a caller the tool knows: ALREADY_APPROVED when the
    # plan asked about is approved, proposals.IN_REVIEW_REFUSAL when it is in review (L1 review I1b), else
    # NO_LAUNCH_REQUEST. A setup key is never answered here: confirm_proposal refuses it in its own words (sign in,
    # finish setup)
    if w.key_class != "draft":
        status = (w.asked_plan or {}).get("status")
        stands = status in (plans.APPROVED, plans.IN_REVIEW)
        nothing = (
            ALREADY_APPROVED
            if status == plans.APPROVED
            else proposals.IN_REVIEW_REFUSAL
            if status == plans.IN_REVIEW
            else NO_LAUNCH_REQUEST
        )
        earlier = earlier_confirmation(
            w.launch_proposal,
            stands=stands,
            confirmed=_approved_the_plan_asked(w),
            known=known,
            nothing=nothing,
            idempotency_key=idempotency_key,
        )
        if earlier is not None:
            return earlier
    waiting = (w.launch_proposal or {}).get("proposal_id") or ""
    return dispatch(
        "confirm_proposal", proposal_id=waiting, idempotency_key=idempotency_key, approval_code=approval_code
    )


@public_tool(cls="RO", scope="read", commands=("ask",))
def ask_admiry(question: str) -> dict:
    """Admiry's own answer to the owner's question about the plan, its numbers or what Admiry will do ("why this
    budget?", "what happens if I spend less?", "why not Facebook?"). Send the question as the owner asked it (up to
    1,000 characters) and show `say` as written. Do not answer such questions from your own knowledge: your numbers
    will not match Admiry's. Admiry answers only from this business's own plan and facts and, when the plan cites
    them, from Admiry's reviewed notes on advertising this kind of business (published averages, never a promise),
    and says so when it cannot. This changes nothing; a change goes to update_marketing_plan."""
    return dispatch("ask", question=question)
