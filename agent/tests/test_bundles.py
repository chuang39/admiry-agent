"""agent/bundles.py without a database (A3p-3 spec §6.2-§6.5; plan 2026-10-04-agent-a3p-3-3-site-bundles B4-B6, B9):
the zip unpacked counting real bytes, the path tricks and the project refused before anything is read, the contract
and the safety checks with a file, a rule and a fix for every problem, and the serving helpers' paths and caches."""

from __future__ import annotations

import io
import stat
import struct
import warnings
import zipfile
import zlib

import pytest
from PIL import Image

from agent import bundles, hosted

AGENT = "http://testserver"
PAGE = "pg_bundle_test"
SNIPPET = f'<script async src="{AGENT}/t/admiry.js" data-page="{PAGE}"></script>'
WORDS = (
    "Bright Smile Dental straightens teeth with Invisalign in Palo Alto. Same-week appointments, evening hours and a "
    "free first consultation for every new patient. Our location is two blocks from the Caltrain station. "
)
HOME = (
    f"<!doctype html><html><head><title>Bright Smile Dental</title>{SNIPPET}</head><body><h1>Invisalign</h1>"
    f'<p>{WORDS * 3}</p><form method="post" action="{AGENT}/t/form/{PAGE}" data-admiry-intent="appointment">'
    '<input name="name"><button type="submit">Send</button></form></body></html>'
)
# a framework's page: short, and an inline script that carries the page's words ("location", "open (")
ABOUT = (
    f'<html><head>{SNIPPET}</head><body><p>About us.</p><script>self.__next_f.push([1,"Our location: open (Mon-Fri)"])'
    "</script></body></html>"
)


def png(w: int = 4, h: int = 4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def png_header(w: int, h: int) -> bytes:
    """A PNG whose header says w x h with no pixels behind it: Pillow reads the size without decoding anything."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


def flagged_encrypted(data: bytes) -> bytes:
    """`data` with every entry's encryption flag set, in its local header and in the central directory (zipfile
    writes no encrypted entry of its own)."""
    out = bytearray(data)
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = out.find(signature)
        while at >= 0:
            out[at + offset] |= 0x1
            at = out.find(signature, at + 4)
    return bytes(out)


def make_zip(entries, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """A zip of `entries`: a dict name -> content, or a list of (name, content, extra) where extra may set
    `symlink` or `raw_name` (a name written as it is, past zipfile's own clean-up)."""
    if isinstance(entries, dict):
        entries = [(name, content, {}) for name, content in entries.items()]
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a duplicate name is a test's point, not zipfile's warning
        with zipfile.ZipFile(buf, "w", compression) as zf:
            for name, content, extra in entries:
                info = zipfile.ZipInfo(name)
                info.compress_type = compression
                if "raw_name" in extra:
                    info.filename = extra["raw_name"]
                if extra.get("symlink"):
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, content.encode() if isinstance(content, str) else content)
    return buf.getvalue()


def site(**extra) -> dict:
    files = {
        "index.html": HOME,
        "about/index.html": ABOUT,
        "404.html": f"<html><head>{SNIPPET}</head><body><p>Not here.</p></body></html>",
        "_next/static/chunks/app-4f3b2a1c9d.js": "if (location.hash) window.open('/x');",
        "style.css": "body { margin: 0 }",
        "logo.png": png(),
    }
    files.update(extra)
    return {k: v for k, v in files.items() if v is not None}


def judge(data: bytes, *, own_host: str | None = None) -> bundles.Judged:
    return bundles.judge(io.BytesIO(data), page_id=PAGE, own_host=own_host, agent_base=AGENT, snippet=SNIPPET)


def rules(judged: bundles.Judged) -> set[str]:
    return {p["rule"] for p in judged.problems}


# ---------------------------------------------------------------- a build passes


def test_a_build_passes_with_its_files_and_its_title():
    judged = judge(make_zip(site()))
    assert judged.ok, judged.problems
    assert set(judged.files) == set(site())
    assert judged.title == "Bright Smile Dental" and judged.home == HOME
    assert judged.size == sum(len(v.encode() if isinstance(v, str) else v) for v in site().values())


def test_inline_scripts_are_not_read_and_only_the_home_needs_words():
    """B6: a framework's inline script carries the page's words ("Our location", "open (") and its 404 page is short;
    a single page is still held to both rules."""
    assert judge(make_zip(site())).ok
    rules_of_a_single_page = {r for r, _ in hosted.problems(ABOUT, page_id=PAGE, own_host=None, agent_base=AGENT)}
    assert rules_of_a_single_page == {"text", "navigation"}
    short_home = judge(make_zip(site(**{"index.html": ABOUT})))
    assert rules(short_home) == {"text"} and short_home.problems[0]["file"] == "index.html"


def test_a_zip_of_the_build_folder_is_read_from_inside_it_and_mac_junk_is_left_out():
    entries = {f"out/{k}": v for k, v in site().items()}
    entries["__MACOSX/out/._index.html"] = "junk"
    entries["out/.DS_Store"] = "junk"
    judged = judge(make_zip(entries))
    assert judged.ok, judged.problems
    assert set(judged.files) == set(site())


# ---------------------------------------------------------------- the zip itself (B4)


@pytest.mark.parametrize(
    "raw",
    ["../evil.html", "/etc/evil.html", "C:/evil.html", "a\\evil.html", "a/../evil.html", "a//evil.html", "a\x00.html"],
)
def test_unpacking_refuses_the_path_tricks(raw):
    judged = judge(make_zip([("index.html", HOME, {}), ("x", "evil", {"raw_name": raw})]))
    assert judged.stopped and rules(judged) == {"path"} and judged.files == {}
    assert judged.problems[0]["fix"]


def test_unpacking_refuses_links_case_twins_doubles_and_encryption():
    link = judge(make_zip([("index.html", HOME, {}), ("about.html", "index.html", {"symlink": True})]))
    assert link.stopped and "symbolic link" in link.problems[0]["message"]
    twins = judge(make_zip({"index.html": HOME, "Index.html": HOME}))
    assert twins.stopped and "differ only by case" in twins.problems[0]["message"]
    doubles = judge(make_zip([("index.html", HOME, {}), ("index.html", HOME, {})]))
    assert doubles.stopped and "twice" in doubles.problems[0]["message"]
    secret = judge(flagged_encrypted(make_zip({"index.html": HOME})))
    assert secret.stopped and rules(secret) == {"zip"}
    assert rules(judge(b"not a zip at all")) == {"zip"}


def test_unpacking_counts_real_bytes():
    """A bomb stops past 100 times its packed size; a member whose header lies about its size is refused, never
    read as the header says; the site's 25 MB are counted as they come."""
    bomb = judge(make_zip({"index.html": HOME, "data.json": b"\0" * (3 * 1024 * 1024)}))
    assert bomb.stopped and rules(bomb) == {"zip_ratio"} and bomb.problems[0]["file"] == "data.json"
    data = bytearray(make_zip({"index.html": HOME}, compression=zipfile.ZIP_STORED))
    at = data.rfind(b"PK\x01\x02")
    struct.pack_into("<I", data, at + 24, 10)  # the central directory says the page is 10 bytes
    liar = judge(bytes(data))
    assert liar.stopped and rules(liar) == {"zip"}
    four = b"\1" * (4 * 1024 * 1024)
    big = {f"part{i}.js": four for i in range(7)}  # 28 MB, stored: no ratio to trip
    total = judge(make_zip({"index.html": HOME, **big}, compression=zipfile.ZIP_STORED))
    assert total.stopped and rules(total) == {"total_size"}


def test_the_central_directory_is_bounded():
    entries = {f"d{i}/": "" for i in range(bundles.MAX_ENTRIES + 1)}
    judged = judge(make_zip({"index.html": HOME, **entries}, compression=zipfile.ZIP_STORED))
    assert judged.stopped and rules(judged) == {"file_count"}
    many = {f"p{i}.txt": "x" for i in range(bundles.MAX_FILES + 1)}
    assert rules(judge(make_zip({"index.html": HOME, **many}))) == {"file_count"}


@pytest.mark.parametrize(
    ("entries", "named"),
    [
        ({"package.json": "{}", "src/app.js": "x"}, "package.json"),
        ({"index.html": HOME, "node_modules/react/index.js": "x"}, "node_modules/"),
        ({"index.html": HOME, ".git/config": "x"}, ".git/"),
        ({"index.html": HOME, ".env.local": "KEY=1"}, ".env.local"),
        ({"index.html": HOME, "certs/server.key": "x"}, "certs/server.key"),
        ({"index.html": HOME, "id_ed25519": "x"}, "id_ed25519"),
        ({"my-site/package.json": "{}", "my-site/out/index.html": HOME}, "package.json"),
    ],
)
def test_a_project_is_refused_with_the_folder_to_upload(entries, named):
    judged = judge(make_zip(entries))
    assert judged.stopped and rules(judged) == {"project"} and judged.files == {}
    assert named in judged.problems[0]["message"]
    assert "out/" in judged.problems[0]["fix"] and "dist/" in judged.problems[0]["fix"]


# ---------------------------------------------------------------- the contract (B5)


def test_the_contract_names_every_file_that_breaks_it():
    deep = "a/b/c/d/e/f/g/h/i/page.html"
    judged = judge(
        make_zip(
            site(
                **{
                    "app.js.map": "{}",
                    "x" * 197 + ".css": "a{}",
                    deep: ABOUT,
                    "privacy/index.html": ABOUT,
                    "latin1.html": "<html><body>caf\xe9</body></html>".encode("latin-1"),
                    "broken.png": b"\x89PNG not really",
                    "huge.png": png_header(8000, 6000),
                    "video.js": b"\0\1" * (3 * 1024 * 1024),
                }
            ),
            compression=zipfile.ZIP_STORED,  # 6 MB of a pattern deflates far past the ratio
        )
    )
    assert not judged.stopped
    by_file = {p["file"]: p["rule"] for p in judged.problems}
    assert by_file == {
        "app.js.map": "file_type",
        "x" * 197 + ".css": "path",
        deep: "path",
        "privacy/index.html": "reserved",
        "latin1.html": "encoding",
        "broken.png": "image",
        "huge.png": "image_pixels",
        "video.js": "file_size",
    }
    assert all(p["fix"] and p["message"] for p in judged.problems)


def test_a_site_needs_its_home_page_at_the_top():
    judged = judge(make_zip({k: v for k, v in site().items() if k != "index.html"}))
    assert rules(judged) == {"index"} and judged.problems[0]["file"] == "/"


# ---------------------------------------------------------------- safety (B6) and the structured problems (§6.5)


def test_the_problems_name_file_rule_and_fix():
    untracked = HOME.replace(SNIPPET, "")
    cdn = ABOUT.replace("</head>", '<script src="https://cdn.jsdelivr.net/npm/x.js"></script></head>')
    elsewhere = ABOUT.replace("<p>About", '<form action="https://forms.example.com/x"></form><p>About')
    handler = ABOUT.replace("<p>About", '<p onclick="go()">About')
    judged = judge(
        make_zip(site(**{"index.html": untracked, "cdn.html": cdn, "form.html": elsewhere, "on.html": handler}))
    )
    found = {(p["file"], p["rule"]): p for p in judged.problems}
    assert set(found) == {
        ("index.html", "tracking"),
        ("cdn.html", "script_host"),
        ("form.html", "form_target"),
        ("on.html", "event_handler"),
    }
    assert SNIPPET in found["index.html", "tracking"]["fix"] and "next/script" in found["index.html", "tracking"]["fix"]
    assert found["cdn.html", "script_host"]["message"].endswith("cdn.jsdelivr.net/npm/x.js.")
    assert (
        found["cdn.html", "script_host"]["fix"] == "Bundle the library into the site instead of loading it from a CDN."
    )
    assert f"{AGENT}/t/form/{PAGE}" in found["form.html", "form_target"]["fix"]
    for p in judged.problems:
        assert set(p) == {"file", "rule", "message", "fix"}
    body = bundles.answer(judged)
    assert body["status"] == "refused" and body["say"] == bundles.REFUSED and len(body["problems"]) == 4


def test_the_script_tag_must_name_this_page_and_the_agent_host():
    other_page = HOME.replace(f'data-page="{PAGE}"', 'data-page="pg_someone_else"')
    other_host = HOME.replace(f"{AGENT}/t/admiry.js", "https://elsewhere.example.com/t/admiry.js")
    assert ("index.html", "tracking") in {
        (p["file"], p["rule"]) for p in judge(make_zip(site(**{"index.html": other_page}))).problems
    }
    assert "tracking" in rules(judge(make_zip(site(**{"index.html": other_host}))))


def test_an_answer_lists_fifty_problems_and_counts_the_rest():
    judged = judge(make_zip(site(**{f"p{i}.map": "{}" for i in range(60)})))
    body = bundles.answer(judged)
    assert len(body["problems"]) == bundles.MAX_PROBLEMS and "The first 50 of 60 are listed." in body["say"]


# ---------------------------------------------------------------- serving helpers (B9)


def test_a_path_names_its_file_the_static_hosts_way():
    assert bundles.candidates("/") == ["index.html"]
    assert bundles.candidates("/about/") == ["about/index.html"]
    assert bundles.candidates("/about") == ["about", "about.html", "about/index.html"]
    assert bundles.candidates("/_next/static/a.js") == [
        "_next/static/a.js",
        "_next/static/a.js.html",
        "_next/static/a.js/index.html",
    ]


@pytest.mark.parametrize(
    ("path", "cache"),
    [
        ("index.html", "public, max-age=300"),
        ("about/index.html", "public, max-age=300"),
        ("_next/static/chunks/main.js", "public, max-age=31536000, immutable"),
        ("_astro/index.js", "public, max-age=31536000, immutable"),
        ("assets/index-4f3b2a1c.js", "public, max-age=31536000, immutable"),
        ("css/main.min.2f8d3a9b1c.css", "public, max-age=31536000, immutable"),
        ("logo.png", "public, max-age=3600"),
        ("hero-section.jpg", "public, max-age=3600"),
        ("photo-20240512.jpg", "public, max-age=3600"),
    ],
)
def test_the_cache_is_the_files(path, cache):
    assert bundles.cache_control(path) == cache
