"""The operator reads the knowledge's review dates and how often plans cite it (capability layer K3; spec 2026-10-07
§11: the review date shown to the operator; plan P15). Reads files and counts: no business's own data, no audit row."""

from psycopg.types.json import Jsonb

from agent import knowledge_index as ki
from agent import ops
from agent.tests import knowledge_fixtures as kf
from agent.tests.fixtures import business_with_key


def test_the_operator_reads_each_files_review_and_how_often_plans_cite_it(conn, tmp_path, monkeypatch, capsys):
    root = kf.use(monkeypatch, kf.tree(tmp_path))
    kf.write(root, "dentists", "offers", channels=(), reviewed_at="2025-01-02")
    ki.forget()
    business_with_key(conn)  # biz_1
    audited = conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]
    for n, ids in enumerate((["dentists/compliance"], ["dentists/compliance", "dentists/gone"], [])):
        payload = {"sections": {"consultation": {}, "rationale": {"knowledge_ids": ids}}, "lines": {}}
        conn.execute(
            "INSERT INTO plan (id, business_id, version, json, status) VALUES (%s, 'biz_1', %s, %s, 'draft')",
            (f"pl_k3_{n}", n + 1, Jsonb(payload)),
        )
    assert ops.main(["knowledge"]) == 0
    lines = capsys.readouterr().out.splitlines()
    line = next(x for x in lines if x.startswith("dentists/compliance "))
    assert "reviewed 2026-10-09 by founder" in line and line.endswith("cited=2")
    offers = next(x for x in lines if x.startswith("dentists/offers "))
    assert "reviewed 2025-01-02 by founder" in offers and "review due" in offers and offers.endswith("cited=0")
    assert "dentists/gone cited=1 (no such file now)" in lines
    assert lines[-1] == "2 of 3 plan versions from a consultation in the last 30 days cite knowledge"
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == audited  # no audit row


def test_only_consultation_plan_versions_of_the_last_days_are_counted(conn, tmp_path, monkeypatch, capsys):
    kf.use(monkeypatch, kf.tree(tmp_path))
    business_with_key(conn)  # biz_1
    cited = {"rationale": {"knowledge_ids": ["dentists/compliance"]}}
    for version, sections, days_old in (
        (1, {"consultation": {}, **cited}, 2),
        (2, {"consultation": {}, **cited}, 40),  # before the default window
        (3, dict(cited), 2),  # no consultation section: the legacy path, never a plan an owner was advised on
        (4, {"consultation": {}, "rationale": {"why_primary": "x"}}, 3),  # from before K3: no knowledge_ids at all
    ):
        conn.execute(
            "INSERT INTO plan (id, business_id, version, json, status, created_at) "
            "VALUES (%s, 'biz_1', %s, %s, 'draft', now() - make_interval(days => %s))",
            (f"pl_k3_{version}", version, Jsonb({"sections": sections, "lines": {}}), days_old),
        )
    assert ops.main(["knowledge"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert next(x for x in lines if x.startswith("dentists/compliance ")).endswith("cited=1")
    assert lines[-1] == "1 of 2 plan versions from a consultation in the last 30 days cite knowledge"
    assert ops.main(["knowledge", "--days", "60"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert next(x for x in lines if x.startswith("dentists/compliance ")).endswith("cited=2")
    assert lines[-1] == "2 of 3 plan versions from a consultation in the last 60 days cite knowledge"


def test_a_file_that_breaks_the_format_is_named_and_the_command_fails(conn, tmp_path, monkeypatch, capsys):
    root = kf.use(monkeypatch, kf.tree(tmp_path))
    (root / "dentists" / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
    assert ops.main(["knowledge"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("dentists/broken:")
