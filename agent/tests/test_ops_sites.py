"""The industry sites' operator commands (A3p-6 batch 1 Task 8; plan 2026-10-06-agent-a3p-6-1-industry-site-templates
Q22, Q23, Review Focus "partial"): `site-preview` builds one sample in one style as the business's bundle preview and
prints its address, what the render check opened, the speed test's scores; `site-gallery` builds the six and then one
index bundle -- the six with their three screenshots and a link into each, the six sites under `/<sample>-<style>/` --
the one address the founder opens."""

from __future__ import annotations

import hashlib
import io
import json
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from agent import bundles, config, hosted, landing, llm, ops, pagespeed, render_check
from agent.sites import build, gallery, samples, stock
from agent.tests import fixtures
from agent.tests.conftest import JARGON
from agent.tests.test_render_check import RENDERER, Renderer
from agent.tests.test_site_upload import _serving
from agent.tests.test_sites_build import KEY, Speed
from agent.tests.test_sites_dental import DENTAL_LIBRARY
from agent.tests.test_sites_render import _entries, load_library
from agent.tests.test_tools_hosting import DOMAIN

ROOFER_PAGES = (
    "/",
    "/about/",
    "/services/",
    "/contact/",
    "/services/roof-replacement/",
    "/services/roof-repair/",
    "/services/storm-damage/",
)
SIX = [f"{s}-{style}" for s, styles in gallery.chosen() for style in styles]  # the reviewed two, by default


# ---------------------------------------------------------------- stand-ins


class SiteRenderer(Renderer):
    """The renderer's stand-in, opening every page of the site at the three sizes (or only `pages`, out of time)."""

    def __init__(self, pages=ROOFER_PAGES, partial: bool = False) -> None:
        super().__init__()
        self.pages, self.partial = pages, partial

    def handler(self, request: httpx.Request) -> httpx.Response:
        answer = super().handler(request)
        body = json.loads(answer.content)
        if answer.status_code != 200:
            return answer
        opened = [{"path": p, "viewport": v, "status": 200} for p in self.pages for v in ("phone", "tablet", "desktop")]
        return httpx.Response(200, json={**body, "pages": opened, "partial": self.partial})


def _renderer(monkeypatch, r: Renderer) -> Renderer:
    monkeypatch.setenv("AGENT_RENDER_URL", RENDERER)
    monkeypatch.setattr(render_check, "transport", lambda: httpx.MockTransport(r.handler))
    return r


@pytest.fixture
def renderer(monkeypatch) -> SiteRenderer:
    return _renderer(monkeypatch, SiteRenderer())


@pytest.fixture
def speed(monkeypatch) -> Speed:
    monkeypatch.setenv("AGENT_PAGESPEED_API_KEY", KEY)
    s = Speed()
    monkeypatch.setattr(pagespeed, "transport", lambda: httpx.MockTransport(s.handler))
    return s


def _jpeg(shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1100, 733), (40, shade, 160)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def libraries(conn, tmp_path):
    """Both families' fake stock libraries (the dental photos' bytes differ from the roofer's, so none is 'kept')."""
    load_library(conn, tmp_path)
    entries, answers = _entries(DENTAL_LIBRARY, "dental")
    for n, e in enumerate(entries, 1):
        data = _jpeg((n * 23) % 200)
        answers[e["image_url"]] = data
        e.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest = tmp_path / "manifest-dental.json"
    manifest.write_text(json.dumps({"version": 1, "created": "2026-10-05T16:10:18Z", "images": entries}))
    out = stock.load(conn, manifest, fetch=answers.__getitem__)
    assert out["loaded"] == len(entries) and not out["failed"]
    return conn


def _biz(conn, monkeypatch, biz: str = "biz_ops_sites") -> str:
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
    return biz


def _newest(conn, biz: str) -> dict:
    page = conn.execute("SELECT id FROM page WHERE business_id = %s", (biz,)).fetchone()["id"]
    return conn.execute(
        "SELECT * FROM page_preview WHERE page_id = %s AND expires_at > now() ORDER BY created_at DESC LIMIT 1", (page,)
    ).fetchone()


def _actions(conn, tool: str) -> list[dict]:
    return conn.execute("SELECT * FROM agent_action WHERE tool = %s", (tool,)).fetchall()


# ---------------------------------------------------------------- site-preview (Q22)


def test_site_preview_prints_the_address_the_loads_and_the_scores(
    conn, monkeypatch, libraries, renderer, speed, capsys
):
    biz = _biz(conn, monkeypatch)
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid"]) == 0
    out = capsys.readouterr().out
    prev = _newest(conn, biz)
    host = bundles.preview_host(conn, prev["page_id"], DOMAIN)
    assert f"https://{host}/" in out
    assert "loads 21 (phone, tablet, desktop), partial no" in out
    assert "performance 92, accessibility 97, best practices 100, seo 69 (lcp 2413 ms)" in out
    assert prev["sections"]["sample"] == "roofer" and prev["sections"]["style"] == "solid"
    ttl = conn.execute("SELECT expires_at - created_at AS t FROM page_preview WHERE token = %s", (prev["token"],))
    assert ttl.fetchone()["t"].days in (6, 7)  # a look can take a week (Q22)
    (row,) = _actions(conn, "ops.site-preview")
    assert row["business_id"] == biz and row["actor"] == ops.ACTOR and row["ok"]


def test_site_preview_with_the_models_words(conn, monkeypatch, libraries, renderer, speed, capsys):
    biz = _biz(conn, monkeypatch)
    model = fixtures.site_words_fixture()
    model.home.lead = "We replace and repair roofs, and we help homeowners through storm damage."
    gen = fixtures.FakeGenerator(sites=[model])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "clean", "--words", "model"]) == 0
    out = capsys.readouterr().out
    assert "loads 21 (phone, tablet, desktop), partial no" in out and "performance 92" in out
    assert len(gen.site_calls) == 1
    row = conn.execute("SELECT data FROM site_spec WHERE business_id = %s", (biz,)).fetchone()
    fake = model.model_dump(mode="json")
    assert row["data"]["words"] == fake != samples.load("roofer", style="clean").words.model_dump(mode="json")


def test_site_preview_prints_a_partial_render_with_the_pages_not_opened(conn, monkeypatch, libraries, capsys):
    biz = _biz(conn, monkeypatch)
    _renderer(monkeypatch, SiteRenderer(pages=("/",), partial=True))
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "bold"]) == 0
    out = capsys.readouterr().out
    assert "loads 3 (phone, tablet, desktop), partial yes" in out
    line = next(x for x in out.splitlines() if x.startswith("not opened "))
    assert set(line.removeprefix("not opened ").split(", ")) == set(ROOFER_PAGES) - {"/"}


def test_a_template_fault_prints_the_problems_and_exits_1(conn, monkeypatch, libraries, renderer, capsys):
    biz = _biz(conn, monkeypatch)
    renderer.problems = [
        {"file": "/about/", "rule": "overflow", "message": "The about page scrolls sideways.", "fix": ""}
    ]
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid"]) == 1
    err = capsys.readouterr().err
    assert "/about/" in err and "The about page scrolls sideways." in err
    assert _newest(conn, biz) is None


def test_the_renderer_down_exits_1(conn, monkeypatch, libraries, renderer, capsys):
    biz = _biz(conn, monkeypatch)
    renderer.status = 503
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid"]) == 1
    assert render_check.UNAVAILABLE in capsys.readouterr().err


def test_hosting_off_exits_1(conn, monkeypatch, libraries, capsys):
    conn.execute("INSERT INTO business (id) VALUES ('biz_off')")
    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    assert ops.main(["site-preview", "biz_off", "--sample", "dentist", "--style", "calm"]) == 1
    assert "Hosting is not set up on this server." in capsys.readouterr().err
    assert ops.main(["site-gallery", "biz_off"]) == 1
    assert "Hosting is not set up on this server." in capsys.readouterr().err


def test_without_the_render_check_or_the_speed_test_it_says_so(conn, monkeypatch, libraries, capsys):
    biz = _biz(conn, monkeypatch)
    assert ops.main(["site-preview", biz, "--sample", "dentist", "--style", "warm"]) == 0
    cap = capsys.readouterr()
    assert "render check not run" in cap.out and "speed test not run" in cap.out
    assert "AGENT_RENDER_URL" in cap.err and "AGENT_PAGESPEED_API_KEY" in cap.err


# ---------------------------------------------------------------- site-gallery (Q22, Q23)


def test_site_gallery_builds_the_six_then_one_index(client, conn, monkeypatch, libraries, renderer, speed, capsys):
    biz = _biz(conn, monkeypatch)
    _serving(client)
    assert ops.main(["site-gallery", biz]) == 0
    out = capsys.readouterr().out
    assert len(renderer.asked) == 6 and len(speed.asked) == 6  # six full builds
    for name in SIX:
        assert name in out
    assert out.count("loads 21 (phone, tablet, desktop), partial no") == 6

    prev = _newest(conn, biz)
    assert prev["sections"]["kind"] == "site" and prev["sections"]["gallery"] == SIX
    host = bundles.preview_host(conn, prev["page_id"], DOMAIN)
    assert out.strip().splitlines()[-1] == f"gallery https://{host}/"
    # the gallery is the business's one live preview
    live = conn.execute(
        "SELECT count(*) AS n FROM page_preview WHERE page_id = %s AND expires_at > now()", (prev["page_id"],)
    ).fetchone()
    assert live["n"] == 1

    files = bundles.preview_files(conn, prev["token"])
    shots = sorted(p for p in files if p.startswith("shots/") and p.endswith(".jpg"))
    assert shots == sorted(f"shots/{n}-{size}.jpg" for n in SIX for size in ("phone", "tablet", "desktop"))
    index = bytes(
        conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (files["index.html"],)).fetchone()["bytes"]
    ).decode()
    for name in SIX:
        assert f'href="/{name}/"' in index and f"{name}/index.html" in files
        for size in ("phone", "tablet", "desktop"):
            assert f'src="/shots/{name}-{size}.jpg"' in index
    # the sites inside are rendered with their prefix (Q23)
    roofer = bytes(
        conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (files["roofer-solid/index.html"],)).fetchone()[
            "bytes"
        ]
    ).decode()
    assert 'href="/roofer-solid/about/"' in roofer and 'href="/about/"' not in roofer

    # the static checks pass on what is stored, every page carries the kit's snippet and no jargon
    stored = {
        p: bytes(conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()["bytes"])
        for p, sha in files.items()
        if not p.startswith("assets/img/") and "/assets/img/" not in p
    }
    page_id = prev["page_id"]
    judged = bundles.Judged(files=stored)
    settings = config.load()
    snippet = landing.snippet(page_id, settings.public_base_url, landing.google_tag(conn, biz))
    bundles.check(judged, page_id=page_id, own_host=None, agent_base=settings.public_base_url, snippet=snippet)
    assert judged.ok, judged.problems
    for p, data in stored.items():
        if p.endswith(".html"):
            text = hosted.visible_text(data.decode())
            assert not JARGON.search(text), (p, JARGON.search(text))
    assert len(hosted.visible_text(index)) >= hosted.MIN_TEXT_CHARS

    # served on the preview host: the index, a site under its prefix, a screenshot
    for path in ("/", "/roofer-solid/", "/dentist-boutique/about/", "/shots/roofer-solid-phone.jpg"):
        r = client.get(path, headers={"host": host})
        assert r.status_code == 200, path

    (row,) = _actions(conn, "ops.site-gallery")
    assert row["business_id"] == biz and row["ok"]
    # the gallery is Admiry's own page of looks, never a site to go live
    assert build.publish_gate(conn, hosted.preview(conn, prev["token"])) == (
        "This preview is Admiry's gallery of looks; it is not published."
    )


def test_gallery_build_answers_the_index_preview(conn, monkeypatch, libraries, renderer):
    biz = _biz(conn, monkeypatch)
    seen: list[str] = []
    built = gallery.build(conn, config.load(), biz, on_built=lambda name, b: seen.append(name))
    assert seen == SIX
    assert built.preview_url == f"https://{urlsplit(built.preview_url).hostname}/"
    assert hosted.preview(conn, built.token)["sections"]["gallery"] == SIX
    assert built.version == 0  # the index bundle has no document of its own


def test_a_fault_in_one_of_the_six_stops_the_gallery(conn, monkeypatch, libraries, renderer, capsys):
    biz = _biz(conn, monkeypatch)
    renderer.problems = [{"file": "/", "rule": "overflow", "message": "The home page scrolls sideways.", "fix": ""}]
    assert ops.main(["site-gallery", biz]) == 1
    assert "The home page scrolls sideways." in capsys.readouterr().err
    assert len(renderer.asked) == 1
    assert _actions(conn, "ops.site-gallery")[0]["ok"] is False


# ---------------------------------------------------------------- fix round 1


def test_a_malformed_accent_is_one_line_and_exit_1(conn, monkeypatch, libraries, capsys):
    biz = _biz(conn, monkeypatch)
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid", "--accent", "orange"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "The accent colour must be written like #1D4ED8, six hexadecimal digits."
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 0


def test_a_bad_document_from_the_sample_is_one_line_and_exit_1(conn, monkeypatch, libraries, capsys):
    biz = _biz(conn, monkeypatch)

    def broken(*args, **kwargs):
        return samples.spec.SiteSpec.model_validate({"family": "home_services"})  # no facts: pydantic refuses

    monkeypatch.setattr(samples, "load", broken)
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and len(err.strip().splitlines()) == 1 and "facts" in err


def test_the_model_unreachable_is_one_line_and_exit_1(conn, monkeypatch, libraries, capsys):
    biz = _biz(conn, monkeypatch)
    gen = fixtures.FakeGenerator(error="the assistant is unreachable")
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid", "--words", "model"]) == 1
    assert capsys.readouterr().err.strip() == "the assistant is unreachable"
    assert _actions(conn, "ops.site-preview")[0]["ok"] is False


def test_an_llm_error_raised_past_the_writer_is_one_line_and_exit_1(conn, monkeypatch, libraries, capsys):
    biz = _biz(conn, monkeypatch)

    def down(*args, **kwargs):
        raise llm.LLMError("the assistant took too long; try again")

    monkeypatch.setattr(ops.words, "write", down)
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid", "--words", "model"]) == 1
    assert capsys.readouterr().err.strip() == "the assistant took too long; try again"


def test_the_gallery_counts_its_refs_by_distinct_blob():
    from agent.sites import render as site_render

    many = {f"site-{i}/assets/img/p{i % 3}.webp": "abc"[i % 3] * 64 for i in range(400)}  # 400 paths, 3 blobs
    whole = site_render.Rendered(files={"index.html": b"<html></html>"}, refs=many)
    judged = bundles.Judged(files=dict(whole.files))
    gallery._limits(whole, judged, ref_bytes=10)
    assert judged.ok, judged.problems

    over = {f"f{i}.txt": b"x" for i in range(bundles.MAX_FILES + 1)}
    whole = site_render.Rendered(files=over, refs={})
    judged = bundles.Judged(files=dict(over))
    gallery._limits(whole, judged, ref_bytes=0)
    assert [p["rule"] for p in judged.problems] == ["file_count"]
    assert judged.problems[0]["message"].startswith(gallery.OVER)

    whole = site_render.Rendered(files={"index.html": b"x"}, refs={})
    judged = bundles.Judged(files={"index.html": b"x"})
    gallery._limits(whole, judged, ref_bytes=bundles.MAX_TOTAL_BYTES)
    assert [p["rule"] for p in judged.problems] == ["total_size"]


def test_a_gallery_over_the_limit_says_so(conn, monkeypatch, libraries, renderer, capsys):
    biz = _biz(conn, monkeypatch)
    monkeypatch.setattr(gallery, "MAX_FILES", 20)  # the gallery's own limit; each site's build keeps bundles'
    assert ops.main(["site-gallery", biz]) == 1
    err = capsys.readouterr().err
    assert "\nThe gallery is over the bundle's limit:\n" in err and "file_count" in err
    assert "Admiry's template failed a check" not in err
