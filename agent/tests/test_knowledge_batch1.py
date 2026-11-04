"""The first batch of knowledge (capability layer K3; spec 2026-10-07 §6.1 and C3: dentists, home services with its
trades, attorneys × Google Search, NewsBreak; plan P7): the files it holds, the writing limits that keep a draft's
reading within its 1,500 tokens, and that every draft the batch serves reads its vertical's compliance, its trade and
Google Search -- and NewsBreak where the plan may run there -- before anything else. Pending files count here (the
batch is tested before its review). Only the batch's own files are held to the limits and to one source id per source:
a reviewed file K4 promotes into a vertical later joins it without turning these red (its crawled pages are numbered
web-1, web-2, ... per run, so two promoted files may cite web-1 for different pages)."""

import pytest

from agent import knowledge_index as ki
from agent.roles import strategist

TOPICS = ("compliance", "google-search", "offers", "newsbreak")
TRADES = ("plumbing", "hvac", "roofing", "electrical", "landscaping", "remodel")
BATCH = {"dentists": TOPICS, "attorneys": TOPICS, "home_services": (*TOPICS, *TRADES)}
BATCH_IDS = {f"{v}/{t}" for v, ts in BATCH.items() for t in ts}
KEY_POINTS_WORDS, TRADE_KEY_POINTS_WORDS = 110, 90
DO_NOT_SAY_LINES, DO_NOT_SAY_WORDS = 4, 14
CONFIDENCE = {"compliance": "medium", "google-search": "medium", "offers": "low", "newsbreak": "low"}
CHANNELS = {"compliance": (), "offers": (), "google-search": ("google_search",), "newsbreak": ("newsbreak",)}


def _files(vertical: str) -> dict[str, ki.Entry]:
    return {e.topic: e for e in ki.load() if e.vertical == vertical}


@pytest.mark.parametrize("vertical", sorted(BATCH))
def test_the_batch_holds_its_files(vertical):
    held = set(_files(vertical))
    assert set(BATCH[vertical]) <= held, sorted(set(BATCH[vertical]) - held)


@pytest.mark.parametrize("vertical", sorted(BATCH))
def test_each_file_keeps_the_writing_limits(vertical):
    files = _files(vertical)
    for topic in BATCH[vertical]:
        e = files.get(topic)
        assert e is not None, f"{vertical}/{topic} has no file"
        trade = topic in TRADES
        words = len([w for w in ki.key_points(e).split() if w != "-"])
        assert words <= (TRADE_KEY_POINTS_WORDS if trade else KEY_POINTS_WORDS), (e.id, words)
        dont = ki.do_not_say(e)
        assert len(dont) <= DO_NOT_SAY_LINES, e.id
        assert all(len(d.split()) - 1 <= DO_NOT_SAY_WORDS for d in dont), e.id
        assert e.confidence == ("medium" if trade else CONFIDENCE[topic]), e.id
        assert e.channels == (("google_search",) if trade else CHANNELS[topic]), e.id
        assert "stage:plan" in e.tags and (not trade or f"trade:{topic}" in e.tags), e.id


DRAFTS = [
    *[(v, None, nb) for v in ("dentists", "attorneys") for nb in (False, True)],
    *[("home_services", t, nb) for t in (*TRADES, None) for nb in (False, True)],
]


@pytest.mark.parametrize(("vertical", "trade", "newsbreak"), DRAFTS)
def test_a_draft_reads_compliance_its_trade_and_search_first(vertical, trade, newsbreak):
    chans = ("google_search", "newsbreak") if newsbreak else ("google_search",)
    got = ki.select(ki.load(), vertical, channels=chans, topics=strategist.topics(trade, chans))
    must = [f"{vertical}/{t}" for t in ("compliance", trade, "google-search", "newsbreak" if newsbreak else None) if t]
    assert got.ids[: len(must)] == must
    assert ki.tokens(got.text) <= ki.BUDGET_TOKENS


def test_one_source_id_names_one_source_across_the_batch():
    seen: dict[str, str] = {}
    for e in ki.load():
        # K4's promoted files number their crawled pages web-<n> per run: another file's web-1 is not the batch's
        if e.id not in BATCH_IDS:
            continue
        for s in e.sources:
            where = s.get("url") or s.get("doc")
            assert seen.setdefault(s["id"], where) == where, (e.id, s["id"])


def test_every_trade_the_strategist_reads_has_its_file():
    """Every trade the Strategist can detect has its `home_services/<trade>` file among the loaded files: a trade added
    to agent/trades.py must come with its file (K4), and a file added together with one leaves this green."""
    ids = {e.id for e in ki.load()}
    wanted = {f"home_services/{t}" for t in strategist.TRADES}
    assert wanted <= ids, sorted(wanted - ids)
