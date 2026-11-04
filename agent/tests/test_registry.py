"""agent/channels/registry.py: which channels exist, what the owner calls each, which click id belongs to which
channel, and the per-business account rows (channel_account) with their owner-side tokens sealed at rest."""

import logging
import re

import pytest
from fastmcp.exceptions import ToolError

from agent import config
from agent.channels import base, registry
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_ENCRYPTION_KEY
from agent.tests.fixtures import FakeAdapter

OTHER_KEY = "fedcba9876543210" * 4
TOKEN = "tok_SECRET_value"


@pytest.fixture
def channels(monkeypatch):
    """registry.CHANNELS as this test's own copy: whatever a test registers is gone after it."""
    monkeypatch.setattr(registry, "CHANNELS", dict(registry.CHANNELS))
    return registry.CHANNELS


def _business(conn, biz_id: str = "biz_1") -> None:
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz_id,))


def _save(conn, channel="google_search", biz_id="biz_1", **kw):
    kw = {"mode": "own_account", "status": "pending", "external": {}} | kw
    registry.save_account(conn, biz_id, channel, **kw)


# ---------------------------------------------------------------- names, order, click ids


def test_every_channel_in_the_order_has_an_owner_name_and_none_is_a_company_name():
    assert set(registry.LABELS) == set(registry.ORDER)
    assert len(set(registry.ORDER)) == len(registry.ORDER)
    for channel in registry.ORDER:
        name = registry.label(channel)
        # D76: Microsoft Advertising, Meta and OpenAI appear only in Settings' small print and the agreement
        assert not re.search(r"\b(?:microsoft|meta|openai)\b", name, re.IGNORECASE), name
        assert not any(jargon.search(name) for jargon in (JARGON, ACCOUNT_JARGON, MONEY_JARGON)), name
    assert registry.label("not_a_channel") == "not_a_channel"


@pytest.mark.parametrize(
    ("kind", "channel"),
    [
        ("gclid", "google_search"),
        ("gbraid", "google_search"),
        ("wbraid", "google_search"),
        ("msclkid", "microsoft_search"),
        ("fbclid", "meta"),
        ("ndclid", "nextdoor"),
        ("nb_cid", "newsbreak"),
        ("rdt_cid", "reddit"),
        ("oppref", "chatgpt_ads"),
        ("utm_source", None),
        ("", None),
        (None, None),
    ],
)
def test_a_click_id_kind_belongs_to_one_channel(kind, channel):
    assert registry.channel_for_click(kind) == channel


def test_every_click_id_kind_maps_to_a_channel_the_cards_can_name():
    assert set(registry.CLICK_ID_KINDS.values()) <= set(registry.ORDER)


# ---------------------------------------------------------------- adapters


def test_a_registered_adapter_is_found_by_its_channel(channels):
    fake = FakeAdapter("reddit")  # a real id that nothing registers in M0 (NewsBreak gets its stub in Task 4)
    assert isinstance(fake, base.Adapter)
    assert registry.register(fake) is fake
    assert registry.adapter("reddit") is fake
    assert registry.known("reddit")
    assert not registry.known("chatgpt_ads")


def test_a_channel_with_no_owner_name_is_not_registered(channels):
    """`label` falls back to the raw id, so an id LABELS does not name would reach owner text as it is."""
    with pytest.raises(ValueError, match="myspace"):
        registry.register(FakeAdapter("myspace"))
    with pytest.raises(ValueError, match="fake"):
        registry.register(FakeAdapter())  # the default id
    assert "myspace" not in channels and "fake" not in channels


def test_a_channel_has_one_adapter(channels):
    first = FakeAdapter("reddit")
    registry.register(first)
    with pytest.raises(ValueError, match="reddit"):
        registry.register(FakeAdapter("reddit"))
    assert registry.adapter("reddit") is first
    # the shipped adapters count too: importing agent.channels registered Google Search
    with pytest.raises(ValueError, match="google_search"):
        registry.register(FakeAdapter("google_search"))


def test_an_unknown_channel_is_a_tool_error(channels):
    with pytest.raises(ToolError, match="unknown channel"):
        registry.adapter("myspace")


# ---------------------------------------------------------------- accounts


def test_a_business_without_a_row_has_no_account(conn):
    _business(conn)
    assert registry.account(conn, "biz_1", "google_search") is None
    assert registry.account(conn, "biz_nobody", "google_search") is None


def test_an_account_round_trips(conn):
    _business(conn)
    _save(conn, external={"customer_id": "1234567890"})
    assert registry.account(conn, "biz_1", "google_search") == base.Account(
        "biz_1", "google_search", "own_account", "pending", {"customer_id": "1234567890"}, None
    )
    assert registry.account(conn, "biz_1", "meta") is None


def test_saving_again_updates_the_row_and_keeps_the_token(conn):
    _business(conn)
    _save(conn, "chatgpt_ads", secret=TOKEN)
    _save(conn, "chatgpt_ads", status="needs_attention", external={"ad_account_id": "acct_9"}, last_error="Billing")
    account = registry.account(conn, "biz_1", "chatgpt_ads")
    assert (account.status, account.external, account.secret) == ("needs_attention", {"ad_account_id": "acct_9"}, TOKEN)
    assert conn.execute("SELECT last_error FROM channel_account").fetchone()["last_error"] == "Billing"
    _save(conn, "chatgpt_ads", status="active")  # the error is cleared by saying nothing
    assert conn.execute("SELECT last_error FROM channel_account").fetchone()["last_error"] is None
    assert registry.account(conn, "biz_1", "chatgpt_ads").secret == TOKEN


def test_each_business_has_its_own_account_and_token(conn):
    for biz in ("biz_1", "biz_2"):
        _business(conn, biz)
        _save(conn, "chatgpt_ads", biz_id=biz, secret=f"tok_{biz}")
    assert registry.account(conn, "biz_1", "chatgpt_ads").secret == "tok_biz_1"
    assert registry.account(conn, "biz_2", "chatgpt_ads").secret == "tok_biz_2"


def test_a_token_is_sealed_at_rest_and_never_printed(conn):
    _business(conn)
    _save(conn, "chatgpt_ads", external={"ad_account_id": "acct_9"}, secret=TOKEN)
    row = conn.execute("SELECT * FROM channel_account").fetchone()
    assert isinstance(row["secret_enc"], bytes) and TOKEN.encode() not in row["secret_enc"]
    assert TOKEN not in str({k: v for k, v in row.items() if k != "secret_enc"})
    account = registry.account(conn, "biz_1", "chatgpt_ads")
    assert account.secret == TOKEN
    assert TOKEN not in repr(account) and TOKEN not in str(account)
    settings = repr(config.load())  # neither the token nor the key that seals it is ever printed with the settings
    assert TOKEN not in settings and TEST_ENCRYPTION_KEY not in settings


def test_a_token_cannot_be_stored_without_the_key(conn, monkeypatch):
    _business(conn)
    monkeypatch.delenv("AGENT_KEY_ENCRYPTION_KEY")
    with pytest.raises(ValueError, match="AGENT_KEY_ENCRYPTION_KEY is not set") as refused:
        _save(conn, "chatgpt_ads", secret=TOKEN)
    assert TOKEN not in str(refused.value)
    assert registry.account(conn, "biz_1", "chatgpt_ads") is None  # nothing was written
    _save(conn, "google_search")  # an account with no token needs no key
    assert registry.account(conn, "biz_1", "google_search").secret is None


@pytest.mark.parametrize("key", [None, OTHER_KEY], ids=["no key configured", "a different key configured"])
def test_a_token_the_box_cannot_open_reads_as_none_with_one_warning(conn, monkeypatch, caplog, key):
    _business(conn)
    _save(conn, "chatgpt_ads", external={"ad_account_id": "acct_9"}, secret=TOKEN)
    sealed = conn.execute("SELECT secret_enc FROM channel_account").fetchone()["secret_enc"]
    if key is None:
        monkeypatch.delenv("AGENT_KEY_ENCRYPTION_KEY")
    else:
        monkeypatch.setenv("AGENT_KEY_ENCRYPTION_KEY", key)
    with caplog.at_level(logging.WARNING, logger="agent.channels.registry"):
        account = registry.account(conn, "biz_1", "chatgpt_ads")
    assert account.secret is None and account.external == {"ad_account_id": "acct_9"}
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 1 and "biz_1/chatgpt_ads" in warned[0]
    assert TOKEN not in warned[0] and str(sealed) not in warned[0] and (key is None or key not in warned[0])


def test_managed_channels_are_the_managed_rows_in_card_order(conn):
    _business(conn)
    _business(conn, "biz_2")
    _save(conn, "newsbreak", mode="managed")
    _save(conn, "google_search")
    _save(conn, "zz_not_shipped", mode="managed")  # a row for a channel this code does not know sorts last
    _save(conn, "meta", mode="managed")
    _save(conn, "google_search", biz_id="biz_2")
    assert registry.managed_channels(conn, "biz_1") == ["newsbreak", "meta", "zz_not_shipped"]
    assert registry.managed_channels(conn, "biz_2") == []
