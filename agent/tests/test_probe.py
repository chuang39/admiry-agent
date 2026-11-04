"""The scripted connector probe (A1b-4d): its pure pieces, and the whole run against the app in process through
the MCP SDK's own client — the code path a third-party assistant runs — with the dental walk and the refresh
rotation."""

import asyncio
import functools
import os
import secrets
from datetime import date

import httpx
import pytest

from agent import benchmark, crawl, llm, models, places, probe, tools_start
from agent.surface import gate
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture
from agent.tests.test_oauth_http import consented_code, exchange, register

READS = {
    probe.OTHER_ANSWER: models.AnswerRead(
        option_id="2000_4000", amount_usd=3000, miles=None, note=None, understood=True, services=[]
    ),
}


@pytest.fixture
def fake(monkeypatch):
    gen = FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture(), reads=READS)
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    monkeypatch.setattr(
        tools_start.crawl, "fetch_site", lambda url: [crawl.Page(url=url, title="Site", text="We do Invisalign")]
    )
    monkeypatch.setattr(
        places,
        "search",
        # the business as Google Maps lists it, close by its website and phone: the first question offers it, and the
        # probe's pick (the first option) centres the ads (plan 2026-10-03-agent-listing-confirm)
        lambda q, k, **kw: [
            places.Candidate(
                "ChIJ1",
                "Bright Smile Dental",
                "500 University Ave, Palo Alto, CA",
                37.4419,
                -122.143,
                website="https://brightsmile.example",
                phone="(650) 555-0100",
            )
        ],
    )
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "places-test-key")
    return gen


def test_pick_answer_uses_free_text_once_on_the_value_or_an_amount_question():
    amount = {"input": "amount", "options": [{"id": "500_1000"}], "other": {"allowed": True}}
    assert probe.pick_answer(amount, allow_other=True) == {"other": probe.OTHER_ANSWER}
    assert probe.pick_answer(amount, allow_other=False) == {"choice": "500_1000"}
    value = {"id": "customer_value", "input": "single", "options": [{"id": "lt_1000"}], "other": {"allowed": True}}
    assert probe.pick_answer(value, allow_other=True) == {"other": probe.OTHER_ANSWER}
    assert probe.pick_answer({**value, "other": {"allowed": False}}, allow_other=True) == {"choice": "lt_1000"}
    single = {"input": "single", "options": [{"id": "bookings"}, {"id": "calls"}], "other": {"allowed": True}}
    assert probe.pick_answer(single, allow_other=True) == {"choice": "bookings"}
    multi = {"input": "multi", "options": [{"id": "a"}, {"id": "b"}], "other": {"allowed": True}}
    assert probe.pick_answer(multi, allow_other=True) == {"choice": ["a"]}
    assert probe.pick_answer({"input": "amount", "options": [], "other": {"allowed": False}}, allow_other=False) == {
        "other": probe.OTHER_ANSWER
    }


def test_pick_answer_meets_the_website_question_first():
    """A3p-5 batch 1 Task 4: the walk now meets the website question before anything else. With a site the reading
    already named, `site_known` is offered first (Q5) and is the first option, same as any other question; with none,
    "No website" is the only option. Either way `pick_answer` needs no special case for `website` itself -- the
    generic first-option rule already does the right thing -- but the two questions a "No website" tap brings next
    (`business_name`, `services_offered`) have no options at all, so the generic no-options branch would otherwise
    answer them with OTHER_ANSWER ("around three thousand"), a name and a services list that mean nothing."""
    known = {
        "id": "website",
        "input": "website",
        "options": [{"id": "site_known", "label": "Yes: brightsmile.example"}, {"id": "none", "label": "No website"}],
        "other": {"allowed": True},
    }
    assert probe.pick_answer(known, allow_other=True) == {"choice": "site_known"}
    unknown = {
        "id": "website",
        "input": "website",
        "options": [{"id": "none", "label": "No website"}],
        "other": {"allowed": True},
    }
    assert probe.pick_answer(unknown, allow_other=True) == {"choice": "none"}
    name = {"id": "business_name", "input": "text", "options": [], "other": {"allowed": True}}
    assert probe.pick_answer(name, allow_other=True) == {"other": probe.NAME_ANSWER}
    assert probe.pick_answer(name, allow_other=False) == {"other": probe.NAME_ANSWER}  # never gated on allow_other
    services = {"id": "services_offered", "input": "text", "options": [], "other": {"allowed": True}}
    assert probe.pick_answer(services, allow_other=True) == {"other": probe.SERVICES_ANSWER}


def test_walk_ready_reads_data_ready_or_the_recommend_action_of_a_hand_off():
    assert probe.walk_ready({"data": {"ready": True}, "next": {"kind": "actions", "actions": []}})
    assert not probe.walk_ready({"data": {"ready": False}, "next": {"kind": "question", "question": {}}})
    # a hand-off that picked up a fully answered session: no `ready`, the build action as next
    handed = {"data": {"session_id": "cs_1"}, "next": {"kind": "actions", "actions": [{"tool": "get_marketing_plan"}]}}
    assert probe.walk_ready(handed)
    assert not probe.walk_ready({"data": {}, "next": {"kind": "actions", "actions": [{"tool": "connect_account"}]}})


def test_the_tool_list_is_judged_against_the_names_the_docs_publish():
    """Final review C, minor 14: the tools row compares what tools/list shows with /connect/tools.json, and names what
    only one side has."""
    assert probe.against_the_docs(["a", "b"], ["b", "a"]) == (True, "the names /connect/tools.json publishes")
    assert probe.against_the_docs(["a", "whoami"], ["a", "b"]) == (
        False,
        "listed, not in /connect/tools.json: ['whoami']; in /connect/tools.json, not listed: ['b']",
    )
    assert probe.against_the_docs(["a"], ["a", "b"]) == (False, "in /connect/tools.json, not listed: ['b']")
    assert probe.against_the_docs(["a"], None) == (False, "/connect/tools.json could not be read")


def test_render_is_a_dated_markdown_table_that_marks_failures():
    rows = [
        probe.Row("P1", "a check", "fine | really", True),
        probe.Row("P2", "another", "bad", False),
        probe.Row("P3", "noted", "x", None),
    ]
    text = probe.render(rows, base="https://x.example", when=date(2026, 9, 27), title="Probe (key)")
    assert text.startswith("#### Probe (key) — 2026-09-27 — https://x.example\n")
    assert "| P1 | a check | fine \\| really | yes |" in text
    assert "| P2 | another | bad | **no** |" in text
    assert "| P3 | noted | x |  |" in text


def test_record_puts_the_table_under_the_results_heading_newest_last(tmp_path):
    f = tmp_path / "runbook.md"
    f.write_text("# Runbook\n\n### Results\n\n(appended)\n\n## Part B\n\ntext\n")
    probe.record(f, "#### first\n\n| a |\n")
    probe.record(f, "#### second\n\n| b |\n")
    body = f.read_text()
    assert body.index("### Results") < body.index("#### first") < body.index("#### second") < body.index("## Part B")
    assert body.endswith("## Part B\n\ntext\n")
    plain = tmp_path / "plain.md"
    plain.write_text("notes")
    probe.record(plain, "#### t\n")
    assert plain.read_text() == "notes\n\n#### t\n"


def test_parse_callback_reads_code_and_state():
    assert probe.parse_callback(" http://localhost:8765/callback?code=abc&state=s1 ") == ("abc", "s1")
    assert probe.parse_callback("http://localhost:8765/callback?code=abc") == ("abc", None)
    with pytest.raises(ValueError):
        probe.parse_callback("http://localhost:8765/callback?error=access_denied")


def test_read_secret_wants_a_private_file_of_the_right_kind(tmp_path):
    f = tmp_path / "key"
    f.write_text("ak_abc\n")
    os.chmod(f, 0o644)
    with pytest.raises(ValueError, match="readable by others"):
        probe.read_secret(str(f), prefix="ak_", what="key")
    os.chmod(f, 0o600)
    assert probe.read_secret(str(f), prefix="ak_", what="key") == "ak_abc"
    with pytest.raises(ValueError, match="hand-off token"):
        probe.read_secret(str(f), prefix="hs_", what="hand-off token")


def test_file_storage_is_private(tmp_path):
    s = probe.FileStorage(tmp_path / "probe" / "host.json")
    s.save({"tokens": {"access_token": "t", "token_type": "bearer"}})
    assert (s.path.stat().st_mode & 0o777) == 0o600
    assert asyncio.run(s.get_tokens()).access_token == "t"
    assert asyncio.run(s.get_client_info()) is None


def test_main_refuses_bad_credentials_and_rotation_without_oauth(tmp_path, capsys):
    bad = tmp_path / "bad"
    bad.write_text("hello\n")
    os.chmod(bad, 0o600)
    assert probe.main(["https://x.example", "--key-file", str(bad)]) == 2
    good = tmp_path / "key"
    good.write_text("ak_abc\n")
    os.chmod(good, 0o600)
    assert probe.main(["https://x.example", "--key-file", str(good), "--rotation-check"]) == 2
    assert "--rotation-check needs --oauth" in capsys.readouterr().err


def _asgi_factory(app):
    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers, auth=auth, timeout=30
        )

    return factory


def _run(client, coro_fn, **kwargs):
    """Run the probe on the app's own event loop (the TestClient's portal), not a second loop in this thread."""
    return client.portal.call(functools.partial(coro_fn, **kwargs))


def test_the_probe_walks_the_server_in_process_through_the_sdk_client(client, conn, fake, strict_surface):
    benchmark.seed(conn)
    key = business_with_key(conn, scopes=("read", "manage", "account"), claim_state="claimed")
    rows = _run(
        client,
        probe.run,
        base="http://testserver",
        headers={"Authorization": f"Bearer {key}"},
        walk=True,
        factory=_asgi_factory(client.app),
    )
    by_id = {r.id: r for r in rows}
    assert [r.id for r in rows] == ["P1", "P2a", "P2b", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11"]
    failed = [(r.id, r.observed) for r in rows if r.ok is False]
    assert not failed, failed
    assert "HTTP 401" in by_id["P1"].observed and "resource_metadata" in by_id["P1"].observed
    assert "io.modelcontextprotocol/ui" in by_id["P3"].observed
    assert (
        "plan card on ['get_marketing_plan', 'update_marketing_budget', 'update_marketing_plan']"
        in by_id["P4"].observed
    )
    assert "27 tools" in by_id["P4"].observed and "commands hidden True" in by_id["P4"].observed
    assert by_id["P4"].observed.endswith("; the names /connect/tools.json publishes")
    assert "text/html;profile=mcp-app" in by_id["P5"].observed
    # the website first (the walk's words name none: its first option is the fixture site the reading saw, read), the
    # business on Google Maps next (its pick centres the ads), then the four the walk always asks
    assert "6 question(s); free text used True; ready True" in by_id["P7"].observed
    assert "More Invisalign bookings within 3 miles of Bright Smile Dental" in by_id["P8"].observed
    assert by_id["P9"].observed.endswith("at $1,500/mo") and by_id["P9"].ok is True
    assert by_id["P10"].ok is True and "/plan/" in by_id["P10"].observed
    # two more: the answers that give the website and pick the business on Google Maps
    assert by_id["P11"].observed.startswith("10 calls;")


def test_a_refused_step_is_a_failing_row_not_a_lost_table(client, conn, fake, strict_surface):
    # a read-only key: `create_marketing_plan` (manage scope) is refused, so the walk stops at P7 with the refusal and
    # the rows before it stand; P11 is not written because nothing was said
    key = business_with_key(conn, scopes=("read",), claim_state="claimed")
    rows = _run(
        client,
        probe.run,
        base="http://testserver",
        headers={"Authorization": f"Bearer {key}"},
        walk=True,
        factory=_asgi_factory(client.app),
    )
    assert [r.id for r in rows] == ["P1", "P2a", "P2b", "P3", "P4", "P5", "P6", "P7"]
    assert rows[-1].ok is False and rows[-1].observed.startswith("refused: ")
    assert "lacks the 'manage' scope" in rows[-1].observed  # the scope's refusal, not a name the surface turned away


def test_a_command_on_the_list_fails_the_tools_row(client, conn, strict_surface, monkeypatch):
    """A command among the public tools (the public set grown by mistake) leaves the cards as they were: only the
    absence of a command catches it."""
    monkeypatch.setattr(gate, "PUBLIC", gate.PUBLIC | {"whoami"})
    key = business_with_key(conn, scopes=("read",), claim_state="claimed")
    rows = _run(
        client,
        probe.run,
        base="http://testserver",
        headers={"Authorization": f"Bearer {key}"},
        factory=_asgi_factory(client.app),
    )
    tools_row = next(r for r in rows if r.id == "P4")
    assert "28 tools" in tools_row.observed and "plan card on ['get_marketing_plan'" in tools_row.observed
    assert tools_row.ok is False and "commands hidden False" in tools_row.observed
    assert "listed, not in /connect/tools.json: ['whoami']" in tools_row.observed


def test_a_public_tool_missing_from_the_list_fails_the_tools_row(client, conn, strict_surface, monkeypatch):
    """Final review C, minor 14: the row read the walk's tools and the cards, so a public tool missing from tools/list
    passed it. The listed names are compared with the ones /connect/tools.json publishes, and the row names what
    differs."""
    monkeypatch.setattr(gate, "PUBLIC", gate.PUBLIC - {"contact_support"})
    key = business_with_key(conn, scopes=("read",), claim_state="claimed")
    rows = _run(
        client,
        probe.run,
        base="http://testserver",
        headers={"Authorization": f"Bearer {key}"},
        factory=_asgi_factory(client.app),
    )
    tools_row = next(r for r in rows if r.id == "P4")
    assert "26 tools" in tools_row.observed and "commands hidden True" in tools_row.observed
    assert tools_row.ok is False
    assert tools_row.observed.endswith("; in /connect/tools.json, not listed: ['contact_support']")


def test_rotation_check_proves_the_refresh_family_rule(client, conn, tmp_path):
    cid = register(client)
    verifier = secrets.token_urlsafe(48)
    code = consented_code(client, conn, cid, verifier)
    tok = exchange(client, cid, code, verifier).json()
    store = probe.FileStorage(tmp_path / "store.json")
    store.save(
        {"client": {"client_id": cid, "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}, "tokens": tok}
    )
    rows = []
    _run(client, probe._rotation, base="http://testserver", storage=store, factory=_asgi_factory(client.app), rows=rows)
    assert [(r.id, r.ok) for r in rows] == [("P12", True), ("P13", True), ("P14", True)], [r.observed for r in rows]
    assert "HTTP 401 invalid_grant" in rows[1].observed
    assert "tokens" not in store.load()  # the grant is dead: the store no longer offers it
