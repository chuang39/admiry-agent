"""Conversation snapshots (spec 2026-09-30 support-and-feedback §7, D137, D147): one business's conversation as dated
turns -- the consultation, the say of the tools that make or change the plan, the homepage's free text after it,
e-mail both ways, the words put on a support ticket -- and a plan version with its cards. A ticket (support.py) and a
vote (feedback.py) each store one when they are made, so what the owner saw survives later changes and the e-mail purge
(D62). Read-only, and it never raises: a failure is {"error": …} and a log line, inside a savepoint, so the caller's
transaction goes on."""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone

import psycopg

from . import business, consult, landing, plans, site_install
from .util import iso

log = logging.getLogger(__name__)
MAX_TURNS = 200
MAX_TEXT = 2000
PURGED = "[purged]"
SAID = (
    "recommend",
    "adjust",
    "revise_plan",
    "create_plan",
    "change_plan",
    "contact_support",
    "update_business",  # the reply to the owner's "not my business" is its say (final review M4); `_via` names the actor
)  # tools whose `say` is a turn
SHOWN_MAIL = ("received", "answered", "queued", "sent")  # outcomes of messages that reached someone
_SEQ = itertools.count()  # a stable order among turns with the same time


def build(
    conn: psycopg.Connection,
    business_id: str | None,
    *,
    plan_id: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """The snapshot: {built_at, business, turns, dropped, plan, error}. `plan_id` cuts the turns at that plan version
    (the call that made it included) and shows that version; without it, every turn and the latest plan. `thread_id`
    adds an e-mail thread that has no business yet. The newest MAX_TURNS turns, each at most MAX_TEXT characters."""
    built_at = datetime.now(timezone.utc)
    try:
        with conn.transaction():  # a savepoint inside the caller's transaction: a failure here never aborts it
            snap = _build(conn, business_id, plan_id, thread_id)
    except Exception as e:  # a snapshot never blocks the ticket or the vote it belongs to
        log.warning("snapshot of business %s failed", business_id, exc_info=True)
        return {"built_at": iso(built_at), "error": type(e).__name__}
    return {"built_at": iso(built_at), **snap, "error": None}


def _build(conn, business_id: str | None, plan_id: str | None, thread_id: str | None) -> dict:
    plan = None
    if business_id:
        plan = plans.get(conn, business_id, plan_id) if plan_id else plans.latest(conn, business_id)
    turns: list[dict] = []
    if business_id:
        turns += _consultations(conn, business_id)
        turns += _tool_turns(conn, business_id)
        turns += _ticket_turns(conn, business_id)
    turns += _mail_turns(conn, business_id, thread_id)
    if plan_id and plan:
        until = datetime.fromisoformat(plan["created_at"])  # the plan row and its tool's audit row share now()
        turns = [t for t in turns if t["at"] <= until]
    turns.sort(key=lambda t: (t["at"], t["seq"]))
    dropped = max(0, len(turns) - MAX_TURNS)
    return {
        "business": _business(conn, business_id),
        "turns": [_public(t) for t in turns[dropped:]],
        "dropped": dropped,
        "plan": _plan(conn, plan),
    }


def _turn(who: str, via: str, text, at: datetime, *, truncated: bool = False) -> dict:
    text = str(text or "")
    return {
        "who": who,
        "via": via,
        "text": text[:MAX_TEXT],
        "at": at,
        "truncated": truncated or len(text) > MAX_TEXT,
        "seq": next(_SEQ),
    }


def _public(t: dict) -> dict:
    return {"who": t["who"], "via": t["via"], "text": t["text"], "at": iso(t["at"]), "truncated": t["truncated"]}


def _via(row: dict) -> str:
    """Where a keyed call came from: the e-mail channel's per-message keys, the homepage's own (anonymous, a draft, or
    the business key labelled `website` a claim upgraded), else an outside assistant."""
    if row.get("source") == "email":
        return "email"
    if row.get("actor_class") in ("anonymous", "draft") or row.get("label") in ("website", "draft"):
        return "home"
    return "mcp"


def _consultations(conn, business_id: str) -> list[dict]:
    """Each consultation as one block at the time of its `start` call (§7): the opening words, start's say, then every
    question the owner answered, in the tree's order, with their pick (its label) or their own words. The say of the
    `answer` calls is not repeated (it only presents the next question); pre-answered known facts are not the owner's.
    start's say already ends with the first question (for an assistant that shows only text), so that question is not
    given a turn of its own: each question appears once, and one nobody answered still shows in start's say."""
    out = []
    for s in conn.execute(
        "SELECT id, tree, opening_text, known_facts, answers, options, created_at FROM consult_session "
        "WHERE business_id = %s ORDER BY created_at",
        (business_id,),
    ).fetchall():
        start = conn.execute(
            "SELECT a.ts, a.result, a.actor_class, k.label, k.source FROM agent_action a "
            "LEFT JOIN api_key k ON k.id = a.api_key_id WHERE a.business_id = %s AND a.tool = 'start' AND a.ok "
            "AND a.result->'data'->>'session_id' = %s ORDER BY a.ts LIMIT 1",
            (business_id, s["id"]),
        ).fetchone()
        at, via = (start["ts"], _via(start)) if start else (s["created_at"], "home")
        out.append(_turn("owner", via, s["opening_text"], at))
        said = ""
        if start and isinstance(start["result"], dict) and start["result"].get("say"):
            said = start["result"]["say"]
            out.append(_turn("admiry", via, said, at))
        # the known facts too: a question's words can depend on them (the website question for a site start could not
        # open, consult.prompt_for: A3p-5 batch 2 Task 0, ruling R0-i)
        session = {"tree": s["tree"], "answers": s["answers"], "options": s["options"], "known_facts": s["known_facts"]}
        for q in consult.tree_questions(consult.load_tree(s["tree"])):  # the website, the name, the services first
            a = s["answers"].get(q["id"])
            if not isinstance(a, dict) or a.get("source") == "known_fact":
                continue
            options = s["options"].get(q["id"], q["options"])
            asked = f"{consult.prompt_for(q, session)} Options: {consult.option_labels(options)}"
            if asked not in said:
                out.append(_turn("admiry", via, asked, at))
            out.append(_turn("owner", via, _answer_words(a, options), at))
    return out


def _answer_words(a: dict, options: list[dict]) -> str:
    """What the owner answered: their own words when they typed, else the labels of what they picked."""
    if a.get("source") == "other":
        return str(a.get("raw") or a.get("note") or "")
    picks = a.get("choice")
    ids = picks if isinstance(picks, list) else [picks]
    labels = [o["label"] for o in options if o["id"] in ids]
    return ", ".join(labels) or str(a.get("raw") or "")


def _tool_turns(conn, business_id: str) -> list[dict]:
    """The say of the tools that make, change or hand over the plan, and the homepage's free text after it: web.say
    rows keep the owner's words in result.text since D147 (older rows only the 80-character argument, marked
    truncated). A web.say whose words a ticket took (result.ticket_id, home_api._support_env) is left out: they are
    the ticket's own turn (_ticket_turns). One whose read asked for a person but opened no ticket -- the anonymous
    limit, say -- keeps its words here: nothing else holds them.
    A web.turn row (the harness, K6 slice 1) reads the same way, with Admiry's `say` as its own turn; one whose
    dispatch was `answer` is left out, the consultation's turn holds those words."""
    out = []
    for r in conn.execute(
        "SELECT a.tool, a.ts, a.ok, a.args_redacted, a.result, a.actor_class, k.label, k.source FROM agent_action a "
        "LEFT JOIN api_key k ON k.id = a.api_key_id WHERE a.business_id = %s "
        "AND ((a.ok AND a.tool = ANY(%s)) OR a.tool IN ('web.say', 'web.turn')) ORDER BY a.ts, a.id",
        (business_id, list(SAID)),
    ).fetchall():
        result = r["result"] if isinstance(r["result"], dict) else {}
        if r["tool"] in ("web.say", "web.turn"):
            if result.get("ticket_id") or (r["tool"] == "web.turn" and result.get("dispatch") == "answer"):
                continue  # the ticket's turn, or the consultation's own (K6 slice 1 D7), holds those words
            text = result.get("text")
            argument = str((r["args_redacted"] or {}).get("text") or "")
            out.append(_turn("owner", _via(r), text or argument, r["ts"], truncated=not text))
            if r["tool"] == "web.turn" and result.get("say"):  # the harness's reply: Admiry's own turn
                out.append(_turn("admiry", _via(r), result["say"], r["ts"]))
        elif result.get("say") and "replay_of" not in result:
            out.append(_turn("admiry", _via(r), result["say"], r["ts"]))
    return out


def _ticket_turns(conn, business_id: str) -> list[dict]:
    """The words the owner (or their assistant) put on a support ticket outside e-mail -- e-mail's own rows carry the
    rest -- with an assistant's excerpt (contact_support's `context`) just before them."""
    out = []
    for e in conn.execute(
        "SELECT e.via, e.body, e.data, e.created_at FROM ticket_event e JOIN support_ticket t ON t.id = e.ticket_id "
        "WHERE t.business_id = %s AND e.kind = 'owner_words' AND e.via IS DISTINCT FROM 'email' ORDER BY e.created_at",
        (business_id,),
    ).fetchall():
        context = (e["data"] or {}).get("context")
        if context:
            out.append(_turn("assistant", e["via"] or "mcp", context, e["created_at"]))
        out.append(_turn("owner", e["via"] or "home", e["body"], e["created_at"]))
    return out


def _mail_turns(conn, business_id: str | None, thread_id: str | None) -> list[dict]:
    """E-mail both ways on the business's threads (and `thread_id`, a thread with no business yet), only messages that
    reached someone; a message whose text D62 already forgot reads PURGED."""
    out = []
    for m in conn.execute(
        "SELECT m.direction, m.new_text, m.sent_by, m.created_at FROM email_message m "
        "JOIN email_thread t ON t.id = m.thread_id WHERE (t.business_id = %s OR t.id = %s) AND m.outcome = ANY(%s) "
        "ORDER BY m.created_at",
        (business_id, thread_id, list(SHOWN_MAIL)),
    ).fetchall():
        who = "owner" if m["direction"] == "in" else ("ops" if m["sent_by"] == "ops" else "admiry")
        text = m["new_text"] if m["new_text"] is not None else PURGED
        out.append(_turn(who, "email", text, m["created_at"]))
    return out


def _business(conn, business_id: str | None) -> dict | None:
    if not business_id:
        return None
    biz = business.row(conn, business_id)
    knowledge = (business.latest_knowledge(conn, business_id) or (None, {}))[1]
    return {"id": business_id, "name": knowledge.get("name"), "claim_state": biz["claim_state"] if biz else None}


def _plan(conn, plan: dict | None) -> dict | None:
    """The plan version as the owner saw it: its lines and sections, and the cards when it came from a consultation."""
    if plan is None:
        return None
    out = {k: plan[k] for k in ("id", "version", "status", "lines", "sections")}
    if "consultation" in plan["sections"]:
        knowledge = (business.latest_knowledge(conn, plan["business_id"]) or (None, {}))[1]
        name = plans.name_for(knowledge) or plans.address_for(knowledge)  # as every card builder names the place
        out["cards"] = plans.cards(
            {**plan, "business_name": name},
            final_host=landing.final_host(conn, plan["business_id"]),
            website=site_install.row(conn, plan["business_id"]),
        )
    return out
