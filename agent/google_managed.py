"""The Google Ads accounts Admiry runs prepaid businesses' Google Search ads in (A3c-1, plan
2026-10-03-agent-a3c-1-managed-google; founder decision 2026-10-03, amending spec 2026-09-25 D26: a pool of accounts
filed by industry, not one account per business).

A prepaid (`managed`) business's Google Search ads run in a client account under Admiry's manager account that Admiry
pays Google for, and the business's balance pays Admiry back day by day (`jobs.wallet_settle`).

**The pool** (`google_ads_pool`): every such account, filed under a scope -- `root` (any business), a family (L1,
`benchmark.CATEGORIES`: `attorneys`) or a family's trade (L2, spec 2026-10-01 §7.6: `attorneys/personal_injury`) --
and named after it in Google Ads (`group`: `account_root`, `account_attorneys`, `account_attorneys_personal_injury`).
An account takes at most `capacity` live businesses (None: no limit; 1 is what Google's third-party policy asks, "a
separate account for each end-advertiser") and a `closed` one takes no new business; a scope may hold several accounts
(the next one opens when the first is full).

**The binding** (`google_ads_binding`): a business runs in exactly one pool account at a time, and every account it
ever ran in stays recorded with the industry it had then, who bound it and when it was released (`history`). `assign`
is the allocator, called by the launch the first time a prepaid business's ads are built: the business's trade's
account, else its family's, else `root` -- within a scope the open account with room and the fewest live businesses --
recorded in one transaction with the candidate accounts locked, so two launches never overfill one. Nowhere with room:
the launch waits (ACCOUNT_OPENING) and the operator is told what to open (the Google Search adapter). A business keeps
its account: ads built there stay there, and only an operator moves it (`release`, then `bind`).

The trade comes from the business's knowledge (`trade`: `home_services/plumbing` or `plumbing`), which G2's playbooks
resolve; until then a business matches its family's account, or `root`.

An account is opened in the manager account's UI (or by API once Basic access allows `CreateCustomerClient`), billed
to Admiry's payments profile, and added with `agent.ops google-account add`, which reads it first (`probe`: a client
account, not a manager, in US dollars, enabled). A Customer ID an owner linked (`ads_link`) and the manager account
itself are never pool accounts.

Database functions take the caller's connection and never call Google; `probe` calls Google and takes none."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite

import httpx
import psycopg

from . import benchmark, business, config, geo, wallet
from .channels.google_search import client as google_ads

CHANNEL = "google_search"
ROOT = "root"
CURRENCY = "USD"  # every amount Admiry keeps is US dollars (the balance, the plans, Google's micros read back)
AUTO = "job:allocator"  # `bound_by` of the launch's own pick
POOL_COLUMNS = "customer_id, scope, capacity, status, descriptive_name, time_zone, added_by, added_at, note"
BINDING_COLUMNS = "id, business_id, customer_id, family, trade, bound_by, bound_at, released_at, note"
PROBE_QUERY = (
    "SELECT customer.descriptive_name, customer.manager, customer.currency_code, customer.time_zone, "
    "customer.status, customer.test_account FROM customer"
)

# L2: the trades of each family (spec 2026-10-01 §7.6, waves 1-3; G2's playbooks replace this list). A family not
# listed has no trade of its own yet.
TRADES: dict[str, tuple[str, ...]] = {
    "home_services": (
        "plumbing",
        "hvac",
        "electrical",
        "roofing",
        "remodeling",
        "windows_doors",
        "solar",
        "landscaping",
        "pest_control",
        "house_cleaning",
    ),
    "dentists": (
        "general_dentistry",
        "emergency_dentistry",
        "cosmetic_dentistry",
        "orthodontics_invisalign",
        "dental_implants",
        "pediatric_dentistry",
    ),
    "attorneys": (
        "personal_injury",
        "family_law",
        "estate_planning",
        "business_law",
        "immigration_law",
        "criminal_defense",
    ),
    "automotive_repair": ("auto_repair", "auto_body", "auto_detailing"),
    "physicians": ("chiropractic", "physical_therapy", "optometry", "med_spa"),
    "animals_pets": ("veterinary", "pet_grooming"),
    "personal_services": ("hair_salon", "nail_salon_spa", "barber_shop"),
    "health_fitness": ("gym_fitness_studio",),
    "real_estate": ("real_estate_agent", "property_management"),
    "finance_insurance": ("insurance_agent", "accounting_tax"),
}


# operator sentences (agent.ops; never shown to an owner)
BAD_SCOPE = (
    "scope must be root, a family ({families}) or <family>/<trade> with one of that family's trades; not {scope!r}"
)
BAD_CAPACITY = "capacity must be a whole number of 1 or more, or none"
IS_MANAGER = "{cid} is Admiry's manager account; add a client account opened under it"
OWNERS_OWN = "{cid} is an owner's own Google Ads account linked to Admiry (ads_link); add an account Admiry opened"
IN_POOL = "{cid} is already in the pool (scope {scope})"
NOT_IN_POOL = "{cid} is not in the pool; add it first (google-account add)"
NO_BUSINESS = "no active business {biz}"
NOT_PREPAID = (
    "business {biz} runs Google Search in its own Google Ads account (ads_mode {mode}); there is nothing to bind -- "
    "its owner links that account (ads-link)"
)
ALREADY = "business {biz} already runs in {cid}"
HAS_ANOTHER = (
    "business {biz} runs in {cid}; a business has one account -- release that one first (google-account release)"
)
CLOSED = "{cid} is closed to new businesses; pass --force to bind all the same"
FULL = "{cid} is full ({live} of {capacity}); pass --force to bind all the same"
BUILT_ELSEWHERE = (
    "business {biz} has Google Search ads built in {cid}; they stay there -- an operator removes them before the "
    "business moves to another account"
)
BUILT_HERE = "business {biz} has Google Search ads built in {cid}; pass --force to release it all the same"
NOT_BOUND = "business {biz} has no Google Ads account of Admiry's"
# founder 2026-10-03: no Google Ads account of Admiry's for a business that never paid (`wallet.has_paid`); --force too
NOT_PAID = "business {biz} has not paid yet (no card top-up); Admiry gives it no Google Ads account until it does"
# why a pool account takes no new business (operator sentences: `waiting`, the no-room mail, `bind`; spec 2026-10-03
# cold start §3.3)
WHY_CLOSED = "{group} {cid} is closed to new businesses"
WHY_FULL = "{group} {cid} is full ({live} of {capacity})"
WHY_RIVAL = "{group} {cid} already runs {other}, which competes with it ({family}, areas overlap)"
COMPETES = "business {biz} competes with {other} in {cid} ({family}, areas overlap); pass --force to bind all the same"
NO_SCOPE_ACCOUNT = "no account of its scopes is in the pool yet"
# a binding's note for a tenant of one industry whose area could not be compared (spec 2026-10-03 cold start §3.3)
NOT_COMPARED = "not compared with {other} ({family}): {why}"


class PoolRefused(Exception):
    """An operator-readable reason the pool or a binding was not changed."""


@dataclass(frozen=True)
class Pooled:
    """One pool account, with the businesses it runs now (`live`)."""

    customer_id: str
    scope: str
    capacity: int | None
    status: str
    descriptive_name: str | None
    time_zone: str | None
    added_by: str
    added_at: datetime
    note: str | None
    live: int = 0

    @property
    def display(self) -> str:
        return format_customer_id(self.customer_id)

    @property
    def group(self) -> str:
        return group(self.scope)

    @property
    def has_room(self) -> bool:
        return self.status == "open" and (self.capacity is None or self.live < self.capacity)


@dataclass(frozen=True)
class Binding:
    """A business's run in one pool account: live while `released_at` is None."""

    id: int
    business_id: str
    customer_id: str
    family: str
    trade: str | None
    bound_by: str
    bound_at: datetime
    released_at: datetime | None
    note: str | None

    @property
    def display(self) -> str:
        return format_customer_id(self.customer_id)


def format_customer_id(customer_id: str) -> str:
    return f"{customer_id[:3]}-{customer_id[3:6]}-{customer_id[6:]}"


# ---------------------------------------------------------------- industries and scopes


def group(scope: str) -> str:
    """The account's name in Google Ads for its scope: `account_root`, `account_attorneys`,
    `account_attorneys_personal_injury`."""
    return "account_" + scope.replace("/", "_")


def normalize_scope(text: str) -> str:
    """`text` as a scope (`root`, a family, `<family>/<trade>`; `_` between family and trade is taken for the slash,
    as `group` writes it, and an `account_` prefix is dropped); PoolRefused otherwise."""
    raw = (text or "").strip().lower()
    scope = raw.removeprefix("account_")
    if scope == ROOT or scope in benchmark.CATEGORIES:
        return scope
    if "/" not in scope:  # `attorneys_personal_injury`, as the account is named (the longest family that fits)
        family = next(
            (f for f in sorted(benchmark.CATEGORIES, key=len, reverse=True) if scope.startswith(f + "_")), None
        )
        scope = f"{family}/{scope[len(family) + 1 :]}" if family else scope
    family, _, trade = scope.partition("/")
    if family in benchmark.CATEGORIES and trade in TRADES.get(family, ()):
        return scope
    raise PoolRefused(BAD_SCOPE.format(families=", ".join(benchmark.CATEGORIES), scope=text))


def industry(conn: psycopg.Connection, business_id: str) -> tuple[str, str | None]:
    """The business's family (L1, `benchmark.CATEGORIES`; fallback when its knowledge names none or another) and trade
    (L2, its knowledge's `trade` -- `home_services/plumbing` or `plumbing` -- when it is one of the family's: None
    until G2 resolves it)."""
    found = business.latest_knowledge(conn, business_id)
    knowledge = found[1] if found else {}
    family = knowledge.get("category")
    family = family if family in benchmark.CATEGORIES else "fallback"
    trade = knowledge.get("trade")
    trade = trade.rsplit("/", 1)[-1].strip().lower() if isinstance(trade, str) else ""
    return family, (trade if trade in TRADES.get(family, ()) else None)


def candidates(family: str, trade: str | None) -> list[str]:
    """The scopes a business of this industry may run in, the most specific first: its trade's, its family's, root."""
    return ([f"{family}/{trade}"] if trade else []) + [family, ROOT]


# ---------------------------------------------------------------- competitors (spec 2026-10-03 cold start §3.3)


@dataclass(frozen=True)
class Footprint:
    """What two businesses compare to know whether they compete in one shared pool account: the industry (family,
    trade) and the area -- the listing's point and the approved plan's radius, or the service-area names."""

    business_id: str
    family: str
    trade: str | None
    point: tuple[float, float] | None
    radius_miles: float | None
    names: frozenset[str]


def _point(listing) -> tuple[float, float] | None:
    """The listing's (lat, lng) when both read as numbers on the Earth; None otherwise (text that is no number, NaN,
    a point off the Earth, no listing): never an exception."""
    if not isinstance(listing, dict):
        return None
    try:
        lat, lng = float(listing["lat"]), float(listing["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None  # False for NaN too


def _area_names(value) -> frozenset[str]:
    """`knowledge.service_area` as names to compare, trimmed and in lower case, by `profile._listed`'s rule: one text is
    one area, not its letters; a blank, a number, a flag or an object names nothing (final review I1)."""
    entries = value if isinstance(value, list) else [value]
    return frozenset(v.strip().lower() for v in entries if isinstance(v, str) and v.strip())


def _radius(value) -> float | None:
    try:
        miles = float(value)
    except (TypeError, ValueError):
        return None
    return miles if isfinite(miles) and miles > 0 else None


def footprint(
    conn: psycopg.Connection, business_id: str, *, family: str | None = None, trade: str | None = None
) -> Footprint:
    """The business's footprint: its industry (`industry`, unless the binding's own as bound is given), its latest
    knowledge's listing point and service-area names (trimmed, lower case), and its approved plan's radius (the
    consultation's `geo.radius_miles`; None without an approved plan)."""
    if family is None:
        family, trade = industry(conn, business_id)
    found = business.latest_knowledge(conn, business_id)
    knowledge = found[1] if found else {}
    row = conn.execute(
        "SELECT json->'sections'->'consultation'->'geo'->>'radius_miles' AS radius FROM plan "
        "WHERE business_id = %s AND status = 'approved' ORDER BY version DESC LIMIT 1",
        (business_id,),
    ).fetchone()
    radius = _radius(row["radius"]) if row else None
    names = _area_names(knowledge.get("service_area"))
    return Footprint(business_id, family, trade, _point(knowledge.get("listing")), radius, names)


def _one_industry(a: Footprint, b: Footprint) -> bool:
    """Two businesses in one industry: the same family other than `fallback`, and the same trade when both have one (a
    business is never in one industry with itself, for this rule)."""
    if a.business_id == b.business_id or a.family != b.family or a.family == "fallback":
        return False
    return not (a.trade and b.trade and a.trade != b.trade)


def competes(a: Footprint, b: Footprint) -> bool:
    """Two businesses compete in one shared account when they are in one industry -- the same family other than
    `fallback`, and the same trade when both have one -- and their areas overlap: both with a point and a radius,
    the points nearer than the radii's sum; neither with a point, a service-area name in common. A point against
    names, or a missing radius, is not comparable: they do not compete. A business never competes with itself."""
    if not _one_industry(a, b):
        return False
    if a.point is not None and b.point is not None:
        if a.radius_miles is None or b.radius_miles is None:
            return False
        return geo.miles(*a.point, *b.point) < a.radius_miles + b.radius_miles
    if a.point is None and b.point is None:
        return bool(a.names & b.names)
    return False


def unjudged(a: Footprint, b: Footprint) -> str | None:
    """Why two businesses of one industry could not be compared, so `competes` let them share an account (spec
    2026-10-03 cold start §3.3: said in the binding's note, so the operator can move one later): "a point against area
    names" or "no radius"; None when they were compared, or are not in one industry."""
    if not _one_industry(a, b):
        return None
    if (a.point is None) != (b.point is None):
        return "a point against area names"
    if a.point is not None and (a.radius_miles is None or b.radius_miles is None):
        return "no radius"
    return None


# ---------------------------------------------------------------- the pool


def _pooled(rows: list[dict], live: dict[str, int]) -> list[Pooled]:
    return [Pooled(**r, live=live.get(r["customer_id"], 0)) for r in rows]


def _live(conn: psycopg.Connection, customer_ids: list[str] | None = None) -> dict[str, int]:
    """Live businesses per pool account (every account, or `customer_ids`)."""
    where = "released_at IS NULL" + (" AND customer_id = ANY(%s)" if customer_ids is not None else "")
    rows = conn.execute(
        f"SELECT customer_id, count(*) AS n FROM google_ads_binding WHERE {where} GROUP BY customer_id",
        (customer_ids,) if customer_ids is not None else (),
    ).fetchall()
    return {r["customer_id"]: r["n"] for r in rows}


def pool(conn: psycopg.Connection) -> list[Pooled]:
    """Every pool account, by scope then the order they were added."""
    rows = conn.execute(f"SELECT {POOL_COLUMNS} FROM google_ads_pool ORDER BY scope, added_at, customer_id").fetchall()
    return _pooled(rows, _live(conn))


def account(conn: psycopg.Connection, customer_id: str) -> Pooled | None:
    r = conn.execute(f"SELECT {POOL_COLUMNS} FROM google_ads_pool WHERE customer_id = %s", (customer_id,)).fetchone()
    return _pooled([r], _live(conn, [customer_id]))[0] if r else None


def _capacity(value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise PoolRefused(BAD_CAPACITY)
    return value


def add(
    conn: psycopg.Connection,
    settings: config.Settings,
    customer_id: str,
    scope: str,
    *,
    capacity: int | None,
    by: str,
    found: dict | None = None,
    note: str | None = None,
) -> Pooled:
    """A client account into the pool under `scope` (`normalize_scope`), with what Google said of it (`found`,
    `probe`'s answer: its name and time zone); PoolRefused for the manager account, an owner's linked account, or one
    already in the pool."""
    scope = normalize_scope(scope)
    capacity = _capacity(capacity)
    shown = format_customer_id(customer_id)
    if settings.google_ads_mcc_id and customer_id == settings.google_ads_mcc_id.replace("-", ""):
        raise PoolRefused(IS_MANAGER.format(cid=shown))
    if conn.execute("SELECT 1 FROM ads_link WHERE customer_id = %s", (customer_id,)).fetchone():
        raise PoolRefused(OWNERS_OWN.format(cid=shown))
    there = account(conn, customer_id)
    if there is not None:
        raise PoolRefused(IN_POOL.format(cid=shown, scope=there.scope))
    found = found or {}
    try:
        conn.execute(
            "INSERT INTO google_ads_pool (customer_id, scope, capacity, descriptive_name, time_zone, added_by, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (customer_id, scope, capacity, found.get("descriptive_name"), found.get("time_zone"), by, note),
        )
    except psycopg.errors.UniqueViolation as e:  # another operator added it meanwhile
        raise PoolRefused(IN_POOL.format(cid=shown, scope=scope)) from e
    return account(conn, customer_id)


def set_capacity(conn: psycopg.Connection, customer_id: str, capacity: int | None) -> Pooled:
    """A pool account's capacity (None: no limit). One under the live count binds no new business and moves none."""
    if account(conn, customer_id) is None:
        raise PoolRefused(NOT_IN_POOL.format(cid=format_customer_id(customer_id)))
    conn.execute("UPDATE google_ads_pool SET capacity = %s WHERE customer_id = %s", (_capacity(capacity), customer_id))
    return account(conn, customer_id)


def set_status(conn: psycopg.Connection, customer_id: str, status: str) -> Pooled:
    """A pool account `open` or `closed`: a closed account keeps the businesses it runs and takes no new one."""
    if status not in ("open", "closed"):
        raise PoolRefused("status must be open or closed")
    if account(conn, customer_id) is None:
        raise PoolRefused(NOT_IN_POOL.format(cid=format_customer_id(customer_id)))
    conn.execute("UPDATE google_ads_pool SET status = %s WHERE customer_id = %s", (status, customer_id))
    return account(conn, customer_id)


# ---------------------------------------------------------------- bindings


def get(conn: psycopg.Connection, business_id: str) -> Binding | None:
    """The business's live binding, or None."""
    r = conn.execute(
        f"SELECT {BINDING_COLUMNS} FROM google_ads_binding WHERE business_id = %s AND released_at IS NULL",
        (business_id,),
    ).fetchone()
    return Binding(**r) if r else None


def history(conn: psycopg.Connection, business_id: str) -> list[Binding]:
    """Every account the business ran in, oldest first (the live one last, when there is one)."""
    rows = conn.execute(
        f"SELECT {BINDING_COLUMNS} FROM google_ads_binding WHERE business_id = %s ORDER BY bound_at, id",
        (business_id,),
    ).fetchall()
    return [Binding(**r) for r in rows]


def businesses(conn: psycopg.Connection, customer_id: str) -> list[Binding]:
    """The businesses a pool account runs now, the earliest bound first."""
    rows = conn.execute(
        f"SELECT {BINDING_COLUMNS} FROM google_ads_binding WHERE customer_id = %s AND released_at IS NULL "
        "ORDER BY bound_at, id",
        (customer_id,),
    ).fetchall()
    return [Binding(**r) for r in rows]


def rival(conn: psycopg.Connection, business_id: str, customer_id: str) -> Footprint | None:
    """The first business live in the pool account (the earliest bound) that competes with this one (`competes`; each
    live business with its industry as bound), or None."""
    mine = footprint(conn, business_id)
    for b in businesses(conn, customer_id):
        if b.business_id == business_id:
            continue
        theirs = footprint(conn, b.business_id, family=b.family, trade=b.trade)
        if competes(mine, theirs):
            return theirs
    return None


def note_for(conn: psycopg.Connection, business_id: str, customer_id: str) -> str | None:
    """The binding's note for a business joining the pool account: one NOT_COMPARED clause per live tenant of its
    industry whose area could not be compared with its own (`unjudged`); None when there is none."""
    mine = footprint(conn, business_id)
    said = []
    for b in businesses(conn, customer_id):
        if b.business_id == business_id:
            continue
        why = unjudged(mine, footprint(conn, b.business_id, family=b.family, trade=b.trade))
        if why is not None:
            said.append(NOT_COMPARED.format(other=b.business_id, family=b.family, why=why))
    return "; ".join(said) or None


def refusal(conn: psycopg.Connection, business_id: str, a: Pooled) -> str | None:
    """Why the pool account takes no new business now -- closed, full, or running a business that competes with this
    one -- as an operator sentence; None when it can take it."""
    if a.status != "open":
        return WHY_CLOSED.format(group=a.group, cid=a.display)
    if not a.has_room:
        return WHY_FULL.format(group=a.group, cid=a.display, live=a.live, capacity=a.capacity)
    other = rival(conn, business_id, a.customer_id)
    if other is not None:
        return WHY_RIVAL.format(group=a.group, cid=a.display, other=other.business_id, family=other.family)
    return None


def reasons(conn: psycopg.Connection, business_id: str) -> list[str]:
    """Why no pool account of the business's scopes takes it now, one operator sentence per account (`refusal`), or
    NO_SCOPE_ACCOUNT when the pool has none of its scopes; empty when one of them takes it (the next launch step
    binds it by itself)."""
    scopes = candidates(*industry(conn, business_id))
    accounts = [a for a in pool(conn) if a.scope in scopes]
    if not accounts:
        return [NO_SCOPE_ACCOUNT]
    said = [refusal(conn, business_id, a) for a in accounts]
    return [] if any(s is None for s in said) else [s for s in said if s is not None]


def first_bound(conn: psycopg.Connection, business_id: str) -> datetime | None:
    """When Admiry first ran the business's Google Search in an account of Admiry's: no day before it is the
    balance's (`jobs.wallet_settle` -- a day of ads in the owner's own account, before the business was prepaid, is
    the owner's with Google). None: never."""
    r = conn.execute(
        "SELECT min(bound_at) AS since FROM google_ads_binding WHERE business_id = %s", (business_id,)
    ).fetchone()
    return r["since"] if r else None


def _built_in(conn: psycopg.Connection, business_id: str) -> str | None:
    """The account the business's Google Search ads are built in, when they are (`resources.campaign`)."""
    r = conn.execute(
        "SELECT resources FROM channel_campaign WHERE business_id = %s AND channel = %s", (business_id, CHANNEL)
    ).fetchone()
    resources = (r["resources"] if r else None) or {}
    return resources.get("customer_id") if resources.get("campaign") else None


def _prepaid_business(conn: psycopg.Connection, business_id: str) -> None:
    """PoolRefused unless the business is active and prepaid (Google Search's mode `managed`)."""
    row = business.row(conn, business_id)
    if row is None or row["status"] != "active":
        raise PoolRefused(NO_BUSINESS.format(biz=business_id))
    from . import ads_mode  # imported here: ads_mode imports google_ads_link, which imports this module

    mode = ads_mode.current(conn, business_id)
    if mode != "managed":
        raise PoolRefused(NOT_PREPAID.format(biz=business_id, mode=mode))


def _paid(conn: psycopg.Connection, business_id: str) -> None:
    """PoolRefused(NOT_PAID) unless the business paid by card (`wallet.has_paid`; founder 2026-10-03)."""
    if not wallet.has_paid(conn, business_id):
        raise PoolRefused(NOT_PAID.format(biz=business_id))


def _insert(
    conn: psycopg.Connection, business_id: str, customer_id: str, *, by: str, note: str | None = None
) -> Binding:
    family, trade = industry(conn, business_id)
    conn.execute(
        "INSERT INTO google_ads_binding (business_id, customer_id, family, trade, bound_by, note) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (business_id, customer_id, family, trade, by, note),
    )
    from . import google_ads_link  # imported here: google_ads_link imports this module

    google_ads_link.mirror(conn, business_id)
    return get(conn, business_id)


def assign(conn: psycopg.Connection, business_id: str) -> tuple[Binding | None, bool]:
    """The allocator (the launch's, for a prepaid business): the business's live binding when it has one; else the
    first scope of `candidates` with an open account that has room and runs no competitor (`refusal`) -- the one with
    the fewest live businesses, then
    the earliest added -- bound now (`bound_by` AUTO, the business's industry recorded, the note naming any tenant of
    its industry it could not be compared with: `note_for`), inside one transaction that
    locks the business row and then the candidate accounts, so two launches never overfill one. Returns (the binding,
    whether this call made it); (None, False) when no candidate scope has room."""
    with conn.transaction():
        conn.execute("SELECT 1 FROM business WHERE id = %s FOR UPDATE", (business_id,))
        live = get(conn, business_id)
        if live is not None:
            return live, False
        _prepaid_business(conn, business_id)
        _paid(conn, business_id)
        scopes = candidates(*industry(conn, business_id))
        rows = conn.execute(
            f"SELECT {POOL_COLUMNS} FROM google_ads_pool WHERE scope = ANY(%s) AND status = 'open' "
            "ORDER BY customer_id FOR UPDATE",
            (scopes,),
        ).fetchall()
        open_ = _pooled(rows, _live(conn, [r["customer_id"] for r in rows]))
        for scope in scopes:
            room = sorted(
                (a for a in open_ if a.scope == scope and refusal(conn, business_id, a) is None),
                key=lambda a: (a.live, a.added_at, a.customer_id),
            )
            if room:
                chosen = room[0].customer_id
                return _insert(conn, business_id, chosen, by=AUTO, note=note_for(conn, business_id, chosen)), True
    return None, False


def bind(
    conn: psycopg.Connection,
    settings: config.Settings,
    business_id: str,
    customer_id: str,
    *,
    by: str,
    note: str | None = None,
    force: bool = False,
) -> Binding:
    """An operator's choice of the business's account, inside one transaction (the business row locked first): an
    active prepaid business with no live account and no ads built in another one, onto a pool account that is open,
    with room and no competitor (`rival`) -- `force` binds onto a closed, full or shared one all the same; the note is
    the operator's, then any tenant it could not be compared with (`note_for`). The same pair already bound is ALREADY."""
    shown = format_customer_id(customer_id)
    with conn.transaction():
        conn.execute("SELECT 1 FROM business WHERE id = %s FOR UPDATE", (business_id,))
        _prepaid_business(conn, business_id)
        _paid(conn, business_id)  # never forced: no account for a business that never paid
        live = get(conn, business_id)
        if live is not None:
            raise PoolRefused(
                (ALREADY if live.customer_id == customer_id else HAS_ANOTHER).format(biz=business_id, cid=live.display)
            )
        conn.execute("SELECT 1 FROM google_ads_pool WHERE customer_id = %s FOR UPDATE", (customer_id,))
        target = account(conn, customer_id)
        if target is None:
            raise PoolRefused(NOT_IN_POOL.format(cid=shown))
        if not force and target.status != "open":
            raise PoolRefused(CLOSED.format(cid=shown))
        if not force and not target.has_room:
            raise PoolRefused(FULL.format(cid=shown, live=target.live, capacity=target.capacity))
        other = None if force else rival(conn, business_id, customer_id)
        if other is not None:
            raise PoolRefused(COMPETES.format(biz=business_id, other=other.business_id, cid=shown, family=other.family))
        built = _built_in(conn, business_id)
        if built is not None and built != customer_id:
            raise PoolRefused(BUILT_ELSEWHERE.format(biz=business_id, cid=format_customer_id(built)))
        said = "; ".join(x for x in (note, note_for(conn, business_id, customer_id)) if x) or None
        return _insert(conn, business_id, customer_id, by=by, note=said)


def release(conn: psycopg.Connection, business_id: str, *, force: bool = False) -> Binding:
    """End the business's live binding (kept in its history), inside one transaction; PoolRefused when there is none,
    or -- unless `force` -- while its Google Search ads are built in that account (they keep running there until
    someone removes them). Google Search's `channel_account` row follows (status none)."""
    with conn.transaction():
        conn.execute("SELECT 1 FROM business WHERE id = %s FOR UPDATE", (business_id,))
        live = get(conn, business_id)
        if live is None:
            raise PoolRefused(NOT_BOUND.format(biz=business_id))
        if not force and _built_in(conn, business_id) == live.customer_id:
            raise PoolRefused(BUILT_HERE.format(biz=business_id, cid=live.display))
        r = conn.execute(
            "UPDATE google_ads_binding SET released_at = now() WHERE id = %s RETURNING released_at", (live.id,)
        ).fetchone()
        from . import google_ads_link

        google_ads_link.mirror(conn, business_id)
    return replace(live, released_at=r["released_at"])


def waiting(conn: psycopg.Connection) -> list[dict]:
    """The active prepaid businesses that paid (a card top-up: `wallet.has_paid`), whose approved plan funds Google
    Search and that run in no account yet, oldest approved plan first -- each with its industry's scopes (the most
    specific first), whether one of them has room now (then the next launch step binds it by itself), and why no
    account takes it (`reasons`)."""
    rows = conn.execute(
        "SELECT b.id, min(p.created_at) AS since FROM business b JOIN plan p ON p.business_id = b.id "
        "AND p.status = 'approved' LEFT JOIN channel_account a ON a.business_id = b.id AND a.channel = %(ch)s "
        "WHERE b.status = 'active' AND coalesce(a.mode, b.ads_mode) = 'managed' "
        "AND NOT EXISTS (SELECT 1 FROM google_ads_binding g WHERE g.business_id = b.id AND g.released_at IS NULL) "
        "AND EXISTS (SELECT 1 FROM wallet_entry w WHERE w.business_id = b.id AND w.kind = 'topup') "
        "AND EXISTS (SELECT 1 FROM "
        "jsonb_array_elements(coalesce(p.json->'sections'->'ads'->'channels', '[]'::jsonb)) c "
        "WHERE c->>'channel' = %(ch)s AND coalesce((c->>'monthly_usd')::numeric, 0) > 0) "
        "GROUP BY b.id ORDER BY since, b.id",
        {"ch": CHANNEL},
    ).fetchall()
    out = []
    for r in rows:
        family, trade = industry(conn, r["id"])
        scopes = candidates(family, trade)
        why = reasons(conn, r["id"])
        out.append(
            {
                "business_id": r["id"],
                "since": r["since"],
                "family": family,
                "trade": trade,
                "scopes": scopes,
                "room": not why,
                "reasons": why,
            }
        )
    return out


def probe(settings: config.Settings, customer_id: str, *, http: httpx.Client | None = None) -> dict:
    """What Google says of the account through Admiry's manager account (`login-customer-id`): its name, whether it is
    a manager, its currency, time zone, status and test flag -- and `problems`, the reasons it cannot carry a
    prepaid business's ads (a manager account, not US dollars, not enabled). google_ads.AdsError when Google refuses
    (an account the manager account does not manage reads as an access problem)."""
    rows = google_ads.search(settings, customer_id, PROBE_QUERY, http=http)
    customer = (rows[0].get("customer") if rows else None) or {}
    found = {
        "descriptive_name": customer.get("descriptiveName"),
        "manager": bool(customer.get("manager")),
        "currency_code": customer.get("currencyCode"),
        "time_zone": customer.get("timeZone"),
        "status": customer.get("status"),
        "test_account": bool(customer.get("testAccount")),
    }
    problems = []
    if not rows:
        problems.append("Google returned no customer row")
    if found["manager"]:
        problems.append("a manager account cannot run ads")
    if found["currency_code"] != CURRENCY:
        problems.append(f"its currency is {found['currency_code']}, not {CURRENCY}")
    if found["status"] != "ENABLED":
        problems.append(f"its status is {found['status']}, not ENABLED")
    return {**found, "problems": problems}
