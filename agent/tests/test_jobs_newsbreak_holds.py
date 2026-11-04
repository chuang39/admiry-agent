"""ads-sync under NewsBreak's day holds (M1 Task 6 fix round 1, item 1): the hourly read's own stops never wait for the
next UTC day. Under either hold -- Admiry's own day budget (`NB_QPD_BUDGET`) or two throttled answers in a row --
ads-sync still runs a stops-only pass over NewsBreak's rows: the ads on their way out (`leftover`, `replaced`) turned
OFF, and a paused row whose campaign NewsBreak shows ON owing NewsBreak its pause again (`pause_owed`, which the hourly
ads-launch sends past either hold). It reads only what those stops need -- the ads' and the campaign's status -- and
writes nothing else: no metrics, no review, no month's cap, no end date. And ads-launch's own owed stop (the M1
follow-up's fix round 1): a refused version's stop NewsBreak did not take goes out under either hold, alone. Over
`FakeNewsBreak`, through the adapter."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from agent import ads_run, config, db, jobs, tick
from agent.channels.newsbreak import adapter as nb
from agent.channels.newsbreak import client as newsbreak
from agent.tests.conftest import TEST_DB_URL
from agent.tests.test_newsbreak_adapter import (  # noqa: F401 - images, launched and nb_live are fixtures
    BIZ,
    NB,
    NEW_HEADLINES,
    _cut_after_the_campaigns_on,
    _left_on,
    add_plan,
    answer_switch,
    images,
    launched,
    nb_live,
    nb_row,
    nb_sections,
    queue,
    refuse_on,
    seed,
    step,
    switched,
)


def _held_by_the_budget(conn) -> None:
    """NewsBreak's requests of this UTC day at Admiry's own budget."""
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-launch', now(), true, %s)",
        (Jsonb({"ops_by_channel": {NB: jobs.NB_QPD_BUDGET}}),),
    )


def _held_by_throttles(conn) -> None:
    """A run earlier this UTC day stopped NewsBreak after two throttled answers in a row."""
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-sync', now(), true, %s)",
        (Jsonb({"ops_by_channel": {NB: 2}, "stopped_by_channel": {NB: "throttled"}}),),
    )


def _sync() -> dict:
    return tick.run("ads-sync", url=TEST_DB_URL)


def _metric_rows(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM metric_daily WHERE channel = %s", (NB,)).fetchone()["n"]


def test_under_the_budget_hold_ads_sync_still_turns_off_a_left_over_ad(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """The reviewer's probe: an ad left over from a change that did not go through, ON at NewsBreak, and NewsBreak's
    requests of the day at Admiry's own budget. The hourly read's OFF is a stop: ads-sync reads the ads' status, turns
    that ad OFF and lists it no more -- and reads nothing else (no report, no campaign or ad-set list), writes no metric
    and leaves the row's read time alone; the run says NewsBreak is held and counts the two requests."""
    left = _left_on(conn, nb_live)
    synced = nb_row(conn)["synced_at"]
    _held_by_the_budget(conn)
    nb_live.sent.clear()
    out = _sync()
    assert nb_live.kinds() == ["ad/getList", "ad/updateStatus"] and nb_live.ads[left]["status"] == "OFF"
    assert left not in nb_row(conn)["resources"].get("leftover", [])
    assert out["stopped_by_channel"] == {NB: "ops budget"} and out["ops_by_channel"][NB] == 2
    assert _metric_rows(conn) == 0 and nb_row(conn)["synced_at"] == synced


def test_under_the_budget_hold_ads_sync_still_spots_a_paused_row_newsbreak_serves(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """A row Admiry shows paused whose campaign NewsBreak shows ON (someone switched it on there): spotting it is a stop
    -- ads-sync reads the campaign's status alone and owes NewsBreak the pause again (`pause_owed`, the adapter's
    sentence) -- and the next ads-launch, held too, still sends the pause."""
    conn.execute(
        "UPDATE channel_campaign SET state = 'paused', paused_reason = 'no_funds' WHERE business_id = %s AND channel = %s",
        (BIZ, NB),
    )
    campaign = nb_row(conn)["resources"]["campaign"]
    assert nb_live.campaigns[campaign]["status"] == "ON"
    _held_by_the_budget(conn)
    nb_live.sent.clear()
    out = _sync()
    row = nb_row(conn)
    assert nb_live.kinds() == ["campaign/getList"] and out["stopped_by_channel"] == {NB: "ops budget"}
    assert (row["state"], row["resources"].get("pause_owed"), row["last_error"]) == (
        "paused",
        True,
        nb.WORDS.pause_owed,
    )
    nb_live.sent.clear()
    tick.run("ads-launch", url=TEST_DB_URL)
    assert nb_live.kinds() == ["campaign/updateStatus"] and nb_live.campaigns[campaign]["status"] == "OFF"
    assert "pause_owed" not in nb_row(conn)["resources"]


def test_under_a_hold_newsbreak_serving_a_paused_row_tells_an_operator_once_each_time_as_the_whole_read_does(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
    resend,
):
    """Final review (a deferred minor): the stops-only pass that finds NewsBreak serving a row Admiry shows paused sends
    the operator the mail the whole read sends, once per occurrence and under the same mark (`resources.told`): once
    while it stands, however many held hours; again only when NewsBreak serves the paused ads anew."""
    conn.execute(
        "UPDATE channel_campaign SET state = 'paused', paused_reason = 'no_funds' "
        "WHERE business_id = %s AND channel = %s",
        (BIZ, NB),
    )
    campaign = nb_row(conn)["resources"]["campaign"]
    _held_by_the_budget(conn)
    _sync()
    _sync()  # the pause is owed now: the pass has nothing to look at
    (mail,) = resend.sent
    assert mail["to"] == ["ops@example.com"] and "run at NewsBreak" in mail["subject"]
    assert nb_row(conn)["resources"]["told"][nb.RUNNING_THERE] == {"n": 1, "open": True}
    tick.run("ads-launch", url=TEST_DB_URL)  # the owed pause goes out
    _sync()  # NewsBreak shows the campaign OFF: the occurrence is over
    assert len(resend.sent) == 1 and nb_row(conn)["resources"]["told"][nb.RUNNING_THERE]["open"] is False
    nb_live.campaigns[campaign]["status"] = "ON"  # switched on in Ads Manager again
    _sync()
    assert len(resend.sent) == 2 and resend.keys[0] != resend.keys[1]


def test_the_stops_only_pass_leaves_a_business_another_step_holds_to_the_next_pass(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """Final review (a deferred minor): the stops-only pass takes the business's step lock like every lifecycle step --
    while another step holds it, the pass answers `busy` and sends nothing; the next pass sends the stop."""
    left = _left_on(conn, nb_live)
    nb_live.sent.clear()
    with db.connect(TEST_DB_URL) as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"ads:{BIZ}",))
        out = ads_run.sync_stops(conn, config.load(), BIZ, NB)
        other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"ads:{BIZ}",))
    assert out.get("busy") is True and nb_live.sent == [] and left in nb_row(conn)["resources"]["leftover"]
    out = ads_run.sync_stops(conn, config.load(), BIZ, NB)
    assert "busy" not in out and nb_live.ads[left]["status"] == "OFF"


def test_under_the_throttle_hold_the_stops_are_tried_and_a_throttle_leaves_them_for_the_next_run(
    conn,
    nb_live,  # noqa: F811 - the fixture of the same name
    launched,  # noqa: F811 - the fixture of the same name
):
    """The controller's revised ruling: a 4034 may be NewsBreak's per-minute limit, which the client cannot tell from
    the daily one, and a stop that fails costs one request -- so under the throttle hold the stops are still tried. A
    throttled answer leaves the ad ON and listed, an error of the run; the next run turns it OFF."""
    left = _left_on(conn, nb_live)
    _held_by_throttles(conn)
    nb_live.throttle = 1
    nb_live.sent.clear()
    out = _sync()
    assert (out["errors"], out["stopped_by_channel"]) == (1, {NB: "throttled"})
    assert nb_live.ads[left]["status"] == "ON" and left in nb_row(conn)["resources"]["leftover"]
    nb_live.sent.clear()
    out = _sync()
    assert nb_live.kinds() == ["ad/getList", "ad/updateStatus"] and nb_live.ads[left]["status"] == "OFF"


def test_a_business_reached_after_the_budget_ran_out_mid_run_still_gets_its_stops(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """The budget can run out during the run: the business read before it gets the whole read; the one after, only its
    stops."""
    left = _left_on(conn, nb_live)  # biz_1's left-over ad, ON
    v0 = seed(conn, biz="biz_0")  # a business read first (its id sorts first) with nothing on its way out
    queue(conn, v0, biz="biz_0")
    assert ads_run.launch(conn, config.load(), "biz_0", NB)["state"] == "live"
    conn.execute(
        "INSERT INTO job_run (job, started_at, ok, details) VALUES ('ads-launch', now(), true, %s)",
        (Jsonb({"ops_by_channel": {NB: jobs.NB_QPD_BUDGET - 4}}),),  # room for one whole read: four requests
    )
    nb_live.sent.clear()
    out = _sync()
    assert nb_live.kinds() == [
        "reports/getIntegratedReport",  # biz_0: the whole read
        "ad/getList",
        "campaign/getList",
        "ad-set/getList",
        "ad/getList",  # biz_1: its stops alone
        "ad/updateStatus",
    ]
    assert nb_live.ads[left]["status"] == "OFF" and out["stopped_by_channel"] == {NB: "ops budget"}


def test_under_the_throttle_hold_ads_launch_still_sends_an_owed_stop_alone(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """The M1 follow-up's fix round 1 (F2, the review's probe): a refused version's stop NewsBreak did not take is owed
    -- a stop, so the hourly ads-launch sends it under NewsBreak's day holds too (two throttles in a row here), alone,
    and the row fails with the campaign OFF rather than serving for the rest of the UTC day."""
    _cut_after_the_campaigns_on(conn, nb_live)
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    assert step(conn)["state"] == "launching"
    back()
    _held_by_throttles(conn)
    nb_live.sent.clear()
    out = tick.run("ads-launch", url=TEST_DB_URL)
    row = nb_row(conn)
    assert (out["stopped_by_channel"], nb_live.kinds()) == ({NB: "throttled"}, ["campaign/updateStatus"])
    assert (row["state"], row["last_error"], switched(nb_live)[0]) == ("failed", nb.TOO_LITTLE_COPY, "OFF")


def test_under_the_throttle_hold_a_refused_enables_owed_stop_still_goes_out_alone(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """Fix round 2 (G1, the re-review's probe A): a resume NewsBreak refuses (the ad set's switch) whose stop it answered
    busy -- a busy answer itself feeds the throttle hold -- is owed like any refused row's stop, never left for the
    enable to run again: the hourly ads-launch sends it under the hold, alone, and the row fails with the campaign OFF
    rather than serving for the rest of the UTC day."""
    ads_run.pause(conn, config.load(), BIZ, NB, by="owner")
    refuse_on(nb_live, "ad-set")
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    assert ads_run.resume(conn, config.load(), BIZ, NB)["state"] == "launching"
    back()
    _held_by_throttles(conn)
    nb_live.sent.clear()
    out = tick.run("ads-launch", url=TEST_DB_URL)
    row = nb_row(conn)
    refused = newsbreak.REFUSED_WITH_REASON.format(41926)
    assert (out["stopped_by_channel"], nb_live.kinds()) == ({NB: "throttled"}, ["campaign/updateStatus"])
    assert (row["state"], row["last_error"], switched(nb_live)[0]) == ("failed", refused, "OFF")


def test_under_the_hold_an_approval_keeps_the_owed_stop_and_launches_once_it_lifts(conn, nb_live, launched):  # noqa: F811 - the fixtures of the same name
    """Fix round 2 (G2, the re-review's probe B): a newer approval while a refused row's stop is owed keeps it owed --
    a stop, so under the throttle hold ads-launch sends that OFF alone; the refused version no longer the approved one,
    nothing fails and the row stays `launching` with its campaign OFF; once the hold lifts, the newer version
    launches."""
    _cut_after_the_campaigns_on(conn, nb_live)
    add_plan(conn, nb_sections(headlines=["x" * 91]))
    back = answer_switch(nb_live, "campaign", "OFF", 4034)
    assert step(conn)["state"] == "launching"
    back()
    v3 = add_plan(conn, nb_sections(headlines=NEW_HEADLINES))
    queue(conn, v3)  # the approval
    _held_by_throttles(conn)
    nb_live.sent.clear()
    tick.run("ads-launch", url=TEST_DB_URL)
    row = nb_row(conn)
    assert (nb_live.kinds(), row["state"], switched(nb_live)[0]) == (["campaign/updateStatus"], "launching", "OFF")
    assert "stop_owed" not in row["resources"] and row["last_error"] is None
    conn.execute("DELETE FROM job_run")  # the next UTC day: no hold
    tick.run("ads-launch", url=TEST_DB_URL)
    row = nb_row(conn)
    assert (row["state"], row["plan_id"], switched(nb_live)[0]) == ("live", v3, "ON")
