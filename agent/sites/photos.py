"""The photo kinds of an industry site, the slots they fill and the checklist (A3p-6 batch 2 Q7, Q8, Q11; spec T11-T13).

The owner says what the photos they upload show (`page_photo.kind`); the kinds are the family's checklist in order of
value (T12), each with how many the site uses. `images` names the owner's photos by the slot of the site each goes to
-- for `images.fill` to resolve -- and lists a page's own photos only, and none that has no kind (a starter page's
photos keep none: nothing says what they show, so nothing places them). `checklist` says what is on file and what is
still to upload, and `key_photos` whether the site may go live: a logo (or the wordmark the owner accepted for one) and
the family's key photo (T13), in the plan's own sentences (`GATE`). `rebuild` is what the upload link does once the
photos of a site have changed (Q7): the site's preview built again from what is on file."""

from __future__ import annotations

import logging

import psycopg

from .. import config, db, landing
from ..models import SiteWords
from . import families, intake, looks, words
from .spec import ImageRef, SiteSpec

log = logging.getLogger(__name__)

# What each family asks for, in the checklist's order (T12): `need` is how many photos of that kind the site uses
KINDS: dict[str, tuple[dict, ...]] = {
    "home_services": (
        {"id": "logo", "label": "Your logo", "need": 1},
        {"id": "key", "label": "One photo of your own work, your team or your truck", "need": 1},
        {"id": "place", "label": "Your shop or yard", "need": 1},
        {"id": "team", "label": "You and your team", "need": 1},
        {"id": "work", "label": "More of your work", "need": 3},
        {"id": "before_after", "label": "Before and after", "need": 2},
    ),
    "dental": (
        {"id": "logo", "label": "Your logo", "need": 1},
        {"id": "key", "label": "Your office, outside or in", "need": 1},
        {"id": "place", "label": "The waiting room or a treatment room", "need": 2},
        {"id": "team", "label": "You and your team", "need": 1},
        {"id": "dentist", "label": "The dentist", "need": 1},
    ),
}
# Every kind there is, once, in the order they first appear (what the public surface lists as `photo_kinds`)
KIND_IDS: tuple[str, ...] = tuple(dict.fromkeys(kind["id"] for kinds in KINDS.values() for kind in kinds))
# The most photos a site's upload link keeps: a family's needs summed
MAX_SITE_PHOTOS: dict[str, int] = {family: sum(kind["need"] for kind in kinds) for family, kinds in KINDS.items()}
# The kinds without which a site does not go live (T13): the logo, or the owner's accepted wordmark, and the key photo
BLOCKING = ("logo", "key")

_GALLERY = ("gallery-1", "gallery-2", "gallery-3")
# The slots of the site a kind's photos go to, in the order they were uploaded; the photos past the last slot stay
# unplaced. The key photo is the family's `key_photo_slot`; work and before-and-after share the home page's gallery.
SLOTS: dict[str, dict[str, tuple[str, ...]]] = {
    "home_services": {
        "logo": ("logo",),
        "key": ("hero",),
        "place": ("about", "contact"),
        "team": ("team",),
        "work": _GALLERY,
        "before_after": _GALLERY,
    },
    "dental": {
        "logo": ("logo",),
        "key": ("office-1",),
        "place": ("office-2", "contact"),
        "team": ("team",),
        "dentist": ("dentist",),
    },
}

# What keeps a site from going live (Q11), by family and by what is missing: both, the logo alone, the key photo alone
_LOGO = "Your site needs your logo before it goes live."
GATE: dict[str, dict[str, str]] = {
    "home_services": {
        "both": "Your site needs your logo and one photo of your own work before it goes live.",
        "logo": _LOGO,
        "key": "Your site needs one photo of your own work before it goes live.",
    },
    "dental": {
        "both": "Your site needs your logo and one photo of your office before it goes live.",
        "logo": _LOGO,
        "key": "Your site needs one photo of your office before it goes live.",
    },
}


def is_site(page: dict | None) -> bool:
    """Whether a page row is an industry site's: the owner has said something to it (`site_draft`) or a build has
    written its document (`site_spec_id`). A starter page is neither."""
    row = page or {}
    return row.get("site_draft") is not None or row.get("site_spec_id") is not None


def family_of(page: dict | None) -> str | None:
    """The family of the site a page row is: the draft's, else -- for a site that was published -- the one its
    `sections` name. The fallback reads `sections` only from a row that carries them: a row from `landing`
    (`PAGE_COLS`) does not, so its family is its draft's alone; a caller that selected the page whole has both.
    None for a page that is no site, and for a family that is none of ours."""
    row = page or {}
    draft, sections = row.get("site_draft"), row.get("sections")
    for found in (
        draft.get("family") if isinstance(draft, dict) else None,
        sections.get("family") if isinstance(sections, dict) else None,
    ):
        if isinstance(found, str) and found in families.FAMILIES:
            return found
    return None


def was_built(page: dict | None) -> bool:
    """Whether a page row is a site that was built, so that a change of its photos has a preview to change: the draft
    keeps the words of a build that passed, the page names a family of ours and the draft's look is one of that
    family's own (Task 5's follow-up: a look picked under another family is none). A site never built takes its photos
    into its first build."""
    draft, family = intake.draft(page), family_of(page)
    return draft["words"] is not None and family is not None and looks.own_style(family, draft["style"]) is not None


def _counts(conn: psycopg.Connection, page_id: str) -> dict[str, int]:
    """How many photos of each kind the page has: its own, and only those that say what they show."""
    rows = conn.execute(
        "SELECT kind, count(*) AS n FROM page_photo WHERE page_id = %s AND kind IS NOT NULL GROUP BY kind", (page_id,)
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def images(conn: psycopg.Connection, page_id: str, family: str) -> dict[str, ImageRef]:
    """The owner's photos of `page_id` by slot, as `SiteSpec.images` names them (Q8): `logo`; the key photo in the
    family's key slot; the rest by `SLOTS`, a kind's photos in the order they were uploaded. A photo with no kind and
    a photo of a kind this family does not ask for have no slot, and another page's photos are never read."""
    slots = SLOTS.get(family, {})
    rows = conn.execute(
        "SELECT id, kind FROM page_photo WHERE page_id = %s AND kind = ANY(%s) ORDER BY created_at, id",
        (page_id, list(slots)),
    ).fetchall()
    placed: dict[str, ImageRef] = {}
    used: dict[tuple[str, ...], int] = {}  # the slots two kinds share (the gallery) are filled from the one count
    for r in rows:
        of_kind = slots[r["kind"]]
        at = used.get(of_kind, 0)
        if at < len(of_kind):
            placed[of_kind[at]] = ImageRef(source="owner", id=r["id"], alt="")
            used[of_kind] = at + 1
    return placed


def checklist(conn: psycopg.Connection, page_id: str, family: str, draft: dict | None) -> list[dict]:
    """What the owner is asked to upload, in order of value (T12): each kind with how many are on file, how many the
    site uses, whether it is done and whether the site waits for it. The logo is done too when the owner accepted
    their business name as the wordmark (`wordmark_ok` in the draft)."""
    have = _counts(conn, page_id)
    wordmark = bool((draft or {}).get("wordmark_ok"))
    return [
        {
            "id": kind["id"],
            "label": kind["label"],
            "need": kind["need"],
            "have": have.get(kind["id"], 0),
            "done": have.get(kind["id"], 0) >= kind["need"] or (kind["id"] == "logo" and wordmark),
            "blocks_publish": kind["id"] in BLOCKING,
        }
        for kind in KINDS.get(family, ())
    ]


def key_photos(conn: psycopg.Connection, page_id: str, family: str, draft: dict | None) -> dict:
    """Whether the site may go live (T13): a logo -- or the wordmark the owner accepted -- and the family's key photo.
    `logo` and `key` say what is on file, `wordmark_ok` the owner's acceptance; when not `ok`, `sentence` is Q11's
    words for what is missing (`GATE`). ValueError for a family that is none of ours: that is the caller's mistake,
    not a missing photo."""
    if family not in GATE:
        raise ValueError(f"no site family called {family!r}")
    have = _counts(conn, page_id)
    logo, key = have.get("logo", 0) >= 1, have.get("key", 0) >= 1
    wordmark = bool((draft or {}).get("wordmark_ok"))
    covered = logo or wordmark
    case = None if covered and key else "key" if covered else "logo" if key else "both"
    return {
        "ok": case is None,
        "logo": logo,
        "key": key,
        "wordmark_ok": wordmark,
        "sentence": GATE[family][case] if case else None,
    }


def rebuild(page_id: str) -> str | None:
    """The site's preview built again from what is on file, for a change of its photos (Q7, Q9): the draft's look,
    accent and words -- the model is not asked --, the facts read again (`intake.facts`: a change to the profile
    reaches the site), the photos as they are now and the checklist, through `build.preview(checks="static")`: the
    upload's checks and no render check, so an owner on a phone waits seconds and not a minute. It runs after the
    change has committed, on a connection of its own, and holds no row of the page while it builds. The new preview's
    address; None when nothing was built -- the site was never built and the photos join its first build (`was_built`:
    the draft keeps no words, names no family of ours, or has no look of its family's), a fact the model reads changed
    since the words were written (final review I5: `words.fingerprint` of what it would build is not the draft's
    `words_for`, the rule `build_site` keeps words by -- the old words never stand beside new facts; the next build
    writes them again) -- or the build failed: a template fault, a document that no longer reads, hosting off, the
    business gone, or anything else going wrong in it. A rebuild is best effort, so it never raises (fix round 0): the
    photo is already saved, and an error here would turn that into one. A failure is Admiry's to look at, never the
    owner's to fix (it is logged at ERROR with its traceback, and its problems when a check found them): the photos stay
    on file and the site shows them the next time it is built."""
    from . import build  # it reads this module (`publish_gate`): read when asked, no import cycle forms

    settings = config.load()
    with db.connect(settings.database_url) as conn:
        page = landing.get(conn, page_id)
        if not was_built(page):  # never built, no family of ours, or no look of the family's: nothing to build
            return None
        draft, family = intake.draft(page), family_of(page)
        biz = page["business_id"]
        try:
            site = SiteSpec(
                family=family,
                style=draft["style"],
                trade=draft["trade"],
                accent=draft["accent"],
                facts=intake.facts(conn, biz, page)[0],
                words=SiteWords.model_validate(draft["words"]),
                images=images(conn, page_id, family),
                checklist={
                    "items": checklist(conn, page_id, family, draft),
                    "key_photos_ok": key_photos(conn, page_id, family, draft)["ok"],
                    "wordmark_ok": bool(draft["wordmark_ok"]),
                },
            )
            if words.fingerprint(site) != draft["words_for"]:  # the words were written for other facts: not built
                return None
            return build.preview(conn, settings, biz, site, checks="static").preview_url
        except Exception as e:  # a rebuild is best effort; the photo is saved either way (logged, never raised)
            problems = e.problems if isinstance(e, build.TemplateFault) else None
            log.exception("site rebuild: page %s did not build after a photo change (problems: %s)", page_id, problems)
            return None
