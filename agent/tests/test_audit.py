from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastmcp.exceptions import ToolError

from agent import audit


def test_record_stores_non_json_values_as_strings(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    at = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    row_id = audit.record(
        conn,
        tool="whoami",
        args={"n": 1},
        result={"at": at, "amount": Decimal("12.50")},
        ok=True,
        business_id="biz_1",
        actor="key:k1",
        actor_class="business",
        api_key_id=None,
        ip="local",
    )
    row = conn.execute("SELECT result FROM agent_action WHERE id = %s", (row_id,)).fetchone()
    assert row["result"] == {"at": "2026-09-24 12:00:00+00:00", "amount": "12.50"}


def test_record_scrubs_secret_fields_in_args_and_results(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    row_id = audit.record(
        conn,
        tool="start",
        args={"text": "hi", "approval_code": "48-311"},
        result={"business_id": "biz_1", "draft_key": "ak_" + "f" * 40, "nested": {"api_key": "x"}},
        ok=True,
        business_id="biz_1",
        actor="ip:1.2.3.4",
        actor_class="anonymous",
        api_key_id=None,
        ip="1.2.3.4",
    )
    row = conn.execute("SELECT args_redacted, result FROM agent_action WHERE id = %s", (row_id,)).fetchone()
    assert row["args_redacted"] == {"text": "hi", "approval_code": "***"}
    assert row["result"] == {"business_id": "biz_1", "draft_key": "***", "nested": {"api_key": "***"}}


def test_record_scrubs_a_leads_personal_fields(conn):
    """A3p-1 P6 / I6: a lead's contact (whole), its preferred time and message, and the owner's note on it
    (update_lead audits it as `lead_note`) never reach an audit row, at any depth."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    lead = {
        "id": "ld_1",
        "contact": {"name": "Ana", "phone": "6505550199", "email": None},
        "service": "Invisalign",
        "preferred_time": "mornings",
        "message": "my retainer cracked",
    }
    row_id = audit.record(
        conn,
        tool="update_lead",
        args={"lead_id": "ld_1", "status": "contacted", "lead_note": "Ana wants Tuesday"},
        result={"data": lead, "say": "Marked lead ld_1 as contacted.", "next": {"kind": "none"}},
        ok=True,
        business_id="biz_1",
        actor="key:k1",
        actor_class="business",
        api_key_id=None,
        ip="local",
    )
    row = conn.execute("SELECT args_redacted, result FROM agent_action WHERE id = %s", (row_id,)).fetchone()
    assert row["args_redacted"] == {"lead_id": "ld_1", "lead_note": "***", "status": "contacted"}
    assert row["result"]["data"] == {
        "id": "ld_1",
        "contact": "***",
        "service": "Invisalign",
        "preferred_time": "***",
        "message": "***",
    }


# A public call's identity (G1 Task 18) is what the call means, not how its JSON happened to be written: a host that
# re-serialises its retry sends the same call.


def test_a_call_identity_leaves_out_an_argument_sent_as_null():
    left_out = audit.call_hash("t", {"idempotency_key": "k"})
    assert audit.call_hash("t", {"idempotency_key": "k", "message": None}) == left_out


def test_a_call_identity_keeps_a_null_inside_an_object():
    """Inside `changes`, null means something: "remove the target"."""
    assert audit.call_hash("t", {"changes": {"target": None}}) != audit.call_hash("t", {"changes": {}})


def test_a_call_identity_reads_a_whole_number_written_with_a_decimal_point_as_that_number_at_any_depth():
    for whole, written in (
        ({"monthly_usd": 1500}, {"monthly_usd": 1500.0}),
        ({"changes": {"monthly_budget_usd": 1500}}, {"changes": {"monthly_budget_usd": 1500.0}}),
        ({"changes": {"channel_caps": {"newsbreak": 200}}}, {"changes": {"channel_caps": {"newsbreak": 200.0}}}),
        ({"items": [{"usd": 0}, 3]}, {"items": [{"usd": 0.0}, 3.0]}),
    ):
        assert audit.call_hash("t", written) == audit.call_hash("t", whole), written


def test_a_call_identity_normalises_nothing_else():
    """A fractional number, a boolean and a string are compared exactly as sent: the owner's words are the request."""
    for one, other in (
        ({"monthly_usd": 12.5}, {"monthly_usd": 12}),
        ({"monthly_usd": 12.5}, {"monthly_usd": 13}),
        ({"owner_confirmed": True}, {"owner_confirmed": 1}),
        ({"owner_confirmed": False}, {"owner_confirmed": 0}),
        ({"message": "make it $1,500"}, {"message": "make it $1,500 "}),
        ({"message": "Bookings"}, {"message": "bookings"}),
        ({"message": "1500"}, {"message": 1500}),
        ({"message": ""}, {}),
    ):
        assert audit.call_hash("t", one) != audit.call_hash("t", other), (one, other)


def test_a_call_identity_ignores_the_order_of_keys():
    assert audit.call_hash("t", {"idempotency_key": "k", "monthly_usd": 1500}) == audit.call_hash(
        "t", {"monthly_usd": 1500, "idempotency_key": "k"}
    )
    in_order = audit.call_hash("t", {"changes": {"target": 30, "autopilot": "off"}})
    assert audit.call_hash("t", {"changes": {"autopilot": "off", "target": 30}}) == in_order


def _surface_row(
    conn, *, tool: str = "adjust", public: str = "update_marketing_plan", business_id: str | None = "biz_1", call="h"
) -> int:
    """One successful public call's row with the idempotency key "k", as run() writes it for `public`."""
    return audit.record(
        conn,
        tool=tool,
        args={},
        result={"data": {}, "say": "Done.", "next": {"kind": "none"}},
        ok=True,
        business_id=business_id,
        actor="key:k1",
        actor_class="business",
        api_key_id=None,
        ip="local",
        idempotency_key="k",
        surface_tool=public,
        surface_call_hash=call,
    )


def test_a_public_call_is_found_again_for_a_day(conn):
    """A host's retry comes within minutes; a key reused days later is a new call, routed afresh."""
    row_id = _surface_row(conn)
    for age, found in (("23 hours", True), ("25 hours", False)):
        conn.execute("UPDATE agent_action SET ts = now() - %s::interval WHERE id = %s", (age, row_id))
        assert (audit.find_surface(conn, "biz_1", "update_marketing_plan", "k") is not None) is found, age


def test_a_commands_replay_answers_within_the_window_then_refuses_the_key(conn):
    """Final review A P1 (open item B1): a command's own replay is bounded by the same window. Within it the row
    answers; after it the key is refused as a reused one -- an answer that old may no longer be true (a pause answered
    "paused" nine days on, while the ads ran), and the unique index never lets the command store that key again, so a
    new key is the way on."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    args = {"channel": "all", "idempotency_key": "k"}
    row_id = audit.record(
        conn,
        tool="pause",
        args=args,
        result={"data": {}, "say": "Paused.", "next": {"kind": "none"}},
        ok=True,
        business_id="biz_1",
        actor="key:k1",
        actor_class="business",
        api_key_id=None,
        ip="local",
        idempotency_key="k",
    )
    conn.execute("UPDATE agent_action SET ts = now() - interval '23 hours' WHERE id = %s", (row_id,))
    assert audit.replay(conn, "biz_1", "pause", "k", args_hash=audit.args_hash(args)).id == row_id
    conn.execute("UPDATE agent_action SET ts = now() - interval '25 hours' WHERE id = %s", (row_id,))
    with pytest.raises(ToolError) as refused:
        audit.replay(conn, "biz_1", "pause", "k", args_hash=audit.args_hash(args))
    assert str(refused.value) == audit.KEY_REUSED


def test_record_stores_the_public_name_without_a_nul(conn):
    """Final review A m1: the public name is the caller's (a resource read stores the address it was sent), so it is
    made storable like every other string a caller supplies."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    row_id = audit.record(
        conn,
        tool="performance",
        args={},
        result={"error": "period must be one of 7d, 30d, month, last_month"},
        ok=False,
        business_id="biz_1",
        actor="key:k1",
        actor_class="business",
        api_key_id=None,
        ip="local",
        surface_tool="resource:admiry://marketing/\x00state",
    )
    stored = conn.execute("SELECT surface_tool FROM agent_action WHERE id = %s", (row_id,)).fetchone()
    assert stored["surface_tool"] == "resource:admiry://marketing/state"


def test_the_lookup_never_matches_a_row_without_a_business(conn):
    _surface_row(conn, business_id=None)
    assert audit.find_surface(conn, None, "update_marketing_plan", "k") is None


def test_the_lookup_finds_this_same_call_before_another_with_the_key(conn):
    """One key may carry the ask and then the confirmation of a launch: the row of the call being repeated comes
    first, whichever was written first; without one, the latest row with the key (which refuses the key's reuse)."""
    launch = "launch_marketing_plan"
    asked = _surface_row(conn, tool="approve_plan", public=launch, call="ask")
    confirmed = _surface_row(conn, tool="confirm_proposal", public=launch, call="confirm")
    found = audit.find_surface(conn, "biz_1", launch, "k", call_hash="ask")
    assert (found.id, found.tool, found.call_hash) == (asked, "approve_plan", "ask")
    assert audit.find_surface(conn, "biz_1", launch, "k", call_hash="other").id == confirmed
    assert audit.find_surface(conn, "biz_1", launch, "k").id == confirmed


def test_record_scrubs_a_sites_reviews_and_its_e_mail_and_keeps_how_a_mail_went(conn):
    """A3p-6 batch 2 Task 4 fix round 1: a site's reviews (a reviewer's first name and words) never reach an audit row,
    at any depth, nor the e-mail address a site's facts show; `email` anywhere else is how a mail went ("sent", in
    set_landing_page's answer, which a replay gives back) and stays."""
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    review = {"text": "They fixed our roof in a day.", "first_name": "Dana", "source": "Google"}
    facts = {"email": "office@oakline.example", "reviews": [review], "warranty": "10 years"}
    row_id = audit.record(
        conn,
        tool="build_site",
        args={"facts": ["email", "reviews", "warranty"]},
        result={"data": {"facts": facts, "site": {"facts": dict(facts)}, "email": "sent"}, "say": "Done."},
        ok=True,
        business_id="biz_1",
        actor="key:k1",
        actor_class="business",
        api_key_id=None,
        ip="local",
    )
    row = conn.execute("SELECT args_redacted, result FROM agent_action WHERE id = %s", (row_id,)).fetchone()
    scrubbed = {"email": "***", "reviews": "***", "warranty": "10 years"}
    assert row["result"]["data"] == {"facts": scrubbed, "site": {"facts": scrubbed}, "email": "sent"}
    assert row["args_redacted"] == {"facts": '["email", "reviews", "warranty"]'}  # the keys only, never scrubbed away
