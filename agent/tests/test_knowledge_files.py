"""The knowledge as it ships (capability layer K3; spec 2026-10-07 §6.1, §12, §13; plan P10, P14): every file loads
(frontmatter, a source for every number), INDEX.md has its line, every doc a file cites is in the repository, no file
waits for its review, and INDEX.md's taxonomy table is the research document's, counted again."""

import re
import typing
from collections import Counter
from pathlib import Path

from agent import knowledge_index as ki
from agent import models

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "docs" / "research" / "2026-10-05-local-service-verticals.md"


def test_every_file_loads():
    ki.load()  # KnowledgeError names the file and the rule it breaks


def test_the_index_lists_every_file():
    assert ki.index_files() == ki.index_block(ki.load()), "run: .venv/bin/python -m agent.knowledge_index index"


def test_the_knowledge_directory_holds_the_index_and_vertical_directories_of_markdown_files():
    def shown(directory: Path) -> list[Path]:  # a dotfile (a Finder .DS_Store) is never in git
        return [p for p in directory.iterdir() if not p.name.startswith(".")]

    assert [p.name for p in shown(ki.KNOWLEDGE_DIR) if p.is_file()] == [ki.INDEX]
    for directory in (p for p in shown(ki.KNOWLEDGE_DIR) if p.is_dir()):
        assert [p.name for p in shown(directory) if p.suffix != ".md"] == [], directory.name


def test_every_doc_a_file_cites_is_in_the_repository():
    for e in ki.load():
        for s in e.sources:
            if "doc" in s:
                assert (REPO / s["doc"].split("#")[0]).is_file(), (e.id, s["doc"])


def test_no_file_waits_for_its_review():
    """The review gate (plan P10): a file written for review says `reviewed_by: pending`; main never carries one. On
    the first batch's branch this test fails until the founder's review (Task 16), by design."""
    assert [e.id for e in ki.load() if e.reviewed_by == ki.PENDING] == []


def _cell(counts: Counter) -> str:
    return " · ".join(f"{c} {n}" for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _research_groups() -> list[tuple[str, str, int, str]]:
    """(§, group, rows, code categories) counted from the research document's §2 tables: a row's category is its Tree
    column's first name when models.Category has it, else fallback (the research's proposed families)."""
    categories = set(typing.get_args(models.Category))
    out, section, title, counts = [], None, None, Counter()
    for line in RESEARCH.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^### (2\.\d+) (.+)$", line)
        if heading or line.startswith("## 3."):
            if section:
                out.append((section, title, sum(counts.values()), _cell(counts)))
            section, title, counts = (heading.group(1), heading.group(2), Counter()) if heading else (None, None, None)
            continue
        if section and line.startswith("| ") and not line.startswith(("| Vertical", "|---")):
            tree = re.search(r"`([a-z_]+)`", line.split("|")[2]).group(1)
            counts[tree if tree in categories else "fallback"] += 1
    return out


def _index_groups() -> list[tuple[str, str, int, str]]:
    rows = []
    for line in ki.index_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if re.fullmatch(r"2\.\d+", cells[0]):
            rows.append((cells[0], cells[1], int(cells[2]), cells[3]))
    return rows


def test_the_taxonomy_table_is_the_research_documents():
    """Spec §13: the research document's 26 groups mapped to the code's 11 categories, as part of INDEX.md."""
    research = _research_groups()
    assert len(research) == 26 and sum(r[2] for r in research) == 361
    assert _index_groups() == research
