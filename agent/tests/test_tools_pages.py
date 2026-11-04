import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from agent import ads_run, business, config, crawl, db, envelope, hosted, landing, mail, plans, proposals, tools_pages
from agent.channels.google_search import adapter as google
from agent.mcp_app import REGISTRY
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON, TEST_DB_URL, call_tool, data, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_ads_run import (  # noqa: F401 (fixtures)
    BIZ,
    GOOGLE,
    PAGE_URL,
    gads,
    google_error,
    live,
    row,
    seed,
)
from agent.tests.test_landing import _plan, fake  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import (
    DOMAIN,
    GOOD_HTML,
    GOOD_HTML_V2,
    HOSTNAME,
    URL,
    _checked,
    _launched_with_kit,
    _publish,
)
from agent.tests.test_track import _page

# A resolver that never touches real DNS, for tools that call crawl.public_host themselves
# (set_landing_page): "*.example" is RFC 2606 reserved and does not resolve on the real network.
_PUBLIC_IP = ["93.184.216.34"]
OFF_OLD_ADDRESS = ads_run.OFF_OLD_ADDRESS.format(label="Google Search")
ADDRESS_RETRY = ads_run.ADDRESS_RETRY.format(labels="Google Search")
PAGE_DOWN = f"{ads_run.PAUSE_WORDS['page_down']}. {ads_run.PAUSE_ADVICE['page_down']}"


def _execution(res) -> dict:
    """The reply's ads execution as A3p-2 shaped it -- Google Search's state and the reply's ads sentence, kept for
    one release (M0 Task 9 D12) -- once the per-channel form is checked to agree: one entry, Google Search's."""
    ex = data(res)["execution"]
    assert set(ex["channels"]) == {GOOGLE}, ex
    assert ex["channels"][GOOGLE] == {"state": ex["state"], "say": ex["say"]}, ex
    return {"state": ex["state"], "say": ex["say"]}


def _report(ok: bool, rules: list[dict] | None = None, url: str = "https://brightsmile.example/x") -> dict:
    """A minimal, valid landing.check()-shaped report for tests that mock the check itself (the 14-rule
    logic is agent/tests/test_landing.py's job; these tests are only about the tools around it)."""
    if rules is None:
        rules = [
            {
                "id": "reachable",
                "title": "Opens securely and fast",
                "ok": ok,
                "detail": "detail",
                "fix": None if ok else "Fix it.",
            }
        ]
    return {
        "ok": ok,
        "url": url,
        "rules": rules,
        "fetched": {"status": 200, "bytes": 10, "seconds": 0.1, "final_url": url},
    }


def test_get_page_kit_returns_the_kit_and_emails_it_once_per_plan(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
        ),
    )
    res = call_tool(client, "get_page_kit", {}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["plan_id"] == plan_id and env["data"]["emailed"] is True and env["data"]["email"] == "sent"
    assert env["say"].startswith("Here is everything to build the page for your Invisalign ads")
    assert "e-mailed it to owner@example.com" in env["say"] and not JARGON.search(env["say"])
    assert env["next"]["kind"] == "actions" and env["next"]["actions"][0]["tool"] == "set_landing_page"
    assert len(sent) == 1
    text = json.loads(sent[0].read())["text"]
    assert text.startswith("Everything to build the landing page") and "Reply to this e-mail" not in text
    assert f'Form: Make the form <form method="post" action="{env["data"]["tracking"]["form_action"]}">' in text
    again = call_tool(client, "get_page_kit", {}, key=key)
    assert data(again)["emailed"] is False and data(again)["email"] == "already" and len(sent) == 1  # no second mail
    assert "e-mailed this version to owner@example.com earlier" in again["structuredContent"]["say"]


def test_get_page_kit_without_mail_or_owner_says_so(client, conn, fake, monkeypatch):  # noqa: F811
    key, _biz, _ = _plan(client, conn, fake)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    env = call_tool(client, "get_page_kit", {}, key=key)["structuredContent"]
    assert env["data"]["emailed"] is False and env["data"]["email"] == "unconfigured"
    assert "e-mail is not set up on this server" in env["say"]


def test_get_page_kit_with_mail_configured_but_no_owner_email_says_so(client, conn, fake, monkeypatch):  # noqa: F811
    """Unlike the mail-unconfigured case above, this needs RESEND_API_KEY set and no owner_email: the two
    reasons are checked in that order in prepare, so this is the only way to reach "no_owner" in a test."""
    key, _biz, _ = _plan(client, conn, fake)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    env = call_tool(client, "get_page_kit", {}, key=key)["structuredContent"]
    assert env["data"]["emailed"] is False and env["data"]["email"] == "no_owner"
    assert "No owner e-mail on file yet" in env["say"]


def test_get_page_kit_says_when_the_mail_did_not_go_through(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"message": "bad from"}))
        ),
    )
    res = call_tool(client, "get_page_kit", {}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["emailed"] is False and env["data"]["email"] == "failed"
    assert "The e-mail to owner@example.com did not go through; the kit is here." in env["say"]


def test_get_page_kit_retries_after_a_failed_send(client, conn, fake, monkeypatch):  # noqa: F811
    """R6: a failed send releases the claim (kit_plan_id back to NULL) so the next call tries again; once one
    succeeds, a further call for the same version is blocked as "already" (no second HTTP call)."""
    key, biz, plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(422, json={"message": "bad from"})
        return httpx.Response(200, json={"id": "em_2"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    first = call_tool(client, "get_page_kit", {}, key=key)
    page_id = data(first)["page_id"]
    assert data(first)["email"] == "failed" and data(first)["emailed"] is False
    assert landing.get(conn, page_id)["kit_plan_id"] is None  # the claim was released
    assert calls["n"] == 1

    second = call_tool(client, "get_page_kit", {}, key=key)
    assert data(second)["email"] == "sent" and data(second)["emailed"] is True
    assert calls["n"] == 2
    page = landing.get(conn, page_id)
    assert page["kit_plan_id"] == plan_id and page["kit_sent_at"] is not None

    third = call_tool(client, "get_page_kit", {}, key=key)
    assert data(third)["email"] == "already" and data(third)["emailed"] is False
    assert calls["n"] == 2  # no third HTTP call


def test_get_page_kit_releases_the_claim_when_send_raises_unexpectedly(client, conn, fake, monkeypatch):  # noqa: F811
    """A non-MailError exception between the claim and a successful send (a bug in mail.send, a Jinja error,
    anything mail.send itself does not convert to MailError) must not strand the claim: the tool call errors,
    but the claim is released so a later call still sends normally."""
    key, biz, plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mail, "send", boom)
    res = call_tool(client, "get_page_kit", {}, key=key)
    assert res["isError"], tool_text(res)

    page = landing.current(conn, biz)
    assert page is not None and page["kit_plan_id"] is None  # the claim was released, not stranded

    sent = []
    monkeypatch.setattr(mail, "send", lambda *a, **k: (sent.append(1), "em_ok")[1])
    again = call_tool(client, "get_page_kit", {}, key=key)
    assert not again["isError"], tool_text(again)
    assert data(again)["email"] == "sent" and data(again)["emailed"] is True and len(sent) == 1
    assert landing.get(conn, page["id"])["kit_plan_id"] == plan_id


# M1 Task 4: what the kit's footer lines say about who needs them, when a funded channel's page_rules name the rules
NEWSBREAK_WHEN = "NewsBreak requires it and Google is happier with it too"
# with no business phone on file the contact line is this instruction (fix round 1), never wording to print
NO_PHONE_ASK = "Add your business phone or a business e-mail address in the footer"


def _fund_newsbreak(conn, plan_id: str, monthly_usd: float = 300.0) -> None:
    """The plan's NewsBreak entry (it copies Google's words, as the allocator's does) at `monthly_usd`."""
    plan = conn.execute("SELECT json FROM plan WHERE id = %s", (plan_id,)).fetchone()["json"]
    entries = [c for c in plan["sections"]["ads"]["channels"] if c["channel"] != "newsbreak"]
    plan["sections"]["ads"]["channels"] = [*entries, {**entries[0], "channel": "newsbreak", "monthly_usd": monthly_usd}]
    conn.execute("UPDATE plan SET json = %s WHERE id = %s", (Jsonb(plan), plan_id))


def _texts(kit: dict) -> list[str]:
    return [line["text"] for line in kit["rules"]["required_lines"]]


def test_the_kit_names_the_footer_lines_only_when_a_funded_channel_asks_for_them(client, conn, fake):  # noqa: F811
    key, biz, plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    plain = data(call_tool(client, "get_page_kit", {}, key=key))
    own = ["General Dentist", "The fee shown is the minimum fee only"]
    assert _texts(plain) == own  # the recipe's own lines, nothing more
    assert "owner@example.com" not in json.dumps(plain)  # the sign-in address is in no kit (fix round 1)
    ids = [c["id"] for c in plain["checklist"]]  # the checklist is the same for every business
    assert ids[8:11] == ["privacy", "terms", "contact"]

    _fund_newsbreak(conn, plan_id, monthly_usd=0)  # an entry with no money in it funds nothing
    assert _texts(data(call_tool(client, "get_page_kit", {}, key=key))) == own

    _fund_newsbreak(conn, plan_id)
    lines = data(call_tool(client, "get_page_kit", {}, key=key))["rules"]["required_lines"]
    assert [ln["text"] for ln in lines] == [*own, "Terms of use", "650-555-0100"]  # the phone as the facts have it
    for line in lines[2:]:
        assert line["when"].startswith("in the footer") and NEWSBREAK_WHEN in line["when"] and line["always"] is False
    assert not JARGON.search(json.dumps(lines)) and not MONEY_JARGON.search(json.dumps(lines))
    # what the check and the hosted pages read is untouched, and so is the recipe the kit copies its lines from
    assert _texts(landing.kit(conn, biz)) == own
    assert [ln["text"] for ln in landing.load_recipe("dentists")["required_lines"]] == own


def test_a_business_without_a_phone_is_asked_for_one_never_handed_an_address(client, conn, fake):  # noqa: F811
    """Fix round 1: with no phone on file the contact line is an instruction for the assistant to carry out with the
    owner -- nothing to print -- and the owner's sign-in address is never offered as wording."""
    key, biz, plan_id = _plan(client, conn, fake)
    _fund_newsbreak(conn, plan_id)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    latest = business.latest_knowledge(conn, biz)[1]
    business.add_knowledge(conn, biz, {**latest, "phone": None}, source="owner")
    kit = data(call_tool(client, "get_page_kit", {}, key=key))
    assert kit["rules"]["required_lines"][-1] == {
        "text": None,
        "when": f"{NO_PHONE_ASK}; {NEWSBREAK_WHEN}",
        "always": False,
    }
    assert "owner@example.com" not in json.dumps(kit)
    assert not JARGON.search(json.dumps(kit["rules"]["required_lines"]))


@pytest.mark.parametrize("phone", [True, False])
@pytest.mark.parametrize("owner_email", [True, False])
def test_no_placeholder_is_ever_offered_as_wording(client, conn, fake, monkeypatch, phone, owner_email):  # noqa: F811
    """Fix round 1: the kit's wording is the recipe's own lines, "Terms of use" and the business's own phone -- never
    a stand-in such as "the business's phone or e-mail", in the kit or in the e-mail that prints every line as
    `Required wording (...): "<text>"`, whether or not the business has a phone or the owner an address on file."""
    key, biz, plan_id = _plan(client, conn, fake)
    _fund_newsbreak(conn, plan_id)
    if not phone:
        latest = business.latest_knowledge(conn, biz)[1]
        business.add_knowledge(conn, biz, {**latest, "phone": None}, source="owner")
    if owner_email:
        conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent: list[str] = []

    def send(request):
        sent.append(json.loads(request.read())["text"])
        return httpx.Response(200, json={"id": "em_1"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(send)))
    kit = data(call_tool(client, "get_page_kit", {}, key=key))

    real = {*(ln["text"] for ln in landing.load_recipe("dentists")["required_lines"]), "Terms of use"}
    real |= {"650-555-0100"} if phone else set()
    lines = kit["rules"]["required_lines"]
    assert {ln["text"] for ln in lines if ln["text"]} == real  # exactly the real wording, nothing invented
    assert (lines[-1]["text"] is None) is (not phone) and (NO_PHONE_ASK in lines[-1]["when"]) is (not phone)
    assert "owner@example.com" not in json.dumps(kit)
    if owner_email:
        (text,) = sent
        printed = re.findall(r'^- Required wording \(.*\): "(.*)"$', text, re.MULTILINE)
        assert set(printed) == real
        assert (f"- {NO_PHONE_ASK}; {NEWSBREAK_WHEN}" in text) is (not phone)
        assert "owner@example.com" not in text
    else:
        assert sent == []  # no owner address on file: nothing is mailed


def test_the_kit_mail_names_the_two_lines_only_when_newsbreak_is_funded(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, plan_id = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent: list[str] = []

    def send(request):
        sent.append(json.loads(request.read())["text"])
        return httpx.Response(200, json={"id": "em_1"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(send)))
    assert data(call_tool(client, "get_page_kit", {}, key=key))["email"] == "sent"
    assert "NewsBreak" not in sent[0] and "Terms of use" not in sent[0]
    assert "Terms link in the footer" in sent[0]  # the checklist the page is judged by is the same for every business

    conn.execute("UPDATE page SET kit_plan_id = NULL")  # mailed again for the next plan version
    _fund_newsbreak(conn, plan_id)
    assert data(call_tool(client, "get_page_kit", {}, key=key))["email"] == "sent" and len(sent) == 2
    required = [ln for ln in sent[1].splitlines() if ln.startswith("- Required wording")]
    assert required[-2:] == [
        f'- Required wording (in the footer, as a link; {NEWSBREAK_WHEN}): "Terms of use"',
        f'- Required wording (in the footer; {NEWSBREAK_WHEN}): "650-555-0100"',
    ]
    assert not (JARGON.search(sent[1]) or ACCOUNT_JARGON.search(sent[1]) or MONEY_JARGON.search(sent[1]))
    assert "owner@example.com" not in sent[0] and "owner@example.com" not in sent[1]  # it is the To:, not the text


def test_get_page_kit_points_at_no_tool_that_does_not_exist():
    """`create_page` is A3p-2: the kit's description says who builds the page, and names no such tool."""
    desc = next(m.tool.description for m in REGISTRY if m.name == "get_page_kit")
    assert "create_page" not in desc and "build the page from the kit" in desc


def test_no_tool_description_names_an_internal_ruling():
    """Round 5: what an assistant reads (every MCP tool's description) never carries the plan's ruling codes (H27,
    H28, S15 ...) -- those live in code comments."""
    import re

    codes = re.compile(r"\b[A-Z]\d{1,3}\b")
    named = {m.name: codes.findall(m.tool.description or "") for m in REGISTRY}
    assert {name: found for name, found in named.items() if found} == {}


def test_kit_needs_a_plan_tool(client, conn):
    from agent.tests.fixtures import business_with_key

    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(client, "get_page_kit", {}, key=key)
    assert res["isError"] and "Build the plan first" in tool_text(res)


def test_a_form_becomes_a_lead_and_an_owner_email(client, conn, monkeypatch):
    key, page = _page(conn)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    sent = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_2"}))[1])
        ),
    )
    r = client.post(
        f"/t/form/{page['id']}",
        data={
            "name": "Ana",
            "phone": "650 555 0199",
            "service": "Invisalign",
            "preferred_time": "mornings",
            "message": "my retainer cracked",
            "_gclid": "Cj0xyz",
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True and "call you back" in r.json()["say"]
    lead = conn.execute(
        "SELECT kind, status, fields, click_id, notified_at FROM lead WHERE business_id = 'biz_1'"
    ).fetchone()
    assert (
        lead["kind"] == "form"
        and lead["status"] == "new"
        and lead["fields"]["phone"] == "650 555 0199"
        and lead["click_id"] == "Cj0xyz"
    )
    assert lead["notified_at"] is not None and len(sent) == 1
    body = sent[0].read().decode()
    assert "Ana" in body and "650 555 0199" in body and "Invisalign" in body
    assert conn.execute("SELECT count(*) AS n FROM page_event WHERE kind = 'form'").fetchone()["n"] == 1
    # the leads tool: contact under one key, the audit row without it
    res = call_tool(client, "list_leads", {}, key=key)
    assert not res["isError"], tool_text(res)
    lead_out = data(res)["leads"][0]
    assert (
        lead_out["contact"] == {"name": "Ana", "phone": "650 555 0199", "email": None} and lead_out["status"] == "new"
    )
    assert lead_out["preferred_time"] == "mornings" and lead_out["message"] == "my retainer cracked"
    # I6: the person's own words sit only under keys the audit scrubs, in list_leads and update_lead alike, and
    # update_lead's say and note never carry them either
    res = call_tool(
        client,
        "update_lead",
        {"lead_id": lead_out["id"], "status": "contacted", "note": "Ana wants Tuesday", "idempotency_key": "u1"},
        key=key,
    )
    assert res["structuredContent"]["say"] == f"Marked lead {lead_out['id']} as contacted."
    rows = conn.execute(
        "SELECT tool, args_redacted, result FROM agent_action WHERE tool IN ('list_leads', 'update_lead') ORDER BY id"
    ).fetchall()
    assert [r["tool"] for r in rows] == ["list_leads", "update_lead"]
    for audit_row in rows:
        stored = json.dumps([audit_row["args_redacted"], audit_row["result"]])
        for private in ("Ana", "650 555 0199", "mornings", "retainer", "Tuesday"):
            assert private not in stored, (audit_row["tool"], private)
    assert rows[1]["args_redacted"]["lead_note"] == "***" and rows[1]["result"]["data"]["contact"] == "***"


def test_update_lead_moves_status_and_keeps_a_note(client, conn):
    key, page = _page(conn)
    client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199"})
    lead_id = data(call_tool(client, "list_leads", {}, key=key))["leads"][0]["id"]
    res = call_tool(
        client,
        "update_lead",
        {"lead_id": lead_id, "status": "booked", "note": "Tuesday 10am", "idempotency_key": "u1"},
        key=key,
    )
    assert not res["isError"] and data(res)["status"] == "booked"
    assert res["structuredContent"]["say"] == f"Marked lead {lead_id} as booked."  # I6: never the person's name
    bad = call_tool(client, "update_lead", {"lead_id": lead_id, "status": "maybe", "idempotency_key": "u2"}, key=key)
    assert bad["isError"] and "new, contacted, booked, won, lost or spam" in tool_text(bad)


def test_list_leads_validates_since(client, conn):
    key, page = _page(conn)
    client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199"})
    bad = call_tool(client, "list_leads", {"since": "garbage"}, key=key)
    assert bad["isError"] and "since must be an ISO date or time, e.g. 2026-09-01" in tool_text(bad)
    ok = call_tool(client, "list_leads", {"since": "2026-01-01"}, key=key)
    assert not ok["isError"] and len(data(ok)["leads"]) == 1


# ---------------------------------------------------------------- set_landing_page / check_landing_page / get_page


def test_set_landing_page_publishes_and_reports_a_passing_check(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    url = "https://brightsmile.example/invisalign"
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=url))
    res = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": "s1"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["page"]["url"] == url and env["data"]["page"]["status"] == "published"
    assert env["data"]["check"]["ok"] is True
    # the page view is re-read after save_check: checked_at is fresh in the same response as the check itself
    assert env["data"]["page"]["checked_at"] is not None
    assert env["say"] == (
        "Your ads will send people to brightsmile.example. The page passes 1 of 1 checks. "
        "(e-mail is not set up on this server, so it is only here.)"  # R12: the report is mailed, when mail is on
    )
    assert env["data"]["email"] == "unconfigured" and env["data"]["emailed"] is False
    assert not JARGON.search(env["say"])
    assert env["next"]["kind"] == "actions" and env["next"]["actions"][0]["tool"] == "approve_plan"
    assert landing.current(conn, biz)["url"] == url


def test_set_landing_page_says_what_to_fix_when_it_fails(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    url = "https://brightsmile.example/x"
    rules = [
        {"id": "reachable", "title": "t", "ok": True, "detail": "d", "fix": None},
        {"id": "phone", "title": "t2", "ok": False, "detail": "d2", "fix": "Put the phone at the top."},
    ]
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(False, rules=rules, url=url))
    res = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": "s2"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["check"]["ok"] is False
    assert "passes 1 of 2 checks" in env["say"] and "Put the phone at the top." in env["say"]
    assert env["next"]["kind"] == "actions" and env["next"]["actions"][0]["tool"] == "check_landing_page"
    # the URL is still published even though the check fails: fix in place, don't strand the owner mid-launch
    assert landing.current(conn, biz)["url"] == url


def test_set_landing_page_says_worth_doing_for_a_warning_even_when_ok(client, conn, fake, monkeypatch):  # noqa: F811
    """R7: a warning on a BLOCKING rule (e.g. the sensitive-category compliance note) no longer holds `ok`
    at False, but its advice should still reach the owner -- as a "Worth doing" suffix, not a "To fix" one."""
    key, _biz, _ = _plan(client, conn, fake)
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    url = "https://brightsmile.example/x"
    rules = [
        {"id": "reachable", "title": "t", "ok": True, "detail": "d", "fix": None},
        {
            "id": "compliance",
            "title": "t2",
            "ok": None,
            "detail": "sensitive category",
            "fix": "Keep the form to those fields, or use a call button and a booking link instead.",
        },
    ]
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, rules=rules, url=url))
    res = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": "s4"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["check"]["ok"] is True
    assert "Worth doing: Keep the form to those fields" in env["say"] and "To fix" not in env["say"]
    assert env["next"]["actions"][0]["tool"] == "approve_plan"  # a warning never blocks the launch action either


def test_a_page_that_passes_offers_the_launch_only_while_the_plan_is_a_draft(client, conn, fake, monkeypatch):  # noqa: F811
    """Final review B (pre-existing on main): a page that passes offers to launch the plan while the latest plan is a
    draft. Once it is approved there is nothing to launch: set_landing_page and check_landing_page offer nothing next.
    A page that fails still offers to check it again."""
    key, biz, plan_id = _plan(client, conn, fake)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    url = "https://brightsmile.example/x"
    reports = {"ok": True}
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(reports["ok"], url=url))
    drafted = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": "s1"}, key=key)
    assert drafted["structuredContent"]["next"]["actions"][0]["tool"] == "approve_plan", tool_text(drafted)
    with conn.transaction():  # the owner approves the plan on their page
        asked = proposals.propose_plan(conn, biz, plan_id)
        proposals.confirm(conn, biz, asked["proposal_id"], code=None, web_owner=True)
    assert plans.latest(conn, biz)["status"] == "approved"
    for res in (
        call_tool(client, "set_landing_page", {"url": url, "idempotency_key": "s2"}, key=key),
        call_tool(client, "check_landing_page", {}, key=key),
    ):
        assert not res["isError"] and data(res)["check"]["ok"] is True, tool_text(res)
        assert res["structuredContent"]["next"] == envelope.NONE
    reports["ok"] = False
    failing = call_tool(client, "check_landing_page", {}, key=key)
    assert failing["structuredContent"]["next"]["actions"][0]["tool"] == "check_landing_page", tool_text(failing)


def test_set_landing_page_moves_live_ads_from_a_hosted_address(client, conn, gads, live, monkeypatch):  # noqa: F811
    """H27 (round 3): a business whose ads are already live at its Admiry-hosted address, given its own URL via
    set_landing_page, gets that address moved to the ads right away too -- `ads_run.flag_moved_page` (shared
    with `apply_publish_page`, not copied) sets `resources.url_stale`, and `launch_now` (called from `prepare`,
    same reasoning as `publish_page`'s after-commit hook: network work never runs inside `body`'s transaction)
    sends the move before this call even returns, one URL-only diff mutate, and leaves the row not due."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["page"]["url"] == PAGE_URL
    assert env["data"]["execution"]["state"] == "live"
    assert "Google Search: live" in env["say"]
    diff_bodies = [b for k, b in gads.sent[sent:] if k == "diff"]
    assert diff_bodies, f"expected a diff mutate moving the ad; sent kinds were {[k for k, _ in gads.sent[sent:]]}"
    new_full = PAGE_URL + "?src=gads"
    creates = [
        op["adGroupAdOperation"]["create"]
        for body in diff_bodies
        for op in body["mutateOperations"]
        if op.get("adGroupAdOperation", {}).get("create")
    ]
    assert any(c["ad"]["finalUrls"] == [new_full] for c in creates)
    r = row(conn)
    assert r["resources"]["final_url"] == new_full and "url_stale" not in r["resources"]
    assert landing.current(conn, BIZ)["source"] == "owner"
    due = f"SELECT business_id FROM channel_campaign c WHERE {ads_run.due(config.load(), GOOGLE)}"
    assert conn.execute(due).fetchall() == []  # nothing left for the sweeper


def test_set_landing_page_with_its_own_hosted_address_is_a_no_op(client, conn, gads, live, monkeypatch):  # noqa: F811
    """H27 (round 3): given the business's own Admiry-hosted address back, set_landing_page changes nothing.
    Naming it here would otherwise flip the live hosted row to `source = 'owner'` at its own address for no
    reason -- and then `hosted.serve` would stop answering it (H25: only a hosted/admiry-source row does)."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": URL, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert "already your Admiry-hosted page" in env["say"]
    assert env["data"]["page"]["url"] == URL
    assert env["data"]["check"] is None and env["data"]["execution"] is None
    assert gads.sent[sent:] == []  # nothing sent to Google
    page = landing.current(conn, BIZ)
    assert page["source"] == "hosted" and page["url"] == URL and page["hostname"] == HOSTNAME
    assert hosted.serve(conn, config.load(), HOSTNAME, "/", "GET").status_code == 200


def _hosted_live(client, conn, gads, monkeypatch) -> str:  # noqa: F811 (fixtures)
    """Ads live at the business's published Admiry-hosted page (test_tools_hosting's own steps); returns the key."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    return key


OWN_HOSTED = "That address is your Admiry-hosted page; publish version 1 again instead"


def test_set_landing_page_refuses_its_own_hosted_address_once_the_owners_page_is_live(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H28 (W6): the business's one page row keeps its hosted hostname through a switch to the owner's own page
    (H25), but it is `source = 'owner'` from then on and the pages host answers only a hosted row -- naming the
    slug back here left the row owner at Admiry's hostname (a 404) and moved the live ads there. Refused with the
    tool that does bring the hosted page back, whatever the spelling of the address; nothing written or sent."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    assert not call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)["isError"]
    before, sent = landing.current(conn, BIZ), len(gads.sent)

    for i, url in enumerate([URL, f"http://{HOSTNAME.upper()}./contact"]):
        res = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": f"s2{i}"}, key=key)
        assert res["isError"] and tool_text(res) == OWN_HOSTED, url

    assert landing.current(conn, BIZ) == before and before["source"] == "owner"
    assert gads.sent[sent:] == [] and row(conn)["resources"]["final_url"] == PAGE_URL + "?src=gads"
    assert hosted.serve(conn, config.load(), HOSTNAME, "/", "GET").status_code == 404  # as before: not served


def test_set_landing_page_refuses_its_own_hosted_address_while_that_page_is_down(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H28 (W5): taken down (unpublish_page), the hosted page answers "gone"; naming its address here used to say
    it already was the Admiry-hosted page and nothing changed, as if it were live. Refused with the way back."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    pid = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))["proposal_id"]
    assert not call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "cu1"}, key=key)["isError"]

    res = call_tool(client, "set_landing_page", {"url": URL, "idempotency_key": "s1"}, key=key)

    assert res["isError"] and tool_text(res) == OWN_HOSTED
    page = landing.current(conn, BIZ)
    assert (page["source"], page["status"], page["url"]) == ("hosted", "unpublished", URL)


def test_set_landing_page_refuses_every_other_address_under_the_pages_domain(client, conn, monkeypatch):
    """H28: a name under the pages domain that is not this business's own hosted page -- another business's, the
    bare domain, one two labels deep, a trailing-dot spelling -- is never an owner's page (Admiry serves those
    names, not the owner). Refused before anything is written: no page row appears, the other page is untouched."""
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    key_c, _ = seed(conn, biz="biz_c", link=None, page=False, conversions=False)
    _publish(client, conn, monkeypatch, key_c, GOOD_HTML, "c")  # another business's hosted page, at HOSTNAME
    key, _ = seed(conn, link=None, page=False, conversions=False)
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)

    for i, url in enumerate([URL, f"https://{DOMAIN}/", f"https://a.b.{DOMAIN}/x", f"https://{HOSTNAME}./"]):
        res = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": f"o{i}"}, key=key)
        assert res["isError"] and tool_text(res) == "Only your own site or your Admiry page can be the ads' page", url

    assert conn.execute("SELECT count(*) AS n FROM page WHERE business_id = %s", (BIZ,)).fetchone()["n"] == 0
    other = landing.current(conn, "biz_c")
    assert (other["source"], other["status"], other["hostname"]) == ("hosted", "published", HOSTNAME)


def test_set_landing_page_with_the_switch_off_says_the_ads_keep_their_old_address(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H28 item 5 (W3): live ads, the switch off, a new address named -- the reply used to end on ads_run.OFF
    ("not launched yet, and nothing is spent"), wrong for ads that are running. It says what publish_page says:
    the ads keep their old address until execution is on (the move is queued for then); nothing is sent."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    new = "https://brightsmile.example/new"
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=new))
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": new, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert _execution(res) == {"state": "off", "say": OFF_OLD_ADDRESS}
    assert env["say"].endswith(" " + OFF_OLD_ADDRESS) and ads_run.OFF not in env["say"]
    assert gads.sent[sent:] == [] and row(conn)["resources"]["url_stale"] is True


@pytest.mark.parametrize("switch", ["live", "off"])
def test_set_landing_page_under_paused_ads_moves_them_at_the_resume(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
    switch,
):
    """H28 item 5: paused ads (an owner's pause) are told nothing until they are resumed, whatever the switch --
    the reply says where they are (paused, never "not launched yet"), and the resume moves them first (H26)."""
    key, _v1 = _launched_with_kit(conn, gads)
    ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", switch)
    new = "https://brightsmile.example/new"
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=new))
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": new, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    assert _execution(res) == {"state": "paused", "say": "Google Search: paused by you today."}
    assert gads.sent[sent:] == [] and row(conn)["resources"]["url_stale"] is True
    if switch == "live":
        assert ads_run.resume(conn, config.load(), BIZ, GOOGLE)["state"] == "live"
        assert row(conn)["resources"]["final_url"] == new + "?src=gads"


def test_set_landing_page_launches_once_its_check_is_saved(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H28 item 5 (W7): the ads step runs after the check is saved, as publish_page's after-commit hook does. Ads
    built but never turned on (`launching`) need the page's check to pass first (P3), and set_owner_url has just
    cleared the old one: launched before the new check was saved, the reply said "not checked yet" right after
    the check had passed, and the ads waited for the hourly sweep."""
    key, _v1 = _launched_with_kit(conn, gads)
    conn.execute(
        "UPDATE channel_campaign SET state = 'launching', launched_at = NULL WHERE business_id = %s AND channel = %s",
        (BIZ, GOOGLE),
    )
    new = "https://brightsmile.example/new"
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=new))

    res = call_tool(client, "set_landing_page", {"url": new, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["execution"]["state"] == "live" and "not been checked" not in env["say"]
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("live", None)
    assert r["resources"]["final_url"] == new + "?src=gads"


PREVIEW_LINK = "A preview link expires; publish the page or give the ads your own site's address"


def test_set_landing_page_refuses_the_agent_hosts_own_addresses(client, conn, monkeypatch):
    """H29: a preview link (`<public base>/p/<token>`, good for 24 hours) or anything else on Admiry's own hosts is
    never the owner's page: refused before anything is written."""
    key, _ = seed(conn, link=None, page=False, conversions=False)
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)

    for i, url in enumerate(["http://testserver/p/pv_0123456789abcdef", "https://TESTSERVER./connect"]):
        res = call_tool(client, "set_landing_page", {"url": url, "idempotency_key": f"a{i}"}, key=key)
        assert res["isError"] and tool_text(res) == PREVIEW_LINK, url
    assert conn.execute("SELECT count(*) AS n FROM page WHERE business_id = %s", (BIZ,)).fetchone()["n"] == 0

    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://home.admiry.test")  # the browser host, when it is split off
    with pytest.raises(ToolError, match="A preview link expires"):
        tools_pages._admiry_address(conn, None, "home.admiry.test")
    assert tools_pages._admiry_address(conn, None, "brightsmile.example") is False


def _refused_move(client, conn, gads, monkeypatch, key: str, url: str) -> None:  # noqa: F811 (fixtures)
    """set_landing_page(url) with Google Ads refusing the ads' move there (`resources.refused_url`)."""
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=url))
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})
    assert not call_tool(client, "set_landing_page", {"url": url, "idempotency_key": "refused"}, key=key)["isError"]
    del gads.fail["diff"]
    assert row(conn)["resources"]["refused_url"] == url


def test_a_passing_check_on_the_refused_page_is_the_retry(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H29: Google Ads would not take the page's new address; the owner fixes that page, and the check that passes
    on it is the retry -- the refusal goes and the move is queued, the next sweep sends it. A check that still
    fails changes nothing."""
    key, _v1 = _launched_with_kit(conn, gads)
    new = "https://brightsmile.example/new"
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    _refused_move(client, conn, gads, monkeypatch, key, new)
    assert row(conn)["state"] == "live"  # the ads keep the owner's earlier page, which is up

    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(False, url=new))
    failing = call_tool(client, "check_landing_page", {}, key=key)
    assert ADDRESS_RETRY not in failing["structuredContent"]["say"]
    assert row(conn)["resources"]["refused_url"] == new and "url_stale" not in row(conn)["resources"]

    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=new))
    passing = call_tool(client, "check_landing_page", {}, key=key)
    assert passing["structuredContent"]["say"].endswith(" " + ADDRESS_RETRY + ".")
    r = row(conn)
    assert "refused_url" not in r["resources"] and r["resources"]["url_stale"] is True

    sent = len(gads.sent)
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)  # the next sweep
    assert "diff" in gads.kinds()[sent:]
    r = row(conn)
    assert r["resources"]["final_url"] == new + "?src=gads" and "url_stale" not in r["resources"]


def test_naming_the_fixed_page_again_retries_its_address_right_away(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H29: set_landing_page with the refused address again (the page fixed) runs the check; it passes, so the
    retry goes out in the same call, like any move."""
    key, _v1 = _launched_with_kit(conn, gads)
    new = "https://brightsmile.example/new"
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    _refused_move(client, conn, gads, monkeypatch, key, new)

    res = call_tool(client, "set_landing_page", {"url": new, "idempotency_key": "s2"}, key=key)

    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert env["data"]["execution"]["state"] == "live" and "did not take" not in env["say"]
    r = row(conn)
    assert r["resources"]["final_url"] == new + "?src=gads"
    assert "refused_url" not in r["resources"] and "url_stale" not in r["resources"]


def test_republishing_the_fixed_page_at_a_refused_address_retries_it(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H29, publish_page's side: Google Ads would not take the hosted address for the ads (they keep the owner's own
    page, which is up); a fixed version published at that same address passes its check, and that is the retry --
    the ads move there in the same confirm, though the address itself did not change."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_PAGES_DOMAIN", DOMAIN)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=URL))
    gads.fail["diff"] = google_error(400, {"urlFieldError": "INVALID_URL"})
    _publish(client, conn, monkeypatch, key, GOOD_HTML, "1")
    del gads.fail["diff"]
    r = row(conn)
    assert (r["state"], r["resources"]["refused_url"]) == ("live", URL)
    assert r["resources"]["final_url"] == PAGE_URL + "?src=gads"

    out = _publish(client, conn, monkeypatch, key, GOOD_HTML_V2, "2")

    assert out["ads"] == "moved"
    r = row(conn)
    assert r["resources"]["final_url"] == URL + "?src=gads"
    assert "refused_url" not in r["resources"] and "url_stale" not in r["resources"]


@pytest.mark.parametrize("way_back", ["move", "publish", "resume", "fix"])
def test_a_refused_move_off_a_hosted_page_pauses_the_ads_until_they_can_come_back(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
    way_back,
):
    """H29 (W/Y1): the ads live at the business's hosted page; set_landing_page names the owner's own site and Google
    Ads refuses the move -- the ads would keep sending paid clicks to the hosted address, which answers "not found"
    from the moment the owner's page is the live one. Admiry pauses them instead (spec §8) and says why. The
    owner's next move that goes through (another address, the hosted page published again, or -- once a check of
    the page the owner fixed passed, the retry, H29 -- that page named again) resumes Admiry's pause (H30).
    Re-specified by M0 Task 9 D3: the owner's own resume no longer lifts it -- Admiry's pause is Admiry's, and the
    status line says what does."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    campaign = row(conn)["resources"]["campaign"]
    sent = len(gads.sent)
    _refused_move(client, conn, gads, monkeypatch, key, PAGE_URL)

    assert "pause" in gads.kinds()[sent:] and gads.campaigns[campaign]["status"] == "PAUSED"
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"]) == ("paused", "admiry", "page_down")
    assert r["resources"]["final_url"] == URL + "?src=gads"
    assert hosted.serve(conn, config.load(), HOSTNAME, "/", "GET").status_code == 404
    status = ads_run.status_sentence(r)
    assert status.startswith(f"Google Search: paused by Admiry today: {PAGE_DOWN}.")
    assert "did not take the page address brightsmile.example" in status and JARGON.search(status) is None
    note = "SELECT result->>'note' AS note FROM agent_action WHERE tool = 'job.ads_launch' ORDER BY id DESC LIMIT 1"
    assert conn.execute(note).fetchone()["note"] == "paused your ads on Google Search: the page they point at is down"

    if way_back == "move":
        other = "https://brightsmile.example/other"
        monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=other))
        res = call_tool(client, "set_landing_page", {"url": other, "idempotency_key": "s2"}, key=key)
        assert data(res)["execution"]["state"] == "live"
        final = other + "?src=gads"
    elif way_back == "publish":
        prop = data(call_tool(client, "publish_page", {"idempotency_key": "rb", "version": 1}, key=key))
        _checked(monkeypatch, conn, URL)
        res = call_tool(
            client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "c"}, key=key
        )
        assert not res["isError"], tool_text(res)
        final = URL + "?src=gads"
    elif way_back == "fix":
        say = call_tool(client, "check_landing_page", {}, key=key)["structuredContent"]["say"]
        assert say.endswith(f" {ADDRESS_RETRY}{tools_pages.RETRY_ONCE_NAMED}.")  # D3: no resume lifts page_down
        assert row(conn)["state"] == "paused"  # a read tool never turns them on
        with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):  # D3: not the owner's resume to lift
            ads_run.resume(conn, config.load(), BIZ, GOOGLE)
        # the page the owner fixed, named again: the address did not change, its check passes -- the way out
        res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s3"}, key=key)
        assert data(res)["execution"]["state"] == "live"
        final = PAGE_URL + "?src=gads"  # the retry went out with that resume
    else:
        with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):  # D3: the owner's resume never lifts it
            ads_run.resume(conn, config.load(), BIZ, GOOGLE)
        r = row(conn)
        assert (r["state"], r["paused_reason"], r["resources"]["refused_url"]) == ("paused", "page_down", PAGE_URL)
        assert gads.campaigns[campaign]["status"] == "PAUSED"
        return

    r = row(conn)
    assert r["state"] == "live" and gads.campaigns[campaign]["status"] == "ENABLED"
    assert r["resources"]["final_url"] == final and "paused_by" not in r["resources"] and r["paused_reason"] is None
    assert "refused_url" not in r["resources"]


def test_an_owners_pause_during_a_refused_move_off_a_hosted_page_stands_beside_admirys(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
):
    """H29 with H24, re-specified by M0 Task 9 (the two pauses; D5): the owner pauses while the refused move is
    still out (S17: a pause never waits for the step); Admiry's page-down pause stands beside the owner's, never
    over it -- so a later set_landing_page lifts Admiry's alone and the ads stay paused by the owner, and the
    owner's resume alone never puts them back on a page that is down."""
    key = _hosted_live(client, conn, gads, monkeypatch)

    def refused_while_the_owner_pauses(body):
        with db.connect(TEST_DB_URL) as other:
            ads_run.pause(other, config.load(), BIZ, GOOGLE, by="owner")
        return google_error(400, {"urlFieldError": "INVALID_URL"})

    gads._diff = refused_while_the_owner_pauses
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    assert not call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)["isError"]

    r = row(conn)
    assert (r["state"], r["resources"]["owner_paused"], r["paused_reason"]) == ("paused", True, "page_down")
    assert r["resources"]["refused_url"] == PAGE_URL

    other = "https://brightsmile.example/other"
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=other))
    res = call_tool(client, "set_landing_page", {"url": other, "idempotency_key": "s2"}, key=key)
    assert _execution(res)["state"] == "paused"
    r = row(conn)
    assert (r["state"], r["resources"]["owner_paused"], r["paused_reason"]) == ("paused", True, None)


@pytest.mark.parametrize("paused_by", ["admiry", "owner"])
def test_set_landing_page_resumes_only_admirys_own_pause(
    client,
    conn,
    gads,  # noqa: F811
    live,  # noqa: F811
    monkeypatch,
    paused_by,
):
    """H30: unpublish_page takes the hosted page down and pauses the ads for Admiry (H23); set_landing_page making
    the owner's own page the live one resumes that pause, as publish_page does (H24) -- never an owner's own pause.
    M0 Task 9 D5: unpublish's pause stands beside an owner's pause already there, and set_landing_page lifts it
    alone: the owner's stands."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    if paused_by == "owner":
        ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")
    pid = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))["proposal_id"]
    assert not call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "cu1"}, key=key)["isError"]
    r = row(conn)
    assert (r["state"], r["paused_reason"], "owner_paused" in r["resources"]) == (
        "paused",
        "page_down",
        paused_by == "owner",
    )
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    r = row(conn)
    if paused_by == "admiry":
        assert _execution(res)["state"] == "live" and r["state"] == "live"
        assert r["resources"]["final_url"] == PAGE_URL + "?src=gads" and "paused_by" not in r["resources"]
    else:
        assert _execution(res) == {"state": "paused", "say": "Google Search: paused by you today."}
        assert (r["state"], r["resources"]["owner_paused"], r["paused_reason"]) == ("paused", True, None)
        assert gads.sent[sent:] == []


def test_an_owners_pause_landing_before_set_landing_pages_resume_stays_the_owners(
    client,
    conn,
    gads,  # noqa: F811 - pytest fixture
    live,  # noqa: F811 - pytest fixture
    monkeypatch,
):
    """The final review's F3, set_landing_page's side (H30): the owner's own page made live ends Admiry's own pause
    -- that pause alone (M0's two pauses: each is lifted only by its own resume). An owner's pause landing after
    set_landing_page read the row is theirs, and the ads stay paused by them."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    pid = data(call_tool(client, "unpublish_page", {"idempotency_key": "u1"}, key=key))["proposal_id"]
    assert not call_tool(client, "confirm_proposal", {"proposal_id": pid, "idempotency_key": "cu1"}, key=key)["isError"]
    assert row(conn)["paused_reason"] == "page_down"
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    real_resume = ads_run.resume

    def resumed_after_the_owner_pauses(*args, **kwargs):
        ads_run.pause(conn, config.load(), BIZ, GOOGLE, by="owner")  # lands after set_landing_page read the row
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(ads_run, "resume", resumed_after_the_owner_pauses)
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    assert _execution(res) == {"state": "paused", "say": "Google Search: paused by you today."}
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"]) == ("paused", "owner") and gads.sent[sent:] == []
    assert r["paused_reason"] is None


@pytest.mark.parametrize("switch", ["off", "validate"])
def test_a_move_off_a_hosted_page_the_switch_holds_back_owes_admirys_page_down_pause(
    client,
    conn,
    gads,  # noqa: F811 - pytest fixture
    live,  # noqa: F811 - pytest fixture
    monkeypatch,
    switch,
):
    """Task 3's final review (carried into Task 4): the ads live at the business's hosted page; the switch turned
    `off` (or `validate`, which never changes what is built), set_landing_page names the owner's own site. The move
    cannot reach Google Ads, and the address the ads keep answers "not found" from now on -- so Admiry records its
    page-down pause, owed to Google Ads (ruling S11), and says so, instead of "live" or "the old address". Nothing
    goes to Google; once the switch is live the sweep sends the PAUSED status, and the owner's resume moves the ads
    to their own page first. Re-specified by M0 Task 9 D3: the owner's resume never lifts Admiry's pause -- naming
    the owner's page again once the switch is live (its check passing) is the way back."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", switch)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))
    sent = len(gads.sent)

    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert not res["isError"], tool_text(res)
    assert gads.sent[sent:] == []
    r = row(conn)
    assert (r["state"], r["resources"]["paused_by"], r["paused_reason"], r["resources"]["pause_owed"]) == (
        "paused",
        "admiry",
        "page_down",
        True,
    )
    assert r["resources"]["final_url"] == URL + "?src=gads" and r["resources"]["url_stale"] is True
    say = f"Google Search: paused by Admiry today: {PAGE_DOWN}. {google.PAUSE_OWED}."
    assert _execution(res) == {"state": "paused", "say": say}
    assert res["structuredContent"]["say"].endswith(" " + say) and OFF_OLD_ADDRESS not in say
    assert not JARGON.search(res["structuredContent"]["say"])

    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    ads_run.launch(conn, config.load(), BIZ, GOOGLE)  # the hourly sweep: the owed PAUSED status goes out
    assert gads.kinds()[sent:] == ["pause"] and "pause_owed" not in row(conn)["resources"]
    assert ads_run.status_sentence(row(conn)) == f"Google Search: paused by Admiry today: {PAGE_DOWN}."
    with pytest.raises(ToolError, match=ads_run.PAUSED_BY_ADMIRY):  # D3: the owner's resume never lifts it
        ads_run.resume(conn, config.load(), BIZ, GOOGLE)
    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s2"}, key=key)
    assert _execution(res)["state"] == "live"
    r = row(conn)
    assert r["resources"]["final_url"] == PAGE_URL + "?src=gads" and r["paused_reason"] is None


def test_a_move_off_a_hosted_page_leaves_ads_someone_paused_in_google_ads_alone(
    client,
    conn,
    gads,  # noqa: F811 - pytest fixture
    live,  # noqa: F811 - pytest fixture
    monkeypatch,
):
    """Ruling H33: someone paused the ads in Google Ads itself (ads-sync's note, the row `limited`); the move off the
    hosted page under `off` records no pause of Admiry's over theirs -- they are off already."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    conn.execute(
        "UPDATE channel_campaign SET state = 'limited', last_error = %s WHERE business_id = %s AND channel = %s",
        (google.PAUSED_IN_GOOGLE, BIZ, GOOGLE),
    )
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))

    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert _execution(res) == {"state": "off", "say": OFF_OLD_ADDRESS}
    r = row(conn)
    assert (r["state"], r["last_error"]) == ("limited", google.PAUSED_IN_GOOGLE)
    assert not {"paused_by", "pause_owed"} & set(r["resources"]) and r["paused_reason"] is None


def test_ads_not_turned_on_yet_are_not_paused_for_a_move_the_switch_holds_back(
    client,
    conn,
    gads,  # noqa: F811 - pytest fixture
    live,  # noqa: F811 - pytest fixture
    monkeypatch,
):
    """Built but not turned on (`launching`): nothing serves at the hosted address, and the enable moves the ads to
    the page's address before it turns them on (H26) -- so no page-down pause for them, only the queued move."""
    key = _hosted_live(client, conn, gads, monkeypatch)
    conn.execute(
        "UPDATE channel_campaign SET state = 'launching' WHERE business_id = %s AND channel = %s", (BIZ, GOOGLE)
    )
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=PAGE_URL))

    res = call_tool(client, "set_landing_page", {"url": PAGE_URL, "idempotency_key": "s1"}, key=key)

    assert _execution(res) == {"state": "off", "say": OFF_OLD_ADDRESS}
    r = row(conn)
    assert r["state"] == "launching" and "paused_by" not in r["resources"] and r["resources"]["url_stale"] is True


def test_a_move_between_two_owner_addresses_under_the_off_switch_pauses_nothing(
    client,
    conn,
    gads,  # noqa: F811 - pytest fixture
    live,  # noqa: F811 - pytest fixture
    monkeypatch,
):
    """The page-down pause is only for ads left on the business's hosted address: an owner's earlier page is theirs
    to keep up, so a move from it under `off` still reads "the ads keep their old address"."""
    key, _v1 = _launched_with_kit(conn, gads)
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "off")
    new = "https://brightsmile.example/new"
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True, url=new))

    res = call_tool(client, "set_landing_page", {"url": new, "idempotency_key": "s1"}, key=key)

    assert _execution(res) == {"state": "off", "say": OFF_OLD_ADDRESS}
    assert row(conn)["state"] == "live"


def test_set_landing_page_refuses_a_private_host(client, conn, fake):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    res = call_tool(client, "set_landing_page", {"url": "http://10.0.0.1/", "idempotency_key": "s3"}, key=key)
    assert res["isError"]
    # crawl.CrawlError's wording, naming this tool's own parameter
    assert tool_text(res) == "url must be a public address"
    assert landing.current(conn, biz) is None


def test_set_landing_page_without_a_plan_writes_nothing(client, conn, monkeypatch):
    """I11: the kit (which needs the plan) is built before anything is published, so a refusal leaves no page
    row behind -- not even the kit's preview placeholder."""
    key = business_with_key(conn, claim_state="claimed")
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    res = call_tool(
        client, "set_landing_page", {"url": "https://brightsmile.example/x", "idempotency_key": "n1"}, key=key
    )
    assert res["isError"] and "Build the plan first" in tool_text(res)
    assert conn.execute("SELECT count(*) AS n FROM page").fetchone()["n"] == 0


def test_set_landing_page_emails_its_report_then_checks_only_on_a_flip(client, conn, fake, monkeypatch):  # noqa: F811
    """R12 (I7): set_landing_page e-mails the report it saves (every report goes out by e-mail, spec §6); a
    check_landing_page with the same result sends nothing more, and one whose result flips sends the next."""
    key, biz, _ = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    monkeypatch.setattr(crawl, "resolve", lambda h: _PUBLIC_IP)
    sent = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
        ),
    )
    reports = iter([_report(False), _report(False), _report(True)])
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: next(reports))

    first = call_tool(
        client, "set_landing_page", {"url": "https://brightsmile.example/x", "idempotency_key": "e1"}, key=key
    )
    assert not first["isError"], tool_text(first)
    assert data(first)["email"] == "sent" and len(sent) == 1
    assert first["structuredContent"]["say"].endswith("I e-mailed it to owner@example.com too.")
    body = json.loads(sent[0].read())
    assert body["subject"] == "Your landing page: 0 of 1 checks pass" and "Reply to this e-mail" not in body["text"]

    same = call_tool(client, "check_landing_page", {}, key=key)
    assert data(same)["email"] == "unchanged" and len(sent) == 1
    flipped = call_tool(client, "check_landing_page", {}, key=key)
    assert data(flipped)["email"] == "sent" and len(sent) == 2


def test_a_report_that_could_not_be_mailed_says_the_report_is_here(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    refuse = httpx.MockTransport(lambda r: httpx.Response(200, text="OK"))  # a 2xx that is not JSON: a MailError
    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=refuse))
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: _report(True))
    res = call_tool(client, "check_landing_page", {}, key=key)
    assert not res["isError"], tool_text(res)
    assert data(res)["email"] == "failed"
    assert res["structuredContent"]["say"].endswith("did not go through; the report is here.)")


def test_check_landing_page_emails_only_when_the_result_changes(client, conn, fake, monkeypatch):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    sent = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
        ),
    )
    reports = iter([_report(False), _report(False), _report(True)])
    monkeypatch.setattr(tools_pages.landing, "check", lambda *a, **k: next(reports))

    first = call_tool(client, "check_landing_page", {}, key=key)
    assert not first["isError"], tool_text(first)
    assert data(first)["email"] == "sent" and len(sent) == 1
    assert "e-mailed it to owner@example.com" in first["structuredContent"]["say"]
    assert first["structuredContent"]["next"]["actions"][0]["tool"] == "check_landing_page"  # fix, then again

    second = call_tool(client, "check_landing_page", {}, key=key)
    assert not second["isError"], tool_text(second)
    assert data(second)["email"] == "unchanged" and len(sent) == 1  # unchanged ok: no second mail
    assert "Same result as the last check, so no new e-mail" in second["structuredContent"]["say"]

    third = call_tool(client, "check_landing_page", {}, key=key)
    assert not third["isError"], tool_text(third)
    assert data(third)["email"] == "sent" and len(sent) == 2  # ok flipped back to True: mails again
    assert third["structuredContent"]["next"]["actions"][0]["tool"] == "approve_plan"  # passing: launch it


def test_check_landing_page_can_try_a_url_without_saving_it(client, conn, fake, monkeypatch):  # noqa: F811
    """R10: an ad-hoc `url` check is a dry run -- it never touches the live page's own check_json/checked_at,
    and never e-mails, even with mail fully configured and the owner on file (so this isn't passing only
    because there was nothing to save/send in the first place)."""
    key, biz, _ = _plan(client, conn, fake)
    conn.execute("UPDATE business SET owner_email = 'owner@example.com' WHERE id = %s", (biz,))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    sent = []
    monkeypatch.setattr(
        mail,
        "client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(200, json={"id": "em_1"}))[1])
        ),
    )
    seen = []

    def fake_check(url, page, kit, *, conn, **kw):
        seen.append(url)
        return _report(True, url=url)

    monkeypatch.setattr(tools_pages.landing, "check", fake_check)
    res = call_tool(client, "check_landing_page", {"url": "https://brightsmile.example/candidate"}, key=key)
    assert not res["isError"], tool_text(res)
    env = res["structuredContent"]
    assert seen == ["https://brightsmile.example/candidate"]
    assert env["data"]["check"]["url"] == "https://brightsmile.example/candidate"
    assert "email" not in env["data"] and "emailed" not in env["data"]
    assert env["say"].startswith("brightsmile.example would pass")
    assert env["next"]["actions"][0]["tool"] == "set_landing_page"  # a passing candidate can become the page
    # trying a candidate never publishes it: the live page is still the one set_landing_page named ...
    current_page = landing.current(conn, biz)
    assert current_page["url"] == "https://brightsmile.example/x"
    # ... and R10: nothing about it is touched by the candidate check -- no save, no e-mail
    assert current_page["check_json"] is None and current_page["checked_at"] is None
    assert sent == []


def test_check_landing_page_needs_a_page_first(client, conn, fake):  # noqa: F811
    key, _biz, _ = _plan(client, conn, fake)
    res = call_tool(client, "check_landing_page", {}, key=key)
    assert res["isError"] and "There is no page to check yet" in tool_text(res)


def test_get_page_returns_the_page_and_the_last_check(client, conn, fake):  # noqa: F811
    key, biz, _ = _plan(client, conn, fake)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    landing.save_check(conn, page["id"], _report(True))
    res = call_tool(client, "get_page", {}, key=key)
    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["page"]["url"] == "https://brightsmile.example/x" and env["page"]["status"] == "published"
    assert env["check"]["ok"] is True
    say = res["structuredContent"]["say"]
    assert "brightsmile.example/x" in say and "1/1" in say and not JARGON.search(say)


def test_get_page_reads_the_check_as_the_launch_judges_it(client, conn, fake):  # noqa: F811
    """Final review minor 6: an owner page's report saved before Message-match became advice there (spec 2026-10-12
    §3.6, R4) -- its rule failed, blocking -- reads back through get_page as the launch judges it
    (landing.judged_report): the rule `ok` None, the report `ok` True; the say counts what passes."""
    key, biz, _ = _plan(client, conn, fake)
    page = landing.set_owner_url(conn, biz, "https://brightsmile.example/x")
    rules = [
        {"id": "reachable", "title": "Opens securely and fast", "ok": True, "detail": "d.", "fix": None},
        {"id": "message_match", "title": "Headline matches the ad", "ok": False, "detail": "d.", "fix": "Say it."},
    ]
    landing.save_check(conn, page["id"], {**_report(False, rules), "headline": "Fresh bread every morning"})
    res = call_tool(client, "get_page", {}, key=key)
    assert not res["isError"], tool_text(res)
    check = data(res)["check"]
    assert check["ok"] is True and next(r for r in check["rules"] if r["id"] == "message_match")["ok"] is None
    assert "1/2 passing" in res["structuredContent"]["say"]


def test_get_page_before_anything_is_set(client, conn):
    key = business_with_key(conn, claim_state="claimed")
    res = call_tool(client, "get_page", {}, key=key)
    assert not res["isError"], tool_text(res)
    env = data(res)
    assert env["page"] is None and env["check"] is None
    assert (
        "No landing page is set yet. Admiry can write one, or you can name your own." in res["structuredContent"]["say"]
    )


# ---------------------------------------------------------------- get_page_stats


def _day(days_ago: int, minutes: int = 5) -> datetime:
    """A moment on the UTC day `days_ago` days back: just after its midnight, so never later than now."""
    today = datetime.now(UTC).date()
    return (
        datetime(today.year, today.month, today.day, tzinfo=UTC) - timedelta(days=days_ago) + timedelta(minutes=minutes)
    )


def _stats_business(conn) -> tuple[str, dict]:
    """biz_1's page with visits, taps and forms over the last weeks, its leads, and the ads' clicks beside them --
    and another business's page that must never be counted."""
    key, page = _page(conn)
    for days_ago, kind in (
        (0, "visit"),
        (0, "visit"),
        (0, "call"),
        (1, "form"),
        (1, "visit"),
        (3, "book"),
        (3, "mail"),
        (10, "visit"),
    ):
        conn.execute(
            "INSERT INTO page_event (page_id, kind, ts) VALUES (%s, %s, %s)", (page["id"], kind, _day(days_ago))
        )
    for days_ago in (0, 1, 12):
        conn.execute(
            "INSERT INTO lead (id, business_id, page_id, kind, created_at) VALUES (%s, 'biz_1', %s, 'form', %s)",
            (db.new_id("ld"), page["id"], _day(days_ago)),
        )
    today = datetime.now(UTC).date()
    for days_ago, clicks in ((0, 9), (2, 4), (20, 7)):
        conn.execute(
            "INSERT INTO metric_daily (business_id, channel, day, cost_micros, clicks, impressions, calls, leads, "
            "conversions, booked, source) VALUES ('biz_1', 'google_search', %s, 0, %s, 0, 0, 0, 0, 0, 'api')",
            (today - timedelta(days=days_ago), clicks),
        )
    business_with_key(conn, biz_id="biz_2", claim_state="claimed")
    other = landing.set_owner_url(conn, "biz_2", "https://other.example/")
    conn.execute("INSERT INTO page_event (page_id, kind) VALUES (%s, 'visit')", (other["id"],))
    conn.execute("INSERT INTO lead (id, business_id, kind) VALUES (%s, 'biz_2', 'form')", (db.new_id("ld"),))
    return key, page


def test_get_page_stats_sums_the_page_by_day_beside_the_ads_clicks(client, conn):
    key, _page_row = _stats_business(conn)
    today = datetime.now(UTC).date()

    res = call_tool(client, "get_page_stats", {}, key=key)

    assert not res["isError"], tool_text(res)
    out = data(res)
    zero = dict.fromkeys(("visit", "call", "mail", "book", "form", "booked_online", "leads", "ad_clicks"), 0)
    assert out["period"] == "7d" and len(out["days"]) == 7
    assert [d["day"] for d in out["days"]] == [(today - timedelta(days=i)).isoformat() for i in range(7)]
    by_day = {d["day"]: {k: v for k, v in d.items() if k != "day"} for d in out["days"]}
    expected = {
        0: {**zero, "visit": 2, "call": 1, "leads": 1, "ad_clicks": 9},
        1: {**zero, "visit": 1, "form": 1, "leads": 1},
        2: {**zero, "ad_clicks": 4},
        3: {**zero, "book": 1, "mail": 1},
    }
    for i in range(7):
        assert by_day[(today - timedelta(days=i)).isoformat()] == expected.get(i, zero), i
    assert out["totals"] == {
        "visit": 3,
        "call": 1,
        "mail": 1,
        "book": 1,
        "form": 1,
        "booked_online": 0,
        "leads": 2,
        "ad_clicks": 13,
    }
    say = res["structuredContent"]["say"]
    assert say == (
        "Your page over the last 7 days: 3 visits, 1 phone tap, 1 form sent, 1 booking click, 1 e-mail tap; "
        "2 leads; 13 clicks from your ads."
    )
    assert not JARGON.search(say)

    month = data(call_tool(client, "get_page_stats", {"period": "30d"}, key=key))
    assert len(month["days"]) == 30 and month["days"][-1]["day"] == (today - timedelta(days=29)).isoformat()
    assert month["totals"] == {
        "visit": 4,
        "call": 1,
        "mail": 1,
        "book": 1,
        "form": 1,
        "booked_online": 0,
        "leads": 3,
        "ad_clicks": 20,
    }


def test_get_page_stats_zero_fills_a_quiet_page_and_refuses_an_unknown_period(client, conn):
    key, _page_row = _page(conn)
    quiet = call_tool(client, "get_page_stats", {"period": "7d"}, key=key)
    assert not quiet["isError"], tool_text(quiet)
    assert len(data(quiet)["days"]) == 7 and set(data(quiet)["totals"].values()) == {0}
    assert quiet["structuredContent"]["say"].startswith("Your page over the last 7 days: 0 visits, 0 phone taps")

    for period in ("month", "90d", ""):
        bad = call_tool(client, "get_page_stats", {"period": period}, key=key)
        assert bad["isError"] and tool_text(bad) == "period must be one of 7d, 30d", period
