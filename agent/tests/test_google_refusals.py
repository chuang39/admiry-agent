"""spec 2026-10-11 §4: each refusal detail classified -- ad_text, keyword, destination, business, other -- by a table
over what Google's path points at and the policy topic (plan D13); the adapter carries the details into the
lifecycle's PlatformError."""

import pytest

from agent.channels.base import PlatformError
from agent.channels.google_search import adapter as google
from agent.channels.google_search import refusals
from agent.channels.google_search.client import AdsError, RefusalDetail


def detail(
    kind: str, *, topic: str | None = None, code: str = "policyFindingError: POLICY_FINDING", certificate: bool = False
) -> RefusalDetail:
    return RefusalDetail(
        code=code,
        operation=5,
        kind=kind,
        position=1 if kind in ("headline", "description") else None,
        field="mutate_operations[5].x",
        topic=topic,
        type="PROHIBITED" if topic else None,
        exemptible=False,
        certificate=certificate,
        text="Invisalign Deals",
    )


@pytest.mark.parametrize(
    ("found", "cls"),
    [
        (detail("headline", topic="TRADEMARKS_IN_AD_TEXT"), refusals.AD_TEXT),
        (detail("description", code="stringLengthError: TOO_LONG"), refusals.AD_TEXT),
        (detail("ad", topic="PUNCTUATION_AND_SYMBOLS"), refusals.AD_TEXT),
        (detail("keyword", code="policyViolationError: POLICY_ERROR", topic="Trademarks"), refusals.KEYWORD),
        (detail("keyword", code="criterionError: INVALID_KEYWORD_TEXT"), refusals.KEYWORD),
        (detail("url", code="urlFieldError: INVALID_URL"), refusals.DESTINATION),
        (detail("headline", topic="DESTINATION_MISMATCH"), refusals.DESTINATION),
        (detail("ad", topic="DESTINATION_NOT_WORKING"), refusals.DESTINATION),
        (detail("phone", code="assetError: INVALID_PHONE_NUMBER"), refusals.BUSINESS),
        (detail("ad", topic="HEALTHCARE", certificate=True), refusals.BUSINESS),
        (detail("headline", topic="RESTRICTED_FINANCIAL_PRODUCTS"), refusals.BUSINESS),
        (detail("budget", code="campaignBudgetError: INVALID_AMOUNT"), refusals.OTHER),
        (detail("criterion", code="criterionError: INVALID_PROXIMITY"), refusals.OTHER),
        (detail("ad"), refusals.OTHER),  # an ad with no topic: nothing says what Google minds
        (detail("other", code="requestError: UNKNOWN"), refusals.OTHER),
    ],
)
def test_each_detail_is_classified_by_its_path_and_topic(found, cls):
    assert refusals.classify(found) == cls


def test_the_classes_are_the_five_the_spec_names():
    assert refusals.CLASSES == ("ad_text", "keyword", "destination", "business", "other")


# ---------------------------------------------------------------- item 7: the table's order decides a conflict


def test_a_phone_outranks_a_destination_topic_riding_along():
    """D13 lists "a phone is business" first: a call-asset refusal stays business even when it also carries a
    DESTINATION_* topic (not realistic together, but the table's order must still hold)."""
    found = detail("phone", code="assetError: INVALID_PHONE_NUMBER", topic="DESTINATION_MISMATCH")
    assert refusals.classify(found) == refusals.BUSINESS


def test_a_url_outranks_a_certificate_constraint_riding_along():
    """D13 lists the URL rule before the certificate rule: a refused URL is destination (the page), never business,
    even when the same detail also carries a certificate constraint."""
    found = detail("url", code="urlFieldError: INVALID_URL", topic="HEALTHCARE", certificate=True)
    assert refusals.classify(found) == refusals.DESTINATION


def test_a_restricted_topic_outranks_the_keyword_kind():
    """D13 lists the certificate/restricted-topic rule before the keyword rule: a keyword refused for a restricted
    category (the owner's to certify or prove, not Admiry's to just drop) is business, not keyword."""
    found = detail("keyword", code="criterionError: INVALID_KEYWORD_TEXT", topic="HEALTHCARE")
    assert refusals.classify(found) == refusals.BUSINESS


def test_the_adapter_carries_the_details_into_the_platform_error():
    e = AdsError("refused", codes=("policyFindingError: POLICY_FINDING",), status=400, details=(detail("headline"),))
    out = google._platform(e)
    assert isinstance(out, PlatformError) and out.retryable is False and out.details == e.details
    assert google._platform(AdsError("busy", status=503)).details == ()
    assert PlatformError("x", retryable=True).details == ()
