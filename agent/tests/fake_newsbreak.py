"""A small stateful fake of NewsBreak's Business API (M1 Task 2), an `httpx.MockTransport` handler modelled on
`test_ads_run.FakeGoogle`: it keeps the ad accounts, campaigns, ad sets, ads, uploaded assets, events and report rows it
was given or made, so a list finds what a create made -- also when the create's answer was lost -- and a test can read
the state NewsBreak would be in. The `nbapi` fixture (conftest) installs it on the client module's `client()`.

Every request is recorded in `requests`, and in `sent` as (endpoint, payload): the endpoint is the path under
`business-api/v1` without its trailing id (`campaign/create`, `campaign/updateStatus`, `event/getList`), or
`tracking/attribute` for the Conversion API's postback; the payload is the JSON body, a GET's query (the documented
array parameters as lists), or the asset upload's form (the file as {filename, content_type, content}).

Knobs: `fail[endpoint]` answers that endpoint with a response, or raises an exception, instead; `lose` holds endpoints
whose call is carried out and whose answer then never arrives (once each); `throttle` is how many of the next API
requests NewsBreak answers with code 4034 -- the Rate Limits article documents a global limit per developer app and,
independently, limits per endpoint: with `throttle_endpoints` empty every API endpoint counts (the global limit), with
endpoints named only those do and the others keep answering (an endpoint's own limit); the token-less postback is never
throttled (it is no developer app's call). `org_admin` False is a token that is not an Org Admin's: every spending-cap
update answers "Update failed. No access to change adAccount budget". `down` fails every request.

Its answers follow the articles the client pins (`agent/channels/newsbreak/client.py`): the `code`/`errMsg`/`data`
envelope over HTTP 200; 4033 for a missing or wrong `Access-Token`; 4034 when throttled; 403 for an organisation the
token is not in; the spending cap's per-account messages, word for word; every `business-api/v1` endpoint's documented
method (another one answers HTTP 405) and required fields. A refusal the docs give no code for -- a required field
missing, an id that is not there -- is `INVALID` (40001), this fake's own stand-in. An ad set's `targeting` follows
Create an Ad Set and Update An Ad Set word for word -- "If not specified, defaults to "all" (unlimited) for each
dimension" -- with no merge: a dimension a create or an update leaves out is stored as {"positive": ["all"]}, so an
update without `targeting` widens the ad set to everyone. The postback needs no token (the docs' postback URLs carry
none) and answers HTTP 400 without `callback` or `event_type`; `rejected_clicks` holds click ids it answers HTTP 400 for
(NewsBreak's own answer to a click id past its 7 days). An ad is created under review (`auditStatus` PENDING, NewsBreak's
own `statusTxt`); `reject` and `approve` are NewsBreak's review."""

from __future__ import annotations

import hashlib
import itertools
import json
from email.parser import BytesParser
from email.policy import HTTP

import httpx

API_PREFIX = "/business-api/v1/"
TRACKING = "tracking/attribute"
THROTTLED = 4034
INVALID = 40001  # this fake's stand-in for a refusal NewsBreak documents no code for
# the ten dimensions of Targeting Values (https://advertising-api.newsbreak.com/hc/en-us/articles/40556309321357)
TARGETING_DIMENSIONS = (
    "gender",
    "ageGroup",
    "interest",
    "location",
    "deviceLocation",
    "language",
    "os",
    "manufacturer",
    "carrier",
    "network",
)
NO_BUDGET_ACCESS = "Update failed. No access to change adAccount budget"
ACCOUNT_CATEGORIES = {
    "ECOMM",
    "ECOMM-HEALTH",
    "LEADGEN-NATIONAL",
    "LEADGEN-LOCAL",
    "APP-APP_CONVERSION",
    "APP-WEB2APP",
    "SUBSCRIPTION_CONTENT",
    "TRAFFIC_ARBITRAGE-SEARCH",
    "TRAFFIC_ARBITRAGE-CONTENT",
    "TRAFFIC_ARBITRAGE-OFFER_WALL",
}
OBJECTIVES = {"WEB_CONVERSION", "APP_CONVERSION", "REACH", "WEB_TRAFFIC", "APP_TRAFFIC"}
BID_TYPES = {
    "CPM",
    "CPC",
    "DAY_ONE_MAX_CONVERSION_VALUE",
    "MAX_CONVERSION",
    "MAX_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
    "DAY_ONE_TARGET_ROAS",
}
CREATIVE_TYPES = {"IMAGE", "VIDEO", "GIF", "PLAYABLE_VIDEO"}
PAGE_SIZES = {5, 10, 20, 50, 100, 200, 500}
DIMENSIONS = {"DATE", "HOUR", "ORG", "AD_ACCOUNT", "CAMPAIGN", "AD_SET", "AD", "PLACEMENT"}
METRICS = {
    "COST",
    "IMPRESSION",
    "CLICK",
    "CONVERSION",
    "VALUE",
    "CPM",
    "CPC",
    "CPA",
    "CTR",
    "CVR",
    "VPA",
    "COMPLETE_PAYMENT_ROAS",
    "SALE_ROAS",
    "APP_PURCHASE_ROAS",
    "APP_IN_APP_AD_IMPR_ROAS",
}
DATE_RANGES = {
    "FIXED",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "MONTH_TO_DATE",
    "QUARTER_TO_DATE",
    "TODAY",
}
CAP_MAX_CENTS = 10_000_000_000
UNDER_REVIEW = "Under review. It usually takes 1-2 work days."
# endpoint -> (method, its handler's name); the endpoints whose path ends in an id
METHODS = {
    "ad-account/getGroupsByOrgIds": "GET",
    "ad-account/create": "POST",
    "balance/updateAccountsBudget": "POST",
    "balance/getAccountBudgetInfo": "POST",
    "ad/uploadAssets": "POST",
    "campaign/getList": "GET",
    "campaign/create": "POST",
    "campaign/update": "PUT",
    "campaign/updateStatus": "PUT",
    "ad-set/getList": "GET",
    "ad-set/create": "POST",
    "ad-set/update": "PUT",
    "ad-set/updateStatus": "PUT",
    "ad/getList": "GET",
    "ad/create": "POST",
    "ad/update": "PUT",
    "ad/updateStatus": "PUT",
    "reports/getIntegratedReport": "POST",
    "event/getList": "GET",
    TRACKING: "GET",
}
WITH_ID = {
    "campaign/update",
    "campaign/updateStatus",
    "ad-set/update",
    "ad-set/updateStatus",
    "ad/update",
    "ad/updateStatus",
    "event/getList",
}
LIST_PARAMS = {"orgIds", "onlineStatus", "campaignIds", "adSetIds", "os"}  # documented as arrays (`?k=1&k=2`)


def envelope(code: int, err_msg: str = "", data: object = None, *, status: int = 200) -> httpx.Response:
    """NewsBreak's answer: `code` (0 = success), `errMsg` when code != 0, `data`."""
    body: dict = {"code": code, "errMsg": err_msg}
    if data is not None:
        body["data"] = data
    return httpx.Response(status, json=body)


def ok(data: object) -> httpx.Response:
    return envelope(0, "", data)


def invalid(why: str) -> httpx.Response:
    return envelope(INVALID, why)


class FakeNewsBreak:
    def __init__(self, *, token: str, org_id: str, org_name: str = "Admiry") -> None:
        self.token = token
        self.org_id = org_id
        self.org_name = org_name
        self.sent: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, httpx.Response | Exception] = {}
        self.lose: set[str] = set()
        self.throttle = 0
        self.throttle_endpoints: set[str] = set()  # empty: every API endpoint (the global limit)
        self.org_admin = True  # the token is an Org Admin's: it may set spending caps
        self.down = False
        self.ids = (str(n) for n in itertools.count(1_000_001))  # NewsBreak's ids are numeric strings
        self.clock = (str(n) for n in itertools.count(1_790_000_000))  # createTime / updateTime: Unix seconds
        self.account_status = "ACTIVE"  # the status ad-account/create gives a new account
        self.accounts: dict[str, dict] = {}  # id -> the account as ad-account/create answers it
        self.caps: dict[str, int] = {}  # ad account id -> its spending cap in cents, once one is set
        self.spent: dict[str, int] = {}  # ad account id -> its total spend in cents
        self.campaigns: dict[str, dict] = {}
        self.ad_sets: dict[str, dict] = {}
        self.ads: dict[str, dict] = {}
        self.assets: dict[str, dict] = {}  # assetUrl -> {"adAccountId", "filename", "content_type", "content"}
        self.events: dict[str, list[dict]] = {}  # ad account id -> its events (made by hand in Ads Manager)
        self.report_rows: list[dict] = []  # what reports/getIntegratedReport answers from, filtered by day and account
        self.conversions: list[dict] = []  # the postbacks NewsBreak took
        self.rejected_clicks: set[str] = set()
        self.waits: list[float] = []  # the seconds the adapter paused between two calls (`nbapi` keeps them here)
        self.on_request: list = []  # callables run with each request as it arrives (a test's clock, say)

    # ---------------------------------------------------------------- the transport

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for hook in self.on_request:
            hook(request)
        if self.down:
            raise httpx.ConnectError("NewsBreak is down", request=request)
        if request.url.host != "business.newsbreak.com":
            return httpx.Response(404, text="not NewsBreak")
        endpoint, ident = self.endpoint(request.url.path)
        payload = self.payload(request)
        self.sent.append((endpoint, payload))
        if endpoint not in METHODS:
            return httpx.Response(404, text="<html>Not Found</html>")
        if request.method != METHODS[endpoint]:
            return httpx.Response(405, text="<html>Method Not Allowed</html>")
        if (
            self.throttle > 0
            and endpoint != TRACKING
            and (endpoint in self.throttle_endpoints or not self.throttle_endpoints)
        ):
            self.throttle -= 1
            return envelope(THROTTLED, "Rate limit exceeded")
        if endpoint != TRACKING and request.headers.get("Access-Token") != self.token:
            return envelope(4033, "Invalid access token")
        failure = self.fail.get(endpoint)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        answer = getattr(self, "_" + endpoint.replace("/", "_").replace("-", "_"))(ident, payload)
        if endpoint in self.lose:
            self.lose.discard(endpoint)
            raise httpx.ReadTimeout("the answer never came back", request=request)
        return answer

    @staticmethod
    def endpoint(path: str) -> tuple[str, str | None]:
        rest = path.removeprefix(API_PREFIX).strip("/") if path.startswith(API_PREFIX) else path.strip("/")
        head, _, tail = rest.rpartition("/")
        if head in WITH_ID:
            return head, tail
        return rest, None

    @staticmethod
    def payload(request: httpx.Request) -> dict:
        if request.method == "GET":
            params = request.url.params
            return {k: params.get_list(k) if k in LIST_PARAMS else params.get(k) for k in params}
        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
            )
            form: dict = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                content = part.get_payload(decode=True)
                if part.get_filename() is None:
                    form[name] = content.decode()
                else:
                    form[name] = {
                        "filename": part.get_filename(),
                        "content_type": part.get_content_type(),
                        "content": content,
                    }
            return form
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return {"_raw": request.content}
        return body if isinstance(body, dict) else {"_raw": body}

    def kinds(self) -> list[str]:
        return [endpoint for endpoint, _ in self.sent]

    def bodies(self, endpoint: str) -> list[dict]:
        return [payload for e, payload in self.sent if e == endpoint]

    # ---------------------------------------------------------------- what a test sets up

    def add_account(self, name: str, *, status: str = "ACTIVE") -> str:
        """An ad account already in the Organization (made earlier, by hand or by a lost create)."""
        return self._make_account(name, "Admiry", "Business and Industrial//Small Business", "LEADGEN-LOCAL", status)

    def add_campaign(self, ad_account_id: str, name: str, *, status: str = "ON") -> str:
        return self._make_campaign(ad_account_id, name, "WEB_TRAFFIC", status)["id"]

    def reject(self, ad_id: str, text: str) -> None:
        """NewsBreak's review turned the ad down, saying `text` (`statusTxt`)."""
        self.ads[ad_id].update(auditStatus="REJECTED", onlineStatus="REJECTED", statusTxt=text)

    def approve(self, ad_id: str) -> None:
        self.ads[ad_id].update(auditStatus="APPROVED", onlineStatus="ACTIVE", statusTxt="")

    def add_report_row(
        self, ad_id: str, day: str, *, cost: int, impression: int = 0, click: int = 0, conversion: int = 0
    ):
        """A row of the HOURLY report for one ad on one day (`cost` in cents), named from the fake's own objects; the
        cost also counts as the account's spend (what the spending cap is checked against)."""
        ad = self.ads[ad_id]
        ad_set = self.ad_sets[ad["adSetId"]]
        campaign = self.campaigns[ad["campaignId"]]
        account = self.accounts[ad["adAccountId"]]
        row = {
            "date": day,
            "adId": ad_id,
            "ad": ad["name"],
            "adSetId": ad_set["id"],
            "adSet": ad_set["name"],
            "campaignId": campaign["id"],
            "campaign": campaign["name"],
            "adAccountId": account["id"],
            "adAccount": account["name"],
            "cost": cost,
            "costDecimal": float(cost),
            "impression": impression,
            "click": click,
            "conversion": conversion,
        }
        self.report_rows.append(row)
        self.spent[account["id"]] = self.spent.get(account["id"], 0) + cost
        return row

    # ---------------------------------------------------------------- ad accounts and the spending cap

    def _make_account(self, name: str, company: str, industry: str, category: str, status: str) -> str:
        now = next(self.clock)
        account = {
            "id": next(self.ids),
            "orgId": self.org_id,
            "name": name,
            "orgName": self.org_name,
            "companyName": company,
            "industry": industry,
            "accountCategory": category,
            "paymentType": "PRE_PAID",
            "billingThreshold": 0,
            "status": status,
            "ownerId": "1",
            "createTime": now,
            "updateTime": now,
        }
        self.accounts[account["id"]] = account
        return account["id"]

    def _ad_account_getGroupsByOrgIds(self, ident, p: dict) -> httpx.Response:
        if not p.get("orgIds"):
            return invalid("orgIds is required")
        rows = [
            {
                "id": org,
                "name": self.org_name,
                "adAccounts": [
                    {"id": a["id"], "name": a["name"], "createTime": a["createTime"]} for a in self.accounts.values()
                ],
            }
            for org in p["orgIds"]
            if org == self.org_id
        ]
        return ok({"list": rows})

    def _ad_account_create(self, ident, p: dict) -> httpx.Response:
        missing = [k for k in ("adAccountName", "companyName", "industry", "accountCategory", "orgId") if not p.get(k)]
        if missing:
            return invalid(f"{missing[0]} is required")
        if p["orgId"] != self.org_id:
            return envelope(403, "Permission denied")
        if not 1 <= len(p["adAccountName"]) <= 256 or not 1 <= len(p["companyName"]) <= 1024:
            return invalid("adAccountName must be 1-256 characters and companyName 1-1024")
        if p["accountCategory"] not in ACCOUNT_CATEGORIES:
            return invalid("accountCategory is not one of the listed values")
        category, _, subcategory = p["industry"].partition("//")
        if not category or not subcategory:
            return invalid("industry must be <category>//<subcategory> from the industry list")
        made = self._make_account(
            p["adAccountName"], p["companyName"], p["industry"], p["accountCategory"], self.account_status
        )
        return ok(self.accounts[made])

    def _balance_updateAccountsBudget(self, ident, p: dict) -> httpx.Response:
        updates = p.get("adAccountsBudgetUpdate")
        if not isinstance(updates, list) or not updates:
            return invalid("adAccountsBudgetUpdate is required")
        rows = []
        for u in updates:
            account, budget = str(u.get("adAccountId")), u.get("budget")
            if account not in self.accounts:
                message = "Update failed. adAccount not found"
            elif not self.org_admin:
                message = NO_BUDGET_ACCESS
            elif not isinstance(budget, int) or not 0 <= budget <= CAP_MAX_CENTS:
                message = "Update failed. Budget must be between 0 cents and 10000000000 cents"
            elif budget <= self.spent.get(account, 0):
                message = (
                    "Update failed. New budget must be greater than current spending, current spending: "
                    f"{self.spent.get(account, 0)} cents"
                )
            elif account not in self.caps:
                message = f"Budget is enabled and set successfully to {budget} cents"
            else:
                message = f"Budget updated successfully. From {self.caps[account]} cents to {budget} cents"
            if message.startswith("Budget"):
                self.caps[account] = budget
            rows.append({"adAccountId": account, "message": message})
        return ok({"list": rows})

    def _balance_getAccountBudgetInfo(self, ident, p: dict) -> httpx.Response:
        """Get Account Spending Cap (article 40262348828173): the figures only for an account with a cap -- as the
        article's example writes them, decimal strings, here in cents (the article names no unit; the API's other money
        is cents) -- else `canViewBudget` false and why."""
        ids = p.get("accountIds")
        if not isinstance(ids, list) or not ids:
            return invalid("accountIds is required")
        rows = []
        for account in (str(i) for i in ids[:500]):
            if account not in self.accounts:
                rows.append({"accountId": account, "canViewBudget": False, "failMessage": "No access"})
            elif account not in self.caps:
                rows.append(
                    {
                        "accountId": account,
                        "canViewBudget": False,
                        "failMessage": "No account spending cap is set for this account",
                    }
                )
            else:
                cap, spent = self.caps[account], self.spent.get(account, 0)
                rows.append(
                    {
                        "accountId": account,
                        "accountRemaining": str(float(cap - spent)),
                        "accountSpendingCap": str(float(cap)),
                        "accountTotalSpend": str(float(spent)),
                        "canViewBudget": True,
                    }
                )
        return ok({"list": rows})

    def _ad_uploadAssets(self, ident, p: dict) -> httpx.Response:
        asset, account = p.get("asset"), p.get("adAccountId")
        if not isinstance(asset, dict) or not asset.get("content"):
            return invalid("asset is required")
        if account not in self.accounts:
            return invalid("adAccountId not found")
        digest = hashlib.sha1(asset["content"], usedforsecurity=False).hexdigest()  # a CDN-like file name
        extension = asset["filename"].rpartition(".")[2]
        url = f"https://static.particlenews.com/nova/assets/{account}/{digest}.{extension}"  # carries the account id
        self.assets[url] = {"adAccountId": account, **asset}
        return ok({"assetUrl": url, "mediaId": next(self.ids)})

    # ---------------------------------------------------------------- the lists

    def _page(self, objects: dict[str, dict], p: dict, *, filters: dict[str, str]) -> httpx.Response:
        if not p.get("adAccountId"):
            return invalid("adAccountId is required")
        try:
            page, size = int(p.get("pageNo") or ""), int(p.get("pageSize") or "")
        except ValueError:
            return invalid("pageNo and pageSize are required")
        if page < 1 or size not in PAGE_SIZES:
            return invalid("pageSize must be one of 5, 10, 20, 50, 100, 200, 500")
        rows = [o for o in objects.values() if o["adAccountId"] == p["adAccountId"]]
        for param, key in filters.items():
            if p.get(param):
                rows = [o for o in rows if o[key] in p[param]]
        if p.get("search"):
            rows = [o for o in rows if p["search"] in o["name"] or p["search"] == o["id"]]
        chunk = rows[(page - 1) * size : page * size]
        return ok(
            {"rows": chunk, "pageNo": page, "pageSize": size, "total": len(rows), "hasNext": page * size < len(rows)}
        )

    def _campaign_getList(self, ident, p: dict) -> httpx.Response:
        return self._page(self.campaigns, p, filters={})

    def _ad_set_getList(self, ident, p: dict) -> httpx.Response:
        return self._page(self.ad_sets, p, filters={"campaignIds": "campaignId"})

    def _ad_getList(self, ident, p: dict) -> httpx.Response:
        return self._page(self.ads, p, filters={"campaignIds": "campaignId", "adSetIds": "adSetId"})

    # ---------------------------------------------------------------- campaigns, ad sets, ads

    def _make_campaign(self, account: str, name: str, objective: str, status: str) -> dict:
        now = next(self.clock)
        campaign = {
            "id": next(self.ids),
            "orgId": self.org_id,
            "adAccountId": account,
            "name": name,
            "objective": objective,
            "budget": 0,
            "onlineStatus": "ACTIVE",
            "status": status,
            "createTime": now,
            "updateTime": now,
        }
        self.campaigns[campaign["id"]] = campaign
        return campaign

    def _campaign_create(self, ident, p: dict) -> httpx.Response:
        if not p.get("adAccountId") or not p.get("name") or not p.get("objective"):
            return invalid("adAccountId, name and objective are required")
        if p["adAccountId"] not in self.accounts:
            return invalid("adAccountId not found")
        if p["objective"] not in OBJECTIVES or p.get("status", "ON") not in ("ON", "OFF"):
            return invalid("objective or status is not one of the listed values")
        return ok(self._make_campaign(p["adAccountId"], p["name"], p["objective"], p.get("status", "ON")))

    def _campaign_update(self, ident, p: dict) -> httpx.Response:
        if ident not in self.campaigns:
            return invalid("campaign not found")
        if not p.get("name"):
            return invalid("name is required")
        self.campaigns[ident].update(name=p["name"], updateTime=next(self.clock))
        return ok(self.campaigns[ident])

    def _status(self, objects: dict[str, dict], ident, p: dict) -> httpx.Response:
        if ident not in objects:
            return invalid("not found")
        if p.get("status") not in ("ON", "OFF"):
            return invalid("status must be ON or OFF")
        objects[ident].update(status=p["status"], updateTime=next(self.clock))
        return ok(objects[ident])

    def _campaign_updateStatus(self, ident, p: dict) -> httpx.Response:
        return self._status(self.campaigns, ident, p)

    def _ad_set_fields(self, p: dict, *, create: bool) -> str | None:
        """Why NewsBreak would refuse these ad set fields, or None."""
        required = ("campaignId", "name", "budgetType", "budget", "startTime", "endTime", "bidType") if create else ()
        missing = [k for k in required if p.get(k) in (None, "")]
        if missing:
            return f"{missing[0]} is required"
        if "budgetType" in p and p["budgetType"] not in ("DAILY", "TOTAL"):
            return "budgetType must be DAILY or TOTAL"
        for k in ("budget", "startTime", "endTime", "bidRate"):
            if k in p and not isinstance(p[k], int):
                return f"{k} must be an integer"
        if "bidType" in p and p["bidType"] not in BID_TYPES:
            return "bidType is not one of the listed values"
        if p.get("bidType") in ("CPM", "CPC", "TARGET_CPA") and not isinstance(p.get("bidRate"), int):
            return "bidRate is required for CPM, CPC and TARGET_CPA"
        if "deliveryRate" in p and p["deliveryRate"] not in ("EVENLY", "ASAP"):
            return "deliveryRate must be EVENLY or ASAP"
        if p.get("status", "ON") not in ("ON", "OFF"):
            return "status must be ON or OFF"
        targeting = p.get("targeting", {})
        if not isinstance(targeting, dict) or not all(
            isinstance(v, dict) and set(v) <= {"positive", "negative"} for v in targeting.values()
        ):
            return "targeting is a map of {positive, negative} lists"
        return None

    @staticmethod
    def _targeting(p: dict) -> dict:
        """The targeting an ad set holds after a create or an update: Create an Ad Set and Update An Ad Set (articles
        37869216635789, 37869314889229) say of `targeting` "If not specified, defaults to "all" (unlimited) for each
        dimension", and promise no merge with what the ad set held -- every dimension the body leaves out is "all"."""
        return {dimension: {"positive": ["all"]} for dimension in TARGETING_DIMENSIONS} | (p.get("targeting") or {})

    def _ad_set_create(self, ident, p: dict) -> httpx.Response:
        refused = self._ad_set_fields(p, create=True)
        if refused:
            return invalid(refused)
        campaign = self.campaigns.get(p["campaignId"])
        if campaign is None:
            return invalid("campaignId not found")
        now = next(self.clock)
        ad_set = {
            **p,
            "id": next(self.ids),
            "orgId": self.org_id,
            "adAccountId": campaign["adAccountId"],
            "status": p.get("status", "ON"),
            "onlineStatus": "ACTIVE",
            "targeting": self._targeting(p),
            "createTime": now,
            "updateTime": now,
        }
        self.ad_sets[ad_set["id"]] = ad_set
        return ok(ad_set)

    def _ad_set_update(self, ident, p: dict) -> httpx.Response:
        """The other fields a body leaves out keep their values; `targeting` does not (`_targeting`)."""
        if ident not in self.ad_sets:
            return invalid("ad set not found")
        refused = self._ad_set_fields(p, create=False)
        if refused:
            return invalid(refused)
        self.ad_sets[ident].update(p, targeting=self._targeting(p), updateTime=next(self.clock))
        return ok(self.ad_sets[ident])

    def _ad_set_updateStatus(self, ident, p: dict) -> httpx.Response:
        return self._status(self.ad_sets, ident, p)

    def _creative(self, creative: object, account: str, *, create: bool) -> str | None:
        """Why NewsBreak would refuse this `creative`, or None."""
        if not isinstance(creative, dict):
            return "creative is required"
        required = ("type", "headline", "assetUrl", "description", "callToAction", "brandName") if create else ()
        missing = [k for k in required if not creative.get(k)]
        if missing:
            return f"creative.{missing[0]} is required"
        if "type" in creative and creative["type"] not in CREATIVE_TYPES:
            return "creative.type is not one of the listed values"
        url = creative.get("assetUrl")
        if url is not None and self.assets.get(url, {}).get("adAccountId") != account:
            return "creative.assetUrl must be an asset uploaded to NewsBreak's CDN for this ad account"
        return None

    def _ad_create(self, ident, p: dict) -> httpx.Response:
        if not p.get("adSetId") or not p.get("name"):
            return invalid("adSetId and name are required")
        ad_set = self.ad_sets.get(p["adSetId"])
        if ad_set is None:
            return invalid("adSetId not found")
        if "newsletterCreative" in p:
            return invalid("this ad set takes creative, not newsletterCreative")
        refused = self._creative(p.get("creative"), ad_set["adAccountId"], create=True)
        if refused:
            return invalid(refused)
        if p.get("status", "ON") not in ("ON", "OFF"):
            return invalid("status must be ON or OFF")
        now = next(self.clock)
        creative = dict(p["creative"])
        ad = {
            "id": next(self.ids),
            "name": p["name"],
            "adAccountId": ad_set["adAccountId"],
            "campaignId": ad_set["campaignId"],
            "adSetId": ad_set["id"],
            "status": p.get("status", "ON"),
            "auditStatus": "PENDING",
            "onlineStatus": "PENDING",
            "statusTxt": UNDER_REVIEW,
            "creative": {"format": "NATIVE", "type": creative.pop("type"), "content": creative},
            "createTime": now,
            "updateTime": now,
        }
        self.ads[ad["id"]] = ad
        return ok(ad)

    def _ad_update(self, ident, p: dict) -> httpx.Response:
        ad = self.ads.get(ident)
        if ad is None:
            return invalid("ad not found")
        if "creative" in p:
            refused = self._creative(p["creative"], ad["adAccountId"], create=False)
            if refused:
                return invalid(refused)
            creative = dict(p["creative"])
            ad["creative"]["type"] = creative.pop("type", ad["creative"]["type"])
            ad["creative"]["content"].update(creative)
            ad.update(auditStatus="PENDING", onlineStatus="PENDING", statusTxt=UNDER_REVIEW)  # a change is reviewed
        if p.get("status", "ON") not in ("ON", "OFF"):
            return invalid("status must be ON or OFF")
        ad.update({k: v for k, v in p.items() if k in ("name", "status")}, updateTime=next(self.clock))
        return ok(ad)

    def _ad_updateStatus(self, ident, p: dict) -> httpx.Response:
        return self._status(self.ads, ident, p)

    # ---------------------------------------------------------------- reports, events, the postback

    def _reports_getIntegratedReport(self, ident, p: dict) -> httpx.Response:
        missing = [k for k in ("name", "timezone", "dateRange", "dimensions", "metrics") if not p.get(k)]
        if missing:
            return invalid(f"{missing[0]} is required")
        if p["dateRange"] not in DATE_RANGES or p.get("dataSource", "HOURLY") not in ("HOURLY", "REALTIME"):
            return invalid("dateRange or dataSource is not one of the listed values")
        if not set(p["dimensions"]) <= DIMENSIONS or not set(p["metrics"]) <= METRICS:
            return invalid("dimensions and metrics are UPPERCASE names from the listed values")
        if p["dateRange"] != "FIXED":
            return invalid("this fake answers FIXED date ranges only")
        if not p.get("startDate") or not p.get("endDate"):
            return invalid("startDate and endDate are required when dateRange=FIXED")
        ids = {str(i) for i in p.get("filterIds") or []}
        rows = [
            r
            for r in self.report_rows
            if p["startDate"] <= r["date"] <= p["endDate"] and (not ids or str(r["adAccountId"]) in ids)
        ]
        return ok({"rows": rows, "aggregateData": []})

    def _event_getList(self, ident, p: dict) -> httpx.Response:
        return ok({"list": self.events.get(ident, [])})

    def _tracking_attribute(self, ident, p: dict) -> httpx.Response:
        """The docs give the postback's failures only as "typically 400" and no body: a plain-text 400 here."""
        if not p.get("callback") or not p.get("event_type"):
            return httpx.Response(400, text="callback and event_type are required")
        if p["callback"] in self.rejected_clicks:
            return httpx.Response(400, text="click id mismatch")
        self.conversions.append(p)
        return httpx.Response(200, json={"code": 0, "errMsg": ""})
