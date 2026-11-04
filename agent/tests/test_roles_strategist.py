"""What a plan draft reads of Admiry's knowledge and which ids it keeps (capability layer K3; spec 2026-10-07 §4's
Strategist row, §6.1; plan P3, P4, P7)."""

import pytest

from agent import config, trades
from agent import knowledge_index as ki
from agent.roles import strategist
from agent.tests import knowledge_fixtures as kf


@pytest.mark.parametrize(
    ("detail", "service", "services", "want"),
    [
        ("residential plumber", None, [], "plumbing"),
        ("HVAC contractor", None, [], "hvac"),
        ("roofer", None, [], "roofing"),
        ("licensed electrician", None, [], "electrical"),
        ("landscape design and build", None, [], "landscaping"),
        ("general contractor", "Kitchen remodels", [], "remodel"),
        ("handyman", None, [{"name": "Emergency plumbing"}], "plumbing"),
        ("family dentist", "Invisalign", [], None),
        ("", None, [], None),
    ],
)
def test_the_trade_is_read_from_what_the_business_does_then_its_services(detail, service, services, want):
    knowledge = {"category_detail": detail, "services": services}
    assert strategist.trade(knowledge, {"service": service, "services": [service] if service else []}) == want


def test_what_the_business_does_wins_over_the_service():
    assert strategist.trade({"category_detail": "roofer", "services": []}, {"service": "Drain cleaning"}) == "roofing"


@pytest.mark.parametrize(
    ("detail", "trade_field", "service", "services", "want"),
    [
        # the owner-set `trade` names the trade when what the business does does not (sites/intake._trade_of, the
        # consultation's options through family_for and google_managed.industry read it too; both stored forms)
        ("Family business", "plumbing", None, [], "plumbing"),
        ("Family business", "home_services/plumbing", None, [], "plumbing"),
        ("Family business", "home_services/hvac", None, [], "hvac"),
        ("Family business", "Roofing", None, [], "roofing"),
        ("Family business", "home_services/remodeling", None, [], "remodel"),
        # the order: what the business does, then `trade`, then the consultation's service, then the services
        ("residential roofer", "plumbing", None, [], "roofing"),
        ("Family business", "plumbing", "Roof repair", [], "plumbing"),
        ("Family business", "plumbing", None, [{"name": "Roof repair"}], "plumbing"),
        # a `trade` that names none of the table's, or is no text, names nothing: the next text is read
        ("Family business", "home_services/windows_doors", "Roof repair", [], "roofing"),
        ("Family business", ["plumbing"], "Roof repair", [], "roofing"),
        ("Family business", None, "Roof repair", [], "roofing"),
    ],
)
def test_the_owner_set_trade_is_read_after_what_the_business_does_and_before_its_services(
    detail, trade_field, service, services, want
):
    knowledge = {"category_detail": detail, "trade": trade_field, "services": services}
    assert strategist.trade(knowledge, {"service": service, "services": [service] if service else []}) == want


def test_the_trade_is_read_from_the_shared_table_and_text_that_is_no_text_is_skipped():
    assert strategist.TRADES == trades.NAMES
    both = {"category_detail": None, "services": [None, {"name": None}, {"name": "Roof repair"}, "a string"]}
    assert strategist.trade(both, {"service": None, "services": None}) == "roofing"
    assert strategist.trade({}, {}) is None


def test_newsbreak_is_in_play_only_while_its_switch_is_on_and_the_service_fits_or_the_plan_has_it(monkeypatch):
    assert strategist.channels(config.load(), {"newsbreak_fit"}, None) == ("google_search",)  # the switch is off
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    s = config.load()
    assert strategist.channels(s, {"newsbreak_fit"}, None) == ("google_search", "newsbreak")
    assert strategist.channels(s, {"meta_fit"}, None) == ("google_search",)
    named = {"ads": {"channels": [], "named": {"newsbreak": 300}}}
    funded = {"ads": {"channels": [{"channel": "newsbreak", "monthly_usd": 300}]}}
    both = ("google_search", "newsbreak")
    assert strategist.channels(s, set(), named) == strategist.channels(s, set(), funded) == both


def test_the_topics_run_compliance_trade_search_newsbreak_offers():
    assert strategist.topics("roofing", ("google_search", "newsbreak")) == (
        "compliance",
        "roofing",
        "google-search",
        "newsbreak",
        "offers",
    )
    assert strategist.topics(None, ("google_search",)) == ("compliance", "google-search", "offers")


def test_kept_holds_only_ids_given_once_in_the_drafts_order():
    given = ki.Retrieved("x", ["dentists/compliance", "dentists/google-search"])
    named = [
        "dentists/google-search",
        " dentists/compliance ",
        "dentists/made-up",
        "attorneys/compliance",
        "dentists/google-search",
    ]
    assert strategist.kept(named, given) == ["dentists/google-search", "dentists/compliance"]
    assert strategist.kept(["dentists/compliance"], None) == []


def test_knowledge_for_reads_the_trees_files_for_the_trade_and_the_channels(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    for topic, chans in (
        ("compliance", ()),
        ("roofing", ("google_search",)),
        ("google-search", ("google_search",)),
        ("newsbreak", ("newsbreak",)),
        ("offers", ()),
    ):
        kf.write(root, "home_services", topic, channels=chans)
    kf.write(root, "dentists", "compliance", channels=())
    monkeypatch.setenv("AGENT_KNOWLEDGE", "on")
    roofer = {"category_detail": "roofer", "services": []}
    got = strategist.knowledge_for(config.load(), "home_services", roofer, {"service": "Roofing"}, {"newsbreak_fit"})
    assert got.ids == [  # NewsBreak's switch is off: not in play
        "home_services/compliance",
        "home_services/roofing",
        "home_services/google-search",
        "home_services/offers",
    ]
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    got = strategist.knowledge_for(config.load(), "home_services", roofer, {"service": "Roofing"}, {"newsbreak_fit"})
    assert got.ids[3:] == ["home_services/newsbreak", "home_services/offers"]


def test_knowledge_for_reads_the_trade_file_of_a_trade_only_the_owner_set(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    for topic in ("compliance", "roofing", "plumbing", "google-search"):
        kf.write(root, "home_services", topic, channels=())
    monkeypatch.setenv("AGENT_KNOWLEDGE", "on")
    owner_set = {"category_detail": "Family business", "trade": "home_services/plumbing", "services": []}
    got = strategist.knowledge_for(config.load(), "home_services", owner_set, {"service": "Roof repair"}, set())
    assert got.ids == ["home_services/compliance", "home_services/plumbing", "home_services/google-search"]


def test_knowledge_for_reads_nothing_while_the_switch_is_off_or_the_tree_has_no_file(tmp_path, monkeypatch):
    kf.use(monkeypatch, kf.tree(tmp_path))
    assert strategist.knowledge_for(config.load(), "dentists", {}, {}, set()) is None
    monkeypatch.setenv("AGENT_KNOWLEDGE", "on")
    assert strategist.knowledge_for(config.load(), "dentists", {}, {}, set()).ids[0] == "dentists/compliance"
    assert strategist.knowledge_for(config.load(), "fallback", {}, {}, set()) is None


def test_the_answerer_reads_the_files_the_plan_cites(tmp_path, monkeypatch):
    kf.use(monkeypatch, kf.tree(tmp_path))
    monkeypatch.setenv("AGENT_KNOWLEDGE", "on")
    s = config.load()
    told = strategist.for_answer(s, {"knowledge_ids": ["dentists/offers", "dentists/gone"]})
    assert told.startswith("== dentists/offers ==") and "dentists/compliance" not in told
    assert strategist.for_answer(s, {"knowledge_ids": []}) is None and strategist.for_answer(s, None) is None
    monkeypatch.delenv("AGENT_KNOWLEDGE")
    assert strategist.for_answer(config.load(), {"knowledge_ids": ["dentists/offers"]}) is None
