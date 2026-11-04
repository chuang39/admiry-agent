"""The NewsBreak Business API client (spec 2026-09-29-admiry-channels-design D88-D90, §5.1, §9; plan
2026-09-29-agent-m1-newsbreak Task 2): the `Access-Token` header, NewsBreak's `code`/`errMsg`/`data` envelope, its codes
mapped to owner sentences that never carry the token or NewsBreak's own words (those stay in `NewsBreakError.detail`,
for the audit row, with the token cut out), one log line per call -- `newsbreak <method> <path> code=<code>
status=<status>`, never a body -- and a request meter for `job_run.details.ops_by_channel` (Google's request hook,
copied: two adapters, two meters) that also keeps the run of throttled answers (`throttled_in_a_row`). The client never
retries and never waits: a throttle (code 4034) is BUSY at once, with the code, and the job decides how long to stay
away (two in a row: the rest of the UTC day, M1 Task 6).

NewsBreak facts (read 2026-10-01 from NewsBreak's own help centers -- the Business API's
https://advertising-api.newsbreak.com/hc/en-us and Ads Manager's https://adhelp.newsbreak.com/hc/en-us; Admiry has no
Organization or token yet, so `python -m agent.ops nb-probe` confirms the account facts the day it does, and the pilot
the rest):
  Return Codes (.../articles/37869109014541): 0 success; 403 permission denied, 4031 not login, 4033 invalid access
    token, 4034 rate limit exceeded, -1 unknown error. Other codes exist undocumented (Update an Ad names 41926).
  Rate Limits (.../articles/40262642898701): per developer app, Basic 10 QPS / 600 QPM / 864,000 QPD; a throttled
    call answers 4034; wait 5 minutes after a QPM throttle, until 00:00 UTC after a QPD one.
  Get Ad Accounts (.../articles/37869173867789): GET ad-account/getGroupsByOrgIds?orgIds=..., data.list[] =
    organisations, each with adAccounts[] {id, name, createTime}.
  Create An Ad Account (.../articles/45689340192781): POST ad-account/create {adAccountName (1-256), companyName
    (1-1024), industry ("<category>//<subcategory>" from the industry list), accountCategory (LEADGEN-LOCAL among
    ten), orgId}; data = the account, `status` ACTIVE in the doc's example and `paymentType` PRE_PAID or POST_PAID.
    The article names no manual step; Ads Manager's "Create Ad Accounts" (adhelp .../42301482876173) says more ad
    accounts than an organisation's allowance are asked for by e-mail to adsupport@newsbreak.com, and "Guide to
    Organization-Level Billing" (adhelp .../46386014874125) that an Org Admin allocates organisation credit to each
    account by hand (no API is documented for it).
  Update Account Spending Cap (.../articles/40484091417485): POST balance/updateAccountsBudget {adAccountsBudgetUpdate:
    [{adAccountId, budget (cents, 0..10,000,000,000)}]}; only an Org Admin may; data.list[] = {adAccountId, message},
    code 0 even when an update failed ("Update failed. ..."). The cap is the account's LIFETIME spending cap (Get
    Account Spending Cap, .../articles/40262348828173): a new cap must exceed the account's total spend.
  Get Account Spending Cap (.../articles/40262348828173): POST balance/getAccountBudgetInfo {accountIds}; data.list[] =
    {accountId, accountRemaining, accountSpendingCap, accountTotalSpend (doubles, decimal strings in the example; the
    unit is not named -- cents, as the API's other money), canViewBudget, failMessage}; only for an account with a cap.
  Upload Single Ad Assets (.../articles/37869305274509): POST ad/uploadAssets, multipart: `asset` (the file),
    `adAccountId`; data.assetUrl (on NewsBreak's CDN, carrying the account id: the only URL an ad's creative takes).
  Campaigns, ad sets, ads (.../articles/37869159614477, 37869190998925, 37869170482061, 37869222017549,
    37869216635789, 37869226813965, 37869314889229, 37869247409677, 37869297161741, 37869300764813, 37869332787853,
    37869330859533): <object>/create (POST), <object>/getList (GET, adAccountId + pageNo + pageSize, rows + hasNext),
    <object>/update/{id} (PUT) and <object>/updateStatus/{id} (PUT {status: ON|OFF}); objects are campaign, ad-set, ad.
  Run a Synchronous Report (.../articles/37825594764941): POST reports/getIntegratedReport {name, timezone (required;
    NewsBreak's default PDT), dateRange FIXED + startDate/endDate, filter AD_ACCOUNT + filterIds (integers), dataSource
    HOURLY ("the official basis for income settlement"), dimensions, metrics}; data.rows[] with cost in cents.
  Get Events (.../articles/37869390553101): GET event/getList/{adAccountId}; data.list[]. No API creates an event:
    an event is made in Ads Manager (Tools → Event Management → Create/test web event → Postback) and must pass its
    test there before it saves (adhelp .../46523921313165, .../44376215592589).
  The Conversion API's postback (adhelp .../46523993732365, .../46523921313165, .../48102435000205): GET
    https://business.newsbreak.com/tracking/attribute?callback=<click id>&event_type=<the event's type>[&nb_value=
    <dollars>]; no token; a click id is good for 7 days ("typically 400" after).
"""

from __future__ import annotations

import logging
import math
import mimetypes
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from ... import config

log = logging.getLogger(__name__)

BASE_URL = "https://business.newsbreak.com/business-api/v1"
# from the docs (https://adhelp.newsbreak.com/hc/en-us/articles/46523921313165, read 2026-10-01); confirm at the pilot
# (nb-probe sends no conversion)
TRACKING_URL = "https://business.newsbreak.com/tracking/attribute"
TRACKING_PATH = "tracking/attribute"  # how the log line names it
API_HOST = urlsplit(BASE_URL).hostname  # the postback's host too: the meter counts both
TIMEOUT_S = 15.0
THROTTLED = 4034
# from the docs (https://advertising-api.newsbreak.com/hc/en-us/articles/37869109014541, read 2026-10-01): permission
# denied, not login, invalid access token -- the token's to fix, an operator's job
ACCESS_CODES = frozenset({403, 4031, 4033})
# from the docs (https://advertising-api.newsbreak.com/hc/en-us/articles/37869305274509, read 2026-10-01): the
# multipart fields of ad/uploadAssets (the article's header table says application/json; its example, and a file,
# are multipart/form-data); confirm at the pilot's first upload (nb-probe uploads nothing)
ASSET_FIELD = "asset"
ASSET_ACCOUNT_FIELD = "adAccountId"
# the report's `timezone` is required (https://advertising-api.newsbreak.com/hc/en-us/articles/37825594764941, read
# 2026-10-01; NewsBreak's own default PDT) and `report()` has no default for it: a report's days are its zone's. Admiry
# reports a NewsBreak account in America/Los_Angeles, NewsBreak's own default, kept in `resources.time_zone` (Task 5);
# this is the zone for an account whose zone is unknown -- UTC, as M0's wallet-settle reads an unknown zone
REPORT_TIMEZONE = "UTC"
# ad-account/create's `industry`, by Admiry category (models.Category): values copied verbatim from NewsBreak's industry
# list, the sheet https://docs.google.com/spreadsheets/d/1Mptt6K_xebdzP0kWh5Jourfkciii0OnTK6mUJUSVTKw that "Create An
# Ad Account" points to -- from the docs (https://advertising-api.newsbreak.com/hc/en-us/articles/45689340192781, read
# 2026-10-01); confirm with nb-probe (--category). A category spanning several of the list's subcategories takes its
# group's "Other", an unknown one the list's own catch-all for a small business.
INDUSTRIES = {
    "home_services": "Home and Garden Services//Home Improvement and Repair",
    "attorneys": "Legal Services//Lawyers",
    "finance_insurance": "Finance and Insurance//Other",
    "real_estate": "Real Estate//Other",
    "dentists": "Health and Medical Services//Dental Care",
    "physicians": "Health and Medical Services//General Practitioners",
    "health_fitness": "Fitness Activities//Other",
    "automotive_repair": "Vehicles//Automotive Services",
    "animals_pets": "Pet Ownership//Other",
    "personal_services": "Business and Industrial//Small Business",
    "fallback": "Business and Industrial//Small Business",
}
# The native ad's text limits -- from the docs (NewsBreak's Product Catalog, "Native in-feed ad specs",
# https://docs.google.com/presentation/d/1jKx__G-p8AL-rMfT-yLio9cTp4v3NET7Xa9ahAIMzhc, linked from
# https://adhelp.newsbreak.com/hc/en-us/articles/42301540100109; the 90-character headline also from
# https://adhelp.newsbreak.com/hc/en-us/articles/42374536424205; read 2026-10-01); confirm at the pilot's first ad
# review (nb-probe makes no ad). An over-long headline is turned down in NewsBreak's review, not refused by the API.
HEADLINE_MAX = 90  # fewer than 46 recommended
DESCRIPTION_MAX = 90  # "BODY TEXT"
BRAND_NAME_MAX = 25  # "ADVERTISER NAME": the business's name, which can be longer
CALL_TO_ACTION_MAX = 15
PAGE_SIZE = 500  # the largest page a getList takes (5, 10, 20, 50, 100, 200 or 500)
MAX_PAGES = 20  # 10,000 objects: far beyond one business's account; a hasNext that never ends is NewsBreak misbehaving
PAGINATION_RUNAWAY = "pagination: RUNAWAY"  # this client's own detail (not NewsBreak's) for that guard
DETAIL_MAX = 500  # NewsBreak's errMsg kept for the audit row, at most this many characters

# Owner sentences (str(NewsBreakError) is exactly one of these, or REFUSED_WITH_REASON.format(code)): the owner hears
# "NewsBreak", never an API word; NewsBreak's own text and the token never reach them.
ACCESS_TROUBLE = "Admiry's access to NewsBreak needs attention; an Admiry operator will fix it, try again later"
BUSY = "NewsBreak is busy right now; try again in a few minutes"
UNREACHABLE = "NewsBreak is unreachable right now; try again in a few minutes"
REFUSED = "NewsBreak refused this request; an Admiry operator will look into it"
REFUSED_WITH_REASON = "NewsBreak refused this request (code {}); an Admiry operator will look into it"
# Admiry's own side, before anything is sent: the image file on the box could not be read
ASSET_UNREADABLE = "Admiry could not read the image for your NewsBreak ad; an Admiry operator will look into it"


class NewsBreakError(Exception):
    """NewsBreak did not do what was asked. `owner` is the owner-readable sentence (str(e) == owner); `code` is
    NewsBreak's own code when its envelope carried one (THROTTLED for a throttle), else None; `status` is the HTTP
    status, or None when no answer came (a transport failure, or nothing was sent); `detail` is NewsBreak's own
    `errMsg` (or a refusal's message) for the audit row -- the token cut out, never part of str(e) or repr(e)."""

    def __init__(
        self, owner: str, *, code: int | None = None, status: int | None = None, detail: str | None = None
    ) -> None:
        super().__init__(owner)
        self.owner = owner
        self.code = code
        self.status = status
        self.detail = detail

    @property
    def throttled(self) -> bool:
        """A throttle, NewsBreak's own (code 4034) or one on the way (HTTP 429, which carries no NewsBreak code): the
        one predicate a job counts throttles by."""
        return self.code == THROTTLED or self.status == 429


# ---------------------------------------------------------------- the HTTP client and its meter


class _Meter:
    """Requests a client sent to NewsBreak's host (spec §9: NewsBreak's limits count requests per developer app) --
    counted from the requests themselves, a throttled or refused one included -- and the throttled answers in a row it
    got (`throttled`; M1 Task 6: two in a row stop the jobs' NewsBreak work for the rest of the UTC day). Only counts:
    never keeps a body."""

    def __init__(self) -> None:
        self.ops = 0
        self.throttled = 0

    def __call__(self, request: httpx.Request) -> None:
        if request.url.host == API_HOST:
            self.ops += 1


def client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The NewsBreak client, metered (`ops_sent`, `throttled_in_a_row`): 15 s a request, 5 s to connect. `transport` is
    for tests."""
    return httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_S, connect=5.0), transport=transport, event_hooks={"request": [_Meter()]}
    )


def _meters(http: httpx.Client) -> list[_Meter]:
    return [hook for hook in http.event_hooks["request"] if isinstance(hook, _Meter)]


def ops_sent(http: httpx.Client) -> int:
    """The requests `http` has sent to NewsBreak so far, when `client()` made it; 0 for any other client."""
    return sum(meter.ops for meter in _meters(http))


def throttled_in_a_row(http: httpx.Client) -> int:
    """NewsBreak's throttled answers in a row on `http`, the last ones it got (`NewsBreakError.throttled`: a 4034, or a
    429 -- the postback's too): any other answer starts it again from zero, and a request that got no answer leaves it
    as it was. 0 for a client `client()` did not make. The jobs stop NewsBreak's rows for the day at two (T6-4)."""
    return sum(meter.throttled for meter in _meters(http))


def _answered(http: httpx.Client, error: NewsBreakError | None) -> None:
    """One answer NewsBreak gave on `http` (None: a success), kept in its run of throttled answers."""
    for meter in _meters(http):
        meter.throttled = meter.throttled + 1 if error is not None and error.throttled else 0


@contextmanager
def _session(http: httpx.Client | None) -> Iterator[httpx.Client]:
    if http is not None:
        yield http
        return
    with client() as own:
        yield own


# ---------------------------------------------------------------- one call: the header, the envelope, the errors


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _code(body: dict) -> int | None:
    code = body.get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def scrub(settings: config.Settings, text: object) -> str | None:
    """NewsBreak's own words as Admiry keeps them -- an error's for the audit row, an ad review's for the owner's card
    (the adapter's `channel_issue` rows): the token cut out (words that echo it must not carry it on), at most
    DETAIL_MAX characters; None for no words."""
    if not isinstance(text, str) or not text:
        return None
    if settings.newsbreak_access_token:
        text = text.replace(settings.newsbreak_access_token, "[token]")
    return text[:DETAIL_MAX]


def _log(method: str, path: str, code: int | None, status: int | None, *, ok: bool) -> None:
    log.log(logging.INFO if ok else logging.WARNING, "newsbreak %s %s code=%s status=%s", method, path, code, status)


def _failure(status: int, code: int | None, detail: str | None, *, envelope: bool) -> NewsBreakError | None:
    """The error an answer is, or None for a success. NewsBreak down or not answering in its envelope (a 5xx; a 2xx
    that is not the envelope, such as a proxy's page) is UNREACHABLE; a throttle (4034, or a 429 on the way) BUSY; the
    token's trouble (HTTP 401/403, codes 403/4031/4033) ACCESS_TROUBLE; any other code REFUSED_WITH_REASON; any other
    non-2xx REFUSED. `envelope` False (the postback, whose answer the docs do not describe) lets a 2xx without
    NewsBreak's envelope succeed."""
    if status >= 500:
        return NewsBreakError(UNREACHABLE, code=code, status=status, detail=detail)
    if code == THROTTLED or status == 429:
        return NewsBreakError(BUSY, code=code, status=status, detail=detail)
    if code in ACCESS_CODES or status in (401, 403):
        return NewsBreakError(ACCESS_TROUBLE, code=code, status=status, detail=detail)
    if code:
        return NewsBreakError(REFUSED_WITH_REASON.format(code), code=code, status=status, detail=detail)
    if not 200 <= status < 300:
        return NewsBreakError(REFUSED, code=code, status=status, detail=detail)
    if code is None and envelope:
        return NewsBreakError(UNREACHABLE, status=status)
    return None


def _answer(
    http: httpx.Client,
    settings: config.Settings,
    method: str,
    path: str,
    response: httpx.Response,
    *,
    envelope: bool = True,
) -> dict:
    """NewsBreak's answer on `http`: its `data`, or the NewsBreakError it is -- counted in `http`'s run of throttled
    answers either way (`throttled_in_a_row`)."""
    body = _json(response)
    code = _code(body)
    error = _failure(response.status_code, code, scrub(settings, body.get("errMsg")), envelope=envelope)
    _log(method, path, code, response.status_code, ok=error is None)
    _answered(http, error)
    if error is not None:
        raise error
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _send(http: httpx.Client, settings: config.Settings, method: str, path: str, **kwargs) -> dict:
    """One Business API call (`kwargs`: httpx's json, params, files, data). Without a token nothing is sent."""
    token = settings.newsbreak_access_token
    if not token:
        _log(method, path, None, None, ok=False)
        raise NewsBreakError(ACCESS_TROUBLE, detail="NEWSBREAK_ACCESS_TOKEN is not set")
    try:
        response = http.request(method, f"{BASE_URL}/{path}", headers={"Access-Token": token}, **kwargs)
    except httpx.HTTPError:  # transport, timeouts, a malformed URL
        _log(method, path, None, None, ok=False)
        raise NewsBreakError(UNREACHABLE) from None
    return _answer(http, settings, method, path, response)


def call(
    settings: config.Settings,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    http: httpx.Client | None = None,
) -> dict:
    """One Business API call (`path` under BASE_URL): the envelope's `data` ({} when it is not an object), or
    NewsBreakError."""
    with _session(http) as h:
        return _send(h, settings, method, path, json=json, params=params)


def _id(value: object) -> str:
    """An id as one path segment: NewsBreak's ids are numeric strings, and a stored value never reaches another
    endpoint by carrying a slash."""
    return quote(str(value), safe="")


def _pages(settings: config.Settings, path: str, params: dict, *, http: httpx.Client | None) -> list[dict]:
    """Every row of a getList, page after page while NewsBreak says `hasNext`, up to MAX_PAGES."""
    rows: list[dict] = []
    with _session(http) as h:
        for page in range(1, MAX_PAGES + 1):
            data = _send(h, settings, "GET", path, params={**params, "pageNo": page, "pageSize": PAGE_SIZE})
            batch = data.get("rows")
            batch = [r for r in batch if isinstance(r, dict)] if isinstance(batch, list) else []
            rows.extend(batch)
            if not batch or data.get("hasNext") is not True:
                return rows
    raise NewsBreakError(REFUSED, detail=PAGINATION_RUNAWAY)


# ---------------------------------------------------------------- ad accounts and the spending cap


def industry(category: str | None) -> str:
    """The `industry` ad-account/create takes for a business of this Admiry category (INDUSTRIES; any other: the
    fallback's)."""
    return INDUSTRIES.get(category or "", INDUSTRIES["fallback"])


def ad_accounts(settings: config.Settings, org_id: str, *, http: httpx.Client | None = None) -> list[dict]:
    """The organisation's ad accounts ({id, name, createTime}); none when the token cannot see that organisation."""
    data = call(settings, "GET", "ad-account/getGroupsByOrgIds", params={"orgIds": [org_id]}, http=http)
    for org in data.get("list") or []:
        if isinstance(org, dict) and str(org.get("id")) == str(org_id):
            return [a for a in org.get("adAccounts") or [] if isinstance(a, dict)]
    return []


def create_ad_account(
    settings: config.Settings,
    *,
    org_id: str,
    name: str,
    company_name: str,
    industry: str,
    category: str = "LEADGEN-LOCAL",
    http: httpx.Client | None = None,
) -> dict:
    """A new ad account under the organisation (NewsBreak's answer: id, status, paymentType, ...). NewsBreak cannot
    delete an ad account: look one up by name (`ad_accounts`) before making it."""
    body = {
        "adAccountName": name,
        "companyName": company_name,
        "industry": industry,
        "accountCategory": category,
        "orgId": org_id,
    }
    return call(settings, "POST", "ad-account/create", json=body, http=http)


def set_spending_cap(
    settings: config.Settings, ad_account_id: str, cap_cents: int, *, http: httpx.Client | None = None
) -> str:
    """Set the account's spending cap (NewsBreak's own ceiling, D88; a LIFETIME cap that must stay above the
    account's total spend) and return NewsBreak's message. NewsBreak answers code 0 whether the update took or not,
    so anything but its success message ("... successfully ...") raises, with the message as the detail: a cap that did
    not take must never pass for one that did. "No access to change adAccount budget" (the token is not an Org
    Admin's) is ACCESS_TROUBLE, the token's trouble like code 403 -- once an operator fixes it, the same request goes
    through; every other failure is REFUSED."""
    body = {"adAccountsBudgetUpdate": [{"adAccountId": ad_account_id, "budget": cap_cents}]}
    data = call(settings, "POST", "balance/updateAccountsBudget", json=body, http=http)
    for row in data.get("list") or []:
        if isinstance(row, dict) and str(row.get("adAccountId")) == str(ad_account_id):
            message = str(row.get("message") or "")
            if "successfully" in message:
                return message
            owner = ACCESS_TROUBLE if "No access" in message else REFUSED
            raise NewsBreakError(owner, detail=scrub(settings, message) or "no message")
    raise NewsBreakError(REFUSED, detail="no result for the ad account")


# from the docs (https://advertising-api.newsbreak.com/hc/en-us/articles/40262348828173, read 2026-10-01): the answer for
# an account without a spending cap; confirm at the pilot (nb-probe reads no budget)
NO_CAP_SET = "No account spending cap is set"


def total_spend(settings: config.Settings, ad_account_id: str, *, http: httpx.Client | None = None) -> int | None:
    """What the account has spent so far, in cents rounded up (Get Account Spending Cap's `accountTotalSpend`: the
    figure a new lifetime cap must exceed), or None while the account has no cap -- NewsBreak shows the figures only
    for an account with one. A token that may not see them ("No access") is ACCESS_TROUBLE, like the cap's; any other
    refusal, or an answer without a figure, REFUSED. The article names no unit: read as cents, the unit of the cap
    and the report's cost -- confirm at the pilot (nb-probe reads no budget)."""
    data = call(settings, "POST", "balance/getAccountBudgetInfo", json={"accountIds": [ad_account_id]}, http=http)
    for row in data.get("list") or []:
        if not isinstance(row, dict) or str(row.get("accountId")) != str(ad_account_id):
            continue
        if row.get("canViewBudget") is not True:
            message = str(row.get("failMessage") or "")
            if NO_CAP_SET in message:
                return None
            owner = ACCESS_TROUBLE if "No access" in message else REFUSED
            raise NewsBreakError(owner, detail=scrub(settings, message) or "no message")
        try:
            return math.ceil(Decimal(str(row.get("accountTotalSpend"))))
        except (ArithmeticError, ValueError):  # decimal.InvalidOperation is an ArithmeticError
            raise NewsBreakError(REFUSED, detail="accountTotalSpend is not a number") from None
    raise NewsBreakError(REFUSED, detail="no budget figures for the ad account")


# ---------------------------------------------------------------- the asset upload


def upload_asset(
    settings: config.Settings, ad_account_id: str, file_path: Path, *, http: httpx.Client | None = None
) -> str:
    """Upload one image to NewsBreak's CDN for this ad account and return its URL (an ad's creative takes no other):
    a multipart form, the file as ASSET_FIELD and the account as ASSET_ACCOUNT_FIELD. A file that cannot be read is
    ASSET_UNREADABLE before anything is sent; the detail names the error, never the path."""
    path = Path(file_path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        content = path.read_bytes()
    except OSError as e:
        raise NewsBreakError(ASSET_UNREADABLE, detail=f"{type(e).__name__}: {e.strerror}") from None
    files = {ASSET_FIELD: (path.name, content, mime)}
    with _session(http) as h:
        data = _send(h, settings, "POST", "ad/uploadAssets", files=files, data={ASSET_ACCOUNT_FIELD: ad_account_id})
    url = data.get("assetUrl")
    if not isinstance(url, str) or not url:
        raise NewsBreakError(REFUSED, detail="no assetUrl in NewsBreak's answer")
    return url


# ---------------------------------------------------------------- campaigns, ad sets, ads


def _on_off(on: bool) -> dict:
    return {"status": "ON" if on else "OFF"}


def campaigns(settings: config.Settings, ad_account_id: str, *, http: httpx.Client | None = None) -> list[dict]:
    return _pages(settings, "campaign/getList", {"adAccountId": ad_account_id}, http=http)


def create_campaign(settings: config.Settings, body: dict, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "POST", "campaign/create", json=body, http=http)


def update_campaign(
    settings: config.Settings, campaign_id: str, body: dict, *, http: httpx.Client | None = None
) -> dict:
    return call(settings, "PUT", f"campaign/update/{_id(campaign_id)}", json=body, http=http)


def campaign_status(settings: config.Settings, campaign_id: str, on: bool, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "PUT", f"campaign/updateStatus/{_id(campaign_id)}", json=_on_off(on), http=http)


def ad_sets(settings: config.Settings, ad_account_id: str, *, http: httpx.Client | None = None) -> list[dict]:
    return _pages(settings, "ad-set/getList", {"adAccountId": ad_account_id}, http=http)


def create_ad_set(settings: config.Settings, body: dict, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "POST", "ad-set/create", json=body, http=http)


def update_ad_set(settings: config.Settings, ad_set_id: str, body: dict, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "PUT", f"ad-set/update/{_id(ad_set_id)}", json=body, http=http)


def ad_set_status(settings: config.Settings, ad_set_id: str, on: bool, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "PUT", f"ad-set/updateStatus/{_id(ad_set_id)}", json=_on_off(on), http=http)


def ads(settings: config.Settings, ad_account_id: str, *, http: httpx.Client | None = None) -> list[dict]:
    return _pages(settings, "ad/getList", {"adAccountId": ad_account_id}, http=http)


def create_ad(settings: config.Settings, body: dict, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "POST", "ad/create", json=body, http=http)


def update_ad(settings: config.Settings, ad_id: str, body: dict, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "PUT", f"ad/update/{_id(ad_id)}", json=body, http=http)


def ad_status(settings: config.Settings, ad_id: str, on: bool, *, http: httpx.Client | None = None) -> dict:
    return call(settings, "PUT", f"ad/updateStatus/{_id(ad_id)}", json=_on_off(on), http=http)


# ---------------------------------------------------------------- the report and the events


def report(
    settings: config.Settings,
    ad_account_id: str,
    *,
    start: date,
    end: date,
    timezone: str,
    http: httpx.Client | None = None,
) -> list[dict]:
    """The account's figures by day and ad, `start` to `end` (FIXED dates, the days of `timezone`), from the HOURLY
    source -- NewsBreak's settlement basis, never REALTIME: the rows as NewsBreak returned them (`cost` in cents).
    `timezone` has no default -- a report's days are its zone's, and a forgotten zone would settle and reconcile the
    wrong days without an error: the caller passes the account's zone (`resources.time_zone`, America/Los_Angeles for
    NewsBreak's accounts, Task 5), or REPORT_TIMEZONE when it is unknown. An ad account id that is not a number
    (NewsBreak's `filterIds` are integers) is REFUSED before anything is sent."""
    try:
        account = int(ad_account_id)
    except (TypeError, ValueError):  # "act_1", or None
        raise NewsBreakError(REFUSED, detail="the ad account id is not a number") from None
    body = {
        "name": f"admiry {ad_account_id} {start.isoformat()}..{end.isoformat()}",
        "timezone": timezone,
        "dateRange": "FIXED",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "filter": "AD_ACCOUNT",
        "filterIds": [account],
        "dataSource": "HOURLY",
        "dimensions": ["DATE", "AD"],
        "metrics": ["COST", "IMPRESSION", "CLICK", "CONVERSION"],
    }
    data = call(settings, "POST", "reports/getIntegratedReport", json=body, http=http)
    rows = data.get("rows")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def events(settings: config.Settings, ad_account_id: str, *, http: httpx.Client | None = None) -> list[dict]:
    """The account's tracking events ({id, name, type PIXEL|POSTBACK, eventType, ...}): made by hand in Ads Manager,
    since NewsBreak documents no API that creates one."""
    data = call(settings, "GET", f"event/getList/{_id(ad_account_id)}", http=http)
    rows = data.get("list")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


# ---------------------------------------------------------------- the Conversion API

# The event Admiry's leads are sent back as (M1 Task 5, R10): NewsBreak has no API that creates one, so an operator makes
# it by hand in each ad account's Event Management under exactly this name -- the docs' own form example
# (https://adhelp.newsbreak.com/hc/en-us/articles/46523921313165, read 2026-10-01) -- and the postback's `event_type`
# must match it exactly
ADMIRY_LEAD_EVENT = "submit_form"


def _dollars(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def send_conversion(
    settings: config.Settings,
    *,
    click_id: str,
    event_type: str,
    value_cents: int = 0,
    http: httpx.Client | None = None,
) -> None:
    """One conversion back to NewsBreak by its click id: the postback (`callback` = the click id exactly as received,
    `event_type` = the type of the event made in the ad account's Event Management, case-sensitive), `nb_value` in
    dollars only when there is a value (the docs ask for it only when the event records one). No identifier of the
    person (D84) and no token: the postback is not a Business API call. A refusal (an expired click id is "typically
    400") is NewsBreakError like any other."""
    params = {"callback": click_id, "event_type": event_type}
    if value_cents:
        params["nb_value"] = _dollars(value_cents)
    with _session(http) as h:
        try:
            response = h.get(TRACKING_URL, params=params)
        except httpx.HTTPError:
            _log("GET", TRACKING_PATH, None, None, ok=False)
            raise NewsBreakError(UNREACHABLE) from None
        _answer(h, settings, "GET", TRACKING_PATH, response, envelope=False)
