"""The reads the public surface is built on (spec 2026-10-01 §4.3, §5; plan G1 Tasks 4-7): business_profile, and,
added by the later tasks, performance, marketing_state and ask. Commands like every other: `run()` authenticates,
audits and answers in the envelope."""

from __future__ import annotations

from datetime import UTC

from fastmcp.exceptions import ToolError

from . import (
    ads_mode,
    ads_refusal,
    ads_run,
    auth,
    business,
    config,
    consult,
    envelope,
    funnel,
    keys,
    landing,
    launch_check,
    launch_review,
    leads,
    llm,
    plan_review,
    plans,
    profile,
    proposals,
    setup,
    state,
    tools_pages,
)
from .channels import registry
from .mcp_app import agent_tool, need_business, run
from .roles import analyst, strategist
from .surface import gate
from .util import iso

NOTHING_ON_FILE = "Admiry knows nothing about this business yet; begin by saying what it does and where"
# What get_marketing_state says to a business with nothing begun (founder, 2026-10-13): who is speaking, what it can do,
# and what to tell it, in the owner's words; {who} is the name a sentence may use (`_name_to_say`)
WELCOME = (
    "Welcome to Admiry — I'm your marketing assistant. I can build an ad plan for {who}, show you the plan and how "
    "your ads are doing. Tell me what the business does and what you want."
)
# The account step's label while the owner has chosen no way yet (the founder's ruling, 2026-10-13): it names neither
# way; the step carries the question and both choices (ads_mode.WAY_QUESTION, WAY_CHOICES)
WAY_STEP = "Choose how your ads run"
STATE_KEYS = (
    "surface_version",
    "caller",
    "business",
    "setup",
    "consultation",
    "goal",
    "plan",
    "channels",
    "funnel_month",
    "leads",
    "money",
    "issues",
    "pending",
    "changes",
    "opportunities",
)
# A proposal's kind -> the public tool that confirms it (spec §4.4: the proposing tool, called again)
PENDING_TOOL = {
    "approve_plan": "launch_marketing_plan",
    "publish_page": "update_landing_page",
    "unpublish_page": "update_landing_page",
}
# What a waiting change needs (`pending[].needs`): the code from the owner's page, or the owner's yes
NEEDS = ("code", "owner_yes")
NEEDS_CODE, NEEDS_OWNER_YES = NEEDS
NO_BUSINESS = "This key is not tied to a business."
BEGIN = envelope.actions(
    {"id": "begin", "label": "Plan my advertising", "tool": "create_marketing_plan", "primary": True}
)
SHOW_PLAN = envelope.actions(
    {"id": "show_plan", "label": "Build my plan", "tool": "get_marketing_plan", "primary": True}
)
LAUNCH = envelope.actions(
    {"id": "launch", "label": "Launch the plan", "tool": "launch_marketing_plan", "primary": True}
)
# A draft cannot launch (both launch tools refuse it): connect_account gives a draft its sign-in link
SIGN_IN = envelope.actions(
    {"id": "sign_in", "label": "Sign in to keep this plan", "tool": "connect_account", "primary": True}
)
# The steps left before a launch, offered in this order (`_launch_left`): the account, then the page
CONNECT = envelope.actions(
    {"id": "connect", "label": "Connect where the ads run", "tool": "connect_account", "primary": True}
)
PAGE = envelope.actions(
    {"id": "page", "label": "Give the ads their page", "tool": "update_landing_page", "primary": True}
)
OUTCOMES = envelope.actions(
    {"id": "record_outcomes", "label": "Say what the leads became", "tool": "get_leads", "primary": True}
)
# `setup.blocked` when the approval's own check lets the plan through and only the verification is left: asking to
# launch answers with the page where the owner finishes it (approve_plan's needs_verification)
NOT_VERIFIED = (
    "This plan needs the business verified first; asking to launch returns the page where the owner finishes that"
)
# What a line of the homepage's setup card may carry beside its words (setup._steps), carried as it is: what a place
# with nothing to connect waits for, the page's fix a place not built yet waits for, the plan's line about its picture
LINE_KEYS = ("reason", "page", "image")
# The used-up balance (spec §4.3: "no funds"): the business's own flag, `paused_reason` 'no_funds' on its row, which
# jobs.spend_guard sets -- the balance at zero, or (A3c-1, `_funds_stop`) the spend not taken from it yet using it up --
# and a top-up clears. The places Admiry runs from the balance are paused with it (`_funds_stop`), and ads that have
# not started wait for funds.
NO_FUNDS_ISSUE = (
    "Your Admiry balance is used up. Add funds: the ads Admiry runs from it are paused, and ads not started yet wait."
)
# Admiry's own pauses of a channel row that are issues (spec §4.3: "the page is down"), by the row's `paused_reason`:
# either of the page's reasons reads as the page being down; `no_funds` is a row the funds stop paused (A3c-1), said in
# its own row's sentence beside the business's flag (NO_FUNDS_ISSUE). Every other pause, the owner's included, stays in
# its channel row only.
PAUSE_ISSUES = {**dict.fromkeys(ads_run.PAGE_REASONS, "page_down"), "no_funds": "no_funds"}


def _name_to_say(knowledge: dict | None) -> str:
    """The name a sentence may lead with, in every command of this module: one a website or a Google listing vouches
    for (`plans.name_for`, the founder's walk of 2026-09-28: a name the model derived from the owner's own words reads
    as a label, not a place), when it is text (`profile.clean_name`); else "your business"."""
    return profile.clean_name(plans.name_for(knowledge)) or "your business"


def _consultation_facts(conn, biz: str) -> tuple[dict | None, dict]:
    """The latest consultation (open or ready first, else the planned one) and what it holds."""
    session = consult.handoff_for(conn, biz)
    if session is None:
        return None, {}
    return session, consult.facts(session, consult.load_tree(session["tree"]))


@agent_tool(cls="RO", scope="read")
def business_profile(include: list[str] | None = None) -> dict:
    """What Admiry knows about the business: who it is (business), where it works (locations: the address or the
    places it serves, how far, its hours and when the phone is answered), what it sells (services, each with an id
    and whether the plan leads with it), its offers, and the facts ads may use (facts). `include` limits the answer
    to some of business, locations, services, offers, facts."""

    def body(conn, p):
        biz = need_business(p)
        wanted = profile.SECTIONS if include is None else tuple(include)
        if not set(wanted) <= set(profile.SECTIONS):
            raise ToolError(f"include may name {', '.join(profile.SECTIONS)}")
        latest = business.latest_knowledge(conn, biz)
        if latest is None:
            raise ToolError(NOTHING_ON_FILE)
        session, facts = _consultation_facts(conn, biz)
        full = profile.view(
            business.row(conn, biz),
            latest[0],
            latest[1],
            goal=business.latest_goal(conn, biz),
            session=session,
            radius_miles=facts.get("radius_miles"),
            plan_offer=facts.get("offer"),
        )
        n = len(full["services"])
        say = f"Here is what I know about {_name_to_say(latest[1])}: {n} service{'s' if n != 1 else ''} on file."
        data = {key: full[key] for key in profile.SECTIONS if key in wanted}
        return envelope.make({**data, "knowledge_version": full["knowledge_version"]}, say=say)

    return run("business_profile", "read", {"include": include}, body)


@agent_tool(cls="RO", scope="read")
def performance(period: str = "30d") -> dict:
    """How the advertising is doing: what was spent, the visits the ads sent, the leads, how many were booked and
    won, what the owner said the won ones were worth, and the cost of a lead and of a booked one -- for the business
    (funnel), for each channel (by_channel) and for each service on file (by_service: leads that asked for something
    else count under Other, and leads that named no service under Not stated) -- beside the goal and the plan's own
    estimate for the same span. period is 7d, 30d, month or last_month; the two months carry each platform's
    statement, to be read to the owner exactly as written. Leads the owner reported count for the business and for
    no channel."""

    def body(conn, p):
        biz = need_business(p)
        latest = business.latest_knowledge(conn, biz)
        services = profile.services(latest[1] if latest else None)
        report = funnel.build(conn, biz, period, services=services)
        goal = business.latest_goal(conn, biz)
        progress = funnel.goal_progress(conn, biz, goal)
        report["goal"] = {"type": goal["goal_type"], "target": goal["target"], **progress} if progress else None
        if period in funnel.DAYS:
            # The page's events and leads are counted by UTC date and page_stats drops any date after its last row, so
            # the rows end on the later of the account's day and the UTC day: an account behind UTC (every US zone)
            # would otherwise lose its evening from the rows until its own day turned.
            today, _ = funnel.calendar(conn, biz)
            last = max(today, ads_run.now().astimezone(UTC).date())
            report["days"] = tools_pages.page_stats(conn, biz, funnel.DAYS[period], last)
        return envelope.make(report, say=funnel.say(report))

    return run("performance", "read", {"period": period}, body)


def _launch_refusal(conn, biz: str, plan: dict) -> str | None:
    """The approval's own check asked of `plan` now, as a read (`proposals.launch_refusal`, shared with the setup
    card's second look since S0): None when the check lets the plan through, else the sentence the launch would
    answer with."""
    return proposals.launch_refusal(conn, biz, plan["id"])


def _setup(conn, settings, biz: str, row: dict, plan: dict | None) -> dict:
    """What stands between the business and running ads, in order -- the page before the account, since the page comes
    before the money (the founder's ruling of 2026-10-13): each step done or not, and the public tool that moves it.
    The account step reads the homepage's own setup state: `channels` holds its lines as the "Ready to
    launch" card shows them (one or more per place the plan funds, whatever a place asks for, each with its own
    `done`; a place with nothing to connect that is not ready carries what it waits for, `reason`; a place not built
    yet whose own page rules the saved check does not pass carries the page's fix, `page`; a place that shows a picture
    with the ad carries the plan's line about it, `image`), and the step is done once one place the plan funds is
    ready, because a place that is not ready waits and never holds the others back (D77, `ads_run.precheck`). While no
    place is ready, the step's label is what the first open line waits for: its `reason` when it has one (a place with
    nothing to connect), else its words, `text` (the owner's own account to connect). The step agrees with the
    approval's own check (`proposals.precheck_approve_plan`): it reads the funded places whose execution switch is on,
    and all of them when none is. The page step is done while the page is published at an address and its
    saved check passed (`ads_run._page_ready`: a page taken down is no page) -- and, for a business whose ads are
    already built on a place that follows the page (`ads_run.page_rows`), once the page on file has an address: built
    ads need only that (`ads_run._launch_rules`). The approval step is the latest version's own: a changed plan that
    waits for approval leaves it undone, while the version approved before it runs.

    `ready_to_launch` is what the launch itself would answer: the four steps before the approval done, the latest
    plan a draft, the business verified, and the approval's own check letting that draft through now
    (`_launch_refusal`). `blocked` is the sentence that says why a launch asked for now would not go through: the
    check's own refusal of the latest draft, or -- the check passing -- NOT_VERIFIED for a business not verified yet;
    None with no plan, an approved latest plan, or nothing in the way.

    `checklist` is the latest draft's launch checklist as an assistant reads it (launch_check.public; None without a
    draft): each item's problem, suggestion, whether it blocks, and the one public tool with the arguments that fix
    it.

    While the owner has chosen no way for a draft that funds Google Search (ads_mode.asks; the founder's ruling of
    2026-10-13), the undone account step is their choice: its label (WAY_STEP) names neither way, and it carries the
    question and both choices (`ask_owner`, `choices`), as the checklist's decide items carry theirs. The
    words that would pick one way say the neutral line instead (ads_mode.WAY_LINE; owner-chooses review, fix round 1,
    item 4): Google Search's line asking for the owner's Customer ID, while no way is chosen, and `blocked`'s sentence
    for a link not made, while the step is their choice (the account step leads then, so no ask is read off it).

    The page comes before the money (the same day's ruling): while the latest draft has no page settled -- the
    checklist's page item is on its list (launch_check.page_first) -- the page step is not done (page-first review,
    item 5: not even where ads built on one place count the address on file) and carries the page question and its
    ways (`ask_owner`, `choices`: launch_check.page_ask) -- or, once a page is made and waits to be published, the one
    step that publishes it (launch_check.page_in_hand; final review, item 1) -- and the account step carries its own
    question only once the page is settled. The checklist's page item reads what the step asks (`page`)."""
    draft = row["claim_state"] == "draft"
    approved = plan is not None and plan["status"] == "approved"
    account_steps = setup.state(conn, settings, biz)["steps"] if plan is not None else []
    checked = [s for s in account_steps if registry.exec_mode(settings, s["channel"]) != "off"] or account_steps
    account_done = any(s["done"] for s in checked)  # every line of one place shares that place's `done`
    waiting = next((s.get("reason") or s["text"] for s in checked if not s["done"]), None)
    # the founder's ruling (2026-10-13): while the owner has chosen no way, the step is their choice -- never one way
    unchosen = plan is not None and ads_mode.asks(conn, biz, plan=plan)
    asking = unchosen and not account_done
    label = WAY_STEP if asking else "Connect where the ads run" if account_done or waiting is None else waiting
    # the page comes before the money (the founder's ruling, 2026-10-13): the checklist's own page item, for the draft
    page_first = launch_check.page_first(conn, biz, plan)
    page = landing.current(conn, biz)
    report = landing.judged_report(conn, page)  # as the launch judges it (ads_run._page_ready)
    if ads_run.page_rows(conn, biz):  # ads built on a place that follows the page: a change needs only its address
        page_done = bool(page and page.get("url"))
    else:
        page_done = bool(page and page.get("status") == "published" and page.get("url") and report and report.get("ok"))
    # never done while the page comes first (page-first review, item 5): ads built on one place take its address on
    # file, while another place the draft funds still has no page -- the state would lead past the page to the account
    # while connect_account asks for the page
    page_done = page_done and not page_first
    # what the page step asks (launch_check.page_ask): the page question and its ways, or -- a page in hand -- the step
    # that publishes it (final review, item 1); read once, the checklist's page item reads the same (item 5)
    page_ask = launch_check.page_ask(conn, biz) if page_first else None
    steps = [
        {"id": "sign_in", "label": "Sign in", "done": not draft, "tool": None},
        {"id": "plan", "label": "Build the plan", "done": plan is not None, "tool": "get_marketing_plan"},
        {
            "id": "page",
            "label": "A page for the ads that passes its check",
            "done": page_done,
            "tool": "update_landing_page",
        }
        # no page yet: the page question and its ways, as the checklist's page item asks them
        | ({"ask_owner": page_ask[0], "choices": page_ask[1]} if page_ask is not None else {}),
        {
            "id": "account",
            "label": label,
            "done": account_done,
            "tool": "connect_account",
            "channels": [  # no `url`: the tool gives the link
                {
                    "channel": s["channel"],
                    "label": s["label"],
                    "text": ads_mode.neutral(s["text"]) if unchosen else s["text"],
                    "done": s["done"],
                }
                | {key: s[key] for key in LINE_KEYS if key in s}
                for s in account_steps
            ],
        }
        # as the checklist's decide items carry theirs (launch_check.fix_for): the question and both choices -- once
        # the page is settled
        | ({"ask_owner": ads_mode.WAY_QUESTION, "choices": ads_mode.choices()} if asking and not page_first else {}),
        {"id": "approval", "label": "Approve the plan", "done": approved, "tool": "launch_marketing_plan"},
    ]
    to_launch = plan is not None and plan["status"] == "draft"
    blocked = _launch_refusal(conn, biz, plan) if to_launch else None
    if blocked is None and to_launch and row["claim_state"] != "verified":
        blocked = NOT_VERIFIED
    if asking and blocked is not None:  # the link's sentence, alone or among several places' (each after its name)
        blocked = ads_mode.neutral(blocked)
    out = {
        "ready_to_launch": to_launch and blocked is None and all(s["done"] for s in steps[:4]),
        "blocked": blocked,
        "steps": steps,
        # spec 2026-10-11 §3: the whole list for the latest draft, each item with its fix; None without a draft
        "checklist": launch_check.public(conn, settings, biz, page=page_ask),
    }
    if draft:
        out["sign_in_url"] = f"{settings.web_base_url}/connect"
    return out


def _consultation(session: dict | None) -> dict | None:
    if session is None:
        return None
    todo = consult.pending(session) if session["status"] == "open" else []
    return {
        "session_id": session["id"],
        "status": session["status"],
        "phase": "intake" if session["status"] in ("open", "ready") else "proposal",
        "pending_question": todo[0]["id"] if todo else None,
    }


def _plan(plan: dict | None) -> dict | None:
    if plan is None:
        return None
    return {
        "id": plan["id"],
        "version": plan["version"],
        "status": plan["status"],
        "monthly_usd": plans.total_usd(plan["sections"]),
        "split": [
            {"channel": c["channel"], "label": registry.label(c["channel"]), "monthly_usd": float(c["monthly_usd"])}
            for c in plans.funded(plan["sections"])
        ],
        "plan_url": plans.plan_url(plan["id"]),
    }


def issue_text(label: str, issue: dict) -> str:
    """A channel row's issue as the owner reads it: the platform's own words when it gave some; an ad the platform did
    not approve, which has none, as one sentence -- the ad that needs a change, its headline, and the platform's policy
    topics, each by its own name as the platform wrote it. Public: the "Your ads" page's cards say the same (YA-1
    final review I2)."""
    if "platform_text" in issue:
        return issue["platform_text"]
    text = f"{label} needs a change to one of your ads"
    if issue.get("headline"):
        text += f' ("{issue["headline"]}")'
    topics = [t["topic"] for t in issue.get("topics") or [] if t.get("topic")]
    if topics:
        text += ": " + ", ".join(topics)
    return text + "."


def _platform_issues(st: state.MarketingState) -> list[dict]:
    """What the platforms say: one issue for each of a channel row's own `issues` (its open `channel_issue` rows, the
    reasons a sync read, the ads a platform did not approve), in row order; then each open `channel_issue` of a channel
    that has no row (only `st.issues` holds it). No issue is listed twice."""
    with_rows = {r.channel for r in st.channels}
    on_rows = [
        {"kind": i["kind"], "channel": r.channel, "label": r.label, "text": issue_text(r.label, i)}
        for r in st.channels
        for i in r.issues
    ]
    return on_rows + [
        {"kind": i["kind"], "channel": i["channel"], "label": i["label"], "text": i["platform_text"]}
        for i in st.issues
        if i["channel"] not in with_rows
    ]


def _paused_by_admiry(conn, biz: str, rows: list[state.ChannelRow]) -> list[dict]:
    """Admiry's own issues for its pauses (`PAUSE_ISSUES`): one per channel row it paused for the page or the balance,
    in that row's own sentence (`rows`, the state's channel rows); `paused_reason` is read off the raw rows. A raw row
    is said only with the sentence of a row that reads paused itself (A8): the raw rows are read after the state's, and
    a pause that committed between the two reads is the next read's to say."""
    said = {r.channel: r.sentence for r in rows if r.state == "paused"}
    return [
        {
            "kind": PAUSE_ISSUES[r["paused_reason"]],
            "channel": r["channel"],
            "label": registry.label(r["channel"]),
            "text": said[r["channel"]],
        }
        for r in ads_run.rows(conn, biz)
        if r["state"] == "paused" and r["paused_reason"] in PAUSE_ISSUES and r["channel"] in said
    ]


def _cannot_launch(blocked: str) -> str:
    """Why a launch asked for now would not go through (`setup.blocked`), as one sentence: the problem's own end mark
    kept ("?", "!"), a "." added only when it has none (as launch_check.note_text keeps one -- a blind `rstrip('.')
    + '.'` turns "Why?" into "Why?.", task review H4)."""
    said = blocked.strip()
    if not said.endswith((".", "!", "?")):
        said += "."
    return f"It cannot launch yet: {said}"


# S0 (the Google launch trace, 2026-10-07): a refusal used to leave `next` NONE, the refusal in `say` alone. Now the
# assistant is asked for what the launch needs (plan_review.ask_for): a question the owner answers in words, carrying
# the tool the answer goes to, or the action that fixes it.
ASK_LABELS = {
    "budget": "Change the budget",
    "page": "Give the ads their page",
    "funds": "Add funds",
    "plan": "Change the plan",
    "account": "Connect where the ads run",
}


def _ask_next(blocked: str) -> dict:
    """`next` for a launch refusal: its ask as a question (an input the owner types into) or an action; NONE for a
    refusal no ask knows."""
    ask = plan_review.ask_for(blocked)
    if ask is None:
        return envelope.NONE
    if ask.get("input"):
        return envelope.question(
            id=ask["id"],
            index=1,
            count=1,
            prompt=blocked,
            input=ask["input"],
            options=[],
            placeholder=plan_review.PLACEHOLDERS[ask["id"]],
            tool=ask["tool"],
        )
    return envelope.actions({"id": ask["id"], "label": ASK_LABELS[ask["id"]], "tool": ask["tool"], "primary": True})


# an ask item's id (its bare resolution, L1 R10) -> the question's input (plan_review's asks: a phone is typed, an
# address placed, the miles a number)
ASK_INPUTS = {"phone": "text", "location": "address", "radius": "radius"}


def _first_blocking(setup_view: dict) -> dict | None:
    """The checklist's first blocking item (spec 2026-10-11 §3), or None -- no draft, or nothing blocks."""
    return next((i for i in (setup_view.get("checklist") or {}).get("items") or [] if i["blocking"]), None)


def _fix_next(item: dict) -> dict:
    """`next` for a checklist item (plan D8): an ask as a question (its `ask_owner` the prompt, the tool on it), a
    decide's choices as actions, a fix as one action with its arguments; NONE for an item no tool fixes. One return
    point for the actions (fix["tool"] read once, the single-fix case its own one-choice list), so a dict literal
    naming the tool from `fix` appears once (test_surface_contract's static scan)."""
    fix = item["fix"]
    if fix is None:
        return envelope.NONE
    if fix["ask_owner"] and item["id"] in ASK_INPUTS:
        return envelope.question(
            id=item["id"],
            index=1,
            count=1,
            prompt=fix["ask_owner"],
            input=ASK_INPUTS[item["id"]],
            options=[],
            placeholder=plan_review.PLACEHOLDERS[item["id"]],
            tool=fix["tool"],
        )
    choices = fix.get("choices") or [{"label": fix["label"], "args": fix["args"]}]
    # `primary` on the first: a budget's choices, or a fix's one action. Never the page's ways, which are none marked
    # first: the page item is on the list only while the page comes first (launch_check.page_first), and then the
    # state's page step carries the same choices and leads (`_launch_left` returns at it before reading the list)
    return envelope.actions(
        *[
            {"id": item["id"], "label": c["label"], "tool": fix["tool"], "args": c["args"], "primary": i == 0}
            for i, c in enumerate(choices)
        ]
    )


def _asked(step: dict) -> dict:
    """`next` for a step that carries the owner's choice (`_setup`): the page's three ways, or the two ways the ads run
    -- side by side, none marked first (the founder's ruling, 2026-10-13)."""
    if step["id"] == "page":
        return envelope.actions(*launch_check.page_actions(step["choices"]))
    return envelope.actions(*ads_mode.way_actions())


def _launch_left(who: str, amount: str, setup_view: dict, *, draft: bool) -> tuple[str, dict]:
    """The sentence and the step for a plan version that waits for its launch, from what is left (`_setup`), the first
    thing first: a draft signs in (it cannot launch: both launch tools refuse it); then, while no page is settled, the
    page, by its question with its three ways side by side, none marked first -- the page comes before the money (the
    founder's ruling, 2026-10-13); then the account, then the page, each said by its own step's label -- the account,
    while the owner has chosen no way, by its question with both ways side by side, neither marked first (the same
    ruling); then the checklist's first blocking item, with its fix, before the approval's own refusal, which stays for
    what the list leaves to the steps -- the money, the account -- with the launch still offered when only the
    verification is left, since asking to launch returns the page where the owner finishes it; else the launch."""
    head = f"The plan for {who} is ready: {amount} a month."
    if draft:
        return f"{head} Sign in to keep it and launch it.", SIGN_IN
    steps = {s["id"]: s for s in setup_view["steps"]}
    for step, action in ((steps["page"], None), (steps["account"], CONNECT), (steps["page"], PAGE)):
        if not step["done"] and step.get("choices"):  # the owner's choice: the page's ways, or how the ads run
            return f"{head} {step['ask_owner']}", _asked(step)
        if not step["done"] and action is not None:
            return f"{head} It still needs: {step['label'].rstrip('.')}.", action
    first = _first_blocking(setup_view)  # spec 2026-10-11 §3: the list's first blocking item leads, with its fix
    blocked = setup_view["blocked"]
    if first is not None:  # say names the same problem next fixes (task review H4), never the approval's own text
        return f"{head} {_cannot_launch(first['problem'])}", _fix_next(first)
    if blocked is not None:
        return f"{head} {_cannot_launch(blocked)}", LAUNCH if blocked == NOT_VERIFIED else _ask_next(blocked)
    return f"The plan for {who} is ready and waits for your approval: {amount} a month.", LAUNCH


def _next_step(
    who: str,
    session,
    plan,
    approved: bool,
    rows,
    month: dict,
    waiting: int,
    *,
    draft: bool,
    setup_view: dict,
    review_words: str | None = None,
) -> tuple[str, dict]:
    """`say` and `next` from where the business stands, whatever the key may run (`_words` drops what it may not). An
    open consultation with nothing left to ask (its questions changed under it) reads as ready, as `answer` reads it. A
    plan in Admiry's review (L1) says so and offers nothing to call. A plan not approved yet offers the first thing left
    before its launch (`_launch_left`). `approved` is whether any version is: once one is, a newer draft (the owner
    changed the running plan) is said after where the ads stand, with why it cannot launch yet -- the checklist's
    first blocking item's problem, the same one its first thing left (`_launch_left`'s own `next`) fixes, or (when
    nothing on the list blocks) the approval's own refusal -- and its first thing left outranks recording outcomes."""
    if session is not None and session["status"] == "open" and (nxt := consult.next_question(session)) is not None:
        return f"A consultation for {who} is in progress. {consult.question_sentence(nxt)}", nxt
    if session is not None and session["status"] in ("open", "ready"):
        return "The questions are answered; the plan is ready to be built.", SHOW_PLAN
    if plan is None:
        return WELCOME.format(who=who), BEGIN
    amount = plans.usd(plans.total_usd(plan["sections"]))
    if plan["status"] == plans.IN_REVIEW:  # L1: a first launch in Admiry's review (spec 2026-10-10 §6.3)
        return review_words or launch_review.IN_REVIEW_SAY, envelope.NONE
    if not approved:
        return _launch_left(who, amount, setup_view, draft=draft)
    said = " ".join(r.sentence for r in rows) or "The plan is approved; nothing is running yet."
    said += f" This month so far: {plans.usd(month['spend_usd'])} spent, {month['leads']} lead"
    said += "." if month["leads"] == 1 else "s."
    if waiting:
        said += f" {waiting} lead{' waits' if waiting == 1 else 's wait'} for an outcome."
    if plan["status"] == "draft":
        said += (
            f" A changed version of the plan waits for your approval: {amount} a month."
            " Until then the approved one stays in place."
        )
        first = _first_blocking(setup_view)
        if first is not None:  # say names the same problem next fixes (task review H4), never the approval's own
            said += f" {_cannot_launch(first['problem'])}"
        elif setup_view["blocked"] is not None:
            said += f" {_cannot_launch(setup_view['blocked'])}"
        return said, _launch_left(who, amount, setup_view, draft=draft)[1]
    return said, OUTCOMES if waiting else envelope.NONE


def _offered(nxt: dict, scopes: tuple[str, ...]) -> dict:
    """`next` without an action whose public tool needs a scope the key lacks, as the surface's own registry gives each
    tool's scope (get_marketing_plan reads, so every key is offered it: building needs manage, and the tool says so
    itself); `envelope.NONE` once no action is left. A question is not filtered."""
    if nxt.get("kind") != "actions":
        return nxt
    # read here, not at import: the public tools register after every command module, this one included
    from .surface.registry import SURFACE

    needs = {m.name: m.scope for m in SURFACE}
    # a tool the registry does not list is held to read, which every key that reads the state has
    kept = [a for a in nxt["actions"] if needs.get(a["tool"], "read") in scopes]
    return {**nxt, "actions": kept} if kept else envelope.NONE


def _words(
    who: str,
    session,
    plan,
    approved: bool,
    rows,
    month: dict,
    waiting: int,
    *,
    draft: bool,
    setup_view: dict,
    scopes: tuple[str, ...],
    review_words: str | None = None,
) -> tuple[str, dict]:
    """`say` and `next`: the one most useful step from where the business stands (`_next_step`) that this key can run
    (`_offered`); `who` is the name a sentence may use (`_name_to_say`), `setup_view` the state's own `setup`
    (`_setup`), `review_words` the latest plan's own words while it is in Admiry's review (`launch_review.owner_words`).
    A draft -- a business nobody claimed yet, or a caller with a draft key -- is told to sign in where another would
    launch. The sentence is the same for every key: a step the key's scopes cannot run is not offered, and then `next`
    is none."""
    say, nxt = _next_step(
        who,
        session,
        plan,
        approved,
        rows,
        month,
        waiting,
        draft=draft,
        setup_view=setup_view,
        review_words=review_words,
    )
    return say, _offered(nxt, scopes)


def owner_question(conn, settings, biz: str, key: keys.KeyRecord) -> tuple[str, dict] | None:
    """(the question, `next`) when get_marketing_state's `next` for this business and `key` is the owner's choice --
    the page's three ways (or the page in hand's own step) while no page is settled, else how the ads run while no way
    is chosen (a step's `ask_owner` and `choices`, which `_launch_left` leads to; the founder's ruling of 2026-10-13) --
    read by the state's own steps; None otherwise. What a read of the plan asks with it (surface/plan.asking_the_owner;
    owner-chooses review, fix round 1, item 2), so the read never asks where the state leads elsewhere (a consultation
    under way, the plan still to build, the sign-in, a place already ready, a key that cannot run the step's tool). Only
    `next` is read: the figures only `say` reads (the ads' rows, the month, the leads waiting) are left empty, and the
    setup is read only while a step of the latest draft can ask (launch_check.page_first, ads_mode.asks)."""
    plan = plans.latest(conn, biz)
    if plan is None or plan["status"] != plans.DRAFT:
        return None
    if not launch_check.page_first(conn, biz, plan) and not ads_mode.asks(conn, biz, plan=plan):
        return None
    row = business.row(conn, biz)
    setup_view = _setup(conn, settings, biz, row, plan)
    _say, nxt = _words(
        "",
        consult.handoff_for(conn, biz),
        plan,
        ads_run.approved(conn, biz) is not None,
        [],
        {"spend_usd": 0, "leads": 0},
        0,
        draft=row["claim_state"] == "draft" or key.cls == "draft",
        setup_view=setup_view,
        scopes=key.scopes,
    )
    asking = [s for s in setup_view["steps"] if s.get("choices") and not s["done"]]
    return next(((s["ask_owner"], nxt) for s in asking if nxt == _asked(s)), None)


def _pending(conn, biz: str, plan: dict | None) -> list[dict]:
    """The changes waiting for the owner's yes, each with the public tool that confirms it: the page's publish and
    unpublish requests, and of the requests to launch only one the owner can still use -- the latest plan's, while it
    is a draft (`proposals.open_for_plan`: the request its plan page gives a code for, and the one
    launch_marketing_plan confirms); no code approves a version a later one replaced, nor one already approved."""
    # the one launch request `proposals.open_for_plan` returns for the latest plan: the open proposals' rows carry no
    # payload to read a plan from, and of two requests open for one plan only the newest can be confirmed; a request
    # left open beside the one that approved the plan has nothing left to launch
    draft = plan is not None and plan["status"] == "draft"
    usable = proposals.open_for_plan(conn, biz, plan["id"]) if draft else None
    return [
        {
            "kind": pr["kind"],
            "summary": pr["summary"],
            "needs": NEEDS_CODE if pr["needs_code"] else NEEDS_OWNER_YES,
            "tool": PENDING_TOOL.get(pr["kind"]),
            "approval_url": pr["approval_url"],
            "expires_at": pr["expires_at"],
        }
        for pr in proposals.list_open(conn, biz)
        if pr["kind"] != "approve_plan" or (usable is not None and pr["proposal_id"] == usable["proposal_id"])
    ]


@agent_tool(cls="RO", scope="read")
def marketing_state() -> dict:
    """Where the business stands, in one read: who is calling, the business, what is left before the ads can run
    (setup), the consultation in progress and its next question, the goal and how far along it is this month, the
    plan and its split, each channel's ads right now, this month's spend and leads, how many leads still wait for an
    outcome, what a platform is unhappy with (issues, in its own words), and the changes waiting for the owner's
    approval (pending). Call it first in every session; `next` is the one most useful step."""

    def body(conn, p):
        settings = config.load()
        caller = {
            "class": p.key.cls,
            "scopes": list(p.key.scopes),
            "label": p.key.label,
            "expires_at": iso(p.key.expires_at),
        }
        empty = dict.fromkeys(STATE_KEYS)
        base = {**empty, "surface_version": gate.SURFACE_VERSION, "caller": caller}
        biz = p.business_id
        if biz is None:
            return envelope.make(
                {**base, "channels": [], "issues": [], "pending": [], "changes": [], "opportunities": []},
                say=NO_BUSINESS,
            )
        row = business.row(conn, biz)
        knowledge = (business.latest_knowledge(conn, biz) or (None, {}))[1]
        name = profile.clean_name(knowledge.get("name"))
        st = state.build(conn, settings, biz)
        session = consult.handoff_for(conn, biz)
        plan = plans.latest(conn, biz)
        approved = ads_run.approved(conn, biz) is not None
        goal = business.latest_goal(conn, biz)
        month = funnel.month(conn, biz)
        waiting = leads.awaiting(conn, biz)
        # the platforms' own words first, then the empty balance, then Admiry's own pauses, then the leads that wait
        issues = _platform_issues(st)
        if row["paused_reason"] == "no_funds":  # the business's flag: jobs.spend_guard sets it, a top-up clears it
            issues.append({"kind": "no_funds", "channel": None, "label": None, "text": NO_FUNDS_ISSUE})
        issues += _paused_by_admiry(conn, biz, st.channels)
        if waiting:
            has = "has" if waiting == 1 else "have"
            issues.append(
                {
                    "kind": "leads_waiting",
                    "channel": None,
                    "label": None,
                    "text": f"{waiting} lead{'' if waiting == 1 else 's'} {has} no outcome yet",
                }
            )
        draft = row["claim_state"] == "draft" or p.key.cls == "draft"
        setup_view = _setup(conn, settings, biz, row, plan)
        review_words = launch_review.owner_words(conn, biz)  # L1: a first launch in Admiry's review (spec §6.3)
        # spec 2026-10-11 §5.1 (plan D9): what Admiry removed from the latest version's ads after Google refused it
        changes = ads_refusal.changes(conn, biz, plan["id"]) if plan is not None else []
        say, nxt = _words(
            _name_to_say(knowledge),
            session,
            plan,
            approved,
            st.channels,
            month,
            waiting,
            draft=draft,
            setup_view=setup_view,
            scopes=p.key.scopes,
            review_words=review_words,
        )
        if changes:
            say = f"{say} {ads_refusal.say(changes)}"
        data = {
            **base,
            "business": {
                "id": biz,
                "name": name,
                "trade": knowledge.get("trade"),
                "claim_state": row["claim_state"],
                "locale": row["locale"],
            },
            "setup": setup_view,
            "consultation": _consultation(session),
            "goal": (
                {
                    "id": goal["id"],
                    "type": goal["goal_type"],
                    "target": goal["target"],
                    "period": "month",
                    "max_cost_per_customer_usd": goal["max_cost_per_customer_usd"],
                    "monthly_limit_usd": goal["monthly_limit_usd"],
                    # whose limit: "owner" when the owner set it, "system" when Admiry set it as the plan's ceiling
                    "limit_source": goal["limit_source"],
                    "progress": funnel.goal_progress(conn, biz, goal),
                }
                if goal
                else None
            ),
            "plan": _plan(plan),
            "channels": [r.model_dump(mode="json") for r in st.channels],
            "funnel_month": month,
            "leads": {"awaiting_outcome": waiting},
            "money": st.money,
            "issues": issues,
            "pending": _pending(conn, biz, plan),
            "changes": changes,
            # the Analyst's suggestions from its latest look an owner may read (capability layer K2, plan R2)
            "opportunities": analyst.opportunities(conn, settings, biz),
        }
        return envelope.make(data, say=say, next=nxt)

    return run("marketing_state", "read", {}, body)


ASK_MAX = 1000  # characters of a question the answerer reads, whole: a longer one is refused, never cut
NOT_GROUNDED = (
    "I cannot answer that from what I have on this business. Ask it another way, or ask for a person and the Admiry "
    "team replies by e-mail."
)
# AGENT_SUPPORT off: contact_support would refuse, so the sentence offers no person and `next` no action
NOT_GROUNDED_SUPPORT_OFF = "I cannot answer that from what I have on this business. Ask it another way."
ASK_A_PERSON = envelope.actions(
    {"id": "contact_support", "label": "Ask a person", "tool": "contact_support", "primary": True}
)


@agent_tool(cls="RO", scope="read")
def ask(question: str) -> dict:
    """Admiry's own answer to a question about the plan, its numbers or what Admiry will do, in the owner's words
    ("why Google Search first?", "what happens if I spend less?"). Send the question as the owner asked it (up to
    1,000 characters) and show `say` as written: Admiry answers only from this business's plan and what it knows
    about the business and, when the plan cites them, from Admiry's reviewed notes on advertising this kind of
    business (published averages, never a promise), and says so when it cannot. A change or a new fact inside a
    question is not acted on; send those as a change to the plan."""

    def prepare(conn, p):
        biz = need_business(p)
        words = (question or "").strip()
        if not words:
            raise ToolError("ask a question in the owner's own words")
        if len(words) > ASK_MAX:
            raise ToolError(f"ask the question in at most {ASK_MAX:,} characters")
        on_file = business.latest_knowledge(conn, biz)
        latest = plans.latest(conn, biz)
        # nothing to answer from: refused before the day's model limit is looked at and the model is asked (final
        # review B, minor)
        if on_file is None and latest is None:
            raise ToolError(NOTHING_ON_FILE)
        auth.check_llm_cap(conn, p)
        knowledge = (on_file or (None, {}))[1]
        cards = rationale = ways = None
        if latest is not None and "consultation" in latest["sections"]:
            cards = plans.cards(
                {**latest, "business_name": plans.name_for(knowledge) or plans.address_for(knowledge)},
                final_host=landing.final_host(conn, biz),
            )
            rationale = latest["sections"].get("rationale")
            # how each place the plan funds runs, and the other way it could (walk fixes 1: asked how to switch to
            # Admiry running the ads, the answerer had nothing about it)
            ways = plans.ways(cards["plan"]["channels"])
        material = {"cards": cards, "rationale": rationale, "knowledge": knowledge, "ways": ways}
        told = strategist.for_answer(config.load(), rationale)  # capability layer K3: the files the plan cites
        if told:
            material["trade_knowledge"] = told
        try:
            return llm.generator().answer_question(words, material)
        except llm.LLMError as e:
            raise ToolError(str(e)) from e

    def body(conn, p, answer):
        if answer.grounded and answer.text.strip():
            return envelope.make({"grounded": True}, say=answer.text.strip(), next=envelope.NONE)
        if config.load().support:  # a person is offered only where contact_support takes the request
            return envelope.make({"grounded": False}, say=NOT_GROUNDED, next=ASK_A_PERSON)
        return envelope.make({"grounded": False}, say=NOT_GROUNDED_SUPPORT_OFF, next=envelope.NONE)

    return run("ask", "read", {"question": question}, body, prepare=prepare)
