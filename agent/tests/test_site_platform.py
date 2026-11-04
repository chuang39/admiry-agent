"""A3p-5 batch 2 Task 1: the platform an owner's site is built on, read from the HTML Admiry already fetched
(site_platform, plan Q1), each platform's steps for Admiry's code (site_install.STEPS, Q2 and Q3), the platform on the
profile (the crawl, the reading, profile.carry), the kit's `install` part and `<head>` in the kit and the check (Q4),
and the readers of the owner's own site, read or not (Q13)."""

from html import escape
from pathlib import Path

import httpx
import pytest

from agent import (
    booking_hosts,
    business,
    config,
    crawl,
    landing,
    listings,
    mail,
    page_builder,
    places,
    profile,
    site_install,
    site_platform,
    tools_business,
)
from agent.tests.conftest import JARGON, call_tool, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture
from agent.tests.listings_fakes import found
from agent.tests.test_landing import GOOD, _plan, _serve, fake  # noqa: F401 - fake is a pytest fixture used by name

SITES = Path(__file__).parent / "fixtures" / "sites"
PUBLIC = ["93.184.216.34"]
BUILT = [p for p in site_platform.PLATFORMS if p != "other"]
WHERE = "Put this inside <head> on every page (or before </body> where your site allows only that), exactly as given."
SQUARE_WIDGET = "https://square.site/appointments/buyer/widget/3b296b3a-d8b8-4b40-b939-7ab25a7ff85e/LAY8WSMJNDVDJ.js"
IN_A_FRAME = (
    "Your site's builder keeps added code in a frame, where it can't count calls and forms. Use an Admiry page for "
    "your ads instead."
)


def _site(name: str) -> str:
    return (SITES / f"{name}.html").read_text(encoding="utf-8")


def _read_as(platform: str):
    """A crawl stand-in whose first page tells `platform`, as crawl.fetch_site sets it (Q1)."""
    return lambda url: [
        crawl.Page(url=url, title="Site", text="We do Invisalign", platform=platform),
        crawl.Page(url=url.rstrip("/") + "/about", title="About", text="Our team"),
    ]


# ---------------------------------------------------------------- the platform (Q1)


def test_the_platforms_are_the_specs_and_other():
    assert site_platform.PLATFORMS == (
        "wix",
        "squarespace",
        "wordpress_com",
        "wordpress",
        "godaddy",
        "shopify",
        "webflow",
        "square_online",
        "other",
    )


@pytest.mark.parametrize("platform", BUILT)
def test_each_platforms_page_reads_as_that_platform(platform):
    assert site_platform.detect(_site(platform), {}) == platform


@pytest.mark.parametrize(
    ("platform", "html", "headers"),
    [
        ("wix", '<img src="https://static.wixstatic.com/media/logo.png/v1/fill/w_120,h_60/logo.png">', {}),
        ("wix", '<script src="https://static.parastorage.com/services/main.js"></script>', {}),
        ("wix", "<h1>Welcome</h1>", {"X-Wix-Request-Id": "1791245253.48392361521458"}),
        ("wix", '<meta name="generator" content="Wix.com Website Builder"/>', {}),
        ("squarespace", '<link rel="stylesheet" href="https://static1.squarespace.com/static/site.css">', {}),
        ("squarespace", '<meta name="generator" content="Squarespace">', {}),
        (
            "wordpress_com",
            '<meta name="generator" content="WordPress.com" /><link rel="stylesheet" href="https://s0.wp.com/a.css">',
            {},
        ),
        (
            "wordpress_com",
            '<link rel="stylesheet" href="https://s1.wp.com/wp-content/mu-plugins/jetpack-mu-wpcom-plugin/moon/a.css">',
            {},
        ),
        (
            "wordpress_com",
            (
                '<img src="https://brightsmile.files.wordpress.com/2026/10/logo.png">'
                '<script id="wpcom-actionbar-placeholder-js-extra">var actionbardata = {};</script>'
            ),
            {},
        ),
        ("wordpress", '<link rel="stylesheet" href="/wp-content/themes/astra/style.css">', {}),
        ("wordpress", '<script src="https://brightsmile.example/wp-includes/js/jquery/jquery.min.js"></script>', {}),
        ("wordpress", '<meta name="generator" content="WordPress 6.8.2" />', {}),
        ("godaddy", '<img src="//img1.wsimg.com/isteam/ip/4b5c6d7e/logo.png/:/rs=h:120">', {}),
        ("godaddy", '<script src="//img1.wsimg.com/blobby/go/gpub/4b5c6d7e/script.js"></script>', {}),
        ("godaddy", '<meta name="generator" content="Go Daddy Website Builder 7.0.5350">', {}),
        ("godaddy", '<meta name="generator" content="Starfield Technologies; Go Daddy Website Builder 8.0.0000"/>', {}),
        ("shopify", '<script src="https://cdn.shopify.com/shopifycloud/shop-js/client.js"></script>', {}),
        ("shopify", "<h1>Welcome</h1>", {"X-ShopId": "48734371"}),
        ("shopify", "<h1>Welcome</h1>", {"powered-by": "Shopify"}),
        ("webflow", '<html data-wf-page="64f1a2b3c4d5e6f7a8b9c0d1"><body><h1>Welcome</h1></body></html>', {}),
        ("webflow", '<link href="https://cdn.prod.website-files.com/64f1/css/site.css" rel="stylesheet">', {}),
        ("webflow", '<meta content="Webflow" name="generator"/>', {}),
        ("square_online", '<script src="https://cdn3.editmysite.com/app/website/js/runtime.js"></script>', {}),
        ("square_online", '<meta name="generator" content="Square Online">', {}),
    ],
)
def test_each_signature_alone_tells_its_platform(platform, html, headers):
    """Every signature plan Q1 names, alone in the markup or the headers (Shopify's `powered-by` and Square Online's
    generator are what those platforms send today: the Task 1 check of real sites) -- never a square.site asset:
    Square's booking widget loads from there on any site that embeds it (fix round 1, ruling R1-a)."""
    assert site_platform.detect(html, headers) == platform


@pytest.mark.parametrize(
    ("expected", "html"),
    [
        # GoDaddy's hosting puts its monitoring script from img1.wsimg.com on every site it hosts, WordPress or not
        (
            "wordpress",
            (
                '<link rel="stylesheet" href="/wp-content/themes/astra/style.css">'
                '<script src="https://img1.wsimg.com/traffic-assets/js/tccl.min.js"></script>'
            ),
        ),
        (
            "other",
            (
                '<script src="https://img1.wsimg.com/traffic-assets/js/tccl.min.js"></script>'
                '<script src="https://img1.wsimg.com/signals/js/clients/scc-c2/scc-c2.min.js"></script>'
                '<link rel="preconnect" href="https://img1.wsimg.com">'
            ),
        ),
        # Weebly, Square Online's older sibling, loads from the same CDN, never Square Online's app
        (
            "other",
            (
                '<script src="//cdn2.editmysite.com/js/site/main.js"></script>'
                '<link rel="stylesheet" href="//cdn2.editmysite.com/css/sites.css">'
            ),
        ),
        # Jetpack on a site of its own loads from WordPress.com's static host and says it is not WordPress.com
        (
            "wordpress",
            (
                '<meta name="generator" content="WordPress 6.8.2" />'
                '<script src="https://s0.wp.com/wp-content/js/devicepx-jetpack.js"></script>'
                '<script>var JetpackInstantSearchOptions = {"isWpcom":false};</script>'
            ),
        ),
        # links and frames are other sites' pages, and words are words: none is what the site is built on
        (
            "other",
            (
                '<a href="https://square.site/book/15XJ9KMDVV4T0/bright-smile">Book</a>'
                '<iframe src="https://square.site/book/15XJ9KMDVV4T0/embed"></iframe>'
                '<a href="https://www.wix.com/">Made with Wix</a><form action="https://cdn.shopify.com/x"></form>'
            ),
        ),
        # Square's booking widget is a script from square.site on whatever site embeds it (fix round 1, ruling R1-a)
        ("other", f'<script src="{SQUARE_WIDGET}"></script>'),
        (
            "wordpress",
            f'<link rel="stylesheet" href="/wp-content/themes/astra/style.css"><script src="{SQUARE_WIDGET}"></script>',
        ),
        ("other", "<p>Our old site was on static.wixstatic.com, then cdn.shopify.com and WordPress.com.</p>"),
        ("other", '<script>var asset = "https://static.parastorage.com/services/main.js";</script>'),
        ("other", ""),
    ],
)
def test_lookalikes_are_no_signatures(expected, html):
    assert site_platform.detect(html, {}) == expected


def test_a_site_whose_platform_cannot_be_told_gets_the_generic_steps_never_a_wrong_platforms():
    """The plan's Review Focus: no signature, or two platforms' (the markup's and a header's included), is `other`,
    and `other`'s steps -- never one platform's steps for another's site."""
    generic = site_install.steps_for("other")
    cases = [
        (_site("no_signature"), {}),
        (_site("two_platforms"), {}),
        (_site("squarespace"), {"x-wix-request-id": "1"}),
    ]
    for html, headers in cases:
        platform = site_platform.detect(html, headers)
        assert platform == "other", headers
        steps = site_install.steps_for(platform)
        assert steps == generic and steps["placement"] == "head"
        said = " ".join([*steps["steps"], steps["name"]])
        assert not any(site_install.STEPS[p]["name"] in said for p in BUILT)
    assert generic["steps"] == [
        "Paste Admiry's code inside <head> on every page, or before </body> where your site allows only that."
    ]


def test_the_crawl_reads_the_platform_from_the_first_page_it_reads():
    """Q1: crawl.fetch_site sets Page.platform on the first page it reads, from that page's HTML and headers; the
    other pages carry none, and the address is never a signature."""

    def serve(pages: dict[str, tuple[dict, str]]) -> httpx.MockTransport:
        def handler(request):
            if request.url.path not in pages:
                return httpx.Response(404)
            headers, body = pages[request.url.path]
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **headers}, text=body)

        return httpx.MockTransport(handler)

    home = _site("wix").replace("</body>", "<a href='/about'>About us</a></body>")
    t = serve({"/": ({}, home), "/about": ({}, _site("squarespace"))})
    pages = crawl.fetch_site("https://brightsmile.example/", transport=t, resolver=lambda h: PUBLIC)
    assert [(p.url, p.platform) for p in pages] == [
        ("https://brightsmile.example/", "wix"),
        ("https://brightsmile.example/about", None),
    ]
    t = serve({"/": ({"x-wix-request-id": "1791245253.48"}, "<html><body><h1>Welcome</h1></body></html>")})
    assert crawl.fetch_site("https://brightsmile.example/", transport=t, resolver=lambda h: PUBLIC)[0].platform == "wix"
    t = serve({"/": ({}, _site("no_signature"))})
    site = crawl.fetch_site("https://brightsmile.wixsite.com/", transport=t, resolver=lambda h: PUBLIC)
    assert site[0].platform == "other"  # a Wix-looking address, no Wix signature: never guessed from the address


# ---------------------------------------------------------------- the platform on the profile


def test_a_reading_stores_the_platform_and_a_later_one_keeps_it(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    monkeypatch.setattr(crawl, "fetch_site", _read_as("squarespace"))
    key = business_with_key(conn)
    args = {"text": "Family dentist in Palo Alto", "website_url": "https://brightsmile.example"}
    first = call_tool(client, "describe_business", {**args, "idempotency_key": "d1"}, key=key)
    assert not first["isError"], tool_text(first)
    assert business.latest_knowledge(conn, "biz_1")[1]["website_platform"] == "squarespace"
    # plain words, the model naming the same site: no page read, and what the site is built on stands
    again = call_tool(client, "describe_business", {"text": "Now with implants", "idempotency_key": "d2"}, key=key)
    assert not again["isError"], tool_text(again)
    version, latest = business.latest_knowledge(conn, "biz_1")
    assert version == 2 and latest["website_platform"] == "squarespace"
    # plain words naming no site at all: the site and its platform both stay (ruling F3's pair)
    fake.knowledge = fake.knowledge.model_copy(update={"website": None})
    third = call_tool(client, "describe_business", {"text": "Open Saturdays too", "idempotency_key": "d3"}, key=key)
    assert not third["isError"], tool_text(third)
    version, latest = business.latest_knowledge(conn, "biz_1")
    assert version == 3 and (latest["website"], latest["website_platform"]) == (
        "https://brightsmile.example",
        "squarespace",
    )


def test_a_reading_records_the_platform_of_the_site_it_read_only():
    """tools_business.extract copies the first page's platform beside the reading's data -- only when the reading's
    website is the site it crawled, and never from a reading that read no page."""
    pages = [{"url": "https://brightsmile.example/", "title": "Site", "text": "We do Invisalign", "platform": "wix"}]
    gen = FakeGenerator(knowledge=knowledge_fixture())
    read = tools_business.extract("dentist", "https://www.brightsmile.example/", "en-US", gen=gen, pages=pages)
    assert read["website_platform"] == "wix"
    elsewhere = FakeGenerator(knowledge=knowledge_fixture().model_copy(update={"website": "https://calendly.com/bsd"}))
    assert "website_platform" not in tools_business.extract(
        "dentist", "https://brightsmile.example", "en-US", gen=elsewhere, pages=pages
    )
    assert "website_platform" not in tools_business.extract("dentist", None, "en-US", gen=gen, pages=[])
    untold = [{**pages[0], "platform": None}]
    assert "website_platform" not in tools_business.extract(
        "dentist", "https://brightsmile.example", "en-US", gen=gen, pages=untold
    )


def test_the_platform_stays_with_the_site_it_was_read_from():
    """profile.carry: a new reading that brings no platform keeps the one on file when it names no site (ruling F3
    keeps the site too) or names that same site; another site, read or not, never inherits it (Q13: a site never read
    has no platform)."""
    wix = {"website": "https://brightsmile.example", "website_platform": "wix"}
    plain = {**knowledge_fixture().model_dump(), "website": None}
    kept = profile.carry(wix, plain)
    assert (kept["website"], kept["website_platform"]) == ("https://brightsmile.example", "wix")
    assert profile.carry(wix, {**plain, "website": "www.brightsmile.example/"})["website_platform"] == "wix"
    newer = {**plain, "website": "https://newsmile.example", "website_platform": "squarespace"}
    assert profile.carry(wix, newer)["website_platform"] == "squarespace"
    assert "website_platform" not in profile.carry(wix, {**plain, "website": "https://newsmile.example"})
    unread = profile.carry(wix, {**plain, "website_unread": "https://newsmile.example"})
    assert unread["website_unread"] == "https://newsmile.example" and "website_platform" not in unread


def test_an_owner_who_names_another_website_drops_the_old_sites_platform(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    monkeypatch.setattr(crawl, "fetch_site", _read_as("wix"))
    key = business_with_key(conn)
    args = {"text": "dentist", "website_url": "https://brightsmile.example", "idempotency_key": "d1"}
    assert not call_tool(client, "describe_business", args, key=key)["isError"]
    same = call_tool(
        client,
        "update_business",
        {"patch": {"website": "https://www.brightsmile.example/"}, "idempotency_key": "u1"},
        key=key,
    )
    assert not same["isError"], tool_text(same)
    assert business.latest_knowledge(conn, "biz_1")[1]["website_platform"] == "wix"  # the same site, `www.` aside
    other = call_tool(
        client, "update_business", {"patch": {"website": "https://newsmile.example"}, "idempotency_key": "u2"}, key=key
    )
    assert not other["isError"], tool_text(other)
    latest = business.latest_knowledge(conn, "biz_1")[1]
    assert latest["website"] == "https://newsmile.example" and "website_platform" not in latest


# ---------------------------------------------------------------- the steps (Q2, Q3)


def test_every_platform_has_its_steps_in_owners_words():
    assert set(site_install.STEPS) == set(site_platform.PLATFORMS)
    for platform in site_platform.PLATFORMS:
        entry = site_install.STEPS[platform]
        assert set(entry) == {"name", "steps", "requirement", "help_url", "placement", "invite", "in_frame"}, platform
        assert entry["steps"] and entry["placement"] in site_install.PLACEMENTS, platform
        said = " ".join([*entry["steps"], entry["requirement"] or "", *entry["invite"]])
        assert not JARGON.search(said) and "password" not in said.lower(), platform  # W12: no password, anywhere
        if entry["requirement"]:  # Q11's requirement line
            assert entry["requirement"].startswith(f"{entry['name']} lets you add code once "), platform
            assert entry["requirement"].endswith("."), platform
        if platform != "other":  # the vendor's own help page, and each staff line names the page it comes from
            assert entry["help_url"].startswith("https://"), platform
            assert entry["invite"] and all("https://" in line for line in entry["invite"]), platform
    requirement = {p: site_install.STEPS[p]["requirement"] for p in site_platform.PLATFORMS}
    assert "Premium plan" in requirement["wix"] and "domain" in requirement["wix"] and "published" in requirement["wix"]
    assert "Core, Plus, Advanced or legacy plan" in requirement["squarespace"]
    assert "Personal, Premium, Business or Commerce plan" in requirement["wordpress_com"]
    assert "paid Site plan" in requirement["webflow"] and "Core, Growth, Agency or Freelancer" in requirement["webflow"]
    for none_needed in ("wordpress", "godaddy", "shopify", "square_online", "other"):
        assert requirement[none_needed] is None, none_needed
    steps = {p: " ".join(site_install.STEPS[p]["steps"]) for p in site_platform.PLATFORMS}
    assert "Settings → Custom Code (in Development & integrations) → + Add Custom Code" in steps["wix"]
    assert "All pages" in steps["wix"] and "choose Head" in steps["wix"]
    assert "Custom Code" in steps["squarespace"] and "Code Injection" in steps["squarespace"]
    assert "in Header" in steps["squarespace"]
    assert "Insert Headers and Footers plugin by WPCode" in steps["wordpress_com"]
    assert "Code Snippets → Header & Footer" in steps["wordpress_com"] and "theme's header" in steps["wordpress"]
    assert "Online Store → Themes" in steps["shopify"] and "theme.liquid" in steps["shopify"]
    assert "</head>" in steps["shopify"] and "Customer events" in steps["shopify"]
    assert "Site settings → Custom code" in steps["webflow"] and "Head code" in steps["webflow"]
    assert "Settings → Tracking Tools" in steps["square_online"] and "head" in steps["square_online"]
    assert "publish" in steps["square_online"].lower()


def test_godaddys_builder_keeps_added_code_in_a_frame_so_its_steps_send_the_ads_to_an_admiry_page():
    """Q3, checked in Task 1 (the task report): GoDaddy's own help says the HTML section's embed "is placed inside of an
    iframe", and a published Websites + Marketing site's HTML section is an <iframe srcDoc> holding the code apart from
    the page's own DOM -- where Admiry's script cannot see the page's buttons and forms."""
    godaddy = site_install.steps_for("godaddy")
    assert godaddy["steps"] == [IN_A_FRAME]
    assert godaddy["placement"] == "page_section" and godaddy["in_frame"] is True and godaddy["requirement"] is None
    assert [p for p in site_platform.PLATFORMS if site_install.STEPS[p]["in_frame"]] == ["godaddy"]


def test_steps_for_a_platform_is_a_copy_and_other_for_any_it_does_not_know():
    assert site_install.steps_for("wix") == {"platform": "wix", **site_install.STEPS["wix"]}
    for unknown in (None, "", "drupal", "WIX", 7, {"platform": "wix"}):
        assert site_install.steps_for(unknown) == {"platform": "other", **site_install.STEPS["other"]}, unknown
    copy = site_install.steps_for("wix")
    copy["steps"].append("Something else")
    copy["invite"].clear()
    assert "Something else" not in site_install.STEPS["wix"]["steps"] and site_install.STEPS["wix"]["invite"]


# ---------------------------------------------------------------- the kit and the check (Q2, Q4, Q13)


def test_the_kits_install_part_follows_the_stored_platform(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    monkeypatch.setattr(crawl, "fetch_site", _read_as("wix"))
    _key, biz, _plan_id = _plan(client, conn, fake)
    assert business.latest_knowledge(conn, biz)[1]["website_platform"] == "wix"
    kit = landing.kit(conn, biz)
    wix = site_install.STEPS["wix"]
    assert kit["install"] == {
        "platform": "wix",
        "name": "Wix",
        "steps": wix["steps"],
        "requirement": wix["requirement"],
        "help_url": wix["help_url"],
        "placement": "head",
        "snippet": kit["tracking"]["snippet"],
    }
    # an address the owner gave and Admiry never read: no platform, so the generic steps (Q13); the kit's facts name
    # it, and a booking page given that way is the page's booking link
    latest = business.latest_knowledge(conn, biz)[1]
    unread = {k: v for k, v in latest.items() if k not in ("website", "website_platform")}
    business.add_knowledge(conn, biz, {**unread, "website_unread": "https://calendly.com/brightsmile"}, source="owner")
    kit = landing.kit(conn, biz)
    assert kit["install"]["platform"] == "other" and kit["install"]["steps"] == site_install.STEPS["other"]["steps"]
    assert kit["facts"]["website"] == "https://calendly.com/brightsmile"
    assert page_builder._booking_url(kit["facts"]) == "https://calendly.com/brightsmile"


def test_the_kit_and_its_email_say_head_and_carry_the_sites_steps(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
    monkeypatch,
):
    monkeypatch.setattr(crawl, "fetch_site", _read_as("squarespace"))
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    assert kit["tracking"]["where"] == WHERE
    text = mail.render("mail_kit.txt.j2", kit=kit, website=site_install.email_section(conn, biz))
    assert f"TRACKING\n{WHERE}\n" in text and "Put this on the page exactly as given" not in text
    # the row's sentence and the four ways first (Task 2, ruling R0-e), then the steps
    section = text.split("\nYOUR WEBSITE\n", 1)[1].split("\n\n")[1]
    squarespace = site_install.STEPS["squarespace"]
    assert section.splitlines() == [
        squarespace["requirement"],
        "On Squarespace:",
        *[f"{n}. {step}" for n, step in enumerate(squarespace["steps"], 1)],
        f"Help: {squarespace['help_url']}",
    ]
    assert not JARGON.search(section)
    # GoDaddy's one sentence is said, not numbered; a business with no website has no such section
    latest = business.latest_knowledge(conn, biz)[1]
    business.add_knowledge(conn, biz, {**latest, "website_platform": "godaddy"}, source="owner")
    godaddy = mail.render("mail_kit.txt.j2", kit=landing.kit(conn, biz), website=site_install.email_section(conn, biz))
    assert f"\n\nOn GoDaddy Websites + Marketing:\n{IN_A_FRAME}\n" in godaddy
    nothing = {k: v for k, v in latest.items() if k not in ("website", "website_unread", "website_platform")}
    business.add_knowledge(conn, biz, nothing, source="owner")
    assert site_install.email_section(conn, biz) is None and landing.kit(conn, biz)["install"] is None  # R1-f
    assert "YOUR WEBSITE" not in mail.render("mail_kit.txt.j2", kit=landing.kit(conn, biz), website=None)


def test_the_checks_tracking_fix_says_head_and_the_code_counts_anywhere(
    client,
    conn,
    fake,  # noqa: F811 - the fixture shadows the imported name
):
    _key, biz, _plan_id = _plan(client, conn, fake)
    kit = landing.kit(conn, biz)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")

    def tracking(html: str) -> dict:
        t = _serve({page["url"]: (200, html)})
        report = landing.check(page["url"], page, kit, conn=conn, transport=t, resolver=lambda h: PUBLIC)
        return next(r for r in report["rules"] if r["id"] == "tracking")

    missing = tracking(GOOD.replace("{pid}", "pg_other"))
    assert missing["ok"] is False
    assert missing["fix"] == (
        "Paste the tracking snippet from the kit inside <head> on every page (or before </body> where your site allows "
        "only that), exactly as given (it carries this page's token)."
    )
    snippet = kit["tracking"]["snippet"]
    assert tracking(GOOD.replace("{pid}", page["id"]))["ok"] is True  # before </body>, where GOOD has it
    in_head = GOOD.replace("{pid}", "pg_other").replace("</head>", f"{snippet}</head>")
    assert tracking(in_head)["ok"] is True
    # a builder that keeps the code in a frame (GoDaddy's HTML section: an <iframe srcdoc>) leaves it out of the page
    framed = GOOD.replace("{pid}", "pg_other").replace(
        "</body>", f'<iframe srcdoc="{escape(snippet)}"></iframe></body>'
    )
    assert tracking(framed)["ok"] is False


# ---------------------------------------------------------------- the owner's own site, read or not (Q13)


def test_the_profile_names_the_owners_address_read_or_not():
    row = {"id": "biz_1", "locale": "en-US", "claim_state": "claimed"}
    unread = profile.view(row, 1, {"website_unread": "https://brightsmile.example"})
    assert unread["business"]["website"] == "https://brightsmile.example"
    both = profile.view(row, 1, {"website": "https://a.example", "website_unread": "https://b.example"})
    assert both["business"]["website"] == "https://a.example"  # a site Admiry read comes first
    assert profile.view(row, 1, {})["business"]["website"] is None
    assert profile.own_site({"website_unread": "b.example"}) == "b.example" and profile.own_site(None) is None


def test_a_place_whose_website_is_the_owners_unread_address_is_close(conn, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    monkeypatch.setattr(
        places,
        "search",
        lambda q, k, **kw: [found(name="BSD Inc", place_id="ChIJ9", website="https://www.brightsmile.example/")],
    )
    knowledge = {"name": "Bright Smile Dental", "website_unread": "brightsmile.example"}
    out = listings.lookup(conn, config.load(), knowledge, {}, business_id="biz_1", rejected=[])
    assert [c["place_id"] for c in out] == ["ChIJ9"]  # start's lookup: by the owner's address alone
    out = listings.find(conn, config.load(), "Bright Smile Dental", business_id="biz_1", knowledge=knowledge)
    assert [c["place_id"] for c in out] == ["ChIJ9"]  # the owner's own search: the same


def test_a_booking_page_is_one_businesss_page_on_a_host_many_share(conn, monkeypatch):
    """Ruling R1-e (fix round 1): the booking services Admiry knows (booking_hosts.BOOKING_HOSTS, the one list the
    check and the page's booking link read too) are shared hosts for the listing rules -- two addresses there are one
    site only with the same page -- so an owner's unread booking page never makes another business's place close."""
    mine, theirs = "https://calendly.com/brightsmile", "https://calendly.com/othersmile"
    assert not listings.same_site(theirs, mine)
    assert listings.same_site("calendly.com/BrightSmile/", mine)  # the same page, its case and slash aside
    for a, b in (
        ("https://square.site/book/L1/bright-smile", "https://square.site/book/L2/other-smile"),
        ("https://www.zocdoc.com/practice/bright-smile-1", "https://www.zocdoc.com/practice/other-smile-2"),
        (
            "https://app.acuityscheduling.com/schedule.php?owner=1",
            "https://app.acuityscheduling.com/schedule.php?owner=2",
        ),
        (
            "https://book.squareup.com/appointments/a/location/L1",
            "https://book.squareup.com/appointments/b/location/L2",
        ),
        ("https://book.housecallpro.com/book/Bright-Smile/1", "https://book.housecallpro.com/book/Other-Smile/2"),
    ):
        assert not listings.same_site(a, b) and listings.same_site(a, a), a
    austin = found(
        name="Other Smile", address="1 Congress Ave, Austin, TX 78701, USA", place_id="ChIJ7", website=theirs
    )
    ours = found(name="BSD Inc", place_id="ChIJ9", website=f"{mine}/")
    rule = {"name": "Bright Smile Dental", "website": mine, "phone": None, "towns": ["Palo Alto"]}
    assert not listings.close(austin, **rule) and listings.close(ours, **rule)
    # start's lookup and the owner's own search, with the booking page as the owner's unread address (Q13)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    monkeypatch.setattr(places, "search", lambda q, k, **kw: [austin, ours])
    knowledge = {"name": "Bright Smile Dental", "service_area": ["Palo Alto"], "website_unread": mine}
    offered = listings.lookup(conn, config.load(), knowledge, {}, business_id="biz_1", rejected=[])
    assert [c["place_id"] for c in offered] == ["ChIJ9"]
    out = listings.find(conn, config.load(), "Bright Smile Dental", business_id="biz_1", knowledge=knowledge)
    assert [c["place_id"] for c in out] == ["ChIJ9"]
    # one list: what the check and the booking link read is what the listing rules share
    assert landing.BOOKING_HOSTS is booking_hosts.BOOKING_HOSTS
    assert set(booking_hosts.BOOKING_HOSTS) <= listings.SHARED_HOSTS
