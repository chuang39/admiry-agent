"""The owner's photos (A3p-2 Task 6; spec 2026-09-28-admiry-pages-design.md §7, plan H13, ledger H35/H37): a
phone-friendly upload link (`create_page` mints the token; `update_page`/`revise_page` show the same one back
while it is still valid) that needs no assistant and no MCP call to use -- `GET /u/{token}` renders a small,
script-free form; `POST /u/{token}` stores what was sent, or removes one photo (`action=remove&photo=<id>`).

A photo is never kept as the phone sent it: each one is read by its magic bytes alone (never the filename or
the browser's declared Content-Type), decoded, EXIF-transposed (H37 -- a phone's JPEG keeps the sensor's
landscape pixels plus a rotation tag; apply the rotation to the pixels themselves before anything else, or a
portrait shot renders sideways), shrunk to at most 1600 px on its long side and re-encoded as a plain JPEG
with no EXIF of its own (`Image.save` only carries metadata forward when told to, and this never passes
`exif=`, so the output is clean by omission). A batch of files is all-or-nothing, memory-safely, and without
ever holding the page row's own lock for longer than a handful of INSERTs takes (H39, review round 2): the
five-photo cap is checked once, optimistically and without a lock, before a single file is even read; every
file is then read, decoded, resized and re-encoded in turn -- never more than one file's bytes and one
decoded image alive at once -- with no database connection open for the batch at all while this, the slow
part, happens; only once every file has been validated and encoded does one short transaction open at all,
re-checking the cap for real (the authoritative check, under the row's lock) and writing every photo in the
batch together, so a bad file anywhere in it still leaves nothing stored, and a concurrent writer to the same
page is blocked only as long as that last, fast step takes -- never as long as the client's own upload or
Pillow's own decode did. The request body itself is bounded twice over: Content-Length must be present and
within budget before anything is parsed, and the bytes actually received are counted as they stream in, so a
body that lies about its own size (or omits the header to dodge the first check) is still cut off the
instant it passes budget.

Every successful change (an upload or a removal) re-renders the page from its current `sections` through the
exact path `update_page`/`revise_page` use (`page_builder.finish` + `page_builder.save`), so `sections.version`
advances the same way and a fresh preview always exists to show; the response is the same upload page again,
its thumbnails and message (or refusal) reflecting what just happened.

An industry site's page (A3p-6 batch 2 Task 3, plan Q7; `photos.is_site`) is another page on the same link, and the
starter page's path above is never reached for it. The owner says what each batch shows (`kind`, one of its family's:
`photos.KINDS`), and a batch is taken only with one and only up to what that kind still has room for -- its `need`,
counted under the page row's lock like the starter's five; the logo is the exception: a new one replaces the one before,
in the same transaction. A logo keeps its transparency (a PNG, `_process(logo=True)`) at most 800 px across and 300 KB.
The thumbnails come from the link itself (`GET /u/{token}/photo/{id}`): a site's preview is a bundle, which serves no
photo route. After a batch or a removal has committed -- never inside its transaction, never holding the page row --
the site's preview is built again without the model and without the render check (`photos.rebuild`), and the page
says where to see it; when that fails the photo is kept, and the page says the site shows it the next time it is
built."""

from __future__ import annotations

import io

import psycopg
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException

from . import config, db, hosted, landing, leads, page_builder
from .models import PageCopy
from .sites import photos as site_photos
from .templating import env as _env

router = APIRouter()

MAX_PHOTOS = 5
MAX_BODY_BYTES = 40 * 1024 * 1024  # the whole request, checked by Content-Length before anything is parsed
MAX_PART_BYTES = 8 * 1024 * 1024  # one file part; enforced by hand in upload_post (see its docstring)
# Files one form may carry: more than a starter page's five, so that a longer batch for an industry site is refused by
# its kind's own cap, in that cap's own sentence, and not by the parser as a form that could not be read
FORM_LIMITS = {
    "max_files": max(MAX_PHOTOS, *site_photos.MAX_SITE_PHOTOS.values()),
    "max_fields": 10,
    "max_part_size": MAX_PART_BYTES,
}
MAX_DIM = 1600
TARGET_BYTES = 600 * 1024
QUALITY_LADDER = (82, 70, 60)
# A logo (A3p-6 batch 2 Task 3): the header of a site shows it small, so it is kept small -- and as a PNG when it has
# transparency; one that is still over its bytes is scaled down by LOGO_SCALE until it is not, or until its long side
# is MIN_LOGO_DIM or less
MAX_LOGO_DIM = 800
LOGO_TARGET_BYTES = 300 * 1024
MIN_LOGO_DIM = 200
LOGO_SCALE = 0.8
LOGO = "logo"  # the kind whose new photo replaces the one before

TOO_MUCH = "That is too much to send at once; add photos a few at a time."
NO_CONTENT_LENGTH = "Send the photos from a browser."
BAD_REQUEST = "That upload could not be read; please try again."
BAD_FORMAT = "That file is not a JPEG, PNG or WebP photo."
BAD_IMAGE = "That photo could not be read; try again or use a different one."
TOO_MANY = "Five photos at most; remove one first"
NEED_A_FILE = "Choose at least one photo."
# An industry site's page (Q12's words; the cap sentence is fix round 0's, which sets the label in curly quotes as the
# code base writes what it refuses -- the plan's own sentence did not read with the longer labels)
NEED_A_KIND = "Say what the photos show first."
KIND_FULL = "The site has all it uses for “{label}”; remove one first."
SAVED = "Saved."
# the preview's address follows "Saved." as a button the owner opens (the founder's dev walk, 2026-10-08: it was plain
# text to copy; the phone redesign, 2026-10-15: a button, not the address spelled out)
SEE_SITE = "See your site"
SEE_PAGE = "See your page"  # a starter page's
SAVED_LATER = "Saved. Your site shows them the next time it is built."

# H37: the decompression-bomb ceiling for this upload path specifically -- a process-global Pillow setting
# (agent/uploads.py is the only module that decodes an image at all), set once at import time rather than
# per call.
Image.MAX_IMAGE_PIXELS = 40_000_000

_MAGIC_JPEG = b"\xff\xd8\xff"
_MAGIC_PNG = b"\x89PNG\r\n\x1a\n"


def _headers(agent_base: str) -> dict[str, str]:
    """H13: a strict CSP like the hosted pages' own, but scoped to exactly what this plain form needs -- this origin's
    own script and nothing inline (upload.js, since the phone redesign of 2026-10-15: a photo chosen in a square sends
    itself; every action is still a full-page form post, never fetch/XHR, so no connect-src), images from this same
    origin and from the agent host (`agent_base`): the thumbnails are a preview's own photos, which answer there only
    (H42), while this page is linked on the browser host. Its look is the shared stylesheets' and font (base.css,
    page.css, upload.css: every Admiry page looks like the front page, ruling 2026-10-07), from this origin only."""
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' {agent_base} "
            "data:; form-action 'self'"
        ),
        "X-Robots-Tag": "noindex",
        "Cache-Control": "no-store",  # the photo count shown here changes on every visit; never cached
    }


# ---------------------------------------------------------------- the whole body's own byte budget (H38)


class _TooLarge(Exception):
    """Raised by `_counted_receive`'s wrapped `receive` the instant a request body -- multipart or not --
    passes MAX_BODY_BYTES as it actually arrives, whatever Content-Length claimed (including a Content-Length
    `upload_post` already let through, or a request with none at all -- this is the one check nothing can
    dodge)."""


def _counted_receive(receive, limit: int):
    """Wraps an ASGI `receive` callable so Starlette's own `Request.stream()` -- what every form parser reads
    from, multipart or urlencoded alike, `request.form()` included -- raises `_TooLarge` the moment the
    running total of body bytes actually received passes `limit`. `request._receive` has no public
    replacement hook; reassigning it before the first read is the only way to see every byte as it streams
    in rather than trust a header that can simply lie (H38, review round 1)."""
    total = 0

    async def wrapped():
        nonlocal total
        message = await receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > limit:
                raise _TooLarge()
        return message

    return wrapped


# ---------------------------------------------------------------- one photo: sniff, decode, resize, re-encode


class _BadPhoto(Exception):
    """What `_process` found wrong with one photo, for the caller to turn into a response (status, message)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _sniff(head: bytes) -> str | None:
    """The photo format `head` (a part's first bytes) actually is, by its magic bytes alone (H13: JPEG `FF D8
    FF`, PNG's eight-byte signature, WebP's `RIFF????WEBP`) -- never the filename or the part's declared
    Content-Type, either of which a file can carry while holding anything at all. None for anything else."""
    if head.startswith(_MAGIC_JPEG):
        return "image/jpeg"
    if head.startswith(_MAGIC_PNG):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _jpeg(img: Image.Image, target: int) -> bytes:
    """`img` as a plain JPEG, no EXIF: at the first quality of the ladder (82, then 70, then 60) that fits in `target`
    bytes, else the last one's own size regardless."""
    out = b""
    for quality in QUALITY_LADDER:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= target:
            break
    return out


def _has_alpha(img: Image.Image) -> bool:
    """Whether some of `img`'s pixels can be see-through: an alpha channel, or a palette's or a colour key's clear
    colour (`Image.has_transparency_data` says the same, from Pillow 10.1; this service allows 10.0)."""
    return img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in img.info


def _logo(img: Image.Image) -> tuple[bytes, int, int]:
    """A logo (A3p-6 batch 2 Task 3, plan Q7), at most MAX_LOGO_DIM on the long side and LOGO_TARGET_BYTES. One with
    transparency stays a PNG with its alpha channel -- a logo flattened into a JPEG carries a white box into a header
    of another colour --, any other is a JPEG, as every photo is. If it is still over its bytes after that (a JPEG's
    ladder of qualities; a PNG has none), it is scaled down by LOGO_SCALE and encoded again, until it fits or its long
    side is down to MIN_LOGO_DIM or less: a logo is small by nature, and a heavy one is a picture of something, not a
    mark."""
    png = _has_alpha(img)
    img = img.convert("RGBA" if png else "RGB")
    img.thumbnail((MAX_LOGO_DIM, MAX_LOGO_DIM))
    while True:
        if png:
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            out = buf.getvalue()
        else:
            out = _jpeg(img, LOGO_TARGET_BYTES)
        if len(out) <= LOGO_TARGET_BYTES or max(img.size) <= MIN_LOGO_DIM:
            return out, img.width, img.height
        img = img.resize(
            (max(1, round(img.width * LOGO_SCALE)), max(1, round(img.height * LOGO_SCALE))), Image.Resampling.LANCZOS
        )


def _stored_type(data: bytes) -> str:
    """The type `_process` made of a photo, read from its own bytes: a PNG only when it kept a logo's transparency."""
    return "image/png" if data.startswith(_MAGIC_PNG) else "image/jpeg"


def _process(raw: bytes, *, logo: bool = False) -> tuple[bytes, int, int]:
    """One uploaded photo -> (JPEG bytes, width, height), stored shape (H37): at most MAX_DIM on the long
    side, at most TARGET_BYTES when the quality ladder gets there (82, then 70, then 60 -- whichever first
    fits, else 60's own size regardless), no EXIF. `_BadPhoto` (415) for anything that is not a readable
    photo: a format `_sniff` does not recognise, a file Pillow cannot decode (corrupt, truncated --
    `UnidentifiedImageError` is a subclass of `OSError`, so catching `OSError` covers both), or one whose
    pixel count is over the decompression-bomb ceiling.

    H38 (review round 1): the pixel count is checked right after `Image.open()` -- which parses only the
    header, nothing of the pixel data -- and strictly before `.load()`/`exif_transpose` ever decode a single
    one; Pillow's own `MAX_IMAGE_PIXELS` (set at import time, above) is kept as a second line, not the first,
    because it only *raises* above 2x that ceiling and merely *warns* between 1x and 2x, which would have let
    a handful of ~80 MP files (each a fraction of a megabyte on the wire, trivially within MAX_PART_BYTES)
    decode in full before this function ever got a say.

    `logo` (A3p-6 batch 2 Task 3): the photo is an industry site's logo, which keeps a transparency it has as a PNG
    and is held to MAX_LOGO_DIM and LOGO_TARGET_BYTES (`_logo`); the bytes' own signature says which it came out as
    (`_stored_type`). Every other photo is as above."""
    kind = _sniff(raw[:12])
    if kind is None:
        raise _BadPhoto(415, BAD_FORMAT)
    try:
        img = Image.open(io.BytesIO(raw))
    except (OSError, Image.DecompressionBombError) as e:
        raise _BadPhoto(415, BAD_IMAGE) from e
    if img.width * img.height > Image.MAX_IMAGE_PIXELS:
        raise _BadPhoto(415, BAD_IMAGE)
    try:
        img.load()
    except (OSError, Image.DecompressionBombError) as e:
        raise _BadPhoto(415, BAD_IMAGE) from e
    img = ImageOps.exif_transpose(img)  # H37: the pixels themselves, before anything else strips the tag
    if logo:
        return _logo(img)
    img = img.convert("RGB")
    img.thumbnail((MAX_DIM, MAX_DIM))
    return _jpeg(img, TARGET_BYTES), img.width, img.height


# ---------------------------------------------------------------- the upload token (create_page mints it)


def new_token(conn: psycopg.Connection, page_id: str) -> str:
    """create_page's own upload link (H35): a token good for hosted.PREVIEW_TTL, the same 24 hours a preview
    is -- the only thing that ever inserts a row here; update_page and revise_page only ever read the newest
    one still valid back (`latest_upload_url`), never mint a fresh one themselves."""
    token = db.new_id("up")
    conn.execute(
        "INSERT INTO upload_token (token, page_id, expires_at) VALUES (%s, %s, now() + %s)",
        (token, page_id, hosted.PREVIEW_TTL),
    )
    return token


def latest_upload_url(conn: psycopg.Connection, page_id: str, web_base_url: str) -> str | None:
    """The owner's still-valid upload link for `page_id`, if create_page ever minted one: the newest one that
    has not expired, else None (H13: "while it is valid" -- update_page/revise_page show exactly this, never
    minting a replacement)."""
    row = conn.execute(
        "SELECT token FROM upload_token WHERE page_id = %s AND expires_at > now() ORDER BY created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    return f"{web_base_url}/u/{row['token']}" if row else None


def _token_row(conn: psycopg.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT page_id FROM upload_token WHERE token = %s AND expires_at > now()", (token,)).fetchone()
    return dict(row) if row else None


def _token_valid(token: str) -> bool:
    """Whether `token` is a link still good now (`_token_row`), on a connection of its own: `upload_post` asks before it
    reads a byte of the body (A3p-6 batch 2 final review I3)."""
    with db.connect(config.load().database_url) as conn:
        return _token_row(conn, token) is not None


# ---------------------------------------------------------------- re-rendering a change (shared by upload/remove)


def _save_photos(
    conn: psycopg.Connection, page_id: str, biz: str, hostname: str | None, sections: dict, photos: list[dict]
) -> str:
    """Every change here makes, whatever it was (an upload or a removal): re-render the page from its current
    `sections` with `photos` as the only thing that moved, under the exact path update_page/revise_page use
    (`page_builder.finish` then `page_builder.save`, reused rather than reimplemented) -- so `sections.version`
    advances the same optimistic-concurrency way any other edit does, and a fresh preview always exists.
    Returns the new preview's token. Called only from inside the caller's own still-open `with
    conn.transaction()` block (`_do_remove`'s, or `_store_batch`'s) -- the row is already locked there
    either way, so `page_builder.save`'s own SELECT ... FOR UPDATE just reconfirms a lock this session
    already holds."""
    settings = config.load()
    kit = landing.kit(conn, biz)
    copy = PageCopy.model_validate(sections["copy"])
    hours = sections.get("hours")
    html, problems = page_builder.finish(
        settings, kit, sections["tree"], copy, photos=photos, hostname=hostname, hours=hours
    )
    if problems:  # never expected: Admiry's own template + copy always sanitises clean (Task 5); a hard stop
        # if it ever does not, rather than silently serving a page that failed its own rules.
        raise RuntimeError("uploads: the page did not pass its own rules after a photo change: " + " ".join(problems))
    token, _saved = page_builder.save(
        conn,
        page_id,
        tree=sections["tree"],
        copy=copy,
        photos=photos,
        hours=hours,
        html=html,
        title=page_builder.title_for(copy, kit),
        expect_version=sections.get("version"),
    )
    return token


def _kinds_of(page: dict | None) -> dict[str, dict] | None:
    """The kinds an industry site's photos may be, by id, in the checklist's order: `photos.KINDS` of the family the
    page row names (`photos.family_of`). None when nothing says which family it is."""
    family = site_photos.family_of(page)
    return {kind["id"]: kind for kind in site_photos.KINDS[family]} if family else None


def _room(conn: psycopg.Connection, page_id: str, kind: dict) -> int:
    """How many more photos of `kind` (an entry of `photos.KINDS`) the page may take: what the site uses less what it
    has -- the logo, which a new photo replaces, as many as the site uses whatever it has."""
    if kind["id"] == LOGO:
        return kind["need"]
    have = conn.execute(
        "SELECT count(*) AS n FROM page_photo WHERE page_id = %s AND kind = %s", (page_id, kind["id"])
    ).fetchone()["n"]
    return kind["need"] - have


def _token_and_count(token: str, kind: str, batch: int) -> tuple[str, str | None, str | None] | Response:
    """H39 (review round 2), step 1: the token's own validity and an early, optimistic judgement of a batch of `batch`
    photos said to show `kind` -- read-only, no lock, so a batch that is obviously over its cap is refused before a
    single file is even read. A starter page's: the five-photo cap against the photos on its `sections`. An industry
    site's (A3p-6 batch 2 Task 3): `kind` must be one of its family's, and the batch must fit in what that kind has room
    for (`_room`). This is not the authoritative check: `_store_batch`'s and `_store_site_batch`'s own, taken under the
    page row's lock once every file has already been decoded, are (a concurrent upload or removal landing in between
    is still caught there). Returns (the page's id, the kind of a site's batch -- None for a starter page's --, the
    sentence that refuses the batch -- None when it may go on); a `Response` (404) only when the token itself does not
    resolve, or the page is a site of no known family: nothing says what it asks for."""
    with db.connect(config.load().database_url) as conn:
        row = _token_row(conn, token)
        if row is None:
            return _not_found()
        page_id = row["page_id"]
        page = conn.execute("SELECT sections, site_draft, site_spec_id FROM page WHERE id = %s", (page_id,)).fetchone()
        if not site_photos.is_site(page):
            existing = len(((page["sections"] or {}) if page else {}).get("photos") or [])
            return page_id, None, TOO_MANY if existing + batch > MAX_PHOTOS else None
        kinds = _kinds_of(page)
        if kinds is None:
            return _not_found()
        if kind not in kinds:
            return page_id, None, NEED_A_KIND
        full = batch > _room(conn, page_id, kinds[kind])
        return page_id, kind, KIND_FULL.format(label=kinds[kind]["label"]) if full else None


def _store_batch(page_id: str, processed: list[tuple[bytes, int, int]]) -> tuple[str | None, str | None]:
    """H39, step 3: the only part of a batch that ever locks the page row. Every photo in `processed` has
    already been read, sniffed, decoded, resized and re-encoded by the time this runs (step 2, in
    `upload_post`'s own loop) -- so this is a handful of INSERTs, the *authoritative* re-check of the
    five-photo cap against the count actually on the row right now, and the re-render/save, all synchronous
    inside one short `with db.connect(...) as conn, conn.transaction():` -- no `await` anywhere in it, so the
    row's lock is held only as long as these statements themselves take, never for as long as a client's own
    upload or Pillow's own decode did (H38's fix held it across exactly that, which is what this round
    corrects). Returns (a preview token, None) on success or (None, a refusal message) when the
    authoritative count check itself is what ends the batch -- never both. The `with` statement's own exit
    handles commit-or-rollback and close on every path, refusal or exception alike; there is no separate
    "abort" to remember to call."""
    with db.connect(config.load().database_url) as conn, conn.transaction():
        row = conn.execute(
            "SELECT business_id, sections, hostname FROM page WHERE id = %s FOR UPDATE", (page_id,)
        ).fetchone()
        sections = row["sections"] or {}
        existing = list(sections.get("photos") or [])
        if len(existing) + len(processed) > MAX_PHOTOS:
            return None, TOO_MANY
        new_specs = []
        for data, width, height in processed:
            photo_id = db.new_id("ph")
            conn.execute(
                "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height) "
                "VALUES (%s, %s, %s, 'image/jpeg', %s, %s)",
                (photo_id, page_id, data, width, height),
            )
            new_specs.append({"id": photo_id, "width": width, "height": height})
        preview_token = _save_photos(conn, page_id, row["business_id"], row["hostname"], sections, existing + new_specs)
        return preview_token, None


def _store_site_batch(page_id: str, kind: str, processed: list[tuple[bytes, int, int]]) -> str | None:
    """H39, step 3 for an industry site's page: `_store_batch`'s own short transaction -- the page row locked, the cap
    checked for real against what is on the page right now, a handful of INSERTs -- but for a kind. The batch must fit
    in what `kind` has room for (`_room`), and a logo's new photo takes the place of the one before: that one is
    deleted in this same transaction, so the page never has two logos nor none while the upload runs. Nothing is
    re-rendered here: the site's preview is built again after this has committed (`_site_saved`). Each photo is stored
    with its kind, the type `_process` made of it, and the moment its own statement ran (`clock_timestamp()`: `now()`
    is one value for a whole transaction, and the order of a batch is the order the owner sent it in, which the
    gallery's slots follow). None on success, else the sentence that refuses the batch."""
    with db.connect(config.load().database_url) as conn, conn.transaction():
        page = conn.execute(
            "SELECT sections, site_draft, site_spec_id FROM page WHERE id = %s FOR UPDATE", (page_id,)
        ).fetchone()
        kinds = _kinds_of(page)
        if kinds is None:
            return BAD_REQUEST
        if kind not in kinds:
            return NEED_A_KIND
        if len(processed) > _room(conn, page_id, kinds[kind]):
            return KIND_FULL.format(label=kinds[kind]["label"])
        if kind == LOGO:
            conn.execute("DELETE FROM page_photo WHERE page_id = %s AND kind = %s", (page_id, LOGO))
        for data, width, height in processed:
            conn.execute(
                "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height, kind, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())",
                (db.new_id("ph"), page_id, data, _stored_type(data), width, height, kind),
            )
    return None


def _do_remove(conn: psycopg.Connection, page_id: str, photo_id: str) -> tuple[bool, str | None]:
    """One photo gone -- and, for a starter page, the page re-rendered without it, the same transaction, so a delete and
    a stale re-render can never land apart. Returns (whether a photo was removed, the starter page's fresh preview token
    -- None for an industry site's page, whose preview is built again after this has committed: `_site_saved`). A
    `photo_id` not currently on the page (already removed, a stale thumbnail form double-submitted) is a quiet no-op:
    nothing removed, nothing to re-render. A site's photos are those that say what they show: one that says nothing --
    a starter page's, from before the business had a site -- is not this link's to remove."""
    with conn.transaction():
        row = conn.execute(
            "SELECT business_id, sections, hostname, site_draft, site_spec_id FROM page WHERE id = %s FOR UPDATE",
            (page_id,),
        ).fetchone()
        if site_photos.is_site(row):
            gone = conn.execute(
                "DELETE FROM page_photo WHERE id = %s AND page_id = %s AND kind IS NOT NULL", (photo_id, page_id)
            ).rowcount
            return gone > 0, None
        sections = row["sections"] or {}
        existing = list(sections.get("photos") or [])
        remaining = [p for p in existing if p["id"] != photo_id]
        if len(remaining) == len(existing):
            return False, None
        conn.execute("DELETE FROM page_photo WHERE id = %s AND page_id = %s", (photo_id, page_id))
        return True, _save_photos(conn, page_id, row["business_id"], row["hostname"], sections, remaining)


# ---------------------------------------------------------------- rendering the upload page itself


def _not_found() -> Response:
    """An expired or unknown token (H13): a plain page, no business data -- there is no row to name anything
    from, the same principle `hosted._not_found`/`_preview_not_found` use for their own guessable tokens."""
    return PlainTextResponse(
        "Not found.", status_code=404, headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-store"}
    )


def _latest_preview_token(conn: psycopg.Connection, page_id: str) -> str | None:
    """The page's own newest still-valid preview (H37): create_page always makes one before it ever mints an
    upload token, and every upload or removal makes a fresh one again right after -- so, for any upload token
    that is itself still valid, one of these always exists too. `None` only in a defensive, never-expected
    case (a page whose every preview has since expired without this one having); the caller shows no
    thumbnails rather than a broken image."""
    row = conn.execute(
        "SELECT token FROM page_preview WHERE page_id = %s AND kind = 'html' AND expires_at > now() "
        "ORDER BY created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    return row["token"] if row else None


def _thumbnails(agent_base: str, preview_token: str | None, stored: list[dict]) -> list[dict]:
    """H13: each stored photo shown through the page's own newest preview (`/p/{token}/photos/{id}`, the same
    route a preview's own `<img>` tags resolve to, H3) -- never a route of this upload token's own, so a
    photo is visible here through exactly the one path that already exists for it. By its full agent-host
    address (`agent_base`, H42): that route answers nowhere else, and this page is linked on the browser host."""
    if not preview_token:
        return []
    return [{"id": p["id"], "src": f"{agent_base}/p/{preview_token}/photos/{p['id']}"} for p in stored]


def _site_groups(conn: psycopg.Connection, token: str, page_id: str, family: str, draft: dict | None) -> list[dict]:
    """An industry site's photos by kind, in the checklist's order (Q7, `photos.checklist`): each kind with its label,
    how many photos it has of how many the site uses, its photos -- `{id, src}`, `src` the link's own thumbnail route
    (`upload_photo`), since a site's preview is a bundle and serves no photo route --, how many empty squares the page
    still offers for it (`left`: what the site uses less what it has; the logo one whatever it has, which a new photo
    replaces) and whether the site waits for it before going live (`blocks`: the logo or the key photo while missing,
    the logo covered too by the wordmark the owner accepted). A photo that says nothing of what it shows is none of
    these."""
    rows = conn.execute(
        "SELECT id, kind FROM page_photo WHERE page_id = %s AND kind IS NOT NULL ORDER BY created_at, id", (page_id,)
    ).fetchall()
    groups = []
    for item in site_photos.checklist(conn, page_id, family, draft):
        mine = [{"id": r["id"], "src": f"/u/{token}/photo/{r['id']}"} for r in rows if r["kind"] == item["id"]]
        groups.append(
            {
                "id": item["id"],
                "label": item["label"],
                "need": item["need"],
                "have": len(mine),
                "photos": mine,
                "left": 1 if item["id"] == LOGO else max(item["need"] - len(mine), 0),
                "blocks": item["blocks_publish"] and not item["done"],
            }
        )
    return groups


def _render(
    conn: psycopg.Connection,
    token: str,
    page_id: str,
    *,
    message: str | None = None,
    link: str | None = None,
    link_label: str | None = None,
    error: str | None = None,
    status: int = 200,
    preview_token: str | None = None,
) -> Response:
    page = conn.execute(
        "SELECT business_id, sections, site_draft, site_spec_id FROM page WHERE id = %s", (page_id,)
    ).fetchone()
    name = leads.business_name(conn, page["business_id"]) if page else "the business"
    agent_base = config.load().public_base_url
    if site_photos.is_site(page):  # an industry site's page: the kinds, not the starter's five, and the gate's sentence
        family = site_photos.family_of(page)
        if family is None:
            return _not_found()
        draft = page["site_draft"]
        shown = {
            "kinds": _site_groups(conn, token, page_id, family, draft),
            "gate": site_photos.key_photos(conn, page_id, family, draft)["sentence"],
        }
    else:
        stored = ((page["sections"] or {}).get("photos") or []) if page else []
        pv_token = preview_token or _latest_preview_token(conn, page_id)
        shown = {
            "kinds": None,
            "photos": _thumbnails(agent_base, pv_token, stored),
            "count": len(stored),
            "max_photos": MAX_PHOTOS,
        }
    out = _env.get_template("upload.html").render(
        name=name, token=token, message=message, link=link, link_label=link_label, error=error, **shown
    )
    return HTMLResponse(out, status_code=status, headers=_headers(agent_base))


def _with_preview_note(conn: psycopg.Connection, token: str, page_id: str, verb: str, preview_token: str) -> Response:
    """H37: after a successful change, the upload page shows its own updated thumbnails (through this same
    fresh preview, already in hand -- no second query for it) plus a button to its address -- no extra tool call
    needed to see the result."""
    preview_url = f"{config.load().public_base_url}/p/{preview_token}"
    return _render(
        conn, token, page_id, message=f"{verb}.", link=preview_url, link_label=SEE_PAGE, preview_token=preview_token
    )


# ---------------------------------------------------------------- the blocking work (one dispatch per request)


def _handle_error(token: str, message: str, status: int) -> Response:
    with db.connect(config.load().database_url) as conn:
        row = _token_row(conn, token)
        if row is None:
            return _not_found()
        return _render(conn, token, row["page_id"], error=message, status=status)


def _handle_remove(token: str, photo_id: str) -> Response:
    with db.connect(config.load().database_url) as conn:
        row = _token_row(conn, token)
        if row is None:
            return _not_found()
        removed, preview_token = _do_remove(conn, row["page_id"], photo_id)
        if not removed:
            return _render(conn, token, row["page_id"])
        if preview_token is not None:  # a starter page: its new preview is already made
            return _with_preview_note(conn, token, row["page_id"], "Photo removed", preview_token)
    return _site_saved(token, row["page_id"])  # a site's: built again now, with no connection of this request open


def _site_saved(token: str, page_id: str) -> Response:
    """After a change of an industry site's photos has committed (a batch stored, a photo removed), whatever the change
    was: the site's preview built again without the model and without the render check (`photos.rebuild`, on a
    connection of its own -- none of this request's is open while it builds), and the upload page again, saying what
    happened (Q7, Q12): saved, with a button to the site; that the site shows the photo the next time it is built, when
    the build failed (the photo is kept); or just that it is saved, when the site was never built (`photos.was_built`:
    the draft keeps no words yet, or no look of its family's) -- the photos join its first build."""
    with db.connect(config.load().database_url) as conn:
        built = site_photos.was_built(landing.get(conn, page_id))
    link = None
    if not built or (link := site_photos.rebuild(page_id)):
        message = SAVED
    else:
        message = SAVED_LATER
    with db.connect(config.load().database_url) as conn:
        return _render(conn, token, page_id, message=message, link=link, link_label=SEE_SITE)


def _render_success(token: str, page_id: str, verb: str, preview_token: str) -> Response:
    """`_store_batch`'s own connection is already committed and closed by the time there is anything to show
    for it -- this opens a fresh one, same as every other render."""
    with db.connect(config.load().database_url) as conn:
        return _with_preview_note(conn, token, page_id, verb, preview_token)


def _field(form: FormData, key: str) -> str:
    v = form.get(key)
    return v.strip() if isinstance(v, str) else ""


# ---------------------------------------------------------------- the routes


@router.get("/u/{token}")
def upload_get(token: str) -> Response:
    with db.connect(config.load().database_url) as conn:
        row = _token_row(conn, token)
        if row is None:
            return _not_found()
        return _render(conn, token, row["page_id"])


@router.get("/u/{token}/photo/{photo_id}")
def upload_photo(token: str, photo_id: str) -> Response:
    """One photo of the link's own page, as stored: the thumbnails of an industry site's upload page (Q7), which cannot
    come through a preview as a starter page's do. The token must be valid and the photo this page's own -- any other
    answers the same plain 404 an expired link does. A browser may keep it five minutes, a proxy never: the photo's id
    is in its address, so a photo taken away is simply not asked for again."""
    with db.connect(config.load().database_url) as conn:
        row = _token_row(conn, token)
        if row is None:
            return _not_found()
        photo = conn.execute(
            "SELECT bytes, content_type FROM page_photo WHERE id = %s AND page_id = %s", (photo_id, row["page_id"])
        ).fetchone()
    if photo is None:
        return _not_found()
    return Response(
        bytes(photo["bytes"]),
        media_type=photo["content_type"],
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Robots-Tag": "noindex",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/u/{token}")
async def upload_post(token: str, request: Request) -> Response:
    """Multipart photos (`photos`, up to MAX_PHOTOS, or an industry site's kind's own cap, said to show `kind`), or a
    removal (`action=remove&photo=<id>`).

    The body is bounded twice over (H38, review round 1). First, by its own declared size: no Content-Length
    at all is refused outright (411 -- every browser sends one for a file input; there is no legitimate
    chunked-without-a-length case this endpoint needs to support, and trusting one would mean trusting an
    unbounded stream) and a Content-Length over budget is refused before anything is read. Second, and the
    only check that cannot simply be lied to, by what actually arrives: `_counted_receive` wraps this
    request's own ASGI `receive` so `request.form()` -- which reads every byte through `Request.stream()`,
    multipart or urlencoded alike -- raises `_TooLarge` the instant the running total passes MAX_BODY_BYTES,
    whatever the header said.

    `max_part_size` in FORM_LIMITS (Starlette 1.7's own `request.form()` limit, track.FORM_LIMITS's shape)
    only ever bounds a plain FIELD's value (`action`, `photo`) -- Starlette's `MultiPartParser.on_part_data`
    checks it only `if self._current_part.file is None`, which is never true for a part that carries a
    filename (a photo): that data is spooled straight to a temp file with no size check of its own. So a
    single part that is genuinely too big is instead caught by hand, per file, once it has been read (itself
    now bounded by the two checks above) -- one 413, same sentence as the whole-request guard, since both say
    the same thing to the owner.

    Every photo in a `photos` batch is read, decoded, resized and re-encoded in turn (H39, review round 2) --
    never more than one file's bytes and one decoded image alive at once -- entirely before any database
    connection is opened for the batch at all: no lock is ever held across a client's own upload pace or
    Pillow's own decode time (H38's fix held the page row's lock across exactly that; this round moves the
    lock to cover only the short, synchronous insert-and-save step that actually needs it, `_store_batch`).

    An industry site's page (A3p-6 batch 2 Task 3) is judged by its own rules, in the same three steps: step 1 also
    reads the `kind` field, refusing a batch with none or one the family has not, or more photos than the kind has room
    for; step 2 treats a logo (`kind=logo`) as `_process(raw, logo=True)` does; step 3 is `_store_site_batch`, which
    stores and re-renders nothing -- the site's preview is built again after it, in `_site_saved`, with the page row not
    held and no connection of this request open.

    The link itself is looked up before any of the body is read (A3p-6 batch 2 final review I3): one that is none, or
    has ended, answers the plain 404 with the form never parsed -- only a link the owner was given makes this read and
    spool up to MAX_BODY_BYTES."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        return await run_in_threadpool(_handle_error, token, NO_CONTENT_LENGTH, 411)
    try:
        if int(content_length) > MAX_BODY_BYTES:
            return await run_in_threadpool(_handle_error, token, TOO_MUCH, 413)
    except ValueError:
        return await run_in_threadpool(_handle_error, token, BAD_REQUEST, 400)
    if not await run_in_threadpool(_token_valid, token):
        return _not_found()

    request._receive = _counted_receive(request._receive, MAX_BODY_BYTES)
    try:
        form = await request.form(**FORM_LIMITS)
    except _TooLarge:
        return await run_in_threadpool(_handle_error, token, TOO_MUCH, 413)
    except HTTPException:
        return await run_in_threadpool(_handle_error, token, BAD_REQUEST, 400)

    if _field(form, "action") == "remove":
        return await run_in_threadpool(_handle_remove, token, _field(form, "photo"))

    files = [f for f in form.getlist("photos") if isinstance(f, UploadFile) and f.filename]
    if not files:
        return await run_in_threadpool(_handle_error, token, NEED_A_FILE, 400)

    # H39 step 1: the token and an early, optimistic judgement of the batch -- read-only, no lock (see
    # _token_and_count).
    opened = await run_in_threadpool(_token_and_count, token, _field(form, "kind"), len(files))
    if isinstance(opened, Response):
        return opened
    page_id, kind, refusal = opened
    if refusal:
        return await run_in_threadpool(_handle_error, token, refusal, 400)

    # H39 step 2: every file read, sniffed, decoded, resized and re-encoded before anything is written, and
    # with no database connection open at all while this -- the slow, client- and Pillow-paced part -- runs.
    own = {"logo": True} if kind == LOGO else {}  # the one kind `_process` treats as its own
    processed: list[tuple[bytes, int, int]] = []
    for f in files:
        raw = await f.read()
        if len(raw) > MAX_PART_BYTES:
            return await run_in_threadpool(_handle_error, token, TOO_MUCH, 413)
        try:
            processed.append(await run_in_threadpool(_process, raw, **own))
        except _BadPhoto as e:
            return await run_in_threadpool(_handle_error, token, e.message, e.status)
        del raw  # dropped before the next file is even read

    # H39 step 3: the only part of a batch that ever locks the page row -- see _store_batch.
    if kind is not None:
        error = await run_in_threadpool(_store_site_batch, page_id, kind, processed)
        if error:
            return await run_in_threadpool(_handle_error, token, error, 400)
        return await run_in_threadpool(_site_saved, token, page_id)
    preview_token, error = await run_in_threadpool(_store_batch, page_id, processed)
    if error:
        return await run_in_threadpool(_handle_error, token, error, 400)
    return await run_in_threadpool(_render_success, token, page_id, "Photos added", preview_token)
