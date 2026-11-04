"""What a public tool needs to know before it picks its command (spec 2026-10-01 §3; plan G1 Task 9): the caller's
business, its claim and the scopes of its key, its open consultation, its latest plan and its open proposals; for
the owner's yes (Task 18), the plan asked about and whether the business's Admiry-hosted page is up, and at which
version; and, for a pause or a resume sent again (final review A P1), where the ads have a row and where the owner's
pause stands.

Read once, under the caller's own key, with no audit row: the command the tool then runs authenticates again and
writes the one row (one public call, one command, one row). `NOWHERE` is a caller that could not be resolved here: no
key, an unknown, revoked or expired key, a key without the scope the tool needs, a business that is not active. The tool
still dispatches, and the command authenticates and authorizes again under its own scope: it refuses in its own words
(recording the attempt when the key is known), or runs, for a key whose scopes it accepts. A tool reads
`business_id is None` as "caller unknown" (a platform key, which names no business, reads the same), never as "nothing
has been started". A rate limit is the one refusal raised from here, exactly as `run()` raises it: unrecorded."""

from __future__ import annotations

from dataclasses import dataclass

from .. import ads_mode, ads_run, auth, business, config, consult, db, keys, landing, launch_check, plans, proposals
from .actions import Owner


@dataclass(frozen=True)
class Where:
    business_id: str | None = None
    key_class: str | None = None
    scopes: tuple[str, ...] = ()  # the key's scopes: a tool refuses itself what its command would refuse at a cost
    # The business's claim: draft (nobody signed in for it yet, or a claim that lapsed), claimed or verified
    claim_state: str | None = None
    session: dict | None = None  # the open or ready consultation
    plan: dict | None = None  # the latest plan
    proposals: tuple = ()  # the open proposals, newest first
    # the open approve_plan proposal of the plan asked about, unless that plan is approved: one left open on an approved
    # plan waits for nothing, while one on a version since revised still reaches confirm_proposal, whose refusal says
    # to approve the newest version (and uses no code)
    launch_proposal: dict | None = None
    publish_proposal: dict | None = None  # the open publish_page proposal of the page asked about
    # What the owner's yes needs before it answers from an earlier call (Task 18): what that answer said still stands
    asked_plan: dict | None = None  # the plan asked about (the call's plan_id, the caller's own), else the latest
    hosted_page_live: bool = False  # the business's Admiry-hosted page is published now
    hosted_page_version: int | None = None  # the version of that page that is live; None while it is not up
    # What a pause or a resume sent again is held against (final review A P1): the channels the ads have a row for, in
    # card order, and those of them under the owner's pause now (ads_run.owner_paused)
    channels: tuple[str, ...] = ()
    owner_paused: tuple[str, ...] = ()
    # What a read of the plan asks with it (the founder's ruling, 2026-10-13), read only when a tool asks (`question`):
    # (the question, `next`) while get_marketing_state's own `next` for this key is the owner's choice -- the page's
    # three ways (or the page in hand's own step) while no page is settled, else how the ads run while no way is chosen
    # (tools_state.owner_question; owner-chooses review, fix round 1, item 2); None otherwise
    asks: tuple[str, dict] | None = None

    @property
    def pending_question(self) -> str | None:
        """The id of the question the open consultation waits on; None when none is open."""
        if self.session is None or self.session["status"] != "open":
            return None
        todo = consult.pending(self.session)
        return todo[0]["id"] if todo else None

    @property
    def ready_to_build(self) -> bool:
        """The consultation's questions are answered and its plan is not built yet: `ready`, or still `open` with
        nothing left to ask (its questions changed under it). `recommend` builds from both."""
        if self.session is None:
            return False
        status = self.session["status"]
        return status == "ready" or (status == "open" and not consult.pending(self.session))

    def proposal(self, kind: str) -> dict | None:
        """The newest open proposal of one kind (approve_plan, publish_page, unpublish_page)."""
        return next((p for p in self.proposals if p["kind"] == kind), None)


NOWHERE = Where()


def _hosted_live(page: dict | None) -> bool:
    """Whether a page row is the business's live Admiry-hosted page: published, and Admiry's (hosted or admiry), as
    unpublish_page decides that there is one to take down (tools_hosting.py)."""
    return page is not None and page["status"] == "published" and page.get("source") in ("hosted", "admiry")


def _publish_proposal(conn, biz: str, publish: dict | None) -> dict | None:
    """The open publish_page proposal for exactly the page a call names: its preview token, or its version."""
    if not publish:
        return None
    token, version = publish.get("token"), publish.get("version")
    if isinstance(token, str) and token:
        return proposals.open_for_publish_token(conn, biz, token)
    if isinstance(version, int) and not isinstance(version, bool):
        return proposals.open_for_publish_version(conn, biz, version)
    return None


def where(
    *, scope: str = "read", plan_id: str | None = None, publish: dict | None = None, question: bool = False
) -> Where:
    """`scope` is the scope of the tool that asks (a key that lacks it is `NOWHERE`, and the command the tool
    dispatches refuses it in its own words). `plan_id` names the plan asked about -- which is `asked_plan`, read for the
    caller's own business, and whose launch proposal is looked up unless it is approved; the latest plan when omitted.
    `publish` names the page a publish was asked for ({"token": …} or {"version": …}): `publish_proposal` is the open
    proposal for exactly that page, so the owner's yes confirms what the owner was shown and never merely the newest
    request. `question` reads `asks` too: the owner's choice get_marketing_state would lead this key to, if any."""
    settings = config.load()
    if not auth.bearer(auth.request_headers()):
        return NOWHERE
    with db.connect(settings.database_url) as conn:
        try:
            principal = auth.require(conn, scope, web_base_url=settings.web_base_url)
        except auth.RateLimited:
            raise
        except auth.Denied:
            return NOWHERE
        biz, scopes = principal.business_id, tuple(principal.key.scopes)
        if biz is None:
            return Where(key_class=principal.key.cls, scopes=scopes)
        plan = plans.latest(conn, biz)
        asked = plans.get(conn, biz, plan_id) if plan_id else plan
        page = landing.current(conn, biz)
        live = _hosted_live(page)
        ads = ads_run.rows(conn, biz)
        if question and plan is not None:
            from .. import tools_state  # a command module: loaded by now (mcp_app imports it before the surface)

            asks = tools_state.owner_question(conn, settings, biz, principal.key)
        else:
            asks = None
        return Where(
            business_id=biz,
            key_class=principal.key.cls,
            scopes=scopes,
            claim_state=business.row(conn, biz)["claim_state"],
            session=consult.open_for(conn, biz),
            plan=plan,
            proposals=tuple(proposals.list_open(conn, biz)),
            launch_proposal=(
                proposals.open_for_plan(conn, biz, asked["id"]) if asked and asked["status"] != "approved" else None
            ),
            publish_proposal=_publish_proposal(conn, biz, publish),
            asked_plan=asked,
            hosted_page_live=live,
            hosted_page_version=page["version"] if live else None,
            channels=tuple(row["channel"] for row in ads),
            owner_paused=tuple(row["channel"] for row in ads if ads_run.owner_paused(row)),
            asks=asks,
        )


def _caller_business(conn) -> str | None:
    """The business of the request's key, by the key alone -- no rate count, no touch, no audit row: read after the
    command that answered, which authenticated the caller already. None for no key, an unknown one, or one that names
    no business."""
    token = auth.bearer(auth.request_headers())
    rec = keys.lookup(conn, token) if token else None
    return rec.business_id if rec is not None else None


def owner_choice() -> Owner:
    """What a plan answer that offers a way asks of the caller's business (actions.translate), in one read -- one
    connection, one look at the key (page-first review, item 3): the page question and its ways as actions while its
    latest draft has no page settled (launch_check.page_first; the founder's ruling, 2026-10-13: the page comes before
    the money) -- or, a page made and not published yet, the step that publishes it (launch_check.page_ask; final
    review, item 1) -- and whether it has already chosen how its ads run (ads_mode.chosen; owner-chooses review, fix
    round 1, item 1). Read after the command that answered, which authenticated the caller already: by the key alone,
    with no rate count, no touch and no audit row. Neither, for a caller with no key, an unknown one, or one that names
    no business."""
    if not auth.bearer(auth.request_headers()):
        return Owner(page=None, chosen=False)
    with db.connect(config.load().database_url) as conn:
        biz = _caller_business(conn)
        if biz is None:
            return Owner(page=None, chosen=False)
        page = None
        if launch_check.page_first(conn, biz):  # the page question and its ways, or the page in hand's own step
            question, choices = launch_check.page_ask(conn, biz)
            page = question, launch_check.page_actions(choices)
        return Owner(page=page, chosen=ads_mode.chosen(conn, biz))
