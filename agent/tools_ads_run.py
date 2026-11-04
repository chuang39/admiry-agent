"""The owner-facing tools over the ads lifecycle that stay here (spec 2026-09-28 admiry-google-search-execution-design.md
§6; A3 Task 5; spec 2026-09-29-admiry-channels-design §7.1): get_report (RO, database only) reads Google Search's
`channel_campaign` row and its `metric_daily` days, and every channel's statement for period="month"; pause and resume
(W) take a channel, or "all" (M0 Task 4: `ads_run.pause_channels` / `resume_channels`, each platform call in `prepare`
-- never inside `run()`'s own transaction). The assistant asks which channel when several run (the assistant skill's
references/after-launch.md). The reading tools over every channel -- get_channel_status, get_status_issues -- moved to
tools_channels.py (M0 Task 6).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import ads_run, config, envelope, google_ads_link, plans
from .mcp_app import agent_tool, need_business, run

GOOGLE = "google_search"  # the channel get_report's day-by-day view reads
ALL = "all"
PERIOD_DAYS = {"7d": 7, "30d": 30}
BAD_PERIOD = "period must be one of 7d, 30d, month"
NO_LINK = "No Google Ads account is linked yet."
NOT_LAUNCHED = "Your ads have not launched yet."


def _outcome_data(out: dict) -> dict:
    """`pause_channels`/`resume_channels`'s answer as the tool's data: each channel's outcome without its "say" (the
    envelope's `say` carries the sentences; keeping them twice invites the texts drifting apart), and `state`, the
    one every channel it touched is in now (None when they differ)."""
    channels = {ch: {k: v for k, v in o.items() if k != "say"} for ch, o in out["channels"].items()}
    states = {o["state"] for o in channels.values()}
    return {"state": states.pop() if len(states) == 1 else None, "channels": channels}


@agent_tool(cls="RO", scope="read")
def get_report(period: str = "7d") -> dict:
    """Day-by-day spend, clicks, impressions, calls and conversions for the last 7 or 30 days with the plan's
    own planning estimate for the same span beside them, or -- period="month" -- the current and previous
    month's totals with the Customer ID and Google's no-fee notice (the monthly statement). period is one of
    7d, 30d, month."""

    def body(conn, p):
        biz = need_business(p)
        if period not in ("7d", "30d", "month"):
            raise ToolError(BAD_PERIOD)
        row = ads_run.get(conn, biz, GOOGLE)
        if period == "month":
            month_statement = ads_run.statement(conn, biz)  # one entry per channel with a row
            if month_statement is None:  # no ads row: an account linked or not
                link = google_ads_link.get(conn, biz)
                say = NOT_LAUNCHED if link is not None and link.live else NO_LINK
                return envelope.make({"period": period, "statement": None}, say=say)
            # every channel's spend, in card order: A3's sentence for the first, then ", $120 on NewsBreak" for each other
            spent = [(e["label"], plans.usd(e["months"][0]["spent_usd"])) for e in month_statement["channels"]]
            (label, amount), *rest = spent
            say = (
                f"{amount} spent so far this month on {label}{''.join(f', {a} on {lb}' for lb, a in rest)}. "
                f"{month_statement['no_fee_notice']}."
            )
            return envelope.make({"period": period, "statement": month_statement}, say=say)
        n = PERIOD_DAYS[period]
        days = ads_run.day_rows(conn, biz, GOOGLE, n, row)
        day_totals = ads_run.totals(days)
        plan = plans.get(conn, biz, row["plan_id"]) if row else None
        planning = ads_run.scaled_planning(plan, n) if plan else None
        say = (
            f"Over the last {n} days: {plans.usd(day_totals['spent_usd'])} spent, {day_totals['clicks']} clicks, "
            f"{day_totals['calls']} calls."
        )
        return envelope.make({"period": period, "days": days, "totals": day_totals, "planning": planning}, say=say)

    return run("get_report", "read", {"period": period}, body)


@agent_tool(cls="W", scope="manage")
def pause(idempotency_key: str, channel: str = ALL) -> dict:
    """Stop spending on this business's ads right away: on one channel (google_search, newsbreak, ...) or on
    every one ("all", the default). Always allowed -- stopping spend needs no approval and never counts against the
    hourly change limit -- and resume starts them again later. When the business runs ads on more than one channel,
    ask the owner which, or all of them. Use a new idempotency_key per intended pause."""

    def prepare(conn, p):
        return ads_run.pause_by_owner(conn, config.load(), need_business(p), channel, by="assistant")

    def body(conn, p, ctx):
        return envelope.make(_outcome_data(ctx), say=ctx["say"])

    args = {"channel": channel, "idempotency_key": idempotency_key}
    return run("pause", "manage", args, body, idempotency_key=idempotency_key, prepare=prepare)


@agent_tool(cls="W", scope="manage", approval="assistant")
def resume(idempotency_key: str, channel: str = ALL) -> dict:
    """Start spending on this business's ads again after the owner paused them: on one channel (google_search,
    newsbreak, ...) or on every one ("all", the default). A pause Admiry made itself (the month's budget spent, no
    funds) is lifted by Admiry, not here. IMPORTANT ACTION: confirm with the owner before calling, since it starts
    spend again. Use a new idempotency_key per intended resume."""

    def prepare(conn, p):
        return ads_run.resume_by_owner(conn, config.load(), need_business(p), channel, by="assistant")

    def body(conn, p, ctx):
        return envelope.make(_outcome_data(ctx), say=ctx["say"])

    args = {"channel": channel, "idempotency_key": idempotency_key}
    return run("resume", "manage", args, body, idempotency_key=idempotency_key, prepare=prepare)
