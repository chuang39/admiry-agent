"""The service entry point: the access-log filter keeps claim tokens out of uvicorn's lines."""

import logging
import runpy

import uvicorn

# Loaded without entering sys.modules: test_health runs agent.__main__ as __main__ through runpy, which warns
# when the module was imported first.
_main = runpy.run_module("agent.__main__")
redact_claim, install_access_filter = _main["redact_claim"], _main["install_access_filter"]


def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.7:5000", "GET", path, "1.1", 200),
        None,
    )


def test_the_access_log_never_prints_a_claim_token():
    record = access_record("/connect?claim=abc")
    assert redact_claim(record) is True
    assert "abc" not in record.getMessage() and "/connect?claim=***" in record.getMessage()
    record = access_record("/connect/google/start?next=/connect?claim=abc&x=1")
    redact_claim(record)
    assert "abc" not in record.getMessage() and "claim=***&x=1" in record.getMessage()
    record = access_record("/plan/pl_0123456789abcdef")
    redact_claim(record)
    assert record.getMessage().endswith('"GET /plan/pl_0123456789abcdef HTTP/1.1" 200')


def test_the_filter_survives_uvicorns_logging_setup():
    access = logging.getLogger("uvicorn.access")
    try:
        install_access_filter()
        uvicorn.Config("agent.app:create_app", factory=True).configure_logging()  # what uvicorn.run does first
        assert redact_claim in access.filters
    finally:
        access.removeFilter(redact_claim)
