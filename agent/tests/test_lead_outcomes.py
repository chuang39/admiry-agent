"""What a lead became (spec 2026-10-01 §5, D163; plan G1 Task 2): its value when won, why it was lost, where the row
came from; the leads still waiting for an outcome; a lead the owner reports."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, business, funnel, landing, leads
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key

SCOPES = ("read", "manage", "leads")
LA = "America/Los_Angeles"


def _page_lead(conn, name="Ana", click=("g1", "gclid")) -> str:
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    return leads.record(conn, page, {"name": name, "phone": "6505550199", "service": "Invisalign"}, click)["id"]


def _calendar(conn) -> tuple:
    """The business's account calendar, as add_lead reads it (funnel.calendar): UTC while nothing runs."""
    return funnel.calendar(conn, "biz_1")


def _account_in(conn, zone: str) -> None:
    """The business's ads account keeps its days in `zone`: what its first channel row records."""
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, resources) "
        "VALUES ('biz_1', 'google_search', 'pl_x', 'live', %s)",
        (Jsonb({"time_zone": zone}),),
    )


def _at(monkeypatch, when: datetime) -> None:
    """The account clock (ads_run.now: the calendar's today) reads `when`."""
    monkeypatch.setattr(ads_run, "now", lambda: when)


def test_a_page_lead_starts_with_no_value_no_reason_and_the_page_as_its_source(conn):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    _page_lead(conn)
    lead = leads.list_for(conn, "biz_1", None)[0]
    assert (lead["value_usd"], lead["reason"], lead["source"], lead["kind"]) == (None, None, "page", "form")


def test_won_takes_a_value_and_lost_takes_a_reason(conn):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    lid = _page_lead(conn)
    won = leads.update(conn, "biz_1", lid, "won", None, value_usd=1800)
    assert (won["status"], won["value_usd"]) == ("won", 1800.0)
    lost = leads.update(conn, "biz_1", lid, "lost", None, reason="out_of_area")
    assert (lost["status"], lost["reason"], lost["value_usd"]) == ("lost", "out_of_area", 1800.0)  # a value stays


def test_a_lead_that_became_a_job_keeps_no_reason_for_being_lost(conn):
    """Open item B10: a reason stayed on a lead later marked won. Booked or won clears it; any other status keeps the
    reason given earlier until another replaces it."""
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    lid = _page_lead(conn)
    leads.update(conn, "biz_1", lid, "lost", None, reason="price")
    won = leads.update(conn, "biz_1", lid, "won", None, value_usd=1800)
    assert (won["status"], won["reason"], won["value_usd"]) == ("won", None, 1800.0)
    leads.update(conn, "biz_1", lid, "lost", None, reason="no_answer")
    assert leads.update(conn, "biz_1", lid, "contacted", None)["reason"] == "no_answer"  # not a job: it stays
    assert leads.update(conn, "biz_1", lid, "booked", None)["reason"] is None


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"value_usd": -5}, "value_usd must be between 0 and 1,000,000"),
        ({"value_usd": 2_000_000}, "value_usd must be between 0 and 1,000,000"),
        ({"reason": "because"}, "reason must be one of"),
    ],
)
def test_a_bad_value_or_reason_is_refused_and_nothing_changes(conn, kwargs, text):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    lid = _page_lead(conn)
    with pytest.raises(ToolError, match=text):
        leads.update(conn, "biz_1", lid, "won", None, **kwargs)
    assert leads.list_for(conn, "biz_1", None)[0]["status"] == "new"


def test_the_list_filters_by_kind_and_by_waiting_and_finds_one_lead(conn):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    first = _page_lead(conn, "Ana")
    _page_lead(conn, "Bo")
    leads.update(conn, "biz_1", first, "booked", None)
    leads.add_owner(conn, "biz_1", kind="call", status="won", value_usd=900, calendar=_calendar(conn))
    assert [x["contact"]["name"] for x in leads.list_for(conn, "biz_1", None, awaiting_outcome=True)] == ["Bo"]
    assert [x["source"] for x in leads.list_for(conn, "biz_1", None, kind="call")] == ["owner"]
    assert [x["id"] for x in leads.list_for(conn, "biz_1", None, lead_id=first)] == [first]
    assert leads.counts(conn, "biz_1") == {"new": 1, "contacted": 0, "booked": 1, "won": 1, "lost": 0, "spam": 0}
    with pytest.raises(ToolError, match="kind must be one of form, call, other or booking"):
        leads.list_for(conn, "biz_1", None, kind="fax")


def test_a_lead_the_owner_reports_has_no_channel_and_keeps_its_day(conn):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    lead = leads.add_owner(
        conn,
        "biz_1",
        kind="call",
        status="won",
        calendar=_calendar(conn),
        happened_at="2026-09-28",
        service="Water heaters",
        value_usd=1800,
        note="called after seeing the ad",
        contact={"name": "Johnson", "phone": "408-555-0142"},
    )
    assert (lead["source"], lead["channel"], lead["click"], lead["kind"]) == ("owner", None, False, "call")
    noon = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)  # nothing runs yet: the day is UTC's, at noon
    assert datetime.fromisoformat(lead["when"]) == noon  # any session zone
    assert lead["service"] == "Water heaters"
    assert lead["contact"] == {"name": "Johnson", "phone": "408-555-0142", "email": None}
    row = conn.execute("SELECT created_at, note FROM lead").fetchone()
    assert row["created_at"] == noon and row["note"] == "called after seeing the ad"


def test_a_lead_reported_with_a_date_is_counted_on_that_day_in_the_accounts_calendar(client, conn, monkeypatch):
    """Final review B, Important 5: "2026-10-01" was stored at midnight UTC -- 5 pm on 30 September in Los Angeles --
    so October's funnel and the goal's progress left it out. A date alone is that day in the account's own calendar,
    stored at noon there."""
    key = business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    _account_in(conn, LA)
    goal = business.set_goal(conn, "biz_1", target=10, monthly_limit_usd=2000)
    _at(monkeypatch, datetime(2026, 10, 2, 19, tzinfo=timezone.utc))  # noon on 2 October there
    sent = {"kind": "call", "status": "won", "value_usd": 900, "happened_at": "2026-10-01", "idempotency_key": "d1"}
    added = call_tool(client, "add_lead", sent, key=key)
    assert not added["isError"], tool_text(added)
    stored = conn.execute("SELECT created_at FROM lead").fetchone()["created_at"]
    assert stored == datetime(2026, 10, 1, 12, tzinfo=ZoneInfo(LA))
    month = funnel.month(conn, "biz_1")
    assert (month["leads"], month["won"], month["revenue_reported_usd"]) == (1, 1, 900.0)
    assert funnel.build(conn, "biz_1", "last_month")["funnel"]["leads"] == 0  # not September's
    assert funnel.goal_progress(conn, "biz_1", goal)["so_far"] == 1


def test_todays_date_there_is_taken_whatever_the_hour_and_tomorrows_is_refused(conn, monkeypatch):
    """The future is judged in the account's calendar too: late in the evening there it is already tomorrow in UTC,
    and today's date there is still today; early in the morning its noon is still ahead, and the date is today all
    the same. Tomorrow's date there is refused (it was taken while UTC's day had begun)."""
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    _account_in(conn, LA)
    _at(monkeypatch, datetime(2026, 10, 2, 5, 30, tzinfo=timezone.utc))  # 10:30 pm on 1 October there
    late = leads.add_owner(
        conn, "biz_1", kind="call", status="booked", calendar=_calendar(conn), happened_at="2026-10-01"
    )
    assert datetime.fromisoformat(late["when"]) == datetime(2026, 10, 1, 12, tzinfo=ZoneInfo(LA))
    with pytest.raises(ToolError, match="happened_at cannot be in the future"):
        leads.add_owner(conn, "biz_1", kind="call", status="booked", calendar=_calendar(conn), happened_at="2026-10-02")
    _at(monkeypatch, datetime(2026, 10, 1, 13, tzinfo=timezone.utc))  # 6 am on 1 October there
    early = leads.add_owner(
        conn, "biz_1", kind="call", status="booked", calendar=_calendar(conn), happened_at="2026-10-01"
    )
    assert datetime.fromisoformat(early["when"]) == datetime(2026, 10, 1, 12, tzinfo=ZoneInfo(LA))
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 2


def test_a_time_with_no_zone_is_read_in_the_accounts_zone_and_one_with_a_zone_is_kept(conn):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    _account_in(conn, LA)
    there = leads.add_owner(
        conn, "biz_1", kind="call", status="won", calendar=_calendar(conn), happened_at="2026-09-28T09:15"
    )
    assert datetime.fromisoformat(there["when"]) == datetime(2026, 9, 28, 9, 15, tzinfo=ZoneInfo(LA))
    zoned = leads.add_owner(
        conn, "biz_1", kind="call", status="won", calendar=_calendar(conn), happened_at="2026-09-28T09:15:00+00:00"
    )
    assert datetime.fromisoformat(zoned["when"]) == datetime(2026, 9, 28, 9, 15, tzinfo=timezone.utc)
    with pytest.raises(ToolError, match="happened_at cannot be in the future"):
        leads.add_owner(
            conn, "biz_1", kind="call", status="won", calendar=_calendar(conn), happened_at="2999-01-01T09:15"
        )


def test_a_walk_in_is_a_lead_of_kind_other(conn):
    """The table's check on `kind` takes 'other' (the migration lead_kind_other ran on this database), and the new
    column's own check refuses a source that is not one of the three."""
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    lead = leads.add_owner(
        conn, "biz_1", kind="other", status="booked", calendar=_calendar(conn), note="walked in, saw the ad"
    )
    assert (lead["kind"], lead["source"], lead["status"]) == ("other", "owner", "booked")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE lead SET source = 'friend' WHERE id = %s", (lead["id"],))


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"kind": "fax", "status": "won"}, "kind must be one of form, call or other"),
        # A3p-3 P27: a booking completed on the page is the page's to report, never the owner's
        ({"kind": "booking", "status": "won"}, "kind must be one of form, call or other"),
        ({"kind": "call", "status": "maybe"}, "status must be one of"),
        ({"kind": "call", "status": "won", "happened_at": "yesterday"}, "happened_at must be an ISO date or time"),
        ({"kind": "call", "status": "won", "happened_at": "2999-01-01"}, "happened_at cannot be in the future"),
        ({"kind": "call", "status": "won", "contact": "Johnson"}, "contact must be an object"),
        # open item A4 (final review B, Important 6): a contact is taken whole or refused, never cut down to what fits
        (
            {"kind": "call", "status": "won", "contact": {"name": "Ana", "mobile": "650 555 0100"}},
            "^contact takes name, phone and email$",
        ),
        (
            {"kind": "call", "status": "won", "contact": {"name": "Ana", "phone": 6505550199}},
            r"^contact\.phone must be text$",
        ),
        (
            {"kind": "call", "status": "won", "contact": {"email": ["ana@example.com"]}},
            r"^contact\.email must be text$",
        ),
    ],
)
def test_a_reported_lead_is_checked_before_it_is_stored(conn, kwargs, text):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    with pytest.raises(ToolError, match=text):
        leads.add_owner(conn, "biz_1", calendar=_calendar(conn), **kwargs)
    assert conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 0


@pytest.mark.parametrize(
    ("contact", "kept"),
    [
        ({"name": "Johnson", "phone": "408-555-0142"}, {"name": "Johnson", "phone": "408-555-0142", "email": None}),
        ({"email": " j@example.com "}, {"name": None, "phone": None, "email": "j@example.com"}),
        ({"name": "Johnson", "phone": None, "email": ""}, {"name": "Johnson", "phone": None, "email": None}),  # unsent
        (None, {"name": None, "phone": None, "email": None}),
    ],
)
def test_a_contact_in_a_shape_it_takes_is_kept(conn, contact, kept):
    business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    lead = leads.add_owner(conn, "biz_1", kind="call", status="won", calendar=_calendar(conn), contact=contact)
    assert lead["contact"] == kept


def test_the_tools_carry_the_new_fields_and_never_put_a_person_in_the_audit_row(client, conn):
    key = business_with_key(conn, scopes=SCOPES, claim_state="claimed")
    added = call_tool(
        client,
        "add_lead",
        {
            "kind": "call",
            "status": "won",
            "value_usd": 1800,
            "note": "the Johnson job",
            "contact": {"name": "Johnson", "phone": "408-555-0142"},
            "idempotency_key": "al-1",
        },
        key=key,
    )
    assert not added["isError"], tool_text(added)
    lid = data(added)["id"]
    say = added["structuredContent"]["say"]
    assert say == f"Recorded lead {lid} as won, worth $1,800." and not JARGON.search(say)
    updated = call_tool(
        client,
        "update_lead",
        {"lead_id": lid, "status": "lost", "reason": "price", "idempotency_key": "ul-1"},
        key=key,
    )
    assert data(updated)["reason"] == "price"
    listed = call_tool(client, "list_leads", {"awaiting_outcome": True}, key=key)
    assert data(listed) == {"leads": [], "counts": leads.counts(conn, "biz_1"), "awaiting_outcome": 0}
    # refused (a key the contact does not take): the failure's row holds no person either
    person = {"name": "Johnson", "mobile": "408-555-0142"}
    refused = call_tool(
        client, "add_lead", {"kind": "call", "status": "won", "contact": person, "idempotency_key": "al-2"}, key=key
    )
    assert refused["isError"] and "contact takes name, phone and email" in tool_text(refused)
    rows = conn.execute(
        "SELECT ok, args_redacted, result FROM agent_action WHERE tool = 'add_lead' ORDER BY id"
    ).fetchall()
    assert [r["ok"] for r in rows] == [True, False]
    assert "Johnson" not in str(rows) and "408-555-0142" not in str(rows)


def test_a_key_without_the_leads_scope_can_neither_read_nor_report(client, conn):
    key = business_with_key(conn, scopes=("read", "manage"), claim_state="claimed")
    for name, args in (
        ("list_leads", {}),
        ("add_lead", {"kind": "call", "status": "won", "idempotency_key": "x"}),
    ):
        res = call_tool(client, name, args, key=key)
        assert res["isError"] and "lacks the 'leads' scope" in tool_text(res), name
