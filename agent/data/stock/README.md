# Stock photo library for the industry site templates

Free-license stock photos for Admiry's industry website templates (A3p-6), so a site preview has a
photo in every slot before the owner uploads their own. The founder decided on 2026-10-05: "free
licenses first, download a batch in advance". This is that first batch, downloaded and reviewed on
2026-10-05: **60 photos, 36 for `home_services` (roofing, plumbing, HVAC, electrical) and 24 for
`dental`**. 55 come from Unsplash and 5 from Pexels.

```
agent/data/stock/
  manifest.json                  the record: one entry per photo
  files/<family>/<slot>-<nn>.jpg the image files (not in git)
```

## Slots

| family | slot | count | what it holds |
|---|---|---|---|
| home_services | `hero` | 5 | wide, calm, room for text: roofs against the sky, two house exteriors, a light switch on a bare wall |
| home_services | `service-roofing` | 8 | shingles, metal roofs, roof lines, a gutter |
| home_services | `service-plumbing` | 7 | faucet, shower valve, valves, copper and metal pipes, fittings, a drain |
| home_services | `service-hvac` | 5 | outdoor condensers, vents, a thermostat |
| home_services | `service-electrical` | 4 | outlet, wires, conduit, light fixtures |
| home_services | `about` | 2 | hand tools, a toolbox |
| home_services | `gallery` | 3 | finished details: a kitchen faucet, a ceiling vent, a pendant lamp |
| home_services | `general` | 2 | a house exterior, a quiet residential street |
| dental | `hero` | 3 | a treatment room, a waiting area, a building against open sky |
| dental | `service-treatment` | 7 | treatment rooms and chairs; dental models (braces, an implant crown) |
| dental | `reception` | 4 | waiting rooms and lounges |
| dental | `instruments` | 5 | hand instruments, mirror and probe, pliers on a tray |
| dental | `aligners` | 3 | a clear aligner, aligners on models, toothbrushes |
| dental | `exterior` | 2 | office building exteriors |

## The manifest is the record

`files/` is not committed. `manifest.json` is the record of the library. Each entry carries the file
path, family, slot, a short subject, alt text, the source, the photo page, the exact `image_url` that
was downloaded, the photographer and their page, the license and its URL, the download time, width,
height, bytes and the `sha256` of the file, plus `people: false` and `logos: false`.

Each image can be fetched again from its `image_url` and checked against its `sha256`. A test re-fetch
on 2026-10-05 returned byte-identical files from both CDNs (images.unsplash.com and images.pexels.com).
If a CDN ever re-encodes a photo, the hash will not match. Look at the new file against the product
rules below before using it. To restore `files/` from the repo root:

```sh
.venv/bin/python - <<'EOF'
import hashlib, json, pathlib, urllib.request

root = pathlib.Path("agent/data/stock")
for e in json.loads((root / "manifest.json").read_text())["images"]:
    path = root / e["file"]
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == e["sha256"]:
        continue
    req = urllib.request.Request(e["image_url"], headers={"User-Agent": "admiry-stock-restore/1"})
    data = urllib.request.urlopen(req, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != e["sha256"]:
        raise SystemExit(f"{e['file']}: bytes changed at the source; review the new image before using it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print("restored", e["file"])
EOF
```

## Licenses

Only free licenses are used. Both allow commercial use without asking permission and without credit.
Credit is optional, but the manifest keeps the photographer and photo page for every entry.

**Unsplash License** (https://unsplash.com/license): an irrevocable, nonexclusive, worldwide license
to download, copy, modify, distribute and use the photos for free, including commercially, without
permission from or attribution to the photographer or Unsplash. Not allowed:

- selling photos without significant modification;
- compiling Unsplash photos to replicate a similar or competing service.

**Unsplash+ is excluded.** Unsplash+ photos carry a different, paid license. Every Unsplash entry here
was checked as a free photo: `premium` and `plus` are false in Unsplash's own photo data, and the file
is served from `images.unsplash.com`, not `plus.unsplash.com`. Never add an Unsplash+ photo to this
library.

**Pexels License** (https://www.pexels.com/license/): free to use, commercial use allowed,
attribution not required, modification allowed. Not allowed:

- selling unaltered copies (for example as posters, prints or on physical products);
- redistributing or selling the photos on other stock photo or wallpaper platforms;
- implying endorsement by people or brands in the image;
- using the photos as part of a trademark, design mark, trade name, business name or service mark.

What that means for Admiry: the photos may appear inside customers' sites. The library must never be
offered as a collection to browse or download, which would be a competing stock service. A stock
photo must never become part of a business's logo or wordmark.

## Product rules (every photo)

These are product rules, not preferences:

1. **No people at all.** No faces, bodies, recognizable hands or silhouettes. A website must never
   show a stranger as if they were the business's staff or customer (spec T11: people are only ever
   the owner's real photos).
2. **No logos, brand names, readable product names, license plates, house numbers or street signs.**
3. **No text overlays or watermarks.** No illustrations or renders. Sharp, well lit and professional.
4. **Landscape, at least 1600 px wide.** All 60 are landscape and 2400 px wide.

Every downloaded file was opened and looked at, with zoomed crops of small details such as equipment
badges, porch columns, mailboxes, car fronts and window reflections. About 1,700 candidates were
screened on contact sheets, 175 were downloaded and reviewed at full size, and 60 were kept. The
usual reasons for rejection were house numbers, maker names on equipment, license plates, people
visible through windows or in reflections, Unsplash+ status, and renders.

Borderline but kept. Each passes the rules, and is listed so a reviewer can check it quickly:

- `home_services/service-hvac-03`: no name or logo on the thermostat, but its round design is
  recognizable as a well-known product.
- `home_services/general-02`: a distant information board with illegible text and a far-off SUV with
  no readable plate or badge.
- `home_services/about-01`, `home_services/service-electrical-02`, `dental/service-treatment-03`,
  `dental/instruments-02`, `dental/instruments-05`, `dental/reception-04`: small embossed or printed
  marks that cannot be read at full size.
- `home_services/service-roofing-05` and `dental/aligners-01` were published by companies (a roofing
  contractor and an aligner maker) under the free license. Neither photo shows any branding.

Known gaps: no clean free photo was found of a water heater, of pipes under a sink, or of an electrical
panel. Every candidate showed a maker's name. A street-level residential street was hard for the same
reason: house numbers, plates and street signs. These are candidates for AI-made scenes (spec T10)
or a later paid source.

## How Admiry uses it

A3p-6 (spec `docs/superpowers/specs/2026-10-05-admiry-pages-a3p-6-industry-sites-design.md`, T10
and T25) loads these photos into Admiry's asset store as the families' licensed stock library. Each
photo is stored in Admiry's own storage with its source, license and author on record. In the spec's
`site_asset` table, `family` maps to `family`, `slot` to `kind`, `sha256` to `blob_sha256`, `license`
to `licence`, `photographer` to `author`, and `ai` is false. T10 orders images as the owner's photos
first, then this library, then AI scenes. In previews a stock photo is labelled "Replace with your
photo" (T12). Stock never stands in for the family's key photo (T13) or for any person (T11).

## On a box

The files are not in git, and a CDN can re-encode a photo over time, so a box loads the library from a copy of
`files/` and fetches from the CDN only a photo that copy lacks. From a checkout that has `agent/data/stock/files`
(the restore script above makes it), with `$C` the box's `docker compose` command:

```sh
scp -r agent/data/stock/files root@<box>:/root/stock-files
$C cp /root/stock-files agent:/tmp/stock-files
$C exec -T agent python -m agent.ops stock-load --files /tmp/stock-files
$C exec -T agent rm -rf /tmp/stock-files; rm -rf /root/stock-files
```

`--files` takes the copy as it is (`<DIR>/home_services/hero-01.jpg` for the manifest's
`files/home_services/hero-01.jpg`); `--manifest PATH` and `--family F` (repeatable) narrow or move the run. A run
without `--files` fetches every photo from its `image_url` (the address is checked as public first) for a box no
copy reaches.

What it stores, once per box (the blobs are content-addressed, so every site shares them): each photo's original
in `site_blob`, WebP copies of it (quality 72) at 640, 1024 and 1600 pixels wide, never wider than the original,
each a `site_asset_file` row, and the `site_asset` row with the licence record (the manifest's `created` is its
`reviewed_at`). Nothing in `bundles.gc` collects them. The 60 originals are about 36 MB and their sizes about
25 MB.

The hash rule: every photo's SHA-256, from the copy or from the CDN alike, must be the manifest's. A photo that
differs is not stored and is named on stderr with "bytes changed at the source; review the new image before using
it", and the command exits 1 (a wrong local file is failed, never replaced by the CDN's answer). Run again after
fixing the file: a photo already stored is `kept`.

Expect `loaded 60 (60 from files, 0 fetched), kept 0, failed 0` the first time and `loaded 0 (0 from files, 0
fetched), kept 60, failed 0` on every run after; `--family dental` counts 24 instead of 60. The library lives in
the database from then on, so delete both copies as above.
