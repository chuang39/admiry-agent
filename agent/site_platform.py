"""The platform an owner's website is built on (A3p-5 batch 2, plan Q1; spec W9), read from the HTML and the headers
Admiry already fetched -- the reading's crawl reads it on the first page it reads (crawl.fetch_site) -- never by a
request of its own, and never guessed from the address.

A signature is a generator meta tag, a response header, a page marker, or an asset the page loads: a `src` or a
`srcset` (on any element but a link, a frame or a form) or a `<link>`'s `href` (but a canonical or alternate page's).
A link (`<a href>`), a frame and the page's own words are other sites' pages and words, never what the site is built
on: a booking link to square.site or a word about Wix names neither. One platform's signatures and no other's name
it; none, or two platforms' (a header's included), are `other`, whose steps are the generic ones -- never a wrong
platform's steps (the plan's Review Focus). WordPress.com runs WordPress, so its signatures win over self-hosted
WordPress's rather than making two.

Three signatures differ from the plan's words, after the Task 1 check of real sites (the task report): GoDaddy's
hosting puts its own monitoring script from img1.wsimg.com on every site it hosts, WordPress or hand-made, so only the
builder's own assets there (`/isteam/`, `/blobby/go/`, `website-builder`) name GoDaddy Websites + Marketing; Weebly,
Square Online's older sibling, loads from editmysite.com too, so only Square Online's app there (`/app/`) names it,
and so does Square Online's own generator -- never a square.site asset, which Square's booking widget is on whatever
site embeds it (fix round 1, ruling R1-a); Shopify no longer sends `x-shopid`, and its `powered-by: Shopify` counts
as well. Jetpack on a self-hosted WordPress loads from WordPress.com's static hosts, so a WordPress.com asset names
WordPress.com only with its generator or one of its own markers (WPCOM_MARKS)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from urllib.parse import urlsplit

PLATFORMS = (
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
OTHER = "other"
ASSET_ATTRS = ("src", "data-src", "srcset", "data-srcset")  # what loads a part of the page
NOT_ASSETS = ("a", "area", "iframe", "frame", "form")  # another page, never this one's part
PAGE_RELS = frozenset({"canonical", "alternate", "shortlink", "next", "prev", "author", "me"})  # a <link> to a page
# What only WordPress.com's own sites carry: its mu-plugins (Simple sites), its site helper (sites with plugins), its
# action bar. Jetpack elsewhere says "isWpcom" -- none of these
WPCOM_MARKS = ("jetpack-mu-wpcom", "wpcomsh", "wpcom-actionbar", "/mu-plugins/wpcom")
WP_COM_STATIC = re.compile(r"s\d+\.wp\.com")
GODADDY_BUILDER = ("/isteam/", "/blobby/go/")  # img1.wsimg.com's paths only Websites + Marketing serves
WORDPRESS = re.compile(r"wordpress(?!\.com)\b")  # a generator "WordPress 6.8.2", never "WordPress.com"


class _Signs(HTMLParser):
    """One pass over a page (the crawl's first page is at most crawl.MAX_BYTES): its generator metas, the assets it
    loads as (host, path), lower case, and whether an element carries Webflow's `data-wf-page`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.generators: list[str] = []
        self.assets: list[tuple[str, str]] = []
        self.wf_page = False

    def handle_starttag(self, tag, attrs):
        found = {name: value or "" for name, value in attrs}
        self.wf_page = self.wf_page or "data-wf-page" in found
        if tag == "meta" and found.get("name", "").strip().lower() == "generator":
            self.generators.append(found.get("content", "").strip().lower())
        if tag in NOT_ASSETS:
            return
        if tag == "link" and found.get("href") and not set(found.get("rel", "").lower().split()) & PAGE_RELS:
            self._asset(found["href"])
        for name in ASSET_ATTRS:
            for url in found.get(name, "").split(",") if "srcset" in name else [found.get(name, "")]:
                self._asset(url.strip().split(" ")[0])

    def _asset(self, url: str) -> None:
        if not url:
            return
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:  # "http://[x": not an address, so not an asset
            return
        self.assets.append((host.lower(), parts.path.lower()))


def _loads(seen: _Signs, host: str, *paths: str) -> bool:
    """Whether the page loads an asset from `host` or a host under it -- and, given `paths`, one whose path starts
    with one of them."""
    return any((h == host or h.endswith("." + host)) and (not paths or p.startswith(paths)) for h, p in seen.assets)


def _generator(seen: _Signs, *words: str) -> bool:
    return any(word in generator for generator in seen.generators for word in words)


def _wix(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    return (
        _loads(seen, "static.wixstatic.com")
        or _loads(seen, "static.parastorage.com")
        or "x-wix-request-id" in headers
        or _generator(seen, "wix.com")
    )


def _squarespace(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    return _loads(seen, "static1.squarespace.com") or _generator(seen, "squarespace")


def _wordpress_com(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    asset = _loads(seen, "wordpress.com") or any(WP_COM_STATIC.fullmatch(h) for h, _ in seen.assets)
    own = any(g.startswith("wordpress.com") for g in seen.generators) or any(mark in html for mark in WPCOM_MARKS)
    return asset and own


def _wordpress(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    paths = any("/wp-content/" in p or "/wp-includes/" in p for _, p in seen.assets)
    return paths or any(WORDPRESS.match(g) for g in seen.generators)


def _godaddy(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    builder = any(
        h == "img1.wsimg.com" and (p.startswith(GODADDY_BUILDER) or "website-builder" in p) for h, p in seen.assets
    )
    return builder or _generator(seen, "go daddy website builder", "starfield technologies")


def _shopify(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    powered = headers.get("powered-by", "").strip().lower() == "shopify"
    return _loads(seen, "cdn.shopify.com") or "x-shopid" in headers or powered


def _webflow(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    return seen.wf_page or _loads(seen, "website-files.com") or _generator(seen, "webflow")


def _square_online(seen: _Signs, headers: Mapping[str, str], html: str) -> bool:
    return _loads(seen, "editmysite.com", "/app/") or _generator(seen, "square online")


RULES: dict[str, Callable[[_Signs, Mapping[str, str], str], bool]] = {
    "wix": _wix,
    "squarespace": _squarespace,
    "wordpress_com": _wordpress_com,
    "wordpress": _wordpress,
    "godaddy": _godaddy,
    "shopify": _shopify,
    "webflow": _webflow,
    "square_online": _square_online,
}


def detect(html: str, headers: Mapping[str, str]) -> str:
    """The platform `html` (a page as the crawl decoded it) and its response `headers` tell, one of PLATFORMS: the one
    platform whose signatures they carry, else `other` -- none, or two platforms'."""
    seen = _Signs()
    seen.feed(html or "")
    seen.close()
    lower = {name.lower(): value for name, value in (headers or {}).items()}
    found = {name for name, rule in RULES.items() if rule(seen, lower, html or "")}
    if "wordpress_com" in found:
        found.discard("wordpress")
    return found.pop() if len(found) == 1 else OTHER
