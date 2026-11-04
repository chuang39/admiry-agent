"""plan_edit: the "Your ads" page's three edits as a preview that stores nothing and a commit that writes the draft
(spec 2026-10-06-admiry-your-ads-page-design.md §3, Y5–Y8, Y13)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from agent import ads_run, benchmark, business, plan_edit, tools_consult
from agent.tests.test_ads_run import live  # noqa: F401 - the Google switch on, a fixture
from agent.tests.test_tools_consult import _home_plan, fake, recommended_with  # noqa: F401 - the model fake, a fixture

WORDS_H = ["One Headline Here", "Two Headline Here", "Three Headline Here"]
WORDS_D = ["The first description of the ad.", "The second description of the ad."]


def approved_plan(client, conn, fake) -> dict:  # noqa: F811 - the fixture
    """A consulted plan for biz_1 ($2,000 limit, Google Search at $2,000), approved."""
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (r["plan_id"],))
    return ads_run.approved(conn, "biz_1")


def two_platforms(client, conn, limit: int) -> dict:
    """A prepaid roofer's consulted plan at its owner's `limit` a month, approved: the allocator funds Google Search
    and gives NewsBreak its band's share, not a pin -- $1,700 and $300 of $2,000, $2,550 and $450 of $3,000 -- and
    NewsBreak's words are a copy of Google Search's. The test needs `fake`, `newsbreak_on` and `pictures`."""
    benchmark.seed(conn)
    _key, r = _home_plan(client, conn, service="svc_roofing", limit=limit, prepaid=True)
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (r["plan_id"],))
    return ads_run.approved(conn, "biz_1")


def _plans(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"]


def _abandoned_draft(conn, base: dict, **edit) -> dict:
    """What an assistant's `adjust` leaves behind when the owner never approves it: a draft from the approved version
    with `edit`, whose answers stay recorded on the consultation."""
    return tools_consult.adjust_commit(conn, "biz_1", tools_consult.adjust_prepare(conn, "biz_1", base["id"], **edit))


def test_day_usd_is_the_platforms_figure_floored_to_the_cent():
    assert plan_edit.day_usd(400) == 13.15 and plan_edit.day_usd(2000) == 65.78 and plan_edit.day_usd(300) == 9.86


def test_a_budget_preview_moves_one_platform_and_the_total_and_stores_nothing(client, conn, fake, live):  # noqa: F811 - the fixtures
    base = approved_plan(client, conn, fake)
    pv = plan_edit.preview(conn, "biz_1", base["id"], plan_edit.Change("budget", channel="google_search", amount=1500))
    assert pv.rows == [
        {
            "channel": "google_search",
            "label": "Google Search",
            "old_usd": 2000.0,
            "new_usd": 1500.0,
            "day_usd": 49.34,
            "changed": True,
        }
    ]
    assert (pv.total_old, pv.total_new) == (2000.0, 1500.0) and pv.words == {}
    assert pv.next_steps == [
        {"channel": "google_search", "label": "Google Search", "sentence": "Google Search gets the new amount now."}
    ]
    assert _plans(conn) == 1


def test_the_switch_off_says_so_in_the_next_steps(client, conn, fake):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)
    pv = plan_edit.preview(conn, "biz_1", base["id"], plan_edit.Change("budget", channel="google_search", amount=1500))
    assert pv.next_steps[0]["sentence"] == ads_run.OFF_CHANNEL.format(label="Google Search")


# an amount on a plan with two platforms (final review I1): the other platform is the allocator's share of the new
# total, which holds within the plan's band; across a band it may move or go, and What happens next says so for each

GOOGLE_NOW = {
    "channel": "google_search",
    "label": "Google Search",
    "sentence": "Google Search gets the new amount now.",
}


def _amounts(pv: plan_edit.Preview) -> list[tuple]:
    return [(r["channel"], r["old_usd"], r["new_usd"], r["changed"]) for r in pv.rows]


def _google(amount: int) -> plan_edit.Change:
    return plan_edit.Change("budget", channel="google_search", amount=amount)


def test_an_amount_raised_within_the_band_holds_the_other_platform_and_names_only_the_edited_one(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    live,  # noqa: F811 - the fixture
    newsbreak_on,
    pictures,
):
    base = two_platforms(client, conn, 2000)
    business.set_goal(conn, "biz_1", monthly_limit_usd=3000)  # room above the plan's $2,000 to raise it
    pv = plan_edit.preview(conn, "biz_1", base["id"], _google(2000))
    assert _amounts(pv) == [("google_search", 1700.0, 2000.0, True), ("newsbreak", 300.0, 300.0, False)]
    assert (pv.total_old, pv.total_new) == (2000.0, 2300.0) and pv.next_steps == [GOOGLE_NOW]


def test_an_amount_that_takes_the_total_under_the_other_platforms_band_says_it_is_left_out(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    live,  # noqa: F811 - the fixture
    newsbreak_on,
    pictures,
):
    """The review's own case: $2,000 (Google Search $1,700, NewsBreak $300), Google Search lowered to $400 -- the new
    total, $700, is under the band that funds NewsBreak, so the version leaves it out and its ads go off."""
    base = two_platforms(client, conn, 2000)
    pv = plan_edit.preview(conn, "biz_1", base["id"], _google(400))
    assert _amounts(pv) == [("google_search", 1700.0, 400.0, True), ("newsbreak", 300.0, None, True)]
    assert pv.next_steps == [
        GOOGLE_NOW,
        {
            "channel": "newsbreak",
            "label": "NewsBreak",
            "sentence": "NewsBreak: not in this version; Admiry switches its ads off.",
        },
    ]


def test_an_amount_that_takes_the_total_into_a_lower_band_says_what_the_other_platform_gets(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    live,  # noqa: F811 - the fixture
    newsbreak_on,
    pictures,
):
    """$3,000 (Google Search $2,550, NewsBreak $450), Google Search lowered to $1,000: the new total, $1,450, gives
    NewsBreak its $300 test, not a share of $450."""
    base = two_platforms(client, conn, 3000)
    pv = plan_edit.preview(conn, "biz_1", base["id"], _google(1000))
    assert _amounts(pv) == [("google_search", 2550.0, 1000.0, True), ("newsbreak", 450.0, 300.0, True)]
    assert pv.next_steps == [
        GOOGLE_NOW,
        {
            "channel": "newsbreak",
            "label": "NewsBreak",
            "sentence": "NewsBreak gets $300 a month now, the most this total gives it.",
        },
    ]


def test_a_left_out_platform_whose_switch_is_off_reads_the_switch_sentence(
    client,
    conn,
    fake,  # noqa: F811 - the fixture
    live,  # noqa: F811 - the fixture
    newsbreak_on,
    pictures,
    monkeypatch,
):
    """NewsBreak switched off since the approval: no new version funds it, and its sentence is the switch's."""
    base = two_platforms(client, conn, 2000)
    monkeypatch.delenv("AGENT_EXEC_NEWSBREAK")
    pv = plan_edit.preview(conn, "biz_1", base["id"], _google(1600))
    assert _amounts(pv)[1] == ("newsbreak", 300.0, None, True)
    assert pv.next_steps == [
        GOOGLE_NOW,
        {"channel": "newsbreak", "label": "NewsBreak", "sentence": ads_run.OFF_CHANNEL.format(label="NewsBreak")},
    ]


def test_an_area_preview_changes_the_radius_and_no_amount(client, conn, fake, live):  # noqa: F811 - the fixtures
    base = approved_plan(client, conn, fake)
    pv = plan_edit.preview(conn, "biz_1", base["id"], plan_edit.Change("area", radius=25))
    assert (pv.radius_old, pv.radius_new) == (5.0, 25.0)
    assert pv.rows[0]["changed"] is False and pv.total_old == pv.total_new == 2000.0
    assert pv.next_steps == [
        {"channel": "google_search", "label": "Google Search", "sentence": "Google Search redraws the area now."}
    ]


def test_a_words_preview_keeps_the_passing_lines_and_notes_the_dropped_one(client, conn, fake, live):  # noqa: F811 - the fixtures
    base = approved_plan(client, conn, fake)
    change = plan_edit.Change(
        "words", channel="google_search", headlines=("Call now!!", *WORDS_H), descriptions=tuple(WORDS_D)
    )
    pv = plan_edit.preview(conn, "biz_1", base["id"], change)
    assert pv.words == {
        "google_search": {
            "label": "Google Search",
            "headlines": WORDS_H,
            "descriptions": WORDS_D,
            "notes": ['Dropped a headline ("Call now!!"): it has an exclamation mark.'],
        }
    }
    assert pv.rows[0]["changed"] is False
    assert pv.next_steps[0]["sentence"].startswith("Google Search reviews new words before they show")
    assert pv.ctx[4][0]["ads"]["channels"][0]["headlines"] == WORDS_H


def test_too_few_passing_lines_is_refused_with_the_notes(client, conn, fake):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)
    change = plan_edit.Change(
        "words", channel="google_search", headlines=("Call now!!", "Only One"), descriptions=tuple(WORDS_D)
    )
    with pytest.raises(ToolError) as e:
        plan_edit.preview(conn, "biz_1", base["id"], change)
    assert str(e.value) == (
        'Dropped a headline ("Call now!!"): it has an exclamation mark. '
        "Keep at least 3 headlines and 2 descriptions that pass"
    )


@pytest.mark.parametrize(
    ("change", "sentence"),
    [
        (plan_edit.Change("budget", channel="google_search", amount=2000), plan_edit.NOTHING_CHANGED),
        (plan_edit.Change("area", radius=5), plan_edit.NOTHING_CHANGED),
        (plan_edit.Change("budget", channel="newsbreak", amount=300), "NewsBreak is not in your plan"),
        (plan_edit.Change("budget", channel=None, amount=300), plan_edit.WHICH),
        (plan_edit.Change("budget", channel="google_search", amount=2500), r"above your \$2,000 monthly limit"),
        (plan_edit.Change("budget", channel="google_search", amount=50), "needs at least"),
    ],
)
def test_refusals_say_why(client, conn, fake, change, sentence):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)
    with pytest.raises(ToolError, match=sentence):
        plan_edit.preview(conn, "biz_1", base["id"], change)
    assert _plans(conn) == 1


def test_the_base_must_be_the_approved_version(client, conn, fake):  # noqa: F811 - the fixture
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000)["data"]
    change = plan_edit.Change("budget", channel="google_search", amount=1500)
    with pytest.raises(ToolError, match=plan_edit.NOT_LAUNCHED):  # nothing approved yet
        plan_edit.preview(conn, "biz_1", r["plan_id"], change)
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (r["plan_id"],))
    with pytest.raises(ToolError, match="changed since you looked"):
        plan_edit.preview(conn, "biz_1", "pl_0123456789abcdef", change)


def test_commit_writes_the_draft_from_the_same_inputs(client, conn, fake):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)
    plan = plan_edit.commit(conn, "biz_1", base["id"], plan_edit.Change("budget", channel="google_search", amount=1500))
    assert plan["version"] == 2 and plan["status"] == "draft" and plan["diff_from"] == base["id"]
    assert plan["sections"]["ads"]["channels"][0]["monthly_usd"] == 1500.0
    assert plan["sections"]["ads"]["named"] == {"google_search": 1500}  # the pin adjust keeps through later versions
    assert ads_run.approved(conn, "biz_1")["id"] == base["id"]  # still the running one: the approval is the route's


# the base is the approved version (Y6): what an abandoned draft left in the consultation's answers never rides along


def test_an_abandoned_drafts_radius_does_not_ride_along_on_a_budget_preview(client, conn, fake):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)
    draft = _abandoned_draft(conn, base, radius_miles=25)
    assert draft["sections"]["consultation"]["geo"]["radius_miles"] == 25.0  # the setup holds: the draft moved it
    pv = plan_edit.preview(conn, "biz_1", base["id"], plan_edit.Change("budget", channel="google_search", amount=1500))
    assert pv.radius_old == pv.radius_new == 5.0
    assert pv.ctx[4][0]["consultation"]["geo"]["radius_miles"] == 5.0


def test_a_commit_over_an_abandoned_draft_writes_the_approved_radius_and_supersedes_it(client, conn, fake):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)
    draft = _abandoned_draft(conn, base, radius_miles=25)
    plan = plan_edit.commit(conn, "biz_1", base["id"], plan_edit.Change("budget", channel="google_search", amount=1500))
    assert plan["sections"]["consultation"]["geo"]["radius_miles"] == 5.0
    assert plan["status"] == "draft" and plan["diff_from"] == base["id"]
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (draft["id"],)).fetchone()["status"] == "superseded"


# the area edit sends its own radius (10); the budget and words edits keep the approved version's 5
@pytest.mark.parametrize(
    ("change", "radius"),
    [
        (plan_edit.Change("budget", channel="google_search", amount=1500), 5.0),
        (plan_edit.Change("area", radius=10), 10.0),
        (
            plan_edit.Change("words", channel="google_search", headlines=tuple(WORDS_H), descriptions=tuple(WORDS_D)),
            5.0,
        ),
    ],
    ids=["budget", "area", "words"],
)
def test_no_edit_carries_an_abandoned_drafts_radius_objective_or_offer(client, conn, fake, change, radius):  # noqa: F811 - the fixture
    base = approved_plan(client, conn, fake)  # radius 5, objective calls, no offer
    draft = _abandoned_draft(conn, base, radius_miles=25, objective="bookings", offer="Free whitening")
    moved = draft["sections"]["consultation"]
    assert (moved["geo"]["radius_miles"], moved["objective"], moved["offer"]) == (25.0, "bookings", "Free whitening")
    got = plan_edit.preview(conn, "biz_1", base["id"], change).ctx[4][0]["consultation"]
    assert (got["geo"]["radius_miles"], got["objective"], got["offer"]) == (radius, "calls", None)


def test_the_approved_versions_own_offer_is_kept_over_an_abandoned_drafts(client, conn, fake):  # noqa: F811 - the fixture
    r = recommended_with(client, conn, fake, monthly_budget_usd=2000, known_facts={"offer": "Free whitening"})["data"]
    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (r["plan_id"],))
    base = ads_run.approved(conn, "biz_1")
    assert _abandoned_draft(conn, base, offer="Half price")["sections"]["consultation"]["offer"] == "Half price"
    change = plan_edit.Change("budget", channel="google_search", amount=1500)
    assert plan_edit.preview(conn, "biz_1", base["id"], change).ctx[4][0]["consultation"]["offer"] == "Free whitening"


def test_the_pins_leave_out_what_adjust_would_refuse_and_pin_no_offer_as_an_empty_one():
    whole = {"consultation": {"geo": {"radius_miles": 5.0}, "objective": "calls", "offer": "Free whitening"}}
    assert plan_edit._base_answers(whole) == {"radius_miles": 5.0, "objective": "calls", "offer": "Free whitening"}
    # a radius the owner's own words gave outside adjust's 1 to 100, an objective it does not know, no offer
    odd = {"consultation": {"geo": {"radius_miles": 500.0}, "objective": "mystery", "offer": None}}
    assert plan_edit._base_answers(odd) == {"offer": ""}
    assert plan_edit._base_answers({"consultation": {"geo": {"radius_miles": None}}}) == {"offer": ""}
