"""Read a business's website for the Business Agent (spec §4.2 row 1: crawl ≤ 20 pages).

Public hosts only — the SSRF guard resolves the name and refuses private, loopback and link-local
addresses — the same site as the start URL (its host, and the host's bare or `www.` form, itself
vetted as public: twin_host), hard caps on pages, bytes, characters and time; plain text out,
never HTML. Redirects are never followed automatically: a Location is vetted like a link (the
same site or dropped), so a page cannot bounce the crawler to a private address. Bodies are
streamed and cut at MAX_BYTES or at the time budget, which is checked before every page and after
every body chunk; the cut counts inflated bytes, and the inflating is read_capped's own, never
httpx's, so a compressed body cannot balloon past the cap in memory. Accepted residuals: the name
is resolved once up front and again by the HTTP client per request (DNS rebinding needs attacker
DNS with a short TTL); the character cap is checked between pages, so one page can overshoot it by
up to PER_PAGE_CHARS; httpx's timeouts are per operation, so once the budget is spent a page body
runs past it by at most one chunk read (<= TIMEOUT_S), while the connect and a server trickling
its status line and headers are bounded only per read (TIMEOUT_S each) — a total deadline on that
phase needs a watchdog outside httpx. The transport, the resolver and the clock are injectable for
tests. The first page read also tells the platform the site is built on (site_platform, A3p-5 batch 2 Q1): from that
page's HTML and headers, which the crawl fetched anyway."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from . import site_platform

MAX_PAGES = 20
MAX_BYTES = 2_000_000  # per page body (2026-10-12: a page-builder page keeps ~800 KB of style before its <body>)
PER_PAGE_CHARS = 20_000
TOTAL_CHARS = 100_000
BUDGET_S = 10.0
TIMEOUT_S = 5.0
UA = "AdmiryBot/1.0 (+https://admiry.ai/local)"
# What read_capped can inflate within its cap, and so all a fetch here asks for: never zstd or br, which httpx
# would inflate a whole network read of at once (a 6 KB zstd body is 200 MB).
ACCEPT_ENCODING = "gzip, deflate"
SKIP = {"script", "style", "noscript", "svg", "template"}
BINARY_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".mp4", ".mp3", ".svg", ".ico")
HTTP_LOGGERS = ("httpx", "httpcore")  # the loggers that print the URL of each request the client makes


def quiet_http_logs() -> None:
    """httpx logs every request it makes as an INFO line holding the whole URL ("HTTP Request: GET ..."), and
    research-run reads the websites of businesses nearby with it: a website lives in memory for one run (plan K4 D5),
    not in a log that journald and Airflow's task log keep. Called where the agent sets its logging up (agent/tick.py,
    python -m agent): httpx and httpcore speak at WARNING and above only."""
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class CrawlError(ValueError):
    """The URL cannot be fetched safely (private host, bad scheme, unresolvable)."""


@dataclass
class Page:
    url: str
    title: str
    text: str
    # the platform the site is built on (site_platform.detect), on the first page read only (A3p-5 batch 2, Q1)
    platform: str | None = None


INVALID_URL = "website_url must be a valid http(s) address"


def resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except socket.gaierror as e:
        raise CrawlError(f"cannot resolve {host}") from e
    except UnicodeError as e:  # IDNA refuses the name (an empty or over-long label) before any lookup
        raise CrawlError(INVALID_URL) from e


def _split(url: str):
    """urlsplit(url), also refusing what httpx would refuse at fetch time (a bad port, control
    characters, an over-long URL). Raises ValueError (UnicodeError included) or httpx.InvalidURL."""
    parts = urlsplit(url)
    _ = parts.port, httpx.URL(url)
    return parts


def public_host(url: str, *, resolver: Callable[[str], list[str]] = resolve) -> str:
    """The host of a public http(s) URL; CrawlError otherwise (never another exception)."""
    try:
        parts = _split(url)
    except (ValueError, httpx.InvalidURL) as e:  # e.g. "http://[", a non-numeric port
        raise CrawlError(INVALID_URL) from e
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CrawlError("website_url must be an http(s) address")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise CrawlError("website_url must be a public address")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a) for a in resolver(host)]
        except CrawlError:
            raise
        except ValueError as e:  # UnicodeError included: a name that cannot be encoded, or junk back
            raise CrawlError(INVALID_URL) from e
    if not addresses or not all(a.is_global for a in addresses):
        raise CrawlError("website_url must be a public address")
    return host


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.parts, self.links, self._skip, self._in_title = "", [], [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract(html: str) -> tuple[str, str, list[str]]:
    """(title, text, links) of one page; whitespace collapsed, text capped."""
    p = _Text()
    p.feed(html)
    text = re.sub(r"\s+", " ", " ".join(p.parts)).strip()[:PER_PAGE_CHARS]
    return p.title.strip(), text, p.links


def read_capped(
    r: httpx.Response, cap: int, *, deadline: float | None = None, clock: Callable[[], float] = time.monotonic
) -> bytes:
    """At most `cap` bytes of a streamed response's body, as the page (inflated), never more in memory: the
    raw stream is read chunk by chunk and a gzip/deflate body is inflated here with a `max_length`, not by
    httpx, whose decoders inflate each network read whole. A body in any other encoding (one this module
    never asks for, ACCEPT_ENCODING) reads as b"". Reading also stops at `deadline` (checked after every
    chunk): httpx's timeout bounds each read, not the whole body. What was read before a cut is kept, and a
    body that breaks mid-way keeps what inflated before the break."""
    if r.is_stream_consumed:  # an answer built whole in memory (a MockTransport's bytes): httpx already read it
        return r.content[:cap]
    coding = r.headers.get("content-encoding", "").strip().lower()
    if coding in ("", "identity"):
        inflate = None
    elif coding in ("gzip", "x-gzip", "deflate"):
        inflate = zlib.decompressobj(zlib.MAX_WBITS | 32)  # a gzip or a zlib header, detected
    else:
        return b""
    out, size = [], 0
    try:
        for data in r.iter_raw():
            while data and size < cap:
                if inflate is None:
                    piece, data = data[: cap - size], b""
                else:
                    piece, data = inflate.decompress(data, cap - size), inflate.unconsumed_tail
                out.append(piece)
                size += len(piece)
            if size >= cap or (inflate is not None and inflate.eof) or (deadline is not None and clock() >= deadline):
                break  # eof: whatever follows a finished gzip member is junk zlib would only copy and keep
    except zlib.error:
        pass  # a corrupt or raw-deflate body: keep what inflated before it
    return b"".join(out)


def decode(raw: bytes, encoding: str | None) -> str:
    """The page as text. httpx takes any name `codecs.lookup` knows as the charset, so `charset=rot13` (or
    base64, hex, zlib, idna) would raise here rather than in the check that asked: those read as utf-8."""
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeError):
        return raw.decode("utf-8", errors="replace")


def _read(
    client: httpx.Client, url: str, *, deadline: float, clock: Callable[[], float]
) -> tuple[httpx.Response, bytes]:
    """GET with the body streamed and cut at MAX_BYTES (read_capped), so a huge or compressed page never
    fills memory, and at the crawl's deadline, so a server trickling bytes cannot hold the crawl."""
    with client.stream("GET", url) as r:
        body = read_capped(r, MAX_BYTES, deadline=deadline, clock=clock)
    return r, body


def twin_host(host: str) -> str:
    """The other form of a site's host (A3p-5 ruling R2-h): "www.acme.example" for "acme.example", and back."""
    return host.removeprefix("www.") if host.startswith("www.") else f"www.{host}"


def fetch_site(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Callable[[str], list[str]] = resolve,
    clock: Callable[[], float] = time.monotonic,
    max_pages: int = MAX_PAGES,
) -> list[Page]:
    """The start page and up to `max_pages` - 1 pages (MAX_PAGES by default) of the same site it links to, as plain
    text. The site is the start's host and its other form, bare or `www.` (twin_host, A3p-5 ruling R2-h: an owner types
    "acme.example" for a site that redirects to "www.acme.example"): the other form is vetted as the start's is
    (public_host) the first time a link or a redirect names it, and dropped for good when it is not public. The first
    page read carries the site's platform (`Page.platform`, site_platform.detect on its own HTML and headers: A3p-5
    batch 2, Q1); the others none."""
    host = public_host(url, resolver=resolver)
    twin, vetted = twin_host(host), {host: True}
    pages: list[Page] = []
    queue, seen, total, deadline = [url], {url}, 0, clock() + BUDGET_S

    def ours(p) -> bool:
        if p.hostname not in (host, twin):
            return False
        if p.hostname not in vetted:
            try:
                vetted[p.hostname] = public_host(f"{p.scheme}://{p.hostname}/", resolver=resolver) == p.hostname
            except CrawlError:
                vetted[p.hostname] = False
        return vetted[p.hostname]

    def enqueue(base: str, link: str) -> None:
        try:
            target = urljoin(base, link).split("#")[0]
            p = _split(target)
        except (ValueError, httpx.InvalidURL):  # unparsable, e.g. "http://[object Object]": skipped
            return
        if p.scheme in ("http", "https") and target not in seen and not p.path.lower().endswith(BINARY_EXT) and ours(p):
            seen.add(target)
            queue.append(target)

    with httpx.Client(
        transport=transport,
        timeout=TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": UA, "Accept-Encoding": ACCEPT_ENCODING},
    ) as client:
        while queue and len(pages) < max_pages and total < TOTAL_CHARS and clock() < deadline:
            page_url = queue.pop(0)
            try:
                r, body = _read(client, page_url, deadline=deadline, clock=clock)
            except (httpx.HTTPError, httpx.InvalidURL):  # InvalidURL is not an HTTPError; _split vets first
                continue
            if r.is_redirect:  # vetted like a link: the same site or dropped, never auto-followed
                if r.headers.get("location"):
                    enqueue(page_url, r.headers["location"])
                continue
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("text/html"):
                continue
            html = decode(body, r.encoding)
            title, text, links = extract(html)
            if text:
                platform = None if pages else site_platform.detect(html, r.headers)
                pages.append(Page(url=page_url, title=title, text=text, platform=platform))
                total += len(text)
            for link in links:
                enqueue(page_url, link)
    return pages
