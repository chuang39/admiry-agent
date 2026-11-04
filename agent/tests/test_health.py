import importlib
import logging
import runpy

import pytest
import uvicorn
from fastapi.testclient import TestClient

import agent.app
from agent import config, db
from agent.app import create_app


@pytest.fixture
def http_loggers():
    """httpx's and httpcore's levels as the test found them: running agent.__main__ sets both to WARNING, for good."""
    names = ("httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    for n in names:
        logging.getLogger(n).setLevel(logging.NOTSET)
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def test_importing_the_app_module_never_touches_the_database(monkeypatch):
    # uvicorn builds the app itself (factory mode), so importing agent.app -- which test
    # collection does -- must not need a reachable database.
    def unreachable(*args, **kwargs):
        raise AssertionError("agent.app connected to the database at import time")

    monkeypatch.setattr(db, "connect", unreachable)
    importlib.reload(agent.app)


def test_main_runs_uvicorn_on_the_app_factory(monkeypatch, http_loggers):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("PORT", "8123")
    runpy.run_module("agent.__main__", run_name="__main__")
    [(args, kwargs)] = calls
    assert args == ("agent.app:create_app",)
    assert (kwargs["factory"], kwargs["host"], kwargs["port"]) == (True, "0.0.0.0", 8123)


def test_main_keeps_the_addresses_the_http_client_reads_out_of_the_log(monkeypatch, http_loggers):
    """K4 R7 (plan D5): httpx logs every request's whole URL at INFO, and the service logs at INFO; the website of a
    business nearby is read by research-run (agent/tick.py does the same) and lives in memory for one run."""
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    runpy.run_module("agent.__main__", run_name="__main__")
    assert [logging.getLogger(n).level for n in ("httpx", "httpcore")] == [logging.WARNING, logging.WARNING]


def test_health_reports_ok_and_build_sha(monkeypatch):
    monkeypatch.setenv("BIDX_BUILD_SHA", "abc1234")
    with TestClient(create_app()) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "sha": "abc1234"}


def test_foundation_settings_treat_an_empty_value_as_unset(monkeypatch):
    for var in ("PORT", "AGENT_PUBLIC_BASE_URL", "AGENT_DATABASE_URL", "BIDX_BUILD_SHA", "AGENT_ENV"):
        monkeypatch.setenv(var, "")  # compose passes "" for an unset var
    s = config.load()
    assert (s.port, s.public_base_url, s.database_url, s.build_sha, s.env) == (
        8097,
        "http://localhost:8097",
        "postgresql:///bidx_agent",
        "dev",
        "local",
    )
    assert db.database_url() == "postgresql:///bidx_agent"  # the operator CLIs' connection too


def test_health_defaults_sha_to_dev(monkeypatch):
    monkeypatch.delenv("BIDX_BUILD_SHA", raising=False)
    with TestClient(create_app()) as c:
        assert c.get("/health").json()["sha"] == "dev"
