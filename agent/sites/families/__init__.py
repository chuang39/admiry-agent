"""The two families of industry site and their three styles each (A3p-6 batch 1 Q2, Q15, Q20; spec T3, T4, T29).

A family is one structure (the sections, the form, the problem picker, the banned words); a style is design tokens and
a hero layout over it. The tokens are the canvas's, hex for hex, except where a pair failed `Style.pairs()` and was
darkened until it passed (recorded in the as-built section of the plan)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

HERO_LAYOUTS = ("split-tinted", "split-white", "dark-split", "centred")


@dataclass(frozen=True)
class FontFile:
    """One self-hosted face: `file` under agent/data/sites/fonts/, `weight` the CSS range (`600 800`) or one weight."""

    face: str
    file: str
    weight: str


@dataclass(frozen=True)
class Style:
    display_font: str
    body_font: str
    fonts: tuple[FontFile, ...]
    tokens: dict[str, str]  # ground, ink, text, muted, line, line_alt, tint, tint2, dark, on_dark, photo (+ dark_card)
    radii: dict[str, int]  # button, card, image (+ pill, input where the style has them)
    hero: str
    topbar: bool
    trust: str  # pills | stats | band
    on_accent: str  # the button's words on the accent
    accent: str
    accent_options: tuple[str, ...]  # the first is the accent
    display_upper: bool = False
    muted_on_dark: bool = False  # `muted` is the on-dark secondary text (Bold), not a colour for the light grounds
    accent_text_on: tuple[str, ...] = ()  # grounds (token names) where the accent is the eyebrow's text colour
    features: tuple[str, ...] = ()  # the canvas's per-style extras the templates switch on

    def pairs(self) -> list[tuple[str, str, str, float]]:
        """(label, text colour, ground, least ratio) for every text-on-ground pair the style uses: 4.5 for body
        text, 3 for the display sizes (24 px and up: the eyebrow and heading colours)."""
        t = self.tokens
        light = [("ground", t["ground"]), ("tint", t["tint"]), ("tint2", t["tint2"])]
        out = [(f"ink on {n}", t["ink"], g, 3.0) for n, g in light]
        out += [(f"text on {n}", t["text"], g, 4.5) for n, g in light]
        if not self.muted_on_dark:
            out += [(f"muted on {n}", t["muted"], g, 4.5) for n, g in light]
        darks = [("dark", t["dark"])] + ([("dark_card", t["dark_card"])] if "dark_card" in t else [])
        out += [(f"on_dark on {n}", t["on_dark"], g, 4.5) for n, g in darks]
        if self.muted_on_dark:
            out += [(f"muted on {n}", t["muted"], g, 4.5) for n, g in darks]
        for g in self.accent_text_on:  # the eyebrow in the accent, for every accent the owner may pick
            out += [(f"accent {a} eyebrow on {g}", a, t[g], 4.5) for a in self.accent_options]
        out.append(("on_accent on accent", self.on_accent, self.accent, 4.5))
        return out


@dataclass(frozen=True)
class Form:
    """Q12: the family's one form. `honeypot` is added by the template; every field has a visible label."""

    intent: str
    fields: tuple[str, ...]
    button: str
    note: str
    honeypot: str = "website2"


@dataclass(frozen=True)
class Problem:
    """One line of the problem picker: shown when a service of the business has one of `keywords` in its name or
    slug (Q20)."""

    label: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Family:
    id: str
    trades: tuple[str, ...]
    styles: dict[str, Style]
    key_photo_slot: str
    form: Form
    banned: tuple[str, ...]
    problems: dict[str, tuple[Problem, ...]]
    schema_type: dict[str, str]
    slots: Callable[[object], dict[str, str]]  # the family's slot map (Q15): spec -> {slot: stock kind}

    def slot_kinds(self, spec) -> dict[str, str]:
        """The stock kind each photo slot of `spec` is filled from (Q15). People slots (team, dentist, work-people)
        are never listed: they take the owner's photo or the layout with no person (T11)."""
        return self.slots(spec)


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FAMILIES = {"home_services", "dental"}


def get(family_id: str) -> Family:
    """The family, or KeyError for one that does not exist (the modules load on first use: they import this one)."""
    if family_id not in FAMILIES:
        raise KeyError(family_id)
    from . import dental, home_services

    return {"home_services": home_services.FAMILY, "dental": dental.FAMILY}[family_id]
