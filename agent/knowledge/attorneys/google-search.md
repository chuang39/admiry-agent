---
vertical: attorneys
channels: [google_search]
tags: [stage:plan, stage:running, keywords, negatives]
sources:
  - id: research-verticals
    doc: "docs/research/2026-10-05-local-service-verticals.md"
    as_of: 2026-10-05
  - id: liq-search-2026
    url: "https://localiq.com/blog/search-advertising-benchmarks/"
    as_of: 2026-10-08
  - id: liq-legal
    url: "https://localiq.com/blog/legal-search-advertising-benchmarks/"
    as_of: 2026-10-08
  - id: pareto-legal-lsa
    url: "https://pareto.legal/local-services-ads-statistics/"
    as_of: 2026-10-08
  - id: iii-auto
    url: "https://www.iii.org/fact-statistic/facts-statistics-auto-insurance"
    as_of: 2026-10-08
  - id: g-pa-crime
    url: "https://support.google.com/adspolicy/answer/16701956?hl=en"
    as_of: 2026-10-08
  - id: g-pa-rel
    url: "https://support.google.com/adspolicy/answer/16700849?hl=en"
    as_of: 2026-10-08
  - id: g-pa-fin
    url: "https://support.google.com/adspolicy/answer/16700443?hl=en"
    as_of: 2026-10-08
  - id: lsa-us
    url: "https://support.google.com/localservices/answer/6224841?hl=en&co=GENIE.CountryCode%3DUS"
    as_of: 2026-10-08
  - id: lsa-req
    url: "https://support.google.com/localservices/answer/12174778?hl=en&co=GENIE.CountryCode%3DUS"
    as_of: 2026-10-08
  - id: g-gov
    url: "https://support.google.com/adspolicy/answer/13156083?hl=en"
    as_of: 2026-10-09
  - id: fl-bar-handbook
    url: "https://www-media.floridabar.org/uploads/2025/12/Handbook-2025-Approved-by-SCA-12-10-25.pdf"
    as_of: 2026-10-09
  - id: tx-bar-ads
    url: "https://www.texasbar.com/tdrpc/"
    as_of: 2026-10-09
  - id: g-bail
    url: "https://support.google.com/adspolicy/answer/13527531?hl=en"
    as_of: 2026-10-15
confidence: medium
reviewed_by: founder
reviewed_at: 2026-10-15
---

# Attorneys — Google Search

How people search for a lawyer by practice area, which searches to buy and which to keep out.

## Key points

- One practice area per ad group: arrests and DUI want a call now; injury, family, estate and immigration a consultation [research-verticals].
- Search themes: "<practice> lawyer" or "attorney" with the town or "near me"; "free consultation" only when it is free.
- Keep out: law school, paralegal and lawyer jobs or salaries, legal aid and pro bono, forms and do-it-yourself kits, court and arrest records, areas the firm does not take.
- Criminal defense, divorce and bankruptcy ads cannot use Customer Match, your data segments, audience expansion or lookalike segments [g-pa-crime] [g-pa-rel] [g-pa-fin].
- Google Local Services offers lawyer categories, each checked against the state bar [lsa-us] [lsa-req].

## Details

Searchers name a practice and a place, such as "DUI lawyer near me", and each practice needs its own ads. The research document gives a call as the action for criminal defense and DUI, and a free consultation for injury, family, estate and immigration, so each ad leads with its practice's action [research-verticals].

Estate and probate had the lowest cost per lead of LocaliQ's legal categories in its 2022-23 data, which suits a small budget (the research document's reading). Personal injury had the highest cost per lead and, the research document says, the highest value per case: an average auto bodily-injury liability claim was paid at $28,278 in 2024 [liq-legal] [iii-auto] [research-verticals].

Google Local Services lists lawyer categories and checks each lawyer's state bar license [lsa-us] [lsa-req]. In one vendor's study of plaintiff-side firms, a Local Services lead cost more than a Google Ads lead and a signed case cost less (see the Numbers) [pareto-legal-lsa].

The research document equates the "your data segments" that the three practice areas cannot use with remarketing lists. Google's own audiences, in-market and affinity segments and location targeting stay available [g-pa-crime] [g-pa-rel] [g-pa-fin] [research-verticals].

Google's government documents policy lets only certified governments and authorized providers promote direct acquisition of documents such as ESTA, tourism or student visas, a DV lottery entry, an EIN or a name change; it names no law firms, so our reading is that a firm's ad offering to obtain one of these directly may need that certification [g-gov].

A firm that works in another language says so in its ads and has its page in that language; the research document lists "languages spoken" for injury firms and "multilingual content" for immigration firms [research-verticals]. In Florida and Texas an ad carries its required statements in each language it uses, though a line saying a language is spoken does not trigger this [fl-bar-handbook] [tx-bar-ads].

## Numbers

- Attorneys & Legal Services' average cost per lead in search advertising: $131.63, from LocaliQ's 2026 benchmarks (its own customers' Google Ads and Microsoft Ads campaigns) [liq-search-2026].
- By practice, cost per lead in LocaliQ's legal search benchmarks (256 US search campaigns, April 2022 to March 2023; its "averages" are medians): personal injury $159.17, criminal $101.49, bankruptcy $82.27, estate and probate $72.24, family $103.54, tax $120.30, general practice $96.54; all legal $111.05 [liq-legal].
- Local Services against Google Ads for law firms, in one vendor's study of 13 plaintiff-side firms and $3.3M of combined 2025 spend: $205 against $95 a lead; $2,485 against $2,971 per signed case [pareto-legal-lsa].
- Average auto bodily-injury liability claim paid: $28,278 in 2024 (ISO data for private passenger auto, all limits combined, including loss adjustment expenses, excluding Massachusetts and most no-fault states) [iii-auto].

## Do not say

- Another firm's or lawyer's name, in the ad or as a search term [fl-bar-handbook].
- "Free consultation" when the first meeting is billed.
- "Available around the clock" unless it is.
- Bail bond services: Google bans the ads, immigration bail included [g-bail].
