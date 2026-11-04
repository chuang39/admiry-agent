"""The "Your ads" templates over a fixture context, and the mock the founder reviews (spec 2026-10-06 §2, §3.2, Y15)."""

from __future__ import annotations

import re
from html import unescape

from agent import ads_mode, your_ads_mock
from agent.templating import env
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON


def _page(**changes) -> str:
    return env.get_template("your_ads.html").render(**{**your_ads_mock.fixture(), **changes})


def test_the_page_shows_the_month_the_area_and_one_card_per_platform():
    html = _page()
    assert "<title>Admiry — Your ads</title>" in html and "<h1>Your ads</h1>" in html
    assert "This month so far: <strong>$412</strong> of $2,300 · 31 visits from your ads · 4 leads · 2 calls" in html
    assert "Within 15 miles of the business — 500 University Ave, Palo Alto, CA." in html
    assert html.index('id="google_search"') < html.index('id="newsbreak"')
    google = html.split('id="google_search"')[1].split("</section>")[0]
    assert (
        "Connected ✓ · account 123-456-7890" in google
        and "$9.80 of $65.78 today · $412 of $2,000 this month · 2 calls, 1 form this week" in google
    )
    assert 'action="/connect/ads/pause"' in google and 'Pause my ads<span class="sr"> on Google Search</span>' in google
    # the owner's own account, linked: the lock's sentence and no action, as the page says it (Settings did before)
    assert ads_mode.LINK_IN_PLACE.format(display="123-456-7890") in unescape(google) and "/?setup=" not in google
    assert "Version 5 · running" in google and "Invisalign in Palo Alto<br>" in google
    assert "Shows for: invisalign palo alto · clear aligners" in google and "Sends visitors to: Invisalign" in google
    assert "$2,000 a month · about $65.78 a day" in google
    assert 'name="kind" value="budget"' in google and 'name="kind" value="words"' in google
    assert ">Invisalign in Palo Alto\nSame-Week Appointments\nClear Aligners Near You</textarea>" in google
    assert "At most 30 characters each; keep at least 3, at most 15." in google
    nb = html.split('id="newsbreak"')[1].split("</section>")[0]
    assert "NewsBreak uses Google Search's words." in nb and "Give NewsBreak its own words" in nb
    # run by Admiry for a business that pays Google itself: waiting for funds, no Add funds (one_action), no Pause
    assert "Run by Admiry" in nb and "Add funds" not in nb and 'action="/connect/ads/' not in nb
    assert '<p class="row-meta">Version 5</p>' in nb and "still runs" not in nb  # the approved version, not running
    assert "Facebook &amp; Instagram — Not in this plan yet; coming to Admiry." in html
    assert "Admiry charges no management fee." in html and "Nothing changes until you approve it." in html
    assert 'name="base" value="pl_base000000000"' in html and "<style" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert JARGON.findall(html) == [] and ACCOUNT_JARGON.findall(html) == [] and MONEY_JARGON.findall(html) == []


def test_the_page_before_an_approved_plan_and_the_banners():
    html = _page(approved=None, platforms=[], latest_plan={"id": "pl_draft0000000001", "version": 1})
    assert "Your ads are not launched yet." in html and 'href="/plan/pl_draft0000000001"' in html
    assert 'name="kind"' not in html
    html = _page(approved_version=5, notice=None)
    assert '<p class="banner" role="status">Version 5 approved.</p>' in html
    html = _page(
        notice="Nothing changed.",
        open_form={
            "kind": "budget",
            "channel": "google_search",
            "amount": "2000",
            "radius": None,
            "headlines": "",
            "descriptions": "",
        },
    )
    assert '<p class="banner banner-alert" role="alert">Nothing changed.</p>' in html
    google = html.split('id="google_search"')[1].split("</section>")[0]
    assert (
        '<details class="edit" open>' in google and 'name="amount" inputmode="numeric" required value="2000"' in google
    )
    html = _page(newer_draft={"id": "pl_draft0000000002", "version": 6})
    assert "A newer version of your plan is waiting for your approval." in html and "See version 6" in html


def test_the_review_lists_amounts_words_and_what_happens_next():
    html = env.get_template("your_ads_review.html").render(**your_ads_mock.review_fixture())
    assert "<title>Admiry — Review the change</title>" in html and "<h2>New words for Google Search</h2>" in html
    assert '<td>Google Search</td><td class="num">$2,000</td><td class="num">unchanged</td>' in html
    assert '<td>Total a month</td><td class="num">$2,300</td><td class="num">unchanged</td>' in html
    assert (
        "Google Search: the lines that will run" in html
        and "Dropped a headline (&#34;Call now!!&#34;): it has an exclamation mark." in html
    )
    assert "Google Search reviews new words before they show" in html
    assert (
        'action="/ads/approve"' in html
        and ">Approve and send</button>" in html
        and 'href="/ads?b=biz_mock#google_search">Back</a>' in html
    )
    assert 'name="headlines" value="Invisalign in Palo Alto\nSame-Week Appointments\nClear Aligners Near You"' in html
    assert JARGON.findall(html) == [] and "<style" not in html
    assert "Where your ads show" not in html
    html = env.get_template("your_ads_review.html").render(
        **{**your_ads_mock.review_fixture(), "radius": {"old": "15", "new": "25"}}
    )
    assert "<p>Within 15 → 25 miles of the business.</p>" in html


def test_the_mock_frames_the_page_twice_and_the_review_with_the_styles_inlined():
    mock = your_ads_mock.render()
    assert (
        mock.count("<iframe") == 3
        and 'class="desktop"' in mock
        and 'class="phone"' in mock
        and 'class="review"' in mock
    )
    assert "srcdoc=" in mock and "/static/your_ads.css" not in mock and ".settings-wrap {" in mock
    assert "https://" not in mock


def test_the_changes_admiry_made_section_shows_only_with_changes():
    assert "Changes Admiry made" not in _page()
    html = _page(changes=["Removed the headline “Invisalign Deals”: a name Google treats as a trademark."])
    section = html.split('id="changes"')[1].split("</section>")[0]
    assert "Changes Admiry made" in section and "Removed the headline “Invisalign Deals”" in section
    assert your_ads_mock.fixture()["changes"] == [] and "stay as you agreed" in section
