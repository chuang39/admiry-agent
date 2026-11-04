from agent import db


def test_schema_is_idempotent_and_creates_the_tables(conn):
    db.apply_schema(conn)  # second application must be a no-op
    names = {
        r["table_name"]
        for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    }
    assert {
        "business",
        "knowledge",
        "api_key",
        "goal",
        "plan",
        "proposal",
        "approval_code",
        "agent_action",
        "job_run",
        "benchmark",
        "schema_migration",
        "channel_account",
        "channel_campaign",
        "metric_daily",
        "channel_issue",
        "allocation",
    } <= names


def test_new_id_has_prefix_and_entropy():
    a, b = db.new_id("biz"), db.new_id("biz")
    assert a.startswith("biz_") and len(a) == 4 + 16 and a != b


def test_idempotency_index_rejects_a_duplicate_key(conn):
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    conn.execute(
        "INSERT INTO agent_action (business_id, actor, tool, args_hash, idempotency_key) "
        "VALUES ('biz_1', 'key:k1', 'revoke_key', 'h', 'idem-1')"
    )
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO agent_action (business_id, actor, tool, args_hash, idempotency_key) "
            "VALUES ('biz_1', 'key:k1', 'revoke_key', 'h', 'idem-1')"
        )


def test_a_channel_campaign_takes_the_lifecycle_states_only(conn):
    """Spec 2026-09-29 §5.3. A3's `validated` is `queued` with validated_at set (plan P4): the table refuses it."""
    import psycopg
    import pytest

    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    insert = "INSERT INTO channel_campaign (business_id, channel, plan_id, state) VALUES ('biz_1', %s, 'pl_1', %s)"
    states = ("waiting_account", "waiting_creative", "queued", "launching", "live", "paused", "limited", "failed")
    for i, state in enumerate((*states, "operator")):
        conn.execute(insert, (f"channel_{i}", state))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(insert, ("channel_x", "validated"))
