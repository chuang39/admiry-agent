"""A plan's channels (spec 2026-09-29-admiry-channels-design §6.2, §7.2; plan 2026-09-29-agent-m0-channel-framework
P8, Task 5): the facts `allocate.initial` reads, fetched here -- every channel's adapter capabilities (a channel with
no adapter can start nothing, so it is never funded), the category's benchmark rows, the fit flags of the services the
owner picked, the tree's audience, the business's phone and page form, the goal's caps -- and the split written into
the plan's sections: one entry per funded channel with its amount, role, reason, basis and mode (the draft's copy for
that channel, or the primary's when the draft wrote none), the split at every budget level, the owner's named
amounts, and the draft's channels no adapter runs yet (for their later lines, `plans.later_rows`). `plans.py` reads
what this writes.

What a business can have (final review I2): a channel whose switch is `off` is never in a plan (Google Search aside:
its switch only says whether anything is sent to Google), a channel whose ads show a picture is in a plan only while
Admiry's set has one for the trade (M1 Task 7 fix round 1, item 3: the picture is pinned on the version, `fund`, so
approving a plan that funds it always approves its picture), and a channel that runs only from a balance Admiry holds
is in a plan only for a business that prepays (`usable_modes`). Such a channel, when the plan would have funded it, is
a later line saying why (`held`)."""

from __future__ import annotations

import psycopg
from fastmcp.exceptions import ToolError

from . import ads_mode, allocate, benchmark, business, config, consult, plans
from .channels import registry

COPY = ("themes", "negatives", "headlines", "descriptions", "landing_service")


def usable_modes(conn: psycopg.Connection, business_id: str) -> set[str]:
    """The ways this business can run a channel (spec §6.2's "a mode the business can use"): on its own account,
    always; Admiry running it from a balance only when the business prepays -- `ads_mode.current`, which answers
    whether this business pays through Admiry -- until M1m opens funding to own-account businesses."""
    return set(ads_mode.MODES) if ads_mode.current(conn, business_id) == "managed" else {"own_account"}


def switched_on(settings: config.Settings, channel: str) -> bool:
    """Whether a plan may have the channel at all: its switch (`registry.exec_mode`) is not `off`. Google Search
    always: its switch only says whether anything is sent to Google (A3 D49), and every plan starts there."""
    return channel == allocate.PRIMARY or registry.exec_mode(settings, channel) != "off"


def held(settings: config.Settings, usable: set[str], *, pictured: bool = True) -> dict[str, str]:
    """The registered channels that could run ads but that a business with the `usable` modes cannot have in a plan
    now, and why (`plans.HELD` words it): `coming` (its switch is off), `picture` (its ads show a picture and Admiry's
    set has none for the trade: `pictured` False -- before `prepaid`, which prepaying alone would not lift) or
    `prepaid` (it runs only from a balance Admiry holds, which the business does not have)."""
    out = {}
    for channel in registry.ORDER:
        caps = registry.adapter(channel).capabilities() if registry.known(channel) else None
        if caps is None or not caps.create:
            continue
        if not switched_on(settings, channel):
            out[channel] = "coming"
        elif caps.needs_image and not pictured:
            out[channel] = "picture"
        elif not caps.modes & usable:
            out[channel] = "prepaid"
    return out


def trade(session: dict, tree: dict, knowledge: dict) -> tuple[str, str | None]:
    """The business's trade and the service its consultation picked: what a version's picture is chosen by
    (`plans.choose_picture`), as the version's consultation section records them."""
    return knowledge.get("category", "fallback"), consult.facts(session, tree).get("service")


def facts(
    conn: psycopg.Connection,
    category: str,
    *,
    usable: set[str] | None = None,
    off: set[str] = frozenset(),
    modes: dict[str, str] | None = None,
) -> list[allocate.ChannelFacts]:
    """Every channel of the table, in card order: what its adapter declares, the modes the business can use
    (`usable`; None: every mode the adapter declares) and its benchmark row (P8: none when no published cost per lead
    exists). A channel in `off` (switched off) can create nothing here, so no band funds it. Its floor is the one for
    the way it runs for this business (`modes`, as `modes()` reads them; A3c-1: Google Search prepaid from $300)."""
    out = []
    for channel in registry.ORDER:
        caps = registry.adapter(channel).capabilities() if registry.known(channel) else None
        row = benchmark.lookup(conn, category, channel)
        declared = set(caps.modes) if caps else set()
        out.append(
            allocate.ChannelFacts(
                channel=channel,
                create=bool(caps and caps.create) and channel not in off,
                modes=declared,
                usable_modes=declared if usable is None else set(usable),
                min_monthly_usd=caps.floor((modes or {}).get(channel) or plans.default_mode(channel)) if caps else 0,
                cpl_avg=row["cpl_avg"] if row else None,
                lead_to_booking=row["lead_to_booking"] if row else None,
            )
        )
    return out


def modes(conn: psycopg.Connection, business_id: str) -> dict[str, str]:
    """How each channel runs for this business: its `channel_account` row's mode; Google Search without a row reads
    `business.ads_mode`, its mode for one more release (P6)."""
    rows = conn.execute("SELECT channel, mode FROM channel_account WHERE business_id = %s", (business_id,)).fetchall()
    out = {r["channel"]: r["mode"] for r in rows}
    if allocate.PRIMARY not in out:
        out[allocate.PRIMARY] = (business.row(conn, business_id) or {}).get("ads_mode") or "own_account"
    return out


def _has_form(conn: psycopg.Connection, business_id: str) -> bool:
    """A form on the business's published page -- known for a page Admiry holds; an owner's own page is read only by
    the landing check, which keeps no form flag, so it counts as none (the phone decides, §6.2)."""
    row = conn.execute(
        "SELECT html FROM page WHERE business_id = %s AND status = 'published'", (business_id,)
    ).fetchone()
    return bool(row and row["html"] and "<form" in row["html"].lower())


def inputs(
    conn: psycopg.Connection,
    business_id: str,
    *,
    session: dict,
    tree: dict,
    knowledge: dict,
    goal: dict | None,
    amount: float,
    named: dict[str, int] | None,
    usable: set[str] | None = None,
    off: set[str] = frozenset(),
    modes: dict[str, str] | None = None,
) -> allocate.Inputs:
    category = knowledge.get("category", "fallback")
    return allocate.Inputs(
        category=category,
        amount_usd=int(amount),
        channels=facts(conn, category, usable=usable, off=off, modes=modes),
        named={ch: int(v) for ch, v in (named or {}).items()},
        caps={ch: int(v) for ch, v in ((goal or {}).get("channel_caps") or {}).items()},
        compliance_flags=set(knowledge.get("compliance_flags") or []),
        service_flags=consult.service_flags(session, tree),
        homeowner_trade=tree.get("audience") == "homeowners",
        has_phone=len("".join(c for c in knowledge.get("phone") or "" if c.isdigit())) >= 10,
        has_form=_has_form(conn, business_id),
        service=consult.facts(session, tree).get("service"),
    )


def _creatable(channel: str) -> bool:
    return registry.known(channel) and registry.adapter(channel).capabilities().create


def fund(
    conn: psycopg.Connection,
    business_id: str,
    sections: dict,
    *,
    session: dict,
    tree: dict,
    knowledge: dict,
    goal: dict | None,
    spend: float,
    levels: dict[str, int],
    named: dict[str, int] | None,
) -> list[str]:
    """Split `spend` across the channels by §6.2 and write it into `sections["ads"]` (the module docstring says
    what); returns the plan's notes. The entries already there -- the model's draft, or the previous version's rows
    on `adjust` -- give each channel its copy; a draft channel with no adapter is kept for its later line, and one
    that could run but is not funded is a note. A channel the business cannot have (`held`) is never funded, even
    named (the pin is kept for a later version, and a note says why); the ones the plan would have funded go in
    `ads["held"]` with why, for their later lines. The version's picture is chosen here, once (`plans.choose_picture`:
    the previous version's while the trade has it): a channel that shows one is held without it, and pinned with it
    on the version (`plans.pin_picture`, M1 Task 7 fix round 1, items 3 and 4). ToolError when nothing can be funded
    (named amounts beyond the plan)."""
    usable = usable_modes(conn, business_id)
    category, service = trade(session, tree, knowledge)
    shown = plans.choose_picture(category, service, previous=sections)
    left_out = held(config.load(), usable, pictured=shown is not None)
    off = {ch for ch, why in left_out.items() if why in ("coming", "picture")}  # can create nothing in this plan
    pinned = {ch: v for ch, v in (named or {}).items() if ch not in left_out}
    common = {"session": session, "tree": tree, "knowledge": knowledge, "goal": goal, "amount": spend}
    account_modes = modes(conn, business_id)  # each channel's floor is its way's (A3c-1)
    base = inputs(conn, business_id, **common, named=pinned, usable=usable, off=off, modes=account_modes)
    split = allocate.initial(base)
    if not split.rows:
        raise ToolError(split.notes[-1])
    # what the same amount would fund with every channel open to this business: those held back get a later line
    could = {
        r.channel for r in allocate.initial(inputs(conn, business_id, **common, named=named, modes=account_modes)).rows
    }
    ads = sections["ads"] = dict(sections["ads"])
    entries = {c["channel"]: c for c in ads.get("channels") or []}
    pages = sections["presence"]["pages"]
    first = (
        entries.get(allocate.PRIMARY)
        or next(iter(entries.values()), None)
        or {"landing_service": pages[0]["service"] if pages else ""}
    )
    ads["channels"] = [
        {
            "channel": r.channel,
            "monthly_usd": float(r.monthly_usd),
            **{k: _copy(entries.get(r.channel, first), k) for k in COPY},
            "role": r.role,
            "why": r.why,
            "basis": r.basis,
            "mode": account_modes.get(r.channel) or plans.default_mode(r.channel),
            **_borrowed(r.channel, entries.get(r.channel), first),
        }
        for r in split.rows
    ]
    paid = {r.channel for r in split.rows}
    unpaid = [ch for ch in entries if ch not in paid]
    if "later" not in ads:  # a fresh draft: the channels it proposed that no adapter runs (plans.later_rows words them)
        ads["later"] = [ch for ch in unpaid if not registry.known(ch)]  # adjust keeps the version's own
    ads["held"] = {ch: why for ch, why in left_out.items() if ch in could and ch not in paid}
    notes = split.notes + [
        f"{registry.label(ch)}: not in this plan."
        for ch in unpaid
        if _creatable(ch) and ch not in split.dropped and ch not in left_out
    ]
    notes += [plans.held_note(ch, left_out[ch]) for ch in named or {} if ch in left_out]
    ads["named"] = {ch: int(v) for ch, v in (named or {}).items()}
    by_level = allocate.levels(base, {k: int(v) for k, v in levels.items()})
    ads["splits"] = {level: {r.channel: r.monthly_usd for r in s.rows} for level, s in by_level.items()}
    ads["monthly_limit_usd"] = max(float(ads.get("monthly_limit_usd") or 0), float(spend))
    plans.pin_picture(sections, shown)
    return notes


def _borrowed(channel: str, own: dict | None, first: dict) -> dict:
    """{"copy_of": <channel>} on an entry that runs another channel's words -- the draft wrote none for it, so it has
    `first`'s until its own build fits them (M1) -- kept through later versions: the plan page then says its ads come
    with the channel instead of showing another channel's ads under its name (final review M6). Never on the primary
    (`plans.copy_of`): brought back after a level dropped it, Google Search takes back the words the other entries
    carried for it, and those are the ones its build runs."""
    if channel == allocate.PRIMARY:
        return {}
    source = plans.copy_of(own) if own is not None else first.get("channel")
    return {"copy_of": source} if source else {}


def _copy(entry: dict, key: str):
    value = entry.get(key)
    if key == "landing_service":
        return value or ""
    return list(value or [])
