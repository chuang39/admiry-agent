"""Where an ad can reach, in ZIP codes (plan 2026-09-29-agent-m1-newsbreak P1, P2). A point and a radius become the ZIP
codes whose centroid lies within the radius (`zips_within`), and those become NewsBreak location ids
(`newsbreak_ids`).

Two catalogs are checked in under agent/data. agent/Dockerfile copies agent/, so they ship in the image; each stays
under 1.5 MB, which a test guards:
  zcta_centroids.csv       zip,lat,lng       the internal point of every ZIP Code Tabulation Area (ZCTA, the Census
                                             Bureau's areal stand-in for a ZIP), from its Gazetteer file
  newsbreak_locations.csv  zip,location_id   the Zipcode rows of NewsBreak's published location sheet
The first line of each file is a `#` comment with its source URL and the day it was downloaded. `refresh`, the code
behind `python -m agent.ops geo-refresh`, rebuilds both from those public sources (agent/data/README.md).

Cost: nothing is read until first use; each catalog is then read once per process and kept (about 40 ms and 11 MB for
both). A `zips_within` call is one pass over the 33,791 ZCTAs (about 8 ms), with no I/O and no index."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from math import asin, cos, isfinite, radians, sin, sqrt
from pathlib import Path

import httpx

from . import crawl

MAX_ZIPS = 300
EARTH_MILES = 3958.8
DATA_DIR = Path(__file__).resolve().parent / "data"
ZCTA_FILE = "zcta_centroids.csv"
NEWSBREAK_FILE = "newsbreak_locations.csv"
CENSUS_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_zcta_national.zip"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1Y_gc5N_tWfyo10h1u5A3lu1587Pamz1jndcELRBC34k/export?format=csv"
TIMEOUT_S = 60.0
MAX_REDIRECTS = 5


class RefreshError(ValueError):
    """A source could not be fetched, is not a public address, or is not the file the catalog is built from."""


@dataclass(frozen=True)
class Zcta:
    zip: str
    lat: float
    lng: float


def _read(name: str) -> Iterator[dict[str, str]]:
    """The data rows of a catalog file, as dicts by column name; its `#` comment lines are skipped."""
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(line for line in f if not line.startswith("#"))


@cache
def zctas() -> list[Zcta]:
    return [Zcta(row["zip"], float(row["lat"]), float(row["lng"])) for row in _read(ZCTA_FILE)]


@cache
def _locations() -> dict[str, str]:
    return {row["zip"]: row["location_id"] for row in _read(NEWSBREAK_FILE)}


def miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance by the haversine formula."""
    p1, p2 = radians(lat1), radians(lat2)
    h = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lng2 - lng1) / 2) ** 2
    return 2 * EARTH_MILES * asin(sqrt(min(1.0, h)))


def _on_earth(lat: float, lng: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):  # false for NaN too
        raise ValueError(f"latitude must be within -90..90 and longitude within -180..180, got {lat}, {lng}")


def zips_within(lat: float, lng: float, radius_miles: float) -> list[str]:
    """The ZIP codes whose centroid lies within `radius_miles` of the point, nearest first: at least the ZIP whose
    centroid is nearest (however far it is), at most the MAX_ZIPS nearest. A point off the Earth is a ValueError."""
    _on_earth(lat, lng)
    away = [(miles(lat, lng, z.lat, z.lng), z.zip) for z in zctas()]
    inside = sorted(a for a in away if a[0] <= radius_miles)[:MAX_ZIPS]
    return [code for _, code in inside or [min(away)]]


def nearest(lat: float, lng: float) -> tuple[str, float]:
    """(the ZIP whose centroid is nearest the point, how many miles away that centroid is) -- what `zips_within` falls
    back on, with the distance it does not tell: a point outside the US still gets a ZIP, only a far one (M1 Task 5,
    NewsBreak's precheck). A point off the Earth is a ValueError."""
    _on_earth(lat, lng)
    away, code = min((miles(lat, lng, z.lat, z.lng), z.zip) for z in zctas())
    return code, away


def newsbreak_ids(zips: list[str]) -> tuple[list[str], list[str]]:
    """(NewsBreak's location ids for the ZIPs it lists, in the ZIPs' order; the ZIPs it has no id for)."""
    known = _locations()
    return [known[z] for z in zips if z in known], [z for z in zips if z not in known]


def client() -> httpx.Client:
    """What `refresh` downloads with when it is given none. It follows no redirect by itself: `_download` does."""
    return httpx.Client(timeout=TIMEOUT_S)


def _vet(url: str) -> None:
    try:
        crawl.public_host(url)
    except crawl.CrawlError as e:
        raise RefreshError(f"will not fetch {url}: not a public http(s) address") from e


def _download(http: httpx.Client, url: str) -> bytes:
    """The body at `url`. Redirects are followed here, not by httpx, so that every address is vetted before it is
    asked: Google answers the export of a sheet with a redirect to another host."""
    start = url
    for _ in range(MAX_REDIRECTS + 1):
        _vet(url)
        try:
            r = http.get(url, follow_redirects=False)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise RefreshError(f"could not download {url}: {e}") from e
        if r.next_request is not None:
            url = str(r.next_request.url)
            continue
        if r.status_code != 200:
            raise RefreshError(f"could not download {url}: HTTP {r.status_code}")
        return r.content
    raise RefreshError(f"could not download {start}: more than {MAX_REDIRECTS} redirects")


def _is_number(text: str) -> bool:
    try:
        return isfinite(float(text))
    except ValueError:
        return False


def _zcta_rows(blob: bytes) -> list[tuple[str, str, str]]:
    """(zip, lat, lng) of every row of the Gazetteer zip, sorted by ZIP. The 2026 file is pipe-separated where the
    plan expected tab-separated, so either is read; blanks around a column name or a value are ignored."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".txt")]
            if len(members) != 1:
                raise RefreshError(f"expected one .txt file in the Census zip, found {archive.namelist()}")
            lines = archive.read(members[0]).decode("utf-8-sig", errors="replace").splitlines()
    except zipfile.BadZipFile as e:
        raise RefreshError("the Census download is not a zip file") from e
    wanted = ("GEOID", "INTPTLAT", "INTPTLONG")
    for delimiter in ("|", "\t"):
        names = [n.strip() for n in (lines[0] if lines else "").split(delimiter)]
        if set(wanted) <= set(names):
            break
    else:
        raise RefreshError(f"the Census file has no {', '.join(wanted)} columns (found: {lines[:1]})")
    at = [names.index(n) for n in wanted]
    rows: dict[str, tuple[str, str]] = {}
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(delimiter)]
        if len(cells) <= max(at):
            continue
        code, lat, lng = (cells[i] for i in at)
        if re.fullmatch(r"\d{5}", code) and _is_number(lat) and _is_number(lng):
            rows.setdefault(code, (lat, lng))
    if not rows:
        raise RefreshError("the Census file has no ZCTA rows")
    return [(code, lat, lng) for code, (lat, lng) in sorted(rows.items())]


def _newsbreak_rows(blob: bytes) -> list[tuple[str, str]]:
    """(zip, location id) of every Zipcode row of NewsBreak's sheet, sorted by ZIP; the ZIP is the five digits in the
    row's Location (a blank and quotes around `94301,  United States`), and a ZIP listed twice keeps its first id."""
    wanted = ("Location_id", "Location_type", "Location")
    ids: dict[str, str] = {}
    try:
        reader = csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace"), newline=""))
        if not set(wanted) <= set(reader.fieldnames or []):
            raise RefreshError(f"NewsBreak's sheet has no {', '.join(wanted)} columns (found: {reader.fieldnames})")
        for row in reader:
            found = re.search(r"(?<!\d)\d{5}(?!\d)", row["Location"] or "")
            location_id = (row["Location_id"] or "").strip()
            if (row["Location_type"] or "").strip() == "Zipcode" and found and location_id:
                ids.setdefault(found.group(), location_id)
    except csv.Error as e:
        raise RefreshError(f"NewsBreak's sheet is not a CSV file: {e}") from e
    if not ids:
        raise RefreshError("NewsBreak's sheet has no Zipcode rows")
    return sorted(ids.items())


def _write(path: Path, source: str, retrieved: str, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """The catalog: a comment naming the source and the day, the header, the rows. Written beside the old file and
    renamed over it."""
    scratch = path.with_name(path.name + ".tmp")
    with scratch.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# source: {source}; retrieved {retrieved}\n")
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    scratch.replace(path)


def refresh(*, census_url: str, sheet_url: str, out_dir: Path, http: httpx.Client | None = None) -> dict:
    """Rebuild both catalogs in `out_dir` from their sources and return {"zctas": n, "newsbreak_zips": m}. Both
    sources are read and parsed before either file is touched, so a failure (RefreshError) leaves the old files as
    they were. Only public http(s) addresses are fetched, each redirect included. `http` stays the caller's to
    close; this process's caches of the catalogs are not reset."""
    for url in (census_url, sheet_url):  # both vetted before any request: a refused second one costs no download
        _vet(url)
    session = client() if http is None else http
    try:
        zcta = _zcta_rows(_download(session, census_url))
        newsbreak = _newsbreak_rows(_download(session, sheet_url))
    finally:
        if http is None:
            session.close()
    today = datetime.now(UTC).date().isoformat()
    _write(out_dir / ZCTA_FILE, census_url, today, ("zip", "lat", "lng"), zcta)
    _write(out_dir / NEWSBREAK_FILE, sheet_url, today, ("zip", "location_id"), newsbreak)
    return {"zctas": len(zcta), "newsbreak_zips": len(newsbreak)}
