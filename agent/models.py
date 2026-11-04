"""Shapes the Business Agent fills and the owner sees (spec §4.1, §4.2 row 1).

They double as the structured-output schemas the model fills (agent/llm.py), so every field carries
the description the model reads, every field is required on the wire (nullable where unknown; the support fields below have Python defaults but stay required on the wire), and no field
has a constraint the API grammar might not enforce — limits (ad text lengths, budgets) are applied
in code afterwards (agent/plans.py)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Category = Literal[
    "home_services",
    "attorneys",
    "finance_insurance",
    "real_estate",
    "dentists",
    "physicians",
    "health_fitness",
    "automotive_repair",
    "animals_pets",
    "personal_services",
    "fallback",
]  # == benchmark.CATEGORIES (test-pinned)
Channel = Literal[
    "google_search",
    "newsbreak",
    "microsoft_search",
    "meta",
    "nextdoor",
    "lsa",
    "reddit",
    "chatgpt_ads",
]  # == channels.registry.ORDER (test-pinned): stored plans and adjust's `channel`
# What a draft may propose (SYSTEM_PLAN: "Use only google_search and meta"): the allocator decides what is funded, and
# the legacy create_plan path, which funds the draft as written, can never fund a channel no adapter runs.
DraftChannel = Literal["google_search", "meta"]


class Service(BaseModel):
    name: str = Field(description="One service the business sells, in the owner's words, e.g. 'Emergency plumbing'.")
    intent_phrases: list[str] = Field(
        description="3-6 phrases a customer types when they need this service; no brand names."
    )
    price_hint: str | None = Field(description="A price or range if the owner or the site states one, else null.")


class GoalHint(BaseModel):
    target_customers_per_month: int | None = Field(
        description="New customers or leads per month the owner asked for, if stated, else null."
    )
    monthly_budget_usd: float | None = Field(description="Monthly advertising budget in USD if stated, else null.")
    focus_services: list[str] = Field(description="Services the owner singled out; empty when none.")


class Knowledge(BaseModel):
    """Business Knowledge: what Admiry knows about one business (versioned in the knowledge table)."""

    name: str = Field(description="The business name as the owner or the site writes it.")
    category: Category = Field(description="The closest benchmark category; 'fallback' when none fits.")
    category_detail: str = Field(
        description="The trade or specialty in plain words, e.g. 'residential plumber', 'family dentist'."
    )
    service_area: list[str] = Field(
        description="Cities, neighbourhoods or a radius the business serves, as stated; empty when unknown."
    )
    stated_address: str | None = Field(
        default=None,
        description="The business's own street address or ZIP code exactly as the owner's own words (<owner>) give "
        "it, never from the website or a listing; null when they name only a town or an area.",
    )
    services: list[Service] = Field(description="Every distinct service named by the owner or the site (at least one).")
    hours: str | None = Field(description="Opening hours as written, else null.")
    phone: str | None = Field(description="The business phone number exactly as written, else null.")
    website: str | None = Field(description="The website URL if known, else null.")
    pricing_hints: list[str] = Field(
        description="Prices, fees or 'free estimate' style statements found in the text or site; empty when none."
    )
    typical_customer_value_usd: float | None = Field(
        description="Roughly what one new customer spends with this business, in USD: from the prices in the text "
        "or site when stated, else a typical figure for this trade (a florist about 60, a plumber about 400, a "
        "dentist about 2500). Null only when the trade is unclear."
    )
    usp: list[str] = Field(
        description="Up to 3 reasons customers pick this business, taken from the text or site only — never invented."
    )
    reviews_summary: str | None = Field(description="One sentence on reviews if the site shows them, else null.")
    compliance_flags: list[str] = Field(
        description="Regulated or sensitive areas that constrain ads: 'healthcare', 'legal', 'financial', 'housing', 'alcohol', 'cannabis'; empty when none."
    )
    locale: str = Field(description="BCP-47 locale of the owner, e.g. 'en-US'.")
    goal_hint: GoalHint = Field(
        description="What the owner said they want: customers per month, budget, focus services."
    )


def _all_required(schema: dict) -> None:
    """The wire schema lists every field as required -- the module rule: the model decides each one -- even where Python
    gives a default, so reads built by hand (tests, fixtures, older callers) need not name the support fields; keep
    default off the wire (the API's accepted subset is unverified)."""
    schema["required"] = list(schema["properties"])
    for prop in schema["properties"].values():
        prop.pop("default", None)


class PageSpec(BaseModel):
    service: str = Field(description="The service this page sells (one of the services).")
    path: str = Field(
        description="URL path: lowercase words joined by '-', service then area, e.g. '/emergency-plumber-san-jose'."
    )
    angle: str = Field(description="The one promise the page makes, e.g. '24/7, at your door in 60 minutes'.")


class AdsChannel(BaseModel):
    channel: DraftChannel = Field(description="google_search, or meta for visual, discretionary services.")
    monthly_usd: float = Field(
        description="Monthly budget for this channel in USD; the sum over channels never exceeds the owner's monthly limit."
    )
    themes: list[str] = Field(
        description="Search themes (service × area) the channel targets, in the owner's words; for Meta, audience themes."
    )
    negatives: list[str] = Field(
        description="Searches to exclude (jobs, DIY, other trades, other cities); empty for Meta."
    )
    headlines: list[str] = Field(
        description="5-8 ad headlines, each at most 30 characters, plain claims from the knowledge only."
    )
    descriptions: list[str] = Field(description="2-4 ad descriptions, each at most 90 characters.")
    landing_service: str = Field(description="The service whose page the ads send visitors to.")


class PlanDraft(BaseModel):
    """What the model proposes; agent/plans.py turns it into plan.json with the owner-facing lines."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    pages: list[PageSpec] = Field(description="One page per service the plan sells (the storefront home is implied).")
    gbp_notes: list[str] = Field(
        description="Changes to make on the Google Business Profile (categories, services, hours); empty when none."
    )
    discovery_fixes: list[str] = Field(
        description="Local SEO/GEO fixes on Admiry's pages, plain sentences; empty when none."
    )
    ads: list[AdsChannel] = Field(
        description="Channels to run, budgets summing to at most the monthly limit; empty when the owner has no advertising budget."
    )
    creative_angles: list[str] = Field(
        description="3-5 image angles for the creative set: offer, before/after, testimonial, local, team."
    )
    tracking: list[str] = Field(
        description="Conversion tracking to connect: 'calls', 'form on your page', 'appointment link'."
    )
    review_request_after_days: int = Field(
        description="Days after a won job to ask for a review; 3 unless the owner says otherwise."
    )
    autopilot: Literal["off", "safe", "all"] = Field(
        description="'safe' unless the owner asked otherwise: automatic changes stay within the limit."
    )
    rationale: str = Field(description="Two sentences for the owner on why this split; no advertising jargon.")
    # capability layer K3 (plan 2026-10-08-agent-k3 P4): read leniently, as PlanReview's grades -- a list the model
    # mangled costs the citation, never the plan; code keeps only ids the draft was given (roles/strategist.kept)
    knowledge_ids: list[str] = Field(
        default_factory=list,
        description="The ids of the <trade_knowledge> notes this draft used, each exactly as written between ==, most "
        "used first; empty when <trade_knowledge> is absent or none was used.",
    )

    @field_validator("knowledge_ids", mode="before")
    @classmethod
    def _ids_leniently(cls, value):
        if not isinstance(value, list):
            return []
        return [i.strip() for i in value if isinstance(i, str) and i.strip()]


class _SupportFields(BaseModel):
    """The support read (spec 2026-09-30 §4.1, D131): carried by every read of the owner's own words, so noticing that
    the owner wants a person costs no extra model call."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    support: Literal["none", "help", "human", "dissatisfied"] = Field(
        default="none",
        description="human: the words ask for a person, a representative, or someone to call or write them. help: they "
        "ask for help, or ask a question, that this conversation cannot answer (billing, their account, a problem, how "
        "something works beyond the plan's own numbers). dissatisfied: they say Admiry's answers or plan are wrong, "
        "useless or not what they asked for, or show frustration with Admiry itself. none: anything else; asking to "
        "change the budget, the area, the offer or the goal is none, never dissatisfied.",
    )
    support_summary: str | None = Field(
        default=None, description="One short sentence of what the owner needs when support is not none, else null."
    )
    # plan 2026-10-03-agent-listing-not-mine-words, D1: read on the same call; the server acts on it only for a listing
    # the owner confirmed (listings.unlink_and_say)
    listing_not_mine: bool = Field(
        default=False,
        description="true when the words say the Google Maps place or listing linked to the business is a different "
        'business or is not theirs; false otherwise, also when "not my business" means not their concern, when they '
        "ask to change where the ads show or only dislike the plan.",
    )


class AnswerRead(_SupportFields):
    """How the owner's own words answer one consultation question (agent/llm.py read_answer)."""

    option_id: str | None = Field(description="The id of the option the words mean, if any; else null.")
    amount_usd: float | None = Field(description="A money amount in USD the words state, else null.")
    miles: float | None = Field(description="A distance in miles the words state, else null.")
    note: str | None = Field(description="A short restatement in the owner's words when no option fits, else null.")
    understood: bool = Field(description="true when the words answer the question at all; false when they do not.")
    services: list[str] = Field(
        description="For the services question (id services_offered): the distinct services the words name, each "
        "a short name, e.g. 'Teeth whitening'; empty for any other question.",
    )


class AdjustRead(_SupportFields):
    """How the owner's own words after seeing the plan map to adjust's flat parameters (agent/llm.py
    read_adjust). Every field of the plan is null unless the words plainly ask to change it; understood is false
    when the words name none of them; asks_for_change says whether the words ask for any change at all, also one
    these fields cannot express."""

    monthly_budget_usd: float | None = Field(
        description="A new monthly budget in USD the words state (a daily amount × 30), else null."
    )
    level: Literal["minimum", "conservative", "recommended", "aggressive"] | None = Field(
        description="A budget level the words name or clearly mean (e.g. 'the cheaper one'), else null."
    )
    radius_miles: float | None = Field(description="A new radius in miles the words state, else null.")
    offer: str | None = Field(description="A new offer or promotion the words state, in a few words, else null.")
    objective: Literal["bookings", "calls", "enquiries", "awareness", "any"] | None = Field(
        description="A new objective the words state, else null."
    )
    channel: Channel | None = Field(
        description="The one channel whose monthly amount the words set (a platform id from the cards' channels or "
        "later list, e.g. newsbreak for NewsBreak), else null."
    )
    channel_monthly_usd: float | None = Field(
        description="The monthly amount in USD the words set for that channel (a daily amount × 30), else null."
    )
    understood: bool = Field(
        description="true when the words ask to change at least one of these fields; false when they do not."
    )
    asks_for_change: bool = Field(
        description="True when the words ask to change something about the plan or the ads, including a change none "
        "of the fields above can express ('no ads in the evening', 'lead with the free consultation'); false when they "
        "only agree, approve, thank, greet or ask a question."
    )


class MailRead(_SupportFields):
    """What an e-mail sent after the plan asks for (agent/llm.py read_mail; spec 2026-09-28 §5.6): one intent from
    the fixed set, how plainly the words ask for it, and adjust's fields when the intent is adjust."""

    intent: Literal["adjust", "plan", "connect", "question", "unclear", "other"] = Field(
        description="adjust: change the plan's budget, level, radius, offer or objective. plan: see the plan again. "
        "connect: how to link Google Ads or prepay. question: a question about the plan or the business's ads. "
        "unclear: the words ask for nothing readable. other: anything else (a person will read it)."
    )
    confidence: float = Field(description="0 to 1: how plainly the words ask for that intent.")
    monthly_budget_usd: float | None = Field(
        description="A new monthly budget in USD the words state (a daily amount × 30), else null."
    )
    level: Literal["minimum", "conservative", "recommended", "aggressive"] | None = Field(
        description="A budget level the words name or clearly mean (e.g. 'the cheaper one'), else null."
    )
    radius_miles: float | None = Field(description="A new radius in miles the words state, else null.")
    offer: str | None = Field(description="A new offer or promotion the words state, in a few words, else null.")
    objective: Literal["bookings", "calls", "enquiries", "awareness", "any"] | None = Field(
        description="A new objective the words state, else null."
    )
    question: str | None = Field(description="The question asked, in a few words, when intent is question; else null.")


class MailAnswer(BaseModel):
    """An answer to a question about the plan, from the material given and nothing else (agent/llm.py
    answer_question; spec 2026-09-28 §5.7)."""

    text: str = Field(description="At most three short paragraphs answering the question from the material.")
    grounded: bool = Field(
        description="true when every statement in text comes from the material; false when the material cannot answer."
    )


PlanDimension = Literal["fit", "budget", "copy", "keywords", "estimate", "clarity"]  # == roles.contracts.PLAN.ids


def _readable(raw: object, grade: type[BaseModel]) -> list:
    """The grades that read as grades (a dimension in any case): one that does not -- a word for a number, a fraction,
    a dimension the rubric does not have -- is dropped, never the whole answer (the Critic's answers, read leniently:
    PlanReview, InsightReview)."""
    kept = []
    for g in raw if isinstance(raw, list) else []:
        if isinstance(g, grade):  # built in code (fixtures, a copy), already a grade
            kept.append(g)
            continue
        if not isinstance(g, dict):
            continue
        try:
            kept.append(grade.model_validate({**g, "dimension": str(g.get("dimension", "")).strip().lower()}))
        except ValidationError:
            continue
    return kept


_ID_GAPS = re.compile(r"[\s_-]+")  # whatever a judge may put between the words of a check id
_ID_EDGES = "-.:,;"  # what is left at its ends: a hyphen, and the punctuation of the sentence the id ends or begins


def _read_check_ids(raw: object) -> list[str]:
    """The check ids a judge's `failed` names, read the same way for the plan's grades and the research's: the strings
    of a list, lowercased, each run of spaces, underscores and hyphens made one hyphen (`No_Forecast`, `no forecast` and
    `no-forecast` are one id, as the rubric spells it), the hyphens and the full stops, colons, commas and semicolons at
    its ends taken off (`no-forecast.` is `no-forecast`), the empty ones left out. Anything else is dropped, never the
    grade. An id that names no check of the dimension is kept: only the caps and the issues ignore it."""
    if not isinstance(raw, list):
        return []
    ids = [_ID_GAPS.sub("-", f.lower()).strip(_ID_EDGES) for f in raw if isinstance(f, str)]
    return [i for i in ids if i]


class PlanGrade(BaseModel):
    """One dimension of the Critic's plan rubric (agent/roles/contracts.py PLAN): for Admiry's team, never shown to the
    owner (spec 2026-10-07 capability layer §4, §7). `failed` (K5) names the dimension's checks the plan fails, before
    the grade; code caps the grade by them (roles.base.grades_of). Read leniently: anything in `failed` that is not a
    check id's words is dropped, never the grade."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    dimension: PlanDimension = Field(
        description="The rubric dimension: fit, budget, copy, keywords, estimate or clarity."
    )
    failed: list[str] = Field(
        default_factory=list,
        description="The ids of this dimension's checks the plan fails, as the rubric names them; empty when none.",
    )
    grade: int = Field(description="A whole number from 1 to 5 by the rubric's anchors.")
    note: str = Field(
        description="One sentence for Admiry's team on why, at most 200 characters; what is wrong when under 4."
    )

    @field_validator("failed", mode="before")
    @classmethod
    def _check_ids(cls, raw: object) -> list:
        return _read_check_ids(raw)


ReviewTopic = Literal["services", "area", "money", "page", "consultation", "compliance"]


class ReviewCheck(BaseModel):
    """One thing Admiry looked at in its second look before the owner confirms the plan (PlanReview.checks)."""

    topic: ReviewTopic = Field(
        description="What was looked at: services, area, money, page, consultation or compliance."
    )
    ok: bool = Field(
        description="true when nothing about this topic needs the owner's attention before confirming; false when "
        "the sentence says something the owner should know first."
    )
    text: str = Field(description="One plain sentence for the owner, at most 200 characters, from the material only.")


class PlanReview(BaseModel):
    """Admiry's second look at a plan with money in place, before the owner confirms it and the ads start
    (agent/llm.py review_plan; S0, 2026-10-07): a summary for the owner and one check per topic. Advisory only --
    the launch's own check (agent/proposals.py precheck_approve_plan) alone decides what can launch. The team's
    `grades` and `missing` (capability layer K1) are asked for on the wire but read leniently: an answer without them,
    or with a grade the judge mangled, still gives the owner the summary and the checks (final review I1) -- what is
    dropped counts as not graded (roles.base.grades_of)."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    summary: str = Field(
        description="Two or three sentences for the owner: what will run, where, at what daily budget, and how many "
        "days the balance covers; every figure from the material."
    )
    checks: list[ReviewCheck] = Field(
        description="One entry per topic, in the order services, area, money, page, consultation, compliance."
    )
    grounded: bool = Field(
        description="true when every statement comes from the material; false when the material cannot support "
        "the summary."
    )
    grades: list[PlanGrade] = Field(
        default_factory=list,
        description="For Admiry's team only: one entry per rubric dimension, in the order fit, budget, copy, "
        "keywords, estimate, clarity.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="What you needed to judge the plan and did not have, each in a few words; empty when nothing.",
    )

    @field_validator("grades", mode="before")
    @classmethod
    def _readable_grades(cls, raw: object) -> list:
        return _readable(raw, PlanGrade)

    @field_validator("missing", mode="before")
    @classmethod
    def _missing_words(cls, raw: object) -> list:
        return [m for m in raw if isinstance(m, str)] if isinstance(raw, list) else []


class ConfiguredReview(BaseModel):
    """The Reviewer's last look before a first launch goes live (L1, spec 2026-10-10 §6.2 step 3; agent/llm.py
    review_configured): whether the ads as configured -- the exact headlines and descriptions Google will show, the
    daily budget, the area and the page -- read as the plan the owner agreed to, for this business. Admiry's team's
    alone: the owner reads launch_review's own sentences."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    matches: bool = Field(
        description="true when the configured ads say what the agreed plan says, for this business, with nothing a "
        "reviewer would stop before they go live."
    )
    issues: list[str] = Field(
        description="One short sentence per thing a reviewer would stop the ads for: a claim the business cannot back, "
        "a service or a place that is not the business's, a budget or a page that is not the agreed one. Empty when "
        "none."
    )
    confidence: float = Field(description="0 to 1: how sure the review is, from the material alone.")


InsightDimension = Literal["grounded", "priority", "fit", "clarity", "honesty"]  # == roles.contracts.INSIGHT.ids
InsightConfidence = Literal["low", "medium", "high"]


class InsightGrade(BaseModel):
    """One dimension of the Critic's insight rubric (agent/roles/contracts.py INSIGHT): for Admiry's team only."""

    dimension: InsightDimension = Field(
        description="The rubric dimension: grounded, priority, fit, clarity or honesty."
    )
    grade: int = Field(description="A whole number from 1 to 5 by the rubric's anchors.")
    note: str = Field(
        description="One sentence for Admiry's team on why, at most 200 characters; what is wrong when under 4."
    )


class InsightReview(BaseModel):
    """The Critic's grades of one Insight (agent/llm.py review_insight; capability layer K2), for Admiry's team only.
    Read leniently, as PlanReview's grades are: a grade that does not read as one is dropped and counts as not
    graded (roles.base.grades_of)."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    grades: list[InsightGrade] = Field(
        default_factory=list,
        description="One entry per rubric dimension, in the order grounded, priority, fit, clarity, honesty.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="What you needed to judge the insight and did not have, each in a few words; empty when nothing.",
    )

    @field_validator("grades", mode="before")
    @classmethod
    def _readable_grades(cls, raw: object) -> list:
        return _readable(raw, InsightGrade)

    @field_validator("missing", mode="before")
    @classmethod
    def _missing_words(cls, raw: object) -> list:
        return [m for m in raw if isinstance(m, str)] if isinstance(raw, list) else []


class InsightFinding(BaseModel):
    """One thing the day's figures say (Insight.findings)."""

    text: str = Field(
        description="One plain sentence for the owner, at most 200 characters, every number from the material."
    )


class InsightPick(BaseModel):
    """One of the material's candidates the Analyst recommends (Insight.recommendations). The action is the
    candidate's -- a public tool and its arguments, written by code -- never the model's (plan K2 R1)."""

    candidate_id: str = Field(description="The id of one entry of the material's candidates, exactly as written there.")
    text: str = Field(
        description="One or two plain sentences for the owner, at most 240 characters: what to do and why, from the "
        "material only."
    )
    confidence: InsightConfidence = Field(
        description="high when the material's figures clearly call for it, medium when they point to it, low when "
        "they are early or thin."
    )


class Insight(BaseModel):
    """The Analyst's daily look at one business's results (agent/roles/analyst.py; capability layer K2, spec
    2026-10-07 §5): `summary` is the digest's "Admiry's take"; the recommendations pick among the candidates code
    computed and never name an action of their own."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    summary: str = Field(
        description="Two or three short sentences for the owner: what the figures mean, how they connect to the "
        "flagged items and what to do first; a quiet day is said to be quiet."
    )
    findings: list[InsightFinding] = Field(
        description="At most four things the figures say, most important first; empty on a quiet day."
    )
    recommendations: list[InsightPick] = Field(
        description="At most three of the material's candidates, most important first; empty when none fits."
    )
    grounded: bool = Field(
        description="true when every statement comes from the material; false when the material gives nothing true "
        "to say."
    )


ResearchDimension = Literal["grounded", "numbers", "relevant"]  # == roles.contracts.RESEARCH.ids
ResearchConfidence = Literal["low", "medium", "high"]


# The Critic's grade of one research dimension (agent/roles/contracts.py RESEARCH), read as PlanGrade's is (K5): the
# checks the dimension fails come before the grade and code caps the grade by them (roles.base.grades_of). The docstring
# below rides the wire as the schema's description, which the judge reads: it says what a grade is, not how code reads it.
class ResearchGrade(BaseModel):
    """One dimension of the Critic's research rubric, for Admiry's team only: the ids of the dimension's checks the
    research fails, named before the grade, then the grade."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    dimension: ResearchDimension = Field(description="The rubric dimension: grounded, numbers or relevant.")
    failed: list[str] = Field(
        default_factory=list,
        description="The ids of this dimension's checks the research fails, as the rubric names them; empty when none.",
    )
    grade: int = Field(description="A whole number from 1 to 5 by the rubric's anchors.")
    note: str = Field(
        description="One sentence for Admiry's team on why, at most 200 characters; what is wrong when under 4."
    )

    # Read leniently, as PlanGrade's is (_read_check_ids): what `failed` holds that is not a string is dropped, never the
    # grade; a string that names no check of the dimension is kept (the reader does not know the rubric) and only the
    # caps and the issues ignore it (roles.base.cap, failed_of).
    @field_validator("failed", mode="before")
    @classmethod
    def _check_ids(cls, raw: object) -> list:
        return _read_check_ids(raw)


class ResearchReview(BaseModel):
    """The Critic's grades of one piece of research (agent/llm.py review_research; capability layer K4), for Admiry's
    team only. Read leniently, as PlanReview's and InsightReview's grades are: a grade that does not read as one is
    dropped and counts as not graded (roles.base.grades_of)."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    grades: list[ResearchGrade] = Field(
        default_factory=list, description="One entry per rubric dimension, in the order grounded, numbers, relevant."
    )
    missing: list[str] = Field(
        default_factory=list,
        description="What more the researcher would need to read to do better, each in a few words; empty when "
        "nothing.",
    )

    @field_validator("grades", mode="before")
    @classmethod
    def _readable_grades(cls, raw: object) -> list:
        return _readable(raw, ResearchGrade)

    @field_validator("missing", mode="before")
    @classmethod
    def _missing_words(cls, raw: object) -> list:
        return [m for m in raw if isinstance(m, str)] if isinstance(raw, list) else []


class ResearchFinding(BaseModel):
    """One thing the sources show (ResearchBrief.findings), citing them by id."""

    text: str = Field(
        description="One or two plain sentences, at most 300 characters: what the cited sources show, with no number "
        "the material or a cited source does not state; a business nearby described, never named; no person's name and "
        "no mention of Admiry."
    )
    source_ids: list[str] = Field(
        description="The ids of the material's sources this rests on, exactly as written there; at least one."
    )
    confidence: ResearchConfidence = Field(
        description="high when several sources agree or a published figure states it, medium when one good source "
        "does, low when it rests on one business's own page or little else."
    )


class ResearchBrief(BaseModel):
    """The Researcher's look into one business's market (agent/roles/researcher.py; capability layer K4, spec
    2026-10-07 §4): findings citing the material's sources, the searches to aim for and to keep out, offer angles and
    what the sources could not answer. Material for Admiry's planner and, once the Critic delivers it, for the owner."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    findings: list[ResearchFinding] = Field(description="At most eight findings on the topics, most useful first.")
    suggested_themes: list[str] = Field(
        description="At most ten searches a customer here types for this business's services, a few lowercase words "
        "each; no business's name."
    )
    suggested_negatives: list[str] = Field(
        description="At most ten searches the ads should stay away from (jobs, do-it-yourself, other trades, other "
        "places), a few lowercase words each."
    )
    offer_angles: list[str] = Field(
        description="At most five offers or reasons to choose this business, one sentence each, only what the "
        "material says the business offers, within the trade's rules."
    )
    unknowns: list[str] = Field(
        description="What the sources could not answer that a plan would need, one short sentence each; empty when "
        "nothing."
    )
    grounded: bool = Field(
        description="true when every statement comes from the material or the pages; false when they give nothing "
        "true to say."
    )


class KnowledgeLine(BaseModel):
    """One line of a knowledge draft (agent/research.py), with the sources it rests on."""

    text: str = Field(
        description="One plain sentence (a Details line may be two); a placeholder in angle brackets (<town>), never "
        "square brackets; no link."
    )
    source_ids: list[str] = Field(
        description="The ids of the material's sources this line rests on, exactly as written there; at least one for "
        "a line that states a figure, a rule or a fact a source gives; empty for advice of the trade's own."
    )


class KnowledgeDraft(BaseModel):
    """One reviewed guide's draft for a consultation tree and a topic (agent/research.py; capability layer K4, spec
    2026-10-07 §6.1): what agent/knowledge/<tree>/<topic>.md holds once the founder has reviewed it (K3's format)."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    title: str = Field(description="What the guide is about, in a few words after the trade's name: 'Google Search'.")
    summary: str = Field(
        description="One sentence on what the guide says, at most 160 characters, no digit, no '|', '<' or '>', ending "
        "with a full stop."
    )
    key_points: list[KnowledgeLine] = Field(
        description="Four to six things a planner must know first, at most 110 words in all (90 for a trade), no money "
        "figure."
    )
    details: list[KnowledgeLine] = Field(description="The reasons and the nuances, at most 400 words in all.")
    numbers: list[KnowledgeLine] = Field(
        description="Every figure, each with its year or scope and the source that states it; an estimate marked "
        "(estimate); empty when the sources give none."
    )
    do_not_say: list[KnowledgeLine] = Field(
        description="At most four things ads in this trade must never claim or say, at most fourteen words each."
    )
    confidence: ResearchConfidence = Field(
        description="high when published sources agree, medium when one good source says it, low when it is thin."
    )
    grounded: bool = Field(description="false when the material gives nothing true to say on the topic.")


RuleKind = Literal["word", "pattern"]
RuleApplies = Literal["headline", "description", "keyword", "any"]
RuleLevel = Literal["warn", "block"]


class RuleProposal(BaseModel):
    """One rule the daily refusal summary proposes (agent/refusal_summary.py; spec 2026-10-12 §2.4): read by code,
    which checks every field again before a row is written -- the value, the lines it names, the thresholds -- so a
    wrong proposal is dropped, never trusted (plan D14)."""

    kind: RuleKind = Field(
        description="word: a whole word or short phrase several lines share and Google refused for the same reason; "
        "pattern: one of the material's shapes, by its id."
    )
    applies_to: RuleApplies = Field(description="headline, description, keyword (a search phrase), or any.")
    value: str = Field(
        description="The word or phrase (2 to 40 characters, letters and digits), or the shape's id exactly as the "
        "material lists it. Never a regular expression."
    )
    topic: str | None = Field(
        default=None,
        description="Google's policy topic the lines share, exactly as the material writes it; null when they share "
        "none.",
    )
    why: str = Field(
        description="Why Google refuses it, in one plain sentence an owner can read, at most 160 characters; no "
        "Google codes, no advertising jargon."
    )
    level: RuleLevel = Field(
        description="warn when a person should look at such a line first; block only when Google refuses it every "
        "time. A trademark is always warn."
    )
    line_ids: list[str] = Field(
        description="The ids of the material's lines this rule would have caught, exactly as written; lines from at "
        "least two businesses, or at least three lines."
    )
    reason: str = Field(description="For Admiry's team: why this rule, in one sentence, at most 200 characters.")


class RuleProposals(BaseModel):
    """The summary's proposals (agent/llm.py propose_rules): at most five, or none."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    proposals: list[RuleProposal] = Field(
        description="At most five rules, the most often refused first; empty when the lines share nothing a rule "
        "could catch."
    )


class FaqItem(BaseModel):
    """One question on an Admiry-written page and its answer (PageCopy.faq)."""

    q: str = Field(description="A question a customer of this business asks, at most 120 characters.")
    a: str = Field(
        description="Its answer in a few plain sentences, at most 400 characters, from the kit's facts only; when "
        "the kit cannot answer it, say the business answers it on the phone."
    )


class PageCopy(BaseModel):
    """The words of a landing page Admiry writes itself (agent/llm.py write_page / revise_page; spec 2026-09-28
    §7). The vertical's template places them beside what the page adds on its own -- the phone number on the call
    button, the form, the hours, the address, the privacy link and the lines the law requires -- so none of those
    is a field here. Like the shapes above, no field carries a constraint the API grammar might not enforce: the
    limits in the descriptions are checked again in code (agent/page_builder.py `lint`), and copy that breaks one
    is asked for again, once, with what was wrong."""

    headline: str = Field(
        description="The page's one headline: the service and the city the way the ads say them, e.g. "
        "'Invisalign in Palo Alto', at most 60 characters."
    )
    subheadline: str = Field(description="One plain sentence under the headline, at most 120 characters.")
    offer_line: str = Field(
        description="The kit's offer in one line, at most 80 characters; an empty string when the kit has no offer."
    )
    trust_points: list[str] = Field(
        description="Up to 4 short reasons to choose this business, each at most 90 characters, from the kit's "
        "facts only."
    )
    service_paragraphs: list[str] = Field(
        description="1 to 3 plain paragraphs about the service -- what happens, how it works -- each at most 400 "
        "characters."
    )
    faq: list[FaqItem] = Field(description="Up to 5 of the recipe's questions, each with its answer.")
    cta_label: str = Field(description="The words on the call button, at most 30 characters, e.g. 'Call to book'.")


class Step(BaseModel):
    """One step of "how it works" on an industry site (SiteWords.home.steps)."""

    title: str = Field(description="The step in a few words, at most 60 characters.")
    text: str = Field(description="One or two plain sentences, at most 120 characters.")


class HomeWords(BaseModel):
    eyebrow: str = Field(description="A short line above the headline, at most 60 characters; no capitals needed.")
    headline: str = Field(
        description="The home page's one headline, at most 60 characters: it names the lead service and the city."
    )
    lead: str = Field(description="One plain sentence under the headline, at most 120 characters.")
    services_heading: str = Field(description="The heading over the services, at most 60 characters.")
    services_lead: str = Field(description="One sentence under it, at most 120 characters.")
    why_heading: str = Field(description="The heading over the reasons to choose the business, at most 60 characters.")
    why_points: list[str] = Field(description="Up to 4 reasons, each at most 90 characters, from the facts only.")
    how_heading: str = Field(description="The heading over how it works, at most 60 characters.")
    steps: list[Step] = Field(description="3 or 4 steps.")
    about_heading: str = Field(description="The heading over the owner's story, at most 60 characters.")
    about_text: str = Field(description="A short paragraph, at most 400 characters, from the facts only.")
    area_heading: str = Field(description="The heading over where the business works, at most 60 characters.")
    area_text: str = Field(description="A sentence or two, at most 400 characters.")
    faq: list[FaqItem] = Field(description="4 to 6 questions with their answers.")
    cta_heading: str = Field(description="The closing call to action's heading, at most 60 characters.")
    # A3p-6 batch 1 Task 3: headings over sections the template places (Q19: every section heading not fixed by the
    # template is the model's); empty leaves the section without a visible heading.
    reviews_heading: str = Field(default="", description="The heading over the reviews, at most 60 characters.")
    work_heading: str = Field(
        default="", description="The heading over the photos of recent work, at most 60 characters."
    )
    faq_heading: str = Field(default="", description="The heading over the questions, at most 60 characters.")
    # A3p-6 batch 1 Task 4: the dental home's modules (the dentist takes about_heading and about_text)
    new_patients_heading: str = Field(
        default="", description="Dental: the heading over the new-patient welcome, at most 60 characters."
    )
    insurance_heading: str = Field(
        default="", description="Dental: the heading over insurance and payment, at most 60 characters."
    )
    office_heading: str = Field(
        default="", description="Dental: the heading over the photos of the office, at most 60 characters."
    )
    cta_text: str = Field(description="One sentence under it, at most 120 characters.")


class AboutWords(BaseModel):
    headline: str = Field(description="The about page's headline, at most 60 characters.")
    lead: str = Field(description="One plain sentence under it, at most 120 characters.")
    story: list[str] = Field(description="2 or 3 short paragraphs, each at most 400 characters, from the facts only.")
    values_heading: str = Field(description="The heading over the values, at most 60 characters.")
    values: list[str] = Field(description="Short values, each at most 90 characters, from the facts only.")
    community: str = Field(description="A sentence about the community served, at most 400 characters.")


class ServicesWords(BaseModel):
    headline: str = Field(description="The services page's headline, at most 60 characters.")
    lead: str = Field(description="One plain sentence under it, at most 120 characters.")
    intro: str = Field(description="A short paragraph, at most 400 characters.")


class ContactWords(BaseModel):
    headline: str = Field(description="The contact page's headline, at most 60 characters.")
    lead: str = Field(description="One plain sentence under it, at most 120 characters.")
    response_line: str = Field(description="What happens after the form is sent, at most 120 characters.")


class ServiceWords(BaseModel):
    slug: str = Field(description="The service's slug, as the facts give it.")
    lead: str = Field(description="One plain sentence under the page's headline, at most 120 characters.")
    what: str = Field(description="What the service is and what happens, at most 400 characters.")
    signs: list[str] = Field(description="Signs a customer needs it, each at most 90 characters.")
    includes: list[str] = Field(description="What it includes, each at most 90 characters.")
    faq: list[FaqItem] = Field(description="Up to 3 questions with their answers.")
    signs_heading: str = Field(default="", description="The heading over the signs, at most 60 characters.")
    includes_heading: str = Field(default="", description="The heading over what it includes, at most 60 characters.")
    faq_heading: str = Field(default="", description="The heading over the questions, at most 60 characters.")


class SiteWords(BaseModel):
    """The words of an industry site (agent/llm.py write_site; plan A3p-6 batch 1 Q11): data the templates place
    beside what they add on their own -- the phone, the forms, the hours, the address, the notices -- so none of
    those is a field. Limits live in the descriptions and are checked again in code (agent/sites/words.py lint)."""

    home: HomeWords
    about: AboutWords
    services: ServicesWords
    contact: ContactWords
    service_pages: list[ServiceWords] = Field(description="One for each lead service, by slug.")
    cta_label: str = Field(description="The words on the call button, at most 30 characters.")
    book_label: str = Field(description="The words on the second button, at most 30 characters.")
    # the founder's dev walk, 2026-10-08: the owner's hours as typed ("9am-5pm Monday to Friday"), written tidily in
    # this same call; agent/sites/words.py keeps them only when they fit, else the site shows the owner's own lines
    hours: list[str] = Field(
        default_factory=list,
        description="The opening hours in <facts>, the same days and times, one line per group of days the way a "
        'shop door says them: "Monday to Friday, 9 am to 5 pm", "Sunday, closed". At most 7 lines of at most '
        "40 characters. Empty when <facts> has no hours.",
    )


class PageRevision(PageCopy):
    """revise_page's answer (agent/llm.py; A3p-3 spec P22): the page's words with the owner's instruction applied, and
    whether the instruction asks for more than the one-page template can hold -- the homepage then invites the owner to
    continue with their assistant, and nothing on the page changes."""

    beyond_template: bool = Field(
        default=False,
        description="True only when the instruction asks for something this one-page template cannot hold: another "
        "page, a new kind of section, a different layout, colours or fonts, or a widget from another company (a "
        "booking calendar, a chat box, a shop). Then copy the page's words back unchanged. False for any change of "
        "words, tone, length or emphasis.",
    )


class SupportRead(_SupportFields):
    """The support read alone (agent/llm.py read_support): a free-text turn with no other read of its own -- a ready
    consultation, none at all, or a message too long for read_adjust (spec 2026-09-30 §4.2)."""


TURN_INTENTS = (
    "answer",
    "go_to",
    "start_over",
    "budget",
    "build",
    "explain",
    "ask",
    "skip",
    "smalltalk",
    "support",
    "not_mine",
    "unclear",
)


class Turn(_SupportFields):
    """One turn of the home's free text while a consultation is open or ready (agent/llm.py read_turn; spec
    2026-10-10 K6 slice 1 §4.3): the intent, Admiry's words, how plainly the words carry the intent, and the fields of
    that intent alone -- the answer's reading (AnswerRead's fields) for answer, the question to return to for go_to,
    the monthly amount for budget; every other field null (services empty). Flat, as AdjustRead and MailRead are."""

    intent: Literal[
        "answer",
        "go_to",
        "start_over",
        "budget",
        "build",
        "explain",
        "ask",
        "skip",
        "smalltalk",
        "support",
        "not_mine",
        "unclear",
    ] = Field(
        description="answer: the words answer the open question. go_to: the owner wants to return to an earlier "
        "question (question_id is one of may_go_to). start_over: the owner wants to start the whole consultation "
        "over from nothing. budget: the words are about the budget (monthly_usd when they name a monthly amount). "
        "build: the owner asks Admiry to build the plan now (only when the consultation is ready). explain: the "
        "owner asks what the open question means or why it is asked. ask: a question about Admiry, the ads or the "
        "plan to come. skip: the owner would rather not answer the open question. smalltalk: thanks, a greeting, an "
        "aside that asks for nothing. support: the owner asks for a person, for help this conversation cannot give, "
        "or is unhappy with Admiry. not_mine: the words say the Google Maps place linked to the business is not "
        "theirs. unclear: the words ask for nothing readable."
    )
    say: str = Field(
        description="Admiry's words for this turn: at most two short sentences, complete on their own, plain words, "
        "no advertising jargon, no promise about results. The owner reads them for explain, ask and smalltalk; they "
        "may be empty for every other intent (Admiry's own commands and fixed sentences speak)."
    )
    confidence: float = Field(description="0 to 1: how plainly the words carry the intent.")
    option_id: str | None = Field(
        description="answer: the id of the open question's option the words mean, else null; null for every other "
        "intent."
    )
    amount_usd: float | None = Field(
        description="answer: a money amount in USD the words state when the open question asks for one, else null."
    )
    miles: float | None = Field(
        description="answer: a distance in miles the words state when the open question asks for one, else null."
    )
    note: str | None = Field(
        description="answer: a short restatement in the owner's words when no option fits (for an address question, "
        "the place the words name: a street address, a ZIP code or a city), else null."
    )
    services: list[str] = Field(
        description="answer to the services question (id services_offered): the distinct services the words name, "
        "each a short name, e.g. 'Teeth whitening'; empty for every other question and intent."
    )
    question_id: str | None = Field(
        description="go_to: the id of the question to return to, one of may_go_to; null for every other intent."
    )
    monthly_usd: float | None = Field(
        description="budget: the monthly amount in USD the words name (a daily amount × 30); null when they name "
        "none, and for every other intent."
    )

    def reading(self) -> AnswerRead:
        """The answer's reading this turn carries, as consult.parse reads one: understood when the words are the
        answer; the support read beside it, as every reading of the owner's words carries it."""
        return AnswerRead(
            option_id=self.option_id,
            amount_usd=self.amount_usd,
            miles=self.miles,
            note=self.note,
            understood=self.intent == "answer",
            services=list(self.services),
            support=self.support,
            support_summary=self.support_summary,
            listing_not_mine=self.listing_not_mine,
        )
