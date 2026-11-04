"""ads_conversions.py (spec 2026-09-28 admiry-google-search-execution-design.md §4, D43; pages spec
2026-09-28-admiry-pages-design.md §5.3; A3 Task 2): the `Admiry · <kind>` conversion actions (ten since A3p-3), call
reporting, the account's tag, and the `conversion_action` rows `landing.google_tag` reads."""

from __future__ import annotations

import json

import httpx
import pytest

from agent import config, google_auth
from agent.channels.google_search import client as google_ads
from agent.channels.google_search import conversions as ads_conversions
from agent.tests.fixtures import business_with_key

CID = "1234567890"
SECRETS = ("devtok_SECRET", "1//refresh_SECRET", "ya29.SECRET", "GOCSPX-x")
TOKEN_OK = httpx.Response(200, json={"access_token": "ya29.SECRET", "expires_in": 3599, "token_type": "Bearer"})


def fake_client(sent: list, *, customer: httpx.Response, actions: list, mutate=None) -> httpx.Client:
    """A client whose transport answers Google Ads REST like the shared client does: the token endpoint, the
    customer read (by its GAQL), the conversion_action read(s) (one per call, in order — `ensure` may read them
    once, before creating, or twice, to pick up tagSnippets for what it just created), and any mutate call — a
    fixed reply, or `mutate(request) -> httpx.Response` for a test that needs to fail one specific call or
    inspect it. `ensure` never reads a mutate's own response body (the real resource names and labels come from
    the next `actions` read), so a plain 200 with an empty body is enough unless a test wants otherwise. Like
    Google, an actions read that excludes REMOVED actions gets none."""
    actions_iter = iter(actions)
    reply = mutate if mutate is not None else httpx.Response(200, json={})

    def handler(request):
        sent.append(request)
        url = str(request.url)
        if url == google_auth.TOKEN_URL:
            return TOKEN_OK
        if url.endswith("googleAds:search"):
            body = json.loads(request.content)
            assert set(body) == {"query"}  # A3 final review C1: v25 refuses a page size
            if "customer.conversion_tracking_setting" in body["query"]:
                return customer
            answer = next(actions_iter)
            if "conversion_action.status != 'REMOVED'" not in body["query"]:
                return answer
            rows = answer.json().get("results") or []
            live = [r for r in rows if r["conversionAction"].get("status") != "REMOVED"]
            return httpx.Response(200, json={"results": live})
        if url.endswith("googleAds:mutate"):
            return reply(request) if callable(reply) else reply
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def customer_response(
    *, tracking_id="123", reporting=True, conversion_reporting=True, time_zone="America/Chicago"
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "customer": {
                        "resourceName": f"customers/{CID}",
                        "conversionTrackingSetting": {"conversionTrackingId": tracking_id},
                        "callReportingSetting": {
                            "callReportingEnabled": reporting,
                            "callConversionReportingEnabled": conversion_reporting,
                        },
                        "timeZone": time_zone,
                    }
                }
            ]
        },
    )


def _snippets(label: str | None) -> list:
    if not label:
        return []
    return [
        {
            "type": "WEBPAGE",
            "pageFormat": "HTML",
            "eventSnippet": f"gtag('event', 'conversion', {{'send_to': 'AW-123/{label}'}});",
        }
    ]


def actions_response(labels: dict, *, removed: tuple[str, ...] = (), renamed: dict | None = None) -> httpx.Response:
    """One conversion_action row per kind in `labels` (kind -> label, or None for no HTML snippet yet); `removed`
    kinds carry status REMOVED (someone removed them by hand in Google Ads); `renamed` gives a kind another
    resource id (an action created again)."""
    rows = [
        {
            "conversionAction": {
                "resourceName": f"customers/{CID}/conversionActions/{(renamed or {}).get(kind, kind)}",
                "name": ads_conversions.KINDS[kind][0],
                "type": ads_conversions.KINDS[kind][1],
                "status": "REMOVED" if kind in removed else "ENABLED",
                "tagSnippets": _snippets(label),
            }
        }
        for kind, label in labels.items()
    ]
    return httpx.Response(200, json={"results": rows})


def _mutates(sent: list) -> list:
    return [r for r in sent if str(r.url).endswith("googleAds:mutate")]


def _searches(sent: list) -> list:
    return [r for r in sent if str(r.url).endswith("googleAds:search")]


def _rows(conn, biz="biz_1") -> dict:
    return {
        r["kind"]: (r["resource_name"], r["label"])
        for r in conn.execute(
            "SELECT kind, resource_name, label FROM conversion_action WHERE business_id = %s", (biz,)
        ).fetchall()
    }


# ---------------------------------------------------------------- off / validate / live gating


def test_ensure_never_calls_google_when_execution_is_off(conn, ads_api):
    business_with_key(conn, biz_id="biz_1")
    sent: list = []
    http = fake_client(sent, customer=customer_response(), actions=[])
    out = ads_conversions.ensure(conn, ads_api, "biz_1", CID, http=http)
    assert out == {
        "tag": None,
        "created": [],
        "existing": [],
        "call_reporting": False,
        "time_zone": None,
        "skipped": "off",
    }
    assert sent == []
    assert _rows(conn) == {}


def test_ensure_in_validate_mode_validates_the_create_and_stores_only_the_tag(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "validate")
    settings = config.load()
    sent: list = []
    http = fake_client(sent, customer=customer_response(), actions=[actions_response({})])
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)
    mutates = _mutates(sent)
    assert len(mutates) == 1
    body = json.loads(mutates[0].content)
    assert body["validateOnly"] is True and len(body["mutateOperations"]) == len(ads_conversions.KINDS) == 10
    assert len(_searches(sent)) == 2  # customer + the one existing-actions read; no re-read after a dry run
    assert _rows(conn) == {"tag": (f"customers/{CID}", "AW-123")}
    assert out["tag"] == "AW-123" and out["created"] == list(ads_conversions.KINDS) and out["existing"] == []


def test_ensure_follows_google_searchs_own_switch_over_the_old_one(conn, ads_api, monkeypatch):
    """M0 ruling 1: the switch is Google Search's own (`registry.exec_mode`): AGENT_EXEC_GOOGLE_SEARCH wins over the
    old AGENT_ADS_EXECUTION, which only stands in while the new one is unset."""
    business_with_key(conn, biz_id="biz_1")

    def run(**env):
        for name in ("AGENT_EXEC_GOOGLE_SEARCH", "AGENT_ADS_EXECUTION"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        sent: list = []
        final = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
        http = fake_client(sent, customer=customer_response(), actions=[actions_response({}), final])
        return ads_conversions.ensure(conn, config.load(), "biz_1", CID, http=http), sent

    out, sent = run(AGENT_EXEC_GOOGLE_SEARCH="live")  # (a) the new switch alone: live, end to end
    assert "skipped" not in out and json.loads(_mutates(sent)[0].content)["validateOnly"] is False
    assert set(_rows(conn)) == {"tag", *ads_conversions.KINDS}
    conn.execute("DELETE FROM conversion_action")
    out, sent = run(AGENT_EXEC_GOOGLE_SEARCH="validate", AGENT_ADS_EXECUTION="live")  # (b) validate wins
    assert [json.loads(m.content)["validateOnly"] for m in _mutates(sent)] == [True]
    assert _rows(conn) == {"tag": (f"customers/{CID}", "AW-123")}
    out, sent = run(AGENT_EXEC_GOOGLE_SEARCH="off", AGENT_ADS_EXECUTION="live")  # (c) an explicit off calls nothing
    assert out["skipped"] == "off" and sent == []


# ---------------------------------------------------------------- creating the missing kinds (live)


def test_ensure_creates_every_missing_kind_on_a_fresh_account(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    final = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    http = fake_client(sent, customer=customer_response(), actions=[actions_response({}), final])
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)

    mutates = _mutates(sent)
    assert len(mutates) == 1  # "one mutate creating the missing ones"
    body = json.loads(mutates[0].content)
    assert body["validateOnly"] is False and len(body["mutateOperations"]) == len(ads_conversions.KINDS) == 10
    names = {op["conversionActionOperation"]["create"]["name"] for op in body["mutateOperations"]}
    assert names == {info[0] for info in ads_conversions.KINDS.values()}
    call_ad_create = next(
        op["conversionActionOperation"]["create"]
        for op in body["mutateOperations"]
        if op["conversionActionOperation"]["create"]["name"] == "Admiry · Call from ad"
    )
    assert call_ad_create == {
        "name": "Admiry · Call from ad",
        "type": "AD_CALL",
        "category": "PHONE_CALL_LEAD",
        "status": "ENABLED",
        "primaryForGoal": True,
        "countingType": "ONE_PER_CLICK",
        "valueSettings": {"defaultValue": 0, "alwaysUseDefaultValue": True},
        "clickThroughLookbackWindowDays": 30,
        "phoneCallDurationSeconds": 60,
    }

    assert out["created"] == list(ads_conversions.KINDS) and out["existing"] == []
    assert out["tag"] == "AW-123" and out["call_reporting"] is True  # already on in customer_response()'s defaults
    assert out["time_zone"] == "America/Chicago"  # ruling S14: read in the same customer query, for ads_run to keep
    customer_query = json.loads(_searches(sent)[0].content)["query"]
    assert "customer.time_zone" in customer_query

    rows = _rows(conn)
    assert rows["tag"] == (f"customers/{CID}", "AW-123")
    for kind in ads_conversions.KINDS:
        assert rows[kind] == (f"customers/{CID}/conversionActions/{kind}", f"L_{kind}")


def test_ensure_creates_only_the_missing_kinds_when_some_already_exist(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    three = actions_response({"form": "F1", "call": "C1", "mail": "M1"})
    seven = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    http = fake_client(sent, customer=customer_response(), actions=[three, seven])
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)

    mutates = _mutates(sent)
    assert len(mutates) == 1
    # "an account where three exist -> the rest" (seven since A3p-3 added appointment, call_site and booked_online)
    assert len(json.loads(mutates[0].content)["mutateOperations"]) == len(ads_conversions.KINDS) - 3 == 7
    assert out["existing"] == ["form", "call", "mail"]
    assert out["created"] == ["call_ad", "booked", "won", "book", "appointment", "call_site", "booked_online"]
    assert len(_rows(conn)) == len(ads_conversions.KINDS) + 1 == 11  # every kind + tag


def test_an_action_removed_by_hand_is_created_again_not_reused(conn, ads_api, monkeypatch):
    """A3 final review I1: the existing-actions read skips REMOVED actions -- a dead action of the same name must
    never be what the page script and the call asset report to."""
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    labels = {kind: f"L_{kind}" for kind in ads_conversions.KINDS}
    before = actions_response(labels, removed=("form",))
    after = actions_response(labels | {"form": "L_form2"}, renamed={"form": "form2"})
    http = fake_client(sent, customer=customer_response(), actions=[before, after])
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)
    query = json.loads(_searches(sent)[1].content)["query"]
    assert "conversion_action.status != 'REMOVED'" in query
    creates = json.loads(_mutates(sent)[0].content)["mutateOperations"]
    assert [op["conversionActionOperation"]["create"]["name"] for op in creates] == ["Admiry · Form sent"]
    assert out["created"] == ["form"]
    assert _rows(conn)["form"] == (f"customers/{CID}/conversionActions/form2", "L_form2")


def test_ensure_is_idempotent_a_second_call_creates_nothing_new(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    seven = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    first_sent: list = []
    http1 = fake_client(first_sent, customer=customer_response(), actions=[actions_response({}), seven])
    ads_conversions.ensure(conn, settings, "biz_1", CID, http=http1)
    before = _rows(conn)

    second_sent: list = []
    http2 = fake_client(second_sent, customer=customer_response(), actions=[seven])  # already all 7: one read only
    out2 = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http2)

    assert _mutates(second_sent) == []  # nothing missing -> no create mutate
    assert out2["created"] == [] and out2["existing"] == list(ads_conversions.KINDS)
    assert _rows(conn) == before  # re-upserted to the same values, no duplicates


# ---------------------------------------------------------------- call reporting


def test_ensure_turns_on_call_reporting_when_it_was_off(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    seven = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    http = fake_client(sent, customer=customer_response(reporting=False, conversion_reporting=False), actions=[seven])
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)

    mutates = _mutates(sent)
    assert len(mutates) == 1  # nothing missing to create: this is the call-reporting update alone
    op = json.loads(mutates[0].content)["mutateOperations"][0]
    assert op == {
        "customerOperation": {
            "update": {
                "resourceName": f"customers/{CID}",
                "callReportingSetting": {
                    "callReportingEnabled": True,
                    "callConversionReportingEnabled": True,
                    "callConversionAction": f"customers/{CID}/conversionActions/call_ad",
                },
            },
            "updateMask": (
                "callReportingSetting.callReportingEnabled,"
                "callReportingSetting.callConversionReportingEnabled,"
                "callReportingSetting.callConversionAction"
            ),
        }
    }
    assert out["call_reporting"] is True


def test_ensure_does_not_touch_call_reporting_when_already_fully_on(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    seven = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    http = fake_client(sent, customer=customer_response(reporting=True, conversion_reporting=True), actions=[seven])
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)
    assert _mutates(sent) == []
    assert out["call_reporting"] is True


@pytest.mark.parametrize("reporting,conversion_reporting", [(False, True), (True, False)])
def test_ensure_turns_on_call_reporting_when_only_one_flag_was_off(
    conn, ads_api, monkeypatch, reporting, conversion_reporting
):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    seven = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    http = fake_client(
        sent,
        customer=customer_response(reporting=reporting, conversion_reporting=conversion_reporting),
        actions=[seven],
    )
    out = ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)
    assert len(_mutates(sent)) == 1
    assert out["call_reporting"] is True


# ---------------------------------------------------------------- failures


def test_a_google_failure_propagates_and_stores_nothing(conn, ads_api, monkeypatch):
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    http = fake_client(
        sent,
        customer=customer_response(),
        actions=[actions_response({})],
        mutate=lambda request: httpx.Response(
            429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "devtok_SECRET"}}
        ),
    )
    with pytest.raises(google_ads.AdsError) as e:
        ads_conversions.ensure(conn, settings, "biz_1", CID, http=http)
    assert str(e.value) == google_ads.BUSY
    assert not any(secret in str(e.value) for secret in SECRETS)
    assert _rows(conn) == {}


# ---------------------------------------------------------------- no http passed: ensure opens its own, once


def test_ensure_opens_its_own_client_once_when_none_is_passed(conn, ads_api, monkeypatch):
    """setup.py's call sites pass no `http=` (unlike jobs.ads_link_sync, which already has one open); `ensure`
    should still make its several Google calls (customer, actions ×1-2, mutate) over one session, not open and
    close a fresh `httpx.Client` (a new connection) for each one the way calling `google_ads.search`/`mutate`
    directly with no `http=` of their own would. (Each call still re-exchanges its own access token — `_call`
    does that regardless of session reuse — so this checks client acquisitions, not token requests.)"""
    business_with_key(conn, biz_id="biz_1")
    monkeypatch.setenv("AGENT_ADS_EXECUTION", "live")
    settings = config.load()
    sent: list = []
    seven = actions_response({kind: f"L_{kind}" for kind in ads_conversions.KINDS})
    http = fake_client(sent, customer=customer_response(), actions=[actions_response({}), seven])
    opens: list = []
    monkeypatch.setattr(google_ads, "client", lambda: opens.append(1) or http)

    out = ads_conversions.ensure(conn, settings, "biz_1", CID)  # no http= at all

    assert len(opens) == 1  # one client acquired for the whole call, not one per Google request
    assert len(_searches(sent)) == 3 and len(_mutates(sent)) == 1  # customer + 2 actions reads + the create mutate
    assert out["tag"] == "AW-123" and out["created"] == list(ads_conversions.KINDS)


# ---------------------------------------------------------------- labels()


def test_labels_reads_every_kind_plus_the_tag(conn):
    business_with_key(conn, biz_id="biz_1")
    ads_conversions._upsert(conn, "biz_1", "tag", f"customers/{CID}", "AW-123")
    ads_conversions._upsert(conn, "biz_1", "form", f"customers/{CID}/conversionActions/form", "AbCdEf")
    assert ads_conversions.labels(conn, "biz_1") == {"tag": "AW-123", "form": "AbCdEf"}


def test_labels_is_empty_before_ensure_ever_ran(conn):
    business_with_key(conn, biz_id="biz_1")
    assert ads_conversions.labels(conn, "biz_1") == {}


def test_a_website_call_actions_label_is_read_from_its_phone_conversion_ids():
    """A3p-3 P26: a website call's snippet sets the tag's call settings rather than sending an event: its label is in
    `phone_conversion_ids`; an event snippet's `send_to` still wins where there is one."""
    call = "gtag('set', {'phone_conversion_number': '650-555-0100', 'phone_conversion_ids': ['AW-123/CaLlLbL']});"
    assert ads_conversions._label([{"pageFormat": "HTML", "eventSnippet": call}]) == "CaLlLbL"
    event = "gtag('event', 'conversion', {'send_to': 'AW-123/FoRm'});"
    assert ads_conversions._label([{"pageFormat": "HTML", "eventSnippet": event}]) == "FoRm"
    assert ads_conversions._label([{"pageFormat": "AMP", "eventSnippet": call}]) is None


def test_the_three_actions_of_measured_contacts_are_primary():
    """A3p-3 P25-P27: one primary conversion per contact -- an appointment request, a call from the site, a booking
    completed on the page; the call counts after a minute, like the call from the ad."""
    assert ads_conversions.KINDS["appointment"][:4] == (
        "Admiry · Appointment requested",
        "WEBPAGE",
        "BOOK_APPOINTMENT",
        True,
    )
    assert ads_conversions.KINDS["call_site"] == (
        "Admiry · Call from site",
        "WEBSITE_CALL",
        "PHONE_CALL_LEAD",
        True,
        {"phoneCallDurationSeconds": 60},
    )
    assert ads_conversions.KINDS["booked_online"][:4] == ("Admiry · Booked online", "WEBPAGE", "BOOK_APPOINTMENT", True)
