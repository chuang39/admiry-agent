"""Every refusal Google answers a validateOnly or a launch with, recorded (spec 2026-10-11 §6): one `ads_refusal` row
per refusal detail (channels/google_search/client.RefusalDetail) with its class (channels/google_search/refusals.py)
and what Admiry did -- `dropped` (the line left out of every later Google build of the business, by its exact text,
whichever version it was refused in: `dropped` reads it back for adapter.build -- ruling L2, final review),
`returned` (the owner was asked), `held` (a person looks). The owner's words for what Admiry removed (spec §5.1:
`changes`, `line`, `say`) name the line in the owner's own text and a plain why -- a table by policy topic and class,
never Google's own words or codes -- for the drops in effect for a version. Scoped by business everywhere it is
read: an owner's text is never shown to another owner. Part B's daily summary will read the table by day."""

from __future__ import annotations

import logging

import psycopg

from . import business, config, db, local_rules, plans
from .channels.google_search import build as google_build
from .channels.google_search.client import RefusalDetail
from .util import iso

log = logging.getLogger(__name__)
CLASSES = ("ad_text", "keyword", "destination", "business", "other")
ACTIONS = ("dropped", "returned", "held")
DROPPED, RETURNED, HELD = ACTIONS
GOOGLE_SEARCH = "google_search"  # the channel whose builds leave a dropped line out (the only one the Reviewer checks)
COLS = "id, business_id, plan_id, channel, review_id, code, topic, type, field, kind, text, class, action, created_at"
# the owner's words (spec §5.1): what was removed, by what Google pointed at; why, by the topic, else by the class
WHAT = {"headline": "the headline", "description": "the description", "keyword": "the search phrase"}
WHAT_ELSE = {"headline": "a headline", "description": "a description", "keyword": "a search phrase"}
WHY_BY_TOPIC = {
    "TRADEMARKS_IN_AD_TEXT": "a name Google treats as a trademark",
    "TRADEMARKS": "a name Google treats as a trademark",
    "TRADEMARK": "a name Google treats as a trademark",
}
WHY_BY_CLASS = {
    "ad_text": "Google's rules for ad text",
    "keyword": "Google's rules for search phrases",
    "destination": "Google's rules for the page the ads send people to",
    "business": "something Google needs to know about the business",
    "other": "Google's rules",
}
INTRO = "Google refused a part of your ads; Admiry removed it, and the rest of your ads stay as you agreed."
SAY_ONE = "Admiry removed one part of your ads that Google refused ({why}); the rest stay as you agreed."
SAY_MANY = "Admiry removed {n} parts of your ads that Google refused; the rest stay as you agreed."


def record(
    conn: psycopg.Connection,
    *,
    business_id: str,
    plan_id: str,
    channel: str,
    review_id: str | None,
    found: list[tuple[RefusalDetail, str, str]],
) -> list[str]:
    """One row per (detail, class, action), in order, whole or not at all (Ruling E1): every (class, action) in
    `found` is checked before anything is written, so a later tuple's bad class or action never leaves an earlier
    tuple's row behind; the writes then all go in inside one transaction (a savepoint when the caller already has
    one). `created_at` is `clock_timestamp()`, not the column's own `now()` default -- `now()` is frozen to the
    transaction's start, so rows from the same call would otherwise all land on one timestamp and `changes` /
    `for_review` (`ORDER BY created_at, id`) would stop reflecting the order `found` was given in. The ids.
    ValueError for a class or an action the table does not take (a programming error, never Google's doing)."""
    for _detail, cls, action in found:
        if cls not in CLASSES or action not in ACTIONS:
            raise ValueError(
                f"ads_refusal takes a class of {CLASSES} and an action of {ACTIONS}, not {cls!r}/{action!r}"
            )
    ids: list[str] = []
    with conn.transaction():
        for detail, cls, action in found:
            row_id = db.new_id("ar")
            conn.execute(
                "INSERT INTO ads_refusal (id, business_id, plan_id, channel, review_id, code, topic, type, field, "
                "kind, text, class, action, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "clock_timestamp())",
                (
                    row_id,
                    business_id,
                    plan_id,
                    channel,
                    review_id,
                    detail.code,
                    detail.topic,
                    detail.type,
                    detail.field,
                    detail.kind,
                    detail.text,
                    cls,
                    action,
                ),
            )
            ids.append(row_id)
    return ids


def dropped(
    conn: psycopg.Connection,
    business_id: str,
    *,
    channel: str = GOOGLE_SEARCH,
    settings: config.Settings | None = None,
) -> google_build.Dropped:
    """What every Google build of the business leaves out (spec 2026-10-11 §5 "How a drop is kept"; plan D3, ruling
    L2): a line Google refused, by its exact text -- the texts of every row Admiry dropped for the business on
    `channel`, whichever version it was refused in, by kind. Business-wide, never one version's: a later version that
    carries the line unchanged never sends it again (its update build, a page move, the Reviewer's validateOnly); a
    line the owner rewrote is new text, and goes. Given `settings` (every build and the Reviewer), the active block
    rules ride on it too (spec 2026-10-12 §2.3, plan D3: local_rules.block_rules -- none while AGENT_REFUSAL_RULES is
    off); without them, the exact texts alone (what `changes` and `flagged` read)."""
    rows = conn.execute(
        "SELECT kind, text FROM ads_refusal WHERE business_id = %s AND channel = %s AND action = %s "
        "AND text IS NOT NULL",
        (business_id, channel, DROPPED),
    ).fetchall()
    by: dict[str, set[str]] = {"headline": set(), "description": set(), "keyword": set()}
    for r in rows:
        if r["kind"] in by:
            by[r["kind"]].add(r["text"])
    out = google_build.Dropped(
        headlines=frozenset(by["headline"]),
        descriptions=frozenset(by["description"]),
        keywords=frozenset(by["keyword"]),
    )
    if settings is not None:
        out = local_rules.with_rules(out, local_rules.block_rules(conn, settings))
    return out


def what(kind: str, text: str | None) -> str:
    """The thing removed, in the owner's own text: 'the headline “…”'; without a text, 'a headline'."""
    if text:
        return f"{WHAT.get(kind, 'the line')} “{text}”"
    return WHAT_ELSE.get(kind, "a line")


def why(cls: str, topic: str | None) -> str:
    """Why Google refused it, in plain words: the topic's sentence when the table knows it, else the class's."""
    return WHY_BY_TOPIC.get((topic or "").upper()) or WHY_BY_CLASS.get(cls) or WHY_BY_CLASS["other"]


def _still_sent(conn: psycopg.Connection, business_id: str, plan_id: str) -> dict[str, frozenset[str]] | None:
    """What the version would send Google Search were nothing left out, by kind: the headlines and descriptions its
    Google Search entry holds, the search phrases its build makes (`google_build.built_keywords`, with the
    business's latest knowledge, as the Reviewer reads it). The entry is chosen exactly the way a build does
    (`google_build.search_channel`, ruling N1, final review) -- {} when the version has no Google Search entry a
    build could actually run (no entry by that name, no consultation, no money on it, or no estimate the ceiling
    can price: a $0 entry sends nothing, so it inherits no earlier version's drop either); None when the version is
    not found or a read fails (logged)."""
    try:
        plan = plans.get(conn, business_id, plan_id)
        if plan is None:
            return None
        sections = plan["sections"]
        ads = sections.get("ads") or {}
        ch = google_build.search_channel(plan, GOOGLE_SEARCH)
        if ch is None:
            return {}
        view = {**plan, "sections": {**sections, "ads": {**ads, "channels": [ch]}}}  # built_keywords reads the first
        knowledge = (business.latest_knowledge(conn, business_id) or (None, {}))[1]
        return {
            "headline": frozenset(ch.get("headlines") or []),
            "description": frozenset(ch.get("descriptions") or []),
            "keyword": google_build.built_keywords(view, knowledge),
        }
    except Exception as e:  # noqa: BLE001 - a page read never raises for this: the version's own drops still show
        log.warning("ads_refusal: what plan %s still sends could not be read: %s", plan_id, type(e).__name__)
        return None


def changes(conn: psycopg.Connection, business_id: str, plan_id: str) -> list[dict]:
    """What Admiry removed from this version's ads after Google refused it (spec §5.1), oldest first: the drops in
    effect for the version (ruling L2, final review) -- its own `dropped` rows, and an earlier version's of the same
    business whose line the version would still send (a headline or a description its Google Search entry still
    holds, a search phrase its build still makes: `_still_sent`), since every Google build of the business leaves
    that line out (`dropped`). A line the owner has since rewritten or removed is not listed; a later version's drop
    is never an earlier version's change. Each {what, why, when} in owner words, one per distinct dropped line (its
    kind and text) at its earliest -- a line two policy topics both blocked is two rows but one change to the owner.
    Only this business's own rows; when the version cannot be read, its own rows alone (never raises for it)."""
    rows = conn.execute(
        "SELECT r.plan_id, r.kind, r.text, r.topic, r.class, r.created_at FROM ads_refusal r "
        "JOIN plan p ON p.id = r.plan_id AND p.business_id = r.business_id "
        "WHERE r.business_id = %(b)s AND r.action = %(a)s AND (r.plan_id = %(p)s OR (r.channel = %(ch)s "
        "AND r.text IS NOT NULL AND p.version < (SELECT version FROM plan WHERE id = %(p)s AND business_id = %(b)s))) "
        "ORDER BY r.created_at, r.id",
        {"b": business_id, "a": DROPPED, "p": plan_id, "ch": GOOGLE_SEARCH},
    ).fetchall()
    earlier = any(r["plan_id"] != plan_id for r in rows)
    sent = (_still_sent(conn, business_id, plan_id) if earlier else None) or {}
    seen: set[tuple[str, str | None]] = set()
    out: list[dict] = []
    for r in rows:
        key = (r["kind"], r["text"])
        if key in seen or (r["plan_id"] != plan_id and r["text"] not in sent.get(r["kind"], ())):
            continue
        seen.add(key)
        out.append(
            {"what": what(r["kind"], r["text"]), "why": why(r["class"], r["topic"]), "when": iso(r["created_at"])}
        )
    return out


KINDS_SENT = ("headline", "description", "keyword")  # what a build sends, in the order the checklist lists them


def carried(conn: psycopg.Connection, business_id: str, plan: dict) -> dict[str, list[str]]:
    """What the version would send Google Search were nothing left out, by kind, in the draft's own order (spec
    2026-10-12 §2.1): the headlines and descriptions its Google Search entry holds, the search phrases its build makes
    (`google_build.built_keywords`, sorted, with the business's latest knowledge), the entry chosen exactly the way a
    build does (`google_build.search_channel`, ruling N1). {} for a version with no entry a build could run (no
    consultation, no money on it, no estimate the ceiling can price) or one that cannot be read (logged)."""
    try:
        sections = plan["sections"]
        ch = google_build.search_channel(plan, GOOGLE_SEARCH)
        if ch is None:
            return {}
        ads = sections.get("ads") or {}
        view = {**plan, "sections": {**sections, "ads": {**ads, "channels": [ch]}}}  # built_keywords reads the first
        knowledge = (business.latest_knowledge(conn, business_id) or (None, {}))[1]
        return {
            "headline": list(ch.get("headlines") or []),
            "description": list(ch.get("descriptions") or []),
            "keyword": sorted(google_build.built_keywords(view, knowledge)),
        }
    except Exception as e:  # noqa: BLE001 - a draft that cannot be read makes no item, never a failed checklist
        log.warning("ads_refusal: what plan %s carries could not be read: %s", plan.get("id"), type(e).__name__)
        return {}


def why_for(conn: psycopg.Connection, business_id: str, kind: str, text: str) -> str:
    """Why Google refused this business's line, in the owner's words: the earliest dropped row's sentence (`why`);
    the plain class sentence when no row knows the line. `dropped` and `avoid_words`' own channel, Google Search
    alone -- the only one the Reviewer checks (ruling Y1: a row recorded for another channel must never answer for
    a Google Search line of the same text)."""
    row = conn.execute(
        "SELECT class, topic FROM ads_refusal WHERE business_id = %s AND channel = %s AND kind = %s AND text = %s "
        "AND action = %s ORDER BY created_at, id LIMIT 1",
        (business_id, GOOGLE_SEARCH, kind, text, DROPPED),
    ).fetchone()
    return why(row["class"], row["topic"]) if row else WHY_BY_CLASS["other"]


def flagged(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, plan: dict
) -> tuple[list[tuple[str, str]], list[local_rules.Finding]]:
    """What the draft still carries that Google refused before, or that an active rule catches (spec 2026-10-12 §2.1,
    §2.3): the lines refused by their exact text as (kind, text) in the draft's order, and the rule findings
    (local_rules.check, none while the switch is off) on the rest -- a line both refused and matched is exact alone.
    ([], []) for a draft with no runnable Google Search entry. Read by launch_check.refused_items and by
    blockers.write_copy's gate."""
    sent = carried(conn, business_id, plan)
    if not sent:
        return [], []
    left_out = dropped(conn, business_id)
    exact_sets = {"headline": left_out.headlines, "description": left_out.descriptions, "keyword": left_out.keywords}
    lines = [(kind, text) for kind in KINDS_SENT for text in sent.get(kind, [])]
    exact = [(kind, text) for kind, text in lines if text in exact_sets[kind]]
    rest = [line for line in lines if line not in exact]
    return exact, local_rules.check(rest, local_rules.load(conn, settings))


def avoid_words(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> list[str]:
    """The plan writer's words to avoid (spec 2026-10-12 §2.3; plan D10): the business's own lines Google refused
    (every kind, by exact text, the earliest first -- whatever the switch says: the rewrite the §2.1 item asks for
    must not write them back), then the active rules' words and the pattern rules' tells (local_rules.tells; none
    while the switch is off); each once, at most local_rules.AVOID_MAX, the business's own first."""
    rows = conn.execute(
        "SELECT text, min(created_at) AS first FROM ads_refusal WHERE business_id = %s AND channel = %s "
        "AND action = %s AND text IS NOT NULL GROUP BY text ORDER BY first, text",
        (business_id, GOOGLE_SEARCH, DROPPED),
    ).fetchall()
    out: list[str] = []
    for word in [*(r["text"] for r in rows), *local_rules.tells(local_rules.load(conn, settings))]:
        if word not in out:
            out.append(word)
    return out[: local_rules.AVOID_MAX]


def line(change: dict) -> str:
    """One change as the plan page, the home card and Your ads print it."""
    return f"Removed {change['what']}: {change['why']}."


def say(found: list[dict]) -> str | None:
    """The sentence get_marketing_state adds for the changes, or None for none."""
    if not found:
        return None
    if len(found) == 1:
        return SAY_ONE.format(why=found[0]["why"])
    return SAY_MANY.format(n=len(found))


def for_review(conn: psycopg.Connection, review_id: str) -> list[dict]:
    """A review's rows whole, oldest first -- the admin's (ops_review.detail): Google's code and topic included."""
    rows = conn.execute(
        f"SELECT {COLS} FROM ads_refusal WHERE review_id = %s ORDER BY created_at, id", (review_id,)
    ).fetchall()
    return [{**dict(r), "created_at": iso(r["created_at"])} for r in rows]
