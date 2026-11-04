"""AGENT_SUPPORT and its two companions (spec 2026-09-30 support-and-feedback §11, D144): off by default; on needs the
Resend key and the ops address, and a person's mailbox while the e-mail channel is off; the /plan sampling rate is a
number in [0, 1]; anything else refuses to boot. The three tables exist."""

import pytest

from agent import config

NAMES = (
    "AGENT_SUPPORT",
    "AGENT_SUPPORT_REPLY_TO",
    "AGENT_FEEDBACK_PLAN_PAGE_RATE",
    "AGENT_EMAIL_INBOUND",
    "RESEND_API_KEY",
    "RESEND_WEBHOOK_SECRET",
    "AGENT_OPS_EMAIL",
)


@pytest.fixture
def env(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_off_by_default(env):
    s = config.load()
    assert s.support is False and s.support_reply_to is None and s.feedback_plan_page_rate == 0.2


def test_on_needs_the_resend_key_and_the_ops_address(env):
    env.setenv("AGENT_SUPPORT", "on")
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        config.load()
    env.setenv("RESEND_API_KEY", "re_x")
    with pytest.raises(ValueError, match="AGENT_OPS_EMAIL"):
        config.load()


def test_on_with_the_channel_off_needs_a_person_to_reply_to(env):
    env.setenv("AGENT_SUPPORT", "on")
    env.setenv("RESEND_API_KEY", "re_x")
    env.setenv("AGENT_OPS_EMAIL", "ops@example.com")
    with pytest.raises(ValueError, match="AGENT_SUPPORT_REPLY_TO"):
        config.load()
    env.setenv("AGENT_SUPPORT_REPLY_TO", " Support@Example.com ")
    s = config.load()
    assert s.support is True and s.support_reply_to == "support@example.com"


def test_on_with_the_channel_on_needs_no_reply_to(env):
    for name, value in (
        ("AGENT_SUPPORT", "on"),
        ("RESEND_API_KEY", "re_x"),
        ("AGENT_OPS_EMAIL", "ops@example.com"),
        ("AGENT_EMAIL_INBOUND", "on"),
        ("RESEND_WEBHOOK_SECRET", "whsec_x"),
    ):
        env.setenv(name, value)
    assert config.load().support is True


@pytest.mark.parametrize(
    "name, value",
    [
        ("AGENT_SUPPORT", "yes"),
        ("AGENT_SUPPORT_REPLY_TO", "a@b@c"),
        ("AGENT_SUPPORT_REPLY_TO", "nobody"),
        ("AGENT_FEEDBACK_PLAN_PAGE_RATE", "1.5"),
        ("AGENT_FEEDBACK_PLAN_PAGE_RATE", "-0.1"),
        ("AGENT_FEEDBACK_PLAN_PAGE_RATE", "nan"),
        ("AGENT_FEEDBACK_PLAN_PAGE_RATE", "a fifth"),
    ],
)
def test_bad_values_refuse_to_boot(env, name, value):
    env.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.load()


def test_the_rate_takes_any_number_in_range(env):
    env.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "1")
    assert config.load().feedback_plan_page_rate == 1.0
    env.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "0")
    assert config.load().feedback_plan_page_rate == 0.0


def test_the_schema_has_the_three_tables_and_the_partial_indexes(conn):
    tables = {
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('support_ticket', 'ticket_event', 'plan_feedback')"
        )
    }
    assert tables == {"support_ticket", "ticket_event", "plan_feedback"}
    idx = {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'support_ticket'")}
    assert {"support_ticket_one_open", "support_ticket_thread_open", "support_ticket_queue"} <= idx
