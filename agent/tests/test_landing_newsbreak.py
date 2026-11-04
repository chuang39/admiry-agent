"""The landing check's `terms` and `contact` rules and the page kit's footer lines (M1 Task 4; plan P8, spec
2026-09-29-admiry-channels-design D89): NewsBreak asks of the page its ads point at a link to terms of use and the
advertiser's contact information. Two rules after `privacy`, warnings for Google (the stored `ok` is the BLOCKING
rules' verdict and never moves) and blocking for a channel whose `Capabilities.page_rules` name them, through
`landing.passed` at precheck; the kit says so when a funded channel asks (`rules.required_lines` gains the two lines);
and Admiry's own hosted pages (A3p-2) carry both, so every trade template passes. The hosted `/terms` page itself is
tested beside `/privacy` in test_hosted_serving.py."""

from __future__ import annotations

import json
import time

import pytest
from fastmcp.exceptions import ToolError

from agent import ads_run, config, hosted, landing, page_builder, proposals
from agent.channels import registry
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.test_ads_run import BIZ, PAGE_URL, sections, seed
from agent.tests.test_ads_run_channels import (  # noqa: F401 - nb_live is a fixture
    add_plan_by_hand,
    funds,
    nb_live,
    newsbreak_only,
)
from agent.tests.test_landing import GOOD, _plan, _serve, fake  # noqa: F401 - fake is a fixture
from agent.tests.test_page_builder import AGENT, HOST, PUBLIC_IP, URL, _kit, _render, _served

TERMS_FIX = "Add a Terms link in the footer."
CONTACT_FIX = "Put the business's phone or e-mail in the footer."
# M1 Task 7 (A3): an owner's sentence names no tool; on a page Admiry wrote, the footer is Admiry's to fill
NO_PHONE_FIX = "Put the business's e-mail in the footer, or tell Admiry the business phone so it can look for it there."
NO_PHONE_FIX_HOSTED = (
    "Tell Admiry your business phone; once your page is updated and published again, its footer shows it."
)
TERMS_LINK = '<a href="/terms">Terms</a> · '
PHONE_END = " · (650) 555-0100</footer>"
FOOTER = '<footer><a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> · (650) 555-0100</footer>'
PAGE = "https://brightsmile.example/invisalign"


def _by_id(report: dict) -> dict[str, dict]:
    return {r["id"]: r for r in report["rules"]}


def _without_phone(html: str) -> str:
    """`html` with the phone gone from its footer (the call link at the top stays)."""
    return html.replace(PHONE_END, "</footer>")


def _long(html: str, chars: int = 3_200) -> str:
    """`html` with `chars` characters of ordinary words between its body and its footer: the call link at the top is
    then further from the end of the page than the contact window reaches."""
    assert html.count("<footer>") == 1
    return html.replace("<footer>", "<p>" + "Plain words about the practice. " * (chars // 32 + 1) + "</p><footer>")


@pytest.fixture
def checked(client, conn, fake):  # noqa: F811 - the fixture shadows the imported name
    """The dental walk's real business and page, as a function: `checked(html)` serves `html` ({pid} is the page's
    token) at the page's address and returns the real check's report on it. `mutate(kit)` edits that call's kit."""
    _key, biz, _plan_id = _plan(client, conn, fake)
    page = landing.set_owner_url(conn, biz, PAGE)
    conn.execute("INSERT INTO page_event (page_id, kind) VALUES (%s, 'visit')", (page["id"],))

    def run(html: str = GOOD, *, mutate=None, **kw) -> dict:
        kit = landing.kit(conn, biz)
        if mutate:
            mutate(kit)
        served = _serve({PAGE: (200, html.replace("{pid}", page["id"]))})
        return landing.check(PAGE, page, kit, conn=conn, transport=served, resolver=lambda h: PUBLIC_IP, **kw)

    return run


# ---------------------------------------------------------------- the checklist and a good page


def test_the_checklist_gains_terms_and_contact_after_privacy():
    ids = [c["id"] for c in landing.CHECKLIST]
    assert len(ids) == 14 and ids[8:11] == ["privacy", "terms", "contact"]
    titles = {c["id"]: c["title"] for c in landing.CHECKLIST}
    assert titles["terms"] == "Terms link in the footer"
    assert titles["contact"] == "Phone or e-mail in the footer"
    assert "terms" not in landing.BLOCKING and "contact" not in landing.BLOCKING  # a warning for Google, never a gate
    # the Terms link the kit now asks for is no "link away" (fix round 1), as privacy already was; the rule's own
    # title is the owner's words now (spec 2026-10-12 §3.5), not a list of what it exempts
    assert titles["one_goal"] == "Keeps visitors on the page"


def test_good_is_the_footer_this_module_takes_apart():
    assert FOOTER in GOOD and TERMS_LINK in GOOD and PHONE_END in GOOD


def test_a_good_page_with_a_terms_link_and_the_phone_in_the_footer_passes_all_fourteen(checked):
    report = checked()
    assert [r["id"] for r in report["rules"]] == [c["id"] for c in landing.CHECKLIST] and len(report["rules"]) == 14
    by = _by_id(report)
    assert by["terms"]["ok"] is True and by["contact"]["ok"] is True
    assert by["terms"]["fix"] is None and by["contact"]["fix"] is None
    assert report["ok"] is True and not [r for r in report["rules"] if r["ok"] is False]
    assert landing.passed(report, ("terms", "contact")) is True


def test_the_new_rules_speak_in_the_owners_words(checked):
    by = _by_id(checked(_without_phone(_long(GOOD.replace(TERMS_LINK, "")))))
    for rule in ("terms", "contact"):
        said = " ".join([by[rule]["title"], by[rule]["detail"], by[rule]["fix"]])
        assert not (JARGON.search(said) or ACCOUNT_JARGON.search(said) or MONEY_JARGON.search(said)), rule


# ---------------------------------------------------------------- a page without them: Google passes, NewsBreak does not


def test_a_page_without_a_terms_link_passes_for_google_and_fails_for_newsbreak(checked):
    report = checked(GOOD.replace(TERMS_LINK, ""))
    by = _by_id(report)
    assert by["terms"]["ok"] is None and by["terms"]["fix"] == TERMS_FIX  # a warning, never an outright failure
    assert by["contact"]["ok"] is True
    assert report["ok"] is True  # the stored verdict stays the blocking rules'
    assert landing.passed(report) is True  # Google Search launches
    assert landing.passed(report, ("terms", "contact")) is False  # NewsBreak does not
    assert landing.passed(report, ("contact",)) is True and landing.passed(report, ("terms",)) is False


def test_a_page_without_contact_in_its_footer_passes_for_google_and_fails_for_newsbreak(checked):
    report = checked(_without_phone(_long(GOOD)))
    by = _by_id(report)
    assert by["terms"]["ok"] is True
    assert by["contact"]["ok"] is None and by["contact"]["fix"] == CONTACT_FIX
    assert report["ok"] is True and landing.passed(report) is True
    assert landing.passed(report, ("terms", "contact")) is False and landing.passed(report, ("terms",)) is True


def test_asking_for_the_rules_changes_neither_the_verdict_nor_a_rule(checked):
    html = GOOD.replace(TERMS_LINK, "")
    plain, asked = checked(html), checked(html, extra_rules=("terms", "contact"))
    assert asked["ok"] is True and asked["rules"] == plain["rules"]
    assert _by_id(asked)["terms"]["ok"] is None  # still a warning: `passed` is what judges a channel's own rules


# ---------------------------------------------------------------- the terms rule: an <a href> that says terms


@pytest.mark.parametrize(
    "link",
    [
        '<a href="/terms">Terms</a>',
        '<a href="/legal">Terms &amp; Conditions</a>',
        '<a href="/legal"><b>Terms</b> of use</a>',
        '<a href="/Terms-Of-Service">Legal</a>',
        '<a href="https://legal.example/policies/terms.html">Rules</a>',
        '<a href="/legal">TERMS OF USE</a>',
        '<a href="#terms">Terms</a>',  # a fragment naming a section of the page is a link to terms there
    ],
)
def test_a_link_whose_address_or_words_say_terms_is_a_terms_link(checked, link):
    assert _by_id(checked(GOOD.replace(TERMS_LINK, link + " · ")))["terms"]["ok"] is True


@pytest.mark.parametrize(
    "link",
    [
        "Terms of use",  # words, not a link
        '<a name="terms">Terms</a>',  # an anchor with no address
        '<a href="/legal">Legal</a>',  # nothing about terms in the address or the words
        '<a href="">Terms</a>',  # an empty address
        '<a href="#">Terms</a>',  # a placeholder: it goes nowhere (fix round 1)
        '<a href=" # ">Terms of use</a>',
        '<a href="javascript:void(0)">Terms</a>',  # a script call, not a page
        '<a href="JavaScript:openTerms()">Legal</a>',  # whatever the address says
        '<a href="java&#9;script:openTerms()">Terms</a>',  # a browser drops the tab inside the scheme
        '<a href="/legal">Legal</a> Terms of use',  # the link ended before the words
        "<script>var l = '<a href=\"/terms\">Terms</a>';</script>",  # inside a script: not a link on the page
        '<noscript><a href="/terms">Terms</a></noscript>',  # nor in markup a visitor's browser does not show
        '<template><a href="/terms">Terms</a></template>',
    ],
)
def test_words_without_a_link_to_terms_are_not_a_terms_link(checked, link):
    report = checked(GOOD.replace(TERMS_LINK, link + " · "))
    by = _by_id(report)
    assert by["terms"]["ok"] is None and by["terms"]["fix"] == TERMS_FIX
    assert report["ok"] is True


def test_an_unclosed_terms_link_still_counts(checked):
    assert _by_id(checked(GOOD.replace(TERMS_LINK, '<a href="/terms">Terms of use · ')))["terms"]["ok"] is True


def test_a_terms_link_to_another_site_is_not_a_link_away(checked):
    """One goal allows two links to other sites and exempts booking, the map and a privacy notice; the Terms link the
    kit now asks for is exempt the same way (fix round 1). A third link elsewhere still counts."""
    away = '<a href="https://facebook.com/brightsmile">f</a> <a href="https://instagram.com/brightsmile">i</a>'
    html = GOOD.replace("<footer>", away + "<footer>")
    elsewhere = '<a href="https://legal.example/policies/terms.html">Terms</a> · '
    kept = _by_id(checked(html.replace(TERMS_LINK, elsewhere)))["one_goal"]
    assert kept["ok"] is True and kept["detail"] == "2 link(s) to other sites."
    third = '<a href="https://news.example/story">News</a> · '
    tipped = _by_id(checked(html.replace(TERMS_LINK, third)))["one_goal"]
    assert tipped["ok"] is None and tipped["detail"] == "3 link(s) to other sites."
    assert tipped["fix"] == "Remove the menu and links away from the page; keep call, book, map, privacy and terms."


def test_the_link_tracking_is_linear_on_unclosed_tags(checked):
    """C2 again: the headline parser finishes MAX_HTML of unclosed <h1> in linear time; the link tracking must too --
    MAX_HTML of unclosed <a href> and one unclosed link carrying MAX_HTML of markup both finish in well under five
    seconds. (2026-10-12: MAX_HTML grew to 2 MB, so this now-larger worst case takes longer in absolute terms, but
    still linearly.)"""
    for body in ('<a href="x">' * (landing.MAX_HTML // 12), '<a href="x">' + "<b>y</b>" * (landing.MAX_HTML // 8)):
        started = time.monotonic()
        report = checked(body)
        assert time.monotonic() - started < 5.0
        assert _by_id(report)["terms"]["ok"] is None and _by_id(report)["contact"]["ok"] is None


# ---------------------------------------------------------------- the contact rule: the phone or an e-mail, late on the page


@pytest.mark.parametrize(
    "contact",
    [
        "(650) 555-0100",
        "650.555.0100",
        "+1 650 555 0100",
        "1-650-555-0100",
        "(650)&nbsp;555-0100",
        "<span>650</span>-<span>555</span>-<span>0100</span>",
        '<a href="tel:+16505550100">Call us</a>',  # the number is in the link, not in the words
        "write to hello@brightsmile.example",
        '<a href="mailto:hello@brightsmile.example">Write to us</a>',  # the address is in the link
        "Hello@BrightSmile.Example",
    ],
)
def test_the_businesss_phone_or_an_email_late_on_the_page_is_contact(checked, contact):
    report = checked(_long(GOOD.replace(PHONE_END, f" · {contact}</footer>")))
    assert _by_id(report)["contact"]["ok"] is True, _by_id(report)["contact"]


@pytest.mark.parametrize(
    "tail",
    [
        "",  # nothing
        " · (408) 555-0000",  # a phone, but not the business's
        " · Open Monday to Friday",  # words only
        ' · <img src="/logo@2x.png" alt="">',  # a file name that looks like an address
        ' · <a href="https://maps.google.com/?q=x">Find us</a>',
    ],
)
def test_other_things_late_on_the_page_are_not_contact(checked, tail):
    report = checked(_long(GOOD.replace(PHONE_END, f"{tail}</footer>")))
    by = _by_id(report)
    assert by["contact"]["ok"] is None and by["contact"]["fix"] == CONTACT_FIX
    assert "footer" in by["contact"]["detail"]
    assert report["ok"] is True and landing.passed(report, ("contact",)) is False


def test_the_contact_window_is_the_last_three_thousand_characters(checked):
    """On a short page the window is the whole page, so the call link at the top is in it; on a page with 3,000
    characters of words after the top, it is not "in the footer" any more."""
    short = _without_phone(GOOD)
    assert _by_id(checked(short))["contact"]["ok"] is True
    assert _by_id(checked(_long(short, 3_000)))["contact"]["ok"] is None


@pytest.mark.parametrize("words, ok", [(2_950, True), (3_050, None)])
def test_the_contact_window_reaches_back_about_three_thousand_characters(checked, words, ok):
    """The phone, then `words` characters of other words, then the end of the page: just inside the window, and just
    outside it (a bare page, so nothing else on it can be contact)."""
    html = f"<html><body><p>Call (650) 555-0100</p><p>{'w' * words}</p></body></html>"
    assert _by_id(checked(html))["contact"]["ok"] is ok


def test_scripts_and_icons_after_the_footer_do_not_push_the_contact_out_of_the_window(checked):
    """The window counts what a visitor reads: a footer followed by 20 KB of inline script and icons is still the
    end of the page."""
    bloat = "<script>" + "var a = 1; " * 2_000 + "</script>" + '<svg viewBox="0 0 1 1"><path d="M0 0"/></svg>' * 100
    assert _by_id(checked(GOOD.replace("<script async", bloat + "<script async")))["contact"]["ok"] is True


def test_a_phone_that_is_only_in_a_script_is_not_contact(checked):
    """JSON-LD says the number to a crawler, not to a visitor: it is not in the footer."""
    ld = '<script type="application/ld+json">{"telephone": "+16505550100"}</script>'
    html = _without_phone(_long(GOOD)).replace("<script async", ld + "<script async")
    assert _by_id(checked(html))["contact"]["ok"] is None


def test_a_business_without_a_known_phone_can_only_show_an_email(checked):
    def no_phone(kit):
        kit["facts"]["phone"] = None

    bare = _without_phone(_long(GOOD))
    assert _by_id(checked(bare, mutate=no_phone))["contact"]["fix"] == NO_PHONE_FIX
    mail_to = ' · <a href="mailto:hello@brightsmile.example">hello@brightsmile.example</a></footer>'
    assert _by_id(checked(bare.replace("</footer>", mail_to), mutate=no_phone))["contact"]["ok"] is True
    # a phone on the page cannot be told from anyone's when none is on file, so it is not enough on its own
    assert _by_id(checked(bare.replace("</footer>", PHONE_END), mutate=no_phone))["contact"]["ok"] is None


# ---------------------------------------------------------------- the approval: NewsBreak's launch needs both, Google's does not


def consulted() -> dict:
    """test_ads_run's plan sections with the rest of what a consultation stores, which the kit reads (`plans.cards`)."""
    secs = sections()
    secs["consultation"].update({"conversions": ["phone_call_60s"], "duration_days": 30})
    return secs


def _save_real_check(conn, html: str) -> dict:
    """The seeded business's page, checked for real against `html` (the dental page's words), the report saved on it."""
    kit = landing.kit(conn, BIZ)
    page = landing.current(conn, BIZ)
    assert page["url"] == PAGE_URL
    served = _serve({PAGE_URL: (200, html.replace("{pid}", page["id"]))})
    report = landing.check(PAGE_URL, page, kit, conn=conn, transport=served, resolver=lambda h: PUBLIC_IP)
    landing.save_check(conn, page["id"], report)
    return report


def _refused(conn, plan: dict) -> str:
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, plan)
    return str(e.value)


def test_a_newsbreak_launch_is_refused_until_the_page_has_a_terms_link_and_contact(conn):
    """M0's page precondition (`ads_run._page_ready` -> `landing.passed` with the channel's `page_rules`), on the
    stored report of a real check: the first rule not passed gives the refusal its sentence."""
    seed(conn, secs=consulted())
    funds(conn)
    plan = {"id": "pl_x", "sections": newsbreak_only()}
    report = _save_real_check(conn, GOOD.replace(TERMS_LINK, ""))
    assert report["ok"] is True and _by_id(report)["terms"]["ok"] is None
    assert _refused(conn, plan) == ads_run.PAGE_FIX.format(fix=TERMS_FIX)
    assert _refused(conn, plan) == (
        "Your page needs a fix before your ads can send people there: Add a Terms link in the footer."
    )

    _save_real_check(conn, _without_phone(_long(GOOD)))  # a Terms link, but no contact in the footer
    assert _refused(conn, plan) == ads_run.PAGE_FIX.format(fix=CONTACT_FIX)

    _save_real_check(conn, GOOD)  # both: NewsBreak may launch
    ads_run.precheck(conn, config.load(), BIZ, plan)


def test_the_approval_of_a_newsbreak_plan_is_refused_with_that_sentence_and_no_code_is_used(conn, nb_live):  # noqa: F811 - the fixture shadows the imported name
    """NewsBreak's page rules are NewsBreak's own (M1 Task 5b, R17): beside another channel that can launch, they park
    NewsBreak's row instead of refusing the approval (test_ads_run_newsbreak.py). A plan NewsBreak alone carries has
    no other channel, so nothing could launch: the approval is refused with the fix, as M0 refused it (the controller's
    ruling in 5b's fix round 1), and no code is used."""
    seed(conn, status="superseded", secs=consulted())
    _save_real_check(conn, GOOD.replace(TERMS_LINK, ""))
    plan_id = add_plan_by_hand(conn, newsbreak_only(), status="draft")
    funds(conn)
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    with pytest.raises(ToolError) as e, conn.transaction():
        proposals.confirm(conn, BIZ, prop["proposal_id"], code=None, web_owner=True)
    assert str(e.value) == ads_run.PAGE_FIX.format(fix=TERMS_FIX)
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert ads_run.get(conn, BIZ, "newsbreak") is None


def test_the_same_page_launches_google_search(conn):
    """Google Search names no page rules, and the report's own `ok` is the blocking six's."""
    seed(conn, secs=consulted())
    report = _save_real_check(conn, GOOD.replace(TERMS_LINK, ""))
    assert report["ok"] is True and registry.adapter("google_search").capabilities().page_rules == ()
    ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_g", "sections": sections()})


# ---------------------------------------------------------------- the page kit's footer
# (the lines `get_page_kit` adds for a funded channel are tested with the other get_page_kit tests, test_tools_pages.py)


@pytest.mark.parametrize("tree", page_builder.TREES)
def test_every_recipe_footer_asks_for_terms_privacy_and_the_phone_or_email(tree):
    footer = next(s for s in landing.load_recipe(tree)["sections"] if s["id"] == "footer")
    for part in ("Terms", "Privacy", "phone or e-mail"):
        assert part in footer["guide"], (tree, part)
    assert "Terms" in footer["example"] and "Privacy" in footer["example"] and "555" in footer["example"]
    assert not JARGON.search(footer["guide"] + footer["example"])


# ---------------------------------------------------------------- Admiry's hosted pages carry both


def _footer(html: str) -> str:
    return html[html.index("<footer>") : html.index("</footer>")]


@pytest.mark.parametrize("tree", page_builder.TREES)
def test_a_hosted_page_from_every_trade_template_passes_both_rules(client, conn, fake, tree):  # noqa: F811 - the fixture shadows the imported name
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = _kit(conn, biz, tree)
    html_ = _render(kit, tree)
    page = landing.get(conn, kit["page_id"])
    report = landing.check(URL, page, kit, conn=conn, transport=_served(URL, html_), resolver=lambda h: PUBLIC_IP)
    by = _by_id(report)
    assert by["terms"]["ok"] is True and by["contact"]["ok"] is True, (by["terms"], by["contact"])
    assert landing.passed(report, ("terms", "contact")) is True
    assert hosted.sanitise(html_, page_id=kit["page_id"], own_host=HOST, agent_base=AGENT) == []
    footer = _footer(html_)
    assert '<a href="/terms">Terms of use</a>' in footer and '<a href="/privacy">Privacy notice</a>' in footer
    assert footer.index("/terms") < footer.index("/privacy")  # beside it, Terms first
    assert '<a href="tel:+16505550100" data-admiry="call">650-555-0100</a>' in footer


@pytest.mark.parametrize("tree", page_builder.TREES)
def test_a_hosted_page_shows_the_business_phone_and_never_the_owners_sign_in_address(client, conn, fake, tree):  # noqa: F811 - the fixture shadows the imported name
    """Fix round 1 (privacy): the owner's sign-in address is the owner's to publish, not Admiry's to guess. The footer
    shows the business's phone; with none on file it shows no contact line at all -- and the check then says to add
    a business phone (which Google's call asset wants anyway)."""
    _key, biz, _plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    for phone, contact_ok in (("650-555-0100", True), (None, None)):
        kit = _kit(conn, biz, tree)
        kit["facts"]["phone"] = phone
        html_ = _render(kit, tree)
        footer = _footer(html_)
        assert "owner@example.com" not in html_ and "mailto:" not in html_
        assert ('href="tel:+16505550100"' in footer) is bool(phone) and ("tel:" in html_) is bool(phone)
        assert '<a href="/terms">Terms of use</a>' in footer and '<a href="/privacy">Privacy notice</a>' in footer
        page = landing.get(conn, kit["page_id"])
        report = landing.check(URL, page, kit, conn=conn, transport=_served(URL, html_), resolver=lambda h: PUBLIC_IP)
        assert _by_id(report)["terms"]["ok"] is True
        assert _by_id(report)["contact"]["ok"] is contact_ok
        assert _by_id(report)["contact"]["fix"] == (None if phone else NO_PHONE_FIX)
        assert hosted.sanitise(html_, page_id=kit["page_id"], own_host=HOST, agent_base=AGENT) == []


def test_a_page_admiry_wrote_without_a_phone_asks_for_the_phone_not_for_a_footer_nobody_can_edit(client, conn, fake):  # noqa: F811 - the fixture shadows the imported name
    """M1 Task 7 (A3): the footer of a page Admiry wrote is Admiry's template (it shows the business phone, never an
    address), so its no-phone fix asks for the phone and says how it reaches the page; the same words at the owner's
    own address, or an assistant's own page Admiry hosts, are theirs to edit -- the general fix. Neither names a
    tool."""
    from agent.mcp_app import REGISTRY

    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = _kit(conn, biz, "dentists")
    kit["facts"]["phone"] = None
    html_ = _render(kit, "dentists")
    conn.execute(
        "UPDATE page SET source = 'admiry', hostname = %s, url = %s, status = 'published' WHERE id = %s",
        (HOST, URL, kit["page_id"]),
    )

    def fix(url: str) -> str:
        page = landing.get(conn, kit["page_id"])
        served = _served(url, html_)
        return _by_id(landing.check(url, page, kit, conn=conn, transport=served, resolver=lambda h: PUBLIC_IP))[
            "contact"
        ]["fix"]

    assert fix(URL) == NO_PHONE_FIX_HOSTED
    assert fix("https://brightsmile.example/") == NO_PHONE_FIX  # the owner's own site
    conn.execute("UPDATE page SET source = 'hosted' WHERE id = %s", (kit["page_id"],))
    assert fix(URL) == NO_PHONE_FIX  # an assistant's own page, hosted
    for sentence in (NO_PHONE_FIX, NO_PHONE_FIX_HOSTED):
        assert not [t.name for t in REGISTRY if "_" in t.name and t.name in sentence], sentence
        assert not JARGON.search(sentence) and not ACCOUNT_JARGON.search(sentence)


def test_the_owners_sign_in_address_is_in_no_kit_the_page_builder_reads(client, conn, fake):  # noqa: F811 - the fixture shadows the imported name
    _key, biz, _plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    kit = landing.kit(conn, biz)
    assert "owner@example.com" not in json.dumps(kit)
    assert "owner@example.com" not in json.dumps(page_builder.model_kit(kit))
