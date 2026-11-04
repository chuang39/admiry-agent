---
vertical: dentists
channels: [google_search]
tags: [stage:plan, stage:running, keywords, negatives]
sources:
  - id: research-verticals
    doc: "docs/research/2026-10-05-local-service-verticals.md"
    as_of: 2026-10-05
  - id: liq-search-2026
    url: "https://localiq.com/blog/search-advertising-benchmarks/"
    as_of: 2026-10-08
  - id: liq-health-2026
    url: "https://localiq.com/blog/healthcare-search-advertising-benchmarks/"
    as_of: 2026-10-09
  - id: meps-dental
    url: "https://meps.ahrq.gov/data_files/publications/st555/stat555.shtml"
    as_of: 2026-10-08
  - id: g-pa-health
    url: "https://support.google.com/adspolicy/answer/16701855?hl=en"
    as_of: 2026-10-09
  - id: lsa-us
    url: "https://support.google.com/localservices/answer/6224841?hl=en&co=GENIE.CountryCode%3DUS"
    as_of: 2026-10-09
  - id: lsa-req
    url: "https://support.google.com/localservices/answer/12174778?hl=en&co=GENIE.CountryCode%3DUS"
    as_of: 2026-10-09
  - id: g-limited
    url: "https://support.google.com/adspolicy/answer/13889491?hl=en"
    as_of: 2026-10-08
  - id: fl-456-062
    url: "http://www.leg.state.fl.us/statutes/index.cfm?App_mode=Display_Statute&URL=0400-0499/0456/Sections/0456.062.html"
    as_of: 2026-10-09
  - id: fl-dental-id
    url: "https://www.flrules.org/gateway/ruleNo.asp?id=64B5-4.002"
    as_of: 2026-10-15
confidence: medium
reviewed_by: founder
reviewed_at: 2026-10-15
---

# Dentists — Google Search

How patients search for a dentist, which searches to buy and which to keep out.

## Key points

- Give each service its own ads: emergencies want a call now, family dentistry a booking, implants, Invisalign and cosmetic work a free consultation, without "free" in Florida [research-verticals] [fl-456-062].
- Search themes: the service with the town or "near me"; an insurer's name only when the practice takes it.
- Keep out: dental jobs and schools, free or low-cost clinics, dental insurance plans, home remedies, supplies, pet dentistry.
- Implant, extraction, oral-surgery and gum-disease ads cannot use your data segments, Customer Match or lookalike segments (Google's Health category, our reading) [g-pa-health].
- Google Local Services lists "Dentist" and "Orthodontist"; other specialties have no entry (our reading: they sign up as Dentist) [lsa-us].

## Details

Patients search by service and place, such as "dentist near me", and each kind of search needs its own ad. Someone with a
toothache wants to call now and may search after hours, so an emergency ad shows the call button and the hours. Someone choosing
a family dentist books online. Implant, clear-aligner and cosmetic cases are large, so the ad offers a consultation and mentions
payment plans or financing [research-verticals]; "Invisalign" is a trademark, so our reading is that only a provider's ad text
names it. Florida, Texas, North Carolina, California and New York practices keep prices and discounts, and Florida practices
"free", out of the ad text (see the offers file).

The "keep out" searches are people looking for work or a school, a free or low-cost clinic, an insurance plan, a home remedy,
supplies or a pet's teeth. None of them is searching for the practice's own services, so they belong on the negative list.

Google Local Services lists "Dentist" and "Orthodontist" and no other dental specialty; that specialty practices sign up as
Dentist is our reading, not a Google statement [lsa-us]. For a health provider Google checks the provider and the practice
(background checks, state licenses), general liability insurance for the practice and malpractice insurance for the provider,
and the NPI number. On average, screening takes 3–4 weeks after the documents are submitted [lsa-req]. Pre-badge ads are not
available for health care, so no ad shows before the badge is earned [lsa-us].

A new Google Ads account may serve few ads at first. Google limits the ads of an advertiser it does not yet judge qualified,
and account maturity is one factor it weighs; our reading is that a new account is exposed [g-limited].

Google's Health category names surgical procedures, injections and treatments for chronic conditions, so our reading is that
implant, extraction, oral-surgery and gum-disease ads cannot use your data segments (remarketing lists), Customer Match, audience
expansion or lookalike segments; routine dentistry is not named [g-pa-health] [research-verticals].

## Numbers

- Dentists & Dental Services' average cost per lead in search advertising: $72.97, from LocaliQ's 2026 benchmarks (its own customers' Google Ads and Microsoft Ads campaigns) [liq-search-2026].
- By service, cost per lead in LocaliQ's healthcare search benchmarks (3,542 US search campaigns across 16 healthcare subcategories, at least 43 each, October 2024 to September 2025; its "averages" are medians): general dentistry $84.77, orthodontics $71.52, oral and maxillofacial surgery $55.15, emergency dentistry $75.19 [liq-health-2026].
- "dentist near me" ran 313 on Google Trends against 100 for "plumber near me" (US, the 12 months to 2026-10-05; relative interest, not search counts) [research-verticals].
- Dental spending per patient with a visit: $887 a year (2021, in 2021 dollars; payments from all sources; US civilian noninstitutionalized population aged 2 and older) [meps-dental].

## Do not say

- Another practice's name.
- "Always open" or "same day" unless the hours say so.
- "We take every insurance."
- "Sleep dentistry" in Florida without a general anesthesia permit [fl-dental-id].
