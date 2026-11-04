"""Admiry's code on the owner's own website (A3p-5 batch 2; spec §3, W7-W12): each platform's steps (`STEPS`, plan Q2),
the steps for one business's site (`steps_for`, `platform_of`), the kit's `install` part (`kit_part`), the plan's "Your
website" row (`row`, Q5) and the four ways to get the code there (`WAYS`, Q6), and what reads the row: the status's
`website` block (`status`), what Check now runs (`check_now`), the homepage card's steps (`steps_view`) and the plan
e-mail's section (`email_section`). Two of the ways are this module's too (Task 3): the web person's e-mail
(`send_steps`, Q7, W11) and Admiry's team (`ask_team`, Q8, W12) -- the commands send_install_steps and
request_install_help run them, each only while the row needs the code. Task 4: the row reads what the hourly page-check
last saw of the code on the owner's page (`code_found`, ruling R0-d), the words for the code gone (`CODE_GONE`, plan
Q9), and the platform a check's fetch read is recorded once (`record_platform`, ruling R1-c). The final review: a saved
check that finds the code fulfils the owner's asks for it, which ends their hourly watch (`fulfil`, ruling F-1); Admiry's
team's ticket carries the page and the code to paste (`ask_team`, F-2); a website on file without a scheme is read as
https:// (`_address`, F-3); and the help reply's contact stays out of the ledger (`HELP_RECORDED`, F-4).

The steps were checked on 2026-10-05 against each vendor's own help page (Task 1's report): every entry's `help_url`
is the page its steps come from, and every staff line in `invite` names the page it comes from. Since the spec's own
check, Squarespace's Code Injection moved under Pages → Custom Code (spec §3.3 still says Settings); Wix's All pages
choice loads the code once per visit (Admiry's script listens on the whole page, so a load on each page would count a
tap twice); and Webflow's head code goes live once the site is published.

GoDaddy Websites + Marketing (Q3): GoDaddy's own help says an HTML section's embed "is placed inside of an iframe",
and a published site's HTML section is an `<iframe srcDoc>` holding the code apart from the page's own markup, where
Admiry's script cannot see the page's buttons and forms. So its steps say so and send the ads to an Admiry page
(`in_frame`: the plan's row offers that way first). An owner-facing sentence here never names a password (W12).

The row judges the page the ads use, never a site the ads do not use (ruling R2-b): a page Admiry hosts is
`admiry_page`; an owner page, on any host, on which Admiry last saw the code is `found` (`code_found`: what the hourly
check last saw there, else the page's saved check; ruling R0-d); otherwise `needs_code`, with the website's platform
steps while the ads have no page or theirs is on the website, and `other`'s for an owner page somewhere else, whose
builder Admiry never read."""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

from . import business, config, db, envelope, mail, mail_grants, plans, profile, site_platform
from .util import iso

log = logging.getLogger(__name__)

PLACEMENTS = ("head", "page_section")
KIT_KEYS = ("platform", "name", "steps", "requirement", "help_url", "placement")  # the kit's part, with the code
HOSTED = ("hosted", "admiry")  # the page sources Admiry hosts (hosting_minimum.HOSTED)
# Q11's words (spec §3.1): the row's three sentences, Check now's answer when the check passes, and the steps view's
# first line
NEEDS_CODE = (
    "Your website needs Admiry's code before the ads can run. It lets Google count calls and forms from your site."
)
FOUND = "Admiry's code is on your site."
ADMIRY_PAGE = "Your ads use your Admiry page; your website stays as it is."
FOUND_IT = "Found it. Your ads can run."
HOW_TO = "Here is how to add it on {name}."
# the fourth way's words (spec §3.1; ruling R2-f): the homepage says them when it is chosen, and so does Admiry's own
# page written for a business with a website of its own (update_landing_page's "write")
ADMIRY_PAGE_WAY = "Your website stays as it is. Admiry builds a page for your ads."
# The web person's e-mail (Q7, W11): its subject and last sentence, and the reply (Q11's words). At most SENDS_PER_DAY
# a business in the last 24 hours -- a rolling day, as support's own daily limit and the per-key limits count; the
# per-business model cap counts from midnight in the database's zone, and no limit counts in the business's own zone
SENDS_PER_DAY = 3
INSTALL_SUBJECT = "How to add Admiry's code to {business}'s website"
CHECKED_HOURLY = "Admiry checks the site every hour and tells {owner} when the code shows up."
SENT = "Sent to {address}. We'll tell you when the code shows up."
# what the ledger keeps of that reply, and so what a replay of the call answers: never the address (ruling R3-e)
SENT_RECORDED = "Sent to your web person. We'll tell you when the code shows up."
# its refusals, each said whole, and nothing sent: the e-mail channel's own words for an address its rule refuses
# (mail_web), the owner's own address, a fourth in a day, a business nobody signed in for (no address to reply to), a
# send that did not go through, a server with no e-mail
NOT_AN_ADDRESS = "That is not an e-mail address."
OWN_ADDRESS = "That is your own address; give your web person's address instead."
TOO_MANY = f"Admiry sends the steps at most {SENDS_PER_DAY} times a day; try again tomorrow."
NO_OWNER = "Sign in with Google first, so your web person can reply to you."
NOT_SENT = "The e-mail did not go out; try again in a few minutes."
NO_MAIL = "E-mail is not set up on this server, so nothing was sent."
# either way for a business with no website of its own (a row that needs no code refuses in its own sentence)
NO_SITE = "There is no website on file for the business."
# Admiry's team (Q8, W12): the ticket's words; the reply, whose promise and helper sentence follow
# AGENT_SITE_HELP_PROMISE and AGENT_SITE_HELPER_EMAIL; with no address to reach the owner at, the ask for one, which
# claims nobody will be in touch before there is one; and the staff's own line in the ticket
HELP_WORDS = "Install Admiry's code on {site} ({platform})"
HELP_SAY = "Our team will contact you at {contact} {promise}."
# what the ledger keeps of that reply, and so what a replay of the call answers: never the address, which may be
# anyone's (A3p-5 batch 2 final review, ruling F-4; the web person's address is kept out the same way, R3-e)
HELP_RECORDED = "Our team will contact you {promise}."
HELP_SOON = "soon"
HELP_HELPER = "They'll ask you to add {helper} as a helper on {platform}, never for your password."
HELP_CONTACT = "Admiry's team needs an e-mail address to reach you: send one, and your request goes to them."
STAFF_NEVER = "Never ask the owner for a password: they add the helper in the platform's own settings."
# The code gone from the owner's page (plan Q9, spec W15; Q11's subject): the e-mail page-check sends once per change,
# whose last sentence links to the steps (`steps_link`), and the status's `say` while it stays gone (`code_gone`)
CODE_GONE = "Admiry's code is no longer on {host}. Calls and forms from your site are not being counted."
CODE_GONE_SUBJECT = "Admiry's code is missing from {host}"
# The code shown up on a pending install's page while the check's report stayed unsent, its result unchanged (ruling
# R4-a): the watch's own e-mail, once (jobs._watch)
CODE_ON = "Admiry's code is on {host} now. Calls and forms from your site will be counted."
CODE_ON_SUBJECT = "Admiry's code is on {host}"
# Q6's four ways, as an assistant's `next` offers them before the launch actions: the public tool and the view or
# action that picks each. Send and help are update_landing_page's actions send_install_steps and request_install_help
# (Task 3, ruling R0-f); help is offered only while support is on (`ways_for`, ruling R3-f).
WAYS: list[dict] = [
    {"id": "install_self", "label": "I'll add it", "tool": "get_landing_page", "args": {"view": "kit"}},
    {
        "id": "install_send",
        "label": "Send it to my web person",
        "tool": "update_landing_page",
        "args": {"action": "send_install_steps"},
    },
    {
        "id": "install_help",
        "label": "Have Admiry's team do it",
        "tool": "update_landing_page",
        "args": {"action": "request_install_help"},
    },
    {
        "id": "admiry_page",
        "label": "Use an Admiry page instead",
        "tool": "update_landing_page",
        "args": {"action": "write"},
    },
]


def _needs(name: str, requirement: str) -> str:
    """Q11's requirement line, said before the steps: "{Platform} lets you add code once {requirement}."."""
    return f"{name} lets you add code once {requirement}."


STEPS: dict[str, dict] = {
    "wix": {
        "name": "Wix",
        "steps": [
            (
                "In your site's dashboard, go to Settings → Custom Code (in Development & integrations) → + Add Custom "
                "Code."
            ),
            "Paste Admiry's code and name it Admiry.",
            "Under Add Code to Pages, choose All pages and load the code only once per visit.",
            "Under Place Code in, choose Head, then click Apply.",
        ],
        "requirement": _needs("Wix", "the site is published and has its own domain, which needs a Premium plan"),
        "help_url": "https://support.wix.com/en/article/embedding-custom-code-on-your-site",
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Roles & Permissions → Invite Collaborators: the helper's e-mail and a role that can add custom code, "
                "such as Admin (Co-Owner), then Send Invites; a Wix site has one free collaborator seat. "
                "https://support.wix.com/en/article/inviting-people-to-contribute-to-your-site"
            ),
        ],
    },
    "squarespace": {
        "name": "Squarespace",
        "steps": [
            (
                "Go to Pages → Custom Code (at the bottom of the panel) → Code Injection. On a version 7.0 site it is "
                "Pages → Website Tools → Code Injection."
            ),
            "Paste Admiry's code in Header, then click Save.",
        ],
        "requirement": _needs(
            "Squarespace", "the site has a Core, Plus, Advanced or legacy plan that includes code injection"
        ),
        "help_url": "https://support.squarespace.com/hc/en-us/articles/205815908-Using-code-injection",
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Permissions & Ownership → Invite Contributor: the helper's name and e-mail and the Administrator "
                "permission (only owners and administrators can add code injection), then Invite. "
                "https://support.squarespace.com/hc/en-us/articles/206537287-Invite-a-contributor · "
                "https://support.squarespace.com/hc/en-us/articles/206537297-Contributor-roles-and-permissions"
            ),
        ],
    },
    "wordpress_com": {
        "name": "WordPress.com",
        "steps": [
            "Install and activate the free Insert Headers and Footers plugin by WPCode.",
            "In your site's dashboard, go to Code Snippets → Header & Footer.",
            "Paste Admiry's code in Header, then click Save Changes.",
        ],
        "requirement": _needs("WordPress.com", "the site has a Personal, Premium, Business or Commerce plan"),
        "help_url": "https://wordpress.com/support/adding-code-to-headers/",
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Users → All Users → Add a team member: the helper's e-mail and the Administrator role, then Send "
                "invitation; the helper accepts with a WordPress.com account of their own. "
                "https://wordpress.com/support/invite-people/"
            ),
        ],
    },
    "wordpress": {
        "name": "WordPress",
        "steps": [
            "In your WordPress dashboard, install and activate the free Insert Headers and Footers plugin by WPCode.",
            "Go to Code Snippets → Header & Footer, paste Admiry's code in Header, then click Save Changes.",
            "Or paste it into your theme's header, just before </head>.",
        ],
        "requirement": None,
        "help_url": "https://wordpress.org/plugins/insert-headers-and-footers/",
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Users → Add New User: the helper's e-mail, the Administrator role and Send User Notification, then "
                "Add New User; WordPress e-mails the helper a link to set up their own sign-in. "
                "https://wordpress.org/documentation/article/users-add-new-screen/"
            ),
        ],
    },
    "godaddy": {
        "name": "GoDaddy Websites + Marketing",
        "steps": [
            (
                "Your site's builder keeps added code in a frame, where it can't count calls and forms. Use an Admiry "
                "page for your ads instead."
            ),
        ],
        "requirement": None,
        "help_url": "https://www.godaddy.com/help/troubleshoot-html-or-custom-code-not-displaying-properly-28025",
        "placement": "page_section",
        "in_frame": True,
        "invite": [
            (
                "Delegate Access → Invite to Access: the helper's name and e-mail and the Products & Domains access "
                "level, then Invite; the helper needs a GoDaddy account. "
                "https://www.godaddy.com/help/invite-a-delegate-to-access-my-godaddy-account-12376"
            ),
        ],
    },
    "shopify": {
        "name": "Shopify",
        "steps": [
            "In Shopify, go to Online Store → Themes, click … beside your current theme, then Edit code.",
            "Open theme.liquid (under Layout), paste Admiry's code just before </head>, then click Save.",
            "Don't use Customer events: code there runs apart from the page and can't see its buttons and forms.",
        ],
        "requirement": None,
        "help_url": (
            "https://help.shopify.com/en/manual/online-store/themes/customizing-themes/edit-code/edit-theme-code"
        ),
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Settings → Users → Security: the owner gives the 4-digit collaborator request code, Admiry's Shopify "
                "Partner account sends the request, and the owner accepts it under Settings → Users (Requests) with a "
                "role that can edit themes; collaborators don't count toward the store's users. "
                "https://help.shopify.com/en/manual/your-account/users/security/collaborator-accounts"
            ),
            (
                "On a plan that can add users (Basic and Starter can't): Settings → Users → Add users, the helper's "
                "e-mail and a role that can edit themes, then Save. "
                "https://help.shopify.com/en/manual/your-account/users/invite-users"
            ),
        ],
    },
    "webflow": {
        "name": "Webflow",
        "steps": [
            "Go to Site settings → Custom code.",
            "Paste Admiry's code in Head code, then click Save changes.",
            "Publish the site: added code goes live only once the site is published.",
        ],
        "requirement": _needs(
            "Webflow", "the site has a paid Site plan, or its Workspace has a Core, Growth, Agency or Freelancer plan"
        ),
        "help_url": "https://help.webflow.com/hc/en-us/articles/33961357265299-Custom-code-in-head-and-body-tags",
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Dashboard → Team → Users → Agency or Freelancer Guests → Invite guest: free for the owner; the "
                "guest's own Workspace needs a Freelancer or Agency plan. "
                "https://help.webflow.com/hc/en-us/articles/"
                "40787895879059-Guide-for-clients-of-agencies-and-freelancers"
            ),
        ],
    },
    "square_online": {
        "name": "Square Online",
        "steps": [
            "In Square Dashboard, go to Channels → Square Online → Settings → Tracking Tools.",
            "Click Add new code, name it Admiry, paste Admiry's code and choose head, then click Save.",
            "Publish your site from the site editor.",
        ],
        "requirement": None,
        "help_url": "https://squareup.com/help/us/en/article/6957-add-custom-tracking-code-to-your-website",
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Square Dashboard → Staff → Team → Team members → Add team member, with a permission set that has "
                "Full access: today only full access can edit Square Online. "
                "https://squareup.com/help/us/en/article/8356-add-and-manage-team-members · "
                "https://community.squareup.com/t5/Online-Store/"
                "How-can-I-give-my-employees-team-members-access-to-my-Square/m-p/193290"
            ),
        ],
    },
    "other": {
        "name": "your site",
        "steps": [
            "Paste Admiry's code inside <head> on every page, or before </body> where your site allows only that.",
        ],
        "requirement": None,
        "help_url": None,
        "placement": "head",
        "in_frame": False,
        "invite": [
            (
                "Ask whoever runs the site to add the helper's e-mail as a user who can edit the site's code, in the "
                "site's own team or user settings."
            ),
        ],
    },
}


def steps_for(platform: str | None) -> dict:
    """The steps for `platform` (one of site_platform.PLATFORMS): its STEPS entry with the platform it is for, as a
    copy a caller may change -- `other`'s for any platform it does not know or None (a site never read, Q13; one whose
    platform could not be told, Q1), never another platform's."""
    key = platform if isinstance(platform, str) and platform in STEPS else "other"
    entry = STEPS[key]
    return {"platform": key, **entry, "steps": list(entry["steps"]), "invite": list(entry["invite"])}


def platform_of(knowledge: dict | None) -> str | None:
    """The platform the business's own site is built on (`website_platform`), or None: none on file, or no site of its
    own. The reading stores it from the crawl's first page (Q1); a saved check's fetch of the owner's own site records
    it once there is none (`record_platform`, ruling R1-c) -- for an address the owner gave that Admiry could not read
    too (`website_unread`), whose steps are `other`'s until a check fetches it (Q13)."""
    k = knowledge or {}
    return k.get("website_platform") if profile.own_site(k) else None


class _Moved(Exception):
    """record_platform's write landed on top of a version it did not read: rolled back."""


def record_platform(conn, biz: str, report: dict | None) -> str | None:
    """Ruling R1-c (spec W9: the platform is read from the HTML Admiry already fetched -- the reading's crawl, the
    check's fetch; Q13: a site never read has its platform once a check fetches it): the platform a saved check's fetch
    read (`fetched.platform`, landing.check) becomes the knowledge's `website_platform`, through a new knowledge
    version (source "check") -- once: only while none is stored (ruling R4-c: the crawl's `other` -- no signature, or
    two -- is never replaced by a reading of another page); only a platform the page named, never `other`; only for a
    page on the owner's own site (profile.own_site, listings.same_site: the host, `www.` aside), never one elsewhere,
    whose builder is not the site's. The commands that save a check of the page on file call it (set_landing_page,
    check_landing_page without an address), and so does the hourly page-check through their steps when it records a
    pending install's first pass (tools_pages.check_on_file, name_page; ruling R3-a) -- never a dry run, and never its
    hourly look itself. The new version is the one read plus the platform, so it is written only on top of that one:
    another version landing in between (the owner's own change) wins, and nothing is recorded -- the next saved check
    tries again; the caller's check stands either way. Returns the platform recorded, else None."""
    from . import listings  # it imports consult, geocode and auth: read when a check is saved

    platform = ((report or {}).get("fetched") or {}).get("platform")
    if platform not in site_platform.PLATFORMS or platform == site_platform.OTHER:
        return None
    try:
        with conn.transaction():
            latest = business.latest_knowledge(conn, biz)
            version, knowledge = latest if latest else (0, {})
            site = profile.own_site(knowledge)
            if not site or knowledge.get(profile.PLATFORM) is not None:
                return None
            if not listings.same_site((report or {}).get("url"), site):
                return None
            written = business.add_knowledge(conn, biz, {**knowledge, profile.PLATFORM: platform}, source="check")
            if written != version + 1:
                raise _Moved
    except (ToolError, _Moved):  # another version landed meanwhile: never written over
        log.info("site platform: business %s's knowledge moved meanwhile; %s not recorded", biz, platform)
        return None
    return platform


def install_part(platform: str | None, snippet: str) -> dict:
    """The steps for `platform` as an owner reads them -- the kit's `install` keys (spec §4) -- and the code they put
    there. The staff's invitation steps are not in it: they are for Admiry's team (Q8)."""
    steps = steps_for(platform)
    return {**{key: steps[key] for key in KIT_KEYS}, "snippet": snippet}


def kit_part(knowledge: dict | None, snippet: str, page: dict | None = None) -> dict | None:
    """The kit's `install` part (spec §4; `get_landing_page` with view "kit", where "I'll add it" points): the steps the
    row shows (`_steps_platform`, ruling R2-b; `page` is the business's one page row, as the kit has it) and the code
    they put there. None for a business with no website of its own (ruling R1-f), whose row is None too."""
    k = knowledge or {}
    if not profile.own_site(k):
        return None
    return install_part(_steps_platform(k, page), snippet)


def ways_for(platform: str | None) -> list[dict]:
    """The ways offered, in the order an owner reads them, as copies a caller may change: Q6's four in WAYS' order, the
    fourth first for a platform whose builder keeps added code in a frame (`in_frame`: GoDaddy, Q3) -- without "Have
    Admiry's team do it" while support is off (AGENT_SUPPORT, ruling R3-f): no ticket could reach a person, so it is
    offered nowhere, not in the row, `next`, the homepage card or the plan e-mail, which all read these."""
    support = config.load().support
    ways = [{**way, "args": dict(way["args"])} for way in WAYS if support or way["id"] != "install_help"]
    if steps_for(platform)["in_frame"]:
        ways.insert(0, ways.pop())
    return ways


def _knowledge(conn, biz: str) -> dict:
    latest = business.latest_knowledge(conn, biz)
    return latest[1] if latest else {}


def _used(page: dict | None) -> dict | None:
    """The page the ads use (ruling R2-b): the business's one page row once it has an address -- the kit's placeholder,
    with none yet, is no page."""
    return page if page and page.get("url") else None


def code_found(page: dict | None) -> bool:
    """Whether Admiry's code is on `page` as Admiry last saw it (plan Q5; ruling R0-d): on an owner page, what the
    hourly page-check last saw there (`code_seen`, plan Q9) once it has looked since the page's last saved check, else
    that check's tracking rule (`checked_found`) -- so after the hourly check saw the code gone, the row never says it
    is on the site. page-check records the code seen only where that check found it (jobs.page_check), so a `code_seen`
    stands only beside a check that found the code, and the launch, which reads the check, agrees with the row. A page
    Admiry hosts is judged by its check alone."""
    if page and page.get("source") == "owner" and page.get("code_seen") is not None:
        return bool(page["code_seen"])
    return checked_found(page)


def checked_found(page: dict | None) -> bool:
    """Whether `page`'s saved check found Admiry's code on it: its tracking rule passed (plan Q5)."""
    from . import landing  # it imports this module at load time: read when a page is judged

    return tracking_passed(landing.saved_report(page) if page else None)


def tracking_passed(report: dict | None) -> bool:
    """Whether a check report's tracking rule passed: Admiry's code with the page's token was on the page."""
    return any(r.get("id") == "tracking" and r.get("ok") is True for r in (report or {}).get("rules") or [])


def fulfil(conn, biz: str, report: dict | None) -> int:
    """A3p-5 batch 2 final review, ruling F-1: a saved check that found Admiry's code (`tracking_passed`) on the page
    the ads use fulfils the business's asks for the code still open -- `install_request.found_at`, set once. The
    commands' own steps that save a check of that page run it (tools_pages.check_on_file, name_page): the owner's own
    Check now, an assistant's check_landing_page or set_landing_page, and the hourly watch when it records a pending
    install's first pass (jobs._watch, ruling R3-a). The watch reads only the asks not fulfilled (jobs._watched), so it
    ends for good at the first pass, and its own "Admiry's code is on {host}" e-mail (ruling R4-a) goes at most once an
    ask; a code that comes and goes after it is the code-gone e-mail's (ruling R4-b). Returns how many it fulfilled."""
    if not tracking_passed(report):
        return 0
    return conn.execute(
        "UPDATE install_request SET found_at = now() WHERE business_id = %s AND found_at IS NULL", (biz,)
    ).rowcount


def code_gone(page: dict | None) -> str | None:
    """The status's sentence while Admiry's code is gone from the owner's page (plan Q9, spec W15): CODE_GONE for the
    page's host once the hourly check saw it go (`code_seen` false: page-check records it only where the code had been
    seen, after two hourly looks in a row without it, ruling R4-b), else None."""
    if not page or page.get("source") != "owner" or page.get("code_seen") is not False:
        return None
    return CODE_GONE.format(host=urlparse(page.get("url") or "").hostname or page.get("url"))


def steps_link() -> str:
    """Where the code-gone e-mail sends the owner to add the code again: the homepage's "I'll add it" behind sign-in
    (the plan e-mail's way links, ruling R2-c), which opens the steps and the code."""
    return f"{config.load().web_base_url}/?website=install_self"


def _steps_platform(knowledge: dict, page: dict | None) -> str | None:
    """Whose steps an owner is shown (ruling R2-b): the website's platform (`platform_of`), unless the ads use an owner
    page somewhere else -- one with an address that is not on the website (listings.same_site: the host, `www.` aside)
    -- whose builder Admiry never read: None then, `other`'s steps. A page Admiry hosts already carries the code, so the
    owner's own site is still the website."""
    from . import listings  # it imports consult, geocode and auth: read when the steps are wanted

    used = _used(page)
    if used and used.get("source") == "owner" and not listings.same_site(used["url"], profile.own_site(knowledge)):
        return None
    return platform_of(knowledge)


def _row(knowledge: dict, page: dict | None) -> dict:
    """`row` for a business with a website of its own, from its knowledge and its one page row (rulings R2-b, R2-c).
    The homepage card's line (spec 2026-10-12 §3.5): `url`, the page the ads use, else the website, and
    `platform_name`, the builder it is on as the owner reads it -- None when Admiry cannot tell (`other`'s steps)."""
    if page is not None and page.get("source") in HOSTED:
        return {"state": "admiry_page", "say": ADMIRY_PAGE}
    used, steps = _used(page), steps_for(_steps_platform(knowledge, page))
    where = {
        "url": used["url"] if used else profile.own_site(knowledge),
        "platform_name": None if steps["platform"] == "other" else steps["name"],
    }
    if code_found(used):
        return {"state": "found", "say": FOUND, **where}
    return {
        "state": "needs_code",
        "say": NEEDS_CODE,
        "platform": steps["platform"],
        **where,
        "steps": steps["steps"],
        "ways": ways_for(steps["platform"]),
    }


def row(conn, biz: str) -> dict | None:
    """The plan's "Your website" row (plan Q5, spec W7): None for a business with no website of its own (neither
    `website` nor `website_unread`, Q13 -- the page card stands for it); `{"state": "admiry_page", "say"}` when the ads
    use a page Admiry hosts; `{"state": "found", "say", "url", "platform_name"}` when the ads use an owner page, on any
    host, on which Admiry last saw the code (ruling R2-b; `code_found`: what the hourly check last saw there, else the
    page's saved check, ruling R0-d); else `{"state": "needs_code", "say", "platform", "url", "platform_name", "steps",
    "ways"}` -- the page the ads use and its builder's name (`_row`), the steps the owner follows (the website's, or
    `other`'s for an owner page elsewhere) and the ways offered in their order (ruling R2-c; `ways_for`: Admiry's team
    only while support is on, R3-f). plans.cards puts it in the plan's `setup["website"]`."""
    from . import landing  # it imports this module at load time: read when a row is built

    knowledge = _knowledge(conn, biz)
    if not profile.own_site(knowledge):
        return None
    return _row(knowledge, landing.current(conn, biz))


def status(conn, biz: str) -> dict | None:
    """get_landing_page's status `website` block (spec §4): the owner's own site (`url`), the platform it is built on
    (the generic steps' `other` when it cannot be told), whether the page the ads use carries the code and when it was
    last checked (`code`, `checked_at`: rulings R2-b, R2-c), and `way` (ruling R0-c) -- "admiry_page" while the ads use
    a page Admiry hosts, else the way the owner last asked for the code by ("send" or "help", `_latest_way`), else
    null. None with no website of its own."""
    from . import landing  # it imports this module at load time: read when a block is built

    knowledge = _knowledge(conn, biz)
    site = profile.own_site(knowledge)
    if not site:
        return None
    page = landing.current(conn, biz)
    used = _used(page)
    return {
        "url": site,
        "platform": steps_for(platform_of(knowledge))["platform"],
        "code": "found" if code_found(used) else "missing",
        "checked_at": iso(used["checked_at"]) if used and used.get("checked_at") else None,
        "way": "admiry_page" if _row(knowledge, page)["state"] == "admiry_page" else _latest_way(conn, biz),
    }


def _latest_way(conn, biz: str) -> str | None:
    """The way the owner last asked for Admiry's code by (ruling R0-c): the newest install_request's, "send" or "help";
    None before any."""
    found = conn.execute(
        "SELECT way FROM install_request WHERE business_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (biz,)
    ).fetchone()
    return found["way"] if found else None


def check_now(conn, biz: str) -> tuple[str, str] | None:
    """What Check now runs (Q6), as (command, address): with an owner page the ads use, ("check_landing_page", its
    address) -- the page on file checked again as that command checks it, its report e-mailed only when whether it
    passes changed, and ads that run never moved (rulings R2-b, R2-d); with none, ("set_landing_page", the owner's
    website as an address, `_address`: one without a scheme read as https://, ruling F-3), which names it the ads' page
    and e-mails its first report. None for a business with no website of its own, and while the ads use a page Admiry
    hosts (its row has no Check now). The one place the target is read: Check now, the hourly watch of a pending
    install (jobs._watch) and the two ways' site (`_target`) all ask here."""
    from . import landing  # it imports this module at load time: read when Check now is pressed

    site = profile.own_site(_knowledge(conn, biz))
    if not site:
        return None
    page = landing.current(conn, biz)
    if page is not None and page.get("source") in HOSTED:
        return None
    used = _used(page)
    return ("check_landing_page", used["url"]) if used else ("set_landing_page", _address(site))


def _address(site: str) -> str:
    """The owner's own site as an address Admiry fetches (A3p-5 batch 2 final review, ruling F-3): `website` is text a
    model or an assistant wrote, which may come without a scheme ("acme.example", "www.acme.example"), so one without
    is read as https:// -- the rule consult applies to an address in the owner's words (consult.normalize_site) --
    before Check now names it or the watch fetches it, never handed on to be refused as no http(s) address; one with a
    scheme stays as it is."""
    text = site.strip()
    return text if "://" in text else f"https://{text}"


def steps_view(conn, biz: str, found: dict | None) -> dict | None:
    """The homepage card's steps for a row that needs the code (spec §3.1 "I'll add it", Q6): "Here is how to add it on
    {name}." (`how`), the steps of the row's platform and Admiry's code for the business's one page -- the kit's own
    code, the same token (landing.snippet). None for any other row."""
    if not found or found.get("state") != "needs_code":
        return None
    _page, code = _code(conn, biz)
    part = install_part(found["platform"], code)
    return {**part, "how": HOW_TO.format(name=part["name"])}


def _code(conn, biz: str) -> tuple[dict, str]:
    """The business's one page row -- the kit's placeholder, made when there is none yet (landing.ensure_page_for_kit)
    -- and Admiry's code for it: the kit's own code, with the page's token (landing.snippet). For the card's steps
    (`steps_view`), the web person's e-mail and Admiry's team's ticket (`ask_team`, ruling F-2)."""
    from . import landing  # it imports this module at load time: read when the code is wanted

    page = landing.ensure_page_for_kit(conn, biz)
    code = landing.snippet(page["id"], config.load().public_base_url, landing.google_tag(conn, biz))
    return page, code


def email_section(conn, biz: str) -> dict | None:
    """The plan e-mail's YOUR WEBSITE section (spec §3.2; rulings R0-e, R2-c): the row's state and sentence and, while
    the code is missing, the row's ways (ruling R3-f) in its order as links to the homepage's card behind sign-in
    (`{AGENT_WEB_BASE_URL}/?website=<way id>`). The template takes the steps and the code from the kit's `install`
    part, which follows the same page (ruling R2-b). None with no website of its own: no section (ruling R1-f)."""
    found = row(conn, biz)
    if found is None:
        return None
    web = config.load().web_base_url
    ways = [{"id": w["id"], "label": w["label"], "url": f"{web}/?website={w['id']}"} for w in found.get("ways") or []]
    return {"state": found["state"], "say": found["say"], "ways": ways}


# ---------------------------------------------------------------- two of the ways (Task 3: Q7, Q8, W11, W12)


def send_steps(conn, biz: str, email: str) -> str:
    """The web person's e-mail (plan Q7, spec W11): Q7's fixed message -- the business's name, the site the code goes on,
    the steps and the code the homepage's card shows (steps_view, for the page the ads use: rulings R2-b, R2-i) and
    CHECKED_HOURLY -- never a word of the caller's, from Admiry's sender with reply-to the owner's sign-in address, to
    `email` alone. Refused in a sentence (ToolError), nothing sent: an address the e-mail channel's rule refuses
    (mail_grants.valid), a row that needs no code or none (`_needing`), a business nobody signed in for (no address to
    reply to), the owner's own address in any case, a server with no e-mail, a fourth in the last 24 hours
    (SENDS_PER_DAY: counted and recorded under one lock per business, so two sends at once cannot both be the third),
    and a send that did not go through, whose row is taken back so that it does not count.

    The command's `prepare`, outside its transaction: the e-mail goes out here (mcp_app.run: no outside call inside the
    transaction), its install_request row written first and committed, keeping the address only as its SHA-256. Returns
    the address as given, trimmed: for the command's own answer alone, never its audit row (ruling R3-e)."""
    address = email.strip() if isinstance(email, str) else ""
    if not mail_grants.valid(address):
        raise ToolError(NOT_AN_ADDRESS)
    found = _needing(conn, biz)
    owner = _owner(conn, biz)
    if owner is None:
        raise ToolError(NO_OWNER)
    if address.lower() == owner.lower():
        raise ToolError(OWN_ADDRESS)
    if not mail.configured():
        raise ToolError(NO_MAIL)
    site = _target(conn, biz)
    named = _named(_knowledge(conn, biz), site)
    install = steps_view(conn, biz, found)
    text = mail.render(
        "mail_install.txt.j2",
        owner=owner,
        business=named,
        site=site,
        install=install,
        checked=CHECKED_HOURLY.format(owner=owner),
    )
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"install_send:{biz}",))
        sent = conn.execute(
            "SELECT count(*) AS n FROM install_request WHERE business_id = %s AND way = 'send' "
            "AND created_at > now() - interval '1 day'",
            (biz,),
        ).fetchone()["n"]
        if sent >= SENDS_PER_DAY:
            raise ToolError(TOO_MANY)
        request_id = db.new_id("ir")
        conn.execute(
            "INSERT INTO install_request (id, business_id, way, to_hash) VALUES (%s, %s, 'send', %s)",
            (request_id, biz, _hashed(address)),
        )
    try:
        provider_id = mail.send(address, INSTALL_SUBJECT.format(business=named), text, reply_to=owner)
    except mail.MailError:
        provider_id = None  # mail.py logged Resend's refusal, or why it could not be reached
    except Exception:
        _take_back(conn, request_id)
        raise
    if provider_id is None:
        _take_back(conn, request_id)
        raise ToolError(NOT_SENT)
    return address


def ask_team(
    conn, biz: str, contact: str | None, *, source: str, actor: str, ip: str | None, draft: bool
) -> tuple[dict, str]:
    """Admiry's team (plan Q8, spec W12): SP1's ticket (support.open_or_append) with reason `website` and Q8's words for
    the site the code goes on and its platform, its context for the staff (`_staff_context`: the platform's helper
    invitation steps, never a password; the business's page id and the kit's install part -- the steps and the exact
    code to paste -- so the staff install from the ticket, ruling F-2). The business's one page row is made sure of
    first, as the web person's e-mail does (`_code`): the code's token, and the page the hourly watch of the ask looks
    at (jobs._watch, ruling R3-a). Its contact is the address given (the e-mail channel's rule, lower case), else the
    owner's sign-in address -- passed on an append too, so a ticket still waiting for one takes it; SP1's
    acknowledgement and the ops notice go once the command has committed (support_mail.after_tool). With neither, the
    ticket waits in SP1's awaiting_contact and the answer asks for an address (HELP_CONTACT), with this way again as
    `next`, and claims nobody will be in touch; sent again with one, the same ticket takes it and opens. An
    install_request row `help` records the ask (the status's `way`, ruling R0-c).

    Returns (the answer the ledger keeps, the sentence this call answers): Q8's reply names the contact, which may be
    anyone's address, so the kept answer -- what a replay of the call answers -- says it without one (`help_say`,
    ruling F-4). Refused in a sentence (ToolError): support off, an address the rule refuses, a row that needs no code
    or none, a draft visitor's address over SP1's day. Runs in the command's transaction and writes the database
    only."""
    from . import support  # it imports transcript, which imports landing and this module: read when help is asked

    if not config.load().support:
        raise ToolError(support.NOT_AVAILABLE)
    given = None
    if isinstance(contact, str) and contact.strip():
        if not mail_grants.valid(contact.strip()):
            raise ToolError(NOT_AN_ADDRESS)
        given = contact.strip().lower()
    found = _needing(conn, biz)
    steps = steps_for(found["platform"])
    site = _target(conn, biz)
    page, code = _code(conn, biz)  # ruling F-2: the page row made sure of, and the code the staff paste
    words = HELP_WORDS.format(site=site, platform=steps["name"])
    owner = None if source == "email" else _owner(conn, biz)  # an e-mail ticket's contact is its thread's (SP1)
    try:
        out = support.open_or_append(
            conn,
            business_id=biz,
            source=source,
            reason="website",
            words=words,
            summary=words,
            actor=actor,
            contact_email=given or owner,
            context=_staff_context(site, steps, given, page["id"], code),
            ip=ip,
            draft=draft,
        )
    except support.Limited as e:
        raise ToolError(str(e)) from e
    conn.execute("INSERT INTO install_request (id, business_id, way) VALUES (%s, %s, 'help')", (db.new_id("ir"), biz))
    data = {"support": support.public(out)}
    if out["status"] == "awaiting_contact":
        return envelope.make(data, say=HELP_CONTACT, next=envelope.actions(_way("install_help"))), HELP_CONTACT
    return envelope.make(data, say=help_say(None, steps["name"])), help_say(out["contact_email"], steps["name"])


def help_say(contact: str | None, platform: str) -> str:
    """Q8's reply: HELP_SAY with AGENT_SITE_HELP_PROMISE as its promise ("soon" without it), then HELP_HELPER while
    AGENT_SITE_HELPER_EMAIL names the helper -- dropped until the founder sets it (spec §8 item 1). `platform` is the
    steps' name: "Wix", or "your site" for a platform that could not be told. Without `contact`, the sentence the
    ledger keeps (HELP_RECORDED, ruling F-4): the same reply with the address left out."""
    settings = config.load()
    promise = settings.site_help_promise or HELP_SOON
    said = HELP_SAY.format(contact=contact, promise=promise) if contact else HELP_RECORDED.format(promise=promise)
    if settings.site_helper_email:
        said += " " + HELP_HELPER.format(helper=settings.site_helper_email, platform=platform)
    return said


def _needing(conn, biz: str) -> dict:
    """The plan's row while it needs Admiry's code -- the ways exist only then (Q6) -- else refused in a sentence:
    the row's own (the code found, an Admiry page for the ads) or NO_SITE with no website of its own."""
    found = row(conn, biz)
    if found is None:
        raise ToolError(NO_SITE)
    if found["state"] != "needs_code":
        raise ToolError(found["say"])
    return found


def _target(conn, biz: str) -> str:
    """Where the code goes, for a row that needs it: the owner page the ads use, else the owner's own website (Check
    now's address, ruling R2-b)."""
    return check_now(conn, biz)[1]


def _owner(conn, biz: str) -> str | None:
    """The business's owner's sign-in address (Google verified it at the claim), or None for a business nobody signed in
    for."""
    found = conn.execute("SELECT owner_email FROM business WHERE id = %s", (biz,)).fetchone()
    return ((found["owner_email"] or "").strip() or None) if found else None


def _named(knowledge: dict, site: str) -> str:
    """How the web person's e-mail names the business: the name a sentence may lead with (plans.name_for: one a site, a
    listing or the owner vouches for), else the site's host."""
    return plans.name_for(knowledge) or urlparse(site).hostname or site


def _hashed(address: str) -> str:
    """The SHA-256 of an address, trimmed and in lower case: all an install_request row keeps of it (spec §5)."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def _take_back(conn, request_id: str) -> None:
    """A send that did not go through counts toward nothing: its install_request row goes."""
    conn.execute("DELETE FROM install_request WHERE id = %s", (request_id,))


def _way(way_id: str) -> dict:
    """One of Q6's four ways, as a copy a caller may change."""
    way = next(w for w in WAYS if w["id"] == way_id)
    return {**way, "args": dict(way["args"])}


def _staff_context(site: str, steps: dict, given: str | None, page_id: str, code: str) -> str:
    """The ticket's context for Admiry's team (plan Q8: the context "install"; spec W12): the site and its platform, the
    business's page id (`page_id`, the token Admiry's code carries), how the owner adds a helper there (STEPS'
    `invite`, each line naming the vendor page it comes from), the address to invite (AGENT_SITE_HELPER_EMAIL), the
    kit's install part -- the platform's requirement, its steps and help page, and the exact code to paste (`code`),
    the very code the owner's card shows (ruling F-2) -- and the contact the owner gave; and that a password is never
    asked for."""
    helper = config.load().site_helper_email
    lines = [
        "install",
        f"Site: {site}",
        f"Platform: {steps['name']} ({steps['platform']})",
        f"Page: {page_id}",
        STAFF_NEVER,
        "How the owner adds the helper:",
        *[f"- {line}" for line in steps["invite"]],
        f"Helper's address: {helper or 'not set yet (AGENT_SITE_HELPER_EMAIL)'}",
        "How to add Admiry's code, as the owner's card shows it:",
        *([steps["requirement"]] if steps["requirement"] else []),
        *[f"{n}. {step}" for n, step in enumerate(steps["steps"], 1)],
        *([f"Help: {steps['help_url']}"] if steps["help_url"] else []),
        "Admiry's code to paste:",
        code,
    ]
    if given:
        lines.append(f"Contact the owner gave: {given}")
    return "\n".join(lines)
