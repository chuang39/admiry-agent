"""agent/uploads.py: GET/POST /u/{token} (A3p-2 Task 6; spec 2026-09-28-admiry-pages-design.md §7, plan H13,
ledger H35/H37) -- the owner's own upload page, and the real data the photo-serving routes
(/photos/{id} on the tenant host, /p/{token}/photos/{id} on the preview host) gain through it. Every test
photo is a tiny real JPEG/PNG/WebP file built with Pillow right here, never read from disk.

`_ready` is this file's own setup: a business with a real plan (create_page needs the full "dental walk" kit,
test_tools_hosting.py's own `_hosted_biz`/`_created`) and a page Admiry wrote, so every test starts from a
valid, unexpired upload token exactly the way create_page hands one to an owner."""

from __future__ import annotations

import asyncio
import io
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from starlette.requests import Request

from agent import db, hosted, landing, uploads
from agent.tests.conftest import TEST_DB_URL, call_tool, data, tool_text
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import HOSTNAME, _created

# ---------------------------------------------------------------- tiny real photos, built with Pillow


def _photo(fmt: str, size=(40, 30), color="red", **save_kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt, **save_kw)
    return buf.getvalue()


def _big_jpeg(size=(4000, 3000)) -> bytes:
    """A large, non-uniform JPEG (a few bands plus a stripe pattern -- fast to build, still non-trivial to
    compress) the way a real phone photo is: at quality 82 it lands comfortably under 600 KB on its own,
    without needing the quality ladder's lower rungs at all."""
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    colors = [
        (200, 50, 50),
        (50, 200, 50),
        (50, 50, 200),
        (200, 200, 50),
        (50, 200, 200),
        (120, 60, 180),
        (250, 140, 20),
    ]
    band = size[1] // len(colors)
    for i, c in enumerate(colors):
        draw.rectangle([0, i * band, size[0], (i + 1) * band], fill=c)
    for x in range(0, size[0], 50):
        draw.line([(x, 0), (0, min(x, size[1]))], fill=(255, 255, 255), width=8)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _portrait_jpeg(size=(300, 400), orientation=6) -> bytes:
    """A frame stored exactly as a phone held in portrait writes one: the sensor's own pixels (here already
    (300, 400), just to keep the maths simple) plus an EXIF orientation tag saying how to display it --
    orientation 6 rotates 90° clockwise, ending at (400, 300)."""
    img = Image.new("RGB", size, "purple")
    ImageDraw.Draw(img).rectangle([10, 10, size[0] // 2, size[1] // 2], fill="yellow")  # an asymmetric mark
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _part(name: str, content: bytes, ctype: str) -> tuple[str, tuple[str, bytes, str]]:
    return "photos", (name, content, ctype)


def _multipart_body(parts: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """A hand-built multipart body + its Content-Type (boundary included), for the two H38 tests that need to
    control headers TestClient's own `files=` convenience always computes for us (a real Content-Length, in
    particular) -- (field name, filename, content, content-type) per part."""
    boundary = "----admiryTestBoundary"
    out = []
    for field, filename, content, ctype in parts:
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- setup


def _ready(client, conn, monkeypatch, fake) -> tuple[str, str, str, str]:  # noqa: F811 (fixture-shaped helper)
    """(key, business_id, page_id, upload_token) for a business with a real page Admiry wrote and a valid,
    unexpired upload link -- create_page's own (H35 filled)."""
    key, biz, env = _created(client, conn, monkeypatch, fake)
    page_id = landing.current(conn, biz)["id"]
    token = env["upload_url"].rsplit("/", 1)[-1]
    return key, biz, page_id, token


def _photo_count(conn, page_id: str) -> int:
    return conn.execute("SELECT count(*) AS n FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["n"]


def _latest_preview(conn, page_id: str) -> dict:
    return conn.execute(
        "SELECT token, html, sections FROM page_preview WHERE page_id = %s ORDER BY created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()


# ---------------------------------------------------------------- GET /u/{token}: the upload page itself


def test_upload_page_shows_the_business_name_and_the_count_already_stored(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, _page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.get(f"/u/{token}")

    assert r.status_code == 200
    assert "Bright Smile Dental" in r.text
    assert "0 of 5" in r.text
    assert '<input type="file" name="photos" accept="image/*" multiple' in r.text
    assert r.headers["x-robots-tag"] == "noindex"
    # H42: the thumbnails come from the agent host (conftest's AGENT_PUBLIC_BASE_URL), the only host a preview's
    # photos answer on -- this page itself is linked on the browser host
    assert r.headers["content-security-policy"] == (  # script-src 'self': upload.js, since the phone redesign
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' http://testserver "
        "data:; form-action 'self'"
    )
    assert "set-cookie" not in r.headers


def test_unknown_or_expired_token_is_a_plain_404(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, _page_id, token = _ready(client, conn, monkeypatch, fake)
    conn.execute("UPDATE upload_token SET expires_at = now() - interval '1 minute' WHERE token = %s", (token,))

    expired = client.get(f"/u/{token}")
    guessed = client.get("/u/up_0000000000000000")
    posted = client.post(f"/u/{token}", data={"action": "remove", "photo": "ph_x"})

    for r in (expired, guessed, posted):
        assert r.status_code == 404
        assert "Bright Smile Dental" not in r.text


# ---------------------------------------------------------------- POST /u/{token}: uploading photos


def test_uploading_a_jpeg_png_and_webp_stores_all_three_as_jpeg(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    files = [
        _part("a.jpg", _photo("JPEG", color="red"), "image/jpeg"),
        _part("b.png", _photo("PNG", color="blue"), "image/png"),
        _part("c.webp", _photo("WEBP", color="green"), "image/webp"),
    ]

    r = client.post(f"/u/{token}", files=files)

    assert r.status_code == 200 and "Photos added" in r.text
    rows = conn.execute("SELECT content_type FROM page_photo WHERE page_id = %s", (page_id,)).fetchall()
    assert [row["content_type"] for row in rows] == ["image/jpeg"] * 3
    assert _photo_count(conn, page_id) == 3
    assert "3 of 5" in r.text


def test_a_large_jpeg_is_resized_to_1600_on_the_long_side_and_shrunk_under_600kb(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.post(f"/u/{token}", files=[_part("big.jpg", _big_jpeg(), "image/jpeg")])

    assert r.status_code == 200, r.text
    row = conn.execute("SELECT bytes, width, height FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()
    assert (row["width"], row["height"]) == (1600, 1200)
    assert len(row["bytes"]) <= 600 * 1024
    stored = Image.open(io.BytesIO(row["bytes"]))
    assert stored.format == "JPEG" and stored.size == (1600, 1200)


def test_a_portrait_photo_comes_out_upright_with_exif_gone(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.post(f"/u/{token}", files=[_part("p.jpg", _portrait_jpeg(), "image/jpeg")])

    assert r.status_code == 200, r.text
    row = conn.execute("SELECT bytes, width, height FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()
    # H37: orientation 6 on a (300, 400) source rotates to (400, 300) -- the pixels themselves, not just a tag
    assert (row["width"], row["height"]) == (400, 300)
    stored = Image.open(io.BytesIO(row["bytes"]))
    assert stored.size == (400, 300)
    assert dict(stored.getexif()) == {}  # no EXIF of any kind survives the re-encode


def test_a_gif_svg_or_pdf_named_jpg_is_refused_by_magic_bytes_not_filename(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    bad = [
        ("gif.jpg", _photo("GIF"), "image/jpeg"),
        ("fake.jpg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/jpeg"),
        ("fake2.jpg", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "image/jpeg"),
    ]
    for name, content, ctype in bad:
        r = client.post(f"/u/{token}", files=[_part(name, content, ctype)])
        assert r.status_code == 415, (name, r.text)
        assert "not a JPEG, PNG or WebP" in r.text
    assert _photo_count(conn, page_id) == 0


def test_a_truncated_jpeg_is_415(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    whole = _photo("JPEG", size=(200, 200))
    truncated = whole[: len(whole) // 2]

    r = client.post(f"/u/{token}", files=[_part("t.jpg", truncated, "image/jpeg")])

    assert r.status_code == 415
    assert _photo_count(conn, page_id) == 0


def test_a_sixth_photo_is_refused_and_a_batch_that_would_go_over_stores_nothing(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    five = [
        _part(f"{i}.jpg", _photo("JPEG", color=("red", "green", "blue", "yellow", "purple")[i]), "image/jpeg")
        for i in range(5)
    ]
    ok = client.post(f"/u/{token}", files=five)
    assert ok.status_code == 200 and _photo_count(conn, page_id) == 5

    sixth = client.post(f"/u/{token}", files=[_part("6.jpg", _photo("JPEG"), "image/jpeg")])
    assert sixth.status_code == 400
    assert "Five photos at most; remove one first" in sixth.text
    assert _photo_count(conn, page_id) == 5  # nothing added

    conn.execute("DELETE FROM page_photo WHERE page_id = %s", (page_id,))
    conn.execute("UPDATE page SET sections = jsonb_set(sections, '{photos}', '[]') WHERE id = %s", (page_id,))
    four = [_part(f"{i}.jpg", _photo("JPEG"), "image/jpeg") for i in range(4)]
    assert client.post(f"/u/{token}", files=four).status_code == 200
    assert _photo_count(conn, page_id) == 4

    two_more = [_part("a.jpg", _photo("JPEG"), "image/jpeg"), _part("b.jpg", _photo("JPEG"), "image/jpeg")]
    over = client.post(f"/u/{token}", files=two_more)
    assert over.status_code == 400
    assert _photo_count(conn, page_id) == 4  # the whole batch of two is refused, not just the sixth


def test_a_9mb_part_is_413(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.post(f"/u/{token}", files=[_part("huge.jpg", b"x" * (9 * 1024 * 1024), "image/jpeg")])

    assert r.status_code == 413
    assert _photo_count(conn, page_id) == 0


def test_content_length_over_40mb_is_413_before_parsing(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.post(f"/u/{token}", files=[_part("huge.jpg", b"x" * (42 * 1024 * 1024), "image/jpeg")])

    assert r.status_code == 413
    assert _photo_count(conn, page_id) == 0


def test_no_file_chosen_is_a_plain_400(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, _page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.post(f"/u/{token}", data={"nothing": "here"})

    assert r.status_code == 400


# ---------------------------------------------------------------- removal


def test_removing_a_photo_deletes_it_and_rerenders(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    up = client.post(
        f"/u/{token}",
        files=[
            _part("a.jpg", _photo("JPEG", color="red"), "image/jpeg"),
            _part("b.jpg", _photo("JPEG", color="blue"), "image/jpeg"),
        ],
    )
    assert up.status_code == 200
    ids = [row["id"] for row in conn.execute("SELECT id FROM page_photo WHERE page_id = %s", (page_id,)).fetchall()]
    assert len(ids) == 2

    r = client.post(f"/u/{token}", data={"action": "remove", "photo": ids[0]})

    assert r.status_code == 200 and "Photo removed" in r.text and "1 of 5" in r.text
    assert _photo_count(conn, page_id) == 1
    remaining = conn.execute("SELECT id FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["id"]
    assert remaining == ids[1]
    sections = conn.execute("SELECT sections FROM page WHERE id = %s", (page_id,)).fetchone()["sections"]
    assert [p["id"] for p in sections["photos"]] == [ids[1]]


def test_removing_an_id_not_on_the_page_is_a_quiet_no_op(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    client.post(f"/u/{token}", files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")])
    before = _photo_count(conn, page_id)

    r = client.post(f"/u/{token}", data={"action": "remove", "photo": "ph_doesnotexist"})

    assert r.status_code == 200
    assert _photo_count(conn, page_id) == before


# ---------------------------------------------------------------- photos on the page itself (rendering, sanitiser)


def test_after_upload_update_page_and_revise_page_rerender_with_img_tags(client, conn, monkeypatch, fake):  # noqa: F811
    key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    up = client.post(f"/u/{token}", files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")])
    assert up.status_code == 200
    photo_id = conn.execute("SELECT id FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["id"]

    res = call_tool(client, "update_page", {"section": "hours", "text": "Mon-Fri 8-5"}, key=key)
    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["sections"]["photos"] == [
        conn.execute("SELECT id, width, height FROM page_photo WHERE id = %s", (photo_id,)).fetchone()
    ]
    prev_html = hosted.preview(conn, env["token"])["html"]
    assert f'src="/photos/{photo_id}"' in prev_html
    assert env["upload_url"].endswith(f"/u/{token}")  # H13: still shown back, while it is valid

    # a lone copies[0] repeats (FakeGenerator._next_copy): the same words create_page already used pass
    # lint again unchanged, so revise_page needs no special setup here.
    res2 = call_tool(client, "revise_page", {"instruction": "keep everything, just re-render"}, key=key)
    assert not res2["isError"], tool_text(res2)
    env2 = data(res2)
    prev_html2 = hosted.preview(conn, env2["token"])["html"]
    assert f'src="/photos/{photo_id}"' in prev_html2
    assert env2["upload_url"].endswith(f"/u/{token}")


def test_hosted_sanitise_accepts_a_page_with_photos(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    up = client.post(
        f"/u/{token}",
        files=[
            _part("a.jpg", _photo("JPEG", color="red"), "image/jpeg"),
            _part("b.jpg", _photo("JPEG", color="blue"), "image/jpeg"),
        ],
    )
    assert up.status_code == 200
    prev = _latest_preview(conn, page_id)

    assert hosted.sanitise(prev["html"], page_id=page_id, own_host=None, agent_base="http://testserver") == []


def test_preview_host_serves_the_photo_under_p_token_photos_id(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    up = client.post(f"/u/{token}", files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")])
    assert up.status_code == 200
    photo_id = conn.execute("SELECT id FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["id"]
    prev = _latest_preview(conn, page_id)

    shown = client.get(f"/p/{prev['token']}")
    assert shown.status_code == 200
    assert f'src="/p/{prev["token"]}/photos/{photo_id}"' in shown.text
    # the stored/live copy is untouched: still the root-absolute path a tenant host resolves directly
    assert f'src="/photos/{photo_id}"' in prev["html"]
    assert f'src="/p/{prev["token"]}/photos/{photo_id}"' not in prev["html"]

    fetched = client.get(f"/p/{prev['token']}/photos/{photo_id}")
    assert (
        fetched.status_code == 200
        and fetched.content
        == conn.execute("SELECT bytes FROM page_photo WHERE id = %s", (photo_id,)).fetchone()["bytes"]
    )

    # H13/H37: the upload page's own thumbnail goes through this exact same route -- no route of its own -- on
    # the agent host by its full address (H42: previews and their photos answer nowhere else)
    back = client.get(f"/u/{token}")
    assert back.status_code == 200
    assert f'src="http://testserver/p/{prev["token"]}/photos/{photo_id}"' in back.text


def test_the_upload_pages_thumbnails_load_from_the_agent_host_when_it_is_on_the_browser_host(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811 - pytest fixture
):
    """H42: create_page hands the upload link out on the browser host (AGENT_WEB_BASE_URL), where a preview's
    photo route now answers 404 -- so the page's thumbnails name the agent host in full, and its CSP lets them."""
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://home.test")
    up = client.post(f"/u/{token}", files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")], headers={"Host": "home.test"})
    assert up.status_code == 200
    photo_id = conn.execute("SELECT id FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["id"]
    path = f"/p/{_latest_preview(conn, page_id)['token']}/photos/{photo_id}"

    page = client.get(f"/u/{token}", headers={"Host": "home.test"})

    assert f'src="http://testserver{path}"' in page.text and f'src="{path}"' not in page.text
    assert "img-src 'self' http://testserver data:" in page.headers["content-security-policy"]
    assert client.get(path, headers={"Host": "home.test"}).status_code == 404
    assert client.get(f"http://testserver{path}").status_code == 200


def test_tenant_host_serves_the_photo_with_a_day_of_caching_and_no_cookie(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    up = client.post(f"/u/{token}", files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")])
    assert up.status_code == 200
    photo_id = conn.execute("SELECT id FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["id"]
    prev_html = _latest_preview(conn, page_id)["html"]
    conn.execute(
        "UPDATE page SET hostname = %s, url = %s, source = 'hosted', status = 'published', html = %s WHERE id = %s",
        (HOSTNAME, f"https://{HOSTNAME}/", prev_html, page_id),
    )

    from agent.app import create_app

    with TestClient(create_app(), base_url=f"https://{HOSTNAME}") as tenant:
        page = tenant.get("/")
        assert page.status_code == 200 and f'src="/photos/{photo_id}"' in page.text

        photo = tenant.get(f"/photos/{photo_id}")
        assert photo.status_code == 200
        assert photo.headers["cache-control"] == "public, max-age=86400"
        assert photo.headers["content-type"] == "image/jpeg"
        assert "set-cookie" not in photo.headers
        assert (
            photo.content == conn.execute("SELECT bytes FROM page_photo WHERE id = %s", (photo_id,)).fetchone()["bytes"]
        )


# ---------------------------------------------------------------- H38 (review round 1): memory safety


def test_counted_receive_aborts_as_soon_as_the_running_total_passes_the_limit():
    """Unit-level, no HTTP involved: `_counted_receive` must raise on the message that pushes the running
    total over its limit -- never read further before doing so (the review's "at most ~40 MB read")."""
    seen: list[int] = []

    async def fake_receive():
        seen.append(1)
        return {"type": "http.request", "body": b"x" * (10 * 1024 * 1024), "more_body": True}

    wrapped = uploads._counted_receive(fake_receive, limit=25 * 1024 * 1024)

    async def drive():
        with pytest.raises(uploads._TooLarge):
            for _ in range(10):
                await wrapped()

    asyncio.run(drive())
    assert len(seen) == 3  # 10 + 10 + 10 MB > 25 MB on the third message; a fourth is never attempted


def test_a_link_that_is_none_answers_404_before_a_byte_of_the_body_is_read(client, monkeypatch):
    """Final review I3: the link is looked up before the body is read -- 20 MB sent to a link that is none (or has
    ended) answers the plain 404 with the form never parsed and not one byte of the body received, so nobody without a
    link can make the app read and spool up to MAX_BODY_BYTES."""
    parsed: list[int] = []
    received: list[int] = []
    real_form, real_counted = Request.form, uploads._counted_receive

    def form(self, **limits):
        parsed.append(1)
        return real_form(self, **limits)

    def counted(receive, limit):
        inner = real_counted(receive, limit)

        async def wrapped():
            message = await inner()
            received.append(len(message.get("body", b"")))
            return message

        return wrapped

    monkeypatch.setattr(Request, "form", form)
    monkeypatch.setattr(uploads, "_counted_receive", counted)
    body, content_type = _multipart_body([("photos", "big.jpg", b"x" * (20 * 1024 * 1024), "image/jpeg")])

    r = client.post("/u/up_nope", content=body, headers={"content-type": content_type})

    assert r.status_code == 404 and r.text == "Not found."
    assert parsed == [] and sum(received) == 0


def test_a_post_with_no_content_length_is_411(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    body, content_type = _multipart_body([("photos", "a.jpg", _photo("JPEG"), "image/jpeg")])

    def streamed():
        yield body

    r = client.post(f"/u/{token}", content=streamed(), headers={"content-type": content_type})

    assert r.status_code == 411
    assert _photo_count(conn, page_id) == 0


def test_a_lying_content_length_is_413_once_the_stream_itself_exceeds_it(client, conn, monkeypatch, fake):  # noqa: F811
    """The header says 1 MB; the body actually sent is 45 MB -- the header-value fast path never trips (1 MB
    is well within budget), so only the counted-receive wrapper can catch this."""
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    huge = b"x" * (45 * 1024 * 1024)
    body, content_type = _multipart_body([("photos", "huge.jpg", huge, "image/jpeg")])

    r = client.post(
        f"/u/{token}", content=body, headers={"content-type": content_type, "content-length": str(1024 * 1024)}
    )

    assert r.status_code == 413
    assert _photo_count(conn, page_id) == 0


def test_an_80mp_photo_is_415_before_any_pixel_is_decoded(client, conn, monkeypatch, fake):  # noqa: F811
    """80 MP (2x MAX_IMAGE_PIXELS) compresses to a few hundred KB as a solid fill -- trivially inside
    MAX_PART_BYTES -- so the header-based pixel check, not the part size, is what must refuse this, and it
    must do so without ever calling Image.load() (RSS stays flat -- asserted here by making .load() itself
    fail the test if it is ever reached, rather than by reading memory)."""
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    huge = Image.new("RGB", (10000, 8000), "red")  # 80,000,000 px, 2x the 40,000,000 ceiling
    buf = io.BytesIO()
    huge.save(buf, format="PNG")
    raw = buf.getvalue()
    assert len(raw) < 2 * 1024 * 1024  # comfortably inside MAX_PART_BYTES -- the part size proves nothing here

    def _load_must_not_be_called(self):
        raise AssertionError("Image.load() was called on an over-the-limit photo (H38)")

    monkeypatch.setattr(Image.Image, "load", _load_must_not_be_called)

    r = client.post(f"/u/{token}", files=[_part("big.png", raw, "image/png")])

    assert r.status_code == 415
    assert "could not be read" in r.text
    assert _photo_count(conn, page_id) == 0


def test_a_bad_photo_partway_through_a_batch_means_nothing_from_it_is_stored(client, conn, monkeypatch, fake):  # noqa: F811
    """H39 (review round 2): a and b are never written at all, not written-then-undone -- step 2 (read, sniff,
    decode, resize, re-encode) validates every file in the batch before `_store_batch`'s own transaction ever
    opens, so a bad file later in the batch is refused before the database has seen any of it."""
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    r = client.post(
        f"/u/{token}",
        files=[
            _part("a.jpg", _photo("JPEG", color="red"), "image/jpeg"),
            _part("b.jpg", _photo("JPEG", color="blue"), "image/jpeg"),
            _part("c.gif", _photo("GIF"), "image/jpeg"),  # the third file in the batch is bad
        ],
    )

    assert r.status_code == 415
    assert _photo_count(conn, page_id) == 0  # a and b were never stored, not stored then undone


def test_the_five_photo_cap_is_still_checked_before_any_file_is_read(client, conn, monkeypatch, fake):  # noqa: F811
    """The early, optimistic count check (H39 step 1) is read-only and happens from `len(files)` alone,
    before the first `await f.read()` -- a batch that is refused for being too large never even touches
    Pillow."""
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)
    five = [_part(f"{i}.jpg", _photo("JPEG"), "image/jpeg") for i in range(5)]
    assert client.post(f"/u/{token}", files=five).status_code == 200
    assert _photo_count(conn, page_id) == 5

    def _process_must_not_be_called(raw):
        raise AssertionError("a file was processed although the batch was already over the five-photo cap")

    monkeypatch.setattr(uploads, "_process", _process_must_not_be_called)

    r = client.post(f"/u/{token}", files=[_part("6.jpg", _photo("JPEG"), "image/jpeg")])

    assert r.status_code == 400
    assert "Five photos at most; remove one first" in r.text
    assert _photo_count(conn, page_id) == 5


# ---------------------------------------------------------------- H39 (review round 2): the lock's own duration


def test_a_slow_part_does_not_hold_the_page_row_lock(client, conn, monkeypatch, fake):  # noqa: F811
    """H39: step 2 (read, sniff, decode, resize, re-encode -- `_process`, here made to stall) opens no
    database connection of its own at all, so nothing it does, however long it takes, can be holding the page
    row's lock. Proven directly rather than by timing: while the upload is deliberately stalled inside
    `_process`, a second, independent connection takes `FOR UPDATE NOWAIT` on the very same row -- which
    raises immediately instead of blocking if any other transaction already holds it, so its plain success
    here is the proof that none does."""
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    entered = threading.Event()
    release = threading.Event()
    real_process = uploads._process

    def slow_process(raw: bytes) -> tuple[bytes, int, int]:
        entered.set()
        release.wait(5)
        return real_process(raw)

    monkeypatch.setattr(uploads, "_process", slow_process)

    outcome: dict = {}

    def do_upload() -> None:
        outcome["response"] = client.post(f"/u/{token}", files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")])

    upload_thread = threading.Thread(target=do_upload)
    upload_thread.start()
    try:
        assert entered.wait(5), "the upload never reached _process"

        with db.connect(TEST_DB_URL) as second:
            row = second.execute("SELECT id FROM page WHERE id = %s FOR UPDATE NOWAIT", (page_id,)).fetchone()
        assert row is not None
    finally:
        release.set()
        upload_thread.join(5)

    assert outcome["response"].status_code == 200
    assert _photo_count(conn, page_id) == 1
