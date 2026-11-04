"""Admiry Pages, the assistant-facing side (A3p-2 Task 3, spec 2026-09-28-admiry-pages-design.md §3, plan H7-H11):
preview_page (a private preview on Admiry's own host), publish_page and unpublish_page (proposals the owner
confirms, since a published address is what the ads use), and versions on get_page (agent/tools_pages.py). Task 5
(§7, H12): create_page, update_page and revise_page -- a page Admiry writes itself (agent/page_builder.py), each
answering with a preview publish_page takes like any other.

Publishing itself -- the sanitiser, the version, page.hostname/url/source/status/html -- is agent/proposals.py's
job (precheck_publish_page/apply_publish_page, precheck_unpublish_page/apply_unpublish_page), run from
confirm_proposal inside its own transaction. The network work that transaction must never hold open -- the
14-rule check against the now-live hosted URL, the report e-mail (R12), pausing the ads once an unpublish has
taken the page down (H23: the write and the pause happen in that order, the write inside the transaction, the
pause after it commits), and handing the ads their new address (ads_run.launch_now, so H9's diff runs right
away rather than waiting for the hourly sweep), channel by channel for every channel whose ads follow the page (M0
Task 9) -- is this module's AFTER_COMMIT, dispatched by
tools_proposals.confirm_proposal once proposals.confirm's own transaction has committed (the same shape it
already uses for approve_plan's own launch_now, P6)."""

from __future__ import annotations

import base64
import logging

import httpx
from fastmcp.exceptions import ToolError

# tools_pages as a module, its helpers read at call time: importing agent.tools_pages first (a test file does)
# loads mcp_app, whose tail imports this module while tools_pages is still half-built -- a from-import of its
# names would fail there.
from . import (
    ads_run,
    auth,
    bundles,
    business,
    config,
    crawl,
    db,
    envelope,
    hosted,
    hosting_minimum,
    landing,
    page_builder,
    plans,
    proposals,
    site_install,
    site_upload,
    tools_pages,
    uploads,
)
from .channels import registry
from .mcp_app import agent_tool, need_business, run
from .sites import photos as site_photos
from .util import iso

log = logging.getLogger(__name__)

HOSTING_OFF = "Hosting is not set up on this server"  # H1, word for word


def _check_transport() -> httpx.BaseTransport | None:
    """The transport `_after_publish_page` fetches the just-published hosted URL through: real network (None)
    in production. A test monkeypatches this name to a MockTransport keyed to that URL -- the same shape
    mail.client and each channel adapter's client are overridden in tests -- rather than landing.check's own `transport` default,
    since this call also passes `resolver=crawl.resolve` read fresh (not landing.check's default, bound once
    at its own module's import time) so a test's `monkeypatch.setattr(crawl, "resolve", ...)` reaches it too."""
    return None


@agent_tool(cls="W", scope="manage")
def preview_page(html: str, title: str | None = None) -> dict:
    """A private preview of a page you write yourself (full HTML), on Admiry's own host: a link only you and
    the owner have, good for 24 hours, never indexed. A page with a problem -- an external script Admiry does
    not know, a form posting elsewhere, too little real text, and the rest of the sanitiser's rules -- is
    refused with every reason at once and no preview; fix the page and call this again. Once it looks right,
    publish_page(token) proposes making it the live page the ads send people to; the owner confirms."""

    def body(conn, p):
        biz = need_business(p)
        settings = config.load()
        page = landing.ensure_page_for_kit(conn, biz)
        problems = hosted.sanitise(
            html, page_id=page["id"], own_host=page.get("hostname"), agent_base=settings.public_base_url
        )
        if problems:
            return envelope.make({"problems": problems}, say="This page cannot be previewed yet: " + " ".join(problems))
        token = hosted.save_preview(conn, page["id"], html, title)
        prev = hosted.preview(conn, token)
        preview_url = f"{settings.public_base_url}/p/{token}"
        return envelope.make(
            {"preview_url": preview_url, "token": token, "expires_at": iso(prev["expires_at"]), "problems": []},
            say=f"Here is the preview: {preview_url}. Look it over, then publish it to make it live.",
        )

    return run("preview_page", "manage", {"title": title, "html_bytes": len(html.encode("utf-8"))}, body)


@agent_tool(cls="W", scope="manage")
def request_site_upload() -> dict:
    """A one-time link to upload a whole site you built -- a Next.js static export, a Vite or Astro build, a Hugo
    site: several pages, or a framework's build -- as one zip of its build output (the files inside out/, dist/ or
    public/, index.html at the top). Good for one upload in the next 30 minutes: run `how` (curl --fail-with-body -T
    site.zip <upload_url>) where site.zip is. Every page must carry `snippet` (the kit's script tag) as a plain
    <script> tag, forms post to `form_action`, and the site keeps within `limits`. The upload answers with every
    problem to fix ({file, rule, message, fix}: fix them, build, ask for a new link and upload again), or with a
    private preview link and a token: publish_page(token) proposes making it the live site; the owner confirms.
    Admiry hosts the built files only: a site that needs a server is hosted elsewhere and named with
    set_landing_page."""

    def body(conn, p):
        biz = need_business(p)
        settings = config.load()
        if not settings.pages_domain:
            raise ToolError(HOSTING_OFF)
        page = landing.ensure_page_for_kit(conn, biz)
        link = site_upload.new_link(conn, biz, page["id"], settings.public_base_url)
        tag = landing.google_tag(conn, biz)
        data = {
            "upload_url": link["upload_url"],
            "expires_at": link["expires_at"],
            "how": link["how"],
            "limits": bundles.LIMITS,
            "page_id": page["id"],
            "snippet": landing.snippet(page["id"], settings.public_base_url, tag),
            "form_action": f"{settings.public_base_url}/t/form/{page['id']}",
        }
        say = (
            "Here is a one-time link to upload the site's files; it works once, for the next 30 minutes. "
            + hosting_minimum.MINIMUM
        )
        return envelope.make(data, say=say)

    return run("request_site_upload", "manage", {}, body)


def _propose_publish(conn, biz: str, *, token: str | None, version: int | None) -> tuple[dict | None, str]:
    """The proposal publish_page creates (H8): (the proposal, the assistant-facing `say`). Shared with the
    homepage's own Publish button (Task 7, home_api.py's /api/page/publish), which confirms this same proposal
    itself right away rather than waiting for the owner's separate approval -- the building-out stays here
    either way, so there is exactly one place that reserves a hostname and writes a publish_page payload.

    H40: publishing is idempotent per preview token. (None, "This page is already published at <url>.")
    once this token is already live -- a confirmed publish_page proposal already named it, or a page_version
    on file is exactly what publishing it now would produce -- and (the still-open proposal, its own summary)
    once an earlier, unconfirmed call already proposed this same token (reused rather than duplicated, the
    way pages.plan_approve reuses open_for_plan). Both checks are scoped to `token`: publish_page(version=n)
    stays the deliberate, repeatable rollback H7 already documents, so a version republish always proposes
    fresh, exactly as before. A caller getting `None` back must not treat this as a fresh "proposed" status --
    there is nothing to confirm."""
    settings = config.load()
    if not settings.pages_domain:
        raise ToolError(HOSTING_OFF)
    if bool(token) == (version is not None):
        raise ToolError("Give either token or version, not both or neither.")
    # H22 review: the same lookup precheck_publish_page/apply_publish_page use, so a token belonging to
    # another business or a version that does not exist refuses here exactly as it would at confirm time.
    named = {"token": token} if token else {"version": version}
    page_id, html, _title, _source, _sections = proposals._publish_source(conn, biz, named)
    if token:
        from .sites import build  # read when asked, as hosted.serve reads bundles: no import cycle forms

        held = build.publish_gate(conn, hosted.preview(conn, token))
        if held:  # A3p-6 Q11: an industry site waiting for its photos is refused before anything is reserved
            raise ToolError(held)
    files = proposals._bundle_files(conn, page_id, named)  # A3p-3 B10: a site bundle's files, else None
    live = proposals._already_published_url(conn, biz, page_id, html, token, files)
    if live:
        return None, f"This page is already published at {live}."
    # A3p-3 P17: a site taken down for the hosting minimum, or whose countdown has ended, is refused at the request
    # already (confirm_proposal's precheck refuses it again, should it change meanwhile)
    hosting_minimum.precheck_publish(conn, settings, biz, landing.get(conn, page_id))
    if token:
        reused = proposals.open_for_publish_token(conn, biz, token)
        if reused:
            return reused, f"{reused['summary']}. Confirm to publish it; nothing has changed since you last asked."
    knowledge = business.latest_knowledge(conn, biz)
    name = plans.name_for(knowledge[1] if knowledge else {})
    hostname = hosted.hostname_for(conn, biz, name, settings.pages_domain)
    if token:
        preview_url = f"{settings.public_base_url}/p/{token}"
        if files is not None:  # a site bundle's preview is on the business's preview host (A3p-3 B8)
            preview_url = f"https://{bundles.preview_host(conn, page_id, settings.pages_domain)}/"
        # H22 minor: the hostname is reserved here, in the payload -- precheck_publish_page refuses if
        # another business takes the slug (a first-ever hosted publish only) before this is confirmed.
        payload = {"token": token, "hostname": hostname}
        summary = f"Publish the {'site' if files is not None else 'page'} at {preview_url} to {hostname}"
        say = f"Look at {preview_url}; confirm to publish it at https://{hostname}/ — the ads will send people there."
    else:
        preview_url = f"https://{hostname}/"
        payload = {"version": version, "hostname": hostname}
        summary = f"Republish version {version} at {hostname}"
        say = f"Confirm to republish version {version} at https://{hostname}/ — the ads will send people there."
    what_changes = [
        f"The ads will send people to https://{hostname}/",
        "Admiry runs the 14-rule check on it right away and e-mails you the report",
        "If your ads are already running, they move to this address as soon as you confirm",
    ]
    # A3p-3 (P15, plan Q9): the hosting minimum before the owner's yes, and while the site is not covered the date to
    # start the ads by; nothing for an exempt business
    note = hosting_minimum.publish_note(conn, biz, landing.current(conn, biz))
    if note:
        what_changes.append(note)
        say = f"{say} {note}"
    prop = proposals.create(
        conn,
        biz,
        cls="nonspend",
        kind="publish_page",
        payload=payload,
        summary=summary,
        what_changes=what_changes,
        approval_url=preview_url,
    )
    return prop, say


@agent_tool(cls="P", scope="manage")
def publish_page(idempotency_key: str, token: str | None = None, version: int | None = None) -> dict:
    """Propose making a page you previewed (token, from preview_page, create_page, update_page or revise_page) --
    or an earlier version of the current one (version, from get_page's versions) -- the live page the ads send
    people to. Give exactly one of the two. This only proposes it: nothing changes until the owner agrees and you
    call confirm_proposal, which also runs the 14-rule check on the now-live address and e-mails the report, and
    moves the ads there if they are already running. Refused with one sentence when hosting is not set up on this
    server, the token has expired, the version does not exist, or both/neither of token and version were given.
    Calling this again with a token already published (or still awaiting your confirm_proposal) never proposes
    a duplicate: it answers with the live URL, or hands back the same open proposal."""

    def body(conn, p):
        biz = need_business(p)
        prop, say = _propose_publish(conn, biz, token=token, version=version)
        if prop is None:
            return envelope.make({"status": "already_published"}, say=say)
        return envelope.make({"status": "proposed", **prop}, say=say)

    return run(
        "publish_page",
        "manage",
        {"token": token, "version": version, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
    )


@agent_tool(cls="P", scope="manage")
def unpublish_page(idempotency_key: str) -> dict:
    """Propose taking the current Admiry-hosted page down: its address answers "gone" to every visitor until
    you publish it again (publish_page(version=n) brings back any earlier version, address unchanged).
    Nothing changes until the owner agrees and you call confirm_proposal, which takes the page down and, when
    the ads are already running, pauses them too (in that order: the page first)."""
    # H23: the page goes down inside confirm's transaction; the pause follows in the same request, after it.

    def body(conn, p):
        biz = need_business(p)
        page = landing.current(conn, biz)
        if page is None or page["status"] != "published" or page.get("source") not in ("hosted", "admiry"):
            raise ToolError("There is no published Admiry-hosted page to take down.")
        hostname = page["hostname"]
        # H24: ads already paused (an owner's pause included) are not "paused" by this in the owner's eyes -- say so
        # accurately; confirming still adds Admiry's page-down pause beside theirs (M0 Task 9 D5)
        pausing = any(r["state"] != "paused" for r in ads_run.on_host(conn, biz, hostname))
        gone = f'https://{hostname}/ will answer "gone" until you publish again'
        # H23: the page goes down and the pause follows in the same request -- one bullet, that order.
        what_changes = [f"Pauses the ads and takes the page down: {gone}"] if pausing else [gone]
        say = (
            f"Confirm to pause your ads and take https://{hostname}/ down."
            if pausing
            else (f"Confirm to take https://{hostname}/ down.")
        )
        prop = proposals.create(
            conn,
            biz,
            cls="nonspend",
            kind="unpublish_page",
            payload={},
            summary=f"Take down the page at https://{hostname}/",
            what_changes=what_changes,
            approval_url=f"https://{hostname}/",
        )
        return envelope.make({"status": "proposed", **prop}, say=say)

    return run("unpublish_page", "manage", {"idempotency_key": idempotency_key}, body, idempotency_key=idempotency_key)


# ---------------------------------------------------------------- a page Admiry writes (Task 5, H12, H35)

# What an assistant (or the homepage) does next with a page Admiry wrote: publish it, or say what to change.
NEXT_PAGE = envelope.actions(
    {"id": "publish", "label": "Publish this page", "tool": "publish_page", "primary": True},
    {"id": "change", "label": "Change something", "tool": "revise_page"},
)
# A3p-3 P22/P23: a change that is more than the template holds, or a page the assistant now manages, starts from the
# live page's source
NEXT_SOURCE = envelope.actions(
    {"id": "source", "label": "Start from the page's source", "tool": "get_page_source", "primary": True},
)
ASSISTANTS_PAGE = (
    "The live page is the one your assistant made, not the one Admiry wrote: change it from its source, or have "
    "Admiry write a new page"
)
# A3p-6 batch 2 (Q14, Q12): the page of a home-services business or a dental practice is its site once Admiry's own sites
# are on; changes to it by sentence are batch 3's (T17)
NO_CHANGES_YET = "Changes to this site by sentence come with the next release. You can change its facts and photos now."


def _assistants_page(page: dict | None) -> bool:
    """A3p-3 P23 (plan Q14): the live page is the assistant's own HTML (`source = 'hosted'`, published) -- the page is
    the assistant's now, and Admiry's quick changes (update_page, revise_page) would work on its own older words."""
    return page is not None and page.get("source") == "hosted" and page.get("status") == "published"


def _is_site(conn, biz: str) -> bool:
    """Whether the business's page is an industry site's (A3p-6 batch 2 Q14): its row carries the owner's answers for a
    site or a document a build wrote (`photos.is_site`) and no page Admiry wrote -- a starter page with a draft left
    beside it is still the starter page -- stands as the business's page."""
    return page_builder.current(conn, biz) is None and site_photos.is_site(landing.current(conn, biz))


def _missing_note(kit: dict) -> str:
    """The say's last sentence when a required line needs a number the business's facts do not give (a contractor's
    licence number): the page leaves the bare label off rather than print it empty, and says where it is needed."""
    _lines, missing = page_builder.required_lines(kit)
    return "".join(
        f" Where the law asks for it ({m['when']}), the page must also show “{m['text']}” with your number; tell me "
        "the number and I will add it."
        for m in missing
    )


@agent_tool(cls="W", scope="manage")
def create_page(template: str | None = None, brief: str | None = None) -> dict:
    """Admiry writes the landing page for the ads itself -- for an owner with no page and no assistant to build one:
    the plan's service and city, the business's own facts, the kind of business's layout (template: dentists,
    home_services, attorneys or fallback; leave it out for the business's own kind), a call button, a form that
    sends leads to the owner, the privacy notice and the lines the law asks for. brief is an optional sentence of
    direction ("warmer", "stress the same-week appointments"). The words only state facts Admiry has for the
    business -- never a price, a number of years, a rating or a licence number it was not given. Answers with a
    private preview link (24 hours), the token and the page's sections; change one part with update_page or say
    what to change with revise_page, then publish_page(token) proposes making it the live page. Needs the plan
    first. When the answer asks for a number the law wants on the page (a contractor's licence), add it to the
    business's facts as written there ("License #" and the number, with update_business), then call update_page
    or create_page again. For a home-services business or a dental practice, with Admiry's own sites on and no page
    of this kind written yet, this starts the business's website instead (build_site's first step): the answer asks
    which look the owner wants, then the facts Admiry still needs (template and brief do not apply)."""
    # read when asked: tools_sites registers its commands after this module's, and the registry keeps that order
    from . import tools_sites

    args = {"template": template, "brief": brief}
    # Q14: a family business's page step is build_site's own prepare and body; its e-mail follows the commit
    site = tools_sites.BuildCall(audit_args=args)

    def prepare(conn, p):
        # refused, never cut, before the day's model limit is looked at (final review C, minor 13); page_builder's own
        # cut stays as a guard for its other callers
        if len((brief or "").strip()) > page_builder.MAX_DIRECTION:
            raise ToolError(f"the brief takes at most {page_builder.MAX_DIRECTION:,} characters")
        biz = need_business(p)
        # decided before the model or the day's limit is looked at: a site asks for the limit only when it writes words
        if tools_sites.takes_over(conn, biz):
            args["site"] = True  # the row counts toward the day only when this call wrote the site's words (auth)
            return "site", site.prepare(conn, p)
        settings = config.load()
        # final review I4: with Admiry's own sites off, a starter page is never written over a site's row (a draft or a
        # live site) -- refused before anything is written, and the refusal never counts toward the day (auth)
        if not settings.sites and _is_site(conn, biz):
            args["site"] = True
            raise ToolError(tools_sites.SITES_OFF)
        auth.check_llm_cap(conn, p)
        kit = landing.kit(conn, biz)
        tree = page_builder.tree_for(template, kit)  # a wrong name is refused before the model is asked
        copy, html = page_builder.build(conn, settings, biz, template=tree, brief=brief)
        return "page", (kit, tree, copy, html)

    def body(conn, p, ctx):
        found, work = ctx
        if found == "site":
            return tools_sites.answer(conn, p, work)
        kit, tree, copy, html = work
        settings = config.load()
        token, sections = page_builder.save(
            conn,
            kit["page_id"],
            tree=tree,
            copy=copy,
            photos=[],  # H35: the owner's photos come with the upload link minted just below
            hours=None,
            html=html,
            title=page_builder.title_for(copy, kit),
            # H16: None for an assistant's own call; the web base's origin inside home_api.py's
            # hosted.with_embed_origin, when the homepage itself is what just called this tool.
            embed_origin=hosted.current_embed_origin(),
        )
        upload_url = f"{settings.web_base_url}/u/{uploads.new_token(conn, kit['page_id'])}"  # H35, filled (Task 6)
        preview_url = f"{settings.public_base_url}/p/{token}"
        # the fourth way's words first for a business with a website of its own -- the kit's facts name it,
        # profile.own_site (A3p-5 batch 2, ruling R2-f)
        instead = f"{site_install.ADMIRY_PAGE_WAY} " if kit["facts"].get("website") else ""
        say = (
            f"{instead}Here is the page I wrote for your {kit['facts']['service']} ads: {preview_url} (the link works "
            "for 24 hours). Tell me what to change, or publish it to make it the page your ads send people to."
            + _missing_note(kit)
            + f" Add your own photos any time, right from a phone: {upload_url}"
        )
        data = {"preview_url": preview_url, "token": token, "sections": sections, "upload_url": upload_url}
        return envelope.make(data, say=say, next=NEXT_PAGE)

    return run("create_page", "manage", args, body, prepare=prepare, after=site.after)


@agent_tool(cls="W", scope="manage")
def update_page(section: str, text: str) -> dict:
    """Change one part of the page Admiry wrote (create_page), exactly as written -- no rewriting: section is
    headline, subheadline or offer_line (one line of text); trust (the reasons to choose the business, one per line,
    at most 4); service (the paragraphs about the service, one per line, at most 3); faq (one question and its
    answer per line, the question ending with "?": "Is there parking? Yes, behind the building."); or hours (one
    line; empty goes back to the business's own hours). The new words are checked against the same rules as
    Admiry's own (only facts Admiry has for the business). Answers with a new preview link and token for
    publish_page."""

    args = {"section": section, "text": text}

    def body(conn, p):
        biz = need_business(p)
        settings = config.load()
        found = page_builder.current(conn, biz, lock=True)
        if found is None:
            if _is_site(conn, biz):
                args["site"] = True  # the refusal's row says whose it was: it never counts toward the day (auth)
                raise ToolError(NO_CHANGES_YET)
            raise ToolError(page_builder.NO_PAGE)
        page, sections = found
        if _assistants_page(page):
            raise ToolError(ASSISTANTS_PAGE)
        kit = landing.kit(conn, biz)
        copy, hours = page_builder.edit(kit, sections, section, text)
        photos = list(sections.get("photos") or [])
        html, problems = page_builder.finish(
            settings,
            kit,
            sections["tree"],
            copy,
            photos=photos,
            hostname=page_builder.page_hostname(page, settings),
            hours=hours,
        )
        if problems:
            raise ToolError(page_builder.REFUSED + " ".join(problems))
        token, saved = page_builder.save(
            conn,
            kit["page_id"],
            tree=sections["tree"],
            copy=copy,
            photos=photos,
            hours=hours,
            html=html,
            title=page_builder.title_for(copy, kit),
            expect_version=sections.get("version"),
        )
        preview_url = f"{settings.public_base_url}/p/{token}"
        upload_url = uploads.latest_upload_url(conn, kit["page_id"], settings.web_base_url)  # H13: while valid
        say = f"I changed the {page_builder.LABELS[section]} as you wrote it. Here is the new preview: {preview_url}."
        data = {"preview_url": preview_url, "token": token, "sections": saved, "upload_url": upload_url}
        return envelope.make(data, say=say, next=NEXT_PAGE)

    return run("update_page", "manage", args, body)


@agent_tool(cls="W", scope="manage")
def revise_page(instruction: str) -> dict:
    """Have Admiry rewrite the page it wrote (create_page) from one sentence of direction -- "make it warmer",
    "shorter paragraphs", "lead with the same-week appointments" -- keeping every part the sentence does not
    touch. The same rules hold as for a new page: only facts Admiry has for the business. Answers with a new
    preview link and token for publish_page; update_page changes one part exactly as written instead. A sentence
    that asks for more than this one page holds (another page, a new kind of section, a layout or colour change, a
    widget from another company) changes nothing: the answer says so (beyond_template) and points to the page's
    source, to change it yourself."""

    args = {"instruction": instruction}

    def prepare(conn, p):
        words = (instruction or "").strip()
        if len(words) > page_builder.MAX_DIRECTION:  # refused, never cut, before the day's model limit is looked at
            raise ToolError(f"the instruction takes at most {page_builder.MAX_DIRECTION:,} characters")
        biz = need_business(p)
        # a site's changes by sentence are the next release's: refused before the day's limit and before the model
        if _is_site(conn, biz):
            args["site"] = True  # the refusal's row says whose it was: it never counts toward the day (auth)
            raise ToolError(NO_CHANGES_YET)
        auth.check_llm_cap(conn, p)
        if not words:
            raise ToolError("Say what to change, e.g. “make it warmer” or “shorter paragraphs”.")
        found = page_builder.current(conn, biz)
        if found is None:
            raise ToolError(page_builder.NO_PAGE)
        page, sections = found
        if _assistants_page(page):  # before the model is asked: its words would be Admiry's older page's
            raise ToolError(ASSISTANTS_PAGE)
        settings = config.load()
        kit = landing.kit(conn, biz)
        hostname = page_builder.page_hostname(page, settings)
        try:
            copy, html = page_builder.revise(settings, kit, sections, instruction=words, hostname=hostname)
        except page_builder.Beyond:
            return None  # A3p-3 P22: more than the template holds; nothing is written
        return kit, sections, copy, html

    def body(conn, p, ctx):
        if ctx is None:
            data = {"beyond_template": True, "preview_url": None, "token": None}
            return envelope.make(data, say=page_builder.BEYOND, next=NEXT_SOURCE)
        kit, sections, copy, html = ctx
        settings = config.load()
        token, saved = page_builder.save(
            conn,
            kit["page_id"],
            tree=sections["tree"],
            copy=copy,
            photos=list(sections.get("photos") or []),
            hours=sections.get("hours"),
            html=html,
            title=page_builder.title_for(copy, kit),
            expect_version=sections.get("version"),  # an update_page landing meanwhile is never lost
            # H16: None for an assistant's own call; the web base's origin inside home_api.py's
            # hosted.with_embed_origin, when the homepage itself is what just called this tool.
            embed_origin=hosted.current_embed_origin(),
        )
        preview_url = f"{settings.public_base_url}/p/{token}"
        upload_url = uploads.latest_upload_url(conn, kit["page_id"], settings.web_base_url)  # H13: while valid
        say = f"I rewrote the page. Here is the new preview: {preview_url}."
        data = {"preview_url": preview_url, "token": token, "sections": saved, "upload_url": upload_url}
        return envelope.make(data, say=say, next=NEXT_PAGE)

    return run("revise_page", "manage", args, body, prepare=prepare)


# ---------------------------------------------------------------- after-commit hooks (H8, H11; P6's own shape)


def _shots(conn, page: dict) -> list[dict] | None:
    """A3p-3 batch 4 (R7): a published site bundle's screenshots from the render check, as the report e-mail's
    attachments; None for a page with none."""
    found = []
    for size in ("phone", "tablet", "desktop"):
        data = bundles.shot(conn, page["id"], page["version"], size)
        if data:
            found.append({"filename": f"site-{size}.jpg", "content": base64.b64encode(data).decode("ascii")})
    return found or None


def _after_publish_page(result: dict, biz: str) -> dict:
    """Runs once proposals.confirm's transaction for apply_publish_page has committed (tools_proposals.
    confirm_proposal dispatches here by AFTER_COMMIT[kind]): the 14-rule check against the address that is now
    actually live, the report saved and e-mailed (R12), then the ads, channel by channel (M0 Task 9) --
    `ads_run.launch_now` once when `apply_publish_page` classified any channel's ads `"moved"` (so H9's diff runs in
    this request rather than waiting for the hourly sweep), and `ads_run.resume` of each channel it named in
    `"resume"` with the reason it read there (H24/H30: Admiry's pause for the page, never an owner's and never one
    for money -- and only while it is still that pause when the resume runs: a pause that landed meanwhile stays,
    an owner's beside it included) -- a channel is never both (`"resume"` names only paused rows, never `"moved"`
    ones). Never raises: the page is already published by the time this runs, so a check/mail/platform hiccup
    answers with what it could still do rather than failing a publish that otherwise succeeded; whatever it could
    not finish is retried by check_landing_page or the hourly sweep (a resume itself is not retried automatically --
    page-check lifts its own once the page opens, and a publish or set_landing_page again lifts either). `"ads"`
    stays Google Search's word for one release; `"ads_by_channel"` has every channel's."""
    data = dict(result["data"])
    say = result["say"]
    resume_needed = data.pop("resume", None) or {}
    try:
        settings = config.load()
        with db.connect(settings.database_url) as conn:
            page = landing.get(conn, data["page"]["id"])
            kit = landing.kit(conn, biz)
            report = landing.check(
                page["url"], page, kit, conn=conn, transport=_check_transport(), resolver=crawl.resolve
            )
            landing.save_check(conn, page["id"], report)
            # H29: a check that passes at the address a platform refused is the retry -- a republish at the same
            # address ("unchanged") then moves the ads like a new one
            retried = ads_run.retry_refused_address(conn, biz, page["url"]) if report["ok"] else []
            by_channel = dict(data.get("ads_by_channel") or {})
            for channel in retried:
                if by_channel.get(channel) == "unchanged":
                    by_channel[channel] = "moved"
            data["ads_by_channel"] = by_channel
            to, reason = tools_pages._mail_report(conn, biz, report, attachments=_shots(conn, page))
            page = landing.get(conn, page["id"])
        data["page"] = {**data["page"], "checked_at": iso(page["checked_at"])}
        data["check"], data["emailed"], data["email"] = report, reason == "sent", reason
        say = f"{say} " + tools_pages._summary(report) + tools_pages._mail_note(reason, to, "report")
    except Exception:  # the page is already published; a check/mail hiccup must not fail this reply
        log.warning("publish_page: the post-publish check failed for business %s", biz, exc_info=True)
        say = f"{say} I could not check the page just yet; check it again in a moment."
    by_channel = dict(data.get("ads_by_channel") or {})
    if "moved" in by_channel.values():
        launched = ads_run.launch_now(biz)  # never raises (its own contract)
        say = f"{say} {launched['say']}"
    if resume_needed:
        settings = config.load()
        try:
            with db.connect(settings.database_url) as conn:
                for channel, reason in resume_needed.items():
                    # decided inside confirm's transaction, sent seconds later (after the check and the e-mail):
                    # only while that pause for the page is still the one there -- a pause landing meanwhile stays
                    # (F3), an owner's beside it included (then this lifts Admiry's alone and the ads stay off)
                    try:
                        resumed = ads_run.resume(conn, settings, biz, channel, reason=reason)
                    except ToolError as e:
                        log.info("publish_page: resume on %s for business %s waits: %s", channel, biz, e)
                        continue
                    by_channel[channel] = resumed["state"]
                    say = f"{say} {resumed['say']}"
        except Exception:  # the page is already published; a resume hiccup must not fail this reply
            log.warning("publish_page: resume failed for business %s", biz, exc_info=True)
    for channel, word in by_channel.items():
        if word == "off":
            # H22 review minor: built, live ads (never "paused" -- that reads "paused" instead) with the switch
            # off must not read as if nothing about them is true right now; they are still whatever the platform
            # has them as, at their old address, until execution is turned back on.
            say = f"{say} {ads_run.OFF_OLD_ADDRESS.format(label=registry.label(channel))}"
    if by_channel:
        data["ads_by_channel"] = by_channel
        data["ads"] = by_channel.get(proposals.GOOGLE, data.get("ads"))
    try:  # A3p-3 (plan Q10): whether the ads keep hosting free, and the date the site comes down if they do not
        with db.connect(config.load().database_url) as conn:
            hosting = hosting_minimum.status(conn, biz, landing.get(conn, data["page"]["id"]))
        data["hosting"] = hosting
        if hosting and hosting["say"]:
            say = f"{say} {hosting['say']}"
    except Exception:  # the page is already published; a read hiccup must not fail this reply
        log.warning("publish_page: the hosting status failed for business %s", biz, exc_info=True)
    return {**result, "data": data, "say": say}


def _after_unpublish_page(result: dict, biz: str) -> dict:
    """Runs once proposals.confirm's transaction for apply_unpublish_page has committed: pauses the ads of every
    channel it named (`"pause"`: built, following the page, carrying its hosted address) for the page --
    `ads_run.pause_page_down`, by Admiry, reason `page_down`: the page they point at is gone, a pause the hourly
    page-check never undoes (A3p-2 Task 4), stacked beside an owner's pause already there (M0 Task 9 D5) -- never
    inside that transaction, since a pause calls the platform when the switch is live. A pause failure (the platform
    down, or its own owed-pause bookkeeping) never fails this reply: the page is already down, the hourly ads-launch
    retries an owed pause every hour at no further cost to this call, and page-check's backstop pauses running ads
    still pointing at it. `"ads"` stays Google Search's state for one release; `"ads_by_channel"` has every
    channel's."""
    data = dict(result["data"])
    hostname, version = data["page"]["hostname"], data["page"]["version"]
    say = f'https://{hostname}/ now answers "gone". Publishing version {version} again brings it back.'
    channels = data.pop("pause", None) or []
    if channels:
        settings = config.load()
        by_channel: dict[str, str] = {}
        try:
            with db.connect(settings.database_url) as conn:
                for channel in channels:
                    try:
                        ads_run.pause_page_down(conn, settings, biz, channel)
                    except Exception:  # one channel's hiccup never keeps the others' pauses from going out
                        log.warning("unpublish_page: pause on %s failed for business %s", channel, biz, exc_info=True)
                        continue
                    row = ads_run.get(conn, biz, channel)
                    by_channel[channel] = row["state"]
                    say = f"{say} {ads_run.status_sentence(row)}"
        except Exception:  # the page is already unpublished; a pause hiccup must not fail this reply
            log.warning("unpublish_page: pause failed for business %s", biz, exc_info=True)
        if by_channel:
            data["ads_by_channel"] = by_channel
            if proposals.GOOGLE in by_channel:
                data["ads"] = by_channel[proposals.GOOGLE]
    return {**result, "data": data, "say": say}


AFTER_COMMIT = {"publish_page": _after_publish_page, "unpublish_page": _after_unpublish_page}
