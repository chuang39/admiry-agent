"""The switch per channel (AGENT_EXEC_<CHANNEL>, spec 2026-09-29 D81, plan P7) and the key owner-side tokens are sealed
under (AGENT_KEY_ENCRYPTION_KEY): read once by config.load(), refused at boot when malformed."""

import pytest

from agent import config
from agent.channels import registry
from agent.tests.conftest import TEST_ENCRYPTION_KEY


def test_every_agent_exec_variable_is_one_channels_switch(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "validate")
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    monkeypatch.setenv("AGENT_EXEC_MICROSOFT_SEARCH", "off")
    assert config.load().exec_modes == {"google_search": "validate", "newsbreak": "live", "microsoft_search": "off"}


def test_no_switch_set_means_no_entries():
    assert config.load().exec_modes == {}


def test_an_empty_switch_counts_as_unset(monkeypatch):
    """Compose passes an empty string for a variable the box's .env does not set."""
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "")
    assert config.load().exec_modes == {}


@pytest.mark.parametrize("name", ["AGENT_EXEC_GOOGLE_SEARCH", "AGENT_EXEC_NEWSBREAK", "AGENT_EXEC_META"])
def test_a_switch_outside_off_validate_live_refuses_to_boot(monkeypatch, name):
    monkeypatch.setenv(name, "maybe")
    with pytest.raises(ValueError, match=name):
        config.load()


def test_the_old_google_switch_is_still_read_and_still_validated(monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    assert config.load().ads_execution == "validate"
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "maybe")
    with pytest.raises(ValueError, match="AGENT_ADS_EXECUTION"):
        config.load()


def test_the_launch_allowlist_is_its_owners_emails_lower_cased(monkeypatch):
    """AGENT_LAUNCH_ALLOWLIST (2026-10-08): the owners whose businesses may launch while a switch is on; not an
    AGENT_EXEC_ name, which every switch is read from."""
    assert config.load().launch_allowlist == frozenset()
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "")  # compose's empty value
    assert config.load().launch_allowlist == frozenset()
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", " Founder@Example.com, b@example.com ,,")
    settings = config.load()
    assert settings.launch_allowlist == frozenset({"founder@example.com", "b@example.com"})
    assert settings.exec_modes == {}


def test_a_launch_allowlist_entry_that_is_not_an_email_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com,validate")
    with pytest.raises(ValueError, match="AGENT_LAUNCH_ALLOWLIST"):
        config.load()


def test_the_encryption_key_is_optional_and_never_printed(monkeypatch):
    monkeypatch.delenv("AGENT_KEY_ENCRYPTION_KEY")
    assert config.load().key_encryption_key is None
    monkeypatch.setenv("AGENT_KEY_ENCRYPTION_KEY", "")  # compose's empty value
    assert config.load().key_encryption_key is None
    monkeypatch.setenv("AGENT_KEY_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY.upper())
    settings = config.load()
    assert settings.key_encryption_key == TEST_ENCRYPTION_KEY.upper()
    assert TEST_ENCRYPTION_KEY.upper() not in repr(settings)


@pytest.mark.parametrize("bad", ["hunter2", "zz" * 32, "ab" * 31, "a" * 65])
def test_a_key_that_is_not_32_hex_bytes_refuses_to_boot_without_echoing_it(monkeypatch, bad):
    monkeypatch.setenv("AGENT_KEY_ENCRYPTION_KEY", bad)
    with pytest.raises(ValueError, match="AGENT_KEY_ENCRYPTION_KEY") as refused:
        config.load()
    assert bad not in str(refused.value)


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({}, "off"),  # both unset
        ({"AGENT_ADS_EXECUTION": "live"}, "live"),  # the old variable is the alias for one release
        ({"AGENT_EXEC_GOOGLE_SEARCH": "validate"}, "validate"),
        ({"AGENT_EXEC_GOOGLE_SEARCH": "validate", "AGENT_ADS_EXECUTION": "live"}, "validate"),  # the new one wins
        ({"AGENT_EXEC_GOOGLE_SEARCH": "off", "AGENT_ADS_EXECUTION": "live"}, "off"),  # an explicit off wins too
        ({"AGENT_EXEC_GOOGLE_SEARCH": "", "AGENT_ADS_EXECUTION": "live"}, "live"),  # compose's empty value is unset
    ],
)
def test_google_search_reads_its_own_switch_then_the_old_one(monkeypatch, env, want):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert registry.exec_mode(config.load(), "google_search") == want


def test_every_other_channel_is_off_until_its_own_switch_says_otherwise(monkeypatch):
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")  # the alias is Google's alone
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    settings = config.load()
    assert registry.exec_mode(settings, "newsbreak") == "validate"
    assert registry.exec_mode(settings, "meta") == "off"
    assert registry.exec_mode(settings, "not_a_channel") == "off"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_CARD_COST_BPS", "2001"),
        ("AGENT_ACH_COST_BPS", "-1"),
        ("AGENT_CARD_COST_FIXED_CENTS", "10001"),
        ("AGENT_ACH_COST_CAP_CENTS", "five"),
        ("AGENT_ACH_TOPUPS", "yes"),
        ("AGENT_CARD_LINK", "yes"),
    ],
)
def test_payment_cost_settings_out_of_range_refuse_to_boot(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.load()


def test_link_by_stripe_is_off_unless_agent_card_link_is_on(monkeypatch):
    """Fix round 2b, item 1: Link by Stripe beside the card only once it is activated in the Stripe dashboard -- off
    by default (compose passes off for a box whose .env does not set it), read as AGENT_ACH_TOPUPS is."""
    assert config.load().card_link is False
    for value, on in (("", False), ("off", False), ("on", True)):
        monkeypatch.setenv("AGENT_CARD_LINK", value)
        assert config.load().card_link is on, value


def test_admirys_own_sites_are_off_unless_agent_sites_is_on(monkeypatch):
    """A3p-6 batch 2 (plan Q22): off, every business gets the one-page starter page as before batch 2; compose passes
    0 for a box whose .env does not set it. on or 1 turns them on, off or 0 keeps them off, as their sibling switches
    read (Task 4 fix round 1); anything else refuses to boot, like AGENT_ACH_TOPUPS."""
    assert config.load().sites is False
    for value, on in (("", False), ("0", False), ("off", False), ("1", True), ("on", True), ("ON", True)):
        monkeypatch.setenv("AGENT_SITES", value)
        assert config.load().sites is on, value
    for value in ("yes", "true", "2"):
        monkeypatch.setenv("AGENT_SITES", value)
        with pytest.raises(ValueError, match="AGENT_SITES"):
            config.load()


def test_the_roles_are_off_unless_agent_roles_says_validate_or_on(monkeypatch):
    """AGENT_ROLES (spec 2026-10-07 capability layer §8.3): unset or compose's empty value is off; validate and on in
    any case, with spaces around them."""
    assert config.load().roles_mode == "off"
    monkeypatch.setenv("AGENT_ROLES", "")
    assert config.load().roles_mode == "off"
    for raw, want in (("validate", "validate"), ("ON", "on"), (" off ", "off")):
        monkeypatch.setenv("AGENT_ROLES", raw)
        assert config.load().roles_mode == want


def test_a_roles_switch_outside_off_validate_on_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("AGENT_ROLES", "live")
    with pytest.raises(ValueError, match="AGENT_ROLES"):
        config.load()


def test_knowledge_is_off_unless_agent_knowledge_says_on(monkeypatch):
    """AGENT_KNOWLEDGE (capability layer K3, plan P9): unset or compose's empty value is off; on and 1 in any case."""
    assert config.load().knowledge is False
    for raw, want in (("", False), ("off", False), ("0", False), ("on", True), (" ON ", True), ("1", True)):
        monkeypatch.setenv("AGENT_KNOWLEDGE", raw)
        assert config.load().knowledge is want, raw


def test_a_knowledge_switch_outside_on_and_off_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE", "validate")
    with pytest.raises(ValueError, match="AGENT_KNOWLEDGE must be on or off"):
        config.load()


def test_harness_is_off_unless_agent_harness_says_on(monkeypatch):
    """AGENT_HARNESS (capability layer K6 slice 1, spec 2026-10-10 §7): unset or compose's empty value is off; on and
    1 in any case."""
    assert config.load().harness is False
    for raw, want in (("", False), ("off", False), ("0", False), ("on", True), (" ON ", True), ("1", True)):
        monkeypatch.setenv("AGENT_HARNESS", raw)
        assert config.load().harness is want, raw


def test_a_harness_switch_outside_on_and_off_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS", "validate")
    with pytest.raises(ValueError, match="AGENT_HARNESS must be on or off"):
        config.load()


SANDBOX_SECRET = "sk_test_never_printed_42"


@pytest.mark.parametrize("var", ["STRIPE_SANDBOX_SECRET_KEY", "STRIPE_SANDBOX_WEBHOOK_SECRET"])
@pytest.mark.parametrize("switch", ["AGENT_EXEC_GOOGLE_SEARCH", "AGENT_EXEC_NEWSBREAK", "AGENT_ADS_EXECUTION"])
def test_a_stripe_sandbox_key_with_any_switch_live_refuses_to_boot(monkeypatch, var, switch):
    """Final review I3: the sandbox keys pay for allowlisted owners' demo launches, which build nothing that spends
    (live_paused). With any switch `live` an allowlisted owner's sandbox money would fund real ads, so the box
    refuses to boot -- and never prints the key."""
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    monkeypatch.setenv(var, SANDBOX_SECRET)
    monkeypatch.setenv(switch, "live")
    with pytest.raises(ValueError, match="STRIPE_SANDBOX") as e:
        config.load()
    assert SANDBOX_SECRET not in str(e.value)


@pytest.mark.parametrize("mode", ["off", "validate", "live_paused", ""])
def test_a_stripe_sandbox_key_boots_beside_switches_that_spend_nothing(monkeypatch, mode):
    monkeypatch.setenv("AGENT_LAUNCH_ALLOWLIST", "founder@example.com")
    monkeypatch.setenv("STRIPE_SANDBOX_SECRET_KEY", SANDBOX_SECRET)
    monkeypatch.setenv("STRIPE_SANDBOX_WEBHOOK_SECRET", "whsec_sandbox")
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", mode)
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    assert config.load().stripe_sandbox_secret_key == SANDBOX_SECRET


def test_a_live_switch_boots_without_the_sandbox_keys(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_GOOGLE_SEARCH", "live")
    monkeypatch.setenv("STRIPE_SANDBOX_SECRET_KEY", "")  # compose's empty value counts as unset
    assert registry.exec_mode(config.load(), "google_search") == "live"
