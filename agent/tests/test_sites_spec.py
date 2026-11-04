"""The industry site's document and its two families (A3p-6 batch 1 Task 1; plan
2026-10-06-agent-a3p-6-1-industry-site-templates Q1, Q2, Q10, Q15, Q20): the samples load and validate in each style,
every refusal is one sentence, every style's token pairs read, the slot map, and the tables."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from agent import page_builder
from agent.models import SiteWords
from agent.sites import families, samples, spec

SAMPLE_FAMILIES = (
    ("roofer", "home_services"),
    ("dentist", "dental"),
    ("hvac", "home_services"),
    ("plumber", "home_services"),
    ("electrician", "home_services"),
)
STYLES = [(n, s) for n, f in SAMPLE_FAMILIES for s in families.get(f).styles]


def _load(name: str = "roofer", style: str = "solid", **over) -> spec.SiteSpec:
    s = samples.load(name, style=style)
    return s.model_copy(update=over) if over else s


def _refused(s: spec.SiteSpec) -> str:
    with pytest.raises(ToolError) as e:
        spec.validate(s)
    msg = str(e.value)
    assert msg.endswith(".") and msg.count(". ") == 0, msg  # one sentence
    return msg


def test_the_samples_and_their_styles():
    assert samples.names() == ["roofer", "dentist", "hvac", "plumber", "electrician"]  # a sample per trade (2026-10-08)
    assert len(STYLES) == 15
    for name, style in STYLES:
        s = spec.validate(samples.load(name, style=style))
        assert s.style == style and isinstance(s.words, SiteWords)
        pages = spec.pages(s)
        assert len(pages) == 7
        assert pages[:4] == [
            ("home", "index.html", None),
            ("about", "about/index.html", None),
            ("services", "services/index.html", None),
            ("contact", "contact/index.html", None),
        ]
        leads = [x.slug for x in s.facts.services if x.lead]
        assert [p[2] for p in pages[4:]] == leads and len(leads) == 3
        assert all(p[0] == "service" and p[1] == f"services/{p[2]}/index.html" for p in pages[4:])
        assert {w.slug for w in s.words.service_pages} == set(leads)


def test_the_samples_are_the_plans_businesses():
    roofer, dentist = _load("roofer"), _load("dentist", "calm")
    assert (roofer.facts.name, roofer.facts.city, roofer.trade) == ("Oakline Roofing", "Charlotte", "roofing")
    assert (dentist.facts.name, dentist.facts.city, dentist.facts.storefront) == (
        "Clearwater Family Dental",
        "San Jose",
        True,
    )
    assert len(roofer.facts.services) == len(dentist.facts.services) == 6
    assert dentist.facts.dentist.name == "Dr. [Your name], DDS"
    for s in (roofer, dentist):
        assert s.facts.email is None and "555-01" in s.facts.phone
        assert all(r.text.startswith("[A real review") and r.first_name == "[First name]" for r in s.facts.reviews)


def test_service_url_and_unknown_sample():
    s = _load()
    assert (
        spec.service_url(s, "oakline.example.com", "roof-repair") == "https://oakline.example.com/services/roof-repair/"
    )
    assert spec.service_url(s, None, "roof-repair") == "/services/roof-repair/"
    with pytest.raises(ValueError):
        samples.load("florist", style="solid")


def test_refusals_are_one_sentence_each():
    s = _load()
    services = list(s.facts.services)

    def with_services(items):
        return s.model_copy(update={"facts": s.facts.model_copy(update={"services": items})})

    seventh = [x.model_copy(update={"lead": True}) for x in services] + [
        spec.Service(slug="gutters-2", name="Gutters", lead=True)
    ]
    assert len([x for x in seventh if x.lead]) == 7
    assert "6" in _refused(with_services(seventh))
    twice = services + [services[0].model_copy()]
    assert "roof-replacement" in _refused(with_services(twice))
    assert "slug" in _refused(with_services([services[0].model_copy(update={"slug": "roof repair"})] + services[1:]))
    assert _refused(with_services([x.model_copy(update={"lead": False}) for x in services]))
    assert _refused(s.model_copy(update={"family": "plumbing_co"}))
    assert _refused(s.model_copy(update={"style": "calm"}))  # the other family's
    assert _refused(s.model_copy(update={"trade": "dentist"}))
    assert _refused(s.model_copy(update={"accent": "#ABC"}))
    facts = s.facts
    assert _refused(s.model_copy(update={"facts": facts.model_copy(update={"gbp_url": "http://g.page/x"})})) == (
        "That Google profile link must start with https://."
    )
    assert "https://" in _refused(s.model_copy(update={"facts": facts.model_copy(update={"gbp_url": "javascript:x"})}))
    assert "e-mail" in _refused(s.model_copy(update={"facts": facts.model_copy(update={"email": "not an address"})}))
    for host in ("https://evil.example/x", "https://notgoogle.com/x", "https://google.com.evil.example/x"):
        assert _refused(s.model_copy(update={"facts": facts.model_copy(update={"gbp_url": host})})) == (
            "That Google profile link must be a Google address."
        )
    for good in ("https://maps.app.goo.gl/x", "https://www.google.com/maps/place/x", "https://google.com/maps"):
        assert spec.validate(s.model_copy(update={"facts": facts.model_copy(update={"gbp_url": good})}))
    ok = facts.model_copy(update={"gbp_url": "https://g.page/r/x", "email": "hello@oakline.example"})
    assert spec.validate(s.model_copy(update={"facts": ok})).facts.email == "hello@oakline.example"
    assert "too light" in _refused(s.model_copy(update={"accent": "#FFFF99"}))
    assert "too dark" in _refused(_load("roofer", "bold").model_copy(update={"accent": "#222222"}))
    assert "too dark" in _refused(_load("dentist", "boutique").model_copy(update={"accent": "#222222"}))


def test_the_phone_is_one_target_four_ways():
    s = _load()
    for written in ("(704) 555-0142", "704.555.0142", "+1 704 555 0142", "7045550142"):
        t = s.model_copy(update={"facts": s.facts.model_copy(update={"phone": written})})
        assert page_builder.tel(spec.validate(t).facts.phone) == "+17045550142"
    assert page_builder._tel("7045550142") == "+17045550142"
    for bad in ("555-0142", "", "  "):
        t = s.model_copy(update={"facts": s.facts.model_copy(update={"phone": bad})})
        assert "phone" in _refused(t)


def test_accent_validates_and_is_kept():
    s = _load("roofer", "bold")
    assert spec.validate(s.model_copy(update={"accent": "#fb923c"})).accent == "#FB923C"
    assert spec.validate(samples.load("dentist", style="boutique", accent="#E8B4A0")).accent == "#E8B4A0"


def test_contrast():
    assert spec.contrast("#FFFFFF", "#000000") == 21.0
    assert spec.contrast("#000000", "#FFFFFF") == 21.0
    assert spec.contrast("#777777", "#777777") == 1.0


@pytest.mark.parametrize(("name", "style"), STYLES)
def test_every_style_reads(name, style):
    fam = families.get(dict(SAMPLE_FAMILIES)[name])
    st = fam.styles[style]
    assert st.accent in st.accent_options and st.accent_options[0] == st.accent and len(st.accent_options) == 4
    pairs = st.pairs()
    assert pairs
    for label, fg, bg, floor in pairs:
        assert spec.exact_contrast(fg, bg) >= floor, (
            f"{fam.id}/{style}: {label} {fg} on {bg} is {spec.contrast(fg, bg):.2f}"
        )
    for opt in st.accent_options:  # the button's words on every accent the owner may pick
        assert spec.exact_contrast(st.on_accent, opt) >= 4.5, f"{fam.id}/{style}: {st.on_accent} on {opt}"


def test_the_eyebrow_in_the_accent_is_checked():
    labels = [p[0] for p in families.get("dental").styles["boutique"].pairs()]
    assert any("eyebrow on dark" in x for x in labels)
    assert any("eyebrow on dark" in x for x in [p[0] for p in families.get("home_services").styles["bold"].pairs()])


def test_the_families():
    assert families.FAMILIES == {"home_services", "dental"}
    hs, de = families.get("home_services"), families.get("dental")
    assert set(hs.styles) == {"solid", "clean", "bold"} and set(de.styles) == {"calm", "warm", "boutique"}
    assert "best" in hs.banned and "specialist" not in hs.banned
    assert {"specialist", "specialists", "best", "#1"} <= set(de.banned)
    assert hs.form.intent == "quote" and de.form.intent == "appointment"
    assert de.form.fields == ("name", "phone", "patient", "service", "preferred_time")
    assert hs.problems["roofing"][0].label == "It's leaking" and de.problems == {}
    assert (hs.key_photo_slot, de.key_photo_slot) == ("hero", "office-1")
    assert hs.styles["solid"].accent == "#C2410C" and hs.styles["bold"].display_upper
    with pytest.raises(KeyError):
        families.get("law")


def test_slot_kinds_follow_the_slot_map():
    roofer = families.get("home_services").slot_kinds(_load())
    assert roofer["hero"] == "hero" and roofer["about"] == "about" and roofer["contact"] == "general"
    assert [roofer[f"gallery-{i}"] for i in (1, 2, 3)] == ["gallery"] * 3
    assert roofer["service-roof-repair"] == "service-roofing"
    assert not any(k in roofer for k in ("team", "dentist", "work-people"))  # people slots stay empty
    dentist = families.get("dental").slot_kinds(_load("dentist", "calm"))
    assert dentist["hero"] == "hero" and dentist["office-1"] == "reception" and dentist["office-2"] == "exterior"
    assert dentist["contact"] == "exterior"
    assert dentist["service-invisalign"] == "aligners" and dentist["service-dental-implants"] == "service-treatment"


def test_the_tables_are_there_and_empty(conn):
    for t in ("site_asset", "site_asset_file", "site_spec"):
        assert conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] == 0
    cols = {
        r["column_name"]
        for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'page'")
    }
    assert "site_spec_id" in cols
    for t in ("page_preview", "page_version"):
        assert conn.execute(f"SELECT count(shot_tablet) AS n FROM {t}").fetchone()["n"] == 0


def test_a_trades_hero_is_a_photo_of_its_own_trade():
    """2026-10-08: the hero kind was the family's, and four of its five photos are roofs -- an AC contractor's site led
    with a roof. A trade other than roofing leads with its own trade's photos; the roofer keeps the curated heroes, and
    a business of no trade keeps them too."""
    home = families.get("home_services")
    assert home.slot_kinds(_load())["hero"] == "hero"
    for sample, kind in (
        ("hvac", "service-hvac"),
        ("plumber", "service-plumbing"),
        ("electrician", "service-electrical"),
    ):
        assert home.slot_kinds(_load(sample))["hero"] == kind, sample
    assert home.slot_kinds(_load("hvac", trade=None))["hero"] == "hero"
