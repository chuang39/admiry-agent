"""What the grading page shows of one frozen plan (K5 P8): exactly the material the judge reads
(plan_review.material_of, the knowledge as a model reads it), laid out for a person -- never a case id, a flaw's name or
a grade of the judge's. Of the knowledge, every field a person can read is here; left out are only the ones that name
the case or mean nothing to a person: the listing's place id, its point, the locale and the category's id."""

from __future__ import annotations

from .. import plan_review, profile
from .cases import Graded


def of(item: Graded) -> dict:
    material = plan_review.material_of(item.plan, item.knowledge, final_host=None, money=None, blocked=None)
    known = profile.for_model(material["knowledge"])
    cards = material["cards"] or {}
    plan = cards.get("plan") or {}
    hint = known.get("goal_hint") or {}
    return {
        "business": {
            "name": known.get("name"),
            "trade": known.get("category_detail"),
            "area": ", ".join(known.get("service_area") or []),
            "address": (known.get("listing") or {}).get("address"),
            "hours": known.get("hours"),
            "phone": known.get("phone"),
            "website": known.get("website"),
            "services": [
                {"name": s.get("name"), "phrases": s.get("intent_phrases") or [], "price": s.get("price_hint")}
                for s in known.get("services") or []
                if isinstance(s, dict)
            ],
            "usp": known.get("usp") or [],
            "pricing": known.get("pricing_hints") or [],
            "customer_value": known.get("typical_customer_value_usd"),
            "reviews": known.get("reviews_summary"),
            "compliance": known.get("compliance_flags") or [],
            "budget_said": hint.get("monthly_budget_usd"),
            "customers_wanted": hint.get("target_customers_per_month"),
            "focus": hint.get("focus_services") or [],
        },
        "goal": plan.get("goal"),
        "setup": plan.get("setup") or {},
        "channels": plan.get("channels") or [],
        "budget": cards.get("budget") or {},
        "economics": cards.get("economics") or {},
        "rationale": material["rationale"] or {},
        "ads": material["ads"],
    }
