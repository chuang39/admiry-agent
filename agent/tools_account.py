"""get_audit_log (spec §4.0): the owner sees plain sentences; a platform key bound to the business
also sees the tool rows."""

from __future__ import annotations

from datetime import date

from . import envelope, wallet
from .channels import registry
from .mcp_app import agent_tool, need_business, run
from .util import iso

WHO = {
    "muse": "Muse",
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "web": "The owner (web)",
    "draft": "The setup session",
    "ops": "An operator",
}
VERBS = {  # tool -> (past tense with placeholders, infinitive for failures)
    "start": ("{who} started this business's setup", "start the setup"),
    "describe_business": ("{who} described the business (knowledge v{version})", "describe the business"),
    "update_business": ("{who} updated the business details ({updated})", "update the business details"),
    "set_goal": ("{who} set the goal", "set the goal"),
    "change_goal": ("{who} changed the goal", "change the goal"),
    "create_plan": ("{who} created plan v{version}", "create a plan"),
    "revise_plan": ("{who} revised the plan into v{version}", "revise the plan"),
    "approve_plan": ("{who} asked to launch a plan (proposal {proposal_id})", "ask to launch a plan"),
    "confirm_proposal": ("{who} confirmed proposal {proposal_id}", "confirm a proposal"),
    "decline_proposal": ("{who} declined proposal {proposal_id}", "decline a proposal"),
    "revoke_key": ("{who} revoked a key", "revoke a key"),
    "update_lead": ("{who} marked a lead {status}", "mark a lead"),
    "add_lead": ("{who} recorded a lead the owner reported ({status})", "record a lead"),
    "set_landing_page": ("{who} set the landing page", "set the landing page"),
    "preview_page": ("{who} made a preview of a page", "make a preview of a page"),
    "publish_page": ("{who} asked to publish the page (proposal {proposal_id})", "ask to publish the page"),
    "unpublish_page": ("{who} asked to take the page down (proposal {proposal_id})", "ask to take the page down"),
    "create_page": ("{who} had Admiry write a page for the ads", "have Admiry write a page for the ads"),
    "update_page": ("{who} changed part of the page Admiry wrote", "change part of the page Admiry wrote"),
    "revise_page": ("{who} had Admiry rewrite the page", "have Admiry rewrite the page"),
    "get_page_source": ("{who} read the source of the page Admiry hosts", "read the page's source"),  # A3p-3
    "request_site_upload": ("{who} asked for a link to upload a site", "ask for a link to upload a site"),  # A3p-3
    # A3p-5 batch 2 (plan Q7, Q8): two of the four ways to Admiry's code on the owner's own website
    "send_install_steps": (
        "{who} e-mailed the steps for Admiry's code to the owner's web person",
        "e-mail the steps for Admiry's code to the web person",
    ),
    "request_install_help": (
        "{who} asked Admiry's team to put Admiry's code on the website",
        "ask Admiry's team to put Admiry's code on the website",
    ),
    # A3p-6 batch 2 (plan Q6, Q10): the industry site Admiry builds -- a call answers a question or builds it
    "build_site": ("{who} asked Admiry to build the site", "build the site"),
    "get_site": ("{who} looked at the site Admiry builds", "look at the site Admiry builds"),
    "get_site_templates": ("{who} looked at the looks a site can have", "look at the looks a site can have"),
    "answer": ("{who} answered the consultation questions", "answer the consultation questions"),
    "recommend": ("{who} built plan v{version} from the consultation", "build a plan from the consultation"),
    "adjust": ("{who} adjusted the plan into v{version}", "adjust the plan"),
    "change_plan": ("{who} changed the plan into v{version} from the owner's words", "change the plan"),
    "ask": ("{who} asked Admiry a question about the plan", "ask Admiry a question"),
    "ops.verify": ("{who} verified the business", "verify the business"),
    "ops.issue-code": ("{who} issued an approval code for proposal {proposal_id}", "issue an approval code"),
    # read-only tools
    "whoami": ("{who} checked its key", "check its key"),
    "list_keys": ("{who} listed the keys", "list the keys"),
    "get_business": ("{who} looked at the business details", "look at the business details"),
    "business_profile": ("{who} looked at the business profile", "look at the business profile"),
    "performance": ("{who} looked at how the advertising is doing", "look at how the advertising is doing"),
    "marketing_state": ("{who} looked at where the business stands", "look at where the business stands"),
    "get_plan": ("{who} looked at the plan", "look at the plan"),
    "get_page_kit": ("{who} got the kit to build the ads page", "get the kit to build the ads page"),
    "check_landing_page": ("{who} checked the landing page", "check the landing page"),
    "get_page": ("{who} looked at the landing page", "look at the landing page"),
    "get_page_stats": ("{who} looked at how the page is doing", "look at how the page is doing"),
    "list_leads": ("{who} looked at the leads", "look at the leads"),
    "list_proposals": ("{who} looked at the proposals", "look at the proposals"),
    "get_proposal": ("{who} looked at the proposals", "look at the proposals"),
    "get_audit_log": ("{who} looked at this log", "look at this log"),
    "request_connect_link": ("{who} asked for the owner's sign-in link", "ask for the owner's sign-in link"),
    "web.sign_in": ("{who} signed in", "sign in"),
    "web.claim": ("{who} claimed the business", "claim the business"),
    "web.key_create": ("{who} created a key for {label}", "create a key"),
    "web.key_revoke": ("{who} revoked a key", "revoke a key"),
    "web.code_issue": ("{who} showed the approval code for proposal {proposal_id}", "show an approval code"),
    "web.approve_plan": ("{who} approved and launched a plan (proposal {proposal_id})", "approve a plan"),
    # S0 and capability layer K1 (final review I3): the second look -- never its grades or its score -- and the phone
    # the launch asks for, saved on the setup card
    "web.review_plan": ("{who} asked Admiry for a second look at the plan", "ask for a second look at the plan"),
    "job.critic": ("{who} took a second look at the plan", "take a second look at the plan"),
    # capability layer K2: the Analyst's daily look (its rows carry no business, so no owner reads this one)
    "job.analyst": ("{who} looked at how the ads did", "look at how the ads did"),
    # the daily digest's controls (plan 2026-10-07-agent-k2 Task 12)
    "web.digest_frequency": ("{who} set the e-mail report to {note}", "change the e-mail report"),
    "web.email_status": ("{who} asked Admiry to send e-mails to the address again", "resume e-mails"),
    "system.digest_unsubscribe": ("Reports were changed to {note} from the e-mail's link", "change the reports"),
    # the operator's commands (Task 16): only email_status is on the business; the others' rows carry none
    "ops.email_status": (
        "{who} let Admiry send e-mails to the address again",
        "let Admiry send e-mails to the address again",
    ),
    "ops.digest": ("{who} set the report's switch to {note}", "set the report's switch"),
    "ops.digest_seed": ("{who} added a day of test numbers", "add a day of test numbers"),
    "ops.insight": ("{who} read the daily look", "read the daily look"),
    "ops.insight_rebuild": ("{who} built the daily look again", "build the daily look again"),
    "web.eval_grade": ("{who} graded a plan of Admiry's golden set", "grade a plan of Admiry's golden set"),
    "ops.eval_case": ("{who} made a golden-set case from a business", "make a golden-set case from a business"),
    # capability layer K4: the Researcher's model calls (its rows carry no business, so no owner reads this one)
    "job.researcher": ("{who} looked into the market", "look into the market"),
    "ops.research": ("{who} asked Admiry to look into the market", "ask Admiry to look into the market"),
    "ops.research_show": ("{who} read Admiry's research", "read Admiry's research"),
    # the Researcher's work, asked for and read by the owner's assistant (capability layer K4)
    "request_research": ("{who} asked Admiry to research the market", "ask Admiry to research the market"),
    "get_research": ("{who} read Admiry's research on the market", "read Admiry's research"),
    # the Analyst's daily look, read by the owner's assistant (Task 14)
    "insights": ("{who} read Admiry's daily look at the results", "read the daily look"),
    # Resend's events (Task 13)
    "system.email_bounced": (
        "E-mails to your address bounced; lead alerts and reports are paused",
        "record the bounce",
    ),
    "system.digest_complaint": (
        "Admiry turned the reports off after the e-mail was marked as spam",
        "turn the reports off",
    ),
    "web.fact": ("{who} gave the business phone for the ads", "give the business phone for the ads"),
    # A3p-2 Task 7: the homepage page card's Publish button (its own proposal, confirmed as the web owner)
    "web.publish_page": ("{who} published the page (proposal {proposal_id})", "publish the page"),
    "web.agree": (
        "{who} accepted the Google Ads Management Agreement ({version})",
        "accept the Google Ads Management Agreement",
    ),
    "web.ads_link_invite": ("{who} asked to link Google Ads account {customer_id}", "link a Google Ads account"),
    "web.ads_link_check": ("{who} checked the Google Ads link: {status}", "check the Google Ads link"),
    "web.ads_link_active": (
        "{who} confirmed Google Ads account {customer_id} is linked",
        "confirm the Google Ads link",
    ),
    "job.ads_link_active": (
        "{who} confirmed Google Ads account {customer_id} is linked",
        "confirm the Google Ads link",
    ),
    "connect_google_ads": ("{who} asked to link Google Ads account {customer_id}", "link a Google Ads account"),
    # M0 Task 6: the channel tools (tools_channels.py)
    "connect_channel": ("{who} asked to connect {label}", "connect a channel"),
    "get_connection_status": ("{who} looked at how each channel is connected", "look at how each channel is connected"),
    "get_platform_statement": ("{who} pulled the monthly statement", "pull the monthly statement"),
    "ops.ads_link": ("{who} set the Google Ads link for account {customer_id} to {status}", "set the Google Ads link"),
    "web.ads_mode": ("{who} chose how Admiry runs the ads: {mode}", "choose how Admiry runs the ads"),
    "web.agree_prepayment": ("{who} accepted the prepayment terms ({version})", "accept the prepayment terms"),
    "get_balance": ("{who} looked at the balance", "look at the balance"),
    "list_payments": ("{who} looked at the payments", "look at the payments"),
    "request_payment_link": ("{who} asked for the add-funds link", "ask for the add-funds link"),
    "ops.refund": ("{who} refunded {amount} to the original payment", "refund to the original payment"),
    "ops.adjust": ("{who} corrected the balance by {amount} ({note})", "correct the balance"),
    "web.pay_start": ("{who} started a payment of {amount}", "start a payment"),
    "web.pay_stopped": (
        "{who} started a payment of {amount}, but the business no longer uses prepaid ads: nothing was charged",
        "start a payment",
    ),
    "web.topup": ("{who} confirmed a payment: {amount} added to the balance", "add funds"),
    "job.spend_guard": ("{who} paused your ads: balance is $0. Add funds to resume.", "pause the ads"),
    # A3c-1: the Google Ads account a prepaid business's Google Search runs in -- one of Admiry's, never named here
    "job.google_account": (
        "{who} chose the Admiry Google Ads account your Google Search ads run in",
        "choose the Google Ads account",
    ),
    "ops.google_account_bind": (
        "{who} chose the Admiry Google Ads account your Google Search ads run in",
        "choose the Google Ads account",
    ),
    "ops.google_account_release": (
        "{who} released the Admiry Google Ads account your Google Search ads ran in",
        "release the Google Ads account",
    ),
    # M0 Task 7: one day's spend of a channel Admiry runs from the balance, taken from it (wallet-settle)
    "job.wallet_settle": ("{who} charged your balance {cost} for {channel} ads on {day}", "charge the balance"),
    # A3 Task 4: every lifecycle transition of the Google Search ads; `note` is the owner phrase ads_run wrote.
    "job.ads_launch": ("{who} {note}", "launch the ads"),
    # M1 Task 5b: what the hourly read of NewsBreak changed there itself -- the month's spending cap, ads on their way
    # out turned off (`note`: which, in the owner's words), ads NewsBreak switched off at its cap turned back on (one
    # line per event, its `note` naming what went through: M1 Task 7)
    "job.ads_sync_cap": ("{who} let {channel} spend up to {cost} this month", "set this month's spending limit"),
    "job.ads_sync_off": ("{who} {note}", "turn off ads on their way out"),
    "job.ads_sync_on": ("{who} {note}", "turn the ads back on"),
    # M1 Task 6: the hourly conversion upload -- which leads a channel's ads brought, told back to it by click id
    # (`note`: how many, in the owner's words); and the two NewsBreak operator commands
    "job.conversion_upload": ("{who} {note}", "tell the channel which leads came from its ads"),
    "ops.nb_reconcile": (
        "{who} checked your {channel} spend for {day} against {channel}'s own figures",
        "check the spend against the channel's own figures",
    ),
    "ops.nb_account_ready": ("{who} marked your {channel} account ready to run your ads", "mark the account ready"),
    # A3p-2 Task 4: the hourly page-check pausing the ads while their page is down, and resuming its own pause.
    "job.page_check": ("{who} {note}", "check the page"),
    "job.hosting_minimum": ("{who} {note}", "keep the site online"),  # A3p-3: the countdown, the takedown, the purge
    "ops.hosting_exempt": ("{who} {note}", "change whether hosting stays free"),  # A3p-3 P18
    # A3 Task 5: the five owner-facing ads tools, and /connect's own Pause/Resume buttons.
    "get_channel_status": ("{who} checked how the ads are doing", "check how the ads are doing"),
    "get_report": ("{who} pulled a report on the ads", "pull a report on the ads"),
    "get_status_issues": ("{who} checked for issues with the ads", "check for issues with the ads"),
    "pause": ("{who} paused the ads", "pause the ads"),
    "resume": ("{who} resumed the ads", "resume the ads"),
    "web.ads_pause": ("{who} paused the ads", "pause the ads"),
    "web.ads_resume": ("{who} resumed the ads", "resume the ads"),
    # SP1 (spec 2026-09-30 support-and-feedback): support requests and plan feedback
    "contact_support": ("{who} asked Admiry support for help", "ask Admiry support for help"),
    "web.support": ("{who} asked Admiry support for help", "ask Admiry support for help"),
    "web.help": ("{who} asked Admiry support for help", "ask Admiry support for help"),  # the Help button (2026-10-12)
    "web.feedback": ("{who} sent feedback on a plan", "send feedback on a plan"),
    # walk fixes 1, Task 4 round 1: an address put on the map (geocode.place), one row per Google Places call
    "places.lookup": ("{who} looked up a place on the map", "look up a place on the map"),
    # the owner's own word on the business's Google Maps listing, from Settings (plan 2026-10-03-agent-listing-confirm,
    # Task 6); a "none" among the choices reads as VARIANTS says
    "web.listing_unlink": (
        "{who} unlinked the business's Google Maps listing",
        "unlink the business's Google Maps listing",
    ),
    "web.listing_find": ("{who} looked the business up on Google Maps", "look the business up on Google Maps"),
    "web.listing_choose": (
        "{who} linked the business's Google Maps listing",
        "link the business's Google Maps listing",
    ),
}

# A tool whose rows say different things by what they did (M0 Task 7): (tool, a key of the result, its value) -> the
# past tense for such a row, in VERBS' placeholders
VARIANTS = {
    ("job.spend_guard", "paused_reason", "month_cap"): (
        "{who} paused {channel}: this month's amount is spent. It starts again on the 1st."
    ),
    # A3c-1: the funds stop counts the spend not charged to the balance yet, so the balance itself may still show money
    ("job.spend_guard", "funds_stop", "business"): (
        "{who} paused your ads: recent ad spend, not yet taken from your balance, uses it up. Add funds to resume."
    ),
    ("job.spend_guard", "funds_stop", "channel"): "{who} paused {channel} until you add funds.",
    # the owner's words that said the Google Maps place linked is not theirs unlinked it inside the command (plan
    # 2026-10-03-agent-listing-not-mine-words, D5); for change_plan, ahead of its "not a change" reading of the same row
    ("answer", "listing", "unlink"): "{who} unlinked the business's Google Maps listing",
    ("change_plan", "listing", "unlink"): "{who} unlinked the business's Google Maps listing",
    # G1 Task 7: change_plan's words that were not a change (a ticket took them, or they named nothing) made no version
    ("change_plan", "changed", False): (
        "{who} sent the owner's words; they were not a change to the plan, and it did not change"
    ),
    # the founder's ruling (2026-10-13): asked to connect with no way chosen, nothing connects -- the owner is asked
    ("connect_channel", "status", "choose"): "{who} asked how you want your ads to run",
    # the same day's ruling: the page comes before the money -- asked to connect with no page settled, the page is asked
    ("connect_channel", "status", "page_first"): "{who} asked how you want the page for your ads",
    # M1 Task 6: nb-account-ready's spend check -- the figures already in order, only the mail mark dropped (fix round
    # 1, item 4: nothing of the owner's was held); NewsBreak's total recorded for the hourly read's own stop of a new
    # month, so the month's cap goes in (M1 Task 7 fix round 1, M4: no change of the owner's waited); or recorded, so a
    # held change goes out. The first that matches is said: the order matters
    ("ops.nb_account_ready", "recorded", False): "{who} checked {channel}'s spend figures and found them in order",
    ("ops.nb_account_ready", "held", "month"): (
        "{who} checked {channel}'s spend figures, so this month's budget there is set within the hour"
    ),
    ("ops.nb_account_ready", "recorded", True): (
        "{who} checked {channel}'s spend figures, so your latest change there goes out"
    ),
    # the owner's word on the business's Google Maps listing, which update_business answers with `listing` (plan
    # 2026-10-03-agent-listing-confirm, Task 5 round 1): such an answer has no `updated` list to name
    ("update_business", "listing", "unlink"): "{who} unlinked the business's Google Maps listing",
    ("update_business", "listing", "find"): "{who} looked the business up on Google Maps",
    ("update_business", "listing", "choose"): "{who} linked the business's Google Maps listing",
    ("update_business", "listing", "none"): "{who} said none of the places found on Google Maps is the business",
    # ... and Settings' "None of these" (web.listing_choose answers {"none": True, ...}; a place linked has no `none`)
    ("web.listing_choose", "none", True): "{who} said none of the places found on Google Maps is the business",
}
VERIFIES = ("web.ads_link_active", "job.ads_link_active", "ops.ads_link", "web.topup")  # may carry verified_now
MODE_TEXT = {
    "own_account": "in the owner's own Google Ads account",
    "managed": "prepaid, in a Google Ads account Admiry runs",
}


def _who(row: dict, label: str | None) -> str:
    """The sentence's subject: the key's assistant, else by actor — a visitor, the owner on the web, Stripe, Admiry
    (a scheduled job, or a place it looked up on the map), an operator, an assistant."""
    if label in WHO:
        return WHO[label]
    actor = row["actor"]
    if actor.startswith("ip:"):
        return "A visitor"
    if actor.startswith("web:"):
        return "The owner (web)"
    if actor == "stripe":
        return "Stripe"
    if actor == "unsubscribe link":  # the daily digest's link (Task 12)
        return "The e-mail's link"
    if actor == "resend":  # Resend's bounce and complaint events (Task 13)
        return "Resend"
    if actor.startswith("job:") or actor == "places":
        return "Admiry"
    return "An operator" if row["actor_class"] == "platform" else "An assistant"


def _amount(value) -> str:
    """A result's amount_usd as owner text ("$500.00", "-$25.50")."""
    return wallet.usd(round(value * 100)) if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _cents(value) -> str:
    """A result's cents as owner text ("$12.34")."""
    return wallet.usd(value) if isinstance(value, int) and not isinstance(value, bool) else "?"


def _day(value) -> str:
    """An ISO day as owner text ("Sep 29")."""
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return "?"
    return f"{day:%b} {day.day}"


def sentence(row: dict, label: str | None) -> str:
    who = _who(row, label)
    result = row["result"] if isinstance(row["result"], dict) else {}
    past, infinitive = VERBS.get(row["tool"], (f"{{who}} used {row['tool']}", f"use {row['tool']}"))
    if not row["ok"]:
        return f"{who} tried to {infinitive}, but: {result.get('error', 'it failed')}"
    if "replay_of" in result:
        return f"{who} repeated an earlier request (no change)"
    # A successful call made through an agent tool (run()/run_anonymous()) is stored as its
    # {data, say, next} envelope; web./job./ops./system rows never go through run() and stay plain.
    data = result.get("data", result) if envelope.is_envelope(result) else result
    if row["tool"] == "approve_plan" and data.get("status") == "needs_verification":
        return f"{who} asked to launch a plan, but the business is not verified yet"
    if row["tool"] in ("connect_google_ads", "connect_channel") and data.get("status") == "needs_agreement":
        return f"{who} asked to link a Google Ads account, but the owner has not accepted the management agreement yet"
    if row["tool"] == "connect_channel" and data.get("status") == "pending":
        return f"{who} asked to link Google Ads account {data.get('customer_id', '?')}"
    past = next((v for (tool, k, value), v in VARIANTS.items() if tool == row["tool"] and data.get(k) == value), past)
    given = {**(row.get("args_redacted") or {}), **data}  # a job's row may name what it acted on in its args
    channel = given.get("channel")
    fields = {
        "who": who,
        "version": data.get("version", "?"),
        "updated": ", ".join(data.get("updated", [])),
        "proposal_id": data.get("proposal_id", "?"),
        "label": data.get("label", "?"),
        "customer_id": data.get("customer_id", "?"),
        "status": data.get("status", "?"),
        "mode": MODE_TEXT.get(data.get("ads_mode"), "?"),
        "amount": _amount(data.get("amount_usd")),
        "note": data.get("note") or "",
        "channel": registry.label(channel) if isinstance(channel, str) and channel else "?",  # D76: the owner's name
        "cost": _cents(data.get("cost_cents")),
        "day": _day(given.get("day")),
    }
    text = past.format(**fields)
    if row["tool"] in VERIFIES and data.get("verified_now"):
        text += "; the business is now verified"
    return text


@agent_tool(cls="RO", scope="read")
def get_audit_log(cursor: int | None = None, limit: int = 50) -> dict:
    """What happened on this business, newest first, as plain sentences ("Muse created plan v2",
    "The owner confirmed proposal …"). Paginate with next_cursor; limit at most 100. Use it to
    answer "what did you change?" — a platform key bound to the business also sees the tool rows."""

    def body(conn, p):
        biz = need_business(p)
        n = max(1, min(int(limit), 100))
        rows = conn.execute(
            "SELECT a.id, a.ts, a.actor, a.actor_class, a.tool, a.args_redacted, a.result, a.ok, k.label "
            "FROM agent_action a LEFT JOIN api_key k ON k.id = a.api_key_id "
            "WHERE a.business_id = %s AND (%s::bigint IS NULL OR a.id < %s) ORDER BY a.id DESC LIMIT %s",
            (biz, cursor, cursor, n + 1),
        ).fetchall()
        more = len(rows) > n
        entries = []
        for r in rows[:n]:
            e = {"id": r["id"], "ts": iso(r["ts"]), "sentence": sentence(r, r["label"])}
            if p.key.cls == "platform":
                e["detail"] = {"tool": r["tool"], "actor": r["actor"], "args": r["args_redacted"], "ok": r["ok"]}
            entries.append(e)
        return {"entries": entries, "next_cursor": entries[-1]["id"] if more and entries else None}

    return run("get_audit_log", "read", {"cursor": cursor, "limit": limit}, body)
