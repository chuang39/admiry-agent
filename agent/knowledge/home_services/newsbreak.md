---
vertical: home_services
channels: [newsbreak]
tags: [stage:plan, newsbreak, creative]
sources:
  - id: channels-spec
    doc: "docs/superpowers/specs/2026-09-29-admiry-channels-design.md"
    as_of: 2026-10-08
  - id: nb-leadgen
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/46673553566477-Lead-Gen-Campaigns-on-NewsBreak-Ads-Guide-to-Launching-and-Scaling"
    as_of: 2026-10-08
  - id: nb-targeting
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/43986606295309-Targeting-on-NewsBreak-Ads-Manager"
    as_of: 2026-10-08
  - id: nb-legal
    url: "https://biz.newsbreak.com/legal"
    as_of: 2026-10-08
  - id: nb-api-ad
    url: "https://advertising-api.newsbreak.com/hc/en-us/articles/37869297161741-Create-an-Ad"
    as_of: 2026-10-08
  - id: nb-pixel
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/46524013485837-NewsBreak-Pixel-Quick-Verification-Checklist"
    as_of: 2026-10-08
  - id: nb-events
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/44376215592589-Set-Up-Event-Tracking"
    as_of: 2026-10-15
  - id: nb-trends
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/47859331637133-Industry-Trends-Continuously-Updated"
    as_of: 2026-10-08
  - id: nb-industries
    url: "https://docs.google.com/spreadsheets/d/1Mptt6K_xebdzP0kWh5Jourfkciii0OnTK6mUJUSVTKw"
    as_of: 2026-10-08
  - id: nb-cost
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/42303156200461-NewsBreak-Advertising-Cost"
    as_of: 2026-10-08
  - id: nb-ad-specs
    url: "https://adhelp.newsbreak.com/hc/en-us/articles/42374536424205"
    as_of: 2026-10-08
confidence: low
reviewed_by: founder
reviewed_at: 2026-10-15
---

# Home services — NewsBreak

Why NewsBreak suits home-improvement projects, and what its ads and the business's page need there.

## Key points

- NewsBreak shows image ads to people reading local news, not searching: it suits projects a homeowner plans (windows, solar, roofing, bathrooms), not emergencies (our reading) [nb-leadgen] [channels-spec].
- Headlines name the project, the town and the owner's own offer, not a search phrase.
- Target by ZIP code, NewsBreak's own advice for local services [nb-targeting].
- The page needs terms of use, a privacy policy and the business's contact details [nb-legal].
- NewsBreak reviews each ad; its API example's status text says the review usually takes one to two work days [nb-api-ad].

## Details

NewsBreak is a local news feed. Its ads reach people while they read, not while they search, so a home-improvement ad introduces
the project and the business to the neighborhood instead of answering an urgent need. Admiry builds image ads from the plan's
headlines and sends the reader to the business's page [channels-spec]. NewsBreak's help page puts home improvement at 31 % of its
lead-gen volume and names windows, solar, roofing and bathrooms inside it; it does not mention emergencies, and keeping those on
Google Search is our reading [nb-leadgen].

Admiry adds NewsBreak to a home-services plan as a $300 test from $800 a month when the service is flagged as a fit, and gives it
15 % from $1,500; the flagged services are project trades, such as windows, solar, roofing, bathroom and kitchen remodeling, HVAC
replacement and landscaping projects. The money comes from the business's balance.
NewsBreak's help page gives the minimum budget as $10 and sets daily budgets at the ad-set level; Admiry reads that as $10 a day on
each ad set, a floor of $300 a month for the channel. NewsBreak publishes no cost per lead for a local service [channels-spec]
[nb-cost].

Targeting is by ZIP code, which NewsBreak recommends for local service providers (its example is HVAC); Admiry picks the ZIP codes
inside the plan's radius [nb-targeting] [channels-spec]. NewsBreak reviews each ad; its API example's status text says the review
usually takes one to two work days [nb-api-ad]. A headline is 90 characters or less, by NewsBreak's rejection example
[nb-ad-specs].

Admiry sets a home-services business's NewsBreak account to the industry "Home and Garden Services//Home Improvement and Repair",
a value on NewsBreak's industry list [nb-industries] [channels-spec].

NewsBreak's terms make the advertiser warrant that each landing page carries terms of use, a privacy policy and the advertiser's
contact information, and its policy asks for a contact link or section with an actual email address, phone number or physical
address. Its policy also bars hyperbolic claims, and asks financial services to follow state and local rules with adequate
disclosures; it does not say whether an ad that offers financing counts [nb-legal]. Our reading is that the compliance file's
license-number and "bonded" rules hold on NewsBreak as on Google. Admiry sends NewsBreak no customer's details: attribution runs
on NewsBreak's click id alone [channels-spec]; NewsBreak's pixel checklist calls its click id, nb_cid, essential for attributing
conversions [nb-pixel].

## Numbers

- NewsBreak's minimum budget to run ads: $10; its help page sets daily budgets at the ad-set level [nb-cost].
- Share of NewsBreak's lead-gen volume by category: Finance 32.44 %, Home Improvement 31.00 %, Health and Wellness 18.27 %, Commerce 18.25 %; inside Home Improvement: Window 25 %, Solar Installation 24 %, Roofing 21 %, Bathroom 15 % (historical figures meant as guides; the four add to 85 % and the page names nothing else) [nb-leadgen].
- Cost per click on NewsBreak's industry-trends page (data for July 2026): $0.37–$1.61 for Lead Generation; the page has no row for home services and calls its ranges broad, for general direction only; Admiry's channels spec reads this range as national lead aggregators', not single-location businesses' [nb-trends] [channels-spec].
- Headlines: 90 characters or less (NewsBreak's rejection help article gives this as the fix for an over-long headline) [nb-ad-specs].
- A click id is valid for 7 days, and a postback sent later fails with a 400 error (NewsBreak's event-tracking help, as the channels spec records it) [nb-events] [channels-spec].

## Do not say

- "Emergency" or a fast-arrival promise.
- A price or a discount the business has not stated.
