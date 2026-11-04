"""What Admiry's admins read and do with the refusal rules (spec 2026-10-12 §2.5; Part B phase 1): the proposed rules
with their evidence (approve as warn or block, reject), the active rules (change the level, retire), a rule written
by hand (active at once: a person wrote it), and the last daily summaries. Shared by the admin page (ops_web.py
/ops/rules) and the admin MCP (ops_mcp.py rules_list, rule_decide), which check who the admin is
(AGENT_ADMIN_EMAILS); each decision is an audit row in their name (admin:<email>, class platform), committed with the
change (plan D16). A rule's `why` is owner words: checked here as every owner sentence is (copy.jargon)."""

from __future__ import annotations

import psycopg
from fastmcp.exceptions import ToolError

from . import audit, copy, db, local_rules, refusal_summary

ACTIONS = ("approve", "reject", "retire", "level")
SUMMARY_DAYS = 14
NOT_FOUND = "No such rule."
BAD_ACTION = "No such action."
BAD_STATUS = "A status is proposed, active, rejected or retired."
NEEDS_LEVEL = "Choose warn or block."
NOT_PROPOSED = "This rule is not waiting for a decision."
NOT_ACTIVE = "This rule is not active."
NOOP_LEVEL = "This rule is already at that level."
NEEDS_WHY = "Write why, in the owner's words."
WHY_JARGON = "Why must read as an owner would: no advertising terms."
EXISTS = "A rule like this already exists."
WHY_MAX = 160
TOPIC_MAX = 60  # capped like other free text an admin types in (ops_rules final review, minor 4)
_AFTER = {
    "approve": local_rules.ACTIVE,
    "reject": local_rules.REJECTED,
    "retire": local_rules.RETIRED,
    "level": local_rules.ACTIVE,
}


def rules(conn: psycopg.Connection, status: str) -> list[dict]:
    """The rules of one status, oldest first (local_rules.view: dates as text, a pattern's tell)."""
    if status not in local_rules.STATUSES:
        raise ToolError(BAD_STATUS)
    rows = conn.execute(
        f"SELECT {local_rules.COLS} FROM refusal_rule WHERE status = %s ORDER BY created_at, id", (status,)
    ).fetchall()
    return [local_rules.view(r) for r in rows]


def page_view(conn: psycopg.Connection) -> dict:
    """The page: the proposed rules, the active ones, the last SUMMARY_DAYS summaries, and the form's choices."""
    return {
        "proposed": rules(conn, local_rules.PROPOSED),
        "active": rules(conn, local_rules.ACTIVE),
        "summaries": refusal_summary.latest(conn, SUMMARY_DAYS),
        "shapes": [{"id": s.id, "tell": s.tell} for s in local_rules.SHAPES.values()],
        "kinds": local_rules.KINDS,
        "applies": local_rules.APPLIES,
        "levels": local_rules.LEVELS,
    }


def _why(text: object, *, required: bool) -> str | None:
    """A why in owner words: collapsed, at most WHY_MAX; None for none when it is optional."""
    said = " ".join(str(text).split()) if isinstance(text, str) else ""
    if not said:
        if required:
            raise ToolError(NEEDS_WHY)
        return None
    if copy.jargon(said):
        raise ToolError(WHY_JARGON)
    return said[:WHY_MAX]


def _audit(conn: psycopg.Connection, *, tool: str, admin: str, args: dict, result: dict, ip: str | None) -> None:
    audit.record(
        conn,
        tool=tool,
        args=args,
        result=result,
        ok=True,
        business_id=None,
        actor=f"admin:{admin}",
        actor_class="platform",
        api_key_id=None,
        ip=ip,
    )


def decide(
    conn: psycopg.Connection,
    rule_id: str,
    *,
    admin: str,
    action: str,
    level: str | None = None,
    why: str | None = None,
    ip: str | None = None,
) -> dict:
    """One decision on one rule, with its audit row (admin.rule_decide), in one transaction: `approve` (with `level`)
    or `reject` a proposed rule; `retire` an active one, or change its `level` (action `level`). `why` replaces the
    owner-words sentence when given, on `approve` and `level` alone -- a `reject` or a `retire` never rewrites it.
    A refusal -- no such rule, a wrong status, no level, a `level` action to the level the rule already has, a why
    with jargon -- is a ToolError with its sentence and nothing written. The rule as the page shows it."""
    if action not in ACTIONS:
        raise ToolError(BAD_ACTION)
    if action in ("approve", "level") and level not in local_rules.LEVELS:
        raise ToolError(NEEDS_LEVEL)
    said = _why(why, required=False)
    with conn.transaction():
        row = conn.execute("SELECT status, level FROM refusal_rule WHERE id = %s FOR UPDATE", (rule_id,)).fetchone()
        if row is None:
            raise ToolError(NOT_FOUND)
        if action in ("approve", "reject") and row["status"] != local_rules.PROPOSED:
            raise ToolError(NOT_PROPOSED)
        if action in ("retire", "level") and row["status"] != local_rules.ACTIVE:
            raise ToolError(NOT_ACTIVE)
        if action == "level" and level == row["level"]:  # a no-op decision: refused before any row is touched
            raise ToolError(NOOP_LEVEL)
        new_level = level if action in ("approve", "level") else row["level"]
        new_why = said if action in ("approve", "level") else None
        out = conn.execute(
            "UPDATE refusal_rule SET status = %s, level = %s, why = coalesce(%s, why), decided_by = %s, "
            f"decided_at = now() WHERE id = %s RETURNING {local_rules.COLS}",
            (_AFTER[action], new_level, new_why, f"admin:{admin}", rule_id),
        ).fetchone()
        _audit(
            conn,
            tool="admin.rule_decide",
            admin=admin,
            args={"rule_id": rule_id, "action": action, "level": level},
            result={"status": out["status"], "level": out["level"]},
            ip=ip,
        )
    return local_rules.view(out)


def add(
    conn: psycopg.Connection,
    *,
    admin: str,
    kind: str,
    value: object,
    applies_to: str,
    level: str,
    why: object,
    topic: object = None,
    ip: str | None = None,
) -> dict:
    """A rule written by hand: active at once at the level written (a person wrote it: that is the approval), with
    its audit row (admin.rule_add), in one transaction. The value through local_rules.validate; a key that already
    lives (proposed or active) is EXISTS. The rule as the page shows it."""
    try:
        stored = local_rules.validate(kind, value)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if applies_to not in local_rules.APPLIES:
        raise ToolError(local_rules.BAD_APPLIES)
    if level not in local_rules.LEVELS:
        raise ToolError(local_rules.BAD_LEVEL)
    said = _why(why, required=True)
    topic_ = " ".join(str(topic).split()).upper()[:TOPIC_MAX] if isinstance(topic, str) and topic.strip() else None
    rule_id = db.new_id("rr")
    try:
        with conn.transaction():
            out = conn.execute(
                "INSERT INTO refusal_rule (id, kind, applies_to, value, topic, why, level, status, proposed_by, "
                "decided_by, decided_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                f"RETURNING {local_rules.COLS}",
                (
                    rule_id,
                    kind,
                    applies_to,
                    stored,
                    topic_,
                    said,
                    level,
                    local_rules.ACTIVE,
                    f"admin:{admin}",
                    f"admin:{admin}",
                ),
            ).fetchone()
            _audit(
                conn,
                tool="admin.rule_add",
                admin=admin,
                args={"kind": kind, "applies_to": applies_to, "value": stored, "level": level},
                result={"rule_id": rule_id},
                ip=ip,
            )
    except psycopg.errors.UniqueViolation as e:
        raise ToolError(EXISTS) from e
    return local_rules.view(out)
