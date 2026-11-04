"""The industry site's words (A3p-6 batch 1 Task 7; plan Q11): the samples pass the linter, each claim the facts do
not back is refused with a sentence naming the piece, `write` asks twice, the model reads the facts and never the
snippet or the form address."""

from __future__ import annotations

import json
import re

import pytest
from fastmcp.exceptions import ToolError

from agent import llm
from agent.sites import families, samples, spec, words
from agent.tests import fixtures


def _roofer(**kw):
    return samples.load("roofer", style="solid", **kw)


def _dentist():
    return samples.load("dentist", style="calm")


def _fam(spec):
    return families.get(spec.family)


def _with(spec, **home):
    """`spec`'s words with the home page's fields replaced."""
    w = spec.words.model_copy(deep=True)
    w.home = w.home.model_copy(update=home)
    return w


def test_both_samples_pass():
    for s in (_roofer(), _dentist()):
        assert words.lint(s.words, s, _fam(s)) == []


def _styles_of(name: str) -> list[str]:
    """The sample's family's styles, the base (the canvas's first artboard) first."""
    raw = json.loads((samples.DIR / f"{name}.json").read_text(encoding="utf-8"))
    return list(families.get(raw["family"]).styles)


SIX = [(name, style) for name in ("roofer", "dentist") for style in _styles_of(name)]


@pytest.mark.parametrize("name, style", SIX)
def test_every_look_says_its_own_artboards_words_and_passes_the_linter(name, style):
    """Fix round 1: each of the six looks carries its artboard's words (the base's, with the style's own laid over
    them), and each passes the linter and Q4 -- the home headline names a lead service and the city."""
    s = spec.validate(samples.load(name, style=style))
    assert words.lint(s.words, s, _fam(s)) == []
    head = s.words.home.headline.lower()
    assert any(n.lower() in head for n in words.lead_names(s)) and s.facts.city.lower() in head, head


def _known(over: dict, into: dict, where: tuple) -> None:
    """Every key of `over` (an override) is one `into` (the base words) has, at every depth."""
    for key, value in over.items():
        assert key in into, (*where, key)
        if isinstance(value, dict):
            _known(value, into[key], (*where, key))


def test_the_style_words_only_change_what_their_artboard_words_differently():
    """Each sample's `style_words` name only its family's other styles and only fields the base words have (a typo
    would be dropped silently), and each changes something; the facts -- the placeholders Q10 keeps, the reviews and
    the dentist -- are the same in every style. A trade's sample (2026-10-08) has none: every look says its base words,
    and the style alone changes."""
    for name in samples.names():
        raw = json.loads((samples.DIR / f"{name}.json").read_text(encoding="utf-8"))
        base, *others = _styles_of(name)
        assert bool(raw["style_words"]) is (name in ("roofer", "dentist")), name  # the canvas's artboards: these two
        assert set(raw["style_words"]) <= set(others), name
        first = samples.load(name, style=base)
        for style, over in raw["style_words"].items():
            _known(over, raw["words"], (name, style))
            look = samples.load(name, style=style)
            assert look.words != first.words and look.facts == first.facts, (name, style)
    clean, warm, boutique = (
        samples.load("roofer", style="clean").words.home,
        samples.load("dentist", style="warm").words.home,
        samples.load("dentist", style="boutique").words.home,
    )
    assert clean.lead.startswith("A roof you won't think about for decades.")  # Q4: Clean's soft line is its lead
    assert warm.eyebrow and boutique.eyebrow and not samples.load("dentist", style="calm").words.home.eyebrow


@pytest.mark.parametrize(
    "text, piece",
    [
        ("Roof repairs from $199 a visit", "price"),
        ("More than 20 years on Charlotte roofs", "years"),
        ("Rated 4.9 stars by owners", "rating"),
        ("Licensed, License #12345", "licence"),
        ("Call 704-555-9999 today", "phone"),
        ("Write to hello@example.com", "e-mail"),
        ("The BEST ROOF in town", "capitals"),
    ],
)
def test_claims_the_facts_do_not_back_are_refused(text, piece):
    s = _roofer()
    w = _with(s, lead=text)
    notes = words.lint(w, s, _fam(s))
    assert notes and all(n.startswith("The home page's lead") for n in notes), notes


def test_the_business_own_phone_and_backed_numbers_are_fine():
    s = _roofer()
    w = _with(s, lead="Call (704) 555-0142, 15 years on Charlotte roofs, a 10-year workmanship warranty.")
    assert words.lint(w, s, _fam(s)) == []


def test_faq_answers_are_checked_too():
    s = _roofer()
    w = s.words.model_copy(deep=True)
    w.home.faq[0].a = "We take Aetna and cost $50 a month."
    notes = words.lint(w, s, _fam(s))
    assert any("The home page's faq 1 a" in n and "a price" in n for n in notes), notes


def test_every_heading_is_linted():
    s = _roofer()
    w = s.words.model_copy(deep=True)
    w.home.reviews_heading = "Rated 4.9 stars"
    w.service_pages[0].signs_heading = "BEST signs"
    notes = words.lint(w, s, _fam(s))
    assert any("reviews heading" in n for n in notes) and any(
        "roof-replacement page's signs heading" in n for n in notes
    )


def test_the_limit_of_a_field_is_the_limit_of_its_description():
    s = _roofer()
    w = _with(s, new_patients_heading="x" * 61)
    assert any("over 60 characters" in n for n in words.lint(w, s, _fam(s)))


def test_banned_words_per_family():
    r = _roofer()
    assert any("guaranteed" in n for n in words.lint(_with(r, lead="A guaranteed fit."), r, _fam(r)))
    d = _dentist()
    for word in ("specialist", "painless", "pain-free"):
        notes = words.lint(_with(d, lead=f"A {word} visit."), d, _fam(d))
        assert any(word in n for n in notes), (word, notes)


def test_the_home_headline_names_the_service_and_the_city():
    s = _roofer()
    notes = words.lint(_with(s, headline="Roofs you can trust"), s, _fam(s))
    assert any("must name the lead service and the city" in n for n in notes)
    assert words.lint(_with(s, headline="Roof repair in Charlotte"), s, _fam(s)) == []


def test_a_service_page_for_no_lead_service_is_refused():
    s = _roofer()
    w = s.words.model_copy(deep=True)
    w.service_pages[0] = w.service_pages[0].model_copy(update={"slug": "gutters"})  # a service with no page of its own
    notes = words.lint(w, s, _fam(s))
    assert any("“gutters”" in n and "lead services" in n for n in notes)
    assert any("roof-replacement service has no page words" in n for n in notes)


def test_the_counts():
    s = _roofer()
    w = _with(s, why_points=["a"] * 5, steps=s.words.home.steps[:1])
    notes = words.lint(w, s, _fam(s))
    assert any("more than 4 reasons" in n for n in notes) and any("3 or 4 steps" in n for n in notes)


def test_write_asks_again_with_the_notes(monkeypatch):
    s = _roofer(words=False)
    assert s.words is None
    good = fixtures.site_words_fixture()
    bad = good.model_copy(deep=True)
    bad.home.lead = "More than 20 years on Charlotte roofs"
    gen = fixtures.FakeGenerator(sites=[bad, good])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    out = words.write(None, object(), s, direction="Keep it warm")
    assert out == good and len(gen.site_calls) == 2
    assert gen.site_calls[0]["direction"] == "Keep it warm"
    second = gen.site_calls[1]["direction"]
    assert second.startswith("Keep it warm") and "The home page's lead" in second


def test_write_fails_with_the_notes_after_two_refusals(monkeypatch):
    s = _roofer(words=False)
    bad = fixtures.site_words_fixture()
    bad.home.lead = "More than 20 years on Charlotte roofs"
    gen = fixtures.FakeGenerator(sites=[bad])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    with pytest.raises(ToolError) as e:
        words.write(None, object(), s)
    assert "The home page's lead" in str(e.value) and len(gen.site_calls) == 2


def test_the_model_reads_the_facts_never_the_snippet_or_the_form_address(monkeypatch):
    s = _roofer(words=False)
    s.facts.email = "forms@oakline.example"
    s.facts.address = "1 Secret Street"
    gen = fixtures.FakeGenerator()
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    words.write(None, object(), s)
    call = gen.site_calls[0]
    text = repr(call)
    assert call["facts"]["name"] == "Oakline Roofing" and call["facts"]["services"]
    assert "forms@oakline.example" not in text and "1 Secret Street" not in text and "snippet" not in text
    assert call["message"]["headline_pattern"] == "Roof replacement in Charlotte"
    assert "guaranteed" in call["family"]["banned"]


def test_system_site_names_the_rules():
    t = llm.SYSTEM_SITE
    for needle in (
        "never write them",
        "phone",
        "forms",
        "hours",
        "address",
        "the law",
        "Never invent",
        "names the lead service and the city",
    ):
        assert needle in t, needle


@pytest.mark.parametrize(
    "where, piece",
    [
        ("home.headline", "home page's headline"),
        ("home.lead", "home page's lead"),
        ("about.headline", "about page's headline"),
        ("about.lead", "about page's lead"),
        ("services.headline", "services page's headline"),
        ("contact.headline", "contact page's headline"),
        ("cta_label", "call button"),
        ("book_label", "second button"),
        ("service_pages.0.lead", "page's lead"),
        ("service_pages.0.what", "page's what"),
    ],
)
def test_an_empty_required_piece_is_named(where, piece):
    s = _roofer()
    for blank in ("", "   "):
        w = s.words.model_copy(deep=True)
        *path, last = where.split(".")
        target = w
        for part in path:
            target = target[int(part)] if part.isdigit() else getattr(target, part)
        setattr(target, last, blank)
        notes = words.lint(w, s, _fam(s))
        assert any(piece in n and "empty" in n for n in notes), (where, notes)


def test_an_empty_section_heading_is_allowed():
    s = _roofer()
    w = _with(s, services_heading="", faq_heading="")
    assert words.lint(w, s, _fam(s)) == []


# ---------------------------------------------------------------- the hours, written tidily (2026-10-08)
# The founder's dev walk, 2026-10-08: the owner's "9am-5pm Monday to Friday" went on the site as typed. The words'
# call writes them the way a shop door says them -- one line per group of days -- in the same call, never another.
TIDY = ["Monday to Friday, 9 am to 5 pm", "Saturday and Sunday, closed"]


def _writes(monkeypatch, hours: list[str]):
    out = fixtures.site_words_fixture().model_copy(update={"hours": hours})
    gen = fixtures.FakeGenerator(sites=[out])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    return gen


def test_the_words_carry_the_hours_written_tidily_and_a_time_rewritten_is_no_refusal(monkeypatch):
    s = _roofer(words=False)
    s.facts.hours = ["9-17 mon-fri, weekends closed"]  # "5 pm" is in none of the owner's words, and that is fine
    gen = _writes(monkeypatch, TIDY)
    out = words.write(None, object(), s)
    assert out.hours == TIDY and len(gen.site_calls) == 1


@pytest.mark.parametrize(
    "given, written",
    [
        ([], TIDY),  # the owner gave no hours: none are made up
        (["9am-5pm Monday to Friday"], ["Monday to Friday, 9 am to 5 pm, and on most public holidays too"]),  # long
        (["9am-5pm Monday to Friday"], [f"Day {i}, 9 am to 5 pm" for i in range(8)]),  # more lines than a week
        (["9am-5pm Monday to Friday"], ["Call 704-555-0142 for hours"]),  # a phone is the template's
    ],
)
def test_hours_the_owner_never_gave_or_that_do_not_fit_are_left_to_the_owners_own_lines(monkeypatch, given, written):
    s = _roofer(words=False)
    s.facts.hours = given
    _writes(monkeypatch, written)
    assert words.write(None, object(), s).hours == []


def test_the_prompt_asks_for_the_hours_in_one_shape():
    prompt = " ".join(llm.SYSTEM_SITE.split())
    assert "Monday to Friday, 9 am to 5 pm" in prompt and "Never add a day or a time" in prompt


def test_every_sample_passes_the_linter_in_its_base_style():
    """The roofer and the dentist, and a sample for each other home-services trade (2026-10-08: hvac, plumber,
    electrician -- their words written by the model and linted, then kept as the sample's)."""
    assert samples.names() == ["roofer", "dentist", "hvac", "plumber", "electrician"]
    for name in samples.names():
        family = json.loads((samples.DIR / f"{name}.json").read_text(encoding="utf-8"))["family"]
        s = samples.load(name, style=next(iter(families.get(family).styles)))
        assert words.lint(s.words, s, _fam(s)) == [], name


# What the K3 review of the knowledge files (2026-10-15) keeps off a home-services site: claim help, which Florida and
# Texas limit for a contractor (agent/knowledge/home_services/roofing.md), and "bonded" or "insured" without the type
# of coverage, which California disciplines on a website too (agent/knowledge/home_services/compliance.md, Key point 3)
CLAIM_HELP = re.compile(
    r"insurance claims?|adjuster|deductible|\bbonded\b|(?:handle|file|help with) (?:the|your) claim", re.IGNORECASE
)
INSURANCE = re.compile(r"\b(?:general liability )?insur(?:ed|ance)\b", re.IGNORECASE)


def _texts(value):
    """Every string in a JSON value, however deep."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _texts(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _texts(inner)


def test_no_sample_offers_claim_help_or_claims_insurance_without_naming_the_coverage():
    """Each sample's facts, words and looks: no claim help and no "bonded"; a home-services sample says "insured" or
    "insurance" only as "general liability insured" (the dentist's insurance is the plans it takes); and none carries
    the `insurance_claims` fact the intake no longer takes."""
    for name in samples.names():
        raw = json.loads((samples.DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert "insurance_claims" not in raw["facts"], name
        for text in _texts({key: raw.get(key) for key in ("facts", "words", "style_words")}):
            assert not CLAIM_HELP.search(text), (name, text)
            if raw["family"] == "home_services":
                for claim in INSURANCE.finditer(text):
                    assert claim.group(0).lower().startswith("general liability"), (name, text)
