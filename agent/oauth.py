"""MCP OAuth 2.1 for third-party assistants (spec 2026-09-26 §6, D35): FastMCP's authorization server with
Postgres storage. An access token is an api_key row (oa_…), so every tool sees an OAuth caller exactly like
an ak_ key holder; refresh tokens rotate and a reused one revokes its grant."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg
from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Jsonb

from . import config, db, keys

SCOPES = ("read", "manage", "leads", "account")
DEFAULT_SCOPES = ("read", "manage", "leads")
SCOPE_WORDS = {
    "read": "Read your plans and reports",  # it also asks Admiry to research the market (D14: no money, nothing public)
    "manage": "Make changes within your monthly budget",
    "leads": "See your leads and record what became of them",  # it also records outcomes and adds leads (final A I2)
    "account": "Manage keys and settings",
}
CODE_TTL, ACCESS_TTL, REFRESH_TTL, TXN_TTL = (
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(days=30),
    timedelta(minutes=10),
)
HITS_PER_MINUTE = 20
CONSENT_PATH = "/oauth/consent"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


class _Abort(Exception):
    """Signals a TokenError from inside `with conn.transaction():`. TokenError is a frozen dataclass, and
    psycopg's transaction context manager is generator-based: on the way out it does `exc.__traceback__ =
    traceback`, which a frozen dataclass's __setattr__ refuses. Raise this plain Exception instead and
    translate it to TokenError once execution is back outside the transaction block."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def redirect_uri_ok(uri: str) -> bool:
    """Open DCR accepts only https callbacks, or plain http to a loopback host (a desktop client's local
    listener): never javascript:, data: or an http page on someone else's host (final review I2)."""
    u = urlsplit(uri)
    if u.scheme == "https":
        return bool(u.hostname)
    return u.scheme == "http" and u.hostname in LOOPBACK_HOSTS


def redirect_host(uri: str) -> str:
    """The host the consent page names as where the owner is sent back to."""
    return urlsplit(uri).hostname or uri


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


def new_txn_id() -> str:
    return "txn_" + secrets.token_hex(8)


def take_txn(conn: psycopg.Connection, txn_id: str) -> dict | None:
    r = conn.execute(
        "UPDATE oauth_txn SET used_at = now() WHERE id = %s AND used_at IS NULL AND expires_at > now() "
        "RETURNING id, client_id, params",
        (txn_id,),
    ).fetchone()
    return dict(r) if r else None


def peek_txn(conn: psycopg.Connection, txn_id: str) -> dict | None:
    """The transaction for rendering the consent page (not consumed)."""
    r = conn.execute(
        "SELECT id, client_id, params, expires_at FROM oauth_txn WHERE id = %s AND used_at IS NULL AND expires_at > now()",
        (txn_id,),
    ).fetchone()
    return dict(r) if r else None


def peek_code_resource(conn: psycopg.Connection, code: str) -> str | None:
    """The RFC 8707 `resource` an unspent, unexpired code was minted for (A1b-4b Task 2's /token guard: the
    SDK's TokenHandler never passes the request's own `resource` field to exchange_authorization_code, so
    the mismatch has to be caught before it, from this same value). A peek: it does not consume the code."""
    r = conn.execute(
        "SELECT resource FROM oauth_code WHERE code_hash = %s AND used_at IS NULL AND expires_at > now()",
        (_hash(code),),
    ).fetchone()
    return r["resource"] if r else None


def client_name(conn: psycopg.Connection, client_id: str) -> str:
    r = conn.execute("SELECT client_name FROM oauth_client WHERE id = %s", (client_id,)).fetchone()
    return (r["client_name"] if r else None) or "your assistant"


def mint_code(
    conn: psycopg.Connection, *, txn: dict, business_id: str, owner_email: str, scopes: list[str]
) -> tuple[str, str]:
    """Create the grant and the one-use code for a consented transaction; returns (code, redirect URL)."""
    p = txn["params"]
    grant_id = db.new_id("gr")
    conn.execute(
        "INSERT INTO oauth_grant (id, client_id, business_id, owner_email, scopes) VALUES (%s, %s, %s, %s, %s)",
        (grant_id, txn["client_id"], business_id, owner_email, list(scopes)),
    )
    code = "oc_" + secrets.token_hex(24)
    conn.execute(
        "INSERT INTO oauth_code (code_hash, grant_id, client_id, scopes, code_challenge, redirect_uri, "
        "redirect_uri_provided_explicitly, resource, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + %s)",
        (
            _hash(code),
            grant_id,
            txn["client_id"],
            list(scopes),
            p["code_challenge"],
            p.get("redirect_uri"),
            bool(p.get("redirect_uri_provided_explicitly", True)),
            p.get("resource"),
            CODE_TTL,
        ),
    )
    return code, construct_redirect_uri(str(p["redirect_uri"]), code=code, state=p.get("state"))


def revoke_grant(conn: psycopg.Connection, grant_id: str) -> int:
    """Revoke the grant, its refresh tokens and its access key. Returns 1 if the grant is revoked once
    this returns — by this call or an earlier one — 0 only when grant_id names no grant at all, so a
    caller's audit can record success on a repeat revoke (the api_key row this call touches, if any, may
    already have been revoked by the caller a moment earlier, e.g. web.revoke_key on a Keys-table row)."""
    just_revoked = conn.execute(
        "UPDATE oauth_grant SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id", (grant_id,)
    ).fetchone()
    conn.execute("UPDATE oauth_token SET revoked_at = now() WHERE grant_id = %s AND revoked_at IS NULL", (grant_id,))
    conn.execute("UPDATE api_key SET revoked_at = now() WHERE grant_id = %s AND revoked_at IS NULL", (grant_id,))
    if just_revoked is not None:
        return 1
    return 1 if conn.execute("SELECT 1 FROM oauth_grant WHERE id = %s", (grant_id,)).fetchone() else 0


def grants_for_business(conn: psycopg.Connection, business_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT g.id, c.client_name, g.scopes, g.created_at, g.last_used_at, g.revoked_at FROM oauth_grant g "
        "JOIN oauth_client c ON c.id = g.client_id WHERE g.business_id = %s ORDER BY g.created_at DESC",
        (business_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def hit(conn: psycopg.Connection, ip: str, path: str) -> bool:
    """P8: True when this request is within 20 per minute for the address; prunes old rows as it goes."""
    conn.execute("DELETE FROM oauth_hit WHERE ts < now() - interval '2 minutes'")
    n = conn.execute(
        "SELECT count(*) AS n FROM oauth_hit WHERE ip = %s AND ts > now() - interval '1 minute'", (ip,)
    ).fetchone()["n"]
    if n >= HITS_PER_MINUTE:
        return False
    conn.execute("INSERT INTO oauth_hit (ip) VALUES (%s)", (ip,))
    return True


class AdmiryOAuthProvider(OAuthProvider):
    def __init__(self, *, base_url: str, database_url: str | None = None) -> None:
        super().__init__(
            base_url=base_url,
            service_documentation_url=f"{base_url}/connect/docs",
            # default_scopes is what an unspecified DCR request is registered with, capping every later
            # /authorize call from that client (OAuthClientInformationFull.validate_scope); it is SCOPES, not
            # DEFAULT_SCOPES, so a client that never names a scope at registration can still ask for leads/account
            # at /authorize — the owner is the real gate, ticking or unticking them on the consent page (Task 3).
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=list(SCOPES), default_scopes=list(SCOPES)
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self._database_url = database_url

    def _conn(self):
        return db.connect(self._database_url or config.load().database_url)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in client_info.redirect_uris or [] if not redirect_uri_ok(str(u))]
        if bad:  # raised before any connection is open (RegistrationError is a frozen dataclass, see _Abort)
            raise RegistrationError(
                "invalid_redirect_uri", "redirect URIs must use https (or http to localhost / 127.0.0.1)"
            )
        name = client_info.client_name or "your assistant"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO oauth_client (id, client_name, redirect_uris, info) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET client_name = EXCLUDED.client_name, redirect_uris = EXCLUDED.redirect_uris, "
                "info = EXCLUDED.info",
                (
                    client_info.client_id,
                    name,
                    Jsonb([str(u) for u in client_info.redirect_uris or []]),
                    Jsonb(client_info.model_dump(mode="json")),
                ),
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT info FROM oauth_client WHERE id = %s AND disabled_at IS NULL", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate(r["info"]) if r else None

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        txn_id = new_txn_id()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO oauth_txn (id, client_id, params, expires_at) VALUES (%s, %s, %s, now() + %s)",
                (txn_id, client.client_id, Jsonb(params.model_dump(mode="json")), TXN_TTL),
            )
        # the consent page is a browser page: on the web host, where the owner's session cookie lives (spec 2026-09-29 §3)
        return f"{config.load().web_base_url}{CONSENT_PATH}?txn={txn_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT c.*, g.id AS grant_id FROM oauth_code c JOIN oauth_grant g ON g.id = c.grant_id WHERE "
                "c.code_hash = %s AND c.client_id = %s AND c.used_at IS NULL AND c.expires_at > now() "
                "AND g.revoked_at IS NULL",
                (_hash(authorization_code), client.client_id),
            ).fetchone()
        if not r:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(r["scopes"]),
            expires_at=float(_epoch(r["expires_at"])),
            client_id=r["client_id"],
            code_challenge=r["code_challenge"],
            redirect_uri=r["redirect_uri"],
            redirect_uri_provided_explicitly=r["redirect_uri_provided_explicitly"],
            resource=r["resource"],
            subject=r["grant_id"],
        )

    def _issue(
        self, conn: psycopg.Connection, grant: dict, client_name: str, *, rotated_from: str | None
    ) -> OAuthToken:
        access, rec = keys.mint(
            conn,
            business_id=grant["business_id"],
            cls="business",
            scopes=tuple(grant["scopes"]),
            label=client_name,
            source="oauth",
            grant_id=grant["id"],
            prefix="oa",
            ttl=ACCESS_TTL,
        )
        refresh = "or_" + secrets.token_hex(24)
        conn.execute(
            "INSERT INTO oauth_token (id, grant_id, api_key_id, refresh_hash, refresh_expires_at, rotated_from) "
            "VALUES (%s, %s, %s, %s, now() + %s, %s)",
            (db.new_id("ot"), grant["id"], rec.id, _hash(refresh), REFRESH_TTL, rotated_from),
        )
        conn.execute("UPDATE oauth_grant SET last_used_at = now() WHERE id = %s", (grant["id"],))
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=int(ACCESS_TTL.total_seconds()),
            refresh_token=refresh,
            scope=" ".join(grant["scopes"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._conn() as conn:
            try:
                with conn.transaction():
                    spent = conn.execute(
                        "UPDATE oauth_code SET used_at = now() WHERE code_hash = %s AND used_at IS NULL "
                        "AND expires_at > now() RETURNING grant_id",
                        (_hash(authorization_code.code),),
                    ).fetchone()
                    if not spent:
                        raise _Abort("invalid_grant", "authorization code already used or expired")
                    grant = dict(
                        conn.execute(
                            "SELECT id, business_id, scopes, client_id FROM oauth_grant WHERE id = %s "
                            "AND revoked_at IS NULL",
                            (spent["grant_id"],),
                        ).fetchone()
                        or {}
                    )
                    if not grant or grant["client_id"] != client.client_id:
                        raise _Abort("invalid_grant", "this code belongs to another client")
                    return self._issue(conn, grant, client.client_name or "your assistant", rotated_from=None)
            except _Abort as e:
                raise TokenError(e.error, e.description) from None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT t.id, t.used_at, t.revoked_at, t.refresh_expires_at, g.id AS grant_id, g.client_id, "
                "g.scopes, g.revoked_at AS grant_revoked FROM oauth_token t JOIN oauth_grant g ON g.id = t.grant_id "
                "WHERE t.refresh_hash = %s",
                (_hash(refresh_token),),
            ).fetchone()
            if not r or r["client_id"] != client.client_id or r["grant_revoked"] or r["revoked_at"]:
                return None
            if r["used_at"] is not None:  # a spent refresh token presented again: the family is compromised
                revoke_grant(conn, r["grant_id"])
                return None
            if r["refresh_expires_at"] < datetime.now(timezone.utc):
                return None
        return RefreshToken(
            token=refresh_token,
            client_id=r["client_id"],
            scopes=list(r["scopes"]),
            expires_at=_epoch(r["refresh_expires_at"]),
            subject=r["grant_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "requested scopes exceed the grant")
        with self._conn() as conn:
            # This single UPDATE is its own atomic unit (autocommit) and must NOT sit inside the
            # conn.transaction() below: on the "already used" branch we need the revoke_grant it triggers
            # to survive, and raising TokenError (a frozen dataclass) from inside conn.transaction()'s
            # generator-based __exit__ crashes with FrozenInstanceError (see _Abort's docstring) -- so
            # nothing here may raise while a transaction that must not be rolled back is still open.
            old = conn.execute(
                "UPDATE oauth_token SET used_at = now() WHERE refresh_hash = %s AND used_at IS NULL "
                "RETURNING id, api_key_id, grant_id",
                (_hash(refresh_token.token),),
            ).fetchone()
            if not old:
                # A spent refresh token reached exchange: either presented twice, or a race that read
                # load_refresh_token's used_at IS NULL before a concurrent winner committed. Either way
                # the token was disclosed once already -- treat it exactly as load_refresh_token treats
                # reuse: the whole family is compromised.
                existing = conn.execute(
                    "SELECT grant_id FROM oauth_token WHERE refresh_hash = %s", (_hash(refresh_token.token),)
                ).fetchone()
                if existing:
                    revoke_grant(conn, existing["grant_id"])
                raise TokenError("invalid_grant", "refresh token already used")
            with conn.transaction():
                grant = dict(
                    conn.execute(
                        "SELECT id, business_id, scopes, client_id FROM oauth_grant WHERE id = %s",
                        (old["grant_id"],),
                    ).fetchone()
                )
                keys.revoke(conn, old["api_key_id"], grant["business_id"])
                grant["scopes"] = list(scopes) or list(grant["scopes"])
                return self._issue(conn, grant, client.client_name or "your assistant", rotated_from=old["id"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._conn() as conn:
            rec = keys.lookup(conn, token)
            if rec is None or rec.revoked_at is not None or rec.is_expired():
                return None
            if rec.business_id is not None:
                b = conn.execute("SELECT status FROM business WHERE id = %s", (rec.business_id,)).fetchone()
                if not b or b["status"] != "active":
                    return None
            client_id = "key"
            if rec.grant_id:
                g = conn.execute(
                    "SELECT client_id, revoked_at FROM oauth_grant WHERE id = %s", (rec.grant_id,)
                ).fetchone()
                if not g or g["revoked_at"]:
                    return None
                client_id = g["client_id"]
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=list(rec.scopes),
            expires_at=_epoch(rec.expires_at),
            subject=rec.business_id,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._conn() as conn:
            if isinstance(token, AccessToken):
                rec = keys.lookup(conn, token.token)
                if rec and rec.grant_id:
                    revoke_grant(conn, rec.grant_id)
                elif rec:
                    keys.revoke(conn, rec.id, rec.business_id)
            else:
                r = conn.execute(
                    "SELECT grant_id FROM oauth_token WHERE refresh_hash = %s", (_hash(token.token),)
                ).fetchone()
                if r:
                    revoke_grant(conn, r["grant_id"])
