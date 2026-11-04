"""The "Your ads" mock for the founder's review (spec 2026-10-06 Y15; the channels spec's §7.2 rule: a mock approved
before the thing is built): the page's own templates over a fixture context, the three stylesheets inlined, framed at
desktop and phone widths plus the review step, one file.

    .venv/bin/python -m agent.your_ads_mock > docs/superpowers/mocks/your-ads.html

No database and no network; opened from disk, the fonts fall back to the system's (base.css's @font-face points at
/static/fonts). The fixture's keys are the page context's (agent/your_ads.py builds the same keys from the database;
test_your_ads_page pins the two to one shape)."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from . import ads_mode, ads_refusal, plan_words, plans
from .templating import env
from .your_ads import FOOT, NEWER

STATIC = Path(__file__).parent / "static"
BASE = "pl_base000000000"
HEADER = {
    "session": {"csrf": "mock"},
    "signed_in": True,
    "initial": "O",
    "email": "owner@example.com",
    "csrf": "mock",
    "site": "",
}
HEADLINES = ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners Near You"]
DESCRIPTIONS = ["Clear aligners from a family dentist. Book online.", "Straighter teeth without metal braces."]


def _ads(copy_of: str | None = None) -> dict:
    return {
        "headlines": HEADLINES,
        "descriptions": DESCRIPTIONS,
        "headlines_text": "\n".join(HEADLINES),
        "descriptions_text": "\n".join(DESCRIPTIONS),
        "themes": ["invisalign palo alto", "clear aligners"],
        "landing": "Invisalign",
        "copy_of": copy_of,
    }


def fixture() -> dict:
    """Bright Smile Dental, which pays Google itself: Google Search live on version 5, the approved one, in the owner's
    own account -- linked, so the way it runs is locked and the row offers nothing (setup.state's sentence says why);
    NewsBreak run by Admiry, waiting for funds, with no Add funds (that is a prepaid business's, `one_action`);
    Facebook & Instagram later. A state the page's builder can produce (final review minor)."""
    business = SimpleNamespace(id="biz_mock", name="Bright Smile Dental", switch_label="Bright Smile Dental")
    return {
        **HEADER,
        "business": business,
        "businesses": [business],
        "approved": {"id": BASE, "version": 5},
        "latest_plan": {"id": BASE, "version": 5},
        "in_review": None,  # L1: this business's plan is long approved, never sent to Admiry's review
        "newer_draft": None,
        "newer_words": NEWER,
        "changes": [],
        "changes_intro": ads_refusal.INTRO,
        "month": {"spent": "$412", "cap": "$2,300", "visits": 31, "leads": 4, "calls": 2},
        "area": {
            "sentence": "within 15 miles of the business",
            "address": "500 University Ave, Palo Alto, CA",
            "radius": 15,
        },
        "platforms": [
            {
                "channel": "google_search",
                "label": "Google Search",
                "state_text": "Live",
                "sentence": "Google Search: live · $9.80 of $65.78 today.",
                "account": "Connected ✓",
                "account_public": "123-456-7890",
                "action": None,
                "lock": ads_mode.LINK_IN_PLACE.format(display="123-456-7890"),
                "note": None,
                "numbers": "$9.80 of $65.78 today · $412 of $2,000 this month · 2 calls, 1 form this week",
                "issues": [],
                "toggle": "pause",
                "running": {"version": 5, "note": None, "live": True},
                "ads": _ads(),
                "budget": {"amount": 2000, "monthly": "$2,000", "day": "$65.78"},
                "limits": plan_words.limits("google_search"),
            },
            {
                "channel": "newsbreak",
                "label": "NewsBreak",
                "state_text": "Waiting for funds",
                "sentence": "NewsBreak: waiting for funds.",
                "account": "Run by Admiry",
                "account_public": None,
                "action": None,
                "lock": None,
                "note": None,
                "numbers": "$0 of $300 this month",
                "issues": [],
                "toggle": None,
                "running": {"version": 5, "note": None, "live": False},
                "ads": _ads(copy_of="Google Search"),
                "budget": {"amount": 300, "monthly": "$300", "day": "$9.86"},
                "limits": plan_words.limits("newsbreak"),
            },
        ],
        "later": [{"label": "Facebook & Instagram", "why": "Not in this plan yet; coming to Admiry."}],
        "approved_version": None,
        "notice": None,
        "open_form": None,
        "foot": FOOT,
        "disclosure": plans.DISCLOSURE,
    }


def review_fixture() -> dict:
    """The review of new words for Google Search: one line dropped, the amounts unchanged."""
    return {
        **HEADER,
        "business": {"id": "biz_mock"},
        "headline": "New words for Google Search",
        "kind": "words",
        "channel": "google_search",
        "rows": [
            {"label": "Google Search", "old": "$2,000", "new": "$2,000", "day": "$65.78", "changed": False},
            {"label": "NewsBreak", "old": "$300", "new": "$300", "day": "$9.86", "changed": False},
        ],
        "total": {"old": "$2,300", "new": "$2,300", "changed": False},
        "radius": None,
        "words": [
            {
                "label": "Google Search",
                "headlines": HEADLINES,
                "descriptions": DESCRIPTIONS,
                "notes": ['Dropped a headline ("Call now!!"): it has an exclamation mark.'],
            },
            {"label": "NewsBreak", "headlines": HEADLINES, "descriptions": DESCRIPTIONS, "notes": []},
        ],
        "next_steps": [
            {
                "label": "Google Search",
                "sentence": (
                    "Google Search reviews new words before they show, usually within a day; the current ad pauses "
                    "while the new one is reviewed."
                ),
            },
            {
                "label": "NewsBreak",
                "sentence": "NewsBreak reviews the new words first; your current ads keep running until the new ones pass.",
            },
        ],
        "fields": {
            "base": BASE,
            "kind": "words",
            "channel": "google_search",
            "amount": None,
            "radius": None,
            "headlines": "\n".join(HEADLINES),
            "descriptions": "\n".join(DESCRIPTIONS),
        },
        "back": "/ads?b=biz_mock#google_search",
        "disclosure": plans.DISCLOSURE,
    }


def _inlined(doc: str) -> str:
    """The page with its three stylesheets in a <style> each (the mock is opened from disk) and site.js left out."""
    css = "".join(
        f"<style>{(STATIC / name).read_text()}</style>" for name in ("base.css", "settings.css", "your_ads.css")
    )
    doc = re.sub(r'<link rel="stylesheet" href="/static/[a-z_]+\.css">\n?', "", doc)
    doc = doc.replace('<script src="/static/site.js" defer></script>\n', "")
    return doc.replace("</head>", css + "</head>", 1)


def _frame(cls: str, title: str, doc: str, height: int) -> str:
    return f'<iframe class="{cls}" title="{title}" srcdoc="{html.escape(doc, quote=True)}" style="height:{height}px"></iframe>'


def render() -> str:
    page = _inlined(env.get_template("your_ads.html").render(**fixture()))
    review = _inlined(env.get_template("your_ads_review.html").render(**review_fixture()))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Admiry — Your ads (mock)</title>
<style>
  /* Mock of the "Your ads" page (spec 2026-10-06-admiry-your-ads-page-design.md, Y15) for the founder's review:
     the page's own templates and stylesheets (agent/your_ads_mock.py renders this file), framed at desktop and phone
     widths, then the review step. */
  body {{ margin: 0; padding: 24px; background: #EEF0F4; font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; color: #11161F; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  p {{ margin: 0 0 16px; color: #626A75; max-width: 760px; }}
  .frames {{ display: flex; flex-wrap: wrap; gap: 32px; align-items: flex-start; }}
  iframe {{ display: block; border: 1px solid #DBDEE2; border-radius: 12px; background: #fff; }}
  .desktop {{ width: 980px; max-width: 100%; }}
  .phone {{ width: 375px; }}
  .review {{ width: 980px; max-width: 100%; }}
</style>
</head>
<body>
<h1>Your ads — mock</h1>
<p>The page at desktop and phone widths (every form is a native fold: open "Change" to see it), then the review step
after a words change. Rendered from the real templates over a fixture business; approve the look before the routes are
built (spec Y15).</p>
<div class="frames">
{_frame("desktop", "Your ads at desktop width", page, 2400)}
{_frame("phone", "Your ads at phone width", page, 3200)}
{_frame("review", "The review step", review, 1500)}
</div>
</body>
</html>
"""


if __name__ == "__main__":
    sys.stdout.write(render())
