"""Settings backend (spec 2026-09-29-admiry-settings-redesign §5): the CSP on /connect, ?b= business selection,
the per-business redirect targets, sign-out to the home, and readable times (no raw ISO timestamp on the page)."""

import re

from agent import claims, keys, mail_grants, mcp_app  # noqa: F401 - mcp_app first: connect<->surface.told cycle
from agent.tests.conftest import call_tool
from agent.tests.test_claims import draft_business
from agent.tests.test_oauth_consent import _sign_in
from agent.tests.test_web import csrf_of, sign_in
from agent.tests.test_web_grants import _grant

ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _two_businesses(conn):
    """A (older) and B (newer), both claimed by owner@example.com, and C owned by someone else."""
    conn.execute(
        "INSERT INTO business (id, owner_email, claim_state, slug, created_at) VALUES "
        "('biz_a', 'owner@example.com', 'claimed', 'a', now() - interval '2 days'), "
        "('biz_b', 'owner@example.com', 'claimed', 'b', now() - interval '1 day'), "
        "('biz_c', 'other@example.com', 'claimed', 'c', now())"
    )


def _text_only(html: str) -> str:
    """The page with every tag's attributes dropped: a <time datetime> may hold ISO, the text must not."""
    return re.sub(r"<(/?[A-Za-z][^\s>/]*)[^>]*>", r"<\1>", html)


def test_connect_sends_a_self_only_csp(client, conn, google):
    sign_in(client)
    r = client.get("/connect")
    csp = r.headers["content-security-policy"]
    for part in ("script-src 'self'", "style-src 'self'", "font-src 'self'", "frame-ancestors 'none'"):
        assert part in csp, part
    assert "form-action 'self'" in csp and "accounts.google.com" not in csp  # every /connect form posts to itself


def test_connect_shows_one_business_the_newest_unless_b_names_another_of_the_owners(client, conn, google):
    _two_businesses(conn)
    sign_in(client)
    page = client.get("/connect").text
    assert 'href="/?setup=biz_b"' in page and 'href="/?setup=biz_a"' not in page
    page = client.get("/connect?b=biz_a").text
    assert 'href="/?setup=biz_a"' in page and 'href="/?setup=biz_b"' not in page
    for other in ("biz_c", "nope", ""):  # someone else's, unknown, empty: the newest, never an error
        r = client.get(f"/connect?b={other}")
        assert r.status_code == 200 and 'href="/?setup=biz_b"' in r.text, other
        assert "biz_c" not in r.text and 'href="/?setup=biz_a"' not in r.text


def test_a_grant_revoke_returns_to_that_businesss_assistants(client, conn, google):
    _access_token, grant_id = _grant(client, conn)
    csrf = _sign_in(client, google)
    r = client.post(f"/connect/grants/{grant_id}/revoke", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#assistants"


def test_a_key_revoke_returns_to_that_businesss_advanced(client, conn, google):
    draft_business(conn)
    claims.claim(conn, "biz_1", "owner@example.com")
    _, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    sign_in(client)
    r = client.post(f"/connect/keys/{rec.id}/revoke", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#advanced"


def test_a_new_business_opens_on_its_own_overview(client, conn, google):
    csrf = _sign_in(client, google)
    r = client.post("/connect/businesses/new", data={"csrf": csrf}, follow_redirects=False)
    biz_id = conn.execute("SELECT id FROM business").fetchone()["id"]
    assert r.status_code == 303 and r.headers["location"] == f"/connect?b={biz_id}#overview"


def test_a_claim_returns_to_the_claimed_business(client, conn, google):
    draft_business(conn)
    conn.execute("UPDATE business SET created_at = now() - interval '3 days' WHERE id = 'biz_1'")
    _two_businesses(conn)
    url, _ = claims.new_link(conn, "biz_1")
    sign_in(client)
    r = client.post(
        "/connect/claim", data={"claim": url.split("claim=")[1], "csrf": csrf_of(client)}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1&claimed=1"
    page = client.get(r.headers["location"]).text  # biz_1 is shown although biz_b is newer
    assert "now yours" in page and 'href="/?setup=biz_1"' in page and 'href="/?setup=biz_b"' not in page


def test_the_claim_confirmation_shows_the_business_being_claimed(client, conn, google):
    draft_business(conn)
    claims.claim(conn, "biz_1", "owner@example.com")
    conn.execute("UPDATE business SET created_at = now() - interval '3 days' WHERE id = 'biz_1'")
    _two_businesses(conn)
    url, _ = claims.new_link(conn, "biz_1")
    sign_in(client)
    page = client.get(f"/connect?claim={url.split('claim=')[1]}").text  # already theirs: asks, shows biz_1
    assert 'action="/connect/claim"' in page and 'href="/?setup=biz_1"' in page and 'href="/?setup=biz_b"' not in page


def test_sign_out_lands_on_the_home(client, conn, google):
    sign_in(client)
    r = client.post("/connect/signout", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_address_add_and_revoke_return_to_that_businesss_email(client, conn, google, resend):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'claimed', 'owner@example.com', 'acme')"
    )
    csrf = _sign_in(client, google)
    r = client.post(
        "/connect/addresses",
        data={"csrf": csrf, "business_id": "biz_1", "address": "cue@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#email"
    g = mail_grants.live_for(conn, "biz_1", "cue@example.com")
    r = client.post(f"/connect/addresses/{g['id']}/revoke", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect?b=biz_1#email"


def test_no_raw_iso_timestamp_on_the_page(client, conn, google, resend):
    access_token, _grant_id = _grant(client, conn)
    call_tool(client, "get_marketing_state", {}, key=access_token)  # last used + the connection light
    plaintext, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="muse")
    call_tool(client, "whoami", {}, key=plaintext)
    mail_grants.add(conn, "biz_1", "cue@example.com", scopes=["read"], via="connect", created_by="owner@example.com")
    _sign_in(client, google)
    html = client.get("/connect").text
    assert '<p class="row-title">Muse</p>' in html and "cue@example.com" in html and "just now" in html
    assert not ISO.search(_text_only(html)), ISO.search(_text_only(html))
