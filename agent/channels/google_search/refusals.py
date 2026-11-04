"""Google's refusals, classified (spec 2026-10-11 §4; plan D13): each RefusalDetail the client read off an error body
sorted into one of five classes by a table over what Google's field path points at and the policy topic it names --
ad_text (a headline or description Google refuses: Admiry's to fix), keyword (a search phrase: Admiry's), destination
(the page: unreachable, refused, a mismatch with the business: the owner's, or Admiry's own page's), business (a fact
the business must show or prove: the call asset's phone, a certificate or licence a restricted category needs: the
owner's), other (anything else, an account-level refusal, a code the table does not know: a person's). Pure: no
database, no HTTP, no words for an owner (agent/ads_refusal.py has those)."""

from __future__ import annotations

from collections.abc import Callable

from .client import RefusalDetail

AD_TEXT, KEYWORD, DESTINATION, BUSINESS, OTHER = "ad_text", "keyword", "destination", "business", "other"
CLASSES = (AD_TEXT, KEYWORD, DESTINATION, BUSINESS, OTHER)
DESTINATION_PREFIX = "DESTINATION"  # DESTINATION_NOT_WORKING, DESTINATION_MISMATCH, DESTINATION_NOT_CRAWLABLE, …
DESTINATION_DOMAINS = ("urlFieldError",)  # the URL fields' own errors
# restricted categories (a certification, a licence or a disclosure the business must hold or show): the owner's
BUSINESS_TOPICS = frozenset(
    {
        "HEALTHCARE",
        "RESTRICTED_HEALTHCARE",
        "RESTRICTED_FINANCIAL_PRODUCTS",
        "FINANCIAL_SERVICES",
        "FINANCIAL_PRODUCTS_AND_SERVICES",
        "LEGAL_REQUIREMENTS",
        "GAMBLING",
        "ALCOHOL",
        "RESTRICTED_DRUG_TERMS",
    }
)
RULES: tuple[tuple[str, Callable[[RefusalDetail], bool]], ...] = (
    (BUSINESS, lambda d: d.kind == "phone"),
    (
        DESTINATION,
        lambda d: (
            d.kind == "url"
            or any(d.code.startswith(f"{domain}:") for domain in DESTINATION_DOMAINS)
            or (d.topic or "").upper().startswith(DESTINATION_PREFIX)
        ),
    ),
    (BUSINESS, lambda d: d.certificate or (d.topic or "").upper() in BUSINESS_TOPICS),
    (KEYWORD, lambda d: d.kind == "keyword"),
    (AD_TEXT, lambda d: d.kind in ("headline", "description") or (d.kind == "ad" and d.topic is not None)),
)


def classify(detail: RefusalDetail) -> str:
    """The class of one refusal detail: the first rule of RULES that holds, else OTHER."""
    for cls, holds in RULES:
        if holds(detail):
            return cls
    return OTHER
