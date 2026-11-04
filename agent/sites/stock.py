"""The stock photo library in Admiry's storage (A3p-6 batch 1 Q9; the manifest and its rules: data/stock/README.md).

`load` reads `data/stock/manifest.json` and keeps each photo once in `site_blob` (`bundles.store`, by SHA-256) with
WebP sizes beside it and its licence record in `site_asset` / `site_asset_file`. The bytes come from a local copy of
`data/stock/files/` first (the files are not in git, and a CDN can re-encode a photo over time, so a box that relied
on the CDN alone could end with an empty library) and from the entry's `image_url` only when no local file is there;
either way the SHA-256 must be the manifest's, or the entry is failed and nothing of it is stored. `pick` chooses a
photo for a slot, never one with people or one nobody reviewed, the same one for the same seed; `variants` names the
sizes. `bundles.gc` never collects a blob a `site_asset` or `site_asset_file` row names."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
from PIL import Image

from .. import bundles, crawl

MANIFEST = Path(__file__).resolve().parent.parent / "data" / "stock" / "manifest.json"
WIDTHS = (640, 1024, 1600)  # the sizes a page picks from (never above the original's width)
WEBP_QUALITY = 72
CHANGED = "bytes changed at the source; review the new image before using it"  # the README's own rule
FETCH_TIMEOUT = 60.0


@dataclass(frozen=True)
class Asset:
    id: str
    family: str
    kind: str
    alt: str
    width: int
    height: int
    author: str | None
    licence: str


def fetch(url: str) -> bytes:
    """One photo from its CDN: the address vetted as public first (`crawl.public_host`, as every fetch of the agent's
    is, CrawlError otherwise), no redirect followed, 60 seconds."""
    crawl.public_host(url)
    r = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=False, headers={"User-Agent": "admiry-stock-load/1"})
    r.raise_for_status()
    return r.content


def _webp(image: Image.Image, width: int) -> bytes:
    height = max(1, round(image.height * width / image.width))
    buf = io.BytesIO()
    image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS).save(
        buf, "WEBP", quality=WEBP_QUALITY, method=6
    )
    return buf.getvalue()


def _bytes_of(e: dict, files_dir: Path | None, fetch_: Callable[[str], bytes]) -> tuple[bytes, bool]:
    """The entry's bytes and whether they came from the local copy: its `file` (`files/<family>/<slot>-<nn>.jpg`)
    read as `files_dir / "<family>/<slot>-<nn>.jpg"`, else fetched."""
    if files_dir is not None:
        local = files_dir / e["file"].removeprefix("files/")
        if local.is_file():
            return local.read_bytes(), True
    return fetch_(e["image_url"]), False


def _store(conn: psycopg.Connection, e: dict, data: bytes, reviewed_at: str) -> None:
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        sized = {f"{w}": _webp(im, w) for w in WIDTHS if w <= im.width}
    sha = e["sha256"]
    with conn.transaction():
        stored = bundles.store(conn, {"original": data, **sized})
        asset_id = "sa_" + sha[:20]
        conn.execute(
            "INSERT INTO site_asset (id, family, kind, blob_sha256, source, page_url, author, author_url, licence, "
            "licence_url, alt, subject, width, height, people, logos, ai, reviewed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s)",
            (
                asset_id,
                e["family"],
                e["slot"],
                stored["original"],
                e["source"],
                e.get("page_url"),
                e.get("photographer"),
                e.get("photographer_url"),
                e["license"],
                e.get("license_url"),
                e["alt"],
                e.get("subject"),
                e["width"],
                e["height"],
                bool(e.get("people")),
                bool(e.get("logos")),
                reviewed_at,
            ),
        )
        for name, blob in stored.items():
            if name != "original":
                conn.execute(
                    "INSERT INTO site_asset_file (asset_id, width, sha256) VALUES (%s, %s, %s)",
                    (asset_id, int(name), blob),
                )


def load(
    conn: psycopg.Connection,
    manifest: Path,
    *,
    fetch: Callable[[str], bytes],
    files_dir: Path | None = None,
    families: Iterable[str] | None = None,
) -> dict:
    """The manifest's photos (of `families`, all by default) stored: {"loaded", "from_files", "fetched", "kept",
    "failed": [(file, reason)]}. An entry already stored (its original's SHA-256 is a `site_asset`) is kept as it is."""
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    wanted = set(families) if families is not None else None
    out: dict = {"loaded": 0, "from_files": 0, "fetched": 0, "kept": 0, "failed": []}
    for e in doc["images"]:
        if wanted is not None and e["family"] not in wanted:
            continue
        if conn.execute("SELECT 1 FROM site_asset WHERE blob_sha256 = %s", (e["sha256"],)).fetchone():
            out["kept"] += 1
            continue
        try:
            data, local = _bytes_of(e, files_dir, fetch)
        except Exception as err:  # noqa: BLE001 - one photo that cannot be read must not stop the library
            out["failed"].append((e["file"], f"{type(err).__name__}: {err}"))
            continue
        if hashlib.sha256(data).hexdigest() != e["sha256"]:  # a wrong local file included: never the CDN instead
            out["failed"].append((e["file"], CHANGED))
            continue
        try:
            _store(conn, e, data, doc["created"])
        except (OSError, ValueError) as err:  # an image Pillow cannot decode
            out["failed"].append((e["file"], f"{type(err).__name__}: {err}"))
            continue
        out["loaded"] += 1
        out["from_files" if local else "fetched"] += 1
    return out


def pick(conn: psycopg.Connection, family: str, kind: str, *, seed: str, exclude: Iterable[str] = ()) -> Asset | None:
    """A reviewed photo without people for the family's slot, other than `exclude`: the candidates ordered by
    `hashtext(seed || id)`, so a business keeps its photos across rebuilds; None when the slot has none left."""
    r = conn.execute(
        "SELECT id, family, kind, alt, width, height, author, licence FROM site_asset "
        "WHERE family = %s AND kind = %s AND NOT people AND reviewed_at IS NOT NULL AND id <> ALL(%s) "
        "ORDER BY hashtext(%s || id), id LIMIT 1",
        (family, kind, list(exclude), seed),
    ).fetchone()
    return Asset(**r) if r else None


def variants(conn: psycopg.Connection, asset_id: str) -> dict[int, str]:
    """The asset's WebP sizes: width -> the blob's SHA-256."""
    rows = conn.execute("SELECT width, sha256 FROM site_asset_file WHERE asset_id = %s", (asset_id,)).fetchall()
    return {r["width"]: r["sha256"] for r in rows}
