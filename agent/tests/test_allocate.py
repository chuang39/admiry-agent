"""allocate.initial: a plan's amount split across channels by budget band (spec 2026-09-29-admiry-channels-design
§6.2, D79, D80; plan 2026-09-29-agent-m0-channel-framework Task 5, Review Focus 3). Pure: synthetic facts for every
channel of the spec's table, so the bands are tested whatever the registry holds today."""

from __future__ import annotations

import random
from dataclasses import replace

from agent import allocate
from agent.allocate import ChannelFacts, Inputs, Row
from agent.channels import registry
from agent.tests.conftest import JARGON, MONEY_JARGON

FLOORS = {  # spec §2: the platform minimums (D87)
    "google_search": 100,
    "newsbreak": 300,
    "microsoft_search": 100,
    "meta": 150,
    "nextdoor": 150,
    "lsa": 100,
    "reddit": 600,
    "chatgpt_ads": 300,
}
MODES = {
    "google_search": {"own_account", "managed"},
    "newsbreak": {"managed"},
    "microsoft_search": {"own_account", "managed"},
    "meta": {"own_account", "managed"},
    "nextdoor": {"managed"},
    "lsa": {"own_account"},
    "reddit": {"own_account"},
    "chatgpt_ads": {"own_account"},
}
# Home services by Appendix B, with the spec's 0.42 for search (so Facebook & Instagram qualifies by the ratio:
# 42.95 / 0.13 = 331 against 2 × 90.92 / 0.42 = 433); no row for NewsBreak, Reddit or ChatGPT (P8).
HOME = {
    "google_search": (90.92, 0.42),
    "microsoft_search": (90.92, 0.42),
    "meta": (42.95, 0.13),
    "nextdoor": (50.0, 0.25),
    "lsa": (53.0, 0.44),
}
# Dentists as seeded: 61.56 / 0.13 = 474 is more than 2 × 72.97 / 0.50 = 292, so Facebook & Instagram only by meta_fit.
DENTISTS = {"google_search": (72.97, 0.50), "microsoft_search": (72.97, 0.50), "meta": (61.56, 0.13)}
ROOFING = {"meta_fit", "newsbreak_fit"}


def fact(channel: str, rows: dict = HOME, *, create: bool = True, floor: int | None = None) -> ChannelFacts:
    cpl, l2b = rows.get(channel, (None, None))
    return ChannelFacts(
        channel=channel,
        create=create,
        modes=set(MODES[channel]),
        usable_modes={"own_account", "managed"},
        min_monthly_usd=FLOORS[channel] if floor is None else floor,
        cpl_avg=cpl,
        lead_to_booking=l2b,
    )


def every_channel(rows: dict = HOME, **floors: int) -> list[ChannelFacts]:
    """Every channel of the spec's table as if each had an adapter that can create."""
    return [fact(ch, rows, floor=floors.get(ch)) for ch in registry.ORDER]


def m0_channels(rows: dict = HOME) -> list[ChannelFacts]:
    """What M0 registers: Google Search and the NewsBreak stub can create; the rest have no adapter yet."""
    return [
        fact(ch, rows) if ch in ("google_search", "newsbreak") else replace(fact(ch, rows), create=False, modes=set())
        for ch in registry.ORDER
    ]


def inputs(
    amount: int,
    channels: list[ChannelFacts] | None = None,
    *,
    named: dict | None = None,
    caps: dict | None = None,
    flags: set | None = None,
    homeowner: bool = True,
    phone: bool = True,
    form: bool = False,
    category: str = "home_services",
    service: str | None = "Emergency repairs",
) -> Inputs:
    return Inputs(
        category=category,
        amount_usd=amount,
        channels=every_channel() if channels is None else channels,
        named=named or {},
        caps=caps or {},
        compliance_flags=set(),
        service_flags=flags or set(),
        homeowner_trade=homeowner,
        has_phone=phone,
        has_form=form,
        service=service,
    )


def amounts(split: allocate.Split) -> dict[str, int]:
    return {r.channel: r.monthly_usd for r in split.rows}


# ---------------------------------------------------------------- the pieces


def test_expected_cost_per_booked_is_the_cost_per_lead_over_the_booking_rate():
    assert allocate.expected_cost_per_booked(fact("meta")) == 42.95 / 0.13
    assert allocate.expected_cost_per_booked(fact("newsbreak")) is None  # no published cost per lead (P8)
    assert allocate.expected_cost_per_booked(replace(fact("meta"), lead_to_booking=0)) is None


def test_the_bands_are_the_spec_table_rows():
    assert [allocate.band(a) for a in (0, 499, 500, 799, 800, 1499, 1500, 20_000)] == [
        "<500",
        "<500",
        "500-799",
        "500-799",
        "800-1499",
        "800-1499",
        ">=1500",
        ">=1500",
    ]
    assert allocate.RANK == ("primary", "fit", "secondary", "test")


def test_eligible_is_create_a_usable_mode_a_benchmark_row_or_a_fit_flag():
    # home services: every channel with a row qualifies; NewsBreak has none and needs its flag; Local Services is
    # never allocated (D82); Reddit and ChatGPT only when the owner names them (D83, D103)
    assert allocate.eligible(inputs(2000)) == ["google_search", "microsoft_search", "meta", "nextdoor"]
    assert allocate.eligible(inputs(2000, flags=ROOFING)) == [
        "google_search",
        "newsbreak",
        "microsoft_search",
        "meta",
        "nextdoor",
    ]
    # dentists: Facebook & Instagram is 3.2 × Search per booked job, so only a meta_fit service admits it
    dentists = every_channel(DENTISTS)
    assert "meta" not in allocate.eligible(inputs(2000, dentists, category="dentists", homeowner=False))
    assert "meta" in allocate.eligible(inputs(2000, dentists, category="dentists", flags={"meta_fit"}))
    # no adapter that can create, or no mode the business can use: not eligible, whatever the row says
    assert allocate.eligible(inputs(2000, m0_channels(), flags=ROOFING)) == ["google_search", "newsbreak"]
    no_mode = [replace(f, usable_modes={"own_account"}) if f.channel == "newsbreak" else f for f in every_channel()]
    assert "newsbreak" not in allocate.eligible(inputs(2000, no_mode, flags=ROOFING))
    # as seeded (0.60 for home-services search, not the spec's 0.42), Facebook & Instagram costs 2.2 × Search per
    # booked job: over the 2 × line, so only meta_fit admits it
    seeded = every_channel({**HOME, "google_search": (90.92, 0.60)})
    assert "meta" not in allocate.eligible(inputs(2000, seeded))
    assert "meta" in allocate.eligible(inputs(2000, seeded, flags={"meta_fit"}))
    # Nextdoor is for homeowner trades with a phone or a form
    assert "nextdoor" not in allocate.eligible(inputs(2000, homeowner=False))
    assert "nextdoor" not in allocate.eligible(inputs(2000, phone=False))
    assert "nextdoor" in allocate.eligible(inputs(2000, phone=False, form=True))


# ---------------------------------------------------------------- Review Focus 3: the band table, then properties

# A plumber (home services, no fit flag, a homeowner trade with a phone), every channel creatable: Google Search the
# rest (never below half from $800), Bing 10 % from $800 (never below its $100 minimum), Facebook & Instagram 30 %
# (25 % from $1,500), Nextdoor 15 % from $1,500. Integer shares, the rounding remainder to Google Search.
BAND_TABLE = {
    50: {"google_search": 50},  # Google Search alone under its $100 minimum: "too small for ads"
    100: {"google_search": 100},
    300: {"google_search": 300},
    499: {"google_search": 499},
    500: {"google_search": 350, "meta": 150},
    650: {"google_search": 455, "meta": 195},
    799: {"google_search": 560, "meta": 239},
    800: {"google_search": 460, "microsoft_search": 100, "meta": 240},
    950: {"google_search": 565, "microsoft_search": 100, "meta": 285},
    1000: {"google_search": 600, "microsoft_search": 100, "meta": 300},
    1234: {"google_search": 741, "microsoft_search": 123, "meta": 370},
    1499: {"google_search": 901, "microsoft_search": 149, "meta": 449},
    1500: {"google_search": 750, "microsoft_search": 150, "meta": 375, "nextdoor": 225},
    1501: {"google_search": 751, "microsoft_search": 150, "meta": 375, "nextdoor": 225},
    1999: {"google_search": 1002, "microsoft_search": 199, "meta": 499, "nextdoor": 299},
    2000: {"google_search": 1000, "microsoft_search": 200, "meta": 500, "nextdoor": 300},
    2500: {"google_search": 1250, "microsoft_search": 250, "meta": 625, "nextdoor": 375},
    3333: {"google_search": 1668, "microsoft_search": 333, "meta": 833, "nextdoor": 499},
    5000: {"google_search": 2500, "microsoft_search": 500, "meta": 1250, "nextdoor": 750},
    10000: {"google_search": 5000, "microsoft_search": 1000, "meta": 2500, "nextdoor": 1500},
}


def _check_properties(inp: Inputs, split: allocate.Split) -> None:
    facts = {f.channel: f for f in inp.channels}
    got = amounts(split)
    assert split.rows, (inp, split)
    # to the dollar: the rows and what nobody may take (said in a note) make the amount
    assert sum(got.values()) + split.unplaced == inp.amount_usd, (inp, split)
    assert split.unplaced == 0 or any(allocate._usd(split.unplaced) in n for n in split.notes), split
    for ch, pin in inp.named.items():  # a named amount is exact: never overrun, never shrunk below the owner's
        want = max(pin, facts[ch].min_monthly_usd)
        assert got[ch] == (min(want, inp.caps[ch]) if ch in inp.caps else want), (ch, inp, split)
    too_small = allocate.TOO_SMALL in split.notes
    for r in split.rows:
        assert isinstance(r.monthly_usd, int) and r.monthly_usd >= 0
        if not too_small:
            assert r.monthly_usd >= facts[r.channel].min_monthly_usd, (r, inp)  # floors hold
        if r.channel in inp.caps:
            assert r.monthly_usd <= inp.caps[r.channel], (r, inp)  # caps hold
        assert r.why and r.why.endswith(".") and not JARGON.search(r.why) and not MONEY_JARGON.search(r.why)
        assert registry.label(r.channel) in r.why  # the owner names of D76, never an id
        assert r.role in ("primary", "secondary", "test") and r.basis in ("benchmark", "named", "fit")
        assert r.channel != "lsa"  # D82
        assert facts[r.channel].create or r.channel in inp.named  # never a channel nothing can start, unless named
    assert set(inp.named) <= set(got), (inp, split)  # named channels always join
    assert split == allocate.initial(inp)  # the same inputs, the same split
    if too_small:
        assert [r.channel for r in split.rows] == ["google_search"]
        assert inp.amount_usd < facts["google_search"].min_monthly_usd
    if "google_search" in got and inp.amount_usd >= 800 and not inp.named:
        assert 2 * got["google_search"] >= inp.amount_usd  # never below half from $800
    primary = next((r for r in split.rows if r.channel == "google_search"), None)
    if primary is not None and "most of it" in primary.why:
        assert 2 * primary.monthly_usd >= sum(got.values())  # "most" only when it is most
    if inp.amount_usd < 500 and not inp.named:
        assert set(got) == {"google_search"}  # under $500, Google Search alone


def test_initial_split_properties():
    for amount, want in BAND_TABLE.items():  # 20 amounts across the four bands: the table reproduced
        inp = inputs(amount)
        split = allocate.initial(inp)
        assert amounts(split) == want, amount
        _check_properties(inp, split)
    assert allocate.initial(inputs(50)).notes == [allocate.TOO_SMALL]
    rng = random.Random(20260929)
    for _ in range(200):
        inp = _random_inputs(rng)
        _check_properties(inp, allocate.initial(inp))


def _random_inputs(rng: random.Random) -> Inputs:
    """A hand-rolled generator: any amount in any band; Google Search always there (every plan has its primary) and
    any mix of the others -- creatable or not, any floor, a row or none; any fit flags, audience and contact; up to two
    named channels whose amounts fit the plan; caps on up to two others (never on the primary or a named channel, so
    the rest always has a home and the sum property is exact; caps on those are tested below)."""
    amount = rng.choice(
        [
            rng.randint(1, 99),
            rng.randint(100, 499),
            rng.randint(500, 799),
            rng.randint(800, 1499),
            rng.randint(1500, 20_000),
        ]
    )
    rows = rng.choice([HOME, DENTISTS])
    channels = [fact("google_search", rows, floor=rng.choice([50, 100, 150]))]
    for ch in registry.ORDER[1:]:
        if rng.random() < 0.8:
            f = fact(ch, rows, floor=rng.choice([FLOORS[ch], 50, 200, 400]), create=rng.random() < 0.8)
            if rng.random() < 0.2:
                f = replace(f, cpl_avg=None, lead_to_booking=None)
            channels.append(f)
    named: dict[str, int] = {}
    budget = amount
    for f in rng.sample(channels, k=min(len(channels), rng.choice([0, 0, 1, 2]))):
        pin = rng.randint(0, max(1, amount // 2))
        need = max(pin, f.min_monthly_usd)
        if f.channel != "lsa" and need <= budget:
            named[f.channel] = pin
            budget -= need
    free = [f.channel for f in channels if f.channel != "google_search" and f.channel not in named]
    caps = {ch: rng.randint(0, amount) for ch in rng.sample(free, k=min(len(free), rng.choice([0, 1, 2])))}
    return inputs(
        amount,
        channels,
        named=named,
        caps=caps,
        flags={f for f in ("meta_fit", "newsbreak_fit") if rng.random() < 0.4},
        homeowner=rng.random() < 0.5,
        phone=rng.random() < 0.7,
        form=rng.random() < 0.3,
        category="home_services" if rows is HOME else "dentists",
    )


# ---------------------------------------------------------------- the rules one by one


def test_meta_fit_admits_dentists_invisalign():
    dentists = every_channel(DENTISTS)
    plain = allocate.initial(inputs(1000, dentists, category="dentists", homeowner=False, service="Cleaning"))
    assert amounts(plain) == {"google_search": 900, "microsoft_search": 100}  # 3.2 × Search: not by the ratio
    fit = allocate.initial(
        inputs(1000, dentists, category="dentists", homeowner=False, flags={"meta_fit"}, service="Invisalign")
    )
    assert amounts(fit) == {"google_search": 600, "microsoft_search": 100, "meta": 300}
    row = next(r for r in fit.rows if r.channel == "meta")
    assert (row.role, row.basis) == ("secondary", "fit")
    assert row.why == (
        "Facebook & Instagram gets $300 a month: people who are not searching yet but live nearby and match your "
        "customers."
    )
    primary = fit.rows[0]
    assert (primary.channel, primary.role, primary.basis) == ("google_search", "primary", "benchmark")
    assert (
        primary.why == "Google Search gets most of it because people there are already looking for Invisalign near you."
    )


def test_newsbreak_fit_admits_roofing_at_800_with_a_300_row():
    # M0's registry: Google Search and NewsBreak can create; roofing carries newsbreak_fit
    split = allocate.initial(inputs(800, m0_channels(), flags=ROOFING, service="Roofing"))
    assert amounts(split) == {"google_search": 500, "newsbreak": 300}
    nb = next(r for r in split.rows if r.channel == "newsbreak")
    assert (nb.role, nb.basis) == ("test", "fit")
    assert nb.why == (
        "NewsBreak is a $300 test: local news readers in your ZIP codes, no published cost per lead yet, we will know "
        "in 30 days."
    )
    # the same service at $799, or a service without the flag at $800: Google Search alone
    assert amounts(allocate.initial(inputs(799, m0_channels(), flags=ROOFING))) == {"google_search": 799}
    assert amounts(allocate.initial(inputs(800, m0_channels()))) == {"google_search": 800}
    # from $1,500 NewsBreak takes 15 %, never less than its $300 test
    assert amounts(allocate.initial(inputs(1500, m0_channels(), flags=ROOFING))) == {
        "google_search": 1200,
        "newsbreak": 300,
    }
    assert amounts(allocate.initial(inputs(3000, m0_channels(), flags=ROOFING))) == {
        "google_search": 2550,
        "newsbreak": 450,
    }
    # "15 %, never under $300" is the test's own amount, not only NewsBreak's floor
    cheaper = [replace(f, min_monthly_usd=100) if f.channel == "newsbreak" else f for f in m0_channels()]
    assert amounts(allocate.initial(inputs(1500, cheaper, flags=ROOFING)))["newsbreak"] == 300


def test_newsbreak_by_the_ratio_joins_from_1500_only():
    """Once pooled evidence gives NewsBreak a cost per booked job within 2 × Search's (D91, M6), it joins from
    $1,500 without the flag; its $300 test at $800-1,499 stays for fit services."""
    pooled = [replace(f, cpl_avg=40.0, lead_to_booking=0.2) if f.channel == "newsbreak" else f for f in m0_channels()]
    assert "newsbreak" in allocate.eligible(inputs(1000, pooled))
    assert amounts(allocate.initial(inputs(1000, pooled))) == {"google_search": 1000}
    split = allocate.initial(inputs(1500, pooled))
    assert amounts(split) == {"google_search": 1200, "newsbreak": 300}
    assert [(r.role, r.basis) for r in split.rows if r.channel == "newsbreak"] == [("test", "benchmark")]


def test_floors_that_do_not_fit_drop_tests_first():
    # $1,500 for a plumber with dearer minimums: 750 (half) + 300 + 300 + 300 is over; Nextdoor (a test) goes first
    split = allocate.initial(inputs(1500, every_channel(microsoft_search=300, meta=300, nextdoor=300)))
    assert amounts(split) == {"google_search": 825, "microsoft_search": 300, "meta": 375}
    assert split.dropped == ["nextdoor"]
    assert split.notes == ["Nextdoor needs at least $300 a month, which does not fit this budget beside the others."]
    # roofing at $800 with every channel creatable: half is $400, so Bing (a secondary) goes, then the fit channels
    # from the bottom of their tier -- NewsBreak (newsbreak_fit) before Facebook & Instagram (meta_fit)
    roof = allocate.initial(inputs(800, flags=ROOFING, service="Roofing"))
    assert amounts(roof) == {"google_search": 560, "meta": 240} and roof.dropped == ["microsoft_search", "newsbreak"]
    # $900: the fit channels both fit at their floors once Bing is gone, and Google Search keeps its half
    assert amounts(allocate.initial(inputs(900, flags=ROOFING))) == {
        "google_search": 450,
        "newsbreak": 300,
        "meta": 150,
    }
    # $1,500 for a roofer: every channel fits at its floor and the shares above the floors shrink to keep the half
    full = allocate.initial(inputs(1500, flags=ROOFING))
    assert amounts(full) == {
        "google_search": 751,
        "newsbreak": 300,
        "microsoft_search": 107,
        "meta": 182,
        "nextdoor": 160,
    }


def test_a_named_channel_joins_at_its_floor_even_under_500():
    split = allocate.initial(inputs(450, m0_channels(), named={"newsbreak": 300}))
    assert amounts(split) == {"google_search": 150, "newsbreak": 300}
    nb = next(r for r in split.rows if r.channel == "newsbreak")
    assert nb.basis == "named"
    assert nb.why == (
        "NewsBreak gets the $300 a month you set: local news readers in your ZIP codes, no published cost per lead "
        "yet, we will know in 30 days."
    )
    # a named amount below the floor joins at the floor; above it, at the amount (and it is never scaled)
    assert amounts(allocate.initial(inputs(450, m0_channels(), named={"newsbreak": 100}))) == {
        "google_search": 150,
        "newsbreak": 300,
    }
    assert amounts(allocate.initial(inputs(2000, m0_channels(), named={"newsbreak": 700}, flags=ROOFING))) == {
        "google_search": 1300,
        "newsbreak": 700,
    }
    # a named amount is exact: at $3,000 NewsBreak's share would be $450; the owner's $300 stands
    assert amounts(allocate.initial(inputs(3000, m0_channels(), named={"newsbreak": 300}, flags=ROOFING))) == {
        "google_search": 2700,
        "newsbreak": 300,
    }
    # a report-only channel or ChatGPT only ever joins when named (D83, D103), at its floor
    assert "reddit" not in amounts(allocate.initial(inputs(20_000)))
    assert amounts(allocate.initial(inputs(2000, named={"chatgpt_ads": 0})))["chatgpt_ads"] == 300
    # Local Services is never part of the budget (D82), named or not
    never = allocate.initial(inputs(2000, named={"lsa": 500}))
    assert "lsa" not in amounts(never) and "lsa" in never.dropped


def test_named_amounts_beyond_the_plan_leave_no_split_and_say_why():
    split = allocate.initial(inputs(400, m0_channels(), named={"newsbreak": 500}))
    assert split.rows == [] and split.notes == [
        "the amounts you set per channel come to $500 a month, more than the plan's $400."
    ]
    # the named amounts fit, Google Search's floor does not: the primary goes, the named one keeps exactly its amount,
    # and the $50 nobody can take is said, never added to the owner's $300
    tight = allocate.initial(inputs(350, m0_channels(), named={"newsbreak": 300}))
    assert amounts(tight) == {"newsbreak": 300} and tight.dropped == ["google_search"] and tight.unplaced == 50
    assert tight.notes[-1] == "$50 of your budget has nowhere to go; raise a channel's amount or lower the total."
    assert tight.rows[0].why.startswith("NewsBreak gets the $300 a month you set: local news readers")


def test_caps_water_fill():
    # Facebook & Instagram capped under its $500 share: its $300 excess is re-split over the other shares (Bing 10,
    # Nextdoor 15), and Google Search keeps its half -- it takes only what they cannot
    capped = allocate.initial(inputs(2000, caps={"meta": 200}))
    assert amounts(capped) == {"google_search": 1000, "microsoft_search": 320, "meta": 200, "nextdoor": 480}
    # capped shares all round: what the others cannot take goes to Google Search
    both = allocate.initial(inputs(2000, caps={"meta": 200, "microsoft_search": 250, "nextdoor": 350}))
    assert amounts(both) == {"google_search": 1200, "microsoft_search": 250, "meta": 200, "nextdoor": 350}
    # Google Search capped under its half: it stops at its cap and the pool is re-split over the rest by their shares
    primary = allocate.initial(inputs(2000, caps={"google_search": 900}))
    assert amounts(primary) == {"google_search": 900, "microsoft_search": 220, "meta": 550, "nextdoor": 330}
    # a cap below a channel's minimum keeps it out (NewsBreak's $300 test cannot run under a $200 ceiling)
    out = allocate.initial(inputs(800, m0_channels(), flags=ROOFING, caps={"newsbreak": 200}))
    assert amounts(out) == {"google_search": 800} and out.dropped == ["newsbreak"]
    assert out.notes == ["NewsBreak: your limit of $200 a month is below its $300 minimum, so it is not in this plan."]
    # every channel capped short of the amount: the rest is left unspent and the plan says so
    short = allocate.initial(inputs(400, caps={"google_search": 300}))
    assert amounts(short) == {"google_search": 300} and short.unplaced == 100
    assert short.notes == ["the limits you set per channel leave $100 of the $400 a month unspent."]


def test_a_named_channel_above_its_cap_stops_at_the_cap():
    split = allocate.initial(inputs(2000, m0_channels(), named={"newsbreak": 700}, caps={"newsbreak": 500}))
    assert amounts(split) == {"google_search": 1500, "newsbreak": 500}
    assert split.rows[1].why.startswith("NewsBreak gets $500 a month, the most your goal allows it (you set $700):")
    raised = allocate.initial(inputs(450, m0_channels(), named={"newsbreak": 100}))
    assert raised.rows[1].why.startswith("NewsBreak gets $300 a month, its minimum (you set $100):")


def test_a_named_primary_takes_exactly_its_amount_and_the_others_share_the_rest():
    """Google Search pinned at $500 of $2,000 (Bing and Facebook & Instagram eligible, no homeowner trade): the pin
    holds, and Bing and Facebook & Instagram take the other $1,500 by their shares, 10 : 25."""
    split = allocate.initial(inputs(2000, named={"google_search": 500}, homeowner=False, service="Leaks"))
    assert amounts(split) == {"google_search": 500, "microsoft_search": 429, "meta": 1071} and split.unplaced == 0
    assert split.rows[0].why == (
        "Google Search gets the $500 a month you set: people there are already looking for Leaks near you."
    )
    # a pin under half is no half-rule floor: at $800 a roofer keeps every channel whose minimum fits beside $100
    low = allocate.initial(inputs(800, named={"google_search": 100}, flags=ROOFING))
    assert amounts(low) == {"google_search": 100, "newsbreak": 300, "microsoft_search": 115, "meta": 285}
    # a remainder no channel can take is said, never poured into the pin: NewsBreak's $300 test is flat below $1,500
    flat = allocate.initial(inputs(1000, m0_channels(), named={"google_search": 500}, flags=ROOFING))
    assert amounts(flat) == {"google_search": 500, "newsbreak": 300} and flat.unplaced == 200
    assert flat.notes == ["$200 of your budget has nowhere to go; raise a channel's amount or lower the total."]


def test_google_search_is_most_of_it_only_when_it_is():
    split = allocate.initial(inputs(2000, m0_channels(), named={"newsbreak": 1200}, flags=ROOFING, service="Roofing"))
    assert amounts(split) == {"google_search": 800, "newsbreak": 1200}
    assert split.rows[0].why == (
        "Google Search gets $800 a month because people there are already looking for Roofing near you."
    )


def test_nothing_that_can_run_leaves_no_split():
    none = [replace(f, create=False) for f in m0_channels()]
    split = allocate.initial(inputs(1000, none))
    assert split.rows == [] and split.notes == [allocate.NOTHING]


def test_levels_recompute_the_split_for_each_amount():
    inp = inputs(1000, m0_channels(), flags=ROOFING)
    got = allocate.levels(inp, {"minimum": 300, "conservative": 900, "recommended": 2200, "aggressive": 3700})
    assert {k: amounts(v) for k, v in got.items()} == {
        "minimum": {"google_search": 300},
        "conservative": {"google_search": 600, "newsbreak": 300},
        "recommended": {"google_search": 1870, "newsbreak": 330},
        "aggressive": {"google_search": 3145, "newsbreak": 555},
    }
    assert got["recommended"] == allocate.initial(replace(inp, amount_usd=2200))


def test_the_rows_speak_in_owner_words():
    split = allocate.initial(inputs(3000, flags=ROOFING, service="Roofing"))
    assert [r.channel for r in split.rows] == ["google_search", "newsbreak", "microsoft_search", "meta", "nextdoor"]
    by = {r.channel: r for r in split.rows}
    assert by["microsoft_search"].why == (
        f"Bing Search gets ${by['microsoft_search'].monthly_usd} a month: the same searches as on Google, at a lower "
        "price per click."
    )
    assert by["nextdoor"].why.startswith(f"Nextdoor is a ${by['nextdoor'].monthly_usd} test: neighbours asking")
    assert [by[c].role for c in ("google_search", "newsbreak", "microsoft_search", "meta", "nextdoor")] == [
        "primary",
        "test",
        "secondary",
        "secondary",
        "test",
    ]
    alone = allocate.initial(inputs(400, service=None))
    assert alone.rows == [
        Row(
            channel="google_search",
            monthly_usd=400,
            role="primary",
            why="Google Search gets all of it because people there are already looking for what you offer near you.",
            basis="benchmark",
        )
    ]
    assert allocate.NAMES == registry.LABELS  # the owner names the table speaks with are D76's
