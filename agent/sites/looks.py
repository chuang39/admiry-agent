"""The looks an owner picks an industry site by (A3p-6 batch 2 Q5; spec T4, T27; the founder's ruling 1 of 2026-10-06:
the look is picked from the samples' screenshots, and one site is built after the pick). Each family's three styles,
named and said in one line, each shown by its sample site's phone screenshot: the six files
`agent/static/sites/looks/<sample>-<style>.jpg` (390 px wide, each under 100 KB), taken from the dev gallery of
2026-10-06 and refreshed with `python -m agent.ops site-looks <business_id> --out agent/static/sites/looks`, served
with the homepage's static files on the host the owner reads them on."""

from __future__ import annotations

from pathlib import Path

from .. import config

DIR = Path(__file__).resolve().parent.parent / "static" / "sites" / "looks"  # the pictures, as shipped

# Q5's names and lines (Q12's table: the founder approves them with the plan), in each family's own order of styles
# (families.<family>.FAMILY.styles); Warm's and Boutique's say what their pictures show (Task 4 fix round 1)
LOOKS: dict[str, tuple[dict, ...]] = {
    "home_services": (
        {"id": "solid", "name": "Solid", "line": "Navy and white, an orange button, your towns and hours up top."},
        {"id": "clean", "name": "Clean", "line": "White and soft green, round buttons, your numbers in a row."},
        {"id": "bold", "name": "Bold", "line": "Dark and yellow, big headlines, straight to the problem."},
    ),
    "dental": (
        {"id": "calm", "name": "Calm", "line": "Soft teal, gentle type, new patients welcomed up front."},
        {"id": "warm", "name": "Warm", "line": "Peach and terracotta, round and friendly."},
        {"id": "boutique", "name": "Boutique", "line": "Deep green and gold, a serif wordmark, understated."},
    ),
}
# The sample site each family's looks are pictured by (agent/sites/samples.py), and a home-services trade's own (the
# founder's dev walk, 2026-10-08): a trade's while its three pictures are shipped, else the family's
SAMPLE = {"home_services": "roofer", "dental": "dentist"}
TRADE_SAMPLE = {"roofing": "roofer", "hvac": "hvac", "plumbing": "plumber", "electrical": "electrician"}


def sample_for(family: str, trade: str | None = None) -> str:
    """The sample whose screenshots picture the looks of a business of `family` and `trade`: the trade's own when every
    one of the family's looks has its picture under DIR, else the family's."""
    own = TRADE_SAMPLE.get(trade or "") if family == "home_services" else None
    if own and all((DIR / f"{own}-{look['id']}.jpg").is_file() for look in LOOKS[family]):
        return own
    return SAMPLE[family]


def file_name(family: str, style: str, trade: str | None = None) -> str:
    """A look's picture, as it is named under agent/static/sites/looks/: `roofer-solid.jpg`."""
    return f"{sample_for(family, trade)}-{style}.jpg"


def look_url(settings: config.Settings, family: str, style: str, trade: str | None = None) -> str:
    """Where a look's picture is served: the homepage's static files, on the browser host (`web_base_url`)."""
    return f"{settings.web_base_url}/static/sites/looks/{file_name(family, style, trade)}"


def looks(settings: config.Settings, family: str, trade: str | None = None) -> list[dict]:
    """The family's three looks, each `{id, name, line, look_url}`, in the family's order, pictured by the trade's own
    sample when it has one (`sample_for`)."""
    return [{**look, "look_url": look_url(settings, family, look["id"], trade)} for look in LOOKS[family]]


def own_style(family: str, style: str | None) -> str | None:
    """The look a draft keeps while it is one of the family's own; one picked under another family -- the business's
    kind changed since -- is none (Task 4 fix round 1; here, where `build_site` and the photos' rebuild both read it)."""
    return style if style in {look["id"] for look in LOOKS.get(family, ())} else None


def name(family: str, style: str) -> str:
    """A look's name as the owner reads it ("Bold"); the style as given for one that is none of the family's."""
    return next((look["name"] for look in LOOKS.get(family, ()) if look["id"] == style), style)
