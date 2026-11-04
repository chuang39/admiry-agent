# Admiry knowledge — index

What Admiry asserts about advertising local services: curated files, reviewed by the founder and versioned in git
(capability layer spec `docs/superpowers/specs/2026-10-07-admiry-capability-layer-design.md` §6.1). A file is
`<vertical>/<topic>.md`, its vertical a consultation tree's id; its id is `<vertical>/<topic>`. A plan draft reads the
matching files' Key points and Do not say lines within 1,500 tokens (`agent/knowledge_index.py`); an owner's assistant
reads this index as `admiry://knowledge` and a file as `admiry://knowledge/<vertical>/<topic>`. A file waiting for its
review says `reviewed_by: pending` and is never read by a model or served.

## Files

The table between the two markers is written by `.venv/bin/python -m agent.knowledge_index index`; never edit it by
hand. `.venv/bin/python -m agent.knowledge_index preview <vertical> --channel google_search --topic compliance` prints
what a draft reads.

<!-- files:begin -->
| Id | Channels | Tags | Summary | Sources | Confidence | Reviewed |
|---|---|---|---|---|---|---|
| attorneys/compliance | any | stage:plan, ad-text, disclosures | Bar rules every law firm's ads keep, the states that add their own, and what Google limits. | 15 | medium | 2026-10-15 (founder) |
| attorneys/google-search | google_search | stage:plan, stage:running, keywords, negatives | How people search for a lawyer by practice area, which searches to buy and which to keep out. | 14 | medium | 2026-10-15 (founder) |
| attorneys/newsbreak | newsbreak | stage:plan, newsbreak, creative | What a law firm can expect from NewsBreak's local news feed, and what its ads and page need there. | 14 | low | 2026-10-15 (founder) |
| attorneys/offers | any | stage:plan, offers, ad-text | What law firms lead with in their ads, and the bar rules each offer brings. | 5 | low | 2026-10-15 (founder) |
| dentists/compliance | any | stage:plan, ad-text, disclosures | Claims, offers and targeting a dental practice's ads must avoid, and the notices its page must carry. | 21 | medium | 2026-10-15 (founder) |
| dentists/google-search | google_search | stage:plan, stage:running, keywords, negatives | How patients search for a dentist, which searches to buy and which to keep out. | 10 | medium | 2026-10-15 (founder) |
| dentists/newsbreak | newsbreak | stage:plan, newsbreak, creative | What a dental practice can expect from NewsBreak's local news feed, and what its ads and page need there. | 9 | low | 2026-10-15 (founder) |
| dentists/offers | any | stage:plan, offers, ad-text | Offers that move dental patients to book, and the rules that come with them. | 9 | low | 2026-10-15 (founder) |
| home_services/compliance | any | stage:plan, ad-text, disclosures, licensing | License numbers, verification and claims a home-services business's ads must get right, state by state. | 28 | medium | 2026-10-15 (founder) |
| home_services/electrical | google_search | stage:plan, keywords, negatives, trade:electrical | How an electrician's ads catch repairs and upgrades, and which searches to keep out. | 10 | medium | 2026-10-15 (founder) |
| home_services/google-search | google_search | stage:plan, stage:running, keywords, negatives | How homeowners search for a trade, which searches to buy and which to keep out. | 7 | medium | 2026-10-15 (founder) |
| home_services/hvac | google_search | stage:plan, keywords, negatives, trade:hvac | How heating and cooling ads catch breakdowns, replacements and maintenance plans, and which searches to keep out. | 11 | medium | 2026-10-15 (founder) |
| home_services/landscaping | google_search | stage:plan, keywords, negatives, trade:landscaping | How a landscaper's ads catch design projects and recurring lawn care, and which searches to keep out. | 14 | medium | 2026-10-15 (founder) |
| home_services/newsbreak | newsbreak | stage:plan, newsbreak, creative | Why NewsBreak suits home-improvement projects, and what its ads and the business's page need there. | 11 | low | 2026-10-15 (founder) |
| home_services/offers | any | stage:plan, offers, ad-text | Offers that move homeowners to call or ask for a quote, and the rules each brings. | 7 | low | 2026-10-15 (founder) |
| home_services/plumbing | google_search | stage:plan, keywords, negatives, trade:plumbing | How a plumber's ads catch emergencies and bigger jobs, and which searches to keep out. | 11 | medium | 2026-10-15 (founder) |
| home_services/remodel | google_search | stage:plan, keywords, negatives, trade:remodel | How a remodeler's ads catch kitchen and bathroom projects, and which searches to keep out. | 11 | medium | 2026-10-15 (founder) |
| home_services/roofing | google_search | stage:plan, keywords, negatives, trade:roofing | How a roofer's ads catch repairs, replacements and storm damage, and which searches to keep out. | 8 | medium | 2026-10-15 (founder) |
<!-- files:end -->

## The research taxonomy and the code's categories

`docs/research/2026-10-05-local-service-verticals.md` sorts 361 verticals into 26 groups (§2.1–§2.26); the code knows
11 categories (`models.Category`, `benchmark.CATEGORIES`). A row's category is its Tree column when that names one of
the 11 (the research's "existing benchmark family"), else `fallback`: the research's proposed families
`education_childcare`, `events`, `senior_care`, `mental_health` and `funeral` have no category yet. Only
`home_services`, `dentists` and `attorneys` have a consultation tree of their own; every other category runs the
`fallback` consultation (`consult.tree_for`), so its knowledge, when written, lives under `fallback/` until its tree
exists. `agent/tests/test_knowledge_files.py` counts the research document's tables again and fails when this table
differs.

| § | Research group | Rows | Code categories (rows) | Knowledge (batch 1) |
|---|---|---|---|---|
| 2.1 | Home services — repair and trades | 66 | home_services 62 · fallback 4 | home_services: compliance, google-search, offers, newsbreak; plumbing, hvac, roofing, electrical, remodel |
| 2.2 | Home services — outdoor and property | 16 | home_services 15 · fallback 1 | home_services (as 2.1); landscaping |
| 2.3 | Cleaning | 7 | home_services 4 · fallback 3 | home_services (as 2.1), for its home_services rows |
| 2.4 | Moving, hauling, storage and transportation | 9 | fallback 5 · home_services 4 | home_services (as 2.1), for its home_services rows |
| 2.5 | Auto services | 19 | automotive_repair 17 · fallback 2 | — |
| 2.6 | Health — medical and allied health | 27 | physicians 26 · fallback 1 | — |
| 2.7 | Aesthetics and cosmetic medicine | 6 | physicians 4 · personal_services 2 | — |
| 2.8 | Dental | 11 | dentists 10 · fallback 1 | dentists: compliance, google-search, offers, newsbreak |
| 2.9 | Mental health and therapy | 9 | fallback 9 | — |
| 2.10 | Wellness and alternative care | 9 | physicians 4 · fallback 2 · personal_services 2 · health_fitness 1 | — |
| 2.11 | Beauty and personal care | 13 | personal_services 10 · fallback 2 · physicians 1 | — |
| 2.12 | Fitness and sports instruction | 13 | fallback 7 · health_fitness 6 | — |
| 2.13 | Pets | 11 | animals_pets 10 · fallback 1 | — |
| 2.14 | Legal | 25 | attorneys 20 · fallback 5 | attorneys: compliance, google-search, offers, newsbreak |
| 2.15 | Financial, tax and insurance | 14 | finance_insurance 12 · fallback 2 | — |
| 2.16 | Real estate and property | 10 | real_estate 6 · fallback 3 · home_services 1 | — |
| 2.17 | Childcare, senior care and family services | 11 | fallback 11 | — |
| 2.18 | Education, tutoring and lessons | 14 | fallback 14 | — |
| 2.19 | Events and weddings | 16 | fallback 16 | — |
| 2.20 | Photography, video and creative | 11 | fallback 11 | — |
| 2.21 | Tech and electronics repair | 7 | fallback 7 | — |
| 2.22 | Business, government-document and professional services | 12 | fallback 12 | — |
| 2.23 | Funeral and memorial | 6 | fallback 5 · animals_pets 1 | — |
| 2.24 | Religious, nonprofit and community services | 2 | fallback 2 | — |
| 2.25 | Hospitality-adjacent and food services | 9 | fallback 9 | — |
| 2.26 | Personal and household services | 8 | fallback 8 | — |
