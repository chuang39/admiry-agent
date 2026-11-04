"""The creatives tool (hackathon plan 2026-11-04 Task 6; spec §3 step 3, Review Focus 1 and 4): the command
`ad_creatives` behind the public tool `suggest_ad_creatives`, the routes that serve a set's images by their
unguessable id, and the Google build carrying the owner's accepted set (ruling R13). The models are the studio tests'
fake; no network."""

from __future__ import annotations

import threading
import time

import pytest

from agent import config, uploads
from agent.channels.google_search import adapter as google_adapter
from agent.creatives import meta as metamod
from agent.creatives import studio
from agent.surface import gate, registry
from agent.tests.conftest import call_tool, data, rpc, tool_text
from agent.tests.fixtures import business_with_key
from agent.tests.test_creatives_google_ops import _roofer
from agent.tests.test_creatives_studio import FakeMeta, business, photo_bytes

BIZ = "biz_roof"
TOOL = "suggest_ad_creatives"


@pytest.fixture
def launched(monkeypatch):
    """start's thread replaced: the test runs `studio.run` itself, with the fake."""
    calls: list[str] = []
    monkeypatch.setattr(studio, "_launch", lambda settings, set_id: calls.append(set_id))
    return calls


def owner(conn, photos=(), biz=BIZ) -> str:
    """The business (its knowledge, its page and `photos`) and a key for it."""
    business(conn, biz, photos=photos)
    return business_with_key(conn, biz_id=biz)


def call(client, key, **args) -> dict:
    res = call_tool(client, TOOL, args, key=key)
    assert not res["isError"], tool_text(res)
    return res["structuredContent"]


def ready(client, conn, launched, key) -> dict:
    """A first call (the set is `making`), the run with the fake, and the second call's card."""
    first = data(call_tool(client, TOOL, {}, key=key))
    assert first["status"] == "making" and launched[-1] == first["set_id"]
    studio.run(config.load(), first["set_id"], meta=FakeMeta())
    return call(client, key)


def sets(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM ad_creative_set").fetchone()["n"]


def end_runs(conn) -> None:
    """Every `making` set ended, as its run would end it: a new one may start."""
    conn.execute("UPDATE ad_creative_set SET status = 'failed', error = 'ended' WHERE status = 'making'")


def test_no_photos_returns_upload_link_and_calls_no_model(client, conn, launched, monkeypatch):
    """The upload link is the one create_page or build_site minted, read back (ruling R24): the studio never mints."""
    clients: list[object] = []
    monkeypatch.setattr(metamod, "client", lambda settings: clients.append(settings))
    key = owner(conn)
    token = uploads.new_token(conn, f"pg_{BIZ}")  # as create_page would
    answer = call(client, key)
    card = answer["data"]
    assert card["status"] == "needs_photos" and card["set_id"] is None and card["images"] == []
    assert card["upload_url"] == f"{config.load().web_base_url}/u/{token}"
    assert card["upload_url"] in answer["say"]  # the owner is given the link itself
    assert answer["assistant_notes"] == card["error"] == studio.NEEDS_PHOTOS
    # and again (refresh too): the same link, still nothing made
    assert call(client, key, refresh=True)["data"]["upload_url"] == card["upload_url"]
    assert launched == [] and clients == [] and sets(conn) == 0


def test_no_photos_and_no_valid_link_names_the_tool_that_gives_one(client, conn, launched):
    key = owner(conn)
    token = uploads.new_token(conn, f"pg_{BIZ}")
    conn.execute("UPDATE upload_token SET expires_at = now() - interval '1 minute' WHERE token = %s", (token,))
    answer = call(client, key)
    card = answer["data"]
    assert card["status"] == "needs_photos" and card["upload_url"] is None
    assert "update_landing_page" in card["error"] and answer["assistant_notes"] == card["error"]
    assert "/u/" not in answer["say"] and "update_landing_page" not in answer["say"]  # the owner's words name no tool
    assert launched == [] and sets(conn) == 0


def test_first_call_returns_making_within_2s(client, conn, monkeypatch):
    key = owner(conn, photos=[photo_bytes()])
    started = threading.Event()

    def slow(settings, set_id, *, meta=None):  # the real run takes one to four minutes
        started.set()
        time.sleep(5)

    monkeypatch.setattr(studio, "run", slow)
    t0 = time.monotonic()
    answer = call(client, key)
    took = time.monotonic() - t0
    card = answer["data"]
    assert took < 2, took
    assert card["status"] == "making" and card["set_id"].startswith("cs_")
    assert started.wait(2)  # the run did start, on a thread of its own
    assert "30 seconds" in answer["assistant_notes"]  # the assistant is told to call again
    # called again while it is making: the same set, at once, and no second run
    t0 = time.monotonic()
    again = call(client, key)["data"]
    assert time.monotonic() - t0 < 2
    assert again["status"] == "making" and again["set_id"] == card["set_id"] and sets(conn) == 1


def test_a_poll_fails_a_dead_making_set_and_starts_nothing(client, conn, launched):
    """A run lives at most studio.GIVE_UP_S; a set still `making` after studio.STALE_S lost its process (a deploy):
    the next poll fails it with the owner's sentence and answers with that failed card -- a new paid run waits for the
    owner's yes (refresh)."""
    key = owner(conn, photos=[photo_bytes()])
    dead = call(client, key)["data"]["set_id"]
    conn.execute("UPDATE ad_creative_set SET created_at = now() - interval '11 minutes' WHERE id = %s", (dead,))
    answer = call(client, key)
    card = answer["data"]
    assert (card["status"], card["set_id"], card["error"]) == ("failed", dead, studio.DIED)
    assert launched == [dead] and sets(conn) == 1
    assert studio.DIED in answer["say"] and "refresh" in answer["assistant_notes"]
    assert call(client, key)["data"]["set_id"] == dead  # polling again starts nothing either
    fresh = call(client, key, refresh=True)["data"]  # the owner said yes
    assert fresh["status"] == "making" and launched == [dead, fresh["set_id"]]


def test_new_sets_are_capped_per_day_and_polls_of_a_live_set_are_free(client, conn, launched):
    key = owner(conn, photos=[photo_bytes()])
    cap = config.load().creatives_per_day
    assert cap == 10  # the default
    for n in range(cap):
        made = call(client, key, refresh=True)["data"]
        assert made["status"] == "making" and len(launched) == n + 1
        if n < cap - 1:
            end_runs(conn)
    # at the ceiling, with its last set still making: polls, and a refresh while it makes, answer with that set
    for args in ({}, {"refresh": True}, {}):
        assert call(client, key, **args)["data"]["set_id"] == made["set_id"], args
    end_runs(conn)
    res = call_tool(client, TOOL, {"refresh": True}, key=key)
    assert res["isError"] and studio.DAILY_LIMIT.format(cap=cap) in tool_text(res)
    assert sets(conn) == cap and len(launched) == cap
    # yesterday's (UTC) sets are not today's
    conn.execute("UPDATE ad_creative_set SET created_at = created_at - interval '1 day'")
    assert call(client, key, refresh=True)["data"]["status"] == "making" and len(launched) == cap + 1


def test_a_draft_key_starts_fewer_sets_a_day(client, conn, launched, monkeypatch):
    business(conn, BIZ, photos=[photo_bytes()])
    key = business_with_key(conn, biz_id=BIZ, cls="draft")
    cap = config.load().creatives_per_day_draft
    assert cap == 3  # the default
    for _ in range(cap):
        assert call(client, key, refresh=True)["data"]["status"] == "making"
        end_runs(conn)
    res = call_tool(client, TOOL, {"refresh": True}, key=key)
    assert res["isError"] and studio.DAILY_LIMIT.format(cap=cap) in tool_text(res)
    monkeypatch.setenv("AGENT_CREATIVES_PER_DAY_DRAFT", "4")  # the ceiling is the settings' own
    assert call(client, key, refresh=True)["data"]["status"] == "making" and len(launched) == cap + 1


def test_second_call_returns_ready_card(client, conn, launched):
    key = owner(conn, photos=[photo_bytes()])
    answer = ready(client, conn, launched, key)
    card = answer["data"]
    assert card["status"] == "ready" and card["set_id"] == launched[0]
    assert set(card) >= {"status", "set_id", "steps", "images", "text", "removed", "models", "upload_url"}
    base = config.load().web_base_url
    assert sorted(i["format"] for i in card["images"]) == ["landscape", "square"]
    for img in card["images"]:
        assert set(img) == {"id", "format", "url", "original_url", "method", "checks"}
        assert img["url"] == f"{base}/creatives/{img['id']}" and img["original_url"] == f"{img['url']}/original"
    heads = card["text"]["headlines"]
    assert heads and all(h["text"] and h["source"] for h in heads)
    assert {"text": "Licensed & Insured", "why": "checked (rule: home_services/compliance: licence)"} in card["removed"]
    assert card["steps"] and {s["model"] for s in card["steps"]} >= {metamod.SPARK, metamod.SAM, metamod.WONDER}
    assert "ready" in answer["say"] and "accept_set_id" in answer["assistant_notes"]
    assert launched == [card["set_id"]]  # the second call made nothing new
    # refresh: a new set, making
    fresh = call(client, key, refresh=True)["data"]
    assert fresh["status"] == "making" and fresh["set_id"] != card["set_id"] and launched[-1] == fresh["set_id"]


def test_accept_marks_accepted(client, conn, launched):
    key = owner(conn, photos=[photo_bytes()])
    set_id = ready(client, conn, launched, key)["data"]["set_id"]
    # another business's key cannot accept it, nor can an id that is no set
    other = owner(conn, biz="biz_other")
    res = call_tool(client, TOOL, {"accept_set_id": set_id}, key=other)
    assert res["isError"] and studio.NOT_YOURS in tool_text(res)
    res = call_tool(client, TOOL, {"accept_set_id": "cs_nosuchset"}, key=key)
    assert res["isError"] and studio.NOT_YOURS in tool_text(res)
    assert studio.accepted(conn, BIZ) is None
    answer = call(client, key, accept_set_id=set_id)
    assert answer["data"]["status"] == "accepted" and answer["data"]["set_id"] == set_id
    assert "accepted" in answer["say"]
    row = conn.execute("SELECT status, accepted_at FROM ad_creative_set WHERE id = %s", (set_id,)).fetchone()
    assert row["status"] == "accepted" and row["accepted_at"] is not None
    assert studio.accepted(conn, BIZ)["set_id"] == set_id
    # accepting it again changes nothing; a set still making is not accepted
    assert call(client, key, accept_set_id=set_id)["data"]["status"] == "accepted"
    making = call(client, key, refresh=True)["data"]["set_id"]
    res = call_tool(client, TOOL, {"accept_set_id": making}, key=key)
    assert res["isError"] and studio.NOT_READY in tool_text(res)
    # with a newer set making, the answer to accepting is still the accepted set's card, not the newest set's
    again = call(client, key, accept_set_id=set_id)
    assert (again["data"]["set_id"], again["data"]["status"]) == (set_id, "accepted") and again["data"]["images"]
    assert "accepted" in again["say"]


def test_image_route_serves_bytes_and_404s_unknown(client, conn, launched):
    key = owner(conn, photos=[photo := photo_bytes()])
    card = ready(client, conn, launched, key)["data"]
    base = config.load().web_base_url
    for img in card["images"]:
        row = conn.execute("SELECT bytes, content_type FROM ad_creative_image WHERE id = %s", (img["id"],)).fetchone()
        got = client.get(img["url"].removeprefix(base))
        assert got.status_code == 200 and got.content == bytes(row["bytes"])
        assert got.headers["content-type"] == row["content_type"]
        original = client.get(img["original_url"].removeprefix(base))
        assert original.status_code == 200 and original.content == photo  # the owner's own photo, as uploaded
        assert original.headers["content-type"] == "image/jpeg"
    for path in ("/creatives/ci_0000000000000000", "/creatives/ci_0000000000000000/original", "/creatives/x"):
        assert client.get(path).status_code == 404, path


def test_tool_listed_in_public_surface(client, conn, strict_surface):
    key = business_with_key(conn, claim_state="claimed")
    listed = {t["name"]: t for t in rpc(client, "tools/list", key=key)["result"]["tools"]}
    assert TOOL in listed and "ad_creatives" not in listed  # the command behind it stays hidden
    tool = listed[TOOL]
    assert set(tool["inputSchema"]["properties"]) == {"accept_set_id", "refresh"}
    assert not tool["inputSchema"].get("required")
    notes = tool["annotations"]
    assert notes["readOnlyHint"] is False and notes["destructiveHint"] is False
    assert notes["idempotentHint"] is False  # refresh makes a new set, as create_marketing_plan starts over
    meta = next(m for m in registry.SURFACE if m.name == TOOL)
    assert (meta.cls, meta.scope, meta.approval, meta.commands) == ("W", "manage", "none", ("ad_creatives",))
    assert TOOL in gate.PUBLIC and gate.SURFACE_VERSION == "2026-11-04.2"
    said = " ".join(tool["description"].split())
    for words in (
        "30 seconds",
        "source",
        "Removed",
        "ask the owner",
        "Never call it with accept_set_id without the owner's yes",
        "a paid run",
    ):
        assert words in said, words


# ---------------------------------------------------------------- ruling R13: the Google build carries the set


def build_payload(conn) -> dict:
    plan, knowledge = _roofer()
    return google_adapter.ADAPTER.build(conn, config.load(), BIZ, plan, knowledge, None).payload


def test_build_payload_carries_the_accepted_set(client, conn, launched):
    key = owner(conn, photos=[photo_bytes()])
    set_id = ready(client, conn, launched, key)["data"]["set_id"]
    assert build_payload(conn)["creatives"] is None  # ready, not accepted: nothing goes to Google
    call(client, key, accept_set_id=set_id)
    creatives = build_payload(conn)["creatives"]
    assert creatives == studio.accepted(conn, BIZ) and creatives["set_id"] == set_id
    assert creatives["headlines"] and len(creatives["images"]) == 2
    assert all(isinstance(img["bytes"], bytes) for img in creatives["images"])


def test_build_payload_creatives_none_without_a_set(conn):
    business(conn, BIZ, photos=[photo_bytes()])
    assert build_payload(conn)["creatives"] is None
