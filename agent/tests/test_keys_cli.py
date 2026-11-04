from agent import keys
from agent.tests.conftest import TEST_DB_URL


def test_cli_mints_a_platform_key_and_prints_it_once(conn, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_URL", TEST_DB_URL)
    assert keys.main(["mint", "--class", "platform", "--label", "ops"]) == 0
    out, err = capsys.readouterr()
    plaintext = out.strip()
    assert plaintext.startswith("ak_") and len(plaintext) == 43
    assert "class=platform" in err and "scopes=read" in err
    rec = keys.lookup(conn, plaintext)
    assert rec is not None and rec.cls == "platform" and rec.label == "ops"


def test_cli_mints_a_scoped_business_key(conn, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_URL", TEST_DB_URL)
    conn.execute("INSERT INTO business (id) VALUES ('biz_1')")
    assert (
        keys.main(
            [
                "mint",
                "--class",
                "business",
                "--business",
                "biz_1",
                "--scopes",
                "read,manage,leads",
                "--label",
                "muse",
                "--ttl-hours",
                "1",
            ]
        )
        == 0
    )
    plaintext = capsys.readouterr().out.strip()
    rec = keys.lookup(conn, plaintext)
    assert rec.business_id == "biz_1" and rec.scopes == ("read", "manage", "leads") and rec.expires_at is not None
