"""The render check from the agent's side (A3p-3 batch 4; plan 2026-10-04-agent-a3p-3-4-render-check.md R4-R8,
Review Focus 4-6), with the renderer stubbed by an httpx MockTransport (renderer/tests drive the real one): an upload
refused on the render's problems with its preview dropped, a renderer that does not answer, a render that passes and
its screenshots -- kept with the preview, carried onto the version, shown on the homepage card and attached to the
report e-mail -- and the live site's daily render telling its owner once."""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image

from agent import bundles, config, jobs, landing, render_check
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.test_bundles import make_zip
from agent.tests.test_home_api import IP
from agent.tests.test_hosting_publish import card  # noqa: F401 (fixture)
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_site_upload import _link, _publish, _put, _site
from agent.tests.test_tools_hosting import GOOD_HTML, HOSTNAME, URL, _hosted_biz, _mailbox

RENDERER = "http://renderer.test"


def _jpeg(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, "JPEG")
    return buf.getvalue()


PHONE, TABLET, DESKTOP = _jpeg("white"), _jpeg("gray"), _jpeg("black")
BROKEN = {
    "file": "/",
    "rule": "render",
    "message": "GET /_next/static/chunks/app.js returned 404.",
    "fix": "Remove basePath and assetPrefix from next.config.js: the site is served from the root of its own host.",
}


class Renderer:
    """The renderer's stand-in: what it answers next, and every request it got."""

    def __init__(self) -> None:
        self.problems: list[dict] = []
        self.status = 200
        self.asked: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{RENDERER}/render"
        self.asked.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "busy"})
        shots = {
            "phone": base64.b64encode(PHONE).decode(),
            "tablet": base64.b64encode(TABLET).decode(),
            "desktop": base64.b64encode(DESKTOP).decode(),
        }
        pages = [{"path": "/", "viewport": v, "status": 200} for v in ("phone", "tablet", "desktop")]
        return httpx.Response(200, json={"pages": pages, "problems": self.problems, "shots": shots, "partial": False})


@pytest.fixture
def renderer(monkeypatch) -> Renderer:
    monkeypatch.setenv("AGENT_RENDER_URL", RENDERER)
    r = Renderer()
    monkeypatch.setattr(render_check, "transport", lambda: httpx.MockTransport(r.handler))
    return r


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


# ---------------------------------------------------------------- in the upload (Review Focus 4)


def test_the_upload_is_refused_on_render_problems(
    client,
    conn,
    monkeypatch,
    renderer,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    link = _link(client, key)
    renderer.problems = [BROKEN]
    r = _put(client, link, make_zip(_site(link)))
    assert r.status_code == 422
    assert r.json() == {"status": "refused", "say": bundles.REFUSED, "problems": [BROKEN]}
    asked = renderer.asked[0]
    assert asked["url"].startswith("https://preview--") and asked["page_id"] == link["page_id"]
    assert _count(conn, "page_preview") == 0 and _count(conn, "page_preview_file") == 0  # the preview is gone
    upload = data(call_tool(client, "get_page", {}, key=key))["upload"]
    assert upload["status"] == "refused" and upload["problems"] == [BROKEN]

    renderer.status = 503
    down = _put(client, _link(client, key), make_zip(_site(link)))
    assert down.status_code == 503 and down.json()["problems"][0]["message"] == render_check.UNAVAILABLE
    assert _count(conn, "page_preview") == 0


def test_without_a_renderer_the_static_checks_decide(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    assert not render_check.on(config.load())
    link = _link(client, key)
    ready = _put(client, link, make_zip(_site(link))).json()
    assert ready["status"] == "ready" and ready["render"] is None


# ---------------------------------------------------------------- the screenshots (Review Focus 5)


def test_a_passing_render_keeps_its_screenshots_onto_the_version_and_the_report(
    client,
    conn,
    monkeypatch,
    renderer,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, _biz = _hosted_biz(client, conn, monkeypatch, fake)
    sent = _mailbox(monkeypatch)
    link = _link(client, key)
    r = _put(client, link, make_zip(_site(link)))
    assert r.status_code == 200, r.text
    ready = r.json()
    assert ready["render"] == {"loads": 3, "partial": False}
    row = conn.execute(
        "SELECT shot_phone, shot_tablet, shot_desktop FROM page_preview WHERE token = %s", (ready["token"],)
    ).fetchone()
    assert (bytes(row["shot_phone"]), bytes(row["shot_tablet"]), bytes(row["shot_desktop"])) == (PHONE, TABLET, DESKTOP)
    _publish(client, conn, monkeypatch, key, ready["token"], "1")
    page = landing.get(conn, link["page_id"])
    assert bundles.shot(conn, page["id"], page["version"], "phone") == PHONE
    assert bundles.shot(conn, page["id"], page["version"], "tablet") == TABLET
    assert bundles.shot(conn, page["id"], page["version"], "desktop") == DESKTOP
    report = json.loads(sent[-1].content)
    assert [a["filename"] for a in report["attachments"]] == [
        "site-phone.jpg",
        "site-tablet.jpg",
        "site-desktop.jpg",
    ]
    assert base64.b64decode(report["attachments"][0]["content"]) == PHONE
    assert "Attached: your site on a phone, on a tablet and on a computer" in report["text"]


def test_the_card_shows_the_assistants_site(client, conn, card):  # noqa: F811 - the fixture shadows the import
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    page = landing.ensure_page_for_kit(conn, biz)
    conn.execute(
        "UPDATE page SET source = 'hosted', status = 'published', hostname = %s, url = %s, html = %s, version = 1 "
        "WHERE id = %s",
        (HOSTNAME, URL, GOOD_HTML, page["id"]),
    )
    conn.execute(
        "INSERT INTO page_version (page_id, version, html, title, source, kind, shot_phone, shot_tablet) "
        "VALUES (%s, 1, %s, 't', 'hosted', 'bundle', %s, %s)",
        (page["id"], GOOD_HTML, PHONE, TABLET),
    )
    state = client.post("/api/page/state", json={"csrf": card}, headers=IP).json()["page"]
    assert state["managed_by"] == "assistant" and state["shot_url"] == "/api/page/shot?size=phone&v=1"
    shot = client.get("/api/page/shot?size=phone")
    assert shot.status_code == 200 and shot.content == PHONE and shot.headers["content-type"] == "image/jpeg"
    assert shot.headers["cache-control"] == "private, max-age=300"
    tablet = client.get("/api/page/shot?size=tablet")
    assert tablet.status_code == 200 and tablet.content == TABLET and tablet.headers["content-type"] == "image/jpeg"
    assert client.get("/api/page/shot?size=desktop").status_code == 404  # none taken at that size
    assert client.get("/api/page/shot?size=watch").status_code == 404
    assert client.get("/api/page/shot?size=huge").status_code == 404
    client.cookies.clear()
    assert client.get("/api/page/shot?size=phone").status_code == 404  # the owner's own only


# ---------------------------------------------------------------- after publishing (Review Focus 6)


def test_the_live_site_is_rendered_daily_and_the_owner_told_once(
    client,
    conn,
    monkeypatch,
    renderer,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    sent = _mailbox(monkeypatch)
    link = _link(client, key)
    ready = _put(client, link, make_zip(_site(link))).json()
    _publish(client, conn, monkeypatch, key, ready["token"], "1")
    monkeypatch.setattr(landing, "reachable", lambda url, **kwargs: (True, "200"))
    renderer.problems = [BROKEN]
    before = len(sent)
    out = jobs.page_check(conn, 0, 1)
    assert (out["rendered"], out["render_mailed"], out["render_errors"]) == (1, 1, 0)
    assert renderer.asked[-1]["url"] == URL
    mailed = json.loads(sent[-1].content)
    assert len(sent) == before + 1 and mailed["subject"] == f"Something on your site {HOSTNAME} needs fixing"
    assert BROKEN["message"] in mailed["text"]
    view = data(call_tool(client, "get_page", {}, key=key))["page"]["render"]
    assert view["problems"] == [BROKEN] and view["at"]
    assert jobs.page_check(conn, 0, 1)["rendered"] == 0  # once a day
    conn.execute(
        "UPDATE page SET rendered_at = %s WHERE business_id = %s", (datetime.now(UTC) - timedelta(days=2), biz)
    )
    again = jobs.page_check(conn, 0, 1)
    assert (again["rendered"], again["render_mailed"]) == (1, 0)  # still broken: told once
    renderer.status = 500
    conn.execute("UPDATE page SET rendered_at = NULL WHERE business_id = %s", (biz,))
    assert jobs.page_check(conn, 0, 1)["render_errors"] == 1
    result = call_tool(client, "get_page", {}, key=key)
    assert not result["isError"], tool_text(result)
