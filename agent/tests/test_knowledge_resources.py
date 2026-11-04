"""Admiry's knowledge as an owner's assistant reads it (capability layer K3; spec 2026-10-07 §6.1 "Exposure"; plan
P11): `admiry://knowledge`, the index as it ships, and `admiry://knowledge/{vertical}/{topic}`, one reviewed file
verbatim. No command runs and no row is written; a file waiting for its review is no resource."""

import asyncio
import json
import logging
from urllib.parse import unquote

import pytest

from agent import knowledge_index as ki
from agent.surface import told
from agent.tests import knowledge_fixtures as kf
from agent.tests.conftest import rpc
from agent.tests.fixtures import business_with_key


@pytest.fixture
def tree(tmp_path, monkeypatch):
    # <tmp>/agent/knowledge, as the repository lays it out: an address that climbs out of it has an `agent` to land in
    return kf.use(monkeypatch, kf.tree(tmp_path / "agent" / "knowledge"))


def _read(client, key, uri) -> dict:
    return rpc(client, "resources/read", {"uri": uri}, key=key)


@pytest.mark.parametrize("switch", ["off", "on"])
def test_the_index_and_a_file_are_resources_read_without_a_command(client, conn, tree, monkeypatch, switch):
    monkeypatch.setenv("AGENT_KNOWLEDGE", switch)  # plan P9: the resources serve either way, they never read the switch
    key = business_with_key(conn)
    listed = {r["uri"] for r in rpc(client, "resources/list", key=key)["result"]["resources"]}
    templates = rpc(client, "resources/templates/list", key=key)["result"]["resourceTemplates"]
    assert told.KNOWLEDGE_RESOURCE in listed and told.KNOWLEDGE_TEMPLATE in {t["uriTemplate"] for t in templates}
    assert {t["uriTemplate"]: t["mimeType"] for t in templates}[told.KNOWLEDGE_TEMPLATE] == "text/markdown"
    conn.execute("DELETE FROM agent_action")
    index = _read(client, key, "admiry://knowledge")["result"]["contents"][0]
    assert index["text"] == (tree / "INDEX.md").read_text(encoding="utf-8") and index["mimeType"] == "text/markdown"
    file = _read(client, key, "admiry://knowledge/dentists/compliance")["result"]["contents"][0]
    assert file["text"] == (tree / "dentists" / "compliance.md").read_text(encoding="utf-8")
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0


@pytest.mark.parametrize("uri", ["admiry://knowledge/dentists/nothing", "admiry://knowledge/plumbers/roofing"])
def test_a_file_that_is_not_there_is_refused_in_words(client, conn, tree, uri):
    key = business_with_key(conn)
    conn.execute("DELETE FROM agent_action")
    body = _read(client, key, uri)
    assert "result" not in body and "Admiry has no knowledge file" in body["error"]["message"]
    assert body["error"]["message"].endswith("admiry://knowledge lists them.")
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == 0  # a refusal writes no row either


SECRET = "SECRET: the text of a file that is no knowledge"


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("..%2F..%2Fagent/config", id="encoded slashes"),
        pytest.param("%2e%2e%2F%2e%2e%2Fagent/config", id="encoded dots too"),
    ],
)
def test_an_address_that_climbs_out_is_no_resource(client, conn, tree, address):
    """A resource read finds a file by its id among the loaded ones, never by a path built from the address. FastMCP
    decodes each segment of the address, so these two reach `{vertical}` as `../../agent`: planted where joining that
    under the knowledge root would look, the text must never come back."""
    vertical, topic = address.split("/")
    (tree.parent / "config.md").write_text(SECRET, encoding="utf-8")
    # the premise: a naive join of the decoded address under the root reads the planted file
    assert (tree / unquote(vertical) / f"{topic}.md").read_text(encoding="utf-8") == SECRET
    body = _read(client, business_with_key(conn), f"admiry://knowledge/{address}")
    assert "result" not in body and SECRET not in json.dumps(body)


def test_a_file_waiting_for_its_review_is_no_resource(client, conn, tree):
    kf.write(tree, "dentists", "offers", channels=(), reviewed_by="pending")
    ki.forget()
    body = _read(client, business_with_key(conn), "admiry://knowledge/dentists/offers")
    assert "result" not in body and "Admiry has no knowledge file dentists/offers" in body["error"]["message"]


def test_the_index_never_lists_a_file_waiting_for_its_review(client, conn, tree):
    """INDEX.md lists a file waiting for its review (the table says `(pending)`: the founder reads it on the batch
    branch), but no pending file reaches a resource (plan P10): its row is not served, the reviewed rows are."""
    kf.write(tree, "dentists", "pending-note", channels=(), reviewed_by="pending")
    ki.write_index(tree)
    assert "| dentists/pending-note |" in (tree / "INDEX.md").read_text(encoding="utf-8")  # the writer lists it
    ki.forget()
    served = _read(client, business_with_key(conn), "admiry://knowledge")["result"]["contents"][0]["text"]
    assert "pending-note" not in served and "(pending)" not in served
    assert "| dentists/compliance |" in served and "| dentists/google-search |" in served


@pytest.mark.parametrize(
    ("damage", "says"),
    [
        pytest.param(lambda path: path.unlink(), "the file cannot be read", id="gone"),
        pytest.param(lambda path: path.write_bytes(b"\xff\xfe\xfd"), "the file is not UTF-8", id="not utf-8"),
    ],
)
def test_a_file_that_went_after_the_load_is_refused_in_words(client, conn, tree, caplog, damage, says):
    """The load is cached, so a file deleted or broken on disk since is still listed: reading it is the same refusal as
    for a file nobody has, with one ERROR line that names the file and the rule."""
    key = business_with_key(conn)
    uri = "admiry://knowledge/dentists/compliance"
    assert "result" in _read(client, key, uri)  # read once: the load is cached from here on
    damage(tree / "dentists" / "compliance.md")  # and no ki.forget()
    with caplog.at_level(logging.ERROR, logger=told.__name__):
        body = _read(client, key, uri)
    assert "result" not in body
    assert (
        body["error"]["message"] == "Admiry has no knowledge file dentists/compliance; admiry://knowledge lists them."
    )
    assert [r.getMessage() for r in caplog.records if r.name == told.__name__] == [
        f"knowledge: dentists/compliance: {says}; this file is not served until it is fixed"
    ]


def test_no_knowledge_read_runs_on_the_event_loop(client, conn, tree, monkeypatch):
    """The first read parses every file, and the event loop serves the homepage, the API and /mcp. FastMCP sends a fixed
    resource's sync function to a worker thread but calls a template's function on the loop, so the file's resource
    sends its reads to one itself, as performance_resource does with its command."""
    where: list[tuple[str, str]] = []

    def watch(name: str) -> None:
        real = getattr(ki, name)

        def watched(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                where.append((name, "on the loop"))
            except RuntimeError:  # no loop runs in this thread: a worker thread
                where.append((name, "in a thread"))
            return real(*args, **kwargs)

        monkeypatch.setattr(ki, name, watched)

    for name in ("served_index_text", "get", "file_text"):
        watch(name)
    key = business_with_key(conn)
    for uri in ("admiry://knowledge", "admiry://knowledge/dentists/compliance"):
        assert "result" in _read(client, key, uri), uri
    assert where == [("served_index_text", "in a thread"), ("get", "in a thread"), ("file_text", "in a thread")]
