"""The hosting minimum (spec 2026-10-03-admiry-pages-a3p-3-design P12-P20, §3; plan
2026-10-04-agent-a3p-3-1-hosting-minimum-and-muse Q1-Q17): Admiry hosts a site for free while the business's ads
through Admiry run at $200 a month or spend $200 in 30 days. A site that is not covered gets a 30-day countdown; the
owner is told before building (the kit, the homepage card), before publishing (the publish request), at the start, 7
days and 1 day before (e-mails) and when it comes down; it comes down with the ads that point at it paused first; its
files are kept 90 days, and coming back needs ads ready at $200 a month.

The rule (`coverage`, `ready`, the business's calendar `today`), what the publish path calls (`publish_note`,
`precheck_publish`, `on_publish`, `on_unpublish`), what the status views read (`status`, `sentence`, `digest_line`)
and the daily job's transitions (`judge`, `purge`: `jobs.hosting_minimum` selects the rows) live here. The countdown is
stored on the page row (`countdown_ends`, `countdown_mail`); an operator's exemption on the business (P18).

Every sentence an owner reads is the spec's §3.2 sentence, word for word."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from . import ads_run, audit, config, mail
from .channels import registry
from .util import iso

log = logging.getLogger(__name__)

MIN_MONTHLY_USD = 200  # P12: what the business's ads through Admiry run at, or spend in WINDOW_DAYS
WINDOW_DAYS = 30  # P13 (b): the spend window, ending today in the business's calendar
COUNTDOWN_DAYS = 30  # P14
KEEP_DAYS = 90  # P16: a site taken down for the minimum keeps its files this long
REMINDERS = (7, 1)  # P15: the e-mails before the end, in days left
JOB = "hosting-minimum"
BY = f"job:{JOB}"  # who the takedown's pauses are by (`resources.paused_by`)
HOSTED = ("hosted", "admiry")  # the page sources Admiry hosts (the minimum applies to nothing else, P12)

# §3.2, word for word
MINIMUM = (
    "Hosting is free while your ads with Admiry spend at least $200 a month. Without that, the site comes down 30 days "
    "later. We remind you first."
)
START_BY = "Start your ads by {date} to keep the site online."
LAPSED = (
    "Your ads with Admiry spent less than $200 in the last 30 days, so free hosting for {host} ends on {date}. To keep "
    "it: run your ads at $200 a month or more. You can also download the site: {link}. When the site comes down, ads "
    "that point to it pause; point them to another page to keep them running."
)
STAYS_UNTIL = "Your site {host} stays online until {date} unless your ads run at $200 a month or more."
KEPT = "Your ads are back above $200 a month. {host} stays online."
OFFLINE = (
    "Your site {host} is offline. We keep its pages and photos until {date}. To bring it back, get your ads ready at "
    "$200 a month and publish it again."
)
DOWNLOAD = "Download it: {link}."
COMEBACK = (
    "Publishing again needs your ads ready at $200 a month: an approved plan, and funds in your balance or your Google "
    "Ads link."
)
# the subjects of the countdown's e-mails
SUBJECT_START = "Free hosting for {host} ends on {date}"
SUBJECT_REMINDER = {7: "Your site {host} stays online until {date}", 1: "One day left for your site {host}"}
SUBJECT_KEPT = "Your site {host} stays online"
SUBJECT_DOWN = "Your site {host} is offline"
# the audit notes (`job.hosting_minimum`: "{who} {note}"), each in the owner's words
NOTE_START = (
    "started the 30 days before {host} comes down on {date}: your ads with Admiry spent less than $200 in 30 days"
)
NOTE_REMINDER = "reminded you that {host} stays online until {date} unless your ads run at $200 a month or more"
NOTE_KEPT = "kept {host} online: your ads are back above $200 a month"
NOTE_DOWN = "took {host} down after 30 days of ads under $200 a month; its pages and photos are kept until {date}"
NOTE_PURGED = "deleted the pages and photos of {host}, kept 90 days after it came down"
NOTE_EXEMPT = "made hosting free for this business whatever its ads spend ({reason})"
NOTE_UNEXEMPT = (
    "ended the business's free hosting whatever its ads spend: hosting is free while its ads spend $200 a month"
)

_PAGE_COLS = "countdown_ends, countdown_mail, taken_down_reason, taken_down_at, purge_after"


def day_text(day: date) -> str:
    """A date the way the owner reads it: "November 3, 2026"."""
    return f"{day:%B} {day.day}, {day.year}"


def download_url(settings: config.Settings, biz: str) -> str:
    """Where the owner downloads the site (P19): the browser host's own page, behind Google sign-in (`web.py`'s
    `/connect/site`), so no e-mail ever carries a link that hands the site to whoever holds it."""
    return f"{settings.web_base_url}/connect/site?b={biz}"


# ---------------------------------------------------------------- the rule (P13, P17, P18)


@dataclass(frozen=True)
class Coverage:
    """Whether the business's hosted site is covered today (P13), and why: `exempt` (P18), `live_budget` (a), `spend`
    (b) or `short` (not covered). `live_usd` is (a)'s monthly amount, `spent_usd` (b)'s spend, in dollars."""

    covered: bool
    why: str
    live_usd: float
    spent_usd: float


def exemption(conn: psycopg.Connection, biz: str) -> dict | None:
    """The operator's exemption (P18): {"reason", "by", "at"}, or None while the minimum applies."""
    r = conn.execute(
        "SELECT hosting_exempt, hosting_exempt_by, hosting_exempt_at FROM business WHERE id = %s", (biz,)
    ).fetchone()
    if r is None or not r["hosting_exempt"]:
        return None
    return {"reason": r["hosting_exempt"], "by": r["hosting_exempt_by"], "at": r["hosting_exempt_at"]}


def today(conn: psycopg.Connection, biz: str) -> date:
    """Today in the business's own calendar (spec §3.1: the ads' reports' dates): the zone of its first channel row, in
    card order, whose account time zone is known (`resources.time_zone`), else UTC -- read from `ads_run.now()`, the
    clock tests move."""
    for row in ads_run.rows(conn, biz):
        if (row.get("resources") or {}).get("time_zone"):
            return ads_run.account_today(row)
    return ads_run.account_today(None)


def _serving(row: dict) -> bool:
    """A channel row whose ads run now: `live`, or `limited` (the platform serves with a problem) unless the hourly
    read's note says the ads were switched off on the platform itself (the adapter's `paused_elsewhere` /
    `paused_there`)."""
    if row["state"] == "live":
        return True
    if row["state"] != "limited":
        return False
    channel = row["channel"]
    words = registry.adapter(channel).words if registry.known(channel) else None
    switched_off = {w for w in (words.paused_elsewhere, words.paused_there) if w} if words else set()
    return row.get("last_error") not in switched_off


def live_usd(conn: psycopg.Connection, biz: str) -> float:
    """P13 (a): the approved plan's monthly amounts over the channels whose ads run now (`_serving`)."""
    plan = ads_run.approved(conn, biz)
    if plan is None:
        return 0.0
    return float(sum(ads_run.month_amount(plan, r["channel"]) or 0.0 for r in ads_run.rows(conn, biz) if _serving(r)))


def spent_usd(conn: psycopg.Connection, biz: str, day: date) -> float:
    """P13 (b): what the business's ads through Admiry spent over the WINDOW_DAYS days ending `day` -- every channel,
    managed and own account alike (`metric_daily`, each channel's days in its own account's calendar)."""
    r = conn.execute(
        "SELECT COALESCE(sum(cost_micros), 0) AS micros FROM metric_daily WHERE business_id = %s AND day > %s "
        "AND day <= %s",
        (biz, day - timedelta(days=WINDOW_DAYS), day),
    ).fetchone()
    return int(r["micros"]) / 1_000_000


def coverage(conn: psycopg.Connection, biz: str, *, day: date | None = None) -> Coverage:
    """P13: covered when exempt (P18), or (a) the ads run on a monthly budget of $200 or more across channels (a fresh
    launch, or Google spending under the budget, never counts against the owner), or (b) the last 30 days' spend
    reached $200 (a short pause never counts against them either)."""
    day = day or today(conn, biz)
    live = live_usd(conn, biz)
    spent = spent_usd(conn, biz, day)
    if exemption(conn, biz):
        why = "exempt"
    elif live >= MIN_MONTHLY_USD:
        why = "live_budget"
    elif spent >= MIN_MONTHLY_USD:
        why = "spend"
    else:
        why = "short"
    return Coverage(covered=why != "short", why=why, live_usd=round(live, 2), spent_usd=round(spent, 2))


def ready_usd(conn: psycopg.Connection, settings: config.Settings, biz: str) -> float:
    """P17's amount: the approved plan's monthly amounts over the channels whose account can carry ads now
    (`ads_run.account_status`: the owner's own link, or funds in the balance for a channel Admiry runs from it)."""
    plan = ads_run.approved(conn, biz)
    if plan is None:
        return 0.0
    total = 0.0
    for channel in ads_run.funded_channels(plan):
        if registry.known(channel) and ads_run.account_status(conn, settings, biz, channel).ready:
            total += ads_run.month_amount(plan, channel) or 0.0
    return total


def ready(conn: psycopg.Connection, settings: config.Settings, biz: str) -> bool:
    """P17: the business is ready to be covered -- an approved plan whose channels that can run now carry $200 a month
    or more."""
    return ready_usd(conn, settings, biz) >= MIN_MONTHLY_USD


# ---------------------------------------------------------------- the page's countdown (Q4)


def hosting_row(conn: psycopg.Connection, page_id: str) -> dict:
    """The page's countdown and takedown columns (all None for a page that never had either)."""
    r = conn.execute(f"SELECT {_PAGE_COLS} FROM page WHERE id = %s", (page_id,)).fetchone()
    return dict(r) if r else dict.fromkeys(_PAGE_COLS.split(", "))


def _running(h: dict, day: date) -> bool:
    """A countdown is running: its end is still ahead, and the site was not taken down for the minimum since."""
    return h["countdown_ends"] is not None and h["countdown_ends"] > day and h["taken_down_reason"] != "hosting_minimum"


def _hosted(page: dict | None) -> bool:
    return page is not None and page.get("source") in HOSTED


def publish_note(conn: psycopg.Connection, biz: str, page: dict | None) -> str | None:
    """What a publish request says before the owner's yes (P15, §3.2 "Before publishing"; Q9): the minimum, and while
    the site is not covered the date to start the ads by -- the running countdown's end, else the one this publish
    starts (today + 30). None for an exempt business: the minimum is not theirs to read."""
    if exemption(conn, biz):
        return None
    day = today(conn, biz)
    if coverage(conn, biz, day=day).covered:
        return MINIMUM
    h = hosting_row(conn, page["id"]) if page else dict.fromkeys(_PAGE_COLS.split(", "))
    ends = h["countdown_ends"] if _running(h, day) else day + timedelta(days=COUNTDOWN_DAYS)
    return f"{MINIMUM} {START_BY.format(date=day_text(ends))}"


def precheck_publish(conn: psycopg.Connection, settings: config.Settings, biz: str, page: dict | None) -> None:
    """P17 (Q8): publishing a site taken down for the minimum, or one whose countdown has ended (an owner's unpublish
    and republish does not reset the clock), needs the business ready to be covered -- unless it is exempt or covered
    already. ToolError with §3.3's sentence otherwise."""
    if page is None:
        return
    h = hosting_row(conn, page["id"])
    day = today(conn, biz)
    after_takedown = h["taken_down_reason"] == "hosting_minimum"
    ended = h["countdown_ends"] is not None and h["countdown_ends"] <= day
    if not (after_takedown or ended):
        return
    if exemption(conn, biz) or coverage(conn, biz, day=day).covered or ready(conn, settings, biz):
        return
    raise ToolError(COMEBACK)


def on_publish(conn: psycopg.Connection, biz: str, page_id: str) -> date | None:
    """`apply_publish_page`'s write, in its transaction (Q4, Q8): the takedown fields cleared (the site is back), and
    the countdown -- kept while it runs (a republish never resets it), cleared when the business is exempt or covered
    and nothing runs, else started here: today + 30, with no start e-mail (the publish request named the date). Returns
    the countdown's end, or None."""
    h = hosting_row(conn, page_id)
    day = today(conn, biz)
    running = _running(h, day)
    if running:
        ends, sent = h["countdown_ends"], h["countdown_mail"]
    elif exemption(conn, biz) or coverage(conn, biz, day=day).covered:
        ends, sent = None, None
    else:
        ends, sent = day + timedelta(days=COUNTDOWN_DAYS), {"start": "publish"}
    conn.execute(
        "UPDATE page SET countdown_ends = %s, countdown_mail = %s, taken_down_reason = NULL, taken_down_at = NULL, "
        "purge_after = NULL WHERE id = %s",
        (ends, Jsonb(sent) if sent is not None else None, page_id),
    )
    return ends


def on_unpublish(conn: psycopg.Connection, page_id: str) -> None:
    """The owner's own unpublish (`apply_unpublish_page`): why and when it came down. Its files are kept (A3p-2: a
    version published again brings it back), and its countdown stays on the row (Q4): coming back after its end needs
    the ads ready (`precheck_publish`)."""
    conn.execute(
        "UPDATE page SET taken_down_reason = 'owner', taken_down_at = now() WHERE id = %s",
        (page_id,),
    )


# ---------------------------------------------------------------- what the owner reads (Q10, Q17)


def sentence(conn: psycopg.Connection, biz: str, page: dict | None) -> str | None:
    """The countdown's sentence while it runs on a published hosted site (§3.2 "During"), the takedown's while the
    files are kept (§3.2 "Takedown"), else None."""
    if not _hosted(page) or not page.get("hostname"):
        return None
    h = hosting_row(conn, page["id"])
    host = page["hostname"]
    if page["status"] == "published" and h["countdown_ends"] and not exemption(conn, biz):
        return STAYS_UNTIL.format(host=host, date=day_text(h["countdown_ends"]))
    if page["status"] == "unpublished" and h["taken_down_reason"] == "hosting_minimum" and h["purge_after"]:
        return OFFLINE.format(host=host, date=day_text(h["purge_after"]))
    return None


def status(conn: psycopg.Connection, biz: str, page: dict | None) -> dict | None:
    """The `hosting` block of a hosted page's status (spec §7, Q10): whether the site is covered and why, the
    countdown's end, the exemption, why it is down and until when its files are kept, and the owner's sentence (`say`)
    -- None for a page Admiry does not host."""
    if not _hosted(page):
        return None
    h = hosting_row(conn, page["id"])
    cov = coverage(conn, biz)
    down = page["status"] == "unpublished"
    return {
        "minimum_usd": MIN_MONTHLY_USD,
        "covered": cov.covered,
        "why": cov.why,
        "exempt": cov.why == "exempt",
        "countdown_ends": iso(h["countdown_ends"]) if h["countdown_ends"] and not down else None,
        "taken_down": h["taken_down_reason"] if down else None,
        "keep_until": iso(h["purge_after"]) if down and h["purge_after"] else None,
        "say": sentence(conn, biz, page),
    }


def digest_line(conn: psycopg.Connection, biz: str) -> str | None:
    """The daily digest's line every day of a countdown (P15, Q17): the countdown's sentence, else None. The digest
    (Dg) is not built yet; this is what it carries once it is."""
    from . import landing  # landing imports nothing of this module; kept local so this module stays a leaf of it

    page = landing.current(conn, biz)
    if page is None or page["status"] != "published":
        return None
    return sentence(conn, biz, page)


# ---------------------------------------------------------------- the operator's exemption (P18, Q16)


def set_exempt(conn: psycopg.Connection, biz: str, reason: str, *, by: str) -> bool:
    """Exempt the business (P18): the reason, who and when; a countdown running on its page is cleared without an
    e-mail. False when there is no such business."""
    with conn.transaction():
        found = conn.execute(
            "UPDATE business SET hosting_exempt = %s, hosting_exempt_by = %s, hosting_exempt_at = now(), "
            "updated_at = now() WHERE id = %s RETURNING id",
            (reason, by, biz),
        ).fetchone()
        if found is None:
            return False
        conn.execute(
            "UPDATE page SET countdown_ends = NULL, countdown_mail = NULL WHERE business_id = %s "
            "AND countdown_ends IS NOT NULL",
            (biz,),
        )
    return True


def clear_exempt(conn: psycopg.Connection, biz: str) -> bool:
    """End the exemption: the minimum applies again from the next daily run. False when there is no such business."""
    found = conn.execute(
        "UPDATE business SET hosting_exempt = NULL, hosting_exempt_by = NULL, hosting_exempt_at = NULL, "
        "updated_at = now() WHERE id = %s RETURNING id",
        (biz,),
    ).fetchone()
    return found is not None


# ---------------------------------------------------------------- the daily job's transitions (§3.1, Q5-Q7)


def _empty_out() -> dict:
    return {
        "checked": 0,
        "exempt": 0,
        "covered": 0,
        "started": 0,
        "reminded": 0,
        "cleared": 0,
        "taken_down": 0,
        "purged": 0,
        "errors": 0,
        "ops": 0,
        "ops_by_channel": {},
        "mails": {},
    }


def new_run() -> dict:
    """The details of one run of the job (`jobs.hosting_minimum` returns them)."""
    return _empty_out()


def _mailed(out: dict, outcome: str) -> None:
    out["mails"][outcome] = out["mails"].get(outcome, 0) + 1


def _audit(conn: psycopg.Connection, biz: str, page: dict, note: str, **result) -> None:
    audit.record_job(
        conn,
        job=JOB,
        tool="job.hosting_minimum",
        business_id=biz,
        args={"page_id": page["id"]},
        result={"page_id": page["id"], "hostname": page.get("hostname"), "note": note, **result},
    )


def _claim(conn: psycopg.Connection, page_id: str, ends: date, marks: dict, unless: str) -> bool:
    """Record `marks` in the countdown's e-mails while the countdown is still the one judged (`ends`) on a published
    page and `unless` is not recorded yet: True once, for the run that claims it -- the e-mail goes after (at most
    once, whatever runs at the same time)."""
    r = conn.execute(
        "UPDATE page SET countdown_mail = COALESCE(countdown_mail, '{}'::jsonb) || %s WHERE id = %s "
        "AND status = 'published' AND countdown_ends = %s AND NOT (COALESCE(countdown_mail, '{}'::jsonb) ? %s) "
        "RETURNING id",
        (Jsonb(marks), page_id, ends, unless),
    ).fetchone()
    return r is not None


def judge(conn: psycopg.Connection, settings: config.Settings, biz: str, page: dict, out: dict) -> None:
    """§3.1 for one published hosted page: exempt -> nothing (a countdown left from before the exemption is cleared,
    silently); covered -> a countdown is cleared (one e-mail when a countdown e-mail went out); not covered -> the
    countdown starts (its e-mail), reminds at 7 days and 1 day left (each once; a missed day still sends it, never
    both), and at its end the site comes down (`take_down`)."""
    out["checked"] += 1
    h = hosting_row(conn, page["id"])
    host = page["hostname"]
    day = today(conn, biz)
    ends = h["countdown_ends"]
    if exemption(conn, biz):
        out["exempt"] += 1
        if ends is not None:
            conn.execute(
                "UPDATE page SET countdown_ends = NULL, countdown_mail = NULL WHERE id = %s AND countdown_ends = %s",
                (page["id"], ends),
            )
        return
    cov = coverage(conn, biz, day=day)
    sent = h["countdown_mail"] or {}
    if cov.covered:
        out["covered"] += 1
        if ends is None:
            return
        cleared = conn.execute(
            "UPDATE page SET countdown_ends = NULL, countdown_mail = NULL WHERE id = %s AND countdown_ends = %s "
            "RETURNING id",
            (page["id"], ends),
        ).fetchone()
        if cleared is None:
            return
        out["cleared"] += 1
        told = sent.get("start") not in (None, "publish") or any(f"d{n}" in sent for n in REMINDERS)
        email = None
        if told:  # §3.2 "Countdown cleared": only after a countdown e-mail
            email = mail.to_owner(conn, biz, "mail_hosting_kept.txt.j2", SUBJECT_KEPT.format(host=host), host=host)
            _mailed(out, email)
        _audit(conn, biz, page, NOTE_KEPT.format(host=host), why=cov.why, email=email)
        return
    if ends is None:
        ends = day + timedelta(days=COUNTDOWN_DAYS)
        started = conn.execute(
            "UPDATE page SET countdown_ends = %s, countdown_mail = %s WHERE id = %s AND status = 'published' "
            "AND countdown_ends IS NULL RETURNING id",
            (ends, Jsonb({"start": iso(day)}), page["id"]),
        ).fetchone()
        if started is None:
            return
        out["started"] += 1
        when = day_text(ends)
        email = mail.to_owner(
            conn,
            biz,
            "mail_hosting_start.txt.j2",
            SUBJECT_START.format(host=host, date=when),
            text=LAPSED.format(host=host, date=when, link=download_url(settings, biz)),
        )
        _mailed(out, email)
        _audit(conn, biz, page, NOTE_START.format(host=host, date=when), ends=iso(ends), email=email)
        return
    if day >= ends:
        take_down(conn, settings, biz, page, ends, day, out)
        return
    left = (ends - day).days
    due = next((n for n in sorted(REMINDERS) if left <= n and f"d{n}" not in sent), None)
    if due is None:
        return
    # the reminder due now, and any longer one not sent yet (a missed day): the owner gets one e-mail, never two
    marks = {f"d{n}": iso(day) for n in REMINDERS if n >= due and f"d{n}" not in sent}
    if not _claim(conn, page["id"], ends, marks, f"d{due}"):
        return
    out["reminded"] += 1
    when = day_text(ends)
    email = mail.to_owner(
        conn,
        biz,
        "mail_hosting_reminder.txt.j2",
        SUBJECT_REMINDER[due].format(host=host, date=when),
        text=STAYS_UNTIL.format(host=host, date=when),
        link=download_url(settings, biz),
    )
    _mailed(out, email)
    _audit(conn, biz, page, NOTE_REMINDER.format(host=host, date=when), days_left=left, email=email)


def _pause_first(conn: psycopg.Connection, settings: config.Settings, biz: str, host: str, out: dict) -> list[dict]:
    """Q6: every built row whose ads carry the site's address paused by Admiry for the page (`page_down`) before the
    site goes down -- sent under the channel's `live` switch, owed otherwise (ruling S11); beside an owner's pause it
    stacks (M0 Task 9 D5). One channel's failure never holds the others, nor the takedown: page-check's backstop (H43)
    pauses running ads on a gone page within the hour. The rows paused, as they stand after."""
    paused: list[dict] = []
    for row in ads_run.on_host(conn, biz, host):
        channel = row["channel"]
        if row["state"] == "paused" and row["paused_reason"] == "page_down":
            continue
        adapter = registry.adapter(channel)
        try:
            with adapter.client() as http:
                sent = adapter.ops_sent(http)
                ads_run.pause(conn, settings, biz, channel, http=http, by=BY, reason="page_down")
                ops = adapter.ops_sent(http) - sent
        except Exception as e:  # noqa: BLE001 - one channel never stops the takedown; its words may be the platform's
            log.warning("hosting-minimum: the pause on %s failed for business %s: %s", channel, biz, type(e).__name__)
            out["errors"] += 1
            continue
        out["ops"] += ops
        out["ops_by_channel"][channel] = out["ops_by_channel"].get(channel, 0) + ops
        after = ads_run.get(conn, biz, channel)
        if after is not None:
            paused.append(after)
    return paused


def _lift(conn: psycopg.Connection, settings: config.Settings, biz: str, page_id: str, paused: list[dict]) -> None:
    """A takedown that lost the race with a publish (the page is live again): the pause it made for the page is
    lifted, as a publish lifts Admiry's page pause -- never the owner's, and only while the page stays live."""
    live = conn.execute("SELECT status FROM page WHERE id = %s", (page_id,)).fetchone()
    if live is None or live["status"] != "published":
        return
    for row in paused:
        if row.get("state") == "paused" and row.get("paused_reason") == "page_down":
            try:
                ads_run.resume(conn, settings, biz, row["channel"], reason="page_down")
            except ToolError as e:  # busy, or no longer that pause: the publish's own resume, or the next step, has it
                log.info("hosting-minimum: the resume on %s for business %s waits: %s", row["channel"], biz, e)


def take_down(
    conn: psycopg.Connection, settings: config.Settings, biz: str, page: dict, ends: date, day: date, out: dict
) -> bool:
    """§3.3, P16 (Q6): the countdown ended with the business still not covered. Claimed once (`down` in the
    countdown's e-mails), the ads that carry the site's address paused first, then the site down in one transaction --
    A3p-2's unpublish with the reason `hosting_minimum`: the hostname answers 410 and `/pages/ask` stops vouching for
    it -- its files kept until `purge_after` (today + 90), then the owner's e-mail. True when the site came down."""
    host = page["hostname"]
    if not _claim(conn, page["id"], ends, {"down": iso(day)}, "down"):
        return False
    paused = _pause_first(conn, settings, biz, host, out)
    keep = day + timedelta(days=KEEP_DAYS)
    with conn.transaction():
        done = conn.execute(
            "UPDATE page SET status = 'unpublished', taken_down_reason = 'hosting_minimum', taken_down_at = now(), "
            "purge_after = %s, countdown_ends = NULL, countdown_mail = NULL WHERE id = %s AND status = 'published' "
            "AND countdown_ends = %s RETURNING id",
            (keep, page["id"], ends),
        ).fetchone()
        if done is not None:
            ads_run.mark_page_down(conn, biz)
    if done is None:  # published again, or taken down, meanwhile: nothing more of this run's
        log.info("hosting-minimum: business %s's page changed during its takedown; left as it is", biz)
        _lift(conn, settings, biz, page["id"], paused)
        return False
    out["taken_down"] += 1
    when = day_text(keep)
    link = download_url(settings, biz)
    email = mail.to_owner(
        conn,
        biz,
        "mail_hosting_down.txt.j2",
        SUBJECT_DOWN.format(host=host),
        text=f"{OFFLINE.format(host=host, date=when)} {DOWNLOAD.format(link=link)}",
        status=" ".join(ads_run.status_sentence(r) for r in paused),
    )
    _mailed(out, email)
    _audit(
        conn,
        biz,
        page,
        NOTE_DOWN.format(host=host, date=when),
        keep_until=iso(keep),
        paused=[r["channel"] for r in paused],
        email=email,
    )
    log.info("hosting-minimum: business %s's site %s came down (its files kept until %s)", biz, host, keep)
    return True


def purge(conn: psycopg.Connection, biz: str, page_id: str, out: dict) -> bool:
    """P16 (Q7): 90 days after a takedown for the minimum, a site not back up loses its files -- versions, previews,
    photos, upload links, the site documents (`site_spec`: the owner's facts), the owner's own answers kept between
    builds (`site_draft`, A3p-6 batch 2), the HTML and the sections -- and its address (`hostname` NULL: another
    business may take it).
    The row stays with its leads and events, which are the business's records; `taken_down_reason` stays, so coming
    back still needs the ads ready (P17). A row the owner's own page took over since (`source = 'owner'`) keeps its
    address for the ads (`url`), its status and its check: only Admiry's files and hostname go. True when it purged."""
    day = today(conn, biz)
    with conn.transaction():
        page = conn.execute(
            "SELECT id, hostname, source FROM page WHERE id = %s AND taken_down_reason = 'hosting_minimum' "
            "AND purge_after IS NOT NULL AND purge_after <= %s "
            "AND NOT (source IN ('hosted', 'admiry') AND status = 'published') FOR UPDATE",
            (page_id, day),
        ).fetchone()
        if page is None:
            return False
        # a site bundle's files go with its versions and previews (their file rows cascade; the blobs nothing names
        # any more are the hourly collection's), and its upload and zip links and its preview host with them (B13)
        for table in (
            "page_version",
            "page_preview",
            "page_photo",
            "upload_token",
            "site_upload",
            "site_zip",
            "site_spec",
        ):
            conn.execute(f"DELETE FROM {table} WHERE page_id = %s", (page_id,))
        if page["source"] in HOSTED:
            conn.execute(
                "UPDATE page SET html = NULL, sections = NULL, hostname = NULL, url = '', check_json = NULL, "
                "checked_at = NULL, purge_after = NULL, preview_host = NULL, site_spec_id = NULL, site_draft = NULL "
                "WHERE id = %s",
                (page_id,),
            )
        else:
            conn.execute(
                "UPDATE page SET html = NULL, sections = NULL, hostname = NULL, purge_after = NULL, "
                "preview_host = NULL, site_spec_id = NULL, site_draft = NULL WHERE id = %s",
                (page_id,),
            )
        _audit(conn, biz, dict(page), NOTE_PURGED.format(host=page["hostname"]))
    out["purged"] += 1
    return True
