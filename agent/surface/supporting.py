"""The supporting tools (spec 2026-10-01 §4.2, §4.4): what a launch needs beside the plan -- the profile's changes,
the accounts, the page, the billing -- and the activity log. `contact_support` is published as it is."""

from __future__ import annotations

import re
from collections.abc import Callable

from fastmcp.exceptions import ToolError

from .. import ads_mode, blockers, config, db, launch_check, tools_sites
from . import actions
from .registry import data_of, dispatch, earlier_confirmation, passthrough, public_tool
from .where import Where, where

# "site" (A3p-6 batch 2, plan Q15): where the website Admiry builds for a home-services business or a dental practice
# stands
PAGE_VIEWS = ("status", "check", "kit", "source", "site")
# send_install_steps and request_install_help (A3p-5 batch 2, plan Q6-Q8): two of the four ways to Admiry's code on the
# owner's own website; build_site (A3p-6 batch 2, plan Q15): the website Admiry builds itself
PAGE_ACTIONS = (
    "write",
    "revise",
    "edit",
    "use_url",
    "preview_html",
    "upload_site",
    "publish",
    "unpublish",
    "send_install_steps",
    "request_install_help",
    "build_site",
    "fix_headline",
)
BILLING_VIEWS = ("position", "payments", "topup_link")
PATCH_OR_DESCRIPTION = "send a patch, or a description (with or without a website) to read again, not both"
NEED_DESCRIPTION = (
    "a description is needed: one or two sentences on what the business does and where (with website_url when there "
    "is a site to read)"
)
NOTHING_WAITING = "nothing is waiting for the owner's yes: ask for it first, show what changes, then confirm"
SAME_PAGE = "to confirm a publish, send the same token or version you asked with, and owner_confirmed: true"
# What a public patch takes: the keys update_business_profile's description names, in its order. The command takes
# more, never described to assistants (hours and phone go inside `locations`).
PUBLIC_PATCH = (
    "name",
    "category_detail",
    "website",
    "pricing_hints",
    "usp",
    "locale",
    "services",
    "offers",
    "locations",
    "google_listing",
    "notes",
)

# plan D15 (Review Focus 5): the checklist's fix carries `<…>` where only the owner knows the value; one sent as it is
# is refused in a sentence, nothing written -- a placeholder stored as the phone or the address is worse than none
PLACEHOLDER = re.compile(r"\s*<[^<>]+>\s*")
PLACEHOLDER_SENT = (
    "a value written as <…> is a placeholder from setup.checklist: ask the owner, then send their answer in its place"
)
# connect_account's own choice (ads_mode.WAY_CHOICES) carries the Customer ID as a placeholder: one sent as it is is
# refused here, nothing run -- the invitation's path would otherwise answer with the agreement before it read the number
CUSTOMER_ID_PLACEHOLDER = (
    f"account_id {ads_mode.CUSTOMER_ID} is a placeholder: ask the owner for the 10-digit Customer ID at the top of "
    "their Google Ads page, then send it in its place"
)


def _no_placeholders(value: object) -> None:
    """ToolError PLACEHOLDER_SENT for any string in `value` -- a dict or a list, however deep -- that is a `<…>`
    placeholder."""
    if isinstance(value, str) and PLACEHOLDER.fullmatch(value):
        raise ToolError(PLACEHOLDER_SENT)
    if isinstance(value, dict):
        for inner in value.values():
            _no_placeholders(inner)
    if isinstance(value, list):
        for inner in value:
            _no_placeholders(inner)


def command_patch(patch: dict) -> dict:
    """A public patch in the command's keys: `services` and `offers` are by-id changes, `locations` is the one
    location, `notes` are added; the rest of PUBLIC_PATCH passes as it is. Refused (ToolError): an empty patch, a key
    the patch does not take, and services, offers or notes in another shape -- in the tool's own names, as the
    command's would be service_changes, offer_changes and notes_add (final review C, minor 1)."""
    if not isinstance(patch, dict):
        raise ToolError("patch must be an object")
    if not patch:
        raise ToolError(PATCH_OR_DESCRIPTION)
    _no_placeholders(patch)
    unknown = set(patch) - set(PUBLIC_PATCH)
    if unknown:
        raise ToolError(f"unknown patch fields: {sorted(unknown)}; a patch takes {', '.join(PUBLIC_PATCH)}")
    for key in ("services", "offers"):
        if key in patch and not (isinstance(patch[key], list) and all(isinstance(item, dict) for item in patch[key])):
            raise ToolError(f"{key} must be a list of objects")
    notes = patch.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(note, str) and note.strip() for note in notes):
        raise ToolError("notes must be a list of sentences")
    out = {key: value for key, value in patch.items() if key not in ("services", "offers", "locations", "notes")}
    if "services" in patch:
        out["service_changes"] = patch["services"]
    if "offers" in patch:
        out["offer_changes"] = patch["offers"]
    if "locations" in patch:
        places = patch["locations"]
        if not isinstance(places, list) or len(places) != 1 or not isinstance(places[0], dict):
            raise ToolError("locations takes one object: the business has one location")
        out["location"] = places[0]
    if "notes" in patch:
        out["notes_add"] = patch["notes"]
    return out


# not destructive (final review A m8): every version is kept: a new start or a new reading replaces nothing for good,
# and the approved plan and the running ads are untouched
@public_tool(cls="W", scope="manage", commands=("update_business", "describe_business"), open_world=True)
def update_business_profile(
    idempotency_key: str, patch: dict | None = None, description: str | None = None, website_url: str | None = None
) -> dict:
    """Correct or add what Admiry knows about the business. Either `patch`: name, category_detail, website (null
    removes it), pricing_hints and usp (each a list of sentences: the list you send replaces the one on file),
    locale; `services` and `offers` as lists of changes (an item with an `id` changes that one, or removes it with
    `remove: true`; an item without an id adds one: a service needs `name`, an offer `text`); `locations` with one
    object (address: the street address, or the city and ZIP code, the ads centre on, which Admiry places on the
    map; geo_model storefront or service_area; areas; hours; phone; phone_hours: when someone answers the phone);
    `google_listing`, alone in a patch: null when the owner says the Google Maps listing Admiry linked is not their
    business (Admiry unlinks it and never offers it again), {"find": "<the business's name and its address or city>"}
    to look it up (the answer lists the close places, each with an id, and links none), or
    {"choose": "<one of those ids>"} for the one the owner says is theirs ({"choose": "none"} when none is);
    `notes` (sentences worth keeping). A patch takes these keys and no other. Or a new reading of the business:
    `description` (required: one or two sentences on what it does and where) and, with it, `website_url` when
    there is a site for Admiry to read. A new reading replaces what the last one said: the services, the hours,
    the phone, the places served, and the website unless you send website_url again. The services' ids, the
    offers, the notes, how the business works its area, when the phone is answered and the Google listing (the
    address and the map point the ads centre on: a new reading does not look the business up again) stay. So send
    a correction as a `patch`, and a description only when the business itself changed. Confirm the wording with
    the owner first. The radius is part of the plan, not of the profile. Use a new idempotency_key per change."""
    # beside a patch, a blank description or website is no second kind of change: a client may fill the strings it
    # does not use with ""; and beside a description, neither is an empty patch: the description is read (final review
    # B, minor). An empty patch alone is told what to send (command_patch)
    rereading = bool((description or "").strip() or (website_url or "").strip())
    if patch == {} and rereading:
        patch = None
    if patch is not None:
        if rereading:  # both kinds of change at once
            raise ToolError(PATCH_OR_DESCRIPTION)
        return dispatch("update_business", patch=command_patch(patch), idempotency_key=idempotency_key)
    if description is None and website_url is None:  # neither kind of change
        raise ToolError(PATCH_OR_DESCRIPTION)
    if not (description or "").strip():  # a site alone, or blank words: a reading starts from the owner's words
        raise ToolError(NEED_DESCRIPTION)
    return dispatch("describe_business", text=description, idempotency_key=idempotency_key, website_url=website_url)


@public_tool(cls="W", scope="manage", approval="assistant", commands=("connect_channel", "request_connect_link"))
def connect_account(
    idempotency_key: str, channel: str = "google_search", mode: str | None = None, account_id: str | None = None
) -> dict:
    """Ask the owner how their ads should run and never choose for them; then call this with their answer, to connect
    the business's account where the ads run (channel: google_search, the default, or newsbreak). For Google Search
    the two ways, side by side: Admiry runs the ads in its own Google Ads account from the owner's prepaid balance,
    with no Google account needed (mode "managed": the answer is connect_url, the page where the owner chooses that
    way, accepts the terms and adds funds); or the owner's own Google Ads account (account_id, the 10-digit Customer
    ID at the top of its page, 123-456-7890: Admiry sends an invitation the owner accepts inside Google Ads). Called
    with neither while the owner has not chosen, it changes nothing: the answer is status choose, the question to ask
    (ask_owner) and both choices, each with the arguments to send back. The page comes first: while the business has
    no page for its ads yet, a call that would ask the way or answer a step on the web changes nothing and answers
    status page_first, the page question (ask_owner) and its three ways for update_landing_page -- or, with a page made
    and not published yet, the step that publishes it. The first time, the answer may be status needs_agreement with the
    page where the owner accepts Admiry's management agreement; then call again with a new idempotency_key.
    NewsBreak runs from the owner's balance with nothing to connect: the answer says what it waits for while it is not
    ready (next_step: funds, added through get_billing's topup_link, or Admiry getting its account ready). An owner
    not signed in yet gets their sign-in link instead: give it to them.
    IMPORTANT ACTION: confirm the account with the owner before calling. Use a new idempotency_key per call."""
    if isinstance(account_id, str) and PLACEHOLDER.fullmatch(account_id):
        raise ToolError(CUSTOMER_ID_PLACEHOLDER)
    w = where(scope="manage")
    # nobody is signed in for the business -- a setup key, or a business whose claim lapsed while its owner's keys stay
    # valid: the sign-in link, as get_marketing_state's sign-in step says (a draft's account cannot be connected)
    if w.key_class == "draft" or w.claim_state == "draft":
        return dispatch("request_connect_link")
    return dispatch(
        "connect_channel", channel=channel, idempotency_key=idempotency_key, mode=mode, account_id=account_id
    )


@public_tool(
    cls="W",
    scope="read",
    commands=("get_page", "check_landing_page", "get_page_kit", "get_page_source", "get_site"),
    open_world=True,
)
def get_landing_page(view: str = "status", candidate_url: str | None = None) -> dict:
    """The page the ads send people to. view "status": its address, whether it is live, its versions, its last
    check, what Admiry's hourly look last saw and, for a page Admiry hosts, `hosting`: whether the ads keep it free
    and when it comes down if they do not. view "check": run the check now on the page on file, or on
    candidate_url, an address tried without changing anything. view "kit": everything needed to build the page
    somewhere else -- the facts, the words the ads use, the sections, the rules, the tracking snippet, the checklist it
    is judged by, and what a whole site uploaded to Admiry must be (`bundle`; the kit is also e-mailed to the owner,
    once per plan version). view "source": the page Admiry hosts, as it is live -- its whole HTML, to change it and
    send it back with update_landing_page's preview_html (keep Admiry's script tag in it as it is), or for a site
    uploaded whole its files and a link to them as one zip (an hour), to change and upload again with upload_site; or
    to keep a copy. view "site": the website Admiry builds for a home-services business or a dental practice
    (update_landing_page's "build_site"), wherever it stands: `family` and `trade`, `status` (none before its first
    build, then preview, then published), the `style` and `accent` picked and the three `looks` (`id`, `name`, `line`
    and `look_url`, the address of a picture), the `facts` the owner gave for it (what Admiry already knows is in
    get_business_profile) and those still `missing`, `extras_asked` (whether the industry's own facts were offered), its
    private `preview` (`url`, `token`, `expires_at`: the link works for 24 hours, and the `token` is what publishing
    takes -- an upload through the owner's link makes a new preview and ends this one; `render`: how the render check
    went, `loads` the page loads it made and `partial` whether time ran out before every page was opened, null when it
    did not run), the photo `checklist` (each kind of photo: `id`, `label`, `need`: how many the site uses, `have`: how
    many are in, `done`, `blocks_publish`), `key_photos` (`ok`: the logo, or the wordmark the owner accepted, and the
    family's key photo are in; if not, `sentence` says what keeps the site from going live, `logo`, `key` and
    `wordmark_ok` which), `upload_url` (the link where the owner adds the logo and the photos from a phone: photos reach
    Admiry only through it; only for a key that may manage, null otherwise), `published` once it is live (`url`,
    `hostname`, `version`, `check`), `hosting` and `hosting_note` as for any page Admiry hosts, and `muse`, a sentence
    to relay once it is live. view "status" also shows the last site uploaded (`upload`: its problems or its preview).
    For a business with a website of its own, view "status" also shows `website`: its address (`url`), the platform it
    is built on (`platform`), whether Admiry's code is on the page the ads use (`code`: missing or found), when that
    page was last checked (`checked_at`) and how the owner chose to get the code there (`way`: send or help; admiry_page
    while the ads use an Admiry page; null before any) -- and `say` says so when Admiry's hourly look finds the code
    gone from the owner's page; view "kit" also has `install`: how to put Admiry's code on the owner's site -- the
    platform (`platform`, `name`), the steps there (`steps`), what the platform needs first (`requirement`), its help
    page (`help_url`), where the code goes (`placement`: head, or page_section) and the code itself (`snippet`). Ads
    never launch to a page that fails its check; NewsBreak's also wait until the page's footer has a Terms link and the
    business's phone or e-mail, which the check only warns about. Admiry hosts a page for free while the business's ads
    with Admiry spend at least $200 a month; tell the owner before building on Admiry hosting."""
    if view not in PAGE_VIEWS:
        raise ToolError("view must be status, check, kit, source or site")
    if candidate_url and view != "check":
        raise ToolError("candidate_url goes with view check")
    if view == "kit":
        return dispatch("get_page_kit")
    if view == "check":
        return dispatch("check_landing_page", url=candidate_url)
    if view == "source":
        return dispatch("get_page_source")
    if view == "site":
        return dispatch("get_site")
    return dispatch("get_page")


def _need(action: str, **fields) -> None:
    missing = [name for name, value in fields.items() if value in (None, "")]
    if missing:
        raise ToolError(f"{action} needs {' and '.join(fields)}")


def _published_the_live_version(w: Where) -> Callable[[dict], bool]:
    """Whether a publish's first answer, sent again, published the version that is live now (Task 18): its data names
    the page as the publish left it (confirm_proposal's `page.version`). A yes that published an earlier version is
    not answered with that publish once another version is the live one; an answer that names no version stands
    while the page is up."""
    live = w.hosted_page_version

    def confirmed(answer: dict) -> bool:
        page = data_of(answer).get("page")
        version = page.get("version") if isinstance(page, dict) else None
        return version is None or version == live

    return confirmed


@public_tool(
    cls="P",
    scope="manage",
    approval="assistant",
    commands=(
        "create_page",
        "revise_page",
        "update_page",
        "set_landing_page",
        "preview_page",
        "request_site_upload",
        "publish_page",
        "unpublish_page",
        "confirm_proposal",
        "send_install_steps",
        "request_install_help",
        "build_site",
    ),
    # it can take the live page down, which pauses the ads that send people to it, and move running ads to another
    # address (final review A m8; C minor 8)
    destructive=True,
    open_world=True,  # use_url checks the owner's page, a publish checks the live one: both are fetched
    # write, revise, edit, preview_html, build_site and fix_headline make a new preview, upload_site a new link,
    # send_install_steps a new e-mail and request_install_help more words on the ticket, on every call
    idempotent=False,
)
def update_landing_page(
    action: str,
    idempotency_key: str,
    template: str | None = None,
    brief: str | None = None,
    instruction: str | None = None,
    section: str | None = None,
    text: str | None = None,
    url: str | None = None,
    html: str | None = None,
    title: str | None = None,
    token: str | None = None,
    version: int | None = None,
    owner_confirmed: bool = False,
    email: str | None = None,
    contact: str | None = None,
    style: str | None = None,
    accent: str | None = None,
    facts: dict | None = None,
    wordmark: bool | None = None,
) -> dict:
    """Give the ads their page. IMPORTANT ACTION: never set owner_confirmed without the owner's yes, never name a page
    with use_url that the owner did not give you, and never send the steps to an address the owner did not give you.
    action is one of:
    - "write": Admiry writes the page from the business's facts and the plan (optional brief, a sentence of
      direction, up to 1,000 characters; optional template: dentists, home_services, attorneys or fallback). This is
      the usual way. The answer is a private preview link and a token. For a home-services business or a dental
      practice, where Admiry's own sites are on and it has not written the business's page yet, "write" starts
      "build_site" instead (brief and template do not apply): the answer is its first question.
    - "revise": Admiry rewrites its page from `instruction` (up to 1,000 characters: "make it warmer"). "edit":
      change one part exactly as written (`section` and `text`): headline (one line, up to 60 characters),
      subheadline (120), offer_line (80), trust (one reason per line, up to 4 lines of 90), service (one paragraph
      per line, up to 3 of 400), faq (one "question? answer" per line, up to 5) or hours (an empty text puts the
      business's own hours back). Text over a limit is refused, never cut. Each revise or edit answers with a new
      link and a new token. A revise that asks for more than the one page holds (another page, a layout or colour
      change, a widget from another company) changes nothing and answers beyond_template: start from
      get_landing_page's view "source" instead. Once a page you made is live, revise and edit no longer apply, and
      they do not apply to a site Admiry built ("build_site") yet: call "build_site" again to change that one.
    - "use_url": the owner already has a page: `url` becomes the ads' page at once and is checked. Ads that already
      run move to it right away, whether or not it passes the check, and ads Admiry paused because their page was
      down can start again: confirm the address with the owner first.
    - "preview_html": a page you wrote yourself (`html`, optional `title`), or the live page's source with your
      changes (keep Admiry's script tag as it is), put on a private preview link.
    - "upload_site": for an assistant that can run commands, a whole site you built (several pages, or a framework's
      build: a Next.js static export, Vite, Astro, Hugo): the answer is a link that takes one upload in the next 30
      minutes and the command to run (`how`: curl -T a zip of the build folder's files, index.html at the top),
      with the script tag every page carries and the limits (get_landing_page's kit has the whole contract). The
      upload answers with every problem to fix (file, rule, message, fix: fix them, build, and ask for a new link),
      or with a private preview link and a token to publish. Admiry hosts built files only, never a server.
    - "publish": make a previewed page or site (`token`) or an earlier version (`version`) the live page.
      "unpublish": take Admiry's hosted page down; the ads that send people to it are paused with it.
    - "send_install_steps": while the owner's website still needs Admiry's code, e-mail the steps for its platform and
      the code to whoever runs the site, at `email` (the one address the owner gives): one fixed message from Admiry,
      with no words of yours in it, and replies go to the owner; at most 3 a day. The answer says where it went; an
      address that is not one, the owner's own address or a fourth in a day is refused in a sentence, and nothing is
      sent.
    - "request_install_help": while the owner's website still needs the code, ask Admiry's team to put it there: a
      person at Admiry contacts the owner at `contact` (an e-mail address), else at their sign-in address, and asks
      them to add a helper in the platform's own settings, never for a password. With no address to reach them at,
      the answer asks for one, and the same call with `contact` sends the request on. When Admiry's team is not
      available yet, the answer says so.
    - "build_site": for a home-services business (a roofer, a plumber, heating and cooling, an electrician) or a
      dental practice, Admiry builds the business's own website, several pages in one of three looks, filled with its
      own facts. Start with no arguments: each call answers one question -- which look the owner wants (its `id` is
      style: answer with `style`, the look's id; get_site_templates shows the three, a picture each), then each fact
      Admiry still needs, one at a time (its `id` is name, phone, place or services: answer with `facts`, {"<id>": the
      owner's words}) -- or, once nothing is left to ask, the site: a private preview link and its `token` (24 hours),
      the checklist of the photos the site uses, `upload_url` (the link where the owner adds the logo and the photos
      from a phone: a photo reaches Admiry only through that link) and what still keeps it from going live. The
      owner's uploads rebuild the preview and end the earlier link: when they say the photos are in, read
      `get_landing_page` with `view: "site"` and publish with the `token` it shows now. `facts` also takes what the
      owner says about the business beyond those four, which the site shows only when given (keys: name, phone, place,
      address, service_area, services, hours, about, email, social, licence, years, insured, warranty, free_estimate,
      emergency, financing, languages, reviews, dentist, new_patients, insurance_plans, in_office_plan,
      emergency_line); send only what the owner said. `place` is the answer to the place question: an
      address, or the towns the business serves. name, phone, address, service_area, hours and services are written to
      the business's profile, which the ads and the pages share (an address typed over a Google listing the owner
      confirmed is the exception: it stays among the site's own facts), so read them back with get_business_profile,
      not from the view's `facts`. A fact is text unless it is: `services` a list of names (or of {name, lead?,
      price_line?}); `service_area`, `hours`, `languages` and `insurance_plans` a list (or lines of text); `social`
      {facebook?, instagram?, tiktok?, yelp?, other?}, each an https address; `reviews` a list of {text, first_name,
      source, when?}; `dentist` {name, credentials?, school?, years?, note?}; `years` a whole number; `insured` true
      or false. A fact that does not fit is refused in a sentence, and nothing is written.
      `accent` is a #RRGGBB colour for the buttons; `wordmark: true` when the owner accepts their business name as the
      logo. Call again to change the look, the colour or a fact: a new look or colour keeps the site's words, a
      changed fact has Admiry write them again. Publishing takes the owner's yes in the same two calls as any page,
      once the logo (or the wordmark) and one real photo of the business are in. Another kind of business is told
      Admiry writes it a page instead; where Admiry's own sites are not on yet, the answer says so. A business whose
      page Admiry already wrote is told a site in its place comes with the next release; "write" rewrites that page
      (brief and template apply).
    - "fix_headline" (no other arguments): put the plan's main service and city on Admiry's own page's headline --
      the site built again with every other word kept, or the headline of the page Admiry wrote changed -- as a
      new preview and its token, like "edit"; where the site still needs its look or a fact first, the answer is
      that question instead, exactly as "build_site" asks it. Refused when the headline is no longer what stops
      the launch -- get_marketing_state's setup.checklist says what does now.
    publish and unpublish take two calls: the first says what will change; after the owner agrees, the same call
    (for publish, with the same token or version) and owner_confirmed: true does it. The yes confirms that page and
    no other. Use a new idempotency_key per call."""
    if action == "write":
        return dispatch("create_page", template=template, brief=brief)
    if action == "revise":
        _need("revise", instruction=instruction)
        return dispatch("revise_page", instruction=instruction)
    if action == "edit":
        if not section or text is None:  # an empty text is an edit: update_page puts the business's own hours back
            raise ToolError("edit needs section and text")
        return dispatch("update_page", section=section, text=text)
    if action == "use_url":
        _need("use_url", url=url)
        _no_placeholders(url)
        return dispatch("set_landing_page", url=url, idempotency_key=idempotency_key)
    if action == "preview_html":
        _need("preview_html", html=html)
        return dispatch("preview_page", html=html, title=title)
    if action == "upload_site":
        return dispatch("request_site_upload")
    if action == "publish" and owner_confirmed:
        # the yes confirms the page the owner was shown: the proposal for this token or this version, never merely
        # the newest one that waits (spec §4.4)
        if bool(token) == (version is not None):
            raise ToolError(SAME_PAGE)
        page = {"token": token} if token else {"version": version}
        w = where(scope="manage", publish=page)
        waiting = w.publish_proposal
        # nothing waiting: the first answer of this same yes while the hosted page is up at the version that yes
        # published, else "nothing is waiting" to a caller the tool knows
        earlier = earlier_confirmation(
            waiting,
            stands=w.hosted_page_live,
            confirmed=_published_the_live_version(w),
            known=w.business_id is not None,
            nothing=NOTHING_WAITING,
            idempotency_key=idempotency_key,
        )
        if earlier is not None:
            return earlier
        return dispatch(
            "confirm_proposal", proposal_id=(waiting or {}).get("proposal_id") or "", idempotency_key=idempotency_key
        )
    if action == "unpublish" and owner_confirmed:
        # one hosted page per business: the newest request is the one; with none, the first answer of this same yes
        # while the hosted page is down
        w = where(scope="manage")
        waiting = w.proposal("unpublish_page")
        earlier = earlier_confirmation(
            waiting,
            stands=not w.hosted_page_live,
            known=w.business_id is not None,
            nothing=NOTHING_WAITING,
            idempotency_key=idempotency_key,
        )
        if earlier is not None:
            return earlier
        return dispatch(
            "confirm_proposal", proposal_id=(waiting or {}).get("proposal_id") or "", idempotency_key=idempotency_key
        )
    if action == "publish":
        return dispatch("publish_page", idempotency_key=idempotency_key, token=token, version=version)
    if action == "unpublish":
        return dispatch("unpublish_page", idempotency_key=idempotency_key)
    # A3p-5 batch 2 (plan Q7, Q8): the web person's e-mail and Admiry's team, two of the four ways to Admiry's code on
    # the owner's own website; each command acts on the caller's own business only and refuses in a sentence
    if action == "send_install_steps":
        _need("send_install_steps", email=email)
        return dispatch("send_install_steps", email=email, idempotency_key=idempotency_key)
    if action == "request_install_help":
        return dispatch("request_install_help", idempotency_key=idempotency_key, contact=contact)
    # A3p-6 batch 2 (plan Q15): the website Admiry builds for a home-services business or a dental practice. Every
    # argument is optional -- a call with none asks the next fact or the look -- and each is checked, with the facts, by
    # the command; it acts on the caller's own business only. Like `write`, it takes no idempotency key: a repeat is a
    # new preview
    if action == "build_site":
        return dispatch("build_site", style=style, accent=accent, facts=facts, wordmark=wordmark)
    if action == "fix_headline":
        # follow-up 2026-10-12: the home's own "Fix the headline" body, shared (blockers.run_headline_fix) -- the
        # checklist's own item for the fix_headline action is found the way home_api.api_blocker's own fallback
        # does (this action takes none of its own); `site` runs tools_sites.fix_headline directly, under its own
        # command machinery (run("build_site", …): audit, lock, rate -- the same one `build_site`'s own calls
        # share), translated into public tool names as `dispatch` would; `starter` dispatches update_page as
        # "edit" does
        settings = config.load()
        w = where(scope="manage")
        if w.business_id is None:
            # review round 1, I1: a caller `where` cannot resolve -- an unknown key, one without "manage" scope, or
            # no key at all -- never reaches run(), so it would otherwise be refused here in the wrong words
            # (blockers.PLAN_MOVED, from a plan_id of None) and leave no audit row. Hand off to a real command so
            # it authenticates again and refuses in its own words, with its own denied row recorded (as the
            # owner's-yes branches above do with no proposal waiting: dispatch("confirm_proposal", proposal_id=
            # "")) -- tools_sites.fix_headline's own auth check runs before prepare_build, so an empty headline
            # here is never read
            return actions.translate(tools_sites.fix_headline(""))
        with db.connect(settings.database_url) as conn:
            listed = (launch_check.check(conn, settings, w.business_id) or {}).get("items") or []
        found = next((i for i in listed if any(a["id"] == blockers.FIX_HEADLINE["id"] for a in i["actions"])), None)
        if found is None:
            # review round 1, M2: no claimed plan_id to check here (this action takes none) -- without an item,
            # there is nothing to read one from either, and asking blockers.run_headline_fix to decide would only
            # read blockers.PLAN_MOVED out of a plan_id of None, never the true reason (an approved plan, or none)
            raise ToolError(blockers.NO_HEADLINE_FIX)
        _headline, env = blockers.run_headline_fix(
            settings,
            w.business_id,
            found,
            found["plan_id"],  # the item's own -- the latest draft's, since `found` was just read fresh from it
            site=lambda h: actions.translate(tools_sites.fix_headline(h)),
            starter=lambda h: dispatch("update_page", section="headline", text=h),
        )
        return env
    raise ToolError(f"action must be one of {', '.join(PAGE_ACTIONS)}")


@public_tool(cls="RO", scope="read", commands=("get_balance", "list_payments", "request_payment_link"))
def get_billing(
    view: str = "position", amount_usd: float | None = None, cursor: int | None = None, limit: int = 50
) -> dict:
    """The money side when Admiry runs ads from the owner's prepaid balance. view "position": the balance. view
    "payments": what was added and returned, newest first (cursor and limit page through it). view "topup_link"
    (needs the manage scope): the link where the owner adds funds later (the first payment's link is
    connect_account's, for Admiry's way), with the allowed range -- or, until the owner can pay, no link and a note
    that says what comes first (the page for the ads, while it is not settled: the page question and its ways, or the
    step that publishes a page already made, as next); amount_usd, when given, is only checked against that range.
    While the owner has not chosen how the ads run, every view changes nothing and answers the question to put to
    them, as connect_account does (ask_owner, choices, next: the page first while it is not settled). A business
    whose ads run in the owner's own account pays the platform directly and has no balance: the answer says so.
    view "topup_link" also carries `prices` and `price_note`: what that amount costs by card and, when Admiry offers it,
    by bank transfer. Each price includes a transaction fee -- what Stripe charges to process the payment, passed on at
    cost and not refunded; Admiry adds no fee. Relay price_note as written; say nothing else about fees. USD."""
    if view not in BILLING_VIEWS:
        raise ToolError("view must be position, payments or topup_link")
    if view == "payments":
        return dispatch("list_payments", cursor=cursor, limit=limit)
    if view == "topup_link":
        return dispatch("request_payment_link", amount_usd=amount_usd)
    return dispatch("get_balance")


@public_tool(cls="RO", scope="read", commands=("get_audit_log",))
def get_activity_log(cursor: int | None = None, limit: int = 50) -> dict:
    """What happened on this business, newest first, as plain sentences ("Muse built plan v2 from the
    consultation", "Claude paused the ads"): what Admiry ran is there, reads and the refusals it gave too. Use it to
    answer "what did you change?". Page with next_cursor; limit at most 100."""
    return dispatch("get_audit_log", cursor=cursor, limit=limit)


passthrough("contact_support")
# A3p-6 batch 2 (plan Q15): the looks Admiry builds a website in, a read tool as it is
passthrough("get_site_templates")
# Capability layer K4: the Researcher's work, published as it is (the command's words are the tool's)
passthrough("get_research")
passthrough("request_research")
