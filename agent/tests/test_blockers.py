"""B0 (spec 2026-10-08 §3.1, §3.2): what stops a launch, as the home says it -- agent/blockers.py over the launch's own
check, for test_ads_run's ready business (`seed`: its own Google Ads account linked, a published page that passed its
check, a phone, a point on the map) with its plan a draft and the Google switch live: each refusal's kind and its
controls, the budget choices that pass, the funds and the account left to the launch card until the account step is
done, and a refusal no row knows said alone."""

import pytest
from psycopg.types.json import Jsonb

from agent import ads_mode, ads_run, agreements, blockers, copy, landing, operations, plans, proposals
from agent.channels.google_search import adapter as google
from agent.tests.test_ads_run import BIZ, PAGE_URL, _check_failed, _knowledge, _no_link, _no_page, _unchecked, seed
from agent.tests.test_review_launch import draft_sections

LEVELS = {"minimum": 300, "conservative": 1000, "recommended": 2000, "aggressive": 4000}
MESSAGE_FIX = "Say the service and the city in the headline, like the ad: “Invisalign in Palo Alto”."


@pytest.fixture
def live(monkeypatch):
    """The Google switch on, as test_ads_run's `live` (this file's own: no fixture imported under a test's argument)."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")


def _draft(conn, secs: dict | None = None, **kw) -> dict:
    """The ready business with its plan a draft: `secs`, else `draft_sections(**kw)` (the consultation's cards too)."""
    seed(conn, status="draft", secs=secs or draft_sections(**kw))
    return plans.latest(conn, BIZ)


def _blocker(conn, *, funded: bool = True) -> dict | None:
    """What B0 said for the latest draft (`blockers.describe` over the launch's own refusal) -- the resolution table
    these tests pin; the home reads it through the checklist now (agent/launch_check.py)."""
    plan = plans.latest(conn, BIZ)
    if plan is None or plan["status"] != plans.DRAFT:
        return None
    found = blockers.describe(conn, BIZ, plan, proposals.launch_refusal(conn, BIZ, plan["id"]))
    if found is not None and not funded and found["resolution"] in ("funds", "account"):
        return None
    return found


def _said(blocked: str) -> str:
    return blockers.SAY.format(blocked=blocked.rstrip("."))


def test_nothing_stands_in_the_way_of_a_ready_draft(conn, live):
    _draft(conn)
    assert _blocker(conn) is None


def test_with_every_switch_off_nothing_is_said(conn):
    _draft(conn)
    _knowledge(conn, phone=None)
    assert _blocker(conn) is None  # the launch's check asks nothing of a place whose switch is off


def test_an_approved_plan_has_nothing_in_its_way(conn, live):
    seed(conn, status="approved", secs=draft_sections())
    _knowledge(conn, phone=None)
    assert _blocker(conn) is None


def test_the_phone_is_one_question_asked_before_the_account_step_too(conn, live):
    plan = _draft(conn)
    _knowledge(conn, phone=None)
    assert _blocker(conn, funded=False) == {
        "plan_id": plan["id"],
        "id": "phone",
        "kind": "ask",
        "resolution": "phone",
        "text": ads_run.NO_PHONE,
        "say": _said(ads_run.NO_PHONE),
        "question": {"input": "tel", "placeholder": "The business phone, with its area code"},
        "actions": [],
    }


def test_the_place_is_a_question(conn, live):
    _draft(conn)
    _knowledge(conn, listing=None, service_area=[])
    found = _blocker(conn)
    assert (found["id"], found["kind"]) == ("location", "ask")
    assert found["question"] == {"input": "address", "placeholder": "The address the ads should centre on"}


def test_the_miles_are_a_question(conn, live):
    _draft(conn, radius_miles=None)
    found = _blocker(conn)
    assert found["id"] == "radius"
    assert found["question"] == {"input": "miles", "placeholder": "How many miles around the business"}


def test_admiry_writes_the_ad_text(conn, live):
    _draft(conn, headlines=["Invisalign in Palo Alto"])
    found = _blocker(conn)
    assert found["text"] == google.TOO_LITTLE_COPY and found["kind"] == "fix"
    assert found["actions"] == [blockers.WRITE_COPY] and found["question"] is None


def test_admiry_draws_a_fresh_plan(conn, live):
    _draft(conn, secs={**draft_sections(), "estimate": {"basis": "none"}})
    found = _blocker(conn)
    assert found["text"] == google.NOT_SEARCH_PLAN and found["actions"] == [blockers.FRESH_PLAN]


def test_a_missing_page_is_the_owners_choice(conn, live):
    _draft(conn)
    _no_page(conn)
    found = _blocker(conn)
    assert (found["text"], found["kind"]) == (ads_run.NO_PAGE, "decide")
    assert found["actions"] == [blockers.BUILD_SITE, blockers.OWN_SITE]
    # fix round 2b, item 5 (Addendum 2's rule for the page): two ways to a page offered together, neither leads --
    # same style, the order unchanged
    assert [a["id"] for a in found["actions"]] == ["build_site", "own_site"]
    assert not any(a.get("primary") for a in found["actions"])


def test_an_unchecked_page_is_checked_now(conn, live):
    _draft(conn)
    _unchecked(conn)
    found = _blocker(conn)
    assert found["text"] == ads_run.PAGE_UNCHECKED.format(label="Google Search")
    assert found["actions"] == [blockers.CHECK_NOW]


def _message_report(conn) -> None:
    """The page's saved report failing Message-match on a headline that names no lead service of the plan."""
    report = {
        "ok": False,
        "url": PAGE_URL,
        "headline": "Dentist in Palo Alto",
        "rules": [
            {"id": "reachable", "ok": True, "fix": None},
            {"id": "message_match", "ok": False, "fix": MESSAGE_FIX, "detail": "Headline: “Dentist in Palo Alto”."},
        ],
    }
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(report),))


def test_the_headline_fix_is_offered_only_for_admirys_own_page(conn, live):
    """B0 P6, revised by R4 (2026-10-12): Message-match's fix on the owner's own page is theirs to make -- a warning
    there now, never a blocker, so nothing stands in the way on it alone; on Admiry's starter page or Admiry's site,
    Admiry puts the plan's headline there itself."""
    _draft(conn)
    _message_report(conn)
    assert _blocker(conn) is None  # R4: a warning on the owner's own page (source 'owner'), never a blocker
    conn.execute(
        "UPDATE page SET sections = %s, source = 'admiry' WHERE business_id = %s",
        (Jsonb({"copy": {"headline": "Dentist in Palo Alto"}}), BIZ),
    )
    found = _blocker(conn)
    assert found["text"] == ads_run.PAGE_FIX.format(fix=MESSAGE_FIX)
    assert found["actions"] == [blockers.FIX_HEADLINE]  # the starter page Admiry wrote
    conn.execute(
        "UPDATE page SET sections = %s WHERE business_id = %s", (Jsonb({"kind": "site", "site_spec_id": "ss_1"}), BIZ)
    )
    assert _blocker(conn)["actions"] == [blockers.FIX_HEADLINE]  # Admiry's site
    page = landing.current(conn, BIZ)
    assert blockers.admiry_page(conn, page) == "site"
    assert blockers.headline_fix(conn, BIZ, "Your page needs a fix before your ads can send people there: x") is None


def test_another_rule_of_the_page_is_fixed_by_the_owner_and_checked_again(conn, live):
    _draft(conn)
    _check_failed(conn)
    found = _blocker(conn)
    assert found["resolution"] == "page_fix" and found["actions"] == [blockers.CHECK_AGAIN]


def test_over_the_limit_the_choices_are_the_limit_and_the_levels_under_it(conn, live):
    _draft(conn, secs={**draft_sections(monthly_usd=2500.0), "levels": LEVELS})  # the goal's limit is $2,000
    found = _blocker(conn)
    assert (found["text"], found["kind"]) == (ads_run.OVER_LIMIT, "decide")
    assert found["actions"] == [
        {"id": "budget", "label": "$66 a day", "args": {"monthly_budget_usd": 2000}, "primary": True},
        {"id": "budget", "label": "$33 a day", "args": {"level": "conservative"}},
        {"id": "budget", "label": "$10 a day", "args": {"level": "minimum"}},
    ]


def test_under_the_floor_the_choices_raise_the_place_to_it(conn, live):
    _draft(conn, secs={**draft_sections(monthly_usd=50.0), "levels": LEVELS})  # Google's own-account floor: $100
    found = _blocker(conn)
    assert found["text"] == ads_run.UNDER_FLOOR.format(label="Google Search", floor="$100")
    assert found["actions"] == [
        {"id": "budget", "label": "$3 a day", "args": {"monthly_budget_usd": 100}, "primary": True},
        {"id": "budget", "label": "$10 a day", "args": {"level": "minimum"}},
        {"id": "budget", "label": "$33 a day", "args": {"level": "conservative"}},
    ]


def test_under_the_floor_the_choices_name_the_switched_on_place_the_refusal_is_about(conn, monkeypatch):
    """Review Focus: the launch checks only the places whose switch is on; with Google Search off and NewsBreak on, both
    under their floors, the refusal and its choices are NewsBreak's, never Google's."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "off")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    secs = draft_sections(monthly_usd=50.0)
    secs["ads"]["channels"].append({"channel": "newsbreak", "monthly_usd": 100.0})
    _draft(conn, secs=secs)
    found = _blocker(conn)
    assert found["text"] == ads_run.UNDER_FLOOR.format(label="NewsBreak", floor="$300")
    assert found["actions"] == [
        {
            "id": "budget",
            "label": "NewsBreak at $300 a month",
            "args": {"channel": "newsbreak", "channel_monthly_usd": 300},
            "primary": True,
        }
    ]


def test_the_account_and_the_funds_wait_for_the_launch_card_until_the_account_step_is_done(conn, live):
    """L1: before the account step is done the launch card offers the ways to run the ads, not a blocker; once it is
    (`funded`), the funds and the account blockers are said. The account's two ways side by side, neither marked
    first (founder 2026-10-13: the home shows both ways equally); and while the owner has chosen no way (Addendum 3),
    the words that would pick one -- "since your ads run in your own account" -- are the neutral line instead."""
    plan = _draft(conn)
    _no_link(conn)  # no link, no agreement, the default mode: no way chosen yet
    assert _blocker(conn, funded=False) is None
    found = _blocker(conn, funded=True)
    assert found["text"] == ads_mode.WAY_LINE and found["kind"] == "decide" and found["resolution"] == "account"
    assert found["id"] == "account"  # page-first review, item 2: the ask's id is read off the refusal as given
    assert found["say"] == blockers.SAY.format(blocked=ads_mode.WAY_LINE) and google.NOT_LINKED not in found["say"]
    assert found["actions"] == [blockers.PREPAY, blockers.LINK]
    agreements.accept(conn, business_id=BIZ, email="owner@example.com", ip="local")  # on the way to its own account
    found = _blocker(conn, funded=True)
    assert found["text"] == google.NOT_LINKED and found["kind"] == "decide" and found["id"] == "account"
    assert found["actions"] == [blockers.PREPAY, blockers.LINK]
    funds = blockers.describe(conn, BIZ, plan, ads_run.NO_FUNDS.format(label="Google Search"))
    assert funds["resolution"] == "funds" and funds["actions"] == [blockers.ADD_FUNDS]


def test_a_refusal_no_row_knows_is_said_alone_with_nothing_to_press(conn, live):
    plan = _draft(conn)
    found = blockers.describe(conn, BIZ, plan, "Something the launch never says.")
    assert found == {
        "plan_id": plan["id"],
        "id": None,
        "kind": None,
        "resolution": None,
        "text": "Something the launch never says.",
        "say": "Before your ads can start: Something the launch never says.",
        "question": None,
        "actions": [],
    }
    assert blockers.describe(conn, BIZ, plan, None) is None


def test_the_words_are_the_owners_and_send_nowhere():
    controls = (
        blockers.WRITE_COPY,
        blockers.FIX_HEADLINE,
        blockers.CHECK_NOW,
        blockers.CHECK_AGAIN,
        blockers.BUILD_SITE,
        blockers.OWN_SITE,
        blockers.ADD_FUNDS,
        blockers.PREPAY,
        blockers.LINK,
        blockers.FRESH_PLAN,
    )
    for text in (blockers.SAY, *(c["label"] for c in controls)):
        assert copy.jargon(text) is None, text
    assert all("tool" not in c for c in controls)  # the contract's static scan reads every "tool" key


@pytest.mark.parametrize(
    "words,address",
    [
        ("500 University Ave, Palo Alto, CA", "500 University Ave, Palo Alto, CA"),
        ("500 University Ave", "500 University Ave"),
        ("my address is 500 University Ave, Palo Alto, CA", "500 University Ave, Palo Alto, CA"),
        ("we're at 12 Main St", "12 Main St"),
        ("Palo Alto, CA", "Palo Alto, CA"),
        ("Palo Alto, CA 94301", "Palo Alto, CA 94301"),
        ("94301", "94301"),
        # fix round 2: a unit's own further parts, in order -- a unit before the town, the country after the state
        ("500 University Ave, Palo Alto, CA 94301", "500 University Ave, Palo Alto, CA 94301"),
        ("500 University Ave Apt 2, Palo Alto, CA 94301", "500 University Ave Apt 2, Palo Alto, CA 94301"),
        ("500 University Ave, Palo Alto, CA, USA", "500 University Ave, Palo Alto, CA, USA"),
        ("change the area to Palo Alto, CA", None),  # ends like an address; asks for something else (Review Focus 3)
        ("change the budget to $10 at 5 Main St", None),  # contains one; asks for something else
        ("make it 10", None),
        ("sure, ok", None),  # lowercase: no town (the state's two capitals are case-sensitive)
        ("put $300 on newsbreak", None),
        ("what colour is the sky", None),
        # fix round 2: a street-shaped opening whose tail is plain English, not one of an address's own further parts
        ("500 University Ave do something else entirely", None),
        ("we're at 12 Main St also raise the budget please", None),
        ("at 5 Main St send a text to my friend", None),
        ("123 Main St, please also increase the daily budget to fifty", None),
        ("my address is 500 University Ave and also change the offer", None),
        # MAX_ANSWER + 1 characters, address-shaped throughout: the length is checked before any regex runs
        ("500 University Ave, " + "x" * (blockers.MAX_ANSWER + 1 - len("500 University Ave, ")), None),
        # fix round 3: the town and the state after a street must be written as names, not any short plain-English tail
        ("500 University Ave, call me", None),
        ("500 University Ave, change my budget", None),
        ("500 University Ave, call me please", None),
        ("my address is 500 University Ave, also call me", None),
        ("12 Main St ok", None),
        ("500 University Ave unit economics", None),
        ("500 University Ave, Change my budget", None),  # one capitalised word is not a town of capitalised words
        ("500 University Ave. Change my budget", None),
        ("500 University Ave apt to change", None),  # "to" is neither a number nor a single letter
        ("12 Main St, ok", None),
        ("500 University Ave Unit B, Palo Alto, CA 94301", "500 University Ave Unit B, Palo Alto, CA 94301"),
        ("500 University Ave #12", "500 University Ave #12"),
        ("500 University Ave Suite 200", "500 University Ave Suite 200"),
        ("500 University Ave, Palo Alto CA 94301", "500 University Ave, Palo Alto CA 94301"),
        ("123 St. Louis Ave, St. Louis, MO 63101", "123 St. Louis Ave, St. Louis, MO 63101"),
        ("500 University Ave 94301", "500 University Ave 94301"),
        ("500 University Ave, Palo Alto", "500 University Ave, Palo Alto"),
        ("500 university ave", "500 university ave"),
        ("it's at 500 University Ave.", "500 University Ave."),  # after its lead-in; a final period may stay
        ("500 University Ave Palo Alto CA", None),  # accepted (rounds 2-3): the question's own field takes these
        ("500 university ave, palo alto, ca", None),  # accepted (rounds 2-3): the question's own field takes these
        # final review m6: names, not words -- a state is one of the real two-letter codes, and a town made only of
        # reply words is no town, after a street as alone
        ("Yes, OK", None),
        ("Thanks, OK", None),
        ("No, NO", None),
        ("Fine, GO", None),
        ("Hi, ME", None),
        ("500 University Ave, Palo Alto, GO", None),
        ("12 Main St, Thanks", None),
        ("Tulsa, OK", "Tulsa, OK"),
        ("Portland, ME 04101", "Portland, ME 04101"),
    ],
)
def test_an_address_typed_in_the_conversation_is_taken_only_as_a_whole(words, address):
    """Fix rounds 1-3 (Review Focus 3, plan P10): a Places lookup is never spent on words that ask for something else
    -- ending like an address, merely containing one, or opening like one with plain English after -- is not enough;
    the whole text (after at most one short lead-in) must be the address, its own further parts (a unit, the town,
    the state, the ZIP, the country) and nothing else, and no longer than any other answer. What is taken is the address
    after its lead-in (final review I4): the map is asked about it, and nothing else."""
    assert len(words) <= blockers.MAX_ANSWER or address is None  # guards the table itself, not just read_typed
    assert blockers.read_typed({"kind": "ask", "id": "location"}, words) == address


@pytest.mark.parametrize(
    ("words", "taken"),
    [
        ("(650) 555-0100", "(650) 555-0100"),
        ("my number is 650-555-0100", "650-555-0100"),
        ("it's 650.555.0100", "650.555.0100"),
        ("it’s 650.555.0100", "650.555.0100"),  # a phone keyboard's curly apostrophe
        ("my phone is 650-555-0100", "650-555-0100"),
        ("call me at 650 555 0100", "650 555 0100"),
        ("here's my number: 650-555-0100.", "650-555-0100"),
        ("phone number: (650) 555-0100", "(650) 555-0100"),
        ("tel: 650-555-0100", "650-555-0100"),
        ("our phone is +1 650 555 0100", "+1 650 555 0100"),
        ("my number's 650-555-0100", "650-555-0100"),
        ("mobile 650 555 0100", "650 555 0100"),
        ("cell 650-555-0100", "650-555-0100"),
        ("you can reach us on 650 555 0100", "650 555 0100"),
        ("it is 650.555.0100", "650.555.0100"),
        ("my number is 650 555 0100, and make it $20 a day", None),  # a change too: the adjust read's
        ("don't use 650-555-0100, that's the old one", None),
        ("don't use 650-555-0100", None),
        ("my old number is 650-555-0100", None),
        ("not 650-555-0100", None),
        ("room204 650-555-0100", None),  # a lead word with digits (F5: none)
        ("my customer id is 123-456-7890", None),  # a Google Ads Customer ID (follow-up final review M3)
        ("customer id: 123-456-7890", None),
        ("link my google ads account 123-456-7890", None),
    ],
)
def test_a_phone_typed_in_the_conversation_is_the_whole_answer_after_a_short_lead_in(words, taken):
    """Follow-up R3 (F5): any short lead-in without digits or a negation, the phone, nothing after it but a period."""
    assert blockers.read_typed({"kind": "ask", "id": "phone"}, words) == taken


def test_a_curly_apostrophe_reads_as_a_straight_one_in_an_address_too():
    assert blockers.read_typed({"kind": "ask", "id": "location"}, "we’re at 12 Main St") == "12 Main St"


@pytest.mark.parametrize(
    "words,phone",
    [
        ("(650) 555-0100", "(650) 555-0100"),
        ("my number is 650-555-0100", "650-555-0100"),
        ("it's 650.555.0100", "650.555.0100"),
        ("my number is 650 555 0100", "650 555 0100"),
        ("+1 650-555-0100", "+1 650-555-0100"),
        ("our business phone number is (650) 555-0100.", "(650) 555-0100"),
        ("the number is 650-555-0100", "650-555-0100"),
        ("call 650-555-0100", "650-555-0100"),
        ("Phone: 650-555-0100", "650-555-0100"),
        ("my number is 650 555 0100, and make it $20 a day", None),  # a change after it: the adjust read's
        ("don't use 650-555-0100, that's the old one", None),  # words about a number are not the number
        ("set the budget to 1000 a month", None),
        (
            "budget 6505550100",
            "6505550100",
        ),  # any short lead-in (follow-up R3): with the phone question open, the digits are the phone
        ("1500", None),
    ],
)
def test_a_phone_typed_in_the_conversation_is_taken_only_as_the_whole_answer(words, phone):
    """Final review m7: the composer's words are the phone only when the whole text, after an optional short lead-in, is
    a phone in its shape and nothing after it but a period -- anything else goes to the adjust read, which keeps a
    change asked beside the number. The question's own field (`read_answer`) still takes the phone it finds anywhere;
    the miles were read whole already (`_MILES.fullmatch`)."""
    assert blockers.read_typed({"kind": "ask", "id": "phone"}, words) == phone
    assert blockers.read_answer("phone", "my number is 650 555 0100, and make it $20 a day") == "650 555 0100"
    miles = {"kind": "ask", "id": "radius"}
    assert blockers.read_typed(miles, "within 15 miles") == 15
    assert blockers.read_typed(miles, "15 miles, and make it $20 a day") is None


def test_what_each_control_runs_is_safe_or_confirmed_by_the_owner():
    """B0 P8 (operations read by the card): a control the home runs at the owner's tap or from their words runs only
    safe commands; one that runs a dangerous command says how the owner confirms it on Admiry's own surface."""
    drawn = {
        c["id"]
        for c in (
            blockers.WRITE_COPY,
            blockers.FIX_HEADLINE,
            blockers.CHECK_NOW,
            blockers.BUILD_SITE,
            blockers.OWN_SITE,
            blockers.ADD_FUNDS,
            blockers.PREPAY,
            blockers.LINK,
            blockers.FRESH_PLAN,
        )
    } | {"budget", "answer", "publish"}
    assert drawn == set(blockers.RUNS)
    for control, (commands, confirmed) in blockers.RUNS.items():
        dangerous = [c for c in commands if operations.kind(c) == "dangerous"]
        assert bool(dangerous) == bool(confirmed), control
    assert all(operations.kind(c) == "safe" for c in blockers.RUNS["answer"][0])  # typed answers (§3.3) included


@pytest.mark.parametrize("name", ["NO_RADIUS", "OVER_LIMIT", "UNDER_FLOOR", "TOO_LITTLE_COPY"])
def test_a_plan_no_consultation_made_is_offered_no_fix_that_runs_adjust(conn, name):
    """Follow-up R5 (F3): `adjust` takes only a plan a consultation made, so for an assistant's plan
    (create_marketing_plan: no `consultation` section) the budget choices, the miles question and Admiry writing the
    ad text are not offered -- the refusal is said alone, as one no row knows (P12): nothing to press that cannot run."""
    from agent import ads_run
    from agent.channels.google_search import adapter as google

    text = (getattr(ads_run, name, None) or getattr(google, name)).format(label="Google Search", floor="$200", fix="x")
    found = blockers.describe(conn, "biz_1", {"id": "pl_0000000000000001", "sections": {}}, text)
    assert (found["id"], found["kind"], found["resolution"], found["question"], found["actions"]) == (
        None,
        None,
        None,
        None,
        [],
    )
    assert found["say"] == blockers.SAY.format(blocked=text.rstrip("."))
