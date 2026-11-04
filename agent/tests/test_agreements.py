"""The management agreement and the Google Ads settings (spec 2026-09-25 rev. 2 D28, §3, §4, §7): the legal file and
its page, click-wrap rows, the link-mode rules and the new schema."""

from pathlib import Path

import psycopg
import pytest

from agent import agreements, audit, claims, config, db, legal
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, call_tool, data
from agent.tests.fixtures import business_with_key
from agent.tests.test_claims import draft_business


def test_the_schema_adds_the_agreement_and_link_tables_and_ads_mode(conn):
    db.apply_schema(conn)  # a second application is a no-op
    conn.execute("INSERT INTO business (id) VALUES ('biz_1'), ('biz_2')")
    assert conn.execute("SELECT ads_mode FROM business WHERE id = 'biz_1'").fetchone()["ads_mode"] == "own_account"
    conn.execute("INSERT INTO ads_link (business_id, customer_id, status) VALUES ('biz_1', '1234567890', 'pending')")
    for cid, status in (("123456789", "pending"), ("1234567891", "linked")):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO ads_link (business_id, customer_id, status) VALUES ('biz_2', %s, %s)", (cid, status)
            )
    with pytest.raises(psycopg.errors.UniqueViolation):  # one business per Customer ID (D26)
        conn.execute(
            "INSERT INTO ads_link (business_id, customer_id, status) VALUES ('biz_2', '1234567890', 'pending')"
        )
    conn.execute(
        "INSERT INTO agreement_acceptance (business_id, email, version) VALUES ('biz_1', 'o@example.com', 'v0')"
    )


def test_legal_parse_reads_the_version_and_renders_escaped_html():
    version, html = legal.parse(
        "---\nversion: v9\n---\n# Title\n\nA <b>tag</b> and **bold**\nnext line\n\n- one\n- two\n"
    )
    assert version == "v9"
    assert html == (
        "<h1>Title</h1>\n<p>A &lt;b&gt;tag&lt;/b&gt; and <strong>bold</strong> next line</p>\n"
        "<ul><li>one</li><li>two</li></ul>"
    )


def test_legal_parse_refuses_a_file_without_a_usable_version():
    for text in ("# no front matter\n", "---\ntitle: x\n---\nbody\n", "---\nversion: two words\n---\nbody\n"):
        with pytest.raises(ValueError):
            legal.parse(text)


def test_the_shipped_agreement_is_v1_unmarked_with_every_required_part():
    version, html = legal.current()
    assert version == "v1" == agreements.CURRENT_VERSION
    assert "DRAFT" not in html and "[to be completed by counsel]" in html
    for part in (
        "Scope of authority",
        "Changes Admiry makes to keep your ads running",
        "$0 platform fee",
        "30 days",
        "without any penalty",
        "No guarantee of results",
        "Your data",
        "Ending this agreement and unlinking",
        "Governing law",
    ):
        assert part in html, part


def test_the_prepayment_addendum_names_the_payment_cost_and_re_asks(conn):
    assert agreements.version(legal.ADDENDUM) == "v0.2"
    _, html = legal.current(legal.ADDENDUM)
    assert "by card or by bank transfer" in html
    assert "Admiry adds nothing to it" in html and "is not refunded" in html
    assert "full amount you pay is added to your balance" not in html
    # an owner who accepted v0.1 is asked again
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    conn.execute(
        "INSERT INTO agreement_acceptance (business_id, document, version, email) "
        "VALUES ('biz_1', %s, 'v0.1', 'o@example.com')",
        (legal.ADDENDUM,),
    )
    assert not agreements.accepted(conn, "biz_1", legal.ADDENDUM)


def test_accept_records_the_owner_and_accepted_follows_the_current_version(conn, monkeypatch):
    draft_business(conn)
    claims.claim(conn, "biz_1", "owner@example.com")
    assert not agreements.accepted(conn, "biz_1")
    assert agreements.accept(conn, business_id="biz_1", email="Owner@Example.com", ip="203.0.113.5") == "v1"
    row = conn.execute("SELECT business_id, email, version, ip, accepted_at FROM agreement_acceptance").fetchone()
    assert (row["business_id"], row["email"], row["version"], row["ip"]) == (
        "biz_1",
        "owner@example.com",
        "v1",
        "203.0.113.5",
    )
    assert row["accepted_at"] is not None and agreements.accepted(conn, "biz_1")
    monkeypatch.setattr(agreements, "CURRENT_VERSION", "v2")  # counsel's version ships: ask again
    assert not agreements.accepted(conn, "biz_1")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    assert [r["version"] for r in conn.execute("SELECT version FROM agreement_acceptance ORDER BY id")] == [
        "v1",
        "v2",
    ]


def test_accepting_twice_keeps_one_row(conn):
    draft_business(conn)
    for _ in range(2):
        agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip="local")
    assert conn.execute("SELECT count(*) AS n FROM agreement_acceptance").fetchone()["n"] == 1


def test_the_agreement_page_is_public_noindex_and_plain(client):
    r = client.get("/legal/management-agreement")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["x-robots-tag"] == "noindex" and '<meta name="robots" content="noindex">' in r.text
    assert "Google Ads Management Agreement" in r.text and "Version v1" in r.text and "DRAFT" not in r.text
    assert (
        JARGON.findall(r.text) == []
        and ACCOUNT_JARGON.findall(r.text) == []
        and "manager account" not in r.text.lower()
    )
    assert "<form" not in r.text


def test_the_link_mode_is_manual_unless_every_google_ads_setting_is_there(monkeypatch, ads_api):
    assert ads_api.google_ads_link_mode == "api" and ads_api.google_ads_mcc_id == "1112223333"
    assert "devtok_SECRET" not in repr(ads_api) and "1//refresh_SECRET" not in repr(ads_api)
    assert "GOCSPX-x" not in repr(ads_api) and ads_api.google_client_secret == "GOCSPX-x"  # it mints MCC tokens too
    monkeypatch.setenv("GOOGLE_ADS_LINK_MODE", "manual")
    assert config.load().google_ads_link_mode == "manual"  # the operator can hold the api back
    monkeypatch.delenv("GOOGLE_ADS_LINK_MODE")
    for missing in ("GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_OAUTH_CLIENT_SECRET"):
        monkeypatch.setenv(missing, "")  # compose passes "" for an unset var
        assert config.load().google_ads_link_mode == "manual"


def test_config_refuses_an_unknown_mode_a_bad_manager_id_or_api_without_its_keys(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_LINK_MODE", "auto")
    with pytest.raises(ValueError, match="GOOGLE_ADS_LINK_MODE"):
        config.load()
    monkeypatch.setenv("GOOGLE_ADS_LINK_MODE", "api")  # nothing else set
    with pytest.raises(ValueError, match="needs GOOGLE_ADS_MCC_ID"):
        config.load()
    monkeypatch.delenv("GOOGLE_ADS_LINK_MODE")
    monkeypatch.setenv("GOOGLE_ADS_MCC_ID", "12345")
    with pytest.raises(ValueError, match="10-digit"):
        config.load()


def test_a_developer_token_never_reaches_an_audit_row(conn):
    assert audit.scrub({"developer_token": "devtok", "x": {"refresh_token": "r", "access_token": "a"}}) == {
        "developer_token": "***",
        "x": {"refresh_token": "***", "access_token": "***"},
    }
    audit.record_job(
        conn,
        job="ads-link-sync",
        tool="job.ads_link_active",
        business_id=None,
        args={"developer_token": "devtok"},
        result={"ok": 1},
    )
    row = conn.execute("SELECT actor, actor_class, api_key_id, args_redacted FROM agent_action").fetchone()
    assert dict(row) == {
        "actor": "job:ads-link-sync",
        "actor_class": "job",
        "api_key_id": None,
        "args_redacted": {"developer_token": "***"},
    }


WEBSITE_TERMS = Path(__file__).resolve().parents[2] / "website" / "app" / "terms" / "page.tsx"
# spec 2026-10-11 §5.2 (plan D11): the founder's draft, "a keyword" said as "a search phrase" for owners
CLAUSE = (
    "Google and other ad platforms check every ad against their own rules and sometimes refuse a headline, a "
    "description, a search phrase or a page. When that happens, Admiry may remove or rewrite the refused part, "
    "keeping the rest of your ads as you agreed, so they can run. Admiry never changes your budget, your area or "
    "your offer this way. Admiry tells you by e-mail each time it makes such a change, shows it on your plan, and "
    "keeps a record you or your assistant can read at any time. If you would rather Admiry hold your ads instead, "
    "tell Admiry and it will."
)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def test_the_agreement_and_the_websites_terms_carry_the_clause_word_for_word():
    _version, html = legal.current()
    assert "Changes Admiry makes to keep your ads running" in html and CLAUSE in _one_line(html)
    assert JARGON.findall(html) == []
    tsx = _one_line(WEBSITE_TERMS.read_text(encoding="utf-8"))
    assert "Changes Admiry makes to keep your ads running" in tsx and CLAUSE in tsx
    assert 'const UPDATED = "2026-10-11"' in tsx


def test_an_owner_who_accepted_v0_is_asked_again_before_anything_links(client, conn):
    """spec §5.2: a new agreement version re-asks every owner (the agreement's own clause on changes): an acceptance
    of v0.1 no longer counts, and connecting a Google Ads account answers needs_agreement until v1 is accepted."""
    key = business_with_key(conn, claim_state="claimed")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_1'")
    conn.execute(
        "INSERT INTO agreement_acceptance (business_id, document, version, email) VALUES ('biz_1', %s, 'v0.1', 'owner@example.com')",
        (legal.AGREEMENT,),
    )
    assert not agreements.accepted(conn, "biz_1")
    res = call_tool(client, "connect_account", {"account_id": "123-456-7890", "idempotency_key": "c1"}, key=key)
    assert not res["isError"] and data(res)["status"] == "needs_agreement"
    assert agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None) == "v1"
    assert agreements.accepted(conn, "biz_1")
    assert [r["version"] for r in conn.execute("SELECT version FROM agreement_acceptance ORDER BY id")] == [
        "v0.1",
        "v1",
    ]
