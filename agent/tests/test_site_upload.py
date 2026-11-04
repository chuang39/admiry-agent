"""A site bundle end to end (A3p-3 spec §6, §7, P30-P37; plan 2026-10-04-agent-a3p-3-3-site-bundles B2-B13): the
one-time link (request_site_upload), the PUT and its bounds, a refusal that stores nothing, the business's preview host, the
publish through the same proposal, serving by path on the tenant host, versions, the source as files and a zip, the
download page, and the collection and the purge. `_hosted_biz` is the dental walk with hosting on; `_serving` points
the app's tenant-host middleware at the pages domain the walk turned on after the app was made."""

from __future__ import annotations

import hashlib
import io
import zipfile
from urllib.parse import urlsplit

from agent import bundles, config, hosted, hosting_minimum, site_upload
from agent.tests.conftest import JARGON, call_tool, data, tool_text
from agent.tests.test_bundles import WORDS, make_zip, png
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import GOOD_HTML, HOSTNAME, URL, _checked, _hosted_biz, _mailbox
from agent.tests.test_web import sign_in

AGENT = "http://testserver"


def _serving(client) -> None:
    """The tenant-host middleware reads the settings the app was made with; the walk turned the pages domain on
    after that."""
    client.app.state.settings = config.load()


def _site(link: dict, *, headline: str = "Invisalign in Palo Alto") -> dict:
    """A small build for the link's page: its own script tag and form address on every page."""
    snippet, action = link["snippet"], link["form_action"]
    head = f"<head><title>Bright Smile Dental</title>{snippet}</head>"
    return {
        "index.html": (
            f"<!doctype html><html>{head}<body><h1>{headline}</h1><p>{WORDS * 3}</p>"
            f'<form method="post" action="{action}" data-admiry-intent="appointment"><input name="name">'
            '<input name="phone"><button type="submit">Send</button></form><a href="/privacy">Privacy</a></body></html>'
        ),
        "about/index.html": f"<html>{head}<body><p>About Bright Smile Dental.</p></body></html>",
        "404.html": f"<html>{head}<body><p>That page is not here.</p></body></html>",
        "_next/static/chunks/app-4f3b2a1c9d.js": "console.log(location.pathname)",
        "logo.svg": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        "img/team.png": png(),
    }


def _link(client, key: str) -> dict:
    res = call_tool(client, "request_site_upload", {}, key=key)
    assert not res["isError"], tool_text(res)
    return data(res)


def _put(client, link: dict, body: bytes, **kwargs):
    return client.put(urlsplit(link["upload_url"]).path, content=body, **kwargs)


def _upload(client, key: str, **site) -> dict:
    link = _link(client, key)
    r = _put(client, link, make_zip(_site(link, **site)))
    assert r.status_code == 200, r.text
    return {**r.json(), "link": link}


def _publish(client, conn, monkeypatch, key: str, token: str, idem: str) -> dict:
    prop = data(call_tool(client, "publish_page", {"idempotency_key": f"pb{idem}", "token": token}, key=key))
    assert prop["status"] == "proposed", prop
    _checked(monkeypatch, conn, URL)
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": f"cb{idem}"}, key=key
    )
    assert not res["isError"], tool_text(res)
    return prop


def _get(client, host: str, path: str = "/", **headers):
    return client.get(path, headers={"host": host, **headers})


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


# ---------------------------------------------------------------- the whole way (Review Focus 4)


def test_a_bundle_goes_from_upload_to_live_and_its_form_makes_a_lead(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    res = call_tool(client, "request_site_upload", {}, key=key)
    link = data(res)
    assert link["upload_url"].startswith(f"{AGENT}/s/su_")
    assert link["how"] == f"curl --fail-with-body -T site.zip {link['upload_url']}"
    assert link["limits"]["files"] == 300 and ".woff2" in link["limits"]["types"]
    assert link["snippet"] == f'<script async src="{AGENT}/t/admiry.js" data-page="{link["page_id"]}"></script>'
    say = res["structuredContent"]["say"]
    assert "30 minutes" in say and hosting_minimum.MINIMUM in say and not JARGON.search(say)
    stored = conn.execute(
        "SELECT result FROM agent_action WHERE tool = 'request_site_upload' ORDER BY id DESC LIMIT 1"
    ).fetchone()["result"]
    assert stored["data"]["upload_url"] == "***" and stored["data"]["how"] == "***"  # the link is a capability

    files = _site(link)
    r = _put(client, link, make_zip(files))
    assert r.status_code == 200, r.text
    ready = r.json()
    assert (ready["status"], ready["problems"], ready["files"]) == ("ready", [], len(files))
    assert ready["say"].startswith(bundles.READY) and ready["preview_url"] in ready["say"]
    preview = urlsplit(ready["preview_url"]).hostname
    assert preview.startswith("preview--") and preview.endswith(".business.test") and "pg_" not in preview

    first = _get(client, preview)
    assert first.status_code == 200 and first.text == files["index.html"]
    assert first.headers["x-robots-tag"] == "noindex" and first.headers["cache-control"] == "no-store"
    assert "set-cookie" not in first.headers
    assert _get(client, preview, "/about/").text == files["about/index.html"]

    prop = _publish(client, conn, monkeypatch, key, ready["token"], "1")
    assert prop["approval_url"] == ready["preview_url"] and prop["summary"].startswith("Publish the site at")
    live = _get(client, HOSTNAME)
    assert live.status_code == 200 and live.text == files["index.html"]  # served as uploaded: nothing injected
    sha = hashlib.sha256(files["index.html"].encode()).hexdigest()
    assert live.headers["etag"] == f'"{sha}"' and live.headers["cache-control"] == "public, max-age=300"
    assert live.headers["content-security-policy"] == hosted.csp(AGENT)
    assert _get(client, HOSTNAME, "/", **{"if-none-match": live.headers["etag"]}).status_code == 304
    assert _get(client, HOSTNAME, "/about").text == files["about/index.html"]
    assert "Bright Smile Dental" in _get(client, HOSTNAME, "/privacy").text  # Admiry's own notice
    assert (
        conn.execute("SELECT html FROM page WHERE business_id = %s", (biz,)).fetchone()["html"] == files["index.html"]
    )

    lead = client.post(
        f"/t/form/{link['page_id']}", data={"name": "Ana", "phone": "6505550199", "_intent": "appointment"}
    )
    assert lead.status_code == 200 and lead.json()["lead"] is True and lead.json()["conversion"] == "appointment"
    assert conn.execute("SELECT intent FROM lead WHERE business_id = %s", (biz,)).fetchone()["intent"] == "appointment"

    page = data(call_tool(client, "get_page", {}, key=key))["page"]
    assert (page["versions"][0]["kind"], page["versions"][0]["live"]) == ("bundle", True)
    assert page["upload"]["status"] == "ready" and page["upload"]["token"] == ready["token"]


def test_the_public_tool_hands_the_link(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    res = call_tool(client, "update_landing_page", {"action": "upload_site", "idempotency_key": "us1"}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res)["upload_url"].startswith(f"{AGENT}/s/su_")
    monkeypatch.delenv("AGENT_PAGES_DOMAIN")
    off = call_tool(client, "request_site_upload", {}, key=key)
    assert off["isError"] and tool_text(off).startswith("Hosting is not set up on this server")


# ---------------------------------------------------------------- the link and the body (Review Focus 5)


def test_the_link_is_single_use_and_bounded(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    link = _link(client, key)
    body = make_zip(_site(link))
    chunked = _put(client, link, iter([body]))  # no Content-Length
    assert chunked.status_code == 411 and chunked.json()["problems"][0]["rule"] == "upload"
    declared = _put(client, link, b"PK", headers={"content-length": str(bundles.MAX_ZIP_BYTES + 1)})
    assert declared.status_code == 413 and declared.json()["problems"][0]["rule"] == "zip_size"
    lying = _put(client, link, b"\0" * (bundles.MAX_ZIP_BYTES + 1), headers={"content-length": "10"})
    assert lying.status_code == 413  # the bytes are counted as they come, whatever the header said
    assert _put(client, link, body).status_code == 200  # none of those used the link up
    again = _put(client, link, body)
    assert again.status_code == 410 and again.json()["problems"][0]["message"] == site_upload.USED
    unknown = client.put("/s/su_never_minted", content=body)
    assert unknown.status_code == 404 and unknown.json()["problems"][0]["fix"] == site_upload.NEW_LINK
    late = _link(client, key)
    conn.execute(
        "UPDATE site_upload SET expires_at = now() - interval '1 minute' WHERE token = %s",
        (late["upload_url"].rsplit("/", 1)[1],),
    )
    expired = _put(client, late, body)
    assert expired.status_code == 410 and expired.json()["problems"][0]["message"] == site_upload.EXPIRED


def test_a_refused_upload_stores_nothing_and_the_status_shows_it(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    link = _link(client, key)
    files = _site(link)
    files["index.html"] = files["index.html"].replace(
        "</head>", '<script src="https://cdn.jsdelivr.net/npm/x.js"></script></head>'
    )
    r = _put(client, link, make_zip(files))
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "refused" and body["say"] == bundles.REFUSED
    assert [(p["file"], p["rule"]) for p in body["problems"]] == [("index.html", "script_host")]
    assert body["problems"][0]["fix"] == "Bundle the library into the site instead of loading it from a CDN."
    assert (_count(conn, "site_blob"), _count(conn, "page_preview"), _count(conn, "page_preview_file")) == (0, 0, 0)
    status = data(call_tool(client, "get_page", {}, key=key))  # no page is set yet: the upload is the status
    assert status["page"] is None and status["upload"]["status"] == "refused"
    assert status["upload"]["problems"][0]["rule"] == "script_host" and "token" not in status["upload"]


# ---------------------------------------------------------------- the preview host (Review Focus 6)


def test_the_preview_host_serves_the_latest_upload_only(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _serving(client)
    first = _upload(client, key, headline="Invisalign in Palo Alto, first")
    second = _upload(client, key, headline="Invisalign in Palo Alto, second")
    assert first["preview_url"] == second["preview_url"]  # one preview host per business
    preview = urlsplit(second["preview_url"]).hostname
    assert "second" in _get(client, preview).text
    stale = call_tool(client, "publish_page", {"idempotency_key": "pbs", "token": first["token"]}, key=key)
    assert stale["isError"] and tool_text(stale).startswith("This preview has expired or is unknown")
    assert client.get(f"/pages/ask?domain={preview}").status_code == 200
    assert client.get(f"/p/{second['token']}").status_code == 404  # a bundle's pages need a host of their own
    robots = _get(client, preview, "/robots.txt")
    assert robots.text == "User-agent: *\nDisallow: /\n" and robots.headers["x-robots-tag"] == "noindex"

    page_id = second["link"]["page_id"]
    for url in (f"https://{preview}/about/", "https://elsewhere.example.com/"):
        assert client.post("/t/event", data={"page": page_id, "kind": "visit", "url": url}).status_code == 204
    assert _count(conn, "page_event") == 1  # the preview's look is no visit; the other one counts

    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 minute'")
    gone = _get(client, preview)
    assert gone.status_code == 404 and gone.headers["x-robots-tag"] == "noindex"
    assert client.get(f"/pages/ask?domain={preview}").status_code == 404
    assert client.get("/pages/ask?domain=preview--0000000000000000.business.test").status_code == 404


# ---------------------------------------------------------------- serving by path (Review Focus 7)


def test_serving_by_path(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    ready = _upload(client, key)
    _publish(client, conn, monkeypatch, key, ready["token"], "1")
    js = _get(client, HOSTNAME, "/_next/static/chunks/app-4f3b2a1c9d.js")
    assert js.headers["content-type"] == "text/javascript; charset=utf-8"
    assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
    svg = _get(client, HOSTNAME, "/logo.svg")
    assert (
        svg.headers["content-type"] == "image/svg+xml" and svg.headers["content-security-policy"] == bundles.SVG_POLICY
    )
    assert svg.headers["x-content-type-options"] == "nosniff"
    image = _get(client, HOSTNAME, "/img/team.png")
    assert image.headers["content-type"] == "image/png" and image.headers["cache-control"] == "public, max-age=3600"
    missing = _get(client, HOSTNAME, "/no/such/page")
    assert missing.status_code == 404 and "That page is not here." in missing.text
    assert missing.headers["cache-control"] == "no-store"
    assert _get(client, HOSTNAME, "/robots.txt").text == hosted.ROBOTS_TXT  # the bundle has none: Admiry's
    assert client.post("/", headers={"host": HOSTNAME}).status_code == 404


def test_a_bundle_version_comes_back_after_a_single_page(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    ready = _upload(client, key)
    _publish(client, conn, monkeypatch, key, ready["token"], "1")
    again = call_tool(client, "publish_page", {"idempotency_key": "pb2", "token": ready["token"]}, key=key)
    assert data(again)["status"] == "already_published"  # H40 for a bundle: the same files are live
    prev = data(call_tool(client, "preview_page", {"html": GOOD_HTML}, key=key))
    _publish(client, conn, monkeypatch, key, prev["token"], "3")
    assert "Invisalign in Palo Alto" in _get(client, HOSTNAME).text
    assert _get(client, HOSTNAME, "/about/").status_code == 404  # a single page answers "/" alone
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pb4", "version": 1}, key=key))
    _checked(monkeypatch, conn, URL)
    done = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "cb4"}, key=key
    )
    assert not done["isError"], tool_text(done)
    assert _get(client, HOSTNAME, "/about/").status_code == 200
    kinds = [(v["version"], v["kind"]) for v in data(call_tool(client, "get_page", {}, key=key))["page"]["versions"]]
    assert kinds == [(3, "bundle"), (2, "html"), (1, "bundle")]


# ---------------------------------------------------------------- the source (Review Focus 8)


def test_the_source_of_a_bundle_is_its_files_and_a_zip(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
    google,
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    ready = _upload(client, key)
    _publish(client, conn, monkeypatch, key, ready["token"], "1")
    res = call_tool(client, "get_landing_page", {"view": "source"}, key=key)
    src = data(res)
    files = _site(ready["link"])
    assert (src["kind"], src["version"], src["hostname"], src["live"]) == ("bundle", 1, HOSTNAME, True)
    assert [f["path"] for f in src["files"]] == sorted(files) and "html" not in src
    assert {f["path"]: f["size"] for f in src["files"]}["index.html"] == len(files["index.html"].encode())
    say = res["structuredContent"]["say"]
    assert "6 files of version 1" in say and src["zip_url"] not in say and not JARGON.search(say)
    stored = conn.execute(
        "SELECT result FROM agent_action WHERE tool = 'get_page_source' ORDER BY id DESC LIMIT 1"
    ).fetchone()["result"]
    assert stored["data"]["zip_url"] == "***"
    got = client.get(urlsplit(src["zip_url"]).path)
    assert got.status_code == 200 and got.headers["content-disposition"] == f'attachment; filename="{HOSTNAME}.zip"'
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        assert sorted(zf.namelist()) == sorted(files) and zf.read("index.html").decode() == files["index.html"]
    conn.execute("UPDATE site_zip SET expires_at = now() - interval '1 minute'")
    assert client.get(urlsplit(src["zip_url"]).path).status_code == 404

    google.email = "owner@example.com"
    sign_in(client, f"/connect/site?b={biz}")
    download = client.get(f"/connect/site?b={biz}")
    assert (
        download.status_code == 200
        and download.headers["content-disposition"] == f'attachment; filename="{HOSTNAME}.zip"'
    )
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        assert sorted(zf.namelist()) == sorted(files)


# ---------------------------------------------------------------- the collection and the purge (B13)


def test_the_collection_frees_only_what_nothing_names(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    ready = _upload(client, key)
    _publish(client, conn, monkeypatch, key, ready["token"], "1")
    named = _count(conn, "site_blob")
    for orphan in (b"left behind", b"just uploaded"):
        conn.execute(
            "INSERT INTO site_blob (sha256, bytes, size) VALUES (%s, %s, %s)",
            (bundles.sha256(orphan), orphan, len(orphan)),
        )
    conn.execute("UPDATE site_blob SET touched_at = now() - interval '2 days'")
    conn.execute("UPDATE site_blob SET touched_at = now() WHERE sha256 = %s", (bundles.sha256(b"just uploaded"),))
    assert bundles.gc(conn) == 1  # the old orphan; the new one and every named file stay
    assert _count(conn, "site_blob") == named + 1

    # the purge, 90 days after a takedown for the minimum: the versions, previews and links go, the blobs follow
    conn.execute(
        "UPDATE page SET status = 'unpublished', taken_down_reason = 'hosting_minimum', "
        "purge_after = current_date - 1 WHERE business_id = %s",
        (biz,),
    )
    page = conn.execute("SELECT id FROM page WHERE business_id = %s", (biz,)).fetchone()
    assert hosting_minimum.purge(conn, biz, page["id"], {"purged": 0})
    after = conn.execute("SELECT preview_host, hostname FROM page WHERE id = %s", (page["id"],)).fetchone()
    assert (after["preview_host"], after["hostname"]) == (None, None)
    for table in ("page_version_file", "page_preview_file", "site_upload", "site_zip"):
        assert _count(conn, table) == 0, table
    conn.execute("UPDATE site_blob SET touched_at = now() - interval '2 days'")
    assert bundles.gc(conn) == named + 1 and _count(conn, "site_blob") == 0
