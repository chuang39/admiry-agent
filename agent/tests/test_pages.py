from datetime import datetime, timedelta, timezone

from markupsafe import escape
from psycopg.types.json import Jsonb

from agent import ads_refusal, benchmark, business, consult, llm, pages, plans, tools_consult
from agent.channels.google_search.client import RefusalDetail
from agent.tests.conftest import JARGON, call_tool, tool_text
from agent.tests.fixtures import FakeGenerator, business_with_key, knowledge_fixture, plan_draft_fixture
from agent.tests.test_web import csrf_of, sign_in


def test_plan_page_renders_the_lines_and_is_noindex(client, conn, monkeypatch):
    monkeypatch.setattr(
        llm, "generator", lambda settings=None: FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    )
    benchmark.seed(conn)
    key = business_with_key(conn)
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)
    plan = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    r = client.get(f"/plan/{plan['plan_id']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"  # the id is the capability: never cached on the way
    html = r.text
    assert '<meta name="robots" content="noindex">' in html and "Bright Smile Dental" in html
    for line in plan["lines"]:
        assert str(escape(line)) in html  # as the template escapes it ("Facebook &amp; Instagram")
    assert "Invisalign in Palo Alto" in html and "$1,300" in html and "benchmark" in html
    assert JARGON.findall(html) == []


def test_the_plan_page_shows_the_picture_that_approving_the_plan_approves(
    client, conn, newsbreak_on, monkeypatch, tmp_path
):
    """M1 Task 7 (plan P7): the owner approves on this page too, so it shows the picture NewsBreak shows with the ad --
    the one its version pinned, with the manifest's words for a screen reader -- and the line that approving the plan
    approves it; once that picture left Admiry's set, the line that says so, and no picture (fix round 1, item 4)."""
    import json

    from agent.channels.newsbreak import creative
    from agent.tests.fixtures import creative_set
    from agent.tests.test_ads_run_channels import add_plan_by_hand
    from agent.tests.test_plans_channels import home_plan

    root = creative_set(tmp_path / "creative")
    monkeypatch.setattr(creative, "ROOT", root)
    sections, _ = home_plan(conn, prepaid=True)  # a roofer's plan: Google Search and NewsBreak, the trade's picture
    plan_id = add_plan_by_hand(conn, sections, status="draft")
    html = client.get(f"/plan/{plan_id}").text
    assert '<img src="http://testserver/static/creative/home_services/1.jpg" alt="A solid green test picture"' in html
    assert "NewsBreak shows this picture with your ad; approving the plan approves it" in html
    assert JARGON.findall(html) == []
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"] = [e for e in manifest["entries"] if e["file"] != "home_services/1.jpg"]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    html = client.get(f"/plan/{plan_id}").text
    gone = (  # final review I3: running ads keep their picture while a new version waits for a new one
        "The picture for your NewsBreak ad is no longer in Admiry&#39;s set: NewsBreak ads already running keep their "
        "current picture while a new version of your plan waits for a new one"
    )
    assert gone in html and "/static/creative/" not in html


def test_unknown_plan_is_404(client):
    assert client.get("/plan/pl_0123456789abcdef").status_code == 404


def test_malformed_plan_ids_are_404_before_the_database(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("the plan page opened a connection for a malformed id")

    monkeypatch.setattr(pages.db, "connect", unreachable)
    for path in (
        "/plan/../etc",
        "/plan/pl_zz",
        "/plan/pl_nope",
        "/plan/pl_0123456789abcdeg",
        "/plan/PL_0123456789abcdef",
    ):
        assert client.get(path).status_code == 404, path


def owner_plan(client, conn, monkeypatch, *, verified=True, email="owner@example.com"):
    """A plan on a business owned (and by default verified) by the signed-in owner; returns (plan_id, business key)."""
    monkeypatch.setattr(
        llm, "generator", lambda settings=None: FakeGenerator(knowledge=knowledge_fixture(), draft=plan_draft_fixture())
    )
    benchmark.seed(conn)
    key = business_with_key(conn, claim_state="verified" if verified else "claimed")
    conn.execute("UPDATE business SET owner_email = %s WHERE id = 'biz_1'", (email,))
    call_tool(client, "describe_business", {"text": "dentist", "idempotency_key": "d1"}, key=key)
    call_tool(client, "set_goal", {"monthly_budget_usd": 2000, "idempotency_key": "g1"}, key=key)
    plan = call_tool(client, "create_plan", {"idempotency_key": "p1"}, key=key)["structuredContent"]["data"]
    return plan["plan_id"], key


def test_anonymous_and_other_accounts_see_the_plan_but_cannot_approve(client, conn, monkeypatch, google):
    plan_id, _ = owner_plan(client, conn, monkeypatch)
    html = client.get(f"/plan/{plan_id}").text
    assert f'href="/connect/google/start?next=/plan/{plan_id}"' in html and "<form" not in html
    google.email = "someone@example.com"
    sign_in(client, f"/plan/{plan_id}")
    html = client.get(f"/plan/{plan_id}").text
    assert "another Google account" in html and "<form" not in html and "Bright Smile Dental" in html


def test_a_signed_in_viewer_of_an_unclaimed_business_is_told_to_claim_it_first(client, conn, monkeypatch, google):
    plan_id, _ = owner_plan(client, conn, monkeypatch, verified=False)
    conn.execute("UPDATE business SET owner_email = NULL")
    sign_in(client, f"/plan/{plan_id}")
    html = client.get(f"/plan/{plan_id}").text
    assert "Claim the business first" in html and "another Google account" not in html and "<form" not in html


def test_the_owner_of_an_unverified_business_sees_why_there_is_no_button(client, conn, monkeypatch, google):
    plan_id, _ = owner_plan(client, conn, monkeypatch, verified=False)
    sign_in(client, f"/plan/{plan_id}")
    html = client.get(f"/plan/{plan_id}").text
    assert "must be verified" in html and "<form" not in html
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)})
    assert r.status_code == 409 and "verified" in r.text
    r = client.post(f"/plan/{plan_id}/code", data={"csrf": csrf_of(client)})
    assert r.status_code == 409 and "verified" in r.text
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 0


def test_the_owner_reads_a_code_and_the_assistant_confirms_with_it(client, conn, monkeypatch, google):
    plan_id, key = owner_plan(client, conn, monkeypatch)
    sign_in(client, f"/plan/{plan_id}")
    html = client.get(f"/plan/{plan_id}").text
    assert f'action="/plan/{plan_id}/code"' in html and f'action="/plan/{plan_id}/approve"' in html
    r = client.post(f"/plan/{plan_id}/code", data={"csrf": csrf_of(client)})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    code = r.text.split('class="code-box">')[1].split("<")[0]  # page.css's code box (ruling 2026-10-07)
    assert len(code) == 6 and code[2] == "-" and code.replace("-", "").isdigit()
    prop = conn.execute("SELECT id, created_by, class, kind FROM proposal").fetchone()
    assert prop["created_by"] == "web" and prop["class"] == "spend" and prop["kind"] == "approve_plan"
    assert conn.execute("SELECT issued_via FROM approval_code").fetchone()["issued_via"] == "plan_page"
    audited = str(conn.execute("SELECT args_redacted, result FROM agent_action").fetchall())
    assert code not in audited and code.replace("-", "") not in audited  # the code never enters an audit row
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["id"], "approval_code": code, "idempotency_key": "c1"}, key=key
    )
    assert not res["isError"], tool_text(res)
    assert res["structuredContent"]["data"]["status"] == "confirmed"
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    issued = conn.execute("SELECT approval_ref FROM agent_action WHERE tool = 'web.code_issue'").fetchone()
    assert issued["approval_ref"] == prop["id"]
    assert "Approved" in client.get(f"/plan/{plan_id}").text


def test_the_owner_approves_on_the_page_without_a_code(client, conn, monkeypatch, google):
    plan_id, key = owner_plan(client, conn, monkeypatch)
    res = call_tool(
        client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key
    )  # the assistant proposed first
    pid = res["structuredContent"]["data"]["proposal_id"]
    sign_in(client, f"/plan/{plan_id}")
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/plan/{plan_id}"
    assert (
        conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 1
    )  # the open one was used, not a second
    prop = conn.execute("SELECT status FROM proposal WHERE id = %s", (pid,)).fetchone()
    assert prop["status"] == "confirmed"
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    row = conn.execute(
        "SELECT actor, approval_kind, approval_ref, result FROM agent_action WHERE tool = 'web.approve_plan'"
    ).fetchone()
    assert row["actor"] == "web:owner@example.com" and row["approval_kind"] == "web" and row["approval_ref"] == pid
    assert row["result"]["execution"]["ads"] == "off"  # the ads switch is off here (A3 P5)
    html = client.get(f"/plan/{plan_id}").text
    assert "Approved" in html and "<form" not in html
    # A3 Task 5: the live badge, not the frozen "off" value; ruling S18: an approval under `off` queued nothing, and
    # the page says what launches it once the switch is on
    assert "ads: Not launched (execution off): approve a new version once execution is on" in html
    assert (
        client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}).status_code == 409
    )  # not a draft any more


def test_the_owners_approve_sends_a_first_launch_to_review(client, conn, monkeypatch, google):
    """L1 review I1/M8: with AGENT_LAUNCH_REVIEW on, the plan page's own Approve sends a first launch to Admiry's
    review and launches nothing; the page then shows the in-review sentence, not "Approved"."""
    from agent import launch_review

    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    plan_id, key = owner_plan(client, conn, monkeypatch)
    call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)  # proposed first
    sign_in(client, f"/plan/{plan_id}")
    r = client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/plan/{plan_id}"
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "in_review"
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0
    html = client.get(f"/plan/{plan_id}").text
    assert launch_review.IN_REVIEW_SAY in html and "Approved" not in html


def test_plan_page_shows_the_live_ads_execution_line(client, conn, monkeypatch, google):
    """A3 Task 5: the Execution list's "ads" row reads live from channel_campaign ("Live since …" / "Launching" /
    "Paused"), not the value plan.json.execution.ads froze in at approval time."""
    from datetime import UTC, datetime, timedelta

    plan_id, key = owner_plan(client, conn, monkeypatch)
    call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)
    sign_in(client, f"/plan/{plan_id}")
    client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)})
    launched_at = datetime.now(UTC) - timedelta(days=1)
    conn.execute(
        "INSERT INTO channel_campaign (business_id, channel, plan_id, state, launched_at) "
        "VALUES ('biz_1', 'google_search', %s, 'live', %s)",
        (plan_id, launched_at),
    )
    html = client.get(f"/plan/{plan_id}").text
    assert "ads: Live since yesterday" in html
    conn.execute("UPDATE channel_campaign SET state = 'paused' WHERE business_id = 'biz_1'")
    assert "ads: Paused" in client.get(f"/plan/{plan_id}").text
    conn.execute("UPDATE channel_campaign SET state = 'launching' WHERE business_id = 'biz_1'")
    assert "ads: Launching" in client.get(f"/plan/{plan_id}").text


def test_the_plan_pages_execution_list_never_shows_a_channel_id(client, conn, monkeypatch, google):
    """M0: plan.execution carries one entry per funded channel beside A3's "ads" (its mirror for one release); the
    page's list keeps showing the one "ads" line, and never a channel's raw id."""
    plan_id, key = owner_plan(client, conn, monkeypatch)
    call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)
    sign_in(client, f"/plan/{plan_id}")
    client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)})
    stored = conn.execute("SELECT json->'execution' AS e FROM plan WHERE id = %s", (plan_id,)).fetchone()["e"]
    assert stored["google_search"] == "off"  # the approval recorded the channel ...
    html = client.get(f"/plan/{plan_id}").text
    listed = html.split("<h2>Execution</h2>", 1)[1].split("</ul>", 1)[0]
    assert "ads: Not launched (execution off)" in listed and "google_search" not in listed  # ... the list names none


def test_plan_forms_need_the_csrf_token_and_the_owner(client, conn, monkeypatch, google):
    plan_id, _ = owner_plan(client, conn, monkeypatch)
    assert client.post(f"/plan/{plan_id}/code", data={"csrf": "x"}).status_code == 403  # no session at all
    google.email = "someone@example.com"
    sign_in(client, "/connect")
    assert client.post(f"/plan/{plan_id}/approve", data={"csrf": csrf_of(client)}).status_code == 403
    assert client.post(f"/plan/{plan_id}/code", data={"csrf": csrf_of(client)}).status_code == 403
    assert client.post("/plan/pl_0123456789abcdef/approve", data={"csrf": csrf_of(client)}).status_code == 404
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 0


def test_the_assistant_path_still_needs_a_code(client, conn, monkeypatch, google):
    plan_id, key = owner_plan(client, conn, monkeypatch)
    pid = call_tool(client, "approve_plan", {"plan_id": plan_id, "idempotency_key": "a1"}, key=key)[
        "structuredContent"
    ]["data"]["proposal_id"]
    res = call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "c1"}, key=key)
    assert res["isError"] and "approval code" in tool_text(res)


def test_a_code_request_racing_a_change_is_a_409_notice(client, conn, monkeypatch, google):
    from fastmcp.exceptions import ToolError

    plan_id, _ = owner_plan(client, conn, monkeypatch)
    sign_in(client, f"/plan/{plan_id}")
    for error in (
        ToolError("this plan is superseded; create or revise a plan first"),
        ValueError("proposal is confirmed"),
    ):

        def racing(*args, error=error, **kwargs):
            raise error

        monkeypatch.setattr(pages.proposals, "issue_code", racing)
        r = client.post(f"/plan/{plan_id}/code", data={"csrf": csrf_of(client)})
        assert r.status_code == 409 and str(error) in r.text and f'href="/plan/{plan_id}"' in r.text
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.code_issue'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 0  # rolled back with it


def test_the_plan_page_tells_an_unverified_owner_how_to_verify_for_each_mode(client, conn, monkeypatch, google):
    plan_id, _ = owner_plan(client, conn, monkeypatch, verified=False)
    sign_in(client, f"/plan/{plan_id}")
    html = client.get(f"/plan/{plan_id}").text
    assert "must be verified" in html and '<a href="/?setup=biz_1">Link your Google Ads account</a>' in html
    assert "Add funds" not in html and "Nothing is spent on ads until then." in html
    conn.execute("UPDATE business SET ads_mode = 'managed' WHERE id = 'biz_1'")
    html = client.get(f"/plan/{plan_id}").text
    assert (
        '<a href="/?setup=biz_1">Add funds</a> to verify and unlock launching' in html
        and "Link your Google Ads" not in html
    )
    assert "Nothing is spent on ads until then." in html
    assert "Nothing is charged" not in html  # adding funds is a charge
    assert JARGON.findall(html) == [] and "<form" not in html


def _consulted_plan(conn) -> str:
    """A plan row shaped like recommend's output (consultation / estimate.columns / rationale / levels
    sections), built the same way `recommend` does: a ready consult session, then tools_consult.build with
    a fixture draft, then plans.create_version."""
    benchmark.seed(conn)
    business_with_key(conn)
    conn.execute(
        "INSERT INTO knowledge (business_id, version, json, source) VALUES ('biz_1', 1, %s, 'extract')",
        (Jsonb(knowledge_fixture().model_dump()),),
    )
    business.set_goal(conn, "biz_1", monthly_limit_usd=2000)
    tree = consult.load_tree("dentists")
    knowledge = knowledge_fixture().model_dump()
    s = consult.create(
        conn,
        "biz_1",
        tree="dentists",
        locale="en-US",
        opening_text="dentist",
        known_facts={},
        options=consult.build_options(tree, knowledge),
        answers={
            "objective": {"choice": "bookings", "raw": "", "source": "choice"},
            "service": {"choice": "svc_invisalign", "raw": "", "source": "choice"},
            "customer_value": {"choice": "1000_plus", "amount_usd": 3000.0, "raw": "", "source": "choice"},
            "radius": {"miles": 5.0, "raw": "", "source": "choice"},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    sections, basis, notes, goal = tools_consult.build(
        conn, "biz_1", s, knowledge, level="recommended", monthly_usd=None, draft=plan_draft_fixture(), previous=None
    )
    plan = plans.create_version(
        conn,
        "biz_1",
        goal_id=goal["id"],
        sections=sections,
        estimate_basis=basis,
        rationale=sections["rationale"]["why_primary"],
        notes=notes,
    )
    return plan["id"]


def test_plan_page_shows_the_economics_and_the_reasons(client, conn):
    plan_id = _consulted_plan(conn)
    html = client.get(f"/plan/{plan_id}").text
    for text in (
        "What the budget should do in 30 days",
        "a day, about",
        "Planning",
        "Potential revenue",
        "not a forecast",
        "Why Google Search first",
        "Why not TikTok yet",
        "cost per lead",
    ):
        assert text in html, text
    assert JARGON.findall(html) == []


def test_plan_page_shows_every_funded_channel_and_the_later_lines(client, conn, newsbreak_on, pictures):
    """M0 Task 5 (spec 2026-09-29 §7.2): a prepaid roofer's plan funds Google Search and NewsBreak's $300 test; the
    page shows each with its amount, reason and who runs it, the level control's amounts, and the later lines."""
    from agent.tests.test_plans_channels import NB_WHY, home_plan

    sections, notes = home_plan(conn, prepaid=True)
    plan = plans.create_version(
        conn, "biz_1", goal_id=None, sections=sections, estimate_basis={}, rationale="", notes=notes
    )
    html = client.get(f"/plan/{plan['id']}").text
    for text in (
        "Google Search — $700 a month",
        "Google Search gets most of it because people there are already looking for Roofing near you.",
        "NewsBreak — $300 a month",
        NB_WHY,
        "Admiry runs it from your balance",
        "recommended $33 a day",
        "Facebook & Instagram — Not in this plan yet; coming to Admiry.",
        "Yelp — Not now: Yelp opens its ads only to large partners.",
        "TikTok — Worth a test after 30 days of booking data. Not now.",
    ):
        assert str(escape(text)) in html, text
    assert JARGON.findall(html) == []


def test_plan_page_never_shows_another_channels_ads_under_a_channel(client, conn, newsbreak_on, pictures):
    """Final review M6: the draft wrote no NewsBreak copy, so NewsBreak's entry carries Google Search's words for M1's
    build to fit to NewsBreak; the page shows them under Google Search alone, and NewsBreak's section says in one plain
    line that its ads come with the channel."""
    from agent.tests.test_plans_channels import home_plan

    sections, notes = home_plan(conn, prepaid=True)
    google, nb = sections["ads"]["channels"]
    assert nb["headlines"] == google["headlines"] and nb["copy_of"] == "google_search" and "copy_of" not in google
    plan = plans.create_version(
        conn, "biz_1", goal_id=None, sections=sections, estimate_basis={}, rationale="", notes=notes
    )
    html = client.get(f"/plan/{plan['id']}").text
    google_card = html.split("Google Search — $700/mo")[1].split("</section>")[0]
    nb_card = html.split("NewsBreak — $300/mo")[1].split("</section>")[0]
    headline = str(escape(google["headlines"][0]))
    assert headline in google_card and headline not in nb_card and "<table>" not in nb_card
    assert "Its ads are written from this plan when NewsBreak starts." in nb_card


def test_simple_pages_give_their_buttons_a_text_colour():
    """Founder's iPhone in dark mode (2026-09-28): the consent page's Allow / Not now were two empty boxes — a
    white background with the system's (white) button text. Every hand-written page sets the text colour."""
    from pathlib import Path

    # the consent, plan and e-mail link pages are on page.css since the mobile audit (2026-09-30)
    css = Path("agent/static/page.css").read_text()
    rule = css[css.index(":where(.page) button {") : css.index("}", css.index(":where(.page) button {"))]
    assert "background: var(--card)" in rule and "color: var(--fg)" in rule and "appearance: none" in rule
    # Settings (connect.html) has no inline style since the redesign (spec 2026-09-29-admiry-settings-redesign §5.4):
    # its buttons take their colours from settings.css
    css = Path("agent/static/settings.css").read_text()
    rule = css[css.index(":where(.settings) button {") : css.index("}", css.index(":where(.settings) button {"))]
    assert "background: var(--card)" in rule and "color: var(--fg)" in rule and "appearance: none" in rule
    assert "--card: #FFFFFF" in Path("agent/static/base.css").read_text()
    assert '<meta name="color-scheme" content="light">' in Path("agent/templates/connect.html").read_text()


def _dropped(conn, plan_id: str, business_id: str = "biz_1") -> None:
    found = RefusalDetail(
        code="policyFindingError: POLICY_FINDING",
        operation=5,
        kind="headline",
        position=1,
        field="mutate_operations[5].x",
        topic="TRADEMARKS_IN_AD_TEXT",
        type="PROHIBITED",
        exemptible=False,
        certificate=False,
        text="Same-Week Appointments",
    )
    ads_refusal.record(
        conn,
        business_id=business_id,
        plan_id=plan_id,
        channel="google_search",
        review_id=None,
        found=[(found, "ad_text", "dropped")],
    )


def test_the_plan_page_shows_the_changes_admiry_made(client, conn, monkeypatch, google):
    """spec 2026-10-11 §5.1: what Admiry removed after Google refused it, on the version's page, in owner words."""
    plan_id, _key = owner_plan(client, conn, monkeypatch)
    assert "Changes Admiry made" not in client.get(f"/plan/{plan_id}").text
    _dropped(conn, plan_id)
    html = client.get(f"/plan/{plan_id}").text
    assert "Changes Admiry made" in html and escape(ads_refusal.INTRO) in html
    assert escape("Removed the headline “Same-Week Appointments”: a name Google treats as a trademark.") in html
    assert "POLICY" not in html and JARGON.findall(html) == []
