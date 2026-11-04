# Demo video script (3:00)

For the founder. Follows spec section 9. Everything the founder says to Muse is in quotes; captions are burned in
on screen. Total run time of the live parts is cut down by fast-forward where marked.

## Pre-flight checklist

- [ ] Signed in on admiry.ai in the browser.
- [ ] Muse connected to the Admiry connector at `https://agent.admiry.ai/mcp`.
- [ ] A roofing, remodel or landscaping business set up, with 3-5 job photos you have the rights to.
- [ ] Stripe test card ready for the allowlisted top-up: `4242 4242 4242 4242`, any future expiry, any CVC
      (Stripe's public test card).
- [ ] The approval code ready: it is shown on the signed-in plan page.
- [ ] Google Ads UI open on the managed client account, on the Campaigns list.
- [ ] Screen recorder on; notifications off; repo open in a second tab for the last beat.

## Beats

### 0:00 - 0:10  The problem

- **Say (voice-over):** "A roofer is good at roofs and has a phone full of job photos. Running Google Ads still
  means cropping images, writing 30-character headlines, and dodging claims they can't back."
- **On screen:** a phone camera roll of job photos, then an empty Google Ads asset screen.
- **Caption:** "Good at roofs. Not at ad specs."

### 0:10 - 0:30  Muse builds the site and plan (fast-forwarded)

- **Say to Muse:** "I'm a roofer in Austin, help me get customers."
- **On screen:** Muse conversation with Admiry connected; site preview and plan appear. Fast-forward 4x.
- **Caption:** "existing Admiry: site and plan in one conversation (built before the hackathon)"

### 0:30 - 0:40  The photos

- **Say to Muse:** "Here are my job photos." (Open the upload link on the phone, add 3-5 photos.)
- **On screen:** the phone upload page, the photos landing.
- **Caption:** "existing Admiry: the owner's own photos"

### 0:40 - 1:20  suggest_ad_creatives, model by model

- **Say to Muse:** "What ads would you run?"
- **On screen:** the tool call in Muse; as the run progresses, show the model names as they work (use the
  progress or the repo log side by side).
- **Captions, one per step as it runs:**
  - "Muse Spark reads the photos and writes the ads"
  - "SAM 3.1 finds the work in each photo"
  - "Muse Image extends only the margins to Google's sizes"
  - "Muse Wonder checks policy, seams and every claim"
- **Say (voice-over):** "Four Meta models, each with one job. The photo pixels stay the owner's: if the work fits
  a plain crop, it is a plain crop; otherwise our code pastes the whole original back in."

### 1:20 - 2:00  The card

- **Say to Muse:** (none, let the card sit) then voice-over: "Every claim shows the fact it came from."
- **On screen:** the creatives card. Toggle before/after on one image (original pixels vs the extended margins).
  Click a headline to show its source fact. Scroll to the **Removed** list.
- **Caption:** "Before/after: every photo pixel is the owner's"
- **Caption (claims):** "Each claim cites a fact the owner gave"
- **Caption (Removed):** "What the checker cut, and why"

### 2:00 - 2:30  Set it up

- **Say to Muse:** "Looks good, set it up."
- **On screen:** Muse approval step: enter the approval code from the plan page; top-up with the Stripe test
  card if asked. Muse answers with the campaign id and a link. Cut to the Google Ads UI: the campaign, status
  **Paused**, ad group, keywords, responsive search ad, the landscape and square images, the callouts.
- **Caption:** "Real Google Ads campaign, created live. Paused: nothing spends."

### 2:30 - 3:00  The repo

- **Say (voice-over):** "The imported agent is marked as pre-existing. Everything after it is new: the creative
  studio, the tool, the Google image and callout assets, and the paused live mode. A run costs about ten cents."
- **On screen:** GitHub repo README: the "Existing Admiry vs built during the hackathon" table, then the commit
  list after the import commit.
- **Caption:** "New: creatives, tool, Google assets, live_paused. About $0.10 per run."

## Notes

- Keep ids, e-mails, keys and customer ids out of frame (blur the campaign id in the recording if needed).
- If a run fails live, the card shows the plain-crop fallback and the Removed list; that is the honest path, say so.
