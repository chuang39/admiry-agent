"""agent/page_builder.py (A3p-2 Task 5; spec 2026-09-28-admiry-pages-design.md §7, plan H12, rulings H31 and H35):
the four templates render a page that passes the sanitiser and the 14-rule check as served at its hosted address,
with the vertical's required lines verbatim and the form posting to Admiry; the linter refuses what the business's
facts do not back; `build` asks the model once, once more with the notes, then fails with them. The model is
agent/tests/fixtures.py's FakeGenerator (the `fake` fixture); the kits are the dental walk's real one
(test_landing.py's `_plan`), switched to each tree by the plan's own consultation category."""

from __future__ import annotations

from html.parser import HTMLParser

import httpx
import pytest
from fastmcp.exceptions import ToolError

from agent import business, config, hosted, landing, llm, page_builder
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.fixtures import FakeGenerator, page_copy_fixture
from agent.tests.test_landing import _plan
from agent.tests.test_tools_consult import fake  # noqa: F401 (fixture)

AGENT = "http://testserver"  # conftest's AGENT_PUBLIC_BASE_URL
HOST = "bright-smile-dental.business.test"
URL = f"https://{HOST}/"
PUBLIC_IP = ["93.184.216.34"]  # RFC 5737 documentation range: never a real, reachable address


def _kit(conn, biz: str, tree: str) -> dict:
    """The business's real kit with its plan's consultation switched to `tree` (the category is what picks it)."""
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,consultation,business,category}', to_jsonb(%s::text)) "
        "WHERE business_id = %s",
        (tree, biz),
    )
    return landing.kit(conn, biz)


def _render(kit: dict, tree: str, copy=None, **kw) -> str:
    args = {"page_id": kit["page_id"], "agent_base": AGENT, "photos": [], "hostname": HOST, **kw}
    return page_builder.render(tree, kit, copy or page_copy_fixture(), **args)


def _served(url: str, html_: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            return httpx.Response(200, text=html_, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


class _Forms(HTMLParser):
    """Every <form> on a page: its method, its action, and the names of its fields in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"method": a.get("method"), "action": a.get("action"), "names": []})
        elif tag in ("input", "select", "textarea") and self.forms and a.get("name"):
            self.forms[-1]["names"].append(a["name"])


def _forms(html_: str) -> list[dict]:
    p = _Forms()
    p.feed(html_)
    return p.forms


# ---------------------------------------------------------------- render: the sanitiser and the check


@pytest.mark.parametrize("tree", page_builder.TREES)
def test_every_tree_renders_a_page_the_sanitiser_and_the_check_pass(client, conn, fake, tree):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = _kit(conn, biz, tree)
    html_ = _render(kit, tree)

    assert hosted.sanitise(html_, page_id=kit["page_id"], own_host=HOST, agent_base=AGENT) == []
    page = landing.get(conn, kit["page_id"])
    report = landing.check(URL, page, kit, conn=conn, transport=_served(URL, html_), resolver=lambda h: PUBLIC_IP)
    by = {r["id"]: r for r in report["rules"]}
    # the blocking six all pass -- a sensitive vertical's compliance rule warns about its form by design (R3/R7)
    assert all(by[rule]["ok"] is not False for rule in landing.BLOCKING), [by[r] for r in landing.BLOCKING]
    assert all(by[rule]["ok"] is True for rule in ("reachable", "phone", "message_match", "tracking", "privacy"))
    assert by["compliance"]["ok"] is (None if kit["rules"]["sensitive"] else True), by["compliance"]
    assert sum(r["ok"] is True for r in report["rules"]) >= 10, [r for r in report["rules"] if r["ok"] is not True]
    assert report["ok"] is True


def test_the_required_lines_appear_verbatim(client, conn, fake):  # noqa: F811
    """Every line a business's recipe words, printed as worded: the dentists' two. The attorneys' recipe carries none
    since the K3 review of the knowledge files (2026-10-15): New York's "Attorney Advertising" and prior-results line
    went with its rules on 2026-06-01 (agent/knowledge/attorneys/compliance.md, Numbers), so an attorneys page prints
    neither."""
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = _kit(conn, biz, "dentists")
    text = hosted.visible_text(_render(kit, "dentists"))
    lines = [line["text"] for line in kit["rules"]["required_lines"]]
    assert lines == ["General Dentist", "The fee shown is the minimum fee only"] and all(line in text for line in lines)
    kit = _kit(conn, biz, "attorneys")
    text = hosted.visible_text(_render(kit, "attorneys"))
    assert kit["rules"]["required_lines"] == []
    assert "Attorney Advertising" not in text and "Prior results" not in text


def test_the_law_follows_the_business_not_the_layout(client, conn, fake):  # noqa: F811
    """A dental practice on the attorneys' layout prints the dentists' lines: the lines are the business's own
    vertical's, whichever template was chosen."""
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)  # dentists
    text = hosted.visible_text(_render(kit, "attorneys"))
    assert "General Dentist" in text and "The fee shown is the minimum fee only" in text


def test_a_licence_label_is_printed_only_with_the_businesss_own_number(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = _kit(conn, biz, "home_services")
    text = hosted.visible_text(_render(kit, "home_services"))
    assert "License #" not in text  # a bare label would be wrong on the page
    assert page_builder.required_lines(kit)[1][0]["text"] == "License #"

    latest = business.latest_knowledge(conn, biz)[1]
    business.add_knowledge(conn, biz, {**latest, "usp": [*latest["usp"], "License #TACLA00000C"]}, source="owner")
    kit = landing.kit(conn, biz)
    text = hosted.visible_text(_render(kit, "home_services"))
    assert "License #TACLA00000C" in text
    assert page_builder.required_lines(kit)[1] == []


@pytest.mark.parametrize("tree", page_builder.TREES)
def test_the_form_asks_the_verticals_fields_and_posts_to_admiry(client, conn, fake, tree):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = _kit(conn, biz, tree)
    [form] = _forms(_render(kit, tree))
    assert form["method"] == "post"
    assert form["action"] == kit["tracking"]["form_action"] == f"{AGENT}/t/form/{kit['page_id']}"
    assert [n for n in form["names"] if n != "website2"] == kit["rules"]["form_fields"]
    assert form["names"][-1] == "website2"  # the honeypot track.form drops


def test_the_page_is_one_mobile_first_file_with_one_goal(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)  # dentists
    html_ = _render(kit, "dentists")
    low = html_.lower()
    assert html_.startswith("<!doctype html>") and '<html lang="en">' in html_
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html_
    assert "<title>Invisalign in Palo Alto | Bright Smile Dental</title>" in html_
    assert f'<link rel="canonical" href="{URL}">' in html_
    # inline CSS only, system fonts, nothing loaded from elsewhere, no menu, no script but the snippet
    assert low.count("<style>") == 1 and "system-ui" in html_ and '<link rel="stylesheet"' not in low
    assert "@import" not in low and "url(" not in low and "<img" not in low and "<iframe" not in low
    assert "<nav" not in low
    assert low.count("<script") == 1 and kit["tracking"]["snippet"] in html_
    # the call button, and the call bar that stays in reach on a phone
    # hero, hours, the footer (M1 Task 4) and the bar
    assert html_.count('href="tel:+16505550100" data-admiry="call"') == 4
    assert 'href="/privacy"' in html_
    assert "Mon-Fri 8-5" in html_ and "https://maps.google.com/?q=500+University+Ave%2C+Palo+Alto%2C+CA" in html_


def test_the_booking_link_sits_beside_call_when_the_address_is_a_booking_page(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    assert 'data-admiry="book"' not in _render(kit, "dentists")  # brightsmile.example is no booking page
    kit["facts"]["website"] = "https://calendly.com/bright-smile"
    html_ = _render(kit, "dentists")
    assert '<a class="button second" href="https://calendly.com/bright-smile" data-admiry="book">' in html_
    assert 'data-admiry="book"' not in _render(kit, "fallback")  # a call-only vertical offers no booking


def test_the_models_words_are_escaped(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    copy = page_copy_fixture(
        headline="Invisalign in Palo Alto <script>location='https://evil.example/'</script>",
        trust_points=['<img src=x onerror="alert(1)">', '"><a href="javascript:alert(1)">x</a>'],
    )
    html_ = _render(kit, "dentists", copy)
    assert "<script>location" not in html_ and "&lt;script&gt;location" in html_
    assert "<img src=x" not in html_ and 'href="javascript:' not in html_
    assert hosted.sanitise(html_, page_id=kit["page_id"], own_host=HOST, agent_base=AGENT) == []


def test_no_phone_means_no_call_button_and_the_form_is_the_goal(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    kit["facts"]["phone"] = None
    html_ = _render(kit, "dentists")
    assert "tel:" not in html_ and '<a class="button call" href="#request">Request a call</a>' in html_
    assert hosted.sanitise(html_, page_id=kit["page_id"], own_host=HOST, agent_base=AGENT) == []


def test_the_owners_hours_line_replaces_the_kits(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    html_ = _render(kit, "dentists", hours="Mon-Sat 8-5")
    assert "Mon-Sat 8-5" in html_ and "Mon-Fri 8-5" not in html_


def test_a_wrong_template_is_refused_naming_the_four(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    assert page_builder.tree_for(None, kit) == "dentists" and page_builder.tree_for(" Attorneys ", kit) == "attorneys"
    with pytest.raises(ToolError) as e:
        page_builder.tree_for("florists", kit)
    assert all(tree in str(e.value) for tree in page_builder.TREES)
    assert page_builder.tree_for(None, {**kit, "tree": "physicians"}) == "fallback"  # no template of its own


# ---------------------------------------------------------------- lint (the spec's P9)

KIT = {
    "facts": {
        "name": "Bright Smile Dental",
        "category": "family dentist",
        "service": "Invisalign",
        "services": ["Invisalign", "Cleanings"],
        "location": "500 University Ave, Palo Alto, CA",
        "city": "Palo Alto",
        "area": "within 5 miles",
        "phone": "650-555-0100",
        "hours": "Mon-Fri 8-5",
        "offer": None,
        "usp": ["Same-week appointments"],
        "pricing_hints": ["cleanings $120"],
        "reviews_summary": None,
        "website": "https://brightsmile.example",
    },
    "message": {"headline_pattern": "Invisalign in Palo Alto", "offer": None},
    "rules": {"banned_words": landing.load_recipe("dentists")["banned"]},
}
RECIPE = landing.load_recipe("dentists")


def _kit_with(**facts) -> dict:
    return {**KIT, "facts": {**KIT["facts"], **facts}}


def _notes(kit=KIT, **changes) -> list[str]:
    return page_builder.lint(page_copy_fixture(**changes), kit, RECIPE)


def test_lint_passes_copy_that_only_states_the_facts():
    assert _notes() == []  # "Cleanings $120" is the business's own pricing hint


@pytest.mark.parametrize(
    "trust, kind",
    [
        ("20 years in Palo Alto", "a number of years"),
        ("Twenty years of smiles", "a number of years"),
        ("Serving Palo Alto since 1998", "a number of years"),
        ("A 5-year warranty on every aligner", "a number of years"),
        ("First visit only $99", "a price"),
        ("20% off your first cleaning", "a price"),
        ("Rated 4.9 by our patients", "a rating"),
        ("4.9 stars on Google", "a rating"),
        ("A 5-star practice", "a rating"),
        ("Over 300 reviews", "a rating"),
        ("License #TX12345", "a licence number"),
    ],
)
def test_lint_refuses_a_claim_the_facts_do_not_back(trust, kind):
    notes = _notes(trust_points=[trust])
    assert len(notes) == 1 and notes[0].startswith(f"Reason 1 to choose the business names {kind} (“"), notes
    assert notes[0].endswith("the business's facts do not have.")


@pytest.mark.parametrize(
    "trust, facts",
    [
        ("20 years in Palo Alto", {"usp": ["20 years in Palo Alto"]}),
        ("Twenty years of smiles", {"usp": ["Serving Palo Alto for 20 years"]}),
        ("Cleanings from $120", {}),
        ("A $99 first exam", {"offer": "$99 first exam"}),
        ("Rated 4.9 from 120 reviews", {"reviews_summary": "4.9 average from 120 Google reviews"}),
        ("License #TX12345", {"usp": ["Licensed, License #TX12345"]}),
    ],
)
def test_lint_allows_the_same_claim_when_the_facts_make_it(trust, facts):
    assert _notes(_kit_with(**facts), trust_points=[trust]) == []


def test_lint_refuses_banned_words_foreign_phones_e_mail_and_capitals():
    assert _notes(subheadline="The best family dentist in Palo Alto.") == [
        "The line under the headline uses “best”, a word Admiry leaves off pages for this kind of business."
    ]
    assert _notes(offer_line="Painless visits.") == [
        "The offer line uses “painless”, a word Admiry leaves off pages for this kind of business."
    ]
    assert _notes(service_paragraphs=["Call (408) 555-0000 to book."]) == [
        "Service paragraph 1 includes a phone number that is not the business's own."
    ]
    assert _notes(service_paragraphs=["Call (650) 555-0100 to book."]) == []  # the business's own
    assert _notes(service_paragraphs=["Write to hello@brightsmile.example."]) == [
        "Service paragraph 1 includes an e-mail address."
    ]
    assert _notes(cta_label="CALL TODAY") == ["The call button has a word written in capitals."]


def test_a_headline_names_one_of_the_services_the_owner_picked():
    """Founder's dev walk, 2026-10-07: an owner who picked six services got a kit whose service was all six joined
    (86 characters), and the headline had to hold that whole phrase within 60 -- no copy could pass, so "Build a page"
    always failed. The headline names one of the services picked (the first leads the pattern) and the city."""
    six = ["AC services", "Emergency repairs", "Installations", "Maintenance plans", "Remodels", "Roofing"]
    kit = {
        **_kit_with(service="AC services, Emergency repairs, Installations, Maintenance plans, Remodels and Roofing"),
        "message": {"headline_pattern": "AC services in Palo Alto", "offer": None},
    }
    kit["facts"]["lead_services"] = six
    assert _notes(kit, headline="AC services in Palo Alto") == []
    assert _notes(kit, headline="Emergency repairs in Palo Alto") == []
    assert _notes(kit, headline="Fast, friendly help in Palo Alto") == [
        "The headline must name the service and the city the way the ads do (“AC services in Palo Alto”)."
    ]
    assert _notes(kit, headline="AC services near you") == [
        "The headline must name the service and the city the way the ads do (“AC services in Palo Alto”)."
    ]


def test_lint_holds_the_limits_and_the_headline_to_the_ads_words():
    assert _notes(headline="Invisalign in Palo Alto " + "x" * 40) == ["The headline is over 60 characters."]
    assert _notes(trust_points=["Same-week appointments"] * 5) == [
        "There are more than 4 reasons to choose the business."
    ]
    assert _notes(service_paragraphs=[]) == ["The page needs at least one paragraph about the service."]
    assert _notes(headline="A straighter smile") == [
        "The headline must name the service and the city the way the ads do (“Invisalign in Palo Alto”)."
    ]
    # with no city and no offer to name, the service alone is what the check can ask for
    assert _notes(_kit_with(city=None), headline="Invisalign for adults") == []


# ---------------------------------------------------------------- build: once, once more with the notes, then fail


def test_build_asks_the_model_once_and_renders_its_words(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    copy, html_ = page_builder.build(conn, config.load(), biz, template=None, brief="warmer")
    assert copy == page_copy_fixture() and "<h1>Invisalign in Palo Alto</h1>" in html_
    [call] = fake.page_calls
    assert call["method"] == "write_page" and call["brief"] == "warmer"
    assert call["kit"]["facts"]["name"] == "Bright Smile Dental" and "snippet" not in str(call["kit"])
    assert [s["id"] for s in call["recipe"]["sections"]] == ["hero", "trust", "service", "faq"]
    assert "painless" in call["recipe"]["banned"]


def test_build_asks_again_with_the_notes_and_takes_the_fixed_words(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    fake.copies = [page_copy_fixture(trust_points=["20 years in Palo Alto"]), page_copy_fixture()]
    copy, _html = page_builder.build(conn, config.load(), biz, template=None, brief="warmer")
    assert copy == page_copy_fixture()
    first, second = fake.page_calls
    assert first["brief"] == "warmer"
    assert second["brief"].startswith(
        "warmer\n\nYour last answer broke these rules; fix them and keep everything else:"
    )
    assert "(“20 years”)" in second["brief"]


def test_build_fails_with_the_notes_when_the_second_answer_breaks_the_rules_too(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    fake.copies = [page_copy_fixture(trust_points=["20 years in Palo Alto"], offer_line="Only $99 this week.")]
    with pytest.raises(ToolError) as e:
        page_builder.build(conn, config.load(), biz, template=None, brief=None)
    message = str(e.value)
    assert message.startswith("The page's copy did not pass Admiry's rules: ")
    assert "(“20 years”)" in message and "(“$99”)" in message
    assert len(fake.page_calls) == 2 and fake.page_calls[1]["brief"].startswith("Your last answer broke these rules")
    assert not (JARGON.search(message) or ACCOUNT_JARGON.search(message) or MONEY_JARGON.search(message))


def test_build_turns_the_models_own_failure_into_one_sentence(client, conn, fake, monkeypatch):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    monkeypatch.setattr(llm, "generator", lambda settings=None: FakeGenerator(error="the assistant is busy"))
    with pytest.raises(ToolError, match="the assistant is busy"):
        page_builder.build(conn, config.load(), biz, template=None, brief=None)


def test_build_needs_the_plan(conn, fake):  # noqa: F811
    conn.execute("INSERT INTO business (id, claim_state) VALUES ('biz_x', 'claimed')")
    with pytest.raises(ToolError, match="Build the plan first"):
        page_builder.build(conn, config.load(), "biz_x", template=None, brief=None)
    assert fake.page_calls == []


def test_revise_asks_again_with_the_notes_after_the_instruction(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    sections = {"tree": "dentists", "copy": page_copy_fixture().model_dump(), "photos": [], "version": 1, "hours": None}
    fake.copies = [page_copy_fixture(offer_line="Only $99 this week."), page_copy_fixture(offer_line="")]
    copy, html_ = page_builder.revise(config.load(), kit, sections, instruction="lead with an offer", hostname=None)
    assert copy.offer_line == "" and '<p class="offer">' not in html_
    first, second = fake.page_calls
    assert first["method"] == second["method"] == "revise_page" and first["copy"] == page_copy_fixture()
    assert first["instruction"] == "lead with an offer"
    assert second["instruction"].startswith("lead with an offer\n\nYour last answer broke these rules")
    assert "(“$99”)" in second["instruction"]


def test_save_counts_the_drafts_and_never_loses_a_change_made_meanwhile(client, conn, fake):  # noqa: F811
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    args = {
        "tree": "dentists",
        "copy": page_copy_fixture(),
        "photos": [],
        "hours": None,
        "html": "<p>x</p>",
        "title": "T",
    }
    token, first = page_builder.save(conn, kit["page_id"], **args)
    assert first["version"] == 1 and hosted.preview(conn, token)["sections"] == first
    _token, second = page_builder.save(conn, kit["page_id"], **args, expect_version=1)
    assert second["version"] == 2
    with pytest.raises(ToolError, match="The page changed while Admiry was rewriting it"):
        page_builder.save(conn, kit["page_id"], **args, expect_version=1)  # a revise that started from draft 1
    assert page_builder.current(conn, biz)[1] == second
