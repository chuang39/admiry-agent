"""The industry site rendered into a bundle (A3p-6 batch 1 Q3-Q5, Q12, Q15-Q20, Q23, Q25, Q26; spec T4-T7, T20).

`bundle` renders a validated `SiteSpec` with Admiry's own templates (`templates/sites/`, autoescaped: the facts and the
model's words are untrusted text) into the files of an A3p-3 site bundle: the pages (home, about, services, contact,
one per lead service, and 404.html), the stylesheet rendered from the style's tokens, the one small script, the style's
self-hosted fonts, and the owner's photos (the logo, when the owner gave one, in the header of every page where the header
is light); the stock photos' sizes, already stored, are named in `refs`. Every asset name carries its content's hash
(`bundles.hashed`: served for a year), and every path the pages write starts with `base` (Q23: "" for a site,
"/roofer-solid" inside the gallery) except Admiry's notices (/privacy, /terms) and the agent's own addresses."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from .. import bundles, page_builder
from ..templating import env
from . import families
from .families import Family, FontFile, Style
from .images import Resolved, path
from .spec import SiteSpec, exact_contrast, pages

AGENT_DIR = Path(__file__).resolve().parent.parent
FONTS_DIR = AGENT_DIR / "data" / "sites" / "fonts"
SCRIPT = AGENT_DIR / "static" / "sites" / "site.js"

# Q19: the second action's words, per family -- the header and the hero, and the phone bar's shorter one
ACTIONS = {
    "home_services": {"header": "Get a quote", "bar": "Get a quote"},
    "dental": {"header": "Book an appointment", "bar": "Book"},
}
# Q12: each family's form words beyond the button and the note -- the labels, the reason's last choice, the times
FORM_WORDS = {
    "home_services": {
        "service_label": "What needs doing?",
        "other": None,
        "time_label": "When do you need it?",
        "times": ("Today", "This week", "Flexible"),
        "patient_label": None,
        "patients": (),
    },
    "dental": {
        "service_label": "What is the visit for?",
        "other": "Something else",
        "time_label": "Preferred time",
        "times": ("Morning", "Afternoon", "Any time"),
        "patient_label": "New or existing patient",
        "patients": ("New patient", "Existing patient"),
    },
}
# Q3: the pages that carry the form (dental's home ends with "request an appointment")
FORM_PAGES = {"home_services": ("contact", "service"), "dental": ("home", "contact", "service")}
# Boutique's two-line wordmark: the name, then this line (and the city) spaced in the accent
WORDMARK_LINE = {"dental": "Dentistry"}
HERO_SIZES = "100vw"
CARD_SIZES = "(min-width: 1024px) 33vw, (min-width: 768px) 50vw, 100vw"
FALLBACK_SCHEMA = {"home_services": "HomeAndConstructionBusiness", "dental": "Dentist"}
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class Rendered:
    """`files` the bundle's own bytes by path; `refs` the stock photos' paths -> the SHA-256 of a blob already in
    `site_blob` (Q9: never read again); `pages` the site's pages in `spec.pages` order (404.html is not one)."""

    files: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)


def _tag(digest: str, n: int) -> str:
    """The first `n` hex digits of `digest`, more when needed until `bundles.hashed` reads them as a hash (a part of
    only digits or only letters is not one)."""
    while n < len(digest) and not bundles.hashed(f"x-{digest[:n]}.x"):
        n += 1
    return digest[:n]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _font_file(f: FontFile) -> tuple[str, bytes]:
    data = (FONTS_DIR / f.file).read_bytes()
    stem = _SLUG.sub("-", Path(f.file).stem.lower()).strip("-")
    return f"assets/fonts/{stem}-{_tag(_sha(data), 8)}.woff2", data


def fonts(style: Style) -> dict[str, bytes]:
    """The style's self-hosted fonts as the bundle carries them: `assets/fonts/<face>-<sha8>.woff2`."""
    return dict(_font_file(f) for f in style.fonts)


def _font_faces(style: Style) -> list[dict]:
    """One @font-face per file: its family, its weight (a variable range or one weight), its name relative to the
    stylesheet (`assets/site-….css` -> `fonts/…`)."""
    return [
        {"family": f.face, "url": _font_file(f)[0].removeprefix("assets/"), "weight": f.weight} for f in style.fonts
    ]


def css(style: Style, accent: str | None = None) -> str:
    """The stylesheet: the style's tokens as custom properties on :root (`accent` the owner's pick when given), the
    @font-face rules naming the fonts relative to the stylesheet itself (so `base` needs nothing there), the
    components, and the blueprint's three widths (phone up to 767 px, tablet 768-1023, desktop 1024 and up)."""
    t = style.tokens
    return env.get_template("sites/site.css.j2").render(
        style=style,
        t=t,
        accent=(accent or style.accent).upper(),
        subtle=t["text"] if style.muted_on_dark else t["muted"],
        dark_card=t.get("dark_card", t["dark"]),
        eyebrow_on_dark=(accent or style.accent).upper() if "dark" in style.accent_text_on else t["on_dark"],
        faces=_font_faces(style),
        display=style.display_font,
        body=style.body_font,
    )


def _script() -> bytes:
    return SCRIPT.read_bytes()


# ---------------------------------------------------------------- what the templates read


def header_is_light(style: Style) -> bool:
    """Whether the header stands on a light ground -- the page's own, white in five of the six looks; the look with the
    two-line wordmark paints it with its dark one (the stylesheet's `.header` rule for that feature). The owner's logo
    is one file, drawn for a light ground as nearly every logo is, so it shows in a light header and nowhere else (A3p-6
    batch 2 ruling): not in the footer, which is dark in every look, and not in a dark header, which keeps its
    wordmark. Where the logo does not show, nothing of it is written into the bundle."""
    ground = style.tokens["dark"] if "two-line-wordmark" in style.features else style.tokens["ground"]
    return exact_contrast(ground, "#000000") > exact_contrast(ground, "#FFFFFF")


def _photo(r: Resolved | None, slot: str, *, base: str, preview: bool, fallback_alt: str) -> dict | None:
    if r is None:
        return None
    widths = sorted(r.variants)
    src_w = max([w for w in widths if w <= 1024] or widths[:1])
    return {
        "slot": slot,
        "source": r.source,
        "src": f"{base}/{path(r.variants[src_w], r.ext)}",
        "srcset": ", ".join(f"{base}/{path(r.variants[w], r.ext)} {w}w" for w in widths),
        "width": r.width,
        "height": r.height,
        "alt": r.alt or fallback_alt,
        "label": preview and r.label,
    }


def _words_match(keyword: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _picker(spec: SiteSpec, family: Family, services: list[dict]) -> list[dict]:
    """Q20: each of the trade's problems that one of the business's services answers (by a keyword in its name or
    slug, the lead services first), linked to that service; none at all when fewer than two match. The template adds
    "Not sure", to the contact page."""
    ordered = sorted(services, key=lambda s: not s["lead"])
    items = []
    for problem in family.problems.get(spec.trade or "", ()):
        for s in ordered:
            text = f"{s['name'].lower()} {s['slug']} {s['slug'].replace('-', ' ')}"
            if any(_words_match(k.lower(), text) for k in problem.keywords):
                items.append({"label": problem.label, "url": s["url"]})
                break
    return items if len(items) >= 2 else []


def _trust(spec: SiteSpec) -> list[dict]:
    """Q19: facts only -- the rating as given, the licence as given, "{n} years", "Insured", "Open Saturdays"."""
    f = spec.facts
    out = []
    if f.rating:
        out.append({"value": f.rating, "label": ""})
    if f.licence:
        out.append({"value": f.licence, "label": ""})
    if f.years:
        out.append({"value": str(f.years), "label": "years"})
    if f.insured:
        out.append({"value": "Insured", "label": ""})
    if any(re.match(r"\s*sat", h, re.IGNORECASE) and "closed" not in h.lower() for h in f.hours):
        out.append({"value": "Open Saturdays", "label": ""})
    return out


def _structured(spec: SiteSpec, family: Family, tel: str, towns: list[str], hostname: str | None) -> dict:
    """Q25: the business for search engines -- by trade, its name and phone, its address (a storefront) or the towns
    it serves, its social pages; no hours (the owner's lines are free text)."""
    f = spec.facts
    data: dict = {
        "@context": "https://schema.org",
        "@type": family.schema_type.get(spec.trade or "") or FALLBACK_SCHEMA.get(family.id, "LocalBusiness"),
        "name": f.name,
        "telephone": tel,
    }
    if hostname:
        data["url"] = f"https://{hostname}/"
    if f.storefront and f.address:
        data["address"] = {"@type": "PostalAddress", "streetAddress": f.address, "addressLocality": f.city}
        if f.state:
            data["address"]["addressRegion"] = f.state
    else:
        data["areaServed"] = [{"@type": "City", "name": t} for t in towns]
    if f.social:
        data["sameAs"] = list(f.social.values())
    return data


def _badge(style: Style, f, kind: str, dental: bool) -> str | None:
    """The hero's badge: home services the free estimate (home, contact, a service page); Calm's "Accepting new
    patients" on the home page when the practice said it welcomes new patients."""
    if dental:
        ok = "badge-accepting-new-patients" in style.features and f.new_patients and kind == "home"
        return "Accepting new patients" if ok else None
    return f.free_estimate if kind in ("home", "contact", "service") else None


def _title(head: str, name: str) -> str:
    """`page_builder.title_for`'s rule: the H1, and the business's name when the H1 does not already carry it."""
    return f"{head} | {name}" if name and name.lower() not in head.lower() else head


def bundle(
    spec: SiteSpec,
    *,
    page_id: str,
    agent_base: str,
    snippet: str,
    hostname: str | None,
    images: dict[str, Resolved],
    base: str = "",
    preview: bool = True,
) -> Rendered:
    """The site's files. `spec` is validated (`spec.validate`) and has its words; `images` is `images.fill`'s;
    `hostname` the site's own host when it has one (the canonical links and the structured data's url); `preview`
    prints "Replace with your photo" on every photo that is not the owner's (T12), never on a live site (T13)."""
    if spec.words is None:
        raise ValueError("the site has no words yet")
    if spec.words.hours:  # the hours as the words wrote them (2026-10-08), where the site shows the owner's lines
        spec = spec.model_copy(update={"facts": spec.facts.model_copy(update={"hours": list(spec.words.hours)})})
    family = families.get(spec.family)
    style = family.styles[spec.style]
    f, w = spec.facts, spec.words
    tel = page_builder.tel(f.phone) or ""
    base = base.rstrip("/")

    out = Rendered()
    font_files = fonts(style)
    sheet = css(style, spec.accent).encode("utf-8")
    script = _script()
    css_path = f"assets/site-{_tag(_sha(sheet), 12)}.css"
    js_path = f"assets/site-{_tag(_sha(script), 12)}.js"
    out.files.update({css_path: sheet, js_path: script, **font_files})

    def photo(slot: str) -> dict | None:
        return _photo(images.get(slot), slot, base=base, preview=preview, fallback_alt=f.name)

    services = [
        {
            "slug": s.slug,
            "name": s.name,
            "lead": s.lead,
            "summary": s.summary,
            "includes": s.includes,
            "signs": s.signs,
            "price_line": s.price_line,
            "url": f"{base}/services/{s.slug}/" if s.lead else f"{base}/services/#svc-{s.slug}",
            "photo": photo(f"service-{s.slug}"),
        }
        for s in f.services
    ]
    towns = f.service_area or ([f.city] if f.city else [])
    place = f.address if (f.storefront and f.address) else ", ".join(x for x in (f.city, f.state) if x)
    gallery = [p for p in (photo(f"gallery-{i}") for i in (1, 2, 3)) if p]
    show_work = [p for p in gallery if p["source"] == "owner" or preview]
    dental = family.id == "dental"

    def own_place(p: dict | None) -> dict | None:
        """A photo standing for the practice's own rooms or building: a stock one only in a preview (labelled), never
        on a live site, as for "recent work"."""
        return p if p and (p["source"] == "owner" or preview) else None

    office = [p for p in (own_place(photo(f"office-{i}")) for i in (1, 2)) if p]
    contact_photo = own_place(photo("contact")) if dental else photo("contact")
    actions = ACTIONS[family.id]
    site = {
        "spec": spec,
        "f": f,
        "w": w,
        "style": style,
        "family": family,
        "base": base,
        "tel": tel,
        "phone": f.phone.strip(),
        "actions": actions,
        "services": services,
        "lead_services": [s for s in services if s["lead"]],
        "picker": _picker(spec, family, services),
        "trust": _trust(spec),
        "towns": towns,
        "where": "Find us" if (f.storefront and f.address) else "Where we work",
        "place": place,
        "map_url": f"https://maps.google.com/?q={quote_plus(place)}" if place else None,
        "logo": photo("logo") if header_is_light(style) else None,
        "hero_photo": photo("hero"),
        "about_photo": photo("about"),
        "contact_photo": contact_photo,
        "team_photo": photo("team"),
        "dentist_photo": photo("dentist"),
        "work": show_work,
        "office": office,
        "urgent": f.emergency_line if dental else f.emergency,
        "wordmark_line": " · ".join(x for x in (WORDMARK_LINE.get(family.id, ""), f.city) if x),
        "form": {
            "action": f"{agent_base}/t/form/{page_id}",
            "intent": family.form.intent,
            "fields": list(family.form.fields),
            "honeypot": family.form.honeypot,
            "button": family.form.button,
            "note": family.form.note,
            **FORM_WORDS[family.id],
        },
        "css_path": css_path,
        "js_path": js_path,
        "ld": _structured(spec, family, tel, towns, hostname),
        "snippet": snippet,
        "hero_sizes": HERO_SIZES,
        "card_sizes": CARD_SIZES,
    }

    def canonical(at: str) -> str | None:
        return f"https://{hostname}{base}/{at.removesuffix('index.html')}" if hostname else None

    by_slug = {p.slug: p for p in w.service_pages}
    for kind, at, slug in pages(spec):
        ctx = {"kind": kind, "path": at, "canonical": canonical(at), "has_form": kind in FORM_PAGES[family.id]}
        if kind == "home":
            head, lead = w.home.headline, w.home.lead
        elif kind == "about":
            head, lead = w.about.headline, w.about.lead
        elif kind == "services":
            head, lead = w.services.headline, w.services.lead
        elif kind == "contact":
            head, lead = w.contact.headline, w.contact.lead
        else:
            svc = next(s for s in services if s["slug"] == slug)
            words = by_slug.get(slug)
            head = f"{svc['name']} in {f.city}" if f.city else svc["name"]
            lead = (words.lead if words else "") or svc["summary"]
            ctx.update(svc=svc, sw=words, related=[s for s in services if s["slug"] != slug])
        ctx.update(h1=head, lead=lead, title=_title(head, f.name), description=lead)
        ctx["quote_url"] = "#quote" if ctx["has_form"] else f"{base}/contact/#quote"
        ctx["badge"] = _badge(style, f, kind, dental)
        template = "home_dental" if (dental and kind == "home") else kind
        html = env.get_template(f"sites/{template}.html").render(**site, **ctx)
        out.files[at] = html.encode("utf-8")
        out.pages.append(at)
    missing = {
        "kind": "404",
        "path": "404.html",
        "canonical": None,
        "has_form": False,
        "h1": "That page isn't here.",
        "lead": "",
        "title": _title("That page isn't here.", f.name),
        "description": "",
        "quote_url": f"{base}/contact/#quote",
        "badge": None,
    }
    out.files["404.html"] = env.get_template("sites/404.html").render(**site, **missing).encode("utf-8")

    shown = "".join(b.decode("utf-8") for p, b in out.files.items() if p.endswith(".html"))
    for r in images.values():
        for sha in r.variants.values():
            at = path(sha, r.ext)
            if f"{base}/{at}" not in shown:
                continue
            if r.bytes is None:
                out.refs[at] = sha
            else:
                out.files[at] = r.bytes
    return out
