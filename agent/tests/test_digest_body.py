"""The digest's stored body and its owner sentences, and the unsubscribe tokens (digest spec 2026-09-30 §4, §4.4, §9;
plan 2026-10-07-agent-k2 Task 9, Dg Task 5, P9)."""

import json
from datetime import UTC, datetime

import pytest

from agent import config, digest_body, digest_links
from agent.tests import digest_fixtures as df
from agent.tests.conftest import TEST_LINK_SECRET

BUILT = datetime(2026, 10, 6, 15, 2, 11, tzinfo=UTC)
OTHER_SECRET = "cd" * 32


@pytest.fixture
def linked(monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", TEST_LINK_SECRET)
    return config.load()


def _body(settings, *, weekly=False, final=True, items=None, several=False):
    numbers = df.sample_numbers(weekly=weekly)
    return digest_body.build(
        numbers,
        df.sample_items(settings) if items is None else items,
        edition="weekly" if weekly else "daily",
        numbers_final=final,
        business_name="Bright Smile Dental",
        several=several,
        built_at=BUILT,
        safe_actions=["Admiry paused NewsBreak: the balance ran out."],
    )


def test_token_round_trip(linked):
    token = digest_links.mint(linked, "biz_1")
    assert token.startswith("v" + digest_links.fingerprint(TEST_LINK_SECRET) + ".")
    assert digest_links.verify(linked, "biz_1", token)


def test_previous_secret_still_verifies(monkeypatch, linked):
    old = digest_links.mint(linked, "biz_1")
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", OTHER_SECRET)
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET_PREVIOUS", TEST_LINK_SECRET)
    rotated = config.load()
    assert digest_links.verify(rotated, "biz_1", old)
    assert digest_links.verify(rotated, "biz_1", digest_links.mint(rotated, "biz_1"))
    monkeypatch.delenv("AGENT_DIGEST_LINK_SECRET_PREVIOUS")
    assert not digest_links.verify(config.load(), "biz_1", old)  # the retired key no longer counts


def test_bad_or_foreign_tokens_fail_the_same(linked):
    token = digest_links.mint(linked, "biz_1")
    fp, mac = token[1:].split(".", 1)
    for bad in (
        digest_links.mint(linked, "biz_2"),  # another business's
        f"v{fp}.{mac[:-2]}xx",  # the mac changed
        f"v000000.{mac}",  # no key has that fingerprint
        token[1:],  # no v
        "",
    ):
        assert digest_links.verify(linked, "biz_1", bad) is False


def test_urls_use_the_web_base(linked):
    web = linked.web_base_url
    assert digest_links.unsubscribe_url(linked, "biz_1").startswith(f"{web}/unsubscribe/v")
    assert digest_links.unsubscribe_url(linked, "biz_1", "weekly").endswith("?b=biz_1&f=weekly")
    assert digest_links.report_url(linked, "biz_1", df.ANALYST_DAY) == f"{web}/?report=biz_1&day=2026-10-05"
    assert digest_links.ads_url(linked, "biz_1") == f"{web}/ads?b=biz_1"


def test_the_body_is_the_specs_shape(linked):
    body = _body(linked)
    assert body["version"] == 1 and body["edition"] == "daily" and body["day"] == "2026-10-05"
    assert body["time_zone"] == "America/Los_Angeles" and body["built_at"] == "2026-10-06T15:02:11+00:00"
    assert body["month"] == {"name": "October", "spent_usd": 412.3, "limit_usd": 600.0, "limit_source": "owner"}
    gs = body["rows"][0]
    assert gs == {
        "channel": "google_search",
        "label": "Google Search",
        "class": "active",
        "state_text": None,
        "spend_usd": 23.1,
        "clicks": 41,
        "calls": 2,
        "forms": 1,
        "leads": 2,
        "daily_budget_usd": 19.73,
        "source": "api",
    }
    assert body["rows"][-1] == {"channel": "page", "label": digest_body.YOUR_PAGE, "leads": 1}
    assert body["items"][0] == {
        "rule": "paused_by_system",
        "severity": "high",
        "text": "NewsBreak is paused: there is no money left to run it. Add funds to start it again.",
        "link": {"label": "See your ads", "url": f"{linked.web_base_url}/ads?b=biz_1"},
        "ref": "pause:newsbreak",
    }
    assert body["week"] is None and body["recommendations"] == [] and body["note"] is None
    assert body["business"] == "Bright Smile Dental" and body["disclosure"]


def test_body_holds_no_lead_fields_codes_or_tokens(linked):
    shown = json.dumps(_body(linked, weekly=True))
    assert "BUDGET_CONSTRAINED" not in shown and "/unsubscribe/" not in shown and "@" not in shown


def test_sentence_not_final(linked):
    final = digest_body.sentence(df.sample_numbers(), edition="daily", numbers_final=True)
    assert final == "Yesterday: $28.10 spent, 2 calls, 1 form. October: $412.30 of $600."
    later = digest_body.sentence(df.sample_numbers(), edition="daily", numbers_final=False)
    assert later == f"{digest_body.NOT_FINAL} {final}"


def test_subject_needs_you_prefix(linked):
    numbers, items = df.sample_numbers(), df.sample_items(linked)
    assert digest_body.subject(numbers, items, edition="daily", business_name="X", several=False) == (
        "Needs you: Yesterday: $28.10 spent, 2 calls, 1 form"
    )
    quiet = [i for i in items if i.severity != "high"]
    assert digest_body.subject(numbers, quiet, edition="daily", business_name="X", several=False) == (
        "Yesterday: $28.10 spent, 2 calls, 1 form"
    )
    weekly = df.sample_numbers(weekly=True)
    assert digest_body.subject(weekly, quiet, edition="weekly", business_name="X", several=False) == (
        "Your week: $167 spent, 10 leads, 3 booked"
    )


def test_business_name_suffix_only_for_several(linked):
    one = digest_body.subject(df.sample_numbers(), [], edition="daily", business_name="Joe\r\nBcc: x", several=False)
    two = digest_body.subject(df.sample_numbers(), [], edition="daily", business_name="Joe\r\nBcc: x", several=True)
    assert "Joe" not in one and two.endswith("· Joe Bcc: x") and "\r" not in two and "\n" not in two


def test_week_rows_and_prior_keys(linked):
    week = _body(linked, weekly=True)["week"]
    assert set(week["rows"][0]) == {
        "channel",
        "label",
        "spend_usd",
        "leads",
        "calls",
        "booked",
        "cost_per_lead_usd",
        "cost_per_booked_usd",
        "sample_word",
        "source",
    }
    assert week["prior"][0] == {"channel": "google_search", "spend_usd": 120.0, "leads": 7, "calls": 5, "booked": 2}
    assert week["leads_by_status"] == {"new": 4, "booked": 3, "lost": 1}
    assert week["month_sentence"] == "You spent $412.30 in October. 14 leads, 4 booked."


def test_with_insight_sets_the_take_and_the_suggestions(linked):
    rec = {
        "text": "Tell Admiry which callers booked.",
        "action": {"tool": "get_leads", "label": "Say what the leads became"},
    }
    body = digest_body.with_insight(_body(linked), take="Two calls yesterday.", recommendations=[rec])
    assert body["note"] == "Two calls yesterday."
    assert body["recommendations"] == [
        {"text": "Tell Admiry which callers booked.", "label": "Say what the leads became"}
    ]
