# Verified creatives + real-time Google Ads on Admiry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import the existing Admiry agent as one labeled commit, then add verified ad creatives from the owner's
job photos (a new public tool) and a `live_paused` mode that creates the Google Ads campaign for real, with the new
image and callout assets, and never turns it on.

**Architecture:** New package `agent/creatives/` (Model API client for the new calls, image geometry, the claim
ledger, the pipeline) behind one new command and one new public tool; two edits to the existing Google Search builder
and launch lifecycle. Runs on the Linode dev box from an image built on the box.

**Tech Stack:** Python 3.12, FastAPI + FastMCP (existing), httpx, pydantic, Pillow, `meta-sam-parser`, Postgres 16,
Google Ads REST v25 (existing client).

**Spec:** `docs/superpowers/specs/2026-11-04-muse-verified-creatives-google-ads-design.md`

## Global Constraints

- Deadline: submit by 17:30 PT 2026-11-04 (hard deadline 18:00 PT); code freeze 14:00 PT.
- Model API base `https://api.meta.ai/v1`, key from `META_MODEL_API_KEY` (existing setting `llm_api_key`).
- New code uses Standard-mode models only: `muse-spark-1.3`, `muse-wonder-1.0`, `muse-image-1.0`, `sam-3.1`. The
  existing agent's own `AGENT_LLM_MODEL` is not changed.
- Google image assets: landscape 1200×628 (1.91:1, min 600×314), square 1200×1200 (1:1, min 300×300), each ≤ 5 MB.
- Text limits: headlines ≤ 30 chars (3–15), descriptions ≤ 90 (2–4), callouts ≤ 25 (0–4).
- No text, logo or button is ever drawn on an image. The owner's work pixels are their own (pixel-lock).
- `live_paused` never enables a campaign, by any path (launch step, resume tool, jobs). Allowlisted owners only.
- Nothing that failed a check is stored as accepted or sent to Google.
- The import commit changes nothing inside `agent/`; every later commit is hackathon work.
- No box IPs, internal paths or project ids in committed files: scripts take them from arguments or env.
- `make`-free here: `ruff format` + `ruff check` with the `[tool.ruff]` settings copied from the private repo (line
  length 120) on every Python file touched; tests run with a private `AGENT_TEST_DB_URL`.

## Review Focus

1. **Muse's tool call outlives the pipeline** (60–120 s): `suggest_ad_creatives` must return within a few seconds
   with `status: "making"` and the assistant calls again. Test in Task 6.
2. **A photo SAM finds nothing in, or a photo below Google's minimum**: falls back to the plain crop, or is skipped
   with a reason; the run still finishes. Tests in Tasks 3 and 5.
3. **The image model returns a size other than the canvas asked for**: resized to the canvas before the pixel-lock.
   Test in Task 3.
4. **The owner has no photos yet**: the tool answers with the existing upload link and calls no model. Test in Task 6.
5. **Resume or a later launch step under `live_paused`**: refused, nothing enabled. Test in Task 8.

---

### Task 1: Import the agent and make it run here

**Files:**
- Create: `agent/**` (copied from the private repo at `6f6f213b`, unchanged), `pyproject.toml` (only
  `[tool.ruff]` + `[tool.pytest.ini_options]` copied from the private repo), `.gitignore`, `README.md` (two sections:
  "Pre-existing: imported from Admiry" and "Built during the hackathon", the second empty for now).

- [ ] **Step 1:** `git -C ~/MyProjects/BidX archive 6f6f213b agent | tar -x -C ~/MyProjects/admiry-agent`; check
  no secret literal and no box IP: `grep -rnE '69\.164|172\.233|sk_live|BEGIN [A-Z ]*PRIVATE KEY' agent` → nothing.
- [ ] **Step 2:** `uv venv && uv pip install -r agent/requirements.txt pytest ruff` (Python 3.12).
- [ ] **Step 3:** Run a slice of the existing suite against a private DB:
  `AGENT_TEST_DB_URL=postgresql:///admiry_hack_test .venv/bin/pytest agent/tests/test_surface_gate.py agent/tests/test_google_search_build.py -q`
  (use the nearest existing file names under `agent/tests/`). Expected: PASS. A failure here is an import problem to
  fix in `pyproject.toml`/paths, never in `agent/`.
- [ ] **Step 4:** Commit `import: Admiry agent as of the private repo at 6f6f213b -- pre-existing, not hackathon
  work` (agent/ alone), then a second commit for `pyproject.toml`, `.gitignore`, `README.md`. Push.

### Task 2: Run this repo's agent on the dev box; walk the existing flow

**Files:**
- Create: `scripts/deploy-dev.sh` (args: `<ssh-host> <compose-dir> <git-sha>`)

**Interfaces:** Produces the deploy command every later task uses: `scripts/deploy-dev.sh "$HOST" "$DIR" "$(git rev-parse HEAD)"`.

- [ ] **Step 1: Write `deploy-dev.sh`:** on the box: clone or fetch this repo into `/srv/admiry-agent`, check out the
  sha, read the agent service's image name from `docker compose config` in `<compose-dir>`, `docker build -f
  agent/Dockerfile --build-arg BIDX_BUILD_SHA=<sha> -t <image>:<sha> .`, set `AGENT_IMAGE_TAG=<sha>` in that dir's
  `.env` (back up `.env` first, same-inode write), `docker compose up -d agent`, then poll
  `https://agent.dev.adx.admiry.ai/connect/docs` until 200 (90 s max). Rollback = the same script with the previous
  sha (`1f55a1dc…` is already on the box).
- [ ] **Step 2:** Deploy the import commit. Expected: 200, `docker compose ps agent` healthy, its logs free of
  tracebacks.
- [ ] **Step 3: Walk the existing flow** as the founder's allowlisted address with a roofer test business: plan,
  site, photo upload (3 roofing photos), Stripe test payment, confirm → validate-only Google call succeeds. Write
  each blocker caused by the GCP billing outage into `docs/ops/dev-walk-2026-11-04.md`; fix only those, by the
  cheapest bypass, each as its own commit.
- [ ] **Step 4:** Commit the script and the walk notes.

### Task 3: Model API client for the new calls

**Files:**
- Create: `agent/creatives/__init__.py`, `agent/creatives/meta.py`, `scripts/probe_models.py`
- Test: `agent/tests/test_creatives_meta.py`, fixture `agent/tests/fixtures/sam_one_bit.txt` (recorded by the probe)
- Modify: `agent/requirements.txt` (+ `meta-sam-parser`)

**Interfaces — Produces:**
- `SPARK = "muse-spark-1.3"`, `WONDER = "muse-wonder-1.0"`, `IMAGE = "muse-image-1.0"`, `SAM = "sam-3.1"`
- `class MetaError(Exception)`
- `@dataclass(frozen=True) class Segment: box: tuple[int, int, int, int]; width: int; height: int; confidence: float | None; mask: bytes; mask_w: int; mask_h: int` (`mask` row-major 0/1, `mask_w*mask_h` bytes)
- `class MetaClient: __init__(self, http: httpx.Client)`
  - `json(self, model: str, system: str, parts: list[dict], schema: type[M]) -> M` — chat/completions, `response_format` json_schema (same shape as `llm.Generator._parse`); `parts` items are `{"type": "text", "text": …}` or `{"type": "image_url", "image_url": {"url": data_uri}}`
  - `edit_image(self, png: bytes, prompt: str, size: str) -> bytes` — `POST images/edits` JSON `{"model": IMAGE, "prompt", "images": [{"image_url": data_uri}], "n": 1, "size", "response_format": "b64_json"}`
  - `segment(self, png: bytes, phrase: str) -> list[Segment]` — `POST responses` with `input_text` + `input_image` (base64 data URL), `metadata: {"mask_encoding": "one_bit", "include_confidence": "true"}`; parse with `meta_sam_parser`
- `def client(settings: config.Settings | None = None) -> MetaClient` — base URL and key from settings, timeout 120 s.

- [ ] **Step 1: Probe first.** `scripts/probe_models.py <photo.jpg>` calls each of the four once and prints the
  shapes; it saves the raw SAM output to the fixture. Decide from it: whether `responses` answers non-streaming
  (else read the SSE `response.output_text.delta` events and join them), the `meta_sam_parser` function names, and
  the returned image size for `size="1792x1024"`/`"1024x1024"`.
- [ ] **Step 2: Failing tests** (httpx `MockTransport`): `test_json_sends_image_parts_and_schema` (request body has
  the `image_url` part and `response_format.json_schema.name == schema.__name__`); `test_edit_image_returns_bytes`
  (b64 in `data[0].b64_json` → bytes); `test_segment_parses_fixture` (fixture → ≥1 `Segment`, `len(mask) ==
  mask_w*mask_h`, `0 <= confidence <= 1`); `test_segment_empty_answer_is_empty_list`; `test_http_error_raises_meta_error`.
- [ ] **Step 3:** Implement; run tests → PASS; ruff; commit.

### Task 4: Image geometry (pure)

**Files:**
- Create: `agent/creatives/images.py`
- Test: `agent/tests/test_creatives_images.py`

**Interfaces — Consumes:** `Segment`. **Produces:**
- `SIZES = {"landscape": (1200, 628), "square": (1200, 1200)}`, `MINIMUM = {"landscape": (600, 314), "square": (300, 300)}`, `MODEL_SIZE = {"landscape": "1792x1024", "square": "1024x1024"}`
- `load(raw: bytes) -> Image.Image` (EXIF-transposed RGB)
- `mask_of(seg: Segment, photo: Image.Image) -> Image.Image` ("L", 0/255, photo size; the raster resized when smaller)
- `pad(photo: Image.Image, fmt: str) -> tuple[Image.Image, tuple[int, int, int, int]]` (photo contained and centred on the format's canvas, padding neutral grey; returns the canvas and the photo's placement box)
- `pixel_lock(generated: Image.Image, photo: Image.Image, mask: Image.Image, place: tuple[int, int, int, int], fmt: str) -> Image.Image`
- `iou(a: Image.Image, b: Image.Image) -> float`
- `plain_crop(photo: Image.Image, fmt: str, focus: tuple[int, int, int, int] | None) -> Image.Image` (cover crop centred on `focus`)
- `too_small(photo: Image.Image, fmt: str) -> bool`
- `encode(img: Image.Image) -> tuple[bytes, str]` (PNG, or JPEG q90 when PNG > 5 MB)

- [ ] **Step 1: Failing tests:** `test_pad_keeps_photo_pixels_at_place`; `test_pixel_lock_restores_masked_pixels`
  (a generated canvas of noise: inside the mask the output equals the photo exactly); `test_pixel_lock_resizes_odd_model_size`
  (generated 1792×1024 for landscape → output 1200×628); `test_iou_identical_is_1_disjoint_is_0`;
  `test_plain_crop_has_format_size`; `test_too_small_for_tiny_photo` (500×300 landscape → True);
  `test_load_applies_exif_orientation`; `test_encode_under_5mb`.
- [ ] **Step 2:** Implement; PASS; ruff; commit.

### Task 5: Claim ledger and the pipeline

**Files:**
- Create: `agent/creatives/copy.py`, `agent/creatives/studio.py`, `agent/creatives/prompts.py`
- Modify: `agent/schema.sql` (append the two tables below)
- Test: `agent/tests/test_creatives_copy.py`, `agent/tests/test_creatives_studio.py`

```sql
CREATE TABLE IF NOT EXISTS ad_creative_set (
  id          TEXT PRIMARY KEY,                                   -- cs_…
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  status      TEXT NOT NULL CHECK (status IN ('making', 'ready', 'failed', 'accepted')),
  steps       JSONB NOT NULL DEFAULT '[]', ledger JSONB, error TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(), accepted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ad_creative_set_biz ON ad_creative_set (business_id, created_at DESC);
CREATE TABLE IF NOT EXISTS ad_creative_image (
  id        TEXT PRIMARY KEY,                                     -- ci_…, unguessable (db.new_id)
  set_id    TEXT NOT NULL REFERENCES ad_creative_set(id) ON DELETE CASCADE,
  photo_id  TEXT NOT NULL, format TEXT NOT NULL CHECK (format IN ('landscape', 'square')),
  method    TEXT NOT NULL CHECK (method IN ('extended', 'plain_crop')),
  bytes     BYTEA NOT NULL, content_type TEXT NOT NULL, width INT NOT NULL, height INT NOT NULL,
  checks    JSONB NOT NULL
);
```

**Interfaces — Consumes:** Task 3, Task 4. **Produces:**
- `copy.py`: pydantic `PhotoRead(photo_id, subject, suitable, reason: str | None, privacy: list[str])`,
  `Claim(text, fact_ids: list[str])`, `AdText(headlines, descriptions, callouts: list[Claim])`,
  `Verdict(text, grounded: bool, rule: str | None, why: str)`, `Check(verdicts: list[Verdict])`,
  `Removed(text, kind, why)`, `Ledger(kept: AdText, removed: list[Removed], checker: str)`;
  `LIMITS = {"headlines": (30, 3, 15), "descriptions": (90, 2, 4), "callouts": (25, 0, 4)}`; `MAX_REPAIRS = 2`;
  `facts_of(knowledge: dict) -> list[dict]` (`{"id": "f1", "text": …}` from the business's stored knowledge: name,
  trade, services, area, phone, years, licence, offers, whatever is present);
  `write_and_check(meta: MetaClient, facts: list[dict], rules: str, trade: str) -> Ledger`.
- `studio.py`: `start(conn, settings, business_id: str) -> dict` (creates a `making` set and runs `run` on a
  daemon thread unless one is already `making`; returns `card`), `run(settings, set_id: str, *, meta: MetaClient | None = None) -> None`,
  `card(conn, business_id: str, base_url: str) -> dict`, `accept(conn, business_id: str, set_id: str) -> dict`,
  `accepted(conn, business_id: str) -> dict | None` (`{"set_id", "headlines": [str], "descriptions": [str], "callouts": [str], "images": [{"id", "format", "bytes"}]}`).
- `run` order (spec §4): read photos with Spark (`PhotoRead` per `page_photo` of the business's current page, max
  5); a photo with `suitable == False` or any `privacy` flag is skipped, its reason kept for the card → per suitable photo and format: SAM `segment(subject)` → `pad` → `edit_image` (prompt from `prompts.py`:
  fill only the grey padding with a continuation of the scene, no text, no people, no logos) → `pixel_lock` →
  Wonder vision check (policy) + SAM re-segment, `iou >= 0.9` → else `plain_crop`; text via `write_and_check`
  with `knowledge/home_services/compliance.md` + the trade's file as `rules`; each step appended to `steps` as
  `{"step", "model", "ok", "ms"}`.

- [ ] **Step 1: Failing tests (copy, fake MetaClient):** `test_ungrounded_claim_is_removed_after_two_repairs`
  ("Licensed & insured" with no licence fact → in `removed`, rule named); `test_grounded_claim_kept`;
  `test_overlong_headline_dropped_not_truncated`; `test_wonder_failure_falls_back_to_spark_checker`
  (`checker == "muse-spark-1.3"`); `test_facts_of_roofer_sample` (uses `data/sites/samples/roofer.json`).
- [ ] **Step 2: Failing tests (studio, DB + fake MetaClient):** `test_run_writes_ready_set_with_images`;
  `test_privacy_flagged_photo_skipped`; `test_sam_empty_uses_plain_crop`; `test_small_photo_skipped_with_reason`; `test_failed_image_check_uses_plain_crop`;
  `test_accept_then_accepted_returns_kept_text_and_images`; `test_accept_rejects_set_of_other_business`.
- [ ] **Step 3:** Implement; PASS; ruff; commit.

### Task 6: Command, public tool, image route

**Files:**
- Create: `agent/tools_creatives.py` (command `ad_creatives` via `mcp_app.agent_tool(cls="W", scope="manage")`
  and `mcp_app.run`, following `tools_pages.py`), `agent/surface/creatives.py`
- Modify: the module list that imports tool modules at boot (where `tools_pages` is imported); `agent/web.py`
  (`GET /creatives/{image_id}` and `GET /creatives/{image_id}/original` — bytes by unguessable id, like page photos);
  `agent/surface/contract.py` (contract `2026-11-04`); the surface tests that list public tools
- Test: `agent/tests/test_creatives_tool.py`

**Interfaces — Consumes:** `studio.start/card/accept`. **Produces:**
- `tools_creatives` is imported before `surface/creatives` (`public_tool` checks the command exists at import).
- `@public_tool(cls="W", scope="manage", commands=("ad_creatives",)) def suggest_ad_creatives(accept_set_id: str | None = None, refresh: bool = False) -> dict`
  — no set or `refresh` → `start`; else the latest `card`; `accept_set_id` → `accept`. Docstring tells the assistant:
  call again while `status == "making"`; show each image URL and each claim with its source; ask the owner before
  accepting.
- card: `{"status", "set_id", "steps", "images": [{"id", "format", "url", "original_url", "method", "checks"}], "text": {"headlines": [{"text", "source"}], …}, "removed": [{"text", "why"}], "models": [...], "upload_url": str | None}`

- [ ] **Step 1: Failing tests:** `test_no_photos_returns_upload_link_and_calls_no_model`;
  `test_first_call_returns_making_within_2s` (studio.run patched to sleep 5 s); `test_second_call_returns_ready_card`;
  `test_accept_marks_accepted`; `test_image_route_serves_bytes_and_404s_unknown`; `test_tool_listed_in_public_surface`.
- [ ] **Step 2:** Implement; PASS together with the existing surface tests; ruff; commit; deploy (Task 2 script);
  call it once over `/mcp` with a business key on the dev roofer business.

### Task 7: Google builder: accepted text, image and callout assets

**Files:**
- Modify: `agent/channels/google_search/build.py` (`operations(..., creatives: dict | None = None)`, new
  `creative_asset_ops`), `agent/channels/google_search/adapter.py` (`build` puts `studio.accepted(...)` into
  `payload["creatives"]`; `launch` passes it to `operations`), `resources_from` (asset results do not break it)
- Test: `agent/tests/test_creatives_google_ops.py`

**Interfaces — Produces:** `creative_asset_ops(campaign_ref: str, creatives: dict, customer_id: str, start: int) -> list[dict]`
— per image: `assetOperation.create` `{resourceName: temp, name: f"admiry {set_id} {format} {i}", type: "IMAGE", imageAsset: {data: b64}}`
then `campaignAssetOperation.create` `{campaign: campaign_ref, asset: temp, fieldType: "AD_IMAGE"}`; per callout:
`calloutAsset: {calloutText}` + `fieldType: "CALLOUT"`. Temp names from the existing `temp()`, unique across the call.

- [ ] **Step 1: Failing tests:** `test_operations_without_creatives_unchanged` (equal to today's output for the
  roofer fixture); `test_operations_use_accepted_headlines_and_descriptions` (existing pinning rule still applied);
  `test_asset_ops_link_images_and_callouts` (counts, field types, temp names unique and negative);
  `test_resources_from_ignores_asset_results`.
- [ ] **Step 2:** Implement; PASS with the existing google_search tests; ruff; commit.

### Task 8: `live_paused` execution mode

**Files:**
- Modify: `agent/config.py` (`ADS_EXECUTION_MODES` + `"live_paused"`), `agent/ads_run.py` (step routing: under
  `live_paused` a `launching` row → `"stop"` when `stop_owed` else `None`; `_create` returns after `_store_built`
  without `_enable`; the resume path refuses under `live_paused` with "Demo mode: these ads were built paused in
  Google Ads and are not turned on."; the owner's one-clause state for such a row: "built in Google Ads, paused —
  demo mode, nothing spends")
- Test: `agent/tests/test_live_paused.py`

- [ ] **Step 1: Failing tests:** `test_config_accepts_live_paused`; `test_queued_row_creates`;
  `test_launching_row_never_enables` (`_enable` patched to raise); `test_create_skips_enable`;
  `test_resume_refused_under_live_paused`; `test_off_list_owner_is_off` (allowlist still applies).
- [ ] **Step 2:** Implement; PASS with the existing ads_run tests; ruff; commit.

### Task 9: Live smoke on dev, then the Muse walk

**Files:**
- Create: `scripts/smoke_live_paused.py` (args: business id; prints the campaign and its assets from a GAQL
  read-back)

- [ ] **Step 1:** Deploy; set `AGENT_EXEC_GOOGLE_SEARCH=live_paused` in the box `.env`; restart agent.
- [ ] **Step 2:** On the roofer business: `suggest_ad_creatives` → ready → accept → existing confirm-and-launch.
- [ ] **Step 3:** `scripts/smoke_live_paused.py <biz>` → campaign `PAUSED`, ≥ 1 `AD_IMAGE` and ≥ 1 `CALLOUT`
  campaign asset, RSA headlines equal the accepted ones. Any `ENABLED` → set the mode back to `validate` at once and
  stop.
- [ ] **Step 4:** The same flow from Muse on the phone, recorded for the video. Commit the script.

### Task 10: README and video script

**Files:**
- Modify: `README.md` ("Built during the hackathon": N1–N4 with commit links; models and what each does; cost per
  run; how to run tests; the import commit named as pre-existing)
- Create: `docs/demo-script.md` (the spec's §9 beats with the exact Muse prompts)

- [ ] **Step 1:** Write both; check every link resolves on GitHub; commit; push. Submission is the founder's.
