"""From a plan version to NewsBreak Business API bodies (spec 2026-09-29-admiry-channels-design D88-D90, §5.1; plan
2026-09-29-agent-m1-newsbreak P4, P5 and Task 3).

Everything here is a pure function: a plan version -- plus the few values only the adapter has: the ZIP codes' NewsBreak
location ids, the uploaded picture's address on NewsBreak's CDN, the page's address, the name to show -- in, the request
bodies out. No database, no HTTP, no settings. The NewsBreak adapter (`adapter.py`, Task 5) is the only module that sends
them: its `launch` creates a campaign (`campaign_body`), an ad set (`ad_set_body`) and up to `MAX_ADS` ads
(`ad_bodies`), and its `update` applies what `diff` returns. A body is built before the ids it names exist, so each
id parameter takes None and the adapter fills the real one in when it has made the object.

What NewsBreak's own documentation says shapes these bodies (read 2026-10-01; `client.py` lists the articles):
  - Every create's `status` defaults to ON, so every body here says `"OFF"`: a launch creates everything off and the
    adapter switches it on afterwards (Review Focus 1).
  - "Update An Ad Set": `targeting` "defaults to "all" (unlimited) for each dimension" when it is not specified, so a
    budget-only update would take the ZIP codes away and show the ads to everyone. Every ad-set update `diff` returns
    carries the whole targeting (`targeting`), whatever else changed.
  - A campaign's objective is WEB_TRAFFIC ("Website Traffic: CPC-based ... without requiring conversion tracking");
    language values are `en_us` / `es_us`; an ad set's `startTime` / `endTime` are Unix seconds; an ad's `assetUrl` is
    an address on NewsBreak's CDN that includes the ad account (the adapter uploads the picture and passes it in).
  - The copy limits are measured in width: a headline and a description at most 90, the advertiser name 25, the button
    15, a double-width character counting twice. An over-long headline is not refused by the API, it is turned down in
    NewsBreak's review -- so `lint` drops it here.
  - Dynamic copy is `{city}` in the API's own example (NewsBreak's help article writes `{City}`): `lint` accepts both,
    once in a line, counted as written; the ads go out with `{city}`.

`diff` reads two keys of the row's `resources`: `ad_set_id` (the ad set the replacement ads go into) and `ads`
(`{"<slot>": ad id}`, the ads running now). It compares what a plan would show -- the pairs of lines, the button, the
approved picture -- and the budget, the bid and the area, not what NewsBreak holds, so the adapter calls it with the
plan version the campaign was last built from as `old_plan`. What a plan version does not carry is invisible to it: the
page's address (the adapter's `move_page`), the business's name and the picture file's bytes. A change of words replaces
every ad -- there is no per-ad diff -- and the replacements wait for NewsBreak's review (minutes to two work days) while
the old ones, turned off at once, do not serve: whether to hold the offs back until the new ads are approved is the
adapter's call, which is why `diff` lists them last.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ... import landing, plans
from ..google_search import build as google_build
from . import client as newsbreak

CHANNEL = "newsbreak"
HEADLINE_MAX_NB = newsbreak.HEADLINE_MAX  # one source of truth: client.py pins them from NewsBreak's spec page
DESCRIPTION_MAX_NB = newsbreak.DESCRIPTION_MAX
CPC_SHARE = 0.05  # P4: the bid is this share of the plan's average cost per lead...
CPC_MIN_CENTS = 40  # ...between these two (NewsBreak clicks run $0.37-1.61)
CPC_MAX_CENTS = 150
DAILY_MIN_CENTS = 1000  # NewsBreak's $10-a-day minimum for an ad set
MAX_ADS = 3

OBJECTIVE = "WEB_TRAFFIC"
BUDGET_TYPE = "DAILY"
BID_TYPE = "CPC"
DELIVERY_RATE = "EVENLY"
LANGUAGE = "en_us"  # every locale is en-US (D10)
LANGUAGES = ("en_us", "es_us")  # Targeting Values: the two NewsBreak takes
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
CTA_CALL = "Call now"  # P5: the button when the plan counts calls...
CTA_QUOTE = "Get a quote"  # ...and when it does not
SOURCE_PARAM, SOURCE = "src", "nb"
# NewsBreak fills the macro in with the click id the Conversion API needs, under a name of ours: `nb_cid` is the Pixel's
# and reserved (https://adhelp.newsbreak.com/hc/en-us/articles/42302023675917, 42367821562253)
CLICK_ID_PARAM = "newsbreak_cid"
CALLBACK_MACRO = "__CALLBACK_PARAM__"

BRAND_TOO_LONG = (
    "Your business name is too long for NewsBreak's {max}-character advertiser name; tell me a shorter name to show"
)
BRAND_MISSING = "NewsBreak needs your business name to show with your ad; tell me the name to show"
NO_ADS = "the plan has no ad words NewsBreak can take: the running ads would be turned off with nothing to replace them"


class BrandNameError(ValueError):
    """The business name cannot be NewsBreak's advertiser name: `str(e)` is the sentence for the owner."""


# ---------------------------------------------------------------- money


def daily_cents(monthly_usd: float) -> int:
    """The ad set's daily budget from the plan's monthly amount: the month over 30.4 days, floored to the cent (the
    rule Google's `daily_micros` follows too), never below NewsBreak's $10-a-day minimum."""
    return max(DAILY_MIN_CENTS, math.floor(monthly_usd / 30.4 * 100))


def cpc_cents(estimate: dict) -> int:
    """P4: the CPC bid -- `CPC_SHARE` of the plan's average cost per lead, in cents, between `CPC_MIN_CENTS` and
    `CPC_MAX_CENTS`. The cost per lead is the one the plan's estimate carries (Google Search's range, read the way the
    Google build reads it). A plan with none -- NewsBreak alone has no published cost per lead -- bids the ceiling:
    the day's budget is what limits the spend, and the first weeks show what a click really costs."""
    try:
        average = google_build._cpl_avg(estimate)  # the one place the plan's cost per lead is read, Google's own
    except ValueError:
        return CPC_MAX_CENTS
    return min(CPC_MAX_CENTS, max(CPC_MIN_CENTS, plans.half_up(average * CPC_SHARE * 100)))


def campaign_name(business_name: str | None, biz: str) -> str:
    """One campaign per business, named so a lookup by name (a lost create answer, P8) finds exactly it: the
    business id keeps two businesses of one name apart."""
    return f"Admiry · {business_name} · {biz}" if business_name else f"Admiry · {biz}"


# ---------------------------------------------------------------- the copy linter


def width(text: str) -> int:
    """NewsBreak's length: "a double-width character counts twice" (the adapter measures its bodies with it once more
    before any create, M1 Task 5's R3)."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


_CITY = re.compile(r"\{[Cc]ity\}")
_BRACE = re.compile(r"[{}]")


def _dynamic(text: str) -> str:
    """The placeholder as the API's own example spells it."""
    return _CITY.sub("{city}", text)


def _length_reason(text: str, limit: int) -> str | None:
    wide = width(text)
    if wide <= limit:
        return None
    return f"is over {limit} characters" + (" (a wide character counts as two)" if wide > len(text) else "")


def _placeholder_reason(text: str) -> str | None:
    """`{city}` or `{City}` is NewsBreak's dynamic copy, once in a line; any other brace is no placeholder of ours
    and would reach readers as it is."""
    if len(_CITY.findall(text)) > 1:
        return "uses the city placeholder more than once"
    if _BRACE.search(_CITY.sub("", text)):
        return "has a placeholder we do not use"
    return None


def _screen(kind: str, text: str, banned: list[str]) -> str | None:
    """The note that drops one line, or None when it stays: a blank line first (no copy at all: an ad of it would have
    nothing to read, M1 Task 5a), NewsBreak's own rules next (the width, the placeholders), then Google's other copy
    rules -- through `lint_copy`, with NewsBreak's lengths."""
    limit = HEADLINE_MAX_NB if kind == "headline" else DESCRIPTION_MAX_NB
    reason = ("is blank" if not text.strip() else None) or _length_reason(text, limit) or _placeholder_reason(text)
    if reason:
        return f'Dropped a {kind} ("{text}"): it {reason}.'
    if kind == "headline":
        kept, _, notes = google_build.lint_copy([text], [], banned, max_headline=limit)
    else:
        _, kept, notes = google_build.lint_copy([], [text], banned, max_description=limit)
    return None if kept else notes[0]


def lint(headlines: list[str], descriptions: list[str], banned: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Google's copy rules (`google_search.build.lint_copy`: no "!" in a headline, at most one in a description, no
    phone number or e-mail address, no shouted word, none of `banned`) with NewsBreak's lengths -- 90 and 90, a
    double-width character counting twice -- and its dynamic copy: `{city}` or `{City}`, once in a line, counted at
    its written length. Returns the lines that pass, each list in its given order, and one plain-English note per
    dropped line (headlines first)."""
    kept_h: list[str] = []
    kept_d: list[str] = []
    notes: list[str] = []
    for text in headlines:
        note = _screen("headline", text, banned)
        if note is None:
            kept_h.append(text)
        else:
            notes.append(note)
    for text in descriptions:
        note = _screen("description", text, banned)
        if note is None:
            kept_d.append(text)
        else:
            notes.append(note)
    return kept_h, kept_d, notes


def entry(plan: dict) -> dict:
    """The plan's funded NewsBreak row (`sections.ads.channels[]`); ValueError when it has none -- a plan that no
    longer funds NewsBreak is the framework's pause, not something to build."""
    channels = (((plan or {}).get("sections") or {}).get("ads") or {}).get("channels") or []
    found = next((c for c in channels if c.get("channel") == CHANNEL and c.get("monthly_usd")), None)
    if found is None:
        raise ValueError("the plan funds nothing on NewsBreak")
    return found


def ad_copy(plan: dict) -> tuple[list[str], list[str], list[str]]:
    """The plan's NewsBreak ad lines after `lint`, with the plan's own trade's banned list (the Google build reads the
    same recipe): (headlines, descriptions, notes). The row's words may be Google Search's (`copy_of`)."""
    row = entry(plan)
    category = ((plan["sections"].get("consultation") or {}).get("business") or {}).get("category") or "fallback"
    return lint(
        list(row.get("headlines") or []), list(row.get("descriptions") or []), landing.load_recipe(category)["banned"]
    )


def call_to_action(plan: dict) -> str:
    """P5: "Call now" when the plan counts phone calls as results, else "Get a quote" (NewsBreak has no click-to-call:
    either button opens the business's page)."""
    conversions = ((plan.get("sections") or {}).get("consultation") or {}).get("conversions") or []
    return CTA_CALL if any(str(c).startswith("phone_call") for c in conversions) else CTA_QUOTE


# ---------------------------------------------------------------- the name NewsBreak shows

_DANGLING = " ,;:&+-–—/|·"
# the words that join a name to what follows them: never the last word of a cut name (final review, a deferred minor)
_CONNECTIVES = frozenset({"of", "and", "&", "the", "for", "in", "at"})


def brand_name(business_name: str | None) -> str:
    """The advertiser name NewsBreak shows -- the business's name, at most `BRAND_NAME_MAX` wide. A longer name is cut
    after the last whole word that fits, with no ellipsis and no dangling "&" or comma -- nor a word that joins it to
    one cut away ("of", "and", "&", "the", "for", "in", "at", whatever their case: "… Dentistry of" is "… Dentistry");
    a name with no word that fits (or none at all) is a `BrandNameError` whose sentence tells the owner what to give."""
    name = " ".join((business_name or "").split())
    if not name:
        raise BrandNameError(BRAND_MISSING)
    limit = newsbreak.BRAND_NAME_MAX
    if width(name) <= limit:
        return name
    shown = ""
    for word in name.split(" "):
        candidate = f"{shown} {word}" if shown else word
        if width(candidate) > limit:
            break
        shown = candidate
    shown = shown.rstrip(_DANGLING)
    while shown:
        head, _, last = shown.rpartition(" ")
        if last.lower() not in _CONNECTIVES:
            break
        shown = head.rstrip(_DANGLING)
    if not shown:
        raise BrandNameError(BRAND_TOO_LONG.format(max=limit))
    return shown


# ---------------------------------------------------------------- the bodies


def campaign_body(*, name: str, ad_account_id: str | None = None) -> dict:
    """The body of NewsBreak's "Create a Campaign": a website-traffic campaign, created OFF. `ad_account_id` is None
    until the adapter has the business's ad account (it is looked up, or made, at launch)."""
    return {"adAccountId": ad_account_id, "name": name, "objective": OBJECTIVE, "status": "OFF"}


def targeting(location_ids: list[str], language: str = LANGUAGE) -> dict:
    """The ad set's whole targeting: the ZIP codes' location ids and the language named, every other dimension "all".
    Always every dimension -- an update that left one out would reset it to "all" at NewsBreak. ValueError for an empty
    list of ids, "all", or anything that is not a number as text: such a body would reach everyone."""
    if (
        not isinstance(location_ids, (list, tuple))
        or not location_ids
        or not all(isinstance(i, str) and re.fullmatch(r"[0-9]+", i) for i in location_ids)
    ):
        raise ValueError(
            "an ad set needs NewsBreak location ids, each a number as text: "
            'none, "all" or anything else would reach everyone'
        )
    if language not in LANGUAGES:
        raise ValueError(f"the language must be one of {', '.join(LANGUAGES)}")
    named = {"location": list(location_ids), "language": [language]}
    return {dimension: {"positive": named.get(dimension, ["all"])} for dimension in TARGETING_DIMENSIONS}


def ad_set_body(
    *,
    campaign_id: str | None,
    name: str,
    daily_cents: int,
    cpc_cents: int,
    location_ids: list[str],
    language: str,
    start: int,
    end: int,
) -> dict:
    """The body of NewsBreak's "Create an Ad Set": a daily budget, a CPC bid delivered evenly through the day, the ZIP
    codes named, created OFF. `start` and `end` are Unix seconds; `campaign_id` is None until the adapter has made the
    campaign."""
    return {
        "campaignId": campaign_id,
        "name": name,
        "budgetType": BUDGET_TYPE,
        "budget": daily_cents,
        "startTime": start,
        "endTime": end,
        "bidType": BID_TYPE,
        "bidRate": cpc_cents,
        "deliveryRate": DELIVERY_RATE,
        "targeting": targeting(location_ids, language),
        "status": "OFF",
    }


def _pairs(headlines: list[str], descriptions: list[str]) -> list[tuple[str, str]]:
    """The (headline, description) pairs the ads show: each list longest first (equal lengths keep the plan's order),
    zipped, at most `MAX_ADS`."""

    def longest_first(lines: list[str]) -> list[str]:
        return sorted(lines, key=len, reverse=True)

    return list(zip(longest_first(headlines), longest_first(descriptions), strict=False))[:MAX_ADS]


def _ad_name(index: int, creative: dict) -> str:
    """An ad's name, such as `Admiry ad 1 3f9a1c2b`: the slot and a short digest of what the ad shows, so an ad with
    other words is another name -- a lookup by name never mistakes an old ad for its replacement. The ad set it goes into
    is no part of it."""
    digest = hashlib.sha256(json.dumps(creative, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    return f"Admiry ad {index + 1} {digest}"


def ad_bodies(
    *,
    ad_set_id: str | None,
    brand: str | None,
    headlines: list[str],
    descriptions: list[str],
    cta: str,
    asset_url: str,
    logo_url: str | None,
    click_url: str,
) -> list[dict]:
    """The bodies of NewsBreak's "Create an Ad", one per pair of lines: up to `MAX_ADS` image ads, headline i with
    description i with each list longest first, created OFF, all on the same picture, button and page (`click_url`, the
    address `page_url` made). The name shown is `brand_name(brand)` -- BrandNameError when none fits; a button over
    `CALL_TO_ACTION_MAX` is a ValueError (the two this module uses fit). `ad_set_id` is None until the adapter has made
    the ad set."""
    shown = brand_name(brand)
    if width(cta) > newsbreak.CALL_TO_ACTION_MAX:
        raise ValueError(f"the call to action is over {newsbreak.CALL_TO_ACTION_MAX} characters")
    ads = []
    for index, (headline, description) in enumerate(_pairs(headlines, descriptions)):
        creative = {
            "type": "IMAGE",
            "headline": _dynamic(headline),
            "description": _dynamic(description),
            "callToAction": cta,
            "brandName": shown,
            "assetUrl": asset_url,
            "clickThroughUrl": click_url,
        }
        if logo_url:
            creative["logoUrl"] = logo_url
        ads.append({"adSetId": ad_set_id, "name": _ad_name(index, creative), "status": "OFF", "creative": creative})
    return ads


# ---------------------------------------------------------------- the click URL


def page_url(url: str) -> str:
    """The address the ads carry: the business's page with `src=nb` and `newsbreak_cid=__CALLBACK_PARAM__` added to its
    query (NewsBreak replaces the macro with the click id the Conversion API needs, and our page's script stores it),
    each unless it is there already; an existing query and a fragment stay as they are, the fragment last, and the macro
    is never percent-encoded. Idempotent -- the lifecycle compares what the ads carry with this of the page's address."""
    parts = urlsplit(url)
    present = parse_qsl(parts.query, keep_blank_values=True)
    missing = [
        f"{key}={value}"
        for key, value in ((SOURCE_PARAM, SOURCE), (CLICK_ID_PARAM, CALLBACK_MACRO))
        if (key, value) not in present
    ]
    if not missing:
        return parts.geturl()
    return urlunsplit(parts._replace(query="&".join([parts.query, *missing]) if parts.query else "&".join(missing)))


# ---------------------------------------------------------------- diff


def _estimate(plan: dict) -> dict:
    return (plan.get("sections") or {}).get("estimate") or {}


def _area(plan: dict) -> tuple:
    """Where the plan reaches: the radius and the listing it centres on. Equal on two versions, the ad set keeps its
    ZIP codes."""
    geo = ((plan.get("sections") or {}).get("consultation") or {}).get("geo") or {}
    return geo.get("radius_miles"), geo.get("center")


def _shown(plan: dict) -> tuple:
    """What the plan's ads show: the pairs of lines as they go out, the button, the picture the version pinned when it
    was made and approving it approved (`plans.pinned`, M1 Task 7) -- the same file whether or not its approval could
    store it once it left the set (final review I3: the running ads keep it). Two versions that show the same keep
    their ads."""
    headlines, descriptions, _notes = ad_copy(plan)
    pairs = tuple((_dynamic(h), _dynamic(d)) for h, d in _pairs(headlines, descriptions))
    return pairs, call_to_action(plan), plans.pinned(plan.get("sections") or {})


def _running(resources: dict) -> list[str]:
    """The ids of the ads running now, in the order of their slots; a slot with no id is skipped."""
    ads = resources.get("ads") or {}
    slots = sorted(ads, key=lambda slot: (not str(slot).isdigit(), int(slot) if str(slot).isdigit() else 0, str(slot)))
    return [ads[slot] for slot in slots if ads[slot]]


def diff(
    old_plan: dict,
    new_plan: dict,
    resources: dict,
    *,
    location_ids: list[str],
    asset_url: str,
    click_url: str,
    brand: str | None,
    logo_url: str | None = None,
    with_money: bool = True,
) -> list[tuple[str, dict | str]]:
    """What a newer approved plan version changes on the campaign `resources` an earlier launch built, as operations in
    the order to apply them: `("ad_set_update", body)`, `("ad_create", body)`…, `("ad_off", ad id)`….

    The budget, the bid and the area change the ad set in place -- one update, first (a refusal further down never holds
    a budget change back), carrying only the fields that moved and always the whole `targeting` for `location_ids`
    (the ones for the new area; NewsBreak resets an omitted targeting to everyone). `with_money` False leaves the ad
    set to the caller: the adapter's update sends the new version's whole money and area first every time (S16, final
    review I1), whatever moved. Changed words, button or approved picture are new ads -- built like a launch's, with
    `asset_url`, `click_url`, `brand` and `logo_url` -- and every ad in `resources["ads"]` turned off, after the new
    ones are made so a refusal part-way leaves the old ones running. Nothing changed is an empty list. ValueError when
    either version funds nothing on NewsBreak (a dropped channel is the framework's pause), or when the new words leave
    no ad to replace the running ones. NewsBreak reviews a new ad before it serves; whether the old ones wait for that
    is the adapter's to decide."""
    old, new = entry(old_plan), entry(new_plan)
    moved: dict = {}
    if daily_cents(old["monthly_usd"]) != daily_cents(new["monthly_usd"]):
        moved["budget"] = daily_cents(new["monthly_usd"])
    if cpc_cents(_estimate(old_plan)) != cpc_cents(_estimate(new_plan)):
        moved["bidRate"] = cpc_cents(_estimate(new_plan))
    ops: list[tuple[str, dict | str]] = []
    if with_money and (moved or _area(old_plan) != _area(new_plan)):
        ops.append(("ad_set_update", {**moved, "targeting": targeting(location_ids, LANGUAGE)}))
    if _shown(old_plan) != _shown(new_plan):
        headlines, descriptions, _notes = ad_copy(new_plan)
        created = ad_bodies(
            ad_set_id=resources.get("ad_set_id"),
            brand=brand,
            headlines=headlines,
            descriptions=descriptions,
            cta=call_to_action(new_plan),
            asset_url=asset_url,
            logo_url=logo_url,
            click_url=click_url,
        )
        if not created:
            raise ValueError(NO_ADS)
        ops.extend(("ad_create", body) for body in created)
        ops.extend(("ad_off", ad_id) for ad_id in _running(resources))
    return ops
