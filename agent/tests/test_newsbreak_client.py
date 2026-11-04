"""The NewsBreak Business API client (M1 Task 2; `agent/channels/newsbreak/client.py`) over `FakeNewsBreak`: the
settings it reads; the `Access-Token` header and the `code`/`errMsg`/`data` envelope; every error branch and its owner
sentence, NewsBreak's own words kept in `.detail` and never in the sentence; the throttle signal; the log line; the
request meter; the ADS_ENV leak check over the error and the log; and each endpoint's method, path and fields as
NewsBreak's articles give them -- the ad accounts, the spending cap, the asset upload's multipart, the lists page by
page, the status switches, the report's parameters, the events, the Conversion API's postback."""

from __future__ import annotations

import logging
import re
import typing
from datetime import date

import httpx
import pytest

from agent import config, models, ops
from agent.channels.newsbreak import client as newsbreak
from agent.channels.newsbreak.client import NewsBreakError
from agent.tests import fake_newsbreak
from agent.tests.conftest import ACCOUNT_JARGON, ADS_ENV, ADS_SECRETS, JARGON, MONEY_JARGON
from agent.tests.fake_newsbreak import envelope

TOKEN = ADS_ENV["NEWSBREAK_ACCESS_TOKEN"]
ORG = ADS_ENV["NEWSBREAK_ORG_ID"]
LOGGER = "agent.channels.newsbreak.client"


@pytest.fixture
def settings(nbapi):
    return config.load()


def campaign_body(account: str, name: str = "Admiry · Bright Smile · biz_1") -> dict:
    return {"adAccountId": account, "name": name, "objective": "WEB_TRAFFIC", "status": "OFF"}


def refusal(call) -> NewsBreakError:
    with pytest.raises(NewsBreakError) as e:
        call()
    return e.value


# ---------------------------------------------------------------- the settings


def test_the_newsbreak_settings_are_optional_and_the_token_is_never_printed(monkeypatch):
    settings = config.load()
    assert (settings.newsbreak_access_token, settings.newsbreak_org_id, settings.newsbreak_ready) == (None, None, False)
    monkeypatch.setenv("NEWSBREAK_ACCESS_TOKEN", "")  # compose's empty value counts as unset
    monkeypatch.setenv("NEWSBREAK_ORG_ID", "")
    settings = config.load()
    assert (settings.newsbreak_access_token, settings.newsbreak_org_id, settings.newsbreak_ready) == (None, None, False)
    monkeypatch.setenv("NEWSBREAK_ACCESS_TOKEN", f" {TOKEN}\n")  # a stray blank or newline from the box's .env
    monkeypatch.setenv("NEWSBREAK_ORG_ID", f"{ORG} ")
    settings = config.load()
    assert (settings.newsbreak_access_token, settings.newsbreak_org_id, settings.newsbreak_ready) == (TOKEN, ORG, True)
    assert TOKEN not in repr(settings) and f"newsbreak_org_id='{ORG}'" in repr(settings)


def test_newsbreak_is_ready_only_with_both_the_token_and_the_organisation(monkeypatch):
    monkeypatch.setenv("NEWSBREAK_ACCESS_TOKEN", TOKEN)
    assert not config.load().newsbreak_ready  # every ad account lives under the Organization
    monkeypatch.delenv("NEWSBREAK_ACCESS_TOKEN")
    monkeypatch.setenv("NEWSBREAK_ORG_ID", ORG)
    assert not config.load().newsbreak_ready  # every call carries the token


# ---------------------------------------------------------------- the header and the envelope


def test_every_call_carries_the_access_token_and_answers_the_envelopes_data(nbapi, settings):
    account = nbapi.add_account("Admiry · Bright Smile · biz_1")
    data = newsbreak.call(
        settings, "GET", "campaign/getList", params={"adAccountId": account, "pageNo": 1, "pageSize": 5}
    )
    assert data == {"rows": [], "pageNo": 1, "pageSize": 5, "total": 0, "hasNext": False}
    request = nbapi.requests[-1]
    assert request.headers["Access-Token"] == TOKEN
    assert str(request.url) == f"{newsbreak.BASE_URL}/campaign/getList?adAccountId={account}&pageNo=1&pageSize=5"
    made = newsbreak.call(settings, "POST", "campaign/create", json=campaign_body(account))
    assert made["name"] == "Admiry · Bright Smile · biz_1" and made["id"] in nbapi.campaigns
    assert nbapi.sent[-1] == ("campaign/create", campaign_body(account))
    assert nbapi.requests[-1].headers["Content-Type"] == "application/json"


def test_a_success_whose_data_is_not_an_object_answers_an_empty_one(nbapi, settings):
    nbapi.fail["campaign/create"] = httpx.Response(200, json={"code": 0, "errMsg": "", "data": None})
    assert newsbreak.call(settings, "POST", "campaign/create", json={}) == {}


def test_without_a_token_nothing_is_sent(nbapi, monkeypatch):
    monkeypatch.delenv("NEWSBREAK_ACCESS_TOKEN")
    e = refusal(lambda: newsbreak.call(config.load(), "POST", "campaign/create", json={}))
    assert (str(e), e.code, e.status) == (newsbreak.ACCESS_TROUBLE, None, None)
    assert nbapi.requests == []


def test_a_token_newsbreak_does_not_take_is_access_trouble(nbapi, settings):
    nbapi.token = "another token"
    e = refusal(lambda: newsbreak.call(settings, "POST", "campaign/create", json={}))
    assert (str(e), e.code, e.status, e.detail) == (newsbreak.ACCESS_TROUBLE, 4033, 200, "Invalid access token")


# ---------------------------------------------------------------- every error branch


@pytest.mark.parametrize(
    ("answer", "owner", "code", "status"),
    [
        (httpx.ConnectError("no route"), newsbreak.UNREACHABLE, None, None),
        (httpx.ReadTimeout("slow"), newsbreak.UNREACHABLE, None, None),
        (httpx.Response(503, text="<html>maintenance</html>"), newsbreak.UNREACHABLE, None, 503),
        (envelope(-1, "Unknown error", status=500), newsbreak.UNREACHABLE, -1, 500),
        (httpx.Response(401, text="unauthorized"), newsbreak.ACCESS_TROUBLE, None, 401),
        (httpx.Response(403, text="forbidden"), newsbreak.ACCESS_TROUBLE, None, 403),
        (envelope(4031, "Not login"), newsbreak.ACCESS_TROUBLE, 4031, 200),
        (envelope(4033, "Invalid access token"), newsbreak.ACCESS_TROUBLE, 4033, 200),
        (envelope(403, "Permission denied"), newsbreak.ACCESS_TROUBLE, 403, 200),
        (envelope(4034, "Rate limit exceeded"), newsbreak.BUSY, 4034, 200),
        (httpx.Response(429, text="slow down"), newsbreak.BUSY, None, 429),
        (
            envelope(41926, "type change rejected"),
            "NewsBreak refused this request (code 41926); an Admiry operator will look into it",
            41926,
            200,
        ),
        (
            envelope(-1, "Unknown error"),
            "NewsBreak refused this request (code -1); an Admiry operator will look into it",
            -1,
            200,
        ),
        (envelope(41926, "type change rejected", status=400), newsbreak.REFUSED_WITH_REASON.format(41926), 41926, 400),
        (httpx.Response(404, text="<html>Not Found</html>"), newsbreak.REFUSED, None, 404),
        (envelope(0, status=400), newsbreak.REFUSED, 0, 400),
        (httpx.Response(200, text="<html>a captive portal</html>"), newsbreak.UNREACHABLE, None, 200),
        (httpx.Response(200, json={"code": "0", "data": {}}), newsbreak.UNREACHABLE, None, 200),  # not the envelope
    ],
)
def test_every_answer_maps_to_one_owner_sentence(nbapi, settings, answer, owner, code, status):
    account = nbapi.add_account("acct")
    nbapi.fail["campaign/create"] = answer
    e = refusal(lambda: newsbreak.create_campaign(settings, campaign_body(account)))
    assert (str(e), e.owner, e.code, e.status) == (owner, owner, code, status)
    assert len(nbapi.requests) == 1  # never retried by the client: retrying is the lifecycle's


def test_newsbreaks_own_words_stay_in_the_detail_never_in_the_sentence(nbapi, settings):
    account = nbapi.add_account("acct")
    nbapi.fail["campaign/create"] = envelope(41926, "Switching from IMAGE to VIDEO is not allowed")
    e = refusal(lambda: newsbreak.create_campaign(settings, campaign_body(account)))
    assert e.detail == "Switching from IMAGE to VIDEO is not allowed"
    assert "IMAGE" not in str(e) and "IMAGE" not in repr(e)
    nbapi.fail["campaign/create"] = envelope(41926, "x" * 2000)  # an audit row holds a bounded excerpt
    assert refusal(lambda: newsbreak.create_campaign(settings, campaign_body(account))).detail == "x" * 500


def test_the_owner_sentences_say_newsbreak_and_no_jargon():
    sentences = (
        newsbreak.ACCESS_TROUBLE,
        newsbreak.BUSY,
        newsbreak.UNREACHABLE,
        newsbreak.REFUSED,
        newsbreak.REFUSED_WITH_REASON.format(4033),
        newsbreak.ASSET_UNREADABLE,
    )
    for s in sentences:
        assert "NewsBreak" in s, s
        assert not JARGON.search(s) and not ACCOUNT_JARGON.search(s) and not MONEY_JARGON.search(s), s


# ---------------------------------------------------------------- the throttle rule


def test_a_throttle_is_busy_at_once_and_the_client_never_waits_or_retries(nbapi, settings):
    """NewsBreak's 4034 is either limit (QPM: wait 5 minutes; QPD: the next UTC day) and the client cannot tell
    which: it answers BUSY with the code at once, one request per call, and the job decides how long to stay away."""
    account = nbapi.add_account("acct")
    nbapi.throttle = 2
    for _ in range(2):
        e = refusal(lambda: newsbreak.campaigns(settings, account))
        assert (e.owner, e.code, e.throttled) == (newsbreak.BUSY, newsbreak.THROTTLED, True)
    assert len(nbapi.requests) == 2
    assert newsbreak.campaigns(settings, account) == []  # the limit lifted: the next call goes through


@pytest.mark.parametrize(
    ("answer", "throttled"),
    [
        (envelope(4034, "Rate limit exceeded"), True),
        (httpx.Response(429, text="slow down"), True),  # a throttle on the way: BUSY with no NewsBreak code
        (envelope(4033, "Invalid access token"), False),
        (httpx.Response(503, text="down"), False),
        (envelope(41926, "type change rejected"), False),
        (httpx.ConnectError("no route"), False),
    ],
)
def test_one_predicate_says_throttled_for_a_4034_and_for_a_429(nbapi, settings, answer, throttled):
    """A job counting throttles ("two in a row: stop for today") counts by `throttled`, never by `.code`, which a 429
    does not carry."""
    account = nbapi.add_account("acct")
    nbapi.fail["campaign/getList"] = answer
    assert refusal(lambda: newsbreak.campaigns(settings, account)).throttled is throttled


def test_an_endpoints_own_limit_leaves_the_other_endpoints_answering(nbapi, settings):
    """Rate Limits: a global limit per developer app and, independently, one per endpoint -- the fake's `throttle`
    takes every API endpoint, or only those `throttle_endpoints` names."""
    account = nbapi.add_account("acct")
    nbapi.throttle, nbapi.throttle_endpoints = 1, {"campaign/getList"}
    assert newsbreak.ad_sets(settings, account) == []  # another endpoint's limit is not reached
    assert refusal(lambda: newsbreak.campaigns(settings, account)).throttled
    assert newsbreak.campaigns(settings, account) == []
    nbapi.throttle, nbapi.throttle_endpoints = 1, set()  # the global limit: whichever API endpoint comes next
    assert refusal(lambda: newsbreak.ad_sets(settings, account)).throttled


def test_the_postback_is_never_throttled(nbapi, settings):
    """The Conversion API's postback is no Business API call (no developer app's token): no app limit reaches it."""
    nbapi.throttle = 5
    newsbreak.send_conversion(settings, click_id="nvss_1", event_type="submit_form")
    assert nbapi.conversions == [{"callback": "nvss_1", "event_type": "submit_form"}]
    assert nbapi.throttle == 5


def test_the_client_counts_newsbreaks_throttled_answers_in_a_row(nbapi, settings):
    """M1 Task 6 (T6-4): the jobs stop NewsBreak's rows for the rest of the UTC day after two throttled answers in a
    row, so the client keeps the run of them on itself: a throttle (`throttled`: a 4034, or a 429 -- the postback's
    too) adds one, any other answer -- a success, a refusal, NewsBreak down in a 5xx -- starts it again from zero, and
    a request that got no answer at all leaves it as it was."""
    account = nbapi.add_account("acct")
    with newsbreak.client() as http:
        assert newsbreak.throttled_in_a_row(http) == 0
        nbapi.throttle = 2
        refusal(lambda: newsbreak.campaigns(settings, account, http=http))
        refusal(lambda: newsbreak.ad_sets(settings, account, http=http))
        assert newsbreak.throttled_in_a_row(http) == 2
        newsbreak.campaigns(settings, account, http=http)
        assert newsbreak.throttled_in_a_row(http) == 0
        nbapi.throttle = 1
        refusal(lambda: newsbreak.campaigns(settings, account, http=http))
        nbapi.fail["ad-set/getList"] = httpx.ConnectError("no route")  # no answer: the run of throttles stands
        refusal(lambda: newsbreak.ad_sets(settings, account, http=http))
        assert newsbreak.throttled_in_a_row(http) == 1
        nbapi.fail["tracking/attribute"] = httpx.Response(429, text="slow down")  # the postback's throttle counts too
        refusal(lambda: newsbreak.send_conversion(settings, click_id="nvss_1", event_type="submit_form", http=http))
        assert newsbreak.throttled_in_a_row(http) == 2
        nbapi.fail["ad-set/getList"] = httpx.Response(503, text="down")  # an answer, and no throttle
        refusal(lambda: newsbreak.ad_sets(settings, account, http=http))
        assert newsbreak.throttled_in_a_row(http) == 0
        nbapi.throttle = 1
        refusal(lambda: newsbreak.campaigns(settings, account, http=http))
        nbapi.fail["campaign/getList"] = envelope(41926, "type change rejected")  # a refusal ends the run too
        refusal(lambda: newsbreak.campaigns(settings, account, http=http))
        assert newsbreak.throttled_in_a_row(http) == 0
        del nbapi.fail["tracking/attribute"]
        nbapi.throttle = 1
        refusal(lambda: newsbreak.campaigns(settings, account, http=http))
        newsbreak.send_conversion(settings, click_id="nvss_2", event_type="submit_form", http=http)
        assert newsbreak.throttled_in_a_row(http) == 0  # a postback NewsBreak took is an answer like any other
    with httpx.Client() as foreign:
        assert newsbreak.throttled_in_a_row(foreign) == 0


# ---------------------------------------------------------------- the log and the leak check


def test_every_call_is_logged_with_its_method_path_and_code_and_never_its_body(nbapi, settings, caplog):
    account = nbapi.add_account("acct")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        newsbreak.create_campaign(settings, campaign_body(account, name="the words of the body"))
        nbapi.throttle = 1
        refusal(lambda: newsbreak.campaigns(settings, account))
        nbapi.fail["campaign/create"] = httpx.ConnectError("down")
        refusal(lambda: newsbreak.create_campaign(settings, campaign_body(account)))
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == LOGGER] == [
        ("INFO", "newsbreak POST campaign/create code=0 status=200"),
        ("WARNING", "newsbreak GET campaign/getList code=4034 status=200"),
        ("WARNING", "newsbreak POST campaign/create code=None status=None"),
    ]


def test_no_secret_reaches_the_error_its_detail_or_the_log(nbapi, settings, caplog):
    nbapi.fail["ad-account/create"] = envelope(40001, f"industry invalid; your token {TOKEN} is fine")
    with caplog.at_level(logging.DEBUG):
        e = refusal(
            lambda: newsbreak.create_ad_account(
                settings, org_id=ORG, name="n", company_name="c", industry="Legal Services//Lawyers"
            )
        )
    assert e.detail == "industry invalid; your token [token] is fine"  # the audit row it goes to cannot carry it
    logged = " ".join(r.getMessage() for r in caplog.records)
    for text in (str(e), repr(e), e.detail, logged):
        assert not any(secret in text for secret in ADS_SECRETS), text
    assert "industry invalid" not in logged  # NewsBreak's words are the audit row's, never the log's


# ---------------------------------------------------------------- the meter


def test_the_client_meters_every_request_it_sends_to_newsbreak(nbapi, settings):
    account = nbapi.add_account("acct")
    with newsbreak.client() as http:
        newsbreak.campaigns(settings, account, http=http)
        nbapi.throttle = 1
        refusal(lambda: newsbreak.campaigns(settings, account, http=http))  # a throttled request still counted
        newsbreak.send_conversion(settings, click_id="nvss_1", event_type="submit_form", http=http)
        assert newsbreak.ops_sent(http) == 3
        http.get("https://example.com/elsewhere")
        assert newsbreak.ops_sent(http) == 3  # another host is not NewsBreak's quota
    with httpx.Client() as foreign:
        assert newsbreak.ops_sent(foreign) == 0


def test_a_test_without_the_fake_cannot_reach_newsbreak():
    """conftest's guard: since M1 `ads_api` configures NewsBreak too, a test that forgets `nbapi` fails right here --
    past any broad `except` -- instead of sending the test token to NewsBreak. A test's own transport is allowed."""
    with pytest.raises(pytest.fail.Exception, match="nbapi"):
        newsbreak.client()
    with newsbreak.client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        assert newsbreak.ops_sent(http) == 0


# ---------------------------------------------------------------- ad accounts and the spending cap


def test_ad_accounts_lists_the_organisations_accounts(nbapi, settings):
    one, two = nbapi.add_account("Admiry · One · biz_1"), nbapi.add_account("Admiry · Two · biz_2")
    accounts = newsbreak.ad_accounts(settings, ORG)
    assert [(a["id"], a["name"]) for a in accounts] == [(one, "Admiry · One · biz_1"), (two, "Admiry · Two · biz_2")]
    assert nbapi.sent[-1] == ("ad-account/getGroupsByOrgIds", {"orgIds": [ORG]})
    assert nbapi.requests[-1].method == "GET"
    assert newsbreak.ad_accounts(settings, "1234") == []  # an organisation the token is not in lists nothing
    nbapi.fail["ad-account/getGroupsByOrgIds"] = envelope(
        0,
        data={
            "list": [
                {"id": "555", "name": "someone else", "adAccounts": [{"id": "9", "name": "theirs"}]},
                {"id": ORG, "name": "Admiry", "adAccounts": [{"id": "1", "name": "ours"}, "not an account"]},
            ]
        },
    )
    assert newsbreak.ad_accounts(settings, ORG) == [{"id": "1", "name": "ours"}]  # only the organisation asked for


def test_create_ad_account_sends_the_documented_fields_as_a_local_lead_generator(nbapi, settings):
    made = newsbreak.create_ad_account(
        settings,
        org_id=ORG,
        name="Admiry · Bright Smile · biz_1",
        company_name="Bright Smile Dental",
        industry="Health and Medical Services//Dental Care",
    )
    assert nbapi.bodies("ad-account/create") == [
        {
            "adAccountName": "Admiry · Bright Smile · biz_1",
            "companyName": "Bright Smile Dental",
            "industry": "Health and Medical Services//Dental Care",
            "accountCategory": "LEADGEN-LOCAL",
            "orgId": ORG,
        }
    ]
    assert (made["status"], made["paymentType"]) == ("ACTIVE", "PRE_PAID") and made["id"] in nbapi.accounts


def test_set_spending_cap_answers_newsbreaks_message_and_refuses_a_failed_update(nbapi, settings):
    account = nbapi.add_account("acct")
    assert (
        newsbreak.set_spending_cap(settings, account, 30_000) == "Budget is enabled and set successfully to 30000 cents"
    )
    assert nbapi.bodies("balance/updateAccountsBudget") == [
        {"adAccountsBudgetUpdate": [{"adAccountId": account, "budget": 30_000}]}
    ]
    assert newsbreak.set_spending_cap(settings, account, 60_000) == (
        "Budget updated successfully. From 30000 cents to 60000 cents"
    )
    nbapi.spent[account] = 70_000  # the cap is the account's lifetime cap: it must stay above what was spent
    e = refusal(lambda: newsbreak.set_spending_cap(settings, account, 65_000))
    assert (str(e), e.code, e.detail) == (
        newsbreak.REFUSED,
        None,
        "Update failed. New budget must be greater than current spending, current spending: 70000 cents",
    )
    assert nbapi.caps[account] == 60_000  # NewsBreak answered code 0 and changed nothing
    e = refusal(lambda: newsbreak.set_spending_cap(settings, "999", 65_000))
    assert e.detail == "Update failed. adAccount not found"


def test_a_token_that_is_not_an_org_admins_cannot_set_the_cap(nbapi, settings):
    """Only an Org Admin may set a cap; NewsBreak says so with code 0 and its failure message. An operator fixes the
    token, and the same request then goes through: the token's trouble, like code 403."""
    account = nbapi.add_account("acct")
    nbapi.org_admin = False
    e = refusal(lambda: newsbreak.set_spending_cap(settings, account, 30_000))
    assert (str(e), e.code, e.detail) == (
        newsbreak.ACCESS_TROUBLE,
        None,
        "Update failed. No access to change adAccount budget",
    )
    assert account not in nbapi.caps


def test_a_spending_cap_answer_without_the_accounts_row_is_refused(nbapi, settings):
    account = nbapi.add_account("acct")
    nbapi.fail["balance/updateAccountsBudget"] = envelope(0, data={"list": []})
    assert refusal(lambda: newsbreak.set_spending_cap(settings, account, 30_000)).owner == newsbreak.REFUSED


# ---------------------------------------------------------------- the account's total spend (M1 Task 5a, R5)


def test_total_spend_is_the_accounts_spend_so_far_in_cents_once_it_has_a_cap(nbapi, settings):
    """Get Account Spending Cap (`balance/getAccountBudgetInfo`): the figures of an account with a cap -- the lifetime
    cap a new one must exceed is checked against `accountTotalSpend`."""
    account = nbapi.add_account("acct")
    newsbreak.set_spending_cap(settings, account, 30_000)
    nbapi.spent[account] = 12_345
    assert newsbreak.total_spend(settings, account) == 12_345
    assert nbapi.bodies("balance/getAccountBudgetInfo") == [{"accountIds": [account]}]


def test_total_spend_reads_newsbreaks_decimal_figures_rounding_a_fraction_of_a_cent_up(nbapi, settings):
    account = nbapi.add_account("acct")
    for figure, cents in (("200.0", 200), (199.2, 200), ("0", 0), (7, 7)):
        row = {"accountId": account, "accountTotalSpend": figure, "canViewBudget": True}
        nbapi.fail["balance/getAccountBudgetInfo"] = envelope(0, data={"list": [row]})
        assert newsbreak.total_spend(settings, account) == cents, figure


def test_an_account_without_a_cap_shows_no_total_spend(nbapi, settings):
    """NewsBreak shows the figures only for an account with a cap ("No account spending cap is set for this
    account"): None, never a made-up zero -- the caller decides what an account Admiry never capped has spent."""
    account = nbapi.add_account("acct")
    assert newsbreak.total_spend(settings, account) is None


def test_a_total_spend_newsbreak_will_not_show_is_refused_with_its_reason_kept(nbapi, settings):
    account = nbapi.add_account("acct")
    nbapi.fail["balance/getAccountBudgetInfo"] = envelope(
        0, data={"list": [{"accountId": account, "canViewBudget": False, "failMessage": "No access"}]}
    )
    e = refusal(lambda: newsbreak.total_spend(settings, account))
    assert (e.owner, e.detail) == (newsbreak.ACCESS_TROUBLE, "No access")  # the token's to fix, as for the cap
    nbapi.fail["balance/getAccountBudgetInfo"] = envelope(
        0, data={"list": [{"accountId": account, "canViewBudget": False, "failMessage": "Something else"}]}
    )
    assert refusal(lambda: newsbreak.total_spend(settings, account)).owner == newsbreak.REFUSED
    for answer in ({"list": []}, {"list": [{"accountId": account, "canViewBudget": True}]}):
        nbapi.fail["balance/getAccountBudgetInfo"] = envelope(0, data=answer)
        assert refusal(lambda: newsbreak.total_spend(settings, account)).owner == newsbreak.REFUSED
    nbapi.fail["balance/getAccountBudgetInfo"] = envelope(
        0, data={"list": [{"accountId": account, "canViewBudget": True, "accountTotalSpend": "a lot"}]}
    )
    assert refusal(lambda: newsbreak.total_spend(settings, account)).owner == newsbreak.REFUSED


def test_the_lead_event_is_newsbreaks_documented_form_event():
    """No API creates an event: an operator makes it by hand in each ad account under exactly this name, which the
    postback's `event_type` must match (task-2-report fact 4)."""
    assert newsbreak.ADMIRY_LEAD_EVENT == "submit_form"


# ---------------------------------------------------------------- the asset upload


def test_upload_asset_sends_the_file_as_multipart_and_answers_the_cdn_url(nbapi, settings, tmp_path):
    account = nbapi.add_account("acct")
    image = tmp_path / "dentists_1.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0 a small jpeg")
    url = newsbreak.upload_asset(settings, account, image)
    request = nbapi.requests[-1]
    assert (request.method, request.url.path) == ("POST", "/business-api/v1/ad/uploadAssets")
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert request.headers["Access-Token"] == TOKEN
    assert nbapi.bodies("ad/uploadAssets") == [
        {
            "asset": {
                "filename": "dentists_1.jpg",
                "content_type": "image/jpeg",
                "content": b"\xff\xd8\xff\xe0 a small jpeg",
            },
            "adAccountId": account,
        }
    ]
    assert url.startswith(f"https://static.particlenews.com/nova/assets/{account}/") and url in nbapi.assets


def test_an_upload_answer_without_a_url_is_refused(nbapi, settings, tmp_path):
    account = nbapi.add_account("acct")
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG")
    nbapi.fail["ad/uploadAssets"] = envelope(0, data={"mediaId": "1"})
    assert refusal(lambda: newsbreak.upload_asset(settings, account, image)).owner == newsbreak.REFUSED
    nbapi.fail["ad/uploadAssets"] = envelope(4034, "Rate limit exceeded")
    assert refusal(lambda: newsbreak.upload_asset(settings, account, image)).owner == newsbreak.BUSY


def test_an_image_that_cannot_be_read_is_refused_without_its_path(nbapi, settings, tmp_path):
    account = nbapi.add_account("acct")
    for unreadable in (tmp_path / "private-folder" / "dentists_1.jpg", tmp_path):  # gone, and a directory
        with pytest.raises(NewsBreakError) as caught:
            newsbreak.upload_asset(settings, account, unreadable)
        e = caught.value
        assert (str(e), e.code, e.status) == (newsbreak.ASSET_UNREADABLE, None, None)
        assert e.detail in ("FileNotFoundError: No such file or directory", "IsADirectoryError: Is a directory")
        assert str(tmp_path) not in str(e) + repr(e) + e.detail and "private-folder" not in repr(e) + e.detail
    assert nbapi.requests == []  # nothing was sent


# ---------------------------------------------------------------- campaigns, ad sets, ads


def test_the_lists_read_every_page(nbapi, settings, monkeypatch):
    monkeypatch.setattr(newsbreak, "PAGE_SIZE", 5)
    account = nbapi.add_account("acct")
    for i in range(6):
        nbapi.add_campaign(account, f"c{i}")
    nbapi.add_campaign(nbapi.add_account("another business"), "theirs")
    assert [c["name"] for c in newsbreak.campaigns(settings, account)] == [f"c{i}" for i in range(6)]
    assert [(p["adAccountId"], p["pageNo"], p["pageSize"]) for p in nbapi.bodies("campaign/getList")] == [
        (account, "1", "5"),
        (account, "2", "5"),
    ]


def test_a_list_that_never_ends_is_refused_rather_than_followed_for_ever(nbapi, settings, monkeypatch):
    monkeypatch.setattr(newsbreak, "PAGE_SIZE", 5)
    monkeypatch.setattr(newsbreak, "MAX_PAGES", 2)
    account = nbapi.add_account("acct")
    for i in range(11):
        nbapi.add_campaign(account, f"c{i}")
    e = refusal(lambda: newsbreak.campaigns(settings, account))
    assert (e.owner, e.detail) == (newsbreak.REFUSED, newsbreak.PAGINATION_RUNAWAY)
    assert len(nbapi.requests) == 2


def test_a_lost_create_answer_leaves_the_object_findable_by_name(nbapi, settings):
    """What P3's recovery rests on: NewsBreak made it, the answer never came, the next list finds it."""
    account = nbapi.add_account("acct")
    nbapi.lose.add("campaign/create")
    assert refusal(lambda: newsbreak.create_campaign(settings, campaign_body(account))).owner == newsbreak.UNREACHABLE
    assert [c["name"] for c in newsbreak.campaigns(settings, account)] == ["Admiry · Bright Smile · biz_1"]


def test_creates_updates_and_lists_send_their_bodies_to_their_documented_paths(nbapi, settings, tmp_path):
    account = nbapi.add_account("acct")
    campaign = newsbreak.create_campaign(settings, campaign_body(account))["id"]
    ad_set_body = {
        "campaignId": campaign,
        "name": "Palo Alto ZIP codes",
        "budgetType": "DAILY",
        "budget": 1000,
        "startTime": 1_790_000_000,
        "endTime": 1_800_000_000,
        "bidType": "CPC",
        "bidRate": 150,
        "deliveryRate": "EVENLY",
        "targeting": {"location": {"positive": ["2886"]}, "language": {"positive": ["en_us"]}},
        "status": "OFF",
    }
    ad_set = newsbreak.create_ad_set(settings, ad_set_body)["id"]
    image = tmp_path / "dentists_1.jpg"
    image.write_bytes(b"\xff\xd8 jpeg")
    creative = {
        "type": "IMAGE",
        "headline": "Invisalign in {city}",
        "assetUrl": newsbreak.upload_asset(settings, account, image),
        "description": "Clear aligners from a family dentist.",
        "callToAction": "Get a quote",
        "brandName": "Bright Smile Dental",
        "clickThroughUrl": "https://brightsmile.example/?src=nb",
    }
    ad = newsbreak.create_ad(settings, {"adSetId": ad_set, "name": "ad 1", "creative": creative, "status": "OFF"})["id"]
    newsbreak.update_campaign(settings, campaign, {"name": "renamed"})
    newsbreak.update_ad_set(settings, ad_set, {"budget": 1500, "targeting": ad_set_body["targeting"]})  # always whole
    newsbreak.update_ad(settings, ad, {"creative": {"headline": "Same-week appointments"}})
    assert [(r.method, r.url.path) for r in nbapi.requests] == [
        ("POST", "/business-api/v1/campaign/create"),
        ("POST", "/business-api/v1/ad-set/create"),
        ("POST", "/business-api/v1/ad/uploadAssets"),
        ("POST", "/business-api/v1/ad/create"),
        ("PUT", f"/business-api/v1/campaign/update/{campaign}"),
        ("PUT", f"/business-api/v1/ad-set/update/{ad_set}"),
        ("PUT", f"/business-api/v1/ad/update/{ad}"),
    ]
    assert nbapi.bodies("ad-set/create") == [ad_set_body]
    assert nbapi.bodies("ad/create")[0]["creative"] == creative
    assert nbapi.campaigns[campaign]["name"] == "renamed" and nbapi.ad_sets[ad_set]["budget"] == 1500
    assert nbapi.ad_sets[ad_set]["targeting"]["location"] == {"positive": ["2886"]}  # the ZIP codes stay
    assert nbapi.ads[ad]["creative"]["content"]["headline"] == "Same-week appointments"
    assert [s["id"] for s in newsbreak.ad_sets(settings, account)] == [ad_set]
    listed = newsbreak.ads(settings, account)
    assert [(a["id"], a["auditStatus"], a["statusTxt"]) for a in listed] == [
        (ad, "PENDING", "Under review. It usually takes 1-2 work days.")
    ]
    assert [p["adAccountId"] for p in nbapi.bodies("ad-set/getList") + nbapi.bodies("ad/getList")] == [account] * 2


def test_an_ad_set_update_without_its_targeting_widens_it_to_everyone(nbapi, settings):
    """Update An Ad Set (37869314889229): `targeting` -- "If not specified, defaults to "all" (unlimited) for each
    dimension"; no merge with the stored targeting is promised. A budget-only update would take the business's ZIP
    codes away, so every ad-set update sends the whole targeting (Task 3's diff, Task 5's update)."""
    account = nbapi.add_account("acct")
    zips = {"location": {"positive": ["2886", "2895"]}, "language": {"positive": ["en_us"]}}
    body = {
        "campaignId": nbapi.add_campaign(account, "c"),
        "name": "s",
        "budgetType": "DAILY",
        "budget": 1000,
        "startTime": 1,
        "endTime": 2,
        "bidType": "CPC",
        "bidRate": 150,
        "targeting": zips,
    }
    ad_set = newsbreak.create_ad_set(settings, body)["id"]
    everyone = {dimension: {"positive": ["all"]} for dimension in fake_newsbreak.TARGETING_DIMENSIONS}
    assert nbapi.ad_sets[ad_set]["targeting"] == everyone | zips  # an unnamed dimension is "all" from the start
    newsbreak.update_ad_set(settings, ad_set, {"budget": 1500})
    assert nbapi.ad_sets[ad_set]["targeting"] == everyone  # the ZIP codes are gone
    assert nbapi.ad_sets[ad_set]["budget"] == 1500


def test_the_status_switches_put_on_or_off(nbapi, settings):
    account = nbapi.add_account("acct")
    campaign = nbapi.add_campaign(account, "c")
    ad_set = newsbreak.create_ad_set(
        settings,
        {
            "campaignId": campaign,
            "name": "s",
            "budgetType": "DAILY",
            "budget": 1000,
            "startTime": 1,
            "endTime": 2,
            "bidType": "CPC",
            "bidRate": 100,
            "status": "OFF",
        },
    )["id"]
    assert newsbreak.campaign_status(settings, campaign, False)["status"] == "OFF"
    assert newsbreak.ad_set_status(settings, ad_set, True)["status"] == "ON"
    nbapi.fail["ad/updateStatus"] = envelope(0, data={"id": "77", "status": "ON"})
    assert newsbreak.ad_status(settings, "77", True)["status"] == "ON"
    assert [(r.method, r.url.path) for r in nbapi.requests[-3:]] == [
        ("PUT", f"/business-api/v1/campaign/updateStatus/{campaign}"),
        ("PUT", f"/business-api/v1/ad-set/updateStatus/{ad_set}"),
        ("PUT", "/business-api/v1/ad/updateStatus/77"),
    ]
    assert [p for e, p in nbapi.sent[-3:]] == [{"status": "OFF"}, {"status": "ON"}, {"status": "ON"}]


def test_an_id_stays_one_path_segment(nbapi, settings):
    refusal(lambda: newsbreak.campaign_status(settings, "1/../../ad-account/create", True))
    assert (
        nbapi.requests[-1].url.raw_path == b"/business-api/v1/campaign/updateStatus/1%2F..%2F..%2Fad-account%2Fcreate"
    )


# ---------------------------------------------------------------- the report, the events, the postback


def test_report_asks_for_the_hourly_settlement_figures_by_day_and_ad_for_fixed_dates(nbapi, settings):
    account = nbapi.add_account("acct")
    in_range = {"date": "2026-09-30", "adAccountId": account, "adId": "5", "cost": 1234, "costDecimal": 1234.0}
    nbapi.report_rows = [in_range, {**in_range, "date": "2026-10-02"}, {**in_range, "adAccountId": "999"}]
    rows = newsbreak.report(
        settings, account, start=date(2026, 9, 28), end=date(2026, 10, 1), timezone="America/Los_Angeles"
    )
    assert rows == [in_range]  # as NewsBreak returned them
    assert nbapi.bodies("reports/getIntegratedReport") == [
        {
            "name": f"admiry {account} 2026-09-28..2026-10-01",
            "timezone": "America/Los_Angeles",
            "dateRange": "FIXED",
            "startDate": "2026-09-28",
            "endDate": "2026-10-01",
            "filter": "AD_ACCOUNT",
            "filterIds": [int(account)],
            "dataSource": "HOURLY",
            "dimensions": ["DATE", "AD"],
            "metrics": ["COST", "IMPRESSION", "CLICK", "CONVERSION"],
        }
    ]
    assert nbapi.requests[-1].method == "POST"
    newsbreak.report(
        settings, account, start=date(2026, 9, 30), end=date(2026, 9, 30), timezone=newsbreak.REPORT_TIMEZONE
    )
    assert nbapi.bodies("reports/getIntegratedReport")[-1]["timezone"] == "UTC"  # the zone for an unknown one


def test_a_report_without_the_accounts_zone_fails_before_any_request(nbapi, settings):
    """The days a report sums are its zone's: a caller that forgot the zone would settle and reconcile the wrong days
    without an error, so there is no default."""
    account = nbapi.add_account("acct")
    with pytest.raises(TypeError, match="timezone"):
        newsbreak.report(settings, account, start=date(2026, 9, 30), end=date(2026, 9, 30))
    assert nbapi.requests == []


@pytest.mark.parametrize("ad_account_id", ["act_1", None])
def test_an_ad_account_id_that_is_not_a_number_is_refused_before_any_request(nbapi, settings, ad_account_id):
    """`filterIds` are integers (Run a Synchronous Report): an id that is none is refused here, never a ValueError."""
    e = refusal(
        lambda: newsbreak.report(
            settings, ad_account_id, start=date(2026, 9, 30), end=date(2026, 9, 30), timezone="UTC"
        )
    )
    assert (str(e), e.code, e.status, e.detail) == (newsbreak.REFUSED, None, None, "the ad account id is not a number")
    assert nbapi.requests == []


def test_events_reads_the_accounts_events(nbapi, settings):
    account = nbapi.add_account("acct")
    lead = {"id": "77", "name": "Admiry lead", "type": "POSTBACK", "eventType": "submit_form"}
    nbapi.events[account] = [lead]
    assert newsbreak.events(settings, account) == [lead]
    assert (nbapi.requests[-1].method, nbapi.requests[-1].url.path) == (
        "GET",
        f"/business-api/v1/event/getList/{account}",
    )
    assert newsbreak.events(settings, nbapi.add_account("none yet")) == []


def test_send_conversion_is_the_postback_by_click_id_without_the_token(nbapi, settings):
    newsbreak.send_conversion(settings, click_id="nvss_abc", event_type="submit_form")
    newsbreak.send_conversion(settings, click_id="nvss_def", event_type="submit_form", value_cents=1250)
    assert nbapi.conversions == [
        {"callback": "nvss_abc", "event_type": "submit_form"},
        {"callback": "nvss_def", "event_type": "submit_form", "nb_value": "12.50"},
    ]
    request = nbapi.requests[-1]
    assert request.method == "GET" and str(request.url).startswith("https://business.newsbreak.com/tracking/attribute?")
    assert "Access-Token" not in request.headers


def test_a_postback_newsbreak_refuses_is_refused_and_any_success_will_do(nbapi, settings):
    nbapi.rejected_clicks.add("nvss_old")  # past its 7 days: "typically 400"
    e = refusal(lambda: newsbreak.send_conversion(settings, click_id="nvss_old", event_type="submit_form"))
    assert (str(e), e.status) == (newsbreak.REFUSED, 400)
    nbapi.fail["tracking/attribute"] = httpx.Response(200, text="ok")  # the docs do not say what it answers
    newsbreak.send_conversion(settings, click_id="nvss_new", event_type="submit_form")
    nbapi.fail["tracking/attribute"] = httpx.Response(502)
    assert refusal(lambda: newsbreak.send_conversion(settings, click_id="x", event_type="y")).owner == (
        newsbreak.UNREACHABLE
    )


# ---------------------------------------------------------------- the facts pinned from NewsBreak's docs


def test_every_category_has_an_industry_value_of_newsbreaks_list():
    assert set(newsbreak.INDUSTRIES) == set(typing.get_args(models.Category))
    assert all(re.fullmatch(r"[^/]+//[^/]+", v) for v in newsbreak.INDUSTRIES.values())  # <category>//<subcategory>
    assert newsbreak.industry("dentists") == "Health and Medical Services//Dental Care"
    assert newsbreak.industry("attorneys") == "Legal Services//Lawyers"
    assert newsbreak.industry("not a category") == newsbreak.industry(None) == newsbreak.INDUSTRIES["fallback"]
    assert newsbreak.INDUSTRIES["fallback"] == "Business and Industrial//Small Business"


def test_the_native_ads_text_limits_hold_the_plans_buttons():
    assert (newsbreak.HEADLINE_MAX, newsbreak.DESCRIPTION_MAX, newsbreak.BRAND_NAME_MAX) == (90, 90, 25)
    assert newsbreak.CALL_TO_ACTION_MAX == 15
    assert all(len(cta) <= newsbreak.CALL_TO_ACTION_MAX for cta in ("Call now", "Get a quote"))  # plan P5's two


# ---------------------------------------------------------------- the operator's probe (agent.ops nb-probe)


def test_nb_probe_needs_the_token_and_the_organisation(nbapi, capsys, monkeypatch):
    for unset in ("NEWSBREAK_ORG_ID", "NEWSBREAK_ACCESS_TOKEN"):  # first the organisation alone, then both
        monkeypatch.delenv(unset)
        assert ops.main(["nb-probe"]) == 1
        assert "NEWSBREAK_ACCESS_TOKEN and NEWSBREAK_ORG_ID" in capsys.readouterr().err
    assert nbapi.requests == []


def test_nb_probe_lists_the_organisations_ad_accounts_and_makes_none(nbapi, capsys):
    one = nbapi.add_account("Admiry · probe")
    assert ops.main(["nb-probe"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == [f"ad_account: {one} Admiry · probe created {nbapi.accounts[one]['createTime']}"]
    assert f"1 ad account(s) in organisation {ORG}" in out.err
    assert nbapi.kinds() == ["ad-account/getGroupsByOrgIds"]
    assert TOKEN not in out.out + out.err


def test_nb_probe_creates_an_account_and_prints_newsbreaks_answer(nbapi, capsys):
    argv = ["nb-probe", "--create-account", "Admiry · probe", "--company", "Admiry Inc", "--category", "dentists"]
    assert ops.main(argv) == 0
    assert nbapi.bodies("ad-account/create") == [
        {
            "adAccountName": "Admiry · probe",
            "companyName": "Admiry Inc",
            "industry": "Health and Medical Services//Dental Care",
            "accountCategory": "LEADGEN-LOCAL",
            "orgId": ORG,
        }
    ]
    (made,) = nbapi.accounts.values()
    out = capsys.readouterr()
    assert out.out.splitlines() == [
        f"id: {made['id']}",
        "name: Admiry · probe",
        "status: ACTIVE",
        "paymentType: PRE_PAID",
        "accountCategory: LEADGEN-LOCAL",
        "industry: Health and Medical Services//Dental Care",
        "companyName: Admiry Inc",
        "billingThreshold: 0",
    ]
    assert f"created ad account {made['id']} in organisation {ORG}: status ACTIVE" in out.err


def test_nb_probe_defaults_the_company_to_the_name_and_the_industry_to_the_fallback(nbapi, capsys):
    assert ops.main(["nb-probe", "--create-account", "Admiry · probe"]) == 0
    body = nbapi.bodies("ad-account/create")[0]
    assert (body["companyName"], body["industry"]) == ("Admiry · probe", newsbreak.INDUSTRIES["fallback"])


def test_nb_probe_says_when_a_new_account_waits_at_newsbreak(nbapi, capsys):
    nbapi.account_status = "PENDING"
    assert ops.main(["nb-probe", "--create-account", "Admiry · probe"]) == 0
    err = capsys.readouterr().err
    assert "status PENDING, not ACTIVE: the new account waits for a step at NewsBreak" in err


def test_nb_probe_never_makes_a_second_account_of_one_name(nbapi, capsys):
    existing = nbapi.add_account("Admiry · probe")
    assert ops.main(["nb-probe", "--create-account", "Admiry · probe"]) == 1
    assert nbapi.bodies("ad-account/create") == []
    assert f"an ad account named 'Admiry · probe' exists already ({existing})" in capsys.readouterr().err


def test_nb_probe_prints_newsbreaks_reason_without_the_token(nbapi, capsys):
    nbapi.fail["ad-account/create"] = envelope(40001, f"industry invalid (token {TOKEN})")
    assert ops.main(["nb-probe", "--create-account", "Admiry · probe"]) == 1
    err = capsys.readouterr().err
    assert newsbreak.REFUSED_WITH_REASON.format(40001) in err
    assert "NewsBreak said: industry invalid (token [token])" in err and TOKEN not in err


def test_nb_probe_reports_a_token_newsbreak_refuses(nbapi, capsys):
    nbapi.token = "another token"
    assert ops.main(["nb-probe"]) == 1
    err = capsys.readouterr().err
    assert f"{newsbreak.ACCESS_TROUBLE} (code 4033, HTTP 200) -- NewsBreak said: Invalid access token" in err


def test_nb_probe_takes_only_a_known_category(nbapi):
    with pytest.raises(SystemExit):
        ops.main(["nb-probe", "--create-account", "Admiry · probe", "--category", "plumbers"])
    assert nbapi.requests == []
