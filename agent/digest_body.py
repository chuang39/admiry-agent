"""The digest's stored body and its owner sentences (digest spec 2026-09-30 §4.1, §4.2, §4.4; plan
2026-10-07-agent-k2 Task 9, Dg Task 5). The body is what every surface reads -- the mail, get_insights' `report`, the
homepage's `?report=`: the numbers' sentence, a row per channel shown, the "needs you" items, the Monday week, and
Admiry's take and suggestions (the Analyst's look, `with_insight`). Aggregates only: never a lead's fields, a platform
code, a token or an address."""

from __future__ import annotations

from datetime import datetime

from . import mail, plans
from .digest_numbers import Numbers, usd
from .digest_rules import Item

NOT_FINAL = "Yesterday's numbers aren't all in yet; here is what we have."
FOOTER_DAILY = "You get this every morning while your ads run."
FOOTER_WEEKLY = "You get this every Monday."
ASK_ADMIRY = "To ask about these numbers, open Admiry."
REPLY_INVITE = "Reply to this e-mail to ask about these numbers."
NEEDS_YOU = "Needs you: "
YOUR_PAGE = "Your page"
MAX_SUBJECT = 78
STATUS_ORDER = ("new", "contacted", "booked", "won", "lost")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _day_totals(numbers: Numbers) -> tuple[int, int, int]:
    return (
        sum(r.spend_cents for r in numbers.rows),
        sum(r.calls for r in numbers.rows),
        sum(r.forms for r in numbers.rows),
    )


def subject(numbers: Numbers, items: list[Item], *, edition: str, business_name: str, several: bool) -> str:
    """The mail's subject: yesterday's spend, calls and forms -- the week's spend, leads and booked on a Monday --
    "Needs you: " first when an item is high, the business's name last when the owner has more than one."""
    if edition == "weekly" and numbers.week is not None:
        rows = numbers.week.rows
        spent = sum(r.spend_cents for r in rows)
        line = f"Your week: {usd(spent)} spent, {plural(sum(r.leads for r in rows), 'lead')}, "
        line += f"{sum(r.booked for r in rows)} booked"
    else:
        spent, calls, forms = _day_totals(numbers)
        line = f"Yesterday: {usd(spent)} spent, {plural(calls, 'call')}, {plural(forms, 'form')}"
    if any(i.severity == "high" for i in items):
        line = NEEDS_YOU + line
    if several:
        line = f"{line} · {business_name}"
    return mail.subject_line(line)[:MAX_SUBJECT]


def sentence(numbers: Numbers, *, edition: str, numbers_final: bool) -> str:
    """Yesterday's spend, calls and forms and the month by name ("October: $412.30 of $600")."""
    spent, calls, forms = _day_totals(numbers)
    month_total = sum(numbers.month_spend_cents.values())
    said = f"Yesterday: {usd(spent)} spent, {plural(calls, 'call')}, {plural(forms, 'form')}."
    if numbers.limit_usd:
        said += f" {numbers.month_name}: {usd(month_total)} of {plans.usd(numbers.limit_usd)}."
    else:
        said += f" {numbers.month_name} so far: {usd(month_total)}."
    return f"{NOT_FINAL} {said}" if not numbers_final else said


def _week(numbers: Numbers) -> dict | None:
    week = numbers.week
    if week is None:
        return None
    best = next((r.label for r in week.rows if r.channel == week.best_channel), None)
    said = (
        f"You spent {usd(week.month_spend_cents)} in {numbers.month_name}. {plural(week.month_leads, 'lead')}, "
        f"{week.month_booked} booked."
    )
    if best:
        said += f" Best channel: {best}."
    return {
        "rows": [
            {
                "channel": r.channel,
                "label": r.label,
                "spend_usd": r.spend_cents / 100,
                "leads": r.leads,
                "calls": r.calls,
                "booked": r.booked,
                "cost_per_lead_usd": r.cost_per_lead_usd,
                "cost_per_booked_usd": r.cost_per_booked_usd,
                "sample_word": r.sample_word,
                "source": r.source,
            }
            for r in week.rows
        ],
        "prior": [
            {
                "channel": r.channel,
                "spend_usd": r.prior_spend_cents / 100,
                "leads": r.prior_leads,
                "calls": r.prior_calls,
                "booked": r.prior_booked,
            }
            for r in week.rows
        ],
        "leads_by_status": dict(week.leads_by_status),
        "best_channel": week.best_channel,
        "month_sentence": said,
    }


def build(
    numbers: Numbers,
    items: list[Item],
    *,
    edition: str,
    numbers_final: bool,
    business_name: str,
    several: bool,
    built_at: datetime,
    safe_actions: list[str],
) -> dict:
    """The stored body (§4.4), without the take: `with_insight` adds it."""
    rows: list[dict] = [
        {
            "channel": r.channel,
            "label": r.label,
            "class": r.cls,
            "state_text": r.state_text,
            "spend_usd": r.spend_cents / 100,
            "clicks": r.clicks,
            "calls": r.calls,
            "forms": r.forms,
            "leads": r.leads,
            "daily_budget_usd": r.daily_budget_cents / 100 if r.daily_budget_cents else None,
            "source": r.source,
        }
        for r in numbers.rows
    ]
    if numbers.organic_leads > 0:
        rows.append({"channel": "page", "label": YOUR_PAGE, "leads": numbers.organic_leads})
    return {
        "version": 1,
        "edition": edition,
        "day": numbers.day.isoformat(),
        "time_zone": numbers.zone,
        "numbers_final": numbers_final,
        "built_at": built_at.isoformat(),
        "business": business_name,
        "subject": subject(numbers, items, edition=edition, business_name=business_name, several=several),
        "sentence": sentence(numbers, edition=edition, numbers_final=numbers_final),
        "month": {
            "name": numbers.month_name,
            "spent_usd": sum(numbers.month_spend_cents.values()) / 100,
            "limit_usd": numbers.limit_usd,
            "limit_source": numbers.limit_source,
        },
        "rows": rows,
        "items": [
            {
                "rule": i.rule,
                "severity": i.severity,
                "text": i.text,
                "link": {"label": i.link[0], "url": i.link[1]} if i.link else None,
                "ref": i.ref,
            }
            for i in items
        ],
        "week": _week(numbers) if edition == "weekly" else None,
        "recommendations": [],
        "safe_actions": list(safe_actions),
        "note": None,
        "disclosure": plans.DISCLOSURE,
    }


def with_insight(body: dict, *, take: str | None, recommendations: list[dict]) -> dict:
    """The body with Admiry's take (the Analyst's summary an owner may read) and its suggestions' words and labels --
    never their tools: the mail's way in is "Open Admiry"; the owner's assistant reads the actions in get_insights."""
    return {
        **body,
        "note": take,
        "recommendations": [{"text": r["text"], "label": r["action"]["label"]} for r in recommendations],
    }
