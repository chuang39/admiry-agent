"""agent/hosted.py's serving side (A3p-2 Task 2): the tenant-host middleware (agent/app.py's _TenantHost),
previews and /pages/ask on the agent host, and the three new templates. `pages_client` points a TestClient
at a tenant host (https://bright-smile.business.test) with AGENT_PAGES_DOMAIN set before create_app() runs
-- agent/tests/conftest.py's own `client` fixture never sets that variable, so every other test module keeps
hosting off, and a request to /p/{token} or /pages/ask must still name the AGENT host explicitly (AGENT_BASE)
since pages_client's default host is the tenant one and would otherwise be swallowed by hosted.serve itself."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import business, db, hosted, landing
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON

DOMAIN = "business.test"
HOST = f"bright-smile.{DOMAIN}"
BASE = f"https://{HOST}"
AGENT_BASE = "http://testserver"  # conftest.py's AGENT_PUBLIC_BASE_URL

BODY = (
    '<html><head><meta charset="utf-8"></head><body>'
    "<h1>Bright Smile Dental</h1><p>Same-week appointments across the Peninsula.</p>"
    "</body></html>"
)


@pytest.fixture
def pages_client(conn, monkeypatch):
    """A TestClient whose default host is a tenant one, with hosting turned on (AGENT_PAGES_DOMAIN set
    before create_app() reads the environment)."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    from agent.app import create_app

    with TestClient(create_app(), base_url=BASE) as c:
        yield c


def _business(conn, biz_id: str) -> str:
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'draft')", (biz_id,))
    return biz_id


def _page(conn, biz_id: str, *, status: str, hostname: str | None, html: str = BODY) -> str:
    page_id = db.new_id("pg")
    url = f"https://{hostname}/" if hostname else ""
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, hostname, html, status) "
        "VALUES (%s, %s, 'hosted', %s, %s, %s, %s)",
        (page_id, biz_id, url, hostname, html, status),
    )
    return page_id


def _knowledge(conn, biz_id: str, **overrides) -> None:
    data = {
        "name": "Bright Smile Dental",
        "website": "https://brightsmile.example.com",
        "phone": "(650) 555-0100",
        "category": "dentists",
    }
    data.update(overrides)
    business.add_knowledge(conn, biz_id, data, source="owner")


# ---------------------------------------------------------------- the live page ("/", H3, H6)


def test_published_page_serves_its_html_with_the_csp_and_short_cache(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    r = pages_client.get("/")

    assert r.status_code == 200
    assert "Bright Smile Dental" in r.text
    assert r.headers["content-security-policy"] == hosted.csp(AGENT_BASE)
    assert r.headers["cache-control"] == "public, max-age=60"
    assert "set-cookie" not in r.headers


def test_head_on_a_tenant_host_answers_like_get(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    got = pages_client.get("/")
    head = pages_client.head("/")

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["cache-control"] == got.headers["cache-control"]
    assert head.headers["content-security-policy"] == got.headers["content-security-policy"]


# ---------------------------------------------------------------- /privacy, /robots.txt, /photos/{id}


def test_privacy_names_the_business_and_its_form_fields(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    _knowledge(conn, "biz_a")

    r = pages_client.get("/privacy")

    assert r.status_code == 200
    assert "Bright Smile Dental" in r.text
    for field in ("name", "phone", "service", "preferred_time"):  # dentists.json's form_fields
        assert field in r.text
    assert "www.admiry.ai/privacy" in r.text
    assert "no cookies" in r.text.lower()


def test_terms_names_who_runs_the_page_what_it_is_for_and_who_hosts_it(pages_client, conn):
    """M1 Task 4: the page NewsBreak's rule (and Google's good manners) asks every hosted page to link to -- plain
    terms of use for the business's page, in the privacy notice's structure, tone and context."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    _knowledge(conn, "biz_a")
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_a'")

    r = pages_client.get("/terms")

    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    text = hosted.visible_text(r.text)
    assert "<title>Terms of use — Bright Smile Dental</title>" in r.text
    assert "This page belongs to Bright Smile Dental." in text  # who runs the page
    assert "describes Bright Smile Dental's services" in text and "call or send the form" in text  # what it is for
    assert "provided as it is" in text and "no warranty" in text.lower()  # nothing promised beyond the business's own
    assert "Questions about these terms go to Bright Smile Dental, (650) 555-0100." in text
    assert "owner@example.com" not in r.text  # final review I5: never the owner's sign-in address
    assert "Admiry, Inc. hosts this page" in text and "not a party" in text  # who hosts it
    assert 'href="/privacy"' in r.text  # the notice about what the form collects
    # the live page's own headers, exactly as /privacy answers: the CSP, a minute of cache, no cookie of Admiry's
    privacy = pages_client.get("/privacy")
    assert r.headers["content-security-policy"] == privacy.headers["content-security-policy"] == hosted.csp(AGENT_BASE)
    assert r.headers["cache-control"] == "public, max-age=60" and "set-cookie" not in r.headers


def test_terms_without_a_phone_send_questions_through_the_form_and_never_publish_the_owners_address(pages_client, conn):
    """Final review I5: the terms page names no e-mail address -- the owner's sign-in address is never published there
    (the privacy notice's contact line is counsel's to settle) -- so questions go to the business's phone or, without
    one, through the form on the page."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    _knowledge(conn, "biz_a", phone=None)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = 'biz_a'")

    r = pages_client.get("/terms")

    assert r.status_code == 200 and "owner@example.com" not in r.text
    text = hosted.visible_text(r.text)
    assert "Questions about these terms go to Bright Smile Dental through the form on this page." in text


def test_terms_with_neither_a_phone_nor_a_form_still_name_the_business():
    business = {"name": "Bright Smile Dental", "phone": None, "email": "owner@example.com"}

    html = hosted.terms_html(business, {"form_fields": []})

    assert "Questions about these terms go to Bright Smile Dental." in hosted.visible_text(html)
    assert "owner@example.com" not in html


def test_terms_follow_what_the_page_has_a_page_with_no_phone_has_no_call_to_invite(pages_client, conn):
    """Fix round 1: a hosted page with no business phone has no call button (and no phone in its footer), so its terms
    invite the visitor to send the form -- not to call."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    _knowledge(conn, "biz_a", phone=None)

    text = hosted.visible_text(pages_client.get("/terms").text)

    assert "The page describes Bright Smile Dental's services and invites you to send the form on it." in text
    assert "Sending the form does not by itself make an agreement for any service" in text
    assert "call" not in text.lower()


@pytest.mark.parametrize(
    "phone, fields, invites, acts",
    [
        (
            "(650) 555-0100",
            ["name", "phone"],
            " and invites you to call or send the form on it.",
            "Calling or sending the form",
        ),
        ("(650) 555-0100", [], " and invites you to call.", "Calling"),
        (None, ["name", "phone"], " and invites you to send the form on it.", "Sending the form"),
        (None, [], ".", "Using this page"),  # a page that offers neither: it only describes
    ],
)
def test_terms_name_the_ways_the_page_offers_to_reach_the_business(phone, fields, invites, acts):
    business = {"name": "Bright Smile Dental", "phone": phone, "email": None}

    text = hosted.visible_text(hosted.terms_html(business, {"form_fields": fields}))

    assert f"The page describes Bright Smile Dental's services{invites} What it says is general information" in text
    assert f"{acts} does not by itself make an agreement for any service; any agreement is between you and" in text


def test_terms_say_it_in_the_owners_words(pages_client, conn):
    """The terms text passes the same word lists every owner-facing sentence does (JARGON, ACCOUNT_JARGON,
    MONEY_JARGON). It needs the founder's review before the first client page links to it."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    _knowledge(conn, "biz_a")

    text = hosted.visible_text(pages_client.get("/terms").text)

    assert len(text) > 400
    assert not (JARGON.search(text) or ACCOUNT_JARGON.search(text) or MONEY_JARGON.search(text))


def test_robots_txt(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    r = pages_client.get("/robots.txt")

    assert r.status_code == 200
    assert r.text == "User-agent: *\nAllow: /\n"
    # Task 2 review minor: the live page's own headers, not FastAPI's PlainTextResponse defaults.
    assert r.headers["cache-control"] == "public, max-age=60"
    assert r.headers["content-security-policy"] == hosted.csp(AGENT_BASE)


def test_photo_404_before_any_photo_exists(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    r = pages_client.get("/photos/ph_doesnotexist")

    assert r.status_code == 404


# ---------------------------------------------------------------- what does not exist on a tenant host


def test_mcp_and_connect_and_track_do_not_exist_on_a_tenant_host(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    assert pages_client.get("/mcp").status_code == 404
    assert pages_client.get("/connect").status_code == 404
    assert pages_client.get("/t/admiry.js").status_code == 404
    assert pages_client.post("/t/event").status_code == 404


def test_a_method_other_than_get_or_head_is_404(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    assert pages_client.post("/").status_code == 404


# ---------------------------------------------------------------- unpublished and unknown hosts


def test_unpublished_page_answers_410_everywhere_with_only_the_name(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="unpublished", hostname=HOST)
    _knowledge(conn, "biz_a", phone="(650) 555-9999")

    for path in ("/", "/privacy", "/terms", "/robots.txt"):
        r = pages_client.get(path)
        assert r.status_code == 410, path
        assert "Bright Smile Dental" in r.text
        assert "555-9999" not in r.text
        # Task 2 review minor: never cached -- a page that comes back later must not stay "gone" for visitors
        # (or a CDN) holding an earlier 410.
        assert r.headers["cache-control"] == "no-store", path


def test_clean_host_strips_a_trailing_dot_and_a_port_together(pages_client, conn):
    """Task 2 review minor: the absolute-FQDN form (a trailing ".") a client or Caddy may send is the same
    hostname as without it, exactly like the port is already stripped."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    r = pages_client.get(f"{AGENT_BASE}/pages/ask?domain={HOST}.:443")

    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_is_pages_host_matches_exactly_one_label_under_the_domain(pages_client, conn):
    """Task 2 review minor: "a.b.business.test" is two labels under the pages domain -- never a hostname
    `hosted.hostname_for` mints (a slug has no dot in it) -- so it must not be treated as one of ours."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)
    assert hosted.is_pages_host(f"a.{HOST}", DOMAIN) is False
    assert hosted.is_pages_host(HOST, DOMAIN) is True
    assert hosted.is_pages_host(DOMAIN, DOMAIN) is False  # the bare pages domain itself: nothing hosted at its apex

    r = pages_client.get(f"{AGENT_BASE}/pages/ask?domain=a.{HOST}")

    assert r.status_code == 404
    assert r.json() == {"ok": False}


def test_unknown_hostname_is_a_plain_404_with_no_business_data(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    for path in ("/", "/terms"):
        r = pages_client.get(f"https://nobody-here.{DOMAIN}{path}")

        assert r.status_code == 404, path
        assert "Bright Smile Dental" not in r.text


# ---------------------------------------------------------------- previews (the agent host, H3, H16)


def test_preview_serves_noindex_and_default_frame_ancestors_none(pages_client, conn):
    _business(conn, "biz_a")
    page_id = _page(conn, "biz_a", status="preview", hostname=None)
    token = hosted.save_preview(conn, page_id, BODY, "Bright Smile Dental")

    r = pages_client.get(f"{AGENT_BASE}/p/{token}")

    assert r.status_code == 200
    assert "Bright Smile Dental" in r.text
    assert '<meta name="robots" content="noindex">' in r.text
    assert r.headers["x-robots-tag"] == "noindex"
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    # H42/H45: sandboxed in an opaque origin, fetching only the public /t/ endpoints -- the preview's own policy
    assert r.headers["content-security-policy"] == hosted.csp(AGENT_BASE, preview=True)
    assert "set-cookie" not in r.headers


def _connect_src(policy: str) -> list[str]:
    return dict(d.strip().split(" ", 1) for d in policy.split(";"))["connect-src"].split()


def test_a_previews_form_sent_through_the_tracking_script_stores_the_lead(pages_client, conn):
    """H45 (final round 2): a preview may reach the agent host's public, unauthenticated /t/ endpoints -- the
    tracking script's visit beacon and its form send -- and nothing else. The served policy names exactly that one
    path-scoped source, so the script's `fetch` to /t/form/<id> is allowed while one to /api/csrf, /connect/keys or
    /mcp is refused (the browser enforces it; a source whose path ends in "/" matches that path as a prefix, CSP3).
    The form send itself, as the sandboxed browser makes it -- a cross-origin fetch from an opaque origin: `Origin:
    null`, no cookie, JSON asked for -- stores the lead, and the answer carries the CORS header the script needs to
    read it and show its thank-you line."""
    _business(conn, "biz_a")
    page_id = _page(conn, "biz_a", status="preview", hostname=None)
    form = (
        f'<form method="post" action="{AGENT_BASE}/t/form/{page_id}"><input name="name"><input name="phone">'
        f"<button>Send</button></form>{landing.snippet(page_id, AGENT_BASE, None)}"
    )
    token = hosted.save_preview(conn, page_id, BODY.replace("</body>", form + "</body>"), "Bright Smile Dental")

    shown = pages_client.get(f"{AGENT_BASE}/p/{token}")

    assert shown.status_code == 200 and f'src="{AGENT_BASE}/t/admiry.js"' in shown.text
    sources = _connect_src(shown.headers["content-security-policy"])
    assert sources == [f"{AGENT_BASE}/t/"]  # one source, and a path-scoped one
    for allowed in (f"{AGENT_BASE}/t/form/{page_id}", f"{AGENT_BASE}/t/event"):
        assert any(allowed.startswith(s) for s in sources), allowed
    for refused in (f"{AGENT_BASE}/api/csrf", f"{AGENT_BASE}/connect/keys", f"{AGENT_BASE}/mcp"):
        assert not any(refused.startswith(s) for s in sources), refused

    sent = pages_client.post(
        f"{AGENT_BASE}/t/form/{page_id}",
        data={"name": "Ana", "phone": "6505550199"},
        headers={"Origin": "null", "Accept": "application/json"},
    )

    assert sent.status_code == 200 and sent.json()["ok"] is True and sent.json()["lead"] is True
    assert sent.headers["access-control-allow-origin"] == "*"
    assert conn.execute("SELECT page_id FROM lead").fetchone()["page_id"] == page_id


def test_preview_with_embed_origin_frames_there_instead(pages_client, conn):
    _business(conn, "biz_a")
    page_id = _page(conn, "biz_a", status="preview", hostname=None)
    token = hosted.save_preview(conn, page_id, BODY, "Bright Smile Dental", embed_origin="https://home.test")

    r = pages_client.get(f"{AGENT_BASE}/p/{token}")

    assert r.status_code == 200
    assert "frame-ancestors https://home.test" in r.headers["content-security-policy"]
    assert r.headers["content-security-policy"].endswith("; sandbox allow-scripts allow-forms")


@pytest.mark.parametrize("host", ["home.test", "evil.example", "home.test:443"])
def test_a_preview_and_its_photos_answer_only_on_the_agent_host(pages_client, conn, monkeypatch, host):
    """H42 (final review): the browser host (AGENT_WEB_BASE_URL -- where the owner's session cookie lives) and any
    other name answer a preview token, and its photos, exactly like a token that does not exist: a 404 with no
    business data. Only the agent host (AGENT_PUBLIC_BASE_URL's), whatever port it is asked on, serves them."""
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://home.test")
    _business(conn, "biz_a")
    page_id = _page(conn, "biz_a", status="preview", hostname=None)
    token = hosted.save_preview(conn, page_id, BODY, "Bright Smile Dental", embed_origin="https://home.test")
    conn.execute(
        "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height) "
        "VALUES ('ph_1', %s, %s, 'image/jpeg', 1, 1)",
        (page_id, b"\xff\xd8\xff photo"),
    )

    elsewhere = pages_client.get(f"/p/{token}", headers={"Host": host})
    photo_elsewhere = pages_client.get(f"/p/{token}/photos/ph_1", headers={"Host": host})

    assert elsewhere.status_code == 404 and "Bright Smile Dental" not in elsewhere.text
    assert elsewhere.headers["x-robots-tag"] == "noindex"
    assert photo_elsewhere.status_code == 404 and photo_elsewhere.content != b"\xff\xd8\xff photo"
    for agent_host in ("testserver", "testserver:8097", "TestServer."):
        shown = pages_client.get(f"/p/{token}", headers={"Host": agent_host})
        photo = pages_client.get(f"/p/{token}/photos/ph_1", headers={"Host": agent_host})
        assert shown.status_code == 200 and "Bright Smile Dental" in shown.text, agent_host
        assert photo.status_code == 200 and photo.content == b"\xff\xd8\xff photo", agent_host


def test_preview_expires_and_a_guess_finds_nothing(pages_client, conn):
    _business(conn, "biz_a")
    page_id = _page(conn, "biz_a", status="preview", hostname=None)
    token = hosted.save_preview(conn, page_id, BODY, "Bright Smile Dental")
    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute' WHERE token = %s", (token,))

    expired = pages_client.get(f"{AGENT_BASE}/p/{token}")
    guessed = pages_client.get(f"{AGENT_BASE}/p/pv_0000000000000000")

    for r in (expired, guessed):
        assert r.status_code == 404
        assert "Bright Smile Dental" not in r.text
        assert r.headers["x-robots-tag"] == "noindex"


def test_preview_photo_404_before_any_photo_exists(pages_client, conn):
    _business(conn, "biz_a")
    page_id = _page(conn, "biz_a", status="preview", hostname=None)
    token = hosted.save_preview(conn, page_id, BODY, "Bright Smile Dental")

    r = pages_client.get(f"{AGENT_BASE}/p/{token}/photos/ph_doesnotexist")

    assert r.status_code == 404


# ---------------------------------------------------------------- /pages/ask (Caddy's on_demand_tls check, H4)


def test_pages_ask_200_for_a_published_hostname_404_otherwise(pages_client, conn):
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    ok = pages_client.get(f"{AGENT_BASE}/pages/ask?domain={HOST}")
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}

    with_port = pages_client.get(f"{AGENT_BASE}/pages/ask?domain={HOST}:443")
    assert with_port.status_code == 200
    assert with_port.json() == {"ok": True}

    unknown = pages_client.get(f"{AGENT_BASE}/pages/ask?domain=nobody-here.{DOMAIN}")
    assert unknown.status_code == 404
    assert unknown.json() == {"ok": False}

    outside = pages_client.get(f"{AGENT_BASE}/pages/ask?domain=evil.example.com")
    assert outside.status_code == 404


def test_pages_ask_404_for_a_hostname_taken_down(pages_client, conn):
    """A page that was published and later unpublished keeps its hostname (H2), but /pages/ask must stop
    vouching for it the moment it is not `published` any more -- Caddy would otherwise keep renewing a
    certificate for a site that now answers every path 410."""
    _business(conn, "biz_a")
    _page(conn, "biz_a", status="unpublished", hostname=HOST)

    r = pages_client.get(f"{AGENT_BASE}/pages/ask?domain={HOST}")

    assert r.status_code == 404
    assert r.json() == {"ok": False}


# ---------------------------------------------------------------- hosting off (H1)


def test_middleware_is_inert_without_a_pages_domain(conn, monkeypatch):
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    from agent.app import create_app

    _business(conn, "biz_a")
    _page(conn, "biz_a", status="published", hostname=HOST)

    with TestClient(create_app(), base_url=BASE) as c:
        # Hosting is off: the tenant Host is never intercepted, so ordinary routing applies -- /health
        # answers exactly as it would on the agent's own host, regardless of the page that exists.
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_pages_ask_404_when_hosting_is_off(conn, monkeypatch):
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    from agent.app import create_app

    with TestClient(create_app(), base_url=AGENT_BASE) as c:
        r = c.get(f"/pages/ask?domain={HOST}")

        assert r.status_code == 404
        assert r.json() == {"ok": False}
