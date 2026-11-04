"""A first launch's review after the owner's yes and before its ads go live (L1, spec 2026-10-10 §6): one
`launch_review` row per version the owner agreed to (and paid for) while AGENT_LAUNCH_REVIEW is on. The yes moves the
plan from draft to in_review (`submit`, inside the approval's own transaction: proposals.confirm); Admiry's Reviewer
(roles/reviewer.py) takes it through its steps -- creative, configured, checked -- and passes it (the approval itself)
or holds it for an admin; an admin passes, returns or rejects it (ops_review.py). A new version while one is in review
withdraws it (`withdraw_open`, plans.create_version). The data and the owner's words only: what passes a review lives
with the Reviewer, which imports the approval. The plan's statuses are plans.py's, written here in their words (plans
imports this module)."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import db
from .roles import queue as role_queue

STATES = ("queued", "creative", "configured", "checked", "passed", "held", "returned", "rejected", "withdrawn")
RUNNING = ("queued", "creative", "configured", "checked")  # the Reviewer's to take on
OPEN = (*RUNNING, "held")  # not decided yet: the Reviewer's, or an admin's
ROLE, KIND = "reviewer", "launch"
PROMISE = (
    "Your ads go live within 1 hour to 1 business day. Admiry e-mails you if anything needs your attention; write to "
    "hello@admiry.ai any time."
)
IN_REVIEW_SAY = f"In review: Admiry is checking your ads and setting them up with Google. {PROMISE}"
HELD_SAY = (
    "Admiry is checking one thing about your ads before they go live, and e-mails you within one business day. Write "
    "to hello@admiry.ai any time."
)
CHANGED = "this plan changed while confirming; ask for a fresh proposal"  # apply_approve_plan's own words
DECIDED = "This review is already decided."
COLS = (
    "id, plan_id, business_id, state, steps, hold_reason, approval, decided_by, decided_at, note, resolution, "
    "created_at, updated_at"
)
VALIDATED = "validated"  # the step the Reviewer records for every validateOnly call Google answered (plan D1)
# the only resolutions `decide()` ever writes (spec 2026-10-11 §5; plan D4) -- launch_check.items reads a stored
# resolution outside this tuple (a hand-written row, or an older version's) as the plain note, never raising
# (ruling F1, task review)
RESOLUTIONS = ("phone", "check_page")


def submit(conn: psycopg.Connection, business_id: str, plan_id: str, *, approval: dict) -> dict:
    """The owner's yes to a first launch while AGENT_LAUNCH_REVIEW is on (proposals.confirm, inside its transaction):
    the draft becomes in_review, a review row is opened with how the owner said yes (`approval`), and the Reviewer's
    job is queued. ToolError CHANGED when the plan is no longer that draft (a concurrent change: the confirmation rolls
    back, as apply_approve_plan's own)."""
    moved = conn.execute(
        "UPDATE plan SET status = 'in_review' WHERE id = %s AND business_id = %s AND status = 'draft'",
        (plan_id, business_id),
    ).rowcount
    if moved == 0:
        raise ToolError(CHANGED)
    review_id = db.new_id("lr")
    conn.execute(
        "INSERT INTO launch_review (id, plan_id, business_id, approval) VALUES (%s, %s, %s, %s)",
        (review_id, plan_id, business_id, Jsonb(approval)),
    )
    role_queue.enqueue(
        conn, business_id=business_id, role=ROLE, kind=KIND, brief={"review_id": review_id, "plan_id": plan_id}
    )
    return {"plan_id": plan_id, "plan_status": "in_review", "review_id": review_id}


def withdraw_open(conn: psycopg.Connection, business_id: str) -> int:
    """A new version while one is in review (plans.create_version, inside its transaction): the open review ends,
    `withdrawn` by the owner -- nothing is e-mailed -- and its waiting job is skipped. How many ended."""
    ended = conn.execute(
        "UPDATE launch_review SET state = 'withdrawn', decided_by = 'owner', decided_at = now(), updated_at = now() "
        "WHERE business_id = %s AND state = ANY(%s)",
        (business_id, list(OPEN)),
    ).rowcount
    if ended:
        conn.execute(
            "UPDATE agent_job SET status = 'skipped', result = %s, finished_at = now(), updated_at = now() "
            "WHERE business_id = %s AND role = %s AND kind = %s AND status = 'queued'",
            (Jsonb({"reason": "withdrawn"}), business_id, ROLE, KIND),
        )
    return ended


def get(conn: psycopg.Connection, review_id: str) -> dict | None:
    row = conn.execute(f"SELECT {COLS} FROM launch_review WHERE id = %s", (review_id,)).fetchone()
    return dict(row) if row else None


def open_for(conn: psycopg.Connection, business_id: str) -> dict | None:
    """The business's review not decided yet, if any (one at a time: a new version withdraws the open one)."""
    row = conn.execute(
        f"SELECT {COLS} FROM launch_review WHERE business_id = %s AND state = ANY(%s) ORDER BY created_at DESC LIMIT 1",
        (business_id, list(OPEN)),
    ).fetchone()
    return dict(row) if row else None


def note_for(conn: psycopg.Connection, business_id: str, plan_id: str) -> dict | None:
    """The word that sent this version back to the owner (returned or rejected): {state, note, resolution} --
    `resolution` the fix the Reviewer's return names (spec 2026-10-11 §5: `phone`, `check_page`), None for an
    admin's; None otherwise."""
    row = conn.execute(
        "SELECT state, note, resolution FROM launch_review WHERE business_id = %s AND plan_id = %s "
        "AND state IN ('returned', 'rejected') ORDER BY created_at DESC LIMIT 1",
        (business_id, plan_id),
    ).fetchone()
    return dict(row) if row else None


def step(
    conn: psycopg.Connection, review_id: str, *, name: str, ok: bool, detail: dict, state: str | None = None
) -> None:
    """One of the Reviewer's steps on its row (`steps`: name, ok, detail, at), and the state it reached -- while the
    review is still the Reviewer's (an admin's decision meanwhile wins)."""
    entry = {"name": name, "ok": ok, "detail": detail, "at": datetime.now(UTC).isoformat()}
    conn.execute(
        "UPDATE launch_review SET steps = steps || %s, state = COALESCE(%s, state), updated_at = now() "
        "WHERE id = %s AND state = ANY(%s)",
        (Jsonb([entry]), state, review_id, list(RUNNING)),
    )


def append_step(conn: psycopg.Connection, review_id: str, *, name: str, ok: bool, detail: dict) -> None:
    """One ledger entry on `steps`, recorded whatever the review's state is now -- never gated on RUNNING like
    `step` (ruling G1, task review fix round 1): something that already happened (a validateOnly call Google
    answered, the change e-mail sent at the pass) is never silently lost to a decision that landed meanwhile, or to
    a pass that has already moved the row past RUNNING by the time it is recorded."""
    entry = {"name": name, "ok": ok, "detail": detail, "at": datetime.now(UTC).isoformat()}
    conn.execute(
        "UPDATE launch_review SET steps = steps || %s, updated_at = now() WHERE id = %s", (Jsonb([entry]), review_id)
    )


def count_validation(conn: psycopg.Connection, review_id: str, *, ok: bool, detail: dict) -> None:
    """Spec 2026-10-11 §5 (plan D1; ruling G1, task review fix round 1): one validateOnly call Google answered,
    counted via `append_step` whatever the review's state is now -- an admin's decision, or a new version
    withdrawing it, meanwhile wins the review, but the call itself still happened and must count toward
    `reviewer.VALIDATIONS_PER_VERSION`, so a later resubmission of the same version never makes a fourth or fifth
    call. `validations()` reads these the same way it read `step`'s own VALIDATED entries before."""
    append_step(conn, review_id, name=VALIDATED, ok=ok, detail=detail)


def has_step(conn: psycopg.Connection, plan_id: str, name: str) -> bool:
    """Whether any review row of this plan version already carries a step named `name` (ruling G5, task review fix
    round 1: the change e-mail's own "once a version" check, across every round -- the same cross-row pattern
    `validations()` uses for a different step name)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM launch_review r CROSS JOIN LATERAL jsonb_array_elements(r.steps) s "
        "WHERE r.plan_id = %s AND s->>'name' = %s) AS found",
        (plan_id, name),
    ).fetchone()
    return bool(row["found"])


def hold(conn: psycopg.Connection, review_id: str, reason: str) -> bool:
    """The review waits for a person: `held`, with the admin's words for why (a held one takes the newer reason).
    Whether it became held now (False: held already, or decided meanwhile). The read and the write are one
    transaction (ruling R20), and the write's own WHERE repeats the OPEN check the read made, so a decision an admin
    committed between them is never overwritten (inside a caller's own transaction this is a savepoint)."""
    with conn.transaction():
        was = conn.execute("SELECT state FROM launch_review WHERE id = %s FOR UPDATE", (review_id,)).fetchone()
        if was is None or was["state"] not in OPEN:
            return False
        conn.execute(
            "UPDATE launch_review SET state = 'held', hold_reason = %s, updated_at = now() WHERE id = %s "
            "AND state = ANY(%s)",
            (reason, review_id, list(OPEN)),
        )
    return was["state"] != "held"


def decide(
    conn: psycopg.Connection,
    review_id: str,
    *,
    state: str,
    by: str,
    note: str | None = None,
    resolution: str | None = None,
) -> dict:
    """A review decided: passed (the approval itself is the caller's, in the same transaction), returned or rejected
    (the plan back to a draft; `note`, the owner's to read first on its list, always given; `resolution`, the fix the
    note names on the owner's list, spec 2026-10-11 §5 -- the Reviewer's returns give one, an admin's none, and it is
    always one of RESOLUTIONS). ValueError (a programming error, never the admin's words: Task 10's admin command
    refuses a blank note first, and Task 9's back_to_owner always passes one) when a return or a reject carries no
    note, or when `resolution` is given and is not one of RESOLUTIONS (ruling F1, task review: so `blockers.controls`
    is never fed a value its own `fixed[resolution]` would raise KeyError on) -- either way before any write.
    ToolError DECIDED when it was decided already. Both updates are one transaction (ruling R20), so a return's
    plan-draft status commits with its decision (inside a caller's own transaction this is a savepoint). Every writer
    that locks both rows takes the plan row before the review row -- create_version, submit, and this one: the plan
    row is locked first here too, so an owner's new version at the instant of a decision waits instead of deadlocking
    against `approve`, which calls this first in its own transaction."""
    if state in ("returned", "rejected") and not (note or "").strip():
        raise ValueError("a return or a reject carries its note")
    if resolution is not None and resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}, not {resolution!r}")
    with conn.transaction():
        conn.execute(
            "SELECT 1 FROM plan WHERE id = (SELECT plan_id FROM launch_review WHERE id = %s) FOR UPDATE",
            (review_id,),
        )
        row = conn.execute(
            "UPDATE launch_review SET state = %s, decided_by = %s, decided_at = now(), note = %s, resolution = %s, "
            f"updated_at = now() WHERE id = %s AND state = ANY(%s) RETURNING {COLS}",
            (state, by, note, resolution, review_id, list(OPEN)),
        ).fetchone()
        if row is None:
            raise ToolError(DECIDED)
        if state in ("returned", "rejected"):
            conn.execute("UPDATE plan SET status = 'draft' WHERE id = %s AND status = 'in_review'", (row["plan_id"],))
    return dict(row)


def returned_before(conn: psycopg.Connection, plan_id: str, review_id: str) -> str | None:
    """The note of the last time a person (an admin) returned an *earlier* version of this plan to its owner, if one
    did (L1, ruling R17): the Reviewer holds such a version for a person instead of passing it. `review_id` (the
    version now being checked) is excluded: a concurrent admin decision on this same review is not "an earlier
    return" and is caught instead by `approve`'s own DECIDED check (ruling R19a), never read back here as a reason to
    (redundantly) hold what is already decided. A version the Reviewer itself returned (the owner's own page) is
    reviewed again in full."""
    row = conn.execute(
        "SELECT note FROM launch_review WHERE plan_id = %s AND id != %s AND state = 'returned' "
        "AND decided_by LIKE 'admin:%%' ORDER BY created_at DESC LIMIT 1",
        (plan_id, review_id),
    ).fetchone()
    return row["note"] if row else None


def validations(conn: psycopg.Connection, plan_id: str) -> int:
    """How many validateOnly calls Google answered for this plan version (spec 2026-10-11 §5; plan D1): the VALIDATED
    steps -- an acceptance or a refusal, `ok` either way -- over every review row of the version, across their
    rounds. A call Google did not answer leaves no step and is not counted. The Reviewer makes no call once
    reviewer.VALIDATIONS_PER_VERSION are counted; Part B's check before the payment spends from the same count."""
    row = conn.execute(
        "SELECT count(*) AS n FROM launch_review r CROSS JOIN LATERAL jsonb_array_elements(r.steps) s "
        "WHERE r.plan_id = %s AND s->>'name' = %s",
        (plan_id, VALIDATED),
    ).fetchone()
    return int(row["n"])


def owner_words(conn: psycopg.Connection, business_id: str) -> str | None:
    """What the owner reads while the latest plan is in review: IN_REVIEW_SAY, or HELD_SAY while a person looks; None
    otherwise."""
    row = conn.execute(
        "SELECT p.status, r.state FROM plan p LEFT JOIN launch_review r ON r.plan_id = p.id AND r.state = ANY(%s) "
        "WHERE p.business_id = %s ORDER BY p.version DESC LIMIT 1",
        (list(OPEN), business_id),
    ).fetchone()
    if row is None or row["status"] != "in_review":
        return None
    return HELD_SAY if row["state"] == "held" else IN_REVIEW_SAY
