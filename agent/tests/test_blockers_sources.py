"""R9 (spec 2026-10-12 §3.6): `blockers.admiry_page`'s reading of a page's `sections`, and whether its `source` is
the owner's own (`landing.OWNER_SOURCE`), are two independent questions -- checked here over hand-built rows of the
shapes a real page takes (a starter page or a site Admiry wrote, an assistant's own hosted upload, the owner's own
address), rather than through the fixtures of test_blockers_headline.py and test_checklist_fixes.py, which build a
page through the real create_page/publish_page flow and so never need to say `source` themselves. Review fix round
1 (Minor 5): an assistant's own hosted upload (`source` 'hosted', no `sections`) is `admiry_page` None too, exactly
like the owner's own page -- but it is not the owner's page (Message-match still blocks there; the fix on offer is
Check again, never Fix the headline, since Admiry cannot write into an assistant's own HTML)."""

from psycopg.types.json import Jsonb

from agent import blockers, db, landing
from agent.tests.fixtures import business_with_key


def test_admirys_own_page_by_source_agrees_with_blockers_admiry_page(conn):
    """R9 (2026-10-12): `blockers.admiry_page` reads `kind` from `sections` alone; only the literal `source`
    'owner' is the owner's own page -- the two never need to be derived from each other."""
    cases = [
        ("hosted", {"kind": "site"}, "site"),
        ("hosted", {"copy": {"headline": "x"}}, "starter"),
        ("admiry", {"copy": {"headline": "x"}}, "starter"),
        ("hosted", {}, None),  # an assistant's own upload, no sections: not Admiry's page, but not the owner's either
        ("owner", {}, None),
    ]
    for i, (source, sections, kind) in enumerate(cases):
        biz = f"biz_r9_{i}"  # page_one_live allows one published row per business: a fresh one for each case
        business_with_key(conn, biz_id=biz, claim_state="claimed")
        pid = db.new_id("pg")
        conn.execute(
            "INSERT INTO page (id, business_id, source, url, status, sections) VALUES (%s, %s, %s, %s, "
            "'published', %s)",
            (pid, biz, source, f"https://x{i}.example/", Jsonb(sections)),
        )
        page = landing.get(conn, pid)
        assert blockers.admiry_page(conn, page) == kind
        assert (page["source"] == landing.OWNER_SOURCE) == (source == "owner")  # only the literal owner row is
