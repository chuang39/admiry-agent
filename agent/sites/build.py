"""An industry site built into the business's bundle preview (A3p-6 batch 1 Q6, Q8; batch 2 Q9, Q11; T13, T20, T21).

`preview` renders a site document with Admiry's templates and puts it through A3p-3's bundle pipeline exactly as an
assistant's upload goes: the static checks (`bundles.check`, the sanitiser on every page, the kit's script tag, the
limits), the files kept once each (`bundles.store`; the stock photos' sizes are already kept and only named), the
business's one bundle preview with its `site_spec` row, served on its preview host; then the render check at a phone, a
tablet and a desktop size, and Google's speed test of the home page (`checks="full"`; a photo change builds again with
`checks="static"`, the first two alone). A problem in any of them is the fault of Admiry's own template, never
something for the owner to fix: it drops the preview and raises `TemplateFault`. A preview built here is published only
once `publish_gate` lets it: the site's logo (or the wordmark the owner accepted) and the family's key photo are in
(A3p-6 batch 2 Q11, T13) and the preview carries them -- and what goes live is not the preview's files but the same
document drawn again for live (`live`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

import psycopg
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from .. import bundles, business, config, db, hosted, landing, pagespeed, render_check, site_upload
from ..util import iso
from . import images, intake, photos, render
from . import spec as spec_mod
from .spec import SiteSpec

log = logging.getLogger(__name__)

# Q11: the two refusals of `publish_gate` beside the key photos' own (`photos.GATE`)
GALLERY = "This preview is Admiry's gallery of looks; it is not published."
REBUILD_FIRST = "Your photos are in; build the site once more, then publish."
PUBLISH_FAILED = "Admiry couldn't publish your site just now. Try again in a few minutes."
CHECKS = ("full", "static")
STATIC_ONLY = "built without the render check (a photo change)"
NO_RENDER = "The render check is not set up on this server (AGENT_RENDER_URL): only the static checks ran."
NO_SPEED = "Google's speed test is not set up on this server (AGENT_PAGESPEED_API_KEY): it did not run."
SPEED_DOWN = "Google's speed test did not answer: the preview stands without its scores."
SLOW = "Google's speed test gave the home page {n} of 100 on a phone; Admiry aims for 80 or more{lcp}."
NOT_OPENED = "The render check ran out of time before opening: {pages}."
HELD_BACK = "Google's speed test gave the home page {n} of 100 on a phone; Admiry holds a preview back under 50."
LIGHTEN = "Lighten Admiry's template{lcp}."


class TemplateFault(Exception):
    """A built site failed a check: Admiry's own template's fault (never shown to an owner as theirs to fix).
    `problems` in the static checks' shape, `{file, rule, message, fix}`."""

    def __init__(self, problems: list[dict]) -> None:
        super().__init__("; ".join(p.get("message", "") for p in problems[:3]))
        self.problems = problems


@dataclass
class Built:
    """What a build left: the preview (its token, address and end), how many files and bytes it serves, the version of
    the `site_spec` document it wrote (0 for a bundle no document stands behind: the gallery's index), what the render
    check opened (None without one), the speed test's scores (None unset, `{"error": "unavailable"}` when Google did
    not answer), and the notes for the operator -- never a problem (a problem raises)."""

    token: str
    preview_url: str
    expires_at: str
    files: int
    bytes: int
    version: int = 0
    render: dict | None = None
    pagespeed: dict | None = None
    warnings: list[str] = field(default_factory=list)


def _url_path(at: str) -> str:
    """A page's file in the bundle as the address a visitor opens: `about/index.html` -> `/about/`."""
    return "/" + at.removesuffix("index.html")


def _static(
    rendered: render.Rendered, *, page_id: str, own_host: str | None, agent_base: str, snippet: str, ref_bytes: int
) -> bundles.Judged:
    """The checks an upload passes (§6.4), on the rendered files, and the limits counted with the stock photos."""
    judged = bundles.Judged(files=rendered.files)
    bundles.check(judged, page_id=page_id, own_host=own_host, agent_base=agent_base, snippet=snippet)
    if len(rendered.files) + len(rendered.refs) > bundles.MAX_FILES:
        judged.problems.append(bundles.problem("/", "file_count", f"The site has more than {bundles.MAX_FILES} files."))
    if judged.size + ref_bytes > bundles.MAX_TOTAL_BYTES:
        judged.problems.append(
            bundles.problem("/", "total_size", f"The site is more than {bundles._mb(bundles.MAX_TOTAL_BYTES)}.")
        )
    return judged


def _render_checked(
    conn: psycopg.Connection,
    site: SiteSpec,
    biz: str,
    page_id: str,
    *,
    own_host: str | None,
    agent_base: str,
    snippet: str,
    preview: bool,
) -> tuple[render.Rendered, bundles.Judged, int]:
    """The site's slots filled and its bundle rendered (`preview`: the rendering for looking at, or the one for a live
    site), then put through the upload's static checks with the stock photos' bytes counted in the limits; TemplateFault
    for any problem. Returns the bundle, what the checks read (its home page and title) and the stock photos' bytes."""
    filled = images.fill(conn, site, business_id=biz, page_id=page_id)
    rendered = render.bundle(
        site,
        page_id=page_id,
        agent_base=agent_base,
        snippet=snippet,
        hostname=own_host,
        images=filled,
        preview=preview,
    )
    ref_sizes = {
        r["sha256"]: r["size"]
        for r in conn.execute(
            "SELECT sha256, size FROM site_blob WHERE sha256 = ANY(%s)", (list(set(rendered.refs.values())),)
        ).fetchall()
    }
    ref_bytes = sum(ref_sizes.get(sha, 0) for sha in rendered.refs.values())
    judged = _static(
        rendered, page_id=page_id, own_host=own_host, agent_base=agent_base, snippet=snippet, ref_bytes=ref_bytes
    )
    if not judged.ok:
        raise TemplateFault(judged.problems)
    return rendered, judged, ref_bytes


def _keep(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    page_id: str,
    site: SiteSpec,
    judged: bundles.Judged,
    refs: dict[str, str],
    *,
    sample: str | None,
    ttl: timedelta,
) -> tuple[str, str, str, int]:
    """In one transaction, under the advisory lock an upload's keeping takes (`site_upload._keep`): the files, the
    stock photos named (touched, so the collection leaves them), the `site_spec` row (the page's next version), the
    bundle preview (ending the one before) and the preview host, and `page.site_spec_id`. Returns (token, preview
    URL, expires_at, the document's version)."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"bundle:{page_id}",))
        stored = bundles.store(conn, judged.files)
        bundles.touch(conn, refs)
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM site_spec WHERE page_id = %s", (page_id,)
        ).fetchone()["n"]
        spec_id = db.new_id("ss")
        conn.execute(
            "INSERT INTO site_spec (id, business_id, page_id, version, family, style, data) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (spec_id, biz, page_id, version, site.family, site.style, Jsonb(site.model_dump(mode="json"))),
        )
        sections = {
            "kind": "site",
            "site_spec_id": spec_id,
            "family": site.family,
            "style": site.style,
            "sample": sample,
            "render": None,
            "pagespeed": None,
        }
        token = bundles.save_preview(conn, page_id, judged, {**stored, **refs}, ttl=ttl, sections=sections)
        host = bundles.preview_host(conn, page_id, settings.pages_domain)
        conn.execute("UPDATE page SET site_spec_id = %s WHERE id = %s", (spec_id, page_id))
        expires = hosted.preview(conn, token)["expires_at"]
    return token, f"https://{host}/", iso(expires), version


def _render(settings: config.Settings, url: str, page_id: str, token: str, pages: list[str]) -> dict:
    """The render check of the kept preview: its problems drop it and raise TemplateFault; the renderer not answering
    drops it and raises `render_check.Unavailable`; a pass keeps the three screenshots and answers {loads, partial,
    pages (the addresses opened), not_opened (the site's pages a partial render never reached)}."""
    try:
        rendered = render_check.render(settings, url, page_id)
    except render_check.Unavailable:
        site_upload._drop(token)
        raise
    if rendered["problems"]:
        site_upload._drop(token)
        raise TemplateFault(rendered["problems"])
    site_upload._keep_shots(token, rendered)
    opened = list(dict.fromkeys(p.get("path") for p in rendered["pages"] if isinstance(p, dict) and p.get("path")))
    missed = [p for p in (_url_path(at) for at in pages) if p not in opened]
    return {"loads": len(rendered["pages"]), "partial": rendered["partial"], "pages": opened, "not_opened": missed}


def _speed(settings: config.Settings, url: str, token: str, site: SiteSpec, warnings: list[str]) -> dict:
    """Google's speed test of the home page: under 50 drops the preview and raises TemplateFault; under 80 tells the
    operator (a WARNING and a note); Google not answering leaves the preview standing."""
    try:
        scores = pagespeed.score(settings, url)
    except pagespeed.Unavailable:
        log.warning(
            "site build: pagespeed unavailable for %s/%s on %s", site.family, site.style, urlsplit(url).hostname
        )
        warnings.append(SPEED_DOWN)
        return {"error": "unavailable"}
    n, lcp_ms = scores["performance"], scores["lcp_ms"]
    lcp = f": the LCP was {lcp_ms} ms" if lcp_ms is not None else ": the LCP was not reported"
    if n < pagespeed.REFUSE_UNDER:
        site_upload._drop(token)
        message = HELD_BACK.format(n=n)
        raise TemplateFault([bundles.problem("/", "speed", message, LIGHTEN.format(lcp=lcp))])
    if n < pagespeed.WARN_UNDER:
        log.warning(
            "site build: pagespeed %s for %s/%s on %s (%s)",
            n,
            site.family,
            site.style,
            urlsplit(url).hostname,
            f"lcp {lcp_ms} ms" if lcp_ms is not None else "lcp not reported",
        )
        warnings.append(
            SLOW.format(n=n, lcp=f" (the LCP was {lcp_ms} ms)" if lcp_ms is not None else " (the LCP was not reported)")
        )
    return scores


def preview(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    spec: SiteSpec,
    *,
    sample: str | None = None,
    ttl: timedelta = hosted.PREVIEW_TTL,
    checks: str = "full",
) -> Built:
    """The site `spec` built as the business's bundle preview (Q6), checked as an upload is, then by the render check
    and Google's speed test when they are set up (`checks="full"`). `checks="static"` (Q9: a photo change, seconds
    and not a minute on a phone) keeps the preview on the upload's checks alone: no render check, no speed test,
    `sections.render` and `pagespeed` None, `Built.render` None and a note saying so; the preview then has no
    screenshots. `conn` is an autocommit connection: the preview is committed before the renderer and Google open it
    through the preview host (and not inside a transaction: refused with ValueError). One build of a page at a time
    in this process (the upload's own lock), from its keeping to its speed test. ToolError when hosting is off, the
    business is not active or the document is not one Admiry renders; TemplateFault for any check's problem, and
    `render_check.Unavailable` when the renderer does not answer. A static-check problem keeps nothing; a render or
    speed fault drops the new preview, but the `site_spec` row and `page.site_spec_id` stay, and the business's earlier
    preview was already ended when the new one was saved."""
    if checks not in CHECKS:
        raise ValueError(f"checks must be one of {', '.join(CHECKS)}, not {checks!r}")
    if not (conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE):
        raise ValueError(
            "build.preview needs an autocommit connection outside a transaction: the render check reads what it keeps"
        )
    row = business.row(conn, biz)
    if row is None or row["status"] != "active":
        raise ToolError(site_upload.NOT_ACTIVE)
    if not settings.pages_domain:
        raise ToolError(site_upload.HOSTING_OFF)
    site = spec_mod.validate(spec)
    page = landing.ensure_page_for_kit(conn, biz)
    page_id = page["id"]
    agent_base = settings.public_base_url
    snippet = landing.snippet(page_id, agent_base, landing.google_tag(conn, biz))
    rendered, judged, ref_bytes = _render_checked(
        conn,
        site,
        biz,
        page_id,
        own_host=page.get("hostname"),
        agent_base=agent_base,
        snippet=snippet,
        preview=True,
    )

    warnings: list[str] = []
    checked: dict | None = None
    scores: dict | None = None
    with site_upload._page_lock(page_id):
        token, url, expires, version = _keep(
            conn, settings, biz, page_id, site, judged, rendered.refs, sample=sample, ttl=ttl
        )
        if checks == "static":
            warnings.append(STATIC_ONLY)
        else:
            if render_check.on(settings):
                checked = _render(settings, url, page_id, token, rendered.pages)
                if checked["partial"]:
                    warnings.append(NOT_OPENED.format(pages=", ".join(checked["not_opened"])))
            else:
                warnings.append(NO_RENDER)
            if pagespeed.on(settings):
                scores = _speed(settings, url, token, site, warnings)
            else:
                warnings.append(NO_SPEED)
            conn.execute(
                "UPDATE page_preview SET sections = sections || %s WHERE token = %s",
                (Jsonb({"render": checked, "pagespeed": scores}), token),
            )
    return Built(
        token=token,
        preview_url=url,
        expires_at=expires,
        files=len(judged.files) + len(rendered.refs),
        bytes=judged.size + ref_bytes,
        version=version,
        render=checked,
        pagespeed=scores,
        warnings=warnings,
    )


@dataclass
class Live:
    """A site as it goes live (`live`): `files` the bundle's own bytes by path, `refs` the stock photos' paths -> the SHA-256
    of blobs already stored, `home` the home page's HTML and `title` its <title>, which a version keeps as its own."""

    files: dict[str, bytes]
    refs: dict[str, str]
    home: str
    title: str | None

    def named(self) -> dict[str, str]:
        """Every file's SHA-256 by path, as a version's files name them: what a version made from this would hold."""
        return {**{path: bundles.sha256(data) for path, data in self.files.items()}, **self.refs}


def live(
    conn: psycopg.Connection, settings: config.Settings, biz: str, page_id: str, sections: dict, *, hostname: str
) -> Live:
    """The site a preview shows, drawn again for going live at `hostname` (A3p-6 batch 2: publishing copies no preview).
    A preview is for looking at -- "Replace with your photo" on every stock photo, stock shown as the business's "recent
    work" and as the practice's rooms, and no canonical link or structured-data `url` because it stands on a host of its
    own -- and a live site is none of those (T11-T14). The document is the preview's (`site_spec` by
    `sections["site_spec_id"]`, the page's own) and so are its stock photos (the picks are seeded by the business), so a
    page differs from the preview's only by those labels, those stock-only sections and the canonical links; the
    screenshots the render check kept of the preview stand for it. The static checks run on what is drawn, as they did
    on the preview, with this host; a problem (or a document that is not there) is Admiry's template's fault, never
    the owner's to fix: it is logged at ERROR with its problems and answered as PUBLISH_FAILED, so nothing is published.
    Database and CPU only, no network: it runs inside the publish's transaction. The tracking snippet is the kit's as
    it is now, the business's Google tag included."""
    spec_id = sections.get("site_spec_id")
    row = conn.execute("SELECT data FROM site_spec WHERE id = %s AND page_id = %s", (spec_id, page_id)).fetchone()
    if row is None:
        log.error("site publish: page %s has no document %s to draw for live", page_id, spec_id)
        raise ToolError(PUBLISH_FAILED)
    agent_base = settings.public_base_url
    snippet = landing.snippet(page_id, agent_base, landing.google_tag(conn, biz))
    try:
        site = SiteSpec.model_validate(row["data"])
        rendered, judged, _ref_bytes = _render_checked(
            conn, site, biz, page_id, own_host=hostname, agent_base=agent_base, snippet=snippet, preview=False
        )
    except (TemplateFault, ValueError) as e:  # ValueError: pydantic's, and the renderer's for a document with no words
        problems = e.problems if isinstance(e, TemplateFault) else None
        log.exception("site publish: page %s did not draw for live (problems: %s)", page_id, problems)
        raise ToolError(PUBLISH_FAILED) from e
    return Live(files=rendered.files, refs=rendered.refs, home=judged.home or "", title=judged.title)


def _carries(conn: psycopg.Connection, sections: dict, page_id: str, family: str) -> bool:
    """Whether the preview shows the logo and the key photo the page has now (Q11): its document (`site_spec`, by the
    id its sections name) names, in the logo slot and the family's key slot, the owner's photo that `photos.images`
    would name today -- or none where there is none (the wordmark the owner accepted). False when a photo came in, was
    replaced or was taken out after the build, or the document is gone: the owner builds the site once more."""
    row = conn.execute(
        "SELECT data FROM site_spec WHERE id = %s AND page_id = %s", (sections.get("site_spec_id"), page_id)
    ).fetchone()
    if row is None:
        return False
    named = (row["data"] or {}).get("images") or {}
    on_file = photos.images(conn, page_id, family)
    for slot in (images.LOGO_SLOT, photos.SLOTS[family]["key"][0]):
        ref = named.get(slot) or {}
        shown = ref.get("id") if ref.get("source") == "owner" else None
        if shown != (on_file[slot].id if slot in on_file else None):
            return False
    return True


def publish_gate(conn: psycopg.Connection, preview: dict | None) -> str | None:
    """Why a preview may not be published yet (Q11, T13), or None. Only an industry site's preview is judged (a bundle
    preview whose sections say `kind: site`; an assistant's upload and a one-page preview are none of this): the
    gallery of looks is never published (`GALLERY`); a site waits for its logo -- or the wordmark the owner accepted
    -- and the family's key photo (`photos.key_photos`, the family read from the preview's own sections, never from
    the business); and the preview must carry the photos the page has now (`REBUILD_FIRST`). A preview whose family
    has no sentences (`photos.GATE`) is Admiry's own fault: it is logged at ERROR and answered as `PUBLISH_FAILED`,
    never raised. A dental site publishes with its appointment form: nothing here asks about it."""
    sections = (preview or {}).get("sections") or {}
    if not (preview and preview.get("kind") == "bundle" and sections.get("kind") == "site"):
        return None
    family = sections.get("family")
    if family is None or sections.get("gallery"):
        return GALLERY
    page_id = preview["page_id"]
    if not (isinstance(family, str) and family in photos.GATE):  # no sentences for it: Admiry's own fault, never raised
        log.error("site publish: page %s's preview names a family that is none of ours: %r", page_id, family)
        return PUBLISH_FAILED
    found = photos.key_photos(conn, page_id, family, intake.draft(landing.get(conn, page_id)))
    if not found["ok"]:
        return found["sentence"]
    return None if _carries(conn, sections, page_id, family) else REBUILD_FIRST
