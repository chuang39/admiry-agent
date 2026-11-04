"""Plan tools (spec §4.1): create_plan, get_plan, revise_plan, change_plan. approve_plan lives with the proposals."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import (
    ads_refusal,
    auth,
    business,
    config,
    consult,
    envelope,
    landing,
    listings,
    llm,
    plans,
    profile,
    site_install,
    support,
    support_mail,
    tools_consult,
)
from .mcp_app import PLAN_VIEW, agent_tool, need_business, run
from .roles import researcher, strategist

MAX_INSTRUCTION = 1000


def draft(
    conn, biz: str, goal_id: str | None, *, instruction: str | None = None, previous: dict | None = None
) -> tuple:
    """Knowledge + goal (goal_id, or the latest) → (sections, estimate_basis, notes, rationale, goal).
    Calls the LLM: prepare only."""
    latest = business.latest_knowledge(conn, biz)
    if latest is None:
        raise ToolError("describe the business first, then create a plan")
    knowledge = latest[1]
    goal = business.goal_by_id(conn, biz, goal_id) if goal_id else business.latest_goal(conn, biz)
    if goal is None:
        raise ToolError("set a goal first: a monthly budget alone is enough")
    try:
        d = llm.generator().draft_plan(knowledge, goal, instruction=instruction, previous=previous)
    except llm.LLMError as e:
        raise ToolError(str(e)) from e
    sections, notes = plans.normalize(d, goal["monthly_limit_usd"], autopilot=goal["autopilot"])
    basis = plans.add_estimate(conn, sections, knowledge.get("category", "fallback"))
    return sections, basis, notes, d.rationale, goal


def draft_consulted(conn, biz: str, prev: dict, instruction: str) -> tuple:
    """revise_plan on a plan that came from a consultation: the model's draft with the instruction, rebuilt by
    tools_consult.build at the previous level and spend, so the plan stays Google Search only and keeps its
    consultation, rationale, levels and two-column estimate. draft()'s tuple plus the session. Calls the LLM."""
    session = tools_consult.plan_session(conn, biz, prev["id"])
    latest = business.latest_knowledge(conn, biz)
    if latest is None:
        raise ToolError("describe the business first")
    knowledge = latest[1]
    goal = business.latest_goal(conn, biz)
    tree = consult.load_tree(session["tree"])
    spend = plans.total_usd(prev["sections"])  # the plan's own amount: a rewrite keeps it
    try:
        gen = llm.generator()
    except llm.LLMError as e:
        raise ToolError(str(e)) from e
    consultation = {**consult.facts(session, tree), "channel": "google_search", "monthly_budget_usd": spend}
    given = strategist.knowledge_for(
        config.load(),
        session["tree"],
        knowledge,
        consultation,
        consult.service_flags(session, tree),
        previous=prev["sections"],
    )
    research = researcher.for_strategist(conn, config.load(), biz)
    d = tools_consult.draft_with_copy(
        gen,
        knowledge,
        {**(goal or {}), "monthly_limit_usd": spend},
        biz=biz,
        instruction=instruction,
        previous=prev["sections"],
        consultation=consultation,
        given=given,
        research=research,
        avoid=ads_refusal.avoid_words(conn, config.load(), biz),
    )
    sections, basis, notes, goal = tools_consult.build(
        conn,
        biz,
        session,
        knowledge,
        level=prev["sections"]["consultation"]["level"],
        monthly_usd=spend,  # the whole plan's amount, every channel's
        draft=d,
        previous=None,
        named=plans.pins(prev["sections"]),  # the amounts the owner set per channel stay set
        research_id=research["id"] if research else None,
    )
    return sections, basis, notes, sections["rationale"]["why_primary"], goal, session


def write_revision(conn, biz: str, prev: dict, revised: tuple) -> dict:
    """The version a revision writes -- revise_plan's, and change_plan's rewrite -- from `prev` and draft()'s or
    draft_consulted()'s tuple (`revised`, its session last), with the consultation behind it, when there is one,
    marked as having produced it. Inside the caller's transaction; returns the new version."""
    sections, basis, notes, rationale, goal, session = revised
    plan = plans.create_version(
        conn,
        biz,
        goal_id=goal["id"] if goal else None,
        sections=sections,
        estimate_basis=basis,
        rationale=rationale,
        notes=notes,
        diff_from=prev["id"],
    )
    if session is not None:  # adjust finds the consultation through the plan it last produced
        consult.mark_planned(conn, session["id"], plan["id"])
    return plan


@agent_tool(cls="W", scope="manage")
def create_plan(idempotency_key: str, goal_id: str | None = None) -> dict:
    """Write the marketing plan for this business from its Business Knowledge and goal: the pages
    to create, local SEO/GEO fixes, which channels to run with how many dollars a month (never
    above the monthly limit), ad text for the owner to review, tracking to connect, and the
    estimated monthly leads with its basis. Read the lines back to the owner in order; details
    are on plan_url. Nothing runs until approve_plan is confirmed."""

    def prepare(conn, p):
        auth.check_llm_cap(conn, p)
        biz = need_business(p)
        ready = conn.execute(
            "SELECT id FROM consult_session WHERE business_id = %s AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
            (biz,),
        ).fetchone()
        if ready:
            raise ToolError(f"a consultation is ready (session {ready['id']}): build its plan instead")
        return draft(conn, biz, goal_id)

    def body(conn, p, ctx):
        sections, basis, notes, rationale, goal = ctx
        plan = plans.create_version(
            conn,
            p.business_id,
            goal_id=goal["id"],
            sections=sections,
            estimate_basis=basis,
            rationale=rationale,
            notes=notes,
        )
        return {**plans.summary(plan), "plan_url": plans.plan_url(plan["id"])}

    return run(
        "create_plan",
        "manage",
        {"goal_id": goal_id, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
    )


@agent_tool(cls="RO", scope="read", app=PLAN_VIEW)
def get_plan(plan_id: str | None = None) -> dict:
    """The current plan (or one by id): the owner-facing lines in order, the estimate with its
    basis, the channel budgets, the status (draft / in_review / approved / superseded) and plan_url
    where the owner reviews and approves it."""

    def body(conn, p):
        biz = need_business(p)
        plan = plans.get(conn, biz, plan_id) if plan_id else plans.latest(conn, biz)
        if plan is None:
            raise ToolError("no plan yet" if plan_id is None else "no plan with that id for this business")
        out = {
            **plans.summary(plan),
            "plan_url": plans.plan_url(plan["id"]),
            "estimate": plan["sections"]["estimate"],
            "ads": plan["sections"]["ads"],
        }
        if "consultation" in plan["sections"]:
            latest = business.latest_knowledge(conn, biz)
            plan["business_name"] = (plans.name_for(latest[1]) or plans.address_for(latest[1])) if latest else None
            # the plan's "Your website" row (A3p-5 batch 2 Q5): its sentence goes in `say` (envelope._plan_say)
            out["cards"] = plans.cards(
                plan, final_host=landing.final_host(conn, biz), website=site_install.row(conn, biz)
            )
        return out

    return run("get_plan", "read", {"plan_id": plan_id}, body)


@agent_tool(cls="W", scope="manage")
def revise_plan(plan_id: str, instruction: str, idempotency_key: str) -> dict:
    """Change the plan the way the owner asked, in their words: "no Meta", "focus on water
    heaters", "half the budget". Makes a new draft version (the old one is kept for comparison)
    within the current goal's monthly limit and returns its lines; approve_plan is needed again."""

    def prepare(conn, p):
        auth.check_llm_cap(conn, p)
        biz = need_business(p)
        prev = plans.get(conn, biz, plan_id)
        if prev is None:
            raise ToolError("no plan with that id for this business")
        if not instruction.strip():
            raise ToolError("say what to change, e.g. 'no Meta' or 'focus on water heaters'")
        words = instruction.strip()[:MAX_INSTRUCTION]
        if "consultation" in prev["sections"]:
            return prev, draft_consulted(conn, biz, prev, words)
        # the latest goal, not the one prev was drafted against: a lowered limit applies to the revision
        return prev, (*draft(conn, biz, None, instruction=words, previous=prev["sections"]), None)

    def body(conn, p, ctx):
        prev, revised = ctx
        plan = write_revision(conn, p.business_id, prev, revised)
        return {**plans.summary(plan), "plan_url": plans.plan_url(plan["id"])}

    return run(
        "revise_plan",
        "manage",
        {"plan_id": plan_id, "instruction": instruction, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
    )


# change_plan's answer when nothing changes: NOT_A_CHANGE for words that asked for a person or for help while Admiry's
# support is off; NO_CHANGE_ASKED for words that ask for no change at all -- a yes, a thank-you (re-review B)
NOT_A_CHANGE = (
    "That did not read as a change to the plan, so nothing changed. Tell me what to change: the budget, the area, "
    "the offer, or what the ads should lead with."
)
NO_CHANGE_ASKED = (
    "Nothing changed: the plan stands as it is. When you want something different, say what to change: the budget, "
    "the area, the offer, or what the ads should lead with."
)
# The owner signs in so the team's reply can reach them: contact_support's own action (tools_support.SIGN_IN_ACTIONS),
# written out here because importing a tool module would register its tools ahead of this one's (mcp_app sets the order)
SIGN_IN_FOR_THE_REPLY = envelope.actions(
    {"id": "sign_in", "label": "Sign in to get the reply", "tool": "connect_account", "primary": True}
)


def _to_the_team(conn, p, read, text: str) -> dict:
    """change_plan's answer when Admiry's support is on and the words ask for a person or for help, or complain: the
    ticket takes the turn whatever else they say, as on the homepage (home_api._say_adjust). The business's ticket is
    opened or appended with the words, as `answer` does (tools_consult._support_answer); no version is written, and the
    ticket's mail goes out after the commit (run()'s `after`). A ticket with no address to reply to yet asks the owner
    to sign in."""
    source = support.source_of(p.key)
    try:
        out = support.open_or_append(
            conn,
            business_id=p.business_id,
            source=source,
            reason=read.support,
            words=text,
            summary=read.support_summary or text,
            actor=p.actor,
            # every e-mail call arrives from the one address "mail": the channel meters its senders itself
            ip=None if source == "email" else p.ip,
            draft=p.key.cls == "draft",
        )
    except support.Limited as e:
        raise ToolError(str(e)) from e
    nxt = SIGN_IN_FOR_THE_REPLY if out["status"] == "awaiting_contact" else envelope.NONE
    return envelope.make(
        {"changed": False, "support": support.public(out)}, say=support.say_for(out, planned=True), next=nxt
    )


@agent_tool(cls="W", scope="manage")  # no card of its own: the public tool in front of it carries one (Task 13)
def change_plan(plan_id: str, words: str, idempotency_key: str) -> dict:
    """Change a plan from the owner's own words (up to 1,000 characters), sent exactly as typed. When the words name
    the budget, a budget level, the area, the offer, the objective or one channel's amount, that is changed and
    nothing else; any other request ("lead with the free consultation", "no evening ads") has Admiry rewrite the
    plan, keeping its budget. Either way the answer is a new version with its cards; the old one is kept. Words that
    ask for no change (an agreement, a thank-you, a question the answerer cannot place) change nothing, and the answer
    says so. Words that ask for a person or for help, or that complain, are never rewritten into the plan: where
    Admiry's support is available they go to the Admiry team, nothing changes and `say` tells the owner so; where it is
    not, only a change they name is made."""

    def prepare(conn, p):
        auth.check_llm_cap(conn, p)
        biz = need_business(p)
        prev = plans.get(conn, biz, plan_id)
        if prev is None or "consultation" not in prev["sections"]:
            raise ToolError("no plan with that id for this business that came from a consultation")
        text = (words or "").strip()
        if not text:
            raise ToolError("say what to change, in the owner's own words")
        if len(text) > MAX_INSTRUCTION:  # refused, never cut: the reader and the rewrite act on the same whole words
            raise ToolError(f"say what to change in at most {MAX_INSTRUCTION:,} characters")
        knowledge = (business.latest_knowledge(conn, biz) or (None, {}))[1]
        cards = plans.cards(
            {**prev, "business_name": plans.name_for(knowledge) or plans.address_for(knowledge)},
            final_host=landing.final_host(conn, biz),
        )
        try:
            read = llm.generator().read_adjust(text, cards)
        except llm.LLMError as e:
            raise ToolError(str(e)) from e
        # The support read is routed as the homepage routes it (home_api._say_adjust): where Admiry's support is on, a
        # ticket takes the turn whatever else the words say; where it is off, the read is set aside -- the fields it
        # read are still adjust's change, and words that name none are not rewritten ("how do I pay?" is no plan).
        asks = read.support != "none"
        if asks and config.load().support:
            return "ticket", (read, text)
        fields = {k: v for k in tools_consult.ADJUST_FIELDS if (v := getattr(read, k)) is not None}
        # words that call the place linked not the owner's (plan 2026-10-03-agent-listing-not-mine-words, D2, D4): the
        # listing the owner confirmed is unlinked and the plan stays as it is -- a change the same words named too is
        # not made, and the answer says so (`inner`); nothing linked, they are read as ever
        if read.listing_not_mine and profile.business_listing(knowledge) is not None:
            return "unlink", bool(fields)
        if read.understood and fields:
            return "adjust", tools_consult.adjust_prepare(conn, need_business(p), plan_id, **fields)
        if asks:
            return "nothing", NOT_A_CHANGE
        # words that name no field are rewritten only when they ask for a change: "Yes, that looks right." made a new
        # version, replaced the one the owner read and voided its code (final review B, Important 8)
        if not read.asks_for_change:
            return "nothing", NO_CHANGE_ASKED
        return "rewrite", (prev, draft_consulted(conn, biz, prev, text))

    def body(conn, p, ctx):
        kind, inner = ctx
        if kind == "ticket":
            return _to_the_team(conn, p, *inner)
        if kind == "nothing":  # `inner` is the sentence that says why nothing changed
            return envelope.make({"changed": False}, say=inner, next=envelope.NONE)
        if kind == "unlink":  # as every unlink, in this transaction; no version. Unlinked meanwhile: nothing written
            unlinked, said = listings.unlink_and_say(conn, p.business_id)
            out = {"changed": False, "listing": "unlink"} if unlinked is not None else {"changed": False}
            say = said + listings.LINK_AGAIN + (listings.NOTHING_ELSE if inner else "")  # `inner`: a change was named
            return envelope.make(out, say=say, next=envelope.NONE)
        if kind == "adjust":
            return tools_consult.adjust_body(conn, p, inner)
        prev, revised = inner
        plan = write_revision(conn, p.business_id, prev, revised)
        *_, session = revised  # draft_consulted's: the consultation behind the plan, now marked with it
        knowledge = (business.latest_knowledge(conn, p.business_id) or (None, {}))[1]
        return tools_consult._result(conn, plan, knowledge, {**session, "status": "planned"}, p)

    return run(
        "change_plan",
        "manage",
        {"plan_id": plan_id, "words": words, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
        prepare=prepare,
        after=support_mail.after_tool,  # a ticket the words opened: its mail goes out after the commit
    )
