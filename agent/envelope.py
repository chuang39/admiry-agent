"""The response envelope every tool answers in (spec 2026-09-26 §5, D34): `data` for the machine,
`say` for the owner (an assistant may relay it verbatim; it is complete on its own), `next` for what
the assistant presents (a question with options, up to seven actions, a link, or nothing).
`run()` wraps every result through `wrap()`: a tool returns `make(...)` itself, or `SAY[tool]` writes
its sentence from the data. `assistant_notes` is one line for the assistant, never shown."""

from __future__ import annotations

from collections.abc import Callable

NONE: dict = {"kind": "none"}
# Ruling R2-a (A3p-5 batch 2): the four ways to put Admiry's code on the owner's site (plan Q6) come before up to three
# launch actions -- spec 2026-09-26 §5 said three; W8 and Q6 are newer, and no word an assistant reads names the cap
MAX_ACTIONS = 7
OTHER_PLACEHOLDER = "Something else…"


def question(
    *,
    id: str,
    index: int,
    count: int,
    prompt: str,
    input: str,
    options: list[dict],
    other: bool = True,
    placeholder: str = OTHER_PLACEHOLDER,
    tool: str | None = None,
) -> dict:
    # "website" and "text": the questions common to every tree (plan 2026-10-05-agent-a3p-5-1, Q2), answered with words
    # or the website question's one option. `tool` (S0): the public tool the answer goes to, for a question asked
    # outside a consultation (what the launch needs: tools_state._launch_left); a consultation's questions carry none,
    # their answers go to update_marketing_plan as the skill says.
    assert input in ("single", "multi", "amount", "radius", "address", "website", "text"), input
    asked = {
        "id": id,
        "index": index,
        "count": count,
        "prompt": prompt,
        "input": input,
        "options": [dict(o) for o in options],
        "other": {"allowed": other, "placeholder": placeholder},
    }
    if tool:
        asked["tool"] = tool
    return {"kind": "question", "question": asked}


def actions(*items: dict) -> dict:
    """`next` with 1 to MAX_ACTIONS actions, each as an assistant reads it: its id, label and tool, the arguments that
    pick the same thing when it has them (a way to put Admiry's code on the site: plan Q6, ruling R2-a), and whether
    it leads."""
    if not 0 < len(items) <= MAX_ACTIONS:
        raise ValueError(f"1 to {MAX_ACTIONS} actions")
    out = []
    for it in items:
        entry = {"id": it["id"], "label": it["label"], "tool": it["tool"]}
        if it.get("args"):
            entry["args"] = dict(it["args"])
        if it.get("primary"):
            entry["primary"] = True
        out.append(entry)
    return {"kind": "actions", "actions": out}


def link(url: str, label: str) -> dict:
    return {"kind": "link", "link": {"url": url, "label": label}}


def make(data: dict, *, say: str, next: dict = NONE, notes: str | None = None) -> dict:
    if not isinstance(say, str) or not say.strip():
        raise ValueError("say must be owner-facing text")
    out = {"data": data, "say": say, "next": next}
    if notes:
        out["assistant_notes"] = notes
    return out


def is_envelope(result) -> bool:
    return isinstance(result, dict) and {"data", "say", "next"} <= set(result)


def wrap(tool: str, result: dict) -> dict:
    """An envelope passes through; a plain result gets its `say` from the table."""
    if is_envelope(result):
        return result
    return make(result, say=SAY[tool](result))


# ---------------------------------------------------------------- the words for the tools that exist


def _plan_url(d: dict) -> str | None:
    """create_plan/get_plan/revise_plan carry plan_url at the top."""
    return d.get("plan_url") or (d.get("summary") or {}).get("plan_url")


def _website_say(d: dict) -> str | None:
    """The plan's "Your website" row's sentence (A3p-5 batch 2, Q5: site_install.row), when the plan's cards carry one:
    get_plan's `say` says it; its `next` stays empty, as a read's always is (ruling R2-c)."""
    plan = (d.get("cards") or {}).get("plan") or {}
    row = (plan.get("setup") or {}).get("website")
    return row.get("say") if isinstance(row, dict) else None


def _plan_say(d: dict) -> str:
    lines = d.get("lines") or (d.get("summary") or {}).get("lines") or []
    head = "Here is the plan:" if lines else "The plan is on its page."
    url = _plan_url(d)
    said = [head, *[f"{ln}." if not ln.endswith(".") else ln for ln in lines]]
    website = _website_say(d)
    if website:
        said.append(website)
    return " ".join(said) + (f" Details and approval: {url}" if url else "")


def _whoami_say(d: dict) -> str:
    biz = d.get("business")
    if not biz:
        return "This key is not tied to a business."
    state = {
        "draft": "a draft: nothing publishes or spends yet",
        "claimed": "claimed by its owner",
        "verified": "verified",
    }
    s = f"You are connected to business {biz['id']}, {state.get(biz.get('claim_state'), biz.get('claim_state'))}."
    if d.get("open_session_id"):
        s += " A consultation is in progress; continue it with its session_id."
    return s


def _proposal_say(d: dict) -> str:
    if d.get("status") == "needs_verification":
        return f"This plan needs the business verified first: {d.get('verify_url')}"
    if d.get("status") == "proposed":
        return f"{d.get('summary', 'A change is proposed')}. Nothing happens until you confirm it."
    return d.get("summary") or "Done."


def _connect_link_say(d: dict) -> str:
    """Never interpolate connect_url into text: it carries the one-time claim token, and unlike a
    plain field the audit row cannot scrub a secret baked into a sentence (audit.SECRET_FIELDS scrubs
    by key, not by content) — the assistant already has the real link in data.connect_url."""
    if d.get("note"):
        return d["note"]
    return "Send the owner the sign-in link in this response so they can claim the business and manage keys."


def account_word(mode: str | None, status: str | None) -> str:
    """Where a business's account on a channel stands, in one word -- the one classification the Your ads page's
    account line (your_ads.status_word) and get_connection_status's sentence (`_connection_clause`) both read, off the
    account row's `mode` and `status` (final review M8): `needs_attention` (a platform's notice comes first, whoever
    runs it), `managed` (Admiry runs it), `connected`, `pending`, `refused`, `inactive`, else `none`."""
    if status == "needs_attention":
        return "needs_attention"
    if mode == "managed":
        return "managed"
    if status == "active":
        return "connected"
    return status if status in ("pending", "refused", "inactive") else "none"


def _connection_clause(c: dict) -> str:
    """One channel of get_connection_status in plain words (spec 2026-09-29 §4), by its `account_word`: needing
    attention, run by Admiry, connected, an invitation waiting, refused, gone inactive (a withdrawn invitation mirrors
    as inactive), or not connected yet -- which only a channel nobody tried to connect ever reads."""
    label = c.get("label")
    account = f"account {c['account_public']}" if c.get("account_public") else "the account"
    return {
        "needs_attention": f"{label} needs attention",
        "managed": f"{label} is run by Admiry from your balance",
        "connected": f"{label} is connected: {account}",
        "pending": f"{label}: the invitation to {account} is waiting for you to accept it",
        "refused": f"{label}: {account} refused the invitation; ask the owner to try again",
        "inactive": f"{label}: the link to {account} is inactive or was withdrawn; ask the owner to link it again",
    }.get(account_word(c.get("mode"), c.get("status")), f"{label} is not connected yet")


def _status_say(d: dict) -> str:
    return " ".join(f"{_connection_clause(c)}." for c in d.get("channels") or []) or "No channel is set up yet."


def _connect_say(d: dict) -> str:
    """connect_channel and its Google alias: never the claim token (connect_url here is /?setup=<business>, the
    setup step, which carries none)."""
    status = d.get("status")
    if status == "needs_agreement":
        return f"The owner has to accept the Google Ads Management Agreement first: {d.get('connect_url')}"
    if status == "pending":  # the instructions are one text (the old " ".join spelled it out letter by letter)
        return f"Invitation sent to {d.get('customer_id')}. {d.get('instructions') or ''}".strip()
    if status == "connected":
        return f"{d.get('label')} is connected: account {d.get('account_public')}."
    # the steps, then what a channel with nothing to connect waits for while it is not ready (`next_step`, M1 follow-up
    # 2 fix round 1, F2: get_connection_status's own words -- NewsBreak's funds, or NewsBreak getting the account ready)
    said = [s["text"] for s in d.get("steps") or []] + ([d["next_step"]] if d.get("next_step") else [])
    steps = " ".join(s.rstrip(".") + "." for s in said)
    return f"{d.get('label')}: {steps}" if steps else f"{d.get('label')}: finish in the Admiry conversation."


SAY: dict[str, Callable[[dict], str]] = {
    "whoami": _whoami_say,
    "list_keys": lambda d: f"{len(d.get('keys', []))} key(s) are issued for this business.",
    "revoke_key": lambda d: f"Key {d.get('revoked')} is revoked.",
    "describe_business": lambda d: (
        f"I wrote down what {(d.get('knowledge') or {}).get('name') or 'the business'} does "
        f"(version {d.get('version')})."
    ),
    "get_business": lambda d: f"Here is what I know about {(d.get('knowledge') or {}).get('name') or 'the business'}.",
    "update_business": lambda d: f"Updated (version {d.get('version')}).",
    "set_goal": lambda d: (
        "Goal recorded."
        + (
            f" Monthly budget {(d.get('goal') or {})['monthly_limit_usd']:.0f} USD."
            if (d.get("goal") or {}).get("monthly_limit_usd")
            else ""
        )
    ),
    "request_connect_link": _connect_link_say,
    "create_plan": _plan_say,
    "get_plan": _plan_say,
    "revise_plan": _plan_say,
    "approve_plan": _proposal_say,
    "list_proposals": lambda d: f"{len(d.get('proposals', []))} open proposal(s).",
    "get_proposal": _proposal_say,
    "confirm_proposal": lambda d: d.get("summary") or "Confirmed.",
    "decline_proposal": lambda d: "Declined; nothing changed.",
    "get_audit_log": lambda d: f"{len(d.get('entries', []))} recent action(s).",
    "connect_google_ads": _connect_say,
    "connect_channel": _connect_say,
    "get_connection_status": _status_say,
    "get_balance": lambda d: f"Prepaid balance: {d.get('balance_usd')} USD.",
    "request_payment_link": lambda d: (
        f"Add funds here: {d['pay_url']}" if d.get("pay_url") else d.get("note") or "Payments are not available yet."
    ),
    "list_payments": lambda d: f"{len(d.get('payments', []))} payment(s).",
}
# Tools that always return make(...) themselves, never a plain dict for wrap() to write a say for:
# start opens a consultation session (spec 2026-09-26 §4.2); answer, recommend, adjust continue it.
# get_page_kit (A3p-1 Task 2) writes its own say: whether it was e-mailed varies by call, unlike SAY's
# fixed-per-tool sentences. list_leads/update_lead (A3p-1 Task 3): same reason -- the count and the
# lead's name vary by call. set_landing_page/check_landing_page/get_page (A3p-1 Task 4): same reason --
# the say carries the check's pass count and fixes, which vary by call. get_channel_status/get_report/
# get_status_issues/pause/resume (A3 Task 5): same reason -- the sentence carries the state, the numbers or
# Google's own words, which vary by call; get_platform_statement (M0 Task 6) too: each channel's spend.
# contact_support (SP1 Task 7): same reason -- the sentence says whether the request opened a new one or joined an
# earlier one, and whether the reply still needs the owner's address.
EXPLICIT: frozenset[str] = frozenset(
    {
        "insights",  # capability layer K2: the Analyst's daily look (tools_insights)
        # capability layer K4: the research's day and count, or why there is none, and what an ask did -- each varies by
        # call.
        "get_research",
        "request_research",
        "start",
        "answer",
        "recommend",
        "adjust",
        "get_page_kit",
        "list_leads",
        "update_lead",
        "add_lead",
        "set_landing_page",
        "check_landing_page",
        "get_page",
        "get_channel_status",
        "get_report",
        "get_status_issues",
        "get_platform_statement",
        "pause",
        "resume",
        # A3p-2 Task 3: preview_page's say lists the sanitiser's own problems on a refusal; publish_page/
        # unpublish_page's say carries the proposal's summary or (after confirm_proposal) the check/pause outcome.
        "preview_page",
        "publish_page",
        "unpublish_page",
        # A3p-2 Task 4: get_page_stats' say carries the period's own counts.
        "get_page_stats",
        # A3p-2 Task 5: the page's own preview link, and what the law still needs on it, vary by call.
        "create_page",
        "update_page",
        "revise_page",
        "contact_support",
        # hackathon 2026-11-04 Task 6: ad_creatives' say follows its set's status (making, ready, needs photos, ...).
        "ad_creatives",
        # G1 Task 4: business_profile's say names the business and counts its services, which vary by call.
        "business_profile",
        # G1 Task 5: performance's say carries the period's own money and counts, which vary by call.
        "performance",
        # G1 Task 6: marketing_state's say and next follow where the business stands, which varies by call.
        "marketing_state",
        # G1 Task 7: ask's say is Admiry's answer to the question; change_plan's is the changed plan's, that the words
        # asked for a person, or that they asked for no change -- each varies by call.
        "ask",
        "change_plan",
        # G1 Task 17: change_goal's say reads the goal back as it now stands, which varies by call.
        "change_goal",
        # A3p-3: get_page_source's say names the version and where it lives, which vary by call.
        "get_page_source",
        # A3p-3 batch 3: request_site_upload's say goes with its link, which varies by call.
        "request_site_upload",
        # A3p-5 batch 2 (plan Q7, Q8): the web person's e-mail names the address it went to; Admiry's team, the contact
        # it reaches the owner at, or the ask for one -- each varies by call.
        "send_install_steps",
        "request_install_help",
        # A3p-6 batch 2 (plan Q6, Q10): the site's question, its preview address and photo link, or where it stands --
        # each varies by call.
        "build_site",
        "get_site",
        "get_site_templates",
    }
)
