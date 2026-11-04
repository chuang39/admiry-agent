"""Operator commands, run on the box with AGENT_DATABASE_URL set (never tools):
  python -m agent.ops verify <business_id>       mark a business verified by the operator (early access;
                                                 GBP / phone verification arrive with A1b) and upgrade its
                                                 newest active draft key to a lasting business key
  python -m agent.ops issue-code <proposal_id>   print a fresh approval code (issued_via=ops) to read to the owner
  python -m agent.ops proposals <business_id>    list the business's open proposals (id kind class expires_at summary)
  python -m agent.ops plans <business_id>        list the business's plans
  python -m agent.ops ads-link <business_id> <customer_id> --status active|pending|refused [--replace]
                                                 record a Google Ads link an operator handled in Admiry's manager
                                                 account (the manual link mode, or a fallback); active is stored as
                                                 'manual' and verifies the business (verified_via=ads_link); the
                                                 business must be active and on its own Google Ads account (ads_mode
                                                 own_account), and its owner must have accepted the current management
                                                 agreement; changing the Customer ID of an existing link needs
                                                 --replace
  python -m agent.ops google-ads-auth            one-time: sign in with Admiry's Google Ads login and print the
                                                 refresh token for GOOGLE_ADS_REFRESH_TOKEN (stdout only; never
                                                 stored, no database needed)
  python -m agent.ops ads-probe <customer_id> [--validate-campaign]
                                                 the A3 spike (spec §13): read a handful of fields of that Google
                                                 Ads account (name, currency, time zone, conversion tracking id,
                                                 call reporting) and print them; with --validate-campaign, also
                                                 send a validateOnly mutate of a paused budget + Search campaign
                                                 named "Admiry · probe", built the way a launch builds them, and
                                                 print "validate: ok" or the failure. No database; run against the
                                                 pilot account (one Admiry's manager account manages) before any
                                                 owner's plan reaches Google
  python -m agent.ops refund <business_id> <amount_usd> [--note …]
                                                 refund unspent balance to the cards that paid it (most recent
                                                 payment first; refused above the balance or above what cards paid);
                                                 a refund Stripe made that is not recorded yet (a lost answer, the
                                                 Stripe dashboard) is recorded first, and then nothing new is refunded
  python -m agent.ops adjust <business_id> <±amount_usd> --note …
                                                 a signed correction of a prepaid business's balance; the owner
                                                 reads the note
  python -m agent.ops email-queue                list the e-mail threads waiting for a person (thread, address,
                                                 business, last words)
  python -m agent.ops email-reply <thread_id> --text-file F
                                                 answers as Admiry; a ticket's thread: the ticket takes the reply and
                                                 closes once it is sent (any other thread: sent_by=ops, reopens it)
  python -m agent.ops email-stats [--day YYYY-MM-DD]
                                                 outcomes of that day's inbound and outbound e-mail (today by default)
  python -m agent.ops nb-probe [--create-account NAME [--company C] [--category CAT]]
                                                 the M1 spike (plan P3): print the ad accounts of Admiry's NewsBreak
                                                 Organization (NEWSBREAK_ORG_ID); with --create-account, create one
                                                 under it (LEADGEN-LOCAL; companyName --company, default NAME; the
                                                 industry pinned for --category, default fallback) unless one of that
                                                 name exists (NewsBreak cannot delete an account), and print
                                                 NewsBreak's answer: a status other than ACTIVE means the account
                                                 waits for a manual step. No database; never prints the token
  python -m agent.ops geo-refresh [--census-url U] [--sheet-url U]
                                                 rebuild the two checked-in catalogs behind NewsBreak's ZIP targeting
                                                 from their public sources (the Census Gazetteer's ZIP-code centroids,
                                                 NewsBreak's location sheet): agent/data/zcta_centroids.csv and
                                                 newsbreak_locations.csv, each with a comment naming its source and
                                                 the day; prints their row counts. Only public addresses are fetched;
                                                 a source that cannot be read leaves both files as they were. No
                                                 database; then update the table in agent/data/README.md and commit
  python -m agent.ops nb-reconcile <business_id> <day>
                                                 the M1 pilot's daily check (plan P10): the business's NewsBreak spend
                                                 on <day> (YYYY-MM-DD, a day of the ad account's own calendar that is
                                                 over) from NewsBreak's HOURLY report, metric_daily and the balance's
                                                 spend entry, one line each in whole cents; exit 0 only when the three
                                                 agree to the cent. A fourth line cross-checks NewsBreak's lifetime
                                                 total against its HOURLY report over the account's life and shows
                                                 the difference (it may hold spend the hourly report has not yet); it
                                                 never changes the exit. Never prints the token
  python -m agent.ops nb-account-ready <business_id>
                                                 once a NewsBreak account's trouble is fixed at NewsBreak (allowance,
                                                 verification, funding): clears the "waiting at NewsBreak" mark so the
                                                 next hourly step goes on, and a spend check that stops Admiry's
                                                 changes: NewsBreak's total becomes the last cap Admiry set when it is
                                                 above that cap beyond NewsBreak's own overshoot (never lowered
                                                 otherwise), and the mail mark goes. Never prints the token
  python -m agent.ops google-account add <customer_id> --scope S [--capacity N] [--note …]
                                                 A3c-1: a Google Ads client account into the pool prepaid businesses'
                                                 Google Search runs in. S is root (any business), a family (L1:
                                                 attorneys) or <family>/<trade> (L2: attorneys/personal_injury; the
                                                 account's name account_attorneys_personal_injury is taken too); N the
                                                 businesses it runs at most (none: no limit; 1 is Google's third-party
                                                 policy, an account per advertiser). The account is read from Google
                                                 first through Admiry's manager account: a client account, US dollars,
                                                 enabled, or it is refused
  python -m agent.ops google-account list [--businesses]
                                                 the pool: scope, account, open/closed, live businesses of capacity,
                                                 Google's name and time zone (with --businesses, the ones each runs)
  python -m agent.ops google-account capacity <customer_id> <N|none>
  python -m agent.ops google-account close|open <customer_id>
                                                 a closed account keeps its businesses and takes no new one
  python -m agent.ops google-account bind <business_id> <customer_id> [--force] [--note …]
                                                 an operator's choice of a prepaid business's account (the launch
                                                 otherwise picks one itself: the trade's, the family's, else root);
                                                 --force binds onto a closed or full account
  python -m agent.ops google-account release <business_id> [--force]
                                                 end the business's binding (its history keeps it); refused while its
                                                 ads are built in that account unless --force (they stay there)
  python -m agent.ops google-account show <business_id>
                                                 every account the business ran in, the live one last
  python -m agent.ops google-account waiting     prepaid businesses with an approved Google Search plan and no account
                                                 yet, the scopes they can run in, and whether one has room
  python -m agent.ops hosting-exempt <business_id> --reason … | --clear
                                                 A3p-3 P18: hosting stays free for the business whatever its ads spend
                                                 (the pilot, a partner; the reason is kept with who and when), and a
                                                 countdown running on its site is cleared without an e-mail; --clear
                                                 ends it, and the $200 minimum applies from the next daily run
  python -m agent.ops hosting <business_id>      the hosting minimum as the daily job judges the business today: covered
                                                 or not and why, the live monthly amount, the 30 days' spend, whether
                                                 its ads are ready at $200 a month, the countdown, the takedown, the
                                                 exemption
  python -m agent.ops stock-load [--manifest PATH] [--files DIR] [--family F]
                                                 A3p-6: store the stock photo library (agent/data/stock/manifest.json)
                                                 with its WebP sizes and licence record, and print "loaded N (A from
                                                 files, B fetched), kept K, failed F"; --files is a copy of
                                                 agent/data/stock/files/ (each photo is read from it first, and only
                                                 one with no file there is fetched from its CDN address); every photo's
                                                 SHA-256 must be the manifest's, or it is failed (named on stderr, exit
                                                 1) and nothing of it is stored; one already stored is kept; --family
                                                 loads one family (repeatable)
  python -m agent.ops site-preview <business_id> --sample roofer|dentist --style <style> [--accent #RRGGBB]
                                   [--words fixture|model] [--days 7]
                                                 A3p-6: build that sample in that style (roofer: solid, clean, bold;
                                                 dentist: calm, warm, boutique) as the business's bundle preview,
                                                 kept --days days (7 by default), and print its address and token,
                                                 "loads N (the screenshot sizes kept), partial yes|no" with the pages
                                                 not opened when partial, and Google's speed scores or why none;
                                                 --words model has the model write the words from the sample's facts
                                                 (fixture: the sample's own). Exit 1 with the problems on a template
                                                 fault, or when the renderer is down, hosting is off
                                                 (AGENT_PAGES_DOMAIN) or the business is not active
  python -m agent.ops site-gallery <business_id> [--days 7] [--samples hvac,plumber,electrician]
                                                 A3p-6: the six (roofer × solid, clean, bold; dentist × calm, warm,
                                                 boutique), each a full build printed as it goes, then one index
                                                 bundle -- the six with their three screenshots and a link into each,
                                                 the sites under /<sample>-<style>/ -- kept as the business's one
                                                 preview; prints "gallery <address>" last, the page to review the
                                                 looks on. Exits 1 at the first fault. --samples builds the
                                                 samples named instead (a trade's own, 2026-10-08), in their styles
  python -m agent.ops site-looks <business_id> --out DIR
                                                 A3p-6 batch 2: the phone screenshots of the business's live gallery
                                                 (site-gallery) written into DIR as the six looks an owner picks a
                                                 site by -- <sample>-<style>.jpg, at most 390 px wide, JPEG quality 72
                                                 -- one path printed per file; --out agent/static/sites/looks
                                                 refreshes the shipped ones (then commit them). Nothing is written
                                                 and it exits 1 when the business has no live gallery or a screenshot
                                                 is missing (the render check was not set up for the gallery)
  python -m agent.ops digest-preview <business_id> [--day YYYY-MM-DD] [--edition daily|weekly] [--no-note]
                                                 K2 (the daily digest): that day's report (the business's yesterday by
                                                 default) as the job would build it, with the operator's block -- its
                                                 subject and text printed; nothing stored, nothing sent; --no-note
                                                 leaves the stored look's take out
  python -m agent.ops digest <business_id> on|operator|off|clear
                                                 the business's own digest switch (operator_flags.digest; clear goes
                                                 back to AGENT_DIGEST); AGENT_DIGEST=off still wins
  python -m agent.ops email-status <business_id> ok
                                                 e-mails go to the business's address again after a bounce (as the
                                                 Reports card's resume does)
  python -m agent.ops digest-seed <business_id> --day YYYY-MM-DD [--state live|paused:<reason>] [--spend 23.10]
                                  [--clicks 41] [--calls 2] [--leads 1]
                                                 dev only (refused on prod): a day of numbers for a business whose ads
                                                 do not run, so the digest has something to report -- a Google Search
                                                 row with no campaign (only when there is none), the operator's numbers
                                                 for that day, --leads form leads at local noon; refused for a
                                                 business with a built campaign or Google's own numbers
  python -m agent.ops insight <business_id> [--day YYYY-MM-DD]
                                                 K2: the Analyst's look for that day (the latest by default) as the
                                                 team sees it: status, mode, verdict, score, why no owner reads it,
                                                 rounds, model calls, the flags' rules, the candidates, every sentence
                                                 with why it was dropped, the Critic's issues
  python -m agent.ops insight-rebuild <business_id> --day YYYY-MM-DD
                                                 dev only (refused on prod): that day's look built again now and
                                                 printed as `insight` prints it
  python -m agent.ops research <business_id> [--topic T ...] [--note TEXT] [--now]
                                                 K4: research queued for the business now (the operator's: no
                                                 24-hour or 30-day wait, the day's caps still hold); --now (dev only,
                                                 refused on prod) runs it at once and prints it as research-show does
                                                 -- unless the day's 50 research runs are used up or the business has
                                                 research running: the job then waits for research-run, and the
                                                 command says so (a job skipped before a run began says why)
  python -m agent.ops research-show <business_id> [--run ID]
                                                 K4: the latest research run (or that one) as the team sees it:
                                                 status, mode, trigger, verdict, score, confidence, why no owner reads
                                                 it, each round's verdict and what it read, the sources, every
                                                 sentence and search with why it was dropped, the Critic's issues
  python -m agent.ops research-due               K4: the businesses automatic research is due for by the rules, by
                                                 the runs AGENT_ROLES's mode can stand for (on: only research built on;
                                                 off reads as validate, though with it off the sweep queues none) and
                                                 not cut to one sweep's 20 a shard: how many, then each id
  python -m agent.ops eval-online [--days 30]
                                                 K5: the online numbers of the capability spec §7 over the last N days
                                                 -- plan approval, first turn to first launch, plan changes before
                                                 approval, thumbs down and why, Google Search's cost per lead against
                                                 the vertical benchmark (7 and 30 days), the Critic's second tries
                                                 (plans, daily looks, research runs), research confidence (K4's runs
                                                 by confidence), the Analyst's suggestions followed within 3 days
                                                 (looks at least 3 days old only); aggregates only, nothing stored
  python -m agent.ops knowledge [--days 30]
                                                 K3: each knowledge file's review and how often plans cite it -- its
                                                 review date, reviewer and age ("review due" past a year), confidence
                                                 and sources, and how many consultation plan versions of the last N
                                                 days cite it; an id cited whose file is gone; then how many versions
                                                 cite any. A file waiting for its review is listed ("by pending")
The code (and the listings) go to stdout; messages to stderr. verify, issue-code, ads-link, refund, adjust,
nb-reconcile, nb-account-ready, hosting-exempt, stock-load, site-preview, site-gallery, google-account (but list,
show and waiting), email-status, digest, digest-seed, insight, insight-rebuild, research and research-show each leave
an agent_action row (actor ops:cli, class platform) — never the code itself; the last six with no business on it (the
business is in its args), so no owner reads them. eval-online, knowledge and research-due, like email-stats, leave none:
they change nothing and show no business's own data (research-due prints only the ids of the businesses due; knowledge
reads the files and counts plan versions)."""

from __future__ import annotations

import argparse
import io
import secrets
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp.exceptions import ToolError
from PIL import Image
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from . import (
    ads_run,
    agreements,
    audit,
    bundles,
    business,
    config,
    db,
    digest,
    digest_audience,
    digest_zone,
    geo,
    google_ads_link,
    google_auth,
    google_managed,
    hosting_minimum,
    keys,
    knowledge_index,
    llm,
    mail_dispatch,
    mail_out,
    proposals,
    render_check,
    stripe_client,
    support,
    support_mail,
    wallet,
)
from .channels import registry
from .channels.google_search import build as ads_launch  # A3's names for the modules moved in M0
from .channels.google_search import client as google_ads
from .channels.newsbreak import adapter as nb_adapter
from .channels.newsbreak import client as newsbreak
from .eval import online
from .roles import analyst, research_material, researcher
from .sites import build as site_build
from .sites import gallery, samples, stock, words

ACTOR = "ops:cli"
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
ADS_AUTH_REDIRECT = "http://localhost:8097/connect/google/callback"  # already an authorised URI of the OAuth client


def _audit(conn, tool: str, args: dict, result: dict, business_id: str | None, *, ok: bool = True) -> None:
    audit.record(
        conn,
        tool=tool,
        args=args,
        result=result,
        ok=ok,
        business_id=business_id,
        actor=ACTOR,
        actor_class="platform",
        api_key_id=None,
        ip=None,
    )


def _verify(conn, biz: str) -> int:
    with conn.transaction():
        r = conn.execute(
            "UPDATE business SET claim_state = 'verified', verified_via = 'ops', verified_at = now(), "
            "claim_expires_at = NULL, updated_at = now() WHERE id = %s RETURNING id",
            (biz,),
        ).fetchone()
        if r is None:
            print(f"no business {biz}", file=sys.stderr)
            return 1
        # the owner's setup key keeps working, now lasting and allowed to approve (the key itself is unchanged)
        key_id = keys.upgrade_draft(conn, biz)
        _audit(conn, "ops.verify", {"business_id": biz}, {"verified": biz, "key_upgraded": key_id}, biz)
    print(f"{biz} verified (via=ops)", file=sys.stderr)
    print(f"key {key_id} upgraded to business" if key_id else "no active draft key to upgrade", file=sys.stderr)
    return 0


def _issue_code(conn, pid: str) -> int:
    try:
        with conn.transaction():
            code = proposals.issue_code(conn, pid, via="ops")
            biz = conn.execute("SELECT business_id FROM proposal WHERE id = %s", (pid,)).fetchone()["business_id"]
            _audit(conn, "ops.issue-code", {"proposal_id": pid}, {"proposal_id": pid}, biz)  # never the code
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"proposal={pid} issued_via=ops", file=sys.stderr)
    print(code)
    return 0


def _ads_link(conn, biz: str, text: str, status: str, replace: bool = False) -> int:
    try:
        cid = google_ads_link.normalize_customer_id(text)
    except google_ads_link.LinkRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    row = business.row(conn, biz)
    if row is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    if row["status"] != "active":
        print(f"business {biz} is not active (status {row['status']})", file=sys.stderr)
        return 1
    if row["ads_mode"] != "own_account":  # a prepaid business's ads run in a pool account of Admiry's (A3c-1)
        print(
            f"business {biz} is prepaid (ads_mode {row['ads_mode']}): its ads run in a Google Ads account of Admiry's "
            "(google-account); there is nothing to link",
            file=sys.stderr,
        )
        return 1
    current = google_ads_link.get(conn, biz)
    if current is not None and current.customer_id != cid and not replace:
        print(f"this business is linked to {current.display}; pass --replace to change it", file=sys.stderr)
        return 1
    if not agreements.accepted(conn, biz):
        print(
            f"the owner has not accepted the management agreement ({agreements.CURRENT_VERSION}); "
            "they accept it in the Admiry conversation first",
            file=sys.stderr,
        )
        return 1
    stored = "manual" if status == "active" else status
    try:
        with conn.transaction():
            link = google_ads_link.save(conn, biz, cid, status=stored)
            verified = google_ads_link.verify_by_link(conn, biz) if stored == "manual" else False
            _audit(
                conn,
                "ops.ads_link",
                {"business_id": biz, "customer_id": cid, "status": status},
                {"customer_id": link.display, "status": status, "verified_now": verified},
                biz,
            )
    except google_ads_link.LinkRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    print(
        f"{biz} google ads {link.display} {stored}" + (" — verified (via=ads_link)" if verified else ""),
        file=sys.stderr,
    )
    return 0


def _record_stripe_refunds(
    conn, http: httpx.Client, secret_key: str, biz: str
) -> list[tuple[str, stripe_client.Refund]]:
    """Stripe's side first: the refunds Stripe made (succeeded or pending) against this business's paid sessions that
    are not fully refunded here, and that the balance does not hold yet — an attempt whose answer was lost, a crash
    before our commit, a refund made in the Stripe dashboard — are recorded, each in its own transaction with its
    audit row. Every list is read before anything is written, with no transaction open; a StripeError (unreachable
    included) propagates and nothing is recorded. Returns [(session_id, refund)] as recorded now."""
    sessions = conn.execute(
        "SELECT id, payment_intent FROM payment_session WHERE business_id = %s AND status = 'paid' "
        "AND payment_intent IS NOT NULL AND refunded_cents < amount_cents ORDER BY paid_at, id",
        (biz,),
    ).fetchall()
    listed = [
        (s["id"], stripe_client.list_refunds(http, secret_key=secret_key, payment_intent=s["payment_intent"]))
        for s in sessions
    ]
    found = [
        (session_id, r)
        for session_id, refunds in listed
        for r in reversed(refunds)  # oldest first
        if r.status in ("succeeded", "pending")
    ]
    recorded = []
    for session_id, r in found:
        with conn.transaction():
            entry = wallet.refund(
                conn, biz, refund_id=r.id, amount_cents=r.amount_cents, session_id=session_id, note=None
            )
            if entry is not None:  # None: already recorded
                _audit(
                    conn,
                    "ops.refund",
                    {"business_id": biz, "session_id": session_id, "amount_usd": wallet.to_usd(r.amount_cents)},
                    {
                        "refund_id": r.id,
                        "amount_usd": wallet.to_usd(r.amount_cents),
                        "status": r.status,
                        "recorded_from_stripe": True,
                    },
                    biz,
                )
        if entry is not None:
            recorded.append((session_id, r))
            print(f"{r.id} {wallet.usd(r.amount_cents)} against {session_id} ({r.status}, made earlier)")
    return recorded


def _refund(conn, biz: str, amount_text: str, note: str | None) -> int:
    """Return unspent balance to the cards that paid it (spec 2026-09-25 rev. 2 D30; rev. 1 §4), whatever the
    business's mode — money can always go back. Under wallet.lock, Stripe's side comes first: a refund Stripe made
    that the balance does not hold yet (an answer lost, a crash before our commit, the Stripe dashboard) is recorded
    and the run stops there with nothing new refunded, so a re-run only resolves and never refunds on top of an
    unrecorded refund; if that check fails, nothing is refunded. Then: refused above the balance or above what cards
    paid; one Stripe refund per session, most recent first, each recorded as soon as Stripe accepts it. The
    Idempotency-Key is the session and its refunded total, so repeating the same attempt replays Stripe's answer.
    No answer or a 5xx is reported as unconfirmed (it may have gone through: the next run records it if it did), a
    4xx as Stripe's refusal; either way what was refunded before it stays recorded."""
    settings = config.load()
    if not settings.stripe_secret_key:
        print("STRIPE_SECRET_KEY is not set: refunds go through Stripe", file=sys.stderr)
        return 1
    try:
        cents = wallet.ops_cents(amount_text, signed=False)
    except wallet.AmountRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    with wallet.lock(conn, biz), stripe_client.client() as http:
        try:
            recorded = _record_stripe_refunds(conn, http, settings.stripe_secret_key, biz)
        except stripe_client.StripeError as e:
            print(f"could not check Stripe for earlier refunds ({e}); nothing was refunded", file=sys.stderr)
            return 1
        if recorded:
            print(
                f"recorded {', '.join(r.id for _, r in recorded)} that Stripe had already made (an earlier attempt "
                f"or the Stripe dashboard); nothing new was refunded; balance now "
                f"{wallet.usd(wallet.balance_cents(conn, biz))}",
                file=sys.stderr,
            )
            return 1
        balance = wallet.balance_cents(conn, biz)
        if cents > balance:
            print(f"refused: {wallet.usd(cents)} is more than the balance {wallet.usd(balance)}", file=sys.stderr)
            return 1
        plan = wallet.refund_plan(conn, biz, cents)
        covered = sum(p["amount_cents"] for p in plan)
        if covered < cents:
            print(
                f"refused: only {wallet.usd(covered)} was credited from payments and not yet refunded", file=sys.stderr
            )
            return 1
        done = 0
        for p in plan:
            try:
                r = stripe_client.create_refund(
                    http,
                    secret_key=settings.stripe_secret_key,
                    payment_intent=p["payment_intent"],
                    amount_cents=p["amount_cents"],
                    idempotency_key=f"refund-{p['session_id']}-{p['refunded_before']}",
                )
            except stripe_client.StripeUnreachable:
                # the amount still to go, not the command's: what was refunded before it is recorded, so asking for
                # the whole amount again would refund that part twice over if this one never reached Stripe
                print(
                    f"could not confirm the refund of {wallet.usd(p['amount_cents'])} against {p['session_id']}: "
                    f"it may have gone through. Run the command again for the {wallet.usd(cents - done)} still to "
                    f"go: it first records any refund Stripe already made and, if it finds one, refunds nothing "
                    f"new. {wallet.usd(done)} was refunded before it",
                    file=sys.stderr,
                )
                return 1
            except stripe_client.StripeError as e:
                print(
                    f"Stripe refused the refund against {p['session_id']}: {e}; {wallet.usd(done)} was refunded before it",
                    file=sys.stderr,
                )
                return 1
            with conn.transaction():
                if (
                    wallet.refund(
                        conn, biz, refund_id=r.id, amount_cents=p["amount_cents"], session_id=p["session_id"], note=note
                    )
                    is not None
                ):
                    _audit(
                        conn,
                        "ops.refund",
                        {
                            "business_id": biz,
                            "session_id": p["session_id"],
                            "amount_usd": wallet.to_usd(p["amount_cents"]),
                        },
                        {"refund_id": r.id, "amount_usd": wallet.to_usd(p["amount_cents"]), "status": r.status},
                        biz,
                    )
            done += p["amount_cents"]
            print(f"{r.id} {wallet.usd(p['amount_cents'])} against {p['session_id']} ({r.status})")
    print(f"refunded {wallet.usd(done)}; balance {wallet.usd(wallet.balance_cents(conn, biz))}", file=sys.stderr)
    return 0


def _adjust(conn, biz: str, amount_text: str, note: str) -> int:
    """A signed correction of a prepaid business's balance with a required note (a late Google credit, goodwill);
    lifts a no-funds pause when the balance comes back above zero. An own_account business has no balance."""
    if not note.strip():
        print("a note is required: the owner reads it next to the correction", file=sys.stderr)
        return 1
    try:
        cents = wallet.ops_cents(amount_text, signed=True)
    except wallet.AmountRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    row = business.row(conn, biz)
    if row is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    if row["ads_mode"] != "managed":
        print(
            f"business {biz} is not prepaid (ads_mode {row['ads_mode']}): it has no balance to adjust", file=sys.stderr
        )
        return 1
    with wallet.lock(conn, biz), conn.transaction():
        entry = wallet.adjust(conn, biz, cents, note=note)
        resumed = wallet.resume_if_funded(conn, biz)
        _audit(
            conn,
            "ops.adjust",
            {"business_id": biz, "amount_usd": wallet.to_usd(cents), "note": note.strip()},
            {"entry_id": entry, "amount_usd": wallet.to_usd(cents), "note": note.strip(), "resumed": resumed},
            biz,
        )
    print(f"adjusted {wallet.usd(cents)}; balance {wallet.usd(wallet.balance_cents(conn, biz))}", file=sys.stderr)
    return 0


def _email_queue(conn) -> int:
    rows = conn.execute(
        "SELECT t.id, t.counterpart, t.business_id, t.last_in_at, "
        "(SELECT new_text FROM email_message m WHERE m.thread_id = t.id AND m.direction = 'in' ORDER BY m.created_at DESC LIMIT 1) AS last "
        "FROM email_thread t WHERE t.status = 'operator' ORDER BY t.last_in_at"
    ).fetchall()
    for r in rows:
        words = (r["last"] or "").replace("\n", " ")[:120]
        when = f"{r['last_in_at']:%Y-%m-%d %H:%M}" if r["last_in_at"] else "-"
        print(f"{r['id']} {mail_dispatch.reply_address(conn, r)} {r['business_id'] or '-'} {when} {words}")
    print(f"{len(rows)} waiting", file=sys.stderr)
    return 0


def _email_reply(conn, thread_id: str, path: str) -> int:
    thread = conn.execute("SELECT * FROM email_thread WHERE id = %s", (thread_id,)).fetchone()
    if thread is None:
        print(f"no thread {thread_id}", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        print("the text file is empty", file=sys.stderr)
        return 1
    settings = config.load()
    ticket = support.open_for_thread(conn, thread_id) if settings.support else None
    if ticket is not None:  # D146: the thread's ticket takes the reply, on this thread, and closes once it is sent
        try:
            reply = support.reply(conn, ticket["id"], text, actor="ops:cli", close=True, thread_id=thread_id)
            event_id = reply["event_id"]
        except support.Refused as e:
            print(str(e), file=sys.stderr)
            return 1
        support_mail.flush(ticket["id"])
        state = conn.execute("SELECT mail_state FROM ticket_event WHERE id = %s", (event_id,)).fetchone()["mail_state"]
        outcome = "sent" if state == "sent" else "failed"
        audit.record(
            conn,
            tool="ops.email_reply",
            args={"thread_id": thread_id, "chars": len(text)},
            result={"outcome": outcome, "ticket_id": ticket["id"]},
            ok=outcome == "sent",
            business_id=thread["business_id"],
            actor="ops:cli",
            actor_class="platform",
            api_key_id=None,
            ip=None,
        )
        if outcome == "sent":
            print(f"sent {ticket['id']}", file=sys.stderr)
            return 0
        # not the legacy path's "nothing was sent, run it again": the reply is in the outbox and goes out by itself
        print(
            f"failed {ticket['id']}: the reply is queued and the support-mail job retries it; "
            "do not run email-reply again",
            file=sys.stderr,
        )
        return 1
    out = mail_out.deliver(
        conn,
        settings,
        thread=dict(thread),
        to=mail_dispatch.reply_address(conn, thread),
        paragraphs=text.split("\n\n"),
        sent_by="ops",
    )
    if out["outcome"] == "sent":  # a failed send never reached its recipient: leave the thread with the operator
        conn.execute("UPDATE email_thread SET status = 'open', unclear = 0 WHERE id = %s", (thread_id,))
    audit.record(
        conn,
        tool="ops.email_reply",
        args={"thread_id": thread_id, "chars": len(text)},
        result={"outcome": out["outcome"], "message_id": out["message_id"]},
        ok=out["outcome"] == "sent",
        business_id=thread["business_id"],
        actor="ops:cli",
        actor_class="platform",
        api_key_id=None,
        ip=None,
    )
    print(f"{out['outcome']} {out['message_id']}", file=sys.stderr)
    return 0 if out["outcome"] == "sent" else 1


def _email_stats(conn, day: str | None) -> int:
    rows = conn.execute(
        "SELECT direction, outcome, count(*) AS n FROM email_message "
        "WHERE created_at >= date_trunc('day', COALESCE(%s::timestamptz, now())) "
        "AND created_at < date_trunc('day', COALESCE(%s::timestamptz, now())) + interval '1 day' "
        "GROUP BY direction, outcome ORDER BY direction, outcome",
        (day, day),
    ).fetchall()
    for r in rows:
        print(f"{r['direction']} {r['outcome']} {r['n']}")
    return 0


def _google_ads_auth(read: Callable[[], str] | None = None, http: httpx.Client | None = None) -> int:
    """Paste-back OAuth for Admiry's own Google Ads login: print the consent URL, read back the address the browser
    landed on (or the bare code), check its state, exchange the code and print the refresh token to stdout."""
    settings = config.load()
    if not settings.google_client_id or not settings.google_client_secret:
        print(
            "set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET (the agent's OAuth client) first",
            file=sys.stderr,
        )
        return 1
    verifier, challenge = google_auth.pkce()
    state = secrets.token_urlsafe(16)
    url = google_auth.authorize_url(
        client_id=settings.google_client_id,
        redirect_uri=ADS_AUTH_REDIRECT,
        state=state,
        challenge=challenge,
        scope=ADS_SCOPE,
        offline=True,
    )
    print(
        "1. Open this address in a browser and sign in with Admiry's Google Ads login:\n" + url + "\n"
        "2. Google then opens localhost:8097; that page may fail to load or show an error, which is fine.\n"
        "3. Copy the whole address from the address bar, paste it here and press Enter:",
        file=sys.stderr,
    )
    pasted = (read or sys.stdin.readline)().strip()
    code = pasted
    if pasted.startswith("http"):
        query = parse_qs(urlparse(pasted).query)
        if query.get("state", [""])[0] != state:
            print(
                "that address belongs to another sign-in; run the command again and paste the new one", file=sys.stderr
            )
            return 1
        code = query.get("code", [""])[0]
    if not code:
        print("no code in what was pasted; run the command again", file=sys.stderr)
        return 1
    try:
        token = google_auth.offline_token(
            http or google_auth.client(),
            code=code,
            verifier=verifier,
            redirect_uri=ADS_AUTH_REDIRECT,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
    except google_auth.SignInError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(token)
    print(
        "Store it as GOOGLE_ADS_REFRESH_TOKEN (dev: the box .env; prod: its Secret Manager shell). "
        "This command saved it nowhere.",
        file=sys.stderr,
    )
    return 0


PROBE_QUERY = (
    "SELECT customer.descriptive_name, customer.currency_code, customer.time_zone, customer.test_account, "
    "customer.conversion_tracking_setting.conversion_tracking_id, "
    "customer.call_reporting_setting.call_reporting_enabled, "
    "customer.call_reporting_setting.call_conversion_reporting_enabled FROM customer"
)
PROBE_CAMPAIGN = "Admiry · probe"
PROBE_DAILY_USD = 10.0
PROBE_CEILING_USD = 2.0  # the floor of a launch's own CPC ceiling (P9)


def _ads_probe(customer_id: str, validate_campaign: bool) -> int:
    """The A3 spike (spec 2026-09-28 admiry-google-search-execution-design §13), run by an operator against a real
    account before any owner's plan reaches Google: read a handful of account fields and, with
    --validate-campaign, send a validateOnly mutate of a paused budget + Search campaign built by the launch's own
    builders (`ads_launch.budget_op`/`campaign_op`: Maximize Clicks with a ceiling, Google Search only, PRESENCE --
    A3 final review I4: a campaign without a bidding strategy is refused). Prints only field values and Google's own
    error code (google_ads.AdsError); never a token. No database, no audit row (a read, like google-ads-auth) — the
    switch (AGENT_ADS_EXECUTION) does not gate it: an operator runs it before anything else in A3 is wired up,
    against an account Admiry's manager account manages (every call goes through it, `login-customer-id`)."""
    try:
        cid = google_ads_link.normalize_customer_id(customer_id)
    except google_ads_link.LinkRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    settings = config.load()
    try:
        rows = google_ads.search(settings, cid, PROBE_QUERY)
    except google_ads.AdsError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not rows:
        print(f"no customer {cid}", file=sys.stderr)
        return 1
    customer = rows[0].get("customer") or {}
    tracking = customer.get("conversionTrackingSetting") or {}
    calls = customer.get("callReportingSetting") or {}
    print(f"descriptive_name: {customer.get('descriptiveName')}")
    print(f"currency_code: {customer.get('currencyCode')}")
    print(f"time_zone: {customer.get('timeZone')}")
    print(f"test_account: {customer.get('testAccount')}")
    print(f"conversion_tracking_id: {tracking.get('conversionTrackingId')}")
    print(f"call_reporting_enabled: {calls.get('callReportingEnabled')}")
    print(f"call_conversion_reporting_enabled: {calls.get('callConversionReportingEnabled')}")
    if not validate_campaign:
        return 0
    budget = google_ads.temp("campaignBudgets", -1, cid)
    operations = [
        ads_launch.budget_op(budget, f"{PROBE_CAMPAIGN} budget", google_ads.to_micros(PROBE_DAILY_USD)),
        ads_launch.campaign_op(
            google_ads.temp("campaigns", -2, cid), PROBE_CAMPAIGN, budget, google_ads.to_micros(PROBE_CEILING_USD)
        ),
    ]
    try:
        google_ads.mutate(settings, cid, operations, validate_only=True)
    except google_ads.AdsError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("validate: ok")
    return 0


PROBE_ACCOUNT_FIELDS = (
    "id",
    "name",
    "status",
    "paymentType",
    "accountCategory",
    "industry",
    "companyName",
    "billingThreshold",
)


def _nb_failure(e: newsbreak.NewsBreakError) -> str:
    """For the operator, not an owner: the sentence, NewsBreak's code and status, and its own words (`detail`, the
    token already cut out)."""
    said = f" -- NewsBreak said: {e.detail}" if e.detail else ""
    return f"{e} (code {e.code}, HTTP {e.status}){said}"


def _nb_probe(create_account: str | None, company: str | None, category: str) -> int:
    """The M1 spike (plan P3), run by an operator once Admiry's NewsBreak Organization and token exist: list the
    organisation's ad accounts and, with `create_account`, make one -- the call every business's launch will make,
    so its answer says whether creating an account needs a manual step (a status other than ACTIVE) and whether the
    pinned industry value is NewsBreak's. Prints field values and NewsBreak's own words, never the token. No database,
    no audit row (like ads-probe); the switch (AGENT_EXEC_NEWSBREAK) does not gate it."""
    settings = config.load()
    if not settings.newsbreak_ready:
        print("set NEWSBREAK_ACCESS_TOKEN and NEWSBREAK_ORG_ID (the box .env) first", file=sys.stderr)
        return 1
    org = settings.newsbreak_org_id
    try:
        accounts = newsbreak.ad_accounts(settings, org)
    except newsbreak.NewsBreakError as e:
        print(_nb_failure(e), file=sys.stderr)
        return 1
    for a in accounts:
        print(f"ad_account: {a.get('id')} {a.get('name')} created {a.get('createTime')}")
    print(f"{len(accounts)} ad account(s) in organisation {org}", file=sys.stderr)
    if create_account is None:
        return 0
    taken = next((a for a in accounts if a.get("name") == create_account), None)
    if taken is not None:
        print(
            f"an ad account named {create_account!r} exists already ({taken.get('id')}); NewsBreak cannot delete one, "
            "so nothing was made",
            file=sys.stderr,
        )
        return 1
    try:
        made = newsbreak.create_ad_account(
            settings,
            org_id=org,
            name=create_account,
            company_name=company or create_account,
            industry=newsbreak.industry(category),
        )
    except newsbreak.NewsBreakError as e:
        print(_nb_failure(e), file=sys.stderr)
        return 1
    for key in PROBE_ACCOUNT_FIELDS:
        print(f"{key}: {made.get(key)}")
    status = made.get("status")
    waits = "" if status == "ACTIVE" else ", not ACTIVE: the new account waits for a step at NewsBreak"
    print(f"created ad account {made.get('id')} in organisation {org}: status {status}{waits}", file=sys.stderr)
    return 0


NB = nb_adapter.CHANNEL
REPORT_WINDOW_DAYS = 30  # NewsBreak's report covers at most 30 days a request (Run a Synchronous Report, client.py)
WAIT_MARKS = ("account_status", "account_checked_at", "account_mailed")  # the adapter's "waiting at NewsBreak" (R11)


def _whole_cents(value: object) -> int | None:
    """A cost NewsBreak gives in cents, as a whole number; None when it is not one (integers only, T6-7)."""
    try:
        exact = Decimal(str(value))
    except (ArithmeticError, ValueError):  # decimal.InvalidOperation is an ArithmeticError
        return None
    return int(exact) if exact.is_finite() and exact == exact.to_integral_value() else None


def _report_cents(rows: list[dict], first: date, last: date) -> tuple[int | None, str | None]:
    """What NewsBreak's report rows cost from `first` to `last` (their own `date`), in cents: (cents, None) -- or (None,
    why) when a cost cannot be counted: a row with no cost at all (final review Minor 3), one not in whole cents, or
    money on no day Admiry can read (a row with a cost and no `date` it can place, NewsBreak's key or format not the
    one Admiry reads: fix round 1, item 3), each of which must never pass for nothing spent."""
    total, nowhere = 0, 0
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("date")))
        except ValueError:
            day = None
        if day is not None and not first <= day <= last:
            continue
        if row.get("cost") is None:
            return None, "a row with no cost"
        cents = _whole_cents(row.get("cost"))
        if cents is None:
            return None, "a cost not in whole cents"
        if day is None:
            nowhere += cents
        else:
            total += cents
    if nowhere:
        return None, f"{nowhere} cents on no day (rows whose day Admiry cannot read)"
    return total, None


def _lifetime_line(
    conn, settings: config.Settings, http: httpx.Client, biz: str, account_id: str, zone_name: str, today: date
) -> tuple[str, dict]:
    """nb-reconcile's fourth line (the controller's answer 1 to T6-7): NewsBreak's lifetime total for the account (Get
    Account Spending Cap) beside its HOURLY report summed over the account's life -- from the account row's first day
    in the account's zone (or an earlier `metric_daily` day) through today, in REPORT_WINDOW_DAYS windows -- and the
    difference, which may hold spend the hourly report has not yet (whether NewsBreak's total is live is a pilot
    check). Never part of the exit status. Returns the line and its figures for the audit row."""
    zone = ads_run.zone(zone_name)
    opened = conn.execute(
        "SELECT created_at FROM channel_account WHERE business_id = %s AND channel = %s", (biz, NB)
    ).fetchone()
    first = opened["created_at"].astimezone(zone).date() if opened else today
    earliest = conn.execute(
        "SELECT min(day) AS day FROM metric_daily WHERE business_id = %s AND channel = %s", (biz, NB)
    ).fetchone()["day"]
    if earliest is not None and earliest < first:
        first = earliest
    first = min(first, today)  # an account row stamped after the account's today (a clock ahead) still has today
    try:
        total = newsbreak.total_spend(settings, account_id, http=http)
        life, why, start = 0, None, first
        while start <= today and life is not None:
            end = min(start + timedelta(days=REPORT_WINDOW_DAYS - 1), today)
            rows = newsbreak.report(settings, account_id, start=start, end=end, timezone=zone_name, http=http)
            cents, why = _report_cents(rows, start, end)
            life = None if cents is None else life + cents
            start = end + timedelta(days=1)
    except newsbreak.NewsBreakError as e:
        return f"lifetime  not read: {_nb_failure(e)} (not part of the exit status)", {}
    if total is None:
        said = "NewsBreak shows no total (the account has no spending cap)"
    else:
        said = f"NewsBreak's total {total} cents"
    if life is None:
        return f"lifetime  {said}; its HOURLY report holds {why} (not part of the exit status)", {}
    gap = f"difference {total - life} cents" if total is not None else "no difference to show"
    line = (
        f"lifetime  {said}, its HOURLY report {first}..{today} {life} cents: {gap} (it may include spend not yet in "
        "the hourly report; not part of the exit status)"
    )
    return line, {"lifetime_cents": total, "life_report_cents": life}


def _nb_reconcile(conn, biz: str, day_text: str) -> int:
    """Plan P10 (M1 Task 6, ruling T6-7): the business's NewsBreak spend on one closed day of the ad account's own
    calendar (`resources.time_zone`, America/Los_Angeles), from three sources, to the cent and in integers only:
    NewsBreak's HOURLY report (summed over the account's ads; a row with no cost, or a cost it puts on no day Admiry can
    read, fails the line: fix round 1, item 3, final review Minor 3), `metric_daily` (micros, which must be whole
    cents) and the balance's `spend` entry (`newsbreak:<day>`; none = 0). Exit 0 only when the three agree; a fourth
    line, the lifetime cross-check (`_lifetime_line`), never changes it. Prints figures and NewsBreak's own words,
    never the token; each run leaves an `ops.nb_reconcile` audit row with the figures."""
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        print(f"{day_text!r} is not a day (YYYY-MM-DD)", file=sys.stderr)
        return 1
    settings = config.load()
    account = registry.account(conn, biz, NB)
    row = ads_run.get(conn, biz, NB)
    resources = (row or {}).get("resources") or {}
    account_id = (account.external if account else {}).get("ad_account_id") or resources.get("ad_account_id")
    if not account_id:
        print(f"business {biz} has no NewsBreak ad account", file=sys.stderr)
        return 1
    if not settings.newsbreak_ready:
        print("set NEWSBREAK_ACCESS_TOKEN and NEWSBREAK_ORG_ID (the box .env) first", file=sys.stderr)
        return 1
    zone_name = resources.get("time_zone") or newsbreak.REPORT_TIMEZONE
    today = ads_run.account_today(row)
    if day >= today:
        print(f"{day} is not over yet in {zone_name} (today there is {today})", file=sys.stderr)
        return 1
    with newsbreak.client() as http:
        try:
            rows = newsbreak.report(settings, account_id, start=day, end=day, timezone=zone_name, http=http)
        except newsbreak.NewsBreakError as e:
            print(_nb_failure(e), file=sys.stderr)
            return 1
        reported, why = _report_cents(rows, day, day)
        found = conn.execute(
            "SELECT cost_micros FROM metric_daily WHERE business_id = %s AND channel = %s AND day = %s", (biz, NB, day)
        ).fetchone()
        micros = int(found["cost_micros"]) if found else 0
        entry = conn.execute(
            "SELECT amount_cents FROM wallet_entry WHERE business_id = %s AND kind = 'spend' AND ref = %s",
            (biz, f"{NB}:{day.isoformat()}"),
        ).fetchone()
        spent = -int(entry["amount_cents"]) if entry else 0
        lifetime, figures = _lifetime_line(conn, settings, http, biz, account_id, zone_name, today)
    whole = micros % 10_000 == 0
    print(
        f"{'newsbreak report':<18}{day}  "
        + (f"{reported} cents" if reported is not None else str(why))
        + f"  (HOURLY, {zone_name}, ad account {account_id})"
    )
    print(
        f"{'metric_daily':<18}{day}  "
        + (f"{micros // 10_000} cents" if whole else f"{micros} micros (not whole cents)")
        + ("" if found else "  (no row)")
    )
    print(f"{'wallet spend':<18}{day}  {spent} cents  ({NB}:{day.isoformat()}" + ("" if entry else ", no entry") + ")")
    print(lifetime)
    matches = reported is not None and whole and micros == reported * 10_000 and spent == reported
    if matches:
        print("lines 1-3 match to the cent", file=sys.stderr)
    else:
        print("lines 1-3 differ: see the figures above", file=sys.stderr)
    _audit(
        conn,
        "ops.nb_reconcile",
        {"business_id": biz, "channel": NB, "day": day.isoformat()},
        {
            "report_cents": reported,
            **({"report_why": why} if reported is None else {}),  # why the report could not be counted, as line 1 says
            "metric_micros": micros,
            "spend_cents": spent,
            "matches": matches,
            **figures,
        },
        biz,
    )
    return 0 if matches else 1


def _nb_locked(conn, biz: str) -> dict | None:
    """The business's NewsBreak account row, locked until the caller's transaction ends: a step writes it too (read,
    merged, written whole)."""
    return conn.execute(
        "SELECT mode, status, external FROM channel_account WHERE business_id = %s AND channel = %s FOR UPDATE",
        (biz, NB),
    ).fetchone()


def _nb_stop(conn, biz: str) -> str | None:
    """The sentence the business's NewsBreak row waits on (`channel_campaign.last_error`): SPEND_CHECK for a change of
    the owner's, MONTH_CHECK for the hourly read's own cap of a new month (M1 Task 7 fix round 1, M4)."""
    row = conn.execute(
        "SELECT last_error FROM channel_campaign WHERE business_id = %s AND channel = %s", (biz, NB)
    ).fetchone()
    return row["last_error"] if row else None


def _nb_account_ready(conn, biz: str) -> int:
    """Ruling T6-5 (M1 Task 6): an operator fixed a NewsBreak account at NewsBreak. Two clearings, each audited
    (`ops.nb_account_ready`): the "waiting at NewsBreak" mark Task 5a sets -- an account NewsBreak made other than
    ACTIVE, or refused to make (its allowance) -- goes (`account_status`, `account_checked_at`, `account_mailed`; the
    account row active again, or `none` while there is no ad account), so the next hourly step goes on without
    waiting out `RECHECK`; and a SPEND_CHECK stop: NewsBreak's lifetime total is read and recorded as the last cap
    Admiry set (`spending_cap_cents`) only when it is above it beyond NewsBreak's own overshoot -- recording a lower
    total would make a later, ordinary spend look like a mismatch -- and the mail mark (`spend_check_mailed_at`)
    goes; the audit row says when the stop was the read's own for a new month (`held` "month": its own activity line,
    M1 Task 7 fix round 1). Nothing to clear says so, exit 0; without NewsBreak's total a spend check stays, exit 1."""
    with conn.transaction():
        row = _nb_locked(conn, biz)
        if row is None:
            print(f"business {biz} has no NewsBreak account", file=sys.stderr)
            return 1
        external = dict(row["external"])
        said = external.get("account_status")
        waited = said not in (None, nb_adapter.ACTIVE)
        if waited:
            external = {key: value for key, value in external.items() if key not in WAIT_MARKS}
            status = "active" if external.get("ad_account_id") else "none"
            registry.save_account(conn, biz, NB, mode=row["mode"], status=status, external=external)
            _audit(
                conn,
                "ops.nb_account_ready",
                {"business_id": biz, "channel": NB},
                {"cleared": "account", "was": said},
                biz,
            )
    if waited:
        print(
            f"cleared the wait at NewsBreak ({said}) for business {biz}: the next hourly step goes on", file=sys.stderr
        )
    last, marked = external.get("spending_cap_cents"), bool(external.get("spend_check_mailed_at"))
    if last is None and not marked:
        if not waited:
            print(f"nothing waits at NewsBreak for business {biz}: nothing to clear", file=sys.stderr)
        return 0
    settings = config.load()
    account_id = external.get("ad_account_id")
    if not settings.newsbreak_ready or not account_id:
        if marked:
            print(
                "cannot read NewsBreak's total (no NewsBreak settings or ad account): the spend check stays",
                file=sys.stderr,
            )
            return 1
        if not waited:
            print(f"nothing waits at NewsBreak for business {biz}: nothing to clear", file=sys.stderr)
        return 0
    try:
        total = newsbreak.total_spend(settings, account_id)
    except newsbreak.NewsBreakError as e:
        print(f"{_nb_failure(e)}; the spend check stays", file=sys.stderr)
        return 1
    over = total is not None and last is not None and total > int(last) + nb_adapter.cap_overshoot_cents(int(last))
    if not over and not marked:
        if not waited:
            print(f"nothing waits at NewsBreak for business {biz}: nothing to clear", file=sys.stderr)
        return 0
    with conn.transaction():
        row = _nb_locked(conn, biz)
        current = dict(row["external"])
        if over:
            current["spending_cap_cents"] = total
        current.pop("spend_check_mailed_at", None)
        registry.save_account(conn, biz, NB, mode=row["mode"], status=row["status"], external=current)
        cleared = {"cleared": "spend_check", "recorded": over, "total_cents": total, "last_cap_cents": last}
        # the read's own stop for a new month: its cap goes in, no change of the owner's waited
        if _nb_stop(conn, biz) == nb_adapter.MONTH_CHECK:
            cleared["held"] = "month"
        _audit(conn, "ops.nb_account_ready", {"business_id": biz, "channel": NB}, cleared, biz)
    if over:
        print(
            f"recorded NewsBreak's total {total} cents as the last cap Admiry set for business {biz} (it was {last}): "
            "the next hourly step sets the cap from the recorded base",
            file=sys.stderr,
        )
    else:
        print(
            f"NewsBreak's total {total} cents is within the last cap Admiry set ({last}): only the mail mark was "
            "cleared",
            file=sys.stderr,
        )
    return 0


def _geo_refresh(census_url: str, sheet_url: str) -> int:
    """Rebuild the two geo catalogs in agent/data (agent/geo.py); the row counts go to stdout, a refusal or a source
    that cannot be used to stderr, with both files left as they were."""
    try:
        counts = geo.refresh(census_url=census_url, sheet_url=sheet_url, out_dir=geo.DATA_DIR)
    except geo.RefreshError as e:
        print(f"geo-refresh: {e}", file=sys.stderr)
        return 1
    print(f"{geo.ZCTA_FILE}: {counts['zctas']} rows")
    print(f"{geo.NEWSBREAK_FILE}: {counts['newsbreak_zips']} rows")
    print("update the date and rows in agent/data/README.md, then commit both files", file=sys.stderr)
    return 0


def _stock_load(conn, manifest: str | None, files: str | None, families: list[str] | None) -> int:
    """Store the stock library (agent/sites/stock.py); the counts go to stdout, what failed and why to stderr."""
    path = Path(manifest) if manifest else stock.MANIFEST
    if not path.is_file():
        print(f"stock-load: no manifest at {path}", file=sys.stderr)
        return 1
    if files is not None and not Path(files).is_dir():
        print(f"stock-load: {files} is not a directory", file=sys.stderr)
        return 1
    out = stock.load(conn, path, fetch=stock.fetch, files_dir=Path(files) if files else None, families=families or None)
    counts = {k: out[k] for k in ("loaded", "from_files", "fetched", "kept")} | {"failed": len(out["failed"])}
    _audit(conn, "ops.stock_load", {"manifest": str(path), "files": files, "families": families or []}, counts, None)
    print(
        f"loaded {out['loaded']} ({out['from_files']} from files, {out['fetched']} fetched), kept {out['kept']}, "
        f"failed {len(out['failed'])}"
    )
    for file, reason in out["failed"]:
        print(f"{file}: {reason}", file=sys.stderr)
    return 1 if out["failed"] else 0


def _print_built(conn, built: site_build.Built, prefix: str = "") -> dict:
    """A site build's lines (Q22): its address, the render check's loads with the screenshot sizes kept and whether it
    ran out of time (the pages it never opened when it did: Review Focus "partial"), Google's scores or why none; the
    notes to stderr. Returns what the audit row keeps. The preview's token is printed on purpose: an operator-only
    dev command, a token meant to be shared (the founder's review, publish_page, clearing it), and it expires."""
    print(f"{prefix}preview {built.preview_url} (token {built.token}, until {built.expires_at})")
    print(f"{prefix}files {built.files}, {built.bytes / 1_000_000:.1f} MB")
    checked = built.render
    if checked is None:
        print(f"{prefix}render check not run")
    else:
        shots, _spec_id = gallery._kept(conn, built.token)
        sizes = ", ".join(size for size, _label in gallery.SIZES if size in shots) or "no screenshots"
        print(f"{prefix}loads {checked['loads']} ({sizes}), partial {'yes' if checked['partial'] else 'no'}")
        if checked["partial"]:
            print(f"{prefix}not opened {', '.join(checked['not_opened']) or '(none named)'}")
    scores = built.pagespeed
    if scores is None:
        print(f"{prefix}speed test not run")
    elif "error" in scores:
        print(f"{prefix}speed test did not answer")
    else:
        lcp = f" (lcp {scores['lcp_ms']} ms)" if scores.get("lcp_ms") is not None else ""
        print(
            f"{prefix}pagespeed performance {scores['performance']}, accessibility {scores['accessibility']}, "
            f"best practices {scores['best_practices']}, seo {scores['seo']}{lcp}"
        )
    for note in built.warnings:
        print(f"{prefix}note: {note}", file=sys.stderr)
    return {
        "preview_url": built.preview_url,
        "expires_at": built.expires_at,
        "files": built.files,
        "loads": checked["loads"] if checked else None,
        "partial": checked["partial"] if checked else None,
        "performance": scores.get("performance") if scores else None,
    }


def _site_failure(e: Exception) -> dict:
    """A site build that did not finish, on stderr (a template fault's every problem with its fix); the audit's result."""
    if isinstance(e, site_build.TemplateFault):
        over = isinstance(e, gallery.OverLimit)
        print(gallery.OVER if over else "Admiry's template failed a check:", file=sys.stderr)
        for p in e.problems:
            fix = f" -- {p['fix']}" if p.get("fix") else ""
            print(f"  {p.get('file') or '/'} [{p.get('rule')}] {p.get('message')}{fix}", file=sys.stderr)
        return {"error": "over_limit" if over else "template_fault", "problems": e.problems[:20]}
    if isinstance(e, ValidationError):  # a sample document pydantic refuses: one line, never a traceback
        line = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        print(f"The sample is not a site document Admiry can read: {line}", file=sys.stderr)
        return {"error": "ValidationError", "message": line}
    print(str(e), file=sys.stderr)
    return {"error": type(e).__name__, "message": str(e)}


_SITE_ERRORS = (site_build.TemplateFault, render_check.Unavailable, ToolError, ValidationError, llm.LLMError)


def _site_preview(conn, biz: str, sample: str, style: str, accent: str | None, words_from: str, days: int) -> int:
    """`site-preview` (Q22): one sample in one style as the business's bundle preview, kept `days` days."""
    settings = config.load()
    args = {"business_id": biz, "sample": sample, "style": style, "accent": accent, "words": words_from, "days": days}
    try:
        site = samples.load(sample, style=style, accent=accent, words=words_from == "fixture")
        if words_from == "model":
            site = site.model_copy(update={"words": words.write(conn, settings, site)})
        built = site_build.preview(conn, settings, biz, site, sample=sample, ttl=timedelta(days=days))
    except _SITE_ERRORS as e:
        _audit(conn, "ops.site-preview", args, _site_failure(e), biz, ok=False)
        return 1
    _audit(conn, "ops.site-preview", args, _print_built(conn, built), biz)
    return 0


def _site_gallery(conn, biz: str, days: int, samples_named: tuple[str, ...] | None = None) -> int:
    """`site-gallery` (Q22): the six -- or the samples named (`--samples`, 2026-10-08: a trade's own) -- printed as each
    is built, then the index bundle's address last."""
    settings = config.load()
    args = {"business_id": biz, "days": days, "samples": list(samples_named or gallery.REVIEWED)}
    done: dict[str, dict] = {}

    def each(name: str, built: site_build.Built) -> None:
        done[name] = _print_built(conn, built, prefix=f"{name}: ")

    try:
        built = gallery.build(conn, settings, biz, ttl=timedelta(days=days), on_built=each, samples_named=samples_named)
    except _SITE_ERRORS as e:
        _audit(conn, "ops.site-gallery", args, {**_site_failure(e), "built": list(done)}, biz, ok=False)
        return 1
    result = {"preview_url": built.preview_url, "expires_at": built.expires_at, "files": built.files, "sites": done}
    _audit(conn, "ops.site-gallery", args, result, biz)
    print(f"gallery {built.preview_url}")
    return 0


LOOK_WIDTH = 390  # a look's picture: the phone's width the render check opens (agent/sites/looks.py)
LOOK_QUALITY = 72


def _look_jpeg(shot: bytes) -> bytes:
    """A gallery's phone screenshot as a look's picture: at most LOOK_WIDTH wide, JPEG at LOOK_QUALITY."""
    with Image.open(io.BytesIO(shot)) as img:
        img = img.convert("RGB")
        if img.width > LOOK_WIDTH:
            img = img.resize((LOOK_WIDTH, round(img.height * LOOK_WIDTH / img.width)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=LOOK_QUALITY, optimize=True)
    return buf.getvalue()


def _site_looks(conn, biz: str, out: str) -> int:
    """`site-looks` (A3p-6 batch 2 Q5): the phone screenshots of the business's live gallery preview (`site-gallery`)
    written into `out` as the six looks' pictures, `<sample>-<style>.jpg` (`_look_jpeg`), one path printed per file;
    `--out agent/static/sites/looks` refreshes the ones the homepage and build_site show. All six or none: nothing is
    written without a live gallery or with a screenshot missing (exit 1, on stderr). A read and local files: no row."""
    row = conn.execute(
        "SELECT v.token, v.sections->'gallery' AS looks FROM page p JOIN page_preview v ON v.page_id = p.id AND v.kind = 'bundle' "
        "AND v.expires_at > now() WHERE p.business_id = %s AND v.sections ? 'gallery' "
        "ORDER BY v.created_at DESC LIMIT 1",
        (biz,),
    ).fetchone()
    if row is None:
        print(f"site-looks: business {biz} has no gallery live; build one with site-gallery first", file=sys.stderr)
        return 1
    files = bundles.preview_files(conn, row["token"])
    shown = list(row["looks"] or [])  # the looks this gallery holds: the six, or the samples it was built for
    shots: dict[str, bytes] = {}
    for name in shown:
        sha = files.get(f"shots/{name}-phone.jpg")
        blob = conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (sha,)).fetchone() if sha else None
        if blob is not None:
            shots[name] = bytes(blob["bytes"])
    missing = [name for name in shown if name not in shots]
    if missing:
        print(f"site-looks: the gallery has no phone screenshot of {', '.join(missing)}", file=sys.stderr)
        return 1
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    for name, shot in shots.items():
        path = folder / f"{name}.jpg"
        path.write_bytes(_look_jpeg(shot))
        print(path)
    return 0


def _capacity_arg(text: str | None) -> int | None:
    """`--capacity` / the capacity command's value: a whole number, or none/unlimited for no limit."""
    if text is None or text.strip().lower() in ("none", "unlimited"):
        return None
    try:
        return int(text)
    except ValueError:
        raise google_managed.PoolRefused(google_managed.BAD_CAPACITY) from None


def _pool_line(a: google_managed.Pooled) -> str:
    cap = "no limit" if a.capacity is None else str(a.capacity)
    return (
        f"{a.group} {a.display} {a.status} live {a.live}/{cap} name={a.descriptive_name!r} tz={a.time_zone} "
        f"added {a.added_at:%Y-%m-%d} by {a.added_by}" + (f" note={a.note!r}" if a.note else "")
    )


def _google_account_add(conn, text: str, scope: str, capacity: str | None, note: str | None) -> int:
    """A3c-1: read the account from Google (through Admiry's manager account) and add it to the pool; refused when
    Google cannot read it, or it cannot carry a prepaid business's ads (`google_managed.probe`'s problems)."""
    try:
        cid = google_ads_link.normalize_customer_id(text)
        scope = google_managed.normalize_scope(scope)
        cap = _capacity_arg(capacity)
    except (google_ads_link.LinkRefused, google_managed.PoolRefused) as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        found = google_managed.probe(config.load(), cid)
    except google_ads.AdsError as e:
        print(f"Google could not read {google_managed.format_customer_id(cid)}: {e}", file=sys.stderr)
        return 1
    for field in ("descriptive_name", "currency_code", "time_zone", "status", "test_account"):
        print(f"{field}: {found[field]}")
    if found["problems"]:
        for problem in found["problems"]:
            print(f"refused: {problem}", file=sys.stderr)
        return 1
    try:
        with conn.transaction():
            added = google_managed.add(conn, config.load(), cid, scope, capacity=cap, by=ACTOR, found=found, note=note)
            _audit(
                conn,
                "ops.google_pool_add",
                {"customer_id": cid, "scope": scope, "capacity": cap, "note": note},
                {"group": added.group, "customer_id": added.display},
                None,
            )
    except google_managed.PoolRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    print(_pool_line(added))
    print(f"added {added.display} as {added.group}: name it so in Google Ads", file=sys.stderr)
    return 0


def _google_account_set(conn, text: str, *, capacity: str | None = None, status: str | None = None) -> int:
    """A pool account's capacity, or open/closed."""
    try:
        cid = google_ads_link.normalize_customer_id(text)
        with conn.transaction():
            if status is not None:
                done = google_managed.set_status(conn, cid, status)
                args = {"customer_id": cid, "status": status}
            else:
                done = google_managed.set_capacity(conn, cid, _capacity_arg(capacity))
                args = {"customer_id": cid, "capacity": done.capacity}
            _audit(conn, "ops.google_pool_change", args, {"customer_id": done.display}, None)
    except (google_ads_link.LinkRefused, google_managed.PoolRefused) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(_pool_line(done))
    return 0


def _google_account_bind(conn, biz: str, text: str, force: bool, note: str | None) -> int:
    try:
        cid = google_ads_link.normalize_customer_id(text)
        with conn.transaction():
            bound = google_managed.bind(conn, config.load(), biz, cid, by=ACTOR, note=note, force=force)
            _audit(
                conn,
                "ops.google_account_bind",
                {"business_id": biz, "customer_id": cid, "force": force, "note": note},
                {"customer_id": bound.customer_id, "binding": bound.id, "family": bound.family, "trade": bound.trade},
                biz,
            )
    except (google_ads_link.LinkRefused, google_managed.PoolRefused) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{biz} runs Google Search in {bound.display}", file=sys.stderr)
    return 0


def _google_account_release(conn, biz: str, force: bool) -> int:
    try:
        with conn.transaction():
            was = google_managed.release(conn, biz, force=force)
            _audit(
                conn,
                "ops.google_account_release",
                {"business_id": biz, "force": force},
                {"customer_id": was.customer_id, "binding": was.id},
                biz,
            )
    except google_managed.PoolRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{biz} released from {was.display}; its next launch picks an account again", file=sys.stderr)
    return 0


def _google_account(conn, args) -> int:
    """`google-account`'s commands that need the database."""
    if args.action == "add":
        return _google_account_add(conn, args.customer_id, args.scope, args.capacity, args.note)
    if args.action == "capacity":
        return _google_account_set(conn, args.customer_id, capacity=args.value)
    if args.action in ("close", "open"):
        return _google_account_set(conn, args.customer_id, status="closed" if args.action == "close" else "open")
    if args.action == "bind":
        return _google_account_bind(conn, args.business_id, args.customer_id, args.force, args.note)
    if args.action == "release":
        return _google_account_release(conn, args.business_id, args.force)
    if args.action == "show":
        for b in google_managed.history(conn, args.business_id):
            ended = f"released {b.released_at:%Y-%m-%d %H:%M}" if b.released_at else "live"
            trade = f"/{b.trade}" if b.trade else ""
            noted = f" -- {b.note}" if b.note else ""
            print(f"{b.display} {b.family}{trade} bound {b.bound_at:%Y-%m-%d %H:%M} by {b.bound_by} {ended}{noted}")
        return 0
    if args.action == "waiting":
        for w in google_managed.waiting(conn):
            room = "room" if w["room"] else "no room: " + "; ".join(w["reasons"])
            print(f"{w['business_id']} since {w['since']:%Y-%m-%d} scopes {','.join(w['scopes'])} {room}")
        return 0
    for a in google_managed.pool(conn):  # list
        print(_pool_line(a))
        if args.businesses:
            for b in google_managed.businesses(conn, a.customer_id):
                noted = f" -- {b.note}" if b.note else ""
                print(f"  {b.business_id} bound {b.bound_at:%Y-%m-%d} by {b.bound_by}{noted}")
    return 0


def _hosting_exempt(conn, biz: str, reason: str | None, clear: bool) -> int:
    """P18: exempt the business from the hosting minimum (a running countdown cleared without an e-mail), or end it."""
    if clear:
        with conn.transaction():
            if not hosting_minimum.clear_exempt(conn, biz):
                print(f"no business {biz}", file=sys.stderr)
                return 1
            _audit(conn, "ops.hosting_exempt", {"business_id": biz}, {"note": hosting_minimum.NOTE_UNEXEMPT}, biz)
        print(f"{biz}: the hosting minimum applies again from the next daily run", file=sys.stderr)
        return 0
    words = (reason or "").strip()
    if not words:
        print("give --reason (the pilot, a partner) or --clear", file=sys.stderr)
        return 1
    with conn.transaction():
        if not hosting_minimum.set_exempt(conn, biz, words, by=ACTOR):
            print(f"no business {biz}", file=sys.stderr)
            return 1
        note = hosting_minimum.NOTE_EXEMPT.format(reason=words)
        _audit(conn, "ops.hosting_exempt", {"business_id": biz, "reason": words}, {"note": note}, biz)
    print(f"{biz}: hosting is free whatever its ads spend ({words})", file=sys.stderr)
    return 0


def _hosting(conn, biz: str) -> int:
    """The hosting minimum as the daily job judges the business today (read only, no audit row)."""
    if business.row(conn, biz) is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    settings = config.load()
    day = hosting_minimum.today(conn, biz)
    cov = hosting_minimum.coverage(conn, biz, day=day)
    print(f"today {day} (the business's calendar)")
    print(f"covered {'yes' if cov.covered else 'no'} ({cov.why})")
    print(f"live monthly ${cov.live_usd:,.2f} · spent in {hosting_minimum.WINDOW_DAYS} days ${cov.spent_usd:,.2f}")
    print(
        f"ready at ${hosting_minimum.MIN_MONTHLY_USD} a month: ${hosting_minimum.ready_usd(conn, settings, biz):,.2f}"
    )
    exempt = hosting_minimum.exemption(conn, biz)
    print(f"exempt {exempt['reason']} (by {exempt['by']}, {exempt['at']:%Y-%m-%d})" if exempt else "exempt no")
    page = conn.execute(
        "SELECT id, source, status, hostname FROM page WHERE business_id = %s ORDER BY created_at DESC LIMIT 1", (biz,)
    ).fetchone()
    if page is None:
        print("page none")
        return 0
    h = hosting_minimum.hosting_row(conn, page["id"])
    print(f"page {page['id']} {page['source']} {page['status']} {page['hostname'] or '-'}")
    print(f"countdown ends {h['countdown_ends'] or '-'} · e-mails {h['countdown_mail'] or '-'}")
    print(
        f"taken down {h['taken_down_reason'] or '-'} {h['taken_down_at'] or ''} · files kept until {h['purge_after'] or '-'}"
    )
    return 0


def _local_yesterday(conn, biz: str) -> date:
    return ads_run.now().astimezone(digest_zone.zone_of(conn, biz)).date() - timedelta(days=1)


def _digest_preview(conn, biz: str, day_text: str | None, edition: str | None, no_note: bool) -> int:
    """The operator's rendering of a day's report (digest.preview): read only, nothing stored or sent, no audit row."""
    if business.row(conn, biz) is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    day = date.fromisoformat(day_text) if day_text else _local_yesterday(conn, biz)
    out = digest.preview(conn, config.load(), biz, day, edition=edition, note=not no_note)
    print(f"subject: {out['subject']}")
    print()
    print(out["text"])
    return 0


def _digest_flag(conn, biz: str, value: str) -> int:
    """The business's own digest switch (operator_flags.digest: on, operator or off; clear removes it). Audited with no
    business on the row (Dg P11): the switch is the team's, never the owner's activity."""
    with conn.transaction():
        if value == "clear":
            r = conn.execute(
                "UPDATE business SET operator_flags = coalesce(operator_flags, '{}'::jsonb) - 'digest' "
                "WHERE id = %s RETURNING id",
                (biz,),
            ).fetchone()
        else:
            r = conn.execute(
                "UPDATE business SET operator_flags = coalesce(operator_flags, '{}'::jsonb) || "
                "jsonb_build_object('digest', %s::text) WHERE id = %s RETURNING id",
                (value, biz),
            ).fetchone()
        if r is None:
            print(f"no business {biz}", file=sys.stderr)
            return 1
        _audit(conn, "ops.digest", {"business": biz, "value": value}, {"note": value}, None)
    print(f"{biz}: digest {value}", file=sys.stderr)
    return 0


def _email_status(conn, biz: str) -> int:
    """Mail goes to the business's address again after a bounce (as the Reports card's resume does)."""
    with conn.transaction():
        r = conn.execute("UPDATE business SET notify_email_status = 'ok' WHERE id = %s RETURNING id", (biz,)).fetchone()
        if r is None:
            print(f"no business {biz}", file=sys.stderr)
            return 1
        _audit(conn, "ops.email_status", {"business_id": biz}, {}, biz)
    print(f"{biz}: e-mails to the address resume", file=sys.stderr)
    return 0


SEED_PAUSES = ("owner", *digest_audience.SYSTEM_REASONS)


def _digest_seed(conn, biz: str, day_text: str, state: str, spend: str, clicks: int, calls: int, leads: int) -> int:
    """Dev only: a day of numbers for a business whose ads do not run, so the digest has something to report. The
    Google Search row is inserted only when absent and never with a campaign (launch and sync leave it alone); the
    numbers are the operator's (source operator), replacing that day's; --leads form leads land at local noon."""
    if config.load().env == "prod":
        print("digest-seed is for dev: it refuses on prod", file=sys.stderr)
        return 1
    if business.row(conn, biz) is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    reason = state.removeprefix("paused:") if state.startswith("paused:") else None
    if state != "live" and reason not in SEED_PAUSES:
        print(f"--state is live or paused:<reason>, the reason one of {', '.join(SEED_PAUSES)}", file=sys.stderr)
        return 1
    real = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM channel_campaign WHERE business_id = %s AND resources ? 'campaign') "
        "OR EXISTS (SELECT 1 FROM metric_daily WHERE business_id = %s AND source = 'api') AS real",
        (biz, biz),
    ).fetchone()["real"]
    if real:
        print(
            f"{biz} has real ads (a built campaign or Google's numbers): digest-seed never writes beside them",
            file=sys.stderr,
        )
        return 1
    day = date.fromisoformat(day_text)
    start, _ = digest_zone.bounds(day, digest_zone.zone_of(conn, biz))
    plan = ads_run.approved(conn, biz)
    with conn.transaction():
        added = conn.execute(
            "INSERT INTO channel_campaign (business_id, channel, plan_id, state, paused_reason, resources, "
            "launched_at, paused_at) VALUES (%s, 'google_search', %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (business_id, channel) DO NOTHING RETURNING channel",
            (
                biz,
                plan["id"] if plan else "seed",
                "live" if reason is None else "paused",
                None if reason in (None, "owner") else reason,
                Jsonb({"owner_paused": True} if reason == "owner" else {}),
                start - timedelta(days=1),
                start if reason else None,
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
            "conversions, source) VALUES (%s, 'google_search', %s, %s, 0, %s, %s, 0, 0, 'operator') "
            "ON CONFLICT (business_id, channel, day) DO UPDATE SET cost_micros = EXCLUDED.cost_micros, "
            "clicks = EXCLUDED.clicks, calls = EXCLUDED.calls, source = 'operator', synced_at = now()",
            (biz, day, int(Decimal(spend) * 1_000_000), clicks, calls),
        )
        for _ in range(leads):
            conn.execute(
                "INSERT INTO lead (id, business_id, kind, status, channel, fields, created_at) "
                "VALUES (%s, %s, 'form', 'new', 'google_search', '{}'::jsonb, %s)",
                (db.new_id("ld"), biz, start + timedelta(hours=12)),
            )
        args = {"business": biz, "day": day.isoformat(), "state": state, "spend": spend, "clicks": clicks}
        _audit(conn, "ops.digest_seed", {**args, "calls": calls, "leads": leads}, {}, None)
    if added is None:
        print(f"{biz}: the Google Search row was already there; its state stays as it was", file=sys.stderr)
    print(
        f"{biz}: {day} seeded (${Decimal(spend):,.2f}, {clicks} clicks, {calls} calls, {leads} leads)", file=sys.stderr
    )
    return 0


def _print_insight(v: dict) -> None:
    """analyst.team_view as lines: the row, the flags' rules, the candidates' ids, every sentence with why it was
    dropped, and the Critic's issues."""
    score = "-" if v["score"] is None else f"{v['score']:.2f}"
    print(
        f"insight {v['id']} {v['day']} status={v['status']} mode={v['mode']} verdict={v['verdict'] or '-'} "
        f"score={score} dropped={v['dropped'] or '-'} rounds={v['rounds']} calls={v['calls']}"
    )
    m = v.get("metrics") or {}
    print("flags: " + (", ".join(f["rule"] for f in m.get("flags", [])) or "-"))
    print("candidates: " + (", ".join(c["id"] for c in m.get("candidates", [])) or "-"))

    def why(dropped: str | None) -> str:
        return f"  [dropped: {dropped}]" if dropped else ""

    words = v.get("insight") or {}
    if words:
        print(f"summary: {words.get('summary')}{why(words.get('summary_dropped'))}")
    for f in words.get("findings", []):
        print(f"finding: {f['text']}{why(f.get('dropped'))}")
    for r in words.get("recommendations", []):
        print(f"recommendation {r['id']}: {r['text']}{why(r.get('dropped'))}")
    for i in (v.get("review") or {}).get("issues", []):
        print(f"issue: {i['dimension']}: {i['text']}")


def _insight(conn, biz: str, day_text: str | None) -> int:
    """The day's look as the team sees it (the latest by default); audited with no business on the row."""
    if day_text:
        row = conn.execute(
            "SELECT * FROM insight WHERE business_id = %s AND day = %s", (biz, date.fromisoformat(day_text))
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM insight WHERE business_id = %s ORDER BY day DESC LIMIT 1", (biz,)).fetchone()
    if row is None:
        print(f"{biz}: no look" + (f" for {day_text}" if day_text else ""), file=sys.stderr)
        return 1
    with conn.transaction():
        _audit(conn, "ops.insight", {"business": biz, "day": row["day"].isoformat()}, {}, None)
    _print_insight(analyst.team_view(row))
    return 0


def _insight_rebuild(conn, biz: str, day_text: str) -> int:
    """Dev only: the day's look built again now (analyst.rebuild), printed as `insight` prints it."""
    settings = config.load()
    if settings.env == "prod":
        print("insight-rebuild is for dev: it refuses on prod", file=sys.stderr)
        return 1
    if business.row(conn, biz) is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    day = date.fromisoformat(day_text)
    try:
        row = analyst.rebuild(conn, settings, biz, day)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    if row is None:
        print(
            f"{biz}: nothing to look at for {day} (its ads did not run that day, or AGENT_ROLES is off)",
            file=sys.stderr,
        )
        return 1
    with conn.transaction():
        _audit(conn, "ops.insight_rebuild", {"business": biz, "day": day.isoformat()}, {"status": row["status"]}, None)
    _print_insight(analyst.team_view(row))
    return 0


def _print_research(v: dict) -> None:
    """researcher.team_view as lines: the row, what it read, each round, the sources, every sentence and search with why
    it was dropped, and the Critic's issues."""
    score = "-" if v["score"] is None else f"{v['score']:.2f}"
    print(
        f"research {v['id']} {v['created_at'][:16]} status={v['status']} mode={v['mode']} trigger={v['trigger']} "
        f"verdict={v['verdict'] or '-'} score={score} confidence={v['confidence'] or '-'} "
        f"dropped={v['dropped'] or '-'} rounds={v['rounds']} calls={v['calls']}"
    )
    d = v.get("materials_digest") or {}
    print(f"read: places={d.get('places', '-')} fetched_more={d.get('fetched_more')} counts={d.get('counts')}")
    review = v.get("review") or {}
    for r in review.get("rounds", []):
        missing = ", ".join(r.get("missing") or []) or "-"
        print(f"round {r['round']}: verdict={r.get('verdict') or '-'} score={r.get('score')} missing={missing}")
    for s in d.get("sources", []):
        print(f"source {s['id']} [{s['kind']}] {s['label']}" + (f" {s['url']}" if s.get("url") else ""))

    def why(dropped: str | None) -> str:
        return f"  [dropped: {dropped}]" if dropped else ""

    b = v.get("brief_json") or {}
    for f in b.get("findings", []):
        print(
            f"finding ({f['confidence']}; {', '.join(f['source_ids']) or 'no source'}): {f['text']}{why(f['dropped'])}"
        )
    for key, label in (
        ("suggested_themes", "aim for"),
        ("suggested_negatives", "keep out"),
        ("offer_angles", "offer angle"),
        ("unknowns", "unknown"),
    ):
        for x in b.get(key, []):
            print(f"{label}: {x['text']}{why(x['dropped'])}")
    for i in (review.get("final") or {}).get("issues", []):
        print(f"issue: {i['dimension']}: {i['text']}")


def _not_run(conn, job_id: str) -> str:
    """Why `research --now` ran nothing, from what the job and the day say now: the handler skipped the job (its own
    reason, as it wrote it: the business's calls for the day are used up, nothing is known of the business),
    research-run took it, or run_now left it waiting (the day's research runs are used up, or the business has research
    running)."""
    job = conn.execute("SELECT business_id, status, result FROM agent_job WHERE id = %s", (job_id,)).fetchone()
    if job is not None and job["status"] == "skipped":
        reason = (job["result"] or {}).get("reason") or "no reason given"
        return f"the job was skipped before a run began: {reason}"
    if job is None or job["status"] != "queued":
        return f"research-run took the job ({job['status'] if job else 'gone'})"
    if researcher.runs_today_all(conn) >= researcher.MAX_RUNS_PER_DAY:
        return (
            f"the day's {researcher.MAX_RUNS_PER_DAY} research runs are used up; the job ({job_id}) waits for "
            "research-run, which takes it tomorrow (UTC)"
        )
    running = conn.execute(
        "SELECT 1 FROM agent_job WHERE business_id = %s AND role = %s AND status = 'running' LIMIT 1",
        (job["business_id"], researcher.ROLE),
    ).fetchone()
    if running is not None:
        return f"research is running for the business already; the job ({job_id}) waits for research-run"
    return f"the job did not run; it waits for research-run ({job_id})"


def _research(conn, biz: str, topics: list[str] | None, note: str | None, now: bool) -> int:
    """Research queued for the business by the operator (researcher.request's `operator`: no wait); with --now (dev
    only) run at once and printed -- unless the day's research runs are used up or the business has research running,
    when the job waits for research-run and the command says so. Audited with no business on the row."""
    settings = config.load()
    if now and settings.env == "prod":
        print("research --now is for dev: it refuses on prod (research-run takes it within the hour)", file=sys.stderr)
        return 1
    if business.row(conn, biz) is None:
        print(f"no business {biz}", file=sys.stderr)
        return 1
    with conn.transaction():
        asked = researcher.request(conn, settings, biz, trigger="operator", topics=topics, note=note, by=ACTOR)
        _audit(conn, "ops.research", {"business": biz, "topics": topics, "now": now}, {"queued": asked.queued}, None)
    if not asked.queued:
        print(f"{biz}: AGENT_ROLES is off; nothing queued", file=sys.stderr)
        return 1
    if not now:
        print(f"{biz}: research queued ({asked.job_id}); research-run takes it within the hour", file=sys.stderr)
        return 0
    row = researcher.run_now(conn, settings, asked.job_id)
    if row is None:
        print(f"{biz}: {_not_run(conn, asked.job_id)}", file=sys.stderr)
        return 1
    _print_research(researcher.team_view(row))
    return 0


def _research_show(conn, biz: str, run_id: str | None) -> int:
    """A research run as the team sees it (the latest by default); audited with no business on the row."""
    if run_id:
        row = conn.execute("SELECT * FROM research_run WHERE business_id = %s AND id = %s", (biz, run_id)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM research_run WHERE business_id = %s ORDER BY created_at DESC LIMIT 1", (biz,)
        ).fetchone()
    if row is None:
        print(f"{biz}: no research" + (f" {run_id}" if run_id else ""), file=sys.stderr)
        return 1
    with conn.transaction():
        _audit(conn, "ops.research_show", {"business": biz, "run": row["id"]}, {}, None)
    _print_research(researcher.team_view(row))
    return 0


def _research_due(conn) -> int:
    """The businesses automatic research is due for by the rules (`researcher.due`), how many then each id: all shards
    and no limit, so not one sweep's SWEEP_LIMIT a shard, by the runs AGENT_ROLES's mode can stand for (with it on,
    only research built on; off reads as validate, though with it off the sweep queues none). Reads ids only: no audit
    row."""
    ids = researcher.due(conn, config.load().roles_mode, limit=100_000)
    print(len(ids))
    for biz in ids:
        print(biz)
    return 0


def _knowledge(conn, days: int) -> int:
    """Each knowledge file as the operator reads it (spec §11: the review date shown to the operator; plan K3 P15): its
    review date, reviewer and age ("review due" past REVIEW_DUE_DAYS), confidence, sources, and how many consultation
    plan versions of the last `days` days cite it; an id cited whose file is gone; then how many versions cite any.
    Reads files and counts only -- no business's own data -- so it writes no audit row, as `plans` does."""
    try:
        entries = knowledge_index.load(knowledge_index.KNOWLEDGE_DIR)
    except knowledge_index.KnowledgeError as e:
        print(e, file=sys.stderr)
        return 1
    rows = conn.execute(
        "SELECT json->'sections'->'rationale'->'knowledge_ids' AS ids FROM plan "
        "WHERE created_at > now() - make_interval(days => %s) AND json->'sections' ? 'consultation'",
        (days,),
    ).fetchall()
    counts = Counter(i for r in rows for i in (r["ids"] or []))
    today = datetime.now(UTC).date()
    for e in entries:
        age = (today - e.reviewed_at).days
        due = ", review due" if age > knowledge_index.REVIEW_DUE_DAYS else ""
        print(
            f"{e.id} reviewed {e.reviewed_at.isoformat()} by {e.reviewed_by} ({age} days old{due}) "
            f"confidence={e.confidence} sources={len(e.sources)} cited={counts.get(e.id, 0)}"
        )
    known = {e.id for e in entries}
    for missing in sorted(set(counts) - known):
        print(f"{missing} cited={counts[missing]} (no such file now)")
    citing = sum(1 for r in rows if r["ids"])
    print(f"{citing} of {len(rows)} plan versions from a consultation in the last {days} days cite knowledge")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.ops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify").add_argument("business_id")
    sub.add_parser("issue-code").add_argument("proposal_id")
    sub.add_parser("proposals").add_argument("business_id")
    sub.add_parser("plans").add_argument("business_id")
    link = sub.add_parser("ads-link")
    link.add_argument("business_id")
    link.add_argument("customer_id")
    link.add_argument("--status", required=True, choices=("active", "pending", "refused"))
    link.add_argument("--replace", action="store_true", help="change the Customer ID of an existing link")
    sub.add_parser("google-ads-auth")
    probe = sub.add_parser("ads-probe")
    probe.add_argument("customer_id")
    probe.add_argument("--validate-campaign", action="store_true")
    refund = sub.add_parser("refund")
    refund.add_argument("business_id")
    refund.add_argument("amount_usd")
    refund.add_argument("--note")
    adjust = sub.add_parser("adjust")
    adjust.add_argument("business_id")
    adjust.add_argument("amount_usd")  # "-25.50" parses as a positional: no option looks like a number
    adjust.add_argument("--note", required=True)
    sub.add_parser("email-queue")
    reply = sub.add_parser("email-reply")
    reply.add_argument("thread_id")
    reply.add_argument("--text-file", required=True)
    stats = sub.add_parser("email-stats")
    stats.add_argument("--day", default=None)
    geo_refresh = sub.add_parser("geo-refresh")
    geo_refresh.add_argument(
        "--census-url", default=geo.CENSUS_URL, help="the Census Gazetteer ZCTA zip (default: 2026)"
    )
    geo_refresh.add_argument("--sheet-url", default=geo.SHEET_URL, help="CSV export of NewsBreak's location sheet")
    stock_load = sub.add_parser("stock-load")
    stock_load.add_argument("--manifest", help="the manifest (default: agent/data/stock/manifest.json)")
    stock_load.add_argument("--files", help="a copy of agent/data/stock/files/, read before any photo is fetched")
    stock_load.add_argument("--family", action="append", help="load only this family (repeatable)")
    site_preview = sub.add_parser("site-preview")
    site_preview.add_argument("business_id")
    site_preview.add_argument("--sample", required=True, choices=samples.names())
    site_preview.add_argument(
        "--style", required=True, help="roofer: solid, clean, bold; dentist: calm, warm, boutique"
    )
    site_preview.add_argument("--accent", default=None, help="#RRGGBB, read on the style's button")
    site_preview.add_argument("--words", choices=("fixture", "model"), default="fixture")
    site_preview.add_argument("--days", type=int, default=7, help="how long the preview stays (default 7)")
    site_gallery = sub.add_parser("site-gallery")
    site_gallery.add_argument("business_id")
    site_gallery.add_argument("--days", type=int, default=7, help="how long the gallery stays (default 7)")
    site_gallery.add_argument(
        "--samples", default=None, help="the samples to build, comma-separated (default: the six)"
    )
    site_looks = sub.add_parser("site-looks")
    site_looks.add_argument("business_id")
    site_looks.add_argument("--out", required=True, help="where the six pictures go (agent/static/sites/looks)")
    nb_probe = sub.add_parser("nb-probe")
    nb_probe.add_argument("--create-account", metavar="NAME", default=None)
    nb_probe.add_argument("--company", default=None, help="companyName of the new account (default: NAME)")
    nb_probe.add_argument("--category", choices=sorted(newsbreak.INDUSTRIES), default="fallback")
    reconcile = sub.add_parser("nb-reconcile")
    reconcile.add_argument("business_id")
    reconcile.add_argument("day", help="YYYY-MM-DD, a day of the ad account's own calendar that is over")
    sub.add_parser("nb-account-ready").add_argument("business_id")
    gacct = sub.add_parser("google-account").add_subparsers(dest="action", required=True)
    g_add = gacct.add_parser("add")
    g_add.add_argument("customer_id")
    g_add.add_argument("--scope", default="root", help="root (default: any business), a family, or <family>/<trade>")
    g_add.add_argument(
        "--capacity",
        default="1",
        help="the businesses it runs at most (default 1, one advertiser per account; none: no limit)",
    )
    g_add.add_argument("--note", default=None)
    gacct.add_parser("list").add_argument("--businesses", action="store_true")
    g_cap = gacct.add_parser("capacity")
    g_cap.add_argument("customer_id")
    g_cap.add_argument("value", help="a whole number, or none")
    gacct.add_parser("close").add_argument("customer_id")
    gacct.add_parser("open").add_argument("customer_id")
    g_bind = gacct.add_parser("bind")
    g_bind.add_argument("business_id")
    g_bind.add_argument("customer_id")
    g_bind.add_argument("--force", action="store_true", help="onto a closed or full account all the same")
    g_bind.add_argument("--note", default=None)
    g_rel = gacct.add_parser("release")
    g_rel.add_argument("business_id")
    g_rel.add_argument("--force", action="store_true", help="even while its ads are built in that account")
    gacct.add_parser("show").add_argument("business_id")
    gacct.add_parser("waiting")
    exempt = sub.add_parser("hosting-exempt")
    exempt.add_argument("business_id")
    which = exempt.add_mutually_exclusive_group(required=True)
    which.add_argument("--reason", help="why hosting stays free for it (the pilot, a partner)")
    which.add_argument("--clear", action="store_true", help="end the exemption")
    sub.add_parser("hosting").add_argument("business_id")
    dg_preview = sub.add_parser("digest-preview")
    dg_preview.add_argument("business_id")
    dg_preview.add_argument("--day", help="YYYY-MM-DD, the business's local day (default: its yesterday)")
    dg_preview.add_argument("--edition", choices=("daily", "weekly"))
    dg_preview.add_argument("--no-note", action="store_true", help="leave the stored look's take out")
    dg_flag = sub.add_parser("digest")
    dg_flag.add_argument("business_id")
    dg_flag.add_argument("value", choices=("on", "operator", "off", "clear"))
    email_status = sub.add_parser("email-status")
    email_status.add_argument("business_id")
    email_status.add_argument("value", choices=("ok",))
    dg_seed = sub.add_parser("digest-seed")
    dg_seed.add_argument("business_id")
    dg_seed.add_argument("--day", required=True, help="YYYY-MM-DD, the business's local day")
    dg_seed.add_argument("--state", default="live", help="live (default) or paused:<reason>")
    dg_seed.add_argument("--spend", default="0", help="US dollars, e.g. 23.10")
    dg_seed.add_argument("--clicks", type=int, default=0)
    dg_seed.add_argument("--calls", type=int, default=0)
    dg_seed.add_argument("--leads", type=int, default=0)
    look = sub.add_parser("insight")
    look.add_argument("business_id")
    look.add_argument("--day", help="YYYY-MM-DD (default: the latest look)")
    rebuild = sub.add_parser("insight-rebuild")
    rebuild.add_argument("business_id")
    rebuild.add_argument("--day", required=True, help="YYYY-MM-DD")
    rs = sub.add_parser("research")
    rs.add_argument("business_id")
    rs.add_argument("--topic", action="append", dest="topics", choices=research_material.TOPICS)
    rs.add_argument("--note", help="the question to look into, in words")
    rs.add_argument("--now", action="store_true", help="dev only: run it at once")
    show = sub.add_parser("research-show")
    show.add_argument("business_id")
    show.add_argument("--run", help="a research run's id (default: the latest)")
    sub.add_parser("research-due")
    sub.add_parser("eval-online").add_argument("--days", type=int, default=30, help="the window (default 30 days)")
    kn = sub.add_parser("knowledge")
    kn.add_argument("--days", type=int, default=30, help="the window of plan versions counted (default 30)")
    args = parser.parse_args(argv)
    if args.cmd == "google-ads-auth":  # no database: the token is printed, never stored
        return _google_ads_auth()
    if args.cmd == "ads-probe":  # no database: a read (and an optional validateOnly mutate) against Google alone
        return _ads_probe(args.customer_id, args.validate_campaign)
    if args.cmd == "nb-probe":  # no database: a read (and an optional account create) against NewsBreak alone
        return _nb_probe(args.create_account, args.company, args.category)
    if args.cmd == "geo-refresh":  # no database: two public downloads become two files in agent/data
        return _geo_refresh(args.census_url, args.sheet_url)
    with db.connect() as conn:
        if args.cmd == "verify":
            return _verify(conn, args.business_id)
        if args.cmd == "issue-code":
            return _issue_code(conn, args.proposal_id)
        if args.cmd == "ads-link":
            return _ads_link(conn, args.business_id, args.customer_id, args.status, args.replace)
        if args.cmd == "refund":
            return _refund(conn, args.business_id, args.amount_usd, args.note)
        if args.cmd == "adjust":
            return _adjust(conn, args.business_id, args.amount_usd, args.note)
        if args.cmd == "email-queue":
            return _email_queue(conn)
        if args.cmd == "email-reply":
            return _email_reply(conn, args.thread_id, args.text_file)
        if args.cmd == "email-stats":
            return _email_stats(conn, args.day)
        if args.cmd == "nb-reconcile":
            return _nb_reconcile(conn, args.business_id, args.day)
        if args.cmd == "nb-account-ready":
            return _nb_account_ready(conn, args.business_id)
        if args.cmd == "google-account":
            return _google_account(conn, args)
        if args.cmd == "hosting-exempt":
            return _hosting_exempt(conn, args.business_id, args.reason, args.clear)
        if args.cmd == "hosting":
            return _hosting(conn, args.business_id)
        if args.cmd == "digest-preview":
            return _digest_preview(conn, args.business_id, args.day, args.edition, args.no_note)
        if args.cmd == "digest":
            return _digest_flag(conn, args.business_id, args.value)
        if args.cmd == "email-status":
            return _email_status(conn, args.business_id)
        if args.cmd == "digest-seed":
            return _digest_seed(
                conn, args.business_id, args.day, args.state, args.spend, args.clicks, args.calls, args.leads
            )
        if args.cmd == "insight":
            return _insight(conn, args.business_id, args.day)
        if args.cmd == "insight-rebuild":
            return _insight_rebuild(conn, args.business_id, args.day)
        if args.cmd == "research":
            return _research(conn, args.business_id, args.topics, args.note, args.now)
        if args.cmd == "research-show":
            return _research_show(conn, args.business_id, args.run)
        if args.cmd == "research-due":
            return _research_due(conn)
        if args.cmd == "eval-online":
            print(online.text(online.metrics(conn, days=args.days, now=ads_run.now())))
            return 0
        if args.cmd == "knowledge":
            return _knowledge(conn, args.days)
        if args.cmd == "stock-load":
            return _stock_load(conn, args.manifest, args.files, args.family)
        if args.cmd == "site-preview":
            return _site_preview(conn, args.business_id, args.sample, args.style, args.accent, args.words, args.days)
        if args.cmd == "site-gallery":
            named = tuple(n.strip() for n in args.samples.split(",") if n.strip()) if args.samples else None
            try:
                gallery.chosen(named)
            except ValueError as e:
                print(f"site-gallery: {e}", file=sys.stderr)
                return 2
            return _site_gallery(conn, args.business_id, args.days, named)
        if args.cmd == "site-looks":
            return _site_looks(conn, args.business_id, args.out)
        if args.cmd == "proposals":
            for p in proposals.list_open(conn, args.business_id):
                print(f"{p['proposal_id']} {p['kind']} {p['class']} {p['expires_at']} {p['summary']}")
            return 0
        rows = conn.execute(
            "SELECT id, version, status, created_at FROM plan WHERE business_id = %s ORDER BY version",
            (args.business_id,),
        ).fetchall()
        for r in rows:
            print(f"{r['id']} v{r['version']} {r['status']} {r['created_at']:%Y-%m-%d %H:%M}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
