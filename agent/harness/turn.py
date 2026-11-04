"""One turn of the home's free text while a consultation is open or ready (spec 2026-10-10 K6 slice 1 §4): the code
fast path, else the material and one Turn call, then code runs the dispatch (dispatch.py); one web.turn audit row.
`run` answers home_api with what to do (dispatch.Reroute, dispatch.StartOver, or a finished dict) and never builds a
response: home_api keeps the cookies, the wall's reply and the visitor block."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from .. import audit, auth, consult, llm, transcript
from . import context, dispatch, fast, skills
from .dispatch import Reroute, StartOver  # noqa: F401 - home_api reads the outcomes from here

MAX_CHARS = consult.MAX_OTHER  # the composer's words, as `answer` reads them
EMPTY = "Send a choice or a few words."
TOO_LONG = f"Keep it under {MAX_CHARS} characters."
WORDS_ONLY = ("website", "business_name")  # questions whose words never went to a model (§4.1 item 4), with the place


def run(
    conn,
    settings,
    rec,
    biz: dict,
    session: dict,
    text: str,
    *,
    key: str,
    ip: str,
    wall: bool,
    support_on: bool,
    linked: bool,
    idempotency_key: str | None = None,
    question_id: str | None = None,
):
    """Reroute, StartOver, or a dict: an envelope, {"error": sentence}, or {"wall": True} (the wall's reply, D10).
    `question_id` is the question card the page shows (C1): one asked again (go_to) is the question the words answer.
    auth.RateLimited propagates (home_api answers 429)."""
    with auth.with_headers({"Authorization": f"Bearer {key}", "X-Forwarded-For": ip}):
        try:
            principal = auth.require(conn, "manage")
        except auth.Denied as e:
            return {"error": str(e)}
        text = (text or "").strip()
        if not text:
            return {"error": EMPTY}
        if len(text) > MAX_CHARS:
            return {"error": TOO_LONG}
        open_q = context.open_question(session, question_id)
        # §4.1 the fast path: no model call, no material
        if fast.says_start_over(text):
            outcome = dispatch.start_over(conn, biz)
            _record(
                conn,
                principal,
                text,
                {"intent": "start_over", "dispatch": "start_over", "fast": True, "counted": False},
                ok=True,
            )
            return outcome
        if (site := consult.site_reanswer(session, text)) is not None:  # an address in the words (spec 2026-10-12 §3.4)
            _record(
                conn,
                principal,
                text,
                {"intent": "answer", "dispatch": "answer", "fast": True, "counted": False, "website": site},
                ok=True,
            )
            return Reroute("answer", {"answers": {consult.WEBSITE: {"other": text}}})
        if open_q is not None and _words_only(session, open_q, text):
            _record(
                conn,
                principal,
                text,
                {"intent": "answer", "dispatch": "answer", "fast": True, "counted": False},
                ok=True,
            )
            return Reroute("answer", {"answers": {open_q["id"]: {"other": text}}})
        if wall:
            return {"wall": True}
        # §4.2, §4.3 the one call
        try:
            auth.check_llm_cap(conn, principal)
            gen = llm.generator()
        except (ToolError, llm.LLMError) as e:
            _record(conn, principal, text, {"error": str(e), "fast": False, "counted": True}, ok=False)
            return {"error": str(e)}
        material = context.load(conn, biz["id"], session, open_question=open_q)
        try:
            turn = gen.read_turn(material, text, skill=skills.load("before_plan"))
        except llm.LLMError as e:
            _record(conn, principal, text, {"error": str(e), "fast": False, "counted": True}, ok=False)
            return {"error": str(e)}
        # §4.4 the dispatch
        outcome = dispatch.run(
            conn,
            principal,
            biz,
            session,
            text,
            turn,
            support_on=support_on,
            linked=linked,
            idempotency_key=idempotency_key,
            open_q=open_q,
        )
        name = dispatch.name_of(outcome)
        result = {
            "intent": turn.intent,
            "confidence": turn.confidence,
            "dispatch": name,
            "say": outcome.get("say") if isinstance(outcome, dict) else None,
            "fast": False,
            # D7 (Ruling 8): a {choice} reroute counts on this row; a words reroute counts on answer's free_text row
            "counted": name != "answer" or not any("other" in v for v in outcome.body["answers"].values()),
        }
        if isinstance(outcome, dict):
            # Ruling 6 (controller): a ticket opened by this dispatch leaves its words to the ticket's own turn, as
            # home_api._support_env marks a web.say row that did the same (support.public exposes the id)
            ticket_id = ((outcome.get("data") or {}).get("support") or {}).get("ticket_id")
            if ticket_id:
                result["ticket_id"] = ticket_id
        _record(conn, principal, text, result, ok=True)
        return outcome


def _words_only(session: dict, open_q: dict, text: str) -> bool:
    """§4.1 item 4 and D5: the website, the name and the Google Maps place read their words in code, as today; a text
    question's "rather not say" is parse's own skip."""
    if open_q["id"] in WORDS_ONLY or open_q["input"] == "website":
        return True
    q = next((x for x in consult.questions(session) if x["id"] == open_q["id"]), None)
    if q is not None and q.get("options_from") == "listing":
        return True
    return open_q["input"] == "text" and consult.skips(text)


def _record(conn, principal, text: str, result: dict, *, ok: bool) -> None:
    """One web.turn row (§8): the words whole in result.text (the audit clips the argument), the intent, the dispatch,
    Admiry's say, `fast` for a turn that spent no call, `counted` for one that spends one of the day's calls."""
    audit.record(
        conn,
        tool="web.turn",
        args={"text": text},
        result={**result, "text": text[: transcript.MAX_TEXT]},
        ok=ok,
        business_id=principal.business_id,
        actor=principal.actor,
        actor_class=principal.key.cls,
        api_key_id=principal.key.id,
        ip=principal.ip,
    )
