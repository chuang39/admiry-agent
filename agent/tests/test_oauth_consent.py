import re
import secrets
from urllib.parse import parse_qs, urlparse

import psycopg

from agent import oauth
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, TEST_DB_URL, call_tool, data
from agent.tests.test_oauth_http import CB, exchange, register


def _txn(client, conn, cid, scopes="read manage leads account"):
    verifier = secrets.token_urlsafe(48)
    r = client.get(
        "/authorize",
        params={
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": CB,
            "state": "st",
            "code_challenge": oauth.s256(verifier),
            "code_challenge_method": "S256",
            "scope": scopes,
            "resource": "http://testserver/mcp",
        },
        follow_redirects=False,
    )
    return r.headers["location"].split("txn=")[1], verifier


def _sign_in(client, google, email="owner@example.com"):
    google.email = email
    r = client.get("/connect/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get(f"/connect/google/callback?state={state}&code=x", follow_redirects=False)
    page = client.get("/connect")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_consent_requires_sign_in_and_expires(client, conn, google):
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    r = client.get(f"/oauth/consent?txn={txn}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/connect/google/start?next=/oauth/consent%3Ftxn%3D{txn}"
    google.email = "owner@example.com"  # the encoded next survives the round trip through Google sign-in
    start = client.get(r.headers["location"], follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    back = client.get(f"/connect/google/callback?state={state}&code=x", follow_redirects=False)
    assert back.headers["location"] == f"/oauth/consent?txn={txn}"
    _sign_in(client, google)
    conn.execute("UPDATE oauth_txn SET expires_at = now() - interval '1 minute'")
    r = client.get(f"/oauth/consent?txn={txn}")
    assert r.status_code == 410 and "expired" in r.text
    assert client.get("/oauth/consent?txn=txn_0000000000000000").status_code == 410


def test_consent_page_lists_businesses_scopes_and_the_assistant(client, conn, google):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES "
        "('biz_1', 'claimed', 'owner@example.com', 'palo-alto-dental')"
    )
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES "
        "('biz_2', 'claimed', 'other@example.com', 'someone-else')"
    )
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.get(f"/oauth/consent?txn={txn}")
    assert r.status_code == 200 and "Claude" in r.text and "biz_1" in r.text and "biz_2" not in r.text
    assert "A new business" in r.text
    for word in (
        "Read your plans and reports",
        "Make changes within your monthly budget",
        "See your leads and record what became of them",  # leads also records outcomes and adds leads (A I2)
        "Manage keys and settings",
    ):
        assert word in r.text
    assert not JARGON.search(r.text) and not ACCOUNT_JARGON.search(r.text)
    assert r.headers["cache-control"] == "no-store" and 'name="robots" content="noindex"' in r.text
    assert csrf in r.text


def test_allow_mints_a_code_the_client_can_exchange(client, conn, google):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES "
        "('biz_1', 'claimed', 'owner@example.com', 'palo-alto-dental')"
    )
    cid = register(client)
    txn, verifier = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent",
        data={
            "txn": txn,
            "csrf": csrf,
            "business": "biz_1",
            "scopes": ["read", "manage", "leads"],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith(CB + "?code=oc_")
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, cid, code, verifier).json()
    who = data(call_tool(client, "whoami", {}, key=tok["access_token"]))
    assert who["business"]["id"] == "biz_1" and set(who["key"]["scopes"]) == {
        "read",
        "manage",
        "leads",
    }  # account unticked
    row = conn.execute("SELECT actor, business_id, result FROM agent_action WHERE tool = 'web.oauth_grant'").fetchone()
    assert row["actor"] == "web:owner@example.com" and row["business_id"] == "biz_1"
    assert (
        client.post(
            "/oauth/consent",
            data={"txn": txn, "csrf": csrf, "business": "biz_1", "decision": "allow"},
            follow_redirects=False,
        ).status_code
        == 410
    )


def test_new_business_is_created_and_claimed_for_the_signer(client, conn, google):
    cid = register(client)
    txn, verifier = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent",
        data={"txn": txn, "csrf": csrf, "business": "new", "scopes": ["read", "manage"], "decision": "allow"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    biz = conn.execute("SELECT id, owner_email, claim_state, slug FROM business").fetchone()
    assert biz["owner_email"] == "owner@example.com" and biz["claim_state"] == "claimed" and biz["slug"]
    assert conn.execute("SELECT count(*) AS n FROM api_key WHERE class = 'draft'").fetchone()["n"] == 0
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, cid, code, verifier).json()
    assert data(call_tool(client, "whoami", {}, key=tok["access_token"]))["business"]["id"] == biz["id"]


def test_consent_refuses_a_business_of_another_owner_and_a_bad_csrf(client, conn, google):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_2', 'claimed', 'other@example.com', 'someone-else')"
    )
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent",
        data={"txn": txn, "csrf": csrf, "business": "biz_2", "decision": "allow"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and conn.execute("SELECT count(*) AS n FROM oauth_code").fetchone()["n"] == 0
    r = client.post(
        "/oauth/consent",
        data={"txn": txn, "csrf": "bad", "business": "new", "decision": "allow"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_deny_sends_access_denied_to_the_client(client, conn, google):
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    r = client.post(
        "/oauth/consent", data={"txn": txn, "csrf": csrf, "business": "new", "decision": "deny"}, follow_redirects=False
    )
    assert (
        r.status_code == 303 and "error=access_denied" in r.headers["location"] and "state=st" in r.headers["location"]
    )
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 0


def test_account_scope_is_off_by_default(client, conn, google):
    """D13: account is off by default — the owner must tick it. read/manage/leads are pre-checked; account
    is not. An untouched page (allow with the boxes exactly as rendered) must not grant account."""
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES "
        "('biz_1', 'claimed', 'owner@example.com', 'palo-alto-dental')"
    )
    cid = register(client)
    txn, verifier = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    page = client.get(f"/oauth/consent?txn={txn}").text
    boxes = dict(re.findall(r'<input type="checkbox" name="scopes" value="([a-z]+)"([^>]*)>', page))
    assert set(boxes) == {"read", "manage", "leads", "account"}
    for scope in ("read", "manage", "leads"):
        assert "checked" in boxes[scope]
    assert "checked" not in boxes["account"]

    r = client.post(
        "/oauth/consent",
        data={
            "txn": txn,
            "csrf": csrf,
            "business": "biz_1",
            "scopes": ["read", "manage", "leads"],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, cid, code, verifier).json()
    who = data(call_tool(client, "whoami", {}, key=tok["access_token"]))
    assert set(who["key"]["scopes"]) == {"read", "manage", "leads"}


def _allow(client, txn, csrf, business="new", scopes=("read", "manage", "leads")):
    return client.post(
        "/oauth/consent",
        data={"txn": txn, "csrf": csrf, "business": business, "scopes": list(scopes), "decision": "allow"},
        follow_redirects=False,
    )


def test_a_read_only_request_is_granted_read_only(client, conn, google):
    """Final review I1: the fixed scopes (read, manage) are granted only when the client asked for them — a
    client that asked for `read` never gets `manage`, which the page never showed."""
    cid = register(client)
    txn, verifier = _txn(client, conn, cid, scopes="read")
    csrf = _sign_in(client, google)
    page = client.get(f"/oauth/consent?txn={txn}").text
    boxes = dict(re.findall(r'<input type="checkbox" name="scopes" value="([a-z]+)"([^>]*)>', page))
    assert set(boxes) == {"read"} and "Make changes within your monthly budget" not in page
    r = _allow(client, txn, csrf, scopes=("read", "manage"))  # a forged extra scope is ignored too
    assert r.status_code == 303
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = exchange(client, cid, code, verifier).json()
    assert tok["scope"] == "read"
    assert data(call_tool(client, "whoami", {}, key=tok["access_token"]))["key"]["scopes"] == ["read"]


def test_consent_page_names_the_return_host_and_refuses_framing(client, conn, google):
    """Final review I2 (b, c): the page says where the owner is sent back to (the redirect host, not only the
    self-asserted client name), and no /oauth/consent response may be framed."""
    cid = register(client, name="<b>Claude</b>")
    txn, _ = _txn(client, conn, cid)
    r = client.get(f"/oauth/consent?txn={txn}", follow_redirects=False)  # signed out: the redirect is covered too
    assert r.headers["x-frame-options"] == "DENY"
    csrf = _sign_in(client, google)
    r = client.get(f"/oauth/consent?txn={txn}")
    assert r.status_code == 200 and "You'll be sent back to" in r.text and "claude.ai" in r.text
    assert "<b>Claude</b>" not in r.text  # the client name is escaped
    for resp in (r, client.post("/oauth/consent", data={"txn": "txn_nope", "csrf": csrf}, follow_redirects=False)):
        assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert resp.headers["x-frame-options"] == "DENY"


def test_two_consents_by_one_signer_share_one_blank_business(client, conn, google):
    """Final review I3: "A new business" reuses the signer's blank business (no knowledge yet) instead of
    creating another one per consent."""
    cid = register(client)
    csrf = _sign_in(client, google)
    for _ in range(2):
        txn, _ = _txn(client, conn, cid)
        assert _allow(client, txn, csrf).status_code == 303
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 1
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    grants = conn.execute("SELECT business_id FROM oauth_grant").fetchall()
    assert [g["business_id"] for g in grants] == [biz, biz]


def test_consent_new_business_is_refused_while_another_request_holds_the_lock(client, conn, google):
    """I3: the newbiz advisory lock is shared with /connect/businesses/new; a held lock answers 409 and leaves
    the transaction usable (the txn is not consumed)."""
    cid = register(client)
    txn, _ = _txn(client, conn, cid)
    csrf = _sign_in(client, google)
    with psycopg.connect(TEST_DB_URL, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('newbiz:owner@example.com'))")
        r = _allow(client, txn, csrf)
        assert r.status_code == 409 and "try again in a moment" in r.text
    assert conn.execute("SELECT count(*) AS n FROM business").fetchone()["n"] == 0
    assert _allow(client, txn, csrf).status_code == 303  # the transaction is still there
