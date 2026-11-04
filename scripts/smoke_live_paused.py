"""The `live_paused` smoke on dev (hackathon spec 2026-11-04-muse-verified-creatives-google-ads, plan Task 9, ruling
R27): the owner's accepted creative set pushed into a real Google Ads campaign that is built PAUSED and never enabled.

Run inside the agent's own container, against its own settings and database (nothing is read from the command line but
the business id and the step):

    python - <business_id> --validate  < scripts/smoke_live_paused.py
    python - <business_id> --create    < scripts/smoke_live_paused.py
    python - <business_id> --readback  < scripts/smoke_live_paused.py

--validate  builds through the Google Search adapter exactly as a launch does (`adapter.build`: the accepted set is
            `payload["creatives"]`; `_operations`: the create mutate) and sends it validateOnly -- Google checks every
            operation and builds nothing. The database writes a build makes along the way (the account's tag row) are
            rolled back. The account is the business's own (`_customer_id`, as a launch resolves it); a business with
            none yet is checked against the pool account the allocator would pick for it (read only, nothing bound).
--create    one real create through the product's own path (`ads_run.launch` -> `_advance` -> `_create`) with this
            process's Settings switched to `live_paused` in memory only (the box's .env stays as it is, so no other
            business is touched): stored, marked `demo_paused`, never enabled. Refused before anything is sent when
            the business lacks what that path needs (an approved plan funding Google Search, a ready account, its
            place on AGENT_LAUNCH_ALLOWLIST), or already has a campaign built (one create, never two).
--readback  GAQL: the business's campaigns and their status, their campaign assets by field type, and the responsive
            search ads' headlines next to the accepted set's. Any campaign ENABLED is paused at once with the
            adapter's own `set_paused` and the step exits 3.

Customer IDs are masked in everything printed (`<cid>`); Google's refusal bodies are printed verbatim otherwise. Exit
codes: 0 done, 1 Google refused or failed, 2 refused before sending (what is missing is printed), 3 a campaign was
found ENABLED (paused now)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import Counter

CHANNEL = "google_search"
ADS_HOST = "googleads.googleapis.com"
STEPS = ("validate", "create", "readback")
LIVE_PAUSED = "live_paused"


# ---------------------------------------------------------------- pure helpers (agent/tests/test_smoke_live_paused.py)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """`<business_id>` and exactly one of --validate, --create, --readback; `step` names the one given."""
    p = argparse.ArgumentParser(prog="smoke_live_paused", description="The live_paused smoke on dev (ruling R27).")
    p.add_argument("business_id")
    group = p.add_mutually_exclusive_group(required=True)
    for step in STEPS:
        group.add_argument(f"--{step}", dest="step", action="store_const", const=step)
    args = p.parse_args(argv)
    if not args.business_id.startswith("biz_"):
        p.error(f"not a business id: {args.business_id!r}")
    return args


def mask(value: object, customer_id: str | None) -> object:
    """`value` (a string, or JSON-like data) with the Customer ID replaced by `<cid>` wherever it appears."""
    if not customer_id:
        return value
    if isinstance(value, str):
        return value.replace(customer_id, "<cid>")
    if isinstance(value, dict):
        return {k: mask(v, customer_id) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask(v, customer_id) for v in value]
    return value


def op_counts(ops: list[dict]) -> dict[str, int]:
    """The create mutate by operation kind, campaign-asset links by their field type (`campaignAssetOperation:AD_IMAGE`)
    and asset creates by what they carry (`assetOperation:IMAGE`, `assetOperation:CALLOUT`, `assetOperation:CALL`)."""
    out: Counter = Counter()
    for op in ops:
        for kind, body in op.items():
            create = (body or {}).get("create") if isinstance(body, dict) else None
            if kind == "campaignAssetOperation" and isinstance(create, dict):
                out[f"{kind}:{create.get('fieldType')}"] += 1
            elif kind == "assetOperation" and isinstance(create, dict):
                carried = (
                    "IMAGE"
                    if "imageAsset" in create
                    else "CALLOUT"
                    if "calloutAsset" in create
                    else "CALL"
                    if "callAsset" in create
                    else "OTHER"
                )
                out[f"{kind}:{carried}"] += 1
            else:
                out[kind] += 1
    return dict(sorted(out.items()))


def sent_headlines(ops: list[dict]) -> list[list[dict]]:
    """Every responsive search ad's headline entries in a create mutate, in operation order."""
    out = []
    for op in ops:
        ad = ((op.get("adGroupAdOperation") or {}).get("create") or {}).get("ad") or {}
        rsa = ad.get("responsiveSearchAd")
        if rsa is not None:
            out.append(list(rsa.get("headlines") or []))
    return out


def compare_headlines(accepted: list[str], got: list[dict]) -> dict:
    """The accepted set's headlines against an ad's headline entries ({"text", "pinnedField"?}): equal as a set of
    texts, equal in order, what is missing and what is extra, and which entries are pinned (the service-name pin on
    the first line is the build's own rule, not a difference)."""
    texts = [h.get("text") for h in got]
    return {
        "equal": sorted(accepted) == sorted(texts),
        "same_order": accepted == texts,
        "missing": [t for t in accepted if t not in texts],
        "extra": [t for t in texts if t not in accepted],
        "pinned": {h.get("text"): h.get("pinnedField") for h in got if h.get("pinnedField")},
    }


def summarise(campaign_rows: list[dict], asset_rows: list[dict], ad_rows: list[dict]) -> dict:
    """GAQL rows (REST's camelCase) as the read-back table: each campaign's status, the ENABLED ones, the campaign
    assets by field type (removed links left out) with their asset types and callout texts, and each ad's headlines."""
    campaigns = [
        {
            "resource": (r.get("campaign") or {}).get("resourceName"),
            "name": (r.get("campaign") or {}).get("name"),
            "status": (r.get("campaign") or {}).get("status"),
        }
        for r in campaign_rows
    ]
    fields: Counter = Counter()
    assets = []
    for r in asset_rows:
        link, asset = r.get("campaignAsset") or {}, r.get("asset") or {}
        if link.get("status") == "REMOVED":
            continue
        fields[link.get("fieldType")] += 1
        assets.append(
            {
                "field_type": link.get("fieldType"),
                "status": link.get("status"),
                "asset": asset.get("resourceName"),
                "type": asset.get("type"),
                "name": asset.get("name"),
                "callout": (asset.get("calloutAsset") or {}).get("calloutText"),
            }
        )
    ads = []
    for r in ad_rows:
        group, ad = r.get("adGroup") or {}, r.get("adGroupAd") or {}
        rsa = (ad.get("ad") or {}).get("responsiveSearchAd") or {}
        ads.append(
            {
                "ad_group": group.get("name"),
                "ad": ad.get("resourceName"),
                "status": ad.get("status"),
                "headlines": [
                    {k: v for k, v in h.items() if k in ("text", "pinnedField")} for h in rsa.get("headlines") or []
                ],
                "descriptions": [d.get("text") for d in rsa.get("descriptions") or []],
            }
        )
    return {
        "campaigns": campaigns,
        "enabled": [c for c in campaigns if c["status"] == "ENABLED"],
        "asset_fields": dict(sorted(fields.items(), key=lambda kv: str(kv[0]))),
        "assets": assets,
        "ads": ads,
    }


def missing_for_create(facts: dict) -> list[str]:
    """What the product's create path needs and the business lacks (ruling R27 step 3: reported, never seeded), from
    `facts`: `mode` (exec_mode_for under live_paused), `approved` (the approved plan's version, None without one),
    `funded` (it funds Google Search), `account_ready` and `account_reason`, `page_url` (the page's address, empty
    while none is published: every ad's final URL), `built` (a campaign already stored)."""
    out = []
    if facts.get("mode") != LIVE_PAUSED:
        out.append(f"the business is not on AGENT_LAUNCH_ALLOWLIST (its switch reads {facts.get('mode')!r})")
    if facts.get("approved") is None:
        out.append("an approved plan: the business has no approved plan version")
    elif not facts.get("funded"):
        out.append(f"an approved plan that funds Google Search: plan v{facts['approved']} does not")
    if not facts.get("account_ready"):
        out.append(f"a Google Ads account ready for the business: {facts.get('account_reason') or 'not ready'}")
    if not facts.get("page_url"):
        out.append("a published page: the business's page has no address yet, and every ad's final URL is it")
    if facts.get("built"):
        out.append("nothing built yet: a campaign is already stored for the business (one create only)")
    return out


def ads_error(e: Exception, bodies: list[str], customer_id: str | None) -> dict:
    """A Google failure as printed: the client's sentence, Google's codes and HTTP status, each refusal detail
    (operation index, field path, kind, text), and Google's own response bodies verbatim (Customer ID masked)."""
    details = [d.as_dict() if hasattr(d, "as_dict") else dict(vars(d)) for d in getattr(e, "details", ()) or ()]
    return mask(
        {
            "error": str(e),
            "status": getattr(e, "status", None),
            "codes": list(getattr(e, "codes", ()) or ()),
            "details": details,
            "google_bodies": bodies,
        },
        customer_id,
    )


# ---------------------------------------------------------------- the steps (inside the agent container)


def _print(label: str, value: object, customer_id: str | None = None) -> None:
    print(f"== {label}")
    print(json.dumps(mask(value, customer_id), indent=1, default=str, ensure_ascii=False))


def _client(bodies: list[str]):
    """The adapter's own metered client, keeping every Google Ads answer that is not a 2xx, verbatim."""
    from agent.channels.google_search import adapter as google

    http = google.client()

    def keep(response) -> None:
        if response.url.host == ADS_HOST and response.status_code >= 300:
            response.read()
            bodies.append(response.text)

    http.event_hooks["response"].append(keep)
    return http


def _pool_pick(conn, biz: str) -> str | None:
    """The pool account `google_managed.assign` would bind the business to now -- the first of its scopes with an
    open account that has room and runs no competitor, the fewest live businesses first -- read only."""
    from agent import google_managed

    scopes = google_managed.candidates(*google_managed.industry(conn, biz))
    accounts = google_managed.pool(conn)
    for scope in scopes:
        room = sorted(
            (a for a in accounts if a.scope == scope and google_managed.refusal(conn, biz, a) is None),
            key=lambda a: (a.live, a.added_at, a.customer_id),
        )
        if room:
            return room[0].customer_id
    return None


def _account_for(conn, biz: str) -> tuple[str | None, str]:
    """The Customer ID the business's ads are (or would be) built in, and where it came from: the stored row's, the
    business's own as a launch resolves it, else the pool account the allocator would pick (read only)."""
    from agent import ads_run
    from agent.channels.google_search import adapter as google

    row = ads_run.get(conn, biz, CHANNEL) or {}
    stored = (row.get("resources") or {}).get("customer_id")
    if stored:
        return stored, "the business's row (resources.customer_id)"
    try:
        return google._customer_id(conn, biz), "the business's account (_customer_id)"
    except Exception as e:  # noqa: BLE001 - no account of its own is an answer here, not a failure
        picked = _pool_pick(conn, biz)
        source = f"the pool account the allocator would pick (the business has none: {e})"
        return picked, source if picked else f"none: {e}"


def _plan_for(conn, biz: str) -> tuple[dict | None, str]:
    from agent import ads_run, plans

    plan = ads_run.approved(conn, biz)
    if plan is not None:
        return plan, f"approved plan v{plan.get('version')}"
    r = conn.execute(
        "SELECT id, version, status FROM plan WHERE business_id = %s ORDER BY version DESC LIMIT 1", (biz,)
    ).fetchone()
    if r is None:
        return None, "no plan"
    return plans.get(conn, biz, r["id"]), f"plan v{r['version']} ({r['status']}; the business has no approved plan)"


def validate(biz: str) -> int:
    import psycopg

    from agent import ads_run, config, db
    from agent.channels import registry
    from agent.channels.google_search import adapter as google
    from agent.channels.google_search import client as google_ads
    from agent.creatives import studio

    settings = config.load()
    bodies: list[str] = []
    code = 0
    with db.connect(settings.database_url) as conn, _client(bodies) as http:
        print(
            f"switch: {registry.exec_mode(settings, CHANNEL)} (box), for {biz}: "
            f"{registry.exec_mode_for(conn, settings, biz, CHANNEL)}"
        )
        accepted = studio.accepted(conn, biz)
        if accepted is None:
            print("REFUSED: the business has no accepted creative set", file=sys.stderr)
            return 2
        _print(
            "accepted set",
            {
                k: (v if k != "images" else [(i["id"], i["format"], len(i["bytes"])) for i in v])
                for k, v in accepted.items()
            },
        )
        plan, said = _plan_for(conn, biz)
        print(f"plan: {said}")
        if plan is None:
            return 2
        customer_id, source = _account_for(conn, biz)
        print(f"account: {source}")
        if customer_id is None:
            return 2
        knowledge = ads_run._knowledge(conn, biz)
        adapter = registry.adapter(CHANNEL)
        _print("refusals (the adapter's own rules)", adapter.refusals(conn, settings, biz, plan, knowledge))
        with conn.transaction() as tx:  # what the build writes on the way (the account's tag row) is rolled back
            try:
                account = ads_run._account(conn, biz, CHANNEL)
                build = adapter.build(conn, settings, biz, plan, knowledge, account, http=http)
                same = build.payload.get("creatives") == accepted
                print(f"payload['creatives'] is the accepted set: {same}")
                ops = google._operations(conn, settings, build, customer_id, http)
            except Exception as e:  # noqa: BLE001 - the smoke reports whatever stops the build, verbatim
                _print("build FAILED", ads_errors_or(e, bodies, customer_id), customer_id)
                code = 1
                raise psycopg.Rollback(tx) from None
            _print("create mutate, by operation", op_counts(ops))
            for n, headlines in enumerate(sent_headlines(ops), start=1):
                _print(
                    f"ad {n}: headlines against the accepted set", compare_headlines(accepted["headlines"], headlines)
                )
            try:
                google_ads.mutate(settings, customer_id, ops, validate_only=True, http=http)
            except google_ads.AdsError as e:
                _print("validateOnly: REFUSED", ads_error(e, bodies, customer_id), customer_id)
                code = 1
            else:
                print("== validateOnly: OK (Google accepted every operation; nothing was built)")
            raise psycopg.Rollback(tx)
        print(f"Google Ads operations sent: {google.ops_sent(http)}")
    return code


def ads_errors_or(e: Exception, bodies: list[str], customer_id: str | None) -> dict:
    """A failure before or during the mutate: Google's (`ads_error`) or the product's own refusal sentence."""
    if getattr(e, "codes", None) is not None and getattr(e, "owner", None) is not None:
        return ads_error(e, bodies, customer_id)
    return {"error": f"{type(e).__name__}: {e}"}


def create(biz: str) -> int:
    from agent import ads_run, config, db, landing
    from agent.channels import registry

    box = config.load()
    settings = dataclasses.replace(box, exec_modes={**box.exec_modes, CHANNEL: LIVE_PAUSED})  # this process only
    with db.connect(settings.database_url) as conn:
        plan = ads_run.approved(conn, biz)
        row = ads_run.get(conn, biz, CHANNEL) or {}
        status = ads_run.account_status(conn, settings, biz, CHANNEL)
        facts = {
            "mode": registry.exec_mode_for(conn, settings, biz, CHANNEL),
            "approved": plan.get("version") if plan else None,
            "funded": CHANNEL in ads_run.funded_channels(plan),
            "account_ready": status.ready,
            "account_reason": status.reason,
            "page_url": (landing.current(conn, biz) or {}).get("url") or None,
            "built": "campaign" in (row.get("resources") or {}),
        }
        _print("create: what the path needs", facts)
        missing = missing_for_create(facts)
        if missing:
            _print("REFUSED before sending anything; missing", missing)
            return 2
        out = ads_run.launch(conn, settings, biz, CHANNEL)
        after = ads_run.get(conn, biz, CHANNEL) or {}
        resources = after.get("resources") or {}
        cid = resources.get("customer_id")
        _print("create: the step's outcome", out, cid)
        _print(
            "create: the stored row",
            {
                "state": after.get("state"),
                "plan_id": after.get("plan_id"),
                "last_error": after.get("last_error"),
                "demo_paused": resources.get("demo_paused"),
                "campaign": resources.get("campaign"),
                "campaign_budget": resources.get("campaign_budget"),
                "ads": resources.get("ads"),
                "keys": sorted(resources),
            },
            cid,
        )
        return 0 if resources.get("campaign") and resources.get("demo_paused") else 1


def readback(biz: str) -> int:
    from agent import ads_run, config, db
    from agent.channels import registry
    from agent.channels.google_search import adapter as google
    from agent.channels.google_search import client as google_ads
    from agent.creatives import studio

    settings = config.load()
    bodies: list[str] = []
    with db.connect(settings.database_url) as conn, _client(bodies) as http:
        customer_id, source = _account_for(conn, biz)
        print(f"account: {source}")
        if customer_id is None:
            return 2
        like = google._gaql_like(biz)
        try:
            campaigns = google_ads.search(
                settings,
                customer_id,
                "SELECT campaign.resource_name, campaign.name, campaign.status FROM campaign "
                f"WHERE campaign.name LIKE '%{like}' AND campaign.status != 'REMOVED'",
                http=http,
            )
            assets, ads = [], []
            for c in campaigns:
                where = f"campaign.resource_name = '{google._gaql_text(c['campaign']['resourceName'])}'"
                assets += google_ads.search(
                    settings,
                    customer_id,
                    "SELECT campaign.resource_name, campaign_asset.field_type, campaign_asset.status, "
                    "asset.resource_name, asset.type, asset.name, asset.callout_asset.callout_text "
                    f"FROM campaign_asset WHERE {where}",
                    http=http,
                )
                ads += google_ads.search(
                    settings,
                    customer_id,
                    "SELECT ad_group.name, ad_group_ad.resource_name, ad_group_ad.status, "
                    "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions "
                    f"FROM ad_group_ad WHERE {where} AND ad_group_ad.status != 'REMOVED'",
                    http=http,
                )
        except google_ads.AdsError as e:
            _print("read-back FAILED", ads_error(e, bodies, customer_id), customer_id)
            return 1
        table = summarise(campaigns, assets, ads)
        _print("read-back", table, customer_id)
        accepted = studio.accepted(conn, biz)
        for ad in table["ads"]:
            if accepted is not None:
                _print(
                    f"{ad['ad_group']}: headlines against the accepted set",
                    compare_headlines(accepted["headlines"], ad["headlines"]),
                    customer_id,
                )
        if table["enabled"]:
            account = ads_run._account(conn, biz, CHANNEL)
            adapter = registry.adapter(CHANNEL)
            for c in table["enabled"]:
                print(f"!! ENABLED: {mask(c['resource'], customer_id)} -- pausing it now", file=sys.stderr)
                adapter.set_paused(
                    conn, settings, account, {"campaign": c["resource"], "customer_id": customer_id}, True, http=http
                )
            return 3
        print(f"Google Ads operations sent: {google.ops_sent(http)}")
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return {"validate": validate, "create": create, "readback": readback}[args.step](args.business_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
