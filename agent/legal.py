"""The versioned legal texts owners accept (spec 2026-09-25 D28): the Google Ads Management Agreement and, for
prepaid businesses, the Prepayment Addendum. A text is a Markdown file under agent/legal/ with
front matter `version: <v>`; that version is what agreement_acceptance.version stores, so shipping a new file
re-asks every owner. The body is a small Markdown subset rendered here (no dependency): `# ` / `## ` headings,
paragraphs (lines joined), `- ` lists and **bold**; everything else is escaped text.

v1 (spec 2026-10-11 §5.2) adds §2, the changes Admiry makes to keep the ads running; shipping it re-asks every owner
who accepted v0.1."""

from __future__ import annotations

import functools
import re
from pathlib import Path

from markupsafe import escape

AGREEMENT = "management-agreement"  # agreement_acceptance.document values
ADDENDUM = "prepayment-addendum"  # prepaid (managed) businesses, before they add funds (A1b-3)
FILES = {
    AGREEMENT: Path(__file__).parent / "legal" / "management-agreement-v1.md",
    ADDENDUM: Path(__file__).parent / "legal" / "prepayment-addendum-v0.2.md",
}
TITLES = {AGREEMENT: "Google Ads Management Agreement", ADDENDUM: "Prepayment Addendum"}
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_VERSION = re.compile(r"[A-Za-z0-9._-]{1,32}")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", str(escape(text)))


def parse(text: str) -> tuple[str, str]:
    """(version, HTML) of a legal file; ValueError when the front matter has no usable `version:`."""
    front = _FRONT.match(text)
    if front is None:
        raise ValueError("a legal file starts with front matter (---, version: …, ---)")
    meta = dict(line.split(":", 1) for line in front.group(1).splitlines() if ":" in line)
    version = meta.get("version", "").strip()
    if not _VERSION.fullmatch(version):
        raise ValueError(f"legal file version must match {_VERSION.pattern}, not {version!r}")
    html = []
    for block in (b.strip() for b in text[front.end() :].split("\n\n")):
        if not block:
            continue
        lines = block.splitlines()
        if block.startswith("## "):
            html.append(f"<h2>{_inline(block[3:])}</h2>")
        elif block.startswith("# "):
            html.append(f"<h1>{_inline(block[2:])}</h1>")
        elif all(line.startswith("- ") for line in lines):
            html.append("<ul>" + "".join(f"<li>{_inline(line[2:])}</li>" for line in lines) + "</ul>")
        else:
            html.append(f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>")
    return version, "\n".join(html)


@functools.cache
def current(document: str = AGREEMENT) -> tuple[str, str]:
    """The legal text of `document` that ships with this build: (version, HTML)."""
    return parse(FILES[document].read_text(encoding="utf-8"))
