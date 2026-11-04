import pytest

from agent import benchmark


def test_seed_is_idempotent_and_covers_every_category_channel(conn):
    n1 = benchmark.seed(conn)
    n2 = benchmark.seed(conn)
    assert n1 == n2 == len(benchmark.SEED)
    rows = conn.execute("SELECT category, channel, cpl_low, cpl_high, cpl_avg, confidence FROM benchmark").fetchall()
    assert len(rows) == len(benchmark.SEED)
    dent = next(r for r in rows if (r["category"], r["channel"]) == ("dentists", "google_search"))
    assert float(dent["cpl_avg"]) == 72.97
    assert float(dent["cpl_low"]) == pytest.approx(0.7 * 72.97, abs=0.01)
    assert float(dent["cpl_high"]) == pytest.approx(1.5 * 72.97, abs=0.01)
    assert dent["confidence"] == "low"
    cats = {r["category"] for r in rows}
    assert set(benchmark.CATEGORIES) <= cats
    # sub-category rows (home_services_plumbing, ...) refine one CATEGORIES key for one channel
    assert all(any(c == k or c.startswith(k + "_") for k in benchmark.CATEGORIES) for c in cats)


def test_worked_example_from_the_spec(conn):
    benchmark.seed(conn)
    est = benchmark.estimate(conn, "dentists", {"google_search": 1300, "meta": 700})
    assert (est.low, est.high) == (20, 40)
    assert est.confidence == "low"
    assert est.basis == "benchmark"
    assert {r["channel"] for r in est.rows} == {"google_search", "meta"}
    assert est.per_channel["google_search"] == (11.88, 25.45)
    assert est.owner_line() == "Estimated monthly leads: 20–40 (typical range for similar businesses, not a guarantee)"


def test_unknown_category_uses_the_fallback_rows(conn):
    benchmark.seed(conn)
    est = benchmark.estimate(conn, "taxidermy", {"google_search": 1000})
    assert est.rows[0]["category"] == "fallback"
    assert est.low == 10 and est.high == 20  # 1000 / (1.5*66.69) = 10.0 ; 1000 / (0.7*66.69) = 21.4 -> 20


def test_zero_or_missing_budgets_and_unknown_channels(conn):
    benchmark.seed(conn)
    est = benchmark.estimate(conn, "dentists", {"google_search": 0, "meta": 700, "tiktok": 500})
    assert est.per_channel.keys() == {"meta"}  # zero budgets and unknown channels are skipped
    assert (est.low, est.high) == (10, 15)  # 7.58 -> 10 ; 16.24 -> 15
    with pytest.raises(ValueError):
        benchmark.estimate(conn, "dentists", {})


def test_estimate_without_seed_is_an_error(conn):
    """Final review I5: every "the table lacks what an estimate needs" is `NotSeeded` -- the one error the plan tools
    turn into "try again"; anything else is a bug and propagates."""
    with pytest.raises(benchmark.NotSeeded):
        benchmark.estimate(conn, "dentists", {"google_search": 100})
    with pytest.raises(benchmark.NotSeeded):
        benchmark.funnel(conn, "dentists")
    with pytest.raises(benchmark.NotSeeded):
        benchmark.recommend_budget(conn, "dentists", None)
    assert issubclass(benchmark.NotSeeded, LookupError)


def test_funnel_and_recommended_budget(conn):
    benchmark.seed(conn)
    assert benchmark.funnel(conn, "dentists") == (0.5, 0.55, "benchmark dentists, confidence low")
    assert benchmark.funnel(conn, "automotive_repair") == (0.5, 0.5, "benchmark fallback, confidence low")
    assert benchmark.recommend_budget(conn, "dentists", None) == 2200  # 30 × 72.97 → $2,189 → $2,200
    assert benchmark.recommend_budget(conn, "dentists", 2000) == 2000
    assert benchmark.recommend_budget(conn, "automotive_repair", None) == 900  # 30 × 29.96 → $899 → $900
    assert benchmark.recommend_budget(conn, "fallback", 100) == 100  # the owner's number wins, however small


# ---------------------------------------------------------------- the channels' rows (spec 2026-09-29 Appendix B)

BING_COPIES = ("home_services", "dentists", "attorneys", "fallback")
FIELDS = ("cpl_low", "cpl_avg", "cpl_high", "lead_to_booking")


def _rows(conn) -> dict:
    return {
        (r["category"], r["channel"]): r
        for r in conn.execute(
            "SELECT category, channel, cpl_low, cpl_avg, cpl_high, lead_to_booking, source_url, confidence FROM benchmark"
        ).fetchall()
    }


def test_the_channels_rows_follow_appendix_b(conn):
    benchmark.seed(conn)
    rows = _rows(conn)
    for cat in BING_COPIES:  # Bing: no separate benchmark, the category's Google Search row
        bing, search = rows[(cat, "microsoft_search")], rows[(cat, "google_search")]
        assert [bing[k] for k in FIELDS] == [search[k] for k in FIELDS], cat
        assert bing["source_url"] == "no separate benchmark: seeded equal to the category's google_search row"
    assert ("real_estate", "microsoft_search") not in rows  # only the four categories Appendix B names
    nd = rows[("home_services", "nextdoor")]
    assert [float(nd[k]) for k in FIELDS] == [26, 50, 73, 0.25] and nd["confidence"] == "low"
    assert nd["source_url"].startswith("https://blog.nextdoor.com/how-nextdoor-helped-visiting-angels")
    metas = [r for (_c, ch), r in rows.items() if ch == "meta"]
    assert metas and all(float(r["lead_to_booking"]) == 0.13 for r in metas)
    # no published cost per lead: no row at all (P8), so the allocator never proposes them by the ratio
    assert not {ch for _c, ch in rows} & {"newsbreak", "reddit", "chatgpt_ads"}


def test_a_reseed_never_leaves_a_search_rate_empty(conn):
    """Fix round 1 (f): on an autocommit connection every statement of seed() is visible at once, so no statement
    may leave a Search row's funnel rates NULL on the way (funnel() and lookup() would raise for a reader then)."""
    benchmark.seed(conn)

    class Watch:  # runs seed()'s statements, and after each one counts the Search rows with an empty rate
        def __init__(self) -> None:
            self.worst = 0

        def execute(self, sql, params=None):
            out = conn.execute(sql, params)
            empty = conn.execute(
                "SELECT count(*) AS n FROM benchmark WHERE channel = 'google_search' AND category = ANY(%s) "
                "AND (lead_to_booking IS NULL OR booking_to_customer IS NULL)",
                (list(benchmark.FUNNEL),),
            ).fetchone()["n"]
            self.worst = max(self.worst, empty)
            return out

    watch = Watch()
    benchmark.seed(watch)
    assert watch.worst == 0
    assert benchmark.funnel(conn, "home_services") == (0.6, 0.6, "benchmark home_services, confidence low")


def test_lookup_reads_the_booking_rate_per_channel_with_search_as_the_fallback(conn):
    benchmark.seed(conn)
    assert benchmark.lookup(conn, "home_services", "meta")["lead_to_booking"] == 0.13
    assert benchmark.lookup(conn, "home_services", "nextdoor")["lead_to_booking"] == 0.25
    assert benchmark.lookup(conn, "dentists", "google_search")["lead_to_booking"] == 0.5
    assert benchmark.lookup(conn, "attorneys", "microsoft_search")["lead_to_booking"] == 0.4
    # a row without a rate of its own (Local Services) reads the category's Google Search rate ...
    assert benchmark.lookup(conn, "home_services", "lsa")["lead_to_booking"] == 0.6
    # ... and a category whose Search row has none reads the fallback's, as funnel() does
    assert benchmark.lookup(conn, "real_estate", "google_search")["lead_to_booking"] == 0.5
    # an unknown category takes the fallback category's row for the channel, rate included
    assert benchmark.lookup(conn, "taxidermy", "microsoft_search")["id"] == "fallback:microsoft_search"
    assert benchmark.lookup(conn, "dentists", "newsbreak") is None
    assert benchmark.lookup(conn, "dentists", "nextdoor") is None  # home services only; the fallback has none
