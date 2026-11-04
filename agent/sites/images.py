"""The site's photo slots filled (A3p-6 batch 1 Q15; spec T10-T13, T25): the owner's photo where the document names
one, else a photo of the stock library chosen for the slot, never one with people; a people slot (team, dentist,
work-people) takes the owner's photo or stays empty, and its section takes the layout with no person (T11); so does
the logo (batch 2 Q8): the business's own, never stock or AI, and without one the header shows the name.

A stock photo's sizes are blobs already in `site_blob` (Task 2 stored them): the bundle names them as refs and never
reads them again. An owner's photo travels as bytes, one JPEG of at most 1600 pixels across as uploads already are,
stored with the pages -- a logo as a PNG when it kept its transparency (`content_type`)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg

from . import families, stock
from .spec import SiteSpec

PEOPLE_SLOTS = ("team", "dentist", "work-people")  # the owner's real photo only, never stock or AI (T11)
LOGO_SLOT = "logo"  # the owner's logo only, never stock or AI: it is the business's own mark (T13)


@dataclass(frozen=True)
class Resolved:
    """One filled slot: `variants` width -> SHA-256 (a stock photo's WebP sizes, or the owner's one file); `width` and
    `height` the largest variant's; `label` whether a preview shows "Replace with your photo" on it (every photo that
    is not the owner's); `bytes` the owner's photo itself, None for stock (its blobs are stored already);
    `content_type` the type the owner's photo was stored in (`image/png` for a logo that kept its transparency)."""

    source: str
    alt: str
    variants: dict[int, str]
    width: int
    height: int
    label: bool
    bytes: bytes | None = None
    content_type: str = "image/jpeg"

    @property
    def ext(self) -> str:
        """The extension its files carry in the bundle: a stock photo's sizes are WebP, an owner's photo is a PNG when it
        was stored as one and a JPEG otherwise."""
        if self.bytes is None:
            return ".webp"
        return ".png" if self.content_type == "image/png" else ".jpg"


def path(sha256: str, ext: str = ".webp") -> str:
    """Where a photo lives in the bundle: named by its content's hash, so it is served for a year."""
    return f"assets/img/{sha256[:16]}{ext}"


def _owner(conn: psycopg.Connection, photo_id: str, alt: str, page_id: str) -> Resolved | None:
    """The owner's photo, only ever one of `page_id`'s own: another page's photo is no photo at all."""
    row = conn.execute(
        "SELECT bytes, content_type, width, height FROM page_photo WHERE id = %s AND page_id = %s", (photo_id, page_id)
    ).fetchone()
    if row is None:
        return None
    data = bytes(row["bytes"])
    sha = hashlib.sha256(data).hexdigest()
    return Resolved("owner", alt, {row["width"]: sha}, row["width"], row["height"], False, data, row["content_type"])


def _stock(conn: psycopg.Connection, asset: stock.Asset, alt: str | None = None) -> Resolved | None:
    sizes = stock.variants(conn, asset.id)
    if not sizes:
        return None
    widest = max(sizes)
    height = max(1, round(asset.height * widest / asset.width))
    return Resolved("stock", alt or asset.alt, sizes, widest, height, True)


def _asset(conn: psycopg.Connection, asset_id: str) -> stock.Asset | None:
    """A stock photo the document names by id: reviewed and without people, like every one `stock.pick` chooses."""
    r = conn.execute(
        "SELECT id, family, kind, alt, width, height, author, licence FROM site_asset "
        "WHERE id = %s AND NOT people AND reviewed_at IS NOT NULL",
        (asset_id,),
    ).fetchone()
    return stock.Asset(**r) if r else None


def fill(conn: psycopg.Connection, spec: SiteSpec, *, business_id: str, page_id: str) -> dict[str, Resolved]:
    """Every slot of the family's slot map that can be filled, and the people slots and the logo the owner gave a photo
    for. A slot's own ref first (an `owner` one read from `page_photo`, of `page_id` only; a `stock` one by its id),
    then the library: `stock.pick` seeded with `business_id` and the slot, so a business keeps its photos across
    rebuilds, each slot a photo of its own while the library has one left. An `ai` ref is not rendered in batch 1.
    Both keywords are required: they are the tenancy boundary. An `owner` ref naming a photo that is not `page_id`'s
    resolves to nothing, so its slot is filled like any empty slot (a people slot and the logo stay empty)."""
    family = families.get(spec.family)
    out: dict[str, Resolved] = {}
    used: set[str] = set()
    for slot in (*PEOPLE_SLOTS, LOGO_SLOT):
        ref = spec.images.get(slot)
        if ref is not None and ref.source == "owner":
            found = _owner(conn, ref.id, ref.alt, page_id)
            if found:
                out[slot] = found
    for slot, kind in family.slot_kinds(spec).items():
        ref = spec.images.get(slot)
        found = None
        if ref is not None and ref.source == "owner":
            found = _owner(conn, ref.id, ref.alt, page_id)
        elif ref is not None and ref.source == "stock":
            asset = _asset(conn, ref.id)
            if asset is not None:
                used.add(asset.id)
                found = _stock(conn, asset, ref.alt or None)
        if found is None:
            seed = f"{business_id}:{slot}"
            asset = stock.pick(conn, family.id, kind, seed=seed, exclude=used) or stock.pick(
                conn, family.id, kind, seed=seed
            )
            if asset is None and slot == "hero" and kind != "hero":  # a trade's hero with none of its photos in stock
                asset = stock.pick(conn, family.id, "hero", seed=seed, exclude=used)
            if asset is not None:
                used.add(asset.id)
                found = _stock(conn, asset)
        if found is not None:
            out[slot] = found
    return out
