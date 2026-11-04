"""What the Researcher reads (capability layer K4, spec 2026-10-07 §4 Researcher row): the business, its area and
plan, the topics, the benchmark rows, Admiry's guides within a budget, one Places call within the day's budget, up to
ten pages of the businesses nearby -- each source with an id -- and fetch_more's bigger look without a second call.
Admiry's guides are K3's files, read only with AGENT_KNOWLEDGE on (K3's `knowledge_on` fixture, its knowledge_fixtures).
A business nearby is described, never named: its name, address, website and phone stay in memory for the run, and a
source's label and link are its place's -- never its page's title or host, which are its name, nor its website."""

import dataclasses
import json
import logging
import re
import socket
from decimal import Decimal

import httpx
import pytest

from agent import audit, benchmark, config, crawl, places
from agent.copy import jargon
from agent.roles import research_material as rm
from agent.tests import research_fixtures as rf
from agent.tests.fixtures import home_services_knowledge

MAPS = "https://www.google.com/maps/search/"


def _gather(conn, **brief) -> rm.Gathered:
    return rm.gather(conn, config.load(), rf.BIZ, brief)


def _spend_places(conn, n: int) -> None:
    """`n` of the day's Places lookups already made, by anyone."""
    for _ in range(n):
        audit.record_system(conn, tool="places.lookup", actor="places", business_id=None, args={}, result={})


def _public_web(monkeypatch, sites: dict[str, str] | None = None) -> list[str]:
    """The crawl as it runs, with no network: every name resolves to a public address and a request is answered from
    `sites` (host -> html), 404 otherwise. Returns the URLs requested."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    requested: list[str] = []
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        html = (sites or {}).get(request.url.host)
        if html is None:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    monkeypatch.setattr(crawl.httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))
    return requested


def _place(place_id: str, name: str, website: str | None, phone: str | None = None) -> places.Candidate:
    """A place Google found a little north of the business, as `rf.nearby` places its rivals."""
    return places.Candidate(place_id, name, "9 Main St, Palo Alto, CA", 37.45, -122.143, website=website, phone=phone)


def _nearby_ids(conn, monkeypatch, found: list[places.Candidate], **knowledge) -> list[str]:
    """The place ids of the businesses nearby when Places answers `found` for the dental fixture's business with
    `knowledge` changed. Its own listing carries no place id, so only its site, its phone or its name can tell it."""
    base = {**rf.knowledge_fixture().model_dump(), "listing": {**rf.LISTING, "place_id": None}, **knowledge}
    rf.seed(conn, knowledge=base)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch, found)
    rf.fake_pages(monkeypatch)
    return [c.place_id for c in _gather(conn).competitors]


def test_the_material_reads_the_business_its_area_plan_topics_and_benchmark(conn, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)  # a developer's export must not make a Places call
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    g = _gather(conn, topics=["competitors", "teleport"], note="Do people here want Invisalign?")
    m = g.material
    assert (g.vertical, g.category, g.places, g.channels) == ("dentists", "dentists", "no_key", ["google_search"])
    assert m["business"]["name"] == "Bright Smile Dental" and m["trade"] == "family dentist"
    assert m["area"]["town"] == "Palo Alto" and m["area"]["radius_miles"] == 10
    assert m["plan"]["searches"] == ["invisalign palo alto"] and m["plan"]["kept_out"] == ["free"]
    assert [t["id"] for t in m["topics"]] == ["competitors"] and m["owner_asked"] == "Do people here want Invisalign?"
    assert {"id": "benchmark:dentists:google_search", "kind": "benchmark"}.items() <= next(
        s for s in m["sources"] if s["id"] == "benchmark:dentists:google_search"
    ).items()
    row = next(b for b in m["benchmark"] if b["id"] == "benchmark:dentists:google_search")
    assert row["cost_per_lead"] == "$72.97" and "$72.97" in g.sources[row["id"]]["text"]
    assert g.sources["business"]["kind"] == "business" and m["competitors"] == [] and g.pages == []


def test_no_topic_asked_reads_every_topic(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    assert [t["id"] for t in _gather(conn).material["topics"]] == list(rm.TOPICS)


def test_topics_asked_are_read_once_each_in_the_order_asked_and_none_known_asks_for_all(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    asked = _gather(conn, topics=["seasonality", "competitors", "seasonality"])
    assert asked.topics == ["seasonality", "competitors"]
    assert [t["id"] for t in asked.material["topics"]] == ["seasonality", "competitors"]
    assert [t["id"] for t in _gather(conn, topics=["teleport"]).material["topics"]] == list(rm.TOPICS)


def test_a_business_with_no_plan_is_read_from_its_knowledge_alone(conn, monkeypatch):
    rf.seed(conn)
    conn.execute("DELETE FROM plan")
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert g.material["plan"] is None and g.channels == ["google_search"]
    assert g.material["area"]["town"] == "Palo Alto" and g.material["area"]["radius_miles"] is None
    assert [c["query"] for c in calls] == ["family dentist in Palo Alto"]


def test_a_business_with_no_knowledge_is_read_against_the_fallback_averages(conn, monkeypatch):
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'claimed')", (rf.BIZ,))
    benchmark.seed(conn)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    rf.no_guides(monkeypatch)
    g = _gather(conn)
    assert (g.vertical, g.category, g.places) == ("fallback", "fallback", "no_key")
    assert "benchmark:fallback:google_search" in {b["id"] for b in g.benchmark}


def test_one_places_call_finds_the_businesses_nearby_and_leaves_out_this_one(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert calls == [{"query": "family dentist in Palo Alto", "max_results": 10, "with_identity": True}]
    assert g.places == "called" and len(g.competitors) == 6 and g.shown == 5
    assert [c["id"] for c in g.material["competitors"]] == [f"place:{k}" for k in range(1, 6)]
    assert g.material["competitors"][0]["miles"] == 1.0 and g.material["competitors"][0]["page"] == "page:1"
    [row] = conn.execute(
        "SELECT business_id, args_redacted, result FROM agent_action WHERE tool = 'places.lookup'"
    ).fetchall()
    assert row["business_id"] is None and row["args_redacted"]["research"] == rf.BIZ and row["result"] == {"found": 7}


def test_a_business_that_says_no_more_than_its_services_is_searched_by_its_first_service(conn, monkeypatch):
    knowledge = {**rf.knowledge_fixture().model_dump(), "category_detail": None, "listing": rf.LISTING}
    rf.seed(conn, knowledge=knowledge)
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    assert _gather(conn).places == "called"
    assert [c["query"] for c in calls] == ["Invisalign in Palo Alto"]


def test_a_search_that_finds_only_the_business_itself_finds_none(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch, rf.nearby(0))
    tried = rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert (g.places, g.competitors, g.shown, g.pages, tried) == ("none_found", [], 0, [], [])
    [row] = conn.execute("SELECT result FROM agent_action WHERE tool = 'places.lookup'").fetchall()
    assert row["result"] == {"found": 1}  # the call was made and counted


def test_the_business_itself_is_never_listed_as_a_business_nearby(conn, monkeypatch):
    """Each decoy matches the business by one thing only -- its place id, its website's host (`www.` aside), its name
    (case and the DDS aside) -- so each way of telling is needed."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    here = (37.4419, -122.143)
    found = [
        places.Candidate("ChIJself", "Another Name Entirely", "elsewhere", *here, website="https://elsewhere.example"),
        places.Candidate(
            "ChIJ77", "Some Dental Group", "elsewhere", *here, website="https://www.brightsmile.example/x"
        ),
        places.Candidate("ChIJ78", "BRIGHT SMILE DENTAL, DDS", "elsewhere", *here, website="https://bs.example"),
        places.Candidate(
            "ChIJ1", "Rival Dental 1, DDS", "1 Main St", 37.4564, -122.143, website="https://rival1.example"
        ),
    ]
    rf.fake_places(monkeypatch, found)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert [c.place_id for c in g.competitors] == ["ChIJ1"] and g.places == "called"


RIVAL = ("ChIJ1", "Rival Dental 1, DDS", "https://rival1.example")


@pytest.mark.parametrize(
    "stored",
    [
        {"website": "brightsmile.example"},  # a website stored without its scheme
        {"website": None, "website_unread": "https://brightsmile.example"},  # the address Admiry could not read
    ],
)
def test_the_business_is_known_by_its_website_however_it_was_stored(conn, monkeypatch, stored):
    found = [_place("ChIJo", "Another Name", "https://www.BrightSmile.example/contact"), _place(*RIVAL)]
    assert _nearby_ids(conn, monkeypatch, found, **stored) == ["ChIJ1"]


def test_a_website_on_a_shared_host_is_the_business_only_for_the_same_page(conn, monkeypatch):
    found = [
        _place("ChIJown", "Our Page", "https://facebook.com/BrightSmileDental/"),  # the business's own page
        _place("ChIJfb", "Rival On Facebook", "https://www.facebook.com/rivalondental"),  # another business's
        _place(*RIVAL),
    ]
    ids = _nearby_ids(conn, monkeypatch, found, website="https://www.facebook.com/brightsmiledental")
    assert ids == ["ChIJfb", "ChIJ1"]


def test_a_place_with_the_businesss_phone_is_the_business(conn, monkeypatch):
    found = [
        _place("ChIJp", "BSD Palo Alto", "https://bsd.example", "(650) 555-0100"),
        _place(*RIVAL, "(650) 555-0111"),
    ]
    assert _nearby_ids(conn, monkeypatch, found) == ["ChIJ1"]


def test_a_rival_that_only_shares_words_with_the_businesss_name_is_still_a_business_nearby(conn, monkeypatch):
    found = [_place("ChIJo", "Bright Smile Orthodontics", "https://brightsmileortho.example", "(650) 555-0190")]
    assert _nearby_ids(conn, monkeypatch, found) == ["ChIJo"]


def test_stored_values_that_are_no_text_cannot_fail_the_run_after_the_lookup_is_spent(conn, monkeypatch):
    stored = {"name": 5, "website": 123, "phone": 6505550100, "listing": rf.LISTING}
    rf.seed(conn, knowledge={**rf.knowledge_fixture().model_dump(), **stored})
    rf.no_guides(monkeypatch)
    rf.fake_places(
        monkeypatch, [_place("ChIJself", "Bright Smile Dental", "https://brightsmile.example"), _place(*RIVAL)]
    )
    rf.fake_pages(monkeypatch)
    assert [c.place_id for c in _gather(conn).competitors] == ["ChIJ1"]  # its place id still tells the business
    assert [rm.host(123), rm.host(["x"]), rm.core(5), rm.core(None)] == [None, None, "", ""]


def test_research_never_takes_the_last_quarter_of_the_days_places(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    _spend_places(conn, int(config.load().places_daily_budget * rm.PLACES_SHARE))
    g = _gather(conn)
    assert (g.places, g.competitors, calls) == ("budget", [], [])


def test_research_may_use_the_days_places_up_to_three_quarters_of_the_budget(conn, monkeypatch):
    monkeypatch.setenv("AGENT_PLACES_DAILY_BUDGET", "8")  # three quarters of it: 6 lookups
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    _spend_places(conn, 5)
    assert _gather(conn).places == "called" and len(calls) == 1
    assert _gather(conn).places == "budget" and len(calls) == 1  # the run's own lookup was the sixth


def test_no_area_no_call(conn, monkeypatch):
    knowledge = {**rf.knowledge_fixture().model_dump(), "service_area": [], "listing": None}
    rf.seed(conn, knowledge=knowledge)
    conn.execute("UPDATE plan SET json = jsonb_set(json, '{sections,consultation,business,location}', 'null')")
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch)
    assert (_gather(conn).places, calls) == ("no_area", [])


def test_round_one_reads_the_pages_of_the_shown_businesses_cut_to_size(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    long = "x" * (rm.PAGE_CHARS + 500)
    tried = rf.fake_pages(monkeypatch, {f"https://rival{k}.example": long for k in range(1, 11)})
    g = _gather(conn)
    assert tried == [f"https://rival{k}.example" for k in range(1, 6)]
    assert [p["id"] for p in g.pages] == [f"page:{k}" for k in range(1, 6)]
    assert all(len(p["text"]) == rm.PAGE_CHARS for p in g.pages) and g.pages[0]["of"] == "place:1"
    # the page source links to its place on Google Maps, not to the website Places gave
    assert g.sources["page:1"]["url"] == places.maps_url(None, "ChIJ1") and g.sources["page:1"]["kind"] == "page"
    assert g.sources["page:1"]["text"] == "x" * rm.PAGE_CHARS


def test_a_page_that_cannot_be_read_is_skipped(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch, {"https://rival2.example": "Evening hours on Thursdays."})
    g = _gather(conn)
    assert [p["of"] for p in g.pages] == ["place:2"] and g.tried == {1, 2, 3, 4, 5}


def test_a_business_without_a_website_has_no_page_and_costs_no_attempt(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    found = [dataclasses.replace(c, website=None) if c.place_id == "ChIJ2" else c for c in rf.nearby()]
    rf.fake_places(monkeypatch, found)
    tried = rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert tried == [f"https://rival{k}.example" for k in (1, 3, 4, 5)] and g.tried == {1, 3, 4, 5}
    assert [c["page"] for c in g.material["competitors"]] == ["page:1", None, "page:2", "page:3", "page:4"]
    rm.more(g)  # the sixth is read next: the attempts were four, not five
    assert tried[-1] == "https://rival6.example" and len(g.pages) == 5


def test_two_businesses_with_one_website_read_it_once(conn, monkeypatch):
    """One page per site: two offices of one practice list one website, and its words are one claim, not two."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    offices = [
        _place("ChIJc1", "Chain Dental Downtown", "https://chaindental.example/"),
        _place("ChIJc2", "Chain Dental Midtown", "https://www.ChainDental.example/locations"),
    ]
    rf.fake_places(monkeypatch, [*offices, *rf.nearby(4, own=False)])
    pages = {f"https://rival{k}.example": f"Rival {k} offers free whitening." for k in range(1, 5)}
    tried = rf.fake_pages(monkeypatch, {"https://chaindental.example/": "Two offices, one practice.", **pages})
    g = _gather(conn)
    assert tried == ["https://chaindental.example/", *[f"https://rival{k}.example" for k in (1, 2, 3)]]
    assert g.tried == {1, 3, 4, 5} and [p["of"] for p in g.pages] == ["place:1", "place:3", "place:4", "place:5"]
    assert [c["page"] for c in g.material["competitors"]] == ["page:1", None, "page:2", "page:3", "page:4"]
    rm.more(g)  # the sixth shown is rival 4; the second office's site was read and stays read
    assert tried[-1] == "https://rival4.example" and tried.count("https://chaindental.example/") == 1


def test_a_business_whose_website_is_a_page_on_a_shared_host_is_listed_but_not_read(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    on_facebook = _place("ChIJfb", "Rival On Facebook", "https://www.facebook.com/rivalondental")
    rf.fake_places(monkeypatch, [on_facebook, *rf.nearby(2, own=False)])
    tried = rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert tried == ["https://rival1.example", "https://rival2.example"] and g.tried == {2, 3}
    assert [c["page"] for c in g.material["competitors"]] == [None, "page:1", "page:2"]  # still shown, as place:1


@pytest.mark.parametrize(
    "site",
    [
        "https://acme.business.admiry.ai/",  # a page Admiry hosts
        "https://admiry.ai/u/acme-dental",  # a site Admiry builds, on its own domain
        "https://www.Admiry.ai/x",
        "https://acme.business.dev.adx.admiry.ai",  # dev's
        "https://acme.pages.example/",  # AGENT_PAGES_DOMAIN's
        "http://testserver/u/acme-dental",  # the agent's own host (AGENT_PUBLIC_BASE_URL)
    ],
)
def test_a_page_admiry_hosts_is_never_read_so_research_cannot_tell_who_uses_admiry(conn, monkeypatch, site):
    """Founder ruling 2026-10-09 (the B0 R3 boundary): research is public, unnamed market research, so it never says
    whether a business nearby uses Admiry -- it never reads a page on one of Admiry's own hosts. The business stays
    listed, as one without a website is; its page is one page fewer, and no attempt."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "pages.example")
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch, [_place("ChIJa", "Acme Dental", site), _place(*RIVAL)])
    tried = rf.fake_pages(monkeypatch, {site: "Built with Admiry.", "https://rival1.example": "Free whitening."})
    g = _gather(conn)
    assert [c.place_id for c in g.competitors] == ["ChIJa", "ChIJ1"] and g.shown == 2
    assert tried == ["https://rival1.example"] and g.tried == {2}
    assert [c["page"] for c in g.material["competitors"]] == [None, "page:1"]
    assert "Built with" not in json.dumps({"material": g.material, "pages": g.pages})  # its words never read


def test_fetch_page_skips_what_the_crawl_refuses(monkeypatch):
    def refuse(url, **kw):
        raise crawl.CrawlError("website_url must be a public address")

    monkeypatch.setattr(crawl, "fetch_site", refuse)
    assert rm.fetch_page("http://10.0.0.1/") is None


def test_fetch_page_skips_an_address_the_real_crawl_refuses_and_says_nothing_of_it(monkeypatch, caplog):
    """No network: a private address is refused before any lookup. The normal skip is not logged."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("a lookup was made"))
    with caplog.at_level(logging.DEBUG, logger=rm.__name__):
        assert [rm.fetch_page("http://10.0.0.1/"), rm.fetch_page("http://localhost/")] == [None, None]
    assert [r for r in caplog.records if r.name == rm.__name__] == []


@pytest.mark.parametrize(
    "error", [UnicodeError("bad label"), OSError("unreachable"), ValueError("bad"), RecursionError()]
)
def test_fetch_page_gives_none_for_any_error_a_strangers_site_raises(monkeypatch, error):
    def fetch_site(url, **kw):
        raise error

    monkeypatch.setattr(crawl, "fetch_site", fetch_site)
    assert rm.fetch_page("https://rival.example/") is None


def test_a_site_that_cannot_be_read_is_logged_by_its_error_type_alone(monkeypatch, caplog):
    def fetch_site(url, **kw):
        raise UnicodeError(f"cannot read {url}")

    monkeypatch.setattr(crawl, "fetch_site", fetch_site)
    with caplog.at_level(logging.WARNING, logger=rm.__name__):
        assert rm.fetch_page("https://secret-rival.example/") is None
    assert "UnicodeError" in caplog.text and "secret-rival" not in caplog.text


def test_a_website_the_crawl_cannot_even_build_a_request_for_is_skipped_through_the_real_crawl(monkeypatch):
    """A rival sets its own website: a punycode label that is no label passes the public-host check (DNS says public)
    and fails to build a request -- past the crawl's own guards."""
    requested = _public_web(monkeypatch)
    assert rm.fetch_page("https://xn--zz.rival.example/") is None and requested == []


def test_a_rival_with_such_a_website_does_not_fail_the_run_or_the_other_pages(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    hostile = _place("ChIJh", "Hostile Dental", "https://xn--zz.rival.example/")
    calls = rf.fake_places(monkeypatch, [hostile, *rf.nearby(2, own=False)])
    requested = _public_web(
        monkeypatch,
        {
            "rival1.example": "<html><body>One words.</body></html>",
            "rival2.example": "<html><body>Two words.</body></html>",
        },
    )
    g = _gather(conn)
    assert g.places == "called" and len(calls) == 1 and g.tried == {1, 2, 3}
    assert [p["of"] for p in g.pages] == ["place:2", "place:3"] and len(requested) == 2


def test_a_site_the_crawl_refuses_costs_the_run_that_page_and_nothing_else(conn, monkeypatch):
    """Through the real fetch_page: a business whose site is on a private address, or cannot be read, is a page fewer
    in the material, not a failed run."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)

    def fetch_site(url, **kw):
        if "rival2" in url:
            raise crawl.CrawlError("website_url must be a public address")
        return [crawl.Page(url=url, title="Home", text=f"The page of {url}")]

    monkeypatch.setattr(crawl, "fetch_site", fetch_site)
    g = _gather(conn)
    assert [p["of"] for p in g.pages] == ["place:1", "place:3", "place:4", "place:5"]
    assert g.tried == {1, 2, 3, 4, 5} and g.places == "called"


def test_fetch_page_reads_one_page_of_a_site_and_none_when_the_site_gave_none(monkeypatch):
    seen: list[tuple[str, dict]] = []
    served: list[list[crawl.Page]] = [[crawl.Page(url="https://a.example/", title="A", text="first")], []]

    def fetch_site(url, **kw):
        seen.append((url, kw))
        return served.pop(0)

    monkeypatch.setattr(crawl, "fetch_site", fetch_site)
    page = rm.fetch_page("https://a.example/")
    assert (page.text, rm.fetch_page("https://b.example/")) == ("first", None)
    assert seen == [("https://a.example/", {"max_pages": 1}), ("https://b.example/", {"max_pages": 1})]


def test_fetch_more_shows_every_business_and_reads_up_to_ten_pages(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    calls = rf.fake_places(monkeypatch, rf.nearby(12))
    tried = rf.fake_pages(monkeypatch)
    g = rm.more(_gather(conn))
    assert len(calls) == 1 and g.fetched_more and g.shown == 12
    assert len(tried) == rm.MAX_PAGES == 10 and len(g.material["competitors"]) == 12
    assert tried == [f"https://rival{k}.example" for k in range(1, 11)]  # round one's five are not read again


def test_the_guides_a_draft_would_read_are_retrieved_within_their_budget(conn, monkeypatch, knowledge_on):
    """K3's `knowledge_on`: AGENT_KNOWLEDGE on, its small reviewed tree (dentists' compliance, google-search and
    offers; attorneys' compliance) standing in for agent/knowledge."""
    from agent import knowledge_index as ki

    rf.seed(conn)
    real = rm.knowledge_index.retrieve
    asked: list[dict] = []

    def spy(vertical, **kw):
        asked.append({"vertical": vertical, **kw})
        return real(vertical, **kw)

    monkeypatch.setattr(rm.knowledge_index, "retrieve", spy)
    g = _gather(conn)
    assert asked == [
        {
            "vertical": "dentists",
            "channels": ["google_search"],
            "topics": ("compliance", "google-search", "offers"),  # strategist.topics: no trade, Google Search only
            "budget_tokens": rm.KNOWLEDGE_TOKENS,
        }
    ]
    ids = ["dentists/compliance", "dentists/google-search", "dentists/offers"]
    assert g.knowledge_on and g.material["knowledge"]["ids"] == [f"knowledge:{i}" for i in ids]
    assert g.material["knowledge"]["text"].startswith("== dentists/compliance ==")
    source = g.sources["knowledge:dentists/compliance"]
    assert source["url"] == "admiry://knowledge/dentists/compliance"
    assert source["text"] == ki.block(ki.get("dentists/compliance")) and "[src-one]" not in source["text"]
    rm.more(g)
    assert asked[-1]["budget_tokens"] == rm.MORE_KNOWLEDGE_TOKENS


def test_a_trades_own_file_is_read_for_a_business_of_that_trade(conn, monkeypatch, tmp_path):
    from agent.tests import knowledge_fixtures as kf

    root = kf.use(monkeypatch, tmp_path / "knowledge")
    kf.write(root, "home_services", "roofing", tags=("stage:plan", "trade:roofing"))
    kf.write(root, "home_services", "plumbing", tags=("stage:plan", "trade:plumbing"))
    monkeypatch.setenv("AGENT_KNOWLEDGE", "on")
    rf.seed(conn, knowledge={**home_services_knowledge("Roofing"), "listing": rf.LISTING})
    g = _gather(conn)
    assert (g.vertical, g.trade) == ("home_services", "roofing")
    assert g.material["knowledge"]["ids"] == ["knowledge:home_services/roofing"]
    assert g.sources["knowledge:home_services/roofing"]["label"] == "Admiry's guide to home services: roofing"


def test_with_the_knowledge_switch_off_research_reads_no_guide(conn, monkeypatch, tmp_path):
    from agent.tests import knowledge_fixtures as kf

    kf.use(monkeypatch, kf.tree(tmp_path / "knowledge"))  # the files are there; AGENT_KNOWLEDGE is not on
    rf.seed(conn)

    def never(*a, **k):
        raise AssertionError("no guide is read while AGENT_KNOWLEDGE is off")

    monkeypatch.setattr(rm.knowledge_index, "retrieve", never)
    g = _gather(conn)
    assert g.material["knowledge"] is None and not g.knowledge_on
    rm.more(g)
    assert g.material["knowledge"] is None


def test_a_malformed_guide_leaves_the_research_without_guides(conn, monkeypatch, knowledge_on):
    from agent import knowledge_index as ki

    (knowledge_on / "dentists" / "broken.md").write_text("no frontmatter", encoding="utf-8")
    ki.forget()
    rf.seed(conn)
    assert _gather(conn).material["knowledge"] is None  # K3 logs the file and reads none until it is fixed


def test_a_guide_that_goes_after_it_was_retrieved_is_left_out_and_the_run_goes_on(
    conn, monkeypatch, knowledge_on, caplog
):
    """K3's retrieve reads each file inside its own guard; the source's words are read again afterwards, and a file
    can be gone by then. The guide is left out whole -- its words, its id, its source -- and logged."""
    rf.seed(conn)
    real = rm.knowledge_index.retrieve

    def retrieve_then_lose_a_file(vertical, **kw):
        got = real(vertical, **kw)
        (knowledge_on / "dentists" / "offers.md").unlink()
        return got

    monkeypatch.setattr(rm.knowledge_index, "retrieve", retrieve_then_lose_a_file)
    with caplog.at_level(logging.ERROR, logger=rm.__name__):
        g = _gather(conn)
    ids = ["knowledge:dentists/compliance", "knowledge:dentists/google-search"]
    assert g.material["knowledge"]["ids"] == ids and "knowledge:dentists/offers" not in g.sources
    assert "dentists/offers" not in g.material["knowledge"]["text"] and "dentists/compliance" in g.knowledge["text"]
    assert "dentists/offers" in caplog.text


def test_a_tree_without_guides_reads_none(conn, monkeypatch, knowledge_on):
    knowledge = {**rf.knowledge_fixture().model_dump(), "category": "automotive_repair", "listing": rf.LISTING}
    rf.seed(conn, knowledge=knowledge)
    g = _gather(conn)
    assert g.vertical == "fallback" and g.material["knowledge"] is None


def test_names_are_found_whole_and_without_their_suffixes():
    names = rm.names_of(["Rival Dental 2, DDS", "Smile Studio PLLC"])
    assert rm.names_in("We beat rival dental 2 on price.", names) == "rival dental 2"
    assert rm.names_in("Smile Studio, PLLC opened.", names) == "Smile Studio, PLLC"  # the longest form found
    assert rm.names_in("Two smile studios nearby.", names) is None
    # a trailing number is a branch's, as a store number is (the re-review's M-1): Rival Dental is the brand
    assert rm.names_in("Rival Dental 21 is far.", names) == "Rival Dental"
    assert [rm.core("Rival Dental 2, DDS"), rm.core("Smile Studio PLLC")] == ["Rival Dental 2", "Smile Studio"]


def test_a_name_is_masked_wherever_names_in_finds_it_and_nowhere_else():
    """Task 3 R6: what is kept of a sentence that named a business nearby has the name replaced, by the rule that found
    it -- whole words, in any case, & as and, anything but a word between them -- the longest form first (the final
    review's I1: a name with its suffix goes whole)."""
    names = rm.names_of(["Rival Dental 2, DDS", "Smile Studio PLLC", "Smith & Sons Dental"])
    mask = rm.NEARBY_MASK
    assert mask == "[a business nearby]"
    texts = {
        "We beat rival dental 2 on price.": f"We beat {mask} on price.",
        "Rival Dental 2, DDS and SMILE-STUDIO both open.": f"{mask} and {mask} both open.",
        "Rival   Dental\n2": mask,
        "(Rival Dental 2)": f"({mask})",
        "Smith & Sons Dental, again, smith  and  sons dental": f"{mask}, again, {mask}",
        "Rival Dental 2 and Rival Dental 2": f"{mask} and {mask}",
        "Rival Dental 21 is far; two smile studios nearby; Smith&Sons.": f"{mask} 21 is far; two smile studios "
        "nearby; Smith&Sons.",  # the brand without its branch's number (M-1); no other name whole
        "": "",
    }
    for text, masked in texts.items():
        got = rm.mask_names(text, names)
        assert got == (text if masked is None else masked), text
        assert rm.names_in(got, names) is None, text
    assert rm.mask_names("Rival Dental 2", rm.Names()) == "Rival Dental 2"


def test_a_host_is_read_in_lower_case_without_www():
    assert rm.host("https://WWW.Acme.Example/contact?x=1") == "acme.example"
    assert rm.host("http://shop.acme.example:8080/") == "shop.acme.example"
    assert [rm.host(None), rm.host(""), rm.host("no host"), rm.host("http://[bad")] == [None, None, None, None]


def test_names_are_those_of_every_business_found_long_enough_to_look_for(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    short = places.Candidate("ChIJjo", "Jo", "1 Main St", 37.4419, -122.143, website="https://jo.example")
    rf.fake_places(monkeypatch, [*rf.nearby(7), short])
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    forms = {" ".join(f.words) for f in g.names.forms}
    # all seven are named though five are shown; "Jo" is shorter than MIN_NAME
    assert g.shown == 5 and {f"rival dental {k}" for k in range(1, 8)} <= forms and "jo" not in forms
    assert g.names.own == (("bright", "smile", "dental"), ("bright", "smile"))  # the business's own, never a rival's


def _rivals_with_sites(conn, monkeypatch) -> rm.Gathered:
    """Seven rivals (rival1.example .. rival7.example), a second office of the first, and a practice whose website is
    gentle.dental, beside three that have no host a sentence could be held to: one too short, a Facebook page, none."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    short = places.Candidate("ChIJjo", "Jo", "1 Main St", 37.4419, -122.143, website="https://www.a.b/x")
    shared = places.Candidate("ChIJfb", "Fb Dental", "2 Main St", 37.4419, -122.143, website="https://facebook.com/fb")
    nowhere = places.Candidate("ChIJnw", "No Site Dental", "3 Main St", 37.4419, -122.143, website=None)
    gentle = places.Candidate(
        "ChIJgd", "Smile Studio", "4 Main St", 37.4419, -122.143, website="https://www.Gentle.Dental/"
    )
    east = places.Candidate(
        "ChIJr1e", "Rival Dental 1 East", "5 Main St", 37.4419, -122.143, website="https://rival1.example/e"
    )
    rf.fake_places(monkeypatch, [*rf.nearby(7), east, short, shared, nowhere, gentle])
    rf.fake_pages(monkeypatch)
    return _gather(conn)


def test_the_hosts_of_the_businesses_nearby_are_those_of_their_websites_that_name_one_business(conn, monkeypatch):
    """K4 Task 4 fix rounds 1-2: a host names a business as its name does. Every business found (five are shown), the
    host without `www.`, each once (two offices of a practice share one); not a host shorter than a name may be, nor one
    that is no one business's (a Facebook page)."""
    g = _rivals_with_sites(conn, monkeypatch)
    assert g.hosts == (*(f"rival{k}.example" for k in range(1, 8)), "gentle.dental")


def test_a_host_is_found_only_as_a_host_is_written(conn, monkeypatch):
    """K4 Task 4 fix round 2: `rival3.example`, with or without `www.`, in any case, with spaces around a dot, a full
    stop or a port after it -- never the same words with no dot (`gentle dental care` is not gentle.dental), never one
    more label before it or after it."""
    hosts = _rivals_with_sites(conn, monkeypatch).hosts
    found = {
        "rival3.example near me": "rival3.example",
        "Visit www.Rival3.Example for prices": "rival3.example",
        "see https://www.rival1.example/contact": "rival1.example",
        "rival3 . example": "rival3.example",
        "rival3 .example": "rival3.example",
        "rival4. example": "rival4.example",
        "visit rival7.example.": "rival7.example",
        "rival7.example:8080/menu": "rival7.example",
        "mail info@rival5.example now": "rival5.example",
        "Gentle.Dental is near": "gentle.dental",
        "WWW . GENTLE . DENTAL": "gentle.dental",
        "shop.rival2.example": "rival2.example",
    }
    for text, host in found.items():
        assert rm.host_in(text, hosts) == host, text
    for text in (
        "gentle dental care",
        "rival3 example hours",
        "xrival4.example and my-rival4.example",
        "rival5.examples",
        "rival5.example.com",
        "rival10.example",
        "example.com",
        "",
    ):
        assert rm.host_in(text, hosts) is None, text
    assert rm.host_in("gentle.dental", ()) is None


def test_a_host_with_a_hyphen_or_a_subdomain_is_found_whole_and_only_whole():
    hosts = ("gentle-dental.com", "shop.rival.example")
    assert rm.host_in("see gentle-dental.com today", hosts) == "gentle-dental.com"
    assert rm.host_in("see www.shop.rival.example/x", hosts) == "shop.rival.example"
    for text in (
        "my-gentle-dental.com",
        "gentle dental.com",
        "gentle-dental.com.au",
        "rival.example",
        "shop rival example",
    ):
        assert rm.host_in(text, hosts) is None, text
    assert rm.mask_hosts("gentle-dental.com, www.shop.rival.example.", hosts) == f"{rm.NEARBY_MASK}, {rm.NEARBY_MASK}."


def test_a_host_is_masked_as_it_is_found_and_nowhere_else(conn, monkeypatch):
    """K4 Task 4 fix rounds 1-2 (D5): what a run keeps of a text that names a business nearby's website has the host
    replaced -- the `www.` with it, the longest host first -- and a text with only the words of one is left as it is."""
    mask, hosts = rm.NEARBY_MASK, _rivals_with_sites(conn, monkeypatch).hosts
    texts = {
        "rival3.example near me": f"{mask} near me",
        "Visit www.Rival3.Example for prices": f"Visit {mask} for prices",
        "see https://www.rival1.example/contact": f"see https://{mask}/contact",
        "RIVAL2.example and rival3 . example": f"{mask} and {mask}",
        "rival3 .example, rival4. example": f"{mask}, {mask}",
        "visit rival7.example.": f"visit {mask}.",
        "rival7.example:8080/menu": f"{mask}:8080/menu",
        "mail info@rival5.example now": f"mail info@{mask} now",
        "Gentle.Dental is near": f"{mask} is near",
        "gentle dental care": None,  # the words of gentle.dental are no host
        "rival3 example hours": None,
        "xrival4.example and my-rival4.example": None,
        "rival10.example, rival5.examples, rival5.example.com, example.com": None,
        "": "",
    }
    for text, masked in texts.items():
        assert rm.mask_hosts(text, hosts) == (text if masked is None else masked), text
        assert rm.host_in(rm.mask_hosts(text, hosts), hosts) is None, text
    assert rm.mask_hosts("shop.rival.example and rival.example", ("rival.example", "shop.rival.example")) == (
        f"{mask} and {mask}"
    )
    assert rm.mask_hosts("rival3.example", ()) == "rival3.example"


def test_the_counts_a_sentence_may_state_are_those_the_material_gives(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert g.counts() == {Decimal(5), Decimal(6), Decimal(10)}  # shown and read 5, found 6, the plan's radius 10
    conn.execute("UPDATE plan SET json = jsonb_set(json, '{sections,consultation,geo,radius_miles}', 'null')")
    assert _gather(conn).counts() == {Decimal(5), Decimal(6)}


def test_a_findings_sources_are_counted_by_the_businesses_nearby_they_are_from(conn, monkeypatch):
    """The final review's I2: how many businesses nearby a finding's own sources are from -- a place and its own page
    are one business; a page the material cannot tie to a place counts as one of its own; other sources are none."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert g.businesses(["page:1", "page:2"]) == 2
    assert g.businesses(["page:1", "place:1"]) == 1
    assert g.businesses(["place:3", "benchmark:dentists:google_search", "business"]) == 1
    assert g.businesses([]) == 0 and g.businesses(["nothing:1"]) == 0
    g.sources["page:9"] = {"kind": "page", "label": "x", "url": None, "text": "x"}  # no page of the material
    g.pages[1]["of"] = None  # page:2 tied to no place
    assert g.businesses(["page:9", "place:1", "page:1"]) == 2
    assert g.businesses(["page:2", "place:2"]) == 2


def test_without_a_point_on_the_map_for_the_business_a_business_nearby_has_no_distance(conn, monkeypatch):
    rf.seed(conn, knowledge={**rf.knowledge_fixture().model_dump(), "listing": None})
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert [c["miles"] for c in g.material["competitors"]] == [None] * 5 and len(g.competitors) == 6
    assert g.sources["place:1"]["label"] == "A business nearby on Google Maps"
    assert g.sources["page:1"]["label"] == "The website of a business nearby"


def test_a_page_source_is_labelled_from_its_place_never_by_its_title_or_its_host(conn, monkeypatch):
    """A small business's page is titled with its name (`rf.page_title`): the label an owner reads says only where the
    page is from -- a business nearby, how far -- like its place's own."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert g.sources["place:2"]["label"] == "A business nearby on Google Maps, 2.0 miles away"
    assert g.sources["page:1"]["label"] == "The website of a business nearby, 1 mile away"
    assert g.sources["page:2"]["label"] == "The website of a business nearby, 2.0 miles away"
    # the model still reads the page's title, inside <pages>: the guards drop a sentence that names the business
    assert g.pages[1]["title"] == "Rival Dental 2, DDS | Palo Alto"
    for source in g.sources.values():
        assert "Rival" not in source["label"] and "rival" not in source["label"]
        assert not any(page["title"] in source["label"] for page in g.pages)
    shown = {s["id"]: s["label"] for s in g.material["sources"]}
    assert shown["page:2"] == g.sources["page:2"]["label"]


def test_a_business_nearbys_name_address_website_and_phone_stay_out_of_the_material_and_the_digest(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch, [dataclasses.replace(c, phone="(650) 555-0199") for c in rf.nearby()])
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    links = [(s["label"], s["url"]) for s in g.sources.values()]
    shown = json.dumps([g.material, rm.digest(g), links])
    for leak in ("Rival Dental", "Main St", "rival1.example", "rival5.example", "555-0199"):
        assert leak not in shown


def test_a_business_nearby_prints_neither_its_name_nor_its_website_in_a_log_line(conn, monkeypatch):
    """A Competitor or a Gathered that finds its way into a log prints place ids and distances, and none of what Places
    or the pages gave: the names, the websites, the pages' words."""
    assert repr(rm.Competitor("ChIJ1", "Rival Dental 1, DDS", "https://rival1.example", 1.0)) == (
        "Competitor(place_id='ChIJ1', miles=1.0)"
    )
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    printed = repr(_gather(conn))
    assert "ChIJ1" in printed
    for leak in ("Rival Dental", "rival1.example", "rival5.example", "free whitening"):
        assert leak not in printed


def test_a_distance_in_a_label_reads_as_english(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    close = places.Candidate("ChIJ9", "Rival Dental 9, DDS", "9 Main St", 37.4419 + 0.0058, -122.143)
    rf.fake_places(monkeypatch, [close, *rf.nearby(2, own=False)])
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert [g.sources[f"place:{k}"]["label"] for k in (1, 2, 3)] == [
        "A business nearby on Google Maps, 0.4 miles away",
        "A business nearby on Google Maps, 1 mile away",
        "A business nearby on Google Maps, 2.0 miles away",
    ]


def test_the_material_names_a_trade_in_words_where_the_model_reads_it(conn, monkeypatch):
    rf.seed(conn, knowledge={**home_services_knowledge("Plumbing"), "listing": rf.LISTING})
    rf.no_guides(monkeypatch)
    g = _gather(conn)
    trades = {row["id"]: row["trade"] for row in g.material["benchmark"]}
    assert trades["benchmark:home_services_plumbing:lsa"] == "plumbing"
    assert trades["benchmark:home_services:meta"] == "home services"
    assert not any("_" in trade for trade in trades.values())
    assert json.loads(g.sources["benchmark:home_services_plumbing:lsa"]["text"])["trade"] == "plumbing"
    # a business that says nothing of what it does is read as its category in words, not by the category's id
    rf.seed(conn, "biz_2", knowledge={**home_services_knowledge("Plumbing"), "category_detail": None})
    assert rm.gather(conn, config.load(), "biz_2", {}).material["trade"] == "home services"


def test_a_benchmark_source_names_its_trade_in_words_never_by_its_id(conn, monkeypatch):
    rf.seed(conn, knowledge={**home_services_knowledge("Plumbing"), "listing": rf.LISTING})
    rf.no_guides(monkeypatch)
    labels = {i: s["label"] for i, s in _gather(conn).sources.items() if s["kind"] == "benchmark"}
    assert labels["benchmark:home_services_plumbing:lsa"] == (
        "Published cost-per-lead averages for plumbing on Google Local Services (2025)"
    )
    assert labels["benchmark:home_services:meta"] == (
        "Published cost-per-lead averages for home services on Facebook & Instagram (2026)"
    )
    assert labels["benchmark:home_services_locksmith_handyman:lsa"] == (
        "Published cost-per-lead averages for locksmith and handyman work on Google Local Services (2025)"
    )
    assert "benchmark:home_services_hvac:lsa" in labels and all("_" not in label for label in labels.values())


def test_every_category_the_benchmark_keeps_rows_for_has_its_words():
    assert {r["category"] for r in benchmark.SEED} <= set(rm.CATEGORY_WORDS)
    assert not any("_" in words for words in rm.CATEGORY_WORDS.values())
    assert rm.trade_words("dentists") == "dentists" and rm.trade_words("home_services_plumbing") == "plumbing"
    assert rm.trade_words("a_new_trade") == "a new trade"  # an id the table lacks still reads as words


def test_a_benchmark_without_an_address_to_open_has_no_link(conn, monkeypatch):
    """Bing's row is seeded equal to Google's and its `source_url` says so in words (benchmark.BING_SOURCE): a note, not
    a link an owner or an assistant could open."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    sources = _gather(conn).sources
    assert sources["benchmark:dentists:microsoft_search"]["url"] is None
    assert sources["benchmark:dentists:google_search"]["url"] == benchmark.LOCALIQ_SEARCH


def test_the_guides_are_named_in_words_in_what_is_kept(conn, monkeypatch, knowledge_on):
    rf.seed(conn)
    labels = {s["id"]: s["label"] for s in rm.digest(_gather(conn))["sources"] if s["kind"] == "knowledge"}
    assert labels == {
        "knowledge:dentists/compliance": "Admiry's guide to dentists: compliance",
        "knowledge:dentists/google-search": "Admiry's guide to dentists: Google Search",
        "knowledge:dentists/offers": "Admiry's guide to dentists: offers",
    }


def test_every_label_an_owner_may_read_passes_the_owner_word_checks_and_holds_no_id(conn, monkeypatch, knowledge_on):
    rf.seed(conn)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    assert {s["kind"] for s in g.sources.values()} == {"business", "benchmark", "place", "page", "knowledge"}
    for source in g.sources.values():
        assert jargon(source["label"]) is None and not re.search(r"[_/]", source["label"])


def test_the_digest_keeps_ids_kinds_labels_and_links_never_a_nearby_name(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    d = rm.digest(_gather(conn))
    assert len(d["sha256"]) == 64 and d["counts"]["found"] == 6 and d["counts"]["pages"] == 5
    place = next(s for s in d["sources"] if s["id"] == "place:1")
    assert place["url"].startswith(MAPS) and "ChIJ1" in place["url"]
    assert "Rival Dental" not in str(d)


def test_the_digest_holds_no_page_title_and_no_website_of_a_business_nearby(conn, monkeypatch):
    """Places' website is read to fetch the page and goes no further: a page source links to its place on Google Maps,
    and none is labelled with its title or its host."""
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    d = rm.digest(g)
    stored = json.dumps(d)
    assert ".example" not in stored  # no rival's website, and none of the business's own
    assert not any(page["title"] in stored for page in g.pages)
    by_id = {s["id"]: s for s in d["sources"]}
    assert by_id["page:3"] == {
        "id": "page:3",
        "kind": "page",
        "label": "The website of a business nearby, 3.0 miles away",
        "url": places.maps_url(None, "ChIJ3"),
    }
    allowed = {r["source_url"] for r in benchmark.SEED}
    assert all(s["url"] is None or s["url"].startswith(MAPS) or s["url"] in allowed for s in d["sources"])


def test_the_digest_counts_what_was_read_before_and_after_fetch_more(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch, rf.nearby(12))
    rf.fake_pages(monkeypatch)
    g = _gather(conn)
    first = rm.digest(g)
    assert (first["places"], first["fetched_more"], first["vertical"]) == ("called", False, "dentists")
    assert first["counts"] == {"shown": 5, "found": 12, "pages": 5, "tried": 5, "benchmark": 3, "knowledge": 0}
    more = rm.digest(rm.more(g))
    assert more["fetched_more"] is True
    assert more["counts"] == {"shown": 12, "found": 12, "pages": 10, "tried": 10, "benchmark": 3, "knowledge": 0}
    assert more["sha256"] != first["sha256"]


def test_the_digests_hash_follows_what_the_model_read(conn, monkeypatch):
    rf.seed(conn)
    rf.no_guides(monkeypatch)
    rf.fake_places(monkeypatch)
    rf.fake_pages(monkeypatch)
    same = [rm.digest(_gather(conn))["sha256"] for _ in range(2)]
    rf.fake_pages(monkeypatch, {"https://rival1.example": "Another page, other words."})
    assert same[0] == same[1] != rm.digest(_gather(conn))["sha256"]
