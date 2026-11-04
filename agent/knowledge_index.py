"""Admiry's prior knowledge (capability layer K3; spec 2026-10-07-admiry-capability-layer-design §6.1; plan
2026-10-08-agent-k3-knowledge-v1): curated Markdown files, one per vertical and topic (`agent/knowledge/<vertical>/
<topic>.md`, the vertical a consultation tree's id, the id `<vertical>/<topic>`), reviewed by the founder and
versioned in git -- what Admiry asserts about advertising local services. This module loads and checks a file
(`load`: the frontmatter keys exactly as the spec names them, the four sections in order, a source for every line
that states a number), writes INDEX.md's table of files and retrieves by tags within a token budget. A file written
for review says `reviewed_by: pending`: it is loaded and checked, never retrieved, cited or served (plan P10)."""

from __future__ import annotations

import argparse
import functools
import logging
import math
import re
import sys
import typing
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from . import models

log = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
# consult.TREES_DIR (test-pinned), not imported: a vertical is a consultation tree's id, and this module stays a leaf
TREES_DIR = Path(__file__).parent / "consult_trees"
KEYS = ("vertical", "channels", "tags", "sources", "confidence", "reviewed_by", "reviewed_at")  # spec §6.1, exactly
SECTIONS = ("Key points", "Details", "Numbers", "Do not say")  # spec §6.1, in this order
CONFIDENCE = ("low", "medium", "high")
PENDING = "pending"  # written for review (the first batch, K4's agent.research): never read by a model or served
REVIEWERS = ("founder",)
CHANNELS = typing.get_args(models.Channel)  # what a file's `channels` may name; none named: every channel
KEY_POINTS_MAX_WORDS = 300  # spec §6.1
SUMMARY_MAX = 160  # the INDEX line's summary
BUDGET_TOKENS = 1500  # spec §6.1: the knowledge one model call reads
CHARS_PER_TOKEN = 3.5  # conservative for English prose (about 4 is usual), so the budget holds on any tokenizer
REVIEW_DUE_DAYS = 365  # agent.ops knowledge flags a review older than this
VERTICAL = re.compile(r"[a-z][a-z_]*")
TOPIC = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TAG = re.compile(r"[a-z0-9][a-z0-9:-]*")
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
CITATION = re.compile(r"\[([a-z0-9][a-z0-9-]*)\]")
DIGIT = re.compile(r"\d")


class KnowledgeError(ValueError):
    """A knowledge file that breaks the format: the message starts with the file's id and names the rule."""


@dataclass(frozen=True)
class Entry:
    id: str
    vertical: str
    topic: str
    channels: tuple[str, ...]
    tags: tuple[str, ...]
    summary: str
    sources: tuple[dict, ...]
    confidence: str
    reviewed_at: date
    path: Path
    # one of REVIEWERS, or PENDING (plan P16: the one field beyond the brief's, appended with a default so an Entry
    # built by hand -- K4's tests do -- keeps the brief's ten arguments)
    reviewed_by: str = REVIEWERS[0]


def load(root: Path = KNOWLEDGE_DIR) -> list[Entry]:
    """Every file under `root`, in id order, checked; KnowledgeError at the first that breaks the format. The default
    is bound when the module loads: a caller that honours a patched KNOWLEDGE_DIR passes it."""
    return [_entry(path) for path in sorted(root.glob("*/*.md"))]


def key_points(entry: Entry) -> str:
    """The file's Key points as written (its '- ' lines): what retrieval reads first."""
    return _sections(entry)["Key points"]


def do_not_say(entry: Entry) -> list[str]:
    """The file's Do not say lines, each as written ('- …'); none when the section says None."""
    units = _units(_sections(entry)["Do not say"])
    return [] if units == ["None."] else units


def tokens(text: str) -> int:
    """A conservative estimate of `text`'s tokens (CHARS_PER_TOKEN): the budget's unit."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _read(path: Path, name: str) -> str:
    """The file's text. A file that is not UTF-8 or cannot be read breaks the format like any other (a plan must never
    stop on one)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise KnowledgeError(f"{name}: the file is not UTF-8") from None
    except OSError:  # a directory named like a file, a file deleted after `load`, one nobody may read
        raise KnowledgeError(f"{name}: the file cannot be read") from None


def _sections(entry: Entry) -> dict[str, str]:
    return _body(_split(_read(entry.path, entry.id), entry.id)[1], entry.id)[1]


def _entry(path: Path) -> Entry:
    vertical, topic = path.parent.name, path.stem
    name = f"{vertical}/{topic}"
    if not VERTICAL.fullmatch(vertical) or not (TREES_DIR / f"{vertical}.json").is_file():
        raise KnowledgeError(f"{name}: {vertical!r} is not a consultation tree's id")
    if not TOPIC.fullmatch(topic):
        raise KnowledgeError(f"{name}: the topic must be lowercase words joined by '-'")
    front, body = _split(_read(path, name), name)
    _check_front(front, name, vertical)
    summary, sections = _body(body, name)
    _check_citations(sections, front["sources"], name)
    return Entry(
        id=name,
        vertical=vertical,
        topic=topic,
        channels=tuple(front["channels"]),
        tags=tuple(front["tags"]),
        summary=summary,
        sources=tuple(dict(s) for s in front["sources"]),
        confidence=front["confidence"],
        reviewed_at=front["reviewed_at"],
        path=path,
        reviewed_by=front["reviewed_by"],
    )


def _split(text: str, name: str) -> tuple[dict, str]:
    """(the frontmatter, the body): the file opens with a line of three dashes and the frontmatter ends at the next."""
    if not text.startswith("---\n"):
        raise KnowledgeError(f"{name}: no frontmatter: the file starts with a line of three dashes")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise KnowledgeError(f"{name}: the frontmatter has no closing line of three dashes")
    # PyYAML's constructors raise more than YAMLError: ValueError for a date that is not one (2026-02-30), KeyError,
    # IndexError and AttributeError for a malformed tag (!!bool maybe), RecursionError for deep nesting
    try:
        front = yaml.safe_load(head)
    except Exception:  # noqa: BLE001 - whichever it is, the file is at fault, and a plan must never stop on a file
        raise KnowledgeError(f"{name}: the frontmatter is not YAML") from None
    if not isinstance(front, dict):
        raise KnowledgeError(f"{name}: the frontmatter is not a mapping")
    return front, body


def _is_date(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _check_front(front: dict, name: str, vertical: str) -> None:
    if set(front) != set(KEYS):
        # key=str: YAML reads a key such as 1, on or ~ as an int, a bool or None, which sorted() cannot order
        missing, unknown = sorted(set(KEYS) - set(front)), sorted(set(front) - set(KEYS), key=str)
        raise KnowledgeError(
            f"{name}: frontmatter keys must be exactly {', '.join(KEYS)} (missing {missing}, unknown {unknown})"
        )
    if front["vertical"] != vertical:
        raise KnowledgeError(f"{name}: vertical {front['vertical']!r} is not its directory {vertical!r}")
    if not isinstance(front["channels"], list) or any(c not in CHANNELS for c in front["channels"]):
        raise KnowledgeError(f"{name}: channels must be a list of {', '.join(CHANNELS)}")
    tags = front["tags"]
    if not isinstance(tags, list) or any(not isinstance(t, str) or not TAG.fullmatch(t) for t in tags):
        raise KnowledgeError(f"{name}: tags must be a list of lowercase words (letters, digits, ':' and '-')")
    _check_sources(front["sources"], name)
    if front["confidence"] not in CONFIDENCE:
        raise KnowledgeError(f"{name}: confidence must be one of {', '.join(CONFIDENCE)}")
    if front["reviewed_by"] not in (*REVIEWERS, PENDING):
        raise KnowledgeError(f"{name}: reviewed_by must be one of {', '.join((*REVIEWERS, PENDING))}")
    if not _is_date(front["reviewed_at"]):
        raise KnowledgeError(f"{name}: reviewed_at must be a date (2026-10-09)")


def _check_sources(sources, name: str) -> None:
    if not isinstance(sources, list) or not sources:
        raise KnowledgeError(f"{name}: sources must list at least one source")
    seen: set[str] = set()
    for s in sources:
        if not isinstance(s, dict) or set(s) not in ({"id", "url", "as_of"}, {"id", "doc", "as_of"}):
            raise KnowledgeError(f"{name}: each source has an id, a url or a doc, and as_of")
        sid = s["id"]
        if not isinstance(sid, str) or not SOURCE_ID.fullmatch(sid):
            raise KnowledgeError(f"{name}: a source id must be lowercase letters, digits and '-'")
        if sid in seen:
            raise KnowledgeError(f"{name}: the source id {sid!r} is listed twice")
        seen.add(sid)
        if "url" in s and not (isinstance(s["url"], str) and s["url"].startswith(("http://", "https://"))):
            raise KnowledgeError(f"{name}: a source's url starts with http:// or https://")
        if "doc" in s and not (
            isinstance(s["doc"], str) and s["doc"] and not s["doc"].startswith("/") and ".." not in Path(s["doc"]).parts
        ):
            raise KnowledgeError(f"{name}: a source's doc is a path inside the repository")
        if not _is_date(s["as_of"]):
            raise KnowledgeError(f"{name}: a source's as_of must be a date (2026-10-05)")


def _units(text: str) -> list[str]:
    """A section's paragraphs and '- ' items, each joined onto one line (an item's wrapped lines stay with it)."""
    units: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or (line.startswith("- ") and current):
            if current:
                units.append(" ".join(current))
            current = []
        if line.strip():
            current.append(line.strip())
    if current:
        units.append(" ".join(current))
    return units


def _body(body: str, name: str) -> tuple[str, dict[str, str]]:
    """(the summary, {section: its text}): a '# ' title, one summary sentence, then the four sections in order."""
    lines = body.strip("\n").splitlines()
    if not lines or not lines[0].startswith("# "):
        raise KnowledgeError(f"{name}: the body starts with a '# ' title")
    parts = re.split(r"^## ", "\n".join(lines[1:]), flags=re.MULTILINE)
    summary = parts[0].strip()
    # The summary becomes a cell of INDEX.md's table: no '|' (the cell's edge) and no '<' or '>' (the table's markers
    # are HTML comments, and one holding `<!-- files:end -->` ended the table early: write_index grew INDEX.md on every
    # run, and served_index_text, which filters up to the first end marker, served the rows after it -- plan P10)
    if (
        not summary
        or "\n" in summary
        or len(summary) > SUMMARY_MAX
        or not summary.endswith(".")
        or re.search(r"[\d|<>]", summary)
    ):
        raise KnowledgeError(
            f"{name}: under the title comes the summary: one sentence on one line, at most {SUMMARY_MAX} characters, "
            "ending with a full stop, with no digit and none of '|', '<' or '>'"
        )
    pairs = [(p.partition("\n")[0].strip(), p.partition("\n")[2].strip()) for p in parts[1:]]
    found = [heading for heading, _ in pairs]
    if found != list(SECTIONS):
        raise KnowledgeError(
            f"{name}: the sections must be exactly {', '.join(SECTIONS)}, in this order "
            f"(found {', '.join(found) or 'none'})"
        )
    sections = dict(pairs)
    words = len([w for w in sections["Key points"].split() if w != "-"])
    if words > KEY_POINTS_MAX_WORDS:
        raise KnowledgeError(f"{name}: Key points has {words} words; at most {KEY_POINTS_MAX_WORDS}")
    for section in ("Key points", "Numbers", "Do not say"):
        units = _units(sections[section])
        none_ok = section != "Key points" and units == ["None."]
        if not units or not (none_ok or all(u.startswith("- ") for u in units)):
            more = "" if section == "Key points" else " (or None.)"
            raise KnowledgeError(f"{name}: {section} is a list of '- ' lines{more}")
    return summary, sections


def _check_citations(sections: dict[str, str], sources: list[dict], name: str) -> None:
    """Every [id] a line cites is one of the file's sources; every paragraph or item that holds a digit cites one,
    and so does every Numbers item, digit or not ("About half of callers book."); and every source is cited somewhere
    (spec §6.1: Numbers each with its source id; plan P12)."""
    ids = {s["id"] for s in sources}
    used: set[str] = set()
    for section in SECTIONS:
        for unit in _units(sections[section]):
            cites = set(CITATION.findall(unit))
            unknown = sorted(cites - ids)
            if unknown:
                raise KnowledgeError(f"{name}: a line cites {unknown[0]}, which its sources do not list")
            states_number = bool(DIGIT.search(CITATION.sub("", unit))) or (section == "Numbers" and unit != "None.")
            if states_number and not cites:
                raise KnowledgeError(f"{name}: a line states a number without a source: {unit[:60]!r}")
            used |= cites
    never = sorted(ids - used)
    if never:
        raise KnowledgeError(f"{name}: the source {never[0]} is never cited")


INDEX = "INDEX.md"
FILES_BEGIN, FILES_END = "<!-- files:begin -->", "<!-- files:end -->"
INDEX_HEAD = (
    "| Id | Channels | Tags | Summary | Sources | Confidence | Reviewed |",
    "|---|---|---|---|---|---|---|",
)


def index_block(entries: Iterable[Entry]) -> str:
    """INDEX.md's table of files (spec §6.1: one line per file -- its tags, a one-sentence summary, the number of
    sources, confidence, the review date), in id order; channels "any" when the file names none."""
    rows = list(INDEX_HEAD)
    for e in sorted(entries, key=lambda e: e.id):
        rows.append(
            f"| {e.id} | {', '.join(e.channels) or 'any'} | {', '.join(e.tags)} | {e.summary} | {len(e.sources)} | "
            f"{e.confidence} | {e.reviewed_at.isoformat()} ({e.reviewed_by}) |"
        )
    return "\n".join(rows)


def index_text(root: Path | None = None) -> str:
    """INDEX.md as it ships: the `admiry://knowledge` resource."""
    return _read((root or KNOWLEDGE_DIR) / INDEX, INDEX)


def _split_index(root: Path | None) -> tuple[str, str, str]:
    """INDEX.md cut at its two markers: (what comes before the first, what lies between them as written, what follows
    the second)."""
    head, begin, rest = index_text(root).partition(FILES_BEGIN)
    block, end, tail = rest.partition(FILES_END)
    if not begin or not end:
        raise KnowledgeError(f"{INDEX}: the {FILES_BEGIN} and {FILES_END} lines are missing")
    return head, block, tail


def _index_parts(root: Path) -> tuple[str, str, str]:
    head, block, tail = _split_index(root)
    return head, block.strip("\n"), tail


def _is_pending_row(line: str) -> bool:
    """Whether a line is the files table's row of a file waiting for its review: `index_block` ends a row with
    `<date> (<reviewed_by>) |`."""
    return line.startswith("|") and line.rstrip().endswith(f"({PENDING}) |")


def served_index_text(root: Path | None = None) -> str:
    """INDEX.md as the `admiry://knowledge` resource serves it: the file as it ships, but for the rows of the files
    waiting for their review, which the table lists for the founder's review and no resource may show (plan P10). It
    reads INDEX.md alone, never the files: a file that breaks the format stops nothing here."""
    head, block, tail = _split_index(root)
    rows = "\n".join(row for row in block.split("\n") if not _is_pending_row(row))
    return f"{head}{FILES_BEGIN}{rows}{FILES_END}{tail}"


def index_files(root: Path | None = None) -> str:
    """What INDEX.md holds between its two markers."""
    return _index_parts(root or KNOWLEDGE_DIR)[1]


def write_index(root: Path | None = None) -> bool:
    """INDEX.md's table rewritten from the files (`python -m agent.knowledge_index index`, after a file is added or
    changed; K4's agent.research calls it too); the rest of INDEX.md is kept. True when the table changed."""
    root = root or KNOWLEDGE_DIR
    head, block, tail = _index_parts(root)
    new = index_block(load(root))
    if new == block:
        return False
    (root / INDEX).write_text(f"{head}{FILES_BEGIN}\n{new}\n{FILES_END}{tail}", encoding="utf-8")
    return True


SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Retrieved:
    """What one model call reads (`text`: the files' blocks) and which files they are (`ids`, in that order)."""

    text: str
    ids: list[str]


@functools.cache
def _loaded(root: Path) -> tuple[Entry, ...]:
    return tuple(load(root))


def forget() -> None:
    """Drop what was loaded: the files ship with the image and never change while it runs; tests write new ones."""
    _loaded.cache_clear()


def _served() -> list[Entry]:
    """The reviewed files of KNOWLEDGE_DIR (read at call time). A file that breaks the format stops none of the callers:
    nothing is read until it is fixed, the plan is built without knowledge, and the error is logged (the knowledge
    test keeps such a file off main; CI runs nightly, so a box may still meet one)."""
    try:
        return [e for e in _loaded(KNOWLEDGE_DIR) if e.reviewed_by != PENDING]
    except KnowledgeError as e:
        log.error("knowledge: %s; no file is read until it is fixed", e)
        return []


def block(entry: Entry) -> str:
    """What a model reads of one file (plan P6): `== <id> ==`, its Key points, then its Do not say lines; the source
    ids taken out -- the model reads the knowledge, the reviewer reads the sources."""
    ids = {s["id"] for s in entry.sources}

    def plain(text: str) -> str:
        return re.sub(r"\s*\[([a-z0-9][a-z0-9-]*)\]", lambda m: "" if m.group(1) in ids else m.group(0), text)

    parts = [f"== {entry.id} ==", plain(key_points(entry))]
    dont = do_not_say(entry)
    if dont:
        parts += ["Do not say:", plain("\n".join(dont))]
    return "\n".join(parts)


def _on_channel(entry: Entry, channels: tuple[str, ...]) -> bool:
    return not channels or not entry.channels or bool(set(entry.channels) & set(channels))


def _rank(entry: Entry, topics: tuple[str, ...]) -> int | None:
    """The entry's place in the caller's topics (its topic or one of its tags), None when it matches none; 0 when the
    caller names none."""
    if not topics:
        return 0
    return next((i for i, t in enumerate(topics) if t == entry.topic or t in entry.tags), None)


def _fit(entries: list[Entry], budget_tokens: int) -> Retrieved:
    """Whole blocks in order while they fit; a block that does not fit is skipped and the next one tried. A block is
    read from its file here, after the load was cached: a file that changed or went since is logged and skipped the
    same way, so a plan never stops on it."""
    blocks: list[str] = []
    ids: list[str] = []
    used = 0
    for e in entries:
        try:
            b = block(e)
        except KnowledgeError as err:
            log.error("knowledge: %s; this file is left out until it is fixed", err)
            continue
        cost = tokens(b) + (tokens(SEPARATOR) if blocks else 0)
        if used + cost > budget_tokens:
            continue
        blocks.append(b)
        ids.append(e.id)
        used += cost
    return Retrieved(SEPARATOR.join(blocks), ids)


def select(
    entries: Iterable[Entry],
    vertical: str,
    *,
    channels: Iterable[str] = (),
    topics: Iterable[str] = (),
    budget_tokens: int = BUDGET_TOKENS,
) -> Retrieved:
    """The vertical's files for these channels (a file naming none fits every channel) and these topics (none named:
    all), the caller's topics first, then the newest review, then the id; whole blocks within the budget."""
    chans, wanted = tuple(channels), tuple(topics)
    ranked = []
    for e in entries:
        rank = _rank(e, wanted)
        if e.vertical == vertical and rank is not None and _on_channel(e, chans):
            ranked.append((rank, -e.reviewed_at.toordinal(), e.id, e))
    ranked.sort(key=lambda r: r[:3])
    return _fit([r[3] for r in ranked], budget_tokens)


def retrieve(
    vertical: str, *, channels: Iterable[str] = (), topics: Iterable[str] = (), budget_tokens: int = BUDGET_TOKENS
) -> Retrieved:
    """Spec §6.1's retrieval over the reviewed files (a pending one never)."""
    return select(_served(), vertical, channels=channels, topics=topics, budget_tokens=budget_tokens)


def cited(ids: Iterable[str], *, budget_tokens: int = BUDGET_TOKENS) -> Retrieved:
    """The reviewed files these ids name, in their order, each once, within the budget; an id no file has any more is
    skipped (the answerer reads what a plan cites, plan P8)."""
    by_id = {e.id: e for e in _served()}
    return _fit([by_id[i] for i in dict.fromkeys(ids) if i in by_id], budget_tokens)


def get(knowledge_id: str) -> Entry | None:
    """A reviewed file by its id, never by a path built from it (the resource, plan P11)."""
    return next((e for e in _served() if e.id == knowledge_id), None)


def file_text(entry: Entry) -> str:
    """The file verbatim: its frontmatter (sources, confidence, the review) at its head is its metadata. A file that
    went since the load is a KnowledgeError naming it, like every read of a knowledge file."""
    return _read(entry.path, entry.id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.knowledge_index", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="write INDEX.md's table of files from the files (after adding or changing one)")
    preview = sub.add_parser("preview", help="what one model call would read (files waiting for review included)")
    preview.add_argument("vertical")
    preview.add_argument("--channel", action="append", default=[], choices=CHANNELS)
    preview.add_argument("--topic", action="append", default=[])
    preview.add_argument("--budget", type=int, default=BUDGET_TOKENS)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "index":
            changed = write_index(KNOWLEDGE_DIR)
            print(f"{INDEX} " + ("written" if changed else "already lists every file"))
            return 0
        found = select(
            load(KNOWLEDGE_DIR), args.vertical, channels=args.channel, topics=args.topic, budget_tokens=args.budget
        )
    except KnowledgeError as e:
        print(e, file=sys.stderr)
        return 1
    print(found.text)
    print(
        f"-- {len(found.ids)} file(s): {', '.join(found.ids) or 'none'}; about {tokens(found.text)} of "
        f"{args.budget} tokens",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
