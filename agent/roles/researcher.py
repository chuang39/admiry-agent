"""The Researcher (spec 2026-10-07-admiry-capability-layer-design §4 Researcher row, §4.1; capability layer K4). One
run per agent_job ("researcher", "business"), in research-run: code gathers the material (research_material: the
business, its trade, area and plan, the topics, published averages, Admiry's reviewed guides, the businesses nearby on
Google Maps and up to ten of their pages, untrusted), one model call writes a ResearchBrief -- findings citing the
material's sources by id, searches to aim for and to keep out, offer angles, unknowns -- code checks every sentence
(a source the material holds; every number in the words of the sources it cites -- an offer angle's in what the
material says of the business itself, an unknown's in any source's words; no business nearby named; the owner guards of
roles.guard) and every search, and the Critic grades it by the research rubric (contracts.RESEARCH). At most two rounds
(§4.1): revise reruns with the issues, fetch_more first reads more (research_material.more). One research_run row per
job, every round's verdict in it.

Who reads it (founder ruling R-K4, 2026-10-08): with AGENT_ROLES=validate, Admiry's team only (`team_view`,
agent.ops research-show, a platform key's get_research); an owner -- and the Strategist drafting the next plan
version -- only through `owner_view`: AGENT_ROLES on, a run built while it was on, done, the Critic's deliver, and the
sentences the guards passed. A run short of deliver after two rounds stays with the team (plan decision D4). Nothing
is applied: research is material.

A business nearby is described, never named (plan decision D5): a sentence or a search that names one -- by a form of
its Google Maps name or of its page's title (research_material's rule: a person's name, a part of the name, the name
without its trade's last words; a name of words any business of the trade here has only written as a proper noun), or
by the host of its website written as a host -- is dropped (`named`), and nothing stored holds a name or a host: the
words of a dropped sentence, the Critic's notes and the feedback the second round read are kept with every name and
every host replaced (research_material.mask_names, mask_hosts). A name the forms do not know is the Critic's to send
back (the research rubric's serious `unnamed` check). Research never reads a page Admiry hosts and never mentions
Admiry -- a sentence or a search that does is dropped (`named`) and fed back -- so it cannot say whether a business
nearby uses Admiry (founder ruling 2026-10-09, the B0 R3 boundary). Why a run did not reach an owner is its `dropped`: llm
(the model could not answer: research-run tries the job again), material (what the business stored could not be read:
not tried again), review, revise, fetch_more (after two rounds), empty, error, stale."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from .. import audit, business, config, consult, copy, db, llm, models
from . import base, contracts, critic, guard, queue, research_material

log = logging.getLogger(__name__)

ROLE, KIND = "researcher", "business"
BUILDING, DONE, FAILED = "building", "done", "failed"
ASKS = ("owner", "operator")  # who may ask for research (`request`); the sweep's own jobs are queue.AUTO's
TRIGGERS = (queue.AUTO, *ASKS)
EFFORT = "medium"  # the Researcher's reasoning effort (plan D10: a constant, as the caps below)
MAX_FINDINGS, MAX_SEARCHES, MAX_ANGLES, MAX_UNKNOWNS = 8, 10, 5, 5
SEARCH_CHARS = 80
HIGH_SCORE = 0.85  # a delivered run at or above this score is high confidence; below, medium; not delivered, low
BATCH = 10  # research jobs a run takes at most, one at a time (runner._research)
RUN_SECONDS = 1200  # a shard starts no new research after this long (the rest wait an hour; airflow's hourly DAG)
MAX_RUNS_PER_DAY = 50  # research runs started a UTC day, all businesses (plan D10, D15)
STALE_AFTER = "2 hours"  # a run building longer than this was left by a dead runner
RETENTION_DAYS = 396  # 13 months, as the Analyst's looks
UNSOURCED, NAMED = "unsourced", "named"
NO_MODEL, NO_MATERIAL = "llm", "material"  # why a run failed: the model could not answer; the data could not be read
OWN_WORDS = ("page", "place")  # other businesses' own pages and listings: a finding on them alone is never high
GUARD_WORDS = {
    guard.EMPTY: "it was empty",
    guard.UNGROUNDED: "it said the material gave nothing true to say",
    guard.LONG: "it was too long",
    guard.LINK: "it carried a link, an address, a phone number or markup",
    guard.NUMBER: "it stated a number its sources do not hold",
    guard.JARGON: "it used advertising jargon",
    UNSOURCED: "it cites no source the material holds",
    NAMED: "it named a business nearby",
}
OWN_NUMBER = "it stated a number the business's own prices and offers do not hold"  # an offer angle's number guard
# what a sentence or a search that mentions Admiry is told (founder ruling 2026-10-09): dropped as NAMED
ADMIRY_WORDS = "it named Admiry, which research never mentions"
# a search is refused what a sentence is refused (guard._LINK: a link, an address, a phone number, markup) and a bare @
_LINKISH = re.compile(f"{guard._LINK.pattern}|@")


def _capped(confidence: str, ids: list[str], sources: dict[str, dict]) -> str:
    """A finding resting only on businesses' own pages or listings is at most medium: their claim, not a fact."""
    if confidence == "high" and all(sources[i]["kind"] in OWN_WORDS for i in ids):
        return "medium"
    return confidence


def _kept(stored: dict | None) -> bool:
    return bool(stored) and any(not f["dropped"] for f in stored["findings"])


def _unnamed(value, names: research_material.Names, hosts: tuple[str, ...], *, strict: bool = False):
    """`value` -- text, or lists and objects of it -- with every host of the website of a business nearby (mask_hosts:
    first, so a host that holds a name goes whole) and every name of one (mask_names: each form the guard finds; with
    `strict`, however a name that is no description is written) in any text replaced. What is stored of a run goes
    through this, and the feedback a second round reads: names live in memory for one run."""
    if isinstance(value, str):
        return research_material.mask_names(research_material.mask_hosts(value, hosts), names, strict=strict)
    if isinstance(value, list):
        return [_unnamed(v, names, hosts, strict=strict) for v in value]
    if isinstance(value, dict):
        return {k: _unnamed(v, names, hosts, strict=strict) for k, v in value.items()}
    return value


KINDS = ("findings", "suggested_themes", "suggested_negatives", "offer_angles", "unknowns")  # what the research holds


def _stored(stored: dict, names: research_material.Names, hosts: tuple[str, ...]) -> dict:
    """The research as research_run keeps it: what the guards passed as it is -- it names no one by their rule, and an
    owner reads it so -- and what they dropped with every host and name of a business nearby masked however it is
    written there (`_unnamed`, strict: the re-review's N1)."""
    return {
        **stored,
        **{
            kind: [
                {**x, "text": _unnamed(x["text"], names, hosts, strict=True)} if x.get("dropped") else x
                for x in stored.get(kind, [])
            ]
            for kind in KINDS
        },
    }


def _offered(g: research_material.Gathered) -> str:
    """What the business itself says it offers and charges, as the text an offer angle's numbers are held to --
    guard.known takes every numeral of the text it is given as held, so the text is no more than that: each service's
    name and price hint, the prices and offers it states (`pricing_hints`; `offers`, their text and their terms), its
    reasons to be chosen (`usp`) and the offer its plan carries. Never its hours, phone, address, budget or goals, what
    a customer is worth, the date an offer ends, or what a business nearby says. What is stored can be any shape: only
    text is read, and only out of a list where a list is meant."""
    knowledge = g.base.get("business")
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    plan = g.base.get("plan")

    def listed(key: str) -> list:
        found = knowledge.get(key)
        return found if isinstance(found, list) else []

    facts = {
        "services": [[s.get("name"), s.get("price_hint")] for s in listed("services") if isinstance(s, dict)],
        "prices": [p for p in listed("pricing_hints") if isinstance(p, str)],
        "offers": [[o.get("text"), o.get("terms")] for o in listed("offers") if isinstance(o, dict)],
        "reasons": [u for u in listed("usp") if isinstance(u, str)],
        "plan_offer": plan.get("offer") if isinstance(plan, dict) else None,
    }
    return json.dumps(facts, ensure_ascii=False, default=str)


def _guarded(written: models.ResearchBrief, g: research_material.Gathered) -> tuple[dict, list[str]]:
    """The research as stored -- each finding with the source ids the material holds, its confidence (capped) and its
    guard reason; each search, offer angle and unknown with its own -- and the feedback a second round reads. Anything
    naming a business nearby is dropped (NAMED) -- by a form of its name or of its page's title (research_material:
    `names`, `names_in`; in a search only a form that names it wherever it is written), or by the host of its website
    as a host is written (research_material.host_in: a rival's address is named, whatever else it is, a link too); so
    is anything that mentions Admiry (research_material.admiry_in: research never says whether a business nearby uses
    Admiry); a finding citing no source the material holds is dropped (UNSOURCED).
    A sentence's numbers are held to words, never to the ids or the structure the model reads them in: a finding's to
    the words of the sources it cites, the counts the material gives (businesses shown or found, pages read, the
    radius) and how many businesses nearby its own sources are from (research_material.Gathered.businesses: "two of the
    five nearby practices", the prompt's own phrasing); an offer angle's -- the prompt defines it as something the
    business itself offers -- to what the business says it offers and charges (`_offered`), no counts and no other
    business's page; an unknown's to the words of the sources, but for the places' (a place source's words are its
    distance, which is no count of anything), and the counts. The words are the model's as written: what is stored of
    them goes through `_unnamed` (_finish)."""
    feedback: list[str] = []
    names, hosts = g.names, g.hosts
    counts = g.counts()
    offered = _offered(g)
    said = json.dumps([s["text"] for s in g.sources.values() if s["kind"] != "place"], ensure_ascii=False)

    def sentence(
        words: str,
        where: str,
        against: str,
        *,
        extra: Iterable[Decimal],
        sentences: int,
        chars: int,
        number: str = GUARD_WORDS[guard.NUMBER],
    ) -> str | None:
        if research_material.names_in(words, names) or research_material.host_in(words, hosts):
            feedback.append(f"{where}: {GUARD_WORDS[NAMED]}; describe it instead")  # never the name itself (N1)
            return NAMED
        if research_material.admiry_in(words):
            feedback.append(f"{where}: {ADMIRY_WORDS}")
            return NAMED
        why = guard.check(words, written.grounded, against, extra=extra, max_sentences=sentences, max_chars=chars)
        if why:
            feedback.append(f"{where}: {number if why == guard.NUMBER else GUARD_WORDS[why]}")
        return why

    def search(words: str, where: str) -> dict:
        text = " ".join((words or "").lower().split())
        told = None
        if not text:
            why = guard.EMPTY
        elif len(text) > SEARCH_CHARS:
            why = guard.LONG
        elif research_material.host_in(text, hosts):  # a rival's website is named before it is a link, as in a sentence
            why = NAMED
        elif research_material.admiry_in(text):
            why, told = NAMED, ADMIRY_WORDS
        elif _LINKISH.search(text):
            why = guard.LINK
        elif research_material.names_in(text, names, search=True):
            why = NAMED
        elif copy.jargon(text):
            why = guard.JARGON
        else:
            why = None
        if why:
            shown = _unnamed(text, names, hosts, strict=True) if why == NAMED and told is None else text
            feedback.append(f"{where} {shown!r}: {told or GUARD_WORDS[why]}")  # a name or host it named, masked
        return {"text": text, "dropped": why}

    def unique(items: list[dict]) -> list[dict]:
        seen: set[str] = set()
        out = []
        for it in items:
            if it["text"] not in seen:
                seen.add(it["text"])
                out.append(it)
        return out

    findings = []
    for n, f in enumerate(written.findings[:MAX_FINDINGS], 1):
        ids = list(dict.fromkeys(i.strip() for i in f.source_ids if i.strip() in g.sources))
        strangers = [i for i in f.source_ids if i.strip() not in g.sources]
        if strangers:
            feedback.append(f"finding {n}: {', '.join(repr(i) for i in strangers)} is not one of the sources")
        if not ids:
            feedback.append(f"finding {n}: {GUARD_WORDS[UNSOURCED]}")
            findings.append({"text": f.text, "source_ids": [], "confidence": f.confidence, "dropped": UNSOURCED})
            continue
        cited = json.dumps([g.sources[i]["text"] for i in ids], ensure_ascii=False)
        nearby = g.businesses(ids)
        own_counts = counts | ({Decimal(nearby)} if nearby else set())
        findings.append(
            {
                "text": f.text,
                "source_ids": ids,
                "confidence": _capped(f.confidence, ids, g.sources),
                "dropped": sentence(f.text, f"finding {n}", cited, extra=own_counts, sentences=2, chars=300),
            }
        )
    stored = {
        "grounded": written.grounded,
        "findings": findings,
        "suggested_themes": unique([search(s, "search to aim for") for s in written.suggested_themes[:MAX_SEARCHES]]),
        "suggested_negatives": unique(
            [search(s, "search to keep out") for s in written.suggested_negatives[:MAX_SEARCHES]]
        ),
        "offer_angles": [
            {
                "text": a,
                "dropped": sentence(
                    a, f"offer angle {n}", offered, extra=(), sentences=2, chars=200, number=OWN_NUMBER
                ),
            }
            for n, a in enumerate(written.offer_angles[:MAX_ANGLES], 1)
        ],
        "unknowns": [
            {"text": u, "dropped": sentence(u, f"unknown {n}", said, extra=counts, sentences=1, chars=200)}
            for n, u in enumerate(written.unknowns[:MAX_UNKNOWNS], 1)
        ],
    }
    if not _kept(stored):
        feedback.append("no finding could be shown: cite the sources by id and state only their numbers")
    return stored, feedback


def _fetched(g: research_material.Gathered) -> dict:
    return {
        "shown": g.shown,
        "pages": len(g.pages),
        "knowledge": len((g.knowledge or {}).get("ids", [])),
        "fetched_more": g.fetched_more,
    }


def _call(conn: psycopg.Connection, job: dict, row: dict, rnd: int, call: str) -> None:
    """One model call's audit row (job.researcher), written before the call, counted toward the business's background
    day (runner.ROLE_RUNS_PER_DAY): no business on the row -- research leaves no trace on an owner surface (R-K4) --
    the business in its args, where runner.runs_today counts it."""
    audit.record_job(
        conn,
        job="research-run",
        tool="job.researcher",
        business_id=None,
        args={"business": job["business_id"], "run": row["id"], "round": rnd, "call": call},
        result={},
    )


def _start(conn: psycopg.Connection, settings: config.Settings, job: dict, knowledge: dict) -> dict:
    """The job's row, building: a new one, or the row of the job's earlier attempt started again (a busy model's) --
    asked what the job holds now (`enqueue` replaces a waiting job's brief), not what the earlier attempt was."""
    brief = job["brief"] or {}
    topics = [t for t in dict.fromkeys(brief.get("topics") or ()) if t in research_material.TOPICS]
    asked = {"topics": topics or list(research_material.TOPICS), "note": brief.get("note")}
    trigger = brief.get("trigger") if brief.get("trigger") in TRIGGERS else "auto"
    return conn.execute(
        "INSERT INTO research_run (id, job_id, business_id, vertical, trigger, brief, mode, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'building') ON CONFLICT (job_id) DO UPDATE SET status = 'building', "
        "mode = EXCLUDED.mode, vertical = EXCLUDED.vertical, trigger = EXCLUDED.trigger, brief = EXCLUDED.brief, "
        "dropped = NULL, started_at = now(), finished_at = NULL RETURNING *",
        (
            db.new_id("rr"),
            job["id"],
            job["business_id"],
            consult.tree_for(knowledge.get("category") or "fallback"),
            trigger,
            Jsonb(asked),
            settings.roles_mode,
        ),
    ).fetchone()


def _finish(
    conn: psycopg.Connection,
    row: dict,
    g: research_material.Gathered | None,
    *,
    status: str,
    dropped: str | None,
    stored: dict | None = None,
    judged: critic.Judged | None = None,
    rounds_log: list[dict] | None = None,
    calls: int = 0,
    rounds: int = 0,
) -> dict:
    """The row as the run ended. `g` is None when nothing was gathered (no model to ask: gathering spends the day's
    Places lookup): the digest then records nothing. Every name of a business nearby, and every host of a website of
    one, is masked out of the words kept: what the guards dropped and every round's notes (the guards' feedback, the
    Critic's) however a name is written there (`_stored`, `_unnamed` strict); what they passed is kept as it is."""
    names, hosts = (g.names, g.hosts) if g is not None else (research_material.Names(), ())
    final = (
        None
        if judged is None
        else {
            "grades": judged.grades,
            "score": judged.score,
            "verdict": judged.verdict,
            "issues": judged.issues,
            "missing": judged.review.missing,
        }
    )
    score = judged.score if judged else None
    if status != DONE:
        confidence = None
    elif dropped is not None:
        confidence = "low"
    else:
        confidence = "high" if score is not None and score >= HIGH_SCORE else "medium"
    return conn.execute(
        "UPDATE research_run SET status = %s, materials_digest = %s, brief_json = %s, review = %s, score = %s, "
        "verdict = %s, confidence = %s, dropped = %s, rounds = %s, calls = %s, finished_at = now() WHERE id = %s "
        "RETURNING *",
        (
            status,
            Jsonb(research_material.digest(g) if g is not None else {}),
            Jsonb(_stored(stored, names, hosts)) if stored is not None else None,
            Jsonb(_unnamed({"rounds": rounds_log or [], "final": final}, names, hosts, strict=True)),
            score,
            judged.verdict if judged else None,
            confidence,
            dropped,
            rounds,
            calls,
            row["id"],
        ),
    ).fetchone()


def _unreadable(
    conn: psycopg.Connection, row: dict, g: research_material.Gathered | None, err: Exception, **kept
) -> dict:
    """The run failed because the material could not be gathered (`material`): what a business stored can be any shape,
    and gathering can raise on one after the day's Places lookup is spent. Logged by the error's type alone, never its
    words (they can hold what was stored or what a stranger's page said). The job is not tried again for it."""
    log.warning("researcher: the material for %s could not be gathered (%s)", row["business_id"], type(err).__name__)
    return _finish(conn, row, g, status=FAILED, dropped=NO_MATERIAL, **kept)


def _build(conn: psycopg.Connection, settings: config.Settings, job: dict, row: dict) -> dict:
    """The confidence loop (§4.1): at most contracts.RESEARCHER.max_rounds rounds, a second when the Critic does not
    deliver or no finding passed the guards -- after reading more when it asked for more (fetch_more) -- with the
    guards' feedback, its issues and what it missed. Each round's verdict and what it read go into `review`. The model
    is built before anything is gathered: a run that cannot call one must not spend the day's Places lookup nor crawl
    a stranger's site (each attempt of the job would)."""
    try:
        gen = llm.generator(settings, effort=EFFORT)
    except llm.LLMError as e:
        log.warning("researcher: no model for %s: %s", job["business_id"], e)
        return _finish(conn, row, None, status=FAILED, dropped=NO_MODEL)
    try:
        g = research_material.gather(conn, settings, job["business_id"], row["brief"])
    except Exception as e:  # noqa: BLE001 - a business's stored data can be any shape; see _unreadable
        return _unreadable(conn, row, None, e)
    stored: dict | None = None
    judged: critic.Judged | None = None
    feedback: list[str] | None = None
    rounds_log: list[dict] = []
    calls = rounds = 0
    for rnd in range(1, contracts.RESEARCHER.max_rounds + 1):
        calls += 1
        _call(conn, job, row, rnd, "write")
        try:
            written = gen.write_research(g.material, list(g.pages), feedback)
        except llm.LLMError as e:
            log.warning("researcher: no research for %s: %s", job["business_id"], e)
            break
        rounds = rnd
        stored, problems = _guarded(written, g)
        calls += 1
        _call(conn, job, row, rnd, "review")
        try:
            judged = critic.judge_research(settings, g.material, list(g.pages), written.model_dump())
        except llm.LLMError as e:
            log.warning("researcher: no review for %s: %s", job["business_id"], e)
            judged = None
            rounds_log.append(
                {"round": rnd, "verdict": None, "score": None, "problems": problems, "fetched": _fetched(g)}
            )
            break
        rounds_log.append(
            {
                "round": rnd,
                "verdict": judged.verdict,
                "score": judged.score,
                "grades": judged.grades,
                "issues": judged.issues,
                "missing": judged.review.missing,
                "problems": problems,
                "fetched": _fetched(g),
            }
        )
        if (judged.verdict == base.DELIVER and _kept(stored)) or rnd == contracts.RESEARCHER.max_rounds:
            break
        if judged.verdict == base.FETCH_MORE:
            try:
                research_material.more(g)
            except Exception as e:  # noqa: BLE001 - as gather above: round one's research stays, the run fails
                return _unreadable(
                    conn, row, g, e, stored=stored, judged=judged, rounds_log=rounds_log, calls=calls, rounds=rounds
                )
        notes = [
            *problems,
            *(f"{i['dimension']}: {i['text']}" for i in judged.issues),
            *(f"missing: {w}" for w in judged.review.missing),
        ]
        feedback = _unnamed(notes, g.names, g.hosts, strict=True)  # the second round reads no name (the re-review's N1)
    if stored is None:
        return _finish(conn, row, g, status=FAILED, dropped=NO_MODEL, rounds_log=rounds_log, calls=calls, rounds=rounds)
    if judged is None:
        dropped = "review"
    elif judged.verdict != base.DELIVER:
        dropped = judged.verdict
    else:
        dropped = None if _kept(stored) else "empty"
    return _finish(
        conn,
        row,
        g,
        status=DONE,
        dropped=dropped,
        stored=stored,
        judged=judged,
        rounds_log=rounds_log,
        calls=calls,
        rounds=rounds,
    )


def run_job(conn: psycopg.Connection, settings: config.Settings, job: dict) -> tuple[str, dict]:
    """research-run's handler for ("researcher", "business"): skipped when the business's background day has no room
    for a whole run (runner.ROLE_RUNS_PER_DAY) or Admiry knows nothing of it yet; failed when the model could not
    answer (research-run tries it again at the next hour, runner.ATTEMPTS) or when what the business stored could not
    be read (`material`: final, the reason says so); else done, with the run's id, verdict, score and confidence. Any
    other error fails the row (`error`) and propagates: research-run fails the job."""
    from .runner import ROLE_RUNS_PER_DAY, runs_today  # runner imports this module for its HANDLERS

    biz = job["business_id"]
    if runs_today(conn, biz) + contracts.RESEARCHER.max_calls > ROLE_RUNS_PER_DAY:
        return queue.SKIPPED, {"reason": "capped"}
    known = business.latest_knowledge(conn, biz)
    if known is None:
        return queue.SKIPPED, {"reason": "no knowledge"}
    row = _start(conn, settings, job, known[1])
    try:
        row = _build(conn, settings, job, row)
    except Exception:
        conn.execute(
            "UPDATE research_run SET status = 'failed', dropped = 'error', finished_at = now() WHERE id = %s",
            (row["id"],),
        )
        raise
    if row["status"] == FAILED:  # only the model's silence is worth another attempt (runner._handle)
        reason = queue.MODEL_UNAVAILABLE if row["dropped"] == NO_MODEL else row["dropped"]
        return queue.FAILED, {"run": row["id"], "reason": reason}
    return queue.DONE, {
        "run": row["id"],
        "verdict": row["verdict"],
        "score": row["score"],
        "confidence": row["confidence"],
    }


AUTO_EVERY_DAYS = 30  # founder ruling R-K4: not again for a business within 30 days of research that stands for them
RETRY_DAYS = 3  # automatic research that failed but not for its material is tried again after this many days ...
RETRY_LIMIT = 3  # ... and not after this many such failures within AUTO_EVERY_DAYS (the final review's M1)
REQUEST_GAP = "24 hours"  # plan D3: an owner asks at most once a day
SWEEP_LIMIT = 20  # businesses one hourly sweep queues per shard
ASKED_RESERVE = 10  # of a day's MAX_RUNS_PER_DAY, the last runs only a job somebody asked for may take


@dataclass(frozen=True)
class Requested:
    """What an ask did: whether it queued a job, and the research's state (queued, running, recent, draft, unread,
    off)."""

    queued: bool
    status: str
    job_id: str | None


def due(conn: psycopg.Connection, mode: str, i: int = 0, n: int = 1, *, limit: int = SWEEP_LIMIT) -> list[str]:
    """The businesses automatic research is due for (R-K4; plan D1; the final review's M1, M3) with the roles on `mode`:
    active, signed in (not a draft), with knowledge and a plan version built within AUTO_EVERY_DAYS, and no research
    within AUTO_EVERY_DAYS that stands for them -- a run done (whoever asked for it, delivered or not) that `mode` can
    stand for: with the roles on only one built while they were on, so the validate week's research stays the team's
    and its businesses are due again; on validate (or off, when nothing is swept) any -- nor a run that failed for its
    material (`material`: the same data would fail again) nor one still building. A run that failed otherwise (the
    model three times, a dead runner, an error) holds the business RETRY_DAYS, and RETRY_LIMIT of them hold it for
    AUTO_EVERY_DAYS. None waiting or running either, and none skipped today (a skipped job leaves no run behind, so the
    sweep would queue it again every hour). So: the first plan's research, then again only for a business whose plan
    changed after the 30 days. In shard i of n, a business never researched first, then the oldest."""
    from ..jobs import shard_sql  # jobs imports runner, which imports this module

    rows = conn.execute(
        "SELECT b.id FROM business b WHERE b.status = 'active' AND b.claim_state <> 'draft' "
        "AND EXISTS (SELECT 1 FROM knowledge k WHERE k.business_id = b.id) "
        "AND EXISTS (SELECT 1 FROM plan p WHERE p.business_id = b.id "
        "AND p.created_at >= now() - make_interval(days => %(days)s)) "
        "AND NOT EXISTS (SELECT 1 FROM research_run r WHERE r.business_id = b.id "
        "AND r.created_at >= now() - make_interval(days => %(days)s) AND (r.status = 'building' "
        "OR (r.status = 'done' AND (r.mode = 'on' OR NOT %(on)s)) OR (r.status = 'failed' AND r.dropped = %(material)s))) "
        "AND NOT EXISTS (SELECT 1 FROM research_run r WHERE r.business_id = b.id AND r.status = 'failed' "
        "AND r.dropped IS DISTINCT FROM %(material)s AND r.created_at >= now() - make_interval(days => %(retry)s)) "
        "AND (SELECT count(*) FROM research_run r WHERE r.business_id = b.id AND r.status = 'failed' "
        "AND r.dropped IS DISTINCT FROM %(material)s AND r.created_at >= now() - make_interval(days => %(days)s)) "
        "< %(retries)s "
        "AND NOT EXISTS (SELECT 1 FROM agent_job j WHERE j.business_id = b.id AND j.role = %(role)s "
        "AND (j.status IN ('queued', 'running') "
        "OR (j.status = 'skipped' AND j.finished_at >= date_trunc('day', now())))) "
        f"AND {shard_sql('b.id')} "
        "ORDER BY EXISTS (SELECT 1 FROM research_run r WHERE r.business_id = b.id), b.created_at, b.id "
        "LIMIT %(limit)s",
        {
            "days": AUTO_EVERY_DAYS,
            "on": mode == "on",
            "material": NO_MATERIAL,
            "retry": RETRY_DAYS,
            "retries": RETRY_LIMIT,
            "role": ROLE,
            "i": i,
            "n": n,
            "limit": limit,
        },
    ).fetchall()
    return [r["id"] for r in rows]


def sweep(conn: psycopg.Connection, mode: str, i: int, n: int) -> int:
    """research-run's step with the roles validate or on (`mode`): one waiting ("researcher", "business") job for each
    business automatic research is due for (`due`), every topic, the system's own (queue.AUTO: research-run takes the
    jobs somebody asked for before them). A job already waiting for the business is left exactly as it is and not
    counted: an owner's ask can land between `due` and the insert, and its topics and question are not the sweep's to
    replace. How many jobs were queued."""
    queued = 0
    for biz in due(conn, mode, i, n):
        made = queue.enqueue_unless_waiting(
            conn,
            business_id=biz,
            role=ROLE,
            kind=KIND,
            brief={
                "trigger": queue.AUTO,
                "topics": list(research_material.TOPICS),
                "note": None,
                "by": "job:research-run",
            },
        )
        if made is not None:
            queued += 1
    return queued


def waiting_job(conn: psycopg.Connection, business_id: str) -> dict | None:
    """The business's research job that waits or runs (the newest, if two do) as {"id", "status", "trigger"} -- the
    trigger is its brief's: auto, owner or operator -- or None when no research is in hand. What an owner's ask is
    judged by first, and what get_research says of the business."""
    return conn.execute(
        "SELECT id, status, brief->>'trigger' AS trigger FROM agent_job WHERE business_id = %s AND role = %s "
        "AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1",
        (business_id, ROLE),
    ).fetchone()


def recently_done(conn: psycopg.Connection, business_id: str) -> bool:
    """Whether a research run of the business finished within REQUEST_GAP (plan D3: an owner asks at most once a day).
    A run that failed does not count: the owner may ask again."""
    recent = conn.execute(
        "SELECT 1 FROM research_run WHERE business_id = %s AND status = 'done' "
        f"AND finished_at >= now() - interval '{REQUEST_GAP}' LIMIT 1",
        (business_id,),
    ).fetchone()
    return recent is not None


def why_not_ask(conn: psycopg.Connection, business_id: str) -> str | None:
    """Why an owner's ask for research cannot be taken for this business, or None when nothing about the business stops
    it: `draft` (its owner has not signed in; plan D2) or `unread` (Admiry has no knowledge of it, so research-run
    would skip the job -- `no knowledge`, as `due` leaves such a business out -- and the ask's promise would never come
    true). Signing in comes first. The one place that decides it: `request` judges an owner's ask by it, before it looks
    at the research in hand, and get_research offers the ask by it."""
    owned = business.row(conn, business_id)
    if owned is not None and owned["claim_state"] == "draft":
        return "draft"
    known = conn.execute("SELECT 1 FROM knowledge WHERE business_id = %s LIMIT 1", (business_id,)).fetchone()
    return None if known is not None else "unread"


def request(
    conn: psycopg.Connection,
    settings: config.Settings,
    business_id: str,
    *,
    trigger: str,
    topics: list[str] | None = None,
    note: str | None = None,
    by: str | None = None,
) -> Requested:
    """Research asked for by the owner's assistant (`owner`) or the operator (`operator`; R-K4); any other trigger is a
    ValueError (a word that is neither must not pass for the operator's, which skips every wait). Nothing with the
    roles off. The owner's ask queues nothing for a business that has not signed in (`draft`; plan D2) or that Admiry
    has not read (`unread`): `why_not_ask`. While research somebody asked for waits for the business or any research
    runs for it, or within REQUEST_GAP of the last finished run (plan D3), it queues nothing either. Research the sweep
    queued that still waits is not the owner's question researched: the ask puts its topics and note in that job, which
    keeps its place in line (`queued`). The operator's ask skips every wait (a waiting job takes the operator's brief; a
    running one gets a new job behind it) and never the caps, and asks for a business whatever Admiry knows of it: the
    skipped job is how the operator reads why. Inside the caller's transaction."""
    if trigger not in ASKS:
        raise ValueError(f"unknown trigger {trigger!r}: research is asked for by {' or '.join(ASKS)}")
    if settings.roles_mode == "off":
        return Requested(False, "off", None)
    if trigger == "owner":
        stopped = why_not_ask(conn, business_id)
        if stopped is not None:
            return Requested(False, stopped, None)
        busy = waiting_job(conn, business_id)
        if busy is not None and not (busy["status"] == queue.QUEUED and busy["trigger"] == queue.AUTO):
            return Requested(False, busy["status"], busy["id"])
        if recently_done(conn, business_id):
            return Requested(False, "recent", None)
    brief = {"trigger": trigger, "topics": list(topics or research_material.TOPICS), "note": note, "by": by}
    return Requested(True, "queued", queue.enqueue(conn, business_id=business_id, role=ROLE, kind=KIND, brief=brief))


def run_now(conn: psycopg.Connection, settings: config.Settings, job_id: str) -> dict | None:
    """agent.ops research --now (dev only; the command refuses on prod): a waiting job run at once by research-run's
    own handler -- its caps included -- and finished with what the handler returned, as research-run finishes one (a
    model that could not answer is not tried again at the next hour: the failed run is the answer, and the operator
    asks again). The run's row; None when it did not run: the job was not waiting, the day's research runs are used up
    (MAX_RUNS_PER_DAY, which research-run holds too) or the business has research running already (a second run at once
    would call Google and read the same sites again) -- the job then waits for research-run as it was -- or the handler
    skipped it (the business's day, nothing known of it) before a run began."""
    if runs_today_all(conn) >= MAX_RUNS_PER_DAY:
        return None
    job = conn.execute(
        "UPDATE agent_job j SET status = 'running', rounds = rounds + 1, started_at = now(), updated_at = now() "
        "WHERE j.id = %s AND j.status = 'queued' AND NOT EXISTS (SELECT 1 FROM agent_job r WHERE r.status = 'running' "
        "AND r.role = j.role AND r.business_id IS NOT DISTINCT FROM j.business_id) "
        "RETURNING j.id, j.business_id, j.role, j.kind, j.brief, j.rounds, j.created_at",
        (job_id,),
    ).fetchone()
    if job is None:
        return None
    try:
        status, result = run_job(conn, settings, job)
    except Exception as e:
        queue.finish(conn, job["id"], status=queue.FAILED, error=f"{type(e).__name__}: {e}"[:500])
        raise
    queue.finish(conn, job["id"], status=status, result=result)
    return conn.execute("SELECT * FROM research_run WHERE job_id = %s", (job_id,)).fetchone()


def owner_view(settings: config.Settings, row: dict | None) -> dict | None:
    """What an owner -- and the Strategist -- may read of a run (R-K4, exactly the Analyst's rule): with AGENT_ROLES on,
    a run built while it was on, done, the Critic's deliver, with at least one finding the guards passed; of it each
    finding the guards passed with its sources (id, kind, label, link), the searches, offer angles and unknowns they
    passed, the day, the topics and the confidence. None otherwise. A source is described from its place and links to
    it on Google Maps (research_material): no business nearby is named by anything here."""
    if row is None or row["status"] != DONE or not row["brief_json"]:
        return None
    if settings.roles_mode != "on" or row["mode"] != "on" or row["dropped"] is not None:
        return None  # a run built while the roles were validate stays the team's after the switch
    b = row["brief_json"]
    sources = {s["id"]: s for s in (row["materials_digest"] or {}).get("sources", [])}
    findings = [
        {
            "text": f["text"],
            "confidence": f["confidence"],
            "sources": [
                {k: sources[i].get(k) for k in ("id", "kind", "label", "url")} for i in f["source_ids"] if i in sources
            ],
        }
        for f in b.get("findings", [])
        if not f.get("dropped")
    ]
    if not findings:
        return None

    def kept(key: str) -> list[str]:
        return [x["text"] for x in b.get(key, []) if not x.get("dropped")]

    return {
        "day": row["created_at"].date().isoformat(),
        "topics": (row["brief"] or {}).get("topics", []),
        "findings": findings,
        "searches_to_aim_for": kept("suggested_themes"),
        "searches_to_keep_out": kept("suggested_negatives"),
        "offer_angles": kept("offer_angles"),
        "unknowns": kept("unknowns"),
        "confidence": row["confidence"],
    }


RECENT_ROWS = 10  # the newest finished runs looked at for the one an owner may read
FRESH_DAYS = 90  # the Strategist reads research at most this old (plan D18)


@dataclass(frozen=True)
class Latest:
    """The research an owner may read, as `latest_view` finds it: the run's id (the plan records it; nothing an owner
    reads carries it) and `owner_view` of the run."""

    id: str
    view: dict


def latest_view(
    conn: psycopg.Connection, settings: config.Settings, business_id: str, *, within_days: int | None = None
) -> Latest | None:
    """The latest research an owner may read, picked in this one place -- get_research and the Strategist both ask here,
    so the owner and the plan read the same run: of the business's RECENT_ROWS newest finished runs (only those of the
    last `within_days` days, when given) the first `owner_view` passes (R-K4, plan D7: the one authority on what leaves
    the team). None when none does."""
    where, args = "business_id = %s AND status = 'done'", [business_id]
    if within_days is not None:
        where += " AND created_at >= now() - make_interval(days => %s)"
        args.append(within_days)
    rows = conn.execute(
        f"SELECT * FROM research_run WHERE {where} ORDER BY created_at DESC LIMIT %s", (*args, RECENT_ROWS)
    ).fetchall()
    for row in rows:
        view = owner_view(settings, row)
        if view is not None:
            return Latest(row["id"], view)
    return None


def for_strategist(conn: psycopg.Connection, settings: config.Settings, business_id: str) -> dict | None:
    """The research the Strategist reads in <research>: the latest an owner may read (`latest_view`) within FRESH_DAYS
    -- its id (the plan records it, never shows it), its day, the findings' words and confidence, the searches to aim
    for and to keep out, the offer angles and the unknowns, never a source's link or a business nearby's name. None
    otherwise: with the roles off or on validate no plan reads research, and neither does one with none within
    FRESH_DAYS."""
    found = latest_view(conn, settings, business_id, within_days=FRESH_DAYS)
    if found is None:
        return None
    view = found.view
    return {
        "id": found.id,
        "day": view["day"],
        "findings": [{"text": f["text"], "confidence": f["confidence"]} for f in view["findings"]],
        "searches_to_aim_for": view["searches_to_aim_for"],
        "searches_to_keep_out": view["searches_to_keep_out"],
        "offer_angles": view["offer_angles"],
        "unknowns": view["unknowns"],
    }


def team_view(row: dict) -> dict:
    """Everything stored, for a platform key and the operator's commands: the brief, the digest, every sentence with
    its guard reason, every round's verdict, why no owner reads it."""
    return {k: v.isoformat() if isinstance(v, date | datetime) else v for k, v in row.items()}


def runs_today_all(conn: psycopg.Connection) -> int:
    """Research runs started since midnight UTC, all businesses (MAX_RUNS_PER_DAY); a retried job's run counts once."""
    return conn.execute(
        "SELECT count(*) AS n FROM research_run WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
        "AT TIME ZONE 'UTC'"
    ).fetchone()["n"]


def fail_stale(conn: psycopg.Connection, i: int, n: int) -> int:
    """research-run's first step with `purge`, whatever the switch: a run left building past STALE_AFTER (its runner
    died) fails, `stale`. How many."""
    from ..jobs import shard_sql  # jobs imports runner, which imports this module

    return conn.execute(
        "UPDATE research_run SET status = 'failed', dropped = 'stale', finished_at = now() WHERE status = 'building' "
        f"AND started_at < now() - interval '{STALE_AFTER}' AND {shard_sql('business_id')}",
        {"i": i, "n": n},
    ).rowcount


def purge(conn: psycopg.Connection, i: int, n: int) -> int:
    """research-run's first step with `fail_stale`, whatever the switch: runs older than RETENTION_DAYS go. How many
    went."""
    from ..jobs import shard_sql  # jobs imports runner, which imports this module

    return conn.execute(
        f"DELETE FROM research_run WHERE created_at < now() - interval '{RETENTION_DAYS} days' "
        f"AND {shard_sql('business_id')}",
        {"i": i, "n": n},
    ).rowcount
