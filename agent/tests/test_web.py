"""/connect: sign-in round trip (Google mocked), the claim, keys shown once, the connection light, sign-out."""

import re
from urllib.parse import parse_qs, urlparse

import pytest

from agent import business, google_auth, keys, oauth, sessions, web
from agent.tests.conftest import JARGON, call_tool, rpc_raw, without_site_header
from agent.tests.test_claims import draft_business

# a key's (or an assistant's) row on Settings (spec 2026-09-29-admiry-settings-redesign §4.5)
ROW = '<p class="row-title">%s</p>'
# the connection light, gray (spec §4.1): no assistant checked in lately, which is not "not connected"
GRAY_LIGHT = "No assistant has checked in in the last 10 minutes."


def sign_in(client, next_path="/connect"):
    """Drive the round trip: start → (Google, mocked) → callback; returns the callback response."""
    r = client.get(f"/connect/google/start?next={next_path}", follow_redirects=False)
    assert r.status_code == 302, r.text
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return client.get(f"/connect/google/callback?state={state}&code=4%2Fabc", follow_redirects=False)


def csrf_of(client) -> str:
    html = client.get("/connect").text
    return html.split('name="csrf" value="')[1].split('"')[0]


def test_anonymous_connect_offers_google_sign_in_and_nothing_else(client, google):
    r = client.get("/connect")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert '<meta name="robots" content="noindex">' in r.text and 'href="/connect/google/start?next=/connect"' in r.text
    assert "ak_" not in r.text and JARGON.findall(r.text) == []
    r = client.get("/connect?claim=tok")
    assert 'href="/connect/google/start?next=/connect%3Fclaim%3Dtok"' in r.text  # the next path travels encoded


def test_sign_in_is_refused_when_google_is_not_configured(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    assert "not configured" in client.get("/connect").text
    assert client.get("/connect/google/start", follow_redirects=False).status_code == 503


def test_start_sends_the_owner_to_google_with_pkce_state_and_our_callback(client, conn, google):
    r = client.get("/connect/google/start?next=/plan/pl_0123456789abcdef", follow_redirects=False)
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com" and q["client_id"] == "123.apps.googleusercontent.com"
    assert q["redirect_uri"] == "http://testserver/connect/google/callback" and q["code_challenge_method"] == "S256"
    row = conn.execute("SELECT state, next_path FROM login_attempt").fetchone()
    assert row["state"] == q["state"] and row["next_path"] == "/plan/pl_0123456789abcdef"


def test_too_many_sign_in_starts_from_one_address_are_429(client, google):
    for _ in range(20):
        assert client.get("/connect/google/start", follow_redirects=False).status_code == 302
    r = client.get("/connect/google/start", follow_redirects=False)
    assert r.status_code == 429 and "try again in a minute" in r.text


def test_callback_signs_the_owner_in_with_a_hardened_cookie_and_an_audit_row(client, conn, google):
    r = sign_in(client, "/plan/pl_0123456789abcdef")
    assert r.status_code == 302 and r.headers["location"] == "/plan/pl_0123456789abcdef"
    cookie = r.headers["set-cookie"]
    assert (
        cookie.startswith("admiry_session=")
        and "HttpOnly" in cookie
        and "SameSite=lax" in cookie
        and "Secure" not in cookie
    )
    assert (
        google.calls[0]["redirect_uri"] == "http://testserver/connect/google/callback"
        and google.calls[0]["code"] == "4/abc"
    )
    assert conn.execute("SELECT email FROM web_session").fetchone()["email"] == "owner@example.com"
    row = conn.execute("SELECT actor, actor_class, tool, business_id FROM agent_action").fetchone()
    assert row["actor"] == "web:owner@example.com" and row["actor_class"] == "business" and row["tool"] == "web.sign_in"
    assert client.get("/connect").text.count("owner@example.com") >= 1  # signed in


def test_callback_refuses_bad_states_cancellations_and_google_failures(client, google, monkeypatch):
    assert client.get("/connect/google/callback?state=nope&code=x", follow_redirects=False).status_code == 400
    r = client.get("/connect/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get(f"/connect/google/callback?state={state}&error=access_denied", follow_redirects=False)
    assert r.status_code == 400 and "cancelled" in r.text and "admiry_session=" not in r.headers.get("set-cookie", "")
    assert (
        client.get(f"/connect/google/callback?state={state}&code=x", follow_redirects=False).status_code == 400
    )  # used

    def failing(client_, **kw):
        raise google_auth.SignInError("Google did not accept the sign-in; start again from the link")

    monkeypatch.setattr(web.google_auth, "sign_in", failing)
    r = sign_in(client)
    assert (
        r.status_code == 502 and "did not accept" in r.text and "admiry_session=" not in r.headers.get("set-cookie", "")
    )


def test_callback_refuses_a_state_presented_by_another_browser(client, conn, google):
    r = client.get("/connect/google/start", follow_redirects=False)
    login = r.headers["set-cookie"]
    assert login.startswith("admiry_login=") and "HttpOnly" in login and "SameSite=lax" in login
    assert "Path=/connect/google" in login and f"Max-Age={int(sessions.LOGIN_TTL.total_seconds())}" in login
    nonce = login.split("admiry_login=")[1].split(";")[0]
    assert conn.execute("SELECT login_hash FROM login_attempt").fetchone()["login_hash"] == sessions._hash(nonce)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.cookies.clear()  # the victim's browser: no login cookie
    r = client.get(f"/connect/google/callback?state={state}&code=4%2Fabc", follow_redirects=False)
    assert r.status_code == 400 and "admiry_session=" not in r.headers.get("set-cookie", "")
    first = client.get("/connect/google/start", follow_redirects=False)
    client.get("/connect/google/start", follow_redirects=False)  # its cookie replaces the first one's
    state = parse_qs(urlparse(first.headers["location"]).query)["state"][0]
    r = client.get(f"/connect/google/callback?state={state}&code=4%2Fabc", follow_redirects=False)
    assert r.status_code == 400 and "admiry_session=" not in r.headers.get("set-cookie", "")
    assert conn.execute("SELECT count(*) AS n FROM web_session").fetchone()["n"] == 0 and google.calls == []


def test_the_callback_clears_the_login_cookie_on_every_outcome(client, google):
    r = sign_in(client)
    cookies = r.headers.get_list("set-cookie")
    assert r.status_code == 302 and any(c.startswith("admiry_session=") for c in cookies)
    assert any(c.startswith("admiry_login=") and "Max-Age=0" in c and "Path=/connect/google" in c for c in cookies)
    r = client.get("/connect/google/callback?state=nope&code=x", follow_redirects=False)
    assert r.status_code == 400 and any(
        c.startswith("admiry_login=") and "Max-Age=0" in c for c in r.headers.get_list("set-cookie")
    )


def test_open_redirects_are_not_followed(client, google):
    r = sign_in(client, "https://evil.example/")
    assert r.headers["location"] == "/connect"


def test_a_claim_link_asks_first_then_binds_the_draft_to_the_signed_in_owner(client, conn, google):
    plaintext, _key_id = draft_business(conn)
    from agent import claims

    url, _ = claims.new_link(conn, "biz_1")
    token = url.split("claim=")[1]
    sign_in(client, f"/connect?claim={token}")
    r = client.get(f"/connect?claim={token}")  # GET only asks
    assert r.status_code == 200 and "Bright Smile Dental" in r.text and "owner@example.com" in r.text
    assert 'action="/connect/claim"' in r.text and f'name="claim" value="{token}"' in r.text and 'name="csrf"' in r.text
    assert "Claim this business" in r.text and "Not me" in r.text and JARGON.findall(without_site_header(r.text)) == []
    row = business.row(conn, "biz_1")
    assert row["claim_state"] == "draft" and row["owner_email"] is None and claims.take(conn, token) is not None
    r = client.post("/connect/claim", data={"claim": token, "csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["cache-control"] == "no-store"
    r = client.get(r.headers["location"])
    assert "now yours" in r.text and "Bright Smile Dental" in r.text
    row = business.row(conn, "biz_1")
    assert (
        row["owner_email"] == "owner@example.com"
        and row["claim_state"] == "claimed"
        and row["slug"] == "bright-smile-dental"
    )
    assert keys.lookup(conn, plaintext).cls == "business"
    audited = conn.execute(
        "SELECT args_redacted, result FROM agent_action WHERE tool = 'web.claim' AND business_id = 'biz_1'"
    ).fetchall()
    assert len(audited) == 1 and token not in str(audited)
    r = client.get(f"/connect?claim={token}")  # the link is spent
    assert "expired or was already used" in r.text and "Bright Smile Dental" in r.text  # still listed: it is theirs
    r = client.post("/connect/claim", data={"claim": token, "csrf": csrf_of(client)})
    assert "expired or was already used" in r.text


def test_a_claim_by_another_account_is_refused_and_nothing_changes(client, conn, google):
    draft_business(conn)
    from agent import claims

    claims.claim(conn, "biz_1", "first@example.com")
    url, _ = claims.new_link(conn, "biz_1")
    token = url.split("claim=")[1]
    sign_in(client)
    r = client.get(f"/connect?claim={token}")
    assert (
        "another Google account" in r.text
        and "Bright Smile Dental" not in r.text
        and 'action="/connect/claim"' not in r.text
    )
    r = client.post("/connect/claim", data={"claim": token, "csrf": csrf_of(client)})
    assert r.status_code == 409 and "another Google account" in r.text and "Bright Smile Dental" not in r.text
    assert business.row(conn, "biz_1")["owner_email"] == "first@example.com"
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.claim'").fetchone()["n"] == 0


def test_a_released_claim_is_listed_as_not_claimed_with_the_way_back(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL")  # jobs.claim_expire
    page = client.get("/connect").text
    assert (
        "Bright Smile Dental" in page
        and "Not claimed" in page
        and "link to claim it again." in page
        and "Verified" not in page
    )


def test_the_claim_needs_a_post_with_the_csrf_token(client, conn, google):
    draft_business(conn)
    from agent import claims

    url, _ = claims.new_link(conn, "biz_1")
    token = url.split("claim=")[1]
    sign_in(client)
    for _ in range(2):  # opening the link never claims
        client.get(f"/connect?claim={token}")
    assert business.row(conn, "biz_1")["claim_state"] == "draft"
    for data in ({"claim": token}, {"claim": token, "csrf": "wrong"}):
        assert client.post("/connect/claim", data=data).status_code == 403
    assert business.row(conn, "biz_1")["claim_state"] == "draft" and claims.take(conn, token) is not None


def claimed_business(client, conn, google):
    draft_business(conn)
    from agent import claims

    url, _ = claims.new_link(conn, "biz_1")
    sign_in(client, "/connect")
    r = client.post(
        "/connect/claim", data={"claim": url.split("claim=")[1], "csrf": csrf_of(client)}, follow_redirects=False
    )
    assert r.status_code == 303


def test_keys_are_created_with_scopes_shown_once_and_listed_without_secrets(client, conn, google):
    claimed_business(client, conn, google)
    r = client.post(
        "/connect/keys",
        data={"business_id": "biz_1", "label": "muse", "manage": "on", "leads": "on", "csrf": csrf_of(client)},
    )
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    plaintext = r.text.split("ak_")[1][:40]
    rec = keys.lookup(conn, "ak_" + plaintext)
    assert (
        rec.cls == "business"
        and rec.scopes == ("read", "manage", "leads")
        and rec.label == "muse"
        and rec.expires_at is None
    )
    assert "Add Admiry as a connector" in r.text  # the paste prompt travels with the key
    page = client.get("/connect").text
    assert "ak_" not in page and '<p class="row-title">Muse</p>' in page  # the assistant's name, not the label
    for s in ("read", "manage", "leads"):  # one chip a scope, in the words the assistants' chips use
        assert f'<span class="chip">{oauth.SCOPE_WORDS[s]}</span>' in page, s
    assert (
        conn.execute("SELECT result->>'key_id' AS k FROM agent_action WHERE tool = 'web.key_create'").fetchone()["k"]
        == rec.id
    )
    assert JARGON.findall(without_site_header(page)) == [] and JARGON.findall(r.text) == []


def test_key_forms_need_the_csrf_token_a_known_assistant_and_the_owner(client, conn, google):
    claimed_business(client, conn, google)
    assert (
        client.post("/connect/keys", data={"business_id": "biz_1", "label": "muse", "csrf": "wrong"}).status_code == 403
    )
    assert (
        client.post(
            "/connect/keys", data={"business_id": "biz_1", "label": "siri", "csrf": csrf_of(client)}
        ).status_code
        == 400
    )
    conn.execute("INSERT INTO business (id, owner_email, claim_state) VALUES ('biz_2', 'other@example.com', 'claimed')")
    assert (
        client.post(
            "/connect/keys", data={"business_id": "biz_2", "label": "muse", "csrf": csrf_of(client)}
        ).status_code
        == 404
    )
    assert (
        conn.execute("SELECT count(*) AS n FROM api_key WHERE class = 'business' AND label = 'muse'").fetchone()["n"]
        == 0
    )


def test_revoke_and_the_connection_light(client, conn, google):
    claimed_business(client, conn, google)
    r = client.post(
        "/connect/keys", data={"business_id": "biz_1", "label": "claude", "manage": "on", "csrf": csrf_of(client)}
    )
    plaintext = "ak_" + r.text.split("ak_")[1][:40]
    page = client.get("/connect").text
    assert GRAY_LIGHT in page and "An assistant checked in" not in page
    call_tool(client, "get_marketing_state", {}, key=plaintext)
    page = client.get("/connect").text
    assert re.search(r"An assistant checked in <time [^>]+>just now</time>\.", page) and GRAY_LIGHT not in page
    key_id = keys.lookup(conn, plaintext).id
    r = client.post(f"/connect/keys/{key_id}/revoke", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#advanced"
    assert keys.lookup(conn, plaintext).revoked_at is not None and key_id not in client.get("/connect").text
    # the revoked key is refused by the SDK now (A1b-4b Task 2), before run() ever sees it: 401
    assert rpc_raw(client, "tools/call", {"name": "whoami", "arguments": {}}, key=plaintext).status_code == 401
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.key_revoke'").fetchone()["n"] == 1


def test_sign_out_revokes_the_session_and_clears_the_cookie(client, conn, google):
    sign_in(client)
    r = client.post("/connect/signout", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert (
        r.status_code == 303 and "admiry_session=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    )
    assert conn.execute("SELECT revoked_at FROM web_session").fetchone()["revoked_at"] is not None
    assert 'href="/connect/google/start?next=/connect"' in client.get("/connect").text


def test_the_account_box_mints_the_account_scope(client, conn, google):
    claimed_business(client, conn, google)
    r = client.post(
        "/connect/keys", data={"business_id": "biz_1", "label": "claude", "account": "on", "csrf": csrf_of(client)}
    )
    assert keys.lookup(conn, "ak_" + r.text.split("ak_")[1][:40]).scopes == ("read", "account")


def test_a_key_of_another_owners_business_cannot_be_revoked(client, conn, google):
    claimed_business(client, conn, google)
    conn.execute("INSERT INTO business (id, owner_email, claim_state) VALUES ('biz_2', 'other@example.com', 'claimed')")
    plaintext, rec = keys.mint(conn, business_id="biz_2", cls="business", scopes=("read",), label="muse")
    assert client.post(f"/connect/keys/{rec.id}/revoke", data={"csrf": csrf_of(client)}).status_code == 404
    assert keys.lookup(conn, plaintext).revoked_at is None
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.key_revoke'").fetchone()["n"] == 0


def test_posts_without_a_session_are_refused(client, conn, google):
    claimed_business(client, conn, google)
    csrf = csrf_of(client)
    plaintext, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    client.cookies.clear()
    assert client.post("/connect/keys", data={"business_id": "biz_1", "label": "muse", "csrf": csrf}).status_code == 403
    assert client.post(f"/connect/keys/{rec.id}/revoke", data={"csrf": csrf}).status_code == 403
    assert client.post("/connect/signout", data={"csrf": csrf}).status_code == 403
    assert keys.lookup(conn, plaintext).revoked_at is None
    assert (
        conn.execute("SELECT count(*) AS n FROM api_key WHERE business_id = 'biz_1'").fetchone()["n"] == 2
    )  # draft + this one
    assert conn.execute("SELECT revoked_at FROM web_session").fetchone()["revoked_at"] is None


def test_a_new_sign_in_revokes_the_session_it_replaces(client, conn, google):
    sign_in(client)
    first = client.cookies.get("admiry_session")
    google.email = "second@example.com"
    sign_in(client)
    assert sessions.get(conn, first) is None and "second@example.com" in client.get("/connect").text
    assert conn.execute("SELECT count(*) AS n FROM web_session WHERE revoked_at IS NULL").fetchone()["n"] == 1


def test_a_sign_in_without_its_audit_row_leaves_no_session(client, conn, google, monkeypatch):
    def failing(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(web.audit, "record_web", failing)
    with pytest.raises(RuntimeError):
        sign_in(client)
    assert conn.execute("SELECT count(*) AS n FROM web_session").fetchone()["n"] == 0


def test_the_owner_view_names_the_business_only_when_a_site_vouches_for_it(client, conn, google):
    claimed_business(client, conn, google)  # the fixture's knowledge carries a website
    page = client.get("/connect").text
    assert "<h2>Bright Smile Dental</h2>" in page and 'class="state"' not in page and "claimed" not in page
    assert "This business is yours" in page and "Nothing is spent" in page
    conn.execute("UPDATE knowledge SET json = json - 'website'")
    page = client.get("/connect").text
    assert "<h2>Your business</h2>" in page and "Bright Smile Dental" not in page


def test_the_website_key_and_the_connection_light_stay_out_of_the_owners_way(client, conn, google):
    claimed_business(client, conn, google)
    page = client.get("/connect").text
    assert ROW % "website" not in page and ROW % "draft" not in page and "No keys yet" in page
    assert GRAY_LIGHT not in page and 'class="light' not in page
    r = client.post("/connect/keys", data={"business_id": "biz_1", "label": "muse", "csrf": csrf_of(client)})
    plaintext = "ak_" + r.text.split("ak_")[1][:40]
    page = client.get("/connect").text
    assert ROW % "Muse" in page and GRAY_LIGHT in page and ROW % "website" not in page
    key_id = keys.lookup(conn, plaintext).id
    client.post(f"/connect/keys/{key_id}/revoke", data={"csrf": csrf_of(client)})
    page = client.get("/connect").text
    assert GRAY_LIGHT not in page
    website = conn.execute("SELECT revoked_at FROM api_key WHERE label = 'website'").fetchone()
    assert website is not None and website["revoked_at"] is None  # the browser's own key: unlisted, untouched


def test_settings_shows_status_and_links_setup_into_the_conversation(client, conn, google):
    claimed_business(client, conn, google)
    page = client.get("/connect").text
    assert "<h1>Settings</h1>" in page and 'href="/?setup=biz_1"' in page and "Set up ads" in page
    for gone in ('action="/connect/agree"', 'action="/connect/ads-mode"', 'action="/connect/google-ads"', "/pay/"):
        assert gone not in page


def test_settings_offers_no_change_for_a_live_link_on_the_owners_own_account(client, conn, google):
    from agent import google_ads_link

    claimed_business(client, conn, google)
    google_ads_link.save(conn, "biz_1", "1234567890", status="active")
    page = client.get("/connect").text
    assert "Open setup" in page and ">Change<" not in page and "Set up ads" not in page


def test_settings_lists_the_balance_payments_and_accepted_texts_of_a_prepaid_business(client, conn, google):
    from agent import agreements, legal
    from agent.tests.test_wallet import fund

    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    fund(conn, cents=25000)
    page = client.get("/connect").text
    assert "Balance: <strong>$250.00</strong>" in page and "Card payment" in page
    assert "Google Ads Management Agreement v1" in page and "Prepayment Addendum v0.2" in page
    assert "Add funds" in page and JARGON.findall(without_site_header(page)) == []


def test_old_setup_links_land_in_the_conversation(client, conn, google):
    claimed_business(client, conn, google)
    for path in ("/connect?setup=ads", "/connect?ads=agreed_managed", "/pay/biz_1", "/pay/biz_1?done=1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?setup=biz_1", path
    for path in (
        "/connect/agree",
        "/connect/ads-mode",
        "/connect/google-ads",
        "/connect/google-ads/check",
        "/pay/biz_1",
    ):
        assert client.post(path, data={"csrf": csrf_of(client)}).status_code in (404, 405), path


def test_settings_names_every_kind_of_money_in_and_out(client, conn, google):
    from agent import wallet
    from agent.tests.test_wallet import fund

    claimed_business(client, conn, google)
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    fund(conn, cents=25000)
    wallet.refund(conn, "biz_1", refund_id="re_1", amount_cents=5000, session_id="cs_1")
    wallet.adjust(conn, "biz_1", 500, note="goodwill credit")
    page = client.get("/connect").text
    # a top-up's row reads its own note (M1m-1 Task 4): what it cost, not the generic word
    for words in ("<td>Card payment of $250.00 ($0.00 transaction fee)</td>", "<td>Refund</td>", "<td>Correction</td>"):
        assert words in page, words
    assert "<td>refund</td>" not in page and "<td>adjustment</td>" not in page
