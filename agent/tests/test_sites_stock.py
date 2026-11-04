"""agent/sites/stock.py (A3p-6 batch 1 Task 2, Q9): the stock library from its manifest into Admiry's content-addressed
storage -- from a local copy of the files first, the CDN only for an entry with none, every byte checked against the
manifest's SHA-256 -- with its WebP sizes and licence record, picked without people, and never collected."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC

import pytest
from PIL import Image

from agent import bundles, ops
from agent.sites import stock

CREATED = "2026-10-05T16:10:18Z"
SENTENCE = "bytes changed at the source; review the new image before using it"


def jpeg(w: int, h: int, shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (shade, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


# (file, family, slot, width, people)
ENTRIES = [
    ("files/home_services/hero-01.jpg", "home_services", "hero", 2400, False),
    ("files/home_services/hero-02.jpg", "home_services", "hero", 1200, False),
    ("files/dental/reception-01.jpg", "dental", "reception", 1700, False),
]


def image_for(file: str, width: int) -> bytes:
    return jpeg(width, width // 2, sum(map(ord, file)) % 200)


def entry(file, family, slot, width, people=False, data=None):
    data = data or image_for(file, width)
    return {
        "file": file,
        "family": family,
        "slot": slot,
        "subject": f"a {slot}",
        "alt": f"A {slot} photo",
        "source": "unsplash",
        "page_url": f"https://unsplash.com/photos/{file[-6:-4]}",
        "image_url": f"https://images.unsplash.com/{file}",
        "photographer": "Sean Foster",
        "photographer_url": "https://unsplash.com/@fosterious",
        "license": "Unsplash License",
        "license_url": "https://unsplash.com/license",
        "width": width,
        "height": width // 2,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "people": people,
        "logos": False,
    }


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"version": 1, "created": CREATED, "images": [entry(*e) for e in ENTRIES]}), encoding="utf-8"
    )
    return path


class Fetch:
    """A CDN that answers each entry's image_url with its image, and keeps the list of what was asked."""

    def __init__(self, answers=None):
        self.answers = answers or {f"https://images.unsplash.com/{f}": image_for(f, w) for f, _, _, w, _ in ENTRIES}
        self.asked: list[str] = []

    def __call__(self, url):
        self.asked.append(url)
        return self.answers[url]


def copy_of_files(root, only=None):
    """A local copy laid out as agent/data/stock/files/ is: files_dir / "<family>/<slot>-<nn>.jpg"."""
    for f, _, _, w, _ in ENTRIES:
        if only is None or f in only:
            p = root / f.removeprefix("files/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(image_for(f, w))
    return root


def count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_load_stores_assets_originals_and_webp_variants_with_the_licence_record(conn, manifest):
    out = stock.load(conn, manifest, fetch=Fetch())

    assert out == {"loaded": 3, "from_files": 0, "fetched": 3, "kept": 0, "failed": []}
    rows = conn.execute("SELECT * FROM site_asset ORDER BY blob_sha256").fetchall()
    assert len(rows) == 3
    hero = conn.execute("SELECT * FROM site_asset WHERE width = 2400").fetchone()
    assert (hero["family"], hero["kind"], hero["source"], hero["author"], hero["licence"]) == (
        "home_services",
        "hero",
        "unsplash",
        "Sean Foster",
        "Unsplash License",
    )
    assert hero["author_url"] == "https://unsplash.com/@fosterious"
    assert hero["licence_url"] == "https://unsplash.com/license"
    assert hero["page_url"].startswith("https://unsplash.com/photos/")
    assert (hero["alt"], hero["subject"], hero["height"]) == ("A hero photo", "a hero", 1200)
    assert (hero["people"], hero["logos"], hero["ai"]) == (False, False, False)
    assert hero["reviewed_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") == CREATED
    for r in rows:
        original = conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (r["blob_sha256"],)).fetchone()
        assert bytes(original["bytes"])[:2] == b"\xff\xd8"
    assert stock.variants(conn, hero["id"]).keys() == {640, 1024, 1600}
    narrow = conn.execute("SELECT id FROM site_asset WHERE width = 1200").fetchone()
    assert sorted(stock.variants(conn, narrow["id"])) == [640, 1024]  # never above the original's width
    for sha in stock.variants(conn, hero["id"]).values():
        data = bytes(conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()["bytes"])
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    width = {
        w: Image.open(
            io.BytesIO(bytes(conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (s,)).fetchone()["bytes"]))
        ).width
        for w, s in stock.variants(conn, hero["id"]).items()
    }
    assert width == {640: 640, 1024: 1024, 1600: 1600}


def test_a_mismatch_from_the_cdn_is_failed_with_the_readmes_sentence_and_stores_nothing(conn, manifest):
    fetch = Fetch()
    url = "https://images.unsplash.com/files/dental/reception-01.jpg"
    fetch.answers[url] = jpeg(1700, 850, 7)  # re-encoded at the source

    out = stock.load(conn, manifest, fetch=fetch)

    assert out["loaded"] == 2
    assert out["failed"] == [("files/dental/reception-01.jpg", SENTENCE)]
    assert count(conn, "site_asset") == 2
    assert conn.execute("SELECT count(*) AS n FROM site_asset WHERE family = 'dental'").fetchone()["n"] == 0
    assert count(conn, "site_blob") == 2 + 3 + 2  # two originals, their variants; nothing of the failed one


def test_load_twice_keeps_everything_and_adds_nothing(conn, manifest):
    stock.load(conn, manifest, fetch=Fetch())
    blobs = count(conn, "site_blob")
    fetch = Fetch()

    out = stock.load(conn, manifest, fetch=fetch)

    assert out == {"loaded": 0, "from_files": 0, "fetched": 0, "kept": 3, "failed": []}
    assert fetch.asked == [] and count(conn, "site_blob") == blobs and count(conn, "site_asset") == 3


def test_a_family_filter_loads_only_that_family(conn, manifest):
    out = stock.load(conn, manifest, fetch=Fetch(), families={"dental"})

    assert out["loaded"] == 1
    assert [r["family"] for r in conn.execute("SELECT family FROM site_asset").fetchall()] == ["dental"]


def test_a_local_copy_is_read_without_asking_the_cdn(conn, manifest, tmp_path):
    files = copy_of_files(
        tmp_path / "copy", only={"files/home_services/hero-01.jpg", "files/home_services/hero-02.jpg"}
    )
    fetch = Fetch()

    out = stock.load(conn, manifest, fetch=fetch, files_dir=files)

    assert (out["loaded"], out["from_files"], out["fetched"], out["failed"]) == (3, 2, 1, [])
    assert fetch.asked == ["https://images.unsplash.com/files/dental/reception-01.jpg"]  # the entry with no file


def test_a_local_copy_with_every_file_never_calls_fetch(conn, manifest, tmp_path):
    fetch = Fetch()

    out = stock.load(conn, manifest, fetch=fetch, files_dir=copy_of_files(tmp_path / "copy"))

    assert (out["loaded"], out["from_files"], out["fetched"]) == (3, 3, 0) and fetch.asked == []


def test_a_wrong_local_file_is_failed_and_the_cdn_is_not_asked_instead(conn, manifest, tmp_path):
    files = copy_of_files(tmp_path / "copy")
    (files / "dental/reception-01.jpg").write_bytes(b"not the photo")
    fetch = Fetch()

    out = stock.load(conn, manifest, fetch=fetch, files_dir=files)

    assert out["loaded"] == 2 and out["from_files"] == 2 and out["fetched"] == 0
    assert out["failed"] == [("files/dental/reception-01.jpg", SENTENCE)]
    assert fetch.asked == [] and count(conn, "site_asset") == 2


def test_a_fetch_that_fails_is_failed_with_its_reason(conn, manifest):
    def broken(url):
        raise OSError("connection reset")

    out = stock.load(conn, manifest, fetch=broken)

    assert out["loaded"] == 0 and len(out["failed"]) == 3 and "connection reset" in out["failed"][0][1]
    assert count(conn, "site_blob") == 0


def test_pick_skips_people_and_unreviewed_is_stable_per_seed_and_honours_exclude(conn, manifest):
    stock.load(conn, manifest, fetch=Fetch())
    rows = conn.execute("SELECT id, width FROM site_asset WHERE kind = 'hero'").fetchall()
    assert len(rows) == 2
    a = stock.pick(conn, "home_services", "hero", seed="biz_1")
    assert a is not None and (a.family, a.kind) == ("home_services", "hero")
    assert a.alt == "A hero photo" and a.author == "Sean Foster" and a.licence == "Unsplash License"
    assert (a.width, a.height) == (
        {r["id"]: r["width"] for r in rows}[a.id],
        {r["id"]: r["width"] for r in rows}[a.id] // 2,
    )
    assert stock.pick(conn, "home_services", "hero", seed="biz_1") == a  # the same seed, the same photo
    other = stock.pick(conn, "home_services", "hero", seed="biz_1", exclude={a.id})
    assert other is not None and other.id != a.id
    assert stock.pick(conn, "home_services", "hero", seed="biz_1", exclude={a.id, other.id}) is None
    assert stock.pick(conn, "dental", "hero", seed="biz_1") is None  # a slot the family has not
    assert stock.pick(conn, "plumber", "hero", seed="biz_1") is None

    conn.execute("UPDATE site_asset SET people = true WHERE id = %s", (a.id,))
    assert stock.pick(conn, "home_services", "hero", seed="biz_1").id == other.id
    conn.execute("UPDATE site_asset SET reviewed_at = NULL WHERE id = %s", (other.id,))
    assert stock.pick(conn, "home_services", "hero", seed="biz_1") is None


def test_gc_leaves_the_library_and_frees_an_unrelated_blob(conn, manifest):
    stock.load(conn, manifest, fetch=Fetch())
    stray = bundles.store(conn, {"x": b"nothing names this"})["x"]
    conn.execute("UPDATE site_blob SET touched_at = now() - interval '1 day 1 hour'")
    library = count(conn, "site_blob") - 1

    assert bundles.gc(conn) == 1

    assert count(conn, "site_blob") == library
    assert conn.execute("SELECT 1 FROM site_blob WHERE sha256 = %s", (stray,)).fetchone() is None


def test_fetch_refuses_an_address_that_is_not_public():
    with pytest.raises(Exception, match="public"):
        stock.fetch("http://127.0.0.1/x.jpg")


def test_stock_load_command_prints_the_counts_and_exits_0(conn, manifest, tmp_path, monkeypatch, capsys):
    fetch = Fetch()
    monkeypatch.setattr(stock, "fetch", fetch)
    files = copy_of_files(
        tmp_path / "copy", only={"files/home_services/hero-01.jpg", "files/home_services/hero-02.jpg"}
    )

    assert ops.main(["stock-load", "--manifest", str(manifest), "--files", str(files)]) == 0

    assert capsys.readouterr().out.strip() == "loaded 3 (2 from files, 1 fetched), kept 0, failed 0"


def test_stock_load_without_files_fetches_every_entry(conn, manifest, monkeypatch, capsys):
    monkeypatch.setattr(stock, "fetch", Fetch())

    assert ops.main(["stock-load", "--manifest", str(manifest)]) == 0

    assert capsys.readouterr().out.strip() == "loaded 3 (0 from files, 3 fetched), kept 0, failed 0"


def test_stock_load_command_filters_by_family(conn, manifest, monkeypatch, capsys):
    monkeypatch.setattr(stock, "fetch", Fetch())

    assert ops.main(["stock-load", "--manifest", str(manifest), "--family", "dental"]) == 0

    assert capsys.readouterr().out.strip() == "loaded 1 (0 from files, 1 fetched), kept 0, failed 0"


def test_stock_load_command_exits_1_for_a_missing_manifest_or_files_directory(conn, manifest, tmp_path, capsys):
    assert ops.main(["stock-load", "--manifest", str(tmp_path / "nope.json")]) == 1
    assert "nope.json" in capsys.readouterr().err
    assert ops.main(["stock-load", "--manifest", str(manifest), "--files", str(tmp_path / "nodir")]) == 1
    assert "nodir" in capsys.readouterr().err
    assert count(conn, "site_asset") == 0


def test_stock_load_command_exits_1_and_names_a_failed_file(conn, manifest, tmp_path, monkeypatch, capsys):
    files = copy_of_files(tmp_path / "copy")
    (files / "dental/reception-01.jpg").write_bytes(b"wrong")
    monkeypatch.setattr(stock, "fetch", Fetch())

    assert ops.main(["stock-load", "--manifest", str(manifest), "--files", str(files)]) == 1

    cap = capsys.readouterr()
    assert cap.out.strip() == "loaded 2 (2 from files, 0 fetched), kept 0, failed 1"
    assert "files/dental/reception-01.jpg" in cap.err and SENTENCE in cap.err
