"""`python -m agent.ops nb-reconcile <business> <day>` (plan 2026-09-29-agent-m1-newsbreak P10, Task 6; the controller's
ruling T6-7 and its answer 1) and Review Focus 3: NewsBreak's HOURLY cost of the account's day, in the account's own
zone, beside `metric_daily` and the balance's `spend` entry -- three lines that must agree to the cent, in integers, or
the command exits 1 -- then a fourth line, a cumulative cross-check that never changes the exit: NewsBreak's lifetime
total beside its HOURLY report summed over the account's life (30-day windows), the difference shown. The pilot's
exit criterion is fourteen days of lines 1-3 at zero. Over `FakeNewsBreak`, through the adapter that built the ads."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from agent import ops, tick, tools_account, wallet
from agent.tests.conftest import ADS_SECRETS, JARGON, MONEY_JARGON, TEST_DB_URL
from agent.tests.fake_newsbreak import envelope, ok
from agent.tests.test_newsbreak_adapter import (  # noqa: F401 - images, launched and nb_live are fixtures
    BIZ,
    LA,
    NB,
    images,
    launched,
    metric_days,
    nb_live,
    serving,
    sync,
)

DAY = "2026-09-30"  # yesterday in Palo Alto at the tests' NOW (10:00 there on October 1)
LINE = re.compile(r"^(?P<label>[a-z_ ]+?)\s{2,}(?P<day>\d{4}-\d{2}-\d{2})\s+(?P<cents>-?\d+) cents")


def _day_rows(fake, conn, day: str, costs: tuple[int, int]) -> None:
    """NewsBreak's HOURLY report rows for the two running ads on `day`."""
    for ad, cost in zip(serving(conn), costs, strict=True):
        fake.add_report_row(ad, day, cost=cost, click=1)


def _metric(conn, day: str, cost_micros: int) -> None:
    """A `metric_daily` row written by hand, as an operator's correction would be."""
    conn.execute(
        "INSERT INTO metric_daily (business_id, channel, day, cost_micros, impressions, clicks, calls, leads, "
        "conversions, booked, source) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, 'operator') "
        "ON CONFLICT (business_id, channel, day) DO UPDATE SET cost_micros = EXCLUDED.cost_micros",
        (BIZ, NB, day, cost_micros),
    )


def _spend(conn, day: str, cents: int) -> None:
    wallet.spend(conn, BIZ, channel=NB, day=date.fromisoformat(day), cost_cents=cents)


def _reconcile(capsys, day: str = DAY) -> tuple[int, list[str], str]:
    """The command's exit status, its stdout lines and its stderr."""
    code = ops.main(["nb-reconcile", BIZ, day])
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def _figures(lines: list[str]) -> list[tuple[str, str, int]]:
    """Lines 1-3 as (label, day, cents)."""
    found = [LINE.match(line) for line in lines[:3]]
    assert all(found), lines
    return [(m["label"], m["day"], int(m["cents"])) for m in found]


def _read_at(conn, when: datetime) -> None:
    """ads-sync's last read of every NewsBreak day, as of `when` (the job writes the database's own clock)."""
    conn.execute("UPDATE metric_daily SET synced_at = %s WHERE channel = %s", (when, NB))


def _spent(conn) -> dict[str, int]:
    return {
        r["ref"]: r["amount_cents"]
        for r in conn.execute("SELECT ref, amount_cents FROM wallet_entry WHERE kind = 'spend' ORDER BY id")
    }


# ---------------------------------------------------------------- Review Focus 3


def test_hourly_report_becomes_metric_daily_and_one_spend_entry(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """Review Focus 3: NewsBreak's HOURLY report with two ads on three days becomes three `metric_daily` rows (the cost
    summed over the ads, cents -> micros); wallet-settle posts a closed day once -- yesterday only from a read at least
    SETTLE_LAG after its close in the account's zone (an earlier read leaves it `waiting`), never today -- and
    nb-reconcile finds NewsBreak, `metric_daily` and the balance agreeing to the cent, exit 0."""
    for day, costs in (("2026-09-29", (500, 250)), (DAY, (1234, 766)), ("2026-10-01", (99, 1))):
        _day_rows(nb_live, conn, day, costs)
    sync(conn)
    assert [(day, cost) for day, cost, *_rest in metric_days(conn)] == [
        (date(2026, 9, 29), 7_500_000),
        (date(2026, 9, 30), 20_000_000),
        (date(2026, 10, 1), 1_000_000),
    ]
    # Palo Alto's September 30 closed at 07:00 UTC on October 1: a read at 09:07 UTC is inside the three hours
    _read_at(conn, datetime(2026, 10, 1, 9, 7, tzinfo=UTC))
    assert tick.run("wallet-settle", url=TEST_DB_URL) == {"settled": 1, "waiting": 1, "businesses": 1, "errors": 0}
    assert _spent(conn) == {"newsbreak:2026-09-29": -750}
    _read_at(conn, datetime(2026, 10, 1, 10, 7, tzinfo=UTC))
    assert tick.run("wallet-settle", url=TEST_DB_URL)["settled"] == 1
    assert tick.run("wallet-settle", url=TEST_DB_URL)["settled"] == 0  # once
    assert _spent(conn) == {"newsbreak:2026-09-29": -750, "newsbreak:2026-09-30": -2000}

    code, lines, err = _reconcile(capsys)
    assert code == 0, err
    assert _figures(lines) == [
        ("newsbreak report", DAY, 2000),
        ("metric_daily", DAY, 2000),
        ("wallet spend", DAY, 2000),
    ]
    assert len(lines) == 4 and "2850 cents" in lines[3] and "difference 0 cents" in lines[3]


# ---------------------------------------------------------------- the three lines that decide the exit


def test_nb_reconcile_prints_four_matching_lines_and_exits_zero(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """A fixture report against a hand-written `metric_daily` row and spend entry, all 2,000 cents: lines 1-3 show the
    same figure and the command says so; line 4 is the lifetime cross-check."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    _metric(conn, DAY, 20_000_000)
    _spend(conn, DAY, 2000)
    code, lines, err = _reconcile(capsys)
    assert code == 0 and "match" in err
    assert _figures(lines) == [
        ("newsbreak report", DAY, 2000),
        ("metric_daily", DAY, 2000),
        ("wallet spend", DAY, 2000),
    ]
    assert lines[3].startswith("lifetime") and "not part of the exit status" in lines[3]


@pytest.mark.parametrize(
    ("metric_micros", "spend_cents", "extra_report_cents", "off"),
    [
        (20_010_000, 2000, 0, "metric_daily"),
        (20_000_000, 2001, 0, "wallet spend"),
        (20_000_000, 2000, 1, "newsbreak report"),
    ],
    ids=["metric_daily", "the spend entry", "NewsBreak's report"],
)
def test_nb_reconcile_exits_one_on_a_one_cent_difference(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    capsys,
    metric_micros,
    spend_cents,
    extra_report_cents,
    off,
):
    """P10: any difference among lines 1-3, a single cent included, wherever it is, exits 1 -- naming the lines."""
    _day_rows(nb_live, conn, DAY, (1234, 766 + extra_report_cents))
    _metric(conn, DAY, metric_micros)
    _spend(conn, DAY, spend_cents)
    code, lines, err = _reconcile(capsys)
    assert code == 1 and "differ" in err
    figures = {label: cents for label, _day, cents in _figures(lines)}
    assert figures[off] == 2001 and sorted(figures.values()) == [2000, 2000, 2001]


def test_a_cost_in_fractions_of_a_cent_is_a_difference(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """Integers only (T6-7): a `metric_daily` cost that is not a whole number of cents (2,000 cents and one micro) can
    never agree with NewsBreak's whole cents -- the line says so and the command exits 1; and the same for a report
    row whose cost NewsBreak gave in a fraction of a cent (it documents whole cents)."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    _metric(conn, DAY, 20_000_001)
    _spend(conn, DAY, 2000)
    code, lines, _err = _reconcile(capsys)
    assert code == 1 and "20000001 micros" in lines[1] and "not whole cents" in lines[1]
    _metric(conn, DAY, 20_000_000)
    nb_live.report_rows[0]["cost"] = 1234.5
    code, lines, _err = _reconcile(capsys)
    assert code == 1 and "a cost not in whole cents" in lines[0] and "not in whole cents" in lines[3]


def _undated(fake) -> None:
    """NewsBreak answers its report with each row's day under another key than the one Admiry reads (`date`)."""
    real = fake._reports_getIntegratedReport

    def answer(ident, payload):
        data = real(ident, payload).json()["data"]
        data["rows"] = [{**{k: v for k, v in r.items() if k != "date"}, "statDate": r["date"]} for r in data["rows"]]
        return ok(data)

    fake._reports_getIntegratedReport = answer


def test_a_report_row_newsbreak_puts_on_no_day_fails_line_one(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """Fix round 1, item 3 (review M1): money NewsBreak reports on no day Admiry can read must never pass for nothing
    spent -- with `metric_daily` and the spend entry as empty as the day would be, line 1 says the report holds a cost
    on no day and the command exits 1."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    _undated(nb_live)
    opened = datetime(2026, 9, 25, 12, tzinfo=UTC)  # the account's life covers the day, for the fourth line too
    conn.execute("UPDATE channel_account SET created_at = %s WHERE channel = %s", (opened, NB))
    code, lines, err = _reconcile(capsys)
    assert code == 1 and "differ" in err
    assert "2000 cents on no day" in lines[0] and "2000 cents on no day" in lines[3]


def test_a_report_row_without_a_cost_fails_line_one(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """Final review Minor 3: a row of the day's report that carries no cost is money Admiry cannot count, never nothing
    spent -- with `metric_daily` and the spend entry agreeing on the rest, line 1 says the report holds a row with no
    cost and the command exits 1."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    del nb_live.report_rows[1]["cost"]
    _metric(conn, DAY, 12_340_000)
    _spend(conn, DAY, 1234)
    code, lines, err = _reconcile(capsys)
    assert code == 1 and "differ" in err and "a row with no cost" in lines[0]


def test_a_day_nothing_was_spent_on_matches_with_no_rows_at_all(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """A day NewsBreak reports nothing for has no `metric_daily` row and no spend entry: zero everywhere, exit 0. (The
    account row here is stamped by the real clock, after the tests' October 1: the lifetime line still reads today.)"""
    code, lines, _err = _reconcile(capsys)
    assert code == 0 and [cents for _label, _day, cents in _figures(lines)] == [0, 0, 0]
    assert "2026-10-01..2026-10-01 0 cents" in lines[3]


def test_nb_reconcile_reads_the_accounts_day_in_its_own_zone(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """T6-7: the day is the account's (`resources.time_zone`, America/Los_Angeles), asked of NewsBreak as that day
    alone, from the HOURLY source."""
    _reconcile(capsys)
    day_report, *_life = nb_live.bodies("reports/getIntegratedReport")
    assert (day_report["timezone"], day_report["startDate"], day_report["endDate"], day_report["dataSource"]) == (
        LA,
        DAY,
        DAY,
        "HOURLY",
    )


# ---------------------------------------------------------------- the fourth line: a cross-check, never the exit


def test_the_lifetime_line_shows_spend_the_report_does_not_have_yet_and_never_decides_the_exit(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    capsys,
):
    """The coordinator's answer 1: NewsBreak's lifetime total (Get Account Spending Cap) beside its HOURLY report summed
    over the account's life; a total ahead of the report (spend not in the hourly report yet, if the total is live) is
    shown as the difference, labelled so, and lines 1-3 alone decide the exit."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    _metric(conn, DAY, 20_000_000)
    _spend(conn, DAY, 2000)
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] += 37
    code, lines, _err = _reconcile(capsys)
    assert code == 0
    assert "NewsBreak's total 2037 cents" in lines[3] and "2000 cents" in lines[3]
    assert "difference 37 cents" in lines[3] and "not yet in the hourly report" in lines[3]


def test_the_lifetime_report_is_asked_in_windows_of_thirty_days_from_the_accounts_first_day(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    capsys,
):
    """NewsBreak's report covers at most 30 days a request: an account opened 45 days before today (its account row's
    first day in the account's zone) is summed over two windows that end on today."""
    conn.execute(
        "UPDATE channel_account SET created_at = %s WHERE channel = %s", (datetime(2026, 8, 17, 12, tzinfo=UTC), NB)
    )
    _reconcile(capsys)
    _day_report, *life = nb_live.bodies("reports/getIntegratedReport")
    assert [(body["startDate"], body["endDate"]) for body in life] == [
        ("2026-08-17", "2026-09-15"),
        ("2026-09-16", "2026-10-01"),
    ]
    assert {body["timezone"] for body in life} == {LA}


# ---------------------------------------------------------------- what it refuses, and what it leaves behind


def test_a_day_that_is_not_over_yet_is_refused_before_newsbreak_is_asked(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    code, _lines, err = _reconcile(capsys, "2026-10-01")
    assert code == 1 and "not over" in err and nb_live.sent == []


def test_a_business_without_a_newsbreak_ad_account_is_refused(conn, nbapi, capsys):
    conn.execute("INSERT INTO business (id) VALUES (%s)", (BIZ,))
    code, _lines, err = _reconcile(capsys)
    assert code == 1 and "no NewsBreak ad account" in err and nbapi.sent == []
    assert ops.main(["nb-reconcile", BIZ, "30 September"]) == 1  # not a day


def test_newsbreak_refusing_the_report_exits_one_in_its_own_words_without_the_token(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    nb_live.fail["reports/getIntegratedReport"] = envelope(40001, "filterIds invalid for nbtok_SECRET")
    code, lines, err = _reconcile(capsys)
    assert code == 1 and lines == [] and "40001" in err and "filterIds invalid" in err
    assert not any(secret in err for secret in ADS_SECRETS)


def test_each_reconciliation_is_audited_in_plain_words(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    """Like every operator command: an `ops.nb_reconcile` row (actor ops:cli, class platform) with the figures, whose
    owner sentence is plain."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    _metric(conn, DAY, 20_000_000)
    _spend(conn, DAY, 2000)
    _reconcile(capsys)
    (row,) = conn.execute("SELECT * FROM agent_action WHERE tool = 'ops.nb_reconcile'").fetchall()
    assert (row["business_id"], row["actor"], row["actor_class"]) == (BIZ, "ops:cli", "platform")
    assert row["result"]["matches"] is True and row["result"]["report_cents"] == 2000
    said = tools_account.sentence(dict(row), None)
    assert said == "An operator checked your NewsBreak spend for Sep 30 against NewsBreak's own figures"
    assert not JARGON.search(said) and not MONEY_JARGON.search(said)
    assert not any(secret in str(row["result"]) + str(row["args_redacted"]) for secret in ADS_SECRETS)
    assert "report_why" not in row["result"]  # the report was counted


def test_a_reconciliation_whose_report_cannot_be_counted_keeps_the_why_on_its_audit_row(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    capsys,
):
    """Final review (a deferred minor): an audit row whose report figure is missing says why, as line 1 does."""
    _day_rows(nb_live, conn, DAY, (1234, 766))
    del nb_live.report_rows[1]["cost"]
    _reconcile(capsys)
    (row,) = conn.execute("SELECT * FROM agent_action WHERE tool = 'ops.nb_reconcile'").fetchall()
    assert (row["result"]["report_cents"], row["result"]["report_why"], row["result"]["matches"]) == (
        None,
        "a row with no cost",
        False,
    )
