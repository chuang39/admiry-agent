"""Linking the owner's own Google Ads account to Admiry's manager account (spec 2026-09-25 rev. 2 D25, D26, D29, §5).

In code (never on a page): Admiry's MCC sends a customer-client-link invitation (status PENDING) to the owner's
Customer ID; the owner accepts it inside Google Ads; ACTIVE verifies the business (verified_via 'ads_link'). One
ads_link row per business; a Customer ID links to at most one business (UNIQUE, D26).

Two modes (config.google_ads_link_mode): `api` — Google Ads REST over httpx (below); `manual` — no Google call at
all: the owner's request is recorded as pending, an operator sends the invitation from the MCC UI and runs
`agent.ops ads-link … --status active` (stored as 'manual', treated as active).

Google is called between transactions (send/read take no connection): the page routes close theirs first; the tool
(run()'s prepare) and the job call with their connection idle. Failures are LinkError carrying a fixed owner sentence
from this module — never Google's text, a token, the developer token or the account's name — stored in
ads_link.last_error.

Google Ads API facts (checked 2026-09-25; re-check when bumping API_VERSION):
  https://developers.google.com/google-ads/api/docs/release-notes (v25 current)
  https://developers.google.com/google-ads/api/docs/account-management/linking-manager-accounts
  https://developers.google.com/google-ads/api/rest/reference/rest (lowerCamelCase JSON)
  https://developers.google.com/google-ads/api/fields/v25/customer_client_link (PENDING, ACTIVE, REFUSED, CANCELED,
  INACTIVE; every invitation has its own manager_link_id)
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import httpx
import psycopg

from . import agreements, claims, config, google_managed
from .channels import registry
from .channels.google_search import client as google_ads
from .util import iso

# The REST session/token/call plumbing (API_VERSION, BASE_URL, client, _session, _json, _access_token, _call) moved
# to google_ads.py (A3 Task 1; channels/google_search/client.py since M0) so every module that talks to Google Ads
# shares one client; these thin re-exports keep this module's own tests, and jobs.py's `google_ads_link.client()`,
# working untouched. New code imports the client directly.
client = google_ads.client
_json = google_ads._json
_access_token = google_ads._access_token
_call = google_ads._call


@contextmanager
def _session(http: httpx.Client | None) -> Iterator[httpx.Client]:
    """Not a re-export like its siblings above: it must call *this module's* `client` by name (a few tests
    monkeypatch google_ads_link.client to stub the whole session; google_ads._session would resolve `client`
    against google_ads's own globals instead and reach the real network)."""
    if http is not None:
        yield http
        return
    with client() as own:
        yield own


LIVE = ("active", "manual")
RESENDABLE = ("refused", "cancelled", "inactive")
STATUS_FROM_GOOGLE = {
    "PENDING": "pending",
    "ACTIVE": "active",
    "REFUSED": "refused",
    "CANCELED": "cancelled",
    "INACTIVE": "inactive",
}  # UNKNOWN / UNSPECIFIED → None: only the check is recorded
ALREADY_INVITED = "ALREADY_INVITED_BY_THIS_MANAGER"  # the invitation is on its way: counts as sent
ALREADY_MANAGED = "ALREADY_MANAGED_BY_THIS_MANAGER"  # already a client: sent only if this business's row holds it
NOT_FOUND_CODES = frozenset({"CUSTOMER_NOT_FOUND", "INVALID_CUSTOMER_ID"})
QUERY = (
    "SELECT customer_client_link.client_customer, customer_client_link.status, "
    "customer_client_link.resource_name, customer_client_link.manager_link_id FROM customer_client_link "
    "WHERE customer_client_link.client_customer = 'customers/{customer_id}'"
)  # the id is 10 ASCII digits
COLUMNS = "business_id, customer_id, status, resource_name, invited_at, checked_at, last_error"
_SEPARATORS = re.compile(r"[\s-]+")
_TEN_DIGITS = re.compile(r"[0-9]{10}")
_MAX_INPUT = 32

# owner sentences (pages, tool errors, last_error)
NOT_CLAIMED = "claim the business first: open the sign-in link from your assistant, then link Google Ads"
NOT_OWN_ACCOUNT = "this business's ads run in one of Admiry's Google Ads accounts; there is nothing to link"
NEEDS_AGREEMENT = "accept the Google Ads Management Agreement first"
BAD_ID = "a Google Ads Customer ID is 10 digits, like 123-456-7890 (at the top of your Google Ads page)"
TAKEN = (
    "that Google Ads account is already linked to another business on Admiry; "
    "each business needs its own Google Ads account"
)
ALREADY_LINKED = "that Google Ads account is already linked to Admiry; contact Admiry and we will sort it out"
NO_INVITATION = "Google Ads shows no invitation for this account; send it again"
UNREACHABLE = "Google Ads is unreachable right now; try again in a few minutes"
BUSY = "Google Ads is busy right now; try again in a few minutes"
ACCESS_TROUBLE = "Admiry's access to Google Ads needs attention; an Admiry operator will fix it, try again later"
NOT_FOUND = "Google Ads has no account with that Customer ID; check the number at the top of your Google Ads page"
REFUSED = "Google Ads did not accept the invitation; check the Customer ID, or try again later"
ACCEPT = (
    "in your Google Ads account open Admin → Access and security → the Managers tab, and accept the "
    "invitation from Admiry's manager account"
)  # the one place owners read "manager account"


class LinkRefused(Exception):
    """An owner-readable reason this link request is not accepted; `status` is the HTTP code a page answers with,
    `code` is "needs_agreement" when only the agreement is missing."""

    def __init__(self, message: str, *, status: int = 409, code: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code = status, code


class LinkError(Exception):
    """Google did not do what was asked; the message is one of this module's owner sentences."""


class _Already(LinkError):
    """The invitation already exists: the send counts as done."""


class _Managed(LinkError):
    """The account is already a client of Admiry's MCC: done only when this business's own row holds it."""


@dataclass(frozen=True)
class Remote:
    status: str | None  # pending | active | refused | cancelled | inactive; None = Google said UNKNOWN
    resource_name: str | None
    note: str | None = None  # stored as last_error (NO_INVITATION)


@dataclass(frozen=True)
class Link:
    business_id: str
    customer_id: str
    status: str
    resource_name: str | None
    invited_at: datetime
    checked_at: datetime | None
    last_error: str | None

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def resendable(self) -> bool:
        return self.status in RESENDABLE

    @property
    def display(self) -> str:
        return format_customer_id(self.customer_id)

    @property
    def owner_status(self) -> str:
        """The status owners and assistants read: an operator's 'manual' is simply active."""
        return "active" if self.status == "manual" else self.status

    def public(self, mode: str) -> dict:
        return {
            "customer_id": self.display,
            "status": self.owner_status,
            "invited_at": iso(self.invited_at),
            "checked_at": iso(self.checked_at),
            "note": self.last_error,
            "instructions": instructions(mode) if self.status == "pending" else None,
        }


@dataclass(frozen=True)
class Applied:
    link: Link | None
    became_active: bool  # this call moved the row from not-live to active (audit ads_link_active once)
    verified_now: bool  # this call verified the business


def instructions(mode: str) -> str:
    """What the owner does next while the invitation is pending."""
    if mode == "api":
        return f"Invitation sent. To finish, {ACCEPT}. Then press Check now (Admiry also checks every hour)."
    return (
        f"Received. An Admiry operator sends the invitation within one business day; then {ACCEPT}. "
        "Admiry marks the business verified once it is accepted."
    )


def format_customer_id(customer_id: str) -> str:
    return f"{customer_id[:3]}-{customer_id[3:6]}-{customer_id[6:]}"


def normalize_customer_id(text: str | None) -> str:
    """Ten ASCII digits from what the owner typed (dashes and spaces allowed); LinkRefused (400) otherwise."""
    if not isinstance(text, str) or len(text) > _MAX_INPUT:
        raise LinkRefused(BAD_ID, status=400)
    digits = _SEPARATORS.sub("", text)
    if not _TEN_DIGITS.fullmatch(digits):
        raise LinkRefused(BAD_ID, status=400)
    return digits


def get(conn: psycopg.Connection, business_id: str) -> Link | None:
    r = conn.execute(f"SELECT {COLUMNS} FROM ads_link WHERE business_id = %s", (business_id,)).fetchone()
    return Link(**r) if r else None


# ads_link.status -> the status its channel_account(google_search) mirror carries (the M0 migration's own mapping)
MIRRORED = {
    "active": "active",
    "manual": "active",
    "pending": "pending",
    "refused": "refused",
    "cancelled": "inactive",
    "inactive": "inactive",
}


def mirror(conn: psycopg.Connection, business_id: str) -> None:
    """Google Search's `channel_account` row brought up to date with its sources of truth (spec 2026-09-29 §8; plan
    M0 P6): the mode from `business.ads_mode`; the status, the Customer ID, when Admiry last checked the link and
    the note the last check left on it (`last_error`) from the link (no link: `none`, nothing checked) -- or, for a
    prepaid business, from the account Admiry runs it in (A3c-1, `google_managed`: active once one is bound). Every path
    that creates, checks or changes the link, or the mode, calls it (`save`, `apply`, `record_error`, a withdrawn
    invitation, `ads_mode.change`), so the framework's readers (Settings, get_connection_status,
    `managed_channels`) see what the link says; the Google Search adapter still reads the link itself."""
    row = conn.execute("SELECT ads_mode FROM business WHERE id = %s", (business_id,)).fetchone()
    if row is None:
        return
    if row["ads_mode"] == "managed":  # A3c-1: the account Admiry runs it in, active once bound; the link is not used
        bound = google_managed.get(conn, business_id)
        registry.save_account(
            conn,
            business_id,
            "google_search",
            mode="managed",
            status="active" if bound else "none",
            external={"customer_id": bound.customer_id} if bound else {},
        )
        conn.execute(
            "UPDATE channel_account SET checked_at = NULL WHERE business_id = %s AND channel = 'google_search'",
            (business_id,),
        )
        return
    link = get(conn, business_id)
    registry.save_account(
        conn,
        business_id,
        "google_search",
        mode=row["ads_mode"],
        status=MIRRORED.get(link.status, "none") if link else "none",
        external={"customer_id": link.customer_id} if link else {},
        last_error=link.last_error if link else None,
    )
    conn.execute(
        "UPDATE channel_account SET checked_at = %s WHERE business_id = %s AND channel = 'google_search'",
        (link.checked_at if link else None, business_id),
    )


def check_invite(conn: psycopg.Connection, business_id: str, text: str) -> str:
    """The normalised Customer ID when this business may send an invitation to it now; LinkRefused otherwise. In
    order: an active, claimed (or verified) business in own_account mode; the current agreement accepted; a
    well-formed number; no live link on this business; not the same invitation still pending; the number not linked
    to another business (D26 — the text never says which)."""
    row = conn.execute(
        "SELECT claim_state, ads_mode FROM business WHERE id = %s AND status = 'active'", (business_id,)
    ).fetchone()
    if row is None:
        raise LinkRefused("no such business", status=404)
    if row["claim_state"] == "draft":
        raise LinkRefused(NOT_CLAIMED)
    if row["ads_mode"] != "own_account":
        raise LinkRefused(NOT_OWN_ACCOUNT)
    if not agreements.accepted(conn, business_id):
        raise LinkRefused(NEEDS_AGREEMENT, code="needs_agreement")
    customer_id = normalize_customer_id(text)
    current = get(conn, business_id)
    if current is not None and current.live:
        raise LinkRefused(f"this business is already linked to Google Ads account {current.display}")
    if current is not None and current.status == "pending" and current.customer_id == customer_id:
        raise LinkRefused(f"the invitation to {current.display} is already on its way; accept it in Google Ads")
    if conn.execute(
        "SELECT 1 FROM ads_link WHERE customer_id = %s AND business_id <> %s", (customer_id, business_id)
    ).fetchone():
        raise LinkRefused(TAKEN)
    _never_a_pool_account(conn, customer_id)
    return customer_id


def _never_a_pool_account(conn: psycopg.Connection, customer_id: str) -> None:
    """A3c-1 review: one of Admiry's own pool accounts (`google_managed`) is never an owner's link -- the business's ads
    would run there on Admiry's card as its own account's, never taken from a balance. ALREADY_LINKED: it is Admiry's."""
    if google_managed.account(conn, customer_id) is not None:
        raise LinkRefused(ALREADY_LINKED)


def held(conn: psycopg.Connection, business_id: str, customer_id: str) -> bool:
    """This business's own link row already holds this Customer ID (send's `held`)."""
    return (
        conn.execute(
            "SELECT 1 FROM ads_link WHERE business_id = %s AND customer_id = %s", (business_id, customer_id)
        ).fetchone()
        is not None
    )


def save(
    conn: psycopg.Connection, business_id: str, customer_id: str, *, status: str, resource_name: str | None = None
) -> Link:
    """The business's one link row, inserted or replaced (a new invitation, a corrected number, an operator's word):
    invited_at now, no error, checked now unless pending. A Customer ID another business holds → LinkRefused(TAKEN)
    (the UNIQUE constraint; inside a transaction the caller's block rolls back); one of Admiry's pool accounts →
    LinkRefused(ALREADY_LINKED)."""
    _never_a_pool_account(conn, customer_id)
    try:
        conn.execute(
            "INSERT INTO ads_link (business_id, customer_id, status, resource_name, checked_at) "
            "VALUES (%(b)s, %(c)s, %(s)s, %(r)s, CASE WHEN %(checked)s THEN now() END) "
            "ON CONFLICT (business_id) DO UPDATE SET customer_id = EXCLUDED.customer_id, status = EXCLUDED.status, "
            "resource_name = EXCLUDED.resource_name, invited_at = now(), checked_at = EXCLUDED.checked_at, "
            "last_error = NULL",
            {"b": business_id, "c": customer_id, "s": status, "r": resource_name, "checked": status != "pending"},
        )
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name != "ads_link_customer_id_key":
            raise
        raise LinkRefused(TAKEN) from e
    mirror(conn, business_id)
    return get(conn, business_id)


def record_error(conn: psycopg.Connection, business_id: str, customer_id: str, message: str) -> None:
    """Keep Google's failure (an owner sentence) on the business's link row when that row holds the Customer ID the
    call was about (a failed invite for a new number leaves the old row alone); the status stays."""
    conn.execute(
        "UPDATE ads_link SET last_error = %s WHERE business_id = %s AND customer_id = %s",
        (message, business_id, customer_id),
    )
    mirror(conn, business_id)


def verify_by_link(conn: psycopg.Connection, business_id: str) -> bool:
    """Control of the linked Google Ads account verifies a business on its own account (D29): claims.verify with
    verified_via 'ads_link' and ads_mode 'own_account' — claimed → verified, a business released to draft while its
    owner was accepting claimed again first, a verified business untouched, an ownerless or retired one not verified,
    and a prepaid one never (it verifies by its first payment). True when this call verified it. Runs inside the
    caller's transaction."""
    return claims.verify(conn, business_id, via="ads_link", ads_mode="own_account")


def apply(conn: psycopg.Connection, business_id: str, customer_id: str, remote: Remote) -> Applied:
    """Record what Google said about this business's invitation — inside the caller's transaction. The row is
    locked first, so a Check now and the hourly sync reading the same ACTIVE at once serialise: the second sees
    the row already active (became_active False: one ads_link_active audit row, one verification). A row whose
    Customer ID changed since the read (the owner corrected the number) is left alone. ACTIVE verifies a business
    on its own account (verify_by_link; a prepaid one verifies by payment); REFUSED / CANCELLED / INACTIVE only
    change the status (a verified business stays verified — the A3 adapter refuses to act on a link that is not
    active). An unknown status only records the check."""
    row = conn.execute(
        "SELECT customer_id, status FROM ads_link WHERE business_id = %s FOR UPDATE", (business_id,)
    ).fetchone()
    if row is None or row["customer_id"] != customer_id:
        return Applied(link=get(conn, business_id), became_active=False, verified_now=False)
    if remote.status is None:
        conn.execute("UPDATE ads_link SET checked_at = now() WHERE business_id = %s", (business_id,))
        mirror(conn, business_id)
        return Applied(link=get(conn, business_id), became_active=False, verified_now=False)
    conn.execute(
        "UPDATE ads_link SET status = %s, resource_name = COALESCE(%s, resource_name), checked_at = now(), "
        "last_error = %s WHERE business_id = %s",
        (remote.status, remote.resource_name, remote.note, business_id),
    )
    mirror(conn, business_id)
    active = remote.status == "active"
    became = active and row["status"] not in LIVE
    verified = verify_by_link(conn, business_id) if active else False
    return Applied(link=get(conn, business_id), became_active=became, verified_now=verified)


# ---------------------------------------------------------------- Google Ads REST (api mode)


def _from_ads_error(e: google_ads.AdsError) -> LinkError:
    """Translate the shared client's AdsError into this module's own exception shape. ACCESS_TROUBLE, BUSY and
    UNREACHABLE are worded identically in google_ads.py by design, so those three carry over unchanged (str(e)
    already matches); the invitation/link-specific codes are recognised by their bare enum value regardless of
    which error domain Google put them under, and among *every* code a failed request carried (like the old
    single-module _failure's own `codes: set[str]`, not just AdsError.code, the first) — a response that fails for
    two reasons at once must not miss the one this module cares about; anything else becomes this module's own
    generic REFUSED — Google's error code is never shown to an owner for a link failure."""
    if str(e) in (google_ads.ACCESS_TROUBLE, google_ads.BUSY, google_ads.UNREACHABLE):
        return LinkError(str(e))
    values = {code.rsplit(": ", 1)[-1] for code in e.codes}
    if ALREADY_INVITED in values:
        return _Already(REFUSED)
    if ALREADY_MANAGED in values:
        return _Managed(ALREADY_LINKED)
    if values & NOT_FOUND_CODES:
        return LinkError(NOT_FOUND)
    return LinkError(REFUSED)


def send(
    settings: config.Settings, customer_id: str, *, held: bool = False, http: httpx.Client | None = None
) -> str | None:
    """Send the invitation from Admiry's MCC (api mode) and return its resource name. None in manual mode (nothing
    is sent; an operator sends it) and when Google says the invitation already exists. An account that is already
    a client of the MCC counts as sent only when `held` (this business's own row holds the Customer ID; the next
    read settles it); otherwise LinkRefused(ALREADY_LINKED) — nobody on that account acted, so nothing is saved.
    LinkError on any other failure."""
    if settings.google_ads_link_mode != "api":
        return None
    body = {"operation": {"create": {"clientCustomer": f"customers/{customer_id}", "status": "PENDING"}}}
    with _session(http) as h:
        try:
            data = _call(h, settings, f"customers/{settings.google_ads_mcc_id}/customerClientLinks:mutate", body)
        except google_ads.AdsError as e:
            translated = _from_ads_error(e)
            if isinstance(translated, _Already):
                return None
            if isinstance(translated, _Managed):
                if held:
                    return None
                raise LinkRefused(ALREADY_LINKED) from None
            raise translated from None
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return _name(result)


def withdraw(settings: config.Settings, link: Link, *, http: httpx.Client | None = None) -> None:
    """Cancel this business's pending invitation from Admiry's MCC (api mode; spec 2026-09-29 D67: the owner changes
    their mind before accepting it). Nothing is called in manual mode (an operator never sent one, or withdraws it in
    the MCC UI) or when no resource name was stored. LinkError when Google refuses — for example because the owner
    accepted it meanwhile; the next read settles the row."""
    if settings.google_ads_link_mode != "api" or not link.resource_name:
        return
    body = {"operation": {"update": {"resourceName": link.resource_name, "status": "CANCELED"}, "updateMask": "status"}}
    with _session(http) as h:
        try:
            _call(h, settings, f"customers/{settings.google_ads_mcc_id}/customerClientLinks:mutate", body)
        except google_ads.AdsError as e:
            raise _from_ads_error(e) from None


def _name(item: dict) -> str | None:
    """Google's resourceName when it is a string; anything else is not stored."""
    name = item.get("resourceName")
    return name if isinstance(name, str) else None


def _link_id(link: dict) -> int:
    try:
        return int(link.get("managerLinkId") or 0)
    except (TypeError, ValueError):
        return 0


def read(settings: config.Settings, customer_id: str, *, http: httpx.Client | None = None) -> Remote | None:
    """The newest invitation's status for this Customer ID (api mode); None in manual mode (nothing is read). No
    invitation at all reads as cancelled with NO_INVITATION, so the owner is offered Send again."""
    if settings.google_ads_link_mode != "api":
        return None
    with _session(http) as h:
        try:
            data = _call(
                h,
                settings,
                f"customers/{settings.google_ads_mcc_id}/googleAds:search",
                {"query": QUERY.format(customer_id=customer_id)},
            )
        except google_ads.AdsError as e:
            raise _from_ads_error(e) from None
    links = [
        r["customerClientLink"]
        for r in data.get("results") or []
        if isinstance(r, dict) and isinstance(r.get("customerClientLink"), dict)
    ]
    if not links:
        return Remote(status="cancelled", resource_name=None, note=NO_INVITATION)
    newest = max(links, key=_link_id)
    return Remote(status=STATUS_FROM_GOOGLE.get(str(newest.get("status"))), resource_name=_name(newest))
