"""Site bundles (A3p-3 spec §6, P30-P37; plan 2026-10-04-agent-a3p-3-3-site-bundles.md B1-B13): a whole site an
assistant built -- a Next.js static export, a Vite or Astro build, a Hugo site -- uploaded as one zip of its build
output, checked, kept as content-addressed files and served by path. Admiry hosts the built files and never builds or
runs the site's code (P31).

`judge` reads an upload: the zip unpacked counting real bytes (never its headers), every path trick refused, a
project refused instead of its build, the contract (§6.1, §6.2: index.html at the top, the limits, the file types)
and safety (§6.4: every HTML page through `hosted.problems`, Admiry's script tag on every page, P30). It answers the
files and every problem at once, each `{file, rule, message, fix}` (§6.5): the assistant fixes its source, builds and
uploads again. Nothing here touches the database; `agent/site_upload.py` stores what passed.

The storage half (`store`, `save_preview`, the files of a preview or a version, `respond`, `zip_of`, `gc`) keeps
each distinct file once (`site_blob`, by SHA-256, P37) and a preview's or a version's files as rows naming them."""

from __future__ import annotations

import hashlib
import io
import re
import secrets
import stat
import warnings
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import BinaryIO
from urllib.parse import urlsplit

import psycopg
from fastapi.responses import PlainTextResponse, Response
from PIL import Image

from . import hosted

# ---------------------------------------------------------------- the limits (§6.2)

MAX_ZIP_BYTES = 25 * 1024 * 1024  # the upload itself
MAX_TOTAL_BYTES = 25 * 1024 * 1024  # unpacked, in total
MAX_FILES = 300
MAX_FILE_BYTES = 5 * 1024 * 1024  # one file that is neither a page nor an image
MAX_HTML_BYTES = hosted.MAX_HTML_BYTES  # one HTML file (today's single page)
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # one image (today's photo limits)
MAX_IMAGE_PIXELS = 40_000_000
MAX_PATH_CHARS = 200
MAX_DEPTH = 8  # folders
MAX_RATIO = 100  # a member unpacking to more than 100 times its packed size is a bomb ...
RATIO_FLOOR = 1024 * 1024  # ... once past its first megabyte (a small file of spaces is none)
MAX_PROBLEMS = 50  # in one answer; the say counts the rest
# The central directory is read whole before any member: a zip of nothing but empty entries would build hundreds of
# thousands of them from 25 MB. The count and the size its end record names are checked first (B4).
MAX_ENTRIES = 2_000
MAX_DIRECTORY_BYTES = 2 * 1024 * 1024
CHUNK = 64 * 1024

# the file types (§6.2) and how each is served
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    # a Next.js static export's client takes its route payloads (`*.txt`) as text/plain
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".webmanifest": "application/manifest+json",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
HTML = (".html", ".htm")
IMAGES = (".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif")  # raster: their pixels are counted
IMAGE_FORMATS = frozenset({"PNG", "JPEG", "MPO", "GIF", "WEBP", "AVIF", "ICO"})  # what Pillow may read them as
TYPE_LIST = " ".join(TYPES)
# what the kit and the upload link tell the assistant before it builds (§6.2, P33)
LIMITS = {
    "zip_mb": MAX_ZIP_BYTES // (1024 * 1024),
    "unpacked_mb": MAX_TOTAL_BYTES // (1024 * 1024),
    "files": MAX_FILES,
    "file_mb": MAX_FILE_BYTES // (1024 * 1024),
    "html_mb": MAX_HTML_BYTES / 1_000_000,
    "image_mb": MAX_IMAGE_BYTES // (1024 * 1024),
    "image_megapixels": MAX_IMAGE_PIXELS // 1_000_000,
    "path_characters": MAX_PATH_CHARS,
    "folders_deep": MAX_DEPTH,
    "types": list(TYPES),
}
# a Mac's zip adds these, Windows' Explorer those: never part of a site, never served (B4)
JUNK_DIRS = ("__MACOSX/",)
JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# Admiry's own notices on every hosted site (B5)
RESERVED = frozenset(
    {
        "privacy.html",
        "privacy.htm",
        "privacy/index.html",
        "privacy/index.htm",
        "terms.html",
        "terms.htm",
        "terms/index.html",
        "terms/index.htm",
    }
)
_KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_KEY_EXTENSIONS = (".pem", ".key", ".p12", ".pfx")
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# ---------------------------------------------------------------- the sentences (§6.5)

REFUSED = "Your assistant's site needs a few fixes before it can go live."
READY = "The site passed Admiry's checks."
ZIP_FIX = "Zip the files inside the build folder with a plain zip (cd out && zip -r ../site.zip .) and upload that."
SMALLER_FIX = "Make the site smaller: compress the images and leave out the files the site does not use."
PROJECT_FIX = (
    "Build the site and upload the files inside its build folder: out/ for Next.js (output: 'export'), dist/ for "
    "Vite and Astro, public/ for Hugo."
)
FIXES = {
    # the sanitiser's rules (hosted.problems)
    "size": "Move large inline data, styles or scripts into files of their own.",
    "comment": "Close every HTML comment with -->.",
    "text": (
        "Put the page's words in its HTML: prerender the page (a static export does) rather than drawing it with "
        "JavaScript, and say what the business does in a few paragraphs."
    ),
    "script_host": "Bundle the library into the site instead of loading it from a CDN.",
    "frame_host": "Embed only a booking page (Calendly, Acuity, Square) or a Google map; link to anything else.",
    "object": "Remove <embed> and <object>; link to the file, or embed a booking page.",
    "srcdoc": "Give the frame a booking page's address (src) instead of inline content.",
    "event_handler": "Move the code into a script that adds a listener (addEventListener).",
    "meta_refresh": 'Remove the <meta http-equiv="refresh"> tag.',
    "navigation": "Remove the script's navigation; link instead.",
    "base_href": "Remove the <base> tag, or point it at this site.",
    "scheme": "Use https: (or mailto:, tel:, sms:) addresses; no javascript: or data: in links and sources.",
    # the bundle's own
    "zip": ZIP_FIX,
    "zip_size": SMALLER_FIX,
    "total_size": SMALLER_FIX,
    "file_count": "Leave out the files the site does not use (source maps, unused images).",
    "file_size": "Make it smaller (compress or resize it), or leave it out.",
    "zip_ratio": "Upload the site's real files: a file that packs that small is not one.",
    "project": PROJECT_FIX,
    "file_type": "Remove it from the build, or turn it into one of those types.",
    "image": "Export it again as PNG, JPEG, WebP or AVIF.",
    "image_pixels": "Resize it: a page never needs more than a few thousand pixels across.",
    "encoding": "Save the page as UTF-8.",
    "index": "The home page must be index.html at the top of the zip: zip the files inside the build folder.",
    "reserved": "Remove it from the build and link to /privacy and /terms instead.",
}


def problem(file: str, rule: str, message: str, fix: str | None = None) -> dict:
    """One structured problem (§6.5): the file it is in ("/" for the upload as a whole), the rule, the sentence, and
    what to do about it -- every rule has a fix line."""
    return {"file": file, "rule": rule, "message": message, "fix": fix or FIXES[rule]}


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------- unpacking (§6.3, B4)


@dataclass
class Judged:
    """What `judge` read: the files (path -> bytes, the zip's order) and every problem; `home` and `title` are the
    home page's HTML and <title> when it has one."""

    files: dict[str, bytes] = field(default_factory=dict)
    problems: list[dict] = field(default_factory=list)
    home: str | None = None
    title: str | None = None
    stopped: bool = False  # the zip is no site's build: nothing past the first problem is read

    def stop(self, found: dict) -> Judged:
        self.problems.append(found)
        self.stopped = True
        return self

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def size(self) -> int:
        return sum(len(b) for b in self.files.values())


def _end_record(f: BinaryIO) -> tuple[int, int] | None:
    """(entries, central directory bytes) as the zip's end record names them -- the ZIP64 record's when the plain one
    says it is too small to hold them -- or None when there is no end record (zipfile then refuses the file). Read
    before zipfile reads the whole directory into memory (B4)."""
    f.seek(0, io.SEEK_END)
    end = f.tell()
    window = min(end, 22 + 65_535)
    f.seek(end - window)
    tail = f.read(window)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or at + 22 > len(tail):
        return None
    entries = int.from_bytes(tail[at + 10 : at + 12], "little")
    size = int.from_bytes(tail[at + 12 : at + 16], "little")
    locator = at - 20
    if (entries == 0xFFFF or size == 0xFFFFFFFF) and locator >= 0 and tail[locator : locator + 4] == b"PK\x06\x07":
        offset = int.from_bytes(tail[locator + 8 : locator + 16], "little")
        if offset + 56 <= end:
            f.seek(offset)
            record = f.read(56)
            if record[:4] == b"PK\x06\x06":
                entries = int.from_bytes(record[32:40], "little")
                size = int.from_bytes(record[40:48], "little")
    return entries, size


def _plain_path(name: str) -> str | None:
    """The name as a plain relative path, or None for a trick: a control character (NUL included), a backslash, an
    absolute path (`/x`, `C:x`), an empty, `.` or `..` part. A leading `./` (some tools write it) is dropped first."""
    if _CONTROL.search(name) or "\\" in name:
        return None
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or _DRIVE.match(name):
        return None
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return name


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _junk(name: str) -> bool:
    return name.startswith(JUNK_DIRS) or name.rsplit("/", 1)[-1] in JUNK_NAMES


def _project_signs(names: list[str], paths: list[str]) -> list[str]:
    """What makes the upload a project rather than its build (B4): node_modules/, .git/, an .env file or a private key
    anywhere in the zip (`names`, as zipped), or a package.json at the top of the site (`paths`, read from inside a
    wrapping folder) with no index.html beside it."""
    found: list[str] = []
    for name in names:
        parts = name.split("/")
        base = parts[-1]
        if "node_modules" in parts[:-1]:
            found.append("node_modules/")
        elif ".git" in parts[:-1]:
            found.append(".git/")
        elif base.startswith(".env") or base in _KEY_NAMES or base.lower().endswith(_KEY_EXTENSIONS):
            found.append(name)
    if "package.json" in paths and "index.html" not in paths:
        found.append("package.json")
    return list(dict.fromkeys(found))


def _extension(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _limit(path: str) -> int:
    ext = _extension(path)
    if ext in HTML:
        return MAX_HTML_BYTES
    if ext in IMAGES:
        return MAX_IMAGE_BYTES
    return MAX_FILE_BYTES


def unpack(f: BinaryIO) -> Judged:
    """The zip `f` unpacked (§6.3): every name checked before anything is read, then every member read counting its
    real bytes. Stops at the first sign the zip is not a site's build (a path trick, a bomb, a project); otherwise
    keeps reading so one answer lists every problem it can."""
    out = Judged()
    record = _end_record(f)
    if record is not None and (record[0] > MAX_ENTRIES or record[1] > MAX_DIRECTORY_BYTES):
        return out.stop(problem("/", "file_count", f"The zip holds more than {MAX_FILES} files."))
    f.seek(0)
    try:
        zf = zipfile.ZipFile(f)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
        return out.stop(problem("/", "zip", "The upload is not a zip file Admiry can read."))
    with zf:
        members: list[tuple[str, zipfile.ZipInfo]] = []
        seen: dict[str, str] = {}
        for info in zf.infolist():
            name = info.orig_filename  # as zipped: zipfile's own `filename` is cut at a NUL
            path = _plain_path(name)
            if path is None:
                return out.stop(problem(name, "path", f"{name!r} is not a plain relative path.", ZIP_FIX))
            if _junk(path):
                continue
            if _is_symlink(info):
                fix = "Replace the link with the file itself."
                return out.stop(problem(path, "path", f"{path} is a symbolic link.", fix))
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                return out.stop(problem(path, "zip", f"{path} is encrypted.", ZIP_FIX))
            low = path.lower()
            if low in seen:
                first = seen[low]
                if first == path:
                    fix = "Zip the folder again, into a new file."
                    return out.stop(problem(path, "path", f"{path} is in the zip twice.", fix))
                fix = "Rename one of them: names that differ only by case are easy to confuse on the web."
                return out.stop(problem(path, "path", f"{first} and {path} differ only by case.", fix))
            seen[low] = path
            members.append((path, info))
            if len(members) > MAX_FILES:
                return out.stop(problem("/", "file_count", f"The site has more than {MAX_FILES} files."))
        names = [p for p, _ in members]
        members = _unwrapped(members)
        signs = _project_signs(names, [p for p, _ in members])
        if signs:
            return out.stop(problem("/", "project", "This is a project, not its build: " + ", ".join(signs[:5]) + "."))
        total = 0
        for path, info in members:
            ext = _extension(path)
            if ext not in TYPES:
                out.problems.append(
                    problem(path, "file_type", f"{path}: Admiry hosts only these types of files: {TYPE_LIST}.")
                )
                continue
            if len(path) > MAX_PATH_CHARS:
                out.problems.append(
                    problem(path, "path", f"{path} is longer than {MAX_PATH_CHARS} characters.", "Shorten its path.")
                )
                continue
            if path.count("/") > MAX_DEPTH:
                out.problems.append(
                    problem(
                        path,
                        "path",
                        f"{path} is more than {MAX_DEPTH} folders deep.",
                        f"Move it up: at most {MAX_DEPTH} folders deep.",
                    )
                )
                continue
            if path.lower() in RESERVED:
                out.problems.append(
                    problem(path, "reserved", f"{path}: Admiry serves the site's privacy notice and terms itself.")
                )
                continue
            limit = _limit(path)
            try:
                data, why = _read(zf, info, limit, MAX_TOTAL_BYTES - total)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError, ValueError):
                return out.stop(problem(path, "zip", f"{path} could not be unpacked.", ZIP_FIX))
            if why == "ratio":
                return out.stop(
                    problem(path, "zip_ratio", f"{path} unpacks to more than {MAX_RATIO} times its packed size.")
                )
            if why == "total":
                return out.stop(problem("/", "total_size", f"The site is more than {_mb(MAX_TOTAL_BYTES)} unpacked."))
            if why == "file":
                kind = "an HTML page" if ext in HTML else ("an image" if ext in IMAGES else "a file")
                out.problems.append(
                    problem(path, "file_size", f"{path} is more than {_mb(limit)}; {kind} may be {_mb(limit)}.")
                )
                continue
            total += len(data)
            out.files[path] = data
    return out


def _unwrapped(members: list[tuple[str, zipfile.ZipInfo]]) -> list[tuple[str, zipfile.ZipInfo]]:
    """Read from inside the one folder every entry sits in (`zip -r site.zip out` names them all `out/…`, B4)."""
    if not members or any("/" not in p for p, _ in members):
        return members
    tops = {p.split("/", 1)[0] for p, _ in members}
    if len(tops) != 1:
        return members
    return [(p.split("/", 1)[1], info) for p, info in members]


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, room: int) -> tuple[bytes, str | None]:
    """One member's bytes, counted as they come (never `info.file_size`): (bytes, None), or (what was read, why it
    stopped): "file" past `limit`, "total" past the `room` left of the site's 25 MB, "ratio" past 100 times its
    packed size once past its first megabyte."""
    buf = io.BytesIO()
    packed = max(info.compress_size, 1)
    with zf.open(info) as m:
        while True:
            chunk = m.read(CHUNK)
            if not chunk:
                break
            buf.write(chunk)
            n = buf.tell()
            if n > RATIO_FLOOR and n > MAX_RATIO * packed:
                return b"", "ratio"
            if n > room:
                return b"", "total"
            if n > limit:
                return b"", "file"
    return buf.getvalue(), None


# ---------------------------------------------------------------- the checks (§6.4, B5, B6)


class _Tags(HTMLParser):
    """The home page's <title>, and whether a page carries Admiry's script tag for `page_id` (P30): a <script> whose
    `src` is the agent's `/t/admiry.js` and whose `data-page` is the page's id."""

    def __init__(self, page_id: str, agent_host: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_id = page_id
        self.agent_host = agent_host
        self.tracked = False
        self.title: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and a.get("data-page") == self.page_id:
            src = a.get("src") or ""
            host = hosted._url_host(src)
            if host and host.lower() == self.agent_host and urlsplit(src.replace("\\", "/")).path == "/t/admiry.js":
                self.tracked = True
        elif tag == "title" and not self.title:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)


def _tags(text: str, page_id: str, agent_base: str) -> _Tags:
    p = _Tags(page_id, (urlsplit(agent_base).hostname or agent_base).lower())
    p.feed(text)
    p.close()
    return p


def _image_problem(path: str, data: bytes) -> dict | None:
    """An image's header read (never its pixels): one Pillow can open as an image type the web shows, of at most
    40 million pixels."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            fmt, (w, h) = img.format, img.size
    except Exception:  # noqa: BLE001 - a hostile file can make an image plugin raise anything; it is no image
        return problem(path, "image", f"{path} is not an image Admiry can read.")
    if fmt not in IMAGE_FORMATS:
        return problem(path, "image", f"{path} is not a PNG, JPEG, GIF, WebP, AVIF or ICO image.")
    if w * h > MAX_IMAGE_PIXELS:
        return problem(path, "image_pixels", f"{path} is {w}×{h} pixels; an image may have 40 million.")
    return None


def check(judged: Judged, *, page_id: str, own_host: str | None, agent_base: str, snippet: str) -> None:
    """The contract and safety checks on what `unpack` read (§6.4 items 1 and 2), adding to `judged.problems`:
    index.html at the top; every image readable and within its pixels; every HTML page UTF-8, through the sanitiser
    (B6: the text minimum on the home page alone, inline scripts not read line by line), and carrying Admiry's script
    tag (P30; `snippet` is the kit's, for the fix). JavaScript, CSS, JSON and SVG files are not read."""
    if "index.html" not in judged.files:
        judged.problems.append(problem("/", "index", "There is no index.html at the top of the zip."))
    tracking_fix = (
        "Put the kit's script tag on every page as a plain <script> tag -- in Next.js in app/layout's <head>, not "
        f"with next/script: {snippet}"
    )
    form_fix = f"Post the form to {agent_base}/t/form/{page_id}, the kit's form address."
    for path, data in judged.files.items():
        ext = _extension(path)
        if ext in IMAGES:
            found = _image_problem(path, data)
            if found:
                judged.problems.append(found)
            continue
        if ext not in HTML:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            judged.problems.append(problem(path, "encoding", f"{path} is not UTF-8 text."))
            continue
        home = path == "index.html"
        for rule, sentence in hosted.problems(
            text,
            page_id=page_id,
            own_host=own_host,
            agent_base=agent_base,
            text_minimum=home,
            inline_scripts=False,
        ):
            judged.problems.append(problem(path, rule, sentence, form_fix if rule == "form_target" else None))
        tags = _tags(text, page_id, agent_base)
        if not tags.tracked:
            judged.problems.append(
                problem(path, "tracking", f"{path} does not carry Admiry's script tag for this page.", tracking_fix)
            )
        if home:
            judged.home = text
            title = " ".join("".join(tags.title).split())
            judged.title = title[:200] or None


def judge(f: BinaryIO, *, page_id: str, own_host: str | None, agent_base: str, snippet: str) -> Judged:
    """`unpack` then, unless it stopped (the zip is no site's build), `check`: one answer lists every problem."""
    judged = unpack(f)
    if not judged.stopped:
        check(judged, page_id=page_id, own_host=own_host, agent_base=agent_base, snippet=snippet)
    return judged


def answer(judged: Judged) -> dict:
    """The refusal's body (§6.5): the say, and the first MAX_PROBLEMS problems (the say counts the rest)."""
    shown = judged.problems[:MAX_PROBLEMS]
    more = len(judged.problems) - len(shown)
    say = REFUSED if not more else f"{REFUSED} The first {len(shown)} of {len(judged.problems)} are listed."
    return {"status": "refused", "say": say, "problems": shown}


# ---------------------------------------------------------------- storage (§6.7, P37, B1, B7)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store(conn: psycopg.Connection, files: dict[str, bytes]) -> dict[str, str]:
    """Every file's bytes kept once by its SHA-256 (P37), inside the caller's transaction; returns path -> SHA-256.
    A blob already kept is only touched (`touched_at`, which the collection respects: the UPDATE holds its row until
    the caller commits, so `gc` never deletes it under the new rows naming it)."""
    by_path = {path: sha256(data) for path, data in files.items()}
    unique = {sha: files[path] for path, sha in by_path.items()}
    kept = {
        r["sha256"]
        for r in conn.execute(
            "UPDATE site_blob SET touched_at = now() WHERE sha256 = ANY(%s) RETURNING sha256", (list(unique),)
        ).fetchall()
    }
    for sha, data in unique.items():
        if sha not in kept:
            conn.execute(
                "INSERT INTO site_blob (sha256, bytes, size) VALUES (%s, %s, %s) "
                "ON CONFLICT (sha256) DO UPDATE SET touched_at = now()",
                (sha, data, len(data)),
            )
    return by_path


def touch(conn: psycopg.Connection, files: dict[str, str]) -> None:
    """Mark a file list's blobs as named now (B7), inside the caller's transaction: the collection skips them."""
    conn.execute("UPDATE site_blob SET touched_at = now() WHERE sha256 = ANY(%s)", (list(set(files.values())),))


def preview_host(conn: psycopg.Connection, page_id: str, domain: str) -> str:
    """The business's preview host (P35, B8): `preview--<16 hex>.<pages domain>` -- `--` is a form no slug has
    (`hosted.slugify` folds every run of anything else to one hyphen) -- random, never derived from the page id
    every live page prints, minted once and kept, so a business needs one certificate for it."""
    row = conn.execute("SELECT preview_host FROM page WHERE id = %s", (page_id,)).fetchone()
    if row and row["preview_host"] and row["preview_host"].endswith("." + domain):
        return row["preview_host"]
    host = f"preview--{secrets.token_hex(8)}.{domain}"
    conn.execute("UPDATE page SET preview_host = %s WHERE id = %s", (host, page_id))
    return host


def is_preview_host(host: str | None) -> bool:
    return bool(host) and host.lower().startswith("preview--")


def save_preview(
    conn: psycopg.Connection,
    page_id: str,
    judged: Judged,
    files: dict[str, str],
    *,
    ttl=hosted.PREVIEW_TTL,
    sections: dict | None = None,
) -> str:
    """A bundle preview (B7) inside the caller's transaction: the preview row (`html` the home page as uploaded,
    `title` its <title>), its files, and the business's earlier bundle previews ended -- one preview per business,
    a new upload replaces it (P35). `sections`: what Admiry built the bundle from (A3p-6 Q6: an industry site's
    `{"kind": "site", ...}`), None for an assistant's upload. Returns its token."""
    token = hosted.save_preview(
        conn, page_id, judged.home or "", judged.title, kind="bundle", ttl=ttl, sections=sections
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO page_preview_file (token, path, sha256) VALUES (%s, %s, %s)",
            [(token, path, sha) for path, sha in files.items()],
        )
    conn.execute(
        "UPDATE page_preview SET expires_at = now() WHERE page_id = %s AND kind = 'bundle' AND token <> %s "
        "AND expires_at > now()",
        (page_id, token),
    )
    return token


def preview_files(conn: psycopg.Connection, token: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT path, sha256 FROM page_preview_file WHERE token = %s ORDER BY path", (token,)
    ).fetchall()
    return {r["path"]: r["sha256"] for r in rows}


def version_files(conn: psycopg.Connection, page_id: str, version: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT path, sha256 FROM page_version_file WHERE page_id = %s AND version = %s ORDER BY path",
        (page_id, version),
    ).fetchall()
    return {r["path"]: r["sha256"] for r in rows}


def add_version_files(conn: psycopg.Connection, page_id: str, version: int, files: dict[str, str]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO page_version_file (page_id, version, path, sha256) VALUES (%s, %s, %s, %s)",
            [(page_id, version, path, sha) for path, sha in files.items()],
        )


def copy_shots(conn: psycopg.Connection, page_id: str, version: int, payload: dict) -> None:
    """The render check's screenshots (A3p-3 batch 4, R4) onto a new bundle version: the preview's it was published
    from (`payload["token"]`), or the older version's it brings back (`payload["version"]`)."""
    if payload.get("token"):
        conn.execute(
            "UPDATE page_version v SET shot_phone = p.shot_phone, shot_tablet = p.shot_tablet, "
            "shot_desktop = p.shot_desktop FROM page_preview p "
            "WHERE p.token = %s AND v.page_id = %s AND v.version = %s",
            (payload["token"], page_id, version),
        )
    elif payload.get("version") is not None:
        conn.execute(
            "UPDATE page_version v SET shot_phone = o.shot_phone, shot_tablet = o.shot_tablet, "
            "shot_desktop = o.shot_desktop FROM page_version o "
            "WHERE o.page_id = %s AND o.version = %s AND v.page_id = %s AND v.version = %s",
            (page_id, payload["version"], page_id, version),
        )


def shot(conn: psycopg.Connection, page_id: str, version: int, size: str) -> bytes | None:
    """A version's screenshot (`phone`, `tablet` or `desktop`), or None."""
    column = {"phone": "shot_phone", "tablet": "shot_tablet", "desktop": "shot_desktop"}[size]
    row = conn.execute(
        f"SELECT {column} AS shot FROM page_version WHERE page_id = %s AND version = %s", (page_id, version)
    ).fetchone()
    return bytes(row["shot"]) if row and row["shot"] else None


def sizes(conn: psycopg.Connection, files: dict[str, str]) -> list[dict]:
    """[{path, size}] for a file list, by path (the source view, §7)."""
    rows = conn.execute(
        "SELECT sha256, size FROM site_blob WHERE sha256 = ANY(%s)", (list(set(files.values())),)
    ).fetchall()
    size = {r["sha256"]: r["size"] for r in rows}
    return [{"path": path, "size": size.get(sha, 0)} for path, sha in sorted(files.items())]


def zip_of(conn: psycopg.Connection, files: dict[str, str]) -> bytes:
    """The files as one zip, built from the blobs when asked (the source's download, P19)."""
    rows = conn.execute(
        "SELECT sha256, bytes FROM site_blob WHERE sha256 = ANY(%s)", (list(set(files.values())),)
    ).fetchall()
    blobs = {r["sha256"]: bytes(r["bytes"]) for r in rows}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, sha in sorted(files.items()):
            if sha in blobs:
                zf.writestr(path, blobs[sha])
    return buf.getvalue()


def gc(conn: psycopg.Connection, *, i: int = 0, n: int = 1) -> int:
    """Blobs nothing names any more -- no preview's file, no version's, no stock photo or its sizes (A3p-6 Q9: the
    library is never collected) -- untouched for a day (B13), of shard `i` of `n`; returns how many went. A blob an upload is naming right now was touched by it (`store`), so it stays."""
    return conn.execute(
        "DELETE FROM site_blob b WHERE b.touched_at < now() - interval '1 day' "
        "AND mod(abs(hashtext(b.sha256)::bigint), %(n)s) = %(i)s "
        "AND NOT EXISTS (SELECT 1 FROM page_version_file f WHERE f.sha256 = b.sha256) "
        "AND NOT EXISTS (SELECT 1 FROM page_preview_file f WHERE f.sha256 = b.sha256) "
        "AND NOT EXISTS (SELECT 1 FROM site_asset a WHERE a.blob_sha256 = b.sha256) "
        "AND NOT EXISTS (SELECT 1 FROM site_asset_file f WHERE f.sha256 = b.sha256)",
        {"i": i, "n": n},
    ).rowcount


# ---------------------------------------------------------------- serving by path (§6.6, B9)

_HASHED_DIRS = ("_next/static/", "_astro/")
_HASH_PART = re.compile(r"^(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{8,}$")
SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
HTML_MAX_AGE = 300
FILE_MAX_AGE = 3600
HASHED_MAX_AGE = 31_536_000


def candidates(path: str) -> list[str]:
    """The stored files a request path may name, in order (§6.1): `/` the home page, `/x/` its index.html, `/x` the
    file itself, then x.html, then x/index.html."""
    p = path.lstrip("/")
    if p == "" or p.endswith("/"):
        return [p + "index.html"]
    return [p, p + ".html", p + "/index.html"]


def hashed(path: str) -> bool:
    """A name that carries its content's hash (a build tool's output): served for a year (B9)."""
    if path.startswith(_HASHED_DIRS):
        return True
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return any(_HASH_PART.match(part) for part in re.split(r"[.\-]", stem))


def cache_control(path: str) -> str:
    ext = _extension(path)
    if ext in HTML:
        return f"public, max-age={HTML_MAX_AGE}"
    if hashed(path):
        return f"public, max-age={HASHED_MAX_AGE}, immutable"
    return f"public, max-age={FILE_MAX_AGE}"


def named(conn: psycopg.Connection, source: tuple, names: list[str]) -> dict[str, str]:
    """The files among `names` (path -> SHA-256) of `source`: ("version", page_id, version) -- a published bundle --
    or ("preview", token). One indexed read per request, never the whole list."""
    if source[0] == "version":
        rows = conn.execute(
            "SELECT path, sha256 FROM page_version_file WHERE page_id = %s AND version = %s AND path = ANY(%s)",
            (source[1], source[2], names),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT path, sha256 FROM page_preview_file WHERE token = %s AND path = ANY(%s)", (source[1], names)
        ).fetchall()
    return {r["path"]: r["sha256"] for r in rows}


def respond(
    conn: psycopg.Connection,
    source: tuple,
    path: str,
    headers: dict[str, str],
    *,
    if_none_match: str | None = None,
    preview: bool = False,
    robots: str | None = None,
) -> Response:
    """One request of a bundle (B9): the stored file the path names; else, for /robots.txt, `robots` (Admiry's own,
    when the bundle has none); else the bundle's own 404.html with 404, else the plain 404. `headers` are the hosted
    page's (A3p-2, H6); the ETag is the file's SHA-256 (a match answers 304); the cache is the file's (a preview's is
    none); an SVG gets a policy that runs nothing."""
    names = candidates(path)
    found = named(conn, source, [*names, "404.html"])
    hit = next(((name, found[name]) for name in names if name in found), None)
    status = 200
    if hit is None and robots is not None and path == "/robots.txt":
        return PlainTextResponse(
            robots, headers={**headers, "Cache-Control": "no-store" if preview else "public, max-age=3600"}
        )
    if hit is None and "404.html" in found:
        hit, status = ("404.html", found["404.html"]), 404
    if hit is None:
        return PlainTextResponse("Not found.", status_code=404, headers={**headers, "Cache-Control": "no-store"})
    stored, sha = hit
    etag = f'"{sha}"'
    cache = "no-store" if preview or status == 404 else cache_control(stored)
    h = {**headers, "ETag": etag, "Cache-Control": cache}
    if _extension(stored) == ".svg":
        h["Content-Security-Policy"] = SVG_POLICY
    if _extension(stored) == ".woff2":
        # a font is fetched in CORS mode, and a preview runs sandboxed (an opaque origin, `hosted.csp(preview=True)`):
        # without this the browser (and the render check's screenshots) falls back to another face. Fonts only --
        # public files (OFL) with nothing of the business's in them
        h["Access-Control-Allow-Origin"] = "*"
    if status == 200 and if_none_match and etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers=h)
    row = conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()
    if row is None:
        return PlainTextResponse("Not found.", status_code=404, headers={**headers, "Cache-Control": "no-store"})
    return Response(bytes(row["bytes"]), status_code=status, media_type=TYPES[_extension(stored)], headers=h)
