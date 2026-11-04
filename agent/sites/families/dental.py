"""Dental (a general practice): three styles over one structure (plan Q2, Q3, Q12; spec T8: never "specialist")."""

from __future__ import annotations

from . import Family, FontFile, Form, Style

CALM = Style(
    display_font="Plus Jakarta Sans",
    body_font="Plus Jakarta Sans",
    fonts=(FontFile("Plus Jakarta Sans", "plus-jakarta-sans/PlusJakartaSans-Variable.woff2", "400 800"),),
    tokens={
        "ground": "#FFFFFF",
        "ink": "#10302B",
        "text": "#3F5752",
        "muted": "#0B5550",
        "line": "#E2ECE9",
        "line_alt": "#CFE0DB",
        "tint": "#EEF6F4",
        "tint2": "#D3E6E1",
        "dark": "#1F4A44",
        "on_dark": "#E2ECE9",
        "photo": "#C9DBD6",
    },
    radii={"button": 10, "card": 16, "image": 22, "badge": 999},
    hero="split-tinted",
    topbar=False,
    trust="pills",
    on_accent="#FFFFFF",
    accent="#0F766E",
    accent_options=("#0F766E", "#1D4ED8", "#6D28D9", "#0F172A"),
    features=("badge-accepting-new-patients", "emergency-band-above-hero-on-phone"),
)

WARM = Style(
    display_font="Figtree",
    body_font="Figtree",
    fonts=(FontFile("Figtree", "figtree/Figtree-Variable.woff2", "400 800"),),
    tokens={
        "ground": "#FFFFFF",
        "ink": "#2E2340",
        "text": "#5A4C6B",
        "muted": "#8A3A26",
        "line": "#F3E1D7",
        "line_alt": "#F6DCCD",
        "tint": "#FFF3EC",
        "tint2": "#FFF8F4",
        "dark": "#3B2E50",
        "on_dark": "#FFF3EC",
        "photo": "#F3E1D7",
    },
    radii={"button": 999, "card": 22, "image": 28, "input": 14},
    hero="centred",  # the photo full width under the words
    topbar=False,
    trust="pills",
    on_accent="#FFFFFF",
    accent="#B4432A",
    accent_options=("#B4432A", "#7C3AED", "#0F766E", "#1D4ED8"),
)

BOUTIQUE = Style(
    display_font="Cormorant Garamond",
    body_font="Jost",
    fonts=(
        FontFile("Cormorant Garamond", "cormorant-garamond/CormorantGaramond-Medium.woff2", "500"),
        FontFile("Cormorant Garamond", "cormorant-garamond/CormorantGaramond-SemiBold.woff2", "600"),
        FontFile("Cormorant Garamond", "cormorant-garamond/CormorantGaramond-Bold.woff2", "700"),
        FontFile("Jost", "jost/Jost-Variable.woff2", "400 600"),
    ),
    tokens={
        "ground": "#F5F6F4",
        "ink": "#1B2421",
        "text": "#56625E",
        "muted": "#3C4844",
        "line": "#D3D8D2",
        "line_alt": "#DEE2DD",
        "line_alt2": "#E4E8E3",
        "tint": "#F5F6F4",
        "tint2": "#E7E4DA",
        "dark": "#0F1C19",
        "on_dark": "#E7E4DA",
        "photo": "#C9C6BC",
    },
    radii={"button": 2, "card": 2, "image": 2},
    hero="dark-split",
    topbar=False,
    trust="pills",
    on_accent="#0F1C19",
    accent="#C9A86A",
    accent_options=("#C9A86A", "#D4D4D4", "#E8B4A0", "#9CC5B4"),
    accent_text_on=("dark",),  # the spaced uppercase line under the wordmark and the eyebrow
    features=("two-line-wordmark", "spaced-uppercase-eyebrow"),
)


def slots(spec) -> dict[str, str]:
    """Q15: hero, two office photos (the reception and the outside), a treatment photo for each service (aligners
    for a slug naming Invisalign or aligners) and the contact page (the outside)."""
    out = {"hero": "hero", "office-1": "reception", "office-2": "exterior", "contact": "exterior"}
    for s in spec.facts.services:
        aligners = "aligner" in s.slug or "invisalign" in s.slug
        out[f"service-{s.slug}"] = s.stock_slot or ("aligners" if aligners else "service-treatment")
    return out


FAMILY = Family(
    id="dental",
    trades=("dentist",),
    styles={"calm": CALM, "warm": WARM, "boutique": BOUTIQUE},
    key_photo_slot="office-1",
    form=Form(
        intent="appointment",
        fields=("name", "phone", "patient", "service", "preferred_time"),
        button="Request an appointment",
        note="The practice calls you back during business hours.",
    ),
    banned=(
        "guaranteed",
        "guarantee",
        "best",
        "#1",
        "cheapest",
        "lowest price",
        "specialist",
        "specialists",
        "painless",
        "pain-free",
    ),
    problems={},
    schema_type={"dentist": "Dentist"},
    slots=slots,
)
