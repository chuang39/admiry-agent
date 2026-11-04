"""The looks an owner picks an industry site by (A3p-6 batch 2 Task 4; plan
2026-10-06-agent-a3p-6-2-facts-photos-publish Q5, Q12; the founder's ruling 1 of 2026-10-06): each family's three styles
with their names and lines, each shown by its sample site's phone screenshot -- six small JPEGs under
agent/static/sites/looks/, served at /static/sites/looks/ -- and `python -m agent.ops site-looks`, which writes the six
again from a business's gallery preview."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from agent import config, ops
from agent.sites import families, gallery, looks
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON
from agent.tests.test_ops_sites import _biz, libraries, renderer  # noqa: F401 (fixtures)
from agent.tests.test_render_check import PHONE

LOOKS_DIR = Path(__file__).resolve().parents[1] / "static" / "sites" / "looks"
NAMES = ["roofer-solid", "roofer-clean", "roofer-bold", "dentist-calm", "dentist-warm", "dentist-boutique"]
BASE = "http://testserver/static/sites/looks"
# Q5's lines (Q12's table: the founder's to approve), Warm's and Boutique's as the pictures show them (fix round 1)
LINES = {
    "solid": "Navy and white, an orange button, your towns and hours up top.",
    "clean": "White and soft green, round buttons, your numbers in a row.",
    "bold": "Dark and yellow, big headlines, straight to the problem.",
    "calm": "Soft teal, gentle type, new patients welcomed up front.",
    "warm": "Peach and terracotta, round and friendly.",  # fix round 1: the lines say what the pictures show
    "boutique": "Deep green and gold, a serif wordmark, understated.",
}


# ---------------------------------------------------------------- the six files


# a home-services trade's own looks (2026-10-08: site-gallery --samples hvac,plumber,electrician on the dev box, then
# site-looks), beside the reviewed six
TRADE_NAMES = [f"{s}-{style}" for s in ("hvac", "plumber", "electrician") for style in ("solid", "clean", "bold")]


def test_the_looks_are_small_jpegs_the_width_of_a_phone():
    assert sorted(p.name for p in LOOKS_DIR.iterdir()) == sorted(f"{name}.jpg" for name in NAMES + TRADE_NAMES)
    for name in NAMES + TRADE_NAMES:
        data = (LOOKS_DIR / f"{name}.jpg").read_bytes()
        assert data[:3] == b"\xff\xd8\xff", name
        assert len(data) <= 100 * 1024, (name, len(data))
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "JPEG" and img.width == 390, (name, img.size)


def test_a_look_is_served_as_a_jpeg_at_its_address(client):
    r = client.get("/static/sites/looks/roofer-solid.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == (LOOKS_DIR / "roofer-solid.jpg").read_bytes()
    assert client.get("/static/sites/looks/dentist-boutique.jpg").status_code == 200


# ---------------------------------------------------------------- the looks of a family


def test_each_familys_looks_are_its_three_styles_with_q5s_lines_and_their_pictures():
    settings = config.load()
    assert looks.looks(settings, "home_services") == [
        {"id": style, "name": style.capitalize(), "line": LINES[style], "look_url": f"{BASE}/roofer-{style}.jpg"}
        for style in ("solid", "clean", "bold")
    ]
    assert looks.looks(settings, "dental") == [
        {"id": style, "name": style.capitalize(), "line": LINES[style], "look_url": f"{BASE}/dentist-{style}.jpg"}
        for style in ("calm", "warm", "boutique")
    ]
    assert looks.look_url(settings, "dental", "warm") == f"{BASE}/dentist-warm.jpg"
    assert looks.name("home_services", "bold") == "Bold"
    for family in families.FAMILIES:  # a look for every style the family has, in its own order, and no other
        assert [look["id"] for look in looks.LOOKS[family]] == list(families.get(family).styles)
    for look in (look for family in looks.LOOKS.values() for look in family):
        for words in (look["name"], look["line"]):
            assert not (JARGON.search(words) or ACCOUNT_JARGON.search(words) or MONEY_JARGON.search(words)), words


def test_a_look_picked_under_another_family_is_none_of_this_familys():
    """Task 5's follow-up: `own_style` is the one reading `build_site` and the photos' rebuild share of a draft's look."""
    assert looks.own_style("home_services", "bold") == "bold" and looks.own_style("dental", "boutique") == "boutique"
    for family, style in (("home_services", "calm"), ("dental", "solid"), ("dental", "Calm"), ("dental", None)):
        assert looks.own_style(family, style) is None, (family, style)
    assert looks.own_style("attorneys", "solid") is None  # a family that has no looks at all: none, never a KeyError


def test_a_look_is_pictured_on_the_web_host_where_the_owner_reads_it(monkeypatch):
    """The pictures are the homepage's static files: on the browser host (AGENT_WEB_BASE_URL), not the agent's."""
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://admiry.test/")
    settings = config.load()
    assert (
        looks.look_url(settings, "home_services", "solid") == "https://admiry.test/static/sites/looks/roofer-solid.jpg"
    )


# ---------------------------------------------------------------- python -m agent.ops site-looks


def test_site_looks_writes_the_six_from_the_businesss_gallery(
    conn,
    monkeypatch,
    libraries,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
    tmp_path,
    capsys,
):
    biz = _biz(conn, monkeypatch)
    assert ops.main(["site-gallery", biz]) == 0
    capsys.readouterr()
    out = tmp_path / "looks"
    assert ops.main(["site-looks", biz, "--out", str(out)]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert printed == [str(out / f"{name}.jpg") for name in gallery.names()] and gallery.names() == NAMES
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{name}.jpg" for name in NAMES)
    for name in NAMES:
        data = (out / f"{name}.jpg").read_bytes()
        assert data[:3] == b"\xff\xd8\xff" and data != PHONE  # the renderer's phone shot, encoded again
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "JPEG" and img.size == Image.open(io.BytesIO(PHONE)).size


def test_site_looks_without_a_gallery_writes_nothing_and_exits_1(
    conn,
    monkeypatch,
    libraries,  # noqa: F811
    renderer,  # noqa: F811
    tmp_path,
    capsys,
):
    biz = _biz(conn, monkeypatch)
    out = tmp_path / "looks"
    assert ops.main(["site-looks", biz, "--out", str(out)]) == 1
    assert "no gallery" in capsys.readouterr().err and not out.exists()
    # a site preview is not a gallery either
    assert ops.main(["site-preview", biz, "--sample", "roofer", "--style", "solid"]) == 0
    capsys.readouterr()
    assert ops.main(["site-looks", biz, "--out", str(out)]) == 1
    assert "no gallery" in capsys.readouterr().err and not out.exists()


def test_site_looks_writes_nothing_when_a_screenshot_is_missing(
    conn,
    monkeypatch,
    libraries,  # noqa: F811
    tmp_path,
    capsys,
):
    """A gallery built with no render check set up has no screenshots: nothing is written, and the command says so."""
    biz = _biz(conn, monkeypatch)
    assert ops.main(["site-gallery", biz]) == 0
    capsys.readouterr()
    out = tmp_path / "looks"
    assert ops.main(["site-looks", biz, "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "roofer-solid" in err and "dentist-boutique" in err and not out.exists()


# ---------------------------------------------------------------- the looks of a trade (2026-10-08)
# The founder's dev walk, 2026-10-08: an AC contractor picked its look from a roofer's screenshots. A home-services trade
# with its own sample is pictured by it -- once its three pictures are shipped; the family's own sample until then.
TRADE_SAMPLES = {"hvac": "hvac", "plumbing": "plumber", "electrical": "electrician", "roofing": "roofer"}


def test_a_trades_looks_are_its_own_samples_once_its_pictures_are_shipped(monkeypatch, tmp_path):
    settings = config.load()
    monkeypatch.setattr(looks, "DIR", tmp_path)
    for style in ("solid", "clean", "bold"):
        (tmp_path / f"hvac-{style}.jpg").write_bytes(b"\xff\xd8\xff")
    assert [lk["look_url"] for lk in looks.looks(settings, "home_services", "hvac")] == [
        f"{BASE}/hvac-{style}.jpg" for style in ("solid", "clean", "bold")
    ]
    roofer = [f"{BASE}/roofer-{style}.jpg" for style in ("solid", "clean", "bold")]
    assert [lk["look_url"] for lk in looks.looks(settings, "home_services", "plumbing")] == roofer  # not shipped yet
    assert [lk["look_url"] for lk in looks.looks(settings, "home_services", None)] == roofer
    assert [lk["look_url"] for lk in looks.looks(settings, "home_services", "landscaping")] == roofer
    assert looks.looks(settings, "dental", "dentist") == looks.looks(settings, "dental")


def test_every_home_services_trade_has_a_sample_of_its_family():
    from agent.sites import samples
    from agent.sites.families import home_services

    assert set(looks.TRADE_SAMPLE) == set(home_services.TRADES) and looks.TRADE_SAMPLE == TRADE_SAMPLES
    for trade, sample in looks.TRADE_SAMPLE.items():
        s = samples.load(sample, style="solid")
        assert (s.family, s.trade) == ("home_services", trade), sample


def test_the_gallery_builds_the_samples_asked_for_and_the_reviewed_two_by_default():
    assert gallery.names() == NAMES
    assert gallery.names(("hvac", "plumber")) == [
        f"{s}-{style}" for s in ("hvac", "plumber") for style in ("solid", "clean", "bold")
    ]
    for name in gallery.names(("hvac", "plumber", "electrician")):
        assert gallery.LINES[name] == gallery.LINES[f"roofer-{name.split('-', 1)[1]}"]  # a style is a style
    import pytest

    with pytest.raises(ValueError, match="no sample called"):
        gallery.names(("florist",))


def test_site_looks_writes_the_looks_of_the_gallery_it_reads(
    conn,
    monkeypatch,
    libraries,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
    tmp_path,
    capsys,
):
    biz = _biz(conn, monkeypatch)
    assert ops.main(["site-gallery", biz, "--samples", "hvac"]) == 0
    capsys.readouterr()
    out = tmp_path / "looks"
    assert ops.main(["site-looks", biz, "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["hvac-bold.jpg", "hvac-clean.jpg", "hvac-solid.jpg"]
