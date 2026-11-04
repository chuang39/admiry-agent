"""contact_support (spec 2026-09-30 support-and-feedback §4.3): an outside assistant hands the owner to a person at
Admiry. The ticket and its mail are support.py's and support_mail.py's; this is the tool around them."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import business, claims, config, envelope, support, support_mail
from .mcp_app import agent_tool, need_business, run

REASONS = ("help", "human", "dissatisfied")
MAX_MESSAGE = 2000
MAX_CONTEXT = 4000
# The claim link is a capability: it goes in data.connect_url only, which audit.SECRET_FIELDS scrubs from the stored
# result, and never in `next`, which the audit row keeps as it is. `next` names the public tool that makes a fresh one
# (spec 2026-10-01: this command is public as it is, so nothing translates its actions).
SIGN_IN_ACTIONS = envelope.actions(
    {"id": "sign_in", "label": "Sign in to get the reply", "tool": "connect_account", "primary": True}
)


def _may_get_claim_link(conn, biz: str, key) -> bool:
    """Whether `key` may be handed the business's claim link: whoever signs in through it becomes the owner of a business
    that has none, so only a draft's own manage key gets one -- the rule request_connect_link keeps. A business an
    operator claimed or verified before anyone signed in has no owner_email either, so its ticket waits in
    awaiting_contact too, but it is no draft: a link to it would hand the business to a stranger."""
    row = business.row(conn, biz)
    return row is not None and row["claim_state"] == "draft" and "manage" in key.scopes


@agent_tool(cls="W", scope="read")
def contact_support(message: str, summary: str, reason: str, idempotency_key: str, context: str | None = None) -> dict:
    """Hand the owner to a person at Admiry. Call it when the owner asks for a person or a representative, needs help
    Admiry's tools cannot give (billing, their account, a problem), or is unhappy with Admiry's answers or plan.
    `message`: the owner's own words as they said them (up to 2,000 characters). `summary`: one sentence of what they
    need. `reason`: human, help or dissatisfied. `context`: optionally the last few turns of this conversation (up to
    4,000 characters), so the person sees what was said. Then show `say` to the owner: the request is with the Admiry
    team and the reply comes by e-mail. A second call while the first request is open adds to it. When data.connect_url
    is present the owner has not signed in yet: give them that link so the reply can reach them (`connect_account`
    gives a fresh one)."""

    def body(conn, p):
        biz = need_business(p)
        if not config.load().support:
            raise ToolError(support.NOT_AVAILABLE)
        if reason not in REASONS:
            raise ToolError(f"reason must be one of {', '.join(REASONS)}")
        words = (message or "").strip()
        if not words or len(words) > MAX_MESSAGE:
            raise ToolError(f"message must be the owner's own words, 1 to {MAX_MESSAGE} characters")
        if context is not None and len(context) > MAX_CONTEXT:
            raise ToolError(f"context must be at most {MAX_CONTEXT} characters")
        try:
            out = support.open_or_append(
                conn,
                business_id=biz,
                source=support.source_of(p.key),
                reason=reason,
                words=words,
                summary=summary,
                actor=p.actor,
                context=(context or "").strip() or None,
                ip=p.ip,
                draft=p.key.cls == "draft",
            )
        except support.Limited as e:
            raise ToolError(str(e)) from e
        data = {"support": support.public(out)}
        nxt = envelope.NONE
        if out["status"] == "awaiting_contact" and _may_get_claim_link(conn, biz, p.key):
            data["connect_url"], _ = claims.new_link(conn, biz)
            nxt = SIGN_IN_ACTIONS
        return envelope.make(data, say=support.say_for(out), next=nxt)

    return run(
        "contact_support",
        "read",
        {
            "message": message,
            "summary": summary,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "context": context,
        },
        body,
        idempotency_key=idempotency_key,
        after=support_mail.after_tool,
    )
