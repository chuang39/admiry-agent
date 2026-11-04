"""The Researcher's work as an owner's assistant reads and asks for it (capability layer K4, spec 2026-10-07 §4
Researcher row; founder ruling R-K4, 2026-10-08): `request_research` asks Admiry to look into the business's market in
the background -- what nearby businesses in the trade offer, the searches to aim for and to keep out, offers that fit
the trade -- at most once a day (plan D3); `get_research` reads the latest research an owner may read
(researcher.latest_view, the run the plan reads too; researcher.owner_view: AGENT_ROLES on, a run built on, the
Critic's deliver), each finding with its sources. Nothing is applied: the next time Admiry writes the plan's words (a
new plan, a rewrite the owner asks for) it reads delivered research as material; a change of the budget, area, offer,
objective or a channel's amount keeps the words and reads none. A platform key also reads every stored run (`team`).

What the two say about asking agree by construction (pre-flight F5): `get_research` offers the ask only where
`researcher.request` would queue it -- no research waiting or running (`researcher.waiting_job`), none finished within
the day (`researcher.recently_done`: the team's runs and the ones the Critic did not deliver count) and a business
nothing stops an owner's ask for (`researcher.why_not_ask`: its owner has signed in and Admiry has read it, for a run
skips a business it knows nothing of)."""

from __future__ import annotations

import re
from datetime import date

from fastmcp.exceptions import ToolError

from . import config, envelope
from .mcp_app import agent_tool, need_business, run
from .roles import research_material, researcher

KEYS = ("research", "status")  # get_research's result keys in the public contract; a platform key's adds `team`
# `recent`: nothing an owner may read, and a run finished within the day, so request_research would refuse the ask
STATUSES = ("not_on", "none", "recent", "queued", "running", "ready")
TEAM_ROWS = 5
MAX_NOTE = 500
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")  # a NUL and half a character: no jsonb holds either (sites.intake)
NOT_ON = "Admiry's research on your market is not available for this business yet."
NONE = "Admiry has not researched your market yet."
WORKING = "Admiry is researching your market; it is usually ready within the hour."
READY = "Admiry researched your market on {day}: {n} finding{s}, each with where it comes from."
QUEUED = "Admiry will research your market; it is usually ready within the hour."
QUEUED_TEAM = "Admiry's team will research your market; the results are not shown here yet."
RECENT = "Admiry researched your market less than a day ago; ask again tomorrow."
SIGN_IN = "Admiry researches a business's market once its owner has signed in."
# what a business with nothing on file is first told (tools_state.NOTHING_ON_FILE's words: the plan's first step,
# create_marketing_plan, is what reads the business and writes its knowledge)
UNREAD = "Admiry cannot research your market until it knows your business; start by saying what it does and where."
# why an owner's ask is turned away (researcher.why_not_ask) -> the words: request_research refuses with them, and
# get_research says them in place of NONE or RECENT and offers no ask
REFUSED = {"draft": SIGN_IN, "unread": UNREAD}
BAD_TOPICS = f"topics must be among {', '.join(research_material.TOPICS)}"
LONG_NOTE = f"note must be at most {MAX_NOTE} characters"
ASK = {"id": "request_research", "label": "Ask Admiry to research your market", "tool": "request_research"}


def _latest(conn, settings: config.Settings, biz: str) -> dict | None:
    """What an owner may read of the business's latest research: the run `researcher.latest_view` picks, which the
    Strategist reads too."""
    found = researcher.latest_view(conn, settings, biz)
    return found.view if found is not None else None


def _status(conn, settings: config.Settings, biz: str, view: dict | None) -> str:
    """Where the business's research stands for its owner: not on, readable (`ready`), waiting or running, finished
    within the day with nothing readable (`recent`), or none."""
    if settings.roles_mode != "on":
        return "not_on"
    if view is not None:
        return "ready"
    job = researcher.waiting_job(conn, biz)
    if job is not None:
        return job["status"]
    return "recent" if researcher.recently_done(conn, biz) else "none"


def _say(status: str, view: dict | None, *, barred: str | None) -> str:
    if status == "ready":
        d = date.fromisoformat(view["day"])
        n = len(view["findings"])
        return READY.format(day=f"{d:%A, %B} {d.day}", n=n, s="" if n == 1 else "s")
    if status in ("none", "recent") and barred:  # an ask now, or tomorrow, would be turned away
        return REFUSED[barred]
    return {"not_on": NOT_ON, "none": NONE, "recent": RECENT, "queued": WORKING, "running": WORKING}[status]


def _ask_say(settings: config.Settings, asked: researcher.Requested) -> str:
    """What an ask did, in words: nothing with the roles off; research queued, or already waiting or running (the
    results are the team's, not shown, while the roles are on validate); or one finished within the day."""
    if asked.status == "off":
        return NOT_ON
    on = settings.roles_mode == "on"
    if asked.queued:
        return QUEUED if on else QUEUED_TEAM
    if asked.status == "recent":
        return RECENT
    return WORKING if on else QUEUED_TEAM


def _topics(topics: list[str] | None) -> list[str] | None:
    if not topics:
        return None
    if any(t not in research_material.TOPICS for t in topics):
        raise ToolError(BAD_TOPICS)
    return list(dict.fromkeys(topics))


def _storable(text: str | None) -> str | None:
    """`text` without the characters no text can hold -- a NUL, half of a character: JSON can name both and jsonb takes
    neither -- or None as it is."""
    return None if text is None else _UNSTORABLE.sub("", text)


def _note(note: str | None) -> str | None:
    """The owner's question as it is kept: without those characters (the job's brief failed to insert with one),
    stripped, and at most MAX_NOTE of what is kept. None when nothing is left."""
    words = (_storable(note) or "").strip()
    if len(words) > MAX_NOTE:
        raise ToolError(LONG_NOTE)
    return words or None


@agent_tool(cls="RO", scope="read")
def get_research() -> dict:
    """Admiry's latest research on the business's market: research.findings (each with how sure Admiry is and the
    sources it rests on, with their links), research.searches_to_aim_for and research.searches_to_keep_out (what
    customers here type, and what the ads should stay away from), research.offer_angles, research.unknowns (what the
    sources could not answer) and the day it was done. status is ready (there is research to show), queued or running
    (Admiry is on it), recent (Admiry researched less than a day ago and has nothing to show from it), none (ask with
    request_research) or not_on. Show `say` as written; share the findings in their own words, never as a promise.
    The next time Admiry writes the plan's words -- a rewrite the owner asks for, or a new plan -- it reads research
    of the last 90 days by itself; a change of the budget, area, offer, objective or a channel's amount alone keeps
    the words as they are."""

    def body(conn, p):
        biz = need_business(p)
        settings = config.load()
        view = _latest(conn, settings, biz)
        status = _status(conn, settings, biz, view)
        barred = researcher.why_not_ask(conn, biz)
        data = {"research": view, "status": status}
        if p.key.cls == "platform":  # Admiry's team: every stored run, whatever an owner may read (not in KEYS)
            rows = conn.execute(
                "SELECT * FROM research_run WHERE business_id = %s ORDER BY created_at DESC LIMIT %s",
                (biz, TEAM_ROWS),
            ).fetchall()
            data["team"] = [researcher.team_view(r) for r in rows]
        nxt = envelope.actions(ASK) if status == "none" and barred is None else envelope.NONE
        return envelope.make(data, say=_say(status, view, barred=barred), next=nxt)

    return run("get_research", "read", {}, body)


@agent_tool(cls="W", scope="read")
def request_research(idempotency_key: str, topics: list[str] | None = None, note: str | None = None) -> dict:
    """Ask Admiry to research the business's market in the background: what nearby businesses in the trade offer, the
    searches to aim for and to keep out, offers that fit the trade. topics: any of competitors, search_phrases,
    local_demand, seasonality, offer_angles (all when left out); note: the owner's own question, in their words, up to
    500 characters. At most once a day; nothing spends and nothing in the plan changes. Show `say` as written, and
    read the result later with get_research."""

    def body(conn, p):
        biz = need_business(p)
        settings = config.load()
        wanted, words = _topics(topics), _note(note)
        asked = researcher.request(conn, settings, biz, trigger="owner", topics=wanted, note=words, by=p.actor)
        if asked.status in REFUSED:  # not signed in, or not read: the words get_research gives, as a refusal
            raise ToolError(REFUSED[asked.status])
        view = _latest(conn, settings, biz)
        data = {"queued": asked.queued, "status": _status(conn, settings, biz, view), "research": view}
        return envelope.make(data, say=_ask_say(settings, asked))

    # the note as run() records it: the audit row holds the call's arguments, and refuses half a character as the job's
    # brief does (audit._storable takes out the NUL only); the call's identity, a repeat's, is of the note kept
    return run(
        "request_research",
        "read",
        {"topics": topics, "note": _storable(note), "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
    )
