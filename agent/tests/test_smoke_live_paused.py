"""scripts/smoke_live_paused.py's pure helpers (plan Task 9, ruling R27): the arguments, the masking of Customer IDs,
the create mutate's summary, the headline comparison, the read-back table and what a create still lacks."""

from __future__ import annotations

import pytest

from agent.channels.google_search import client as google_ads
from scripts import smoke_live_paused as smoke

CID = "1234567890"


def test_parse_args_takes_one_step():
    assert smoke.parse_args(["biz_x", "--validate"]).step == "validate"
    assert smoke.parse_args(["biz_x", "--create"]).step == "create"
    args = smoke.parse_args(["biz_x", "--readback"])
    assert (args.business_id, args.step) == ("biz_x", "readback")


@pytest.mark.parametrize(
    "argv",
    [["biz_x"], ["biz_x", "--validate", "--create"], ["--validate"], ["someone", "--validate"]],
)
def test_parse_args_refuses(argv):
    with pytest.raises(SystemExit):
        smoke.parse_args(argv)


def test_mask_replaces_the_customer_id_everywhere():
    data = {"campaign": f"customers/{CID}/campaigns/9", "list": [f"customers/{CID}/assets/1", 3], "n": None}
    assert smoke.mask(data, CID) == {
        "campaign": "customers/<cid>/campaigns/9",
        "list": ["customers/<cid>/assets/1", 3],
        "n": None,
    }
    assert smoke.mask("plain", None) == "plain"


def _ops() -> list[dict]:
    rsa = {"headlines": [{"text": "A", "pinnedField": "HEADLINE_1"}, {"text": "B"}], "descriptions": [{"text": "D"}]}
    return [
        {"campaignBudgetOperation": {"create": {}}},
        {"campaignOperation": {"create": {}}},
        {"adGroupAdOperation": {"create": {"ad": {"responsiveSearchAd": rsa}}}},
        {"assetOperation": {"create": {"imageAsset": {"data": "x"}}}},
        {"campaignAssetOperation": {"create": {"fieldType": "AD_IMAGE"}}},
        {"assetOperation": {"create": {"calloutAsset": {"calloutText": "Free"}}}},
        {"campaignAssetOperation": {"create": {"fieldType": "CALLOUT"}}},
        {"assetOperation": {"create": {"callAsset": {}}}},
        {"campaignAssetOperation": {"create": {"fieldType": "CALL"}}},
    ]


def test_op_counts_names_asset_kinds_and_field_types():
    assert smoke.op_counts(_ops()) == {
        "adGroupAdOperation": 1,
        "assetOperation:CALL": 1,
        "assetOperation:CALLOUT": 1,
        "assetOperation:IMAGE": 1,
        "campaignAssetOperation:AD_IMAGE": 1,
        "campaignAssetOperation:CALL": 1,
        "campaignAssetOperation:CALLOUT": 1,
        "campaignBudgetOperation": 1,
        "campaignOperation": 1,
    }


def test_sent_headlines_and_the_comparison_keep_the_pin_apart():
    [headlines] = smoke.sent_headlines(_ops())
    out = smoke.compare_headlines(["A", "B"], headlines)
    assert out == {"equal": True, "same_order": True, "missing": [], "extra": [], "pinned": {"A": "HEADLINE_1"}}
    out = smoke.compare_headlines(["B", "C"], headlines)
    assert (out["equal"], out["missing"], out["extra"]) == (False, ["C"], ["A"])
    assert smoke.compare_headlines(["B", "A"], headlines)["same_order"] is False


def test_summarise_reads_the_rows_back():
    campaigns = [
        {"campaign": {"resourceName": "customers/1/campaigns/7", "name": "Admiry · X · biz_x", "status": "PAUSED"}},
        {"campaign": {"resourceName": "customers/1/campaigns/8", "name": "Admiry · biz_x", "status": "ENABLED"}},
    ]
    assets = [
        {"campaignAsset": {"fieldType": "AD_IMAGE", "status": "ENABLED"}, "asset": {"type": "IMAGE", "name": "i"}},
        {"campaignAsset": {"fieldType": "AD_IMAGE", "status": "ENABLED"}, "asset": {"type": "IMAGE"}},
        {
            "campaignAsset": {"fieldType": "CALLOUT", "status": "ENABLED"},
            "asset": {"calloutAsset": {"calloutText": "F"}},
        },
        {"campaignAsset": {"fieldType": "CALLOUT", "status": "REMOVED"}, "asset": {}},
    ]
    ads = [
        {
            "adGroup": {"name": "roof repair"},
            "adGroupAd": {
                "resourceName": "customers/1/adGroupAds/2~3",
                "status": "ENABLED",
                "ad": {
                    "responsiveSearchAd": {
                        "headlines": [{"text": "A", "pinnedField": "HEADLINE_1", "assetPerformanceLabel": "PENDING"}],
                        "descriptions": [{"text": "D"}],
                    }
                },
            },
        }
    ]
    table = smoke.summarise(campaigns, assets, ads)
    assert [c["status"] for c in table["campaigns"]] == ["PAUSED", "ENABLED"]
    assert [c["resource"] for c in table["enabled"]] == ["customers/1/campaigns/8"]
    assert table["asset_fields"] == {"AD_IMAGE": 2, "CALLOUT": 1}
    assert table["assets"][2]["callout"] == "F"
    assert table["ads"] == [
        {
            "ad_group": "roof repair",
            "ad": "customers/1/adGroupAds/2~3",
            "status": "ENABLED",
            "headlines": [{"text": "A", "pinnedField": "HEADLINE_1"}],
            "descriptions": ["D"],
        }
    ]
    assert smoke.summarise([], [], []) == {"campaigns": [], "enabled": [], "asset_fields": {}, "assets": [], "ads": []}


def test_missing_for_create_names_each_gap():
    ready = {
        "mode": "live_paused",
        "approved": 2,
        "funded": True,
        "account_ready": True,
        "account_reason": None,
        "page_url": "https://example.com/p",
        "built": False,
    }
    assert smoke.missing_for_create(ready) == []
    lacking = {"mode": "off", "approved": None, "account_ready": False, "account_reason": "not linked", "built": True}
    missing = smoke.missing_for_create(lacking)
    assert len(missing) == 5
    assert "AGENT_LAUNCH_ALLOWLIST" in missing[0]
    assert "no approved plan" in missing[1]
    assert "not linked" in missing[2]
    assert "published page" in missing[3]
    assert "one create only" in missing[4]
    assert "does not" in smoke.missing_for_create({**ready, "funded": False})[0]


def test_ads_error_keeps_codes_paths_and_bodies_masked():
    detail = google_ads.RefusalDetail(
        code="urlFieldError: MISSING_PROTOCOL",
        operation=24,
        kind="url",
        position=None,
        field="mutate_operations[24].ad_group_ad_operation.create.ad.final_urls[0]",
        topic=None,
        type=None,
        exemptible=False,
        certificate=False,
        text="?src=gads",
    )
    e = google_ads.AdsError("refused", codes=(detail.code,), status=400, details=(detail,))
    out = smoke.ads_error(e, [f'{{"customers/{CID}": 1}}'], CID)
    assert out["status"] == 400 and out["codes"] == ["urlFieldError: MISSING_PROTOCOL"]
    assert out["details"][0]["field"].endswith("final_urls[0]")
    assert out["google_bodies"] == ['{"customers/<cid>": 1}']
