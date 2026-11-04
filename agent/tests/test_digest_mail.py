"""The rendered digest (digest spec 2026-09-30 §4.1, §4.2, §8, §9; plan 2026-10-07-agent-k2 Task 9, Dg Task 5): the
text and the HTML say the same words, everything escaped, the unsubscribe headers on an owner's mail only, the footer
by the reader's frequency, the operator's preview with its insight line and codes, Admiry's take and suggestions."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent import config, copy, digest_body, digest_mail, mail_out
from agent.tests import digest_fixtures as df
from agent.tests.conftest import TEST_LINK_SECRET

GOLDEN_DIR = Path(__file__).parent / "golden"
BUILT = datetime(2026, 10, 6, 15, 2, 11, tzinfo=UTC)
REC = {
    "text": "Tell Admiry which callers booked.",
    "action": {"tool": "get_leads", "label": "Say what the leads became"},
}


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", TEST_LINK_SECRET)
    return config.load()


def body(settings, *, weekly=False, take="Two calls yesterday, and the page brought one more.", recs=(REC,)):
    built = digest_body.build(
        df.sample_numbers(weekly=weekly),
        df.sample_items(settings),
        edition="weekly" if weekly else "daily",
        numbers_final=True,
        business_name="Bright Smile Dental",
        several=False,
        built_at=BUILT,
        safe_actions=["Admiry paused NewsBreak: the balance ran out."],
    )
    return digest_body.with_insight(built, take=take, recommendations=list(recs))


def render(settings, b, **kw):
    args = {"business_id": "biz_1", "frequency": "daily", "operator": False, "reply_to": None, **kw}
    return digest_mail.render(settings, b, **args)


def golden(name: str, got: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(got, encoding="utf-8")
    assert got == path.read_text(encoding="utf-8")


def test_golden_daily_text(settings):
    golden("digest_daily.txt", render(settings, body(settings))["text"])


def test_golden_weekly_text(settings):
    golden("digest_weekly.txt", render(settings, body(settings, weekly=True))["text"])


def test_golden_daily_html(settings):
    golden("digest_daily.html", render(settings, body(settings))["html"])


def test_the_owners_mail_carries_the_unsubscribe_headers(settings):
    out = render(settings, body(settings))
    url = out["headers"]["List-Unsubscribe"]
    assert url.startswith(f"<{settings.web_base_url}/unsubscribe/v") and url.endswith("?b=biz_1>")
    assert out["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert out["subject"] == "Needs you: Yesterday: $28.10 spent, 2 calls, 1 form" and out["reply_to"] is None


def test_html_escapes_everything(settings):
    b = body(
        settings, take="<b>x</b> & <img src=//x>", recs=({"text": "<script>a</script>", "action": {"label": "<i>"}},)
    )
    b["rows"][1]["state_text"] = "<u>Paused</u>"
    b["items"][1]["text"] = "<a href=x>marked</a>"
    html = render(settings, b)["html"]
    for raw in ("<b>x</b>", "<img src=//x>", "<script>", "<u>Paused</u>", "<a href=x>"):
        assert raw not in html
    assert "&lt;b&gt;x&lt;/b&gt; &amp; &lt;img src=//x&gt;" in html


def test_subject_strips_control_characters(settings):
    b = body(settings)
    b["subject"] = "Joe\r\nBcc: x"
    assert render(settings, b)["subject"] == "Joe Bcc: x"


def test_footer_is_the_last_text_line(settings):
    for weekly in (False, True):
        text = render(settings, body(settings, weekly=weekly))["text"]
        assert text.rstrip("\n").splitlines()[-1] == mail_out.FOOTER


def test_operator_render_has_no_unsubscribe_headers_and_no_link_urls(settings):
    extra = {"insight": None, "codes": {}}
    out = render(settings, body(settings), operator=True, operator_extra=extra)
    assert out["headers"] == {} and "/unsubscribe/" not in out["text"] and "/unsubscribe/" not in out["html"]
    assert "?report=" not in out["text"].split(digest_body.FOOTER_DAILY, 1)[1]
    assert "Weekly only" in out["text"] and "Stop reports for Bright Smile Dental" in out["text"]


def test_operator_render_shows_the_insight_line_and_codes(settings):
    extra = {
        "insight": {
            "status": "done",
            "verdict": "revise",
            "score": 0.55,
            "dropped": "revise",
            "summary_dropped": "number",
            "issues": [{"dimension": "priority", "text": "led with the quiet day"}],
        },
        "codes": {"google_search": ["BUDGET_CONSTRAINED"]},
    }
    text = render(settings, body(settings), operator=True, operator_extra=extra)["text"]
    assert "insight: done · verdict revise · score 0.55 · dropped revise" in text
    assert "summary dropped: number" in text and "issues: priority: led with the quiet day" in text
    assert "codes: google_search: BUDGET_CONSTRAINED" in text
    owner = render(settings, body(settings))["text"]
    assert "insight:" not in owner and "BUDGET_CONSTRAINED" not in owner


def test_weekly_reader_footer_has_only_the_stop_link(settings):
    text = render(settings, body(settings, weekly=True), frequency="weekly")["text"]
    assert digest_body.FOOTER_WEEKLY in text and "Weekly only" not in text and "Stop reports for" in text
    daily = render(settings, body(settings))["text"]
    assert digest_body.FOOTER_DAILY in daily and "Weekly only:" in daily


def test_no_reply_to_means_the_open_admiry_footer(settings):
    assert digest_body.ASK_ADMIRY in render(settings, body(settings))["text"]
    replied = render(settings, body(settings), reply_to="hello+tok@agent.test")
    assert replied["reply_to"] == "hello+tok@agent.test"
    assert digest_body.REPLY_INVITE in replied["text"] and digest_body.ASK_ADMIRY not in replied["text"]


def test_the_take_and_the_suggestions_are_rendered(settings):
    out = render(settings, body(settings))
    assert "Admiry's take: Two calls yesterday, and the page brought one more." in out["text"]
    assert "- Tell Admiry which callers booked." in out["text"]
    plain = render(settings, body(settings, take=None, recs=()))
    assert "Admiry's take" not in plain["text"] and "Admiry suggests" not in plain["text"]
    assert "Admiry's take" not in plain["html"]


def test_every_rendered_word_passes_owner_words(settings):
    for weekly in (False, True):
        out = render(settings, body(settings, weekly=weekly))
        words = re.sub(r"https?://\S+", "", out["text"])
        assert copy.jargon(words) is None, copy.jargon(words)
