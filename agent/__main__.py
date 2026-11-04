import logging
import re

import uvicorn

from .config import load
from .crawl import quiet_http_logs

_CLAIM = re.compile(r"(claim=)[^&\s\"]+")


def redact_claim(record: logging.LogRecord) -> bool:
    """uvicorn.access filter: a claim link's token (/connect?claim=<token>, also inside a sign-in `next`)
    is printed as claim=***; the record is always kept."""
    if isinstance(record.args, tuple):
        record.args = tuple(_CLAIM.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
    if isinstance(record.msg, str):
        record.msg = _CLAIM.sub(r"\1***", record.msg)
    return True


def install_access_filter() -> None:
    """Before uvicorn.run: its logging dictConfig keeps a logger's existing filters."""
    logging.getLogger("uvicorn.access").addFilter(redact_claim)


if __name__ == "__main__":
    # The service's own INFO lines (one per LLM call: agent.llm) reach stderr next to uvicorn's; uvicorn's
    # loggers do not propagate, so nothing is printed twice.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_http_logs()  # the HTTP client's lines hold each request's whole URL (agent/tick.py does the same)
    install_access_filter()
    # Factory mode: uvicorn calls create_app() itself, so importing agent.app stays side-effect free.
    # timeout_keep_alive above Caddy's 2 min upstream idle default: an upstream that closes an
    # idle connection first makes the proxy 502 on reuse (the Caddy->bidder incident, 2026-09-19).
    uvicorn.run("agent.app:create_app", factory=True, host="0.0.0.0", port=load().port, timeout_keep_alive=130)
