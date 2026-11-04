"""agent.ops ads-probe (spec 2026-09-28 admiry-google-search-execution-design §13; A3 Task 1): the operator spike
that reads a handful of account fields and, with --validate-campaign, sends a validateOnly mutate of a paused
budget + campaign — proof the REST client, the account's access, and a real mutate shape all work before any
owner's plan is built. Google is a MockTransport; no network, no database."""

from __future__ import annotations

import json

import httpx
import pytest

from agent import google_auth, ops
from agent.channels.google_search import build as ads_launch
from agent.channels.google_search import client as google_ads
from agent.tests.conftest import ADS_ENV

CID = "1234567890"
SECRETS = ("devtok_SECRET", "1//refresh_SECRET", "ya29.SECRET", "GOCSPX-x")
TOKEN_OK = httpx.Response(200, json={"access_token": "ya29.SECRET", "expires_in": 3599, "token_type": "Bearer"})

CUSTOMER_ROW = {
    "customer": {
        "descriptiveName": "Bright Smile Dental",
        "currencyCode": "USD",
        "timeZone": "America/Chicago",
        "testAccount": False,
        "conversionTrackingSetting": {"conversionTrackingId": "123456789"},
        "callReportingSetting": {"callReportingEnabled": True, "callConversionReportingEnabled": False},
    }
}


BIDDING = ("targetSpend", "manualCpc", "maximizeConversions", "maximizeConversionValue", "targetCpa", "targetRoas")


def _refused(code: dict) -> httpx.Response:
    return httpx.Response(
        400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "details": [{"errors": [{"errorCode": code}]}]}}
    )


def fake_transport(sent: list, responses: dict) -> httpx.Client:
    """A client whose transport answers the token endpoint, then whichever reply's key suffixes the request URL.
    Like v25, it refuses a search asking for a page size, and a Search campaign created with no bidding strategy
    (A3 final review C1, I4)."""

    def handler(request):
        sent.append(request)
        url = str(request.url)
        if url == google_auth.TOKEN_URL:
            return TOKEN_OK
        body = json.loads(request.content)
        if url.endswith("googleAds:search") and "pageSize" in body:
            return _refused({"requestError": "PAGE_SIZE_NOT_SUPPORTED"})
        for op in body.get("mutateOperations") or []:
            campaign = (op.get("campaignOperation") or {}).get("create")
            if campaign is not None and not any(k in campaign for k in BIDDING):
                return _refused({"fieldError": "REQUIRED"})
        reply = next((r for suffix, r in responses.items() if url.endswith(suffix)), httpx.Response(404))
        return reply(request) if callable(reply) else reply

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def probe_env(monkeypatch):
    """Every Google Ads setting present (the ads-probe command needs the client's own keys; it is not gated by
    google_ads_link_mode, since it is run directly against a Customer ID, linked or not)."""
    for name, value in ADS_ENV.items():
        monkeypatch.setenv(name, value)


def patched_client(monkeypatch, responses: dict) -> list:
    """Patch google_ads.client itself (not http= injection: agent.ops has no such parameter, matching the CLI's
    own shape) so ops.main's real argparse dispatch is exercised end to end."""
    sent: list = []
    monkeypatch.setattr(google_ads, "client", lambda: fake_transport(sent, responses))
    return sent


def test_ads_probe_prints_the_account_fields(monkeypatch, capsys, probe_env):
    sent = patched_client(monkeypatch, {"googleAds:search": httpx.Response(200, json={"results": [CUSTOMER_ROW]})})
    assert ops.main(["ads-probe", CID]) == 0
    out = capsys.readouterr().out
    assert "descriptive_name: Bright Smile Dental" in out
    assert "currency_code: USD" in out
    assert "time_zone: America/Chicago" in out
    assert "test_account: False" in out
    assert "conversion_tracking_id: 123456789" in out
    assert "call_reporting_enabled: True" in out
    assert "call_conversion_reporting_enabled: False" in out
    assert not any(s in out for s in SECRETS)
    search = next(r for r in sent if str(r.url).endswith("googleAds:search"))
    assert str(search.url) == f"https://googleads.googleapis.com/v25/customers/{CID}/googleAds:search"
    assert set(json.loads(search.content)) == {"query"}
    query = json.loads(search.content)["query"]
    for field in (
        "customer.descriptive_name",
        "customer.currency_code",
        "customer.time_zone",
        "customer.test_account",
        "customer.conversion_tracking_setting.conversion_tracking_id",
        "customer.call_reporting_setting.call_reporting_enabled",
        "customer.call_reporting_setting.call_conversion_reporting_enabled",
    ):
        assert field in query


def test_ads_probe_accepts_dashes_in_the_customer_id(monkeypatch, capsys, probe_env):
    sent = patched_client(monkeypatch, {"googleAds:search": httpx.Response(200, json={"results": [CUSTOMER_ROW]})})
    assert ops.main(["ads-probe", "123-456-7890"]) == 0
    search = next(r for r in sent if str(r.url).endswith("googleAds:search"))
    assert str(search.url) == f"https://googleads.googleapis.com/v25/customers/{CID}/googleAds:search"


def test_ads_probe_refuses_a_bad_customer_id_without_calling_google(capsys, probe_env):
    assert ops.main(["ads-probe", "not-a-number"]) == 1
    assert "10 digits" in capsys.readouterr().err


def test_ads_probe_with_no_customer_row_is_a_clean_failure(monkeypatch, capsys, probe_env):
    patched_client(monkeypatch, {"googleAds:search": httpx.Response(200, json={"results": []})})
    assert ops.main(["ads-probe", CID]) == 1
    assert f"no customer {CID}" in capsys.readouterr().err


def test_ads_probe_validate_campaign_sends_a_paused_budget_and_campaign_and_prints_ok(monkeypatch, capsys, probe_env):
    mutate_resp = httpx.Response(
        200,
        json={
            "mutateOperationResponses": [
                {"campaignBudgetResult": {"resourceName": f"customers/{CID}/campaignBudgets/111"}},
                {"campaignResult": {"resourceName": f"customers/{CID}/campaigns/222"}},
            ]
        },
    )
    sent = patched_client(
        monkeypatch,
        {
            "googleAds:search": httpx.Response(200, json={"results": [CUSTOMER_ROW]}),
            "googleAds:mutate": mutate_resp,
        },
    )
    assert ops.main(["ads-probe", CID, "--validate-campaign"]) == 0
    out = capsys.readouterr().out
    assert "validate: ok" in out
    mutate_req = next(r for r in sent if str(r.url).endswith("googleAds:mutate"))
    assert str(mutate_req.url) == f"https://googleads.googleapis.com/v25/customers/{CID}/googleAds:mutate"
    body = json.loads(mutate_req.content)
    assert body["partialFailure"] is False and body["validateOnly"] is True
    ops_sent = body["mutateOperations"]
    assert len(ops_sent) == 2
    budget_name = ops_sent[0]["campaignBudgetOperation"]["create"]["resourceName"]
    assert budget_name == f"customers/{CID}/campaignBudgets/-1"
    campaign_create = ops_sent[1]["campaignOperation"]["create"]
    assert campaign_create["name"] == "Admiry · probe"
    assert campaign_create["status"] == "PAUSED"
    assert campaign_create["advertisingChannelType"] == "SEARCH"
    assert campaign_create["campaignBudget"] == budget_name
    assert campaign_create["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    # A3 final review I4: the launch's own shape, built by its own builders -- Maximize Clicks with a ceiling, Google
    # Search only, people in the area -- so the probe proves what a launch will send
    assert campaign_create["targetSpend"] == {"cpcBidCeilingMicros": 2_000_000}
    assert campaign_create["networkSettings"] == {
        "targetGoogleSearch": True,
        "targetSearchNetwork": False,
        "targetContentNetwork": False,
        "targetPartnerSearchNetwork": False,
    }
    assert campaign_create["geoTargetTypeSetting"] == {
        "positiveGeoTargetType": "PRESENCE",
        "negativeGeoTargetType": "PRESENCE",
    }
    assert ops_sent == [
        ads_launch.budget_op(budget_name, "Admiry · probe budget", 10_000_000),
        ads_launch.campaign_op(f"customers/{CID}/campaigns/-2", "Admiry · probe", budget_name, 2_000_000),
    ]
    assert not any(s in out for s in SECRETS)


def test_the_probe_campaign_has_the_shape_a_launch_builds():
    """I4: the probe's campaign and a launch's differ only in their values -- the same keys, the same settings."""
    plan = {
        "business_id": "biz_9",
        "sections": {
            "consultation": {"business": {"category": "fallback", "service": "Repairs", "services": ["Repairs"]}},
            "ads": {
                "channels": [
                    {
                        "channel": "google_search",
                        "monthly_usd": 300.0,
                        "themes": ["repairs"],
                        "negatives": [],
                        "headlines": ["Fast Repairs Near You", "Same-Day Service", "Call For A Free Quote"],
                        "descriptions": ["We fix it fast and fair.", "Licensed, insured, always on time."],
                        "landing_service": "Repairs",
                    }
                ]
            },
            "estimate": {"assumptions": [{"name": "cost per lead", "value": "$5-9"}]},  # the ceiling's $2 floor
        },
    }
    target = ads_launch.Target(kind="proximity", lat=30.0, lng=-97.0, radius_miles=5.0, geo_targets=())
    account = ads_launch.Account(customer_id=CID, tag=None, conversion_actions={})
    launch = ads_launch.operations(
        plan, {"name": "Fixit"}, page_url="https://fixit.example", target=target, account=account, customer_id=CID
    )
    launched = next(op for op in launch if "campaignOperation" in op)["campaignOperation"]["create"]
    budget = f"customers/{CID}/campaignBudgets/-1"
    probe = ads_launch.campaign_op(f"customers/{CID}/campaigns/-2", ops.PROBE_CAMPAIGN, budget, 2_000_000)
    probe = probe["campaignOperation"]["create"]
    assert set(probe) == set(launched)
    assert {k: v for k, v in probe.items() if k != "name"} == {k: v for k, v in launched.items() if k != "name"}


def test_ads_probe_without_validate_campaign_never_mutates(monkeypatch, capsys, probe_env):
    def fail_on_mutate(request):
        raise AssertionError("ads-probe without --validate-campaign must never mutate")

    sent = patched_client(
        monkeypatch,
        {"googleAds:search": httpx.Response(200, json={"results": [CUSTOMER_ROW]}), "googleAds:mutate": fail_on_mutate},
    )
    assert ops.main(["ads-probe", CID]) == 0
    assert not any(str(r.url).endswith("googleAds:mutate") for r in sent)


def test_ads_probe_prints_the_ads_error_to_stderr_and_leaks_nothing(monkeypatch, capsys, probe_env):
    patched_client(monkeypatch, {"googleAds:search": httpx.Response(403, json={"error": {"code": 403}})})
    assert ops.main(["ads-probe", CID]) == 1
    captured = capsys.readouterr()
    assert google_ads.ACCESS_TROUBLE in captured.err
    assert not any(s in captured.out + captured.err for s in SECRETS)


def test_ads_probe_validate_campaign_prints_the_ads_error_when_the_mutate_is_refused(monkeypatch, capsys, probe_env):
    patched_client(
        monkeypatch,
        {
            "googleAds:search": httpx.Response(200, json={"results": [CUSTOMER_ROW]}),
            "googleAds:mutate": httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "details": [{"errors": [{"errorCode": {"campaignError": "DUPLICATE_CAMPAIGN_NAME"}}]}],
                    }
                },
            ),
        },
    )
    assert ops.main(["ads-probe", CID, "--validate-campaign"]) == 1
    captured = capsys.readouterr()
    assert "campaignError: DUPLICATE_CAMPAIGN_NAME" in captured.err
    assert "validate: ok" not in captured.out
    assert not any(s in captured.out + captured.err for s in SECRETS)
