"""Set-up the surface tests share: a business walked to a plan through the public tools (the dental walk of
test_tools_consult). Needs the `walk_fake` fixture (conftest.py)."""

from __future__ import annotations

from agent import benchmark
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.fixtures import business_with_key

WALK_TEXT = "I want to find everyone interested in a dentist within 5 miles of my clinic."
ANSWERS = (
    # the business on Google Maps (plan 2026-10-03-agent-listing-confirm): the walk's own, which centres the ads
    ("listing", {"choice": "gm_1"}),
    ("objective", {"choice": "bookings"}),
    ("service", {"choice": "svc_invisalign"}),
    ("customer_value", {"other": "around three thousand"}),
)


def started(client, conn, *, scopes=("read", "manage", "leads"), claim_state="verified") -> dict:
    """A business with an open consultation (the radius was read from the words): the first question is which of the
    places Google Maps lists is the business; picking it (ANSWERS) centres the ads, so where is not asked."""
    benchmark.seed(conn)
    key = business_with_key(conn, scopes=scopes, claim_state=claim_state)
    res = call_tool(
        client, "create_marketing_plan", {"message": WALK_TEXT, "website_url": "https://brightsmile.example"}, key=key
    )
    assert not res["isError"], tool_text(res)
    return {"key": key, "session_id": data(res)["session_id"]}


SETTLED_URL = "https://brightsmile.example/invisalign"


def settle_page(conn, biz: str = "biz_1", url: str = SETTLED_URL) -> dict:
    """The business's page settled -- the founder's ruling of 2026-10-13: the page comes before how the ads run and the
    money -- as update_landing_page's use_url leaves it, without the fetch: the owner's own page live at `url`, its check
    passed. Returns the page row."""
    from agent import landing

    page = landing.set_owner_url(conn, biz, url)
    rules = [{"id": rule, "ok": True, "fix": None} for rule in ("reachable", "terms", "contact")]
    landing.save_check(conn, page["id"], {"ok": True, "url": url, "rules": rules})
    return page


def planned(client, conn, *, scopes=("read", "manage", "leads"), claim_state="verified") -> dict:
    """The same business with its plan built: {"key", "session_id", "plan_id"}."""
    out = started(client, conn, scopes=scopes, claim_state=claim_state)
    for i, (qid, value) in enumerate(ANSWERS, 1):
        res = call_tool(
            client, "update_marketing_plan", {"answers": {qid: value}, "idempotency_key": f"walk-a{i}"}, key=out["key"]
        )
        assert not res["isError"], tool_text(res)
    built = call_tool(client, "get_marketing_plan", {}, key=out["key"])
    assert not built["isError"], tool_text(built)
    return {**out, "plan_id": data(built)["plan_id"]}
