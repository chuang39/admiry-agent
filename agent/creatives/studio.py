"""The creatives studio (spec 2026-11-04 §4; rulings R2, R3, R15): one set of verified ad creatives per run, made from
the business's own page photos and its own facts, kept in ad_creative_set / ad_creative_image.

`start` makes a `making` set and runs `run` on a daemon thread with its own connection (which waits for the set to
commit with the caller's transaction), unless one is making already; a business with no photos gets its valid upload
link instead (read back, never minted) and nothing runs. `run`:
  1. Spark reads each photo (subject noun phrase, suitability, privacy flags); a photo too small for both of Google's
     sizes is never sent, an unsuitable or private one is skipped, its reason kept for the card;
  2. SAM cuts the work out of each photo;
  3. per photo and format: when the work fits the plain cover crop, that crop (no generation); else Muse Image extends
     the padded photo, the whole original is pasted back (images.restore_photo), and the result must pass SAM's
     re-segmentation (IoU >= 0.9 against the original's mask where it was placed) and Wonder's image policy check --
     any failure falls back to the plain crop, recorded in `checks`;
  4. the text is written and checked meanwhile (copy.write_and_check, reading the rules and the vertical's guide), on
     its own thread.
A run that could use no photo at all, or kept too few lines for Google, ends `failed` with the reasons.
Image calls run on a pool of WORKERS; the whole run gives up after GIVE_UP_S, the set `failed` with a sentence the
owner can read. Every model call is a step `{"step", "model", "ok", "ms"}`, written as it happens."""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from fastmcp.exceptions import ToolError
from PIL import Image
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import config, db, landing, trades, uploads
from . import copy, images, prompts
from . import meta as metamod
from .meta import IMAGE, SAM, SPARK, WONDER, MetaClient, MetaError, Segment

log = logging.getLogger(__name__)

GIVE_UP_S = 240.0  # the whole run (R: 4 minutes)
STALE_S = 600  # a `making` set this old is a run that died with its process: failed, so a new one can start
ROW_WAIT_S = 30.0  # how long a run waits to see its set: start's INSERT commits with the caller's transaction
ROW_POLL_S = 0.1
WORKERS = 4
MAX_PHOTOS = 5
IOU_MIN = 0.9
FORMATS = ("landscape", "square")
RULES_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "home_services"
TRADE_FILES = {"roofing": "roofing.md", "remodel": "remodel.md", "landscaping": "landscaping.md"}
GUIDE_FILES = ("google-search.md", "offers.md")  # the writer's knowledge besides the rules (spec §4 step 4)
# the kinds of a site's photos that show the work (sites/photos.py KINDS), chosen first in this order
PREFERRED_KINDS = ("work", "before_after", "key")

TOO_LONG = "Making your ads took longer than 4 minutes, so it was stopped. Please try again."
NO_MODELS = "The models that make your ads are not available right now. Please try again later."
NO_TEXT = "Your ad text could not be written this time. Please try again."
TOO_FEW = (
    "Too few ad lines passed the checks ({headlines} headlines of the 3 Google needs, {descriptions} descriptions of "
    "the 2 it needs). Tell Admiry more about your business and try again."
)
BROKE = "Something went wrong while making your ads. Please try again."
NO_PHOTOS_USED = "None of your photos could be used for ads. {reasons} Add other photos of your work and try again."
NO_PHOTOS_AT_ALL = "There are no photos of your work to make ads from. Add some and try again."
NEEDS_PHOTOS = (
    "Ads are made from photos of the business's own work, and there are none yet: send the owner the upload link "
    "(upload_url) and start again once the photos are in."
)
NO_UPLOAD_LINK = (
    "Ads are made from photos of the business's own work, and there are none yet. There is no valid upload link right "
    'now: call update_landing_page with action "build_site" (a home-services business or a dental practice; for any '
    'other business, action "write") -- its answer carries a fresh upload_url to send the owner. Start again once the '
    "photos are in."
)
DIED = "Making your ads stopped before it finished. Please try again."
# start's daily ceiling (Settings.creatives_per_day, _draft for a draft key): each new set is a paid run of the models
DAILY_LIMIT = (
    "Admiry makes new ads for a business at most {cap} times a day, and today's are used up. Your latest ads are still "
    "here; ask again tomorrow for new ones."
)
TOO_SMALL = "This photo is too small for an ad; a larger one works."
UNREADABLE = "This photo could not be opened."
NOT_READ = "This photo could not be looked at this time."
NOT_SUITABLE = "This photo does not show your work clearly enough for an ad."
PRIVATE = "This photo shows {flags}, which an ad must not show."
NOT_YOURS = "Those ads are not this business's."
NOT_READY = "Those ads are not ready to use yet."
NO_BUSINESS = "That business does not exist."


class ImageCheck(BaseModel):
    """Wonder's verdict on one output image (spec §4 step 5c, R15 c)."""

    ok: bool
    problems: list[str] = Field(default_factory=list)


class _GiveUp(Exception):
    """The run is past GIVE_UP_S."""


class _Steps(list):
    """The run's steps: each one appended is also appended to the set's row at once, so the card shows progress. Worker
    threads append too; psycopg's connection is thread-safe. A write after the run gave up (or after its connection
    closed) is dropped: the set is no longer `making`."""

    def __init__(self, conn: psycopg.Connection, set_id: str) -> None:
        super().__init__()
        self.conn, self.set_id = conn, set_id
        self.lock = threading.Lock()

    def append(self, step: dict) -> None:
        with self.lock:
            super().append(step)
        try:
            self.conn.execute(
                "UPDATE ad_creative_set SET steps = steps || %s WHERE id = %s AND status = 'making'",
                (Jsonb([step]), self.set_id),
            )
        except psycopg.Error:  # the run is over (given up, connection closed): the step is of no use any more
            log.debug("creatives: step after the run ended dropped set=%s", self.set_id)


def _step(steps: list, name: str, model: str, ok: bool, started: float) -> None:
    steps.append({"step": name, "model": model, "ok": ok, "ms": int((time.monotonic() - started) * 1000)})


@dataclass
class _Photo:
    index: int  # 1-based, the page's order: "Photo 2" on the card
    id: str
    raw: bytes
    content_type: str
    image: Image.Image
    formats: list[str]
    subject: str = ""
    png: bytes = b""


@dataclass
class _Made:
    photo: _Photo
    format: str
    method: str
    image: Image.Image
    checks: dict = field(default_factory=dict)


# ---------------------------------------------------------------- what a run reads


def _knowledge(conn: psycopg.Connection, biz: str) -> dict:
    """The business's stored knowledge, through the launch code's own loader (imported here: ads_run will read the
    accepted set back from this module)."""
    from .. import ads_run

    return ads_run._knowledge(conn, biz)


def _photo_rows(conn: psycopg.Connection, biz: str) -> list[dict]:
    """The current page's photos, at most MAX_PHOTOS: the kinds that show the work first (PREFERRED_KINDS, in that
    order), then the rest, each in the owner's order; a logo is no ad photo."""
    page = landing.current(conn, biz)
    if page is None:
        return []
    return conn.execute(
        "SELECT id, bytes, content_type, width, height FROM page_photo "
        "WHERE page_id = %s AND kind IS DISTINCT FROM 'logo' "
        "ORDER BY coalesce(array_position(%s::text[], kind), %s), created_at, id LIMIT %s",
        (page["id"], list(PREFERRED_KINDS), len(PREFERRED_KINDS) + 1, MAX_PHOTOS),
    ).fetchall()


def _has_photos(conn: psycopg.Connection, biz: str) -> bool:
    page = landing.current(conn, biz)
    if page is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM page_photo WHERE page_id = %s AND kind IS DISTINCT FROM 'logo' LIMIT 1", (page["id"],)
    ).fetchone()
    return row is not None


def _trade(knowledge: dict) -> str | None:
    if knowledge.get("trade") in trades.NAMES:
        return knowledge["trade"]
    services = [s.get("name") for s in knowledge.get("services") or [] if isinstance(s, dict)]
    words = [knowledge.get("category_detail"), knowledge.get("category"), knowledge.get("name"), *services]
    return trades.trade_in(" ".join(w for w in words if isinstance(w, str)))


def _body(path: Path) -> str:
    """A knowledge file without its front matter."""
    text = path.read_text()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip()
    return text


def _guide() -> str:
    """The writer's and the repairer's knowledge besides the rules: how Google search ads and offers are written for
    home services (spec §4 step 4)."""
    return "\n\n".join(f"# home_services/{name[:-3]}\n{_body(RULES_DIR / name)}" for name in GUIDE_FILES)


def _rules(trade: str | None) -> str:
    """The checker's rules: home services' compliance file, plus the trade's file when there is one (R15)."""
    parts = [f"# home_services/compliance\n{_body(RULES_DIR / 'compliance.md')}"]
    name = TRADE_FILES.get(trade or "")
    if name:
        parts.append(f"# home_services/{name[:-3]}\n{_body(RULES_DIR / name)}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- the image steps


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _read(meta: MetaClient, photo: _Photo, business: dict, steps: list) -> copy.PhotoRead | None:
    """Spark's reading of one photo (spec §4 step 1); None when it gave none (the photo is skipped)."""
    parts = [
        {"type": "text", "text": json.dumps({"photo_id": photo.id, **business})},
        {"type": "image_url", "image_url": {"url": metamod.data_uri(photo.raw, photo.content_type)}},
    ]
    started = time.monotonic()
    try:
        read = meta.json(SPARK, prompts.READ_PHOTO, parts, copy.PhotoRead)
    except MetaError:
        _step(steps, f"read_photo {photo.index}", SPARK, False, started)
        return None
    _step(steps, f"read_photo {photo.index}", SPARK, True, started)
    return read


def _best(segments: list[Segment]) -> Segment | None:
    if not segments:
        return None
    return max(segments, key=lambda s: (s.confidence or 0.0, (s.box[2] - s.box[0]) * (s.box[3] - s.box[1])))


def _cut(meta: MetaClient, photo: _Photo, steps: list) -> Image.Image | None:
    """SAM's mask of the work in `photo` (spec §4 step 2), photo-sized; None when SAM found nothing or failed."""
    started = time.monotonic()
    try:
        segment = _best(meta.segment(photo.png, photo.subject))
        mask = images.mask_of(segment, photo.image) if segment else None
    except (MetaError, ValueError):  # ValueError: a raster that does not fit its stated size
        _step(steps, f"cut_out {photo.index}", SAM, False, started)
        return None
    _step(steps, f"cut_out {photo.index}", SAM, mask is not None, started)
    if mask is None or mask.getbbox() is None:
        return None
    return mask


def _plain(photo: _Photo, fmt: str, box: tuple | None, checks: dict) -> _Made:
    if box is not None:
        checks["box_out"] = list(images.crop_box(box, photo.image.size, fmt))
    return _Made(photo, fmt, "plain_crop", images.plain_crop(photo.image, fmt, box), checks)


def _resegment(meta: MetaClient, out: Image.Image, subject: str, steps: list, name: str) -> Image.Image | None:
    started = time.monotonic()
    try:
        segment = _best(meta.segment(_png(out), subject))
        mask = images.mask_of(segment, out) if segment else None
    except (MetaError, ValueError):
        _step(steps, name, SAM, False, started)
        return None
    _step(steps, name, SAM, True, started)
    return mask


def _policy(meta: MetaClient, out: Image.Image, subject: str, fmt: str, steps: list, name: str) -> ImageCheck | None:
    """Wonder's verdict on `out`; None when it gave none (the image is then not checked, so not used)."""
    parts = [
        {"type": "text", "text": json.dumps({"subject": subject, "format": fmt})},
        {"type": "image_url", "image_url": {"url": metamod.data_uri(_jpeg(out), "image/jpeg")}},
    ]
    started = time.monotonic()
    try:
        verdict = meta.json(WONDER, prompts.CHECK_IMAGE.replace("{subject}", subject), parts, ImageCheck)
    except MetaError:
        _step(steps, name, WONDER, False, started)
        return None
    _step(steps, name, WONDER, True, started)
    return verdict


def _one(meta: MetaClient, photo: _Photo, mask: Image.Image | None, fmt: str, steps: list) -> _Made:
    """One output image (spec §4 steps 3 and 5c, R15)."""
    label = f"{photo.index} {fmt}"
    checks: dict[str, Any] = {"photo": photo.index, "subject": photo.subject}
    if mask is None:
        checks["fallback"] = "The work could not be found in the photo, so the photo is cropped as it is."
        return _plain(photo, fmt, None, checks)
    box = mask.getbbox()
    checks["box"] = list(box)
    if images.fits_crop(box, photo.image.size, fmt):
        checks["fits_crop"] = True
        return _plain(photo, fmt, box, checks)
    checks["fits_crop"] = False
    canvas, place = images.pad(photo.image, fmt)
    started = time.monotonic()
    try:
        generated = images.load(meta.edit_image(_png(canvas), prompts.EXTEND, images.MODEL_SIZE[fmt]))
    except (MetaError, OSError) as e:  # OSError: an answer Pillow cannot open
        _step(steps, f"extend {label}", IMAGE, False, started)
        checks["fallback"] = f"The photo could not be extended ({e}), so it is cropped."
        return _plain(photo, fmt, box, checks)
    _step(steps, f"extend {label}", IMAGE, True, started)
    out = images.restore_photo(generated, photo.image, place, fmt)
    pw, ph = place[2] - place[0], place[3] - place[1]
    expected = Image.new("L", images.SIZES[fmt], 0)
    expected.paste(mask.resize((pw, ph), Image.NEAREST), place[:2])
    kx, ky = pw / photo.image.width, ph / photo.image.height
    checks["box_out"] = [
        place[0] + round(box[0] * kx),
        place[1] + round(box[1] * ky),
        place[0] + round(box[2] * kx),
        place[1] + round(box[3] * ky),
    ]
    found = _resegment(meta, out, photo.subject, steps, f"recheck {label}")
    score = images.iou(expected, found) if found is not None else 0.0
    checks["iou"] = round(score, 3)
    if score < IOU_MIN:
        checks["fallback"] = "The work did not stay where it was in the photo, so the photo is cropped instead."
        return _plain(photo, fmt, box, {k: v for k, v in checks.items() if k != "box_out"})
    verdict = _policy(meta, out, photo.subject, fmt, steps, f"check_image {label}")
    checks["policy"] = verdict.model_dump() if verdict is not None else None
    if verdict is None:
        checks["fallback"] = "The extended image could not be checked, so the photo is cropped instead."
        return _plain(photo, fmt, box, {k: v for k, v in checks.items() if k != "box_out"})
    if not verdict.ok or verdict.problems:  # a problem named is a failure, whatever `ok` says
        checks["fallback"] = "The extended image did not pass Google's image rules, so the photo is cropped instead."
        return _plain(photo, fmt, box, {k: v for k, v in checks.items() if k != "box_out"})
    return _Made(photo, fmt, "extended", out, checks)


# ---------------------------------------------------------------- the run


def _wait(future: Future, deadline: float) -> Any:
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout as e:
        raise _GiveUp from e


def _load(rows: list[dict], skipped: list[dict]) -> list[_Photo]:
    photos = []
    for index, row in enumerate(rows, start=1):
        raw = bytes(row["bytes"])
        try:
            img = images.load(raw)
        except OSError:
            skipped.append({"photo_id": row["id"], "index": index, "why": UNREADABLE})
            continue
        formats = [f for f in FORMATS if not images.too_small(img, f)]
        if not formats:
            skipped.append({"photo_id": row["id"], "index": index, "why": TOO_SMALL})
            continue
        photos.append(_Photo(index, row["id"], raw, row["content_type"], img, formats))
    return photos


def _make(conn: psycopg.Connection, meta: MetaClient, biz: str, steps: list, deadline: float) -> dict:
    knowledge = _knowledge(conn, biz)
    facts = copy.facts_of(knowledge)
    trade = _trade(knowledge)
    trade_words = trade or knowledge.get("category_detail") or "local service business"
    skipped: list[dict] = []
    photos = _load(_photo_rows(conn, biz), skipped)
    about = {"business": knowledge.get("name"), "trade": trade_words}
    text_pool = ThreadPoolExecutor(1, thread_name_prefix="creatives-text")
    image_pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="creatives-image")
    try:
        text = text_pool.submit(
            copy.write_and_check, meta, facts, _rules(trade), trade_words, guide=_guide(), steps=steps
        )
        reads = [image_pool.submit(_read, meta, p, about, steps) for p in photos]
        cuts: list[tuple[_Photo, Future]] = []
        for photo, future in zip(photos, reads, strict=True):
            read = _wait(future, deadline)
            if read is None:
                skipped.append({"photo_id": photo.id, "index": photo.index, "why": NOT_READ})
            elif read.privacy:
                why = PRIVATE.format(flags=", ".join(read.privacy))
                skipped.append({"photo_id": photo.id, "index": photo.index, "why": why})
            elif not read.suitable:
                skipped.append({"photo_id": photo.id, "index": photo.index, "why": read.reason or NOT_SUITABLE})
            else:
                photo.subject, photo.png = read.subject, _png(photo.image)
                cuts.append((photo, image_pool.submit(_cut, meta, photo, steps)))
        jobs = []
        for photo, future in cuts:
            mask = _wait(future, deadline)
            jobs.extend(image_pool.submit(_one, meta, photo, mask, fmt, steps) for fmt in photo.formats)
        made = [_wait(job, deadline) for job in jobs]
        ledger = _wait(text, deadline)
    finally:
        text_pool.shutdown(wait=False, cancel_futures=True)
        image_pool.shutdown(wait=False, cancel_futures=True)
    skipped.sort(key=lambda s: s["index"])
    return {"ledger": ledger, "facts": facts, "skipped": skipped, "made": made}


def _fail(conn: psycopg.Connection, set_id: str, error: str) -> None:
    conn.execute(
        "UPDATE ad_creative_set SET status = 'failed', error = %s WHERE id = %s AND status = 'making'", (error, set_id)
    )


def _finish(conn: psycopg.Connection, set_id: str, result: dict) -> None:
    ledger: copy.Ledger = result["ledger"]
    kept = ledger.kept
    stored = {**ledger.model_dump(), "facts": result["facts"], "skipped": result["skipped"]}
    errors = []
    if not result["made"]:  # every photo skipped: the reasons, not a ready set with no images
        reasons = " ".join(f"Photo {s['index']}: {s['why']}" for s in result["skipped"])
        errors.append(NO_PHOTOS_USED.format(reasons=reasons) if reasons else NO_PHOTOS_AT_ALL)
    if len(kept.headlines) < copy.LIMITS["headlines"][1] or len(kept.descriptions) < copy.LIMITS["descriptions"][1]:
        errors.append(TOO_FEW.format(headlines=len(kept.headlines), descriptions=len(kept.descriptions)))
    error = " ".join(errors) or None
    with conn.transaction():
        row = conn.execute("SELECT status FROM ad_creative_set WHERE id = %s FOR UPDATE", (set_id,)).fetchone()
        if row is None or row["status"] != "making":
            return
        for made in result["made"]:
            data, content_type = images.encode(made.image)
            conn.execute(
                "INSERT INTO ad_creative_image (id, set_id, photo_id, format, method, bytes, content_type, width, "
                "height, checks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    db.new_id("ci"),
                    set_id,
                    made.photo.id,
                    made.format,
                    made.method,
                    data,
                    content_type,
                    made.image.width,
                    made.image.height,
                    Jsonb(made.checks),
                ),
            )
        conn.execute(
            "UPDATE ad_creative_set SET status = %s, ledger = %s, error = %s WHERE id = %s",
            ("failed" if error else "ready", Jsonb(stored), error, set_id),
        )


def run(settings: config.Settings, set_id: str, *, meta: MetaClient | None = None) -> None:
    """Make the `making` set `set_id` (the module's docstring), on a connection of its own. Never raises: whatever
    goes wrong ends the set `failed` with a sentence the owner can read. `start` may be called inside the caller's
    transaction (every MCP tool body is), so the set becomes visible here only once that commits: the run waits up to
    ROW_WAIT_S for it, and a set that never appears (the caller rolled back) is no run at all."""
    with db.connect(settings.database_url) as conn:
        row = _await_set(conn, set_id)
        if row is None or row["status"] != "making":
            return
        deadline = time.monotonic() + GIVE_UP_S
        steps = _Steps(conn, set_id)
        try:
            client = meta or metamod.client(settings)
        except MetaError:
            _fail(conn, set_id, NO_MODELS)
            return
        try:
            result = _make(conn, client, row["business_id"], steps, deadline)
            _finish(conn, set_id, result)
        except _GiveUp:
            log.warning("creatives: gave up after %.0f s set=%s", GIVE_UP_S, set_id)
            _fail(conn, set_id, TOO_LONG)
            return
        except MetaError as e:  # only the text writer raises it: twice without an answer
            log.warning("creatives: text not written set=%s: %s", set_id, e)
            _fail(conn, set_id, NO_TEXT)
            return
        except Exception:  # a run on its own thread must end its set, whatever broke (log.exception keeps it)
            log.exception("creatives: run failed set=%s", set_id)
            _fail(conn, set_id, BROKE)


def _await_set(conn: psycopg.Connection, set_id: str) -> dict | None:
    """The set's row once this connection can see it, polling up to ROW_WAIT_S; None if it never appears."""
    ends = time.monotonic() + ROW_WAIT_S
    while True:
        row = conn.execute("SELECT business_id, status FROM ad_creative_set WHERE id = %s", (set_id,)).fetchone()
        if row is not None or time.monotonic() >= ends:
            return row
        time.sleep(ROW_POLL_S)


def _launch(settings: config.Settings, set_id: str) -> None:
    threading.Thread(target=run, args=(settings, set_id), name=f"creatives-{set_id}", daemon=True).start()


def fail_stale(conn: psycopg.Connection, business_id: str) -> bool:
    """The business's `making` set whose run died with its process (older than STALE_S: a run lives at most
    GIVE_UP_S), failed with DIED; True when there was one. Starts nothing: a new run is the owner's to ask for."""
    died = conn.execute(
        "UPDATE ad_creative_set SET status = 'failed', error = %s "
        "WHERE business_id = %s AND status = 'making' AND created_at < now() - make_interval(secs => %s) RETURNING id",
        (DIED, business_id, STALE_S),
    ).fetchall()
    return bool(died)


def start(conn: psycopg.Connection, settings: config.Settings, business_id: str, *, draft: bool = False) -> dict:
    """Start making a set (R3): no photos -> the `needs_photos` card with the valid upload link (or the sentence naming
    the tool that gives a fresh one), no set, no model called; a set already making -> its card, counted nothing; else
    a new `making` set, run on a daemon thread (which waits for this INSERT to commit with the caller's transaction).
    Each new set counts toward the business's day (UTC): past Settings.creatives_per_day of them
    (creatives_per_day_draft for a draft key) the start is refused, a ToolError in the owner's words. Returns the
    card."""
    base = settings.web_base_url
    if not _has_photos(conn, business_id):
        return _needs_photos(conn, business_id, base)
    set_id = None
    with conn.transaction():
        if conn.execute("SELECT 1 FROM business WHERE id = %s FOR UPDATE", (business_id,)).fetchone() is None:
            raise ToolError(NO_BUSINESS)
        fail_stale(conn, business_id)
        making = conn.execute(
            "SELECT id FROM ad_creative_set WHERE business_id = %s AND status = 'making' LIMIT 1", (business_id,)
        ).fetchone()
        if making is None:
            cap = settings.creatives_per_day_draft if draft else settings.creatives_per_day
            today = conn.execute(  # under the business row's lock: two starts can never both pass the count
                "SELECT count(*) AS n FROM ad_creative_set WHERE business_id = %s "
                "AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
                (business_id,),
            ).fetchone()["n"]
            if today >= cap:
                raise ToolError(DAILY_LIMIT.format(cap=cap))
            set_id = db.new_id("cs")
            conn.execute(
                "INSERT INTO ad_creative_set (id, business_id, status) VALUES (%s, %s, 'making')", (set_id, business_id)
            )
    if set_id is not None:
        _launch(settings, set_id)
    return card(conn, business_id, base)


# ---------------------------------------------------------------- the card, accepting


def _empty_text() -> dict:
    return {kind: [] for kind in copy.LIMITS}


def _upload_url(conn: psycopg.Connection, biz: str, base_url: str) -> str | None:
    """The owner's upload link for the current page while one is valid, read back, never minted: only create_page and
    build_site mint them (uploads.new_token); None otherwise."""
    page = landing.current(conn, biz)
    return uploads.latest_upload_url(conn, page["id"], base_url) if page is not None else None


def _needs_photos(conn: psycopg.Connection, biz: str, base_url: str) -> dict:
    """The card before any photo (R3): the valid upload link, and in `error` what the assistant does next."""
    url = _upload_url(conn, biz, base_url)
    return {
        "status": "needs_photos",
        "set_id": None,
        "steps": [],
        "images": [],
        "text": _empty_text(),
        "removed": [],
        "models": {},
        "upload_url": url,
        "error": NEEDS_PHOTOS if url else NO_UPLOAD_LINK,
    }


def _models(ledger: dict | None) -> dict:
    return {
        "read_photos": SPARK,
        "cut_out": SAM,
        "extend": IMAGE,
        "check_images": f"{WONDER} + {SAM}",
        "write_text": SPARK,
        "check_text": (ledger or {}).get("checker") or WONDER,
    }


def card(conn: psycopg.Connection, business_id: str, base_url: str, *, set_id: str | None = None) -> dict:
    """The business's newest set as the owner sees it (R2), or its set `set_id` (another business's is a ToolError).
    Without any set: `needs_photos` when there are no photos, else the same empty card with `status` None (nothing
    made yet). `error` is the failed set's sentence."""
    row = conn.execute(
        "SELECT id, status, steps, ledger, error FROM ad_creative_set WHERE business_id = %s "
        "AND (%s::text IS NULL OR id = %s) ORDER BY created_at DESC LIMIT 1",
        (business_id, set_id, set_id),
    ).fetchone()
    if row is None and set_id is not None:
        raise ToolError(NOT_YOURS)
    if row is None:
        empty = _needs_photos(conn, business_id, base_url)
        if _has_photos(conn, business_id):
            empty["status"], empty["error"] = None, None
        return empty
    ledger = row["ledger"] or {}
    facts = {f["id"]: f["text"] for f in ledger.get("facts") or []}
    text = _empty_text()
    for kind in text:
        for claim in (ledger.get("kept") or {}).get(kind) or []:
            source = "; ".join(facts[i] for i in claim.get("fact_ids") or [] if i in facts)
            text[kind].append({"text": claim["text"], "source": source})
    removed = [{"text": f"Photo {s['index']}", "why": s["why"]} for s in ledger.get("skipped") or []]
    removed += [{"text": r["text"], "why": r["why"]} for r in ledger.get("removed") or []]
    rows = conn.execute(
        "SELECT id, format, method, checks FROM ad_creative_image WHERE set_id = %s "
        "ORDER BY (checks->>'photo')::int, format",
        (row["id"],),
    ).fetchall()
    shown = [
        {
            "id": r["id"],
            "format": r["format"],
            "url": f"{base_url}/creatives/{r['id']}",
            "original_url": f"{base_url}/creatives/{r['id']}/original",
            "method": r["method"],
            "checks": r["checks"],
        }
        for r in rows
    ]
    return {
        "status": row["status"],
        "set_id": row["id"],
        "steps": row["steps"] or [],
        "images": shown,
        "text": text,
        "removed": removed,
        "models": _models(ledger),
        "upload_url": _upload_url(conn, business_id, base_url),
        "error": row["error"],
    }


def accept(conn: psycopg.Connection, business_id: str, set_id: str) -> dict:
    """The owner accepts a `ready` set of their own business: it is what the Google launch uses. Accepting it again is
    a no-op; another business's set, or one not ready, is a ToolError."""
    with conn.transaction():
        row = conn.execute(
            "SELECT status, accepted_at FROM ad_creative_set WHERE id = %s AND business_id = %s FOR UPDATE",
            (set_id, business_id),
        ).fetchone()
        if row is None:
            raise ToolError(NOT_YOURS)
        if row["status"] == "accepted":
            return {"set_id": set_id, "status": "accepted", "accepted_at": row["accepted_at"].isoformat()}
        if row["status"] != "ready":
            raise ToolError(NOT_READY)
        done = conn.execute(
            "UPDATE ad_creative_set SET status = 'accepted', accepted_at = now() WHERE id = %s RETURNING accepted_at",
            (set_id,),
        ).fetchone()
    return {"set_id": set_id, "status": "accepted", "accepted_at": done["accepted_at"].isoformat()}


def accepted(conn: psycopg.Connection, business_id: str) -> dict | None:
    """The business's newest accepted set, as the Google launch uses it: the kept lines and the images' raw bytes."""
    row = conn.execute(
        "SELECT id, ledger FROM ad_creative_set WHERE business_id = %s AND status = 'accepted' "
        "ORDER BY accepted_at DESC LIMIT 1",
        (business_id,),
    ).fetchone()
    if row is None:
        return None
    kept = (row["ledger"] or {}).get("kept") or {}
    rows = conn.execute(
        "SELECT id, format, bytes FROM ad_creative_image WHERE set_id = %s ORDER BY (checks->>'photo')::int, format",
        (row["id"],),
    ).fetchall()
    return {
        "set_id": row["id"],
        **{kind: [c["text"] for c in kept.get(kind) or []] for kind in copy.LIMITS},
        "images": [{"id": r["id"], "format": r["format"], "bytes": bytes(r["bytes"])} for r in rows],
    }
