# `live_paused` smoke on dev — 2026-11-04

Plan Task 9a, ruling R27 (spec 2026-11-04-muse-verified-creatives-google-ads). Goal: the owner's accepted creative
set pushed into a real Google Ads campaign that is built PAUSED and never enabled. Test business: `biz_hackroofer`
(a roofer in Austin, owned by an allowlisted address). Dev agent at `ec92d3f`; the box's Google Search switch stayed
`validate` throughout (no `.env`/compose change, no restart).

Tool: `scripts/smoke_live_paused.py <business_id> --validate | --create | --readback`, piped into the agent
container (`python - <biz> --<step> < scripts/smoke_live_paused.py`). Customer IDs are masked in all output.

**Result: nothing was built.** The set was accepted and Google validated the creative assets, but the product's
create path cannot run for this business yet (see step 3). No campaign exists for it.

## 1. Accept the set (public tool)

`suggest_ad_creatives {"accept_set_id": "cs_2778580f296edee5"}` → `status: "accepted"`, set
`cs_2778580f296edee5`: 15 headlines, 4 descriptions, 2 callouts (`Free Estimates`, `Roof Repairs`), 6 images
(3 square, 3 landscape; 1.1–2.2 MB each).

## 2. `--validate` (validateOnly, nothing built)

- Built through the adapter as a launch does: `payload["creatives"]` **is** the accepted set (`True`).
- The business has no approved plan, so the newest plan (v1, draft) was used; it has no Google Ads account of its
  own (not linked, not prepaid), so the mutate went to the pool account the allocator would pick for it (`root`),
  read only — nothing bound. DB writes made on the way (the account's tag row) were rolled back; after the run the
  business still has 0 `conversion_action` and 0 `channel_campaign` rows.
- The create mutate (65 operations):

| operation | count |
|---|---|
| campaignBudgetOperation | 1 |
| campaignOperation | 1 |
| campaignCriterionOperation | 12 |
| adGroupOperation | 3 |
| adGroupCriterionOperation | 27 |
| adGroupAdOperation | 3 |
| assetOperation: IMAGE | 6 |
| campaignAssetOperation: AD_IMAGE | 6 |
| assetOperation: CALLOUT | 2 |
| campaignAssetOperation: CALLOUT | 2 |
| assetOperation: CALL | 1 |
| campaignAssetOperation: CALL | 1 |

- Each of the 3 responsive search ads carries exactly the 15 accepted headlines, in order; no pin (the first line,
  the business name, names no service).

**Google's answer: refused, HTTP 400 `INVALID_ARGUMENT`** (request id `Sm5e6S53HxqsndH_f_Uaqg`), three errors, all
the same, none on an asset operation:

| error code | message (verbatim) | trigger | field path |
|---|---|---|---|
| `urlFieldError: MISSING_PROTOCOL` | The protocol (http:// or https://) is missing. | `?src=gads` | `mutate_operations[24].ad_group_ad_operation.create.ad.final_urls[0]` |
| `urlFieldError: MISSING_PROTOCOL` | The protocol (http:// or https://) is missing. | `?src=gads` | `mutate_operations[35].ad_group_ad_operation.create.ad.final_urls[0]` |
| `urlFieldError: MISSING_PROTOCOL` | The protocol (http:// or https://) is missing. | `?src=gads` | `mutate_operations[46].ad_group_ad_operation.create.ad.final_urls[0]` |

Cause: the test business's page is an unpublished preview with an empty address, so each ad's final URL is just the
`?src=gads` suffix. A real launch never gets here: its precheck (`_launch_rules` → `_page_ready`) refuses a business
with no page first.

**Diagnostic re-run (not in the committed script):** the same mutate with the final URL stood in by Admiry's own dev
web address (`https://<dev web base>/?src=gads`), validateOnly, DB rolled back → **OK: Google accepted every
operation**, including the 6 `IMAGE` assets with their `AD_IMAGE` links and the 2 callouts with their `CALLOUT`
links. Nothing built.

## 3. `--create` (one real create, `live_paused` in memory only) — not run

The script checks what the product's create path (`ads_run.launch` → `_advance` → `_create`) needs before sending
anything. It refused (exit 2) and sent nothing:

| need | state |
|---|---|
| switch for this business under `live_paused` (allowlist) | `live_paused` ✓ |
| an approved plan funding Google Search | **missing**: only plan v1, a draft |
| a Google Ads account ready for the business | **missing**: "Your Google Ads account is not linked to Admiry yet; link it first, since your ads run in your own account" (`ads_mode` own_account, no link; not prepaid, balance 0, never paid, so the allocator cannot bind it to the pool) |
| a published page (every ad's final URL) | **missing**: preview only, no address |
| nothing built yet (one create only) | ✓ |

Per R27, no plan, payment, page or account was seeded.

## 4. `--readback` (GAQL)

Against the pool account the allocator would pick: campaigns named for `biz_hackroofer`, not removed → **none**.
No campaign assets, no ads. Nothing ENABLED (and so nothing to pause).

| campaign | status | AD_IMAGE | CALLOUT | CALL | RSA headlines = accepted |
|---|---|---|---|---|---|
| — (none built) | — | 0 | 0 | 0 | — |

## 5. What the create still needs

Before `--create` can build the campaign for `biz_hackroofer`:

1. a published page for the business that passes its page check (`_page_ready`; its address becomes every ad's final URL);
2. a Google Ads account: either a linked own account, or the business made prepaid (`managed`) with a payment,
   so the launch's allocator binds it to the pool account;
3. the plan approved (funding Google Search, as v1 does) by the owner.

Then `--create`, then `--readback` (expect PAUSED, 6 `AD_IMAGE`, 2 `CALLOUT`, 1 `CALL`, and the 15 accepted
headlines in each ad).

## 6. Second round (ruling R28: product tools and operator commands only, no Stripe) — stopped at step 2

Read-only checks; nothing was changed in this round.

| R28 step | what the product needs | state |
|---|---|---|
| 1. publish the page (`update_landing_page`) | a preview `token` to publish, then a passing page check | `get_landing_page` → "No landing page is set yet". For a home-services business `write` starts `build_site`: look, facts, the logo (or wordmark) and one real photo, then a preview to publish. Not started, because step 2 blocks the create anyway |
| 2. managed + funded (`agent.ops`) | a Google Ads account bound to the business | **blocked.** `adjust` credits a balance but "an operator's credit is no payment". `google-account bind` and the launch's allocator both refuse a business that never paid by card (`wallet.has_paid`: a `topup` entry, written only by Stripe's webhook; "never forced"). Without a binding, `check_account` answers FIRST_PAYMENT and the approval is refused |
| 3. approve the plan | `claim_state` verified, then the owner's approval code | the business is `claimed`, not verified (`agent.ops verify` exists; a card payment also verifies a prepaid business). The code is shown on the owner's signed-in plan page; `agent.ops issue-code` prints one "to read to the owner" |

The ways forward, either one:

- **Prepaid:** the owner makes one Stripe **test-mode** top-up. The sandbox path for allowlisted owners is in main as of
  `684146e`/`6f73da3`. That one payment verifies the business and lets the launch bind it to the pool account. The
  page, the approval, then `--create` and `--readback` follow.
- **Own account:** `agent.ops ads-link biz_hackroofer <customer_id> --status active` with a client account under
  Admiry's manager account that is not a pool account (pool accounts are refused as links), after the owner accepts
  the current management agreement.
