"""No sentence a command says names a tool (plan G1 Task 8): owners read these words, and the tools they named are
not on the public surface. Docstrings (a command's own description), bare identifiers, SQL and log lines are not
sentences. `next.actions[].tool` is where a tool is named."""

import ast
import re
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
RETIRED = (
    "whoami",
    "list_keys",
    "revoke_key",
    "get_audit_log",
    "connect_google_ads",
    "get_report",
    "describe_business",
    "get_business",
    "update_business",
    "set_goal",
    "request_connect_link",
    "connect_channel",
    "get_connection_status",
    "get_channel_status",
    "get_status_issues",
    "get_platform_statement",
    "preview_page",
    "publish_page",
    "unpublish_page",
    "create_page",
    "update_page",
    "revise_page",
    "get_page_kit",
    "list_leads",
    "set_landing_page",
    "check_landing_page",
    "get_page",
    "get_page_stats",
    "create_plan",
    "get_plan",
    "revise_plan",
    "approve_plan",
    "list_proposals",
    "get_proposal",
    "confirm_proposal",
    "decline_proposal",
    "get_balance",
    "request_payment_link",
    "list_payments",
)
PLAIN = ("start", "answer", "recommend", "adjust", "pause", "resume")  # tools that are also English words
NAMED = re.compile(r"(?<![A-Za-z_.])(" + "|".join(RETIRED) + r")(?![A-Za-z_])")
CALLED = re.compile(
    r"\b(?:call|run|calling|use|with|through|via)\s+`?(?:" + "|".join(PLAIN) + r")`?\b"
    r"|`(?:" + "|".join(PLAIN) + r")`"
    r"|\b(?:" + "|".join(PLAIN) + r")\("
    r"|\((?:" + "|".join(PLAIN) + r"), then (?:" + "|".join(PLAIN) + r")\)"
    r"|\b(?:" + "|".join(PLAIN) + r")s?\s*→"  # a step of a chain: "start → answers → recommend"
)
SQL = re.compile(r"\b(?:SELECT|INSERT INTO|UPDATE|DELETE FROM|FROM proposal|FROM agent_action)\b")
# The model's own prompts name an intent called `adjust`, not a tool.
ALLOWED_FILES = {"llm.py"}
# Templates that name tools on purpose: the connector docs, written for assistants.
DOCS_TEMPLATES = {"connect_docs.html", "llms.txt.j2"}
NOT_SHOWN = re.compile(r"<!--.*?-->|/\*.*?\*/|\{#.*?#\}|data-tool=\"[a-z_]+\"", re.DOTALL)


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _log_strings(tree: ast.AST) -> set[int]:
    """String literals passed to a logger (`log.warning("publish_page: …")`): operator text, not owner copy."""
    found = set()
    for node in ast.walk(tree):
        to_log = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        if to_log and isinstance(node.func.value, ast.Name) and node.func.value.id == "log":
            found |= {id(a) for a in node.args if isinstance(a, ast.Constant)}
    return found


def sentences_naming_a_tool() -> list[str]:
    hits = []
    for path in sorted(AGENT.rglob("*.py")):
        if "tests" in path.parts or path.name in ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text())
        skip = _docstrings(tree) | _log_strings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in skip:
                continue
            text = node.value
            if " " not in text.strip() or SQL.search(text):
                continue  # an identifier, or a query
            if NAMED.search(text) or CALLED.search(text):
                hits.append(f"{path.relative_to(AGENT.parent)}:{node.lineno}: {' '.join(text.split())[:140]}")
    return hits


def templates_naming_a_tool() -> list[str]:
    """A page or an e-mail an owner reads: comments and the homepage's own `data-tool` attributes are not shown."""
    hits = []
    for path in sorted((AGENT / "templates").rglob("*")):
        if not path.is_file() or path.name in DOCS_TEMPLATES:
            continue
        shown = NOT_SHOWN.sub("", path.read_text())
        hits += [f"{path.name}: {m.group(1)}" for m in NAMED.finditer(shown)]
    return hits


def test_no_sentence_names_a_tool():
    assert sentences_naming_a_tool() == []


def test_no_page_an_owner_reads_names_a_tool():
    assert templates_naming_a_tool() == []
