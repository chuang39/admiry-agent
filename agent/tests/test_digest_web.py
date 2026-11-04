"""The owner's controls over the digest (digest spec 2026-09-30 §9; plan 2026-10-07-agent-k2 Task 12, Dg Task 9): the
unsubscribe links (GET never changes anything; the one-click POST and the page's form only ever lower the frequency,
for their own business only), Settings' Reports card, resuming mail after a bounce, the report on the homepage."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from agent import config, digest, digest_links, jobs, tools_account
from agent.tests import digest_fixtures as df
from agent.tests.conftest import TEST_LINK_SECRET
from agent.tests.test_web import claimed_business, csrf_of

OTHER = "cd" * 32


def _token_url(business_id: str = "biz_1", f: str | None = None) -> str:
    return digest_links.unsubscribe_url(config.load(), business_id, f).replace("http://testserver", "")


@pytest.fixture
def linked(conn, monkeypatch):
    """A business with an owner and the link secret set (the digest itself may be off: a sent link keeps working)."""
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", TEST_LINK_SECRET)
    df.seed_business(conn, "biz_1")
    return "biz_1"


def _frequency(conn, biz: str = "biz_1") -> str:
    return conn.execute("SELECT digest_frequency FROM business WHERE id = %s", (biz,)).fetchone()["digest_frequency"]


def test_get_never_changes_state(client, conn, linked):
    url = _token_url(f="off")
    r = client.get(url)
    assert r.status_code == 200 and "<form" in r.text and 'name="f" value="off"' in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert client.head(url).status_code == 200
    assert _frequency(conn) == "daily"


def test_one_click_urlencoded_post_turns_reports_off_without_csrf(client, conn, linked):
    r = client.post(_token_url(), data={"List-Unsubscribe": "One-Click"})
    assert (r.status_code, r.text) == (200, "ok") and _frequency(conn) == "off"
    row = conn.execute("SELECT * FROM agent_action WHERE tool = 'system.digest_unsubscribe'").fetchone()
    assert row["business_id"] == "biz_1"
    assert tools_account.sentence(row, None) == "Reports were changed to no reports from the e-mail's link"


def test_one_click_multipart_post_turns_reports_off(client, conn, linked):
    r = client.post(_token_url(), files={"List-Unsubscribe": (None, "One-Click")})
    assert r.status_code == 200 and _frequency(conn) == "off"


def test_the_pages_form_says_what_changed(client, conn, linked):
    r = client.post(_token_url(f="weekly"), data={"f": "weekly"})
    assert r.status_code == 200 and "now gets weekly reports (Mondays)" in r.text and _frequency(conn) == "weekly"


def test_weekly_link_never_raises_an_off_business(client, conn, linked):
    conn.execute("UPDATE business SET digest_frequency = 'off'")
    client.post(_token_url(f="weekly"), data={"f": "weekly"})
    assert _frequency(conn) == "off"


def test_one_click_changes_only_its_own_business(client, conn, linked):
    df.seed_business(conn, "biz_2")
    client.post(_token_url("biz_1"), data={"List-Unsubscribe": "One-Click"})
    assert (_frequency(conn, "biz_1"), _frequency(conn, "biz_2")) == ("off", "daily")


def test_bad_token_foreign_business_and_retired_key_get_the_same_page(client, conn, linked, monkeypatch):
    df.seed_business(conn, "biz_2")
    good = _token_url("biz_1")
    token = urlparse(good).path.rsplit("/", 1)[1]
    bad = [
        good.replace(token, token[:-2] + "xx"),  # the mac changed
        f"/unsubscribe/{token}?b=biz_2",  # another business's id
    ]
    for url in bad:
        r = client.get(url)
        assert r.status_code == 404 and digest_web_dead() in r.text, url
        assert client.post(url, data={"List-Unsubscribe": "One-Click"}).status_code == 404
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", OTHER)  # rotated with no previous key: the old link is dead
    assert client.get(good).status_code == 404
    assert (_frequency(conn, "biz_1"), _frequency(conn, "biz_2")) == ("daily", "daily")


def digest_web_dead() -> str:
    from agent import digest_web

    return digest_web.LINK_DEAD.split(";")[0]


def test_previous_key_still_works(client, conn, linked, monkeypatch):
    url = _token_url()
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET", OTHER)
    monkeypatch.setenv("AGENT_DIGEST_LINK_SECRET_PREVIOUS", TEST_LINK_SECRET)
    assert client.post(url, data={"List-Unsubscribe": "One-Click"}).status_code == 200 and _frequency(conn) == "off"


def test_missing_secret_answers_the_dead_link_page(client, conn, linked, monkeypatch):
    url = _token_url()
    monkeypatch.delenv("AGENT_DIGEST_LINK_SECRET")
    r = client.get(url)
    assert r.status_code == 404 and digest_web_dead() in r.text


def test_one_click_then_the_next_run_skips(client, conn, digest_env, analyst_biz, monkeypatch):
    digest_env.fail = httpx.Response(500, json={"message": "boom"})
    with pytest.raises(jobs.JobFailed):
        digest.run(conn, config.load(), 0, 1)
    digest_env.fail = None
    assert client.post(_token_url(), data={"List-Unsubscribe": "One-Click"}).status_code == 200
    monkeypatch.setattr("agent.ads_run.now", lambda: datetime(2026, 10, 6, 16, 2, tzinfo=UTC))  # the next hourly run
    digest.run(conn, config.load(), 0, 1)
    assert conn.execute("SELECT status FROM digest").fetchone()["status"] == "skipped"


def _settings(client, conn, google):
    claimed_business(client, conn, google)
    return csrf_of(client)


def test_settings_post_sets_frequency_and_audits(client, conn, google):
    csrf = _settings(client, conn, google)
    r = client.post(
        "/connect/digest", data={"business_id": "biz_1", "frequency": "weekly", "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#reports"
    assert _frequency(conn) == "weekly"
    row = conn.execute("SELECT * FROM agent_action WHERE tool = 'web.digest_frequency'").fetchone()
    assert tools_account.sentence(row, None) == "The owner (web) set the e-mail report to weekly reports (Mondays)"


def test_settings_post_needs_csrf_and_ownership(client, conn, google):
    csrf = _settings(client, conn, google)
    assert client.post("/connect/digest", data={"business_id": "biz_1", "frequency": "off"}).status_code == 403
    df.seed_business(conn, "biz_9", owner="someone@else.com")
    r = client.post("/connect/digest", data={"business_id": "biz_9", "frequency": "off", "csrf": csrf})
    assert r.status_code == 404
    r = client.post("/connect/digest", data={"business_id": "biz_1", "frequency": "hourly", "csrf": csrf})
    assert r.status_code == 400
    assert (_frequency(conn, "biz_1"), _frequency(conn, "biz_9")) == ("daily", "daily")


def test_resume_after_bounce(client, conn, google):
    csrf = _settings(client, conn, google)
    conn.execute("UPDATE business SET notify_email_status = 'bounced'")
    r = client.post("/connect/digest/resume", data={"business_id": "biz_1", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#reports"
    status = conn.execute("SELECT notify_email_status FROM business").fetchone()["notify_email_status"]
    assert status == "ok"
    row = conn.execute("SELECT * FROM agent_action WHERE tool = 'web.email_status'").fetchone()
    assert "send e-mails to the address again" in tools_account.sentence(row, None)


def _nav(html: str) -> list[str]:
    import re

    nav = html.split('<nav class="section-nav" data-sections')[1].split("</nav>")[0]
    return re.findall(r'href="#([a-z]+)"', nav)


def test_reports_card_hidden_while_the_digest_is_off(client, conn, google):
    _settings(client, conn, google)
    html = client.get("/connect").text
    assert _nav(html) == ["overview", "billing", "assistants", "advanced", "account"] and 'id="reports"' not in html


def test_reports_card_when_the_digest_is_on(client, conn, google, digest_env):
    _settings(client, conn, google)
    html = client.get("/connect").text
    assert _nav(html) == ["overview", "billing", "assistants", "reports", "email", "advanced", "account"]
    assert 'name="frequency" value="daily" checked' in html


def test_reports_card_with_reports_off_turns_them_back_on(client, conn, google, digest_env):
    csrf = _settings(client, conn, google)
    conn.execute("UPDATE business SET digest_frequency = 'off'")
    assert 'name="frequency" value="off" checked' in client.get("/connect").text
    client.post("/connect/digest", data={"business_id": "biz_1", "frequency": "daily", "csrf": csrf})
    assert _frequency(conn) == "daily"


def test_reports_card_follows_the_business_flag(client, conn, google, digest_env, monkeypatch):
    _settings(client, conn, google)
    conn.execute("""UPDATE business SET operator_flags = '{"digest": "off"}'""")
    assert 'id="reports"' not in client.get("/connect").text
    monkeypatch.setenv("AGENT_DIGEST", "operator")
    conn.execute("""UPDATE business SET operator_flags = '{"digest": "on"}'""")
    assert 'id="reports"' in client.get("/connect").text


def test_reports_card_offers_resume_after_a_bounce(client, conn, google):
    _settings(client, conn, google)
    conn.execute("UPDATE business SET notify_email_status = 'bounced'")
    html = client.get("/connect").text
    card = html[html.index('id="reports"') :]
    assert "bounced" in card and 'action="/connect/digest/resume"' in card


def test_connect_keeps_business_and_section_through_sign_in(client, conn, google):
    r = client.get("/connect?b=biz_1&section=reports")
    href = r.text.split('href="/connect/google/start?next=')[1].split('"')[0]
    assert unquote(href) == "/connect?b=biz_1&section=reports"
    _settings(client, conn, google)
    r = client.get("/connect?b=biz_1&section=reports", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#reports"


def test_api_report_needs_the_owner_and_hides_operator_rows(client, conn, google, digest_env):
    r = client.get("/api/report?b=biz_1")
    assert r.status_code == 401 and r.json() == {"ok": False, "signin": True}
    _settings(client, conn, google)
    body = {"sentence": "Yesterday: $23.10 spent.", "rows": [], "items": [], "recommendations": [], "note": None}
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status, body) "
        "VALUES ('dg_op', 'biz_1', '2026-10-05', 'daily', 'America/Los_Angeles', 'operator', 'sent', %s)",
        (json.dumps(body),),
    )
    assert client.get("/api/report?b=biz_1&day=2026-10-05").status_code == 404
    conn.execute("UPDATE digest SET mode = 'on'")
    r = client.get("/api/report?b=biz_1&day=2026-10-05")
    assert r.status_code == 200 and r.json()["report"]["sentence"] == "Yesterday: $23.10 spent."
    assert r.json()["insight"] is None
    assert client.get("/api/report?b=biz_404").status_code == 404


def test_api_report_carries_the_owner_view_only_when_on(client, conn, google, digest_env, monkeypatch):
    _settings(client, conn, google)
    conn.execute(
        "INSERT INTO digest (id, business_id, day, edition, time_zone, mode, status, body) "
        "VALUES ('dg_1', 'biz_1', '2026-10-05', 'daily', 'America/Los_Angeles', 'on', 'sent', '{}')"
    )
    look = {"summary": "A quiet day.", "summary_dropped": None, "findings": [], "recommendations": []}
    conn.execute(
        "INSERT INTO insight (id, business_id, day, mode, status, insight, verdict) "
        "VALUES ('in_1', 'biz_1', '2026-10-05', 'on', 'done', %s, 'deliver')",
        (json.dumps(look),),
    )
    monkeypatch.setenv("AGENT_ROLES", "validate")
    assert client.get("/api/report?b=biz_1&day=2026-10-05").json()["insight"] is None
    monkeypatch.setenv("AGENT_ROLES", "on")
    assert client.get("/api/report?b=biz_1&day=2026-10-05").json()["insight"]["summary"] == "A quiet day."


def test_the_unsubscribe_page_names_the_business_and_the_change(client, conn, linked):
    r = client.get(_token_url(f="weekly"))
    q = parse_qs(urlparse(_token_url(f="weekly")).query)
    assert q["f"] == ["weekly"] and "weekly reports (Mondays)" in r.text
