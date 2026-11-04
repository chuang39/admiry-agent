"""What an assistant is told (spec 2026-10-01 §9; plan G1 Task 12): the instructions, the one skill and where it is
served, the four prompts, the state resources. Every tool these words name is a public tool."""

import asyncio
import hashlib
import json
import re

from agent import (
    assistant_skill,
    bundles,
    envelope,
    hosted,
    hosting_minimum,
    keys,
    landing,
    leads,
    mcp_app,
    models,
    site_install,
)
from agent.sites import build
from agent.surface import gate, registry, told, words
from agent.tests.conftest import call_tool, rpc, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.surface_helpers import planned

VERBS = "get|update|create|launch|pause|resume|connect|add|ask|contact"
# A token in backticks that reads as a tool: every one must be a public tool (a typo, or a tool that left, fails here)
TOOLISH = re.compile(rf"`((?:{VERBS})_[a-z_]+)`")
BARE = re.compile(rf"\b((?:{VERBS})_[a-z_]+)\b")  # the instructions name tools without backticks
# The names that left the surface and that TOOLISH's verbs do not catch
LEFT = (
    "whoami",
    "start",
    "answer",
    "recommend",
    "adjust",
    "pause",
    "resume",
    "list_leads",
    "list_keys",
    "list_proposals",
    "list_payments",
    "set_goal",
    "set_landing_page",
    "request_connect_link",
    "request_payment_link",
    "approve_plan",
    "confirm_proposal",
    "decline_proposal",
    "describe_business",
    "revise_plan",
    "revise_page",
    "check_landing_page",
    "preview_page",
    "publish_page",
    "unpublish_page",
    "revoke_key",
)
SKILL_URIS = {
    "skill://admiry-marketing/SKILL.md",
    "skill://admiry-marketing/references/after-launch.md",
    "skill://admiry-marketing/references/building-a-page.md",
    "skill://admiry-marketing/references/first-plan.md",
    "skill://admiry-marketing/references/leads-and-outcomes.md",
    "skill://admiry-marketing/references/when-something-goes-wrong.md",
}


def _public() -> set[str]:
    return {m.name for m in registry.SURFACE}


def test_the_instructions_route_and_name_only_public_tools():
    text = words.INSTRUCTIONS
    assert mcp_app.INSTRUCTIONS == text and len(text) <= 1500
    assert text.startswith("Admiry is a marketing agent for local and service businesses")
    assert set(BARE.findall(text)) == {
        "get_marketing_state",
        "create_marketing_plan",
        "update_marketing_plan",
        "ask_admiry",
        "update_lead",
        "add_lead",
    }
    assert set(BARE.findall(text)) <= _public()
    assert "skill://admiry-marketing/SKILL.md" in text and "never guess it" in text
    assert "professional, polite and patient" in text
    # the owner may approve on the page itself: the words never say a code is the only way
    assert "on their approval page: there, or by reading you the code it shows" in text


def test_the_skill_is_one_agent_skill_with_its_references():
    files = assistant_skill.files()
    assert next(iter(files)) == "SKILL.md" and {assistant_skill.uri(p) for p in files} == SKILL_URIS
    front = assistant_skill.frontmatter()
    assert front["name"] == assistant_skill.NAME == assistant_skill.ROOT.name == "admiry-marketing"
    assert front["version"] == gate.SURFACE_VERSION and len(front["description"]) <= 1024
    assert front["description"].startswith(
        "Plan, launch, run and report on advertising for a local or service business"
    )
    body = assistant_skill.body()
    assert not body.startswith("---") and body.startswith("# Working with Admiry")
    # the body says its version too: both places move with the contract (final review C, Important 4a)
    assert f"This guide is version {gate.SURFACE_VERSION}." in body
    for line in ("professional, polite and patient", "Never invent a number", "Never guess one", "exactly as typed"):
        assert line in body, line
    rules = assistant_skill.rules()
    assert body.startswith(rules.rstrip("\n")) and "## The rules that do not bend" in rules
    assert "## The page" not in rules and "## The workflows" not in rules
    for path in files:  # every file the body points to exists
        assert path == "SKILL.md" or f"`{path}`" in body, path


def test_no_file_of_the_skill_names_a_tool_that_is_not_public():
    for path, text in assistant_skill.files().items():
        named = set(TOOLISH.findall(text))
        assert named <= _public(), (path, sorted(named - _public()))
        assert not [old for old in LEFT if f"`{old}`" in text], path


def test_the_skill_says_what_the_tools_do():
    """The skill was checked sentence by sentence against the tools (2026-10-02). These are the places where a first
    draft said more than the code does: the true sentence stays in, the false one stays out."""
    files = assistant_skill.files()
    first, after = files["references/first-plan.md"], files["references/after-launch.md"]
    wrong = files["references/when-something-goes-wrong.md"]
    assert "With nothing begun, `create_marketing_plan`" in first  # called again it starts over
    assert "each answers with a new link and a new `token`" in first  # an older token publishes an older preview
    assert "either approves it there or reads you the code it shows" in first  # the page approves without a code
    assert "send `answers` with that question's id again" in first  # a typed message answers the waiting question
    assert "`image_note`" in first and "`alt` words" in first  # M1's rule on NewsBreak's picture, carried from skill.md
    assert "It does not lift a pause Admiry made itself" in after
    assert "examples of the searches" in after  # the plan shows examples, the ads run on more
    assert "One place that is ready is enough" in first  # a place that is not ready never holds the others back
    assert "for a place that has not started, the one approved for it" in after  # a waiting place may never run it
    assert "does not stop ads that already run" in after  # an empty balance blocks a start, not a running ad
    assert "launching a version that leaves a place out pauses its ads there" in after
    assert "does not try again by itself" in wrong  # a failed launch needs a new version
    assert "takes no key and starts over" in wrong and "makes a new preview each time" in wrong
    assert "toward a goal of customers" in files["references/leads-and-outcomes.md"]
    whole = "\n".join(files.values())
    for false in (
        "every question can be skipped",
        "Admiry puts the budget where",
        "is lifted by Admiry",
        "or will run once it starts",
    ):
        assert false not in whole, false


def test_the_descriptions_say_what_the_tools_do():
    """The 23 descriptions were checked sentence by sentence against the code (2026-10-02). The places where a first
    draft would have cost an owner money or work are pinned."""
    said = {m.name: " ".join((m.tool.description or "").split()) for m in registry.SURFACE}
    assert "either approve it there or read you the approval code" in said["launch_marketing_plan"]
    assert "whether or not it passes the check" in said["update_landing_page"]  # use_url moves running ads at once
    assert "starts over and reads the business again" in said["create_marketing_plan"]
    # a new reading keeps the Google listing on file and looks nothing up (final review B, Important 9)
    assert (
        "the Google listing (the address and the map point the ads centre on: a new reading does not look the business "
        "up again) stay" in said["update_business_profile"]
    )
    assert "pauses its ads there" in said["update_marketing_budget"]  # a version without a place
    assert "the one approved for it" in said["get_campaigns"] and "not on searches" in said["get_campaigns"]
    assert "no funds" not in said["resume_campaign"]  # nothing pauses a place for an empty balance
    assert "replaces them all" in said["update_marketing_plan"]  # channel_caps is one object, not a patch
    # A3p-5 batch 2 (plan Q6, rulings R2-a, R2-c): while the business's own website needs Admiry's code, the build's
    # and a change's `next` offer the ways to put it there ahead of the ways the plan can run
    ways = "after the ways to put Admiry's code on the business's own website while it still needs it"
    assert ways in said["get_marketing_plan"] and ways in said["update_marketing_plan"]
    # capability layer K3 (Task 7): the answer may draw on the reviewed notes the plan cites, as published averages
    notes = "when the plan cites them, from Admiry's reviewed notes on advertising this kind of business"
    assert notes in said["ask_admiry"] and "(published averages, never a promise)" in said["ask_admiry"]


def test_the_managed_way_opens_the_page_where_the_owner_chooses_it():
    """Owner-chooses review, fix round 1 (item 5): mode managed carries no pick of its own -- its answer is the link to
    the setup card, where the owner chooses that way, accepts the terms and adds funds (the link saves nothing). The
    tool's description and the guide's account step both say so, as they did before the founder's ruling of
    2026-10-13."""
    said = " ".join((next(m for m in registry.SURFACE if m.name == "connect_account").tool.description or "").split())
    assert "the page where the owner chooses that way, accepts the terms and adds funds" in said
    first = " ".join(assistant_skill.files()["references/first-plan.md"].split())
    assert (
        '`mode: "managed"` answers the link where the owner chooses that way, accepts the terms and adds funds' in first
    )


def test_the_listing_rules_hold_whichever_question_asks_for_the_place():
    """Ruling F7: the listing question now comes after the website and the name, so the words assistants read tie
    "never pick one for them" and "Admiry links only the place the owner picks" to a question that asks which Google
    Maps place is the business -- never to "the first question" -- and the skill's step 3 describes the website question
    first."""
    plan = next(m for m in registry.SURFACE if m.name == "create_marketing_plan")
    said = " ".join((plan.tool.description or "").split())
    rule = "when a question asks which Google Maps place is the business, Admiry links only the place the owner picks"
    assert rule in said
    first = assistant_skill.files()["references/first-plan.md"]
    step = next(line for line in first.splitlines() if line.startswith("3. "))
    assert "When a question asks which Google Maps place is the business, show each place as a choice" in step
    assert "never pick one for them" in step
    assert step.index("its very first question asks for one") < step.index("For each question")
    assert "the first question asks whether" not in said and "the first question asks whether" not in first


def test_the_skill_tells_the_four_ways_to_admirys_code_as_admiry_words_them():
    """A3p-5 batch 2 (plan Q12, spec §3.2): before launch the owner's own website must carry Admiry's code. SKILL.md's
    page section says so in one paragraph: the four ways as Admiry words them -- Q6's labels in their order, each with
    the call that picks it -- arrive as `next` actions (up to seven in all: ruling R2-a); "Use an Admiry page instead"
    comes first where the site's builder keeps added code in a frame (GoDaddy, Q3) and "Have Admiry's team do it" only
    when Admiry offers it (ruling R3-f); the code is never claimed to be on the site, since the check says so; and no
    password is asked for (W12). first-plan.md's page step gives the same calls in the walk. The rules every prompt
    carries end before the page section, so the prompts do not change."""
    files = assistant_skill.files()
    whole = " ".join(files["SKILL.md"].split())
    page = whole[whole.index("## The page") : whole.index("## The workflows")]
    at = [page.index(f'"{way["label"]}"') for way in site_install.WAYS]
    assert at == sorted(at)  # Q6's order
    for way in site_install.WAYS:
        assert f"`{way['tool']}`" in page, way["id"]
        for name, value in way["args"].items():
            assert f'`{name}: "{value}"`' in page, way["id"]
    assert envelope.MAX_ACTIONS == 7
    assert "four ways as `next` actions, before its other actions (up to seven actions in all)" in page
    assert '"Use an Admiry page instead" comes first where the site\'s builder keeps added code in a frame' in page
    assert '"Have Admiry\'s team do it" is there only when Admiry offers it' in page
    assert 'Never say the code is on the site: `get_landing_page` with `view: "check"` says whether it is' in page
    assert "Never ask the owner for a password" in page
    assert "Admiry's code" not in assistant_skill.rules()
    lines = files["references/first-plan.md"].splitlines()
    six = next(i for i, line in enumerate(lines) if line.startswith("6. "))
    seven = next(i for i, line in enumerate(lines) if line.startswith("7. "))
    step = " ".join(" ".join(lines[six:seven]).split())
    for call in (
        '`view: "kit"`',
        '`action: "send_install_steps"`',
        '`action: "request_install_help"`',
        '`action: "write"`',
        '`view: "check"`',
        "`candidate_url`",
        '`action: "use_url"`',
    ):
        assert call in step, call
    assert "Never say the code is on the site yourself" in step


def test_the_skill_tells_how_the_website_admiry_builds_goes_and_draws_the_line_on_photos():
    """A3p-6 batch 2 Task 6 (plan Q16, spec T14, T26): for a home-services business or a dental practice SKILL.md's page
    section says in one paragraph how Admiry's own website goes -- the looks shown first (`get_site_templates`), then one
    question a call (the look, answered with `style`; then a fact Admiry lacks, with `facts`), then the site with its
    checklist and the upload link, `get_landing_page` with `view: "site"` for the same any time --, that photos reach
    Admiry only through that link and that an upload ends the earlier preview (publish with the token the view shows
    now), the line T14 draws between what may be generated and what must be real, what publishing waits for and that the
    two-call yes is the same, and where a page Admiry already wrote stands. first-plan.md's page step adds the site and
    when-something-goes-wrong.md says what the action does when a call is sent again. The action is named as the others
    are -- `update_landing_page` with `action: "build_site"`, never a bare `build_site` in backticks -- and the rules
    every prompt carries end before the page section, so the prompts do not change."""
    files = assistant_skill.files()
    whole = " ".join(files["SKILL.md"].split())
    page = whole[whole.index("## The page") : whole.index("## The workflows")]
    site = next(
        " ".join(paragraph.split())
        for paragraph in files["SKILL.md"].split("\n\n")
        if paragraph.startswith("For a home-services business")
    )
    assert site in page
    for call in (
        '`update_landing_page` with `action: "write"` (or `"build_site"`)',
        "`get_site_templates`",
        "`facts`",
        "`style`",
        "`accent`",
        '`get_landing_page` with `view: "site"`',
        "`upload_url`",
        "`wordmark: true`",
    ):
        assert call in site, call
    # the order a call answers in (the coordinator's ruling, the spec's §2 flow): the look, then what Admiry lacks one at a
    # time, then the site
    assert site.index("The look comes first (send its id as `style`)") < site.index("then the facts Admiry still needs")
    assert site.index("then the facts Admiry still needs, one at a time") < site.index(
        "Then the site: a private preview"
    )
    assert (
        'Where they are not on, `write` writes the one page as before and `update_landing_page` with `action: "build_site"` '
        "says so in a sentence."
    ) in site
    # fix round 1: a page Admiry already wrote is rewritten by `write`, and the action says a site in its place comes later
    assert (
        "Where Admiry already wrote the business's page, `write` rewrites that page (a brief and a template apply) and "
        '`action: "build_site"` answers that a site in its place comes with the next release.'
    ) in site
    assert "Photos reach Admiry only through the upload link: give it to the owner" in site
    # fix round 1: an upload makes a new preview and ends the one the build gave: publish with the token the view shows
    uploads = (
        "The owner's uploads rebuild the preview and end the earlier link: when they say the photos are in, read "
        '`get_landing_page` with `view: "site"` and publish with the `token` it shows now.'
    )
    assert uploads in site and site.index("Photos reach Admiry only") < site.index(uploads)
    assert site.index(uploads) < site.index("Publishing waits for the logo")
    # what publishing waits for, then T14's line as the spec words it (the key photo is named before the line says it)
    assert (
        "Publishing waits for the logo, or the owner accepting their business name as the logo (`wordmark: true`)"
        in site
    )
    assert "and the key photo (one photo of their own work, their team or their truck; for a dental practice" in site
    assert "in two calls, as for any page" in site
    line = (
        "a logo and scene images may be generated; the key photo and every photo of people must be real photos of the "
        "business, which you may crop, light or retouch but never invent"
    )
    assert line in site and site.index("Publishing waits for the logo") < site.index(line)
    # Task 8: what to do when the gate answers that the photos came in after the last build and its rebuild failed
    # (`build.REBUILD_FIRST`): build once more with no argument -- the words are kept, no model call -- and publish with
    # the token the view shows now; the sentence quotes the gate's own words, so they cannot drift apart
    rebuild = (
        f'When a publish answers "{build.REBUILD_FIRST}", send `update_landing_page` with `action: "build_site"` and no '
        "arguments, then publish with the `token` the site view shows."
    )
    assert rebuild in site and site.index("in two calls, as for any page") < site.index(rebuild) < site.index(line)
    assert (
        "`revise` and `edit` do not apply to this site yet: change its facts, its look and its colour (`accent`) by "
        'calling `update_landing_page` with `action: "build_site"` again.'
    ) in site
    for tool in ("get_landing_page", "get_site_templates", "update_landing_page"):
        assert tool in {m.name for m in registry.SURFACE}
    assert "build_site" not in assistant_skill.rules() and "get_site_templates" not in assistant_skill.rules()
    for path, text in files.items():  # the action is named with its tool, as every other action is
        assert "`build_site`" not in text, path
    lines = files["references/first-plan.md"].splitlines()
    six = next(i for i, line in enumerate(lines) if line.startswith("6. "))
    seven = next(i for i, line in enumerate(lines) if line.startswith("7. "))
    step = " ".join(" ".join(lines[six:seven]).split())
    bullet = step[step.index("**The page.**") :]
    assert (
        "For a home-services business or a dental practice, where Admiry's own sites are on, `write` starts a whole"
        in bullet
    )
    assert (
        'the look first and then the facts it lacks (answer with `action: "build_site"`, `style` and `facts`)' in bullet
    )
    assert "one real photo of the business are in. " + uploads in bullet
    wrong = " ".join(files["references/when-something-goes-wrong.md"].split())
    assert (
        '`update_landing_page` with `action: "build_site"` or `"fix_headline"` makes a new preview on every call that builds, '
        "and none on "
        "one that only asks a question; it spends one of the day's model calls only when the facts its words come from "
        'changed. `get_landing_page` with `view: "site"` shows the preview already made.'
    ) in wrong


def test_the_skill_names_the_knowledge_and_where_a_plan_cites_it():
    skill = assistant_skill.files()["SKILL.md"]
    assert "`admiry://knowledge`" in skill and "`admiry://knowledge/<vertical>/<topic>`" in skill
    assert "`cards.rationale.knowledge_ids`" in skill and "never a promise for this business" in skill


def _step(text: str, number: int) -> str:
    """first-plan.md's step `number`, its bullets with it, whitespace collapsed."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{number}. "))
    end = next(i for i, line in enumerate(lines) if line.startswith(f"{number + 1}. "))
    return " ".join(" ".join(lines[start:end]).split())


def test_the_skill_puts_the_page_first_then_the_way_then_the_payment():
    """The founder's rulings of 2026-10-13 (brief 2, from a real Muse walk: the connect-and-prepay link came before the
    page): the page comes first, by one of three ways the owner chooses -- Admiry builds it, the assistant designs it
    and Admiry hosts it (preview_html or upload_site, then publish with the owner's yes), or the website the owner
    already has --; then how the ads run; then, for a prepaid balance, the owner pays on the link Admiry gives,
    ticking the agreement themselves. SKILL.md says the order in the rules every prompt carries and the three ways in
    its page section; first-plan.md's step 6 takes them in that order; the checklist's page line names the ways and
    never "free" (hosting is free only while the ads spend at least $200 a month)."""
    files = assistant_skill.files()
    rules = " ".join(assistant_skill.rules().split())
    assert (
        "10. Before the ads run, in this order, each the owner's choice: the page the ads send people to (Admiry builds "
        "it, you design it and Admiry hosts it, or the website they already have); then how the ads run (Admiry running "
        "them from a prepaid balance, or their own Google Ads account); then, for a prepaid balance, the payment on the "
        "link Admiry gives, where the owner ticks the agreement themselves. Ask, with the ways side by side, and never "
        "choose for them."
    ) in rules
    skill = " ".join(files["SKILL.md"].split())
    page = skill[skill.index("## The page") : skill.index("## The workflows")]
    assert (
        "The ads need a page to send people to, and it comes first: before how the ads run and before any payment. "
        "There are three ways, and the owner chooses: Admiry builds it (`update_landing_page` with `action: "
        '"write"`); you design it and Admiry hosts it (`action: "preview_html"` for one page or `action: '
        '"upload_site"` for a whole site, then `action: "publish"` with the owner\'s yes); or the website they '
        'already have (`action: "use_url"`).'
    ) in page
    assert "Admiry writes it unless the owner has their own" not in skill
    step = _step(files["references/first-plan.md"], 6)
    assert step.startswith(
        "6. Before it can run, in this order: the sign-in, the page, how the ads run and, for ads Admiry runs from a "
        "prepaid balance, the payment."
    )
    at = [step.index(f"**{name}.**") for name in ("Sign-in", "The page", "Where the ads run", "The payment")]
    assert at == sorted(at)
    bullet = step[step.index("**The page.**") : step.index("**Where the ads run.**")]
    assert (
        "**The page.** It comes first: before how the ads run and before any payment. Put the three ways to the owner "
        "side by side and let them choose (`next` offers them; never choose for them): Admiry builds it, you design it "
        "and Admiry hosts it, or the website they already have. Admiry builds it: `update_landing_page` with `action: "
        '"write"` has Admiry write it'
    ) in bullet
    assert (
        'You design it and Admiry hosts it: `action: "preview_html"` for one page you write, or `action: '
        '"upload_site"` for a whole site you build (read the kit\'s `bundle` section first), then `publish` with the '
        "owner's yes, in the same two calls."
    ) in bullet
    assert 'The website they already have: `action: "use_url"` with its address.' in bullet
    assert bullet.index("You design it and Admiry hosts it") < bullet.index("The website they already have")
    # final review, item 2: the first payment's link is connect_account's for Admiry's way; get_billing's is for later
    assert (
        "**The payment.** For ads Admiry runs from a prepaid balance, the owner pays on the link Admiry gives: the "
        "first payment's link is `connect_account`'s for that way (`mode: \"managed\"`), and it opens the plan's own "
        "page, with its summary, the agreement and the prepayment terms, which the owner ticks themselves, and one "
        'button to pay on Stripe; `get_billing` with `view: "topup_link"` gives the link for adding funds later. Give '
        "the owner the link; never tick or pay for them."
    ) in step
    wrong = " ".join(files["references/when-something-goes-wrong.md"].split())
    assert (
        '**The page.** Once the page is changed, `get_landing_page` with `view: "check"` runs the check again; a page '
        "not there yet is the owner's choice between the ways `fix.choices` offers (Admiry builds it, you design it and "
        "Admiry hosts it, or the website they already have): ask `fix.ask_owner` and send the one they pick."
    ) in wrong
    for path, text in files.items():
        assert "free page" not in text and "Admiry's free" not in text, path


def test_the_skill_says_the_script_tag_every_hosted_page_carries_and_where_admiry_hosts_it():
    """Brief 2's addendum (founder 2026-10-13): for the page the assistant designs and Admiry hosts, the guide says what
    the code holds it to -- every page carries Admiry's script tag exactly as the kit gives it (`tracking.snippet`,
    what counts the calls, forms and bookings the ads bring): an upload with a page without it is refused with that
    fix (bundles.check's "tracking" rule), and a one-page preview without it gets it added when it is published
    (hosted.inject) -- and what the assistant tells the owner before building: Admiry hosts it, on an address made
    from the business's name under business.admiry.ai (hosted.hostname_for), with the hosting rule in the page card's
    own words (hosting_minimum.MINIMUM). No domain of the owner's own is promised: it is not offered yet."""
    files = assistant_skill.files()
    skill = " ".join(files["SKILL.md"].split())
    page = skill[skill.index("## The page") : skill.index("## The workflows")]
    bullet = _step(files["references/first-plan.md"], 6)
    bullet = bullet[bullet.index("**The page.**") : bullet.index("**Where the ads run.**")]
    tag = (
        'every page carries Admiry\'s script tag exactly as `get_landing_page` with `view: "kit"` gives it '
        "(`tracking.snippet`): it measures the calls, forms and bookings the ads bring. An upload with a page that "
        "lacks it is refused with that fix; a `preview_html` page without it gets it added when it is published."
    )
    assert f"When you design the page and Admiry hosts it, {tag}" in page and f"Every {tag[6:]}" in bullet
    # what the code holds the page to: the kit's tag, refused on an upload's page without it, added on publish
    snippet = landing.snippet("pg_x", "https://agent.example", None)
    assert snippet.startswith("<script") and "/t/admiry.js" in snippet and 'data-page="pg_x"' in snippet
    assert snippet in hosted.inject("<html><body><p>x</p></body></html>", snippet=snippet, noindex=False)
    words = "<p>" + "Bright Smile Dental fits clear aligners in Palo Alto. " * 10 + "</p>"
    for carried, refused in (("", True), (snippet, False)):
        judged = bundles.Judged(files={"index.html": f"<html><body>{words}{carried}</body></html>".encode()})
        bundles.check(judged, page_id="pg_x", own_host=None, agent_base="https://agent.example", snippet=snippet)
        assert any(p["rule"] == "tracking" and snippet in (p["fix"] or "") for p in judged.problems) is refused
    address = f"{hosted.slugify('Bright Smile Dental')}.business.admiry.ai"
    assert address == "bright-smile-dental.business.admiry.ai"
    hosting = (
        "tell the owner in a sentence that Admiry hosts it on an address it gives the site, made from the business's "
        f'name (like {address}), and say Admiry\'s own words on hosting: "{hosting_minimum.MINIMUM}"'
    )
    assert f"Before you build a page Admiry will host, {hosting}" in page
    assert f"Before you build, {hosting}" in bullet
    assert hosting_minimum.MINIMUM == (
        "Hosting is free while your ads with Admiry spend at least $200 a month. Without that, the site comes down 30 "
        "days later. We remind you first."
    )
    assert 'If the owner asks for a domain of their own, say "A domain of your own is not offered yet."' in page
    for path, text in files.items():
        flat = " ".join(text.split()).lower()
        assert "custom domain" not in flat and "connect a domain" not in flat and "buy a domain" not in flat, path


def test_each_consultation_answer_is_the_owners_to_give():
    """Addendum 4 (from a real Muse walk: five answers 10-20 seconds apart, one of them guessed): each consultation
    question is the owner's to answer -- put to them with its choices, exactly their answer sent, never one picked or
    inferred by the assistant; what the owner already said in their own words is confirmed with them in one line.
    SKILL.md says it where it tells how to represent Admiry (the rules every prompt carries), and first-plan.md where
    the questions are answered."""
    files = assistant_skill.files()
    rules = " ".join(assistant_skill.rules().split())
    said = (
        "Each question is the owner's to answer: put it to them as written, with its choices, and send exactly their "
        "answer; never pick or infer one yourself, even when you think you know it. If the owner already said it in "
        "their own words earlier, confirm it with them in one line before you send it."
    )
    assert said in rules and "do not answer one for them" not in rules
    step = _step(files["references/first-plan.md"], 3)
    assert (
        "For each question: it is the owner's to answer. Show `say`, put the question to them with its options, and "
        "send exactly their answer with `update_marketing_plan`"
    ) in step
    assert (
        "Never pick or infer an answer yourself; if the owner already said it in their own words earlier, confirm it "
        "with them in one line before you send it."
    ) in step


def test_the_guide_is_served_over_http_where_its_relative_paths_lead(client):
    """An assistant that reads /connect/skill.md and follows `references/first-plan.md` asks for
    /connect/references/first-plan.md. The bytes are the resource's own, so the manifest's digests hold for both."""
    for entry in assistant_skill.manifest():
        path = entry["uri"].removeprefix("skill://admiry-marketing/")
        served = client.get("/connect/skill.md" if path == "SKILL.md" else f"/connect/{path}")
        assert served.status_code == 200 and served.headers["content-type"].startswith("text/markdown"), path
        raw = served.text.encode()
        assert entry["size"] == len(raw) and entry["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest(), path
    assert client.get("/connect/references/nope.md").status_code == 404
    assert client.get("/connect/references/SKILL.md").status_code == 404


def test_the_skill_relays_the_plans_picture_line_and_never_promises_newsbreak_without_one(client):
    """M1's rule (M1 Task 7 fix round 1, M5), carried from skill.md and read where an assistant finds it: the picture's
    line is relayed whether or not a picture is there (it may say the plan's picture is gone), and NewsBreak is never
    promised to a trade Admiry has no picture for."""
    text = " ".join(client.get("/connect/references/first-plan.md").text.split())
    assert "carries an `image_note`, relay it, with or without an `image`" in text
    assert "Never promise NewsBreak without a picture" in text and "once Admiry has a picture for your trade" in text


def test_the_page_guide_is_generated_from_the_recipes_and_names_public_tools():
    guide = assistant_skill.page_guide()
    assert guide.startswith("# Building a page for the ads")
    for heading in ("## Rules for the destination", "## Dentists", "## Home Services", "## Attorneys", "## Fallback"):
        assert heading in guide
    assert "`update_landing_page`" in guide and "preview_page" not in guide and "get_page_kit" not in guide
    assert "the footer also carries a Terms link" in guide
    assert assistant_skill.page_guide() == guide  # the same bytes every time: a digest can stand on it


def test_the_four_prompts_are_listed_and_carry_the_rules(client, conn):
    key = business_with_key(conn)
    listed = [p["name"] for p in rpc(client, "prompts/list", key=key)["result"]["prompts"]]
    assert (
        sorted(listed)
        == list(told.PROMPTS)
        == [
            "admiry-get-more-customers",
            "admiry-how-are-my-ads-doing",
            "admiry-promote-a-service",
            "admiry-record-lead-outcomes",
        ]
    )
    got = rpc(
        client,
        "prompts/get",
        {"name": "admiry-get-more-customers", "arguments": {"what_you_want": "more Invisalign patients"}},
        key=key,
    )["result"]
    text = " ".join(m["content"]["text"] for m in got["messages"])
    assert "more Invisalign patients" in text and "Never invent a number" in text
    assert "## The workflows" not in text  # the rules, not the whole guide
    assert set(TOOLISH.findall(text)) <= _public() and "`create_marketing_plan`" in text
    assert "If nothing is begun" in text and "otherwise follow its `next`" in text  # never starts an owner over
    for name, arguments, says in (
        ("admiry-how-are-my-ads-doing", {}, "`get_campaign_performance` with period 30d"),
        (
            "admiry-promote-a-service",
            {"service": "teeth whitening"},
            "with a consultation in progress, finish it first",  # a new plan is begun only when nothing is
        ),
        ("admiry-record-lead-outcomes", {}, "`get_leads` with awaiting_outcome true"),
    ):
        got = rpc(client, "prompts/get", {"name": name, "arguments": arguments}, key=key)["result"]
        text = " ".join(m["content"]["text"] for m in got["messages"])
        assert set(TOOLISH.findall(text)) <= _public(), name
        assert says in text, name


def test_every_prompt_argument_is_described_as_text(client, conn):
    """Final review C, minor 6: postponed annotations, and a `str | None` too, made FastMCP describe an argument as
    "Provide as a JSON string matching the following schema: {"type":"string"}". A prompt's arguments are text. The
    re-review's M-J: each carries its own words, where a host that shows a prompt as a form reads them."""
    key = business_with_key(conn)
    listed = rpc(client, "prompts/list", key=key)["result"]["prompts"]
    arguments = [(p["name"], a) for p in listed for a in p.get("arguments") or []]
    assert len(arguments) == 5  # what_you_want and website; period; service and offer
    assert not [(name, a["name"]) for name, a in arguments if "JSON string" in (a.get("description") or "")]
    required = {(name, a["name"]) for name, a in arguments if a.get("required")}
    assert required == {("admiry-get-more-customers", "what_you_want"), ("admiry-promote-a-service", "service")}
    assert {(name, a["name"]): a.get("description") for name, a in arguments} == {
        ("admiry-get-more-customers", "what_you_want"): "the owner's words",
        ("admiry-get-more-customers", "website"): "their site, if any",
        ("admiry-how-are-my-ads-doing", "period"): "7d, 30d, month or last_month",
        ("admiry-promote-a-service", "service"): "what to promote, in the owner's words",
        ("admiry-promote-a-service", "offer"): "an offer to lead with, if any",
    }
    # and the words reach the prompt as typed: an argument annotated other than `str` is read as JSON first, which
    # takes the quotes off words that happen to be a JSON string
    typed = {"what_you_want": '"more Invisalign patients"'}
    got = rpc(client, "prompts/get", {"name": "admiry-get-more-customers", "arguments": typed}, key=key)["result"]
    assert 'The owner says: "more Invisalign patients"' in got["messages"][0]["content"]["text"]


def test_the_resources_are_the_state_the_docs_and_the_skill(client, conn, walk_fake):
    walk = planned(client, conn)
    listed = {r["uri"] for r in rpc(client, "resources/list", key=walk["key"])["result"]["resources"]}
    assert listed == SKILL_URIS | {
        "admiry://marketing/state",
        "admiry://business/profile",
        "admiry://marketing/plan",
        "admiry://docs",
        "admiry://marketing/insights",  # capability layer K2: the Analyst's latest look
        "admiry://marketing/research",  # capability layer K4: the Researcher's latest look
        "admiry://knowledge",  # capability layer K3: the curated knowledge's index
        "ui://admiry/plan",
        "ui://admiry/channels",
    }
    assert set(told.STATE_RESOURCES) <= listed
    templates = rpc(client, "resources/templates/list", key=walk["key"])["result"]["resourceTemplates"]
    assert sorted(t["uriTemplate"] for t in templates) == [
        "admiry://knowledge/{vertical}/{topic}",  # capability layer K3: one reviewed file
        "admiry://marketing/performance/{period}",
    ]

    def read(uri: str) -> str:
        return rpc(client, "resources/read", {"uri": uri}, key=walk["key"])["result"]["contents"][0]["text"]

    conn.execute("DELETE FROM agent_action")
    state = json.loads(read("admiry://marketing/state"))
    assert state["surface_version"] == gate.SURFACE_VERSION and state["plan"]["id"] == walk["plan_id"]
    assert json.loads(read("admiry://business/profile"))["business"]["name"] == "Bright Smile Dental"
    assert json.loads(read("admiry://marketing/plan"))["plan_id"] == walk["plan_id"]
    assert json.loads(read("admiry://marketing/performance/7d"))["period"] == "7d"
    found = conn.execute(
        "SELECT tool, surface_tool, surface_call_hash, idempotency_key FROM agent_action ORDER BY id"
    ).fetchall()
    # a fixed resource is named by its address; the templated one by its template (final review A m1)
    assert [(r["tool"], r["surface_tool"]) for r in found] == [
        ("marketing_state", "resource:admiry://marketing/state"),
        ("business_profile", "resource:admiry://business/profile"),
        ("get_plan", "resource:admiry://marketing/plan"),
        ("performance", "resource:admiry://marketing/performance/{period}"),
    ]
    # a resource read stores no call identity and no key (Task 18): no later call is ever answered from one
    assert [(r["surface_call_hash"], r["idempotency_key"]) for r in found] == [(None, None)] * 4
    assert read("skill://admiry-marketing/SKILL.md") == assistant_skill.files()["SKILL.md"]
    assert "MCP: http://testserver/mcp" in read("admiry://docs")


def test_no_data_resource_runs_its_command_on_the_event_loop(client, conn, walk_fake, monkeypatch):
    """A command waits on the database (psycopg blocks), and the event loop serves the homepage, the API and /mcp: a
    data resource's command runs in a worker thread. FastMCP sends a fixed resource's sync function to one itself, but
    calls a template's function on the loop."""
    walk = planned(client, conn)
    real, calls = told._data, []

    def recording(command: str, **args) -> str:
        try:
            asyncio.get_running_loop()
            calls.append((command, "on the loop"))
        except RuntimeError:  # no loop runs in this thread: a worker thread
            calls.append((command, "in a thread"))
        return real(command, **args)

    monkeypatch.setattr(told, "_data", recording)
    for uri in (*told.STATE_RESOURCES, "admiry://marketing/performance/7d"):
        assert "result" in rpc(client, "resources/read", {"uri": uri}, key=walk["key"]), uri
    assert calls == [
        ("marketing_state", "in a thread"),
        ("business_profile", "in a thread"),
        ("get_plan", "in a thread"),
        ("performance", "in a thread"),
    ]


def test_a_resource_refuses_in_the_commands_own_words(client, conn, walk_fake):
    walk = planned(client, conn)
    body = rpc(client, "resources/read", {"uri": "admiry://marketing/performance/year"}, key=walk["key"])
    assert "result" not in body and "period must be one of 7d, 30d, month, last_month" in body["error"]["message"]


def test_a_resource_never_carries_a_lead(client, conn, walk_fake):
    walk = planned(client, conn)
    listed = {r["uri"] for r in rpc(client, "resources/list", key=walk["key"])["result"]["resources"]}
    templates = rpc(client, "resources/templates/list", key=walk["key"])["result"]["resourceTemplates"]
    listed |= {t["uriTemplate"] for t in templates}
    # the skill's files are its words, pinned above (one says how to record outcomes); every other resource, templated
    # or not, is data
    assert not any("lead" in uri for uri in listed - SKILL_URIS)


def test_a_resource_read_stores_a_short_name_whatever_address_it_was_sent(client, conn, walk_fake):
    """Final review A m1: the address of a read is the caller's, and was stored whole (200,000 characters wrote a row
    of 200,040). The templated resource is stored as its template, a fixed one as its address, and anything else cut
    to its first 200 characters."""
    walk = planned(client, conn)
    conn.execute("DELETE FROM agent_action")
    body = rpc(client, "resources/read", {"uri": "admiry://marketing/performance/" + "x" * 200_000}, key=walk["key"])
    assert "result" not in body  # the command refuses the period, in its own words
    found = conn.execute("SELECT tool, surface_tool FROM agent_action").fetchall()
    assert [(r["tool"], r["surface_tool"]) for r in found] == [
        ("performance", "resource:admiry://marketing/performance/{period}")
    ]
    assert gate.resource_tool("admiry://marketing/state") == "resource:admiry://marketing/state"
    # a knowledge file's read runs no command and writes no row (test_knowledge_resources): it is named as any address
    assert (
        gate.resource_tool("admiry://knowledge/dentists/compliance")
        == "resource:admiry://knowledge/dentists/compliance"
    )
    unknown = "admiry://elsewhere/" + "y" * 500
    assert gate.resource_tool(unknown) == "resource:" + unknown[:200]


# A lead's own words, each a string no answer has any other reason to hold: its person, what they wrote, the service
# they named, and the owner's note on it
LEAD_WORDS = {
    "name": "Zephyrine Quillfeather",
    "phone": "6505557731",
    "email": "zq.lead@nowhere.example",
    "message": "my molar hums like a xylophone",
    "service": "Glimmerwhite deluxe polish",
    "preferred_time": "Thursdays at dusk",
}
LEAD_NOTE = "called back from the ferry terminal"


def test_a_key_without_the_leads_scope_never_reads_a_lead(client, conn, walk_fake, strict_surface):
    """Final review A m9 (a probe kept): a lead is on file, its owner's note too. A key without the `leads` scope calls
    every public read and reads every state resource: no answer holds a word of the lead, and neither does what `ask`
    hands the model."""
    walk = planned(client, conn)
    page = landing.set_owner_url(conn, "biz_1", "https://brightsmile.example/invisalign")
    lead = leads.record(conn, page, LEAD_WORDS, ("g1", "gclid"))
    noted = {"lead_id": lead["id"], "status": "contacted", "note": LEAD_NOTE, "idempotency_key": "n1"}
    assert not (res := call_tool(client, "update_lead", noted, key=walk["key"]))["isError"], tool_text(res)
    walk_fake.answers_to["who wrote to us this week?"] = models.MailAnswer(text="I cannot say who.", grounded=True)
    reader, _ = keys.mint(conn, business_id="biz_1", cls="business", scopes=("read", "manage"), label="claude")
    periods = ("7d", "30d", "month", "last_month")
    reads = [
        ("get_marketing_state", {}),
        ("get_business_profile", {}),
        ("get_connected_accounts", {}),
        ("get_marketing_plan", {}),
        ("get_campaigns", {}),
        *[("get_campaign_performance", {"period": period}) for period in periods],
        ("get_landing_page", {}),
        ("get_billing", {}),
        ("get_activity_log", {}),
        ("ask_admiry", {"question": "who wrote to us this week?"}),
    ]
    answers, refused = [], []
    for tool, args in reads:
        res = call_tool(client, tool, args, key=reader)
        answers.append((tool, json.dumps(res)))
        if res["isError"]:
            refused.append((tool, tool_text(res)))
    # every read answered -- the balance too: no way is chosen yet, so it asks how the ads run (final review, item 2)
    assert refused == [], refused
    for uri in (*told.STATE_RESOURCES, *[f"admiry://marketing/performance/{period}" for period in periods]):
        answers.append((uri, json.dumps(rpc(client, "resources/read", {"uri": uri}, key=reader)["result"])))
    assert walk_fake.answer_calls, "ask handed nothing to the model"
    answers.append(("ask's material", json.dumps(walk_fake.answer_calls, default=str)))
    for written in (*LEAD_WORDS.values(), LEAD_NOTE):
        assert not [where for where, said in answers if written in said], written
