"""Conversion actions and call reporting in the owner's own Google Ads account (spec 2026-09-28
admiry-google-search-execution-design.md §4 "From a plan to Google resources", D43; pages spec
2026-09-28-admiry-pages-design.md §5.3 "Conversions in the owner's account"; A3 Task 2).

Once a business's Google Ads account link becomes active, `ensure` makes sure the account has the ten
`Admiry · <kind>` conversion actions the page's tracking script (and Google's own call tracking) fires, and
turns call reporting on so a forwarding-number call counts as a lead. It then stores the account's own tag
(`AW-<id>`) and every conversion action's label in `conversion_action` (A3p-1's `agent/schema.sql`) — the
table `landing.google_tag` already reads to fill in the page script's `data-gads`/`data-labels` attributes
(`landing.snippet`).

`ensure` is idempotent by name lookup (pages spec §5.3, "created once per account and reused"): a conversion
action already named `Admiry · <kind>` is left alone — its resource name and label are just re-read and
re-stored — so calling it again after a partial failure, or from more than one call site, converges on the
same rows without ever creating a duplicate. One removed by hand in Google Ads is not "already there": the read
skips REMOVED actions, so it is created again (A3 final review I1) rather than a dead one reused.

Google Ads API facts (checked 2026-09-29, current as of client.py's (A3's google_ads.py) own 2026-09-28 check):
  https://developers.google.com/google-ads/api/reference/rpc/v25/ConversionAction (fields, TagSnippet,
  ConversionActionType, ConversionActionCategory)
  https://developers.google.com/google-ads/api/reference/rpc/v25/Customer (conversion_tracking_setting,
  call_reporting_setting)
"""

from __future__ import annotations

import re

import httpx
import psycopg

from ... import config
from .. import registry
from . import client as google_ads

CHANNEL = "google_search"  # whose execution switch gates every call below (M0 ruling 1)

# kind -> (name, type, category, primary_for_goal, extra fields merged into the create body)
KINDS: dict[str, tuple[str, str, str, bool, dict]] = {
    "form": ("Admiry · Form sent", "WEBPAGE", "SUBMIT_LEAD_FORM", True, {}),
    "call_ad": ("Admiry · Call from ad", "AD_CALL", "PHONE_CALL_LEAD", True, {"phoneCallDurationSeconds": 60}),
    "booked": ("Admiry · Booked", "UPLOAD_CLICKS", "BOOK_APPOINTMENT", True, {}),
    "won": ("Admiry · Won", "UPLOAD_CLICKS", "CONVERTED_LEAD", True, {}),
    "call": ("Admiry · Phone tapped", "WEBPAGE", "CONTACT", False, {}),
    "book": ("Admiry · Booking clicked", "WEBPAGE", "BOOK_APPOINTMENT", False, {}),
    "mail": ("Admiry · E-mail tapped", "WEBPAGE", "CONTACT", False, {}),
    # A3p-3 batch 2 (spec 2026-10-03 P25-P27; `WEBSITE_CALL`, `BOOK_APPOINTMENT` and `PHONE_CALL_LEAD` checked in v25's
    # enums, 2026-10-04): an appointment request's own primary (one per form: a consultation's is `form`), a call
    # through Google's forwarding number on the page, and a booking completed on the page (Calendly's message). An
    # account set up with the seven above gets these at its next step (the adapter ensures while a kind is missing)
    "appointment": ("Admiry · Appointment requested", "WEBPAGE", "BOOK_APPOINTMENT", True, {}),
    "call_site": ("Admiry · Call from site", "WEBSITE_CALL", "PHONE_CALL_LEAD", True, {"phoneCallDurationSeconds": 60}),
    "booked_online": ("Admiry · Booked online", "WEBPAGE", "BOOK_APPOINTMENT", True, {}),
}
_KIND_BY_NAME = {info[0]: kind for kind, info in KINDS.items()}

_CUSTOMER_QUERY = (
    "SELECT customer.conversion_tracking_setting.conversion_tracking_id, "
    "customer.call_reporting_setting.call_reporting_enabled, "
    "customer.call_reporting_setting.call_conversion_reporting_enabled, customer.time_zone FROM customer"
)
_ACTIONS_QUERY = (
    "SELECT conversion_action.resource_name, conversion_action.name, conversion_action.type, "
    "conversion_action.tag_snippets FROM conversion_action WHERE conversion_action.name LIKE 'Admiry · %' "
    "AND conversion_action.status != 'REMOVED'"
)
# 'send_to': 'AW-123/AbCdEf' (single or double quotes; Google's own real snippet may wrap onto other lines,
# which this still finds since it never anchors to line boundaries) -> "AbCdEf"
_SEND_TO = re.compile(r"""send_to['"]\s*:\s*['"]AW-\d+/([^'"]+)['"]""")
# a website call's snippet names no send_to: its label is in 'phone_conversion_ids': ['AW-123/AbCdEf'] (A3p-3 P26)
_ANY_LABEL = re.compile(r"""['"]AW-\d+/([^'"]+)['"]""")


def _create_op(kind: str) -> dict:
    name, action_type, category, primary, extra = KINDS[kind]
    return {
        "conversionActionOperation": {
            "create": {
                "name": name,
                "type": action_type,
                "category": category,
                "status": "ENABLED",
                "primaryForGoal": primary,
                "countingType": "ONE_PER_CLICK",
                "valueSettings": {"defaultValue": 0, "alwaysUseDefaultValue": True},
                "clickThroughLookbackWindowDays": 30,
                **extra,
            }
        }
    }


def _call_reporting_op(customer_id: str, call_ad_resource_name: str) -> dict:
    return {
        "customerOperation": {
            "update": {
                "resourceName": f"customers/{customer_id}",
                "callReportingSetting": {
                    "callReportingEnabled": True,
                    "callConversionReportingEnabled": True,
                    "callConversionAction": call_ad_resource_name,
                },
            },
            "updateMask": (
                "callReportingSetting.callReportingEnabled,"
                "callReportingSetting.callConversionReportingEnabled,"
                "callReportingSetting.callConversionAction"
            ),
        }
    }


def _label(tag_snippets: object) -> str | None:
    """The label Admiry's tracking script sends (the part of `send_to` after the account tag) from the
    HTML-page snippet Google generated for a conversion action; None when there is no HTML snippet yet
    (`validate` mode never creates one, so it never reaches this) or its `send_to` cannot be parsed."""
    for snippet in tag_snippets if isinstance(tag_snippets, list) else []:
        if isinstance(snippet, dict) and snippet.get("pageFormat") == "HTML":
            text = snippet.get("eventSnippet") or ""
            match = _SEND_TO.search(text) or _ANY_LABEL.search(text)
            if match:
                return match.group(1)
    return None


def _read_actions(
    settings: config.Settings, customer_id: str, *, http: httpx.Client | None
) -> dict[str, tuple[str, str | None]]:
    """Every existing `Admiry · <kind>` conversion action not removed, by kind: (resource_name, label-or-None)."""
    rows = google_ads.search(settings, customer_id, _ACTIONS_QUERY, http=http)
    out: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        action = row.get("conversionAction")
        if not isinstance(action, dict):
            continue
        kind = _KIND_BY_NAME.get(action.get("name"))
        resource_name = action.get("resourceName")
        if kind and isinstance(resource_name, str):
            out[kind] = (resource_name, _label(action.get("tagSnippets")))
    return out


def _upsert(conn: psycopg.Connection, biz: str, kind: str, resource_name: str, label: str | None) -> None:
    conn.execute(
        "INSERT INTO conversion_action (business_id, kind, resource_name, label) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (business_id, kind) DO UPDATE SET resource_name = EXCLUDED.resource_name, "
        "label = EXCLUDED.label",
        (biz, kind, resource_name, label),
    )


def ensure(
    conn: psycopg.Connection, settings: config.Settings, biz: str, customer_id: str, *, http: httpx.Client | None = None
) -> dict:
    """Make sure `customer_id` (the business's own linked Google Ads account) has all ten `Admiry · <kind>`
    conversion actions and call reporting on, and store its tag and every label `landing.google_tag` reads.
    Runs on `conn` with no transaction of its own — every statement autocommits on its own (callers call this
    outside their row-update transaction: Google is called between transactions, as this module's siblings do).

    Google Search's execution switch (`registry.exec_mode`, M0 ruling 1: AGENT_EXEC_GOOGLE_SEARCH, the old
    AGENT_ADS_EXECUTION standing in while it is unset) gates every Google call: `off` — no Google call is made at
    all, and nothing is stored (`skipped: "off"`); `validate` — the create mutate is `validateOnly` and only the tag
    is stored (nothing was actually created for a re-read to find labels for); `live` — the real thing end to end.

    `ensure` makes 2-5 Google calls of its own (customer; existing actions; the create mutate, when anything
    is missing; a second actions read, to pick up what was just created; the call-reporting update, when it
    was off); when the caller has no open session to hand it (`http=None`), it opens exactly one for all of
    them rather than letting each of `google_ads.search`/`mutate` open and close its own — a caller that
    already has one open (`jobs.ads_link_sync`'s shard loop) passes it through instead.

    Returns `{"tag": "AW-…" | None, "created": [kind, ...], "existing": [kind, ...], "call_reporting": bool,
    "time_zone": "America/Los_Angeles" | None}` -- the account's own time zone, read in the same customer query, for
    the adapter's launch to keep on its row (`resources.time_zone`; ruling S14: Google's `segments.date` is that
    zone's calendar)."""
    if registry.exec_mode(settings, CHANNEL) == "off":
        return {
            "tag": None,
            "created": [],
            "existing": [],
            "call_reporting": False,
            "time_zone": None,
            "skipped": "off",
        }
    if http is not None:
        return _ensure(conn, settings, biz, customer_id, http)
    with google_ads.client() as owned:
        return _ensure(conn, settings, biz, customer_id, owned)


def _ensure(
    conn: psycopg.Connection, settings: config.Settings, biz: str, customer_id: str, http: httpx.Client
) -> dict:
    validate_only = registry.exec_mode(settings, CHANNEL) == "validate"

    customer_rows = google_ads.search(settings, customer_id, _CUSTOMER_QUERY, http=http)
    customer = customer_rows[0].get("customer") if customer_rows else None
    customer = customer if isinstance(customer, dict) else {}
    tracking_id = (customer.get("conversionTrackingSetting") or {}).get("conversionTrackingId")
    tag_id = f"AW-{tracking_id}" if tracking_id else None
    call_setting = customer.get("callReportingSetting") or {}
    reporting_on = bool(call_setting.get("callReportingEnabled")) and bool(
        call_setting.get("callConversionReportingEnabled")
    )

    existing = _read_actions(settings, customer_id, http=http)
    missing = [kind for kind in KINDS if kind not in existing]
    if missing:
        google_ads.mutate(
            settings, customer_id, [_create_op(kind) for kind in missing], validate_only=validate_only, http=http
        )

    zone = customer.get("timeZone")
    result = {
        "tag": tag_id,
        "created": missing,
        "existing": [kind for kind in KINDS if kind not in missing],
        "call_reporting": reporting_on,
        "time_zone": zone if isinstance(zone, str) and zone else None,
    }
    if validate_only:
        if tag_id:
            _upsert(conn, biz, "tag", f"customers/{customer_id}", tag_id)
        return result

    # live: a fresh read picks up tagSnippets for the ones just created (a create response carries no snippet);
    # when nothing was missing, `existing`'s own read already has everything, so no second call is needed.
    final = _read_actions(settings, customer_id, http=http) if missing else existing

    if not reporting_on:
        call_ad = final.get("call_ad")
        if call_ad:
            google_ads.mutate(
                settings, customer_id, [_call_reporting_op(customer_id, call_ad[0])], validate_only=False, http=http
            )
            result["call_reporting"] = True

    for kind, (resource_name, label) in final.items():
        _upsert(conn, biz, kind, resource_name, label)
    if tag_id:
        _upsert(conn, biz, "tag", f"customers/{customer_id}", tag_id)
    return result


def labels(conn: psycopg.Connection, biz: str) -> dict:
    """kind -> label for every `conversion_action` row this business has, plus `'tag'` (`AW-<id>`)."""
    rows = conn.execute("SELECT kind, label FROM conversion_action WHERE business_id = %s", (biz,)).fetchall()
    return {row["kind"]: row["label"] for row in rows}
