"""A command's next actions in the public surface's names (spec 2026-10-01 §9.1; plan G1 Task 9).

A command names its own tools in `next.actions[].tool`: the homepage's script acts on those names, so they stay. An
assistant is told the public tool instead, and the argument that picks the same thing. Sentences never name a tool
(test_sentences_name_no_tool), so `next` is the only place this matters."""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from .. import ads_mode

# a command's tool -> (the public tool, the arguments that pick the same thing)
TOOL: dict[str, tuple[str, dict | None]] = {
    "recommend": ("get_marketing_plan", None),
    "adjust": ("update_marketing_plan", None),
    "connect_channel": ("connect_account", None),
    "connect_google_ads": ("connect_account", None),
    "request_connect_link": ("connect_account", None),
    "request_payment_link": ("get_billing", {"view": "topup_link"}),
    "set_landing_page": ("update_landing_page", {"action": "use_url"}),
    "check_landing_page": ("get_landing_page", {"view": "check"}),
    "approve_plan": ("launch_marketing_plan", None),
    "publish_page": ("update_landing_page", {"action": "publish"}),
    "revise_page": ("update_landing_page", {"action": "revise"}),
    "get_page_source": ("get_landing_page", {"view": "source"}),  # A3p-3: a change bigger than the template holds
    # A3p-6 batch 2 (Q6, Q15): an industry site's "Change the look" is the page tool's build_site action, and its view
    # is the page tool's site view
    "build_site": ("update_landing_page", {"action": "build_site"}),
    "get_site": ("get_landing_page", {"view": "site"}),
}


# The founder's ruling (2026-10-13): the owner chooses how the ads run. A plan answer offers the two ways in the home's
# names -- Admiry running the ads (tools_consult.MANAGED_ACTION, first on the home) and the owner's own account
# (OWN_ACCOUNT_ACTION), both connect_channel -- and an assistant is told connect_account's own question's two choices
# in their place (ads_mode.way_actions): side by side, neither marked first. One of them alone is no choice between
# the two, and is translated as any other action is. A business that has already chosen (ads_mode.chosen: its own
# account linked, live or pending, or the agreement accepted; one Admiry runs the ads for prepays, and a plan offers it
# neither way) is never asked again (owner-chooses review, fix round 1, item 1): it is told one connect_account, the own
# account's, with no arguments -- which answers where its way stands (connected, the invitation pending, or the
# Customer ID still to give) -- and Admiry's way is not offered in its place.
#
# The page comes before the money (the same day's ruling): a plan answer that offers any way the ads run -- either of
# the two, or the funds for a business that prepays (PREPAY_ACTION, the same id) -- while the caller's business has no
# page settled for its latest draft asks the page question instead: its `say` ends with it, and its `next` is the
# page's three ways alone (launch_check.page_actions) -- or, a page made and not published yet, the step that publishes
# it (launch_check.page_ask; final review, item 1) -- never a step toward the way or the money.
MANAGED_ID = "prepay"
WAY_IDS = (MANAGED_ID, "link_own_account")


class Owner(NamedTuple):
    """What a plan answer that offers a way asks of the caller's business, read at once (where.owner_choice;
    page-first review, item 3): the page question and its ways as actions while the page comes first, else None; and
    whether the business has already chosen how its ads run (ads_mode.chosen)."""

    page: tuple[str, list[dict]] | None
    chosen: bool


def _offers_both_ways(actions: list[dict]) -> bool:
    return {a["id"] for a in actions if a["tool"] == "connect_channel" and a["id"] in WAY_IDS} == set(WAY_IDS)


def translate(envelope: dict, *, owner: Callable[[], Owner] | None = None) -> dict:
    """The envelope with its actions' tools in public names (a copy; an envelope without actions as it is); the two ways
    a plan offers as the owner's choice between them (WAY_IDS), where the first of them stood -- unless the caller's
    business has already chosen how its ads run (`owner().chosen`): then the own account's action alone, as any other
    action is translated. Before all that, while the page comes first (`owner().page`: the page question and its
    actions), an envelope that offers a way says the question and offers the page's ways alone. `owner()` is called
    once, and only for an envelope that offers a way (WAY_IDS)."""
    nxt = envelope.get("next") if isinstance(envelope, dict) else None
    if not isinstance(nxt, dict) or nxt.get("kind") != "actions":
        return envelope
    asked = owner() if owner is not None and any(a["id"] in WAY_IDS for a in nxt["actions"]) else None
    if asked is not None and asked.page is not None:  # no page settled: the page question, not the ways or the money
        question, ways = asked.page
        return {**envelope, "say": f"{envelope['say']} {question}", "next": {**nxt, "actions": ways}}
    both = _offers_both_ways(nxt["actions"])
    asks = both and not (asked is not None and asked.chosen)
    out, told = [], False
    for action in nxt["actions"]:
        if both and action["tool"] == "connect_channel" and action["id"] in WAY_IDS:
            if asks and not told:  # the two choices once, where the first of the two ways stood
                out.extend(ads_mode.way_actions())
                told = True
            if asks or action["id"] == MANAGED_ID:  # a way chosen: the own account's connect_account alone
                continue
        public, args = TOOL.get(action["tool"], (action["tool"], None))
        entry = {**action, "tool": public}
        if args:
            entry["args"] = dict(args)
        out.append(entry)
    return {**envelope, "next": {**nxt, "actions": out}}
