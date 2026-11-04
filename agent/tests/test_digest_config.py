"""The daily digest's switches (spec 2026-09-30-admiry-daily-digest-design §12; plan 2026-10-07-agent-k2 Task 1, Dg
Task 1): AGENT_DIGEST off by default, operator and on need the sender, the operator's address and the link secret, the
link secrets are 32-byte hex keys never printed; the owner-word regexes live in agent/copy.py."""

import pytest

from agent import config, copy

LINK = "ab" * 32
PREVIOUS = "cd" * 32
EVENTS = "whsec_SECRETvalue"


def _armed(monkeypatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test_x")
    monkeypatch.setenv("AGENT_OPS_EMAIL", "ops@example.com")
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", LINK)


def test_off_by_default():
    s = config.load()
    assert s.digest == "off" and s.digest_link_secret is None and s.digest_link_secret_previous is None
    assert s.digest_run_seconds == 2400 and s.resend_events_webhook_secret is None


def test_bad_mode_refuses_naming_the_variable(monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST", "daily")
    with pytest.raises(ValueError, match="AGENT_DIGEST must be one of off, operator, on or unset, not 'daily'"):
        config.load()


@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "AGENT_OPS_EMAIL", "AGENT_DIGEST_LINK_SECRET"])
def test_operator_needs_the_three_keys(monkeypatch, missing):
    _armed(monkeypatch)
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    monkeypatch.delenv(missing)
    with pytest.raises(
        ValueError, match="AGENT_DIGEST=operator needs RESEND_API_KEY, AGENT_OPS_EMAIL and AGENT_DIGEST_LINK_SECRET"
    ):
        config.load()


def test_operator_and_on_with_the_three_keys(monkeypatch):
    _armed(monkeypatch)
    for mode in ("operator", "on", "ON"):
        monkeypatch.setenv("AGENT_DIGEST", mode)
        assert config.load().digest == mode.lower()


@pytest.mark.parametrize("name", ["AGENT_DIGEST_LINK_SECRET", "AGENT_DIGEST_LINK_SECRET_PREVIOUS"])
def test_link_secret_must_be_hex_32_bytes(monkeypatch, name):
    monkeypatch.setenv(name, "not-a-hex-key-zzleakzz")
    with pytest.raises(ValueError, match=f"{name} must be a hex string of at least 32 bytes") as e:
        config.load()
    assert "zzleakzz" not in str(e.value)
    monkeypatch.setenv(name, "ab" * 31)  # 31 bytes
    with pytest.raises(ValueError, match=name):
        config.load()


def test_run_seconds_must_be_positive(monkeypatch):
    monkeypatch.setenv("AGENT_DIGEST_RUN_SECONDS", "600")
    assert config.load().digest_run_seconds == 600
    for bad in ("0", "-5", "soon"):
        monkeypatch.setenv("AGENT_DIGEST_RUN_SECONDS", bad)
        with pytest.raises(ValueError, match="AGENT_DIGEST_RUN_SECONDS"):
            config.load()


def test_secrets_never_print(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET_PREVIOUS", PREVIOUS)
    monkeypatch.setenv("RESEND_EVENTS_WEBHOOK_SECRET", EVENTS)
    s = config.load()
    assert (s.digest_link_secret, s.digest_link_secret_previous, s.resend_events_webhook_secret) == (
        LINK,
        PREVIOUS,
        EVENTS,
    )
    shown = repr(s)
    assert LINK not in shown and PREVIOUS not in shown and EVENTS not in shown


def test_owner_words_live_in_copy():
    assert copy.jargon("your campaign") == "campaign"
    assert copy.jargon("Your Ad Sets ran") == "Ad Sets"
    assert copy.jargon("your ledger") == "ledger"
    assert copy.jargon("the MCC") == "MCC"
    assert copy.jargon("your ads") is None
