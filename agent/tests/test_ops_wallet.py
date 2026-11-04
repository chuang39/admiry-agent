"""Operator money commands (spec 2026-09-25 rev. 2 D30, §6; rev. 1 §4 "Refund / adjustment"): agent.ops refund
(whatever the business's mode: money can always go back) and agent.ops adjust (prepaid businesses only). Stripe is
the `stripe` fixture's mock; no network."""

from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from agent import business, claims, ops, stripe_client, wallet
from agent.tests.test_claims import draft_business
from agent.tests.test_wallet import fund, managed


def funded(conn):
    """biz_1 prepaid and paid twice: cs_1 $300 (a day ago, pi_1), then cs_2 $200 (pi_2)."""
    managed(conn)
    fund(conn, cents=30000, session_id="cs_1", payment_intent="pi_1")
    fund(conn, cents=20000, session_id="cs_2", payment_intent="pi_2")
    conn.execute("UPDATE payment_session SET paid_at = now() - interval '1 day' WHERE id = 'cs_1'")


def entries(conn, kind):
    return [
        dict(r) for r in conn.execute("SELECT amount_cents, ref FROM wallet_entry WHERE kind = %s ORDER BY id", (kind,))
    ]


def test_refund_refuses_more_than_the_balance_after_spend(conn, stripe, capsys):
    funded(conn)
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=30000)  # $200 left
    assert ops.main(["refund", "biz_1", "250"]) == 1
    assert "more than the balance $200.00" in capsys.readouterr().err
    assert stripe.sent == [] and entries(conn, "refund") == []


def test_refund_takes_the_most_recent_payments_first_one_stripe_refund_each(conn, stripe, capsys):
    funded(conn)
    assert ops.main(["refund", "biz_1", "300", "--note", "closing the account"]) == 0
    sent = [stripe.form(i) for i in range(2)]
    assert [(s["payment_intent"], s["amount"]) for s in sent] == [("pi_2", "20000"), ("pi_1", "10000")]
    assert [r.headers["idempotency-key"] for r in stripe.sent] == ["refund-cs_2-0", "refund-cs_1-0"]
    assert entries(conn, "refund") == [
        {"amount_cents": -20000, "ref": "re_test_1"},
        {"amount_cents": -10000, "ref": "re_test_2"},
    ]
    assert wallet.balance_cents(conn, "biz_1") == 20000
    refunded = {r["id"]: r["refunded_cents"] for r in conn.execute("SELECT id, refunded_cents FROM payment_session")}
    assert refunded == {"cs_1": 10000, "cs_2": 20000}
    rows = conn.execute(
        "SELECT actor, actor_class, result FROM agent_action WHERE tool = 'ops.refund' ORDER BY id"
    ).fetchall()
    assert [(r["actor"], r["actor_class"]) for r in rows] == [("ops:cli", "platform")] * 2
    assert [r["result"]["amount_usd"] for r in rows] == [200.0, 100.0]
    out = capsys.readouterr().out
    assert "re_test_1 $200.00 against cs_2 (succeeded)" in out and "re_test_2 $100.00 against cs_1" in out


def test_a_second_refund_uses_what_is_left(conn, stripe):
    funded(conn)
    assert ops.main(["refund", "biz_1", "300"]) == 0
    assert ops.main(["refund", "biz_1", "200"]) == 0
    assert (stripe.form(2)["payment_intent"], stripe.form(2)["amount"]) == ("pi_1", "20000")
    assert stripe.sent[2].headers["idempotency-key"] == "refund-cs_1-10000"
    assert wallet.balance_cents(conn, "biz_1") == 0
    assert ops.main(["refund", "biz_1", "1"]) == 1 and len(stripe.sent) == 3


def test_a_stripe_refusal_stops_the_refund_and_keeps_what_was_done(conn, stripe, capsys):
    funded(conn)
    stripe.fail_on = 2
    assert ops.main(["refund", "biz_1", "300"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Stripe refused")  # a 4xx is a refusal, not unconfirmed
    assert "This charge has already been refunded." in err and "$200.00 was refunded before it" in err
    assert entries(conn, "refund") == [{"amount_cents": -20000, "ref": "re_test_1"}]
    assert wallet.balance_cents(conn, "biz_1") == 30000


def test_a_stripe_5xx_during_a_refund_is_reported_as_unconfirmed(conn, stripe, capsys):
    funded(conn)
    stripe.fail = httpx.Response(500, json={"error": {"message": "An unknown error occurred", "type": "api_error"}})
    assert ops.main(["refund", "biz_1", "100"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("could not confirm the refund of $100.00 against cs_2: it may have gone through")
    assert "Run the command again for the $100.00 still to go" in err and "Stripe refused" not in err
    assert len(stripe.listed) == 2 and len(stripe.sent) == 1 and entries(conn, "refund") == []


def test_an_unconfirmed_refund_after_others_asks_only_for_what_is_still_to_go(conn, stripe, capsys, monkeypatch):
    """What was refunded before it is recorded, so the message asks for the rest: the whole amount again would
    refund that part a second time when the unconfirmed refund never reached Stripe."""
    funded(conn)
    real, calls = stripe_client.create_refund, []

    def second_never_arrives(http, **kwargs):
        calls.append(kwargs["payment_intent"])
        if len(calls) == 2:
            raise stripe_client.StripeUnreachable("could not reach Stripe (ConnectError)")
        return real(http, **kwargs)

    monkeypatch.setattr(stripe_client, "create_refund", second_never_arrives)
    assert ops.main(["refund", "biz_1", "300"]) == 1  # cs_2 $200 done, cs_1 $100 unconfirmed
    err = capsys.readouterr().err
    assert err.startswith("could not confirm the refund of $100.00 against cs_1")
    assert "Run the command again for the $100.00 still to go" in err and "$200.00 was refunded before it" in err
    assert ops.main(["refund", "biz_1", "100"]) == 0  # nothing at Stripe: refunded now
    assert [e["amount_cents"] for e in entries(conn, "refund")] == [-20000, -10000]
    assert wallet.balance_cents(conn, "biz_1") == 20000  # $300 in all, as asked


def test_a_refund_made_in_the_stripe_dashboard_is_recorded_before_anything_new(conn, stripe, capsys):
    funded(conn)
    stripe.refunds["pi_1"] = [{"id": "re_dash", "amount": 5000, "status": "succeeded"}]
    stripe.refunds["pi_2"] = [{"id": "re_failed", "amount": 3000, "status": "failed"}]  # gave nothing back
    assert ops.main(["refund", "biz_1", "100"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("recorded re_dash that Stripe had already made") and "nothing new was refunded" in err
    assert "balance now $450.00" in err
    assert stripe.sent == [] and len(stripe.listed) == 2
    assert entries(conn, "refund") == [{"amount_cents": -5000, "ref": "re_dash"}]
    refunded = {r["id"]: r["refunded_cents"] for r in conn.execute("SELECT id, refunded_cents FROM payment_session")}
    assert refunded == {"cs_1": 5000, "cs_2": 0}
    rows = conn.execute(
        "SELECT actor, actor_class, args_redacted, result FROM agent_action WHERE tool = 'ops.refund'"
    ).fetchall()
    assert [(r["actor"], r["actor_class"]) for r in rows] == [("ops:cli", "platform")]
    assert rows[0]["args_redacted"] == {"business_id": "biz_1", "session_id": "cs_1", "amount_usd": "50.0"}  # redacted
    assert rows[0]["result"] == {
        "refund_id": "re_dash",
        "amount_usd": 50.0,
        "status": "succeeded",
        "recorded_from_stripe": True,
    }
    assert ops.main(["refund", "biz_1", "100"]) == 0 and len(stripe.sent) == 1  # resolved: the next run refunds


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("boom"), httpx.Response(503, text="<html>down</html>")], ids=["unreachable", "5xx"]
)
def test_refund_needs_a_stripe_check_that_works(conn, stripe, capsys, failure):
    funded(conn)
    stripe.list_fail = failure
    assert ops.main(["refund", "biz_1", "100"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("could not check Stripe for earlier refunds (") and err.rstrip().endswith(
        "nothing was refunded"
    )
    assert stripe.listed and stripe.sent == [] and entries(conn, "refund") == []


def test_refund_works_whatever_the_mode(conn, stripe):
    draft_business(conn, "biz_own")
    claims.claim(conn, "biz_own", "owner@example.com")  # own_account, the default: never prepaid
    fund(conn, "biz_own", cents=30000, session_id="cs_own", payment_intent="pi_own")  # a payment credited anyway
    assert business.row(conn, "biz_own")["ads_mode"] == "own_account"
    assert ops.main(["refund", "biz_own", "100"]) == 0
    assert (stripe.form()["payment_intent"], stripe.form()["amount"]) == ("pi_own", "10000")
    assert entries(conn, "refund") == [{"amount_cents": -10000, "ref": "re_test_1"}]
    assert wallet.balance_cents(conn, "biz_own") == 20000


def test_refund_needs_stripe_and_money_that_was_paid(conn, stripe, capsys, monkeypatch):
    managed(conn)
    wallet.adjust(conn, "biz_1", 10000, note="goodwill credit")  # $100 that no card paid
    assert ops.main(["refund", "biz_1", "50"]) == 1
    assert "only $0.00 was credited from payments" in capsys.readouterr().err
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    assert ops.main(["refund", "biz_1", "50"]) == 1
    assert "STRIPE_SECRET_KEY" in capsys.readouterr().err and stripe.sent == []


def stripe_model(monkeypatch, paid: dict[str, int], lose_first_on: str) -> SimpleNamespace:
    """A Stripe that remembers: a refund is made once per Idempotency-Key (the same key with other parameters is
    Stripe's idempotency error), never beyond what its payment took, and listed per payment newest first; the
    answer to the first refund on `lose_first_on` is lost after Stripe made it. `made` = the refunds Stripe made,
    in order; `posts` = every refund request sent."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    model = SimpleNamespace(made=[], posts=[], seen={}, lost=False)

    def handler(request):
        if request.method == "GET":
            pi = request.url.params["payment_intent"]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "has_more": False,
                    "data": [r for r in reversed(model.made) if r["payment_intent"] == pi],
                },
            )
        model.posts.append(request)
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        key = request.headers["idempotency-key"]
        if key in model.seen:
            if params != model.seen[key][0]:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Keys for idempotent requests can only be used with the same parameters they "
                            "were first used with.",
                            "type": "idempotency_error",
                        }
                    },
                )
            return httpx.Response(200, json=model.seen[key][1])
        pi, amount = params["payment_intent"], int(params["amount"])
        if amount > paid[pi] - sum(r["amount"] for r in model.made if r["payment_intent"] == pi):
            return httpx.Response(400, json={"error": {"message": "Refund amount is greater than unrefunded amount."}})
        refund = {"id": f"re_{len(model.made) + 1}", "amount": amount, "payment_intent": pi, "status": "succeeded"}
        model.made.append(refund)
        model.seen[key] = (params, refund)
        if pi == lose_first_on and not model.lost:
            model.lost = True
            raise httpx.ReadTimeout("the answer was lost")
        return httpx.Response(200, json=refund)

    monkeypatch.setattr(stripe_client, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return model


def test_a_lost_answer_is_recorded_by_the_next_run_even_after_a_top_up_never_refunded_twice(conn, monkeypatch, capsys):
    """The final review's case: Stripe makes a refund whose answer is lost, a top-up lands, the operator runs the
    command again. The newest payment now leads the plan under a fresh key, so without the check first a second,
    unrecorded refund would go out; with it the run records Stripe's refund and refunds nothing new."""
    funded(conn)
    stripe = stripe_model(monkeypatch, {"pi_1": 30000, "pi_2": 20000, "pi_3": 20000}, lose_first_on="pi_2")
    assert ops.main(["refund", "biz_1", "100"]) == 1  # made on pi_2; the answer is lost
    assert capsys.readouterr().err.startswith("could not confirm")
    assert entries(conn, "refund") == [] and len(stripe.made) == 1
    fund(conn, cents=20000, session_id="cs_3", payment_intent="pi_3")  # a top-up lands meanwhile
    assert ops.main(["refund", "biz_1", "100"]) == 1
    err = capsys.readouterr().err
    assert "recorded re_1" in err and "nothing new was refunded" in err and "balance now $600.00" in err
    assert len(stripe.posts) == 1 and len(stripe.made) == 1  # no second refund was sent
    assert entries(conn, "refund") == [{"amount_cents": -10000, "ref": "re_1"}]
    refunded = {r["id"]: r["refunded_cents"] for r in conn.execute("SELECT id, refunded_cents FROM payment_session")}
    assert refunded == {"cs_1": 0, "cs_2": 10000, "cs_3": 0}
    assert ops.main(["refund", "biz_1", "100"]) == 0  # a deliberate new refund
    assert len(stripe.posts) == 2 and stripe.made[1]["payment_intent"] == "pi_3"
    assert [(-e["amount_cents"], e["ref"]) for e in entries(conn, "refund")] == [
        (r["amount"], r["id"]) for r in stripe.made
    ]
    assert wallet.balance_cents(conn, "biz_1") == 50000


def test_adjust_records_a_signed_correction_with_its_note_and_can_lift_the_pause(conn, capsys):
    managed(conn)
    wallet.spend(conn, "biz_1", channel="google_search", day=date(2026, 9, 24), cost_cents=1000)  # balance -$10
    conn.execute("UPDATE business SET paused_reason = 'no_funds' WHERE id = 'biz_1'")
    assert ops.main(["adjust", "biz_1", "-25.50", "--note", "late Google credit"]) == 0
    assert business.row(conn, "biz_1")["paused_reason"] == "no_funds"
    assert ops.main(["adjust", "biz_1", "50", "--note", "goodwill credit"]) == 0
    assert wallet.balance_cents(conn, "biz_1") == -1000 - 2550 + 5000
    assert business.row(conn, "biz_1")["paused_reason"] is None
    rows = conn.execute(
        "SELECT amount_cents, note, created_by FROM wallet_entry WHERE kind = 'adjustment' ORDER BY id"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {"amount_cents": -2550, "note": "late Google credit", "created_by": "ops"},
        {"amount_cents": 5000, "note": "goodwill credit", "created_by": "ops"},
    ]
    audited = conn.execute("SELECT result FROM agent_action WHERE tool = 'ops.adjust' ORDER BY id").fetchall()
    assert [a["result"]["amount_usd"] for a in audited] == [-25.5, 50.0]
    assert "balance $14.50" in capsys.readouterr().err


def test_adjust_is_for_prepaid_businesses_only(conn, capsys):
    draft_business(conn, "biz_own")
    claims.claim(conn, "biz_own", "owner@example.com")  # own_account, the default
    assert ops.main(["adjust", "biz_own", "50", "--note", "goodwill credit"]) == 1
    assert "has no balance" in capsys.readouterr().err
    assert conn.execute("SELECT count(*) AS n FROM wallet_entry").fetchone()["n"] == 0


def test_money_commands_refuse_bad_input(conn, stripe):
    managed(conn)
    for argv in (
        ["adjust", "biz_1", "10", "--note", "  "],
        ["adjust", "biz_1", "0", "--note", "x"],
        ["adjust", "biz_1", "12.345", "--note", "x"],
        ["adjust", "biz_nope", "10", "--note", "x"],
        ["refund", "biz_1", "-5"],
        ["refund", "biz_1", "0"],
        ["refund", "biz_1", "abc"],
    ):
        assert ops.main(argv) == 1, argv
    with pytest.raises(SystemExit):
        ops.main(["adjust", "biz_1", "10"])  # --note is required
    assert conn.execute("SELECT count(*) AS n FROM wallet_entry").fetchone()["n"] == 0 and stripe.sent == []
