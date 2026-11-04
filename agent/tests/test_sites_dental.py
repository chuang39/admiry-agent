"""The dental site in Calm, Warm and Boutique (A3p-6 batch 1 Task 4; plan 2026-10-06-agent-a3p-6-1-industry-site-templates
Q2, Q3, Q11, Q12, Q15, Q19, Q24, Q25): the sample dentist rendered with Task 3's templates -- every page through the
bundle checks, the service pages' H1s, the appointment form that never asks a health question, no claim the facts
cannot back, the dentist without a photo, the emergency line, new patients and insurance only with their facts, the
office strip (stock only in a preview), no smile gallery, thin facts, the structured data, each style's hero and
header, and the form's `patient` answer stored on the lead and printed in the lead e-mail."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from markupsafe import escape

from agent import hosted, models
from agent.sites import families, images, samples, spec
from agent.sites import words as site_words
from agent.sites.spec import Dentist, Facts, ImageRef, Service
from agent.tests.conftest import JARGON
from agent.tests.test_sites_render import (
    AGENT,
    CHOOSE,
    LABEL,
    PAGE,
    SNIPPET,
    Doc,
    _allowed,
    _weights,
    add_photo,
    assert_no_select_starts_on_an_answer,
    build,
    checked,
    html_pages,
    load_library,
    logo_png,
    make_page,
    text_of,
)
from agent.tests.test_track import _page, _resend

STYLES = ("calm", "warm", "boutique")
TEL = "tel:+14085550173"
PHONE = "(408) 555-0173"
FORM_FIELDS = ["name", "phone", "patient", "service", "preferred_time", "website2"]
SERVICES = [
    "Exams and cleanings",
    "Invisalign",
    "Dental implants",
    "Emergency visits",
    "Kids' dentistry",
    "Teeth whitening",
]
FAMILY = families.get("dental")
# (kind, how many, people): Q15's dental kinds, and one hero with people that must never be placed
DENTAL_LIBRARY = [
    ("hero", 2, False),
    ("hero", 1, True),
    ("reception", 2, False),
    ("exterior", 2, False),
    ("service-treatment", 4, False),
    ("aligners", 2, False),
]
BANNED = ("painless", "pain-free", "pain free", "specialist", "guaranteed")


@pytest.fixture
def library(conn, tmp_path):
    return load_library(conn, tmp_path, DENTAL_LIBRARY, "dental")


def dentist(style: str = "calm", **facts) -> spec.SiteSpec:
    s = samples.load("dentist", style=style)
    if facts:
        s = s.model_copy(update={"facts": s.facts.model_copy(update=facts)})
    return spec.validate(s)


# The thin practice's own words where the sample's state what it does not give: cleanings, implants, emergency
# visits, insurance, new patients, the towns around San Jose.
THIN_HOME = {
    "headline": "Invisalign in San Jose",
    "lead": "Clear aligners and fillings in San Jose, with each step explained.",
    "steps": [
        {"title": "Book online or call", "text": "Pick a time that suits you."},
        {"title": "A first exam", "text": "With time for your questions."},
        {"title": "A clear plan", "text": "Options and costs on paper before any treatment."},
    ],
    "faq": [
        {
            "q": "Is Invisalign right for me?",
            "a": "Many adults and teens can use aligners. An exam tells us whether they suit your teeth.",
        },
        {
            "q": "How long does Invisalign take?",
            "a": "It depends on your teeth. We give you an estimate at your first visit.",
        },
        {"q": "Do you do fillings?", "a": "Yes. Ask us about them when you call."},
        {"q": "How do I book a visit?", "a": "Send the form or call, and we will find a time."},
    ],
}
THIN_PAGES = {
    "about": {"community": "We are proud to care for families in San Jose."},
    "services": {"lead": "Clear aligners and fillings in San Jose."},
}


def thin(style: str) -> spec.SiteSpec:
    """The Review Focus's thin practice: a name, a phone, a city and two services (Invisalign, fillings) -- no
    dentist, no insurance, no new-patient text, no reviews, no emergency line, no hours, no address. Its words are the
    base's (Calm's) with its own wherever those state what it does not give (`THIN_HOME`, `THIN_PAGES`), linted
    against these facts; the test pins, ignoring case, no placeholder, no "accepting new patients", "in pain?",
    "most PPO plans", insurance or "Dr." on any page."""
    s = samples.load("dentist", style=style)
    facts = Facts(
        name="Clearwater Family Dental",
        phone=PHONE,
        city="San Jose",
        services=[
            Service(slug="invisalign", name="Invisalign", lead=True, summary="Clear aligners to straighten teeth."),
            Service(slug="fillings", name="Fillings", summary="Tooth-coloured fillings for cavities."),
        ],
    )
    base = samples.load("dentist", style="calm").words
    words = models.SiteWords.model_validate(
        {
            **base.model_dump(),
            "home": {**base.home.model_dump(), **THIN_HOME},
            "about": {**base.about.model_dump(), **THIN_PAGES["about"]},
            "services": {**base.services.model_dump(), **THIN_PAGES["services"]},
            "service_pages": [p.model_dump() for p in base.service_pages if p.slug == "invisalign"],
        }
    )
    out = spec.validate(s.model_copy(update={"facts": facts, "words": words}))
    assert site_words.lint(out.words, out, FAMILY) == []
    return out


def section(page: str, name: str) -> str:
    """The markup of the first element carrying data-section="name", to its closing </section>."""
    at = page.index(f'data-section="{name}"')
    return page[at : page.index("</section>", at)]


def options(markup: str, name: str) -> list[str]:
    select = markup[markup.index(f'name="{name}"') :]
    select = select[: select.index("</select>")]
    return [o.replace("&#39;", "'") for o in re.findall(r"<option[^>]*>([^<]*)</option>", select)]


# ---------------------------------------------------------------- the bundle and its checks


@pytest.mark.parametrize("style", STYLES)
def test_every_dental_page_passes_the_bundle_checks_and_is_tracked(library, style):
    s = dentist(style)
    rendered = build(library, s)

    assert checked(rendered) == []
    assert rendered.pages == [path for _kind, path, _slug in spec.pages(s)]
    for path, page in html_pages(rendered).items():
        assert page.count(SNIPPET) == 1, path
        for url in Doc(page).urls():
            assert _allowed(url, ""), (path, url)
        calls = [a for a in Doc(page).all("a") if a.get("href", "").startswith("tel:")]
        assert calls and {a["href"] for a in calls} == {TEL}, path
        assert all(a.get("data-admiry") == "call" for a in calls), path


@pytest.mark.parametrize("style", STYLES)
def test_the_h1s_name_the_service_and_the_city(library, style):
    pages = html_pages(build(library, dentist(style)))

    for path, page in pages.items():
        assert len(Doc(page).h1) == 1, path
    assert Doc(pages["services/invisalign/index.html"]).h1 == ["Invisalign in San Jose"]
    assert Doc(pages["services/dental-implants/index.html"]).h1 == ["Dental implants in San Jose"]
    assert Doc(pages["services/emergency-visits/index.html"]).h1 == ["Emergency visits in San Jose"]
    home = Doc(pages["index.html"]).h1[0].lower()
    assert "invisalign" in home and "san jose" in home


# ---------------------------------------------------------------- the appointment form


@pytest.mark.parametrize("style", STYLES)
def test_the_appointment_form_never_asks_a_health_question(library, style):
    """Q12: name, phone, new or existing, the reason from the practice's own list, the time of day -- no free text,
    no date of birth, no insurance number. Q3 puts the form on the home page too ("request an appointment")."""
    pages = html_pages(build(library, dentist(style)))
    with_form = {"index.html", "contact/index.html"} | {
        f"services/{slug}/index.html" for slug in ("invisalign", "dental-implants", "emergency-visits")
    }

    for path, page in pages.items():
        assert "<textarea" not in page, path
        doc = Doc(page)
        forms = doc.all("form")
        if path not in with_form:
            assert forms == [], path
            continue
        assert len(forms) == 1, path
        form = forms[0]
        assert form["action"] == f"{AGENT}/t/form/{PAGE}" and form["method"] == "post", path
        assert form["data-admiry-intent"] == "appointment", path
        fields = [a for t, a in doc.tags if t in ("input", "select", "textarea") and a.get("name")]
        assert [a["name"] for a in fields] == FORM_FIELDS, path
        labelled = {a.get("for") for a in doc.all("label")}
        for a in fields:
            assert a.get("id") in labelled, (path, a["name"])
            assert a.get("autocomplete"), (path, a["name"])
        assert options(page, "patient") == [CHOOSE, "New patient", "Existing patient"], path
        assert options(page, "service") == [CHOOSE, *SERVICES, "Something else"], path
        assert options(page, "preferred_time") == [CHOOSE, "Morning", "Afternoon", "Any time"], path
        assert "What is the visit for?" in text_of(page), path
        assert "Request an appointment" in page and "The practice calls you back during business hours." in page
        assert "What you send goes to Clearwater Family Dental only." in page
        assert "Request a quote" not in page and "ZIP code" not in page


@pytest.mark.parametrize("style", STYLES)
def test_no_select_starts_on_an_answer(library, style):
    """The ruling (A3p-6 batch 1 follow-ups, amended in fix round 1): new or existing patient used to open on "New
    patient", so a visitor who never touched it was recorded as new. Now the patient and the preferred time open on
    "Choose one" everywhere (the patient must be chosen, the time may stay unchosen: no preference); the service opens
    on "Choose one" on the home and contact pages, and on a service page on that page's own service (an informed
    default), the placeholder still first."""
    s = dentist(style)
    pages = html_pages(build(library, s))
    own = {f"services/{x.slug}/index.html": x.name for x in s.facts.services if x.lead}
    with_form = [path for path, page in pages.items() if "<form" in page]

    assert sorted(with_form) == sorted(["index.html", "contact/index.html", *own])
    for path in with_form:
        chosen = {"service": own[path]} if path in own else None
        assert_no_select_starts_on_an_answer(pages[path], {"patient", "service"}, {"preferred_time"}, chosen)


# ---------------------------------------------------------------- the words


@pytest.mark.parametrize("style", STYLES)
def test_no_page_promises_what_a_dentist_cannot(library, style):
    for path, page in html_pages(build(library, dentist(style))).items():
        text = text_of(page).lower()
        for word in BANNED:
            assert word not in text, (path, word)
        assert "smile gallery" not in text and "before and after" not in text, path  # Q24
        assert 'data-section="work"' not in page and 'data-slot="gallery' not in page, path


@pytest.mark.parametrize("style", STYLES)
def test_the_visible_text_has_no_jargon(library, style):
    for path, page in html_pages(build(library, dentist(style))).items():
        assert not JARGON.search(text_of(page)), (path, JARGON.search(text_of(page)))


def test_the_template_words_are_the_plans(library):
    pages = html_pages(build(library, dentist("calm")))
    home, contact = pages["index.html"], pages["contact/index.html"]

    for words in ("Book an appointment", f"Call {PHONE}", "Privacy notice", "Terms of use"):
        assert words in text_of(home), words
    bar = home[home.index('class="actionbar"') :]
    assert ">Book</a>" in bar and ">Call</a>" in bar and "Get a quote" not in home
    for words in ("Call or text", "Hours", "Find us", "Get directions", "100 Example Street, San Jose, CA"):
        assert words in text_of(contact), words
    assert "Where we work" not in text_of(contact)


@pytest.mark.parametrize("style", STYLES)
def test_the_sample_fills_the_section_headings(library, style):
    """Each style's own headings (fix round 1: Warm's and Boutique's artboards word some differently, and a heading
    a style's artboard leaves out is empty, so its section shows none)."""
    s = dentist(style)
    pages = html_pages(build(library, s))
    home, w = pages["index.html"], s.words.home

    for field, name in (
        ("reviews_heading", "reviews"),
        ("faq_heading", "faq"),
        ("new_patients_heading", "new-patients"),
        ("insurance_heading", "insurance"),
        ("office_heading", "office"),
        ("about_heading", "dentist"),
    ):
        heading = getattr(w, field)
        block = section(home, name)
        assert (f"<h2>{escape(heading)}</h2>" in block) if heading else ("<h2>" not in block), (style, field)
    service = pages["services/invisalign/index.html"]
    for heading in ("When to come in", "What the visit includes", "Questions"):
        assert f">{heading}</h2>" in service, heading


# ---------------------------------------------------------------- the home page's structure and modules


def test_the_dental_home_follows_one_structure(library):
    """Q3: header, hero, trust strip, emergency line, services, the dentist, new patients, reviews, insurance and
    payment, the office, questions, request an appointment, footer."""
    for style in STYLES:
        home = html_pages(build(library, dentist(style)))["index.html"]
        order = [x for x in Doc(home).sections() if x != "emergency-top"]
        assert order == [
            "hero",
            "trust",
            "emergency",
            "services",
            "dentist",
            "new-patients",
            "reviews",
            "insurance",
            "office",
            "faq",
            "form",
        ], style
        assert "Open Saturdays" in section(home, "trust")


@pytest.mark.parametrize("style", STYLES)
def test_the_dentist_is_shown_by_name_without_a_photo(library, style):
    home = html_pages(build(library, dentist(style)))["index.html"]
    block = section(home, "dentist")

    assert "Dr. [Your name], DDS" in block and f"<h2>{escape(dentist(style).words.home.about_heading)}</h2>" in block
    assert "<img" not in block and 'data-slot="dentist"' not in home
    other = html_pages(
        build(
            library,
            dentist(style, dentist=Dentist(name="Dr. Ana Ruiz", credentials="DMD", school="UCSF", years=12)),
        )
    )["index.html"]
    block = section(other, "dentist")
    for fact in ("Dr. Ana Ruiz", "DMD", "UCSF", "12 years"):
        assert fact in block, fact
    assert "<img" not in block


def test_the_emergency_line_only_with_one(library):
    for style in STYLES:
        home = html_pages(build(library, dentist(style)))["index.html"]
        assert "Call us for a toothache or a broken tooth." in section(home, "emergency"), style
        assert Doc(home).sections().index("emergency") == Doc(home).sections().index("trust") + 1, style
        services = html_pages(build(library, dentist(style)))["services/index.html"]
        assert 'data-section="emergency"' in services, style
        none = html_pages(build(library, dentist(style, emergency_line=None)))
        for path, page in none.items():
            assert 'data-section="emergency' not in page and "In pain?" not in page, (style, path)
    # Calm: on a phone the band sits above the hero -- "In pain? Call {phone}", a call link
    calm = html_pages(build(library, dentist("calm")))["index.html"]
    doc = Doc(calm)
    assert doc.sections()[:2] == ["emergency-top", "hero"]
    band = [a for a in doc.all("a") if doc.link_text[id(a)].strip() == f"In pain? Call {PHONE}"]
    assert len(band) == 1 and band[0]["href"] == TEL and band[0]["data-admiry"] == "call"
    for style in ("warm", "boutique"):
        assert "In pain?" not in html_pages(build(library, dentist(style)))["index.html"], style
    css = next(b.decode() for p, b in build(library, dentist("calm")).files.items() if p.endswith(".css"))
    assert ".emergency-top" in css and "@media (min-width:768px){.emergency-top{display:none}}" in css
    assert "@media (max-width:767px){.page-home .emergency{display:none}}" in css  # moved, not repeated


def test_insurance_only_with_plans_or_the_in_office_plan(library):
    def home(**facts) -> str:
        return html_pages(build(library, dentist("calm", **facts)))["index.html"]

    both = section(home(), "insurance")
    assert "Most PPO plans" in both and "An in-office plan for patients without insurance" in both
    plans = section(home(in_office_plan=None), "insurance")
    assert "Most PPO plans" in plans and "in-office plan" not in plans
    office = section(home(insurance_plans=[]), "insurance")
    assert "An in-office plan for patients without insurance" in office and "PPO" not in office
    assert 'data-section="insurance"' not in home(insurance_plans=[], in_office_plan=None)


def test_new_patients_only_with_the_text(library):
    home = html_pages(build(library, dentist("calm")))["index.html"]
    assert "New patients are welcome." in section(home, "new-patients")
    hero = section(home, "hero")
    assert '<p class="badge">Accepting new patients</p>' in hero  # Calm's badge, a fact the practice gave

    without = html_pages(build(library, dentist("calm", new_patients=None)))["index.html"]
    assert 'data-section="new-patients"' not in without and "Accepting new patients" not in without
    for style in ("warm", "boutique"):  # the badge is Calm's (Warm's own eyebrow says the words, as its artboard does)
        assert '<p class="badge">' not in html_pages(build(library, dentist(style)))["index.html"]


def _kind(conn, sha: str) -> str:
    return conn.execute(
        "SELECT a.kind FROM site_asset_file f JOIN site_asset a ON a.id = f.asset_id WHERE f.sha256 = %s", (sha,)
    ).fetchone()["kind"]


def test_the_office_strip_is_stock_only_in_a_preview(library):
    """Q15: office-1 from `reception`, office-2 from `exterior`, labelled in a preview. A live site never shows
    another building's photos as this practice's office, nor on its contact page (the ruling on "recent work")."""
    s = dentist("warm")
    resolved = images.fill(library, s, business_id="biz_render", page_id=PAGE)
    assert _kind(library, max(resolved["office-1"].variants.values())) == "reception"
    assert _kind(library, max(resolved["office-2"].variants.values())) == "exterior"
    assert _kind(library, max(resolved["contact"].variants.values())) == "exterior"
    assert _kind(library, max(resolved["service-invisalign"].variants.values())) == "aligners"
    assert _kind(library, max(resolved["service-kids-dentistry"].variants.values())) == "service-treatment"

    shown = html_pages(build(library, s, preview=True))
    figures = [f for f in Doc(section(shown["index.html"], "office") + "</section>").figures]
    assert [f["attrs"]["data-slot"] for f in figures] == ["office-1", "office-2"]
    assert all(f["attrs"]["data-source"] == "stock" and LABEL in f["text"] for f in figures)

    live = html_pages(build(library, s, preview=False))
    assert 'data-section="office"' not in live["index.html"]
    assert 'data-slot="contact"' not in live["contact/index.html"]
    assert 'data-slot="office' not in "".join(live.values()) and LABEL not in "".join(live.values())


# ---------------------------------------------------------------- facts decide the sections


@pytest.mark.parametrize("style", STYLES)
def test_a_practice_with_thin_facts_leaves_out_what_it_has_no_facts_for(library, style):
    s = thin(style)
    for preview in (True, False):
        rendered = build(library, s, preview=preview)
        assert checked(rendered) == [], preview
        pages = html_pages(rendered)
        home = pages["index.html"]
        assert len(text_of(home)) >= hosted.MIN_TEXT_CHARS
        sections = Doc(home).sections()
        for gone in ("dentist", "insurance", "new-patients", "reviews", "emergency", "emergency-top", "trust"):
            assert gone not in sections, (gone, preview)
        assert "services" in sections and "form" in sections
        for path, page in pages.items():
            text = text_of(page).lower()  # every guard ignores case
            assert "[" not in text, path  # no placeholder of any kind
            assert "accepting new patients" not in text and "in pain?" not in text, path
            assert "most ppo plans" not in page.lower() and "dr. " not in text and "insurance" not in text, path
            assert "<h2></h2>" not in page and "<h2>\n" not in page, path
            assert 'data-section="reviews"' not in page, path


# ---------------------------------------------------------------- structured data, styles, weight


def test_the_structured_data_is_a_dentist_with_its_street_address(library):
    pages = html_pages(build(library, dentist("boutique"), hostname="clearwater.admiry.ai"))
    for path, page in pages.items():
        blocks = [b for a, b in Doc(page).scripts if a.get("type") == "application/ld+json"]
        assert len(blocks) == 1, path
        ld = json.loads(blocks[0])
        assert ld["@type"] == "Dentist" and ld["name"] == "Clearwater Family Dental"
        assert ld["telephone"] == "+14085550173" and ld["url"] == "https://clearwater.admiry.ai/"
        assert ld["address"]["@type"] == "PostalAddress"
        assert ld["address"]["streetAddress"] == "100 Example Street, San Jose, CA"
        assert ld["address"]["addressLocality"] == "San Jose" and ld["address"]["addressRegion"] == "CA"
        assert "areaServed" not in ld


def test_each_style_has_its_hero_and_header(library):
    calm, warm, boutique = (html_pages(build(library, dentist(s)))["index.html"] for s in STYLES)

    assert 'class="hero hero--split-tinted"' in calm
    assert 'class="hero hero--centred"' in warm
    assert 'class="hero hero--dark-split"' in boutique
    # Warm: the photo full width under the words
    hero = section(warm, "hero")
    assert hero.index('class="hero-copy"') < hero.index('class="hero-media"')
    # Boutique: the two-line wordmark (the serif name, a spaced uppercase line) and a spaced uppercase eyebrow
    header = boutique[boutique.index('<header class="header">') : boutique.index("</header>")]
    mark = re.search(r'<a class="brand brand--wordmark" href="/">(.*?)</a>', header, re.DOTALL)
    assert mark, header[:400]
    assert '<span class="wordmark-name">Clearwater Family Dental</span>' in mark.group(1)
    assert re.search(r'<span class="wordmark-line">[^<]+</span>', mark.group(1))
    for other in (calm, warm):
        assert "brand--wordmark" not in other and '<a class="brand" href="/">Clearwater Family Dental</a>' in other
    sheet = next(
        b.decode() for p, b in build(library, dentist("boutique")).files.items() if p.endswith(".css")
    ).replace(" ", "")
    assert ".wordmark-line{" in sheet and "letter-spacing:.24em" in sheet and "color:var(--accent)" in sheet
    for style in ("calm", "warm"):
        css = next(b.decode() for p, b in build(library, dentist(style)).files.items() if p.endswith(".css"))
        assert ".wordmark-line" not in css, style


@pytest.mark.parametrize("style", STYLES)
def test_a_logo_is_the_headers_brand_where_the_header_is_light_boutiques_dark_one_keeps_its_wordmark(library, style):
    """A3p-6 batch 2 Task 2 (Q10, fix round 0): Calm and Warm show the owner's logo in the header and keep the name in
    the footer; Boutique's header is dark, so the logo shows nowhere there -- its two-line wordmark stays, whatever the
    owner uploaded."""
    page_id = make_page(library)
    png = add_photo(library, page_id, "ph_logo", logo_png(300, 100), kind="logo", content_type="image/png")
    s = dentist(style).model_copy(update={"images": {"logo": ImageRef(source="owner", id="ph_logo")}})
    path = images.path(hashlib.sha256(png).hexdigest(), ".png")
    shown = build(library, s, photos_of=page_id)
    plain = build(library, dentist(style))

    assert checked(shown) == []
    light = style != "boutique"
    assert (path in shown.files) == light  # the file is in the bundle only where a page shows it
    for name, page in html_pages(shown).items():
        header, footer = page[page.index("<header") : page.index("</header>")], page[page.index("<footer") :]
        assert "brand-logo" not in footer and 'class="brand">Clearwater Family Dental</p>' in footer, name
        assert LABEL not in header, name
        if light:
            assert "wordmark" not in header and header.count('<img class="brand-logo"') == 1, name
            assert f'src="/{path}"' in header and 'alt="Clearwater Family Dental"' in header, name
        else:
            assert "brand-logo" not in page and '<a class="brand brand--wordmark" href="/">' in header, name
            assert '<span class="wordmark-name">Clearwater Family Dental</span>' in header, name
    for name, page in html_pages(plain).items():
        assert "brand-logo" not in page and "Clearwater Family Dental</" in page, name
    assert ("brand--wordmark" in html_pages(plain)["index.html"]) == (style == "boutique")


@pytest.mark.parametrize("style", STYLES)
def test_the_page_weight_budget_holds(library, style):
    s = dentist(style)
    w = _weights(library, build(library, s))

    assert w["css"] <= 40 * 1024 and w["js"] <= 8 * 1024 and w["fonts"] <= 220 * 1024, w
    for path, p in w["pages"].items():
        assert p["html"] <= 150 * 1024, (path, p)
        assert p["total"] <= 1200 * 1024, (path, p)


# ---------------------------------------------------------------- the form's answer reaches the lead


def test_the_patient_answer_is_stored_on_the_lead_and_in_the_lead_email(client, conn, monkeypatch):
    _key, page = _page(conn)
    sent = _resend(monkeypatch)
    form = {
        "name": "Ana",
        "phone": "4085550199",
        "patient": "New patient",
        "service": "Invisalign",
        "preferred_time": "Morning",
        "_intent": "appointment",
    }
    answer = client.post(f"/t/form/{page['id']}", data=form).json()

    assert answer["lead"] is True and answer["conversion"] == "appointment"
    row = conn.execute("SELECT fields, intent FROM lead").fetchone()
    assert row["fields"]["patient"] == "New patient" and row["intent"] == "appointment"
    body = json.loads(sent[0].read())["text"]
    assert "Patient: New patient" in body and "Service: Invisalign" in body and "Preferred time: Morning" in body
    # a form without the field prints no Patient line
    client.post(f"/t/form/{page['id']}", data={"name": "Bo", "phone": "4085550198"})
    assert "Patient:" not in json.loads(sent[1].read())["text"]


def test_a_lead_sent_without_a_patient_stores_none(client, conn, monkeypatch):
    """The ruling: an answer the visitor never chose is stored as none, never as the first option. The browser sends
    nothing for a select left on its disabled "Choose one"; a value sent empty (or blank) is the same."""
    _key, page = _page(conn)
    sent = _resend(monkeypatch)
    form = {"name": "Ana", "phone": "4085550199", "service": "Invisalign", "_intent": "appointment"}
    for extra in ({}, {"patient": "", "preferred_time": ""}, {"patient": "   "}):
        assert client.post(f"/t/form/{page['id']}", data={**form, **extra}).json()["lead"] is True

    rows = conn.execute("SELECT fields FROM lead ORDER BY created_at, id").fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["fields"] == {"name": "Ana", "phone": "4085550199", "service": "Invisalign"}, row["fields"]
    assert len(sent) == 3 and all("Patient:" not in json.loads(r.read())["text"] for r in sent)
