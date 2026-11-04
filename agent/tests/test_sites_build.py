"""An industry site built into a bundle preview (A3p-6 batch 1 Task 6; plan 2026-10-06-agent-a3p-6-1-industry-site-templates
Q6, Q8, Q21, Review Focus "two businesses / a second build" and "partial"): the sample roofer rendered through A3p-3's
bundle pipeline -- the static checks, one blob per distinct file, the bundle preview and its `site_spec` row, served on
the business's preview host -- then the render check at three sizes (its problems and an absent renderer drop the
preview), Google's speed test (held back under 50, logged under 80, Google down is no fault of the template), the font
files' CORS header, a build without the render check (a photo change), and publishing -- held until the site's logo (or
the wordmark the owner accepted) and the family's key photo are in, and until the preview carries them (A3p-6 batch 2
Task 2; plan 2026-10-06-agent-a3p-6-2-facts-photos-publish Q9, Q11)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import bundles, config, db, hosted, landing, pagespeed, proposals, render_check
from agent.sites import build, gallery, intake, photos, samples
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, call_tool, data, tool_text
from agent.tests.test_home_api import IP
from agent.tests.test_hosting_publish import card  # noqa: F401 (fixture)
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_render_check import BROKEN, DESKTOP, PHONE, RENDERER, TABLET, Renderer
from agent.tests.test_site_upload import _publish, _serving, _upload
from agent.tests.test_sites_dental import DENTAL_LIBRARY
from agent.tests.test_sites_render import add_photo, jpeg, library, load_library, logo_png  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import DOMAIN, HOSTNAME, _hosted_biz, _mailbox

AGENT = "http://testserver"
KEY = "psi-test-SECRET-key"
PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


# ---------------------------------------------------------------- stand-ins


class PartialRenderer(Renderer):
    """The renderer's stand-in, answering that its time ran out after the home page."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        answer = super().handler(request)
        body = json.loads(answer.content)
        return httpx.Response(answer.status_code, json={**body, "partial": True})


ROOFER_PAGES = [
    "/",
    "/about/",
    "/services/",
    "/contact/",
    "/services/roof-replacement/",
    "/services/roof-repair/",
    "/services/storm-damage/",
]


class FullRenderer(Renderer):
    """The renderer's stand-in, answering that it opened every page of the roofer's site."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        answer = super().handler(request)
        body = json.loads(answer.content)
        pages = [
            {"path": p, "viewport": v, "status": 200} for p in ROOFER_PAGES for v in ("phone", "tablet", "desktop")
        ]
        return httpx.Response(answer.status_code, json={**body, "pages": pages})


@pytest.fixture
def renderer(monkeypatch) -> Renderer:
    monkeypatch.setenv("AGENT_RENDER_URL", RENDERER)
    r = Renderer()
    monkeypatch.setattr(render_check, "transport", lambda: httpx.MockTransport(r.handler))
    return r


class Speed:
    """Google's PageSpeed Insights, stood in: the performance score it answers next (0-100), its status, every call."""

    def __init__(self) -> None:
        self.performance = 92
        self.status = 200
        self.asked: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"code": self.status}})
        categories = {
            "performance": {"score": self.performance / 100},
            "accessibility": {"score": 0.97},
            "best-practices": {"score": 1.0},
            "seo": {"score": 0.69},
        }
        audits = {
            "largest-contentful-paint": {"numericValue": 2412.6},
            "cumulative-layout-shift": {"numericValue": 0.01234},
        }
        return httpx.Response(
            200,
            json={
                "lighthouseResult": {
                    "fetchTime": "2026-10-06T12:00:00.000Z",
                    "categories": categories,
                    "audits": audits,
                }
            },
        )


@pytest.fixture
def speed(monkeypatch) -> Speed:
    monkeypatch.setenv("AGENT_PAGESPEED_API_KEY", KEY)
    s = Speed()
    monkeypatch.setattr(pagespeed, "transport", lambda: httpx.MockTransport(s.handler))
    return s


# ---------------------------------------------------------------- helpers


def _biz(conn, monkeypatch, biz: str = "biz_roofer") -> str:
    """An active business with hosting on (no plan: a site preview needs none)."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
    return biz


def _build(conn, biz: str, style: str = "solid", *, sample: str = "roofer", **kw) -> build.Built:
    return build.preview(conn, config.load(), biz, samples.load(sample, style=style), sample=sample, **kw)


SAMPLE_STYLE = {"roofer": "solid", "dentist": "calm"}


def _own(conn, biz: str, kind: str) -> str:
    """An owner's photo of `kind` put on the business's page as the upload link will (Task 3; the tests put the rows
    in directly): a logo is a PNG with an alpha channel, any other photo a JPEG, each with bytes of its own. Returns
    the photo's id."""
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    photo_id, n = db.new_id("ph"), _count(conn, "page_photo")
    if kind == "logo":
        add_photo(conn, page_id, photo_id, logo_png(shade=100 + n), kind=kind, content_type="image/png")
    else:
        add_photo(conn, page_id, photo_id, jpeg(900, 600, 40 + 10 * n), kind=kind)
    return photo_id


def _build_own(conn, biz: str, *, sample: str = "roofer", **kw) -> build.Built:
    """The sample with the owner's photos now on its page named in its `images`, as a build of the site names them."""
    site = samples.load(sample, style=SAMPLE_STYLE[sample])
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    site = site.model_copy(update={"images": photos.images(conn, page_id, site.family)})
    return build.preview(conn, config.load(), biz, site, sample=sample, **kw)


def _gate(conn, built: build.Built) -> str | None:
    return build.publish_gate(conn, hosted.preview(conn, built.token))


def _named(conn, built: build.Built) -> dict:
    """The images, by slot, that the document this preview was built from names."""
    spec_id = hosted.preview(conn, built.token)["sections"]["site_spec_id"]
    return conn.execute("SELECT data FROM site_spec WHERE id = %s", (spec_id,)).fetchone()["data"]["images"]


@pytest.fixture
def dental_library(conn, tmp_path):
    return load_library(conn, tmp_path, DENTAL_LIBRARY, "dental")


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def _host(built: build.Built) -> str:
    return urlsplit(built.preview_url).hostname


def _get(client, host: str, path: str = "/"):
    return client.get(path, headers={"host": host})


def _page_id(conn, biz: str) -> str:
    return conn.execute("SELECT id FROM page WHERE business_id = %s", (biz,)).fetchone()["id"]


# ---------------------------------------------------------------- the build (Q6)


def test_the_roofer_in_solid_becomes_the_businesss_bundle_preview(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    renderer,
    fake,  # noqa: F811
):
    _key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _serving(client)
    built = _build(conn, biz)
    page_id = _page_id(conn, biz)

    files = bundles.preview_files(conn, built.token)
    assert built.files == len(files) and built.preview_url == f"https://{_host(built)}/"
    assert _host(built).startswith("preview--") and _host(built).endswith("." + DOMAIN)
    # one blob per distinct file, every file's blob there
    shas = set(files.values())
    kept = conn.execute("SELECT sha256, size FROM site_blob WHERE sha256 = ANY(%s)", (list(shas),)).fetchall()
    assert len(kept) == len(shas)
    size = {r["sha256"]: r["size"] for r in kept}
    assert built.bytes == sum(size[sha] for sha in files.values())  # the pages, the assets and the stock photos

    prev = hosted.preview(conn, built.token)
    assert prev["kind"] == "bundle" and prev["page_id"] == page_id
    sections = prev["sections"]
    spec_row = conn.execute("SELECT * FROM site_spec WHERE page_id = %s", (page_id,)).fetchone()
    assert (spec_row["version"], spec_row["family"], spec_row["style"], spec_row["business_id"]) == (
        1,
        "home_services",
        "solid",
        biz,
    )
    assert spec_row["data"] == samples.load("roofer", style="solid").model_dump(mode="json")
    assert (
        conn.execute("SELECT site_spec_id FROM page WHERE id = %s", (page_id,)).fetchone()["site_spec_id"]
        == (spec_row["id"])
    )
    assert {k: sections[k] for k in ("kind", "site_spec_id", "family", "style", "sample")} == {
        "kind": "site",
        "site_spec_id": spec_row["id"],
        "family": "home_services",
        "style": "solid",
        "sample": "roofer",
    }
    assert sections["pagespeed"] is None and built.pagespeed is None  # the key is unset

    # the render check at three sizes, asked for the preview's own address
    assert renderer.asked == [{"url": built.preview_url, "page_id": page_id, "max_pages": 10}]
    assert built.render == sections["render"]
    assert (
        built.render["loads"] == 3
        and built.render["partial"] is False
        and set(built.render["not_opened"]) == set(ROOFER_PAGES[1:])
    )
    shots = conn.execute(
        "SELECT shot_phone, shot_tablet, shot_desktop FROM page_preview WHERE token = %s", (built.token,)
    ).fetchone()
    assert (bytes(shots["shot_phone"]), bytes(shots["shot_tablet"]), bytes(shots["shot_desktop"])) == (
        PHONE,
        TABLET,
        DESKTOP,
    )
    assert any("speed test is not set up" in w for w in built.warnings)

    # the preview host serves the site by path, with the live site's headers plus noindex
    host = _host(built)
    for path, stored in (("/", "index.html"), ("/about/", "about/index.html")):
        r = _get(client, host, path)
        assert r.status_code == 200 and r.headers["x-robots-tag"] == "noindex", path
        assert r.headers["content-security-policy"] == hosted.csp(AGENT) and r.headers["cache-control"] == "no-store"
        assert bundles.sha256(r.content) == files[stored]
        assert "access-control-allow-origin" not in r.headers
    service = _get(client, host, "/services/roof-repair/")
    assert service.status_code == 200 and "Roof repair in Charlotte" in service.text
    css = next(p for p in files if p.startswith("assets/site-") and p.endswith(".css"))
    sheet = _get(client, host, "/" + css)
    assert sheet.status_code == 200 and sheet.headers["content-type"].startswith("text/css")
    assert sheet.headers["x-robots-tag"] == "noindex" and "access-control-allow-origin" not in sheet.headers
    assert bundles.cache_control(css) == f"public, max-age={bundles.HASHED_MAX_AGE}, immutable"  # a year, live
    stock = next(p for p in files if p.startswith("assets/img/") and p.endswith(".webp"))
    photo = _get(client, host, "/" + stock)
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/webp"
    assert photo.headers["x-robots-tag"] == "noindex" and "access-control-allow-origin" not in photo.headers
    assert bundles.cache_control(stock).startswith(f"public, max-age={bundles.HASHED_MAX_AGE}")
    # the stock photo's blob is the library's own, not a copy
    assert conn.execute("SELECT 1 FROM site_asset_file WHERE sha256 = %s", (files[stock],)).fetchone()


def test_a_font_is_served_with_cors_for_the_sandboxed_preview(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
):
    biz = _biz(conn, monkeypatch)
    _serving(client)
    built = _build(conn, biz)
    files = bundles.preview_files(conn, built.token)
    font = next(p for p in files if p.endswith(".woff2"))
    r = _get(client, _host(built), "/" + font)
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and r.content[:4] == b"wOF2"
    assert r.headers["access-control-allow-origin"] == "*"
    # the live site's serving is the same function: a font there carries it too, a page never
    live = bundles.respond(conn, ("preview", built.token), "/" + font, {}, preview=False)
    assert live.headers["access-control-allow-origin"] == "*"
    page = bundles.respond(conn, ("preview", built.token), "/", {}, preview=False)
    assert "access-control-allow-origin" not in page.headers


def test_hosting_off_or_a_business_not_active_builds_nothing(conn, monkeypatch, library):  # noqa: F811
    conn.execute("INSERT INTO business (id) VALUES ('biz_off')")
    with pytest.raises(ToolError, match="Hosting is not set up on this server."):
        _build(conn, "biz_off")
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    conn.execute("UPDATE business SET status = 'expired' WHERE id = 'biz_off'")
    with pytest.raises(ToolError, match="This business is not active."):
        _build(conn, "biz_off")
    assert _count(conn, "page_preview") == 0 and _count(conn, "site_spec") == 0


def test_a_static_check_problem_is_admirys_template_fault(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    real = build.render.bundle

    def broken(*args, **kwargs):
        out = real(*args, **kwargs)
        out.files["about/index.html"] = b"<!doctype html><html><body><p>No script tag here.</p></body></html>"
        return out

    monkeypatch.setattr(build.render, "bundle", broken)
    with pytest.raises(build.TemplateFault) as e:
        _build(conn, biz)
    assert [p["rule"] for p in e.value.problems] == ["tracking"]
    assert e.value.problems[0]["file"] == "about/index.html"
    assert _count(conn, "page_preview") == 0 and _count(conn, "site_spec") == 0


# ---------------------------------------------------------------- a second build, two businesses (Review Focus)


def test_a_second_build_ends_the_first_and_two_businesses_keep_their_own(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
):
    a = _biz(conn, monkeypatch, "biz_a")
    b = _biz(conn, monkeypatch, "biz_b")
    _serving(client)
    first = _build(conn, a)
    blobs = _count(conn, "site_blob")
    other = _build(conn, b, "bold")
    second = _build(conn, a, "clean")

    # one live preview per business: the second build ended the first; the other business's stands
    assert hosted.preview(conn, first.token) is None
    assert hosted.preview(conn, second.token) is not None and hosted.preview(conn, other.token) is not None
    assert _host(first) == _host(second) != _host(other)
    versions = conn.execute(
        "SELECT version, style FROM site_spec WHERE business_id = %s ORDER BY version", (a,)
    ).fetchall()
    assert [(r["version"], r["style"]) for r in versions] == [(1, "solid"), (2, "clean")]
    newest = conn.execute("SELECT id FROM site_spec WHERE business_id = %s AND version = 2", (a,)).fetchone()["id"]
    assert conn.execute("SELECT site_spec_id FROM page WHERE business_id = %s", (a,)).fetchone()["site_spec_id"] == (
        newest
    )

    # the stock photos they share are the library's blobs: no second copy, nothing new stored for them
    f1, f2, fb = (bundles.preview_files(conn, t.token) for t in (first, second, other))
    stock1 = {sha for p, sha in f1.items() if p.startswith("assets/img/")}
    stock2 = {sha for p, sha in f2.items() if p.startswith("assets/img/")}
    assert stock1 == stock2  # the business keeps its photos across rebuilds (stock.pick seeded by it)
    stock_b = {sha for p, sha in fb.items() if p.startswith("assets/img/")}
    library_blobs = {r["sha256"] for r in conn.execute("SELECT sha256 FROM site_asset_file").fetchall()}
    assert stock2 | stock_b <= library_blobs  # every stock photo either site shows is a library blob
    made = {sha for f in (f2, fb) for p, sha in f.items() if not p.startswith("assets/img/")} - set(f1.values())
    assert _count(conn, "site_blob") == blobs + len(made)  # only the pages and assets that differ are new

    # each preview host serves its own business's site
    assert bundles.sha256(_get(client, _host(second)).content) == f2["index.html"]
    assert bundles.sha256(_get(client, _host(other)).content) == fb["index.html"]
    assert f2["index.html"] != fb["index.html"]

    # a day later the collection frees nothing either business's preview names
    conn.execute("UPDATE site_blob SET touched_at = now() - interval '2 days'")
    bundles.gc(conn)
    named = set(f2.values()) | set(fb.values())
    left = conn.execute("SELECT count(*) AS n FROM site_blob WHERE sha256 = ANY(%s)", (list(named),)).fetchone()
    assert left["n"] == len(named)


# ---------------------------------------------------------------- the render check (Q6, Review Focus "partial")


def test_the_renders_problems_drop_the_preview_as_a_template_fault(conn, monkeypatch, library, renderer):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    renderer.problems = [BROKEN]
    with pytest.raises(build.TemplateFault) as e:
        _build(conn, biz)
    assert e.value.problems == [BROKEN]
    assert _count(conn, "page_preview") == 0 and _count(conn, "page_preview_file") == 0


def test_the_renderer_down_drops_the_preview_and_says_so(conn, monkeypatch, library, renderer):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    renderer.status = 503
    with pytest.raises(render_check.Unavailable):
        _build(conn, biz)
    assert _count(conn, "page_preview") == 0


def test_a_partial_render_is_kept_and_names_the_pages_not_opened(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    monkeypatch.setenv("AGENT_RENDER_URL", RENDERER)
    r = PartialRenderer()
    monkeypatch.setattr(render_check, "transport", lambda: httpx.MockTransport(r.handler))
    built = _build(conn, biz)
    sections = hosted.preview(conn, built.token)["sections"]
    assert built.render["partial"] is True and sections["render"]["partial"] is True
    assert sections["render"]["pages"] == ["/"]
    assert set(sections["render"]["not_opened"]) == {
        "/about/",
        "/services/",
        "/contact/",
        "/services/roof-replacement/",
        "/services/roof-repair/",
        "/services/storm-damage/",
    }
    assert built.render == sections["render"]


def test_without_a_renderer_the_static_checks_decide(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    built = _build(conn, biz)
    assert built.render is None and hosted.preview(conn, built.token)["sections"]["render"] is None
    assert any("render check is not set up" in w for w in built.warnings)


def test_a_preview_ttl_is_the_callers(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    built = _build(conn, biz, ttl=timedelta(days=7))
    row = conn.execute(
        "SELECT expires_at - created_at AS ttl FROM page_preview WHERE token = %s", (built.token,)
    ).fetchone()
    assert timedelta(days=7) - timedelta(seconds=5) < row["ttl"] <= timedelta(days=7)


# ---------------------------------------------------------------- Google's speed test (Q8)


def test_a_good_speed_score_is_kept_with_the_preview(conn, monkeypatch, library, speed, caplog):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    caplog.set_level(logging.DEBUG)
    built = _build(conn, biz)
    asked = speed.asked[0]
    assert str(asked.url).split("?")[0] == PSI
    query = parse_qs(urlsplit(str(asked.url)).query)
    assert query["url"] == [built.preview_url] and query["strategy"] == ["MOBILE"]
    assert query["category"] == ["PERFORMANCE", "ACCESSIBILITY", "BEST_PRACTICES", "SEO"]
    assert asked.headers["x-goog-api-key"] == KEY and "key" not in query  # never in a URL a log could print
    scores = {
        "performance": 92,
        "accessibility": 97,
        "best_practices": 100,
        "seo": 69,
        "lcp_ms": 2413,
        "cls": 0.012,
        "fetched_at": "2026-10-06T12:00:00.000Z",
    }
    assert built.pagespeed == scores and hosted.preview(conn, built.token)["sections"]["pagespeed"] == scores
    assert built.warnings == [w for w in built.warnings if "speed" not in w]
    assert KEY not in caplog.text


def test_a_speed_score_under_50_holds_the_preview_back(conn, monkeypatch, library, speed):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    speed.performance = 45
    with pytest.raises(build.TemplateFault) as e:
        _build(conn, biz)
    assert e.value.problems == [
        {
            "file": "/",
            "rule": "speed",
            "message": "Google's speed test gave the home page 45 of 100 on a phone; Admiry holds a preview back under 50.",
            "fix": "Lighten Admiry's template: the LCP was 2413 ms.",
        }
    ]
    assert _count(conn, "page_preview") == 0


def test_a_speed_score_under_80_is_kept_and_logged(conn, monkeypatch, library, speed, caplog):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    speed.performance = 70
    with caplog.at_level(logging.WARNING, logger="agent.sites.build"):
        built = _build(conn, biz)
    host = _host(built)
    assert f"site build: pagespeed 70 for home_services/solid on {host} (lcp 2413 ms)" in caplog.messages
    assert built.pagespeed["performance"] == 70 and hosted.preview(conn, built.token) is not None
    assert any("70 of 100" in w for w in built.warnings)


def test_google_not_answering_keeps_the_preview(conn, monkeypatch, library, speed, caplog):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    speed.status = 500
    with caplog.at_level(logging.WARNING):
        built = _build(conn, biz)
    assert built.pagespeed == {"error": "unavailable"}
    assert hosted.preview(conn, built.token)["sections"]["pagespeed"] == {"error": "unavailable"}
    assert KEY not in caplog.text


def test_the_key_unset_never_calls_google(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        pagespeed, "transport", lambda: httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(500))[1])
    )
    assert not pagespeed.on(config.load())
    built = _build(conn, biz)
    assert calls == [] and built.pagespeed is None


def test_the_speed_client_reads_an_answer_without_scores_as_unavailable(monkeypatch):
    monkeypatch.setenv("AGENT_PAGESPEED_API_KEY", KEY)
    monkeypatch.setattr(
        pagespeed,
        "transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(200, json={"lighthouseResult": {"categories": {}}})),
    )
    with pytest.raises(pagespeed.Unavailable):
        pagespeed.score(config.load(), "https://preview--x.business.test/")
    assert (pagespeed.REFUSE_UNDER, pagespeed.WARN_UNDER) == (50, 80)
    assert KEY not in repr(config.load())


# ---------------------------------------------------------------- a build without the render check (Q9)


def test_a_static_build_runs_neither_the_render_check_nor_the_speed_test(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,
    speed,
):
    biz = _biz(conn, monkeypatch)
    _serving(client)
    built = _build(conn, biz, checks="static")

    assert renderer.asked == [] and speed.asked == []  # neither the renderer nor Google was asked
    assert built.render is None and built.pagespeed is None
    assert built.warnings == ["built without the render check (a photo change)"] == [build.STATIC_ONLY]
    sections = hosted.preview(conn, built.token)["sections"]
    assert sections["render"] is None and sections["pagespeed"] is None
    # the preview is kept whole, as a full build keeps it: its files, its document, its host -- and no screenshots
    assert built.files == len(bundles.preview_files(conn, built.token))
    assert conn.execute("SELECT version FROM site_spec WHERE business_id = %s", (biz,)).fetchone()["version"] == 1
    served = _get(client, _host(built))
    assert served.status_code == 200 and served.headers["x-robots-tag"] == "noindex"
    shots = conn.execute(
        "SELECT shot_phone, shot_tablet, shot_desktop FROM page_preview WHERE token = %s", (built.token,)
    ).fetchone()
    assert (shots["shot_phone"], shots["shot_tablet"], shots["shot_desktop"]) == (None, None, None)


def test_a_static_build_says_so_with_nothing_set_up_either(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    built = _build(conn, biz, checks="static")
    assert built.warnings == [build.STATIC_ONLY]  # not "the render check is not set up": it was not asked for


def test_a_static_build_still_runs_the_static_checks(conn, monkeypatch, library, renderer):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    real = build.render.bundle

    def broken(*args, **kwargs):
        out = real(*args, **kwargs)
        out.files["about/index.html"] = b"<!doctype html><html><body><p>No script tag here.</p></body></html>"
        return out

    monkeypatch.setattr(build.render, "bundle", broken)
    with pytest.raises(build.TemplateFault) as e:
        _build(conn, biz, checks="static")
    assert [p["rule"] for p in e.value.problems] == ["tracking"] and renderer.asked == []
    assert _count(conn, "page_preview") == 0 and _count(conn, "site_spec") == 0


def test_a_renderer_that_is_down_does_not_stop_a_static_build(conn, monkeypatch, library, renderer):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    renderer.status = 503
    built = _build(conn, biz, checks="static")
    assert hosted.preview(conn, built.token) is not None and renderer.asked == []


def test_a_build_says_the_version_of_the_document_it_wrote(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    first = _build(conn, biz)
    second = _build(conn, biz, "clean", checks="static")

    assert (first.version, second.version) == (1, 2)
    rows = conn.execute("SELECT id, version FROM site_spec WHERE business_id = %s ORDER BY version", (biz,)).fetchall()
    assert [r["version"] for r in rows] == [1, 2]
    assert hosted.preview(conn, second.token)["sections"]["site_spec_id"] == rows[1]["id"]


def test_the_checks_are_full_or_static_and_nothing_else(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    with pytest.raises(ValueError, match="checks"):
        _build(conn, biz, checks="quick")
    assert _count(conn, "page_preview") == 0 and _count(conn, "site_spec") == 0


# ---------------------------------------------------------------- publishing: the key photos' gate (Q11, T13)

HOME_BOTH = "Your site needs your logo and one photo of your own work before it goes live."
HOME_KEY = "Your site needs one photo of your own work before it goes live."
NEEDS_LOGO = "Your site needs your logo before it goes live."
DENTAL_BOTH = "Your site needs your logo and one photo of your office before it goes live."
DENTAL_KEY = "Your site needs one photo of your office before it goes live."
REBUILD = "Your photos are in; build the site once more, then publish."
GALLERY = "This preview is Admiry's gallery of looks; it is not published."
PUBLISH_FAILED = "Admiry couldn't publish your site just now. Try again in a few minutes."  # Task 5's wording


def test_the_gates_words_are_the_plans_and_the_old_refusal_is_gone():
    assert build.REBUILD_FIRST == REBUILD and build.GALLERY == GALLERY and build.PUBLISH_FAILED == PUBLISH_FAILED
    assert not hasattr(build, "NOT_YET")
    for sentence in (REBUILD, GALLERY, PUBLISH_FAILED, HOME_BOTH, HOME_KEY, NEEDS_LOGO, DENTAL_BOTH, DENTAL_KEY):
        assert not (JARGON.search(sentence) or ACCOUNT_JARGON.search(sentence) or MONEY_JARGON.search(sentence))


def test_a_site_preview_of_a_page_with_no_photos_waits_for_the_logo_and_one_photo(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    assert _gate(conn, _build(conn, biz)) == HOME_BOTH


def test_a_logo_alone_waits_for_the_key_photo_and_a_key_photo_alone_for_the_logo(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    logo = _own(conn, biz, "logo")
    assert _gate(conn, _build_own(conn, biz)) == HOME_KEY
    conn.execute("DELETE FROM page_photo WHERE id = %s", (logo,))
    _own(conn, biz, "key")
    assert _gate(conn, _build_own(conn, biz)) == NEEDS_LOGO


def test_a_photo_that_says_nothing_of_what_it_shows_or_is_another_pages_is_no_key_photo(
    conn,
    monkeypatch,
    library,  # noqa: F811
):
    biz = _biz(conn, monkeypatch)
    other = _biz(conn, monkeypatch, "biz_other")
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    add_photo(conn, page_id, "ph_nokind", jpeg(900, 600, 77))  # a starter page's photo: no kind
    _own(conn, other, "logo")
    _own(conn, other, "key")  # the other business's own, which this site never names
    assert _gate(conn, _build_own(conn, biz)) == HOME_BOTH


def test_the_wordmark_the_owner_accepted_stands_for_the_logo(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    intake.update_draft(conn, page_id, {"wordmark_ok": True})
    assert _gate(conn, _build_own(conn, biz)) == HOME_KEY  # the wordmark alone is not the key photo
    _own(conn, biz, "key")
    assert _gate(conn, _build_own(conn, biz)) is None  # the wordmark and the key photo publish
    assert _gate(conn, _build_own(conn, biz, checks="static")) is None


def test_a_logo_and_a_key_photo_both_named_by_the_preview_publish(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    logo, key = _own(conn, biz, "logo"), _own(conn, biz, "key")
    built = _build_own(conn, biz)
    named = _named(conn, built)
    assert (named["logo"]["id"], named["hero"]["id"]) == (logo, key)
    assert _gate(conn, built) is None


def test_a_photo_the_preview_does_not_carry_waits_for_the_site_to_be_built_again(conn, monkeypatch, library):  # noqa: F811
    """The rebuild case (Review Focus): the owner's photos are in but the preview was built before them (or the build
    that should have put them in failed): publishing it would publish a site without the photos it is held for."""
    biz = _biz(conn, monkeypatch)
    _own(conn, biz, "logo")
    built = _build_own(conn, biz)  # names the logo alone
    assert _gate(conn, built) == HOME_KEY
    key = _own(conn, biz, "key")
    assert _gate(conn, built) == REBUILD  # both are on file now; this preview does not carry the key photo
    assert _gate(conn, _build_own(conn, biz, checks="static")) is None  # the rebuild a photo change makes
    # a key photo put in place of the one the preview names: the preview still shows the old one
    again = _build_own(conn, biz)
    conn.execute("DELETE FROM page_photo WHERE id = %s", (key,))
    new = _own(conn, biz, "key")
    assert _gate(conn, again) == REBUILD
    rebuilt = _build_own(conn, biz)
    assert _gate(conn, rebuilt) is None and _named(conn, rebuilt)["hero"]["id"] == new


def test_a_logo_put_in_after_the_build_or_taken_out_after_it_is_a_stale_preview_too(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    intake.update_draft(conn, page_id, {"wordmark_ok": True})
    _own(conn, biz, "key")
    wordmark = _build_own(conn, biz)
    assert _gate(conn, wordmark) is None
    _own(conn, biz, "logo")  # the owner added a logo after the wordmark was accepted: the preview shows no logo
    assert _gate(conn, wordmark) == REBUILD
    with_logo = _build_own(conn, biz, checks="static")
    assert _gate(conn, with_logo) is None
    conn.execute("DELETE FROM page_photo WHERE kind = 'logo'")  # and took it out again: the preview still shows it
    assert _gate(conn, with_logo) == REBUILD
    assert _gate(conn, _build_own(conn, biz, checks="static")) is None  # the wordmark again


def test_only_the_owners_photo_counts_as_the_one_the_preview_shows(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    built = _build_own(conn, biz)
    assert _gate(conn, built) is None
    conn.execute(  # a stock photo in the key slot, whatever id it carries, is not the owner's photo
        "UPDATE site_spec SET data = jsonb_set(data, '{images,hero,source}', '\"stock\"') WHERE business_id = %s",
        (biz,),
    )
    assert _gate(conn, built) == REBUILD


def test_a_preview_whose_document_is_gone_is_held_for_a_rebuild(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    built = _build_own(conn, biz)
    assert _gate(conn, built) is None
    conn.execute(
        "UPDATE page_preview SET sections = sections || %s WHERE token = %s",
        (Jsonb({"site_spec_id": "ss_gone"}), built.token),
    )
    assert _gate(conn, built) == REBUILD


@pytest.mark.parametrize(
    "kinds, sentence",
    [
        ((), DENTAL_BOTH),
        (("logo",), DENTAL_KEY),
        (("key",), NEEDS_LOGO),
        (("logo", "key"), None),
        (("logo", "place", "team", "dentist"), DENTAL_KEY),  # everything but the one the site is held for
    ],
)
def test_a_dental_sites_gate_asks_for_a_photo_of_the_office(conn, monkeypatch, dental_library, kinds, sentence):
    biz = _biz(conn, monkeypatch, "biz_dentist")
    for kind in kinds:
        _own(conn, biz, kind)
    built = _build_own(conn, biz, sample="dentist")
    assert _gate(conn, built) == sentence
    if sentence is None:
        assert set(_named(conn, built)) == {"logo", "office-1"}  # the key photo is the office's first, no dental switch


def test_a_dental_site_built_before_its_office_photo_waits_for_the_site_to_be_built_again(
    conn,
    monkeypatch,
    dental_library,
):
    biz = _biz(conn, monkeypatch, "biz_dentist")
    _own(conn, biz, "logo")
    built = _build_own(conn, biz, sample="dentist")
    office = _own(conn, biz, "key")
    assert _gate(conn, built) == REBUILD  # the key photo is the office's first slot, which this preview left to stock
    rebuilt = _build_own(conn, biz, sample="dentist", checks="static")
    assert _gate(conn, rebuilt) is None
    conn.execute("DELETE FROM page_photo WHERE id = %s", (office,))
    new = _own(conn, biz, "key")
    assert _gate(conn, rebuilt) == REBUILD  # replaced by another: the preview still shows the old one
    assert _gate(conn, _build_own(conn, biz, sample="dentist", checks="static")) is None
    assert photos.images(conn, landing.ensure_page_for_kit(conn, biz)["id"], "dental")["office-1"].id == new


def test_a_dental_site_with_the_wordmark_and_the_office_photo_publishes(conn, monkeypatch, dental_library):
    biz = _biz(conn, monkeypatch, "biz_dentist")
    intake.update_draft(conn, landing.ensure_page_for_kit(conn, biz)["id"], {"wordmark_ok": True})
    _own(conn, biz, "key")
    assert _gate(conn, _build_own(conn, biz, sample="dentist")) is None


def test_the_gate_reads_the_family_from_the_preview_never_from_the_business(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    _key, biz = _hosted_biz(client, conn, monkeypatch, fake)  # a dental practice's business ...
    assert _gate(conn, _build(conn, biz)) == HOME_BOTH  # ... with a roofer's site: the roofer's sentence


def test_a_gallery_is_never_published(conn, monkeypatch):
    sections = {"kind": "site", "gallery": ["roofer-solid"], "site_spec_id": None, "sample": "gallery"}
    page_id = landing.ensure_page_for_kit(conn, _biz(conn, monkeypatch))["id"]
    gallery_preview = {"kind": "bundle", "page_id": page_id, "sections": sections}
    assert build.publish_gate(conn, gallery_preview) == GALLERY
    # one that names a family as well is the gallery all the same: it is never a site to go live
    assert build.publish_gate(conn, {**gallery_preview, "sections": {**sections, "family": "home_services"}}) == GALLERY
    # a site preview with no family says nothing of what it is: the same refusal, not a crash
    assert build.publish_gate(conn, {**gallery_preview, "sections": {"kind": "site", "site_spec_id": None}}) == GALLERY


@pytest.mark.parametrize("family", ["attorneys", "Home_Services", "", 7, ["home_services"], {"home_services": 1}])
def test_a_preview_that_names_a_family_that_is_none_of_ours_is_answered_never_raised(conn, monkeypatch, caplog, family):
    """Task 5's follow-up: the gate reads the family from the preview's own sections, and a family `photos.GATE` has no
    sentences for -- a hand-made preview, a family retired since -- is Admiry's fault: it is answered in the publish
    failure's sentence (the caller refuses with it), logged for the operator, and never a `ValueError` out of
    `photos.key_photos` or a `TypeError` for a value that cannot be looked up."""
    page_id = landing.ensure_page_for_kit(conn, _biz(conn, monkeypatch))["id"]
    preview = {"kind": "bundle", "page_id": page_id, "sections": {"kind": "site", "family": family}}
    with caplog.at_level(logging.ERROR, logger="agent.sites.build"):
        assert build.publish_gate(conn, preview) == PUBLISH_FAILED
    (record,) = [r for r in caplog.records if r.name == "agent.sites.build"]
    assert record.levelno == logging.ERROR and page_id in record.getMessage()


def test_the_gate_leaves_every_other_preview_alone(conn, monkeypatch):
    page_id = landing.ensure_page_for_kit(conn, _biz(conn, monkeypatch))["id"]
    assert build.publish_gate(conn, None) is None
    assert build.publish_gate(conn, {"kind": "html", "page_id": page_id, "sections": {"kind": "site"}}) is None
    assert build.publish_gate(conn, {"kind": "html", "page_id": page_id, "sections": {"copy": {}}}) is None
    assert build.publish_gate(conn, {"kind": "bundle", "page_id": page_id, "sections": None}) is None  # an upload
    assert build.publish_gate(conn, {"kind": "bundle", "page_id": page_id, "sections": {}}) is None


def test_publishing_a_site_preview_waits_for_its_photos_and_then_goes_live(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    bare = _build(conn, biz)
    res = call_tool(client, "publish_page", {"idempotency_key": "pb-bare", "token": bare.token}, key=key)
    assert res["isError"] and tool_text(res) == HOME_BOTH
    assert _count(conn, "proposal") == 0  # nothing proposed, no address reserved
    with pytest.raises(ToolError, match="one photo of your own work before it goes live"):
        proposals.precheck_publish_page(conn, biz, {"token": bare.token})

    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz)
    prop = _publish(client, conn, monkeypatch, key, ready.token, "site")
    assert prop["summary"].startswith("Publish the site at")
    payload = conn.execute("SELECT payload FROM proposal WHERE id = %s", (prop["proposal_id"],)).fetchone()["payload"]
    assert (payload["token"], payload["hostname"]) == (ready.token, HOSTNAME)  # the address was reserved at the request

    page = conn.execute(
        "SELECT id, version, status, hostname, source, sections FROM page WHERE business_id = %s", (biz,)
    ).fetchone()
    assert (page["status"], page["hostname"], page["source"]) == ("published", HOSTNAME, "admiry")
    assert page["sections"]["kind"] == "site" and page["sections"]["family"] == "home_services"
    files = bundles.version_files(conn, page["id"], page["version"])  # drawn again for live: see test_sites_publish
    live = _get(client, HOSTNAME)
    assert live.status_code == 200 and bundles.sha256(live.content) == files["index.html"]
    assert live.text.count('<img class="brand-logo"') == 1  # the owner's logo, in the header (Solid's is light)
    logo_file = next(p for p in files if p.startswith("assets/img/") and p.endswith(".png"))
    served = _get(client, HOSTNAME, "/" + logo_file)  # the PNG itself, as the type it is
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert served.headers["cache-control"].endswith("immutable") and served.content[:4] == b"\x89PNG"
    service = _get(client, HOSTNAME, "/services/roof-replacement/")
    assert service.status_code == 200 and 'data-admiry-intent="quote"' in service.text
    assert _get(client, HOSTNAME, "/about/").status_code == 200
    assert data(call_tool(client, "get_page", {}, key=key))["page"]["versions"][0]["kind"] == "bundle"


def test_a_dental_site_publishes_with_its_appointment_form(
    client,
    conn,
    monkeypatch,
    dental_library,
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    bare = _build(conn, biz, "calm", sample="dentist")
    res = call_tool(client, "publish_page", {"idempotency_key": "pb-bare", "token": bare.token}, key=key)
    assert res["isError"] and tool_text(res) == DENTAL_BOTH and _count(conn, "proposal") == 0

    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz, sample="dentist")
    _publish(client, conn, monkeypatch, key, ready.token, "dental")  # no dental switch: it publishes with its form

    page = conn.execute("SELECT status, sections FROM page WHERE business_id = %s", (biz,)).fetchone()
    assert (
        page["status"] == "published" and page["sections"]["kind"] == "site" and page["sections"]["family"] == "dental"
    )
    for path in ("/", "/contact/", "/services/invisalign/"):
        shown = _get(client, HOSTNAME, path)
        assert shown.status_code == 200, path
        assert 'data-admiry-intent="appointment"' in shown.text and 'name="patient"' in shown.text, path
        assert '<img class="brand-logo"' in shown.text, path


def test_the_gate_runs_again_when_the_proposal_is_confirmed(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    photo = _own(conn, biz, "key")
    ready = _build_own(conn, biz)
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pb-late", "token": ready.token}, key=key))
    assert prop["status"] == "proposed"

    conn.execute("DELETE FROM page_photo WHERE id = %s", (photo,))  # the owner took the photo off in between
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "cb-late"}, key=key
    )
    assert res["isError"] and tool_text(res) == HOME_KEY
    assert conn.execute("SELECT status FROM proposal WHERE id = %s", (prop["proposal_id"],)).fetchone()["status"] == (
        "proposed"
    )
    assert conn.execute("SELECT status FROM page WHERE business_id = %s", (biz,)).fetchone()["status"] != "published"


def test_the_homepage_publish_button_answers_the_gate_for_a_site_preview(
    client,
    conn,
    card,  # noqa: F811
    library,  # noqa: F811
):
    biz = conn.execute("SELECT id FROM business").fetchone()["id"]
    built = _build(conn, biz)
    r = client.post("/api/page/publish", json={"csrf": card, "token": built.token}, headers=IP)
    assert r.status_code == 409 and r.json() == {"ok": False, "error": HOME_BOTH}
    assert _count(conn, "proposal") == 0


def test_an_assistants_own_bundle_publishes_as_before(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811
):
    key, _biz_id = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    ready = _upload(client, key)
    assert build.publish_gate(conn, hosted.preview(conn, ready["token"])) is None
    prop = _publish(client, conn, monkeypatch, key, ready["token"], "own")
    assert prop["summary"].startswith("Publish the site at")
    assert data(call_tool(client, "get_page", {}, key=key))["page"]["versions"][0]["kind"] == "bundle"


# ---------------------------------------------------------------- the final review's fixes


def test_a_build_inside_a_transaction_is_refused(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    with conn.transaction(), pytest.raises(ValueError, match="autocommit"):
        _build(conn, biz)
    assert _count(conn, "page_preview") == 0


def test_a_gallery_inside_a_transaction_is_refused(conn, monkeypatch, library):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    with conn.transaction(), pytest.raises(ValueError, match="autocommit"):
        gallery.build(conn, config.load(), biz)


def test_every_render_says_which_pages_it_did_not_open_and_a_partial_one_warns(
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,
):
    biz = _biz(conn, monkeypatch)
    full = FullRenderer()
    monkeypatch.setattr(render_check, "transport", lambda: httpx.MockTransport(full.handler))
    built = _build(conn, biz)
    assert built.render["partial"] is False and built.render["not_opened"] == [] and built.warnings == [build.NO_SPEED]
    r = PartialRenderer()
    monkeypatch.setattr(render_check, "transport", lambda: httpx.MockTransport(r.handler))
    partial = _build(conn, biz)
    note = next(w for w in partial.warnings if w.startswith("The render check ran out of time before opening: "))
    assert "/services/roof-repair/" in note and "/about/" in note


def test_a_missing_lcp_is_not_printed_as_none(conn, monkeypatch, library, speed, caplog):  # noqa: F811
    biz = _biz(conn, monkeypatch)
    real = pagespeed.score
    monkeypatch.setattr(pagespeed, "score", lambda *a, **k: {**real(*a, **k), "lcp_ms": None})
    speed.performance = 70
    with caplog.at_level(logging.WARNING, logger="agent.sites.build"):
        built = _build(conn, biz)
    assert "None" not in " ".join(built.warnings) and "None" not in caplog.text
    assert any("the LCP was not reported" in w for w in built.warnings)
    speed.performance = 45
    with pytest.raises(build.TemplateFault) as e:
        _build(conn, biz)
    assert "None" not in e.value.problems[0]["fix"] and "not reported" in e.value.problems[0]["fix"]
