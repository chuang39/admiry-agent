"""The Google Ads link adapter (spec 2026-09-25 rev. 2 §5, §9): Customer IDs, the REST calls over MockTransport, and
the link rows — one per business, one business per Customer ID, verification on ACTIVE that never downgrades."""

import json
import threading
from urllib.parse import parse_qs

import httpx
import pytest

from agent import agreements, business, claims, config, db, google_ads_link, google_auth
from agent.google_ads_link import LinkError, LinkRefused, Remote
from agent.tests.conftest import TEST_DB_URL
from agent.tests.test_claims import draft_business

CID = "1234567890"
MCC = "1112223333"
RESOURCE = f"customers/{MCC}/customerClientLinks/{CID}~42"
SECRETS = ("devtok_SECRET", "1//refresh_SECRET", "ya29.SECRET", "GOCSPX-x")


def claimed_agreed(conn, biz="biz_1", email="owner@example.com", agree=True):
    """A business claimed by `email` whose owner accepted the current agreement (as /connect leaves it)."""
    draft_business(conn, biz)
    claims.claim(conn, biz, email)
    if agree:
        agreements.accept(conn, business_id=biz, email=email, ip="203.0.113.5")


def pending_link(conn, biz="biz_1", cid=CID, email="owner@example.com"):
    """claimed_agreed + an invitation on its way."""
    claimed_agreed(conn, biz, email)
    return google_ads_link.save(conn, biz, cid, status="pending", resource_name=RESOURCE if cid == CID else None)


def google_ads(sent: list, *, token=None, mutate=None, search=None) -> httpx.Client:
    """A client whose transport answers like Google: the token endpoint, the link mutate and the search. A reply
    may be a callable (it gets the request; raising stands for a network failure)."""

    def handler(request):
        sent.append(request)
        url = str(request.url)
        if url == google_auth.TOKEN_URL:
            reply = token or httpx.Response(
                200, json={"access_token": "ya29.SECRET", "expires_in": 3599, "token_type": "Bearer"}
            )
        elif url.endswith("/customerClientLinks:mutate"):
            reply = mutate
        elif url.endswith("/googleAds:search"):
            reply = search
        else:
            reply = httpx.Response(404)
        return reply(request) if callable(reply) else reply

    return httpx.Client(transport=httpx.MockTransport(handler))


def failure(status, code=None, rpc_status="INVALID_ARGUMENT"):
    """Google's error body; its own message carries a secret the adapter must never repeat."""
    details = (
        [
            {
                "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                "errors": [{"errorCode": code, "message": "raw words with devtok_SECRET"}],
            }
        ]
        if code
        else []
    )
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": "raw devtok_SECRET", "status": rpc_status, "details": details}},
    )


def _network(request):
    raise httpx.ConnectError("connection refused", request=request)


def _undecodable(request):
    raise httpx.DecodingError("bad gzip devtok_SECRET", request=request)  # an HTTPError that is no TransportError


def test_normalize_customer_id_takes_dashes_and_spaces():
    for text in ("123-456-7890", " 123 456 7890 ", "1234567890"):
        assert google_ads_link.normalize_customer_id(text) == CID
    assert google_ads_link.format_customer_id(CID) == "123-456-7890"


@pytest.mark.parametrize(
    "text", ["123456789", "12345678901", "123-456-789a", "", "١٢٣٤٥٦٧٨٩٠", "123-456-7890" + " " * 40]
)
def test_normalize_customer_id_refuses_anything_else(text):
    with pytest.raises(LinkRefused) as e:
        google_ads_link.normalize_customer_id(text)
    assert e.value.status == 400 and "10 digits" in str(e.value)


def test_the_invitation_exchanges_the_refresh_token_then_mutates_with_the_three_headers(ads_api):
    sent = []
    http = google_ads(sent, mutate=httpx.Response(200, json={"result": {"resourceName": RESOURCE}}))
    assert google_ads_link.send(ads_api, CID, http=http) == RESOURCE
    token, mutate = sent
    assert parse_qs(token.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["1//refresh_SECRET"],
        "client_id": ["123.apps.googleusercontent.com"],
        "client_secret": ["GOCSPX-x"],
    }
    assert str(mutate.url) == f"https://googleads.googleapis.com/v25/customers/{MCC}/customerClientLinks:mutate"
    assert mutate.method == "POST" and mutate.headers["authorization"] == "Bearer ya29.SECRET"
    assert mutate.headers["developer-token"] == "devtok_SECRET" and mutate.headers["login-customer-id"] == MCC
    assert json.loads(mutate.content) == {
        "operation": {"create": {"clientCustomer": f"customers/{CID}", "status": "PENDING"}}
    }


def test_the_status_read_sends_the_gaql_and_takes_the_newest_invitation(ads_api):
    sent = []
    rows = [
        {
            "customerClientLink": {
                "resourceName": RESOURCE,
                "clientCustomer": f"customers/{CID}",
                "status": "ACTIVE",
                "managerLinkId": "42",
            }
        },
        {
            "customerClientLink": {
                "resourceName": f"customers/{MCC}/customerClientLinks/{CID}~7",
                "clientCustomer": f"customers/{CID}",
                "status": "REFUSED",
                "managerLinkId": "7",
            }
        },
    ]
    http = google_ads(sent, search=httpx.Response(200, json={"results": rows}))
    assert google_ads_link.read(ads_api, CID, http=http) == Remote(status="active", resource_name=RESOURCE)
    search = sent[1]
    assert str(search.url) == f"https://googleads.googleapis.com/v25/customers/{MCC}/googleAds:search"
    assert search.headers["login-customer-id"] == MCC and search.headers["developer-token"] == "devtok_SECRET"
    assert json.loads(search.content) == {
        "query": (
            "SELECT customer_client_link.client_customer, customer_client_link.status, customer_client_link.resource_name, "
            "customer_client_link.manager_link_id FROM customer_client_link "
            f"WHERE customer_client_link.client_customer = 'customers/{CID}'"
        )
    }


def test_no_invitation_in_google_reads_as_cancelled_and_canceled_is_spelled_our_way(ads_api):
    none = google_ads([], search=httpx.Response(200, json={}))
    assert google_ads_link.read(ads_api, CID, http=none) == Remote(
        status="cancelled", resource_name=None, note=google_ads_link.NO_INVITATION
    )
    one = {"results": [{"customerClientLink": {"resourceName": RESOURCE, "status": "CANCELED", "managerLinkId": "42"}}]}
    assert (
        google_ads_link.read(ads_api, CID, http=google_ads([], search=httpx.Response(200, json=one))).status
        == "cancelled"
    )


MANAGED = {"managerLinkError": "ALREADY_MANAGED_BY_THIS_MANAGER"}


def test_an_invitation_already_on_its_way_counts_as_sent(ads_api):
    invited = failure(400, {"managerLinkError": "ALREADY_INVITED_BY_THIS_MANAGER"})
    assert google_ads_link.send(ads_api, CID, http=google_ads([], mutate=invited)) is None


def test_an_account_already_managed_is_refused_unless_this_business_holds_it(ads_api):
    with pytest.raises(LinkRefused) as e:  # an account nobody here asked for
        google_ads_link.send(ads_api, CID, http=google_ads([], mutate=failure(400, MANAGED)))
    assert e.value.status == 409 and str(e.value) == google_ads_link.ALREADY_LINKED
    assert not any(s in str(e.value) for s in SECRETS) and e.value.__cause__ is None
    assert google_ads_link.send(ads_api, CID, held=True, http=google_ads([], mutate=failure(400, MANAGED))) is None


def test_a_resource_name_that_is_not_a_string_is_dropped(ads_api):
    sent = google_ads([], mutate=httpx.Response(200, json={"result": {"resourceName": {"x": 1}}}))
    assert google_ads_link.send(ads_api, CID, http=sent) is None
    odd = {"results": [{"customerClientLink": {"resourceName": 42, "status": "PENDING", "managerLinkId": "42"}}]}
    assert google_ads_link.read(ads_api, CID, http=google_ads([], search=httpx.Response(200, json=odd))) == Remote(
        status="pending", resource_name=None
    )


def test_record_error_touches_only_the_row_holding_that_customer_id(conn):
    pending_link(conn)
    google_ads_link.record_error(conn, "biz_1", "0987654321", google_ads_link.BUSY)  # a failed invite for a new number
    assert google_ads_link.get(conn, "biz_1").last_error is None
    google_ads_link.record_error(conn, "biz_1", CID, google_ads_link.BUSY)
    assert google_ads_link.get(conn, "biz_1").last_error == google_ads_link.BUSY


def test_held_is_true_only_for_this_businesss_own_row_with_that_number(conn):
    pending_link(conn)
    google_ads_link.save(conn, "biz_1", CID, status="inactive")
    claimed_agreed(conn, "biz_2", email="two@example.com")
    assert google_ads_link.held(conn, "biz_1", CID)
    assert not google_ads_link.held(conn, "biz_1", "0987654321") and not google_ads_link.held(conn, "biz_2", CID)


@pytest.mark.parametrize(
    "replies, text",
    [
        ({"token": httpx.Response(400, json={"error": "invalid_grant"})}, google_ads_link.ACCESS_TROUBLE),
        (
            {"mutate": failure(403, {"authorizationError": "DEVELOPER_TOKEN_NOT_APPROVED"}, "PERMISSION_DENIED")},
            google_ads_link.ACCESS_TROUBLE,
        ),
        ({"mutate": failure(429, {"quotaError": "RESOURCE_EXHAUSTED"}, "RESOURCE_EXHAUSTED")}, google_ads_link.BUSY),
        ({"mutate": _network}, google_ads_link.UNREACHABLE),
        ({"mutate": _undecodable}, google_ads_link.UNREACHABLE),  # any httpx error, not only transport
        ({"token": _undecodable}, google_ads_link.UNREACHABLE),
        ({"token": httpx.Response(503, json={"error": "backendError"})}, google_ads_link.UNREACHABLE),
        ({"token": httpx.Response(200, text="<html>proxy</html>")}, google_ads_link.UNREACHABLE),
        ({"mutate": failure(400, {"requestError": "INVALID_CUSTOMER_ID"})}, google_ads_link.NOT_FOUND),
    ],
)
def test_google_failures_become_owner_sentences_without_secrets(ads_api, replies, text):
    with pytest.raises(LinkError) as e:
        google_ads_link.send(ads_api, CID, http=google_ads([], **replies))
    assert str(e.value) == text and not any(s in str(e.value) for s in SECRETS)
    assert e.value.__cause__ is None  # no chained request or response


def test_manual_mode_never_calls_google():
    settings = config.load()
    assert settings.google_ads_link_mode == "manual"

    def unreachable(request):
        raise AssertionError("manual mode called Google")

    http = httpx.Client(transport=httpx.MockTransport(unreachable))
    assert (
        google_ads_link.send(settings, CID, http=http) is None
        and google_ads_link.read(settings, CID, http=http) is None
    )


def test_check_invite_needs_a_claimed_business_and_the_agreement(conn):
    draft_business(conn)
    with pytest.raises(LinkRefused) as e:
        google_ads_link.check_invite(conn, "biz_1", CID)
    assert e.value.status == 409 and str(e.value) == google_ads_link.NOT_CLAIMED
    claims.claim(conn, "biz_1", "owner@example.com")
    with pytest.raises(LinkRefused) as e:
        google_ads_link.check_invite(conn, "biz_1", "not a number")  # the agreement is asked before the number
    assert e.value.code == "needs_agreement" and e.value.status == 409
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)
    assert google_ads_link.check_invite(conn, "biz_1", "123-456-7890") == CID
    conn.execute("UPDATE business SET ads_mode = 'managed'")
    with pytest.raises(LinkRefused, match="nothing to link"):
        google_ads_link.check_invite(conn, "biz_1", CID)


def test_a_customer_id_linked_to_another_business_is_refused(conn):
    pending_link(conn, "biz_other")  # the same owner's other business (D26)
    claimed_agreed(conn, "biz_1")
    with pytest.raises(LinkRefused) as e:
        google_ads_link.check_invite(conn, "biz_1", "123-456-7890")
    assert e.value.status == 409 and str(e.value) == google_ads_link.TAKEN and "biz_other" not in str(e.value)
    with pytest.raises(LinkRefused) as e:  # a racing insert hits the UNIQUE constraint
        google_ads_link.save(conn, "biz_1", CID, status="pending")
    assert e.value.status == 409 and str(e.value) == google_ads_link.TAKEN
    assert google_ads_link.get(conn, "biz_1") is None


def test_check_invite_refuses_a_live_link_and_a_repeat_but_lets_a_wrong_number_be_replaced(conn):
    pending_link(conn)
    with pytest.raises(LinkRefused, match="already on its way"):
        google_ads_link.check_invite(conn, "biz_1", CID)
    assert google_ads_link.check_invite(conn, "biz_1", "0987654321") == "0987654321"  # a typo can be corrected
    google_ads_link.save(conn, "biz_1", CID, status="manual")
    with pytest.raises(LinkRefused, match="already linked to Google Ads account 123-456-7890"):
        google_ads_link.check_invite(conn, "biz_1", "0987654321")


def test_save_keeps_one_row_per_business(conn):
    pending_link(conn)
    google_ads_link.save(conn, "biz_1", "0987654321", status="pending")
    rows = conn.execute("SELECT customer_id, status, resource_name, checked_at, last_error FROM ads_link").fetchall()
    assert rows == [
        {
            "customer_id": "0987654321",
            "status": "pending",
            "resource_name": None,
            "checked_at": None,
            "last_error": None,
        }
    ]
    link = google_ads_link.save(conn, "biz_1", "0987654321", status="refused")
    assert (
        link.status == "refused" and link.checked_at is not None and link.display == "098-765-4321" and link.resendable
    )


def test_active_verifies_once_and_a_later_unlink_never_downgrades(conn):
    pending_link(conn)
    with conn.transaction():
        first = google_ads_link.apply(conn, "biz_1", CID, Remote("active", RESOURCE))
    assert first.became_active and first.verified_now and first.link.status == "active"
    row = business.row(conn, "biz_1")
    assert (row["claim_state"], row["verified_via"], row["claim_expires_at"]) == ("verified", "ads_link", None)
    assert row["verified_at"] is not None
    with conn.transaction():
        again = google_ads_link.apply(conn, "biz_1", CID, Remote("active", RESOURCE))
    assert not again.became_active and not again.verified_now
    with conn.transaction():  # the owner removed Admiry in Google Ads
        gone = google_ads_link.apply(conn, "biz_1", CID, Remote("inactive", RESOURCE))
    assert gone.link.status == "inactive" and gone.link.resendable and not gone.became_active
    assert business.row(conn, "biz_1")["claim_state"] == "verified"  # A3's adapter refuses a non-active link


def test_refused_keeps_the_claim_and_clears_an_old_error(conn):
    pending_link(conn)
    google_ads_link.record_error(conn, "biz_1", CID, google_ads_link.BUSY)
    assert google_ads_link.get(conn, "biz_1").last_error == google_ads_link.BUSY
    with conn.transaction():
        out = google_ads_link.apply(conn, "biz_1", CID, Remote("refused", RESOURCE))
    assert out.link.status == "refused" and out.link.last_error is None and not out.became_active
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_an_invitation_accepted_after_the_claim_was_released_claims_again_and_verifies(conn):
    pending_link(conn)
    conn.execute("UPDATE business SET claim_state = 'draft', slug = NULL, claim_expires_at = NULL")  # jobs.claim_expire
    with conn.transaction():
        out = google_ads_link.apply(conn, "biz_1", CID, Remote("active", RESOURCE))
    row = business.row(conn, "biz_1")
    assert out.verified_now and out.became_active
    assert (row["claim_state"], row["slug"], row["owner_email"], row["verified_via"]) == (
        "verified",
        "bright-smile-dental",
        "owner@example.com",
        "ads_link",
    )


def test_a_sync_racing_check_now_verifies_once(conn):
    pending_link(conn)
    results = {}

    def check_now():
        with db.connect(TEST_DB_URL) as other, other.transaction():
            results["second"] = google_ads_link.apply(other, "biz_1", CID, Remote("active", RESOURCE))

    with conn.transaction():
        results["first"] = google_ads_link.apply(conn, "biz_1", CID, Remote("active", RESOURCE))
        racer = threading.Thread(target=check_now)
        racer.start()
        racer.join(0.5)
        assert racer.is_alive()  # waiting on the first one's row lock
    racer.join(5)
    assert results["first"].became_active and results["first"].verified_now
    assert not results["second"].became_active and not results["second"].verified_now
    assert business.row(conn, "biz_1")["claim_state"] == "verified"


def test_a_status_for_a_replaced_number_is_ignored(conn):
    pending_link(conn)
    google_ads_link.save(conn, "biz_1", "0987654321", status="pending")  # the owner corrected the number meanwhile
    with conn.transaction():
        out = google_ads_link.apply(conn, "biz_1", CID, Remote("active", RESOURCE))
    assert not out.became_active and out.link.customer_id == "0987654321" and out.link.status == "pending"
    assert business.row(conn, "biz_1")["claim_state"] == "claimed"


def test_an_unknown_google_status_only_records_the_check(conn):
    pending_link(conn)
    with conn.transaction():
        out = google_ads_link.apply(conn, "biz_1", CID, Remote(None, None))
    assert out.link.status == "pending" and out.link.checked_at is not None and out.link.resource_name == RESOURCE


def test_withdraw_cancels_the_pending_invitation_from_the_manager_side(ads_api, conn):
    link = pending_link(conn)
    sent = []
    google_ads_link.withdraw(ads_api, link, http=google_ads(sent, mutate=httpx.Response(200, json={"result": {}})))
    body = json.loads(sent[-1].content)
    assert sent[-1].url.path.endswith(f"/customers/{MCC}/customerClientLinks:mutate")
    assert body == {"operation": {"update": {"resourceName": RESOURCE, "status": "CANCELED"}, "updateMask": "status"}}


def test_withdraw_calls_nothing_without_a_resource_name(ads_api, conn):
    link = pending_link(conn, cid="9876543210")  # pending_link stores a resource name only for CID
    sent = []
    google_ads_link.withdraw(ads_api, link, http=google_ads(sent))
    assert sent == []


def test_withdraw_calls_nothing_in_manual_mode(conn):
    link = pending_link(conn)  # no ads_api fixture: the link mode is manual
    sent = []
    google_ads_link.withdraw(config.load(), link, http=google_ads(sent))
    assert sent == []


def test_withdraw_refused_by_google_is_a_link_error(ads_api, conn):
    link = pending_link(conn)
    with pytest.raises(LinkError):
        google_ads_link.withdraw(ads_api, link, http=google_ads([], mutate=failure(400)))
