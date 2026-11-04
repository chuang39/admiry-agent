"""From a plan version to Google Ads v25 operations (spec 2026-09-28 admiry-google-search-execution-design.md
§4 "From a plan to Google resources", §7 "The owner approves what runs"; A3 Task 3).

Everything here is a pure function: a plan version (and, for `operations`, the knowledge behind it) in,
Google Ads REST mutate operations out. No database, no HTTP, no settings -- the Google Search adapter (`adapter.py`
beside this module since M0; A3's `agent/ads_run.py` before it) is the only module that sends them for a business's
ads (the operator's `agent.ops ads-probe` only has Google check a probe campaign built here); it resolves
`Target`/`Account`/`page_url` and passes them in here.

D48: one Search campaign per business, named `Admiry · <business> · <biz id>` (a lookup by that name recovers
a lost mutate response before any re-create -- the adapter's `launch` does it); one ad group per plan theme,
at most `MAX_AD_GROUPS`; Google Search network only, people **in** the area (`PRESENCE`), English, all hours.
D49/D50: every launch is one `Mutate` with temporary resource names, created `PAUSED` (`operations`); a second
call enables the campaign (`enable`). D51: Maximize Clicks with a CPC ceiling from the plan's own cost-per-lead
assumption (`cpc_ceiling_micros`, P9). D52: every string an owner (or their assistant) reads stays free of
"campaign", "ad group", "keyword", "CPC" -- the `JARGON` test.

`resources_from` turns one call's `operations` + Google's `mutateOperationResponses` into the durable shape
`diff` (P10) and `channel_campaign.resources` need: ad groups, ads and their keywords keyed by
their *theme* (the one thing that still means something once the mutate that created them is long over),
negative keywords by their text, geo criteria as a list, everything else by its single kind. `diff` builds
the next mutate for a newer approved plan version already running (spec §5 "Later versions"): the budget and
CPC ceiling (paired; `money`); the geo criteria replaced when the area moved; negatives added/removed by text;
ad groups for themes added or removed (a theme that comes back re-enables its paused group, ruling S16); new
keywords added (never removed -- see `diff`'s docstring) to a kept theme's ad group; and a fresh ad (the old
one paused) when the copy changed.

Every keyword and negative is a search phrase Google takes (`_phrase`): the characters it refuses are dropped,
spaces collapsed, lowercase, and a phrase still over 80 characters or 10 words is left out -- one bad phrase from
the model never fails a launch.
"""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass
from itertools import count

from ... import landing, local_rules, plans
from . import client as google_ads

MAX_AD_GROUPS = 5
MAX_KEYWORDS_PER_AD_GROUP = 20
LANGUAGE_CONSTANT = "languageConstants/1000"  # English; every A3 locale is en-US (D10)
NO_EU_POLITICAL = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"


@dataclass(frozen=True)
class Account:
    customer_id: str
    tag: str | None
    conversion_actions: dict[str, str]  # kind -> resource_name (conversions.py KINDS)


@dataclass(frozen=True)
class Target:
    kind: str  # "proximity" | "locations"
    lat: float | None
    lng: float | None
    radius_miles: float | None
    geo_targets: tuple[str, ...]  # "geoTargetConstants/<id>", already resolved (geoTargetConstants:suggest)


@dataclass(frozen=True)
class Dropped:
    """What Admiry leaves out of every Google build of a business without editing any version (spec 2026-10-11 §5
    "How a drop is kept"; plan D3, ruling L2): the headlines, descriptions and search phrases Google refused and Admiry
    dropped, whichever version they were refused in (agent/ads_refusal.py), each exactly as it was sent -- and, since
    Part B phase 1 (spec 2026-10-12 §2.3, plan D3), the active `block` rules (`rules`, local_rules.Rule; attached by
    ads_refusal.dropped when given the settings, none while AGENT_REFUSAL_RULES is off): a line one of them matches is
    left out too, before any build, exactly as a refused line is. `holds` is the one question the build asks. Falsy
    when nothing is dropped and no rule is on."""

    headlines: frozenset[str] = frozenset()
    descriptions: frozenset[str] = frozenset()
    keywords: frozenset[str] = frozenset()
    rules: tuple[local_rules.Rule, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.headlines or self.descriptions or self.keywords or self.rules)

    def holds(self, kind: str, text: str) -> bool:
        """Whether a build leaves `text` (a `headline`, `description` or `keyword`) out: refused by its exact text,
        or caught by an active block rule (a warn rule never holds a line: it is the checklist's)."""
        exact = {"headline": self.headlines, "description": self.descriptions, "keyword": self.keywords}[kind]
        return text in exact or any(
            r.level == local_rules.BLOCK and local_rules.matches(r, kind, text) for r in self.rules
        )


NOTHING_DROPPED = Dropped()


# ---------------------------------------------------------------- money


def daily_micros(monthly_usd: float) -> int:
    """Google's own daily budget from the plan's monthly figure: floor to the cent (a fraction of a cent has
    no meaning to Google, which only ever takes whole micros), 30.4 the average days in a month."""
    return math.floor(monthly_usd / 30.4 * 100) * 10_000


_CPL_RANGE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*[–-]\s*\$?([\d,]+(?:\.\d+)?)")


def _cpl_avg(estimate: dict) -> float:
    """The Google Search channel's average cost per lead, read from the plan's own estimate (never
    re-derived from the benchmark table: this module has no database connection, and the plan already
    carries this). `plans.estimate_columns` -- what ends up in `plan["sections"]["estimate"]` -- keeps it only
    as the "cost per lead" assumption's formatted range (`"$51-109"`, `cpl_avg * (0.7, 1.5)`, `plans.py`'s
    `LOW_FACTOR`/`HIGH_FACTOR`): parse that range and take its midpoint. Falls back to a bare `cpl_avg` number
    or a `cpl_low`/`cpl_high` pair when the caller hands one of those shapes instead (a lighter-weight
    `estimate` a test, or a future caller, builds directly)."""
    for a in estimate.get("assumptions") or []:
        if a.get("name") == "cost per lead":
            m = _CPL_RANGE.search(a.get("value") or "")
            if m:
                lo, hi = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
                return (lo + hi) / 2
    if estimate.get("cpl_avg") is not None:
        return float(estimate["cpl_avg"])
    lo, hi = estimate.get("cpl_low"), estimate.get("cpl_high")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2
    raise ValueError("estimate has no cost-per-lead assumption to set a CPC ceiling from")


def cpc_ceiling_micros(estimate: dict) -> int:
    """P9/D51: Maximize Clicks' `target_spend.cpc_bid_ceiling_micros` -- 15% of the average cost per lead,
    never below $2 or above $20 (tuned on the pilot)."""
    ceiling_usd = min(20.0, max(2.0, _cpl_avg(estimate) * 0.15))
    return google_ads.to_micros(ceiling_usd)


def campaign_name(business_name: str | None, biz: str) -> str:
    """D48: one Search campaign per business; the business id makes the name unique even when two
    businesses share the same name, so a lookup by name (idempotency: a lost mutate response) is exact."""
    return f"Admiry · {business_name} · {biz}" if business_name else f"Admiry · {biz}"


# ---------------------------------------------------------------- the copy linter (spec §7)

_PHONE_RE = re.compile(r"(\(?\d{3}\)?[-.\s]\d{3}[-.\s]?\d{4})|\b\d{3}-\d{4}\b|\b\d{7,}\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _shouts(text: str) -> bool:
    """An ALL-CAPS word of 5 or more letters -- Google ad policy's "excessive capitalization" (P11: up to 4
    letters passes as a plausible acronym -- SEO, HVAC, DDS, LLC -- with no allowlist to maintain; Review
    round 1 S7 replaces the earlier fixed-acronym-list version of this rule with P11's own threshold)."""
    return any(len(w) >= 5 and w.upper() == w for w in re.findall(r"[A-Za-z]+", text))


def _banned_hit(text: str, banned: list[str]) -> str | None:
    """The first of `banned`'s words found as a whole word in `text` (case-insensitive), else None.
    `(?<!\\w)…(?!\\w)`, not `\\b…\\b`: `\\b` cannot sit before a symbol, so "#1" is never caught by it
    (landing.py's `check` hit the same thing first)."""
    low = text.lower()
    for word in banned:
        if re.search(r"(?<!\w)" + re.escape(word.lower()) + r"(?!\w)", low):
            return word
    return None


def _reason(text: str, *, max_len: int, max_bangs: int, banned: list[str]) -> str | None:
    """None when `text` passes every rule; else a plain, owner-readable reason it was dropped -- never
    "campaign", "ad group", "keyword" or "CPC" (D52, the `JARGON` check: owners and their assistants read
    these notes on the plan card)."""
    if len(text) > max_len:
        return f"is over {max_len} characters"
    hit = _banned_hit(text, banned)
    if hit:
        return f'uses a word we avoid ("{hit}")'
    if text.count("!") > max_bangs:
        return "has an exclamation mark" if max_bangs == 0 else "has more than one exclamation mark"
    if _PHONE_RE.search(text) or _EMAIL_RE.search(text):
        return "includes a phone number or e-mail address"
    if _shouts(text):
        return "is written in all capitals"
    return None


def lint_copy(
    headlines: list[str],
    descriptions: list[str],
    banned: list[str],
    *,
    max_headline: int | None = None,
    max_description: int | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """The copy linter, run before approval and again before launch (spec §7): headlines at most
    `plans.HEADLINE_MAX` characters and no "!"; descriptions at most `plans.DESCRIPTION_MAX` characters and at
    most one "!"; neither may include a phone number, an e-mail address, a shouted word, or any of `banned`'s
    words (the recipe's own list -- the universal superlatives Admiry cannot back and the tree's vertical
    rules are both just entries in it; `landing.load_recipe(tree)["banned"]`, the kit's own `rules.banned_words`).
    `max_headline` / `max_description` replace those two limits for a channel with lengths of its own (NewsBreak's,
    M1 Task 3); left out or None they are Google's, which every caller before M1 relies on.
    Returns the lines that pass, each list in its given order, and one plain-English note per dropped line."""
    max_h = plans.HEADLINE_MAX if max_headline is None else max_headline
    max_d = plans.DESCRIPTION_MAX if max_description is None else max_description
    kept_h: list[str] = []
    kept_d: list[str] = []
    notes: list[str] = []
    for h in headlines:
        reason = _reason(h, max_len=max_h, max_bangs=0, banned=banned)
        if reason is None:
            kept_h.append(h)
        else:
            notes.append(f'Dropped a headline ("{h}"): it {reason}.')
    for d in descriptions:
        reason = _reason(d, max_len=max_d, max_bangs=1, banned=banned)
        if reason is None:
            kept_d.append(d)
        else:
            notes.append(f'Dropped a description ("{d}"): it {reason}.')
    return kept_h, kept_d, notes


def kept_lines(
    headlines: list[str], descriptions: list[str], banned: list[str], dropped: Dropped
) -> tuple[list[str], list[str], list[str]]:
    """What a build sends: the copy linter's survivors (`lint_copy`, with its notes) minus the lines Admiry dropped
    after Google refused them (`dropped`, spec 2026-10-11 §5 "How a drop is kept") and the lines an active block rule
    catches (spec 2026-10-12 §2.3): the agreed version is never edited, and every build of it -- the launch, the
    hourly ads-launch, an update, the Reviewer's validateOnly -- sends the same ads."""
    kept_h, kept_d, notes = lint_copy(headlines, descriptions, banned)
    return (
        [h for h in kept_h if not dropped.holds("headline", h)],
        [d for d in kept_d if not dropped.holds("description", d)],
        notes,
    )


def _pin_headlines(headlines: list[str], service_names: list[str]) -> list[dict]:
    """The linted headlines as `responsiveSearchAd.headlines` entries; the first is pinned to position 1
    (`pinnedField: "HEADLINE_1"`) when it names the service, so the ad never opens on a generic line."""
    payload = [{"text": h} for h in headlines]
    if payload and any(s and s.lower() in headlines[0].lower() for s in service_names if s):
        payload[0]["pinnedField"] = "HEADLINE_1"
    return payload


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        text = (raw or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _service_names(business: dict) -> list[str]:
    """The plan's chosen service(s) (`consultation.business`): the multi-select list when there is one, else
    the single `service` string, else none (an "awareness" plan naming no particular service)."""
    return list(business.get("services") or ([business["service"]] if business.get("service") else []))


def _intent_phrases(knowledge: dict, service_names: list[str]) -> list[str]:
    """Every intent phrase `knowledge["services"]` carries for the chosen service(s) (all services' when none
    is singled out) -- the same field `plans.rationale_section` reads, here kept in full rather than the four
    it keeps for the owner's rationale card."""
    wanted = {s.lower() for s in service_names if s}
    out: list[str] = []
    for svc in knowledge.get("services") or []:
        if not wanted or (svc.get("name") or "").lower() in wanted:
            out.extend(svc.get("intent_phrases") or [])
    return out


def _digits(s: str | None) -> str:
    """The last 10 digits of a US phone number (matches `landing.py`'s own `_digits`): what
    `callAsset.phoneNumber` wants next to `countryCode: "US"`."""
    return "".join(ch for ch in (s or "") if ch.isdigit())[-10:]


# Characters Google Ads refuses in a keyword's text (a negative's too): `! @ % , *` and the match-type and
# operator marks. Each becomes a space, so "cost,palo alto" still reads as three words.
_PHRASE_REFUSED = re.compile(r'[!@%,*"<>()?;^\\\[\]{}~|]')
MAX_PHRASE_CHARS = 80
MAX_PHRASE_WORDS = 10


def _phrase(text: str | None) -> str | None:
    """A search phrase as Google takes one for a keyword or a negative (A3 final review): the refused characters
    dropped, spaces collapsed, lowercase; None when nothing is left or it is still over MAX_PHRASE_CHARS
    characters or MAX_PHRASE_WORDS words -- it is left out, never sent for Google to refuse the whole mutate."""
    cleaned = " ".join(_PHRASE_REFUSED.sub(" ", text or "").split()).lower()
    if not cleaned or len(cleaned) > MAX_PHRASE_CHARS or len(cleaned.split()) > MAX_PHRASE_WORDS:
        return None
    return cleaned


def _phrases(items: list[str]) -> list[str]:
    """`_phrase` of each, the ones left out dropped, de-duplicated after cleaning, in order."""
    return _dedupe_keep_order([p for p in (_phrase(item) for item in items) if p])


def _negatives(channel_negatives: list[str], recipe_negatives: list[str]) -> list[str]:
    return _phrases([*channel_negatives, *recipe_negatives])


def _keywords(
    theme: str, example_searches: list[str], intent_phrases: list[str], out: Dropped = NOTHING_DROPPED
) -> list[str]:
    """The ad group's search phrases: the theme, the plan's example searches and the service's intent phrases, cleaned
    (`_phrases`), minus what `out` holds -- the phrases Admiry dropped after Google refused them (spec 2026-10-11 §5)
    and those an active block rule catches (spec 2026-10-12 §2.3) -- capped."""
    return [p for p in _phrases([theme, *example_searches, *intent_phrases]) if not out.holds("keyword", p)][
        :MAX_KEYWORDS_PER_AD_GROUP
    ]


def search_channel(plan: dict, channel: str) -> dict | None:
    """The plan's `channel` entry if a build could actually run it: a consultation, a monthly amount on the
    channel, and a cost estimate `cpc_ceiling_micros` can price -- the one check every Google build runs
    (ruling N1, final review): adapter._search_channel raises ToolError(NOT_SEARCH_PLAN) where this returns None.
    None for a plan with no such channel, no consultation section, no money on it, or no estimate the ceiling can
    price: never runnable, so never sent, whatever `channels[]` lists or in what order."""
    sections = plan.get("sections") or {}
    channels = (sections.get("ads") or {}).get("channels") or []
    ch = next((c for c in channels if c.get("channel") == channel), {})
    if "consultation" not in sections or not ch.get("monthly_usd"):
        return None
    try:
        cpc_ceiling_micros(sections.get("estimate") or {})
    except ValueError:
        return None
    return ch


def built_keywords(plan: dict, knowledge: dict, dropped: Dropped = NOTHING_DROPPED) -> frozenset[str]:
    """Every search phrase a build of this version actually sends (spec 2026-10-11 §5; ruling G6, task review fix
    round 1): the union of `operations`'s own per-theme `_keywords`, across every ad group this version builds --
    what a refused phrase must match to be a real drop, never one Admiry never sent (a wasted drop the owner is
    told about for nothing)."""
    ch = plan["sections"]["ads"]["channels"][0]
    service_names = _service_names(plan["sections"]["consultation"]["business"])
    example_searches = list((plan["sections"].get("rationale") or {}).get("example_searches") or [])
    intent_phrases = _intent_phrases(knowledge, service_names)
    out: set[str] = set()
    for theme in list(ch.get("themes") or [])[:MAX_AD_GROUPS]:
        out.update(_keywords(theme, example_searches, intent_phrases, out=dropped))
    return frozenset(out)


def _ad_creative(page_url: str, headlines_payload: list[dict], descriptions: list[str]) -> dict:
    return {
        "finalUrls": [page_url],
        "responsiveSearchAd": {"headlines": headlines_payload, "descriptions": [{"text": d} for d in descriptions]},
    }


def _geo_criteria_ops(campaign_ref: str, target: Target) -> list[dict]:
    """The campaign's location criteria (P4): one proximity circle, or one per resolved service-area geo
    target constant. Shared by `operations` (create) and `diff` (replace, P10) so the two can never drift."""
    if target.kind == "proximity":
        return [
            {
                "campaignCriterionOperation": {
                    "create": {
                        "campaign": campaign_ref,
                        "proximity": {
                            "geoPoint": {
                                "latitudeInMicroDegrees": int(target.lat * 1e6),
                                "longitudeInMicroDegrees": int(target.lng * 1e6),
                            },
                            "radius": target.radius_miles,
                            "radiusUnits": "MILES",
                        },
                    }
                }
            }
        ]
    return [
        {"campaignCriterionOperation": {"create": {"campaign": campaign_ref, "location": {"geoTargetConstant": geo}}}}
        for geo in target.geo_targets
    ]


def _negative_ops(campaign_ref: str, negatives: list[str]) -> list[dict]:
    return [
        {
            "campaignCriterionOperation": {
                "create": {"campaign": campaign_ref, "negative": True, "keyword": {"text": neg, "matchType": "PHRASE"}}
            }
        }
        for neg in negatives
    ]


def _geo_key(consultation: dict) -> tuple:
    """A comparable snapshot of the campaign's geo target: `consultation.geo`'s radius and center (a
    proximity circle around a Places listing) plus `consultation.business.location` (the free-text
    service-area address a `locations` target is built from -- Review round 1: without this, a business
    with no listing whose service area grew, e.g. Austin -> Austin + Round Rock, changed `target.geo_targets`
    but never showed up here, so `diff` never replaced the location criteria). Equal old vs new means the
    location has not changed, so `diff` leaves the campaign's geo criteria alone."""
    geo = consultation.get("geo") or {}
    return (geo.get("radius_miles"), geo.get("center"), consultation.get("business", {}).get("location"))


def geo_key(plan: dict) -> tuple:
    """A plan version's location as `diff` compares it: two versions with equal keys keep the campaign's location
    criteria as they are, and the adapter's `update` resolves a new location target only when the keys differ (A3
    Task 4 review, ruling S8: a budget change never asks Google to place the service area again)."""
    return _geo_key(plan["sections"]["consultation"])


# ---------------------------------------------------------------- the create-mutate (P8)


def budget_op(resource_name: str, name: str, amount_micros: int) -> dict:
    """The campaign budget's create operation: the campaign's own (never shared), spent evenly through the day."""
    return {
        "campaignBudgetOperation": {
            "create": {
                "resourceName": resource_name,
                "name": name,
                "amountMicros": amount_micros,
                "deliveryMethod": "STANDARD",
                "explicitlyShared": False,
            }
        }
    }


def campaign_op(resource_name: str, name: str, budget: str, cpc_ceiling_micros: int) -> dict:
    """The Search campaign's create operation, PAUSED (D50): Google Search alone (no partners, no Display), people
    *in* the area (PRESENCE), Maximize Clicks with a CPC ceiling (D51, `targetSpend`). The one shape a launch
    builds and `agent.ops ads-probe --validate-campaign` sends Google to check (A3 final review I4: a campaign with
    no bidding strategy is refused as REQUIRED)."""
    return {
        "campaignOperation": {
            "create": {
                "resourceName": resource_name,
                "name": name,
                "status": "PAUSED",
                "advertisingChannelType": "SEARCH",
                "campaignBudget": budget,
                "targetSpend": {"cpcBidCeilingMicros": cpc_ceiling_micros},
                "networkSettings": {
                    "targetGoogleSearch": True,
                    "targetSearchNetwork": False,
                    "targetContentNetwork": False,
                    "targetPartnerSearchNetwork": False,
                },
                "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE", "negativeGeoTargetType": "PRESENCE"},
                "containsEuPoliticalAdvertising": NO_EU_POLITICAL,
            }
        }
    }


CALLOUT_MAX = 25  # Google's callout text limit
RSA_MAX_HEADLINES, RSA_MAX_DESCRIPTIONS = 15, 4  # what one responsive search ad takes


def kept_callouts(callouts: list[str], banned: list[str]) -> list[str]:
    """The accepted callouts a build sends (final review I2: the create is all or nothing, so one callout Google
    refuses fails the whole launch): each once, case-insensitively, in its order, and only the ones that pass the
    headline rules (`_reason`: no banned word, no "!", no phone or e-mail, no shouting) within `CALLOUT_MAX`."""
    return [
        c for c in _dedupe_keep_order(callouts) if _reason(c, max_len=CALLOUT_MAX, max_bangs=0, banned=banned) is None
    ]


def creative_asset_ops(campaign_ref: str, creatives: dict, customer_id: str, start: int) -> list[dict]:
    """The owner's accepted images and callouts (hackathon spec 2026-11-04 §5) as campaign assets: per image an
    `IMAGE` asset (named `admiry <set> <format> <n>`, the bytes base64 as REST carries them) linked for `AD_IMAGE`,
    per callout a callout asset linked for `CALLOUT` -- each asset created right before the link that names it.
    Temporary asset names count down from `start` (negative; the caller hands the next id of its own call, so they
    stay unique across the whole mutate). An image with no bytes, a blank callout or one over `CALLOUT_MAX`
    characters is left out, never sent for Google to refuse the whole mutate."""
    ids = count(start, -1)
    set_id = creatives.get("set_id") or ""
    ops: list[dict] = []

    def link(asset_ref: str, field_type: str) -> dict:
        return {
            "campaignAssetOperation": {
                "create": {"campaign": campaign_ref, "asset": asset_ref, "fieldType": field_type}
            }
        }

    for n, image in enumerate(creatives.get("images") or [], start=1):
        if not image.get("bytes"):
            continue
        ref = google_ads.temp("assets", next(ids), customer_id)
        ops.append(
            {
                "assetOperation": {
                    "create": {
                        "resourceName": ref,
                        "name": f"admiry {set_id} {image.get('format')} {n}",
                        "type": "IMAGE",
                        "imageAsset": {"data": base64.b64encode(image["bytes"]).decode("ascii")},
                    }
                }
            }
        )
        ops.append(link(ref, "AD_IMAGE"))
    for text in creatives.get("callouts") or []:
        text = (text or "").strip()
        if not text or len(text) > CALLOUT_MAX:
            continue
        ref = google_ads.temp("assets", next(ids), customer_id)
        ops.append({"assetOperation": {"create": {"resourceName": ref, "calloutAsset": {"calloutText": text}}}})
        ops.append(link(ref, "CALLOUT"))
    return ops


def operations(
    plan: dict,
    knowledge: dict,
    *,
    page_url: str,
    target: Target,
    account: Account,
    customer_id: str,
    dropped: Dropped = NOTHING_DROPPED,
    creatives: dict | None = None,
) -> list[dict]:
    """The one create-mutate a launch sends (spec §4), every resource `PAUSED` (D50 -- `enable` turns the
    campaign on after this succeeds), temporary resource names negative and unique across the call. Assumes
    the plan is fundable and launchable (a funded Google Search channel, a phone, a resolved `target`):
    `ads_run.precheck`, with the adapter's own `precheck`, is where an unfit plan is refused with an owner sentence,
    not here. `dropped` is what every Google build of the business leaves out -- a line Google refused, by its exact
    text (spec 2026-10-11 §5; ruling L2): those lines and phrases are never sent again.
    `creatives` is the owner's accepted creative set (hackathon spec 2026-11-04 §5; `studio.accepted`'s shape), None
    when there is none -- the mutate is then exactly what it was before. Its headlines and descriptions, when it has
    any, replace the plan's in every ad (the copy linter, `dropped` and the service-name pin still apply); its images
    and callouts are appended as campaign assets (`creative_asset_ops`), after everything else -- the callouts first
    deduplicated and linted with the trade's banned words (`kept_callouts`)."""
    biz = plan["business_id"]
    c = plan["sections"]["consultation"]
    ch = plan["sections"]["ads"]["channels"][0]
    tree_name = c["business"].get("category") or "fallback"
    recipe = landing.load_recipe(tree_name)
    name = campaign_name(plans.name_for(knowledge), biz)
    service_names = _service_names(c["business"])

    ids = count(-1, -1)
    ops: list[dict] = []

    budget_ref = google_ads.temp("campaignBudgets", next(ids), customer_id)
    ops.append(budget_op(budget_ref, f"{name} budget", daily_micros(ch["monthly_usd"])))

    campaign_ref = google_ads.temp("campaigns", next(ids), customer_id)
    ops.append(campaign_op(campaign_ref, name, budget_ref, cpc_ceiling_micros(plan["sections"]["estimate"])))

    ops.extend(_geo_criteria_ops(campaign_ref, target))

    ops.append(
        {
            "campaignCriterionOperation": {
                "create": {"campaign": campaign_ref, "language": {"languageConstant": LANGUAGE_CONSTANT}}
            }
        }
    )

    ops.extend(_negative_ops(campaign_ref, _negatives(list(ch.get("negatives") or []), recipe["negatives"])))

    accepted = creatives or {}
    kept_headlines, kept_descriptions, _notes = kept_lines(
        list(accepted.get("headlines") or ch.get("headlines") or []),
        list(accepted.get("descriptions") or ch.get("descriptions") or []),
        recipe["banned"],
        dropped,
    )
    if accepted:  # the owner's own set may run longer than one ad takes; the plan's never does
        kept_headlines = kept_headlines[:RSA_MAX_HEADLINES]
        kept_descriptions = kept_descriptions[:RSA_MAX_DESCRIPTIONS]
    headlines_payload = _pin_headlines(kept_headlines, service_names)
    example_searches = list((plan["sections"].get("rationale") or {}).get("example_searches") or [])
    intent_phrases = _intent_phrases(knowledge, service_names)

    for theme in list(ch.get("themes") or [])[:MAX_AD_GROUPS]:
        ag_ref = google_ads.temp("adGroups", next(ids), customer_id)
        ops.append(
            {
                "adGroupOperation": {
                    "create": {
                        "resourceName": ag_ref,
                        "name": theme,
                        "campaign": campaign_ref,
                        "status": "ENABLED",
                        "type": "SEARCH_STANDARD",
                    }
                }
            }
        )
        for kw in _keywords(theme, example_searches, intent_phrases, out=dropped):
            ops.append(
                {
                    "adGroupCriterionOperation": {
                        "create": {
                            "adGroup": ag_ref,
                            "status": "ENABLED",
                            "keyword": {"text": kw, "matchType": "PHRASE"},
                        }
                    }
                }
            )
        ops.append(
            {
                "adGroupAdOperation": {
                    "create": {
                        "adGroup": ag_ref,
                        "status": "ENABLED",
                        "ad": _ad_creative(page_url, headlines_payload, kept_descriptions),
                    }
                }
            }
        )

    phone = _digits(knowledge.get("phone"))
    if phone:
        asset_ref = google_ads.temp("assets", next(ids), customer_id)
        call_action = account.conversion_actions.get("call_ad")
        call_asset = {"countryCode": "US", "phoneNumber": phone}
        if call_action:
            call_asset["callConversionReportingState"] = "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
            call_asset["callConversionAction"] = call_action
        else:
            call_asset["callConversionReportingState"] = "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION"
        ops.append({"assetOperation": {"create": {"resourceName": asset_ref, "callAsset": call_asset}}})
        ops.append(
            {"campaignAssetOperation": {"create": {"campaign": campaign_ref, "asset": asset_ref, "fieldType": "CALL"}}}
        )

    if accepted:
        callouts = kept_callouts(list(accepted.get("callouts") or []), recipe["banned"])
        ops.extend(creative_asset_ops(campaign_ref, {**accepted, "callouts": callouts}, customer_id, next(ids)))

    return ops


def enable(campaign_resource: str) -> list[dict]:
    """D50's second call: turn the just-built (paused) campaign on."""
    return [
        {
            "campaignOperation": {
                "update": {"resourceName": campaign_resource, "status": "ENABLED"},
                "updateMask": "status",
            }
        }
    ]


# ---------------------------------------------------------------- resources_from / diff (P10)

_RESULT_KEY = {
    "campaignBudgetOperation": "campaignBudgetResult",
    "campaignOperation": "campaignResult",
    "campaignCriterionOperation": "campaignCriterionResult",
    "adGroupOperation": "adGroupResult",
    "adGroupCriterionOperation": "adGroupCriterionResult",
    "adGroupAdOperation": "adGroupAdResult",
    "assetOperation": "assetResult",
    "campaignAssetOperation": "campaignAssetResult",
}


def resources_from(responses: list[dict], operations: list[dict], *, ad_groups: dict[str, str] | None = None) -> dict:
    """The real resource names Google's `mutateOperationResponses` assigned, read off the same `operations`
    list this call sent (one response per operation, in the same order). Keyed the way `diff` (P10) and
    `channel_campaign.resources` need them -- by the one thing that still means something once
    this mutate is over, since a temp id does not: `"campaign_budget"`, `"campaign"`, `"call_asset"` and
    `"campaign_asset"` (the call asset and its `CALL` link only: an accepted image or callout and its link are not
    kept) each a single resource name; `"ad_groups"` and `"ads"` each `{theme: resource_name}`;
    `"keywords"` `{theme: {keyword_text: resource_name}}`; `"negatives"` `{keyword_text: resource_name}`
    (campaign-level negative criteria); `"geo_criteria"` the location/proximity criteria, as a list (`diff`
    replaces all of them together, never one at a time); `"final_url"` (A3p-2 H9) the address every ad this
    call created actually carries, read off the same `adGroupAdOperation.create.ad.finalUrls` the call sent
    -- never a separate parameter, so it is always exactly what Google was told, whether this ran at launch
    (`operations`) or at a later update (`diff`). The campaign's language criterion is not kept: it
    is never diffed. `ad_groups` (theme -> resource name, what an earlier call stored; A3 Task 4) lets a `diff`
    mutate's new keywords and ads under an ad group that already exists be keyed by its theme too -- their
    `adGroup` is that group's real resource name, not a temp id this call created."""
    out: dict = {"ad_groups": {}, "ads": {}, "keywords": {}, "negatives": {}, "geo_criteria": []}
    theme_by_ref: dict[str, str] = {ref: theme for theme, ref in (ad_groups or {}).items()}
    for op, resp in zip(operations, responses, strict=False):
        if not isinstance(op, dict) or not op or not isinstance(resp, dict):
            continue
        kind = next(iter(op))
        body = (op.get(kind) or {}).get("create")
        if not isinstance(body, dict):
            continue
        result = resp.get(_RESULT_KEY.get(kind, ""))
        resource_name = result.get("resourceName") if isinstance(result, dict) else None
        if not resource_name:
            continue
        if kind == "campaignBudgetOperation":
            out["campaign_budget"] = resource_name
        elif kind == "campaignOperation":
            out["campaign"] = resource_name
        elif kind == "campaignCriterionOperation":
            keyword = body.get("keyword")
            if body.get("negative") and isinstance(keyword, dict) and keyword.get("text"):
                out["negatives"][keyword["text"]] = resource_name
            elif "proximity" in body or "location" in body:
                out["geo_criteria"].append(resource_name)
            # a "language" criterion is never diffed, so its resource name is not worth keeping
        elif kind == "adGroupOperation":
            theme = body.get("name")
            if theme:
                out["ad_groups"][theme] = resource_name
                temp_ref = body.get("resourceName")
                if temp_ref:
                    theme_by_ref[temp_ref] = theme
        elif kind == "adGroupCriterionOperation":
            theme = theme_by_ref.get(body.get("adGroup"))
            keyword = body.get("keyword")
            if theme and isinstance(keyword, dict) and keyword.get("text"):
                out["keywords"].setdefault(theme, {})[keyword["text"]] = resource_name
        elif kind == "adGroupAdOperation":
            theme = theme_by_ref.get(body.get("adGroup"))
            if theme:
                out["ads"][theme] = resource_name
            final_urls = (body.get("ad") or {}).get("finalUrls")
            if final_urls:  # H9: every ad a create/diff sends carries the same page_url; any one confirms it
                out["final_url"] = final_urls[0]
        # the call asset and its CALL link only: an accepted image or callout (creative_asset_ops) is not kept
        elif kind == "assetOperation" and "callAsset" in body:
            out["call_asset"] = resource_name
        elif kind == "campaignAssetOperation" and body.get("fieldType") == "CALL":
            out["campaign_asset"] = resource_name
    return out


def money(plan: dict, resources: dict) -> list[dict]:
    """The plan's daily budget and CPC ceiling as updates to the campaign `resources` holds -- always the pair
    (Google keeps the ceiling on the campaign itself, beside its budget, whether or not its own value moved).
    `diff` opens with them when the budget moved; the adapter's `update` sends them on their own, first, on every
    update of ads already built (ruling S16: a change Google refuses in the rest never holds a budget change back)."""
    ch = plan["sections"]["ads"]["channels"][0]
    return [
        {
            "campaignBudgetOperation": {
                "update": {
                    "resourceName": resources["campaign_budget"],
                    "amountMicros": daily_micros(ch["monthly_usd"]),
                },
                "updateMask": "amountMicros",
            }
        },
        {
            "campaignOperation": {
                "update": {
                    "resourceName": resources["campaign"],
                    "targetSpend": {"cpcBidCeilingMicros": cpc_ceiling_micros(plan["sections"]["estimate"])},
                },
                "updateMask": "targetSpend.cpcBidCeilingMicros",
            }
        },
    ]


def diff(
    old_plan: dict,
    new_plan: dict,
    resources: dict,
    *,
    page_url: str,
    target: Target,
    account: Account,
    customer_id: str,
    with_money: bool = True,
    dropped: Dropped = NOTHING_DROPPED,
) -> list[dict]:
    """P10: the mutate that carries a newer approved plan version's changes onto the campaign `resources` an
    earlier `operations()` call already built (spec §5 "Later versions"), addressed the way `resources_from`
    returns them (ad groups, ads and their keywords by theme; negatives and geo criteria by their own kind).
    Builds, in order: the budget and the CPC ceiling (`money`), when the budget moved and `with_money` (the adapter's
    `update` sends the pair on its own mutate first, ruling S16, and asks for the rest without it); the campaign's geo
    criteria replaced (all removed, all recreated from `target`) when `consultation.geo` (radius or center) moved;
    negative keywords added or removed by text (the plan's own list plus the tree's, same as `operations`); one ad
    group paused per theme that disappeared; per theme that is new, one created with its keywords and ad -- or, when
    `resources` still has a group of that name (the theme left the plan and came back, its group paused), that
    group turned back on (ruling S16: Google refuses a second ad group of the same name in a campaign) and treated
    like a kept theme; new keywords *added* to a kept theme's ad group when `rationale.example_searches` now names
    one `resources` does not already have for it (never removed: a service's `intent_phrases`, folded in by
    `operations` at launch, are not visible here -- `diff`, unlike `operations`, is not given `knowledge` -- so a
    keyword missing from this call's own, narrower candidate list is not evidence it should go; only ever adding
    is always safe); and a new ad with the old one paused, so Google re-reviews just the new text: under every
    kept theme's ad group when the headlines or descriptions changed OR `page_url` no longer matches
    `resources["final_url"]` (A3p-2 H9: the final URL is part of an ad's identity exactly like its copy --
    `resources.get("final_url")` missing, e.g. a campaign recovered by name after a lost create response, is
    treated the same as "changed": refreshing one ad nobody asked for costs a Google operation, but silently
    leaving it on a stale address is the bug H9 exists to prevent), and under a group turned back on always (its
    ad carries the text of whichever version last ran it, which the two versions compared here cannot tell).
    `dropped` is what every Google build of the business leaves out -- a line Google refused, by its exact text, in
    whichever version (spec 2026-10-11 §5; ruling L2): those lines and phrases are never sent again, not even as a
    kept theme's added search phrase."""
    old_ch = old_plan["sections"]["ads"]["channels"][0]
    new_ch = new_plan["sections"]["ads"]["channels"][0]
    old_consultation = old_plan["sections"]["consultation"]
    new_consultation = new_plan["sections"]["consultation"]
    ops: list[dict] = []

    if with_money and daily_micros(old_ch["monthly_usd"]) != daily_micros(new_ch["monthly_usd"]):
        ops.extend(money(new_plan, resources))

    if geo_key(old_plan) != geo_key(new_plan):
        # P10 "replace": every old geo criterion removed, the new target's recreated -- never patched, since
        # a proximity circle and a set of geo target constants are different criterion types.
        ops.extend({"campaignCriterionOperation": {"remove": ref}} for ref in resources.get("geo_criteria") or [])
        ops.extend(_geo_criteria_ops(resources["campaign"], target))

    old_business = old_consultation["business"]
    new_business = new_consultation["business"]
    old_recipe = landing.load_recipe(old_business.get("category") or "fallback")
    new_recipe = landing.load_recipe(new_business.get("category") or "fallback")
    # Review round 1: compared case-insensitively -- "DIY" (old) vs "diy" (new) is a no-op, not a remove-then-create
    # pair. Since the final review every negative is a lowercase `_phrase`, so a key is the text itself, the same
    # text `resources["negatives"]` stored it under.
    old_by_key = {n.lower(): n for n in _negatives(list(old_ch.get("negatives") or []), old_recipe["negatives"])}
    new_by_key = {n.lower(): n for n in _negatives(list(new_ch.get("negatives") or []), new_recipe["negatives"])}
    for key in sorted(set(old_by_key) - set(new_by_key)):
        ref = resources.get("negatives", {}).get(old_by_key[key])
        if ref:
            ops.append({"campaignCriterionOperation": {"remove": ref}})
    added = [new_by_key[key] for key in sorted(set(new_by_key) - set(old_by_key))]
    ops.extend(_negative_ops(resources["campaign"], added))

    old_themes = list(old_ch.get("themes") or [])[:MAX_AD_GROUPS]
    new_themes = list(new_ch.get("themes") or [])[:MAX_AD_GROUPS]
    groups = resources.get("ad_groups", {})
    removed = [t for t in old_themes if t not in new_themes]
    kept = [t for t in new_themes if t in old_themes]
    back = [t for t in new_themes if t not in old_themes and t in groups]  # left the plan once: its group is paused
    added = [t for t in new_themes if t not in old_themes and t not in groups]

    for theme in removed:
        ref = groups.get(theme)
        if ref:
            ops.append(
                {"adGroupOperation": {"update": {"resourceName": ref, "status": "PAUSED"}, "updateMask": "status"}}
            )
    for theme in back:
        ops.append(
            {
                "adGroupOperation": {
                    "update": {"resourceName": groups[theme], "status": "ENABLED"},
                    "updateMask": "status",
                }
            }
        )

    kept_headlines, kept_descriptions, _notes = kept_lines(
        list(new_ch.get("headlines") or []), list(new_ch.get("descriptions") or []), new_recipe["banned"], dropped
    )
    headlines_payload = _pin_headlines(kept_headlines, _service_names(new_business))
    example_searches = list((new_plan["sections"].get("rationale") or {}).get("example_searches") or [])

    for theme in [*kept, *back]:  # add-only (see docstring): never remove a keyword `diff` cannot fully account for
        ag_ref = groups.get(theme)
        if not ag_ref:
            continue
        existing = resources.get("keywords", {}).get(theme, {})
        room = MAX_KEYWORDS_PER_AD_GROUP - len(existing)
        candidates = [
            kw for kw in _phrases([theme, *example_searches]) if kw not in existing and not dropped.holds("keyword", kw)
        ]
        for kw in candidates[: max(room, 0)]:
            ops.append(
                {
                    "adGroupCriterionOperation": {
                        "create": {
                            "adGroup": ag_ref,
                            "status": "ENABLED",
                            "keyword": {"text": kw, "matchType": "PHRASE"},
                        }
                    }
                }
            )

    if added:
        ids = count(-1, -1)
        for theme in added:
            ag_ref = google_ads.temp("adGroups", next(ids), customer_id)
            ops.append(
                {
                    "adGroupOperation": {
                        "create": {
                            "resourceName": ag_ref,
                            "name": theme,
                            "campaign": resources["campaign"],
                            "status": "ENABLED",
                            "type": "SEARCH_STANDARD",
                        }
                    }
                }
            )
            for kw in [p for p in _phrases([theme, *example_searches]) if not dropped.holds("keyword", p)][
                :MAX_KEYWORDS_PER_AD_GROUP
            ]:
                ops.append(
                    {
                        "adGroupCriterionOperation": {
                            "create": {
                                "adGroup": ag_ref,
                                "status": "ENABLED",
                                "keyword": {"text": kw, "matchType": "PHRASE"},
                            }
                        }
                    }
                )
            ops.append(
                {
                    "adGroupAdOperation": {
                        "create": {
                            "adGroup": ag_ref,
                            "status": "ENABLED",
                            "ad": _ad_creative(page_url, headlines_payload, kept_descriptions),
                        }
                    }
                }
            )

    copy_changed = list(old_ch.get("headlines") or []) != list(new_ch.get("headlines") or []) or list(
        old_ch.get("descriptions") or []
    ) != list(new_ch.get("descriptions") or [])
    url_changed = resources.get("final_url") != page_url  # H9: an unknown final_url reads as changed (see docstring)
    refresh_ad = copy_changed or url_changed
    for theme in [*(kept if refresh_ad else []), *back]:
        ag_ref = groups.get(theme)
        if not ag_ref:
            continue
        ops.append(
            {
                "adGroupAdOperation": {
                    "create": {
                        "adGroup": ag_ref,
                        "status": "ENABLED",
                        "ad": _ad_creative(page_url, headlines_payload, kept_descriptions),
                    }
                }
            }
        )
        old_ad_ref = resources.get("ads", {}).get(theme)
        if old_ad_ref:
            ops.append(
                {
                    "adGroupAdOperation": {
                        "update": {"resourceName": old_ad_ref, "status": "PAUSED"},
                        "updateMask": "status",
                    }
                }
            )

    return ops
