"""A site bundle's upload and its source's download, on the agent host (A3p-3 spec §6.3, §6.5, §7, P19, P32; plan
2026-10-04-agent-a3p-3-3-site-bundles.md B2, B3, B11, B12).

`request_site_upload` (agent/tools_hosting.py, behind `update_landing_page(action="upload_site")`) mints a link here: `PUT <agent base>/s/<token>`, bound to the business's page,
good for 30 minutes and one upload -- an assistant with a shell sends its zip with `curl --fail-with-body -T site.zip
<link>`, with no key on the command line. The body is bounded twice, the photo upload's way (agent/uploads.py): its
Content-Length must be there (411) and within 25 MB (413), and the bytes are counted as they stream in, whatever the
header said. It is spooled to a temporary file (in memory up to a megabyte), the link is claimed once the whole body
is in, and the zip is judged in a thread (agent/bundles.py; at most two at once, so a burst of uploads never holds
more than two sites in memory). A refused upload stores nothing but its result on the link's row; one that passes
stores its files and the business's bundle preview in one transaction; then, where the render check runs
(AGENT_RENDER_URL, A3p-3 batch 4), a headless browser opens that preview -- its problems refuse the upload like the
static checks' and drop the preview, a renderer that does not answer refuses it too (503), and its screenshots stay with
the preview. One upload of a page at a time, from its keeping to its render. The answer is JSON either way:
`{status: "ready", say, preview_url, token, expires_at, files, bytes, problems: []}` (200) or `{status: "refused",
say, problems: [{file, rule, message, fix}]}` (422, or 404/410/411/413 for the link or the body).

`GET <agent base>/s/zip/<token>` is a bundle's source as one zip (`get_page_source`'s `zip_url`): a token good for an
hour, like every other link of the agent's -- no signing key exists on the boxes."""

from __future__ import annotations

import tempfile
import threading
from datetime import timedelta

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from . import bundles, config, db, hosted, landing, render_check, uploads
from .util import iso

router = APIRouter()

LINK_TTL = timedelta(minutes=30)
ZIP_TTL = timedelta(hours=1)
SPOOL_BYTES = 1024 * 1024  # a body stays in memory up to this, on disk past it
BUSY_WAIT_S = 60
_UNPACKING = threading.BoundedSemaphore(2)  # at most two sites unpacked at once (B3)
# one upload of a page at a time in this process, from its keeping to its render (A3p-3 batch 4): the render opens the
# business's preview host, which shows its newest preview -- a second upload landing meanwhile would be judged instead
_PAGES: dict[str, threading.Lock] = {}
_PAGES_GUARD = threading.Lock()
LAST_PROBLEMS = 20  # the last upload's problems get_page shows (B12)

HOW = "curl --fail-with-body -T site.zip {url}"
NEW_LINK = "Ask Admiry for a new upload link."
NO_LENGTH = "The upload needs its length (a Content-Length header)."
NO_LENGTH_FIX = "Upload the file with curl -T site.zip <link>, which sends its length."
UNKNOWN = "This upload link is not one of Admiry's."
USED = "This upload link was already used: each link takes one upload."
EXPIRED = "This upload link has expired: a link works for 30 minutes."
BUSY = "Admiry is checking other sites right now; upload again in a minute."
NOT_ACTIVE = "This business is not active."
CUT_OFF = "The upload was cut off before it ended."
HOSTING_OFF = "Hosting is not set up on this server."


def _refused(status: int, file: str, rule: str, message: str, fix: str) -> JSONResponse:
    body = {"status": "refused", "say": bundles.REFUSED, "problems": [bundles.problem(file, rule, message, fix)]}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- the link (B2)


def new_link(conn: psycopg.Connection, biz: str, page_id: str, base: str) -> dict:
    """A one-time upload link for the business's page: {token, upload_url, expires_at, how}."""
    token = db.new_id("su")
    row = conn.execute(
        "INSERT INTO site_upload (token, business_id, page_id, expires_at) VALUES (%s, %s, %s, now() + %s) "
        "RETURNING expires_at",
        (token, biz, page_id, LINK_TTL),
    ).fetchone()
    url = f"{base}/s/{token}"
    return {"token": token, "upload_url": url, "expires_at": iso(row["expires_at"]), "how": HOW.format(url=url)}


def last(conn: psycopg.Connection, page_id: str) -> dict | None:
    """The business's last upload, for get_page (B12): its result (`status`, `say`, the first problems, the preview
    link and token when it passed) and when it came; None before any."""
    row = conn.execute(
        "SELECT result, used_at FROM site_upload WHERE page_id = %s AND used_at IS NOT NULL AND result IS NOT NULL "
        "ORDER BY used_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row["result"])
    out = {
        "status": result.get("status"),
        "at": iso(row["used_at"]),
        "say": result.get("say"),
        "problems": list(result.get("problems") or [])[:LAST_PROBLEMS],
    }
    if result.get("status") == "ready":
        out.update(preview_url=result.get("preview_url"), token=result.get("token"))
    return out


def prune(conn: psycopg.Connection, *, i: int = 0, n: int = 1) -> int:
    """Expired zip links, and upload links a week past their end (of shard `i` of `n`, the hourly page-check's split,
    B13); returns how many rows went."""
    shard = {"i": i, "n": n}
    zips = conn.execute(
        "DELETE FROM site_zip WHERE expires_at <= now() AND mod(abs(hashtext(token)::bigint), %(n)s) = %(i)s", shard
    ).rowcount
    links = conn.execute(
        "DELETE FROM site_upload WHERE expires_at <= now() - interval '7 days' "
        "AND mod(abs(hashtext(token)::bigint), %(n)s) = %(i)s",
        shard,
    ).rowcount
    return zips + links


# ---------------------------------------------------------------- the upload (B3)


def _claim(token: str) -> dict | JSONResponse:
    """The link's business and page, claimed for this upload (single use); or the refusal: a link never minted (404),
    used or expired (410), a business no longer active (403), hosting off (404)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        row = conn.execute(
            "UPDATE site_upload SET used_at = now() WHERE token = %s AND used_at IS NULL AND expires_at > now() "
            "RETURNING business_id, page_id",
            (token,),
        ).fetchone()
        if row is None:
            seen = conn.execute("SELECT used_at FROM site_upload WHERE token = %s", (token,)).fetchone()
            if seen is None:
                return _refused(404, "/", "link", UNKNOWN, NEW_LINK)
            return _refused(410, "/", "link", USED if seen["used_at"] else EXPIRED, NEW_LINK)
        if not settings.pages_domain:
            return _refused(404, "/", "link", HOSTING_OFF, NEW_LINK)
        active = conn.execute("SELECT status FROM business WHERE id = %s", (row["business_id"],)).fetchone()
        if active is None or active["status"] != "active":
            return _refused(403, "/", "link", NOT_ACTIVE, NEW_LINK)
        page = landing.get(conn, row["page_id"])
        tag = landing.google_tag(conn, row["business_id"])
        return {
            "business_id": row["business_id"],
            "page_id": row["page_id"],
            "own_host": page.get("hostname") if page else None,
            "snippet": landing.snippet(row["page_id"], settings.public_base_url, tag),
        }


def _record(token: str, result: dict) -> None:
    with db.connect(config.load().database_url) as conn:
        conn.execute("UPDATE site_upload SET result = %s WHERE token = %s", (Jsonb(result), token))


def _keep(link: dict, judged: bundles.Judged) -> dict:
    """A site that passed (B7): its files, the business's bundle preview (ending the one before) and its preview host,
    in one transaction. Answers what the ready result names: the preview's link, token, end, files and bytes."""
    settings = config.load()
    with db.connect(settings.database_url) as conn, conn.transaction():
        # two uploads for one page at once: one after the other, so the later one ends the earlier's preview
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"bundle:{link['page_id']}",))
        files = bundles.store(conn, judged.files)
        preview = bundles.save_preview(conn, link["page_id"], judged, files)
        host = bundles.preview_host(conn, link["page_id"], settings.pages_domain)
        expires = hosted.preview(conn, preview)["expires_at"]
    return {
        "preview_url": f"https://{host}/",
        "token": preview,
        "expires_at": iso(expires),
        "files": len(files),
        "bytes": judged.size,
    }


def _ready(kept: dict, rendered: dict | None) -> dict:
    url = kept["preview_url"]
    return {
        "status": "ready",
        "say": f"{bundles.READY} Here is its preview: {url}. Look it over, then publish it to make it live.",
        **kept,
        "problems": [],
        # A3p-3 batch 4: what the headless browser opened (None when no render check runs on this server)
        "render": {"loads": len(rendered["pages"]), "partial": rendered["partial"]} if rendered else None,
    }


def _drop(preview: str) -> None:
    """The preview of a site the render refused: gone at once (its files' rows with it); the files nothing names any
    more go with the next collection."""
    with db.connect(config.load().database_url) as conn:
        conn.execute("DELETE FROM page_preview WHERE token = %s", (preview,))


def _keep_shots(preview: str, rendered: dict) -> None:
    with db.connect(config.load().database_url) as conn:
        conn.execute(
            "UPDATE page_preview SET shot_phone = %s, shot_tablet = %s, shot_desktop = %s WHERE token = %s",
            (
                render_check.shot(rendered, "phone"),
                render_check.shot(rendered, "tablet"),
                render_check.shot(rendered, "desktop"),
                preview,
            ),
        )


def _render(token: str, link: dict, kept: dict) -> JSONResponse | dict | None:
    """The render check of a kept site (A3p-3 batch 4, R5): None when no render check runs here; the render when it
    found nothing (its screenshots kept with the preview); else the refusal -- its problems (422), or the renderer not
    answering (503) -- with the preview dropped and the result on the link's row."""
    settings = config.load()
    if not render_check.on(settings):
        return None
    try:
        rendered = render_check.render(settings, kept["preview_url"], link["page_id"])
    except render_check.Unavailable:
        _drop(kept["token"])
        refused = _refused(503, "/", "render", render_check.UNAVAILABLE, render_check.UNAVAILABLE_FIX)
        _record(
            token,
            {
                "status": "refused",
                "say": bundles.REFUSED,
                "problems": [bundles.problem("/", "render", render_check.UNAVAILABLE, render_check.UNAVAILABLE_FIX)],
            },
        )
        return refused
    if rendered["problems"]:
        _drop(kept["token"])
        result = bundles.answer(bundles.Judged(problems=rendered["problems"]))
        _record(token, result)
        return JSONResponse(result, status_code=422, headers={"Cache-Control": "no-store"})
    _keep_shots(kept["token"], rendered)
    return rendered


def _page_lock(page_id: str) -> threading.Lock:
    with _PAGES_GUARD:
        return _PAGES.setdefault(page_id, threading.Lock())


def _handle(token: str, body) -> JSONResponse:
    """The blocking half of the upload: the claim, the judging, the keeping and the render, at most two at once."""
    if not _UNPACKING.acquire(timeout=BUSY_WAIT_S):
        return _refused(503, "/", "busy", BUSY, "Upload again in a minute with the same link.")
    try:
        link = _claim(token)
        if isinstance(link, JSONResponse):
            return link
        settings = config.load()
        judged = bundles.judge(
            body,
            page_id=link["page_id"],
            own_host=link["own_host"],
            agent_base=settings.public_base_url,
            snippet=link["snippet"],
        )
        if not judged.ok:
            result = bundles.answer(judged)
            _record(token, result)
            return JSONResponse(result, status_code=422, headers={"Cache-Control": "no-store"})
        with _page_lock(link["page_id"]):
            kept = _keep(link, judged)
            rendered = _render(token, link, kept)
        if isinstance(rendered, JSONResponse):
            return rendered
        result = _ready(kept, rendered)
        _record(token, result)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    finally:
        _UNPACKING.release()


@router.put("/s/{token}")
async def site_put(token: str, request: Request) -> Response:
    """The zip of a site's build output, PUT to its one-time link (B3)."""
    length = request.headers.get("content-length")
    if length is None:
        return _refused(411, "/", "upload", NO_LENGTH, NO_LENGTH_FIX)
    try:
        declared = int(length)
    except ValueError:
        return _refused(400, "/", "upload", NO_LENGTH, NO_LENGTH_FIX)
    too_big = f"The zip is more than {bundles._mb(bundles.MAX_ZIP_BYTES)}."
    if declared > bundles.MAX_ZIP_BYTES:
        return _refused(413, "/", "zip_size", too_big, bundles.SMALLER_FIX)
    request._receive = uploads._counted_receive(request._receive, bundles.MAX_ZIP_BYTES)
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as spool:
        try:
            async for chunk in request.stream():
                spool.write(chunk)
        except uploads._TooLarge:
            return _refused(413, "/", "zip_size", too_big, bundles.SMALLER_FIX)
        except ClientDisconnect:
            return _refused(400, "/", "upload", CUT_OFF, "Upload the zip again with the same link.")
        spool.seek(0)
        return await run_in_threadpool(_handle, token, spool)


# ---------------------------------------------------------------- the source's zip (B11)


def zip_link(conn: psycopg.Connection, page_id: str, version: int, base: str) -> dict:
    """A link to a bundle version's files as one zip, good for an hour: {zip_url, zip_expires_at}."""
    token = db.new_id("sz")
    row = conn.execute(
        "INSERT INTO site_zip (token, page_id, version, expires_at) VALUES (%s, %s, %s, now() + %s) "
        "RETURNING expires_at",
        (token, page_id, version, ZIP_TTL),
    ).fetchone()
    return {"zip_url": f"{base}/s/zip/{token}", "zip_expires_at": iso(row["expires_at"])}


def zip_response(data: bytes, hostname: str | None) -> Response:
    name = f"{hostname or 'site'}.zip"
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/s/zip/{token}")
def site_zip(token: str) -> Response:
    """A bundle's files as one zip while its link lasts; a plain 404 otherwise (an expired link and an unknown one read
    the same)."""
    with db.connect(config.load().database_url) as conn:
        row = conn.execute(
            "SELECT z.page_id, z.version, p.hostname FROM site_zip z JOIN page p ON p.id = z.page_id "
            "WHERE z.token = %s AND z.expires_at > now()",
            (token,),
        ).fetchone()
        files = bundles.version_files(conn, row["page_id"], row["version"]) if row else {}
        if not files:
            return PlainTextResponse("Not found.", status_code=404, headers={"Cache-Control": "no-store"})
        data = bundles.zip_of(conn, files)
    return zip_response(data, row["hostname"])
