"""The hourly conversion upload (plan 2026-09-29-agent-m1-newsbreak Task 6, P9; the controller's rulings T6-1 to T6-4):
the leads a NewsBreak click brought -- the page's script keeps NewsBreak's click id as the `nb_cid` kind -- go back to
NewsBreak by click id, as the account's hand-made lead event, once each (`lead.uploaded_at`); only those (T6-2: the
channel and a NewsBreak click id, never an owner-reported lead, nor one the owner marked spam), only within NewsBreak's
7 days, a refused one retried; an account without the lead event waits (T6-3); one request a second per business;
NewsBreak's day budget and its throttles hold the upload for the rest of the UTC day (T6-4); `validate` records what
would go and sends nothing, `off` nothing at all; the token nowhere but in the settings. Over `FakeNewsBreak`."""

from __future__ import annotations

import logging

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent import jobs, landing, tick, tools_account
from agent.channels import registry
from agent.tests.conftest import ADS_SECRETS, JARGON, MONEY_JARGON, TEST_DB_URL
from agent.tests.fake_newsbreak import envelope
from agent.tests.fixtures import business_with_key

BIZ = "biz_1"
NB = "newsbreak"
LEAD_EVENT = {"id": "77", "name": "submit_form", "type": "POSTBACK", "eventType": "submit_form"}
TOKEN = "nbtok_SECRET"  # conftest's ADS_ENV token, which the `nbapi` fake answers to


@pytest.fixture
def nb_on(monkeypatch, nbapi):
    """NewsBreak faked and its switch `live`; returns the fake."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "live")
    return nbapi


def _business(conn, biz: str = BIZ) -> None:
    conn.execute("INSERT INTO business (id) VALUES (%s) ON CONFLICT DO NOTHING", (biz,))


def _account(conn, fake, biz: str = BIZ, *, event: bool = True, known: bool = True) -> str:
    """The business's NewsBreak ad account as the launch leaves it: made at NewsBreak, its id on the account row; the
    lead event made by hand there (`event`), its id already kept on the row (`known`) or not yet. Returns its id."""
    _business(conn, biz)
    account_id = fake.add_account(f"Admiry · Bright Smile · {biz}")
    if event:
        fake.events[account_id] = [LEAD_EVENT]
    held = {"ad_account_id": account_id} | ({"event_id": "77"} if event and known else {})
    registry.save_account(conn, biz, NB, mode="managed", status="active", external=held)
    return account_id


def _lead(
    conn,
    lead_id: str,
    *,
    biz: str = BIZ,
    click: str | None = "nvss_1",
    kind: str | None = "nb_cid",
    channel: str | None = NB,
    age: str = "5 minutes",
    status: str = "new",
    uploaded: bool = False,
) -> None:
    """A lead as the page's form keeps one, `age` old."""
    conn.execute(
        "INSERT INTO lead (id, business_id, kind, fields, click_id, click_id_kind, channel, status, uploaded_at, "
        "created_at) VALUES (%s, %s, 'form', '{}', %s, %s, %s, %s, CASE WHEN %s THEN now() END, now() - %s::interval)",
        (lead_id, biz, click, kind, channel, status, uploaded, age),
    )


def _uploaded(conn) -> set[str]:
    """The leads marked sent back (`uploaded_at`)."""
    return {r["id"] for r in conn.execute("SELECT id FROM lead WHERE uploaded_at IS NOT NULL").fetchall()}


def _external(conn, biz: str = BIZ) -> dict:
    return registry.account(conn, biz, NB).external


def _run() -> dict:
    return tick.run("conversion-upload", url=TEST_DB_URL)


def _postbacks(fake) -> list[str]:
    """Every click id a postback carried, NewsBreak's refusals included, in order."""
    return [body["callback"] for body in fake.bodies("tracking/attribute")]


# ---------------------------------------------------------------- Review Focus 5


def test_lead_with_nb_cid_is_uploaded_once(client, conn, nb_on):
    """Review Focus 5: a form on the page carrying NewsBreak's click id (`_nb_cid`, what the page's script adds) is a
    NewsBreak lead; conversion-upload looks the account's hand-made lead event up once, keeps its id, posts one `lead`
    event with `callback = <the click id>` as that event, and marks the lead sent; a second run sends nothing."""
    business_with_key(conn, scopes=("read", "manage", "leads"), claim_state="claimed")
    page = landing.set_owner_url(conn, BIZ, "https://brightsmile.example/invisalign")
    _account(conn, nb_on, known=False)
    r = client.post(f"/t/form/{page['id']}", data={"name": "Ana", "phone": "6505550199", "_nb_cid": "nvss_abc"})
    assert r.status_code == 200
    lead = conn.execute("SELECT id, channel, click_id, click_id_kind, uploaded_at FROM lead").fetchone()
    assert (lead["channel"], lead["click_id"], lead["click_id_kind"], lead["uploaded_at"]) == (
        NB,
        "nvss_abc",
        "nb_cid",
        None,
    )
    out = _run()
    assert nb_on.conversions == [{"callback": "nvss_abc", "event_type": "submit_form"}]
    assert nb_on.kinds() == ["event/getList", "tracking/attribute"]
    assert (out["sent"], out["rejected"], out["ops_by_channel"]) == (1, 0, {NB: 2})
    assert _uploaded(conn) == {lead["id"]} and _external(conn)["event_id"] == "77"
    (row,) = conn.execute("SELECT * FROM agent_action WHERE tool = 'job.conversion_upload'").fetchall()
    assert (row["business_id"], row["actor"], row["actor_class"]) == (BIZ, "job:conversion-upload", "job")
    said = tools_account.sentence(dict(row), None)
    assert said == "Admiry told NewsBreak about 1 lead from its ads, without names or numbers"
    assert not JARGON.search(said) and not MONEY_JARGON.search(said)

    nb_on.sent.clear()
    again = _run()
    assert nb_on.sent == [] and (again["sent"], again["ops_by_channel"]) == (0, {NB: 0})
    assert len(nb_on.conversions) == 1


# ---------------------------------------------------------------- T6-2: which leads go back


def test_only_newsbreak_click_leads_of_the_last_seven_days_go_back_and_never_spam(conn, nb_on):
    """T6-2: the upload takes a lead only with the NewsBreak channel and a NewsBreak click id, not sent yet and created
    within NewsBreak's 7 days (a click id is good for 7 days) -- never every lead of a business: an owner-reported lead
    (no channel, no click id), another channel's, one with an empty click id, one already sent, and one the owner
    marked spam (the controller's answer 2) all stay as they are."""
    _account(conn, nb_on)
    _lead(conn, "ld_ok", click="nvss_ok", age="6 days 23 hours 59 minutes")
    _lead(conn, "ld_old", click="nvss_old", age="7 days 1 minute")
    _lead(conn, "ld_google", click="g1", kind="gclid", channel="google_search")
    _lead(conn, "ld_owner", click=None, kind=None, channel=None)
    _lead(conn, "ld_blank", click="")
    _lead(conn, "ld_other_kind", click="g9", kind="gclid")  # on NewsBreak, yet no NewsBreak click id
    _lead(conn, "ld_sent", click="nvss_sent", uploaded=True)
    _lead(conn, "ld_spam", click="nvss_spam", status="spam")
    out = _run()
    assert _postbacks(nb_on) == ["nvss_ok"] and (out["sent"], out["rejected"], out["waiting"]) == (1, 0, 0)
    assert _uploaded(conn) == {"ld_ok", "ld_sent"}


def test_a_rejected_event_stays_unmarked_and_is_retried(conn, nb_on):
    """A click NewsBreak refuses (its answer to a click id past its 7 days, "typically 400") is counted `rejected`,
    stays unmarked and goes again the next hour -- the others of the business go on -- until NewsBreak takes it."""
    _account(conn, nb_on)
    _lead(conn, "ld_a", click="nvss_refused", age="2 minutes")
    _lead(conn, "ld_b", click="nvss_fine", age="1 minute")
    nb_on.rejected_clicks = {"nvss_refused"}
    out = _run()
    assert (out["sent"], out["rejected"]) == (1, 1) and _uploaded(conn) == {"ld_b"}
    assert _postbacks(nb_on) == ["nvss_refused", "nvss_fine"]
    nb_on.sent.clear()
    assert _run()["rejected"] == 1 and _postbacks(nb_on) == ["nvss_refused"]  # tried again, only it
    nb_on.rejected_clicks = set()
    nb_on.sent.clear()
    assert _run()["sent"] == 1 and _uploaded(conn) == {"ld_a", "ld_b"}
    nb_on.sent.clear()
    assert _run()["sent"] == 0 and nb_on.sent == []


# ---------------------------------------------------------------- T6-3: the hand-made lead event


def test_an_account_whose_lead_event_is_not_made_yet_waits_with_its_leads(conn, nb_on):
    """T6-3: NewsBreak has no API that makes the lead event; until an operator makes it by hand (the launch mailed them
    once), the account is looked at once a run and skipped -- its leads wait unmarked (`waiting`), retried within the
    7 days. The hour after the event exists, they go, and its id is kept: the next run asks NewsBreak nothing but the
    postbacks."""
    account_id = _account(conn, nb_on, event=False)
    _lead(conn, "ld_a", click="nvss_a", age="2 minutes")
    _lead(conn, "ld_b", click="nvss_b", age="1 minute")
    out = _run()
    assert (out["sent"], out["waiting"]) == (0, 2) and nb_on.kinds() == ["event/getList"]
    assert _uploaded(conn) == set() and "event_id" not in _external(conn)
    nb_on.events[account_id] = [LEAD_EVENT]  # the operator made it by hand
    nb_on.sent.clear()
    out = _run()
    assert (out["sent"], out["waiting"]) == (2, 0) and _external(conn)["event_id"] == "77"
    assert nb_on.kinds() == ["event/getList", "tracking/attribute", "tracking/attribute"]
    _lead(conn, "ld_c", click="nvss_c")
    nb_on.sent.clear()
    assert _run()["sent"] == 1 and nb_on.kinds() == ["tracking/attribute"]


def test_a_newsbreak_lead_of_a_business_without_a_newsbreak_ad_account_waits(conn, nb_on):
    """A lead whose business has no NewsBreak ad account on file (a click from a test link, an account not made yet)
    has nowhere to go: it waits, and NewsBreak is asked nothing."""
    _business(conn)
    _lead(conn, "ld_a")
    _business(conn, "biz_2")
    registry.save_account(conn, "biz_2", NB, mode="managed", status="none", external={})
    _lead(conn, "ld_b", biz="biz_2")
    out = _run()
    assert (out["sent"], out["waiting"]) == (0, 2) and nb_on.sent == [] and _uploaded(conn) == set()


def test_newsbreak_down_leaves_every_lead_for_the_next_hour(conn, nb_on, caplog):
    """NewsBreak not answering: each business's first postback fails (an error, logged by its type alone) and its
    other leads are not tried this run -- the next business is -- nothing is marked, and the next run sends them all.
    The requests NewsBreak never answered still count in the day's budget."""
    for biz in ("biz_a", "biz_b"):
        _account(conn, nb_on, biz)
        _lead(conn, f"ld_{biz}", biz=biz, click=f"nvss_{biz}", age="2 minutes")
    _lead(conn, "ld_biz_a_2", biz="biz_a", click="nvss_biz_a_2", age="1 minute")
    nb_on.down = True
    with caplog.at_level(logging.WARNING):
        out = _run()
    assert (out["sent"], out["errors"], out["ops_by_channel"]) == (0, 2, {NB: 2}) and _uploaded(conn) == set()
    tried = [r.url.params["callback"] for r in nb_on.requests]  # (a NewsBreak that is down records no `sent`)
    assert tried == ["nvss_biz_a", "nvss_biz_b"] and "PlatformError" in caplog.text
    nb_on.down = False
    assert _run()["sent"] == 3 and _uploaded(conn) == {"ld_biz_a", "ld_biz_a_2", "ld_biz_b"}


def test_each_business_sends_one_request_a_second_and_never_waits_for_another(conn, nb_on):
    """The adapter's pacer (Task 5a) paces the postbacks too: a second between two of one business, none between two
    businesses."""
    _account(conn, nb_on, "biz_a")
    _account(conn, nb_on, "biz_b")
    _lead(conn, "ld_a1", biz="biz_a", click="nvss_a1", age="3 minutes")
    _lead(conn, "ld_a2", biz="biz_a", click="nvss_a2", age="2 minutes")
    _lead(conn, "ld_b1", biz="biz_b", click="nvss_b1", age="1 minute")
    assert _run()["sent"] == 3
    assert len(nb_on.waits) == 1 and 0.9 < nb_on.waits[0] <= 1.0


# ---------------------------------------------------------------- the switch


def test_under_validate_the_upload_records_what_would_go_and_sends_nothing(conn, nb_on, monkeypatch):
    """Every NewsBreak write under `live` alone: the postback is one, so under `validate` the run records what it would
    send -- how many leads per business, and which (fix round 1, item 6) -- calls nothing, and leaves the lead for the
    day the switch is `live`."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    _account(conn, nb_on)
    _lead(conn, "ld_a", click="nvss_a")
    out = _run()
    assert out["modes"] == {NB: "validate"} and nb_on.sent == [] and _uploaded(conn) == set()
    assert (out["sent"], out["validated"]) == (0, 1)
    assert (out["would_send"], out["would_send_leads"]) == ({BIZ: 1}, ["ld_a"])


def test_under_validate_the_record_is_counts_per_business_and_twenty_leads_at_most(conn, nb_on, monkeypatch):
    """Fix round 1, item 6 (review M4): a validate run lists the same leads every hour for their 7 days, so it keeps
    counts per business and at most the first 20 lead ids -- never every lead, and never a click id."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    for biz, n in (("biz_a", 22), ("biz_b", 3)):
        _account(conn, nb_on, biz)
        for k in range(n):
            _lead(conn, f"ld_{biz}_{k:02d}", biz=biz, click=f"nvss_{biz}_{k}", age=f"{100 - k} minutes")
    out = _run()
    assert out["validated"] == 25 and out["would_send"] == {"biz_a": 22, "biz_b": 3}
    assert out["would_send_leads"] == [f"ld_biz_a_{k:02d}" for k in range(20)]
    assert "nvss" not in str(out) and nb_on.requests == []


def test_under_validate_no_lead_event_is_looked_up(conn, nb_on, monkeypatch):
    """Fix round 1, item 7 (review M5): `validate` calls NewsBreak for nothing -- not even the lead event's lookup of an
    account whose event is not known yet (a read, but the upload's only reason to make it is a postback)."""
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "validate")
    _account(conn, nb_on, known=False)
    _lead(conn, "ld_a")
    out = _run()
    assert out["validated"] == 1 and nb_on.requests == [] and "event_id" not in _external(conn)


def test_with_the_switch_off_nothing_is_read_or_sent(conn, nb_on, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_NEWSBREAK", "off")
    _account(conn, nb_on)
    _lead(conn, "ld_a")
    out = _run()
    assert out["modes"] == {} and out["ops_by_channel"] == {} and nb_on.sent == [] and _uploaded(conn) == set()


# ---------------------------------------------------------------- T6-4: NewsBreak's day budget and its throttles


def _sent_today(conn, job: str, newsbreak_requests: int) -> None:
    """A run of `job` today that sent NewsBreak this many requests."""
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES (%s, now(), true, %s)",
        (job, Jsonb({"ops_by_channel": {NB: newsbreak_requests}})),
    )


def test_at_newsbreaks_day_budget_the_upload_waits_for_the_next_utc_day(conn, nb_on):
    """T6-4: NewsBreak's requests of the day (`ops_by_channel.newsbreak` of every ops job, this one's included) at
    `NB_QPD_BUDGET` hold the upload for the rest of the UTC day: nothing is sent, the run says so, and the leads wait.
    One request short of it, one more postback goes and the next waits."""
    assert jobs.NB_QPD_BUDGET == 691_200  # 80 % of NewsBreak's 864,000 requests a day per developer app
    _account(conn, nb_on)
    _lead(conn, "ld_a", click="nvss_a", age="2 minutes")
    _lead(conn, "ld_b", click="nvss_b", age="1 minute")
    _sent_today(conn, "conversion-upload", jobs.NB_QPD_BUDGET - 1)
    out = _run()
    assert (out["sent"], out["stopped_by_channel"]) == (1, {NB: "ops budget"}) and _postbacks(nb_on) == ["nvss_a"]
    nb_on.sent.clear()
    out = _run()
    assert (out["sent"], out["stopped_by_channel"]) == (0, {NB: "ops budget"}) and nb_on.sent == []
    conn.execute("UPDATE job_run SET started_at = started_at - interval '1 day'")  # the next UTC day
    assert _run()["sent"] == 1 and _uploaded(conn) == {"ld_a", "ld_b"}


def test_two_throttled_answers_in_a_row_hold_the_upload_for_the_rest_of_the_utc_day(conn, nb_on):
    """T6-4: two throttled answers in a row (`NewsBreakError.throttled`; a 429 on the postback here) stop NewsBreak's
    work for the rest of the UTC day: the third business is not tried, the run says `throttled`, and a later run that
    day sends nothing though NewsBreak answers again; the next day it all goes."""
    for biz in ("biz_a", "biz_b", "biz_c"):
        _account(conn, nb_on, biz)
        _lead(conn, f"ld_{biz}", biz=biz, click=f"nvss_{biz}")
    nb_on.fail["tracking/attribute"] = httpx.Response(429, text="slow down")
    out = _run()
    assert (out["sent"], out["errors"], out["stopped_by_channel"]) == (0, 2, {NB: "throttled"})
    assert _postbacks(nb_on) == ["nvss_biz_a", "nvss_biz_b"] and out["ops_by_channel"] == {NB: 2}
    del nb_on.fail["tracking/attribute"]
    nb_on.sent.clear()
    out = _run()
    assert (out["sent"], out["stopped_by_channel"]) == (0, {NB: "throttled"}) and nb_on.sent == []
    conn.execute("UPDATE job_run SET started_at = started_at - interval '1 day'")
    assert _run()["sent"] == 3


def test_a_database_failure_on_one_business_never_costs_the_run_its_newsbreak_count(conn, nb_on, monkeypatch, caplog):
    """Fix round 1, item 5 (review M3): a database error after a business's postback went (its audit row here) is that
    business's error alone -- logged by its type, never its words -- the next business is sent, and the run's details
    keep every NewsBreak request it made, so the day's budget counts them."""
    for biz in ("biz_a", "biz_b"):
        _account(conn, nb_on, biz)
        _lead(conn, f"ld_{biz}", biz=biz, click=f"nvss_{biz}")
    real = jobs.audit.record_job

    def failing(conn_, *, job, tool, business_id, args, result):
        if business_id == "biz_a":
            raise RuntimeError("the database went away with words of its own")
        return real(conn_, job=job, tool=tool, business_id=business_id, args=args, result=result)

    monkeypatch.setattr(jobs.audit, "record_job", failing)
    with caplog.at_level(logging.WARNING):
        out = _run()
    assert (out["sent"], out["errors"], out["ops_by_channel"]) == (2, 1, {NB: 2}) and _postbacks(nb_on) == [
        "nvss_biz_a",
        "nvss_biz_b",
    ]
    assert "RuntimeError" in caplog.text and "words of its own" not in caplog.text
    recorded = conn.execute("SELECT ok, details FROM job_run WHERE job = 'conversion-upload'").fetchone()
    assert recorded["ok"] is True and recorded["details"]["ops_by_channel"] == {NB: 2}


def test_one_throttled_answer_alone_holds_nothing(conn, nb_on):
    """A single throttle (NewsBreak's per-minute limit, say) costs that one business its turn this hour; the next
    business answers and the run goes on."""
    for biz in ("biz_a", "biz_b"):
        _account(conn, nb_on, biz)
        _lead(conn, f"ld_{biz}", biz=biz, click=f"nvss_{biz}")
    nb_on.fail["tracking/attribute"] = httpx.Response(429, text="slow down")
    postbacks = []

    def lifted_after_the_first(request):  # runs as each request arrives, before NewsBreak's answer is chosen
        if request.url.path.endswith("tracking/attribute"):
            postbacks.append(request)
            if len(postbacks) == 2:
                nb_on.fail.pop("tracking/attribute")

    nb_on.on_request.append(lifted_after_the_first)
    out = _run()
    assert (out["sent"], out["errors"]) == (1, 1) and "stopped_by_channel" not in out
    assert _uploaded(conn) == {"ld_biz_b"}


# ---------------------------------------------------------------- the token stays in the settings


def test_the_token_never_reaches_the_details_the_audit_or_the_log(conn, nb_on, caplog):
    """The token never leaves the settings: an event lookup NewsBreak refuses with words that echo it, and a successful
    upload, leave it out of the run's details, the audit rows and the log."""
    _account(conn, nb_on, "biz_a", known=False)
    nb_on.fail["event/getList"] = envelope(40001, f"bad request for {TOKEN}")
    _lead(conn, "ld_a", biz="biz_a", click="nvss_a")
    _account(conn, nb_on, "biz_b")
    _lead(conn, "ld_b", biz="biz_b", click="nvss_b")
    with caplog.at_level(logging.DEBUG):
        out = _run()
    assert (out["sent"], out["errors"]) == (1, 1)
    details = conn.execute("SELECT details::text AS d FROM job_run WHERE job = 'conversion-upload'").fetchone()["d"]
    audited = " ".join(
        r["t"] for r in conn.execute("SELECT result::text || args_redacted::text AS t FROM agent_action")
    )
    for text in (details, audited, caplog.text):
        assert not any(secret in text for secret in ADS_SECRETS), text
