# Admiry on Muse: verified ad creatives and a real-time Google Ads campaign (Meta hackathon MVP)

Date: 2026-11-04. Status: design, awaiting the founder's review. Supersedes this morning's standalone
"voice + photos" design (deleted in the same commit). Deadline: submission due 2026-11-04 21:00 ET (18:00 PT).

## 1. The demo in one paragraph

A roofer (or a remodeler, a landscaper) talks to **Muse**, Meta's assistant, with the Admiry connector added.
In one conversation Admiry builds their website and proposes a managed ad plan; both are **pre-existing**.
**New today:** Admiry turns the owner's own job photos into Google-ready image ads plus ad text, checks every
claim and every image, and on the owner's OK the agent backend **creates the Google Ads campaign in real time**
through the live Google Ads API. The campaign is created **paused**, so nothing is spent. Muse shows the result;
the Google Ads UI shows the same campaign, with the new images, appearing.

Track: Startups. Problem statement: *Build for Your World*.

## 2. Pre-existing vs new

**Pre-existing, imported in one commit** ("import: Admiry agent as of the private repo at 6f6f213b -- pre-existing,
not hackathon work"): the whole `agent/` package, unchanged. It contains the Muse connector (MCP gateway with
OAuth), the admiry.ai web chat, the website builder (home-services templates, a roofer sample), marketing plans,
the managed Google Search campaign builder (validate-only on dev), the phone photo upload page and the
home-services knowledge (roofing, remodel, landscaping, compliance, google-search). It already calls Muse Spark.

**New, every commit after the import** (the README's "Built during the hackathon" lists them):

- **N1 Creative studio** (`agent/creatives/`): the owner's job photos → Google image assets and ad text, each
  checked (§4).
- **N2 Public tool `suggest_ad_creatives`** (and the owner's acceptance through the same tool), with a
  creatives card in Muse and in the web chat. Surface contract bumped.
- **N3 Google builder additions:** image assets (landscape 1.91:1, square 1:1) and callout assets linked to the
  campaign; accepted headlines and descriptions feed the responsive search ad.
- **N4 Execution mode `live_paused`:** the real create mutate (not validate-only), everything paused, and this
  path never enables a campaign. Only for businesses on the launch allowlist.
- **N5** README, run notes, the video.

Video rule: pre-existing steps are shown fast and labeled "existing Admiry"; the highlighted part is N1–N4.

## 3. Owner flow (roofer demo)

1. In Muse: "I'm a roofer in Austin, help me get customers." Admiry builds the site and the plan. *(existing, fast)*
2. The owner adds 3–5 job photos through the existing upload link. *(existing)*
3. "What ads would you run?" → `suggest_ad_creatives`. *(new)* Muse shows, per chosen photo, a landscape and a
   square image, plus headlines, descriptions and callouts. Each claim shows the profile fact it came from; a
   "Removed" list says what the checker cut and why; a before/after toggle shows which pixels are the owner's.
4. "Looks good, set it up." → acceptance, then the existing confirm-and-launch path running in `live_paused`.
   *(new)* Campaign, budget, ad group, keywords, ad, image and callout assets are created live and paused. Muse
   answers with the campaign id and a Google Ads link.

## 4. Creative pipeline (N1)

| # | Step | Model | In → Out | If it fails |
|---|------|-------|----------|-------------|
| 1 | Read the photos | `muse-spark-1.3` (vision, JSON output) | photos + profile → per photo: subject noun phrase ("new shingle roof"), ad suitability, privacy flags (faces, house numbers, plates, customer names) | skip the photo |
| 2 | Cut out the work | `sam-3.1` (`meta-sam-parser` decodes the mask) | photo + noun phrase → mask of the real work | plain crop of the original |
| 3 | Extend to Google's sizes | `muse-image-1.0` (`/v1/images/edits`) | the original padded onto a 1.91:1 and a 1:1 canvas → the model fills only the padding (sky, yard) and tidies clutter outside the mask; our code then pastes the masked original pixels back (**pixel-lock**) | plain crop of the original |
| 4 | Write the text | `muse-spark-1.3` (JSON output) | profile + knowledge (google-search, the trade's file, offers) → 15 headlines ≤ 30 chars, 4 descriptions ≤ 90, 4 callouts ≤ 25, each claim citing a profile fact | retry once with the validation error |
| 5a | Check the text | `muse-wonder-1.0` | text + profile + home-services compliance rules → per claim: grounded or not, rule hits | `muse-spark-1.3` as checker, labeled |
| 5b | Repair | `muse-spark-1.3` | rejected claims → rewritten text, re-checked; max 2 rounds, then the claim is dropped | drop the claim |
| 5c | Check the images | `muse-wonder-1.0` (vision) + `sam-3.1` | each output → Google image-asset policy (no overlaid text, logo or button; not a collage; sharp; subject not cut off) and SAM re-segmentation: the work's mask must sit where the original's was (IoU ≥ 0.9) | fall back to the plain crop |

**Invariants:** every claim shown or sent has a grounding fact; the owner's work in every image is their own
pixels; no text is drawn on images (Google's image-asset policy, and honesty); nothing that failed a check is
sent to Google. Owner content goes to **Standard-mode** models only (Contributor mode lets Meta train on it); the
existing agent's own model choice is not changed.

## 5. Real-time Google Ads creation (N3, N4)

- Existing: one create mutate (campaign, budget, ad group, keywords, responsive search ad), all PAUSED, sent
  validate-only on dev; enabling is a separate step after the owner confirms.
- New: accepted headlines and descriptions replace the generated ones in the responsive search ad; image assets
  are uploaded and linked to the campaign for `AD_IMAGE`, callouts for `CALLOUT`.
- New mode `AGENT_EXEC_GOOGLE_SEARCH=live_paused`: the same mutate with `validate_only=false`; the enable step
  refuses in this mode; allowlisted businesses only. Target: the client account under the Admiry manager account
  (both reachable through the API on 2026-11-04 despite the GCP billing outage). While paused nothing can spend.
  After the demo the campaign is removed or left paused, the founder's call.

## 6. Where it runs

- Code lives here; the package path stays `agent/`, so after the hackathon the new commits port back to the
  private repo as patches.
- Live product: the Linode dev box. Its agent image is built on the box from this repo (the usual registry is
  unavailable while GCP billing is off), same compose service and tag variable. The Muse connector and the web
  chat use the dev endpoints. Prod is not touched.
- Cost: about $0.10 per creatives run (4–6 images at $0.01, SAM at $0.0025 per image, text calls in cents).

## 7. Step 0 and testing

- **Step 0 (first ~90 minutes):** the import commit; build and run it on dev; walk the existing flow with a roofer
  test business on the allowlist (plan, site, photos, Stripe test payment, confirm → validate-only). Fix only
  blockers caused by the GCP outage, by the cheapest bypass.
- One probe per new model call (Muse Image edit on a padded canvas, SAM mask decode, Wonder vision check).
- Unit tests for the new code (pixel-lock geometry, IoU check, claim ledger, Google asset operations) next to the
  existing suite, which stays green for every module touched.
- One live smoke: creatives for the demo business → `live_paused` push → a GAQL read-back shows the campaign
  PAUSED with its image and callout assets.

## 8. Schedule (Pacific time, today)

| Time | Work |
|------|------|
| 01:00–02:30 | Step 0 |
| 02:30–08:00 | N1 creative studio, N2 tool and card |
| 08:00–11:00 | N3 + N4 live-paused push, live smoke |
| 11:00–14:00 | the Muse walk end to end, fixes, polish |
| 14:00–16:30 | code freeze, video, README |
| by 17:30 | submit (deadline 18:00 PT) |

## 9. Demo video (3 minutes)

0:00 the problem → 0:10 Muse builds the site and plan, fast-forwarded and labeled existing → 0:30 the photos →
0:40 `suggest_ad_creatives` with each Meta model named as it runs → 1:20 the card: before/after pixels, claims
with sources, the Removed list ("licensed and insured" cut, never stated) → 2:00 "set it up", and the Google Ads
UI refreshes with the paused campaign and its images → 2:30 the repo, new vs existing, cost per run.

## 10. Decisions for the founder

1. **Publishing:** the import commit makes the whole Admiry agent package public, irreversibly. No license file
   is added (all rights reserved) unless you choose one.
2. **Real Google Ads writes:** created paused in the client account; after the demo removed or left paused.
3. **Demo photos:** a real roofing, remodel or landscaping job we have the rights to.
4. **Team list** and each member's own $150 credit claim, both due today.
