"""`python -m agent.research` (capability layer K4; spec 2026-10-07 §6.1 "Production"; founder rulings R-K3 and R-K4,
2026-10-08; plan D9): the Researcher writing Admiry's reviewed guides in K3's format (agent/knowledge_index.py) for
the founder to review -- never into a prompt unreviewed.

  python -m agent.research vertical <tree> [--topic T] [--section N ...] [--out DIR] [--replace]
      <tree> is a consultation tree's id (K3's verticals: attorneys, dentists, fallback, home_services). A topic is
      K3's -- compliance, google-search, offers, newsbreak (all four when --topic is left out; never under fallback,
      whose files are one trade each) -- or a trade under its tree (`home_services --topic pest-control`; another
      family's trade under fallback, `fallback --topic physicians --section 2.6`), tagged trade:<topic>. Reads
      Admiry's research notes (docs/research/2026-10-05-local-service-verticals.md: the tree's parts in SECTIONS, or
      --section's, with the policy and disclosure codes their tables use), the published cost-per-lead averages
      (benchmark.SEED), the reviewed guides Admiry already has for the tree, and the first page of up to ten published
      sources those parts cite (written out, or by a link reference the notes define at their end; crawl.py's guards
      and caps; untrusted, in a tag of their own, and a source is called by its host, never by the page's title);
      writes each topic with one model call, checks every line against the sources it cites and the whole file with
      K3's own loader, has the Critic grade it by the research rubric (one more round when it falls short), and
      writes DIR/<tree>/<topic>.md -- reviewed_by: pending -- with DIR/<tree>/<topic>.review.json beside it (the
      rounds, the grades, what was left out, the checks still open). DIR is docs/research/knowledge-drafts by default:
      outside agent/, so no image and no retrieval ever reads a draft; a DIR that is agent/ or reaches into it (by a
      symlink, or the same folder in another case) is refused. A topic whose draft or review file is already in DIR is
      left alone -- the founder edits drafts in place -- with a line saying so, before any page or model is asked for;
      --replace writes it again over both. A source has one id in every file: the notes' `research-verticals`, a
      benchmark row's `bench-…`, a guide's `guide-…`, a page's the id a file of the knowledge already gives its
      address, else `web-`, the words of its host and path and six hex digits of a hash of the address.
  python -m agent.research promote <tree>/<topic> --reviewer NAME [--from DIR] [--root DIR]
      after the founder's review (edit the draft in place first): NAME is one of knowledge_index.REVIEWERS; stamps
      reviewed_by and reviewed_at (today), checks the file with K3's loader, moves it into agent/knowledge/<tree>/
      <topic>.md (never over a file already there; the root must be K3's knowledge, an INDEX.md with its table's
      markers), removes its review file and rewrites INDEX.md's table (knowledge_index.write_index). When anything
      fails the knowledge is left as it was. Then run K3's knowledge-file test and commit.

Needs META_MODEL_API_KEY (vertical, when a topic is to be written) and a checkout (the notes live in docs/). No
database. It reads the reviewed guides whatever AGENT_KNOWLEDGE says: they are this offline command's material, not a
research run's. Our own aggregated data (spec §6.1, n >= 5) is not read yet (plan D13). Exit status 0; 1 when a topic
got no draft, is refused by K3's loader or still has checks open (its line says which, and its review file lists
them), a promotion was refused or no model is configured; 2 when the arguments cannot be run. A topic left alone
because its draft is there is no failure."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlsplit

from . import benchmark, config, knowledge_index, llm, models, plans
from .channels import registry
from .roles import base, critic, guard, research_material, strategist

REPO = Path(__file__).resolve().parents[1]
DOC_PATH = "docs/research/2026-10-05-local-service-verticals.md"
DOC = REPO / DOC_PATH
DOC_AS_OF = date(2026, 10, 5)  # the day the notes read their sources (K3's source registry)
DOC_ID = "research-verticals"  # the notes' source id, as K3's batch cites them
DRAFTS = REPO / "docs" / "research" / "knowledge-drafts"
# The notes' groups whose rows are each tree's category (K3's INDEX taxonomy table); fallback's files name theirs
SECTIONS: dict[str, tuple[str, ...]] = {
    "dentists": ("2.8",),
    "attorneys": ("2.14",),
    "home_services": ("2.1", "2.2", "2.3", "2.4"),
}
SHARED = ("1.2", "1.4")  # the ad-policy and disclosure codes the trades' tables use
TOPICS = (strategist.COMPLIANCE, strategist.SEARCH, strategist.OFFERS, strategist.NEWSBREAK)  # K3's four per tree
# K3's batch-1 scheme ("Every file"): a topic's channels ([] = every channel) and tags; a trade's are below
CHANNELS_OF = {
    strategist.COMPLIANCE: [],
    strategist.SEARCH: ["google_search"],
    strategist.OFFERS: [],
    strategist.NEWSBREAK: ["newsbreak"],
}
TAGS_OF = {
    strategist.COMPLIANCE: ["stage:plan", "ad-text", "disclosures"],
    strategist.SEARCH: ["stage:plan", "stage:running", "keywords", "negatives"],
    strategist.OFFERS: ["stage:plan", "offers", "ad-text"],
    strategist.NEWSBREAK: ["stage:plan", "newsbreak", "creative"],
}
VERTICAL_WORDS = {
    "dentists": "Dentists",
    "home_services": "Home services",
    "attorneys": "Attorneys",
    "fallback": "Other trades",
}
BENCH_CATEGORY = {
    "dentists": "dentists",
    "attorneys": "attorneys",
    "home_services": "home_services",
    "fallback": "fallback",
}
MAX_PAGES = 10
PAGE_CHARS = 4000
WEB_SLUG_MAX = 40  # the words of a crawled page's address in its id, at most: "web-", them, "-" and WEB_HASH_CHARS
WEB_HASH_CHARS = 6  # hex digits of the address's sha256 that end its id: 51 characters in all, at most
EFFORT = "medium"
KEY_POINTS_WORDS, TRADE_KEY_POINTS_WORDS, DETAILS_WORDS = 110, 90, 400  # K3's rules for the writer
KEY_POINTS_LINES = 6
DONT_LINES, DONT_WORDS = 4, 14
SUMMARY_MAX = knowledge_index.SUMMARY_MAX
_HEADING = re.compile(r"^(#{2,3}) (\d+(?:\.\d+)?)\b", re.MULTILINE)
# an address written out, or the label of a reference-style link: `[text][label]`
_CITED = re.compile(r"https?://[^\s)>\]\"']+|\]\[([^\]]+)\]")
_REFERENCE = re.compile(r"^\[([^\]]+)\]:[ \t]*(https?://\S+)", re.MULTILINE)  # `[label]: https://…`
# what a line may not hold (SYSTEM_KNOWLEDGE: "no link, e-mail address or phone number"): roles.guard's patterns for a
# link and an e-mail address, and its phone number -- (555) 123-4567, 555-123-4567, 555.123.4567, 555 123 4567. Not its
# angle brackets: a placeholder (<town>) is written in them.
_LINK = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
NO_LINK = "no link, e-mail address or phone number in the text"
# Every tag name llm's prompts wrap data in: DATA_TAGS lists some of them in words and the _tag(…) calls name the rest
# (a test reads llm.py for any that is missing). A line that holds one in angle brackets, or any closing tag, could close
# or open a block of a prompt the knowledge is read in; a placeholder (<town>) is none.
DATA_TAG_NAMES = frozenset(re.findall(r"<([a-z_]+)>", llm.DATA_TAGS)) | {
    "feedback",
    "instruction",
    "pages",
    "research",
    "insight",
    "trade_knowledge",
    "locale",
    "kit",
    "recipe",
    "page_copy",
    "facts",
    "family",
    "message",
}
_TAG = re.compile(r"<\s*/|<\s*(?:" + "|".join(sorted(DATA_TAG_NAMES)) + r")\b", re.IGNORECASE)
NO_TAG = "no data tag in the text (</…>, <knowledge>, <instruction> …); placeholders such as <town> are fine"
NO_MONEY = "no money figure in a key point — put it under Numbers with its source"
ALREADY = "{}: a draft is already there — edit it, promote it, or delete it (or pass --replace)"


def trees() -> tuple[str, ...]:
    """K3's verticals: the consultation trees' ids (`_common` is no tree)."""
    return tuple(sorted(p.stem for p in knowledge_index.TREES_DIR.glob("*.json") if not p.stem.startswith("_")))


def channels_for(topic: str) -> list[str]:
    return list(CHANNELS_OF.get(topic, ["google_search"]))


def tags_for(tree: str, topic: str) -> list[str]:
    if topic not in TAGS_OF:  # a trade
        return ["stage:plan", "keywords", "negatives", f"trade:{topic}"]
    extra = ["licensing"] if tree == "home_services" and topic == strategist.COMPLIANCE else []
    return [*TAGS_OF[topic], *extra]


def section(doc: str, number: str) -> str:
    """One numbered part of the notes, its heading line included, up to the next heading of its level or above; ""
    when the notes have none."""
    found = list(_HEADING.finditer(doc))
    for k, m in enumerate(found):
        if m.group(2) == number:
            level = len(m.group(1))
            end = next((n.start() for n in found[k + 1 :] if len(n.group(1)) <= level), len(doc))
            return doc[m.start() : end].strip()
    return ""


def _label(raw: str) -> str:
    """A link reference's label as markdown reads it: case and spacing do not matter."""
    return " ".join(raw.lower().split())


def references(doc: str) -> dict[str, str]:
    """The notes' link references by label -- `[g-av]: https://…`, defined at the end of the document, where its tables
    cite them as `[text][g-av]` and write no address of their own. The first definition of a label counts."""
    out: dict[str, str] = {}
    for m in _REFERENCE.finditer(doc):
        out.setdefault(_label(m.group(1)), m.group(2))
    return out


def links(text: str, defined: dict[str, str] | None = None) -> list[str]:
    """The http(s) addresses a text cites, once each, in order, trailing punctuation off: written out, or through a
    reference-style link (`[text][label]`) whose label `defined` (references) gives an address; a label it does not
    define names none."""
    found = []
    for m in _CITED.finditer(text):
        url = (defined or {}).get(_label(m.group(1))) if m.group(1) else m.group(0)
        if url:
            found.append(url.rstrip(".,;:"))
    return list(dict.fromkeys(found))


@dataclass
class Material:
    tree: str
    material: dict
    pages: list[dict]
    sources: dict[str, dict]  # id (a K3 source id) -> {"kind", "label", "url" | "doc", "as_of", "text"}


def _slug(*parts: str) -> str:
    return "-".join(re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in parts)


def _address(url: str) -> str:
    """An address as one page is known by it: its host (lower case, `www.` off), its path without a trailing `/` and its
    query. The scheme and the fragment make no other page. What urlsplit cannot read stands for itself."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = f"?{parts.query}" if parts.query else ""
    return f"{research_material.host(url) or ''}{parts.path.rstrip('/')}{query}"


def _web_id(url: str) -> str:
    """A crawled page's id, a function of its address alone (plan K4 R3, fix round 1): `web-`, the words of its host and
    path cut to WEB_SLUG_MAX characters, `-` and the first WEB_HASH_CHARS hex digits of the sha256 of the address
    (`_address`: the query counts, so `?x=1` and `?x=2` are two pages). The same address has the same id in every
    run, in any order and beside any other page, so one id names one source in every file; the words are for the
    founder's eye, the hash tells `/a-b` from `/a/b`. No words (an address that slugs to nothing): `web-` and the hash."""
    address = _address(url)
    words = _slug(address.partition("?")[0])[:WEB_SLUG_MAX].rstrip("-")
    digest = hashlib.sha256(address.encode("utf-8", "replace")).hexdigest()[:WEB_HASH_CHARS]
    return f"web-{words}-{digest}" if words else f"web-{digest}"


def _page_label(url: str) -> str:
    """What the model reads a page source is called: its host. Never the page's own title, which is whatever its site
    says (the title goes to the model in <pages>, in a tag of its own)."""
    host = research_material.host(url)
    return f"A published page on {host}"[:80] if host else "A published page"


def gather(tree: str, *, sections: tuple[str, ...], doc: str, today: date) -> Material:
    """What one tree's drafts read, each source under the id its file will cite -- one id, one source, in every file:
    the notes' parts (and the shared codes) as `research-verticals`; the tree's benchmark rows that have a published
    address (`bench-<category>-<channel>`; Bing's says in words that it is Google's, and a K3 source is an address or a
    document); Admiry's reviewed guides for the tree (`guide-<tree>-<topic>`: their blocks, source ids out), whatever
    AGENT_KNOWLEDGE says; and the first page of up to MAX_PAGES sources the parts cite, written out or by a link
    reference of the notes, each under the id a file of the knowledge (any tree's) already gives its address, else the
    id its address makes (`_web_id`: the words of host and path and a hash, the same in every run). A page source is
    labelled by its host (`_page_label`); the page's own title goes to the model in <pages> and nowhere else."""
    sources: dict[str, dict] = {}
    notes = "\n\n".join(t for t in (section(doc, n) for n in (*sections, *SHARED)) if t)
    if notes:
        sources[DOC_ID] = {
            "kind": "doc",
            "label": "Admiry's research notes",
            "doc": DOC_PATH,
            "as_of": DOC_AS_OF,
            "text": notes,
        }
    rows = []
    category = BENCH_CATEGORY[tree]
    for r in benchmark.SEED:
        if r["source_url"].startswith(("http://", "https://")) and (
            r["category"] == category or r["category"].startswith(f"{category}_")
        ):
            sid = _slug("bench", r["category"], r["channel"])
            row = {
                "id": sid,
                "trade": r["category"],
                "channel": registry.label(r["channel"]),
                "cost_per_lead": plans.usd(r["cpl_avg"]),
                "low": plans.usd(r["cpl_low"]),
                "high": plans.usd(r["cpl_high"]),
                "year": r["as_of"],
            }
            rows.append(row)
            sources[sid] = {
                "kind": "benchmark",
                "label": f"Published cost-per-lead averages: {r['category']}, {row['channel']}",
                "url": r["source_url"],
                "as_of": today,
                "text": json.dumps(row, ensure_ascii=False),
            }
    guides = []
    # an address (_address) -> the id a file of the knowledge, any tree's and waiting or reviewed, gives it
    ids_of: dict[str, str] = {}
    # KNOWLEDGE_DIR is passed on purpose: load's default is bound when K3's module loads, a patched one is honoured here
    for e in knowledge_index.load(knowledge_index.KNOWLEDGE_DIR):
        for s in e.sources:
            if s.get("url"):
                ids_of.setdefault(_address(s["url"]), s["id"])
        if e.vertical == tree and e.reviewed_by != knowledge_index.PENDING:
            sid, text = _slug("guide", e.vertical, e.topic), knowledge_index.block(e)
            guides.append({"id": sid, "text": text})
            sources[sid] = {
                "kind": "knowledge",
                "label": f"Admiry's guide {e.id}",
                "doc": f"agent/knowledge/{e.id}.md",
                "as_of": e.reviewed_at,
                "text": text,
            }
    pages = []
    wanted: dict[str, str] = {}  # an address (_address) -> the address as the notes write it: one page for each
    for url in links("\n".join(section(doc, n) for n in sections), references(doc)):
        if len(wanted) < MAX_PAGES:
            wanted.setdefault(_address(url), url)
    for address, url in wanted.items():
        page = research_material.fetch_page(url)
        if page is None or not page.text:
            continue
        pid = ids_of.get(address)
        if pid is None or pid in sources:  # the knowledge's id, unless this run uses it for another source
            pid = _web_id(url)
        if pid in sources:  # two addresses in one id (the hash is short): the second is left out, never renamed
            continue
        text = page.text[:PAGE_CHARS]
        pages.append({"id": pid, "title": page.title[:120], "text": text})
        sources[pid] = {"kind": "page", "label": _page_label(url), "url": url, "as_of": today, "text": text}
    material = {
        "tree": tree,
        "notes": notes,
        "benchmark": rows,
        "knowledge": guides,
        "sources": [{"id": i, "kind": s["kind"], "label": s["label"]} for i, s in sources.items()],
    }
    return Material(tree, material, pages, sources)


def held(text: str, source_text: str) -> bool:
    """Whether every number `text` states is in `source_text`: an amount to the cent or rounded half up to whole
    dollars, any other number as written (roles.guard's reading)."""
    money, other = guard.tokens(text)
    known = guard.known(source_text)
    whole = {x.quantize(Decimal(1), rounding=ROUND_HALF_UP) for x in known}
    return all(m in known or m in whole for m in money) and all(o in known for o in other)


def checked(draft: models.KnowledgeDraft, m: Material, topic: str) -> tuple[dict, list[str], list[str]]:
    """(the draft as the file takes it, what a second round reads, the lines left out). Each line keeps the source ids
    the material holds; square brackets become angle brackets (K3 reads a bracketed word as a source id); a line is left
    out when it holds a link, an e-mail address, a phone number or a data tag, when a Key point states a money figure,
    and when it states a figure (a digit, or a number in words) that its cited sources do not state; a Numbers line needs
    a source. The summary, the word limits K3 sets its writer and a draft with nothing true to say are fed back."""
    problems: list[str] = []
    left: list[str] = []

    def lines(items: list[models.KnowledgeLine], where: str, *, cited: bool, money_ok: bool = True) -> list[dict]:
        out = []
        for n, item in enumerate(items, 1):
            text = " ".join(item.text.split())
            if "[" in text or "]" in text:
                problems.append(f"{where} {n}: write a placeholder in angle brackets, never square ones")
                text = text.replace("[", "<").replace("]", ">")
            ids = []
            for raw in item.source_ids:
                sid = raw.strip()
                if sid in m.sources:
                    if sid not in ids:
                        ids.append(sid)
                else:
                    problems.append(f"{where} {n}: {raw!r} is not one of the sources")
            words = " ".join(m.sources[i]["text"] for i in ids)
            amounts, other = guard.tokens(text)
            if _LINK.search(text):
                problems.append(f"{where} {n}: {NO_LINK}")
                left.append(text)
            elif _TAG.search(text):
                problems.append(f"{where} {n}: {NO_TAG}")
                left.append(text)
            elif amounts and not money_ok:
                problems.append(f"{where} {n}: {NO_MONEY}")
                left.append(text)
            elif (amounts or other or re.search(r"\d", text)) and not (ids and held(text, words)):
                problems.append(f"{where} {n}: its sources do not state every figure in it")
                left.append(text)
            elif cited and not ids:
                problems.append(f"{where} {n}: a figure needs the source that states it")
                left.append(text)
            elif text:
                out.append({"text": text, "source_ids": ids})
        return out

    title = " ".join(draft.title.split())
    if _TAG.search(title):
        problems.append(f"title: {NO_TAG}")
        title = ""
    kept = {
        "title": title or topic,
        "summary": " ".join(draft.summary.split()),
        "key_points": lines(draft.key_points, "key point", cited=False, money_ok=False),
        "details": lines(draft.details, "detail", cited=False),
        "numbers": lines(draft.numbers, "number", cited=True),
        "do_not_say": lines(draft.do_not_say, "do-not-say line", cited=False),
        "confidence": draft.confidence,
    }
    s = kept["summary"]
    # what K3's loader refuses of a summary: a digit, and '|', '<' or '>' (the cell of INDEX.md's table, its markers)
    if not s or len(s) > SUMMARY_MAX or not s.endswith(".") or re.search(r"[\d|<>]", s) or guard.sentences(s) != 1:
        problems.append(
            f"summary: one sentence, at most {SUMMARY_MAX} characters, no digit and no '|', '<' or '>', "
            "ending with a full stop"
        )
    limit = KEY_POINTS_WORDS if topic in TAGS_OF else TRADE_KEY_POINTS_WORDS
    words = sum(len(x["text"].split()) for x in kept["key_points"])
    if not kept["key_points"] or words > limit:
        problems.append(f"key points: one to six lines, at most {limit} words in all (now {words})")
    if len(kept["key_points"]) > KEY_POINTS_LINES:
        problems.append(f"key points: at most {KEY_POINTS_LINES} lines (now {len(kept['key_points'])})")
    if sum(len(x["text"].split()) for x in kept["details"]) > DETAILS_WORDS:
        problems.append(f"details: at most {DETAILS_WORDS} words")
    if len(kept["do_not_say"]) > DONT_LINES or any(len(x["text"].split()) > DONT_WORDS for x in kept["do_not_say"]):
        problems.append(f"do not say: at most {DONT_LINES} lines of at most {DONT_WORDS} words")
    if not draft.grounded:
        problems.append("it said the material gave nothing true to say on the topic")
    return kept, problems, left


def _cite(item: dict) -> str:
    """A line with its sources, as K3's files write them: `[id]` before the closing full stop."""
    text, ids = item["text"], " ".join(f"[{i}]" for i in item["source_ids"])
    if not ids:
        return text
    return f"{text[:-1]} {ids}." if text.endswith(".") else f"{text} {ids}"


def render(tree: str, topic: str, draft: dict, m: Material, today: date) -> str:
    """The draft as K3's file (its Task 1 format, its batch-1 scheme): the frontmatter -- the sources it cites, in the
    order it first cites them, `reviewed_by: pending` -- the title, the summary and the four sections."""
    every = [x for key in ("key_points", "details", "numbers", "do_not_say") for x in draft[key]]
    cited = list(dict.fromkeys(i for x in every for i in x["source_ids"]))
    front = [
        "---",
        f"vertical: {tree}",
        f"channels: [{', '.join(channels_for(topic))}]",
        f"tags: [{', '.join(tags_for(tree, topic))}]",
        "sources:",
    ]
    for sid in cited:
        s = m.sources[sid]
        where = "url" if s.get("url") else "doc"
        front += [f"  - id: {sid}", f"    {where}: {json.dumps(s[where])}", f"    as_of: {s['as_of'].isoformat()}"]
    front += [
        f"confidence: {draft['confidence']}",
        f"reviewed_by: {knowledge_index.PENDING}",
        f"reviewed_at: {today.isoformat()}",
        "---",
    ]

    def listed(key: str) -> str:
        return "\n".join(f"- {_cite(x)}" for x in draft[key]) or "None."

    body = [
        f"# {VERTICAL_WORDS[tree]} — {draft['title']}",
        "",
        draft["summary"],
        "",
        "## Key points",
        "",
        listed("key_points"),
        "",
        "## Details",
        "",
        listed("details"),
        "",
        "## Numbers",
        "",
        listed("numbers"),
        "",
        "## Do not say",
        "",
        listed("do_not_say"),
        "",
    ]
    return "\n".join(front) + "\n\n" + "\n".join(body)


def loads(tree: str, topic: str, text: str) -> str | None:
    """K3's own loader's refusal of the file, or None when it reads it (the file alone, in a scratch tree)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / tree / f"{topic}.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        try:
            knowledge_index.load(Path(tmp))
        except knowledge_index.KnowledgeError as e:
            return str(e)
    return None


def write(gen, settings, m: Material, topic: str, today: date) -> dict:
    """One topic's file: at most two rounds (spec §4.1) -- a second when the Critic does not deliver, a check failed or
    K3's loader refused the file, with what was wrong. {"text": the file or None, "rounds", "left_out": the lines the
    last file leaves out, "refused": K3's refusal of the last file, or None, "open": the checks the last file still
    fails -- what the writer's checks found, K3's refusal among them}."""
    material = {**m.material, "topic": topic, "writing": "a knowledge file for Admiry's planner"}
    feedback: list[str] | None = None
    out: dict = {"text": None, "rounds": [], "left_out": [], "refused": None, "open": []}
    for rnd in (1, 2):
        try:
            draft = gen.write_knowledge(material, m.pages, feedback)
        except llm.LLMError as e:
            out["rounds"].append({"round": rnd, "error": str(e)})
            break
        kept, problems, left = checked(draft, m, topic)
        text = render(m.tree, topic, kept, m, today)
        refused = loads(m.tree, topic, text)
        if refused:
            problems.append(f"the file: {refused}")
        out.update(text=text, left_out=left, refused=refused, open=problems)
        try:
            judged = critic.judge_research(settings, material, m.pages, draft.model_dump())
        except llm.LLMError as e:
            out["rounds"].append({"round": rnd, "problems": problems, "error": str(e)})
            break
        out["rounds"].append(
            {
                "round": rnd,
                "score": judged.score,
                "verdict": judged.verdict,
                "grades": judged.grades,
                "issues": judged.issues,
                "missing": judged.review.missing,
                "problems": problems,
            }
        )
        if judged.verdict == base.DELIVER and not problems:
            break
        feedback = [
            *problems,
            *(f"{i['dimension']}: {i['text']}" for i in judged.issues),
            *(f"missing: {w}" for w in judged.review.missing),
        ]
    return out


def promote(
    name: str, *, reviewer: str, drafts: Path = DRAFTS, root: Path | None = None, today: date | None = None
) -> Path:
    """The reviewed draft `<tree>/<topic>` stamped (reviewed_by `reviewer`, one of knowledge_index.REVIEWERS;
    reviewed_at today), checked by K3's loader, moved into the knowledge (root: agent/knowledge) and INDEX.md's table
    rewritten (knowledge_index.write_index); its review file removed. The file. FileNotFoundError without the draft,
    FileExistsError when a reviewed file is already there, ValueError for an unknown reviewer, a name that is not a
    tree of K3's and a topic (it is joined onto two folders, so it must not climb out of them), a root that is not K3's
    knowledge (no INDEX.md with its table's markers), a file this command did not write unreviewed, one K3's loader
    refuses, or a knowledge that does not load or cannot be written: the knowledge is then as it was -- the file, the
    folders this call made and INDEX.md as they were -- and the draft and its review file are where they were."""
    root = root or knowledge_index.KNOWLEDGE_DIR
    today = today or datetime.now(UTC).date()
    if reviewer not in knowledge_index.REVIEWERS:
        raise ValueError(f"--reviewer must be one of {', '.join(knowledge_index.REVIEWERS)}")
    tree, _, topic = name.partition("/")
    if tree not in trees() or not knowledge_index.TOPIC.fullmatch(topic):
        raise ValueError("name a draft as <tree>/<topic> (dentists/google-search)")
    src, review = drafts / tree / f"{topic}.md", drafts / tree / f"{topic}.review.json"
    target = root / tree / f"{topic}.md"
    if not src.exists():
        raise FileNotFoundError(f"no draft {src}")
    if target.exists():
        raise FileExistsError(f"{target} exists: reviewed knowledge is edited by hand, never replaced by a draft")
    try:
        knowledge_index.index_files(root)  # a readable INDEX.md that has the markers of its table
    except knowledge_index.KnowledgeError:
        raise ValueError(f"not K3's knowledge: {root}") from None
    text = src.read_text(encoding="utf-8")
    if len(re.findall(rf"^reviewed_by: {knowledge_index.PENDING}$", text, re.MULTILINE)) != 1:
        raise ValueError(f"{src} is not an unreviewed draft of this command")
    stamped = re.sub(
        rf"^reviewed_by: {knowledge_index.PENDING}$", f"reviewed_by: {reviewer}", text, count=1, flags=re.MULTILINE
    )
    stamped = re.sub(r"^reviewed_at: .+$", f"reviewed_at: {today.isoformat()}", stamped, count=1, flags=re.MULTILINE)
    refused = loads(tree, topic, stamped)
    if refused:
        raise ValueError(f"fix the draft first: {refused}")
    made: list[Path] = []  # the folders this call makes, the innermost first
    folder = target.parent
    while not folder.exists():
        made.append(folder)
        folder = folder.parent
    index = root / knowledge_index.INDEX
    index_before = index.read_bytes()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stamped, encoding="utf-8")
        knowledge_index.write_index(root)
    except (knowledge_index.KnowledgeError, OSError) as e:
        # another file of the knowledge is malformed, or something could not be written: put everything back
        target.unlink(missing_ok=True)
        for made_folder in made:
            with contextlib.suppress(OSError):
                made_folder.rmdir()
        with contextlib.suppress(OSError):
            if index.read_bytes() != index_before:  # a rewrite that failed half-way
                index.write_bytes(index_before)
        what = "does not load" if isinstance(e, knowledge_index.KnowledgeError) else "cannot be written"
        raise ValueError(f"the knowledge {what}, so nothing was promoted: {e}") from e
    knowledge_index.forget()
    src.unlink()
    review.unlink(missing_ok=True)
    return target


def _topic(raw: str) -> str:
    if not knowledge_index.TOPIC.fullmatch(raw):
        raise argparse.ArgumentTypeError("a topic is lowercase words joined by '-' (google-search, pest-control)")
    return raw


def _in_agent(path: Path) -> bool:
    """Whether `path` is agent/ or lies inside it, judged by what the names reach (os.path.samefile) and not by how they
    are spelled: a symlink on the way, or another case of the same name on a case-insensitive disk, leads into the same
    folder. A path that does not exist yet counts by the folders of it that do."""
    agent = REPO / "agent"
    here = path.resolve()
    for step in (here, *here.parents):
        try:
            if step.exists() and os.path.samefile(step, agent):
                return True
        except OSError:  # a folder that cannot be read says nothing about agent/
            continue
    return False


def _there(path: Path) -> bool:
    """Whether something is at `path`, a symlink that leads nowhere included."""
    return path.exists() or path.is_symlink()


def _vertical(tree: str, topic: str | None, wanted: list[str] | None, out: Path, replace: bool = False) -> int:
    # --out, and the folder of this tree in it if it is already there (it may be a symlink), must not reach agent/
    for place, why in ((out, "choose another --out"), (out / tree, f"{out / tree} leads into it")):
        if _in_agent(place):
            print(f"drafts go outside agent/, where no image and no plan reads them: {why}", file=sys.stderr)
            return 2
    if tree == "fallback" and (topic is None or topic in TOPICS):
        # one family's part of the notes would make every fallback business read that family's compliance notes
        print(
            "fallback's files are one trade each: --topic <trade> --section <part>, never compliance, google-search, "
            "offers or newsbreak",
            file=sys.stderr,
        )
        return 2
    sections = tuple(wanted or SECTIONS.get(tree, ()))
    if not sections:
        print("fallback's files name their part of the research notes: --section 2.6 (repeatable)", file=sys.stderr)
        return 2
    doc = DOC.read_text(encoding="utf-8")
    missing = [n for n in sections if not section(doc, n)]
    if missing:
        print(f"the research notes have no §{', §'.join(missing)}", file=sys.stderr)
        return 2
    folder = out / tree
    # A draft the founder has been editing, or its review file, is never written over (--replace says it may be). Said
    # before anything is read or asked: a skip costs no page and no model call, and needs no key.
    todo: list[str] = []
    for t in [topic] if topic else TOPICS:
        if not replace and (_there(folder / f"{t}.md") or _there(folder / f"{t}.review.json")):
            print(ALREADY.format(f"{tree}/{t}"))
        else:
            todo.append(t)
    if not todo:
        return 0
    settings = config.load()
    try:
        gen = llm.generator(settings, effort=EFFORT)
    except llm.LLMError as e:
        print(str(e), file=sys.stderr)
        return 1
    today = datetime.now(UTC).date()
    try:
        m = gather(tree, sections=sections, doc=doc, today=today)
    except knowledge_index.KnowledgeError as e:
        print(f"agent/knowledge does not load: {e}", file=sys.stderr)
        return 1
    unclean = 0  # topics with no draft, a file K3's loader refuses, or checks still open: the exit status says so
    for t in todo:
        got = write(gen, settings, m, t, today)
        last = got["rounds"][-1] if got["rounds"] else {}
        if got["text"] is None:
            print(f"{tree}/{t}: no draft ({last.get('error', 'the model gave none')})", file=sys.stderr)
            unclean += 1
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{t}.md"
        path.write_text(got["text"], encoding="utf-8")
        meta = {k: got[k] for k in ("rounds", "left_out", "refused", "open")} | {
            "written": today.isoformat(),
            "model": settings.llm_model,
        }
        (folder / f"{t}.review.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        notes = []
        if got["refused"]:
            notes.append(f"K3's loader refuses it: {got['refused']}")
        if got["open"]:
            notes.append(f"{len(got['open'])} checks open: see {t}.review.json")
        unclean += bool(notes)
        note = f" ({'; '.join(notes)})" if notes else ""
        print(f"{tree}/{t}: {last.get('verdict') or 'no verdict'} {last.get('score', '-')} -> {path}{note}")
    return 1 if unclean else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.research", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("vertical", help="write a tree's knowledge drafts for the founder's review")
    v.add_argument("tree", choices=trees())
    v.add_argument("--topic", type=_topic, help="one topic: K3's four, or a trade; fallback needs a trade")
    v.add_argument("--section", action="append", help="a part of the research notes, e.g. 2.6 (fallback needs one)")
    v.add_argument("--out", type=Path, default=DRAFTS, help="where the drafts go (outside agent/)")
    v.add_argument(
        "--replace",
        action="store_true",
        help="write a topic again over the draft and review file already there (the founder's edits go)",
    )
    p = sub.add_parser("promote", help="move a reviewed draft into agent/knowledge")
    p.add_argument("name", help="<tree>/<topic>")
    p.add_argument("--reviewer", required=True, choices=knowledge_index.REVIEWERS)
    p.add_argument("--from", dest="drafts", type=Path, default=DRAFTS, help="where the drafts are")
    p.add_argument("--root", type=Path, default=None, help="the knowledge to promote into (default agent/knowledge)")
    args = parser.parse_args(argv)
    if args.cmd == "vertical":
        return _vertical(args.tree, args.topic, args.section, args.out, args.replace)
    try:
        target = promote(args.name, reviewer=args.reviewer, drafts=args.drafts, root=args.root)
    except (OSError, ValueError) as e:  # no draft, a file in the way, one that cannot be read or written, a refusal
        print(str(e), file=sys.stderr)
        return 1
    print(f"{target}: reviewed by {args.reviewer}; INDEX.md rewritten. Run K3's knowledge-file test, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
