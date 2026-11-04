"""change_plan and ask (spec 2026-10-01 D153, D165, §6.6; plan G1 Task 7): the owner's own words after the plan
change it -- a budget sentence through adjust's own code, anything else through a rewrite -- and a question is
answered from the business's own material."""

import pytest

from agent import (
    auth,
    business,
    envelope,
    keys,
    listings,
    models,
    plans,
    profile,
    proposals,
    tools_consult,
    tools_support,
)
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.surface_helpers import planned, started

# The most either command reads of the owner's words: 1,000 characters, read whole; one more is refused, never cut.
LONGEST = "Lead with the free consultation." + " Really." * 121
SUPPORT_READS = ("human", "help", "dissatisfied")  # every read of the words that is not "none"
NOT_A_CHANGE = (
    "That did not read as a change to the plan, so nothing changed. Tell me what to change: the budget, the area, "
    "the offer, or what the ads should lead with."
)
# the answer to words that ask for no change at all -- a yes, a thank-you (re-review B's words note)
NO_CHANGE_ASKED = (
    "Nothing changed: the plan stands as it is. When you want something different, say what to change: the budget, "
    "the area, the offer, or what the ads should lead with."
)
PLAN_TAIL = "If you also want a change to the plan, tell me what to change."  # the homepage's after-plan ack ends so
NO_CHANGE_LOGGED = "Muse sent the owner's words; they were not a change to the plan, and it did not change"


def _read(**fields) -> models.AdjustRead:
    """A read of the owner's words: no field, and words that ask for some change (the rewrite's read, as every read was
    before the reader said whether the words ask for one); a test of words that ask for none says so."""
    blank = {
        "monthly_budget_usd": None,
        "level": None,
        "radius_miles": None,
        "offer": None,
        "objective": None,
        "channel": None,
        "channel_monthly_usd": None,
        "understood": False,
        "asks_for_change": True,
    }
    return models.AdjustRead(**{**blank, **fields})


def _change(client, walk, words, ik):
    return call_tool(
        client, "change_plan", {"plan_id": walk["plan_id"], "words": words, "idempotency_key": ik}, key=walk["key"]
    )


def test_a_budget_sentence_changes_the_budget_without_a_rewrite(client, conn, walk_fake):
    walk = planned(client, conn)
    walk_fake.adjusts["make it $1,500 a month"] = _read(monthly_budget_usd=1500, understood=True)
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, "make it $1,500 a month", "cp-1")
    assert not res["isError"], tool_text(res)
    assert data(res)["cards"]["budget"]["monthly_usd"] == 1500 and data(res)["version"] == 2
    assert len(walk_fake.plan_calls) == drafts  # adjust's own code: no second draft from the model
    assert plans.total_usd(plans.latest(conn, "biz_1")["sections"]) == 1500.0


def test_other_words_rewrite_the_plan_and_keep_the_budget(client, conn, walk_fake):
    walk = planned(client, conn)
    before = plans.total_usd(plans.latest(conn, "biz_1")["sections"])
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, "lead with the free consultation", "cp-2")
    assert not res["isError"], tool_text(res)
    assert data(res)["version"] == 2 and "cards" in data(res)
    assert len(walk_fake.plan_calls) == drafts + 1
    assert walk_fake.plan_calls[-1]["instruction"] == "lead with the free consultation"
    assert plans.total_usd(plans.latest(conn, "biz_1")["sections"]) == before


def test_a_rewrite_hands_the_model_the_plans_own_budget(client, conn, walk_fake):
    """A rewrite keeps the plan's amount (`plans.total_usd`), so the model is asked with it as the goal's limit and as
    the consultation's monthly_budget_usd: here the $1,500 the owner set, below the goal's $2,000 limit."""
    walk = planned(client, conn)
    walk_fake.adjusts["make it $1,500 a month"] = _read(monthly_budget_usd=1500, understood=True)
    cheaper = _change(client, walk, "make it $1,500 a month", "cp-30")
    assert not cheaper["isError"], tool_text(cheaper)
    res = _change(client, {**walk, "plan_id": data(cheaper)["plan_id"]}, "lead with the free consultation", "cp-31")
    assert not res["isError"], tool_text(res)
    call = walk_fake.plan_calls[-1]
    assert call["goal"]["monthly_limit_usd"] == 1500 and call["consultation"]["monthly_budget_usd"] == 1500
    assert call["consultation"]["channel"] == "google_search"
    assert plans.total_usd(plans.latest(conn, "biz_1")["sections"]) == 1500.0


def test_a_rewrite_without_the_ad_text_is_asked_once_more(client, conn, walk_fake):
    walk = planned(client, conn)
    thin = walk_fake.draft.model_copy(deep=True)
    thin.ads[0].headlines, thin.ads[0].descriptions = ["Only one"], []
    drafts = iter([thin, walk_fake.draft])
    walk_fake.draft_plan = lambda *a, **kw: (walk_fake.plan_calls.append(kw), next(drafts))[1]
    before = len(walk_fake.plan_calls)
    res = _change(client, walk, "lead with the free consultation", "cp-32")
    assert not res["isError"], tool_text(res)
    assert len(walk_fake.plan_calls) == before + 2 and data(res)["version"] == 2
    assert len(data(res)["cards"]["ads_preview"]["headlines"]) == 3


def test_a_rewrite_still_without_the_ad_text_is_refused_and_writes_no_version(client, conn, walk_fake):
    walk = planned(client, conn)
    thin = walk_fake.draft.model_copy(deep=True)
    thin.ads[0].headlines, thin.ads[0].descriptions = [], []
    walk_fake.draft_plan = lambda *a, **kw: (walk_fake.plan_calls.append(kw), thin)[1]
    before = len(walk_fake.plan_calls)
    res = _change(client, walk, "lead with the free consultation", "cp-33")
    assert res["isError"] and tools_consult.NO_AD_TEXT in tool_text(res)
    assert len(walk_fake.plan_calls) == before + 2
    assert plans.latest(conn, "biz_1")["version"] == 1


def test_words_that_ask_for_no_change_change_nothing_and_the_owners_code_still_launches(client, conn, walk_fake):
    """Final review B, Important 8 (the controller's ruling): "Yes, that looks right." names no field and asks for no
    change, so nothing is rewritten -- no new version: the version the owner read stays the latest, its launch request
    stays open, and the code the owner read for it still launches it. The answer says the plan stands (re-review B's
    words note: "That did not read as a change" read oddly after a yes)."""
    walk = planned(client, conn)
    asked = call_tool(client, "approve_plan", {"plan_id": walk["plan_id"], "idempotency_key": "ap-1"}, key=walk["key"])
    assert not asked["isError"], tool_text(asked)
    pid = data(asked)["proposal_id"]
    code = proposals.issue_code(conn, pid, via="plan_page")
    walk_fake.adjusts["Yes, that looks right."] = _read(asks_for_change=False)
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, "Yes, that looks right.", "cp-20")
    assert not res["isError"], tool_text(res)
    assert data(res) == {"changed": False}
    assert res["structuredContent"]["say"] == NO_CHANGE_ASKED and res["structuredContent"]["next"] == envelope.NONE
    assert len(walk_fake.plan_calls) == drafts  # no rewrite was drafted
    latest = plans.latest(conn, "biz_1")
    assert (latest["id"], latest["version"], latest["status"]) == (walk["plan_id"], 1, "draft")
    assert proposals.open_for_plan(conn, "biz_1", walk["plan_id"])["proposal_id"] == pid
    go = call_tool(client, "launch_marketing_plan", {"idempotency_key": "go", "approval_code": code}, key=walk["key"])
    assert not go["isError"], tool_text(go)
    assert plans.latest(conn, "biz_1")["status"] == "approved"


def test_words_that_ask_for_a_change_no_field_holds_are_rewritten(client, conn, walk_fake):
    walk = planned(client, conn)
    walk_fake.adjusts["no ads in the evening"] = _read(asks_for_change=True)
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, "no ads in the evening", "cp-21")
    assert not res["isError"], tool_text(res)
    assert data(res)["version"] == 2 and len(walk_fake.plan_calls) == drafts + 1
    assert walk_fake.plan_calls[-1]["instruction"] == "no ads in the evening"


def test_a_field_the_reader_names_is_changed_whatever_it_said_about_a_change(client, conn, walk_fake):
    """adjust's own change comes first: whether the words ask for a change decides only what happens to words that
    name no field."""
    walk = planned(client, conn)
    walk_fake.adjusts["make it $1,500 a month"] = _read(monthly_budget_usd=1500, understood=True, asks_for_change=False)
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, "make it $1,500 a month", "cp-22")
    assert not res["isError"], tool_text(res)
    assert data(res)["cards"]["budget"]["monthly_usd"] == 1500 and data(res)["version"] == 2
    assert len(walk_fake.plan_calls) == drafts  # adjust's own code: no draft from the model


@pytest.mark.parametrize("asks", SUPPORT_READS)
def test_with_support_off_a_support_read_that_names_no_change_changes_nothing(client, conn, walk_fake, asks):
    """AGENT_SUPPORT off (the default), as on the homepage: no ticket, no rewrite either -- a rewrite of "how do I
    pay?" would be worse than nothing -- and nothing offered that would only be refused."""
    walk = planned(client, conn)
    walk_fake.adjusts["how do I pay for this?"] = _read(support=asks, support_summary="needs something")
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, "how do I pay for this?", "cp-3")
    assert not res["isError"], tool_text(res)
    assert data(res) == {"changed": False}
    assert res["structuredContent"]["say"] == NOT_A_CHANGE and res["structuredContent"]["next"] == envelope.NONE
    assert len(walk_fake.plan_calls) == drafts and plans.latest(conn, "biz_1")["version"] == 1
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0


def test_with_support_off_a_complaint_that_names_the_budget_changes_it(client, conn, walk_fake):
    """AGENT_SUPPORT off, as on the homepage: the support read is set aside and the fields it read are adjust's
    change."""
    walk = planned(client, conn)
    words = "This plan is useless, make it $1,500 a month"
    walk_fake.adjusts[words] = _read(
        monthly_budget_usd=1500, understood=True, support="dissatisfied", support_summary="unhappy with the plan"
    )
    res = _change(client, walk, words, "cp-13")
    assert not res["isError"], tool_text(res)
    assert data(res)["version"] == 2 and data(res)["cards"]["budget"]["monthly_usd"] == 1500


def test_with_support_on_words_that_ask_for_a_person_go_to_the_admiry_team(client, conn, walk_fake, support_on):
    """AGENT_SUPPORT on: the command opens the business's ticket with the owner's words itself, as `answer` does; its
    mail goes out after the commit; no version is written."""
    walk = planned(client, conn)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    walk_fake.adjusts["let me talk to someone"] = _read(support="human", support_summary="wants a person")
    mailed = len(support_on.sent)
    res = _change(client, walk, "let me talk to someone", "cp-9")
    assert not res["isError"], tool_text(res)
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert (t["business_id"], t["source"], t["reason"], t["summary"]) == ("biz_1", "mcp", "human", "wants a person")
    words = conn.execute(
        "SELECT body FROM ticket_event WHERE ticket_id = %s AND kind = 'owner_words'", (t["id"],)
    ).fetchall()
    assert [w["body"] for w in words] == ["let me talk to someone"]
    assert data(res) == {"changed": False, "support": {"ticket_id": t["id"], "status": "open", "appended": False}}
    env = res["structuredContent"]
    assert (
        env["say"] == f"I've passed this to the Admiry team. They'll reply by e-mail to owner@example.com. {PLAN_TAIL}"
    )
    assert env["next"] == envelope.NONE
    assert plans.latest(conn, "biz_1")["version"] == 1
    assert [m["to"] for m in support_on.sent[mailed:]] == [["owner@example.com"], ["ops@example.com"]]
    newest = data(call_tool(client, "get_audit_log", {"limit": 1}, key=walk["key"]))["entries"][0]
    assert newest["sentence"] == NO_CHANGE_LOGGED


@pytest.mark.parametrize("asks", SUPPORT_READS)
def test_with_support_on_the_ticket_takes_the_turn_even_when_a_change_was_read(
    client, conn, walk_fake, support_on, asks
):
    """As on the homepage: whatever else the words say, the ticket takes the turn; the change waits for the owner to
    say it again."""
    walk = planned(client, conn)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    words = "This plan is useless, make it $1,500 a month"
    walk_fake.adjusts[words] = _read(
        monthly_budget_usd=1500, understood=True, support=asks, support_summary="needs the Admiry team"
    )
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, words, "cp-14")
    assert not res["isError"], tool_text(res)
    t = conn.execute("SELECT id, reason FROM support_ticket").fetchone()
    assert t["reason"] == asks
    assert data(res) == {"changed": False, "support": {"ticket_id": t["id"], "status": "open", "appended": False}}
    assert res["structuredContent"]["say"].endswith(PLAN_TAIL)
    assert len(walk_fake.plan_calls) == drafts and plans.latest(conn, "biz_1")["version"] == 1


def test_with_support_on_a_draft_key_is_told_to_sign_in_for_the_reply(client, conn, walk_fake, support_on):
    """A draft business has no contact address: the ticket waits for one, the sentence says to sign in, and `next`
    names the public tool that gives the sign-in link (a public name: the surface passes it on unchanged)."""
    walk = planned(client, conn, claim_state="draft")
    draft_key, _ = keys.mint(conn, business_id="biz_1", cls="draft", scopes=("read", "manage"), label="draft")
    walk_fake.adjusts["let me talk to someone"] = _read(support="human", support_summary="wants a person")
    res = call_tool(
        client,
        "change_plan",
        {"plan_id": walk["plan_id"], "words": "let me talk to someone", "idempotency_key": "cp-15"},
        key=draft_key,
    )
    assert not res["isError"], tool_text(res)
    assert data(res)["changed"] is False and data(res)["support"]["status"] == "awaiting_contact"
    env = res["structuredContent"]
    assert "Sign in with Google so they can reply to you by e-mail." in env["say"] and env["say"].endswith(PLAN_TAIL)
    sign_in = {"id": "sign_in", "label": "Sign in to get the reply", "tool": "connect_account", "primary": True}
    assert env["next"] == {"kind": "actions", "actions": [sign_in]}
    assert env["next"] == tools_support.SIGN_IN_ACTIONS  # contact_support's own action, written out in tools_plan
    assert plans.latest(conn, "biz_1")["version"] == 1


def test_the_log_says_what_the_words_did(client, conn, walk_fake):
    """A change reads as the new version; words that were not a change read as no change, never as "v?"."""
    walk = planned(client, conn)
    walk_fake.adjusts["make it $1,500 a month"] = _read(monthly_budget_usd=1500, understood=True)
    walk_fake.adjusts["let me talk to someone"] = _read(support="human", support_summary="wants a person")
    assert not _change(client, walk, "make it $1,500 a month", "cp-7")["isError"]
    assert not _change(client, walk, "let me talk to someone", "cp-8")["isError"]
    entries = data(call_tool(client, "get_audit_log", {"limit": 2}, key=walk["key"]))["entries"]
    assert [e["sentence"] for e in entries] == [
        NO_CHANGE_LOGGED,
        "Muse changed the plan into v2 from the owner's words",
    ]


NOT_MINE = "That place on the map is not my business"


def _listing_on_file(conn) -> dict | None:
    return profile.business_listing(business.latest_knowledge(conn, "biz_1")[1])


def test_words_that_say_the_listing_is_not_theirs_unlink_it_and_change_nothing(client, conn, walk_fake):
    """Plan 2026-10-03-agent-listing-not-mine-words, D4: after the plan, the words make no plan change and no version;
    the listing the owner picked is unlinked as Settings unlinks it, the answer says so and how to link the right
    one, and the activity log says it was unlinked. The same words again find nothing linked: their usual path."""
    walk = planned(client, conn)
    assert _listing_on_file(conn)["place_id"] == "ChIJ1"  # the walk picked it
    walk_fake.adjusts[NOT_MINE] = _read(listing_not_mine=True, asks_for_change=False)
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, NOT_MINE, "cp-40")
    assert not res["isError"], tool_text(res)
    assert data(res) == {"changed": False, "listing": "unlink"}
    # after the plan no consultation asks where the ads centre: a new version centres on the areas served (Task 2 round 1)
    assert res["structuredContent"]["say"] == (
        listings.UNLINKED.format(name="Bright Smile Dental") + listings.NO_CENTRE_LATER + listings.LINK_AGAIN
    )
    assert res["structuredContent"]["next"] == envelope.NONE
    assert _listing_on_file(conn) is None
    assert business.latest_knowledge(conn, "biz_1")[1]["listing_rejected"] == ["ChIJ1"]
    assert len(walk_fake.plan_calls) == drafts and plans.latest(conn, "biz_1")["version"] == 1
    newest = data(call_tool(client, "get_audit_log", {"limit": 1}, key=walk["key"]))["entries"][0]
    assert newest["sentence"] == "Muse unlinked the business's Google Maps listing"
    again = _change(client, walk, NOT_MINE, "cp-41")
    assert not again["isError"], tool_text(again)
    assert data(again) == {"changed": False} and again["structuredContent"]["say"] == NO_CHANGE_ASKED


def test_with_no_listing_linked_the_words_take_the_usual_kinds(client, conn, walk_fake):
    """Nothing linked (unlinked already, as Settings' "Not my business" does): the flag changes nothing -- a budget the
    words name is adjust's change, as ever."""
    walk = planned(client, conn)
    listings.unlink_listing(conn, "biz_1")
    walk_fake.adjusts[NOT_MINE] = _read(listing_not_mine=True, monthly_budget_usd=1500, understood=True)
    res = _change(client, walk, NOT_MINE, "cp-42")
    assert not res["isError"], tool_text(res)
    assert data(res)["version"] == 2 and data(res)["cards"]["budget"]["monthly_usd"] == 1500


def test_a_complaint_never_unlinks_the_listing(client, conn, walk_fake):
    """Review focus 1: "this plan is not what I wanted" is dislike, not a listing -- a read that sets only `support`."""
    walk = planned(client, conn)
    walk_fake.adjusts["this plan is not what I wanted"] = _read(
        support="dissatisfied", support_summary="unhappy with the plan", asks_for_change=False
    )
    res = _change(client, walk, "this plan is not what I wanted", "cp-43")
    assert not res["isError"], tool_text(res)
    assert data(res) == {"changed": False} and res["structuredContent"]["say"] == NOT_A_CHANGE
    assert _listing_on_file(conn)["place_id"] == "ChIJ1"


def test_a_change_named_with_the_unlink_is_not_made_and_the_answer_says_so(client, conn, walk_fake):
    """Task 1 fix round 1 (the controller's ruling): words that say the place is not theirs and name a change too
    ("not my place, and make it $1,500") unlink it, as D4 says; the change is not made, and the answer says to ask for
    it again -- never dropped without a word."""
    walk = planned(client, conn)
    words = "That place is not my business, and make it $1,500 a month"
    walk_fake.adjusts[words] = _read(listing_not_mine=True, monthly_budget_usd=1500, understood=True)
    res = _change(client, walk, words, "cp-44")
    assert not res["isError"], tool_text(res)
    assert data(res) == {"changed": False, "listing": "unlink"}
    assert res["structuredContent"]["say"] == (
        listings.UNLINKED.format(name="Bright Smile Dental")
        + listings.NO_CENTRE_LATER
        + listings.LINK_AGAIN
        + listings.NOTHING_ELSE
    )
    assert listings.NOTHING_ELSE == " Nothing else was changed: ask for the other change again."
    assert _listing_on_file(conn) is None and plans.latest(conn, "biz_1")["version"] == 1


def test_a_listing_unlinked_while_the_words_were_read_writes_nothing_and_claims_no_unlink(client, conn, walk_fake):
    """The race (Task 1 fix round 1): the listing is unlinked by another path -- Settings' "Not my business" -- after
    prepare saw it and before the body runs. The body finds nothing linked: it says so, writes nothing, and the
    activity log claims no unlink."""
    walk = planned(client, conn)
    read = _read(listing_not_mine=True, asks_for_change=False)

    def read_adjust(text, cards):
        listings.unlink_listing(conn, "biz_1")  # lands while the model reads the words, outside the command
        return read

    walk_fake.read_adjust = read_adjust
    before = business.latest_knowledge(conn, "biz_1")[0]
    res = _change(client, walk, NOT_MINE, "cp-45")
    assert not res["isError"], tool_text(res)
    assert data(res) == {"changed": False}
    assert res["structuredContent"]["say"] == listings.NOTHING_LINKED + listings.LINK_AGAIN
    assert business.latest_knowledge(conn, "biz_1")[0] == before + 1  # the other path's unlink, none of the command's
    newest = data(call_tool(client, "get_audit_log", {"limit": 1}, key=walk["key"]))["entries"][0]
    assert newest["sentence"] == NO_CHANGE_LOGGED
    assert plans.latest(conn, "biz_1")["version"] == 1


@pytest.mark.parametrize(
    ("budget", "refusal"),
    [(250_000, "monthly_budget_usd must be between 1 and 100,000"), (90_000, "a month is above")],
)
def test_a_value_adjust_refuses_is_refused_through_change_plan_too(client, conn, walk_fake, budget, refusal):
    """The model cannot talk its way past a check: what it read goes through adjust_prepare's own (its range, then the
    monthly limit in build), and a refusal is the answer -- never a rewrite instead."""
    walk = planned(client, conn)
    words = f"make it ${budget:,} a month"
    walk_fake.adjusts[words] = _read(monthly_budget_usd=budget, understood=True)
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, words, "cp-16")
    assert res["isError"] and refusal in tool_text(res)
    assert len(walk_fake.plan_calls) == drafts and plans.latest(conn, "biz_1")["version"] == 1


def test_change_plan_refuses_no_words_and_another_plan(client, conn, walk_fake):
    walk = planned(client, conn)
    blank = _change(client, walk, "   ", "cp-4")
    assert blank["isError"] and "say what to change" in tool_text(blank)
    other = call_tool(
        client, "change_plan", {"plan_id": "pl_nope", "words": "cheaper", "idempotency_key": "cp-5"}, key=walk["key"]
    )
    assert other["isError"] and "no plan with that id" in tool_text(other)


def test_adjust_still_behaves_as_before(client, conn, walk_fake):
    walk = planned(client, conn)
    res = call_tool(
        client,
        "adjust",
        {"plan_id": walk["plan_id"], "idempotency_key": "adj-1", "monthly_budget_usd": 1500},
        key=walk["key"],
    )
    assert not res["isError"] and data(res)["cards"]["budget"]["monthly_usd"] == 1500
    none = call_tool(client, "adjust", {"plan_id": walk["plan_id"], "idempotency_key": "adj-2"}, key=walk["key"])
    assert none["isError"] and "say what to change" in tool_text(none)


def test_a_question_is_answered_from_the_plan_and_only_from_it(client, conn, walk_fake):
    planned(client, conn)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")  # may ask
    question = "why Google Search first?"
    walk_fake.answers_to[question] = models.MailAnswer(
        text="People searching for Invisalign are ready to book, so that is where the plan starts.", grounded=True
    )
    res = call_tool(client, "ask", {"question": question}, key=reader)
    assert not res["isError"], tool_text(res)
    assert data(res) == {"grounded": True}
    say = res["structuredContent"]["say"]
    assert say.startswith("People searching for Invisalign") and not JARGON.search(say)
    material = walk_fake.answer_calls[-1]["material"]
    # `ways`: how each place the plan funds runs, and the other way it could (walk fixes 1, Task 6)
    assert set(material) == {"cards", "rationale", "knowledge", "ways"} and material["cards"]["plan"]["goal"]
    unknown = call_tool(client, "ask", {"question": "what is my neighbour's budget?"}, key=reader)
    assert data(unknown) == {"grounded": False}
    # AGENT_SUPPORT off (the default): no person on offer, since contact_support would refuse
    assert unknown["structuredContent"]["say"] == (
        "I cannot answer that from what I have on this business. Ask it another way."
    )
    assert unknown["structuredContent"]["next"] == envelope.NONE
    empty = call_tool(client, "ask", {"question": "  "}, key=reader)
    assert empty["isError"] and "ask a question" in tool_text(empty)


def test_ask_answers_a_business_with_no_plan_yet_from_what_it_knows(client, conn, walk_fake):
    """Knowledge on file and a consultation open, no plan from it yet: the material has no cards and no rationale, and
    a question is still answered -- grounded or not -- never an error."""
    walk = started(client, conn)
    known = "what do you know about my business?"
    walk_fake.answers_to[known] = models.MailAnswer(text="Bright Smile Dental offers Invisalign.", grounded=True)
    res = call_tool(client, "ask", {"question": known}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert data(res) == {"grounded": True}
    material = walk_fake.answer_calls[-1]["material"]
    assert material["cards"] is None and material["rationale"] is None and material["knowledge"]["name"]
    unknown = call_tool(client, "ask", {"question": "what will my plan cost?"}, key=walk["key"])
    assert not unknown["isError"], tool_text(unknown)
    assert data(unknown) == {"grounded": False}


def test_with_support_on_a_question_admiry_cannot_answer_offers_a_person(client, conn, walk_fake, support_on):
    walk = planned(client, conn)
    res = call_tool(client, "ask", {"question": "what is my neighbour's budget?"}, key=walk["key"])
    assert not res["isError"], tool_text(res)
    assert data(res) == {"grounded": False}
    assert res["structuredContent"]["say"] == (
        "I cannot answer that from what I have on this business. Ask it another way, or ask for a person and the "
        "Admiry team replies by e-mail."
    )
    assert [a["tool"] for a in res["structuredContent"]["next"]["actions"]] == ["contact_support"]


def test_a_budget_sentence_after_400_characters_still_changes_the_budget(client, conn, walk_fake):
    """The reader gets the whole words, so a budget sentence late in a long message is adjust's own change."""
    walk = planned(client, conn)
    words = "We had a busy summer and the phones rang all day long. " * 8 + "Make it $1,500 a month."
    assert words.index("Make it") > 400 and len(words) <= 1000
    walk_fake.adjusts[words] = _read(monthly_budget_usd=1500, understood=True)  # keyed by the whole text
    drafts = len(walk_fake.plan_calls)
    res = _change(client, walk, words, "cp-10")
    assert not res["isError"], tool_text(res)
    assert data(res)["cards"]["budget"]["monthly_usd"] == 1500 and data(res)["version"] == 2
    assert len(walk_fake.plan_calls) == drafts and walk_fake.adjust_calls[-1]["text"] == words


def test_more_than_1000_characters_are_refused_before_the_model_never_cut(client, conn, walk_fake):
    walk = planned(client, conn)
    assert len(LONGEST + "!") == 1001
    calls = (len(walk_fake.adjust_calls), len(walk_fake.plan_calls), len(walk_fake.answer_calls))
    last = conn.execute("SELECT max(id) AS id FROM agent_action").fetchone()["id"]

    change = _change(client, walk, LONGEST + "!", "cp-11")
    question = call_tool(client, "ask", {"question": LONGEST + "?"}, key=walk["key"])

    assert change["isError"] and "say what to change in at most 1,000 characters" in tool_text(change)
    assert question["isError"] and "ask the question in at most 1,000 characters" in tool_text(question)
    assert (len(walk_fake.adjust_calls), len(walk_fake.plan_calls), len(walk_fake.answer_calls)) == calls
    rows = conn.execute("SELECT tool, ok FROM agent_action WHERE id > %s ORDER BY id", (last,)).fetchall()
    assert [(r["tool"], r["ok"]) for r in rows] == [("change_plan", False), ("ask", False)]  # the refusals' own
    assert plans.latest(conn, "biz_1")["version"] == 1


def test_1000_characters_reach_the_reader_the_rewrite_and_the_answerer_whole(client, conn, walk_fake):
    walk = planned(client, conn)
    assert len(LONGEST) == 1000
    res = _change(client, walk, LONGEST, "cp-12")  # names no field adjust has: a rewrite
    assert not res["isError"], tool_text(res)
    assert walk_fake.adjust_calls[-1]["text"] == LONGEST and walk_fake.plan_calls[-1]["instruction"] == LONGEST
    asked = call_tool(client, "ask", {"question": LONGEST}, key=walk["key"])
    assert not asked["isError"], tool_text(asked)
    assert walk_fake.answer_calls[-1]["question"] == LONGEST


def test_ask_and_change_plan_count_against_the_daily_model_limit(client, conn, monkeypatch, walk_fake):
    """Both call the model: auth.LLM_TOOLS, one at a time per business and counted per day. Once the business's
    limit is reached both are refused before the model is called -- a read-only key's question included."""
    walk = planned(client, conn)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="claude")
    assert {"ask", "change_plan"} <= set(auth.LLM_TOOLS)
    monkeypatch.setattr(auth, "LLM_CALLS_PER_DAY", 0)  # both keys are business keys: their limit, already reached
    answered, read = len(walk_fake.answer_calls), len(walk_fake.adjust_calls)

    question = call_tool(client, "ask", {"question": "why Google Search first?"}, key=reader)
    change = _change(client, walk, "make it $1,500 a month", "cp-6")

    assert question["isError"] and "today's limit" in tool_text(question)
    assert len(walk_fake.answer_calls) == answered  # the model never saw the question
    assert change["isError"] and "today's limit" in tool_text(change)
    assert len(walk_fake.adjust_calls) == read and plans.latest(conn, "biz_1")["version"] == 1
