"""The Analyst's daily look as an owner's assistant reads it (capability layer K2, spec 2026-10-07 §5.1 step 5; plan
2026-10-07-agent-k2 R2, R10, Task 14): what yesterday's figures mean and what Admiry suggests, each suggestion with
the action that does it (a public tool and its arguments, chosen by code), beside the report e-mailed that morning.
Reading applies nothing: the assistant runs an action only after the owner says yes, and the action asks for the
owner's approval itself where money is involved (founder 2026-10-07: no automatic actions). A platform key also reads
every stored look (`team`)."""

from __future__ import annotations

from datetime import date

from fastmcp.exceptions import ToolError

from . import config, digest, envelope
from .mcp_app import agent_tool, need_business, run
from .roles import analyst

KEYS = ("insight", "report", "recent", "frequency", "next_report_at")  # the public contract's result keys
RECENT = 14
RUNNING = ("live", "launching", "limited")
BAD_DAY = "day must look like 2026-10-05"
NOT_ON = "Admiry's daily look at your results is not on for this business yet."
NO_ADS = "No look at your results yet: none of your ads are running."
NOT_THAT_DAY = "Admiry has no look at your results for that day."
FIRST = "Admiry's first look at your results comes the morning after your ads first run."
SUGGESTS = "Admiry looked at {weekday}'s results and has {n} suggestion{s}."


def _day(day: str | None) -> date | None:
    if day is None:
        return None
    try:
        return date.fromisoformat(day)
    except ValueError as e:
        raise ToolError(BAD_DAY) from e


def _say(conn, settings, biz: str, view: dict | None, wanted: date | None) -> str:
    """The look's own words when there is one an owner may read; else why there is none, the first that holds."""
    if view is not None:
        if view["summary"]:
            return view["summary"]
        if view["findings"]:
            return view["findings"][0]
        n = len(view["recommendations"])
        weekday = date.fromisoformat(view["day"]).strftime("%A")
        return SUGGESTS.format(weekday=weekday, n=n, s="" if n == 1 else "s")
    if settings.roles_mode != "on":
        return NOT_ON
    running = conn.execute(
        "SELECT 1 FROM channel_campaign WHERE business_id = %s AND state = ANY(%s) LIMIT 1", (biz, list(RUNNING))
    ).fetchone()
    if running is None:
        return NO_ADS
    return NOT_THAT_DAY if wanted else FIRST


@agent_tool(cls="RO", scope="read")
def insights(day: str | None = None) -> dict:
    """Admiry's daily look at the advertising's results -- the latest, or one day's (day, 2026-10-05) -- with its
    suggestions and their actions, the report e-mailed that morning, the days before, and how often the owner gets the
    report. Applies nothing."""

    def body(conn, p):
        biz = need_business(p)
        settings = config.load()
        wanted = _day(day)
        rows = conn.execute(
            "SELECT * FROM insight WHERE business_id = %s AND status = 'done' AND (%s::date IS NULL OR day = %s) "
            "ORDER BY day DESC LIMIT %s",
            (biz, wanted, wanted, RECENT),
        ).fetchall()
        views = [v for v in (analyst.owner_view(settings, r) for r in rows) if v is not None]
        latest = views[0] if views else None
        on_day = wanted or (date.fromisoformat(latest["day"]) if latest else None)
        sent = digest.latest_sent(conn, biz, day=on_day)
        frequency = conn.execute("SELECT digest_frequency FROM business WHERE id = %s", (biz,)).fetchone()
        next_at = digest.next_report_at(conn, settings, biz)
        data = {
            "insight": latest,
            "report": sent["body"] if sent else None,
            "recent": [{"day": v["day"], "summary": v["summary"]} for v in views[1:]],
            "frequency": frequency["digest_frequency"],
            "next_report_at": next_at.isoformat() if next_at else None,
        }
        if p.key.cls == "platform":  # Admiry's team: every stored look, whatever an owner may read (not in KEYS)
            team = conn.execute(
                "SELECT * FROM insight WHERE business_id = %s ORDER BY day DESC LIMIT %s", (biz, RECENT)
            ).fetchall()
            data["team"] = [analyst.team_view(r) for r in team]
        acts = [
            {"id": r["id"], "label": r["action"]["label"], "tool": r["action"]["tool"], "args": r["action"]["args"]}
            for r in (latest or {}).get("recommendations", [])
        ]
        nxt = envelope.actions(*acts) if acts else envelope.NONE
        return envelope.make(data, say=_say(conn, settings, biz, latest, wanted), next=nxt)

    return run("insights", "read", {"day": day}, body)
