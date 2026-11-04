"""The industry site gallery (A3p-6 batch 1 Q22, Q23; spec T29, §12 item 4): the two samples in their family's three
styles, each a full `build.preview` (the static checks, the render check at three sizes, Google's speed test), then one
more bundle -- `index.html` with the six, each with its name, a line on its style, its three screenshots
(`shots/<sample>-<style>-<size>.jpg`) and a link into it, and the six sites under `/<sample>-<style>/` rendered with
that `base` -- through the static checks only (each site was already checked whole). It becomes the business's one
live preview (P35), the address the founder opens to approve the looks. Operator-only (`agent.ops site-gallery`)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import psycopg
from fastmcp.exceptions import ToolError

from .. import bundles, business, config, hosted, landing, site_upload
from ..templating import env
from ..util import iso
from . import build as site_build
from . import images, render, samples
from . import spec as spec_mod

MAX_FILES = bundles.MAX_FILES  # the gallery's file limit (a test lowers it alone)
OVER = "The gallery is over the bundle's limit:"
# every sample with its family's styles, in the order an index shows them; the founder's gallery of 2026-10-06 is the
# first two, built when no sample is named (REVIEWED); a trade's sample is built by name (`--samples`, 2026-10-08)
HOME, DENTAL = ("solid", "clean", "bold"), ("calm", "warm", "boutique")
SAMPLES = (("roofer", HOME), ("dentist", DENTAL), ("hvac", HOME), ("plumber", HOME), ("electrician", HOME))
REVIEWED = ("roofer", "dentist")
SIZES = (("phone", "a phone"), ("tablet", "a tablet"), ("desktop", "a computer"))
# Q2's table in plain words: what the founder is looking at in each
LINES = {
    "roofer-solid": "Solid: Archivo headings over Public Sans, navy and white with an orange button, and a top bar "
    "with the towns, the hours and the phone.",
    "roofer-clean": "Clean: Manrope throughout, white and soft green with a round green button, the numbers that "
    "matter in a row and the reviews on a dark ground.",
    "roofer-bold": "Bold: tall uppercase Barlow Condensed headings on a near-black ground, a yellow button and a "
    "yellow strip of reasons to call, the problem picker right under it.",
    "dentist-calm": "Calm: Plus Jakarta Sans, deep teal on a pale mint ground, an Accepting new patients badge and, "
    "on a phone, the emergency line above the welcome.",
    "dentist-warm": "Warm: Figtree with round buttons, plum words on a peach ground, the welcome centred over a "
    "full-width photo.",
    "dentist-boutique": "Boutique: Cormorant Garamond over Jost, a deep green ground with gold, square corners and the "
    "practice's name on two lines.",
}
# a home-services trade's sample in a style is described as the roofer is in it: the style is the same
LINES |= {f"{name}-{style}": LINES[f"roofer-{style}"] for name in ("hvac", "plumber", "electrician") for style in HOME}


class OverLimit(site_build.TemplateFault):
    """The gallery's index bundle past a bundle's file or byte limit: the six sites together, not any template."""


def _limits(whole: render.Rendered, judged: bundles.Judged, *, ref_bytes: int) -> None:
    """The bundle's limits for the gallery (ruling, Task 8 fix round 1): its own files plus its refs counted by
    distinct blob -- the six sites name the same stored photos under six prefixes, each already checked by its own
    build -- and the bytes the same way (`ref_bytes` is the distinct blobs' sizes)."""
    entries = len(judged.files) + len(set(whole.refs.values()))
    if entries > MAX_FILES:
        judged.problems.append(
            bundles.problem("/", "file_count", f"{OVER} {entries} files and stored photos, at most {MAX_FILES}.")
        )
    if judged.size + ref_bytes > bundles.MAX_TOTAL_BYTES:
        size = bundles._mb(judged.size + ref_bytes)
        limit = bundles._mb(bundles.MAX_TOTAL_BYTES)
        judged.problems.append(bundles.problem("/", "total_size", f"{OVER} {size}, at most {limit}."))


def chosen(samples: tuple[str, ...] | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """The samples a gallery builds, in SAMPLES' order: the ones named, else REVIEWED. ValueError for a name no sample
    has."""
    picked = samples or REVIEWED
    known = {name for name, _styles in SAMPLES}
    for name in picked:
        if name not in known:
            raise ValueError(f"no sample called {name!r}; use {', '.join(sorted(known))}")
    return [(name, styles) for name, styles in SAMPLES if name in picked]


def names(samples: tuple[str, ...] | None = None) -> list[str]:
    return [f"{sample}-{style}" for sample, styles in chosen(samples) for style in styles]


def _kept(conn: psycopg.Connection, token: str) -> tuple[dict[str, bytes], str | None]:
    """A preview's three screenshots as the render check kept them (none without a render check) and its `site_spec`
    row, read at once: the next build ends the preview."""
    row = conn.execute(
        "SELECT shot_phone, shot_tablet, shot_desktop, sections FROM page_preview WHERE token = %s", (token,)
    ).fetchone()
    if row is None:
        return {}, None
    shots = {size: bytes(row[f"shot_{size}"]) for size, _label in SIZES if row[f"shot_{size}"] is not None}
    return shots, (row["sections"] or {}).get("site_spec_id")


def build_six(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    *,
    ttl: timedelta,
    on_built: Callable[[str, site_build.Built], None] | None = None,
    samples_named: tuple[str, ...] | None = None,
) -> list[tuple[str, spec_mod.SiteSpec, site_build.Built, dict[str, bytes], str | None]]:
    """The six full builds, one after another (each ends the one before, P35); each one's screenshots and `site_spec`
    row read at once.
    The first fault stops the run: TemplateFault, `render_check.Unavailable` or a ToolError, as `build.preview`."""
    out = []
    for sample, styles in chosen(samples_named):
        for style in styles:
            name = f"{sample}-{style}"
            site = samples.load(sample, style=style)
            built = site_build.preview(conn, settings, biz, site, sample=sample, ttl=ttl)
            if on_built:
                on_built(name, built)
            out.append((name, site, built, *_kept(conn, built.token)))
    return out


def build(
    conn: psycopg.Connection,
    settings: config.Settings,
    biz: str,
    *,
    ttl: timedelta = hosted.PREVIEW_TTL,
    on_built: Callable[[str, site_build.Built], None] | None = None,
    samples_named: tuple[str, ...] | None = None,
) -> site_build.Built:
    """The six (`build_six`, `on_built` called after each), then the index bundle kept as the business's preview.
    `conn` is autocommit, as `build.preview` needs. Its `sections` say `kind: site` (so `build.publish_gate` refuses
    it too), `gallery` the six names in order and `sites` each one's `site_spec` row."""
    if not (conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE):
        raise ValueError(
            "gallery.build needs an autocommit connection outside a transaction: each build's render check reads what it keeps"
        )
    row = business.row(conn, biz)
    if row is None or row["status"] != "active":
        raise ToolError(site_upload.NOT_ACTIVE)
    if not settings.pages_domain:
        raise ToolError(site_upload.HOSTING_OFF)
    six = build_six(conn, settings, biz, ttl=ttl, on_built=on_built, samples_named=samples_named)

    page = landing.ensure_page_for_kit(conn, biz)
    page_id, own_host, agent_base = page["id"], page.get("hostname"), settings.public_base_url
    snippet = landing.snippet(page_id, agent_base, landing.google_tag(conn, biz))
    whole = render.Rendered()
    looks = []
    for name, spec, _built, shots, _spec_id in six:
        site = spec_mod.validate(spec)
        filled = images.fill(conn, site, business_id=biz, page_id=page_id)
        one = render.bundle(
            site,
            page_id=page_id,
            agent_base=agent_base,
            snippet=snippet,
            hostname=own_host,
            images=filled,
            base=f"/{name}",
        )
        whole.files.update({f"{name}/{p}": data for p, data in one.files.items()})
        whole.refs.update({f"{name}/{p}": sha for p, sha in one.refs.items()})
        whole.pages.extend(f"{name}/{p}" for p in one.pages)
        whole.files.update({f"shots/{name}-{size}.jpg": data for size, data in shots.items()})
        style_title = name.split("-", 1)[1].capitalize()
        looks.append(
            {
                "name": name,
                "title": f"{site.facts.name} in {style_title}",
                "line": LINES[name],
                "shots": [(size, label) for size, label in SIZES if size in shots],
            }
        )
    index = env.get_template("sites/gallery.html").render(looks=looks, snippet=snippet)
    whole.files = {"index.html": index.encode("utf-8"), **whole.files}
    whole.pages.insert(0, "index.html")

    distinct = list(set(whole.refs.values()))
    ref_bytes = sum(
        r["size"] for r in conn.execute("SELECT size FROM site_blob WHERE sha256 = ANY(%s)", (distinct,)).fetchall()
    )
    judged = bundles.Judged(files=whole.files)
    bundles.check(judged, page_id=page_id, own_host=own_host, agent_base=agent_base, snippet=snippet)
    if not judged.ok:
        raise site_build.TemplateFault(judged.problems)
    _limits(whole, judged, ref_bytes=ref_bytes)
    if not judged.ok:
        raise OverLimit(judged.problems)

    sections = {
        "kind": "site",
        "gallery": [name for name, *_rest in six],
        "sites": {name: spec_id for name, *_rest, spec_id in six},
        "site_spec_id": None,
        "sample": "gallery",
        "render": None,
        "pagespeed": None,
    }
    with site_upload._page_lock(page_id), conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"bundle:{page_id}",))
        stored = bundles.store(conn, judged.files)
        bundles.touch(conn, whole.refs)
        token = bundles.save_preview(conn, page_id, judged, {**stored, **whole.refs}, ttl=ttl, sections=sections)
        host = bundles.preview_host(conn, page_id, settings.pages_domain)
        expires = hosted.preview(conn, token)["expires_at"]
    return site_build.Built(
        token=token,
        preview_url=f"https://{host}/",
        expires_at=iso(expires),
        files=len(judged.files) + len(set(whole.refs.values())),
        bytes=judged.size + ref_bytes,
    )
