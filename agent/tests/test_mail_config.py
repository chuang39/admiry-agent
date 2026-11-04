"""AGENT_EMAIL_INBOUND and the Resend settings (spec 2026-09-28 §8): off by default, on needs both Resend values,
anything else refuses to boot; the address is one plain address; the limits are whole numbers >= 1."""

import pytest

from agent import config


def test_email_channel_is_off_by_default(monkeypatch):
    for name in (
        "AGENT_EMAIL_INBOUND",
        "RESEND_API_KEY",
        "RESEND_WEBHOOK_SECRET",
        "AGENT_EMAIL_ADDRESS",
        "AGENT_OPS_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    s = config.load()
    assert s.email_inbound is False and s.resend_api_key is None and s.resend_webhook_secret is None
    assert s.email_address == "hello@agent.admiry.ai" and s.ops_email is None
    assert (s.email_per_sender_hour, s.email_per_sender_day, s.email_per_thread_hour) == (20, 100, 10)


def test_on_needs_both_resend_values(monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "on")
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    with pytest.raises(ValueError, match="RESEND_WEBHOOK_SECRET"):
        config.load()
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_x")
    assert config.load().email_inbound is True


def test_bad_values_refuse_to_boot(monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "yes")
    with pytest.raises(ValueError, match="AGENT_EMAIL_INBOUND"):
        config.load()
    monkeypatch.setenv("AGENT_EMAIL_INBOUND", "off")
    monkeypatch.setenv("AGENT_EMAIL_ADDRESS", "hello+tag@agent.admiry.ai")
    with pytest.raises(ValueError, match="AGENT_EMAIL_ADDRESS"):
        config.load()
    monkeypatch.setenv("AGENT_EMAIL_ADDRESS", "Hello@Agent.Admiry.ai")
    monkeypatch.setenv("AGENT_EMAIL_PER_SENDER_HOUR", "0")
    with pytest.raises(ValueError, match="AGENT_EMAIL_PER_SENDER_HOUR"):
        config.load()
    monkeypatch.setenv("AGENT_EMAIL_PER_SENDER_HOUR", "5")
    assert config.load().email_address == "hello@agent.admiry.ai"


def test_secrets_never_print(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_SECRET")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_SECRET")
    assert "SECRET" not in repr(config.load())


def test_mail_from_is_the_one_address_unless_overridden(monkeypatch):
    """D59 / P16: every Admiry mail, A3p-1's page kit and lead notices too, comes from Admiry <AGENT_EMAIL_ADDRESS>;
    AGENT_MAIL_FROM is only an explicit override."""
    for name in ("AGENT_MAIL_FROM", "AGENT_EMAIL_ADDRESS", "AGENT_EMAIL_INBOUND"):
        monkeypatch.delenv(name, raising=False)
    assert config.load().mail_from == "Admiry <hello@agent.admiry.ai>"
    monkeypatch.setenv("AGENT_EMAIL_ADDRESS", "hello@agent.dev.adx.admiry.ai")
    assert config.load().mail_from == "Admiry <hello@agent.dev.adx.admiry.ai>"
    monkeypatch.setenv("AGENT_MAIL_FROM", "Admiry Team <team@agent.admiry.ai>")
    assert config.load().mail_from == "Admiry Team <team@agent.admiry.ai>"
