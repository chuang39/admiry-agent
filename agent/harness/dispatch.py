"""What code does with a Turn's one dispatch (spec 2026-10-10 K6 slice 1 §4.4, §5), and the outcomes `turn.run` hands
home_api: a Reroute into a registered command (`answer`, the reading handed through consult.with_reading; `recommend`;
`update_business`), a StartOver, or a finished envelope (`again`: Admiry's words, then the open question as `answer`
asks it, or at ready the build action). Every operation here is non-dangerous (operations.SAFE for the commands,
operations.HARNESS_SAFE for the harness's own); nothing here builds a response, touches a cookie or calls a model.
Admiry's fixed sentences are here (D8): the model's `say` reaches the owner for explain, ask, smalltalk and unclear
alone, and only through `safe_say` (I5)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import business, consult, copy, envelope, listings, support
from . import fast

CONFIDENCE = 0.6  # below it the turn is unclear (mail_dispatch.CONFIDENCE)
START_OVER_CONFIDENCE = 0.8  # a model-read start over below it asks back (§4.4); the fast path's phrases never ask
# The intents that run a registered command, and which (operations.SAFE); the rest are operations.HARNESS_SAFE
COMMAND_OF = {
    "answer": "answer",
    "build": "recommend",
    "budget": "set_goal",
    "support": "contact_support",
    "not_mine": "update_business",
}
READY_SAY = "Say the word and I will build the plan."  # home_api.READY_SAY (a test keeps them one)
RECOMMEND = envelope.actions({"id": "recommend", "label": "Build my plan", "tool": "recommend", "primary": True})
BUDGET_LATER = (
    "The budget is set once the plan is drawn, right after these questions. If you have a monthly amount in mind, "
    "tell me now and I will plan within it."
)
BUDGET_NOTED = "Noted: ${amount:,.0f} a month. I will plan within it."
BUDGET_RANGE = f"A monthly budget is an amount up to ${business.BUDGET_MAX:,.0f}. Which amount?"
CONFIRM_START_OVER = "Start over from the top? The answers so far would go. Say yes and I will."
NOT_READY = "A few questions first, then the plan."
SKIP_SAY = "No problem. The closest option is fine."
SUPPORT_OFF_SAY = "I cannot hand this to a person here. Tell me what is unclear and I will do my best."
NOT_LINKED_SAY = "No Google Maps place is linked to the business, so there is nothing to unlink."
UNCLEAR_SAY = "I did not quite get that. In a few words, what would you like?"
SAY_MAX = 300  # the most of the model's say the owner reads (safe_say)
FALLBACK_SAY = {  # when the model's say is empty, or not safe_say's, for an intent the owner reads it for
    "explain": "This one helps me plan the ads around what matters to you.",
    "ask": "That comes with the plan.",
    "smalltalk": "Glad to help.",
}


@dataclass(frozen=True)
class Reroute:
    """home_api runs this registered command for the turn; `reading` is handed to `answer` (consult.with_reading)."""

    action: str
    body: dict
    reading: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StartOver:
    fresh: bool  # a draft retired: the cookies go with the response; a claimed business: they stay


def name_of(outcome) -> str:
    """What the audit row says was dispatched."""
    if isinstance(outcome, Reroute):
        return outcome.action
    if isinstance(outcome, StartOver):
        return "start_over"
    return "wall" if outcome.get("wall") else "error" if "error" in outcome else "say"


def run(
    conn,
    principal,
    biz: dict,
    session: dict,
    text: str,
    turn,
    *,
    support_on: bool,
    linked: bool,
    idempotency_key,
    open_q: dict | None = None,
):
    """The one dispatch for `turn` (D1: the fields of its intent alone are read). `open_q` is the question the words
    answer (turn.run's context.open_question: one asked again, open or at ready, C1); None, the first unanswered one."""
    if open_q is None:
        nxt = consult.next_question(session)
        open_q = nxt["question"] if nxt else None
    intent = turn.intent if turn.confidence >= CONFIDENCE else "unclear"
    say = (turn.say or "").strip() if intent == turn.intent else ""
    if intent == "answer":
        if open_q is None:
            return again(session, "", open_q)
        return Reroute("answer", {"answers": {open_q["id"]: {"other": text}}}, {open_q["id"]: turn.reading()})
    if intent == "go_to":
        if turn.question_id not in consult.may_go_to(session):
            return again(session, UNCLEAR_SAY, open_q)
        q = consult.question_for(session, turn.question_id)
        return envelope.make({"session_id": session["id"]}, say=f"Of course. {consult.question_sentence(q)}", next=q)
    if intent == "start_over":
        if turn.confidence < START_OVER_CONFIDENCE:
            # asked alone (M2): no question card and no build action from the server (the page may still draw its Build
            # button at ready); the yes or no is the next turn
            return envelope.make({"session_id": session["id"]}, say=CONFIRM_START_OVER)
        return start_over(conn, biz)
    if intent == "budget":
        return budget(conn, biz, session, turn.monthly_usd, open_q)
    if intent == "build":
        if session["status"] != "ready":
            return again(session, NOT_READY, open_q)
        return Reroute("recommend", {})
    if intent == "skip":
        if open_q is None:
            return again(session, "", open_q)
        q = next(x for x in consult.questions(session) if x["id"] == open_q["id"])
        opt = fast.skip_option(q, open_q["options"])
        if opt is not None:
            return Reroute("answer", {"answers": {q["id"]: {"choice": opt}}})
        if q["input"] in ("text", "address"):
            return Reroute("answer", {"answers": {q["id"]: {"other": text}}}, {q["id"]: consult.SKIP})
        return again(session, SKIP_SAY, open_q)
    if intent == "support":
        if not support_on:
            return again(session, SUPPORT_OFF_SAY, open_q)
        return ticket(conn, principal, session, text, turn, open_q)
    if intent == "not_mine":
        if not linked:
            return again(session, NOT_LINKED_SAY, open_q)
        return Reroute("update_business", {"idempotency_key": idempotency_key, "then": listings.LINK_AGAIN})
    return again(session, safe_say(intent, say), open_q)


def safe_say(intent: str, say: str) -> str:
    """The model's `say` as the owner may read it (I5): only with no advertising jargon (copy.jargon), no digit -- no
    number the model made up -- and at most SAY_MAX characters; else the intent's fixed sentence (unclear's:
    UNCLEAR_SAY)."""
    say = (say or "").strip()
    if say and copy.jargon(say) is None and re.search(r"\d", say) is None and len(say) <= SAY_MAX:
        return say
    return FALLBACK_SAY.get(intent, UNCLEAR_SAY)


def again(session: dict, say: str, open_q: dict | None = None) -> dict:
    """`say`, then the open question again as `answer` asks it (the sentence, the envelope): `open_q` when the page
    shows it asked again (`asked`) -- or, at ready with none asked again, what to do: the build action."""
    nxt = asked(session, open_q)
    if nxt is None:
        return envelope.make({"session_id": session["id"]}, say=f"{say} {READY_SAY}".strip(), next=RECOMMEND)
    return envelope.make({"session_id": session["id"]}, say=f"{say} {consult.question_sentence(nxt)}".strip(), next=nxt)


def asked(session: dict, open_q: dict | None) -> dict | None:
    """The question to ask again (Ruling 19): `open_q` -- run()'s, one the page shows asked again (go_to, C1) -- as
    question_for asks it when it is on the menu, else the first unanswered question; None at ready with none asked
    again."""
    if open_q is not None and open_q["id"] in consult.may_go_to(session):
        return consult.question_for(session, open_q["id"])
    return consult.next_question(session)


def start_over(conn, biz: dict) -> StartOver:
    """§5: a draft nobody owns retired (business.retire_draft: expired, keys revoked; home_api deletes the cookies),
    a business with an owner -- claimed, or a released claim (claim-expire puts it back to draft but keeps its owner
    e-mail and its upgraded key, I1) -- has its open or ready consultation superseded (the next words run `start` on
    it). Nothing else is touched."""
    if biz["claim_state"] == "draft" and not biz.get("owner_email"):
        business.retire_draft(conn, biz["id"])
        return StartOver(fresh=True)
    consult.supersede_open(conn, biz["id"])
    return StartOver(fresh=False)


def budget(conn, biz: dict, session: dict, amount: float | None, open_q: dict | None = None) -> dict:
    """R-K6-4: an amount is recorded on the goal as the owner's limit, the rest of the latest goal kept (D6); none,
    Admiry says the budget comes with the plan and draws no card (the next words are read with this ask in recent)."""
    if amount is None:
        return envelope.make({"session_id": session["id"]}, say=BUDGET_LATER)
    if not (0 < amount <= business.BUDGET_MAX):
        return again(session, BUDGET_RANGE, open_q)
    held = business.latest_goal(conn, biz["id"]) or {}
    business.set_goal(
        conn,
        biz["id"],
        target=held.get("target"),
        monthly_limit_usd=float(amount),
        focus_services=held.get("focus_services") or (),
        limit_source="owner",
    )
    return again(session, BUDGET_NOTED.format(amount=amount), open_q)


def ticket(conn, principal, session: dict, text: str, turn, open_q: dict | None = None) -> dict:
    """A turn that asks for a person with support on (D9): the business's ticket opened or appended as
    home_api._support_env and tools_consult._support_answer open one; nothing recorded for the question; the
    acknowledgement, then the open question again -- unless the acknowledgement's next is the sign-in link, which
    stands (an anonymous visitor signs in so a reply can reach them)."""
    try:
        out = support.open_or_append(
            conn,
            business_id=principal.business_id,
            source=support.source_of(principal.key),
            reason=turn.support if turn.support != "none" else "help",
            words=text,
            summary=turn.support_summary or text,
            actor=principal.actor,
            ip=principal.ip,
            draft=principal.key.cls == "draft",
        )
    except support.Limited as e:
        return {"error": str(e)}
    env = support.envelope_for(out, planned=False)
    nxt = asked(session, open_q)
    if nxt is None or env["next"]["kind"] != "none":
        return env
    return {**env, "say": f"{env['say']} {consult.question_sentence(nxt)}", "next": nxt}
