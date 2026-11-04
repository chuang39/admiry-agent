"""The web step (spec 2026-09-28 §3.2, §6.4, D55, D56) and the addresses block on /connect (§4)."""

import re

from agent import benchmark, mail_dispatch, mail_grants
from agent.tests.conftest import JARGON
from agent.tests.test_mail_dispatch import (  # noqa: F401 - fixture
    CUE,
    OWNER,
    fake,
    last_sent,
    reply_to,
    thread_row,
    walk_to_ready,
)
from agent.tests.test_mail_in import inbound
from agent.tests.test_oauth_consent import _sign_in


def link_of(resend) -> str:
    return re.search(r"http://testserver/m/([A-Za-z0-9_-]+)", last_sent(resend)["text"]).group(1)


def test_link_requires_sign_in_then_shows_the_question_only_for_another_address(client, conn, resend, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    walk_to_ready(client, conn, resend, sender=CUE)
    token = link_of(resend)
    r = client.get(f"/m/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/connect/google/start?next=/m/{token}"
    _sign_in(client, google)  # owner@example.com
    page = client.get(f"/m/{token}")
    assert page.status_code == 200 and page.headers["x-frame-options"] == "DENY"
    assert (
        CUE in page.text and 'name="allow"' in page.text and "See my plan" in page.text and not JARGON.search(page.text)
    )
    assert client.get("/m/nope").status_code == 404


def test_continue_claims_builds_the_plan_grants_the_address_and_writes_back(client, conn, resend, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend, sender=CUE)
    token = link_of(resend)
    csrf = _sign_in(client, google)
    r = client.post(
        f"/m/{token}", data={"csrf": csrf, "allow": "yes", "scopes": ["read", "manage"]}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/plan/pl_")
    biz = conn.execute("SELECT claim_state, owner_email FROM business WHERE id = %s", (t["business_id"],)).fetchone()
    assert biz["claim_state"] == "claimed" and biz["owner_email"] == OWNER
    assert conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 1
    g = mail_grants.live_for(conn, t["business_id"], CUE)
    assert g and g["scopes"] == ["read", "manage"] and g["via"] == "web_step" and g["created_by"] == OWNER
    assert conn.execute("SELECT used_at FROM email_link").fetchone()["used_at"] is not None
    sent = last_sent(resend)
    assert (
        sent["to"] == [CUE] and mail_dispatch.SIGNED_IN.format(email=OWNER) in sent["text"] and "/plan/" in sent["text"]
    )
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.mail_link'").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT count(*) AS n FROM api_key WHERE source = 'email' AND revoked_at IS NULL").fetchone()["n"]
        == 0
    )
    # the thread goes on by e-mail: the agent adjusts under its grant
    inbound(client, resend, from_=CUE, text="Make it $1,500 a month", to=reply_to(t))
    assert "$1,500" in last_sent(resend)["text"]
    # and the link is spent: the owner is sent to the plan, anyone else sees 404
    r = client.get(f"/m/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/plan/")
    _sign_in(client, google, email="other@example.com")
    assert client.get(f"/m/{token}").status_code == 404
    assert client.post(f"/m/{token}", data={"csrf": "x"}, follow_redirects=False).status_code in (403, 410)


def test_use_link_wins_once(client, conn, resend, fake):  # noqa: F811 - fixture
    """The primitive mail_web.link_continue's race guard relies on (review round 1, finding 2): the first call
    to spend a link wins (True); every later call on the same token_hash loses (False) — the UPDATE's row lock
    is what would serialize two callers racing the same link, not shown here since this is a single thread, but
    the True-once/False-after contract is what the guard needs and is what this asserts directly."""
    benchmark.seed(conn)
    walk_to_ready(client, conn, resend, sender=CUE)
    token = link_of(resend)
    token_hash = mail_grants.peek_link(conn, token)["token_hash"]
    assert mail_grants.use_link(conn, token_hash) is True
    assert mail_grants.use_link(conn, token_hash) is False


def test_a_second_post_of_a_spent_link_answers_410_with_no_repeat_grant_or_mail(client, conn, resend, fake, google):  # noqa: F811 - fixture
    """The observable half of the double-click / two-tabs race (review round 1, finding 2): once the link is
    spent, a second POST from the same signed-in owner finds it already used and answers 410 — no second grant,
    audit row or SIGNED_IN mail, matching the finding's stated symptom of the unguarded code."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend, sender=CUE)
    token = link_of(resend)
    csrf = _sign_in(client, google)
    r1 = client.post(
        f"/m/{token}", data={"csrf": csrf, "allow": "yes", "scopes": ["read", "manage"]}, follow_redirects=False
    )
    assert r1.status_code == 303
    sent_before = len(resend.sent)
    r2 = client.post(
        f"/m/{token}", data={"csrf": csrf, "allow": "yes", "scopes": ["read", "manage"]}, follow_redirects=False
    )
    assert r2.status_code == 410
    assert len(resend.sent) == sent_before
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.mail_link'").fetchone()["n"] == 1
    g = mail_grants.live_for(conn, t["business_id"], CUE)
    assert g and g["scopes"] == ["read", "manage"]  # the first POST's grant, untouched by the second


def test_the_same_address_is_asked_nothing_and_granted_nothing(client, conn, resend, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)  # owner@example.com wrote
    token = link_of(resend)
    csrf = _sign_in(client, google)
    assert 'name="allow"' not in client.get(f"/m/{token}").text
    r = client.post(
        f"/m/{token}", data={"csrf": csrf, "allow": "yes", "scopes": ["read", "manage"]}, follow_redirects=False
    )
    assert r.status_code == 303 and mail_grants.for_business(conn, t["business_id"]) == []


def test_a_declined_address_gets_no_mail_and_no_grant(client, conn, resend, fake, google):  # noqa: F811 - fixture
    """finding I2: link_continue used to send SIGNED_IN + the plan link to link["counterpart"] whenever the link
    was won, whatever `allow` said. An owner who declines the address (no "allow" in the POST) must get silence
    -- not a leak of the plan and a sign-in confirmation to an address they just refused -- while the redirect to
    the owner's own plan still works."""
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend, sender=CUE)
    token = link_of(resend)
    csrf = _sign_in(client, google)  # owner@example.com signs in; CUE (the counterpart) is a different address
    sent_before = len(resend.sent)
    r = client.post(f"/m/{token}", data={"csrf": csrf}, follow_redirects=False)  # no "allow"
    assert r.status_code == 303 and r.headers["location"].startswith("/plan/")
    assert len(resend.sent) == sent_before
    assert mail_grants.live_for(conn, t["business_id"], CUE) is None


def test_mail_routes_are_404_when_the_channel_is_off(client):
    """Like the webhook (finding I1): /m/{token} and the two /connect/addresses routes must not even hint the
    capability exists when AGENT_EMAIL_INBOUND is off, before any session or CSRF check."""
    assert client.get("/m/anything", follow_redirects=False).status_code == 404
    assert client.post("/m/anything", data={"csrf": "x"}, follow_redirects=False).status_code == 404
    assert (
        client.post(
            "/connect/addresses",
            data={"csrf": "x", "business_id": "biz_1", "address": "a@b.com"},
            follow_redirects=False,
        ).status_code
        == 404
    )
    assert (
        client.post("/connect/addresses/grant_1/revoke", data={"csrf": "x"}, follow_redirects=False).status_code == 404
    )


def test_connect_hides_the_inbox_block_when_the_channel_is_off(client, conn, google):
    """finding I5: /connect must not advertise "Write to Admiry at …" or the "Add an address" form while the
    channel is off (the resend fixture's absence here means AGENT_EMAIL_INBOUND defaults to off)."""
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'claimed', 'owner@example.com', 'acme')"
    )
    _sign_in(client, google)
    page = client.get("/connect").text
    assert "Write to Admiry at" not in page and "Add an address" not in page


def test_link_on_an_expired_draft_is_404(client, conn, resend, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend)
    token = link_of(resend)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = %s", (t["business_id"],))
    csrf = _sign_in(client, google)
    assert client.get(f"/m/{token}").status_code == 404
    # POST answers 410 like every other dead-link case on this route (GET keeps 404 for both: review round 1)
    assert client.post(f"/m/{token}", data={"csrf": csrf}, follow_redirects=False).status_code == 410
    assert (
        conn.execute("SELECT owner_email FROM business WHERE id = %s", (t["business_id"],)).fetchone()["owner_email"]
        is None
    )


def test_a_business_claimed_by_someone_else_refuses(client, conn, resend, fake, google):  # noqa: F811 - fixture
    benchmark.seed(conn)
    t = walk_to_ready(client, conn, resend, sender=CUE)
    token = link_of(resend)
    conn.execute(
        "UPDATE business SET claim_state = 'claimed', owner_email = 'other@example.com' WHERE id = %s",
        (t["business_id"],),
    )
    csrf = _sign_in(client, google)
    r = client.post(f"/m/{token}", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 403 and conn.execute("SELECT count(*) AS n FROM plan").fetchone()["n"] == 0


def test_connect_lists_adds_and_revokes_addresses(client, conn, google, resend):
    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'claimed', 'owner@example.com', 'acme')"
    )
    csrf = _sign_in(client, google)
    page = client.get("/connect").text
    assert "hello@agent.test" in page and "Add an address" in page
    r = client.post(
        "/connect/addresses",
        data={"csrf": csrf, "business_id": "biz_1", "address": "Cue@Example.com", "manage": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    g = mail_grants.live_for(conn, "biz_1", "cue@example.com")
    assert g and g["scopes"] == ["read", "manage"] and g["via"] == "connect"
    page = client.get("/connect").text
    assert "cue@example.com" in page and f"/connect/addresses/{g['id']}/revoke" in page
    bad = client.post(
        "/connect/addresses",
        data={"csrf": csrf, "business_id": "biz_1", "address": "not an address"},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    other = _sign_in(client, google, email="other@example.com")
    assert (
        client.post(f"/connect/addresses/{g['id']}/revoke", data={"csrf": other}, follow_redirects=False).status_code
        == 404
    )
    csrf = _sign_in(client, google)
    assert (
        client.post(f"/connect/addresses/{g['id']}/revoke", data={"csrf": csrf}, follow_redirects=False).status_code
        == 303
    )
    assert mail_grants.live_for(conn, "biz_1", "cue@example.com") is None
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM agent_action WHERE tool IN ('web.mail_grant', 'web.mail_grant_revoke')"
        ).fetchone()["n"]
        == 2
    )


def test_per_message_keys_never_show_in_the_keys_table(client, conn, google, resend):
    from agent import keys

    conn.execute(
        "INSERT INTO business (id, claim_state, owner_email, slug) VALUES ('biz_1', 'claimed', 'owner@example.com', 'acme')"
    )
    _, rec = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read",), label="email", source="email")
    _sign_in(client, google)
    assert f"/connect/keys/{rec.id}/revoke" not in client.get("/connect").text
