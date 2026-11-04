# agent/data — the geo catalogs

Two checked-in catalogs turn a business's point and radius into the ZIP codes its NewsBreak ads are aimed at, and
those into NewsBreak's location ids (`agent/geo.py`; plan `docs/superpowers/plans/2026-09-29-agent-m1-newsbreak.md`,
P1 and P2). `agent/Dockerfile` copies `agent/`, so both ship in the agent image; each stays under 1.5 MB
(`agent/tests/test_geo.py` guards it).

| File | Columns | Source | Retrieved | Rows |
| --- | --- | --- | --- | --- |
| `zcta_centroids.csv` | `zip,lat,lng` | US Census Bureau 2026 Gazetteer, ZIP Code Tabulation Areas (public domain): `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_zcta_national.zip`. Three of its columns: `GEOID` (the ZIP), `INTPTLAT`, `INTPTLONG` (the internal point) | 2026-10-01 | 33,791 |
| `newsbreak_locations.csv` | `zip,location_id` | NewsBreak's published location sheet, as a CSV export: `https://docs.google.com/spreadsheets/d/1Y_gc5N_tWfyo10h1u5A3lu1587Pamz1jndcELRBC34k/export?format=csv`. The rows with `Location_type` = `Zipcode`; the ZIP is the five digits in `Location` | 2026-10-01 | 42,618 |

The first line of each file is a comment, `# source: <url>; retrieved <YYYY-MM-DD>`; readers skip lines that start
with `#`. Rows are sorted by ZIP. A ZIP is text: five characters, leading zeros kept.

## Refresh

```bash
.venv/bin/python -m agent.ops geo-refresh [--census-url URL] [--sheet-url URL]
```

Downloads both sources (only public http(s) addresses, each redirect vetted; Google's export of the sheet redirects to
another host), rewrites both files here and prints their row counts. A source that cannot be fetched or is not the file
expected (a sign-in page, a renamed column) fails the command with both files left as they were. It needs no database.
Then update the table above (date and rows), run `agent/tests/test_geo.py` and commit the two files.

- The Census publishes a new Gazetteer each year: pass the new URL with `--census-url`, and move `CENSUS_URL` in
  `agent/geo.py` with it. The 2026 file is pipe-separated (the plan expected tab-separated); the refresh reads either.
- NewsBreak adds locations to its sheet over time: refresh when a ZIP you need comes back unmapped.

## What the catalogs do not cover

A ZCTA is the Census Bureau's stand-in for a ZIP, not the post office's list. As of 2026-10-01, 8,835 of NewsBreak's
ZIPs (post-office-box-only and single-address ZIPs) have no ZCTA, so no centroid and never a target; and 8 ZCTAs
(27268, 72405, 72713, 75036, 75072, 85191, 87151, 89437) have no NewsBreak id, which `geo.newsbreak_ids` returns as
unmapped. A ZIP is in range when its centroid is, not when its area is: a large rural ZIP whose boundary reaches the
radius but whose centroid does not is left out.
