"""Seed helpers the daily digest's and the Analyst's tests share (plan 2026-10-07-agent-k2 Tasks 2-16; Dg plan Task 2):
a business, its channel rows, its daily numbers, its leads and an approved plan, each with only the columns the digest
reads."""

from __future__ import annotations

from datetime import UTC, date, datetime

from psycopg.types.json import Jsonb

from agent import business, config, db, digest_audience, digest_zone, llm, plans
from agent.tests.fixtures import FakeGenerator
from agent.tests.test_ads_run import sections


def sample_numbers(*, weekly: bool = False):
    """A Los Angeles business's Monday 2026-10-05 by hand (no database): Google Search $23.10, 41 clicks, 2 calls,
    1 form; NewsBreak $5 and paused for funds; one lead from the page; October $412.30 of $600; on `weekly`, the week
    against the week before."""
    from agent.digest_numbers import ChannelDay, Numbers, Week, WeekRow

    rows = [
        ChannelDay("google_search", "Google Search", "active", None, 2310, 41, 900, 2, 0.0, 1, 2, 1973, "api"),
        ChannelDay(
            "newsbreak",
            "NewsBreak",
            "active",
            "Paused: there is no money left to run it",
            500,
            10,
            300,
            0,
            0.0,
            0,
            0,
            986,
            "api",
        ),
    ]
    week = None
    if weekly:
        week = Week(
            rows=[
                WeekRow(
                    "google_search",
                    "Google Search",
                    "api",
                    14200,
                    9,
                    6,
                    3,
                    12000,
                    7,
                    5,
                    2,
                    15.78,
                    None,
                    "early: 11 leads",
                ),
                WeekRow("newsbreak", "NewsBreak", "api", 2500, 1, 0, 0, 3000, 2, 0, 0, 25.0, None, "early: 3 leads"),
            ],
            leads_by_status={"new": 4, "booked": 3, "lost": 1},
            best_channel=None,
            month_spend_cents=41230,
            month_leads=14,
            month_booked=4,
        )
    return Numbers(
        day=date(2026, 10, 5),
        zone="America/Los_Angeles",
        rows=rows,
        organic_leads=1,
        leads_by_status={"new": 2, "booked": 1},
        month_name="October",
        next_month_name="November",
        month_spend_cents={"google_search": 39730, "newsbreak": 1500},
        channel_amount_cents={"google_search": 45000, "newsbreak": 15000},
        limit_usd=600.0,
        limit_source="owner",
        week=week,
    )


def sample_items(settings):
    """Three "needs you" items for sample_numbers: a high one with a link, a medium one, an info one."""
    from agent import digest_links
    from agent.digest_rules import Item

    ads = ("See your ads", digest_links.ads_url(settings, "biz_1"))
    report = ("Open Admiry", digest_links.report_url(settings, "biz_1", date(2026, 10, 5)))
    return [
        Item(
            "paused_by_system",
            "high",
            "NewsBreak is paused: there is no money left to run it. Add funds to start it again.",
            ads,
            "pause:newsbreak",
        ),
        Item(
            "leads_unmarked",
            "medium",
            "2 people who contacted you are still marked new. Tell Admiry which ones booked (in Claude, ChatGPT or "
            "Muse) — that is how it learns what a booked job costs.",
            None,
            "leads_unmarked",
        ),
        Item(
            "limit_near",
            "info",
            "Google Search: $397.30 of its $450 for October; it pauses at $450 and restarts November 1.",
            report,
            "limit:google_search",
            ("BUDGET_CONSTRAINED",),
        ),
    ]


ANALYST_DAY = date(2026, 10, 5)  # a Monday: the Tuesday run reports on it
ANALYST_NOW = datetime(2026, 10, 6, 15, 2, tzinfo=UTC)  # 08:02 on Tuesday in Los Angeles


def fake_llm(monkeypatch, **kw) -> FakeGenerator:
    """A FakeGenerator installed as the service's generator, whatever override a caller passes."""
    gen = FakeGenerator(**kw)
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    return gen


def analyst_inputs(conn, biz: str = "biz_1", day: date = ANALYST_DAY):
    """(settings, audience, numbers, items) for `biz`'s `day`, as the Analyst's job builds them (frequency daily)."""
    from agent.roles import analyst

    settings = config.load()
    zone = digest_zone.zone_of(conn, biz)
    aud = digest_audience.audience(conn, {"id": biz, "paused_reason": None, "digest_frequency": "daily"}, day, zone)
    numbers, items, _ = analyst.inputs(conn, settings, biz, day, zone, aud)
    return settings, aud, numbers, items


def analyst_metrics(conn, biz: str = "biz_1", day: date = ANALYST_DAY) -> dict:
    from agent.roles import analyst

    settings, aud, numbers, items = analyst_inputs(conn, biz, day)
    return analyst.metrics(conn, settings, biz, day, aud=aud, numbers=numbers, items=items)


def analyst_build(conn, biz: str = "biz_1", day: date = ANALYST_DAY, now: datetime = ANALYST_NOW):
    from agent.roles import analyst

    settings, aud, numbers, items = analyst_inputs(conn, biz, day)
    business_row = {"id": biz, "paused_reason": None}
    return analyst.ensure(conn, settings, business_row, day, now=now, aud=aud, numbers=numbers, items=items)


def seed_business(
    conn,
    biz: str = "biz_1",
    *,
    owner: str | None = "owner@example.com",
    zone: str | None = None,
    frequency: str = "daily",
    address: str | None = None,
    flags: dict | None = None,
) -> str:
    """A claimed, active business; `zone` is its stored time zone, `address` its listing's (in its knowledge)."""
    conn.execute(
        "INSERT INTO business (id, owner_email, claim_state, time_zone, digest_frequency, operator_flags) "
        "VALUES (%s, %s, 'claimed', %s, %s, %s)",
        (biz, owner, zone, frequency, Jsonb(flags or {})),
    )
    if address is not None:
        business.add_knowledge(conn, biz, {"listing": {"address": address}}, source="owner")
    return biz


def seed_channel(
    conn,
    biz: str,
    channel: str = "google_search",
    *,
    state: str = "live",
    reason: str | None = None,
    paused_at: datetime | None = None,
    launched_at: datetime | None = None,
    resources: dict | None = None,
    last_error: str | None = None,
    synced_at: datetime | None = None,
    plan_id: str = "pl_1",
) -> None:
    """One channel_campaign row, as the jobs leave it."""
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, paused_reason, resources, last_error, "
        "launched_at, paused_at, synced_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (biz, channel, plan_id, state, reason, Jsonb(resources or {}), last_error, launched_at, paused_at, synced_at),
    )


def seed_metric(
    conn,
    biz: str,
    day: date,
    channel: str = "google_search",
    *,
    cost_usd: float = 0.0,
    clicks: int = 0,
    impressions: int = 0,
    calls: int = 0,
    conversions: float = 0,
    leads: int = 0,
    source: str = "api",
) -> None:
    """The channel's numbers for `day` (replacing any already there)."""
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (business_id, channel, day) DO UPDATE SET cost_micros = EXCLUDED.cost_micros, "
        "impressions = EXCLUDED.impressions, clicks = EXCLUDED.clicks, calls = EXCLUDED.calls, "
        "leads = EXCLUDED.leads, conversions = EXCLUDED.conversions, source = EXCLUDED.source",
        (biz, channel, day, round(cost_usd * 1_000_000), impressions, clicks, calls, leads, conversions, source),
    )


def seed_lead(
    conn,
    biz: str,
    created_at: datetime,
    channel: str | None = "google_search",
    *,
    kind: str = "form",
    status: str = "new",
    fields: dict | None = None,
) -> str:
    """A lead the page recorded at `created_at` (`channel` None: one no ad brought)."""
    lead_id = db.new_id("ld")
    conn.execute(
        "INSERT INTO lead (id, business_id, kind, status, channel, fields, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (lead_id, biz, kind, status, channel, Jsonb(fields or {}), created_at),
    )
    return lead_id


def seed_plan(conn, biz: str, amounts: dict[str, float], *, status: str = "approved") -> str:
    """A plan funding `amounts` ({channel: monthly USD}), Google Search with the test plan's whole entry, any other
    channel by its amount; its id minted, so several businesses can each have one."""
    secs = sections(monthly_usd=amounts.get("google_search", 0) or 1000.0)
    # the consultation fields the plan's cards read too (plans.cards), as `recommend` stores them
    secs["consultation"] |= {"conversions": plans.CONVERSIONS["bookings"], "duration_days": 30, "offer": None}
    secs["ads"]["channels"] = [
        {**secs["ads"]["channels"][0], "monthly_usd": usd}
        if ch == "google_search"
        else {"channel": ch, "monthly_usd": usd}
        for ch, usd in amounts.items()
    ]
    version = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 AS v FROM plan WHERE business_id = %s", (biz,)
    ).fetchone()["v"]
    plan_id = db.new_id("pl")
    body = {"sections": secs, "lines": plans.owner_lines(secs), "rationale": "", "notes": []}
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status) VALUES (%s, %s, %s, %s, %s)",
        (plan_id, biz, version, Jsonb(body), status),
    )
    return plan_id
