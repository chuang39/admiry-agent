"""The Google builder's accepted creatives (hackathon plan 2026-11-04 Task 7, spec §5 "Real-time Google Ads creation"):
the owner's accepted headlines and descriptions feed the responsive search ad (the copy linter and the service-name pin
still apply), and the accepted images and callouts become campaign assets (`AD_IMAGE`, `CALLOUT`) in the same create
mutate. With no accepted creatives the mutate is exactly what it was before (the roofer golden, frozen from f7a53f5).
Pure functions, plus the adapter's launch with Google and the database stubbed out."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

from agent.channels import base
from agent.channels.google_search import adapter as google_adapter
from agent.channels.google_search import build as ads_launch

GOLDEN = Path(__file__).parent / "golden" / "ads_ops_roofer.json"
CID = "4445556666"
ACCOUNT = ads_launch.Account(
    customer_id=CID, tag="AW-987654321", conversion_actions={"call_ad": f"customers/{CID}/conversionActions/7"}
)
TARGET = ads_launch.Target(kind="proximity", lat=30.2672, lng=-97.7431, radius_miles=15.0, geo_targets=())
PAGE_URL = "https://hillcountryroofing.example/roof-repair?src=gads"

PLAN_HEADLINES = ["Roof Repair in Austin", "Free Roof Inspections", "Licensed Austin Roofers"]
PLAN_DESCRIPTIONS = [
    "Leaks, storm damage and worn shingles fixed by a local crew. Book a visit.",
    "Licensed and insured roofers serving Austin and Round Rock.",
]

LANDSCAPE = b"\x89PNG\r\n\x1a\n landscape pixels"
SQUARE = b"\x89PNG\r\n\x1a\n square pixels"


def _roofer() -> tuple[dict, dict]:
    """A roofer's approved plan and its knowledge, in the shape `operations` reads (the demo business: a roofer in
    Austin, two themes, the home-services recipe's banned words and negatives)."""
    plan = {
        "id": "plan_roof1",
        "business_id": "biz_roofer",
        "sections": {
            "consultation": {
                "business": {
                    "category": "home_services",
                    "service": "Roof Repair",
                    "services": ["Roof Repair"],
                    "location": "Austin, TX",
                },
                "geo": {"radius_miles": 15.0, "center": "Austin, TX"},
            },
            "ads": {
                "channels": [
                    {
                        "channel": "google_search",
                        "monthly_usd": 1500.0,
                        "themes": ["roof repair austin", "roof replacement"],
                        "negatives": ["roofing jobs"],
                        "headlines": PLAN_HEADLINES,
                        "descriptions": PLAN_DESCRIPTIONS,
                        "landing_service": "Roof Repair",
                    }
                ]
            },
            "rationale": {"example_searches": ["roof leak repair near me", "roofer austin"]},
            "estimate": {"assumptions": [{"name": "cost per lead", "value": "$60-140"}]},
        },
    }
    knowledge = {
        "phone": "(512) 555-0142",
        "name": "Hill Country Roofing",
        "website": "https://hillcountryroofing.example",
        "services": [{"name": "Roof Repair", "intent_phrases": ["emergency roof repair", "shingle repair"]}],
    }
    return plan, knowledge


def _creatives(**over) -> dict:
    """An accepted creative set as `studio.accepted` returns it (Task 5's shape)."""
    out = {
        "set_id": "cs_roof1",
        "headlines": [
            "Roof Repair, Austin TX",  # names the service: pinned to position 1
            "Storm Damage Fixed Fast",
            "Free Roof Inspection",
            "A Headline Far Too Long For Google To Take",  # over 30 characters: the linter drops it
            "Guaranteed Leak-Free Roofs",  # a word the home-services recipe bans: dropped
        ],
        "descriptions": [
            "Local crew, 18 years on Austin roofs. Photos of our own work on every job.",
            "Shingle, metal and flat roofs repaired. Book a free inspection online.",
            "Call 512-555-0142 today",  # a phone number: dropped
        ],
        "callouts": ["Free Inspections", "Licensed & Insured", "Family Owned"],
        "images": [
            {"id": "img_1", "format": "landscape", "bytes": LANDSCAPE},
            {"id": "img_2", "format": "square", "bytes": SQUARE},
        ],
    }
    out.update(over)
    return out


def _ops(creatives: dict | None = None, *, knowledge: dict | None = None, **kw) -> list[dict]:
    plan, roofer_knowledge = _roofer()
    if creatives is not None:
        kw["creatives"] = creatives
    return ads_launch.operations(
        plan,
        roofer_knowledge if knowledge is None else knowledge,
        page_url=PAGE_URL,
        target=TARGET,
        account=ACCOUNT,
        customer_id=CID,
        **kw,
    )


def _rsas(ops: list[dict]) -> list[dict]:
    return [
        op["adGroupAdOperation"]["create"]["ad"]["responsiveSearchAd"]
        for op in ops
        if "create" in op.get("adGroupAdOperation", {})
    ]


def _temp_names(ops: list[dict]) -> list[str]:
    names = []
    for op in ops:
        body = (op[next(iter(op))] or {}).get("create")
        if isinstance(body, dict) and isinstance(body.get("resourceName"), str):
            names.append(body["resourceName"])
    return names


def _results(ops: list[dict]) -> list[dict]:
    """Google's `mutateOperationResponses` for `ops`: one per operation, in order, each a fresh real resource name."""
    out = []
    for n, op in enumerate(ops, start=101):
        kind = next(iter(op))
        out.append({ads_launch._RESULT_KEY[kind]: {"resourceName": f"customers/{CID}/{kind}/{n}"}})
    return out


# ---------------------------------------------------------------- no accepted creatives: nothing changes


def test_operations_without_creatives_unchanged():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))  # today's output, frozen before Task 7 touched build.py
    assert _ops() == golden
    plan, knowledge = _roofer()
    assert (
        ads_launch.operations(
            plan, knowledge, page_url=PAGE_URL, target=TARGET, account=ACCOUNT, customer_id=CID, creatives=None
        )
        == golden
    )
    assert _ops({}) == golden  # an empty dict is "nothing accepted" too


# ---------------------------------------------------------------- accepted headlines and descriptions


def test_operations_use_accepted_headlines_and_descriptions():
    rsas = _rsas(_ops(_creatives()))
    assert len(rsas) == 2  # one ad per theme, every one carrying the accepted copy
    for rsa in rsas:
        assert rsa["headlines"] == [
            {"text": "Roof Repair, Austin TX", "pinnedField": "HEADLINE_1"},  # the existing pin rule, unchanged
            {"text": "Storm Damage Fixed Fast"},
            {"text": "Free Roof Inspection"},
        ]
        assert rsa["descriptions"] == [
            {"text": "Local crew, 18 years on Austin roofs. Photos of our own work on every job."},
            {"text": "Shingle, metal and flat roofs repaired. Book a free inspection online."},
        ]
        assert not {h["text"] for h in rsa["headlines"]} & set(PLAN_HEADLINES)

    # a first accepted headline that does not name the service is not pinned
    unpinned = _rsas(_ops(_creatives(headlines=["Storm Damage Fixed Fast", "Roof Repair, Austin TX", "Free Quote"])))
    assert all("pinnedField" not in h for h in unpinned[0]["headlines"])

    # a line Google refused for the business stays out of the accepted copy too
    dropped = ads_launch.Dropped(headlines=frozenset({"Storm Damage Fixed Fast"}))
    texts = [h["text"] for h in _rsas(_ops(_creatives(), dropped=dropped))[0]["headlines"]]
    assert texts == ["Roof Repair, Austin TX", "Free Roof Inspection"]


def test_operations_keep_the_plans_copy_where_nothing_was_accepted():
    """Empty lists are allowed (images only, say): an empty accepted list leaves the plan's own lines in the ad."""
    rsa = _rsas(_ops(_creatives(headlines=[], descriptions=[])))[0]
    assert [h["text"] for h in rsa["headlines"]] == PLAN_HEADLINES
    assert [d["text"] for d in rsa["descriptions"]] == PLAN_DESCRIPTIONS


# ---------------------------------------------------------------- image and callout assets


def test_asset_ops_link_images_and_callouts():
    campaign_ref = f"customers/{CID}/campaigns/-2"
    creatives = _creatives(
        callouts=["Free Inspections", "", "Licensed & Insured", "A callout far over the limit", "Family Owned"],
    )
    ops = ads_launch.creative_asset_ops(campaign_ref, creatives, CID, -20)

    assets = [op["assetOperation"]["create"] for op in ops if "assetOperation" in op]
    links = [op["campaignAssetOperation"]["create"] for op in ops if "campaignAssetOperation" in op]
    assert len(ops) == len(assets) + len(links)
    images = [a for a in assets if a.get("type") == "IMAGE"]
    callouts = [a for a in assets if "calloutAsset" in a]
    assert len(images) == 2 and len(callouts) == 3  # the empty and the 28-character callout are left out
    assert images[0] == {
        "resourceName": f"customers/{CID}/assets/-20",
        "name": "admiry cs_roof1 landscape 1",
        "type": "IMAGE",
        "imageAsset": {"data": base64.b64encode(LANDSCAPE).decode("ascii")},
    }
    assert images[1]["name"] == "admiry cs_roof1 square 2"
    assert images[1]["imageAsset"]["data"] == base64.b64encode(SQUARE).decode("ascii")
    assert [c["calloutAsset"]["calloutText"] for c in callouts] == [
        "Free Inspections",
        "Licensed & Insured",
        "Family Owned",
    ]
    assert sorted(link["fieldType"] for link in links) == ["AD_IMAGE"] * 2 + ["CALLOUT"] * 3
    assert all(link["campaign"] == campaign_ref for link in links)

    # every asset is created before the link that names it, and every link names one of them, by its field type
    position = {op["assetOperation"]["create"]["resourceName"]: i for i, op in enumerate(ops) if "assetOperation" in op}
    for i, op in enumerate(ops):
        link = op.get("campaignAssetOperation", {}).get("create")
        if link:
            assert position[link["asset"]] < i
            made = ops[position[link["asset"]]]["assetOperation"]["create"]
            assert link["fieldType"] == ("AD_IMAGE" if made.get("type") == "IMAGE" else "CALLOUT")

    names = [a["resourceName"] for a in assets]
    ids = [int(n.rsplit("/", 1)[1]) for n in names]
    assert len(set(ids)) == len(ids) and all(i < 0 for i in ids) and max(ids) == -20
    assert all(n.startswith(f"customers/{CID}/assets/") for n in names)

    # inside the one create mutate: unique negative temp names across the whole call, the campaign before its links
    full = _ops(_creatives())
    temp_ids = [int(n.rsplit("/", 1)[1]) for n in _temp_names(full)]
    assert len(set(temp_ids)) == len(temp_ids) and all(i < 0 for i in temp_ids)
    campaign_at = next(i for i, op in enumerate(full) if "campaignOperation" in op)
    link_kinds = [op["campaignAssetOperation"]["create"] for op in full if "campaignAssetOperation" in op]
    assert sorted(x["fieldType"] for x in link_kinds) == [
        "AD_IMAGE",
        "AD_IMAGE",
        "CALL",
        "CALLOUT",
        "CALLOUT",
        "CALLOUT",
    ]
    assert all(i > campaign_at for i, op in enumerate(full) if "campaignAssetOperation" in op)
    campaign_ref = full[campaign_at]["campaignOperation"]["create"]["resourceName"]
    assert all(x["campaign"] == campaign_ref for x in link_kinds)
    # what comes before the assets is the mutate the same accepted copy builds without them
    copy_only = _ops(_creatives(images=[], callouts=[]))
    assert full[: len(copy_only)] == copy_only and len(full) == len(copy_only) + 10

    # nothing accepted: no asset operations at all
    assert ads_launch.creative_asset_ops(campaign_ref, _creatives(images=[], callouts=[]), CID, -9) == []


def test_operations_lint_and_dedupe_the_accepted_callouts():
    """Final review I2: the create is all or nothing, so one callout Google refuses fails the whole launch. The build
    sends a callout only once (case-insensitively) and only when it passes the headline rules with the trade's banned
    words: no banned word, no "!", no phone, no shouting, at most `CALLOUT_MAX` characters."""
    callouts = [
        "Free Inspections",
        "Guaranteed Repairs",  # a word the home-services recipe bans
        "Call Us Today!",  # an exclamation mark
        "free inspections",  # the first one again
        "Family Owned",
        "Licensed & Insured",
    ]
    ops = _ops(_creatives(callouts=callouts))
    sent = [
        op["assetOperation"]["create"]["calloutAsset"]["calloutText"]
        for op in ops
        if "calloutAsset" in op.get("assetOperation", {}).get("create", {})
    ]
    assert sent == ["Free Inspections", "Family Owned", "Licensed & Insured"]
    links = [op["campaignAssetOperation"]["create"]["fieldType"] for op in ops if "campaignAssetOperation" in op]
    assert links.count("CALLOUT") == 3


# ---------------------------------------------------------------- resources_from


def test_resources_from_ignores_asset_results():
    ops = _ops(_creatives())
    responses = _results(ops)
    got = ads_launch.resources_from(responses, ops)
    call_at = next(i for i, op in enumerate(ops) if "callAsset" in op.get("assetOperation", {}).get("create", {}))
    call_link_at = next(
        i
        for i, op in enumerate(ops)
        if op.get("campaignAssetOperation", {}).get("create", {}).get("fieldType") == "CALL"
    )
    assert got["call_asset"] == responses[call_at]["assetResult"]["resourceName"]
    assert got["campaign_asset"] == responses[call_link_at]["campaignAssetResult"]["resourceName"]
    copy_only = _ops(_creatives(images=[], callouts=[]))
    assert got == ads_launch.resources_from(responses[: len(copy_only)], copy_only)  # the asset results add nothing

    # a business with no phone: no call asset, and an image's result is never mistaken for one
    _, knowledge = _roofer()
    no_phone = _ops(_creatives(), knowledge={**knowledge, "phone": None})
    got = ads_launch.resources_from(_results(no_phone), no_phone)
    assert "call_asset" not in got and "campaign_asset" not in got
    assert got["campaign"] and got["campaign_budget"] and len(got["ads"]) == 2


# ---------------------------------------------------------------- the adapter's launch passes them through


def test_launch_sends_the_builds_accepted_creatives(monkeypatch):
    sent: list[list[dict]] = []
    monkeypatch.setattr(google_adapter, "_customer_id", lambda conn, biz, settings=None: CID)
    monkeypatch.setattr(google_adapter, "_target", lambda *a, **k: TARGET)
    monkeypatch.setattr(google_adapter, "_account", lambda *a, **k: ACCOUNT)
    monkeypatch.setattr(
        google_adapter.google_ads, "mutate", lambda settings, cid, ops, *, validate_only, http=None: sent.append(ops)
    )
    plan, knowledge = _roofer()
    payload = {
        "business_id": plan["business_id"],
        "plan": plan,
        "knowledge": knowledge,
        "page_url": PAGE_URL,
        "dropped": ads_launch.NOTHING_DROPPED,
    }

    def launch(payload: dict) -> list[dict]:
        build = base.Build(channel="google_search", plan_id=plan["id"], payload=copy.deepcopy(payload), summary={})
        google_adapter.ADAPTER.launch(None, None, None, build, {}, validate_only=True)
        return sent[-1]

    with_creatives = launch({**payload, "creatives": _creatives()})
    fields = [
        op["campaignAssetOperation"]["create"]["fieldType"] for op in with_creatives if "campaignAssetOperation" in op
    ]
    assert fields.count("AD_IMAGE") == 2 and fields.count("CALLOUT") == 3
    assert _rsas(with_creatives)[0]["headlines"][0] == {"text": "Roof Repair, Austin TX", "pinnedField": "HEADLINE_1"}

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert launch(payload) == golden  # a build with no "creatives" key: the mutate of today
    assert launch({**payload, "creatives": None}) == golden
