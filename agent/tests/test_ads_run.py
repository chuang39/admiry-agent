"""ads_run.py, the Google Search ads lifecycle (spec 2026-09-28 admiry-google-search-execution-design §5, §8, §9;
plan P3-P8, Review Focus 1-3; A3 Task 4), against a small stateful fake of Google Ads keyed by URL path and, for a
mutate, by the operation kinds in its body: it remembers the campaigns and ad groups it built, so a lookup finds
them, a second campaign -- or ad group in one campaign -- with the same name is refused like Google refuses it, and
a lost response can be recovered for real. Like v25 it refuses a search that asks for a page size, and a metrics
search that does not name its three days (A3 final review C1, C2)."""

from __future__ import annotations

import dataclasses
import functools
import itertools
import json
import logging
import re
import time
import zlib
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import (
    ads_conversions,
    ads_launch,
    ads_run,
    business,
    config,
    db,
    google_ads,
    google_ads_link,
    google_auth,
    landing,
    plans,
    proposals,
    tick,
    wallet,
)
from agent.channels import registry
from agent.channels.google_search import adapter as google
from agent.tests.conftest import ACCOUNT_JARGON, ADS_SECRETS, JARGON, MONEY_JARGON, TEST_DB_URL, call_tool, tool_text
from agent.tests.fixtures import business_with_key

CID = "1234567890"
BIZ = "biz_1"
GOOGLE = "google_search"
# ads_link.status -> the status its channel_account(google_search) mirror carries (the migration's own mapping)
MIRRORED = {
    "active": "active",
    "manual": "active",
    "pending": "pending",
    "refused": "refused",
    "cancelled": "inactive",
    "inactive": "inactive",
}
PAGE_URL = "https://brightsmile.example/invisalign"
REAL_CLIENT = google.client  # the Google Search adapter's client factory (M0 Task 4), before any test patches it
HEADLINES = ["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners Near You"]
DESCRIPTIONS = ["Clear aligners from a family dentist. Book online.", "Straighter teeth without metal braces."]
KNOWLEDGE = {
    "name": "Bright Smile Dental",
    "website": "https://brightsmile.example",
    "phone": "650-555-0100",
    "category": "dentists",
    "services": [{"name": "Invisalign", "intent_phrases": ["invisalign palo alto", "clear aligners palo alto"]}],
    "service_area": ["Palo Alto"],
    "listing": {
        "place_id": "ChIJ1",
        "name": "Bright Smile Dental",
        "address": "500 University Ave, Palo Alto, CA",
        "lat": 37.4419,
        "lng": -122.143,
        "source": "Google Maps",
    },
}
# a check that passed, as it is saved since M1 Task 4: with the terms and contact rules NewsBreak needs (M1 Task 5b,
# R18: a report without them is checked again before a NewsBreak launch is judged on it)
CHECK_OK = {
    "ok": True,
    "url": PAGE_URL,
    "rules": [
        {"id": "reachable", "ok": True, "fix": None},
        {"id": "terms", "ok": True, "fix": None},
        {"id": "contact", "ok": True, "fix": None},
    ],
}
PHONE_FIX = "Put 650-555-0100 at the top as a tap-to-call link."
CHECK_FAILED = {
    "ok": False,
    "url": PAGE_URL,
    "rules": [
        {"id": "reachable", "ok": True, "fix": None},
        {"id": "mobile", "ok": None, "fix": "Add a viewport line."},
        {"id": "phone", "ok": False, "fix": PHONE_FIX},
        {"id": "tracking", "ok": False, "fix": "Paste the tracking snippet."},
    ],
}


def sections(
    *,
    monthly_usd: float = 1000.0,
    themes: tuple[str, ...] = ("invisalign palo alto",),
    headlines: list[str] = HEADLINES,
    descriptions: list[str] = DESCRIPTIONS,
    negatives: tuple[str, ...] = ("jobs",),
    radius_miles: float | None = 5.0,
) -> dict:
    """A consultation plan's sections, as `recommend` stores them (the parts `ads_launch` and the lines read)."""
    return {
        "presence": {"pages": [], "gbp_notes": []},
        "discovery": {"fixes": [], "monthly_usd": 0},
        "ads": {
            "channels": [
                {
                    "channel": "google_search",
                    "monthly_usd": monthly_usd,
                    "themes": list(themes),
                    "negatives": list(negatives),
                    "headlines": list(headlines),
                    "descriptions": list(descriptions),
                    "landing_service": "Invisalign",
                }
            ],
            "monthly_limit_usd": 2000.0,
        },
        "creative": {"angles": []},
        "conversion": {"tracking": [], "review_request_after_days": 3},
        "optimization": {"autopilot": "safe"},
        "estimate": {
            "basis": "benchmark",
            "low": 10,
            "high": 20,
            "assumptions": [{"name": "cost per lead", "value": "$51–109"}],
        },
        "disclosure": {"text": plans.DISCLOSURE},
        "consultation": {
            "business": {
                "category": "dentists",
                "service": "Invisalign",
                "services": ["Invisalign"],
                "location": "500 University Ave, Palo Alto, CA",
            },
            "objective": "bookings",
            "geo": {"radius_miles": radius_miles, "center": "ChIJ1"},
            "level": "recommended",
            "conversions": plans.CONVERSIONS["bookings"],  # cards() reads conversions and duration_days direct:
            "duration_days": 30,  # every real plan (consultation_section) carries both
        },
        "rationale": {"example_searches": ["invisalign palo alto", "invisalign cost"]},
    }


def add_plan(conn, secs: dict, *, biz: str = BIZ, status: str = "approved") -> str:
    version = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 AS v FROM plan WHERE business_id = %s", (biz,)
    ).fetchone()["v"]
    plan_id = db.new_id("pl")
    body = {"sections": secs, "lines": plans.owner_lines(secs), "rationale": "", "notes": []}
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES (%s, %s, %s, %s, %s)",
        (plan_id, biz, version, Jsonb(body), status),
    )
    return plan_id


def seed(
    conn,
    *,
    biz: str = BIZ,
    cid: str = CID,
    status: str = "approved",
    secs: dict | None = None,
    knowledge: dict | None = None,
    link: str | None = "active",
    check: dict | None = CHECK_OK,
    page: bool = True,
    conversions: bool = True,
    email: str = "owner@example.com",
) -> tuple[str, str]:
    """A verified own-account business ready to launch: knowledge with a phone and a listing point, a $2,000
    goal, a plan (approved unless `status` says otherwise), the linked account (and its channel_account mirror, as
    the M0 migration writes it), a published page whose check passed, and the account's tag and every conversion
    action (`ads_conversions.KINDS`: the conversion setup already ran live; `call_ad` is conversionActions/77).
    Returns (key, plan_id)."""
    key = business_with_key(conn, biz_id=biz, claim_state="verified")
    conn.execute("UPDATE business SET owner_email = %s WHERE id = %s", (email, biz))
    conn.execute(
        "INSERT INTO knowledge (business_id, version, json, source) VALUES (%s, 1, %s, 'extract')",
        (biz, Jsonb(KNOWLEDGE if knowledge is None else knowledge)),
    )
    business.set_goal(conn, biz, monthly_limit_usd=2000)
    plan_id = add_plan(conn, secs or sections(), biz=biz, status=status)
    if link:
        google_ads_link.save(conn, biz, cid, status=link)
        registry.save_account(
            conn, biz, GOOGLE, mode="own_account", status=MIRRORED[link], external={"customer_id": cid}
        )
    if page:
        conn.execute(
            "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, %s, 'owner', %s, "
            "'published', %s)",
            (db.new_id("pg"), biz, PAGE_URL, Jsonb(check) if check is not None else None),
        )
    if conversions:
        rows = [("tag", f"customers/{cid}", "AW-555")] + [
            (kind, f"customers/{cid}/conversionActions/{77 + i}", f"Lbl{i}")
            for i, kind in enumerate(["call_ad", *(k for k in ads_conversions.KINDS if k != "call_ad")])
        ]
        for kind, resource_name, label in rows:
            conn.execute(
                "INSERT INTO conversion_action (business_id, kind, resource_name, label) VALUES (%s, %s, %s, %s)",
                (biz, kind, resource_name, label),
            )
    return key, plan_id


# ---------------------------------------------------------------- the fake Google Ads

RESULTS = {  # operation kind -> (its result key in mutateOperationResponses, its REST collection)
    "campaignBudgetOperation": ("campaignBudgetResult", "campaignBudgets"),
    "campaignOperation": ("campaignResult", "campaigns"),
    "campaignCriterionOperation": ("campaignCriterionResult", "campaignCriteria"),
    "adGroupOperation": ("adGroupResult", "adGroups"),
    "adGroupCriterionOperation": ("adGroupCriterionResult", "adGroupCriteria"),
    "adGroupAdOperation": ("adGroupAdResult", "adGroupAds"),
    "assetOperation": ("assetResult", "assets"),
    "campaignAssetOperation": ("campaignAssetResult", "campaignAssets"),
    "conversionActionOperation": ("conversionActionResult", "conversionActions"),
    "customerOperation": ("customerResult", "customers"),
}
SEARCHES = (  # a GAQL query's marker -> its kind (the campaign lookup first: the others never contain it)
    # A3 Task 5 (ads_run.sync): each more specific than "FROM campaign WHERE", so checked first.
    ("metrics.cost_micros", "metrics"),
    ("campaign.primary_status", "status"),
    ("policy_summary", "policy"),
    ("FROM campaign WHERE", "lookup"),
    ("FROM ad_group_ad ", "read_ads"),
    ("FROM ad_group_criterion ", "read_keywords"),
    ("FROM campaign_criterion ", "read_criteria"),
    ("FROM campaign_asset ", "read_assets"),
    ("conversion_tracking_setting", "customer"),  # the conversion setup's customer read (it asks the time zone too)
    ("FROM customer", "time_zone"),  # sync's own read of the account's time zone (ruling S14), when it has none
    ("FROM conversion_action", "actions"),
)
# ruling S14: the metrics search names its three days, the account's today and the two before it
BETWEEN = re.compile(r"segments\.date BETWEEN '(\d{4}-\d{2}-\d{2})' AND '(\d{4}-\d{2}-\d{2})'")


def google_error(
    status: int, *codes: dict, message: str = "raw words devtok_SECRET 1//refresh_SECRET"
) -> httpx.Response:
    details = [{"errors": [{"errorCode": c, "message": message} for c in codes]}] if codes else []
    rpc = {400: "INVALID_ARGUMENT", 403: "PERMISSION_DENIED", 503: "UNAVAILABLE"}.get(status, "UNKNOWN")
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "status": rpc, "details": details}}
    )


class FakeGoogle:
    """`sent` holds (kind, body) per Google Ads call in order (the token exchange is not one). `fail[kind]`
    answers that kind with a response, or raises an exception, instead; `lose` holds kinds that are done and whose
    answer then never arrives; `down` fails every request, the token exchange included; `delay` slows the token
    exchange (the budget tests: through `sleep`, which a test points at a `StepClock`). Mutates are "create", "enable", "pause", "money" (budget and CPC ceiling updates
    alone, ruling S16), "diff" (any other update), "conversions", "call_reporting", or "validate_*"."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, httpx.Response | Exception] = {}
        self.lose: set[str] = set()
        self.down = False
        self.delay = 0.0
        self.sleep = time.sleep  # how the token exchange's `delay` passes
        self.ids = itertools.count(101)
        self.campaigns: dict[str, dict] = {}  # resource name -> name, status, budget, amount
        self.groups: dict[str, dict] = {}  # ad group resource name -> campaign, name, status
        self.built: dict = {}  # resources_from() of the last real create: what the recovery reads answer with
        self.actions: dict[str, str] = {}  # conversion action name -> resource name
        self.geo: dict[str, list[str]] = {}  # location name -> Google's suggestions, best first
        self.time_zone = "America/Los_Angeles"  # the account's own (customer.time_zone)
        # A3 Task 5 (ads_run.sync): rows a test sets up for the three sync queries to answer with.
        self.day_metrics: list[dict] = []  # segments.date/metrics.* rows, Google REST shape; answered in range only
        self.metric_days: list[tuple[str, str]] = []  # the (first, last) day of every metrics search, in order
        self.campaign_status: dict | None = {"status": "ENABLED", "primaryStatus": "ELIGIBLE"}
        self.ad_policy: list[dict] = []  # adGroupAd/policySummary rows, Google REST shape

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("Google is down", request=request)
        if str(request.url) == google_auth.TOKEN_URL:
            self.sleep(self.delay)
            return httpx.Response(200, json={"access_token": "ya29.SECRET", "expires_in": 3599})
        body = json.loads(request.content)
        if request.url.path.endswith("googleAds:search") and "pageSize" in body:  # v25: refused since v17 (C1)
            self.sent.append(("page_size", body))
            return google_error(400, {"requestError": "PAGE_SIZE_NOT_SUPPORTED"})
        kind = self.kind(request.url.path, body)
        self.sent.append((kind, body))
        failure = self.fail.get(kind)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        answer = httpx.Response(200, json={}) if kind.startswith("validate_") else getattr(self, f"_{kind}")(body)
        if kind in self.lose:
            self.lose.discard(kind)
            raise httpx.ReadTimeout("the answer never came back", request=request)
        return answer

    @staticmethod
    def kind(path: str, body: dict) -> str:
        if path.endswith("geoTargetConstants:suggest"):
            return "suggest"
        if path.endswith("googleAds:search"):
            return next(kind for marker, kind in SEARCHES if marker in body["query"])
        ops = body["mutateOperations"]
        first = next(iter(ops[0]))
        if first == "conversionActionOperation":
            kind = "conversions"
        elif first == "customerOperation":
            kind = "call_reporting"
        elif any("create" in op.get("campaignOperation", {}) for op in ops):
            kind = "create"
        else:
            statuses = {
                op["campaignOperation"]["update"].get("status")
                for op in ops
                if "update" in op.get("campaignOperation", {})
            }
            kind = "enable" if "ENABLED" in statuses else "pause" if "PAUSED" in statuses else "diff"
            if kind == "diff" and all(FakeGoogle.money_op(op) for op in ops):
                kind = "money"
        return f"validate_{kind}" if body.get("validateOnly") else kind

    @staticmethod
    def money_op(op: dict) -> bool:
        """A budget amount or CPC ceiling update: what `ads_launch.money` builds."""
        budget = op.get("campaignBudgetOperation", {})
        campaign = op.get("campaignOperation", {})
        return "update" in budget or campaign.get("updateMask") == "targetSpend.cpcBidCeilingMicros"

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.sent]

    def bodies(self, kind: str) -> list[dict]:
        return [body for k, body in self.sent if k == kind]

    # ---- mutates

    def _results(self, ops: list[dict]) -> list[dict]:
        out = []
        for op in ops:
            kind = next(iter(op))
            result_key, collection = RESULTS[kind]
            body = op[kind]
            name = (body.get("update") or {}).get("resourceName") or body.get("remove")
            out.append({result_key: {"resourceName": name or f"customers/{CID}/{collection}/{next(self.ids)}"}})
        return out

    def _create(self, body: dict) -> httpx.Response:
        ops = body["mutateOperations"]
        campaign = next(op["campaignOperation"]["create"] for op in ops if "campaignOperation" in op)
        if any(c["name"] == campaign["name"] and c["status"] != "REMOVED" for c in self.campaigns.values()):
            return google_error(400, {"campaignError": "DUPLICATE_CAMPAIGN_NAME"})
        responses = self._results(ops)
        self.built = ads_launch.resources_from(responses, ops)
        budget = next(op["campaignBudgetOperation"]["create"] for op in ops if "campaignBudgetOperation" in op)
        self.campaigns[self.built["campaign"]] = {
            "name": campaign["name"],
            "status": campaign["status"],
            "budget": self.built["campaign_budget"],
            "amount": budget["amountMicros"],
        }
        for theme, ref in self.built["ad_groups"].items():
            self.groups[ref] = {"campaign": self.built["campaign"], "name": theme, "status": "ENABLED"}
        return httpx.Response(200, json={"mutateOperationResponses": responses})

    def _apply(self, body: dict) -> httpx.Response:
        ops = body["mutateOperations"]
        taken = {(g["campaign"], g["name"]) for g in self.groups.values() if g["status"] != "REMOVED"}
        for op in ops:  # checked before anything is applied: partial failure is off, a refusal is the whole call
            group = (op.get("adGroupOperation") or {}).get("create")
            if group and (group["campaign"], group["name"]) in taken:
                return google_error(400, {"adGroupError": "DUPLICATE_ADGROUP_NAME"})
            if group:
                taken.add((group["campaign"], group["name"]))
            campaign = (op.get("campaignOperation") or {}).get("update") or {}
            if campaign and campaign["resourceName"] not in self.campaigns:  # Google has no such campaign (any more)
                return google_error(400, {"mutateError": "RESOURCE_NOT_FOUND"})
        responses = self._results(ops)
        for op, response in zip(ops, responses, strict=True):
            update = (op.get("campaignOperation") or {}).get("update") or {}
            if "status" in update:
                self.campaigns[update["resourceName"]]["status"] = update["status"]
            budget = (op.get("campaignBudgetOperation") or {}).get("update") or {}
            for c in self.campaigns.values():
                if budget and c["budget"] == budget["resourceName"]:
                    c["amount"] = budget["amountMicros"]
            group = op.get("adGroupOperation") or {}
            if "update" in group:
                self.groups[group["update"]["resourceName"]]["status"] = group["update"]["status"]
            if "create" in group:
                name = response["adGroupResult"]["resourceName"]
                self.groups[name] = {"campaign": group["create"]["campaign"], "name": group["create"]["name"]}
                self.groups[name]["status"] = group["create"]["status"]
        return httpx.Response(200, json={"mutateOperationResponses": responses})

    _enable = _pause = _diff = _money = _apply

    def _conversions(self, body: dict) -> httpx.Response:
        ops = body["mutateOperations"]
        responses = self._results(ops)
        for op, resp in zip(ops, responses, strict=True):
            self.actions[op["conversionActionOperation"]["create"]["name"]] = resp["conversionActionResult"][
                "resourceName"
            ]
        return httpx.Response(200, json={"mutateOperationResponses": responses})

    def _call_reporting(self, body: dict) -> httpx.Response:
        return httpx.Response(200, json={"mutateOperationResponses": self._results(body["mutateOperations"])})

    # ---- searches

    def _lookup(self, body: dict) -> httpx.Response:
        query = body["query"]
        rows = [
            {
                "campaign": {"resourceName": name, "name": c["name"], "status": c["status"]},
                "campaignBudget": {"resourceName": c["budget"], "amountMicros": str(c["amount"])},
            }
            for name, c in self.campaigns.items()
            if f"campaign.resource_name = '{name}'" in query or ("LIKE" in query and c["status"] != "REMOVED")
        ]
        return httpx.Response(200, json={"results": rows})

    def _read_ads(self, body: dict) -> httpx.Response:
        b = self.built
        rows = [
            {
                "adGroup": {"resourceName": b["ad_groups"][theme], "name": theme},
                "adGroupAd": {"resourceName": ad, "ad": {"finalUrls": [b["final_url"]]} if b.get("final_url") else {}},
            }
            for theme, ad in b["ads"].items()
        ]
        return httpx.Response(200, json={"results": rows})

    def _read_keywords(self, body: dict) -> httpx.Response:
        b = self.built
        rows = [
            {
                "adGroup": {"resourceName": b["ad_groups"][theme], "name": theme},
                "adGroupCriterion": {"resourceName": name, "keyword": {"text": text}},
            }
            for theme, words in b["keywords"].items()
            for text, name in words.items()
        ]
        return httpx.Response(200, json={"results": rows})

    def _read_criteria(self, body: dict) -> httpx.Response:
        b = self.built
        rows = [
            {"campaignCriterion": {"resourceName": n, "type": "KEYWORD", "negative": True, "keyword": {"text": t}}}
            for t, n in b["negatives"].items()
        ]
        rows += [{"campaignCriterion": {"resourceName": n, "type": "PROXIMITY"}} for n in b["geo_criteria"]]
        rows.append(
            {"campaignCriterion": {"resourceName": f"customers/{CID}/campaignCriteria/9~1000", "type": "LANGUAGE"}}
        )
        return httpx.Response(200, json={"results": rows})

    def _read_assets(self, body: dict) -> httpx.Response:
        b = self.built
        rows = (
            [{"campaignAsset": {"resourceName": b["campaign_asset"], "asset": b["call_asset"]}}]
            if "campaign_asset" in b
            else []
        )
        return httpx.Response(200, json={"results": rows})

    def _customer(self, body: dict) -> httpx.Response:
        customer = {
            "resourceName": f"customers/{CID}",
            "conversionTrackingSetting": {"conversionTrackingId": "555"},
            "callReportingSetting": {"callReportingEnabled": True, "callConversionReportingEnabled": True},
            "timeZone": self.time_zone,
        }
        return httpx.Response(200, json={"results": [{"customer": customer}]})

    def _time_zone(self, body: dict) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"customer": {"timeZone": self.time_zone}}]})

    def _actions(self, body: dict) -> httpx.Response:
        rows = [
            {
                "conversionAction": {
                    "resourceName": name,
                    "name": action,
                    "tagSnippets": [
                        {
                            "pageFormat": "HTML",
                            "eventSnippet": f"gtag('event', 'conversion', {{'send_to': 'AW-555/L{i}'}});",
                        }
                    ],
                }
            }
            for i, (action, name) in enumerate(self.actions.items())
        ]
        return httpx.Response(200, json={"results": rows})

    def _metrics(self, body: dict) -> httpx.Response:
        """Ruling S14: only `segments.date BETWEEN '<first>' AND '<last>'`, three days, is answered (GAQL has no
        LAST_3_DAYS), and only with the rows whose day is in that range -- as Google answers."""
        found = BETWEEN.search(body["query"])
        if "DURING" in body["query"] or found is None:
            return google_error(400, {"queryError": "INVALID_VALUE_WITH_DURING_OPERATOR"})
        first, last = found.groups()
        assert date.fromisoformat(last) - date.fromisoformat(first) == timedelta(days=2), (first, last)  # three days
        self.metric_days.append((first, last))
        rows = [r for r in self.day_metrics if first <= r["segments"]["date"] <= last]
        return httpx.Response(200, json={"results": rows})

    def _status(self, body: dict) -> httpx.Response:
        rows = [{"campaign": self.campaign_status}] if self.campaign_status else []
        return httpx.Response(200, json={"results": rows})

    def _policy(self, body: dict) -> httpx.Response:
        return httpx.Response(200, json={"results": self.ad_policy})

    def _suggest(self, body: dict) -> httpx.Response:
        rows = [
            {"geoTargetConstant": {"resourceName": geo}, "searchTerm": name}
            for name in body["locationNames"]["names"]
            for geo in self.geo.get(name, [])
        ]
        return httpx.Response(200, json={"geoTargetConstantSuggestions": rows})


@pytest.fixture
def gads(monkeypatch, ads_api):
    """Google Ads faked for every client the Google Search adapter makes (launch, launch_now, the ads jobs): its
    client factory, which A3 kept in ads_run, patched where it lives since M0 Task 4."""
    fake = FakeGoogle()
    monkeypatch.setattr(google, "client", functools.partial(REAL_CLIENT, transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")


class StepClock:
    """The monotonic clock of the launch budget (`ads_run.launch_now`'s deadline and the Google client's clamp on each
    request), standing still until a test moves it: a budget test then reads no wall-clock speed."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def step_clock(monkeypatch) -> StepClock:
    """`StepClock` in place of the `time` module where the launch budget reads it (both use `monotonic` alone)."""
    stepped = StepClock()
    monkeypatch.setattr(ads_run, "time", stepped)
    monkeypatch.setattr(google, "time", stepped)
    return stepped


@pytest.fixture
def clock(monkeypatch):
    """`clock(instant)` pins the clock every account date is read from (`ads_run.now`, ruling S14)."""

    def at(instant: datetime) -> None:
        monkeypatch.setattr(ads_run, "now", lambda: instant)

    return at


def row(conn, biz: str = BIZ) -> dict | None:
    return ads_run.get(conn, biz, GOOGLE)


def approve(client, conn, key: str, plan_id: str, *, biz: str = BIZ, idem: str = "c1") -> dict:
    """The owner's code relayed by the assistant: confirm_proposal on this plan's proposal (the approval hook runs)."""
    prop = proposals.propose_plan(conn, biz, plan_id)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    res = call_tool(
        client,
        "confirm_proposal",
        {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": idem},
        key=key,
    )
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def queued(conn, plan_id: str, *, biz: str = BIZ) -> None:
    """The row an approval leaves (proposals.apply_approve_plan -> ads_run.queue)."""
    ads_run.queue(conn, biz, GOOGLE, plan_id, ready=True)


def launched(conn, gads) -> tuple[str, str]:
    """A business whose v1 ($1,000 a month) is live. Returns (key, v1's plan id)."""
    key, v1 = seed(conn)
    queued(conn, v1)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live", out
    return key, v1


def plain(text: str) -> bool:
    return not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


# ---------------------------------------------------------------- validate and live launches


def test_validate_mode_checks_the_create_with_google_and_builds_nothing(conn, gads, monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'queued')",
        (BIZ, GOOGLE, plan_id),
    )
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.kinds() == ["validate_create"]  # no lookup: nothing is built, so nothing can be duplicated
    body = gads.bodies("validate_create")[0]
    assert body["validateOnly"] is True and body["partialFailure"] is False
    assert next(iter(body["mutateOperations"][0])) == "campaignBudgetOperation"
    r = row(conn)
    # plan P4: A3's `validated` is `queued` with validated_at set
    assert (r["state"], r["plan_id"], r["resources"], r["last_error"]) == ("queued", plan_id, {}, None)
    assert r["validated_at"] is not None and r["launched_at"] is None
    assert out["state"] == "queued" and out["ops"] == len(body["mutateOperations"])
    assert "found nothing to fix" in out["say"] and plain(out["say"])


def test_live_launch_creates_everything_paused_then_enables_and_stores_every_resource(conn, gads, live):
    _key, plan_id = seed(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'queued')",
        (BIZ, GOOGLE, plan_id),
    )
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    assert gads.kinds() == ["lookup", "create", "enable"]
    lookup = gads.bodies("lookup")[0]["query"]
    assert "campaign.name LIKE 'Admiry · %biz[_]1'" in lookup and "campaign.status != 'REMOVED'" in lookup
    create = gads.bodies("create")[0]
    assert create["validateOnly"] is False and create["partialFailure"] is False
    campaign = next(op["campaignOperation"]["create"] for op in create["mutateOperations"] if "campaignOperation" in op)
    assert campaign["status"] == "PAUSED" and campaign["name"] == "Admiry · Bright Smile Dental · biz_1"
    budget = next(
        op["campaignBudgetOperation"]["create"] for op in create["mutateOperations"] if "campaignBudgetOperation" in op
    )
    assert budget["amountMicros"] == ads_launch.daily_micros(1000)  # 1000 / 30.4 = 32.894… -> $32.89
    ad = next(op["adGroupAdOperation"]["create"] for op in create["mutateOperations"] if "adGroupAdOperation" in op)
    assert ad["ad"]["finalUrls"] == [PAGE_URL + "?src=gads"]  # P3
    asset = next(op["assetOperation"]["create"] for op in create["mutateOperations"] if "assetOperation" in op)
    assert asset["callAsset"]["callConversionAction"] == f"customers/{CID}/conversionActions/77"

    stored = gads.built
    assert gads.bodies("enable")[0]["mutateOperations"] == ads_launch.enable(stored["campaign"])
    assert gads.campaigns[stored["campaign"]]["status"] == "ENABLED"

    r = row(conn)
    assert (r["state"], r["plan_id"], r["channel"], r["last_error"]) == ("live", plan_id, GOOGLE, None)
    # every call went to the linked account's Customer ID, which the row keeps as the account that built the ads
    accounts = {q.url.path.split("/")[3] for q in gads.requests if q.url.path.startswith("/v25/customers/")}
    assert accounts == {CID}
    assert r["launched_at"] is not None and r["resources"] == {**stored, "customer_id": CID}
    assert set(stored) >= {"campaign", "campaign_budget", "ad_groups", "ads", "keywords", "negatives", "geo_criteria"}
    assert list(stored["ad_groups"]) == ["invisalign palo alto"] and len(stored["geo_criteria"]) == 1
    assert out == {
        "state": "live",
        "say": out["say"],
        "ops": 1 + len(create["mutateOperations"]) + 1,  # the lookup, the create, the enable (ruling S4)
        "plan_id": plan_id,
        "last_error": None,
    }
    assert out["say"].startswith("Google Search: live") and plain(out["say"])


def test_the_audit_rows_carry_the_transition_never_a_request_body(client, conn, gads, live):
    key, plan_id = launched(conn, gads)
    rows = conn.execute(
        "SELECT actor, actor_class, args_redacted, result FROM agent_action WHERE tool = 'job.ads_launch'"
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["actor"], rows[0]["actor_class"], rows[0]["args_redacted"]) == ("job:ads-launch", "job", {})
    result = rows[0]["result"]
    assert set(result) == {"from", "to", "plan_id", "ops", "note"}
    assert (result["from"], result["to"], result["plan_id"]) == ("queued", "live", plan_id)
    audited = str(conn.execute("SELECT args_redacted, result FROM agent_action").fetchall())
    for body_part in ("mutateOperations", "Invisalign in Palo Alto", "campaignBudgets", "ya29.SECRET"):
        assert body_part not in audited
    entries = call_tool(client, "get_audit_log", {}, key=key)["structuredContent"]["data"]["entries"]
    sentences = [e["sentence"] for e in entries]
    assert "Admiry launched your ads on Google Search" in sentences
    assert all(plain(s) for s in sentences)


def test_a_missing_account_tag_runs_the_conversion_setup_first(conn, gads, live):
    seed(conn, conversions=False)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live"
    assert gads.kinds() == ["lookup", "customer", "actions", "conversions", "actions", "create", "enable"]
    tag = conn.execute("SELECT label FROM conversion_action WHERE business_id = %s AND kind = 'tag'", (BIZ,)).fetchone()
    assert tag["label"] == "AW-555"
    asset = next(
        op["assetOperation"]["create"] for op in gads.bodies("create")[0]["mutateOperations"] if "assetOperation" in op
    )
    assert asset["callAsset"]["callConversionAction"] == gads.actions["Admiry · Call from ad"]


def test_service_area_names_become_googles_best_location_for_each(conn, gads, live):
    knowledge = {**KNOWLEDGE, "listing": None, "service_area": ["Austin, TX", "Round Rock, TX"]}
    seed(conn, knowledge=knowledge)
    gads.geo = {
        "Austin, TX": ["geoTargetConstants/1026201", "geoTargetConstants/9999999"],
        "Round Rock, TX": ["geoTargetConstants/1026339"],
    }
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    assert [b["locationNames"]["names"] for b in gads.bodies("suggest")] == [["Austin, TX"], ["Round Rock, TX"]]
    criteria = [
        op["campaignCriterionOperation"]["create"]
        for op in gads.bodies("create")[0]["mutateOperations"]
        if "campaignCriterionOperation" in op
    ]
    assert [c["location"]["geoTargetConstant"] for c in criteria if "location" in c] == [
        "geoTargetConstants/1026201",
        "geoTargetConstants/1026339",
    ]
    assert not any("proximity" in c for c in criteria)


def test_a_service_area_google_cannot_place_waits_on_the_row(conn, gads, live):
    knowledge = {**KNOWLEDGE, "listing": None, "service_area": ["Nowhere Special"]}
    seed(conn, knowledge=knowledge)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert "create" not in gads.kinds()
    assert (out["state"], out["last_error"]) == ("queued", ads_run.NO_LOCATION)


def test_the_final_url_carries_src_gads_before_any_fragment(conn):
    seed(conn)
    for url, expected in (
        ("https://brightsmile.example/invisalign", "https://brightsmile.example/invisalign?src=gads"),
        ("https://brightsmile.example/p?a=1#top", "https://brightsmile.example/p?a=1&src=gads#top"),
        ("https://brightsmile.example/p?src=gads", "https://brightsmile.example/p?src=gads"),
    ):
        conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (url, BIZ))
        assert google._page_url(conn, BIZ) == expected


# ---------------------------------------------------------------- Review Focus 1: Google down at approval


def test_launch_after_approval_survives_google_down(client, conn, gads, live):
    key, plan_id = seed(conn, status="draft")
    gads.down = True
    envelope = approve(client, conn, key, plan_id)
    data = envelope["data"]
    assert (data["status"], data["plan_status"]) == ("confirmed", "approved")
    assert data["execution"]["ads"] == "queued"
    assert "Google did not answer; Admiry retries within the hour." in envelope["say"] and plain(envelope["say"])
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"], r["resources"]) == ("queued", plan_id, google.RETRYING, {})

    gads.down = False
    details = tick.run("ads-launch", url=TEST_DB_URL)
    assert details["modes"] == {GOOGLE: "live"} and details["states"] == {"live": 1} and details["errors"] == 0
    assert details["ops"] == sum(len(b.get("mutateOperations") or [None]) for _, b in gads.sent)
    assert gads.kinds() == ["lookup", "create", "enable"]
    assert row(conn)["state"] == "live" and row(conn)["last_error"] is None
    ledger = conn.execute("SELECT ok, details FROM job_run WHERE job = 'ads-launch'").fetchone()
    assert ledger["ok"] is True and ledger["details"]["ops"] == details["ops"]


# ---------------------------------------------------------------- Review Focus 2: the enable fails


def test_enable_failure_is_finished_by_the_sweeper_without_duplicates(conn, gads, live):
    seed(conn)
    gads.fail["enable"] = google_error(503)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert (out["state"], r["state"], r["last_error"]) == ("launching", "launching", google.RETRYING)
    assert r["resources"] == {**gads.built, "customer_id": CID}  # stored before the enable was sent
    assert r["launched_at"] is None
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"
    assert "turn on within the hour" in out["say"]

    del gads.fail["enable"]
    details = tick.run("ads-launch", url=TEST_DB_URL)
    assert details["states"] == {"live": 1}
    assert gads.kinds() == ["lookup", "create", "enable", "lookup", "enable"]
    assert gads.kinds().count("create") == 1 and len(gads.campaigns) == 1
    second_lookup = gads.bodies("lookup")[1]["query"]
    assert f"campaign.resource_name = '{gads.built['campaign']}'" in second_lookup  # the stored one, not a new one
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("live", None) and r["launched_at"] is not None


def test_a_lost_create_response_is_recovered_by_name_without_a_second_create(conn, gads, live):
    _key, plan_id = seed(conn)
    gads.lose = {"create"}  # Google built it all; the answer never arrived
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], out["last_error"]) == ("queued", google.RETRYING)
    assert row(conn)["resources"] == {"creating_plan_id": plan_id} and len(gads.campaigns) == 1

    tick.run("ads-launch", url=TEST_DB_URL)
    assert gads.kinds() == [
        "lookup",
        "create",
        "lookup",  # the name lookup finds it ...
        "read_ads",  # ... and its resources are read back
        "read_keywords",
        "read_criteria",
        "read_assets",
        "enable",
    ]
    r = row(conn)
    assert r["state"] == "live" and r["resources"] == {**gads.built, "customer_id": CID}  # a create's own shape
    assert gads.bodies("enable")[0]["mutateOperations"] == ads_launch.enable(gads.built["campaign"])


def test_a_campaign_found_with_another_budget_gets_the_plans_before_it_is_turned_on(conn, gads, live):
    seed(conn)
    gads.lose = {"create"}
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    gads.campaigns[gads.built["campaign"]]["amount"] = 99_000_000  # changed by hand in Google Ads meanwhile
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    ops = gads.bodies("enable")[0]["mutateOperations"]
    assert ops[0] == {
        "campaignBudgetOperation": {
            "update": {"resourceName": gads.built["campaign_budget"], "amountMicros": ads_launch.daily_micros(1000)},
            "updateMask": "amountMicros",
        }
    }
    assert ops[1:] == ads_launch.enable(gads.built["campaign"])  # in the same mutate: never on at $99 a day


def test_a_duplicate_name_answer_adopts_the_campaign_instead_of_failing(conn, gads, live):
    seed(conn)
    gads.fail["lookup"] = httpx.Response(200, json={"results": []})  # the lookup misses once ...
    gads.campaigns["customers/1234567890/campaigns/5"] = {
        "name": "Admiry · Bright Smile Dental · biz_1",
        "status": "PAUSED",
        "budget": "customers/1234567890/campaignBudgets/6",
        "amount": ads_launch.daily_micros(1000),
    }
    gads.built = {"ad_groups": {}, "ads": {}, "keywords": {}, "negatives": {}, "geo_criteria": []}
    original_create = gads._create

    def create_then_see(body):
        gads.fail.pop("lookup", None)  # ... and sees it once Google says the name is taken
        return original_create(body)

    gads._create = create_then_see
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.kinds()[:3] == ["lookup", "create", "lookup"] and gads.kinds().count("create") == 1
    assert out["state"] == "live" and row(conn)["resources"]["campaign"] == "customers/1234567890/campaigns/5"


def test_a_refused_token_exchange_waits_for_an_operator_instead_of_failing_the_launch(conn, gads, live):
    seed(conn)

    def refuse_the_token(request):
        if str(request.url) == google_auth.TOKEN_URL:  # the refresh token was revoked: a 400, but no Google Ads code
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been revoked."})
        return gads.handler(request)

    with REAL_CLIENT(transport=httpx.MockTransport(refuse_the_token)) as http:
        out = ads_run.launch(conn, config.load(), BIZ, GOOGLE, http=http)
    assert (out["state"], out["last_error"]) == ("queued", google_ads.ACCESS_TROUBLE) and gads.sent == []
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}  # fixed: the next hour launches it


def test_a_request_google_refuses_fails_the_launch_and_is_not_retried(client, conn, gads, live):
    key, _v1 = seed(conn)
    gads.fail["create"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert (out["state"], r["state"]) == ("failed", "failed")
    assert gads.kinds() == ["lookup", "create"]  # nothing built, nothing on: no stop before the row fails (R1)
    assert r["last_error"] == google_ads.REFUSED_WITH_REASON.format("policyFindingError: POLICY_FINDING")
    assert "could not be launched" in out["say"] and plain(out["say"])
    before = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0 and len(gads.sent) == before  # not retried

    del gads.fail["create"]  # the owner revises the ad text and approves again
    v2 = add_plan(conn, sections(headlines=[*HEADLINES, "Family Dentist Open Late"]), status="draft")
    approve(client, conn, key, v2)
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"]) == ("live", v2, None)


# ---------------------------------------------------------------- Review Focus 3 and later versions


def test_update_changes_the_budget_in_place(client, conn, gads, live):
    key, v1 = launched(conn, gads)
    built = dict(gads.built)
    sent_before = len(gads.sent)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    envelope = approve(client, conn, key, v2, idem="c2")

    after = gads.sent[sent_before:]
    assert [k for k, _ in after] == ["money"]  # the money alone (ruling S16): nothing else moved, nothing re-created
    assert after[0][1]["mutateOperations"] == [
        {
            "campaignBudgetOperation": {
                "update": {"resourceName": built["campaign_budget"], "amountMicros": ads_launch.daily_micros(600)},
                "updateMask": "amountMicros",
            }
        },
        {
            "campaignOperation": {
                "update": {
                    "resourceName": built["campaign"],
                    "targetSpend": {"cpcBidCeilingMicros": ads_launch.cpc_ceiling_micros(sections()["estimate"])},
                },
                "updateMask": "targetSpend.cpcBidCeilingMicros",
            }
        },
    ]
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"]) == ("live", v2, None)
    assert r["resources"] == {**built, "customer_id": CID}
    assert gads.campaigns[built["campaign"]]["amount"] == ads_launch.daily_micros(600)
    assert envelope["data"]["execution"]["ads"] == "live"
    statuses = {p["id"]: p["status"] for p in conn.execute("SELECT id, status FROM plan")}
    assert statuses == {v1: "superseded", v2: "approved"}
    note = conn.execute(
        "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()["note"]
    assert note == "updated your ads on Google Search to plan v2"


def test_new_ad_text_and_negatives_are_merged_into_the_stored_resources(client, conn, gads, live):
    key, _v1 = launched(conn, gads)
    old = dict(gads.built)
    v2 = add_plan(
        conn,
        sections(headlines=[*HEADLINES[:2], "Evening Appointments Too"], negatives=("jobs", "cheap")),
        status="draft",
    )
    approve(client, conn, key, v2)
    diff = gads.bodies("diff")[0]["mutateOperations"]
    assert not any("campaignBudgetOperation" in op for op in diff)  # the budget did not change
    r = row(conn)["resources"]
    theme = "invisalign palo alto"
    assert r["ads"][theme] != old["ads"][theme]  # the new ad, keyed by its theme through the stored ad group
    assert {"update": {"resourceName": old["ads"][theme], "status": "PAUSED"}, "updateMask": "status"} in [
        op["adGroupAdOperation"] for op in diff if "adGroupAdOperation" in op
    ]
    assert set(r["negatives"]) == set(old["negatives"]) | {"cheap"}
    assert r["ad_groups"] == old["ad_groups"] and r["keywords"] == old["keywords"]


def test_a_version_approved_mid_launch_is_applied_before_the_enable(client, conn, gads, live):
    key, _v1 = seed(conn)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert row(conn)["state"] == "launching"
    del gads.fail["enable"]
    sent_before = len(gads.sent)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    approve(client, conn, key, v2)
    assert [k for k, _ in gads.sent[sent_before:]] == ["money", "lookup", "enable"]  # lowered while still paused
    assert gads.bodies("enable")[-1]["mutateOperations"] == ads_launch.enable(gads.built["campaign"])
    r = row(conn)
    assert (r["state"], r["plan_id"]) == ("live", v2)
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (campaign["status"], campaign["amount"]) == ("ENABLED", ads_launch.daily_micros(600))


def test_a_refused_update_keeps_the_ads_running_and_stops_retrying_that_version(client, conn, gads, live):
    key, v1 = launched(conn, gads)
    gads.fail["money"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    envelope = approve(client, conn, key, v2)
    r = row(conn)
    assert (r["state"], r["plan_id"], r["resources"]["refused_plan_id"]) == ("live", v1, v2)
    assert envelope["data"]["execution"]["ads"] == "live"
    assert f"{google.REFUSED_CHANGE}." in envelope["say"] and plain(envelope["say"])  # ruling S15
    before = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0 and len(gads.sent) == before
    note = conn.execute(
        "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()["note"]
    assert note.startswith("could not update your ads on Google Search to plan v2: ") and plain(note)


def test_a_lost_update_answer_is_sent_again_and_its_money_operations_are_idempotent(client, conn, gads, live):
    key, v1 = launched(conn, gads)
    built = dict(gads.built)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    gads.lose = {"money"}  # Google lowered the budget; the answer never came back
    approve(client, conn, key, v2)
    assert (row(conn)["plan_id"], row(conn)["last_error"]) == (v1, google.RETRYING)
    tick.run("ads-launch", url=TEST_DB_URL)
    diffs = gads.bodies("money")
    assert len(diffs) == 2 and diffs[0] == diffs[1]  # the same amount and ceiling, set twice: nothing more is spent
    assert gads.campaigns[built["campaign"]]["amount"] == ads_launch.daily_micros(600)
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"], r["resources"]) == (
        "live",
        v2,
        None,
        {**built, "customer_id": CID},
    )


def test_a_campaign_removed_in_google_ads_before_it_is_turned_on_fails_with_one_sentence(conn, gads, live):
    seed(conn)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert row(conn)["state"] == "launching"
    del gads.fail["enable"]
    gads.campaigns[gads.built["campaign"]]["status"] = "REMOVED"  # by hand, in Google Ads
    tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("failed", google.GONE) and gads.kinds().count("enable") == 1
    assert plain(ads_run.status_sentence(r))


def _serving_mid_launch(conn, gads) -> None:
    """A launch whose enable reached Google while its answer did not (review fix round 1, Important 1): the row stays
    `launching`, by design, for the next step to finish -- and Google serves the ads."""
    seed(conn)
    gads.lose = {"enable"}
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert (row(conn)["state"], gads.campaigns[gads.built["campaign"]]["status"]) == ("launching", "ENABLED")


POLICY_REFUSAL = google_ads.REFUSED_WITH_REASON.format("policyFindingError: POLICY_FINDING")
# Google's answer to any change of a campaign removed in Google Ads (the tests' stand-in: nothing keys on it)
REMOVED_CHANGE = {"operationAccessDeniedError": "OPERATION_NOT_PERMITTED_FOR_REMOVED_RESOURCE"}
REMOVED_REFUSAL = google_ads.REFUSED_WITH_REASON.format(
    "operationAccessDeniedError: OPERATION_NOT_PERMITTED_FOR_REMOVED_RESOURCE"
)


def _met_by_the_enable(conn, gads) -> tuple[str, list[str]]:
    """GONE (A3): a launch whose enable Google did not answer, its campaign then removed by hand -- the next enable's
    own lookup finds it removed. Returns (the row's sentence, what the step sends)."""
    seed(conn)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["enable"]
    gads.campaigns[gads.built["campaign"]]["status"] = "REMOVED"  # by hand, in Google Ads
    return google.GONE, ["lookup", "pause", "lookup"]


def _met_by_a_newer_version(conn, gads) -> tuple[str, list[str]]:
    """Ads Google serves mid-launch, their campaign then removed by hand: a newer version's money meets it first."""
    _serving_mid_launch(conn, gads)
    gads.campaigns[gads.built["campaign"]]["status"] = "REMOVED"
    gads.fail["money"] = google_error(400, REMOVED_CHANGE)
    add_plan(conn, sections(monthly_usd=600.0))
    return REMOVED_REFUSAL, ["money", "pause", "lookup"]


def _met_by_a_page_move(conn, gads) -> tuple[str, list[str]]:
    """The same, met first by a move of the page (its money first, ruling S16)."""
    _serving_mid_launch(conn, gads)
    gads.campaigns[gads.built["campaign"]]["status"] = "REMOVED"
    gads.fail["money"] = google_error(400, REMOVED_CHANGE)
    new = "https://brightsmile.example/new"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new, BIZ))
    assert ads_run.flag_moved_page(conn, BIZ, PAGE_URL, new)
    return REMOVED_REFUSAL, ["money", "pause", "lookup"]


@pytest.mark.parametrize(
    "met", [_met_by_the_enable, _met_by_a_newer_version, _met_by_a_page_move], ids=["enable", "newer", "page move"]
)
def test_a_campaign_removed_by_hand_fails_its_row_in_one_step_once_googles_listing_shows_the_stop_done(
    conn, gads, live, met
):
    """M1 follow-up fix round 1 (F1): a row with ads built gets the channel's stop before it fails, and Google Ads
    refuses that too for a campaign removed there. Its own listing says the rest -- a campaign it lists as removed, or
    not at all, serves nothing -- so the refused stop counts as done (a lookup, never an error code) and the row fails
    in the same step, however the step met the campaign: the enable's own lookup (GONE: a stop is sent, and its refusal
    read as done), a newer version's money, or a page move's. No hourly loop follows."""
    sentence, kinds = met(conn, gads)
    gads.fail["pause"] = google_error(400, REMOVED_CHANGE)
    sent = len(gads.sent)
    tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("failed", sentence)
    assert [kind for kind, _ in gads.sent[sent:]] == kinds  # the stop, refused, then Google's listing
    sent = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0 and len(gads.sent) == sent  # nothing to retry


def test_a_pause_of_ads_google_lists_as_serving_always_sends_paused(conn, gads, live):
    """F3: nothing kept on the row can skip a pause. A campaign Google listed as removed once (its refused stop read as
    done), listed again later as serving (enabled again by hand: an assumption), is paused like any other -- PAUSED is
    sent and taken, and nothing is owed."""
    _met_by_the_enable(conn, gads)
    gads.fail["pause"] = google_error(400, REMOVED_CHANGE)
    tick.run("ads-launch", url=TEST_DB_URL)
    assert row(conn)["state"] == "failed"
    del gads.fail["pause"]
    campaign = gads.campaigns[gads.built["campaign"]]
    campaign["status"] = "PAUSED"  # listed again
    v2 = add_plan(conn, sections(monthly_usd=600.0))
    ads_run.queue(conn, BIZ, GOOGLE, v2, ready=True)  # as the approval queues a row whose ads are built
    assert (ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"], campaign["status"]) == ("live", "ENABLED")
    sent = len(gads.sent)
    out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    assert [kind for kind, _ in gads.sent[sent:]] == ["pause"]
    assert (out["state"], campaign["status"], row(conn)["resources"].get("pause_owed")) == ("paused", "PAUSED", None)


def test_a_version_google_refuses_while_the_ads_may_serve_pauses_them_before_the_row_fails(conn, gads, live):
    """M1 follow-up R1 on Google Search (M0's lifecycle is shared): a newer version's update, applied before the enable
    of ads Google already serves, is refused (its money); the row fails -- read by nothing then: no sync, no metrics, no
    Pause on the card -- only once one stop paused the campaign."""
    _serving_mid_launch(conn, gads)
    gads.fail["money"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    add_plan(conn, sections(monthly_usd=600.0))
    sent = len(gads.sent)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], row(conn)["state"], out["last_error"]) == ("failed", "failed", POLICY_REFUSAL)
    assert [kind for kind, _ in gads.sent[sent:]] == ["money", "pause"]
    assert gads.bodies("pause")[-1]["mutateOperations"] == google._status_op(gads.built["campaign"], "PAUSED")
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [
        (google_error(503), google.RETRYING),
        (
            google_error(400, {"mutateError": "RESOURCE_NOT_FOUND"}),
            google_ads.REFUSED_WITH_REASON.format("mutateError: RESOURCE_NOT_FOUND"),
        ),
    ],
    ids=["busy", "refused"],
)
def test_a_pause_google_does_not_take_is_owed_and_sent_alone_until_one_goes_through(conn, gads, live, answer, sentence):
    """R1: the stop can fail too -- Google busy, or refusing it for a campaign it still lists: the campaign may still
    serve, so the row is not failed. Fix round 1 (F2): the stop is owed (`stop_owed` keeps the refusal's sentence) and
    the row says the stop's own; the hourly sweep sends that stop alone -- no money again, no enable -- and once it goes
    through, the row fails with the version's refusal and the campaign paused."""
    _serving_mid_launch(conn, gads)
    gads.fail["money"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    gads.fail["pause"] = answer
    v2 = add_plan(conn, sections(monthly_usd=600.0))
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    campaign = gads.campaigns[gads.built["campaign"]]
    r = row(conn)
    assert (out["state"], r["state"], out["last_error"]) == ("launching", "launching", sentence)
    owed = {"sentence": POLICY_REFUSAL, "plan_id": v2}  # the refusal, and the version it refused (fix round 2)
    assert (r["resources"].get("stop_owed"), campaign["status"]) == (owed, "ENABLED")
    del gads.fail["pause"]
    sent = len(gads.sent)
    tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    assert [kind for kind, _ in gads.sent[sent:]] == ["pause"]  # the owed stop alone
    assert (r["state"], r["last_error"], campaign["status"]) == ("failed", POLICY_REFUSAL, "PAUSED")
    assert "stop_owed" not in r["resources"]


def test_a_retryable_update_failure_is_retried_by_the_sweeper(client, conn, gads, live):
    key, _v1 = launched(conn, gads)
    gads.fail["money"] = google_error(503)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    approve(client, conn, key, v2)
    assert (row(conn)["plan_id"], row(conn)["last_error"]) != (v2, None)
    del gads.fail["money"]
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}
    assert (row(conn)["plan_id"], row(conn)["last_error"]) == (v2, None)


# ---------------------------------------------------------------- precheck (P3, P4, D44)


def _no_link(conn):
    conn.execute("DELETE FROM ads_link")


def _pending_link(conn):
    conn.execute("UPDATE ads_link SET status = 'pending'")


def _no_page(conn):
    conn.execute("DELETE FROM page")


def _unchecked(conn):
    conn.execute("UPDATE page SET check_json = NULL")


def _check_failed(conn):
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(CHECK_FAILED),))


def _preview_only(conn):
    conn.execute("UPDATE page SET status = 'preview', url = ''")


def _knowledge(conn, **changes):
    conn.execute("UPDATE knowledge SET json = %s", (Jsonb({**KNOWLEDGE, **changes}),))


def _prepaid(conn):
    """The business prepays (A3c-1): no link of its own, Google Search's account row managed, and a balance."""
    conn.execute("DELETE FROM ads_link")
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    google_ads_link.mirror(conn, BIZ)
    wallet.add_entry(conn, BIZ, kind="topup", amount_cents=50_000, ref="pi_prepaid", created_by="stripe")


@pytest.mark.parametrize(
    ("change", "sentence"),
    [
        (_no_link, google.NOT_LINKED),
        (_pending_link, google.LINK_PENDING),
        (_no_page, ads_run.NO_PAGE),
        (_preview_only, ads_run.NO_PAGE),
        (_unchecked, ads_run.PAGE_UNCHECKED.format(label="Google Search")),
        (_check_failed, ads_run.PAGE_FIX.format(fix=PHONE_FIX)),  # the check's first fix sentence
        (lambda c: _knowledge(c, phone=None), ads_run.NO_PHONE),
        (lambda c: _knowledge(c, listing=None, service_area=[]), ads_run.NO_LOCATION),
        (lambda c: _knowledge(c, listing={**KNOWLEDGE["listing"], "lat": None}, service_area=[]), ads_run.NO_LOCATION),
    ],
)
def test_precheck_refuses_with_one_owner_sentence(conn, change, sentence):
    _key, plan_id = seed(conn)
    change(conn)
    plan = plans.get(conn, BIZ, plan_id)
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, plan)
    assert str(e.value) == sentence and plain(sentence)


@pytest.mark.parametrize(
    ("secs", "sentence"),
    [
        (sections(radius_miles=None), ads_run.NO_RADIUS),
        (
            sections(headlines=["Invisalign in Palo Alto", "Best Smiles In Town!", "Same-Week Appointments"]),
            google.TOO_LITTLE_COPY,
        ),
        (sections(descriptions=["Clear aligners from a family dentist. Book online."]), google.TOO_LITTLE_COPY),
        (sections(monthly_usd=2500.0), ads_run.OVER_LIMIT),
        ({k: v for k, v in sections().items() if k != "consultation"}, google.NOT_SEARCH_PLAN),
        ({**sections(), "estimate": {"basis": "none"}}, google.NOT_SEARCH_PLAN),
    ],
)
def test_precheck_refuses_a_plan_google_search_cannot_run(conn, secs, sentence):
    _key, _plan_id = seed(conn)
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, {"id": "pl_x", "sections": secs})
    assert str(e.value) == sentence and plain(sentence)


def test_precheck_passes_a_ready_business_and_a_service_area_without_a_listing(conn):
    _key, plan_id = seed(conn)
    ads_run.precheck(conn, config.load(), BIZ, plans.get(conn, BIZ, plan_id))
    _knowledge(conn, listing=None, service_area=["Palo Alto"])
    ads_run.precheck(conn, config.load(), BIZ, plans.get(conn, BIZ, plan_id))


def test_a_precheck_that_fails_at_launch_time_waits_on_the_row(conn, gads, live):
    _key, plan_id = seed(conn)
    conn.execute(  # approved while the page passed; its check failed since
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'queued')",
        (BIZ, GOOGLE, plan_id),
    )
    _check_failed(conn)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.sent == [] and out["state"] == "queued"
    assert out["last_error"] == ads_run.PAGE_FIX.format(fix=PHONE_FIX)
    assert out["say"] == f"Google Search: your ads are waiting to launch. {ads_run.PAGE_FIX.format(fix=PHONE_FIX)}"
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(CHECK_OK),))  # fixed: the next hour launches it
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}


# ---------------------------------------------------------------- the sweeper (ads-launch)


def _row_for(conn, biz: str, state: str, *, resources: dict | None = None, newer: bool = False, refused: bool = False):
    """A business's Google Search row in `state`, and the linked account it runs in (a Customer ID of its own: the
    row keeps none since M0, and a Customer ID links one business); A3's "validated" is written as plan P4 stores
    it (queued, with validated_at set)."""
    business_with_key(conn, biz_id=biz, claim_state="verified")
    cid = f"{zlib.crc32(biz.encode()):010d}"
    google_ads_link.save(conn, biz, cid, status="active")
    registry.save_account(conn, biz, GOOGLE, mode="own_account", status="active", external={"customer_id": cid})
    running = add_plan(conn, sections(), biz=biz, status="superseded" if newer else "approved")
    if newer:
        approved = add_plan(conn, sections(monthly_usd=600.0), biz=biz, status="approved")
        if refused:
            resources = {**(resources or {}), "refused_plan_id": approved}
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, validated_at) "
        "VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)",
        (
            biz,
            GOOGLE,
            running,
            "queued" if state == "validated" else state,
            Jsonb(resources or {}),
            state == "validated",
        ),
    )


def test_the_sweeper_takes_its_shards_unfinished_launches_and_skips_failed_and_live(conn, monkeypatch, live):
    """Live: queued, launching, validated (ruling S9) and a pending newer version; never failed, nor live or
    paused ads with nothing to apply, nor a version Google refused."""
    built = {"campaign": "customers/1/campaigns/1"}
    _row_for(conn, "biz_q", "queued")
    _row_for(conn, "biz_l", "launching", resources=built)
    _row_for(conn, "biz_f", "failed")
    _row_for(conn, "biz_v", "live", resources=built)
    _row_for(conn, "biz_u", "live", resources=built, newer=True)  # a newer approved version to apply
    _row_for(conn, "biz_r", "live", resources=built, newer=True, refused=True)
    _row_for(conn, "biz_p", "paused", newer=True)  # paused before anything was built
    _row_for(conn, "biz_val", "validated")
    calls: list[str] = []

    def fake_launch(conn_, settings, biz, channel, *, http=None):
        calls.append(biz)
        return {"state": "live", "ops": 3}

    monkeypatch.setattr(ads_run, "launch", fake_launch)
    shards = [tick.run("ads-launch", shard=f"{i}/2", url=TEST_DB_URL) for i in range(2)]
    assert sorted(calls) == ["biz_l", "biz_q", "biz_u", "biz_val"]
    assert sum(s["checked"] for s in shards) == 4 and sum(s["ops"] for s in shards) == 12
    owner = {
        r["b"]: r["s"]
        for r in conn.execute(
            "SELECT b, mod(abs(hashtext(b)::bigint), 2) AS s "
            "FROM unnest(ARRAY['biz_l', 'biz_q', 'biz_u', 'biz_val']) AS b"
        )
    }
    assert [s["checked"] for s in shards] == [sum(1 for v in owner.values() if v == i) for i in range(2)]

    calls.clear()
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")  # validate only ever validates what is not built
    tick.run("ads-launch", url=TEST_DB_URL)
    assert calls == ["biz_q"]


def test_the_sweeper_sends_nothing_when_the_switch_is_off(conn, gads):
    seed(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) SELECT %s, %s, id, 'queued' FROM plan",
        (BIZ, GOOGLE),
    )
    assert tick.run("ads-launch", url=TEST_DB_URL) == {
        "modes": {},
        "checked": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": {},
        "states": {},
    }
    assert gads.requests == []


def test_the_sweeper_stops_at_the_daily_operation_budget(conn, monkeypatch, live):
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "100")
    for biz in ("biz_a", "biz_b", "biz_c"):
        _row_for(conn, biz, "queued")
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES "
        "('ads-sync', now(), true, '{\"ops\": 40}'), ('ads-launch', now(), true, '{\"ops\": 10}'), "
        "('ads-launch', now() - interval '2 days', true, '{\"ops\": 5000}'), "
        "('ads-link-sync', now(), true, '{\"ops\": 5000}'), ('ads-launch', now(), false, '{\"error\": \"x\"}')"
    )
    calls: list[str] = []

    def fake_launch(conn_, settings, biz, channel, *, http=None):
        calls.append(biz)
        return {"state": "live", "ops": 30}

    monkeypatch.setattr(ads_run, "launch", fake_launch)
    out = tick.run("ads-launch", url=TEST_DB_URL)  # 50 sent today; 30 + 30 more reach 110 >= 100: stop there
    assert calls == ["biz_a", "biz_b"] and out["ops"] == 60 and out["stopped"] == "ops budget"
    again = tick.run("ads-launch", url=TEST_DB_URL)  # 110 counted from the ledger now
    assert again == {
        "modes": {GOOGLE: "live"},
        "checked": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": {GOOGLE: 0},
        "states": {},
        "stopped": "ops budget",
    }
    assert calls == ["biz_a", "biz_b"]


def test_the_sweeper_isolates_a_business_that_fails_unexpectedly(conn, monkeypatch, live, caplog):
    for biz in ("biz_a", "biz_b"):
        _row_for(conn, biz, "queued")

    def fake_launch(conn_, settings, biz, channel, *, http=None):
        if biz == "biz_a":
            raise RuntimeError("boom with ya29.SECRET")
        return {"state": "live", "ops": 1}

    monkeypatch.setattr(ads_run, "launch", fake_launch)
    out = tick.run("ads-launch", url=TEST_DB_URL)
    assert (out["errors"], out["checked"], out["states"]) == (1, 1, {"live": 1})
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "biz_a" in logged and "SECRET" not in logged


def test_a_second_step_while_one_runs_is_told_it_is_busy(conn, gads, live):
    seed(conn)
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"ads:{BIZ}",))
        out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"ads:{BIZ}",))
    assert out.get("busy") is True and gads.sent == []


def test_the_ops_budget_setting_is_checked_at_boot(monkeypatch):
    assert config.load().ads_ops_daily_budget == 2300
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "500")
    assert config.load().ads_ops_daily_budget == 500
    for bad in ("0", "-1", "lots"):
        monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", bad)
        with pytest.raises(ValueError):
            config.load()


# ---------------------------------------------------------------- launch_now (P6)


def test_launch_now_within_a_tiny_budget_leaves_the_launch_queued(conn, gads, live, step_clock):
    """The token exchange takes 0.05 s of a 0.01 s budget on a clock that moves only with it (`StepClock`): the
    exchange went out with the budget as its timeout, and the next request is refused -- however slow the machine."""
    seed(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) SELECT %s, %s, id, 'queued' FROM plan",
        (BIZ, GOOGLE),
    )
    gads.delay, gads.sleep = 0.05, step_clock.sleep  # the token exchange alone outlasts the budget
    out = ads_run.launch_now(BIZ, budget_s=0.01)
    say = "Google Search: your ads are waiting to launch. Google did not answer; Admiry retries within the hour."
    assert out == {"channels": {GOOGLE: {"state": "queued", "say": say}}, "say": say}
    assert gads.requests[0].extensions["timeout"]["read"] <= 0.01  # the budget is each request's own timeout
    assert gads.sent == []  # the next request was never sent
    assert row(conn)["last_error"] == google.RETRYING


def test_launch_now_never_raises(conn, monkeypatch, live):
    seed(conn)  # M0: launch_now steps the channels the approved plan funds, read from the database

    def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(ads_run, "launch", broken)
    assert ads_run.launch_now(BIZ)["channels"][GOOGLE]["state"] == "queued"
    monkeypatch.setattr(ads_run.db, "connect", broken)
    assert "retries within the hour" in ads_run.launch_now(BIZ)["say"]


def test_launch_now_with_the_switch_off_opens_nothing(monkeypatch):
    """Every channel's switch off: nothing is opened, so no channel can be named (M0: the funded channels are the
    database's to say) -- only that nothing is launched."""

    def unreachable(*args, **kwargs):
        raise AssertionError("launch_now opened a connection with the switch off")

    monkeypatch.setattr(ads_run.db, "connect", unreachable)
    assert ads_run.launch_now(BIZ) == {"channels": {}, "say": ads_run.OFF}
    assert ads_run.OFF_CHANNEL.format(label="Google Search") == (
        "Google Search: your ads are not launched yet, and nothing is spent on them until they are."
    )  # A3's words, now per channel


# ---------------------------------------------------------------- the web approval (P6)


def test_the_plan_pages_approve_launches_after_its_transaction(client, conn, gads, live, google):
    from agent.tests.test_web import csrf_of, sign_in  # here, not at the top: agent.web first starts an import cycle

    _key, plan_id = seed(conn, status="draft")
    sign_in(client, f"/plan/{plan_id}")
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/plan/{plan_id}"
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("live", plan_id)
    assert gads.kinds() == ["lookup", "create", "enable"]
    web = conn.execute("SELECT result FROM agent_action WHERE tool = 'web.approve_plan'").fetchone()["result"]
    assert web["execution"]["ads"] == "queued"  # the approval's own record; the launch has its own row


def test_the_plan_pages_approve_refuses_a_plan_that_cannot_launch(client, conn, gads, live, google):
    from agent.tests.test_web import csrf_of, sign_in

    _key, plan_id = seed(conn, status="draft", check=None)
    sign_in(client, f"/plan/{plan_id}")
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 409 and "Your page has not been checked yet" in r.text
    assert conn.execute("SELECT status FROM plan").fetchone()["status"] == "draft"
    assert row(conn) is None and gads.requests == []


# ---------------------------------------------------------------- the launch allowlist (2026-10-08)


def test_an_owner_off_the_launch_allowlist_approves_as_if_the_switch_were_off(
    client, conn, gads, live, google, monkeypatch
):
    """AGENT_LAUNCH_ALLOWLIST set and the business's owner not on it: the approval goes through as it does with the
    switch `off` -- no launch check (this page was never checked, which would refuse it), nothing queued, nothing
    sent to Google, the version marked as approved with execution off."""
    from agent.tests.test_web import csrf_of, sign_in

    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    _key, plan_id = seed(conn, status="draft", check=None)
    sign_in(client, f"/plan/{plan_id}")
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT status FROM plan").fetchone()["status"] == "approved"
    assert row(conn) is None and gads.requests == []
    web = conn.execute("SELECT result FROM agent_action WHERE tool = 'web.approve_plan'").fetchone()["result"]
    assert web["execution"]["ads"] == "off"


def test_an_owner_on_the_launch_allowlist_launches_whatever_the_case_written_there(
    client, conn, gads, live, google, monkeypatch
):
    from agent.tests.test_web import csrf_of, sign_in

    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "someone@example.com, Owner@Example.com")
    _key, plan_id = seed(conn, status="draft")  # owner@example.com, the address sign_in signs in with
    sign_in(client, f"/plan/{plan_id}")
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("live", plan_id)
    assert gads.kinds() == ["lookup", "create", "enable"]


def test_launch_and_launch_now_send_nothing_for_a_business_off_the_allowlist(conn, gads, live, monkeypatch):
    """The second guard: a business already approved (here with no row, as an approval under `off` leaves it) is
    never stepped for an owner off the list, by the hourly job's `launch` or the approval's `launch_now`."""
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    seed(conn)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] is None and out["ops"] == 0
    assert ads_run.launch_now(BIZ) == {"channels": {}, "say": ads_run.OFF}
    assert row(conn) is None and gads.requests == []


def test_a_business_with_no_owner_is_off_the_allowlist(conn, gads, live, monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "owner@example.com")
    seed(conn)
    conn.execute("UPDATE business SET owner_email = NULL WHERE id = %s", (BIZ,))
    assert registry.exec_mode_for(conn, config.load(), BIZ, GOOGLE) == "off"
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["ops"] == 0 and gads.requests == []


# ---------------------------------------------------------------- pause and resume


def test_pause_and_resume_a_live_business(conn, gads, live):
    launched(conn, gads)
    out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    assert gads.bodies("pause")[0]["mutateOperations"] == [
        {
            "campaignOperation": {
                "update": {"resourceName": gads.built["campaign"], "status": "PAUSED"},
                "updateMask": "status",
            }
        }
    ]
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"]) == ("paused", "assistant") and r["paused_at"] is not None
    assert out["say"] == "Google Search: paused by you today." and out["ops"] == 1
    assert (
        ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")["state"] == "paused"
    )  # a repeat changes nothing
    assert gads.kinds().count("pause") == 1

    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live" and gads.kinds()[-2:] == ["lookup", "enable"]
    r = row(conn)
    assert r["paused_at"] is None and "paused_by" not in r["resources"]
    note = conn.execute(
        "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()["note"]
    assert note == "turned your ads on Google Search back on"
    with pytest.raises(ToolError) as e:
        ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert str(e.value) == ads_run.NOT_PAUSED


def test_a_business_paused_before_its_launch_stays_off_until_resumed(client, conn, gads, live):
    key, v1 = seed(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'queued')",
        (BIZ, GOOGLE, v1),
    )
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    assert gads.sent == [] and row(conn)["state"] == "paused"
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    envelope = approve(client, conn, key, v2)  # approving a version is not resuming
    assert envelope["data"]["execution"]["ads"] == "paused" and row(conn)["plan_id"] == v2
    assert gads.sent == []
    assert ads_run.resume(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    assert gads.kinds() == ["lookup", "create", "enable"]


def test_pause_without_a_row_is_refused(conn, gads, live):
    seed(conn)
    with pytest.raises(ToolError) as e:
        ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    assert str(e.value) == ads_run.NOT_BUILT.format(label="Google Search")
    assert all(
        plain(s.format(label="Google Search"))
        for s in (ads_run.NOT_BUILT, ads_run.NOT_PAUSED, ads_run.BUSY_NOW, ads_run.SWITCHED_OFF)
    )


def test_a_pause_google_does_not_take_is_recorded_and_owed_to_an_operator(conn, gads, live, caplog):
    """Round 2 B: Google failing never leaves the owner's pause unrecorded."""
    launched(conn, gads)
    gads.fail["pause"] = google_error(503)
    with caplog.at_level(logging.WARNING, logger="agent.ads_run"):
        out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("paused", google.PAUSE_OWED) and r["paused_at"] is not None
    assert out["say"] == f"Google Search: paused by you today. {google.PAUSE_OWED}." and plain(out["say"])
    assert "an operator must act while it stays owed" in caplog.text and "SECRET" not in caplog.text
    assert r["resources"]["pause_owed"] is True  # ruling S11: the flag, not the sentence, is what counts


# ---------------------------------------------------------------- owner words and secrets


def test_every_state_reads_plainly_to_the_owner():
    now = datetime.now(UTC)
    rows = [
        None,
        {"state": "queued", "last_error": None},
        {"state": "queued", "last_error": google.RETRYING},
        {"state": "launching", "last_error": google.RETRYING},
        {"state": "queued", "validated_at": now, "last_error": None},  # A3's `validated` (plan P4)
        {"state": "live", "last_error": None},
        {"state": "live", "last_error": google_ads.ACCESS_TROUBLE},
        {"state": "paused", "last_error": None, "paused_at": now, "resources": {"paused_by": "owner"}},
        {"state": "paused", "last_error": None, "paused_at": now - timedelta(days=3), "resources": {}},
        {"state": "paused", "last_error": None, "paused_at": now, "resources": {"paused_by": "admiry"}},
        {"state": "limited", "last_error": None},
        {
            "state": "failed",
            "last_error": google_ads.REFUSED_WITH_REASON.format("campaignError: DUPLICATE_CAMPAIGN_NAME"),
        },
    ]
    sentences = [ads_run.status_sentence(r, channel=GOOGLE) for r in rows]  # M0: rows without one name the channel
    assert all(plain(s) and s.endswith(".") and ".." not in s for s in sentences), sentences
    live_today = ads_run.status_sentence(
        {"state": "live", "last_error": None}, {"spent_usd": 7.8, "budget_usd": 10}, channel=GOOGLE
    )
    assert live_today == "Google Search: live · $7.80 of $10 today."
    assert sentences[7] == "Google Search: paused by you today."
    assert sentences[8] == f"Google Search: paused by you on {(now - timedelta(days=3)):%A}."
    assert sentences[9] == "Google Search: paused by Admiry today."
    assert plain(ads_run.OFF) and all(plain(s) for s in (google.RETRYING, google.GONE, ads_run.NO_RADIUS))


def test_no_secret_reaches_the_row_the_audit_the_ledger_or_the_log(conn, gads, live, caplog):
    seed(conn)
    gads.fail["lookup"] = google_error(403)
    with caplog.at_level(logging.INFO):
        ads_run.launch(conn, config.load(), BIZ, GOOGLE)
        del gads.fail["lookup"]
        gads.fail["create"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
        tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    assert r["state"] == "failed"
    stored = " ".join(
        [
            str(r),
            str(conn.execute("SELECT args_redacted, result FROM agent_action").fetchall()),
            str(conn.execute("SELECT details FROM job_run").fetchall()),
            " ".join(rec.getMessage() for rec in caplog.records),
        ]
    )
    assert not any(v in stored for v in ADS_SECRETS) and "ya29.SECRET" not in stored and "raw words" not in stored


# ---------------------------------------------------------------- queue (spec §5 step 1)


def test_queue_requeues_what_is_not_built_and_keeps_what_is(conn):
    seed(conn)
    older = conn.execute("SELECT id FROM plan").fetchone()["id"]
    newer = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    built = {
        "campaign": "customers/1234567890/campaigns/8",
        "campaign_budget": "customers/1234567890/campaignBudgets/9",
    }
    cases = [  # (state, resources) before -> (state, plan_id) after an approval of `newer`
        ("queued", {}, "queued", newer),
        ("validated", {}, "queued", newer),
        ("failed", {}, "queued", newer),
        ("paused", {}, "paused", newer),  # paused before launch: approving is not resuming
        ("launching", built, "launching", older),  # built: the row keeps the version it built; launch diffs
        ("live", built, "live", older),
        ("limited", built, "limited", older),
        ("paused", built, "paused", older),
        ("failed", built, "launching", older),  # a failed enable: the new version is applied, then turned on
    ]
    for state, resources, expected_state, expected_plan in cases:
        conn.execute("DELETE FROM channel_campaign")
        conn.execute(  # A3's `validated` is `queued` with validated_at set (plan P4)
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources, last_error, validated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'an old error', CASE WHEN %s THEN now() END)",
            (
                BIZ,
                GOOGLE,
                older,
                "queued" if state == "validated" else state,
                Jsonb({**resources, "refused_plan_id": older}),
                state == "validated",
            ),
        )
        ads_run.queue(conn, BIZ, GOOGLE, newer, ready=True)
        r = row(conn)
        assert (r["state"], r["plan_id"], r["last_error"]) == (expected_state, expected_plan, None), state
        # ruling S11: an approval leaves the bookkeeping alone -- except the mark of the version it supersedes
        # (final re-review): the new version is the "changed version" the status line asked for.
        assert r["resources"] == resources
        if not resources:  # P4: a version Google has not checked yet is plain `queued`, as A3 requeued it
            assert r["validated_at"] is None, state
    conn.execute("DELETE FROM channel_campaign")
    ads_run.queue(conn, BIZ, GOOGLE, newer, ready=True)
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("queued", newer)


def test_a_plan_approved_while_the_switch_was_off_launches_when_asked(conn, gads, live):
    _key, plan_id = seed(conn)  # approved, but no row: the switch was off then
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], out["plan_id"]) == ("live", plan_id)
    audit = conn.execute("SELECT result FROM agent_action WHERE tool = 'job.ads_launch'").fetchone()["result"]
    assert (audit["from"], audit["to"]) == (None, "live")


def test_nothing_is_done_for_a_business_without_an_approved_plan(conn, gads, live):
    seed(conn, status="draft")
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] is None and gads.requests == [] and row(conn) is None
    assert ads_run.update(conn, config.load(), BIZ, GOOGLE)["state"] is None


# ---------------------------------------------------------------- review fix round 1


def test_pause_reaches_google_for_a_launch_whose_enable_answer_was_lost(conn, gads, live):
    """Important 1: the enable reached Google, its answer did not -- the row says launching, Google is serving."""
    seed(conn)
    gads.lose = {"enable"}
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (row(conn)["state"], campaign["status"]) == ("launching", "ENABLED")
    out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    assert gads.bodies("pause")[0]["mutateOperations"] == google._status_op(gads.built["campaign"], "PAUSED")
    assert (out["state"], campaign["status"]) == ("paused", "PAUSED")
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0  # paused stays paused: nothing turns it on


def test_pause_of_ads_never_built_changes_only_the_row(conn, gads, live):
    for state in ("queued", "validated", "failed"):
        conn.execute("DELETE FROM channel_campaign")
        conn.execute("DELETE FROM business")
        _key, plan_id = seed(conn)
        conn.execute(  # A3's `validated` is `queued` with validated_at set (plan P4)
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state, validated_at) "
            "VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END)",
            (BIZ, GOOGLE, plan_id, "queued" if state == "validated" else state, state == "validated"),
        )
        assert ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")["state"] == "paused", state
    assert gads.requests == []


def test_a_page_check_failing_after_launch_never_keeps_a_higher_budget_running(client, conn, gads, live):
    """Ruling S8: the page check is a launch rule; the approval and the update of built ads need only a page
    address on file."""
    key, _v1 = launched(conn, gads)
    _check_failed(conn)
    sent = len(gads.sent)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    envelope = approve(client, conn, key, v2)
    assert [k for k, _ in gads.sent[sent:]] == ["money"]
    budgets = [op for op in gads.bodies("money")[0]["mutateOperations"] if "campaignBudgetOperation" in op]
    assert len(budgets) == 1
    assert budgets[0]["campaignBudgetOperation"]["update"]["amountMicros"] == ads_launch.daily_micros(600)
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("live", v2) and envelope["data"]["execution"]["ads"] == "live"


def test_built_ads_waiting_to_be_turned_on_get_the_new_budget_but_wait_for_the_page(client, conn, gads, live):
    key, _v1 = seed(conn)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["enable"]
    _check_failed(conn)  # the page broke before the ads were ever turned on
    sent = len(gads.sent)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    approve(client, conn, key, v2)
    assert [k for k, _ in gads.sent[sent:]] == ["money"]  # the lower budget reached Google; nothing was turned on
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"]) == ("launching", v2, ads_run.PAGE_FIX.format(fix=PHONE_FIX))
    conn.execute("UPDATE page SET check_json = %s", (Jsonb(CHECK_OK),))
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (campaign["status"], campaign["amount"]) == ("ENABLED", ads_launch.daily_micros(600))


@pytest.mark.parametrize(
    ("change", "sentence"),
    [
        (lambda c: None, None),
        (_check_failed, None),  # the page check is a launch rule
        (_unchecked, None),
        (lambda c: _knowledge(c, phone=None), None),  # the call asset exists already
        (lambda c: _knowledge(c, listing=None, service_area=[]), None),  # a location only when it changes
        (_no_page, ads_run.NO_PAGE),  # new ads in a diff need an address
        (_no_link, google.NOT_LINKED),
        (_prepaid, None),  # A3c-1: Admiry's own account runs a prepaid business's ads; no link is asked for
    ],
)
def test_precheck_for_built_ads_holds_only_the_update_rules(conn, change, sentence):
    _key, plan_id = seed(conn)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) VALUES (%s, %s, %s, 'live', %s)",
        (BIZ, GOOGLE, plan_id, Jsonb({"campaign": f"customers/{CID}/campaigns/8"})),
    )
    change(conn)
    plan = plans.get(conn, BIZ, plan_id)
    if sentence is None:
        ads_run.precheck(conn, config.load(), BIZ, plan)
        return
    with pytest.raises(ToolError) as e:
        ads_run.precheck(conn, config.load(), BIZ, plan)
    assert str(e.value) == sentence


def test_an_unplaceable_area_never_blocks_a_budget_decrease_on_unchanged_geo(client, conn, gads, live):
    key, v1 = seed(conn, knowledge={**KNOWLEDGE, "listing": None, "service_area": ["Austin, TX"]})
    gads.geo = {"Austin, TX": ["geoTargetConstants/1026201"]}
    queued(conn, v1)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    _knowledge(conn, listing=None, service_area=["Nowhere Special"])  # new names Google cannot place
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")  # the plan's own location is unchanged
    approve(client, conn, key, v2)
    assert gads.kinds().count("suggest") == 1  # the launch's only
    assert (row(conn)["plan_id"], row(conn)["last_error"]) == (v2, None)
    assert gads.campaigns[gads.built["campaign"]]["amount"] == ads_launch.daily_micros(600)


def test_a_moved_location_is_resolved_for_the_update(client, conn, gads, live):
    key, v1 = seed(conn, knowledge={**KNOWLEDGE, "listing": None, "service_area": ["Austin, TX"]})
    gads.geo = {"Austin, TX": ["geoTargetConstants/1026201"], "Round Rock, TX": ["geoTargetConstants/1026339"]}
    queued(conn, v1)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    _knowledge(conn, listing=None, service_area=["Austin, TX", "Round Rock, TX"])
    moved = sections()
    moved["consultation"]["business"]["location"] = "Austin, TX, Round Rock, TX"
    approve(client, conn, key, add_plan(conn, moved, status="draft"))
    creates = [
        op["campaignCriterionOperation"]["create"]["location"]["geoTargetConstant"]
        for op in gads.bodies("diff")[0]["mutateOperations"]
        if "create" in op.get("campaignCriterionOperation", {})
        and "location" in op["campaignCriterionOperation"]["create"]
    ]
    assert creates == ["geoTargetConstants/1026201", "geoTargetConstants/1026339"]


def test_a_campaign_adopted_after_a_lost_create_keeps_the_version_that_built_it(client, conn, gads, live):
    """Important 3: v1's create applied with its answer lost; v2 approved before anything found v1's campaign."""
    key, v1 = seed(conn, status="draft")
    gads.lose = {"create"}
    approve(client, conn, key, v1)
    assert row(conn)["state"] == "queued" and row(conn)["resources"]["creating_plan_id"] == v1
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    gads.fail["lookup"] = google_error(503)
    approve(client, conn, key, v2, idem="c2")
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("queued", v2)
    del gads.fail["lookup"]
    sent = len(gads.sent)
    tick.run("ads-launch", url=TEST_DB_URL)
    kinds = [k for k, _ in gads.sent[sent:]]
    assert kinds == ["lookup", "read_ads", "read_keywords", "read_criteria", "read_assets", "money", "lookup", "enable"]
    budget = gads.bodies("money")[0]["mutateOperations"][0]["campaignBudgetOperation"]["update"]
    assert budget["amountMicros"] == ads_launch.daily_micros(600)  # v1 -> v2, applied while still paused
    assert gads.kinds().count("create") == 1 and len(gads.campaigns) == 1
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (campaign["status"], campaign["amount"]) == ("ENABLED", ads_launch.daily_micros(600))
    r = row(conn)
    assert (r["state"], r["plan_id"]) == ("live", v2) and "creating_plan_id" not in r["resources"]


def test_a_validated_row_launches_once_the_switch_is_live(conn, gads, monkeypatch):
    """Ruling S9: the pilot goes validate -> live on the same plan."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "queued"  # A3's `validated` (plan P4) ...
    assert row(conn)["validated_at"] is not None  # ... is `queued` with validated_at set
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0  # validate does not check it again
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}
    assert gads.kinds() == ["validate_create", "lookup", "create", "enable"]
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("live", plan_id)
    note = conn.execute(
        "SELECT result FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()["result"]
    assert (note["from"], note["to"], note["note"]) == ("queued", "live", "launched your ads on Google Search")


def test_a_version_approved_while_the_create_runs_is_applied_before_the_enable(conn, gads, live):
    """Minor 5: the enable reads the approved plan again."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    create = gads._create

    def create_while_the_owner_approves(body):
        answer = create(body)
        with db.connect(TEST_DB_URL) as other:  # the owner's approval of v2, in its own request
            other.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
            other.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (v2,))
            ads_run.queue(other, BIZ, GOOGLE, v2, ready=True)
        return answer

    gads._create = create_while_the_owner_approves
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.kinds() == ["lookup", "create", "money", "lookup", "enable"]
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (campaign["status"], campaign["amount"]) == ("ENABLED", ads_launch.daily_micros(600))
    assert (out["state"], out["plan_id"]) == ("live", v2)


def test_an_area_google_cannot_place_is_asked_once_until_the_names_change(conn, gads, live):
    """Ruling S10: the suggestions are cached by the sorted names, an empty answer included."""
    _key, plan_id = seed(conn, knowledge={**KNOWLEDGE, "listing": None, "service_area": ["Nowhere Special"]})
    queued(conn, plan_id)
    tick.run("ads-launch", url=TEST_DB_URL)
    tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    assert gads.kinds().count("suggest") == 1 and r["last_error"] == ads_run.NO_LOCATION
    assert r["resources"]["geo_cache"] == {"names": ["Nowhere Special"], "targets": []}
    _knowledge(conn, listing=None, service_area=["Round Rock, TX", "Austin, TX"])
    gads.geo = {"Austin, TX": ["geoTargetConstants/1026201"], "Round Rock, TX": ["geoTargetConstants/1026339"]}
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}
    assert gads.kinds().count("suggest") == 3  # the names changed: one request per name
    cache = row(conn)["resources"]["geo_cache"]  # kept through the build
    assert cache == {
        "names": ["Austin, TX", "Round Rock, TX"],
        "targets": ["geoTargetConstants/1026339", "geoTargetConstants/1026201"],
    }


def test_launch_now_never_raises_even_when_the_settings_cannot_load(monkeypatch):
    def broken():
        raise ValueError("AGENT_ADS_EXECUTION must be one of off, validate, live or unset, not 'on'")

    monkeypatch.setattr(ads_run.config, "load", broken)
    out = ads_run.launch_now(BIZ)  # no settings, no database: no channel it could name (M0)
    assert out == {"channels": {}, "say": ads_run.LAUNCH_LATER} and "retries within the hour" in out["say"]


# ---------------------------------------------------------------- review fix round 2


def test_a_validated_row_waiting_on_its_page_says_why(conn, gads, monkeypatch):
    """Round 2 A: under `live` a validated row carries the launch's error; the owner reads it."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    _check_failed(conn)
    tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    # A3's `validated` is `queued` with validated_at set (plan P4)
    assert (r["state"], r["last_error"]) == ("queued", ads_run.PAGE_FIX.format(fix=PHONE_FIX))
    assert r["validated_at"] is not None
    assert ads_run.status_sentence(r) == (
        f"Google Search: your ads are waiting to launch. {ads_run.PAGE_FIX.format(fix=PHONE_FIX)}"
    )


@pytest.mark.parametrize("switch", ["off", "validate"])
def test_a_pause_the_switch_keeps_from_google_is_recorded_and_never_undone_by_the_sweep(
    conn, gads, monkeypatch, switch
):
    """Round 2 B: built ads not turned on yet, paused while Google cannot be told, stay off once the switch is live."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    seed(conn)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["enable"]
    assert row(conn)["state"] == "launching"
    monkeypatch.setenv("AGENT_ADS_EXECUTION", switch)
    sent = len(gads.requests)
    out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    assert len(gads.requests) == sent  # Google was not told ...
    assert (out["state"], out["last_error"]) == ("paused", google.PAUSE_OWED)  # ... and the owner hears who will
    assert out["say"].endswith(f"{google.PAUSE_OWED}.")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    kinds_before = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"paused": 1}
    assert [k for k, _ in gads.sent[kinds_before:]] == ["pause"]  # the owed pause only: nothing turned on
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("paused", None) and "pause_owed" not in r["resources"]


def test_paused_ads_get_a_newer_version_at_resume_and_not_before(client, conn, gads, live):
    key, _v1 = launched(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    sent = len(gads.sent)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    envelope = approve(client, conn, key, v2)
    assert envelope["data"]["execution"]["ads"] == "paused" and len(gads.sent) == sent  # nothing while paused
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert [k for k, _ in gads.sent[sent:]] == ["money", "lookup", "enable"]  # v2 first, then back on
    campaign = gads.campaigns[gads.built["campaign"]]
    assert (out["state"], out["plan_id"]) == ("live", v2)
    assert (campaign["status"], campaign["amount"]) == ("ENABLED", ads_launch.daily_micros(600))


def _confirm(client, conn, key: str, plan_id: str) -> dict:
    prop = proposals.propose_plan(conn, BIZ, plan_id)
    code = proposals.issue_code(conn, prop["proposal_id"], via="ops")
    args = {"proposal_id": prop["proposal_id"], "approval_code": code, "idempotency_key": "c9"}
    return call_tool(client, "confirm_proposal", args, key=key)


def test_a_location_change_for_built_ads_needs_a_place_to_centre_on(client, conn, gads, live):
    """Round 2 C: a new version whose location changed is checked the way `_update` will need it."""
    key, _v1 = launched(conn, gads)
    no_radius = add_plan(conn, sections(monthly_usd=600.0, radius_miles=None), status="draft")
    res = _confirm(client, conn, key, no_radius)
    assert res["isError"] and tool_text(res) == ads_run.NO_RADIUS
    assert conn.execute("SELECT used_at FROM approval_code").fetchone()["used_at"] is None  # the code still works
    conn.execute("DELETE FROM plan WHERE id = %s", (no_radius,))
    _knowledge(conn, listing=None, service_area=[])
    moved = sections(monthly_usd=600.0)
    moved["consultation"]["business"]["location"] = "Somewhere else"
    res = _confirm(client, conn, key, add_plan(conn, moved, status="draft"))
    assert res["isError"] and tool_text(res) == ads_run.NO_LOCATION
    same_place = add_plan(conn, sections(monthly_usd=600.0), status="draft")  # unchanged location: no such check
    approve(client, conn, key, same_place, idem="c3")
    assert (row(conn)["plan_id"], row(conn)["last_error"]) == (same_place, None)


def test_a_refused_create_after_a_double_miss_keeps_the_earlier_attempts_mark(client, conn, gads, live):
    """Round 2 D: v1's create applied (answer lost); v2's create is refused as a duplicate name and the second
    lookup misses too -- v2 built nothing, so the mark stays v1 and v3 later adopts v1's campaign as v1."""
    key, v1 = seed(conn, status="draft")
    gads.lose = {"create"}
    approve(client, conn, key, v1)
    assert row(conn)["resources"]["creating_plan_id"] == v1
    new_theme = ("invisalign palo alto", "clear aligners palo alto")
    v2 = add_plan(conn, sections(themes=new_theme), status="draft")
    gads.fail["lookup"] = httpx.Response(200, json={"results": []})  # both lookups miss
    approve(client, conn, key, v2, idem="c2")
    r = row(conn)
    assert (r["state"], r["resources"]["creating_plan_id"]) == ("failed", v1)
    assert "DUPLICATE_CAMPAIGN_NAME" in r["last_error"]
    del gads.fail["lookup"]
    v3 = add_plan(conn, sections(themes=new_theme), status="draft")
    approve(client, conn, key, v3, idem="c3")
    created = [
        op["adGroupOperation"]["create"]["name"]
        for op in gads.bodies("diff")[0]["mutateOperations"]
        if "create" in op.get("adGroupOperation", {})
    ]
    assert created == ["clear aligners palo alto"]  # v1 -> v3: the added theme is built
    assert len(gads.campaigns) == 1 and (row(conn)["state"], row(conn)["plan_id"]) == ("live", v3)


def test_an_unplaceable_area_costs_no_request_after_the_first_sweep(conn, gads, live):
    """Round 2 E: with no create ever sent and Google's empty answer cached, the create step skips its lookup."""
    _key, plan_id = seed(conn, knowledge={**KNOWLEDGE, "listing": None, "service_area": ["Nowhere Special"]})
    queued(conn, plan_id)
    for _ in range(3):
        tick.run("ads-launch", url=TEST_DB_URL)
    assert gads.kinds() == ["lookup", "suggest"]
    assert row(conn)["last_error"] == ads_run.NO_LOCATION
    ops = [r["details"]["ops"] for r in conn.execute("SELECT details FROM job_run ORDER BY id")]
    assert ops == [2, 0, 0]


def test_a_refused_create_restores_the_mark_even_when_the_second_lookup_fails(client, conn, gads, live):
    key, v1 = seed(conn, status="draft")
    gads.lose = {"create"}
    approve(client, conn, key, v1)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    lookups = iter([httpx.Response(200, json={"results": []}), google_error(503)])  # a miss, then Google fails
    original = gads.handler

    def missing_then_down(request):
        body = json.loads(request.content) if request.content and str(request.url) != google_auth.TOKEN_URL else {}
        if "FROM campaign WHERE" in body.get("query", ""):
            gads.sent.append(("lookup", body))
            return next(lookups)
        return original(request)

    with REAL_CLIENT(transport=httpx.MockTransport(missing_then_down)) as http:
        conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
        conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (v2,))
        ads_run.queue(conn, BIZ, GOOGLE, v2, ready=True)
        out = ads_run.launch(conn, config.load(), BIZ, GOOGLE, http=http)
    assert gads.kinds()[-3:] == ["lookup", "create", "lookup"]  # v2's create refused as a duplicate name
    assert (out["state"], out["last_error"]) == ("queued", google.RETRYING)  # the lookup's failure is retryable
    assert row(conn)["resources"]["creating_plan_id"] == v1  # v2 built nothing


# ---------------------------------------------------------------- review fix round 3: ruling S11


def _owed(client, conn, gads) -> tuple[str, str]:
    """Live ads whose pause Google did not take (it keeps failing): paused, the flag set. Returns (key, v1)."""
    key, v1 = launched(conn, gads)
    gads.fail["pause"] = google_error(503)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    assert row(conn)["resources"]["pause_owed"] is True
    return key, v1


def test_approving_a_version_while_a_pause_is_owed_keeps_the_flag_and_the_sentence(client, conn, gads, live):
    key, _v1 = _owed(client, conn, gads)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    envelope = approve(client, conn, key, v2)  # its launch step retries the pause; Google still fails it
    r = row(conn)
    assert (r["state"], r["resources"]["pause_owed"]) == ("paused", True)
    assert r["last_error"] == google.RETRYING  # whatever last_error holds, the owner reads the flag's sentence
    assert envelope["data"]["execution"]["ads"] == "paused"
    assert envelope["say"] == f"Confirmed. Google Search: paused by you today. {google.PAUSE_OWED}."
    assert ads_run.status_sentence(r) == f"Google Search: paused by you today. {google.PAUSE_OWED}."


def test_the_sweeper_sends_one_paused_status_for_an_owed_pause_and_clears_the_flag(client, conn, gads, live):
    _owed(client, conn, gads)
    campaign = gads.campaigns[gads.built["campaign"]]
    assert campaign["status"] == "ENABLED"  # Google still runs the ads
    del gads.fail["pause"]
    sent = len(gads.sent)
    details = tick.run("ads-launch", url=TEST_DB_URL)
    assert [k for k, _ in gads.sent[sent:]] == ["pause"] and details["ops"] == 1
    assert gads.bodies("pause")[-1]["mutateOperations"] == google._status_op(gads.built["campaign"], "PAUSED")
    r = row(conn)
    assert (r["state"], r["last_error"], campaign["status"]) == ("paused", None, "PAUSED")
    assert "pause_owed" not in r["resources"] and r["resources"]["paused_by"] == "owner"
    note = conn.execute(
        "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    ).fetchone()["note"]
    assert note == "finished pausing your ads in Google Ads"
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0  # done: nothing more to send


def test_an_owed_pause_is_sent_even_past_the_days_operation_budget(client, conn, gads, live, monkeypatch):
    _owed(client, conn, gads)
    del gads.fail["pause"]
    monkeypatch.setenv("ADS_OPS_DAILY_BUDGET", "10")
    conn.execute("INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-sync', now(), true, '{\"ops\": 50}')")
    details = tick.run("ads-launch", url=TEST_DB_URL)
    assert details["stopped"] == "ops budget" and details["states"] == {"paused": 1}
    assert "pause_owed" not in row(conn)["resources"]


def test_an_owed_pause_without_a_stored_campaign_looks_it_up_first(conn, gads, live):
    _key, plan_id = seed(conn)
    queued(conn, plan_id)
    gads.lose = {"create"}  # a campaign exists in Google, the row stored none
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    conn.execute(
        "UPDATE channel_campaign SET state = 'paused', paused_at = now(), resources = resources || %s",
        (Jsonb({"pause_owed": True, "paused_by": "owner"}),),
    )
    sent = len(gads.sent)
    tick.run("ads-launch", url=TEST_DB_URL)
    assert [k for k, _ in gads.sent[sent:]] == ["lookup", "pause"]
    assert gads.bodies("pause")[0]["mutateOperations"] == google._status_op(gads.built["campaign"], "PAUSED")
    assert "pause_owed" not in row(conn)["resources"]


def test_a_pause_google_took_never_sets_the_flag(conn, gads, live):
    launched(conn, gads)
    out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    r = row(conn)
    assert "pause_owed" not in r["resources"] and r["last_error"] is None
    assert out["say"] == "Google Search: paused by you today."


def test_resume_clears_an_owed_pause(client, conn, gads, live):
    _owed(client, conn, gads)
    del gads.fail["pause"]
    sent = len(gads.sent)
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert out["state"] == "live" and "pause_owed" not in r["resources"] and r["last_error"] is None
    # the pause never reached Google, so the campaign is still on: the enable step finds it ENABLED and sends nothing
    assert [k for k, _ in gads.sent[sent:]] == ["lookup"]


def test_an_owed_pause_google_refuses_stays_paused_and_owed(client, conn, gads, live):
    """A refusal of the PAUSED status must never fail a paused row (a failed built row is requeued as launching
    by the next approval): it stays paused, the flag stays, and the owner still reads the operator sentence."""
    _owed(client, conn, gads)
    gads.fail["pause"] = google_error(400, {"mutateError": "RESOURCE_NOT_FOUND"})
    tick.run("ads-launch", url=TEST_DB_URL)
    r = row(conn)
    assert (r["state"], r["resources"]["pause_owed"]) == ("paused", True)
    assert r["last_error"] == google_ads.REFUSED_WITH_REASON.format("mutateError: RESOURCE_NOT_FOUND")
    assert ads_run.status_sentence(r).endswith(f"{google.PAUSE_OWED}.")


# ---------------------------------------------------------------- A3 final review


def test_validate_then_live_creates_the_conversion_actions_before_the_launch(conn, gads, monkeypatch):
    """I1: `validate` stores only the account's tag, so the first live step runs the conversion setup for real --
    all seven actions exist, and the call asset reports calls on its own action, not the account's."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, plan_id = seed(conn, conversions=False)
    queued(conn, plan_id)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "queued"  # A3's `validated` (plan P4)
    assert row(conn)["validated_at"] is not None
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM conversion_action WHERE business_id = %s", (BIZ,))}
    assert kinds == {"tag"}  # validate built nothing to read labels from
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    sent = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}
    assert [k for k, _ in gads.sent[sent:]] == [
        "lookup",
        "customer",
        "actions",
        "conversions",
        "actions",
        "create",
        "enable",
    ]
    rows = {r["kind"]: r["resource_name"] for r in conn.execute("SELECT kind, resource_name FROM conversion_action")}
    assert set(rows) == {"tag", *ads_conversions.KINDS}
    asset = next(
        op["assetOperation"]["create"] for op in gads.bodies("create")[0]["mutateOperations"] if "assetOperation" in op
    )
    assert asset["callAsset"]["callConversionReportingState"] == "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
    assert asset["callAsset"]["callConversionAction"] == rows["call_ad"] == gads.actions["Admiry · Call from ad"]


def test_a_theme_that_comes_back_turns_its_paused_group_back_on(client, conn, gads, live):
    """S16 (I3): Google refuses a second ad group of the same name in a campaign -- a theme removed and approved
    again re-enables its paused group, adds what its keywords miss, and gets the approved ad text."""
    both = ("invisalign palo alto", "clear aligners palo alto")
    key, v1 = seed(conn, secs=sections(themes=both), status="draft")
    approve(client, conn, key, v1)
    group = gads.built["ad_groups"]["clear aligners palo alto"]
    old_ad = gads.built["ads"]["clear aligners palo alto"]
    approve(client, conn, key, add_plan(conn, sections(themes=both[:1]), status="draft"), idem="c2")
    assert gads.groups[group]["status"] == "PAUSED"
    sent = len(gads.sent)
    v3 = add_plan(conn, sections(themes=both), status="draft")
    approve(client, conn, key, v3, idem="c3")
    assert [k for k, _ in gads.sent[sent:]] == ["money", "diff"]
    ops = gads.bodies("diff")[-1]["mutateOperations"]
    assert not any("create" in op.get("adGroupOperation", {}) for op in ops)  # no second group of that name
    assert {"update": {"resourceName": group, "status": "ENABLED"}, "updateMask": "status"} in [
        op["adGroupOperation"] for op in ops if "adGroupOperation" in op
    ]
    ads = [op["adGroupAdOperation"] for op in ops if "adGroupAdOperation" in op]
    assert [a["create"]["adGroup"] for a in ads if "create" in a] == [group]  # the approved text, in that group
    assert {"update": {"resourceName": old_ad, "status": "PAUSED"}, "updateMask": "status"} in ads
    assert gads.groups[group]["status"] == "ENABLED" and len(gads.groups) == 2
    r = row(conn)
    assert (r["plan_id"], r["last_error"]) == (v3, None) and r["resources"]["ads"]["clear aligners palo alto"] != old_ad


def test_a_structural_refusal_after_a_budget_cut_leaves_the_new_budget_running(client, conn, gads, live):
    """S16: the money is its own mutate, first -- Google refusing the rest of a change never holds back its budget
    cut. The row keeps the refusal (and the version the ads were built from), and the owner reads it (S15)."""
    key, v1 = launched(conn, gads)
    campaign = gads.built["campaign"]
    gads.groups["customers/1234567890/adGroups/999"] = {  # made by hand in Google Ads, named like the new theme
        "campaign": campaign,
        "name": "clear aligners palo alto",
        "status": "ENABLED",
    }
    v2 = add_plan(
        conn, sections(monthly_usd=600.0, themes=("invisalign palo alto", "clear aligners palo alto")), status="draft"
    )
    envelope = approve(client, conn, key, v2)
    assert gads.kinds()[-2:] == ["money", "diff"]
    assert gads.campaigns[campaign]["amount"] == ads_launch.daily_micros(600)  # the budget cut is in
    r = row(conn)
    refusal = google_ads.REFUSED_WITH_REASON.format("adGroupError: DUPLICATE_ADGROUP_NAME")
    assert (r["state"], r["plan_id"], r["last_error"], r["resources"]["refused_plan_id"]) == ("live", v1, refusal, v2)
    assert envelope["say"].endswith(f"{google.REFUSED_CHANGE}. {refusal}.") and plain(envelope["say"])
    sent = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0 and len(gads.sent) == sent  # not retried


def test_resume_after_a_refused_change_turns_the_ads_back_on_as_they_were_built(client, conn, gads, live):
    """S15/S16: the version Google refused is not sent again when the ads come back on (it would be refused again
    and fail the row): they run as built, with the refused version's budget that Google already took."""
    key, _v1 = launched(conn, gads)
    campaign = gads.built["campaign"]
    gads.groups["customers/1234567890/adGroups/999"] = {
        "campaign": campaign,
        "name": "clear aligners palo alto",
        "status": "ENABLED",
    }
    v2 = add_plan(
        conn, sections(monthly_usd=600.0, themes=("invisalign palo alto", "clear aligners palo alto")), status="draft"
    )
    approve(client, conn, key, v2)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    sent = len(gads.sent)
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert [k for k, _ in gads.sent[sent:]] == ["lookup", "enable"]  # no structural mutate, no second refusal
    assert out["state"] == "live" and gads.campaigns[campaign]["status"] == "ENABLED"
    assert gads.campaigns[campaign]["amount"] == ads_launch.daily_micros(600)
    assert row(conn)["resources"]["refused_plan_id"] == v2 and google.REFUSED_CHANGE in out["say"]


def test_a_pause_while_a_step_holds_the_lock_is_recorded_and_the_step_never_enables(conn, gads, live):
    """S17: `pause` never waits for (or fails on) the lock: it records the pause at once, owed to Google; the step
    that held the lock keeps it paused and never turns the ads on; the next sweep tells Google."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    create = gads._create
    paused: list[dict] = []

    def create_while_the_owner_pauses(body):
        answer = create(body)
        with db.connect(TEST_DB_URL) as other:  # the owner's pause, in its own request, while the step runs
            paused.append(ads_run.pause(other, config.load(), BIZ, GOOGLE, by="owner"))
        return answer

    gads._create = create_while_the_owner_pauses
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert paused[0]["state"] == "paused" and paused[0]["ops"] == 0  # recorded, not refused as busy
    assert gads.kinds() == ["lookup", "create"]  # never an enable
    r = row(conn)
    assert (out["state"], r["state"], r["resources"]["pause_owed"], r["resources"]["paused_by"]) == (
        "paused",
        "paused",
        True,
        "owner",
    )
    assert r["resources"]["campaign"] == gads.built["campaign"]  # the build is kept, under the pause
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"
    sent = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"paused": 1}
    assert [k for k, _ in gads.sent[sent:]] == ["pause"]  # the sweep's repause (S11): Google is told
    assert "pause_owed" not in row(conn)["resources"] and "enable" not in gads.kinds()


def test_a_pause_while_another_step_holds_the_lock_is_never_refused(conn, gads, live):
    """S17: a pause is never BUSY_NOW -- only `resume` waits its turn."""
    launched(conn, gads)
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"ads:{BIZ}",))
        out = ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
        with pytest.raises(ToolError) as e:
            ads_run.resume(conn, config.load(), BIZ, GOOGLE)
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"ads:{BIZ}",))
    assert str(e.value) == ads_run.BUSY_NOW
    assert (out["state"], out["last_error"], out["ops"]) == ("paused", google.PAUSE_OWED, 0)
    assert "pause" not in gads.kinds()  # Google is told by the next sweep
    tick.run("ads-launch", url=TEST_DB_URL)
    assert gads.kinds().count("pause") == 1 and gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED"


def test_every_search_body_is_the_query_alone(conn, gads, live):
    """C1: v25 refuses `pageSize` -- the fake refuses it too, so no search here may carry one."""
    launched(conn, gads)
    ads_run.sync(conn, config.load(), BIZ, GOOGLE)
    searches = [body for kind, body in gads.sent if kind not in ("page_size",) and "query" in body]
    assert searches and all(set(body) == {"query"} for body in searches)
    assert "page_size" not in gads.kinds()


@pytest.mark.parametrize("answer_lost", [False, True])
def test_a_pause_while_the_enable_step_reads_google_keeps_the_ads_off(conn, gads, live, answer_lost):
    """S17: the enable step reads the row again right before it turns anything on, and never writes `live` over a
    pause -- here the owner pauses while the step looks the campaign up. With the earlier enable's answer lost,
    Google already serves (nothing left to send): the row still stays paused, and the sweep tells Google."""
    seed(conn)
    if answer_lost:
        gads.lose = {"enable"}
    else:
        gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    gads.fail.pop("enable", None)
    assert row(conn)["state"] == "launching"
    lookup = gads._lookup

    def lookup_while_the_owner_pauses(body):
        with db.connect(TEST_DB_URL) as other:
            ads_run.pause(other, config.load(), BIZ, GOOGLE, by="owner")
        return lookup(body)

    gads._lookup = lookup_while_the_owner_pauses
    enables = gads.kinds().count("enable")
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    gads._lookup = lookup
    assert gads.kinds().count("enable") == enables  # nothing turned on
    r = row(conn)
    assert (out["state"], r["state"], r["resources"]["pause_owed"], r["launched_at"]) == (
        "paused",
        "paused",
        True,
        None,
    )
    tick.run("ads-launch", url=TEST_DB_URL)
    assert gads.campaigns[gads.built["campaign"]]["status"] == "PAUSED" and "pause_owed" not in row(conn)["resources"]


def test_a_pause_while_a_newer_version_is_applied_mid_launch_sends_nothing_more(conn, gads, live):
    """S17: a pause that lands while the enable step applies a newer version (its money first) ends the step there
    -- no lookup, no enable, no page check writing its sentence over the pause."""
    seed(conn)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["enable"]
    _check_failed(conn)  # were the step to go on, the page check would put its own sentence on the row
    money = gads._money

    def money_while_the_owner_pauses(body):
        with db.connect(TEST_DB_URL) as other:
            ads_run.pause(other, config.load(), BIZ, GOOGLE, by="owner")
        return money(body)

    gads._money = money_while_the_owner_pauses
    conn.execute("UPDATE plan SET status = 'superseded'")
    v2 = add_plan(conn, sections(monthly_usd=600.0))  # approved while the ads are built, not yet on
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert [k for k, _ in gads.sent[sent:]] == ["money"]
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"], r["resources"]["pause_owed"]) == (
        "paused",
        v2,
        google.PAUSE_OWED,
        True,
    )


def test_a_new_approval_drops_the_mark_of_the_version_google_refused(conn, gads, live):
    """Final re-review: a refused version's mark (`refused_plan_id`, ruling S15) used to outlive the next approval,
    so the status line kept asking for a changed version after the owner had approved one."""
    launched(conn, gads)
    refused = add_plan(conn, sections(monthly_usd=400.0), status="approved")
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || %s WHERE business_id = %s",
        (Jsonb({"refused_plan_id": refused}), BIZ),
    )
    assert google.REFUSED_CHANGE in ads_run.status_sentence(ads_run.get(conn, BIZ, GOOGLE))
    newer = add_plan(conn, sections(monthly_usd=500.0), status="approved")
    ads_run.queue(conn, BIZ, GOOGLE, newer, ready=True)
    row = ads_run.get(conn, BIZ, GOOGLE)
    assert "refused_plan_id" not in row["resources"]
    assert google.REFUSED_CHANGE not in ads_run.status_sentence(row)
    assert row["resources"]["campaign"] == gads.built["campaign"]  # the rest of the bookkeeping survives


def test_a_pause_that_lands_while_a_resume_is_under_way_wins(conn, gads, live, monkeypatch):
    """Final re-review (ruling S17): a lock-free pause could slip in between resume's read and its write; the
    resume then turned the ads on. The resume's write is conditioned on the pause it read, and a pause of a row
    already paused moves `paused_at`, so the later pause is the one that stands."""
    launched(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")
    before = len(gads.kinds())
    original, raced = ads_run.get, []

    def get_then_pause_again(c, biz, channel):
        row = original(c, biz, channel)
        if not raced:  # the resume's own read: the owner's second pause lands right after it
            raced.append(row["paused_at"])
            conn.execute(
                "UPDATE channel_campaign SET paused_at = now() + interval '1 second' WHERE business_id = %s", (biz,)
            )
        return row

    monkeypatch.setattr(ads_run, "get", get_then_pause_again)
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    row = original(conn, BIZ, GOOGLE)
    assert out["state"] == "paused" and row["state"] == "paused" and row["paused_at"] > raced[0]
    assert "enable" not in gads.kinds()[before:]  # Google was never told to turn them on


# ---------------------------------------------------------------- M0 Task 3: the row per business and channel


def test_rows_lists_every_channel_row_of_the_business_in_card_order(conn):
    _key, plan_id = seed(conn)
    _row_for(conn, "biz_2", "live")  # another business's row is never one of this business's
    for channel in ("reddit", "newsbreak", GOOGLE):  # written out of order
        conn.execute(
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'queued')",
            (BIZ, channel, plan_id),
        )
    assert [r["channel"] for r in ads_run.rows(conn, BIZ)] == [GOOGLE, "newsbreak", "reddit"]  # registry.ORDER
    assert ads_run.get(conn, BIZ, "newsbreak")["channel"] == "newsbreak"
    assert ads_run.get(conn, BIZ, "meta") is None and ads_run.rows(conn, "biz_none") == []


def test_queue_waits_for_an_account_that_is_not_ready(conn):
    """Spec §5.3 step 1: `waiting_account` when the channel's account is not ready; a later approval with the account
    ready queues it. Built ads keep their state either way (the precheck gates every step that could change them)."""
    _key, v1 = seed(conn)
    ads_run.queue(conn, BIZ, GOOGLE, v1, ready=False)
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("waiting_account", v1)
    v2 = add_plan(conn, sections(monthly_usd=600.0), status="draft")
    ads_run.queue(conn, BIZ, GOOGLE, v2, ready=True)
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("queued", v2)
    conn.execute("UPDATE channel_campaign SET state = 'live', resources = %s", (Jsonb({"campaign": "c/1"}),))
    ads_run.queue(conn, BIZ, GOOGLE, v1, ready=False)
    assert (row(conn)["state"], row(conn)["plan_id"]) == ("live", v2)
    assert plain(ads_run.status_sentence({"channel": GOOGLE, "state": "waiting_account", "last_error": None}))


def test_a_waiting_row_never_launches_until_its_account_is_ready(conn, gads, live):
    """M0 Task 4 (spec §5.3 step 1, D77): ads-launch re-checks a waiting row every hour -- at no platform cost -- and
    launches it the hour its account is ready (here: the owner accepted the invitation)."""
    _key, plan_id = seed(conn, link="pending")
    ads_run.queue(conn, BIZ, GOOGLE, plan_id, ready=False, reason=google.LINK_PENDING)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], out["last_error"]) == ("waiting_account", google.LINK_PENDING) and gads.requests == []
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"waiting_account": 1} and gads.requests == []
    with conn.transaction():
        google_ads_link.apply(conn, BIZ, CID, google_ads_link.Remote(status="active", resource_name=None))
    assert tick.run("ads-launch", url=TEST_DB_URL)["states"] == {"live": 1}
    assert gads.kinds() == ["lookup", "create", "enable"] and row(conn)["last_error"] is None


def test_a_pause_records_the_owners_mark_beside_who_paused_and_a_resume_clears_it(conn, gads, live):
    """M0 Task 4 fix round 1: the owner's pause is its own mark (`resources.owner_paused`); `paused_reason` is only
    ever Admiry's."""
    launched(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="assistant")  # the assistant pauses for the owner
    r = row(conn)
    assert (r["state"], r["paused_reason"], r["resources"]["paused_by"]) == ("paused", None, "assistant")
    assert r["resources"]["owner_paused"] is True
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")  # a repeat re-records the pause, mark and all
    assert (row(conn)["resources"]["owner_paused"], row(conn)["resources"]["paused_by"]) == (True, "owner")
    assert ads_run.resume(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    assert row(conn)["paused_reason"] is None and "owner_paused" not in row(conn)["resources"]


def test_due_is_the_step_rows_of_its_own_channel(conn, live):
    _row_for(conn, "biz_q", "queued")
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) SELECT 'biz_q', 'newsbreak', id, 'queued' "
        "FROM plan WHERE business_id = 'biz_q'"
    )

    def due(channel: str) -> list[tuple[str, str]]:
        where = ads_run.due(config.load(), channel)
        rows = conn.execute(f"SELECT business_id, channel FROM channel_campaign c WHERE {where}").fetchall()
        return [(r["business_id"], r["channel"]) for r in rows]

    assert due(GOOGLE) == [("biz_q", GOOGLE)] and due("newsbreak") == [("biz_q", "newsbreak")]
    with pytest.raises(ValueError, match="nowhere"):  # the id goes into the SQL text: only the registry's own
        ads_run.due(config.load(), "nowhere'; --")


def test_a_version_approved_after_a_check_is_checked_by_google_again(conn, gads, monkeypatch):
    """Plan P4 keeps A3's semantics: a `queued` row with validated_at set is A3's `validated`, so a new approved
    version of a plan never built (or a resume of one) is plain `queued` again -- `validate` checks it with Google,
    and the audit says so each time."""
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    _key, v1 = seed(conn)
    queued(conn, v1)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
    v2 = add_plan(conn, sections(monthly_usd=600.0))
    queued(conn, v2)
    assert (row(conn)["state"], row(conn)["validated_at"]) == ("queued", None)
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 1  # due again under `validate`
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    ads_run.resume(conn, config.load(), BIZ, GOOGLE)  # nothing built: back to `queued`, and checked again
    assert gads.kinds() == ["validate_create"] * 3
    r = row(conn)
    assert (r["state"], r["plan_id"], r["validated_at"] is not None) == ("queued", v2, True)
    notes = conn.execute(
        "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id"
    ).fetchall()
    assert [n["note"] for n in notes] == ["had Google check your ads (a test run: nothing is shown or spent)"] * 3


def test_status_sentence_names_the_rows_own_channel():
    assert ads_run.status_sentence({"channel": "newsbreak", "state": "live", "last_error": None}) == "NewsBreak: live."
    assert ads_run.status_sentence({"channel": GOOGLE, "state": "live", "last_error": None}) == "Google Search: live."
    for state in ("waiting_account", "waiting_creative", "operator"):  # the spec's other states read plainly too
        sentence = ads_run.status_sentence({"channel": "newsbreak", "state": state, "last_error": None})
        assert sentence.startswith("NewsBreak: ") and "launched" not in sentence and plain(sentence), state


def test_url_stale_set_by_a_publish_mid_update_survives_the_updates_own_write(conn, gads, live):
    """A3p-2 Task 3 review, H22 minor 5: `_update` reads `resources.url_stale` at its start (as part of the row
    it reads) and only ever clears it, in its own final write, when the page's URL still matches what it just
    diffed against. A second publish that commits its own `url_stale = true` (a newer address again) while this
    update is still out sending Google its mutates (the money first) must not have that flag silently dropped
    by this write -- `_KEEP` preserves it from the row's own current value, and the write's own CASE re-checks
    the page's URL server-side rather than trusting this call's now-stale in-memory snapshot."""
    _key, v1 = launched(conn, gads)
    v2 = add_plan(conn, sections(monthly_usd=1500.0))
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
    page_id = landing.current(conn, BIZ)["id"]
    newer_url = "https://newly-published.business.test/"

    def publish_commits_now() -> None:
        with db.connect(TEST_DB_URL) as other:
            other.execute("UPDATE page SET url = %s WHERE id = %s", (newer_url, page_id))
            other.execute(
                "UPDATE channel_campaign SET resources = resources || '{\"url_stale\": true}' WHERE business_id = %s "
                "AND channel = 'google_search'",
                (BIZ,),
            )

    real_money = gads._money

    def money_then_publish(body):
        out = real_money(body)
        publish_commits_now()
        return out

    gads._money = money_then_publish
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    r = row(conn)
    assert r["plan_id"] == v2  # the update itself still went through
    assert r["resources"]["url_stale"] is True  # not silently dropped by this update's own final write
    assert r["resources"].get("final_url") != newer_url + "?src=gads"  # this update never saw the new address

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)  # the next step picks the still-set flag up and moves the ad
    r = row(conn)
    assert "url_stale" not in r["resources"] and r["resources"]["final_url"] == newer_url + "?src=gads"
    assert "diff" in [k for k, _ in gads.sent[sent:]]


def test_a_launching_row_applies_a_pending_url_move_before_it_is_enabled(conn, gads, live):
    """A3p-2 Task 3 review, H22 minor 4: a row still `launching` (built, not yet turned on) that also owes a
    URL move (`resources.url_stale`, set by a publish that committed while the enable step had not run yet)
    gets that move applied -- via `_update`, exactly like a newer plan would -- before anything is turned on,
    so the ad Google enables is never one still carrying the old address."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    gads.fail["enable"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["enable"]
    assert row(conn)["state"] == "launching"
    old_ad_ref = gads.built["ads"]["invisalign palo alto"]
    new_url = "https://republished.business.test/"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || '{\"url_stale\": true}' WHERE business_id = %s "
        "AND channel = 'google_search'",
        (BIZ,),
    )

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    r = row(conn)
    assert r["state"] == "live" and "url_stale" not in r["resources"]
    new_full_url = new_url + "?src=gads"
    assert r["resources"]["final_url"] == new_full_url
    diff_bodies = [b for k, b in gads.sent[sent:] if k == "diff"]
    assert diff_bodies, f"expected a diff mutate; sent {[k for k, _ in gads.sent[sent:]]}"
    creates = [
        op["adGroupAdOperation"]["create"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("create")
    ]
    assert any(c["ad"]["finalUrls"] == [new_full_url] for c in creates)
    updates = [
        op["adGroupAdOperation"]["update"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("update")
    ]
    assert {"resourceName": old_ad_ref, "status": "PAUSED"} in updates
    assert gads.campaigns[gads.built["campaign"]]["status"] == "ENABLED"


def test_a_page_move_during_the_create_call_still_moves_the_ad_before_it_is_enabled(conn, gads, live):
    """Minor 4 (round 2, R5): a publish can commit while `_create`'s own mutate is still out -- before
    `apply_publish_page` ever finds anything built (`ads_run.get` still reads no `campaign` in `resources`), so
    it reports "not built" and sets no `url_stale` at all; this row's own flag stays unset for the whole step.
    `_enable` must not rely on the flag alone: it also compares `resources.final_url` (what the ads it just
    built actually carry, H9) against a freshly read `_page_url`, and that mismatch alone is enough to apply
    the move -- before anything is turned on -- exactly like the flag would."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    new_url = "https://republished.business.test/"
    real_create = gads._create

    def create_then_a_publish_commits(body):
        out = real_create(body)
        with db.connect(TEST_DB_URL) as other:  # a publish, in its own request, while the create is still out
            other.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
        return out

    gads._create = create_then_a_publish_commits
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    r = row(conn)
    assert r["state"] == "live"
    assert "url_stale" not in r["resources"]  # the flag genuinely was never set for this race
    new_full_url = new_url + "?src=gads"
    assert r["resources"]["final_url"] == new_full_url
    diff_bodies = gads.bodies("diff")
    assert diff_bodies, f"expected a diff mutate moving the ad; sent kinds were {gads.kinds()}"
    creates = [
        op["adGroupAdOperation"]["create"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("create")
    ]
    assert any(c["ad"]["finalUrls"] == [new_full_url] for c in creates)
    assert gads.campaigns[gads.built["campaign"]]["status"] == "ENABLED"


def test_a_refused_version_gets_a_url_only_refresh_never_a_resend(client, conn, gads, live):
    """H26/S15 (round 2, R6): a version Google refused (`resources.refused_plan_id`) must never be sent again
    (ruling S15) -- but a page move that lands afterward still needs to reach the ad that is actually running.
    `_refresh_url` diffs the row's own running (unrefused) plan against itself with the new address: one
    URL-only mutate, the refused headline never appears again, `plan_id` stays at the version that is really
    live, and `refused_plan_id` survives untouched -- so a later sweep sends nothing more, and a plain owner
    resume afterward turns the ads back on instead of failing the row (the bug this fixes: routing the refused
    plan itself through `_update` for the move refused it all over again, every sweep, and failed a resume that
    landed in the same state)."""
    key, v1 = launched(conn, gads)
    v2 = add_plan(conn, sections(headlines=["Refused Headline", *HEADLINES[1:]]), status="draft")
    gads.fail["diff"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    approve(client, conn, key, v2)
    del gads.fail["diff"]
    r = row(conn)
    assert (r["state"], r["plan_id"], r["resources"]["refused_plan_id"]) == ("live", v1, v2)

    new_url = "https://republished.business.test/"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || '{\"url_stale\": true}' WHERE business_id = %s "
        "AND channel = 'google_search'",
        (BIZ,),
    )

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    assert [k for k, _ in gads.sent[sent:]] == ["diff"]  # one URL-only mutate: no money, no repeat of the refusal
    assert "Refused Headline" not in json.dumps(gads.bodies("diff")[-1])
    r = row(conn)
    assert r["state"] == "live" and r["plan_id"] == v1  # the running version never advanced past what worked
    assert r["resources"]["refused_plan_id"] == v2  # still true: v2 itself was never re-sent
    assert "url_stale" not in r["resources"]
    assert r["resources"]["final_url"] == new_url + "?src=gads"

    sent = len(gads.sent)  # a second sweep sends nothing more: url_stale is gone, v2 is still refused
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.sent[sent:] == []

    assert ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")["state"] == "paused"
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live"  # R6: an owner's resume no longer fails the row
    assert row(conn)["resources"]["refused_plan_id"] == v2


def test_a_refused_url_refresh_is_not_re_sent_and_a_resume_still_enables(client, conn, gads, live):
    """H27 (round 3, S5): the URL-only mutate `_refresh_url` sends for a row whose plan is already refused can
    itself be refused by Google -- a different reason (the new address itself, say) than whatever refused the
    plan. `_refuse_url` records `resources.refused_url` and `last_error`, clears `url_stale`, and leaves
    `refused_plan_id` exactly as it was; the next sweep sends nothing more (`_refresh_url`'s own leading check,
    no Google call spent proving the same refusal again); and a plain owner resume on this row still turns the
    ads on, at whatever address is still actually on them, with `last_error` explaining why the move did not
    happen -- never failing the row over it (the bug: `_enable`'s own nested call used to raise past its own
    dispatch context and land in `_advance`'s generic `_fail`)."""
    key, v1 = launched(conn, gads)
    v2 = add_plan(conn, sections(headlines=["Refused Headline", *HEADLINES[1:]]), status="draft")
    gads.fail["diff"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    approve(client, conn, key, v2)
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_plan_id"] == v2

    new_url = "https://republished.business.test/"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || '{\"url_stale\": true}' WHERE business_id = %s "
        "AND channel = 'google_search'",
        (BIZ,),
    )
    gads.fail["diff"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})  # this address, refused too

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["diff"]

    assert [k for k, _ in gads.sent[sent:]] == ["diff"]
    r = row(conn)
    assert r["state"] == "live" and r["plan_id"] == v1
    assert r["resources"]["refused_plan_id"] == v2  # untouched: the plan was never re-tried, let alone refused
    assert r["resources"]["refused_url"] == new_url  # H27: the address, not the plan, is what is marked
    assert "url_stale" not in r["resources"]
    assert r["last_error"]

    sent = len(gads.sent)  # nothing left to try: the current address is still the one already refused
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.sent[sent:] == []

    assert ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")["state"] == "paused"
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert out["state"] == "live"  # never _fail over a refused move
    r = row(conn)
    assert r["resources"]["refused_plan_id"] == v2 and r["resources"]["refused_url"] == new_url


def test_a_refused_url_only_update_does_not_poison_the_plan_it_diffed(conn, gads, live):
    """H27 (round 3, S5b): a plain page move under an *unchanged* plan (H9's own case -- no plan was ever
    refused, so `_work` sends this through `_update`, not `_refresh_url`, which only exists once a plan is
    already marked) still needs the same protection when Google refuses the structural mutate: the plan it was
    diffing (`old["id"] == plan["id"]`, `url_only` -- nothing about content was ever being asked to change) must
    not be marked `resources.refused_plan_id` for it. Doing that would freeze the version that is, after all,
    still the one actually running fine, and block a later, genuinely newer plan from ever being tried against
    it -- the fix: `_refuse_url` records the address instead, exactly like `_refresh_url`'s own refusal."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert row(conn)["state"] == "live" and row(conn)["plan_id"] == v1

    new_url = "https://republished.business.test/"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || '{\"url_stale\": true}' WHERE business_id = %s "
        "AND channel = 'google_search'",
        (BIZ,),
    )
    gads.fail["diff"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["diff"]

    assert [k for k, _ in gads.sent[sent:]] == ["money", "diff"]  # _update's own two mutates, the second refused
    r = row(conn)
    assert r["state"] == "live" and r["plan_id"] == v1  # never advanced, but never marked refused either
    assert "refused_plan_id" not in r["resources"]  # S5b: the running plan was never the problem
    assert r["resources"]["refused_url"] == new_url
    assert "url_stale" not in r["resources"]
    assert r["last_error"]

    # a later, genuinely newer plan is not blocked by the earlier url-only refusal
    v2 = add_plan(conn, sections(monthly_usd=1500.0))
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert r["plan_id"] == v2 and r["state"] == "live" and "refused_plan_id" not in r["resources"]
    assert r["resources"]["final_url"] == new_url + "?src=gads"  # and the stale address finally caught up too


def test_a_refresh_url_restores_a_concurrent_queues_own_clearing_of_refused_plan_id(client, conn, gads, live):
    """H27 (round 3, S9): `_refresh_url` reads its row once, before sending its own mutate. A fresh approval's
    own `queue()` clears `resources.refused_plan_id` itself (a newer version supersedes whatever was refused)
    -- if that commits, on its own connection, while this call's mutate is still out, this call's *own* final
    write must not undo it by writing back its now-stale in-memory copy of the old, still-refused mark.
    `refused_plan_id` is restored from the row's current value server-side, exactly like every other `_KEPT`
    field (`_write_resources`'s own `keep_refused_plan_id=True`), never from this call's own snapshot."""
    key, v1 = launched(conn, gads)
    v2 = add_plan(conn, sections(headlines=["Refused Headline", *HEADLINES[1:]]), status="draft")
    gads.fail["diff"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    approve(client, conn, key, v2)
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_plan_id"] == v2

    new_url = "https://republished.business.test/"
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (new_url, BIZ))
    conn.execute(
        "UPDATE channel_campaign SET resources = resources || '{\"url_stale\": true}' WHERE business_id = %s "
        "AND channel = 'google_search'",
        (BIZ,),
    )
    real_diff = gads._diff

    def diff_then_a_fresh_approval_commits(body):
        out = real_diff(body)
        with db.connect(TEST_DB_URL) as other:  # a fresh approval, in its own request, while this mutate is out
            ads_run.queue(other, BIZ, GOOGLE, v1, ready=True)
        return out

    gads._diff = diff_then_a_fresh_approval_commits
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    r = row(conn)
    assert r["state"] == "live" and r["plan_id"] == v1
    assert "refused_plan_id" not in r["resources"]  # the concurrent queue()'s own clearing survives this write
    assert "url_stale" not in r["resources"]
    assert r["resources"]["final_url"] == new_url + "?src=gads"


# ---------------------------------------------------------------- H28: a page address Google Ads refused


def _move_page(conn, url: str) -> None:
    """The page's address moves (a publish, set_landing_page): the page row, then the ads' own flag, the way both
    writers do it (`ads_run.flag_moved_page`)."""
    old = landing.current(conn, BIZ)["url"]
    conn.execute("UPDATE page SET url = %s WHERE business_id = %s", (url, BIZ))
    ads_run.flag_moved_page(conn, BIZ, old, url)


def _refused_plan(conn, gads, v1: str) -> str:
    """v2 approved and refused by Google (`resources.refused_plan_id`): a page move from here on goes through
    `_refresh_url`, never `_update`."""
    v2 = add_plan(conn, sections(headlines=["Refused Headline", *HEADLINES[1:]]))
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
    gads.fail["diff"] = google_error(400, {"policyFindingError": "POLICY_FINDING"})
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_plan_id"] == v2
    return v2


def _due(conn) -> list[str]:
    q = f"SELECT business_id FROM channel_campaign c WHERE {ads_run.due(config.load(), GOOGLE)}"
    return [r["business_id"] for r in conn.execute(q).fetchall()]


def test_a_refused_page_address_is_never_re_sent_by_a_resume_and_the_status_line_names_it(conn, gads, live):
    """H28 (X3, Y1): once Google Ads refuses the move to an address (`resources.refused_url`), nothing sends that
    address again while the page stays at it -- not `_enable`'s own move before it turns paused ads back on (every
    resume used to re-send it, the money with it), and not the sweep. A resume turns the ads back on as they are,
    and clears `last_error` as every enable does; `refused_url` is what keeps saying why the ads are not at the
    page's address (`status_sentence`)."""
    _key, v1 = launched(conn, gads)
    refused = "https://refused.example/"
    _move_page(conn, refused)
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})  # stays: a re-send would show up here
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert r["resources"]["refused_url"] == refused and "url_stale" not in r["resources"]

    for _ in range(2):  # every resume, not only the first
        assert ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")["state"] == "paused"
        sent = len(gads.sent)
        out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
        assert gads.kinds()[sent:] == ["lookup", "enable"]  # the address was never sent again, nor the money
        r = row(conn)
        assert (out["state"], r["state"], r["plan_id"], r["last_error"]) == ("live", "live", v1, None)
        assert r["resources"]["refused_url"] == refused
        assert r["resources"]["final_url"] == PAGE_URL + "?src=gads"  # still the earlier page
        assert out["say"] == (
            "Google Search: live. Google Ads did not take the page address refused.example; the ads keep their "
            "earlier page — use a different address or fix the page."
        )
        assert plain(out["say"])

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)  # nor the sweep
    assert gads.sent[sent:] == [] and _due(conn) == []


@pytest.mark.parametrize("plan_refused", [False, True])
def test_a_refused_page_address_is_cleared_by_a_later_move_and_sent_again_after_it(conn, gads, live, plan_refused):
    """H28 (X1): `refused_url` is cleared once the ads do move to a different address, so a later move back to the
    once-refused one (the owner fixed that site, say) is sent like any other -- it used to stay marked forever, and
    X -> Y -> X never sent X. Both URL-only paths: `_update` (the approved plan is the running one) and
    `_refresh_url` (the approved plan is one Google refused)."""
    _key, v1 = launched(conn, gads)
    if plan_refused:
        _refused_plan(conn, gads, v1)
    x, y = "https://x-site.example/", "https://y-site.example/"
    _move_page(conn, x)
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_url"] == x

    _move_page(conn, y)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert r["resources"]["final_url"] == y + "?src=gads"
    assert "refused_url" not in r["resources"] and "x-site.example" not in ads_run.status_sentence(r)

    _move_page(conn, x)  # back to the address Google refused before
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    finals = [
        op["adGroupAdOperation"]["create"]["ad"]["finalUrls"]
        for kind, body in gads.sent[sent:]
        if kind == "diff"
        for op in body["mutateOperations"]
        if "create" in op.get("adGroupAdOperation", {})
    ]
    assert [x + "?src=gads"] in finals
    r = row(conn)
    assert r["resources"]["final_url"] == x + "?src=gads"
    assert "refused_url" not in r["resources"] and "url_stale" not in r["resources"]
    assert (r["resources"].get("refused_plan_id") is not None) is plan_refused  # the plan's own mark is untouched


def test_moving_back_to_the_address_the_ads_still_carry_clears_the_refusal(conn, gads, live):
    """H28: the page going back to the address the ads never left (the refused move undone) needs nothing sent to
    move them, and the refusal is moot: `refused_url` goes and the status line stops naming it."""
    launched(conn, gads)
    _move_page(conn, "https://y-site.example/")
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_url"] == "https://y-site.example/"

    _move_page(conn, PAGE_URL)
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    assert "diff" not in gads.kinds()[sent:]  # nothing to move
    r = row(conn)
    assert "refused_url" not in r["resources"] and "url_stale" not in r["resources"]
    assert r["resources"]["final_url"] == PAGE_URL + "?src=gads"
    assert ads_run.status_sentence(r) == "Google Search: live."


def test_a_move_committed_while_a_refused_address_was_out_is_kept_for_the_next_step(conn, gads, live):
    """H28 (Y2): `_refuse_url` keeps `url_stale` through the same CASE as every other write -- a move to Z that
    commits while the mutate for X is out (and is then refused) must not have its flag dropped with X's; the next
    step moves the ads to Z, and that successful move clears the refusal of X."""
    launched(conn, gads)
    x, z = "https://x-site.example/", "https://z-site.example/"
    _move_page(conn, x)
    real_diff = gads._diff

    def refused_while_a_move_commits(body):
        with db.connect(TEST_DB_URL) as other:  # set_landing_page(Z), in its own request, while X is out
            other.execute("UPDATE page SET url = %s WHERE business_id = %s", (z, BIZ))
            ads_run.flag_moved_page(other, BIZ, x, z)
        return google_error(400, {"urlFieldError": "INVALID_URL"})

    gads._diff = refused_while_a_move_commits
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    gads._diff = real_diff

    r = row(conn)
    assert r["resources"]["refused_url"] == x
    assert r["resources"]["url_stale"] is True  # Z's own flag survived X's refusal
    assert _due(conn) == [BIZ]

    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert r["resources"]["final_url"] == z + "?src=gads"
    assert "url_stale" not in r["resources"] and "refused_url" not in r["resources"]


@pytest.mark.parametrize("plan_refused", [False, True])
def test_a_page_back_at_the_refused_address_before_the_ads_moved_sends_nothing(
    conn, gads, live, monkeypatch, plan_refused
):
    """H28: the page leaves the refused address and comes back to it before the ads ever moved (paused meanwhile,
    or the switch off): the flag both moves left (`url_stale`) is dropped at no Google cost -- by the resume's
    enable and by the sweep alike (`_update`'s URL-only branch, or `_refresh_url` under a refused plan) -- and the
    refusal stands."""
    _key, v1 = launched(conn, gads)
    if plan_refused:
        _refused_plan(conn, gads, v1)
    x, y = "https://x-site.example/", "https://y-site.example/"
    _move_page(conn, x)
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})  # stays: a re-send would show up here
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert row(conn)["resources"]["refused_url"] == x

    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    _move_page(conn, y)
    _move_page(conn, x)  # back before anything moved
    assert row(conn)["resources"]["url_stale"] is True
    sent = len(gads.sent)
    out = ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    assert gads.kinds()[sent:] == ["lookup", "enable"] and out["state"] == "live"
    r = row(conn)
    assert "url_stale" not in r["resources"] and r["resources"]["refused_url"] == x

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    _move_page(conn, y)
    _move_page(conn, x)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    assert _due(conn) == [BIZ]
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert gads.sent[sent:] == [] and _due(conn) == []
    r = row(conn)
    assert "url_stale" not in r["resources"] and r["resources"]["refused_url"] == x
    assert r["resources"]["final_url"] == PAGE_URL + "?src=gads"


def test_a_refused_page_address_reads_plainly_wherever_the_ads_exist():
    """H28: the sentence `resources.refused_url` adds -- the page address by its host, what the ads do meanwhile,
    what to do -- in every state with ads built, jargon-free; it replaces the "not all in yet" of an update on its
    way, as the refused plan's own mark does (both say what happened instead)."""
    now = datetime.now(UTC)
    refused = {"refused_url": "https://refused.example/new-page"}
    line = (
        "Google Ads did not take the page address refused.example; the ads keep their earlier page — use a "
        "different address or fix the page."
    )
    rows = [
        {"channel": GOOGLE, "state": "launching", "last_error": None, "resources": refused},
        {
            "channel": GOOGLE,
            "state": "live",
            "last_error": google_ads.REFUSED_WITH_REASON.format("urlFieldError: INVALID_URL"),
            "resources": refused,
        },
        {
            "channel": GOOGLE,
            "state": "paused",
            "last_error": None,
            "paused_at": now,
            "resources": {**refused, "paused_by": "owner"},
        },
        {"channel": GOOGLE, "state": "limited", "last_error": None, "resources": refused},
    ]
    for r in rows:
        sentence = ads_run.status_sentence(r)
        assert line in sentence and plain(sentence), sentence
        assert sentence.endswith(".") and ".." not in sentence
    assert "not all in yet" not in ads_run.status_sentence(rows[1])
    live_row = {"channel": GOOGLE, "state": "live", "last_error": None, "resources": {}}
    assert ads_run.status_sentence(live_row) == "Google Search: live."
    # round 5: a retry still owed (RETRYING) keeps S16's phrase -- a refused address says what was refused, not
    # that nothing is on its way (a newer version's update, say, is)
    retrying = ads_run.status_sentence(
        {"channel": GOOGLE, "state": "live", "last_error": google.RETRYING, "resources": refused}
    )
    assert retrying == (f"Google Search: live, but your latest change is not all in yet. {line} {google.RETRYING}.")


def test_a_passing_check_is_the_retry_of_the_address_google_refused(conn, gads, live):
    """H29 ("fix the page" is the retry): `retry_refused_address`, which every saved check that passes calls, clears
    the refusal and queues the move -- only for the address actually refused -- and the next step sends it."""
    launched(conn, gads)
    x = "https://x-site.example/"
    _move_page(conn, x)
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_url"] == x and _due(conn) == []

    assert ads_run.retry_refused_address(conn, BIZ, "https://another-site.example/") == []
    assert row(conn)["resources"]["refused_url"] == x and "url_stale" not in row(conn)["resources"]
    assert ads_run.retry_refused_address(conn, BIZ, x) == [GOOGLE]
    r = row(conn)
    assert "refused_url" not in r["resources"] and r["resources"]["url_stale"] is True and _due(conn) == [BIZ]

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)  # the next sweep
    finals = [
        op["adGroupAdOperation"]["create"]["ad"]["finalUrls"]
        for kind, body in gads.sent[sent:]
        if kind == "diff"
        for op in body["mutateOperations"]
        if "create" in op.get("adGroupAdOperation", {})
    ]
    assert [x + "?src=gads"] in finals
    r = row(conn)
    assert r["resources"]["final_url"] == x + "?src=gads" and "url_stale" not in r["resources"]
    assert "refused_url" not in r["resources"] and ads_run.status_sentence(r) == "Google Search: live."


def test_a_skip_at_the_refused_address_drops_the_retry_nothing_is_left_for(conn, gads, live):
    """Round 5 (the re-review's C): a move to S that Google did not answer (RETRYING, `url_stale` kept for the
    retry), then the page back at the address Google refused before the retry ran -- the skip leaves nothing to
    retry, so the status line must not keep promising one: RETRYING goes with the flag. Another sentence stays."""
    launched(conn, gads)
    r_url = "https://r-site.example/"
    _move_page(conn, r_url)
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    _move_page(conn, "https://s-site.example/")
    gads.fail["diff"] = google_error(503)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    r = row(conn)
    assert r["last_error"] == google.RETRYING and r["resources"]["url_stale"] is True

    _move_page(conn, r_url)  # back before the retry
    del gads.fail["diff"]
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)

    assert gads.sent[sent:] == []
    r = row(conn)
    assert r["last_error"] is None and "url_stale" not in r["resources"] and r["resources"]["refused_url"] == r_url
    assert ads_run.status_sentence(r) == (
        "Google Search: live. Google Ads did not take the page address r-site.example; the ads keep their earlier "
        "page — use a different address or fix the page."
    )


# ---------------------------------------------------------------- spec 2026-10-11 §5 (plan D3): a drop is kept out of every build


def _dropped_row(conn, plan_id: str) -> None:
    from agent import ads_refusal
    from agent.channels.google_search.client import RefusalDetail

    found = RefusalDetail(
        code="policyFindingError: POLICY_FINDING",
        operation=5,
        kind="headline",
        position=1,
        field="mutate_operations[5].ad_group_ad_operation.create.ad.responsive_search_ad.headlines[1].text",
        topic="TRADEMARKS_IN_AD_TEXT",
        type="PROHIBITED",
        exemptible=False,
        certificate=False,
        text="Same-Week Appointments",
    )
    ads_refusal.record(
        conn,
        business_id=BIZ,
        plan_id=plan_id,
        channel=GOOGLE,
        review_id=None,
        found=[
            (found, "ad_text", "dropped"),
            (dataclasses.replace(found, kind="keyword", text="invisalign cost"), "keyword", "dropped"),
        ],
    )


def test_the_adapters_build_carries_the_versions_dropped_lines(conn):
    _key, plan_id = seed(conn)
    _dropped_row(conn, plan_id)
    built = google.ADAPTER.build(
        conn, config.load(), BIZ, plans.get(conn, BIZ, plan_id), KNOWLEDGE, ads_run._account(conn, BIZ, GOOGLE)
    )
    assert built.payload["dropped"] == ads_launch.Dropped(
        headlines=frozenset({"Same-Week Appointments"}), keywords=frozenset({"invisalign cost"})
    )


def test_a_line_admiry_dropped_after_googles_refusal_is_left_out_of_the_create(conn, gads, live):
    """The ads_refusal row, never the plan's json, is what the create leaves out: the launch, the hourly ads-launch
    and an update build the same ads (they all go through adapter.build)."""
    _key, plan_id = seed(conn, secs=sections(headlines=[*HEADLINES, "Family Dentist in Palo Alto"]))
    _dropped_row(conn, plan_id)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES (%s, %s, %s, 'queued')",
        (BIZ, GOOGLE, plan_id),
    )
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    create = gads.bodies("create")[0]["mutateOperations"]
    ad = next(
        op["adGroupAdOperation"]["create"]["ad"]["responsiveSearchAd"] for op in create if "adGroupAdOperation" in op
    )
    assert [h["text"] for h in ad["headlines"]] == [
        "Invisalign in Palo Alto",
        "Clear Aligners Near You",
        "Family Dentist in Palo Alto",
    ]
    keywords = [
        op["adGroupCriterionOperation"]["create"]["keyword"]["text"]
        for op in create
        if "adGroupCriterionOperation" in op
    ]
    assert "invisalign cost" not in keywords and "invisalign palo alto" in keywords
    stored = plans.get(conn, BIZ, plan_id)["sections"]["ads"]["channels"][0]["headlines"]
    assert stored == [*HEADLINES, "Family Dentist in Palo Alto"]  # the agreed version untouched


# ---------------------------------------------------------------- final review fix wave (ruling L2): the business's drop

FOUR = [*HEADLINES, "Family Dentist in Palo Alto"]  # one more than plans.MIN_HEADLINES: one can go
REFUSED_HEADLINE = "Same-Week Appointments"  # `_dropped_row`'s headline (its phrase: "invisalign cost")


def _live_with_a_drop(conn) -> tuple[str, str]:
    """v1 (four headlines) live, built without the headline and the search phrase its review dropped. (key, v1)."""
    key, v1 = seed(conn, secs=sections(headlines=FOUR))
    _dropped_row(conn, v1)
    queued(conn, v1)
    assert ads_run.launch(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
    return key, v1


def _created(ops: list[dict], kind: str) -> list[dict]:
    return [op[kind]["create"] for op in ops if "create" in op.get(kind, {})]


def test_a_line_google_refused_stays_out_of_every_later_versions_update(client, conn, gads, live):
    """Ruling L2 (final review, Important 2; spec §5 "How a drop is kept"): a line Google refused stays out of every
    later Google build of the business, by its exact text -- a budget-only v2 never adds v1's refused search phrase
    back, a v3 whose ad text changed gets a fresh ad without v1's refused headline, and a v4 after it still leaves
    both out."""
    key, _v1 = _live_with_a_drop(conn)
    sent = len(gads.sent)
    v2 = add_plan(conn, sections(monthly_usd=600.0, headlines=FOUR), status="draft")
    approve(client, conn, key, v2, idem="c2")
    assert [k for k, _ in gads.sent[sent:]] == ["money"]  # the money alone: the refused phrase is never added back
    assert row(conn)["plan_id"] == v2
    for idem, extra in (("c3", "Evening Appointments Too"), ("c4", "Weekend Visits Available")):
        sent = len(gads.sent)
        version = add_plan(conn, sections(monthly_usd=600.0, headlines=[*FOUR, extra]), status="draft")
        approve(client, conn, key, version, idem=idem)
        ops = [op for kind, body in gads.sent[sent:] if kind == "diff" for op in body["mutateOperations"]]
        (ad,) = _created(ops, "adGroupAdOperation")
        assert [h["text"] for h in ad["ad"]["responsiveSearchAd"]["headlines"]] == [
            h for h in [*FOUR, extra] if h != REFUSED_HEADLINE
        ]
        assert "invisalign cost" not in [c["keyword"]["text"] for c in _created(ops, "adGroupCriterionOperation")]
        assert row(conn)["plan_id"] == version


def test_a_page_move_of_a_later_version_leaves_an_earlier_refusal_out(conn, gads, live):
    """Ruling L2: `move_page` builds the running version's fresh ad at the page's new address -- for a running v2 (its
    budget-only update went: money alone), v1's refused headline and search phrase stay out of it."""
    _key, v1 = _live_with_a_drop(conn)
    v2 = add_plan(conn, sections(monthly_usd=600.0, headlines=FOUR))
    conn.execute("UPDATE plan SET status = 'superseded' WHERE id = %s", (v1,))
    conn.execute("UPDATE channel_campaign SET plan_id = %s WHERE business_id = %s", (v2, BIZ))
    new = "https://brightsmile.example/new"
    _move_page(conn, new)
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    ops = [op for kind, body in gads.sent[sent:] if kind == "diff" for op in body["mutateOperations"]]
    (ad,) = _created(ops, "adGroupAdOperation")
    assert ad["ad"]["finalUrls"] == [f"{new}?src=gads"]
    assert [h["text"] for h in ad["ad"]["responsiveSearchAd"]["headlines"]] == [
        h for h in FOUR if h != REFUSED_HEADLINE
    ]
    assert _created(ops, "adGroupCriterionOperation") == []  # v1's refused phrase is never added back
    r = row(conn)
    assert (r["plan_id"], r["resources"]["final_url"]) == (v2, f"{new}?src=gads")


def _block_rule(conn, value: str, *, applies_to: str = "any") -> None:
    """One active `block` refusal_rule row, as /ops/rules would write it (ruling Y1's tests: a rule that turned
    active after a plan was approved -- `launch` and `move_page` must still guard against the ad it leaves too
    thin, the same way `update` already does)."""
    from agent import db

    conn.execute(
        "INSERT INTO refusal_rule (id, kind, applies_to, value, why, level, status, proposed_by) "
        "VALUES (%s, 'word', %s, %s, 'a word we avoid', 'block', 'active', 'admin:founder@example.com')",
        (db.new_id("rr"), applies_to, value),
    )


def test_a_create_an_active_block_rule_leaves_too_thin_is_never_sent(conn, gads, live, monkeypatch):
    """Ruling Y1: `launch`'s create path carries the same too-thin guard `update` already has -- a block rule
    active by the time the create is sent (even though the plan was approved before it existed) leaves this
    business's three headlines down to two, fewer than Google takes, so no create mutate is sent at all: the row
    fails with TOO_LITTLE_COPY rather than building a too-thin ad."""
    _key, v1 = seed(conn)
    queued(conn, v1)
    monkeypatch.setenv("AGENT_REFUSAL_RULES", "on")
    _block_rule(conn, "same-week", applies_to="headline")
    sent = len(gads.sent)
    out = ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert (out["state"], out["last_error"]) == ("failed", google.TOO_LITTLE_COPY)
    assert "create" not in [k for k, _ in gads.sent[sent:]]  # not even the campaign budget went
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("failed", google.TOO_LITTLE_COPY)
    assert "creating_plan_id" not in r["resources"]  # the attempt was never even marked


def test_a_page_move_an_active_block_rule_leaves_too_thin_is_never_sent(conn, gads, live, monkeypatch):
    """Ruling Y1: `move_page` carries the same too-thin guard `update` already has -- a block rule that turned
    active since v1 was built now leaves its ad two headlines, fewer than Google takes, so the fresh ad at the new
    address is never sent: the money goes first (ruling S16), the address mutate is TOO_LITTLE_COPY, not retried,
    and the ads keep running at the old address."""
    _key, v1 = launched(conn, gads)
    new = "https://brightsmile.example/new"
    _move_page(conn, new)
    monkeypatch.setenv("AGENT_REFUSAL_RULES", "on")
    _block_rule(conn, "same-week", applies_to="headline")
    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)
    assert [k for k, _ in gads.sent[sent:]] == ["money"]  # the address mutate never went
    r = row(conn)
    assert (r["state"], r["plan_id"], r["last_error"]) == ("live", v1, google.TOO_LITTLE_COPY)
    assert r["resources"]["refused_plan_id"] == v1
    assert r["resources"]["final_url"] == PAGE_URL + "?src=gads"  # the address never moved


def test_an_update_an_earlier_refusal_leaves_too_thin_is_never_sent(client, conn, gads, live):
    """Ruling L2's too-thin check: an update is never reviewed, so a v2 whose ad text keeps v1's refused headline among
    three lines -- two once it is left out, fewer than Google takes -- is never sent for Google to refuse: the money
    goes first (ruling S16), the rest is TOO_LITTLE_COPY, not tried again, and the ads keep running v1."""
    key, v1 = _live_with_a_drop(conn)
    sent = len(gads.sent)
    thin = ["Invisalign in Palo Alto", REFUSED_HEADLINE, "Evening Appointments Too"]
    v2 = add_plan(conn, sections(headlines=thin), status="draft")
    approve(client, conn, key, v2, idem="c2")
    assert [k for k, _ in gads.sent[sent:]] == ["money"]
    r = row(conn)
    assert (r["state"], r["plan_id"], r["resources"]["refused_plan_id"]) == ("live", v1, v2)
    assert r["last_error"] == google.TOO_LITTLE_COPY
    before = len(gads.sent)
    assert tick.run("ads-launch", url=TEST_DB_URL)["checked"] == 0 and len(gads.sent) == before
