"""`python -m agent.ops nb-account-ready <business>` (plan 2026-09-29-agent-m1-newsbreak Task 6; the controller's
ruling T6-5 and its answer 3): what an operator runs once a NewsBreak account's trouble is fixed at NewsBreak. It
clears the "waiting at NewsBreak" mark Task 5a sets -- an account NewsBreak made in a state other than ACTIVE, or
refused to make -- so the next hourly step goes on at once, and it clears a SPEND_CHECK stop: the cap Admiry last set is
recorded as NewsBreak's own total only when that total is above it by more than NewsBreak's overshoot (never lowered
otherwise: a later spend must not look like a mismatch), and the mail mark goes. Each clearing is audited like the
other operator commands. Over `FakeNewsBreak`, through the adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent import ads_run, config, ops, tools_account
from agent.channels import registry
from agent.channels.newsbreak import adapter as nb
from agent.tests.conftest import ADS_SECRETS, JARGON, MONEY_JARGON
from agent.tests.fake_newsbreak import envelope
from agent.tests.test_newsbreak_adapter import (  # noqa: F401 - images, launched and nb_live are fixtures
    BIZ,
    NB,
    NOW,
    add_plan,
    external,
    images,
    launched,
    nb_live,
    nb_sections,
    queue,
    seed,
    step,
    sync,
)


def _ready(capsys, biz: str = BIZ) -> tuple[int, str]:
    code = ops.main(["nb-account-ready", biz])
    out, err = capsys.readouterr()
    return code, out + err


def _audited(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM agent_action WHERE tool = 'ops.nb_account_ready' ORDER BY id")]


def _plain(sentence: str) -> bool:
    return not JARGON.search(sentence) and not MONEY_JARGON.search(sentence)


# ---------------------------------------------------------------- the "waiting at NewsBreak" mark


def test_the_wait_at_newsbreak_is_cleared_and_the_next_step_starts_the_ads(conn, nb_live, resend, capsys):  # noqa: F811 - the fixtures of the same name
    """NewsBreak made the account PENDING (no API reads an account's status): the row waits with the operator mailed.
    The operator funds and verifies it at NewsBreak and runs the command: the mark goes (status, when it was looked
    at, the mails sent for it), the account row is active again and ready at once -- no hour to wait -- and the next
    step starts the ads. Audited, in plain words."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.account_status = "PENDING"
    assert step(conn)["last_error"] == nb.ACCOUNT_WAIT
    (account_id,) = nb_live.accounts
    code, said = _ready(capsys)
    assert code == 0 and "PENDING" in said
    held = external(conn)
    assert held == {"ad_account_id": account_id} and registry.account(conn, BIZ, NB).status == "active"
    assert ads_run.account_status(conn, config.load(), BIZ, NB).ready
    assert step(conn)["state"] == "live"
    (row,) = _audited(conn)
    assert (row["actor"], row["actor_class"], row["result"]) == (
        "ops:cli",
        "platform",
        {"cleared": "account", "was": "PENDING"},
    )
    sentence = tools_account.sentence(row, None)
    assert sentence == "An operator marked your NewsBreak account ready to run your ads" and _plain(sentence)


def test_after_a_refused_create_the_next_step_makes_the_account(conn, nb_live, resend, capsys):  # noqa: F811 - the fixtures of the same name
    """NewsBreak refused to make the account (the Organization's allowance): once an operator had it raised, the command
    clears the mark -- the account row back to `none`, since there is no ad account yet -- and the next step makes
    it."""
    v1 = seed(conn)
    queue(conn, v1)
    nb_live.fail["ad-account/create"] = envelope(41003, "ad account limit reached for this organization")
    assert step(conn)["last_error"] == nb.ACCOUNT_WAIT and not nb_live.accounts
    del nb_live.fail["ad-account/create"]
    code, _said = _ready(capsys)
    assert code == 0 and external(conn) == {} and registry.account(conn, BIZ, NB).status == "none"
    assert step(conn)["state"] == "live" and len(nb_live.accounts) == 1


# ---------------------------------------------------------------- a SPEND_CHECK stop


def _spend_check(conn, fake) -> str:
    """NewsBreak's total above the 60,000-cent cap the launch set by more than NewsBreak's own overshoot: the next
    change stops on SPEND_CHECK and the operator is mailed. Returns the ad account's id."""
    (account_id,) = fake.accounts
    fake.spent[account_id] = 61_000
    add_plan(conn, nb_sections(monthly_usd=900))
    assert step(conn)["last_error"] == nb.SPEND_CHECK
    assert "spend_check_mailed_at" in external(conn)
    return account_id


def test_a_spend_check_is_cleared_at_newsbreaks_total_and_the_next_step_sets_the_cap(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    resend,
    capsys,
):
    """T6-5: the operator checked the figures (the spend is real) and runs the command: NewsBreak's total is read once
    and recorded as the last cap, the mail mark goes, and the next step sets the month's cap from the recorded base --
    the change goes out."""
    account_id = _spend_check(conn, nb_live)
    nb_live.sent.clear()
    code, said = _ready(capsys)
    assert code == 0 and "61000" in said
    assert nb_live.kinds() == ["balance/getAccountBudgetInfo"]  # read, nothing set at NewsBreak
    held = external(conn)
    assert held["spending_cap_cents"] == 61_000 and "spend_check_mailed_at" not in held
    out = step(conn)
    assert (out["state"], out["last_error"]) == ("live", None) and nb_live.caps[account_id] == 90_000
    (row,) = _audited(conn)
    assert row["result"] == {
        "cleared": "spend_check",
        "recorded": True,
        "total_cents": 61_000,
        "last_cap_cents": 60_000,
    }
    sentence = tools_account.sentence(row, None)
    assert sentence == "An operator checked NewsBreak's spend figures, so your latest change there goes out" and _plain(
        sentence
    )


def test_clearing_the_reads_own_stop_for_a_new_month_says_the_months_budget_goes_in(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    resend,
    capsys,
    monkeypatch,
):
    """Fix round 1, M4: the stop was the hourly read's own, for a new account month (MONTH_CHECK) -- no change of the
    owner's waited -- so the activity line says this month's budget goes in, never "your latest change there goes
    out"; and the next read sets it."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = 61_000  # the launch capped it at 60,000
    monkeypatch.setattr(ads_run, "now", lambda: datetime(2026, 11, 2, 17, tzinfo=UTC))
    assert sync(conn)["last_error"] == nb.MONTH_CHECK
    code, _said = _ready(capsys)
    (row,) = _audited(conn)
    assert code == 0 and row["result"] == {
        "cleared": "spend_check",
        "recorded": True,
        "total_cents": 61_000,
        "last_cap_cents": 60_000,
        "held": "month",
    }
    sentence = tools_account.sentence(row, None)
    assert sentence == (
        "An operator checked NewsBreak's spend figures, so this month's budget there is set within the hour"
    )
    assert _plain(sentence) and "change" not in sentence
    out = sync(conn)
    assert (out["state"], out["last_error"]) == ("live", None) and nb_live.caps[account_id] == 121_000


@pytest.mark.parametrize("total", [30_000, 60_600], ids=["well within the cap", "at NewsBreak's own overshoot"])
def test_a_cap_newsbreaks_total_is_within_is_never_changed(conn, nb_live, launched, capsys, total):  # noqa: F811 - the fixtures of the same name
    """The coordinator's answer 3: with NewsBreak's total within the last cap Admiry set and NewsBreak's own overshoot
    above it (30,000, or 60,600, the edge, on a 60,000 cap: no spend check stands), recording it as the cap would make
    a later, ordinary spend look like a mismatch; a leftover mail mark alone is dropped."""
    (account_id,) = nb_live.accounts
    nb_live.spent[account_id] = total
    marked = {**external(conn), "spend_check_mailed_at": NOW.isoformat()}
    registry.save_account(conn, BIZ, NB, mode="managed", status="active", external=marked)
    code, _said = _ready(capsys)
    held = external(conn)
    assert code == 0 and held["spending_cap_cents"] == 60_000 and "spend_check_mailed_at" not in held
    (row,) = _audited(conn)
    assert row["result"] == {
        "cleared": "spend_check",
        "recorded": False,
        "total_cents": total,
        "last_cap_cents": 60_000,
    }
    # fix round 1, item 4 (review M2): only the mark went, so nothing of the owner's was held -- its own sentence
    sentence = tools_account.sentence(row, None)
    assert sentence == "An operator checked NewsBreak's spend figures and found them in order" and _plain(sentence)


def test_newsbreak_not_answering_clears_no_spend_check(conn, nb_live, launched, resend, capsys):  # noqa: F811 - the fixtures of the same name
    """Without NewsBreak's total there is nothing to record: the stop and its mark stay, the command exits 1 and says
    why in NewsBreak's terms -- never with the token."""
    _spend_check(conn, nb_live)
    nb_live.down = True
    code, said = _ready(capsys)
    held = external(conn)
    assert code == 1 and held["spending_cap_cents"] == 60_000 and "spend_check_mailed_at" in held
    assert "unreachable" in said and not any(secret in said for secret in ADS_SECRETS)
    assert _audited(conn) == []


# ---------------------------------------------------------------- nothing to clear


def test_with_nothing_to_clear_it_says_so_and_changes_nothing(conn, nb_live, launched, capsys):  # noqa: F811 - the fixtures of the same name
    before = external(conn)
    code, said = _ready(capsys)
    assert code == 0 and "nothing" in said and external(conn) == before and _audited(conn) == []


def test_a_business_without_a_newsbreak_account_is_refused(conn, nbapi, capsys):
    conn.execute("INSERT INTO business (id) VALUES (%s)", (BIZ,))
    code, said = _ready(capsys)
    assert code == 1 and "no NewsBreak account" in said and nbapi.sent == []
