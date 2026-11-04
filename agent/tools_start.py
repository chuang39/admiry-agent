"""`start` (spec 2026-09-26 §4.2, revised A1b-4b Task 4): begin or redo the consultation for the caller's
own business. One sentence → a new knowledge version, a goal read from the words (never erasing an
owner-stated one with nothing), and a consultation session with the first question — superseding any
session still open. It no longer drafts a plan: `recommend` does, after the questions.

`begin_anonymous` is the pre-Task-4 anonymous entry point (a draft business, its knowledge, a goal, a
session, and a 24-hour draft key), kept as a plain function for the homepage (A1b-4c) rather than an
`/mcp` tool."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastmcp.exceptions import ToolError

from . import (  # noqa: F401 (crawl: patched in tests)
    auth,
    business,
    claims,
    config,
    consult,
    crawl,
    envelope,
    geocode,
    keys,
    listings,
    llm,
    places,
    plans,
    profile,
)

SESSION_TTL = timedelta(hours=24)
STEP_LABELS = (
    "Understood the goal",
    "Read your website and Google listing",
    "Checked local search demand",
    "Compared channels",
    "Estimated what the budget will do",
)
NO_SITE_LABEL = "No website or listing to read; went on what you told me"
# How the first turn says how many questions wait (opening_say): one to six spelled, more than six "a few" (ruling R0-h)
SPELLED = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def steps(session: dict, knowledge: dict, *, planned: bool) -> list[dict]:
    # a website or the business's own Google listing, as the owner confirmed it, was read (plan
    # 2026-10-03-agent-listing-confirm): an address the owner placed is neither, and a lookup's guess is not theirs
    read = bool(knowledge.get("website") or profile.business_listing(knowledge))
    settled = planned or session["status"] in ("ready", "planned")
    done = [settled, read or settled, settled, planned, planned]
    labels = list(STEP_LABELS)
    if not read and settled:  # nothing to read: the step is over, and says so, rather than spinning forever
        labels[1] = NO_SITE_LABEL
    return [{"label": label, "done": flag} for label, flag in zip(labels, done, strict=True)]


def opening_say(
    understanding: dict, corrections: list[str], remaining: int, notes: list[str] = (), question: str | None = None
) -> str:
    """The first turn's words; `question` (consult.question_sentence of the first question) ends it, so the
    text alone asks the owner something they can answer."""
    what = understanding.get("category_detail") or "business"
    lead = (
        f"I can build that for you. {understanding['name']} is a {what}."
        if understanding.get("name")
        else f"I can build that for you. You run a {what}."
    )
    parts = [lead, *corrections]
    if len(notes) == 1:
        parts.append(f"One thing I could not use: {notes[0]}.")
    elif notes:
        parts.append(f"A few things I could not use: {'; '.join(notes)}.")
    if remaining:
        # one to six spelled (ruling P2); more than six "a few" (A3p-5 batch 2 Task 0, ruling R0-h: the founder's words,
        # 2026-10-05) -- without a website read the website question and the name come first, then the places on
        # Google Maps -- counted while the name may still find them (ruling F1) -- and the services, then the trade's
        # own, where the ads centre still to ask: up to nine
        n = SPELLED.get(remaining, "a few")
        parts.append(f"Before I plan anything, {n} quick question{'s' if remaining != 1 else ''}.")
        if question:
            parts.append(question)
    else:
        parts.append("I have what I need; say the word and I will build the plan.")
    return " ".join(parts)


# `steps` above must be defined before this import: mcp_app's bottom-of-module import list loads
# tools_consult, which imports `steps` back from this module, and that happens while this module's own
# top-level code is still running (Python re-enters this partially-initialized module rather than
# reimporting it) — so anything tools_consult needs from here must already exist above this line.
from .mcp_app import agent_tool, need_business, run, run_anonymous
from .tools_business import check_locale, check_text, extract, site_pages


def url_in_words(text: str) -> str | None:
    """The first address in the owner's opening words (W2) that is not a social or listing site's page: the scan the
    website question's answer gets too (consult.site_in_words, ruling R2-d), a social page passed over for the next
    address. None when the words name none."""
    return consult.site_in_words(text, social=False)


def with_unread_site(knowledge: dict, url: str) -> dict:
    """`knowledge` with `url`, an address that could not be read, as its `website_unread` and never its `website`
    (ruling R2-e: a `website` is a site read -- steps() says it was, and plans.name_for lets it vouch for the name): a
    `website` naming the same site (listings.same_site: the same host, `www.` aside) is taken off; another site's
    stays."""
    out = {**knowledge, "website_unread": url}
    if listings.same_site(out.get("website"), url):
        del out["website"]
    return out


def _read_site(
    text: str, website_url: str | None, locale: str, gen, pages: list[dict] | None = None
) -> tuple[dict, dict | None]:
    """The extraction, and the site tried, as ruling P1 wants it carried on the session's known facts
    (`{"url": ..., "read": bool}`; None when no site was tried at all): `website_url`, when the caller passed one, or
    else the first address in the owner's own words (`url_in_words`, W2). The site is crawled once (`pages`, when the
    caller crawled it already, is not crawled again) and counts as read only when the crawl read a page (ruling R2-a):
    with none -- a redirect to another host, a timeout, nothing but non-HTML -- the reading goes on from the words, and
    the knowledge names the address as unread (`with_unread_site`, ruling R2-e). An explicit `website_url` the crawl
    refuses still refuses (today's refusal: an assistant passed it and can say so); one found in the words goes on
    without it (Q8). Either way the model is called once."""
    url = website_url or url_in_words(text)
    if not url:
        return extract(text, None, locale, gen=gen), None
    if pages is None:
        try:
            pages = site_pages(url)
        except ToolError:  # the crawl refused the address
            if website_url:
                raise
            pages = []
    if not pages:  # nothing read: no reading of the site, so it never fills the knowledge's website
        return with_unread_site(extract(text, None, locale, gen=gen, pages=[]), url), {"url": url, "read": False}
    return extract(text, url, locale, gen=gen, pages=pages), {"url": url, "read": True}


def _extract_ctx(
    conn,
    text: str,
    website_url: str | None,
    locale: str,
    known_facts: dict | None,
    *,
    business_id: str | None,
    pages: list[dict] | None = None,
):
    """The extraction `start` and `begin_anonymous` share (and `read_site`, with the `pages` it crawled): knowledge from
    the words (and the site when there is one, `_read_site`), and the goal it implies, clipped to range with a note
    each. Nothing is looked up here: the business is looked up after the reading, by its name (listings.lookup)."""
    settings = config.load()
    check_locale(locale)
    check_text(text)
    try:
        gen = llm.generator(settings)
    except llm.LLMError as e:
        raise ToolError(str(e)) from e
    facts = consult.clean_known_facts(known_facts)
    data, site = _read_site(text, website_url, locale, gen, pages)
    # a site was tried, read or not (ruling P1): the session's known facts carry it -- for consult.preanswers when it
    # was read, for the website question's words and options when it could not be opened (A3p-5 batch 2 Task 0, R0-i)
    if site:
        facts = {**facts, "website": site}
    hint = data.get("goal_hint") or {}
    limit = facts.get("monthly_budget_usd") or hint.get("monthly_budget_usd")
    stated = facts.get("target_customers_per_month")
    wanted = int(stated) if stated is not None else hint.get("target_customers_per_month")
    target, limit, focus, goal_notes = business.check_goal(wanted, limit, hint.get("focus_services") or [])
    ceiling = facts.get("max_cost_per_customer_usd")
    if not business.cost_ok(ceiling):
        goal_notes.append(f"a most-per-customer of ${ceiling:,.0f} was out of range and was not recorded")
        ceiling = None
    goal = {
        "target": target,
        "monthly_limit_usd": limit,
        "focus_services": focus,
        "max_cost_per_customer_usd": ceiling,
        "goal_type": facts.get("goal_type"),  # what the goal counts, when stated
    }
    return data, goal, facts, goal_notes


def _offered(conn, data: dict, facts: dict, *, business_id: str | None) -> list[dict]:
    """The places close to the business that the consultation's first question offers (plan
    2026-10-03-agent-listing-confirm): none when the business already has a listing the owner confirmed; never one
    the owner said is not theirs. Calls Google (prepare only)."""
    on_file = (business.latest_knowledge(conn, business_id) or (None, {}))[1] if business_id else {}
    if profile.business_listing(on_file):
        return []
    rejected = on_file.get("listing_rejected") or []
    return listings.lookup(conn, config.load(), data, facts, business_id=business_id, rejected=rejected)


def _owners_address(data: dict, facts: dict) -> dict:
    """`facts` with the address the owner's own words gave (the reading's stated_address: a street address or ZIP code)
    as the known address, when no assistant passed one: it goes on the map and centres the ads as that one does (the
    founder's dev walk, 2026-10-04), under the known facts' own rules (consult.clean_known_facts). The reading's field
    is taken off `data`: the profile keeps the place on the map, never the words."""
    stated = data.pop("stated_address", None)
    if facts.get("address"):
        return facts
    return {**facts, **consult.clean_known_facts({"address": stated})}


def _known_place(conn, biz: str | None, facts: dict) -> places.Candidate | None:
    """The address an assistant already knew (the known fact `address`), put on the map: it is where the ads centre,
    always -- whatever start's lookup found and whatever is on file (walk fixes 1, Task 4 rounds 1 and 2, the
    controller's rulings). The body adopts it as update_business adopts an address (profile.apply): the business's
    listing on file is moved to it, its name kept; else the place is kept as an address. The location question is then
    pre-answered from its point. The lookup, the budget, the cap and the row of any address (geocode.place: start's own
    lookup of the business is a row already, so the budget counts it). None when there is no such address, no key, no
    place found, or no lookups left (geocode.NoLookups): the question is then asked (skipped on a server with no key)."""
    address = facts.get("address")
    settings = config.load()
    if not address or not geocode.can_place(settings):
        return None
    try:
        return geocode.place(conn, settings, address, business_id=biz)
    except geocode.NoLookups:
        return None


class SiteUnreadable(ToolError):
    """A website answered mid-consultation that cannot be read (`read_site`): the crawl refused the address, or read no
    page (ruling R2-a). The website answer goes on without it (Q8)."""


NOTHING_READ = "could not read the website: no page of it could be read"


def read_site(
    conn, biz: str, session: dict, url: str
) -> tuple[dict, dict, dict, list[str], list[dict], places.Candidate | None]:
    """The reading a website answered mid-consultation brings (Q7, W3), as `start` reads one: the crawl, once, and one
    model reading of the session's opening words with its pages (`_extract_ctx`, the site passed as `website_url`), the
    address the words gave (`_owners_address`), the places close to the business (`_offered`) and the address an
    assistant knew, on the map (`_known_place`) -- (data, goal, facts, goal_notes, offered, placed), for `adopt_reading`
    and consult.restart. SiteUnreadable, before any model call, when the crawl refuses the address or reads no page (a
    redirect to another host, a timeout, nothing but non-HTML: ruling R2-a): the caller goes on without the site (Q8).
    A model that fails raises ToolError, from llm.LLMError. Calls the crawler, the model and Google: prepare only."""
    try:
        pages = site_pages(url)
    except ToolError as e:
        raise SiteUnreadable(str(e)) from e
    if not pages:
        raise SiteUnreadable(NOTHING_READ)
    data, goal, facts, goal_notes = _extract_ctx(
        conn, session["opening_text"], url, session["locale"], session.get("known_facts"), business_id=biz, pages=pages
    )
    facts = _owners_address(data, facts)
    return data, goal, facts, goal_notes, _offered(conn, data, facts, business_id=biz), _known_place(conn, biz, facts)


def _session_payload(
    conn, biz: str, text: str, locale: str, data: dict, facts: dict, offered: list[dict], goal_notes: list[str]
):
    """The consultation session `start` and `begin_anonymous` both build from the extraction, and the
    envelope around it (business_id and connect_url are the caller's to fill in). `offered`, the places close to the
    business (`_offered`), are the first question's options."""
    opening_text = text.replace("\x00", "")  # a TEXT column, unlike args_redacted, cannot hold a NUL
    tree_name = consult.tree_for(data.get("category", "fallback"))
    tree = consult.load_tree(tree_name)
    options = consult.build_options(tree, data, candidates=offered, known_facts=facts)
    session = consult.create(
        conn,
        biz,
        tree=tree_name,
        locale=locale,
        opening_text=opening_text,
        known_facts=facts,
        options=options,
        answers=consult.preanswers(tree, facts, data, opening_text, can_place=geocode.can_place(config.load())),
        expires_at=datetime.now(timezone.utc) + SESSION_TTL,
    )
    understanding = {
        "name": plans.name_for(data),  # only a name a website or listing vouches for leads the opening sentence
        "category": data.get("category"),
        "category_detail": data.get("category_detail"),
        "service_area": data.get("service_area") or [],
        "services": [s["name"] for s in data.get("services", [])],
    }
    said = consult.corrections(tree, opening_text)
    # a site that read no page -- an address found in the words (W2, Q8) or an explicit website_url (ruling R2-a) -- is
    # said once, SITE_UNREAD_SAY right after the lead sentence, before the first question, which asks for the address
    # again with Try again and Ask me instead (A3p-5 batch 2 Task 0, ruling R0-i; spec 2026-10-12 §3.2, R1) -- in `say`
    # alone: data.corrections stays the reading's corrections, as assistants read it
    opening = said
    if (tried := consult.site_unopened(facts)) is not None:
        opening = [consult.SITE_UNREAD_SAY.format(host=consult.host_of(tried)), *said]
    first = consult.next_question(session)
    nxt = first or envelope.actions({"id": "recommend", "label": "Build my plan", "tool": "recommend", "primary": True})
    payload = {
        "session_id": session["id"],
        "understanding": understanding,
        "corrections": said,
        "notes": goal_notes,
        "steps": steps(session, data, planned=False),
    }
    if facts.get("website", {}).get("read"):  # what was taken from the site, for the home's facts card (R6)
        payload["reading"] = consult.reading_view(data)
    say = opening_say(
        understanding,
        opening,
        consult.questions_left(session),  # what the first question's card counts (ruling F1)
        goal_notes,
        consult.question_sentence(first) if first else None,
    )
    return payload, say, nxt


HANDOFF_DEAD = "this hand-off link was already used or expired; start again on the website"
HANDOFF_FOREIGN = "this plan belongs to a signed-in account; connect that account instead"


def _handoff_step(conn, principal, session: dict) -> dict:
    """The moved session's next step, in start's own envelope shape (Task 3): the question it left off
    on, the recommend action when it was already fully answered, or — when the owner built the plan on the
    website — that plan, with its cards and launch actions (never the questions again)."""
    if session["status"] == "planned":
        plan = plans.latest(conn, session["business_id"])
        if plan is not None:
            from . import tools_consult  # local: tools_consult imports steps() from this module

            latest = business.latest_knowledge(conn, session["business_id"])
            env = tools_consult._result(conn, plan, latest[1] if latest else {}, session, principal)
            env["say"] = "I picked up the plan you built on the website. " + env["say"]
            return env
    nxt = consult.next_question(session) or envelope.actions(
        {"id": "recommend", "label": "Build my plan", "tool": "recommend", "primary": True}
    )
    is_question = nxt["kind"] == "question"
    tail = consult.question_sentence(nxt) if is_question else "Say the word and I will build the plan."
    payload = {
        "business_id": session["business_id"],
        "draft_key": None,
        "connect_url": f"{config.load().web_base_url}/connect",
        "session_id": session["id"],
    }
    notes = (
        "Show say as written, then present next.question's options as choices with a way to type an answer."
        if is_question
        else "Show say as written, then offer next.actions as a button."
    )
    return envelope.make(payload, say="I picked up the plan you started on the website. " + tail, next=nxt, notes=notes)


def adopt_reading(
    conn, biz: str, data: dict, goal: dict, facts: dict, offered: list[dict], placed: places.Candidate | None
) -> dict:
    """The knowledge version, the placed address adopted (or the owner's own place readopted), the goal and the
    listing offer a fresh reading brings (Q9): `start`'s own body, from after `consult.supersede_open` to before
    `_session_payload` -- the website answer (tools_consult.answer, Q7) calls it the same way, without superseding,
    since that restarts the session in place rather than opening a new one. `facts` rides along unused: both callers
    have it from the same extraction (`read_site`'s, for the website answer). Returns the knowledge on file."""
    before = business.latest_knowledge(conn, biz)  # where the owner put the ads' centre, if they did: kept below
    business.add_knowledge(conn, biz, data, source="extract")
    data = business.latest_knowledge(conn, biz)[1]  # the version on file (profile.carry kept a service's stored id)
    if placed is not None:  # the address the assistant knew, on the map: adopted as update_business adopts one
        where = {"location": {"address": placed.address, "listing": geocode.listing(placed)}}
        business.add_knowledge(conn, biz, profile.apply(data, where), source="owner")
        data = business.latest_knowledge(conn, biz)[1]
    else:  # nothing placed by this call: the place the owner put on the map stays the centre (A1), even when the
        # call named an address the map could not place or the day's lookups were spent
        data = business.readopt_place(conn, biz, (before[1] if before else {}).get("listing"))
    ceiling, goal_type = goal["max_cost_per_customer_usd"], goal["goal_type"]
    stated = (goal["target"], goal["monthly_limit_usd"], ceiling, goal_type)  # a stated type alone is reason enough
    if any(value is not None for value in stated):
        limit, limit_source, focus = goal["monthly_limit_usd"], "owner", goal["focus_services"]
        prev = business.latest_goal(conn, biz)
        # what the words leave out is carried forward, never erased: an owner-stated limit, the focus services
        if limit is None and prev and prev["monthly_limit_usd"] is not None:
            limit, limit_source = prev["monthly_limit_usd"], prev["limit_source"]
        if not focus and prev:
            focus = prev["focus_services"]
        business.set_goal(
            conn,
            biz,
            target=goal["target"] if goal["target"] is not None else (prev or {}).get("target"),
            monthly_limit_usd=limit,
            focus_services=focus,
            limit_source=limit_source,
            max_cost_per_customer_usd=ceiling if ceiling is not None else business.KEEP,
            goal_type=goal_type if goal_type is not None else business.KEEP,
        )
    listings.save_offer(conn, biz, offered)  # the places the first question offers, replacing any before
    return data


@agent_tool(cls="W", scope="manage", open_world=True)
def start(
    text: str = "",
    website_url: str | None = None,
    locale: str = "en-US",
    known_facts: dict | None = None,
    handoff: str | None = None,
) -> dict:
    """Begin, or redo, the consultation for this business: one or two sentences on what it does, where,
    and what the owner wants, plus the website if there is one. Put everything you already know about the
    owner in known_facts (consult.KNOWN_FACTS: business_name, address, objective, service, customer_value_usd,
    radius_miles, monthly_budget_usd, offer, target_customers_per_month, max_cost_per_customer_usd, goal_type): a
    fact that answers a question skips it, and the last three go onto the goal (goal_type — customers, calls or
    bookings — is what it counts). Admiry reads the site, looks the business up on Google Maps by its name, writes
    what it understood, and returns the first question to ask the owner — when Google Maps lists places close to the
    business, whether one of them is it (present the places as choices; Admiry links only the one the owner picks).
    Call it again to start over from new words — any open consultation is superseded, and an owner-stated monthly
    budget is kept unless the new words name a different one. If the owner pasted a hand-off token from
    the website (a `hs_…` code, valid 30 minutes, one use), pass it as `handoff`: if it already names this
    business (e.g. right after connecting through the consent page's "Continue the plan you started on the
    website"), nothing is moved; otherwise, when that business is still an unclaimed draft, its knowledge
    and goal are folded in and it is retired. With no text, this resumes that consultation exactly where
    the website left off, no new words needed — the next question, or the finished plan itself with its
    cards and launch actions; with text too, a fresh consultation then starts from the new words, as an
    ordinary `start` would. An unknown, already-used or expired token is refused, and so is one
    naming a business already claimed by someone else — ask the owner to start again on the website or
    connect that account instead. Returns business_id, connect_url (the owner's page), session_id (for
    answer and recommend), and notes (things read from the words that could not be used, e.g. a budget out
    of range — say already mentions them)."""

    def prepare(conn, p):
        biz = need_business(p)
        if handoff is not None and not consult.peek_handoff(conn, handoff):
            # Refuse before any extraction, so a dead token never spends a model call; take_handoff still
            # does the real, consuming check in body() under the transaction (this is read-only).
            raise ToolError(HANDOFF_DEAD)
        if handoff is not None and not text.strip():
            return None  # no words to extract: the body only moves the hand-off session
        auth.check_llm_cap(conn, p)
        data, goal, facts, goal_notes = _extract_ctx(conn, text, website_url, locale, known_facts, business_id=biz)
        facts = _owners_address(data, facts)
        return (
            data,
            goal,
            facts,
            _offered(conn, data, facts, business_id=biz),
            goal_notes,
            _known_place(conn, biz, facts),
        )

    def body(conn, p, ctx):
        biz = need_business(p)
        if handoff is not None:
            session = consult.take_handoff(conn, handoff)
            if session is None:
                raise ToolError(HANDOFF_DEAD)
            if session["business_id"] != biz:
                # The token's session still belongs to another business: only a business still an
                # unclaimed, active draft may be folded in (and retired) this way — a claimed business,
                # the signer's own or anyone else's, is never expired or stripped of its keys just because
                # someone pasted its hand-off token elsewhere (a raised ToolError rolls this transaction
                # back, so take_handoff's clearing above is undone and the token stays redeemable).
                # Locked (m3, final review): a concurrent claim of the draft (the wall's sign-in, the consent
                # page) waits for this move, or this move sees it claimed — never both.
                source = conn.execute(
                    "SELECT claim_state, status FROM business WHERE id = %s FOR UPDATE", (session["business_id"],)
                ).fetchone()
                if source is None or source["claim_state"] != "draft" or source["status"] != "active":
                    raise ToolError(HANDOFF_FOREIGN)
                source_biz = session["business_id"]
                session = consult.move_session(conn, session, to_business=biz)
                listings.move_offer(conn, source_biz, biz)  # the places its first question offers go with it
            if ctx is None:
                return _handoff_step(conn, p, session)
        data, goal, facts, offered, goal_notes, placed = ctx
        consult.supersede_open(conn, biz)
        data = adopt_reading(conn, biz, data, goal, facts, offered, placed)
        payload, say, nxt = _session_payload(conn, biz, text, locale, data, facts, offered, goal_notes)
        payload = {
            "business_id": biz,
            "draft_key": None,
            "connect_url": f"{config.load().web_base_url}/connect",
            **payload,
        }
        return envelope.make(
            payload,
            say=say,
            next=nxt,
            notes="Show say as written, then present next.question's options as choices with a way to type an answer.",
        )

    return run(
        "start",
        "manage",
        {"text": text, "website_url": website_url, "locale": locale, "known_facts": known_facts, "handoff": handoff},
        body,
        prepare=prepare,
    )


def begin_anonymous(
    text: str, website_url: str | None = None, locale: str = "en-US", known_facts: dict | None = None, *, ip: str
) -> dict:
    """The pre-Task-4 anonymous `start`: a draft business, its knowledge, a goal read from the words, a
    consultation session with the first question, and a 24-hour draft key. Not an `/mcp` tool (A1b-4c's
    homepage calls this directly); `ip` stands in for the request header `run_anonymous` reads for a real
    MCP call, since this function opens no request of its own."""

    def prepare(conn, _ip):
        data, goal, facts, goal_notes = _extract_ctx(conn, text, website_url, locale, known_facts, business_id=None)
        facts = _owners_address(data, facts)
        offered = _offered(conn, data, facts, business_id=None)  # no business yet
        return data, goal, facts, offered, goal_notes, _known_place(conn, None, facts)

    def body(conn, _ip, ctx):
        data, goal, facts, offered, goal_notes, placed = ctx
        biz = business.create(conn, locale=locale)
        business.add_knowledge(conn, biz, data, source="extract")
        if placed is not None:  # the address the words gave, on the map: adopted as start adopts one
            where = {"location": {"address": placed.address, "listing": geocode.listing(placed)}}
            business.add_knowledge(conn, biz, profile.apply(data, where), source="owner")
        listings.save_offer(conn, biz, offered)  # the places the first question offers
        data = business.latest_knowledge(conn, biz)[1]  # as in start; on a new business it is the extraction itself
        business.set_goal(
            conn,
            biz,
            target=goal["target"],
            monthly_limit_usd=goal["monthly_limit_usd"],
            focus_services=goal["focus_services"],
            max_cost_per_customer_usd=goal["max_cost_per_customer_usd"],
        )
        draft_key, _ = keys.mint(conn, business_id=biz, cls="draft", scopes=("read", "manage"), label="draft")
        connect_url, _ = claims.new_link(conn, biz)
        payload, say, nxt = _session_payload(conn, biz, text, locale, data, facts, offered, goal_notes)
        payload = {"business_id": biz, "draft_key": draft_key, "connect_url": connect_url, **payload}
        return envelope.make(
            payload,
            say=say,
            next=nxt,
            notes="Show say as written, then present next.question's options as choices with a way to type an answer.",
        )

    return run_anonymous(
        "start",
        {"text": text, "website_url": website_url, "locale": locale, "known_facts": known_facts},
        prepare=prepare,
        body=body,
        ip=ip,
    )
