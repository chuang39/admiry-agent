"""The Critic (spec 2026-10-07-admiry-capability-layer-design §4, §4.2): judges another role's output by a rubric.
Its plan rubric rides on the S0 second look's own model call (llm.review_plan): one call writes the owner's summary
and checks and the team's grades; the grades are clamped and capped by the checks a dimension fails (K5), and the score
and the verdict computed here (base), never by the model. Its insight rubric grades the Analyst's daily look in a call
of its own (`judge_insight`, K2). Its research rubric, with checks, grades the Researcher's work in a call of its own
(`judge_research`, K4). It never stops an owner's decision, and in K2 nothing automatic exists for it to gate (founder
2026-10-07): its verdict decides what an owner reads."""

from __future__ import annotations

from dataclasses import dataclass

from .. import config, llm, models
from . import base, contracts

GOOD = 4  # a grade under it is an issue the team reads (and, from K2/K4, the next round's feedback)


@dataclass(frozen=True)
class Judged:
    review: models.PlanReview | models.InsightReview | models.ResearchReview
    grades: dict[str, int]
    score: float
    verdict: str
    issues: list[dict]


def judged(
    review: models.PlanReview | models.InsightReview | models.ResearchReview, rubric: base.Rubric = contracts.PLAN
) -> Judged:
    """The rubric applied to the model's answer: the grades (base.grades_of, capped by the checks a dimension fails),
    the score, the verdict, and one issue per dimension under GOOD with the judge's own note ("not graded" for a
    dimension it left out) and, when it failed checks, their ids (`failed`, K5)."""
    raw = [g.model_dump() for g in review.grades]
    grades = base.grades_of(raw, rubric)
    failed = base.failed_of(raw, rubric)
    notes: dict[str, str] = {}
    for g in review.grades:
        notes.setdefault(g.dimension, g.note)
    issues = []
    for d, n in grades.items():
        if n < GOOD:
            issue = {"dimension": d, "text": notes.get(d) or "not graded"}
            if failed.get(d):
                issue["failed"] = failed[d]
            issues.append(issue)
    return Judged(review, grades, base.score(grades, rubric), base.verdict(grades, rubric, review.missing), issues)


def judge_plan(settings: config.Settings, material: dict, *, effort: str | None = None) -> Judged:
    """One model call over `material` (plan_review.material / material_of), judged by the plan rubric. `effort`
    overrides AGENT_LLM_EFFORT for this call only (`agent.eval judge --effort`, K5's measurement); the S0 look and the
    background Critic pass none. llm.LLMError propagates: the caller decides what a look the model could not give
    means."""
    gen = llm.generator(settings, effort=effort) if effort else llm.generator(settings)
    return judged(gen.review_plan(material))


def judge_insight(settings: config.Settings, material: dict, insight: dict) -> Judged:
    """One model call grading `insight` (the Analyst's answer as it wrote it) over the material it read, by the insight
    rubric. llm.LLMError propagates: the Analyst stores the words without a verdict, for the team only."""
    return judged(llm.generator(settings).review_insight(material, insight), contracts.INSIGHT)


def judge_research(settings: config.Settings, material: dict, pages: list[dict], research: dict) -> Judged:
    """One model call grading `research` (the Researcher's answer as it wrote it, or a knowledge file's draft) over
    the material and the pages it read, by the research rubric. llm.LLMError propagates: the Researcher keeps the words
    without a verdict, for the team only."""
    return judged(llm.generator(settings).review_research(material, pages, research), contracts.RESEARCH)
