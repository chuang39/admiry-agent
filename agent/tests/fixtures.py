"""Canned Business Agent outputs and a stand-in for llm.Generator. Tests never call the Meta Model API. Also a
stand-in for a channel adapter (FakeAdapter), and a generated licensed-image set (`creative_set`)."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image as PilImage

from agent import keys, llm, models
from agent.channels import base

# The website `start` read, as a consultation's pre-answer (plan 2026-10-05-agent-a3p-5-1, Q4): a session a test builds
# as answered in full holds it, as one `start` built from a website_url does, so the website question and the two asked
# without a site wait on nothing
READ_SITE = {"website": {"url": "https://brightsmile.example", "read": True, "source": "known_fact"}}


def knowledge_fixture() -> models.Knowledge:
    return models.Knowledge(
        name="Bright Smile Dental",
        category="dentists",
        category_detail="family dentist",
        service_area=["Palo Alto"],
        services=[
            models.Service(name="Invisalign", intent_phrases=["invisalign palo alto"], price_hint=None),
            models.Service(name="Cleanings", intent_phrases=["teeth cleaning near me"], price_hint="$120"),
        ],
        hours="Mon-Fri 8-5",
        phone="650-555-0100",
        website="https://brightsmile.example",
        pricing_hints=["cleanings $120"],
        typical_customer_value_usd=None,
        usp=["Same-week appointments"],
        reviews_summary=None,
        compliance_flags=["healthcare"],
        locale="en-US",
        goal_hint=models.GoalHint(target_customers_per_month=20, monthly_budget_usd=2000, focus_services=[]),
    )


def turn_fixture(**overrides) -> models.Turn:
    """A Turn with every field null but what a test names (the wire schema requires them all): an answer at 0.95."""
    base = {
        "intent": "answer",
        "say": "",
        "confidence": 0.95,
        "option_id": None,
        "amount_usd": None,
        "miles": None,
        "note": None,
        "services": [],
        "question_id": None,
        "monthly_usd": None,
    }
    return models.Turn(**{**base, **overrides})


def home_services_knowledge(service: str = "Roofing") -> dict:
    """A home-services business (the dental fixture's shape): one service, a phone, no compliance flag."""
    return {
        **knowledge_fixture().model_dump(),
        "name": "Summit Roofing",
        "category": "home_services",
        "category_detail": "roofer",
        "services": [{"name": service, "intent_phrases": [f"{service.lower()} near me"], "price_hint": None}],
        "compliance_flags": [],
    }


def plan_draft_fixture(google: float = 1300, meta: float = 700) -> models.PlanDraft:
    return models.PlanDraft(
        pages=[
            models.PageSpec(
                service="Invisalign", path="/invisalign-palo-alto", angle="Straighter teeth without metal braces"
            )
        ],
        gbp_notes=["Add Invisalign to services"],
        discovery_fixes=["Add an FAQ block"],
        ads=[
            models.AdsChannel(
                channel="google_search",
                monthly_usd=google,
                themes=["invisalign palo alto"],
                negatives=["jobs"],
                headlines=["Invisalign in Palo Alto", "Same-Week Appointments", "Clear Aligners, Family Care"],
                descriptions=[
                    "Clear aligners from a family dentist. Book online.",
                    "Same-week consultations in Palo Alto; see the cost before anything starts.",
                ],
                landing_service="Invisalign",
            ),
            models.AdsChannel(
                channel="meta",
                monthly_usd=meta,
                themes=["adults 25-45 near Palo Alto"],
                negatives=[],
                headlines=["A straighter smile"],
                descriptions=["Invisalign consultations this week."],
                landing_service="Invisalign",
            ),
        ],
        creative_angles=["offer", "before/after", "testimonial"],
        tracking=["calls", "form on your page", "appointment link"],
        review_request_after_days=3,
        autopilot="safe",
        rationale="Search catches people looking now; Meta reminds the neighbourhood.",
    )


def page_copy_fixture(**changes) -> models.PageCopy:
    """The words of Bright Smile Dental's page, every fact from knowledge_fixture() (the $120 is its pricing hint,
    the same-week appointments its usp); `changes` swaps whole fields."""
    copy = models.PageCopy(
        headline="Invisalign in Palo Alto",
        subheadline="Clear aligners from Bright Smile Dental, a family dentist in Palo Alto.",
        offer_line="Same-week appointments.",
        trust_points=["Same-week appointments", "A family dentist in Palo Alto", "Cleanings $120"],
        service_paragraphs=[
            (
                "Your first visit is a consultation: we look at your teeth, explain how Invisalign would work for "
                "you and tell you what it costs before anything starts."
            ),
            (
                "Invisalign straightens teeth with clear aligners instead of metal braces. You change to a new set "
                "every few weeks and come in now and then so we can see how it is going."
            ),
        ],
        faq=[
            models.FaqItem(
                q="What does the first visit cost?", a="Ask us when you call; we tell you before anything starts."
            ),
            models.FaqItem(
                q="Do you take my insurance?", a="Call us with your plan and we check it before your visit."
            ),
            models.FaqItem(q="Is there parking?", a="Ask when you book and we tell you where to park."),
        ],
        cta_label="Call to book",
    )
    return copy.model_copy(update=changes)


def site_words_fixture() -> models.SiteWords:
    """The roofer sample's words (agent/data/sites/samples/roofer.json): they pass agent/sites/words.py `lint`."""
    from agent.sites import samples

    return samples.load("roofer", style="solid").words.model_copy(deep=True)


class FakeGenerator:
    """Stands in for llm.Generator: canned outputs, recorded calls, optional failure (`error` fails
    every call, `plan_error` only draft_plan). write_page/revise_page answer `copies` in order, the last one
    repeating (page_copy_fixture() by default): a test puts copy the linter must refuse first. The Analyst's two calls
    (K2) answer `insights` and `insight_reviews` the same way; `insight_error` fails only write_insight and
    `insight_review_error` only review_insight. The Researcher's two calls (K4) answer `researches` and
    `research_reviews` the same way; `research_error` fails only write_research and `research_review_error` only
    review_research. The knowledge writer's call (K4, agent/research.py) answers `knowledge_drafts` the same way;
    `error` fails it too. The harness's call (K6) answers `turns` by the owner's words; `error` fails it too. The
    Reviewer's last look (L1) answers `configured_reviews` the same way; `error` fails it too. The refusal summary's
    call (Part B) answers `rule_proposals` the same way; `error` fails it too."""

    def __init__(
        self,
        knowledge=None,
        draft=None,
        error: str | None = None,
        plan_error: str | None = None,
        reads: dict[str, models.AnswerRead] | None = None,
        adjusts: dict[str, models.AdjustRead] | None = None,
        mails: dict[str, models.MailRead] | None = None,
        answers_to: dict[str, models.MailAnswer] | None = None,
        copies: list[models.PageCopy] | None = None,
        supports: dict[str, models.SupportRead] | None = None,
        sites: list[models.SiteWords] | None = None,
        reviews: list[models.PlanReview] | None = None,
        configured_reviews: list[models.ConfiguredReview] | None = None,
        insights: list[models.Insight] | None = None,
        insight_reviews: list[models.InsightReview] | None = None,
        insight_error: str | None = None,
        insight_review_error: str | None = None,
        researches: list[models.ResearchBrief] | None = None,
        research_reviews: list[models.ResearchReview] | None = None,
        research_error: str | None = None,
        research_review_error: str | None = None,
        knowledge_drafts: list[models.KnowledgeDraft] | None = None,
        turns: dict[str, models.Turn] | None = None,
        rule_proposals: list[models.RuleProposals] | None = None,
    ):
        self.knowledge_drafts = list(knowledge_drafts or [])
        self.knowledge_calls: list[dict] = []
        self.turns = turns or {}
        self.turn_calls: list[dict] = []
        self.rule_proposals = list(rule_proposals or [])
        self.rule_calls: list[dict] = []
        self.insights = list(insights or [])
        self.insight_reviews = list(insight_reviews or [])
        self.insight_error, self.insight_review_error = insight_error, insight_review_error
        self.insight_calls: list[dict] = []
        self.insight_review_calls: list[dict] = []
        self.researches = list(researches or [])
        self.research_reviews = list(research_reviews or [])
        self.research_error, self.research_review_error = research_error, research_review_error
        self.research_calls: list[dict] = []
        self.research_review_calls: list[dict] = []
        self.knowledge, self.draft, self.error, self.plan_error = knowledge, draft, error, plan_error
        self.reads = reads or {}
        self.adjusts = adjusts or {}
        self.mails = mails or {}
        self.answers_to = answers_to or {}
        self.copies = list(copies or [page_copy_fixture()])
        self.supports = supports or {}
        self.sites = list(sites or [])
        self.reviews = list(reviews or [])
        self.review_calls: list[dict] = []
        self.configured_reviews = list(configured_reviews or [])
        self.configured_calls: list[dict] = []
        self.site_calls: list[dict] = []
        self.extract_calls: list[dict] = []
        self.plan_calls: list[dict] = []
        self.read_calls: list[dict] = []
        self.adjust_calls: list[dict] = []
        self.mail_calls: list[dict] = []
        self.answer_calls: list[dict] = []
        self.page_calls: list[dict] = []
        self.support_calls: list[str] = []

    def extract_knowledge(self, text, *, pages, listing, locale):
        self.extract_calls.append({"text": text, "pages": pages, "listing": listing, "locale": locale})
        if self.error:
            raise llm.LLMError(self.error)
        return self.knowledge

    def draft_plan(
        self,
        knowledge,
        goal,
        *,
        instruction=None,
        previous=None,
        consultation=None,
        trade_knowledge=None,
        research=None,
        avoid=None,
    ):
        self.plan_calls.append(
            {
                "knowledge": knowledge,
                "goal": goal,
                "instruction": instruction,
                "previous": previous,
                "consultation": consultation,
                "trade_knowledge": trade_knowledge,
                "research": research,
                "avoid": avoid,
            }
        )
        if self.error or self.plan_error:
            raise llm.LLMError(self.error or self.plan_error)
        return self.draft

    def read_answer(self, question, options, text):
        self.read_calls.append({"question": question, "options": options, "text": text})
        if self.error:
            raise llm.LLMError(self.error)
        return self.reads.get(text) or models.AnswerRead(
            option_id=None, amount_usd=None, miles=None, note=None, understood=False, services=[]
        )

    def read_adjust(self, text, cards):
        """A read of words the test gave none for: they name no field and ask for a change (change_plan rewrites the
        plan from such words; the homepage changes nothing)."""
        self.adjust_calls.append({"text": text, "cards": cards})
        if self.error:
            raise llm.LLMError(self.error)
        return self.adjusts.get(text) or models.AdjustRead(
            monthly_budget_usd=None,
            level=None,
            radius_miles=None,
            offer=None,
            objective=None,
            channel=None,
            channel_monthly_usd=None,
            understood=False,
            asks_for_change=True,
        )

    def read_mail(self, text, cards):
        self.mail_calls.append({"text": text, "cards": cards})
        if self.error:
            raise llm.LLMError(self.error)
        return self.mails.get(text) or models.MailRead(
            intent="unclear",
            confidence=0.0,
            monthly_budget_usd=None,
            level=None,
            radius_miles=None,
            offer=None,
            objective=None,
            question=None,
        )

    def answer_question(self, question, material):
        self.answer_calls.append({"question": question, "material": material})
        if self.error:
            raise llm.LLMError(self.error)
        return self.answers_to.get(question) or models.MailAnswer(text="", grounded=False)

    def _next_copy(self) -> models.PageCopy:
        if self.error:
            raise llm.LLMError(self.error)
        return self.copies.pop(0) if len(self.copies) > 1 else self.copies[0]

    def write_page(self, kit, recipe, brief):
        self.page_calls.append({"method": "write_page", "kit": kit, "recipe": recipe, "brief": brief})
        return self._next_copy()

    def revise_page(self, copy, instruction, kit, recipe):
        self.page_calls.append(
            {"method": "revise_page", "copy": copy, "instruction": instruction, "kit": kit, "recipe": recipe}
        )
        return self._next_copy()

    def write_site(self, facts, family, message, direction):
        self.site_calls.append({"facts": facts, "family": family, "message": message, "direction": direction})
        if self.error:
            raise llm.LLMError(self.error)
        sites = self.sites or [site_words_fixture()]
        return sites.pop(0) if len(sites) > 1 else sites[0]

    def read_support(self, text):
        self.support_calls.append(text)
        if self.error:
            raise llm.LLMError(self.error)
        return self.supports.get(text) or models.SupportRead(support="none", support_summary=None)

    def read_turn(self, material, text, *, skill):
        """The harness's one call (K6 slice 1): the Turn a test gave for these words, else unclear at low confidence."""
        self.turn_calls.append({"material": material, "text": text, "skill": skill})
        if self.error:
            raise llm.LLMError(self.error)
        return self.turns.get(text) or turn_fixture(intent="unclear", confidence=0.3)

    def review_plan(self, material):
        """Admiry's second look (S0): `reviews` in order, the last one repeating; none given, a grounded two-check
        review that names nothing in particular."""
        self.review_calls.append({"material": material})
        if self.error:
            raise llm.LLMError(self.error)
        reviews = self.reviews or [plan_review_fixture()]
        return reviews.pop(0) if len(reviews) > 1 else reviews[0]

    def review_configured(self, material):
        """The Reviewer's last look (L1): `configured_reviews` in order, the last one repeating; none given, a match."""
        self.configured_calls.append({"material": material})
        if self.error:
            raise llm.LLMError(self.error)
        found = self.configured_reviews or [models.ConfiguredReview(matches=True, issues=[], confidence=0.9)]
        return found.pop(0) if len(found) > 1 else found[0]

    def write_insight(self, material, feedback=None):
        """The Analyst's look (K2): `insights` in order, the last one repeating; none given, insight_fixture()."""
        self.insight_calls.append({"material": material, "feedback": feedback})
        if self.error or self.insight_error:
            raise llm.LLMError(self.error or self.insight_error)
        found = self.insights or [insight_fixture()]
        return found.pop(0) if len(found) > 1 else found[0]

    def review_insight(self, material, insight):
        """The Critic's grades of a look (K2): `insight_reviews` in order, the last one repeating; none given, all 5."""
        self.insight_review_calls.append({"material": material, "insight": insight})
        if self.error or self.insight_review_error:
            raise llm.LLMError(self.error or self.insight_review_error)
        found = self.insight_reviews or [insight_review_fixture()]
        return found.pop(0) if len(found) > 1 else found[0]

    def write_research(self, material, pages, feedback=None):
        """The Researcher (K4): `researches` in order, the last one repeating; none given, research_fixture()."""
        self.research_calls.append({"material": material, "pages": pages, "feedback": feedback})
        if self.error or self.research_error:
            raise llm.LLMError(self.error or self.research_error)
        found = self.researches or [research_fixture()]
        return found.pop(0) if len(found) > 1 else found[0]

    def review_research(self, material, pages, research):
        """The Critic's grades of research (K4): `research_reviews` in order, the last repeating; none given, all 5."""
        self.research_review_calls.append({"material": material, "pages": pages, "research": research})
        if self.error or self.research_review_error:
            raise llm.LLMError(self.error or self.research_review_error)
        found = self.research_reviews or [research_review_fixture()]
        return found.pop(0) if len(found) > 1 else found[0]

    def propose_rules(self, material):
        """The refusal summary's call (Part B phase 1): `rule_proposals` in order, the last one repeating; none
        given answers no proposals; `error` fails it too."""
        self.rule_calls.append(material)
        if self.error:
            raise llm.LLMError(self.error)
        if not self.rule_proposals:
            return models.RuleProposals(proposals=[])
        return self.rule_proposals.pop(0) if len(self.rule_proposals) > 1 else self.rule_proposals[0]

    def write_knowledge(self, material, pages, feedback=None):
        """A knowledge draft (K4): `knowledge_drafts` in order, the last repeating; none given, knowledge_draft_fixture()."""
        self.knowledge_calls.append({"material": material, "pages": pages, "feedback": feedback})
        if self.error:
            raise llm.LLMError(self.error)
        found = self.knowledge_drafts or [knowledge_draft_fixture()]
        return found.pop(0) if len(found) > 1 else found[0]


def insight_fixture(**overrides) -> models.Insight:
    """A grounded look at the analyst_biz day ($23.10, 2 calls) with one finding and no recommendation."""
    return models.Insight(
        **{
            "summary": "A quiet day: $23.10 spent and 2 calls.",
            "findings": [models.InsightFinding(text="Google Search brought 2 calls.")],
            "recommendations": [],
            "grounded": True,
            **overrides,
        }
    )


def insight_review_fixture(
    grade: int = 5, low: dict[str, int] | None = None, missing: list[str] | None = None
) -> models.InsightReview:
    """Every insight dimension at `grade`, those in `low` at theirs, each note "<dimension> note"."""
    from agent.roles import contracts

    return models.InsightReview(
        grades=[
            models.InsightGrade(dimension=d, grade=(low or {}).get(d, grade), note=f"{d} note")
            for d in contracts.INSIGHT.ids
        ],
        missing=missing or [],
    )


def research_fixture(**overrides) -> models.ResearchBrief:
    """Research on the dental fixture's market that every guard passes: a finding on the benchmark row ($72.97), one on
    the business, searches to aim for and to keep out, an offer the knowledge states, one unknown."""
    return models.ResearchBrief(
        **{
            "findings": [
                models.ResearchFinding(
                    text="A lead from search ads costs dentists about $72.97 on average.",
                    source_ids=["benchmark:dentists:google_search"],
                    confidence="medium",
                ),
                models.ResearchFinding(
                    text="Same-week appointments set the practice apart.", source_ids=["business"], confidence="high"
                ),
            ],
            "suggested_themes": ["invisalign palo alto", "teeth cleaning near me"],
            "suggested_negatives": ["dental jobs", "dental school"],
            "offer_angles": ["Same-week appointments for cleanings."],
            "unknowns": ["How many people nearby search for Invisalign each month."],
            "grounded": True,
            **overrides,
        }
    )


def research_review_fixture(
    grade: int = 5,
    low: dict[str, int] | None = None,
    missing: list[str] | None = None,
    failed: dict[str, list[str]] | None = None,
) -> models.ResearchReview:
    """Every research dimension at `grade`, those in `low` at theirs, each failing the checks `failed` names for it
    (none by default), each note "<dimension> note"."""
    from agent.roles import contracts

    return models.ResearchReview(
        grades=[
            models.ResearchGrade(
                dimension=d, failed=(failed or {}).get(d, []), grade=(low or {}).get(d, grade), note=f"{d} note"
            )
            for d in contracts.RESEARCH.ids
        ],
        missing=missing or [],
    )


def knowledge_draft_fixture(**overrides) -> models.KnowledgeDraft:
    """A dental Google Search guide every check passes and K3's loader reads: one figure, from the benchmark row
    ($72.97); the notes cited on two lines; nothing uncited holds a digit."""
    line = models.KnowledgeLine
    return models.KnowledgeDraft(
        **{
            "title": "Google Search",
            "summary": "How patients search for a dentist, which searches to buy and which to keep out.",
            "key_points": [
                line(text="Give each service its own ads.", source_ids=["research-verticals"]),
                line(text="Keep out dental jobs, schools and supplies.", source_ids=[]),
            ],
            "details": [
                line(
                    text="Emergency searches happen after hours, so the call button matters.",
                    source_ids=["research-verticals"],
                )
            ],
            "numbers": [
                line(
                    text="Dentists' average cost per lead on Google Search: $72.97.",
                    source_ids=["bench-dentists-google-search"],
                )
            ],
            "do_not_say": [line(text="Another practice's name.", source_ids=[])],
            "confidence": "medium",
            "grounded": True,
            **overrides,
        }
    )


def plan_review_fixture() -> models.PlanReview:
    return models.PlanReview(
        summary="Google Search ads for the service you chose, around the business, at the plan's daily budget.",
        checks=[
            models.ReviewCheck(topic="services", ok=True, text="The ads name the service you chose."),
            models.ReviewCheck(topic="money", ok=True, text="The daily budget fits the balance."),
        ],
        grounded=True,
        grades=[
            models.PlanGrade(dimension=d, grade=4, note="fine")
            for d in ("fit", "budget", "copy", "keywords", "estimate", "clarity")
        ],
        missing=[],
    )


def business_with_key(conn, scopes=("read", "manage"), biz_id="biz_1", claim_state="draft", cls="business") -> str:
    """A business row and a key for it; returns the key's plaintext."""
    conn.execute(
        "INSERT INTO business (id, claim_state) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (biz_id, claim_state)
    )
    # platform keys are minted unbound (keys.mint refuses a business for them); a test may bind one afterwards
    plaintext, _ = keys.mint(
        conn, business_id=None if cls == "platform" else biz_id, cls=cls, scopes=scopes, label="muse"
    )
    return plaintext


class FakeAdapter:
    """A channel adapter that implements the whole protocol (agent/channels/base.py) and does nothing: tests register
    one under `channel` to exercise the registry without a platform. It describes itself (capabilities,
    connect_steps, its words) and adds no launch rule of its own; every call that would reach a platform raises
    base.Unsupported, so a test that reaches one by mistake fails loudly."""

    words = base.Words(
        pause_owed="The fake channel was not told yet; an Admiry operator will pause your ads there",
        refused_change="The fake channel did not take all of your latest change; approve a changed version",
        checked="checked your ads for the fake channel (a test run: nothing is shown or spent)",
        checked_status="your ads were checked and found fine; they are not running yet",
        checked_badge="Checked; turns on once execution is live",
        held_back="live, but the fake channel is holding your ads back",
        repaused="finished pausing your ads on the fake channel",
        no_fee="Admiry charges no fee",
    )

    def __init__(self, channel: str = "fake"):
        self.channel = channel

    def capabilities(self) -> base.Capabilities:
        return base.Capabilities(
            create=True,
            update=True,
            pause=True,
            metrics=True,
            conversions=False,
            leads=False,
            modes={"own_account"},
            min_monthly_usd=100,
            geo="radius",
            lead_formats={"call", "form"},
        )

    def connect_steps(self, account, mode):
        return [base.Step("Connect the fake channel")]

    def check_account(self, conn, settings, account):
        raise base.Unsupported("check_account")

    def precheck(self, conn, settings, business_id, plan, knowledge):
        return None

    def build(self, conn, settings, business_id, plan, knowledge, account, *, http=None):
        raise base.Unsupported("build")

    def launch(self, conn, settings, account, build, existing, *, http=None, validate_only):
        raise base.Unsupported("launch")

    def enable(self, conn, settings, account, plan, resources, *, http=None, seen=None):
        raise base.Unsupported("enable")

    def update(self, conn, settings, account, resources, old_plan, build, *, http=None):
        raise base.Unsupported("update")

    def set_paused(self, conn, settings, account, resources, paused, *, http=None, validate_only=False):
        raise base.Unsupported("set_paused")

    def pull_metrics(self, conn, settings, account, resources, days, *, http=None):
        raise base.Unsupported("pull_metrics")

    def pull_issues(self, conn, settings, account, row, *, http=None):
        raise base.Unsupported("pull_issues")

    def push_conversions(self, settings, account, events, *, http=None):
        raise base.Unsupported("push_conversions")

    def pull_leads(self, settings, account, since, *, http=None):
        raise base.Unsupported("pull_leads")

    def landing_url(self, page_url):
        raise base.Unsupported("landing_url")

    def carried_url(self, resources):
        raise base.Unsupported("carried_url")

    def move_page(self, conn, settings, account, resources, running_plan, build, *, http=None, with_money):
        raise base.Unsupported("move_page")

    def client(self, *, budget_s=None):
        raise base.Unsupported("client")

    def ops_sent(self, http):
        return 0


# the pictures NewsBreak takes are at least 1200 x 627; the generated ones are the 16:9 it recommends
CREATIVE_SIZE = (1200, 675)


def creative_picture(
    path: Path, *, size: tuple[int, int] = CREATIVE_SIZE, fmt: str = "JPEG", color=(30, 90, 160)
) -> Path:
    """One generated picture at `path` (its folders made): a solid colour, so it is the same bytes every run and a few
    kilobytes. Tests make the broken ones (too small, too big, not a picture) from this."""
    path.parent.mkdir(parents=True, exist_ok=True)
    PilImage.new("RGB", size, color).save(path, format=fmt, quality=80)
    return path


def creative_set(root: Path) -> Path:
    """A licensed image set laid out the way `agent/static/creative` ships one (M1 Task 3), for the tests: generated
    1200 x 675 JPEGs and the `manifest.json` that lists them -- a picture each for home services and the fallback, two
    for dentists (a general one first, then one for Invisalign). Returns `root`; a test points `creative.ROOT` at it."""
    entries = [
        ("home_services/1.jpg", "home_services", [], (60, 140, 70), "A solid green test picture"),
        ("dentists/1.jpg", "dentists", [], (30, 90, 160), "A solid blue test picture"),
        ("dentists/2.jpg", "dentists", ["Invisalign"], (150, 60, 120), "A solid purple test picture"),
        ("fallback/1.jpg", "fallback", [], (120, 120, 120), "A solid grey test picture"),
    ]
    for file, _category, _services, color, _alt in entries:
        creative_picture(root / file, color=color)
    manifest = {
        "schema": {},
        "entries": [
            {
                "file": file,
                "category": category,
                "services": services,
                "licence": "Test picture: no licence needed",
                "source": "generated by agent/tests/fixtures.py",
                "alt": alt,
            }
            for file, category, services, _color, alt in entries
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return root
