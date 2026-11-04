"""approve_plan (P, spend class) and the account-side proposal tools (spec §4.0)."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from . import ads_mode, ads_run, business, config, envelope, launch_review, plans, proposals, tools_hosting
from .mcp_app import agent_tool, need_business, run

# What asking to launch answers as its `status` (the public contract lists them): the request waits for the owner,
# or the business is verified first
LAUNCH_STATUSES = ("proposed", "needs_verification")
PROPOSED, NEEDS_VERIFICATION = LAUNCH_STATUSES


def _refuse_setup_key(p) -> None:
    """The 24-hour draft key from `start` reads and drafts; it never approves or confirms (spec §4.0)."""
    if p.key.cls == "draft":
        raise ToolError(
            f"a setup key cannot approve or confirm spending; finish setup at {config.load().web_base_url}/connect"
        )


def _asked(data: dict) -> dict:
    """approve_plan's answer: its sentence as before, and as `next` the page the owner goes to now (final review C,
    minor 3) -- the approval page of the request, or the page where the owner finishes setting up."""
    if data["status"] == NEEDS_VERIFICATION:
        nxt = envelope.link(data["verify_url"], "Finish setting up")
    elif data.get("approval_url"):
        nxt = envelope.link(data["approval_url"], "Open the approval page")
    else:
        nxt = envelope.NONE
    return envelope.make(data, say=envelope.SAY["approve_plan"](data), next=nxt)


@agent_tool(cls="P", scope="manage", approval="code")
def approve_plan(plan_id: str, idempotency_key: str) -> dict:
    """Ask to launch this plan: publish its pages, connect tracking and start the channels within
    the monthly limit. It spends money, so it returns a proposal the owner confirms with the
    approval code from approval_url (their plan page); then call confirm_proposal with that code.
    The business must be verified first; otherwise this returns status needs_verification with
    the page where the owner finishes setup — once verified, call again with a new
    idempotency_key (a repeat of the old key replays the old answer). The 24-hour setup key from
    `start` cannot call this."""

    def body(conn, p):
        _refuse_setup_key(p)
        biz = need_business(p)
        if business.row(conn, biz)["claim_state"] != "verified":
            return _asked({"status": NEEDS_VERIFICATION, "verify_url": f"{config.load().web_base_url}/connect"})
        # a second ask keeps the request the owner already has (final review B, Important 1): the owner's page gave a
        # code for the request open when the owner looked, as pages.plan_code does. Only a draft's: propose_plan refuses
        # an unknown plan and one that is not a draft, so an open request of a replaced or approved plan is never given
        plan = plans.get(conn, biz, plan_id)
        draft = plan is not None and plan["status"] == "draft"
        waiting = proposals.open_for_plan(conn, biz, plan_id) if draft else None
        prop = waiting or proposals.propose_plan(conn, biz, plan_id)
        return _asked({"status": PROPOSED, **prop})

    return run(
        "approve_plan",
        "manage",
        {"plan_id": plan_id, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
    )


@agent_tool(cls="RO", scope="read")
def list_proposals() -> dict:
    """Open proposals for this business: what each would change, whether it needs the owner's
    approval code, and when it expires. Call before confirm_proposal."""

    def body(conn, p):
        return {"proposals": proposals.list_open(conn, need_business(p))}

    return run("list_proposals", "read", {}, body)


@agent_tool(cls="RO", scope="read")
def get_proposal(proposal_id: str) -> dict:
    """One proposal: summary, what changes, approval_url and status."""

    def body(conn, p):
        prop = proposals.get(conn, need_business(p), proposal_id)
        if prop is None:
            raise ToolError("no proposal with that id for this business")
        return prop

    return run("get_proposal", "read", {"proposal_id": proposal_id}, body)


@agent_tool(cls="W", scope="manage")
def confirm_proposal(proposal_id: str, idempotency_key: str, approval_code: str | None = None) -> dict:
    """Carry out a proposal after the owner agreed. Proposals that spend money (launching a plan,
    raising the limit) need the approval code the owner reads from their plan page: relay it
    exactly as they say it, e.g. "48-311", never guess one. Other proposals are confirmed on the
    owner's word (a daily limit applies). Use a new idempotency_key per confirmation. The 24-hour
    setup key from `start` cannot call this. Confirming a plan starts its Google Search ads:
    execution.ads says where they are (live, queued while Admiry retries, off) and the answer's
    sentence says it for the owner. Confirming a publish_page or unpublish_page proposal (A3p-2) also
    runs the 14-rule check and e-mails the report, or pauses the ads first, the same way. With Admiry's
    launch review on, confirming a business's first plan sends it to review instead (data.plan_status
    in_review): Admiry checks the ads and they go live within one business day."""
    approved: list[str] = []
    reviewed: list[str] = []
    touched: dict[str, str] = {}

    def body(conn, p):
        _refuse_setup_key(p)
        biz = need_business(p)
        try:
            out = proposals.confirm(conn, biz, proposal_id, code=approval_code)
        except ToolError as e:
            # Addendum 3 (founder 2026-10-13): a launch refused for a link not made names neither way while the owner
            # has chosen none -- never "since your ads run in your own account"; once a way is chosen, as it was
            said = ads_mode.unchosen_words(conn, biz, str(e))
            if said == str(e):
                raise
            raise ToolError(said) from e
        if out.get("plan_status") == "approved":
            approved.append(biz)
        if out.get("plan_status") == "in_review":
            reviewed.append(biz)
        if out.get("kind") in tools_hosting.AFTER_COMMIT:
            touched["biz"], touched["kind"] = biz, out["kind"]
        return out

    result = run(
        "confirm_proposal",
        "manage",
        {
            "proposal_id": proposal_id,
            "approval_code": "***" if approval_code else None,
            "idempotency_key": idempotency_key,
        },
        body,
        idempotency_key=idempotency_key,
    )
    if approved:  # P6: after run() committed the approval, the launch itself, in this request (never raises)
        result = _with_launch(result, ads_run.launch_now(approved[0]))
    if reviewed:  # L1: a first launch sent to Admiry's review instead -- nothing to launch here
        result = {**result, "say": launch_review.IN_REVIEW_SAY}
    if touched:  # A3p-2 H8/H11: the same after-the-commit shape, keyed by the confirmed proposal's own kind
        result = tools_hosting.AFTER_COMMIT[touched["kind"]](result, touched["biz"])
    return result


def _with_launch(result: dict, launched: dict) -> dict:
    """The envelope with the launch's outcome (`ads_run.launch_now`: {"channels": {channel: {"state", "say"}},
    "say"}): `execution[channel]` is where each channel's ads are now -- and `execution.ads`, Google Search's mirror
    for one release -- and the owner's sentence says it. The audit row keeps the approval's own answer (a replay of
    this call answers with that one)."""
    execution = dict(result["data"].get("execution") or {})
    for channel, outcome in launched["channels"].items():
        execution[channel] = outcome["state"]
    if proposals.GOOGLE in launched["channels"]:
        execution["ads"] = execution[proposals.GOOGLE]
    data = {**result["data"], "execution": execution}
    return {**result, "data": data, "say": " ".join(s for s in (result["say"], launched["say"]) if s)}


@agent_tool(cls="W", scope="manage")
def decline_proposal(proposal_id: str, idempotency_key: str, reason: str | None = None) -> dict:
    """Close a proposal the owner does not want, with their reason in a few words."""

    def body(conn, p):
        return proposals.decline(conn, need_business(p), proposal_id, reason=reason)

    return run(
        "decline_proposal",
        "manage",
        {"proposal_id": proposal_id, "reason": reason, "idempotency_key": idempotency_key},
        body,
        idempotency_key=idempotency_key,
    )
