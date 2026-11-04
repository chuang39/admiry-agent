# Admiry on Muse

**Real work, true claims: a roofer's job photos become a verified Google Ads campaign in one conversation.**

Meta hackathon 2026 · Startups track · *Build for Your World*

Admiry is a marketing agent for local trades — roofers, remodelers, landscapers. An owner talks to **Muse**,
Meta's assistant, with the Admiry connector added. In that one conversation Admiry builds their website and
proposes a managed ad plan. **New in this hackathon:** Admiry turns the owner's own job photos into Google-ready
image ads and ad text, checks every claim against facts the owner gave and every image against Google's policy,
and, on the owner's OK, creates the Google Ads campaign in real time through the live Google Ads API. It is
created **paused**, so nothing is spent until the owner chooses.

<p align="center">
  <img src="docs/img/how-it-works.svg" width="100%" alt="Talk to Muse, share your job photos, and Admiry turns them into checked ads and a paused Google Ads campaign">
</p>

## The problem

A roofer is good at roofs and has a phone full of job photos. Running Google Ads still means writing headlines
under 30 characters, cropping images to Google's 1.91:1 and 1:1 sizes, and steering clear of claims they can't
back. Generic AI ad tools make the last part worse: they write "licensed and insured" for a business that never
said so, and they generate work the business never did.

Admiry does the reverse. The ads are made from the owner's real work and the owner's real facts, and anything it
can't verify is cut before it reaches Google.

## The demo

1. **In Muse:** "I'm a roofer in Austin, help me get customers." Admiry builds the site and the plan.
   *(existing Admiry)*
2. The owner adds 3–5 job photos from their phone through the upload link. *(existing Admiry)*
3. **"What ads would you run?"** → the new `suggest_ad_creatives` tool. Muse shows, per photo, a landscape and a
   square image ad plus headlines, descriptions and callouts:
   - each claim shows the profile fact it came from;
   - a **Removed** list says what the checker cut and why;
   - a before/after toggle shows which pixels are the owner's.
4. **"Looks good, set it up."** → the campaign, budget, ad group, keywords, responsive search ad, image assets and
   callout assets are created live in Google Ads, all paused. Muse answers that the campaign is built in Google Ads
   and paused; the Google Ads UI shows the campaign with the owner's images.

## How it stays honest

| Promise | How it is kept |
|---|---|
| Every claim has a source | Each headline, description and callout cites a fact from the owner's profile. A checker model rejects ungrounded claims and home-services compliance hits; rejected claims get at most two repair rounds, then are dropped. |
| The work in every image is the owner's | SAM finds the work and its box. If the whole work fits a plain crop to Google's size, the ad **is** a plain crop of the original. Otherwise Muse Image extends only the margins and our code pastes the **whole original photo** back in the middle (16 px feathered edge), so every photo pixel is the owner's. A SAM re-segmentation (IoU ≥ 0.9) and a Muse Wonder check (Google policy, seams, duplicated objects) must both pass, or the ad is the plain crop. |
| No text on images | Never drawn: Google's image-asset policy, and honesty. Checked on every output. |
| Nothing unchecked reaches Google | Anything that fails a check is not stored as accepted and is not sent. |
| Nothing spends | The `live_paused` mode sends the real create mutate but never enables a campaign, by any path. Allowlisted businesses only. |
| Owner content stays the owner's | Owner photos and facts go only to Standard-mode models (not Contributor mode, which allows training). |

## How it works

| Step | Model | What it does | If it fails |
|---|---|---|---|
| Read the photos | `muse-spark-1.3` (vision) | names the work ("new shingle roof"), judges ad suitability, flags faces, house numbers, plates and names | the photo is skipped |
| Cut out the work | `sam-3.1` | a mask of the real work | plain crop of the original |
| Extend to Google's sizes | `muse-image-1.0` | only if the work does not fit a plain crop: fills the margins (sky, yard); our code pastes the whole original photo back in the middle | plain crop of the original |
| Write the text | `muse-spark-1.3` | 15 headlines ≤ 30 chars, 4 descriptions ≤ 90, up to 4 callouts ≤ 25, each citing a fact | one retry with the validation error |
| Check the text | `muse-wonder-1.0` | grounded or not, compliance rule hits, per claim | `muse-spark-1.3` as checker, labeled |
| Check the images | `muse-wonder-1.0` + `sam-3.1` | Google image-asset policy, and the work's mask still where it was | plain crop of the original |

About **$0.10 per run** (4–6 images, SAM, and a few text calls).

## Existing Admiry vs built during the hackathon

**Pre-existing.** The whole [`agent/`](agent/) package was imported unchanged from Admiry's private repository
(at `6f6f213b`) in one commit, [`a9f6398`](https://github.com/chuang39/admiry-agent/commit/a9f6398). It is not
hackathon work. It contains the Muse connector (an MCP gateway with OAuth), the admiry.ai web chat, the website
builder, marketing plans, the managed Google Search campaign builder (validate-only on dev), the phone photo upload
page and the home-services knowledge. Its own docs: [`agent/README.md`](agent/README.md).

**Built during the hackathon.** Every commit after the import. Updated as each piece lands:

| # | Piece | Status |
|---|---|---|
| 0 | Run this repo's agent on the dev box and walk the existing flow: [`scripts/deploy-agent.sh`](scripts/deploy-agent.sh), [walk notes](docs/ops/dev-walk-2026-11-04.md) | done |
| N1 | **Creative studio** (`agent/creatives/`): job photos → Google image assets and ad text, each checked | done: [`8a297dc`](https://github.com/chuang39/admiry-agent/commit/8a297dc), [`1b97adb`](https://github.com/chuang39/admiry-agent/commit/1b97adb), [`28594aa`](https://github.com/chuang39/admiry-agent/commit/28594aa), [`1544c91`](https://github.com/chuang39/admiry-agent/commit/1544c91), [`13cbecb`](https://github.com/chuang39/admiry-agent/commit/13cbecb), [`dcb0a2c`](https://github.com/chuang39/admiry-agent/commit/dcb0a2c) |
| N2 | **Public tool `suggest_ad_creatives`** and the owner's acceptance, with a creatives card in Muse (over MCP) | done: [`e93ab4d`](https://github.com/chuang39/admiry-agent/commit/e93ab4d), [`056607b`](https://github.com/chuang39/admiry-agent/commit/056607b) |
| N3 | **Google builder additions:** landscape and square image assets and callout assets linked to the campaign; accepted text feeds the responsive search ad | done: [`f6f3fc2`](https://github.com/chuang39/admiry-agent/commit/f6f3fc2) |
| N4 | **`live_paused` execution mode:** the real create mutate, everything paused, never enabled | done: [`02a11d3`](https://github.com/chuang39/admiry-agent/commit/02a11d3) |
| N5 | README, run notes, demo video | in progress ([demo script](docs/demo-script.md)) |
| + | Stripe sandbox for allowlisted owners | done: [`684146e`](https://github.com/chuang39/admiry-agent/commit/684146e), [`6f73da3`](https://github.com/chuang39/admiry-agent/commit/6f73da3) |
| + | Closed JSON schema fix after the first live run | done: [`ec92d3f`](https://github.com/chuang39/admiry-agent/commit/ec92d3f) |
| + | Live runs and smoke | [docs/ops/live-paused-smoke-2026-11-04.md](docs/ops/live-paused-smoke-2026-11-04.md) |

Design: [spec](docs/superpowers/specs/2026-11-04-muse-verified-creatives-google-ads-design.md) ·
[implementation plan](docs/superpowers/plans/2026-11-04-muse-verified-creatives-google-ads.md)

## Run it locally

Needs Python 3.12, Postgres and a Meta Model API key.

```bash
uv venv --python 3.12
uv pip install -r agent/requirements.txt pytest ruff
createdb bidx_agent
export META_MODEL_API_KEY=...       # Meta Model API key
# optional, demo mode: real Google create, built paused (needs AGENT_LAUNCH_ALLOWLIST)
export AGENT_EXEC_GOOGLE_SEARCH=live_paused
# optional: Stripe sandbox for allowlisted owners (test-mode keys only; they pay with test cards)
export STRIPE_SANDBOX_SECRET_KEY=...
export STRIPE_SANDBOX_WEBHOOK_SECRET=...
.venv/bin/python -m agent           # http://localhost:8097/health, /mcp, /connect/docs
```

Mint a business key and point an assistant's MCP connector at `http://localhost:8097/mcp`
(`Authorization: Bearer ak_…`):

```bash
.venv/bin/python -m agent.keys mint --class business --business <business-id> --scopes read,manage,leads --label muse
```

Tests use their own database:

```bash
createdb admiry_hack_test
AGENT_TEST_DB_URL=postgresql:///admiry_hack_test .venv/bin/pytest
```

Lint with `ruff format` and `ruff check` (settings in [`pyproject.toml`](pyproject.toml), line length 120) on every
Python file you touch.

## Repository layout

| Path | What |
|---|---|
| [`agent/`](agent/) | the Admiry agent: FastAPI app, MCP endpoint at `/mcp`, Postgres schema, tools, site builder, Google Search channel, tests |
| [`agent/creatives/`](agent/creatives/) | the creative studio (N1): Meta Model API client, image geometry, claim ledger, studio |
| [`docs/superpowers/`](docs/superpowers/) | the hackathon design and implementation plan |
| [`docs/ops/`](docs/ops/) | run notes from the dev box |
| [`scripts/`](scripts/) | dev-box deploy, `smoke_live_paused.py` (validate / create / readback), `probe_models.py` |

## License

No license file: all rights reserved.
