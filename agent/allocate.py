"""The initial split of a plan's amount across channels (spec 2026-09-29-admiry-channels-design §6.1, §6.2, D79, D80,
D82, D83, D87, D103; plan 2026-09-29-agent-m0-channel-framework P8, Task 5). Pure: facts in, a split out -- no
connection, no settings, no registry (`plans_channels` fetches the facts and calls it).

The rules, in the order the code applies them:

1. **Eligible** (`eligible`): an adapter that can create, a mode the business can use, and either a benchmark row
   (its expected cost per booked job exists) or the fit flag of the chosen service (`meta_fit`, `newsbreak_fit`; a
   flag beats the row requirement, which is how NewsBreak -- no row by design -- joins). Facebook & Instagram
   qualifies by the ratio (cost per booked within 2x Search's) or `meta_fit`; NewsBreak by `newsbreak_fit`, or by
   the ratio from $1,500 (the pooled evidence of D91, once a row exists); Nextdoor for a homeowner trade with a phone
   or a form. Local Services is never allocated (D82); Reddit and ChatGPT only when the owner names them (D83,
   D103). `compliance_flags` exclude nothing yet: housing and credit put Facebook & Instagram and Nextdoor under
   their platforms' special categories at build time (M4, M5) and no adapter declares a policy list in M0.
2. **The band's set** plus the named channels (`initial`): under $500 Google Search alone; from $500 Facebook &
   Instagram; from $800 Bing and NewsBreak's test for a fit service; from $1,500 NewsBreak by the ratio too, and
   Nextdoor.
3. **Floors**: each channel at least its minimum (the adapter's per-business floor); a named one exactly its named
   amount (raised to its minimum, capped by the owner's own limit for it); a cap below a channel's minimum keeps it
   out. From $800 an un-named Google Search's own floor is half the amount. Floors over the amount drop un-named
   channels from the bottom of `RANK` (tests, then secondaries, then the channels a tree flag promoted), then the
   half-rule yields to the owner's named amounts, then Google Search itself goes when even its minimum does not fit
   beside them. Google Search alone under its minimum keeps one row at the amount, and the plan says the budget is
   too small for ads.
4. **Shares** of the amount by band -- Bing 10 %, Facebook & Instagram 30 % (25 % from $1,500), NewsBreak a flat
   $300 test (15 %, never under $300, from $1,500), Nextdoor 15 % -- each clipped to its cap, never under its floor.
   A capped share's excess is re-split over the other shares by their percentages first (water-filling, each up to
   its own cap). Google Search takes the rest; when the shares leave it less than its half, they shrink toward their
   floors, and a capped Google Search stops at its cap with the pool re-split over the shares. When the owner named
   Google Search (or the channel that stands in for it), it keeps exactly that amount and the shares split the
   remainder. What no channel may take is never poured into a named amount: `Split.unplaced`, said in the notes.
5. **Whole dollars**, integer shares rounded down, the rounding remainder to the channel taking the rest: the rows
   and `unplaced` sum to the amount.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

PRIMARY = "google_search"
RANK = ("primary", "fit", "secondary", "test")  # the floor-fit drop order of §6.2, top first
# Within each tier, top first as the spec lists them; floors that do not fit drop from the bottom.
TIERS = {
    "primary": (PRIMARY,),
    "fit": ("meta", "newsbreak"),
    "secondary": ("meta", "microsoft_search"),
    "test": ("nextdoor", "newsbreak", "reddit", "chatgpt_ads"),
}
FIT_FLAG = {"meta": "meta_fit", "newsbreak": "newsbreak_fit"}
ROLE = {PRIMARY: "primary", "microsoft_search": "secondary", "meta": "secondary"}  # every other channel: "test"
NEVER = frozenset({"lsa"})  # D82: Local Services is the owner's own budget, managed in Google
RATIO = 2  # "eligible" = expected cost per booked within 2x Search's (D80)
HALF_BANDS = ("800-1499", ">=1500")  # "never below 50 %"
NEWSBREAK_TEST_USD = 300  # "a $300 test" (rev. 2)
# The owner names of D76 (== registry.LABELS, test-pinned): this module speaks in sentences but imports nothing.
NAMES = {
    "google_search": "Google Search",
    "microsoft_search": "Bing Search",
    "meta": "Facebook & Instagram",
    "nextdoor": "Nextdoor",
    "lsa": "Google Local Services",
    "newsbreak": "NewsBreak",
    "reddit": "Reddit",
    "chatgpt_ads": "ChatGPT",
}
# Why a channel is in the plan, in the spec's owner words (§6.2); `{service}` is the consultation's service.
REASON = {
    PRIMARY: "people there are already looking for {service} near you",
    "microsoft_search": "the same searches as on Google, at a lower price per click",
    "meta": "people who are not searching yet but live nearby and match your customers",
    "newsbreak": "local news readers in your ZIP codes, no published cost per lead yet, we will know in 30 days",
    "nextdoor": "neighbours asking for recommendations, we will know in 30 days",
}
NO_BENCHMARK = "no published benchmark; we will learn from the first 14 days"  # the spec's words (§6.2)
TOO_SMALL = "the budget is too small for ads"
NOTHING = "no channel can run these ads yet"
NOWHERE = "{amount} of your budget has nowhere to go; raise a channel's amount or lower the total."


@dataclass(frozen=True)
class ChannelFacts:
    """One channel as this business can use it: what its adapter declares (`create`, `modes`, the floor), the modes
    the business can use, and its benchmark row for the category (None when there is none, P8)."""

    channel: str
    create: bool
    modes: set[str]
    usable_modes: set[str]
    min_monthly_usd: int
    cpl_avg: float | None
    lead_to_booking: float | None


@dataclass(frozen=True)
class Inputs:
    category: str
    amount_usd: int
    channels: list[ChannelFacts]  # in the order the rows come back (the caller passes registry.ORDER)
    named: dict[str, int]  # the owner's own amounts per channel (adjust's pin)
    caps: dict[str, int]  # goal.channel_caps
    compliance_flags: set[str]
    service_flags: set[str]  # {"meta_fit", "newsbreak_fit"} from the chosen service's tree entry
    homeowner_trade: bool  # the tree's audience is homeowners
    has_phone: bool
    has_form: bool
    service: str | None = None  # the consultation's service, for the primary's `why`


@dataclass(frozen=True)
class Row:
    channel: str
    monthly_usd: int
    role: str  # primary | secondary | test
    why: str
    basis: str  # benchmark | named | fit


@dataclass(frozen=True)
class Split:
    rows: list[Row]
    dropped: list[str] = field(default_factory=list)  # in S once, left out by a cap or the floors
    notes: list[str] = field(default_factory=list)  # owner sentences for the plan's notes
    unplaced: int = 0  # what no channel may take (caps, or named amounts that are exact): the rows sum to amount - this


def _usd(n: int) -> str:
    return f"${n:,}"


def _name(channel: str) -> str:
    return NAMES.get(channel, channel)


def expected_cost_per_booked(f: ChannelFacts) -> float | None:
    """§6.1: cost per lead over the lead-to-booking rate; None without a benchmark row."""
    if f.cpl_avg is None or not f.lead_to_booking or f.lead_to_booking <= 0:
        return None
    return f.cpl_avg / f.lead_to_booking


def band(amount_usd: int) -> str:
    if amount_usd < 500:
        return "<500"
    if amount_usd < 800:
        return "500-799"
    if amount_usd < 1500:
        return "800-1499"
    return ">=1500"


def eligible(inputs: Inputs) -> list[str]:
    """The channels the business can use by band at some amount, in the order of `inputs.channels` (rule 1)."""
    primary = next((f for f in inputs.channels if f.channel == PRIMARY), None)
    search = expected_cost_per_booked(primary) if primary is not None else None

    def within_ratio(f: ChannelFacts) -> bool:
        cost = expected_cost_per_booked(f)
        return cost is not None and search is not None and cost <= RATIO * search

    out = []
    for f in inputs.channels:
        ch = f.channel
        if not f.create or not (f.modes & f.usable_modes):
            continue
        if ch in (PRIMARY, "microsoft_search"):
            ok = expected_cost_per_booked(f) is not None
        elif ch in FIT_FLAG:
            ok = FIT_FLAG[ch] in inputs.service_flags or within_ratio(f)
        elif ch == "nextdoor":
            ok = expected_cost_per_booked(f) is not None and inputs.homeowner_trade
            ok = ok and (inputs.has_phone or inputs.has_form)
        else:  # no band funds it: Reddit and ChatGPT join only when named (D83, D103), Local Services never (D82)
            ok = False
        if ok:
            out.append(ch)
    return out


def _by_band(inputs: Inputs, b: str) -> list[str]:
    """Rule 2: the eligible channels this amount's band funds."""
    out = []
    for ch in eligible(inputs):
        if ch == "newsbreak":
            fit = FIT_FLAG[ch] in inputs.service_flags
            joins = (b == "800-1499" and fit) or b == ">=1500"
        else:
            joins = {
                PRIMARY: True,
                "meta": b != "<500",
                "microsoft_search": b in HALF_BANDS,
                "nextdoor": b == ">=1500",
            }.get(ch, False)
        if joins:
            out.append(ch)
    return out


def _tier(channel: str, flags: set[str]) -> str:
    if channel == PRIMARY:
        return "primary"
    if FIT_FLAG.get(channel) in flags:
        return "fit"
    return "secondary" if channel in TIERS["secondary"] else "test"


def _rank(channel: str, flags: set[str]) -> tuple[int, int]:
    """Where a channel sits in the drop order: smaller is higher (kept longer)."""
    tier = _tier(channel, flags)
    within = TIERS[tier]
    return RANK.index(tier), within.index(channel) if channel in within else len(within)


def _share(channel: str, b: str) -> int:
    """The band's percentage for a channel; also the weight a re-split pool is shared by (a flat test has none)."""
    return {
        "microsoft_search": 10,
        "meta": 25 if b == ">=1500" else 30,
        "newsbreak": 15 if b == ">=1500" else 0,
        "nextdoor": 15,
    }.get(channel, 0)


def _target(channel: str, amount: int, b: str) -> int:
    """Rule 4: the band's share of the amount, whole dollars rounded down; NewsBreak's test is $300 flat below
    $1,500 and never under $300 from there."""
    if channel == "newsbreak":
        return NEWSBREAK_TEST_USD if b == "800-1499" else max(NEWSBREAK_TEST_USD, amount * _share(channel, b) // 100)
    return amount * _share(channel, b) // 100


def why(
    channel: str, role: str, basis: str, amount: int, *, total: int, service: str | None, pin: int | None = None
) -> str:
    """Why a row is in the plan, in the spec's owner words (§6.2): a named channel says the owner's amount (or why
    it differs: the channel's minimum, the owner's own limit for it); the primary says "all" or "most" of the plan's
    `total` only when that is true."""
    name = _name(channel)
    reason = REASON.get(channel, NO_BENCHMARK).format(service=service or "what you offer")
    if basis == "named":
        if pin is None or amount == pin:
            return f"{name} gets the {_usd(amount)} a month you set: {reason}."
        if amount > pin:
            return f"{name} gets {_usd(amount)} a month, its minimum (you set {_usd(pin)}): {reason}."
        return f"{name} gets {_usd(amount)} a month, the most your goal allows it (you set {_usd(pin)}): {reason}."
    if role == "primary":
        if amount >= total:
            return f"{name} gets all of it because {reason}."
        if 2 * amount >= total:
            return f"{name} gets most of it because {reason}."
        return f"{name} gets {_usd(amount)} a month because {reason}."
    if role == "test":
        return f"{name} is a {_usd(amount)} test: {reason}."
    return f"{name} gets {_usd(amount)} a month: {reason}."


def _water_fill(amounts: dict[str, int], pool: int, weights: dict[str, int], caps: dict[str, int]) -> int:
    """Spread `pool` over the channels of `weights` in proportion to them, each up to its cap; a capped channel's
    excess goes round again. Returns what nobody could take."""
    while pool > 0:
        room = {ch: caps[ch] - amounts[ch] if ch in caps else pool for ch in weights}
        open_ = [ch for ch in weights if room[ch] > 0 and weights[ch] > 0]
        if not open_:
            break
        total = sum(weights[ch] for ch in open_)
        given = 0
        for ch in open_:
            add = min(room[ch], pool * weights[ch] // total)
            amounts[ch] += add
            given += add
        if given == 0:  # shares under a dollar each: one dollar at a time, in the channels' order
            ch = open_[0]
            amounts[ch] += 1
            given = 1
        pool -= given
    return pool


def initial(inputs: Inputs) -> Split:
    """§6.2's split of `inputs.amount_usd` (the module docstring has the rules). The rows come back in the order of
    `inputs.channels` and sum to the amount, except when named amounts exceed it (no rows) or caps leave money
    nobody may take (said in `notes`). Deterministic."""
    amount = max(0, int(inputs.amount_usd))
    facts = {f.channel: f for f in inputs.channels}
    flags = set(inputs.service_flags)
    b = band(amount)
    notes: list[str] = []
    dropped: list[str] = []
    named: dict[str, int] = {}
    for ch, pin in inputs.named.items():
        if ch in NEVER or ch not in facts:
            dropped.append(ch)
            notes.append(f"{_name(ch)} is not part of the plan's budget.")
            continue
        named[ch] = max(0, int(pin))
    chosen = _by_band(inputs, b)
    chosen += [ch for ch in facts if ch in named and ch not in chosen]
    caps = {ch: max(0, int(v)) for ch, v in inputs.caps.items()}
    for ch in list(chosen):
        if ch in caps and caps[ch] < facts[ch].min_monthly_usd:
            chosen.remove(ch)
            dropped.append(ch)
            notes.append(
                f"{_name(ch)}: your limit of {_usd(caps[ch])} a month is below its "
                f"{_usd(facts[ch].min_monthly_usd)} minimum, so it is not in this plan."
            )
    floor = {ch: _capped(max(facts[ch].min_monthly_usd, named.get(ch, 0)), caps.get(ch)) for ch in chosen}
    pinned = sum(floor[ch] for ch in chosen if ch in named)
    if pinned > amount:
        notes.append(
            f"the amounts you set per channel come to {_usd(pinned)} a month, more than the plan's {_usd(amount)}."
        )
        return Split(rows=[], dropped=dropped, notes=notes)
    if chosen == [PRIMARY] and amount < floor[PRIMARY]:
        kept = min(amount, caps.get(PRIMARY, amount))
        row = Row(
            PRIMARY,
            kept,
            "primary",
            why(PRIMARY, "primary", "benchmark", kept, total=kept, service=inputs.service),
            _basis(PRIMARY, named, flags),
        )
        notes.append(TOO_SMALL)
        if kept < amount:
            notes.append(_unspent(amount - kept, amount))
        return Split(rows=[row], dropped=dropped, notes=notes, unplaced=amount - kept)

    half = (amount + 1) // 2 if b in HALF_BANDS else 0

    def primary_floor(h: int) -> int:  # a named primary takes exactly the owner's amount: no half-rule for it
        if PRIMARY in named:
            return floor[PRIMARY]
        return _capped(max(floor[PRIMARY], h), caps.get(PRIMARY))

    def need(h: int) -> int:
        rest = sum(floor[ch] for ch in chosen if ch != PRIMARY)
        return rest + (primary_floor(h) if PRIMARY in chosen else 0)

    for ch in sorted(
        (c for c in chosen if c != PRIMARY and c not in named), key=lambda c: _rank(c, flags), reverse=True
    ):
        if need(half) <= amount:
            break
        chosen.remove(ch)
        dropped.append(ch)
        notes.append(_no_room(ch, floor[ch]))
    if need(half) > amount:  # the owner's named amounts leave Google Search less than half: they win
        half = 0
    if need(half) > amount and PRIMARY in chosen and PRIMARY not in named:
        chosen.remove(PRIMARY)
        dropped.append(PRIMARY)
        notes.append(_no_room(PRIMARY, floor[PRIMARY]))
    if not chosen:
        notes.append(NOTHING)
        return Split(rows=[], dropped=dropped, notes=notes)

    # the anchor takes the rest: Google Search, or (when the owner's amounts left it out) the highest-ranked channel
    anchor = PRIMARY if PRIMARY in chosen else min(chosen, key=lambda c: _rank(c, flags))
    free = [ch for ch in chosen if ch != anchor and ch not in named]  # the channels the band's shares fund
    targets = {ch: _target(ch, amount, b) for ch in free}
    weights = {ch: _share(ch, b) for ch in free}
    amounts = {ch: floor[ch] for ch in chosen if ch in named}  # a named amount is exact, the anchor's too
    for ch in free:
        amounts[ch] = max(floor[ch], _capped(targets[ch], caps.get(ch)))
    # a capped share returns its excess to the pool, which is re-split over the other shares first (§6.2); what they
    # cannot take is left in the rest
    excess = sum(targets[ch] - amounts[ch] for ch in free if ch in caps and targets[ch] > amounts[ch])
    _water_fill(amounts, excess, weights, caps)
    unplaced = 0
    if anchor in named:  # the owner's amount holds: the others take the remainder by their shares
        if (over := sum(amounts.values()) - amount) > 0:
            _shrink(amounts, floor, free, over)
        unplaced = _water_fill(amounts, amount - sum(amounts.values()), weights, caps)
        if unplaced:
            notes.append(NOWHERE.format(amount=_usd(unplaced)))
    else:
        anchor_floor = primary_floor(half) if anchor == PRIMARY else floor[anchor]
        if (over := sum(amounts.values()) - (amount - anchor_floor)) > 0:  # the shares crowd the anchor's floor
            _shrink(amounts, floor, free, over)
        rest = amount - sum(amounts.values())
        if anchor in caps and rest > caps[anchor]:
            unplaced = _water_fill(amounts, rest - caps[anchor], weights, caps)
            rest = caps[anchor]
            if unplaced:
                notes.append(_unspent(unplaced, amount))
        amounts[anchor] = rest
    total = sum(amounts.values())
    rows = []
    for ch in (c for c in facts if c in amounts):
        role, basis = ROLE.get(ch, "test"), _basis(ch, named, flags)
        reason = why(ch, role, basis, amounts[ch], total=total, service=inputs.service, pin=named.get(ch))
        rows.append(Row(ch, amounts[ch], role, reason, basis))
    return Split(rows=rows, dropped=dropped, notes=notes, unplaced=unplaced)


def _shrink(amounts: dict[str, int], floor: dict[str, int], free: list[str], over: int) -> None:
    """Take `over` dollars back from the free channels' parts above their floors, in proportion to those parts."""
    extra = {ch: amounts[ch] - floor[ch] for ch in free}
    total = sum(extra.values())
    for ch, e in extra.items():
        amounts[ch] = floor[ch] + (e * (total - over) // total if total else 0)


def _capped(amount: int, cap: int | None) -> int:
    return amount if cap is None else min(amount, cap)


def _basis(channel: str, named: dict[str, int], flags: set[str]) -> str:
    if channel in named:
        return "named"
    return "fit" if FIT_FLAG.get(channel) in flags else "benchmark"


def _no_room(channel: str, floor: int) -> str:
    return f"{_name(channel)} needs at least {_usd(floor)} a month, which does not fit this budget beside the others."


def _unspent(unspent: int, amount: int) -> str:
    return f"the limits you set per channel leave {_usd(unspent)} of the {_usd(amount)} a month unspent."


def levels(inputs: Inputs, amounts: dict[str, int]) -> dict[str, Split]:
    """The split recomputed at each budget level's amount (the plan card's level control, §7.2)."""
    return {name: initial(replace(inputs, amount_usd=int(amount))) for name, amount in amounts.items()}
