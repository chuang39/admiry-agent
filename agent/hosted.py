"""Admiry Pages, the hosted side (spec 2026-09-28-admiry-pages-design.md; plan
2026-09-30-agent-a3p-2-hosted-pages-and-templates.md H1-H8, H16, H17, H19, H21): the pages-domain slug, a
page's linear version history and unpublished previews, the HTML sanitiser and the CSP they both serve
under, and (Task 2) serving by Host -- a tenant's live page, its privacy notice and its photos on the pages
domain, a preview on the agent host, and the check (`/pages/ask`) Caddy's `on_demand_tls` calls before it
ever issues a certificate. `router` holds only the three routes that stay on the agent host itself
(`/p/{token}`, its photos, `/pages/ask`) -- a tenant `Host` never reaches FastAPI's routing at all;
`agent/app.py`'s `_TenantHost` middleware calls `serve` directly and never calls into the rest of the stack
(H17), which is what keeps `/mcp`, `/connect` and `/t/*` from existing on a tenant host.

H19 (review fix round 1): the sanitiser refuses rather than tries to tell a safe construct from a dangerous
one by inspecting it closely -- an abrupt-closing HTML comment is refused outright rather than parsed
browser-exactly, an inline script that so much as names `location` or calls `open(` is refused rather than
judged by what it targets, `<embed>`/`<object>`/`srcdoc` are refused outright rather than allow-listed, and
any non-http(s) scheme anywhere is refused rather than reasoned about per attribute.

H21 (review fix round 2): an `on*` attribute (`onclick`, `onerror`, ...) is refused on any element, the same
capability-not-payload call as H19's inline-script rule, since it runs inline JS with no `<script>` tag at
all; `_bad_scheme` strips every C0 control, not just tab/CR/LF, before it looks for a scheme, matching how a
browser trims a URL; and the raw comment scan exempts `<script>` bodies, where a leading `<!--` is a
long-standing, valid, harmless JS idiom (ECMAScript Annex B), not a comment to police.

H42 (final review): a preview is anyone's HTML, so it is served sandboxed (`csp(preview=True)`: an opaque origin
whose fetches reach only the public `/t/` endpoints, H45) and on the agent host only (`on_agent_host`) -- the
browser host, where the owner's session cookie lives, answers its token 404. H44: the host checks read a URL the way a browser does (`_url_host`: a
backslash is a slash), and a `formaction` is host-checked like the form's own `action`."""

from __future__ import annotations

import contextlib
import html
import re
import unicodedata
from contextvars import ContextVar
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from psycopg.types.json import Jsonb

from . import business, config, db, landing, leads
from .templating import env as _env

# ---------------------------------------------------------------- constants (H1, H2, H5, H6)

RESERVED = frozenset({"www", "mail", "agent", "api", "admin", "home", "app", "static", "t", "p", "u", "pages"})
GOOGLE_SCRIPT_HOSTS = (
    "www.googletagmanager.com",
    "www.google-analytics.com",
    "googleads.g.doubleclick.net",
    "www.google.com",
)
BOOKING_FRAME_HOSTS = (
    "calendly.com",
    "*.calendly.com",
    "*.acuityscheduling.com",
    "*.squareup.com",
    "*.square.site",
    "www.google.com",
)
# H6's connect-src: the same Google hosts as GOOGLE_SCRIPT_HOSTS, in a different order, plus the beacon's
# own endpoint -- kept as its own tuple rather than re-deriving it from GOOGLE_SCRIPT_HOSTS, since CSP
# directive order is otherwise free but this ruling's literal string is the one to reproduce.
_GOOGLE_CONNECT_HOSTS = (
    "www.google-analytics.com",
    "www.googletagmanager.com",
    "googleads.g.doubleclick.net",
    "www.google.com",
    "stats.g.doubleclick.net",
)
MAX_HTML_BYTES = 1_000_000
MIN_TEXT_CHARS = 500
PREVIEW_TTL = timedelta(hours=24)
_NOINDEX_META = '<meta name="robots" content="noindex">'
# H19: every attribute that can carry a URL, on any tag -- checked for a non-http(s) scheme regardless of
# what the tag is (a <script src> and an <a href> get exactly the same scheme check).
_URL_ATTRS = frozenset({"src", "href", "action", "formaction", "data", "poster", "xlink:href", "background"})
_SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tel", "sms"})


class Refused(Exception):
    """What `sanitise` found wrong with a page, for a caller that wants to raise instead of branching on
    the list (Task 2's preview/publish tools). `str(e)` is every sentence, space-joined."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(" ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------- the slug (H2)

_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A DNS-safe label from a business name (H2): ASCII-folded, lowercased, runs of anything else
    collapsed to one hyphen, cut to 40 characters. Anything left shorter than 3 characters -- including ""
    -- falls back to "page"; `hostname_for` is what makes the result unique and skips RESERVED."""
    ascii_ = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_BAD.sub("-", ascii_.lower()).strip("-")[:40].strip("-")
    return slug if len(slug) >= 3 else "page"


def hostname_for(conn: psycopg.Connection, biz: str, name: str, domain: str) -> str:
    """The business's existing hosted hostname, else a free "<slug>[-n].<domain>" (H2): candidates that are
    RESERVED or already taken by another business's page skip straight to -2, -3, ... This never writes
    anything -- publish_page is what makes a hostname stick, by setting page.hostname, and once that is set
    this keeps returning it whatever `name` is passed on a later call, and however many other rows the
    business's page has picked up meanwhile (an owner address in between, say): the query below is not
    filtered to the live row, so a business's minted slug survives a detour through its own address and back
    (H22, A3p-2 Task 3 review). A stored `hostname` is trusted only when it still ends with `.domain` -- never
    true for one `landing.set_owner_url` wrote before H22 fixed it to stop, so a page from before this fix, or
    one moved to a different pages domain, mints fresh here rather than handing back a foreign address."""
    current = conn.execute(
        "SELECT hostname FROM page WHERE business_id = %s AND hostname IS NOT NULL "
        "ORDER BY (status = 'published') DESC, created_at DESC LIMIT 1",
        (biz,),
    ).fetchone()
    if current and current["hostname"].endswith("." + domain):
        return current["hostname"]
    base = slugify(name)
    n = 1
    while True:
        slug = base if n == 1 else f"{base}-{n}"
        if slug not in RESERVED:
            candidate = f"{slug}.{domain}"
            if not conn.execute("SELECT 1 FROM page WHERE hostname = %s", (candidate,)).fetchone():
                return candidate
        n += 1


# ---------------------------------------------------------------- the parser (H5)

_SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "template"})
_BOOKING_FRAME_TAGS = frozenset({"iframe", "frame"})
_BLOCKED_OBJECT_TAGS = frozenset({"embed", "object"})  # H19: refused outright, never allow-listed by host


class _Scan(HTMLParser):
    """One linear pass collecting everything `sanitise` and `visible_text` need: external script/frame/form
    references (a `formaction` on any tag counts as a form's target, H44), `<embed>`/`<object>` tags, any `srcdoc`
    attribute, any `on*` event-handler attribute (H21), every URL-bearing attribute on every tag (H19), <meta
    http-equiv="refresh">, <base href>, inline <script> text, and the visible text outside
    script/style/head/noscript/template."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.inline_scripts: list[str] = []
        self.frames: list[str] = []
        self.objects: list[str] = []
        self.has_srcdoc = False
        self.has_event_handler = False
        self.urls: list[str] = []
        self.forms: list[str] = []
        self.meta_refresh = False
        self.base_href: str | None = None
        self.text: list[str] = []
        self._skip: list[str] = []
        self._script_has_src = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script":
            src = a.get("src")
            self._script_has_src = bool(src)
            if src:
                self.scripts.append(src)
            self._skip.append(tag)
        elif tag in _SKIP_TAGS:
            self._skip.append(tag)
        if tag in _BOOKING_FRAME_TAGS:
            if a.get("src"):
                self.frames.append(a["src"])
        elif tag in _BLOCKED_OBJECT_TAGS:
            self.objects.append(tag)
        elif tag == "form":
            self.forms.append(a.get("action") or "")
        elif tag == "meta" and (a.get("http-equiv") or "").strip().lower() == "refresh":
            self.meta_refresh = True
        elif tag == "base" and a.get("href"):
            self.base_href = a["href"]
        if "formaction" in a:  # H44: a submit button's own target overrides its form's action -- checked like it
            self.forms.append(a["formaction"] or "")
        if "srcdoc" in a:
            self.has_srcdoc = True
        # H21: html.parser lowercases attribute names, so this catches ONCLICK/OnLoad/... too -- any
        # attribute at all whose name starts with "on" is treated as an event handler, the same
        # capability-not-payload call as H19's inline-script rule (no attempt to enumerate known handler
        # names, e.g. onclick/onerror/onload/...).
        if any(k.startswith("on") for k in a):
            self.has_event_handler = True
        for attr in _URL_ATTRS:
            val = a.get(attr)
            if val:
                self.urls.append(val)

    def handle_endtag(self, tag: str) -> None:
        if self._skip and self._skip[-1] == tag:
            self._skip.pop()

    def handle_data(self, data: str) -> None:
        if self._skip and self._skip[-1] == "script":
            if not self._script_has_src:
                self.inline_scripts.append(data)
        elif not self._skip:
            self.text.append(data)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _scan(html_text: str) -> _Scan:
    p = _Scan()
    p.feed(html_text)
    p.close()
    return p


_HTTP_SLASHES = re.compile(r"^https?:[/]*", re.IGNORECASE)


def _url_host(url: str) -> str | None:
    """The host a browser would fetch `url` from, or None for a same-page reference: empty, a hash/query,
    or a single-slash path. A protocol-relative "//host/..." DOES have a host -- urlsplit parses it as one,
    so it is never mistaken for a same-page path.

    H44 (final review): `url` is first read the way a browser reads it, never the way it is spelled -- every
    C0 control (a tab or a newline above all) dropped wherever it falls and the spaces around it trimmed (as
    `_bad_scheme` does), every backslash a slash (an http(s) URL, or a relative one on an http(s) page, treats
    the two alike: `\\\\evil/x` is `//evil/x`), and after `http:`/`https:` any number of slashes, none included,
    before the host (`https:///evil`, `https:evil` -- a browser skips them all, or reads a bare `https:evil` on an
    http page as a host). A URL urlsplit cannot parse at all comes back as its own "host", which matches no
    allow-list: refused, never waved through as same-page."""
    url = _CONTROL_CHARS.sub("", url or "").strip().replace("\\", "/")
    if not url or url.startswith(("#", "?")) or (url.startswith("/") and not url.startswith("//")):
        return None
    m = _HTTP_SLASHES.match(url)
    if m:
        url = "//" + url[m.end() :]
    try:
        return urlsplit(url).hostname or None
    except ValueError:  # e.g. "//[evil": an unclosed IPv6 bracket
        return url


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    """`host` against literal entries and "*.suffix" entries, matched on a "." boundary: "*.calendly.com"
    matches "x.calendly.com", never bare "calendly.com" or "calendly.com.evil"."""
    host = host.lower()
    return any(host == p.lower() or (p.startswith("*.") and host.endswith(p[1:].lower())) for p in patterns)


def _allowed(url: str, patterns: tuple[str, ...]) -> bool:
    """True when `url` names no host of its own (same-page) or its host matches `patterns`."""
    host = _url_host(url)
    return host is None or _host_matches(host, patterns)


def _form_ok(action: str, *, page_id: str, own_host: str | None, agent_base: str) -> bool:
    if _allowed(action, (own_host,) if own_host else ()):
        return True
    return action == f"{agent_base}/t/form/{page_id}"


def _bad_comment(html_text: str) -> bool:
    """True when `html_text` has an abrupt-closing empty comment (`<!-->` or `<!--->`) or a `<!--` with no
    matching `-->` anywhere after it (H19, Review Focus 1), outside any `<script>` body. Real browsers close
    `<!-->`/`<!--->` immediately -- the HTML5 tokenizer's "abrupt-closing-of-empty-comment" state -- so
    whatever follows is live markup; `html.parser` instead keeps consuming until the next real `-->`,
    silently dropping everything in between (including a live-looking `<script src="evil">` an attacker
    placed right after the abrupt close, banking on our parser -- but not the browser -- treating it as
    still inside the comment). Rather than reproduce that browser state machine, refuse any HTML that is not
    a plain, well-formed `<!-- ... -->`.

    `<script>...</script>` bodies are skipped entirely, start to matching end tag, before ever looking for a
    `<!--` inside them (H21, Review Focus 3): ECMAScript's Annex B makes a `<!--` at the start of a script a
    valid line comment -- the historical "hide this script from browsers that don't know `<script>`" idiom,
    `<script><!--\\ncode();\\n//--></script>`, often written without the closing `//-->` since it only
    matters to browsers nobody runs -- so it is not a comment for this function to police at all; whatever a
    script's content does is `_navigates`'s job, on the text `_Scan` already extracts correctly regardless
    of any `<!--` inside it (html.parser's own script handling is unrelated to this function's bug).

    Linear in `len(html_text)` (H21 round 3, a real regression): `next_comment`/`next_script` are found once
    and only re-searched once the cursor `i` has moved past them, never on every loop turn regardless. A
    document with many `<script>` tags and no comment anywhere used to call `find("<!--", i)` once per tag
    -- and every one of those calls had to scan all the way to the end to confirm there was truly nothing
    left to find, since a failed `find` cannot stop early -- turning a single skip-every-script pass into
    O(tags x len(html_text)) instead of O(len(html_text))."""
    low = html_text.lower()
    n = len(low)
    i = 0
    next_comment = low.find("<!--")
    next_script = low.find("<script")
    while True:
        if next_comment != -1 and next_comment < i:
            next_comment = low.find("<!--", i)
        if next_script != -1 and next_script < i:
            next_script = low.find("<script", i)
        if next_script != -1 and (next_comment == -1 or next_script < next_comment):
            tag_end = low.find(">", next_script)
            body_start = tag_end + 1 if tag_end != -1 else next_script + len("<script")
            close = low.find("</script", body_start)
            i = close + len("</script") if close != -1 else n
            continue
        if next_comment == -1:
            return False
        rest = next_comment + 4
        if low.startswith(">", rest) or low.startswith("->", rest):
            return True
        end = low.find("-->", rest)
        if end == -1:
            return True
        i = end + 3


_JS_ESCAPE_U = re.compile(r"\\u([0-9a-fA-F]{4})")
_JS_ESCAPE_X = re.compile(r"\\x([0-9a-fA-F]{2})")
_NAV_LOCATION = re.compile(r"\blocation\b")
_NAV_OPEN = re.compile(r"\bopen\s*\(")


def _decode_js_text(text: str) -> str:
    """Undo the simplest obfuscations before an inline script is checked for navigation (H19): HTML entities
    and JS `\\uXXXX`/`\\xXX` escapes decoded, then lowercased -- so `l&#111;cation` and
    `\\u006c\\u006fcation` both read as "location"."""
    text = html.unescape(text)
    text = _JS_ESCAPE_U.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = _JS_ESCAPE_X.sub(lambda m: chr(int(m.group(1), 16)), text)
    return text.lower()


def _navigates(script_text: str) -> bool:
    """True when a decoded, whitespace-stripped inline script names `location` in any form (a bare
    identifier, `window['location']`, `top.location`, `location['href']` -- one word-boundary check on
    "location" catches all of these, since a quote or bracket is as much a boundary as anything else),
    calls `open(` on anything (`window.open(`, `top.open(`, bare `open(`), or reads `document.url` (H19:
    refuse the capability outright rather than try to tell a safe navigation from a dangerous one by its
    target -- the old check only refused a `location.href`-style assignment to an absolute URL on another
    host, which a bare `location=`, `window.open(`, or a protocol-relative target all slipped past). A
    landing page never legitimately needs any of these. Whitespace is stripped (not just trimmed) after
    lowercasing so `document . url` -- spacing a real browser tolerates -- cannot dodge the plain substring
    check the way it could dodge nothing in the two regexes, which already tolerate it via `\\b`/`\\s*`."""
    text = re.sub(r"\s+", "", _decode_js_text(script_text))
    return bool(_NAV_LOCATION.search(text) or _NAV_OPEN.search(text) or "document.url" in text)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")  # H21: every C0 control plus DEL, not just tab/CR/LF
_SCHEME = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*):")


def _bad_scheme(value: str) -> bool:
    """True when `value` (an attribute meant to hold a URL) names a scheme other than http(s) or a contact
    scheme (mailto:/tel:/sms:) -- javascript:, data:, vbscript:, blob:, file:, ... A value with no scheme at
    all (a relative path, a fragment, empty) is fine. Every C0 control character (\\x00-\\x1f) and DEL
    (\\x7f) is removed first, wherever it falls -- not just tab/CR/LF (H21) -- the way a browser trims a URL
    before parsing it, so neither "java\\tscript:" nor "\\x01javascript:" nor "java\\x00script:" can hide the
    scheme from this check."""
    cleaned = _CONTROL_CHARS.sub("", value or "")
    m = _SCHEME.match(cleaned)
    return bool(m) and m.group(1).lower() not in _SAFE_SCHEMES


# ---------------------------------------------------------------- the sanitiser and injection (H5)


def sanitise(html: str, *, page_id: str, own_host: str | None, agent_base: str) -> list[str]:
    """Every reason to refuse `html` as a hosted page (H5, H19, H21), each one sentence, all returned together; an
    empty list means the page is fine as given. Never edits and never raises -- Task 2's tools are what turn
    a non-empty list into a refusal (Refused, or a tool's own error). `own_host` is the host the page will
    (or does) serve from; None -- not yet known, e.g. a first preview -- makes every off-host reference in
    the script/base-href host checks count as elsewhere, but relative references are always fine regardless
    (the navigation and scheme checks below do not look at `own_host` at all -- H19: what an inline script
    can navigate to, or what scheme an attribute names, is refused on its own terms). The sentences of
    `problems`, which names each one's rule."""
    return [sentence for _rule, sentence in problems(html, page_id=page_id, own_host=own_host, agent_base=agent_base)]


def problems(
    html: str,
    *,
    page_id: str,
    own_host: str | None,
    agent_base: str,
    text_minimum: bool = True,
    inline_scripts: bool = True,
) -> list[tuple[str, str]]:
    """`sanitise`'s reasons as (rule, sentence) pairs, the rule a short name a bundle's structured problem carries
    (A3p-3 spec §6.5; plan 2026-10-04-agent-a3p-3-3 B6): size, comment, text, script_host, frame_host, object,
    srcdoc, event_handler, form_target, meta_refresh, navigation, base_href, scheme. A bundle's pages turn two rules
    off: `text_minimum` (the visible-text minimum is its home page's alone) and `inline_scripts` (a framework's inline
    scripts carry the page's words, so they are contained by the response headers like its script files, never read
    line by line)."""
    found: list[tuple[str, str]] = []
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        found.append(("size", f"The page is too big ({size:,} bytes); the limit is {MAX_HTML_BYTES:,} bytes."))

    if _bad_comment(html):
        found.append(("comment", "Comments must be well-formed (<!-- … -->)."))

    p = _scan(html)
    text = _squash("".join(p.text))
    if text_minimum and len(text) < MIN_TEXT_CHARS:
        found.append(
            ("text", f"The page needs more real text ({len(text)} characters, at least {MIN_TEXT_CHARS} required).")
        )

    agent_host = urlsplit(agent_base).hostname or agent_base
    script_hosts = ((own_host,) if own_host else ()) + (agent_host,) + GOOGLE_SCRIPT_HOSTS
    bad_scripts = [s for s in p.scripts if not _allowed(s, script_hosts)]
    if bad_scripts:
        found.append(
            (
                "script_host",
                "Scripts may load only from this page, Admiry or Google's tag hosts: " + ", ".join(bad_scripts) + ".",
            )
        )

    bad_frames = [s for s in p.frames if not _allowed(s, BOOKING_FRAME_HOSTS)]
    if bad_frames:
        found.append(
            (
                "frame_host",
                "Frames may show only a booking page (Calendly, Acuity, Square) or a Google map: "
                + ", ".join(bad_frames)
                + ".",
            )
        )

    if p.objects:
        found.append(("object", "Embedded objects are not allowed; use a booking frame."))

    if p.has_srcdoc:
        found.append(("srcdoc", "Frames must load a booking page by address, not inline content."))

    if p.has_event_handler:
        found.append(
            (
                "event_handler",
                "Inline event handlers (onclick, onload, …) are not allowed; use a <script> that listens.",
            )
        )

    bad_forms = [a for a in p.forms if not _form_ok(a, page_id=page_id, own_host=own_host, agent_base=agent_base)]
    if bad_forms:
        target = f"{agent_base}/t/form/{page_id}"
        found.append(
            (
                "form_target",
                f"Forms must post to this page or to Admiry ({target}): " + ", ".join(bad_forms) + " posts elsewhere.",
            )
        )

    if p.meta_refresh:
        found.append(
            (
                "meta_refresh",
                'The page must not redirect: a <meta http-equiv="refresh"> tag sends visitors away on its own.',
            )
        )

    if inline_scripts and any(_navigates(s) for s in p.inline_scripts):
        found.append(("navigation", "Scripts on the page must not navigate away (no location, window.open)."))

    if p.base_href and not _allowed(p.base_href, (own_host,) if own_host else ()):
        found.append(("base_href", f'The page must not redirect: <base href="{p.base_href}"> points at another site.'))

    if any(_bad_scheme(u) for u in p.urls):
        found.append(("scheme", "Links and sources must use https (no javascript:, data: …)."))

    return found


class _AttrScan(HTMLParser):
    """`inject`'s dedup (H19, Review Focus 7): whether a `<script data-page="...">` for `page_id` or a
    `<meta name="robots" content="noindex">` is already on the page, checked by parsing attributes -- so a
    tag an assistant pasted with single quotes or a different attribute order is still recognised, unlike a
    literal substring search for one exact spelling."""

    def __init__(self, page_id: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.page_id = page_id
        self.has_snippet = False
        self.has_noindex = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and self.page_id is not None and a.get("data-page") == self.page_id:
            self.has_snippet = True
        elif (
            tag == "meta"
            and (a.get("name") or "").strip().lower() == "robots"
            and "noindex" in (a.get("content") or "").lower()
        ):
            self.has_noindex = True


def inject(html: str, *, snippet: str, noindex: bool) -> str:
    """`snippet` (the tracking `<script data-page="...">` `landing.snippet` renders) before `</body>` --
    skipped when a tag with that same `data-page` value is already on the page, so an assistant that pasted
    it itself is left untouched. For previews, a `noindex` robots `<meta>` goes into `<head>` (or at the very
    start when there is none), also added only once."""
    out = html
    page_id = None
    if snippet:
        m = re.search(r'data-page="([^"]*)"', snippet)
        page_id = m.group(1) if m else ""
    scan = _AttrScan(page_id)
    scan.feed(out)
    scan.close()
    if snippet and not scan.has_snippet:
        end = re.search(r"</body\s*>", out, re.IGNORECASE)
        out = out[: end.start()] + snippet + out[end.start() :] if end else out + snippet
    if noindex and not scan.has_noindex:
        head = re.search(r"<head[^>]*>", out, re.IGNORECASE)
        out = out[: head.end()] + _NOINDEX_META + out[head.end() :] if head else _NOINDEX_META + out
    return out


def visible_text(html: str) -> str:
    """The page's rendered text, outside script/style/head/noscript/template, whitespace collapsed to single
    spaces."""
    return _squash("".join(_scan(html).text))


# ---------------------------------------------------------------- the CSP (H6)


def csp(agent_base: str, *, frame_ancestors: str = "'none'", preview: bool = False) -> str:
    """One CSP string for every hosted page and preview (H6, plus H19's `object-src 'none'`). `agent_base`
    is trusted for scripts, the beacon's own fetches and the tracking form's post, alongside this page's own
    origin ('self'), Google's tag hosts and the known booking providers. `frame_ancestors` is `'none'` for a
    page served on the pages domain; Task 7 passes the web base's origin for a preview framed on the
    homepage card (H16).

    H42 (final review): a preview's policy (`preview=True`) is the same with two changes, because a preview is
    anyone's HTML opened by whoever has the link, a signed-in owner included: it runs sandboxed -- `sandbox
    allow-scripts allow-forms`, never `allow-same-origin` -- so the browser gives it an opaque origin: its scripts
    run and its forms post, but nothing it sends carries a cookie, and no answer it did not get through CORS is
    readable by it; and it can fetch only the agent host's public, unauthenticated `/t/` endpoints (H45, final
    round 2: `connect-src <agent base>/t/`, one path-scoped source -- the tracking script's visit beacon and form
    send, so a preview's form still sends a lead), never `/api`, `/connect` or `/mcp`, and no other host."""
    scripts = " ".join(f"https://{h}" for h in GOOGLE_SCRIPT_HOSTS)
    connects = " ".join(f"https://{h}" for h in _GOOGLE_CONNECT_HOSTS)
    connect_src = f"{agent_base}/t/" if preview else f"'self' {agent_base} {connects}"
    frames = " ".join(f"https://{h}" for h in BOOKING_FRAME_HOSTS)
    # A preview's plain forms may post only to the public form endpoint (the H45 shape for connect-src): its
    # own opaque origin has no cookies, and 'self' there would name the agent origin itself.
    form_action = f"{agent_base}/t/form/" if preview else f"'self' {agent_base}"
    policy = (
        "default-src 'none'; "
        "style-src 'self' 'unsafe-inline' https:; "
        f"script-src 'self' 'unsafe-inline' {agent_base} {scripts}; "
        "img-src https: data:; "
        "font-src https: data:; "
        f"connect-src {connect_src}; "
        f"frame-src {frames}; "
        "object-src 'none'; "
        f"form-action {form_action}; "
        "base-uri 'self'; "
        f"frame-ancestors {frame_ancestors}"
    )
    if preview:
        policy += "; sandbox allow-scripts allow-forms"
    return policy


def headers(agent_base: str, *, preview: bool, frame_ancestors: str = "'none'") -> dict[str, str]:
    """The response headers for a served page or preview (H3, H6): the CSP (a preview's sandboxed one, H42), a
    cache policy (previews are never cached; a live page for a minute), and, for a preview only, X-Robots-Tag
    (H3's belt-and-braces alongside the `noindex` meta `inject` adds to the body)."""
    h = {
        "Content-Security-Policy": csp(agent_base, frame_ancestors=frame_ancestors, preview=preview),
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store" if preview else "public, max-age=60",
    }
    if preview:
        h["X-Robots-Tag"] = "noindex"
    return h


# ---------------------------------------------------------------- versions and previews (H7, H8, H16)

# Task 7: the homepage's own request context, read by tools_hosting.py's create_page/revise_page so every
# preview page_builder.save mints while the call runs is framed for the web base's origin (H16) -- the same
# contextvar shape auth.with_headers already uses for the homepage's tool calls in process. An MCP call never
# enters with_embed_origin, so current_embed_origin() reads None for it, exactly as save_preview's own default.
_EMBED_ORIGIN: ContextVar[str | None] = ContextVar("hosted_embed_origin", default=None)


@contextlib.contextmanager
def with_embed_origin(origin: str | None):
    """Wraps a create_page/revise_page call made from the homepage (home_api.py) so its preview is minted
    with embed_origin=origin instead of the usual None."""
    token = _EMBED_ORIGIN.set(origin)
    try:
        yield
    finally:
        _EMBED_ORIGIN.reset(token)


def current_embed_origin() -> str | None:
    return _EMBED_ORIGIN.get()


def new_version(
    conn: psycopg.Connection, page_id: str, html: str, title: str | None, source: str, *, kind: str = "html"
) -> int:
    """Append page_version n+1 (H7) and return n+1; `page.html`/`page.version` are untouched here --
    publish_page (Task 2) is what sets them, from a chosen version, once the sanitiser and the check agree.
    An advisory lock on `page_id` (H19, Review Focus 6) serialises concurrent callers for the SAME page:
    without it, two callers can both read the same MAX(version) and race to INSERT the same (page_id,
    version) pair, one losing to a UniqueViolation instead of getting its own next version. `hashtext`
    folds the id to the 32-bit int the single-key form of `pg_advisory_xact_lock` takes (implicitly widened
    to bigint by Postgres); the lock is scoped to this transaction and always released when it ends. `kind` (A3p-3
    B10): "bundle" for a site bundle's version, whose files the caller adds (`bundles.add_version_files`)."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (page_id,))
        n = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM page_version WHERE page_id = %s", (page_id,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO page_version (page_id, version, html, title, source, kind) VALUES (%s, %s, %s, %s, %s, %s)",
            (page_id, n, html, title, source, kind),
        )
    return n


def versions(conn: psycopg.Connection, page_id: str) -> list[dict]:
    """Every version of `page_id`, newest first; `live` marks the one page.version currently points at (H7:
    page.html/page.version are the live copy -- a rollback is a new version whose html equals an old one, so
    the history stays linear and exactly one row is ever live)."""
    live = conn.execute("SELECT version FROM page WHERE id = %s", (page_id,)).fetchone()
    live_n = live["version"] if live else None
    rows = conn.execute(
        "SELECT version, title, source, kind, created_at FROM page_version WHERE page_id = %s ORDER BY version DESC",
        (page_id,),
    ).fetchall()
    return [{**dict(r), "live": r["version"] == live_n} for r in rows]


def save_preview(
    conn: psycopg.Connection,
    page_id: str,
    html: str,
    title: str | None,
    *,
    photos: bool = False,
    embed_origin: str | None = None,
    sections: dict | None = None,
    kind: str = "html",
    ttl: timedelta = PREVIEW_TTL,
) -> str:
    """A preview token good for PREVIEW_TTL (H8's preview_page). `embed_origin` is H16: Task 7 sets it to the
    homepage's own origin so `preview()`'s caller can frame that preview there (a page served on the pages
    domain never sets it -- H6's `frame_ancestors 'none'`). `sections` (Task 5): what Admiry built an
    Admiry-written page from (agent/page_builder.py) -- None for an assistant's own HTML; publishing the
    preview copies it onto the page row.

    `photos` stays unused (Task 6): `page_preview` has no column for it, and none turned out to be needed --
    `preview_get` rewrites a photo's `src` for every preview response regardless (`_localize_photos`, a cheap
    no-op when there is nothing to rewrite), so there was never a reason to know in advance which previews
    have photos. Kept in the signature so a caller that does pass it (none do today) is not a breaking
    change either way.

    `kind` (A3p-3 B7): "bundle" for a site bundle's preview (agent/bundles.py: `html` is its home page as uploaded, its
    files are `page_preview_file` rows)."""
    token = db.new_id("pv")
    conn.execute(
        "INSERT INTO page_preview (token, page_id, html, title, sections, expires_at, embed_origin, kind) "
        "VALUES (%s, %s, %s, %s, %s, now() + %s, %s, %s)",
        (token, page_id, html, title, Jsonb(sections) if sections is not None else None, ttl, embed_origin, kind),
    )
    return token


def preview(conn: psycopg.Connection, token: str) -> dict | None:
    """The live preview for `token`, or None once it is expired or was never minted (never distinguished --
    an unguessable token that is simply gone is not information a caller should get to probe for). `kind` is "html"
    or "bundle" (A3p-3)."""
    row = conn.execute(
        "SELECT page_id, html, title, sections, expires_at, embed_origin, kind FROM page_preview "
        "WHERE token = %s AND expires_at > now()",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def prune_previews(conn: psycopg.Connection, *, i: int = 0, n: int = 1) -> int:
    """Delete every expired preview (of shard `i` of `n`, jobs.py's own split: the hourly page-check prunes them);
    returns how many."""
    return conn.execute(
        "DELETE FROM page_preview WHERE expires_at <= now() AND mod(abs(hashtext(token)::bigint), %(n)s) = %(i)s",
        {"i": i, "n": n},
    ).rowcount


# ---------------------------------------------------------------- the site's source (A3p-3 P19, P23)


def source(conn: psycopg.Connection, biz: str) -> dict | None:
    """What Admiry hosts for the business, to take away or to change (A3p-3 spec §7 `view = "source"`, P19, P23):
    {"kind": "html", "html", "version", "title", "hostname", "live", "page_id"} -- the live version (`page.html`,
    the tracking snippet in it as it is served), or, while a site taken down is kept, the version that was live
    (`live` false); for a site bundle (A3p-3 B11), {"kind": "bundle", "files": [{path, size}], ...} with the same
    other keys (its zip is a link the caller mints). None when Admiry hosts nothing for it: no page, the owner's own
    page, or a site whose files were deleted 90 days after it came down."""
    from . import bundles  # it imports this module: read when a source is asked for

    page = landing.current(conn, biz)
    if page is None or page.get("source") not in ("hosted", "admiry") or not page.get("hostname"):
        return None
    row = conn.execute(
        "SELECT p.html, p.version, v.title, v.kind FROM page p LEFT JOIN page_version v ON v.page_id = p.id "
        "AND v.version = p.version WHERE p.id = %s",
        (page["id"],),
    ).fetchone()
    if row is None or not row["html"]:
        return None
    found = {
        "kind": "html",
        "html": row["html"],
        "version": row["version"],
        "title": row["title"],
        "hostname": page["hostname"],
        "live": page["status"] == "published",
        "page_id": page["id"],
    }
    if row["kind"] == "bundle":
        files = bundles.version_files(conn, page["id"], row["version"])
        if not files:
            return None
        del found["html"]
        found.update(kind="bundle", files=bundles.sizes(conn, files))
    return found


# ---------------------------------------------------------------- serving by Host (H3, H4, H16, H17)

# /p/{token}, /p/{token}/photos/{id}, /pages/ask -- stay on the agent host (H17): the previews answer there only
# (H42, `on_agent_host`); /pages/ask is Caddy's own call, over the compose network (`agent:8097`)
router = APIRouter()
ROBOTS_TXT = "User-agent: *\nAllow: /\n"
_PHOTO_PATH = re.compile(r"^/photos/([^/]+)$")


def _clean_host(host: str) -> str:
    """A raw `Host` header, or `/pages/ask`'s `domain` query parameter, the way both `is_pages_host` and
    `serve`'s hostname lookup need it: lowercased, any trailing ":<port>" stripped, then any trailing "."
    (the absolute-FQDN form a client or Caddy may send) stripped too. Never raises -- None or "" both come
    back "", which then matches nothing (an unknown hostname is 404, never an error, H3)."""
    return re.sub(r":\d+$", "", (host or "").strip().lower()).rstrip(".")


def is_pages_host(host: str, domain: str | None) -> bool:
    """Whether `host` (a request's raw `Host` header) is a hostname the pages domain would ever answer
    (H1, H3, H4): a proper subdomain of `domain` with EXACTLY ONE label under it, so the bare pages domain
    itself (nothing is hosted at its apex), a two-labels-deep name like "a.b.business.test" (never a slug
    `hostname_for` mints), and every other host -- the agent's own, in particular -- all read False. Always
    False when `domain` is unset, which is what makes `app.py`'s `_TenantHost` middleware inert without one
    (H1)."""
    if not domain:
        return False
    h = _clean_host(host)
    if not h.endswith("." + domain):
        return False
    label = h[: -(len(domain) + 1)]
    return bool(label) and "." not in label


def on_agent_host(host: str, settings: config.Settings) -> bool:
    """H42 (final review): whether `host` (a request's raw `Host` header) is the agent host itself --
    AGENT_PUBLIC_BASE_URL's, the one host a preview and its photos answer on. The browser host
    (AGENT_WEB_BASE_URL's, where the owner's session cookie lives) and every other name read False, so a preview's
    HTML never runs on an origin holding a cookie of Admiry's even before its sandbox (`csp(preview=True)`) is
    counted. Compared the way `is_pages_host` compares (`_clean_host`: any case, any port, a trailing dot)."""
    return _clean_host(host) == _clean_host(urlsplit(settings.public_base_url).netloc)


def _page_for_host(conn: psycopg.Connection, host: str) -> dict | None:
    """H25 (round 2): `source` must be 'hosted' or 'admiry' -- a row's `hostname` column outlives a switch back
    to the owner's own address (set_owner_url leaves it exactly as it was, so a later hosted republish keeps
    the slug), so without this filter an owner-source row would still answer here, on Admiry's own host, for a
    page the owner most recently pointed away from A3p-2."""
    row = conn.execute(
        "SELECT p.id, p.business_id, p.hostname, p.html, p.status, p.version, COALESCE(v.kind, 'html') AS kind "
        "FROM page p LEFT JOIN page_version v ON v.page_id = p.id AND v.version = p.version "
        "WHERE p.hostname = %s AND p.source IN ('hosted', 'admiry')",
        (host,),
    ).fetchone()
    return dict(row) if row else None


def _not_found() -> Response:
    """ "Unknown hostname" and "any other path" (H3): a plain page, no business data -- there is no page row
    to name anything from, so this never renders a template."""
    return PlainTextResponse("Not found.", status_code=404)


def _photo_response(conn: psycopg.Connection, page_id: str, photo_id: str) -> Response:
    """One photo of `page_id` (H3's `/photos/{id}`, H16's `/p/{token}/photos/{id}`): the owner's bytes as
    uploaded (Task 6), a day of caching, or 404 -- indistinguishable from a wrong id, same as `preview`."""
    row = conn.execute(
        "SELECT bytes, content_type FROM page_photo WHERE id = %s AND page_id = %s", (photo_id, page_id)
    ).fetchone()
    if row is None:
        return _not_found()
    return Response(row["bytes"], media_type=row["content_type"], headers={"Cache-Control": "public, max-age=86400"})


def _gone(conn: psycopg.Connection, page: dict, agent_base: str) -> Response:
    """An `unpublished` page's own host, whatever the path (H3): the whole site reads 410, not just "/" --
    one sentence, the business's name (`leads.business_name`'s vouched name, else "the business") and
    nothing else about it. Never cached (unlike a live page's minute): a page that comes back later
    (`publish_page`) must not have visitors, or a CDN, still holding onto a stale "gone" response."""
    name = leads.business_name(conn, page["business_id"])
    out = _env.get_template("hosted_gone.html").render(name=name)
    h = {**headers(agent_base, preview=False), "Cache-Control": "no-store"}
    return HTMLResponse(out, status_code=410, headers=h)


def _privacy_context(conn: psycopg.Connection, page: dict) -> tuple[dict, dict]:
    """`privacy_html`'s two arguments (and `terms_html`'s, which takes the same: M1 Task 4), gathered the way
    `landing.kit` gathers a business's own facts: the vouched name, the knowledge's own phone, and the owner's
    sign-in address as the privacy notice's contact (there is no other e-mail on file for a business; the terms page
    never shows it: final review I5) -- plus the vertical recipe's `form_fields`, every field this page's own form can
    ever send, whatever an assistant wrote around it. `knowledge.get("category")` is the same lookup
    `tools_consult.py` and `plans.py` use directly, rather than `landing.kit`'s plan-derived tree -- a hosted page's
    privacy notice and terms must render for any business with a live page, consulted or not."""
    biz_id = page["business_id"]
    latest = business.latest_knowledge(conn, biz_id)
    knowledge = latest[1] if latest else {}
    recipe = landing.load_recipe(knowledge.get("category") or "fallback")
    row = business.row(conn, biz_id) or {}
    biz = {
        "name": leads.business_name(conn, biz_id),
        "phone": knowledge.get("phone") or None,
        "email": row.get("owner_email"),
    }
    return biz, {"form_fields": recipe["form_fields"]}


def privacy_html(business: dict, page: dict) -> str:
    """Admiry's hosted privacy notice for one business (H3): who collects what -- the vertical recipe's own
    `form_fields`, since that is every field this page's form can ever send -- Admiry named as the
    processor with a link to its own policy, the business named as the contact, and the one sentence the
    ads' 14-rule check needs to find true on the page it links to: this page sets no cookies of Admiry's; a
    business's own Google Ads cookies, if it runs any, are the business's, not Admiry's."""
    return _env.get_template("hosted_privacy.html").render(business=business, page=page)


def terms_html(business: dict, page: dict) -> str:
    """Admiry's hosted terms of use for one business's page (M1 Task 4: NewsBreak asks a landing page for a link to
    terms, the landing check's `terms` rule, and the hosted footer carries it): the same arguments as `privacy_html`,
    from the same `_privacy_context`. Plain and minimal -- who runs the page, what it is for (it describes the
    business's services and invites the visitor to call and/or send the form, whichever the page has: a business
    with no phone has no call button, so no call is invited), no warranty beyond what the business itself offers,
    the business as the contact -- its phone, else the form on the page, never an e-mail address: the owner's sign-in
    address is never published here (final review I5) -- and Admiry named as the host and no party to what passes
    between the visitor and the business. The text needs the founder's review before the first client's page links
    to it."""
    return _env.get_template("hosted_terms.html").render(business=business, page=page)


# the two notices a tenant host serves beside its page: path -> what renders it from `_privacy_context`
_NOTICES = {"/privacy": privacy_html, "/terms": terms_html}


def serve(
    conn: psycopg.Connection,
    settings: config.Settings,
    host: str,
    path: str,
    method: str,
    *,
    if_none_match: str | None = None,
) -> Response:
    """Answers one request whose `Host` is a pages-domain hostname (H3), entirely on its own: `app.py`'s
    `_TenantHost` middleware calls this instead of ever reaching FastAPI's routers, so nothing else on the
    agent (`/mcp`, `/connect`, `/t/*`, the homepage, ...) exists on a tenant host. `/` the live page,
    `/privacy` Admiry's hosted notice for it and `/terms` its terms of use (M1 Task 4), `/photos/{id}` an uploaded
    photo, `/robots.txt`; an `unpublished` page's host answers 410 for every path (H3: the whole site is down, not
    just "/"); an unknown hostname, any other path, or any method but GET all answer a plain 404 with no business
    data. A site bundle (A3p-3 B9) answers every path from its files (`bundles.respond`, `if_none_match` its ETag
    check) beside Admiry's two notices; a business's preview host (B8) its newest bundle preview.
    HEAD never reaches here as HEAD -- `_HeadAsGet` (app.py, outermost) has already turned it into a GET by
    the time this runs (H17), so only its own send wrapper strips the body back off. Never sets a cookie."""
    from . import bundles  # it imports this module: read when a request comes

    if method != "GET":
        return _not_found()
    clean = _clean_host(host)
    agent_base = settings.public_base_url  # H6's CSP host and the snippet's own origin -- never the web base
    if bundles.is_preview_host(clean):
        return _serve_preview_host(conn, agent_base, clean, path, if_none_match)
    page = _page_for_host(conn, clean)
    if page is None:
        return _not_found()
    if page["status"] != "published":
        return _gone(conn, page, agent_base)
    if path in _NOTICES:
        biz, page_ctx = _privacy_context(conn, page)
        return HTMLResponse(_NOTICES[path](biz, page_ctx), headers=headers(agent_base, preview=False))
    if page["kind"] == "bundle":
        return bundles.respond(
            conn,
            ("version", page["id"], page["version"]),
            path,
            headers(agent_base, preview=False),
            if_none_match=if_none_match,
            robots=ROBOTS_TXT,
        )
    if path == "/":
        return HTMLResponse(page["html"] or "", headers=headers(agent_base, preview=False))
    if path == "/robots.txt":
        return PlainTextResponse(ROBOTS_TXT, headers=headers(agent_base, preview=False))
    photo = _PHOTO_PATH.match(path)
    if photo:
        return _photo_response(conn, page["id"], photo.group(1))
    return _not_found()


PREVIEW_ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


def _bundle_preview(conn: psycopg.Connection, host: str) -> dict | None:
    """The newest live bundle preview of the business whose preview host is `host` (A3p-3 B8), with its page's id and
    business, or None."""
    row = conn.execute(
        "SELECT v.token, p.id, p.business_id FROM page p JOIN page_preview v ON v.page_id = p.id "
        "AND v.kind = 'bundle' AND v.expires_at > now() WHERE p.preview_host = %s ORDER BY v.created_at DESC LIMIT 1",
        (host,),
    ).fetchone()
    return dict(row) if row else None


def _serve_preview_host(
    conn: psycopg.Connection, agent_base: str, host: str, path: str, if_none_match: str | None
) -> Response:
    """A business's preview host (A3p-3 P35, B8): its newest live bundle preview, by path, with the live site's
    headers plus noindex and no cache -- its own origin, so a bundle's root-relative paths resolve, and no cookie of
    Admiry's anywhere on it (the H42 lesson). Admiry's notices answer there too, for the preview's footer links;
    robots.txt disallows everything. 404 when no bundle preview is live."""
    from . import bundles  # it imports this module: read when a request comes

    h = {**headers(agent_base, preview=False), "X-Robots-Tag": "noindex", "Cache-Control": "no-store"}
    found = _bundle_preview(conn, host)
    if found is None:
        return PlainTextResponse("Not found.", status_code=404, headers=h)
    if path == "/robots.txt":
        return PlainTextResponse(PREVIEW_ROBOTS_TXT, headers=h)
    if path in _NOTICES:
        biz, page_ctx = _privacy_context(conn, {"id": found["id"], "business_id": found["business_id"]})
        return HTMLResponse(_NOTICES[path](biz, page_ctx), headers=h)
    return bundles.respond(conn, ("preview", found["token"]), path, h, if_none_match=if_none_match, preview=True)


def _preview_not_found(agent_base: str) -> Response:
    """Expired or unknown (H3): no business name anywhere in it, and noindex twice over -- the header
    belt, the meta-tag braces (preview_404.html has no `<head>` for `inject` to find, so it is written in
    directly rather than injected)."""
    out = _env.get_template("preview_404.html").render()
    return HTMLResponse(out, status_code=404, headers=headers(agent_base, preview=True))


_PHOTO_SRC = re.compile(r'src="(/photos/[^"]*)"')


def _localize_photos(html_text: str, token: str) -> str:
    """A preview's own copy of the page's photo `<img>` tags (A3p-2 Task 6): `page_builder.render` always
    bakes a root-absolute `src="/photos/{id}"` -- correct as-is once this exact HTML is later published
    verbatim to the tenant host at "/" (apply_publish_page copies a preview's bytes onto the live page
    unchanged, H7), which is why it is never baked any other way. But the SAME bytes are also served here, on
    the agent host, under this preview's own token -- where a root-absolute path cannot reach
    `/p/{token}/photos/{id}` no matter what it reads (RFC 3986 relative resolution cannot do it either: a
    preview's own URL, `/p/{token}` with no trailing slash, is one path segment short of the directory
    `/p/{token}/photos/` a relative reference would need). So this response-only rewrite is what makes the
    preview's photos resolve at all -- the stored preview row, and the page once published, are untouched."""
    return _PHOTO_SRC.sub(lambda m: f'src="/p/{token}{m.group(1)}"', html_text)


@router.get("/p/{token}")
def preview_get(token: str, request: Request) -> Response:
    """A preview (H3, H8, H16), on the agent host only and sandboxed there (H42): any other `Host` -- the
    browser host above all, where the owner's session cookie lives -- answers exactly like a token that does not
    exist."""
    settings = config.load()
    agent_base = settings.public_base_url  # H6's CSP host and the snippet's own origin -- never the web base
    if not on_agent_host(request.headers.get("host", ""), settings):
        return _preview_not_found(agent_base)
    with db.connect(settings.database_url) as conn:
        prev = preview(conn, token)
        if prev is None or prev["kind"] == "bundle":  # A3p-3 B8: a bundle's pages need a host of their own
            return _preview_not_found(agent_base)
        out = inject(prev["html"], snippet="", noindex=True)
        out = _localize_photos(out, token)
        frame_ancestors = prev["embed_origin"] or "'none'"
        return HTMLResponse(out, headers=headers(agent_base, preview=True, frame_ancestors=frame_ancestors))


@router.get("/p/{token}/photos/{id}")
def preview_photo(token: str, id: str, request: Request) -> Response:
    """A preview's photo (H16), on the agent host only like the preview itself (H42): elsewhere a plain 404."""
    settings = config.load()
    if not on_agent_host(request.headers.get("host", ""), settings):
        return _not_found()
    with db.connect(settings.database_url) as conn:
        prev = preview(conn, token)
        if prev is None or prev["kind"] == "bundle":
            return _not_found()
        return _photo_response(conn, prev["page_id"], id)


@router.get("/pages/ask")
def pages_ask(domain: str = "") -> Response:
    """Caddy's `on_demand_tls` check (H4): 200 only for a hostname a `published` page actually answers to,
    under this pages domain; 404 otherwise, including hosting being off entirely. No auth -- Caddy calls
    this inside the compose network, once per hostname it has not already got a certificate for -- and the
    domain asked about is never logged at INFO: it names nobody who has vouched for anything yet, so it is
    exactly the kind of thing that must not build up in a log by the mere fact of being asked."""
    settings = config.load()
    if not settings.pages_domain:
        return JSONResponse({"ok": False}, status_code=404)
    host = _clean_host(domain)
    if not is_pages_host(host, settings.pages_domain):
        return JSONResponse({"ok": False}, status_code=404)
    from . import bundles  # it imports this module: read when Caddy asks

    with db.connect(settings.database_url) as conn:
        if bundles.is_preview_host(host):  # A3p-3 B8: a business's preview host, while a bundle preview is live
            row = _bundle_preview(conn, host)
        else:
            row = conn.execute(
                "SELECT 1 FROM page WHERE hostname = %s AND status = 'published' AND source IN ('hosted', 'admiry')",
                (host,),
            ).fetchone()
    return JSONResponse({"ok": bool(row)}, status_code=200 if row else 404)
