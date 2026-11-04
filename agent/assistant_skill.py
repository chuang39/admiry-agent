"""The skill assistants read (spec 2026-10-01 §9.3; plan G1 Task 12): one Agent Skills directory, served as it is --
over HTTP (/connect/skill.md and /connect/references/<file>: what Muse reads), as MCP resources
(skill://admiry-marketing/…) and, after the protocol upgrade, through the Skills extension, whose manifest
`manifest()` already is. One file is generated: the page guide, from the page recipes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import landing

NAME = "admiry-marketing"
ROOT = Path(__file__).parent / "assistant_skills" / NAME
PAGE_GUIDE = "references/building-a-page.md"
PAGE_FAMILIES = ("dentists", "home_services", "attorneys", "fallback")  # the page recipes that exist
RULES_END = "\n## The page"  # the body up to here is what every prompt carries (spec §9.5: items 1-3)


def page_guide() -> str:
    """What a page for the ads needs, for a page built somewhere else: the destination's rules and every kind of
    business's sections, no business's own facts (`get_landing_page` with view "kit" has those)."""
    lines = [
        "# Building a page for the ads",
        "",
        (
            'Admiry writes this page itself when asked (`update_landing_page` with `action: "write"`); this guide is '
            "for a page you or the owner build somewhere else. For one business's own facts, words and tracking "
            'snippet, call `get_landing_page` with `view: "kit"` once its plan is ready. Name the finished page with '
            '`update_landing_page` (`action: "use_url"`), or put your own HTML on a private preview '
            '(`action: "preview_html"`) and publish it. In the kit, a line with no `text` is a step to take with the '
            "owner, written in its `when`: never print it."
        ),
        "",
        "## Rules for the destination",
    ]
    lines += [f"- {rule}" for rule in landing.GOOGLE_RULES]
    lines.append(
        "- For a plan with NewsBreak, the footer also carries a Terms link and the business's phone or e-mail: the "
        "check only warns without them, but NewsBreak's ads wait for both."
    )
    for family in PAGE_FAMILIES:
        recipe = landing.load_recipe(family)
        lines += ["", f"## {family.replace('_', ' ').title()}"]
        for section in recipe["sections"]:
            lines += ["", f"### {section['title']}", section["guide"]]
            if section.get("example"):
                lines.append(f"> {section['example']}")
    return "\n".join(lines) + "\n"


def files() -> dict[str, str]:
    """Every file of the skill by its path inside it, `SKILL.md` first, the generated one included."""
    out = {"SKILL.md": (ROOT / "SKILL.md").read_text(encoding="utf-8")}
    found = {f"references/{p.name}": p.read_text(encoding="utf-8") for p in (ROOT / "references").glob("*.md")}
    found[PAGE_GUIDE] = page_guide()
    out.update(sorted(found.items()))
    return out


def text(path: str) -> str | None:
    return files().get(path)


def uri(path: str) -> str:
    return f"skill://{NAME}/{path}"


def _split() -> tuple[str, str]:
    """(`SKILL.md`'s frontmatter, its body)."""
    whole = files()["SKILL.md"]
    _, front, body = whole.split("---\n", 2)
    return front, body.lstrip("\n")


def body() -> str:
    return _split()[1]


def rules() -> str:
    """Who does what, how to represent Admiry and the rules that do not bend: what a prompt puts in front of the
    model, whichever workflow it starts."""
    return body().split(RULES_END, 1)[0].rstrip() + "\n"


def frontmatter() -> dict[str, str]:
    """`name`, `description` and `version` (the one metadata key) of `SKILL.md`."""
    out: dict[str, str] = {}
    for line in _split()[0].splitlines():
        key, _, value = line.strip().partition(":")
        if key in ("name", "description", "version") and value.strip():
            out[key] = value.strip().strip('"')
    return out


def manifest() -> list[dict]:
    """Each file's URI, SHA-256 digest and size in bytes, as the Skills extension's manifest wants them."""
    out = []
    for path, content in files().items():
        raw = content.encode("utf-8")
        out.append({"uri": uri(path), "digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "size": len(raw)})
    return out
