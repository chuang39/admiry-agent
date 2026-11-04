---
vertical: attorneys
channels: [newsbreak]
tags: [stage:plan, newsbreak, creative]
sources:
  - id: channels-spec
    doc: "docs/superpowers/specs/2026-09-29-admiry-channels-design.md"
    as_of: 2026-10-08
  - id: aba-7-1
    url: "https://www.americanbar.org/groups/professional_responsibility/publications/model_rules_of_professional_conduct/rule_7_1_communication_concerning_a_lawyer_s_services/"
    as_of: 2026-10-08
  - id: aba-7-1-comment
    url: "https://www.americanbar.org/groups/professional_responsibility/publications/model_rules_of_professional_conduct/rule_7_1_communication_concerning_a_lawyer_s_services/comment_on_rule_7_1/"
    as_of: 2026-10-09
  - id: aba-7-2
    url: "https://www.americanbar.org/groups/professional_responsibility/publications/model_rules_of_professional_conduct/rule_7_2_advertising/"
    as_of: 2026-10-08
  - id: tx-bar-ads
    url: "https://www.texasbar.com/tdrpc/"
    as_of: 2026-10-09
  - id: nb-legal
    url: "https://biz.newsbreak.com/legal"
    as_of: 2026-10-08
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
  - id: fl-bar-handbook
    url: "https://www-media.floridabar.org/uploads/2025/12/Handbook-2025-Approved-by-SCA-12-10-25.pdf"
    as_of: 2026-10-15
  - id: ca-bp-6157-2
    url: "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=BPC&sectionNum=6157.2"
    as_of: 2026-10-15
confidence: low
reviewed_by: founder
reviewed_at: 2026-10-15
---

# Attorneys — NewsBreak

What a law firm can expect from NewsBreak's local news feed, and what its ads and page need there.

## Key points

- NewsBreak shows image ads to people reading local news, not to people searching: it can make an estate-planning or injury firm known nearby, not catch an arrest call.
- NewsBreak publishes no cost per lead for local services, law firms included [channels-spec].
- Write headlines that name the practice area and the town.
- Bar rules hold in every medium: name a responsible lawyer or the firm (Texas: a lawyer) and the office's city, and promise nothing [aba-7-2] [tx-bar-ads] [fl-bar-handbook] [ca-bp-6157-2] [aba-7-1].
- The page needs terms of use, a privacy policy and the firm's contact details [nb-legal].

## Details

NewsBreak is a local news feed. Its ads reach people while they read, not while they search, so a law firm's ad introduces the firm to the neighborhood instead of answering an urgent need; an arrest or DUI call comes from search, where the person is already looking. Admiry builds image ads from the plan's headlines and sends the reader to the firm's page [channels-spec].

Today Admiry adds NewsBreak to a law firm's plan only when the owner names it. The channel's "fit" flag in Admiry's consultation trees marks the project trades that make up NewsBreak's lead-gen volume, and today no legal service carries it. The money comes from the business's balance, at NewsBreak's minimum unless the owner names more. NewsBreak's help page gives the minimum budget as $10 and sets daily budgets at the ad-set level; Admiry reads that as $10 a day on each ad set, a floor of $300 a month for the channel [channels-spec] [nb-cost].

Targeting is by ZIP code, which NewsBreak recommends for local service providers; Admiry picks the ZIP codes inside the plan's radius [nb-targeting] [channels-spec]. NewsBreak reviews each ad; its API example's status text says the review usually takes one to two work days [nb-api-ad]. A headline is 90 characters or less, by NewsBreak's rejection example [nb-ad-specs].

Admiry sets a law firm's NewsBreak account to the industry "Legal Services//Lawyers", a value on NewsBreak's industry list [nb-industries] [channels-spec]. NewsBreak's industry-trends page has no row for legal services and calls its ranges broad, so our reading is that its Lead Generation range is only a rough guide [nb-trends].

ABA Model Rule 7.1 governs all communications about a lawyer's services, whatever means are used, and Rule 7.2 asks every communication to name a responsible lawyer or firm [aba-7-1-comment] [aba-7-2]. NewsBreak's advertising policy also bars exaggerated claims, including any unreasonable claim about the effectiveness of a service. Its terms make the advertiser warrant that each landing page carries terms of use, a privacy policy and the advertiser's contact information, and its policy asks for a contact link or section with an actual email address, phone number or physical address [nb-legal]. Admiry sends NewsBreak no lead's personal details, so nothing about a client or a matter: attribution runs on NewsBreak's click id alone [channels-spec].

## Numbers

- NewsBreak's minimum budget to run ads: $10; its help page sets daily budgets at the ad-set level [nb-cost].
- Cost per click on NewsBreak's industry-trends page (data for July 2026): $0.37–$1.61 for Lead Generation; the page has no row for legal services and calls its ranges broad, for general direction only [nb-trends].
- Headlines: 90 characters or less (NewsBreak's rejection help article gives this as the fix for an over-long headline) [nb-ad-specs].

## Do not say

- "Guaranteed" or a promised outcome.
- A past result or a settlement amount.
