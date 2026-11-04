"""The context tools (spec 2026-10-01 §4.2): where the business stands, what Admiry knows about it, where its
accounts stand. Each is one command."""

from __future__ import annotations

from .registry import dispatch, public_tool


@public_tool(cls="RO", scope="read", commands=("marketing_state",))
def get_marketing_state() -> dict:
    """Where the business's marketing stands, in one read. Call it first in every session, and again whenever you
    need to know what to do next. It returns who is calling, the business, the steps left before ads can run
    (setup: the account step is done once one place the ads run is ready; each place's own line is under it, with
    what a place with nothing to connect still waits for (reason) and what its page still needs (page);
    ready_to_launch is true when the steps before the approval are done and the launch's own check passes; blocked
    carries that check's refusal, or that the business is not verified (an undone step shows in steps, not in
    blocked); checklist is everything that stops the latest draft from launching, at once -- null when there is no
    draft (no plan, a plan in review, an approved plan with no newer draft) -- each item with its problem and
    suggestion in the owner's words, its place, whether it blocks (ready is true when nothing does), and fix: the one
    tool and the arguments that fix it, with ask_owner when only the owner knows a value (ask it, then send their
    answer in place of the <…> placeholder; never invent one) and choices when the owner picks one (send the one they
    pick); fix is null when no tool fixes it: relay its problem, and its suggestion when it has one; fix the blocking
    items in order and read the state again after each; money and the account are not items, they are the steps),
    the consultation in progress and its next question, the goal and its progress this month (limit_source says
    whether the monthly limit is the owner's or a ceiling Admiry set for the plan), the plan and how its budget is
    split, each place the ads run and how it is doing right now (its row carries what its platform says, in the
    platform's own words), this month's spend and leads, how many leads still wait for an outcome, what needs the
    owner (issues: what a platform says about an ad or an account, an empty balance, ads Admiry paused because their
    page is down, leads waiting for an outcome), the changes waiting for the owner's approval (pending), and what
    Admiry removed from the latest version's ads after Google refused it (changes: what, why, when; say mentions it:
    relay it to the owner, who was e-mailed too). `next` is the first thing left to do -- the page first while the
    business has none (the page step's ask_owner and its three choices, the owner's to pick; with a page made and not
    published yet, the one step that publishes it), then how the ads run while the owner has not chosen (the account
    step's ask_owner and its two choices), and the first blocking item's fix once the account and page steps are done --
    offered when your access allows it (none otherwise): show `say`, then offer it. USD; counts only, never a customer's
    name or number."""
    return dispatch("marketing_state")


@public_tool(cls="RO", scope="read", commands=("business_profile",))
def get_business_profile(include: list[str] | None = None) -> dict:
    """What Admiry already knows about the business, so you do not ask the owner again: who it is (business, with the
    Google Maps listing the owner confirmed as theirs, or null), where it works (locations: its address or the places
    it serves, how far it reaches, its hours and when the phone is answered), what it sells (services, each with an id
    and whether the plan leads with it), its offers, and the facts its ads may use (facts). `include` limits the
    answer to some of business, locations, services, offers, facts. A service's id names it in
    update_business_profile, in add_lead and in create_marketing_plan's `service`; a goal's focus_services takes the
    services' names."""
    return dispatch("business_profile", include=include)


@public_tool(cls="RO", scope="read", commands=("get_connection_status",))
def get_connected_accounts() -> dict:
    """Where the business's account stands on every place its ads can run: how each runs (the owner's own account,
    or run by Admiry from the owner's balance), its status (none, pending, active, refused, inactive,
    needs_attention), the account id when the owner has one to look up, and the next step in the owner's words
    while an account is not ready. A place Admiry runs from the owner's balance (NewsBreak) has nothing to connect:
    its next step says what it waits for (funds, or Admiry getting its account ready), and get_campaigns says what
    its ads wait for. Call it before connect_account, or when the owner asks whether something is connected."""
    return dispatch("get_connection_status")
