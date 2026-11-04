"""ads-link-sync (hourly, sharded) and the operator commands ads-link / google-ads-auth (spec 2026-09-25 rev. 2 §2, §5)."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from agent import agreements, business, google_ads_link, google_auth, jobs, ops, tick
from agent.channels.google_search import client as google_ads
from agent.channels.google_search import conversions as ads_conversions
from agent.google_ads_link import LinkError, Remote
from agent.tests.conftest import ADS_ENV, TEST_DB_URL
from agent.tests.test_google_ads_link import CID, RESOURCE, claimed_agreed, pending_link

EMPTY = {"checked": 0, "activated": 0, "errors": 0}


def reads(monkeypatch, answer):
    """Stand in for Google's status read: answer(customer_id) -> Remote, or raises LinkError. Returns the calls."""
    calls = []

    def fake(settings, customer_id, *, http=None):
        calls.append(customer_id)
        return answer(customer_id)

    monkeypatch.setattr(jobs.google_ads_link, "read", fake)
    return calls


def test_ads_link_sync_verifies_a_pending_link_once_it_is_accepted(conn, ads_api, monkeypatch):
    pending_link(conn)
    reads(monkeypatch, lambda cid: Remote("active", RESOURCE))
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", "checked": 1, "activated": 1, "errors": 0}
    row = business.row(conn, "biz_1")
    assert (row["claim_state"], row["verified_via"]) == ("verified", "ads_link")
    audited = conn.execute(
        "SELECT actor, actor_class, result FROM agent_action WHERE tool = 'job.ads_link_active'"
    ).fetchall()
    assert [dict(a) for a in audited] == [
        {
            "actor": "job:ads-link-sync",
            "actor_class": "job",
            "result": {"customer_id": "123-456-7890", "verified_now": True},
        }
    ]
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", **EMPTY}  # an active row is not read again
    assert conn.execute("SELECT ok FROM job_run ORDER BY id").fetchall() == [{"ok": True}, {"ok": True}]


def test_ads_link_sync_reads_only_pending_rows_of_its_own_shard(conn, ads_api, monkeypatch):
    for biz, cid in (("biz_a", "1000000001"), ("biz_b", "1000000002")):
        pending_link(conn, biz, cid, email=f"{biz}@example.com")
    for biz, cid, status in (("biz_c", "1000000003", "active"), ("biz_d", "1000000004", "refused")):
        claimed_agreed(conn, biz, email=f"{biz}@example.com")
        google_ads_link.save(conn, biz, cid, status=status)
    calls = reads(monkeypatch, lambda cid: Remote("pending", None))
    per_shard = [tick.run("ads-link-sync", shard=f"{i}/2", url=TEST_DB_URL)["checked"] for i in range(2)]
    assert sum(per_shard) == 2 and sorted(calls) == ["1000000001", "1000000002"]
    owner = {
        r["b"]: r["s"]
        for r in conn.execute(
            "SELECT b, mod(abs(hashtext(b)::bigint), 2) AS s FROM unnest(ARRAY['biz_a', 'biz_b']) AS b"
        )
    }
    assert per_shard == [sum(1 for s in owner.values() if s == i) for i in range(2)]


def test_ads_link_sync_in_manual_mode_calls_nothing(conn, monkeypatch):
    pending_link(conn)
    calls = reads(monkeypatch, lambda cid: pytest.fail("manual mode read Google"))
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "manual", **EMPTY}
    assert calls == []


def test_ads_link_sync_records_a_google_failure_and_carries_on(conn, ads_api, monkeypatch):
    pending_link(conn, "biz_a", "1000000001", email="a@example.com")
    pending_link(conn, "biz_b", "1000000002", email="b@example.com")

    def answer(cid):
        if cid == "1000000001":
            raise LinkError(google_ads_link.BUSY)
        return Remote("refused", None)

    reads(monkeypatch, answer)
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", "checked": 1, "activated": 0, "errors": 1}
    a, b = google_ads_link.get(conn, "biz_a"), google_ads_link.get(conn, "biz_b")
    assert (a.status, a.last_error) == ("pending", google_ads_link.BUSY) and b.status == "refused"
    assert business.row(conn, "biz_b")["claim_state"] == "claimed"


def test_ads_link_sync_isolates_a_row_that_fails_unexpectedly(conn, ads_api, monkeypatch, caplog):
    pending_link(conn, "biz_a", "1000000001", email="a@example.com")
    pending_link(conn, "biz_b", "1000000002", email="b@example.com")
    reads(monkeypatch, lambda cid: Remote("active", None))
    real = google_ads_link.apply

    def apply(conn_, business_id, customer_id, remote):
        if business_id == "biz_a":
            raise RuntimeError("boom with ya29.SECRET")
        return real(conn_, business_id, customer_id, remote)

    monkeypatch.setattr(jobs.google_ads_link, "apply", apply)
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", "checked": 1, "activated": 1, "errors": 1}
    assert (
        google_ads_link.get(conn, "biz_a").status == "pending"
        and business.row(conn, "biz_a")["claim_state"] == "claimed"
    )
    assert business.row(conn, "biz_b")["claim_state"] == "verified"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "biz_a" in logged and "SECRET" not in logged and "boom" not in logged


def test_a_link_an_operator_registered_as_pending_is_read_after_the_switch_to_api(conn, monkeypatch):
    claimed_agreed(conn)
    assert ops.main(["ads-link", "biz_1", "123-456-7890", "--status", "pending"]) == 0  # manual mode, no Google
    for name, value in ADS_ENV.items():  # the developer token got Basic access
        monkeypatch.setenv(name, value)
    reads(monkeypatch, lambda cid: Remote("active", RESOURCE))
    assert tick.run("ads-link-sync", url=TEST_DB_URL)["activated"] == 1
    link = google_ads_link.get(conn, "biz_1")
    assert (link.status, link.resource_name) == ("active", RESOURCE)
    assert business.row(conn, "biz_1")["claim_state"] == "verified"


def test_ops_ads_link_active_verifies_as_an_operator_registered_link(conn, capsys):
    claimed_agreed(conn)
    assert ops.main(["ads-link", "biz_1", "123-456-7890", "--status", "active"]) == 0
    link = google_ads_link.get(conn, "biz_1")
    assert (link.customer_id, link.status, link.owner_status) == (CID, "manual", "active")
    row = business.row(conn, "biz_1")
    assert (row["claim_state"], row["verified_via"]) == ("verified", "ads_link")
    audited = conn.execute("SELECT actor, actor_class, result FROM agent_action WHERE tool = 'ops.ads_link'").fetchone()
    assert (audited["actor"], audited["actor_class"]) == ("ops:cli", "platform")
    assert audited["result"] == {"customer_id": "123-456-7890", "status": "active", "verified_now": True}
    assert "verified" in capsys.readouterr().err


# ---------------------------------------------------------------- A3 Task 2: conversion actions on activation


def test_ads_link_sync_calls_ads_conversions_ensure_when_a_link_becomes_active(conn, ads_api, monkeypatch):
    pending_link(conn)
    reads(monkeypatch, lambda cid: Remote("active", RESOURCE))
    calls = []
    monkeypatch.setattr(ads_conversions, "ensure", lambda conn_, settings, biz, cid, **kw: calls.append((biz, cid)))
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", "checked": 1, "activated": 1, "errors": 0}
    assert calls == [("biz_1", CID)]


def test_ads_link_sync_does_not_call_ensure_when_the_link_stays_pending(conn, ads_api, monkeypatch):
    pending_link(conn)
    reads(monkeypatch, lambda cid: Remote("pending", None))
    monkeypatch.setattr(ads_conversions, "ensure", lambda *a, **k: pytest.fail("ensure called"))
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", "checked": 1, "activated": 0, "errors": 0}


def test_ads_link_sync_logs_a_conversion_setup_failure_without_failing_the_row(conn, ads_api, monkeypatch, caplog):
    pending_link(conn)
    reads(monkeypatch, lambda cid: Remote("active", RESOURCE))

    def boom(conn_, settings, biz, cid, **kw):
        raise google_ads.AdsError(google_ads.BUSY)

    monkeypatch.setattr(ads_conversions, "ensure", boom)
    assert tick.run("ads-link-sync", url=TEST_DB_URL) == {"mode": "api", "checked": 1, "activated": 1, "errors": 0}
    assert business.row(conn, "biz_1")["claim_state"] == "verified"  # the link stays active despite the failure
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "biz_1" in logged and google_ads.BUSY in logged


def test_ads_link_sync_passes_the_open_http_session_to_ensure(conn, ads_api, monkeypatch):
    """`ensure` gets the same http client the row's own read used (one session per shard run, not a second one)."""
    pending_link(conn)
    reads(monkeypatch, lambda cid: Remote("active", RESOURCE))
    seen = []
    monkeypatch.setattr(ads_conversions, "ensure", lambda conn_, settings, biz, cid, *, http=None: seen.append(http))
    tick.run("ads-link-sync", url=TEST_DB_URL)
    assert len(seen) == 1 and seen[0] is not None


def test_ops_ads_link_pending_and_refused_never_verify(conn):
    claimed_agreed(conn)
    for status in ("pending", "refused"):
        assert ops.main(["ads-link", "biz_1", CID, "--status", status]) == 0
        assert google_ads_link.get(conn, "biz_1").status == status
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_ops_ads_link_refuses_bad_numbers_taken_numbers_no_agreement_and_unknown_businesses(conn, capsys):
    pending_link(conn, "biz_other", email="other@example.com")
    claimed_agreed(conn, "biz_1", agree=False)
    for argv, text in (
        (["ads-link", "biz_1", "12345", "--status", "active"], "10 digits"),
        (["ads-link", "biz_1", CID, "--status", "active"], "agreement"),
        (["ads-link", "biz_nope", CID, "--status", "active"], "no business"),
    ):
        assert ops.main(argv) == 1, argv
        assert text in capsys.readouterr().err
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    assert ops.main(["ads-link", "biz_1", CID, "--status", "active"]) == 1
    assert "already linked to another business" in capsys.readouterr().err
    assert google_ads_link.get(conn, "biz_1") is None and business.row(conn, "biz_1")["claim_state"] == "claimed"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.ads_link'").fetchone()["n"] == 0


def test_ops_ads_link_refuses_a_retired_business(conn, capsys):
    claimed_agreed(conn)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_1'")
    assert ops.main(["ads-link", "biz_1", CID, "--status", "active"]) == 1
    assert "not active" in capsys.readouterr().err
    assert google_ads_link.get(conn, "biz_1") is None
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.ads_link'").fetchone()["n"] == 0


def test_ops_ads_link_changes_a_linked_number_only_with_replace(conn, capsys):
    pending_link(conn)
    assert ops.main(["ads-link", "biz_1", "098-765-4321", "--status", "active"]) == 1
    assert "this business is linked to 123-456-7890; pass --replace to change it" in capsys.readouterr().err
    assert google_ads_link.get(conn, "biz_1").customer_id == CID
    assert ops.main(["ads-link", "biz_1", CID, "--status", "active"]) == 0  # the same number needs no flag
    assert ops.main(["ads-link", "biz_1", "098-765-4321", "--status", "active", "--replace"]) == 0
    assert google_ads_link.get(conn, "biz_1").customer_id == "0987654321"


def test_google_ads_auth_prints_the_refresh_token_and_stores_nothing(conn, capsys, google):
    sent, printed = [], {}

    def handler(request):
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.x",
                "refresh_token": "1//0gREFRESH",
                "expires_in": 3599,
                "scope": ops.ADS_SCOPE,
                "token_type": "Bearer",
            },
        )

    def paste():
        url = next(w for w in capsys.readouterr().err.split() if w.startswith("https://accounts.google.com/"))
        printed["q"] = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        return f"http://localhost:8097/connect/google/callback?state={printed['q']['state']}&code=4%2Fads&scope=x"

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert ops._google_ads_auth(read=paste, http=http) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "1//0gREFRESH" and "1//0gREFRESH" not in err and "GOOGLE_ADS_REFRESH_TOKEN" in err
    q = printed["q"]
    assert (q["scope"], q["access_type"], q["prompt"], q["redirect_uri"]) == (
        ops.ADS_SCOPE,
        "offline",
        "consent",
        ops.ADS_AUTH_REDIRECT,
    )
    form = parse_qs(sent[0].content.decode())
    assert str(sent[0].url) == google_auth.TOKEN_URL and form["code"] == ["4/ads"]
    assert form["grant_type"] == ["authorization_code"] and form["code_verifier"][0]
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


def test_google_ads_auth_refuses_another_sign_ins_address_and_a_missing_client(conn, capsys, monkeypatch, google):
    def unreachable(request):
        raise AssertionError("exchanged a code from another sign-in")

    http = httpx.Client(transport=httpx.MockTransport(unreachable))
    pasted = "http://localhost:8097/connect/google/callback?state=other&code=4%2Fx"
    assert ops._google_ads_auth(read=lambda: pasted, http=http) == 1
    assert "another sign-in" in capsys.readouterr().err
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    assert ops._google_ads_auth(read=lambda: pytest.fail("asked for a code"), http=http) == 1
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in capsys.readouterr().err


def test_the_offline_authorize_url_asks_for_consent_and_the_ads_scope():
    url = google_auth.authorize_url(
        client_id="c", redirect_uri=ops.ADS_AUTH_REDIRECT, state="s", challenge="ch", scope=ops.ADS_SCOPE, offline=True
    )
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert (q["scope"], q["access_type"], q["prompt"], q["redirect_uri"]) == (
        "https://www.googleapis.com/auth/adwords",
        "offline",
        "consent",
        "http://localhost:8097/connect/google/callback",
    )
