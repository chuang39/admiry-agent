# Voice + photos → a verified ad kit (Meta hackathon MVP)

Date: 2026-11-04. Status: design, awaiting review. Deadline: submission due 2026-11-04 21:00 ET (18:00 PT).

## 1. What we are building and why

A local business owner (a roofer, a landscaper, a cake shop) talks to their phone for a minute about what
they do and snaps two or three photos of their real work. A minute or two later they get a small ad kit:
four ready-to-post ad images (two angles × feed square and story) plus the matching text for search and
social ads. Every claim in every ad is traced back to a sentence the owner actually said, and the work shown
in every image is the owner's real photo, not an AI re-drawing of it.

Why this matters: micro businesses don't have time to write ads and can't judge AI copy. Generic AI ads
invent things ("licensed and insured", "best in town", "20% off") and re-draw the work, which is deceptive
for a trade business. The product's promise is **fast, and nothing made up**.

Track: Startups. Problem statement: *Build for Your World*.

## 2. What is new (judged) vs pre-existing (not judged)

- **New, built 2026-11-04 during the hackathon:** everything in this repository. No code is copied from
  Admiry's existing (private) codebase.
- **Pre-existing, not part of the submission:** Admiry's product at admiry.ai (marketing plans, landing
  pages, ad execution). This MVP does not depend on it. A hand-off to it is a stretch item (S2) and, if built,
  is labeled as pre-existing in the README and the video.

## 3. Owner flow (one phone page)

1. **Talk.** Hold-to-record (up to 2 minutes): "I'm Dave, Dave's Decks in Austin, I build cedar and composite
   decks, free estimates, call 512-…". Typing instead is allowed.
2. **Snap.** Add 1–5 photos of finished work (camera or library).
3. **Make my ads.** A live timeline shows each Meta model doing its step (this is also the video's spine).
4. **Review.** Four ad images; under each, the claims as chips. Tapping a chip shows the owner's own words it
   came from. A "Removed" list shows what the checker cut and why. Download all (zip).

## 4. Pipeline and models

| # | Step | Model | In → Out | If it fails |
|---|------|-------|----------|-------------|
| 1 | Transcribe | `muse-voice-transcribe-1.0` (`POST /v1/asr/transcribe`) | WAV (encoded in the browser) → transcript | retry once, then ask the owner to type |
| 2 | Understand | `muse-spark-1.3` (vision + JSON schema output) | transcript + photos → business facts (each with the verbatim quote it came from), per-photo subject noun phrase + quality, two ad angles (headline, body, CTA; each claim points at fact ids) | retry once with the validation error appended |
| 3 | Check copy | `muse-wonder-1.0` (independent checker) | transcript + facts + draft copy → per-claim verdict: grounded (with quote) or not, plus rule hits | fall back to `muse-spark-1.3` as checker, labeled as such |
| 3b | Repair | `muse-spark-1.3` | copy + rejected claims → rewritten copy without them; re-checked; max 2 rounds, then the claim is dropped | drop the claim |
| 4 | Cut out the work | `sam-3.1` (Responses API, base64 `input_image`, `meta-sam-parser` to decode) | each angle's chosen photo + its noun phrase → mask → RGBA cut-out of the real pixels | use the whole photo, center-cropped |
| 5 | Design the scene | `muse-image-1.0` (`/v1/images/generations`, `size` per format) | trade + angle mood → background plate with clear space for the work and the headline | flat brand-color plate |
| 6 | Compose | our code (Pillow) | plate + cut-out (pixel-exact, shadowed) + checked text (rendered by us) → 1080×1080 and 1080×1920 PNG | n/a |
| 7 | Final look (stretch S1) | `muse-wonder-1.0` (vision) | each rendered ad + checked copy → "text in image matches checked copy; layout readable" | mark "not visually checked" |

Five Meta models, each doing a job the others can't: Voice Transcribe (ears), Spark (understanding and
writing), Wonder (independent checking), SAM 3.1 (keeping the real work real), Image (the scene).

**Invariants (what "verified" means here):**

1. Every claim shown in an ad has a grounded verdict with a verbatim transcript quote. No quote → not shown.
2. Rule list the checker enforces even when grounded-looking: no licenses/insurance/certifications, prices,
   discounts, guarantees, "best/#1", health or safety outcomes unless the owner said them verbatim.
3. The pixels of the owner's work in the ad are the owner's photo (from the SAM mask), never generated.
4. Ad text is rendered by our code from the checked copy, never by the image model (image models garble
   text, and the text must be exactly what was checked).

## 5. Architecture

- One Python 3.12 service: FastAPI + the OpenAI SDK pointed at `https://api.meta.ai/v1` (Meta Model API is
  OpenAI-compatible) + `httpx` for the ASR multipart call + `meta-sam-parser` + Pillow.
- A run = a folder `runs/<id>/` (inputs, intermediate JSON, outputs) plus an in-memory state record.
  `POST /api/runs` (multipart: audio WAV or text, photos) → `{id}`; `GET /api/runs/{id}` → step statuses and
  artifact URLs; the page polls every second. Steps 4–5 run in parallel per angle × format.
- Frontend: one static HTML page, vanilla JS, no build step, phone-first. MediaRecorder audio is decoded with
  Web Audio and re-encoded to 16-bit mono WAV in the browser (the ASR endpoint takes WAV only).
- Model ids and the API key come from env (`MODEL_API_KEY`); **Standard mode, not Contributor mode**,
  because owner photos and voice are user content.
- Privacy: EXIF (GPS) stripped from photos on upload, before anything is sent to a model; runs deleted after
  24 hours.
- Abuse and cost: about $0.10 per run (4 images × $0.01, SAM $0.0025/image, ASR ~$0.003/min, LLM cents).
  Per-IP limit 5 runs/hour and a global cap of 200 runs/day (≈ $20/day ceiling). Upload limits: 5 photos ×
  10 MB, audio ≤ 2 min.
- Hosting: our existing Linode dev box behind its Caddy, at `https://studio.dev.adx.admiry.ai` (wildcard DNS
  already points there), run as a systemd service from a venv. No container registry needed.

## 6. Testing

- Unit tests for the pure parts: WAV handling, the claim ledger (grounding, rule hits, repair-loop
  termination), compositing geometry, SAM output parsing against a recorded fixture.
- `scripts/smoke.py`: runs the full pipeline against the live API on the sample assets and asserts the
  invariants (every shown claim has a quote; text in images equals checked copy; outputs exist).
- First implementation step is a probe script per model, confirming the request shapes the docs leave open
  (JSON-schema output on Spark, image sizes returned, SAM mask decode, ASR response fields).

## 7. Scope and schedule (Pacific time, today)

- **Must (code done by ~11:00):** steps 1–6, the page, the claim chips and Removed list, zip download,
  limits, deploy, smoke test.
- **Stretch (until 15:00, in this order):** S1 step 7 final look; S2 hand-off of the facts to Admiry's
  existing product (labeled pre-existing); S3 live captions via the realtime ASR websocket; S4 Muse Image
  full-scene edit of the original photo, accepted only if a SAM re-segmentation shows the work unchanged.
- **15:00 code freeze → 17:00** video and README (what's new, how to run, models used) → **submit by 17:30**.

## 8. Demo video (3 minutes)

0:00 the problem in one line → 0:15 the owner talks and snaps (real phone) → 0:45 the timeline, naming each
Meta model as it runs → 1:30 the four ads → 1:50 tap a claim, see the owner's words; the Removed list with
"licensed and insured" cut because the owner never said it → 2:20 the work in the ad is their real photo
(before/after toggle) → 2:40 repo, what was built today, cost per run.

## 9. Open items for the founder

- **Demo assets:** photos and a voice note we have the rights to (your own business photos, a friend's with
  written OK, or CC0). Avoid medical/dental examples (hackathon anti-project list).
- **Team list** for the submission page (max 4) and each member's own $150 credit claim, both due today.
- The judging rubric table did not come through in the participant guide; if it lists criteria, the video
  outline may shift.
