"""agent/geo.py and the two catalogs behind it (M1 Task 1; plan 2026-09-29-agent-m1-newsbreak P1, P2): a point and a
radius become ZIP codes (Census ZCTA centroids), and those become NewsBreak location ids. The lookups run on the
checked-in catalogs in agent/data; `refresh`, the code behind `python -m agent.ops geo-refresh`, runs here over a
MockTransport and a name service answered from a dict, so no test reaches the network."""

from __future__ import annotations

import csv
import io
import itertools
import math
import re
import socket
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agent import geo, ops

CENSUS = "https://census.example/gazetteer/zcta.zip"
SHEET = "https://sheets.example/spreadsheets/d/abc/export?format=csv"
SHEET_FILE = "https://files.example/export/abc/sheet.csv"  # where the sheet's export hands over to, as Google's does
CENSUS_DEFAULT = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_zcta_national.zip"
SHEET_DEFAULT = "https://docs.google.com/spreadsheets/d/1Y_gc5N_tWfyo10h1u5A3lu1587Pamz1jndcELRBC34k/export?format=csv"
PUBLIC_IP = "93.184.216.34"
ZCTA_FILE = "zcta_centroids.csv"
NEWSBREAK_FILE = "newsbreak_locations.csv"

# The 2026 Gazetteer as the Census publishes it (rows taken from the real file, out of order): pipe-separated, eight
# columns. The plan expected a tab-separated file, so the same table with tabs, and blanks after the last column's
# name and the last value, is read too.
GAZETTEER = (
    "GEOID|GEOIDFQ|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n"
    "99929|860Z200US99929|6251756304|857459558|2413.817|331.067|56.221499|-131.923588\n"
    "94306|860Z200US94306|10581311|0|4.085|0.|37.4162|-122.130858\n"
    "00601|860Z200US00601|166744424|795116|64.38|0.307|18.180621|-66.749931\n"
    "94301|860Z200US94301|6138865|0|2.37|0.|37.444218|-122.149855\n"
)
GAZETTEER_TAB = (
    GAZETTEER.replace("|", "\t").replace("INTPTLONG\n", "INTPTLONG     \n").replace("-122.149855\n", "-122.149855 \n")
)
ZCTA_ROWS = [
    "zip,lat,lng",
    "00601,18.180621,-66.749931",
    "94301,37.444218,-122.149855",
    "94306,37.4162,-122.130858",
    "99929,56.221499,-131.923588",
]

# NewsBreak's location sheet as Google exports it: the Location column is quoted with a leading blank and quotes of
# its own, " ""94301,  United States""", and the rows of every type are mixed.
SHEET_CSV = (
    "Location_id,Location_type,Location\n"
    '1499033616,State," ""Cayey,  United States"""\n'
    '1196946380,Zipcode," ""94301,  United States"""\n'
    '1261397901,DMA," ""DMA: 807 - San Francisco-Oak-San Jose,  United States"""\n'
    '1872265480,Zipcode," ""94306,  United States"""\n'
    '1547858659,City," ""Palo Alto,  California,  United States"""\n'
    '1415289159,Zipcode," ""00601,  United States"""\n'
    '1352482379,Zipcode," ""10001,  United States"""\n'
)
NEWSBREAK_ROWS = [
    "zip,location_id",
    "00601,1415289159",
    "10001,1352482379",
    "94301,1196946380",
    "94306,1872265480",
]

OLD = {
    ZCTA_FILE: "# source: an older download\nzip,lat,lng\n94301,1.0,2.0\n",
    NEWSBREAK_FILE: "# source: an older download\nzip,location_id\n94301,1\n",
}


@pytest.fixture
def dns(monkeypatch):
    """The real resolver (crawl.resolve) over a name service that answers from this dict: a public address for every
    name, unless the test sends the name somewhere else."""
    answers: dict[str, str] = {}

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers.get(host, PUBLIC_IP), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return answers


@pytest.fixture
def no_database(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("geo-refresh needs no database")

    monkeypatch.setattr(ops.db, "connect", refuse)


def census_zip(text: str, member: str = "2026_Gaz_zcta_national.txt") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, text)
    return buf.getvalue()


def routes(census: str = CENSUS, sheet: str = SHEET, *, gazetteer: str = GAZETTEER) -> dict[str, httpx.Response]:
    """The two sources as a web server answers them; the sheet's export is a 307 to the file itself."""
    return {
        census: httpx.Response(200, content=census_zip(gazetteer), headers={"content-type": "application/zip"}),
        sheet: httpx.Response(307, headers={"location": SHEET_FILE}),
        SHEET_FILE: httpx.Response(200, text=SHEET_CSV, headers={"content-type": "text/csv"}),
    }


def web(answers: dict[str, httpx.Response | Callable]) -> tuple[httpx.Client, list[str]]:
    """A client whose transport answers from `answers` by URL (a Response, or a function of the request that makes
    one; anything else is a 404) and records every URL asked."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        reply = answers.get(str(request.url), httpx.Response(404))
        return reply(request) if callable(reply) else reply

    return httpx.Client(transport=httpx.MockTransport(handler)), asked


def read(path: Path) -> tuple[list[str], list[str]]:
    """(the `#` lines at the top of a catalog file, the lines after them)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in lines if line.startswith("#")]
    assert lines[: len(comments)] == comments, "comment lines belong at the top of the file"
    return comments, lines[len(comments) :]


def names_source(comments: list[str], url_pattern: str) -> bool:
    """Whether a file's comment lines are exactly one, naming the source (a regex) and the day it was downloaded."""
    return (
        len(comments) == 1
        and re.fullmatch(rf"# source: {url_pattern}; retrieved \d{{4}}-\d{{2}}-\d{{2}}", comments[0]) is not None
    )


def seed(directory: Path) -> None:
    for name, text in OLD.items():
        (directory / name).write_text(text, encoding="utf-8")


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle miles by the spherical law of cosines: not the haversine geo.py uses, so the two cannot share a
    mistake. The Earth's mean radius is 3958.8 miles."""
    p1, l1, p2, l2 = map(math.radians, (*a, *b))
    cosine = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(l2 - l1)
    return 3958.8 * math.acos(max(-1.0, min(1.0, cosine)))


# --- refresh: the two sources become the two catalogs ------------------------------------------------------------


@pytest.mark.parametrize("gazetteer", [GAZETTEER, GAZETTEER_TAB], ids=["pipe-separated", "tab-separated"])
def test_refresh_rewrites_both_catalogs_from_the_two_sources(dns, tmp_path, gazetteer):
    seed(tmp_path)
    http, asked = web(routes(gazetteer=gazetteer))

    counts = geo.refresh(census_url=CENSUS, sheet_url=SHEET, out_dir=tmp_path, http=http)

    assert counts == {"zctas": 4, "newsbreak_zips": 4}
    assert asked == [CENSUS, SHEET, SHEET_FILE]  # the sheet's export is followed to the file it hands over to
    comments, rows = read(tmp_path / ZCTA_FILE)
    assert rows == ZCTA_ROWS  # three columns, sorted by ZIP, the leading zeros and the sources' digits kept
    assert names_source(comments, re.escape(CENSUS))
    comments, rows = read(tmp_path / NEWSBREAK_FILE)
    assert rows == NEWSBREAK_ROWS  # Zipcode rows only; the five digits of Location become the ZIP
    assert names_source(comments, re.escape(SHEET))
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(OLD)  # no half-written file left behind


def test_refresh_takes_zip_codes_from_zipcode_rows_only(dns, tmp_path):
    sheet = (
        "Location_id,Location_type,Location\n"
        '1161616161,City," ""Zip 12345 Springs,  United States"""\n'  # five digits, but not a Zipcode row
        '1222222222,Zipcode," ""United States"""\n'  # a Zipcode row without any
        '1196946380,Zipcode," ""94301,  United States"""\n'
        '9999999999,Zipcode," ""94301,  United States"""\n'  # the same ZIP again: the first id stays
    )
    answers = routes()
    answers[SHEET_FILE] = httpx.Response(200, text=sheet)
    http, _ = web(answers)

    counts = geo.refresh(census_url=CENSUS, sheet_url=SHEET, out_dir=tmp_path, http=http)

    assert counts["newsbreak_zips"] == 1
    assert read(tmp_path / NEWSBREAK_FILE)[1] == ["zip,location_id", "94301,1196946380"]


def test_refresh_gives_up_on_a_redirect_loop(dns, tmp_path):
    answers = routes()
    answers[SHEET] = lambda request: httpx.Response(307, headers={"location": SHEET})
    http, asked = web(answers)

    with pytest.raises(geo.RefreshError, match="redirect"):
        geo.refresh(census_url=CENSUS, sheet_url=SHEET, out_dir=tmp_path, http=http)

    assert len(asked) <= 10
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("census_url", "sheet_url"),
    [
        ("http://127.0.0.1/zcta.zip", SHEET),
        ("http://localhost/zcta.zip", SHEET),
        (CENSUS, "http://10.0.0.5/sheet.csv"),
        ("http://intranet.example/zcta.zip", SHEET),  # a name that resolves to a private address
        ("file:///etc/passwd", SHEET),
    ],
)
def test_refresh_refuses_a_source_that_is_not_a_public_host_before_fetching_anything(
    dns, tmp_path, census_url, sheet_url
):
    dns["intranet.example"] = "10.1.2.3"
    http, asked = web(routes())

    with pytest.raises(geo.RefreshError, match="public"):
        geo.refresh(census_url=census_url, sheet_url=sheet_url, out_dir=tmp_path, http=http)

    assert asked == []  # both addresses are vetted first: a refused second one does not cost the first download
    assert list(tmp_path.iterdir()) == []


def test_refresh_refuses_a_redirect_to_a_non_public_host(dns, tmp_path):
    metadata = "http://169.254.169.254/latest/meta-data/"
    answers = routes()
    answers[SHEET] = httpx.Response(307, headers={"location": metadata})
    http, asked = web(answers)

    with pytest.raises(geo.RefreshError, match="public"):
        geo.refresh(census_url=CENSUS, sheet_url=SHEET, out_dir=tmp_path, http=http)

    assert metadata not in asked
    assert list(tmp_path.iterdir()) == []


def not_a_zip(answers):
    answers[CENSUS] = httpx.Response(200, content=b"<html>Access denied</html>")


def no_latitude(answers):
    answers[CENSUS] = httpx.Response(200, content=census_zip(GAZETTEER.replace("INTPTLAT", "LATITUDE")))


def no_zcta_rows(answers):
    answers[CENSUS] = httpx.Response(200, content=census_zip(GAZETTEER.splitlines()[0] + "\n"))


def sign_in_page(answers):
    answers[SHEET_FILE] = httpx.Response(200, text="<!doctype html><title>Sign in</title>")


def zipcode_type_renamed(answers):
    answers[SHEET_FILE] = httpx.Response(200, text=SHEET_CSV.replace(",Zipcode,", ",Postal,"))


@pytest.mark.parametrize(
    "break_source",
    [
        lambda answers: answers.update({CENSUS: httpx.Response(404)}),
        lambda answers: answers.update({SHEET_FILE: httpx.Response(500)}),
        not_a_zip,
        no_latitude,
        no_zcta_rows,
        sign_in_page,
        zipcode_type_renamed,
    ],
    ids=[
        "census 404",
        "sheet 500",
        "census not a zip",
        "census without latitude",
        "census without rows",
        "sheet sign-in page",
        "sheet without Zipcode rows",
    ],
)
def test_a_refresh_that_cannot_use_a_source_leaves_both_catalogs_as_they_were(dns, tmp_path, break_source):
    seed(tmp_path)
    answers = routes()
    break_source(answers)
    http, _ = web(answers)

    with pytest.raises(geo.RefreshError):
        geo.refresh(census_url=CENSUS, sheet_url=SHEET, out_dir=tmp_path, http=http)

    assert {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()} == OLD


# --- agent.ops geo-refresh ---------------------------------------------------------------------------------------


def test_geo_refresh_command_rewrites_the_catalogs_from_the_documented_sources(
    dns, no_database, tmp_path, monkeypatch, capsys
):
    http, asked = web(routes(CENSUS_DEFAULT, SHEET_DEFAULT))
    monkeypatch.setattr(geo, "client", lambda: http)
    monkeypatch.setattr(geo, "DATA_DIR", tmp_path)

    assert ops.main(["geo-refresh"]) == 0

    assert asked == [CENSUS_DEFAULT, SHEET_DEFAULT, SHEET_FILE]
    assert read(tmp_path / ZCTA_FILE)[1] == ZCTA_ROWS and read(tmp_path / NEWSBREAK_FILE)[1] == NEWSBREAK_ROWS
    assert capsys.readouterr().out.splitlines() == [f"{ZCTA_FILE}: 4 rows", f"{NEWSBREAK_FILE}: 4 rows"]


def test_geo_refresh_command_takes_other_sources_from_its_flags(dns, no_database, tmp_path, monkeypatch):
    census, sheet = "https://mirror.example/zcta.zip", "https://mirror.example/sheet?format=csv"
    http, asked = web(routes(census, sheet))
    monkeypatch.setattr(geo, "client", lambda: http)
    monkeypatch.setattr(geo, "DATA_DIR", tmp_path)

    assert ops.main(["geo-refresh", "--census-url", census, "--sheet-url", sheet]) == 0

    assert asked == [census, sheet, SHEET_FILE]
    assert read(tmp_path / ZCTA_FILE)[1] == ZCTA_ROWS


def test_geo_refresh_command_says_what_it_refused_and_exits_1(dns, no_database, tmp_path, monkeypatch, capsys):
    http, asked = web(routes(CENSUS_DEFAULT, SHEET_DEFAULT))
    monkeypatch.setattr(geo, "client", lambda: http)
    monkeypatch.setattr(geo, "DATA_DIR", tmp_path)

    assert ops.main(["geo-refresh", "--census-url", "http://127.0.0.1/zcta.zip"]) == 1

    captured = capsys.readouterr()
    assert "http://127.0.0.1/zcta.zip" in captured.err and captured.out == ""
    assert asked == [] and list(tmp_path.iterdir()) == []


# --- the checked-in catalogs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "header", "rows"),
    [(ZCTA_FILE, ["zip", "lat", "lng"], 30_000), (NEWSBREAK_FILE, ["zip", "location_id"], 40_000)],
)
def test_a_checked_in_catalog_has_its_source_comment_its_header_and_its_rows(name, header, rows):
    comments, lines = read(geo.DATA_DIR / name)
    assert names_source(comments, r"https://\S+")
    table = list(csv.reader(lines))
    assert table[0] == header
    assert len(table) - 1 >= rows
    assert all(len(row) == len(header) and re.fullmatch(r"\d{5}", row[0]) for row in table[1:])
    assert len({row[0] for row in table[1:]}) == len(table) - 1  # each ZIP once


@pytest.mark.parametrize("name", [ZCTA_FILE, NEWSBREAK_FILE])
def test_a_checked_in_catalog_stays_under_1_5_mb_because_it_ships_in_the_agent_image(name):
    assert (geo.DATA_DIR / name).stat().st_size <= 1_500_000


def test_zctas_returns_the_catalog_rows_and_reads_them_once():
    rows = geo.zctas()
    assert rows is geo.zctas()  # one read per process, not one per lookup
    assert len(rows) >= 30_000
    assert all(-90 <= z.lat <= 90 and -180 <= z.lng <= 180 for z in rows)
    by_zip = {z.zip: z for z in rows}
    assert (by_zip["94301"].lat, by_zip["94301"].lng) == pytest.approx((37.44, -122.15), abs=0.05)
    assert (by_zip["00601"].lat, by_zip["00601"].lng) == pytest.approx((18.18, -66.75), abs=0.05)  # leading zeros kept


# --- zips_within ---------------------------------------------------------------------------------------------------

PALO_ALTO = (37.4419, -122.143)


def test_zips_within_five_miles_of_palo_alto_has_its_neighbours_and_not_san_jose():
    zips = geo.zips_within(*PALO_ALTO, 5)
    assert "94301" in zips and "94306" in zips
    assert "95112" not in zips


def test_zips_within_lists_the_zips_whose_centroid_is_in_range_nearest_first():
    zips = geo.zips_within(*PALO_ALTO, 4)
    assert set(zips) == {"94301", "94303", "94306", "94305", "94025", "94304", "94027"}
    assert zips[0] == "94301"
    centroids = {z.zip: (z.lat, z.lng) for z in geo.zctas()}
    away = [miles(PALO_ALTO, centroids[z]) for z in zips]
    assert all(a <= b + 1e-6 for a, b in itertools.pairwise(away)) and max(away) <= 4


def test_the_radius_is_in_statute_miles():
    centroid = next((z.lat, z.lng) for z in geo.zctas() if z.zip == "94306")
    away = miles(PALO_ALTO, centroid)
    assert "94306" in geo.zips_within(*PALO_ALTO, away + 0.05)
    assert "94306" not in geo.zips_within(*PALO_ALTO, away - 0.05)


@pytest.mark.parametrize(
    ("point", "radius", "nearest"),
    [(PALO_ALTO, 0.1, "94301"), (PALO_ALTO, 0, "94301"), ((39.5, -116.8), 5, "89310")],
    ids=["a tenth of a mile", "no radius at all", "empty desert"],
)
def test_zips_within_returns_the_nearest_zip_even_when_no_centroid_is_in_range(point, radius, nearest):
    assert geo.zips_within(*point, radius) == [nearest]


def test_zips_within_keeps_the_nearest_max_zips_of_a_wide_radius():
    new_york = (40.7128, -74.0060)
    zips = geo.zips_within(*new_york, 100)
    assert len(zips) == geo.MAX_ZIPS and len(set(zips)) == geo.MAX_ZIPS
    centroids = {z.zip: (z.lat, z.lng) for z in geo.zctas()}
    kept = [miles(new_york, centroids[z]) for z in zips]
    left_out = [miles(new_york, c) for z, c in centroids.items() if z not in set(zips)]
    assert all(a <= b + 1e-6 for a, b in itertools.pairwise(kept))  # nearest first
    assert max(kept) <= min(left_out) + 1e-6  # and the 300 kept are the 300 nearest


@pytest.mark.parametrize(
    ("lat", "lng"),
    [(95.0, -122.1), (37.4, -222.1), (-122.1, 37.4), (float("nan"), -122.1), (37.4, float("inf"))],
    ids=["latitude over 90", "longitude under -180", "latitude and longitude swapped", "not a number", "infinite"],
)
def test_zips_within_refuses_a_point_that_is_not_on_the_earth(lat, lng):
    with pytest.raises(ValueError, match="latitude"):
        geo.zips_within(lat, lng, 5)


# --- nearest (M1 Task 5a, R9: the far point NewsBreak's precheck refuses) -------------------------------------------


def test_nearest_is_the_zcta_with_the_closest_centroid_and_its_distance_in_miles():
    code, away = geo.nearest(*PALO_ALTO)
    assert code == "94301" == geo.zips_within(*PALO_ALTO, 0)[0]
    centroid = next((z.lat, z.lng) for z in geo.zctas() if z.zip == code)
    assert away == pytest.approx(miles(PALO_ALTO, centroid), abs=1e-6) and away < 1


@pytest.mark.parametrize(
    ("point", "code", "low", "high"),
    [((43.6532, -79.3832), "14174", 30, 40), ((19.4326, -99.1332), "78520", 440, 480)],
    ids=["Toronto", "Mexico City"],
)
def test_nearest_measures_how_far_a_point_outside_the_us_is_from_any_zip(point, code, low, high):
    """Task 1's concern: `zips_within` hands such a point the nearest ZIP however far it is; `nearest` says how far."""
    found, away = geo.nearest(*point)
    assert found == code and low < away < high
    assert geo.zips_within(*point, 5) == [found]


@pytest.mark.parametrize(
    ("lat", "lng"),
    [(95.0, -122.1), (-122.1, 37.4), (float("nan"), -122.1)],
    ids=["latitude over 90", "latitude and longitude swapped", "not a number"],
)
def test_nearest_refuses_a_point_that_is_not_on_the_earth(lat, lng):
    with pytest.raises(ValueError, match="latitude"):
        geo.nearest(lat, lng)


# --- newsbreak_ids -------------------------------------------------------------------------------------------------


def test_newsbreak_ids_gives_an_id_for_a_listed_zip_and_reports_one_it_does_not_list():
    ids, unmapped = geo.newsbreak_ids(["94301", "00000"])
    assert len(ids) == 1 and ids[0].isdigit()
    assert unmapped == ["00000"]


def test_newsbreak_ids_come_in_the_order_of_the_zips():
    forward, _ = geo.newsbreak_ids(["94301", "94306", "10001"])
    assert len(set(forward)) == 3
    backward, _ = geo.newsbreak_ids(["10001", "94306", "94301"])
    assert backward == forward[::-1]
    mixed, unmapped = geo.newsbreak_ids(["94306", "00000", "94301", "abcde"])
    assert (mixed, unmapped) == ([forward[1], forward[0]], ["00000", "abcde"])


def test_nearly_every_zcta_has_a_newsbreak_location_id():
    """The catalogs must spell a ZIP the same way. Were one to lose the leading zero, every ZIP of New England, New
    Jersey and Puerto Rico (about 5 in 100) would go unmapped."""
    zips = [z.zip for z in geo.zctas()]
    assert sum(z.startswith("0") for z in zips) > 1000
    ids, unmapped = geo.newsbreak_ids(zips)
    assert len(ids) + len(unmapped) == len(zips)
    assert len(unmapped) <= 0.01 * len(zips)
