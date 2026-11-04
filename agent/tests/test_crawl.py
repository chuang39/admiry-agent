import zlib

import httpx
import pytest

from agent import crawl


def public(host):
    return ["93.184.216.34"]


def site(pages: dict[str, str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)

    return httpx.MockTransport(handler)


def test_fetch_site_reads_same_host_pages_as_text_and_skips_scripts():
    t = site(
        {
            "/": "<html><head><title>Acme Plumbing</title><script>var x=1</script></head><body><h1>Emergency plumbing</h1>"
            "<a href='/water-heaters'>Water heaters</a><a href='https://other.example/x'>Other</a>"
            "<a href='/brochure.pdf'>PDF</a></body></html>",
            "/water-heaters": "<html><title>Water heaters</title><body>Replacement from $1,400</body></html>",
        }
    )
    pages = crawl.fetch_site("https://acme.example/", transport=t, resolver=public)
    assert [(p.title, p.text) for p in pages] == [
        ("Acme Plumbing", "Emergency plumbing Water heaters Other PDF"),
        ("Water heaters", "Replacement from $1,400"),
    ]


def test_fetch_site_caps_pages_and_ignores_non_html():
    links = "".join(f"<a href='/p{i}'>p{i}</a>" for i in range(40))
    pages = {"/": f"<html><body>home {links}</body></html>"}
    pages.update({f"/p{i}": f"<html><body>page {i}</body></html>" for i in range(40)})
    html = site(pages)

    def handler(request):
        if request.url.path == "/p3":
            return httpx.Response(200, headers={"content-type": "application/json"}, text="{}")
        return html.handler(request)

    got = crawl.fetch_site("https://acme.example/", transport=httpx.MockTransport(handler), resolver=public)
    assert len(got) == crawl.MAX_PAGES and all(p.text != "page 3" for p in got)


def test_fetch_site_stops_at_the_time_budget():
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])  # deadline computed, first page fetched, then out of time
    got = crawl.fetch_site(
        "https://acme.example/",
        resolver=public,
        clock=lambda: next(ticks),
        transport=site({"/": "<html><body>home <a href='/a'>a</a></body></html>", "/a": "<html><body>a</body></html>"}),
    )
    assert [p.text for p in got] == ["home a"]


def test_a_trickling_page_is_cut_at_the_deadline_between_chunks():
    sent = []

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=iter([b"<html><body>a<a href='/next'></a>", b"b", b"c</body></html>"]),
        )

    ticks = iter([0.0, 0.0, 100.0])  # deadline computed, first page started, then the budget is gone after chunk 1
    got = crawl.fetch_site(
        "https://acme.example/",
        transport=httpx.MockTransport(handler),
        resolver=public,
        clock=lambda: next(ticks, 100.0),
    )
    assert [p.text for p in got] == ["a"]  # the prefix read before the deadline
    assert sent == ["/"]  # and the crawl ends: /next is never fetched


def test_redirects_to_other_hosts_are_never_fetched():
    sent = []

    def handler(request):
        sent.append(str(request.url))
        if request.url.host == "acme.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>SECRET</body></html>")

    pages = crawl.fetch_site("https://acme.example/", transport=httpx.MockTransport(handler), resolver=public)
    assert pages == [] and sent == ["https://acme.example/"]  # the metadata address was never contacted


def test_same_host_redirects_are_queued_and_followed():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/home"})
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><title>Home</title><body>Welcome</body></html>"
        )

    pages = crawl.fetch_site("https://acme.example/", transport=httpx.MockTransport(handler), resolver=public)
    assert [(p.url, p.title, p.text) for p in pages] == [("https://acme.example/home", "Home", "Welcome")]


def _two_forms(serves: str, sent: list) -> httpx.MockTransport:
    """A site whose host `serves` (the bare form or the `www.` one) answers its pages and whose other form redirects
    every path there; each request is recorded in `sent`. The home page links to the team page on its own form and to
    the about page on the other one."""
    other = serves.removeprefix("www.") if serves.startswith("www.") else f"www.{serves}"
    pages = {
        "/": f"<html><title>Home</title><body>Welcome <a href='/team'>Team</a> <a href='https://{other}/about'>A</a>"
        "</body></html>",
        "/team": "<html><title>Team</title><body>Our team</body></html>",
        "/about": "<html><title>About</title><body>About us</body></html>",
    }

    def handler(request):
        sent.append(str(request.url))
        if request.url.host != serves:
            return httpx.Response(301, headers={"location": f"https://{serves}{request.url.path}"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=pages[request.url.path])

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("start", "serves"),
    [("https://acme.example/", "www.acme.example"), ("https://www.acme.example/", "acme.example")],
    ids=["the bare form sends to www", "www sends to the bare form"],
)
def test_a_sites_bare_and_www_forms_are_one_site(start, serves):
    """A3p-5 ruling R2-h: an owner types "acme.example" for a site that lives at "www.acme.example" (or the other way
    round): the redirect between the two forms is followed -- the new form vetted as public first, as the start's is --
    and a link on either form is the site's."""
    sent, asked = [], []

    def resolver(host):  # records each host vetted
        asked.append(host)
        return public(host)

    pages = crawl.fetch_site(start, transport=_two_forms(serves, sent), resolver=resolver)
    assert [(p.url, p.title) for p in pages] == [
        (f"https://{serves}/", "Home"),
        (f"https://{serves}/team", "Team"),
        (f"https://{serves}/about", "About"),
    ]
    assert set(asked) == {"acme.example", "www.acme.example"}  # each form vetted once
    assert sent[0] == start


@pytest.mark.parametrize(
    "location", ["https://shop.acme.example/", "https://www.other.example/", "https://acme.example.evil.example/"]
)
def test_a_redirect_to_any_other_host_is_still_dropped(location):
    """R2-h joins the two forms of one host and nothing else: another subdomain, another site's www or a look-alike is
    never fetched."""
    sent = []

    def handler(request):
        sent.append(str(request.url))
        if request.url.host == "acme.example":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>ELSEWHERE</body></html>")

    pages = crawl.fetch_site("https://acme.example/", transport=httpx.MockTransport(handler), resolver=public)
    assert pages == [] and sent == ["https://acme.example/"]


def test_the_www_form_is_never_fetched_when_it_resolves_to_a_private_address():
    """R2-h: each form is vetted as the start's is (public_host): a `www.` form that resolves privately is dropped
    unfetched."""
    sent = []

    def resolver(host):
        return ["10.0.0.7"] if host.startswith("www.") else public(host)

    pages = crawl.fetch_site("https://acme.example/", transport=_two_forms("www.acme.example", sent), resolver=resolver)
    assert pages == [] and sent == ["https://acme.example/"]


def test_bodies_are_read_at_most_max_bytes():
    body = "<html><body>" + " " * 150_000 + "MARKER" + " " * (crawl.MAX_BYTES + 100) + "TAIL</body></html>"
    pages = crawl.fetch_site("https://acme.example/", transport=site({"/": body}), resolver=public)
    assert [p.text for p in pages] == ["MARKER"]  # TAIL sits past MAX_BYTES and is never read


def _read(encoding: str, body: bytes, cap: int) -> bytes:
    """read_capped over a streamed (not pre-read) answer carrying `body` as one network read."""
    headers = {"content-encoding": encoding} if encoding else {}
    t = httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, content=iter([body])))
    with httpx.Client(transport=t) as http, http.stream("GET", "https://acme.example/") as r:
        return crawl.read_capped(r, cap)


def test_read_capped_inflates_at_most_the_cap_and_nothing_it_never_asked_for():
    """A3p-1 C2: a gzip or deflate body is inflated here, never past `cap` (httpx's own decoders inflate a whole
    network read at once); an encoding the crawler never asks for (zstd: a 6 KB body can be 200 MB) reads as
    nothing, and the crawler asks for gzip and deflate only."""
    page = b"<html><body>" + b"x" * 500_000 + b"</body></html>"
    gz = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    assert _read("gzip", gz.compress(page) + gz.flush(), 1000) == page[:1000]
    assert _read("deflate", zlib.compress(page), 1000) == page[:1000]
    assert _read("", page, 1000) == page[:1000]
    assert _read("gzip", b"not gzip at all", 1000) == b""  # a broken body keeps what inflated before it: nothing
    assert _read("zstd", b"\x28\xb5\x2f\xfd" + b"\0" * 64, 1000) == b""
    asked = []
    t = httpx.MockTransport(lambda r: (asked.append(r.headers["accept-encoding"]), httpx.Response(404))[1])
    assert crawl.fetch_site("https://acme.example/", transport=t, resolver=public) == []
    assert asked == ["gzip, deflate"]


def test_read_capped_stops_at_the_end_of_a_gzip_member():
    """A3p-1 C2, the re-review's case: bytes after a finished gzip member are junk zlib would copy into
    `unused_data` on every later chunk (quadratic, unbounded until the deadline) — the reader stops at eof and
    pulls no further chunk from the network."""
    page = b"<html><body>page</body></html>"
    gz = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    member = gz.compress(page) + gz.flush()
    pulled = []

    def chunks():
        for i, chunk in enumerate([member] + [b"\0" * 65_536] * 64):
            pulled.append(i)
            yield chunk

    t = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, content=chunks()))
    with httpx.Client(transport=t) as http, http.stream("GET", "https://acme.example/") as r:
        assert crawl.read_capped(r, 300_000) == page
    assert pulled == [0]  # the member alone; not one trailing chunk


def test_decode_falls_back_to_utf8_for_a_charset_that_is_not_a_text_codec():
    """httpx accepts any codec name as `r.encoding`; rot13/base64/hex/zlib raise LookupError on bytes, idna a
    UnicodeError — a page served with one of those must still read, not crash the check."""
    raw = "<h1>Caf\u00e9</h1>".encode()
    assert crawl.decode(raw, "utf-8") == "<h1>Café</h1>"
    assert crawl.decode(raw, None) == "<h1>Café</h1>"
    assert crawl.decode(raw, "latin-1") == raw.decode("latin-1")
    for name in ("rot13", "base64", "hex", "zlib", "idna", "no-such-codec"):
        assert crawl.decode(raw, name) == "<h1>Café</h1>"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://acme.example",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest",
        "http://acme.internal/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
def test_public_host_refuses_unsafe_urls(url):
    with pytest.raises(crawl.CrawlError):
        crawl.public_host(url, resolver=public)


@pytest.mark.parametrize(
    "url", ["http://[", "http://[object Object]/", "http://acme.example:abc/", "https://acme.example/a\x00b"]
)
def test_malformed_start_urls_are_crawl_errors(url):
    with pytest.raises(crawl.CrawlError, match="valid http\\(s\\) address"):
        crawl.fetch_site(url, resolver=public, transport=httpx.MockTransport(lambda r: pytest.fail("never fetched")))


def test_names_the_resolver_cannot_encode_are_crawl_errors():
    long_label = "http://" + "a" * 64 + ".example/"  # IDNA refuses a 64-character label before any lookup
    with pytest.raises(crawl.CrawlError):
        crawl.public_host(long_label)  # the real resolver
    with pytest.raises(crawl.CrawlError, match="valid http\\(s\\) address"):
        crawl.public_host("https://acme.example/", resolver=lambda host: ["not an address"])


def test_unparsable_links_and_redirects_are_skipped():
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path == "/":
            links = "".join(
                f"<a href='{h}'>x</a>"
                for h in (
                    "http://[object Object]",
                    "//[::1",
                    "http://acme.example:abc/x",
                    "/a\x00b",
                    "/b" + "c" * 70_000,
                    "/ok",
                )
            )
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text=f"<html><body>home{links}</body></html>"
            )
        if request.url.path == "/ok":
            return httpx.Response(302, headers={"location": "http://[oops"})
        return httpx.Response(404)

    pages = crawl.fetch_site("https://acme.example/", transport=httpx.MockTransport(handler), resolver=public)
    assert [p.url for p in pages] == ["https://acme.example/"] and sent == ["/", "/ok"]


def test_public_host_refuses_names_that_resolve_privately_mixed_or_not_at_all():
    for addresses in (["10.1.2.3"], ["93.184.216.34", "10.1.2.3"], []):
        with pytest.raises(crawl.CrawlError):
            crawl.public_host("https://acme.example/", resolver=lambda host, a=addresses: a)


def test_fetch_site_reads_at_most_max_pages():
    links = "".join(f"<a href='/p{i}'>p{i}</a>" for i in range(5))
    pages = {"/": f"<html><body>home {links}</body></html>"}
    pages.update({f"/p{i}": f"<html><body>page {i}</body></html>" for i in range(5)})
    got = crawl.fetch_site("https://acme.example/", transport=site(pages), resolver=public, max_pages=1)
    assert [p.text for p in got] == ["home p0 p1 p2 p3 p4"]


HEAVY_HEAD = "<html><head><title>Roof Repair</title><style>" + ("a{color:#000}" * 70_000) + "</style></head>"  # ~800 KB


def test_a_page_whose_body_starts_past_800_kb_is_still_read():
    """The founder's walk, 2026-10-12: a WordPress page with 764 KB of inline style before <body> read as 'no page'
    under the 200 KB cap. The body, its text, its phone and its links are read now."""
    body = "<body><h1>Roof repair in San Rafael</h1><a href='tel:+16282338140'>(628) 233-8140</a><a href='/contact'>Contact</a></body></html>"
    t = site({"/": HEAVY_HEAD + body, "/contact": "<html><body>Call us</body></html>"})
    pages = crawl.fetch_site("https://acme.example/", transport=t, resolver=public)
    assert [(p.title, p.text) for p in pages] == [
        ("Roof Repair", "Roof repair in San Rafael (628) 233-8140 Contact"),
        ("", "Call us"),
    ]


def test_a_page_past_the_cap_is_still_cut():
    """Review focus 1: a body that starts past MAX_BYTES is not read; the crawl stays bounded and raises nothing."""
    head = "<html><head><title>T</title><style>" + ("b{margin:0}" * 200_000) + "</style></head>"  # ~2.2 MB
    t = site({"/": head + "<body><p>late</p></body></html>"})
    pages = crawl.fetch_site("https://acme.example/", transport=t, resolver=public)
    assert pages == []  # no text before the cut: no page
