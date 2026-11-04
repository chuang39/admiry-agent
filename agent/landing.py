"""Admiry Pages, part 1 (spec 2026-09-28-admiry-pages-design.md): page records, per-tree recipes, the kit an
assistant builds a page from, the tracking snippet, and (Task 4) the check that says whether a page is fit for
paid clicks. Nothing here names a vertical: recipes are data (`agent/page_recipes/<tree>.json`)."""

from __future__ import annotations

import html
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import booking_hosts, business, config, crawl, db, places, plans, profile, site_install, site_platform

log = logging.getLogger(__name__)

RECIPES = Path(__file__).parent / "page_recipes"
NO_PLAN = "Build the plan first; the page is written from it."
# `code_seen` (A3p-5 batch 2, Q9): whether the hourly page-check last saw Admiry's code on an owner page (jobs.page_check)
# `site_draft` and `site_spec_id` (A3p-6 batch 2): the industry site's working copy between builds and the document its
# last build wrote -- what makes the page a site (`agent/sites/photos.is_site`)
PAGE_COLS = (
    "id, business_id, source, url, hostname, version, status, check_json, checked_at, kit_plan_id, kit_sent_at, "
    "created_at, code_seen, site_draft, site_spec_id"
)
GOOGLE_RULES = [
    "The page must open over https, show the same business the ad names, and not send people on to another site.",
    "Everything on the page should be true for this business; generic text copied from elsewhere gets ads disapproved.",
    "A privacy notice link is required when the page has a form.",
    (
        "Keep one goal on the page: call, or request a call. No menu, and no links but the ones the page needs: to "
        "call, to book, to the map, and to the privacy and terms pages."
    ),
    "It must work on a phone first: most visits come from one.",
]
# Where Admiry's code goes (A3p-5 batch 2, Q4; spec W16): inside <head>, where Google asks for its tag, or before
# </body> where a site allows only that. The kit says it, and so does the check's failing tracking rule, which still
# finds the code anywhere in the page
TRACKING_WHERE = (
    "Put this inside <head> on every page (or before </body> where your site allows only that), exactly as given."
)
TRACKING_FIX = (
    "Paste the tracking snippet from the kit inside <head> on every page (or before </body> where your site allows "
    "only that), exactly as given (it carries this page's token)."
)


@cache
def load_recipe(tree: str) -> dict:
    path = RECIPES / f"{tree}.json"
    if not path.exists():
        path = RECIPES / "fallback.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- page records


def _row(r) -> dict | None:
    return dict(r) if r else None


def current(conn: psycopg.Connection, biz: str) -> dict | None:
    """The published page row, else the newest of any status — which may be a `preview` row with an empty
    `url` (ensure_page_for_kit's placeholder, before set_landing_page names an address); callers that need an
    address check `url`."""
    r = conn.execute(
        f"SELECT {PAGE_COLS} FROM page WHERE business_id = %s "
        "ORDER BY (status = 'published') DESC, created_at DESC LIMIT 1",
        (biz,),
    ).fetchone()
    return _row(r)


def final_host(conn: psycopg.Connection, biz: str) -> str | None:
    """The hostname the ads send visitors to, for the plan card's ad preview (`plans.cards(final_host=)`, spec
    2026-09-28 A3 §7: the homepage, Muse, Claude and the MCP Apps card alike); None before the business has a
    page with an address. H22: `current`'s own `hostname` column is Admiry's hosted hostname only -- unset for
    an owner page even a live one -- so this falls back to `url`'s own host, the way any other owner-page
    reader must (`plans.cards` stays pure either way: this is the one place the derivation happens). H25
    (round 2): `hostname` is trusted only while the row's `source` is 'hosted' or 'admiry' -- a row that has
    gone (or come back) owner keeps whatever `hostname` a hosted publish last left on it (H25's one row is
    never re-keyed), and a legacy row from before A3p-2 kept its owner address in `hostname` outright (R7), so
    either one falls through to `url`'s own host exactly like a row that was never hosted at all."""
    page = current(conn, biz)
    if page is None:
        return None
    if page.get("hostname") and page.get("source") in ("hosted", "admiry"):
        return page["hostname"]
    return urlparse(page["url"]).hostname if page.get("url") else None


def get(conn: psycopg.Connection, page_id: str) -> dict | None:
    return _row(conn.execute(f"SELECT {PAGE_COLS} FROM page WHERE id = %s", (page_id,)).fetchone())


def set_owner_url(conn: psycopg.Connection, biz: str, url: str) -> dict:
    """The owner's own page becomes the live page. H25 (A3p-2 Task 3 round 2 review, supersedes R11/R5 and
    H22's own "any other live row is unpublished first"): a business has exactly *one* page row, ever,
    whatever `source` it currently has -- `ensure_page_for_kit`'s placeholder first, a hosted publish's row
    after, this function's own after that, in any order and any number of times -- never re-keyed and never a
    second row, so the tracking snippet an assistant already pasted (it carries the row's id) and every event
    `track.py` records against it keep working through every switch between an owner's own address and a
    hosted one, in either direction (R1/R2: naming an owner URL after a hosted publish used to insert a
    *second* row, which then collided with the first on `page_hostname_one` the moment the business went
    hosted again, and orphaned the snippet already pasted on the owner's own site).

    The same address changes nothing (the row keeps its check and its events); another one updates the row in
    place: `source` becomes 'owner', the url, `published`, the version up by one when an earlier address is
    replaced, the old check cleared, and with it what the hourly check last saw there (`code_seen` and its first look
    without the code, `code_missed_at`: A3p-5 batch 2 Q9, ruling R4-b; nothing has been seen at a new address yet) --
    `hostname` (Admiry's hosted one, H22) and the hosted `html`/its `page_version` history are left exactly as they
    are, so a later hosted republish keeps the same slug and the same versions (`hosted.hostname_for`'s own "any row
    for this business" lookup, and `proposals._publish_source`'s, both still find them on this same row -- H2's
    business's one page row is now this row, literally, not merely by rule). A concurrent first publish racing on
    `page_one_live` is not an error: the loser just reports whatever won."""
    row = conn.execute(
        f"SELECT {PAGE_COLS} FROM page WHERE business_id = %s "
        "ORDER BY (status = 'published') DESC, created_at DESC LIMIT 1",
        (biz,),
    ).fetchone()
    if row and row["source"] == "owner" and row["status"] == "published" and row["url"] == url:
        return dict(row)
    try:
        with conn.transaction():
            if row:
                page_id = row["id"]
                conn.execute(
                    "UPDATE page SET url = %s, source = 'owner', status = 'published', "
                    "version = CASE WHEN url IN ('', %s) THEN version ELSE version + 1 END, "
                    "check_json = NULL, checked_at = NULL, code_seen = NULL, code_missed_at = NULL WHERE id = %s",
                    (url, url, page_id),
                )
            else:
                page_id = db.new_id("pg")
                conn.execute(
                    "INSERT INTO page (id, business_id, source, url, status) VALUES (%s, %s, 'owner', %s, 'published')",
                    (page_id, biz, url),
                )
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name != "page_one_live":
            raise
        return current(conn, biz)  # a concurrent first-ever page racing on it: the loser reports whatever won
    return get(conn, page_id)


def save_check(conn: psycopg.Connection, page_id: str, report: dict) -> None:
    """The 14-rule report saved on the page row. H18 (A3p-2): the hourly page-check keeps its own counters in the
    same document (`check_json["reachable"]`, `jobs.page_check`) -- two writers, one document -- so a new report
    replaces the old one and keeps them. What the hourly check last saw of Admiry's code (`code_seen`, A3p-5 batch 2
    Q9, and its first look without it, `code_missed_at`, ruling R4-b) is older than this report, so it goes: until the
    hourly check looks again, the report's own tracking rule is what Admiry last saw (ruling R0-d,
    site_install.code_found)."""
    conn.execute(
        "UPDATE page SET check_json = %s::jsonb || CASE WHEN check_json ? 'reachable' "
        "THEN jsonb_build_object('reachable', check_json->'reachable') ELSE '{}'::jsonb END, checked_at = now(), "
        "code_seen = NULL, code_missed_at = NULL WHERE id = %s",
        (Jsonb(report), page_id),
    )


def saved_report(page: dict | None) -> dict | None:
    """A page row's saved 14-rule report (`check_json` without page-check's own `reachable` counters, H18), or None
    when the page has none yet -- nothing saved, or only the counters of a page the tools have not checked since
    it got its address."""
    doc = (page or {}).get("check_json")
    if not doc or "rules" not in doc:
        return None
    return {k: v for k, v in doc.items() if k != "reachable"}


def ensure_page_for_kit(conn: psycopg.Connection, biz: str) -> dict:
    """The kit needs a page id for the snippet before any page exists: a 'preview' placeholder row (no URL yet)
    that set_landing_page / publish_page later turn into the live page."""
    page = current(conn, biz)
    if page:
        return page
    page_id = db.new_id("pg")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status) VALUES (%s, %s, 'owner', '', 'preview')",
        (page_id, biz),
    )
    return get(conn, page_id)


def mark_kit_sent(conn: psycopg.Connection, page_id: str, plan_id: str) -> None:
    conn.execute("UPDATE page SET kit_plan_id = %s, kit_sent_at = now() WHERE id = %s", (plan_id, page_id))


# ---------------------------------------------------------------- the snippet


def snippet(page_id: str, base: str, tag: dict | None) -> str:
    attrs = f' data-page="{html.escape(page_id, quote=True)}"'
    if tag and tag.get("id"):
        attrs += f' data-gads="{html.escape(tag["id"], quote=True)}"'
        if tag.get("labels"):
            attrs += " data-labels='" + html.escape(json.dumps(tag["labels"]), quote=True) + "'"
        if tag.get("phone"):  # A3p-3 P26: the number the page prints, for Google's forwarding number
            attrs += f' data-phone="{html.escape(tag["phone"], quote=True)}"'
    return f'<script async src="{base}/t/admiry.js"{attrs}></script>'


def google_tag(conn: psycopg.Connection, biz: str) -> dict | None:
    """The owner's account tag (kind 'tag' → AW-…) and the conversion labels A3 wrote; None before A3 ran. With the
    account's call-from-site action (A3p-3 P26), `phone`: the business's phone exactly as the page prints it (the
    kit's facts), which Google's tag swaps for its forwarding number for a visitor from its ad."""
    rows = conn.execute("SELECT kind, label FROM conversion_action WHERE business_id = %s", (biz,)).fetchall()
    labels = {r["kind"]: r["label"] for r in rows if r["label"]}
    tag_id = labels.pop("tag", None)
    if not tag_id:
        return None
    out: dict = {"id": tag_id, "labels": labels}
    if labels.get("call_site"):
        latest = business.latest_knowledge(conn, biz)
        phone = (latest[1] if latest else {}).get("phone")
        if isinstance(phone, str) and phone.strip():
            out["phone"] = phone.strip()
    return out


# ---------------------------------------------------------------- the kit


def _consulted_plan(conn: psycopg.Connection, biz: str) -> dict:
    plan = plans.latest(conn, biz)
    if plan is None or "consultation" not in plan["sections"]:
        raise ToolError(NO_PLAN)
    return plan


def _city(location: str | None) -> str | None:
    """R2: the address's town (places.town: "500 University Ave, Palo Alto, CA 94301, USA" → "Palo Alto"). An address
    it cannot read keeps the old reading: the second-to-last comma-separated part when there are three or more parts,
    else the first ("Paris, France" → "Paris")."""
    found = places.town(location)
    if found:
        return found
    parts = [p.strip() for p in (location or "").split(",")]
    city = parts[-2] if len(parts) >= 3 else parts[0]
    return city or None


_ZIP = re.compile(r"\d{5}(-\d{4})?")


def _town_of(knowledge: dict) -> str | None:
    """The business's own town, for a plan located at a bare ZIP code (the founder's dev walk, 2026-10-08: "AC services
    in 94306"): its Google Maps listing's address, else its address, else the first place it serves that is not a ZIP
    code; None when none says one."""
    for where in ((knowledge.get("listing") or {}).get("address"), knowledge.get("address")):
        town = _city(where) if isinstance(where, str) and where.strip() else None
        if town and not _ZIP.fullmatch(town):
            return town
    served = knowledge.get("service_area") or []
    return next((a.strip() for a in served if isinstance(a, str) and a.strip() and not _ZIP.fullmatch(a.strip())), None)


def lead_services(kit: dict) -> list[str]:
    """The services a headline may name, lower-cased: the ones the owner picked (`facts.lead_services`), else the kit's
    one service phrase (a kit built before the picks were kept, or by hand)."""
    facts = kit.get("facts") or {}
    picked = facts.get("lead_services") or [facts.get("service")]
    return [s.strip().lower() for s in picked if isinstance(s, str) and s.strip()]


_STEM_SUFFIXES = ("ings", "ing", "ers", "er", "es", "ed", "s")
MIN_STEM = 4


def _stem(word: str) -> str:
    """`word` without a common suffix, when enough of it is left to mean something ("roofing" -> "roof", but
    "s" stays "s"): the loosened Message-match rule's own stemming (spec 2026-10-12 §3.6), not a linguist's."""
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM:
            return word[: -len(suffix)]
    return word


def names_a_service(kit: dict, text_low: str) -> bool:
    """Whether lower-cased text names one of the kit's lead services (the Message-match rule's service half): the
    service whole, or any of its words of MIN_STEM letters or more by its stem at the start of a word of the text
    ("roofing" -> "roof" meets "Roof Repair"; spec 2026-10-12 §3.6)."""
    words = [w.strip("“”\"'(),.:;!?&-") for w in text_low.split()]
    for service in lead_services(kit):
        if service in text_low:
            return True
        for part in service.split():
            if len(part) >= MIN_STEM and any(w.startswith(_stem(part)) for w in words):
                return True
    return False


def _message_of(consultation: dict, knowledge: dict) -> dict:
    """The part of the kit Message-match reads (`message_match`, `message_fix`): the service, the services a headline
    may name, the city, the areas the business serves, the offer and the headline pattern -- from the plan's
    consultation and the knowledge alone, so a saved report is judged again on the plan of now without the rest of
    the kit (`judged_report`). `areas` (spec 2026-10-12 §3.6, R4's loosened match): the knowledge's own served places
    that are not ZIP codes, plus its Google Maps listing's town when it has one and is not one either -- deduplicated,
    the city itself left out (that is Message-match's own place to check first). `service_area` may be stored as one
    text or a list (review fix round 1, Important 1): `profile._listed` reads either as whole entries, never a
    text's own letters (`profile._listed`'s own "one text is one area, not its letters")."""
    c = consultation
    service = c["business"].get("service") or (knowledge.get("services") or [{}])[0].get("name") or "your service"
    # the services the owner picked, each one a headline may name; the first leads the pattern (founder's dev walk,
    # 2026-10-07: six picks joined into one 86-character phrase left no 60-character headline that could pass)
    leads = [s for s in c["business"].get("services") or [] if isinstance(s, str) and s.strip()] or [service]
    city = _city(c["business"].get("location"))
    if city and _ZIP.fullmatch(city):  # a ZIP code is no city for a headline: the business's own town, when known
        city = _town_of(knowledge) or city
    areas = [a for a in profile._listed(knowledge.get("service_area")) if not _ZIP.fullmatch(a)]
    town = _city((knowledge.get("listing") or {}).get("address"))
    if town and not _ZIP.fullmatch(town):
        areas.append(town)
    areas = [a for a in dict.fromkeys(areas) if a != city]
    return {
        "facts": {"service": service, "lead_services": leads, "city": city, "areas": areas},
        "message": {"headline_pattern": f"{leads[0]} in {city}" if city else leads[0], "offer": c.get("offer")},
    }


def kit(conn: psycopg.Connection, biz: str) -> dict:
    """Everything an assistant needs to build the page for this business's ads (spec §4). Its `install` part is how the
    code gets onto the owner's own site -- the steps the plan's row shows, for the page the ads use (A3p-5 batch 2:
    site_install.kit_part, ruling R2-b), None with no website of its own (ruling R1-f) -- and its facts name the owner's
    own site, read or not (profile.own_site, Q13): a booking page given that way is the page's booking link
    (page_builder)."""
    plan = _consulted_plan(conn, biz)
    latest = business.latest_knowledge(conn, biz)
    knowledge = latest[1] if latest else {}
    c = plan["sections"]["consultation"]
    ch = plans.primary(plan["sections"])  # the page sells what the primary's ads say (Google Search's)
    tree = c["business"].get("category") or "fallback"
    recipe = load_recipe(tree)
    cards = plans.cards({**plan, "business_name": plans.name_for(knowledge) or plans.address_for(knowledge)})
    said = _message_of(c, knowledge)
    service, leads, city = said["facts"]["service"], said["facts"]["lead_services"], said["facts"]["city"]
    areas = said["facts"]["areas"]
    radius = c["geo"].get("radius_miles")
    settings = config.load()
    page = ensure_page_for_kit(conn, biz)
    tag = google_tag(conn, biz)
    facts = {
        "name": plans.name_for(knowledge),
        "category": knowledge.get("category_detail") or tree,
        "service": service,
        "lead_services": leads,
        "services": [s.get("name") for s in knowledge.get("services") or []],
        "location": c["business"].get("location"),
        "city": city,
        "areas": areas,  # R4's loosened Message-match: any area the business serves, not only the city
        "area": (f"within {radius:g} miles" if radius else "the area the business serves"),
        "phone": knowledge.get("phone"),
        "hours": knowledge.get("hours"),
        "offer": c.get("offer"),
        "usp": list(knowledge.get("usp") or []),
        "pricing_hints": list(knowledge.get("pricing_hints") or []),
        "reviews_summary": knowledge.get("reviews_summary"),
        "website": profile.own_site(knowledge),
    }
    message = {
        "headline_pattern": said["message"]["headline_pattern"],
        "offer": c.get("offer"),
        "themes": list(ch.get("themes") or []),
        "headlines": list(ch.get("headlines") or []),
        "descriptions": list(ch.get("descriptions") or []),
        "example_searches": list(cards.get("rationale", {}).get("example_searches") or []),
        "counts_as_result": c.get("conversions"),
    }
    rules = {
        "google": GOOGLE_RULES,
        "mobile": "Design for a phone first; the phone number is a tap-to-call button; the call button stays visible.",
        "speed": "One HTML file, no frameworks, images under 200 KB each; the page should load in about two seconds on a phone.",
        "sensitive": recipe["sensitive"],
        "sensitive_note": (
            "This kind of business is a sensitive category for Google: no enhanced conversions, no uploads of who "
            "became a customer; the page's form asks nothing personal beyond name, phone, time and service."
            if recipe["sensitive"]
            else None
        ),
        "required_lines": recipe["required_lines"],
        "banned_words": recipe["banned"],
        "form_fields": recipe["form_fields"],
        "form_intent": recipe.get("form_intent") or "consult",  # A3p-3 P25: the form's data-admiry-intent
        "form_rules": recipe["form_rules"],
        "consent": "If the business will text people back, add an unchecked, optional consent box and keep the answer.",
        "reviews": "Only reviews the business really received, with the reviewer's first name and the source; never written by AI.",
    }
    # embedded on the owner's own site, no cookies: the agent host (AGENT_PUBLIC_BASE_URL), not the web host
    form_action = f"{settings.public_base_url}/t/form/{page['id']}"
    tracking = {
        "snippet": snippet(page["id"], settings.public_base_url, tag),
        "where": TRACKING_WHERE,
        "form_action": form_action,
        # I4: the script sends only a form whose action is this address (or marked data-admiry="form"), and the
        # address itself answers a scripts-off post with a thank-you page, so the form needs no backend.
        "form": (
            f'Make the form <form method="post" action="{form_action}"> with inputs named '
            f"{', '.join(recipe['form_fields'])}: Admiry's script sends it and shows the thank-you in its place, "
            "and with scripts off the visitor still gets a thank-you page. The script leaves every other form "
            "on the page alone."
        ),
        "mark_links": 'Add data-admiry="call" to the phone button, data-admiry="book" to a booking link; tel: and mailto: links are counted without a mark.',
        # A3p-3 P25-P27 (plan C9): the form's intent, a booking completed in a Calendly embed, the phone as printed
        "intent": (
            f'Mark the form data-admiry-intent="{recipe.get("form_intent") or "consult"}": consult (a question or a '
            "consultation), appointment (a request for a visit) or quote (a price for a job). An appointment request "
            "counts as its own result; each form counts once."
        ),
        "booking": (
            "A Calendly booking page embedded on the page counts a booking made there as booked online; any other "
            "booking link counts its click."
        ),
        "phone": (
            "Print the business phone exactly as the facts give it: for a visitor from a Google ad, Google shows its "
            "own forwarding number in its place, so the call counts."
        ),
        "google_tag": bool(tag),
        "note": (
            "Calls, forms and bookings on this page count as results in Google Ads and reach the owner as leads."
            if tag
            else "Admiry counts calls, forms and bookings now; they also count in Google Ads once the account is linked."
        ),
    }
    return {
        "plan_id": plan["id"],
        "page_id": page["id"],
        "tree": tree,
        "cta": recipe["cta"],
        "facts": facts,
        "message": message,
        "structure": recipe["sections"],
        "faq": recipe["faq"],
        "rules": rules,
        "tracking": tracking,
        "install": site_install.kit_part(knowledge, tracking["snippet"], page),
        "checklist": CHECKLIST,
        "hosting": hosting_section(),
        "bundle": bundle_section(),
    }


def hosting_section() -> dict:
    """The kit's hosting section (A3p-3 P15, plan Q12): what free hosting on Admiry needs, in the sentence an assistant
    tells the owner before it builds on Admiry hosting -- a page on the owner's own site, or anywhere else, is not
    affected (P12)."""
    from . import hosting_minimum  # it imports ads_run, which imports this module: read when the kit is built

    return {
        "minimum_usd": hosting_minimum.MIN_MONTHLY_USD,
        "free_while": hosting_minimum.MINIMUM,
        "say_first": (
            "Before you build a page Admiry will host, tell the owner: " + hosting_minimum.MINIMUM + " A page on "
            "the owner's own site, or anywhere else, is not affected."
        ),
    }


def bundle_section() -> dict:
    """The kit's section on a whole site uploaded to Admiry (A3p-3 §6.1, §6.2, P31-P33; plan B14): what Admiry hosts
    (built files, never a server), the contract a build meets, its limits, and the steps of an upload -- so the
    assistant knows before it builds. The snippet and the form address are the kit's own (`tracking`)."""
    from . import bundles  # it imports hosted, which imports this module: read when the kit is built

    return {
        "what": (
            "A site of several pages, or a framework's build (a Next.js static export, Vite, Astro, Hugo), can be "
            "hosted on Admiry whole: an assistant connected to Admiry uploads a zip of its build output. Admiry hosts "
            "the built files only, never a server: a site that needs one (server rendering, API routes, sign-in, a "
            "database) lives elsewhere, and its address is given to Admiry instead."
        ),
        "build": [
            (
                "Upload the build output, not the project: the files inside out/ (Next.js), dist/ (Vite, Astro) or "
                "public/ (Hugo), with index.html at the top; a 404.html is optional."
            ),
            "/about/ serves about/index.html; /about serves about.html or about/index.html.",
            (
                "Next.js: a static export. next.config.js: module.exports = { output: 'export', images: { unoptimized: "
                "true }, trailingSlash: true }; basePath and assetPrefix stay unset, since the site lives at the root of "
                "its own address. A static export has no server actions, no Proxy (middleware), no incremental "
                "regeneration, no redirects, rewrites or headers in the config, no cookies and no routes that read the "
                "request."
            ),
            (
                "Every page's words are in its HTML (prerendered, as a static export is), and the home page says what "
                "the business does in a few paragraphs."
            ),
            (
                "Every HTML page carries the tracking snippet above as a plain <script> tag -- in Next.js, inside "
                "app/layout's <head>, not with next/script, which leaves no tag in the HTML."
            ),
            "Forms post to the form address above, marked with their intent; the call button is marked.",
            (
                "Scripts load only from the site itself, Admiry and Google's tag hosts; frames show only booking pages "
                "and Google maps; forms post only to the site or Admiry. Libraries are bundled into the site, never "
                "loaded from a CDN, and no element carries inline code (onclick=, onload=)."
            ),
            "/privacy and /terms are Admiry's notices on every hosted site: link to them, and build no page there.",
        ],
        "limits": bundles.LIMITS,
        "limits_text": (
            f"{bundles.LIMITS['unpacked_mb']} MB unpacked, {bundles.MAX_FILES} files, {bundles.LIMITS['file_mb']} MB a "
            f"file, 1 MB an HTML file, images {bundles.LIMITS['image_mb']} MB and "
            f"{bundles.LIMITS['image_megapixels']} megapixels, paths of {bundles.MAX_PATH_CHARS} characters and "
            f"{bundles.MAX_DEPTH} folders at most; file types {bundles.TYPE_LIST}."
        ),
        "upload": [
            "Build the site.",
            "Zip the files inside the build folder, not the folder: cd out && zip -r ../site.zip .",
            (
                "Ask Admiry for an upload link and run the command it gives: curl --fail-with-body -T site.zip "
                "<upload link>. A link takes one upload in the next 30 minutes."
            ),
            (
                "The answer lists every problem to fix (file, rule, message, fix): fix them in the source, build, and "
                "ask for a new link. Once the site passes, the answer is a private preview link and a token to publish "
                "with the owner's yes."
            ),
        ],
    }


CHECKLIST = [  # the owner's words (spec 2026-10-12 §3.5): the card, the e-mail and get_landing_page's check share them
    {
        "id": "reachable",
        "title": "Opens securely and fast",
        "why": "Google will not send clicks to a page that is down or not on https.",
    },
    {"id": "mobile", "title": "Works on a phone", "why": "Most people who tap an ad are on a phone."},
    {"id": "speed", "title": "Loads quickly", "why": "A slow page loses people before they call."},
    {"id": "phone", "title": "Tap-to-call number at the top", "why": "Most calls from ads come from the first screen."},
    {"id": "one_goal", "title": "Keeps visitors on the page", "why": "Fewer ways to wander off means more calls."},
    {
        "id": "message_match",
        "title": "Headline matches the ad",
        "why": "People check they landed in the right place; Google scores the match.",
    },
    {
        "id": "tracking",
        "title": "Admiry's code is on the page",
        "why": "Lets Google count the calls and forms your ads bring.",
    },
    {"id": "beacon", "title": "Seen working once", "why": "Admiry waits for one real visit through the code."},
    {"id": "privacy", "title": "Privacy notice by the form", "why": "Required when a form collects details."},
    {"id": "terms", "title": "Terms link in the footer", "why": "Some ad platforms require it."},
    {
        "id": "contact",
        "title": "Phone or e-mail in the footer",
        "why": "Visitors and Google expect a way to reach you.",
    },
    {
        "id": "facts",
        "title": "Real facts about the business",
        "why": "Name, area, hours and services make the page credible.",
    },
    {"id": "compliance", "title": "No claims the rules forbid", "why": "Keeps the ads from being disapproved."},
    {"id": "freshness", "title": "Matches your current plan", "why": "The page and the ads say the same thing."},
]
OWNER_SOURCE = "owner"  # a page the owner owns: message-match is advice there (spec 2026-10-12 R4, R9)


# ---------------------------------------------------------------- the check


BLOCKING = ("reachable", "phone", "message_match", "tracking", "privacy", "compliance")
MAX_HTML = (
    2_000_000  # inflated bytes read, at most (C2, raised 2026-10-12); a longer page is judged on its first MAX_HTML
)
SPEED_BYTES = 300_000  # above it the `speed` rule warns (the cap and the warning were one number before 2026-10-12)
# The check's body read (a deadline checked between chunks) and `reachable`'s whole fetch (a deadline on a worker
# thread, H34). httpx's own 5 s bounds each operation only: the connect, the TLS handshake, each write, each read.
FETCH_BUDGET_S = 10.0
HEADLINE_CHARS = 300
LINK_CHARS = 300  # the words of a link read for "terms": its first 300 characters say what the link is
# The Contact rule (M1 Task 4) looks at the end of the page: the last CONTACT_TAIL characters of what a visitor reads.
CONTACT_TAIL = 3_000
# A local part, "@", a domain with at least one dot, a top-level name. Every run is bounded, so a long run of nothing
# in particular (the check reads hostile pages) is never quadratic.
_EMAIL = re.compile(r"[\w.+-]{1,64}@(?:[\w-]{1,63}\.){1,5}[a-z]{2,24}\b", re.IGNORECASE)
TERMS_FIX = "Add a Terms link in the footer."
CONTACT_FIX = "Put the business's phone or e-mail in the footer."
# Admiry has no number to look for, so only an address can be told from anyone's (no owner sentence names a tool:
# M1 Task 7)
NO_PHONE_FIX = "Put the business's e-mail in the footer, or tell Admiry the business phone so it can look for it there."
# the same at the address of a page Admiry wrote: its footer is Admiry's template, which shows the business phone and
# never an address, so the phone is the one way -- and a new version of the page carries it
NO_PHONE_FIX_HOSTED = (
    "Tell Admiry your business phone; once your page is updated and published again, its footer shows it."
)
# The booking services Admiry knows: one list, which the listing rules share too (booking_hosts, ruling R1-e)
BOOKING_HOSTS = booking_hosts.BOOKING_HOSTS


def _digits(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())[-10:]


def _registrable(host: str) -> str:
    """The last two dot-separated labels ("www.brightsmile.example" -> "brightsmile.example"): a subdomain
    still counts as the same site when comparing a redirect target or an outbound link to the page's own host."""
    parts = (host or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _link_host(url: str) -> str | None:
    """The host of one link on the page, or None when it does not parse (I11: urlparse raises ValueError on
    `https://[oops`, which crashed the whole check); an unparsable link is skipped."""
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _goes_nowhere(href: str) -> bool:
    """Whether a link's address is a placeholder that leads nowhere: empty, a bare "#", or a javascript: call. A
    browser drops spaces, tabs and line breaks from an address, so "java<tab>script:" is one too. A "#terms"
    fragment naming a section of the page is a link."""
    plain = re.sub(r"[\x00-\x20]", "", href).lower()
    return plain in ("", "#") or plain.startswith("javascript:")


def _reach(phone: str, tail: str) -> str | None:
    """How a visitor could reach the business from `tail`, the last stretch of what the page says (`_Page.flow`):
    "phone" when the business's own number is in it -- its digits however they are written (`phone` is `_digits`'
    last ten) -- "e-mail" when an address is (a mailto: link counts: the flow carries its target), else None."""
    if phone and phone in "".join(ch for ch in tail if ch.isdigit()):
        return "phone"
    return "e-mail" if _EMAIL.search(tail) else None


class _Page(crawl._Text):
    """crawl's own HTML parser (tags, scripts and styles out, so a banned word inside a <script> or a raw URL
    never counts as page text), plus the first <h1>'s text and the <title>, each cut at HEADLINE_CHARS: one
    linear pass over the page. C2: the regexes this replaces, `<h1[^>]*>(.*?)</h1>` and the same for <title>,
    are quadratic on unclosed tags (300 KB of `<h1>` held the check for about a minute).

    The same pass answers the two rules M1 Task 4 adds, and for the same reason. `terms`: whether any link (an <a>
    whose address goes somewhere -- not empty, not a bare "#", not a javascript: call -- outside a script or a
    noscript) says "terms" in its address or in its first LINK_CHARS characters of words -- decided as the words
    arrive, so a link left unclosed still counts and an unclosed one costs nothing more than a closed one (a new <a>
    ends the one before, as a browser does). `flow`: what a visitor reads, in order, with the target of each tel: and
    mailto: link set where the link sits -- the Contact rule reads the last CONTACT_TAIL characters of it."""

    def __init__(self) -> None:
        super().__init__()
        self.h1, self._h1 = "", "before"  # before | in | after: only the first <h1> is the headline
        self.terms = False
        self._words: str | None = None  # the open link's words so far; None while no link that goes somewhere is open
        self.flow: list[str] = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "h1" and self._h1 == "before":
            self._h1 = "in"
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            self._words = "" if not _goes_nowhere(href) and not self._skip else None
            if self._words is not None:
                self.terms = self.terms or "terms" in href.lower()
                if href.lower().startswith(("tel:", "mailto:")):
                    self.flow.append(f" {href} ")

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "h1" and self._h1 == "in":
            self._h1 = "after"
        elif tag == "a":
            self._words = None

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title + data)[:HEADLINE_CHARS]
        elif not self._skip:
            self.parts.append(data)
            self.flow.append(data)
            if self._h1 == "in":
                self.h1 = (self.h1 + data)[:HEADLINE_CHARS]
            if self._words is not None and not self.terms and len(self._words) < LINK_CHARS:
                self._words = (self._words + data)[:LINK_CHARS]
                self.terms = "terms" in self._words.lower()


@dataclass
class _Fetched:
    """One fetch of a page, the check's way (`_fetch`): the last answer's status (None before any), the body read
    (b"" unless a page came back), its charset, the address the fetch ended at, the seconds it took (0.0 unless a
    page came back), and -- when no page came back -- why, as the Reachable rule's own (detail, fix) sentences. The
    final answer's headers (the platform is read from them too, site_platform.detect), and whether its body read
    stopped at the deadline before the whole body came (`cut`: what came may not be the whole page)."""

    status: int | None
    final: str
    raw: bytes = b""
    charset: str = "utf-8"
    seconds: float = 0.0
    failed: tuple[str, str] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    cut: bool = False


def _fetch(
    url: str,
    host: str,
    *,
    cap: int,
    transport: httpx.BaseTransport | None,
    resolver: Callable[[str], list[str]],
    clock: Callable[[], float],
    answered: Callable[[int], None] | None = None,
) -> _Fetched:
    """GET `url` (whose public host `host` the caller already vetted) the way the check reads a page: one
    same-domain redirect hop allowed, every hop vetted as a public address before it is fetched; the body
    streamed and cut at `cap` inflated bytes and at FETCH_BUDGET_S -- and not read at all when `cap` is 0 (the
    answer's status and headers are all `reachable` asks for). httpx's 5 s is per operation -- the connect, the TLS
    handshake, each write, each read -- so nothing here bounds a hop as a whole: a server that keeps every read under
    5 s (a status line and then one header byte at a time) holds it as long as it likes. The body read stops at the
    deadline; `reachable` puts a deadline on the whole fetch. A transport error, a hop to a private, malformed or
    foreign address, or a second redirect is a `failed` result, never an exception -- the check always reports, and
    page-check (`reachable`) always counts. `answered` hears the final answer's status line as soon as it comes,
    before any of its body (`_look`: up or down is that line alone)."""
    started = clock()
    deadline = started + FETCH_BUDGET_S
    got = _Fetched(status=None, final=url)
    with httpx.Client(
        transport=transport,
        timeout=5.0,
        follow_redirects=False,
        headers={"User-Agent": crawl.UA + " (page check)", "Accept-Encoding": crawl.ACCEPT_ENCODING},
    ) as http:
        for _ in range(2):
            try:
                with http.stream("GET", got.final) as r:
                    got.status = r.status_code
                    location = r.headers.get("location") if r.is_redirect else None
                    if not location:
                        got.headers = r.headers
                        if answered is not None:
                            answered(r.status_code)
                    if not location and cap > 0:
                        # C2: streamed, inflated by crawl.read_capped under its cap, never read whole into memory
                        got.raw = crawl.read_capped(r, cap, deadline=deadline, clock=clock)
                        got.charset = r.encoding or "utf-8"
                        got.cut = len(got.raw) < cap and clock() >= deadline
            except httpx.HTTPError as e:
                got.failed = (
                    f"Could not open the page: {type(e).__name__}.",
                    "Make sure the address opens in a phone browser.",
                )
                return got
            if not location:
                got.seconds = clock() - started
                return got
            # C1: every hop is vetted like the address itself (a public host) before anything fetches it
            try:
                target = urljoin(got.final, location)
                crawl.public_host(target, resolver=resolver)
            except ValueError as e:  # crawl.CrawlError is one: private, unresolvable or malformed
                why = str(e) if isinstance(e, crawl.CrawlError) else crawl.INVALID_URL
                why = why.replace("website_url must be", "it must be")
                got.failed = (
                    f"The address forwards to an address we will not fetch ({why}).",
                    "Give the ads the page's final public address.",
                )
                return got
            if _registrable(urlparse(target).hostname or "") != _registrable(host):
                got.failed = (
                    f"The page sends visitors to another site ({urlparse(target).hostname}).",
                    "Ads must land on the page itself, not a redirect to another site.",
                )
                return got
            got.final = target
    # Both hops were same-domain redirects: the budget ran out before a page (or a foreign redirect, or a transport
    # error) came back. `status`/`final` still hold the last response's code and Location target.
    got.failed = (
        f"The address redirects more than once ({url} → {got.final}).",
        "Give the ads the final address of the page, not one that forwards.",
    )
    return got


def reachable(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Callable[[str], list[str]] = crawl.resolve,
    clock: Callable[[], float] = time.monotonic,
    budget_s: float = FETCH_BUDGET_S,
) -> tuple[bool, str]:
    """Whether the page at `url` opens -- the hourly page-check's one question of a page Admiry hosts (plan H10, spec
    §8). The check's own fetch (`_fetch`: every hop vetted as a public address, one redirect within the site) with no
    body read -- a 2xx status line at the end of it is up, whatever the body then does: (True, "HTTP 200"); otherwise
    (False, what went wrong -- the answer's status, the Reachable rule's own detail, or why the address cannot be
    fetched at all). Unlike the check's Reachable rule, plain http or a slow answer is not a failure here: the
    question is whether the page is up, not whether it is fit for a launch. Never raises for the page's sake. An owner
    page is fetched with its body (`fetch_body`), whose up or down is this one.

    Time (ruling H34): the whole fetch -- the name lookups, both hops -- runs on a worker thread and gets `budget_s`
    (FETCH_BUDGET_S, 10 s): no answer by then is (False, "No answer within 10 s."), whatever httpx's per-operation
    5 s would allow (see `_fetch`). The worker is a daemon thread, left behind: it ends when httpx's own timeouts end
    its fetch -- or with the process, which it never holds open (a ThreadPoolExecutor's worker would be joined at
    exit, keeping `python -m agent.tick` running for as long as a server kept that fetch going)."""
    up, detail, _page = _look(url, read=False, transport=transport, resolver=resolver, clock=clock, budget_s=budget_s)
    return up, detail


def fetch_body(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Callable[[str], list[str]] = crawl.resolve,
    clock: Callable[[], float] = time.monotonic,
    budget_s: float = FETCH_BUDGET_S,
) -> tuple[bool, str, str | None]:
    """The hourly page-check's fetch of an owner page (A3p-5 batch 2, plan Q9; spec W15), and the watch of a pending
    install's (ruling R3-a): `reachable`'s fetch with the body read -- the check's own fetch at the check's limits
    (MAX_HTML, FETCH_BUDGET_S), never a fetcher of its own -- as (up, detail, page). (up, detail) is `reachable`'s
    answer, the final status line's alone: a body that breaks off or comes too slowly never makes a page that opened
    down. `page` is the page as text when one came back to judge with the check's tracking rule (`tracking_found`): a
    2xx answer whose body came whole within the budget and is a page -- not a bot challenge (a 403, 429 or 503 is no
    2xx, and a body with no `<html` is no page) -- else None: an answer that is no page says nothing of the code.
    Never raises for the page's sake (`_look`)."""
    return _look(url, read=True, transport=transport, resolver=resolver, clock=clock, budget_s=budget_s)


def tracking_found(html: str, page_id: str) -> bool:
    """The check's tracking rule on its own (A3p-5 batch 2, Q9): Admiry's script with this page's token is in the page
    -- anywhere in it (the kit says inside <head>, Q4; the rule finds it wherever it is). The check runs it on the page
    it fetched, page-check on the page it reads every hour."""
    return f'data-page="{page_id}"' in html and "/t/admiry.js" in html


def _answered(status: int) -> bool:
    """A final answer's status line that says the page opens: a 2xx."""
    return 200 <= status < 300


def _look(
    url: str,
    *,
    read: bool,
    transport: httpx.BaseTransport | None,
    resolver: Callable[[str], list[str]],
    clock: Callable[[], float],
    budget_s: float,
) -> tuple[bool, str, str | None]:
    """`reachable` and `fetch_body` (`read`): the fetch on a worker thread, all of it within `budget_s` (ruling H34).
    The answer is the final status line's as soon as that line comes -- (up, "HTTP <status>") -- and, when `read` and
    the page opened, the page once its body came (`_page_text`); a body still coming when the budget runs out, or
    breaking off with anything the fetch did not expect, leaves the page up with no page to judge (logged). No
    status line within the budget is (False, "No answer within <budget> s.", None)."""
    answers: queue.Queue = queue.Queue()

    def line(status: int) -> None:
        answers.put(("line", status))

    def fetch() -> None:
        try:
            answers.put(
                (
                    "done",
                    _fetch_page(url, read=read, transport=transport, resolver=resolver, clock=clock, answered=line),
                )
            )
        except Exception as e:  # noqa: BLE001 - handed to the caller, which raises it as its own
            answers.put(("raised", e))

    threading.Thread(target=fetch, name="page-reachable", daemon=True).start()
    ends = time.monotonic() + budget_s
    status: int | None = None
    while True:
        try:
            kind, value = answers.get(timeout=max(0.0, ends - time.monotonic()))
        except queue.Empty:
            if status is None:
                return False, f"No answer within {budget_s:g} s.", None
            return _answered(status), f"HTTP {status}", None  # the status line came; the body did not, in time
        if kind == "done":
            return value
        if kind == "raised":
            if status is None:
                raise value
            log.warning("page fetch: the body of %s broke off (%s)", url, type(value).__name__)
            return _answered(status), f"HTTP {status}", None
        status = value
        if not read or not _answered(status):
            return _answered(status), f"HTTP {status}", None


def _fetch_page(
    url: str,
    *,
    read: bool,
    transport: httpx.BaseTransport | None,
    resolver: Callable[[str], list[str]],
    clock: Callable[[], float],
    answered: Callable[[int], None],
) -> tuple[bool, str, str | None]:
    """`_look`'s fetch, on its worker thread: the check's own fetch (`_fetch`), the body read only when `read`.
    `answered` hears the final answer's status line as soon as it comes, before any of its body."""
    try:
        host = crawl.public_host(url, resolver=resolver)
    except crawl.CrawlError as e:
        return False, str(e).replace("website_url", "the page address"), None
    heard: list[int] = []

    def line(status: int) -> None:
        heard.append(status)
        answered(status)

    cap = MAX_HTML + 1 if read else 0
    got = _fetch(url, host, cap=cap, transport=transport, resolver=resolver, clock=clock, answered=line)
    if not heard:  # no final answer came: the fetch failed before one, and says why
        return False, got.failed[0] if got.failed else f"HTTP {got.status}", None
    up = _answered(heard[-1])
    return up, f"HTTP {heard[-1]}", _page_text(got) if read and up else None


def _page_text(got: _Fetched) -> str | None:
    """The page a fetch brought back, as text, when there is one to judge (A3p-5 batch 2, Q9): its body came whole --
    it neither broke off (`failed`) nor stopped at the deadline (`cut`) -- and is a page, with `<html` in it in any
    case: a bot challenge's answer, or one that is no page, is none. Judged on its first MAX_HTML, as the check is."""
    if got.failed or got.cut:
        return None
    text = crawl.decode(got.raw[:MAX_HTML], got.charset)
    return text if "<html" in text.lower() else None


def check(
    url: str,
    page: dict,
    kit: dict,
    *,
    conn: psycopg.Connection,
    transport: httpx.BaseTransport | None = None,
    resolver: Callable[[str], list[str]] = crawl.resolve,
    clock: Callable[[], float] = time.monotonic,
    extra_rules: tuple[str, ...] = (),
) -> dict:
    """The 14-rule landing-page check (CHECKLIST): fetch `url` (one same-domain redirect hop allowed, every hop
    vetted as a public address before it is fetched; the body streamed and cut at MAX_HTML inflated bytes and
    at FETCH_BUDGET_S) and judge it against `kit` (the facts, the message and the rules for this business) and
    `page` (its id, for the tracking snippet and the visit beacon; its kit_plan_id, for freshness). Each rule's
    `ok` is True (pass), False (fails; a BLOCKING rule failing fails the whole report) or None (a warning:
    reported, never blocking the report's own `ok`). The report's `ok` is the BLOCKING rules' verdict only, since
    the stored report is every channel's: `extra_rules` (rule ids a caller needs beyond BLOCKING -- a channel's
    `Capabilities.page_rules`, M0 Task 4) never change it or any rule; an id the check has no rule for is logged.
    The caller judges its own rules on the report with `passed`, where they must pass outright -- which is why
    `terms` and `contact` (M1 Task 4: NewsBreak asks both of a landing page) are True or a warning, never False.
    A bad or private `url` is refused before any network call, as a ToolError worded like start's
    website_url (ToolError, never CrawlError: this can run inside a tool's `prepare`). `fetched` says what the fetch
    found: its status, size, time and final address, and the platform the page is built on when a page came back
    (`platform`, site_platform.detect: A3p-5 batch 2, ruling R1-c), else None."""
    try:
        host = crawl.public_host(url, resolver=resolver)
    except crawl.CrawlError as e:
        raise ToolError(str(e).replace("website_url", "the page address")) from None

    rules: list[dict] = []

    def rule(id_: str, ok: bool | None, detail: str, fix: str | None = None) -> None:
        entry = next(c for c in CHECKLIST if c["id"] == id_)
        rules.append({"id": id_, "title": entry["title"], "why": entry["why"], "ok": ok, "detail": detail, "fix": fix})

    # fetch: one same-domain redirect hop; a foreign or private redirect or a transport error is a rule
    # failure, not an exception -- the report should always come back so the owner sees what is wrong. The cap is
    # one byte past MAX_HTML, so "over" is known. Two same-domain redirects are a failure too, said plainly, rather
    # than every later rule judging an empty page as if it had really been fetched (`fetched` below keeps the last
    # answer's code and Location target).
    got = _fetch(url, host, cap=MAX_HTML + 1, transport=transport, resolver=resolver, clock=clock)
    status, charset, final, seconds = got.status, got.charset, got.final, got.seconds
    over, raw = len(got.raw) > MAX_HTML, got.raw[:MAX_HTML]
    if got.failed:
        rule("reachable", False, *got.failed)
    else:
        ok = status == 200 and final.startswith("https://")
        rule(
            "reachable",
            ok,
            f"HTTP {status}, {'https' if final.startswith('https://') else 'not https'}, {seconds:.1f} s.",
            None if ok else "The page must answer 200 over https.",
        )

    body = crawl.decode(raw, charset)
    # the platform the site is built on, read from the page this fetch brought back (A3p-5 batch 2, W9; ruling R1-c):
    # the report's `fetched.platform`, which a saved check of the owner's own site records once
    # (site_install.record_platform)
    platform = site_platform.detect(body, got.headers) if not got.failed and _answered(status or 0) else None
    parsed = _Page()
    parsed.feed(body)
    text = _squash(" ".join(parsed.parts))
    low = text.lower()
    html_low = body.lower()
    # R8: the *full* digit string of the visible text, not _digits()'s last-10-digits truncation (right for
    # normalizing a single phone number, wrong here -- any page with hours, a price or a ZIP after the phone
    # number would otherwise fail these text checks even though the number really is on the page).
    text_digits = "".join(ch for ch in text if ch.isdigit())

    has_viewport = re.search(r'name=["\']viewport["\']', html_low) is not None
    rule(
        "mobile",
        has_viewport or None,
        "viewport line present" if has_viewport else "No viewport line: phones will show a shrunken desktop page.",
        None if has_viewport else 'Add <meta name="viewport" content="width=device-width, initial-scale=1"> in <head>.',
    )

    heavy = over or len(raw) > SPEED_BYTES or seconds > 2.5
    size = f"more than {MAX_HTML // 1000} KB" if over else f"{len(raw) // 1024} KB"
    rule(
        "speed",
        None if heavy else True,
        f"{size}, {seconds:.1f} s.",
        "Keep the HTML under 300 KB and the page loading in about two seconds." if heavy else None,
    )

    phone = _digits(kit["facts"].get("phone"))
    tels = re.findall(r'href=["\']tel:([^"\']+)', body, re.IGNORECASE)
    tel_digits = [_digits(t) for t in tels]
    if not phone:
        rule(
            "phone",
            None,
            "Admiry has no phone number for the business.",
            "Tell Admiry the business phone so calls can be counted.",
        )
    elif phone in tel_digits:
        rule("phone", True, "Tap-to-call link with the business number.")
    elif tels:
        rule(
            "phone",
            False,
            f"The phone on the page ({tels[0]}) is not the business number ({kit['facts']['phone']}).",
            "That is not the business number: use the business's own number as the tel: link, or tell Admiry "
            "the new number.",
        )
    elif phone in text_digits:
        rule(
            "phone",
            False,
            "The number is on the page but not as a tap-to-call link.",
            f'Make it <a href="tel:+1{phone}">…</a>.',
        )
    else:
        rule(
            "phone",
            False,
            "The business phone is not on the page.",
            f"Put {kit['facts']['phone']} at the top as a tap-to-call link.",
        )

    ext = [
        h
        for h in re.findall(r'href=["\'](https?://[^"\']+)', body, re.IGNORECASE)
        if (link_host := _link_host(h))
        and _registrable(link_host) != _registrable(host)
        and not any(b in h for b in BOOKING_HOSTS)
        and "maps.google" not in h
        and "privacy" not in h.lower()
        and "terms" not in h.lower()  # M1 Task 4: the Terms link the kit asks for is no more a link away than privacy
    ]
    nav = "<nav" in html_low
    rule(
        "one_goal",
        (not nav and len(ext) <= 2) or None,
        f"{len(ext)} link(s) to other sites{', a menu' if nav else ''}.",
        "Remove the menu and links away from the page; keep call, book, map, privacy and terms."
        if nav or len(ext) > 2
        else None,
    )

    owner = page.get("source") == OWNER_SOURCE
    h1 = _squash(parsed.h1) or _squash(parsed.title)
    mm = message_match(kit, h1)
    rule(
        "message_match",
        True if mm else (None if owner else False),
        f"Headline: “{h1[:80]}”.",
        None if mm else message_fix(kit),
    )

    has_script = tracking_found(body, page["id"])
    rule(
        "tracking",
        has_script,
        "Admiry's script is on the page." if has_script else "Admiry's script with this page's token is missing.",
        None if has_script else TRACKING_FIX,
    )

    seen = (
        conn.execute("SELECT 1 FROM page_event WHERE page_id = %s AND kind = 'visit' LIMIT 1", (page["id"],)).fetchone()
        is not None
    )
    rule(
        "beacon",
        True if seen else None,
        "Admiry has seen the page open." if seen else "Not opened yet since the script went on.",
        None if seen else "Open the page once on your phone; Admiry will see it.",
    )

    has_form = "<form" in html_low
    priv = (
        re.search(r"privacy", body, re.IGNORECASE) is not None
        and re.search(r'href=["\'][^"\']*privacy', body, re.IGNORECASE) is not None
    )
    rule(
        "privacy",
        priv or not has_form,
        "Privacy notice link present."
        if priv
        else ("No form, none needed." if not has_form else "The page has a form but no privacy notice link."),
        None if (priv or not has_form) else "Add a link to a privacy notice near the form.",
    )

    # M1 Task 4 (plan P8): the two rules NewsBreak asks of a landing page. Each is True or a warning (None), never False:
    # they block nothing for Google, and the stored `ok` above stays the BLOCKING rules' verdict. A channel whose
    # `page_rules` name them judges them with `passed`, where a warning is not enough.
    rule(
        "terms",
        True if parsed.terms else None,
        "Terms of use link present." if parsed.terms else "No link to terms of use on the page.",
        None if parsed.terms else TERMS_FIX,
    )
    reach = _reach(phone, _squash(" ".join(parsed.flow))[-CONTACT_TAIL:])
    if reach == "phone":
        rule("contact", True, "The business phone is in the footer.")
    elif reach:
        rule("contact", True, "An e-mail address is in the footer.")
    else:
        written = page.get("source") == "admiry" and bool(page.get("hostname")) and host == page["hostname"]
        rule(
            "contact",
            None,
            "No phone or e-mail for the business in the footer.",
            CONTACT_FIX if phone else NO_PHONE_FIX_HOSTED if written else NO_PHONE_FIX,
        )

    facts_hit = [
        k
        for k, v in (
            ("name", kit["facts"].get("name")),
            ("hours", kit["facts"].get("hours")),
            ("offer", kit["facts"].get("offer")),
            *[(f"usp{i}", u) for i, u in enumerate(kit["facts"].get("usp") or [])],
            *[(f"svc{i}", s) for i, s in enumerate(kit["facts"].get("services") or [])],
        )
        if v and v.lower() in low
    ]
    city = (kit["facts"].get("city") or "").lower()
    area_hit = bool(city and city in low)
    rule(
        "facts",
        (bool(phone and phone in text_digits) and area_hit and len(facts_hit) >= 3) or None,
        f"{len(facts_hit)} business fact(s) found; area {'found' if area_hit else 'missing'}.",
        None
        if len(facts_hit) >= 3 and area_hit
        else "Use the business's own facts from the kit: name, hours, offer, services, the area.",
    )

    # A required line is the law's own wording, never a claim (A3p-2 Task 5): New York's former "Prior results do not
    # guarantee a similar outcome" (gone with its rules on 2026-06-01; a channel's lines remain) must not trip the
    # attorneys' own banned "guarantee" -- Admiry's template prints a required line as written.
    # The business's own name is a name, never a claim: "Best Plumbing Service" must not trip "best"
    claims = low
    for line in [ln["text"] for ln in kit["rules"]["required_lines"]] + [kit["facts"].get("name") or ""]:
        if line.strip():
            claims = claims.replace(line.lower(), " ")
    # (?<!\w)…(?!\w), not \b…\b: \b cannot sit before a "#", so "#1" was never caught
    banned = [
        w for w in kit["rules"]["banned_words"] if re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", claims)
    ]
    missing = [
        ln["text"] for ln in kit["rules"]["required_lines"] if ln.get("always") and ln["text"].lower() not in low
    ]
    if banned or missing:
        rule(
            "compliance",
            False,
            ("Banned words: " + ", ".join(banned) + ". " if banned else "")
            + ("Missing required line(s): " + "; ".join(missing) if missing else ""),
            (
                f"Remove the banned words: {', '.join(banned)}"
                + ("; add the required lines from the kit." if missing else ".")
                if banned
                else "Add the required lines from the kit."
            ),
        )
    elif kit["rules"]["sensitive"] and has_form:
        rule(
            "compliance",
            None,
            "This kind of business is a sensitive category: the form may ask name, phone, time and service — "
            "nothing about health, the case or money troubles — and needs counsel's sign-off before it goes live.",
            "Keep the form to those fields, or use a call button and a booking link instead.",
        )
    else:
        rule("compliance", True, "No banned claims; required lines present.")

    fresh = page.get("kit_plan_id") in (None, kit["plan_id"])
    rule(
        "freshness",
        True if fresh else None,
        "Matches the current plan." if fresh else "The plan changed since the page was written.",
        None if fresh else "Get the kit again and update the headline and offer.",
    )

    _unknown(extra_rules)
    # R7: a warning never blocks -- only an outright False on a BLOCKING rule does (spec §6: "a launch needs
    # every row but Speed and Facts to pass; those two warn"; this function's own docstring: a warning is
    # "reported, never blocking the report's own `ok`"). Without `is not False` here, no sensitive-category
    # page with a form could ever launch, since its compliance rule always warns (never a clean True). The
    # stored verdict is every channel's, so a caller's own rules never change it (`passed` judges those).
    ok = all(r["ok"] is not False for r in rules if r["id"] in BLOCKING)
    return {
        "ok": ok,
        "url": url,
        "rules": rules,
        "headline": h1,  # what Message-match read: the rule is judged again against a later plan (judged)
        "fetched": {
            "status": status,
            "bytes": len(raw),
            "seconds": round(seconds, 2),
            "final_url": final,
            "platform": platform,
        },
    }


def _unknown(extra_rules: tuple[str, ...]) -> None:
    """Log each of a caller's rule ids the check has no rule for: it blocks nothing (M0 named `terms` and `contact`
    for NewsBreak before the check had them; M1 Task 4 added both)."""
    known = {c["id"] for c in CHECKLIST}
    for id_ in extra_rules:
        if id_ not in known:
            log.info("landing check: no %r rule yet; it blocks nothing", id_)


_WORD_CHAR = re.compile(r"\w")


def _as_word(term: str, text_low: str) -> bool:
    """Whether `term` (already lower-cased) is in `text_low` as a whole word, not merely a substring (review fix
    round 1, Minor 2): a short area or city code like "LA" or "SF" must not match inside "Atlanta" or "snowfall".
    `\\b` needs a word/non-word transition, so it cannot anchor an edge that is itself punctuation -- "l.a." or
    "washington, d.c." never satisfied it, on either edge that ends in a dot (review fix round 2): each edge is
    `\\b` only when the term's own character there is a word character, else a lookaround for "no word character
    here" (`(?<!\\w)`/`(?!\\w)`), which a punctuation edge (or the start/end of the text) already satisfies."""
    if not term:
        return False
    start = r"\b" if _WORD_CHAR.match(term[0]) else r"(?<!\w)"
    end = r"\b" if _WORD_CHAR.match(term[-1]) else r"(?!\w)"
    return re.search(start + re.escape(term) + end, text_low) is not None


def message_match(kit: dict, headline: str) -> bool:
    """The Message-match rule: the headline names one of the kit's lead services and its city, one of the areas the
    business serves, or its offer (spec 2026-10-12 §3.6, R4: loosened beyond the city alone, so a headline built
    around a wider served area -- "Marin County Roof Repair" for a Palo Alto plan with Marin among its areas --
    still matches). The city and an area must appear as a whole word (`_as_word`); the offer, which is a phrase an
    owner wrote rather than a place name a short code could collide with, still matches as a substring."""
    low = headline.lower()
    city = (kit["facts"].get("city") or "").lower()
    areas = [a.lower() for a in kit["facts"].get("areas") or [] if isinstance(a, str) and a.strip()]
    offer = (kit["message"].get("offer") or "").lower()
    place = _as_word(city, low) or any(_as_word(a, low) for a in areas) or (offer and offer in low)
    return bool(lead_services(kit) and names_a_service(kit, low) and place)


def message_fix(kit: dict) -> str:
    return f"Say the service and the city in the headline, like the ad: “{kit['message']['headline_pattern']}”."


_HEADLINE_SAID = re.compile(r"Headline: “(.*)”\.", re.DOTALL)  # the rule's own detail (check), up to 80 characters


def judged(report: dict | None, kit: dict, *, owner: bool = False) -> dict | None:
    """A saved report with its Message-match rule judged again against `kit`, the plan of now, on the headline the
    check read (`headline`; a report saved before it was kept: its rule's detail) -- no fetch. The founder's dev walk,
    2026-10-08: the rule depends on the plan (its service and its city), the report is saved when the page is checked,
    and page-check never runs the rule again, so a later version of the plan was judged on an earlier one. `owner`
    (R4, 2026-10-12): on the page the owner owns, a failing match is advice (`ok` None), never a block, as `check`
    itself judges it now; a saved report from before that ruling is re-judged the same way, here, on its next read.
    The report's `ok` follows the BLOCKING rules as `check` computes it. Anything it cannot read is left as it was."""
    rules = (report or {}).get("rules") or []
    rule = next((r for r in rules if r.get("id") == "message_match"), None)
    if rule is None:
        return report
    headline = report.get("headline")
    if headline is None:
        said = _HEADLINE_SAID.fullmatch(rule.get("detail") or "")
        if said is None:
            return report
        headline = said.group(1)
    ok = message_match(kit, headline)
    verdict = True if ok else (None if owner else False)
    if rule.get("ok") is verdict:
        return report
    rules = [{**r, "ok": verdict, "fix": None if ok else message_fix(kit)} if r is rule else r for r in rules]
    return {**report, "rules": rules, "ok": all(r.get("ok") is not False for r in rules if r.get("id") in BLOCKING)}


def judged_report(conn: psycopg.Connection, page: dict | None) -> dict | None:
    """`saved_report`, with Message-match judged against the business's plan of now (`judged`): what decides whether
    the ads may send people to the page (ads_run._page_ready, the setup card, MarketingState). No consulted plan to
    judge it on (NO_PLAN): the saved report."""
    report = saved_report(page)
    if report is None:
        return None
    try:
        plan = _consulted_plan(conn, page["business_id"])
    except ToolError:
        return report
    latest = business.latest_knowledge(conn, page["business_id"])
    said = _message_of(plan["sections"]["consultation"], latest[1] if latest else {})
    return judged(report, said, owner=page.get("source") == OWNER_SOURCE)


def headline_pattern(conn: psycopg.Connection, biz: str) -> str | None:
    """The headline the ads' page should carry for the plan of now -- its lead service and its city, as Message-match
    asks them (`_message_of`) -- or None without a consulted plan (B0's "Fix the headline"). Whitespace-collapsed
    (ruling I4): `_message_of`'s own `leads` keeps a consultation service's stray spacing (it filters on
    `s.strip()` but keeps the un-stripped `s`), while `page_builder.edit` always stores what `_line` collapsed
    (`" ".join(text.split())`) -- so without this, a service written with extra spaces builds a pattern the saved
    preview's own headline can never match, and `fix_for`'s pending-preview check (ruling I3) would offer the edit
    forever instead of advancing to publish. `page_builder._line` is not imported here: `page_builder` imports
    `landing`, so the reverse import would cycle."""
    try:
        plan = _consulted_plan(conn, biz)
    except ToolError:
        return None
    latest = business.latest_knowledge(conn, biz)
    said = _message_of(plan["sections"]["consultation"], latest[1] if latest else {})
    return " ".join(said["message"]["headline_pattern"].split()) or None


def pending_preview(conn: psycopg.Connection, page_id: str) -> dict | None:
    """The page's own newest still-valid starter-page preview (ruling I3: `launch_check.fix_for`'s headline branch
    reads it before offering the edit again) -- {"token", "headline"}, the headline its sections carry
    (`sections.copy.headline`, as `page_builder.save` writes them) -- or None without one. `kind = 'html'`: a site's
    own preview (A3p-6) is read by its own tools (agent/tools_sites.py), never this one."""
    row = conn.execute(
        "SELECT token, sections FROM page_preview WHERE page_id = %s AND kind = 'html' AND expires_at > now() "
        "ORDER BY created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    if row is None:
        return None
    return {"token": row["token"], "headline": ((row["sections"] or {}).get("copy") or {}).get("headline")}


def newest_preview(conn: psycopg.Connection, page_id: str) -> str | None:
    """The token of the page's newest preview that has not ended, of any kind -- a page Admiry wrote or one an
    assistant put on a preview ('html'), a site uploaded whole ('bundle') -- never the gallery of looks, which is never
    published (sites.build.GALLERY); None without one. What a page in hand publishes (launch_check.page_in_hand)."""
    row = conn.execute(
        "SELECT token FROM page_preview WHERE page_id = %s AND expires_at > now() "
        "AND NOT coalesce(sections ? 'gallery', false) ORDER BY created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    return row["token"] if row else None


def passed(report: dict, extra_rules: tuple[str, ...] = ()) -> bool:
    """Whether a stored check `report` lets a caller that needs `extra_rules` too launch (M0 Task 4: `ads_run`'s
    page precondition, with the channel's `Capabilities.page_rules`): the report's own `ok` -- the BLOCKING rules'
    verdict, as `check` stored it -- and each of those rules the check ran passed outright (a warning is not
    enough). A rule the report does not have -- the check never ran it (a report saved before the check had the
    rule), or has no rule for it (logged) -- blocks nothing."""
    _unknown(extra_rules)
    ran = [r for r in report.get("rules") or [] if r.get("id") in extra_rules]
    return bool(report.get("ok")) and all(r.get("ok") is True for r in ran)
