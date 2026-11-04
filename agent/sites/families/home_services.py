"""Home services (roofing, plumbing, HVAC, electrical): three styles over one structure (plan Q2, Q3, Q12, Q20)."""

from __future__ import annotations

from . import Family, FontFile, Form, Problem, Style

_PUBLIC = FontFile("Public Sans", "public-sans/PublicSans-Variable.woff2", "400 700")

SOLID = Style(
    display_font="Archivo",
    body_font="Public Sans",
    fonts=(FontFile("Archivo", "archivo/Archivo-Variable.woff2", "600 800"), _PUBLIC),
    tokens={
        "ground": "#FFFFFF",
        "ink": "#14213D",
        "text": "#1F2937",
        "muted": "#4B5563",
        "line": "#E5E7EB",
        "line_alt": "#D5DBE5",  # inputs
        "tint": "#F3F5F8",
        "tint2": "#E3E8F1",
        "dark": "#14213D",
        "on_dark": "#E3E8F1",
        "photo": "#D9E0EA",
    },
    radii={"button": 6, "card": 8, "image": 10, "pill": 999},
    hero="split-tinted",
    topbar=True,  # area, hours, phone
    trust="pills",
    on_accent="#FFFFFF",
    accent="#C2410C",
    accent_options=("#C2410C", "#1D4ED8", "#15803D", "#B91C1C"),
)

CLEAN = Style(
    display_font="Manrope",
    body_font="Manrope",
    fonts=(FontFile("Manrope", "manrope/Manrope-Variable.woff2", "400 800"),),
    tokens={
        "ground": "#FFFFFF",
        "ink": "#17211B",
        "text": "#4A5751",
        "muted": "#3C4A42",
        "line": "#CBD6CF",
        "line_alt": "#E3EAE6",
        "tint": "#F2F6F4",
        "tint2": "#EAF3EE",
        "dark": "#17211B",
        "dark_card": "#223029",
        "on_dark": "#E4ECE7",
        "photo": "#DDE7E1",
    },
    radii={"button": 999, "card": 16, "image": 24},
    hero="split-white",
    topbar=False,
    trust="stats",
    on_accent="#FFFFFF",
    accent="#1B6B45",
    accent_options=("#1B6B45", "#1D4ED8", "#9A3412", "#0F172A"),
    features=("status-badge", "reviews-on-dark"),
)

BOLD = Style(
    display_font="Barlow Condensed",
    body_font="Barlow",
    fonts=(
        FontFile("Barlow Condensed", "barlow-condensed/BarlowCondensed-Bold.woff2", "700"),
        FontFile("Barlow Condensed", "barlow-condensed/BarlowCondensed-ExtraBold.woff2", "800"),
        FontFile("Barlow", "barlow/Barlow-Regular.woff2", "400"),
        FontFile("Barlow", "barlow/Barlow-Medium.woff2", "500"),
        FontFile("Barlow", "barlow/Barlow-SemiBold.woff2", "600"),
        FontFile("Barlow", "barlow/Barlow-Bold.woff2", "700"),
    ),
    tokens={
        "ground": "#FFFFFF",
        "ink": "#111418",
        "text": "#3D434B",
        "muted": "#C9CDD3",  # on the dark ground only
        "line": "#3A414B",
        "line_alt": "#D9DCE1",
        "tint": "#F1F2F4",
        "tint2": "#D9DCE1",
        "dark": "#111418",
        "on_dark": "#C9CDD3",
        "photo": "#262B32",
    },
    radii={"button": 4, "card": 6, "image": 6},
    hero="dark-split",
    topbar=False,
    trust="band",  # an accent strip
    on_accent="#111418",
    accent="#F5C518",
    accent_options=("#F5C518", "#FB923C", "#4ADE80", "#60A5FA"),
    display_upper=True,
    muted_on_dark=True,
    accent_text_on=("dark",),
    features=("problem-picker-under-hero",),
)

TRADES = ("roofing", "plumbing", "hvac", "electrical")

# Q20: each item maps to the business's services by keywords; "Not sure" is the template's, always.
PROBLEMS = {
    "roofing": (
        Problem("It's leaking", ("repair", "leak")),
        Problem("Shingles are missing", ("repair",)),
        Problem("A storm hit it", ("storm",)),
        Problem("It's getting old", ("replacement", "replace")),
    ),
    "plumbing": (
        Problem("A leak", ("leak", "repair")),
        Problem("A clog or slow drain", ("drain",)),
        Problem("No hot water", ("water heater",)),
        Problem("Low water pressure", ("pressure", "repipe")),
        Problem("A running toilet", ("toilet",)),
    ),
    "hvac": (
        Problem("No cool air", ("ac", "cooling", "repair")),
        Problem("No heat", ("heat", "furnace", "repair")),
        Problem("A strange noise or smell", ("repair",)),
        Problem("The bill went up", ("maintenance", "tune-up")),
    ),
    "electrical": (
        Problem("Breakers keep tripping", ("panel",)),
        Problem("An outlet or switch stopped working", ("repair",)),
        Problem("Lights flicker", ("wiring",)),
        Problem("I need an EV charger", ("ev", "charger")),
    ),
}


def slots(spec) -> dict[str, str]:
    """Q15: hero, about, three gallery photos, a photo for each service (its trade's kind) and the contact page. The hero
    is the trade's own kind for a trade other than roofing (2026-10-08: the family's heroes are roofs, and an AC
    contractor's site led with one); the roofer, and a business of no trade, keep the family's heroes -- and so does a
    library with none of the trade's photos (images.fill)."""
    trade = spec.trade
    hero = f"service-{trade}" if trade in TRADES and trade != "roofing" else "hero"
    out = {"hero": hero, "about": "about", "contact": "general"}
    out.update({f"gallery-{i}": "gallery" for i in (1, 2, 3)})
    for s in spec.facts.services:
        out[f"service-{s.slug}"] = s.stock_slot or (f"service-{trade}" if trade else "general")
    return out


FAMILY = Family(
    id="home_services",
    trades=TRADES,
    styles={"solid": SOLID, "clean": CLEAN, "bold": BOLD},
    key_photo_slot="hero",
    form=Form(
        intent="quote",
        fields=("name", "phone", "service", "zip", "preferred_time"),
        button="Request a quote",
        note="We call you back to agree on a time.",
    ),
    banned=("guaranteed", "guarantee", "best", "#1", "cheapest", "lowest price"),
    problems=PROBLEMS,
    schema_type={
        "roofing": "RoofingContractor",
        "plumbing": "Plumber",
        "hvac": "HVACBusiness",
        "electrical": "Electrician",
    },
    slots=slots,
)
