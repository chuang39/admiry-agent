"""Claim links and the claim (spec §4.0 "Business claim state", §5.4 web hand-off 1).

`start` returns connect_url = {base}/connect?claim=<token>: a 24-hour capability that, after Google
sign-in, binds the draft business to the signer's verified e-mail (claim_state draft -> claimed), reserves
the slug and upgrades the draft key in place so the assistant's session keeps working without a new key.
A claim never verifies: approve_plan and every spend still wait for claim_state = verified (agent.ops
verify in this part; GBP OAuth / phone OTP later). claims.verify is the one claimed → verified transition
(a linked Google Ads account, A1b-2; a prepaid business's first payment, A1b-3). Only the token's sha256
is stored."""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import datetime, timedelta

import psycopg

from . import config, keys, support

LINK_TTL = timedelta(hours=24)
CLAIM_TTL = timedelta(days=14)  # spec §4.0: unverified claims expire after 14 days (jobs.claim_expire)
SLUG_MAX = 40
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


OTHER_ACCOUNT = "this business was claimed by another Google account; sign in with that account"


class ClaimRefused(Exception):
    """An owner-readable reason the claim did not happen."""


def slugify(text: str) -> str:
    """ASCII, lower-case, hyphen-separated, at most SLUG_MAX characters, never empty ("business")."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = _NOT_SLUG.sub("-", ascii_text.lower()).strip("-")[:SLUG_MAX].rstrip("-")
    return slug or "business"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_link(conn: psycopg.Connection, business_id: str) -> tuple[str, datetime]:
    """A fresh claim link for a business: (url, expires_at). Earlier links stay valid until used or expired."""
    token = secrets.token_urlsafe(24)
    row = conn.execute(
        "INSERT INTO claim_link (token_hash, business_id, expires_at) VALUES (%s, %s, now() + %s) RETURNING expires_at",
        (_hash(token), business_id, LINK_TTL),
    ).fetchone()
    return f"{config.load().web_base_url}/connect?claim={token}", row["expires_at"]


def take(conn: psycopg.Connection, token: str) -> dict | None:
    """The live, unused link for a token ({"business_id", "token_hash"}) or None. Opening does not consume."""
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT business_id, token_hash FROM claim_link WHERE token_hash = %s AND used_at IS NULL "
        "AND expires_at > now()",
        (_hash(token),),
    ).fetchone()
    return dict(row) if row else None


def _free_slug(conn: psycopg.Connection, base: str) -> str:
    slug, n = base, 1
    while conn.execute("SELECT 1 FROM business WHERE slug = %s", (slug,)).fetchone():
        n += 1
        slug = f"{base[: SLUG_MAX - len(str(n)) - 1]}-{n}"
    return slug


def claim(conn: psycopg.Connection, business_id: str, email: str, *, token_hash: str | None = None) -> dict:
    """Bind the business to the signer. A draft becomes claimed (owner e-mail, slug, claim deadline, draft key
    upgraded in place); the owner's repeat is a no-op; another account is refused. Runs inside the caller's
    transaction; marks the link used when one was given."""
    email = email.strip().lower()
    row = conn.execute(
        "SELECT id, owner_email, claim_state FROM business WHERE id = %s FOR UPDATE", (business_id,)
    ).fetchone()
    if row is None:
        raise ClaimRefused("this business no longer exists; ask your assistant to start again")
    if row["owner_email"] and row["owner_email"] != email:
        raise ClaimRefused(OTHER_ACCOUNT)
    out = {
        "business_id": business_id,
        "claim_state": row["claim_state"],
        "slug": None,
        "claimed_now": False,
        "key_upgraded": None,
    }
    if row["claim_state"] == "draft":
        latest = conn.execute(
            "SELECT json FROM knowledge WHERE business_id = %s ORDER BY version DESC LIMIT 1", (business_id,)
        ).fetchone()
        slug = _free_slug(conn, slugify((latest["json"].get("name") if latest else "") or ""))
        conn.execute(
            "UPDATE business SET owner_email = %s, claim_state = 'claimed', slug = %s, "
            "claim_expires_at = now() + %s, updated_at = now() WHERE id = %s",
            (email, slug, CLAIM_TTL, business_id),
        )
        key_id = keys.upgrade_draft(conn, business_id)  # the setup key becomes lasting (same upgrade ops.verify does)
        out.update(claim_state="claimed", slug=slug, claimed_now=True, key_upgraded=key_id)
    else:
        out["slug"] = conn.execute("SELECT slug FROM business WHERE id = %s", (business_id,)).fetchone()["slug"]
        if not row["owner_email"]:  # claimed/verified by an operator before any sign-in: record the owner now
            conn.execute("UPDATE business SET owner_email = %s, updated_at = now() WHERE id = %s", (email, business_id))
    if token_hash:
        conn.execute("UPDATE claim_link SET used_at = now() WHERE token_hash = %s AND used_at IS NULL", (token_hash,))
    support.on_claim(conn, business_id, email)  # a ticket waiting for this owner's address (spec 2026-09-30 D133)
    return out


def verify(conn: psycopg.Connection, business_id: str, *, via: str, ads_mode: str | None = None) -> bool:
    """claimed → verified (spec 2026-09-25 rev. 2 D29), the one transition both ways use: a Google Ads link turning
    ACTIVE (via 'ads_link', ads_mode 'own_account') and a prepaid business's first paid top-up (via 'payment',
    ads_mode 'managed'). The claim deadline is cleared and a live setup key made lasting (as ops.verify does). A
    business released to draft by claim-expire while its owner was finishing is claimed again for that owner first
    (fresh slug). A verified business is untouched (never downgraded or re-stamped); one without an owner, retired,
    or in another ads_mode than `ads_mode` (when given) is not verified. True when this call verified it. Runs
    inside the caller's transaction."""
    row = conn.execute(
        "SELECT owner_email, claim_state, status, ads_mode FROM business WHERE id = %s FOR UPDATE", (business_id,)
    ).fetchone()
    if (
        row is None
        or row["status"] != "active"
        or row["claim_state"] == "verified"
        or not row["owner_email"]
        or (ads_mode is not None and row["ads_mode"] != ads_mode)
    ):
        return False
    if row["claim_state"] == "draft":
        claim(conn, business_id, row["owner_email"])
    conn.execute(
        "UPDATE business SET claim_state = 'verified', verified_via = %s, verified_at = now(), "
        "claim_expires_at = NULL, updated_at = now() WHERE id = %s",
        (via, business_id),
    )
    keys.upgrade_draft(conn, business_id)
    return True
