"""D92 (channels spec rev. 4, §3.2): the price of adding funds is the amount plus what the way of paying costs,
computed from operator constants; the amount credited is always the amount chosen."""

import pytest

from agent import config, wallet


@pytest.fixture
def settings(monkeypatch):
    for name in (
        "AGENT_CARD_COST_BPS",
        "AGENT_CARD_COST_FIXED_CENTS",
        "AGENT_ACH_COST_BPS",
        "AGENT_ACH_COST_CAP_CENTS",
    ):
        monkeypatch.delenv(name, raising=False)
    return config.load()


@pytest.mark.parametrize(
    ("credit", "rail", "processing"),
    [
        (100000, "card", 3330),  # the spec's example: $1,000 -> $1,033.30
        (20000, "card", 690),
        (500000, "card", 16530),
        (25055, "card", 857),  # 826.815 rounds half up to 827, + 30
        (100000, "ach", 500),  # 800 capped at 500: the spec's $1,005
        (30000, "ach", 240),
        (62500, "ach", 500),  # exactly the cap
        (20000, "ach", 160),
    ],
)
def test_the_quote_table(settings, credit, rail, processing):
    q = wallet.quote(credit, rail, settings)
    assert q == wallet.Quote(rail, credit, processing, credit + processing)


def test_an_unknown_rail_is_refused(settings):
    with pytest.raises(ValueError):
        wallet.quote(100000, "paypal", settings)


def test_the_constants_are_operator_settings(monkeypatch):
    monkeypatch.setenv("AGENT_CARD_COST_BPS", "290")
    monkeypatch.setenv("AGENT_CARD_COST_FIXED_CENTS", "0")
    assert wallet.quote(100000, "card", config.load()).processing_cents == 2900


def test_the_rails_on_offer_follow_the_switch(monkeypatch):
    monkeypatch.delenv("AGENT_ACH_TOPUPS", raising=False)
    assert wallet.rails(config.load()) == ("card",)
    monkeypatch.setenv("AGENT_ACH_TOPUPS", "on")
    assert wallet.rails(config.load()) == ("card", "ach")


def test_the_notes_name_the_price_and_the_cost():
    assert wallet.topup_note("card", 103330, 3330) == "Card payment of $1,033.30 ($33.30 transaction fee)"
    assert wallet.topup_note("ach", 100500, 500) == "Bank transfer of $1,005.00 ($5.00 transaction fee)"
    assert wallet.refund_note("ach") == "Refund to the bank account"
    assert wallet.refund_note(None) == wallet.refund_note("card") == "Refund to the card"
