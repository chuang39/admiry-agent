"""Google Ads link tool (spec 2026-09-25 rev. 2 §3; spec 2026-09-29-admiry-channels-design §4 item 4):
connect_google_ads (W) is kept as the alias of `connect_channel("google_search", account_id=…)`
(agent/tools_channels.py), which sends the invitation to the owner's own Google Ads account under this tool's name;
get_connection_status moved there too and reads every channel (M0 Task 6)."""

from __future__ import annotations

from . import tools_channels
from .mcp_app import agent_tool


@agent_tool(cls="W", scope="manage", approval="assistant")
def connect_google_ads(customer_id: str, idempotency_key: str) -> dict:
    """Link the owner's own Google Ads account to Admiry so Admiry can run their ads in it -- the same call as
    connect_channel with channel google_search. Pass the 10-digit Customer ID the owner reads at the top of their
    Google Ads page (123-456-7890). Admiry sends an invitation; the owner accepts it inside Google Ads (the returned
    instructions say where) and the business becomes verified -- get_connection_status shows the progress. The owner
    must first accept Admiry's management agreement in the Admiry conversation (connect_url): until then this
    returns status needs_agreement with connect_url; once accepted, call again with a new idempotency_key. IMPORTANT
    ACTION: confirm the Customer ID with the owner before calling. The 24-hour setup key from `start` cannot call
    this."""
    return tools_channels.connect(
        "google_search",
        idempotency_key,
        account_id=customer_id,
        tool="connect_google_ads",
        args={"customer_id": customer_id, "idempotency_key": idempotency_key},
    )
