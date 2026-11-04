---
vertical: dentists
channels: [newsbreak]
tags: [stage:plan, newsbreak, creative]
sources:
  - id: channels-spec
    doc: "docs/superpowers/specs/2026-09-29-admiry-channels-design.md"
    as_of: 2026-10-08
  - id: nb-leadgen
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/46673553566477-Lead-Gen-Campaigns-on-NewsBreak-Ads-Guide-to-Launching-and-Scaling"
    as_of: 2026-10-08
  - id: nb-legal
    url: "https://biz.newsbreak.com/legal"
    as_of: 2026-10-09
  - id: nb-cost
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/42303156200461-NewsBreak-Advertising-Cost"
    as_of: 2026-10-08
  - id: nb-targeting
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/43986606295309-Targeting-on-NewsBreak-Ads-Manager"
    as_of: 2026-10-08
  - id: nb-api-ad
    url: "https://advertising-api.newsbreak.com/hc/en-us/articles/37869297161741-Create-an-Ad"
    as_of: 2026-10-08
  - id: nb-industries
    url: "https://docs.google.com/spreadsheets/d/1Mptt6K_xebdzP0kWh5Jourfkciii0OnTK6mUJUSVTKw"
    as_of: 2026-10-08
  - id: nb-trends
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/47859331637133-Industry-Trends-Continuously-Updated"
    as_of: 2026-10-08
  - id: nb-ad-specs
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/42374536424205"
    as_of: 2026-10-08
confidence: low
reviewed_by: founder
reviewed_at: 2026-10-15
---

# Dentists — NewsBreak

What a dental practice can expect from NewsBreak's local news feed, and what its ads and page need there.

## Key points

- NewsBreak shows image ads to people reading local news, not to people searching: it makes the practice known nearby rather than filling tomorrow's chair.
- NewsBreak's help pages give no cost per lead for dental practices; about three in ten of its lead-gen volume is home improvement [nb-leadgen].
- Write headlines that name the service and the town, not a search phrase.
- Healthcare rules hold in every medium: the dentist named where the state asks, no promised results, no patient stories without permission.
- The page needs terms of use, a privacy policy and the practice's contact details [nb-legal].

## Details

NewsBreak is a local news feed. Its ads reach people while they read, not while they search, so the ad introduces the practice to
the neighborhood instead of answering a need of the moment. Admiry builds image ads from the plan's headlines and sends the
reader to the practice's page [channels-spec].

Today Admiry adds NewsBreak to a dental plan only when the owner names it. The channel's "fit" flag in Admiry's consultation
trees marks the project trades that make up NewsBreak's lead-gen volume, and today no dental service carries it. The money comes
from the business's balance, at NewsBreak's minimum unless the owner names more. NewsBreak's help page gives the minimum budget
as $10 and sets daily budgets at the ad-set level; Admiry reads that as $10 a day on each ad set, a floor of $300 a month for the
channel [channels-spec] [nb-cost].

Targeting is by ZIP code, which NewsBreak recommends for local service providers; Admiry picks the ZIP codes inside the plan's
radius [nb-targeting] [channels-spec]. NewsBreak reviews each ad; its API example's status text says the review usually takes one
to two work days [nb-api-ad]. A headline is 90 characters or less, by NewsBreak's rejection example [nb-ad-specs].

Admiry sets a dental practice's NewsBreak account to the industry "Health and Medical Services//Dental Care", a value on
NewsBreak's industry list [nb-industries] [channels-spec].

NewsBreak's advertising policy says health and cosmetics content must not make illegitimate or unproven claims about results or
lack the disclosures the law requires, and it restricts medical advice given for commercial purposes. Its terms make the
advertiser warrant that each landing page carries terms of use, a privacy policy and the advertiser's contact information, and
its policy asks for a contact link or section with an actual email address, phone number or physical address [nb-legal]. A
patient's name, story or photo needs the patient's written permission, as the compliance file says. Admiry sends NewsBreak no
lead's personal details, so no patient's details: attribution runs on NewsBreak's click id alone [channels-spec].

## Numbers

- NewsBreak's minimum budget to run ads: $10; its help page sets daily budgets at the ad-set level [nb-cost].
- Share of NewsBreak's lead-gen volume by category: Finance 32.44 %, Home Improvement 31.00 %, Health and Wellness 18.27 %, Commerce 18.25 % (historical figures meant as guides; dental is not broken out) [nb-leadgen].
- Cost per click on NewsBreak's industry-trends page (data for July 2026): $0.37–$1.61 for Lead Generation and $0.48–$1.00 for Health & Wellness; the page calls the ranges broad and for general direction only [nb-trends].
- Headlines: 90 characters or less (NewsBreak's rejection help article gives this as the fix for an over-long headline) [nb-ad-specs].

## Do not say

- "Pain-free" or a promised result.
- A patient's story, name or photo.
