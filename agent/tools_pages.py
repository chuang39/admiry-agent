"""Admiry Pages tools (spec 2026-09-28-admiry-pages-design.md §3)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

from . import (
    ads_run,
    config,
    crawl,
    envelope,
    funnel,
    hosted,
    hosting_minimum,
    landing,
    leads,
    mail,
    plans,
    site_install,
    site_upload,
    support,
    support_mail,
)
from .channels import registry
from .mcp_app import agent_tool, need_business, run
from .util import iso

log = logging.getLogger(__name__)

NEXT_SET_PAGE = envelope.actions(
    {"id": "set_landing_page", "label": "Tell Admiry where the page is", "tool": "set_landing_page", "primary": True}
)
NEXT_CHECK_PAGE = envelope.actions(
    {"id": "check_landing_page", "label": "Check the page again", "tool": "check_landing_page", "primary": True}
)
NEXT_APPROVE_PLAN = envelope.actions(
    {"id": "approve_plan", "label": "Launch the plan", "tool": "approve_plan", "primary": True}
)
# H28: set_landing_page and the pages domain -- the business's own hosted address (not live), and any other name
OWN_HOSTED = "That address is your Admiry-hosted page; publish version {version} again instead"
NOT_ADMIRYS = "Only your own site or your Admiry page can be the ads' page"
GOOGLE = "google_search"  # the channel `execution["state"]` mirrors for one release (M0 Task 9 D12)
# check_landing_page's retry, for ads Admiry paused because their page is down (M0 Task 9 D3: no resume lifts that
# pause; naming the page that now passes does, `set_landing_page`)
RETRY_ONCE_NAMED = " once you name this page again as the ads' page"
# H29: and Admiry's own hosts -- a /p/<token> preview (24 hours), or anything else the agent serves
PREVIEW_LINK = "A preview link expires; publish the page or give the ads your own site's address"
# The kit's contact line for a business with no phone on file (M1 Task 4): an instruction for the assistant to carry out
# with the owner, in the line's `when` -- the line has no wording to print, and the owner's sign-in address is never it
NO_PHONE_ASK = "Add your business phone or a business e-mail address in the footer"


def _after_the_check(conn, biz: str, report: dict) -> dict:
    """What set_landing_page and check_landing_page offer once the page on file was checked: to check it again when it
    fails; when it passes, to launch the plan while the latest plan is a draft, and nothing once it is approved --
    there is nothing to launch (final review B: "Launch the plan" on an approved business)."""
    if not report["ok"]:
        return NEXT_CHECK_PAGE
    latest = plans.latest(conn, biz)
    return NEXT_APPROVE_PLAN if latest is not None and latest["status"] == "draft" else envelope.NONE


def _mail_note(reason: str, to: str | None, what: str) -> str:
    """The sentence that says what happened to the e-mail of `what` (the "kit" or the check "report")."""
    if reason == "sent":
        return f" I e-mailed it to {to} too."
    if reason == "unconfigured":
        return " (e-mail is not set up on this server, so it is only here.)"
    if reason == "no_owner":
        return " (No owner e-mail on file yet, so it is only here.)"
    if reason == "already":
        return f" (I e-mailed this version to {to} earlier.)"
    if reason == "unchanged":
        return " (Same result as the last check, so no new e-mail.)"
    return f" (The e-mail to {to} did not go through; the {what} is here.)"  # reason == "failed"


def _owner_to(conn, biz: str) -> str | None:
    """Where owner e-mail goes, or None (no address, or it bounced / unsubscribed). Ruling R1: business.row()
    does not select notify_email_status, so a direct SELECT reads both columns."""
    row = conn.execute("SELECT owner_email, notify_email_status FROM business WHERE id = %s", (biz,)).fetchone()
    return row["owner_email"] if row and row["notify_email_status"] == "ok" and row["owner_email"] else None


def _counts(report: dict) -> tuple[int, int]:
    return sum(1 for r in report["rules"] if r["ok"] is True), len(report["rules"])


def _summary(report: dict, *, lead: str = "The page passes") -> str:
    """The pass-count sentence, "<lead> N of 14 checks.". When the report isn't ok, "To fix: " lists the
    BLOCKING rules that actually failed (R7: a warning never blocks `ok`, so not-ok always means at least one
    BLOCKING rule is False). When it is ok but something still warned (a sensitive-category compliance note,
    say), "Worth doing: " carries the first couple of those warnings' fixes, so that advice still reaches the
    owner even though it never held launch up. `lead` lets an ad-hoc candidate check (R10) read "<host> would
    pass …" instead of the default "The page passes …"."""
    passed, total = _counts(report)
    say = f"{lead} {passed} of {total} checks."
    if not report["ok"]:
        fixes = [r["fix"] for r in report["rules"] if r["ok"] is False and r["id"] in landing.BLOCKING and r["fix"]]
        if fixes:
            say += " To fix: " + "; ".join(fixes[:3])
    else:
        fixes = [r["fix"] for r in report["rules"] if r["ok"] is None and r["fix"]]
        if fixes:
            say += " Worth doing: " + "; ".join(fixes[:2])
    return say


def _mail_report(conn, biz: str, report: dict, attachments: list[dict] | None = None) -> tuple[str | None, str]:
    """E-mail a saved check report to the owner (spec §6: every report goes out by e-mail too): (to, reason),
    the reason one of _mail_note's. `attachments` (A3p-3 batch 4): a published site bundle's screenshots."""
    to = _owner_to(conn, biz)
    if not mail.configured():
        return to, "unconfigured"
    if not to:
        return to, "no_owner"
    passed, total = _counts(report)
    marked = [{**r, "mark": "✓" if r["ok"] is True else ("✗" if r["ok"] is False else "!")} for r in report["rules"]]
    subject = f"Your landing page: {passed} of {total} checks pass"
    text = mail.render(
        "mail_check.txt.j2", report=report, rules=marked, passed=passed, total=total, shots=bool(attachments)
    )
    try:
        sent = mail.send(to, subject, text, attachments=attachments)
    except mail.MailError:
        return to, "failed"  # mail.py already logged the refusal/transport error; nothing to add here
    if sent is None:  # defensive, today unreachable: mail.configured() was already checked above
        return to, "unconfigured"
    return to, "sent"


def _admiry_address(conn, page: dict | None, host: str) -> bool:
    """set_landing_page never makes an address Admiry itself serves an owner's page. H28, the pages domain: Admiry
    serves those names, and only for a hosted row (H25). The business's own hosted hostname (kept on its one page
    row whatever the row's `source`) is a no-op while that hosted page is published (True: "already your
    Admiry-hosted page"); otherwise it is refused with publish_page, the tool that does bring it back -- the owner's
    own page live since (W6: the row turned owner at Admiry's hostname, a 404, and the live ads moved there) or the
    hosted one taken down (W5: it read as already live). Any other name under the pages domain (another business's,
    the bare domain) is refused outright. H29, Admiry's own hosts (the agent's public base and its browser host): a
    `/p/<token>` preview expires in 24 hours, and nothing else there is the owner's. False for every other host. A
    trailing dot is not a different name."""
    name = host.rstrip(".")
    own = page.get("hostname") if page else None
    if own and name == own:
        if page["source"] in ("hosted", "admiry") and page["status"] == "published":
            return True
        versions = hosted.versions(conn, page["id"])
        raise ToolError(OWN_HOSTED.format(version=versions[0]["version"] if versions else "n"))
    settings = config.load()
    if name in {urlparse(settings.public_base_url).hostname, urlparse(settings.web_base_url).hostname}:
        raise ToolError(PREVIEW_LINK)
    domain = settings.pages_domain
    if domain and (name == domain or name.endswith("." + domain)):
        raise ToolError(NOT_ADMIRYS)
    return False


def _ads_follow(conn, biz: str) -> dict:
    """The ads after set_landing_page moved the page under ads already built (`url_stale` queued, H27) -- or retried
    an address a platform refused, now that its page passes (H29) -- channel by channel for every channel whose ads
    follow the page (M0 Task 9), each classified like publish_page's (H28 item 5): paused -- nothing is sent until the
    resume, which moves them first, except that Admiry's pause for the page (it paused them because their page was
    down: unpublish_page, a refused move off the hosted page, or the hourly page-check) ends here, with the owner's own
    page live, exactly as publish_page ends it (H30, H24) -- never an owner's own pause, and never a pause for money
    (D2); that channel's switch `off` -- they keep their old address until execution is on (never ads_run.OFF, "not
    launched yet", which is wrong for ads that are running); otherwise one launch step right away (ads_run.launch_now,
    once for all of them).

    A move off the business's hosted page while a channel's switch keeps it from the platform (`off`, or `validate`,
    which never changes what is built) would leave running ads (`live`, `limited`) carrying an address that answers
    "not found" from now on: Admiry pauses them instead, owed to the platform (ruling S11) with the page-down reason
    (H29's pause, A3p-2 Task 3 final review), and the reply says so rather than "live" or "the old address". Ads not
    turned on yet (`launching`) serve nothing, and their own enable moves them first (H26).

    {"channels": {channel: {"state", "say"}}, "say": every channel's sentence, "state": Google Search's, for one
    release (A3p-2's single-channel shape, M0 Task 9 D12)}."""
    settings = config.load()
    out: dict[str, dict] = {}
    step: list[str] = []
    for row in ads_run.page_rows(conn, biz):
        channel = row["channel"]
        mode = registry.exec_mode(settings, channel)
        if row["state"] == "paused":
            reason = ads_run.page_pause(row)
            if reason and mode != "off":
                try:  # held to that pause again under the lock: one landing since this read stays (F3)
                    resumed = ads_run.resume(conn, settings, biz, channel, reason=reason)
                except Exception:  # the page is already live; a resume hiccup must not fail this reply
                    log.warning("set_landing_page: resume on %s failed for business %s", channel, biz, exc_info=True)
                else:
                    out[channel] = {"state": resumed["state"], "say": resumed["say"]}
                    continue
            out[channel] = {"state": "paused", "say": ads_run.status_sentence(ads_run.get(conn, biz, channel))}
            continue
        serving = row["state"] in ("live", "limited")
        if mode != "live" and serving and ads_run.pause_page_down(conn, settings, biz, channel):
            out[channel] = {"state": "paused", "say": ads_run.status_sentence(ads_run.get(conn, biz, channel))}
            continue
        if mode == "off":
            out[channel] = {"state": "off", "say": ads_run.OFF_OLD_ADDRESS.format(label=registry.label(channel))}
            continue
        step.append(channel)
    if step:
        launched = ads_run.launch_now(biz)  # never raises (its own contract)
        for channel in step:
            found = launched["channels"].get(channel)
            out[channel] = found or {"state": ads_run.get(conn, biz, channel)["state"], "say": launched["say"]}
    channels = {c: out[c] for c in registry.ORDER if c in out}
    return {
        "channels": channels,
        "say": " ".join(c["say"] for c in channels.values() if c.get("say")),
        "state": (channels.get(GOOGLE) or {}).get("state"),
    }


def _page_view(page: dict) -> dict:
    return {
        "id": page["id"],
        "url": page["url"],
        "status": page["status"],
        "checked_at": iso(page["checked_at"]) if page.get("checked_at") else None,
    }


def _footer_lines(conn, biz: str, kit: dict) -> list[dict]:
    """The `rules.required_lines` entries the channels a plan funds add to the kit (M1 Task 4, plan P8): for each of
    the landing check's `terms` and `contact` rules that a funded channel's `Capabilities.page_rules` name, one line
    saying what goes in the footer and who needs it ("NewsBreak requires it and Google is happier with it too").
    Not `always`: the rules judge them, `compliance` does not. Nothing when no funded channel asks (a plan on Google
    Search alone), so the kit and its e-mail read as they did. The contact line is the business's phone as the facts
    have it. With none on file there is nothing to print, so the line carries no wording (`text` is None) and its
    `when` is an instruction for the assistant to carry out with the owner (NO_PHONE_ASK) -- never a stand-in an
    assistant could print as it stands, and never the owner's sign-in address, which is the owner's to publish.
    Only the kit this tool hands out gets the lines: the check, the hosted pages and the recipe's own list never see
    them."""
    plan = plans.latest(conn, biz)
    asked: dict[str, list[str]] = {}  # rule id -> the funded channels whose page_rules name it, as the owner knows them
    for entry in plans.funded(plan["sections"]) if plan else []:
        if registry.known(entry["channel"]):
            for rule in registry.adapter(entry["channel"]).capabilities().page_rules:
                asked.setdefault(rule, []).append(registry.label(entry["channel"]))
    who = {
        rule: " and ".join(names) + (" requires" if len(names) == 1 else " require") for rule, names in asked.items()
    }
    phone = kit["facts"].get("phone")
    wording = (
        ("terms", "Terms of use", "in the footer, as a link"),
        ("contact", phone, "in the footer") if phone else ("contact", None, NO_PHONE_ASK),
    )
    return [
        {"text": text, "when": f"{where}; {who[rule]} it and Google is happier with it too", "always": False}
        for rule, text, where in wording
        if rule in who
    ]


@agent_tool(cls="W", scope="read")
def get_page_kit() -> dict:
    """Everything needed to build the landing page for this business's ads: the facts, the message the ads use,
    the page structure for this kind of business, the rules (Google's, the state's, the sensitive-category ones),
    the tracking snippet to put on the page, how its form sends to Admiry, and the checklist the page is judged
    by. You, the owner's assistant, build the page from the kit, anywhere; then name it with set_landing_page.
    Also e-mailed to the owner once per plan version."""

    def prepare(conn, p):
        biz = need_business(p)
        kit = landing.kit(conn, biz)
        # a new dict and a new list: `rules["required_lines"]` is the (cached) recipe's own list
        lines = _footer_lines(conn, biz, kit)
        kit["rules"] = {**kit["rules"], "required_lines": [*kit["rules"]["required_lines"], *lines]}
        to = _owner_to(conn, biz)
        if not mail.configured():
            return kit, to, "unconfigured"
        if not to:
            return kit, to, "no_owner"
        subject = f"Everything to build the page for your {kit['facts']['service']} ads"
        # before the claim: rendering needs no claim. The plan's "Your website" row and its four ways as links go in its
        # YOUR WEBSITE section (A3p-5 batch 2; rulings R0-e, R2-c)
        text = mail.render("mail_kit.txt.j2", kit=kit, website=site_install.email_section(conn, biz))
        # Ruling R6: claim, then send, both here in prepare (an autocommit connection: each statement is its
        # own transaction). The claim is a compare-and-swap on kit_plan_id, so two concurrent calls for the
        # same version can never both send: the loser's UPDATE matches no row and gets "already". Any failure
        # between the claim and a successful send releases it (kit_plan_id back to NULL) so a later call can
        # try again — not just a mail.MailError refusal, but anything else too (a bug in mail.send, for
        # example): a claim must never outlive the send that was supposed to justify it. body has nothing
        # left to do — mark_kit_sent (landing.py) is unused by this tool now.
        claimed = conn.execute(
            "UPDATE page SET kit_plan_id = %s, kit_sent_at = now() WHERE id = %s AND kit_plan_id IS DISTINCT "
            "FROM %s RETURNING id",
            (kit["plan_id"], kit["page_id"], kit["plan_id"]),
        ).fetchone()
        if not claimed:
            return kit, to, "already"

        def release() -> None:
            conn.execute("UPDATE page SET kit_plan_id = NULL, kit_sent_at = NULL WHERE id = %s", (kit["page_id"],))

        try:
            sent = mail.send(to, subject, text)
        except mail.MailError:
            # mail.py already logged the refusal/transport error; nothing to add here.
            release()
            return kit, to, "failed"
        except Exception:
            release()
            raise
        if sent is None:  # defensive, today unreachable: mail.configured() was already checked above
            release()
            return kit, to, "unconfigured"
        return kit, to, "sent"

    def body(conn, p, ctx):
        kit, to, reason = ctx
        say = (
            f"Here is everything to build the page for your {kit['facts']['service']} ads: the facts, the words the ads "
            f"use, the page's sections in order, the rules, and the tracking snippet to put on it. Build it anywhere, "
            f"then tell me where it is." + _mail_note(reason, to, "kit")
        )
        return envelope.make({**kit, "emailed": reason == "sent", "email": reason}, say=say, next=NEXT_SET_PAGE)

    return run("get_page_kit", "read", {}, body, prepare=prepare)


@agent_tool(cls="RO", scope="leads")
def list_leads(
    since: str | None = None,
    status: str | None = None,
    channel: str | None = None,
    kind: str | None = None,
    awaiting_outcome: bool = False,
    lead_id: str | None = None,
) -> dict:
    """The business's leads (forms, calls and the ones the owner reported), newest first: contact info, what they
    asked for, what each became (status, value_usd when won, reason when lost), where the row came from (source) and
    the channel whose ad click brought it in (channel, with the click_id; none for a visit that came on its own or a
    lead the owner reported). `since` (optional, an ISO date or time) limits it to leads from then on, `status` (new,
    contacted, booked, won, lost, spam) to those in that status, `channel` (google_search, newsbreak, ...) to one
    channel's, `kind` (form, call, other) to one kind, `awaiting_outcome` to those nobody has said the outcome of,
    `lead_id` to one. counts says how many are in each status."""

    def body(conn, p):
        biz = need_business(p)
        rows = leads.list_for(
            conn,
            biz,
            since,
            status=status,
            channel=channel,
            kind=kind,
            awaiting_outcome=awaiting_outcome,
            lead_id=lead_id,
        )
        say = (
            f"{len(rows)} lead(s)"
            + (f" from {registry.label(channel)}" if channel else "")
            + (f" marked {status}" if status else "")
            + (" waiting for an outcome" if awaiting_outcome else "")
            + (f" since {since}" if since else "")
            + ", newest first."
        )
        return envelope.make(
            {"leads": rows, "counts": leads.counts(conn, biz), "awaiting_outcome": leads.awaiting(conn, biz)}, say=say
        )

    args = {
        "since": since,
        "status": status,
        "channel": channel,
        "kind": kind,
        "awaiting_outcome": awaiting_outcome,
        "lead_id": lead_id,
    }
    return run("list_leads", "leads", args, body)


@agent_tool(cls="W", scope="leads")
def update_lead(
    lead_id: str,
    status: str,
    idempotency_key: str,
    note: str | None = None,
    value_usd: float | None = None,
    reason: str | None = None,
) -> dict:
    """Record what a lead became, once the owner says: status (new, contacted, booked, won, lost or spam); value_usd,
    what a won lead was worth in dollars; reason, why one was lost or is not a lead (out_of_area, wrong_service,
    no_answer, price, duplicate, not_a_customer, other); and, optionally, a note. lead_id is the lead's id from
    get_leads. This is what shows which ads bring real customers: record every outcome the owner mentions. Use a new
    idempotency_key per update. Marking a lead booked or won clears a reason given earlier."""

    def body(conn, p):
        biz = need_business(p)
        lead = leads.update(conn, biz, lead_id, status, note, value_usd=value_usd, reason=reason)
        # I6: the say never names the person -- it is stored in the audit row as it is, and a scrub works by key
        worth = f", worth {plans.usd(lead['value_usd'])}" if status == "won" and lead["value_usd"] is not None else ""
        return envelope.make(lead, say=f"Marked lead {lead['id']} as {lead['status']}{worth}.")

    return run(
        "update_lead",
        "leads",
        {  # I6: the owner's note is audited under a secret field's name
            "lead_id": lead_id,
            "status": status,
            "lead_note": note,
            "value_usd": value_usd,
            "reason": reason,
        },
        body,
        idempotency_key=idempotency_key,
    )


@agent_tool(cls="W", scope="leads")
def add_lead(
    kind: str,
    status: str,
    idempotency_key: str,
    happened_at: str | None = None,
    service: str | None = None,
    value_usd: float | None = None,
    note: str | None = None,
    contact: dict | None = None,
) -> dict:
    """Record a customer the owner says the ads brought and Admiry has no lead for: a phone call or a walk-in. kind
    is call, form or other; status is what it became (new, contacted, booked, won, lost, spam); value_usd what a won
    one was worth; happened_at the day it happened, in the business's own calendar (ISO date, today when omitted,
    never a day still to come); service what they wanted; contact an object with name, phone and email, each as
    text, when the owner gives them (no other key). note is a sentence worth keeping about it; to say why one was
    lost, call update_lead afterwards with a reason. Look in get_leads first: when the lead is already there, use
    update_lead instead. It counts in the business's own numbers and in no channel's. Use a new idempotency_key per
    lead."""

    def body(conn, p):
        biz = need_business(p)
        lead = leads.add_owner(
            conn,
            biz,
            kind=kind,
            status=status,
            calendar=funnel.calendar(conn, biz),  # the day it happened is a day in the account's own calendar
            happened_at=happened_at,
            service=service,
            value_usd=value_usd,
            note=note,
            contact=contact,
        )
        worth = f", worth {plans.usd(lead['value_usd'])}" if status == "won" and lead["value_usd"] is not None else ""
        return envelope.make(lead, say=f"Recorded lead {lead['id']} as {lead['status']}{worth}.")

    return run(
        "add_lead",
        "leads",
        {  # I6: a person's name and number, and the owner's note, only under secret field names
            "kind": kind,
            "status": status,
            "happened_at": happened_at,
            "service": service,
            "value_usd": value_usd,
            "lead_note": note,
            "contact": contact,
        },
        body,
        idempotency_key=idempotency_key,
    )


def name_page(conn, biz: str, url: str) -> tuple[bool, str, dict, dict | None, str | None, str | None, dict | None]:
    """set_landing_page's own steps, outside its transaction (its `prepare`), and the hourly watch's when a pending
    install's code first shows up on the owner's website and no page is on file (A3p-5 batch 2, ruling R3-a: the way
    Check now does): `url` becomes the business's one page, its check runs and is saved, the platform its fetch read is
    recorded once for the owner's own site (site_install.record_platform, ruling R1-c), a check that found Admiry's code
    fulfils the asks for it (site_install.fulfil, ruling F-1), the report is e-mailed, and the ads follow the address.
    (noop, host, page, report, to, reason, execution); noop for the business's own published hosted address, whose page
    comes back as it is with nothing else. Refused in a sentence (ToolError): an address the check will not fetch,
    Admiry's own hosts, no plan yet."""
    try:
        host = crawl.public_host(url, resolver=crawl.resolve)
    except crawl.CrawlError as e:
        raise ToolError(str(e).replace("website_url", "url")) from None
    current = landing.current(conn, biz)
    if _admiry_address(conn, current, host):
        return True, current["hostname"], current, None, None, None, None
    kit = landing.kit(conn, biz)  # I11: raises NO_PLAN before anything is written
    old_url = current["url"] if current else ""
    page = landing.set_owner_url(conn, biz, url)
    # H27 (round 3): shared with apply_publish_page (ads_run.flag_moved_page) rather than copied
    moved = ads_run.flag_moved_page(conn, biz, old_url, url)
    report = landing.check(url, page, kit, conn=conn)
    landing.save_check(conn, page["id"], report)
    site_install.record_platform(conn, biz, report)
    site_install.fulfil(conn, biz, report)
    # H29: naming the address a platform refused again, its page now passing, is the retry ("fix the page")
    retried = bool(report["ok"]) and ads_run.retry_refused_address(conn, biz, page["url"])
    to, reason = _mail_report(conn, biz, report)  # R12: the report it saves goes out by e-mail too
    page = landing.get(conn, page["id"])  # re-read: the returned view must carry the check just saved
    # H28 item 5 (W7): the ads step comes after the check is saved, as publish_page's after-commit hook does --
    # a first launch needs the page's check to pass, and set_owner_url has just cleared the old one.
    # M0 Task 9 D3: the owner's resume never lifts Admiry's pause for the page, so naming a page that passes its
    # check is a way out of it even when the address did not change (pointing the ads at a page that is up)
    held = bool(report["ok"]) and any(ads_run.page_pause(r) for r in ads_run.page_rows(conn, biz))
    execution = _ads_follow(conn, biz) if moved or retried or held else None
    return False, host, page, report, to, reason, execution


def check_on_file(conn, biz: str) -> tuple[dict, dict, str | None, str, str | None]:
    """check_landing_page's own steps for the page on file, outside its transaction (its `prepare`), and the hourly
    watch's when a pending install's code first shows up on the owner page the ads use (A3p-5 batch 2, ruling R3-a: the
    way Check now does): the page is checked again and the report saved, the platform its fetch read is recorded once
    for the owner's own site (site_install.record_platform, ruling R1-c), a check that found Admiry's code fulfils the
    asks for it (site_install.fulfil, ruling F-1: the watch of a pending install ends at the first pass, whoever's
    check it is), the report is e-mailed when whether it passes changed since the last saved check (or this is the
    first), and a passing check offers an address a platform refused to it again. (page, report, to, reason, retry):
    `reason` one of _mail_note's, "unchanged" when nothing was e-mailed for an unchanged result; `retry` the sentence
    for an address offered again, else None. Refused in a sentence (ToolError): no plan yet, no page with an address on
    file."""
    kit = landing.kit(conn, biz)
    page = landing.get(conn, kit["page_id"])
    if not page["url"]:
        raise ToolError(
            "There is no page to check yet: give an address to try one, or name the ads' page once you have one."
        )
    report = landing.check(page["url"], page, kit, conn=conn)
    prev = landing.saved_report(page)  # H18: page-check's counters alone are no report
    changed = prev is None or prev.get("ok") != report["ok"]
    landing.save_check(conn, page["id"], report)
    site_install.record_platform(conn, biz, report)
    site_install.fulfil(conn, biz, report)
    # H29: a saved check that passes at the address a platform refused is the retry ("fix the page"); the next
    # step sends it (the hourly sweep, or the resume of ads that are paused -- this read tool never calls a
    # platform itself)
    retry = None
    retried = ads_run.retry_refused_address(conn, biz, page["url"]) if report["ok"] else []
    if retried:
        rows = [ads_run.get(conn, biz, c) or {} for c in retried]
        if any(ads_run.page_pause(r) == "page_down" for r in rows):  # D3: never lifted by a resume
            when = RETRY_ONCE_NAMED
        elif any(r.get("state") == "paused" for r in rows):
            when = " once your ads are back on"
        else:
            when = ""
        retry = ads_run.ADDRESS_RETRY.format(labels=ads_run.labels(retried)) + when
    if not changed:
        return page, report, None, "unchanged", retry
    return page, report, *_mail_report(conn, biz, report), retry


@agent_tool(cls="W", scope="manage")
def set_landing_page(url: str, idempotency_key: str) -> dict:
    """Tell Admiry the address of the page the ads should send people to. It becomes the live page -- the
    business's one page, whose id (the tracking snippet's token) never changes with the address, so the
    snippet already on the page keeps working -- and Admiry runs the 14-rule check on it right away and
    e-mails the report to the owner. A page that doesn't pass yet can still be set -- fix what it flags, then
    call check_landing_page, or set_landing_page again with a corrected URL. Needs the plan first (the check
    judges the page against it). If ads are already live at a different address, they move to this one right
    away, same as publish_page; an address the ads platform would not take for them is tried again once its page
    passes the check; and ads Admiry paused because their page was down come back on (naming a page that passes
    the check again is how they do, when the address did not change). Given the business's own
    published Admiry-hosted address back, this is a no-op -- it says so and changes nothing; any other address
    under Admiry's pages domain is refused (the business's own hosted page, when it is not the live one, comes
    back with publish_page(version=n); no other name there is the owner's), and so is a preview link or anything
    else on Admiry's own site. Use a new idempotency_key per intended change."""
    # Rulings: the ads follow the address (H27); the pages domain (H28) and Admiry's own hosts (H29) are refused;
    # a passing check at a refused address is the retry (H29); Admiry's own pause ends here (H30).

    def prepare(conn, p):
        return name_page(conn, need_business(p), url)

    def body(conn, p, ctx):
        noop, host, page, report, to, reason, execution = ctx
        if noop:
            return envelope.make(
                {"page": _page_view(page), "check": None, "emailed": False, "email": None, "execution": None},
                say=f"That is already your Admiry-hosted page at https://{host}/; nothing changed.",
            )
        say = f"Your ads will send people to {host}. " + _summary(report) + _mail_note(reason, to, "report")
        if execution:  # H27: the ads followed the address, right in this reply
            say = f"{say} {execution['say']}"
        return envelope.make(
            {
                "page": _page_view(page),
                "check": report,
                "emailed": reason == "sent",
                "email": reason,
                "execution": execution,
            },
            say=say,
            next=_after_the_check(conn, need_business(p), report),
        )

    return run("set_landing_page", "manage", {"url": url}, body, idempotency_key=idempotency_key, prepare=prepare)


@agent_tool(cls="RO", scope="read")
def check_landing_page(url: str | None = None) -> dict:
    """Run the 14-rule check again: on the page already on file, or -- give `url` -- on a candidate address,
    tried without publishing it or touching anything saved for the live page (a dry run: nothing is saved and
    no e-mail goes out, so it's safe to try a draft before set_landing_page commits to it). Without `url`, the
    page on file is checked, the report is saved on it, and the owner is e-mailed when whether it passes
    changed since the last saved check, or this is the first one. When an ads platform would not take the page's
    address for the ads, a check of the page on file that passes has Admiry offer that address to it again (the
    page was fixed)."""

    def prepare(conn, p):
        biz = need_business(p)
        if url:
            # R10: a candidate is a dry run against the live page's own facts/rules -- report it back, but
            # never save it as the page's check, never touch checked_at, and never e-mail off it: to/reason
            # None marks this for `body` below.
            kit = landing.kit(conn, biz)
            page = landing.get(conn, kit["page_id"])
            return page, landing.check(url, page, kit, conn=conn), None, None, None
        return check_on_file(conn, biz)

    def body(conn, p, ctx):
        page, report, to, reason, retry = ctx
        if reason is None:  # R10: the ad-hoc, save-and-mail-nothing path
            host = urlparse(report["url"]).hostname or report["url"]
            say = _summary(report, lead=f"{host} would pass")
            # a candidate that passes can become the page; one that fails has its fixes in `say`
            nxt = NEXT_SET_PAGE if report["ok"] else envelope.NONE
            return envelope.make({"page": _page_view(page), "check": report}, say=say, next=nxt)
        say = _summary(report) + _mail_note(reason, to, "report")
        if retry:
            say = f"{say} {retry}."
        return envelope.make(
            {"page": _page_view(page), "check": report, "emailed": reason == "sent", "email": reason},
            say=say,
            next=_after_the_check(conn, need_business(p), report),  # as set_landing_page's
        )

    return run("check_landing_page", "read", {"url": url}, body, prepare=prepare)


@agent_tool(cls="W", scope="manage")
def send_install_steps(email: str, idempotency_key: str) -> dict:
    """E-mail the steps for putting Admiry's code on the owner's website, with the code, to whoever runs the site: one
    fixed message (the business's name, the site, the steps for its platform and the code, the same as the plan's "Your
    website" card shows), sent from Admiry with replies going to the owner, to the one address given. Only while the
    owner's website still needs the code; at most 3 a day for the business. Refused in a sentence, and nothing sent:
    an address that is not one, the owner's own address, a fourth in a day. Use a new idempotency_key per e-mail."""
    # A3p-5 batch 2, plan Q7 (spec W11): the logic is site_install.send_steps; the e-mail goes out in `prepare`. Ruling
    # R3-e: the address never lands in agent_action -- the argument is audited as `recipient` (audit.SECRET_FIELDS),
    # the stored answer, which a replay gives, says "your web person", and only this call's own answer names it
    sent_to: list[str] = []

    def prepare(conn, p):
        sent_to.append(site_install.send_steps(conn, need_business(p), email))

    def body(conn, p, _sent):
        return envelope.make({"way": "send"}, say=site_install.SENT_RECORDED)

    answer = run(
        "send_install_steps", "manage", {"recipient": email}, body, idempotency_key=idempotency_key, prepare=prepare
    )
    return {**answer, "say": site_install.SENT.format(address=sent_to[0])} if sent_to else answer


@agent_tool(cls="W", scope="manage")
def request_install_help(idempotency_key: str, contact: str | None = None) -> dict:
    """Ask Admiry's team to put Admiry's code on the owner's website: a request to a person at Admiry, with the site and
    its platform. The team reaches the owner at `contact` (an e-mail address), or at the owner's sign-in address when
    none is given; with neither, the answer asks for one, and the same call with `contact` sends the request on. The
    team asks the owner to add Admiry as a helper in the platform's own settings, never for a password. Only while the
    owner's website still needs the code. Use a new idempotency_key per request."""
    # A3p-5 batch 2, plan Q8 (spec W12): the logic is site_install.ask_team; SP1's ticket mail goes after the commit.
    # Ruling F-4 (the final review; R3-e's pattern): the contact, which may be anyone's address, never lands in
    # agent_action -- the argument is scrubbed (audit.SECRET_FIELDS), the stored answer, which a replay gives, names no
    # address, and only this call's own answer says Q8's reply with it
    said: list[str] = []

    def body(conn, p):
        answer, live = site_install.ask_team(
            conn,
            need_business(p),
            contact,
            source=support.source_of(p.key),
            actor=p.actor,
            ip=p.ip,
            draft=p.key.cls == "draft",
        )
        said.append(live)
        return answer

    answer = run(
        "request_install_help",
        "manage",
        {"contact": contact},  # audit.SECRET_FIELDS scrubs a contact from the stored arguments
        body,
        idempotency_key=idempotency_key,
        after=support_mail.after_tool,
    )
    return {**answer, "say": said[0]} if said else answer


@agent_tool(cls="RO", scope="read")
def get_page() -> dict:
    """The business's current landing page (its address, status, and when it was last checked), its version
    history when it is one Admiry hosts (hostname, source and every version -- `kind` html or bundle -- the live one
    marked), the last 14-rule check saved for it, if any, and what Admiry's hourly check last saw when it opened the
    page (`reachable`: failed fetches in a row, when, since when). For a page Admiry hosts, `hosting`: whether its ads
    keep hosting free ($200 a month), and when it comes down if they do not. `upload`: the last site uploaded for it
    (its status, its problems, or its preview link and token); `render`: a live site bundle as Admiry's daily look
    last found it (its problems). Call check_landing_page for a fresh check,
    preview_page/publish_page to build and publish a hosted page, request_site_upload for a whole site, or set_landing_page
    for the owner's own address."""

    def body(conn, p):
        biz = need_business(p)
        page = landing.current(conn, biz)
        # spec §4 (A3p-5 batch 2): the owner's own website and whether the page the ads use carries Admiry's code
        website = site_install.status(conn, biz)
        if page is None or not page.get("url"):
            # a site uploaded before any page is set (A3p-3 B12): its problems or its preview are still the status
            return envelope.make(
                {
                    "page": None,
                    "check": None,
                    "upload": site_upload.last(conn, page["id"]) if page else None,
                    "website": website,
                },
                say="No landing page is set yet. Admiry can write one, or you can name your own.",
            )
        is_hosted = page.get("source") in ("hosted", "admiry")
        # as the launch judges it (Message-match against the plan of now: advice on the owner's own page, R4; final
        # review minor 6), without page-check's counters, which `reachable` carries (H18)
        check = landing.judged_report(conn, page)
        if is_hosted and page["status"] != "published":
            # H22 review minor: a page taken down answers "gone" (410) -- say that, not that ads still go there.
            say = f'Your Admiry-hosted page at {page["url"]} answers "gone" until you publish it again.'
        else:
            lead = (
                f"Your ads send people to your Admiry-hosted page at {page['url']}"
                if is_hosted
                else (f"Your ads send people to {page['url']}")
            )
            if check is None:
                say = f"{lead}; it has not been checked yet."
            else:
                passed, total = _counts(check)
                say = f"{lead} (checked {iso(page['checked_at'])}: {passed}/{total} passing)."
        hosting = hosting_minimum.status(conn, biz, page)  # A3p-3 (plan Q10): None for the owner's own page
        if hosting and hosting["say"]:
            # the countdown's date, or until when a site taken down is kept (spec §3.2 "During", "Takedown")
            say = f"{say} {hosting['say']}"
        gone = site_install.code_gone(page)  # A3p-5 batch 2 (plan Q9, spec W15): the hourly check saw the code go
        if gone:
            say = f"{say} {gone}"
        data = {
            **_page_view(page),
            "hostname": page.get("hostname"),
            "source": page.get("source"),
            "versions": hosted.versions(conn, page["id"]) if is_hosted else [],
            "reachable": (page.get("check_json") or {}).get("reachable"),  # the hourly page-check's own counters
            "hosting": hosting,
            # A3p-3 batch 3 (B12): the last site uploaded for this page, its problems or its preview
            "upload": site_upload.last(conn, page["id"]),
            # A3p-3 batch 4 (R8): a live site bundle as the render check last opened it (once a day)
            "render": _render_view(conn, page["id"]) if is_hosted else None,
        }
        return envelope.make({"page": data, "check": check, "website": website}, say=say)

    return run("get_page", "read", {}, body)


def _render_view(conn, page_id: str) -> dict | None:
    """The live site bundle's last render (A3p-3 batch 4, R8): when, its problems, whether time ran out; None before
    the first."""
    row = conn.execute("SELECT render_json, rendered_at FROM page WHERE id = %s", (page_id,)).fetchone()
    if row is None or row["render_json"] is None:
        return None
    doc = row["render_json"]
    return {"at": iso(row["rendered_at"]), "problems": doc.get("problems") or [], "partial": bool(doc.get("partial"))}


NO_SOURCE = "Admiry hosts no page for this business, so there is no source here: its page is wherever it lives"


@agent_tool(cls="RO", scope="read")
def get_page_source() -> dict:
    """The page Admiry hosts for this business, as it is live: its whole HTML, Admiry's script tag included (kind
    "html"); or, for a site uploaded whole (kind "bundle"), its files ({path, size}) and `zip_url`, a link to all of
    them as one zip that works for an hour. Change it and send it back -- a page with preview_page, keeping that
    script tag as it is, a site with request_site_upload -- then publish_page; or keep it as a copy of the site. While a site
    taken down is kept (90 days), the version that was live. Refused when Admiry hosts nothing for the business."""

    def body(conn, p):
        biz = need_business(p)
        found = hosted.source(conn, biz)
        if found is None:
            raise ToolError(NO_SOURCE)
        url = f"https://{found['hostname']}/"
        where = f"live at {url}" if found["live"] else f"that was live at {url}"
        if found["kind"] == "bundle":
            settings = config.load()
            found.update(site_upload.zip_link(conn, found["page_id"], found["version"], settings.public_base_url))
            n = len(found["files"])
            say = (
                f"Here are the {n} files of version {found['version']} of your site, {where}. The link to download "
                "them all works for an hour. Change them, build the site and upload it again."
            )
            return envelope.make(found, say=say)
        say = (
            f"Here is the source of version {found['version']} of your page, {where}. Change it and send it back "
            "for a preview; keep Admiry's script tag in it as it is."
        )
        return envelope.make(found, say=say)

    return run("get_page_source", "read", {}, body)


STATS_PERIODS = {"7d": 7, "30d": 30}
BAD_STATS_PERIOD = "period must be one of 7d, 30d"
# page_event.kind, in get_page_stats' order (A3p-3 P27: a booking completed on the page, `booked_online`)
EVENT_KINDS = ("visit", "call", "mail", "book", "form", "booked_online")
STATS_KEYS = (*EVENT_KINDS, "leads", "ad_clicks")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def page_stats(conn, biz: str, days: int, today: date) -> list[dict]:
    """get_page_stats' `days`: the last `days` calendar days ending `today`, newest first, zero-filled -- per day
    the page's events by kind (every page row the business has had: the one row keeps its id, H25), its leads (not
    one the owner reported: no page brought it in), and the ads' clicks, every channel's summed
    (`metric_daily.clicks`, M0 Task 9 D13). Page events and leads are counted by their UTC date;
    `metric_daily.day` is each ads account's own calendar date (A3's ruling S14), put beside the UTC day of the same
    date."""
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=UTC)
    rows: dict[date, dict] = {first + timedelta(days=i): dict.fromkeys(STATS_KEYS, 0) for i in range(days)}
    for r in conn.execute(
        "SELECT (e.ts AT TIME ZONE 'UTC')::date AS day, e.kind, count(*) AS n FROM page_event e "
        "JOIN page p ON p.id = e.page_id WHERE p.business_id = %s AND e.ts >= %s GROUP BY 1, 2",
        (biz, since),
    ).fetchall():
        if r["day"] in rows and r["kind"] in EVENT_KINDS:
            rows[r["day"]][r["kind"]] = int(r["n"])
    for r in conn.execute(
        "SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS n FROM lead "
        "WHERE business_id = %s AND created_at >= %s AND source <> 'owner' GROUP BY 1",
        (biz, since),
    ).fetchall():
        if r["day"] in rows:
            rows[r["day"]]["leads"] = int(r["n"])
    for r in conn.execute(
        "SELECT day, sum(clicks) AS clicks FROM metric_daily WHERE business_id = %s AND day BETWEEN %s AND %s "
        "GROUP BY day",
        (biz, first, today),
    ).fetchall():
        rows[r["day"]]["ad_clicks"] = int(r["clicks"])
    return [{"day": day.isoformat(), **rows[day]} for day in sorted(rows, reverse=True)]


@agent_tool(cls="RO", scope="read")
def get_page_stats(period: str = "7d") -> dict:
    """What the business's page did, day by day, over the last 7 or 30 days (period "7d" or "30d"): visits, phone
    taps, e-mail taps, booking clicks, bookings made online and forms sent on the page, the leads it brought in, and the clicks the ads
    sent to it beside them (every channel's together), with the period's totals. Days with nothing are there with
    zeros. Days are calendar days in UTC for the page and its leads; the ads' clicks are counted by each ads
    account's own calendar day."""

    def body(conn, p):
        biz = need_business(p)
        if period not in STATS_PERIODS:
            raise ToolError(BAD_STATS_PERIOD)
        n = STATS_PERIODS[period]
        days = page_stats(conn, biz, n, datetime.now(UTC).date())
        totals = {k: sum(d[k] for d in days) for k in STATS_KEYS}
        # A3p-3 P27: bookings made online are said only once there is one (most pages embed no booking calendar)
        online = (
            f"{_plural(totals['booked_online'], 'booking made online', 'bookings made online')}, "
            if totals["booked_online"]
            else ""
        )
        say = (
            f"Your page over the last {n} days: {_plural(totals['visit'], 'visit', 'visits')}, "
            f"{_plural(totals['call'], 'phone tap', 'phone taps')}, {_plural(totals['form'], 'form', 'forms')} "
            f"sent, {_plural(totals['book'], 'booking click', 'booking clicks')}, {online}"
            f"{_plural(totals['mail'], 'e-mail tap', 'e-mail taps')}; {_plural(totals['leads'], 'lead', 'leads')}; "
            f"{_plural(totals['ad_clicks'], 'click', 'clicks')} from your ads."
        )
        return envelope.make({"period": period, "days": days, "totals": totals}, say=say)

    return run("get_page_stats", "read", {"period": period}, body)
