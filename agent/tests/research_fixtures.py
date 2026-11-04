"""Shared helpers for the Researcher's tests (capability layer K4): a signed-in dental business with its knowledge and
a plan, the businesses nearby as Places would answer, their pages, the model faked, a job queued, a stored run."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from agent import benchmark, business, crawl, db, llm, places
from agent.roles import queue, research_material
from agent.tests.fixtures import FakeGenerator, knowledge_fixture

BIZ = "biz_1"
LISTING = {
    "place_id": "ChIJself",
    "name": "Bright Smile Dental",
    "address": "500 University Ave, Palo Alto, CA 94301, USA",
    "lat": 37.4419,
    "lng": -122.143,
}
SECTIONS = {
    "consultation": {
        "business": {
            "category": "dentists",
            "service": "Invisalign",
            "services": ["Invisalign"],
            "location": LISTING["address"],
        },
        "objective": "bookings",
        "offer": None,
        "geo": {"radius_miles": 10, "center": "ChIJself"},
    },
    "ads": {
        "channels": [
            {"channel": "google_search", "monthly_usd": 1300, "themes": ["invisalign palo alto"], "negatives": ["free"]}
        ]
    },
}
# A stored run as research_run keeps it: one finding the guards passed and one they dropped (a rival named), a search
# to aim for kept and one dropped, and the digest's two sources
STORED = {
    "grounded": True,
    "findings": [
        {
            "text": "A lead from search ads costs dentists about $72.97 on average.",
            "source_ids": ["benchmark:dentists:google_search"],
            "confidence": "medium",
            "dropped": None,
        },
        {
            "text": "Rival Dental 2 offers free whitening.",
            "source_ids": ["page:1"],
            "confidence": "medium",
            "dropped": "named",
        },
    ],
    "suggested_themes": [
        {"text": "invisalign palo alto", "dropped": None},
        {"text": "rival dental 2", "dropped": "named"},
    ],
    "suggested_negatives": [{"text": "dental jobs", "dropped": None}],
    "offer_angles": [{"text": "Same-week appointments for cleanings.", "dropped": None}],
    "unknowns": [{"text": "How many people nearby search for Invisalign each month.", "dropped": None}],
}
# The digest as research_material.digest keeps it: a page is described from its place -- never by its title or its
# website -- and links to that place on Google Maps (page:1 is rival 2's, whose miles `nearby` gives as 2.0)
DIGEST = {
    "sha256": "0" * 64,
    "vertical": "dentists",
    "places": "called",
    "fetched_more": False,
    "counts": {},
    "sources": [
        {
            "id": "benchmark:dentists:google_search",
            "kind": "benchmark",
            "label": "Published cost-per-lead averages for dentists on Google Search (2026)",
            "url": benchmark.LOCALIQ_SEARCH,
        },
        {
            "id": "page:1",
            "kind": "page",
            "label": "The website of a business nearby, 2.0 miles away",
            "url": places.maps_url(None, "ChIJ2"),
        },
    ],
}


def seed(conn, biz: str = BIZ, *, claim_state: str = "claimed", plan_at=None, knowledge: dict | None = None) -> None:
    """The business (signed in unless `claim_state` says otherwise), its knowledge with its own Google listing, a plan
    version created at `plan_at` (now), and the benchmark rows."""
    conn.execute(
        "INSERT INTO business (id, claim_state) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (biz, claim_state)
    )
    business.add_knowledge(
        conn, biz, knowledge or {**knowledge_fixture().model_dump(), "listing": LISTING}, source="extract"
    )
    conn.execute(
        "INSERT INTO plan (id, business_id, version, json, status, created_at) VALUES (%s, %s, "
        "COALESCE((SELECT max(version) FROM plan WHERE business_id = %s), 0) + 1, %s, 'draft', %s)",
        (
            db.new_id("pl"),
            biz,
            biz,
            Jsonb({"sections": SECTIONS, "lines": {}, "rationale": "", "notes": []}),
            plan_at or datetime.now(UTC),
        ),
    )
    benchmark.seed(conn)


def nearby(n: int = 6, *, own: bool = True) -> list[places.Candidate]:
    """What the one Places call answers: the business itself first (left out), then `n` rivals with websites, rival k
    about k miles north."""
    out = (
        [
            places.Candidate(
                "ChIJself",
                "Bright Smile Dental",
                LISTING["address"],
                37.4419,
                -122.143,
                website="https://brightsmile.example",
            )
        ]
        if own
        else []
    )
    for k in range(1, n + 1):
        out.append(
            places.Candidate(
                f"ChIJ{k}",
                f"Rival Dental {k}, DDS",
                f"{k} Main St, Palo Alto, CA",
                37.4419 + 0.0145 * k,
                -122.143,
                website=f"https://rival{k}.example",
            )
        )
    return out


def fake_places(monkeypatch, found: list | None = None) -> list[dict]:
    """Places answers `found` (nearby() by default); the returned list records each call's query and options."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    calls: list[dict] = []

    def search(query, key, **kw):
        calls.append({"query": query, **kw})
        return list(found if found is not None else nearby())

    monkeypatch.setattr(places, "search", search)
    return calls


def page_title(url: str) -> str:
    """The title of a rival's page, as a small business writes it: its name and its town -- so a source label that
    takes the title shows a rival's name, and the tests see it. Rival k is `nearby`'s, at https://rivalk.example."""
    k = re.search(r"rival(\d+)\.example", url)
    return f"Rival Dental {k[1]}, DDS | Palo Alto" if k else "Corner Dental | Palo Alto"


def fake_pages(monkeypatch, pages: dict[str, str] | None = None) -> list[str]:
    """Each site's first page from `pages` by URL (every rival's by default), titled with its business's name
    (`page_title`); a URL not there cannot be read. Returns the URLs tried, in order."""
    tried: list[str] = []
    default = {
        f"https://rival{k}.example": f"Rival {k} offers free whitening with every cleaning." for k in range(1, 11)
    }
    served = default if pages is None else pages

    def fetch(url):
        tried.append(url)
        text = served.get(url)
        return crawl.Page(url=url, title=page_title(url), text=text) if text else None

    monkeypatch.setattr(research_material, "fetch_page", fetch)
    return tried


def no_guides(monkeypatch) -> None:
    """Admiry's reviewed guides (K3) left out: AGENT_KNOWLEDGE off, as conftest leaves it (said here so a test that
    reads only what it seeds says so). A test that wants guides takes K3's `knowledge_on` fixture."""
    monkeypatch.delenv("AGENT_KNOWLEDGE", raising=False)


def fake_llm(monkeypatch, **kw) -> FakeGenerator:
    gen = FakeGenerator(**kw)
    monkeypatch.setattr(llm, "generator", lambda settings=None, **_: gen)
    return gen


def job(conn, biz: str = BIZ, **brief) -> str:
    """A waiting research job for the business (the operator's, unless `trigger` says otherwise)."""
    return queue.enqueue(
        conn, business_id=biz, role="researcher", kind="business", brief={"trigger": "operator", **brief}
    )


def run_row(conn, biz: str = BIZ) -> dict | None:
    return conn.execute(
        "SELECT * FROM research_run WHERE business_id = %s ORDER BY created_at DESC LIMIT 1", (biz,)
    ).fetchone()


def failed(conn, biz: str = BIZ, *, reason: str = "llm") -> str:
    """A run that failed as research-run leaves one when the model could not answer (`llm`), finished now: it holds no
    owner's ask back, whatever it was built for. Its id."""
    rid = db.new_id("rr")
    conn.execute(
        "INSERT INTO research_run (id, business_id, vertical, trigger, brief, mode, status, dropped, finished_at) "
        "VALUES (%s, %s, 'dentists', 'auto', %s, 'on', 'failed', %s, now())",
        (rid, biz, Jsonb({"topics": ["competitors"], "note": None}), reason),
    )
    return rid


def delivered(
    conn, biz: str = BIZ, *, mode: str = "on", dropped: str | None = None, days_ago: int = 0, brief_json=None
) -> str:
    """A finished run stored as the Researcher stores one (STORED, DIGEST), the Critic's deliver unless `dropped`
    says otherwise; its id."""
    rid = db.new_id("rr")
    conn.execute(
        "INSERT INTO research_run (id, business_id, vertical, trigger, brief, mode, status, materials_digest, "
        "brief_json, review, score, verdict, confidence, dropped, rounds, calls, created_at, finished_at) VALUES "
        "(%s, %s, 'dentists', 'auto', %s, %s, 'done', %s, %s, '{}'::jsonb, 0.9, %s, %s, %s, 1, 2, "
        "now() - make_interval(days => %s), now() - make_interval(days => %s))",
        (
            rid,
            biz,
            Jsonb({"topics": ["competitors"], "note": None}),
            mode,
            Jsonb(DIGEST),
            Jsonb(brief_json or STORED),
            "deliver" if dropped is None else dropped,
            "high" if dropped is None else "low",
            dropped,
            days_ago,
            days_ago,
        ),
    )
    return rid
