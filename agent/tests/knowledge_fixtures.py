"""Knowledge files for the tests (capability layer K3, plan 2026-10-08-agent-k3-knowledge-v1): small valid files
written into a temporary directory that stands in for agent/knowledge. `text` builds one file as the format wants it;
each argument replaces one part."""

from __future__ import annotations

from pathlib import Path

from agent import knowledge_index as ki

SUMMARY = "How a test business is found on Google Search."
SOURCE = {"id": "src-one", "url": "https://example.com/one", "as_of": "2026-10-05"}
SOURCE_TWO = {"id": "src-two", "doc": "docs/research/2026-10-05-local-service-verticals.md", "as_of": "2026-10-05"}


def _bullets(lines: tuple[str, ...]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else "None."


def text(
    vertical: str = "dentists",
    *,
    channels: tuple[str, ...] = ("google_search",),
    tags: tuple[str, ...] = ("stage:plan",),
    sources: tuple[dict, ...] = (SOURCE,),
    confidence: str = "medium",
    reviewed_by: str = "founder",
    reviewed_at: str = "2026-10-09",
    summary: str = SUMMARY,
    key_points: tuple[str, ...] = ("Lead with the one service the owner picked.",),
    details: str = "Searches name a service and a town [src-one].",
    numbers: tuple[str, ...] = ("A lead costs $72.97 on average [src-one].",),
    do_not_say: tuple[str, ...] = ("Best dentist in town.",),
) -> str:
    front = [
        "---",
        f"vertical: {vertical}",
        f"channels: [{', '.join(channels)}]",
        f"tags: [{', '.join(tags)}]",
        "sources:",
    ]
    for s in sources:
        where = "url" if "url" in s else "doc"
        front += [f"  - id: {s['id']}", f'    {where}: "{s[where]}"', f"    as_of: {s['as_of']}"]
    front += [f"confidence: {confidence}", f"reviewed_by: {reviewed_by}", f"reviewed_at: {reviewed_at}", "---"]
    body = [
        "# Test knowledge",
        "",
        summary,
        "",
        "## Key points",
        "",
        _bullets(key_points),
        "",
        "## Details",
        "",
        details,
        "",
        "## Numbers",
        "",
        _bullets(numbers),
        "",
        "## Do not say",
        "",
        _bullets(do_not_say),
        "",
    ]
    return "\n".join(front) + "\n\n" + "\n".join(body)


def write(root: Path, vertical: str = "dentists", topic: str = "google-search", **kw) -> Path:
    """The file `<root>/<vertical>/<topic>.md`, written from `text(vertical, **kw)`; returns its path."""
    path = root / vertical / f"{topic}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text(vertical, **kw), encoding="utf-8")
    return path


INDEX_TEXT = f"# Test knowledge index\n\n{ki.FILES_BEGIN}\n{ki.FILES_END}\n\nThe end.\n"


def index(root: Path) -> Path:
    """`<root>/INDEX.md` with its two markers and nothing between them."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ki.INDEX).write_text(INDEX_TEXT, encoding="utf-8")
    return root


def use(monkeypatch, root: Path) -> Path:
    """Point knowledge_index at `root` (its KNOWLEDGE_DIR), with nothing loaded yet; returns root."""
    monkeypatch.setattr(ki, "KNOWLEDGE_DIR", root)
    ki.forget()
    return root


def tree(root: Path) -> Path:
    """A small reviewed knowledge tree: INDEX.md; dentists' compliance (every channel), google-search and offers
    (every channel); attorneys' compliance."""
    index(root)
    write(
        root,
        "dentists",
        "compliance",
        channels=(),
        key_points=("Never promise a result [src-one].",),
        do_not_say=("Pain-free [src-one].",),
    )
    write(root, "dentists", "google-search")
    write(root, "dentists", "offers", channels=(), key_points=("Lead with the practice's own offer.",))
    write(root, "attorneys", "compliance", channels=())
    return root
