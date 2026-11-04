"""Google sign-in for /connect (spec §4.5, §5.4): OpenID Connect authorization-code flow with PKCE, over
httpx and Google's documented endpoints — no SDK, no JWT parsing. The identity comes from the userinfo
endpoint, called with the access token the token endpoint just returned over TLS, so the e-mail is
Google's word and `email_verified` is required. Scopes are `openid email` only (no profile, no Ads/GBP
for owners; `offline_token` serves only the operator's one-time Google Ads sign-in). Failures are
SignInError with an owner-readable reason; the code, tokens and client secret never appear in one."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPES = "openid email"
TIMEOUT_S = 15.0


class SignInError(Exception):
    """An owner-readable reason the sign-in did not complete."""


@dataclass(frozen=True)
class Identity:
    email: str  # lower-cased, verified by Google
    sub: str  # Google's stable account id


def pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(48)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(
    *, client_id: str, redirect_uri: str, state: str, challenge: str, scope: str = SCOPES, offline: bool = False
) -> str:
    """Google's consent URL. The owner sign-in uses the defaults (`openid email`, online, account chooser);
    `offline=True` asks for a refresh token (access_type=offline, prompt=consent) — only agent.ops google-ads-auth,
    for Admiry's own Google Ads login."""
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent" if offline else "select_account",
        "access_type": "offline" if offline else "online",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=5.0))


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def sign_in(
    client: httpx.Client, *, code: str, verifier: str, redirect_uri: str, client_id: str, client_secret: str
) -> Identity:
    """Exchange the authorization code, then read who signed in. Two calls, both to Google."""
    form = {
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
    }
    try:
        token = client.post(TOKEN_URL, data=form)
    except httpx.TransportError as e:
        raise SignInError("Google is unreachable right now; try again in a minute") from e
    access = _json(token).get("access_token") if token.is_success else None
    if not access:
        raise SignInError("Google did not accept the sign-in; start again from the link")
    try:
        info = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
    except httpx.TransportError as e:
        raise SignInError("Google is unreachable right now; try again in a minute") from e
    data = _json(info)
    if not info.is_success or not data:
        raise SignInError("Google is unavailable right now; try again in a minute")
    email, sub = data.get("email"), data.get("sub")
    if not email or not sub or data.get("email_verified") is not True:
        raise SignInError("Admiry needs a Google account with a verified e-mail address")
    return Identity(email=str(email).strip().lower(), sub=str(sub))


def offline_token(
    client: httpx.Client, *, code: str, verifier: str, redirect_uri: str, client_id: str, client_secret: str
) -> str:
    """Exchange an authorization code from an offline consent for its refresh token (agent.ops google-ads-auth)."""
    form = {
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
    }
    try:
        response = client.post(TOKEN_URL, data=form)
    except httpx.TransportError as e:
        raise SignInError("Google is unreachable right now; try again in a minute") from e
    if not response.is_success:
        raise SignInError("Google did not accept the code; run the command again")
    token = _json(response).get("refresh_token")
    if not token:
        raise SignInError(
            "Google returned no lasting access; remove the app's access at "
            "https://myaccount.google.com/permissions and run the command again"
        )
    return str(token)
