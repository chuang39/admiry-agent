"""Admiry's own website for a home-services business or a dental practice (A3p-6 batch 2 Q5, Q6, Q10, Q12, Q21, Q22;
spec T26): `build_site` asks which look the owner wants -- the three samples' screenshots (`sites/looks.py`) --, then
the facts Admiry still needs, one at a time (the spec's §2 flow: pick a look, then ask only what is missing), and then
builds the site into the business's bundle preview (`sites/build.py`) and shows it with its photo checklist and the
phone link the owner adds the logo and the photos with; `get_site` says where the site stands (`site_view`);
`get_site_templates` shows the looks. The family and the trade are the business's knowledge's
(`sites/intake.family_for`): any other business keeps the one-page page `create_page` writes. With AGENT_SITES off the
three answer SITES_OFF before anything else (Q22).

`build_site`'s work is `prepare_build`, on `run`'s autocommit connection outside its transaction, as `create_page`'s
model call is: the facts written (`intake.merge`), the owner's choices kept on the draft (`intake.update_draft`), the
model's words, and the build, which commits its preview before the render check opens it. `answer` writes the envelope
inside the transaction. The model writes the words only when none it wrote stand for what it reads now
(`words.fingerprint`): a new look, a colour, the wordmark, a photo or a new phone number builds without it, and so does
a retry after a build that failed once the words were written (`words_pending`, fix round 1). Only a build that writes
words asks for the day's limit, and only such a call counts toward it (its audit says `model`). `build_site` is one of
`auth.LLM_TOOLS`: `run` holds the business's lock while it builds, one site at a time. A business whose page is the
starter page Admiry wrote keeps it (STARTER_PAGE, fix round 1).

Task 5: `create_page` -- the `write` action, the fourth way's tool, the state's page step -- hands a family business with
no starter page yet to the same `prepare_build` and `answer` (`takes_over`, Q14), and the call that builds the site for
the first time writes its owner one e-mail once it has committed (`BuildCall.after`, Q13; the draft's `mailed_ready`).

Task 6: an assistant reaches them through the public surface (`surface/supporting.py`, Q15): `build_site` is
`update_landing_page`'s action of that name, `get_site` is `get_landing_page`'s view `site` (`surface/actions.py` says so
in a `next`), and `get_site_templates` is published as it is."""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass

import psycopg
from fastmcp.exceptions import ToolError

from . import (
    auth,
    business,
    config,
    db,
    envelope,
    hosted,
    hosting_minimum,
    landing,
    mail,
    page_builder,
    plans,
    render_check,
    site_upload,
    uploads,
)
from .mcp_app import agent_tool, need_business, run
from .models import SiteWords
from .sites import build, families, intake, looks, photos, words
from .sites import spec as spec_mod
from .sites.spec import SiteSpec
from .util import iso

log = logging.getLogger(__name__)

# Q12's words, verbatim (the founder approves them with the plan)
SITES_OFF = "Admiry's own sites are not on for this server yet."
NOT_A_FAMILY = "Admiry builds sites for home services and dental practices; for this business it writes a page instead."
PICK_A_LOOK = "Pick a look. You can change words, colours and photos after."
BUILT = "Your site is ready to look over: {preview_url} (the link works for 24 hours)."
LOOK_CHANGED = "Here is your site in the {name} look: {preview_url}."
PHOTOS = "Photos make your site yours — add your logo and photos from a phone: {upload_url}."
NO_PHOTOS_YET = (
    "No logo or photos yet? Your assistant (Muse, for example) can make your logo and help with your photos."
)
# the build's offer of the industry's own facts, once (Q4's extras)
EXTRAS_OFFER = {
    "home_services": "Want to add your licence, years in business, a warranty or a review? Say them and I'll add them.",
    "dental": "Want to add the dentist's name, the insurance you take or a review? Say them and I'll add them.",
}
WORDMARK_OK = "Your business name stands as the logo, in the look you picked."
# the e-mail of the site's first build (Q13): its subject is Q12's; its words are the template's
READY_SUBJECT = "Your site is ready to look over"
READY_TEMPLATE = "mail_site_ready.txt.j2"
BUILD_FAILED = "Admiry couldn't build your site just now. Try again in a few minutes."
EXPIRED = "Your preview expired; build it again."
# where the site stands (get_site)
STATE_NONE = "Admiry can build a site for this business; pick a look."
STATE_PREVIEW = "Your site is ready to look over: {url}."
STATE_LIVE = "Your site is live at {url}."
TEMPLATES = "Admiry builds a site in one of these looks; pick one and Admiry fills it with the business's own facts."
# fix round 1's, for the founder's list: a look picked and no site built yet; a business whose ads use the starter page
NOT_BUILT = "Your look is picked; build the site to see it."
STARTER_PAGE = "Your ads already use the page Admiry wrote. A site in its place comes with the next release."
# this task's own: a look the family has not, the photo link's label, the families' names
STYLE_UNKNOWN = "There is no look called “{style}” for this business; use {styles}."
UPLOAD_LABEL = "Add your logo and photos"
FAMILY_NAMES = {"home_services": "Home services", "dental": "Dental"}

# A build made from the homepage's site card (Task 7, Q17): the card asks the industry's own facts on its own, so the
# build offers none in words and leaves `extras_asked` to the card's Skip or answer (`on_the_card`)
_ON_THE_CARD: ContextVar[bool] = ContextVar("sites_on_the_card", default=False)


@contextlib.contextmanager
def on_the_card():
    """Wraps a `build_site` call made from the homepage's site card (home_api's /api/site/build), as
    `hosted.with_embed_origin` wraps the page card's: `answer` says no offer of the industry's own facts and marks none
    asked -- the card shows them until the owner gives or skips them."""
    token = _ON_THE_CARD.set(True)
    try:
        yield
    finally:
        _ON_THE_CARD.reset(token)


# What an assistant (or the homepage) does with a site whose logo and key photo are in: publish it, or try another look
NEXT_SITE = envelope.actions(
    {"id": "publish", "label": "Publish your site", "tool": "publish_page", "primary": True},
    {"id": "look", "label": "Change the look", "tool": "build_site"},
)


@dataclass(frozen=True)
class Prepared:
    """What `prepare_build` came to (Q6), for `answer`: `step` `pick` (no look yet: the look is asked before any fact; the
    facts still `missing` come after it), `ask` (a look picked, the required facts still `missing`, the first one asked),
    with the question's `index` and `count` (`_numbered`) -- or `built`: the build, its document, whether it is the first
    build of the site that passed (fix round 1: a failed one showed the owner nothing), whether the look changed from the
    one the site was last built in, and whether the owner accepted the wordmark in this call."""

    step: str
    family: str
    page_id: str
    missing: tuple[str, ...] = ()
    index: int = 1  # a question's place among the questions of this intake (`_numbered`), and how many there are
    count: int = 1
    built: build.Built | None = None
    site: SiteSpec | None = None
    first: bool = False
    look_changed: bool = False
    wordmark: bool = False


# ---------------------------------------------------------------- the pieces of a call


def _family(conn: psycopg.Connection, biz: str) -> tuple[str, str | None]:
    """The family and the trade the business's knowledge says (Q1), or ToolError NOT_A_FAMILY."""
    latest = business.latest_knowledge(conn, biz)
    found = intake.family_for(latest[1] if latest else None)
    if found is None:
        raise ToolError(NOT_A_FAMILY)
    return found


def _style(family: str, style: str | None) -> str | None:
    """The look a call picks -- by its id or its name, in any case ("Bold") -- or None for none; ToolError naming the
    family's three for one it has not."""
    if style is None or not style.strip():
        return None
    ids = [look["id"] for look in looks.LOOKS[family]]
    picked = style.strip().lower()
    if picked not in ids:
        named = f"{', '.join(ids[:-1])} or {ids[-1]}"
        raise ToolError(STYLE_UNKNOWN.format(style=style.strip()[:40], styles=named))
    return picked


def _colour(family: str, style: str | None, accent: str | None, kept: str | None) -> str | None:
    """The accent the draft keeps (spec.validate's rule, `spec.accent_for`): the one the call gives, checked against the
    look in force (ToolError in its own words), none for an empty one (the look's own colour); else the one kept, while
    the button's words still read on it in the look in force -- one picked for another look that would not read here
    gives way to this look's own, rather than holding every build of it back."""
    if accent is not None:
        return spec_mod.accent_for(family, style, accent.strip()) if accent.strip() else None
    if kept is None:
        return None
    try:
        return spec_mod.accent_for(family, style, kept)
    except ToolError:
        return None


def _kept_words(draft: dict, wanted: str) -> SiteWords | None:
    """Words the model already wrote from what it would read now (`wanted`, `words.fingerprint`): the last passing
    build's, else those a build that then failed had written (`words_pending`, fix round 1); None: the model writes
    them again."""
    pending = draft.get("words_pending") or {}
    for found, read in ((draft["words"], draft.get("words_for")), (pending.get("words"), pending.get("for"))):
        if found is None or read != wanted:
            continue
        try:
            return SiteWords.model_validate(found)
        except ValueError:  # words of a shape the site no longer reads: written again
            continue
    return None


def _with_headline(said: SiteWords, site: SiteSpec, headline: str) -> SiteWords:
    """`said` with the home page's headline `headline` (B0's "Fix the headline": the plan's lead service and city, as
    Message-match asks them), checked by the rules every word of the site passes (page_builder.text_notes over the
    site's facts, its length) -- ToolError with its notes when it breaks one, and nothing built."""
    rules = page_builder.rules_for(words._kit(site, families.get(site.family)), {})
    notes = page_builder.text_notes("The home page's headline", headline, words._limit(said.home, "headline"), rules)
    if notes:
        raise ToolError(words.REFUSED + " ".join(notes))
    return said.model_copy(update={"home": said.home.model_copy(update={"headline": headline})})


def _key_label(family: str) -> str:
    """The label of the family's key photo (Q7): what the site needs most after the logo."""
    return next(kind["label"] for kind in photos.KINDS[family] if kind["id"] == "key")


def _sections(conn: psycopg.Connection, page_id: str) -> dict:
    row = conn.execute("SELECT sections FROM page WHERE id = %s", (page_id,)).fetchone()
    return (row or {}).get("sections") or {}


def _live_preview(conn: psycopg.Connection, page_id: str) -> dict | None:
    """The business's live site preview (Q10): its newest bundle preview whose sections say `kind: site` -- never the
    gallery of looks -- with its address on the business's preview host, its token, its end and what the render check
    opened (None when it did not run); None once it has ended. The current site is read from here, never from
    `page.site_spec_id` (batch 1's note, Q19 B)."""
    row = conn.execute(
        "SELECT v.token, v.expires_at, v.sections, p.preview_host FROM page_preview v JOIN page p ON p.id = v.page_id "
        "WHERE v.page_id = %s AND v.kind = 'bundle' AND v.expires_at > now() AND v.sections->>'kind' = 'site' "
        "AND NOT (v.sections ? 'gallery') ORDER BY v.created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    if row is None:
        return None
    rendered = (row["sections"] or {}).get("render")
    return {
        "url": f"https://{row['preview_host']}/",
        "token": row["token"],
        "expires_at": iso(row["expires_at"]),
        "render": {"loads": rendered["loads"], "partial": rendered["partial"]} if isinstance(rendered, dict) else None,
    }


def pending_preview(conn: psycopg.Connection, page_id: str, headline: str) -> dict | None:
    """Ruling I3a (follow-up 2026-10-12): launch_check.fix_for's site branch reads this before offering fix_headline
    again, the way landing.pending_preview does for the starter page -- but a site's own preview (kind 'bundle',
    `_live_preview`) carries no headline of its own to compare (unlike a starter page's `sections.copy.headline`),
    so this reads the site's last build that passed instead (`sites.intake.draft`'s "words": built again only once
    what the model reads changes, so "words" stays this build's until the next one passes). {"token"} once that
    build's own home headline already is `headline` and its preview has not ended; None otherwise (nothing built
    yet for this headline, or the preview since ended): offering publish for a preview that never got the fix would
    make the wrong page live."""
    page = landing.get(conn, page_id)
    words = (intake.draft(page)["words"] or {}).get("home") or {}
    if words.get("headline") != headline:
        return None
    preview = _live_preview(conn, page_id)
    return {"token": preview["token"]} if preview else None


# ---------------------------------------------------------------- where the site stands (Q10)


def site_view(conn: psycopg.Connection, settings: config.Settings, biz: str) -> dict:
    """Where the business's site stands (Q10): its family and trade, `status` -- `none` before its first build,
    `preview` once built (`preview` None once that preview has ended), `published` once it is the live page -- the look
    and the colour picked, the looks, the draft's own facts (never the profile's: the profile shows those), the required
    facts still missing, whether the extras were offered, the live preview, the photo checklist and whether the logo
    and the key photo are in (`key_photos`), the upload link while one is valid, the live site (`published`), the
    hosting minimum's status and the note above publishing, and the Muse invitation once it is live. Reads only: no
    page row is made. ToolError NOT_A_FAMILY for a business of no family. A business whose page is the starter page
    Admiry wrote has no site (`status` none, fix round 1), and a look the draft keeps from another family is none."""
    from .home_api import MUSE_INVITE  # the homepage's own words; read when asked, no import cycle forms

    family, trade = _family(conn, biz)
    page = landing.current(conn, biz)
    page_id = page["id"] if page else None
    draft = intake.draft(page)
    _doc, missing = intake.facts(conn, biz, page)
    live = page is not None and page["status"] == "published" and _sections(conn, page_id).get("kind") == "site"
    if page_builder.current(conn, biz) is not None:  # the starter page Admiry wrote is the business's page
        status = "none"
    else:
        status = "published" if live else "preview" if draft["words"] is not None else "none"
    published = None
    if live:
        published = {
            "url": page["url"],
            "hostname": page["hostname"],
            "version": page["version"],
            "check": landing.saved_report(page),
        }
    return {
        "family": family,
        "trade": trade,
        "status": status,
        "style": looks.own_style(family, draft["style"]),
        "accent": draft["accent"],
        "looks": looks.looks(settings, family, trade),  # pictured by the trade's own sample when it has one
        "facts": draft["facts"],
        "missing": missing,
        "extras_asked": draft["extras_asked"],
        "preview": _live_preview(conn, page_id) if page_id else None,
        "checklist": photos.checklist(conn, page_id, family, draft),
        "key_photos": photos.key_photos(conn, page_id, family, draft),
        "upload_url": uploads.latest_upload_url(conn, page_id, settings.web_base_url) if page_id else None,
        "published": published,
        "hosting": hosting_minimum.status(conn, biz, page),
        "hosting_note": hosting_minimum.publish_note(conn, biz, page),
        "muse": MUSE_INVITE if live else None,
    }


def state_say(conn: psycopg.Connection, view: dict) -> str:
    """`get_site`'s sentence for where the site stands (Q12): live at its address; ready to look over, with what still
    keeps it from going live (`build.publish_gate`: the logo and the key photo, or a build since they came in); its
    preview ended; not built yet, its look picked (fix round 1) or not. The page step says it too while the site is
    under way (launch_check.page_in_hand)."""
    if view["status"] == "published":
        return STATE_LIVE.format(url=view["published"]["url"])
    if view["status"] == "none":
        return NOT_BUILT if view["style"] else STATE_NONE
    if view["preview"] is None:
        return EXPIRED
    said = STATE_PREVIEW.format(url=view["preview"]["url"])
    held = build.publish_gate(conn, hosted.preview(conn, view["preview"]["token"]))
    return f"{said} {held}" if held else said


# ---------------------------------------------------------------- the build (Q6)


def _numbered(conn: psycopg.Connection, page_id: str, draft: dict, question: str, after: int) -> tuple[int, int]:
    """Where a question stands among those of this intake (Task 6 fix round 1): the draft keeps how many questions were
    asked so far -- the look, then each fact -- and which one last, so the question asked now is number `asked + 1` and
    the count is its number plus the `after` facts still missing after it: "1 of 5", "2 of 5", ... and a call that
    answered two questions at once shortens the count ("3 of 4"). The same question asked again -- a call sent twice, an
    answer that said nothing new -- keeps the number it had. `prepare_build` starts the count again once a site builds."""
    asked = draft["asked"]
    if draft["asked_last"] != question:
        asked += 1
        intake.update_draft(conn, page_id, {"asked": asked, "asked_last": question})
    return asked, asked + after


def prepare_build(
    conn: psycopg.Connection,
    p: auth.Principal,
    *,
    style: str | None = None,
    accent: str | None = None,
    facts: dict | None = None,
    wordmark: bool | None = None,
    audit_args: dict | None = None,
    headline: str | None = None,
) -> Prepared:
    """`build_site`'s work, outside `run`'s transaction (Q6). Everything a call says is checked before anything is
    written (the starter page Admiry wrote, a bad patch, a look the family has not, a colour that does not read: one
    sentence, nothing changed); then the page row, the facts (`intake.merge`: the profile's on the profile, the rest on
    the draft), and the look, the colour and the wordmark on the draft. With no look (or one of another family) it
    answers `pick`, whatever is missing; with a look and a required fact missing, `ask`; else it builds -- words the
    model already wrote from what it would read now, else the model's, kept on the draft before the build (the day's
    limit asked first, and `model` set in `audit_args`, the call's audit arguments, so the day counts the call) -- with
    the owner's photos and the checklist, the render check and the speed test, and keeps the words as the site's once
    the build passed. A check the build fails is Admiry's template's fault, logged at ERROR with its problems and
    answered BUILD_FAILED, as is a renderer that does not answer.
    `headline` (B0's "Fix the headline", `fix_headline`): the home page's headline replaced by it once the words are
    known, every other word as the model's rule leaves it, and kept on the draft with them."""
    settings = config.load()
    if not settings.sites:
        raise ToolError(SITES_OFF)
    biz = need_business(p)
    if not settings.pages_domain:
        raise ToolError(site_upload.HOSTING_OFF)
    family, trade = _family(conn, biz)
    if page_builder.current(conn, biz) is not None:  # the starter page and its photos and link stay as they are
        raise ToolError(STARTER_PAGE)
    # one sentence naming the key of a bad fact, before anything is written (the answer to the place question included)
    patch = intake.check(facts)
    picked = _style(family, style)
    before = intake.draft(landing.current(conn, biz))
    kept_style = looks.own_style(family, before["style"])
    in_force = picked or kept_style
    colour = _colour(family, in_force, accent, before["accent"])

    page = landing.ensure_page_for_kit(conn, biz)
    if patch:
        intake.merge(conn, biz, page, patch)
    changes: dict = {"family": family, "trade": trade, "style": in_force, "accent": colour}
    if wordmark is not None:
        changes["wordmark_ok"] = bool(wordmark)
    draft = intake.update_draft(conn, page["id"], changes)
    page = landing.get(conn, page["id"])
    doc, missing = intake.facts(conn, biz, page)
    if draft["style"] is None:  # the look comes first (the spec's §2 flow); the facts still missing are asked after it
        index, count = _numbered(conn, page["id"], draft, "style", len(missing))
        return Prepared("pick", family, page["id"], missing=tuple(missing), index=index, count=count)
    if missing:
        index, count = _numbered(conn, page["id"], draft, missing[0], len(missing) - 1)
        return Prepared("ask", family, page["id"], missing=tuple(missing), index=index, count=count)

    site = SiteSpec(family=family, style=draft["style"], trade=trade, accent=draft["accent"], facts=doc)
    wanted = words.fingerprint(site)
    kept = _kept_words(draft, wanted)
    if kept is None:
        auth.check_llm_cap(conn, p)  # only a build that writes words spends a model call
        if audit_args is not None:
            audit_args["model"] = True  # and only such a call counts toward the day (auth.check_llm_cap)
        kept = words.write(conn, settings, site)
        pending = {"words": kept.model_dump(mode="json"), "for": wanted}
        intake.update_draft(conn, page["id"], {"words_pending": pending})  # paid for: a retry pays nothing
    if headline is not None:  # B0: the plan's headline on the home page; the other words stay the draft's
        kept = _with_headline(kept, site, headline)
    checklist = {
        "items": photos.checklist(conn, page["id"], family, draft),
        "key_photos_ok": photos.key_photos(conn, page["id"], family, draft)["ok"],
        "wordmark_ok": bool(draft["wordmark_ok"]),
    }
    site = site.model_copy(
        update={"words": kept, "images": photos.images(conn, page["id"], family), "checklist": checklist}
    )
    try:
        built = build.preview(conn, settings, biz, site, checks="full")
    except build.TemplateFault as e:
        log.exception(
            "site build: business %s, %s in %s, failed a check (problems: %s)", biz, family, site.style, e.problems
        )
        raise ToolError(BUILD_FAILED) from e
    except render_check.Unavailable as e:
        log.exception("site build: business %s, %s in %s: the renderer did not answer", biz, family, site.style)
        raise ToolError(BUILD_FAILED) from e
    passed = {"words": site.words.model_dump(mode="json"), "words_for": wanted, "words_pending": None}
    passed |= {"asked": 0, "asked_last": None}  # the intake is over: the next one is numbered from the start
    intake.update_draft(conn, page["id"], passed)  # the site's words now: a draft with them was built
    return Prepared(
        "built",
        family,
        page["id"],
        built=built,
        site=site,
        first=before["words"] is None,  # no build passed before this one (a purge clears them too)
        look_changed=before["words"] is not None and picked is not None and picked != kept_style,
        wordmark=wordmark is True,
    )


def _built_say(ctx: Prepared, upload_url: str, key_photos: dict, offer: bool) -> str:
    """What a build says (Q12): the preview's address (the look's name when the look changed); on the page's first
    site the hosting minimum; the wordmark accepted; while the logo or the key photo is missing the photos line and
    T14's; and the industry's own facts offered, once."""
    built = ctx.built
    if ctx.look_changed:
        said = [LOOK_CHANGED.format(name=looks.name(ctx.family, ctx.site.style), preview_url=built.preview_url)]
    else:
        said = [BUILT.format(preview_url=built.preview_url)]
    if ctx.first:
        said.append(hosting_minimum.MINIMUM)
    if ctx.wordmark:
        said.append(WORDMARK_OK)
    if not key_photos["ok"]:
        said += [PHOTOS.format(upload_url=upload_url), NO_PHOTOS_YET]
    if offer:
        said.append(EXTRAS_OFFER[ctx.family])
    return " ".join(said)


def answer(conn: psycopg.Connection, p: auth.Principal, ctx: Prepared) -> dict:
    """`build_site`'s envelope, inside `run`'s transaction (Q6): the look as a question over the three, the first missing
    fact as a question (Q4: the answer comes back as `facts={<its id>: ...}`) -- each told its place among the questions
    of this intake, "2 of 5" (`_numbered`) --, or the site -- `site_view` with the preview's address and token at the top
    and the upload link (minted when none is valid), the build's sentence, and next the link for the photos while the
    logo or the key photo is missing, else publishing (`NEXT_SITE`)."""
    settings = config.load()
    biz = need_business(p)
    if ctx.step == "ask":
        fact = ctx.missing[0]
        prompt = intake.QUESTIONS[fact]
        nxt = envelope.question(
            id=fact, index=ctx.index, count=ctx.count, prompt=prompt, input="text", options=[], other=True
        )
        data = {"missing": list(ctx.missing), "site": site_view(conn, settings, biz)}
        return envelope.make(data, say=prompt, next=nxt)
    if ctx.step == "pick":
        shown = looks.looks(settings, ctx.family, _family(conn, biz)[1])
        options = [{"id": look["id"], "label": look["name"], "sublabel": look["line"]} for look in shown]
        nxt = envelope.question(
            id="style",
            index=ctx.index,
            count=ctx.count,
            prompt=PICK_A_LOOK,
            input="single",
            options=options,
            other=False,
        )
        return envelope.make({"looks": shown, "site": site_view(conn, settings, biz)}, say=PICK_A_LOOK, next=nxt)
    upload_url = uploads.latest_upload_url(conn, ctx.page_id, settings.web_base_url)
    if upload_url is None:
        upload_url = f"{settings.web_base_url}/u/{uploads.new_token(conn, ctx.page_id)}"
    offer = not _ON_THE_CARD.get() and not intake.draft(landing.get(conn, ctx.page_id))["extras_asked"]
    if offer:  # offered once: an assistant sends them as facts, the homepage's card asks them on its own
        intake.update_draft(conn, ctx.page_id, {"extras_asked": True})
    view = site_view(conn, settings, biz)
    data = {"preview_url": ctx.built.preview_url, "token": ctx.built.token, **view, "upload_url": upload_url}
    say = _built_say(ctx, upload_url, view["key_photos"], offer)
    nxt = NEXT_SITE if view["key_photos"]["ok"] else envelope.link(upload_url, UPLOAD_LABEL)
    return envelope.make(data, say=say, next=nxt)


# ---------------------------------------------------------------- one call that may build (Q6, Q13, Q14)


def takes_over(conn: psycopg.Connection, biz: str) -> bool:
    """Whether `create_page` -- the `write` action, the fourth way's tool, the state's page step -- hands the business to
    the site (Q14): AGENT_SITES is on (Q22), the knowledge says a family (`intake.family_for`) and no starter page Admiry
    wrote is the business's page already. Decided from what is on file alone: no model, no page row written, no limit
    looked at."""
    if not config.load().sites:
        return False
    latest = business.latest_knowledge(conn, biz)
    return intake.family_for(latest[1] if latest else None) is not None and page_builder.current(conn, biz) is None


class BuildCall:
    """One call of a command that may build the site -- `build_site`, and `create_page` for a family business (Q14): its
    `prepare` is `prepare_build` (it remembers the caller's business and what the build came to), its `after` is what
    follows once the call has committed (`run` holds no connection then): the e-mail of the site's first build (Q13).
    One object so the two commands share the one e-mail rule."""

    def __init__(self, **call) -> None:
        self.call = call  # prepare_build's arguments
        self.biz: str | None = None
        self.prepared: Prepared | None = None

    def prepare(self, conn: psycopg.Connection, p: auth.Principal) -> Prepared:
        self.biz = p.business_id
        self.prepared = prepare_build(conn, p, **self.call)
        return self.prepared

    def after(self, result: dict) -> None:
        """The site's first build, written to the owner once (Q13): the preview's address, the hosting minimum, the photo
        checklist and the phone link to add them with (`READY_TEMPLATE`). It goes when this call built the site and the
        draft says no such e-mail was sent yet (`mailed_ready`); the flag is set only once it was sent -- no address on
        file, no mail on this server or a refusal send nothing and set nothing, so the next build tries again -- and
        never twice. A site that is live already (built before the letter existed: its draft has no words and no flag, so
        the build reads as the first) is sent none: the letter says to publish once the photos are in (Task 6). Nothing
        here fails the call (the build is committed): `run` logs what raises."""
        done = self.prepared
        if done is None or done.step != "built":
            return
        data = result["data"]
        with db.connect(config.load().database_url) as conn:
            page = landing.get(conn, done.page_id)
            live = page is not None and page["status"] == "published"
            live = live and _sections(conn, done.page_id).get("kind") == "site"
            if live or intake.draft(page)["mailed_ready"]:
                return
            latest = business.latest_knowledge(conn, self.biz)
            sent = mail.to_owner(
                conn,
                self.biz,
                READY_TEMPLATE,
                READY_SUBJECT,
                name=plans.name_for(latest[1] if latest else None) or "Your business",
                preview_url=data["preview_url"],
                minimum=hosting_minimum.MINIMUM,
                checklist=data["checklist"],
                upload_url=data["upload_url"],
                assistant_line=NO_PHOTOS_YET,
            )
            if sent == "sent":
                intake.update_draft(conn, done.page_id, {"mailed_ready": True})


# ---------------------------------------------------------------- the commands


@agent_tool(cls="W", scope="manage")
def build_site(
    style: str | None = None, accent: str | None = None, facts: dict | None = None, wordmark: bool | None = None
) -> dict:
    """Admiry builds the business's website itself -- for a home-services business (a roofer, a plumber, heating and
    cooling, an electrician) or a dental practice: a site of several pages (home, about, services, contact, a page for
    each main service) in one of three looks, filled with the business's own facts. Call it with no arguments to start:
    the answer asks which look the owner wants (style: the look's id, get_site_templates shows the three with a picture
    of each), then the facts Admiry still needs, one at a time (answer with facts={<the question's id>: <the owner's
    words>}), then builds the site: a private preview link (24 hours), its token for publish_page, and a link
    (upload_url) where the owner adds the logo and the photos from a phone, each told what it shows -- photos reach
    Admiry only there.
    facts may also carry what the industry's own visitors look for, shown only when given (licence, years, insured,
    warranty, reviews, the dentist, new_patients, insurance_plans, and more: hours, about, email, social); accent is a
    #RRGGBB colour for the buttons; wordmark true when the owner accepts their business name as the logo. Publishing
    waits for the logo (or the wordmark) and one real photo of the business. Building again with only a new look or
    colour reuses the site's words; a changed fact has Admiry write them again. A business of another kind is told
    Admiry writes it a one-page page instead."""
    sent = sorted(str(key) for key in facts) if isinstance(facts, dict) else None  # the keys, never what they say
    args = {"style": style, "accent": accent, "facts": sent, "wordmark": wordmark}  # `model` added once it writes

    work = BuildCall(style=style, accent=accent, facts=facts, wordmark=wordmark, audit_args=args)
    return run("build_site", "manage", args, answer, prepare=work.prepare, after=work.after)


def fix_headline(headline: str) -> dict:
    """B0's "Fix the headline" (spec 2026-10-08 §3.1, §8.2): the business's site built again with `headline` as its home
    page's headline -- the plan's lead service and city -- every other word kept, so no model call unless what the model
    reads changed since its words were written (then build_site's own rule writes them, and counts). build_site's call
    under its own name: its lock, its audit row (`headline` among its arguments), its answer with the preview's token.
    Not a public command: the homepage's message calls it in process (home_api's /api/blocker)."""
    args = {"style": None, "accent": None, "facts": None, "wordmark": None, "headline": True}
    work = BuildCall(headline=headline, audit_args=args)
    return run("build_site", "manage", args, answer, prepare=work.prepare, after=work.after)


@agent_tool(cls="RO", scope="read")
def get_site() -> dict:
    """Where the business's own website (the one build_site makes) stands: status none (not built yet), preview (its
    private link, token and end; once that has ended, build_site makes a new one) or published (its address, version
    and last check). Also the look and the colour picked, the facts the owner gave for the site, the facts still
    needed, the photo checklist (each kind of photo, how many the site uses, how many are in, which keep the site from
    going live), whether the logo and the key photo are in, the phone link to upload them (only for a key that may
    manage), and the hosting minimum. For a home-services business or a dental practice; any other business gets a
    one-page page instead."""

    def body(conn, p):
        settings = config.load()
        if not settings.sites:
            raise ToolError(SITES_OFF)
        biz = need_business(p)
        view = site_view(conn, settings, biz)
        if not p.key.allows("manage"):  # final review I2: the link changes the site's photos -- a manage key's to give
            view["upload_url"] = None
        starter = page_builder.current(conn, biz) is not None  # the starter page Admiry wrote is the business's page
        return envelope.make(view, say=STARTER_PAGE if starter else state_say(conn, view))

    return run("get_site", "read", {}, body)


@agent_tool(cls="RO", scope="read")
def get_site_templates() -> dict:
    """The looks Admiry builds a website in, each with a picture, to show the owner before update_landing_page's
    "build_site". `families`: home_services (roofing, plumbing, heating and cooling, electrical) and dental, each with
    its `id`, `name`, `trades`, the photo it needs most after the logo (`key_photo`) and its three `styles`: a look's
    `id` (the `style` to send), its `name`, one `line` on it and `look_url`, the address of a picture of a sample site
    on a phone. Show the owner the pictures and let them pick. Where Admiry's own sites are not on yet, it says so."""

    def body(conn, p):
        settings = config.load()
        if not settings.sites:
            raise ToolError(SITES_OFF)
        latest = business.latest_knowledge(conn, p.business_id) if p.business_id else None
        own = intake.family_for(latest[1] if latest else None)  # the asker's own trade pictures its family's looks
        shown = [
            {
                "id": family,
                "name": FAMILY_NAMES[family],
                "trades": list(families.get(family).trades),
                "key_photo": _key_label(family),
                "styles": looks.looks(settings, family, own[1] if own and own[0] == family else None),
            }
            for family in looks.LOOKS
        ]
        return envelope.make({"families": shown}, say=TEMPLATES)

    return run("get_site_templates", "read", {}, body)
