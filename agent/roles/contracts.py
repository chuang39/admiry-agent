"""The Critic's rubrics and each role's contract (spec 2026-10-07-admiry-capability-layer-design §4, §7). PLAN is the
plan rubric: the six dimensions the founder grades the golden set on (§7), which the Critic grades every plan look by
(agent/plan_review.py) and the eval compares (agent/eval); each plan dimension carries the checks the judge answers
before its grade (K5). INSIGHT (K2) grades the Analyst's daily look (agent/roles/analyst.py), without checks; RESEARCH
(K4) the Researcher's look into a business's market and the knowledge drafts it writes (agent/roles/researcher.py,
agent/research.py), with checks as PLAN's (K5): no founder-graded set calibrates it, so the checks and their caps are
what make it tell good from bad (plan K4 D17)."""

from __future__ import annotations

from .. import models
from .base import Check, Contract, Dimension, Rubric

PLAN = Rubric(
    name="plan",
    dimensions=(
        Dimension(
            "fit",
            "Services and area",
            "The ad text and the search phrases are about the service the owner chose, and the area fits the trade "
            "and where the business is.",
            checks=(
                Check(
                    "service",
                    "every search phrase and every headline in `ads` is about the service the owner chose (the plan's "
                    "goal names it) or a close variant of it",
                    serious=True,
                ),
                Check("page", "the page the ads send people to (landing_service in `ads`) sells that same service"),
                Check(
                    "area",
                    "the reach (the cards' area) is one this trade's customers travel, about 10 miles for a dentist, "
                    "25 for a home-service trade and 50 for an attorney, and every place the ads name is in or near "
                    "the business's area",
                ),
            ),
        ),
        Dimension(
            "budget",
            "Budget",
            "The monthly amount is sensible against the benchmark estimate and, when given, the balance; no place is "
            "funded below its floor.",
            checks=(
                Check(
                    "limit",
                    "the monthly amount is within the monthly budget the owner gave (goal_hint in the knowledge), when "
                    "they gave one",
                    serious=True,
                ),
                Check("floor", "no place is funded below the minimum level (budget levels, minimum)", serious=True),
                Check(
                    "enough",
                    "at the benchmark's cost per lead the amount buys a steady flow: the planning column's lower lead "
                    "count is 3 or more",
                ),
                Check("balance", "when the money is given, the balance covers at least 7 days at the daily budget"),
            ),
        ),
        Dimension(
            "copy",
            "Ad text",
            "The ad text is clear and specific and within the trade's rules: no claim this trade's ads must not make, "
            "no price or promise the material does not state.",
            checks=(
                Check(
                    "claims",
                    "every claim in a headline or description in `ads`, among the lines the launch keeps, is in the "
                    "knowledge: no price, discount, offer, rating, years, hours or guarantee it does not state",
                    serious=True,
                ),
                Check(
                    "rules",
                    "nothing this trade's ads must not say, among the lines the launch keeps: for healthcare or legal no "
                    "promise of an outcome (pain-free, guaranteed, we win, the biggest settlements) and no best or #1",
                    serious=True,
                ),
                Check(
                    "enough",
                    "at least 5 headlines and 2 descriptions are left once the linter drops what it refuses (lint in "
                    "`ads`)",
                ),
                Check(
                    "specific",
                    "the lines name the service and give a reason to choose this business (a usp, the area, the hours) "
                    "rather than filler",
                ),
            ),
        ),
        Dimension(
            "keywords",
            "Searches",
            "The search phrases are what a customer types for this service here, and the phrases left out keep away "
            "people who are not customers.",
            checks=(
                Check(
                    "intent",
                    "the search phrases (themes in `ads`) are what someone who wants this service now types: the "
                    "service with the area or near me, or the problem it fixes",
                ),
                Check(
                    "waste",
                    "no search phrase draws people who are not customers: jobs, salary, training, do-it-yourself, "
                    "free, another trade or another city",
                    serious=True,
                ),
                Check(
                    "negatives",
                    "the phrases left out (negatives in `ads`) keep away job seekers, do-it-yourself and free-service "
                    "searches",
                ),
            ),
        ),
        Dimension(
            "estimate",
            "Estimate",
            "The lead estimate states its basis and its range and promises nothing.",
            checks=(
                Check(
                    "basis",
                    "the estimate names its basis: the benchmark row, the rates and the customer value (economics "
                    "assumptions)",
                    serious=True,
                ),
                Check("range", "the leads are a range from low to high, not one number"),
                Check(
                    "promise",
                    "nothing promises a result: the footnote says it is not a forecast and no words say guaranteed or "
                    "you will get",
                    serious=True,
                ),
            ),
        ),
        Dimension(
            "clarity",
            "Clear to the owner",
            "The plan's words are plain, free of advertising jargon, and say what will happen.",
            checks=(
                Check(
                    "jargon",
                    "the plan's words (the cards and the rationale) carry no advertising jargon: CPC, CTR, ROAS, CPA, "
                    "impressions, broad match, PMax, top of funnel",
                ),
                Check("what", "the words say what runs, where, for how much a day and what counts as a result"),
            ),
        ),
    ),
    threshold=0.7,
)
INSIGHT = Rubric(
    name="insight",
    dimensions=(
        Dimension(
            "grounded",
            "Every number from the material",
            "Each amount, count and comparison in the summary, the findings and the recommendations is one the "
            "material states, and nothing is forecast.",
        ),
        Dimension(
            "priority",
            "What matters first",
            "The summary and the first recommendation take up the most pressing item the material flags (high before "
            "medium before info), and no item is invented.",
        ),
        Dimension(
            "fit",
            "Recommendations fit",
            "Each recommendation answers a flagged item or a figure in the material, and its words say what the owner "
            "would do and why.",
        ),
        Dimension("clarity", "Clear to the owner", "Plain words and short sentences, free of advertising jargon."),
        Dimension(
            "honesty",
            "Honest",
            "Figures resting on few leads are called early, a quiet day is called quiet, and nothing promises results.",
        ),
    ),
    threshold=0.7,
    anchors=(
        "5 = a marketer would send it to the owner as it is",
        "4 = small fixes only",
        "3 = workable, but change something before sending",
        "2 = a real problem",
        "1 = wrong or unusable",
    ),
)
RESEARCH = Rubric(
    name="research",
    dimensions=(
        Dimension(
            "grounded",
            "Grounded in its sources",
            "Each finding says only what the sources it cites say, with a confidence they bear (a business's own page "
            "is its claim, not a fact), the searches and offer angles follow from the material, and a business nearby "
            "is described, never named.",
            checks=(
                Check(
                    "cited",
                    "every finding (or a guide's line) says only what the sources it cites say, or a count the "
                    "material itself gives (businesses shown or found, pages read, the radius) or how many businesses "
                    "nearby a finding's own sources are from",
                    serious=True,
                ),
                Check("claims", "what a business's own page or listing says is reported as its claim, never as a fact"),
                Check(
                    "unnamed",
                    "no finding, search, offer angle or unknown names a business nearby (by its name, its brand or its "
                    "website) or a person who is not this business's own: each is described instead, as in two of the "
                    "five nearby practices",
                    serious=True,
                ),
            ),
        ),
        Dimension(
            "numbers",
            "No invented numbers",
            "Every amount, count and percentage is one a cited source states, or a count the material itself gives "
            "(businesses shown or found, pages read, the radius) or how many businesses nearby a finding's own sources "
            "are from, and nothing is forecast or estimated.",
            checks=(
                Check(
                    "stated",
                    "every amount, count and percentage is one a cited source states, or a count the material itself "
                    "gives (businesses shown or found, pages read, the radius) or how many businesses nearby a "
                    "finding's own sources are from",
                    serious=True,
                ),
                Check("no-forecast", "nothing is forecast, estimated or averaged by the researcher", serious=True),
            ),
        ),
        Dimension(
            "relevant",
            "Relevant to the service and area",
            "The findings, searches and offer angles are about the services this business sells, where it sells them, "
            "and the topics asked; nothing is about another trade or another place.",
            checks=(
                Check(
                    "service",
                    "it is about the services this business sells (a guide: the trade and the topic it names)",
                    serious=True,
                ),
                Check("area", "it is about where this business works, never another town or state unless it names one"),
                Check("topics", "it answers the topics asked, each at least once"),
            ),
        ),
    ),
    threshold=0.7,
    anchors=(
        "5 = a marketer would plan with it as it is",
        "4 = small fixes only",
        "3 = workable, but check something before planning with it",
        "2 = a real problem",
        "1 = wrong or unusable",
    ),
)
RUBRICS = {PLAN.name: PLAN, INSIGHT.name: INSIGHT, RESEARCH.name: RESEARCH}

STRATEGIST = Contract(role="strategist", output=models.PlanDraft, rubric=PLAN, max_calls=2, max_rounds=1)
ANALYST = Contract(role="analyst", output=models.Insight, rubric=INSIGHT, max_calls=4, max_rounds=2)
RESEARCHER = Contract(role="researcher", output=models.ResearchBrief, rubric=RESEARCH, max_calls=4, max_rounds=2)
CRITIC = Contract(role="critic", output=models.PlanReview, rubric=None, max_calls=1, max_rounds=1)
CONTRACTS = {c.role: c for c in (STRATEGIST, ANALYST, RESEARCHER, CRITIC)}
