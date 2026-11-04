"""A plan's channels (spec 2026-09-29-admiry-channels-design §6.2, §7.2; plan 2026-09-29-agent-m0-channel-framework
Task 5): the split `allocate.initial` makes of the plan's amount, written into the plan's sections by
`plans_channels.fund`, and what the plan card shows of it -- one row per funded channel, the split at every budget
level, the later lines -- and the estimate summed over the funded rows."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp.exceptions import ToolError

from agent import benchmark, business, config, consult, plans, plans_channels, state, tools_consult
from agent.channels import registry
from agent.channels.newsbreak import creative
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.fixtures import (
    FakeAdapter,
    business_with_key,
    creative_set,
    home_services_knowledge,
    plan_draft_fixture,
)

ROW_KEYS = {"platform", "label", "role", "monthly_usd", "per_day", "why", "counts_as_result", "mode_text"}
NB_WHY = (
    "NewsBreak is a $300 test: local news readers in your ZIP codes, no published cost per lead yet, we will know in "
    "30 days."
)
COMING = {"platform": "meta", "label": "Facebook & Instagram", "why": "Not in this plan yet; coming to Admiry."}
NB_COMING = {"platform": "newsbreak", "label": "NewsBreak", "why": "Not in this plan yet; coming to Admiry."}
NB_PREPAID = {
    "platform": "newsbreak",
    "label": "NewsBreak",
    "why": "Not in this plan; Admiry runs it only for businesses that prepay.",
}
NB_PICTURE = {
    "platform": "newsbreak",
    "label": "NewsBreak",
    "why": "Not in this plan yet; once Admiry has a picture for your trade.",
}
NO_BENCHMARK = "no published benchmark; we will learn from the first 14 days"


def set_without(root, file: str) -> None:
    """The set at `root` without `file`: the founder took the picture out (its manifest entry gone)."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"] = [e for e in manifest["entries"] if e["file"] != file]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def home_plan(
    conn,
    *,
    service: str = "svc_roofing",
    limit: float = 1000,
    named: dict | None = None,
    caps: dict | None = None,
    draft=None,
    prepaid: bool = False,
) -> tuple[dict, list[str]]:
    """A roofer's consultation (home services) turned into a plan's sections by tools_consult.build, as recommend
    does, at the recommended level of an owner's limit; `prepaid`: the business prepays (Admiry runs its ads from a
    balance), the only way a channel Admiry runs from a balance -- NewsBreak -- can be in its plan in M0."""
    benchmark.seed(conn)
    business_with_key(conn, claim_state="claimed")
    if prepaid:
        conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    knowledge = home_services_knowledge()
    business.add_knowledge(conn, "biz_1", knowledge, source="extract")
    business.set_goal(conn, "biz_1", monthly_limit_usd=limit, channel_caps=caps)
    tree = consult.load_tree("home_services")
    session = consult.create(
        conn,
        "biz_1",
        tree="home_services",
        locale="en-US",
        opening_text="roofer",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": [service], "raw": "", "source": "choice"},
            "customer_value": {"choice": "1000_3000", "amount_usd": 1800.0, "raw": "", "source": "choice"},
            "radius": {"miles": 10.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    sections, _basis, notes, _goal = tools_consult.build(
        conn,
        "biz_1",
        session,
        knowledge,
        level="recommended",
        monthly_usd=None,
        draft=draft or plan_draft_fixture(),
        previous=None,
        named=named,
    )
    return sections, notes


def funded(sections: dict) -> list[tuple[str, float]]:
    return [(c["channel"], c["monthly_usd"]) for c in sections["ads"]["channels"]]


# ---------------------------------------------------------------- what a business can have (final review I2)


def test_an_own_account_business_gets_google_only_and_newsbreak_waits_for_prepaying(conn, newsbreak_on, pictures):
    """Spec §6.2's "a mode the business can use": NewsBreak runs only from a balance Admiry holds, which in M0 only a
    prepaid business has -- so a roofer who pays Google directly gets Google Search alone at $1,500, and the later
    lines say why NewsBreak is not there."""
    sections, notes = home_plan(conn, limit=1500)
    assert funded(sections) == [("google_search", 1500.0)]
    later = plans.later_rows(sections)
    assert NB_PREPAID in later and [x["platform"] for x in later].count("newsbreak") == 1
    assert sections["ads"]["splits"]["aggressive"] == {"google_search": 1500}  # no level funds it either
    assert not [n for n in notes if "NewsBreak" in n]  # the later line says it


def test_a_prepaid_business_gets_google_search_and_newsbreak(conn, newsbreak_on, pictures):
    sections, _ = home_plan(conn, limit=1500, prepaid=True)
    assert funded(sections) == [("google_search", 1200.0), ("newsbreak", 300.0)]
    assert "newsbreak" not in [x["platform"] for x in plans.later_rows(sections)]


def test_with_newsbreaks_switch_off_no_business_gets_it(conn):
    """AGENT_EXEC_NEWSBREAK unset (the conftest strips it; prod's compose defaults it to off): even a prepaid
    roofer's plan is Google Search alone, NewsBreak is a later line coming to Admiry, and Settings lists no NewsBreak
    row -- Google Search is listed whatever its own switch says (off only means nothing is sent to Google)."""
    sections, _ = home_plan(conn, limit=1500, prepaid=True)
    assert funded(sections) == [("google_search", 1500.0)]
    assert NB_COMING in plans.later_rows(sections)
    assert [c.channel for c in state.connections(conn, config.load(), "biz_1")] == ["google_search"]


def test_a_pin_on_a_channel_the_business_cannot_have_is_left_out_and_said(conn, newsbreak_on, pictures):
    """adjust's "put $300 on NewsBreak" funds nothing an own-account business cannot pay for: the pin is kept for a
    later version, the plan says why it is not in this one."""
    sections, notes = home_plan(conn, named={"newsbreak": 300})
    assert funded(sections) == [("google_search", 1000.0)]
    assert "NewsBreak: not in this plan; Admiry runs it only for businesses that prepay." in notes
    assert sections["ads"]["named"] == {"newsbreak": 300}


def test_a_roofing_plan_funds_google_search_and_the_newsbreak_test(conn, newsbreak_on, pictures):
    sections, notes = home_plan(conn, prepaid=True)
    ads = sections["ads"]
    assert funded(sections) == [("google_search", 700.0), ("newsbreak", 300.0)]
    g, nb = ads["channels"]
    assert (g["role"], g["basis"], g["mode"]) == ("primary", "benchmark", "managed")  # it prepays: Google's too
    assert g["why"] == "Google Search gets most of it because people there are already looking for Roofing near you."
    assert (nb["role"], nb["basis"], nb["mode"], nb["why"]) == ("test", "fit", "managed", NB_WHY)
    # the draft wrote no NewsBreak copy: it runs the primary's words (M1's build fits them to its own limits)
    for k in ("themes", "negatives", "headlines", "descriptions", "landing_service"):
        assert nb[k] == g[k], k
    # the draft's Meta has no adapter yet: kept for its later line, never a funded row (and no note: the line says it)
    assert ads["later"] == ["meta"] and not [n for n in notes if "Facebook" in n or "Meta" in n]
    assert ads["splits"] == {
        "minimum": {"google_search": 300},
        "conservative": {"google_search": 500},
        "recommended": {"google_search": 700, "newsbreak": 300},
        "aggressive": {"google_search": 700, "newsbreak": 300},
    }
    assert ads["named"] == {}
    # the estimate sums the funded rows: NewsBreak's $300 is spent, but it has no published row to estimate leads by
    assert sections["estimate"]["columns"]["planning"]["spend"] == 1000
    assert sections["estimate"]["benchmark_rows"] == ["home_services:google_search"]


def test_a_service_without_the_flag_stays_on_google_search(conn):
    sections, _ = home_plan(conn, service="svc_emergency")
    assert funded(sections) == [("google_search", 1000.0)]
    assert sections["ads"]["channels"][0]["why"].startswith("Google Search gets all of it because")


def test_a_channel_a_new_amount_leaves_out_is_a_note(conn, newsbreak_on, pictures):
    """adjust re-splits the previous version's rows: NewsBreak's test needs $800, so at $700 it leaves, said once."""
    sections, _ = home_plan(conn, prepaid=True)
    again, _basis, notes, _goal = tools_consult.build(
        conn,
        "biz_1",
        consult.open_for(conn, "biz_1"),
        home_services_knowledge(),
        level="custom",
        monthly_usd=700,
        draft=None,
        previous=sections,
    )
    assert funded(again) == [("google_search", 700.0)] and "NewsBreak: not in this plan." in notes
    assert again["ads"]["later"] == ["meta"]  # the draft's later lines stay with the plan


def test_a_funded_channel_keeps_the_copy_the_draft_wrote_for_it(conn, monkeypatch, newsbreak_on, pictures):
    monkeypatch.setitem(registry.CHANNELS, "meta", FakeAdapter("meta"))  # as if Facebook & Instagram shipped
    monkeypatch.setenv("AGENT_EXEC_META", "validate")  # and switched on
    sections, _ = home_plan(conn, prepaid=True)  # a roofer's service is meta_fit
    by = {c["channel"]: c for c in sections["ads"]["channels"]}
    assert funded(sections) == [("google_search", 500.0), ("newsbreak", 300.0), ("meta", 200.0)]
    assert by["meta"]["headlines"] == ["A straighter smile"]  # the draft's own Meta copy
    assert by["newsbreak"]["headlines"] == by["google_search"]["headlines"]  # none written: the primary's
    assert sections["ads"]["later"] == []


def test_the_later_lines_by_class(conn, monkeypatch):
    """Ruling (fix round 1): Yelp and TikTok always; ChatGPT always (D103) until it is funded; every registered
    channel that cannot create and is not funded (a report-only one, D83); every channel the draft proposed that has
    no adapter; never a channel that is neither registered nor proposed (Bing, Nextdoor here). Final review I2: every
    channel the plan would fund that the business cannot have yet (NewsBreak here: its switch is off)."""

    class ReportOnly(FakeAdapter):
        def capabilities(self):
            return super().capabilities().model_copy(update={"create": False})

    monkeypatch.setitem(registry.CHANNELS, "reddit", ReportOnly("reddit"))
    sections, _ = home_plan(conn)
    later = plans.later_rows(sections)
    assert later == [
        NB_COMING,
        COMING,
        {
            "platform": "reddit",
            "label": "Reddit",
            "why": "Run by Admiry from the platform's own report when you ask for it.",
        },
        {"platform": "chatgpt_ads", "label": "ChatGPT", "why": "Later: Admiry is trying it on its own ads first."},
        {"platform": "yelp", "label": "Yelp", "why": "Not now: Yelp opens its ads only to large partners."},
        {"platform": "tiktok", "label": "TikTok", "why": "Worth a test after 30 days of booking data. Not now."},
    ]
    # a report-only channel the owner funds (a named amount) is a row, not a later line
    sections["ads"]["channels"].append({"channel": "reddit", "monthly_usd": 600.0, "basis": "named"})
    assert "reddit" not in [x["platform"] for x in plans.later_rows(sections)]
    words = " ".join(x["why"] for x in later)
    assert not JARGON.search(words) and not ACCOUNT_JARGON.search(words) and not MONEY_JARGON.search(words)


def test_google_search_runs_as_the_business_chose_until_its_account_row_says_otherwise(conn):
    """P6: without a channel_account row, Google Search's mode is business.ads_mode (managed here, so the card says
    Admiry runs it); a row, once written, wins."""
    benchmark.seed(conn)
    business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    assert plans_channels.modes(conn, "biz_1") == {"google_search": "managed"}
    registry.save_account(conn, "biz_1", "google_search", mode="own_account", status="none", external={})
    registry.save_account(conn, "biz_1", "newsbreak", mode="managed", status="none", external={})
    assert plans_channels.modes(conn, "biz_1") == {"google_search": "own_account", "newsbreak": "managed"}
    assert plans.mode_text("google_search", "managed") == "Admiry runs it from your balance"


def test_the_plan_card_renders_every_funded_row_and_the_later_lines(conn, monkeypatch, newsbreak_on, pictures):
    monkeypatch.setitem(registry.CHANNELS, "microsoft_search", FakeAdapter("microsoft_search"))  # as if Bing shipped
    monkeypatch.setenv("AGENT_EXEC_MICROSOFT_SEARCH", "validate")  # and switched on
    sections, _ = home_plan(conn, prepaid=True)
    c = plans.cards({"id": "pl_1", "business_id": "biz_1", "business_name": "Summit Roofing", "sections": sections})
    rows = c["plan"]["channels"]
    assert [set(r) for r in rows] == [ROW_KEYS] * 3
    assert [(r["platform"], r["label"], r["role"], r["monthly_usd"], r["per_day"]) for r in rows] == [
        ("google_search", "Google Search", "primary", 600, 20),
        ("newsbreak", "NewsBreak", "test", 300, 10),
        ("microsoft_search", "Bing Search", "secondary", 100, 3),
    ]
    assert [r["mode_text"] for r in rows] == [
        "Admiry runs it from your balance",  # the business prepays: Google Search too
        "Admiry runs it from your balance",
        "your own Bing Search account",
    ]
    assert all(r["counts_as_result"] == ["consultation_booking", "phone_call_60s"] for r in rows)
    assert rows[1]["why"] == NB_WHY
    assert (
        rows[2]["why"] == "Bing Search gets $100 a month: the same searches as on Google, at a lower price per click."
    )
    later = c["plan"]["later"]  # Bing is funded here; Nextdoor has no adapter and no draft proposed it
    assert [(x["platform"], x["label"]) for x in later] == [
        ("meta", "Facebook & Instagram"),
        ("chatgpt_ads", "ChatGPT"),
        ("yelp", "Yelp"),
        ("tiktok", "TikTok"),
    ]
    assert later[0] == COMING and later[3]["why"] == "Worth a test after 30 days of booking data. Not now."
    assert (c["budget"]["monthly_usd"], c["budget"]["per_day"]) == (1000, 33)
    assert c["budget"]["levels"]["recommended"] == {
        "monthly_usd": 1000,
        "split": {"google_search": 600, "newsbreak": 300, "microsoft_search": 100},
    }
    assert c["budget"]["levels"]["minimum"] == {"monthly_usd": 300, "split": {"google_search": 300}}
    words = " ".join(f"{r['label']} {r['why']} {r['mode_text']}" for r in rows) + " ".join(x["why"] for x in later)
    assert not JARGON.search(words) and not ACCOUNT_JARGON.search(words) and not MONEY_JARGON.search(words)


def test_older_and_hand_built_plans_still_read(conn, newsbreak_on, pictures):
    """A plan stored before the split (or built by hand, as the A3 tests do) has no role, reason, mode, later lines or
    level splits: the card fills them in."""
    sections, _ = home_plan(conn, prepaid=True)
    for c in sections["ads"]["channels"]:
        for k in ("role", "why", "basis", "mode"):
            c.pop(k)
    for k in ("later", "splits", "named", "held"):
        sections["ads"].pop(k)
    c = plans.cards({"id": "pl_1", "business_id": "biz_1", "sections": sections})
    rows = c["plan"]["channels"]
    assert [(r["platform"], r["role"], r["mode_text"]) for r in rows] == [
        ("google_search", "primary", "your own Google Ads account"),
        ("newsbreak", "test", "Admiry runs it from your balance"),
    ]
    assert all(r["why"] and r["label"] in r["why"] for r in rows)
    assert [x["platform"] for x in c["plan"]["later"]] == ["chatgpt_ads", "yelp", "tiktok"]
    assert c["budget"]["levels"]["recommended"] == {"monthly_usd": 1000, "split": {"google_search": 1000}}


# ---------------------------------------------------------------- the picture on the plan card (M1 Task 7, plan P7)

PICTURE = "http://testserver/static/creative/{}"  # conftest's base: the web host serves /static
IMAGE_SHOWN = "NewsBreak shows this picture with your ad; approving the plan approves it"
IMAGE_GONE = (  # final review I3: running ads keep the picture they carry; a new version waits for a new one
    "The picture for your NewsBreak ad is no longer in Admiry's set: NewsBreak ads already running keep their current "
    "picture while a new version of your plan waits for a new one"
)
IMAGE_NONE = "This version of your plan has no picture for your NewsBreak ad, so NewsBreak waits for a new version"


def _preview(sections: dict) -> dict:
    return plans.cards({"id": "pl_1", "business_id": "biz_1", "sections": sections})["ads_preview"]


def _add_picture(root, **entry) -> None:
    """One more entry in the set at `root`, first in its manifest: a generated picture already there, under `entry`'s
    file name, trade and services (the rest copied from the first entry)."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"].insert(0, {**manifest["entries"][0], **entry})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_a_plan_that_funds_newsbreak_shows_the_picture_that_approving_it_approves(conn, newsbreak_on, pictures):
    """P7: the ad preview carries the picture NewsBreak shows with the ad -- its address and its words for an owner who
    cannot see it (the manifest's `alt`) -- and the line that approving the plan approves it."""
    sections, _ = home_plan(conn, prepaid=True)  # a roofer: home services, Google Search and NewsBreak
    preview = _preview(sections)
    assert preview["image"] == {"url": PICTURE.format("home_services/1.jpg"), "alt": "A solid green test picture"}
    assert preview["image_note"] == IMAGE_SHOWN
    for line in (IMAGE_SHOWN, IMAGE_GONE, IMAGE_NONE, NB_PICTURE["why"]):
        assert not JARGON.search(line) and not ACCOUNT_JARGON.search(line) and not MONEY_JARGON.search(line)


def test_a_trade_without_a_picture_gets_no_newsbreak_and_its_later_line_says_until_when(
    conn, newsbreak_on, no_pictures
):
    """Fix round 1, item 3 (the controller's ruling): the allocator never funds a channel that shows a picture while
    Admiry's set has none for the trade -- NewsBreak is held the way a channel switched off is (`ads.held`), its later
    line says until when, and the plan is Google Search's alone, with no picture; once the trade has one, the plan's
    next version funds both, the picture pinned on it."""
    sections, notes = home_plan(conn, prepaid=True)
    assert funded(sections) == [("google_search", 1000.0)]
    assert sections["ads"]["held"] == {"newsbreak": "picture"} and NB_PICTURE in plans.later_rows(sections)
    assert all("newsbreak" not in split for split in sections["ads"]["splits"].values())  # no level funds it
    assert "proposed_image" not in sections["creative"] and "image_note" not in _preview(sections)
    assert not [n for n in notes if "NewsBreak" in n]  # the later line says it
    # a business that does not prepay hears the picture first: prepaying alone would not bring NewsBreak
    assert plans_channels.held(config.load(), {"own_account"}, pictured=False) == {"newsbreak": "picture"}
    assert plans_channels.held(config.load(), {"own_account"}) == {"newsbreak": "prepaid"}
    creative_set(no_pictures)  # the founder adds the trade's picture (the generated set, in the same folder)
    again = _adjusted(conn, sections)
    assert funded(again) == [("google_search", 700.0), ("newsbreak", 300.0)] and again["ads"]["held"] == {}
    assert again["creative"]["proposed_image"] == {"newsbreak": "home_services/1.jpg"}
    assert _preview(again)["image_note"] == IMAGE_SHOWN and NB_PICTURE not in plans.later_rows(again)


def test_a_pin_on_newsbreak_without_a_picture_for_the_trade_is_left_out_and_said(conn, newsbreak_on, no_pictures):
    sections, notes = home_plan(conn, named={"newsbreak": 300}, prepaid=True)
    assert funded(sections) == [("google_search", 1000.0)] and sections["ads"]["named"] == {"newsbreak": 300}
    assert "NewsBreak: not in this plan yet; once Admiry has a picture for your trade." in notes


def test_a_version_pins_its_picture_and_shows_that_one_whatever_the_set_offers_later(conn, newsbreak_on, pictures):
    """Fix round 1, item 4 (review M8): the picture is chosen once, when the version is made, and pinned on it
    (`creative.proposed_image`) -- by the trade and the service (`creative.pick`) -- so the card shows that one, and
    approving the version approves that one (test_proposals), whatever the set offers by then."""
    sections, _ = home_plan(conn, prepaid=True)
    assert sections["creative"]["proposed_image"] == {"newsbreak": "home_services/1.jpg"}
    _add_picture(pictures, file="dentists/2.jpg", category="home_services", services=["Roofing"])  # a roofing picture
    assert creative.pick("home_services", "Roofing").file == "dentists/2.jpg"  # what a new version would pin now
    assert _preview(sections)["image"]["url"] == PICTURE.format("home_services/1.jpg")  # this one keeps its own


def test_a_pinned_picture_that_left_the_set_is_said_on_the_card_and_never_swapped(conn, newsbreak_on, pictures):
    """Item 4: the set no longer has the version's picture -- the card says so and shows none (another picture of the
    trade is never put in its place: the owner did not see it); a version with no picture at all says that."""
    sections, _ = home_plan(conn, prepaid=True)
    set_without(pictures, "home_services/1.jpg")
    _add_picture(pictures, file="dentists/1.jpg", category="home_services")  # the trade has another picture
    preview = _preview(sections)
    assert (preview["image"], preview["image_note"]) == (None, IMAGE_GONE)
    del sections["creative"]["proposed_image"]  # a version made before the pin, never approved
    preview = _preview(sections)
    assert (preview["image"], preview["image_note"]) == (None, IMAGE_NONE)


def test_a_new_version_keeps_the_picture_of_the_version_it_comes_from_while_the_set_has_it(
    conn, newsbreak_on, pictures
):
    """adjust's version carries the creative section of the version it comes from: it keeps that version's picture
    (its pin, or -- made before the pin -- the one approved with it) while the trade still has it -- the running ads
    keep theirs, and NewsBreak reviews nothing new -- else the usual pick."""
    sections, _ = home_plan(conn, prepaid=True)
    _add_picture(pictures, file="dentists/2.jpg", category="home_services", services=["Roofing"])  # a roofing picture
    again = _adjusted(conn, sections)
    assert again["creative"]["proposed_image"] == {"newsbreak": "home_services/1.jpg"}  # the version's own, kept
    approved = {k: v for k, v in again["creative"].items() if k != "proposed_image"}
    approved["approved_image"] = {"newsbreak": "home_services/1.jpg"}
    assert _adjusted(conn, {**again, "creative": approved})["creative"]["proposed_image"] == {
        "newsbreak": "home_services/1.jpg"
    }
    other_trade = {**again["creative"], "proposed_image": {"newsbreak": "fallback/1.jpg"}}  # not the roofer's: not kept
    assert _adjusted(conn, {**again, "creative": other_trade})["creative"]["proposed_image"] == {
        "newsbreak": "dentists/2.jpg"
    }
    set_without(pictures, "home_services/1.jpg")
    assert _adjusted(conn, again)["creative"]["proposed_image"] == {"newsbreak": "dentists/2.jpg"}  # gone: re-picked


def _adjusted(conn, previous: dict) -> dict:
    """adjust's sections from `previous`, at the recommended level (tools_consult.build with `previous`)."""
    again, _basis, _notes, _goal = tools_consult.build(
        conn,
        "biz_1",
        consult.open_for(conn, "biz_1"),
        home_services_knowledge(),
        level="recommended",
        monthly_usd=None,
        draft=None,
        previous=previous,
    )
    return again


def test_a_plan_whose_channels_show_no_picture_has_none_on_its_card(conn, pictures):
    sections, _ = home_plan(conn)  # Google Search alone
    assert set(_preview(sections)) == {"headlines", "descriptions", "final_host", "dropped"}
    assert "proposed_image" not in sections["creative"]


def test_the_primary_row_the_total_and_the_pins():
    s = {
        "ads": {
            "channels": [
                {"channel": "newsbreak", "monthly_usd": 300.0, "role": "test"},
                {"channel": "google_search", "monthly_usd": 700.0, "role": "primary"},
            ],
            "named": {"newsbreak": 300},
        }
    }
    assert plans.primary(s)["channel"] == "google_search"
    assert plans.total_usd(s) == 1000 and plans.pins(s) == {"newsbreak": 300}
    assert (
        plans.primary({"ads": {"channels": [{"channel": "newsbreak", "monthly_usd": 300.0}]}})["channel"] == "newsbreak"
    )
    assert plans.primary({"ads": {"channels": []}}) == {} and plans.pins({"ads": {"channels": []}}) == {}
    assert plans.CHANNEL_NAMES is registry.LABELS  # the old name, kept one release


def test_a_named_amount_is_kept_in_the_plan(conn, newsbreak_on, pictures):
    sections, _ = home_plan(conn, service="svc_emergency", named={"newsbreak": 400}, prepaid=True)
    assert [(c["channel"], c["monthly_usd"], c["basis"]) for c in sections["ads"]["channels"]] == [
        ("google_search", 600.0, "benchmark"),
        ("newsbreak", 400.0, "named"),
    ]
    assert sections["ads"]["named"] == {"newsbreak": 400}


def test_named_amounts_beyond_the_plan_are_refused(conn, newsbreak_on, pictures):
    with pytest.raises(ToolError, match=r"come to \$1,200 a month, more than the plan's \$1,000"):
        home_plan(conn, named={"newsbreak": 1200}, prepaid=True)


def test_a_cap_below_newsbreaks_minimum_keeps_it_out(conn, newsbreak_on, pictures):
    sections, notes = home_plan(conn, caps={"newsbreak": 200}, prepaid=True)
    assert funded(sections) == [("google_search", 1000.0)]
    assert "NewsBreak: your limit of $200 a month is below its $300 minimum, so it is not in this plan." in notes


def test_estimate_columns_sum_over_the_funded_rows(conn):
    benchmark.seed(conn)
    kw = {"customer_value_usd": 900, "value_source": "owner", "footnote_noun": "home-service businesses"}
    est = plans.estimate_columns(
        conn,
        "home_services",
        split={"google_search": 1500, "microsoft_search": 200, "newsbreak": 300},
        recommended_split={"google_search": 2000},
        **kw,
    )
    p = est["columns"]["planning"]
    # $1,500 on Search and $200 on Bing bring the leads; NewsBreak's $300 is spent with no published row to estimate
    assert (p["spend"], p["leads"], p["bookings"]) == (2000, [10, 25], [6, 15])
    assert est["columns"]["recommended"]["spend"] == 2000
    assert est["benchmark_rows"] == ["home_services:google_search", "home_services:microsoft_search"]
    assert [(a["name"], a["basis"]) for a in est["assumptions"]] == [
        ("cost per lead", "benchmark home_services:google_search US, confidence low"),
        ("cost per lead, Bing Search", "benchmark home_services:microsoft_search US, confidence low"),
        ("cost per lead, NewsBreak", NO_BENCHMARK),
        ("lead to booking", "benchmark home_services, confidence low"),
        ("lead to booking, Bing Search", "benchmark home_services:microsoft_search, confidence low"),
        ("booking to customer", "benchmark home_services, confidence low"),
        ("customer value", "your answer"),
    ]
    # Search books 60 % of its leads, Facebook & Instagram 13 %: the bookings blend them by each one's leads
    mixed = plans.estimate_columns(
        conn,
        "home_services",
        split={"google_search": 1000, "meta": 1000},
        recommended_split={"google_search": 2000},
        **kw,
    )
    assert (mixed["columns"]["planning"]["leads"], mixed["columns"]["planning"]["bookings"]) == ([25, 50], [7, 14])


def test_estimate_columns_take_a_split_without_any_published_benchmark(conn):
    """Fix round 1, IMPORTANT 1: NewsBreak alone (a pin that takes the whole minimum level) has no benchmark row; the
    columns show no leads for it, its assumption says so in the spec's words, and nothing raises."""
    benchmark.seed(conn)
    kw = {"customer_value_usd": 900, "value_source": "owner", "footnote_noun": "home-service businesses"}
    est = plans.estimate_columns(
        conn, "home_services", split={"newsbreak": 300}, recommended_split={"newsbreak": 300}, **kw
    )
    p = est["columns"]["planning"]
    assert (p["spend"], p["leads"], p["bookings"], p["customers"], p["revenue"]) == (
        300,
        [0, 0],
        [0, 0],
        [0, 0],
        [0, 0],
    )
    assert (est["basis"], est["benchmark_rows"], est["low"], est["high"]) == ("none", [], 0, 0)
    assert est["assumptions"][0] == {
        "name": "cost per lead, NewsBreak",
        "value": "not published",
        "basis": NO_BENCHMARK,
    }
    # the plan's line says why there is no number, never "no advertising budget"
    lines = plans.owner_lines({**_sections_for_lines(), "estimate": est})
    assert lines["estimate"] == f"Estimated monthly leads: not estimated yet ({NO_BENCHMARK})"


def _sections_for_lines() -> dict:
    return {
        "ads": {"channels": [{"channel": "newsbreak", "monthly_usd": 300.0}]},
        "optimization": {"autopilot": "safe"},
        "disclosure": {"text": plans.DISCLOSURE},
    }
