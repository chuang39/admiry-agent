"""The upload link of an industry site (A3p-6 batch 2 Task 3; plan 2026-10-06-agent-a3p-6-2-facts-photos-publish Q7, Q8,
Q9, Q12, Q21 and its Review Focus "A photo with no kind, and a photo of another page"): the page of a site asks what
the photos are -- the family's kinds, in the checklist's order, each with how many are in of how many the site uses --,
takes a batch only with a kind and only up to what that kind still has room for, keeps a logo's transparency as a PNG
of at most 800 px and 300 KB (a new logo replaces the one before), shows its own thumbnails through the link itself,
and after a batch or a removal builds the site's preview again without the model and without the render check, so the
owner sees their photos -- a rebuild that fails keeps the photo and says the site shows it the next time it is built.
The one-page starter page keeps its form and its five photos (A3p-2: test_uploads.py).

`_site` is this file's own setup: a business whose page is an industry site's -- the owner's answers in its draft, the
words of the sample roofer kept as a build leaves them (none: the site was never built) -- and a valid upload link."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading

import pytest
from PIL import Image

from agent import bundles, db, landing, uploads
from agent.sites import build, intake, photos, samples
from agent.sites import words as site_words
from agent.sites.spec import SiteSpec
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_DB_URL
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_render_check import renderer  # noqa: F401 (fixture)
from agent.tests.test_sites_render import Doc, add_photo, jpeg, library, logo_png  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import DOMAIN
from agent.tests.test_uploads import _part, _photo, _ready

BIZ = "biz_site"
LABEL = "Replace with your photo"
# the preview's address is a link the owner can open (the founder's dev walk, 2026-10-08: it was plain text to copy)
# -- since the phone redesign (2026-10-15) a button beside "Saved.", not the address spelled out
SEE_YOUR_SITE = re.compile(
    r'<p class="done">Saved\.</p><a class="btn" href="(https://preview--[0-9a-f]{16}\.business\.test/)" '
    r'target="_blank" rel="noopener">See your site</a>'
)
# a square's own file box (the attribute order the starter page's tests quote too), the page's one instruction, and
# the tag on the two kinds the site waits for
PICK = '<input type="file" name="photos" accept="image/*"'
TAP = "Tap a square to add a photo from your phone."
NEEDED = '<span class="badge badge-need">Needed to go live</span>'
DONE = '<a class="btn btn-dark" href="/">Done</a>'  # back to the home, where the conversation and the card are
SAVED_LATER = "Saved. Your site shows them the next time it is built."
SAY_WHAT = "Say what the photos show first."
KEY_LABEL = "One photo of your own work, your team or your truck"
# The sentence that refuses a photo of a kind the site has all of (fix round 0): the kind's label in curly quotes
KEY_FULL = f"The site has all it uses for “{KEY_LABEL}”; remove one first."
LOGO_FULL = "The site has all it uses for “Your logo”; remove one first."
WORK_FULL = "The site has all it uses for “More of your work”; remove one first."
ROOMS_FULL = "The site has all it uses for “The waiting room or a treatment room”; remove one first."


# ---------------------------------------------------------------- setup


def _site(conn, monkeypatch, *, family: str | None = "home_services", words: bool = True, biz: str = BIZ):
    """(business id, page id, upload token) of a business whose page is an industry site's, on a server with hosting on:
    the draft names the family, the look and the owner's facts (the sample roofer's services among them), keeps the
    sample roofer's words when `words` (a build was made: photos rebuild the site) -- with the fingerprint of the facts
    they stand for, as a build that passed leaves it (`words_for`, final review I5) -- and holds none otherwise (it was
    never built: the photos join its first build)."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    conn.execute("INSERT INTO business (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (biz,))
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    draft = {
        **intake.draft(None),
        "family": family,
        "trade": {"home_services": "roofing", "dental": "dentist"}.get(family),
        "style": {"home_services": "solid", "dental": "calm"}.get(family),
        "facts": {
            "name": "Oakline Roofing",
            "phone": "(704) 555-0142",
            "service_area": ["Charlotte", "Matthews"],
            "services": [{"name": n, "lead": True} for n in ("Roof replacement", "Roof repair", "Storm damage")],
        },
    }
    if words:
        draft["words"] = samples.load("roofer", style="solid").words.model_dump(mode="json")
    intake.save_draft(conn, page_id, draft)
    if words:
        facts = intake.facts(conn, biz, landing.get(conn, page_id))[0]
        read = SiteSpec(family=family, style=draft["style"], trade=draft["trade"], facts=facts)
        intake.update_draft(conn, page_id, {"words_for": site_words.fingerprint(read)})
    return biz, page_id, uploads.new_token(conn, page_id)


def _post(client, token: str, kind: str | None, *files):
    """A batch of `files` ((name, bytes, content type) each) said to show `kind` (None: the form field is not sent at
    all), as the page's form sends it."""
    return client.post(
        f"/u/{token}",
        data={} if kind is None else {"kind": kind},
        files=[_part(name, content, ctype) for name, content, ctype in files],
    )


def _jpg(name: str = "a.jpg", shade: int = 120, size=(1200, 800)) -> tuple[str, bytes, str]:
    return name, jpeg(*size, shade), "image/jpeg"


def _rows(conn, page_id: str, kind: str | None = None) -> list[dict]:
    return conn.execute(
        "SELECT id, kind, content_type, width, height, bytes, created_at FROM page_photo "
        "WHERE page_id = %s AND (%s::text IS NULL OR kind = %s) ORDER BY created_at, id",
        (page_id, kind, kind),
    ).fetchall()


def _preview(conn, page_id: str) -> dict | None:
    """The page's live bundle preview: its token, its sections."""
    return conn.execute(
        "SELECT token, sections FROM page_preview WHERE page_id = %s AND kind = 'bundle' AND expires_at > now() "
        "ORDER BY created_at DESC LIMIT 1",
        (page_id,),
    ).fetchone()


def _file(conn, token: str, path: str) -> bytes:
    sha = bundles.preview_files(conn, token)[path]
    return bytes(conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()["bytes"])


def _document(conn, preview: dict) -> dict:
    """The document the preview was built from."""
    return conn.execute("SELECT data FROM site_spec WHERE id = %s", (preview["sections"]["site_spec_id"],)).fetchone()[
        "data"
    ]


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def _kind_inputs(html: str) -> list[str]:
    """The kinds the page's empty squares name, in order: one hidden field per square."""
    return re.findall(r'<input type="hidden" name="kind" value="([a-z_]+)">', html)


def _section(html: str, kind: str) -> str:
    """One kind's section of the page."""
    return html.split(f'id="kind-{kind}"')[1].split("</section>")[0]


def _shown(html: str) -> str:
    """The words a page shows: its style and tags gone."""
    return " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL)).split())


def _noise_png(size=(1600, 800)) -> bytes:
    """A logo of random pixels, alpha channel included: a PNG that cannot be made small but by making it smaller."""
    buf = io.BytesIO()
    Image.frombytes("RGBA", size, os.urandom(size[0] * size[1] * 4)).save(buf, "PNG")
    return buf.getvalue()


def _noise_jpeg(size=(3000, 2300)) -> bytes:
    """A JPEG of random pixels at a high quality: close to the largest part the link takes (8 MiB, 7.7 MiB here)."""
    buf = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


# ---------------------------------------------------------------- the page: what are these photos? (Q7)


def test_the_page_of_a_site_shows_each_kind_as_squares_to_tap_in_the_checklists_order(client, conn, monkeypatch):
    """The phone redesign (2026-10-15): nothing is chosen from a list -- each kind is a row of squares, one per photo the
    site still uses, and a square is its own form naming the kind."""
    _biz, _page_id, token = _site(conn, monkeypatch, words=False)

    r = client.get(f"/u/{token}")

    page = r.text
    assert r.status_code == 200 and "<select" not in page and "What are these photos?" not in page
    assert re.findall(r"<h2>([^<]*)</h2>", page) == [k["label"] for k in photos.KINDS["home_services"]]
    assert _kind_inputs(page) == [k["id"] for k in photos.KINDS["home_services"] for _ in range(k["need"])]
    # several at once only where more than one is still wanted: the work (3) and the before-and-afters (2)
    assert page.count(PICK + " multiple required") == 5 and page.count(PICK + " required") == 4
    for have_of_need in ("0 of 1", "0 of 3", "0 of 2"):  # the logo, the work, the before-and-afters
        assert have_of_need in page
    assert "of 5" not in page and "photos at most" not in page
    # the two the site waits for are tagged, and the gate's own sentence stands above the squares
    assert page.count(NEEDED) == 2 and NEEDED in _section(page, "logo") and NEEDED in _section(page, "key")
    assert TAP in page and photos.GATE["home_services"]["both"] in page
    assert page.index(photos.GATE["home_services"]["both"]) < page.index('id="kind-logo"')
    assert DONE in page and page.index(DONE) > page.rindex(PICK)  # last, after every square
    assert r.headers["x-robots-tag"] == "noindex" and r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers


def test_a_dental_site_asks_for_its_own_kinds_and_two_photos_of_the_rooms(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, family="dental", words=False)

    page = client.get(f"/u/{token}").text
    assert re.findall(r"<h2>([^<]*)</h2>", page) == [k["label"] for k in photos.KINDS["dental"]]
    assert _kind_inputs(page) == ["logo", "key", "place", "place", "team", "dentist"]  # the rooms: two squares
    assert "0 of 2" in page and "0 of 3" not in page
    assert photos.GATE["dental"]["both"] in page

    three = _post(client, token, "place", _jpg("a.jpg", 50), _jpg("b.jpg", 80), _jpg("c.jpg", 110))
    assert three.status_code == 400 and ROOMS_FULL in three.text
    assert _rows(conn, page_id) == []
    two = _post(client, token, "place", _jpg("a.jpg", 50), _jpg("b.jpg", 80))
    assert two.status_code == 200 and "2 of 2" in two.text
    assert [r["kind"] for r in _rows(conn, page_id)] == ["place", "place"]


def test_the_starter_page_keeps_its_form_its_five_photos_and_no_kind(client, conn, monkeypatch, fake):  # noqa: F811
    _key, _biz, page_id, token = _ready(client, conn, monkeypatch, fake)

    page = client.get(f"/u/{token}").text
    assert "<select" not in page and "What are these photos?" not in page and "0 of 5" in page and DONE in page
    assert page.count(PICK + " multiple required") == 5 and _kind_inputs(page) == []  # five squares, no kind
    assert "Needed to go live" not in page and TAP in page

    # a `kind` sent to a starter page is nothing it asks for: the photo is stored as it always was, with none
    r = client.post(f"/u/{token}", data={"kind": "logo"}, files=[_part("a.jpg", _photo("JPEG"), "image/jpeg")])
    assert r.status_code == 200 and "1 of 5" in r.text and r.text.count(PICK) == 4  # one square filled, four left
    assert re.search(  # a link here too, as a button beside the sentence
        r'<p class="done">Photos added\.</p><a class="btn" href="https?://[^"]+/p/[^"]+" target="_blank" rel="noopener">'
        r"See your page</a>",
        r.text,
    )
    (row,) = _rows(conn, page_id)
    assert row["kind"] is None and row["content_type"] == "image/jpeg"
    sections = conn.execute("SELECT sections FROM page WHERE id = %s", (page_id,)).fetchone()["sections"]
    assert [p["id"] for p in sections["photos"]] == [row["id"]]  # re-rendered from the page's own copy, as ever


def test_a_batch_without_a_kind_or_with_one_the_family_has_not_stores_nothing(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)

    for kind in (None, "", "hero", "dentist", "LOGO"):  # `dentist` is the other family's, the rest are nobody's
        r = _post(client, token, kind, _jpg())
        assert r.status_code == 400 and SAY_WHAT in r.text, kind
    assert _rows(conn, page_id) == [] and _count(conn, "page_preview") == 0


def test_a_site_page_with_no_family_is_not_found(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, family=None, words=False)

    assert client.get(f"/u/{token}").status_code == 404
    assert _post(client, token, "key", _jpg()).status_code == 404
    assert _rows(conn, page_id) == []


# ---------------------------------------------------------------- a batch: the kind, the room, the order


def test_a_key_photo_is_stored_as_one_and_the_preview_shows_it_in_the_hero_with_no_model_and_no_render_check(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
):
    _biz, page_id, token = _site(conn, monkeypatch)

    def no_model(*args, **kwargs):
        raise AssertionError("a photo change asked the model for words")

    monkeypatch.setattr(site_words, "write", no_model)

    r = _post(client, token, "key", _jpg())

    assert r.status_code == 200, r.text
    (row,) = _rows(conn, page_id)
    assert row["kind"] == "key" and row["content_type"] == "image/jpeg"
    preview = _preview(conn, page_id)
    host = conn.execute("SELECT preview_host FROM page WHERE id = %s", (page_id,)).fetchone()["preview_host"]
    assert SEE_YOUR_SITE.search(r.text).group(1) == f"https://{host}/" and preview["sections"]["kind"] == "site"
    assert preview["sections"]["render"] is None and renderer.asked == []  # the render check was never asked
    # the photo as stored is a file of the new bundle, and the hero shows it as the owner's, with no label
    path = f"assets/img/{hashlib.sha256(bytes(row['bytes'])).hexdigest()[:16]}.jpg"
    assert bundles.preview_files(conn, preview["token"])[path] == hashlib.sha256(bytes(row["bytes"])).hexdigest()
    home = Doc(_file(conn, preview["token"], "index.html").decode())
    hero = next(f for f in home.figures if f["attrs"].get("data-slot") == "hero")
    assert hero["attrs"]["data-source"] == "owner" and LABEL not in hero["text"]
    assert hero["imgs"][0]["src"] == "/" + path
    document = _document(conn, preview)
    assert document["images"]["hero"]["id"] == row["id"] and document["images"]["hero"]["source"] == "owner"
    assert document["checklist"]["key_photos_ok"] is False  # the logo is still to come
    assert [(i["id"], i["have"], i["done"]) for i in document["checklist"]["items"]][:2] == [
        ("logo", 0, False),
        ("key", 1, True),
    ]


def test_a_second_photo_of_a_kind_at_its_need_is_refused_with_the_cap_sentence(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    assert _post(client, token, "key", _jpg("a.jpg", 50)).status_code == 200

    again = _post(client, token, "key", _jpg("b.jpg", 90))

    assert again.status_code == 400 and KEY_FULL in again.text
    assert [r["kind"] for r in _rows(conn, page_id)] == ["key"]
    # the page offers no square for a kind that is full, and still the others' (the work's three, the before-and-afters'
    # two)
    page = client.get(f"/u/{token}").text
    offered = _kind_inputs(page)
    assert "key" not in offered
    assert offered == ["logo", "place", "team", "work", "work", "work", "before_after", "before_after"]
    assert "1 of 1" in page


def test_a_batch_of_more_than_the_kind_has_room_for_stores_none_of_it(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    shades = [30, 60, 90, 120, 150, 180]

    four = _post(client, token, "work", *[_jpg(f"{s}.jpg", s) for s in shades[:4]])
    six = _post(client, token, "work", *[_jpg(f"{s}.jpg", s) for s in shades])  # more than the old form took
    assert (four.status_code, six.status_code) == (400, 400) and WORK_FULL in four.text and WORK_FULL in six.text
    assert _rows(conn, page_id) == []

    two = _post(client, token, "work", *[_jpg(f"{s}.jpg", s) for s in shades[:2]])
    assert two.status_code == 200 and "2 of 3" in two.text
    over = _post(client, token, "work", _jpg("x.jpg", 10), _jpg("y.jpg", 20))  # one place left, two sent
    assert over.status_code == 400 and WORK_FULL in over.text
    assert len(_rows(conn, page_id, "work")) == 2
    last = _post(client, token, "work", _jpg("z.jpg", 240))
    assert last.status_code == 200 and "3 of 3" in last.text and len(_rows(conn, page_id, "work")) == 3
    assert _post(client, token, "work", _jpg("w.jpg", 5)).status_code == 400


def test_a_batch_over_its_kinds_room_is_refused_before_a_single_photo_is_decoded(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    assert _post(client, token, "key", _jpg()).status_code == 200

    def no_decoding(raw, **kwargs):
        raise AssertionError("a photo was decoded although its batch was over its kind's room")

    monkeypatch.setattr(uploads, "_process", no_decoding)
    for kind, count in (("key", 1), ("work", 4)):
        r = _post(client, token, kind, *[_jpg(f"{i}.jpg", 30 * i) for i in range(count)])
        assert r.status_code == 400 and {"key": KEY_FULL, "work": WORK_FULL}[kind] in r.text, kind
    assert [r["kind"] for r in _rows(conn, page_id)] == ["key"]


def test_the_store_checks_the_room_again_under_the_lock(conn, monkeypatch):
    """The early check is only an early one: a batch that fitted when it was judged can find its kind full by the time
    it is stored (a second upload landed in between), and then none of it is kept."""
    _biz, page_id, _token = _site(conn, monkeypatch, words=False)
    one = [uploads._process(jpeg(900, 600, 90))]

    assert uploads._store_site_batch(page_id, "key", one) is None
    assert uploads._store_site_batch(page_id, "key", one) == KEY_FULL
    assert uploads._store_site_batch(page_id, "work", one * 4) == WORK_FULL
    assert uploads._store_site_batch(page_id, "hero", one) == SAY_WHAT  # not a kind of this family
    assert [r["kind"] for r in _rows(conn, page_id)] == ["key"]
    for _ in range(2):  # a logo has room whatever is on file: the second takes the first's place
        assert uploads._store_site_batch(page_id, "logo", one) is None
    assert [r["kind"] for r in _rows(conn, page_id)] == ["key", "logo"]
    conn.execute("UPDATE page SET site_draft = NULL WHERE id = %s", (page_id,))  # the draft cleared meanwhile
    assert uploads._store_site_batch(page_id, "key", one) == uploads.BAD_REQUEST and len(_rows(conn, page_id)) == 2


def test_the_photos_of_a_batch_keep_the_order_they_were_sent_in(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
):
    _biz, page_id, token = _site(conn, monkeypatch)

    r = _post(client, token, "work", _jpg("a.jpg", 200), _jpg("b.jpg", 100), _jpg("c.jpg", 20))

    assert r.status_code == 200, r.text
    rows = _rows(conn, page_id, "work")
    assert len({r["created_at"] for r in rows}) == 3  # each photo its own moment, though one statement's worth of rows
    document = _document(conn, _preview(conn, page_id))
    assert [document["images"][f"gallery-{i}"]["id"] for i in (1, 2, 3)] == [r["id"] for r in rows]
    # and the first of them is the one whose bytes were sent first (the lightest shade last)
    shades = [Image.open(io.BytesIO(bytes(r["bytes"]))).convert("RGB").getpixel((5, 5))[0] for r in rows]
    assert shades[0] > shades[1] > shades[2]


def test_the_page_lists_each_kind_with_its_photos_and_their_thumbnails_come_from_the_link(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    _post(client, token, "key", _jpg("k.jpg", 70))
    _post(client, token, "work", _jpg("w1.jpg", 100), _jpg("w2.jpg", 160))
    key = _rows(conn, page_id, "key")[0]["id"]
    work = [r["id"] for r in _rows(conn, page_id, "work")]

    page = client.get(f"/u/{token}").text

    assert f'src="/u/{token}/photo/{key}"' in page
    for photo_id in work:
        assert f'src="/u/{token}/photo/{photo_id}"' in page
    assert "/p/pv_" not in page  # not through a preview: a site's preview is a bundle that serves no photo route
    # each photo under its own kind's label, in the checklist's order, with how many of how many
    assert page.index(KEY_LABEL + "</h2>") < page.index("More of your work</h2>") < page.index("Before and after</h2>")
    assert page.index(f"/photo/{key}") < page.index("More of your work</h2>") < page.index(f"/photo/{work[0]}")
    assert "1 of 1" in page and "2 of 3" in page and "0 of 2" in page
    assert page.count('name="action" value="remove"') == 3 and page.count('aria-label="Remove"') == 3
    assert f'<input type="hidden" name="photo" value="{key}">' in page
    # the squares still to fill follow the photos: none for the key (at its need), one for the work (two of three, so
    # one at a time), two for the before-and-afters (several at once)
    assert _kind_inputs(page) == ["logo", "place", "team", "work", "before_after", "before_after"]
    assert page.count(PICK + " multiple required") == 2 and page.count(PICK + " required") == 4
    assert page.index(f"/photo/{work[1]}") < page.index('value="work"')


def test_the_page_carries_its_own_script_and_stylesheet_and_offers_upload_without_the_script(client, conn, monkeypatch):
    """The phone redesign (2026-10-15): a photo chosen in a square sends itself (upload.js, let in by a CSP of
    `script-src 'self'` -- never inline), and a browser with no script gets an Upload button in the square instead."""
    _biz, _page_id, token = _site(conn, monkeypatch, words=False)

    r = client.get(f"/u/{token}")

    page = r.text
    assert page.index('href="/static/page.css"') < page.index('href="/static/upload.css"')
    assert '<script src="/static/upload.js" defer></script>' in page
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'; script-src 'self'; style-src 'self'; ") and "unsafe-inline" not in csp
    assert "<script>" not in page and " onchange=" not in page  # the CSP allows no inline script
    assert page.count('<button class="btn btn-dark send" type="submit">Upload</button>') == 9
    assert page.count('<span class="sending-words">Sending…</span>') == 9
    for path, kind in (("/static/upload.js", "javascript"), ("/static/upload.css", "text/css")):
        f = client.get(path)
        assert f.status_code == 200 and kind in f.headers["content-type"], path


def test_the_logo_square_offers_replace_once_a_logo_is_in(client, conn, monkeypatch):
    _biz, _page_id, token = _site(conn, monkeypatch, words=False)
    before = client.get(f"/u/{token}").text
    assert '<span class="pick-words">Add</span>' in _section(before, "logo") and "Replace" not in before

    assert _post(client, token, "logo", ("logo.png", logo_png(400, 100, 140), "image/png")).status_code == 200

    logo = _section(client.get(f"/u/{token}").text, "logo")
    assert 'aria-label="Remove"' in logo and '<span class="pick-words">Replace</span>' in logo
    assert _kind_inputs(logo) == ["logo"] and "1 of 1" in logo and "Needed to go live" not in logo


def test_the_gate_is_said_above_the_squares_until_the_logo_and_the_key_photo_are_in(client, conn, monkeypatch):
    _biz, _page_id, token = _site(conn, monkeypatch, words=False)
    assert _post(client, token, "logo", ("logo.png", logo_png(400, 100, 140), "image/png")).status_code == 200

    page = client.get(f"/u/{token}").text
    assert photos.GATE["home_services"]["key"] in page and page.count(NEEDED) == 1 and NEEDED in _section(page, "key")

    assert _post(client, token, "key", _jpg("k.jpg", 70)).status_code == 200
    page = client.get(f"/u/{token}").text
    assert "Needed to go live" not in page and "before it goes live" not in page and TAP in page


def test_a_wordmark_the_owner_accepted_stands_for_the_logo(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    intake.update_draft(conn, page_id, {"wordmark_ok": True})

    page = client.get(f"/u/{token}").text

    assert photos.GATE["home_services"]["key"] in page and page.count(NEEDED) == 1
    assert NEEDED not in _section(page, "logo") and _kind_inputs(_section(page, "logo")) == ["logo"]


def test_the_words_of_the_page_pass_the_jargon_tests(client, conn, monkeypatch):
    _biz, _page_id, token = _site(conn, monkeypatch, words=False)
    texts = [client.get(f"/u/{token}").text]
    texts.append(_post(client, token, None, _jpg()).text)
    texts.append(_post(client, token, "key", _jpg()).text)
    texts.append(_post(client, token, "key", _jpg()).text)
    for html in texts:
        words = _shown(html)
        assert not (JARGON.search(words) or ACCOUNT_JARGON.search(words) or MONEY_JARGON.search(words)), words


# ---------------------------------------------------------------- the logo: a PNG keeps its transparency (Q7)


def test_a_logo_with_transparency_stays_a_png_and_keeps_its_alpha(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)

    r = _post(client, token, "logo", ("logo.png", logo_png(400, 100, 140), "image/png"))

    assert r.status_code == 200, r.text
    (row,) = _rows(conn, page_id)
    assert row["kind"] == "logo" and row["content_type"] == "image/png"
    stored = Image.open(io.BytesIO(bytes(row["bytes"])))
    assert stored.format == "PNG" and stored.mode == "RGBA" and stored.size == (400, 100)  # never made bigger
    assert (row["width"], row["height"]) == (400, 100)
    assert stored.getpixel((0, 0))[3] == 0 and stored.getpixel((200, 50))[3] == 255  # the see-through border stays


def test_a_big_logo_is_brought_to_800_px_and_a_noisy_one_under_300_kb(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)

    wide = _post(client, token, "logo", ("wide.png", logo_png(2400, 1200, 90), "image/png"))
    assert wide.status_code == 200, wide.text
    (row,) = _rows(conn, page_id)
    assert (row["width"], row["height"], row["content_type"]) == (800, 400, "image/png")
    assert Image.open(io.BytesIO(bytes(row["bytes"]))).size == (800, 400)

    raw = _noise_png()
    assert len(raw) > 3 * 1024 * 1024  # megabytes of pixels that no encoder can make small
    noisy = _post(client, token, "logo", ("noise.png", raw, "image/png"))
    assert noisy.status_code == 200, noisy.text
    (row,) = _rows(conn, page_id)  # the new logo replaced the one before
    stored = Image.open(io.BytesIO(bytes(row["bytes"])))
    assert row["content_type"] == "image/png" and stored.format == "PNG" and stored.mode == "RGBA"
    assert len(row["bytes"]) <= 300 * 1024 and max(stored.size) <= 800
    assert stored.size == (row["width"], row["height"])
    assert max(stored.size) >= 200 and stored.getchannel("A").getextrema()[0] < 255  # smaller, not flattened


def test_a_logo_without_transparency_is_a_jpeg_like_any_photo(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    flat_png = _photo("PNG", size=(300, 100), color="navy")
    assert Image.open(io.BytesIO(flat_png)).mode == "RGB"

    r = _post(client, token, "logo", ("logo.png", flat_png, "image/png"))
    assert r.status_code == 200, r.text
    (row,) = _rows(conn, page_id)
    assert row["content_type"] == "image/jpeg" and Image.open(io.BytesIO(bytes(row["bytes"]))).format == "JPEG"

    wide = _post(client, token, "logo", _jpg("logo.jpg", 90, size=(1600, 400)))
    assert wide.status_code == 200, wide.text
    (row,) = _rows(conn, page_id)
    assert (row["width"], row["height"], row["content_type"]) == (800, 200, "image/jpeg")
    assert Image.open(io.BytesIO(bytes(row["bytes"]))).format == "JPEG"


def test_an_8_mb_jpeg_logo_comes_out_at_most_800_px_and_300_kb(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    raw = _noise_jpeg()
    assert 7 * 1024 * 1024 < len(raw) <= uploads.MAX_PART_BYTES

    r = _post(client, token, "logo", ("card.jpg", raw, "image/jpeg"))

    assert r.status_code == 200, r.text
    (row,) = _rows(conn, page_id)
    stored = Image.open(io.BytesIO(bytes(row["bytes"])))
    assert row["content_type"] == "image/jpeg" and stored.format == "JPEG"
    assert max(stored.size) <= 800 and len(row["bytes"]) <= 300 * 1024
    assert stored.size == (row["width"], row["height"])


def test_the_other_kinds_are_processed_as_a_photo_always_was(client, conn, monkeypatch):
    """A photo that is not a logo stays at 1600 px as a JPEG -- a transparent PNG of a team photo too: flattened."""
    _biz, page_id, token = _site(conn, monkeypatch, words=False)

    r = _post(client, token, "team", ("t.png", logo_png(2400, 1200, 90), "image/png"))

    assert r.status_code == 200, r.text
    (row,) = _rows(conn, page_id)
    assert (row["content_type"], row["width"], row["height"]) == ("image/jpeg", 1600, 800)
    assert Image.open(io.BytesIO(bytes(row["bytes"]))).format == "JPEG"


def test_a_new_logo_replaces_the_one_before_in_the_same_step(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    assert _post(client, token, "logo", ("a.png", logo_png(400, 100, 100), "image/png")).status_code == 200
    first = _rows(conn, page_id, "logo")[0]["id"]

    r = _post(client, token, "logo", ("b.png", logo_png(300, 90, 200), "image/png"))

    assert r.status_code == 200, r.text
    (row,) = _rows(conn, page_id, "logo")  # one row of kind logo
    assert row["id"] != first and (row["width"], row["height"]) == (300, 90)
    assert "1 of 1" in r.text and f"/photo/{first}" not in r.text
    # a batch of two logos is more than the site uses: refused, and the logo it has stands
    two = _post(client, token, "logo", _jpg("c.jpg", 10), _jpg("d.jpg", 20))
    assert two.status_code == 400 and LOGO_FULL in two.text
    assert [r["id"] for r in _rows(conn, page_id, "logo")] == [row["id"]]
    # the form keeps the logo among its kinds, for the owner who wants to replace theirs
    assert 'value="logo"' in client.get(f"/u/{token}").text


# ---------------------------------------------------------------- the thumbnails route (Q7, Q21)


def test_a_photo_is_served_to_the_link_of_its_own_page_with_its_type_and_a_private_five_minutes(
    client, conn, monkeypatch
):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    _post(client, token, "logo", ("logo.png", logo_png(), "image/png"))
    _post(client, token, "key", _jpg())
    logo, key = (_rows(conn, page_id, k)[0] for k in ("logo", "key"))

    for row, content_type in ((key, "image/jpeg"), (logo, "image/png")):
        r = client.get(f"/u/{token}/photo/{row['id']}")
        assert r.status_code == 200 and r.content == bytes(row["bytes"])
        assert r.headers["content-type"] == content_type
        assert r.headers["cache-control"] == "private, max-age=300"
        assert r.headers["x-robots-tag"] == "noindex" and r.headers["x-content-type-options"] == "nosniff"
        assert "set-cookie" not in r.headers


def test_a_photo_of_another_page_a_guess_and_an_expired_link_answer_the_same_404(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)
    _post(client, token, "key", _jpg())
    mine = _rows(conn, page_id)[0]["id"]
    _other_biz, other_page, other_token = _site(conn, monkeypatch, words=False, biz="biz_other")
    _post(client, other_token, "key", _jpg("o.jpg", 33))
    theirs = _rows(conn, other_page)[0]["id"]
    conn.execute("INSERT INTO business (id) VALUES ('biz_starter')")
    add_photo(conn, landing.ensure_page_for_kit(conn, "biz_starter")["id"], "ph_starter", jpeg(300, 200, 44))

    assert client.get(f"/u/{token}/photo/{mine}").status_code == 200
    answers = [
        client.get(f"/u/{token}/photo/{theirs}"),  # another site's photo, asked through this page's link
        client.get(f"/u/{token}/photo/ph_starter"),  # a starter page's, the same
        client.get(f"/u/{token}/photo/ph_doesnotexist"),
        client.get(f"/u/up_0000000000000000/photo/{mine}"),  # a guessed link
    ]
    conn.execute("UPDATE upload_token SET expires_at = now() - interval '1 minute' WHERE token = %s", (token,))
    answers.append(client.get(f"/u/{token}/photo/{mine}"))  # an expired one
    for r in answers:
        assert r.status_code == 404 and r.text == "Not found." and r.headers["x-robots-tag"] == "noindex"
        assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------- the rebuild (Q7, Q9)


def test_a_removal_on_a_site_page_deletes_the_photo_and_builds_the_site_again(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
):
    _biz, page_id, token = _site(conn, monkeypatch)
    assert _post(client, token, "logo", ("logo.png", logo_png(), "image/png")).status_code == 200
    assert _post(client, token, "key", _jpg()).status_code == 200
    key, logo = (_rows(conn, page_id, k)[0] for k in ("key", "logo"))
    before = _preview(conn, page_id)
    assert _document(conn, before)["images"]["hero"]["id"] == key["id"]

    r = client.post(f"/u/{token}", data={"action": "remove", "photo": key["id"]})

    assert r.status_code == 200, r.text
    assert [x["id"] for x in _rows(conn, page_id)] == [logo["id"]]
    after = _preview(conn, page_id)
    assert SEE_YOUR_SITE.search(r.text) and after["token"] != before["token"]
    document = _document(conn, after)
    assert "hero" not in document["images"] and document["images"]["logo"]["id"] == logo["id"]
    assert _count(conn, "site_spec") == 3  # one build for each of the three changes
    assert document["checklist"]["key_photos_ok"] is False and renderer.asked == []
    assert "1 of 1" in r.text and f"/photo/{key['id']}" not in r.text


def test_removing_what_is_not_there_or_is_not_a_kinds_photo_changes_nothing(client, conn, monkeypatch):
    _biz, page_id, token = _site(conn, monkeypatch)
    add_photo(conn, page_id, "ph_nokind", jpeg(900, 600, 77))  # a photo a starter page took: it says nothing of itself

    for photo in ("ph_doesnotexist", "ph_nokind", ""):
        r = client.post(f"/u/{token}", data={"action": "remove", "photo": photo})
        assert r.status_code == 200 and "Saved" not in r.text, photo
    assert [x["id"] for x in _rows(conn, page_id)] == ["ph_nokind"]  # still there
    assert _count(conn, "site_spec") == 0 and _count(conn, "page_preview") == 0  # and nothing was built for it


def test_a_photo_that_says_nothing_of_itself_is_never_placed_in_the_site(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
):
    """Review Focus: a starter page's photo (no kind) on a business that later got a site is not shown on the site's
    link, not removable through it, and not in any slot of any build."""
    _biz, page_id, token = _site(conn, monkeypatch)
    starter = add_photo(conn, page_id, "ph_nokind", jpeg(1000, 700, 77))
    assert _post(client, token, "key", _jpg("k.jpg", 150)).status_code == 200
    assert _post(client, token, "work", _jpg("w.jpg", 30)).status_code == 200

    page = client.get(f"/u/{token}").text
    assert "ph_nokind" not in page
    preview = _preview(conn, page_id)
    document = _document(conn, preview)
    assert "ph_nokind" not in {ref["id"] for ref in document["images"].values()}
    assert hashlib.sha256(starter).hexdigest() not in bundles.preview_files(conn, preview["token"]).values()
    assert document["images"]["hero"]["id"] != "ph_nokind" and document["images"]["gallery-1"]["source"] == "owner"


def test_a_site_never_built_saves_the_photo_and_waits_for_its_first_build(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
    caplog,
):
    _biz, page_id, token = _site(conn, monkeypatch, words=False)

    with caplog.at_level(logging.INFO):
        r = _post(client, token, "key", _jpg())
        assert photos.rebuild(page_id) is None  # nothing to rebuild from either

    assert r.status_code == 200 and '<p class="done">Saved.</p>' in r.text and "See your site" not in r.text
    assert [x["kind"] for x in _rows(conn, page_id)] == ["key"]
    assert _count(conn, "page_preview") == 0 and _count(conn, "site_spec") == 0 and renderer.asked == []
    assert not [rec for rec in caplog.records if rec.name == "agent.sites.photos"]  # not a failure: nothing is logged


def test_a_draft_whose_look_is_none_of_its_familys_is_not_rebuilt_and_the_page_says_saved(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
    caplog,
):
    """Task 5's follow-up: the business's kind changed after a look was picked (the draft says home services and a
    dental look): there is no look of its own to build in, so the photo is saved and joins the next build -- no
    rebuild, no failure to log, the same "Saved." a site never built says."""
    _biz, page_id, token = _site(conn, monkeypatch)
    conn.execute('UPDATE page SET site_draft = site_draft || \'{"style": "calm"}\' WHERE id = %s', (page_id,))

    with caplog.at_level(logging.INFO):
        r = _post(client, token, "key", _jpg())
        assert photos.rebuild(page_id) is None

    assert r.status_code == 200 and '<p class="done">Saved.</p>' in r.text and "See your site" not in r.text
    assert [x["kind"] for x in _rows(conn, page_id)] == ["key"]
    assert _count(conn, "page_preview") == 0 and _count(conn, "site_spec") == 0 and renderer.asked == []
    assert not [rec for rec in caplog.records if rec.name == "agent.sites.photos"]  # nothing went wrong: nothing logged
    conn.execute('UPDATE page SET site_draft = site_draft || \'{"style": "solid"}\' WHERE id = %s', (page_id,))
    assert photos.rebuild(page_id) is not None  # with the family's own look it builds as ever


def test_a_rebuild_that_fails_its_checks_keeps_the_photo_logs_and_says_it_shows_next_time(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    caplog,
):
    _biz, page_id, token = _site(conn, monkeypatch)
    real = bundles.check

    def refusing(judged, **kwargs):
        real(judged, **kwargs)
        judged.problems.append(bundles.problem("/", "stand_in", "The stand-in refuses every page.", "Nothing: a test."))

    monkeypatch.setattr(bundles, "check", refusing)

    with caplog.at_level(logging.ERROR, logger="agent.sites.photos"):
        r = _post(client, token, "key", _jpg())

    assert r.status_code == 200, r.text
    assert f'<p class="done">{SAVED_LATER}</p>' in r.text and "See your site" not in r.text
    assert [x["kind"] for x in _rows(conn, page_id)] == ["key"]  # the photo is kept
    logged = [rec for rec in caplog.records if rec.name == "agent.sites.photos" and rec.levelno == logging.ERROR]
    assert len(logged) == 1 and "stand_in" in logged[0].getMessage() and page_id in logged[0].getMessage()
    assert _count(conn, "page_preview") == 0  # a static-check problem keeps nothing: no half-built site stands
    assert "1 of 1" in r.text  # and the page shows it


def test_a_document_the_rebuild_cannot_read_is_the_same_as_a_failed_check(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    caplog,
):
    _biz, page_id, token = _site(conn, monkeypatch)
    conn.execute(
        "UPDATE page SET site_draft = jsonb_set(site_draft, '{words}', '{\"home\": 1}') WHERE id = %s", (page_id,)
    )

    with caplog.at_level(logging.ERROR, logger="agent.sites.photos"):
        r = _post(client, token, "key", _jpg())

    assert r.status_code == 200 and SAVED_LATER in r.text and len(_rows(conn, page_id)) == 1
    assert [rec.levelno for rec in caplog.records if rec.name == "agent.sites.photos"] == [logging.ERROR]


def test_a_rebuild_that_breaks_for_any_reason_is_never_a_500_and_keeps_the_photo(
    client,
    conn,
    monkeypatch,
    caplog,
):
    """Fix round 0: a rebuild is best effort. Whatever goes wrong in it -- here a build stand-in that raises a
    `RuntimeError`, which no check of the site's own would ever name -- the photo is saved and the owner reads that the
    site shows it next time, for a batch and for a removal alike; the failure is logged at ERROR with its traceback."""
    _biz, page_id, token = _site(conn, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("the build stand-in broke")

    monkeypatch.setattr(build, "preview", broken)

    with caplog.at_level(logging.ERROR, logger="agent.sites.photos"):
        added = _post(client, token, "key", _jpg())
        (row,) = _rows(conn, page_id)
        removed = client.post(f"/u/{token}", data={"action": "remove", "photo": row["id"]})
        assert photos.rebuild(page_id) is None  # and asked directly, it answers None, not an exception

    assert added.status_code == 200 and f'<p class="done">{SAVED_LATER}</p>' in added.text and "1 of 1" in added.text
    assert row["kind"] == "key"
    assert removed.status_code == 200 and f'<p class="done">{SAVED_LATER}</p>' in removed.text
    assert _rows(conn, page_id) == []  # the removal was made, whatever the build did
    logged = [rec for rec in caplog.records if rec.name == "agent.sites.photos" and rec.levelno == logging.ERROR]
    assert len(logged) == 3 and all(rec.exc_info and rec.exc_info[0] is RuntimeError for rec in logged)
    assert all(page_id in rec.getMessage() for rec in logged)


def test_hosting_off_is_a_failed_rebuild_like_any_other(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    caplog,
):
    _biz, page_id, token = _site(conn, monkeypatch)
    monkeypatch.delenv("AGENT_PAGES_DOMAIN")

    with caplog.at_level(logging.ERROR, logger="agent.sites.photos"):
        r = _post(client, token, "key", _jpg())

    assert r.status_code == 200 and f'<p class="done">{SAVED_LATER}</p>' in r.text and len(_rows(conn, page_id)) == 1
    assert [rec.levelno for rec in caplog.records if rec.name == "agent.sites.photos"] == [logging.ERROR]


def test_rebuild_answers_the_new_previews_address_and_nothing_without_a_page_or_words(
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
):
    _biz, page_id, _token = _site(conn, monkeypatch)
    add_photo(conn, page_id, "ph_logo", logo_png(), kind="logo", content_type="image/png")

    url = photos.rebuild(page_id)

    preview = _preview(conn, page_id)
    assert re.fullmatch(r"https://preview--[0-9a-f]{16}\.business\.test/", url)
    assert preview["sections"]["family"] == "home_services" and preview["sections"]["style"] == "solid"
    document = _document(conn, preview)
    assert document["images"]["logo"]["id"] == "ph_logo" and document["facts"]["name"] == "Oakline Roofing"
    assert document["words"] == samples.load("roofer", style="solid").words.model_dump(mode="json")
    assert document["checklist"]["wordmark_ok"] is False and document["checklist"]["key_photos_ok"] is False
    assert photos.rebuild("pg_doesnotexist") is None
    conn.execute("UPDATE page SET site_draft = site_draft || '{\"words\": null}' WHERE id = %s", (page_id,))
    builds = _count(conn, "site_spec")
    assert photos.rebuild(page_id) is None and _count(conn, "site_spec") == builds


def test_a_rebuild_holds_no_row_of_the_page_while_it_builds(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
):
    """The photos are committed before the build starts, and the build runs on a connection of its own: while it is
    stalled, a second connection takes the page row with `FOR UPDATE NOWAIT`, which raises at once if anyone holds
    it."""
    _biz, page_id, token = _site(conn, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    real = build.preview

    def slow(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(build, "preview", slow)
    outcome: dict = {}
    sender = threading.Thread(target=lambda: outcome.update(response=_post(client, token, "key", _jpg())))
    sender.start()
    try:
        assert entered.wait(10), "the upload never reached the build"
        with db.connect(TEST_DB_URL) as second:
            row = second.execute("SELECT id FROM page WHERE id = %s FOR UPDATE NOWAIT", (page_id,)).fetchone()
            assert row is not None
            assert (
                second.execute("SELECT count(*) AS n FROM page_photo WHERE page_id = %s", (page_id,)).fetchone()["n"]
                == 1
            )  # the photo is already committed
    finally:
        release.set()
        sender.join(20)

    assert outcome["response"].status_code == 200 and SEE_YOUR_SITE.search(outcome["response"].text)


# ---------------------------------------------------------------- _process, the one photo (Q7)


def test_process_keeps_a_logos_alpha_and_treats_every_other_photo_as_before():
    assert (uploads.MAX_LOGO_DIM, uploads.LOGO_TARGET_BYTES) == (800, 300 * 1024)
    alpha = logo_png(400, 100, 140)

    kept, w, h = uploads._process(alpha, logo=True)
    assert (w, h) == (400, 100) and kept[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(kept)).mode == "RGBA"
    flat, w, h = uploads._process(alpha)  # no `logo`: the call every photo makes
    assert (w, h) == (400, 100) and flat[:3] == b"\xff\xd8\xff"
    palette = Image.new("P", (40, 20), 0)  # every pixel the palette's first colour, which is the clear one
    buf = io.BytesIO()
    palette.save(buf, "PNG", transparency=0)  # a palette image with a clear colour is transparent too
    cleared, _w, _h = uploads._process(buf.getvalue(), logo=True)
    assert Image.open(io.BytesIO(cleared)).mode == "RGBA" and Image.open(io.BytesIO(cleared)).getpixel((0, 0))[3] == 0
    with pytest.raises(uploads._BadPhoto):
        uploads._process(b"GIF89a" + b"\x00" * 20, logo=True)


def test_a_logo_over_its_bytes_is_scaled_down_by_a_fifth_until_it_fits_or_is_no_more_than_200_px(monkeypatch):
    buf = io.BytesIO()
    Image.frombytes("RGB", (800, 600), os.urandom(800 * 600 * 3)).save(buf, "JPEG", quality=95)

    monkeypatch.setattr(uploads, "LOGO_TARGET_BYTES", 40 * 1024)
    fitted, w, h = uploads._process(buf.getvalue(), logo=True)
    assert len(fitted) <= 40 * 1024 and w in (640, 512, 410, 328, 262, 210) and abs(h / w - 0.75) < 0.01
    assert Image.open(io.BytesIO(fitted)).size == (w, h)

    monkeypatch.setattr(uploads, "LOGO_TARGET_BYTES", 100)  # nothing can be that small: it stops at the floor
    floor, w, h = uploads._process(buf.getvalue(), logo=True)
    assert 160 <= max(w, h) <= 200 and Image.open(io.BytesIO(floor)).size == (w, h)

    noisy_png = io.BytesIO()  # a transparent PNG goes the same way
    Image.frombytes("RGBA", (800, 400), os.urandom(800 * 400 * 4)).save(noisy_png, "PNG")
    png, w, h = uploads._process(noisy_png.getvalue(), logo=True)
    assert 160 <= max(w, h) <= 200 and Image.open(io.BytesIO(png)).mode == "RGBA"
