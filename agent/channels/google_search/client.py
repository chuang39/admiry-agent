"""The Google Ads REST client (spec 2026-09-28 admiry-google-search-execution-design §3, §13; A3 Task 1): the
access-token exchange, `search` (GAQL, paged) and `mutate` (`validateOnly`, `partialFailure: false` always — partial
failure is never turned on, so a failed operation fails the whole call as an `AdsError`, and `partialFailureError`
is never read), headers, and Google's error bodies mapped to owner-readable sentences that never carry a token, a
developer token, or the raw JSON of Google's own words.

This module began as `google_ads_link.py`'s own private plumbing (the MCC-link adapter); A3 moved it into
`agent/google_ads.py` and M0 moved that file here (the old path is an import shim for one release), so every module
that talks to Google Ads (`conversions.py`, `adapter.py`'s launch, update, pause and sync, `google_ads_link.py`)
shares one client. `google_ads_link.py` keeps thin re-exports of the moved names so its own tests, and `jobs.py`'s
`google_ads_link.client()`, stay green untouched.

Google Ads API facts (checked 2026-09-28, current as of `google_ads_link.py`'s own 2026-09-25 check):
  https://developers.google.com/google-ads/api/rest/reference/rest (lowerCamelCase JSON; `GoogleAdsService.Search`
  paginates with `pageToken`/`nextPageToken` in pages of a fixed 10,000 rows -- `pageSize` is refused since v17
  (`PAGE_SIZE_NOT_SUPPORTED`), so a search body carries only `query` and, after the first page, `pageToken`;
  `GoogleAdsService.Mutate` takes `mutateOperations`)
  https://developers.google.com/google-ads/api/docs/best-practices/quotas (developer tokens retired 2026-09-09:
  access now belongs to the Cloud project that owns the OAuth client; the header is accepted but no longer required)
  https://developers.google.com/google-ads/api/reference/rpc/v25/GeoTargetConstantService (`SuggestGeoTargetConstants`,
  no customer in the path: a shared lookup, not a customer's own resource)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

import httpx

from ... import config, google_auth

log = logging.getLogger(__name__)

API_VERSION = "v25"  # current major version on 2026-09-28 (matches google_ads_link.py's own last check)
BASE_URL = f"https://googleads.googleapis.com/{API_VERSION}"
TIMEOUT_S = 15.0
_MICROS = Decimal(1_000_000)
MAX_PAGES = 100  # 100 x Google's fixed 10,000-row page is far beyond anything this client reads; a repeated or
# endless nextPageToken (a Google hiccup) must not hang a 20s approval-request caller or drain Explorer's daily quota

# Owner sentences (tool errors, `AdsError.owner`; str(e) is exactly one of these, or REFUSED_WITH_REASON.format(code)).
# Google's own error text and every secret (tokens, the developer token) are never repeated to an owner or logged.
ACCESS_TROUBLE = "Admiry's access to Google Ads needs attention; an Admiry operator will fix it, try again later"
BUSY = "Google Ads is busy right now; try again in a few minutes"
UNREACHABLE = "Google Ads is unreachable right now; try again in a few minutes"
REFUSED = "Google Ads refused this request; an Admiry operator will look into it"
REFUSED_WITH_REASON = "Google Ads refused this request ({}); an Admiry operator will look into it"
PAGINATION_RUNAWAY = "pagination: RUNAWAY"  # this client's own code (not Google's) for the two search() guards below


KINDS = ("headline", "description", "keyword", "url", "phone", "budget", "criterion", "ad", "other")


@dataclass(frozen=True)
class RefusalDetail:
    """One thing Google refused in a mutate, as its error body says it (spec 2026-10-11 §4): `code` as `codes`
    carries it; `operation`, the index of the mutate operation the error's field path names; `kind`, what the path
    points at (KINDS: its innermost known element -- a headline, a description, a keyword, a URL, the call asset's
    phone, the budget, another criterion, the ad as a whole, or other); `position`, that headline's or description's
    index in the ad as sent; `field`, the whole path, dotted; `topic` and `type`, the policy topic entry (one detail
    per entry) or the policy violation's name; `exemptible`, a violation Google would exempt on request (only a
    policy violation's entry ever states this; on a policy finding it is always False, meaning "not stated", never
    "no"); `certificate`, a topic whose constraint asks for a certificate or a licence; `text`, the offending text -- the line as it was
    sent (plan D16), else Google's `trigger`, else the policy evidence. Never Google's own message."""

    code: str
    operation: int | None
    kind: str
    position: int | None
    field: str
    topic: str | None
    type: str | None
    exemptible: bool
    certificate: bool
    text: str | None

    def as_dict(self) -> dict:
        return asdict(self)


class AdsError(Exception):
    """Google Ads did not do what was asked. `owner` is the owner-readable sentence (str(e) == owner); `codes` is
    every one of Google's own error enums as "<domain>: <ENUM_VALUE>" (e.g. "campaignError:
    DUPLICATE_CAMPAIGN_NAME") when the failure was a 400 with at least one parseable errorCode, else `()`; `code`
    is a convenience view of `codes[0]` (None when `codes` is empty) for callers that only care about one reason.
    `status` is the HTTP status Google answered with, or None for a transport failure or a client-side guard (no
    response was ever received, or none was needed). `details` (spec 2026-10-11 §4) is one RefusalDetail per error
    and policy topic Google's body carries, for a mutate it refused -- what it points at, the topic, the offending
    text -- and `()` for every other failure; nothing else changes for existing callers."""

    def __init__(
        self,
        owner: str,
        *,
        codes: tuple[str, ...] = (),
        status: int | None = None,
        details: tuple[RefusalDetail, ...] = (),
    ) -> None:
        super().__init__(owner)
        self.owner = owner
        self.codes = codes
        self.code = codes[0] if codes else None
        self.status = status
        self.details = tuple(details)


def client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=5.0))


@contextmanager
def _session(http: httpx.Client | None) -> Iterator[httpx.Client]:
    if http is not None:
        yield http
        return
    with client() as own:
        yield own


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _access_token(http: httpx.Client, settings: config.Settings) -> str:
    """Exchange Admiry's MCC refresh token for an access token (the agent's own OAuth client). Google down
    (transport, 5xx, a body that is not JSON) is UNREACHABLE; a refusal (4xx, no token) is ACCESS_TROUBLE."""
    form = {
        "grant_type": "refresh_token",
        "refresh_token": settings.google_ads_refresh_token or "",
        "client_id": settings.google_client_id or "",
        "client_secret": settings.google_client_secret or "",
    }
    try:
        response = http.post(google_auth.TOKEN_URL, data=form)
        data = response.json()
    except (httpx.HTTPError, ValueError):  # transport, decoding, redirects; or not JSON
        raise AdsError(UNREACHABLE) from None
    if response.status_code >= 500:
        raise AdsError(UNREACHABLE, status=response.status_code)
    token = data.get("access_token") if response.is_success and isinstance(data, dict) else None
    if not token:
        raise AdsError(ACCESS_TROUBLE, status=response.status_code)
    return str(token)


def headers(settings: config.Settings, access: str) -> dict:
    """Authorization, the manager account for `login-customer-id`, and `developer-token` only when one is
    configured (retired by Google on 2026-09-09; access now belongs to the Cloud project, so an unset token means
    nothing was sent rather than an empty header value)."""
    h = {"Authorization": f"Bearer {access}", "login-customer-id": settings.google_ads_mcc_id or ""}
    if settings.google_ads_developer_token:
        h["developer-token"] = settings.google_ads_developer_token
    return h


def _codes(data: dict) -> list[str]:
    """ "<domain>: <ENUM_VALUE>" for every errorCode in the error body's details, in order; empty when there is
    none. `partialFailureError` is never read: partial failure is always off, so a failed operation fails the whole
    call and its reason is here, under `error`. `details` and each one's `errors` are read through `_list` (Ruling C3;
    ruling L3, final review), so a body where either is not a list never raises out of `_failure`."""
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    codes: list[str] = []
    for detail in _list(error.get("details")):
        if not isinstance(detail, dict):
            continue
        for item in _list(detail.get("errors")):
            code = item.get("errorCode") if isinstance(item, dict) else None
            if isinstance(code, dict):
                codes.extend(f"{domain}: {value}" for domain, value in code.items())
    return codes


_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_INDEXED = ("mutate_operations", "operations")  # the element whose index is the operation Google names
# what a path's element says Google points at (innermost first: a keyword inside a criterion operation is a keyword)
# Ruling C1: no blanket `call_asset` marker -- the call asset also carries Admiry's own conversion action and
# reporting state, and a refusal of those is not the owner's phone to fix (see the leaf check in `_details`, which
# promotes `other` to `phone` only when the path stops at the call asset itself and the code names a number problem)
_KIND_OF = (
    ("headlines", "headline"),
    ("descriptions", "description"),
    ("keyword", "keyword"),
    ("final_urls", "url"),
    ("final_url", "url"),
    ("final_mobile_urls", "url"),
    ("final_url_suffix", "url"),
    ("phone_number", "phone"),
    ("country_code", "phone"),  # the call asset's own (it has no other use in a mutate this client sends)
    ("amount_micros", "budget"),
    ("campaign_budget_operation", "budget"),
    ("campaign_criterion_operation", "criterion"),
    ("ad_group_criterion_operation", "criterion"),
    ("ad", "ad"),
)
_NUMBER_LEAF = "call_asset"  # Ruling C1: an error located here (nothing deeper) is `phone` only with a NUMBER code


def _snake(name: object) -> str:
    """A field path element's name as a proto field name: Google writes them snake_case ("responsive_search_ad"); a
    camelCase one (a body re-typed by hand, a future shape) reads the same way."""
    return _CAMEL.sub(r"\1_\2", str(name)).lower()


def _camel(name: str) -> str:
    """The JSON key of a proto field name, as the mutate body was sent (adGroupAdOperation, responsiveSearchAd)."""
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _list(value: object) -> list:
    """`value` as a list to iterate, defensively (Ruling C3): Google's own body is well-formed, but a repeated
    field can still arrive as some other JSON type in a body nobody anticipated; `x.get(k) or []` alone is not
    enough, since `or` only replaces a falsy value, never a truthy non-list one (a lone int, a dict)."""
    return value if isinstance(value, list) else []


def _index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # Ruling C3: str.isdigit() also accepts non-ASCII digit characters (e.g. "²") that int() then refuses --
    # ascii-only first, so a weird index is read as "none given" rather than raising.
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def _elements(item: dict) -> list[tuple[str, int | None]]:
    """The error's field path as (proto name, index) pairs; [] when the body carries none it can read."""
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    out: list[tuple[str, int | None]] = []
    for element in _list(location.get("fieldPathElements")):
        if isinstance(element, dict) and element.get("fieldName"):
            out.append((_snake(element["fieldName"]), _index(element.get("index"))))
    return out


def _path(elements: list[tuple[str, int | None]]) -> str:
    return ".".join(f"{name}[{index}]" if index is not None else name for name, index in elements)


def _operation_index(elements: list[tuple[str, int | None]]) -> int | None:
    return next((index for name, index in elements if name in _INDEXED and index is not None), None)


def _kind_and_position(elements: list[tuple[str, int | None]]) -> tuple[str, int | None]:
    for name, index in reversed(elements):
        for marker, kind in _KIND_OF:
            if name == marker:
                return kind, (index if kind in ("headline", "description") else None)
    return "other", None


_TEXT_ENTRY = ("headlines", "descriptions", "keyword")  # these wrap {"text": …, …}; a sibling of "text" inside the
# same entry (AdTextAsset's own pinned_field, Keyword's own match_type) is never the offending line (D16 fix round 1)


def _sent_text(operations: list[dict] | None, elements: list[tuple[str, int | None]]) -> str | None:
    """The text at the error's field path inside the operation it names, read off the mutate body that was sent --
    the headline, the description, the keyword, the URL as it went out; None when the body is not at hand, the path
    leads nowhere in it, or it does not end in text. Once the path reaches a headline, a description or a keyword
    entry (`_TEXT_ENTRY`), its own "text" is read directly and any further path element is ignored -- a sibling
    field inside that same entry (pinned_field, match_type) must never be read as if it were the offending text."""
    op_index = _operation_index(elements)
    if operations is None or op_index is None or not 0 <= op_index < len(operations):
        return None
    node: object = operations[op_index]
    started = False
    for name, index in elements:
        if not started:
            started = name in _INDEXED
            continue
        key = _camel(name)
        if isinstance(node, dict) and key not in node and len(node) == 1 and next(iter(node)).endswith("Operation"):
            node = next(iter(node.values()))  # a path that names no operation kind: the one there is
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
        if index is not None:
            if not isinstance(node, list) or not 0 <= index < len(node):
                return None
            node = node[index]
        if name in _TEXT_ENTRY:
            break  # whatever the path still names past here is a sibling of "text", not a deeper path to follow
    if isinstance(node, dict):  # headlines[i] / keyword is {"text": …}: a path may stop at, or be cut off at, the entry
        node = node.get("text")
    return node if isinstance(node, str) and node else None


# Google's own doc for policy_topic_entries: these are "the PROHIBITED or FULLY_LIMITED ... entries that prevented
# the resource from being saved (among any other entries the resource may also have)" -- Ruling C2
_BLOCKING_TYPES = ("PROHIBITED", "FULLY_LIMITED")


def _topics(item: dict) -> list[dict]:
    """The policy topic entries of a policy finding, or a policy violation's one entry; [{}] for an error with none
    (one detail per error at least). Ruling C2: when any entry of a policy finding is typed PROHIBITED or
    FULLY_LIMITED, only those entries actually blocked the save -- a plain LIMITED one riding along names nothing
    the owner need act on, and becomes no detail; when none is typed that way (or typed at all), every entry is
    kept, as before."""
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    finding = details.get("policyFindingDetails") if isinstance(details.get("policyFindingDetails"), dict) else {}
    entries = [e for e in _list(finding.get("policyTopicEntries")) if isinstance(e, dict)]
    blocking = [e for e in entries if e.get("type") in _BLOCKING_TYPES]
    entries = blocking or entries
    violation = details.get("policyViolationDetails")
    if isinstance(violation, dict):
        key = violation.get("key") if isinstance(violation.get("key"), dict) else {}
        entries.append(
            {
                "topic": violation.get("externalPolicyName") or key.get("policyName"),
                "exemptible": bool(violation.get("isExemptible")),
                "violating": key.get("violatingText"),
            }
        )
    return entries or [{}]


def _certificate(entry: dict) -> bool:
    return any(
        isinstance(c, dict) and any(str(k).startswith("certificate") for k in c)
        for c in _list(entry.get("constraints"))
    )


def _evidence_text(entry: dict) -> str | None:
    for evidence in _list(entry.get("evidences")):
        text_list = evidence.get("textList") if isinstance(evidence, dict) else None
        texts = text_list.get("texts") if isinstance(text_list, dict) else None
        if isinstance(texts, list) and texts and isinstance(texts[0], str) and texts[0]:
            return texts[0]
    violating = entry.get("violating")
    return violating if isinstance(violating, str) and violating else None


def _details(data: dict, operations: list[dict] | None) -> tuple[RefusalDetail, ...]:
    """Every RefusalDetail the error body carries (spec 2026-10-11 §4; plan D13, D16), in order: one per error and
    policy topic entry, each with what the path points at and the offending text -- the sent line when
    `operations` (the mutate body) is given. Reads what it can and never raises: a body of any shape is a tuple,
    empty when nothing in it is a coded error."""
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    out: list[RefusalDetail] = []
    for detail in _list(error.get("details")):
        if not isinstance(detail, dict):
            continue
        for item in _list(detail.get("errors")):
            if not isinstance(item, dict):
                continue
            code = item.get("errorCode")
            if not isinstance(code, dict) or not code:
                continue
            domain, value = next(iter(code.items()))
            elements = _elements(item)
            kind, position = _kind_and_position(elements)
            if (
                kind == "other"
                and elements
                and elements[-1][0] == _NUMBER_LEAF
                and domain == "assetError"
                and isinstance(value, str)
                and "NUMBER" in value
            ):
                kind = "phone"  # Ruling C1: Google points at the call asset itself only for a number-shaped refusal
            trigger = item.get("trigger") if isinstance(item.get("trigger"), dict) else {}
            said = trigger.get("stringValue")
            sent = _sent_text(operations, elements)
            for entry in _topics(item):
                topic, kind_of = entry.get("topic"), entry.get("type")
                out.append(
                    RefusalDetail(
                        code=f"{domain}: {value}",
                        operation=_operation_index(elements),
                        kind=kind,
                        position=position,
                        field=_path(elements),
                        topic=topic if isinstance(topic, str) and topic else None,
                        type=kind_of if isinstance(kind_of, str) and kind_of else None,
                        exemptible=bool(entry.get("exemptible")),  # a policy finding never states this: False here
                        # means "not stated", not "no" -- only a policy violation's own entry sets it (item 8)
                        certificate=_certificate(entry),
                        text=sent or (said if isinstance(said, str) and said else None) or _evidence_text(entry),
                    )
                )
    return tuple(out)


def _details_safely(data: dict, operations: list[dict] | None) -> tuple[RefusalDetail, ...]:
    """`_details`, with a net under it (Ruling C3): its own sites are guarded against a malformed body, but a
    shape nobody anticipated is still a possibility this client must survive -- a refusal is always an AdsError,
    never an unrelated exception. Anything caught is logged at warning, never the body (it may carry a secret)."""
    try:
        return _details(data, operations)
    except (AttributeError, TypeError, ValueError, KeyError) as e:
        log.warning("google_ads: a refusal body did not parse into details (%s); details=()", type(e).__name__)
        return ()


def _failure(status: int, data: dict, operations: list[dict] | None = None) -> AdsError:
    """An owner sentence for Google's error body; the raw JSON, and any secret Google's own message might carry,
    never reach it. `operations`, the mutate body sent, lets each refusal detail name the line as it went out --
    `details` is read only when `operations` is given (a mutate): a search or a transport failure sent no mutate,
    so there is no sent operation for a detail to belong to, and it carries `details=()` even when Google's body
    is itself coded and shaped like a refusal (Review Focus 1 / Ruling B1). A body `_details` cannot make sense of
    never costs the refusal itself (Ruling C3): the result is still an AdsError, just with `details=()`."""
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    if status in (401, 403):
        return AdsError(ACCESS_TROUBLE, status=status)
    if status == 429 or error.get("status") == "RESOURCE_EXHAUSTED":
        return AdsError(BUSY, status=status)
    if status >= 500:
        return AdsError(UNREACHABLE, status=status)
    codes = _codes(data)
    if codes:
        return AdsError(
            REFUSED_WITH_REASON.format(codes[0]),
            codes=tuple(codes),
            status=status,
            details=_details_safely(data, operations) if operations is not None else (),
        )
    return AdsError(REFUSED, status=status)


def _call(http: httpx.Client, settings: config.Settings, path: str, body: dict) -> dict:
    access = _access_token(http, settings)
    try:
        response = http.post(f"{BASE_URL}/{path}", headers=headers(settings, access), json=body)
    except httpx.HTTPError:
        raise AdsError(UNREACHABLE) from None
    data = _json(response)
    if response.is_success:
        return data
    sent = body.get("mutateOperations")
    raise _failure(response.status_code, data, sent if isinstance(sent, list) else None)


def search(settings: config.Settings, customer_id: str, gaql: str, *, http: httpx.Client | None = None) -> list[dict]:
    """Every result row of a GAQL query against `customer_id`'s own resources (Admiry's manager account acts on
    its behalf through the `login-customer-id` header), across as many pages as Google sends (`nextPageToken`), up
    to MAX_PAGES. The body is the query alone, plus the page token after the first page: Google's pages are a fixed
    10,000 rows and v25 refuses a `pageSize` (A3 final review C1). A token that repeats the one just used, or
    paging that still has not stopped by MAX_PAGES, is AdsError(REFUSED, codes=(PAGINATION_RUNAWAY,)) rather than a
    hang: a caller may be a 20s approval request or an hourly job metered against Explorer's daily operation quota,
    and either way Google is not behaving."""
    rows: list[dict] = []
    page_token: str | None = None
    with _session(http) as h:
        for _ in range(MAX_PAGES):
            body: dict = {"query": gaql}
            if page_token:
                body["pageToken"] = page_token
            data = _call(h, settings, f"customers/{customer_id}/googleAds:search", body)
            results = data.get("results")
            if isinstance(results, list):
                rows.extend(r for r in results if isinstance(r, dict))
            next_token = data.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return rows
            if next_token == page_token:
                raise AdsError(REFUSED, codes=(PAGINATION_RUNAWAY,))
            page_token = next_token
    raise AdsError(REFUSED, codes=(PAGINATION_RUNAWAY,))


def mutate(
    settings: config.Settings,
    customer_id: str,
    operations: list[dict],
    *,
    validate_only: bool,
    http: httpx.Client | None = None,
) -> list[dict]:
    """One `GoogleAdsService.Mutate` call, partial failure always off: a failed operation raises AdsError for the
    whole call, so `mutateOperationResponses` (when returned) can be trusted whole. `validate_only` sends
    `validateOnly: true` — Google checks the operations and builds nothing."""
    log.info(
        "google_ads mutate customer_id=%s operations=%d validate_only=%s", customer_id, len(operations), validate_only
    )  # a count only, at INFO, whether this call goes on to succeed or raise — never the operations themselves
    body = {"mutateOperations": operations, "partialFailure": False, "validateOnly": validate_only}
    with _session(http) as h:
        data = _call(h, settings, f"customers/{customer_id}/googleAds:mutate", body)
    responses = data.get("mutateOperationResponses")
    return responses if isinstance(responses, list) else []


def suggest_geo_targets(settings: config.Settings, names: list[str], *, http: httpx.Client | None = None) -> list[str]:
    """Resource names of Google's geo-target-constant suggestions for these free-text location names (a service
    area's cities, when there is no Places listing to give a proximity circle instead; A3 Task 4). Always the
    `en`/`US` locale — every A3 locale is English/US (spec D42, D52)."""
    body = {"locale": "en", "countryCode": "US", "locationNames": {"names": names}}
    with _session(http) as h:
        data = _call(h, settings, "geoTargetConstants:suggest", body)
    out: list[str] = []
    for item in data.get("geoTargetConstantSuggestions") or []:
        constant = item.get("geoTargetConstant") if isinstance(item, dict) else None
        name = constant.get("resourceName") if isinstance(constant, dict) else None
        if isinstance(name, str):
            out.append(name)
    return out


def temp(kind: str, n: int, customer_id: str) -> str:
    """A temporary resource name for a create operation inside one mutate (Google resolves it once the call
    succeeds); `n` is negative and unique across the call, `kind` the resource's plural REST segment
    (e.g. "campaignBudgets")."""
    return f"customers/{customer_id}/{kind}/{n}"


def to_micros(usd: float) -> int:
    """Whole micros for a dollar amount, rounded to the nearest one (every Google Ads money field is integer
    micros); `str(usd)` first so the rounding sees the decimal digits actually written, not a binary-float
    approximation of them."""
    return int((Decimal(str(usd)) * _MICROS).to_integral_value(rounding=ROUND_HALF_UP))


def from_micros(m: int) -> float:
    return float(Decimal(m) / _MICROS)
