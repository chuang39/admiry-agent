"""A public call sent again with the same idempotency key is answered with its first answer (spec 2026-10-01 §11;
plan G1 Task 18), also where the tool picks its command from where the business stands and the first call changed
that: a budget change, the last answer of the consultation, a launch with its code. The first answer is given again
only while it still stands. A change to the plan stands while the version it made is the latest; after that the tool
itself refuses the key as already used, and runs nothing. A confirmation (a launch with its code, the owner's yes to a
page) is answered from an earlier call only when nothing waits for it to confirm, only from this same call, and only
while what that answer says still stands: the plan it approved is the one asked about and still approved, the page is
up at the version it published, or down."""

import asyncio
from types import SimpleNamespace

from agent import ads_run, audit, keys, landing, models, plans, proposals
from agent.surface import gate
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import ANSWERS, planned, started
from agent.tests.test_surface_supporting import offline  # noqa: F401 - the fixture: the page check, patched
from agent.tests.test_tools_hosting import GOOD_HTML, GOOD_HTML_V2  # two pages the hosting rules accept

# The walk's answers before the question of what one customer is worth: the business on Google Maps, the objective,
# the service (surface_helpers.ANSWERS)
BEFORE_THE_VALUE = tuple((qid, value) for qid, value in ANSWERS if qid != "customer_value")
REUSED = "idempotency_key already used for a different request; use a new key"
NOTHING_FOR_A_CODE = "nothing is waiting for an approval code on this plan"
ALREADY_APPROVED = "this plan is already approved: nothing is left to confirm, and the code was not used"
NOTHING_FOR_A_YES = "nothing is waiting for the owner's yes"
TOO_LONG = "idempotency_key is too long (over 200 characters); send a shorter unique key"


def _rows(conn) -> list[tuple]:
    """(command, public tool, whether the row is a replay) for every row, oldest first."""
    found = conn.execute("SELECT tool, surface_tool, result FROM agent_action ORDER BY id").fetchall()
    return [(r["tool"], r["surface_tool"], "replay_of" in (r["result"] or {})) for r in found]


def _count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]


def _last(conn) -> dict:
    """The last row: its command, public tool, whether it succeeded, its result, key and arguments."""
    return conn.execute(
        "SELECT tool, surface_tool, ok, result, idempotency_key, args_redacted FROM agent_action ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _recorded_refusal(row: dict) -> tuple:
    """A refusal of a reused key as its row records it: not ok, the sentence as its error, no key (so it is never
    replayed)."""
    return row["ok"], row["result"], row["idempotency_key"]


def _take_down_request(conn) -> str:
    """An unpublish_page request waiting for the owner's yes. Nothing is hosted in tests, so confirm_proposal refuses
    to carry it out, in its own words."""
    waiting = {"cls": "nonspend", "what_changes": [], "approval_url": "https://bright.example/"}
    request = proposals.create(
        conn, "biz_1", kind="unpublish_page", payload={}, summary="Take down the page", **waiting
    )
    return request["proposal_id"]


def _went_through(
    conn,
    sent: dict,
    *,
    tool: str = "confirm_proposal",
    say: str = "Done.",
    args: dict | None = None,
    made: dict | None = None,
):
    """An earlier update_landing_page call `sent` that went through, its row written as run() writes one: hosting is
    off in tests unless a test turns it on, and without it a page's yes cannot really go through. `made` is what its
    answer's data says beside the status (a publish names the page as it left it)."""
    key = sent["idempotency_key"]
    audit.record(
        conn,
        tool=tool,
        args=args if args is not None else {"idempotency_key": key},
        result={"data": {"status": "confirmed", **(made or {})}, "say": say, "next": {"kind": "none"}},
        ok=True,
        business_id="biz_1",
        actor="key:earlier",
        actor_class="business",
        api_key_id=None,
        ip="local",
        idempotency_key=key,
        surface_tool="update_landing_page",
        surface_call_hash=audit.call_hash("update_landing_page", sent),
    )


def _hosted_page(conn, status: str, version: int = 1) -> None:
    """The business's Admiry-hosted page, published or taken down, at `version`, written directly (hosting is off in
    tests unless a test turns it on)."""
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, version, status) VALUES ('pg_hosted', 'biz_1', "
        "'hosted', 'https://bright.business.test/', 'bright.business.test', %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, version = EXCLUDED.version",
        (version, status),
    )


def test_a_budget_change_sent_twice_changes_the_plan_once(client, conn, walk_fake):
    walk = planned(client, conn)
    conn.execute("DELETE FROM agent_action")
    args = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "same-1"}
    first = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    again = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert (data(again)["plan_id"], data(again)["version"]) == (data(first)["plan_id"], 2)
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert plans.latest(conn, "biz_1")["version"] == 2  # the repeat made no third version
    assert _rows(conn) == [("adjust", "update_marketing_plan", False), ("adjust", "update_marketing_plan", True)]
    assert [a["tool"] for a in again["structuredContent"]["next"]["actions"]] == [
        a["tool"] for a in first["structuredContent"]["next"]["actions"]
    ]  # the replay's actions are in public names too


def test_the_budget_tool_sent_twice_changes_the_plan_once(client, conn, walk_fake):
    walk = planned(client, conn)
    args = {"monthly_usd": 1500, "idempotency_key": "same-b"}
    first = call_tool(client, "update_marketing_budget", args, key=walk["key"])
    again = call_tool(client, "update_marketing_budget", args, key=walk["key"])
    assert not again["isError"] and data(again)["plan_id"] == data(first)["plan_id"], tool_text(again)
    assert plans.latest(conn, "biz_1")["version"] == 2


def test_the_same_change_written_another_way_is_the_same_call(client, conn, walk_fake):
    """A host that re-serialises its retry -- the whole number with a decimal point, an argument it now sends as null
    -- sends the same call: it is answered with the first answer, and the plan gets no third version."""
    walk = planned(client, conn)
    sent = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "same-10"}
    first = call_tool(client, "update_marketing_plan", sent, key=walk["key"])
    rewritten = {"changes": {"monthly_budget_usd": 1500.0}, "idempotency_key": "same-10", "message": None}
    again = call_tool(client, "update_marketing_plan", rewritten, key=walk["key"])
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert data(again)["plan_id"] == data(first)["plan_id"]
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert plans.latest(conn, "biz_1")["version"] == 2  # no third version
    assert _rows(conn)[-1] == ("adjust", "update_marketing_plan", True)


def test_the_last_answer_sent_twice_is_answered_the_same_twice(client, conn, walk_fake):
    walk = started(client, conn)
    for i, (qid, value) in enumerate(BEFORE_THE_VALUE, 1):
        turn = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"a{i}"}, key=walk["key"]
        )
        assert not turn["isError"], tool_text(turn)
    args = {"message": "around three thousand", "idempotency_key": "same-2"}
    first = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    again = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    assert data(first)["ready"] is True and not again["isError"], tool_text(again)  # not "show the plan first"
    assert data(again)["ready"] is True and again["structuredContent"]["say"] == first["structuredContent"]["say"]


def test_a_change_that_names_its_plan_sent_twice_changes_the_plan_once(client, conn, walk_fake):
    """Right after a change the version it made is the latest, so the same call is answered with its first answer,
    also when it names the plan it changed."""
    walk = planned(client, conn)
    args = {"changes": {"monthly_budget_usd": 1500}, "plan_id": walk["plan_id"], "idempotency_key": "same-12"}
    first = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    again = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert (data(again)["plan_id"], data(again)["version"]) == (data(first)["plan_id"], 2)
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert plans.latest(conn, "biz_1")["version"] == 2  # the repeat made no third version
    changed = ("adjust", "update_marketing_plan")
    assert _rows(conn)[-2:] == [(*changed, False), (*changed, True)]


def _back_to_the_first_budget(client, conn, walk, tool: str, change, *, command: str = "adjust") -> None:
    """$1,500 with key K (version 2), then $2,000 with another key (version 3), then "back to $1,500": the first call
    sent again with K. Its first answer said $1,500 of a version that is no longer the latest, so it is not given
    again. The key was used, so the tool refuses it itself as a reused key -- one row, under the first call's command,
    holding the call as the assistant sent it -- and routes nothing: no command's own replay, which knows the call
    when it names its plan, can give the answer the tool declined. The same request with a fresh key makes the change
    (version 4). `change(usd, plan_id)` is the call's arguments; `plan_id` is the latest plan when the call is made,
    for a call that names its plan."""
    first = {**change(1500, walk["plan_id"]), "idempotency_key": "back-1"}
    made = call_tool(client, tool, first, key=walk["key"])
    assert not made["isError"] and data(made)["version"] == 2, tool_text(made)
    second = {**change(2000, data(made)["plan_id"]), "idempotency_key": "back-2"}
    later = call_tool(client, tool, second, key=walk["key"])
    assert not later["isError"] and data(later)["cards"]["budget"]["monthly_usd"] == 2000, tool_text(later)
    before = _count(conn)
    again = call_tool(client, tool, first, key=walk["key"])
    assert again["isError"] and REUSED in tool_text(again), tool_text(again)  # not version 2's "$1,500 a month"
    refusal = _last(conn)
    assert _count(conn) == before + 1 and (refusal["tool"], refusal["surface_tool"]) == (command, tool)
    assert _recorded_refusal(refusal) == (False, {"error": REUSED}, None)  # a refusal, not a replay
    assert set(refusal["args_redacted"]) == set(first)  # the tool's own: the call as sent, not a command's arguments
    assert plans.latest(conn, "biz_1")["id"] == data(later)["plan_id"]  # still the $2,000 version
    fresh = call_tool(client, tool, {**first, "idempotency_key": "back-3"}, key=walk["key"])
    assert not fresh["isError"], tool_text(fresh)
    assert (data(fresh)["version"], data(fresh)["cards"]["budget"]["monthly_usd"]) == (4, 1500)


def test_a_plan_change_sent_again_after_a_later_version_is_refused_as_a_reused_key(client, conn, walk_fake):
    walk = planned(client, conn)
    _back_to_the_first_budget(
        client, conn, walk, "update_marketing_plan", lambda usd, _plan: {"changes": {"monthly_budget_usd": usd}}
    )


def test_a_plan_change_that_names_its_plan_is_refused_the_same_way(client, conn, walk_fake):
    """The review's case. A call that names its plan would reach adjust with the very arguments of the first call,
    and adjust's own replay would answer with the version the tool had just declined to give again."""
    walk = planned(client, conn)
    _back_to_the_first_budget(
        client,
        conn,
        walk,
        "update_marketing_plan",
        lambda usd, plan: {"changes": {"monthly_budget_usd": usd}, "plan_id": plan},
    )


def test_words_that_changed_a_named_plan_are_refused_the_same_way(client, conn, walk_fake):
    """The same through the owner's own words, read as a change of the budget: the refusal is recorded under the
    command that read them."""
    walk = planned(client, conn)
    blank = dict.fromkeys(("level", "radius_miles", "offer", "objective", "channel", "channel_monthly_usd"), None)
    for usd in (1500, 2000):
        read = models.AdjustRead(monthly_budget_usd=usd, understood=True, asks_for_change=True, **blank)
        walk_fake.adjusts[f"make it ${usd:,} a month"] = read
    _back_to_the_first_budget(
        client,
        conn,
        walk,
        "update_marketing_plan",
        lambda usd, plan: {"message": f"make it ${usd:,} a month", "plan_id": plan},
        command="change_plan",
    )


def test_a_budget_sent_again_after_a_later_version_is_refused_as_a_reused_key(client, conn, walk_fake):
    walk = planned(client, conn)
    _back_to_the_first_budget(client, conn, walk, "update_marketing_budget", lambda usd, _plan: {"monthly_usd": usd})


def test_the_last_answer_sent_again_after_the_plan_was_built_is_answered_the_same(client, conn, walk_fake):
    """An answer that made no plan version stands whatever versions came after it: the consultation's last answer,
    sent again once the plan is built, is answered with its first answer (routed afresh, it would go to change_plan as
    words about the plan)."""
    walk = started(client, conn)
    for i, (qid, value) in enumerate(BEFORE_THE_VALUE, 1):
        turn = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"b{i}"}, key=walk["key"]
        )
        assert not turn["isError"], tool_text(turn)
    last = {"message": "around three thousand", "idempotency_key": "last-1"}
    first = call_tool(client, "update_marketing_plan", last, key=walk["key"])
    assert data(first)["ready"] is True, tool_text(first)
    built = call_tool(client, "get_marketing_plan", {}, key=walk["key"])
    assert not built["isError"] and plans.latest(conn, "biz_1")["version"] == 1, tool_text(built)
    again = call_tool(client, "update_marketing_plan", last, key=walk["key"])
    assert not again["isError"], tool_text(again)
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert _rows(conn)[-1] == ("answer", "update_marketing_plan", True)


def test_a_launch_sent_twice_with_its_code_launches_once_and_keeps_the_code_out(client, conn, walk_fake):
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    args = {"idempotency_key": "same-3", "approval_code": code}
    first = call_tool(client, "launch_marketing_plan", args, key=walk["key"])
    again = call_tool(client, "launch_marketing_plan", args, key=walk["key"])
    assert not first["isError"] and not again["isError"], tool_text(again)
    # the approval's own answer, which its row holds and a replay gives back (as confirm_proposal's own replay does:
    # test_tools_proposals); where the ads stand is added to the first answer after the commit, and only to it
    assert first["structuredContent"]["say"] == f"{again['structuredContent']['say']} {ads_run.OFF}"
    assert plans.latest(conn, "biz_1")["status"] == "approved"
    launched = ("confirm_proposal", "launch_marketing_plan")
    assert _rows(conn)[-2:] == [(*launched, False), (*launched, True)]  # one approval, then its replay
    stored = conn.execute(
        "SELECT args_redacted, result, surface_call_hash FROM agent_action WHERE idempotency_key = 'same-3'"
    ).fetchone()
    assert code not in str(stored["args_redacted"]) and code not in str(stored["result"])
    blanked = {"idempotency_key": "same-3", "approval_code": "***"}
    assert stored["surface_call_hash"] == audit.call_hash("launch_marketing_plan", blanked)  # the code is not hashed


def test_a_launch_to_review_sent_twice_with_its_code_sends_it_once(client, conn, walk_fake, monkeypatch):
    """L1 review I1c: the review's own case of the pattern above -- the code that sent a first launch to Admiry's
    review, sent again with the same key, is answered with that same first answer, not refused as a reused key and
    not sent to review a second time."""
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    args = {"idempotency_key": "same-review", "approval_code": code}
    first = call_tool(client, "launch_marketing_plan", args, key=walk["key"])
    again = call_tool(client, "launch_marketing_plan", args, key=walk["key"])
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert data(first)["plan_status"] == "in_review" and data(again)["plan_status"] == "in_review"
    assert plans.latest(conn, "biz_1")["status"] == "in_review"
    launched = ("confirm_proposal", "launch_marketing_plan")
    assert _rows(conn)[-2:] == [(*launched, False), (*launched, True)]  # one review, then its replay


def test_a_key_used_for_another_request_is_refused_and_nothing_runs(client, conn, walk_fake):
    walk = planned(client, conn)
    one = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "same-4"}
    assert not call_tool(client, "update_marketing_plan", one, key=walk["key"])["isError"]
    before = _count(conn)
    other = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1200}, "idempotency_key": "same-4"},
        key=walk["key"],
    )
    assert other["isError"] and "idempotency_key already used for a different request" in tool_text(other)
    assert plans.latest(conn, "biz_1")["version"] == 2
    assert _rows(conn)[-1][:2] == ("adjust", "update_marketing_plan")  # the refusal is recorded under the same pair
    assert _count(conn) == before + 1 and _recorded_refusal(_last(conn)) == (False, {"error": REUSED}, None)


def test_a_key_reused_for_another_command_of_the_same_tool_is_refused_and_nothing_runs(client, conn, walk_fake):
    """The earlier call is found by the public tool and the key, whichever command it ran: a budget change sent with
    the key of a goal change would otherwise reach adjust, which never saw that key."""
    walk = planned(client, conn)
    goal = {"changes": {"target": 30}, "idempotency_key": "same-6"}
    assert not call_tool(client, "update_marketing_plan", goal, key=walk["key"])["isError"]
    budget = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "same-6"}
    before = _count(conn)
    refused = call_tool(client, "update_marketing_plan", budget, key=walk["key"])
    assert refused["isError"] and "idempotency_key already used for a different request" in tool_text(refused)
    assert plans.latest(conn, "biz_1")["version"] == 1  # adjust never ran
    assert _rows(conn)[-1][:2] == ("change_goal", "update_marketing_plan")  # under the command the key ran
    assert _count(conn) == before + 1 and _recorded_refusal(_last(conn)) == (False, {"error": REUSED}, None)


def test_the_first_answer_goes_only_to_a_caller_who_may_make_the_call(client, conn, walk_fake):
    """The earlier call is looked up for the caller's own business, under the tool's scope. The same call with the
    same key from another business is that business's own first call (refused in the tool's words: it has no plan);
    from a key of this business that may not manage it, it is refused in the command's words. Neither gets this
    call's answer."""
    walk = planned(client, conn)
    args = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "same-7"}
    assert not call_tool(client, "update_marketing_plan", args, key=walk["key"])["isError"]
    other = business_with_key(conn, scopes=("read", "manage", "leads"), biz_id="biz_2", claim_state="verified")
    theirs = call_tool(client, "update_marketing_plan", args, key=other)
    assert theirs["isError"] and "there is no plan yet: begin a plan first" in tool_text(theirs)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    read_only = call_tool(client, "update_marketing_plan", args, key=reader)
    assert read_only["isError"] and "lacks the 'manage' scope" in tool_text(read_only)


def test_the_page_tool_leaves_each_action_its_own_rule_for_a_key(client, conn, walk_fake, offline):  # noqa: F811 - the fixture imported above
    """Only the owner's yes looks for an earlier call, and only when nothing waits for it: a page written with the key
    the owner's own page was named with is written (writing takes no key), as before Task 18."""
    walk = planned(client, conn)
    named = {"action": "use_url", "url": "https://brightsmile.example/invisalign", "idempotency_key": "same-8"}
    assert not call_tool(client, "update_landing_page", named, key=walk["key"])["isError"]
    written = call_tool(
        client, "update_landing_page", {"action": "write", "idempotency_key": "same-8"}, key=walk["key"]
    )
    assert not written["isError"], tool_text(written)
    assert _rows(conn)[-1] == ("create_page", "update_landing_page", False)


def test_a_key_used_for_an_earlier_launch_never_confirms_a_new_request_with_the_old_answer(client, conn, walk_fake):
    """The review's case. A confirmation's identity leaves its code out, so every launch sent with one key looks the
    same: a launch request that waits goes to confirm_proposal, which refuses a key already used for another request
    (the request stays open, and a fresh key confirms it); only a confirmation with nothing left to confirm is
    answered from an earlier call."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    first_code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    launched = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "launch-1", "approval_code": first_code}, key=walk["key"]
    )
    assert not launched["isError"], tool_text(launched)
    changed = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "change-1"},
        key=walk["key"],
    )
    assert not changed["isError"], tool_text(changed)
    asked_again = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-2"}, key=walk["key"])
    waiting = data(asked_again)["proposal_id"]
    new_code = proposals.issue_code(conn, waiting, via="test")
    before = _count(conn)
    reused = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "launch-1", "approval_code": new_code}, key=walk["key"]
    )
    assert reused["isError"] and REUSED in tool_text(reused)  # not the earlier launch's "Confirmed."
    refusal = _last(conn)
    assert _count(conn) == before + 1 and _recorded_refusal(refusal) == (False, {"error": REUSED}, None)
    assert (refusal["tool"], refusal["args_redacted"]["proposal_id"]) == ("confirm_proposal", waiting)
    assert proposals.get(conn, "biz_1", waiting)["status"] == "proposed"  # the new request still waits
    fresh = call_tool(
        client, "launch_marketing_plan", {"idempotency_key": "launch-2", "approval_code": new_code}, key=walk["key"]
    )
    assert not fresh["isError"], tool_text(fresh)
    assert proposals.get(conn, "biz_1", waiting)["status"] == "confirmed"
    assert (plans.latest(conn, "biz_1")["version"], plans.latest(conn, "biz_1")["status"]) == (2, "approved")


def test_one_key_for_the_ask_and_the_confirmation_of_a_launch(client, conn, walk_fake):
    """The ask's row does not stand in the confirmation's way, and a repeat of that confirmation, with nothing left to
    confirm, is answered with its own first answer, not refused because the ask used the key first."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "one-key"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    confirming = {"idempotency_key": "one-key", "approval_code": code}
    confirmed = call_tool(client, "launch_marketing_plan", confirming, key=walk["key"])
    assert not confirmed["isError"], tool_text(confirmed)
    assert plans.latest(conn, "biz_1")["status"] == "approved"
    again = call_tool(client, "launch_marketing_plan", confirming, key=walk["key"])
    assert not again["isError"], tool_text(again)
    launched = ("confirm_proposal", "launch_marketing_plan")
    assert _rows(conn)[-2:] == [(*launched, False), (*launched, True)]


def test_the_yes_to_take_the_page_down_goes_to_what_waits_and_a_refused_one_is_not_given_again(client, conn, walk_fake):
    """With a request waiting, the owner's yes reaches confirm_proposal for that request (which refuses it here,
    nothing being hosted). With nothing waiting and no earlier yes that went through, the tool says nothing waits and
    writes no row: a refused call is never an answer to give again."""
    walk = planned(client, conn)
    request = _take_down_request(conn)
    yes = {"action": "unpublish", "owner_confirmed": True, "idempotency_key": "down-1"}
    before = _count(conn)
    call_tool(client, "update_landing_page", yes, key=walk["key"])
    reached = _last(conn)
    assert _count(conn) == before + 1
    assert (reached["tool"], reached["surface_tool"], reached["args_redacted"]["proposal_id"]) == (
        "confirm_proposal",
        "update_landing_page",
        request,
    )
    proposals.decline(conn, "biz_1", request, reason="not now")  # nothing waits any more
    before = _count(conn)
    nothing = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert nothing["isError"] and "nothing is waiting for the owner's yes" in tool_text(nothing)
    assert _count(conn) == before


def test_a_key_that_took_the_page_down_once_never_confirms_a_new_request_with_the_old_answer(client, conn, walk_fake):
    """The review's case for the page. Nothing is hosted in tests, so the yes that went through earlier is written as
    run() writes one. A new request that waits goes to confirm_proposal, which refuses the key already used; with
    nothing waiting, the same yes is answered with its first answer."""
    walk = planned(client, conn)
    yes = {"action": "unpublish", "owner_confirmed": True, "idempotency_key": "down-2"}
    earlier = {"proposal_id": "pr_earlier", "approval_code": None, "idempotency_key": "down-2"}
    _went_through(conn, yes, say="Your page is down.", args=earlier)
    request = _take_down_request(conn)
    before = _count(conn)
    reused = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert reused["isError"] and REUSED in tool_text(reused)  # not "Your page is down."
    refusal = _last(conn)
    assert _count(conn) == before + 1 and _recorded_refusal(refusal) == (False, {"error": REUSED}, None)
    assert (refusal["tool"], refusal["args_redacted"]["proposal_id"]) == ("confirm_proposal", request)
    assert proposals.get(conn, "biz_1", request)["status"] == "proposed"
    proposals.decline(conn, "biz_1", request, reason="not now")
    again = call_tool(client, "update_landing_page", yes, key=walk["key"])  # no page: it is down
    assert not again["isError"] and again["structuredContent"]["say"] == "Your page is down.", tool_text(again)
    assert _rows(conn)[-1] == ("confirm_proposal", "update_landing_page", True)


def test_a_launch_key_that_confirmed_an_earlier_version_is_not_answered_for_a_newer_one(client, conn, walk_fake):
    """The re-review's case. v1 was launched with key K; the plan changed and v2 was never asked for. A launch sent
    with K and a code is not answered "Confirmed." -- v2 is not approved -- but told that nothing waits for a code,
    and nothing is written. Right after a launch, its true repeat is still answered with its first answer
    (test_a_launch_sent_twice_with_its_code_launches_once_and_keeps_the_code_out)."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-1"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
    launch = {"idempotency_key": "launch-1", "approval_code": code}
    assert not call_tool(client, "launch_marketing_plan", launch, key=walk["key"])["isError"]
    changed = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "change-1"},
        key=walk["key"],
    )
    assert not changed["isError"], tool_text(changed)
    before = _count(conn)
    again = call_tool(client, "launch_marketing_plan", launch, key=walk["key"])
    assert again["isError"] and NOTHING_FOR_A_CODE in tool_text(again)
    assert _count(conn) == before  # no replay row, no command row
    assert (plans.latest(conn, "biz_1")["version"], plans.latest(conn, "biz_1")["status"]) == (2, "draft")


def test_a_launch_key_that_confirmed_one_version_is_not_answered_for_the_version_launched_since(
    client, conn, walk_fake
):
    """Version 1 was launched with key K; the plan changed, and version 2 was asked for and launched under other keys.
    The plan asked about is approved, but it is not the plan K's answer confirmed: the K call sent again is told that
    the plan is already approved (final review open item A1) -- not "Confirmed." with version 1 in its data -- and
    nothing is written. The launch of version 2, sent again, is still answered with its first answer."""
    walk = planned(client, conn)

    def launch(ask: str, key: str) -> dict:
        """Ask, read the code as the owner would, launch with it: the launch as sent."""
        asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": ask}, key=walk["key"])
        code = proposals.issue_code(conn, data(asked)["proposal_id"], via="test")
        sent = {"idempotency_key": key, "approval_code": code}
        done = call_tool(client, "launch_marketing_plan", sent, key=walk["key"])
        assert not done["isError"], tool_text(done)
        return sent

    first = launch("ask-1", "launch-1")
    changed = call_tool(
        client,
        "update_marketing_plan",
        {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "change-1"},
        key=walk["key"],
    )
    assert not changed["isError"], tool_text(changed)
    second = launch("ask-2", "launch-2")
    latest = plans.latest(conn, "biz_1")
    assert (latest["id"], latest["version"], latest["status"]) == (data(changed)["plan_id"], 2, "approved")
    before = _count(conn)
    again = call_tool(client, "launch_marketing_plan", first, key=walk["key"])
    assert again["isError"] and ALREADY_APPROVED in tool_text(again), tool_text(again)  # not version 1's answer
    assert _count(conn) == before  # no replay row, no command row
    repeat = call_tool(client, "launch_marketing_plan", second, key=walk["key"])
    assert not repeat["isError"] and data(repeat)["plan_id"] == latest["id"], tool_text(repeat)
    assert _rows(conn)[-1] == ("confirm_proposal", "launch_marketing_plan", True)


def test_a_launch_key_only_the_ask_used_leaves_nothing_waiting(client, conn, walk_fake):
    """One key for the ask and, once the request lapsed, for a confirmation: nothing waits, and that is what the owner
    hears -- not "already used for a different request" recorded under the ask. Once the plan is approved under
    another key, the call is told the plan is already approved (final review open item A1): a confirmation counts
    only a row of this same call."""
    walk = planned(client, conn)
    asked = call_tool(client, "launch_marketing_plan", {"idempotency_key": "one-key"}, key=walk["key"])
    proposals.decline(conn, "biz_1", data(asked)["proposal_id"], reason="lapsed")
    confirming = {"idempotency_key": "one-key", "approval_code": "12-345"}
    before = _count(conn)
    lapsed = call_tool(client, "launch_marketing_plan", confirming, key=walk["key"])
    assert lapsed["isError"] and NOTHING_FOR_A_CODE in tool_text(lapsed)
    assert _count(conn) == before
    asked_again = call_tool(client, "launch_marketing_plan", {"idempotency_key": "ask-2"}, key=walk["key"])
    code = proposals.issue_code(conn, data(asked_again)["proposal_id"], via="test")
    launch = {"idempotency_key": "launch-2", "approval_code": code}
    assert not call_tool(client, "launch_marketing_plan", launch, key=walk["key"])["isError"]
    assert plans.latest(conn, "biz_1")["status"] == "approved"
    before = _count(conn)
    approved = call_tool(client, "launch_marketing_plan", confirming, key=walk["key"])
    assert approved["isError"] and ALREADY_APPROVED in tool_text(approved)
    assert _count(conn) == before


def test_a_page_key_only_the_ask_used_leaves_nothing_waiting(client, conn, walk_fake):
    """The ask to take the page down went through with key K and its request lapsed: the yes sent with K is told that
    nothing waits, and nothing is written."""
    walk = planned(client, conn)
    _went_through(conn, {"action": "unpublish", "idempotency_key": "down-3"}, tool="unpublish_page")
    yes = {"action": "unpublish", "owner_confirmed": True, "idempotency_key": "down-3"}
    before = _count(conn)
    res = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert res["isError"] and NOTHING_FOR_A_YES in tool_text(res)
    assert _count(conn) == before


def test_a_yes_to_take_the_page_down_is_answered_again_only_while_the_page_is_down(client, conn, walk_fake):
    walk = planned(client, conn)
    yes = {"action": "unpublish", "owner_confirmed": True, "idempotency_key": "down-4"}
    _went_through(conn, yes, say="Your page is down.")
    _hosted_page(conn, "unpublished")
    down = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert not down["isError"] and down["structuredContent"]["say"] == "Your page is down.", tool_text(down)
    _hosted_page(conn, "published")  # published again since
    before = _count(conn)
    up = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert up["isError"] and NOTHING_FOR_A_YES in tool_text(up)  # not "Your page is down."
    assert _count(conn) == before


def test_a_yes_to_publish_is_answered_again_only_while_the_page_is_up(client, conn, walk_fake):
    walk = planned(client, conn)
    yes = {"action": "publish", "token": "pv_one", "owner_confirmed": True, "idempotency_key": "up-1"}
    _went_through(conn, yes, say="Your page is live.")
    _hosted_page(conn, "published")
    up = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert not up["isError"] and up["structuredContent"]["say"] == "Your page is live.", tool_text(up)
    _hosted_page(conn, "unpublished")  # taken down since
    before = _count(conn)
    down = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert down["isError"] and NOTHING_FOR_A_YES in tool_text(down)  # not "Your page is live."
    assert _count(conn) == before


def test_a_yes_to_publish_is_answered_again_only_while_the_version_it_published_is_live(client, conn, walk_fake):
    """A publish's answer names the page as the publish left it, its version included. While that version is the live
    one, the same yes is answered with its first answer. Once another version is live the page is up, but not as that
    answer says: the yes is told that nothing waits, and nothing is written."""
    walk = planned(client, conn)
    yes = {"action": "publish", "token": "pv_one", "owner_confirmed": True, "idempotency_key": "up-2"}
    _hosted_page(conn, "published", version=2)
    _went_through(conn, yes, say="Your page is live.", made={"page": {"status": "published", "version": 2}})
    up = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert not up["isError"] and up["structuredContent"]["say"] == "Your page is live.", tool_text(up)
    assert _rows(conn)[-1] == ("confirm_proposal", "update_landing_page", True)
    _hosted_page(conn, "published", version=3)  # another version was published since, under other keys
    before = _count(conn)
    other = call_tool(client, "update_landing_page", yes, key=walk["key"])
    assert other["isError"] and NOTHING_FOR_A_YES in tool_text(other), tool_text(other)  # not version 2's answer
    assert _count(conn) == before


def test_a_real_publish_names_its_version_and_its_yes_is_given_again_only_while_that_version_is_live(
    client,
    conn,
    walk_fake,
    offline,  # noqa: F811 - the fixture imported above
    monkeypatch,
):
    """The same with hosting on, through the tool from the preview to the owner's yes: what a publish really stores
    names the version it made live, and that is what the yes sent again is held against."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "business.test")
    walk = planned(client, conn)

    def publish(html: str, n: int) -> dict:
        """Preview a page, ask to publish it, give the owner's yes: the yes as sent."""
        preview = {"action": "preview_html", "html": html, "idempotency_key": f"show-{n}"}
        shown = call_tool(client, "update_landing_page", preview, key=walk["key"])
        asking = {"action": "publish", "token": data(shown)["token"], "idempotency_key": f"ask-{n}"}
        asked = call_tool(client, "update_landing_page", asking, key=walk["key"])
        assert not asked["isError"] and data(asked)["status"] == "proposed", tool_text(asked)
        yes = {**asking, "owner_confirmed": True, "idempotency_key": f"yes-{n}"}
        done = call_tool(client, "update_landing_page", yes, key=walk["key"])
        assert not done["isError"] and data(done)["page"]["version"] == n, tool_text(done)
        return yes

    first = publish(GOOD_HTML, 1)
    again = call_tool(client, "update_landing_page", first, key=walk["key"])
    assert not again["isError"] and data(again)["page"]["version"] == 1, tool_text(again)
    assert _rows(conn)[-1] == ("confirm_proposal", "update_landing_page", True)
    publish(GOOD_HTML_V2, 2)
    before = _count(conn)
    stale = call_tool(client, "update_landing_page", first, key=walk["key"])
    assert stale["isError"] and NOTHING_FOR_A_YES in tool_text(stale), tool_text(stale)  # not version 1's answer
    assert _count(conn) == before


def test_a_key_without_a_business_is_never_answered_from_a_row(client, conn):
    """A platform key names no business, so it has no earlier call of its own: not even a row written without a
    business answers it. Its call goes to the command, which refuses a key bound to no business."""
    platform = business_with_key(conn, cls="platform")
    args = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "same-11"}
    audit.record(  # under another command than adjust: only the public lookup could find it
        conn,
        tool="change_goal",
        args={},
        result={"data": {}, "say": "Someone else's answer.", "next": {"kind": "none"}},
        ok=True,
        business_id=None,
        actor="key:elsewhere",
        actor_class="platform",
        api_key_id=None,
        ip="local",
        idempotency_key="same-11",
        surface_tool="update_marketing_plan",
        surface_call_hash=audit.call_hash("update_marketing_plan", args),
    )
    res = call_tool(client, "update_marketing_plan", args, key=platform)
    assert res["isError"] and "not bound to a business" in tool_text(res)


def test_a_row_from_before_the_calls_identity_is_left_to_the_commands_own_replay(client, conn, walk_fake):
    """A row written before surface_call_hash existed names no public call: a repeat goes its way as it did before,
    and the command's own replay answers it."""
    walk = planned(client, conn)
    args = {"changes": {"target": 30}, "idempotency_key": "same-9"}
    first = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    conn.execute("UPDATE agent_action SET surface_call_hash = NULL")
    again = call_tool(client, "update_marketing_plan", args, key=walk["key"])
    assert not again["isError"], tool_text(again)
    assert again["structuredContent"]["say"] == first["structuredContent"]["say"]
    assert _rows(conn)[-1] == ("change_goal", "update_marketing_plan", True)


def test_a_refused_call_is_not_replayed(client, conn, walk_fake):
    walk = planned(client, conn)
    conn.execute("DELETE FROM agent_action")
    args = {"changes": {"monthly_budget_usd": 250000}, "idempotency_key": "same-5"}
    for _ in range(2):
        res = call_tool(client, "update_marketing_plan", args, key=walk["key"])
        assert res["isError"] and "monthly_budget_usd must be between 1 and 100,000" in tool_text(res)
    assert [replay for _, public, replay in _rows(conn) if public == "update_marketing_plan"] == [False, False]


def test_a_command_called_directly_keeps_its_own_replay(client, conn, walk_fake):
    """The homepage and the suite call commands without the public surface: nothing changes for them."""
    walk = planned(client, conn)
    conn.execute("DELETE FROM agent_action")
    args = {"plan_id": walk["plan_id"], "idempotency_key": "cmd-1", "monthly_budget_usd": 1500}
    first = call_tool(client, "adjust", args, key=walk["key"])
    again = call_tool(client, "adjust", args, key=walk["key"])
    assert data(again)["plan_id"] == data(first)["plan_id"]
    assert _rows(conn) == [("adjust", None, False), ("adjust", None, True)]
    hashes = conn.execute("SELECT surface_call_hash FROM agent_action").fetchall()
    assert [r["surface_call_hash"] for r in hashes] == [None, None]


def test_the_gate_keeps_the_call_and_blanks_a_code():
    assert gate.call() is None  # outside a public call
    name, sent = (
        gate.SURFACE_TOOL.set("launch_marketing_plan"),
        gate.SURFACE_ARGS.set({"approval_code": "48-311", "k": 1}),
    )
    try:
        assert gate.call() == ("launch_marketing_plan", {"approval_code": "***", "k": 1})
    finally:
        gate.SURFACE_ARGS.reset(sent)
        gate.SURFACE_TOOL.reset(name)
    assert audit.call_hash("t", {"a": 1}) == audit.call_hash("t", {"a": 1}) != audit.call_hash("u", {"a": 1})


def test_the_gate_blanks_a_code_sent_as_a_number_zero_included():
    """Final review A m6: a code may come as a number (48311; "00-000" is 0): every form of it is blanked out of the
    call's identity. A code left out stays left out."""
    for code, kept in ((48311, "***"), (0, "***"), (None, None), ("", "")):
        name, sent = (
            gate.SURFACE_TOOL.set("launch_marketing_plan"),
            gate.SURFACE_ARGS.set({"approval_code": code, "idempotency_key": "k"}),
        )
        try:
            assert gate.call() == ("launch_marketing_plan", {"approval_code": kept, "idempotency_key": "k"}), code
        finally:
            gate.SURFACE_ARGS.reset(sent)
            gate.SURFACE_TOOL.reset(name)


def test_a_command_key_older_than_the_window_is_refused_and_changes_nothing(client, conn, walk_fake):
    """Final review A P1 (open item B1): a command called with a key whose row is 25 hours old is not answered from
    that row. It is refused as a reused key, the refusal is recorded, and nothing changes. Within the window the same
    call is still answered with its first answer."""
    walk = planned(client, conn)
    args = {"plan_id": walk["plan_id"], "idempotency_key": "old-1", "monthly_budget_usd": 1500}
    first = call_tool(client, "adjust", args, key=walk["key"])
    again = call_tool(client, "adjust", args, key=walk["key"])
    assert not first["isError"] and not again["isError"], tool_text(again)
    assert data(again)["plan_id"] == data(first)["plan_id"] and _rows(conn)[-1] == ("adjust", None, True)
    conn.execute("UPDATE agent_action SET ts = ts - interval '25 hours' WHERE idempotency_key = 'old-1'")
    before = _count(conn)
    late = call_tool(client, "adjust", args, key=walk["key"])
    assert late["isError"] and tool_text(late) == REUSED, tool_text(late)
    assert _count(conn) == before + 1 and _recorded_refusal(_last(conn)) == (False, {"error": REUSED}, None)
    assert plans.latest(conn, "biz_1")["version"] == 2  # no third version, and none given from the old row


def test_a_pause_sent_with_a_key_days_old_is_refused_while_the_ads_run(client, conn, walk_fake, strict_surface):
    """The review's reproduction: the ads were paused with key K and run again since; nine days later the assistant
    sends K again. It is not answered "paused" from the old row while the ads run: the key is refused, the ads are
    left as they are, and a new key pauses them."""
    walk = planned(client, conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES ('biz_1', 'google_search', %s, 'live', '{\"campaign\": \"customers/1/campaigns/2\"}')",
        (walk["plan_id"],),
    )
    first = call_tool(client, "pause_campaign", {"idempotency_key": "pause-1"}, key=walk["key"])
    assert not first["isError"] and ads_run.get(conn, "biz_1", "google_search")["state"] == "paused", tool_text(first)
    # the ads run again (execution is off in tests, so a resume cannot turn them on: written as it would leave them)
    conn.execute(
        "UPDATE channel_campaign SET state = 'live', paused_reason = NULL, "
        "resources = resources - 'owner_paused' - 'paused_by' - 'pause_owed' WHERE business_id = 'biz_1'"
    )
    conn.execute("UPDATE agent_action SET ts = ts - interval '9 days' WHERE idempotency_key = 'pause-1'")
    before = _count(conn)
    again = call_tool(client, "pause_campaign", {"idempotency_key": "pause-1"}, key=walk["key"])
    assert again["isError"] and tool_text(again) == REUSED, tool_text(again)
    refusal = _last(conn)
    assert _count(conn) == before + 1 and (refusal["tool"], refusal["surface_tool"]) == ("pause", "pause_campaign")
    assert _recorded_refusal(refusal) == (False, {"error": REUSED}, None)
    assert ads_run.get(conn, "biz_1", "google_search")["state"] == "live"
    fresh = call_tool(client, "pause_campaign", {"idempotency_key": "pause-2"}, key=walk["key"])
    assert not fresh["isError"] and ads_run.get(conn, "biz_1", "google_search")["state"] == "paused", tool_text(fresh)


def test_a_key_over_200_characters_is_refused_and_nothing_acts(client, conn, walk_fake, offline, strict_surface):  # noqa: F811 - the fixture imported above
    """Final review A P2: a pause and a page named with use_url act before their transaction. With a key over 200
    characters neither acts: the call is refused, its row says why -- never "internal error" -- and the ads and the
    page on file are as they were. 200 characters pass."""
    walk = planned(client, conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES ('biz_1', 'google_search', %s, 'live', '{\"campaign\": \"customers/1/campaigns/2\"}')",
        (walk["plan_id"],),
    )
    page = landing.current(conn, "biz_1")
    calls = (
        ("pause_campaign", {}, "pause"),
        (
            "update_landing_page",
            {"action": "use_url", "url": "https://brightsmile.example/invisalign"},
            "set_landing_page",
        ),
    )
    for tool, args, command in calls:
        before = _count(conn)
        res = call_tool(client, tool, {**args, "idempotency_key": "k" * 201}, key=walk["key"])
        assert res["isError"] and tool_text(res) == TOO_LONG, (tool, tool_text(res))
        refusal = _last(conn)
        assert _count(conn) == before + 1 and (refusal["tool"], refusal["surface_tool"]) == (command, tool)
        assert _recorded_refusal(refusal) == (False, {"error": TOO_LONG}, None)
    assert ads_run.get(conn, "biz_1", "google_search")["state"] == "live" and landing.current(conn, "biz_1") == page
    for tool, args, _ in calls:
        res = call_tool(client, tool, {**args, "idempotency_key": "k" * 200}, key=walk["key"])
        assert not res["isError"], (tool, tool_text(res))
    assert ads_run.get(conn, "biz_1", "google_search")["state"] == "paused"
    assert landing.current(conn, "biz_1")["url"] == "https://brightsmile.example/invisalign"


def test_the_gate_keeps_its_own_copy_of_the_arguments():
    """Nothing a tool or a command does to a nested argument changes the identity of the call in progress."""
    sent = {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "k"}
    seen = []

    async def call_next(context):
        sent["changes"]["monthly_budget_usd"] = 9  # what a tool might do to the object it was handed
        seen.append(gate.call())
        return "answered"

    context = SimpleNamespace(message=SimpleNamespace(name="update_marketing_plan", arguments=sent))
    assert asyncio.run(gate.SurfaceGate().on_call_tool(context, call_next)) == "answered"
    assert seen == [("update_marketing_plan", {"changes": {"monthly_budget_usd": 1500}, "idempotency_key": "k"})]
