"""Admiry's licensed image set (spec 2026-09-29-admiry-channels-design D89; plan 2026-09-29-agent-m1-newsbreak P6): the
pictures a NewsBreak ad shows until A5's creative set exists -- a few per trade, licensed once, no cost per business.

`agent/static/creative/manifest.json` lists them: `{"schema": {...}, "entries": [{file, category, services, licence,
source, alt}, ...]}`. `file` is the picture's path under that folder (`dentists/1.jpg`), which the web app also serves
at `/static/creative/<file>` for the plan card; `category` is a trade (`models.Category`); `services` the services it
suits, matched without regard to capitals, empty for a general picture of the trade; `licence` and `source` say whose
it is and where it came from; `alt` is one plain sentence about what it shows, which the assistant reads to an owner who
cannot see it -- so it says only what is there. The founder adds the pictures and their entries ("The founder, now",
plan Setup); the manifest ships with the schema and none.

NewsBreak takes JPEG or PNG, at least 1200 x 627 pixels (16:9 is recommended), at most 5 MB (its Product Catalog and
Create an Ad articles, read 2026-10-01; confirm at the pilot). `problem` is that check, for one entry; `manifest`
skips what fails it, with a warning that names the file and why -- an operator reads it in the log, the owner never sees
it -- and `pick` chooses among what is left: a picture the plan already carries while its trade still has it, else a
service's own picture, else the trade's first. A trade with no picture left gets None, and no plan funds NewsBreak for
it until the founder adds one (`plans_channels.held`, M1 Task 7 fix round 1); `find` looks up the picture a plan
version pinned, by its file, while the set has it. Importing this module reads nothing; `manifest` reads the file and
the pictures' headers (Pillow does not decode the pixels) once, and again only when the manifest or one of its pictures
changes (M1 Task 7: the plan card asks on every render) -- a call that finds them as they were costs a `stat` per file,
and nothing the bidder's request path ever runs.
"""

from __future__ import annotations

import copy
import json
import logging
import typing
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PilImage

from ... import models

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2] / "static" / "creative"  # agent/static/creative
MANIFEST = "manifest.json"
FIELDS = ("file", "category", "services", "licence", "source", "alt")
CATEGORIES = typing.get_args(models.Category)
MIN_WIDTH, MIN_HEIGHT = 1200, 627
MAX_BYTES = 5_000_000  # "5 MB": decimal, so a file under it is under either reading
FORMATS = ("JPEG", "PNG")
# what `manifest` last read (M1 Task 7): (the folder and the manifest's stamp, its pictures' stamps, the manifest's
# entries, those that passed) -- a stamp being a file's (modification time, size) -- so a call that finds every file as
# it was reads none of them again, and a skipped picture's warning is logged once per change, not once per render; a
# manifest that is missing or cannot be read is remembered too, its entries None (fix round 1, M7)
_SEEN: tuple[tuple, tuple, list | None, list[dict]] | None = None


@dataclass(frozen=True)
class Image:
    """One picture chosen for a business: its manifest `file`, where it is on disk (`path`, for the upload to
    NewsBreak), and the words that go with it."""

    file: str
    path: Path
    alt: str
    licence: str
    source: str


def _words(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def problem(entry: object) -> str | None:
    """Why this manifest entry cannot be used -- a sentence for the operator's log -- or None when it can: an object
    with every field of `FIELDS`, a trade of `models.Category`, a picture inside the folder that is a JPEG or a PNG of
    at least 1200 x 627 pixels and at most 5 MB."""
    if not isinstance(entry, dict):
        return "is not an object"
    for field in ("file", "category", "licence", "source", "alt"):
        if not _words(entry.get(field)):
            return f'has no "{field}"'
    services = entry.get("services")
    if not isinstance(services, list) or not all(_words(s) for s in services):
        return '"services" is not a list of names'
    if entry["category"] not in CATEGORIES:
        return f'the category "{entry["category"]}" is not one of the trades'
    root = ROOT.resolve()
    path = (ROOT / entry["file"]).resolve()
    if not path.is_relative_to(root):
        return "is outside the creative folder"
    if not path.is_file():
        return "the file is not there"
    size = path.stat().st_size
    if size > MAX_BYTES:
        return f"is {size} bytes, larger than 5 MB"
    try:
        with PilImage.open(path) as picture:
            kind, (width, height) = picture.format, picture.size
    except (OSError, ValueError, PilImage.DecompressionBombError):
        return "is not a picture"
    if kind not in FORMATS:
        return f"is a {kind}, not a JPEG or a PNG"
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return f"is {width} × {height}, smaller than {MIN_WIDTH} × {MIN_HEIGHT}"
    return None


def _stamp(path: Path) -> tuple[int, int] | None:
    """A file as `manifest` remembers it: (modification time, size); None when it cannot be read."""
    try:
        found = path.stat()
    except (OSError, ValueError):
        return None
    return found.st_mtime_ns, found.st_size


def _entries() -> list | None:
    """The manifest's list of entries as written; None, with a warning, when it is missing, is not JSON or has none."""
    try:
        raw = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("creative: cannot read %s (%s)", MANIFEST, type(e).__name__)
        return None
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        log.warning("creative: %s has no list of entries", MANIFEST)
        return None
    return entries


def manifest() -> list[dict]:
    """The manifest's entries that pass `problem`, in the order written; each one skipped is a warning with its file and
    why. A manifest that is missing, is not JSON or has no list of entries is an empty set and a warning: a plan card
    never fails for the pictures. Read once, and again only when the manifest or one of the pictures it names changed
    (`_SEEN`) -- a manifest that cannot be read, too, is read and logged again only once it changes; the answer is the
    caller's own copy."""
    global _SEEN
    seen = _SEEN
    head = (str(ROOT), _stamp(ROOT / MANIFEST))
    entries = seen[2] if seen is not None and seen[0] == head else _entries()
    if entries is None:
        _SEEN = (head, (), None, [])
        return []
    files = tuple(
        _stamp(ROOT / e["file"]) if isinstance(e, dict) and isinstance(e.get("file"), str) else None for e in entries
    )
    if seen is not None and seen[0] == head and seen[1] == files:
        return copy.deepcopy(seen[3])
    usable = []
    for entry in entries:
        reason = problem(entry)
        if reason is None:
            usable.append(entry)
        else:
            named = entry.get("file") if isinstance(entry, dict) else None
            log.warning("creative: skipping %s: %s", named or "an entry", reason)
    _SEEN = (head, files, entries, usable)
    return copy.deepcopy(usable)


def _same(text: object) -> str:
    return " ".join(str(text).lower().split())


def pick(category: str | None, service: str | None, *, kept: str | None = None) -> Image | None:
    """The picture for a business of this trade offering this service: `kept` -- the picture a plan already carries,
    approved with an earlier version -- while the trade still has it (the running ads keep their picture, M1 Task 7);
    else an entry of the trade that lists the service (capitals and blanks do not matter), else the trade's first
    entry; None when the trade has none (no trade borrows another's)."""
    entries = [e for e in manifest() if e["category"] == category]
    if not entries:
        return None
    wanted = _same(service)
    chosen = next((e for e in entries if kept and e["file"] == kept), None) or next(
        (e for e in entries if wanted and wanted in {_same(s) for s in e["services"]}), entries[0]
    )
    return _image(chosen)


def find(file: str | None) -> Image | None:
    """The picture `file` names while the set has it, whatever its trade (M1 Task 7 fix round 1, item 4): a plan
    version's own, pinned when the version was made -- what its card shows and approving it approves -- looked up by
    its file, never chosen again; None for none, or once it left the set."""
    entry = next((e for e in manifest() if file and e["file"] == file), None)
    return _image(entry) if entry else None


def _image(entry: dict) -> Image:
    return Image(
        file=entry["file"],
        path=ROOT / entry["file"],
        alt=entry["alt"],
        licence=entry["licence"],
        source=entry["source"],
    )
