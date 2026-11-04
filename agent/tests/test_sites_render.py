"""The industry site rendered into a bundle (A3p-6 batch 1 Task 3; plan 2026-10-06-agent-a3p-6-1-industry-site-templates
Q3-Q5, Q12, Q15-Q20, Q23, Q25, Q26): the sample roofer in Solid, Clean and Bold, every page through A3p-3's bundle checks
with its tracking, one H1, the one phone number, the menu and the phone bar, the quote form, nothing foreign, the base
prefix, stock labelled only in a preview, people slots never stock, the fonts, the stylesheet's tokens, the weight
budget, thin facts, markup in the facts, the problem picker, the structured data, the 404 page and an owner's photo."""

from __future__ import annotations

import hashlib
import io
import json
import re
from html.parser import HTMLParser

import pytest
from markupsafe import escape
from PIL import Image

from agent import bundles, db, hosted, models
from agent.sites import families, images, render, samples, spec, stock
from agent.sites import words as site_words
from agent.sites.spec import Facts, ImageRef, Review, Service
from agent.tests.conftest import JARGON

AGENT = "http://testserver"
PAGE = "pg_site_test"
SNIPPET = f'<script async src="{AGENT}/t/admiry.js" data-page="{PAGE}"></script>'
STYLES = ("solid", "clean", "bold")
TEL = "tel:+17045550142"
FORM_FIELDS = ["name", "phone", "service", "zip", "preferred_time", "website2"]
LABEL = "Replace with your photo"
FAMILY = families.get("home_services")


# ---------------------------------------------------------------- a fake stock library (Task 2's loader)


def jpeg(w: int, h: int, shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (shade, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


def logo_png(w: int = 400, h: int = 100, shade: int = 140) -> bytes:
    """A logo that kept its transparency (A3p-6 batch 2 Task 3 keeps one): a PNG with an alpha channel, its middle
    opaque and the rest see-through."""
    img = Image.new("RGBA", (w, h), (20, 60, shade, 0))
    img.paste((20, 60, shade, 255), (10, 10, w - 10, h - 10))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_page(conn, biz: str = "biz_render") -> str:
    """A business and its one page row, for the owner photos a test puts on it."""
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'draft') ON CONFLICT (id) DO NOTHING", (biz,))
    page_id = db.new_id("pg")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, html, status) "
        "VALUES (%s, %s, 'hosted', '', NULL, '', 'preview')",
        (page_id, biz),
    )
    return page_id


def add_photo(
    conn, page_id: str, photo_id: str, data: bytes, *, kind: str | None = None, content_type: str = "image/jpeg"
) -> bytes:
    """An owner's photo of `kind` on `page_id`, its size read from the file (Task 3's upload link is what makes these
    rows in a flow; the tests put them in directly). Returns `data`."""
    width, height = Image.open(io.BytesIO(data)).size
    conn.execute(
        "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (photo_id, page_id, data, content_type, width, height, kind),
    )
    return data


# (kind, how many, people)
LIBRARY = [("hero", 2, False), ("hero", 1, True), ("about", 2, False), ("gallery", 3, False), ("general", 2, False)]
LIBRARY += [("service-roofing", 4, False), ("about", 1, True)]


def _entries(spec=LIBRARY, family: str = "home_services") -> tuple[list[dict], dict[str, bytes]]:
    out, answers, n = [], {}, 0
    for kind, count, people in spec:
        for _ in range(count):
            n += 1
            file = f"files/{family}/{kind}-{n:02d}.jpg"
            data = jpeg(1100, 733, (n * 17) % 200)
            url = f"https://images.unsplash.com/{file}"
            answers[url] = data
            out.append(
                {
                    "file": file,
                    "family": family,
                    "slot": kind,
                    "subject": kind,
                    "alt": f"A {kind} photo, number {n}",
                    "source": "unsplash",
                    "page_url": f"https://unsplash.com/photos/{n}",
                    "image_url": url,
                    "photographer": "Sean Foster",
                    "license": "Unsplash License",
                    "width": 1100,
                    "height": 733,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "people": people,
                    "logos": False,
                }
            )
    return out, answers


def load_library(conn, tmp_path, spec=LIBRARY, family: str = "home_services"):
    """A fake stock library of `spec` (kind, how many, people) for `family`, loaded by Task 2's loader."""
    entries, answers = _entries(spec, family)
    manifest = tmp_path / f"manifest-{family}.json"
    manifest.write_text(json.dumps({"version": 1, "created": "2026-10-05T16:10:18Z", "images": entries}))
    out = stock.load(conn, manifest, fetch=answers.__getitem__)
    assert out["loaded"] == len(entries) and not out["failed"]
    return conn


@pytest.fixture
def library(conn, tmp_path):
    return load_library(conn, tmp_path)


# ---------------------------------------------------------------- helpers


class Doc(HTMLParser):
    """Every start tag with its attributes, the H1s' text, the scripts' bodies by type, the figures."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.h1: list[str] = []
        self.scripts: list[tuple[dict, str]] = []
        self.figures: list[dict] = []  # {attrs, text, imgs}
        self._h1: list[str] | None = None
        self._script: tuple[dict, list[str]] | None = None
        self._figure: dict | None = None
        self.link_text: dict[int, str] = {}
        self._link: dict | None = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        self.tags.append((tag, a))
        if tag == "h1":
            self._h1 = []
        elif tag == "script":
            self._script = (a, [])
        elif tag == "figure":
            self._figure = {"attrs": a, "text": [], "imgs": []}
        elif tag == "a":
            self._link = a
            self.link_text[id(a)] = ""
        elif tag == "img" and self._figure is not None:
            self._figure["imgs"].append(a)

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        if tag == "h1" and self._h1 is not None:
            self.h1.append(" ".join("".join(self._h1).split()))
            self._h1 = None
        elif tag == "script" and self._script is not None:
            self.scripts.append((self._script[0], "".join(self._script[1])))
            self._script = None
        elif tag == "figure" and self._figure is not None:
            self._figure["text"] = " ".join("".join(self._figure["text"]).split())
            self.figures.append(self._figure)
            self._figure = None

    def handle_data(self, data):
        if self._link is not None:
            self.link_text[id(self._link)] += data
        if self._h1 is not None:
            self._h1.append(data)
        if self._script is not None:
            self._script[1].append(data)
        if self._figure is not None:
            self._figure["text"].append(data)

    def all(self, tag: str) -> list[dict]:
        return [a for t, a in self.tags if t == tag]

    def urls(self) -> list[str]:
        out = []
        for _t, a in self.tags:
            out += [a[k] for k in ("href", "src", "action") if a.get(k)]
            if a.get("srcset"):
                out += [part.strip().split()[0] for part in a["srcset"].split(",")]
        return out

    def sections(self) -> list[str]:
        return [a["data-section"] for _t, a in self.tags if a.get("data-section")]


def roofer(style: str = "solid", **facts) -> spec.SiteSpec:
    s = samples.load("roofer", style=style)
    if facts:
        s = s.model_copy(update={"facts": s.facts.model_copy(update=facts)})
    return spec.validate(s)


def build(
    conn, s: spec.SiteSpec, *, base: str = "", preview: bool = True, hostname: str | None = None, photos_of: str = PAGE
):
    """The bundle of `s`; `photos_of` is the page whose owner photos the slots may use (the rendered page's own)."""
    resolved = images.fill(conn, s, business_id="biz_render", page_id=photos_of)
    return render.bundle(
        s,
        page_id=PAGE,
        agent_base=AGENT,
        snippet=SNIPPET,
        hostname=hostname,
        images=resolved,
        base=base,
        preview=preview,
    )


def html_pages(rendered) -> dict[str, str]:
    return {p: b.decode("utf-8") for p, b in rendered.files.items() if p.endswith(".html")}


def checked(rendered, own_host: str | None = None) -> list[dict]:
    judged = bundles.Judged(files=dict(rendered.files))
    bundles.check(judged, page_id=PAGE, own_host=own_host, agent_base=AGENT, snippet=SNIPPET)
    return judged.problems


def text_of(page: str) -> str:
    return hosted.visible_text(page)


CHOOSE = "Choose one"


class _Selects(HTMLParser):
    """Every <select> of a page by name: its attributes and its options' (attributes, text)."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, dict] = {}
        self._select: dict | None = None
        self._option: list | None = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "select":
            self._select = {"attrs": a, "options": []}
            self.found[a.get("name", "")] = self._select
        elif tag == "option" and self._select is not None:
            self._option = [a, ""]
            self._select["options"].append(self._option)

    def handle_endtag(self, tag):
        if tag in ("option", "select"):
            self._option = None
        if tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data


def selects(page: str) -> dict[str, dict]:
    return _Selects(page).found


def assert_no_select_starts_on_an_answer(
    page: str, required: set[str], optional: set[str], preselected: dict[str, str] | None = None
) -> None:
    """The ruling (A3p-6 batch 1 follow-ups, amended in fix round 1): every select's first choice is an empty,
    disabled "Choose one", and it is the selected one -- except in a select `preselected` names, which opens on the
    choice it gives (a service page's own service: the informed default); no other choice is selected or disabled,
    and only `required` carry the attribute. So nobody sends an answer they never picked or were not shown."""
    preselected = preselected or {}
    found = selects(page)
    assert set(found) == required | optional, sorted(found)
    for name, select in found.items():
        (first, text), *rest = select["options"]
        assert text.strip() == CHOOSE and first.get("value") == "" and "disabled" in first, (name, first, text)
        chosen = [t.strip() for a, t in rest if "selected" in a]
        if name in preselected:
            assert "selected" not in first and chosen == [preselected[name]], (name, chosen)
        else:
            assert "selected" in first and chosen == [], (name, chosen)
        assert rest and not [a for a, _t in rest if "disabled" in a], name
        assert ("required" in select["attrs"]) == (name in required), (name, select["attrs"])


# The thin roofer's own words where the sample's state what it does not give: a licence, insurance, insurance-claim
# help, free inspections, a warranty, years, a family business, repairs or storm work, the towns around Charlotte.
THIN_HOME = {
    "eyebrow": "Roofing in Charlotte",
    "headline": "Roof replacement in Charlotte",
    "lead": "New roofs and gutters for homes in Charlotte.",
    "services_lead": "",
    "why_points": [],
    "about_text": "",
    "steps": [
        {"title": "Call or send the form", "text": "Tell us what is going on. We call you back."},
        {"title": "We look at the roof", "text": "We check it and tell you plainly what it needs."},
        {"title": "We do the work", "text": "We do the work and tell you when it is done."},
    ],
    "area_text": "",
    "faq": [
        {
            "q": "How long does a new roof take?",
            "a": "It depends on the size of the roof and the weather. We give you a schedule before we start.",
        },
        {
            "q": "How do I know if I need a new roof?",
            "a": "It depends on the age of the roof and how widespread the damage is. We look at it and tell you plainly.",
        },
        {"q": "Do you put in gutters too?", "a": "Yes. Ask us about them when you call."},
        {"q": "How do I get a quote?", "a": "Call, or send the form, and we call you back to agree on a time."},
    ],
    "cta_text": "Call now, or send the form and we will call you back.",
}
THIN_PAGES = {
    "about": {
        "lead": "A roofing company in Charlotte.",
        "story": [
            "We replace roofs and put in gutters for homes in Charlotte.",
            "We explain the job before it starts and tell you plainly what the roof needs.",
        ],
    },
    "services": {"lead": "New roofs and gutters for homes in Charlotte."},
}


def thin(style: str) -> spec.SiteSpec:
    """The Review Focus's thin business: a name, a phone, a city and two services (a roof replacement, gutters),
    nothing else. Its words are the sample's base words (Solid's) with its own wherever those state what it does not
    give (`THIN_HOME`, `THIN_PAGES`, the form's heading), so they pass the linter against these facts, as the model's
    would. The test pins that no page names a licence, insurance, a warranty, years or "free", nor shows the trust
    strip, the reviews, Saturdays or a placeholder."""
    s = samples.load("roofer", style=style)
    base = samples.load("roofer", style="solid").words
    facts = Facts(
        name="Oakline Roofing",
        phone="(704) 555-0142",
        city="Charlotte",
        services=[
            Service(slug="roof-replacement", name="Roof replacement", lead=True, summary="A new roof, start to end."),
            Service(slug="gutters", name="Gutters and downspouts", summary="Gutters that carry water away."),
        ],
    )
    own = models.SiteWords.model_validate(
        {
            **base.model_dump(),
            "home": {**base.home.model_dump(), **THIN_HOME},
            "about": {**base.about.model_dump(), **THIN_PAGES["about"]},
            "services": {**base.services.model_dump(), **THIN_PAGES["services"]},
            "service_pages": [p.model_dump() for p in base.service_pages if p.slug == "roof-replacement"],
            "book_label": "Tell us about your roof",
        }
    )
    out = spec.validate(s.model_copy(update={"facts": facts, "words": own}))
    assert site_words.lint(out.words, out, FAMILY) == []
    return out


# ---------------------------------------------------------------- the bundle and its checks


@pytest.mark.parametrize("style", STYLES)
def test_every_page_passes_the_bundle_checks_and_is_tracked(library, style):
    s = roofer(style)
    rendered = build(library, s)

    assert checked(rendered) == []
    assert rendered.pages == [path for _kind, path, _slug in spec.pages(s)]
    assert sorted(html_pages(rendered)) == sorted([*rendered.pages, "404.html"])
    for path, page in html_pages(rendered).items():
        assert page.count(SNIPPET) == 1, path
        assert page.index(SNIPPET) > page.index("</footer>"), path
    assert len(rendered.files) + len(rendered.refs) <= bundles.MAX_FILES


@pytest.mark.parametrize("style", STYLES)
def test_one_h1_per_page_the_service_pages_say_the_service_and_the_city(library, style):
    rendered = build(library, roofer(style))
    pages = html_pages(rendered)

    for path, page in pages.items():
        assert len(Doc(page).h1) == 1, path
    assert Doc(pages["services/roof-replacement/index.html"]).h1 == ["Roof replacement in Charlotte"]
    assert Doc(pages["services/roof-repair/index.html"]).h1 == ["Roof repair in Charlotte"]
    assert Doc(pages["services/storm-damage/index.html"]).h1 == ["Storm damage repair in Charlotte"]
    s = roofer(style)
    head, lead = s.words.home.headline, s.words.home.lead
    home = Doc(pages["index.html"]).h1
    assert home == [head]  # each style's own (fix round 1): Q4 -- a lead service and the city
    assert any(n.lower() in head.lower() for n in site_words.lead_names(s)) and "charlotte" in head.lower()
    assert f"<title>{escape(head)} | Oakline Roofing</title>" in pages["index.html"]
    assert f'<meta name="description" content="{escape(lead)}">' in pages["index.html"]


@pytest.mark.parametrize("style", STYLES)
def test_every_phone_link_is_the_one_number_marked_as_a_call(library, style):
    pages = html_pages(build(library, roofer(style)))

    for path, page in pages.items():
        doc = Doc(page)
        texts = doc.link_text
        calls = [a for a in doc.all("a") if a.get("href", "").startswith("tel:")]
        assert len(calls) >= 2, path  # the header and the phone bar at least
        for a in calls:
            assert a["href"] == TEL, path
            assert a.get("data-admiry") == "call", path
            name, seen = a.get("aria-label", ""), " ".join(texts[id(a)].split())
            assert "(704) 555-0142" in name, path
            assert name.startswith(seen) and seen, (path, name, seen)  # WCAG 2.5.3: the name starts with the label


@pytest.mark.parametrize("phone", ["(704) 555-0142", "704.555.0142", "+1 704 555 0142", "7045550142"])
def test_a_phone_written_four_ways_dials_one_number(library, phone):
    pages = html_pages(build(library, roofer("solid", phone=phone)))

    targets = {a["href"] for page in pages.values() for a in Doc(page).all("a") if a.get("href", "").startswith("tel:")}
    assert targets == {TEL}


@pytest.mark.parametrize("style", STYLES)
def test_the_phone_bar_and_the_menu_are_on_every_page(library, style):
    for path, page in html_pages(build(library, roofer(style))).items():
        doc = Doc(page)
        bars = [a for a in doc.all("div") if "actionbar" in a.get("class", "").split()]
        assert len(bars) == 1, path
        menus = [a for a in doc.all("details") if "menu" in a.get("class", "").split()]
        assert len(menus) == 1, path
        assert doc.all("summary"), path
        assert [a for a in doc.all("nav") if a.get("aria-label")], path
        assert any(a.get("href") == "#main" for a in doc.all("a")), path  # the skip link
        assert doc.all("main") and doc.all("main")[0].get("id") == "main", path


@pytest.mark.parametrize("style", STYLES)
def test_the_quote_form_is_on_contact_and_every_service_page(library, style):
    pages = html_pages(build(library, roofer(style)))
    with_form = {"contact/index.html"} | {
        f"services/{slug}/index.html" for slug in ("roof-replacement", "roof-repair", "storm-damage")
    }

    for path, page in pages.items():
        doc = Doc(page)
        forms = doc.all("form")
        if path not in with_form:
            assert forms == [], path
            continue
        assert len(forms) == 1, path
        form = forms[0]
        assert form["action"] == f"{AGENT}/t/form/{PAGE}" and form["method"] == "post", path
        assert form["data-admiry-intent"] == "quote", path
        fields = [a for t, a in doc.tags if t in ("input", "select", "textarea") and a.get("name")]
        assert [a["name"] for a in fields] == FORM_FIELDS, path
        labelled = {a.get("for") for a in doc.all("label")}
        for a in fields:
            assert a.get("id") in labelled, (path, a["name"])
        assert {a["name"]: a.get("autocomplete") for a in fields}["zip"] == "postal-code"
        assert {a["name"]: a.get("inputmode") for a in fields}["zip"] == "numeric"
        assert "Request a quote" in page and "We call you back to agree on a time." in page
        assert "What you send goes to Oakline Roofing only." in page
        assert any(a.get("href") == "/privacy" for a in doc.all("a"))
        options = [o for o in re.findall(r"<option[^>]*>([^<]*)</option>", page)]
        assert {"Today", "This week", "Flexible", "Roof replacement", "Gutters", "Skylights"} <= set(options)
        assert "<textarea" not in page


@pytest.mark.parametrize("style", STYLES)
def test_no_select_starts_on_an_answer(library, style):
    """On the contact page the service opens on "Choose one" and must be chosen; on a service page it opens on that
    page's own service (the visitor is on its page: an informed default), the placeholder still first. The preferred
    time opens on "Choose one" everywhere and may stay there (no preference)."""
    s = roofer(style)
    pages = html_pages(build(library, s))
    own = {f"services/{x.slug}/index.html": x.name for x in s.facts.services if x.lead}
    with_form = [path for path, page in pages.items() if "<form" in page]

    assert sorted(with_form) == sorted(["contact/index.html", *own])
    for path in with_form:
        chosen = {"service": own[path]} if path in own else None
        assert_no_select_starts_on_an_answer(pages[path], {"service"}, {"preferred_time"}, chosen)


@pytest.mark.parametrize("style", STYLES)
def test_nothing_runs_but_the_snippet_the_structured_data_and_the_sites_own_script(library, style):
    for path, page in html_pages(build(library, roofer(style))).items():
        doc = Doc(page)
        assert not [k for _t, a in doc.tags for k in a if k.startswith("on")], path
        assert not doc.all("iframe") and not doc.all("embed") and not doc.all("object"), path
        for a, body in doc.scripts:
            if a.get("type") == "application/ld+json":
                continue
            if a.get("data-page") == PAGE:
                assert a["src"] == f"{AGENT}/t/admiry.js"
                continue
            assert re.fullmatch(r"/assets/site-[0-9a-f]{12,}\.js", a.get("src", "")), (path, a)
            assert a.get("defer") is not None and not body.strip()
        assert len(doc.scripts) == 3, path


GBP = "https://g.page/r/oakline-roofing"
EMAIL = "hello@oakline.example"


def _allowed(url: str, base: str) -> bool:
    if url.startswith(("tel:", "#", f"{AGENT}/")) or url in ("/privacy", "/terms", f"mailto:{EMAIL}", GBP):
        return True
    if url.startswith("https://maps.google.com/?q="):  # Q14's "Get directions"
        return True
    if base:
        return url == base or url.startswith((base + "/", base + "#"))
    return url.startswith("/") and not url.startswith("//")


@pytest.mark.parametrize("style", STYLES)
def test_every_address_is_the_sites_own_or_admirys(library, style):
    for path, page in html_pages(build(library, roofer(style))).items():
        for url in Doc(page).urls():
            assert _allowed(url, ""), (path, url)


def test_a_base_prefixes_every_path_of_the_site(library):
    rendered = build(library, roofer("bold"), base="/x")

    assert checked(rendered) == []
    for path, page in html_pages(rendered).items():
        urls = Doc(page).urls()
        assert any(u.startswith("/x/assets/") for u in urls), path
        for url in urls:
            assert _allowed(url, "/x"), (path, url)
    css = next(b.decode() for p, b in rendered.files.items() if p.endswith(".css"))
    assert "url(/" not in css and "url(fonts/" in css  # relative to the stylesheet: the base needs nothing there


# ---------------------------------------------------------------- photos


def test_stock_photos_carry_the_label_only_in_a_preview(library):
    s = roofer("clean")
    shown = html_pages(build(library, s, preview=True))
    live = html_pages(build(library, s, preview=False))

    stock_figures = 0
    for path, page in shown.items():
        for fig in Doc(page).figures:
            if fig["attrs"].get("data-source") == "stock":
                stock_figures += 1
                assert LABEL in fig["text"], path
    assert stock_figures >= 7  # the hero, the about photo, the gallery, the service photos, the contact photo
    for path, page in live.items():
        assert LABEL not in page, path


def test_the_hero_loads_first_and_every_other_photo_waits(library):
    page = html_pages(build(library, roofer("solid")))["index.html"]
    imgs = Doc(page).all("img")

    hero = [a for a in imgs if a.get("fetchpriority") == "high"]
    assert len(hero) == 1 and hero[0].get("sizes") == "100vw" and "loading" not in hero[0]
    for a in imgs:
        assert a.get("width") and a.get("height") and a.get("alt") and a.get("srcset"), a
        if a is not hero[0]:
            assert a.get("loading") == "lazy", a
    cards = [a for a in imgs if a.get("sizes", "").startswith("(min-width: 1024px) 33vw")]
    assert cards and all(a["sizes"] == "(min-width: 1024px) 33vw, (min-width: 768px) 50vw, 100vw" for a in cards)


def test_the_people_slots_are_never_filled_from_stock(library):
    s = roofer("solid")
    resolved = images.fill(library, s, business_id="biz_render", page_id=PAGE)
    people = {
        r["blob_sha256"] for r in library.execute("SELECT blob_sha256 FROM site_asset WHERE people").fetchall()
    } | {
        r["sha256"]
        for r in library.execute(
            "SELECT f.sha256 FROM site_asset_file f JOIN site_asset a ON a.id = f.asset_id WHERE a.people"
        ).fetchall()
    }
    rendered = build(library, s)

    assert not {"team", "dentist", "work-people"} & set(resolved)
    assert not people & set(rendered.refs.values())
    for page in html_pages(rendered).values():
        assert 'data-slot="team"' not in page
    # each slot its own photo where the library allows: two heroes, two abouts, three for the gallery
    gallery = {resolved[f"gallery-{i}"].variants[max(resolved[f"gallery-{i}"].variants)] for i in (1, 2, 3)}
    assert len(gallery) == 3


def test_a_stock_photos_paths_are_refs_to_stored_blobs(library):
    rendered = build(library, roofer("solid"))

    assert rendered.refs
    for path, sha in rendered.refs.items():
        assert path == images.path(sha) and re.fullmatch(r"assets/img/[0-9a-f]{16}\.webp", path)
        assert library.execute("SELECT 1 FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()
        assert path not in rendered.files
    shown = "".join(html_pages(rendered).values())
    for path in rendered.refs:
        assert f"/{path}" in shown


def test_an_owner_photo_renders_from_the_bundle_without_a_label(library):
    library.execute("INSERT INTO business (id, claim_state) VALUES ('biz_render', 'draft')")
    page_id = db.new_id("pg")
    library.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, html, status) "
        "VALUES (%s, 'biz_render', 'hosted', '', NULL, '', 'preview')",
        (page_id,),
    )
    photo = jpeg(1600, 1067, 120)
    library.execute(
        "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height) "
        "VALUES ('ph_owner', %s, %s, 'image/jpeg', 1600, 1067)",
        (page_id, photo),
    )
    s = roofer("solid").model_copy(
        update={"images": {"hero": ImageRef(source="owner", id="ph_owner", alt="Our crew on a roof in Matthews")}}
    )
    resolved = images.fill(library, s, business_id="biz_render", page_id=page_id)
    assert resolved["hero"].source == "owner" and resolved["hero"].bytes == photo and not resolved["hero"].label

    rendered = build(library, s, photos_of=page_id)
    path = images.path(hashlib.sha256(photo).hexdigest(), ".jpg")

    assert re.fullmatch(r"assets/img/[0-9a-f]{16}\.jpg", path)
    assert rendered.files[path] == photo and path not in rendered.refs
    home = Doc(html_pages(rendered)["index.html"])
    hero = next(f for f in home.figures if f["attrs"].get("data-slot") == "hero")
    assert hero["attrs"]["data-source"] == "owner" and LABEL not in hero["text"]
    assert hero["imgs"][0]["src"] == f"/{path}" and hero["imgs"][0]["alt"] == "Our crew on a roof in Matthews"
    assert checked(rendered) == []


# ---------------------------------------------------------------- the owner's logo (A3p-6 batch 2 Task 2, Q8, Q10)


def _between(page: str, start: str, end: str) -> str:
    return page[page.index(start) : page.index(end)]


def _logos(part: str) -> list[dict]:
    return [a for a in Doc(part).all("img") if "brand-logo" in a.get("class", "").split()]


def _with_logo(conn, style: str = "solid"):
    """The roofer in `style` naming a PNG logo of its own page: (the document, the page's id, the logo's bytes)."""
    page_id = make_page(conn)
    png = add_photo(conn, page_id, "ph_logo", logo_png(), kind="logo", content_type="image/png")
    s = roofer(style).model_copy(update={"images": {"logo": ImageRef(source="owner", id="ph_logo")}})
    return s, page_id, png


@pytest.mark.parametrize("style", STYLES)
def test_an_owner_logo_stands_in_the_header_of_every_page_and_the_footer_keeps_the_name(library, style):
    s, page_id, png = _with_logo(library, style)
    rendered = build(library, s, photos_of=page_id)
    path = images.path(hashlib.sha256(png).hexdigest(), ".png")

    assert re.fullmatch(r"assets/img/[0-9a-f]{16}\.png", path)
    assert rendered.files[path] == png and path not in rendered.refs  # the owner's bytes, in the bundle
    assert checked(rendered) == []  # the static checks read the PNG as an image
    for name, page in html_pages(rendered).items():
        header, footer = _between(page, "<header", "</header>"), _between(page, "<footer", "</footer>")
        assert page.count('<img class="brand-logo"') == 1, name  # the header's, nothing else (the footer is dark)
        assert 'data-slot="logo"' not in page, name  # no figure: it is the brand, not one of the page's photos
        assert LABEL not in header, name  # the owner's own: never "Replace with your photo"
        (img,) = _logos(header)
        assert img["src"] == f"/{path}" and img["srcset"] == f"/{path} 400w", name
        assert (img["width"], img["height"]) == ("400", "100"), name
        assert img["alt"] == "Oakline Roofing", name  # the business's name, for a screen reader
        assert img["sizes"] == "200px", name  # a srcset in widths comes with the width it is shown at
        assert "loading" not in img, name  # on the first screen of every page: never lazy
        head = Doc(header)
        brand = next(a for a in head.all("a") if a.get("class") == "brand")
        assert brand["href"] == "/" and head.link_text[id(brand)].strip() == "", name  # a link home holding the logo
        assert _logos(footer) == [] and '<p class="brand">Oakline Roofing</p>' in footer, name  # the name, as before


@pytest.mark.parametrize("style", STYLES)
def test_without_a_logo_the_header_and_the_footer_keep_the_businesss_name(library, style):
    for name, page in html_pages(build(library, roofer(style))).items():
        assert "brand-logo" not in page, name
        assert '<a class="brand" href="/">Oakline Roofing</a>' in _between(page, "<header", "</header>"), name
        assert '<p class="brand">Oakline Roofing</p>' in _between(page, "<footer", "</footer>"), name


# The looks whose header stands on a light ground: the logo -- one file, drawn for a light ground -- shows there and
# nowhere else (ruling of A3p-6 batch 2 Task 2 fix round 0): Boutique's header is dark, so it keeps its wordmark.
LOGO_LOOKS = {
    ("home_services", "solid"),
    ("home_services", "clean"),
    ("home_services", "bold"),
    ("dental", "calm"),
    ("dental", "warm"),
}


def test_the_logo_is_shown_in_the_looks_whose_header_is_light_and_in_no_other():
    every = {(fid, name): st for fid in sorted(families.FAMILIES) for name, st in families.get(fid).styles.items()}
    assert {look for look, st in every.items() if render.header_is_light(st)} == LOGO_LOOKS
    for look, st in every.items():  # the rule is the stylesheet's own: a dark header is the one `.header` paints dark
        dark_in_css = ".header{background:var(--dark)" in render.css(st)
        assert dark_in_css == (look not in LOGO_LOOKS), look


def test_a_logo_goes_into_the_bundle_as_the_type_it_was_stored_in(library):
    page_id = make_page(library)
    stored = {
        "ph_png": (add_photo(library, page_id, "ph_png", logo_png(), kind="logo", content_type="image/png"), ".png"),
        "ph_jpg": (add_photo(library, page_id, "ph_jpg", jpeg(600, 150, 90), kind="logo"), ".jpg"),
    }
    for photo_id, (data, ext) in stored.items():
        s = roofer("clean").model_copy(update={"images": {"logo": ImageRef(source="owner", id=photo_id)}})
        resolved = images.fill(library, s, business_id="biz_render", page_id=page_id)["logo"]
        assert (resolved.source, resolved.label, resolved.ext) == ("owner", False, ext)
        rendered = build(library, s, photos_of=page_id)
        path = images.path(hashlib.sha256(data).hexdigest(), ext)
        assert rendered.files[path] == data and checked(rendered) == []
        (img,) = _logos(_between(html_pages(rendered)["index.html"], "<header", "</header>"))
        assert img["src"] == f"/{path}"
    # a photo says what it is: the stored type is the file's, a stock photo is always WebP
    assert images.fill(library, roofer("solid"), business_id="biz_render", page_id=PAGE)["hero"].ext == ".webp"
    assert images.Resolved("owner", "", {1: "a"}, 1, 1, False, b"x").content_type == "image/jpeg"


def test_the_logo_slot_takes_the_owners_photo_of_this_page_alone(library):
    asset = library.execute("SELECT id FROM site_asset WHERE NOT people AND reviewed_at IS NOT NULL").fetchone()["id"]
    other = make_page(library, "biz_other")
    add_photo(library, other, "ph_theirs", logo_png(), kind="logo", content_type="image/png")
    for ref in (
        ImageRef(source="stock", id=asset),  # a stock photo is never a business's logo
        ImageRef(source="ai", id="ai_1"),  # nor one nobody has drawn
        ImageRef(source="owner", id="ph_theirs"),  # nor another page's
    ):
        s = roofer("solid").model_copy(update={"images": {"logo": ref}})
        assert "logo" not in images.fill(library, s, business_id="biz_render", page_id=PAGE), ref
        assert "brand-logo" not in html_pages(build(library, s))["index.html"], ref
    assert "logo" not in images.fill(library, roofer("solid"), business_id="biz_render", page_id=PAGE)  # none to lend


def test_a_logo_neither_wears_the_label_nor_takes_it_off_the_stock_photos(library):
    s, page_id, _png = _with_logo(library)
    shown = html_pages(build(library, s, photos_of=page_id, preview=True))["index.html"]
    labelled = [f for f in Doc(shown).figures if LABEL in f["text"]]
    assert labelled and all(f["attrs"].get("data-source") == "stock" for f in labelled)  # the others, as before
    live = html_pages(build(library, s, photos_of=page_id, preview=False))["index.html"]
    assert LABEL not in live and "brand-logo" in live


def test_the_logo_is_40_px_tall_and_48_from_1024_px_and_the_header_keeps_its_44_px_minimum():
    for family_id in sorted(families.FAMILIES):
        for name, style in families.get(family_id).styles.items():
            css = render.css(style)
            at = css.index("@media (min-width:1024px){\n")  # the desktop block (not the centred hero's one-liner)
            desktop = css[at : css.index("@media (prefers-reduced-motion")]
            assert ".brand-logo{height:40px;width:auto;max-width:200px;object-fit:contain;display:block}" in css[:at]
            assert ".brand-logo{height:48px}" in desktop and ".brand-logo{height:48px}" not in css[:at], name
            assert re.search(r"\.header \.brand\{[^}]*min-height:44px", css), name
            # a wide logo gives way on a narrow phone instead of pushing the menu off the screen; a name is as it was
            assert ".brand-logo{min-width:0}" in css[:at] and ".header .brand:has(.brand-logo){min-width:0}" in css[:at]
            assert not re.search(r"\.header \.brand\{[^}]*min-width", css), name


# ---------------------------------------------------------------- fonts, the stylesheet, the script, the budget


@pytest.mark.parametrize("style", STYLES)
def test_the_styles_fonts_ship_in_the_bundle_and_the_font_faces_name_them(library, style):
    rendered = build(library, roofer(style))
    st = FAMILY.styles[style]
    fonts = {p: b for p, b in rendered.files.items() if p.startswith("assets/fonts/")}
    css = next(b.decode() for p, b in rendered.files.items() if re.fullmatch(r"assets/site-[0-9a-f]{12,}\.css", p))

    assert len(fonts) == len({f.file for f in st.fonts}) and fonts == render.fonts(st)
    for path, data in fonts.items():
        assert path.endswith(".woff2") and data[:4] == b"wOF2", path
        assert f"url({path.removeprefix('assets/')})" in css, path
    assert css.count("@font-face") == len(fonts)
    assert "font-display:swap" in css.replace(" ", "")
    for face in {f.face for f in st.fonts}:
        assert f'font-family:"{face}"' in css.replace(": ", ":"), face


@pytest.mark.parametrize("style", STYLES)
def test_the_stylesheet_carries_the_styles_accent_and_radii(library, style):
    st = FAMILY.styles[style]
    css = render.css(st)

    assert f"--accent:{st.accent}" in css and f"--on-accent:{st.on_accent}" in css
    for name, value in st.radii.items():
        assert f"--r-{name}:{value}px" in css, name
    for token in ("ink", "text", "tint", "dark"):
        assert f"--{token}:{st.tokens[token]}" in css, token
    for width in ("767px", "768px", "1023px", "1024px"):
        assert width in css
    assert "prefers-reduced-motion" in css
    assert "transition" not in css and "animation" not in css and "@keyframes" not in css
    picked = render.css(st, accent=st.accent_options[1])
    assert f"--accent:{st.accent_options[1]}" in picked
    rendered = build(library, roofer(style).model_copy(update={"accent": st.accent_options[1]}))
    sheet = next(b.decode() for p, b in rendered.files.items() if p.endswith(".css"))
    assert f"--accent:{st.accent_options[1]}" in sheet


def test_every_asset_name_carries_its_hash(library):
    rendered = build(library, roofer("bold"))

    for path in [*rendered.files, *rendered.refs]:
        if path.endswith(".html"):
            continue
        assert path.startswith("assets/") and bundles.hashed(path), path
    assert not any(p == "robots.txt" for p in rendered.files)


def test_the_script_only_closes_the_menu_and_hides_the_bar_while_typing(library):
    js = next(b.decode() for p, b in build(library, roofer("solid")).files.items() if p.endswith(".js"))

    assert "Escape" in js and "focusin" in js and "focusout" in js
    for banned in ("location", "open(", "fetch", "XMLHttpRequest", "sendBeacon", "innerHTML", "eval"):
        assert banned not in js, banned


def _weights(conn, rendered) -> dict:
    """Q17's numbers, per page: HTML, the stylesheet, the script, the fonts, and the smallest variant of every image
    the page shows."""
    css = sum(len(b) for p, b in rendered.files.items() if p.endswith(".css"))
    js = sum(len(b) for p, b in rendered.files.items() if p.endswith(".js"))
    fonts = sum(len(b) for p, b in rendered.files.items() if p.startswith("assets/fonts/"))

    def size(path: str) -> int:
        if path in rendered.files:
            return len(rendered.files[path])
        row = conn.execute("SELECT size FROM site_blob WHERE sha256 = %s", (rendered.refs[path],)).fetchone()
        return row["size"]

    pages = {}
    for path, page in html_pages(rendered).items():
        smallest = 0
        for img in Doc(page).all("img"):
            choices = [part.strip().split() for part in img["srcset"].split(",")]
            src = min(choices, key=lambda c: int(c[1].rstrip("w")))[0]
            smallest += size(src.lstrip("/"))
        html_bytes = len(page.encode())
        pages[path] = {"html": html_bytes, "total": html_bytes + css + js + fonts + smallest}
    return {"css": css, "js": js, "fonts": fonts, "pages": pages}


@pytest.mark.parametrize("style", STYLES)
def test_the_page_weight_budget_holds(library, style):
    s = roofer(style)
    rendered = build(library, s)
    w = _weights(library, rendered)

    assert w["css"] <= 40 * 1024 and w["js"] <= 8 * 1024 and w["fonts"] <= 220 * 1024, w
    for path, p in w["pages"].items():
        assert p["html"] <= 150 * 1024, (path, p)
        assert p["total"] <= 1200 * 1024, (path, p)
    for slot, r in images.fill(library, s, business_id="biz_render", page_id=PAGE).items():
        for width, sha in r.variants.items():
            n = library.execute("SELECT size FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()["size"]
            assert n <= 350 * 1024, (slot, width)
            if slot == "hero" and width == 1024:
                assert n <= 200 * 1024


# ---------------------------------------------------------------- facts decide the sections


@pytest.mark.parametrize("style", STYLES)
def test_a_business_with_thin_facts_leaves_out_what_it_has_no_facts_for(library, style):
    s = thin(style)
    for preview in (True, False):
        rendered = build(library, s, preview=preview)
        assert checked(rendered) == [], preview
        pages = html_pages(rendered)
        home = pages["index.html"]
        assert len(text_of(home)) >= hosted.MIN_TEXT_CHARS
        sections = Doc(home).sections()
        for gone in ("trust", "reviews", "emergency", "picker"):
            assert gone not in sections, (gone, preview)
        if not preview:
            assert "work" not in sections  # the gallery is the owner's work: stock stands in for it in a preview only
        for page in pages.values():
            text = text_of(page).lower()  # every guard ignores case: "Licensed, insured" would pass a "License" one
            assert "licen" not in page.lower() and "years" not in text.split()
            assert "warranty" not in page.lower() and "insur" not in text and not re.search(r"\bfree\b", text)
            assert 'data-section="trust"' not in page and 'data-section="reviews"' not in page
            assert "open saturdays" not in text
            assert "[" not in text  # no placeholder of any kind
            assert "<h2></h2>" not in page and "<h2>\n" not in page
        assert "Read our reviews on Google" not in home


def test_the_roofer_has_every_section_it_has_facts_for(library):
    home = html_pages(build(library, roofer("solid")))["index.html"]
    sections = Doc(home).sections()

    for kept in ("hero", "trust", "picker", "services", "why", "reviews", "work", "how", "area", "faq", "cta"):
        assert kept in sections, kept
    assert "emergency" not in sections  # the roofer offers none
    order = [
        x
        for x in sections
        if x in ("hero", "trust", "picker", "services", "why", "reviews", "work", "how", "area", "faq", "cta")
    ]
    assert order == ["hero", "trust", "picker", "services", "why", "reviews", "work", "how", "area", "faq", "cta"]
    text = text_of(home)
    for fact in ("NC License #00000", "15 years", "Insured", "Open Saturdays", "10-year workmanship warranty"):
        assert fact in text, fact


def test_the_emergency_band_only_with_an_emergency_line(library):
    home = html_pages(build(library, roofer("bold", emergency="Emergency tarping after a storm")))["index.html"]
    doc = Doc(home)

    assert doc.sections()[:2] == ["hero", "emergency"]
    assert "Emergency tarping after a storm" in text_of(home)


def test_markup_in_the_facts_is_shown_as_text(library):
    s = roofer(
        "solid",
        name="Oakline & Sons <Roofing>",
        reviews=[Review(text="<script>alert(1)</script> Great crew & fair", first_name="<b>Ann</b>", source="Google")],
    )
    rendered = build(library, s)

    assert checked(rendered) == []
    for path, page in html_pages(rendered).items():
        assert "<Roofing>" not in page and "<script>alert" not in page and "<b>Ann" not in page, path
        assert "Oakline &amp; Sons &lt;Roofing&gt;" in page, path
        assert "Oakline & Sons <Roofing>" in text_of(page)
    home = html_pages(rendered)["index.html"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Great crew &amp; fair" in home
    ld = json.loads(next(b for a, b in Doc(home).scripts if a.get("type") == "application/ld+json"))
    assert ld["name"] == "Oakline & Sons <Roofing>"


def test_the_problem_picker_lists_what_the_services_match(library):
    home = html_pages(build(library, roofer("bold")))["index.html"]
    picker = home[home.index('data-section="picker"') :]
    picker = picker[: picker.index("</section>")]

    assert "<h2>What's going on?</h2>" in picker
    items = re.findall(r"<a [^>]*>([^<]*)</a>", picker)
    assert [i.replace("&#39;", "'") for i in items] == [
        "It's leaking",
        "Shingles are missing",
        "A storm hit it",
        "It's getting old",
        "Not sure",
    ]
    assert 'href="/services/roof-repair/"' in picker and 'href="/services/storm-damage/"' in picker
    assert 'href="/contact/"' in picker
    # one item matched is no picker at all (Q20: fewer than two)
    one = html_pages(build(library, thin("bold")))["index.html"]
    assert 'data-section="picker"' not in one and "going on?" not in one


def test_the_licence_line_only_with_a_licence(library):
    with_licence = html_pages(build(library, roofer("clean")))
    without = html_pages(build(library, roofer("clean", licence=None)))

    assert all("NC License #00000" in p for p in with_licence.values())  # the footer carries it on every page
    assert not any("NC License" in p for p in without.values())


def test_the_structured_data(library):
    s = roofer("solid")
    for hostname in (None, "oakline-roofing.admiry.ai"):
        pages = html_pages(build(library, s, hostname=hostname))
        for path, page in pages.items():
            blocks = [b for a, b in Doc(page).scripts if a.get("type") == "application/ld+json"]
            assert len(blocks) == 1, path
            ld = json.loads(blocks[0])
            assert ld["@context"] == "https://schema.org" and ld["@type"] == "RoofingContractor"
            assert ld["name"] == "Oakline Roofing" and ld["telephone"] == "+17045550142"
            assert [a["name"] for a in ld["areaServed"]] == s.facts.service_area
            assert "address" not in ld and "openingHours" not in ld
            if hostname:
                assert ld["url"] == "https://oakline-roofing.admiry.ai/"
            else:
                assert "url" not in ld
        if hostname:
            assert '<link rel="canonical" href="https://oakline-roofing.admiry.ai/about/">' in pages["about/index.html"]
        else:
            assert 'rel="canonical"' not in pages["index.html"]


def test_the_404_page(library):
    rendered = build(library, roofer("clean"))
    page = html_pages(rendered)["404.html"]
    doc = Doc(page)

    assert doc.h1 == ["That page isn't here."]
    assert any(a.get("href") == "/" and "Back to the home page" in page for a in doc.all("a"))
    assert doc.all("header") and SNIPPET in page
    assert "404.html" not in rendered.pages


@pytest.mark.parametrize("style", STYLES)
def test_the_visible_text_has_no_jargon(library, style):
    for path, page in html_pages(build(library, roofer(style))).items():
        assert not JARGON.search(text_of(page)), (path, JARGON.search(text_of(page)))


def test_the_template_words_are_the_plans(library):
    pages = html_pages(build(library, roofer("solid")))
    home, contact = pages["index.html"], pages["contact/index.html"]

    for words in (
        "Home",
        "Services",
        "About",
        "Contact",
        "Get a quote",
        "Call (704) 555-0142",
        "Privacy notice",
        "Terms of use",
    ):
        assert words in text_of(home), words
    for words in ("Call or text", "Hours", "Where we work", "Get directions"):
        assert words in text_of(contact), words
    assert "Find us" not in text_of(contact)
    assert "Related services" in text_of(pages["services/roof-repair/index.html"])
    assert 'href="https://maps.google.com/?q=Charlotte%2C+NC"' in contact


def test_images_path_names_a_blob_by_its_hash():
    sha = hashlib.sha256(b"x").hexdigest()
    assert images.path(sha) == f"assets/img/{sha[:16]}.webp"
    assert images.path(sha, ".jpg") == f"assets/img/{sha[:16]}.jpg"


def test_a_google_profile_and_an_email_render_as_links(library):
    rendered = build(library, roofer("solid", gbp_url=GBP, email=EMAIL))
    pages = html_pages(rendered)

    assert checked(rendered) == []
    for path, page in pages.items():
        for url in Doc(page).urls():
            assert _allowed(url, ""), (path, url)
        assert f'href="mailto:{EMAIL}"' in page, path
    assert f'href="{GBP}"' in pages["index.html"] and "Read our reviews on Google" in pages["index.html"]
    assert f'href="{GBP}"' in pages["contact/index.html"]


def test_a_service_block_id_never_takes_a_fixed_id(library):
    s = roofer("solid")
    gutters = next(x for x in s.facts.services if x.slug == "gutters")
    s = s.model_copy(
        update={
            "facts": s.facts.model_copy(
                update={
                    "services": [*s.facts.services, gutters.model_copy(update={"slug": "quote", "name": "Quote help"})]
                }
            )
        }
    )
    pages = html_pages(build(library, spec.validate(s)))
    services = pages["services/index.html"]

    ids = re.findall(r'\sid="([^"]+)"', services)
    assert len(ids) == len(set(ids)), ids
    assert 'id="svc-quote"' in services and 'id="svc-gutters"' in services
    assert 'href="/services/#svc-gutters"' in pages["index.html"]


def test_an_owner_ref_to_another_pages_photo_is_never_rendered(library):
    library.execute("INSERT INTO business (id, claim_state) VALUES ('biz_other', 'draft')")
    other = db.new_id("pg")
    library.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, html, status) "
        "VALUES (%s, 'biz_other', 'hosted', '', NULL, '', 'preview')",
        (other,),
    )
    photo = jpeg(1600, 1067, 33)
    library.execute(
        "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height) "
        "VALUES ('ph_theirs', %s, %s, 'image/jpeg', 1600, 1067)",
        (other, photo),
    )
    s = roofer("solid").model_copy(
        update={
            "images": {
                "hero": ImageRef(source="owner", id="ph_theirs"),
                "team": ImageRef(source="owner", id="ph_theirs"),
            }
        }
    )
    resolved = images.fill(library, s, business_id="biz_render", page_id=PAGE)

    assert resolved["hero"].source == "stock" and resolved["hero"].bytes is None  # as for any empty slot
    assert "team" not in resolved  # a people slot stays empty
    rendered = build(library, s)
    assert photo not in rendered.files.values()
    assert images.path(hashlib.sha256(photo).hexdigest(), ".jpg") not in "".join(html_pages(rendered).values())


def test_fill_needs_the_business_and_the_page():
    with pytest.raises(TypeError):
        images.fill(None, roofer("solid"))  # type: ignore[call-arg]


def test_boutiques_menu_panel_has_its_own_focus_ring(library):
    from agent.sites import families

    css = render.css(families.get("dental").styles["boutique"])
    assert ".header.menu-panel:focus-visible{outline-color:var(--ink)}" in css.replace(" ", "")
    assert ".menu-panel:focus-visible" not in render.css(families.get("dental").styles["calm"])


def test_the_site_shows_the_hours_as_the_words_wrote_them(conn, library):
    """2026-10-08: the words' tidy hours, where the site shows hours; the owner's own lines without them."""
    s = roofer(hours=["9am-5pm Monday to Friday"])
    tidy = s.model_copy(update={"words": s.words.model_copy(update={"hours": ["Monday to Friday, 9 am to 5 pm"]})})
    contact = html_pages(build(conn, tidy))["contact/index.html"]
    assert "Monday to Friday, 9 am to 5 pm" in contact and "9am-5pm Monday to Friday" not in contact
    plain = html_pages(build(conn, s))["contact/index.html"]
    assert "9am-5pm Monday to Friday" in plain


def test_a_trades_hero_falls_back_to_the_curated_heroes_when_the_library_has_none_of_its_trade(conn, library):
    """This library has no heating-and-cooling photo: the hvac sample's hero is one of the family's heroes, never empty."""
    s = spec.validate(samples.load("hvac", style="solid"))
    filled = images.fill(conn, s, business_id="biz_render", page_id=PAGE)
    assert "hero" in filled
