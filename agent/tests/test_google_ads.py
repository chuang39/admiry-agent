"""The Google Ads REST client (spec 2026-09-28 admiry-google-search-execution-design, A3 Task 1): the access-token
exchange, GAQL search pagination, mutate with validateOnly, header rules, geo-target suggestions, and Google's
errors mapped to owner sentences that never carry a token, a code's raw JSON, or Google's own wording."""

from __future__ import annotations

import json

import httpx
import pytest

from agent import config, google_auth
from agent.channels.google_search import client as google_ads
from agent.channels.google_search import refusals
from agent.channels.google_search.client import AdsError

CID = "1234567890"
MCC = "1112223333"
SECRETS = ("devtok_SECRET", "1//refresh_SECRET", "ya29.SECRET", "GOCSPX-x")
TOKEN_OK = httpx.Response(200, json={"access_token": "ya29.SECRET", "expires_in": 3599, "token_type": "Bearer"})
CUSTOMER_GAQL = "SELECT customer.descriptive_name FROM customer"


def fake_client(sent: list, *, token=None, responses: dict | None = None) -> httpx.Client:
    """A client whose transport answers like Google: the token endpoint, then whichever reply's key suffixes the
    requested URL. A reply may be a callable (it gets the request; raising stands for a network failure)."""
    responses = responses or {}

    def handler(request):
        sent.append(request)
        url = str(request.url)
        if url == google_auth.TOKEN_URL:
            reply = token or TOKEN_OK
        else:
            reply = next((r for suffix, r in responses.items() if url.endswith(suffix)), httpx.Response(404))
        return reply(request) if callable(reply) else reply

    return httpx.Client(transport=httpx.MockTransport(handler))


def _network(request):
    raise httpx.ConnectError("connection refused", request=request)


def _undecodable(request):
    raise httpx.DecodingError("bad gzip devtok_SECRET", request=request)  # an HTTPError that is no TransportError


def failure(status: int, code: dict | None = None, rpc_status: str = "INVALID_ARGUMENT") -> httpx.Response:
    """Google's error body; its own message carries a secret the client must never repeat."""
    details = [{"errors": [{"errorCode": code, "message": "raw words with devtok_SECRET"}]}] if code else []
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": "raw devtok_SECRET", "status": rpc_status, "details": details}},
    )


# ---------------------------------------------------------------- headers


def test_headers_omit_developer_token_when_unset():
    settings = config.load()  # nothing set in the environment: no ads keys
    h = google_ads.headers(settings, "ya29.SECRET")
    assert h["Authorization"] == "Bearer ya29.SECRET"
    assert h["login-customer-id"] == ""
    assert "developer-token" not in h


def test_headers_include_developer_token_when_set(ads_api):
    h = google_ads.headers(ads_api, "ya29.SECRET")
    assert h["developer-token"] == "devtok_SECRET" and h["login-customer-id"] == MCC


# ---------------------------------------------------------------- search


def test_search_follows_next_page_token_and_returns_every_row(ads_api):
    sent: list = []
    page1 = httpx.Response(200, json={"results": [{"customer": {"descriptiveName": "A"}}], "nextPageToken": "p2"})
    page2 = httpx.Response(200, json={"results": [{"customer": {"descriptiveName": "B"}}]})
    calls = iter([page1, page2])
    http = fake_client(sent, responses={"googleAds:search": lambda request: next(calls)})
    rows = google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert rows == [{"customer": {"descriptiveName": "A"}}, {"customer": {"descriptiveName": "B"}}]
    searches = [r for r in sent if str(r.url).endswith("googleAds:search")]
    assert len(searches) == 2
    assert str(searches[0].url) == f"https://googleads.googleapis.com/v25/customers/{CID}/googleAds:search"
    # A3 final review C1: v25 refuses `pageSize` (PAGE_SIZE_NOT_SUPPORTED, pages are a fixed 10,000 rows) -- the
    # body is the query alone, plus the page token after the first page
    assert json.loads(searches[0].content) == {"query": CUSTOMER_GAQL}
    assert json.loads(searches[1].content) == {"query": CUSTOMER_GAQL, "pageToken": "p2"}
    assert searches[0].headers["login-customer-id"] == MCC and searches[0].headers["developer-token"] == "devtok_SECRET"


def test_search_with_no_results_key_is_an_empty_list(ads_api):
    http = fake_client([], responses={"googleAds:search": httpx.Response(200, json={})})
    assert google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http) == []


def test_search_stops_with_an_ads_error_when_the_next_page_token_repeats(ads_api):
    """A Google hiccup that echoes the same nextPageToken forever must not hang a 20s approval-request caller or
    drain the daily operation quota (later tasks call search from both)."""
    sent: list = []
    same_forever = httpx.Response(200, json={"results": [], "nextPageToken": "same"})
    http = fake_client(sent, responses={"googleAds:search": same_forever})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert e.value.code == google_ads.PAGINATION_RUNAWAY and str(e.value) == google_ads.REFUSED
    searches = [r for r in sent if str(r.url).endswith("googleAds:search")]
    assert len(searches) == 2  # the repeat is caught as soon as it is seen: page 1, then the repeating page 2


def test_search_stops_with_an_ads_error_at_the_page_ceiling(ads_api):
    """Distinct tokens that never end (not a repeat, just an endless result) are bounded by MAX_PAGES all the
    same: 100 x Google's fixed 10,000-row page is far beyond anything this client reads."""
    sent: list = []
    counter = iter(range(1, google_ads.MAX_PAGES + 10))

    def never_ending(request):
        return httpx.Response(200, json={"results": [], "nextPageToken": f"token-{next(counter)}"})

    http = fake_client(sent, responses={"googleAds:search": never_ending})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert e.value.code == google_ads.PAGINATION_RUNAWAY
    searches = [r for r in sent if str(r.url).endswith("googleAds:search")]
    assert len(searches) == google_ads.MAX_PAGES


# ---------------------------------------------------------------- mutate


def test_mutate_sends_partial_failure_off_and_the_validate_only_flag(ads_api):
    sent: list = []
    operations = [{"campaignBudgetOperation": {"create": {"name": "x"}}}]
    resp = httpx.Response(200, json={"mutateOperationResponses": [{"campaignBudgetResult": {"resourceName": "x"}}]})
    http = fake_client(sent, responses={"googleAds:mutate": resp})
    out = google_ads.mutate(ads_api, CID, operations, validate_only=True, http=http)
    assert out == [{"campaignBudgetResult": {"resourceName": "x"}}]
    mutate_req = next(r for r in sent if str(r.url).endswith("googleAds:mutate"))
    assert str(mutate_req.url) == f"https://googleads.googleapis.com/v25/customers/{CID}/googleAds:mutate"
    assert json.loads(mutate_req.content) == {
        "mutateOperations": operations,
        "partialFailure": False,
        "validateOnly": True,
    }


def test_mutate_live_sends_validate_only_false(ads_api):
    sent: list = []
    resp = httpx.Response(200, json={"mutateOperationResponses": []})
    http = fake_client(sent, responses={"googleAds:mutate": resp})
    google_ads.mutate(ads_api, CID, [], validate_only=False, http=http)
    mutate_req = next(r for r in sent if str(r.url).endswith("googleAds:mutate"))
    assert json.loads(mutate_req.content)["validateOnly"] is False


def test_mutate_with_no_responses_key_is_an_empty_list(ads_api):
    http = fake_client([], responses={"googleAds:mutate": httpx.Response(200, json={})})
    assert google_ads.mutate(ads_api, CID, [], validate_only=True, http=http) == []


def test_mutate_logs_the_operation_count_at_info_without_bodies(ads_api, caplog):
    """Global Constraint (operation counting): every mutate logs len(operations) at INFO, never the bodies."""
    resp = httpx.Response(200, json={"mutateOperationResponses": []})
    http = fake_client([], responses={"googleAds:mutate": resp})
    operations = [
        {"campaignBudgetOperation": {"create": {"name": "a secret campaign name"}}},
        {"campaignOperation": {"create": {"name": "another secret name"}}},
    ]
    with caplog.at_level("INFO", logger="agent.channels.google_search.client"):
        google_ads.mutate(ads_api, CID, operations, validate_only=True, http=http)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert f"customer_id={CID}" in logged and "operations=2" in logged and "validate_only=True" in logged
    assert "secret campaign name" not in logged and "another secret name" not in logged


def test_mutate_logs_even_when_the_call_fails(ads_api, caplog):
    http = fake_client([], responses={"googleAds:mutate": failure(503)})
    with caplog.at_level("INFO", logger="agent.channels.google_search.client"), pytest.raises(AdsError):
        google_ads.mutate(ads_api, CID, [{"campaignOperation": {"create": {}}}], validate_only=False, http=http)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "operations=1" in logged and "validate_only=False" in logged


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "response, owner",
    [
        (failure(401), google_ads.ACCESS_TROUBLE),
        (failure(403), google_ads.ACCESS_TROUBLE),
        (failure(429, rpc_status="RESOURCE_EXHAUSTED"), google_ads.BUSY),
        (failure(503), google_ads.UNREACHABLE),
    ],
)
def test_error_mapping_by_status(ads_api, response, owner):
    http = fake_client([], responses={"googleAds:search": response})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert str(e.value) == owner and e.value.code is None
    assert not any(s in str(e.value) for s in SECRETS)
    assert e.value.__cause__ is None  # no chained request or response


def test_400_with_an_error_code_carries_the_code_without_the_raw_json(ads_api):
    response = failure(400, {"campaignError": "DUPLICATE_CAMPAIGN_NAME"})
    http = fake_client([], responses={"googleAds:search": response})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert e.value.code == "campaignError: DUPLICATE_CAMPAIGN_NAME"
    assert e.value.codes == ("campaignError: DUPLICATE_CAMPAIGN_NAME",)
    assert e.value.status == 400
    assert "campaignError: DUPLICATE_CAMPAIGN_NAME" in str(e.value)
    assert "{" not in str(e.value) and "errorCode" not in str(e.value)
    assert not any(s in str(e.value) for s in SECRETS)


def failure_multi(status: int, codes: list[dict]) -> httpx.Response:
    """Like failure(), but with several details[].errors[].errorCode entries — Google sometimes fails a request
    for more than one reason at once."""
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": "raw devtok_SECRET",
                "status": "INVALID_ARGUMENT",
                "details": [{"errors": [{"errorCode": c, "message": "raw devtok_SECRET"} for c in codes]}],
            }
        },
    )


def test_a_multi_code_failure_keeps_every_code_not_just_the_first(ads_api):
    response = failure_multi(400, [{"fieldError": "REQUIRED"}, {"campaignError": "DUPLICATE_CAMPAIGN_NAME"}])
    http = fake_client([], responses={"googleAds:search": response})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert e.value.codes == ("fieldError: REQUIRED", "campaignError: DUPLICATE_CAMPAIGN_NAME")
    assert e.value.code == "fieldError: REQUIRED"  # the first, for callers that only look at one


def test_google_ads_link_translator_recognises_a_later_code_in_a_multi_code_failure():
    """google_ads_link._from_ads_error must match against every code, not just AdsError.code (the first) --
    otherwise a response that fails for two reasons at once could miss the one this module cares about. Tested
    here (not in test_google_ads_link.py, which stays byte-for-byte as it was) because the translator is
    importable on its own, with no Google call needed."""
    from agent import google_ads_link

    e = AdsError(
        "unused owner text",
        codes=("fieldError: REQUIRED", "managerLinkError: ALREADY_MANAGED_BY_THIS_MANAGER"),
        status=400,
    )
    translated = google_ads_link._from_ads_error(e)
    assert isinstance(translated, google_ads_link._Managed)
    assert str(translated) == google_ads_link.ALREADY_LINKED


def test_400_with_no_parseable_code_is_a_generic_refusal(ads_api):
    http = fake_client([], responses={"googleAds:search": failure(400)})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert e.value.code is None and e.value.status == 400
    assert not any(s in str(e.value) for s in SECRETS)


@pytest.mark.parametrize("break_it", [_network, _undecodable])
def test_transport_failures_are_unreachable_without_secrets(ads_api, break_it):
    http = fake_client([], responses={"googleAds:search": break_it})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert str(e.value) == google_ads.UNREACHABLE
    assert not any(s in str(e.value) for s in SECRETS)


def test_a_token_endpoint_refusal_is_access_trouble(ads_api):
    http = fake_client([], token=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert str(e.value) == google_ads.ACCESS_TROUBLE
    assert not any(s in str(e.value) for s in SECRETS)


def test_a_token_endpoint_5xx_is_unreachable(ads_api):
    http = fake_client([], token=httpx.Response(503, json={"error": "backendError"}))
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert str(e.value) == google_ads.UNREACHABLE


def test_no_secret_from_the_settings_ever_reaches_an_error(ads_api):
    """The ADS_ENV leak check (grep target for other modules' tests), extended to google_ads."""
    from agent.tests.conftest import ADS_SECRETS

    http = fake_client([], responses={"googleAds:search": failure(403)})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert not any(v in str(e.value) for v in ADS_SECRETS)


# ---------------------------------------------------------------- suggest_geo_targets (ruling S1)


def test_suggest_geo_targets_posts_locale_and_country_and_returns_resource_names(ads_api):
    sent: list = []
    resp = httpx.Response(
        200,
        json={
            "geoTargetConstantSuggestions": [
                {"geoTargetConstant": {"resourceName": "geoTargetConstants/1023191"}},
                {"geoTargetConstant": {"resourceName": "geoTargetConstants/1014448"}},
            ]
        },
    )
    http = fake_client(sent, responses={"geoTargetConstants:suggest": resp})
    out = google_ads.suggest_geo_targets(ads_api, ["Austin, TX", "Round Rock, TX"], http=http)
    assert out == ["geoTargetConstants/1023191", "geoTargetConstants/1014448"]
    req = next(r for r in sent if str(r.url).endswith("geoTargetConstants:suggest"))
    assert str(req.url) == "https://googleads.googleapis.com/v25/geoTargetConstants:suggest"
    assert json.loads(req.content) == {
        "locale": "en",
        "countryCode": "US",
        "locationNames": {"names": ["Austin, TX", "Round Rock, TX"]},
    }
    assert req.headers["login-customer-id"] == MCC
    assert req.headers["Authorization"] == "Bearer ya29.SECRET"
    assert req.headers["developer-token"] == "devtok_SECRET"


def test_suggest_geo_targets_with_no_suggestions_is_an_empty_list(ads_api):
    http = fake_client([], responses={"geoTargetConstants:suggest": httpx.Response(200, json={})})
    assert google_ads.suggest_geo_targets(ads_api, ["Nowhere"], http=http) == []


def test_suggest_geo_targets_omits_developer_token_when_unset():
    settings = config.load()  # nothing set in the environment: no ads keys
    sent: list = []
    http = fake_client(sent, responses={"geoTargetConstants:suggest": httpx.Response(200, json={})})
    google_ads.suggest_geo_targets(settings, ["Nowhere"], http=http)
    req = next(r for r in sent if str(r.url).endswith("geoTargetConstants:suggest"))
    assert req.headers["login-customer-id"] == ""
    assert req.headers["Authorization"] == "Bearer ya29.SECRET"
    assert "developer-token" not in req.headers


# ---------------------------------------------------------------- temp / micros


def test_temp_resource_name():
    assert google_ads.temp("campaignBudgets", -1, CID) == f"customers/{CID}/campaignBudgets/-1"
    assert google_ads.temp("campaigns", -2, CID) == f"customers/{CID}/campaigns/-2"


@pytest.mark.parametrize(
    "usd, micros", [(10.0, 10_000_000), (2.5, 2_500_000), (0.01, 10_000), (12.34, 12_340_000), (20, 20_000_000)]
)
def test_micros_round_trip(usd, micros):
    assert google_ads.to_micros(usd) == micros
    assert google_ads.from_micros(micros) == float(usd)


# ---------------------------------------------------------------- AdsError itself


def test_ads_error_str_is_the_owner_sentence_and_carries_codes_and_status():
    e = AdsError("try again later", codes=("campaignError: DUPLICATE_CAMPAIGN_NAME",), status=400)
    assert str(e) == "try again later" and e.owner == "try again later"
    assert e.code == "campaignError: DUPLICATE_CAMPAIGN_NAME"
    assert e.codes == ("campaignError: DUPLICATE_CAMPAIGN_NAME",) and e.status == 400


def test_ads_error_defaults_code_status_and_codes_to_empty():
    e = AdsError("try again later")
    assert e.code is None and e.status is None and e.codes == ()


def test_ads_error_code_is_the_first_of_several_codes():
    """`code` is a convenience view of `codes[0]`, not an independently settable field -- the two can never
    disagree (the review's "code stays the first")."""
    e = AdsError("try again later", codes=("fieldError: REQUIRED", "campaignError: DUPLICATE_CAMPAIGN_NAME"))
    assert e.code == "fieldError: REQUIRED"
    assert e.codes == ("fieldError: REQUIRED", "campaignError: DUPLICATE_CAMPAIGN_NAME")


# ---------------------------------------------------------------- config: AGENT_ADS_EXECUTION


def test_ads_execution_defaults_to_off_and_accepts_the_other_two_modes(monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "")
    assert config.load().ads_execution == "off"
    for mode in ("validate", "live"):
        monkeypatch.setenv("AGENT_ADS_EXECUTION", mode)
        assert config.load().ads_execution == mode


def test_ads_execution_refuses_an_unknown_value(monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "maybe")
    with pytest.raises(ValueError, match="AGENT_ADS_EXECUTION"):
        config.load()


# ---------------------------------------------------------------- spec 2026-10-11 §4: a refusal read in detail

HEADLINE_PATH = [
    {"fieldName": "mutate_operations", "index": 5},
    {"fieldName": "ad_group_ad_operation"},
    {"fieldName": "create"},
    {"fieldName": "ad"},
    {"fieldName": "responsive_search_ad"},
    {"fieldName": "headlines", "index": 1},
    {"fieldName": "text"},
]
SENT_AD = {
    "adGroupAdOperation": {
        "create": {
            "adGroup": "customers/1234567890/adGroups/-3",
            "status": "ENABLED",
            "ad": {
                "finalUrls": ["https://brightsmile.example/invisalign?src=gads"],
                "responsiveSearchAd": {
                    "headlines": [{"text": "Invisalign in Palo Alto"}, {"text": "Invisalign Deals"}],
                    "descriptions": [{"text": "Clear aligners from a family dentist. Book online."}],
                },
            },
        }
    }
}
SENT_KEYWORD = {
    "adGroupCriterionOperation": {
        "create": {
            "adGroup": "customers/1234567890/adGroups/-3",
            "status": "ENABLED",
            "keyword": {"text": "invisalign deals", "matchType": "PHRASE"},
        }
    }
}
SENT = [{}, {}, {}, {}, SENT_KEYWORD, SENT_AD]  # the keyword at index 4, the ad at index 5


def refusal(
    code: dict,
    path: list[dict] | None,
    *,
    trigger: str | None = None,
    details: dict | None = None,
    message: str = "raw words devtok_SECRET",
) -> dict:
    """One GoogleAdsError as the REST body carries it (lowerCamelCase keys, snake_case proto names in the path)."""
    item: dict = {"errorCode": code, "message": message}
    if path is not None:
        item["location"] = {"fieldPathElements": path}
    if trigger is not None:
        item["trigger"] = {"stringValue": trigger}
    if details is not None:
        item["details"] = details
    return item


def body(*errors: dict) -> dict:
    return {
        "error": {
            "code": 400,
            "message": "Request contains an invalid argument. devtok_SECRET",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "errors": list(errors),
                    "requestId": "abc123",
                }
            ],
        }
    }


TRADEMARK = {
    "policyFindingDetails": {
        "policyTopicEntries": [
            {
                "topic": "TRADEMARKS_IN_AD_TEXT",
                "type": "PROHIBITED",
                "evidences": [{"textList": {"texts": ["Invisalign"]}}],
            }
        ]
    }
}


def test_a_policy_finding_on_a_headline_is_read_with_the_sent_line_as_its_text():
    data = body(
        refusal({"policyFindingError": "POLICY_FINDING"}, HEADLINE_PATH, trigger="Invisalign", details=TRADEMARK)
    )
    (d,) = google_ads._details(data, SENT)
    assert (d.code, d.operation, d.kind, d.position) == ("policyFindingError: POLICY_FINDING", 5, "headline", 1)
    assert d.field == "mutate_operations[5].ad_group_ad_operation.create.ad.responsive_search_ad.headlines[1].text"
    assert (d.topic, d.type, d.exemptible, d.certificate) == ("TRADEMARKS_IN_AD_TEXT", "PROHIBITED", False, False)
    assert d.text == "Invisalign Deals"  # the whole line as sent, not the trigger's word (D16)
    assert "devtok_SECRET" not in repr(d) and d.as_dict()["kind"] == "headline"


def test_the_trigger_then_the_evidence_stand_in_when_the_sent_body_is_not_at_hand():
    data = body(
        refusal({"policyFindingError": "POLICY_FINDING"}, HEADLINE_PATH, trigger="Invisalign", details=TRADEMARK)
    )
    assert google_ads._details(data, None)[0].text == "Invisalign"
    data = body(refusal({"policyFindingError": "POLICY_FINDING"}, HEADLINE_PATH, details=TRADEMARK))
    assert google_ads._details(data, None)[0].text == "Invisalign"


def test_a_keyword_policy_violation_is_read_with_its_phrase_and_exemptibility():
    path = [
        {"fieldName": "mutate_operations", "index": 4},
        {"fieldName": "ad_group_criterion_operation"},
        {"fieldName": "create"},
        {"fieldName": "keyword"},
        {"fieldName": "text"},
    ]
    details = {
        "policyViolationDetails": {
            "externalPolicyName": "Trademarks",
            "externalPolicyDescription": "raw devtok_SECRET",
            "isExemptible": True,
            "key": {"policyName": "trademarks", "violatingText": "invisalign"},
        }
    }
    (d,) = google_ads._details(body(refusal({"policyViolationError": "POLICY_ERROR"}, path, details=details)), SENT)
    assert (d.kind, d.operation, d.text, d.topic, d.exemptible) == (
        "keyword",
        4,
        "invisalign deals",
        "Trademarks",
        True,
    )
    assert d.position is None and d.type is None


def test_a_destination_finding_a_phone_and_a_budget_are_read_by_their_paths():
    url_path = HEADLINE_PATH[:4] + [{"fieldName": "final_urls", "index": 0}]
    phone_path = [
        {"fieldName": "mutate_operations", "index": 9},
        {"fieldName": "asset_operation"},
        {"fieldName": "create"},
        {"fieldName": "call_asset"},
        {"fieldName": "phone_number"},
    ]
    budget_path = [
        {"fieldName": "mutate_operations", "index": 0},
        {"fieldName": "campaign_budget_operation"},
        {"fieldName": "create"},
        {"fieldName": "amount_micros"},
    ]
    mismatch = {
        "policyFindingDetails": {"policyTopicEntries": [{"topic": "DESTINATION_MISMATCH", "type": "PROHIBITED"}]}
    }
    data = body(
        refusal({"policyFindingError": "POLICY_FINDING"}, url_path, details=mismatch),
        refusal({"assetError": "INVALID_PHONE_NUMBER"}, phone_path, trigger="650"),
        refusal({"campaignBudgetError": "INVALID_AMOUNT"}, budget_path),
    )
    url, phone, budget = google_ads._details(data, SENT)
    assert (url.kind, url.topic, url.text) == (
        "url",
        "DESTINATION_MISMATCH",
        "https://brightsmile.example/invisalign?src=gads",
    )
    assert (phone.kind, phone.text, phone.operation) == ("phone", "650", 9)
    assert (budget.kind, budget.text, budget.topic) == ("budget", None, None)


def test_one_detail_per_policy_topic_entry_and_a_certificate_constraint_is_read():
    # Ruling C2: both entries are PROHIBITED here, so both blocked the save and both survive -- contrast the next
    # test, where a merely-LIMITED entry alongside a PROHIBITED one is dropped.
    entries = [
        {
            "topic": "HEALTHCARE",
            "type": "PROHIBITED",
            "constraints": [{"certificateMissingInCountryList": {"countries": []}}],
        },
        {"topic": "TRADEMARKS_IN_AD_TEXT", "type": "PROHIBITED"},
    ]
    data = body(
        refusal(
            {"policyFindingError": "POLICY_FINDING"},
            HEADLINE_PATH[:4],
            details={"policyFindingDetails": {"policyTopicEntries": entries}},
        )
    )
    first, second = google_ads._details(data, SENT)
    assert (first.kind, first.topic, first.certificate) == ("ad", "HEALTHCARE", True)
    assert (second.kind, second.topic, second.certificate) == ("ad", "TRADEMARKS_IN_AD_TEXT", False)
    assert first.text is None  # the ad itself: no line to quote


def test_a_blocking_policy_topic_entry_crowds_out_a_merely_limited_one():
    """Ruling C2: Google's own doc for policy_topic_entries says they are "the PROHIBITED or FULLY_LIMITED ...
    entries that prevented the resource from being saved (among any other entries the resource may also have)" --
    a plain LIMITED entry alongside a PROHIBITED one named nothing that blocked the save, and must not become a
    detail (the owner would be asked to fix a topic that refused nothing)."""
    entries = [
        {"topic": "HEALTHCARE", "type": "LIMITED"},
        {"topic": "TRADEMARKS_IN_AD_TEXT", "type": "PROHIBITED"},
    ]
    data = body(
        refusal(
            {"policyFindingError": "POLICY_FINDING"},
            HEADLINE_PATH[:4],
            details={"policyFindingDetails": {"policyTopicEntries": entries}},
        )
    )
    (only,) = google_ads._details(data, SENT)
    assert (only.topic, only.type) == ("TRADEMARKS_IN_AD_TEXT", "PROHIBITED")


def test_a_body_in_an_unexpected_shape_never_raises_and_reads_as_other():
    """Review Focus 1: no location, an index as a string, camelCase proto names, a trigger that is not text, an error
    with no code at all -- what can be read is read, the rest is `other`, and nothing raises."""
    odd = body(
        refusal({"adError": "INVALID_INPUT"}, None),
        refusal(
            {"stringLengthError": "TOO_LONG"},
            [
                {"fieldName": "mutateOperations", "index": "5"},
                {"fieldName": "adGroupAdOperation"},
                {"fieldName": "create"},
                {"fieldName": "ad"},
                {"fieldName": "responsiveSearchAd"},
                {"fieldName": "headlines", "index": "1"},
                {"fieldName": "text"},
            ],
        ),
        {"errorCode": {}, "message": "no code"},
        {"errorCode": {"requestError": "UNKNOWN"}, "trigger": {"int64Value": "7"}, "location": "not an object"},
    )
    found = google_ads._details(odd, SENT)
    assert [d.kind for d in found] == ["other", "headline", "other"]
    assert found[0].operation is None and found[0].field == "" and found[0].text is None
    assert found[1].text == "Invisalign Deals" and found[1].operation == 5  # the camelCase path still reads
    assert google_ads._details({}, None) == () and google_ads._details({"error": "x"}, None) == ()


def test_kinds_is_exactly_the_tables_own_kinds_plus_other():
    assert {k for _, k in google_ads._KIND_OF} | {"other"} == set(google_ads.KINDS)


def test_a_call_asset_error_is_phone_only_at_its_number_fields_or_a_number_coded_leaf():
    """Ruling C1: the call asset also carries Admiry's own callConversionAction/callConversionReportingState
    (build.py) -- a refusal of those is not the owner's phone to fix. Only the number fields themselves, or the
    call asset's own leaf with an assetError code naming a number problem, are kind `phone`; a conversion-action
    refusal and a non-number leaf refusal (the recording-consent requirement) are `other`."""
    leaf = [
        {"fieldName": "mutate_operations", "index": 9},
        {"fieldName": "asset_operation"},
        {"fieldName": "create"},
        {"fieldName": "call_asset"},
    ]
    conversion_action = body(
        refusal({"assetError": "CALL_INVALID_CONVERSION_ACTION"}, leaf + [{"fieldName": "call_conversion_action"}])
    )
    number_field = body(refusal({"assetError": "INVALID_PHONE_NUMBER"}, leaf + [{"fieldName": "phone_number"}]))
    country_field = body(
        refusal({"assetError": "PHONE_NUMBER_NOT_SUPPORTED_FOR_COUNTRY"}, leaf + [{"fieldName": "country_code"}])
    )
    consent = body(refusal({"assetError": "CALL_CUSTOMER_CONSENT_FOR_CALL_RECORDING_REQUIRED"}, leaf))
    number_leaf = body(refusal({"assetError": "INVALID_PHONE_NUMBER"}, leaf))
    assert google_ads._details(conversion_action, SENT)[0].kind == "other"
    assert google_ads._details(number_field, SENT)[0].kind == "phone"
    assert google_ads._details(country_field, SENT)[0].kind == "phone"
    assert google_ads._details(consent, SENT)[0].kind == "other"
    assert google_ads._details(number_leaf, SENT)[0].kind == "phone"
    assert refusals.classify(google_ads._details(conversion_action, SENT)[0]) == refusals.OTHER
    assert refusals.classify(google_ads._details(consent, SENT)[0]) == refusals.OTHER
    assert refusals.classify(google_ads._details(number_leaf, SENT)[0]) == refusals.BUSINESS


def test_a_sibling_field_is_never_read_as_the_entrys_own_text():
    """D16 fix round 1: the text at the end of the path is not always the offending line -- a path to a sibling
    field inside the same headline or keyword entry (AdTextAsset's pinned_field, Keyword's match_type) must still
    read that entry's own `text`, never the sibling's value."""
    sent = [
        {"adGroupCriterionOperation": {"create": {"keyword": {"text": "invisalign deals", "matchType": "PHRASE"}}}},
        {
            "adGroupAdOperation": {
                "create": {
                    "ad": {
                        "responsiveSearchAd": {"headlines": [{"text": "Invisalign Deals", "pinnedField": "HEADLINE_1"}]}
                    }
                }
            }
        },
    ]
    match_type_path = [
        {"fieldName": "mutate_operations", "index": 0},
        {"fieldName": "ad_group_criterion_operation"},
        {"fieldName": "create"},
        {"fieldName": "keyword"},
        {"fieldName": "match_type"},
    ]
    pinned_field_path = [
        {"fieldName": "mutate_operations", "index": 1},
        {"fieldName": "ad_group_ad_operation"},
        {"fieldName": "create"},
        {"fieldName": "ad"},
        {"fieldName": "responsive_search_ad"},
        {"fieldName": "headlines", "index": 0},
        {"fieldName": "pinned_field"},
    ]
    data = body(
        refusal({"criterionError": "INVALID_KEYWORD_TEXT"}, match_type_path),
        refusal({"assetError": "PINNED_FIELD_INVALID"}, pinned_field_path),
    )
    keyword, headline = google_ads._details(data, sent)
    assert keyword.text == "invisalign deals"  # not "PHRASE"
    assert headline.text == "Invisalign Deals"  # not "HEADLINE_1"


def test_the_mutate_refusal_carries_the_details_beside_the_codes(ads_api):
    data = body(
        refusal({"policyFindingError": "POLICY_FINDING"}, HEADLINE_PATH, trigger="Invisalign", details=TRADEMARK)
    )
    http = fake_client([], responses={"googleAds:mutate": httpx.Response(400, json=data)})
    with pytest.raises(AdsError) as e:
        google_ads.mutate(ads_api, CID, SENT, validate_only=True, http=http)
    assert e.value.codes == ("policyFindingError: POLICY_FINDING",) and len(e.value.details) == 1
    assert e.value.details[0].text == "Invisalign Deals" and e.value.status == 400
    assert not any(s in str(e.value) for s in SECRETS)


def test_a_search_refusal_and_a_transport_failure_carry_no_details(ads_api):
    http = fake_client([], responses={"googleAds:search": failure(400, {"queryError": "BAD_FIELD"})})
    with pytest.raises(AdsError) as e:
        google_ads.search(ads_api, CID, CUSTOMER_GAQL, http=http)
    assert e.value.details == () and e.value.code == "queryError: BAD_FIELD"
    assert AdsError("x").details == ()
    # item 5: driven through a real transport failure, and a coded 5xx body, not just the bare constructor default
    http = fake_client([], responses={"googleAds:mutate": _network})
    with pytest.raises(AdsError) as e:
        google_ads.mutate(ads_api, CID, [{"campaignOperation": {"create": {}}}], validate_only=False, http=http)
    assert str(e.value) == google_ads.UNREACHABLE and e.value.details == ()
    http = fake_client([], responses={"googleAds:mutate": failure(503, {"internalError": "INTERNAL_ERROR"})})
    with pytest.raises(AdsError) as e:
        google_ads.mutate(ads_api, CID, [{"campaignOperation": {"create": {}}}], validate_only=False, http=http)
    assert str(e.value) == google_ads.UNREACHABLE and e.value.details == ()


def test_five_once_raising_shapes_are_read_safely_through_failure():
    """Ruling C3: each of these once raised inside `_details` -- an AttributeError (evidences.textList not a
    dict), two TypeErrors (constraints, policyTopicEntries not a list), a third TypeError (fieldPathElements not a
    list) and a ValueError (a non-ASCII digit index). Guarded directly now, with `_failure`'s own try/except as the
    net for whatever is still missed. Driven through `_failure` itself (not `_details` directly), `operations`
    non-None ([{}]) so the details branch is the one taken."""
    evidence_not_a_list = body(
        refusal(
            {"policyFindingError": "POLICY_FINDING"},
            None,
            details={
                "policyFindingDetails": {"policyTopicEntries": [{"topic": "X", "evidences": [{"textList": ["x"]}]}]}
            },
        )
    )
    constraints_not_a_list = body(
        refusal(
            {"policyFindingError": "POLICY_FINDING"},
            None,
            details={"policyFindingDetails": {"policyTopicEntries": [{"topic": "X", "constraints": 1}]}},
        )
    )
    path_not_a_list = body(refusal({"adError": "INVALID_INPUT"}, 1))
    entries_not_a_list = body(
        refusal(
            {"policyFindingError": "POLICY_FINDING"}, None, details={"policyFindingDetails": {"policyTopicEntries": 1}}
        )
    )
    odd_index = body(refusal({"adError": "INVALID_INPUT"}, [{"fieldName": "headlines", "index": "²"}]))
    for data, code in [
        (evidence_not_a_list, "policyFindingError: POLICY_FINDING"),
        (constraints_not_a_list, "policyFindingError: POLICY_FINDING"),
        (path_not_a_list, "adError: INVALID_INPUT"),
        (entries_not_a_list, "policyFindingError: POLICY_FINDING"),
        (odd_index, "adError: INVALID_INPUT"),
    ]:
        e = google_ads._failure(400, data, [{}])
        assert e.codes == (code,)


@pytest.mark.parametrize(
    "odd", [{"errorCode": {"adError": "INVALID_INPUT"}}, "adError: INVALID_INPUT", 7], ids=["dict", "string", "number"]
)
@pytest.mark.parametrize("where", ["errors", "details"])
def test_errors_or_details_that_are_not_a_list_are_still_a_refusal_with_no_codes(odd, where):
    """Ruling L3 (final review, Minor 5): `_codes` reads the body's `details` and each one's `errors` through `_list`,
    the module's own guard (Ruling C3), as `_details` does -- a body where either is a dict, a string or a number is
    still an AdsError, with no codes and no details, and nothing raises out of `_failure`."""
    data = body(refusal({"adError": "INVALID_INPUT"}, None))
    if where == "errors":
        data["error"]["details"][0]["errors"] = odd
    else:
        data["error"]["details"] = odd
    e = google_ads._failure(400, data, [{}])
    assert isinstance(e, AdsError) and e.codes == () and e.details == () and str(e) == google_ads.REFUSED
