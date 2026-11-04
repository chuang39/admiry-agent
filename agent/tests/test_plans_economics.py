import pytest

from agent import benchmark, plans, profile
from agent.tests.fixtures import knowledge_fixture, plan_draft_fixture


def test_levels_round_to_100_and_clip_to_the_limit():
    assert plans.levels(2400, None) == {"minimum": 300, "conservative": 1100, "recommended": 2400, "aggressive": 4000}
    assert plans.levels(2000, 2000) == {"minimum": 300, "conservative": 900, "recommended": 2000, "aggressive": 2000}
    # conservative never drops below the $10-a-day minimum; an owner's limit below it clips every level
    assert plans.levels(500, None) == {"minimum": 300, "conservative": 300, "recommended": 500, "aggressive": 800}
    assert plans.levels(2000, 200) == {"minimum": 200, "conservative": 200, "recommended": 200, "aggressive": 200}


def test_columns_are_ranges_with_a_basis_for_every_factor(conn):
    benchmark.seed(conn)
    est = plans.estimate_columns(
        conn,
        "dentists",
        split={"google_search": 2400},
        recommended_split={"google_search": 1500},
        customer_value_usd=2500,
        value_source="owner",
        footnote_noun="dental practices",
    )
    p, c = est["columns"]["planning"], est["columns"]["recommended"]
    assert p["spend"] == 2400 and c["spend"] == 1500
    assert p["leads"] == [20, 45] and c["leads"] == [15, 30]  # spend ÷ (1.5 | 0.7 × 72.97), each end to 5
    assert p["bookings"] == [10, 23] and p["customers"] == [6, 13]  # × 0.5, × 0.55, rounded
    assert p["revenue"] == [15000, 32500] and c["revenue"][0] <= p["revenue"][0]
    names = [a["name"] for a in est["assumptions"]]
    assert names == ["cost per lead", "lead to booking", "booking to customer", "customer value"]
    assert est["assumptions"][0]["basis"].startswith("benchmark dentists:google_search")
    assert est["assumptions"][3] == {"name": "customer value", "value": "$2,500", "basis": "your answer"}
    assert (
        est["footnote"]
        == "Planning estimates from published averages for US dental practices, not a forecast. Actual results vary."
    )
    assert (est["low"], est["high"], est["basis"]) == (20, 45, "benchmark")
    for col in (p, c):
        for k in ("leads", "bookings", "customers", "revenue"):
            assert col[k][0] <= col[k][1]


def test_revenue_under_a_thousand_rounds_to_the_nearest_50_dollars(conn):
    """A low customer value (a dentist's "Under $200" band) is not rounded up to $500 steps: under $1,000 the revenue
    goes to the nearest $50, from $1,000 to the nearest $500 as before."""
    benchmark.seed(conn)

    def revenue(value: float) -> list[int]:
        est = plans.estimate_columns(
            conn,
            "dentists",
            split={"google_search": 2400},
            recommended_split={"google_search": 2400},
            customer_value_usd=value,
            value_source="owner",
            footnote_noun="dental practices",
        )
        return est["columns"]["planning"]["revenue"]

    assert revenue(60) == [350, 800]  # customers [6, 13] as above: $360 and $780
    assert revenue(150) == [900, 2000]  # $900 stays; $1,950 is past $1,000


def test_a_spend_below_the_minimum_is_refused_not_crashed(conn):
    benchmark.seed(conn)
    for spend in (50, 0):
        with pytest.raises(ValueError, match=r"\$100"):
            plans.estimate_columns(
                conn,
                "dentists",
                split={"google_search": spend},
                recommended_split={"google_search": 2000},
                customer_value_usd=2500,
                value_source="owner",
                footnote_noun="dental practices",
            )


def test_the_minimum_spend_itself_estimates_cleanly(conn):
    benchmark.seed(conn)
    est = plans.estimate_columns(
        conn,
        "dentists",
        split={"google_search": 100},
        recommended_split={"google_search": 50},
        customer_value_usd=2500,
        value_source="owner",
        footnote_noun="dental practices",
    )
    assert est["columns"]["planning"]["spend"] == 100
    assert est["columns"]["recommended"]["spend"] == 100  # a recommendation below the minimum is estimated at it


def test_name_for_trusts_only_a_name_a_website_or_listing_vouches_for():
    from agent import plans

    assert plans.name_for({"name": "Bright Smile Dental", "website": "https://bright.example"}) == "Bright Smile Dental"
    confirmed = {"place_id": "ChIJ1", "confirmed": True}  # the one the owner confirmed as the business's
    assert plans.name_for({"name": "Bright Smile Dental", "listing": confirmed}) == "Bright Smile Dental"
    assert plans.name_for({"name": "H1B Visa Lawyer"}) is None  # derived from the owner's words: "your business"
    assert plans.name_for({"website": "https://x.example"}) is None and plans.name_for(None) is None


def test_only_a_listing_the_owner_confirmed_vouches_for_the_name():
    listing = {"place_id": "ChIJ1", "name": "Bright Smile Dental", "address": "x", "source": "Google Maps"}
    assert plans.name_for({"name": "Bright Smile Dental", "listing": listing}) is None
    assert plans.name_for({"name": "Bright Smile Dental", "listing": {**listing, "confirmed": True}}) == (
        "Bright Smile Dental"
    )


def test_name_for_reads_where_the_name_came_from():
    """Ruling R0-k (A3p-5 batch 2 Task 0, item c): the name a reading of a site gave is vouched for, and so is the
    owner's own; a name a reading of plain words gave is not, even beside a site an earlier reading read and
    profile.carry kept (ruling F3) -- unless the owner confirmed the business's Google listing. A knowledge stored
    before the field existed keeps the old rule: its website vouches."""
    site = "https://brightsmile.example"
    read = {"name": "Bright Smile Dental", "website": site, "name_source": "site"}
    assert plans.name_for(read) == "Bright Smile Dental"
    later = profile.carry(read, {"name": "H1B Visa Lawyer", "name_source": "words"})  # a reading of plain words
    assert (later["website"], later["name_source"]) == (site, "words")
    assert plans.name_for(later) is None
    confirmed = {"place_id": "ChIJ1", "name": "H1B Visa Lawyer", "confirmed": True}
    assert plans.name_for({**later, "listing": confirmed}) == "H1B Visa Lawyer"
    assert plans.name_for({"name": "Bright Smile Dental", "website": site}) == "Bright Smile Dental"  # stored before
    assert plans.name_for({"name": "Sunny Smiles", "name_source": "owner"}) == "Sunny Smiles"  # no site, no listing
    assert plans.name_for({"name": "Sunny Smiles", "name_source": "words"}) is None


def test_name_for_gives_only_a_name_in_text_with_something_in_it():
    """Open item B3: a name stored as something other than text before update_business checked its shape (an object,
    a number, a list, blanks) is no name, whatever vouches for it; one in text is given as the profile reads it,
    stripped (profile.clean_name)."""
    site = {"website": "https://bright.example"}
    for name in ({"en": "Bright Smile"}, 42, ["Bright Smile"], "   "):
        assert plans.name_for({**site, "name": name}) is None, name
    assert plans.name_for({**site, "name": "  Bright Smile Dental "}) == "Bright Smile Dental"


def test_a_name_stored_as_an_object_reads_as_your_business_on_the_card(conn):
    plan = _cards_plan(conn)
    stored = {**knowledge_fixture().model_dump(), "name": {"en": "Bright Smile"}}
    goal = plans.cards({**plan, "business_name": plans.name_for(stored)})["plan"]["goal"]
    assert goal == "More Invisalign bookings within 5 miles of your business"


# An address the owner placed (walk fixes 1, Task 4: geocode.listing): the place the ads centre on, no business's name
PLACED = {
    "place_id": "ChIJ2",
    "name": "123 Main St",
    "address": "123 Main St, Houston, TX 77002",
    "lat": 29.76,
    "lng": -95.37,
    "source": "Google Maps",
    "kind": "address",
}


def test_a_placed_address_vouches_for_no_name_and_is_the_place_a_sentence_may_centre_on():
    placed = {"name": "H1B Visa Lawyer", "listing": PLACED}
    assert plans.name_for(placed) is None
    assert plans.address_for(placed) == "123 Main St, Houston, TX 77002"
    own = {k: v for k, v in PLACED.items() if k != "kind"}
    business_listing = {**placed, "listing": {**own, "confirmed": True}}  # the business's own, confirmed
    assert plans.name_for(business_listing) == "H1B Visa Lawyer" and plans.address_for(business_listing) is None
    assert plans.name_for({**placed, "listing": own}) is None  # a lookup's guess names nothing
    assert plans.name_for({**placed, "website": "https://h1b.example"}) == "H1B Visa Lawyer"  # a website vouches
    assert plans.address_for(None) is None and plans.address_for({"listing": ["123 Main St"]}) is None


def test_the_goal_sentence_centres_on_the_placed_address_when_no_name_is_vouched(conn):
    plan = _cards_plan(conn)
    knowledge = {**knowledge_fixture().model_dump(), "website": None, "listing": PLACED}
    name = plans.name_for(knowledge) or plans.address_for(knowledge)
    cards = plans.cards({**plan, "business_name": name})
    assert cards["plan"]["goal"] == "More Invisalign bookings within 5 miles of 123 Main St, Houston, TX 77002"
    assert cards["plan"]["setup"]["area"] == "within 5 miles of the business"


# ---------------------------------------------------------------- ads_preview (A3 Task 3)


def _cards_plan(conn, *, headlines=None, descriptions=None) -> dict:
    """A plan whose sections are complete enough for cards() -- the same shape build() leaves after a
    consultation, built directly (no client/DB walk needed) from the existing fixtures."""
    benchmark.seed(conn)
    draft = plan_draft_fixture()
    if headlines is not None:
        draft.ads[0].headlines = headlines
    if descriptions is not None:
        draft.ads[0].descriptions = descriptions
    sections, _notes = plans.normalize(draft, 2000)
    sections["ads"]["channels"] = [{**sections["ads"]["channels"][0], "monthly_usd": 2000.0}]  # Google Search alone
    knowledge = knowledge_fixture().model_dump()
    facts = {
        "objective": "bookings",
        "service": "Invisalign",
        "services": ["Invisalign"],
        "customer_value_usd": 3000.0,
        "customer_value_source": "owner",
        "radius_miles": 5.0,
        "offer": None,
    }
    sections["estimate"] = plans.estimate_columns(
        conn,
        "dentists",
        split={"google_search": 2000},
        recommended_split={"google_search": 1500},
        customer_value_usd=3000,
        value_source="owner",
        footnote_noun="dental practices",
    )
    sections["consultation"] = plans.consultation_section(facts, knowledge, level="recommended", listing=None)
    sections["rationale"] = plans.rationale_section(knowledge, facts)
    sections["levels"] = plans.levels(1500, None)
    return {"id": "pl_1", "business_id": "biz_1", "business_name": "Bright Smile Dental", "sections": sections}


def test_ads_preview_carries_the_kept_copy_and_the_page_host(conn):
    plan = _cards_plan(conn)
    preview = plans.cards(plan, final_host="brightsmile.example")["ads_preview"]
    assert preview == {
        "headlines": ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners, Family Care"],
        "descriptions": [
            "Clear aligners from a family dentist. Book online.",
            "Same-week consultations in Palo Alto; see the cost before anything starts.",
        ],
        "final_host": "brightsmile.example",
        "dropped": [],
    }
    # cards() is pure: no final_host given is None, not a lookup of its own
    assert plans.cards(plan)["ads_preview"]["final_host"] is None


def test_ads_preview_drops_a_banned_headline_with_a_plain_note(conn):
    plan = _cards_plan(conn, headlines=["Best Invisalign in Town", "Same-Week Appointments", "Gentle Family Dentist"])
    preview = plans.cards(plan)["ads_preview"]
    assert preview["headlines"] == ["Same-Week Appointments", "Gentle Family Dentist"]
    assert preview["dropped"] == ['Dropped a headline ("Best Invisalign in Town"): it uses a word we avoid ("best").']
    from agent.tests.conftest import JARGON

    assert not JARGON.search(preview["dropped"][0])
