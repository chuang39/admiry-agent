"""Admiry's second look at a plan with the money in place, before the owner confirms it and the ads start (S0,
founder's dev walk 2026-10-07; the setup card's `review` step, setup.py). Two parts. The launch's own check, as a read
(`proposals.launch_refusal`): `blocked`, the sentence the launch would refuse with today, and `ask`, what Admiry needs
from the owner for it -- a question with its input where the owner types the answer (the phone, the address, the
miles), an action otherwise, always with the public tool an assistant calls (`ask_for`); the card, the assistant's
`next` and the e-mail all say the same thing. And one model call over the plan, the business and the money
(`llm.review_plan`): `summary` and `checks`, advisory -- the model never blocks, the owner decides. Once per plan
version, balance and refusal: the stored row (`plan_review`) answers a second look at the same state; a model that
cannot answer leaves no row, and the check and the money stand alone (`model` = unavailable)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from . import ads_mode, ads_run, audit, business, config, db, landing, llm, plan_words, plans, proposals, wallet
from .channels import registry
from .channels.google_search import adapter as google
from .roles import critic

log = logging.getLogger(__name__)

# What each refusal the launch can give asks of the owner: the id the card and the assistant's `next` carry, `input`
# for a question the owner answers in words (envelope.question's kinds), and the public tool an assistant calls with
# the answer. A filled sentence (a label, a floor, a fix) matches its template.
_PHONE = {"id": "phone", "input": "text", "tool": "update_business_profile"}
_LOCATION = {"id": "location", "input": "address", "tool": "update_business_profile"}
_RADIUS = {"id": "radius", "input": "radius", "tool": "update_marketing_plan"}
_BUDGET = {"id": "budget", "tool": "update_marketing_budget"}
_PAGE = {"id": "page", "tool": "update_landing_page"}
_FUNDS = {"id": "funds", "tool": "get_billing"}
_PLAN = {"id": "plan", "tool": "update_marketing_plan"}
_ACCOUNT = {"id": "account", "tool": "connect_account"}
ASKS: tuple[tuple[str, dict], ...] = (
    (ads_run.NO_PHONE, _PHONE),
    (ads_run.NO_LOCATION, _LOCATION),
    (ads_run.NO_RADIUS, _RADIUS),
    (ads_run.OVER_LIMIT, _BUDGET),
    (ads_run.UNDER_FLOOR, _BUDGET),
    (ads_run.NO_PAGE, _PAGE),
    (ads_run.PAGE_UNCHECKED, _PAGE),
    (ads_run.CHECKING_PAGE, _PAGE),
    (ads_run.PAGE_FIX, _PAGE),
    (ads_run.NO_FUNDS, _FUNDS),
    (google.FIRST_PAYMENT, _FUNDS),
    (google.TOO_LITTLE_COPY, _PLAN),
    (google.NOT_SEARCH_PLAN, _PLAN),
    (google.NOT_LINKED, _ACCOUNT),
    (google.LINK_PENDING, _ACCOUNT),
)
# B0 (spec 2026-10-08 §3.1): what Admiry offers on its own surfaces for each refusal, beside the ask an assistant gets --
# `kind`: fix (Admiry does it), ask (only the owner knows: one question), decide (the owner chooses) -- and
# `resolution`, the controls the home draws for it (agent/blockers.py). The MCP side reads ASKS alone, as before.
RESOLUTIONS: dict[str, dict] = {
    google.TOO_LITTLE_COPY: {"kind": "fix", "resolution": "write_copy"},
    ads_run.PAGE_FIX: {"kind": "fix", "resolution": "page_fix"},
    ads_run.PAGE_UNCHECKED: {"kind": "fix", "resolution": "check_page"},
    ads_run.CHECKING_PAGE: {"kind": "fix", "resolution": "check_page"},
    ads_run.NO_PAGE: {"kind": "decide", "resolution": "page"},
    ads_run.NO_PHONE: {"kind": "ask", "resolution": "phone"},
    ads_run.NO_LOCATION: {"kind": "ask", "resolution": "location"},
    ads_run.NO_RADIUS: {"kind": "ask", "resolution": "radius"},
    ads_run.OVER_LIMIT: {"kind": "decide", "resolution": "budget"},
    ads_run.UNDER_FLOOR: {"kind": "decide", "resolution": "budget"},
    ads_run.NO_FUNDS: {"kind": "decide", "resolution": "funds"},
    google.FIRST_PAYMENT: {"kind": "decide", "resolution": "funds"},
    google.NOT_LINKED: {"kind": "decide", "resolution": "account"},
    google.LINK_PENDING: {"kind": "decide", "resolution": "account"},
    google.NOT_SEARCH_PLAN: {"kind": "fix", "resolution": "fresh_plan"},
}
# The words an answer goes in with, by the ask's id: MarketingState's question (tools_state._ask_next) and the home's
PLACEHOLDERS = {
    "phone": "The business phone, with its area code",
    "location": "The address the ads should centre on",
    "radius": "How many miles around the business",
}
_PLACEHOLDER = re.compile(r"\\\{\w+\\\}")  # a `{label}` inside re.escape's output
_PATTERNS = [(re.compile(_PLACEHOLDER.sub(".+?", re.escape(t)), re.DOTALL), t) for t, _ask in ASKS]
_ASK = dict(ASKS)
# a refusal over several places names each place before a reason that does not name it ("Google Search: <its reason>.",
# ads_run._nothing_launches): the place's name is taken off before the sentence is matched (B0 P12)
_PLACE = re.compile("^(?:" + "|".join(re.escape(label) for label in registry.LABELS.values()) + "): ")


def template_for(blocked: str | None, *, place: bool = True) -> str | None:
    """The template of a launch refusal (a filled sentence -- a label, a floor, a fix -- matches its template), or None
    for no refusal or one no row knows. A refusal over several places joins their sentences ("Add funds to start Google
    Search. Add funds to start NewsBreak."): with `place` (the default -- the home's own reading, B0 P12), the first
    place's sentence picks it, its place's name taken off when it leads; `place=False` is the MCP side's reading,
    unchanged since before B0 -- the whole refusal, then its first sentence, with no place's name taken off."""
    if not blocked:
        return None
    first = blocked.split(". ")[0].rstrip(".")
    texts = (blocked, first, _PLACE.sub("", first)) if place else (blocked, first)
    for text in texts:
        for pattern, template in _PATTERNS:
            if pattern.fullmatch(text):
                return template
    return None


def ask_for(blocked: str | None) -> dict | None:
    """The ask for a launch refusal: `{id, input?, text, tool}`, `text` the refusal itself (the owner's words); None
    for no refusal or one no table row knows (`template_for`). The MCP side's ask, exactly as before B0: no place's
    name is taken off a refusal that leads with one (spec §3.1, "the MCP side is unchanged")."""
    template = template_for(blocked, place=False)
    return {**_ASK[template], "text": blocked} if template else None


def resolution_for(blocked: str | None) -> dict | None:
    """What Admiry offers on its own surfaces for a launch refusal (RESOLUTIONS): `{kind, resolution}`; None for no
    refusal or one no row knows."""
    template = template_for(blocked)
    return dict(RESOLUTIONS[template]) if template else None


def ask_id_for(blocked: str | None) -> str | None:
    """The ask id for a launch refusal, for the home's blocker alone (`agent/blockers.py` `describe`): unlike
    `ask_for`, a refusal's place name is taken off when it leads (B0 P12), so the home still asks its one question or
    offers its one choice even when the refusal shown leads with another place's name. None for no refusal or one no
    row knows."""
    template = template_for(blocked)
    return _ASK[template]["id"] if template else None


@dataclass(frozen=True)
class By:
    """Who asked for the look, for its audit row and its `plan_review.actor`: the signed-in owner on the setup card
    (`owner`), or a scheduled job (`job`)."""

    tool: str
    actor: str
    actor_class: str
    ip: str | None = None

    @classmethod
    def owner(cls, email: str, ip: str | None) -> By:
        return cls("web.review_plan", f"web:{email}", "business", ip)

    @classmethod
    def job(cls, name: str) -> By:
        return cls(f"job.{name}", f"job:{name}", "job")


@dataclass(frozen=True)
class Look:
    """A second look: `owner`, what the card and the assistant read -- never a grade, a score or a verdict (those are
    the team's: operator-only) -- and the stored review's id, score and verdict (None when the model could not
    answer)."""

    owner: dict
    review_id: str | None
    score: float | None
    verdict: str | None


def material(
    conn: psycopg.Connection, business_id: str, plan: dict, *, money: dict | None, blocked: str | None
) -> dict:
    """What the judge reads (llm.review_plan) for a stored plan: `material_of` with the business's latest knowledge
    and, for a consultation plan, its landing page's host. The eval (agent/eval) passes neither money nor a refusal:
    it judges the plan alone."""
    knowledge = (business.latest_knowledge(conn, business_id) or (None, {}))[1]
    host = landing.final_host(conn, business_id) if "consultation" in plan["sections"] else None
    return material_of(plan, knowledge, final_host=host, money=money, blocked=blocked)


def material_of(
    plan: dict, knowledge: dict, *, final_host: str | None, money: dict | None, blocked: str | None
) -> dict:
    """What the judge reads, from the plan and the knowledge alone (no database: the eval's frozen plans and the
    grading page read exactly this, K5 P8): a consultation plan's cards and rationale; `ads`, one entry per funded
    place with every search phrase, every phrase left out, all of its ad text, the page it sends people to and what
    the launch's linter would drop (K5 P2a: the cards preview three headlines and no phrase at all); what Admiry
    knows; the money and the launch check's refusal. The ids of the knowledge and of the research the draft read stay
    out of both copies of the rationale (`_as_judged`, capability layer K3 P5, K4 D18)."""
    cards = rationale = None
    ads: list[dict] = []
    sections = plan["sections"]
    if "consultation" in sections:
        named = {**plan, "business_name": plans.name_for(knowledge) or plans.address_for(knowledge)}
        cards = plans.cards(named, final_host=final_host)
        # new dicts, never `pop`: the cards' rationale is the stored section itself
        cards = {**cards, "rationale": _as_judged(cards["rationale"])}
        rationale = _as_judged(sections.get("rationale"))
        category = sections["consultation"]["business"].get("category")
        for entry in plans.funded(sections):
            headlines, descriptions = list(entry.get("headlines") or []), list(entry.get("descriptions") or [])
            # each place's own launch linter (NewsBreak runs to 90 and allows {city}; the rest are Google's 30 / 90)
            _kept_h, _kept_d, dropped = plan_words.lint(entry["channel"], category, headlines, descriptions)
            ads.append(
                {
                    "channel": registry.label(entry["channel"]),
                    "monthly_usd": int(entry["monthly_usd"]),
                    "themes": list(entry.get("themes") or []),
                    "negatives": list(entry.get("negatives") or []),
                    "headlines": headlines,
                    "descriptions": descriptions,
                    "landing_service": entry.get("landing_service"),
                    "lint": dropped,
                }
            )
    return {
        "cards": cards,
        "rationale": rationale,
        "ads": ads,
        "knowledge": knowledge,
        "money": money,
        "blocked": blocked,
    }


TEAM_TRACES = ("knowledge_ids", "research_id")  # what the draft read (K3, K4): the team's record, never the judge's


def _as_judged(rationale: dict | None) -> dict | None:
    """The rationale as the judge read it before K3: the ids of the knowledge and the research the draft read are the
    team's trace of what it read, not the judge's material (plan 2026-10-08-agent-k3 P5, plan 2026-10-08-agent-k4 D18:
    the plan rubric's material stays as K5 calibrated it)."""
    if rationale is None:
        return None
    return {k: v for k, v in rationale.items() if k not in TEAM_TRACES}


def look(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, *, by: By, plan_id: str | None = None
) -> Look | None:
    """The second look, judged by the Critic's plan rubric (capability layer K1): at the latest plan while it is a
    draft (the setup card's), or at the version `plan_id` names unless a newer one superseded it (the background
    Critic's, agent/roles/runner.py); None otherwise. The launch's check is read only for a draft. Once per plan
    version, balance and refusal: a stored row with a score answers instead of the model (a row from before K1, with
    none, does not). A model that cannot answer stores nothing; its audit row says so."""
    plan = plans.get(conn, business_id, plan_id) if plan_id else plans.latest(conn, business_id)
    if plan is None or plan["status"] == plans.SUPERSEDED or (plan_id is None and plan["status"] != plans.DRAFT):
        return None
    blocked = proposals.launch_refusal(conn, business_id, plan["id"]) if plan["status"] == plans.DRAFT else None
    cents, funds = money(conn, business_id, plan)
    # no ad text the launch would take: its compliance check is Admiry's own sentence (NO_COPY_CHECKED)
    short = google.copy_short(plan)
    head = {"plan_id": plan["id"], "blocked": blocked, "ask": ask_for(blocked), "money": funds}
    stored = conn.execute(
        "SELECT id, review, score, verdict FROM plan_review WHERE plan_id = %s AND balance_cents IS NOT DISTINCT FROM "
        "%s AND blocked IS NOT DISTINCT FROM %s AND score IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (plan["id"], cents, blocked),
    ).fetchone()
    if stored is not None:
        return Look(
            {**head, **_words(stored["review"], short), "model": "ok"}, stored["id"], stored["score"], stored["verdict"]
        )
    try:
        judged = critic.judge_plan(settings, material(conn, business_id, plan, money=funds, blocked=blocked))
    except llm.LLMError as e:
        _record(conn, business_id, plan["id"], by, {"model": "unavailable", "error": str(e)})
        return Look({**head, "summary": None, "checks": [], "grounded": None, "model": "unavailable"}, None, None, None)
    review = judged.review.model_dump()
    review_id = db.new_id("rv")
    conn.execute(
        "INSERT INTO plan_review (id, plan_id, business_id, balance_cents, blocked, review, model, score, verdict, "
        "actor) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            review_id,
            plan["id"],
            business_id,
            cents,
            blocked,
            Jsonb(review),
            settings.llm_model,
            judged.score,
            judged.verdict,
            by.actor,
        ),
    )
    _record(
        conn, business_id, plan["id"], by, {"model": "ok", "grounded": judged.review.grounded, "review_id": review_id}
    )
    return Look({**head, **_words(review, short), "model": "ok"}, review_id, judged.score, judged.verdict)


def review(conn: psycopg.Connection, settings: config.Settings, business_id: str, *, by: By) -> dict | None:
    """The setup card's second look (S0): the latest draft's, in the owner's words (`Look.owner`); None when no draft
    waits."""
    looked = look(conn, settings, business_id, by=by)
    return looked.owner if looked else None


# The receipt's next step (pay._tell_owner_paid): the owner who paid and closed the tab learns what comes next -- where
# the plan's review stands while it is in review (L1), the one thing still in its way, or the yes, by the home's link
REVIEW_NEXT = "Next: confirm your plan and your ads start. Confirm here: {link}"
NEEDS_NEXT = "Before your ads can start, Admiry needs one thing from you: {blocked}. Answer here: {link}"


def next_step_words(conn: psycopg.Connection, business_id: str, web_base_url: str) -> str | None:
    """The sentence for the owner's receipt on what comes next for the latest plan (pay._tell_owner_paid): where its
    review stands while it is in review (L1); while it is a draft, the first thing still in its way (the checklist's,
    else the launch's own check) or the yes, by the home's link; None otherwise."""
    from . import launch_check, launch_review  # launch_check imports blockers, which imports this module

    plan = plans.latest(conn, business_id)
    if plan is None:
        return None
    if plan["status"] == plans.IN_REVIEW:
        return launch_review.owner_words(conn, business_id)
    if plan["status"] != plans.DRAFT:
        return None
    link = f"{web_base_url}/?setup={business_id}"
    blocked = launch_check.blocked(conn, config.load(), business_id, plan["id"])
    blocked = blocked or proposals.launch_refusal(conn, business_id, plan["id"])
    if blocked:
        return NEEDS_NEXT.format(blocked=blocked.rstrip("."), link=link)
    return REVIEW_NEXT.format(link=link)


def money(conn, business_id: str, plan: dict) -> tuple[int | None, dict | None]:
    """(balance in cents, the money as the card and the model read it) for a business that prepays: the balance, the
    plan's daily amount (the month ÷ plans.DAYS, as its card says it) and how many days the balance covers at it; (None,
    None) for a business Google bills directly."""
    if ads_mode.current(conn, business_id) != "managed":
        return None, None
    cents = wallet.summary(conn, business_id)["balance_cents"]
    monthly = plans.total_usd(plan["sections"])
    daily = int(monthly) // plans.DAYS if monthly > 0 else None
    return cents, {
        "balance_usd": wallet.to_usd(cents),
        "daily_usd": daily,
        "runway_days": (cents // 100) // daily if daily else None,
    }


# The compliance check of a plan with no ad text the launch would take (the founder's dev walk, 2026-10-08: the model
# passed it, "no ad text was provided, so there are no banned claims"): Admiry's own sentence, never a pass
NO_COPY_CHECKED = "There is no ad text yet, so what the ads will claim has not been checked."


def _words(review: dict, copy_short: bool) -> dict:
    """The owner's words of a stored or fresh review; with no ad text to judge (`copy_short`), the compliance check is
    NO_COPY_CHECKED in its place (or added last, its place in the topics' order). The stored row keeps the model's."""
    checks = review["checks"]
    if copy_short:
        mine = {"topic": "compliance", "ok": False, "text": NO_COPY_CHECKED}
        kept = [c for c in checks if c["topic"] != "compliance"]
        checks = (
            kept + [mine] if len(kept) == len(checks) else [mine if c["topic"] == "compliance" else c for c in checks]
        )
    return {"summary": review["summary"], "checks": checks, "grounded": review["grounded"]}


def _record(conn, business_id: str, plan_id: str, by: By, result: dict) -> None:
    """The look's audit row: web.review_plan for the owner's (counted toward the owner's day, auth.check_llm_cap), or
    job.<name> for a job's (counted toward the roles' own day, roles.runner.ROLE_RUNS_PER_DAY). Never a grade or a
    score: get_audit_log reads these rows."""
    audit.record(
        conn,
        tool=by.tool,
        args={"plan_id": plan_id},
        result=result,
        ok=True,
        business_id=business_id,
        actor=by.actor,
        actor_class=by.actor_class,
        api_key_id=None,
        ip=by.ip,
    )
