"""The outcome tools (spec 2026-10-01 §4.2, D163, D164): how the advertising is doing and what the leads became.
`update_lead` and `add_lead` are published as they are: the public tool and the command are the same function."""

from __future__ import annotations

from .registry import dispatch, passthrough, public_tool


@public_tool(cls="RO", scope="read", commands=("performance",))
def get_campaign_performance(period: str = "30d") -> dict:
    """How the advertising is doing, in the owner's terms: what was spent, the visits the ads sent, the leads, how
    many were booked and won, what the owner said the won ones were worth, and what a lead and a booked one cost --
    for the business (funnel) and for each place the ads run (by_channel); for each service (by_service) and for what
    brought them (by_source: an ad's place, a tagged link, a search engine's or a social network's own results,
    another site, or a visit that came straight to the page) the leads and what they became. Beside them, the plan's
    own estimate for the same span and the goal's progress this calendar month. period is 7d, 30d, month or
    last_month; the two months also carry each platform's statement, which you read to the owner exactly as
    written. Call it when the owner asks how their ads are doing. Show `say` as written; never turn these numbers
    into a forecast. USD."""
    return dispatch("performance", period=period)


@public_tool(cls="RO", scope="read", commands=("insights",))
def get_insights(day: str | None = None) -> dict:
    """Admiry's daily look at the advertising's results, written each morning from yesterday's figures (on Mondays,
    last week's): insight.summary says what they mean, insight.findings what stands out, and insight.recommendations
    what Admiry suggests the owner does next, each with the action that does it (one of your tools and its arguments)
    and how sure Admiry is. report is the e-mail Admiry sent the owner that morning, as sent; recent the days before;
    frequency how often the owner gets it (changed in Admiry's Settings or from the e-mail's own links). day
    (2026-10-05) asks for one day. Show `say` as written. Offer a suggestion in its own words and run its action only
    after the owner says yes: where money is involved the action asks for the owner's approval itself. Admiry never
    applies a suggestion by itself."""
    return dispatch("insights", day=day)


@public_tool(cls="RO", scope="leads", commands=("list_leads",))
def get_leads(
    status: str | None = None,
    since: str | None = None,
    channel: str | None = None,
    kind: str | None = None,
    awaiting_outcome: bool = False,
    lead_id: str | None = None,
) -> dict:
    """The business's leads, the 50 newest that match, newest first: who they are, what they asked for, what each
    became (status; value_usd when won; reason when lost), where the lead came from (source: the page, a platform,
    or the owner's own report), which ads brought it (channel), what brought the visitor (came_from) and what a
    form asked for (intent: consult, appointment or quote). A booking completed on the page is kind booking, with no
    contact: the booking provider has it. Filters: status (new, contacted, booked, won, lost, spam), since (an ISO
    date), channel (google_search, newsbreak, …), kind (form, call, other, booking), awaiting_outcome (only those
    nobody has said the outcome of), lead_id (one lead). counts says how many there are in each status, all of
    them. When the owner tells you what happened with a lead, record it with update_lead; when they mention a
    customer who is not in the list, add_lead."""
    return dispatch(
        "list_leads",
        since=since,
        status=status,
        channel=channel,
        kind=kind,
        awaiting_outcome=awaiting_outcome,
        lead_id=lead_id,
    )


passthrough("update_lead")
passthrough("add_lead")
