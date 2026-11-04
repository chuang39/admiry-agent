"""B0 §3.4 (spec 2026-10-08; R1): no owner-facing sentence sends the owner to another place to resolve what stops a
launch -- "say it in the conversation", "on the plan page", "come back here". Read: every sentence in the service's
Python (docstrings and comments aside), the home's scripts (comments aside), and the pages and e-mails an owner reads
(template comments aside). The one sentence still naming the plan page is the assistants' docs (connect.py): a launch
over MCP is still confirmed with the plan page's code (spec §3.1: the MCP side is unchanged)."""

import ast
import re
from pathlib import Path

AGENT = Path(__file__).parents[1]
ELSEWHERE = re.compile(
    r"\b(?:on (?:the|their|your) plan page|in the conversation|come back here|say it to admiry|the plan page shows)\b",
    re.IGNORECASE,
)
ALLOWED = {("connect.py", "runs once the owner approves on their plan page")}  # the assistants' docs (MCP, unchanged)
ASSISTANTS_ONLY = {"connect_docs.html", "llms.txt.j2"}  # what an assistant reads, not an owner


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _python(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    skip = _docstrings(tree)
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
    ]


def _script(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", text)


def _template(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def test_no_owner_sentence_sends_the_owner_elsewhere():
    found = []
    for path in sorted(AGENT.rglob("*.py")):
        if "tests" in path.parts:
            continue
        for text in _python(path):
            hit = ELSEWHERE.search(text)
            if hit and not any(path.name == f and s in text for f, s in ALLOWED):
                found.append((path.relative_to(AGENT).as_posix(), text[:100]))
    for path in sorted((AGENT / "static").glob("*.js")):
        for hit in ELSEWHERE.finditer(_script(path.read_text())):
            found.append((path.relative_to(AGENT).as_posix(), hit.group(0)))
    for path in sorted((AGENT / "templates").rglob("*")):
        if path.suffix not in (".html", ".j2") or path.name in ASSISTANTS_ONLY:
            continue
        for hit in ELSEWHERE.finditer(_template(path.read_text())):
            found.append((path.relative_to(AGENT).as_posix(), hit.group(0)))
    assert found == []
