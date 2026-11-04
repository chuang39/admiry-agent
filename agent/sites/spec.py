"""The industry site's versioned document (A3p-6 batch 1 Q1; spec T19, T5, T8, T11, T15): family, style, the owner's
facts, the words, the images by slot. The model never writes HTML into it; the templates render it. The models accept
anything of the right shape, so a half-made document can be held and shown; `validate` is the one gate, and each
refusal is one ToolError sentence."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .. import landing, page_builder
from ..models import SiteWords
from . import families

MAX_LEAD_SERVICES = 6  # a page each: with home, about, services and contact the renderer's 10 pages
MIN_PHONE_DIGITS = 10
_ACCENT = re.compile(r"^#[0-9A-Fa-f]{6}$")
MIN_BUTTON_RATIO = 4.5
GOOGLE_HOSTS = frozenset({"google.com", "g.page", "maps.app.goo.gl"})  # and any subdomain of google.com


class Service(BaseModel):
    slug: str  # lowercase letters, digits and hyphens: the service page's path services/<slug>/
    name: str
    lead: bool = False  # a page of its own
    summary: str = ""
    includes: list[str] = Field(default_factory=list)
    signs: list[str] = Field(default_factory=list)
    price_line: str | None = None
    stock_slot: str | None = None  # the stock kind to fill this service's photo from, when not the family's default


class Review(BaseModel):
    text: str
    first_name: str
    source: str
    when: str | None = None


class Dentist(BaseModel):
    name: str
    credentials: str | None = None
    school: str | None = None
    years: int | None = None
    note: str | None = None


class ImageRef(BaseModel):
    source: Literal["owner", "stock", "ai"]
    id: str
    alt: str = ""
    label: bool = False


class Facts(BaseModel):
    """What the owner told Admiry. Every optional fact is None or empty by default, and a line or a section that
    needs one is left out without it (T5, T8)."""

    name: str
    phone: str = ""
    email: str | None = None
    address: str | None = None
    city: str = ""
    state: str | None = None
    service_area: list[str] = Field(default_factory=list)
    storefront: bool = False
    hours: list[str] = Field(default_factory=list)
    about: str | None = None
    services: list[Service] = Field(default_factory=list)
    social: dict[str, str] = Field(default_factory=dict)
    gbp_url: str | None = None
    place_id: str | None = None
    rating: str | None = None
    licence: str | None = None
    years: int | None = None
    warranty: str | None = None
    insured: bool = False
    free_estimate: str | None = None
    emergency: str | None = None
    financing: str | None = None
    languages: list[str] = Field(default_factory=list)
    dentist: Dentist | None = None
    new_patients: str | None = None
    insurance_plans: list[str] = Field(default_factory=list)
    in_office_plan: str | None = None
    emergency_line: str | None = None
    reviews: list[Review] = Field(default_factory=list)


class SiteSpec(BaseModel):
    family: str
    style: str
    trade: str | None = None
    accent: str | None = None
    facts: Facts
    words: SiteWords | None = None
    images: dict[str, ImageRef] = Field(default_factory=dict)  # by slot: hero, about, gallery-1..3, service-<slug>, …
    checklist: dict = Field(default_factory=dict)  # empty in batch 1 (batch 2 fills it, T12)
    version: int = 1


# ---------------------------------------------------------------- colour


def _channel(c: int) -> float:
    v = c / 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def exact_contrast(a: str, b: str) -> float:
    """WCAG's contrast ratio of two #RRGGBB colours, 1 to 21, unrounded: what every threshold compares."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def contrast(a: str, b: str) -> float:
    """The same ratio to four decimals, for showing."""
    return round(exact_contrast(a, b), 4)


# ---------------------------------------------------------------- the gate


def validate(spec: SiteSpec) -> SiteSpec:
    """The document as it may be rendered (accent upper-cased), or one ToolError sentence for the first thing wrong."""
    if spec.family not in families.FAMILIES:
        raise ToolError(f"There is no site family called “{spec.family}”; use home_services or dental.")
    fam = families.get(spec.family)
    if spec.style not in fam.styles:
        raise ToolError(f"The {spec.family} family has no style called “{spec.style}”; use {', '.join(fam.styles)}.")
    if spec.trade is not None and spec.trade not in fam.trades:
        raise ToolError(f"The {spec.family} family has no trade called “{spec.trade}”; use {', '.join(fam.trades)}.")
    target = page_builder.tel(spec.facts.phone)
    if not target or len(re.sub(r"\D", "", target)) < MIN_PHONE_DIGITS:
        raise ToolError("The site needs the business's phone number with its area code, at least 10 digits.")
    seen: set[str] = set()
    for s in spec.facts.services:
        if not families.SLUG_RE.match(s.slug):
            raise ToolError(f"The service slug “{s.slug}” may use only lowercase letters, digits and hyphens.")
        if s.slug in seen:
            raise ToolError(f"The service slug “{s.slug}” is used twice; each service needs its own.")
        seen.add(s.slug)
    leads = sum(1 for s in spec.facts.services if s.lead)
    if leads < 1:
        raise ToolError("The site needs at least one lead service, the one that gets a page of its own.")
    if leads > MAX_LEAD_SERVICES:
        raise ToolError(f"Only {MAX_LEAD_SERVICES} services can have a page of their own; this has {leads}.")
    gbp = spec.facts.gbp_url
    if gbp is not None:
        if not gbp.startswith("https://"):
            raise ToolError("That Google profile link must start with https://.")
        host = (urlsplit(gbp).hostname or "").lower()
        if not (host in GOOGLE_HOSTS or host.endswith(".google.com")):
            raise ToolError("That Google profile link must be a Google address.")
    if spec.facts.email is not None and not landing._EMAIL.fullmatch(spec.facts.email.strip()):
        raise ToolError("That e-mail address does not look right; write it like name@example.com.")
    accent = accent_for(spec.family, spec.style, spec.accent) if spec.accent is not None else None
    return spec.model_copy(update={"accent": accent})


def accent_for(family: str, style: str | None, accent: str) -> str:
    """`accent` as a site may use it (upper-cased), or one ToolError sentence: written like #1D4ED8, and -- once the
    look is known (`style` one of the family's) -- dark or light enough for the words on its button."""
    if not _ACCENT.match(accent):
        raise ToolError("The accent colour must be written like #1D4ED8, six hexadecimal digits.")
    accent = accent.upper()
    if style is None:
        return accent
    on_accent = families.get(family).styles[style].on_accent
    if exact_contrast(on_accent, accent) < MIN_BUTTON_RATIO:
        if _luminance(accent) > _luminance(on_accent):  # the button's words are dark: the accent is too dark
            raise ToolError("That colour is too dark for the button's words; pick a lighter one.")
        raise ToolError("That colour is too light for the button's words; pick a darker one.")
    return accent


def pages(spec: SiteSpec) -> list[tuple[str, str, str | None]]:
    """(kind, path in the bundle, service slug) of every page: home, about, services, contact, then one for each
    lead service."""
    out: list[tuple[str, str, str | None]] = [
        ("home", "index.html", None),
        ("about", "about/index.html", None),
        ("services", "services/index.html", None),
        ("contact", "contact/index.html", None),
    ]
    out += [("service", f"services/{s.slug}/index.html", s.slug) for s in spec.facts.services if s.lead]
    return out


def service_url(spec: SiteSpec, hostname: str | None, slug: str) -> str:
    """Where a service page lives: on the site's host once it has one, else the path (an ad's final URL, batch 4)."""
    path = f"/services/{slug}/"
    return f"https://{hostname}{path}" if hostname else path
