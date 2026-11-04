"""The guard every owner sentence a role writes passes before an owner reads it (capability layer K2, spec 2026-10-07
§4: numbers are computed, never generated; the digest spec 2026-09-30 §6.3, first written for the digest's comment)."""

from decimal import Decimal

import pytest

from agent.roles import guard

MATERIAL = '{"spend": "$23.10", "calls": 2, "forms": 1, "month": "$1,234.50", "clicks_change_pct": 12}'
EXTRA = {Decimal(5), Decimal(6)}  # the reported day's and the next day's day of the month


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Two calls and $23 spent yesterday.", None),
        ("$23.10 spent, 2 calls.", None),
        ("$23.1 spent.", None),
        ("$24 spent.", guard.NUMBER),
        ("$1,234.50 spent this month.", None),
        ("$1,235 spent this month.", None),
        ("$1,236 spent this month.", guard.NUMBER),
        ("Clicks rose 12%.", None),
        ("The 17% drop needs a look.", guard.NUMBER),
        ("Three people clicked.", guard.NUMBER),
        ("One new lead came in.", None),
        ("One thing to do today.", None),
        ("No one called yet.", None),
        ("October 5 was quiet.", None),
        # a number is read whole, never by its first fragment (final review I5)
        ("12,000 people saw your ads.", guard.NUMBER),
        ("About 5,000 searches.", guard.NUMBER),
        ("Calls ran 1.2x the usual.", guard.NUMBER),
        ("$5k spent.", guard.NUMBER),
        ("Clicks rose 12,5 times.", guard.NUMBER),
        ("The 5th was quiet.", None),
        ("The 9th was quiet.", guard.NUMBER),
        ("Your campaign ran.", guard.JARGON),
        ("See https://x.example for more.", guard.LINK),
        ("Call (650) 555-1234 back.", guard.LINK),
        ("<b>hi</b>", guard.LINK),
        ("One. Two. Three. Four.", guard.LONG),
        ("", guard.EMPTY),
        ("   ", guard.EMPTY),
    ],
)
def test_number_guard_table(text, expected):
    assert guard.check(text, True, MATERIAL, extra=EXTRA) == expected


def test_a_number_with_thousands_separators_is_held_whole():
    assert guard.check("1,200 people saw your ads.", True, '{"impressions": 1200}') is None
    assert guard.check("1,200 people saw your ads.", True, '{"impressions": 1}') == guard.NUMBER


def test_ungrounded_and_the_limits():
    assert guard.check("Two calls.", False, MATERIAL) == guard.UNGROUNDED
    assert guard.check("a" * 451, True, MATERIAL) == guard.LONG
    assert guard.check("Two calls. One form.", True, MATERIAL, max_sentences=1) == guard.LONG
    assert guard.check("$23.10 spent.", True, MATERIAL, max_chars=10) == guard.LONG


def test_tokens_split_money_from_other_numbers():
    money, other = guard.tokens("$1,234.50 and 12% of 41 clicks, two calls")
    assert money == {Decimal("1234.50")}
    assert other == {Decimal(12), Decimal(41), Decimal(2)}


def test_known_reads_money_with_separators_whole():
    held = guard.known('{"month": "$1,234.50", "calls": 2}')
    assert Decimal("1234.50") in held and Decimal(2) in held and Decimal(234) not in held


def test_a_decimal_point_ends_no_sentence():
    assert guard.sentences("$23.10 spent. 2 calls.") == 2
