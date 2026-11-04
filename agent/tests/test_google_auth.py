"""The Google round trip over a mock transport: what is sent, what is accepted back (no network)."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from agent import google_auth

ARGS = {
    "code": "4/abc",
    "verifier": "v" * 43,
    "redirect_uri": "http://testserver/connect/google/callback",
    "client_id": "123.apps.googleusercontent.com",
    "client_secret": "GOCSPX-x",
}


def transport(token_reply, userinfo_reply, sent: list):
    def handler(request):
        sent.append(request)
        if request.url == httpx.URL(google_auth.TOKEN_URL):
            return token_reply(request) if callable(token_reply) else token_reply
        if request.url == httpx.URL(google_auth.USERINFO_URL):
            return userinfo_reply
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def ok_token():
    return httpx.Response(
        200,
        json={
            "access_token": "ya29.x",
            "expires_in": 3599,
            "scope": "openid email",
            "token_type": "Bearer",
            "id_token": "eyJ.ignored",
        },
    )


def userinfo(email="Owner@Example.com", verified=True, sub="1057"):
    return httpx.Response(200, json={"sub": sub, "email": email, "email_verified": verified})


def test_pkce_pairs_a_verifier_with_its_s256_challenge():
    verifier, challenge = google_auth.pkce()
    assert 43 <= len(verifier) <= 128 and verifier != google_auth.pkce()[0]
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_authorize_url_asks_google_for_a_verified_email_with_pkce_and_state():
    url = google_auth.authorize_url(
        client_id="123.apps.googleusercontent.com",
        redirect_uri="https://agent.admiry.ai/connect/google/callback",
        state="st_1",
        challenge="ch_1",
    )
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == google_auth.AUTH_URL
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q == {
        "client_id": "123.apps.googleusercontent.com",
        "response_type": "code",
        "redirect_uri": "https://agent.admiry.ai/connect/google/callback",
        "scope": "openid email",
        "state": "st_1",
        "code_challenge": "ch_1",
        "code_challenge_method": "S256",
        "prompt": "select_account",
        "access_type": "online",
    }


def test_sign_in_exchanges_the_code_then_reads_the_verified_email():
    sent: list = []
    client = httpx.Client(transport=transport(ok_token(), userinfo(), sent))
    who = google_auth.sign_in(client, **ARGS)
    assert who == google_auth.Identity(email="owner@example.com", sub="1057")  # lower-cased
    token_req, info_req = sent
    assert token_req.method == "POST" and token_req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(token_req.content.decode()).items()}
    assert form == {
        "code": "4/abc",
        "code_verifier": "v" * 43,
        "redirect_uri": ARGS["redirect_uri"],
        "client_id": ARGS["client_id"],
        "client_secret": "GOCSPX-x",
        "grant_type": "authorization_code",
    }
    assert info_req.method == "GET" and info_req.headers["authorization"] == "Bearer ya29.x"


@pytest.mark.parametrize(
    "token_reply, info_reply, message",
    [
        (
            httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"}),
            userinfo(),
            "Google did not accept",
        ),
        (httpx.Response(200, json={"token_type": "Bearer"}), userinfo(), "Google did not accept"),  # no access token
        (ok_token(), userinfo(verified=False), "verified e-mail"),
        (ok_token(), httpx.Response(200, json={"sub": "1"}), "verified e-mail"),  # no email
        (ok_token(), httpx.Response(500, text="boom"), "Google is unavailable"),
        (ok_token(), httpx.Response(200, text="<html>"), "Google is unavailable"),
    ],
)
def test_failures_are_owner_readable(token_reply, info_reply, message):
    client = httpx.Client(transport=transport(token_reply, info_reply, []))
    with pytest.raises(google_auth.SignInError, match=message):
        google_auth.sign_in(client, **ARGS)


def test_connection_errors_are_owner_readable():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(google_auth.SignInError, match="Google is unreachable"):
        google_auth.sign_in(client, **ARGS)


def test_the_real_client_has_a_short_timeout():
    c = google_auth.client()
    assert c.timeout.read == google_auth.TIMEOUT_S and c.timeout.connect == 5.0
