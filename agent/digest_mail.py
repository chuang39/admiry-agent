"""The digest as an e-mail (digest spec 2026-09-30 §4.1, §4.2, §8, §9; plan 2026-10-07-agent-k2 Task 9, Dg Task 5):
the stored body rendered to a text part and an HTML part with the same words (the HTML from an autoescaped template,
nothing marked safe), the RFC 8058 unsubscribe headers on an owner's mail, the footer by the reader's frequency, and
`mail_out.FOOTER` alone on the last line. An operator's preview names its footer links without their URLs, carries no
unsubscribe header, and adds what Admiry's team reads: the Analyst's look (status, verdict, score, what was dropped,
the Critic's issues) and the platform's codes."""

from __future__ import annotations

from datetime import date

from . import digest_links, mail, mail_out, plans
from .digest_body import (
    ASK_ADMIRY,
    FOOTER_DAILY,
    FOOTER_WEEKLY,
    MAX_SUBJECT,
    REPLY_INVITE,
    STATUS_ORDER,
    YOUR_PAGE,
    plural,
)
from .templating import env

TAKE = "Admiry's take"
SUGGESTS = "Admiry suggests"
NEEDS_YOU = "Needs you"
YOUR_WEEK = "Your week"


def _row_line(r: dict) -> str:
    """A channel's day after its name: spend, the share of its daily budget, clicks, calls, forms, leads."""
    said = f"{plans.usd(r['spend_usd'])} spent"
    if r.get("daily_budget_usd"):
        said += f" ({round(100 * r['spend_usd'] / r['daily_budget_usd'])}% of its daily budget)"
    return (
        f"{said}, {plural(r['clicks'], 'click')}, {plural(r['calls'], 'call')}, {plural(r['forms'], 'form')}, "
        f"{plural(r['leads'], 'lead')}"
    )


def _week_lines(week: dict) -> list[str]:
    prior = {p["channel"]: p for p in week["prior"]}
    lines = []
    for r in week["rows"]:
        p = prior.get(r["channel"], {})
        line = (
            f"{r['label']}: {plans.usd(r['spend_usd'])} spent, {plural(r['leads'], 'lead')}, "
            f"{plural(r['calls'], 'call')}, {r['booked']} booked (the week before: "
            f"{plans.usd(p.get('spend_usd', 0))}, {plural(p.get('leads', 0), 'lead')})"
        )
        if r["cost_per_booked_usd"] is not None:
            line += f"; {plans.usd(r['cost_per_booked_usd'])} per booked job ({r['sample_word']})"
        elif r["cost_per_lead_usd"] is not None:
            line += f"; {plans.usd(r['cost_per_lead_usd'])} per lead ({r['sample_word']})"
        lines.append(line + ".")
    counts = [f"{week['leads_by_status'][s]} {s}" for s in STATUS_ORDER if week["leads_by_status"].get(s)]
    if counts:
        lines.append("Leads this week: " + ", ".join(counts) + ".")
    lines.append(week["month_sentence"])
    return lines


def _operator_lines(extra: dict | None) -> list[str]:
    if extra is None:
        return []
    ins = extra.get("insight")
    lines = ["Operator:"]
    if ins is None:
        lines.append("insight: none")
    else:
        lines.append(
            f"insight: {ins.get('status')} · verdict {ins.get('verdict') or 'none'} · score "
            f"{ins.get('score') if ins.get('score') is not None else 'none'} · dropped {ins.get('dropped') or 'none'}"
        )
        if ins.get("summary_dropped"):
            lines.append(f"summary dropped: {ins['summary_dropped']}")
        if ins.get("issues"):
            lines.append("issues: " + "; ".join(f"{i['dimension']}: {i['text']}" for i in ins["issues"]))
    for channel, codes in (extra.get("codes") or {}).items():
        lines.append(f"codes: {channel}: {', '.join(codes)}")
    return lines


def render(
    settings,
    body: dict,
    *,
    business_id: str,
    frequency: str,
    operator: bool,
    reply_to: str | None,
    operator_extra: dict | None = None,
) -> dict:
    """{"subject", "text", "html", "headers", "reply_to"}: what every attempt sends (stored on the digest row)."""
    business = body.get("business") or "your business"
    report = digest_links.report_url(settings, business_id, date.fromisoformat(body["day"]))
    if operator:
        weekly_only = {"label": "Weekly only", "url": None}
        stop = {"label": f"Stop reports for {business}", "url": None}
        open_admiry = {"label": "Open Admiry", "url": None}
        headers: dict = {}
    else:
        weekly_only = {"label": "Weekly only", "url": digest_links.unsubscribe_url(settings, business_id, "weekly")}
        stop = {
            "label": f"Stop reports for {business}",
            "url": digest_links.unsubscribe_url(settings, business_id, "off"),
        }
        open_admiry = {"label": "Open Admiry", "url": report}
        headers = {
            "List-Unsubscribe": f"<{digest_links.unsubscribe_url(settings, business_id)}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    footer_links = [stop] if frequency == "weekly" else [weekly_only, stop]
    rows = [r for r in body["rows"] if r["channel"] != "page"]
    page = next((r for r in body["rows"] if r["channel"] == "page"), None)
    ctx = {
        "sentence": body["sentence"],
        "rows": [{"label": r["label"], "line": _row_line(r), "state_text": r.get("state_text")} for r in rows],
        "page": f"{YOUR_PAGE}: {plural(page['leads'], 'lead')}" if page else None,
        "items": body["items"],
        "note": body.get("note"),
        "recs": [r["text"] for r in body.get("recommendations") or []],
        "week": _week_lines(body["week"]) if body.get("week") else [],
        "operator": _operator_lines(operator_extra) if operator else [],
        "frequency_line": FOOTER_WEEKLY if frequency == "weekly" else FOOTER_DAILY,
        "footer_links": footer_links,
        "ask": REPLY_INVITE if reply_to else ASK_ADMIRY,
        "open_admiry": open_admiry,
        "disclosure": body.get("disclosure") or plans.DISCLOSURE,
        "footer": mail_out.FOOTER,
        "take_label": TAKE,
        "suggests_label": SUGGESTS,
        "needs_you_label": NEEDS_YOU,
        "week_label": YOUR_WEEK,
    }
    return {
        "subject": mail.subject_line(body["subject"])[:MAX_SUBJECT],
        "text": _text(ctx),
        "html": env.get_template("mail_digest.html").render(**ctx),
        "headers": headers,
        "reply_to": reply_to,
    }


def _link_line(link: dict) -> str:
    return f"{link['label']}: {link['url']}" if link["url"] else link["label"]


def _text(ctx: dict) -> str:
    """The text part: blocks separated by a blank line, `mail_out.FOOTER` alone on the last line."""
    blocks: list[list[str]] = [[ctx["sentence"]]]
    lines = [f"{r['label']}: {r['line']}" + (f" — {r['state_text']}" if r["state_text"] else "") for r in ctx["rows"]]
    if ctx["page"]:
        lines.append(ctx["page"])
    if lines:
        blocks.append(lines)
    if ctx["items"]:
        needs = [f"{ctx['needs_you_label']}:"]
        for i in ctx["items"]:
            needs.append(f"- {i['text']}")
            if i.get("link"):
                needs.append(f"  {i['link']['label']}: {i['link']['url']}")
        blocks.append(needs)
    if ctx["note"]:
        blocks.append([f"{ctx['take_label']}: {ctx['note']}"])
    if ctx["recs"]:
        blocks.append([f"{ctx['suggests_label']}:", *(f"- {r}" for r in ctx["recs"])])
    if ctx["week"]:
        blocks.append([f"{ctx['week_label']}:", *ctx["week"]])
    if ctx["operator"]:
        blocks.append(ctx["operator"])
    blocks.append([ctx["frequency_line"], *(_link_line(link) for link in ctx["footer_links"])])
    blocks.append([ctx["ask"], _link_line(ctx["open_admiry"])])
    blocks.append([ctx["disclosure"]])
    blocks.append([ctx["footer"]])
    return "\n\n".join("\n".join(b) for b in blocks) + "\n"
